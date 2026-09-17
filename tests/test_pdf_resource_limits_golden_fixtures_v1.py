"""Resource-limit, golden-fixture, and atomic-failure tests for PDF imports."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import shutil
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from finance_core.reconciliation import pdf_statement_template_cli
from finance_core.reconciliation.pdf_statement_extractor import (
    DEFAULT_PDF_RESOURCE_LIMITS,
    PdfExtractionErrorCode,
    PdfResourceLimits,
    extract_text_from_pdf,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_TEMPLATE_ID,
    PdfStatementImportExtractionError,
    import_pdf_statement_fixture_to_temp_db,
)
from finance_core.reconciliation.pdf_text_extraction_adapter import (
    build_parse_result_from_extracted_pdf,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parent / "fixtures" / "reconciliation" / "pdf_statement_temp_db"
)
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
SINGLE_PAGE_PDF = FIXTURE_ROOT / "sample_bank_statement.pdf"
MULTI_PAGE_PDF = FIXTURE_ROOT / "multi_page_bank_statement.pdf"
BOUNDARY_PDF = FIXTURE_ROOT / "near_text_boundary_statement.pdf"
ENCRYPTED_PDF = FIXTURE_ROOT / "encrypted_bank_statement.pdf"
_MIB = 1024 * 1024


def _slow_worker(connection: Any, source_content: bytes, limits: PdfResourceLimits) -> None:
    """Picklable worker seam that cannot finish before a focused test deadline."""
    del source_content
    try:
        time.sleep(max(2.0, float(limits.max_parse_seconds) * 10))
    finally:
        connection.close()


def _limits(**changes: object) -> PdfResourceLimits:
    return replace(DEFAULT_PDF_RESOURCE_LIMITS, **changes)


def _manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class TestPdfResourceLimitContract:
    def test_defaults_are_immutable_and_conservative(self) -> None:
        limits = DEFAULT_PDF_RESOURCE_LIMITS
        assert limits == PdfResourceLimits()
        assert limits.max_file_bytes == 25 * _MIB
        assert limits.max_pages == 100
        assert limits.max_chars_per_page == 250_000
        assert limits.max_total_chars == 2_000_000
        assert limits.max_parse_seconds == 15.0
        assert limits.max_worker_memory_bytes == 1024 * _MIB
        with pytest.raises(FrozenInstanceError):
            limits.max_pages = 101  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"max_file_bytes": 0}, "max_file_bytes"),
            ({"max_file_bytes": -1}, "max_file_bytes"),
            ({"max_pages": True}, "max_pages"),
            ({"max_pages": 501}, "max_pages"),
            ({"max_chars_per_page": 0}, "max_chars_per_page"),
            ({"max_total_chars": 1}, "max_total_chars"),
            ({"max_parse_seconds": 0}, "max_parse_seconds"),
            ({"max_parse_seconds": float("inf")}, "max_parse_seconds"),
            ({"max_worker_memory_bytes": 64 * _MIB}, "max_worker_memory_bytes"),
            (
                {"max_file_bytes": 40 * _MIB, "max_worker_memory_bytes": 128 * _MIB},
                "four times",
            ),
        ],
    )
    def test_invalid_limits_fail_at_construction(
        self,
        changes: dict[str, object],
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            _limits(**changes)


class TestGoldenPdfFixtures:
    def test_manifest_hashes_and_metadata_match_checked_in_files(self) -> None:
        manifest = _manifest()
        assert manifest["contract_version"] == "synthetic-pdf-golden-v1"
        for fixture in manifest["fixtures"]:
            fixture_path = FIXTURE_ROOT / fixture["filename"]
            assert fixture_path.is_file()
            assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == fixture["sha256"]
            assert fixture["synthetic_and_sanitized"] is True

    def test_manifest_fixtures_are_strict_parseable_pdfs(self) -> None:
        for fixture in _manifest()["fixtures"]:
            reader = PdfReader(FIXTURE_ROOT / fixture["filename"], strict=True)
            if reader.is_encrypted:
                assert reader.decrypt("synthetic-test-password")
            assert len(reader.pages) == fixture["expected_page_count"]

    def test_valid_single_page_golden_pdf_parses_actual_checked_in_bytes(self) -> None:
        result = build_parse_result_from_extracted_pdf(
            SINGLE_PAGE_PDF,
            DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert result.extraction.total_pages == 1
        assert result.total_rows == 3
        assert [row.amount_direction for row in result.rows] == ["DEBIT", "CREDIT", "DEBIT"]

    def test_valid_multi_page_golden_pdf_preserves_page_and_row_evidence(self) -> None:
        result = build_parse_result_from_extracted_pdf(
            MULTI_PAGE_PDF,
            DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert result.extraction.total_pages == 2
        assert result.total_rows == 4
        assert [row.source_page_number for row in result.rows] == [1, 1, 2, 2]
        assert [row.source_row_number for row in result.rows] == [1, 2, 1, 2]
        assert [row.source_row_ref for row in result.rows] == [
            "page-1:line-1",
            "page-1:line-2",
            "page-2:line-1",
            "page-2:line-2",
        ]
        assert {row.amount_direction for row in result.rows} == {"DEBIT", "CREDIT"}

    def test_controlled_boundary_golden_pdf_is_a_valid_statement_row(self) -> None:
        result = build_parse_result_from_extracted_pdf(
            BOUNDARY_PDF,
            DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert result.total_rows == 1
        assert len(result.extraction.pages[0].raw_text_block) == 512
        assert result.rows[0].amount_direction == "DEBIT"

    def test_fixture_text_contains_no_prohibited_real_data(self) -> None:
        prohibited = ("private-user", "@", "/users/", "finance.db", "production transaction")
        account_number = re.compile(r"(?<!\d)\d{8,}(?!\d)")
        for fixture in _manifest()["fixtures"]:
            reader = PdfReader(FIXTURE_ROOT / fixture["filename"], strict=True)
            if reader.is_encrypted:
                assert reader.decrypt("synthetic-test-password")
            text = "\n".join(page.extract_text() or "" for page in reader.pages).lower()
            assert all(token not in text for token in prohibited)
            assert account_number.search(text) is None


class TestPdfResourceBoundaries:
    @pytest.mark.parametrize(("delta", "expected_success"), [(1, True), (0, True), (-1, False)])
    def test_file_size_below_equal_and_above_limit(
        self,
        delta: int,
        expected_success: bool,
    ) -> None:
        actual_size = SINGLE_PAGE_PDF.stat().st_size
        result = extract_text_from_pdf(
            SINGLE_PAGE_PDF,
            limits=_limits(max_file_bytes=actual_size + delta),
        )
        assert result.success is expected_success
        if not expected_success:
            assert result.error_code == PdfExtractionErrorCode.FILE_SIZE_LIMIT_EXCEEDED

    def test_page_count_below_equal_and_above_limit(self) -> None:
        below = extract_text_from_pdf(SINGLE_PAGE_PDF, limits=_limits(max_pages=2))
        equal = extract_text_from_pdf(MULTI_PAGE_PDF, limits=_limits(max_pages=2))
        above = extract_text_from_pdf(MULTI_PAGE_PDF, limits=_limits(max_pages=1))
        assert below.success is True
        assert equal.success is True
        assert above.error_code == PdfExtractionErrorCode.PAGE_LIMIT_EXCEEDED

    @pytest.mark.parametrize(
        ("limit", "expected_success"), [(513, True), (512, True), (511, False)]
    )
    def test_per_page_text_below_equal_and_above_limit(
        self,
        limit: int,
        expected_success: bool,
    ) -> None:
        result = extract_text_from_pdf(
            BOUNDARY_PDF,
            limits=_limits(max_chars_per_page=limit, max_total_chars=max(512, limit)),
        )
        assert result.success is expected_success
        if not expected_success:
            assert result.error_code == PdfExtractionErrorCode.PAGE_TEXT_LIMIT_EXCEEDED

    def test_total_text_limit_stops_multi_page_extraction(self) -> None:
        baseline = extract_text_from_pdf(MULTI_PAGE_PDF)
        assert baseline.success is True
        page_lengths = [len(page.raw_text_block) for page in baseline.pages]
        total_chars = sum(page_lengths)
        per_page_limit = max(page_lengths)

        equal = extract_text_from_pdf(
            MULTI_PAGE_PDF,
            limits=_limits(
                max_chars_per_page=per_page_limit,
                max_total_chars=total_chars,
            ),
        )
        above = extract_text_from_pdf(
            MULTI_PAGE_PDF,
            limits=_limits(
                max_chars_per_page=per_page_limit,
                max_total_chars=total_chars - 1,
            ),
        )
        assert equal.success is True
        assert above.error_code == PdfExtractionErrorCode.TOTAL_TEXT_LIMIT_EXCEEDED


class TestPdfFailureClassificationsAndCleanup:
    def test_invalid_signature_and_malformed_document_are_distinct(self, tmp_path: Path) -> None:
        invalid_signature = tmp_path / "invalid-signature.pdf"
        invalid_signature.write_bytes(b"not a PDF")
        malformed = tmp_path / "malformed.pdf"
        malformed.write_bytes(b"%PDF-1.4\nmalformed synthetic bytes\n%%EOF\n")

        invalid_result = extract_text_from_pdf(invalid_signature)
        malformed_result = extract_text_from_pdf(malformed)
        assert invalid_result.error_code == PdfExtractionErrorCode.INVALID_SIGNATURE
        assert malformed_result.error_code == PdfExtractionErrorCode.MALFORMED_DOCUMENT
        assert str(tmp_path) not in invalid_result.error_message
        assert str(tmp_path) not in malformed_result.error_message

    def test_encrypted_pdf_has_stable_failure(self) -> None:
        result = extract_text_from_pdf(ENCRYPTED_PDF)
        assert result.error_code == PdfExtractionErrorCode.ENCRYPTED_DOCUMENT
        assert result.pages == ()

    def test_symlink_is_rejected_before_parser_start(self, tmp_path: Path) -> None:
        link = tmp_path / "linked.pdf"
        link.symlink_to(SINGLE_PAGE_PDF)
        result = extract_text_from_pdf(link)
        assert result.error_code == PdfExtractionErrorCode.UNSAFE_PATH

    def test_timeout_is_real_deterministic_and_reaps_worker(self) -> None:
        children_before = {child.pid for child in multiprocessing.active_children()}
        result = extract_text_from_pdf(
            SINGLE_PAGE_PDF,
            limits=_limits(max_parse_seconds=0.1),
            _worker_target=_slow_worker,
        )
        children_after = {child.pid for child in multiprocessing.active_children()}
        assert result.error_code == PdfExtractionErrorCode.PARSE_TIMEOUT
        assert result.pages == ()
        assert children_after == children_before


class TestPdfImportAtomicityAndIdentity:
    def test_all_extraction_failures_happen_before_temp_database_creation(
        self,
        tmp_path: Path,
    ) -> None:
        invalid_signature = tmp_path / "invalid.pdf"
        invalid_signature.write_bytes(b"not a PDF")
        malformed = tmp_path / "malformed.pdf"
        malformed.write_bytes(b"%PDF-1.4\nmalformed synthetic bytes\n%%EOF\n")
        oversize = tmp_path / "oversize.pdf"
        shutil.copyfile(SINGLE_PAGE_PDF, oversize)
        multi_page_reader = PdfReader(MULTI_PAGE_PDF, strict=True)
        multi_page_lengths = [
            len((page.extract_text() or "").strip()) for page in multi_page_reader.pages
        ]
        total_text_limits = _limits(
            max_chars_per_page=max(multi_page_lengths),
            max_total_chars=sum(multi_page_lengths) - 1,
        )

        cases = (
            (invalid_signature, DEFAULT_PDF_RESOURCE_LIMITS),
            (malformed, DEFAULT_PDF_RESOURCE_LIMITS),
            (ENCRYPTED_PDF, DEFAULT_PDF_RESOURCE_LIMITS),
            (oversize, _limits(max_file_bytes=oversize.stat().st_size - 1)),
            (MULTI_PAGE_PDF, _limits(max_pages=1)),
            (BOUNDARY_PDF, _limits(max_chars_per_page=511, max_total_chars=512)),
            (MULTI_PAGE_PDF, total_text_limits),
        )
        for index, (pdf_path, limits) in enumerate(cases):
            db_path = tmp_path / f"failure-{index}.sqlite"
            with pytest.raises(PdfStatementImportExtractionError):
                import_pdf_statement_fixture_to_temp_db(
                    db_path=db_path,
                    pdf_path=pdf_path,
                    source_mode="pdf_text",
                    pdf_limits=limits,
                )
            assert not db_path.exists()

    def test_timeout_happens_before_temp_database_creation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_extract = extract_text_from_pdf

        def slow_extract(
            pdf_path: str | Path,
            *,
            limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
        ) -> Any:
            return real_extract(pdf_path, limits=limits, _worker_target=_slow_worker)

        monkeypatch.setattr(pdf_statement_template_cli, "extract_text_from_pdf", slow_extract)
        db_path = tmp_path / "timeout.sqlite"
        with pytest.raises(PdfStatementImportExtractionError) as exc_info:
            import_pdf_statement_fixture_to_temp_db(
                db_path=db_path,
                pdf_path=SINGLE_PAGE_PDF,
                source_mode="pdf_text",
                pdf_limits=_limits(max_parse_seconds=0.1),
            )
        assert exc_info.value.error_code == PdfExtractionErrorCode.PARSE_TIMEOUT
        assert not db_path.exists()

    def test_content_hash_is_authoritative_when_attachment_path_changes(
        self,
        tmp_path: Path,
    ) -> None:
        first_path = tmp_path / "first.pdf"
        moved_path = tmp_path / "renamed.pdf"
        shutil.copyfile(SINGLE_PAGE_PDF, first_path)
        shutil.copyfile(SINGLE_PAGE_PDF, moved_path)

        first = build_parse_result_from_extracted_pdf(first_path, DEFAULT_TEMPLATE_ID)
        moved = build_parse_result_from_extracted_pdf(moved_path, DEFAULT_TEMPLATE_ID)
        assert first.source_content_hash == moved.source_content_hash
        assert first.pdf_path != moved.pdf_path
        assert all(row.source_content_hash == first.source_content_hash for row in first.rows)
        assert all(row.source_path == str(first_path.resolve()) for row in first.rows)
        assert all(row.source_path == str(moved_path.resolve()) for row in moved.rows)

    def test_attachment_path_remains_preserved_as_import_evidence(self, tmp_path: Path) -> None:
        source_path = tmp_path / "evidence.pdf"
        shutil.copyfile(SINGLE_PAGE_PDF, source_path)
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "evidence.sqlite",
            pdf_path=source_path,
            source_mode="pdf_text",
        )
        expected_path = str(source_path.resolve())
        assert result.pdf_path == expected_path
        assert result.review_queue.summary.attachment_path == expected_path
        assert all(
            row.attachment_path == expected_path for row in result.adapter_result.adapted_rows
        )
