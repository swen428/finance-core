"""Reporting Export Fixture v1.

Deterministic, read-only test fixture/export helper that uses the reporting
constants and query helpers to produce a stable reporting payload suitable
for future CLI or Metabase pipeline tests.

Non-goals:

- No database connection.
- No SQL execution.
- No Metabase dependency.
- No mutation functions.
- No file creation (tests may use ``tmp_path`` externally).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finance_core.reconciliation.reporting_query_helpers import (
    AUDIT_ONLY_FIELD_NAMES,
    DASHBOARD_SAFE_FIELD_NAMES,
    SENSITIVE_FIELD_NAMES,
    ReportingSurface,
    get_reporting_surface,
)

# Stable tuple combining sensitive + audit-only field names (no duplicates)
AUDIT_PAYLOAD_FIELD_NAMES: tuple[str, ...] = SENSITIVE_FIELD_NAMES + tuple(
    name for name in AUDIT_ONLY_FIELD_NAMES if name not in frozenset(SENSITIVE_FIELD_NAMES)
)

# ---------------------------------------------------------------------------
# Synthetic row fixture dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureRow:
    """A synthetic reporting row for export fixture tests.

    Contains only dashboard-safe fields plus explicitly separated
    audit/sensitive fields for testing separation boundaries.
    """

    statement_source_id: str
    statement_row_id: str
    transaction_date: str | None = None
    posted_date: str | None = None
    amount: str | None = None
    currency: str | None = None
    amount_direction: str | None = None
    description: str | None = None
    match_status: str | None = None
    match_rate: float | None = None
    review_status: str | None = None
    review_priority: str | None = None
    requires_human_review: bool = False
    execution_status: str | None = None
    blocked_reason_codes: tuple[str, ...] = ()
    decision_action: str | None = None
    source_type: str | None = None
    source_page_number: int | None = None
    source_row_ref: str | None = None
    import_batch_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    amount_delta: str | None = None
    date_delta_days: int | None = None
    merchant_similarity: float | None = None
    reporting_bucket: str | None = None
    reporting_reason: str | None = None
    # Audit-only / sensitive fields
    evidence_refs: tuple[str, ...] = ()
    guard_decision_refs: tuple[str, ...] = ()
    raw_amount: str | None = None
    raw_text: str | None = None
    raw_row_text: str | None = None
    attachment_path: str | None = None
    source_file: str | None = None
    decision_note: str | None = None
    reviewer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a flat dict of all non-None fields."""
        result: dict[str, Any] = {}
        for field_name in DASHBOARD_SAFE_FIELD_NAMES:
            val = getattr(self, field_name, None)
            if val is not None:
                result[field_name] = val
        return result


# ---------------------------------------------------------------------------
# Synthetic example rows (deterministic)
# ---------------------------------------------------------------------------


SYNTHETIC_ROWS: tuple[FixtureRow, ...] = (
    FixtureRow(
        statement_source_id="src-dbs-card-001",
        statement_row_id="row-001",
        import_batch_id="batch-2026-07-03-a",
        transaction_date="2026-06-28",
        posted_date="2026-06-29",
        amount="42.50",
        currency="SGD",
        amount_direction="debit",
        description="GrabFood AMK HUB",
        match_status="matched",
        match_rate=0.95,
        review_status="resolved",
        review_priority="LOW",
        requires_human_review=False,
        execution_status="executed",
        source_type="CSV",
        amount_delta="0.00",
        date_delta_days=0,
        merchant_similarity=0.95,
        reporting_bucket="reconciled",
        reporting_reason="exact match",
        decision_action="confirm_match",
        evidence_refs=("ev-dbs-001",),
    ),
    FixtureRow(
        statement_source_id="src-ocbc-card-002",
        statement_row_id="row-002",
        import_batch_id="batch-2026-07-03-a",
        transaction_date="2026-06-25",
        posted_date="2026-06-27",
        amount="189.00",
        currency="SGD",
        amount_direction="debit",
        description="NTUC FairPrice",
        match_status="amount_mismatch",
        match_rate=0.70,
        review_status="needs_review",
        review_priority="MEDIUM",
        requires_human_review=True,
        execution_status="blocked",
        blocked_reason_codes=("ambiguous_amount_direction",),
        source_type="CSV",
        amount_delta="12.00",
        date_delta_days=2,
        merchant_similarity=0.70,
        reporting_bucket="amount_mismatch",
        reporting_reason="amount differs by 12.00",
        raw_text="NTUC FP AMK 25JUN S189.00",
        raw_amount="189.00",
        evidence_refs=(),
    ),
    FixtureRow(
        statement_source_id="src-dbs-card-001",
        statement_row_id="row-003",
        import_batch_id="batch-2026-07-03-b",
        transaction_date="2026-06-20",
        posted_date=None,
        amount="15.00",
        currency="SGD",
        amount_direction="debit",
        description="Starbucks Coffee",
        match_status="no_match",
        match_rate=0.0,
        review_status="needs_review",
        review_priority="HIGH",
        requires_human_review=True,
        execution_status="pending",
        source_type="CSV",
        merchant_similarity=0.0,
        reporting_bucket="unmatched_statement",
        reporting_reason="no candidate found",
        raw_text="STARBUCKS 20JUN S$15.00",
        attachment_path="/tmp/statements/dbs_jun_2026.csv",
        decision_note="This looks like a new merchant, needs review",
        reviewer="owner",
    ),
)


# ---------------------------------------------------------------------------
# Export fixture dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportingExportFixture:
    """A deterministic, read-only reporting export payload.

    Separates dashboard-safe fields from audit/sensitive fields.
    All fields are derived from the supplied ``FixtureRow`` objects.
    """

    fixture_name: str
    surface_name: str
    surface: ReportingSurface
    rows: tuple[dict[str, Any], ...]
    row_count: int

    @property
    def dashboard_safe(self) -> bool:
        """True when every row only contains dashboard-safe field names."""
        for row in self.rows:
            for field_name in row:
                if field_name in SENSITIVE_FIELD_NAMES or field_name in AUDIT_ONLY_FIELD_NAMES:
                    return False
        return True


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_reporting_export_fixture(
    rows: tuple[FixtureRow, ...],
    surface_name: str = "intake_health",
) -> ReportingExportFixture:
    """Build a deterministic reporting export fixture from synthetic rows.

    The fixture filters each row down to the field names defined by the
    specified reporting surface. Dashboard-safe fields are included;
    audit/sensitive fields are excluded.

    Raises ``ValueError`` when ``surface_name`` is unknown.
    """
    surface = get_reporting_surface(surface_name)
    if surface is None:
        raise ValueError(f"Unknown reporting surface: {surface_name!r}")

    projected_rows: list[dict[str, Any]] = []
    for row in rows:
        projected: dict[str, Any] = {}
        for field_name in surface.field_names:
            val = getattr(row, field_name, None)
            if val is not None:
                if isinstance(val, tuple):
                    val = list(val)
                projected[field_name] = val
        projected_rows.append(projected)

    return ReportingExportFixture(
        fixture_name=f"fixture-{surface_name}",
        surface_name=surface_name,
        surface=surface,
        rows=tuple(projected_rows),
        row_count=len(projected_rows),
    )


def export_dashboard_safe_payload(
    rows: tuple[FixtureRow, ...],
    surface_name: str = "intake_health",
) -> tuple[dict[str, Any], ...]:
    """Export a dashboard-safe reporting payload as a tuple of dicts.

    Each dict contains only the dashboard-safe field names defined by the
    reporting surface. Sensitive and audit-only fields are excluded.

    Raises ``ValueError`` when ``surface_name`` is unknown.
    """
    fixture = build_reporting_export_fixture(rows, surface_name)
    return fixture.rows


def export_audit_payload(
    rows: tuple[FixtureRow, ...],
) -> tuple[dict[str, Any], ...]:
    """Export an audit-only payload containing both sensitive and audit-only fields.

    This is deliberately separate from ``export_dashboard_safe_payload`` to
    prevent accidental leakage of sensitive data into dashboard exports.

    Includes both SENSITIVE_FIELD_NAMES and AUDIT_ONLY_FIELD_NAMES so that
    audit-only fields such as evidence_refs and guard_decision_refs appear
    when present. Row identity fields (statement_source_id, statement_row_id)
    are preserved for correlation.
    """
    result: list[dict[str, Any]] = []
    for row in rows:
        audit_row: dict[str, Any] = {}
        seen: set[str] = set()
        for field_name in AUDIT_PAYLOAD_FIELD_NAMES:
            val = getattr(row, field_name, None)
            if val is None:
                continue
            if isinstance(val, tuple) and not val:
                continue
            if field_name in seen:
                continue
            if isinstance(val, tuple):
                val = list(val)
            audit_row[field_name] = val
            seen.add(field_name)
        # Also include row identity for correlation
        audit_row["statement_source_id"] = getattr(row, "statement_source_id", None)
        audit_row["statement_row_id"] = getattr(row, "statement_row_id", None)
        result.append(audit_row)
    return tuple(result)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_export_payload_no_sensitive_fields(
    payload: tuple[dict[str, Any], ...],
) -> None:
    """Raise ``ValueError`` if any row in the payload contains a sensitive
    or audit-only field name."""
    for i, row in enumerate(payload):
        for field_name in row:
            if field_name in SENSITIVE_FIELD_NAMES or field_name in AUDIT_ONLY_FIELD_NAMES:
                raise ValueError(
                    f"Row {i}: sensitive or audit-only field {field_name!r} "
                    "found in dashboard-safe payload"
                )


def validate_export_payload_fields(
    payload: tuple[dict[str, Any], ...],
    surface: ReportingSurface,
) -> None:
    """Raise ``ValueError`` if any row contains a field not in the surface's
    field_names tuple."""
    allowed = set(surface.field_names)
    for i, row in enumerate(payload):
        extra = set(row.keys()) - allowed
        if extra:
            raise ValueError(
                f"Row {i}: unexpected fields in {surface.name} payload: {sorted(extra)}"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "AUDIT_PAYLOAD_FIELD_NAMES",
    "FixtureRow",
    "SYNTHETIC_ROWS",
    "ReportingExportFixture",
    "build_reporting_export_fixture",
    "export_dashboard_safe_payload",
    "export_audit_payload",
    "validate_export_payload_no_sensitive_fields",
    "validate_export_payload_fields",
]
