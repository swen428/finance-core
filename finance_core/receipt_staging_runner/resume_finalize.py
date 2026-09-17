"""B5.1c staging runner resume and guarded finalize orchestration.

A B5.1b receipt that is already prepared and durably authorized is safely
resumed by a new process, reconstructed from durable truth, rechecked, and
finalized exactly once through the existing guarded receipt-finalization
boundary.

Contracts
---------
- ``resume_runner_run`` is strictly read-only: it recovers the workspace and
  staging database, rehydrates the persisted authorization with
  ``load_persisted_receipt_finalization_authorization``, verifies the manifest
  operator and local-receipt lineage, and returns a versioned run manifest
  built solely from durable truth.
- ``finalize_runner_run`` performs the same reconstruction and recheck, then
  re-checks every finalization guard immediately before the guarded
  finalization and invokes ``finalize_prepared_receipt`` exactly once.
- The finalize path accepts only stable identities (workspace, manifest,
  authorization ID, validated operator identity).  It never accepts
  caller-supplied amounts, currency, proposal payloads, fact-set payloads,
  calculation snapshots, or settlement obligations.
- Rejected/stale/corrupt durable truth fails closed with a typed bounded error
  and no partial financial facts.
- Exact replay after success returns the same canonical result with
  ``already_finalized`` semantics and creates no duplicate transaction,
  settlement, audit, or authorization records.

This module never accesses ``database/finance.db`` and never weakens the B5.1a
``recover`` command's read-only "zero final facts" contract.

See ``docs/design/b5_1c_resume_and_finalize_v1.md``.
"""

from __future__ import annotations

import sqlite3

from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeRecoveryError,
    ReceiptFinalizationAuthorization,
    finalize_prepared_receipt,
    load_persisted_receipt_finalization_authorization,
)
from finance_core.receipt_finalization.models import (
    FinalizationOutput,
)
from finance_core.receipt_staging_runner.local_intake import (
    require_local_runner_receipt,
)
from finance_core.receipt_staging_runner.models import (
    RUN_MANIFEST_SCHEMA_VERSION,
    RunManifest,
    RunnerFinalizeError,
    RunnerInputManifest,
    RunnerResumeError,
    RunnerWorkspace,
)
from finance_core.receipt_staging_runner.participants import derive_bootstrap_hash
from finance_core.receipt_staging_runner.workspace import recover_runner_workspace
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import open_staging_database

# Canonical financial fact tables that a successful guarded finalization may
# populate.  ``canonical_financial_facts_created`` counts the complete set, so
# the run manifest reflects the full durable financial state a finalization
# created.  (The B5.1a ``recover`` command keeps its own narrower three-table
# zero-facts contract in ``recovery.py``.)
_CANONICAL_FACT_TABLES = (
    "transactions",
    "settlement_obligations",
    "receipt_finalization_audit",
    "calculation_runs",
    "calculation_participant_shares",
    "receipt_groups",
    "receipt_group_receipts",
)

# Bounded block reason used when the guarded finalizer refuses with an error
# that carries no stable reason code of its own.
_UNKNOWN_REFUSAL_REASON = "runner_finalization_refused"
# Bounded retryable-refusal reason used when SQLite reports write-lock
# contention: a contender may retry after the lock is released and must never
# report unverified success.
_RETRYABLE_LOCK_REFUSAL_REASON = "runner_finalization_locked"


# ---------------------------------------------------------------------------
# Shared reconstruction (read-only)
# ---------------------------------------------------------------------------


def _open_recovered_workspace(
    workspace_path: str,
    manifest: RunnerInputManifest,
) -> tuple[RunnerWorkspace, sqlite3.Connection]:
    """Recover the workspace and reopen the staging database.

    Raises
    ------
    RunnerResumeError
        If the workspace or staging database cannot be recovered.
    """
    try:
        workspace = recover_runner_workspace(workspace_path, manifest)
    except Exception as exc:
        raise RunnerResumeError(f"Workspace recovery failed: {exc}") from exc

    conn: sqlite3.Connection | None = None
    try:
        conn = open_staging_database(
            workspace.database_path,
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
    except Exception as exc:
        raise RunnerResumeError(f"Staging database reopen failed: {exc}") from exc
    return workspace, conn


def _rehydrate_authorization(
    conn: sqlite3.Connection,
    authorization_id: str,
) -> ReceiptFinalizationAuthorization:
    """Rehydrate the persisted authorization from durable truth (read-only).

    Every reconstruction failure -- including malformed durable JSON, which
    the recovery boundary can raise as a raw ``json.JSONDecodeError`` -- fails
    closed as a typed ``RunnerResumeError`` with zero writes.  A raw exception
    must never escape the public resume/finalize boundaries.
    """
    try:
        return load_persisted_receipt_finalization_authorization(conn, authorization_id)
    except BridgeRecoveryError as exc:
        raise RunnerResumeError(f"Authorization recovery failed: {exc}") from exc
    except Exception as exc:
        raise RunnerResumeError(
            f"Authorization reconstruction failed from durable truth: {exc}"
        ) from exc


def _verify_operator_and_lineage(
    conn: sqlite3.Connection,
    *,
    workspace: RunnerWorkspace,
    manifest: RunnerInputManifest,
    authorization: ReceiptFinalizationAuthorization,
) -> None:
    """Verify the manifest operator and the local-receipt lineage.

    The authorization's bound receipt must trace to a valid B5.1b local-file
    conversion in this exact workspace, and the validated operator must equal
    the manifest operator.
    """
    receipt_public_id = authorization.prepared.active_fact_set_binding.receipt_public_id
    try:
        require_local_runner_receipt(
            conn, receipt_public_id, workspace=workspace, manifest=manifest
        )
    except Exception as exc:
        raise RunnerResumeError(
            f"Local receipt lineage verification failed for {receipt_public_id!r}: {exc}"
        ) from exc

    # The manifest operator is validated by the CLI command handler before this
    # boundary; re-assert it here so the public boundary is self-contained.
    if authorization.actor_id != manifest.operator_actor_id:
        raise RunnerResumeError(
            f"Authorization actor {authorization.actor_id!r} does not match manifest "
            f"operator {manifest.operator_actor_id!r}"
        )


def _migration_ledger(conn: sqlite3.Connection) -> tuple[int, str]:
    """Return (migration ledger count, latest migration id)."""
    ledger_count = int(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0])
    latest_row = conn.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_sequence DESC LIMIT 1"
    ).fetchone()
    latest = str(latest_row[0]) if latest_row else ""
    return ledger_count, latest


def _canonical_facts_created(conn: sqlite3.Connection) -> int:
    """Return the count of canonical financial fact rows in the staging DB."""
    total = 0
    for table in _CANONICAL_FACT_TABLES:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        total += int(row[0])
    return total


def _read_finalization_result(
    conn: sqlite3.Connection,
    authorization_id: str,
) -> dict[str, object]:
    """Read the durable finalization result for an authorization, if present.

    Returns a bounded dict with finalization/audit/transaction identities, or
    an empty dict when the authorization has not yet been finalized.
    """
    row = conn.execute(
        "SELECT finalization_id, transaction_public_id, idempotency_key, status "
        "FROM receipt_finalization_audit WHERE authorization_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (authorization_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "finalization_public_id": str(row["finalization_id"]),
        "transaction_public_id": (
            None if row["transaction_public_id"] is None else str(row["transaction_public_id"])
        ),
        "status": str(row["status"]),
    }


# ---------------------------------------------------------------------------
# Run manifest reconstruction (solely from durable truth)
# ---------------------------------------------------------------------------


def _build_run_manifest(
    conn: sqlite3.Connection,
    *,
    workspace: RunnerWorkspace,
    manifest: RunnerInputManifest,
    authorization: ReceiptFinalizationAuthorization,
) -> RunManifest:
    """Build the bounded, versioned run manifest from durable truth only."""
    ledger_count, latest_migration = _migration_ledger(conn)
    bootstrap_hash = derive_bootstrap_hash(manifest.participants, manifest.manifest_sha256)
    prepared = authorization.prepared
    binding = prepared.active_fact_set_binding

    facts_created = _canonical_facts_created(conn)
    result = _read_finalization_result(conn, authorization.authorization_id)

    return RunManifest(
        report_schema_version=RUN_MANIFEST_SCHEMA_VERSION,
        workspace_identity=workspace.workspace_identity,
        workspace_path=workspace.workspace_path,
        manifest_hash=manifest.manifest_sha256,
        operator_actor_id=manifest.operator_actor_id,
        database_identity=workspace.database_path,
        migration_ledger_count=ledger_count,
        latest_migration=latest_migration,
        migration_verification="passed",
        participant_bootstrap_hash=bootstrap_hash,
        authorization_id=authorization.authorization_id,
        authorization_state=_read_auth_state(conn, authorization.authorization_id),
        confirmation_id=authorization.confirmation_id,
        content_hash=authorization.content_hash,
        receipt_public_id=prepared.receipt_public_id,
        receipt_group_public_id=prepared.receipt_group_public_id,
        calculation_run_public_id=prepared.calculation_run_public_id,
        calculation_snapshot_id=prepared.calculation_snapshot_id,
        calculation_snapshot_hash=prepared.calculation_snapshot_hash,
        currency_contract_version=prepared.currency_contract_version,
        fact_set_public_id=binding.fact_set_public_id,
        fact_set_version=binding.fact_set_version,
        fact_set_input_hash=binding.fact_set_input_hash,
        fact_set_result_hash=binding.fact_set_result_hash,
        source_evidence_refs=prepared.source_evidence_refs,
        canonical_financial_facts_created=facts_created,
        finalization_executed=bool(result),
        finalization_public_id=str(result["finalization_public_id"]) if result else None,
        transaction_public_id=str(result["transaction_public_id"]) if result else None,
        audit_id=str(result["finalization_public_id"]) if result else None,
    )


def _read_auth_state(conn: sqlite3.Connection, authorization_id: str) -> str:
    """Read the current authorization_state for reporting."""
    row = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    return str(row[0]) if row else "unknown"


# ---------------------------------------------------------------------------
# Public boundary: resume (read-only)
# ---------------------------------------------------------------------------


def resume_runner_run(
    workspace_path: str,
    manifest: RunnerInputManifest,
    *,
    authorization_id: str,
) -> RunManifest:
    """Reconstruct the run manifest from durable truth (strictly read-only).

    Recovers the workspace and staging database, rehydrates the persisted
    authorization, verifies the manifest operator and local-receipt lineage,
    and returns the versioned run manifest.

    Raises
    ------
    RunnerResumeError
        If any reconstruction or verification step fails.  Never writes.
    """
    if not authorization_id or not authorization_id.strip():
        raise RunnerResumeError("authorization_id must be a non-empty string")

    workspace, conn = _open_recovered_workspace(workspace_path, manifest)
    try:
        try:
            authorization = _rehydrate_authorization(conn, authorization_id)
            _verify_operator_and_lineage(
                conn, workspace=workspace, manifest=manifest, authorization=authorization
            )
            return _build_run_manifest(
                conn, workspace=workspace, manifest=manifest, authorization=authorization
            )
        except RunnerResumeError:
            raise
        except Exception as exc:
            raise RunnerResumeError(f"Run manifest reconstruction failed: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public boundary: finalize (guarded, exactly-once through the finalizer)
# ---------------------------------------------------------------------------


def finalize_runner_run(
    workspace_path: str,
    manifest: RunnerInputManifest,
    *,
    authorization_id: str,
) -> tuple[RunManifest, FinalizationOutput]:
    """Resume a durably authorized receipt and finalize it exactly once.

    Performs the same read-only reconstruction as :func:`resume_runner_run`,
    then re-checks every finalization guard immediately before the guarded
    finalization and invokes ``finalize_prepared_receipt`` exactly once.

    On success the durable result (or its ``already_finalized`` replay) is
    re-read and a fresh run manifest is returned alongside the
    ``FinalizationOutput``.

    Raises
    ------
    RunnerFinalizeError
        If the guarded finalization refuses the operation with zero facts, or
        any reconstruction/re-check step fails during the finalize attempt.
    """
    if not authorization_id or not authorization_id.strip():
        raise RunnerFinalizeError("authorization_id must be a non-empty string")

    try:
        workspace, conn = _open_recovered_workspace(workspace_path, manifest)
    except RunnerResumeError as exc:
        raise RunnerFinalizeError(f"Finalize workspace recovery failed: {exc}") from exc

    try:
        try:
            authorization = _rehydrate_authorization(conn, authorization_id)
            _verify_operator_and_lineage(
                conn, workspace=workspace, manifest=manifest, authorization=authorization
            )
            # Re-check the live authorization state immediately before the
            # guarded finalization.  A prior authorization or in-memory object
            # is never sufficient authority; the durable state is re-read here.
            _require_authorized_state(conn, authorization_id)
        except RunnerResumeError as exc:
            raise RunnerFinalizeError(
                f"Finalize authorization re-check failed: {exc}",
                reason="runner_authorization_recheck_failed",
            ) from exc
        except Exception as exc:
            # A raw durable-truth failure (e.g. a sqlite error on the
            # authorizations re-check) must never escape the finalize boundary.
            # Lock contention at the re-check read is a retryable refusal.
            reason = (
                _RETRYABLE_LOCK_REFUSAL_REASON
                if _is_sqlite_busy(exc)
                else "runner_authorization_recheck_failed"
            )
            raise RunnerFinalizeError(
                f"Finalize authorization re-check failed: {exc}",
                reason=reason,
            ) from exc

        try:
            output = finalize_prepared_receipt(conn, authorization)
        except Exception as exc:
            refusal_reason: str | None = getattr(exc, "reason", None)
            if refusal_reason is None:
                # SQLite write-lock contention is a retryable refusal, never
                # unverified success: a contender may retry after the lock is
                # released and must then observe the one durable result.
                if _is_sqlite_busy(exc):
                    raise RunnerFinalizeError(
                        f"Guarded finalization is locked for authorization "
                        f"{authorization_id!r}: {exc}",
                        reason=_RETRYABLE_LOCK_REFUSAL_REASON,
                    ) from exc
                refusal_reason = _UNKNOWN_REFUSAL_REASON
            raise RunnerFinalizeError(
                f"Guarded finalization refused for authorization {authorization_id!r}: {exc}",
                reason=refusal_reason,
            ) from exc

        try:
            manifest_report = _build_run_manifest(
                conn, workspace=workspace, manifest=manifest, authorization=authorization
            )
        except Exception as exc:
            raise RunnerFinalizeError(
                f"Run manifest reconstruction failed after finalization: {exc}"
            ) from exc
        return manifest_report, output
    finally:
        conn.close()


def _is_sqlite_busy(exc: Exception) -> bool:
    """Detect SQLite write-lock contention on an arbitrary exception chain."""
    import sqlite3

    cursor: BaseException | None = exc
    while cursor is not None:
        if isinstance(cursor, sqlite3.OperationalError):
            if "database is locked" in str(cursor) or "database is busy" in str(cursor):
                return True
        cursor = cursor.__cause__
    return False


def _require_authorized_state(conn: sqlite3.Connection, authorization_id: str) -> None:
    """Fail closed unless the durable authorization is still 'authorized'.

    A replay after a successful finalization observes the authorization as
    'consumed'; that is the expected durable state of a completed run and the
    finalizer's own idempotent replay path (checked inside the guarded
    boundary) returns the canonical result.  This pre-check therefore only
    rejects an authorization that is neither 'authorized' nor 'consumed'.
    """
    row = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    if row is None:
        raise RunnerResumeError(f"Authorization {authorization_id!r} not found")
    state = str(row[0])
    if state not in ("authorized", "consumed"):
        raise RunnerResumeError(
            f"Authorization {authorization_id!r} is in state {state!r}; "
            "only 'authorized' or 'consumed' can be finalized"
        )


__all__ = [
    "RunnerFinalizeError",
    "RunnerResumeError",
    "RunManifest",
    "finalize_runner_run",
    "resume_runner_run",
]
