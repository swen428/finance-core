"""Tests for Reconciliation Review Queue v1.

Covers:
  1. get_review_required returns needs_review entries
  2. get_unmatched excludes matched status
  3. get_amount_mismatches returns only amount_mismatch status
  4. get_ambiguous returns ambiguous and possible_duplicate
  5. get_run_summary returns correct aggregated counts
  6. get_all_run_summaries returns summaries for all runs
  7. get_statement_context returns statement + match data
  8. get_match_audit returns detailed audit view
  9. get_match_audits_for_run returns all audits for a run
 10. run_summary with no match results returns zeroes
 11. review queue is read-only
 12. review queue does not touch database/finance.db
 13. empty review queue returns empty list
 14. run summary for nonexistent run returns None
 15. match audit for nonexistent id returns None
"""

from __future__ import annotations

import sqlite3
from datetime import date
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
from finance_core.reconciliation.review_queue import (
    MatchAuditView,
    ReconciliationReviewQueue,
    ReviewQueueEntry,
    RunSummaryView,
    StatementContextView,
)

if TYPE_CHECKING:
    import sqlite3

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_batch(conn: sqlite3.Connection, public_id: str = "batch-q") -> int:
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


def _make_queue(conn: sqlite3.Connection) -> ReconciliationReviewQueue:
    return ReconciliationReviewQueue(conn)


def _make_match_result(status: MatchStatus) -> MatchResult:
    if status == MatchStatus.MATCHED:
        return MatchResult(
            status=status,
            reasons=(ReasonCode.EXACT_AMOUNT_MATCH,),
            evidence=MatchEvidence(
                statement_amount=Decimal("29.90"),
                candidate_amount=Decimal("29.90"),
                candidate_count=1,
            ),
            best_candidate=InternalCandidate(
                internal_id="cand-001",
                transaction_date=date(2024, 12, 1),
                merchant="Apple",
                amount=Decimal("29.90"),
                currency="SGD",
            ),
        )
    if status == MatchStatus.AMOUNT_MISMATCH:
        return MatchResult(
            status=status,
            reasons=(ReasonCode.AMOUNT_DIFFERS,),
            evidence=MatchEvidence(
                statement_amount=Decimal("50.00"),
                candidate_amount=Decimal("29.90"),
                candidate_count=1,
            ),
        )
    return MatchResult(
        status=status,
        reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
        evidence=MatchEvidence(candidate_count=0),
    )


# ---------------------------------------------------------------------------
# 1. get_review_required returns needs_review entries
# ---------------------------------------------------------------------------


def test_get_review_required(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-review")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-r1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-r2", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-review")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.NO_MATCH),
    )

    entries = queue.get_review_required(run_id=run_id)
    assert len(entries) == 1
    assert entries[0].match_status == "no_match"
    assert isinstance(entries[0], ReviewQueueEntry)
    assert entries[0].run_public_id == "run-review"


# ---------------------------------------------------------------------------
# 2. get_unmatched excludes matched status
# ---------------------------------------------------------------------------


def test_get_unmatched(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-unmatch")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-u1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-u2", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-unmatch")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.NO_MATCH),
    )

    unmatched = queue.get_unmatched(run_id)
    assert len(unmatched) == 1
    assert unmatched[0].match_status == "no_match"


# ---------------------------------------------------------------------------
# 3. get_amount_mismatches
# ---------------------------------------------------------------------------


def test_get_amount_mismatches(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-amtq")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-a1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-a2", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-amtq")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.AMOUNT_MISMATCH),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    entries = queue.get_amount_mismatches(run_id=run_id)
    assert len(entries) == 1
    assert entries[0].match_status == "amount_mismatch"
    assert entries[0].amount_delta is not None


# ---------------------------------------------------------------------------
# 4. get_ambiguous
# ---------------------------------------------------------------------------


def test_get_ambiguous(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-ambq")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-amb1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-amb2", batch_id)
    stmt3 = _seed_stmt(migrated_temp_db_connection, "stmt-amb3", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-ambq")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.AMBIGUOUS),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.POSSIBLE_DUPLICATE),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt3,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    entries = queue.get_ambiguous(run_id=run_id)
    assert len(entries) == 2
    statuses = {e.match_status for e in entries}
    assert statuses == {"ambiguous", "possible_duplicate"}


# ---------------------------------------------------------------------------
# 5. get_run_summary returns correct aggregated counts
# ---------------------------------------------------------------------------


def test_get_run_summary(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-summary")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-s1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-s2", batch_id)
    stmt3 = _seed_stmt(migrated_temp_db_connection, "stmt-s3", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-summary")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.NO_MATCH),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt3,
        match_result=_make_match_result(MatchStatus.AMOUNT_MISMATCH),
    )

    summary = queue.get_run_summary(run_id)
    assert summary is not None
    assert isinstance(summary, RunSummaryView)
    assert summary.run_public_id == "run-summary"
    assert summary.total_match_results == 3
    assert summary.matched_count == 1
    assert summary.no_match_count == 1
    assert summary.amount_mismatch_count == 1
    assert summary.needs_review_count == 2


# ---------------------------------------------------------------------------
# 6. get_all_run_summaries
# ---------------------------------------------------------------------------


def test_get_all_run_summaries(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-all")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-all1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-all2", batch_id)

    run1 = repo.create_reconciliation_run(public_id="run-all-1")
    repo.save_match_result(
        run_id=run1,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    run2 = repo.create_reconciliation_run(public_id="run-all-2")
    repo.save_match_result(
        run_id=run2,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.NO_MATCH),
    )

    summaries = queue.get_all_run_summaries()
    assert len(summaries) == 2
    assert summaries[0].run_public_id == "run-all-1"
    assert summaries[1].run_public_id == "run-all-2"


# ---------------------------------------------------------------------------
# 7. get_statement_context
# ---------------------------------------------------------------------------


def test_get_statement_context(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-ctx")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-ctx", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-ctx")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    contexts = queue.get_statement_context(run_id)
    assert len(contexts) == 1
    ctx = contexts[0]
    assert isinstance(ctx, StatementContextView)
    assert ctx.statement_public_id == "stmt-ctx"
    assert ctx.merchant_raw == "Apple"
    assert ctx.match_status == "matched"


# ---------------------------------------------------------------------------
# 8. get_match_audit
# ---------------------------------------------------------------------------


def test_get_match_audit(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-audit")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-audit", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-audit")

    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    audit = queue.get_match_audit(mr_id)
    assert audit is not None
    assert isinstance(audit, MatchAuditView)
    assert audit.match_public_id is not None
    assert audit.match_status == "matched"
    assert audit.run_id == run_id


# ---------------------------------------------------------------------------
# 9. get_match_audits_for_run
# ---------------------------------------------------------------------------


def test_get_match_audits_for_run(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-audits")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-ad1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-ad2", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-audits")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=_make_match_result(MatchStatus.NO_MATCH),
    )

    audits = queue.get_match_audits_for_run(run_id)
    assert len(audits) == 2


# ---------------------------------------------------------------------------
# 10. run_summary with no match results returns zeroes
# ---------------------------------------------------------------------------


def test_run_summary_empty(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    run_id = repo.create_reconciliation_run(public_id="run-empty")

    summary = queue.get_run_summary(run_id)
    assert summary is not None
    assert summary.total_match_results == 0
    assert summary.matched_count == 0


# ---------------------------------------------------------------------------
# 11. review queue is read-only
# ---------------------------------------------------------------------------


def test_review_queue_is_read_only(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-ro")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-roq", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-roq")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    before = len(queue.get_match_audits_for_run(run_id))
    _ = queue.get_review_required(run_id=run_id)
    _ = queue.get_run_summary(run_id)
    after = len(queue.get_match_audits_for_run(run_id))
    assert after == before


# ---------------------------------------------------------------------------
# 12. review queue does not touch database/finance.db
# ---------------------------------------------------------------------------


def test_review_queue_does_not_touch_live_db(
    migrated_temp_db_connection: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH

    queue = _make_queue(migrated_temp_db_connection)
    entries = queue.get_review_required()
    assert entries == []


# ---------------------------------------------------------------------------
# 13. empty review queue returns empty list
# ---------------------------------------------------------------------------


def test_empty_review_queue(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    queue = _make_queue(migrated_temp_db_connection)

    batch_id = _seed_batch(migrated_temp_db_connection, "batch-empty")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-empty", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-empty")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    # All are matched, so review queue should be empty
    entries = queue.get_review_required(run_id=run_id)
    assert entries == []


# ---------------------------------------------------------------------------
# 14. run summary for nonexistent run returns None
# ---------------------------------------------------------------------------


def test_run_summary_nonexistent(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    queue = _make_queue(migrated_temp_db_connection)
    summary = queue.get_run_summary(99999)
    assert summary is None


# ---------------------------------------------------------------------------
# 15. match audit for nonexistent id returns None
# ---------------------------------------------------------------------------


def test_match_audit_nonexistent(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    queue = _make_queue(migrated_temp_db_connection)
    audit = queue.get_match_audit(99999)
    assert audit is None


def test_review_queue_accepts_plain_sqlite_connection(
    migrated_temp_db_path: Path,
) -> None:
    conn = sqlite3.connect(migrated_temp_db_path)
    try:
        repo = _make_repo(conn)
        queue = _make_queue(conn)
        batch_id = repo.create_statement_import_batch(
            public_id="batch-plain-conn",
            source_type="bank_statement",
        )
        stmt_id = repo.create_statement_transaction(
            public_id="stmt-plain-conn",
            batch_id=batch_id,
            merchant_raw="Apple",
            amount=Decimal("29.90"),
            currency="SGD",
        )
        run_id = repo.create_reconciliation_run(public_id="run-plain-conn")
        repo.save_match_result(
            run_id=run_id,
            statement_transaction_id=stmt_id,
            match_result=_make_match_result(MatchStatus.NO_MATCH),
        )

        entries = queue.get_review_required(run_id=run_id)

        assert len(entries) == 1
        assert entries[0].match_status == "no_match"
    finally:
        conn.close()
