"""Reconciliation Review Workflow Audit Layer v1.

A deterministic, in-memory review workflow that enriches review queue items
with audit-ready review statuses, structured evidence, and explicit
confirmation boundaries.  This workflow never applies final financial
mutations.

Design properties:
- **In-memory only**: operates on ``ReviewQueueItem`` objects produced by
  ``generate_review_queue()``, never touches a database.
- **Deterministic**: same inputs always produce the same output.
- **No-final-mutation guarantee**: every output explicitly declares that no
  final financial transactions, settlement obligations, or database records
  have been mutated.
- **AI-safe**: any AI-generated explanation text is labelled as review
  metadata only and is never treated as a financial fact.
- **Confirmation boundary**: every item explicitly states whether user
  confirmation is required before any apply step.

Usage::

    from finance_core.reconciliation.matching import match_batch
    from finance_core.reconciliation.review_queue import generate_review_queue
    from finance_core.reconciliation.review_workflow import build_review_workflow

    candidates = match_batch(statements, app_transactions)
    queue_items, summary = generate_review_queue(candidates)
    workflow = build_review_workflow(queue_items, summary)

    for item in workflow.items:
        print(item.review_status.value, item.requires_confirmation.value)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Sequence

from finance_core.reconciliation.models import (
    IssueType,
    ReasonCode,
    ReconciliationSummary,
    ReviewQueueItem,
    SuggestedAction,
)

# ---------------------------------------------------------------------------
# Review status enum
# ---------------------------------------------------------------------------


class ReviewStatus(str, Enum):
    """Audit-ready review status for a single reconciliation review item.

    Each status describes the current state of the item in the review
    pipeline.  Status names are intentionally explicit and boring:
    no hidden inference, no AI-authored labels.
    """

    ACCEPTED_MATCHED = "accepted_matched"
    """Exact or high-confidence match.  Ready for user review."""

    POSSIBLE_MATCH_NEEDS_CONFIRMATION = "possible_match_needs_confirmation"
    """Likely match but has a fuzzy signal (date window, merchant
    variation, or low confidence) and requires human confirmation."""

    UNMATCHED_STATEMENT = "unmatched_statement"
    """Statement transaction has no matching app record.  User must
    decide whether to create a missing app transaction."""

    UNMATCHED_APP = "unmatched_app"
    """App transaction has no matching statement record.  User must
    investigate whether this is expected."""

    AMOUNT_MISMATCH = "amount_mismatch"
    """Merchant or date information aligns, but the amounts differ.
    User must resolve the discrepancy."""

    DATE_MISMATCH = "date_mismatch"
    """Amount and merchant match, but transaction dates are outside
    the configured tolerance window."""

    MERCHANT_MISMATCH = "merchant_mismatch"
    """Amount and date match, but merchant names do not align."""

    DUPLICATE_SUSPICION = "duplicate_suspicion"
    """Multiple candidates or statement rows claim the same records.
    Possible duplicate, requires human triage."""

    CONFIRMED_PENDING_APPLY = "confirmed_pending_apply"
    """User has confirmed the match but the apply step has not yet
    been executed.  No final mutation has occurred."""

    REJECTED = "rejected"
    """User has reviewed and explicitly rejected this candidate.
    The rejection is audit-visible in the workflow output."""


# ---------------------------------------------------------------------------
# Confirmation requirement enum
# ---------------------------------------------------------------------------


class ConfirmationRequirement(str, Enum):
    """Whether user confirmation is required before any apply step."""

    REQUIRED = "required"
    """User must explicitly confirm before this item can proceed to
    the resolution apply runtime."""

    NOT_REQUIRED = "not_required"
    """This item is either an exact match or has already been
    confirmed and does not require additional confirmation."""


# ---------------------------------------------------------------------------
# Review decision state -- in-memory human review decision overlay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewDecisionState:
    """A lightweight in-memory record of a human review decision.

    Carries no financial mutations.  Records only the reviewer's intent
    (confirm or reject).  The actual apply step is a separate, guarded
    operation outside this audit layer.

    Attributes
    ----------
    outcome:
        ``"confirmed_pending_apply"`` or ``"rejected"``.
    note:
        Optional reviewer note explaining the decision.
    reviewer:
        Identifier of the reviewer.  Defaults to ``"human"``.
    """

    outcome: str
    note: str = ""
    reviewer: str = "human"

    def __post_init__(self):
        if self.outcome not in ("confirmed_pending_apply", "rejected"):
            raise ValueError(
                f"Invalid decision outcome: {self.outcome!r}. "
                f"Must be 'confirmed_pending_apply' or 'rejected'."
            )


# ---------------------------------------------------------------------------
# Review workflow item -- enriched audit-ready item
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewWorkflowItem:
    """A single reconciliation review item enriched with audit-ready fields.

    This dataclass bundles the original ``ReviewQueueItem`` with:
    - A deterministic ``ReviewStatus``
    - Structured statement-side and app-side evidence
    - Match and mismatch reasons derived from reason codes
    - A confidence score and severity indicator
    - A recommended next action
    - An audit note
    - An explicit confirmation-required flag
    - A no-final-mutation guarantee flag

    All monetary values use ``Decimal``.  Dates use ``date | None``.
    """

    queue_item: ReviewQueueItem
    """The original review queue item from ``generate_review_queue()``."""

    review_status: ReviewStatus
    """Deterministic review status derived from issue type and evidence."""

    requires_confirmation: ConfirmationRequirement
    """Whether user confirmation is required before any apply step."""

    # -- Structured evidence (statement side) --
    statement_merchant: str | None = None
    statement_amount: Decimal | None = None
    statement_currency: str | None = None
    statement_transaction_date: date | None = None
    statement_posted_date: date | None = None
    statement_reference: str | None = None

    # -- Structured evidence (app side) --
    app_transaction_id: str | None = None
    app_merchant: str | None = None
    app_amount: Decimal | None = None
    app_currency: str | None = None
    app_transaction_date: date | None = None

    # -- Computed deltas --
    amount_delta: Decimal | None = None
    date_delta_days: int | None = None
    merchant_similarity: float | None = None

    # -- Reasons --
    match_reason: str = ""
    """Human-readable summary of why a match was accepted, derived from
    supporting reason codes.  Empty string when no match exists."""

    mismatch_reason: str = ""
    """Human-readable summary of why a match was not made, derived from
    negative reason codes.  Empty string when no mismatch exists."""

    # -- Quality indicators --
    confidence_score: Decimal = Decimal("0.0")
    severity: str = "info"
    """Severity indicator: ``"error"``, ``"warning"``, or ``"info"``.
    Derived from review priority tier."""

    # -- Actions --
    recommended_action: str = ""
    """Human-readable recommended next action derived from the suggested
    action enum."""

    # -- Audit --
    audit_note: str = ""
    """Deterministic audit note explaining the review context.  May be
    augmented with AI-generated explanation text in future layers, but
    any AI content is labelled explicitly as non-authoritative."""

    ai_explanation: str = ""
    """Reserved for future AI-generated explanation text.  Always empty
    in v1.  When populated, callers must treat it as review metadata
    only -- never as a financial fact."""

    # -- Governance --
    no_final_mutation_applied: bool = True
    """Always ``True`` in v1.  The review workflow audit layer never
    applies final financial mutations.  This flag exists for downstream
    consumers to verify the governance boundary at a glance."""

    # -- Decision audit trail --
    decision_outcome: str = ""
    """When a ``ReviewDecisionState`` override was provided, the outcome
    ("confirmed_pending_apply" or "rejected").  Empty string
    when no decision override was applied."""

    decision_note: str = ""
    """When a ``ReviewDecisionState`` override was provided, the reviewer's
    note explaining the decision.  Empty string when no decision override
    was applied."""

    decision_reviewer: str = ""
    """When a ``ReviewDecisionState`` override was provided, the identifier
    of the reviewer.  Empty string when no decision override was applied."""


# ---------------------------------------------------------------------------
# Review workflow result -- full audit output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewWorkflowResult:
    """Complete output of the reconciliation review workflow audit layer.

    Contains all enriched review items, aggregate counts, and governance
    metadata that downstream consumers (human review UI, Metabase
    reporting, resolution runtime, apply runtime) can rely on.

    This result is a deterministic snapshot -- same inputs always produce
    the same output.
    """

    items: tuple[ReviewWorkflowItem, ...]
    """All review workflow items in deterministic (priority, date,
    merchant, amount) sort order."""

    total_items: int = 0
    matched_count: int = 0
    needs_review_count: int = 0
    amount_mismatch_count: int = 0
    date_mismatch_count: int = 0
    merchant_mismatch_count: int = 0
    unmatched_statement_count: int = 0
    unmatched_app_count: int = 0
    duplicate_suspicion_count: int = 0
    total_confirmation_required: int = 0

    no_final_mutation_applied: bool = True
    """Always ``True`` in v1.  This result is a read-only audit artifact.
    No final financial transactions, settlement obligations, database
    records, or Metabase reporting state have been mutated."""

    audit_context: dict = field(default_factory=dict)
    """Reserved for future audit metadata (run ID, timestamp, reviewer
    identity, etc)."""


# ---------------------------------------------------------------------------
# Review status classification (deterministic)
# ---------------------------------------------------------------------------

# Mapping from IssueType to ReviewStatus when no resolution has been applied.
_ISSUE_TO_REVIEW_STATUS: dict[IssueType, ReviewStatus] = {
    IssueType.MATCHED: ReviewStatus.ACCEPTED_MATCHED,
    IssueType.AMOUNT_MISMATCH: ReviewStatus.AMOUNT_MISMATCH,
    IssueType.CURRENCY_MISMATCH: ReviewStatus.AMOUNT_MISMATCH,
    IssueType.DATE_MISMATCH: ReviewStatus.DATE_MISMATCH,
    IssueType.DATE_WINDOW_MATCH: ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION,
    IssueType.MERCHANT_MISMATCH: ReviewStatus.MERCHANT_MISMATCH,
    IssueType.MERCHANT_VARIATION: ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION,
    IssueType.MISSING_IN_APP: ReviewStatus.UNMATCHED_STATEMENT,
    IssueType.MISSING_IN_STATEMENT: ReviewStatus.UNMATCHED_APP,
    IssueType.POSSIBLE_DUPLICATE: ReviewStatus.DUPLICATE_SUSPICION,
    IssueType.LOW_CONFIDENCE_MATCH: ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION,
    IssueType.NEEDS_REVIEW: ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION,
}
"""Deterministic mapping from issue type to review status."""

# Review statuses that require user confirmation.
_CONFIRMATION_REQUIRED_STATUSES: frozenset[ReviewStatus] = frozenset(
    {
        ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION,
        ReviewStatus.UNMATCHED_STATEMENT,
        ReviewStatus.UNMATCHED_APP,
        ReviewStatus.AMOUNT_MISMATCH,
        ReviewStatus.DATE_MISMATCH,
        ReviewStatus.MERCHANT_MISMATCH,
        ReviewStatus.DUPLICATE_SUSPICION,
    }
)


def classify_review_status(
    issue_type: IssueType,
) -> ReviewStatus:
    """Deterministically classify a review status from an issue type.

    This is a pure function: same issue type always returns the same
    review status.  No AI, no side effects, no database access.
    """
    return _ISSUE_TO_REVIEW_STATUS.get(issue_type, ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION)


def _confirmation_requirement(review_status: ReviewStatus) -> ConfirmationRequirement:
    """Return whether the review status requires user confirmation."""
    if review_status in _CONFIRMATION_REQUIRED_STATUSES:
        return ConfirmationRequirement.REQUIRED
    return ConfirmationRequirement.NOT_REQUIRED


# ---------------------------------------------------------------------------
# Reason builders
# ---------------------------------------------------------------------------

# Reason codes that are supporting (positive evidence).
_SUPPORTING_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.EXACT_AMOUNT_MATCH,
        ReasonCode.SAME_CURRENCY,
        ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE,
        ReasonCode.POSTED_DATE_WITHIN_TOLERANCE,
        ReasonCode.POSTED_DATE_WINDOW_MATCH,
        ReasonCode.MERCHANT_EXACT_MATCH,
        ReasonCode.MERCHANT_NORMALIZED_MATCH,
        ReasonCode.MERCHANT_SIMILARITY_MATCH,
    }
)

# Reason codes that are negative / blocking.
_NEGATIVE_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.AMOUNT_DIFFERS,
        ReasonCode.CURRENCY_DIFFERS,
        ReasonCode.OUTSIDE_DATE_TOLERANCE,
        ReasonCode.NO_CANDIDATE_FOUND,
        ReasonCode.WEAK_MERCHANT_MATCH,
        ReasonCode.MISSING_STATEMENT_DATE,
        ReasonCode.MISSING_INTERNAL_DATE,
        ReasonCode.MULTIPLE_CANDIDATE_MATCHES,
        ReasonCode.NEEDS_REVIEW,
    }
)

# Human-readable labels for reason codes.
_REASON_LABELS: dict[ReasonCode, str] = {
    ReasonCode.EXACT_AMOUNT_MATCH: "exact amount match",
    ReasonCode.SAME_CURRENCY: "same currency",
    ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE: "transaction date within tolerance",
    ReasonCode.POSTED_DATE_WITHIN_TOLERANCE: "posted date within tolerance",
    ReasonCode.POSTED_DATE_WINDOW_MATCH: "posted date within window",
    ReasonCode.MERCHANT_EXACT_MATCH: "merchant exact match",
    ReasonCode.MERCHANT_NORMALIZED_MATCH: "merchant normalized match",
    ReasonCode.MERCHANT_SIMILARITY_MATCH: "merchant similarity match",
    ReasonCode.AMOUNT_DIFFERS: "amount differs",
    ReasonCode.CURRENCY_DIFFERS: "currency differs",
    ReasonCode.OUTSIDE_DATE_TOLERANCE: "outside date tolerance",
    ReasonCode.NO_CANDIDATE_FOUND: "no candidate found",
    ReasonCode.WEAK_MERCHANT_MATCH: "weak merchant match",
    ReasonCode.MISSING_STATEMENT_DATE: "missing statement date",
    ReasonCode.MISSING_INTERNAL_DATE: "missing internal date",
    ReasonCode.MULTIPLE_CANDIDATE_MATCHES: "multiple candidate matches",
    ReasonCode.NEEDS_REVIEW: "needs review",
}


def _build_match_reason(reason_codes: tuple[ReasonCode, ...]) -> str:
    """Build a human-readable match reason from supporting reason codes."""
    supporting = [rc for rc in reason_codes if rc in _SUPPORTING_CODES]
    if not supporting:
        return ""
    labels = [_REASON_LABELS.get(rc, rc.value) for rc in supporting]
    return "; ".join(labels)


def _build_mismatch_reason(reason_codes: tuple[ReasonCode, ...]) -> str:
    """Build a human-readable mismatch reason from negative reason codes."""
    negative = [rc for rc in reason_codes if rc in _NEGATIVE_CODES]
    if not negative:
        return ""
    labels = [_REASON_LABELS.get(rc, rc.value) for rc in negative]
    return "; ".join(labels)


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------


def _severity_from_status(review_status: ReviewStatus) -> str:
    """Derive a severity indicator from the review status.

    Returns ``"error"``, ``"warning"``, or ``"info"``.
    """
    if review_status in (
        ReviewStatus.AMOUNT_MISMATCH,
        ReviewStatus.DUPLICATE_SUSPICION,
    ):
        return "error"
    if review_status in (
        ReviewStatus.DATE_MISMATCH,
        ReviewStatus.MERCHANT_MISMATCH,
        ReviewStatus.UNMATCHED_STATEMENT,
        ReviewStatus.UNMATCHED_APP,
        ReviewStatus.POSSIBLE_MATCH_NEEDS_CONFIRMATION,
    ):
        return "warning"
    return "info"


# ---------------------------------------------------------------------------
# Action label helper
# ---------------------------------------------------------------------------

_ACTION_LABELS: dict[SuggestedAction, str] = {
    SuggestedAction.CONFIRM_MATCH: "Confirm match and queue for apply",
    SuggestedAction.ADJUST_APP_TRANSACTION: "Adjust app transaction to match statement",
    SuggestedAction.CREATE_MISSING_APP_TRANSACTION: "Create missing app transaction record",
    SuggestedAction.MARK_STATEMENT_ONLY: "Mark as statement-only (no app record needed)",
    SuggestedAction.MARK_DUPLICATE: "Mark as duplicate and select canonical record",
    SuggestedAction.IGNORE: "Ignore this item (audit-visible)",
    SuggestedAction.NEEDS_MORE_INFO: "More information needed before decision",
}


def _action_label(action: SuggestedAction) -> str:
    return _ACTION_LABELS.get(action, action.value)


# ---------------------------------------------------------------------------
# Audit note builder
# ---------------------------------------------------------------------------


def _build_audit_note(item: ReviewWorkflowItem) -> str:
    """Build a deterministic audit note describing the review context.

    The note is assembled from structured data only -- no AI generation.
    """
    parts: list[str] = []

    parts.append(f"Review status: {item.review_status.value}")
    parts.append(f"Confirmation required: {item.requires_confirmation.value}")

    if item.statement_merchant:
        parts.append(f"Statement merchant: {item.statement_merchant}")
    if item.statement_amount is not None and item.statement_currency:
        parts.append(f"Statement amount: {item.statement_amount} {item.statement_currency}")

    if item.app_merchant:
        parts.append(f"App merchant: {item.app_merchant}")
    if item.app_amount is not None and item.app_currency:
        parts.append(f"App amount: {item.app_amount} {item.app_currency}")

    if item.amount_delta is not None and item.amount_delta != 0:
        parts.append(f"Amount delta: {item.amount_delta}")
    if item.date_delta_days is not None:
        parts.append(f"Date delta: {item.date_delta_days} days")

    if item.match_reason:
        parts.append(f"Match reasons: {item.match_reason}")
    if item.mismatch_reason:
        parts.append(f"Mismatch reasons: {item.mismatch_reason}")

    parts.append(f"Confidence: {item.confidence_score}")
    parts.append(f"Recommended action: {item.recommended_action}")

    if item.decision_outcome:
        parts.append(f"Decision outcome: {item.decision_outcome}")
        parts.append(f"Decision reviewer: {item.decision_reviewer}")
        if item.decision_note:
            parts.append(f"Decision note: {item.decision_note}")

    parts.append("No final mutation applied: True")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main workflow builder
# ---------------------------------------------------------------------------


def build_review_workflow(
    queue_items: Sequence[ReviewQueueItem],
    summary: ReconciliationSummary | None = None,
    *,
    audit_context: dict | None = None,
    decision_overrides: dict[str, ReviewDecisionState] | None = None,
) -> ReviewWorkflowResult:
    """Build an audit-ready review workflow result from review queue items.

    This is the primary entry point for the review workflow audit layer.
    It takes the output of ``generate_review_queue()`` and enriches each
    item with deterministic review status, structured evidence, match and
    mismatch reasons, severity, recommended action, audit note, and
    confirmation boundary.

    Parameters
    ----------
    queue_items:
        Review queue items from ``generate_review_queue()``.
    summary:
        Optional reconciliation summary for aggregate counts.  When not
        provided, counts are derived from the items.
    audit_context:
        Optional dict of audit metadata (run ID, timestamp, etc).
    decision_overrides:
        Optional dict mapping ``queue_item_id`` to ``ReviewDecisionState``.
        When provided, items matching a key have their review status and
        confirmation requirement overridden by the decision state.  This is
        how human confirmation (``"confirmed_pending_apply"``) and rejection
        (``"rejected"``) are represented without applying financial mutations.

    Returns
    -------
    ReviewWorkflowResult
        Deterministic audit-ready workflow output.  ``no_final_mutation_applied``
        is always ``True``.
    """
    enriched: list[ReviewWorkflowItem] = []

    for qi in queue_items:
        decision_state = None
        if decision_overrides is not None:
            decision_state = decision_overrides.get(qi.queue_item_id)
        item = _enrich_item(qi, decision_state=decision_state)
        enriched.append(item)

    # Build aggregate counts
    if summary is not None:
        total = len(enriched)
        matched = summary.matched_count
        needs_review = summary.needs_review_count
        amount_mm = summary.amount_mismatch_count
        # date_mm and merchant_mm need per-issue inspection from summary items
        date_mm = counts_by_issue(summary, {IssueType.DATE_MISMATCH, IssueType.DATE_WINDOW_MATCH})
        merchant_mm = counts_by_issue(
            summary, {IssueType.MERCHANT_MISMATCH, IssueType.MERCHANT_VARIATION}
        )
        unmatched_stmt = summary.missing_in_app_count
        unmatched_app = summary.missing_in_statement_count
        dup_count = summary.possible_duplicate_count
    else:
        total = len(enriched)
        matched = sum(1 for i in enriched if i.review_status == ReviewStatus.ACCEPTED_MATCHED)
        needs_review = total - matched
        amount_mm = sum(1 for i in enriched if i.review_status == ReviewStatus.AMOUNT_MISMATCH)
        date_mm = sum(1 for i in enriched if i.review_status == ReviewStatus.DATE_MISMATCH)
        merchant_mm = sum(1 for i in enriched if i.review_status == ReviewStatus.MERCHANT_MISMATCH)
        unmatched_stmt = sum(
            1 for i in enriched if i.review_status == ReviewStatus.UNMATCHED_STATEMENT
        )
        unmatched_app = sum(1 for i in enriched if i.review_status == ReviewStatus.UNMATCHED_APP)
        dup_count = sum(1 for i in enriched if i.review_status == ReviewStatus.DUPLICATE_SUSPICION)

    confirmation_required = sum(
        1 for i in enriched if i.requires_confirmation == ConfirmationRequirement.REQUIRED
    )

    return ReviewWorkflowResult(
        items=tuple(enriched),
        total_items=total,
        matched_count=matched,
        needs_review_count=needs_review,
        amount_mismatch_count=amount_mm,
        date_mismatch_count=date_mm,
        merchant_mismatch_count=merchant_mm,
        unmatched_statement_count=unmatched_stmt,
        unmatched_app_count=unmatched_app,
        duplicate_suspicion_count=dup_count,
        total_confirmation_required=confirmation_required,
        no_final_mutation_applied=True,
        audit_context=audit_context or {},
    )


def build_workflow_from_candidates(
    candidates: Sequence,
    *,
    run_label: str = "",
    audit_context: dict | None = None,
    decision_overrides: dict[str, ReviewDecisionState] | None = None,
) -> ReviewWorkflowResult:
    """Convenience: build a review workflow result directly from reconciliation
    candidates, running the review queue generator internally.

    Parameters
    ----------
    candidates:
        Reconciliation candidates from ``match_batch()``.
    run_label:
        Optional label for queue item IDs.
    audit_context:
        Optional audit metadata dict.
    decision_overrides:
        Optional dict mapping ``queue_item_id`` to ``ReviewDecisionState``.
        Passed through to ``build_review_workflow()``.
    """
    from finance_core.reconciliation.review_queue import generate_review_queue

    queue_items, summary = generate_review_queue(list(candidates), run_label=run_label)
    return build_review_workflow(
        queue_items,
        summary,
        audit_context=audit_context,
        decision_overrides=decision_overrides,
    )


# ---------------------------------------------------------------------------
# Internal enrichment
# ---------------------------------------------------------------------------


def _enrich_item(
    qi: ReviewQueueItem,
    *,
    decision_state: "ReviewDecisionState | None" = None,
) -> ReviewWorkflowItem:
    """Enrich a single ReviewQueueItem into a ReviewWorkflowItem.

    When *decision_state* is provided, it overrides the issue-type-based
    review status and confirmation requirement.  This is how human
    confirmation and rejection decisions are represented in the workflow
    without applying financial mutations.
    """
    evidence = qi.structured_evidence

    if evidence is not None:
        stmt_merchant = evidence.statement_merchant
        stmt_amount = evidence.statement_amount
        stmt_currency = evidence.statement_currency
        stmt_txn_date = evidence.statement_transaction_date
        stmt_posted_date = evidence.statement_posted_date
        stmt_ref = evidence.statement_reference
        app_id = evidence.app_transaction_id
        app_merchant = evidence.app_merchant
        app_amount = evidence.app_amount
        app_currency = evidence.app_currency
        app_txn_date = evidence.app_transaction_date
        amount_delta = evidence.amount_delta
        date_delta_days = evidence.date_delta_days
        merchant_sim = evidence.merchant_similarity
        confidence = evidence.confidence_score
    else:
        cand = qi.candidate
        stmt = cand.statement
        stmt_merchant = stmt.merchant_raw
        stmt_amount = stmt.amount
        stmt_currency = stmt.currency
        stmt_txn_date = stmt.transaction_date
        stmt_posted_date = stmt.posted_date
        stmt_ref = stmt.statement_row_reference
        best = cand.best_app_transaction
        if best is not None:
            app_id = best.app_txn_id
            app_merchant = best.merchant
            app_amount = best.amount
            app_currency = best.currency
            app_txn_date = best.transaction_date
        else:
            app_id = None
            app_merchant = None
            app_amount = None
            app_currency = None
            app_txn_date = None
        amount_delta = None
        if stmt_amount is not None and app_amount is not None:
            amount_delta = stmt_amount - app_amount
        date_delta_days = None
        merchant_sim = None
        confidence = cand.confidence_score

    if decision_state is not None:
        if decision_state.outcome == "confirmed_pending_apply":
            review_status = ReviewStatus.CONFIRMED_PENDING_APPLY
            requires_confirmation = ConfirmationRequirement.NOT_REQUIRED
        elif decision_state.outcome == "rejected":
            review_status = ReviewStatus.REJECTED
            requires_confirmation = ConfirmationRequirement.NOT_REQUIRED
        else:
            # Guard: _post_init__ already validates outcomes, but be safe.
            review_status = classify_review_status(qi.issue_type)
            requires_confirmation = _confirmation_requirement(review_status)
    else:
        review_status = classify_review_status(qi.issue_type)
        requires_confirmation = _confirmation_requirement(review_status)

    # Extract decision audit fields when a decision state is present
    if decision_state is not None:
        decision_outcome = decision_state.outcome
        decision_note = decision_state.note
        decision_reviewer = decision_state.reviewer
    else:
        decision_outcome = ""
        decision_note = ""
        decision_reviewer = ""

    reason_codes = qi.reason_codes
    match_reason = _build_match_reason(reason_codes)
    mismatch_reason = _build_mismatch_reason(reason_codes)
    severity = _severity_from_status(review_status)
    recommended_action = _action_label(qi.suggested_action)

    # Build item with placeholder audit_note (will be set after construction)
    item = ReviewWorkflowItem(
        queue_item=qi,
        review_status=review_status,
        requires_confirmation=requires_confirmation,
        statement_merchant=stmt_merchant,
        statement_amount=stmt_amount,
        statement_currency=stmt_currency,
        statement_transaction_date=stmt_txn_date,
        statement_posted_date=stmt_posted_date,
        statement_reference=stmt_ref,
        app_transaction_id=app_id,
        app_merchant=app_merchant,
        app_amount=app_amount,
        app_currency=app_currency,
        app_transaction_date=app_txn_date,
        amount_delta=amount_delta,
        date_delta_days=date_delta_days,
        merchant_similarity=merchant_sim,
        match_reason=match_reason,
        mismatch_reason=mismatch_reason,
        confidence_score=confidence,
        severity=severity,
        recommended_action=recommended_action,
        decision_outcome=decision_outcome,
        decision_note=decision_note,
        decision_reviewer=decision_reviewer,
    )

    # Build audit note from the assembled item
    audit_note = _build_audit_note(item)
    object.__setattr__(item, "audit_note", audit_note)

    return item


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def counts_by_issue(summary: ReconciliationSummary, issue_types: set[IssueType]) -> int:
    """Count review queue items matching a set of issue types from a summary.

    Since ``ReconciliationSummary`` does not carry per-issue-type counts
    for date_window_match or merchant_variation separately, this helper
    inspects the review_items and matched_items lists.
    """
    count = 0
    for item in summary.review_items + summary.matched_items:
        if item.issue_type in issue_types:
            count += 1
    return count


__all__ = [
    "ConfirmationRequirement",
    "ReviewDecisionState",
    "ReviewStatus",
    "ReviewWorkflowItem",
    "ReviewWorkflowResult",
    "build_review_workflow",
    "build_workflow_from_candidates",
    "classify_review_status",
]
