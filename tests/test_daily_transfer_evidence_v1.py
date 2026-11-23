"""Transfer coverage can be read only from the exact delivered role question."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

import pytest

from finance_core.lifecycle_v2 import LifecycleError, VerifiedSource
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.daily_v1.contracts import instruction_ref
from finance_core.lifecycle_v2.daily_v1.transfer_evidence_v1 import (
    QUESTION_FORMAT,
    VerifiedTransferOpeningReplyV1,
    _transfer_terms_digest,
    require_transfer_opening_replies_v1,
)
from finance_core.lifecycle_v2.daily_v1.transfer_review_v1 import read_transfer_create_preview_v1
from tests.daily_account_support_v1 import unknown_time
from tests.test_daily_accounts_lifecycle_v1 import setup
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_daily_transfer_opening_v1 import _account, _on
from tests.test_lifecycle_v2 import live as live


def _retained_questions(ledger, port, transfer):
    original = transfer["instruction_ref"]
    raw_instruction = "Transfer actual SGD 100.00 to MYR 330.00 with an additional SGD 1.00 fee"
    source = VerifiedSource(
        original["evidence_id"],
        ledger.binding.principal_id,
        ledger.binding.client_id,
        original["source_key"],
        raw_instruction,
        sha256(raw_instruction.encode()).hexdigest(),
        original["source_timestamp"],
        "Asia/Singapore",
    )
    port.sources[source.evidence_id] = source
    original.update(instruction_ref(asdict(source), ledger.binding))
    legs = (transfer["outgoing"], transfer["incoming"], transfer["fee"]["charge"])
    for leg in legs:
        coverage = leg["coverage"]
        coverage["binding"]["instruction_ref"] = original
        coverage["binding_sha256"] = digest(coverage["binding"])
        reply = coverage["reply_ref"]
        if reply:
            raw = "已包含"
            source = VerifiedSource(
                reply["evidence_id"],
                ledger.binding.principal_id,
                ledger.binding.client_id,
                reply["source_key"],
                raw,
                sha256(raw.encode()).hexdigest(),
                original["source_timestamp"] + 5,
                "Asia/Singapore",
            )
            port.sources[source.evidence_id] = source
            coverage["reply_ref"] = instruction_ref(asdict(source), ledger.binding)

    terms = _transfer_terms_digest(transfer)
    questions = {}
    for leg in legs:
        coverage = leg["coverage"]
        if coverage["reply_ref"]:
            identity = coverage["reply_ref"]["evidence_id"]
            questions[identity] = {
                "format": QUESTION_FORMAT,
                "delivery_id": "delivery-" + identity,
                "delivered_at": original["source_timestamp"] + 1,
                "transfer_sha256": terms,
                "role_binding_sha256": coverage["binding_sha256"],
            }

    def read_question(conn, identity):
        assert conn.in_transaction
        return VerifiedTransferOpeningReplyV1(identity, canonical(questions[identity]))

    port.transfer_opening_reply_v1 = read_question
    return questions


def test_checks_actual_replies_and_rejects_reusing_a_question_for_the_other_role(live, transfer):
    ledger, port, daily, _, _ = setup(live)
    questions = _retained_questions(ledger, port, transfer)
    with daily._read() as (service, conn):
        require_transfer_opening_replies_v1(service, conn, transfer)
        questions["answer-fee_debit"] = questions["answer-transfer_in"]
        with pytest.raises(LifecycleError, match="exact delivered question"):
            require_transfer_opening_replies_v1(service, conn, transfer)


def test_exact_role_reply_cannot_be_reused_after_other_transfer_terms_change(live, transfer):
    ledger, port, daily, _, _ = setup(live)
    _retained_questions(ledger, port, transfer)
    changed = deepcopy(transfer)
    fee = changed["fee"]["charge"]
    fee["amount"] = "2.00"
    fee["coverage"]["binding"]["amount"] = "2.00"
    fee["coverage"]["binding_sha256"] = digest(fee["coverage"]["binding"])
    with daily._read() as (service, conn):
        with pytest.raises(LifecycleError, match="exact delivered question"):
            require_transfer_opening_replies_v1(service, conn, changed)


def test_complete_transfer_preview_shows_per_currency_deltas_without_writing(live, transfer):
    ledger, port, daily, reader, _ = setup(live)
    boundary = _on("2026-11-21")
    out = _account(ledger, port, daily, reader, "SGD", "1", boundary)
    incoming = _account(ledger, port, daily, reader, "MYR", "2", unknown_time())
    fee = transfer["fee"]["charge"]
    fee["funds_time"] = boundary
    for leg, account in ((transfer["outgoing"], out), (transfer["incoming"], incoming), (fee, out)):
        leg["account_ref"] = asdict(account.account_ref)
        leg["opening_ref"] = asdict(account.opening_ref)
        leg["coverage"]["binding"].update(
            {key: leg[key] for key in ("account_ref", "opening_ref", "funds_time")}
        )
    _retained_questions(ledger, port, transfer)

    before = reader.collect_accounts()
    preview = read_transfer_create_preview_v1(ledger, transfer)
    assert preview.read_identity == before.read_identity
    assert {
        change.currency: (change.recorded_change, change.balance_change)
        for change in preview.account_changes
    } == {"SGD": ("-101.00", "-100.00"), "MYR": ("330.00", "0.00")}
    assert preview.expense_by_currency == (("SGD", "1.00"),)
    assert ledger.reader().full_audit()["fact_version_count"] == 2

    original = port.sources[transfer["instruction_ref"]["evidence_id"]]
    port.add_source(original.evidence_id, "Different transfer")
    with pytest.raises(LifecycleError, match="authenticated source"):
        read_transfer_create_preview_v1(ledger, transfer)
