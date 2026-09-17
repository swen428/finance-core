"""Versioned append-only hash chains for material financial state changes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from finance_core.calculation.authoritative_snapshot import (
    CANONICAL_JSON_CONTRACT_VERSION,
    canonical_json_bytes,
    canonical_json_text,
)
from finance_core.staging_guard import require_staging_database

AUDIT_SCHEMA_VERSION = "v1"
SUPPORTED_AUDIT_SCHEMA_VERSIONS = frozenset({AUDIT_SCHEMA_VERSION})
ZERO_AUDIT_HASH = "0" * 64

# Migration 035 adds an INSERT-collision backstop trigger over the five
# append-only event identities (migration 025 is historical and frozen).
# Its exact RAISE(ABORT) message is matched below so this one trigger keeps
# the typed conflict contract without misclassifying any other
# SQLITE_CONSTRAINT_TRIGGER failure as an audit identity conflict.
_MIGRATION_035_AUDIT_COLLISION_MESSAGE = (
    "UNIQUE audit event identity collision: financial_audit_events rows "
    "are append-only and cannot be replaced"
)


class AuditVerificationError(ValueError):
    """An audit event or chain cannot be trusted."""


class UnsupportedAuditVersionError(AuditVerificationError):
    """An audit event uses a schema version this verifier does not support."""


class AuditChainConflictError(AuditVerificationError):
    """An append conflicts with an existing identity, replay, or chain head."""


class AuditChainTransactionError(RuntimeError):
    """An audit append was attempted outside its required transaction boundary."""


@dataclass(frozen=True)
class AuditEventCommand:
    event_public_id: str
    aggregate_type: str
    aggregate_public_id: str
    event_type: str
    event_payload: object
    new_state: object
    actor_type: str
    actor_public_id: str
    correlation_public_id: str
    causation_public_id: str
    created_at: str
    previous_state: object | None = None
    expected_previous_event_hash: str | None = None
    authorization_public_id: str | None = None
    calculation_snapshot_public_id: str | None = None
    calculation_snapshot_hash: str | None = None
    source_evidence_references: Sequence[str] = ()
    audit_schema_version: str = AUDIT_SCHEMA_VERSION


@dataclass(frozen=True)
class FinancialAuditEvent:
    event_public_id: str
    audit_schema_version: str
    aggregate_type: str
    aggregate_public_id: str
    event_type: str
    event_payload_json: str
    previous_state_json: str
    new_state_json: str
    previous_state_hash: str
    new_state_hash: str
    previous_event_hash: str
    event_hash: str
    actor_type: str
    actor_public_id: str
    authorization_public_id: str | None
    calculation_snapshot_public_id: str | None
    calculation_snapshot_hash: str | None
    source_evidence_references: tuple[str, ...]
    correlation_public_id: str
    causation_public_id: str
    sequence_number: int
    created_at: str

    def verify(self) -> None:
        if self.audit_schema_version not in SUPPORTED_AUDIT_SCHEMA_VERSIONS:
            raise UnsupportedAuditVersionError(
                f"Unsupported audit schema version: {self.audit_schema_version}"
            )
        _validate_event_fields(self)
        _verified_canonical_json_bytes(self.event_payload_json, "event payload")
        previous_state_bytes = _verified_canonical_json_bytes(
            self.previous_state_json, "previous state"
        )
        new_state_bytes = _verified_canonical_json_bytes(self.new_state_json, "new state")
        expected_previous_state = _domain_hash("finance-audit-state-v1", previous_state_bytes)
        expected_new_state = _domain_hash("finance-audit-state-v1", new_state_bytes)
        if self.previous_state_hash != expected_previous_state:
            raise AuditVerificationError("Audit previous-state hash mismatch")
        if self.new_state_hash != expected_new_state:
            raise AuditVerificationError("Audit new-state hash mismatch")
        expected_event = _event_hash(
            self,
            previous_state_hash=expected_previous_state,
            new_state_hash=expected_new_state,
        )
        if self.event_hash != expected_event:
            raise AuditVerificationError("Audit event hash mismatch")


@dataclass(frozen=True)
class AuditChainVerification:
    valid: bool
    aggregate_type: str
    aggregate_public_id: str
    event_count: int
    first_invalid_event_public_id: str | None = None
    first_invalid_sequence_number: int | None = None
    reason: str | None = None
    legacy_without_chain: bool = False


class FinancialAuditRepository:
    """Transaction-neutral SQL operations; no update/delete or commit API."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, event: FinancialAuditEvent) -> None:
        event.verify()
        self._conn.execute(
            """INSERT INTO financial_audit_events (
            event_public_id, audit_schema_version, aggregate_type,
            aggregate_public_id, event_type, event_payload_json,
            previous_state_json, new_state_json, previous_state_hash,
            new_state_hash, previous_event_hash, event_hash, actor_type,
            actor_public_id, authorization_public_id,
            calculation_snapshot_public_id, calculation_snapshot_hash,
            source_evidence_refs_json, correlation_public_id,
            causation_public_id, sequence_number, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _event_values(event),
        )

    def fetch(self, event_public_id: str) -> FinancialAuditEvent | None:
        row = self._conn.execute(
            "SELECT * FROM financial_audit_events WHERE event_public_id = ?",
            (event_public_id,),
        ).fetchone()
        return None if row is None else _row_to_event(row)

    def head(self, aggregate_type: str, aggregate_public_id: str) -> FinancialAuditEvent | None:
        row = self._conn.execute(
            """SELECT * FROM financial_audit_events
            WHERE aggregate_type = ? AND aggregate_public_id = ?
            ORDER BY sequence_number DESC LIMIT 1""",
            (aggregate_type, aggregate_public_id),
        ).fetchone()
        return None if row is None else _row_to_event(row)

    def list_chain(
        self, aggregate_type: str, aggregate_public_id: str
    ) -> list[FinancialAuditEvent]:
        rows = self._conn.execute(
            """SELECT * FROM financial_audit_events
            WHERE aggregate_type = ? AND aggregate_public_id = ?
            ORDER BY sequence_number ASC""",
            (aggregate_type, aggregate_public_id),
        ).fetchall()
        return [_row_to_event(row) for row in rows]


def derive_audit_event_public_id(
    *,
    aggregate_type: str,
    aggregate_public_id: str,
    event_type: str,
    causation_public_id: str,
) -> str:
    material = canonical_json_bytes(
        {
            "aggregate_type": aggregate_type,
            "aggregate_public_id": aggregate_public_id,
            "event_type": event_type,
            "causation_public_id": causation_public_id,
        }
    )
    return f"fae-{_domain_hash('finance-audit-event-id-v1', material)[:24]}"


def financial_state_hash(state: object) -> str:
    return _domain_hash("finance-audit-state-v1", canonical_json_bytes(state))


def append_financial_audit_event(
    conn: sqlite3.Connection,
    command: AuditEventCommand,
) -> tuple[FinancialAuditEvent, bool]:
    """Append inside a caller-owned transaction; return (event, idempotent)."""
    require_staging_database(conn)
    if not conn.in_transaction:
        raise AuditChainTransactionError(
            "Financial audit append requires an active caller-owned transaction"
        )
    _validate_command(command)
    repository = FinancialAuditRepository(conn)
    chain = verify_financial_audit_chain(
        conn,
        aggregate_type=command.aggregate_type,
        aggregate_public_id=command.aggregate_public_id,
    )
    if not chain.valid:
        raise AuditChainConflictError(
            "Cannot append to invalid audit chain at "
            f"{chain.first_invalid_event_public_id}: {chain.reason}"
        )
    existing = repository.fetch(command.event_public_id)
    if existing is not None:
        candidate = _build_event(
            command,
            sequence_number=existing.sequence_number,
            previous_event_hash=existing.previous_event_hash,
            previous_state_json=(
                canonical_json_text(command.previous_state)
                if command.previous_state is not None
                else existing.previous_state_json
            ),
            created_at=existing.created_at,
        )
        if candidate == existing:
            return existing, True
        raise AuditChainConflictError("Audit event public ID has conflicting content")

    head = repository.head(command.aggregate_type, command.aggregate_public_id)
    previous_event_hash = ZERO_AUDIT_HASH if head is None else head.event_hash
    if (
        command.expected_previous_event_hash is not None
        and command.expected_previous_event_hash != previous_event_hash
    ):
        raise AuditChainConflictError("Audit chain predecessor changed")
    if command.previous_state is None:
        previous_state_json = canonical_json_text(None) if head is None else head.new_state_json
    else:
        previous_state_json = canonical_json_text(command.previous_state)
        if head is not None and financial_state_hash(command.previous_state) != head.new_state_hash:
            raise AuditChainConflictError("Audit previous state does not match chain head")
    event = _build_event(
        command,
        sequence_number=1 if head is None else head.sequence_number + 1,
        previous_event_hash=previous_event_hash,
        previous_state_json=previous_state_json,
        created_at=_normalize_timestamp(command.created_at),
    )
    try:
        repository.insert(event)
    except sqlite3.IntegrityError as exc:
        if getattr(exc, "sqlite_errorname", "") in {
            "SQLITE_CONSTRAINT_PRIMARYKEY",
            "SQLITE_CONSTRAINT_UNIQUE",
        }:
            raise AuditChainConflictError("Audit chain append conflicted in SQLite") from exc
        if (
            getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_TRIGGER"
            and str(exc) == _MIGRATION_035_AUDIT_COLLISION_MESSAGE
        ):
            # Round 3 fix F5: only the exact migration 035 collision-backstop
            # message keeps the typed identity-conflict contract; every other
            # trigger-raised IntegrityError propagates unchanged.
            raise AuditChainConflictError("Audit chain append conflicted in SQLite") from exc
        raise
    return event, False


def append_financial_audit_event_atomically(
    conn: sqlite3.Connection,
    command: AuditEventCommand,
) -> tuple[FinancialAuditEvent, bool]:
    """Own a transaction for a standalone audit append."""
    require_staging_database(conn)
    if conn.in_transaction:
        raise AuditChainTransactionError("Standalone audit append requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = append_financial_audit_event(conn, command)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def verify_financial_audit_chain(
    conn: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_public_id: str,
) -> AuditChainVerification:
    rows = conn.execute(
        """SELECT * FROM financial_audit_events
        WHERE aggregate_type = ? AND aggregate_public_id = ?
        ORDER BY sequence_number ASC""",
        (aggregate_type, aggregate_public_id),
    ).fetchall()
    if not rows:
        return AuditChainVerification(
            valid=True,
            aggregate_type=aggregate_type,
            aggregate_public_id=aggregate_public_id,
            event_count=0,
            legacy_without_chain=True,
        )
    previous: FinancialAuditEvent | None = None
    for index, row in enumerate(rows, start=1):
        event_id: str | None = None
        sequence: int | None = None
        try:
            event_id = str(row["event_public_id"])
            sequence = int(row["sequence_number"])
            event = _row_to_event(row)
            if event.sequence_number != index:
                raise AuditVerificationError("Audit sequence is not contiguous")
            expected_previous_hash = ZERO_AUDIT_HASH if previous is None else previous.event_hash
            if event.previous_event_hash != expected_previous_hash:
                raise AuditVerificationError("Audit previous-event hash mismatch")
            if previous is not None and event.previous_state_hash != previous.new_state_hash:
                raise AuditVerificationError("Audit state continuity mismatch")
        except (AuditVerificationError, KeyError, TypeError, ValueError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, AuditVerificationError)
                else f"Malformed audit row: {exc}"
            )
            return AuditChainVerification(
                valid=False,
                aggregate_type=aggregate_type,
                aggregate_public_id=aggregate_public_id,
                event_count=len(rows),
                first_invalid_event_public_id=event_id,
                first_invalid_sequence_number=sequence,
                reason=reason,
            )
        previous = event
    return AuditChainVerification(
        valid=True,
        aggregate_type=aggregate_type,
        aggregate_public_id=aggregate_public_id,
        event_count=len(rows),
    )


def _build_event(
    command: AuditEventCommand,
    *,
    sequence_number: int,
    previous_event_hash: str,
    previous_state_json: str,
    created_at: str,
) -> FinancialAuditEvent:
    payload_json = canonical_json_text(command.event_payload)
    new_state_json = canonical_json_text(command.new_state)
    references = _canonical_references(command.source_evidence_references)
    provisional = FinancialAuditEvent(
        event_public_id=command.event_public_id,
        audit_schema_version=command.audit_schema_version,
        aggregate_type=command.aggregate_type,
        aggregate_public_id=command.aggregate_public_id,
        event_type=command.event_type,
        event_payload_json=payload_json,
        previous_state_json=previous_state_json,
        new_state_json=new_state_json,
        previous_state_hash=_domain_hash(
            "finance-audit-state-v1", previous_state_json.encode("utf-8")
        ),
        new_state_hash=_domain_hash("finance-audit-state-v1", new_state_json.encode("utf-8")),
        previous_event_hash=previous_event_hash,
        event_hash=ZERO_AUDIT_HASH,
        actor_type=command.actor_type,
        actor_public_id=command.actor_public_id,
        authorization_public_id=command.authorization_public_id,
        calculation_snapshot_public_id=command.calculation_snapshot_public_id,
        calculation_snapshot_hash=command.calculation_snapshot_hash,
        source_evidence_references=references,
        correlation_public_id=command.correlation_public_id,
        causation_public_id=command.causation_public_id,
        sequence_number=sequence_number,
        created_at=created_at,
    )
    event = replace(
        provisional,
        event_hash=_event_hash(
            provisional,
            previous_state_hash=provisional.previous_state_hash,
            new_state_hash=provisional.new_state_hash,
        ),
    )
    event.verify()
    return event


def _event_hash(
    event: FinancialAuditEvent,
    *,
    previous_state_hash: str,
    new_state_hash: str,
) -> str:
    material = canonical_json_bytes(
        {
            "event_public_id": event.event_public_id,
            "audit_schema_version": event.audit_schema_version,
            "aggregate_type": event.aggregate_type,
            "aggregate_public_id": event.aggregate_public_id,
            "event_type": event.event_type,
            "event_payload_json": event.event_payload_json,
            "previous_state_json": event.previous_state_json,
            "new_state_json": event.new_state_json,
            "previous_state_hash": previous_state_hash,
            "new_state_hash": new_state_hash,
            "previous_event_hash": event.previous_event_hash,
            "actor_type": event.actor_type,
            "actor_public_id": event.actor_public_id,
            "authorization_public_id": event.authorization_public_id,
            "calculation_snapshot_public_id": event.calculation_snapshot_public_id,
            "calculation_snapshot_hash": event.calculation_snapshot_hash,
            "source_evidence_references": event.source_evidence_references,
            "correlation_public_id": event.correlation_public_id,
            "causation_public_id": event.causation_public_id,
            "sequence_number": event.sequence_number,
            "created_at": event.created_at,
        }
    )
    return _domain_hash("finance-audit-event-v1", material)


def _event_values(event: FinancialAuditEvent) -> tuple[object, ...]:
    return (
        event.event_public_id,
        event.audit_schema_version,
        event.aggregate_type,
        event.aggregate_public_id,
        event.event_type,
        event.event_payload_json,
        event.previous_state_json,
        event.new_state_json,
        event.previous_state_hash,
        event.new_state_hash,
        event.previous_event_hash,
        event.event_hash,
        event.actor_type,
        event.actor_public_id,
        event.authorization_public_id,
        event.calculation_snapshot_public_id,
        event.calculation_snapshot_hash,
        json.dumps(event.source_evidence_references, separators=(",", ":")),
        event.correlation_public_id,
        event.causation_public_id,
        event.sequence_number,
        event.created_at,
    )


def _row_to_event(row: Any) -> FinancialAuditEvent:
    values = dict(row)
    try:
        raw_references = json.loads(values["source_evidence_refs_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuditVerificationError("Audit source references are invalid") from exc
    if not isinstance(raw_references, list) or any(
        not isinstance(reference, str) for reference in raw_references
    ):
        raise AuditVerificationError("Audit source references are invalid")
    event = FinancialAuditEvent(
        event_public_id=values["event_public_id"],
        audit_schema_version=values["audit_schema_version"],
        aggregate_type=values["aggregate_type"],
        aggregate_public_id=values["aggregate_public_id"],
        event_type=values["event_type"],
        event_payload_json=values["event_payload_json"],
        previous_state_json=values["previous_state_json"],
        new_state_json=values["new_state_json"],
        previous_state_hash=values["previous_state_hash"],
        new_state_hash=values["new_state_hash"],
        previous_event_hash=values["previous_event_hash"],
        event_hash=values["event_hash"],
        actor_type=values["actor_type"],
        actor_public_id=values["actor_public_id"],
        authorization_public_id=values["authorization_public_id"],
        calculation_snapshot_public_id=values["calculation_snapshot_public_id"],
        calculation_snapshot_hash=values["calculation_snapshot_hash"],
        source_evidence_references=tuple(raw_references),
        correlation_public_id=values["correlation_public_id"],
        causation_public_id=values["causation_public_id"],
        sequence_number=values["sequence_number"],
        created_at=values["created_at"],
    )
    event.verify()
    return event


def _validate_command(command: AuditEventCommand) -> None:
    if command.audit_schema_version not in SUPPORTED_AUDIT_SCHEMA_VERSIONS:
        raise UnsupportedAuditVersionError(
            f"Unsupported audit schema version: {command.audit_schema_version}"
        )
    required = {
        "event_public_id": command.event_public_id,
        "aggregate_type": command.aggregate_type,
        "aggregate_public_id": command.aggregate_public_id,
        "event_type": command.event_type,
        "actor_type": command.actor_type,
        "actor_public_id": command.actor_public_id,
        "correlation_public_id": command.correlation_public_id,
        "causation_public_id": command.causation_public_id,
    }
    missing = [
        name for name, value in required.items() if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise AuditVerificationError(f"Missing audit fields: {', '.join(missing)}")
    if command.expected_previous_event_hash is not None:
        _validate_hash(command.expected_previous_event_hash, "expected predecessor")
    if (command.calculation_snapshot_public_id is None) != (
        command.calculation_snapshot_hash is None
    ):
        raise AuditVerificationError("Calculation snapshot ID and hash must be supplied together")
    if (
        command.calculation_snapshot_public_id is not None
        and not command.calculation_snapshot_public_id.strip()
    ):
        raise AuditVerificationError("Calculation snapshot public ID must not be empty")
    if command.calculation_snapshot_hash is not None:
        _validate_hash(command.calculation_snapshot_hash, "calculation snapshot")
    if command.authorization_public_id is not None and not command.authorization_public_id.strip():
        raise AuditVerificationError("Authorization public ID must not be empty")
    _normalize_timestamp(command.created_at)
    _canonical_references(command.source_evidence_references)


def _validate_event_fields(event: FinancialAuditEvent) -> None:
    _validate_command(
        AuditEventCommand(
            event_public_id=event.event_public_id,
            audit_schema_version=event.audit_schema_version,
            aggregate_type=event.aggregate_type,
            aggregate_public_id=event.aggregate_public_id,
            event_type=event.event_type,
            event_payload=None,
            previous_state=None,
            new_state=None,
            actor_type=event.actor_type,
            actor_public_id=event.actor_public_id,
            authorization_public_id=event.authorization_public_id,
            calculation_snapshot_public_id=event.calculation_snapshot_public_id,
            calculation_snapshot_hash=event.calculation_snapshot_hash,
            source_evidence_references=event.source_evidence_references,
            correlation_public_id=event.correlation_public_id,
            causation_public_id=event.causation_public_id,
            expected_previous_event_hash=event.previous_event_hash,
            created_at=event.created_at,
        )
    )
    if event.sequence_number <= 0:
        raise AuditVerificationError("Audit sequence number must be positive")
    _validate_hash(event.previous_state_hash, "previous state")
    _validate_hash(event.new_state_hash, "new state")
    _validate_hash(event.previous_event_hash, "previous event")
    _validate_hash(event.event_hash, "event")
    if _normalize_timestamp(event.created_at) != event.created_at:
        raise AuditVerificationError("Audit created_at is not canonical UTC")
    if _canonical_references(event.source_evidence_references) != (
        event.source_evidence_references
    ):
        raise AuditVerificationError("Audit source references are not canonical")


def _validate_hash(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AuditVerificationError(f"Invalid {label} SHA-256 hash")


def _canonical_references(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AuditVerificationError("Audit source references must be a sequence")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise AuditVerificationError("Audit source references must be non-empty strings")
    normalized = tuple(sorted(unicodedata.normalize("NFC", value) for value in values))
    if len(set(normalized)) != len(normalized):
        raise AuditVerificationError("Audit source references must be unique")
    return normalized


def _normalize_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditVerificationError("Audit created_at must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuditVerificationError("Audit created_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _verified_canonical_json_bytes(value: str, label: str) -> bytes:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuditVerificationError(f"Stored {label} is invalid JSON") from exc
    canonical = json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != value:
        raise AuditVerificationError(f"Stored {label} is not canonical JSON")
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"contract_version", "value"}
        or parsed["contract_version"] != CANONICAL_JSON_CONTRACT_VERSION
    ):
        raise AuditVerificationError(f"Stored {label} has unsupported canonical contract")
    return value.encode("utf-8")


def _domain_hash(domain: str, payload: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + payload).hexdigest()
