"""Reconciliation Demo CLI v1 -- lightweight command-line entry points for
running either the end-to-end reconciliation demo (SQLite-backed) or the
in-memory review queue demo.

Usage::

    # SQLite-backed demo (existing)
    python -m finance_core.reconciliation.demo_cli \\
        --statement tests/fixtures/reconciliation/sample_statement.csv \\
        --candidates tests/fixtures/reconciliation/sample_app_transactions.json

    # In-memory review queue demo (new)
    python -m finance_core.reconciliation.demo_cli review \\
        --statement tests/fixtures/reconciliation/review_queue_statement.csv \\
        --app-transactions tests/fixtures/reconciliation/review_queue_app_transactions.json

    # Persist review queue to SQLite (new)
    python -m finance_core.reconciliation.demo_cli persist-review \\
        --statement tests/fixtures/reconciliation/review_queue_statement.csv \\
        --app-transactions tests/fixtures/reconciliation/review_queue_app_transactions.json \\
        --db /tmp/reconciliation_review_demo.db


    # Apply resolve decisions and persist results (new)
    python -m finance_core.reconciliation.demo_cli apply-resolve \\
        --statement tests/fixtures/reconciliation/review_queue_statement.csv \\
        --app-transactions tests/fixtures/reconciliation/review_queue_app_transactions.json \\
        --decisions tests/fixtures/reconciliation/resolution_decisions.json \\
        --db /tmp/reconciliation_apply_demo.db
    # Resolve review queue from SQLite (new)
    python -m finance_core.reconciliation.demo_cli resolve-review \\
        --db /tmp/reconciliation_review_demo.db \\
        --decisions tests/fixtures/reconciliation/resolution_decisions.json

    # List persisted guarded apply execution summaries (read-only)
    python -m finance_core.reconciliation.demo_cli guarded-execution-summaries \\
        --db /tmp/reconciliation_apply_demo.db \\
        --requires-human-review

    # Read-only end-to-end reconciliation demo (no final mutations,
    # no settlement obligations). Defaults to the sample fixtures.
    python -m finance_core.reconciliation.demo_cli read-only-e2e
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from finance_core.reconciliation.demo_fixture import (
    build_read_only_e2e_summary,
    build_review_entries_sorted,
    build_review_table_rows,
    format_in_memory_review_summary,
    format_in_memory_review_table,
    format_read_only_e2e_summary,
    format_review_table,
    run_demo_reconciliation,
    run_in_memory_review,
)
from finance_core.reconciliation.migrations import (
    LIVE_DB_PATH,
    PROJECT_ROOT,
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
)
from finance_core.reconciliation.models import (
    ApplyConflictError,
    ResolutionAction,
    ResolutionDecision,
    ReviewQueueItem,
)
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

# Project root for migration paths and live-DB guard.
_PROJECT_ROOT = PROJECT_ROOT

# -- Migration paths (mirrors tests/conftest.py via the canonical manifest) --
_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS

_LIVE_DB_PATH = LIVE_DB_PATH.resolve()

# -- Default fixtures for the read-only E2E demo (stable, deterministic) --
_DEFAULT_E2E_STATEMENT = (
    _PROJECT_ROOT / "tests" / "fixtures" / "reconciliation" / "sample_statement.csv"
)
_DEFAULT_E2E_APP_TRANSACTIONS = (
    _PROJECT_ROOT / "tests" / "fixtures" / "reconciliation" / "sample_app_transactions.json"
)


def main(argv: list[str] | None = None) -> int:
    """Run the reconciliation demo CLI. Returns 0 on success."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "review":
        return _run_review_command(args)
    elif args.command == "persist-review":
        return _run_persist_review_command(args)
    elif args.command == "apply-resolve":
        return _run_apply_resolve_command(args)
    elif args.command == "resolve-review":
        return _run_resolve_review_command(args)
    elif args.command == "guarded-execution-summaries":
        return _run_guarded_execution_summaries_command(args)
    elif args.command == "read-only-e2e":
        return _run_read_only_e2e_command(args)
    else:
        return _run_demo_command(args)


# ---------------------------------------------------------------------------
# Existing commands
# ---------------------------------------------------------------------------


def _run_review_command(args: argparse.Namespace) -> int:
    """Run the in-memory review queue demo.

    Output format follows the enhanced review queue spec with priority-tier
    grouping::

        Reconciliation Review Queue

        Summary:
          - matched: X
          - review required: Y
          - high priority: A
          - medium priority: B
          - low priority: C

        High Priority
        1. GRAB
           Issue: Amount mismatch
           Statement: SGD 18.80 on 2026-05-26
           App record: SGD 18.20 on 2026-05-24
           Reason codes: AMOUNT_MISMATCH, ...
           Suggested action: Review amount
    """
    statement_path = Path(args.statement).resolve()
    if not statement_path.exists():
        print(f"Error: statement CSV not found: {statement_path}", file=sys.stderr)
        return 1

    app_txn_path = Path(args.app_transactions).resolve()
    if not app_txn_path.exists():
        print(
            f"Error: app transactions JSON not found: {app_txn_path}",
            file=sys.stderr,
        )
        return 1

    try:
        result = run_in_memory_review(statement_path, app_txn_path)

        # Print summary (compact format with priority tier counts)
        print(format_in_memory_review_summary(result))
        print()

        # Print priority-grouped review table
        print(format_in_memory_review_table(result))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Read-only E2E demo command (Read-Only E2E Demo CLI v1)
# ---------------------------------------------------------------------------


def _run_read_only_e2e_command(args: argparse.Namespace) -> int:
    """Run the read-only end-to-end reconciliation demo.

    Proves the current reconciliation pipeline can run from demo fixtures
    through statement parsing/import, matching/review candidate generation,
    and review summary output -- without mutating real financial data.

    The flow:

        sample statement CSV
        + sample app transactions fixture
        -> statement parsing/import (temp DB only)
        -> matching / review candidate generation
        -> read-only review summary output

    Safety properties:
    - Uses a temporary SQLite database only; never touches
      ``database/finance.db`` (enforced by ``_assert_temp_db`` and the
      fixture-path guard below).
    - Does not create, update, delete, or merge final transactions.
    - Does not create settlement obligations.
    - Does not execute guarded apply mutations.
    - Final mutations executed: 0.  Settlement obligations created: 0.
    - Output is deterministic given the same fixture inputs.
    """
    statement_path = Path(args.statement).resolve()
    if not statement_path.exists():
        print(f"Error: statement CSV not found: {statement_path}", file=sys.stderr)
        return 1

    candidates_path = Path(args.app_transactions).resolve()
    if not candidates_path.exists():
        print(
            f"Error: app transactions JSON not found: {candidates_path}",
            file=sys.stderr,
        )
        return 1

    # -- Guard: refuse if either fixture resolves to the live DB path --
    # Fixtures are CSV/JSON, never the live DB, but guard defensively.
    if statement_path == _LIVE_DB_PATH or candidates_path == _LIVE_DB_PATH:
        print(
            "Error: refusing to use live database path as a fixture. Use fixture files instead.",
            file=sys.stderr,
        )
        return 1

    # -- Create temp database (file on disk, never database/finance.db) --
    conn = _create_temp_db()
    try:
        _apply_migrations(conn)

        result = run_demo_reconciliation(
            conn=conn,
            csv_path=statement_path,
            candidate_json_path=candidates_path,
            batch_public_id=args.batch_id or None,
            run_public_id=args.run_id or None,
        )

        # -- Build review queue via existing read-only helpers --
        sorted_entries = build_review_entries_sorted(conn, result.run_summary.run_id)
        review_item_count = len(sorted_entries)

        summary = build_read_only_e2e_summary(
            statement_fixture_path=statement_path,
            app_transactions_fixture_path=candidates_path,
            result=result,
            review_item_count=review_item_count,
        )

        # -- Print read-only E2E summary --
        print(format_read_only_e2e_summary(summary))

        # -- Print match status breakdown (deterministic, stable order) --
        print()
        print("Match status breakdown:")
        for status_key in (
            "matched",
            "amount_mismatch",
            "currency_mismatch",
            "date_mismatch",
            "merchant_mismatch",
            "possible_duplicate",
            "ambiguous",
            "needs_review",
            "no_match",
        ):
            count = summary.match_status_counts.get(status_key, 0)
            if count:
                print(f"  {status_key}: {count}")

        # -- Print review queue summary (reuses existing helpers) --
        print()
        if review_item_count:
            print(f"Review Queue ({review_item_count} items, sorted by priority)")
        else:
            print("Review Queue (all items matched)")
        print()
        table_rows = build_review_table_rows(sorted_entries)
        print(format_review_table(table_rows))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return 0


def _run_demo_command(args: argparse.Namespace) -> int:
    """Run the SQLite-backed reconciliation demo (existing functionality)."""
    statement_path = Path(args.statement).resolve()
    if not statement_path.exists():
        print(f"Error: statement CSV not found: {statement_path}", file=sys.stderr)
        return 1

    candidates_path = Path(args.candidates).resolve()
    if not candidates_path.exists():
        print(f"Error: candidates JSON not found: {candidates_path}", file=sys.stderr)
        return 1

    # -- Create temp database --
    conn = _create_temp_db()
    try:
        _apply_migrations(conn)

        # -- Run demo --
        result = run_demo_reconciliation(
            conn=conn,
            csv_path=statement_path,
            candidate_json_path=candidates_path,
            batch_public_id=args.batch_id or None,
            run_public_id=args.run_id or None,
        )

        # -- Print summary --
        print(result.summary_table)

        # -- Print review queue --
        sorted_entries = build_review_entries_sorted(conn, result.run_summary.run_id)
        table_rows = build_review_table_rows(sorted_entries)

        print()
        if table_rows:
            print(f"Review Queue ({len(table_rows)} items, sorted by priority)")
        else:
            print("Review Queue (all items matched)")
        print()
        print(format_review_table(table_rows))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return 0


# ---------------------------------------------------------------------------
# Persist-review command (Review Queue + Resolution v1)
# ---------------------------------------------------------------------------


def _generate_run_id() -> str:
    """Generate a safe unique run public ID for when --run-id is omitted."""
    import secrets
    from datetime import datetime, timezone

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(4)
    return f"run-{ts}-{suffix}"


def _run_persist_review_command(args: argparse.Namespace) -> int:
    """Persist the review queue into a SQLite database."""
    from finance_core.reconciliation.demo_fixture import (
        _load_app_transactions_from_json,
        _structured_rows_to_statements,
    )
    from finance_core.reconciliation.matching import match_batch
    from finance_core.reconciliation.review_persistence import ReviewQueuePersistence
    from finance_core.reconciliation.review_queue import generate_review_queue
    from finance_core.reconciliation.statement_csv import StatementCsvAdapter

    statement_path = Path(args.statement).resolve()
    if not statement_path.exists():
        print(f"Error: statement CSV not found: {statement_path}", file=sys.stderr)
        return 1

    app_txn_path = Path(args.app_transactions).resolve()
    if not app_txn_path.exists():
        print(
            f"Error: app transactions JSON not found: {app_txn_path}",
            file=sys.stderr,
        )
        return 1

    try:
        # -- Guard: refuse live DB --
        _guard_db_path(args.db, "persist-review")
        # -- Open DB and apply migrations --
        db_path = Path(args.db)
        conn = connect_sqlite(db_path, mode=ConnectionMode.APPLICATION)

        try:
            _apply_migrations(conn)

            # -- Parse statement CSV --
            adapter = StatementCsvAdapter()
            csv_result = adapter.parse_file_hardened(statement_path)
            if not csv_result.success:
                print(f"Error: CSV parse errors: {csv_result.errors}", file=sys.stderr)
                return 1
            statements = _structured_rows_to_statements(csv_result.rows)

            # -- Load app transactions --
            app_transactions = _load_app_transactions_from_json(app_txn_path)

            # -- Run matching --
            candidates = match_batch(statements, app_transactions)

            # -- Derive run id --
            run_id = args.run_id or _generate_run_id()

            # -- Generate review queue --
            queue_items, summary = generate_review_queue(
                candidates,
                run_label=run_id,
            )

            # -- Persist --
            rqp = ReviewQueuePersistence(conn)
            persisted_count = rqp.persist_review_queue(
                queue_items,
                run_public_id=run_id,
            )

            # -- Print summary --
            s = summary
            print("Reconciliation Review Queue -- Persist Mode")
            print("===========================================")
            print(f"  Database:                   {db_path}")
            print(f"  Run ID:                     {run_id}")
            print(f"  Statement transactions:     {s.total_statement_transactions}")
            print(f"  App transactions:           {s.total_app_transactions}")
            print(f"  Matched:                    {s.matched_count}")
            print(f"  Needs review:               {s.needs_review_count}")
            print(f"  Missing in app:             {s.missing_in_app_count}")
            print(f"  Missing in statement:       {s.missing_in_statement_count}")
            print(f"  Amount mismatch:            {s.amount_mismatch_count}")
            print(f"  Possible duplicate:         {s.possible_duplicate_count}")
            print(f"  Persisted review queue:     {persisted_count}")
            print(f"  Pending review:             {rqp.count_by_status('pending')}")

        finally:
            conn.close()

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Resolve-review command (Review Queue + Resolution v1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Apply-resolve command (Reconciliation Apply Persistence v1)
# ---------------------------------------------------------------------------


def _run_apply_resolve_command(args: argparse.Namespace) -> int:
    """Run the full flow: import, match, review queue, apply decisions,
    persist apply results, and print audit summary.

    This is a local demo flow only. It never touches database/finance.db.
    """
    from finance_core.reconciliation.apply import ResolutionApplyRuntime
    from finance_core.reconciliation.apply_persistence import ApplyPersistence
    from finance_core.reconciliation.demo_fixture import (
        _load_app_transactions_from_json,
        _structured_rows_to_statements,
    )
    from finance_core.reconciliation.matching import match_batch
    from finance_core.reconciliation.review_queue import generate_review_queue
    from finance_core.reconciliation.run_summary import (
        format_run_summary,
        summarize_apply_results,
    )
    from finance_core.reconciliation.statement_csv import StatementCsvAdapter

    statement_path = Path(args.statement).resolve()
    if not statement_path.exists():
        print(f"Error: statement CSV not found: {statement_path}", file=sys.stderr)
        return 1

    app_txn_path = Path(args.app_transactions).resolve()
    if not app_txn_path.exists():
        print(f"Error: app transactions JSON not found: {app_txn_path}", file=sys.stderr)
        return 1

    decisions_path = Path(args.decisions).resolve()
    if not decisions_path.exists():
        print(f"Error: decisions JSON not found: {decisions_path}", file=sys.stderr)
        return 1

    try:
        # -- Guard: refuse live DB --
        _guard_db_path(args.db, "apply-resolve")

        db_path = Path(args.db)
        conn = connect_sqlite(db_path, mode=ConnectionMode.APPLICATION)

        try:
            _apply_migrations(conn)

            # -- Parse statement CSV --
            adapter = StatementCsvAdapter()
            csv_result = adapter.parse_file_hardened(statement_path)
            if not csv_result.success:
                print(f"Error: CSV parse errors: {csv_result.errors}", file=sys.stderr)
                return 1
            statements = _structured_rows_to_statements(csv_result.rows)

            # -- Load app transactions --
            app_transactions = _load_app_transactions_from_json(app_txn_path)

            # -- Run matching --
            candidates = match_batch(statements, app_transactions)

            # -- Generate review queue --
            queue_items, _summary = generate_review_queue(candidates)

            # -- Load decisions fixture --
            decisions = _load_decisions_from_json(decisions_path)

            # -- Apply decisions through the apply runtime --
            runtime = ResolutionApplyRuntime()
            decision_map = {d.queue_item_id: d for d in decisions}

            results = []
            for item in queue_items:
                decision = decision_map.get(item.queue_item_id)
                if decision is None:
                    continue
                try:
                    result = runtime.apply(item, decision)
                    results.append(result)
                except ApplyConflictError as exc:
                    print(
                        f"Warning: apply failed for {item.queue_item_id}: {exc}",
                        file=sys.stderr,
                    )

            # -- Persist apply results --
            ap = ApplyPersistence(conn)
            persisted_count = ap.persist_apply_results(results)

            # -- Print audit summary --
            run_summary = summarize_apply_results(
                conn,
                known_queue_item_ids=[q.queue_item_id for q in queue_items],
            )

            print("Reconciliation Apply Persistence -- Demo Run")
            print("============================================")
            print(f"  Database:                   {db_path}")
            print(f"  Statement rows:             {len(statements)}")
            print(f"  App transactions:           {len(app_transactions)}")
            print(f"  Queue items generated:      {len(queue_items)}")
            print(f"  Decisions loaded:           {len(decisions)}")
            print(f"  Apply results produced:     {len(results)}")
            print(f"  Apply results persisted:    {persisted_count}")
            print()
            print(format_run_summary(run_summary))

        finally:
            conn.close()

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _run_resolve_review_command(args: argparse.Namespace) -> int:
    """Load pending review items, apply decisions, persist results."""
    from finance_core.reconciliation.resolution_persistence import ResolutionPersistence
    from finance_core.reconciliation.review_persistence import ReviewQueuePersistence

    db_path = Path(args.db).resolve() if args.db else None
    decisions_path = Path(args.decisions).resolve()
    if not decisions_path.exists():
        print(
            f"Error: decisions JSON not found: {decisions_path}",
            file=sys.stderr,
        )
        return 1

    try:
        # -- Guard: refuse live DB --
        _guard_db_path(args.db, "resolve-review")

        if db_path is None or not db_path.exists():
            print(f"Error: database not found: {db_path}", file=sys.stderr)
            return 1
        # -- Open DB --
        conn = connect_sqlite(db_path, mode=ConnectionMode.APPLICATION)

        try:
            rqp = ReviewQueuePersistence(conn)
            rp = ResolutionPersistence(conn)

            # -- Load pending items --
            pending_rows = rqp.list_pending()
            if not pending_rows:
                print("No pending review items found.", file=sys.stderr)
                return 0

            # -- Reconstruct ReviewQueueItem objects from DB rows --
            items: list[ReviewQueueItem] = []
            for row in pending_rows:
                item = _row_to_review_queue_item(row)
                items.append(item)

            # -- Load decisions fixture --
            decisions = _load_decisions_from_json(decisions_path)

            # -- Apply decisions --
            decision_map: dict[str, ResolutionDecision] = {d.queue_item_id: d for d in decisions}

            decisions_loaded = len(decisions)
            decisions_applied = 0
            successful_results = 0
            failed_results = 0

            for item in items:
                decision = decision_map.get(item.queue_item_id)
                if decision is None:
                    continue
                result = rp.apply_resolution(item, decision)
                decisions_applied += 1
                if result.success:
                    successful_results += 1
                else:
                    failed_results += 1

            # -- Gather status counts --
            pending_remaining = rqp.count_by_status("pending")
            resolved_count = rqp.count_by_status("resolved")
            ignored_count = rqp.count_by_status("ignored")
            needs_more_info_count = rqp.count_by_status("needs_more_info")

            # -- Print summary --
            print("Reconciliation Resolution -- Resolve Mode")
            print("==========================================")
            print(f"  Database:                   {db_path}")
            print(f"  Decisions loaded:           {decisions_loaded}")
            print(f"  Decisions applied:          {decisions_applied}")
            print(f"  Successful results:         {successful_results}")
            print(f"  Failed results:             {failed_results}")
            print(f"  Pending remaining:          {pending_remaining}")
            print(f"  Resolved:                   {resolved_count}")
            print(f"  Ignored:                    {ignored_count}")
            print(f"  Needs more info:            {needs_more_info_count}")

        finally:
            conn.close()

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _run_guarded_execution_summaries_command(args: argparse.Namespace) -> int:
    """List persisted guarded apply execution summaries without mutation."""
    from finance_core.reconciliation.apply_execution_review import (
        list_guarded_apply_execution_review_summaries,
    )
    from finance_core.reconciliation.apply_runtime_persistence import (
        GuardedApplyExecutionRepository,
    )
    from finance_core.reconciliation.models import ApplyExecutionStatus

    try:
        _guard_db_path(args.db, "guarded-execution-summaries")
        db_path = Path(args.db).resolve()
        if not db_path.exists():
            print(f"Error: database not found: {db_path}", file=sys.stderr)
            return 1

        status = ApplyExecutionStatus(args.status) if args.status else None
        conn = connect_sqlite(f"file:{db_path}?mode=ro", mode=ConnectionMode.READ_ONLY)

        try:
            repo = GuardedApplyExecutionRepository(conn)
            summaries = list_guarded_apply_execution_review_summaries(
                repo,
                execution_status=status,
                requires_human_review=True if args.requires_human_review else None,
                limit=args.limit,
            )

            print("Guarded Apply Execution Summaries")
            print("=================================")
            print(f"  Database:             {db_path}")
            print(f"  Count:                {len(summaries)}")
            print("  Mode:                 read-only")
            print()

            if not summaries:
                print("No guarded apply execution summaries found.")
                return 0

            for idx, summary in enumerate(summaries, start=1):
                print(f"{idx}. {summary.idempotency_key}")
                print(f"   status={summary.execution_status.value}")
                print(f"   priority={summary.review_priority.value}")
                print(f"   human_review={summary.requires_human_review}")
                print(f"   dry_run={summary.is_dry_run}")
                print(
                    "   operations="
                    f"{summary.operations_executed} executed, "
                    f"{summary.operations_blocked} blocked, "
                    f"{summary.operations_skipped} skipped"
                )
                print(f"   summary={summary.human_summary}")
        finally:
            conn.close()

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconciliation Demo CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -- review subcommand (in-memory) --
    review_parser = subparsers.add_parser(
        "review",
        help="Run the in-memory review queue demo (no SQLite)",
    )
    review_parser.add_argument(
        "--statement",
        required=True,
        help="Path to the statement CSV file.",
    )
    review_parser.add_argument(
        "--app-transactions",
        required=True,
        help="Path to the app-side transaction candidates JSON file.",
    )

    # -- read-only-e2e subcommand (Read-Only E2E Demo CLI v1) --
    e2e_parser = subparsers.add_parser(
        "read-only-e2e",
        help=(
            "Run a read-only end-to-end reconciliation demo from fixtures "
            "(no final mutations, no settlement obligations)."
        ),
    )
    e2e_parser.add_argument(
        "--statement",
        default=str(_DEFAULT_E2E_STATEMENT),
        help=(
            "Path to the sample statement CSV fixture. "
            "Defaults to tests/fixtures/reconciliation/sample_statement.csv."
        ),
    )
    e2e_parser.add_argument(
        "--app-transactions",
        default=str(_DEFAULT_E2E_APP_TRANSACTIONS),
        help=(
            "Path to the sample app-side transaction candidates JSON fixture. "
            "Defaults to tests/fixtures/reconciliation/sample_app_transactions.json."
        ),
    )
    e2e_parser.add_argument(
        "--batch-id",
        default=None,
        help="Deterministic batch public_id (auto-generated when omitted).",
    )
    e2e_parser.add_argument(
        "--run-id",
        default=None,
        help="Deterministic run public_id (auto-generated when omitted).",
    )

    # -- persist-review subcommand (SQLite persistent) --
    persist_parser = subparsers.add_parser(
        "persist-review",
        help="Persist the review queue into a SQLite database",
    )
    persist_parser.add_argument(
        "--statement",
        required=True,
        help="Path to the statement CSV file.",
    )
    persist_parser.add_argument(
        "--app-transactions",
        required=True,
        help="Path to the app-side transaction candidates JSON file.",
    )
    persist_parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database file (must not be database/finance.db).",
    )
    persist_parser.add_argument(
        "--run-id",
        default="",
        help="Optional run public_id for the reconciliation run.",
    )

    # -- apply-resolve subcommand (Reconciliation Apply Persistence v1) --
    apply_parser = subparsers.add_parser(
        "apply-resolve",
        help="Import, match, apply decisions, persist results, print audit summary",
    )
    apply_parser.add_argument(
        "--statement",
        required=True,
        help="Path to the statement CSV file.",
    )
    apply_parser.add_argument(
        "--app-transactions",
        required=True,
        help="Path to the app-side transaction candidates JSON file.",
    )
    apply_parser.add_argument(
        "--decisions",
        required=True,
        help="Path to the resolution decisions JSON fixture.",
    )
    apply_parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database file (must not be database/finance.db).",
    )

    # -- resolve-review subcommand (SQLite persistent) --
    resolve_parser = subparsers.add_parser(
        "resolve-review",
        help="Resolve pending review queue items from a SQLite database",
    )
    resolve_parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database file (must not be database/finance.db).",
    )
    resolve_parser.add_argument(
        "--decisions",
        required=True,
        help="Path to the resolution decisions JSON fixture.",
    )

    # -- guarded-execution-summaries subcommand (read-only operator surface) --
    guarded_summary_parser = subparsers.add_parser(
        "guarded-execution-summaries",
        help="List persisted guarded apply execution summaries without mutation",
    )
    guarded_summary_parser.add_argument(
        "--db",
        required=True,
        help="Path to the SQLite database file (must not be database/finance.db).",
    )
    guarded_summary_parser.add_argument(
        "--status",
        choices=["executed", "blocked", "partially_blocked", "unsupported", "conflict"],
        default=None,
        help="Optional execution status filter.",
    )
    guarded_summary_parser.add_argument(
        "--requires-human-review",
        action="store_true",
        help="Only show summaries requiring human review.",
    )
    guarded_summary_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum summaries to show after operator-priority ordering.",
    )

    # -- demo subcommand (SQLite-backed, also the default when no command) --
    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the SQLite-backed reconciliation demo",
    )
    demo_parser.add_argument(
        "--statement",
        required=True,
        help="Path to the sample statement CSV file.",
    )
    demo_parser.add_argument(
        "--candidates",
        required=True,
        help="Path to the sample app transaction candidates JSON file.",
    )
    demo_parser.add_argument(
        "--batch-id",
        default=None,
        help="Deterministic batch public_id (auto-generated when omitted).",
    )
    demo_parser.add_argument(
        "--run-id",
        default=None,
        help="Deterministic run public_id (auto-generated when omitted).",
    )

    # Keep top-level args for backward compatibility (default = demo mode)
    parser.add_argument(
        "--statement",
        default=None,
        help="Path to the sample statement CSV file.",
    )
    parser.add_argument(
        "--candidates",
        default=None,
        help="Path to the sample app transaction candidates JSON file.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Deterministic batch public_id (auto-generated when omitted).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Deterministic run public_id (auto-generated when omitted).",
    )

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_temp_db() -> sqlite3.Connection:
    """Create a temporary SQLite database authorised for staging writes."""
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="recon_demo_")
    import os

    os.close(fd)
    # Remove the empty file so create_staging_database can create it fresh.
    os.unlink(tmp_path)

    from finance_core.staging_guard import create_staging_database

    return create_staging_database(Path(tmp_path), migration_paths=_MIGRATION_PATHS)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all project migrations to the connection."""
    for index, migration_path in enumerate(_MIGRATION_PATHS):
        if not migration_path.exists():
            print(f"Warning: migration not found: {migration_path}", file=sys.stderr)
            continue
        # Skip migration 009 if row_fingerprint column already exists
        if migration_path.name == "009_statement_import_fingerprint_dedup.sql":
            cols = conn.execute("PRAGMA table_info(statement_transactions)").fetchall()
            if any(c[1] == "row_fingerprint" for c in cols):
                continue

        # Skip migration 010 if amount_direction column already exists
        if migration_path.name == "010_statement_amount_direction_persistence.sql":
            cols = conn.execute("PRAGMA table_info(statement_transactions)").fetchall()
            if any(c[1] == "amount_direction" for c in cols):
                continue
        apply_migration_paths(conn, _MIGRATION_PATHS[index : index + 1])
    conn.commit()


def _guard_db_path(db_arg: str | None, command_name: str) -> None:
    """Fail if --db is omitted or points to database/finance.db."""
    if db_arg is None:
        raise ValueError(
            f"Error: --db is required for '{command_name}'. "
            "The CLI never defaults to database/finance.db."
        )

    resolved = Path(db_arg).resolve()
    if resolved == _LIVE_DB_PATH:
        raise ValueError(
            f"Error: refusing to use live database at {resolved}. Use a temporary database instead."
        )


def _row_to_review_queue_item(row: sqlite3.Row) -> ReviewQueueItem:
    """Reconstruct a ReviewQueueItem from a reconciliation_review_queue row.

    This is a lossy reconstruction from persisted data, sufficient for
    the resolution workflow (which only needs queue_item_id, issue_type,
    and candidate metadata).
    """
    import json as _json
    from decimal import Decimal as _Decimal

    from finance_core.reconciliation.models import (
        AppTransaction,
        IssueType,
        MatchStatus,
        ReasonCode,
        ReconciliationCandidate,
        StatementAmountDirection,
        StatementTransaction,
        SuggestedAction,
    )

    evidence = _json.loads(row["evidence_json"] or "{}")
    reason_codes_raw = _json.loads(row["reason_codes_json"] or "[]")
    reason_codes = tuple(
        ReasonCode(r) if r in {rc.value for rc in ReasonCode} else ReasonCode.NEEDS_REVIEW
        for r in reason_codes_raw
    )

    # Reconstruct a minimal StatementTransaction from evidence
    from datetime import date as _date

    _stmt_txn_date = None
    if evidence.get("statement_txn_date"):
        _stmt_txn_date = _date.fromisoformat(evidence["statement_txn_date"])
    _stmt_posted_date = None
    if evidence.get("statement_posted_date"):
        _stmt_posted_date = _date.fromisoformat(evidence["statement_posted_date"])
    stmt = StatementTransaction(
        transaction_date=_stmt_txn_date,
        posted_date=_stmt_posted_date,
        merchant_raw=evidence.get("statement_merchant", ""),
        amount=_Decimal(evidence["statement_amount"]) if evidence.get("statement_amount") else None,
        currency=evidence.get("statement_currency"),
        statement_row_reference=row["statement_transaction_ref"],
        amount_direction=StatementAmountDirection(evidence["statement_direction"])
        if evidence.get("statement_direction")
        else None,
        raw_amount=evidence.get("original_amount_text"),
    )

    # Reconstruct a minimal AppTransaction from evidence
    app: AppTransaction | None = None
    if evidence.get("app_txn_id"):
        _app_txn_date = None
        if evidence.get("app_txn_date"):
            _app_txn_date = _date.fromisoformat(evidence["app_txn_date"])
        app = AppTransaction(
            app_txn_id=evidence["app_txn_id"],
            transaction_date=_app_txn_date,  # type: ignore[arg-type]
            merchant=evidence.get("app_merchant", ""),
            amount=_Decimal(evidence.get("app_amount", "0")),
            currency=evidence.get("app_currency", ""),
            transaction_type=evidence.get("candidate_transaction_type"),
        )

    confidence = _Decimal(row["confidence_score"] or "0")

    candidate = ReconciliationCandidate(
        statement=stmt,
        best_app_transaction=app,
        match_status=MatchStatus(_status_from_issue(row["issue_type"])),
        reason_codes=reason_codes,
        issue_type=IssueType(row["issue_type"]),
        confidence_score=confidence,
        candidate_id=row["candidate_id"],
    )

    return ReviewQueueItem(
        queue_item_id=row["public_id"],
        candidate=candidate,
        issue_type=IssueType(row["issue_type"]),
        suggested_action=SuggestedAction(row["suggested_action"]),
        reason_codes=reason_codes,
        evidence_summary=f"reconstructed from persisted row {row['public_id']}",
        priority=row["priority"],
    )


def _status_from_issue(issue_type: str) -> str:
    """Map issue_type back to a MatchStatus for reconstruction."""
    mapping: dict[str, str] = {
        "matched": "matched",
        "amount_mismatch": "amount_mismatch",
        "currency_mismatch": "currency_mismatch",
        "date_mismatch": "date_mismatch",
        "merchant_mismatch": "merchant_mismatch",
        "missing_in_app": "no_match",
        "missing_in_statement": "no_match",
        "possible_duplicate": "possible_duplicate",
        "needs_review": "needs_review",
    }
    return mapping.get(issue_type, "needs_review")


def _load_decisions_from_json(path: Path) -> list[ResolutionDecision]:
    """Load ResolutionDecision objects from a JSON fixture file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    raw_decisions: list[dict[str, str]] = data.get("decisions", [])
    decisions: list[ResolutionDecision] = []
    for raw in raw_decisions:
        decisions.append(
            ResolutionDecision(
                decision_id=raw["decision_id"],
                queue_item_id=raw["queue_item_id"],
                action=ResolutionAction(raw["action"]),
                note=raw.get("note", ""),
                reviewer=raw.get("reviewer", "human"),
            )
        )
    return decisions


if __name__ == "__main__":
    sys.exit(main())
