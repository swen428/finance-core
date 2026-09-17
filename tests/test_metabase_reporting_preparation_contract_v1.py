from __future__ import annotations

from finance_core.reconciliation.reporting_constants import (
    BLOCKED_REASONS,
    DASHBOARD_SECTIONS,
    REPORTING_FIELD_MAP,
    REPORTING_FIELDS,
    REPORTING_SOURCE_GROUPS,
    FieldClassification,
)

# Alias for backward-compatible test references
_field_map = REPORTING_FIELD_MAP

# ===================================================================
# 1. Dashboard-safe fields exclude or flag raw text / sensitive fields
# ===================================================================


class TestSensitiveFieldClassification:
    """raw_text, raw_row_text, attachment_path, and other sensitive fields
    must not be classified as dashboard_safe."""

    def test_raw_text_is_sensitive(self) -> None:
        fm = _field_map
        assert fm["raw_text"].classification == FieldClassification.SENSITIVE

    def test_raw_row_text_is_sensitive(self) -> None:
        fm = _field_map
        assert fm["raw_row_text"].classification == FieldClassification.SENSITIVE

    def test_attachment_path_is_sensitive(self) -> None:
        fm = _field_map
        assert fm["attachment_path"].classification == FieldClassification.SENSITIVE

    def test_source_file_is_sensitive(self) -> None:
        fm = _field_map
        assert fm["source_file"].classification == FieldClassification.SENSITIVE

    def test_decision_note_is_sensitive(self) -> None:
        fm = _field_map
        assert fm["decision_note"].classification == FieldClassification.SENSITIVE

    def test_reviewer_is_sensitive(self) -> None:
        fm = _field_map
        assert fm["reviewer"].classification == FieldClassification.SENSITIVE

    def test_raw_amount_is_audit_only(self) -> None:
        fm = _field_map
        assert fm["raw_amount"].classification == FieldClassification.AUDIT_ONLY

    def test_evidence_refs_is_audit_only(self) -> None:
        fm = _field_map
        assert fm["evidence_refs"].classification == FieldClassification.AUDIT_ONLY

    def test_guard_decision_refs_is_audit_only(self) -> None:
        fm = _field_map
        assert fm["guard_decision_refs"].classification == FieldClassification.AUDIT_ONLY

    def test_no_sensitive_field_is_dashboard_safe(self) -> None:
        for f in REPORTING_FIELDS:
            if f.classification in (FieldClassification.SENSITIVE, FieldClassification.AUDIT_ONLY):
                assert f.classification != FieldClassification.DASHBOARD_SAFE, (
                    f"{f.name} is {f.classification.value} -- must not be dashboard_safe"
                )


# ===================================================================
# 2. Attachment path is treated as sensitive / audit-only
# ===================================================================


class TestAttachmentPathSensitivity:
    """attachment_path must not appear in dashboard_safe fields."""

    def test_attachment_path_not_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["attachment_path"].classification != FieldClassification.DASHBOARD_SAFE

    def test_source_file_not_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["source_file"].classification != FieldClassification.DASHBOARD_SAFE


# ===================================================================
# 3. Amount / date / currency fields are included in reporting mappings
# ===================================================================


class TestCoreFinancialFieldsIncluded:
    """amount, transaction_date, posted_date, and currency must all be
    present in the reporting field registry."""

    def test_amount_is_included(self) -> None:
        fm = _field_map
        assert "amount" in fm
        assert fm["amount"].classification == FieldClassification.DASHBOARD_SAFE

    def test_currency_is_included(self) -> None:
        fm = _field_map
        assert "currency" in fm
        assert fm["currency"].classification == FieldClassification.DASHBOARD_SAFE

    def test_transaction_date_is_included(self) -> None:
        fm = _field_map
        assert "transaction_date" in fm
        assert fm["transaction_date"].classification == FieldClassification.DASHBOARD_SAFE

    def test_posted_date_is_included(self) -> None:
        fm = _field_map
        assert "posted_date" in fm
        assert fm["posted_date"].classification == FieldClassification.DASHBOARD_SAFE

    def test_amount_direction_is_included(self) -> None:
        fm = _field_map
        assert "amount_direction" in fm
        assert fm["amount_direction"].classification == FieldClassification.DASHBOARD_SAFE

    def test_amount_delta_is_included(self) -> None:
        fm = _field_map
        assert "amount_delta" in fm
        assert fm["amount_delta"].classification == FieldClassification.DASHBOARD_SAFE


# ===================================================================
# 4. transaction_date and posted_date remain separate reporting fields
# ===================================================================


class TestDateSeparation:
    """transaction_date and posted_date must be separate, independently
    classified reporting fields."""

    def test_both_date_fields_exist(self) -> None:
        fm = _field_map
        assert "transaction_date" in fm
        assert "posted_date" in fm

    def test_dates_are_separate_entries(self) -> None:
        fm = _field_map
        txn = fm["transaction_date"]
        posted = fm["posted_date"]
        assert txn is not posted
        assert txn.name != posted.name

    def test_both_dates_are_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["transaction_date"].classification == FieldClassification.DASHBOARD_SAFE
        assert fm["posted_date"].classification == FieldClassification.DASHBOARD_SAFE

    def test_date_delta_days_is_separate(self) -> None:
        fm = _field_map
        assert "date_delta_days" in fm
        assert fm["date_delta_days"].expected_type == "int"


# ===================================================================
# 5. blocked_reason_codes and requires_human_review are included
# ===================================================================


class TestBlockedAndReviewFields:
    """blocked_reason_codes and requires_human_review must be included in
    the reporting field registry as dashboard_safe."""

    def test_blocked_reason_codes_is_included(self) -> None:
        fm = _field_map
        assert "blocked_reason_codes" in fm
        assert fm["blocked_reason_codes"].classification == FieldClassification.DASHBOARD_SAFE

    def test_requires_human_review_is_included(self) -> None:
        fm = _field_map
        assert "requires_human_review" in fm
        assert fm["requires_human_review"].classification == FieldClassification.DASHBOARD_SAFE

    def test_execution_status_is_included(self) -> None:
        fm = _field_map
        assert "execution_status" in fm
        assert fm["execution_status"].classification == FieldClassification.DASHBOARD_SAFE


# ===================================================================
# 6. evidence_refs are audit-capable but not blindly dashboard-safe
# ===================================================================


class TestEvidenceRefsClassification:
    """evidence_refs must be audit_only, not dashboard_safe."""

    def test_evidence_refs_is_audit_only(self) -> None:
        fm = _field_map
        assert fm["evidence_refs"].classification == FieldClassification.AUDIT_ONLY

    def test_guard_decision_refs_is_audit_only(self) -> None:
        fm = _field_map
        assert fm["guard_decision_refs"].classification == FieldClassification.AUDIT_ONLY

    def test_evidence_refs_not_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["evidence_refs"].classification != FieldClassification.DASHBOARD_SAFE
        assert fm["guard_decision_refs"].classification != FieldClassification.DASHBOARD_SAFE

    def test_evidence_refs_still_present(self) -> None:
        fm = _field_map
        assert "evidence_refs" in fm
        assert "guard_decision_refs" in fm


# ===================================================================
# 7. Reporting source groups include import, match, review, apply,
#    evidence, and PDF readiness concepts
# ===================================================================


class TestSourceGroupCoverage:
    """All 10 reporting source groups defined in the design doc must be
    present."""

    REQUIRED_GROUP_NAMES = frozenset(
        {
            "statement_import_health",
            "reconciliation_match_outcomes",
            "review_queue_status",
            "review_resolution_outcomes",
            "guarded_apply_plan_status",
            "guarded_apply_execution_status",
            "human_review_required_items",
            "blocked_apply_operations",
            "evidence_completeness",
            "pdf_import_readiness",
        }
    )

    def test_all_ten_source_groups_present(self) -> None:
        actual_names = {g.name for g in REPORTING_SOURCE_GROUPS}
        missing = self.REQUIRED_GROUP_NAMES - actual_names
        assert not missing, f"Missing source groups: {missing}"
        extra = actual_names - self.REQUIRED_GROUP_NAMES
        assert not extra, f"Unexpected source groups: {extra}"

    def test_source_group_count(self) -> None:
        assert len(REPORTING_SOURCE_GROUPS) == 10

    def test_every_group_has_label(self) -> None:
        for g in REPORTING_SOURCE_GROUPS:
            assert g.label, f"Source group {g.name} has empty label"

    def test_every_group_has_sources(self) -> None:
        for g in REPORTING_SOURCE_GROUPS:
            assert len(g.sources) > 0, f"Source group {g.name} has no sources"

    def test_import_health_includes_csv_and_pdf(self) -> None:
        import_group = next(
            g for g in REPORTING_SOURCE_GROUPS if g.name == "statement_import_health"
        )
        source_labels = " ".join(import_group.sources).lower()
        assert "csv" in source_labels
        assert "pdf" in source_labels


# ===================================================================
# 8. All future views / query contracts are read-only
# ===================================================================


class TestReadOnlyContract:
    """Future reporting views must satisfy the read-only contract defined
    in Section 7.1 of the design doc."""

    READ_ONLY_PROPERTIES = (
        "only SELECT statements",
        "deterministic output",
        "no database/finance.db",
        "no state mutation",
        "stable field names",
    )

    FUTURE_VIEW_NAMES = frozenset(
        {
            "statement_import_health",
            "match_outcomes_by_run",
            "review_queue_status",
            "apply_execution_audit",
            "blocked_items",
            "evidence_completeness",
            "pdf_readiness",
        }
    )

    def test_read_only_properties_defined(self) -> None:
        assert len(self.READ_ONLY_PROPERTIES) == 5

    def test_future_view_candidates_defined(self) -> None:
        assert len(self.FUTURE_VIEW_NAMES) == 7

    def test_future_view_names_stable(self) -> None:
        assert "statement_import_health" in self.FUTURE_VIEW_NAMES
        assert "match_outcomes_by_run" in self.FUTURE_VIEW_NAMES
        assert "review_queue_status" in self.FUTURE_VIEW_NAMES
        assert "apply_execution_audit" in self.FUTURE_VIEW_NAMES
        assert "blocked_items" in self.FUTURE_VIEW_NAMES
        assert "evidence_completeness" in self.FUTURE_VIEW_NAMES
        assert "pdf_readiness" in self.FUTURE_VIEW_NAMES

    def test_no_write_keywords_in_contract(self) -> None:
        forbidden = {
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "ALTER",
            "DROP",
            "REPLACE",
            "ATTACH",
            "DETACH",
        }
        assert len(forbidden) == 9


# ===================================================================
# 9. No live database file is referenced
# ===================================================================


class TestNoLiveDatabaseReference:
    """Contract tests must not import or reference database/finance.db."""

    def test_no_live_db_path_in_constants(self) -> None:
        all_text = " ".join(f.name for f in REPORTING_FIELDS)
        all_text += " ".join(g.name for g in REPORTING_SOURCE_GROUPS)
        all_text += " ".join(s.name for s in DASHBOARD_SECTIONS)
        all_text += " ".join(r.code for r in BLOCKED_REASONS)
        assert "database/finance.db" not in all_text
        assert "finance.db" not in all_text

    def test_contract_does_not_open_any_database(self) -> None:
        pass


# ===================================================================
# 10. Sensitive fields require masking/truncation or audit-only
#     classification
# ===================================================================


class TestSensitiveFieldMaskingRequirements:
    """Every field classified as SENSITIVE must have documented masking
    or truncation requirements."""

    SENSITIVE_FIELD_NAMES = frozenset(
        {
            "raw_text",
            "raw_row_text",
            "attachment_path",
            "source_file",
            "decision_note",
            "reviewer",
        }
    )

    AUDIT_ONLY_FIELD_NAMES = frozenset(
        {
            "raw_amount",
            "evidence_refs",
            "guard_decision_refs",
        }
    )

    def test_sensitive_fields_match_design_doc(self) -> None:
        actual_sensitive = {
            f.name for f in REPORTING_FIELDS if f.classification == FieldClassification.SENSITIVE
        }
        assert actual_sensitive == self.SENSITIVE_FIELD_NAMES, (
            f"Expected sensitive: {self.SENSITIVE_FIELD_NAMES}, got: {actual_sensitive}"
        )

    def test_audit_only_fields_match_design_doc(self) -> None:
        actual_audit = {
            f.name for f in REPORTING_FIELDS if f.classification == FieldClassification.AUDIT_ONLY
        }
        assert actual_audit == self.AUDIT_ONLY_FIELD_NAMES, (
            f"Expected audit_only: {self.AUDIT_ONLY_FIELD_NAMES}, got: {actual_audit}"
        )

    def test_sensitive_fields_require_masking(self) -> None:
        for name in self.SENSITIVE_FIELD_NAMES:
            fm = _field_map
            assert fm[name].classification == FieldClassification.SENSITIVE

    def test_audit_only_fields_restricted(self) -> None:
        for name in self.AUDIT_ONLY_FIELD_NAMES:
            fm = _field_map
            assert fm[name].classification == FieldClassification.AUDIT_ONLY
            assert fm[name].classification != FieldClassification.DASHBOARD_SAFE


# ===================================================================
# 11. Reporting field names are stable and unique
# ===================================================================


class TestFieldNameStability:
    """All reporting field names must be unique and stable."""

    def test_field_names_are_unique(self) -> None:
        names = [f.name for f in REPORTING_FIELDS]
        assert len(names) == len(set(names)), (
            f"Duplicate field names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_source_group_names_are_unique(self) -> None:
        names = [g.name for g in REPORTING_SOURCE_GROUPS]
        assert len(names) == len(set(names)), (
            f"Duplicate source group names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_dashboard_section_names_are_unique(self) -> None:
        names = [s.name for s in DASHBOARD_SECTIONS]
        assert len(names) == len(set(names)), (
            f"Duplicate dashboard section names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_blocked_reason_codes_are_unique(self) -> None:
        codes = [r.code for r in BLOCKED_REASONS]
        assert len(codes) == len(set(codes)), (
            f"Duplicate blocked reason codes: {[c for c in codes if codes.count(c) > 1]}"
        )

    def test_dashboard_safe_count(self) -> None:
        safe = [
            f for f in REPORTING_FIELDS if f.classification == FieldClassification.DASHBOARD_SAFE
        ]
        assert len(safe) == 27


# ===================================================================
# 12. Blocked reason codes enumeration
# ===================================================================


class TestBlockedReasonCodes:
    """All 14 blocked reason codes defined in Section 8.1 must be present
    and stable."""

    REQUIRED_CODES = frozenset(
        {
            "missing_currency",
            "ambiguous_dates",
            "ambiguous_amount_direction",
            "duplicate_fingerprint",
            "missing_evidence_refs",
            "unsupported_statement_layout",
            "guard_decision_blocked",
            "execution_blocked",
            "execution_partially_blocked",
            "execution_conflict",
            "execution_unsupported",
            "missing_description",
            "missing_amount",
            "unknown_amount_direction",
        }
    )

    def test_all_fourteen_reason_codes_present(self) -> None:
        actual_codes = {r.code for r in BLOCKED_REASONS}
        assert actual_codes == self.REQUIRED_CODES, (
            f"Missing: {self.REQUIRED_CODES - actual_codes}, "
            f"Extra: {actual_codes - self.REQUIRED_CODES}"
        )

    def test_blocked_reason_count(self) -> None:
        assert len(BLOCKED_REASONS) == 14

    def test_every_code_has_source(self) -> None:
        for r in BLOCKED_REASONS:
            assert r.source, f"Blocked reason {r.code} has empty source"

    def test_every_code_has_meaning(self) -> None:
        for r in BLOCKED_REASONS:
            assert r.meaning, f"Blocked reason {r.code} has empty meaning"

    def test_every_code_is_snake_case(self) -> None:
        import re

        snake = re.compile(r"^[a-z][a-z0-9_]*$")
        for r in BLOCKED_REASONS:
            assert snake.match(r.code), f"Blocked reason code {r.code!r} is not snake_case"


# ===================================================================
# 13. Dashboard candidate sections coverage
# ===================================================================


class TestDashboardSections:
    """All 7 dashboard candidate sections must be present."""

    REQUIRED_SECTIONS = frozenset(
        {
            "intake_health",
            "reconciliation_summary",
            "needs_review",
            "apply_execution_audit",
            "blocked_items",
            "evidence_audit_completeness",
            "pdf_import_readiness",
        }
    )

    def test_all_seven_sections_present(self) -> None:
        actual = {s.name for s in DASHBOARD_SECTIONS}
        assert actual == self.REQUIRED_SECTIONS, (
            f"Missing: {self.REQUIRED_SECTIONS - actual}, Extra: {actual - self.REQUIRED_SECTIONS}"
        )

    def test_dashboard_section_count(self) -> None:
        assert len(DASHBOARD_SECTIONS) == 7

    def test_every_section_has_label(self) -> None:
        for s in DASHBOARD_SECTIONS:
            assert s.label, f"Dashboard section {s.name} has empty label"


# ===================================================================
# 14. Cross-cutting safety: no AI authority, no mutation
# ===================================================================


class TestReportingSafetyBoundaries:
    """The reporting layer must maintain Python/SQLite authority and must
    not allow AI to become the reporting authority."""

    def test_no_ai_authority_in_field_descriptions(self) -> None:
        for f in REPORTING_FIELDS:
            desc_lower = f.description.lower()
            assert "ai is the authority" not in desc_lower, f"Field {f.name} claims AI authority"

    def test_reporting_bucket_is_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["reporting_bucket"].classification == FieldClassification.DASHBOARD_SAFE

    def test_reporting_reason_is_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["reporting_reason"].classification == FieldClassification.DASHBOARD_SAFE

    def test_source_type_is_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["source_type"].classification == FieldClassification.DASHBOARD_SAFE

    def test_source_page_number_is_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["source_page_number"].classification == FieldClassification.DASHBOARD_SAFE

    def test_source_row_ref_is_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["source_row_ref"].classification == FieldClassification.DASHBOARD_SAFE

    def test_merchant_similarity_is_dashboard_safe(self) -> None:
        fm = _field_map
        assert fm["merchant_similarity"].classification == FieldClassification.DASHBOARD_SAFE
