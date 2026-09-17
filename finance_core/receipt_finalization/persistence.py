"""Durable IAF fact-set binding evidence (migration 037).

One append-only row records the active IAF fact-set four-tuple
(``fact_set_public_id``, ``fact_set_version``, ``fact_set_input_hash``,
``fact_set_result_hash``) for every downstream authority record that consumed
it: the historical calculation run, the authoritative calculation snapshot, the
human finalization authorization, and the finalization audit.

Before this boundary the four-tuple was only expressible inside hash-anchored
JSON payloads and ``calc_audit_runs.source_reference`` carried the result hash
alone, so no authority record could be machine-verified against the fact set it
actually consumed.

The caller always owns the transaction: every write here belongs to the calling
service's Unit of Work so binding evidence commits or rolls back with the
authority record it describes.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from finance_core.receipt_finalization.models import ActiveFactSetBinding

BINDING_EVIDENCE_SCHEMA_VERSION = "v1"

BOUND_RECORD_COLUMNS: dict[str, str] = {
    "calculation_run": "calculation_run_id",
    "calculation_snapshot": "calculation_snapshot_public_id",
    "finalization_authorization": "finalization_authorization_id",
    "finalization_audit": "finalization_audit_id",
}


class FactSetBindingEvidenceError(RuntimeError):
    """Durable binding evidence is missing, unwritable, or contradictory."""


def derive_binding_evidence_public_id(bound_record_type: str, bound_record_public_id: str) -> str:
    """Deterministic ``rfsb_`` identity for one authority record's binding."""
    if bound_record_type not in BOUND_RECORD_COLUMNS:
        raise FactSetBindingEvidenceError(
            f"Unsupported bound record type {bound_record_type!r} for fact-set binding evidence"
        )
    if not isinstance(bound_record_public_id, str) or not bound_record_public_id.strip():
        raise FactSetBindingEvidenceError(
            "Fact-set binding evidence requires a non-empty bound record public ID"
        )
    digest = hashlib.sha256(
        f"{bound_record_type}|{bound_record_public_id}".encode("utf-8")
    ).hexdigest()
    return f"rfsb_{digest[:32]}"


def append_fact_set_binding_evidence(
    conn: sqlite3.Connection,
    *,
    binding: ActiveFactSetBinding,
    bound_record_type: str,
    bound_record_public_id: str,
    created_at: str,
) -> str:
    """Append the four-tuple binding evidence row for one authority record.

    Idempotent by deterministic identity with a complete durable field
    comparison: an existing row must describe exactly the same four-tuple,
    receipt, and authority record, otherwise the write fails closed as a
    conflict.  The caller must already own an open transaction.
    """
    column = BOUND_RECORD_COLUMNS.get(bound_record_type)
    if column is None:
        raise FactSetBindingEvidenceError(
            f"Unsupported bound record type {bound_record_type!r} for fact-set binding evidence"
        )
    binding_public_id = derive_binding_evidence_public_id(bound_record_type, bound_record_public_id)
    expected: tuple[Any, ...] = (
        binding_public_id,
        bound_record_type,
        binding.receipt_public_id,
        binding.fact_set_public_id,
        binding.fact_set_version,
        binding.fact_set_input_hash,
        binding.fact_set_result_hash,
        bound_record_public_id,
        BINDING_EVIDENCE_SCHEMA_VERSION,
    )

    existing = _fetch_binding_row(conn, binding_public_id, column)
    if existing is not None:
        if existing != expected:
            raise FactSetBindingEvidenceError(
                f"Durable fact-set binding evidence {binding_public_id!r} contradicts "
                f"this {bound_record_type} binding; refusing to reuse it"
            )
        return binding_public_id

    conn.execute(
        f"""
        INSERT INTO receipt_fact_set_binding_evidence (
            binding_public_id, bound_record_type, receipt_public_id,
            fact_set_public_id, fact_set_version, fact_set_input_hash,
            fact_set_result_hash, {column}, schema_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*expected, created_at),
    )
    return binding_public_id


def read_fact_set_binding_evidence(
    conn: sqlite3.Connection,
    *,
    bound_record_type: str,
    bound_record_public_id: str,
) -> ActiveFactSetBinding | None:
    """Return the recorded four-tuple for one authority record, if any."""
    column = BOUND_RECORD_COLUMNS.get(bound_record_type)
    if column is None:
        raise FactSetBindingEvidenceError(
            f"Unsupported bound record type {bound_record_type!r} for fact-set binding evidence"
        )
    row = conn.execute(
        f"""
        SELECT receipt_public_id, fact_set_public_id, fact_set_version,
               fact_set_input_hash, fact_set_result_hash
        FROM receipt_fact_set_binding_evidence
        WHERE bound_record_type = ? AND {column} = ?
        """,
        (bound_record_type, bound_record_public_id),
    ).fetchone()
    if row is None:
        return None
    return ActiveFactSetBinding(
        receipt_public_id=str(row[0]),
        fact_set_public_id=str(row[1]),
        fact_set_version=int(row[2]),
        fact_set_input_hash=str(row[3]),
        fact_set_result_hash=str(row[4]),
    )


def _fetch_binding_row(
    conn: sqlite3.Connection,
    binding_public_id: str,
    column: str,
) -> tuple[Any, ...] | None:
    row = conn.execute(
        f"""
        SELECT binding_public_id, bound_record_type, receipt_public_id,
               fact_set_public_id, fact_set_version, fact_set_input_hash,
               fact_set_result_hash, {column}, schema_version
        FROM receipt_fact_set_binding_evidence
        WHERE binding_public_id = ?
        """,
        (binding_public_id,),
    ).fetchone()
    if row is None:
        return None
    return (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        int(row[4]),
        str(row[5]),
        str(row[6]),
        None if row[7] is None else str(row[7]),
        str(row[8]),
    )


__all__ = [
    "BINDING_EVIDENCE_SCHEMA_VERSION",
    "BOUND_RECORD_COLUMNS",
    "FactSetBindingEvidenceError",
    "append_fact_set_binding_evidence",
    "derive_binding_evidence_public_id",
    "read_fact_set_binding_evidence",
]
