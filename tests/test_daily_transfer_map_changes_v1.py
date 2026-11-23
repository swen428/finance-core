"""Transfer versions retain historical terms and use current opening decisions."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

import pytest

from finance_core.lifecycle_v2.contracts import canonical
from finance_core.lifecycle_v2.daily_v1 import (
    DailyLedgerReaderV1,
    DailyOpeningChangeServiceV1,
    DailyTransferServiceV1,
    VerifiedTransferChangeTargetV1,
)
from finance_core.lifecycle_v2.daily_v1.contracts import instruction_ref
from finance_core.lifecycle_v2.daily_v1.transfer_evidence_v1 import CHANGE_TARGET_FORMAT
from tests.test_daily_opening_changes_v1 import finish, opening_command, opening_proposal, row_reply
from tests.test_daily_transfer_evidence_v1 import (
    _retained_questions,
    _transfer_with_account_snapshots,
)
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_daily_transfer_opening_v1 import _on
from tests.test_lifecycle_v2 import live as live


@pytest.mark.parametrize("transition", ["transfer_void", "transfer_correct"])
def test_exact_opening_map_treatment_cannot_be_inherited_by_the_next_fact(
    live, transfer, transition
):
    ledger, port, daily, reader, outgoing = _transfer_with_account_snapshots(
        live, transfer, opening_port=True
    )
    account_source = ledger.capture("source1", "capture1")
    account_proposal = daily.propose_account_create(account_source.source_id, "obs1", 0, "propose1")
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "captured-transfer")
    service = DailyTransferServiceV1(ledger)
    initial = service.propose_create(
        capture.source_id, transfer, "transfer-values", "transfer-draft"
    )
    review = service.prepare_review(initial.proposal_id, initial.version)
    service.record_delivery(review.review_id, "transfer-display")
    service.confirm(review.review_id, "transfer-action", "transfer-confirm")

    opening = DailyOpeningChangeServiceV1(ledger)
    boundary = _on("2026-11-22")
    pending = opening_proposal(
        ledger,
        port,
        opening,
        account_source,
        account_proposal,
        outgoing,
        "change-opening",
        opening_command(amount="125", boundary=boundary),
    )
    packet = opening.opening_questions_v1(pending.proposal_id)
    subjects = {subject["impact_role"]: subject for subject in packet["subjects"]}
    delivered = port.deliver_opening_question(packet, "split-transfer-opening")
    answers = row_reply(
        port,
        "pay-role-reply",
        (subjects["transfer_out"], "excluded"),
        (subjects["fee_debit"], "included"),
    )
    proposed = opening_proposal(
        ledger,
        port,
        opening,
        account_source,
        pending,
        outgoing,
        "pay-role-reply",
        opening_command(amount="125", boundary=boundary, answers=answers),
        "change-opening",
        delivered,
    )
    finish(opening, proposed, "pay-role-reply")
    ledger_reader = DailyLedgerReaderV1(ledger)
    before = ledger_reader.collect_with_transfers_v2()
    assert {row.currency: row.balance_amount for row in before.collection.balances}[
        "SGD"
    ] == "25.00"

    reason = "original transfer was entered incorrectly"
    raw = f"{transition} because the {reason}"
    source = port.add_source("modify-transfer", raw)
    ref = instruction_ref(asdict(source), ledger.binding)
    span = [raw.index(reason), len(raw)]
    values = deepcopy(transfer)
    values.update(transition=transition, instruction_ref=ref)
    values["reason_ref"] = {
        "format": "finance-daily-reason-ref-v1",
        "instruction_ref": ref,
        "span": span,
        "quote": reason,
        "quote_sha256": sha256(reason.encode()).hexdigest(),
    }
    if transition == "transfer_void":
        values["status"] = "VOID"
    else:
        values["outgoing"]["amount"] = "115.00"
        current = reader.get_account(outgoing.account_id)
        for leg in (values["outgoing"], values["fee"]["charge"]):
            leg["opening_ref"] = asdict(current.opening_ref)
            leg["funds_time"] = _on("2026-11-23")
            leg["coverage"].update(treatment="excluded", basis="strictly_later", reply_ref=None)
            leg["coverage"]["binding"].update(
                **{key: leg[key] for key in ("opening_ref", "funds_time", "amount")}
            )
        reply = values["incoming"]["coverage"]["reply_ref"]
        reply["evidence_id"] += "-after-map"
        reply["source_key"] += "-after-map"
        _retained_questions(ledger, port, values, raw_instruction=raw)
    target = before.transfers[0].fact_ref
    link = VerifiedTransferChangeTargetV1(
        "transfer-after-map-relation",
        canonical(
            {
                "format": CHANGE_TARGET_FORMAT,
                "instruction_ref": ref,
                "target_fact_ref": asdict(target),
                "transition": transition,
                "reason_span": span,
            }
        ),
    )
    retained_links = {source.evidence_id: link}
    port.transfer_change_target_v1 = lambda _conn, evidence: retained_links[evidence]
    revised = service.propose_change(
        capture.source_id,
        target,
        values,
        "revised-values",
        "revised-draft",
        expected_version=initial.version,
    )
    card = service.prepare_review(revised.proposal_id, revised.version)
    old_fee = next(
        role for role in card.material["before_account_roles"] if role["impact_role"] == "fee_debit"
    )
    assert (old_fee["source"], old_fee["balance_impact"]) == ("map", "0.00")
    service.record_delivery(card.review_id, "revised-display")
    service.confirm(card.review_id, "revised-action", "revised-confirm")
    after = ledger_reader.collect_with_transfers_v2()
    balance = {row.currency: row.balance_amount for row in after.collection.balances}["SGD"]
    if transition == "transfer_void":
        assert balance == "125.00"
        assert after.collection.expense_by_currency == ()
        assert not after.effects
    else:
        assert balance == "9.00"
        new_fee = next(
            row.values for row in after.effects if row.values["impact_role"] == "fee_debit"
        )
        assert (new_fee["source"], new_fee["balance_impact"]) == ("direct", "-1.00")
        assert after.collection.expense_by_currency == (("SGD", "1.00"),)
