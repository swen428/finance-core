"""Service-owned units of work for parser confirmation and conversion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.money import (
    MoneyValidationError,
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    requires_deterministic_intent_policy,
    verify_ai_fallback_child,
    verify_deterministic_intent_policy,
)
from finance_core.parser_proposals.content_hash import (
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.conversion_state import (
    has_receipt_ocr_proposal_link,
    has_receipt_registry_conversion,
)
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_drafts import (
    HumanDraftDecisionBinding,
    HumanDraftError,
    confirm_active_human_draft_in_transaction,
    reject_active_human_draft_in_transaction,
    require_current_human_draft_publication_in_transaction,
    require_human_draft_reject_capability_in_transaction,
)
from finance_core.parser_proposals.lifecycle import (
    CONFIRMED,
    REJECTED,
    raw_intake_status_for_proposal_status,
    validate_transition,
)
from finance_core.parser_proposals.numeric_mirror import (
    decimal_from_numeric_mirror,
    sqlite_numeric_roundtrip_matches,
)
from finance_core.parser_proposals.repository import (
    CanonicalTransactionRepository,
    ParserAuthorizationRepository,
    ParserConversionRepository,
    ParserProposalRepository,
)
from finance_core.staging_guard import require_staging_database


class ParserConfirmationError(ValueError):
    """Base error for authoritative parser confirmation failures."""


class UnauthorizedConfirmationActorError(ParserConfirmationError):
    """The supplied command did not identify an authenticated human actor."""


class InvalidProposalStatusError(ParserConfirmationError):
    """A proposal is not in a status that can be converted."""


class MissingConfirmationRecordError(ParserConfirmationError):
    """No authoritative persisted confirmation exists for a proposal."""


class StaleProposalConfirmationError(ParserConfirmationError):
    """The persisted confirmation is bound to different proposal content."""


class StaleProposalDecisionStateError(ParserConfirmationError):
    """The caller's expected proposal state does not match the durable state.

    Raised atomically inside the decision transaction, before any
    authorization, event, status change, or audit row is written, so a
    decision can never land on content the operator did not authorize.
    """


class AlreadyConvertedProposalError(ParserConfirmationError):
    """A proposal has already produced its one canonical transaction."""


class MissingRequiredTransactionFieldError(ParserConfirmationError):
    """A simple proposal omits a deterministic final transaction field."""


class UnsupportedProposalTypeError(ParserConfirmationError):
    """The narrow converter does not support this proposed transaction type."""


class ProposalConversionError(ParserConfirmationError):
    """Base error for deterministic proposal-to-transaction conversion."""


SIMPLE_EXPENSE_TYPES = frozenset({"personal_expense", "simple_expense", "expense"})
CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX = "txn_parser_proposal"


def confirm_parser_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    authenticated_actor_id: str,
    actor_type: str = "human",
    decision: str = "confirmed",
    confirmation_channel: str = "cli",
    reason: str | None = None,
    confirmation_public_id: str | None = None,
    expected_content_hash: str | None = None,
    expected_version: int | None = None,
    d1_decision_binding: HumanDraftDecisionBinding | None = None,
    clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Atomically persist an authoritative human decision and lifecycle change.

    When ``expected_content_hash`` and/or ``expected_version`` are supplied,
    the durable effective state is re-read inside this same ``BEGIN
    IMMEDIATE`` transaction and compared atomically before anything is
    written; a mismatch raises ``StaleProposalDecisionStateError`` and rolls
    back without persisting any authorization, event, status change, or
    audit row.  Callers that omit both expectations keep the historical
    behavior.
    """
    _validate_human_command(authenticated_actor_id, actor_type, decision, confirmation_channel)
    require_staging_database(conn)
    _begin_immediate(conn)
    try:
        proposals = ParserProposalRepository(conn)
        authorizations = ParserAuthorizationRepository(conn)
        proposal = _require_proposal(proposals, parser_output_id)
        content_hash = compute_effective_proposal_content_hash(conn, proposal)
        decided_at = _now(clock)
        decision_epoch = _epoch_from_iso(decided_at)
        try:
            verify_ai_fallback_child(
                conn,
                proposal,
                content_hash=content_hash,
                proposal_version=resolve_effective_payload(conn, proposal)[2],
                require_resolved=(decision == "confirmed"),
            )
            if requires_deterministic_intent_policy(proposal):
                verify_deterministic_intent_policy(proposal)
        except AiFallbackServiceError as exc:
            raise ParserConfirmationError(str(exc)) from exc
        if decision == "confirmed":
            require_current_human_draft_publication_in_transaction(
                conn,
                parser_output_id=parser_output_id,
                authenticated_actor_id=authenticated_actor_id,
                decision_binding=d1_decision_binding,
                now_epoch=decision_epoch,
            )
        elif d1_decision_binding is not None:
            require_human_draft_reject_capability_in_transaction(
                conn,
                parser_output_id=parser_output_id,
                authenticated_actor_id=authenticated_actor_id,
                decision_binding=d1_decision_binding,
                now_epoch=decision_epoch,
            )
        if expected_content_hash is not None or expected_version is not None:
            _payload, _completion_id, durable_version = resolve_effective_payload(conn, proposal)
            if expected_content_hash is not None and expected_content_hash != content_hash:
                raise StaleProposalDecisionStateError(
                    "Decision expected content hash does not match the durable proposal "
                    "content hash inside the decision transaction."
                )
            if expected_version is not None and expected_version != durable_version:
                raise StaleProposalDecisionStateError(
                    "Decision expected version does not match the durable proposal version "
                    "inside the decision transaction."
                )
        existing = authorizations.get_for_proposal(parser_output_id)
        if existing is not None:
            result = _existing_confirmation_result(
                existing,
                proposal,
                content_hash,
                decision,
                authenticated_actor_id,
                confirmation_public_id,
                confirmation_channel,
            )
            if decision == "confirmed" and d1_decision_binding is not None:
                confirm_active_human_draft_in_transaction(
                    conn,
                    parser_output_id=parser_output_id,
                    authenticated_actor_id=authenticated_actor_id,
                    decision_public_id=str(existing["confirmation_public_id"]),
                    decision_binding=d1_decision_binding,
                    now_epoch=decision_epoch,
                )
            elif decision == "rejected":
                reject_active_human_draft_in_transaction(
                    conn,
                    parser_output_id=parser_output_id,
                    authenticated_actor_id=authenticated_actor_id,
                    decision_public_id=str(existing["confirmation_public_id"]),
                    decision_binding=d1_decision_binding,
                    now_epoch=decision_epoch,
                )
            conn.commit()
            return result

        to_status = CONFIRMED if decision == "confirmed" else REJECTED
        validate_transition(str(proposal["parse_status"]), to_status)
        public_id = confirmation_public_id or f"pca_{uuid.uuid4().hex}"
        payload = json.dumps(
            {
                "authoritative_confirmation_id": public_id,
                "proposal_content_hash": content_hash,
                "authenticated_actor_id": authenticated_actor_id,
                "decision": decision,
                "final_transaction_created": False,
                "raw_input_preserved": True,
                "parser_payload_preserved": True,
            },
            sort_keys=True,
        )
        authorizations.insert(
            confirmation_public_id=public_id,
            parser_output_id=parser_output_id,
            content_hash=content_hash,
            actor_id=authenticated_actor_id,
            state=decision,
            channel=confirmation_channel,
            decided_at=decided_at,
        )
        event_id = proposals.insert_event(
            parser_output_id,
            str(proposal["parse_status"]),
            to_status,
            reason,
            authenticated_actor_id,
            payload,
        )
        legacy_confirmation_id = proposals.insert_legacy_confirmation(
            parser_output_id, to_status, authenticated_actor_id, reason, payload
        )
        proposals.update_status(parser_output_id, to_status)
        proposals.update_raw_intake_status(
            parser_output_id, raw_intake_status_for_proposal_status(to_status)
        )
        _append_parser_decision_audit(
            conn,
            proposal=proposal,
            from_status=str(proposal["parse_status"]),
            to_status=to_status,
            decision=decision,
            reason=reason,
            confirmation_public_id=public_id,
            content_hash=content_hash,
            actor_id=authenticated_actor_id,
            decided_at=decided_at,
        )
        if decision == "confirmed" and d1_decision_binding is not None:
            confirm_active_human_draft_in_transaction(
                conn,
                parser_output_id=parser_output_id,
                authenticated_actor_id=authenticated_actor_id,
                decision_public_id=public_id,
                decision_binding=d1_decision_binding,
                now_epoch=decision_epoch,
            )
        elif decision == "rejected":
            reject_active_human_draft_in_transaction(
                conn,
                parser_output_id=parser_output_id,
                authenticated_actor_id=authenticated_actor_id,
                decision_public_id=public_id,
                decision_binding=d1_decision_binding,
                now_epoch=decision_epoch,
            )
        conn.commit()
        return {
            "parser_output_id": parser_output_id,
            "from_status": proposal["parse_status"],
            "to_status": to_status,
            "raw_intake_status": raw_intake_status_for_proposal_status(to_status),
            "actor_type": "human",
            "event_id": event_id,
            "confirmation_id": public_id,
            "legacy_confirmation_id": legacy_confirmation_id,
            "proposal_content_hash": content_hash,
            "final_transaction_created": False,
            "idempotent": False,
        }
    except Exception as exc:
        _rollback_if_needed(conn)
        if isinstance(exc, HumanDraftError):
            raise ParserConfirmationError(str(exc)) from exc
        raise


def convert_confirmed_parser_proposal(
    conn: sqlite3.Connection, parser_output_id: int
) -> dict[str, Any]:
    """Atomically convert a persisted, authoritative confirmation to one transaction."""
    require_staging_database(conn)
    _begin_immediate(conn)
    try:
        proposals = ParserProposalRepository(conn)
        authorizations = ParserAuthorizationRepository(conn)
        conversions = ParserConversionRepository(conn)
        proposal = _require_proposal(proposals, parser_output_id)
        if proposal["parse_status"] != CONFIRMED:
            raise InvalidProposalStatusError(
                f"Only confirmed parser proposals can be converted: {proposal['parse_status']}"
            )
        content_hash = compute_effective_proposal_content_hash(conn, proposal)
        try:
            verify_ai_fallback_child(
                conn,
                proposal,
                content_hash=content_hash,
                proposal_version=resolve_effective_payload(conn, proposal)[2],
            )
            if requires_deterministic_intent_policy(proposal):
                verify_deterministic_intent_policy(proposal)
        except AiFallbackServiceError as exc:
            raise StaleProposalConfirmationError(str(exc)) from exc
        authorization = _require_active_authorization(
            authorizations.get_for_proposal(parser_output_id), parser_output_id, content_hash
        )
        # B4.0 mutual exclusion: receipt proposals with persisted OCR link
        # evidence are owned by the guarded receipt conversion boundary and
        # must never enter this narrow simple-expense converter, not even as
        # an idempotent replay.
        if has_receipt_ocr_proposal_link(conn, parser_output_id):
            raise UnsupportedProposalTypeError(
                "Receipt proposals with OCR link evidence cannot use the "
                f"legacy simple-expense converter: {parser_output_id}"
            )
        # B4.0 mutual exclusion: a proposal already recorded in the future
        # receipt conversion registry has produced its canonical receipt
        # facts; a registry row is never a legacy-conversion replay.
        if has_receipt_registry_conversion(conn, parser_output_id):
            raise AlreadyConvertedProposalError(
                "Proposal is already recorded in the receipt conversion "
                f"registry: {parser_output_id}"
            )
        existing = conversions.get_for_proposal(parser_output_id)
        if existing is not None:
            conn.commit()
            return {
                "transaction_id": existing["transaction_id"],
                "transaction_public_id": existing["transaction_public_id"],
                "parser_output_id": parser_output_id,
                "confirmation_id": authorization["confirmation_public_id"],
                "final_transaction_created": True,
                "idempotent": True,
            }

        fields = _transaction_fields(conn, proposal)
        _require_exact_transaction_money(conn, fields["amount"])
        public_id = _converted_transaction_public_id(proposal, authorization, fields)
        notes = json.dumps(
            {
                "conversion_source": "parser_proposal_authorization",
                "parser_output_id": proposal["id"],
                "parser_output_public_id": proposal["public_id"],
                "parser_proposal_authorization_id": authorization["confirmation_public_id"],
                "parser_proposal_confirmation_id": authorization["confirmation_public_id"],
                "authenticated_actor_id": authorization["authenticated_actor_id"],
                "proposal_content_hash": content_hash,
                "canonical_amount": fields["amount"],
                "source_evidence": _source_evidence(conn, proposal),
            },
            sort_keys=True,
        )
        transaction_id = CanonicalTransactionRepository(conn).insert(
            {
                "public_id": public_id,
                "intent": fields["intent"],
                "source_channel": fields["source_channel"],
                "transaction_date": fields["transaction_date"],
                "review_status": "confirmed_from_parser_proposal",
                "amount": fields["amount"],
                "currency": fields["currency"],
                "merchant": fields["merchant"],
                "category": fields["category"],
                "notes": notes,
                "raw_input": proposal["raw_text"],
                "statement_batch_id": proposal["statement_batch_id"],
                "parser_output_id": proposal["id"],
                "confidence_score": proposal["confidence_score"],
            }
        )
        _verify_persisted_transaction_money(conn, transaction_id, fields)
        conversions.insert(
            parser_output_id=parser_output_id,
            transaction_id=transaction_id,
            confirmation_public_id=str(authorization["confirmation_public_id"]),
            content_hash=content_hash,
            actor_id=str(authorization["authenticated_actor_id"]),
        )
        _append_parser_conversion_audit(
            conn,
            proposal=proposal,
            authorization=authorization,
            content_hash=content_hash,
            transaction_public_id=public_id,
        )
        conn.commit()
        return {
            "transaction_id": transaction_id,
            "transaction_public_id": public_id,
            "parser_output_id": parser_output_id,
            "confirmation_id": authorization["confirmation_public_id"],
            "final_transaction_created": True,
            "idempotent": False,
        }
    except Exception:
        _rollback_if_needed(conn)
        raise


def _validate_human_command(actor_id: str, actor_type: str, decision: str, channel: str) -> None:
    if actor_type not in {"human", "user"}:
        raise UnauthorizedConfirmationActorError(
            "Only authenticated human actors may authorize parser proposals"
        )
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise UnauthorizedConfirmationActorError("authenticated_actor_id must not be empty")
    if decision not in {"confirmed", "rejected"}:
        raise ParserConfirmationError(f"Unsupported authoritative confirmation state: {decision}")
    if not isinstance(channel, str) or not channel.strip():
        raise ParserConfirmationError("confirmation_channel must not be empty")


def _require_proposal(
    repository: ParserProposalRepository, parser_output_id: int
) -> dict[str, Any]:
    proposal = repository.get(parser_output_id)
    if proposal is None:
        raise ParserConfirmationError(f"parser output not found: {parser_output_id}")
    return proposal


def _existing_confirmation_result(
    existing: dict[str, Any],
    proposal: dict[str, Any],
    content_hash: str,
    decision: str,
    authenticated_actor_id: str,
    confirmation_public_id: str | None,
    confirmation_channel: str,
) -> dict[str, Any]:
    if (
        existing["proposal_content_hash"] != content_hash
        or existing["confirmation_state"] != decision
    ):
        raise ParserConfirmationError("Conflicting parser proposal confirmation replay")
    if existing["authenticated_actor_id"] != authenticated_actor_id:
        raise ParserConfirmationError("Conflicting parser proposal confirmation actor replay")
    if (
        confirmation_public_id is not None
        and existing["confirmation_public_id"] != confirmation_public_id
    ):
        raise ParserConfirmationError("Conflicting parser proposal confirmation ID replay")
    if existing["confirmation_channel"] != confirmation_channel:
        raise ParserConfirmationError("Conflicting parser proposal confirmation channel replay")
    if proposal["parse_status"] != (CONFIRMED if decision == "confirmed" else REJECTED):
        raise ParserConfirmationError("Persisted confirmation and parser proposal status conflict")
    return {
        "parser_output_id": proposal["id"],
        "from_status": proposal["parse_status"],
        "to_status": proposal["parse_status"],
        "raw_intake_status": raw_intake_status_for_proposal_status(proposal["parse_status"]),
        "actor_type": "human",
        "event_id": None,
        "confirmation_id": existing["confirmation_public_id"],
        "legacy_confirmation_id": None,
        "proposal_content_hash": content_hash,
        "final_transaction_created": False,
        "idempotent": True,
    }


def _require_active_authorization(
    authorization: dict[str, Any] | None, parser_output_id: int, content_hash: str
) -> dict[str, Any]:
    if authorization is None:
        raise MissingConfirmationRecordError(
            f"Confirmed parser proposal lacks authoritative confirmation record: {parser_output_id}"
        )
    if (
        authorization["actor_type"] != "human"
        or not str(authorization["authenticated_actor_id"]).strip()
    ):
        raise UnauthorizedConfirmationActorError(
            "Parser confirmation is not an authenticated human authorization"
        )
    if (
        authorization["confirmation_state"] != "confirmed"
        or authorization["revoked_at"] is not None
    ):
        raise MissingConfirmationRecordError("Parser confirmation is not active and confirmed")
    if authorization["proposal_content_hash"] != content_hash:
        raise StaleProposalConfirmationError(
            "Parser confirmation is bound to stale proposal content"
        )
    return authorization


def _begin_immediate(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise ParserConfirmationError(
            "Parser unit of work requires a connection without pending work"
        )
    conn.execute("BEGIN IMMEDIATE")


def _rollback_if_needed(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def _now(clock: Callable[[], str] | None) -> str:
    return clock() if clock is not None else datetime.now(timezone.utc).isoformat()


def _epoch_from_iso(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _append_parser_decision_audit(
    conn: sqlite3.Connection,
    *,
    proposal: dict[str, Any],
    from_status: str,
    to_status: str,
    decision: str,
    reason: str | None,
    confirmation_public_id: str,
    content_hash: str,
    actor_id: str,
    decided_at: str,
) -> None:
    event_type = f"parser_proposal_{decision}"
    aggregate_id = str(proposal["public_id"])
    event_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_id,
        event_type=event_type,
        causation_public_id=confirmation_public_id,
    )
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=aggregate_id,
            event_type=event_type,
            event_payload={
                "decision": decision,
                "reason": reason,
                "proposal_content_hash": content_hash,
                "confirmation_channel": "persisted_authorization",
            },
            previous_state={
                "parse_status": from_status,
                "raw_intake_status": raw_intake_status_for_proposal_status(from_status),
                "proposal_content_hash": content_hash,
                "conversion_status": "not_converted",
            },
            new_state={
                "parse_status": to_status,
                "raw_intake_status": raw_intake_status_for_proposal_status(to_status),
                "proposal_content_hash": content_hash,
                "conversion_status": "not_converted",
            },
            actor_type="human",
            actor_public_id=actor_id,
            authorization_public_id=confirmation_public_id,
            source_evidence_references=_parser_source_references(proposal),
            correlation_public_id=aggregate_id,
            causation_public_id=confirmation_public_id,
            created_at=decided_at,
        ),
    )


def _append_parser_conversion_audit(
    conn: sqlite3.Connection,
    *,
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    content_hash: str,
    transaction_public_id: str,
) -> None:
    event_type = "parser_proposal_converted"
    aggregate_id = str(proposal["public_id"])
    event_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_id,
        event_type=event_type,
        causation_public_id=transaction_public_id,
    )
    stable_state = {
        "parse_status": CONFIRMED,
        "raw_intake_status": raw_intake_status_for_proposal_status(CONFIRMED),
        "proposal_content_hash": content_hash,
    }
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=aggregate_id,
            event_type=event_type,
            event_payload={
                "transaction_public_id": transaction_public_id,
                "proposal_content_hash": content_hash,
            },
            previous_state={**stable_state, "conversion_status": "not_converted"},
            new_state={
                **stable_state,
                "conversion_status": "converted",
                "transaction_public_id": transaction_public_id,
            },
            actor_type="human",
            actor_public_id=str(authorization["authenticated_actor_id"]),
            authorization_public_id=str(authorization["confirmation_public_id"]),
            source_evidence_references=_parser_source_references(proposal),
            correlation_public_id=aggregate_id,
            causation_public_id=transaction_public_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def _parser_source_references(proposal: dict[str, Any]) -> tuple[str, ...]:
    references = [f"parser-output:{proposal['public_id']}"]
    if proposal.get("source_public_id"):
        references.append(f"source:{proposal['source_public_id']}")
    if proposal.get("attachment_id") is not None:
        references.append(f"attachment-id:{proposal['attachment_id']}")
    if proposal.get("statement_batch_id") is not None:
        references.append(f"statement-batch-id:{proposal['statement_batch_id']}")
    return tuple(references)


def _effective_transaction_payload(
    conn: sqlite3.Connection, proposal: dict[str, Any]
) -> dict[str, Any]:
    """Return the effective payload for conversion, resolving any completion.

    Delegates to the single authoritative effective-payload resolver so there
    is exactly one definition of "original + latest completion = effective".
    """
    from finance_core.parser_proposals.effective_payload import EffectivePayloadError

    try:
        effective, _cid, _version = resolve_effective_payload(conn, proposal)
    except EffectivePayloadError as exc:
        raise ProposalConversionError(str(exc)) from exc
    return effective


def _transaction_fields(conn: sqlite3.Connection, proposal: dict[str, Any]) -> dict[str, Any]:
    payload = _effective_transaction_payload(conn, proposal)
    transaction_type = payload.get("transaction_type")
    intent = payload.get("intent")
    if transaction_type not in SIMPLE_EXPENSE_TYPES and not (
        transaction_type is None and intent in {"personal_expense_log", "simple_expense_log"}
    ):
        raise UnsupportedProposalTypeError(
            f"Only simple expense proposals can be converted: {transaction_type or intent}"
        )
    amount, currency = _required_canonical_money(payload)
    transaction_date = _required_date(payload)
    merchant = _optional_text(payload.get("merchant"))
    description = _optional_text(payload.get("description"))
    if merchant is None and description is None:
        raise MissingRequiredTransactionFieldError(
            "Missing required field: merchant_or_description"
        )
    return {
        "intent": intent or "personal_expense_log",
        "amount": amount,
        "currency": currency,
        "transaction_date": transaction_date,
        "merchant": merchant,
        "category": payload.get("category"),
        "description": description,
        "source_channel": _source_channel(conn, proposal),
    }


def _required_canonical_money(payload: dict[str, Any]) -> tuple[str, str]:
    amount_value = payload.get("amount")
    currency_value = payload.get("currency")
    if amount_value is None or amount_value == "":
        raise MissingRequiredTransactionFieldError("Missing required field: amount")
    if currency_value is None or currency_value == "":
        raise MissingRequiredTransactionFieldError("Missing required field: currency")
    try:
        currency = normalize_currency(currency_value)
        amount = money_decimal(amount_value, label="parser proposal amount")
        amount = validate_amount_for_currency(amount, currency, label="parser proposal amount")
        amount = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
            amount, label="parser proposal amount"
        )
        return canonical_money_str(amount, currency), currency
    except MoneyValidationError as exc:
        raise ProposalConversionError("Invalid parser proposal monetary fields") from exc


def _require_exact_transaction_money(
    conn: sqlite3.Connection,
    canonical_amount: str,
) -> None:
    """Refuse a text conversion whose NUMERIC destination would be lossy."""
    if not sqlite_numeric_roundtrip_matches(conn, canonical_amount):
        raise ProposalConversionError(
            "Parser proposal amount cannot be mirrored losslessly by the "
            "transactions NUMERIC amount destination"
        )


def _verify_persisted_transaction_money(
    conn: sqlite3.Connection,
    transaction_id: int,
    fields: dict[str, Any],
) -> None:
    """Read back amount/total_amount/currency before the conversion commits."""
    row = conn.execute(
        "SELECT amount, total_amount, currency FROM transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    expected = money_decimal(fields["amount"])
    if (
        row is None
        or decimal_from_numeric_mirror(row[0]) != expected
        or decimal_from_numeric_mirror(row[1]) != expected
        or row[2] != fields["currency"]
    ):
        raise ProposalConversionError(
            "Persisted parser transaction monetary fields changed during conversion"
        )


def _required_date(payload: dict[str, Any]) -> str:
    value = payload.get("transaction_date", payload.get("date"))
    if not isinstance(value, str) or not value:
        raise MissingRequiredTransactionFieldError("Missing required field: transaction_date")
    try:
        if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise ProposalConversionError("Invalid transaction_date") from exc
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_channel(conn: sqlite3.Connection, proposal: dict[str, Any]) -> str | None:
    if proposal["source_public_id"] is not None:
        row = conn.execute(
            "SELECT source_channel FROM raw_intake_records WHERE public_id = ?",
            (proposal["source_public_id"],),
        ).fetchone()
        if row is not None:
            value = row["source_channel"] if isinstance(row, sqlite3.Row) else row[0]
            if value:
                return str(value)
    source_type = proposal["source_type"]
    if source_type.startswith("telegram_"):
        return "telegram"
    channels = {
        "manual_entry": "manual",
        "system_generated": "system",
        "statement_row": "imported_statement",
    }
    return channels.get(source_type)


def _source_evidence(conn: sqlite3.Connection, proposal: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT public_id, file_path FROM attachments "
        "WHERE parser_output_id = ? OR id = ? ORDER BY id ASC",
        (proposal["id"], proposal["attachment_id"]),
    ).fetchall()
    return {
        "raw_text_preserved": proposal["raw_text"] is not None,
        "source_public_id": proposal["source_public_id"],
        "attachment_paths": [
            {"public_id": row["public_id"], "file_path": row["file_path"]}
            if isinstance(row, sqlite3.Row)
            else {"public_id": row[0], "file_path": row[1]}
            for row in rows
        ],
    }


def _converted_transaction_public_id(
    proposal: dict[str, Any], authorization: dict[str, Any], fields: dict[str, Any]
) -> str:
    identity = json.dumps(
        {
            "parser_output_id": proposal["id"],
            "confirmation_public_id": authorization["confirmation_public_id"],
            "amount": fields["amount"],
            "currency": fields["currency"],
            "transaction_date": fields["transaction_date"],
            "merchant": fields["merchant"],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX}_po{proposal['id']}_pc{digest[:16]}"
