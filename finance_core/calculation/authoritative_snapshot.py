"""Canonical, append-only, hash-verifiable calculation snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from finance_core.money import canonical_decimal_str
from finance_core.staging_guard import require_staging_database

CANONICAL_JSON_CONTRACT_VERSION = "finance-canonical-json-v1"
SNAPSHOT_SCHEMA_VERSION = "v1"
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({SNAPSHOT_SCHEMA_VERSION})
VALID_FINALIZATION_STATUSES = frozenset({"draft", "finalized", "invalidated"})


class CanonicalSerializationError(ValueError):
    """A value cannot enter authoritative canonical JSON."""


class SnapshotVerificationError(ValueError):
    """Persisted snapshot content or binding is not trustworthy."""


class UnsupportedSnapshotVersionError(SnapshotVerificationError):
    """The snapshot version is not supported by this verifier."""


class SnapshotConflictError(SnapshotVerificationError):
    """A snapshot identity or content hash conflicts with durable state."""


class LegacySnapshotUnverifiedError(SnapshotVerificationError):
    """A migration-016 snapshot has no authoritative hash contract."""


def canonical_json_bytes(value: object) -> bytes:
    """Serialize supported values with a versioned deterministic contract."""
    normalized = _normalize(value)
    envelope = {
        "contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "value": normalized,
    }
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def canonical_json_value(payload_json: str, *, label: str = "payload") -> object:
    """Return the normalized value carried by a canonical snapshot payload.

    The stored payload is verified to be canonical JSON with the supported
    contract envelope before its ``value`` is returned, so a caller can read
    hash-bound snapshot material without re-implementing the envelope contract.
    """
    parsed = json.loads(_verified_canonical_bytes(payload_json, label).decode("utf-8"))
    return parsed["value"]


def _normalize(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalSerializationError("Decimal must be finite")
        return {"$decimal": canonical_decimal_str(value)}
    if isinstance(value, float):
        raise CanonicalSerializationError("float is not supported in authoritative JSON")
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalSerializationError("datetime must be timezone-aware")
        utc_value = value.astimezone(timezone.utc)
        return {"$datetime": utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalSerializationError("mapping keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in result:
                raise CanonicalSerializationError("Unicode normalization produced duplicate keys")
            result[key] = _normalize(raw_value)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise CanonicalSerializationError(
        f"unsupported authoritative JSON value: {type(value).__name__}"
    )


def _domain_hash(domain: str, payload: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + payload).hexdigest()


@dataclass(frozen=True)
class AuthoritativeCalculationSnapshot:
    snapshot_public_id: str
    snapshot_schema_version: str
    calculation_type: str
    aggregate_public_id: str
    input_payload_json: str
    output_payload_json: str
    rules_payload_json: str
    input_hash: str
    output_hash: str
    rules_hash: str
    combined_snapshot_hash: str
    money_contract_version: str
    currency_contract_version: str
    algorithm_version: str
    source_references: tuple[str, ...]
    previous_snapshot_public_id: str | None
    actor_type: str
    actor_public_id: str | None
    authorization_reference: str | None
    finalization_status: str
    created_at: str

    def verify(self) -> None:
        if self.snapshot_schema_version not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
            raise UnsupportedSnapshotVersionError(
                f"Unsupported snapshot schema version: {self.snapshot_schema_version}"
            )
        _validate_required_fields(self)
        input_bytes = _verified_canonical_bytes(self.input_payload_json, "input")
        output_bytes = _verified_canonical_bytes(self.output_payload_json, "output")
        rules_bytes = _verified_canonical_bytes(self.rules_payload_json, "rules")
        expected_input = _domain_hash("finance-snapshot-input-v1", input_bytes)
        expected_output = _domain_hash("finance-snapshot-output-v1", output_bytes)
        rules_binding = canonical_json_bytes(
            {
                "rules_payload": json.loads(rules_bytes),
                "algorithm_version": self.algorithm_version,
                "money_contract_version": self.money_contract_version,
                "currency_contract_version": self.currency_contract_version,
            }
        )
        expected_rules = _domain_hash("finance-snapshot-rules-v1", rules_binding)
        expected_combined = _combined_hash(
            self,
            input_hash=expected_input,
            output_hash=expected_output,
            rules_hash=expected_rules,
        )
        expected = (expected_input, expected_output, expected_rules, expected_combined)
        actual = (self.input_hash, self.output_hash, self.rules_hash, self.combined_snapshot_hash)
        if actual != expected:
            raise SnapshotVerificationError("Authoritative calculation snapshot hash mismatch")


def build_authoritative_snapshot(
    *,
    snapshot_public_id: str,
    calculation_type: str,
    aggregate_public_id: str,
    input_payload: object,
    output_payload: object,
    rules_payload: object,
    money_contract_version: str,
    currency_contract_version: str,
    algorithm_version: str,
    source_references: Sequence[str] = (),
    previous_snapshot_public_id: str | None = None,
    actor_type: str = "system",
    actor_public_id: str | None = None,
    authorization_reference: str | None = None,
    finalization_status: str = "draft",
    created_at: str,
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION,
) -> AuthoritativeCalculationSnapshot:
    references = _canonical_references(source_references)
    input_json = canonical_json_text(input_payload)
    output_json = canonical_json_text(output_payload)
    rules_json = canonical_json_text(rules_payload)
    input_hash = _domain_hash("finance-snapshot-input-v1", input_json.encode("utf-8"))
    output_hash = _domain_hash("finance-snapshot-output-v1", output_json.encode("utf-8"))
    rules_binding = canonical_json_bytes(
        {
            "rules_payload": json.loads(rules_json),
            "algorithm_version": algorithm_version,
            "money_contract_version": money_contract_version,
            "currency_contract_version": currency_contract_version,
        }
    )
    rules_hash = _domain_hash("finance-snapshot-rules-v1", rules_binding)
    provisional = AuthoritativeCalculationSnapshot(
        snapshot_public_id=snapshot_public_id,
        snapshot_schema_version=snapshot_schema_version,
        calculation_type=calculation_type,
        aggregate_public_id=aggregate_public_id,
        input_payload_json=input_json,
        output_payload_json=output_json,
        rules_payload_json=rules_json,
        input_hash=input_hash,
        output_hash=output_hash,
        rules_hash=rules_hash,
        combined_snapshot_hash="0" * 64,
        money_contract_version=money_contract_version,
        currency_contract_version=currency_contract_version,
        algorithm_version=algorithm_version,
        source_references=references,
        previous_snapshot_public_id=previous_snapshot_public_id,
        actor_type=actor_type,
        actor_public_id=actor_public_id,
        authorization_reference=authorization_reference,
        finalization_status=finalization_status,
        created_at=created_at,
    )
    combined = _combined_hash(
        provisional,
        input_hash=input_hash,
        output_hash=output_hash,
        rules_hash=rules_hash,
    )
    snapshot = replace(provisional, combined_snapshot_hash=combined)
    snapshot.verify()
    return snapshot


def _combined_hash(
    snapshot: AuthoritativeCalculationSnapshot,
    *,
    input_hash: str,
    output_hash: str,
    rules_hash: str,
) -> str:
    material = canonical_json_bytes(
        {
            "snapshot_schema_version": snapshot.snapshot_schema_version,
            "calculation_type": snapshot.calculation_type,
            "aggregate_public_id": snapshot.aggregate_public_id,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "rules_hash": rules_hash,
            "money_contract_version": snapshot.money_contract_version,
            "currency_contract_version": snapshot.currency_contract_version,
            "algorithm_version": snapshot.algorithm_version,
            "source_references": snapshot.source_references,
            "previous_snapshot_public_id": snapshot.previous_snapshot_public_id,
            "actor_type": snapshot.actor_type,
            "actor_public_id": snapshot.actor_public_id,
            "authorization_reference": snapshot.authorization_reference,
            "finalization_status": snapshot.finalization_status,
        }
    )
    return _domain_hash("finance-combined-snapshot-v1", material)


class AuthoritativeSnapshotRepository:
    """SQL-only append and verified load operations; no update or delete API."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, snapshot: AuthoritativeCalculationSnapshot) -> None:
        snapshot.verify()
        self._conn.execute(
            """INSERT INTO authoritative_calculation_snapshots (
            snapshot_public_id, snapshot_schema_version, calculation_type,
            aggregate_public_id, input_payload_json, output_payload_json,
            rules_payload_json, input_hash, output_hash, rules_hash,
            combined_snapshot_hash, money_contract_version, currency_contract_version,
            algorithm_version, source_references_json, previous_snapshot_public_id,
            actor_type, actor_public_id, authorization_reference, finalization_status,
            created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _snapshot_values(snapshot),
        )

    def fetch(self, snapshot_public_id: str) -> AuthoritativeCalculationSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM authoritative_calculation_snapshots WHERE snapshot_public_id = ?",
            (snapshot_public_id,),
        ).fetchone()
        return None if row is None else _row_to_snapshot(row)

    def fetch_by_hash(self, combined_hash: str) -> AuthoritativeCalculationSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM authoritative_calculation_snapshots WHERE combined_snapshot_hash = ?",
            (combined_hash,),
        ).fetchone()
        return None if row is None else _row_to_snapshot(row)


def persist_authoritative_snapshot(
    conn: sqlite3.Connection,
    snapshot: AuthoritativeCalculationSnapshot,
) -> tuple[AuthoritativeCalculationSnapshot, bool]:
    """Persist in one service-owned transaction; return (record, idempotent)."""
    require_staging_database(conn)
    if conn.in_transaction:
        raise SnapshotConflictError("Snapshot persistence requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = persist_authoritative_snapshot_in_transaction(conn, snapshot)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def persist_authoritative_snapshot_in_transaction(
    conn: sqlite3.Connection,
    snapshot: AuthoritativeCalculationSnapshot,
) -> tuple[AuthoritativeCalculationSnapshot, bool]:
    """Persist inside the caller's already-open transaction.

    The caller owns commit and rollback, so the snapshot (and its finalization
    audit event) becomes durable only together with every other write of the
    caller's Unit of Work -- a preparation can never leave a committed snapshot
    behind while its calculation run or binding evidence rolled back.  Same
    idempotency and conflict contract as :func:`persist_authoritative_snapshot`.
    """
    require_staging_database(conn)
    if not conn.in_transaction:
        raise SnapshotConflictError(
            "Caller-transaction snapshot persistence requires an open transaction"
        )
    repository = AuthoritativeSnapshotRepository(conn)
    existing = repository.fetch(snapshot.snapshot_public_id)
    if existing is not None:
        if existing == snapshot:
            return existing, True
        raise SnapshotConflictError("Snapshot public ID has conflicting content")
    duplicate = repository.fetch_by_hash(snapshot.combined_snapshot_hash)
    if duplicate is not None:
        raise SnapshotConflictError("Snapshot content already has a different public ID")
    repository.insert(snapshot)
    if snapshot.finalization_status == "finalized":
        _append_snapshot_finalization_audit(conn, snapshot)
    return snapshot, False


def _append_snapshot_finalization_audit(
    conn: sqlite3.Connection,
    snapshot: AuthoritativeCalculationSnapshot,
) -> None:
    # Local import avoids a module cycle: the audit hash contract reuses this
    # module's canonical JSON serializer.
    from finance_core.financial_audit import (
        AuditEventCommand,
        append_financial_audit_event,
        derive_audit_event_public_id,
    )

    event_type = "calculation_snapshot_finalized"
    causation_id = snapshot.snapshot_public_id
    event_id = derive_audit_event_public_id(
        aggregate_type="calculation_snapshot",
        aggregate_public_id=snapshot.snapshot_public_id,
        event_type=event_type,
        causation_public_id=causation_id,
    )
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="calculation_snapshot",
            aggregate_public_id=snapshot.snapshot_public_id,
            event_type=event_type,
            event_payload={
                "calculation_type": snapshot.calculation_type,
                "calculation_aggregate_public_id": snapshot.aggregate_public_id,
                "input_hash": snapshot.input_hash,
                "output_hash": snapshot.output_hash,
                "rules_hash": snapshot.rules_hash,
                "combined_snapshot_hash": snapshot.combined_snapshot_hash,
                "algorithm_version": snapshot.algorithm_version,
                "money_contract_version": snapshot.money_contract_version,
                "currency_contract_version": snapshot.currency_contract_version,
            },
            previous_state={"finalization_status": "draft"},
            new_state={
                "finalization_status": "finalized",
                "combined_snapshot_hash": snapshot.combined_snapshot_hash,
            },
            actor_type=snapshot.actor_type,
            actor_public_id=(
                snapshot.actor_public_id or f"calculation-snapshot:{snapshot.actor_type}"
            ),
            authorization_public_id=snapshot.authorization_reference,
            calculation_snapshot_public_id=snapshot.snapshot_public_id,
            calculation_snapshot_hash=snapshot.combined_snapshot_hash,
            source_evidence_references=snapshot.source_references,
            correlation_public_id=snapshot.aggregate_public_id,
            causation_public_id=causation_id,
            created_at=snapshot.created_at,
        ),
    )


def load_snapshot_for_authoritative_use(
    conn: sqlite3.Connection,
    snapshot_public_id: str,
) -> AuthoritativeCalculationSnapshot:
    repository = AuthoritativeSnapshotRepository(conn)
    snapshot = repository.fetch(snapshot_public_id)
    if snapshot is not None:
        return snapshot
    legacy = conn.execute(
        "SELECT 1 FROM calculation_snapshots WHERE snapshot_id = ?",
        (snapshot_public_id,),
    ).fetchone()
    if legacy is not None:
        raise LegacySnapshotUnverifiedError(
            "Legacy calculation snapshot is unverified for authoritative use"
        )
    raise SnapshotVerificationError(f"Authoritative snapshot not found: {snapshot_public_id}")


def verify_snapshot_binding(
    conn: sqlite3.Connection,
    *,
    snapshot_public_id: str,
    expected_combined_hash: str,
    expected_calculation_type: str,
    expected_aggregate_public_id: str,
    expected_currency_contract_version: str,
    expected_authorization_reference: str | None = None,
    expected_output_payload: object | None = None,
) -> AuthoritativeCalculationSnapshot:
    snapshot = load_snapshot_for_authoritative_use(conn, snapshot_public_id)
    if snapshot.combined_snapshot_hash != expected_combined_hash:
        raise SnapshotVerificationError("Finalization snapshot hash does not match")
    if snapshot.calculation_type != expected_calculation_type:
        raise SnapshotVerificationError("Finalization calculation type does not match")
    if snapshot.aggregate_public_id != expected_aggregate_public_id:
        raise SnapshotVerificationError("Finalization aggregate identity does not match")
    if snapshot.currency_contract_version != expected_currency_contract_version:
        raise SnapshotVerificationError("Finalization currency contract version does not match")
    if (
        expected_authorization_reference is not None
        and snapshot.authorization_reference != expected_authorization_reference
    ):
        raise SnapshotVerificationError("Finalization authorization does not match snapshot")
    if (
        expected_output_payload is not None
        and canonical_json_text(expected_output_payload) != snapshot.output_payload_json
    ):
        raise SnapshotVerificationError("Finalization output does not match snapshot")
    if snapshot.finalization_status != "finalized":
        raise SnapshotVerificationError("Authoritative snapshot is not finalized")
    return snapshot


def _snapshot_values(snapshot: AuthoritativeCalculationSnapshot) -> tuple[object, ...]:
    return (
        snapshot.snapshot_public_id,
        snapshot.snapshot_schema_version,
        snapshot.calculation_type,
        snapshot.aggregate_public_id,
        snapshot.input_payload_json,
        snapshot.output_payload_json,
        snapshot.rules_payload_json,
        snapshot.input_hash,
        snapshot.output_hash,
        snapshot.rules_hash,
        snapshot.combined_snapshot_hash,
        snapshot.money_contract_version,
        snapshot.currency_contract_version,
        snapshot.algorithm_version,
        json.dumps(snapshot.source_references, separators=(",", ":")),
        snapshot.previous_snapshot_public_id,
        snapshot.actor_type,
        snapshot.actor_public_id,
        snapshot.authorization_reference,
        snapshot.finalization_status,
        snapshot.created_at,
    )


def _row_to_snapshot(row: Any) -> AuthoritativeCalculationSnapshot:
    values = dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    try:
        references_raw = json.loads(values["source_references_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotVerificationError("Snapshot source references are invalid") from exc
    if not isinstance(references_raw, list) or any(
        not isinstance(item, str) for item in references_raw
    ):
        raise SnapshotVerificationError("Snapshot source references are invalid")
    snapshot = AuthoritativeCalculationSnapshot(
        snapshot_public_id=values["snapshot_public_id"],
        snapshot_schema_version=values["snapshot_schema_version"],
        calculation_type=values["calculation_type"],
        aggregate_public_id=values["aggregate_public_id"],
        input_payload_json=values["input_payload_json"],
        output_payload_json=values["output_payload_json"],
        rules_payload_json=values["rules_payload_json"],
        input_hash=values["input_hash"],
        output_hash=values["output_hash"],
        rules_hash=values["rules_hash"],
        combined_snapshot_hash=values["combined_snapshot_hash"],
        money_contract_version=values["money_contract_version"],
        currency_contract_version=values["currency_contract_version"],
        algorithm_version=values["algorithm_version"],
        source_references=tuple(references_raw),
        previous_snapshot_public_id=values["previous_snapshot_public_id"],
        actor_type=values["actor_type"],
        actor_public_id=values["actor_public_id"],
        authorization_reference=values["authorization_reference"],
        finalization_status=values["finalization_status"],
        created_at=values["created_at"],
    )
    snapshot.verify()
    return snapshot


def _verified_canonical_bytes(value: str, label: str) -> bytes:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotVerificationError(f"Stored {label} payload is invalid JSON") from exc
    canonical = json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != value:
        raise SnapshotVerificationError(f"Stored {label} payload is not canonical JSON")
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"contract_version", "value"}
        or parsed["contract_version"] != CANONICAL_JSON_CONTRACT_VERSION
    ):
        raise SnapshotVerificationError(
            f"Stored {label} payload has an unsupported canonical JSON contract"
        )
    return value.encode("utf-8")


def _canonical_references(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SnapshotVerificationError("Source references must be a sequence of strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise SnapshotVerificationError("Source references must be non-empty strings")
    normalized = tuple(sorted(unicodedata.normalize("NFC", value) for value in values))
    if len(set(normalized)) != len(normalized):
        raise SnapshotVerificationError("Source references must be unique")
    return normalized


def _validate_required_fields(snapshot: AuthoritativeCalculationSnapshot) -> None:
    required = {
        "snapshot_public_id": snapshot.snapshot_public_id,
        "calculation_type": snapshot.calculation_type,
        "aggregate_public_id": snapshot.aggregate_public_id,
        "money_contract_version": snapshot.money_contract_version,
        "currency_contract_version": snapshot.currency_contract_version,
        "algorithm_version": snapshot.algorithm_version,
        "actor_type": snapshot.actor_type,
        "created_at": snapshot.created_at,
    }
    missing = [
        name for name, value in required.items() if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise SnapshotVerificationError(f"Missing snapshot fields: {', '.join(missing)}")
    if snapshot.finalization_status not in VALID_FINALIZATION_STATUSES:
        raise SnapshotVerificationError("Invalid snapshot finalization status")
    try:
        created_at = datetime.fromisoformat(snapshot.created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotVerificationError("Snapshot created_at must be ISO 8601") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise SnapshotVerificationError("Snapshot created_at must be timezone-aware")
    if snapshot.previous_snapshot_public_id == snapshot.snapshot_public_id:
        raise SnapshotVerificationError("Snapshot cannot reference itself as previous")
    if _canonical_references(snapshot.source_references) != snapshot.source_references:
        raise SnapshotVerificationError("Snapshot source references are not canonical")


__all__ = [
    "AuthoritativeCalculationSnapshot",
    "AuthoritativeSnapshotRepository",
    "CANONICAL_JSON_CONTRACT_VERSION",
    "CanonicalSerializationError",
    "LegacySnapshotUnverifiedError",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotConflictError",
    "SnapshotVerificationError",
    "UnsupportedSnapshotVersionError",
    "build_authoritative_snapshot",
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_json_value",
    "load_snapshot_for_authoritative_use",
    "persist_authoritative_snapshot",
    "persist_authoritative_snapshot_in_transaction",
    "verify_snapshot_binding",
]
