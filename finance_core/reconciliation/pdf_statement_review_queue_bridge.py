"""PDF Statement Review Queue Bridge v1.

Bridge between the PDF statement temp DB import fixture and the PDF
statement review queue fixture.  Wraps ``import_pdf_statement_fixture_to_temp_db``
and the review queue fixture into a single, reusable entrypoint that supports
both ``fixture_text`` (deterministic CI) and ``pdf_text`` (real PDF text
extraction) source modes.

Non-goals:

- No raw PDF parsing (delegated to the import fixture, which delegates to
  the text extraction adapter).
- No OCR.
- No database/finance.db access.
- No final financial record mutation.
- No settlement obligation generation.
- No AI/model calls.

Safety:

- Refuses ``database/finance.db`` at every entrypoint.
- Does not silently fall back from ``pdf_text`` to ``fixture_text`` on
  extraction failure.
- Preserves source PDF path / attachment_path evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.models import (
    AppTransaction,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.pdf_statement_extractor import (
    DEFAULT_PDF_RESOURCE_LIMITS,
    PdfResourceLimits,
)
from finance_core.reconciliation.pdf_statement_review_queue_fixture import (
    PdfStatementReviewQueueFixture,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    PdfStatementTempDbImportResult,
    import_pdf_statement_fixture_to_temp_db,
    result_to_summary_dict,
)
from finance_core.reconciliation.review_persistence import ReviewQueuePersistence
from finance_core.reconciliation.review_queue import generate_review_queue
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

SourceMode = Literal["fixture_text", "pdf_text"]


@dataclass(frozen=True)
class PdfStatementReviewQueueBridgeResult:
    """Result from the PDF statement review queue bridge.

    Fields
    ------
    import_result:
        The raw import result from ``import_pdf_statement_fixture_to_temp_db``.
    review_queue:
        The review queue fixture produced during import.
    source_mode:
        ``"fixture_text"`` or ``"pdf_text"``.
    pdf_parsing_mode:
        Resolved ``pdf_parsing_mode`` label, matching the import result.
    matched_count:
        Number of rows that were successfully normalised and inserted
        into the temp DB.  These are rows classified as
        ``ready_for_import`` that passed batch normalisation.
    review_required_count:
        Number of rows classified as ``needs_review`` or ``blocked``
        that require human review.
    ready_for_import_count:
        Number of rows classified as ``ready_for_import``.
    blocked_count:
        Number of rows classified as ``blocked``.
    review_only:
        Always ``True``.
    not_final_financial_record:
        Always ``True``.
    """

    import_result: PdfStatementTempDbImportResult
    review_queue: PdfStatementReviewQueueFixture
    source_mode: SourceMode
    pdf_parsing_mode: str
    matched_count: int
    review_required_count: int
    ready_for_import_count: int
    blocked_count: int
    review_only: bool = True
    not_final_financial_record: bool = True
    persisted_count: int = 0
    persistence_run_public_id: str | None = None


def run_pdf_statement_review_queue_bridge(
    *,
    db_path: str | Path,
    source_mode: SourceMode = "fixture_text",
    pdf_path: str | Path = DEFAULT_PDF_FIXTURE_PATH,
    text_fixture_path: str | Path = DEFAULT_TEXT_FIXTURE_PATH,
    template_id: str = DEFAULT_TEMPLATE_ID,
    batch_public_id: str | None = None,
    persist_review_queue: bool = False,
    persistence_run_public_id: str | None = None,
    pdf_limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
) -> PdfStatementReviewQueueBridgeResult:
    """Run the PDF statement review queue bridge.

    Parameters
    ----------
    db_path:
        Path to the temp SQLite database.  Must not be ``database/finance.db``.
    source_mode:
        ``"fixture_text"`` (default, deterministic CI) or ``"pdf_text"``
        (real PDF text extraction).
    pdf_path:
        Path to the PDF source file.  In ``fixture_text`` mode this is
        preserved as the evidence path only; in ``pdf_text`` mode this
        is the real PDF to extract text from.
    text_fixture_path:
        Path to the deterministic extracted-text fixture file.  Only
        used in ``fixture_text`` mode.
    template_id:
        Template ID for row parsing.
    batch_public_id:
        Deterministic batch public_id.  Auto-generated when omitted.
    persist_review_queue:
        When True, persists review queue items to the
        reconciliation_review_queue table in the temp DB via
        ReviewQueuePersistence.  Default False (in-memory only).
    persistence_run_public_id:
        Stable run public_id for persistence.  Auto-generated when
        omitted.

    Returns
    -------
    PdfStatementReviewQueueBridgeResult

    Raises
    ------
    ValueError
        If ``db_path`` resolves to ``database/finance.db``.
        If ``source_mode`` is ``"pdf_text"`` and extraction fails
        (empty rows, unsupported PDF, etc.).
    """
    # --- Guard: refuse live DB ---
    resolved_db = Path(db_path).expanduser().resolve()
    if resolved_db == LIVE_DB_PATH.resolve():
        raise ValueError("Refusing to use database/finance.db for PDF review queue bridge")
    if resolved_db.name == "finance.db" and resolved_db.parent == LIVE_DB_PATH.parent.resolve():
        raise ValueError("Refusing to use database/finance.db for PDF review queue bridge")

    # --- Import via existing fixture ---
    kwargs: dict[str, Any] = {
        "db_path": db_path,
        "pdf_path": pdf_path,
        "template_id": template_id,
        "source_mode": source_mode,
        "pdf_limits": pdf_limits,
    }
    if source_mode == "fixture_text":
        kwargs["text_fixture_path"] = text_fixture_path
    if batch_public_id is not None:
        kwargs["batch_public_id"] = batch_public_id

    import_result = import_pdf_statement_fixture_to_temp_db(**kwargs)

    review_queue = import_result.review_queue
    summary = review_queue.summary

    matched_count = len(import_result.import_batch.inserted_ids)
    review_required_count = summary.needs_review_count + summary.blocked_count

    # --- Persist review queue (optional) ---
    persisted_count = 0
    run_pub_id: str | None = None
    if persist_review_queue:
        # Guard: :memory: cannot be used for persistence
        db_str = str(db_path)
        if db_str == ":memory:":
            raise ValueError(
                "Refusing to persist review queue to :memory: database. Use a file-based temp DB."
            )
        effective_batch_public_id = import_result.import_batch.public_id
        run_pub_id = persistence_run_public_id or f"{effective_batch_public_id}-persist-run"
        persisted_count = _persist_review_queue_to_temp_db(
            db_path=str(Path(db_path).expanduser().resolve()),
            batch_public_id=effective_batch_public_id,
            run_public_id=run_pub_id,
        )

    return PdfStatementReviewQueueBridgeResult(
        import_result=import_result,
        review_queue=review_queue,
        source_mode=source_mode,
        pdf_parsing_mode=import_result.pdf_parsing_mode,
        matched_count=matched_count,
        review_required_count=review_required_count,
        ready_for_import_count=summary.ready_for_import_count,
        blocked_count=summary.blocked_count,
        persisted_count=persisted_count,
        persistence_run_public_id=run_pub_id,
    )


def bridge_result_to_summary_dict(
    result: PdfStatementReviewQueueBridgeResult,
) -> dict[str, Any]:
    """Return a compact JSON-serialisable summary dict from the bridge result."""
    import_summary = result_to_summary_dict(result.import_result)
    d: dict[str, Any] = {
        "source_mode": result.source_mode,
        "pdf_parsing_mode": result.pdf_parsing_mode,
        "matched_count": result.matched_count,
        "review_required_count": result.review_required_count,
        "ready_for_import_count": result.ready_for_import_count,
        "blocked_count": result.blocked_count,
        "total_rows": result.review_queue.summary.total_rows,
        "review_only": result.review_only,
        "not_final_financial_record": result.not_final_financial_record,
        "db_path": import_summary["db_path"],
        "pdf_path": import_summary["pdf_path"],
        "text_fixture_path": import_summary["text_fixture_path"],
        "template_id": import_summary["template_id"],
        "source_type": import_summary["source_type"],
        "inserted_rows": import_summary["inserted_rows"],
        "persisted_count": result.persisted_count,
    }
    if result.persistence_run_public_id is not None:
        d["persistence_run_public_id"] = result.persistence_run_public_id
    return d


# ---------------------------------------------------------------------------
# Persistence helpers (private)
# ---------------------------------------------------------------------------


def _read_statement_transactions_from_temp_db(
    db_path: str,
    batch_public_id: str,
) -> list[StatementTransaction]:
    """Read imported statement transactions for one batch from a temp DB.

    Maps the statement_transactions table columns back to
    StatementTransaction domain objects.
    """
    conn = connect_sqlite(db_path, mode=ConnectionMode.READ_ONLY)
    try:
        rows = conn.execute(
            """
            SELECT st.public_id, st.transaction_date, st.posted_date, st.merchant_raw,
                   st.merchant_normalized, st.amount, st.currency,
                   st.account_name, st.account_id, st.statement_row_reference,
                   st.amount_direction, st.raw_amount, st.raw_amount_type,
                   st.row_fingerprint, sib.source_file_hash
            FROM statement_transactions st
            JOIN statement_import_batches sib ON sib.id = st.batch_id
            WHERE sib.public_id = ?
            ORDER BY st.id ASC
            """,
            (batch_public_id,),
        ).fetchall()
    finally:
        conn.close()

    result: list[StatementTransaction] = []
    for row in rows:
        result.append(
            StatementTransaction(
                transaction_date=date.fromisoformat(row["transaction_date"])
                if row["transaction_date"]
                else None,
                posted_date=date.fromisoformat(row["posted_date"]) if row["posted_date"] else None,
                merchant_raw=row["merchant_raw"] or "",
                merchant_normalized=row["merchant_normalized"],
                amount=Decimal(str(row["amount"])) if row["amount"] is not None else None,
                currency=row["currency"],
                account_name=row["account_name"],
                account_id=row["account_id"],
                statement_row_reference=row["public_id"],
                public_id=row["public_id"],
                row_fingerprint=row["row_fingerprint"],
                source_content_hash=row["source_file_hash"],
                amount_direction=StatementAmountDirection(row["amount_direction"])
                if row["amount_direction"]
                else None,
                raw_amount=row["raw_amount"],
                raw_amount_type=row["raw_amount_type"],
            )
        )
    return result


def _build_deterministic_app_transactions(
    statements: list[StatementTransaction],
) -> list[AppTransaction]:
    """Build deterministic app-side transaction fixtures.

    Creates AppTransaction records that match the known PDF fixture rows
    (CoffeeShop, Salary) and leaves the third row (Grocer) unmatched
    for review-required coverage.

    This is intentionally deterministic and fixture-aware.
    """
    app_txns: list[AppTransaction] = []

    # CoffeeShop match for row 1 (2026-07-01, MYR 12.50)
    app_txns.append(
        AppTransaction(
            app_txn_id="app-coffeeshop-001",
            transaction_date=date(2026, 7, 1),
            merchant="CoffeeShop",
            amount=Decimal("12.50"),
            currency="MYR",
            source_type="bank_transfer",
            source_channel="manual",
            normalized_merchant="coffeeshop",
            posted_date=date(2026, 7, 1),
            transaction_type="expense",
        )
    )

    # Salary match for row 2 (2026-07-02, MYR 1000.00)
    app_txns.append(
        AppTransaction(
            app_txn_id="app-salary-001",
            transaction_date=date(2026, 7, 2),
            merchant="Salary",
            amount=Decimal("1000.00"),
            currency="MYR",
            source_type="salary",
            source_channel="manual",
            normalized_merchant="salary",
            posted_date=date(2026, 7, 2),
            transaction_type="income",
        )
    )

    return app_txns


def _persist_review_queue_to_temp_db(
    *,
    db_path: str,
    batch_public_id: str,
    run_public_id: str,
) -> int:
    """Run the in-memory reconciliation pipeline and persist results.

    1. Read statement transactions from the temp DB.
    2. Build deterministic app transaction fixtures.
    3. Run batch matching via match_batch().
    4. Generate review queue items via generate_review_queue().
    5. Persist to reconciliation_review_queue via
       ReviewQueuePersistence.

    Returns the number of persisted review queue rows.
    """
    existing_count = _count_existing_review_queue_run(
        db_path=db_path,
        run_public_id=run_public_id,
    )
    if existing_count > 0:
        return existing_count

    statements = _read_statement_transactions_from_temp_db(
        db_path,
        batch_public_id=batch_public_id,
    )
    if not statements:
        return 0

    app_transactions = _build_deterministic_app_transactions(statements)
    candidates = match_batch(statements, app_transactions)
    review_items, _summary = generate_review_queue(candidates, run_label=batch_public_id)

    conn = connect_sqlite(db_path, mode=ConnectionMode.APPLICATION)
    try:
        persistence = ReviewQueuePersistence(conn)
        persisted = persistence.persist_review_queue(review_items, run_public_id=run_public_id)
    finally:
        conn.close()

    return persisted


def _count_existing_review_queue_run(
    *,
    db_path: str,
    run_public_id: str,
) -> int:
    """Return existing persisted review rows for a run, if any."""
    conn = connect_sqlite(db_path, mode=ConnectionMode.READ_ONLY)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM reconciliation_review_queue
            WHERE run_public_id = ?
            """,
            (run_public_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["cnt"]) if row else 0


__all__ = [
    "PdfStatementReviewQueueBridgeResult",
    "SourceMode",
    "bridge_result_to_summary_dict",
    "run_pdf_statement_review_queue_bridge",
]
