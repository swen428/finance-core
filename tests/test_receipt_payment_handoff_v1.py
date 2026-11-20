"""Synthetic Core account inventory verified separately from installed A2 custody."""

from dataclasses import asdict, replace

import pytest

from finance_core.lifecycle_v2.contracts import LifecycleError, VerifiedReceiptSourceV1, digest
from finance_core.lifecycle_v2.daily_v1 import DailyAccountServiceV1
from finance_core.lifecycle_v2.receipt_draft_v1 import VerifiedReceiptManualReplyV1
from tests.daily_account_support_v1 import NOW, DailyPort, command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import create
from tests.test_lifecycle_v2 import live as live


class HandoffPort(DailyPort):
    def __init__(self):
        super().__init__()
        self.receipt = VerifiedReceiptSourceV1(
            "receipt", "user", "chat", "receipt:v1:receipt", "", "a" * 64, NOW, 50, "b" * 64
        )
        self.answer = VerifiedReceiptManualReplyV1(
            "receipt",
            digest(asdict(self.receipt)),
            "user",
            "chat",
            "request",
            20,
            21,
            90,
            NOW,
            "poll-1",
            "c" * 64,
            "商家：Sunny\n日期：2026-11-20\n总额：8.00",
        )
        self.supplement = replace(
            self.answer,
            reply_message_id=22,
            reply_update_id=91,
            response_id="poll-2",
            response_sha256="d" * 64,
            raw_text="币种：SGD",
        )
        self.replies = (self.answer,)

    def receipt_source_v1(self, _conn, evidence_id):
        assert evidence_id == self.receipt.evidence_id
        return self.receipt

    def receipt_manual_replies_v1(self, conn, source):
        assert source == self.receipt
        assert source is not self.receipt
        assert conn.in_transaction and conn.execute("SELECT 1").fetchone()[0] == 1
        return self.replies


def test_receipt_handoff_uses_confirmed_current_accounts_without_payment_inference(live):
    ledger, _, _ = live
    port = ledger.port = HandoffPort()
    ledger.clock = lambda: NOW
    daily = DailyAccountServiceV1(ledger)
    payments = ledger.reader()
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    assert payments.receipt_payment_handoff_v1(source.source_id) is None

    opening = {"amount": "0", "as_of": unknown_time()}
    port.add_source("source2", "create the SGD cash account with zero opening")
    _, _, _, wallet = create(
        ledger,
        port,
        daily,
        command(
            fields={"name": "Wallet", "account_type": "cash", "currency": "SGD", "opening": opening}
        ),
        suffix="2",
    )
    port.add_source("source3", "create the USD bank account with zero opening")
    _, _, _, foreign = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Foreign",
                "account_type": "bank",
                "currency": "USD",
                "opening": opening,
            }
        ),
        suffix="3",
    )
    port.add_source("pending", "propose an unconfirmed card")
    pending = ledger.capture("pending", "capture-pending")
    port.commands["unconfirmed-card"] = command(
        fields={"name": "Unconfirmed", "account_type": "credit_card", "currency": "SGD"}
    )
    daily.propose_account_create(pending.source_id, "unconfirmed-card", 0, "propose-pending")

    port.replies = (port.answer, port.supplement)
    before = payments.full_audit()
    result = payments.receipt_payment_handoff_v1(source.source_id)
    assert result.state == "ACCOUNT_REQUIRED"
    assert result.missing_payment_fields == ("account_id", "account_amount")
    assert [(option.name, option.currency) for option in result.account_options] == [
        ("Wallet", "SGD"),
        ("Foreign", "USD"),
    ]
    assert [item.account_ref for item in result.account_options] == [
        wallet.account_ref,
        foreign.account_ref,
    ]
    assert result.draft == payments.receipt_manual_draft_v1(source.source_id)
    assert payments.full_audit() == before

    port.replies = (port.answer, replace(port.supplement, raw_text="币种：ZZZ"))
    unsupported = payments.receipt_payment_handoff_v1(source.source_id)
    assert unsupported.state == "UNSUPPORTED_RECEIPT_CURRENCY"
    assert unsupported.account_options == ()


@pytest.mark.parametrize(
    ("amount", "currency", "state"),
    [
        ("8.001", "SGD", "INVALID_RECEIPT_AMOUNT"),
        ("8.50", "JPY", "INVALID_RECEIPT_AMOUNT"),
        ("0", "SGD", "INVALID_RECEIPT_AMOUNT"),
        ("8.00", "SGD", "NO_ACCOUNT_OPTIONS"),
        ("800", "JPY", "NO_ACCOUNT_OPTIONS"),
    ],
)
def test_receipt_handoff_uses_bookable_original_money(live, amount, currency, state):
    ledger, _, _ = live
    port = ledger.port = HandoffPort()
    ledger.clock = lambda: NOW
    reader = ledger.reader()
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    answer = replace(port.answer, raw_text=f"商家：Sunny\n日期：2026-11-20\n总额：{amount}")
    currency_reply = replace(port.supplement, raw_text=f"币种：{currency}")
    port.replies = (answer,)
    original = reader.receipt_manual_draft_v1(source.source_id)
    assert original.missing_receipt_fields == ("currency",)
    assert reader.receipt_payment_handoff_v1(source.source_id) is None

    port.replies = (answer, currency_reply)
    result = reader.receipt_payment_handoff_v1(source.source_id)
    assert result.state == state and result.account_options == ()
    assert result.draft.draft_id == original.draft_id
    assert result.draft.manual_reply == answer and result.draft.manual_reply is not answer
    if state == "INVALID_RECEIPT_AMOUNT":
        assert result.missing_payment_fields == ()
        assert result.draft.fields[2].value == amount


def test_invalid_receipt_total_can_be_corrected_after_four_separate_replies(live):
    ledger, _, _ = live
    port = ledger.port = HandoffPort()
    ledger.clock = lambda: NOW
    reader = ledger.reader()
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    port.replies = tuple(
        replace(port.answer, reply_message_id=21 + i, reply_update_id=90 + i, raw_text=text)
        for i, text in enumerate(("商家：Sunny", "日期：2026-11-20", "总额：8.50", "币种：JPY"))
    )
    original = reader.receipt_payment_handoff_v1(source.source_id)
    assert original.state == "INVALID_RECEIPT_AMOUNT"
    paid = replace(port.answer, reply_message_id=25, reply_update_id=94, raw_text="总额：850")
    port.replies += (paid,)
    valid = reader.receipt_payment_handoff_v1(source.source_id)
    assert valid.state == "NO_ACCOUNT_OPTIONS"
    assert valid.draft.draft_id == original.draft.draft_id
    assert valid.draft.manual_supplements[1].raw_text == "总额：8.50"
    amount = next(field for field in valid.draft.fields if field.name == "amount")
    start, end = amount.raw_codepoint_span
    assert amount.reply_update_id == paid.reply_update_id
    assert amount.value == paid.raw_text[start:end] == "850"


def test_receipt_draft_requires_exact_reply_history_container(live):
    ledger, _, _ = live
    port = ledger.port = HandoffPort()
    ledger.clock = lambda: NOW
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    port.replies = [port.answer]
    with pytest.raises(LifecycleError, match="Exact bounded receipt reply history"):
        ledger.reader().receipt_manual_draft_v1(source.source_id)
