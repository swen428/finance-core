"""Tests for Reporting Export Fixture v1.

Verifies that the export fixture module is deterministic, read-only,
correctly separates dashboard-safe from sensitive/audit fields, and
produces JSON-serializable payloads.
"""

from __future__ import annotations

import json

import pytest

from finance_core.reconciliation.reporting_export_fixture import (
    SYNTHETIC_ROWS,
    FixtureRow,
    build_reporting_export_fixture,
    export_audit_payload,
    export_dashboard_safe_payload,
    validate_export_payload_fields,
    validate_export_payload_no_sensitive_fields,
)
from finance_core.reconciliation.reporting_query_helpers import (
    get_reporting_surface,
    get_reporting_surface_names,
)


class TestFixtureRow:
    """FixtureRow must produce correct dicts and preserve field values."""

    def test_fixture_row_to_dict(self) -> None:
        row = FixtureRow(
            statement_source_id="src-001",
            statement_row_id="row-001",
            transaction_date="2026-01-01",
            amount="100.00",
            currency="SGD",
            description="Test",
        )
        d = row.to_dict()
        assert d["statement_source_id"] == "src-001"
        assert d["statement_row_id"] == "row-001"
        assert d["transaction_date"] == "2026-01-01"

    def test_fixture_row_excludes_none(self) -> None:
        row = FixtureRow(
            statement_source_id="src-001",
            statement_row_id="row-001",
        )
        d = row.to_dict()
        assert d["statement_source_id"] == "src-001"
        assert "transaction_date" not in d

    def test_fixture_row_dict_no_sensitive(self) -> None:
        row = FixtureRow(
            statement_source_id="src-001",
            statement_row_id="row-001",
            raw_text="sensitive data",
            attachment_path="/tmp/sensitive.csv",
        )
        d = row.to_dict()
        assert "raw_text" not in d
        assert "attachment_path" not in d

    def test_fixture_row_is_frozen(self) -> None:
        row = SYNTHETIC_ROWS[0]
        with pytest.raises(Exception):
            row.amount = "999.00"  # type: ignore[misc]

    def test_synthetic_rows_are_three(self) -> None:
        assert len(SYNTHETIC_ROWS) == 3


class TestBuildReportingExportFixture:
    """Build helper must produce valid, deterministic fixtures."""

    def test_build_intake_health_fixture(self) -> None:
        fixture = build_reporting_export_fixture(SYNTHETIC_ROWS, surface_name="intake_health")
        assert fixture.fixture_name == "fixture-intake_health"
        assert fixture.surface_name == "intake_health"
        assert fixture.row_count == 3
        assert fixture.dashboard_safe is True

    def test_build_fixture_row_has_expected_fields(self) -> None:
        fixture = build_reporting_export_fixture(SYNTHETIC_ROWS, surface_name="intake_health")
        surface = get_reporting_surface("intake_health")
        assert surface is not None
        for row in fixture.rows:
            for key in row:
                assert key in surface.field_names, f"Unexpected field: {key}"

    def test_build_fixture_deterministic(self) -> None:
        a = build_reporting_export_fixture(SYNTHETIC_ROWS, "reconciliation_summary")
        b = build_reporting_export_fixture(SYNTHETIC_ROWS, "reconciliation_summary")
        assert a == b
        assert a.rows == b.rows

    def test_build_fixture_unknown_surface_raises(self) -> None:
        with pytest.raises(ValueError, match="nonexistent"):
            build_reporting_export_fixture(SYNTHETIC_ROWS, "nonexistent")

    def test_build_all_surfaces(self) -> None:
        for surface_name in get_reporting_surface_names():
            fixture = build_reporting_export_fixture(SYNTHETIC_ROWS, surface_name=surface_name)
            assert fixture.surface_name == surface_name
            assert fixture.dashboard_safe is True
            assert fixture.row_count == 3


class TestExportDashboardSafePayload:
    """Dashboard exports must exclude sensitive and audit-only fields."""

    def test_export_includes_only_dashboard_safe(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "raw_text" not in row
            assert "attachment_path" not in row
            assert "source_file" not in row
            assert "decision_note" not in row
            assert "reviewer" not in row
            assert "evidence_refs" not in row
            assert "raw_amount" not in row

    def test_export_includes_core_fields(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS, surface_name="intake_health")
        row = payload[0]
        assert "transaction_date" in row
        assert "posted_date" in row
        assert "amount" in row
        assert "currency" in row
        assert "description" in row

    def test_export_is_json_serializable(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        json_str = json.dumps(payload, default=str)
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert len(parsed) == 3

    def test_export_deterministic(self) -> None:
        a = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        b = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        assert a == b

    def test_export_unknown_surface_raises(self) -> None:
        with pytest.raises(ValueError, match="nonexistent"):
            export_dashboard_safe_payload(SYNTHETIC_ROWS, "nonexistent")


class TestExportAuditPayload:
    """Audit payload must contain sensitive fields but be explicitly named."""

    def test_audit_payload_includes_sensitive_fields(self) -> None:
        payload = export_audit_payload(SYNTHETIC_ROWS)
        row2 = payload[2]  # Third row has decision_note, attachment_path
        assert "statement_row_id" in row2
        assert row2.get("decision_note") is not None
        assert row2.get("attachment_path") is not None

    def test_audit_payload_includes_correlation_ids(self) -> None:
        payload = export_audit_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "statement_source_id" in row
            assert "statement_row_id" in row

    def test_audit_payload_is_separate_from_dashboard(self) -> None:
        dash = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        audit = export_audit_payload(SYNTHETIC_ROWS)
        # Dashboard payload must not contain any sensitive keys
        for row in dash:
            assert "raw_text" not in row
            assert "attachment_path" not in row
        # Audit payload must contain sensitive keys where present
        found_sensitive = False
        for row in audit:
            if row.get("raw_text") or row.get("attachment_path"):
                found_sensitive = True
        assert found_sensitive


class TestReportingExportFixture:
    """ReportingExportFixture dataclass properties."""

    def test_dashboard_safe_true(self) -> None:
        fixture = build_reporting_export_fixture(SYNTHETIC_ROWS)
        assert fixture.dashboard_safe is True

    def test_fixture_is_frozen(self) -> None:
        fixture = build_reporting_export_fixture(SYNTHETIC_ROWS)
        with pytest.raises(Exception):
            fixture.fixture_name = "modified"  # type: ignore[misc]


class TestValidateExportPayload:
    """Payload validators must catch sensitive field leaks."""

    def test_validate_dashboard_payload_passes(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        validate_export_payload_no_sensitive_fields(payload)

    def test_validate_payload_with_sensitive_fails(self) -> None:
        bad_payload = ({"amount": "10.00", "raw_text": "secret"},)
        with pytest.raises(ValueError, match="raw_text"):
            validate_export_payload_no_sensitive_fields(bad_payload)

    def test_validate_payload_with_audit_only_fails(self) -> None:
        bad_payload = ({"amount": "10.00", "evidence_refs": ["ev-001"]},)
        with pytest.raises(ValueError, match="evidence_refs"):
            validate_export_payload_no_sensitive_fields(bad_payload)

    def test_validate_payload_with_both_sensitive_and_audit_only_fails(self) -> None:
        bad_payload = ({"amount": "10.00", "raw_text": "secret", "evidence_refs": ["ev-001"]},)
        with pytest.raises(ValueError, match="raw_text"):
            validate_export_payload_no_sensitive_fields(bad_payload)

    def test_validate_export_payload_fields_passes(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        surface = get_reporting_surface("intake_health")
        assert surface is not None
        validate_export_payload_fields(payload, surface)

    def test_validate_export_payload_fields_extra_fails(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        bad = tuple({**row, "extra_field": "bad"} for row in payload)
        surface = get_reporting_surface("intake_health")
        assert surface is not None
        with pytest.raises(ValueError, match="extra_field"):
            validate_export_payload_fields(bad, surface)


class TestDateSeparation:
    """transaction_date and posted_date must remain separate in exports."""

    def test_both_dates_present_in_intake_export(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS, surface_name="intake_health")
        row = payload[0]
        assert "transaction_date" in row
        assert "posted_date" in row
        assert row["transaction_date"] == "2026-06-28"
        assert row["posted_date"] == "2026-06-29"

    def test_posted_date_can_be_none(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS, surface_name="intake_health")
        row3 = payload[2]
        # None values are excluded by the builder; when posted_date is None
        # it simply won't appear in the row. The key invariant is that
        # transaction_date is still present.
        assert "transaction_date" in row3


class TestBlockedAndReviewFields:
    """blocked_reason_codes, review_priority, requires_human_review must be
    preserved where present."""

    def test_blocked_reason_codes_present(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS, surface_name="needs_review")
        row2 = payload[1]
        assert row2.get("blocked_reason_codes") == ["ambiguous_amount_direction"]

    def test_review_priority_present(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS, surface_name="needs_review")
        priorities = {row.get("review_priority") for row in payload}
        assert "HIGH" in priorities
        assert "MEDIUM" in priorities

    def test_requires_human_review_present(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS, surface_name="needs_review")
        assert payload[1]["requires_human_review"] is True
        assert payload[0]["requires_human_review"] is False


class TestNoLiveDatabaseReference:
    """Module must not reference database/finance.db or sqlite3."""

    def test_no_live_db_path(self) -> None:
        import inspect

        from finance_core.reconciliation import reporting_export_fixture as ref

        source = inspect.getsource(ref)
        assert "database/finance.db" not in source
        assert "finance.db" not in source
        assert "sqlite3" not in source.lower()


class TestIntegrationExistingTests:
    """Existing tests must still pass."""

    def test_constants_module_still_ok(self) -> None:
        from finance_core.reconciliation.reporting_constants import (
            get_reporting_field,
        )

        f = get_reporting_field("amount")
        assert f is not None
        assert f.dashboard_safe

    def test_query_helpers_still_ok(self) -> None:
        from finance_core.reconciliation.reporting_query_helpers import (
            require_dashboard_safe_fields,
        )

        require_dashboard_safe_fields("amount", "currency")


class TestAuditPayloadIncludesAuditOnlyFields:
    """Audit payload must include both sensitive and audit-only fields."""

    def test_audit_payload_includes_evidence_refs(self) -> None:
        """evidence_refs must appear in audit payload when present."""
        payload = export_audit_payload(SYNTHETIC_ROWS)
        # row 0 has evidence_refs=("ev-dbs-001",)
        row0 = payload[0]
        assert "evidence_refs" in row0
        assert row0["evidence_refs"] == ["ev-dbs-001"]

    def test_audit_payload_includes_guard_decision_refs(self) -> None:
        """guard_decision_refs must appear in audit payload when present."""
        # Build a row with guard_decision_refs set
        row_with_guard = FixtureRow(
            statement_source_id="src-001",
            statement_row_id="row-gd-001",
            amount="50.00",
            currency="SGD",
            description="Test",
            guard_decision_refs=("gd-abc",),
        )
        payload = export_audit_payload((row_with_guard,))
        row = payload[0]
        assert "guard_decision_refs" in row
        assert row["guard_decision_refs"] == ["gd-abc"]

    def test_audit_payload_excludes_audit_only_when_empty(self) -> None:
        """Audit-only fields with empty default must not appear in audit payload."""
        row_empty = FixtureRow(
            statement_source_id="src-001",
            statement_row_id="row-e-001",
            amount="50.00",
            currency="SGD",
            description="Test",
        )
        payload = export_audit_payload((row_empty,))
        row = payload[0]
        # guard_decision_refs defaults to () and should be excluded
        assert "guard_decision_refs" not in row

    def test_audit_payload_sensitive_fields_still_included(self) -> None:
        """Sensitive fields are still included in audit payload."""
        payload = export_audit_payload(SYNTHETIC_ROWS)
        row2 = payload[2]
        assert "raw_text" in row2
        assert "attachment_path" in row2


class TestDashboardSafeStillExcludesAuditOnly:
    """Dashboard-safe payload must still exclude audit-only fields."""

    def test_dashboard_excludes_evidence_refs(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "evidence_refs" not in row

    def test_dashboard_excludes_guard_decision_refs(self) -> None:
        row_with_guard = FixtureRow(
            statement_source_id="src-001",
            statement_row_id="row-gd-001",
            amount="50.00",
            currency="SGD",
            description="Test",
            guard_decision_refs=("gd-abc",),
        )
        payload = export_dashboard_safe_payload((row_with_guard,))
        assert "guard_decision_refs" not in payload[0]

    def test_dashboard_excludes_raw_text(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "raw_text" not in row

    def test_dashboard_excludes_attachment_path(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "attachment_path" not in row

    def test_dashboard_excludes_decision_note(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "decision_note" not in row

    def test_dashboard_excludes_reviewer(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "reviewer" not in row

    def test_dashboard_excludes_source_file(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "source_file" not in row

    def test_dashboard_excludes_raw_row_text(self) -> None:
        payload = export_dashboard_safe_payload(SYNTHETIC_ROWS)
        for row in payload:
            assert "raw_row_text" not in row
