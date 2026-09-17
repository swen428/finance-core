"""Reporting Query Helpers v1.

Read-only helper layer that uses the reporting constants registry to produce
stable reporting query/export metadata for CLI, export, and Metabase-facing
preparation. All helpers are deterministic, immutable, and side-effect-free.

Non-goals:

- No database connection.
- No SQL execution.
- No Metabase dependency.
- No mutation functions.
- No timestamps, UUIDs, random ordering, or environment-dependent behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from finance_core.reconciliation.reporting_constants import (
    BLOCKED_REASONS,
    DASHBOARD_SECTIONS,
    REPORTING_FIELDS,
    BlockedReason,
    DashboardSection,
    FieldClassification,
    ReportingField,
    ReportingSourceGroup,
    get_audit_only_fields,
    get_blocked_reason,
    get_blocked_reason_codes,
    get_dashboard_safe_fields,
    get_dashboard_section,
    get_dashboard_section_names,
    get_field_names,
    get_fields_by_classification,
    get_reporting_field,
    get_sensitive_fields,
    get_source_group,
    get_source_group_names,
)

# ---------------------------------------------------------------------------
# Read-only re-exports from reporting_constants (convenience)
# ---------------------------------------------------------------------------

# These are thin stable proxies that make query-helpers the one-stop import
# for reporting preparation code. They do not redefine anything; they simply
# forward to the constants module.

dashboard_safe_field_names = get_dashboard_safe_fields
"""-> tuple[ReportingField, ...] -- all DASHBOARD_SAFE reporting fields."""

audit_only_field_names = get_audit_only_fields
"""-> tuple[ReportingField, ...] -- all AUDIT_ONLY reporting fields."""

sensitive_field_names = get_sensitive_fields
"""-> tuple[ReportingField, ...] -- all SENSITIVE reporting fields."""


# ---------------------------------------------------------------------------
# Field name tuples (convenience)
# ---------------------------------------------------------------------------


def _str_field_names(fields: tuple[ReportingField, ...]) -> tuple[str, ...]:
    return tuple(f.name for f in fields)


DASHBOARD_SAFE_FIELD_NAMES: tuple[str, ...] = _str_field_names(get_dashboard_safe_fields())
"""Stable tuple of dashboard-safe field name strings."""

AUDIT_ONLY_FIELD_NAMES: tuple[str, ...] = _str_field_names(get_audit_only_fields())
"""Stable tuple of audit-only field name strings."""

SENSITIVE_FIELD_NAMES: tuple[str, ...] = _str_field_names(get_sensitive_fields())
"""Stable tuple of sensitive field name strings."""

ALL_FIELD_NAMES: tuple[str, ...] = get_field_names()
"""Stable tuple of all reporting field name strings."""


# ---------------------------------------------------------------------------
# Dashboard-safe field validation
# ---------------------------------------------------------------------------


def require_dashboard_safe_fields(*field_names: str) -> None:
    """Raise ``ValueError`` if any field name is not dashboard-safe.

    Accepts field name *strings*, not ``ReportingField`` objects.
    Each name is looked up in the registry. Fields that exist but are
    classified as AUDIT_ONLY or SENSITIVE, or fields that are not in
    the registry at all, cause a ``ValueError``.
    """
    for name in field_names:
        f = get_reporting_field(name)
        if f is None:
            raise ValueError(f"Unknown reporting field: {name!r}")
        if not f.dashboard_safe:
            raise ValueError(
                f"Field {name!r} is classified as {f.classification.value}, "
                f"not {FieldClassification.DASHBOARD_SAFE.value}"
            )


def require_specific_fields(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> None:
    """Raise ``ValueError`` if ``actual`` does not match ``expected`` exactly.

    Both tuples must contain the same field names in the same order.
    Useful for contract tests and export shape validation.
    """
    if expected != actual:
        raise ValueError(f"Field selection mismatch: expected {expected!r}, got {actual!r}")


def require_no_sensitive_fields(*field_names: str) -> None:
    """Raise ``ValueError`` if any field name is classified as SENSITIVE
    or AUDIT_ONLY."""
    for name in field_names:
        f = get_reporting_field(name)
        if f is None:
            raise ValueError(f"Unknown reporting field: {name!r}")
        if f.classification in (FieldClassification.SENSITIVE, FieldClassification.AUDIT_ONLY):
            raise ValueError(f"Field {name!r} is classified as {f.classification.value}")


# ---------------------------------------------------------------------------
# Source-group -> dashboard-section mapping
# ---------------------------------------------------------------------------


SOURCE_GROUP_TO_SECTION: dict[str, str] = {
    "statement_import_health": "intake_health",
    "reconciliation_match_outcomes": "reconciliation_summary",
    "review_queue_status": "needs_review",
    "review_resolution_outcomes": "needs_review",
    "guarded_apply_plan_status": "apply_execution_audit",
    "guarded_apply_execution_status": "apply_execution_audit",
    "human_review_required_items": "needs_review",
    "blocked_apply_operations": "blocked_items",
    "evidence_completeness": "evidence_audit_completeness",
    "pdf_import_readiness": "pdf_import_readiness",
}
"""Frozen mapping from reporting source group name to dashboard section name.

Derived from the Metabase / Reporting Preparation v1 design contract
(Sections 3 and 4). Each source group maps to exactly one dashboard
section. This mapping is stable and must not change in a way that
breaks existing dashboards.
"""


def get_dashboard_section_for_source_group(source_group_name: str) -> str | None:
    """Return the dashboard section name for a reporting source group.

    Returns ``None`` when the source group name is not in the mapping.
    """
    return SOURCE_GROUP_TO_SECTION.get(source_group_name)


# ---------------------------------------------------------------------------
# Reporting surface field selections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportingSurface:
    """A named set of stable field selections for a specific reporting surface.

    Each surface exposes a fixed list of dashboard-safe field names suitable
    for query construction, export projection, or Metabase view definition.
    """

    name: str
    label: str
    dashboard_section: str
    field_names: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """True when every field in this surface is dashboard-safe."""
        try:
            require_dashboard_safe_fields(*self.field_names)
            return True
        except ValueError:
            return False


def _build_reporting_surfaces() -> tuple[ReportingSurface, ...]:
    """Return stable tuple of reporting surfaces derived from the registry."""

    def _safe(name: str) -> str:
        f = get_reporting_field(name)
        if f is None or not f.dashboard_safe:
            raise ValueError(f"Field {name!r} is not dashboard-safe")
        return name

    return (
        ReportingSurface(
            name="intake_health",
            label="Intake Health",
            dashboard_section="intake_health",
            field_names=(
                _safe("statement_source_id"),
                _safe("import_batch_id"),
                _safe("statement_row_id"),
                _safe("source_type"),
                _safe("source_page_number"),
                _safe("source_row_ref"),
                _safe("transaction_date"),
                _safe("posted_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("amount_direction"),
                _safe("description"),
                _safe("created_at"),
                _safe("updated_at"),
            ),
        ),
        ReportingSurface(
            name="reconciliation_summary",
            label="Reconciliation Summary",
            dashboard_section="reconciliation_summary",
            field_names=(
                _safe("statement_source_id"),
                _safe("statement_row_id"),
                _safe("transaction_date"),
                _safe("posted_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("amount_direction"),
                _safe("description"),
                _safe("match_status"),
                _safe("match_rate"),
                _safe("amount_delta"),
                _safe("date_delta_days"),
                _safe("merchant_similarity"),
            ),
        ),
        ReportingSurface(
            name="needs_review",
            label="Needs Review",
            dashboard_section="needs_review",
            field_names=(
                _safe("statement_source_id"),
                _safe("statement_row_id"),
                _safe("transaction_date"),
                _safe("posted_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("amount_direction"),
                _safe("description"),
                _safe("match_status"),
                _safe("review_status"),
                _safe("review_priority"),
                _safe("requires_human_review"),
                _safe("decision_action"),
                _safe("blocked_reason_codes"),
                _safe("created_at"),
                _safe("updated_at"),
            ),
        ),
        ReportingSurface(
            name="apply_execution_audit",
            label="Apply Execution Audit",
            dashboard_section="apply_execution_audit",
            field_names=(
                _safe("statement_source_id"),
                _safe("statement_row_id"),
                _safe("transaction_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("description"),
                _safe("execution_status"),
                _safe("blocked_reason_codes"),
                _safe("requires_human_review"),
                _safe("created_at"),
                _safe("updated_at"),
            ),
        ),
        ReportingSurface(
            name="blocked_items",
            label="Blocked Items",
            dashboard_section="blocked_items",
            field_names=(
                _safe("statement_source_id"),
                _safe("statement_row_id"),
                _safe("transaction_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("description"),
                _safe("execution_status"),
                _safe("blocked_reason_codes"),
                _safe("requires_human_review"),
                _safe("created_at"),
                _safe("updated_at"),
            ),
        ),
        ReportingSurface(
            name="evidence_audit_completeness",
            label="Evidence / Audit Completeness",
            dashboard_section="evidence_audit_completeness",
            field_names=(
                _safe("statement_source_id"),
                _safe("statement_row_id"),
                _safe("transaction_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("description"),
                _safe("reporting_bucket"),
                _safe("reporting_reason"),
                _safe("blocked_reason_codes"),
                _safe("created_at"),
                _safe("updated_at"),
            ),
        ),
        ReportingSurface(
            name="pdf_import_readiness",
            label="PDF Import Readiness",
            dashboard_section="pdf_import_readiness",
            field_names=(
                _safe("statement_source_id"),
                _safe("import_batch_id"),
                _safe("source_type"),
                _safe("source_page_number"),
                _safe("source_row_ref"),
                _safe("transaction_date"),
                _safe("posted_date"),
                _safe("amount"),
                _safe("currency"),
                _safe("amount_direction"),
                _safe("description"),
                _safe("blocked_reason_codes"),
                _safe("created_at"),
            ),
        ),
    )


REPORTING_SURFACES: tuple[ReportingSurface, ...] = _build_reporting_surfaces()
"""Stable tuple of all reporting surfaces, one per dashboard section.

Each surface exposes a fixed set of dashboard-safe field names. The
surface validators guarantee that no sensitive or audit-only field is
ever included in a surface definition.
"""


def get_reporting_surface(name: str) -> ReportingSurface | None:
    """Look up a reporting surface by name."""
    for s in REPORTING_SURFACES:
        if s.name == name:
            return s
    return None


def get_reporting_surface_names() -> tuple[str, ...]:
    """Return stable tuple of all reporting surface names."""
    return tuple(s.name for s in REPORTING_SURFACES)


def get_field_names_for_surface(surface_name: str) -> tuple[str, ...]:
    """Return the field name tuple for a reporting surface.

    Raises ``ValueError`` when the surface name is unknown.
    """
    s = get_reporting_surface(surface_name)
    if s is None:
        raise ValueError(f"Unknown reporting surface: {surface_name!r}")
    return s.field_names


# ---------------------------------------------------------------------------
# Source-group field selection helpers
# ---------------------------------------------------------------------------


def reporting_fields_for_source_group(
    source_group_name: str,
) -> tuple[ReportingField, ...] | None:
    """Return dashboard-safe reporting fields relevant to a source group.

    This is a convenience lookup that returns the fields for the dashboard
    section mapped from the source group. Returns ``None`` when the source
    group is not in the mapping or the corresponding surface is not found.
    """
    section = SOURCE_GROUP_TO_SECTION.get(source_group_name)
    if section is None:
        return None
    surface = get_reporting_surface(section)
    if surface is None:
        return None
    return tuple(
        get_reporting_field(name)  # type: ignore[misc]
        for name in surface.field_names
        if get_reporting_field(name) is not None
    )


# ---------------------------------------------------------------------------
# Reporting field filter helpers
# ---------------------------------------------------------------------------


def filter_field_names(
    field_names: tuple[str, ...],
    *,
    exclude_classifications: tuple[FieldClassification, ...] = (),
    predicate: Callable[[ReportingField], bool] | None = None,
) -> tuple[str, ...]:
    """Return a subset of ``field_names`` after applying filters.

    Filters are applied in order:

    1. Exclude fields whose classification is in ``exclude_classifications``.
    2. Apply optional ``predicate`` callable against each ``ReportingField``.

    Unknown field names are silently skipped.
    """
    result: list[str] = []
    for name in field_names:
        f = get_reporting_field(name)
        if f is None:
            continue
        if f.classification in exclude_classifications:
            continue
        if predicate is not None and not predicate(f):
            continue
        result.append(name)
    return tuple(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Re-exports from reporting_constants
    "FieldClassification",
    "ReportingField",
    "ReportingSourceGroup",
    "DashboardSection",
    "BlockedReason",
    "REPORTING_FIELDS",
    "BLOCKED_REASONS",
    "DASHBOARD_SECTIONS",
    "get_reporting_field",
    "get_field_names",
    "get_fields_by_classification",
    "get_dashboard_safe_fields",
    "get_audit_only_fields",
    "get_sensitive_fields",
    "get_source_group",
    "get_source_group_names",
    "get_dashboard_section",
    "get_dashboard_section_names",
    "get_blocked_reason",
    "get_blocked_reason_codes",
    # Query helper convenience proxies
    "dashboard_safe_field_names",
    "audit_only_field_names",
    "sensitive_field_names",
    "DASHBOARD_SAFE_FIELD_NAMES",
    "AUDIT_ONLY_FIELD_NAMES",
    "SENSITIVE_FIELD_NAMES",
    "ALL_FIELD_NAMES",
    # Validation
    "require_dashboard_safe_fields",
    "require_specific_fields",
    "require_no_sensitive_fields",
    # Source-group -> dashboard-section mapping
    "SOURCE_GROUP_TO_SECTION",
    "get_dashboard_section_for_source_group",
    # Reporting surfaces
    "ReportingSurface",
    "REPORTING_SURFACES",
    "get_reporting_surface",
    "get_reporting_surface_names",
    "get_field_names_for_surface",
    # Source-group field selection
    "reporting_fields_for_source_group",
    # Field filters
    "filter_field_names",
]
