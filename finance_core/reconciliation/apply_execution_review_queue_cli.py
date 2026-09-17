"""Reconciliation Apply Execution Review Queue CLI v1.

A lightweight, read-only CLI that displays the reconciliation apply
execution review queue from persisted guarded apply execution data.

Usage::

    python -m finance_core.reconciliation.apply_execution_review_queue_cli \
        --db /tmp/recon_temp.db

The CLI is a *preview* only. It opens the database in read-only mode
(``file:...?mode=ro``) and never writes, resolves, applies, mutates, or
finalises any record. It refuses to operate on ``database/finance.db``.

Filters::

    --requires-human-review true|false
    --execution-status BLOCKED|PARTIALLY_BLOCKED|EXECUTED|UNSUPPORTED|CONFLICT
    --limit N
    --format text|json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finance_core.reconciliation.apply_execution_review_queue import (
    build_apply_execution_review_queue,
)
from finance_core.reconciliation.apply_runtime_persistence import (
    GuardedApplyExecutionRepository,
)
from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.models import ApplyExecutionStatus
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

_LIVE_DB_PATH = LIVE_DB_PATH.resolve()

_VALID_EXECUTION_STATUSES: set[str] = {s.value for s in ApplyExecutionStatus}


def main(argv: list[str] | None = None) -> int:
    """Run the apply execution review queue CLI. Returns 0 on success."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        return _run_preview_command(args)
    except SystemExit as exc:
        if exc.code is not None and isinstance(exc.code, int):
            return exc.code
        return 2


def _run_preview_command(args: argparse.Namespace) -> int:
    """Load and print apply execution review queue entries without mutation."""
    try:
        _guard_db_path(args.db)
        db_path = Path(args.db).resolve()
        if not db_path.exists():
            print(f"Error: database not found: {db_path}", file=sys.stderr)
            return 1

        conn = connect_sqlite(f"file:{db_path}?mode=ro", mode=ConnectionMode.READ_ONLY)

        try:
            repo = GuardedApplyExecutionRepository(conn)
            queue = build_apply_execution_review_queue(
                repo,
                requires_human_review=args.requires_human_review,
                execution_status=args.execution_status,
                limit=args.limit,
            )
        finally:
            conn.close()

        if args.format == "json":
            payload = _queue_to_json(queue)
            print(json.dumps(payload, indent=2))
            return 0

        print("Reconciliation Apply Execution Review Queue")
        print("===========================================")
        print(f"  Database:  {db_path}")
        print(f"  Count:     {len(queue)}")
        print("  Mode:      read-only")
        if args.requires_human_review is not None:
            print(f"  Filter:    requires_human_review={args.requires_human_review}")
        if args.execution_status is not None:
            print(f"  Filter:    execution_status={args.execution_status.value}")
        if args.limit is not None:
            print(f"  Filter:    limit={args.limit}")
        print()

        if not queue:
            print("No apply execution review queue entries require attention.")
            return 0

        _print_table(queue)
    except Exception as exc:  # noqa: BLE001 - CLI surface reports all errors
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _print_table(queue: tuple) -> None:
    """Print a stable, human-readable table of review queue entries."""
    for idx, entry in enumerate(queue, start=1):
        print(f"{idx}. {entry.execution_id}")
        print(f"   plan_id={entry.plan_id}")
        print(f"   idempotency_key={entry.idempotency_key}")
        print(f"   execution_status={entry.execution_status.value}")
        print(f"   review_priority={entry.review_priority.value}")
        print(f"   requires_human_review={entry.requires_human_review}")
        print(f"   is_blocking={entry.is_blocking}")
        print(f"   is_partially_blocked={entry.is_partially_blocked}")
        print(f"   operations_executed={entry.operations_executed}")
        print(f"   operations_blocked={entry.operations_blocked}")
        print(f"   operations_skipped={entry.operations_skipped}")
        print(f"   total_operations={entry.total_operations}")
        print(f"   block_reason={entry.block_reason or '(none)'}")
        print(f"   reason_codes={list(entry.reason_codes) if entry.reason_codes else '(none)'}")
        print(f"   is_dry_run={entry.is_dry_run}")
        print(f"   executed_at={entry.executed_at}")
        mt = list(entry.mutation_types) if entry.mutation_types else "(none)"
        print(f"   mutation_types={mt}")
        gdr = list(entry.guard_decision_refs) if entry.guard_decision_refs else "(none)"
        print(f"   guard_decision_refs={gdr}")
        print(f"   operation_refs={list(entry.operation_refs)}")
        print(f"   human_summary={entry.human_summary}")


def _queue_to_json(queue: tuple) -> list[dict[str, object]]:
    """Serialize review queue entries to a list of plain dicts for JSON output."""
    result: list[dict[str, object]] = []
    for entry in queue:
        result.append(
            {
                "execution_id": entry.execution_id,
                "plan_id": entry.plan_id,
                "idempotency_key": entry.idempotency_key,
                "execution_status": entry.execution_status.value,
                "review_priority": entry.review_priority.value,
                "requires_human_review": entry.requires_human_review,
                "is_blocking": entry.is_blocking,
                "is_partially_blocked": entry.is_partially_blocked,
                "operations_executed": entry.operations_executed,
                "operations_blocked": entry.operations_blocked,
                "operations_skipped": entry.operations_skipped,
                "total_operations": entry.total_operations,
                "block_reason": entry.block_reason,
                "reason_codes": list(entry.reason_codes),
                "is_dry_run": entry.is_dry_run,
                "executed_at": entry.executed_at,
                "mutation_types": list(entry.mutation_types),
                "guard_decision_refs": list(entry.guard_decision_refs),
                "operation_refs": list(entry.operation_refs),
                "human_summary": entry.human_summary,
            }
        )
    return result


def _guard_db_path(db_arg: str | None) -> None:
    """Fail if --db is omitted or points to database/finance.db."""
    if db_arg is None:
        raise ValueError("Error: --db is required. The CLI never defaults to database/finance.db.")

    resolved = Path(db_arg).resolve()
    if resolved == _LIVE_DB_PATH:
        raise ValueError(
            f"Error: refusing to use live database at {resolved}. Use a temporary database instead."
        )


def _parse_requires_human_review(value: str) -> bool | None:
    """Parse --requires-human-review into bool or None ("all")."""
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "all":
        return None
    raise argparse.ArgumentTypeError(
        f"Invalid requires_human_review: {value!r}. Expected true, false, or all."
    )


def _parse_execution_status(value: str) -> ApplyExecutionStatus:
    """Parse --execution-status into an ApplyExecutionStatus enum value."""
    try:
        return ApplyExecutionStatus(value.lower())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid execution_status: {value!r}. "
            f"Valid values: {', '.join(sorted(_VALID_EXECUTION_STATUSES))}"
        ) from None


def _parse_limit(value: str) -> int:
    """Parse --limit into a non-negative integer."""
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid limit: {value!r}. Expected a non-negative integer."
        ) from None
    if limit < 0:
        raise argparse.ArgumentTypeError(f"Invalid limit: {value}. Must be non-negative.")
    return limit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Display the reconciliation apply execution review queue (read-only). "
            "Does not resolve, apply, or mutate any records."
        ),
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to a migrated SQLite database (never database/finance.db).",
    )
    parser.add_argument(
        "--requires-human-review",
        type=_parse_requires_human_review,
        default=None,
        help="Filter by requires_human_review: true, false, or all (default: all).",
    )
    parser.add_argument(
        "--execution-status",
        type=_parse_execution_status,
        default=None,
        help="Filter by execution status (e.g. BLOCKED, PARTIALLY_BLOCKED, EXECUTED).",
    )
    parser.add_argument(
        "--limit",
        type=_parse_limit,
        default=None,
        help="Maximum number of entries to display.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text (default) or json.",
    )
    return parser


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
