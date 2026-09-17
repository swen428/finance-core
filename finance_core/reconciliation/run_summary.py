"""Reconciliation Run Summary v1 -- deterministic summary over persisted
reconciliation apply results.

Provides a ``RunSummary`` dataclass plus ``summarize_apply_results`` that
scans the ``reconciliation_apply_results`` table and returns structured
counts suitable for CLI output, Metabase readiness, or audit reporting.

Design properties:
- Read-only: never writes or mutates data.
- Deterministic: same rows produce the same summary.
- Does not touch database/finance.db or live data.
- Does NOT create, update, delete, merge, or overwrite final financial
  transaction records.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class RunSummary:
    """Deterministic summary over a set of persisted apply results."""

    total_apply_results: int = 0
    count_by_action: dict[str, int] = field(default_factory=dict)
    count_by_success: dict[str, int] = field(default_factory=dict)
    count_proposal_actions: int = 0
    count_audit_only_actions: int = 0
    count_needs_more_info: int = 0
    count_ignored: int = 0
    count_by_reviewer: dict[str, int] = field(default_factory=dict)
    earliest_applied_at: str = ""
    latest_applied_at: str = ""
    unresolved_queue_items: list[str] = field(default_factory=list)

    @property
    def count_successful(self) -> int:
        return self.count_by_success.get("success", 0)

    @property
    def count_failed(self) -> int:
        return self.count_by_success.get("failed", 0)


# ---------------------------------------------------------------------------
# Action classification constants
# ---------------------------------------------------------------------------

_PROPOSAL_ACTIONS: set[str] = {
    "adjust_app_transaction",
    "create_missing_app_transaction",
}

_AUDIT_ONLY_ACTIONS: set[str] = {
    "mark_duplicate",
    "mark_statement_only",
    "confirm_match",
}


def summarize_apply_results(
    conn: sqlite3.Connection,
    *,
    known_queue_item_ids: Sequence[str] | None = None,
) -> RunSummary:
    """Read all rows from ``reconciliation_apply_results`` and produce a
    deterministic ``RunSummary``.

    Parameters
    ----------
    conn:
        An open ``sqlite3.Connection`` with the migration 008 schema applied.
    known_queue_item_ids:
        Optional list of queue item IDs that are expected to be resolved.
        Items not found in the apply results table are reported as unresolved.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM reconciliation_apply_results ORDER BY applied_at ASC"
    ).fetchall()

    count_by_action: dict[str, int] = {}
    count_success = 0
    count_failed = 0
    count_proposal = 0
    count_audit_only = 0
    count_nmi = 0
    count_ignored = 0
    count_by_reviewer: dict[str, int] = {}
    earliest = ""
    latest = ""

    for row in rows:
        action = row["action"]
        count_by_action[action] = count_by_action.get(action, 0) + 1

        if row["success"]:
            count_success += 1
        else:
            count_failed += 1

        if action in _PROPOSAL_ACTIONS:
            count_proposal += 1
        elif action in _AUDIT_ONLY_ACTIONS:
            count_audit_only += 1

        if action == "needs_more_info":
            count_nmi += 1
        elif action == "ignore":
            count_ignored += 1

        reviewer = row["reviewer"]
        if reviewer is not None:
            count_by_reviewer[reviewer] = count_by_reviewer.get(reviewer, 0) + 1

        applied_at = row["applied_at"]
        if not earliest or applied_at < earliest:
            earliest = applied_at
        if not latest or applied_at > latest:
            latest = applied_at

    # Detect unresolved queue items
    resolved_ids: set[str] = set()
    for row in rows:
        resolved_ids.add(row["queue_item_id"])

    unresolved: list[str] = []
    if known_queue_item_ids:
        unresolved = [qid for qid in known_queue_item_ids if qid not in resolved_ids]

    return RunSummary(
        total_apply_results=len(rows),
        count_by_action=dict(sorted(count_by_action.items())),
        count_by_success={"success": count_success, "failed": count_failed},
        count_proposal_actions=count_proposal,
        count_audit_only_actions=count_audit_only,
        count_needs_more_info=count_nmi,
        count_ignored=count_ignored,
        count_by_reviewer=dict(sorted(count_by_reviewer.items())),
        earliest_applied_at=earliest,
        latest_applied_at=latest,
        unresolved_queue_items=unresolved,
    )


def format_run_summary(summary: RunSummary) -> str:
    """Format a RunSummary as a plain-text block for CLI display."""
    lines = [
        "Reconciliation Apply Run Summary",
        "================================",
        f"  Total apply results:        {summary.total_apply_results}",
        f"  Successful:                 {summary.count_successful}",
        f"  Failed:                     {summary.count_failed}",
    ]

    if summary.count_by_action:
        lines.append("")
        lines.append("  By action:")
        for action, count in summary.count_by_action.items():
            lines.append(f"    {action:<30} {count}")

    lines.append("")
    lines.append("  Classification:")
    lines.append(f"    proposal actions:          {summary.count_proposal_actions}")
    lines.append(f"    audit-only actions:        {summary.count_audit_only_actions}")
    lines.append(f"    needs_more_info:           {summary.count_needs_more_info}")
    lines.append(f"    ignored:                   {summary.count_ignored}")

    if summary.count_by_reviewer:
        lines.append("")
        lines.append("  By reviewer:")
        for reviewer, count in summary.count_by_reviewer.items():
            lines.append(f"    {reviewer:<30} {count}")

    if summary.earliest_applied_at:
        lines.append("")
        lines.append("  Time range:")
        lines.append(f"    earliest: {summary.earliest_applied_at}")
        lines.append(f"    latest:   {summary.latest_applied_at}")

    if summary.unresolved_queue_items:
        lines.append("")
        lines.append(f"  Unresolved queue items:     {len(summary.unresolved_queue_items)}")
        for qid in summary.unresolved_queue_items:
            lines.append(f"    - {qid}")

    return "\n".join(lines)


__all__ = [
    "RunSummary",
    "summarize_apply_results",
    "format_run_summary",
]
