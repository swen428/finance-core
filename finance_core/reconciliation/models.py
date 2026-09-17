"""Reconciliation Foundation v1 -- domain models, statuses, and reason codes.

Statement transactions are structured inputs from bank / credit-card
statements.  Internal candidates come from existing Finance records.
The matcher produces MatchResult instances with a status and a list of
deterministic reason codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


class MatchStatus(str, Enum):
    """Top-level outcome of a single statement-to-candidate match attempt."""

    MATCHED = "matched"
    NO_MATCH = "no_match"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    DATE_MISMATCH = "date_mismatch"
    MERCHANT_MISMATCH = "merchant_mismatch"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    AMBIGUOUS = "ambiguous"
    NEEDS_REVIEW = "needs_review"


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


class ReasonCode(str, Enum):
    """Deterministic reason for why a match was accepted, rejected, or flagged."""

    # Supporting (positive evidence)
    EXACT_AMOUNT_MATCH = "exact_amount_match"
    SAME_CURRENCY = "same_currency"
    TRANSACTION_DATE_WITHIN_TOLERANCE = "transaction_date_within_tolerance"
    POSTED_DATE_WITHIN_TOLERANCE = "posted_date_within_tolerance"
    POSTED_DATE_WINDOW_MATCH = "posted_date_window_match"
    MERCHANT_EXACT_MATCH = "merchant_exact_match"
    MERCHANT_NORMALIZED_MATCH = "merchant_normalized_match"
    MERCHANT_SIMILARITY_MATCH = "merchant_similarity_match"
    DIRECTION_TYPE_COMPATIBLE = "direction_type_compatible"

    # Negative / blocking
    AMOUNT_DIFFERS = "amount_differs"
    CURRENCY_DIFFERS = "currency_differs"
    OUTSIDE_DATE_TOLERANCE = "outside_date_tolerance"
    NO_CANDIDATE_FOUND = "no_candidate_found"
    WEAK_MERCHANT_MATCH = "weak_merchant_match"
    MISSING_STATEMENT_DATE = "missing_statement_date"
    MISSING_INTERNAL_DATE = "missing_internal_date"
    MULTIPLE_CANDIDATE_MATCHES = "multiple_candidate_matches"
    NEEDS_REVIEW = "needs_review"
    MISSING_DIRECTION = "missing_direction"
    UNKNOWN_DIRECTION = "unknown_direction"
    DIRECTION_TYPE_INCOMPATIBLE = "direction_type_incompatible"
    UNKNOWN_TRANSACTION_TYPE = "unknown_transaction_type"
    AMOUNT_SIGN_CONTRADICTION = "amount_sign_contradiction"
    ZERO_AMOUNT_REQUIRES_REVIEW = "zero_amount_requires_review"
    INVALID_SOURCE_IDENTITY = "invalid_source_identity"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementTransaction:
    """A single structured row from an imported bank / credit-card statement.

    All monetary fields use ``Decimal``.  Optional fields default to ``None``.
    """

    transaction_date: date | None
    posted_date: date | None
    merchant_raw: str
    merchant_normalized: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    account_name: str | None = None
    account_id: str | None = None
    source_batch_id: str | None = None
    statement_row_reference: str | None = None
    public_id: str | None = None
    row_fingerprint: str | None = None
    source_content_hash: str | None = None
    amount_direction: "StatementAmountDirection | None" = None
    raw_amount: str | None = None
    raw_amount_type: str | None = None

    def __post_init__(self) -> None:
        if self.amount is not None:
            if not isinstance(self.amount, Decimal):
                raise TypeError("Statement amount must be Decimal or None")
            if not self.amount.is_finite():
                raise ValueError("Statement amount must be finite")
            if self.amount < 0:
                raise ValueError(
                    f"Statement normalized amount must be zero or positive, got {self.amount}"
                )
        if isinstance(self.amount_direction, str):
            try:
                object.__setattr__(
                    self,
                    "amount_direction",
                    StatementAmountDirection(self.amount_direction.strip().lower()),
                )
            except ValueError as exc:
                raise ValueError(
                    f"Invalid statement amount direction: {self.amount_direction!r}"
                ) from exc
        elif self.amount_direction is not None and not isinstance(
            self.amount_direction, StatementAmountDirection
        ):
            raise TypeError("amount_direction must be StatementAmountDirection, string, or None")
        if self.source_content_hash is not None and not _is_sha256(self.source_content_hash):
            raise ValueError("source_content_hash must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class InternalCandidate:
    """An existing Finance internal record to match against."""

    internal_id: str
    transaction_date: date
    merchant: str
    amount: Decimal
    currency: str
    source_type: str | None = None
    source_channel: str | None = None
    attachment_path: str | None = None
    evidence_reference: str | None = None
    transaction_type: "ReconciliationTransactionType | str | None" = None
    posted_date: date | None = None

    def __post_init__(self) -> None:
        if not self.internal_id.strip():
            raise ValueError("Internal candidate identity must not be empty")
        if not isinstance(self.amount, Decimal):
            raise TypeError("Internal candidate amount must be Decimal")
        if not self.amount.is_finite():
            raise ValueError("Internal candidate amount must be finite")
        if self.amount <= 0:
            raise ValueError(f"Internal candidate amount must be positive, got {self.amount}")


@dataclass(frozen=True)
class MatchEvidence:
    """Structured evidence collected during a match attempt.

    This is the deterministic audit trail that explains *why* a match
    was accepted, rejected, or flagged.
    """

    statement_amount: Decimal | None = None
    candidate_amount: Decimal | None = None
    statement_currency: str | None = None
    candidate_currency: str | None = None
    statement_txn_date: date | None = None
    statement_posted_date: date | None = None
    candidate_txn_date: date | None = None
    date_delta_days: int | None = None
    date_tolerance_days: int = 3
    statement_merchant: str | None = None
    candidate_merchant: str | None = None
    statement_merchant_normalized: str | None = None
    candidate_merchant_normalized: str | None = None
    merchant_similarity: float | None = None
    candidate_count: int = 0
    statement_direction: str | None = None
    candidate_transaction_type: str | None = None
    original_amount_text: str | None = None
    original_amount_sign: str | None = None
    compatibility_result: str | None = None
    hard_gate_results: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MatchResult:
    """Outcome of matching one statement transaction against a set of
    internal candidates.

    ``status`` is the top-level outcome; ``reasons`` is a list of
    deterministic reason codes explaining the decision.

    ``best_candidate`` is populated only when the status is ``MATCHED``.
    ``candidates`` holds every internal record that was considered.
    """

    status: MatchStatus
    reasons: tuple[ReasonCode, ...]
    evidence: MatchEvidence = field(default_factory=MatchEvidence)

    statement: StatementTransaction | None = None
    best_candidate: InternalCandidate | None = None
    candidates: tuple[InternalCandidate, ...] = field(default_factory=tuple)
    decision_hash: str | None = None
    decision_material_json: str | None = None
    candidate_set_fingerprint: str | None = None
    decision_contract_version: str | None = None
    matcher_version: str | None = None
    compatibility_version: str | None = None
    merchant_normalization_version: str | None = None
    authorization_public_id: str | None = None

    @property
    def is_matched(self) -> bool:
        return self.status == MatchStatus.MATCHED

    @property
    def needs_review(self) -> bool:
        return self.status in {
            MatchStatus.NEEDS_REVIEW,
            MatchStatus.AMBIGUOUS,
            MatchStatus.POSSIBLE_DUPLICATE,
        }


# ---------------------------------------------------------------------------
# Merchant normalizers (public helpers, deterministic)
# ---------------------------------------------------------------------------


_MERCHANT_ALIAS_MAP: dict[str, str] = {
    "apple.com/bill": "apple",
    "apple": "apple",
    "google": "google",
    "google play": "google",
    "google *youtube": "google",
    "netflix": "netflix",
    "netflix.com": "netflix",
    "spotify": "spotify",
    "spotify usa": "spotify",
    "uber": "uber",
    "uber trip": "uber",
    "uber eats": "uber",
    "grab": "grab",
    "grab taxi": "grab",
    "grab food": "grab",
    "foodpanda": "foodpanda",
    "foodpanda sg": "foodpanda",
    "shopee": "shopee",
    "shopee pay": "shopee",
    "lazada": "lazada",
    "lazada sg": "lazada",
    "amazon": "amazon",
    "amazon prime": "amazon",
    "amazon web services": "amazon",
    "aws": "amazon",
}
"""Conservative merchant alias map -- explicit, not fuzzy-matched.

Future extension: this should live in a config or database table so
users can maintain their own aliases without modifying source code.
"""


def normalize_merchant(raw: str) -> str:
    """Normalize a raw merchant name to a canonical alias.

    Returns the alias if one exists; otherwise returns the lowercased,
    stripped input unchanged.
    """
    key = raw.strip().lower()
    return _MERCHANT_ALIAS_MAP.get(key, key)


def merchant_similarity(a: str, b: str) -> float:
    """Simple token-based Jaccard similarity for merchant names.

    Returns 0.0--1.0.  This is intentionally conservative: similarity
    alone is never treated as a match; it only contributes evidence
    when amount, currency, and date already align.
    """
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Statement amount direction classification
# ---------------------------------------------------------------------------


class StatementAmountDirection(str, Enum):
    """Deterministic classification of an imported statement row amount
    direction and transaction meaning.

    This is a focused, rules-based classification. AI must not be used
    as the final authority for monetary direction. Ambiguous rows should
    be classified as UNKNOWN and left for human review.
    """

    DEBIT = "debit"
    CREDIT = "credit"
    REFUND = "refund"
    REVERSAL = "reversal"
    PAYMENT = "payment"
    FEE = "fee"
    INTEREST = "interest"
    CHARGEBACK = "chargeback"
    CARD_PAYMENT = "card_payment"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    INTEREST_DEBIT = "interest_debit"
    INTEREST_CREDIT = "interest_credit"
    CASH_WITHDRAWAL = "cash_withdrawal"
    CASH_DEPOSIT = "cash_deposit"
    UNKNOWN = "unknown"


class ReconciliationTransactionType(str, Enum):
    """Matcher-only transaction semantics derived from existing transaction intents.

    This enum does not add canonical Finance transaction facts.  It supplies the
    explicit compatibility categories needed before a statement row may be scored.
    """

    EXPENSE = "expense"
    INCOME = "income"
    REFUND = "refund"
    REVERSAL = "reversal"
    CHARGEBACK = "chargeback"
    CARD_PAYMENT = "card_payment"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    FEE = "fee"
    INTEREST_DEBIT = "interest_debit"
    INTEREST_CREDIT = "interest_credit"
    CASH_WITHDRAWAL = "cash_withdrawal"
    CASH_DEPOSIT = "cash_deposit"
    UNKNOWN = "unknown"


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def date_delta(a: date, b: date) -> int:
    """Absolute day delta between two dates."""
    return abs((a - b).days)


@dataclass(frozen=True)
class DateMatchResult:
    """Result of matching an app transaction date against statement dates.

    Fields
    ------
    matched:
        True when the app date is compatible with the statement dates.
    matched_on:
        ``"transaction_date"``, ``"posted_date"``, ``"posted_date_window"``, or ``"none"``.
    day_delta:
        Absolute day difference (positive or zero). ``None`` when no
        comparison was possible.
    """

    matched: bool
    matched_on: str
    day_delta: int | None = None


def match_date_window(
    app_date: date,
    statement_transaction_date: date | None,
    statement_posted_date: date | None,
    *,
    date_tolerance_days: int = 3,
    posted_date_window_days: int = 3,
) -> DateMatchResult:
    """Determine whether an app transaction date can match statement dates.

    Matching rules, applied in priority order:

    1.  **transaction_date is available** (preferred):
        ``app_date`` must equal ``statement_transaction_date`` exactly for
        a match on ``"transaction_date"``.  If ``app_date`` differs from
        ``statement_transaction_date``, the result is ``matched=False`` —
        the posted_date is **never** used as a fallback when
        ``transaction_date`` is present, and ``date_tolerance_days`` is
        **not** applied to this comparison (it is preserved for backward
        compatibility with callers that pass it).

    2.  **only posted_date is available**:
        A match is valid only when ``app_date`` is on or before
        ``statement_posted_date``.  Three sub-cases:

        * ``app_date == posted_date`` → ``"posted_date"`` (exact)
        * ``app_date`` is ≤ ``posted_date_window_days`` *before* ``posted_date``
          → ``"posted_date_window"``
        * ``app_date`` is after ``posted_date`` → ``matched=False``

    3.  **neither date is available**:
        Returns ``matched=False`` with ``day_delta=None``.

    This is a pure deterministic helper.  It does not alter transactions,
    create final records, or mark anything reconciled.
    """
    if statement_transaction_date is not None:
        if app_date == statement_transaction_date:
            return DateMatchResult(matched=True, matched_on="transaction_date", day_delta=0)
        delta = abs((app_date - statement_transaction_date).days)
        return DateMatchResult(matched=False, matched_on="none", day_delta=delta)

    if statement_posted_date is not None:
        signed_delta = (statement_posted_date - app_date).days
        abs_delta = abs(signed_delta)
        if signed_delta < 0:
            return DateMatchResult(matched=False, matched_on="none", day_delta=abs_delta)
        if signed_delta == 0:
            return DateMatchResult(matched=True, matched_on="posted_date", day_delta=0)
        if signed_delta <= posted_date_window_days:
            return DateMatchResult(
                matched=True,
                matched_on="posted_date_window",
                day_delta=abs_delta,
            )
        return DateMatchResult(matched=False, matched_on="none", day_delta=abs_delta)

    return DateMatchResult(matched=False, matched_on="none", day_delta=None)


def amount_exact_match(statement_amount: Decimal, candidate_amount: Decimal) -> bool:
    """Exact Decimal comparison for amounts."""
    return statement_amount == candidate_amount


def currency_match(statement_currency: str | None, candidate_currency: str | None) -> bool:
    """Case-insensitive currency match.  Both must be non-None."""
    if statement_currency is None or candidate_currency is None:
        return False
    return statement_currency.strip().upper() == candidate_currency.strip().upper()


# ---------------------------------------------------------------------------
# Deterministic hash (for test 10 -- deterministic result)
# ---------------------------------------------------------------------------


def hash_result(result: MatchResult) -> str:
    """Stable SHA-256 digest over the MatchResult for verifying reproducibility.

    Uses a canonical JSON payload so the digest is stable across
    Python interpreter processes, unlike the built-in ``hash()``.
    """
    if result.decision_hash is not None:
        return result.decision_hash

    import hashlib
    import json

    payload: dict[str, str | list[str] | None] = {
        "status": result.status.value,
        "reasons": [r.value for r in result.reasons],
        "best_candidate_id": result.best_candidate.internal_id if result.best_candidate else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Review Queue + Resolution v1 domain types
# ---------------------------------------------------------------------------


class IssueType(str, Enum):
    """Issue type for a review queue item produced by the matching engine."""

    MATCHED = "matched"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    DATE_MISMATCH = "date_mismatch"
    MERCHANT_MISMATCH = "merchant_mismatch"
    MISSING_IN_APP = "missing_in_app"
    MISSING_IN_STATEMENT = "missing_in_statement"
    DATE_WINDOW_MATCH = "date_window_match"
    MERCHANT_VARIATION = "merchant_variation"
    LOW_CONFIDENCE_MATCH = "low_confidence_match"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    NEEDS_REVIEW = "needs_review"


class SuggestedAction(str, Enum):
    """Suggested action for a review queue item before human resolution."""

    CONFIRM_MATCH = "confirm_match"
    ADJUST_APP_TRANSACTION = "adjust_app_transaction"
    CREATE_MISSING_APP_TRANSACTION = "create_missing_app_transaction"
    MARK_STATEMENT_ONLY = "mark_statement_only"
    MARK_DUPLICATE = "mark_duplicate"
    IGNORE = "ignore"
    NEEDS_MORE_INFO = "needs_more_info"


class ReviewPriority(str, Enum):
    """Review queue priority tier for triaging reconciliation discrepancies.

    HIGH: requires immediate attention (amount mismatch, missing records,
    duplicates).
    MEDIUM: needs review but not blocking (date-window matches, merchant
    variations, low confidence).
    LOW: informational or for audit-trace purposes only.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Structured Reconciliation Review Evidence v1

# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationReviewEvidence:
    """Deterministic structured evidence for a single reconciliation review item.

    Derived from ``MatchEvidence``, ``ReconciliationCandidate``, and
    ``StatementTransaction`` -- never from AI output.

    All monetary fields use ``Decimal``. Dates use ``date | None``.
    Serialization is handled by ``to_dict()``.

    This boundary exists so reconciliation review/audit data can later be
    persisted safely without relying only on free-text ``evidence_summary``.
    """

    statement_reference: str | None = None
    statement_transaction_date: date | None = None
    statement_posted_date: date | None = None
    statement_merchant: str | None = None
    statement_amount: Decimal | None = None
    statement_currency: str | None = None
    app_transaction_id: str | None = None
    app_transaction_date: date | None = None
    app_merchant: str | None = None
    app_amount: Decimal | None = None
    app_currency: str | None = None
    amount_delta: Decimal | None = None
    date_delta_days: int | None = None
    merchant_similarity: float | None = None
    candidate_count: int = 0
    reason_codes: tuple[ReasonCode, ...] = field(default_factory=tuple)
    issue_type: IssueType = IssueType.NEEDS_REVIEW
    review_priority: ReviewPriority = ReviewPriority.LOW
    suggested_action: SuggestedAction = SuggestedAction.NEEDS_MORE_INFO
    confidence_score: Decimal = field(default=Decimal("0.0"))

    def to_dict(self) -> dict:
        """Serialize to a deterministic, JSON-compatible dict.

        - Dates -> ISO format strings.
        - Decimals -> strings (not floats) to preserve precision.
        - Reason codes -> their stable ``.value`` strings.
        - None values are preserved as ``None`` (JSON ``null``).
        - Field order is the dataclass field order.
        """
        return _structured_evidence_to_dict(self)


# ---------------------------------------------------------------------------


class ResolutionAction(str, Enum):
    """Resolution action taken by a human reviewer."""

    CONFIRM_MATCH = "confirm_match"
    ADJUST_APP_TRANSACTION = "adjust_app_transaction"
    CREATE_MISSING_APP_TRANSACTION = "create_missing_app_transaction"
    MARK_STATEMENT_ONLY = "mark_statement_only"
    MARK_DUPLICATE = "mark_duplicate"
    IGNORE = "ignore"
    NEEDS_MORE_INFO = "needs_more_info"


@dataclass(frozen=True)
class AppTransaction:
    """An app-side transaction used as a reconciliation candidate."""

    app_txn_id: str
    transaction_date: date
    merchant: str
    amount: Decimal
    currency: str
    source_type: str | None = None
    source_channel: str | None = None
    normalized_merchant: str | None = None
    posted_date: date | None = None
    transaction_type: "ReconciliationTransactionType | str | None" = None

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError(f"AppTransaction amount must be positive, got {self.amount}")


@dataclass(frozen=True)
class ReconciliationCandidate:
    """Output of the matching engine for one statement vs the app pool."""

    statement: StatementTransaction
    best_app_transaction: AppTransaction | None = None
    all_app_transactions: tuple[AppTransaction, ...] = field(default_factory=tuple)
    match_status: MatchStatus = MatchStatus.NO_MATCH
    reason_codes: tuple[ReasonCode, ...] = field(default_factory=tuple)
    issue_type: IssueType = IssueType.NEEDS_REVIEW
    confidence_score: Decimal = field(default=Decimal("0.0"))
    evidence: MatchEvidence | None = None
    candidate_id: str = ""
    # Enhanced classification fields
    review_priority: "ReviewPriority" = field(default_factory=lambda: ReviewPriority.LOW)
    is_review_required: bool = field(default=True)

    @property
    def is_matched(self) -> bool:
        return self.match_status == MatchStatus.MATCHED

    @property
    def needs_review(self) -> bool:
        return self.issue_type != IssueType.MATCHED


@dataclass(frozen=True)
class ReviewQueueItem:
    """A reconciliation candidate enriched for human review."""

    candidate: ReconciliationCandidate
    issue_type: IssueType
    suggested_action: SuggestedAction
    queue_item_id: str = ""
    reason_codes: tuple[ReasonCode, ...] = field(default_factory=tuple)
    evidence_summary: str = ""
    structured_evidence: ReconciliationReviewEvidence | None = None
    priority: int = 0


@dataclass(frozen=True)
class ResolutionDecision:
    """Human reviewer's decision on a single review queue item."""

    decision_id: str
    queue_item_id: str
    action: ResolutionAction
    note: str = ""
    reviewer: str = "human"
    resolved_at: str | None = None


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of applying a ResolutionDecision to a ReviewQueueItem."""

    result_id: str
    decision: ResolutionDecision
    queue_item: ReviewQueueItem
    success: bool
    audit_evidence: dict = field(default_factory=dict)
    error_message: str | None = None


@dataclass(frozen=True)
class ReconciliationSummary:
    """Full summary of a reconciliation review queue run."""

    total_statement_transactions: int
    total_app_transactions: int
    matched_count: int = 0
    needs_review_count: int = 0
    review_required_count: int = 0
    missing_in_app_count: int = 0
    missing_in_statement_count: int = 0
    amount_mismatch_count: int = 0
    currency_mismatch_count: int = 0
    possible_duplicate_count: int = 0
    matched_items: list[ReviewQueueItem] = field(default_factory=list)
    review_items: list[ReviewQueueItem] = field(default_factory=list)
    priority_items: list[ReviewQueueItem] = field(default_factory=list)
    # Priority tier counts
    high_priority_count: int = 0
    medium_priority_count: int = 0
    low_priority_count: int = 0
    # Grouped review items by priority tier
    high_priority_items: list[ReviewQueueItem] = field(default_factory=list)
    medium_priority_items: list[ReviewQueueItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolution Apply Runtime v1 domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionApplyResult:
    """Outcome of applying a ResolutionDecision through the apply runtime.

    The apply runtime is a controlled, audit-safe layer that converts
    human resolution decisions into explicit apply results. It never
    silently mutates final financial records.

    Field ``idempotent`` is True when this result represents a repeat
    of an already-applied decision (same decision_id, queue_item_id,
    action, and payload).
    """

    apply_id: str
    decision_id: str
    queue_item_id: str
    candidate_id: str
    action: ResolutionAction
    success: bool
    payload: dict = field(default_factory=dict)
    error_message: str | None = None
    audit_evidence: dict = field(default_factory=dict)
    applied_at: str = ""
    reviewer: str = "human"
    note: str = ""
    statement_reference: str | None = None
    app_transaction_reference: str | None = None
    idempotent: bool = False


class ApplyConflictError(Exception):
    """Raised when a resolution decision conflicts with an already-applied decision."""


class ApplyInstructionError(Exception):
    """Raised when building an ApplyInstruction fails validation.

    This is distinct from ApplyConflictError: ApplyInstructionError
    represents a structural/validation problem in the instruction
    itself (e.g. missing evidence reference, ambiguous candidate,
    unsupported action), while ApplyConflictError represents a
    runtime conflict with already-applied state.
    """


@dataclass(frozen=True)
class ApplyInstruction:
    """Typed, validated instruction bridging the review layer and apply runtime.

    This is the explicit boundary object. Review evidence bundles and
    ``ResolutionDecision`` objects must pass through
    ``build_apply_instruction()`` to produce a validated
    ``ApplyInstruction`` before the apply runtime can act on them.

    An ``ApplyInstruction`` carries:
    - The source review decision identity (decision_id, reviewer, note).
    - The target review queue item identity (queue_item_id, candidate_id).
    - The explicit action to take.
    - Evidence references for audit traceability.
    - Audit metadata for source traceability.

    The apply runtime's ``apply_instruction()`` method accepts only
    ``ApplyInstruction`` objects; raw ``ResolutionDecision`` and
    ``ReviewEvidenceBundle`` objects cannot be applied directly.
    """

    instruction_id: str
    decision_id: str
    queue_item_id: str
    candidate_id: str
    action: ResolutionAction
    reviewer: str = "human"
    note: str = ""
    resolved_at: str | None = None
    issue_type: IssueType = IssueType.NEEDS_REVIEW
    statement_ref: str | None = None
    app_transaction_ref: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    audit_metadata: dict = field(default_factory=dict)
    # The underlying ReviewQueueItem for apply execution
    _item: "ReviewQueueItem | None" = field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Batch Apply State Control v1 domain types
# ---------------------------------------------------------------------------


class BatchApplyState(str, Enum):
    """Explicit lifecycle state for a reconciliation apply batch.

    Each state is a stable identifier used to enforce safe state
    transitions and prevent silent re-application of terminal batches.
    """

    PENDING = "pending"
    """Batch is ready to be applied but has not started yet."""

    APPLYING = "applying"
    """Batch apply is in progress.  In-memory only (not persisted)."""

    APPLIED = "applied"
    """Batch was successfully applied.  Terminal state."""

    FAILED = "failed"
    """Batch apply failed.  Retryable."""

    REJECTED = "rejected"
    """Batch apply was rejected -- typically because the batch was
    already applied or was in an unsafe pre-apply state."""

    @property
    def is_terminal(self) -> bool:
        """Return True when this state is terminal and cannot transition further."""
        return self in (BatchApplyState.APPLIED, BatchApplyState.REJECTED)


@dataclass(frozen=True)
class BatchApplyResult:
    """Outcome of a batch apply operation with explicit state control.

    Wraps a collection of individual ResolutionApplyResult items
    and adds batch-level state tracking, audit metadata, and duplicate
    prevention signals.

    Fields
    ------
    batch_id:
        Stable identifier for this apply batch.
    state:
        The resulting batch state after the apply operation.
    results:
        Individual ResolutionApplyResult items produced by the apply.
        Empty when the batch was rejected before any items were processed.
    audit_metadata:
        Structured audit evidence for the batch-level state transition.
        Includes previous state, transition reason, item counts, and
        evidence reference tracking.
    error_message:
        Human-readable description when state is FAILED or REJECTED.
        None when the batch was applied successfully.
    batch_applied_at:
        ISO 8601 UTC timestamp of when the batch transitioned to its
        terminal state (APPLIED or FAILED).
    idempotent:
        True when this batch was already applied and the current
        call was a duplicate attempt.
    """

    batch_id: str
    state: BatchApplyState
    results: tuple["ResolutionApplyResult", ...] = field(default_factory=tuple)
    audit_metadata: dict = field(default_factory=dict)
    error_message: str | None = None
    batch_applied_at: str = ""
    idempotent: bool = False

    @property
    def success(self) -> bool:
        """Return True when the batch was successfully applied."""
        return self.state == BatchApplyState.APPLIED and not self.idempotent

    @property
    def is_terminal(self) -> bool:
        """Return True when the batch state is terminal."""
        return self.state.is_terminal

    @property
    def applied_count(self) -> int:
        """Number of individual items successfully applied."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        """Number of individual items that failed apply."""
        return sum(1 for r in self.results if not r.success)


# ---------------------------------------------------------------------------
# Issue-type to suggested-action mapping (deterministic)
# ---------------------------------------------------------------------------

_ISSUE_TO_SUGGESTED: dict[IssueType, SuggestedAction] = {
    IssueType.MATCHED: SuggestedAction.CONFIRM_MATCH,
    IssueType.AMOUNT_MISMATCH: SuggestedAction.ADJUST_APP_TRANSACTION,
    IssueType.CURRENCY_MISMATCH: SuggestedAction.ADJUST_APP_TRANSACTION,
    IssueType.DATE_MISMATCH: SuggestedAction.ADJUST_APP_TRANSACTION,
    IssueType.DATE_WINDOW_MATCH: SuggestedAction.ADJUST_APP_TRANSACTION,
    IssueType.MERCHANT_MISMATCH: SuggestedAction.NEEDS_MORE_INFO,
    IssueType.MERCHANT_VARIATION: SuggestedAction.NEEDS_MORE_INFO,
    IssueType.MISSING_IN_APP: SuggestedAction.CREATE_MISSING_APP_TRANSACTION,
    IssueType.MISSING_IN_STATEMENT: SuggestedAction.MARK_STATEMENT_ONLY,
    IssueType.POSSIBLE_DUPLICATE: SuggestedAction.MARK_DUPLICATE,
    IssueType.NEEDS_REVIEW: SuggestedAction.NEEDS_MORE_INFO,
    IssueType.LOW_CONFIDENCE_MATCH: SuggestedAction.NEEDS_MORE_INFO,
}

_ISSUE_PRIORITY: dict[IssueType, int] = {
    IssueType.AMOUNT_MISMATCH: 0,
    IssueType.CURRENCY_MISMATCH: 1,
    IssueType.POSSIBLE_DUPLICATE: 2,
    IssueType.DATE_MISMATCH: 3,
    IssueType.MISSING_IN_APP: 4,
    IssueType.MERCHANT_MISMATCH: 5,
    IssueType.MISSING_IN_STATEMENT: 6,
    IssueType.NEEDS_REVIEW: 7,
    IssueType.DATE_WINDOW_MATCH: 8,
    IssueType.MERCHANT_VARIATION: 9,
    IssueType.LOW_CONFIDENCE_MATCH: 10,
    IssueType.MATCHED: 99,
}

_ACTION_TO_SUPPORTED_ISSUE_TYPES: dict[ResolutionAction, tuple[IssueType, ...]] = {
    ResolutionAction.CONFIRM_MATCH: (IssueType.MATCHED, IssueType.NEEDS_REVIEW),
    ResolutionAction.ADJUST_APP_TRANSACTION: (
        IssueType.AMOUNT_MISMATCH,
        IssueType.CURRENCY_MISMATCH,
        IssueType.DATE_MISMATCH,
        IssueType.DATE_WINDOW_MATCH,
        IssueType.MERCHANT_MISMATCH,
        IssueType.MERCHANT_VARIATION,
    ),
    ResolutionAction.CREATE_MISSING_APP_TRANSACTION: (IssueType.MISSING_IN_APP,),
    ResolutionAction.MARK_STATEMENT_ONLY: (
        IssueType.MISSING_IN_STATEMENT,
        IssueType.NEEDS_REVIEW,
    ),
    ResolutionAction.MARK_DUPLICATE: (IssueType.POSSIBLE_DUPLICATE,),
    ResolutionAction.IGNORE: (),
    ResolutionAction.NEEDS_MORE_INFO: (),
}


def review_priority_for_issue(issue_type: IssueType) -> ReviewPriority:
    """Map an issue type to the deterministic review priority tier.

    HIGH:   amount_mismatch, missing_in_app, possible_duplicate
    MEDIUM: date_mismatch, date_window_match, merchant_variation,
            merchant_mismatch, missing_in_statement, needs_review
    LOW:    matched (informational, kept out of review queue)
    """
    high_issues: set[IssueType] = {
        IssueType.AMOUNT_MISMATCH,
        IssueType.CURRENCY_MISMATCH,
        IssueType.MISSING_IN_APP,
        IssueType.POSSIBLE_DUPLICATE,
    }
    medium_issues: set[IssueType] = {
        IssueType.DATE_MISMATCH,
        IssueType.DATE_WINDOW_MATCH,
        IssueType.MERCHANT_VARIATION,
        IssueType.MERCHANT_MISMATCH,
        IssueType.MISSING_IN_STATEMENT,
        IssueType.NEEDS_REVIEW,
        IssueType.LOW_CONFIDENCE_MATCH,
    }
    if issue_type in high_issues:
        return ReviewPriority.HIGH
    if issue_type in medium_issues:
        return ReviewPriority.MEDIUM
    return ReviewPriority.LOW


def priority_score_for_issue(issue_type: IssueType) -> int:
    """Return a numeric priority score (0 = highest) for deterministic
    sorting of review items."""
    return _ISSUE_PRIORITY.get(issue_type, 99)


def sort_key_for_review_item(item: "ReviewQueueItem") -> tuple[int, str, str, str]:
    """Deterministic sort key for review items: (priority, date, merchant, amount)."""
    stmt = item.candidate.statement
    priority = priority_score_for_issue(item.issue_type)
    date_key = ""
    if stmt.transaction_date is not None:
        date_key = stmt.transaction_date.isoformat()
    merchant_key = stmt.merchant_raw.lower()
    amount_key = str(stmt.amount) if stmt.amount is not None else ""
    return (priority, date_key, merchant_key, amount_key)


def suggested_action_for_issue(issue_type: IssueType) -> SuggestedAction:
    """Return the deterministic suggested action for an issue type."""
    return _ISSUE_TO_SUGGESTED.get(issue_type, SuggestedAction.NEEDS_MORE_INFO)


def priority_for_issue(issue_type: IssueType) -> int:
    """Return the review priority for an issue type (0 = highest)."""
    return _ISSUE_PRIORITY.get(issue_type, 99)


def is_resolution_compatible(action: ResolutionAction, issue_type: IssueType) -> bool:
    """Return True when *action* is compatible with *issue_type*."""
    supported = _ACTION_TO_SUPPORTED_ISSUE_TYPES.get(action, ())
    if not supported:
        return True
    return issue_type in supported


def validate_resolution_decision(
    action: ResolutionAction,
    issue_type: IssueType,
) -> tuple[bool, str | None]:
    """Validate that a resolution action is compatible with an issue type."""
    if is_resolution_compatible(action, issue_type):
        return True, None
    supported_types = _ACTION_TO_SUPPORTED_ISSUE_TYPES.get(action, ())
    supported_str = ", ".join(t.value for t in supported_types)
    return (
        False,
        f"Action '{action.value}' is not compatible with issue type "
        f"'{issue_type.value}'. Supported issue types: {supported_str}",
    )


# ---------------------------------------------------------------------------
# Structured evidence helpers
# ---------------------------------------------------------------------------


def _structured_evidence_to_dict(evidence: "ReconciliationReviewEvidence") -> dict:
    """Internal serialization helper -- deterministic dict builder.

    Field order matches the dataclass field order and is stable across
    Python interpreter processes.

    Uses ``dataclasses.fields()`` to iterate in declaration order, then
    applies type-specific serialization rules per field.
    """
    import dataclasses

    result: dict = {}

    for f in dataclasses.fields(evidence):
        val = getattr(evidence, f.name)

        if isinstance(val, date):
            result[f.name] = val.isoformat()
        elif isinstance(val, Decimal):
            result[f.name] = str(val)
        elif isinstance(val, tuple) and f.name == "reason_codes":
            result[f.name] = [r.value for r in val]
        elif hasattr(val, "value"):
            # Enum fields
            result[f.name] = val.value
        elif isinstance(val, float):
            result[f.name] = val
        else:
            result[f.name] = val

    return result


# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "MatchStatus",
    "ReasonCode",
    "StatementTransaction",
    "InternalCandidate",
    "MatchEvidence",
    "MatchResult",
    "normalize_merchant",
    "merchant_similarity",
    "date_delta",
    "DateMatchResult",
    "match_date_window",
    "amount_exact_match",
    "currency_match",
    "hash_result",
    # Review Queue + Resolution v1
    "IssueType",
    "SuggestedAction",
    "ResolutionAction",
    "ReviewPriority",
    "AppTransaction",
    "ReconciliationCandidate",
    "ReviewQueueItem",
    "ResolutionDecision",
    # Resolution Apply Runtime v1
    "ResolutionApplyResult",
    "ApplyConflictError",
    "ApplyInstructionError",
    "ResolutionResult",
    "StatementAmountDirection",
    "ReconciliationSummary",
    "suggested_action_for_issue",
    "priority_for_issue",
    "review_priority_for_issue",
    "priority_score_for_issue",
    "sort_key_for_review_item",
    "is_resolution_compatible",
    "validate_resolution_decision",
    "ReconciliationReviewEvidence",
    # Batch Apply State Control v1
    "BatchApplyState",
    "BatchApplyResult",
    # Guarded Apply Runtime v1
    "ApplyExecutionStatus",
    "GuardedOperationResult",
    "GuardedApplyExecutionResult",
]

# ---------------------------------------------------------------------------
# Guarded Reconciliation Apply Runtime v1 -- execution result types
# ---------------------------------------------------------------------------


class ApplyExecutionStatus(str, Enum):
    """Stable execution status codes for the guarded apply runtime."""

    EXECUTED = "executed"
    BLOCKED = "blocked"
    PARTIALLY_BLOCKED = "partially_blocked"
    UNSUPPORTED = "unsupported"
    # IDEMPOTENT_REPLAY reserved for future persistent runtime
    CONFLICT = "conflict"


@dataclass(frozen=True)
class GuardedOperationResult:
    """Per-operation execution outcome from the guarded apply runtime."""

    operation_id: str
    decision_id: str
    execution_status: ApplyExecutionStatus
    reason: str = ""
    guard_decision_approved: bool = False
    guard_decision_idempotency_key: str | None = None
    guard_blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    mutation_type: str = ""
    mutation_payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardedApplyExecutionResult:
    """Result of executing a reconciliation apply plan through the guarded apply runtime."""

    plan_id: str
    idempotency_key: str
    execution_status: ApplyExecutionStatus
    results: tuple[GuardedOperationResult, ...]
    total_operations: int = 0
    operated_executed: int = 0
    operated_blocked: int = 0
    operated_skipped: int = 0
    block_reason: str = ""
    guard_decision_refs: tuple[str, ...] = field(default_factory=tuple)
    executed_at: str = ""
    audit_trail: dict[str, object] = field(default_factory=dict)
    is_dry_run: bool = True
