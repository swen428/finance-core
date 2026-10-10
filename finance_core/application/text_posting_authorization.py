"""Fixed accepted-human authority for owning v2 text writes and readback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from finance_core.application.ai_expense_assessment import BASIS, assess_independent_ai_expense_v2
from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.application.posting_contract import (
    POSTING_REVIEW_SCHEMA_V2,
    posting_review_sha256,
)
from finance_core.parser_proposals.independent_ai_validation import sha


@dataclass(frozen=True)
class ExpectedTextPostingAuthorization:
    parser_output_id: int
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    confirmation_public_id: str
    authenticated_actor_id: str
    confirmation_channel: str
    intake_public_id: str
    source_event_key: str
    accepted_attempt_id: str
    posting_review_id: str
    posting_review_hash: str
    decision_id: str
    decision_digest: str
    reconstructed_projection_hash: str
    eligibility_digest: str
    expected_transaction_public_id: str
    existing_conversion: dict[str, Any] | None


@dataclass(frozen=True)
class VerifiedTextPostingAuthorization:
    expected: ExpectedTextPostingAuthorization
    basis: str
    accepted_attempt_id: str
    posting_review_id: str
    posting_review_hash: str
    decision_id: str
    decision_digest: str
    display_digest: str
    eligibility_digest: str


class TextPostingAuthorization(Protocol):
    def verify_in_transaction(
        self, connection, expected: ExpectedTextPostingAuthorization
    ) -> VerifiedTextPostingAuthorization: ...


def require_text_posting_authority(conn, proposal, authorization, authority):
    """Reconstruct the exact expected identity before calling fixed authority.

    Verified v2 ancestry determines the domain, including when accepted records
    are absent. Static accepted mirrors are necessary, never human authenticity.
    """
    from finance_core.application.posting import (
        PostingService,
        _AcceptedTextPostingAuthorization,
        _sha,
    )
    from finance_core.parser_proposals.decision_owner import (
        _converted_transaction_public_id,
        _has_independent_posting_proof,
        _independent_conversion_fields,
        _read_accepted_posting_review,
        resolve_simple_expense_conversion_fields,
    )
    from finance_core.parser_proposals.effective_payload import resolve_effective_payload
    from finance_core.parser_proposals.repository import ParserConversionRepository

    origin = assess_independent_ai_expense_v2(conn, proposal, {})
    version = resolve_effective_payload(conn, proposal)[2]
    digest = authorization["proposal_content_hash"]
    review = _read_accepted_posting_review(
        conn,
        proposal=proposal,
        content_hash=digest,
        proposal_version=version,
        confirmation_public_id=authorization["confirmation_public_id"],
    )
    if origin is None:
        if review is not None and review["schema"] == POSTING_REVIEW_SCHEMA_V2:
            raise AiInvocationError("Full-card text review lacks matching v2 ancestry")
        return None
    if review is None or review["schema"] != POSTING_REVIEW_SCHEMA_V2:
        raise AiInvocationError("Full-card text requires exact accepted v2 review")
    if (
        type(authority) is not _AcceptedTextPostingAuthorization
        or type(authority.service) is not PostingService
        or authority.service._conn is not conn
        or not conn.in_transaction
    ):
        raise AiInvocationError("Text authority is not the fixed accepted Posting composition")
    row = conn.execute(
        "SELECT a.*,d.material_json AS decision_material_json FROM application_posting_attempts a "
        "JOIN application_posting_decisions d USING(attempt_id) "
        "WHERE d.confirmation_public_id=?",
        (authorization["confirmation_public_id"],),
    ).fetchone()
    if row is None:
        raise AiInvocationError("Full-card text acceptance absent")
    from finance_core.calculation.authoritative_snapshot import canonical_json_value

    decision = canonical_json_value(row["decision_material_json"])
    fields = _independent_conversion_fields(
        resolve_simple_expense_conversion_fields(conn, proposal),
        authorization,
    )
    assessment = assess_independent_ai_expense_v2(conn, proposal, review["source"])
    if (
        assessment is None
        or assessment["hard_blockers"]
        or assessment != review["expense_assessment"]
        or not _has_independent_posting_proof(conn, proposal, authorization, fields)
    ):
        raise AiInvocationError("Accepted full-card text basis changed")
    source, binding = review["source"], review["binding"]
    attempt_id = "apa_" + _sha(
        [binding["instance_id"], decision["namespace"], decision["decision_id"]]
    )
    expected = ExpectedTextPostingAuthorization(
        proposal["id"],
        proposal["public_id"],
        version,
        digest,
        authorization["confirmation_public_id"],
        authorization["authenticated_actor_id"],
        authorization["confirmation_channel"],
        proposal["source_public_id"],
        _sha([source["namespace"], source["instance_id"], source["source_event_id"]]),
        attempt_id,
        "apr_" + posting_review_sha256(review),
        posting_review_sha256(review),
        decision["decision_id"],
        decision["decision_digest"],
        sha({**fields, "account": "unspecified"}),
        sha(assessment),
        _converted_transaction_public_id(proposal, authorization, fields),
        ParserConversionRepository(conn).get_for_proposal(proposal["id"]),
    )
    proof = _AcceptedTextPostingAuthorization.verify_in_transaction(authority, conn, expected)
    if (
        type(proof) is not VerifiedTextPostingAuthorization
        or proof.expected != expected
        or proof.basis != BASIS
        or proof.accepted_attempt_id != expected.accepted_attempt_id
        or proof.posting_review_id != expected.posting_review_id
        or proof.posting_review_hash != expected.posting_review_hash
        or proof.decision_id != expected.decision_id
        or proof.decision_digest != expected.decision_digest
        or proof.eligibility_digest != expected.eligibility_digest
        or proof.display_digest != decision["display_evidence_digest"]
        or any(
            not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for v in (
                proof.posting_review_hash,
                proof.decision_digest,
                proof.display_digest,
                proof.eligibility_digest,
            )
        )
    ):
        raise AiInvocationError("Full-card text posting authorization does not verify")
    return proof
