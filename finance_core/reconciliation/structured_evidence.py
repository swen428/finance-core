"""Reconciliation Structured Evidence Persistence v1 -- persists structured
reconciliation evidence records into the reconciliation_structured_evidence
table.

All methods operate on an externally-provided ``sqlite3.Connection``.
The caller is responsible for connection lifecycle and ensuring the
reconciliation persistence schema (including migration 011) is already
applied.

Key design properties:
- Never opens or modifies database/finance.db.
- Deterministic JSON serialization for evidence_payload.
- Idempotent: saving the same public_id with identical data is safe.
- Duplicate public_id with different data raises
  ``StructuredEvidencePersistenceError``.
- Does NOT create, update, delete, merge, or overwrite final financial
  transaction records.
- Does NOT implement auto-apply, auto-resolution, or settlement logic.
- Decimal-safe: monetary values in evidence payloads are serialized as
  strings, never floats.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StructuredEvidencePersistenceError(Exception):
    """Base exception for structured evidence persistence errors."""


class StructuredEvidenceConflictError(StructuredEvidencePersistenceError):
    """Raised when a persisted evidence record conflicts with an existing row."""


# ---------------------------------------------------------------------------
# Evidence record model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuredEvidenceRecord:
    """Input model for a single structured evidence record.

    Fields
    ------
    public_id:
        Stable external identifier. Callers should use
        ``generate_evidence_public_id()`` or provide their own.
    evidence_type:
        Class of evidence (e.g. ``"statement_transaction"``,
        ``"matching_decision"``, ``"review_resolution"``).
    source_type:
        Generator of the evidence (e.g. ``"statement_csv"``,
        ``"matching_engine"``, ``"apply_runtime"``).
    evidence_payload:
        JSON-serializable dict of the evidence body.
    confidence_score:
        Text-encoded Decimal confidence (e.g. ``"0.95"``).
    review_queue_public_id:
        Public_id of the related review queue item, if any.
    statement_transaction_id:
        Public_id of the related statement transaction, if any.
    app_transaction_id:
        Public_id of the related app transaction, if any.
    source_id / source_path / source_page / source_row / source_field:
        Future PDF/OCR source references.
    """

    public_id: str
    evidence_type: str
    source_type: str
    evidence_payload: dict[str, Any]
    confidence_score: str = "0.0"
    review_queue_public_id: str | None = None
    statement_transaction_id: str | None = None
    app_transaction_id: str | None = None
    source_id: str | None = None
    source_path: str | None = None
    source_page: int | None = None
    source_row: int | None = None
    source_field: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.public_id.strip():
            raise ValueError("public_id must not be empty")
        if not self.evidence_type.strip():
            raise ValueError("evidence_type must not be empty")
        if not self.source_type.strip():
            raise ValueError("source_type must not be empty")
        if not isinstance(self.evidence_payload, dict):
            raise ValueError("evidence_payload must be a dict")


# ---------------------------------------------------------------------------
# Public-id generation
# ---------------------------------------------------------------------------


def generate_evidence_public_id(
    evidence_type: str,
    source_type: str,
    *,
    review_queue_public_id: str | None = None,
    statement_transaction_id: str | None = None,
    app_transaction_id: str | None = None,
    source_id: str | None = None,
    source_path: str | None = None,
    source_page: int | None = None,
    source_row: int | None = None,
    source_field: str | None = None,
    payload_digest: str | None = None,
) -> str:
    """Generate a deterministic public_id for a structured evidence record.

    Uses SHA-256 over the key fields to produce a stable, collision-resistant
    identifier with the ``sev-`` prefix.  The same inputs always produce the
    same public_id, so idempotent re-inserts are safe.

    Callers may also provide their own public_id instead of using this helper.
    """
    payload = json.dumps(
        {
            "evidence_type": evidence_type,
            "source_type": source_type,
            "review_queue_public_id": review_queue_public_id,
            "statement_transaction_id": statement_transaction_id,
            "app_transaction_id": app_transaction_id,
            "source_id": source_id,
            "source_path": source_path,
            "source_page": source_page,
            "source_row": source_row,
            "source_field": source_field,
            "payload_digest": payload_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"sev-{digest}"


# ---------------------------------------------------------------------------
# Evidence query result (typed read model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceQueryResult:
    """Read-only typed view of a stored structured evidence record.

    All fields map directly to columns in the
    ``reconciliation_structured_evidence`` table.  The ``evidence_payload``
    field holds the raw JSON text as stored; use :meth:`parsed_payload` to
    decode it safely.

    This dataclass is intentionally frozen -- callers receive a snapshot
    and cannot mutate the underlying evidence row through this object.
    """

    id: int
    public_id: str
    review_queue_public_id: str | None
    statement_transaction_id: str | None
    app_transaction_id: str | None
    evidence_type: str
    source_type: str
    source_id: str | None
    source_path: str | None
    source_page: int | None
    source_row: int | None
    source_field: str | None
    confidence_score: str
    evidence_payload: str  # raw JSON text, exactly as stored
    created_at: str
    updated_at: str

    def parsed_payload(self) -> dict[str, Any]:
        """Safely parse the stored ``evidence_payload`` JSON.

        Returns the decoded dict on success.

        Raises
        ------
        ValueError
            If the stored JSON is malformed and cannot be decoded.  The raw
            payload text is always accessible via ``self.evidence_payload``
            regardless of this outcome.
        """
        try:
            parsed = json.loads(self.evidence_payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Malformed evidence_payload JSON for evidence public_id={self.public_id!r}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"evidence_payload for public_id={self.public_id!r} "
                f"decoded to {type(parsed).__name__}, expected dict"
            )
        return parsed

    def parsed_payload_or_none(self) -> dict[str, Any] | None:
        """Like :meth:`parsed_payload` but returns ``None`` on failure.

        Use this when the caller wants to degrade gracefully rather than
        raising on malformed JSON.
        """
        try:
            return self.parsed_payload()
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Persistence adapter
# ---------------------------------------------------------------------------


class StructuredEvidencePersistence:
    """SQLite persistence adapter for ``StructuredEvidenceRecord`` objects.

    Usage::

        conn = connect_sqlite("/tmp/recon_evidence_demo.db")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # ... apply the default temp migration chain ...
        sep = StructuredEvidencePersistence(conn)
        record = StructuredEvidenceRecord(
            public_id=generate_evidence_public_id(
                "matching_decision", "matching_engine",
                statement_transaction_id="stmt-001",
            ),
            evidence_type="matching_decision",
            source_type="matching_engine",
            evidence_payload={"match_status": "matched", "amount": "29.90"},
            statement_transaction_id="stmt-001",
        )
        sep.save_evidence(record)
        rows = sep.list_by_statement_transaction("stmt-001")
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_evidence(self, record: StructuredEvidenceRecord) -> bool:
        """Insert a single structured evidence record.

        Returns True when a new row is inserted, False when an identical row
        already exists (idempotent replay).

        Raises ``StructuredEvidenceConflictError`` when the same
        ``public_id`` exists with a different payload.
        """
        inserted = self._insert_record(record)
        self._conn.commit()
        return inserted

    def persist_evidence_batch(
        self,
        records: Sequence[StructuredEvidenceRecord],
    ) -> int:
        """Persist a batch of evidence records.

        Returns the number of newly inserted rows.  The entire batch runs
        inside a single transaction; if any record fails, the whole batch
        is rolled back.
        """
        inserted_count = 0
        try:
            with self._conn:
                for record in records:
                    if self._insert_record(record):
                        inserted_count += 1
        except sqlite3.IntegrityError as exc:
            if _is_unique_public_id_error(exc):
                raise StructuredEvidenceConflictError(
                    "Duplicate public_id in batch with conflicting data"
                ) from exc
            raise StructuredEvidencePersistenceError(str(exc)) from exc
        return inserted_count

    def _insert_record(self, record: StructuredEvidenceRecord) -> bool:
        now_created = record.created_at or _utc_now()
        now_updated = record.updated_at or now_created
        payload_json = _serialize_payload(record.evidence_payload)
        try:
            self._conn.execute(
                """
                INSERT INTO reconciliation_structured_evidence (
                    public_id, review_queue_public_id,
                    statement_transaction_id, app_transaction_id,
                    evidence_type, source_type,
                    source_id, source_path, source_page, source_row,
                    source_field, confidence_score, evidence_payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.public_id,
                    record.review_queue_public_id,
                    record.statement_transaction_id,
                    record.app_transaction_id,
                    record.evidence_type,
                    record.source_type,
                    record.source_id,
                    record.source_path,
                    record.source_page,
                    record.source_row,
                    record.source_field,
                    record.confidence_score,
                    payload_json,
                    now_created,
                    now_updated,
                ),
            )
            return True
        except sqlite3.IntegrityError as exc:
            if not _is_unique_public_id_error(exc):
                raise StructuredEvidencePersistenceError(str(exc)) from exc
            existing = self.get_by_public_id(record.public_id)
            if existing is not None and _record_matches(existing, record):
                return False
            raise StructuredEvidenceConflictError(
                f"Conflicting evidence public_id: {record.public_id}"
            ) from exc

    # ------------------------------------------------------------------
    # Read -- single
    # ------------------------------------------------------------------

    def get_by_public_id(self, public_id: str) -> dict[str, Any] | None:
        """Return a single evidence record by public_id, or None."""
        row = self._conn.execute(
            """
            SELECT * FROM reconciliation_structured_evidence
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_by_id(self, evidence_id: int) -> dict[str, Any] | None:
        """Return a single evidence record by internal id, or None."""
        row = self._conn.execute(
            "SELECT * FROM reconciliation_structured_evidence WHERE id = ?",
            (evidence_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Read -- lists
    # ------------------------------------------------------------------

    def list_by_review_queue(self, review_queue_public_id: str) -> list[dict[str, Any]]:
        """All evidence records for a given review queue item."""
        return self._query(
            "WHERE review_queue_public_id = ? ORDER BY created_at ASC, id ASC",
            (review_queue_public_id,),
        )

    def list_by_statement_transaction(self, statement_transaction_id: str) -> list[dict[str, Any]]:
        """All evidence records for a given statement transaction."""
        return self._query(
            "WHERE statement_transaction_id = ? ORDER BY created_at ASC, id ASC",
            (statement_transaction_id,),
        )

    def list_by_app_transaction(self, app_transaction_id: str) -> list[dict[str, Any]]:
        """All evidence records for a given app transaction."""
        return self._query(
            "WHERE app_transaction_id = ? ORDER BY created_at ASC, id ASC",
            (app_transaction_id,),
        )

    def list_by_evidence_type(self, evidence_type: str) -> list[dict[str, Any]]:
        """All evidence records of a given type."""
        return self._query(
            "WHERE evidence_type = ? ORDER BY created_at ASC, id ASC",
            (evidence_type,),
        )

    def list_all(self) -> list[dict[str, Any]]:
        """All evidence records, ordered by created_at then id."""
        return self._query("ORDER BY created_at ASC, id ASC")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _query(
        self,
        clause: str,
        params: tuple = (),
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"SELECT * FROM reconciliation_structured_evidence {clause}",
            params,
        ).fetchall()
        return [_row_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _is_unique_public_id_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc).lower()
    return "unique" in message and "public_id" in message


def _record_matches(row: dict[str, Any], record: StructuredEvidenceRecord) -> bool:
    """Check whether an existing row matches the input record's data."""
    payload_json = _serialize_payload(record.evidence_payload)
    # Source reference fields — nullable in the record; compare when set.
    source_fields = (
        ("source_id", record.source_id),
        ("source_path", record.source_path),
        ("source_page", record.source_page),
        ("source_row", record.source_row),
        ("source_field", record.source_field),
    )
    for col, val in source_fields:
        if val is not None and row.get(col) != val:
            return False
    # Timestamps: compare only when the record supplies them explicitly;
    # skip when empty (the adapter auto-generates them).
    if record.created_at and row.get("created_at") != record.created_at:
        return False
    if record.updated_at and row.get("updated_at") != record.updated_at:
        return False
    return (
        row.get("evidence_type") == record.evidence_type
        and row.get("source_type") == record.source_type
        and row.get("review_queue_public_id") == record.review_queue_public_id
        and row.get("statement_transaction_id") == record.statement_transaction_id
        and row.get("app_transaction_id") == record.app_transaction_id
        and row.get("confidence_score") == record.confidence_score
        and row.get("evidence_payload") == payload_json
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _serialize_payload(payload: dict[str, Any]) -> str:
    """Serialize evidence payload deterministically with Decimal-safe values."""
    canonical = _canonical_json_value(payload)
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def _canonical_json_value(value: Any) -> Any:
    """Convert supported Python values to deterministic JSON-safe values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Evidence reader -- typed read-only query layer
# ---------------------------------------------------------------------------


class EvidenceReader:
    """Read-only query layer for structured reconciliation evidence records.

    Wraps a ``StructuredEvidencePersistence`` adapter and returns typed
    :class:`EvidenceQueryResult` objects.  This reader is intentionally
    read-only and audit-safe:

    - Never mutates evidence rows, review decisions, final transactions,
      or settlement obligations.
    - Preserves raw ``evidence_payload`` text exactly as stored.
    - Handles malformed stored JSON explicitly without silent corruption.
    - Returns deterministic, stable ordering (``created_at``, then ``id``).

    Usage::

        conn = connect_sqlite(":memory:")
        conn.row_factory = sqlite3.Row
        persistence = StructuredEvidencePersistence(conn)
        reader = EvidenceReader(persistence)
        result = reader.get_by_public_id("sev-abc123")
        if result is not None:
            payload = result.parsed_payload()
    """

    def __init__(self, persistence: StructuredEvidencePersistence) -> None:
        self._persistence = persistence

    # ------------------------------------------------------------------
    # Single-record lookups
    # ------------------------------------------------------------------

    def get_by_id(self, evidence_id: int) -> EvidenceQueryResult | None:
        """Return a single evidence record by its internal integer ``id``.

        Returns ``None`` when no record matches.
        """
        raw = self._persistence.get_by_id(evidence_id)
        if raw is None:
            return None
        return EvidenceQueryResult(**dict(raw))

    def get_by_public_id(self, public_id: str) -> EvidenceQueryResult | None:
        """Return a single evidence record by ``public_id``, or ``None``."""
        raw = self._persistence.get_by_public_id(public_id)
        if raw is None:
            return None
        return EvidenceQueryResult(**raw)

    # ------------------------------------------------------------------
    # List queries -- typed, stable ordering
    # ------------------------------------------------------------------

    def list_by_review_queue(self, review_queue_public_id: str) -> list[EvidenceQueryResult]:
        return self._typed_list(self._persistence.list_by_review_queue(review_queue_public_id))

    def list_by_statement_transaction(
        self, statement_transaction_id: str
    ) -> list[EvidenceQueryResult]:
        return self._typed_list(
            self._persistence.list_by_statement_transaction(statement_transaction_id)
        )

    def list_by_app_transaction(self, app_transaction_id: str) -> list[EvidenceQueryResult]:
        return self._typed_list(self._persistence.list_by_app_transaction(app_transaction_id))

    def list_by_evidence_type(self, evidence_type: str) -> list[EvidenceQueryResult]:
        return self._typed_list(self._persistence.list_by_evidence_type(evidence_type))

    def list_all(self) -> list[EvidenceQueryResult]:
        return self._typed_list(self._persistence.list_all())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _typed_list(self, rows: list[dict[str, Any]]) -> list[EvidenceQueryResult]:
        return [EvidenceQueryResult(**r) for r in rows]


__all__ = [
    "EvidenceQueryResult",
    "EvidenceReader",
    "StructuredEvidenceConflictError",
    "StructuredEvidencePersistence",
    "StructuredEvidencePersistenceError",
    "StructuredEvidenceRecord",
    "generate_evidence_public_id",
]
