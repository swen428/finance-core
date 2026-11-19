"""Synthetic Core account inventory verified separately from installed A2 custody."""

from dataclasses import asdict, replace

from finance_core.lifecycle_v2.contracts import VerifiedReceiptSourceV1, digest
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

    def receipt_manual_replies_v1(self, _conn, source):
        assert source == self.receipt
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
