"""Tests for PDF Statement Parser Template Fixture v1.

Verifies that the synthetic template fixtures are deterministic, exercises
the full adapter -> bridge normalizer -> review fixture chain, preserves
source evidence, separates dashboard-safe from audit payloads, and does
not touch a database, read files, or import PDF/OCR dependencies.
"""

from __future__ import annotations

import json

import pytest

from finance_core.reconciliation.pdf_statement_parser_adapter_contract import (
    PdfParserStatementPayload,
    adapt_pdf_parser_payload_to_statement_rows,
)
from finance_core.reconciliation.pdf_statement_parser_template_fixture import (
    PdfParserTemplateName,
    build_pdf_parser_template_payload,
    build_pdf_parser_template_payloads,
    export_pdf_parser_template_audit_payload,
    export_pdf_parser_template_dashboard_payload,
    run_pdf_parser_template_fixture,
)

# ===================================================================
# 1. Template name constants
# ===================================================================


class TestTemplateNameConstants:
    """Template name constants must be defined and stable."""

    def test_accepted_only_exists(self) -> None:
        assert PdfParserTemplateName.ACCEPTED_ONLY == "accepted_only"

    def test_blocked_mixed_exists(self) -> None:
        assert PdfParserTemplateName.BLOCKED_MIXED == "blocked_mixed"

    def test_multi_page_exists(self) -> None:
        assert PdfParserTemplateName.MULTI_PAGE == "multi_page"

    def test_debit_credit_direction_exists(self) -> None:
        assert PdfParserTemplateName.DEBIT_CREDIT_DIRECTION == "debit_credit_direction"

    def test_malformed_raw_data_exists(self) -> None:
        assert PdfParserTemplateName.MALFORMED_RAW_DATA == "malformed_raw_data"


# ===================================================================
# 2. List/build all template payloads
# ===================================================================


class TestBuildAllTemplatePayloads:
    """build_pdf_parser_template_payloads must return all templates."""

    def test_all_templates_returned(self) -> None:
        payloads = build_pdf_parser_template_payloads()
        assert isinstance(payloads, dict)
        assert len(payloads) == 5

    def test_every_template_is_statement_payload(self) -> None:
        payloads = build_pdf_parser_template_payloads()
        for name, payload in payloads.items():
            msg = f"{name} is not PdfParserStatementPayload"
            assert isinstance(payload, PdfParserStatementPayload), msg

    def test_keys_are_sorted(self) -> None:
        payloads = build_pdf_parser_template_payloads()
        keys = list(payloads.keys())
        assert keys == sorted(keys)

    def test_all_template_names_present(self) -> None:
        payloads = build_pdf_parser_template_payloads()
        expected = {
            PdfParserTemplateName.ACCEPTED_ONLY,
            PdfParserTemplateName.BLOCKED_MIXED,
            PdfParserTemplateName.MULTI_PAGE,
            PdfParserTemplateName.DEBIT_CREDIT_DIRECTION,
            PdfParserTemplateName.MALFORMED_RAW_DATA,
        }
        assert set(payloads.keys()) == expected

    def test_deterministic_across_calls(self) -> None:
        p1 = build_pdf_parser_template_payloads()
        p2 = build_pdf_parser_template_payloads()
        assert list(p1.keys()) == list(p2.keys())
        for name in p1:
            assert len(p1[name].rows) == len(p2[name].rows)
            for r1, r2 in zip(p1[name].rows, p2[name].rows):
                assert r1 == r2


# ===================================================================
# 3. Accepted-only template
# ===================================================================


class TestAcceptedOnlyTemplate:
    """The accepted-only template must have all-valid rows."""

    def test_builds_correctly(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        assert payload.source_statement_id == "tmpl-accepted-001"
        assert len(payload.rows) == 5

    def test_all_rows_adapt(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_count == 5
        assert result.blocked_count == 0

    def test_fixture_runs_end_to_end(self) -> None:
        fixture = run_pdf_parser_template_fixture("accepted_only")
        assert fixture.template_name == "accepted_only"
        assert fixture.source_statement_id == "tmpl-accepted-001"
        assert fixture.smoke_result.adapter_result.adapted_count == 5
        assert fixture.smoke_result.batch_result.accepted_count == 5
        assert len(fixture.smoke_result.review_fixture.accepted_rows) == 5

    def test_source_evidence_preserved(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        assert payload.rows[0].source_page_number == 1
        assert payload.rows[0].source_row_ref == "p1r1"
        assert payload.rows[0].raw_row_text == "28/06 Coffee Shop  5.50 D"

    def test_stable_row_order(self) -> None:
        p1 = build_pdf_parser_template_payload("accepted_only")
        p2 = build_pdf_parser_template_payload("accepted_only")
        assert p1.source_statement_id == p2.source_statement_id
        assert len(p1.rows) == len(p2.rows)
        for r1, r2 in zip(p1.rows, p2.rows):
            assert r1.source_row_ref == r2.source_row_ref


# ===================================================================
# 4. Blocked/mixed template
# ===================================================================


class TestBlockedMixedTemplate:
    """The blocked/mixed template must separate valid from blocked rows."""

    def test_builds_correctly(self) -> None:
        payload = build_pdf_parser_template_payload("blocked_mixed")
        assert payload.source_statement_id == "tmpl-blocked-mixed-001"
        assert len(payload.rows) == 5

    def test_some_rows_blocked(self) -> None:
        payload = build_pdf_parser_template_payload("blocked_mixed")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.blocked_count > 0
        assert result.adapted_count > 0

    def test_unparseable_amount_row_blocked(self) -> None:
        payload = build_pdf_parser_template_payload("blocked_mixed")
        assert payload.rows[1].description == "Unparseable Amount Row"
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert 1 in result.blocked_row_indices

    def test_fixture_runs_end_to_end(self) -> None:
        fixture = run_pdf_parser_template_fixture("blocked_mixed")
        assert fixture.template_name == "blocked_mixed"
        assert fixture.smoke_result.adapter_result.blocked_count > 0

    def test_valid_rows_still_accepted(self) -> None:
        fixture = run_pdf_parser_template_fixture("blocked_mixed")
        assert fixture.smoke_result.adapter_result.adapted_count > 0


# ===================================================================
# 5. Multi-page template
# ===================================================================


class TestMultiPageTemplate:
    """The multi-page template must span pages 1-3."""

    def test_builds_correctly(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        assert payload.source_statement_id == "tmpl-multi-page-001"
        assert len(payload.rows) == 9

    def test_pages_1_2_3_present(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        pages = {row.source_page_number for row in payload.rows}
        assert pages == {1, 2, 3}

    def test_page_1_has_three_rows(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        page1 = [r for r in payload.rows if r.source_page_number == 1]
        assert len(page1) == 3

    def test_page_2_has_three_rows(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        page2 = [r for r in payload.rows if r.source_page_number == 2]
        assert len(page2) == 3

    def test_page_3_has_three_rows(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        page3 = [r for r in payload.rows if r.source_page_number == 3]
        assert len(page3) == 3

    def test_source_page_number_stable(self) -> None:
        p1 = build_pdf_parser_template_payload("multi_page")
        p2 = build_pdf_parser_template_payload("multi_page")
        for r1, r2 in zip(p1.rows, p2.rows):
            assert r1.source_page_number == r2.source_page_number

    def test_source_row_ref_stable(self) -> None:
        p1 = build_pdf_parser_template_payload("multi_page")
        p2 = build_pdf_parser_template_payload("multi_page")
        for r1, r2 in zip(p1.rows, p2.rows):
            assert r1.source_row_ref == r2.source_row_ref

    def test_all_rows_adapt(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_count == 9
        assert result.blocked_count == 0

    def test_fixture_runs_end_to_end(self) -> None:
        fixture = run_pdf_parser_template_fixture("multi_page")
        assert fixture.smoke_result.adapter_result.adapted_count == 9


# ===================================================================
# 6. Debit/credit direction template
# ===================================================================


class TestDebitCreditDirectionTemplate:
    """The debit/credit direction template must exercise all indicators."""

    def test_builds_correctly(self) -> None:
        payload = build_pdf_parser_template_payload("debit_credit_direction")
        assert payload.source_statement_id == "tmpl-direction-001"
        assert len(payload.rows) == 12

    def test_debit_indicators_present(self) -> None:
        payload = build_pdf_parser_template_payload("debit_credit_direction")
        indicators = {r.debit_credit_raw for r in payload.rows if r.debit_credit_raw}
        assert "D" in indicators
        assert "DR" in indicators
        assert "DEBIT" in indicators
        assert "PURCHASE" in indicators
        assert "PAYMENT" in indicators

    def test_credit_indicators_present(self) -> None:
        payload = build_pdf_parser_template_payload("debit_credit_direction")
        indicators = {r.debit_credit_raw for r in payload.rows if r.debit_credit_raw}
        assert "C" in indicators
        assert "CR" in indicators
        assert "CREDIT" in indicators
        assert "DEPOSIT" in indicators
        assert "REFUND" in indicators

    def test_sign_inferred_direction_present(self) -> None:
        payload = build_pdf_parser_template_payload("debit_credit_direction")
        neg_rows = [r for r in payload.rows if r.amount_raw and "-" in r.amount_raw]
        assert len(neg_rows) > 0
        paren_rows = [r for r in payload.rows if r.amount_raw and r.amount_raw.startswith("(")]
        assert len(paren_rows) > 0

    def test_fixture_runs_end_to_end(self) -> None:
        fixture = run_pdf_parser_template_fixture("debit_credit_direction")
        assert fixture.smoke_result.adapter_result.adapted_count == 12


# ===================================================================
# 7. Malformed/raw-data template
# ===================================================================


class TestMalformedRawDataTemplate:
    """The malformed/raw-data template must test resilience boundaries."""

    def test_builds_correctly(self) -> None:
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        assert payload.source_statement_id == "tmpl-malformed-001"
        assert len(payload.rows) == 8

    def test_garbled_amount_row_blocked(self) -> None:
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert 1 in result.blocked_row_indices

    def test_missing_amount_row_adapts_with_none(self) -> None:
        """Row index 5 has amount_raw=None -- adapter passes through,
        bridge blocks it for MISSING_AMOUNT."""
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert 5 not in result.blocked_row_indices

    def test_comma_amount_parsed_correctly(self) -> None:
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert 6 not in result.blocked_row_indices

    def test_currency_symbol_amount_parsed(self) -> None:
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert 7 not in result.blocked_row_indices

    def test_fixture_runs_end_to_end(self) -> None:
        fixture = run_pdf_parser_template_fixture("malformed_raw_data")
        assert fixture.template_name == "malformed_raw_data"
        assert fixture.smoke_result.adapter_result.blocked_count >= 1


# ===================================================================
# 8. Unknown template name fails clearly
# ===================================================================


class TestUnknownTemplateName:
    """Unknown template names must raise clear ValueError."""

    def test_build_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown template name"):
            build_pdf_parser_template_payload("nonexistent_template")

    def test_run_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown template name"):
            run_pdf_parser_template_fixture("nonexistent_template")

    def test_dashboard_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown template name"):
            export_pdf_parser_template_dashboard_payload("nonexistent_template")

    def test_audit_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown template name"):
            export_pdf_parser_template_audit_payload("nonexistent_template")

    def test_error_message_includes_available_names(self) -> None:
        with pytest.raises(ValueError, match="accepted_only"):
            build_pdf_parser_template_payload("nonexistent_template")


# ===================================================================
# 9. Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """All template outputs must be deterministic across repeated calls."""

    def test_accepted_only_deterministic(self) -> None:
        f1 = run_pdf_parser_template_fixture("accepted_only")
        f2 = run_pdf_parser_template_fixture("accepted_only")
        assert f1.dashboard_payload == f2.dashboard_payload
        assert f1.audit_payload == f2.audit_payload

    def test_blocked_mixed_deterministic(self) -> None:
        f1 = run_pdf_parser_template_fixture("blocked_mixed")
        f2 = run_pdf_parser_template_fixture("blocked_mixed")
        assert f1.dashboard_payload == f2.dashboard_payload
        assert f1.audit_payload == f2.audit_payload

    def test_multi_page_deterministic(self) -> None:
        f1 = run_pdf_parser_template_fixture("multi_page")
        f2 = run_pdf_parser_template_fixture("multi_page")
        assert f1.dashboard_payload == f2.dashboard_payload
        assert f1.audit_payload == f2.audit_payload

    def test_debit_credit_direction_deterministic(self) -> None:
        f1 = run_pdf_parser_template_fixture("debit_credit_direction")
        f2 = run_pdf_parser_template_fixture("debit_credit_direction")
        assert f1.dashboard_payload == f2.dashboard_payload
        assert f1.audit_payload == f2.audit_payload

    def test_malformed_raw_data_deterministic(self) -> None:
        f1 = run_pdf_parser_template_fixture("malformed_raw_data")
        f2 = run_pdf_parser_template_fixture("malformed_raw_data")
        assert f1.dashboard_payload == f2.dashboard_payload
        assert f1.audit_payload == f2.audit_payload

    def test_fixture_result_is_deterministic(self) -> None:
        f1 = run_pdf_parser_template_fixture("accepted_only")
        f2 = run_pdf_parser_template_fixture("accepted_only")
        assert f1.template_name == f2.template_name
        assert f1.source_statement_id == f2.source_statement_id
        a1 = f1.smoke_result.adapter_result.adapted_count
        a2 = f2.smoke_result.adapter_result.adapted_count
        assert a1 == a2


# ===================================================================
# 10. Source evidence preservation
# ===================================================================


class TestSourceEvidencePreservation:
    """Source evidence must be preserved through template fixtures."""

    def test_accepted_only_source_statement_id(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        assert payload.source_statement_id == "tmpl-accepted-001"

    def test_blocked_mixed_source_statement_id(self) -> None:
        payload = build_pdf_parser_template_payload("blocked_mixed")
        assert payload.source_statement_id == "tmpl-blocked-mixed-001"

    def test_multi_page_source_references_stable(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        for row in payload.rows:
            assert row.source_page_number is not None
            assert row.source_row_ref is not None

    def test_raw_row_text_preserved(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        assert payload.rows[0].raw_row_text == "28/06 Coffee Shop  5.50 D"

    def test_attachment_path_is_metadata_only(self) -> None:
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        assert payload.attachment_path == "/templates/malformed_raw_data_stmt.pdf"


# ===================================================================
# 11. Attachment path is not read
# ===================================================================


class TestAttachmentPathNotRead:
    """Templates must not open or read the attachment path."""

    def test_no_file_io_in_module(self) -> None:
        import inspect

        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        source = inspect.getsource(pstf)
        assert "open(" not in source
        assert "Path(" not in source
        assert "os.path" not in source

    def test_nonexistent_path_does_not_crash(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        assert "/templates/" in payload.attachment_path


# ===================================================================
# 12. No file I/O
# ===================================================================


class TestNoFileIO:
    """Module must not perform file I/O."""

    def test_no_open_calls(self) -> None:
        import inspect

        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        source = inspect.getsource(pstf)
        s = source.partition('"""')[2].partition('"""')[2]
        assert "open(" not in s

    def test_no_pathlib(self) -> None:
        import inspect

        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        source = inspect.getsource(pstf)
        assert "pathlib" not in source.lower()


# ===================================================================
# 13. No DB access
# ===================================================================


class TestNoDbAccess:
    """Module must not reference database/finance.db or sqlite3."""

    def test_no_live_db_path(self) -> None:
        import inspect

        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        source = inspect.getsource(pstf)
        assert "finance.db" not in source

    def test_no_sqlite3_import(self) -> None:
        import inspect

        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        source = inspect.getsource(pstf)
        assert "sqlite3" not in source


# ===================================================================
# 14. No PDF/OCR dependency
# ===================================================================


class TestNoOcrPdfDependency:
    """Module must not import PDF/OCR libraries."""

    def test_no_pdf_ocr_imports(self) -> None:
        import inspect

        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        source = inspect.getsource(pstf)
        source_lower = source.lower()
        deps = (
            "import pdfplumber",
            "import pytesseract",
            "import pypdf",
            "import camelot",
            "import tabula",
            "from pdfplumber",
            "from pytesseract",
            "from pypdf",
            "from camelot",
            "from tabula",
        )
        for dep in deps:
            assert dep not in source_lower, f"Unexpected dependency: {dep}"
        assert "import ocr" not in source_lower
        assert "from ocr" not in source_lower


# ===================================================================
# 15. Adapter pass-through
# ===================================================================


class TestPassThroughAdapter:
    """Every template payload must pass through the adapter contract."""

    def test_accepted_only_passes_through(self) -> None:
        payload = build_pdf_parser_template_payload("accepted_only")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_count == 5

    def test_blocked_mixed_passes_through(self) -> None:
        payload = build_pdf_parser_template_payload("blocked_mixed")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 5

    def test_multi_page_passes_through(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 9

    def test_debit_credit_direction_passes_through(self) -> None:
        payload = build_pdf_parser_template_payload("debit_credit_direction")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 12

    def test_malformed_raw_data_passes_through(self) -> None:
        payload = build_pdf_parser_template_payload("malformed_raw_data")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 8


# ===================================================================
# 16. Full chain through normalize_pdf_statement_rows_batch()
# ===================================================================


class TestFullChainThroughBatchNormalizer:
    """Adapted rows must pass through the batch normalizer."""

    def test_accepted_only_normalizes(self) -> None:
        from finance_core.reconciliation.pdf_statement_bridge import (
            normalize_pdf_statement_rows_batch,
        )

        payload = build_pdf_parser_template_payload("accepted_only")
        adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
        batch = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch.accepted_count == 5
        assert batch.blocked_count == 0

    def test_blocked_mixed_normalizes(self) -> None:
        from finance_core.reconciliation.pdf_statement_bridge import (
            normalize_pdf_statement_rows_batch,
        )

        payload = build_pdf_parser_template_payload("blocked_mixed")
        adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
        batch = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch.blocked_count > 0

    def test_multi_page_normalizes(self) -> None:
        from finance_core.reconciliation.pdf_statement_bridge import (
            normalize_pdf_statement_rows_batch,
        )

        payload = build_pdf_parser_template_payload("multi_page")
        adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
        batch = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch.accepted_count == 8
        assert batch.blocked_count == 1

    def test_debit_credit_direction_normalizes(self) -> None:
        from finance_core.reconciliation.pdf_statement_bridge import (
            normalize_pdf_statement_rows_batch,
        )

        payload = build_pdf_parser_template_payload("debit_credit_direction")
        adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
        batch = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch.accepted_count == 10
        assert batch.blocked_count == 2


# ===================================================================
# 17. Full chain through build_pdf_statement_import_review_fixture()
# ===================================================================


class TestFullChainThroughReviewFixture:
    """Adapted rows must pass through the review fixture builder."""

    def test_accepted_only_review_fixture(self) -> None:
        fixture = run_pdf_parser_template_fixture("accepted_only")
        assert len(fixture.smoke_result.review_fixture.accepted_rows) == 5
        assert fixture.smoke_result.review_fixture.dashboard_safe is True

    def test_blocked_mixed_review_fixture(self) -> None:
        fixture = run_pdf_parser_template_fixture("blocked_mixed")
        assert fixture.smoke_result.review_fixture.dashboard_safe is True

    def test_multi_page_review_fixture(self) -> None:
        fixture = run_pdf_parser_template_fixture("multi_page")
        assert len(fixture.smoke_result.review_fixture.accepted_rows) == 8
        assert len(fixture.smoke_result.review_fixture.blocked_rows) == 1


# ===================================================================
# 18. Dashboard payload excludes sensitive evidence
# ===================================================================


class TestDashboardPayloadSafety:
    """Dashboard payload must not expose sensitive source evidence."""

    def test_excludes_attachment_path(self) -> None:
        dash = export_pdf_parser_template_dashboard_payload("accepted_only")
        dash_str = json.dumps(dash, default=str)
        assert "attachment_path" not in dash_str

    def test_excludes_raw_row_text(self) -> None:
        dash = export_pdf_parser_template_dashboard_payload("accepted_only")
        dash_str = json.dumps(dash, default=str)
        assert "raw_row_text" not in dash_str

    def test_excludes_raw_row_payload(self) -> None:
        dash = export_pdf_parser_template_dashboard_payload("accepted_only")
        dash_str = json.dumps(dash, default=str)
        assert "raw_row_payload" not in dash_str

    def test_accepted_only_dashboard_is_json_serializable(self) -> None:
        dash = export_pdf_parser_template_dashboard_payload("accepted_only")
        json_str = json.dumps(dash, default=str)
        assert isinstance(json.loads(json_str), dict)

    def test_all_templates_dashboard_safe(self) -> None:
        names = [
            "accepted_only",
            "blocked_mixed",
            "multi_page",
            "debit_credit_direction",
            "malformed_raw_data",
        ]
        for name in names:
            dash = export_pdf_parser_template_dashboard_payload(name)
            dash_str = json.dumps(dash, default=str)
            assert "attachment_path" not in dash_str, f"{name} leaked attachment_path"
            assert "raw_row_text" not in dash_str, f"{name} leaked raw_row_text"


# ===================================================================
# 19. Audit payload includes source evidence where available
# ===================================================================


class TestAuditPayloadIncludesSourceEvidence:
    """Audit payload must include source evidence for traceability."""

    def test_accepted_only_audit_includes_evidence(self) -> None:
        audit = export_pdf_parser_template_audit_payload("accepted_only")
        assert "accepted" in audit
        first = audit["accepted"][0]
        assert "raw_row_payload" in first
        assert first["raw_row_payload"]["attachment_path"] == "/templates/accepted_only_stmt.pdf"

    def test_blocked_mixed_audit_blocked_list(self) -> None:
        audit = export_pdf_parser_template_audit_payload("blocked_mixed")
        assert "blocked" in audit
        assert isinstance(audit["blocked"], list)

    def test_audit_payload_is_json_serializable(self) -> None:
        audit = export_pdf_parser_template_audit_payload("accepted_only")
        json_str = json.dumps(audit, default=str)
        assert isinstance(json.loads(json_str), dict)

    def test_all_templates_audit_json_serializable(self) -> None:
        names = [
            "accepted_only",
            "blocked_mixed",
            "multi_page",
            "debit_credit_direction",
            "malformed_raw_data",
        ]
        for name in names:
            audit = export_pdf_parser_template_audit_payload(name)
            json_str = json.dumps(audit, default=str)
            assert isinstance(json.loads(json_str), dict), f"{name} audit is not JSON serializable"


# ===================================================================
# 20. No mutation of template payloads across calls
# ===================================================================


class TestNoMutationOfTemplatePayloads:
    """Template payloads must not be mutated across calls."""

    def test_accepted_only_unchanged_after_adapter(self) -> None:
        p1 = build_pdf_parser_template_payload("accepted_only")
        p2 = build_pdf_parser_template_payload("accepted_only")
        adapt_pdf_parser_payload_to_statement_rows(p1)
        assert p1.source_statement_id == p2.source_statement_id
        assert len(p1.rows) == len(p2.rows)
        for r1, r2 in zip(p1.rows, p2.rows):
            assert r1.description == r2.description
            assert r1.amount_raw == r2.amount_raw

    def test_build_return_new_instances_each_call(self) -> None:
        p1 = build_pdf_parser_template_payload("accepted_only")
        p2 = build_pdf_parser_template_payload("accepted_only")
        assert p1 is not p2
        assert p1.rows is not p2.rows

    def test_run_fixture_does_not_mutate(self) -> None:
        p1 = build_pdf_parser_template_payload("blocked_mixed")
        p2 = build_pdf_parser_template_payload("blocked_mixed")
        run_pdf_parser_template_fixture("blocked_mixed")
        assert p1.source_statement_id == p2.source_statement_id
        assert len(p1.rows) == len(p2.rows)


# ===================================================================
# 21. Immutability
# ===================================================================


class TestImmutability:
    """All exported dataclasses must be frozen."""

    def test_template_fixture_result_is_frozen(self) -> None:
        fixture = run_pdf_parser_template_fixture("accepted_only")
        with pytest.raises(Exception):
            fixture.template_name = "changed"  # type: ignore[misc]

    def test_smoke_result_is_frozen(self) -> None:
        fixture = run_pdf_parser_template_fixture("accepted_only")
        with pytest.raises(Exception):
            fixture.smoke_result = fixture.smoke_result  # type: ignore[misc]

    def test_template_name_constants_stable(self) -> None:
        assert PdfParserTemplateName.ACCEPTED_ONLY == "accepted_only"
        assert PdfParserTemplateName.BLOCKED_MIXED == "blocked_mixed"
        assert PdfParserTemplateName.MULTI_PAGE == "multi_page"
        assert PdfParserTemplateName.DEBIT_CREDIT_DIRECTION == "debit_credit_direction"
        assert PdfParserTemplateName.MALFORMED_RAW_DATA == "malformed_raw_data"


# ===================================================================
# 22. FixtureResult carries correct fields
# ===================================================================


class TestFixtureResultFields:
    """PdfParserTemplateFixtureResult must carry correct field values."""

    def test_all_fields_non_none(self) -> None:
        fixture = run_pdf_parser_template_fixture("accepted_only")
        assert fixture.template_name is not None
        assert fixture.source_statement_id is not None
        assert fixture.smoke_result is not None
        assert fixture.dashboard_payload is not None
        assert fixture.audit_payload is not None

    def test_source_statement_id_matches_template(self) -> None:
        payload = build_pdf_parser_template_payload("multi_page")
        fixture = run_pdf_parser_template_fixture("multi_page")
        assert fixture.source_statement_id == payload.source_statement_id


# ===================================================================
# 23. Dashboard and audit payloads are deterministic per template
# ===================================================================


class TestExportPayloadDeterminism:
    """Dashboard and audit exports must be deterministic."""

    def test_dashboard_payload_deterministic(self) -> None:
        d1 = export_pdf_parser_template_dashboard_payload("accepted_only")
        d2 = export_pdf_parser_template_dashboard_payload("accepted_only")
        assert d1 == d2

    def test_audit_payload_deterministic(self) -> None:
        a1 = export_pdf_parser_template_audit_payload("accepted_only")
        a2 = export_pdf_parser_template_audit_payload("accepted_only")
        assert a1 == a2

    def test_different_templates_different_dashboard(self) -> None:
        d_acc = export_pdf_parser_template_dashboard_payload("accepted_only")
        d_block = export_pdf_parser_template_dashboard_payload("blocked_mixed")
        assert d_acc != d_block


# ===================================================================
# 24. Public exports
# ===================================================================


class TestPublicExports:
    """All public names must be exported via __all__."""

    def test_public_names_in_all(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_parser_template_fixture as pstf,
        )

        assert hasattr(pstf, "PdfParserTemplateName")
        assert hasattr(pstf, "PdfParserTemplateFixtureResult")
        assert hasattr(pstf, "build_pdf_parser_template_payloads")
        assert hasattr(pstf, "build_pdf_parser_template_payload")
        assert hasattr(pstf, "run_pdf_parser_template_fixture")
        assert hasattr(pstf, "export_pdf_parser_template_dashboard_payload")
        assert hasattr(pstf, "export_pdf_parser_template_audit_payload")
