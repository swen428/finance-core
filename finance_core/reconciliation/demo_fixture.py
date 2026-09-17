"""Reconciliation Demo Fixture v1 -- ties together CSV import, in-memory
candidate creation, reconciliation service run, and review queue inspection
into a single callable workflow.

Design properties:
- Wraps an externally-provided ``sqlite3.Connection`` (temp-db only in demo mode).
- Never touches ``database/finance.db``.
- Uses the existing ``StatementCsvAdapter``, ``StatementImporter``,
  ``ReconciliationService``, and ``ReconciliationReviewQueue``.
- Returns structured ``DemoResult`` for CLI or test consumption.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    InternalCandidate,
    IssueType,
    ReconciliationSummary,
    ReviewPriority,
    ReviewQueueItem,
    StatementTransaction,
)
from finance_core.reconciliation.repository import ReconciliationRepository
from finance_core.reconciliation.review_queue import (
    ReconciliationReviewQueue,
    ReviewQueueEntry,
    generate_review_queue,
)
from finance_core.reconciliation.service import (
    ReconciliationRunSummary,
    ReconciliationService,
)
from finance_core.reconciliation.statement_csv import StatementCsvAdapter
from finance_core.reconciliation.statement_import import (
    StatementImporter,
    StructuredStatementRow,
)
from finance_core.runtime_paths import live_database_path

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoResult:
    """Structured result from a completed reconciliation demo run."""

    batch_id: int
    batch_public_id: str
    run_public_id: str
    run_summary: ReconciliationRunSummary
    review_entries: list[ReviewQueueEntry] = field(default_factory=list)
    total_statement_rows: int = 0
    csv_rows_imported: int = 0
    candidates_loaded: int = 0

    @property
    def summary_table(self) -> str:
        """Plain-text summary table for CLI display."""
        s = self.run_summary
        lines = [
            "Reconciliation Demo Run Summary",
            "===============================",
            f"  Batch public ID:         {self.batch_public_id}",
            f"  Run public ID:           {self.run_public_id}",
            f"  CSV rows read:           {self.csv_rows_imported}",
            f"  Statement rows imported: {self.total_statement_rows}",
            f"  App candidates loaded:   {self.candidates_loaded}",
            "",
            "  Results:",
            f"    matched:              {s.matched_count}",
            f"    amount_mismatch:      {s.amount_mismatch_count}",
            f"    date_mismatch:        {s.date_mismatch_count}",
            f"    currency_mismatch:    {s.currency_mismatch_count}",
            f"    merchant_mismatch:    {s.merchant_mismatch_count}",
            f"    no_match:             {s.no_match_count}",
            f"    possible_duplicate:   {s.possible_duplicate_count}",
            f"    ambiguous:            {s.ambiguous_count}",
            f"    needs_review:         {s.needs_review_count}",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class ReviewTableRow:
    """Flattened review queue row for display."""

    status: str
    statement_merchant: str
    statement_amount: str
    statement_date: str
    app_merchant: str
    app_amount: str
    app_date: str
    reason: str
    suggested_action: str


# ---------------------------------------------------------------------------
# Suggested actions mapping
# ---------------------------------------------------------------------------

_SUGGESTED_ACTIONS: dict[str, str] = {
    "matched": "No action needed -- automatic match",
    "no_match": "Review manually -- no app transaction found",
    "amount_mismatch": "Verify correct amount -- statement and app differ",
    "currency_mismatch": "Check currency -- statement and app differ",
    "date_mismatch": "Verify transaction date -- outside tolerance window",
    "merchant_mismatch": "Check merchant name -- could not match",
    "possible_duplicate": "Resolve duplicate -- multiple app records match",
    "ambiguous": "Manual review required -- multiple candidates matched",
    "needs_review": "Manual review required",
}

# Order: most important first
_REVIEW_ORDER: dict[str, int] = {
    "amount_mismatch": 0,
    "possible_duplicate": 1,
    "ambiguous": 2,
    "currency_mismatch": 3,
    "date_mismatch": 4,
    "merchant_mismatch": 5,
    "needs_review": 6,
    "no_match": 7,
    "matched": 8,
}

# ---------------------------------------------------------------------------
# Core fixture
# ---------------------------------------------------------------------------


def run_demo_reconciliation(
    conn: sqlite3.Connection,
    csv_path: str | Path,
    candidates: Sequence[InternalCandidate] | None = None,
    candidate_json_path: str | Path | None = None,
    *,
    source_type: str = "manual_test_fixture",
    batch_public_id: str | None = None,
    run_public_id: str | None = None,
) -> DemoResult:
    """Run end-to-end reconciliation demo against a temp SQLite database.

    Parameters
    ----------
    conn:
        An open ``sqlite3.Connection`` to a **temporary** database with
        the reconciliation persistence schema already migrated.
    csv_path:
        Path to a CSV statement file.
    candidates:
        In-memory ``InternalCandidate`` sequence.  Provide this **or**
        ``candidate_json_path``, not both.
    candidate_json_path:
        Path to a JSON file of candidate records.  Provide this **or**
        ``candidates``, not both.
    source_type:
        Source type label for the import batch.
    batch_public_id:
        Deterministic public_id for the import batch.  Auto-generated
        when ``None``.
    run_public_id:
        Deterministic public_id for the reconciliation run.
        Auto-generated when ``None``.

    Returns
    -------
    DemoResult with run summary and review queue entries.
    """
    conn.row_factory = sqlite3.Row

    # --- Guard: never touch live DB ---
    _assert_temp_db(conn)

    # --- Load candidates ---
    if candidates is not None and candidate_json_path is not None:
        raise ValueError("Provide candidates or candidate_json_path, not both")
    if candidate_json_path is not None:
        candidates = _load_candidates_from_json(candidate_json_path)
    if candidates is None:
        raise ValueError("No candidates provided")

    # --- Step 1: Parse CSV ---
    adapter = StatementCsvAdapter()
    csv_result = adapter.parse_file_hardened(Path(csv_path))
    if not csv_result.success:
        raise ValueError(f"CSV parse errors: {csv_result.errors}")

    # --- Step 2: Import into temp DB ---
    importer = StatementImporter(conn)
    if batch_public_id is None:
        batch_public_id = f"demo-batch-{csv_result.rows[0].merchant_raw}-{len(csv_result.rows)}rows"
    batch = importer.import_rows(
        csv_result.rows,
        source_type=source_type,
        public_id=batch_public_id,
        source_file_path=str(Path(csv_path)),
        source_file_hash=csv_result.source_content_hash,
    )

    # --- Step 3: Run reconciliation ---
    repo = ReconciliationRepository(conn)
    service = ReconciliationService(repo)
    if run_public_id is None:
        run_public_id = f"demo-run-{batch.batch_id}"

    run_summary = service.run_reconciliation_for_batch(
        batch_id=batch.batch_id,
        candidates=list(candidates),
        run_public_id=run_public_id,
        matcher_version="demo-v1",
    )

    # --- Step 4: Build review queue ---
    queue = ReconciliationReviewQueue(conn)
    review_entries = queue.get_unmatched(run_id=run_summary.run_id)

    return DemoResult(
        batch_id=batch.batch_id,
        batch_public_id=batch.public_id,
        run_public_id=run_public_id,
        run_summary=run_summary,
        review_entries=review_entries,
        total_statement_rows=batch.row_count,
        csv_rows_imported=len(csv_result.rows),
        candidates_loaded=len(candidates),
    )


def build_review_entries_sorted(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[ReviewQueueEntry]:
    """Return review queue entries sorted by priority (most important first).

    Uses the existing ``ReconciliationReviewQueue`` to fetch all unmatched
    entries, then sorts them by review priority.
    """
    queue = ReconciliationReviewQueue(conn)
    entries = queue.get_unmatched(run_id=run_id)
    entries.sort(key=lambda e: _REVIEW_ORDER.get(e.match_status, 99))
    return entries


def build_review_table_rows(
    entries: list[ReviewQueueEntry],
) -> list[ReviewTableRow]:
    """Convert review queue entries into display-ready rows."""
    rows: list[ReviewTableRow] = []
    for entry in entries:
        evidence = entry.evidence
        status = entry.match_status
        reason_codes = ", ".join(entry.reason_codes) if entry.reason_codes else "none"
        suggested = _SUGGESTED_ACTIONS.get(status, "Review manually")

        stmt_merchant = evidence.get("statement_merchant", "?")
        stmt_amount = evidence.get("statement_amount", "?")
        stmt_date = (
            evidence.get("statement_txn_date") or evidence.get("statement_posted_date") or "?"
        )

        app_merchant = evidence.get("candidate_merchant", "-")
        app_amount = evidence.get("candidate_amount", "-")
        app_date = evidence.get("candidate_txn_date", "-")

        rows.append(
            ReviewTableRow(
                status=status,
                statement_merchant=str(stmt_merchant) if stmt_merchant else "?",
                statement_amount=str(stmt_amount) if stmt_amount else "?",
                statement_date=str(stmt_date) if stmt_date else "?",
                app_merchant=str(app_merchant) if app_merchant else "-",
                app_amount=str(app_amount) if app_amount else "-",
                app_date=str(app_date) if app_date else "-",
                reason=reason_codes,
                suggested_action=suggested,
            )
        )
    return rows


def format_review_table(rows: list[ReviewTableRow]) -> str:
    """Format review table rows as a plain-text table."""
    if not rows:
        return "No review items.\n"

    status_w = max(max(len(r.status) for r in rows), 18)
    stmt_w = max(max(len(r.statement_merchant) + len(" (SGD 000.00)") for r in rows), 28)
    app_w = max(max(len(r.app_merchant) + len(" (SGD 000.00)") for r in rows), 28)
    reason_w = max(max(len(r.reason) for r in rows), 30)
    action_w = max(max(len(r.suggested_action) for r in rows), 40)

    header = (
        f"  {'Status':<{status_w}}  {'Statement':<{stmt_w}}  "
        f"{'App Record':<{app_w}}  {'Reason':<{reason_w}}  "
        f"{'Suggested Action':<{action_w}}"
    )
    sep = "-" * len(header)

    lines = [sep, header, sep]
    for r in rows:
        stmt_cell = f"{r.statement_merchant} (SGD {r.statement_amount})"
        app_cell = f"{r.app_merchant} (SGD {r.app_amount})" if r.app_merchant != "-" else "-"
        lines.append(
            f"  {r.status:<{status_w}}  {stmt_cell:<{stmt_w}}  "
            f"{app_cell:<{app_w}}  {r.reason:<{reason_w}}  "
            f"{r.suggested_action:<{action_w}}"
        )
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Read-Only E2E Demo CLI v1 -- thin orchestration summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadOnlyE2EDemoSummary:
    """Structured summary for the read-only end-to-end reconciliation demo.

    Built by :func:`build_read_only_e2e_summary` from a completed
    :class:`DemoResult`.  All fields are derived deterministically from the
    demo run; nothing is time-dependent, random, or AI-generated.

    This is a read-only summary only -- it never describes final financial
    record mutations or settlement obligation creation, both of which are
    explicitly zero in a read-only demo run.
    """

    statement_fixture_path: str
    app_transactions_fixture_path: str
    statement_rows_parsed: int
    app_transactions_loaded: int
    candidate_matches: int
    review_items: int
    run_public_id: str
    batch_public_id: str
    match_status_counts: dict[str, int]


def build_read_only_e2e_summary(
    statement_fixture_path: str | Path,
    app_transactions_fixture_path: str | Path,
    result: DemoResult,
    review_item_count: int,
) -> ReadOnlyE2EDemoSummary:
    """Build a deterministic read-only E2E demo summary from a demo result.

    Parameters
    ----------
    statement_fixture_path:
        Path to the statement CSV fixture used by the demo run.
    app_transactions_fixture_path:
        Path to the app-side candidate JSON fixture used by the demo run.
    result:
        The completed :class:`DemoResult` from
        :func:`run_demo_reconciliation`.
    review_item_count:
        Number of review queue items surfaced for the run (non-matched
        match results).  Computed by the caller from the existing review
        queue helpers.
    """
    return ReadOnlyE2EDemoSummary(
        statement_fixture_path=str(Path(statement_fixture_path)),
        app_transactions_fixture_path=str(Path(app_transactions_fixture_path)),
        statement_rows_parsed=result.total_statement_rows,
        app_transactions_loaded=result.candidates_loaded,
        candidate_matches=result.run_summary.matched_count,
        review_items=review_item_count,
        run_public_id=result.run_public_id,
        batch_public_id=result.batch_public_id,
        match_status_counts=dict(result.run_summary.match_status_counts),
    )


def format_read_only_e2e_summary(summary: ReadOnlyE2EDemoSummary) -> str:
    """Format a read-only E2E demo summary as deterministic plain text.

    The output is stable across runs given the same fixture inputs:

        READ-ONLY E2E DEMO
        Statement fixture: ...
        App transactions fixture: ...
        Statement rows parsed: X
        App transactions loaded: Y
        Candidate matches: Z
        Review items: N
        Final mutations executed: 0
        Settlement obligations created: 0
    """
    s = summary
    lines: list[str] = [
        "READ-ONLY E2E DEMO",
        f"Statement fixture: {s.statement_fixture_path}",
        f"App transactions fixture: {s.app_transactions_fixture_path}",
        f"Statement rows parsed: {s.statement_rows_parsed}",
        f"App transactions loaded: {s.app_transactions_loaded}",
        f"Candidate matches: {s.candidate_matches}",
        f"Review items: {s.review_items}",
        "Final mutations executed: 0",
        "Settlement obligations created: 0",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_candidates_from_json(path: str | Path) -> list[InternalCandidate]:
    """Load InternalCandidate objects from a JSON fixture file."""
    with open(Path(path), "r", encoding="utf-8") as fh:
        data = json.load(fh)

    raw_candidates: list[dict[str, str]] = data.get("candidates", [])
    candidates: list[InternalCandidate] = []
    for raw in raw_candidates:
        candidates.append(
            InternalCandidate(
                internal_id=raw["internal_id"],
                transaction_date=date.fromisoformat(raw["transaction_date"]),
                merchant=raw["merchant"],
                amount=Decimal(raw["amount"]),
                currency=raw["currency"],
                source_type=raw.get("source_type"),
                source_channel=raw.get("source_channel"),
                transaction_type=raw.get("transaction_type", "expense"),
            )
        )
    return candidates


def _assert_temp_db(conn: sqlite3.Connection) -> None:
    """Raise ValueError if the connection points to the live database."""
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not db_path:
        return
    db_path = Path(db_path).resolve()
    live_path = live_database_path()
    if db_path == live_path:
        raise ValueError(
            "Refusing to run demo reconciliation against live database "
            f"at {db_path}.  Use a temporary database instead."
        )


__all__ = [
    "DemoResult",
    "ReviewTableRow",
    "run_demo_reconciliation",
    "InMemoryReviewResult",
    "run_in_memory_review",
    "format_in_memory_review_summary",
    "format_in_memory_review_table",
    "build_review_entries_sorted",
    "build_review_table_rows",
    "format_review_table",
]

# ============================================================================
# In-Memory Review Queue Fixture (Review Queue + Resolution v1)
# ============================================================================
"""In-memory review queue workflow that does not use SQLite at all.

Loads statement CSV and app transaction JSON, runs the deterministic
batch matching engine and review queue generator, and returns a structured
result suitable for CLI or test consumption.
"""


@dataclass(frozen=True)
class InMemoryReviewResult:
    """Structured result from an in-memory review queue run."""

    total_statement_rows: int
    total_app_transactions: int
    summary: ReconciliationSummary
    queue_items: list[ReviewQueueItem] = field(default_factory=list)


_REVIEW_QUEUE_DISPLAY_ORDER: dict[IssueType, int] = {
    IssueType.AMOUNT_MISMATCH: 0,
    IssueType.CURRENCY_MISMATCH: 1,
    IssueType.POSSIBLE_DUPLICATE: 2,
    IssueType.DATE_MISMATCH: 3,
    IssueType.MISSING_IN_APP: 4,
    IssueType.MERCHANT_MISMATCH: 5,
    IssueType.MISSING_IN_STATEMENT: 6,
    IssueType.NEEDS_REVIEW: 7,
    IssueType.MATCHED: 99,
}


def run_in_memory_review(
    statement_csv_path: str | Path,
    app_transactions_json_path: str | Path,
) -> InMemoryReviewResult:
    """Run the full in-memory review queue workflow.

    1. Parse the statement CSV.
    2. Load app transactions from JSON.
    3. Run the deterministic batch matching engine.
    4. Generate the review queue.
    5. Return a structured ``InMemoryReviewResult``.

    Parameters
    ----------
    statement_csv_path:
        Path to a statement CSV file.
    app_transactions_json_path:
        Path to a JSON file with app-side transaction candidates.

    Returns
    -------
    InMemoryReviewResult
    """
    # Parse CSV
    adapter = StatementCsvAdapter()
    csv_result = adapter.parse_file_hardened(Path(statement_csv_path))
    if not csv_result.success:
        raise ValueError(f"CSV parse errors: {csv_result.errors}")

    # Convert structured rows to StatementTransaction domain models
    statements = _structured_rows_to_statements(csv_result.rows)

    # Load app transactions from JSON
    app_transactions = _load_app_transactions_from_json(app_transactions_json_path)

    # Run matching
    candidates = match_batch(statements, app_transactions)

    # Generate review queue
    queue_items, summary = generate_review_queue(candidates)

    return InMemoryReviewResult(
        total_statement_rows=len(statements),
        total_app_transactions=len(app_transactions),
        summary=summary,
        queue_items=queue_items,
    )


def format_in_memory_review_summary(result: InMemoryReviewResult) -> str:
    """Format an in-memory review run as a plain-text summary suitable for CLI."""
    s = result.summary
    lines = [
        "Reconciliation Review Queue",
        "",
        "Summary:",
        f"  - matched:          {s.matched_count}",
        f"  - review required:  {s.review_required_count}",
        f"  - high priority:    {s.high_priority_count}",
        f"  - medium priority:  {s.medium_priority_count}",
        f"  - low priority:     {s.low_priority_count}",
    ]

    return "\n".join(lines)


def format_in_memory_review_table(result: InMemoryReviewResult) -> str:
    """Format priority review items grouped by priority tier as a plain-text display.

    Output follows the spec::

        High Priority
        1. GRAB
           Issue: Amount mismatch
           Statement: SGD 18.80 on 2026-05-26
           App record: SGD 18.20 on 2026-05-24
           Reason codes: AMOUNT_MISMATCH, ...
           Suggested action: Review amount
    """
    s = result.summary
    high = s.high_priority_items
    medium = s.medium_priority_items
    low = [
        q
        for q in result.queue_items
        if q.candidate.review_priority == ReviewPriority.LOW and q.issue_type != IssueType.MATCHED
    ]

    if not high and not medium and not low:
        return "No review items -- all matched.\n"

    lines: list[str] = []

    def _format_item(q: ReviewQueueItem, idx: int) -> list[str]:
        stmt = q.candidate.statement
        app = q.candidate.best_app_transaction
        item_lines: list[str] = []

        # Merchant name as heading
        merchant = stmt.merchant_raw or "Unknown"
        item_lines.append(f"  {idx}. {merchant}")

        # Issue type
        issue_label = q.issue_type.value.replace("_", " ").title()
        item_lines.append(f"     Issue: {issue_label}")

        # Statement info
        stmt_amount = f"SGD {stmt.amount}" if stmt.amount is not None else "SGD ?"
        if stmt.transaction_date is not None:
            stmt_date = stmt.transaction_date.isoformat()
        elif stmt.posted_date is not None:
            stmt_date = stmt.posted_date.isoformat()
        else:
            stmt_date = "?"
        item_lines.append(f"     Statement: {stmt_amount} on {stmt_date}")

        # App record info
        if app is not None:
            app_amount = f"SGD {app.amount}"
            app_date = app.transaction_date.isoformat() if app.transaction_date else "?"
            item_lines.append(f"     App record: {app_amount} on {app_date}")
        else:
            item_lines.append("     App record: no match")

        # Reason codes
        if q.reason_codes:
            reason_str = ", ".join(r.value for r in q.reason_codes)
            item_lines.append(f"     Reason codes: {reason_str}")

        # Suggested action
        item_lines.append(f"     Suggested action: {q.suggested_action.value}")

        return item_lines

    idx = 0

    if high:
        lines.append("High Priority")
        for q in high:
            idx += 1
            lines.extend(_format_item(q, idx))
        lines.append("")

    if medium:
        lines.append("Medium Priority")
        for q in medium:
            idx += 1
            lines.extend(_format_item(q, idx))
        lines.append("")

    if low:
        lines.append("Low Priority")
        for q in low:
            idx += 1
            lines.extend(_format_item(q, idx))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _structured_rows_to_statements(
    rows: Sequence[StructuredStatementRow],
) -> list[StatementTransaction]:
    """Convert StructuredStatementRow objects to StatementTransaction models."""
    statements: list[StatementTransaction] = []
    for row in rows:
        statements.append(
            StatementTransaction(
                transaction_date=row.transaction_date,
                posted_date=row.posted_date,
                merchant_raw=row.merchant_raw,
                merchant_normalized=row.merchant_normalized,
                amount=row.amount,
                currency=row.currency,
                account_name=row.account_name,
                account_id=row.account_id,
                statement_row_reference=row.statement_row_reference,
                amount_direction=row.amount_direction,
                raw_amount=row.raw_amount,
                raw_amount_type=row.raw_amount_type,
            )
        )
    return statements


def _load_app_transactions_from_json(path: str | Path) -> list[AppTransaction]:
    """Load AppTransaction objects from a JSON fixture file."""
    with open(Path(path), "r", encoding="utf-8") as fh:
        data = json.load(fh)

    raw_candidates: list[dict[str, str]] = data.get("app_transactions", [])
    app_txns: list[AppTransaction] = []
    for raw in raw_candidates:
        app_txns.append(
            AppTransaction(
                app_txn_id=raw["app_txn_id"],
                transaction_date=date.fromisoformat(raw["transaction_date"]),
                merchant=raw["merchant"],
                amount=Decimal(raw["amount"]),
                currency=raw["currency"],
                source_type=raw.get("source_type"),
                source_channel=raw.get("source_channel"),
                normalized_merchant=raw.get("normalized_merchant"),
                posted_date=date.fromisoformat(raw["posted_date"])
                if raw.get("posted_date")
                else None,
                transaction_type=raw.get("transaction_type", "expense"),
            )
        )
    return app_txns
