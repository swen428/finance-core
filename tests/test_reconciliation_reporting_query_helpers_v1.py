"""Tests for Reporting Query Helpers v1.

Verifies that the query helpers module is read-only, deterministic,
immutable, and correctly surfaces the reporting constants registry
for CLI, export, and Metabase-facing preparation.
"""

from __future__ import annotations

import pytest

from finance_core.reconciliation import reporting_constants as rc
from finance_core.reconciliation import reporting_query_helpers as qh


class TestModuleStructure:
    """The helpers module must export all expected symbols."""

    def test_re_exports_field_classification(self) -> None:
        assert qh.FieldClassification is rc.FieldClassification

    def test_re_exports_get_reporting_field(self) -> None:
        assert qh.get_reporting_field is rc.get_reporting_field

    def test_re_exports_get_field_names(self) -> None:
        assert qh.get_field_names is rc.get_field_names

    def test_re_exports_tuple_constants(self) -> None:
        assert qh.REPORTING_FIELDS is rc.REPORTING_FIELDS
        assert qh.BLOCKED_REASONS is rc.BLOCKED_REASONS
        assert qh.DASHBOARD_SECTIONS is rc.DASHBOARD_SECTIONS

    def test_convenience_proxies_exist(self) -> None:
        assert callable(qh.dashboard_safe_field_names)
        assert callable(qh.audit_only_field_names)
        assert callable(qh.sensitive_field_names)

    def test_field_name_tuples_exist(self) -> None:
        assert isinstance(qh.DASHBOARD_SAFE_FIELD_NAMES, tuple)
        assert isinstance(qh.AUDIT_ONLY_FIELD_NAMES, tuple)
        assert isinstance(qh.SENSITIVE_FIELD_NAMES, tuple)
        assert isinstance(qh.ALL_FIELD_NAMES, tuple)


class TestReadOnly:
    """Module must be read-only with immutable exports."""

    def test_dashboard_safe_field_names_is_tuple(self) -> None:
        assert isinstance(qh.DASHBOARD_SAFE_FIELD_NAMES, tuple)

    def test_audit_only_field_names_is_tuple(self) -> None:
        assert isinstance(qh.AUDIT_ONLY_FIELD_NAMES, tuple)

    def test_sensitive_field_names_is_tuple(self) -> None:
        assert isinstance(qh.SENSITIVE_FIELD_NAMES, tuple)

    def test_all_field_names_is_tuple(self) -> None:
        assert isinstance(qh.ALL_FIELD_NAMES, tuple)

    def test_source_group_to_section_is_dict(self) -> None:
        assert isinstance(qh.SOURCE_GROUP_TO_SECTION, dict)

    def test_reporting_surfaces_is_tuple(self) -> None:
        assert isinstance(qh.REPORTING_SURFACES, tuple)

    def test_reporting_surface_is_frozen(self) -> None:
        s = qh.REPORTING_SURFACES[0]
        with pytest.raises(Exception):
            s.name = "modified"  # type: ignore[misc]


class TestNoLiveDatabaseReference:
    """Module must not reference database/finance.db or sqlite3."""

    def test_no_live_db_path_in_helpers(self) -> None:
        import inspect

        source = inspect.getsource(qh)
        assert "database/finance.db" not in source
        assert "finance.db" not in source
        assert "sqlite3" not in source.lower()


class TestConvenienceProxies:
    """Convenience proxy functions must match reporting_constants results."""

    def test_dashboard_safe_proxy(self) -> None:
        assert qh.dashboard_safe_field_names() == rc.get_dashboard_safe_fields()

    def test_audit_only_proxy(self) -> None:
        assert qh.audit_only_field_names() == rc.get_audit_only_fields()

    def test_sensitive_proxy(self) -> None:
        assert qh.sensitive_field_names() == rc.get_sensitive_fields()

    def test_dashboard_safe_field_names_tuple(self) -> None:
        expected = tuple(f.name for f in rc.get_dashboard_safe_fields())
        assert qh.DASHBOARD_SAFE_FIELD_NAMES == expected

    def test_audit_only_field_names_tuple(self) -> None:
        expected = tuple(f.name for f in rc.get_audit_only_fields())
        assert qh.AUDIT_ONLY_FIELD_NAMES == expected

    def test_sensitive_field_names_tuple(self) -> None:
        expected = tuple(f.name for f in rc.get_sensitive_fields())
        assert qh.SENSITIVE_FIELD_NAMES == expected

    def test_all_field_names_tuple(self) -> None:
        assert qh.ALL_FIELD_NAMES == rc.get_field_names()


class TestDeterministicOrdering:
    """All exports must be deterministically ordered."""

    def test_dashboard_safe_field_names_stable(self) -> None:
        a = qh.DASHBOARD_SAFE_FIELD_NAMES
        b = tuple(f.name for f in rc.get_dashboard_safe_fields())
        assert a == b
        assert a == qh.DASHBOARD_SAFE_FIELD_NAMES

    def test_audit_only_field_names_stable(self) -> None:
        a = qh.AUDIT_ONLY_FIELD_NAMES
        b = tuple(f.name for f in rc.get_audit_only_fields())
        assert a == b

    def test_sensitive_field_names_stable(self) -> None:
        a = qh.SENSITIVE_FIELD_NAMES
        b = tuple(f.name for f in rc.get_sensitive_fields())
        assert a == b

    def test_reporting_surfaces_stable(self) -> None:
        a = qh.get_reporting_surface_names()
        b = qh.get_reporting_surface_names()
        assert a == b

    def test_source_group_to_section_stable_keys(self) -> None:
        keys = tuple(qh.SOURCE_GROUP_TO_SECTION.keys())
        assert keys == tuple(qh.SOURCE_GROUP_TO_SECTION.keys())


class TestFieldNameTuplesExcludeSensitive:
    """Dashboard-safe field name tuples must exclude sensitive/audit-only fields."""

    SENSITIVE_NAMES = frozenset(
        {
            "raw_text",
            "raw_row_text",
            "attachment_path",
            "source_file",
            "decision_note",
            "reviewer",
        }
    )
    AUDIT_ONLY_NAMES = frozenset(
        {
            "raw_amount",
            "evidence_refs",
            "guard_decision_refs",
        }
    )

    def test_dashboard_safe_excludes_sensitive(self) -> None:
        for name in self.SENSITIVE_NAMES:
            assert name not in qh.DASHBOARD_SAFE_FIELD_NAMES

    def test_dashboard_safe_excludes_audit_only(self) -> None:
        for name in self.AUDIT_ONLY_NAMES:
            assert name not in qh.DASHBOARD_SAFE_FIELD_NAMES

    def test_sensitive_in_sensitive_tuple(self) -> None:
        for name in self.SENSITIVE_NAMES:
            assert name in qh.SENSITIVE_FIELD_NAMES

    def test_audit_only_in_audit_tuple(self) -> None:
        for name in self.AUDIT_ONLY_NAMES:
            assert name in qh.AUDIT_ONLY_FIELD_NAMES


class TestRequireDashboardSafeFields:
    """Validation helper must reject non-dashboard-safe fields."""

    def test_all_dashboard_safe_passes(self) -> None:
        qh.require_dashboard_safe_fields("amount", "currency", "transaction_date")

    def test_sensitive_raises(self) -> None:
        with pytest.raises(ValueError, match="raw_text"):
            qh.require_dashboard_safe_fields("amount", "raw_text")

    def test_audit_only_raises(self) -> None:
        with pytest.raises(ValueError, match="evidence_refs"):
            qh.require_dashboard_safe_fields("amount", "evidence_refs")

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValueError, match="nonexistent"):
            qh.require_dashboard_safe_fields("nonexistent")

    def test_empty_pass(self) -> None:
        qh.require_dashboard_safe_fields()


class TestRequireNoSensitiveFields:
    """Validation helper must reject sensitive/audit-only fields."""

    def test_dashboard_safe_passes(self) -> None:
        qh.require_no_sensitive_fields("amount", "currency")

    def test_sensitive_raises(self) -> None:
        with pytest.raises(ValueError, match="raw_text"):
            qh.require_no_sensitive_fields("amount", "raw_text")

    def test_audit_only_raises(self) -> None:
        with pytest.raises(ValueError, match="evidence_refs"):
            qh.require_no_sensitive_fields("amount", "evidence_refs")

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="nonexistent"):
            qh.require_no_sensitive_fields("nonexistent")


class TestRequireSpecificFields:
    """Field tuple validator must be strict about identity and ordering."""

    def test_exact_match_passes(self) -> None:
        qh.require_specific_fields(("amount", "currency"), ("amount", "currency"))

    def test_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="Field selection mismatch"):
            qh.require_specific_fields(("amount", "currency"), ("amount",))

    def test_order_matters(self) -> None:
        with pytest.raises(ValueError):
            qh.require_specific_fields(("amount", "currency"), ("currency", "amount"))


class TestSourceGroupToSectionMapping:
    """Every source group must map to an existing dashboard section."""

    def test_all_ten_source_groups_mapped(self) -> None:
        groups = rc.get_source_group_names()
        assert len(groups) == 10
        for g in groups:
            assert g in qh.SOURCE_GROUP_TO_SECTION, f"{g} not mapped"

    def test_all_target_sections_exist(self) -> None:
        for section_name in qh.SOURCE_GROUP_TO_SECTION.values():
            assert rc.get_dashboard_section(section_name) is not None

    def test_get_dashboard_section_for_source_group(self) -> None:
        assert qh.get_dashboard_section_for_source_group("review_queue_status") == "needs_review"
        assert (
            qh.get_dashboard_section_for_source_group("statement_import_health") == "intake_health"
        )

    def test_unknown_source_group_returns_none(self) -> None:
        assert qh.get_dashboard_section_for_source_group("nonexistent") is None


class TestReportingSurfaces:
    """Reporting surfaces must be valid and cover all dashboard sections."""

    def test_all_seven_surfaces_exist(self) -> None:
        assert len(qh.REPORTING_SURFACES) == 7

    def test_surface_names_match_sections(self) -> None:
        surface_names = set(qh.get_reporting_surface_names())
        section_names = set(rc.get_dashboard_section_names())
        assert surface_names == section_names

    def test_every_surface_is_valid(self) -> None:
        for s in qh.REPORTING_SURFACES:
            assert s.valid, f"Surface {s.name} is not valid"

    def test_every_surface_has_fields(self) -> None:
        for s in qh.REPORTING_SURFACES:
            assert len(s.field_names) > 0, f"Surface {s.name} has no fields"

    def test_surface_field_names_are_dashboard_safe(self) -> None:
        sensitive = qh.SENSITIVE_FIELD_NAMES
        audit = qh.AUDIT_ONLY_FIELD_NAMES
        for s in qh.REPORTING_SURFACES:
            for name in s.field_names:
                assert name not in sensitive
                assert name not in audit

    def test_get_reporting_surface(self) -> None:
        s = qh.get_reporting_surface("intake_health")
        assert s is not None
        assert s.label == "Intake Health"
        assert s.dashboard_section == "intake_health"
        assert "transaction_date" in s.field_names
        assert "posted_date" in s.field_names

    def test_get_reporting_surface_missing(self) -> None:
        assert qh.get_reporting_surface("nonexistent") is None

    def test_get_field_names_for_surface(self) -> None:
        names = qh.get_field_names_for_surface("needs_review")
        assert isinstance(names, tuple)
        assert "review_status" in names
        assert "review_priority" in names
        assert "requires_human_review" in names

    def test_get_field_names_for_surface_unknown(self) -> None:
        with pytest.raises(ValueError, match="nonexistent"):
            qh.get_field_names_for_surface("nonexistent")

    def test_get_reporting_surface_names(self) -> None:
        names = qh.get_reporting_surface_names()
        assert isinstance(names, tuple)
        assert len(names) == 7
        assert "intake_health" in names


class TestReportingFieldsForSourceGroup:
    """Source group -> field lookup must return correct fields."""

    def test_valid_source_group_returns_fields(self) -> None:
        fields = qh.reporting_fields_for_source_group("reconciliation_match_outcomes")
        assert fields is not None
        assert len(fields) > 0
        for f in fields:
            assert f.dashboard_safe

    def test_unknown_source_group_returns_none(self) -> None:
        assert qh.reporting_fields_for_source_group("nonexistent") is None

    def test_fields_are_reporting_field_objects(self) -> None:
        fields = qh.reporting_fields_for_source_group("review_queue_status")
        assert fields is not None
        for f in fields:
            assert isinstance(f, rc.ReportingField)


class TestFilterFieldNames:
    """Field filter helper must correctly include/exclude fields."""

    def test_no_filters_returns_all(self) -> None:
        names = ("amount", "currency", "transaction_date")
        assert qh.filter_field_names(names) == names

    def test_exclude_sensitive(self) -> None:
        names = ("amount", "raw_text", "currency", "attachment_path")
        result = qh.filter_field_names(
            names,
            exclude_classifications=(rc.FieldClassification.SENSITIVE,),
        )
        assert "raw_text" not in result
        assert "attachment_path" not in result
        assert "amount" in result
        assert "currency" in result

    def test_exclude_audit_only(self) -> None:
        names = ("amount", "evidence_refs", "currency")
        result = qh.filter_field_names(
            names,
            exclude_classifications=(rc.FieldClassification.AUDIT_ONLY,),
        )
        assert "evidence_refs" not in result
        assert "amount" in result

    def test_predicate_filter(self) -> None:
        names = ("amount", "currency", "raw_text")
        result = qh.filter_field_names(
            names,
            predicate=lambda f: f.dashboard_safe,
        )
        assert "raw_text" not in result
        assert "amount" in result
        assert "currency" in result

    def test_unknown_fields_skipped(self) -> None:
        names = ("amount", "nonexistent", "currency")
        result = qh.filter_field_names(names)
        assert "amount" in result
        assert "currency" in result
        assert "nonexistent" not in result

    def test_empty_input(self) -> None:
        assert qh.filter_field_names(()) == ()


class TestDateSeparationPreserved:
    """transaction_date and posted_date must remain separate in surfaces."""

    def test_intake_health_has_both_dates(self) -> None:
        s = qh.get_reporting_surface("intake_health")
        assert s is not None
        assert "transaction_date" in s.field_names
        assert "posted_date" in s.field_names

    def test_reconciliation_summary_has_both_dates(self) -> None:
        s = qh.get_reporting_surface("reconciliation_summary")
        assert s is not None
        assert "transaction_date" in s.field_names
        assert "posted_date" in s.field_names

    def test_no_surface_has_only_posted_date(self) -> None:
        for s in qh.REPORTING_SURFACES:
            if "posted_date" in s.field_names:
                assert "transaction_date" in s.field_names


class TestIntegrationWithConstants:
    """Query helpers must be consistent with reporting_constants."""

    def test_all_helper_fields_in_constants(self) -> None:
        for s in qh.REPORTING_SURFACES:
            for name in s.field_names:
                assert qh.get_reporting_field(name) is not None

    def test_all_constants_helpers_re_exported(self) -> None:
        assert qh.get_reporting_field is rc.get_reporting_field
        assert qh.get_field_names is rc.get_field_names
        assert qh.get_fields_by_classification is rc.get_fields_by_classification

    def test_source_group_to_section_consistent(self) -> None:
        for src_group_name, section_name in qh.SOURCE_GROUP_TO_SECTION.items():
            assert rc.get_source_group(src_group_name) is not None
            assert rc.get_dashboard_section(section_name) is not None


class TestNoMutationFunctions:
    """Module must not expose any mutation or write functions."""

    def test_no_set_del_update_methods(self) -> None:
        import inspect

        for name, obj in inspect.getmembers(qh):
            if not name.startswith("_") and callable(obj) and not inspect.isclass(obj):
                try:
                    src = inspect.getsource(obj).lower()
                except (TypeError, OSError):
                    continue
                assert "insert" not in src, f"{name} contains INSERT"
                assert "update" not in src, f"{name} contains UPDATE"
                assert "delete" not in src, f"{name} contains DELETE"
                assert "execute" not in src, f"{name} contains EXECUTE"
                assert "commit" not in src, f"{name} contains COMMIT"
                assert ".connect(" not in src, f"{name} contains .connect("
