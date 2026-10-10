"""Exact typed full-card receipt authority rebuilt in the financial snapshot."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Protocol

from finance_core.application.ai_expense_assessment import BASIS, assess_independent_ai_expense_v2
from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.bookkeeping_metadata import (
    build_application_receipt_projection,
    read_receipt_bookkeeping_metadata,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.parser_proposals.independent_ai_validation import sha
from finance_core.parser_proposals.repository import ParserProposalRepository


@dataclass(frozen=True)
class ExpectedReceiptPostingAuthorization:
    command: dict[str, Any]
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    confirmation_public_id: str
    source_event_key: str
    authenticated_actor_id: str
    payer_participant_public_id: str
    participants: tuple[tuple[str, int], ...]
    reconstructed_projection_hash: str
    existing_conversion: dict[str, Any] | None


@dataclass(frozen=True)
class VerifiedReceiptPostingAuthorization:
    expected: ExpectedReceiptPostingAuthorization
    basis: str
    accepted_attempt_id: str
    posting_review_id: str
    posting_review_hash: str
    decision_id: str
    decision_digest: str
    display_digest: str
    eligibility_digest: str


class ReceiptPostingAuthorization(Protocol):
    def verify_in_transaction(
        self, connection, expected: ExpectedReceiptPostingAuthorization
    ) -> VerifiedReceiptPostingAuthorization: ...


def receipt_projection(fields, payer, metadata):
    amount, currency = fields["canonical_amount"], fields["currency"]
    calculation = calculate_receipt_split(
        {
            "currency": currency,
            "participants": [payer],
            "payer": payer,
            "receipts": [
                {
                    "merchant": fields["merchant"],
                    "paid_by": payer,
                    "net_paid": amount,
                    "items": [
                        {"description": "Receipt total", "amount": amount, "owners": [payer]}
                    ],
                }
            ],
        }
    )
    return build_application_receipt_projection(
        bookkeeping_metadata=metadata,
        merchant=fields["merchant"],
        receipt_date=fields["receipt_date"],
        currency=currency,
        payer_participant_public_id=payer,
        calculation=calculation,
    )


def require_conversion_authority(conn, command, existing, authority, metadata=None):
    from finance_core.parser_proposals.effective_payload import resolve_effective_payload
    from finance_core.parser_proposals.receipt_facts_conversion import (
        _require_bound_confirmation,
        _require_proposal,
        resolve_independent_receipt_payload_fields,
    )

    proposal = ParserProposalRepository(conn).get_lineage_row_by_id(
        _require_proposal(conn, command.proposal_public_id)["id"]
    )
    # Use the sealed invocation source; fixed port must reauthenticate it.
    row = conn.execute(
        "SELECT a.material_json FROM independent_ai_attempts a JOIN "
        "independent_ai_publications p ON p.attempt_id=a.attempt_id WHERE "
        "p.child_parser_output_id IN (SELECT id FROM parser_outputs WHERE "
        "source_public_id=?) ORDER BY p.published_at DESC LIMIT 1",
        (proposal["source_public_id"],),
    ).fetchone()
    if row is None:
        return None
    import json

    source = json.loads(row[0])["source"]
    assessment = assess_independent_ai_expense_v2(conn, proposal, source)
    if assessment is None:
        return None
    if authority is None or assessment["hard_blockers"]:
        raise AiInvocationError("Full-card receipt requires fixed accepted posting authorization")
    payload, _, version = resolve_effective_payload(conn, proposal)
    fields = resolve_independent_receipt_payload_fields(conn, payload)
    projection = receipt_projection(fields, command.payer_participant_public_id, metadata)
    _, confirmation = _require_bound_confirmation(conn, proposal, command.expected_content_hash)
    entries = tuple(
        (str(x["participant_public_id"]), int(x["is_included"])) for x in command.participants
    )
    from finance_core.application.admission import VerifiedSource
    from finance_core.application.posting import source_event_key

    expected = ExpectedReceiptPostingAuthorization(
        dataclasses.asdict(command),
        proposal["public_id"],
        version,
        command.expected_content_hash,
        confirmation["confirmation_public_id"],
        source_event_key(VerifiedSource(**source)),
        command.authenticated_actor_id,
        command.payer_participant_public_id,
        entries,
        sha(projection),
        existing,
    )
    from finance_core.application.posting import (
        PostingService,
        _AcceptedReceiptPostingAuthorization,
    )

    if (
        type(authority) is not _AcceptedReceiptPostingAuthorization
        or type(authority.service) is not PostingService
        or authority.service._conn is not conn
    ):
        raise AiInvocationError("Receipt authority is not the fixed Posting composition")
    proof = _AcceptedReceiptPostingAuthorization.verify_in_transaction(authority, conn, expected)
    if (
        type(proof) is not VerifiedReceiptPostingAuthorization
        or proof.expected != expected
        or proof.basis != BASIS
        or any(
            not isinstance(v, str) or not v
            for v in (
                proof.accepted_attempt_id,
                proof.posting_review_id,
                proof.posting_review_hash,
                proof.decision_id,
                proof.decision_digest,
                proof.display_digest,
                proof.eligibility_digest,
            )
        )
    ):
        raise AiInvocationError("Full-card receipt posting authorization does not verify")
    return proof


def require_receipt_posting_authority(conn, receipt_id, authority):
    from finance_core.parser_proposals.receipt_facts_conversion import (
        ReceiptFactsConversionCommand,
        _get_registry_row,
    )

    row = conn.execute(
        "SELECT "
        "c.command_public_id,c.authenticated_actor_id,p.public_id,"
        "c.proposal_content_hash,c.receipt_id FROM receipt_proposal_conversions c "
        "JOIN receipts r ON r.id=c.receipt_id "
        "JOIN parser_outputs p ON p.id=c.parser_output_id WHERE r.public_id=?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        return
    import json

    from finance_core.application.ai_invocation_contract import AI_INPUT_SCHEMA

    origins = conn.execute(
        "SELECT a.material_json FROM independent_ai_attempts a JOIN "
        "independent_ai_publications p ON p.attempt_id=a.attempt_id JOIN parser_outputs po "
        "ON po.id=p.child_parser_output_id WHERE po.source_public_id=(SELECT "
        "source_public_id FROM parser_outputs WHERE public_id=?)",
        (row[2],),
    ).fetchall()
    if not any(json.loads(origin[0])["schema"] == AI_INPUT_SCHEMA for origin in origins):
        return
    members = conn.execute(
        "SELECT p.public_id,rp.is_included,(p.id=r.payer_participant_id) FROM "
        "receipt_participants rp JOIN participants p ON p.id=rp.participant_id JOIN receipts "
        "r ON r.id=rp.receipt_id WHERE rp.receipt_id=? ORDER BY p.public_id",
        (row[4],),
    ).fetchall()
    payer = [m[0] for m in members if m[2]]
    if len(payer) != 1:
        raise AiInvocationError("Full-card receipt payer evidence absent")
    command = ReceiptFactsConversionCommand(
        command_public_id=row[0],
        proposal_public_id=row[2],
        expected_content_hash=row[3],
        payer_participant_public_id=payer[0],
        participants=tuple({"participant_public_id": m[0], "is_included": m[1]} for m in members),
        authenticated_actor_id=row[1],
        channel="independent_application",
    )
    return require_conversion_authority(
        conn,
        command,
        _get_registry_row(conn, row[0]),
        authority,
        read_receipt_bookkeeping_metadata(conn, receipt_id),
    )
