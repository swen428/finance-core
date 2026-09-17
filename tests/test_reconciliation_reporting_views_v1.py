"""Tests for Reconciliation Reporting Views v1.

Covers the consolidated reporting-bucket query in
``database/queries/reconciliation/reconciliation_reporting_views_v1.sql``:

  1. The query executes without error on a migrated temp DB.
  2. ``reconciled`` bucket -- matched and (where present) applied rows.
  3. ``unmatched_statement`` bucket -- ``no_match`` rows.
  4. ``amount_mismatch`` bucket -- mismatch statuses.
  5. ``needs_review`` bucket -- pending review queue.
  6. ``blocked`` bucket -- guard decision blocked, and guarded execution blocked.
  7. ``apply_partially_blocked`` bucket -- partial guarded execution.
  8. ``apply_failed`` bucket -- apply result success=0 and exec conflict.
  9. ``pending_apply`` bucket -- resolved but no apply result.
 10. Buckets are not conflated (blocked != needs_review != apply_failed).
 11. Deterministic ordering across runs.
 12. The query is read-only (no source table mutation).
 13. The query does not touch ``database/finance.db``.
 14. The SQL file contains no INSERT/UPDATE/DELETE/CREATE/ALTER/DROP.
"""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from finance_core.reconciliation.models import (
    InternalCandidate,
    MatchEvidence,
    MatchResult,
    MatchStatus,
    ReasonCode,
)
from finance_core.reconciliation.repository import ReconciliationRepository

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"
REPORTING_SQL = (
    REPO_ROOT / "database" / "queries" / "reconciliation" / "reconciliation_reporting_views_v1.sql"
)

WRITE_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE|ATTACH|DETACH|PRAGMA)\b",
    re.IGNORECASE,
)


def _read_sql() -> str:
    return REPORTING_SQL.read_text(encoding="utf-8")


def _run(conn: sqlite3.Connection, where: str | None = None) -> list[sqlite3.Row]:
    sql = _read_sql()
    if where:
        sql = f"SELECT * FROM ({sql}) WHERE {where}"
    return conn.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_batch(conn: sqlite3.Connection, public_id: str = "batch-rv1") -> int:
    conn.execute(
        """
        INSERT INTO statement_import_batches (public_id, source_type, currency)
        VALUES (?, 'bank_statement', 'SGD')
        """,
        (public_id,),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_import_batches WHERE public_id = ?", (public_id,)
    ).fetchone()["id"]


def _seed_stmt(
    conn: sqlite3.Connection,
    public_id: str,
    batch_id: int,
    *,
    merchant_raw: str = "Apple",
    amount: Decimal = Decimal("29.90"),
    currency: str = "SGD",
    transaction_date: str | None = "2024-12-01",
) -> int:
    conn.execute(
        """
        INSERT INTO statement_transactions (
          public_id, batch_id, merchant_raw, amount, currency, transaction_date
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (public_id, batch_id, merchant_raw, str(amount), currency, transaction_date),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_transactions WHERE public_id = ?", (public_id,)
    ).fetchone()["id"]


def _make_repo(conn: sqlite3.Connection) -> ReconciliationRepository:
    return ReconciliationRepository(conn)


def _matched_result() -> MatchResult:
    return MatchResult(
        status=MatchStatus.MATCHED,
        reasons=(ReasonCode.EXACT_AMOUNT_MATCH,),
        evidence=MatchEvidence(
            statement_amount=Decimal("29.90"),
            candidate_amount=Decimal("29.90"),
            merchant_similarity=1.0,
            candidate_count=1,
        ),
        best_candidate=InternalCandidate(
            internal_id="cand-001",
            transaction_date=__import__("datetime").date(2024, 12, 1),
            merchant="Apple",
            amount=Decimal("29.90"),
            currency="SGD",
        ),
    )


def _mismatch_result() -> MatchResult:
    return MatchResult(
        status=MatchStatus.AMOUNT_MISMATCH,
        reasons=(ReasonCode.AMOUNT_DIFFERS,),
        evidence=MatchEvidence(
            statement_amount=Decimal("29.90"),
            candidate_amount=Decimal("19.90"),
            merchant_similarity=1.0,
            candidate_count=1,
        ),
    )


def _no_match_result() -> MatchResult:
    return MatchResult(
        status=MatchStatus.NO_MATCH,
        reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
        evidence=MatchEvidence(candidate_count=0),
    )


def _seed_review_queue(
    conn: sqlite3.Connection,
    *,
    queue_public_id: str,
    candidate_id: str,
    statement_transaction_ref: str,
    app_transaction_ref: str | None = None,
    issue_type: str = "amount_mismatch",
    status: str = "pending",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_review_queue (
            public_id, run_public_id, candidate_id, issue_type,
            suggested_action, priority, statement_transaction_ref,
            app_transaction_ref, confidence_score,
            reason_codes_json, evidence_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            queue_public_id,
            "run-rv1",
            candidate_id,
            issue_type,
            "review_manual",
            1,
            statement_transaction_ref,
            app_transaction_ref,
            "0.5",
            "[]",
            "{}",
            status,
        ),
    )
    conn.commit()


def _seed_decision(
    conn: sqlite3.Connection,
    *,
    decision_public_id: str,
    review_queue_public_id: str,
    decision_action: str = "confirm_match",
    resolved_at: str | None = "2024-12-02T00:00:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_resolution_decisions (
            public_id, review_queue_public_id, decision_action,
            decision_note, reviewer, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            decision_public_id,
            review_queue_public_id,
            decision_action,
            None,
            "test-reviewer",
            resolved_at,
        ),
    )
    conn.commit()


def _seed_apply_result(
    conn: sqlite3.Connection,
    *,
    apply_id: str,
    decision_id: str,
    queue_item_id: str,
    action: str = "confirm_match",
    success: int = 1,
    idempotent: int = 1,
    fingerprint: str = "fp-1",
    applied_at: str = "2024-12-02T00:00:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_apply_results (
            apply_id, decision_id, queue_item_id, candidate_id,
            action, success, idempotent, payload_json,
            audit_evidence_json, reviewer, note, fingerprint, applied_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, '{}', '{}', NULL, NULL, ?, ?)
        """,
        (
            apply_id,
            decision_id,
            queue_item_id,
            action,
            success,
            idempotent,
            fingerprint,
            applied_at,
        ),
    )
    conn.commit()


def _seed_guarded_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    plan_id: str,
    idempotency_key: str,
    execution_fingerprint: str,
    execution_status: str,
    total_operations: int,
    operations_executed: int,
    operations_blocked: int,
    operations_skipped: int,
    executed_at: str = "2024-12-02T00:00:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_guarded_apply_executions (
            execution_id, plan_id, idempotency_key, execution_fingerprint,
            execution_status, total_operations, operations_executed,
            operations_blocked, operations_skipped, block_reason,
            guard_decision_refs_json, audit_trail_json, is_dry_run, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '[]', '[]', 0, ?)
        """,
        (
            execution_id,
            plan_id,
            idempotency_key,
            execution_fingerprint,
            execution_status,
            total_operations,
            operations_executed,
            operations_blocked,
            operations_skipped,
            executed_at,
        ),
    )
    conn.commit()


def _seed_operation_result(
    conn: sqlite3.Connection,
    *,
    operation_result_id: str,
    execution_id: str,
    operation_id: str,
    decision_id: str,
    execution_status: str,
    mutation_type: str = "link",
    guard_decision_approved: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_guarded_apply_operation_results (
            operation_result_id, execution_id, operation_id, decision_id,
            execution_status, reason, guard_decision_approved,
            guard_decision_idempotency_key, guard_blocked_reasons_json,
            mutation_type, mutation_payload_json
        ) VALUES (?, ?, ?, ?, ?, '', ?, NULL, '[]', ?, '{}')
        """,
        (
            operation_result_id,
            execution_id,
            operation_id,
            decision_id,
            execution_status,
            guard_decision_approved,
            mutation_type,
        ),
    )
    conn.commit()


def _seed_guard_decision(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    idempotency_key: str,
    source_statement_ref: str,
    approved: int,
    action: str = "blocked",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_final_mutation_guard_decisions (
            proposal_id, idempotency_key, action, approved,
            blocked_reasons_json, preview_json, evidence_refs_json,
            source_statement_ref, guard_version
        ) VALUES (?, ?, ?, ?, '[]', NULL, '[]', ?, 'v1')
        """,
        (proposal_id, idempotency_key, action, approved, source_statement_ref),
    )
    conn.commit()


def _seed_run(
    conn: sqlite3.Connection,
    public_id: str = "run-rv1",
) -> tuple[int, int]:
    repo = _make_repo(conn)
    batch_id = _seed_batch(conn)
    repo.create_reconciliation_run(public_id=public_id)
    run_id = conn.execute(
        "SELECT id FROM reconciliation_runs WHERE public_id = ?", (public_id,)
    ).fetchone()["id"]
    return batch_id, run_id


# ---------------------------------------------------------------------------
# 1. query executes
# ---------------------------------------------------------------------------


def test_query_executes_on_empty(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    rows = _run(migrated_temp_db_connection)
    assert rows == []


# ---------------------------------------------------------------------------
# 2. reconciled
# ---------------------------------------------------------------------------


def test_reconciled_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    stmt_id = _seed_stmt(conn, "stmt-recon", batch_id)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["reconciled"]
    assert rows[0]["reporting_reason"] == "matched by matcher"


# ---------------------------------------------------------------------------
# 3. unmatched_statement
# ---------------------------------------------------------------------------


def test_unmatched_statement_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    stmt_id = _seed_stmt(conn, "stmt-unmatched", batch_id)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_no_match_result(),
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["unmatched_statement"]


# ---------------------------------------------------------------------------
# 4. amount_mismatch
# ---------------------------------------------------------------------------


def test_amount_mismatch_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    stmt_id = _seed_stmt(conn, "stmt-mismatch", batch_id)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_mismatch_result(),
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["amount_mismatch"]


# ---------------------------------------------------------------------------
# 5. needs_review (pending review queue)
# ---------------------------------------------------------------------------


def test_needs_review_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "AppleNR"
    stmt_id = _seed_stmt(conn, "stmt-needs-review", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_mismatch_result(),
    )
    # A pending review queue item should promote from amount_mismatch to
    # needs_review, because review_status='pending' wins.
    _seed_review_queue(
        conn,
        queue_public_id="rq-nr",
        candidate_id="cand-nr",
        statement_transaction_ref=merchant,
        status="pending",
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["needs_review"]
    assert rows[0]["review_status"] == "pending"


# ---------------------------------------------------------------------------
# 6. blocked -- guard decision blocked, and guarded execution blocked
# ---------------------------------------------------------------------------


def test_blocked_via_guard_decision(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "AppleBlock"
    stmt_id = _seed_stmt(conn, "stmt-block-g", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    _seed_review_queue(
        conn,
        queue_public_id="rq-b",
        candidate_id="cand-b",
        statement_transaction_ref=merchant,
        status="resolved",
    )
    _seed_decision(
        conn,
        decision_public_id="dec-b",
        review_queue_public_id="rq-b",
    )
    _seed_guard_decision(
        conn,
        proposal_id="prop-b",
        idempotency_key="ik-b",
        source_statement_ref=merchant,
        approved=0,
        action="blocked",
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["blocked"]
    assert rows[0]["guard_approved"] == 0


def test_blocked_via_guarded_execution(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "AppleExeBlock"
    stmt_id = _seed_stmt(conn, "stmt-block-e", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    _seed_review_queue(
        conn,
        queue_public_id="rq-be",
        candidate_id="cand-be",
        statement_transaction_ref=merchant,
        status="resolved",
    )
    _seed_decision(
        conn,
        decision_public_id="dec-be",
        review_queue_public_id="rq-be",
    )
    _seed_apply_result(
        conn,
        apply_id="apply-be",
        decision_id="dec-be",
        queue_item_id="rq-be",
        success=1,
    )
    _seed_guarded_execution(
        conn,
        execution_id="exe-be",
        plan_id="plan-be",
        idempotency_key="ik-be",
        execution_fingerprint="fp-be",
        execution_status="blocked",
        total_operations=1,
        operations_executed=0,
        operations_blocked=1,
        operations_skipped=0,
    )
    _seed_operation_result(
        conn,
        operation_result_id="op-be",
        execution_id="exe-be",
        operation_id="op-1",
        decision_id="dec-be",
        execution_status="blocked",
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["blocked"]


# ---------------------------------------------------------------------------
# 7. apply_partially_blocked
# ---------------------------------------------------------------------------


def test_apply_partially_blocked_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "ApplePartial"
    stmt_id = _seed_stmt(conn, "stmt-pb", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    _seed_review_queue(
        conn,
        queue_public_id="rq-pb",
        candidate_id="cand-pb",
        statement_transaction_ref=merchant,
        status="resolved",
    )
    _seed_decision(
        conn,
        decision_public_id="dec-pb",
        review_queue_public_id="rq-pb",
    )
    _seed_apply_result(
        conn,
        apply_id="apply-pb",
        decision_id="dec-pb",
        queue_item_id="rq-pb",
        success=1,
    )
    _seed_guarded_execution(
        conn,
        execution_id="exe-pb",
        plan_id="plan-pb",
        idempotency_key="ik-pb",
        execution_fingerprint="fp-pb",
        execution_status="partially_blocked",
        total_operations=2,
        operations_executed=1,
        operations_blocked=1,
        operations_skipped=0,
    )
    _seed_operation_result(
        conn,
        operation_result_id="op-pb-a",
        execution_id="exe-pb",
        operation_id="op-1",
        decision_id="dec-pb",
        execution_status="executed",
    )
    _seed_operation_result(
        conn,
        operation_result_id="op-pb-b",
        execution_id="exe-pb",
        operation_id="op-2",
        decision_id="dec-pb",
        execution_status="blocked",
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["apply_partially_blocked"]


# ---------------------------------------------------------------------------
# 8. apply_failed -- apply result success=0 and exec conflict
# ---------------------------------------------------------------------------


def test_apply_failed_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "AppleFail"
    stmt_id = _seed_stmt(conn, "stmt-fail", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    _seed_review_queue(
        conn,
        queue_public_id="rq-af",
        candidate_id="cand-af",
        statement_transaction_ref=merchant,
        status="resolved",
    )
    _seed_decision(
        conn,
        decision_public_id="dec-af",
        review_queue_public_id="rq-af",
    )
    _seed_apply_result(
        conn,
        apply_id="apply-af",
        decision_id="dec-af",
        queue_item_id="rq-af",
        success=0,
        idempotent=0,
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["apply_failed"]


def test_apply_failed_via_exec_conflict(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "AppleConflict"
    stmt_id = _seed_stmt(conn, "stmt-conflict", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    _seed_review_queue(
        conn,
        queue_public_id="rq-cf",
        candidate_id="cand-cf",
        statement_transaction_ref=merchant,
        status="resolved",
    )
    _seed_decision(
        conn,
        decision_public_id="dec-cf",
        review_queue_public_id="rq-cf",
    )
    _seed_apply_result(
        conn,
        apply_id="apply-cf",
        decision_id="dec-cf",
        queue_item_id="rq-cf",
        success=1,
    )
    _seed_guarded_execution(
        conn,
        execution_id="exe-cf",
        plan_id="plan-cf",
        idempotency_key="ik-cf",
        execution_fingerprint="fp-cf",
        execution_status="conflict",
        total_operations=1,
        operations_executed=0,
        operations_blocked=0,
        operations_skipped=1,
    )
    _seed_operation_result(
        conn,
        operation_result_id="op-cf",
        execution_id="exe-cf",
        operation_id="op-1",
        decision_id="dec-cf",
        execution_status="conflict",
    )
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["apply_failed"]


# ---------------------------------------------------------------------------
# 9. pending_apply -- resolved but no apply result, no blocking signal
# ---------------------------------------------------------------------------


def test_pending_apply_bucket(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    merchant = "ApplePending"
    stmt_id = _seed_stmt(conn, "stmt-pending", batch_id, merchant_raw=merchant)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    _seed_review_queue(
        conn,
        queue_public_id="rq-pa",
        candidate_id="cand-pa",
        statement_transaction_ref=merchant,
        status="resolved",
    )
    _seed_decision(
        conn,
        decision_public_id="dec-pa",
        review_queue_public_id="rq-pa",
    )
    # No apply result, no guard decision, no execution.
    rows = _run(conn)
    assert [r["reporting_bucket"] for r in rows] == ["pending_apply"]
    assert rows[0]["reporting_reason"] == "resolved but not yet applied"


# ---------------------------------------------------------------------------
# 10. no conflation -- mixed bucket row exercises all distinct labels
# ---------------------------------------------------------------------------


def test_buckets_not_conflated(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    repo = _make_repo(conn)

    # matched -> reconciled
    s1 = _seed_stmt(conn, "stmt-mix-1", batch_id, merchant_raw="Mix1")
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=s1,
        match_result=_matched_result(),
    )
    # no_match -> unmatched_statement
    s2 = _seed_stmt(conn, "stmt-mix-2", batch_id, merchant_raw="Mix2")
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=s2,
        match_result=_no_match_result(),
    )
    # mismatch -> amount_mismatch
    s3 = _seed_stmt(conn, "stmt-mix-3", batch_id, merchant_raw="Mix3")
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=s3,
        match_result=_mismatch_result(),
    )

    rows = _run(conn)
    buckets = sorted(r["reporting_bucket"] for r in rows)
    assert buckets == ["amount_mismatch", "reconciled", "unmatched_statement"]


# ---------------------------------------------------------------------------
# 11. deterministic ordering
# ---------------------------------------------------------------------------


def test_deterministic_ordering(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    repo = _make_repo(conn)
    ids = []
    for i in range(5):
        sid = _seed_stmt(conn, f"stmt-ord-{i}", batch_id, merchant_raw=f"Ord{i}")
        repo.save_match_result(
            run_id=run_id,
            statement_transaction_id=sid,
            match_result=_no_match_result(),
        )
        ids.append(sid)

    rows_a = _run(conn)
    rows_b = _run(conn)
    assert [r["statement_transaction_id"] for r in rows_a] == ids
    assert rows_a == rows_b


# ---------------------------------------------------------------------------
# 12. read-only -- source table counts unchanged after running
# ---------------------------------------------------------------------------


def test_query_is_read_only(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    batch_id, run_id = _seed_run(conn)
    stmt_id = _seed_stmt(conn, "stmt-ro", batch_id)
    repo = _make_repo(conn)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )

    tables = [
        "statement_transactions",
        "reconciliation_runs",
        "reconciliation_match_results",
        "reconciliation_review_queue",
        "reconciliation_resolution_decisions",
        "reconciliation_apply_results",
        "reconciliation_guarded_apply_executions",
        "reconciliation_guarded_apply_operation_results",
        "reconciliation_final_mutation_guard_decisions",
    ]
    before = {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"] for t in tables}
    _run(conn)
    _run(conn)  # run twice for good measure
    after = {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"] for t in tables}
    assert before == after


# ---------------------------------------------------------------------------
# 13. does not touch live DB
# ---------------------------------------------------------------------------


def test_does_not_touch_live_db(
    migrated_temp_db_connection: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    db_file = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(db_file) == temp_db_path
    assert temp_db_path != LIVE_DB_PATH

    batch_id, run_id = _seed_run(migrated_temp_db_connection)
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-live", batch_id)
    repo = _make_repo(migrated_temp_db_connection)
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_matched_result(),
    )
    rows = _run(migrated_temp_db_connection)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 14. SQL file contains no write statements
# ---------------------------------------------------------------------------


def test_sql_file_has_no_write_statements() -> None:
    sql = _read_sql()
    # Strip SQL comments to avoid false positives from prose mentioning words.
    stripped = re.sub(r"--[^\n]*", "", sql)
    match = WRITE_FORBIDDEN.search(stripped)
    assert match is None, f"reporting query must be read-only; found a write/DDL token: {match}"
