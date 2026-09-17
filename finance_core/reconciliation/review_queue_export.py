"""Reconciliation Review Queue Export v1 -- read-only, export-friendly
projection of existing guarded apply review summary / review queue data into a
deterministic structure for future Metabase, Telegram, or CLI reporting.

This is a pure projection layer. It converts existing review summary and review
queue objects into a frozen, export-friendly structure. It never applies
reconciliation results, mutates final transactions, generates settlement
obligations, modifies live database data, or alters existing migrations.

Key invariants:
- **Read-only**: never applies, never mutates final transactions, never
  generates settlement obligations, never opens or writes ``database/finance.db``.
- **Deterministic**: same inputs always produce the same export. Sorting and
  counts are fully derived from the supplied objects.
- **Type-guarded input boundary**: the builder accepts existing review summary /
  review queue objects only. Raw dicts or unvalidated inputs are rejected.
- **Decimal safety**: monetary ``Decimal`` values are preserved as ``Decimal``
  and never converted through ``float``.
- **Injectable timestamp**: ``generated_at`` is caller-supplied (defaults to an
  empty string) so tests can make output fully deterministic.

Non-goals (explicitly out of scope):
- No apply execution, no final transaction mutation, no settlement obligation
  generation.
- No live database changes, no migration changes.
- No Telegram / OCR / PDF / Metabase runtime interaction.
- No AI model calls or AI-as-authority judgments.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Protocol, Union

from finance_core.reconciliation.apply_execution_review import (
    GuardedApplyExecutionReviewSummary,
)
from finance_core.reconciliation.models import (
    IssueType,
    MatchStatus,
    ReviewPriority,
    ReviewQueueItem,
)

# ---------------------------------------------------------------------------
# Supported review-summary source inputs
# ---------------------------------------------------------------------------

# Each input the builder accepts is one of the existing guarded apply / review
# summary structures in the project. ``ReviewQueueEntry`` is imported lazily
# inside the adapter to avoid an import cycle with the review queue module.


@dataclass(frozen=True)
class ReviewQueueExportItem:
    """A single read-only, export-friendly review queue row.

    All fields are derived from the supplied review summary / review queue
    object; none are random, time-dependent, or AI-generated. Monetary fields
    stay as ``Decimal`` (never ``float``). Dates stay as ``date | None``.

    Fields
    ------
    review_id:
        Stable identifier for the review row. For a guarded apply review
        summary this is the ``idempotency_key``; for a review queue item this
        is its ``queue_item_id`` (falling back to the candidate id).
    priority:
        Review priority tier (``ReviewPriority``).
    status:
        Stable status string. For guarded apply summaries this is the
        ``ApplyExecutionStatus`` value; for review queue items this is the
        ``MatchStatus`` value.
    requires_human_review:
        True when the source flags the row for human review.
    reason_codes:
        Stable reason-code strings explaining the review outcome, in
        deterministic order.
    evidence_refs:
        Stable evidence reference strings (e.g. guard decision refs or
        evidence public ids), in deterministic order.
    statement_transaction_id:
        Statement transaction identifier, if available from the source.
    app_transaction_id:
        App transaction identifier, if available from the source.
    internal_candidate_id:
        Internal candidate identifier, if available from the source.
    amount_delta:
        Statement-vs-candidate amount delta as ``Decimal``, if available from
        the source. Never converted through ``float``.
    merchant:
        Merchant name, if available from the source.
    transaction_date:
        Transaction date, if available from the source.
    amount:
        Monetary amount as ``Decimal``, if available from the source. Never
        converted through ``float``.
    currency:
        Currency code, if available from the source.
    source_type:
        Stable label identifying which input type this item was built from
        (e.g. ``"guarded_apply_review_summary"`` or ``"review_queue_item"``).
    """

    review_id: str
    priority: ReviewPriority
    status: str
    requires_human_review: bool
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    statement_transaction_id: str | None = None
    app_transaction_id: str | None = None
    internal_candidate_id: str | None = None
    amount_delta: Decimal | None = None
    merchant: str | None = None
    transaction_date: date | None = None
    amount: Decimal | None = None
    currency: str | None = None
    source_type: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to a deterministic, JSON-compatible dict.

        Dates -> ISO format strings. Decimals -> strings (not floats) to
        preserve precision. Enums -> their stable ``.value`` strings. Field
        order is the dataclass field order.
        """
        return _export_item_to_dict(self)


@dataclass(frozen=True)
class ReviewQueueExport:
    """Deterministic, read-only export of a reconciliation review queue run.

    Built by ``build_review_queue_export()``. Items are sorted deterministically
    (highest priority first, then status, then transaction date, then
    ``review_id``). Summary counts are derived from the sorted items.

    Fields
    ------
    generated_at:
        Caller-supplied timestamp string. Defaults to an empty string so
        tests can make output fully deterministic without a clock.
    total_items:
        Total number of exported review items.
    by_priority:
        Mapping of ``ReviewPriority`` value -> count, derived from items.
        Always contains every priority tier (zero when absent).
    by_status:
        Mapping of status string -> count, derived from items.
    items:
        Frozen tuple of sorted ``ReviewQueueExportItem`` rows.
    """

    generated_at: str
    total_items: int
    by_priority: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    items: tuple[ReviewQueueExportItem, ...] = field(default_factory=tuple)


# Union of accepted input object types. Kept as a typing alias for clarity.
ReviewSummaryInput = Union[
    GuardedApplyExecutionReviewSummary,
    ReviewQueueItem,
    "ReviewQueueEntryLike",
]


class ReviewQueueEntryLike(Protocol):
    """Structural type for the review queue ``ReviewQueueEntry`` object.

    ``ReviewQueueEntry`` lives in ``finance_core.reconciliation.review_queue`` and is a
    thin persisted view: it carries ``public_id``, ``match_status`` (a stable
    string), ``reason_codes`` (strings), ``evidence`` (dict), the internal
    candidate id, an ``amount_delta`` Decimal, and a ``merchant_similarity``
    float. It does **not** carry merchant/date/amount/currency/app transaction
    id directly -- those richer fields are available via the ``ReviewQueueItem``
    path instead. Matching the entry via a Protocol avoids a hard import (and a
    potential import cycle) while still allowing real ``ReviewQueueEntry``
    instances to be accepted.

    The structural fallback in ``_looks_like_review_queue_entry`` requires the
    minimal attribute set the adapter actually reads (``public_id``,
    ``match_status``, ``reason_codes``, ``evidence``, ``internal_candidate_id``,
    ``amount_delta``). Objects missing any of these are rejected with
    ``TypeError`` so near-miss lookalikes are not silently projected.
    """

    public_id: str
    match_status: str
    reason_codes: list[str]
    evidence: dict[str, object]
    internal_candidate_id: str | None
    amount_delta: Decimal | None
    merchant_similarity: float | None
    # ``statement_transaction_id`` is present on the concrete
    # ``ReviewQueueEntry`` but read defensively by the adapter (a minimal
    # lookalike may omit it), so it is not part of the required-attrs set used
    # by ``_looks_like_review_queue_entry``.
    statement_transaction_id: int | None


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_review_queue_export(
    sources: Iterable[ReviewSummaryInput],
    *,
    generated_at: str = "",
) -> ReviewQueueExport:
    """Build a deterministic, read-only review queue export from existing
    guarded apply review summary / review queue objects.

    The builder is a pure projection: it reads the supplied objects and
    produces a frozen export. It never applies reconciliation results, mutates
    final transactions, generates settlement obligations, opens or writes
    ``database/finance.db``, or alters migrations.

    Parameters
    ----------
    sources:
        Iterable of existing review summary / review queue objects. Each must
        be a ``GuardedApplyExecutionReviewSummary``, a ``ReviewQueueItem``, or
        a ``ReviewQueueEntry``-like object. Raw dicts and other unvalidated
        inputs are rejected.
    generated_at:
        Optional caller-supplied timestamp string included verbatim in the
        export. Defaults to ``""`` so tests can produce deterministic output
        without a clock. No timestamp is generated internally.

    Returns
    -------
    ReviewQueueExport
        Frozen export with deterministically sorted items and derived summary
        counts. Returns an empty export (zero items, zero counts) for empty
        input.
    """
    items = [_adapt_source(src) for src in sources]
    items.sort(key=_export_sort_key)

    by_priority = _count_by_priority(items)
    by_status = _count_by_status(items)

    return ReviewQueueExport(
        generated_at=generated_at,
        total_items=len(items),
        by_priority=by_priority,
        by_status=by_status,
        items=tuple(items),
    )


# ---------------------------------------------------------------------------
# Per-source adapters -- read-only, deterministic
# ---------------------------------------------------------------------------


def _adapt_source(source: ReviewSummaryInput) -> ReviewQueueExportItem:
    """Project a single supported source object into an export item.

    Dispatches on the concrete type. Unsupported inputs raise ``TypeError``
    so the read-only/type-guarded boundary is explicit.
    """
    if isinstance(source, GuardedApplyExecutionReviewSummary):
        return _from_guarded_apply_review_summary(source)
    if isinstance(source, ReviewQueueItem):
        return _from_review_queue_item(source)
    # Fall back to the ReviewQueueEntry-like structural protocol.
    if _looks_like_review_queue_entry(source):
        return _from_review_queue_entry(source)
    raise TypeError(
        "build_review_queue_export accepts GuardedApplyExecutionReviewSummary, "
        "ReviewQueueItem, or ReviewQueueEntry-like objects; got "
        f"{type(source).__name__}"
    )


def _from_guarded_apply_review_summary(
    summary: GuardedApplyExecutionReviewSummary,
) -> ReviewQueueExportItem:
    """Project a guarded apply execution review summary into an export item.

    The summary carries priority, status, human-review flag, block reason, and
    guard decision refs, but not merchant/date/amount/currency. Reason codes
    are derived from the non-empty ``block_reason`` plus the guard decision
    refs so reviewers can trace why a row was flagged.
    """
    reason_codes: list[str] = []
    if summary.block_reason:
        reason_codes.append(summary.block_reason)
    # Guard decision refs are evidence references for guarded apply summaries.
    evidence_refs = tuple(summary.guard_decision_refs)

    return ReviewQueueExportItem(
        review_id=summary.idempotency_key,
        priority=summary.review_priority,
        status=summary.execution_status.value,
        requires_human_review=summary.requires_human_review,
        reason_codes=tuple(reason_codes),
        evidence_refs=evidence_refs,
        # Merchant/date/amount/currency are not carried by the summary.
        statement_transaction_id=None,
        app_transaction_id=None,
        merchant=None,
        transaction_date=None,
        amount=None,
        currency=None,
        source_type="guarded_apply_review_summary",
    )


def _from_review_queue_item(item: ReviewQueueItem) -> ReviewQueueExportItem:
    """Project a review queue item (candidate-enriched) into an export item.

    Review queue items carry the candidate's statement merchant/date/amount/
    currency and the app transaction id, plus structured evidence when present.
    """
    candidate = item.candidate
    statement = candidate.statement
    app_txn = candidate.best_app_transaction

    merchant = statement.merchant_raw if statement.merchant_raw else None
    transaction_date = statement.transaction_date
    amount = statement.amount
    currency = statement.currency
    statement_transaction_id = statement.statement_row_reference
    app_transaction_id = app_txn.app_txn_id if app_txn is not None else None

    # Reason codes: structured evidence reason codes win when present,
    # otherwise fall back to the item's reason codes, then candidate's.
    if item.structured_evidence is not None:
        reason_codes = tuple(rc.value for rc in item.structured_evidence.reason_codes)
    else:
        reason_codes = tuple(rc.value for rc in item.reason_codes) or tuple(
            rc.value for rc in candidate.reason_codes
        )

    # Evidence references: structured evidence statement/app refs plus the
    # app transaction id, kept deterministic and de-duplicated.
    evidence_refs: list[str] = []
    if item.structured_evidence is not None:
        ev = item.structured_evidence
        if ev.statement_reference is not None:
            evidence_refs.append(ev.statement_reference)
        if ev.app_transaction_id is not None:
            evidence_refs.append(ev.app_transaction_id)
    if app_transaction_id is not None:
        evidence_refs.append(app_txn_id_ref(app_transaction_id))
    evidence_refs = _dedup_preserve_order(evidence_refs)

    priority = _priority_for_review_queue_item(item)
    status = candidate.match_status.value
    requires_human_review = item.candidate.needs_review

    return ReviewQueueExportItem(
        review_id=_review_id_for_item(item),
        priority=priority,
        status=status,
        requires_human_review=requires_human_review,
        reason_codes=reason_codes,
        evidence_refs=tuple(evidence_refs),
        statement_transaction_id=statement_transaction_id,
        app_transaction_id=app_transaction_id,
        merchant=merchant,
        transaction_date=transaction_date,
        amount=amount,
        currency=currency,
        source_type="review_queue_item",
    )


def _from_review_queue_entry(entry: ReviewQueueEntryLike) -> ReviewQueueExportItem:
    """Project a review queue entry (persisted view) into an export item.

    ``ReviewQueueEntry`` is a thin persisted view: it carries the match status,
    reason codes, evidence dict, internal candidate id, amount delta, and
    merchant similarity. Merchant/date/amount/currency/app transaction id are
    not present on the entry itself, so those fields are left ``None`` here --
    callers that need the richer fields should feed ``ReviewQueueItem`` objects
    instead. ``requires_human_review`` is derived from the match status.
    """
    status_str = entry.match_status
    reason_codes = tuple(entry.reason_codes)
    priority = _priority_for_status_str(status_str)
    requires_review = _status_requires_review(status_str)

    # Evidence references: keys of the evidence dict (deterministic, sorted)
    # plus the internal candidate id when present.
    evidence_refs: list[str] = []
    evidence = entry.evidence
    if isinstance(evidence, dict):
        evidence_refs.extend(sorted(str(k) for k in evidence))
    internal_candidate_id = entry.internal_candidate_id
    if internal_candidate_id:
        evidence_refs.append(internal_candidate_id)
    evidence_refs = _dedup_preserve_order(evidence_refs)

    statement_transaction_id = (
        str(entry.statement_transaction_id) if entry.statement_transaction_id is not None else None
    )

    return ReviewQueueExportItem(
        review_id=entry.public_id,
        priority=priority,
        status=status_str,
        requires_human_review=requires_review,
        reason_codes=reason_codes,
        evidence_refs=tuple(evidence_refs),
        statement_transaction_id=statement_transaction_id,
        app_transaction_id=None,
        internal_candidate_id=internal_candidate_id,
        amount_delta=entry.amount_delta,
        merchant=None,
        transaction_date=None,
        amount=None,
        currency=None,
        source_type="review_queue_entry",
    )


# ---------------------------------------------------------------------------
# Deterministic sorting + counting
# ---------------------------------------------------------------------------


def _export_sort_key(item: ReviewQueueExportItem) -> tuple[int, str, int, str, str]:
    """Deterministic sort key for export items.

    Order:
    1. Highest priority first (HIGH < MEDIUM < LOW).
    2. Then status ascending (stable string order).
    3. Then transaction date ascending (ISO string); missing dates sort last
       within the same priority/status group so dated items stay more
       immediately actionable.
    4. Then ``review_id`` ascending as the final stable tie-breaker.
    """
    missing_date_rank = 1 if item.transaction_date is None else 0
    return (
        _review_priority_rank(item.priority),
        item.status,
        missing_date_rank,
        item.transaction_date.isoformat() if item.transaction_date is not None else "",
        item.review_id,
    )


def _count_by_priority(items: list[ReviewQueueExportItem]) -> dict[str, int]:
    """Count items per priority tier. Every tier is always present (zero when
    absent) so downstream consumers get a stable shape."""
    counts: Counter[str] = Counter()
    for item in items:
        counts[item.priority.value] += 1
    return {tier.value: counts.get(tier.value, 0) for tier in ReviewPriority}


def _count_by_status(items: list[ReviewQueueExportItem]) -> dict[str, int]:
    """Count items per status string, sorted by status for determinism."""
    counts: Counter[str] = Counter(item.status for item in items)
    return {status: counts[status] for status in sorted(counts)}


# ---------------------------------------------------------------------------
# Priority derivation helpers
# ---------------------------------------------------------------------------


def _priority_for_review_queue_item(item: ReviewQueueItem) -> ReviewPriority:
    """Derive a deterministic review priority for a review queue item.

    Prefers the candidate's explicit ``review_priority`` tier; otherwise maps
    the item's ``issue_type`` through the project's deterministic mapping.
    """
    candidate = item.candidate
    if candidate.review_priority is not None:
        return candidate.review_priority
    return _review_priority_for_issue(item.issue_type)


def _priority_for_status(status: MatchStatus) -> ReviewPriority:
    """Derive a deterministic review priority from a match status enum.

    Matches the project's existing review-priority semantics: needs-review
    statuses (needs_review, ambiguous, possible_duplicate, amount/currency/
    date/merchant mismatch, no_match) are HIGH; matched is LOW.
    """
    if status in {
        MatchStatus.NEEDS_REVIEW,
        MatchStatus.AMBIGUOUS,
        MatchStatus.POSSIBLE_DUPLICATE,
        MatchStatus.AMOUNT_MISMATCH,
        MatchStatus.CURRENCY_MISMATCH,
        MatchStatus.DATE_MISMATCH,
        MatchStatus.MERCHANT_MISMATCH,
        MatchStatus.NO_MATCH,
    }:
        return ReviewPriority.HIGH
    return ReviewPriority.LOW


def _priority_for_status_str(status_str: str) -> ReviewPriority:
    """Derive a deterministic review priority from a status string.

    Used for persisted review queue entries whose ``match_status`` is stored
    as a string. Unknown statuses default to MEDIUM so they surface for
    review rather than being silently buried.
    """
    try:
        return _priority_for_status(MatchStatus(status_str))
    except ValueError:
        return ReviewPriority.MEDIUM


def _status_requires_review(status_str: str) -> bool:
    """Return True when a status string represents a needs-review outcome.

    Matches the ``needs_review`` semantics on ``MatchResult`` for known
    statuses; unknown statuses are treated as requiring review.
    """
    try:
        status = MatchStatus(status_str)
    except ValueError:
        return True
    return status in {
        MatchStatus.NEEDS_REVIEW,
        MatchStatus.AMBIGUOUS,
        MatchStatus.POSSIBLE_DUPLICATE,
        MatchStatus.AMOUNT_MISMATCH,
        MatchStatus.CURRENCY_MISMATCH,
        MatchStatus.DATE_MISMATCH,
        MatchStatus.MERCHANT_MISMATCH,
        MatchStatus.NO_MATCH,
    }


def _review_priority_for_issue(issue_type: IssueType) -> ReviewPriority:
    """Thin wrapper around the project's deterministic issue->priority mapping.

    Imported lazily to keep this module's top-level imports minimal.
    """
    from finance_core.reconciliation.models import review_priority_for_issue

    return review_priority_for_issue(issue_type)


def _review_priority_rank(priority: ReviewPriority) -> int:
    """Sort HIGH before MEDIUM before LOW."""
    if priority == ReviewPriority.HIGH:
        return 0
    if priority == ReviewPriority.MEDIUM:
        return 1
    return 2


def _review_id_for_item(item: ReviewQueueItem) -> str:
    """Pick a stable review id for a review queue item."""
    if item.queue_item_id:
        return item.queue_item_id
    if item.candidate.candidate_id:
        return item.candidate.candidate_id
    return ""


def app_txn_id_ref(app_transaction_id: str) -> str:
    """Format an app transaction id as a stable evidence reference string."""
    return f"app_txn:{app_transaction_id}"


# Minimal attribute set the ``_from_review_queue_entry`` adapter actually
# reads. Requiring all of these (rather than just ``public_id`` /
# ``match_status`` / ``reason_codes``) keeps the structural fallback tight so
# near-miss lookalike objects are rejected at the type-guarded boundary instead
# of being silently accepted and projected with missing fields.
_REVIEW_QUEUE_ENTRY_REQUIRED_ATTRS: tuple[str, ...] = (
    "public_id",
    "match_status",
    "reason_codes",
    "evidence",
    "internal_candidate_id",
    "amount_delta",
)


def _looks_like_review_queue_entry(obj: object) -> bool:
    """Best-effort structural check for a ``ReviewQueueEntry``-like object.

    Avoids importing the concrete class (which would risk an import cycle) by
    checking for the minimal attribute set the adapter actually reads. This is
    intentionally stricter than matching on ``public_id`` / ``match_status`` /
    ``reason_codes`` alone: an object that is missing ``evidence``,
    ``internal_candidate_id``, or ``amount_delta`` is not a safe stand-in for a
    ``ReviewQueueEntry`` and is rejected so the caller sees a clear
    ``TypeError`` rather than a silently partial projection.
    """
    return all(hasattr(obj, attr) for attr in _REVIEW_QUEUE_ENTRY_REQUIRED_ATTRS)


def _dedup_preserve_order(values: list[str]) -> list[str]:
    """De-duplicate strings while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


# ---------------------------------------------------------------------------
# Serialization helper -- deterministic, Decimal-safe
# ---------------------------------------------------------------------------


def _export_item_to_dict(item: ReviewQueueExportItem) -> dict[str, object]:
    """Serialize an export item to a deterministic, JSON-compatible dict.

    Dates -> ISO format strings. Decimals -> strings (not floats) to preserve
    precision. Enums -> their stable ``.value`` strings. Field order is the
    dataclass field order.
    """
    import dataclasses

    result: dict[str, object] = {}
    for f in dataclasses.fields(item):
        value = getattr(item, f.name)
        if isinstance(value, date):
            result[f.name] = value.isoformat()
        elif isinstance(value, Decimal):
            result[f.name] = str(value)
        elif hasattr(value, "value") and isinstance(value, ReviewPriority):
            result[f.name] = value.value
        else:
            result[f.name] = value
    return result


__all__ = [
    "ReviewQueueExport",
    "ReviewQueueExportItem",
    "ReviewQueueEntryLike",
    "build_review_queue_export",
]
