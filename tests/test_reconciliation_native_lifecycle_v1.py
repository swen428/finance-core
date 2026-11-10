"""Actual native posting/correction into opt-in read-only candidates."""

import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext

import pytest
from test_lifecycle_v2 import live, ready, response  # noqa: F401

from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import VerifiedSource
from finance_core.lifecycle_v2.effective_v1 import (
    EffectiveFilterV1,
    EffectiveLedgerReaderV1,
    EffectiveReadError,
    ReadLimitsV1,
)
from finance_core.reconciliation.matcher import match_statement
from finance_core.reconciliation.models import (
    MatchStatus,
    ReconciliationTransactionType,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.native_lifecycle_v1 import NativeReconciliationAdapterV1


def test_native_correction_keeps_one_version_bound_candidate_without_legacy_writes(live):  # noqa: F811
    service, port, _ = live
    _, _, review = ready(service)
    first = service.confirm(review.review_id, "action1", "confirm1")
    reader = EffectiveLedgerReaderV1(service)
    adapter = NativeReconciliationAdapterV1(reader)
    old = adapter.fetch_candidates().candidates[0]

    text = "change amount to USD 15.00; reason: correction"
    port.sources["native-edit"] = VerifiedSource(
        "native-edit",
        "user",
        "chat",
        "native-edit-message",
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        1792800000,
    )
    port.raw = response(text, "reply", {"amount": "15.00", "currency": "USD"})
    correction = service.prepare_correction(
        first.record_id, 1, "native-obs", "native-correction", "native-edit", "correction"
    )
    service.record_delivery(correction.review_id, "native-delivery")
    second = service.confirm(correction.review_id, "native-action", "native-confirm")
    assert service.confirm(correction.review_id, "native-action", "native-confirm") == second

    actual = adapter.fetch_candidates(EffectiveFilterV1(currency="usd"))
    assert actual.complete is True and actual.scope == "native-lifecycle-v2"
    assert actual.filters.currency == "USD"
    assert len(actual.candidates) == 1
    bound = actual.candidates[0]
    assert bound.fact_ref.record_id == old.fact_ref.record_id == first.record_id
    assert bound.fact_ref.economic_event_id == old.fact_ref.economic_event_id
    assert bound.fact_ref.version == 2
    assert bound.fact_ref.fact_sha256 == second.fact_sha256 != old.fact_ref.fact_sha256
    assert bound.fact_ref.commit_seq == second.commit_seq
    assert bound.source == old.source
    assert bound.account_status == "unknown" and bound.account_id is None
    assert bound.candidate.internal_id == f"lifecycle-v2:{first.record_id}"
    assert bound.candidate.amount == Decimal("15.00")
    assert bound.candidate.currency == "USD"
    assert bound.candidate.transaction_type == ReconciliationTransactionType.EXPENSE
    assert bound.candidate.source_channel is None
    assert bound.candidate.attachment_path is None
    assert second.fact_sha256 in bound.candidate.evidence_reference
    assert adapter.fetch_candidates(EffectiveFilterV1(currency="SGD")).candidates == ()

    statement = StatementTransaction(
        transaction_date=date(2026, 10, 24),
        posted_date=None,
        merchant_raw="Cafe",
        amount=Decimal("15.00"),
        currency="USD",
        amount_direction=StatementAmountDirection.DEBIT,
    )
    matched = match_statement(statement, [bound.candidate])
    assert matched.status == MatchStatus.MATCHED
    assert matched.best_candidate.internal_id == bound.candidate.internal_id
    # The wrapper supplies the version/hash association; a match grants no write.
    assert reader.get(first.record_id).fact_sha256 == bound.fact_ref.fact_sha256
    with lifecycle_read_operation(service.owner) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 2


def test_native_adapter_preserves_one_complete_read_and_read_identity(live, monkeypatch):  # noqa: F811
    service, _, _ = live
    _, _, review = ready(service)
    service.confirm(review.review_id, "action1", "confirm1")
    reader = EffectiveLedgerReaderV1(service)
    original = reader.collect
    observed = []

    def capture(**kwargs):
        result = original(**kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(reader, "collect", capture)
    actual = NativeReconciliationAdapterV1(reader).fetch_candidates(
        EffectiveFilterV1(date_from="2026-10-24", date_to="2026-10-24"),
        ReadLimitsV1(max_records=1, max_fact_versions=1),
    )
    assert len(observed) == 1
    assert actual.read_identity == observed[0].read_identity
    assert actual.filters == observed[0].filters
    assert actual.candidates[0].source == observed[0].facts[0].source


def test_native_candidate_refuses_evidence_failure_even_outside_filter(live):  # noqa: F811
    service, port, _ = live
    _, _, review = ready(service)
    service.confirm(review.review_id, "action1", "confirm1")
    port.sources["source1"] = replace(port.sources["source1"], raw_input="changed")
    adapter = NativeReconciliationAdapterV1(EffectiveLedgerReaderV1(service))
    with pytest.raises(EffectiveReadError) as error:
        adapter.fetch_candidates(EffectiveFilterV1(currency="JPY"))
    assert error.value.code == "fact_evidence_invalid"


def test_native_adapter_empty_and_rejects_arbitrary_fact_input(live):  # noqa: F811
    service, _, _ = live
    collection = NativeReconciliationAdapterV1(EffectiveLedgerReaderV1(service)).fetch_candidates()
    assert collection.complete and collection.candidates == ()
    with pytest.raises(TypeError, match="EffectiveLedgerReaderV1"):
        NativeReconciliationAdapterV1([])


def test_native_candidate_preserves_amount_under_low_ambient_precision(live):  # noqa: F811
    service, _, _ = live
    _, _, review = ready(service)
    posted = service.confirm(review.review_id, "action1", "confirm1")
    adapter = NativeReconciliationAdapterV1(EffectiveLedgerReaderV1(service))
    with localcontext() as context:
        context.prec = 2
        actual = adapter.fetch_candidates()
        assert context.prec == 2
    assert actual.complete and len(actual.candidates) == 1
    assert actual.candidates[0].fact_ref.fact_sha256 == posted.fact_sha256
    assert actual.candidates[0].candidate.amount == Decimal("12.00")
