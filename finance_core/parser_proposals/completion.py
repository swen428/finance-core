"""Authenticated human parser-proposal completion boundary.

Provides the sole public entry point for supplying missing non-monetary
conversion fields after parsing and before confirmation.  The original
parser payload remains immutable source evidence; completion creates an
append-only version record keyed to the exact current proposal content.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Callable

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.parser_proposals.content_hash import (
    compute_effective_proposal_content_hash,
    compute_proposal_content_hash,
)
from finance_core.parser_proposals.conversion_state import (
    has_legacy_transaction_conversion,
    has_receipt_registry_conversion,
)
from finance_core.parser_proposals.effective_payload import (
    resolve_effective_payload,
)
from finance_core.parser_proposals.lifecycle import (
    EDITED_PENDING_CONFIRMATION,
    PARSED_PENDING_CONFIRMATION,
    TERMINAL_STATUSES,
    raw_intake_status_for_proposal_status,
    validate_transition,
)
from finance_core.parser_proposals.repository import (
    ParserProposalRepository,
)
from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ProposalCompletionError(ValueError):
    """Base error for parser proposal completion failures."""


class UnauthorizedCompletionActorError(ProposalCompletionError):
    """The supplied command did not identify an authenticated human actor."""


class InvalidCompletionStatusError(ProposalCompletionError):
    """A proposal is not in a status eligible for completion."""


class StaleProposalContentError(ProposalCompletionError):
    """The expected content hash no longer matches the current effective proposal."""


class NoMaterialChangeError(ProposalCompletionError):
    """The field updates produce no material change to the proposal content."""


class UnknownCompletionFieldError(ProposalCompletionError):
    """A requested field cannot be completed through this boundary."""


class InvalidCompletionFieldValueError(ProposalCompletionError):
    """A supplied field value failed canonical validation."""


class CompletionConflictError(ProposalCompletionError):
    """A completion with the same identity conflicts with the persisted record."""


class InvalidCompletionIdError(ProposalCompletionError):
    """The completion_public_id is missing, malformed, or invalid."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_ACTOR_TYPES = frozenset({"human", "user"})
_PERSISTED_ACTOR_TYPE = "human"

_COMPLETABLE_FIELDS = frozenset(
    {
        "transaction_date",
        "merchant",
        "description",
        "category",
    }
)

_PROHIBITED_FIELDS = frozenset(
    {
        "amount",
        "currency",
        "intent",
        "transaction_type",
        "payer",
        "paid_by",
        "account",
        "account_id",
    }
)

_DATE_LEN = 10

_COMPLETION_ID_MAX_LEN = 200


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def complete_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    actor: str,
    expected_content_hash: str,
    field_updates: dict[str, Any],
    completion_public_id: str,
    actor_type: str = "human",
    completion_channel: str = "cli",
    reason: str | None = None,
    clock: Callable[[], str] | None = None,
    transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    """Complete a parser proposal with authenticated non-monetary field updates.

    The original parser payload is never overwritten.  This boundary creates
    an append-only completion version record and transitions the proposal to
    ``edited_pending_confirmation``.  A fresh confirmation is required after
    completion.
    """
    _validate_command(
        actor,
        actor_type,
        expected_content_hash,
        field_updates,
        completion_channel,
        completion_public_id,
    )
    require_staging_database(conn)

    public_id = completion_public_id

    _begin_immediate(conn)
    try:
        proposals = ParserProposalRepository(conn)
        proposal = _require_proposal(proposals, parser_output_id)

        current_hash = _effective_proposal_content_hash(conn, proposal)

        # Resolve cumulative effective payload for deterministic versioning.
        effective_payload, _current_cid, _current_version = _resolve_completed_payload(
            conn, proposal
        )

        existing = _get_completion_by_public_id(conn, public_id)
        if existing is not None:
            result = _handle_existing_completion(
                conn,
                existing,
                proposal,
                expected_content_hash,
                field_updates,
                actor,
                public_id,
                completion_channel,
            )
            conn.commit()
            return result

        if transaction_guard is not None:
            transaction_guard(conn)

        if expected_content_hash != current_hash:
            raise StaleProposalContentError(
                f"Expected hash {expected_content_hash[:16]}... "
                f"!= current effective hash {current_hash[:16]}..."
            )

        status = str(proposal["parse_status"])
        if status in TERMINAL_STATUSES:
            raise InvalidCompletionStatusError(
                f"Proposal is in terminal status '{status}' and cannot be completed"
            )
        _check_no_conversion(conn, parser_output_id)

        canonical_updates = _validate_and_canonicalize_field_updates(field_updates)
        completed_payload = {**effective_payload, **canonical_updates}

        completed_hash = _compute_expected_completed_hash(conn, proposal, completed_payload)
        if completed_hash == current_hash:
            raise NoMaterialChangeError(
                "Field updates produce no material change to the proposal content"
            )

        next_version = _next_completion_version(conn, parser_output_id)
        from_status = status
        to_status = EDITED_PENDING_CONFIRMATION if status == PARSED_PENDING_CONFIRMATION else status
        if from_status != to_status:
            validate_transition(from_status, to_status)

        now = _now(clock)

        _insert_completion(
            conn,
            completion_public_id=public_id,
            parser_output_id=parser_output_id,
            version_number=next_version,
            base_content_hash=current_hash,
            completed_content_hash=completed_hash,
            completed_payload=completed_payload,
            field_updates=canonical_updates,
            actor_id=actor,
            completion_channel=completion_channel,
            reason=reason,
            created_at=now,
        )

        # Always record a lifecycle event for every completion version.
        event_id = None
        if _completion_should_insert_lifecycle_event(conn, parser_output_id, next_version):
            event_id = _insert_completion_event(
                conn,
                parser_output_id,
                from_status,
                to_status,
                actor,
                public_id,
                next_version,
                current_hash,
                completed_hash,
                canonical_updates,
            )
        if from_status != to_status:
            proposals.update_status(parser_output_id, to_status)
            proposals.update_raw_intake_status(
                parser_output_id, raw_intake_status_for_proposal_status(to_status)
            )

        _append_completion_audit(
            conn,
            proposal=proposal,
            to_status=to_status,
            completion_public_id=public_id,
            version_number=next_version,
            current_hash=current_hash,
            completed_hash=completed_hash,
            actor_id=actor,
            changed_fields=sorted(canonical_updates.keys()),
            created_at=now,
        )

        conn.commit()
        return {
            "parser_output_id": parser_output_id,
            "completion_public_id": public_id,
            "version_number": next_version,
            "from_status": from_status,
            "to_status": to_status,
            "base_content_hash": current_hash,
            "completed_content_hash": completed_hash,
            "changed_fields": sorted(canonical_updates.keys()),
            "actor_type": _PERSISTED_ACTOR_TYPE,
            "event_id": event_id,
            "idempotent": False,
        }
    except Exception:
        _rollback_if_needed(conn)
        raise


# ---------------------------------------------------------------------------
# Effective proposal payload resolution
# ---------------------------------------------------------------------------


def resolve_effective_proposal_payload(
    conn: sqlite3.Connection,
    parser_output_id: int,
) -> tuple[dict[str, Any], str | None, int]:
    """Return (effective_payload, completion_public_id_or_None, version_number).

    This is the **single authoritative effective-payload resolver** for all
    service-level operations: completion building, public effective-payload
    lookup, effective content hashing, confirmation, conversion, and audit
    evidence.
    """
    proposals = ParserProposalRepository(conn)
    proposal = proposals.get(parser_output_id)
    if proposal is None:
        raise ProposalCompletionError(f"parser output not found: {parser_output_id}")
    return _resolve_completed_payload(conn, proposal)


def _resolve_completed_payload(
    conn: sqlite3.Connection, proposal: dict[str, Any]
) -> tuple[dict[str, Any], str | None, int]:
    """Return (effective_payload, completion_public_id, version_number).

    Delegates to the single authoritative effective-payload resolver.
    """
    from finance_core.parser_proposals.effective_payload import EffectivePayloadError

    try:
        return resolve_effective_payload(conn, proposal)
    except EffectivePayloadError as exc:
        raise ProposalCompletionError(str(exc)) from exc


def _effective_proposal_content_hash(conn: sqlite3.Connection, proposal: dict[str, Any]) -> str:
    """Compute the effective content hash, resolving any completion record."""
    return compute_effective_proposal_content_hash(conn, proposal)


# ---------------------------------------------------------------------------
# Command validation
# ---------------------------------------------------------------------------


def _validate_command(
    actor: str,
    actor_type: str,
    expected_content_hash: str,
    field_updates: dict[str, Any],
    completion_channel: str,
    completion_public_id: str,
) -> None:
    if actor_type not in _ALLOWED_ACTOR_TYPES:
        raise UnauthorizedCompletionActorError(
            f"Only authenticated human actors may complete proposals, got: {actor_type}"
        )
    if not isinstance(actor, str):
        raise UnauthorizedCompletionActorError("Authenticated actor must be a string")
    if not actor:
        raise UnauthorizedCompletionActorError("Authenticated actor must not be empty")
    if actor != actor.strip():
        raise UnauthorizedCompletionActorError(
            "Authenticated actor must not have leading or trailing whitespace"
        )
    if not isinstance(expected_content_hash, str) or len(expected_content_hash) != 64:
        raise ProposalCompletionError("expected_content_hash must be a 64-character hex string")
    if not all(c in "0123456789abcdef" for c in expected_content_hash):
        raise ProposalCompletionError("expected_content_hash must be lowercase hex (0-9, a-f)")
    if not isinstance(field_updates, dict) or not field_updates:
        raise ProposalCompletionError("field_updates must be a non-empty dict")
    _validate_completion_public_id(completion_public_id)
    if not isinstance(completion_channel, str):
        raise ProposalCompletionError("completion_channel must be a string")
    if not completion_channel:
        raise ProposalCompletionError("completion_channel must not be empty")
    if completion_channel != completion_channel.strip():
        raise ProposalCompletionError(
            "completion_channel must not have leading or trailing whitespace"
        )


def _require_proposal(
    repository: ParserProposalRepository, parser_output_id: int
) -> dict[str, Any]:
    proposal = repository.get(parser_output_id)
    if proposal is None:
        raise ProposalCompletionError(f"parser output not found: {parser_output_id}")
    return proposal


def _check_no_conversion(conn: sqlite3.Connection, parser_output_id: int) -> None:
    if has_legacy_transaction_conversion(conn, parser_output_id):
        raise InvalidCompletionStatusError(
            f"Proposal {parser_output_id} has already been converted"
        )
    # B4.0 mutual exclusion: a proposal recorded in the future receipt
    # conversion registry has produced canonical receipt facts and is no
    # longer completable.
    if has_receipt_registry_conversion(conn, parser_output_id):
        raise InvalidCompletionStatusError(
            f"Proposal {parser_output_id} is already recorded in the receipt conversion registry"
        )


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def _validate_and_canonicalize_field_updates(
    field_updates: dict[str, Any],
) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for key in field_updates:
        if not isinstance(key, str) or not key:
            raise UnknownCompletionFieldError(f"Invalid field key: {repr(key)}")
        if key not in _COMPLETABLE_FIELDS:
            if key in _PROHIBITED_FIELDS:
                raise UnknownCompletionFieldError(
                    f"Field '{key}' cannot be changed through proposal completion"
                )
            raise UnknownCompletionFieldError(f"Unknown completion field: {key}")

    if "transaction_date" in field_updates:
        canonical["transaction_date"] = _validate_transaction_date(
            field_updates["transaction_date"]
        )
    if "merchant" in field_updates:
        canonical["merchant"] = _validate_trimmed_text(field_updates["merchant"], "merchant")
    if "description" in field_updates:
        canonical["description"] = _validate_trimmed_text(
            field_updates["description"], "description"
        )
    if "category" in field_updates:
        canonical["category"] = _validate_trimmed_text(field_updates["category"], "category")

    return canonical


def _validate_transaction_date(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidCompletionFieldValueError(
            f"transaction_date must be a string, got {type(value).__name__}"
        )
    if len(value) != _DATE_LEN:
        raise InvalidCompletionFieldValueError(
            f"transaction_date must be ISO 8601 YYYY-MM-DD, got: {repr(value)}"
        )
    if value[4] != "-" or value[7] != "-":
        raise InvalidCompletionFieldValueError(
            f"transaction_date must be ISO 8601 YYYY-MM-DD, got: {repr(value)}"
        )
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise InvalidCompletionFieldValueError(
            f"transaction_date is not a valid calendar date: {repr(value)}"
        )
    return value


def _validate_trimmed_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidCompletionFieldValueError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    trimmed = value.strip()
    if not trimmed:
        raise InvalidCompletionFieldValueError(f"{field_name} must not be empty")
    return trimmed


# ---------------------------------------------------------------------------
# Existing completion handling (idempotency)
# ---------------------------------------------------------------------------


def get_completion_by_public_id(
    conn: sqlite3.Connection, completion_public_id: str
) -> dict[str, Any] | None:
    """Read-only lookup of one persisted completion row by its public identity.

    Exposes the replay/conflict material (version, base/completed content
    hashes, canonical field updates, actor, channel) to callers outside this
    module without granting write access.
    """
    return _get_completion_by_public_id(conn, completion_public_id)


def _get_completion_by_public_id(
    conn: sqlite3.Connection, completion_public_id: str
) -> dict[str, Any] | None:
    cursor = conn.execute(
        """
        SELECT id, completion_public_id, parser_output_id, version_number,
               base_content_hash, completed_content_hash, completed_payload_json,
               field_updates_json, actor_type, authenticated_actor_id,
               completion_channel, reason, created_at
        FROM parser_proposal_completions
        WHERE completion_public_id = ?
        """,
        (completion_public_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((col[0] for col in cursor.description), row, strict=True))


def _compute_expected_completed_hash(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    completed_payload: dict[str, Any],
) -> str:
    """Compute what the content hash would be for a given completed payload.

    Builds an effective proposal dict by replacing ``parsed_payload`` with the
    completed payload and delegates to the authoritative hash contract.
    """
    effective = dict(proposal)
    effective["parsed_payload"] = json.dumps(completed_payload, sort_keys=True)
    return compute_proposal_content_hash(conn, effective)


def _handle_existing_completion(
    conn: sqlite3.Connection,
    existing: dict[str, Any],
    proposal: dict[str, Any],
    expected_content_hash: str,
    field_updates: dict[str, Any],
    actor: str,
    public_id: str,
    completion_channel: str,
) -> dict[str, Any]:
    # Verify replay or reject conflict for an existing completion.
    #
    # Never recompute the completed payload hash — that approach only
    # works for version 1.  Instead, compare incoming canonical command
    # material directly against the persisted completion row.  The
    # persisted row is the authoritative record.
    canonical_updates = _validate_and_canonicalize_field_updates(field_updates)
    persisted_updates = json.loads(existing["field_updates_json"])

    same_proposal = existing["parser_output_id"] == proposal["id"]
    same_base_hash = existing["base_content_hash"] == expected_content_hash
    same_actor = existing["authenticated_actor_id"] == actor
    same_channel = existing["completion_channel"] == completion_channel
    same_updates = canonical_updates == persisted_updates

    # reason is non-authoritative metadata — the first persisted value
    # wins and replay with a different reason is still idempotent.
    # It is intentionally excluded from the conflict comparison below.
    if same_proposal and same_base_hash and same_actor and same_channel and same_updates:
        status = str(proposal["parse_status"])
        return {
            "parser_output_id": proposal["id"],
            "completion_public_id": public_id,
            "version_number": existing["version_number"],
            "from_status": status,
            "to_status": status,
            "base_content_hash": existing["base_content_hash"],
            "completed_content_hash": existing["completed_content_hash"],
            "changed_fields": sorted(canonical_updates.keys()),
            "actor_type": _PERSISTED_ACTOR_TYPE,
            "event_id": None,
            "idempotent": True,
        }

    raise CompletionConflictError(
        f"Completion {public_id} already exists with different material. "
        f"same_proposal={same_proposal}, same_base_hash={same_base_hash}, "
        f"same_actor={same_actor}, "
        f"same_channel={same_channel}, same_updates={same_updates}"
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _next_completion_version(conn: sqlite3.Connection, parser_output_id: int) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM parser_proposal_completions
        WHERE parser_output_id = ?
        """,
        (parser_output_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _completion_should_insert_lifecycle_event(
    conn: sqlite3.Connection,
    parser_output_id: int,
    next_version: int,
) -> bool:
    """Return True if no lifecycle event already exists for this version.

    This prevents duplicate events on idempotent replay while ensuring
    every new version always gets its corresponding lifecycle evidence.
    """
    row = conn.execute(
        """
        SELECT 1 FROM parser_proposal_events
        WHERE parser_output_id = ?
          AND event_type = 'edited'
          AND event_reason = ?
        """,
        (parser_output_id, f"completion v{next_version}"),
    ).fetchone()
    return row is None


def _insert_completion(
    conn: sqlite3.Connection,
    *,
    completion_public_id: str,
    parser_output_id: int,
    version_number: int,
    base_content_hash: str,
    completed_content_hash: str,
    completed_payload: dict[str, Any],
    field_updates: dict[str, Any],
    actor_id: str,
    completion_channel: str,
    reason: str | None,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO parser_proposal_completions (
            completion_public_id, parser_output_id, version_number,
            base_content_hash, completed_content_hash, completed_payload_json,
            field_updates_json, actor_type, authenticated_actor_id,
            completion_channel, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'human', ?, ?, ?, ?)
        """,
        (
            completion_public_id,
            parser_output_id,
            version_number,
            base_content_hash,
            completed_content_hash,
            json.dumps(completed_payload, sort_keys=True),
            json.dumps(field_updates, sort_keys=True),
            actor_id,
            completion_channel,
            reason,
            created_at,
        ),
    )


def _insert_completion_event(
    conn: sqlite3.Connection,
    parser_output_id: int,
    from_status: str,
    to_status: str,
    actor: str,
    completion_public_id: str,
    version_number: int,
    current_hash: str,
    completed_hash: str,
    field_updates: dict[str, Any],
) -> int:
    payload = json.dumps(
        {
            "completion_public_id": completion_public_id,
            "version_number": version_number,
            "previous_content_hash": current_hash,
            "new_content_hash": completed_hash,
            "changed_fields": sorted(field_updates.keys()),
            "actor_type": _PERSISTED_ACTOR_TYPE,
        },
        sort_keys=True,
    )
    cursor = conn.execute(
        """
        INSERT INTO parser_proposal_events (
          parser_output_id, from_status, to_status, event_type, event_reason,
          actor_type, actor_identifier, event_payload
        ) VALUES (?, ?, ?, 'edited', ?, 'user', ?, ?)
        """,
        (
            parser_output_id,
            from_status,
            to_status,
            f"completion v{version_number}",
            actor,
            payload,
        ),
    )
    lastrowid = cursor.lastrowid
    assert lastrowid is not None
    return lastrowid


def _append_completion_audit(
    conn: sqlite3.Connection,
    *,
    proposal: dict[str, Any],
    to_status: str,
    completion_public_id: str,
    version_number: int,
    current_hash: str,
    completed_hash: str,
    actor_id: str,
    changed_fields: list[str],
    created_at: str,
) -> None:
    event_type = "parser_proposal_completed"
    aggregate_id = str(proposal["public_id"])
    event_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_id,
        event_type=event_type,
        causation_public_id=completion_public_id,
    )

    from_status = str(proposal["parse_status"])
    previous_raw_intake = raw_intake_status_for_proposal_status(from_status)
    new_raw_intake = raw_intake_status_for_proposal_status(to_status)
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=aggregate_id,
            event_type=event_type,
            event_payload={
                "completion_public_id": completion_public_id,
                "version_number": version_number,
                "previous_content_hash": current_hash,
                "new_content_hash": completed_hash,
                "changed_fields": changed_fields,
            },
            previous_state={
                "parse_status": from_status,
                "raw_intake_status": previous_raw_intake,
                "proposal_content_hash": current_hash,
                "conversion_status": "not_converted",
            },
            new_state={
                "parse_status": to_status,
                "raw_intake_status": new_raw_intake,
                "proposal_content_hash": completed_hash,
                "conversion_status": "not_converted",
            },
            actor_type="human",
            actor_public_id=actor_id,
            authorization_public_id=completion_public_id,
            source_evidence_references=_parser_source_references(proposal),
            correlation_public_id=aggregate_id,
            causation_public_id=completion_public_id,
            created_at=created_at,
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


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------


def _validate_completion_public_id(completion_public_id: str) -> None:
    """Validate the caller-owned completion identity.

    The caller must supply a stable, documented public ID so the same command
    can be safely replayed after timeout, process restart or lost response.
    """
    if not isinstance(completion_public_id, str):
        raise InvalidCompletionIdError(
            f"completion_public_id must be a string, got {type(completion_public_id).__name__}"
        )
    if not completion_public_id or not completion_public_id.strip():
        raise InvalidCompletionIdError("completion_public_id must not be empty")
    if len(completion_public_id) > _COMPLETION_ID_MAX_LEN:
        raise InvalidCompletionIdError(
            f"completion_public_id must not exceed {_COMPLETION_ID_MAX_LEN} characters, "
            f"got {len(completion_public_id)}"
        )
    if not completion_public_id.startswith("pco_"):
        raise InvalidCompletionIdError("completion_public_id must start with 'pco_'")
    if not completion_public_id.isascii():
        raise InvalidCompletionIdError("completion_public_id must be ASCII")
    # Disallow whitespace characters embedded in the ID
    if any(c.isspace() for c in completion_public_id):
        raise InvalidCompletionIdError("completion_public_id must not contain whitespace")


def _begin_immediate(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise ProposalCompletionError(
            "Completion unit of work requires a connection without pending work"
        )
    conn.execute("BEGIN IMMEDIATE")


def _rollback_if_needed(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def _now(clock: Callable[[], str] | None) -> str:
    return clock() if clock is not None else datetime.now(timezone.utc).isoformat()


__all__ = [
    "CompletionConflictError",
    "InvalidCompletionIdError",
    "InvalidCompletionFieldValueError",
    "InvalidCompletionStatusError",
    "NoMaterialChangeError",
    "ProposalCompletionError",
    "StaleProposalContentError",
    "UnauthorizedCompletionActorError",
    "UnknownCompletionFieldError",
    "complete_proposal",
    "get_completion_by_public_id",
    "resolve_effective_proposal_payload",
]
