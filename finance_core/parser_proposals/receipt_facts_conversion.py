"""Guarded receipt proposal-to-facts conversion boundary (B4.1).

Converts one eligible, current-leaf, human-confirmed receipt OCR total
proposal into receipt facts: exactly one ``receipts`` row, explicit
``receipt_participants`` membership rows, one append-only
``receipt_proposal_conversions`` registry row (migration 035), and one
append-only ``financial_audit_events`` event.  Conversion is facts-only in
v1: it never creates transactions, calculation inputs, calculation
snapshots, settlement obligations, receipt items, allocations, adjustments,
or group rows, and it never runs finalization or reconciliation.

The design contract lives in
``docs/design/receipt_proposal_to_facts_conversion_v1.md`` (guards 1-14,
error taxonomy, identity and hashing, approved decisions D1-D7).  Payer and
participant membership are explicit authenticated command inputs; monetary
values are never accepted from the command — they come only from the
confirmed effective proposal payload.
"""

from __future__ import annotations

import hashlib
import json
import os.path
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from finance_core.calculation.authoritative_snapshot import canonical_json_text
from finance_core.financial_audit import (
    AUDIT_SCHEMA_VERSION,
    ZERO_AUDIT_HASH,
    AuditChainTransactionError,
    AuditEventCommand,
    AuditVerificationError,
    FinancialAuditEvent,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.money import MoneyValidationError, money_decimal, normalize_currency
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    verify_ai_fallback_child,
)
from finance_core.parser_proposals.content_hash import (
    ProposalContentHashError,
    canonicalize_proposal_money,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.conversion_state import has_legacy_transaction_conversion
from finance_core.parser_proposals.effective_payload import (
    EffectivePayloadError,
    resolve_effective_payload,
)
from finance_core.parser_proposals.lifecycle import (
    CONFIRMED,
    SUPERSEDED,
    raw_intake_status_for_proposal_status,
)
from finance_core.parser_proposals.numeric_mirror import (
    decimal_from_numeric_mirror,
    sqlite_numeric_roundtrip_matches,
)
from finance_core.parser_proposals.receipt_total_parser import (
    FLAG_AMBIGUOUS_CURRENCY_SYMBOL,
    FLAG_AMBIGUOUS_DATE,
    FLAG_CONFLICTING_DATES,
    FLAG_CONFLICTING_TOTALS,
    FLAG_CURRENCY_NOT_DETERMINED,
    FLAG_DATE_NOT_FOUND,
    FLAG_MERCHANT_NOT_DETERMINED,
    FLAG_OCR_ENGINE_FAILED,
    FLAG_OCR_NO_TEXT,
    FLAG_OCR_RESOURCE_REJECTED,
    FLAG_OCR_UNSUPPORTED_INPUT,
    FLAG_TOTAL_AMOUNT_INVALID,
    FLAG_TOTAL_NOT_FOUND,
    FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT,
)
from finance_core.sqlite_connection import ForeignKeysDisabledError, require_foreign_keys_enabled
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

# ---------------------------------------------------------------------------
# Public errors (Section 7 taxonomy)
# ---------------------------------------------------------------------------


class ReceiptFactsConversionError(ValueError):
    """Base error for receipt proposal-to-facts conversion failures."""


class ConversionStagingDatabaseRejectedError(ReceiptFactsConversionError):
    """The staging guard rejected the database (live database, copies)."""


class ConversionForeignKeysDisabledError(ReceiptFactsConversionError):
    """The connection does not enforce SQLite foreign keys (fail closed)."""


class ConversionCallerOwnedTransactionError(ReceiptFactsConversionError):
    """The connection already carries a caller-owned pending transaction."""


class InvalidConversionCommandError(ReceiptFactsConversionError):
    """The command is malformed, carries unknown fields, or a bad ID pattern."""


class UnauthorizedConversionActorError(ReceiptFactsConversionError):
    """The command did not identify an authenticated human actor."""


class ConversionProposalNotFoundError(ReceiptFactsConversionError):
    """The referenced parser proposal does not exist."""


class StaleConversionTargetError(ReceiptFactsConversionError):
    """The proposal is superseded, not a leaf, or lost its raw-intake pointer."""


class ProposalNotConfirmedError(ReceiptFactsConversionError):
    """The proposal is not in confirmed status with a confirmed authorization."""


class StaleConfirmationHashError(ReceiptFactsConversionError):
    """The Section 6 triple hash equality does not hold for this command."""


class ReceiptFactsAlreadyConvertedError(ReceiptFactsConversionError):
    """The proposal or a supersession-chain member was already converted."""


class UnsupportedConversionProposalTypeError(ReceiptFactsConversionError):
    """The proposal is not a receipt OCR total proposal with link evidence."""


class IncompleteReceiptInputsError(ReceiptFactsConversionError):
    """A required receipt-fact input is missing or invalid."""


class UnsupportedReceiptFactsMetadataError(ReceiptFactsConversionError):
    """The effective payload carries metadata the facts schema cannot persist.

    The ``receipts`` table (migration 002) has no ``description`` or
    ``category`` column.  A fresh conversion whose effective payload carries
    a non-NULL value for either field fails closed before any write: the
    value is never silently dropped, never mapped into ``receipts.notes``,
    and no column is invented.  Authoritative persistence requires a
    separately approved schema task.
    """


class AmbiguousReceiptInputError(ReceiptFactsConversionError):
    """Membership is ambiguous, unknown, or an ambiguity flag is unresolved."""


class ConversionEvidenceLineageError(ReceiptFactsConversionError):
    """The evidence or pointer lineage chain is internally inconsistent."""


class ConversionIdempotencyConflictError(ReceiptFactsConversionError):
    """The same command public ID exists with different canonical material."""


class ConversionPersistenceError(ReceiptFactsConversionError):
    """Constraint violation or unexplained persistence failure."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONVERSION_SCHEMA_VERSION = "v1"
RECEIPT_FACTS_CONVERSION_EVENT_TYPE = "receipt_proposal_converted_to_facts"

_COMMAND_ID_RE = re.compile(r"^rpfc_[A-Za-z0-9_-]{1,195}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_LEN = 10

_ROLE_PAYER = "payer"
_ROLE_PARTICIPANT = "participant"
_ROLE_EXCLUDED = "excluded"

_ENTRY_FIELDS = frozenset({"participant_public_id", "is_included"})
_COMMAND_FIELDS = frozenset(
    {
        "command_public_id",
        "proposal_public_id",
        "expected_content_hash",
        "payer_participant_public_id",
        "participants",
        "authenticated_actor_id",
        "actor_type",
        "channel",
        "reason",
    }
)
_REQUIRED_COMMAND_FIELDS = frozenset(
    {
        "command_public_id",
        "proposal_public_id",
        "expected_content_hash",
        "payer_participant_public_id",
        "participants",
        "authenticated_actor_id",
        "channel",
    }
)

# Guard 13: B1 ambiguity flags material to money, date, or currency.  A
# monetary flag is cleared only by a B3 supersession correcting the
# corresponding monetary field; a date flag is cleared by a human-supplied
# date (completion or supersession bundle).  Resolution never happens here.
_AMOUNT_FLAGS = frozenset(
    {FLAG_TOTAL_NOT_FOUND, FLAG_CONFLICTING_TOTALS, FLAG_TOTAL_AMOUNT_INVALID}
)
_CURRENCY_FLAGS = frozenset(
    {
        FLAG_AMBIGUOUS_CURRENCY_SYMBOL,
        FLAG_CURRENCY_NOT_DETERMINED,
        FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT,
    }
)
_DATE_FLAGS = frozenset({FLAG_AMBIGUOUS_DATE, FLAG_CONFLICTING_DATES, FLAG_DATE_NOT_FOUND})
_INFORMATIONAL_FLAGS = frozenset(
    {
        FLAG_MERCHANT_NOT_DETERMINED,
        FLAG_OCR_NO_TEXT,
        FLAG_OCR_UNSUPPORTED_INPUT,
        FLAG_OCR_ENGINE_FAILED,
        FLAG_OCR_RESOURCE_REJECTED,
    }
)

# Guard 13: the closed vocabulary of recognized B1 ambiguity flag tokens.
# ``ambiguity_flags`` must be a list drawn from this set; any other shape or
# token (including forward-compatible unknown flags) fails closed instead of
# silently passing an unclassified ambiguity into conversion.
_KNOWN_AMBIGUITY_FLAGS = _AMOUNT_FLAGS | _CURRENCY_FLAGS | _DATE_FLAGS | _INFORMATIONAL_FLAGS

# Guard 13: payload evidence source types that mark a human-origin item.  B3
# supersession/completion evidence persists ``user_message`` (the relational
# schema's vocabulary for human-typed input); ``human`` covers payloads that
# spell the actor type directly.  Either way the item is a claim that must be
# backed by durable provenance ids.
_HUMAN_EVIDENCE_SOURCE_TYPES = frozenset({"human", "user_message"})

# ---------------------------------------------------------------------------
# Test-only failure seam
# ---------------------------------------------------------------------------

_failure_injection_hook: Callable[[str], None] | None = None
"""Private test-only failure seam at real conversion write boundaries."""


def _inject_failure(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


# ---------------------------------------------------------------------------
# Command and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptFactsConversionCommand:
    """Immutable authenticated human conversion command (Section 3.3).

    Monetary values are never command inputs.  Membership entries are
    mappings carrying exactly ``participant_public_id`` and an explicit
    ``is_included`` value (approved Decision D5).
    """

    command_public_id: str
    proposal_public_id: str
    expected_content_hash: str
    payer_participant_public_id: str
    participants: Sequence[Mapping[str, object]]
    authenticated_actor_id: str
    channel: str
    actor_type: str = "human"
    reason: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "ReceiptFactsConversionCommand":
        """Build a command from a mapping, rejecting unknown fields fail-closed.

        Item, allocation, adjustment, and group inputs are unknown fields by
        construction (approved Decisions D2 and D6) and are rejected here.
        """
        if not isinstance(data, Mapping):
            raise InvalidConversionCommandError("Conversion command must be a mapping")
        unknown = sorted(set(data) - _COMMAND_FIELDS)
        if unknown:
            raise InvalidConversionCommandError(
                f"Unknown conversion command fields are rejected: {unknown}"
            )
        missing = sorted(_REQUIRED_COMMAND_FIELDS - set(data))
        if missing:
            raise InvalidConversionCommandError(
                f"Conversion command is missing required fields: {missing}"
            )
        participants = data["participants"]
        if isinstance(participants, Sequence) and not isinstance(participants, (str, bytes)):
            participants = tuple(participants)
        return cls(
            command_public_id=data["command_public_id"],  # type: ignore[arg-type]
            proposal_public_id=data["proposal_public_id"],  # type: ignore[arg-type]
            expected_content_hash=data["expected_content_hash"],  # type: ignore[arg-type]
            payer_participant_public_id=data["payer_participant_public_id"],  # type: ignore[arg-type]
            participants=participants,  # type: ignore[arg-type]
            authenticated_actor_id=data["authenticated_actor_id"],  # type: ignore[arg-type]
            channel=data["channel"],  # type: ignore[arg-type]
            actor_type=data.get("actor_type", "human"),  # type: ignore[arg-type]
            reason=data.get("reason"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ReceiptFactsConversionResult:
    """Immutable deterministic conversion result (identical on replay)."""

    command_public_id: str
    proposal_public_id: str
    parser_output_id: int
    receipt_public_id: str
    receipt_id: int
    confirmation_public_id: str
    proposal_content_hash: str
    command_material_hash: str
    conversion_result_hash: str
    idempotent: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_confirmed_receipt_proposal_to_facts(
    conn: sqlite3.Connection,
    command: ReceiptFactsConversionCommand,
    *,
    clock: Callable[[], str] | None = None,
) -> ReceiptFactsConversionResult:
    """Atomically convert one confirmed receipt total proposal to receipt facts.

    Runs the Section 2 guards 1-14 in normative order, then writes the
    ``receipts`` fact row, explicit ``receipt_participants`` membership
    rows, the append-only conversion registry row, and one financial audit
    event inside a single service-owned ``BEGIN IMMEDIATE`` transaction.
    Same command replays idempotently; any failure rolls back completely.
    """
    entries = _validate_command(command)  # guard 1
    _require_staging(conn)  # guard 2
    _acquire_write_transaction(conn)  # guard 3
    try:
        material_hash = _command_material_hash(command, entries)

        existing = _get_registry_row(conn, command.command_public_id)
        if existing is not None:  # guard 4
            # The conflict decision precedes replay integrity: changed
            # material is reported first even when dependent facts or
            # evidence rows were destroyed out-of-band.
            _require_replay_material(existing, material_hash, command)
            _verify_replay_integrity(conn, existing)
            result = _replay_result(existing)
            conn.commit()
            return result

        proposal = _require_proposal(conn, command.proposal_public_id)  # guard 5
        parser_output_id = int(proposal["id"])
        link = _require_single_receipt_link(conn, parser_output_id)  # guard 6
        _require_leaf(conn, proposal)  # guard 7
        raw_intake = _require_single_raw_intake(conn, parser_output_id)  # guard 8
        _require_confirmed_status(proposal)  # guard 9
        effective_hash, authorization = _require_bound_confirmation(  # guard 10
            conn, proposal, command.expected_content_hash
        )
        chain_ids, chain_root_id = _walk_supersession_chain(conn, parser_output_id)
        _require_not_converted(conn, chain_ids)  # guard 11
        effective = _resolve_effective(conn, proposal)
        extraction, attachment = _require_evidence_lineage(  # guard 12
            conn, proposal, link, raw_intake, effective
        )
        _require_source_binding(conn, proposal, raw_intake, extraction, attachment)
        _require_flags_resolved(conn, proposal, effective)  # guard 13
        facts = _require_complete_inputs(conn, command, entries, effective)  # guard 14

        receipt_public_id = derive_receipt_public_id(command.command_public_id)
        confirmation_public_id = str(authorization["confirmation_public_id"])
        now = _now(clock)

        _inject_failure("before_receipt_insert")
        receipt_id = _insert_receipt(
            conn,
            receipt_public_id=receipt_public_id,
            facts=facts,
            proposal=proposal,
            raw_intake=raw_intake,
            attachment=attachment,
        )

        _inject_failure("before_participants_insert")
        _insert_participants(
            conn,
            receipt_id=receipt_id,
            command_public_id=command.command_public_id,
            payer_public_id=command.payer_participant_public_id,
            resolved_entries=facts["resolved_entries"],
        )

        result_hash = _conversion_result_hash(
            receipt_public_id=receipt_public_id,
            facts=facts,
            payer_public_id=command.payer_participant_public_id,
            entries=entries,
            attachment_content_hash=str(extraction["source_attachment_hash"]),
            proposal_content_hash=effective_hash,
            command_material_hash=material_hash,
        )

        _inject_failure("before_conversion_registry_insert")
        _insert_registry_row(
            conn,
            command=command,
            parser_output_id=parser_output_id,
            supersession_root_parser_output_id=chain_root_id,
            receipt_id=receipt_id,
            confirmation_public_id=confirmation_public_id,
            proposal_content_hash=effective_hash,
            command_material_hash=material_hash,
            conversion_result_hash=result_hash,
            created_at=now,
        )

        _inject_failure("before_audit_append")
        audit_event = _append_conversion_audit(
            conn,
            command=command,
            proposal=proposal,
            extraction=extraction,
            receipt_public_id=receipt_public_id,
            confirmation_public_id=confirmation_public_id,
            proposal_content_hash=effective_hash,
            command_material_hash=material_hash,
            conversion_result_hash=result_hash,
            created_at=now,
        )

        _inject_failure("before_persisted_verification")
        _verify_persisted(
            conn,
            command=command,
            entries=entries,
            proposal=proposal,
            link=link,
            raw_intake=raw_intake,
            authorization=authorization,
            extraction=extraction,
            attachment=attachment,
            facts=facts,
            receipt_id=receipt_id,
            receipt_public_id=receipt_public_id,
            effective_hash=effective_hash,
            material_hash=material_hash,
            result_hash=result_hash,
            audit_event=audit_event,
            now=now,
            chain_ids=chain_ids,
            chain_root_id=chain_root_id,
        )

        _inject_failure("before_commit")
        conn.commit()
        return ReceiptFactsConversionResult(
            command_public_id=command.command_public_id,
            proposal_public_id=str(proposal["public_id"]),
            parser_output_id=parser_output_id,
            receipt_public_id=receipt_public_id,
            receipt_id=receipt_id,
            confirmation_public_id=confirmation_public_id,
            proposal_content_hash=effective_hash,
            command_material_hash=material_hash,
            conversion_result_hash=result_hash,
            idempotent=False,
        )
    except ReceiptFactsConversionError:
        _rollback_if_needed(conn)
        raise
    except (AuditVerificationError, AuditChainTransactionError) as exc:
        # Audit-chain conflicts (deterministic event-identity collisions,
        # tampered chain heads) and audit transaction-context failures are
        # persistence failures of this conversion: translate them into the
        # Section 7 taxonomy after a full rollback, preserving the cause.
        _rollback_if_needed(conn)
        raise ConversionPersistenceError(
            "Receipt facts conversion audit event could not be appended atomically"
        ) from exc
    except sqlite3.Error as exc:
        _rollback_if_needed(conn)
        raise ConversionPersistenceError(
            "Receipt proposal-to-facts conversion could not be persisted atomically"
        ) from exc
    except BaseException:
        _rollback_if_needed(conn)
        raise


# ---------------------------------------------------------------------------
# Guard 1: command validation (structural validation precedes hashing)
# ---------------------------------------------------------------------------


def _validate_command(command: ReceiptFactsConversionCommand) -> list[tuple[str, int]]:
    """Validate the command structure and return canonical membership entries.

    Returns entries as ``(participant_public_id, is_included)`` tuples sorted
    by participant public ID.  Deterministic guard 1 error mapping per the
    design: malformed IDs / unknown fields / bad channel →
    ``InvalidConversionCommandError``; actor problems →
    ``UnauthorizedConversionActorError``; missing explicit ``is_included`` →
    ``IncompleteReceiptInputsError``; duplicate or contradictory entries →
    ``AmbiguousReceiptInputError``.
    """
    if not isinstance(command, ReceiptFactsConversionCommand):
        raise InvalidConversionCommandError(
            "Conversion requires a ReceiptFactsConversionCommand instance"
        )
    if not isinstance(command.command_public_id, str) or not _COMMAND_ID_RE.match(
        command.command_public_id
    ):
        raise InvalidConversionCommandError(
            "command_public_id must match 'rpfc_' plus 1-195 characters of [A-Za-z0-9_-]"
        )
    if not isinstance(command.proposal_public_id, str) or not command.proposal_public_id.strip():
        raise InvalidConversionCommandError("proposal_public_id must be a non-empty string")
    if not isinstance(command.expected_content_hash, str) or not _HASH_RE.match(
        command.expected_content_hash
    ):
        raise InvalidConversionCommandError(
            "expected_content_hash must be a 64-character lowercase hex string"
        )
    if command.actor_type != "human":
        raise UnauthorizedConversionActorError(
            f"Only authenticated human actors may convert proposals, got: {command.actor_type!r}"
        )
    if (
        not isinstance(command.authenticated_actor_id, str)
        or not command.authenticated_actor_id
        or command.authenticated_actor_id != command.authenticated_actor_id.strip()
    ):
        raise UnauthorizedConversionActorError(
            "authenticated_actor_id must be a non-empty string without surrounding whitespace"
        )
    if (
        not isinstance(command.channel, str)
        or not command.channel
        or command.channel != command.channel.strip()
    ):
        raise InvalidConversionCommandError(
            "channel must be a non-empty string without surrounding whitespace"
        )
    if command.reason is not None and not isinstance(command.reason, str):
        raise InvalidConversionCommandError("reason must be a string or None")
    if (
        not isinstance(command.payer_participant_public_id, str)
        or not command.payer_participant_public_id.strip()
        or command.payer_participant_public_id != command.payer_participant_public_id.strip()
    ):
        raise IncompleteReceiptInputsError(
            "payer_participant_public_id must be a non-empty string; no default payer is inferred"
        )

    participants = command.participants
    if isinstance(participants, (str, bytes)) or not isinstance(participants, Sequence):
        raise InvalidConversionCommandError(
            "participants must be a sequence of membership entry mappings"
        )
    if len(participants) == 0:
        raise IncompleteReceiptInputsError(
            "participants must contain at least the payer's membership entry"
        )

    entries: list[tuple[str, int]] = []
    seen: dict[str, int] = {}
    for entry in participants:
        if not isinstance(entry, Mapping):
            raise InvalidConversionCommandError(
                "Each membership entry must be a mapping with participant_public_id and is_included"
            )
        extra = sorted(set(entry) - _ENTRY_FIELDS)
        if extra:
            raise InvalidConversionCommandError(
                f"Unknown membership entry fields are rejected: {extra}"
            )
        pid = entry.get("participant_public_id")
        if not isinstance(pid, str) or not pid or pid != pid.strip():
            raise InvalidConversionCommandError(
                "Membership entry participant_public_id must be a non-empty string"
            )
        if "is_included" not in entry:
            raise IncompleteReceiptInputsError(
                f"Membership entry for {pid!r} is missing its explicit is_included value"
            )
        included = _explicit_inclusion(pid, entry["is_included"])
        if pid in seen:
            if seen[pid] != included:
                raise AmbiguousReceiptInputError(
                    f"Contradictory membership entries for participant {pid!r}"
                )
            raise AmbiguousReceiptInputError(
                f"Duplicate membership entries for participant {pid!r}"
            )
        seen[pid] = included
        entries.append((pid, included))

    if command.payer_participant_public_id not in seen:
        raise IncompleteReceiptInputsError(
            "The payer must appear exactly once in the participant-membership structure"
        )
    entries.sort(key=lambda item: item[0])
    return entries


def _explicit_inclusion(pid: str, value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise IncompleteReceiptInputsError(
        f"Membership entry for {pid!r} must carry an explicit is_included value of 0 or 1"
    )


# ---------------------------------------------------------------------------
# Guards 2-3: staging database and transaction acquisition
# ---------------------------------------------------------------------------


def _require_staging(conn: sqlite3.Connection) -> None:
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise ConversionStagingDatabaseRejectedError(str(exc)) from exc
    # R4-F1: the conversion write boundary depends on FK enforcement for its
    # schema backstops; never trust migration/runtime defaults on this
    # connection — verify before the transaction begins or anything is written.
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise ConversionForeignKeysDisabledError(str(exc)) from exc


def _acquire_write_transaction(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        # Caller-owned transaction: reject without touching its state.
        raise ConversionCallerOwnedTransactionError(
            "Conversion unit of work requires a connection without pending work"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        _rollback_if_needed(conn)
        raise ConversionPersistenceError(
            "Could not acquire the conversion write transaction"
        ) from exc


# ---------------------------------------------------------------------------
# Guard 4: idempotency lookup (replay or conflict)
# ---------------------------------------------------------------------------


def _get_registry_row(conn: sqlite3.Connection, command_public_id: str) -> dict[str, Any] | None:
    """Round 5 fix: found/not-found is decided by the registry table alone.

    The dependent ``receipts`` and ``parser_outputs`` rows are attached via
    LEFT JOIN only: a destroyed facts or evidence row must never disguise a
    durable registry row as absent, or guard 4 would silently re-run the
    conversion instead of failing closed.
    """
    cursor = conn.execute(
        """
        SELECT rpc.command_public_id, rpc.parser_output_id,
               rpc.supersession_root_parser_output_id, rpc.receipt_id,
               rpc.confirmation_public_id, rpc.proposal_content_hash,
               rpc.command_material_hash, rpc.conversion_result_hash,
               rpc.actor_type, rpc.authenticated_actor_id, rpc.created_at,
               r.public_id AS receipt_public_id,
               po.public_id AS proposal_public_id
        FROM receipt_proposal_conversions AS rpc
        LEFT JOIN receipts AS r ON r.id = rpc.receipt_id
        LEFT JOIN parser_outputs AS po ON po.id = rpc.parser_output_id
        WHERE rpc.command_public_id = ?
        """,
        (command_public_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_dict(cursor, row)


def _require_replay_material(
    existing: dict[str, Any],
    material_hash: str,
    command: ReceiptFactsConversionCommand,
) -> None:
    # Lookup is by command_public_id only; the persisted material hash is the
    # authoritative comparison.  ``reason`` is excluded from the material.
    if existing["command_material_hash"] != material_hash:
        raise ConversionIdempotencyConflictError(
            f"Conversion command {command.command_public_id} already exists "
            "with different canonical material"
        )


def _replay_result(existing: dict[str, Any]) -> ReceiptFactsConversionResult:
    # Built only after _verify_replay_integrity passed, so the LEFT-JOINed
    # receipt and proposal public IDs are guaranteed to be present.
    return ReceiptFactsConversionResult(
        command_public_id=str(existing["command_public_id"]),
        proposal_public_id=str(existing["proposal_public_id"]),
        parser_output_id=int(existing["parser_output_id"]),
        receipt_public_id=str(existing["receipt_public_id"]),
        receipt_id=int(existing["receipt_id"]),
        confirmation_public_id=str(existing["confirmation_public_id"]),
        proposal_content_hash=str(existing["proposal_content_hash"]),
        command_material_hash=str(existing["command_material_hash"]),
        conversion_result_hash=str(existing["conversion_result_hash"]),
        idempotent=True,
    )


_REPLAY_DRIFT_MESSAGE = (
    "Guard 4 replay refused: the persisted conversion state no longer "
    "matches its registered result binding"
)
_REPLAY_AUDIT_DRIFT_MESSAGE = (
    "Guard 4 replay refused: the conversion audit state no longer matches its registered binding"
)

# Facts-only v1 never writes these receipts columns: both the fresh-write
# verification (_verify_persisted) and the replay verification treat any
# non-NULL value as drift of the persisted conversion state.
NEVER_WRITTEN_RECEIPT_COLUMNS = (
    "transaction_id",
    "gross_amount",
    "subtotal_amount",
    "service_charge_amount",
    "tax_amount",
    "discount_amount",
    "payment_record_attachment_id",
    "notes",
)
"""Receipt columns a B4.1 facts-only conversion never writes.

Shared contract: B4.1 replay verification and the B4.2 readiness boundary
both treat a non-NULL value in any of these columns on a registry-bound
receipt as historical drift; the two consumers must never diverge.
"""


def _verify_replay_integrity(conn: sqlite3.Connection, existing: dict[str, Any]) -> None:
    """Round 3/4 replay hardening: never trust the registry row alone.

    Before an exact replay returns, the current ``receipts`` row, the
    complete deterministic membership set (derived membership public IDs,
    participant linkage, role semantics, inclusion, and row count), the
    evidence lineage hash, and the conversion audit event are re-verified
    against the registered ``conversion_result_hash`` binding.  Missing or
    bypassed triggers and historical drift therefore fail closed as
    ``ConversionPersistenceError`` instead of returning a stale success;
    nothing is ever silently repaired.
    """
    receipt_cursor = conn.execute(
        """
        SELECT public_id, transaction_id, merchant, receipt_datetime,
               gross_amount, subtotal_amount, service_charge_amount,
               tax_amount, discount_amount, net_paid_amount,
               net_paid_amount_canonical_text, currency,
               payer_participant_id, source_channel, raw_input,
               attachment_id, attachment_path, payment_record_attachment_id,
               ocr_confidence, parser_output_id, status, notes
        FROM receipts WHERE id = ?
        """,
        (existing["receipt_id"],),
    )
    receipt_row = receipt_cursor.fetchone()
    if receipt_row is None:
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    receipt = _row_dict(receipt_cursor, receipt_row)
    payer_row = conn.execute(
        "SELECT public_id FROM participants WHERE id = ?",
        (receipt["payer_participant_id"],),
    ).fetchone()
    member_rows = conn.execute(
        """
        SELECT rp.public_id, rp.participant_id, rp.role, rp.is_included,
               p.public_id
        FROM receipt_participants AS rp
        LEFT JOIN participants AS p ON p.id = rp.participant_id
        WHERE rp.receipt_id = ?
        ORDER BY p.public_id
        """,
        (existing["receipt_id"],),
    ).fetchall()
    hash_rows = conn.execute(
        """
        SELECT e.source_attachment_hash, e.attachment_id
        FROM receipt_ocr_proposal_links AS l
        JOIN receipt_ocr_extractions AS e ON e.id = l.extraction_id
        WHERE l.parser_output_id = ?
        """,
        (existing["parser_output_id"],),
    ).fetchall()
    if payer_row is None or len(hash_rows) != 1:
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    # R4-F3: membership rows are authoritative Facts.  Verify the complete
    # deterministic set - derived membership public IDs, participant
    # linkage, payer/non-payer role semantics, and inclusion - not only the
    # (participant, is_included) pairs the result hash happens to cover.
    entries: list[tuple[str, int]] = []
    payer_membership_rows = 0
    for member in member_rows:
        participant_public_id = member[4]
        if participant_public_id is None:
            raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
        included = int(member[3])
        if str(member[0]) != _derive_membership_public_id(
            str(existing["command_public_id"]), str(participant_public_id)
        ):
            raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
        if int(member[1]) == int(receipt["payer_participant_id"]):
            payer_membership_rows += 1
            expected_role = _ROLE_PAYER
        else:
            expected_role = _ROLE_PARTICIPANT if included == 1 else _ROLE_EXCLUDED
        if str(member[2]) != expected_role:
            raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
        entries.append((str(participant_public_id), included))
    if payer_membership_rows != 1:
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    recomputed = _conversion_result_hash(
        receipt_public_id=str(receipt["public_id"]),
        facts={
            "merchant": receipt["merchant"],
            "receipt_date": receipt["receipt_datetime"],
            "canonical_amount": receipt["net_paid_amount_canonical_text"],
            "currency": receipt["currency"],
        },
        payer_public_id=str(payer_row[0]),
        entries=entries,
        attachment_content_hash=str(hash_rows[0][0]),
        proposal_content_hash=str(existing["proposal_content_hash"]),
        command_material_hash=str(existing["command_material_hash"]),
    )
    if recomputed != str(existing["conversion_result_hash"]):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    # The Section 12.2 mirror contract is part of the persisted facts: the
    # legacy NUMERIC compatibility column must still decode Decimal-equal to
    # the authoritative canonical text under the same explicit rule used by
    # the fresh-write path (_verify_persisted).  A drifted mirror alone is
    # drift, never a stale idempotent success.
    mirrored_decimal = decimal_from_numeric_mirror(receipt["net_paid_amount"])
    if mirrored_decimal is None or mirrored_decimal != money_decimal(
        str(receipt["net_paid_amount_canonical_text"])
    ):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    # R4-F4: the registered result hash only covers seven receipt fields.
    # Rebuild every remaining conversion-written field from the proposal,
    # the raw intake source evidence, the attachment evidence, and the
    # deterministic conversion contract; any historical drift, missing
    # evidence, or extra binding fails closed.
    if any(receipt[name] is not None for name in NEVER_WRITTEN_RECEIPT_COLUMNS):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if receipt["status"] != "confirmed":
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if receipt["public_id"] != derive_receipt_public_id(str(existing["command_public_id"])):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if receipt["parser_output_id"] is None or int(receipt["parser_output_id"]) != int(
        existing["parser_output_id"]
    ):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    proposal_row = conn.execute(
        "SELECT attachment_id, confidence_score, source_public_id FROM parser_outputs WHERE id = ?",
        (existing["parser_output_id"],),
    ).fetchone()
    if proposal_row is None or proposal_row[0] is None:
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if receipt["attachment_id"] is None or int(receipt["attachment_id"]) != int(proposal_row[0]):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if receipt["ocr_confidence"] != proposal_row[1]:
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if int(hash_rows[0][1]) != int(proposal_row[0]):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    if proposal_row[2] is not None:
        intake_row = conn.execute(
            "SELECT source_channel, raw_input FROM raw_intake_records WHERE public_id = ?",
            (proposal_row[2],),
        ).fetchone()
    else:
        # No immutable source binding: fall back to the (unique) lineage
        # pointer, exactly one row as required by the fresh-write guard 8.
        intake_rows = conn.execute(
            "SELECT source_channel, raw_input FROM raw_intake_records WHERE parser_output_id = ?",
            (existing["parser_output_id"],),
        ).fetchall()
        intake_row = intake_rows[0] if len(intake_rows) == 1 else None
    if (
        intake_row is None
        or receipt["source_channel"] != intake_row[0]
        or receipt["raw_input"] != intake_row[1]
    ):
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    attachment_row = conn.execute(
        "SELECT file_path FROM attachments WHERE id = ?", (proposal_row[0],)
    ).fetchone()
    if attachment_row is None or receipt["attachment_path"] != attachment_row[0]:
        raise ConversionPersistenceError(_REPLAY_DRIFT_MESSAGE)
    _verify_replay_audit_binding(conn, existing, receipt_public_id=str(receipt["public_id"]))


def _verify_replay_audit_binding(
    conn: sqlite3.Connection,
    existing: dict[str, Any],
    *,
    receipt_public_id: str,
) -> None:
    """R4-F5: verify the full audit semantic binding on replay.

    Chain hash self-consistency alone cannot prove the event still records
    what this conversion wrote: a forger who recomputes valid hashes keeps
    the chain verifying while actor, authorization, references, states, or
    payload drift.  Replay therefore rebuilds every semantic field from the
    registry row, the proposal, and the evidence lineage - the same
    expectations ``_verify_persisted_audit_event`` holds against a fresh
    write - and fails closed on any mismatch.
    """
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        causation_public_id=str(existing["command_public_id"]),
    )
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        fetched = FinancialAuditRepository(conn).fetch(event_id)
        chain_check = verify_financial_audit_chain(
            conn, aggregate_type="receipt", aggregate_public_id=receipt_public_id
        )
    finally:
        conn.row_factory = previous_factory
    if fetched is None:
        raise ConversionPersistenceError(_REPLAY_AUDIT_DRIFT_MESSAGE)
    # Rebuild the deterministic source evidence reference set from the
    # registered proposal and its single OCR extraction lineage.
    proposal_row = conn.execute(
        "SELECT source_public_id, attachment_id FROM parser_outputs WHERE id = ?",
        (existing["parser_output_id"],),
    ).fetchone()
    extraction_rows = conn.execute(
        """
        SELECT e.public_id
        FROM receipt_ocr_proposal_links AS l
        JOIN receipt_ocr_extractions AS e ON e.id = l.extraction_id
        WHERE l.parser_output_id = ?
        """,
        (existing["parser_output_id"],),
    ).fetchall()
    if proposal_row is None or len(extraction_rows) != 1:
        raise ConversionPersistenceError(_REPLAY_AUDIT_DRIFT_MESSAGE)
    references = [f"parser-output:{existing['proposal_public_id']}"]
    if proposal_row[0]:
        references.append(f"source:{proposal_row[0]}")
    if proposal_row[1] is not None:
        references.append(f"attachment-id:{proposal_row[1]}")
    references.append(f"extraction:{extraction_rows[0][0]}")
    references.append(f"confirmation:{existing['confirmation_public_id']}")
    expected_payload = canonical_json_text(
        {
            "command_public_id": str(existing["command_public_id"]),
            "proposal_public_id": str(existing["proposal_public_id"]),
            "confirmation_public_id": str(existing["confirmation_public_id"]),
            "proposal_content_hash": str(existing["proposal_content_hash"]),
            "command_material_hash": str(existing["command_material_hash"]),
            "conversion_result_hash": str(existing["conversion_result_hash"]),
        }
    )
    expected_previous_state = canonical_json_text(
        {
            "conversion_status": "not_converted",
            "parse_status": CONFIRMED,
            "proposal_content_hash": str(existing["proposal_content_hash"]),
        }
    )
    expected_new_state = canonical_json_text(
        {
            "conversion_status": "converted_to_receipt_facts",
            "receipt_public_id": receipt_public_id,
            "proposal_content_hash": str(existing["proposal_content_hash"]),
            "conversion_result_hash": str(existing["conversion_result_hash"]),
        }
    )
    if (
        fetched.event_public_id != event_id
        or fetched.audit_schema_version != AUDIT_SCHEMA_VERSION
        or fetched.aggregate_type != "receipt"
        or fetched.aggregate_public_id != receipt_public_id
        or fetched.event_type != RECEIPT_FACTS_CONVERSION_EVENT_TYPE
        or fetched.actor_type != "human"
        or str(existing["actor_type"]) != "human"
        or fetched.actor_public_id != str(existing["authenticated_actor_id"])
        or fetched.authorization_public_id != str(existing["confirmation_public_id"])
        or fetched.causation_public_id != str(existing["command_public_id"])
        or fetched.correlation_public_id != receipt_public_id
        # Round 5 fix 2: the fresh conversion wrote the registry row and the
        # audit event with one shared timestamp; a chain-valid recomputed
        # event hash cannot legitimise a drifted created_at.
        or fetched.created_at != str(existing["created_at"])
        or fetched.calculation_snapshot_public_id is not None
        or fetched.calculation_snapshot_hash is not None
        or fetched.source_evidence_references != tuple(sorted(references))
        or fetched.event_payload_json != expected_payload
        or fetched.previous_state_json != expected_previous_state
        or fetched.new_state_json != expected_new_state
    ):
        raise ConversionPersistenceError(_REPLAY_AUDIT_DRIFT_MESSAGE)
    if not chain_check.valid or chain_check.legacy_without_chain:
        raise ConversionPersistenceError(_REPLAY_AUDIT_DRIFT_MESSAGE)
    # The conversion event must still be the genesis, and only, event of
    # its receipt aggregate (mirrors the fresh-write round 3 fix F6).
    if (
        fetched.sequence_number != 1
        or fetched.previous_event_hash != ZERO_AUDIT_HASH
        or chain_check.event_count != 1
    ):
        raise ConversionPersistenceError(_REPLAY_AUDIT_DRIFT_MESSAGE)


# ---------------------------------------------------------------------------
# Guards 5-11: proposal state
# ---------------------------------------------------------------------------


def _require_proposal(conn: sqlite3.Connection, proposal_public_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE public_id = ?",
        (proposal_public_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ConversionProposalNotFoundError(f"Parser proposal not found: {proposal_public_id!r}")
    return _row_dict(cursor, row)


def _require_single_receipt_link(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    cursor = conn.execute(
        """
        SELECT id, public_id, extraction_id, parser_contract_version, link_role
        FROM receipt_ocr_proposal_links
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise UnsupportedConversionProposalTypeError(
            "Only receipt OCR total proposals with exactly one persisted OCR "
            "link can be converted to receipt facts"
        )
    return _row_dict(cursor, rows[0])


def _require_leaf(conn: sqlite3.Connection, proposal: dict[str, Any]) -> None:
    if str(proposal["parse_status"]) == SUPERSEDED:
        raise StaleConversionTargetError(
            "A superseded proposal cannot be converted; convert the current leaf"
        )
    child = conn.execute(
        "SELECT 1 FROM parser_outputs WHERE parent_parser_output_id = ? LIMIT 1",
        (proposal["id"],),
    ).fetchone()
    if child is not None:
        raise StaleConversionTargetError(
            "Only the current leaf of a supersession chain can be converted"
        )


def _require_single_raw_intake(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    cursor = conn.execute(
        """
        SELECT id, public_id, attachment_id, attachment_path, attachment_hash,
               status, source_channel, raw_input, source_content_hash
        FROM raw_intake_records
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise StaleConversionTargetError(
            "Only the current proposal for a raw intake record can be converted"
        )
    if len(rows) > 1:
        raise ConversionEvidenceLineageError(
            f"Proposal {parser_output_id} is bound to {len(rows)} raw intake "
            "records; ambiguous source binding fails closed"
        )
    return _row_dict(cursor, rows[0])


def _require_confirmed_status(proposal: dict[str, Any]) -> None:
    status = str(proposal["parse_status"])
    if status != CONFIRMED:
        raise ProposalNotConfirmedError(
            f"Proposal must be confirmed before conversion, got status {status!r}"
        )


def _require_bound_confirmation(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    expected_content_hash: str,
) -> tuple[str, dict[str, Any]]:
    """Guard 10: Section 6 triple hash equality against a human confirmation."""
    cursor = conn.execute(
        """
        SELECT confirmation_public_id, parser_output_id, proposal_content_hash,
               actor_type, authenticated_actor_id, confirmation_state
        FROM parser_proposal_authorizations
        WHERE parser_output_id = ?
        """,
        (proposal["id"],),
    )
    row = cursor.fetchone()
    if row is None:
        raise ProposalNotConfirmedError(
            "Proposal has no authenticated confirmation authorization record"
        )
    authorization = _row_dict(cursor, row)
    state = str(authorization["confirmation_state"])
    if state != "confirmed":
        raise StaleConfirmationHashError(
            f"Confirmation authorization is {state!r}; conversion requires a "
            "live confirmed authorization"
        )
    if str(authorization["actor_type"]) != "human":
        raise StaleConfirmationHashError(
            "Confirmation authorization was not made by an authenticated human"
        )
    try:
        effective_hash = compute_effective_proposal_content_hash(conn, proposal)
    except ProposalContentHashError as exc:
        raise ConversionEvidenceLineageError(str(exc)) from exc
    if expected_content_hash != effective_hash:
        raise StaleConfirmationHashError(
            "The command's expected content hash does not match the recomputed "
            "effective proposal content hash"
        )
    if str(authorization["proposal_content_hash"]) != effective_hash:
        raise StaleConfirmationHashError(
            "The confirmation-bound content hash does not match the recomputed "
            "effective proposal content hash"
        )
    return effective_hash, authorization


def _walk_supersession_chain(
    conn: sqlite3.Connection, parser_output_id: int
) -> tuple[list[int], int]:
    """Return every chain member ID plus the supersession-chain root ID."""
    seen: set[int] = set()
    current = parser_output_id
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT parent_parser_output_id FROM parser_outputs WHERE id = ?",
            (current,),
        ).fetchone()
        parent = row[0] if row is not None else None
        if parent is None:
            break
        current = int(parent)
    root = current
    members: list[int] = []
    queue = [root]
    visited: set[int] = set()
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        members.append(node)
        for child_row in conn.execute(
            "SELECT id FROM parser_outputs WHERE parent_parser_output_id = ?",
            (node,),
        ).fetchall():
            queue.append(int(child_row[0]))
    return members, root


def _require_not_converted(conn: sqlite3.Connection, chain_ids: Sequence[int]) -> None:
    for member_id in chain_ids:
        if has_legacy_transaction_conversion(conn, member_id):
            raise ReceiptFactsAlreadyConvertedError(
                f"Proposal chain member {member_id} was already converted "
                "through the legacy transaction path"
            )
    placeholders = ",".join("?" for _ in chain_ids)
    row = conn.execute(
        "SELECT 1 FROM receipt_proposal_conversions "
        f"WHERE parser_output_id IN ({placeholders}) "
        f"OR supersession_root_parser_output_id IN ({placeholders}) LIMIT 1",
        (*chain_ids, *chain_ids),
    ).fetchone()
    if row is not None:
        raise ReceiptFactsAlreadyConvertedError(
            "This proposal's supersession chain already has receipt facts; "
            "one receipt fact set per proposal chain"
        )


# ---------------------------------------------------------------------------
# Guard 12: evidence lineage consistency
# ---------------------------------------------------------------------------


def _require_evidence_lineage(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    link: dict[str, Any],
    raw_intake: dict[str, Any],
    effective: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT id, public_id, attachment_id, source_attachment_hash
        FROM receipt_ocr_extractions
        WHERE id = ?
        """,
        (link["extraction_id"],),
    )
    row = cursor.fetchone()
    if row is None:
        raise ConversionEvidenceLineageError(
            "The proposal's OCR link references a missing extraction row"
        )
    extraction = _row_dict(cursor, row)

    proposal_attachment_id = proposal.get("attachment_id")
    if proposal_attachment_id is None:
        raise ConversionEvidenceLineageError(
            "A receipt OCR proposal must carry its source attachment identity"
        )
    if int(extraction["attachment_id"]) != int(proposal_attachment_id):
        raise ConversionEvidenceLineageError(
            "The OCR extraction's attachment does not match the proposal's attachment"
        )

    att_cursor = conn.execute(
        "SELECT id, public_id, file_path, file_hash FROM attachments WHERE id = ?",
        (proposal_attachment_id,),
    )
    att_row = att_cursor.fetchone()
    if att_row is None:
        raise ConversionEvidenceLineageError(
            "The proposal's attachment row is missing from the evidence chain"
        )
    attachment = _row_dict(att_cursor, att_row)
    stored_hash = attachment.get("file_hash")
    if stored_hash is not None and str(stored_hash) != str(extraction["source_attachment_hash"]):
        raise ConversionEvidenceLineageError(
            "The attachment content hash disagrees with the OCR extraction's "
            "verified source attachment hash"
        )

    source_public_id = proposal.get("source_public_id")
    if source_public_id is not None and raw_intake["public_id"] != source_public_id:
        raise ConversionEvidenceLineageError(
            "The raw intake record does not match the proposal's source identity"
        )
    intake_attachment_id = raw_intake.get("attachment_id")
    if intake_attachment_id is not None and int(intake_attachment_id) != int(
        proposal_attachment_id
    ):
        raise ConversionEvidenceLineageError(
            "The raw intake attachment identity does not match the proposal's attachment"
        )
    expected_status = raw_intake_status_for_proposal_status(str(proposal["parse_status"]))
    if str(raw_intake["status"]) != expected_status:
        raise ConversionEvidenceLineageError(
            f"Raw intake status {raw_intake['status']!r} disagrees with the "
            f"proposal lifecycle status (expected {expected_status!r})"
        )

    if str(link["link_role"]) == "ai_fallback":
        try:
            _effective, _completion_public_id, proposal_version = resolve_effective_payload(
                conn, proposal
            )
            verified = verify_ai_fallback_child(
                conn,
                proposal,
                content_hash=compute_effective_proposal_content_hash(conn, proposal),
                proposal_version=proposal_version,
            )
        except (AiFallbackServiceError, EffectivePayloadError, ProposalContentHashError) as exc:
            raise ConversionEvidenceLineageError(
                "The AI fallback proposal lineage could not be verified"
            ) from exc
        if verified is None or verified.get("proposal_origin") != "ai_fallback":
            raise ConversionEvidenceLineageError(
                "An AI fallback OCR proposal requires a valid immutable AI lineage edge"
            )

    _require_monetary_evidence_consistency(conn, proposal, effective)
    return extraction, attachment


def _require_monetary_evidence_consistency(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    effective: dict[str, Any],
) -> None:
    """Persisted monetary field evidence must agree with the effective payload."""
    rows = conn.execute(
        """
        SELECT field_name, proposed_value
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ? AND field_name IN ('amount', 'currency')
        """,
        (proposal["id"],),
    ).fetchall()
    amount_rows = [r for r in rows if str(r[0]) == "amount"]
    currency_rows = [r for r in rows if str(r[0]) == "currency"]

    effective_amount = effective.get("amount")
    if effective_amount is not None:
        effective_decimal = _try_money_decimal(effective_amount)
        if effective_decimal is not None:
            if not amount_rows:
                raise ConversionEvidenceLineageError(
                    "The effective amount has no persisted field evidence row"
                )
            for row in amount_rows:
                evidence_decimal = _try_money_decimal(row[1])
                if evidence_decimal is None or evidence_decimal != effective_decimal:
                    raise ConversionEvidenceLineageError(
                        "Persisted amount evidence conflicts with the effective proposal payload"
                    )

    effective_currency = effective.get("currency")
    if effective_currency is not None and isinstance(effective_currency, str):
        canonical = _try_normalize_currency(effective_currency)
        if canonical is not None:
            if not currency_rows:
                raise ConversionEvidenceLineageError(
                    "The effective currency has no persisted field evidence row"
                )
            for row in currency_rows:
                evidence_canonical = _try_normalize_currency(
                    row[1] if isinstance(row[1], str) else None
                )
                if evidence_canonical != canonical:
                    raise ConversionEvidenceLineageError(
                        "Persisted currency evidence conflicts with the effective proposal payload"
                    )


# ---------------------------------------------------------------------------
# Source binding revalidation (telegram_attachment_source, runs after guard 12)
# ---------------------------------------------------------------------------


def _require_source_binding(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    raw_intake: dict[str, Any],
    extraction: dict[str, Any],
    attachment: dict[str, Any],
) -> None:
    """Revalidate the durable source binding at conversion time.

    Ingestion verified the source binding once; conversion re-verifies it
    against the persisted evidence chain so that binding drift after
    ingestion fails closed.  Supports both telegram and local_file channels
    with equivalent strictness.
    """
    source_public_id = proposal.get("source_public_id")
    if not source_public_id or str(raw_intake["public_id"]) != str(source_public_id):
        raise ConversionEvidenceLineageError(
            "The proposal's source identity is missing or does not match its raw intake record"
        )
    source_channel = str(raw_intake.get("source_channel") or "")
    if source_channel not in ("telegram", "local_file"):
        raise ConversionEvidenceLineageError(
            "Receipt facts conversion requires the raw intake source channel to be "
            f"'telegram' or 'local_file', got {source_channel!r}"
        )
    # Durable raw input binding (runs before writes and again at pre-commit
    # revalidation): when intake recorded a source content hash, the raw
    # input text must still hash to it, so post-intake tampering with the
    # authoritative raw input fails closed.  Rows ingested before the hash
    # column existed carry NULL and stay convertible; the migration 035
    # freeze trigger prevents new drift either way.
    stored_content_hash = raw_intake.get("source_content_hash")
    if stored_content_hash is not None:
        recomputed_hash = "sha256:" + _sha256_hex(str(raw_intake["raw_input"]))
        if str(stored_content_hash) != recomputed_hash:
            raise ConversionEvidenceLineageError(
                "The raw intake source content hash no longer matches its raw input"
            )
    identities = {
        proposal.get("attachment_id"),
        raw_intake.get("attachment_id"),
        extraction.get("attachment_id"),
        attachment.get("id"),
    }
    if None in identities or len({int(i) for i in identities if i is not None}) != 1:
        raise ConversionEvidenceLineageError(
            "Proposal, raw intake, OCR extraction, and attachment identities "
            "must be present and identical"
        )
    attachment_id = int(attachment["id"])

    if source_channel == "telegram":
        _require_telegram_source_binding(conn, raw_intake, extraction, attachment, attachment_id)
    else:
        _require_local_source_binding(conn, raw_intake, extraction, attachment, attachment_id)


def _require_telegram_source_binding(
    conn: sqlite3.Connection,
    raw_intake: dict[str, Any],
    extraction: dict[str, Any],
    attachment: dict[str, Any],
    attachment_id: int,
) -> None:
    """Validate the telegram_attachment_source binding (existing logic)."""
    cursor = conn.execute(
        """
        SELECT id, public_id, raw_intake_record_id, attachment_id,
               original_attachment_path, content_hash, source_evidence_payload
        FROM telegram_attachment_source
        WHERE attachment_id = ?
        ORDER BY id
        """,
        (attachment_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ConversionEvidenceLineageError(
            f"Expected exactly one telegram attachment source binding for the "
            f"proposal's attachment, found {len(rows)}"
        )
    binding = _row_dict(cursor, rows[0])
    if int(binding["raw_intake_record_id"]) != int(raw_intake["id"]):
        raise ConversionEvidenceLineageError(
            "The telegram attachment source binding references a different raw intake record"
        )

    content_hash = str(binding["content_hash"])
    if str(extraction["source_attachment_hash"]) != content_hash:
        raise ConversionEvidenceLineageError(
            "The OCR extraction's verified source hash does not match the "
            "telegram attachment source content hash"
        )
    if attachment.get("file_hash") is None or str(attachment["file_hash"]) != content_hash:
        raise ConversionEvidenceLineageError(
            "The canonical attachment content hash is missing or does not "
            "match the telegram attachment source content hash"
        )
    intake_hash = raw_intake.get("attachment_hash")
    if intake_hash is not None and str(intake_hash) != content_hash:
        raise ConversionEvidenceLineageError(
            "The raw intake attachment hash does not match the telegram "
            "attachment source content hash"
        )

    original_path = binding.get("original_attachment_path")
    if not isinstance(original_path, str) or not original_path.strip():
        raise ConversionEvidenceLineageError(
            "The telegram attachment source binding has no original attachment path"
        )
    if attachment.get("file_path") != original_path:
        raise ConversionEvidenceLineageError(
            "The canonical attachment path does not match the binding's original attachment path"
        )
    intake_path = raw_intake.get("attachment_path")
    if intake_path is not None and str(intake_path) != original_path:
        raise ConversionEvidenceLineageError(
            "The raw intake attachment path does not match the binding's original attachment path"
        )
    try:
        evidence_payload = json.loads(str(binding["source_evidence_payload"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConversionEvidenceLineageError(
            "The telegram attachment source evidence payload is not valid JSON"
        ) from exc
    if not isinstance(evidence_payload, dict):
        raise ConversionEvidenceLineageError(
            "The telegram attachment source evidence payload is not a JSON object"
        )
    canonical_path = evidence_payload.get("canonical_path")
    if (
        not isinstance(canonical_path, str)
        or not canonical_path.strip()
        or not os.path.isabs(canonical_path)
    ):
        raise ConversionEvidenceLineageError(
            "The telegram attachment source evidence payload has no absolute canonical path"
        )
    if evidence_payload.get("content_hash") != content_hash:
        raise ConversionEvidenceLineageError(
            "The telegram attachment source evidence payload hash disagrees "
            "with the binding's content hash"
        )


def _require_local_source_binding(
    conn: sqlite3.Connection,
    raw_intake: dict[str, Any],
    extraction: dict[str, Any],
    attachment: dict[str, Any],
    attachment_id: int,
) -> None:
    """Validate the local_attachment_source binding (equivalent strictness)."""
    cursor = conn.execute(
        """
        SELECT id, public_id, raw_intake_record_id, attachment_id,
               workspace_copy_path, content_hash, source_evidence_payload
        FROM local_attachment_source
        WHERE attachment_id = ?
        ORDER BY id
        """,
        (attachment_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ConversionEvidenceLineageError(
            f"Expected exactly one local attachment source binding for the "
            f"proposal's attachment, found {len(rows)}"
        )
    binding = _row_dict(cursor, rows[0])
    if int(binding["raw_intake_record_id"]) != int(raw_intake["id"]):
        raise ConversionEvidenceLineageError(
            "The local attachment source binding references a different raw intake record"
        )

    content_hash = str(binding["content_hash"])
    if str(extraction["source_attachment_hash"]) != content_hash:
        raise ConversionEvidenceLineageError(
            "The OCR extraction's verified source hash does not match the "
            "local attachment source content hash"
        )
    if attachment.get("file_hash") is None or str(attachment["file_hash"]) != content_hash:
        raise ConversionEvidenceLineageError(
            "The canonical attachment content hash is missing or does not "
            "match the local attachment source content hash"
        )
    intake_hash = raw_intake.get("attachment_hash")
    if intake_hash is not None and str(intake_hash) != content_hash:
        raise ConversionEvidenceLineageError(
            "The raw intake attachment hash does not match the local attachment source content hash"
        )

    workspace_copy_path = binding.get("workspace_copy_path")
    if not isinstance(workspace_copy_path, str) or not workspace_copy_path.strip():
        raise ConversionEvidenceLineageError(
            "The local attachment source binding has no workspace copy path"
        )
    if attachment.get("file_path") != workspace_copy_path:
        raise ConversionEvidenceLineageError(
            "The canonical attachment path does not match the binding's workspace copy path"
        )
    intake_path = raw_intake.get("attachment_path")
    if intake_path is not None and str(intake_path) != workspace_copy_path:
        raise ConversionEvidenceLineageError(
            "The raw intake attachment path does not match the binding's workspace copy path"
        )
    try:
        evidence_payload = json.loads(str(binding["source_evidence_payload"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConversionEvidenceLineageError(
            "The local attachment source evidence payload is not valid JSON"
        ) from exc
    if not isinstance(evidence_payload, dict):
        raise ConversionEvidenceLineageError(
            "The local attachment source evidence payload is not a JSON object"
        )
    if evidence_payload.get("content_hash") != content_hash:
        raise ConversionEvidenceLineageError(
            "The local attachment source evidence payload hash disagrees "
            "with the binding's content hash"
        )


# ---------------------------------------------------------------------------
# Guard 13: ambiguity flags resolved
# ---------------------------------------------------------------------------


def _require_flags_resolved(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    effective: dict[str, Any],
) -> None:
    # Round 3 fix F4 (design Section 12.4): the flags must be explicitly
    # present as a JSON list.  A missing key or a top-level null is an
    # unresolved-ambiguity claim, never an implicit empty list; only the
    # explicit ``[]`` states "no outstanding ambiguity".
    if "ambiguity_flags" not in effective:
        raise AmbiguousReceiptInputError(
            "Effective proposal must carry an explicit ambiguity_flags list; "
            "a missing key fails closed"
        )
    flags = effective["ambiguity_flags"]
    if not isinstance(flags, list):
        raise AmbiguousReceiptInputError(
            "Effective proposal ambiguity_flags must be a list of recognized "
            f"flag tokens, got: {type(flags).__name__}"
        )
    malformed = sorted(
        repr(flag)
        for flag in flags
        if not isinstance(flag, str) or flag not in _KNOWN_AMBIGUITY_FLAGS
    )
    if malformed:
        raise AmbiguousReceiptInputError(
            "Effective proposal carries malformed or unrecognized ambiguity "
            f"flags; conversion fails closed: {malformed}"
        )
    # Durable provenance verification is unconditional: an empty flag list
    # can state "no outstanding ambiguity" but never bypasses supersession
    # revision evidence, inherited correction/completion verification, or
    # effective-value binding to the durable rows.
    corrected, completed = _durable_resolution_provenance(conn, proposal, effective)
    outstanding: list[str] = []
    for flag in flags:
        if flag in _AMOUNT_FLAGS and "amount" not in corrected:
            outstanding.append(str(flag))
        elif flag in _CURRENCY_FLAGS and "currency" not in corrected:
            outstanding.append(str(flag))
        elif flag in _DATE_FLAGS:
            date_value = effective.get("transaction_date")
            human_supplied = "transaction_date" in completed or "transaction_date" in corrected
            if date_value is None or not human_supplied:
                outstanding.append(str(flag))
    if outstanding:
        raise AmbiguousReceiptInputError(
            "Effective proposal carries outstanding ambiguity flags material to "
            f"money, date, or currency: {sorted(set(outstanding))}"
        )


def _durable_resolution_provenance(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    effective: dict[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    """Durable corrected/completed field sets for ambiguity-flag clearing.

    Payload-embedded correction metadata and inherited human field evidence
    are claims, not proof: every claim must be backed by an append-only
    ``receipt_proposal_revisions`` row (corrections) or a
    ``parser_proposal_completions`` row (completions) on this proposal's
    parent path.  Missing, contradictory, or forged claims fail closed as
    ``ConversionEvidenceLineageError`` before any write.
    """
    path = _supersession_path(conn, int(proposal["id"]))
    public_ids = _proposal_public_ids(conn, path)
    corrected: set[str] = set()
    last_corrected_value: dict[str, Any] = {}
    leaf_revision: dict[str, Any] | None = None
    leaf_applied: dict[str, Any] | None = None
    for parent_id, child_id in zip(path, path[1:]):
        cursor = conn.execute(
            """
            SELECT correction_public_id, superseded_parser_output_id,
                   replacement_parser_output_id, applied_field_updates_json
            FROM receipt_proposal_revisions
            WHERE replacement_parser_output_id = ?
            """,
            (child_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ConversionEvidenceLineageError(
                "A supersession parent-child edge has no durable receipt proposal revision evidence"
            )
        revision = _row_dict(cursor, row)
        if int(revision["superseded_parser_output_id"]) != parent_id:
            raise ConversionEvidenceLineageError(
                "The durable revision evidence contradicts the supersession parent-child topology"
            )
        applied = _durable_json_object(
            revision["applied_field_updates_json"],
            "revision applied field updates",
        )
        corrected.update(str(name) for name in applied)
        last_corrected_value.update(applied)
        if child_id == path[-1]:
            leaf_revision = revision
            leaf_applied = applied

    _require_payload_correction_matches(effective, leaf_revision, leaf_applied, public_ids)

    completed: set[str] = set()
    leaf_completed_values: dict[str, Any] = {}
    for row in conn.execute(
        "SELECT field_updates_json FROM parser_proposal_completions "
        "WHERE parser_output_id = ? ORDER BY version_number",
        (path[-1],),
    ).fetchall():
        updates = _durable_json_object(row[0], "completion field updates")
        completed.update(str(name) for name in updates)
        # Ascending version order: the latest completion of a field wins.
        leaf_completed_values.update(updates)

    revision_index = _chain_revision_index(conn, path)
    evidence_items = effective.get("field_evidence")
    if isinstance(evidence_items, list):
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            if item.get("evidence_source_type") not in _HUMAN_EVIDENCE_SOURCE_TYPES:
                continue
            field_name = str(item.get("field_name"))
            if item.get("completion_public_id") is not None:
                _verify_inherited_completion_evidence(conn, item, field_name, path, public_ids)
                completed.add(field_name)
            elif item.get("correction_public_id") is not None:
                _verify_inherited_correction_evidence(item, field_name, revision_index, public_ids)
            else:
                raise ConversionEvidenceLineageError(
                    f"Human field evidence for {field_name!r} carries no "
                    "verifiable correction or completion provenance"
                )
            # The human claim must also equal the value the conversion is
            # about to persist: a durable row proves the claim happened, and
            # this binding proves the effective payload still carries it.
            if not _field_values_equal(
                field_name, item.get("proposed_value"), effective.get(field_name)
            ):
                raise ConversionEvidenceLineageError(
                    f"Human field evidence for {field_name!r} does not match "
                    "the effective proposal value it claims to support"
                )

    if "amount" in corrected:
        durable_amount = _try_money_decimal(last_corrected_value.get("amount"))
        effective_amount = _try_money_decimal(effective.get("amount"))
        if durable_amount is None or effective_amount is None or durable_amount != effective_amount:
            raise ConversionEvidenceLineageError(
                "The durably corrected amount does not match the effective proposal amount"
            )
    if "currency" in corrected:
        durable_currency = _try_normalize_currency(last_corrected_value.get("currency"))
        effective_currency = _try_normalize_currency(effective.get("currency"))
        if (
            durable_currency is None
            or effective_currency is None
            or durable_currency != effective_currency
        ):
            raise ConversionEvidenceLineageError(
                "The durably corrected currency does not match the effective proposal currency"
            )
    if "transaction_date" in leaf_completed_values:
        # A leaf completion is the latest durable word on the date; a
        # payload date that drifted afterwards is a forgery, not a fact.
        if leaf_completed_values["transaction_date"] != effective.get("transaction_date"):
            raise ConversionEvidenceLineageError(
                "The durably completed transaction date does not match the "
                "effective proposal transaction date"
            )
    elif "transaction_date" in corrected:
        if last_corrected_value.get("transaction_date") != effective.get("transaction_date"):
            raise ConversionEvidenceLineageError(
                "The durably corrected transaction date does not match the "
                "effective proposal transaction date"
            )
    return frozenset(corrected), frozenset(completed)


def _supersession_path(conn: sqlite3.Connection, leaf_id: int) -> list[int]:
    """Root-to-leaf parent path node IDs (leaf last); cycles fail closed."""
    path = [leaf_id]
    seen = {leaf_id}
    current = leaf_id
    while True:
        row = conn.execute(
            "SELECT parent_parser_output_id FROM parser_outputs WHERE id = ?",
            (current,),
        ).fetchone()
        parent = row[0] if row is not None else None
        if parent is None:
            break
        parent_id = int(parent)
        if parent_id in seen:
            raise ConversionEvidenceLineageError("The supersession parent chain contains a cycle")
        path.append(parent_id)
        seen.add(parent_id)
        current = parent_id
    path.reverse()
    return path


def _proposal_public_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, str]:
    placeholders = ",".join("?" for _ in ids)
    return {
        int(row[0]): str(row[1])
        for row in conn.execute(
            f"SELECT id, public_id FROM parser_outputs WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    }


def _durable_json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConversionEvidenceLineageError(f"Durable {label} are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ConversionEvidenceLineageError(f"Durable {label} are not a JSON object")
    return parsed


def _require_payload_correction_matches(
    effective: dict[str, Any],
    leaf_revision: dict[str, Any] | None,
    leaf_applied: dict[str, Any] | None,
    public_ids: dict[int, str],
) -> None:
    """The leaf payload's correction claim must equal the durable revision row."""
    claim = effective.get("correction")
    if leaf_revision is None or leaf_applied is None:
        if claim is not None:
            raise ConversionEvidenceLineageError(
                "The payload claims a correction but no durable receipt "
                "proposal revision evidence exists"
            )
        return
    if not isinstance(claim, dict):
        raise ConversionEvidenceLineageError(
            "A superseding replacement payload is missing its correction metadata"
        )
    parent_public_id = public_ids.get(int(leaf_revision["superseded_parser_output_id"]))
    if (
        claim.get("correction_public_id") != str(leaf_revision["correction_public_id"])
        or claim.get("corrected_fields") != sorted(str(name) for name in leaf_applied)
        or claim.get("superseded_proposal_public_id") != parent_public_id
        or claim.get("actor_type") != "human"
    ):
        raise ConversionEvidenceLineageError(
            "The payload correction metadata contradicts the durable receipt "
            "proposal revision evidence"
        )


def _chain_revision_index(
    conn: sqlite3.Connection, path: Sequence[int]
) -> dict[str, dict[str, Any]]:
    """Durable revisions on the parent path, indexed by correction public ID."""
    index: dict[str, dict[str, Any]] = {}
    for child_id in path[1:]:
        cursor = conn.execute(
            """
            SELECT correction_public_id, superseded_parser_output_id,
                   applied_field_updates_json
            FROM receipt_proposal_revisions
            WHERE replacement_parser_output_id = ?
            """,
            (child_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            revision = _row_dict(cursor, row)
            index[str(revision["correction_public_id"])] = revision
    return index


def _verify_inherited_correction_evidence(
    item: dict[str, Any],
    field_name: str,
    revision_index: dict[str, dict[str, Any]],
    public_ids: dict[int, str],
) -> None:
    revision = revision_index.get(str(item.get("correction_public_id")))
    if revision is None:
        raise ConversionEvidenceLineageError(
            f"Human correction evidence for {field_name!r} references no "
            "durable revision on this proposal's supersession path"
        )
    applied = _durable_json_object(
        revision["applied_field_updates_json"], "revision applied field updates"
    )
    parent_public_id = public_ids.get(int(revision["superseded_parser_output_id"]))
    if field_name not in applied or item.get("superseded_proposal_public_id") != parent_public_id:
        raise ConversionEvidenceLineageError(
            f"Human correction evidence for {field_name!r} contradicts the "
            "durable revision evidence"
        )
    if not _field_values_equal(field_name, item.get("proposed_value"), applied[field_name]):
        raise ConversionEvidenceLineageError(
            f"Human correction evidence for {field_name!r} does not match the "
            "durably applied correction value"
        )


def _verify_inherited_completion_evidence(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    field_name: str,
    path: Sequence[int],
    public_ids: dict[int, str],
) -> None:
    cursor = conn.execute(
        """
        SELECT parser_output_id, version_number, authenticated_actor_id,
               base_content_hash, completed_content_hash, field_updates_json
        FROM parser_proposal_completions
        WHERE completion_public_id = ?
        """,
        (item.get("completion_public_id"),),
    )
    row = cursor.fetchone()
    if row is None:
        raise ConversionEvidenceLineageError(
            f"Inherited completion evidence for {field_name!r} references no "
            "durable completion record"
        )
    completion = _row_dict(cursor, row)
    source_id = int(completion["parser_output_id"])
    if source_id not in set(path):
        raise ConversionEvidenceLineageError(
            f"Inherited completion evidence for {field_name!r} belongs to a "
            "proposal outside this supersession path"
        )
    updates = _durable_json_object(completion["field_updates_json"], "completion field updates")
    if (
        item.get("completion_version") != int(completion["version_number"])
        or item.get("authenticated_actor_id") != str(completion["authenticated_actor_id"])
        or item.get("base_content_hash") != str(completion["base_content_hash"])
        or item.get("completed_content_hash") != str(completion["completed_content_hash"])
        or item.get("source_proposal_public_id") != public_ids.get(source_id)
        or field_name not in updates
        or updates[field_name] != item.get("proposed_value")
    ):
        raise ConversionEvidenceLineageError(
            f"Inherited completion evidence for {field_name!r} contradicts "
            "the durable completion record"
        )


# ---------------------------------------------------------------------------
# Guard 14: required input completeness (Money Contract, participants)
# ---------------------------------------------------------------------------


def _require_complete_inputs(
    conn: sqlite3.Connection,
    command: ReceiptFactsConversionCommand,
    entries: list[tuple[str, int]],
    effective: dict[str, Any],
) -> dict[str, Any]:
    _require_supported_metadata(effective)

    merchant = effective.get("merchant")
    if not isinstance(merchant, str) or not merchant.strip():
        raise IncompleteReceiptInputsError(
            "Effective proposal has no merchant; receipts.merchant is required"
        )
    merchant = merchant.strip()

    currency_value = effective.get("currency")
    if not isinstance(currency_value, str):
        raise IncompleteReceiptInputsError(
            "Effective proposal has no currency; a supported currency is required"
        )
    try:
        currency = normalize_currency(currency_value)
    except MoneyValidationError as exc:
        raise IncompleteReceiptInputsError(
            f"Effective proposal currency failed the Money Contract: {exc}"
        ) from exc

    amount_value = effective.get("amount")
    if amount_value is None:
        raise IncompleteReceiptInputsError(
            "Effective proposal has no amount; receipts.net_paid_amount is required"
        )
    try:
        canonical_amount = canonicalize_proposal_money(amount_value, currency)
    except MoneyValidationError as exc:
        raise IncompleteReceiptInputsError(
            f"Effective proposal amount failed the Money Contract: {exc}"
        ) from exc
    _require_exact_monetary_representation(conn, canonical_amount)

    receipt_date = _validate_receipt_date(effective.get("transaction_date"))

    resolved_entries: list[dict[str, Any]] = []
    payer_participant_id: int | None = None
    for pid, included in entries:
        row = conn.execute(
            "SELECT id FROM participants WHERE public_id = ?",
            (pid,),
        ).fetchone()
        if row is None:
            raise AmbiguousReceiptInputError(
                f"Membership entry references an unknown participant: {pid!r}"
            )
        participant_id = int(row[0])
        if pid == command.payer_participant_public_id:
            payer_participant_id = participant_id
            role = _ROLE_PAYER
        else:
            role = _ROLE_PARTICIPANT if included == 1 else _ROLE_EXCLUDED
        resolved_entries.append(
            {
                "participant_public_id": pid,
                "participant_id": participant_id,
                "is_included": included,
                "role": role,
            }
        )
    if payer_participant_id is None:
        raise AmbiguousReceiptInputError("The payer could not be resolved to a known participant")

    return {
        "merchant": merchant,
        "currency": currency,
        "canonical_amount": canonical_amount,
        "receipt_date": receipt_date,
        "payer_participant_id": payer_participant_id,
        "resolved_entries": resolved_entries,
    }


def _require_supported_metadata(effective: dict[str, Any]) -> None:
    """Guard 14 opening check (approved by Owner): no silent metadata loss.

    ``receipts`` (migration 002) cannot persist ``description`` or
    ``category``.  A fresh conversion with a non-NULL value for either
    field fails closed before any write; exact replay of an existing
    registry row is unaffected because guard 4 returns before guards run.
    """
    unsupported = sorted(
        name for name in ("description", "category") if effective.get(name) is not None
    )
    if unsupported:
        raise UnsupportedReceiptFactsMetadataError(
            f"The effective payload carries {unsupported} but the receipt "
            "facts schema has no authoritative column for them; refusing to "
            "silently drop or remap human-reviewed metadata"
        )


def _require_exact_monetary_representation(conn: sqlite3.Connection, canonical_amount: str) -> None:
    """Conversion-specific exact persistence boundary (migration 035).

    The canonical amount text is persisted byte-exact in
    ``receipts.net_paid_amount_canonical_text`` (the authoritative
    representation).  The legacy NUMERIC ``net_paid_amount`` column must
    remain Decimal-equal for compatibility readers, so before any write
    the guard asks SQLite itself what NUMERIC affinity will store
    (``CAST(? AS NUMERIC)`` applies the same text-to-INTEGER/REAL
    conversion as the column) and requires the stored mirror to decode
    back Decimal-equal under ``decimal_from_numeric_mirror``.  Amounts
    whose mirror is non-finite, scientific-notation, or unequal are
    refused with a typed validation error instead of being silently
    rounded in the compatibility column.
    """
    if not sqlite_numeric_roundtrip_matches(conn, canonical_amount):
        raise IncompleteReceiptInputsError(
            "Effective proposal amount cannot be mirrored losslessly by the "
            "legacy NUMERIC compatibility column: SQLite NUMERIC affinity "
            "does not round-trip Decimal-equal to the authoritative "
            "canonical text; refusing a lossy write"
        )


def _validate_receipt_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) != _DATE_LEN:
        raise IncompleteReceiptInputsError(
            f"Effective proposal transaction_date must be ISO YYYY-MM-DD, got: {value!r}"
        )
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise IncompleteReceiptInputsError(
            f"Effective proposal transaction_date is not a valid calendar date: {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Deterministic identity derivation and hashing (Section 6, frozen)
# ---------------------------------------------------------------------------


def derive_receipt_public_id(command_public_id: str) -> str:
    """Frozen deterministic derivation: ``rcpt_`` + truncated SHA-256.

    The same command always derives the same receipt public ID, making
    replay reproducible.  A collision with an existing ``receipts.public_id``
    surfaces as ``ConversionPersistenceError``; the service never re-derives.
    """
    digest = _sha256_hex(f"receipt-fact:{command_public_id}")
    return f"rcpt_{digest[:32]}"


def _derive_membership_public_id(command_public_id: str, participant_public_id: str) -> str:
    digest = _sha256_hex(f"receipt-participant:{command_public_id}:{participant_public_id}")
    return f"rcpp_{digest[:32]}"


def _command_material_hash(
    command: ReceiptFactsConversionCommand, entries: list[tuple[str, int]]
) -> str:
    """Canonical command material (Section 6): ``reason`` is excluded."""
    material = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "command_public_id": command.command_public_id,
        "proposal_public_id": command.proposal_public_id,
        "expected_content_hash": command.expected_content_hash,
        "payer_participant_public_id": command.payer_participant_public_id,
        "participants": [
            {"participant_public_id": pid, "is_included": included} for pid, included in entries
        ],
        "authenticated_actor_id": command.authenticated_actor_id,
        "actor_type": command.actor_type,
        "channel": command.channel,
    }
    return _sha256_hex(_canonical_json(material))


def _conversion_result_hash(
    *,
    receipt_public_id: str,
    facts: dict[str, Any],
    payer_public_id: str,
    entries: list[tuple[str, int]],
    attachment_content_hash: str,
    proposal_content_hash: str,
    command_material_hash: str,
) -> str:
    material = {
        "receipt_public_id": receipt_public_id,
        "merchant": facts["merchant"],
        "receipt_date": facts["receipt_date"],
        "net_paid_amount": facts["canonical_amount"],
        "currency": facts["currency"],
        "payer_participant_public_id": payer_public_id,
        "participants": [
            {"participant_public_id": pid, "is_included": included} for pid, included in entries
        ],
        "attachment_content_hash": attachment_content_hash,
        "proposal_content_hash": proposal_content_hash,
        "command_material_hash": command_material_hash,
    }
    return _sha256_hex(_canonical_json(material))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _insert_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_public_id: str,
    facts: dict[str, Any],
    proposal: dict[str, Any],
    raw_intake: dict[str, Any],
    attachment: dict[str, Any],
) -> int:
    # transaction_id and all component amounts stay NULL (facts-only v1);
    # status='confirmed' is written explicitly per approved Decision D4.
    # net_paid_amount_canonical_text (migration 035) carries the exact
    # canonical text authoritatively; the legacy NUMERIC column stays
    # Decimal-equal, enforced by _require_exact_monetary_representation
    # (SQLite CAST-to-NUMERIC round-trip guard) before any write.
    try:
        cursor = conn.execute(
            """
            INSERT INTO receipts (
                public_id, merchant, receipt_datetime, net_paid_amount,
                net_paid_amount_canonical_text,
                currency, payer_participant_id, source_channel, raw_input,
                attachment_id, attachment_path, ocr_confidence,
                parser_output_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed')
            """,
            (
                receipt_public_id,
                facts["merchant"],
                facts["receipt_date"],
                facts["canonical_amount"],
                facts["canonical_amount"],
                facts["currency"],
                facts["payer_participant_id"],
                raw_intake["source_channel"],
                raw_intake["raw_input"],
                proposal["attachment_id"],
                attachment["file_path"],
                proposal["confidence_score"],
                proposal["id"],
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConversionPersistenceError(
            "The receipt fact row violated a schema constraint (identity "
            "collision or uniqueness backstop); no silent re-derivation"
        ) from exc
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise ConversionPersistenceError("Receipt fact insert returned no identity")
    return int(lastrowid)


def _insert_participants(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    command_public_id: str,
    payer_public_id: str,
    resolved_entries: list[dict[str, Any]],
) -> None:
    try:
        conn.executemany(
            """
            INSERT INTO receipt_participants (
                public_id, receipt_id, participant_id, role, is_included
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    _derive_membership_public_id(command_public_id, entry["participant_public_id"]),
                    receipt_id,
                    entry["participant_id"],
                    entry["role"],
                    entry["is_included"],
                )
                for entry in resolved_entries
            ],
        )
    except sqlite3.IntegrityError as exc:
        raise ConversionPersistenceError(
            "Receipt participant membership rows violated a schema constraint"
        ) from exc


def _insert_registry_row(
    conn: sqlite3.Connection,
    *,
    command: ReceiptFactsConversionCommand,
    parser_output_id: int,
    supersession_root_parser_output_id: int,
    receipt_id: int,
    confirmation_public_id: str,
    proposal_content_hash: str,
    command_material_hash: str,
    conversion_result_hash: str,
    created_at: str,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO receipt_proposal_conversions (
                command_public_id, parser_output_id,
                supersession_root_parser_output_id, receipt_id,
                confirmation_public_id, proposal_content_hash,
                command_material_hash, conversion_result_hash,
                actor_type, authenticated_actor_id, conversion_channel,
                reason, schema_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', ?, ?, ?, ?, ?)
            """,
            (
                command.command_public_id,
                parser_output_id,
                supersession_root_parser_output_id,
                receipt_id,
                confirmation_public_id,
                proposal_content_hash,
                command_material_hash,
                conversion_result_hash,
                command.authenticated_actor_id,
                command.channel,
                command.reason,
                CONVERSION_SCHEMA_VERSION,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConversionPersistenceError(
            "The conversion registry row violated a schema constraint"
        ) from exc


def _append_conversion_audit(
    conn: sqlite3.Connection,
    *,
    command: ReceiptFactsConversionCommand,
    proposal: dict[str, Any],
    extraction: dict[str, Any],
    receipt_public_id: str,
    confirmation_public_id: str,
    proposal_content_hash: str,
    command_material_hash: str,
    conversion_result_hash: str,
    created_at: str,
) -> FinancialAuditEvent:
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        causation_public_id=command.command_public_id,
    )
    references = [f"parser-output:{proposal['public_id']}"]
    if proposal.get("source_public_id"):
        references.append(f"source:{proposal['source_public_id']}")
    if proposal.get("attachment_id") is not None:
        references.append(f"attachment-id:{proposal['attachment_id']}")
    references.append(f"extraction:{extraction['public_id']}")
    references.append(f"confirmation:{confirmation_public_id}")
    event, idempotent = append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="receipt",
            aggregate_public_id=receipt_public_id,
            event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
            event_payload={
                "command_public_id": command.command_public_id,
                "proposal_public_id": proposal["public_id"],
                "confirmation_public_id": confirmation_public_id,
                "proposal_content_hash": proposal_content_hash,
                "command_material_hash": command_material_hash,
                "conversion_result_hash": conversion_result_hash,
            },
            previous_state={
                "conversion_status": "not_converted",
                "parse_status": CONFIRMED,
                "proposal_content_hash": proposal_content_hash,
            },
            new_state={
                "conversion_status": "converted_to_receipt_facts",
                "receipt_public_id": receipt_public_id,
                "proposal_content_hash": proposal_content_hash,
                "conversion_result_hash": conversion_result_hash,
            },
            actor_type="human",
            actor_public_id=command.authenticated_actor_id,
            authorization_public_id=confirmation_public_id,
            source_evidence_references=tuple(references),
            correlation_public_id=receipt_public_id,
            causation_public_id=command.command_public_id,
            created_at=created_at,
            # Round 3 fix F6: a fresh conversion event must be the genesis
            # of its receipt aggregate.  A pre-existing, state-compatible
            # head would otherwise let this event chain onto an audit trail
            # this transaction never wrote.
            expected_previous_event_hash=ZERO_AUDIT_HASH,
        ),
    )
    if idempotent:
        # A fresh conversion appends a brand-new audit event; finding the
        # event already recorded means another writer claimed this identity
        # (guard 4 would have replayed a completed conversion instead), so
        # the whole transaction is rolled back rather than adopted.
        raise ConversionPersistenceError(
            "A fresh conversion found its audit event already recorded; "
            "refusing to adopt an audit trail this transaction did not write"
        )
    return event


# ---------------------------------------------------------------------------
# Pre-commit verification and revalidation (Section 5.5)
# ---------------------------------------------------------------------------


def _verify_persisted(
    conn: sqlite3.Connection,
    *,
    command: ReceiptFactsConversionCommand,
    entries: list[tuple[str, int]],
    proposal: dict[str, Any],
    link: dict[str, Any],
    raw_intake: dict[str, Any],
    authorization: dict[str, Any],
    extraction: dict[str, Any],
    attachment: dict[str, Any],
    facts: dict[str, Any],
    receipt_id: int,
    receipt_public_id: str,
    effective_hash: str,
    material_hash: str,
    result_hash: str,
    audit_event: FinancialAuditEvent,
    now: str,
    chain_ids: Sequence[int],
    chain_root_id: int,
) -> None:
    """Complete pre-commit revalidation (Section 5.5).

    Nothing established before the writes is trusted from cache: the
    proposal, OCR link, raw intake, authorization, supersession chain,
    evidence lineage, source binding, ambiguity provenance, and
    completeness guards are re-run against the state visible inside this
    transaction, then compared with the cached snapshots, and the persisted
    rows are compared field-by-field with the command's facts.  Every
    failure surfaces as ``ConversionPersistenceError`` and rolls the whole
    conversion back.
    """
    cursor = conn.execute(
        """
        SELECT public_id, transaction_id, merchant, receipt_datetime,
               gross_amount, subtotal_amount, service_charge_amount,
               tax_amount, discount_amount, net_paid_amount,
               net_paid_amount_canonical_text, currency,
               payer_participant_id, source_channel, raw_input,
               attachment_id, attachment_path, payment_record_attachment_id,
               ocr_confidence, parser_output_id, status, notes
        FROM receipts WHERE id = ?
        """,
        (receipt_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ConversionPersistenceError("Persisted receipt fact row is missing")
    receipt = _row_dict(cursor, row)
    # Facts-only v1 never writes these columns: any non-NULL value means
    # the persisted row is not the row this conversion described.
    if any(receipt[name] is not None for name in NEVER_WRITTEN_RECEIPT_COLUMNS):
        raise ConversionPersistenceError(
            "Persisted receipt fact row carries values in columns the "
            "facts-only conversion never writes"
        )
    # The same mirror-decoding rule as the pre-write guard: the persisted
    # NUMERIC value must decode back Decimal-equal to the canonical text.
    persisted_amount = decimal_from_numeric_mirror(receipt["net_paid_amount"])
    expected_amount = _try_money_decimal(facts["canonical_amount"])
    if (
        receipt["public_id"] != receipt_public_id
        or receipt["merchant"] != facts["merchant"]
        or receipt["receipt_datetime"] != facts["receipt_date"]
        or receipt["net_paid_amount_canonical_text"] != facts["canonical_amount"]
        or persisted_amount is None
        or expected_amount is None
        or persisted_amount != expected_amount
        or receipt["currency"] != facts["currency"]
        or int(receipt["payer_participant_id"]) != int(facts["payer_participant_id"])
        or receipt["source_channel"] != raw_intake["source_channel"]
        or receipt["raw_input"] != raw_intake["raw_input"]
        or receipt["attachment_id"] != proposal["attachment_id"]
        or receipt["attachment_path"] != attachment["file_path"]
        or receipt["ocr_confidence"] != proposal["confidence_score"]
        or int(receipt["parser_output_id"]) != int(proposal["id"])
        or receipt["status"] != "confirmed"
    ):
        raise ConversionPersistenceError(
            "Persisted receipt fact row does not match the conversion command"
        )

    member_rows = conn.execute(
        "SELECT public_id, participant_id, role, is_included "
        "FROM receipt_participants WHERE receipt_id = ? ORDER BY participant_id",
        (receipt_id,),
    ).fetchall()
    expected_members = sorted(
        (
            _derive_membership_public_id(
                command.command_public_id, str(e["participant_public_id"])
            ),
            int(e["participant_id"]),
            str(e["role"]),
            int(e["is_included"]),
        )
        for e in facts["resolved_entries"]
    )
    persisted_members = sorted((str(r[0]), int(r[1]), str(r[2]), int(r[3])) for r in member_rows)
    if persisted_members != expected_members:
        raise ConversionPersistenceError(
            "Persisted receipt membership rows do not match the conversion command"
        )

    reg_cursor = conn.execute(
        """
        SELECT command_public_id, parser_output_id,
               supersession_root_parser_output_id, receipt_id,
               confirmation_public_id, proposal_content_hash,
               command_material_hash, conversion_result_hash, actor_type,
               authenticated_actor_id, conversion_channel, reason,
               schema_version, created_at
        FROM receipt_proposal_conversions WHERE command_public_id = ?
        """,
        (command.command_public_id,),
    )
    reg_row = reg_cursor.fetchone()
    if reg_row is None:
        raise ConversionPersistenceError("Persisted conversion registry row is missing")
    registry = _row_dict(reg_cursor, reg_row)
    if (
        registry["command_public_id"] != command.command_public_id
        or int(registry["parser_output_id"]) != int(proposal["id"])
        or int(registry["supersession_root_parser_output_id"]) != int(chain_root_id)
        or int(registry["receipt_id"]) != receipt_id
        or registry["confirmation_public_id"] != str(authorization["confirmation_public_id"])
        or registry["proposal_content_hash"] != effective_hash
        or registry["command_material_hash"] != material_hash
        or registry["conversion_result_hash"] != result_hash
        or registry["actor_type"] != "human"
        or registry["authenticated_actor_id"] != command.authenticated_actor_id
        or registry["conversion_channel"] != command.channel
        or registry["reason"] != command.reason
        or registry["schema_version"] != CONVERSION_SCHEMA_VERSION
        or registry["created_at"] != now
    ):
        raise ConversionPersistenceError(
            "Persisted conversion registry row does not match the command"
        )

    _verify_persisted_audit_event(
        conn,
        command=command,
        proposal=proposal,
        authorization=authorization,
        receipt_public_id=receipt_public_id,
        effective_hash=effective_hash,
        material_hash=material_hash,
        result_hash=result_hash,
        audit_event=audit_event,
        now=now,
    )

    try:
        _revalidate_before_commit(
            conn,
            command=command,
            entries=entries,
            proposal=proposal,
            link=link,
            raw_intake=raw_intake,
            authorization=authorization,
            extraction=extraction,
            attachment=attachment,
            facts=facts,
            effective_hash=effective_hash,
            chain_ids=chain_ids,
            chain_root_id=chain_root_id,
        )
    except ConversionPersistenceError:
        raise
    except ReceiptFactsConversionError as exc:
        raise ConversionPersistenceError(
            "Pre-commit revalidation failed inside the conversion transaction"
        ) from exc


def _verify_persisted_audit_event(
    conn: sqlite3.Connection,
    *,
    command: ReceiptFactsConversionCommand,
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    receipt_public_id: str,
    effective_hash: str,
    material_hash: str,
    result_hash: str,
    audit_event: FinancialAuditEvent,
    now: str,
) -> None:
    """Fetch and verify the exact audit event this conversion appended.

    The event returned by the fresh (non-idempotent) append is compared
    field-by-field with the persisted row, its payload envelope, its
    derived identity, its authorization/actor binding, and the whole
    receipt aggregate chain, all before commit.  Any mismatch rolls the
    conversion back as ``ConversionPersistenceError``.
    """
    # The audit repository decodes rows by column name; restore the
    # caller's row factory afterwards, whatever it was.
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        fetched = FinancialAuditRepository(conn).fetch(audit_event.event_public_id)
        chain_check = verify_financial_audit_chain(
            conn, aggregate_type="receipt", aggregate_public_id=receipt_public_id
        )
    finally:
        conn.row_factory = previous_factory
    if fetched is None or fetched != audit_event:
        raise ConversionPersistenceError(
            "The persisted financial audit event does not match the event this conversion appended"
        )
    # Round 6 fix: the registry row (verified against ``now`` by the
    # caller) and the audit event must carry the same canonical UTC
    # timestamp byte-exactly, or the committed conversion could never
    # satisfy the replay audit binding again.  The audit repository's own
    # normalization is not trusted to have been a no-op.
    if fetched.created_at != now or audit_event.created_at != now:
        raise ConversionPersistenceError(
            "The persisted audit event timestamp does not match the conversion timestamp"
        )
    try:
        payload_value = json.loads(fetched.event_payload_json)["value"]
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ConversionPersistenceError(
            "The persisted audit event payload is not the canonical envelope"
        ) from exc
    expected_event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        causation_public_id=command.command_public_id,
    )
    confirmation_public_id = str(authorization["confirmation_public_id"])
    if (
        fetched.event_public_id != expected_event_id
        or fetched.aggregate_type != "receipt"
        or fetched.aggregate_public_id != receipt_public_id
        or fetched.event_type != RECEIPT_FACTS_CONVERSION_EVENT_TYPE
        or fetched.actor_type != "human"
        or fetched.actor_public_id != command.authenticated_actor_id
        or fetched.authorization_public_id != confirmation_public_id
        or fetched.causation_public_id != command.command_public_id
        or fetched.correlation_public_id != receipt_public_id
        or payload_value
        != {
            "command_public_id": command.command_public_id,
            "proposal_public_id": str(proposal["public_id"]),
            "confirmation_public_id": confirmation_public_id,
            "proposal_content_hash": effective_hash,
            "command_material_hash": material_hash,
            "conversion_result_hash": result_hash,
        }
    ):
        raise ConversionPersistenceError(
            "The persisted audit event identity or payload does not match this conversion"
        )
    if not chain_check.valid or chain_check.legacy_without_chain:
        raise ConversionPersistenceError(
            "The receipt audit chain is not valid after the conversion append"
        )
    # Round 3 fix F6: the fresh conversion event must be the genesis of its
    # receipt aggregate, and the aggregate chain must contain exactly this
    # one event before commit.
    if (
        fetched.sequence_number != 1
        or fetched.previous_event_hash != ZERO_AUDIT_HASH
        or chain_check.event_count != 1
    ):
        raise ConversionPersistenceError(
            "The conversion audit event is not the genesis event of its receipt aggregate"
        )


def _revalidate_before_commit(
    conn: sqlite3.Connection,
    *,
    command: ReceiptFactsConversionCommand,
    entries: list[tuple[str, int]],
    proposal: dict[str, Any],
    link: dict[str, Any],
    raw_intake: dict[str, Any],
    authorization: dict[str, Any],
    extraction: dict[str, Any],
    attachment: dict[str, Any],
    facts: dict[str, Any],
    effective_hash: str,
    chain_ids: Sequence[int],
    chain_root_id: int,
) -> None:
    """Re-run guards 5-14 against in-transaction state; nothing is cached."""
    fresh_proposal = _require_proposal(conn, command.proposal_public_id)
    if fresh_proposal != proposal:
        raise ConversionPersistenceError(
            "The proposal row changed inside the conversion transaction"
        )
    parser_output_id = int(fresh_proposal["id"])
    fresh_link = _require_single_receipt_link(conn, parser_output_id)
    if fresh_link != link:
        raise ConversionPersistenceError(
            "The OCR proposal link changed inside the conversion transaction"
        )
    _require_leaf(conn, fresh_proposal)
    fresh_raw_intake = _require_single_raw_intake(conn, parser_output_id)
    if fresh_raw_intake != raw_intake:
        raise ConversionPersistenceError(
            "The raw intake record changed inside the conversion transaction"
        )
    _require_confirmed_status(fresh_proposal)
    fresh_hash, fresh_authorization = _require_bound_confirmation(
        conn, fresh_proposal, command.expected_content_hash
    )
    if fresh_hash != effective_hash:
        raise ConversionPersistenceError(
            "Effective content hash changed inside the conversion transaction"
        )
    if fresh_authorization != authorization:
        raise ConversionPersistenceError(
            "The confirmation authorization changed inside the conversion transaction"
        )

    fresh_chain_ids, fresh_root = _walk_supersession_chain(conn, parser_output_id)
    if fresh_root != chain_root_id or sorted(fresh_chain_ids) != sorted(chain_ids):
        raise ConversionPersistenceError(
            "The supersession chain topology changed inside the conversion transaction"
        )
    for member_id in fresh_chain_ids:
        if has_legacy_transaction_conversion(conn, member_id):
            raise ConversionPersistenceError(
                "A legacy conversion appeared on the chain at pre-commit revalidation"
            )
    placeholders = ",".join("?" for _ in fresh_chain_ids)
    registry_commands = conn.execute(
        "SELECT command_public_id FROM receipt_proposal_conversions "
        f"WHERE parser_output_id IN ({placeholders}) "
        f"OR supersession_root_parser_output_id IN ({placeholders})",
        (*fresh_chain_ids, *fresh_chain_ids),
    ).fetchall()
    if [str(r[0]) for r in registry_commands] != [command.command_public_id]:
        raise ConversionPersistenceError(
            "The conversion registry chain state is not exactly this command's row"
        )

    fresh_effective = _resolve_effective(conn, fresh_proposal)
    fresh_extraction, fresh_attachment = _require_evidence_lineage(
        conn, fresh_proposal, fresh_link, fresh_raw_intake, fresh_effective
    )
    if fresh_extraction != extraction or fresh_attachment != attachment:
        raise ConversionPersistenceError(
            "The OCR evidence lineage changed inside the conversion transaction"
        )
    _require_source_binding(
        conn, fresh_proposal, fresh_raw_intake, fresh_extraction, fresh_attachment
    )
    _require_flags_resolved(conn, fresh_proposal, fresh_effective)
    fresh_facts = _require_complete_inputs(conn, command, entries, fresh_effective)
    fresh_amount = _try_money_decimal(fresh_facts["canonical_amount"])
    cached_amount = _try_money_decimal(facts["canonical_amount"])
    if (
        fresh_facts["merchant"] != facts["merchant"]
        or fresh_facts["currency"] != facts["currency"]
        or fresh_facts["receipt_date"] != facts["receipt_date"]
        or fresh_amount is None
        or cached_amount is None
        or fresh_amount != cached_amount
        or int(fresh_facts["payer_participant_id"]) != int(facts["payer_participant_id"])
        or fresh_facts["resolved_entries"] != facts["resolved_entries"]
    ):
        raise ConversionPersistenceError(
            "The receipt facts changed inside the conversion transaction"
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_effective(conn: sqlite3.Connection, proposal: dict[str, Any]) -> dict[str, Any]:
    try:
        effective, _cid, _version = resolve_effective_payload(conn, proposal)
    except EffectivePayloadError as exc:
        raise ConversionEvidenceLineageError(str(exc)) from exc
    return effective


def _try_money_decimal(value: Any) -> Decimal | None:
    try:
        return money_decimal(value)
    except MoneyValidationError:
        return None


def _try_normalize_currency(currency: Any) -> str | None:
    if not isinstance(currency, str):
        return None
    try:
        return normalize_currency(currency)
    except MoneyValidationError:
        return None


def _field_values_equal(field_name: str, claimed: Any, durable: Any) -> bool:
    """Field-aware equality for binding claimed values to durable values.

    Amounts compare as Money Contract decimals and currencies compare
    normalized; every other field compares strictly.  Unparseable claimed
    values never compare equal (fail closed).
    """
    if field_name == "amount":
        claimed_decimal = _try_money_decimal(claimed)
        return claimed_decimal is not None and claimed_decimal == _try_money_decimal(durable)
    if field_name == "currency":
        claimed_currency = _try_normalize_currency(claimed)
        return claimed_currency is not None and claimed_currency == _try_normalize_currency(durable)
    return bool(claimed == durable)


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((col[0] for col in cursor.description), row, strict=True))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rollback_if_needed(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def _now(clock: Callable[[], str] | None) -> str:
    """Return the conversion's canonical UTC timestamp before any write.

    The registry row and the audit event created by one conversion share
    this single timestamp, and both the fresh-write verification and the
    replay audit binding compare them byte-exactly.  The timestamp is
    therefore always emitted in the audit chain's canonical UTC form
    (microseconds, 'Z' suffix): injected clock output is normalized to
    that form - legal non-UTC offsets are converted to UTC - and invalid,
    non-string, or timezone-naive values fail closed as
    ``ConversionPersistenceError`` before anything is written.
    """
    if clock is None:
        parsed = datetime.now(timezone.utc)
    else:
        value = clock()
        if not isinstance(value, str):
            raise ConversionPersistenceError(
                "Conversion clock must return an ISO 8601 timestamp string"
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConversionPersistenceError(
                "Conversion clock returned a non-ISO-8601 timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ConversionPersistenceError("Conversion clock returned a timezone-naive timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "AmbiguousReceiptInputError",
    "ConversionCallerOwnedTransactionError",
    "ConversionEvidenceLineageError",
    "ConversionForeignKeysDisabledError",
    "ConversionIdempotencyConflictError",
    "ConversionPersistenceError",
    "ConversionProposalNotFoundError",
    "ConversionStagingDatabaseRejectedError",
    "IncompleteReceiptInputsError",
    "InvalidConversionCommandError",
    "NEVER_WRITTEN_RECEIPT_COLUMNS",
    "ProposalNotConfirmedError",
    "ReceiptFactsAlreadyConvertedError",
    "ReceiptFactsConversionCommand",
    "ReceiptFactsConversionError",
    "ReceiptFactsConversionResult",
    "StaleConfirmationHashError",
    "StaleConversionTargetError",
    "UnauthorizedConversionActorError",
    "UnsupportedConversionProposalTypeError",
    "UnsupportedReceiptFactsMetadataError",
    "convert_confirmed_receipt_proposal_to_facts",
    "decimal_from_numeric_mirror",
    "derive_receipt_public_id",
]
