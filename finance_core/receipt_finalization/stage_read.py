"""SELECT-only receipt finalization stage projection (S4 bridge support).

Added for the S4 OpenClaw staging bridge under Owner's 2026-08-05 reader
authorization: finalization-aware ``get_status`` must reconstruct the
prepared/authorized/finalized stage of a receipt from durable truth without
any direct SQL in the bridge.  This reader is the owning-module projection
for that reconstruction.

Strictly SELECT-only: no writes, no DDL, no implicit repair, no caching,
no monetary or allocation derivation.  It never begins, commits, or rolls
back a transaction, leaves a caller-owned transaction untouched, and does
not bypass or restate any existing finalization boundary.  Absent rows are
reported as ``None`` fields; corrupted or contradictory durable truth is
surfaced exactly as stored, never repaired.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from finance_core.staging_guard import require_staging_database


@dataclass(frozen=True)
class ReceiptFinalizationStageRead:
    """Bounded durable stage projection for one receipt.

    Every field is ``None`` when the corresponding durable record does not
    exist.  ``authorization_state`` echoes the stored lifecycle state
    verbatim (for example ``authorized`` or ``consumed``); this reader never
    interprets or mutates it.
    """

    receipt_public_id: str
    authorization_id: str | None
    authorization_state: str | None
    calculation_run_public_id: str | None
    calculation_snapshot_id: str | None
    calculation_snapshot_hash: str | None
    finalization_public_id: str | None
    finalization_status: str | None
    transaction_public_id: str | None


def read_receipt_finalization_stage(
    conn: sqlite3.Connection,
    *,
    receipt_public_id: str,
) -> ReceiptFinalizationStageRead:
    """Return the durable finalization stage projection for one receipt.

    The lookup keys are the deterministic identities the owning module
    itself derives for one receipt: the receipt group ``rgrp_<receipt>``,
    the authorization row bound to that group, the authoritative snapshot
    bound to the authorization (or to the group aggregate before any
    authorization exists), and the finalization audit bound to the
    authorization.  Nothing here derives monetary values or allocation
    semantics.
    """
    require_staging_database(conn)
    if (
        not isinstance(receipt_public_id, str)
        or not receipt_public_id
        or not receipt_public_id.strip()
    ):
        raise ValueError("receipt_public_id must be a non-empty string")

    receipt_group_public_id = f"rgrp_{receipt_public_id}"

    auth_row = conn.execute(
        "SELECT authorization_id, authorization_state, calculation_run_public_id, "
        "calculation_snapshot_id FROM receipt_finalization_authorizations "
        "WHERE receipt_group_public_id = ? "
        "ORDER BY created_at DESC, authorization_id DESC LIMIT 1",
        (receipt_group_public_id,),
    ).fetchone()

    if auth_row is None:
        snapshot_row = conn.execute(
            "SELECT snapshot_public_id, combined_snapshot_hash "
            "FROM authoritative_calculation_snapshots WHERE aggregate_public_id = ? "
            "ORDER BY created_at DESC, snapshot_public_id DESC LIMIT 1",
            (receipt_group_public_id,),
        ).fetchone()
        return ReceiptFinalizationStageRead(
            receipt_public_id=receipt_public_id,
            authorization_id=None,
            authorization_state=None,
            calculation_run_public_id=None,
            calculation_snapshot_id=(None if snapshot_row is None else str(snapshot_row[0])),
            calculation_snapshot_hash=(None if snapshot_row is None else str(snapshot_row[1])),
            finalization_public_id=None,
            finalization_status=None,
            transaction_public_id=None,
        )

    authorization_id = str(auth_row[0])
    snapshot_id = str(auth_row[3])
    snapshot_hash_row = conn.execute(
        "SELECT combined_snapshot_hash FROM authoritative_calculation_snapshots "
        "WHERE snapshot_public_id = ?",
        (snapshot_id,),
    ).fetchone()
    audit_row = conn.execute(
        "SELECT finalization_id, status, transaction_public_id "
        "FROM receipt_finalization_audit WHERE authorization_id = ? "
        "ORDER BY created_at DESC, finalization_id DESC LIMIT 1",
        (authorization_id,),
    ).fetchone()

    return ReceiptFinalizationStageRead(
        receipt_public_id=receipt_public_id,
        authorization_id=authorization_id,
        authorization_state=str(auth_row[1]),
        calculation_run_public_id=str(auth_row[2]),
        calculation_snapshot_id=snapshot_id,
        calculation_snapshot_hash=(
            None if snapshot_hash_row is None else str(snapshot_hash_row[0])
        ),
        finalization_public_id=None if audit_row is None else str(audit_row[0]),
        finalization_status=None if audit_row is None else str(audit_row[1]),
        transaction_public_id=(
            None if audit_row is None or audit_row[2] is None else str(audit_row[2])
        ),
    )


@dataclass(frozen=True)
class ReceiptConversionRegistryRead:
    """Bounded projection of one durable receipt conversion registry row.

    Identity and binding hashes only; no amounts and no allocation
    semantics.  ``receipt_public_id`` is ``None`` only if the joined
    receipt row is destroyed — durable truth is surfaced as stored, never
    repaired.
    """

    command_public_id: str
    parser_output_id: int
    receipt_id: int
    receipt_public_id: str | None
    proposal_content_hash: str
    conversion_result_hash: str
    authenticated_actor_id: str


def read_receipt_conversion_registry(
    conn: sqlite3.Connection,
    *,
    command_public_id: str,
) -> ReceiptConversionRegistryRead | None:
    """Return the durable conversion registry row for one command identity.

    ``finalize`` replays use this SELECT-only projection instead of
    re-invoking the conversion boundary after a fact set exists: the
    conversion boundary's replay integrity check is strictly valid only
    before any guarded fact-set persistence on the receipt aggregate, so
    later stages reconstruct from durable truth (mirroring the B5.1 runner,
    whose finalize stage never re-runs conversion).
    """
    require_staging_database(conn)
    if (
        not isinstance(command_public_id, str)
        or not command_public_id
        or not command_public_id.strip()
    ):
        raise ValueError("command_public_id must be a non-empty string")
    row = conn.execute(
        "SELECT rpc.command_public_id, rpc.parser_output_id, rpc.receipt_id, "
        "rpc.proposal_content_hash, rpc.conversion_result_hash, "
        "rpc.authenticated_actor_id, r.public_id "
        "FROM receipt_proposal_conversions AS rpc "
        "LEFT JOIN receipts AS r ON r.id = rpc.receipt_id "
        "WHERE rpc.command_public_id = ?",
        (command_public_id,),
    ).fetchone()
    if row is None:
        return None
    return ReceiptConversionRegistryRead(
        command_public_id=str(row[0]),
        parser_output_id=int(row[1]),
        receipt_id=int(row[2]),
        receipt_public_id=None if row[6] is None else str(row[6]),
        proposal_content_hash=str(row[3]),
        conversion_result_hash=str(row[4]),
        authenticated_actor_id=str(row[5]),
    )


__all__ = [
    "ReceiptConversionRegistryRead",
    "ReceiptFinalizationStageRead",
    "read_receipt_conversion_registry",
    "read_receipt_finalization_stage",
]
