"""Receipt preparation through the existing admitted read owner, with a synthetic evidence port."""

from dataclasses import asdict, replace

import pytest

from finance_core.instance_admission import acquire_production_owner
from finance_core.lifecycle_v2.contracts import LifecycleError, digest
from finance_core.lifecycle_v2.daily_v1 import DailyAccountReaderV1, DailyAccountServiceV1
from finance_core.lifecycle_v2.receipt_fields_v1 import ReceiptWholeDraftV1
from finance_core.lifecycle_v2.receipt_review_fields_v1 import (
    VerifiedReceiptReviewFieldsReplyV1,
    receipt_review_fields_target_v1,
    resolve_receipt_review_fields_v1,
)
from finance_core.lifecycle_v2.receipt_review_preparation_v1 import (
    ReceiptReviewReaderV1,
    VerifiedReceiptPaymentReplyV1,
    VerifiedReceiptPaymentV1,
)
from finance_core.lifecycle_v2.service import LedgerService
from tests.daily_account_support_v1 import NOW, command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import change, create
from tests.test_lifecycle_v2 import live as live
from tests.test_receipt_payment_handoff_v1 import HandoffPort


class PaymentPort(HandoffPort):
    payment = None
    review_reply = None
    payment_inputs = None

    def receipt_review_payment_v1(self, conn, source, draft):
        assert conn.in_transaction
        assert source == self.receipt
        if type(draft) is ReceiptWholeDraftV1:
            assert draft.source == source
        else:
            assert draft.source_evidence_id == self.receipt.evidence_id
        self.payment_inputs = (source, draft)
        return self.payment

    def receipt_review_fields_reply_v1(self, conn, source_evidence_id):
        assert conn.in_transaction and source_evidence_id == self.receipt.evidence_id
        return self.review_reply


def setup_payment(live, *, opening=None):
    ledger, _, _ = live
    port = ledger.port = PaymentPort()
    ledger.clock = lambda: NOW
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    daily = DailyAccountServiceV1(ledger)
    fields = {"name": "Wallet", "account_type": "cash", "currency": "SGD", "opening": opening}
    create(ledger, port, daily, command(fields=fields))
    port.replies = (port.answer, replace(port.supplement, raw_text="币种：USD"))
    handoff = ledger.reader().receipt_payment_handoff_v1(source.source_id)
    payment = payment_for_handoff(source, handoff)
    port.payment = payment
    return ledger, port, daily, source.source_id, payment


def payment_for_handoff(source, handoff):
    selection = VerifiedReceiptPaymentReplyV1(
        "payment-question", "1" * 64, 4002, NOW, 24, 92, NOW + 1, "selection-poll", "2" * 64, "1"
    )
    debit = VerifiedReceiptPaymentReplyV1(
        "debit-question",
        "3" * 64,
        4003,
        NOW + 1,
        26,
        93,
        NOW + 2,
        "debit-poll",
        "4" * 64,
        "SGD 10.37",
    )
    return VerifiedReceiptPaymentV1(
        "receipt",
        source.source_sha256,
        digest(asdict(handoff.draft)),
        "user",
        "chat",
        1,
        handoff.account_options[0],
        selection,
        debit,
        selection.response_id,
        selection.response_sha256,
        selection.reply_update_id,
        handoff.account_options[0],
    )


def test_receipt_preparation_preserves_both_original_amounts_and_requires_later_review(live):
    ledger, port, _, source_id, payment = setup_payment(live)
    reader = ReceiptReviewReaderV1(ledger)
    audit = ledger.reader().full_audit()
    result = reader.prepare(source_id)
    assert result.source == port.receipt and result.draft.manual_supplements == (port.replies[1],)
    assert result.draft.original_image_sha256 == port.receipt.original_sha256
    assert result.payment == payment
    expected_source, expected_draft = port.payment_inputs
    assert result.source == expected_source and result.source is not expected_source
    assert result.draft == expected_draft and result.draft is not expected_draft
    assert result.draft.fields[0] is not expected_draft.fields[0]
    assert result.payment.account.account_ref is not payment.account.account_ref
    assert (result.merchant, result.business_date) == ("Sunny", "2026-11-20")
    assert (result.original_amount, result.original_currency) == ("8.00", "USD")
    assert (result.account_amount, result.account_currency) == ("10.37", "SGD")
    assert result.active_opening_ref is None
    assert result.missing_movement_fields == ("category",)
    assert reader.prepare(source_id).preparation_sha256 == result.preparation_sha256
    port.payment = replace(
        payment, option_number=13, selection=replace(payment.selection, raw_text="13")
    )
    assert reader.prepare(source_id).payment.option_number == 13
    port.payment = payment
    assert ledger.reader().full_audit() == audit
    assert audit["fact_version_count"] == 1
    assert ledger.reader().pending() == []
    ledger.owner.close()
    with acquire_production_owner(live[2]) as owner:
        reopened = LedgerService(owner, port, ledger.binding, ledger.clock)
        assert ReceiptReviewReaderV1(reopened).prepare(source_id) == result
        assert reopened.reader().full_audit() == audit


def test_receipt_preparation_does_not_infer_opening_coverage_and_rejects_stale_account(live):
    ledger, port, daily, source_id, payment = setup_payment(
        live,
        opening={
            "amount": "100",
            "as_of": {**unknown_time(), "precision": "date", "local_date": "2026-11-18"},
        },
    )
    reader = ReceiptReviewReaderV1(ledger)
    account = DailyAccountReaderV1(ledger).get_account(payment.account.account_ref.account_id)
    result = reader.prepare(source_id)
    assert result.active_opening_ref == account.opening_ref
    assert result.missing_movement_fields == ("category", "opening_coverage")
    change(port, daily, account, "account_rename", {"name": "Renamed"}, "rename")
    with pytest.raises(LifecycleError, match="stale_receipt_payment_account_or_opening"):
        reader.prepare(source_id)


def test_receipt_preparation_refuses_unlinked_or_invalid_replies_and_keeps_incomplete_pending(live):
    ledger, port, _, source_id, payment = setup_payment(live)
    reader = ReceiptReviewReaderV1(ledger)
    audit = ledger.reader().full_audit()
    other_account = replace(
        payment.account,
        account_ref=replace(payment.account.account_ref, account_id="other-account"),
        bundle_ref=replace(payment.account.bundle_ref, record_id="other-account"),
        name="Other wallet",
    )
    for wrong in (
        replace(payment, draft_sha256="5" * 64),
        replace(payment, principal_id="another-user"),
        replace(payment, debit_question_selection_update_id=80),
        replace(payment, selection=replace(payment.selection, raw_text="2")),
        replace(payment, debit=replace(payment.debit, raw_text="USD 10.37")),
        replace(payment, debit=replace(payment.debit, raw_text="SGD 10.371")),
        replace(payment, debit=replace(payment.debit, raw_text="SGD 0.00")),
        replace(payment, debit_question_account=other_account),
    ):
        port.payment = wrong
        with pytest.raises(LifecycleError, match="Receipt payment|Movement amounts"):
            reader.prepare(source_id)
    port.payment = None
    assert reader.prepare(source_id) is None
    port.payment = payment
    port.replies = (port.answer,)
    assert reader.prepare(source_id) is None
    assert ledger.reader().full_audit() == audit


def test_receipt_review_fields_exact_opening_reply_reopens_without_booking(live):
    ledger, port, _, source_id, _ = setup_payment(
        live,
        opening={
            "amount": "100",
            "as_of": {**unknown_time(), "precision": "date", "local_date": "2026-11-18"},
        },
    )
    reader = ReceiptReviewReaderV1(ledger)
    prepared = reader.prepare(source_id)
    target = receipt_review_fields_target_v1(prepared)
    assert reader.completed(source_id) is None
    question_timestamp = NOW + 4 * 86400
    reply = VerifiedReceiptPaymentReplyV1(
        "fields-question",
        "5" * 64,
        4004,
        question_timestamp,
        27,
        94,
        question_timestamp + 1,
        "fields-poll",
        "6" * 64,
        "分类：餐饮\n扣款日期：2026-11-17\n期初余额：未包含",
    )
    port.review_reply = VerifiedReceiptReviewFieldsReplyV1(target, reply, "user", "chat")
    audit = ledger.reader().full_audit()
    completed = reader.completed(source_id)
    assert completed.preparation == prepared
    assert completed.category == "餐饮" and completed.actual_debit_date == "2026-11-17"
    assert completed.evidence.target.account_ref is not target.account_ref
    assert completed.evidence.target.opening_ref is not target.opening_ref
    assert completed.opening_treatment == "excluded"
    assert (
        completed.opening_basis == "authenticated_reply" and completed.missing_movement_fields == ()
    )
    port.review_reply = replace(
        port.review_reply, target=replace(target, debit_response_sha256="7" * 64)
    )
    with pytest.raises(LifecycleError, match="linked payment"):
        reader.completed(source_id)
    port.review_reply = replace(
        completed.evidence, target=replace(target, source_evidence_id="another-receipt")
    )
    with pytest.raises(LifecycleError, match="linked payment"):
        reader.completed(source_id)
    port.review_reply = replace(
        completed.evidence, reply=replace(reply, raw_text="分类：餐饮\n扣款日期：2026-11-19")
    )
    with pytest.raises(LifecycleError, match="after the authenticated amount reply"):
        reader.completed(source_id)
    port.review_reply = completed.evidence
    ledger.owner.close()
    with acquire_production_owner(live[2]) as owner:
        reopened = LedgerService(owner, port, ledger.binding, ledger.clock)
        assert ReceiptReviewReaderV1(reopened).completed(source_id) == completed
        assert reopened.reader().full_audit() == audit
        assert reopened.reader().pending() == []


def test_receipt_actual_debit_date_must_be_explicit_and_strictly_after_opening():
    opening = {**unknown_time(), "precision": "date", "local_date": "2026-11-18"}
    reply_timestamp = NOW + 4 * 86400
    for raw in ("分类：餐饮", "分类：餐饮\n扣款日期：2026-11-18"):
        assert resolve_receipt_review_fields_v1(raw, reply_timestamp, opening)[2:] == (None, None)
    assert resolve_receipt_review_fields_v1(
        "分类：餐饮\n扣款日期：2026-11-20", reply_timestamp, opening
    ) == ("餐饮", "2026-11-20", "excluded", "strictly_later")
    assert resolve_receipt_review_fields_v1(
        "分类：餐饮\n扣款日期：2026-11-18\n期初余额：已包含", reply_timestamp, opening
    ) == ("餐饮", "2026-11-18", "included", "authenticated_reply")
    with pytest.raises(LifecycleError, match="contradicts"):
        resolve_receipt_review_fields_v1(
            "分类：餐饮\n扣款日期：2026-11-20\n期初余额：已包含", reply_timestamp, opening
        )
    with pytest.raises(LifecycleError, match="after the authenticated amount reply"):
        resolve_receipt_review_fields_v1(
            "分类：餐饮\n扣款日期：2026-11-22", reply_timestamp, opening
        )
