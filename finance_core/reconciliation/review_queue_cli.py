"""Reconciliation Review Queue Preview CLI v1.

A lightweight, read-only CLI that previews persisted reconciliation review
queue items using
``summarize_reconciliation_review_queue_from_repository()``.

Usage::

    python -m finance_core.reconciliation.review_queue_cli --db /tmp/recon_preview.db

The CLI is a *preview* only.  It opens the database in read-only mode
(``file:...?mode=ro``) and never writes, resolves, applies, mutates, or
finalises any record.  It refuses to operate on ``database/finance.db``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.review_queue import (
    ReconciliationReviewQueueItem,
    summarize_reconciliation_review_queue_from_repository,
)
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

_LIVE_DB_PATH = LIVE_DB_PATH.resolve()


def main(argv: list[str] | None = None) -> int:
    """Run the review queue preview CLI. Returns 0 on success."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _run_preview_command(args)


def _run_preview_command(args: argparse.Namespace) -> int:
    """Load and print review queue preview items without mutation."""
    try:
        _guard_db_path(args.db)
        db_path = Path(args.db).resolve()
        if not db_path.exists():
            print(f"Error: database not found: {db_path}", file=sys.stderr)
            return 1

        conn = connect_sqlite(f"file:{db_path}?mode=ro", mode=ConnectionMode.READ_ONLY)

        try:
            items = summarize_reconciliation_review_queue_from_repository(conn)
        finally:
            conn.close()

        if args.json:
            import json

            # Pure JSON payload -- no banner, so the output is directly
            # parseable by callers and tests.
            print(json.dumps([_item_to_dict(item) for item in items], indent=2))
            return 0

        print("Reconciliation Review Queue Preview")
        print("===================================")
        print(f"  Database:  {db_path}")
        print(f"  Count:     {len(items)}")
        print("  Mode:      read-only")
        print()

        if not items:
            print("No review queue items require attention.")
            return 0

        _print_table(items)
    except Exception as exc:  # noqa: BLE001 - CLI surface reports all errors
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _print_table(items: list[ReconciliationReviewQueueItem]) -> None:
    """Print a stable, human-readable table of preview items."""
    # Fixed column widths keep output stable for tests.
    for idx, item in enumerate(items, start=1):
        print(f"{idx}. {item.review_id}")
        print(f"   source={item.source_type}:{item.source_id}")
        print(f"   status={item.status}")
        print(f"   priority={item.priority}")
        print(f"   reason={item.reason}")
        print(f"   evidence_count={item.evidence_count}")
        print(f"   blocking_count={item.blocking_count}")
        print(f"   created_at={item.created_at}")
        print(f"   summary={item.summary}")


def _item_to_dict(item: ReconciliationReviewQueueItem) -> dict[str, object]:
    """Serialise a preview item to a plain dict for JSON output."""
    return {
        "review_id": item.review_id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "status": item.status,
        "priority": item.priority,
        "reason": item.reason,
        "evidence_count": item.evidence_count,
        "blocking_count": item.blocking_count,
        "created_at": item.created_at,
        "summary": item.summary,
    }


def _guard_db_path(db_arg: str | None) -> None:
    """Fail if --db is omitted or points to database/finance.db."""
    if db_arg is None:
        raise ValueError("Error: --db is required. The CLI never defaults to database/finance.db.")

    resolved = Path(db_arg).resolve()
    if resolved == _LIVE_DB_PATH:
        raise ValueError(
            f"Error: refusing to use live database at {resolved}. Use a temporary database instead."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview the reconciliation review queue (read-only). "
            "Does not resolve, apply, or mutate any records."
        ),
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to a migrated SQLite database (never database/finance.db).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the preview as JSON instead of the default human-readable table.",
    )
    return parser


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
