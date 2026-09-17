"""Reporting Constants / Field Registry v1.

Read-only module exporting stable reporting field names, source group labels,
dashboard section names, blocked reason codes, and sensitivity classifications
based on the Metabase / Reporting Preparation v1 design contract.

This module is deliberately thin and immutable:

- No database connection.
- No Metabase dependency.
- No migration.
- No runtime query execution.
- No live database path reference.
- No mutation functions.

All exported structures are frozen (immutable) dataclasses, enums, or tuples.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
# Sensitivity / classification constants
# ---------------------------------------------------------------------------


class FieldClassification(Enum):
    """Stable reporting field sensitivity classification.

    Values must match the design contract in
    ``docs/design/metabase_reporting_preparation_v1.md``.
    """

    DASHBOARD_SAFE = "dashboard_safe"
    AUDIT_ONLY = "audit_only"
    SENSITIVE = "sensitive"


# ---------------------------------------------------------------------------
# Reporting field registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportingField:
    """A single reporting field as defined in the design doc field mapping."""

    name: str
    source_concept: str
    description: str
    expected_type: str
    classification: FieldClassification

    @property
    def dashboard_safe(self) -> bool:
        """True when this field is safe for unrestricted dashboard display."""
        return self.classification == FieldClassification.DASHBOARD_SAFE

    @property
    def audit_only(self) -> bool:
        """True when this field is restricted to audit views only."""
        return self.classification == FieldClassification.AUDIT_ONLY

    @property
    def sensitive(self) -> bool:
        """True when this field requires masking or truncation in dashboards."""
        return self.classification == FieldClassification.SENSITIVE

    @property
    def masking_required(self) -> bool:
        """True when masking or truncation is required for dashboard display."""
        return self.classification in (
            FieldClassification.SENSITIVE,
            FieldClassification.AUDIT_ONLY,
        )


REPORTING_FIELDS: tuple[ReportingField, ...] = (
    ReportingField(
        name="statement_source_id",
        source_concept="StatementSourceIdentity / import contracts",
        description="Stable identity for the statement source file",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="source_file",
        source_concept="attachment_path from import / intake",
        description="Path to the original source file",
        expected_type="str",
        classification=FieldClassification.SENSITIVE,
    ),
    ReportingField(
        name="attachment_path",
        source_concept="raw_row_payload / StructuredStatementRow",
        description="Path to the original source file",
        expected_type="str",
        classification=FieldClassification.SENSITIVE,
    ),
    ReportingField(
        name="import_batch_id",
        source_concept="StatementImportBatch.public_id",
        description="Public identity of the import batch",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="statement_row_id",
        source_concept="StatementTransaction.public_id",
        description="Public identity of the statement row",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="transaction_date",
        source_concept="StatementTransaction.transaction_date",
        description="Date the transaction occurred",
        expected_type="date",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="posted_date",
        source_concept="StatementTransaction.posted_date",
        description="Date the bank/card posted the transaction",
        expected_type="date",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="amount",
        source_concept="StatementTransaction.amount",
        description="Normalized non-negative amount",
        expected_type="Decimal",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="currency",
        source_concept="StatementTransaction.currency",
        description="Three-letter ISO currency code",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="amount_direction",
        source_concept="StatementAmountDirection",
        description=("Debit / credit / refund / payment / fee / interest / unknown"),
        expected_type="str (enum value)",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="description",
        source_concept="StatementTransaction.merchant_raw",
        description="Merchant or transaction description",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="match_status",
        source_concept="MatchStatus",
        description="Matcher outcome status",
        expected_type="str (enum value)",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="match_rate",
        source_concept="ReconciliationSummary.match_rate",
        description="Percentage of rows matched",
        expected_type="float",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="review_status",
        source_concept="reconciliation_review_queue.status",
        description="Review queue item status",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="review_priority",
        source_concept="ReviewPriority",
        description="HIGH / MEDIUM / LOW",
        expected_type="str (enum value)",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="requires_human_review",
        source_concept=("GuardedApplyExecutionReviewSummary.requires_human_review"),
        description="Whether the row requires human attention",
        expected_type="bool",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="execution_status",
        source_concept="ApplyExecutionStatus",
        description="Guarded apply execution result status",
        expected_type="str (enum value)",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="blocked_reason_codes",
        source_concept="FinalMutationBlockedReason / block_reason",
        description="Stable reason codes for blocked operations",
        expected_type="list[str]",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="evidence_refs",
        source_concept=("StructuredEvidenceRecord.public_id / guard decision refs"),
        description="Evidence reference identifiers",
        expected_type="list[str]",
        classification=FieldClassification.AUDIT_ONLY,
    ),
    ReportingField(
        name="guard_decision_refs",
        source_concept="FinalMutationGuardDecision",
        description="Guard decision reference identifiers",
        expected_type="list[str]",
        classification=FieldClassification.AUDIT_ONLY,
    ),
    ReportingField(
        name="raw_text",
        source_concept="raw_row_text from import / PDF bridge",
        description="Raw extracted text from source row",
        expected_type="str",
        classification=FieldClassification.SENSITIVE,
    ),
    ReportingField(
        name="raw_row_text",
        source_concept=("ParsedPdfStatementRow.raw_row_text / StructuredStatementRow"),
        description="Raw extracted text from source row",
        expected_type="str",
        classification=FieldClassification.SENSITIVE,
    ),
    ReportingField(
        name="raw_amount",
        source_concept="StructuredStatementRow.raw_amount",
        description="Original amount string from source",
        expected_type="str",
        classification=FieldClassification.AUDIT_ONLY,
    ),
    ReportingField(
        name="created_at",
        source_concept="Various persistence tables",
        description="Row creation timestamp",
        expected_type="datetime",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="updated_at",
        source_concept="Various persistence tables",
        description="Row last-update timestamp",
        expected_type="datetime",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="reporting_bucket",
        source_concept="reconciliation_reporting_views_v1.sql",
        description="Consolidated reporting category",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="reporting_reason",
        source_concept="reconciliation_reporting_views_v1.sql",
        description="Human-readable reason for bucket assignment",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="decision_action",
        source_concept="ResolutionDecision.decision_action",
        description="Reviewer's decision (confirm_match, reject_match, etc.)",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="decision_note",
        source_concept="ResolutionDecision.decision_note",
        description="Reviewer's free-text note",
        expected_type="str",
        classification=FieldClassification.SENSITIVE,
    ),
    ReportingField(
        name="reviewer",
        source_concept="ResolutionDecision.reviewer",
        description="Identity of the reviewer",
        expected_type="str",
        classification=FieldClassification.SENSITIVE,
    ),
    ReportingField(
        name="source_page_number",
        source_concept="ParsedPdfStatementRow.source_page_number",
        description="PDF page number",
        expected_type="int",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="source_row_ref",
        source_concept="ParsedPdfStatementRow.source_row_ref",
        description="PDF row/line reference",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="source_type",
        source_concept="Import contracts / source identity",
        description="CSV or PDF source classification",
        expected_type="str",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="amount_delta",
        source_concept=("MatchEvidence.amount_delta / ReviewQueueExportItem.amount_delta"),
        description="Statement vs candidate amount difference",
        expected_type="Decimal",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="date_delta_days",
        source_concept="MatchEvidence.date_delta_days",
        description="Statement vs candidate date difference in days",
        expected_type="int",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
    ReportingField(
        name="merchant_similarity",
        source_concept="MatchEvidence.merchant_similarity",
        description="Normalized merchant name similarity score",
        expected_type="float",
        classification=FieldClassification.DASHBOARD_SAFE,
    ),
)


def get_reporting_field(name: str) -> ReportingField | None:
    """Look up a reporting field by name.

    Returns ``None`` when the field name is not in the registry.
    """
    for f in REPORTING_FIELDS:
        if f.name == name:
            return f
    return None


def get_field_names() -> tuple[str, ...]:
    """Return stable tuple of all reporting field names."""
    return tuple(f.name for f in REPORTING_FIELDS)


def get_fields_by_classification(
    classification: FieldClassification,
) -> tuple[ReportingField, ...]:
    """Return all reporting fields with the given sensitivity classification."""
    return tuple(f for f in REPORTING_FIELDS if f.classification == classification)


def get_dashboard_safe_fields() -> tuple[ReportingField, ...]:
    """Return all fields safe for general Metabase dashboard display."""
    return get_fields_by_classification(FieldClassification.DASHBOARD_SAFE)


def get_audit_only_fields() -> tuple[ReportingField, ...]:
    """Return all fields restricted to audit views."""
    return get_fields_by_classification(FieldClassification.AUDIT_ONLY)


def get_sensitive_fields() -> tuple[ReportingField, ...]:
    """Return all fields requiring masking or truncation in dashboards."""
    return get_fields_by_classification(FieldClassification.SENSITIVE)


# ---------------------------------------------------------------------------
# Reporting source groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportingSourceGroup:
    """A reporting source group as defined in Section 3 of the design doc."""

    name: str
    label: str
    sources: tuple[str, ...]


REPORTING_SOURCE_GROUPS: tuple[ReportingSourceGroup, ...] = (
    ReportingSourceGroup(
        name="statement_import_health",
        label="Statement Import Health",
        sources=(
            "statement_import_batches",
            "statement_transactions",
            "statement_import_contracts",
            "CSV import runtime",
            "future PDF import bridge",
        ),
    ),
    ReportingSourceGroup(
        name="reconciliation_match_outcomes",
        label="Reconciliation Match Outcomes",
        sources=(
            "reconciliation_match_results",
            "matcher",
            "ReconciliationSummary",
        ),
    ),
    ReportingSourceGroup(
        name="review_queue_status",
        label="Review Queue Status",
        sources=(
            "reconciliation_review_queue",
            "ReviewQueueItem",
            "ReconciliationReviewQueue",
            "build_review_queue_export",
        ),
    ),
    ReportingSourceGroup(
        name="review_resolution_outcomes",
        label="Review Resolution Outcomes",
        sources=(
            "reconciliation_resolution_decisions",
            "ResolutionDecision",
            "ResolutionResult",
        ),
    ),
    ReportingSourceGroup(
        name="guarded_apply_plan_status",
        label="Guarded Apply Plan Status",
        sources=(
            "ReconciliationApplyPlan",
            "ApplyPlanOperation",
            "ApplyPlanRisk",
            "build_reconciliation_apply_plan",
        ),
    ),
    ReportingSourceGroup(
        name="guarded_apply_execution_status",
        label="Guarded Apply Execution Status",
        sources=(
            "reconciliation_guarded_apply_executions",
            "reconciliation_guarded_apply_operation_results",
            "GuardedApplyExecutionResult",
            "build_apply_execution_review_queue",
        ),
    ),
    ReportingSourceGroup(
        name="human_review_required_items",
        label="Human-Review-Required Items",
        sources=(
            "GuardedApplyExecutionReviewSummary",
            "ApplyReviewQueueEntry",
            "build_apply_execution_review_queue",
        ),
    ),
    ReportingSourceGroup(
        name="blocked_apply_operations",
        label="Blocked / Partially Blocked Apply Operations",
        sources=(
            "Final mutation guard decisions",
            "reconciliation_final_mutation_guard_decisions",
            "GuardedApplyExecutionResult",
        ),
    ),
    ReportingSourceGroup(
        name="evidence_completeness",
        label="Evidence Completeness / Missing Evidence Warnings",
        sources=(
            "reconciliation_structured_evidence",
            "StructuredEvidenceRecord",
            "ReviewEvidenceBundle",
            "ReviewEvidenceService",
        ),
    ),
    ReportingSourceGroup(
        name="pdf_import_readiness",
        label="PDF Statement Import Readiness",
        sources=(
            "PDF Statement Import Bridge v1",
            "test_pdf_statement_import_bridge_contract_v1.py",
        ),
    ),
)


def get_source_group(name: str) -> ReportingSourceGroup | None:
    """Look up a reporting source group by name.

    Returns ``None`` when the source group name is not in the registry.
    """
    for g in REPORTING_SOURCE_GROUPS:
        if g.name == name:
            return g
    return None


def get_source_group_names() -> tuple[str, ...]:
    """Return stable tuple of all reporting source group names."""
    return tuple(g.name for g in REPORTING_SOURCE_GROUPS)


# ---------------------------------------------------------------------------
# Dashboard candidate sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DashboardSection:
    """A candidate dashboard section as defined in Section 4 of the design doc."""

    name: str
    label: str


DASHBOARD_SECTIONS: tuple[DashboardSection, ...] = (
    DashboardSection(name="intake_health", label="Intake Health"),
    DashboardSection(name="reconciliation_summary", label="Reconciliation Summary"),
    DashboardSection(name="needs_review", label="Needs Review"),
    DashboardSection(name="apply_execution_audit", label="Apply Execution Audit"),
    DashboardSection(name="blocked_items", label="Blocked Items"),
    DashboardSection(
        name="evidence_audit_completeness",
        label="Evidence / Audit Completeness",
    ),
    DashboardSection(name="pdf_import_readiness", label="PDF Import Readiness"),
)


def get_dashboard_section(name: str) -> DashboardSection | None:
    """Look up a dashboard section by name."""
    for s in DASHBOARD_SECTIONS:
        if s.name == name:
            return s
    return None


def get_dashboard_section_names() -> tuple[str, ...]:
    """Return stable tuple of all dashboard section names."""
    return tuple(s.name for s in DASHBOARD_SECTIONS)


# ---------------------------------------------------------------------------
# Blocked reason codes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockedReason:
    """A blocked reason code as defined in Section 8 of the design doc."""

    code: str
    source: str
    meaning: str


BLOCKED_REASONS: tuple[BlockedReason, ...] = (
    BlockedReason(
        code="missing_currency",
        source="Statement import / PDF bridge",
        meaning="Row currency could not be determined",
    ),
    BlockedReason(
        code="ambiguous_dates",
        source="Statement import / PDF bridge",
        meaning="Transaction date is ambiguous or missing",
    ),
    BlockedReason(
        code="ambiguous_amount_direction",
        source="Statement import / PDF bridge",
        meaning="Debit/credit sign is ambiguous",
    ),
    BlockedReason(
        code="duplicate_fingerprint",
        source="Statement import / PDF bridge",
        meaning="Row fingerprint collides within same batch",
    ),
    BlockedReason(
        code="missing_evidence_refs",
        source="Evidence completeness",
        meaning="No evidence records found for the row",
    ),
    BlockedReason(
        code="unsupported_statement_layout",
        source="PDF import readiness",
        meaning="PDF layout is not supported by the parser",
    ),
    BlockedReason(
        code="guard_decision_blocked",
        source="Final mutation guard",
        meaning="Guard decision rejected the mutation proposal",
    ),
    BlockedReason(
        code="execution_blocked",
        source="Guarded apply execution",
        meaning="Apply execution was fully blocked",
    ),
    BlockedReason(
        code="execution_partially_blocked",
        source="Guarded apply execution",
        meaning="Some operations executed, others blocked",
    ),
    BlockedReason(
        code="execution_conflict",
        source="Guarded apply execution",
        meaning="Idempotency conflict on repeat apply",
    ),
    BlockedReason(
        code="execution_unsupported",
        source="Guarded apply execution",
        meaning="Operation type is not supported",
    ),
    BlockedReason(
        code="missing_description",
        source="Statement import / PDF bridge",
        meaning="Row has no merchant/description",
    ),
    BlockedReason(
        code="missing_amount",
        source="Statement import / PDF bridge",
        meaning="Row amount is missing",
    ),
    BlockedReason(
        code="unknown_amount_direction",
        source="Statement import / PDF bridge",
        meaning="Amount direction classified as UNKNOWN",
    ),
)


def get_blocked_reason(code: str) -> BlockedReason | None:
    """Look up a blocked reason by code."""
    for r in BLOCKED_REASONS:
        if r.code == code:
            return r
    return None


def get_blocked_reason_codes() -> tuple[str, ...]:
    """Return stable tuple of all blocked reason codes."""
    return tuple(r.code for r in BLOCKED_REASONS)


# ---------------------------------------------------------------------------
# Module-level lookup helpers
# ---------------------------------------------------------------------------

REPORTING_FIELD_MAP: dict[str, ReportingField] = {f.name: f for f in REPORTING_FIELDS}
"""Frozen field-name -> ReportingField lookup dict.

Cached at module load. Dict keys are immutable strings; values are frozen
dataclass instances. The dict itself is not frozen, but the caller should
treat it as read-only (project convention: no mutation of module-level
exports).
"""

SOURCE_GROUP_MAP: dict[str, ReportingSourceGroup] = {g.name: g for g in REPORTING_SOURCE_GROUPS}

DASHBOARD_SECTION_MAP: dict[str, DashboardSection] = {s.name: s for s in DASHBOARD_SECTIONS}

BLOCKED_REASON_MAP: dict[str, BlockedReason] = {r.code: r for r in BLOCKED_REASONS}

__all__ = [
    "FieldClassification",
    "ReportingField",
    "REPORTING_FIELDS",
    "REPORTING_FIELD_MAP",
    "get_reporting_field",
    "get_field_names",
    "get_fields_by_classification",
    "get_dashboard_safe_fields",
    "get_audit_only_fields",
    "get_sensitive_fields",
    "ReportingSourceGroup",
    "REPORTING_SOURCE_GROUPS",
    "SOURCE_GROUP_MAP",
    "get_source_group",
    "get_source_group_names",
    "DashboardSection",
    "DASHBOARD_SECTIONS",
    "DASHBOARD_SECTION_MAP",
    "get_dashboard_section",
    "get_dashboard_section_names",
    "BlockedReason",
    "BLOCKED_REASONS",
    "BLOCKED_REASON_MAP",
    "get_blocked_reason",
    "get_blocked_reason_codes",
]
