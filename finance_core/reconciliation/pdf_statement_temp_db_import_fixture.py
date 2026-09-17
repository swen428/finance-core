"""PDF Statement Temp DB Import Fixture v1.

Imports a deterministic PDF statement fixture into a caller-supplied
temporary SQLite database. CI uses an adjacent extracted-text fixture rather
than relying on PDF text extraction, but the original PDF path is preserved as
source evidence on the import batch and row payloads.

Non-goals:
- No production PDF parser runtime.
- No OCR.
- No live database access.
- No mutation of final financial records.
- No settlement obligation generation.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from finance_core.reconciliation.migrations import (
    LIVE_DB_PATH,
    TEMP_DB_MIGRATION_PATHS,
)
from finance_core.reconciliation.pdf_statement_bridge import (
    PdfStatementBatchNormalizationResult,
    normalize_pdf_statement_rows_batch,
)
from finance_core.reconciliation.pdf_statement_extractor import (
    DEFAULT_PDF_RESOURCE_LIMITS,
    PdfExtractedLine,
    PdfExtractedPage,
    PdfExtractionResult,
    PdfResourceLimits,
)
from finance_core.reconciliation.pdf_statement_review_queue_fixture import (
    PdfStatementReviewQueueFixture,
    build_pdf_statement_review_queue,
)
from finance_core.reconciliation.pdf_statement_template import get_template
from finance_core.reconciliation.pdf_statement_template_adapter import (
    TemplateAdapterResult,
    convert_template_parse_result_to_adapter_payload,
)
from finance_core.reconciliation.pdf_statement_template_cli import (
    ParseResult,
    _parse_row_heuristic,
)
from finance_core.reconciliation.pdf_text_extraction_adapter import (
    build_parse_result_from_extracted_pdf,
)
from finance_core.reconciliation.statement_identity import read_source_file_evidence
from finance_core.reconciliation.statement_import import (
    StatementImportBatch,
    StatementImporter,
)
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite
from finance_core.staging_guard import create_staging_database

DEFAULT_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "reconciliation"
    / "pdf_statement_temp_db"
)
DEFAULT_PDF_FIXTURE_PATH = DEFAULT_FIXTURE_ROOT / "sample_bank_statement.pdf"
DEFAULT_TEXT_FIXTURE_PATH = DEFAULT_FIXTURE_ROOT / "sample_bank_statement.extracted.txt"
DEFAULT_TEMPLATE_ID = "sample_bank_v1"
DEFAULT_BATCH_PUBLIC_ID = "pdf-temp-db-fixture-sample-bank-v1"


@dataclass(frozen=True)
class PdfStatementTempDbImportResult:
    """Result of importing the deterministic PDF statement fixture."""

    db_path: str
    pdf_path: str
    text_fixture_path: str
    parse_result: ParseResult
    adapter_result: TemplateAdapterResult
    review_queue: PdfStatementReviewQueueFixture
    normalization_result: PdfStatementBatchNormalizationResult
    import_batch: StatementImportBatch
    source_type: str
    review_only: bool = True
    not_final_financial_record: bool = True
    pdf_parsing_mode: str = "fixture_text"


class PdfStatementImportExtractionError(ValueError):
    """A bounded PDF extraction failure prevented the import from starting."""

    def __init__(self, extraction: PdfExtractionResult) -> None:
        self.error_code = extraction.error_code
        message = extraction.error_message or "PDF extraction failed."
        super().__init__(message)


def assert_safe_temp_db_path(db_path: str | Path) -> Path:
    """Return a resolved DB path after refusing the live Finance DB path."""
    if str(db_path) == ":memory:":
        raise ValueError("Refusing to use :memory: for PDF statement fixture temp DB")
    resolved = Path(db_path).expanduser().resolve()
    if resolved == LIVE_DB_PATH.resolve():
        raise ValueError("Refusing to use database/finance.db for PDF statement fixture")
    if resolved.name == "finance.db" and resolved.parent == LIVE_DB_PATH.parent.resolve():
        raise ValueError("Refusing to use database/finance.db for PDF statement fixture")
    return resolved


def connect_and_migrate_temp_db(db_path: str | Path) -> sqlite3.Connection:
    """Open a safe temp DB path and apply the project temp migrations.

    When the target file already exists and is a staging-authorised database,
    reconnects and reuses it.  When the file is empty (e.g. a bare
    ``NamedTemporaryFile`` stub) or does not exist, a fresh staging database
    is created.
    """
    safe_path = assert_safe_temp_db_path(db_path)
    safe_path.parent.mkdir(parents=True, exist_ok=True)

    # Detect an existing staging-authorised database.
    if safe_path.exists() and safe_path.stat().st_size > 0:
        # Reconnect to an already-authorised staging database.
        from finance_core.staging_guard import require_staging_database

        conn = connect_sqlite(safe_path, mode=ConnectionMode.APPLICATION)
        try:
            require_staging_database(conn)
        except Exception:
            conn.close()
            raise
        return conn

    # Remove any empty stub file so create_staging_database can create it fresh.
    if safe_path.exists():
        safe_path.unlink()

    return create_staging_database(safe_path, migration_paths=TEMP_DB_MIGRATION_PATHS)


def build_parse_result_from_text_fixture(
    *,
    pdf_path: str | Path = DEFAULT_PDF_FIXTURE_PATH,
    text_fixture_path: str | Path = DEFAULT_TEXT_FIXTURE_PATH,
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> ParseResult:
    """Build a template parse result from deterministic extracted text.

    This deliberately does not parse the PDF itself. The checked-in text file
    represents extracted text for stable CI while ``pdf_path`` remains the
    preserved source attachment path.
    """
    resolved_pdf = Path(pdf_path).expanduser().resolve()
    resolved_text = Path(text_fixture_path).expanduser().resolve()
    if resolved_pdf.suffix.lower() != ".pdf":
        raise ValueError("pdf_path must point to a .pdf source fixture")
    if not resolved_pdf.exists():
        raise FileNotFoundError(f"PDF fixture not found: {resolved_pdf}")
    if not resolved_text.exists():
        raise FileNotFoundError(f"Extracted-text fixture not found: {resolved_text}")

    template = get_template(template_id)
    source_evidence = read_source_file_evidence(resolved_pdf)
    raw_lines = tuple(
        line.strip()
        for line in resolved_text.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    effective_lines = raw_lines[template.skip_header_lines :]
    if template.skip_footer_lines > 0:
        effective_lines = effective_lines[: -template.skip_footer_lines]

    rows = tuple(
        _parse_row_heuristic(
            raw_line=raw_line,
            source_path=str(resolved_pdf),
            template=template,
            row_index=index,
            source_content_hash=source_evidence.content_hash,
            source_filename=source_evidence.original_filename,
            source_page_number=1,
            source_row_number=index + 1,
        )
        for index, raw_line in enumerate(effective_lines)
    )

    extraction = PdfExtractionResult(
        pdf_path=str(resolved_pdf),
        pages=(
            PdfExtractedPage(
                page_number=1,
                lines=tuple(
                    PdfExtractedLine(text=line, line_number=index)
                    for index, line in enumerate(effective_lines, start=1)
                ),
                raw_text_block="\n".join(effective_lines),
                warnings=("fixture_text_used_instead_of_direct_pdf_extraction",),
            ),
        ),
        total_pages=1,
        total_lines=len(effective_lines),
        source_content_hash=source_evidence.content_hash,
        source_filename=source_evidence.original_filename,
        warnings=("fixture_text_used_instead_of_direct_pdf_extraction",),
        success=True,
        error_message="",
    )

    return ParseResult(
        pdf_path=str(resolved_pdf),
        template_id=template_id,
        template=template,
        rows=rows,
        total_rows=len(rows),
        ok_count=sum(1 for row in rows if row.parse_status == "ok"),
        warning_count=sum(1 for row in rows if row.parse_status == "warning"),
        error_count=sum(1 for row in rows if row.parse_status == "error"),
        extraction=extraction,
        extraction_success=True,
        extraction_warnings=extraction.warnings,
        source_content_hash=source_evidence.content_hash,
        source_filename=source_evidence.original_filename,
    )


def build_parse_result_from_extracted_pdf_statement(
    pdf_path: str | Path,
    template_id: str,
    *,
    limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
) -> ParseResult:
    """Extract text from a real PDF statement and feed through template parser.

    This is the PDF text extraction variant of
    build_parse_result_from_text_fixture().  It calls the PDF text
    extraction adapter, which extracts raw text from the PDF via pypdf,
    then parses each line with the named template.

    Args:
        pdf_path: Path to the real PDF statement file.
        template_id: Template ID for parsing (e.g. sample_bank_v1).

    Returns:
        A ParseResult with extraction metadata and parsed rows.
    """
    return build_parse_result_from_extracted_pdf(
        pdf_path=pdf_path,
        template_id=template_id,
        limits=limits,
    )


def import_pdf_statement_fixture_to_temp_db(
    *,
    db_path: str | Path,
    pdf_path: str | Path = DEFAULT_PDF_FIXTURE_PATH,
    text_fixture_path: str | Path = DEFAULT_TEXT_FIXTURE_PATH,
    template_id: str = DEFAULT_TEMPLATE_ID,
    batch_public_id: str = DEFAULT_BATCH_PUBLIC_ID,
    source_type: str | None = None,
    source_mode: str = "fixture_text",
    pdf_limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
) -> PdfStatementTempDbImportResult:
    """Import the deterministic PDF statement fixture into a temp SQLite DB.

    Args:
        source_mode: "fixture_text" (default) or "pdf_text". When "pdf_text",
            real PDF text extraction is used instead of the fixture text file.
    """
    safe_db_path = assert_safe_temp_db_path(db_path)

    if source_mode == "pdf_text":
        parse_result = build_parse_result_from_extracted_pdf_statement(
            pdf_path=pdf_path,
            template_id=template_id,
            limits=pdf_limits,
        )
        text_fixture_path = Path(str(pdf_path))  # no separate fixture text file
    else:
        parse_result = build_parse_result_from_text_fixture(
            pdf_path=pdf_path,
            text_fixture_path=text_fixture_path,
            template_id=template_id,
        )
    if not parse_result.extraction_success:
        raise PdfStatementImportExtractionError(parse_result.extraction)
    adapter_result = convert_template_parse_result_to_adapter_payload(parse_result)
    parse_statuses = {
        f"row-{row.row_index}": row.parse_status for row in parse_result.rows if row.parse_status
    }
    parser_warnings = {
        f"row-{row.row_index}": row.warnings for row in parse_result.rows if row.warnings
    }
    review_queue = build_pdf_statement_review_queue(
        adapter_result.adapted_rows,
        source_statement_id=adapter_result.source_statement_id,
        attachment_path=adapter_result.source_path,
        template_id=template_id,
        parse_statuses=parse_statuses,
        parser_warnings=parser_warnings,
    )

    ready_refs = {
        row.source_row_ref for row in review_queue.rows if row.classification == "ready_for_import"
    }
    ready_rows = tuple(
        row for row in adapter_result.adapted_rows if row.source_row_ref in ready_refs
    )
    normalization_result = normalize_pdf_statement_rows_batch(ready_rows)

    effective_source_type = source_type or _source_type_for_template(
        parse_result.template.statement_type
    )
    conn = connect_and_migrate_temp_db(safe_db_path)
    try:
        importer = StatementImporter(conn)
        import_batch = importer.import_rows(
            normalization_result.accepted_rows,
            source_type=effective_source_type,
            public_id=batch_public_id,
            account_name=parse_result.template.account_label,
            currency=parse_result.template.currency,
            source_file_path=parse_result.pdf_path,
        )
    finally:
        conn.close()

    parsing_mode = "pdf_text" if source_mode == "pdf_text" else "fixture_text"
    return PdfStatementTempDbImportResult(
        db_path=str(safe_db_path),
        pdf_path=parse_result.pdf_path,
        text_fixture_path=str(Path(text_fixture_path).expanduser().resolve()),
        parse_result=parse_result,
        adapter_result=adapter_result,
        review_queue=review_queue,
        normalization_result=normalization_result,
        import_batch=import_batch,
        source_type=effective_source_type,
        pdf_parsing_mode=parsing_mode,
    )


def result_to_summary_dict(result: PdfStatementTempDbImportResult) -> dict[str, Any]:
    """Return a compact JSON-serializable summary for CLI output."""
    return {
        "db_path": result.db_path,
        "pdf_path": result.pdf_path,
        "text_fixture_path": result.text_fixture_path,
        "template_id": result.parse_result.template_id,
        "source_type": result.source_type,
        "batch_public_id": result.import_batch.public_id,
        "batch_id": result.import_batch.batch_id,
        "parsed_rows": result.parse_result.total_rows,
        "ready_for_import": result.review_queue.summary.ready_for_import_count,
        "needs_review": result.review_queue.summary.needs_review_count,
        "blocked": result.review_queue.summary.blocked_count,
        "normalized_rows": result.normalization_result.accepted_count,
        "inserted_rows": len(result.import_batch.inserted_ids),
        "skipped_duplicates": result.import_batch.skipped_duplicates,
        "idempotent_count": result.import_batch.idempotent_count,
        "review_only": result.review_only,
        "not_final_financial_record": result.not_final_financial_record,
        "pdf_parsing_mode": result.pdf_parsing_mode,
    }


def _source_type_for_template(statement_type: str) -> str:
    if statement_type == "credit_card":
        return "credit_card_statement"
    return "bank_statement"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import the deterministic PDF statement fixture into a temp SQLite DB.",
    )
    parser.add_argument("--db", required=True, help="Temporary SQLite DB path.")
    parser.add_argument("--pdf", default=str(DEFAULT_PDF_FIXTURE_PATH), help="PDF fixture path.")
    parser.add_argument(
        "--text-fixture",
        default=str(DEFAULT_TEXT_FIXTURE_PATH),
        help="Adjacent extracted-text fixture path used for deterministic CI parsing.",
    )
    parser.add_argument("--template", default=DEFAULT_TEMPLATE_ID, help="Template ID.")
    parser.add_argument(
        "--batch-public-id",
        default=DEFAULT_BATCH_PUBLIC_ID,
        help="Deterministic statement import batch public_id.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=args.db,
            pdf_path=args.pdf,
            text_fixture_path=args.text_fixture,
            template_id=args.template,
            batch_public_id=args.batch_public_id,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    summary = result_to_summary_dict(result)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "PDF statement temp DB import fixture: "
            f"{summary['inserted_rows']} inserted, "
            f"{summary['idempotent_count']} idempotent, "
            f"db={summary['db_path']}"
        )
        print(f"PDF parsing mode: {summary['pdf_parsing_mode']}")
        print(f"Source PDF preserved: {summary['pdf_path']}")
    return 0


__all__ = [
    "DEFAULT_BATCH_PUBLIC_ID",
    "DEFAULT_PDF_FIXTURE_PATH",
    "DEFAULT_TEMPLATE_ID",
    "DEFAULT_TEXT_FIXTURE_PATH",
    "PdfStatementTempDbImportResult",
    "PdfStatementImportExtractionError",
    "assert_safe_temp_db_path",
    "build_parse_result_from_extracted_pdf_statement",
    "build_parse_result_from_text_fixture",
    "connect_and_migrate_temp_db",
    "import_pdf_statement_fixture_to_temp_db",
    "main",
    "result_to_summary_dict",
]
