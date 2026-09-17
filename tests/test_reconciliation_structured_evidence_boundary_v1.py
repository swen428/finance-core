"""Tests for Reconciliation Structured Evidence Boundary v1.

Covers deterministic structured evidence model, serialization rules,
review queue integration, and evidence_summary compatibility.

Tests:
 1. Structured evidence exists for amount mismatch review item
 2. Monetary fields are Decimal internally or serialized as strings
 3. Date fields serialize as ISO strings
 4. Reason codes serialize as stable value strings
 5. issue_type and review_priority are included
 6. Missing app transaction evidence handled safely (None values)
 7. Exact matched item has structured evidence but is not review_required
 8. evidence_summary remains present and human-readable
 9. Serialization is deterministic
10. No float artefacts in serialized amount/delta values
11. Currency fields carried through correctly
12. Structured evidence for MISSING_IN_STATEMENT case
13. to_dict omits internal-only types (no MatchEvidence or dataclass refs)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    ReconciliationReviewEvidence,
    ReviewPriority,
    StatementAmountDirection,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.review_queue import (
    build_structured_evidence,
    generate_review_queue,
)

# -- Helpers -----------------------------------------------------------------


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=date(2024, 12, 1),
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="stmt-001",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw) -> AppTransaction:
    defaults: dict = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


# ---------------------------------------------------------------------------
# 1. Structured evidence exists for amount mismatch review item
# ---------------------------------------------------------------------------


def test_structured_evidence_exists_for_amount_mismatch():
    """Amount mismatch items carry deterministic structured evidence."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        transaction_date=date(2024, 12, 5),
        statement_row_reference="s-amt",
    )
    app = _app(
        "app-g",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1

    se = am_items[0].structured_evidence
    assert se is not None, "structured_evidence must not be None for review items"
    assert isinstance(se, ReconciliationReviewEvidence)

    # Key fields should be present
    assert se.statement_amount == Decimal("18.80")
    assert se.app_amount == Decimal("18.20")
    assert se.amount_delta == Decimal("0.60")


# ---------------------------------------------------------------------------
# 2. Monetary fields serialized as strings
# ---------------------------------------------------------------------------


def test_monetary_fields_serialized_as_strings():
    """Decimal fields must serialize as strings, never floats."""
    stmt = _stmt(
        merchant_raw="Shopee",
        amount=Decimal("55.75"),
        statement_row_reference="s-dec",
    )
    app = _app(
        "app-shopee",
        merchant="Shopee",
        amount=Decimal("55.70"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1
    d = am_items[0].structured_evidence.to_dict()

    for key in ("statement_amount", "app_amount", "amount_delta", "confidence_score"):
        assert isinstance(d[key], str), f"{key} must be str, got {type(d[key])}: {d[key]!r}"

    assert d["statement_amount"] == "55.75"
    assert d["app_amount"] == "55.70"
    assert d["amount_delta"] == "0.05"
    assert d["confidence_score"] == "0.0"


# ---------------------------------------------------------------------------
# 3. Date fields serialize as ISO strings
# ---------------------------------------------------------------------------


def test_date_fields_serialize_as_iso_strings():
    """Date fields must serialize as ISO format strings."""
    stmt = _stmt(
        merchant_raw="Netflix",
        amount=Decimal("19.90"),
        transaction_date=date(2024, 11, 15),
        posted_date=date(2024, 11, 18),
        statement_row_reference="s-date",
    )
    app = _app(
        "app-nf",
        merchant="Netflix",
        amount=Decimal("19.90"),
        transaction_date=date(2024, 11, 17),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    # Find any item with structured evidence
    assert len(items) >= 1
    d = items[0].structured_evidence.to_dict()

    assert d["statement_transaction_date"] == "2024-11-15"
    assert d["statement_posted_date"] == "2024-11-18"
    assert d["app_transaction_date"] == "2024-11-17"


# ---------------------------------------------------------------------------
# 4. Reason codes serialize as stable value strings
# ---------------------------------------------------------------------------


def test_reason_codes_serialize_as_value_strings():
    """Reason codes in structured evidence use stable .value strings."""
    stmt = _stmt(
        merchant_raw="Lazada",
        amount=Decimal("100.00"),
        statement_row_reference="s-rc",
    )
    app = _app(
        "app-lz",
        merchant="Lazada",
        amount=Decimal("200.00"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1
    d = am_items[0].structured_evidence.to_dict()

    rc_list = d["reason_codes"]
    assert isinstance(rc_list, list)
    assert len(rc_list) >= 1
    assert all(isinstance(r, str) for r in rc_list), f"all reason codes must be str, got {rc_list}"
    assert "amount_differs" in rc_list


# ---------------------------------------------------------------------------
# 5. issue_type and review_priority are included
# ---------------------------------------------------------------------------


def test_issue_type_and_review_priority_included():
    """Structured evidence includes issue_type and review_priority."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        statement_row_reference="s-prio",
    )
    app = _app(
        "app-gp",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1
    d = am_items[0].structured_evidence.to_dict()

    assert d["issue_type"] == "amount_mismatch"
    assert d["review_priority"] == "high"
    assert d["suggested_action"] == "adjust_app_transaction"


# ---------------------------------------------------------------------------
# 6. Missing app transaction evidence handled safely
# ---------------------------------------------------------------------------


def test_missing_app_transaction_evidence_none_values():
    """When no app transaction pool exists, app fields are None (serialized as null).

    Uses an empty app transaction pool so the matcher has no candidate to
    reference. This is the only scenario where ``best_app_transaction``
    is genuinely ``None``.
    """
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        transaction_date=date(2024, 12, 15),
        currency="SGD",
        statement_row_reference="s-noapp",
    )
    candidates = match_batch([stmt], [])
    items, summary = generate_review_queue(candidates)

    non_matched = [q for q in items if q.issue_type != IssueType.MATCHED]
    assert len(non_matched) >= 1

    se = non_matched[0].structured_evidence
    assert se is not None
    d = se.to_dict()

    # App fields should be None when no app transaction matched
    assert d["app_transaction_id"] is None
    assert d["app_amount"] is None
    assert d["app_merchant"] is None
    assert d["app_currency"] is None
    assert d["app_transaction_date"] is None
    assert d["amount_delta"] is None

    # Statement fields should still be present
    assert d["statement_reference"] == "s-noapp"
    assert d["statement_amount"] == "18.80"
    assert d["statement_currency"] == "SGD"


# ---------------------------------------------------------------------------
# 7. Exact matched item has structured evidence but is not review_required
# ---------------------------------------------------------------------------


def test_exact_match_has_structured_evidence_not_review_required():
    """Exact match items carry structured evidence but are not review_required."""
    stmt = _stmt(
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        statement_row_reference="s-exact",
    )
    app = _app(
        "app-exact",
        merchant="Apple",
        amount=Decimal("29.90"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    matched = [q for q in items if q.issue_type == IssueType.MATCHED]
    assert len(matched) >= 1

    q = matched[0]
    assert q.candidate.is_review_required is False
    assert q.candidate.review_priority == ReviewPriority.LOW

    se = q.structured_evidence
    assert se is not None
    d = se.to_dict()

    assert d["issue_type"] == "matched"
    assert d["review_priority"] == "low"
    assert d["suggested_action"] == "confirm_match"
    assert d["statement_amount"] == "29.90"
    assert d["app_amount"] == "29.90"
    assert d["amount_delta"] == "0.00"


# ---------------------------------------------------------------------------
# 8. evidence_summary remains present and human-readable
# ---------------------------------------------------------------------------


def test_evidence_summary_remains_present():
    """evidence_summary field remains present for human review."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        statement_row_reference="s-evid",
    )
    app = _app(
        "app-ev",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    assert len(items) >= 1
    for q in items:
        assert isinstance(q.evidence_summary, str)
        assert len(q.evidence_summary) > 0

    non_matched = [q for q in items if q.issue_type != IssueType.MATCHED]
    assert len(non_matched) >= 1
    ev = non_matched[0].evidence_summary
    # Should contain statement info
    assert "SGD" in ev or "18.80" in ev


# ---------------------------------------------------------------------------
# 9. Serialization is deterministic
# ---------------------------------------------------------------------------


def test_serialization_is_deterministic():
    """Calling to_dict() multiple times produces identical output."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        statement_row_reference="s-det",
    )
    app = _app(
        "app-det",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1

    d1 = am_items[0].structured_evidence.to_dict()
    d2 = am_items[0].structured_evidence.to_dict()

    assert d1 == d2, "to_dict() must be deterministic across multiple calls"


def test_field_order_is_deterministic():
    """Dictionary keys from to_dict() appear in deterministic field order."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        statement_row_reference="s-ord",
    )
    app = _app(
        "app-ord",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1

    d = am_items[0].structured_evidence.to_dict()
    keys = list(d.keys())

    # Verify expected key order (matching dataclass field order)
    expected_order = [
        "statement_reference",
        "statement_transaction_date",
        "statement_posted_date",
        "statement_merchant",
        "statement_amount",
        "statement_currency",
        "app_transaction_id",
        "app_transaction_date",
        "app_merchant",
        "app_amount",
        "app_currency",
        "amount_delta",
        "date_delta_days",
        "merchant_similarity",
        "candidate_count",
        "reason_codes",
        "issue_type",
        "review_priority",
        "suggested_action",
        "confidence_score",
    ]
    assert keys == expected_order, f"Key order mismatch: {keys}"


# ---------------------------------------------------------------------------
# 10. No float artefacts in serialized amount/delta values
# ---------------------------------------------------------------------------


def test_no_float_artefacts_in_serialized_values():
    """Serialized Decimal values must never contain float artefacts like '.600000000000001'."""
    stmt = _stmt(
        merchant_raw="Amazon",
        amount=Decimal("123.45"),
        statement_row_reference="s-float",
    )
    app = _app(
        "app-fl",
        merchant="Amazon",
        amount=Decimal("100.00"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1
    d = am_items[0].structured_evidence.to_dict()

    # Check all Decimal-derived string fields for floating-point artefacts
    for key in ("statement_amount", "app_amount", "amount_delta", "confidence_score"):
        val = d[key]
        assert isinstance(val, str), f"{key} must be str"
        # Float artefacts typically have many decimal digits or scientific notation
        assert "." not in val or len(val.split(".")[1]) <= 2 or key == "confidence_score", (
            f"{key}={val!r} has suspicious decimal places"
        )
        assert "e" not in val.lower(), f"{key}={val!r} has scientific notation"

    assert d["amount_delta"] == "23.45"


# ---------------------------------------------------------------------------
# 11. Currency fields carried through correctly
# ---------------------------------------------------------------------------


def test_currency_fields_carried_through():
    """Currency fields from statement and app are correctly present."""
    stmt = _stmt(
        merchant_raw="Starbucks",
        amount=Decimal("8.50"),
        currency="USD",
        statement_row_reference="s-cur",
    )
    app = _app(
        "app-cur",
        merchant="Starbucks",
        amount=Decimal("8.50"),
        currency="USD",
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    assert len(items) >= 1
    d = items[0].structured_evidence.to_dict()

    assert d["statement_currency"] == "USD"
    assert d["app_currency"] == "USD"


# ---------------------------------------------------------------------------
# 12. Structured evidence for MISSING_IN_STATEMENT case
# ---------------------------------------------------------------------------


def test_structured_evidence_missing_in_statement():
    """MISSING_IN_STATEMENT items carry structured evidence with app-side data."""
    stmt = _stmt(
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        statement_row_reference="s-exact",
    )
    app = _app(
        "app-matched",
        merchant="Apple",
        amount=Decimal("29.90"),
        transaction_date=date(2024, 12, 1),
    )
    # Use a second app that won't match the single statement
    app2 = _app(
        "app-orphan",
        merchant="Netflix",
        amount=Decimal("19.90"),
        currency="SGD",
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app, app2])
    items, summary = generate_review_queue(candidates)

    mis_items = [q for q in items if q.issue_type == IssueType.MISSING_IN_STATEMENT]
    assert len(mis_items) >= 1

    se = mis_items[0].structured_evidence
    assert se is not None
    d = se.to_dict()

    # App side should be populated
    assert d["app_transaction_id"] == "app-orphan"
    assert d["app_amount"] == "19.90"
    assert d["app_merchant"] == "Netflix"
    assert d["review_priority"] == "medium"
    assert d["issue_type"] == "missing_in_statement"

    # Statement side may be synthetic (from the app itself)
    assert d["statement_amount"] == "19.90"


# ---------------------------------------------------------------------------
# 13. to_dict excludes internal-only types
# ---------------------------------------------------------------------------


def test_to_dict_excludes_internal_types():
    """to_dict must not leak internal Python objects (dataclasses, enums, etc.)."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        statement_row_reference="s-int",
    )
    app = _app(
        "app-int",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1
    d = am_items[0].structured_evidence.to_dict()

    import json

    # Must be JSON-serializable
    json_str = json.dumps(d, sort_keys=True)
    assert len(json_str) > 0

    # No Python object repr artefacts
    assert "ReasonCode." not in json_str
    assert "IssueType." not in json_str
    assert "ReviewPriority." not in json_str
    assert "Decimal(" not in json_str
    assert "datetime." not in json_str


# ---------------------------------------------------------------------------
# 14. build_structured_evidence standalone function
# ---------------------------------------------------------------------------


def test_build_structured_evidence_standalone():
    """The standalone build_structured_evidence function works outside review queue."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        statement_row_reference="s-standalone",
    )
    app = _app(
        "app-sa",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    assert len(candidates) == 1

    se = build_structured_evidence(
        candidates[0],
        suggested=SuggestedAction.ADJUST_APP_TRANSACTION,
    )
    assert isinstance(se, ReconciliationReviewEvidence)
    assert se.statement_amount == Decimal("18.80")
    assert se.app_amount == Decimal("18.20")
    assert se.issue_type == IssueType.AMOUNT_MISMATCH
