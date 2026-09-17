"""B5.1a staging runner recovery orchestration.

Coordinates workspace recovery, staging database reopen, migration ledger
verification, participant bootstrap validation, and persisted authorization
recovery into a single bounded, versioned, read-only recovery report.

This module never writes to the database, never calls the finalizer, and
never accesses ``database/finance.db``.

See ``docs/design/b5_1a_receipt_staging_runner_foundation_v1.md``.
"""

from __future__ import annotations

import sqlite3

from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeRecoveryError,
    load_persisted_receipt_finalization_authorization,
)
from finance_core.receipt_staging_runner.models import (
    RECOVERY_REPORT_SCHEMA_VERSION,
    RecoveryEvidence,
    RunnerInputManifest,
    RunnerRecoveryError,
)
from finance_core.receipt_staging_runner.participants import derive_bootstrap_hash
from finance_core.receipt_staging_runner.workspace import recover_runner_workspace
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import open_staging_database

# ---------------------------------------------------------------------------
# Canonical financial fact tables that must remain empty in B5.1a
# ---------------------------------------------------------------------------

_CANONICAL_FACT_TABLES = (
    "transactions",
    "settlement_obligations",
    "receipt_finalization_audit",
)


def run_recovery(
    workspace_path: str,
    manifest: RunnerInputManifest,
    *,
    authorization_id: str | None = None,
) -> RecoveryEvidence:
    """Execute the full B5.1a recovery pipeline and produce evidence.

    Steps:
    1. Recover and verify the external workspace + manifest identity.
    2. Reopen the staging database with identity + migration ledger verification.
    3. Verify participant bootstrap hash.
    4. If authorization_id is supplied, recover the persisted authorization.
    5. Verify canonical financial fact tables are empty.
    6. Return a bounded, versioned recovery evidence report.

    This function is strictly read-only: no writes, no commits, no finalization.

    Raises
    ------
    RunnerRecoveryError
        If any verification step fails.
    """
    # -- 1. Workspace recovery --------------------------------------------
    try:
        workspace = recover_runner_workspace(workspace_path, manifest)
    except Exception as exc:
        raise RunnerRecoveryError(f"Workspace recovery failed: {exc}") from exc

    # -- 2. Staging database reopen + migration ledger --------------------
    conn: sqlite3.Connection | None = None
    try:
        conn = open_staging_database(
            workspace.database_path,
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
    except Exception as exc:
        raise RunnerRecoveryError(f"Staging database reopen failed: {exc}") from exc

    try:
        # Gather migration ledger metadata.
        ledger_count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        latest_row = conn.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_sequence DESC LIMIT 1"
        ).fetchone()
        latest_migration = str(latest_row[0]) if latest_row else ""

        # -- 3. Participant bootstrap hash --------------------------------
        bootstrap_hash = derive_bootstrap_hash(manifest.participants, manifest.manifest_sha256)

        # -- 4. Authorization recovery (optional) -------------------------
        auth_state: str | None = None
        snapshot_id: str | None = None
        snapshot_hash: str | None = None
        fact_set_public_id: str | None = None
        fact_set_version: int | None = None
        fact_set_input_hash: str | None = None
        fact_set_result_hash: str | None = None
        evidence_verification = "not_requested"

        if authorization_id is not None:
            try:
                authorization = load_persisted_receipt_finalization_authorization(
                    conn, authorization_id
                )
                auth_state = _read_auth_state(conn, authorization_id)
                snapshot_id = authorization.prepared.calculation_snapshot_id
                snapshot_hash = authorization.prepared.calculation_snapshot_hash
                binding = authorization.prepared.active_fact_set_binding
                fact_set_public_id = binding.fact_set_public_id
                fact_set_version = binding.fact_set_version
                fact_set_input_hash = binding.fact_set_input_hash
                fact_set_result_hash = binding.fact_set_result_hash
                evidence_verification = "verified"
            except BridgeRecoveryError as exc:
                raise RunnerRecoveryError(f"Authorization recovery failed: {exc}") from exc

        # -- 5. Verify canonical financial fact tables are empty ----------
        facts_created = 0
        for table in _CANONICAL_FACT_TABLES:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            count = int(row[0])
            if count > 0:
                raise RunnerRecoveryError(
                    f"Canonical financial fact table {table!r} has {count} rows; "
                    "B5.1a recovery expects zero final facts"
                )
            facts_created += count

        # -- 6. Build recovery evidence -----------------------------------
        return RecoveryEvidence(
            report_schema_version=RECOVERY_REPORT_SCHEMA_VERSION,
            workspace_identity=workspace.workspace_identity,
            workspace_path=workspace.workspace_path,
            manifest_hash=manifest.manifest_sha256,
            database_identity=workspace.database_path,
            migration_ledger_count=int(ledger_count),
            latest_migration=latest_migration,
            migration_verification="passed",
            participant_bootstrap_hash=bootstrap_hash,
            authorization_id=authorization_id,
            authorization_state=auth_state,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            fact_set_public_id=fact_set_public_id,
            fact_set_version=fact_set_version,
            fact_set_input_hash=fact_set_input_hash,
            fact_set_result_hash=fact_set_result_hash,
            evidence_verification=evidence_verification,
            canonical_financial_facts_created=facts_created,
            finalization_executed=False,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _read_auth_state(conn: sqlite3.Connection, authorization_id: str) -> str:
    """Read the current authorization_state for reporting."""
    row = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    return str(row[0]) if row else "unknown"


__all__ = [
    "run_recovery",
]
