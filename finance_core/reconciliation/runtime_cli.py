"""Reconciliation Runtime CLI v1 -- focused entry point for running a local
reconciliation workflow from imported statement/application data through
matching and review output, without mutating final transactions or
generating settlement obligations.

Usage::

    # Default: dry-run with test fixtures
    python -m finance_core.reconciliation.runtime_cli \
        --statement tests/fixtures/reconciliation/sample_statement.csv \
        --app-transactions tests/fixtures/reconciliation/sample_app_transactions.json

    # With a specific temp database (still guarded, still read-only)
    python -m finance_core.reconciliation.runtime_cli \
        --statement tests/fixtures/reconciliation/sample_statement.csv \
        --app-transactions tests/fixtures/reconciliation/sample_app_transactions.json \
        --db /tmp/recon_runtime.db

    # Non-dry-run mode (applies match results to temp DB, still no final txns)
    python -m finance_core.reconciliation.runtime_cli \
        --statement tests/fixtures/reconciliation/sample_statement.csv \
        --app-transactions tests/fixtures/reconciliation/sample_app_transactions.json \
        --no-dry-run

Safety properties:
- Uses a temporary SQLite database by default; never touches database/finance.db.
- Dry-run by default: match, review queue, and apply plan are generated
  but no persistence occurs unless --no-dry-run is specified (and then
  only into a temp DB).
- Does not create, update, or delete final financial transactions.
- Does not generate settlement obligations.
- Deterministic output given the same fixture inputs.
- Exits with code 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from finance_core.reconciliation.demo_fixture import (
    _load_app_transactions_from_json,
    _structured_rows_to_statements,
)
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.migrations import (
    LIVE_DB_PATH,
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
)
from finance_core.reconciliation.models import (
    AppTransaction,
    ReconciliationCandidate,
    ReviewQueueItem,
    StatementTransaction,
)
from finance_core.reconciliation.review_queue import generate_review_queue
from finance_core.reconciliation.statement_csv import StatementCsvAdapter
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LIVE_DB_PATH = LIVE_DB_PATH.resolve()
_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS


# ---------------------------------------------------------------------------
# Result model -- deterministic, testable
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationRuntimeResult:
    """Deterministic result of a reconciliation runtime execution.

    Contains all data needed to format CLI output and to assert behavior
    in tests without shelling out.
    """

    exit_code: int
    dry_run: bool
    db_path: Path

    # Inputs
    statement_rows_count: int = 0
    app_transactions_count: int = 0
    statements: tuple[StatementTransaction, ...] = ()
    app_transactions: tuple[AppTransaction, ...] = ()

    # Matching
    candidates: tuple[ReconciliationCandidate, ...] = ()

    # Review queue
    queue_items: tuple[ReviewQueueItem, ...] = ()

    # Error info (when exit_code != 0)
    error_message: str = ""


# ---------------------------------------------------------------------------
# Core runtime function
# ---------------------------------------------------------------------------


def run_reconciliation_runtime(
    statement_csv_path: str | Path,
    app_transactions_json_path: str | Path,
    *,
    db_path: str | Path | None = None,
    dry_run: bool = True,
    date_tolerance_days: int = 3,
) -> ReconciliationRuntimeResult:
    """Run the reconciliation runtime workflow end-to-end."""
    statement_csv_path = Path(statement_csv_path).resolve()
    app_transactions_json_path = Path(app_transactions_json_path).resolve()

    # -- Guard: refuse live database --
    if db_path is not None:
        resolved_db = Path(db_path).resolve()
        if resolved_db == _LIVE_DB_PATH:
            return ReconciliationRuntimeResult(
                exit_code=1,
                dry_run=dry_run,
                db_path=resolved_db,
                error_message=(
                    f"Refusing to use live database at {resolved_db}. "
                    "Use a temporary database instead."
                ),
            )
    else:
        resolved_db = Path(_create_temp_db_path())

    # -- Load statement CSV --
    try:
        adapter = StatementCsvAdapter()
        csv_result = adapter.parse_file(statement_csv_path)
        if not csv_result.success:
            return ReconciliationRuntimeResult(
                exit_code=1,
                dry_run=dry_run,
                db_path=resolved_db,
                error_message=f"CSV parse errors: {csv_result.errors}",
            )
        statements = _structured_rows_to_statements(csv_result.rows)
    except Exception as exc:
        return ReconciliationRuntimeResult(
            exit_code=1,
            dry_run=dry_run,
            db_path=resolved_db,
            error_message=f"Failed to load statement CSV: {exc}",
        )

    # -- Load app transactions --
    try:
        app_transactions = _load_app_transactions_json(app_transactions_json_path)
    except Exception as exc:
        return ReconciliationRuntimeResult(
            exit_code=1,
            dry_run=dry_run,
            db_path=resolved_db,
            statement_rows_count=len(statements),
            statements=tuple(statements),
            error_message=f"Failed to load app transactions: {exc}",
        )

    # -- Run batch matching (in-memory, deterministic) --
    try:
        candidates = match_batch(
            statements,
            app_transactions,
            date_tolerance_days=date_tolerance_days,
        )
    except Exception as exc:
        return ReconciliationRuntimeResult(
            exit_code=1,
            dry_run=dry_run,
            db_path=resolved_db,
            statement_rows_count=len(statements),
            app_transactions_count=len(app_transactions),
            statements=tuple(statements),
            app_transactions=tuple(app_transactions),
            error_message=f"Matching failed: {exc}",
        )

    # -- Generate review queue (in-memory, deterministic) --
    queue_items, _review_summary = generate_review_queue(candidates)

    # -- Optionally persist to temp DB (non-dry-run mode) --
    if not dry_run:
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_sqlite(resolved_db, mode=ConnectionMode.APPLICATION)
            _apply_migrations(conn)

            from finance_core.reconciliation.review_persistence import (
                ReviewQueuePersistence,
            )

            rqp = ReviewQueuePersistence(conn)
            rqp.persist_review_queue(
                queue_items,
                run_public_id="runtime-cli-run",
            )
            conn.commit()
        except Exception as exc:
            return ReconciliationRuntimeResult(
                exit_code=1,
                dry_run=dry_run,
                db_path=resolved_db,
                statement_rows_count=len(statements),
                app_transactions_count=len(app_transactions),
                statements=tuple(statements),
                app_transactions=tuple(app_transactions),
                candidates=tuple(candidates),
                queue_items=tuple(queue_items),
                error_message=f"Persistence failed: {exc}",
            )
        finally:
            if conn is not None:
                conn.close()

    return ReconciliationRuntimeResult(
        exit_code=0,
        dry_run=dry_run,
        db_path=resolved_db,
        statement_rows_count=len(statements),
        app_transactions_count=len(app_transactions),
        statements=tuple(statements),
        app_transactions=tuple(app_transactions),
        candidates=tuple(candidates),
        queue_items=tuple(queue_items),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the reconciliation runtime CLI. Returns 0 on success, 1 on error."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    result = run_reconciliation_runtime(
        statement_csv_path=args.statement,
        app_transactions_json_path=args.app_transactions,
        db_path=args.db if args.db else None,
        dry_run=not args.no_dry_run,
    )

    if result.exit_code != 0:
        print(f"Error: {result.error_message}", file=sys.stderr)
        return result.exit_code

    print(format_runtime_summary(result))
    return 0


# ---------------------------------------------------------------------------
# Output formatting (deterministic)
# ---------------------------------------------------------------------------


def format_runtime_summary(result: ReconciliationRuntimeResult) -> str:
    """Format a reconciliation runtime result for terminal output."""
    lines: list[str] = []
    status_order = (
        "matched",
        "amount_mismatch",
        "currency_mismatch",
        "date_mismatch",
        "merchant_mismatch",
        "possible_duplicate",
        "ambiguous",
        "needs_review",
        "no_match",
    )

    # -- Header --
    lines.append("Reconciliation Runtime v1 Summary")
    lines.append("=" * 70)
    if result.dry_run:
        lines.append("  Mode:        dry-run (read-only, no persistence)")
    else:
        lines.append(f"  Database:    {result.db_path}")
    lines.append("")

    # -- Section 1: Inputs --
    lines.append("Imported / Loaded Records")
    lines.append("-" * 70)
    lines.append(f"  Statement-side records loaded:  {result.statement_rows_count}")
    lines.append(f"  App-side candidate transactions: {result.app_transactions_count}")
    lines.append("")

    # -- Section 2: Statement records detail --
    if result.statement_rows_count > 0:
        lines.append("Statement-Side Transactions")
        lines.append("-" * 40)
        for idx, stmt in enumerate(result.statements, start=1):
            merchant = stmt.merchant_normalized if stmt.merchant_normalized else stmt.merchant_raw
            txn_date = (
                stmt.transaction_date.isoformat()
                if stmt.transaction_date
                else (stmt.posted_date.isoformat() if stmt.posted_date else "N/A")
            )
            amt = f"{stmt.amount:.2f}" if stmt.amount is not None else "N/A"
            currency = stmt.currency or "N/A"
            lines.append(f"  {idx}. {txn_date}  {merchant}  {amt} {currency}")
        lines.append("")

    # -- Section 3: App-side transactions detail --
    if result.app_transactions_count > 0:
        lines.append("App-Side Candidate Transactions (for matching)")
        lines.append("-" * 40)
        for idx, app_txn in enumerate(result.app_transactions, start=1):
            txn_date = app_txn.transaction_date.isoformat() if app_txn.transaction_date else "N/A"
            amt = f"{app_txn.amount:.2f}" if app_txn.amount is not None else "N/A"
            currency = app_txn.currency or "N/A"
            lines.append(f"  {idx}. {txn_date}  {app_txn.merchant}  {amt} {currency}")
        lines.append("")

    # -- Section 4: Match results (by status) --
    lines.append("Match Results")
    lines.append("-" * 40)
    for key in status_order:
        count = _count_status(result.candidates, key)
        if count > 0:
            lines.append(f"  {key:24s}: {count}")

    # -- Section 5: Unmatched records --
    lines.append("")
    lines.append("Unmatched Records")
    lines.append("-" * 40)
    unmatched_statement = _count_status(result.candidates, "no_match")
    # Identify app transactions never referenced by any candidate
    referenced_app_ids: set[str] = set()
    for c in result.candidates:
        if hasattr(c, "best_app_transaction") and c.best_app_transaction is not None:
            referenced_app_ids.add(c.best_app_transaction.app_txn_id)
    unmatched_app = sum(
        1 for a in result.app_transactions if a.app_txn_id not in referenced_app_ids
    )
    lines.append(f"  Unmatched statement records: {unmatched_statement}")
    lines.append(f"  Unmatched app-side records:  {unmatched_app}")
    lines.append("")

    # -- Section 6: Review queue summary --
    lines.append("Review Queue")
    lines.append("-" * 70)
    review_items = [q for q in result.queue_items if not _is_matched(q)]
    matched_items = [q for q in result.queue_items if _is_matched(q)]
    lines.append(f"  Total queue items:          {len(result.queue_items)}")
    lines.append(f"  Matched (no review needed): {len(matched_items)}")
    lines.append(f"  Review-required items:      {len(review_items)}")

    # Priority breakdown
    high = sum(1 for q in review_items if _priority_label(q) == "HIGH")
    medium = sum(1 for q in review_items if _priority_label(q) == "MEDIUM")
    low = sum(1 for q in review_items if _priority_label(q) == "LOW")
    if high + medium + low > 0:
        lines.append("")
        lines.append("  Priority breakdown (review-required only):")
        lines.append(f"    High priority:   {high}")
        lines.append(f"    Medium priority: {medium}")
        lines.append(f"    Low priority:    {low}")

    # List review-required items
    if review_items:
        lines.append("")
        lines.append(f"  Review-required items ({len(review_items)}):")
        shown = min(len(review_items), 10)
        for idx, item in enumerate(review_items[:shown], start=1):
            stmt = item.candidate.statement
            stmt_merchant = stmt.merchant_raw
            stmt_amount = stmt.amount
            amt_str = f"{stmt_amount:.2f}" if stmt_amount is not None else "N/A"
            currency = stmt.currency or ""
            lines.append(
                f"    {idx}. [{_priority_label(item)}] "
                f"{item.issue_type.value} "
                f"-- {stmt_merchant} {amt_str} {currency}"
            )
        if len(review_items) > 10:
            lines.append(f"    ... and {len(review_items) - 10} more items")
    lines.append("")

    # -- Section 7: Guarded Apply Plan (informational) --
    lines.append("Guarded Apply Plan")
    lines.append("-" * 70)
    matched_executable = len(matched_items)
    blocked_pending_review = len(review_items)
    lines.append(f"  Matched (executable if approved): {matched_executable}")
    lines.append(f"  Blocked (pending review):         {blocked_pending_review}")

    if blocked_pending_review > 0:
        lines.append("")
        lines.append("  Blocked operations (require human resolution):")
        for idx, item in enumerate(review_items[:5], start=1):
            stmt = item.candidate.statement
            stmt_merchant = stmt.merchant_raw
            stmt_amount = stmt.amount
            amt_str = f"{stmt_amount:.2f}" if stmt_amount is not None else "N/A"
            lines.append(
                f"    {idx}. {item.issue_type.value} -- "
                f"{item.suggested_action.value} ({stmt_merchant} {amt_str})"
            )
        if len(review_items) > 5:
            lines.append(f"    ... and {len(review_items) - 5} more blocked operations")
    lines.append("")

    # -- Section 8: Blocked / partially blocked outcomes --
    if blocked_pending_review > 0:
        lines.append("Blocked / Partially Blocked Outcomes")
        lines.append("-" * 70)
        lines.append(
            f"  {blocked_pending_review} operation(s) require human review "
            f"before any mutation can proceed."
        )
        lines.append("  No final transactions have been created or mutated.")
        lines.append("")

    # -- Safety footers --
    lines.append("Safety")
    lines.append("-" * 70)
    lines.append("  Final transactions mutated:   0")
    lines.append("  Settlement obligations created:  0")
    lines.append("  Live database touched:         No")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconciliation Runtime CLI v1",
    )
    parser.add_argument(
        "--statement",
        required=True,
        help="Path to the statement CSV file.",
    )
    parser.add_argument(
        "--app-transactions",
        required=True,
        help="Path to the app-side transaction candidates JSON file.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Path to a SQLite database file for persistence. "
            "Defaults to a temporary database. Must not be database/finance.db."
        ),
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help=(
            "When set, persist match results to the SQLite database. "
            "Defaults to dry-run (in-memory only)."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _create_temp_db_path() -> str:
    """Create a temporary file path for a SQLite database."""
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="recon_runtime_")
    os.close(fd)
    return tmp_path


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all project migrations to the connection."""
    for index, migration_path in enumerate(_MIGRATION_PATHS):
        if not migration_path.exists():
            continue
        if migration_path.name == "009_statement_import_fingerprint_dedup.sql":
            cols = conn.execute("PRAGMA table_info(statement_transactions)").fetchall()
            if any(c[1] == "row_fingerprint" for c in cols):
                continue
        if migration_path.name == "010_statement_amount_direction_persistence.sql":
            cols = conn.execute("PRAGMA table_info(statement_transactions)").fetchall()
            if any(c[1] == "amount_direction" for c in cols):
                continue
        apply_migration_paths(conn, _MIGRATION_PATHS[index : index + 1])
    conn.commit()


def _fallback_priority_label(priority_int: int) -> str:
    if priority_int >= 6:
        return "HIGH"
    elif priority_int >= 3:
        return "MEDIUM"
    else:
        return "LOW"


def _priority_label(item: ReviewQueueItem) -> str:
    """Derive priority label from the review queue item's structured evidence."""
    try:
        se = item.structured_evidence
        if se is None:
            return _fallback_priority_label(item.priority)
        rp = se.review_priority
        return rp.value.upper()
    except Exception:
        # Fallback: map priority int to label
        p = item.priority
        if p >= 6:
            return "HIGH"
        elif p >= 3:
            return "MEDIUM"
        else:
            return "LOW"


def _is_matched(item: ReviewQueueItem) -> bool:
    """Return True if the review queue item is a clean match (no review needed)."""
    return item.issue_type.value == "matched"


def _count_status(candidates: Sequence[ReconciliationCandidate], status_value: str) -> int:
    """Count candidates with a given match status."""
    return sum(1 for c in candidates if c.match_status.value == status_value)


def _load_app_transactions_json(path: str | Path) -> list[AppTransaction]:
    """Load AppTransaction objects from a JSON fixture.

    Accepts both the 'candidates' key (InternalCandidate format)
    and 'app_transactions' key (AppTransaction format).
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Try 'app_transactions' key first
    raw_list = data.get("app_transactions", [])
    if raw_list:
        return _load_app_transactions_from_json(str(path))

    # Try 'candidates' key (InternalCandidate format)
    raw_candidates = data.get("candidates", [])
    if raw_candidates:
        from decimal import Decimal as _Decimal

        result: list[AppTransaction] = []
        for c in raw_candidates:
            txn_date_raw = c.get("transaction_date")
            txn_date = date.fromisoformat(txn_date_raw) if txn_date_raw else None
            result.append(
                AppTransaction(
                    app_txn_id=c["internal_id"],
                    transaction_date=txn_date,  # type: ignore[arg-type]
                    merchant=c["merchant"],
                    amount=_Decimal(str(c["amount"])),
                    currency=c["currency"],
                )
            )
        return result

    # Try 'transactions' key as fallback
    raw_txns = data.get("transactions", [])
    if raw_txns:
        from decimal import Decimal as _Decimal

        result = []
        for t in raw_txns:
            txn_date_raw = t.get("transaction_date")
            txn_date = date.fromisoformat(txn_date_raw) if txn_date_raw else None
            result.append(
                AppTransaction(
                    app_txn_id=t.get("app_txn_id", ""),
                    transaction_date=txn_date,  # type: ignore[arg-type]
                    merchant=t.get("merchant", ""),
                    amount=_Decimal(str(t.get("amount", "0"))),
                    currency=t.get("currency", ""),
                )
            )
        return result

    return []


if __name__ == "__main__":
    sys.exit(main())
