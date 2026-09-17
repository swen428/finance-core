"""Tests for Reporting Constants / Field Registry v1.

Verifies that the central reporting constants module exports stable,
immutable field names, source group labels, dashboard section names,
blocked reason codes, and sensitivity classifications matching the
Metabase / Reporting Preparation v1 design contract.

These tests validate the module itself, not the contract test file.
The contract test file (test_metabase_reporting_preparation_contract_v1.py)
should be updated separately to import from this module.
"""

from __future__ import annotations

from finance_core.reconciliation import reporting_constants as rc

# ===================================================================
# 1. Module structure and imports
# ===================================================================


class TestModuleStructure:
    """The module must export all expected top-level symbols."""

    def test_field_classification_enum_exists(self) -> None:
        assert hasattr(rc, "FieldClassification")
        assert rc.FieldClassification.DASHBOARD_SAFE.value == "dashboard_safe"
        assert rc.FieldClassification.AUDIT_ONLY.value == "audit_only"
        assert rc.FieldClassification.SENSITIVE.value == "sensitive"

    def test_reporting_field_dataclass_exists(self) -> None:
        assert hasattr(rc, "ReportingField")
        f = rc.REPORTING_FIELDS[0]
        assert hasattr(f, "name")
        assert hasattr(f, "source_concept")
        assert hasattr(f, "description")
        assert hasattr(f, "expected_type")
        assert hasattr(f, "classification")
        assert hasattr(f, "dashboard_safe")
        assert hasattr(f, "audit_only")
        assert hasattr(f, "sensitive")
        assert hasattr(f, "masking_required")

    def test_reporting_fields_tuple_exists(self) -> None:
        assert hasattr(rc, "REPORTING_FIELDS")
        assert isinstance(rc.REPORTING_FIELDS, tuple)
        assert len(rc.REPORTING_FIELDS) == 36

    def test_reporting_source_groups_tuple_exists(self) -> None:
        assert hasattr(rc, "REPORTING_SOURCE_GROUPS")
        assert isinstance(rc.REPORTING_SOURCE_GROUPS, tuple)
        assert len(rc.REPORTING_SOURCE_GROUPS) == 10

    def test_dashboard_sections_tuple_exists(self) -> None:
        assert hasattr(rc, "DASHBOARD_SECTIONS")
        assert isinstance(rc.DASHBOARD_SECTIONS, tuple)
        assert len(rc.DASHBOARD_SECTIONS) == 7

    def test_blocked_reasons_tuple_exists(self) -> None:
        assert hasattr(rc, "BLOCKED_REASONS")
        assert isinstance(rc.BLOCKED_REASONS, tuple)
        assert len(rc.BLOCKED_REASONS) == 14

    def test_lookup_maps_exist(self) -> None:
        assert hasattr(rc, "REPORTING_FIELD_MAP")
        assert isinstance(rc.REPORTING_FIELD_MAP, dict)
        assert len(rc.REPORTING_FIELD_MAP) == 36
        assert hasattr(rc, "SOURCE_GROUP_MAP")
        assert hasattr(rc, "DASHBOARD_SECTION_MAP")
        assert hasattr(rc, "BLOCKED_REASON_MAP")


# ===================================================================
# 2. Field name uniqueness and stability
# ===================================================================


class TestFieldNameUniqueness:
    """All reporting field names must be unique and stable."""

    def test_field_names_are_unique(self) -> None:
        names = rc.get_field_names()
        assert len(names) == len(set(names)), (
            f"Duplicate field names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_source_group_names_are_unique(self) -> None:
        names = rc.get_source_group_names()
        assert len(names) == len(set(names))

    def test_dashboard_section_names_are_unique(self) -> None:
        names = rc.get_dashboard_section_names()
        assert len(names) == len(set(names))

    def test_blocked_reason_codes_are_unique(self) -> None:
        codes = rc.get_blocked_reason_codes()
        assert len(codes) == len(set(codes))


# ===================================================================
# 3. Required fields exist with correct classifications
# ===================================================================


class TestRequiredFieldsExist:
    """Core reporting fields must be present in the registry."""

    REQUIRED_FIELDS = frozenset(
        {
            "statement_source_id",
            "source_file",
            "attachment_path",
            "import_batch_id",
            "statement_row_id",
            "transaction_date",
            "posted_date",
            "amount",
            "currency",
            "description",
            "match_status",
            "review_status",
            "review_priority",
            "requires_human_review",
            "execution_status",
            "blocked_reason_codes",
            "evidence_refs",
            "raw_text",
            "raw_row_text",
            "created_at",
            "updated_at",
        }
    )

    def test_all_required_fields_present(self) -> None:
        names = set(rc.get_field_names())
        missing = self.REQUIRED_FIELDS - names
        assert not missing, f"Missing required fields: {missing}"

    def test_lookup_map_has_all_fields(self) -> None:
        for name in self.REQUIRED_FIELDS:
            assert name in rc.REPORTING_FIELD_MAP, f"{name} missing from REPORTING_FIELD_MAP"
            assert rc.get_reporting_field(name) is not None, (
                f"{name} missing from get_reporting_field"
            )


# ===================================================================
# 4. transaction_date and posted_date remain separate fields
# ===================================================================


class TestDateSeparation:
    """transaction_date and posted_date must be separate reporting fields."""

    def test_both_date_fields_exist(self) -> None:
        assert rc.get_reporting_field("transaction_date") is not None
        assert rc.get_reporting_field("posted_date") is not None

    def test_dates_are_separate_entries(self) -> None:
        txn = rc.get_reporting_field("transaction_date")
        posted = rc.get_reporting_field("posted_date")
        assert txn is not posted
        assert txn.name != posted.name

    def test_both_dates_are_dashboard_safe(self) -> None:
        txn = rc.get_reporting_field("transaction_date")
        posted = rc.get_reporting_field("posted_date")
        assert txn.dashboard_safe
        assert posted.dashboard_safe


# ===================================================================
# 5. Sensitive field classification
# ===================================================================


class TestSensitiveFieldClassification:
    """raw_text, raw_row_text, and other sensitive fields must not be
    classified as dashboard_safe."""

    SENSITIVE_FIELDS = frozenset(
        {
            "raw_text",
            "raw_row_text",
            "attachment_path",
            "source_file",
            "decision_note",
            "reviewer",
        }
    )

    AUDIT_ONLY_FIELDS = frozenset(
        {
            "raw_amount",
            "evidence_refs",
            "guard_decision_refs",
        }
    )

    def test_raw_text_is_sensitive(self) -> None:
        f = rc.get_reporting_field("raw_text")
        assert f is not None
        assert f.sensitive
        assert not f.dashboard_safe

    def test_raw_row_text_is_sensitive(self) -> None:
        f = rc.get_reporting_field("raw_row_text")
        assert f is not None
        assert f.sensitive
        assert not f.dashboard_safe

    def test_attachment_path_is_sensitive(self) -> None:
        f = rc.get_reporting_field("attachment_path")
        assert f is not None
        assert f.sensitive
        assert not f.dashboard_safe
        assert not f.audit_only

    def test_evidence_refs_are_audit_only(self) -> None:
        f = rc.get_reporting_field("evidence_refs")
        assert f is not None
        assert f.audit_only
        assert not f.dashboard_safe

    def test_get_sensitive_fields_matches_design(self) -> None:
        names = {f.name for f in rc.get_sensitive_fields()}
        assert names == self.SENSITIVE_FIELDS, (
            f"Expected sensitive: {self.SENSITIVE_FIELDS}, got: {names}"
        )

    def test_get_audit_only_fields_matches_design(self) -> None:
        names = {f.name for f in rc.get_audit_only_fields()}
        assert names == self.AUDIT_ONLY_FIELDS, (
            f"Expected audit_only: {self.AUDIT_ONLY_FIELDS}, got: {names}"
        )

    def test_dashboard_safe_fields_count(self) -> None:
        safe = rc.get_dashboard_safe_fields()
        assert len(safe) == 27

    def test_no_sensitive_field_is_dashboard_safe(self) -> None:
        for f in rc.REPORTING_FIELDS:
            if f.sensitive or f.audit_only:
                assert not f.dashboard_safe, (
                    f"{f.name} is {f.classification.value} -- must not be dashboard_safe"
                )


# ===================================================================
# 6. Attachment path sensitivity
# ===================================================================


class TestAttachmentPathSensitivity:
    """attachment_path must not be dashboard_safe."""

    def test_attachment_path_not_dashboard_safe(self) -> None:
        f = rc.get_reporting_field("attachment_path")
        assert f is not None
        assert not f.dashboard_safe

    def test_source_file_not_dashboard_safe(self) -> None:
        f = rc.get_reporting_field("source_file")
        assert f is not None
        assert not f.dashboard_safe


# ===================================================================
# 7. Core financial fields are reporting-visible
# ===================================================================


class TestCoreFinancialFields:
    """amount, transaction_date, posted_date, and currency must be
    dashboard_safe and present."""

    def test_amount_is_dashboard_safe(self) -> None:
        f = rc.get_reporting_field("amount")
        assert f is not None
        assert f.dashboard_safe

    def test_currency_is_dashboard_safe(self) -> None:
        f = rc.get_reporting_field("currency")
        assert f is not None
        assert f.dashboard_safe

    def test_transaction_date_is_dashboard_safe(self) -> None:
        f = rc.get_reporting_field("transaction_date")
        assert f is not None
        assert f.dashboard_safe

    def test_posted_date_is_dashboard_safe(self) -> None:
        f = rc.get_reporting_field("posted_date")
        assert f is not None
        assert f.dashboard_safe


# ===================================================================
# 8. Blocked reason codes
# ===================================================================


class TestBlockedReasonCodes:
    """All 14 blocked reason codes must be present and stable."""

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

    def test_all_fourteen_codes_present(self) -> None:
        actual = set(rc.get_blocked_reason_codes())
        assert actual == self.REQUIRED_CODES, (
            f"Missing: {self.REQUIRED_CODES - actual}, Extra: {actual - self.REQUIRED_CODES}"
        )

    def test_blocked_reason_count(self) -> None:
        assert len(rc.BLOCKED_REASONS) == 14

    def test_every_code_has_source_and_meaning(self) -> None:
        for r in rc.BLOCKED_REASONS:
            assert r.source, f"{r.code} has empty source"
            assert r.meaning, f"{r.code} has empty meaning"

    def test_every_code_is_snake_case(self) -> None:
        import re

        snake = re.compile(r"^[a-z][a-z0-9_]*$")
        for r in rc.BLOCKED_REASONS:
            assert snake.match(r.code), f"{r.code!r} is not snake_case"

    def test_get_blocked_reason_lookup(self) -> None:
        r = rc.get_blocked_reason("missing_currency")
        assert r is not None
        assert r.code == "missing_currency"
        assert "currency" in r.meaning.lower()

    def test_get_blocked_reason_missing(self) -> None:
        assert rc.get_blocked_reason("nonexistent") is None


# ===================================================================
# 9. Source groups coverage
# ===================================================================


class TestSourceGroupCoverage:
    """All 10 reporting source groups must be present."""

    REQUIRED_GROUPS = frozenset(
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

    def test_all_ten_groups_present(self) -> None:
        actual = set(rc.get_source_group_names())
        missing = self.REQUIRED_GROUPS - actual
        assert not missing, f"Missing source groups: {missing}"
        extra = actual - self.REQUIRED_GROUPS
        assert not extra, f"Unexpected source groups: {extra}"

    def test_source_group_count(self) -> None:
        assert len(rc.REPORTING_SOURCE_GROUPS) == 10

    def test_every_group_has_label_and_sources(self) -> None:
        for g in rc.REPORTING_SOURCE_GROUPS:
            assert g.label, f"{g.name} has empty label"
            assert len(g.sources) > 0, f"{g.name} has no sources"

    def test_import_health_includes_csv_and_pdf(self) -> None:
        g = rc.get_source_group("statement_import_health")
        assert g is not None
        source_labels = " ".join(g.sources).lower()
        assert "csv" in source_labels
        assert "pdf" in source_labels

    def test_get_source_group_lookup(self) -> None:
        g = rc.get_source_group("review_queue_status")
        assert g is not None
        assert g.label == "Review Queue Status"

    def test_get_source_group_missing(self) -> None:
        assert rc.get_source_group("nonexistent") is None


# ===================================================================
# 10. Dashboard sections coverage
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
        actual = set(rc.get_dashboard_section_names())
        missing = self.REQUIRED_SECTIONS - actual
        extra = actual - self.REQUIRED_SECTIONS
        assert not missing, f"Missing: {missing}"
        assert not extra, f"Extra: {extra}"

    def test_dashboard_section_count(self) -> None:
        assert len(rc.DASHBOARD_SECTIONS) == 7

    def test_every_section_has_label(self) -> None:
        for s in rc.DASHBOARD_SECTIONS:
            assert s.label, f"{s.name} has empty label"

    def test_get_dashboard_section_lookup(self) -> None:
        s = rc.get_dashboard_section("intake_health")
        assert s is not None
        assert s.label == "Intake Health"

    def test_get_dashboard_section_missing(self) -> None:
        assert rc.get_dashboard_section("nonexistent") is None


# ===================================================================
# 11. No live database reference
# ===================================================================


class TestNoLiveDatabaseReference:
    """The constants module must not reference database/finance.db."""

    def test_no_live_db_path_in_field_names(self) -> None:
        all_text = " ".join(rc.get_field_names())
        assert "database/finance.db" not in all_text
        assert "finance.db" not in all_text

    def test_no_live_db_path_in_source_groups(self) -> None:
        all_text = " ".join(rc.get_source_group_names())
        for g in rc.REPORTING_SOURCE_GROUPS:
            all_text += " " + " ".join(g.sources)
        assert "database/finance.db" not in all_text

    def test_no_live_db_path_in_blocked_reasons(self) -> None:
        all_text = " ".join(rc.get_blocked_reason_codes())
        for r in rc.BLOCKED_REASONS:
            all_text += " " + r.source + " " + r.meaning
        assert "database/finance.db" not in all_text

    def test_no_live_db_path_in_dashboard_sections(self) -> None:
        all_text = " ".join(rc.get_dashboard_section_names())
        for s in rc.DASHBOARD_SECTIONS:
            all_text += " " + s.label
        assert "database/finance.db" not in all_text

    def test_module_does_not_import_db(self) -> None:
        import inspect

        source = inspect.getsource(rc)
        assert "finance.db" not in source
        assert "sqlite3" not in source.lower()


# ===================================================================
# 12. Immutability
# ===================================================================


class TestImmutability:
    """Exported structures must be immutable or not accidentally mutable."""

    def test_reporting_fields_is_tuple_not_list(self) -> None:
        assert isinstance(rc.REPORTING_FIELDS, tuple)

    def test_source_groups_is_tuple_not_list(self) -> None:
        assert isinstance(rc.REPORTING_SOURCE_GROUPS, tuple)

    def test_dashboard_sections_is_tuple_not_list(self) -> None:
        assert isinstance(rc.DASHBOARD_SECTIONS, tuple)

    def test_blocked_reasons_is_tuple_not_list(self) -> None:
        assert isinstance(rc.BLOCKED_REASONS, tuple)

    def test_reporting_field_is_frozen(self) -> None:
        f = rc.REPORTING_FIELDS[0]
        with __import__("pytest").raises(Exception):
            f.name = "modified"  # type: ignore[misc]

    def test_source_group_is_frozen(self) -> None:
        g = rc.REPORTING_SOURCE_GROUPS[0]
        with __import__("pytest").raises(Exception):
            g.name = "modified"  # type: ignore[misc]

    def test_dashboard_section_is_frozen(self) -> None:
        s = rc.DASHBOARD_SECTIONS[0]
        with __import__("pytest").raises(Exception):
            s.name = "modified"  # type: ignore[misc]

    def test_blocked_reason_is_frozen(self) -> None:
        r = rc.BLOCKED_REASONS[0]
        with __import__("pytest").raises(Exception):
            r.code = "modified"  # type: ignore[misc]


# ===================================================================
# 13. get_reporting_field edge cases
# ===================================================================


class TestGetReportingFieldEdgeCases:
    """Lookup helpers should handle edge cases gracefully."""

    def test_lookup_known_field(self) -> None:
        f = rc.get_reporting_field("amount")
        assert f is not None
        assert f.name == "amount"

    def test_lookup_unknown_field(self) -> None:
        assert rc.get_reporting_field("nonexistent_field") is None

    def test_lookup_empty_string(self) -> None:
        assert rc.get_reporting_field("") is None

    def test_get_field_names_is_stable(self) -> None:
        names1 = rc.get_field_names()
        names2 = rc.get_field_names()
        assert names1 == names2
        assert isinstance(names1, tuple)


# ===================================================================
# 14. get_fields_by_classification helpers
# ===================================================================


class TestGetFieldsByClassification:
    """Classification filter helpers must return correct subsets."""

    def test_dashboard_safe_returns_27_fields(self) -> None:
        fields = rc.get_dashboard_safe_fields()
        assert len(fields) == 27
        for f in fields:
            assert f.classification == rc.FieldClassification.DASHBOARD_SAFE

    def test_audit_only_returns_3_fields(self) -> None:
        fields = rc.get_audit_only_fields()
        assert len(fields) == 3
        for f in fields:
            assert f.classification == rc.FieldClassification.AUDIT_ONLY

    def test_sensitive_returns_6_fields(self) -> None:
        fields = rc.get_sensitive_fields()
        assert len(fields) == 6
        for f in fields:
            assert f.classification == rc.FieldClassification.SENSITIVE

    def test_all_fields_classified(self) -> None:
        """Every field must belong to exactly one classification count."""
        total = (
            len(rc.get_dashboard_safe_fields())
            + len(rc.get_audit_only_fields())
            + len(rc.get_sensitive_fields())
        )
        assert total == len(rc.REPORTING_FIELDS)


# ===================================================================
# 15. ReportingField convenience properties
# ===================================================================


class TestReportingFieldProperties:
    """Convenience boolean properties on ReportingField must be correct."""

    def test_dashboard_safe_property(self) -> None:
        f = rc.get_reporting_field("amount")
        assert f is not None
        assert f.dashboard_safe is True
        assert f.audit_only is False
        assert f.sensitive is False
        assert f.masking_required is False

    def test_audit_only_property(self) -> None:
        f = rc.get_reporting_field("evidence_refs")
        assert f is not None
        assert f.dashboard_safe is False
        assert f.audit_only is True
        assert f.sensitive is False
        assert f.masking_required is True

    def test_sensitive_property(self) -> None:
        f = rc.get_reporting_field("raw_text")
        assert f is not None
        assert f.dashboard_safe is False
        assert f.audit_only is False
        assert f.sensitive is True
        assert f.masking_required is True


# ===================================================================
# 16. ReportingField field existence by classification
# ===================================================================


class TestFieldRegistryConsistency:
    """The field registry must be internally consistent."""

    def test_field_map_size_matches_tuple(self) -> None:
        assert len(rc.REPORTING_FIELD_MAP) == len(rc.REPORTING_FIELDS)

    def test_every_field_in_map(self) -> None:
        for f in rc.REPORTING_FIELDS:
            assert rc.REPORTING_FIELD_MAP[f.name] is f

    def test_no_extra_keys_in_map(self) -> None:
        field_names = set(rc.get_field_names())
        map_names = set(rc.REPORTING_FIELD_MAP.keys())
        assert field_names == map_names
