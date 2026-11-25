"""Protect the one ordinary expense effect of an independently confirmed receipt."""

import hashlib
import sqlite3
from contextlib import closing
from contextvars import copy_context
from dataclasses import replace

import pytest

from finance_core.instance_admission import InstanceAdmissionError, acquire_production_owner
from finance_core.instance_admission.sessions import _witness_for, lifecycle_read_operation
from finance_core.lifecycle_v2.contracts import LifecycleError
from finance_core.lifecycle_v2.daily_v1 import DailyLedgerReaderV1, DailyMovementServiceV1
from finance_core.lifecycle_v2.daily_v1.accounts import _EvidenceReadPort
from finance_core.lifecycle_v2.reader import audit_evidence_connection
from finance_core.lifecycle_v2.receipt_retained_manual_v1 import (
    ReceiptJournalReplyRefV1,
    VerifiedReceiptManualHistoryV1,
)
from finance_core.lifecycle_v2.receipt_review_fields_v1 import (
    VerifiedReceiptReviewFieldsReplyV1,
    receipt_review_fields_target_v1,
)
from finance_core.lifecycle_v2.receipt_review_preparation_v1 import (
    ReceiptReviewReaderV1,
    VerifiedReceiptPaymentReplyV1,
)
from finance_core.lifecycle_v2.receipt_snapshot_v1 import receipt_snapshot_evidence_scope
from finance_core.lifecycle_v2.recovery import verify_snapshot_evidence
from finance_core.lifecycle_v2.service import LedgerService
from tests.daily_account_support_v1 import NOW, unknown_time
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_lifecycle_v2 import live as live
from tests.test_receipt_review_preparation_v1 import setup_payment


def _position(sequence, reply):
    return ReceiptJournalReplyRefV1(
        sequence,
        reply.response_id,
        reply.response_sha256,
        reply.reply_message_id,
        reply.reply_update_id,
    )


def _setup_receipt(live):
    ledger, port, _, source_id, payment = setup_payment(
        live,
        receipt_currency="SGD",
        account_debit="7.50",
        opening={
            "amount": "100",
            "as_of": {**unknown_time(), "precision": "date", "local_date": "2026-11-18"},
        },
    )
    preparation = ReceiptReviewReaderV1(ledger).prepare(source_id)
    fields = VerifiedReceiptPaymentReplyV1(
        "fields-question",
        "5" * 64,
        4004,
        NOW + 2,
        27,
        94,
        NOW + 3,
        "fields-poll",
        "6" * 64,
        "分类：餐饮\n期初余额：未包含",
    )
    positions = tuple(
        _position(seq, item)
        for seq, item in zip(
            (3, 4, 6, 8, 10), (*port.replies, payment.selection, payment.debit, fields), strict=True
        )
    )
    history = VerifiedReceiptManualHistoryV1(
        port.receipt,
        ReceiptJournalReplyRefV1(1, "capture-poll", "e" * 64, 19, 89),
        port.replies,
        payment,
        VerifiedReceiptReviewFieldsReplyV1(
            receipt_review_fields_target_v1(preparation), fields, "user", "chat"
        ),
        positions,
    )
    fields_ref = positions[-1]

    def retained(conn, identity, reply):
        assert _witness_for(conn).evidence_callback_token is not None
        return history

    port.receipt_retained_manual_history_v1 = retained
    port.receipt_manual_fields_reply_at_v1 = lambda conn, identity, through: fields_ref
    ledger.clock = lambda: NOW + 10
    return ledger, port, source_id, fields_ref, history


class _SnapshotReceiptLookup:
    def __init__(self, cut, history, delivery=None, action=None):
        self.snapshot_cut = cut
        self._history = history
        self.delivery = delivery
        self.action = action
        self.source_probe = lambda: None

    def receipt_source_v1(self, evidence_id):
        self.source_probe()
        assert evidence_id == self._history.source.evidence_id
        return self._history.source

    def receipt_retained_manual_history_v1(self, evidence_id, fields_reply):
        assert evidence_id == self._history.source.evidence_id
        assert fields_reply == self._history.reply_positions[-1]
        return self._history

    def receipt_manual_fields_reply_at_v1(self, evidence_id, through):
        assert evidence_id == self._history.source.evidence_id
        assert through == self.action.position
        return self._history.reply_positions[-1]

    def receipt_card_delivery_v1(self, evidence_id):
        assert evidence_id == self.delivery.evidence_id
        return self.delivery

    def receipt_confirm_action_v1(self, evidence_id):
        assert evidence_id == self.action.evidence_id
        return self.action


def _verify_admitted_cut(ledger, port, cut, lookup, expected):
    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        with receipt_snapshot_evidence_scope(conn, port, ledger.binding, cut, lookup) as bound:
            assert type(bound) is _EvidenceReadPort
            assert audit_evidence_connection(conn, bound, ledger.binding) == expected
            escaped = copy_context()
        with pytest.raises(LifecycleError, match="scope was not issued or expired"):
            escaped.run(audit_evidence_connection, conn, bound, ledger.binding)

        def swallowed_commit():
            assert _witness_for(conn).evidence_callback_token is not None
            try:
                conn.commit()
            except InstanceAdmissionError:
                pass

        lookup.source_probe = swallowed_commit
        with pytest.raises(InstanceAdmissionError, match="read-only scope"):
            with receipt_snapshot_evidence_scope(conn, port, ledger.binding, cut, lookup) as bound:
                audit_evidence_connection(conn, bound, ledger.binding)
        assert conn.in_transaction
        lookup.source_probe = lambda: None
        conn.rollback()


def test_manual_receipt_independent_confirmation_posts_once_and_replays(live, tmp_path):
    from finance_core.lifecycle_v2.receipt_posting_v1 import (
        VerifiedReceiptCardDeliveryV1,
        VerifiedReceiptConfirmActionV1,
    )

    ledger, port, source_id, fields_ref, history = _setup_receipt(live)
    daily = DailyMovementServiceV1(ledger)
    with pytest.raises(LifecycleError, match="typed receipt entry"):
        daily.propose_movement_create(source_id, "text-observation", 0, "text-proposal")
    proposal = daily.propose_manual_receipt_expense(source_id, fields_ref, "receipt-propose")
    assert (
        daily.propose_manual_receipt_expense(source_id, fields_ref, "receipt-propose") == proposal
    )
    pending_path = tmp_path / "receipt-pending.sqlite"
    pending_cut = ledger.seal_snapshot(pending_path)
    assert len(pending_cut.facts) == 1  # Only the opening account.
    pending_audit = ledger.reader().full_audit()
    pending_lookup = _SnapshotReceiptLookup(pending_cut, history)
    with pytest.raises(LifecycleError, match="historical receipt snapshot evidence"):
        verify_snapshot_evidence(pending_path, port, ledger.binding, pending_cut)
    _verify_admitted_cut(ledger, port, pending_cut, pending_lookup, pending_audit)
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    delivery = VerifiedReceiptCardDeliveryV1(
        "receipt-delivery",
        card.review_id,
        card.review_sha256,
        "user",
        "chat",
        11,
        "card-response",
        "7" * 64,
        4005,
        NOW + 10,
    )
    with pytest.raises(LifecycleError, match="lacks current delivery"):
        daily.confirm(card.review_id, "receipt-action", "receipt-confirm")
    port.receipt_card_delivery_v1 = lambda conn, evidence_id: replace(
        delivery, message_id=fields_ref.message_id
    )
    with pytest.raises(LifecycleError, match="exact complete card"):
        daily.record_delivery(card.review_id, delivery.evidence_id)
    port.receipt_card_delivery_v1 = lambda conn, evidence_id: delivery
    delivered = daily.record_delivery(card.review_id, delivery.evidence_id)
    assert delivered.delivery_evidence_id == delivery.evidence_id
    before = DailyLedgerReaderV1(ledger).collect()
    assert before.balances[0].balance_amount == "100.00" and before.movements == ()

    action = VerifiedReceiptConfirmActionV1(
        "receipt-action",
        card.review_id,
        card.review_sha256,
        delivery.evidence_id,
        "user",
        "chat",
        fields_ref,
        delivery.response_id,
        delivery.response_sha256,
        delivery.message_id,
        NOW + 10,
    )
    port.receipt_confirm_action_v1 = lambda conn, evidence_id: action
    with pytest.raises(LifecycleError, match="separate original receipt confirmation"):
        daily.confirm(card.review_id, action.evidence_id, "receipt-confirm")

    action = replace(
        action,
        position=ReceiptJournalReplyRefV1(12, "confirmation-poll", "8" * 64, 28, 95),
    )
    for unrelated in (
        replace(action, principal_id="someone-else"),
        replace(action, quoted_message_id=4004),
        replace(action, quoted_response_sha256="a" * 64),
    ):
        port.receipt_confirm_action_v1 = lambda conn, evidence_id: unrelated
        with pytest.raises(LifecycleError, match="separate original receipt confirmation"):
            daily.confirm(card.review_id, action.evidence_id, "receipt-confirm")
    port.receipt_confirm_action_v1 = lambda conn, evidence_id: action
    port.receipt_manual_fields_reply_at_v1 = lambda conn, identity, through: (
        fields_ref if through == action.position else None
    )
    with pytest.raises(LifecycleError, match="stale_receipt"):
        daily.confirm(card.review_id, action.evidence_id, "receipt-confirm")
    still_unposted = DailyLedgerReaderV1(ledger).collect()
    assert still_unposted.balances[0].balance_amount == "100.00"
    assert still_unposted.movements == ()
    port.receipt_manual_fields_reply_at_v1 = lambda conn, identity, through: fields_ref
    result = daily.confirm(card.review_id, action.evidence_id, "receipt-confirm")
    assert daily.confirm(card.review_id, action.evidence_id, "receipt-confirm") == result
    assert (
        daily.lookup_commit_receipt(
            "receipt-confirm", review_id=card.review_id, action_evidence_id=action.evidence_id
        )
        == result
    )
    final = DailyLedgerReaderV1(ledger).collect()
    assert final.balances[0].balance_amount == "92.50"
    assert final.expense_by_currency == (("SGD", "8.00"),)
    assert len(final.movements) == 1
    assert final.movements[0].values["original_amount"] == "8.00"
    assert final.movements[0].values["account_amount"] == "7.50"
    assert final.movements[0].economic_event_id == port.receipt.source_key
    assert ledger.reader().receipt_source_v1(source_id) == port.receipt
    audit = ledger.reader().full_audit()
    posted_path = tmp_path / "receipt-posted.sqlite"
    posted_cut = ledger.seal_snapshot(posted_path)
    assert posted_cut.commit_seq == audit["event_count"]
    assert len(posted_cut.facts) == 2
    posted_lookup = _SnapshotReceiptLookup(posted_cut, history, delivery, action)
    _verify_admitted_cut(ledger, port, posted_cut, posted_lookup, audit)
    # A later transport change cannot replace the evidence of the original decision.
    port.receipt_manual_fields_reply_at_v1 = lambda conn, identity, through: (
        fields_ref if through == action.position else None
    )
    ledger.owner.close()
    with acquire_production_owner(live[2]) as owner:
        reopened = LedgerService(owner, port, ledger.binding, lambda: NOW + 1000)
        assert reopened.reader().full_audit() == audit
        assert (
            verify_snapshot_evidence(
                pending_path,
                port,
                ledger.binding,
                pending_cut,
                receipt_snapshot_lookup=pending_lookup,
            )
            == pending_audit
        )
        assert (
            verify_snapshot_evidence(
                posted_path, port, ledger.binding, posted_cut, receipt_snapshot_lookup=posted_lookup
            )
            == audit
        )

        posted_lookup.action = replace(action, quoted_message_id=4004)
        with pytest.raises(LifecycleError, match="separate original receipt confirmation"):
            verify_snapshot_evidence(
                posted_path, port, ledger.binding, posted_cut, receipt_snapshot_lookup=posted_lookup
            )
        posted_lookup.action = action
        corrupt_path = tmp_path / "corrupt-receipt-copy.sqlite"
        corrupt_path.write_bytes(posted_path.read_bytes())
        with closing(sqlite3.connect(corrupt_path)) as conn:
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES('injected','injected')"
            )
            conn.commit()
        modified_cut = replace(
            posted_cut, database_sha256=hashlib.sha256(corrupt_path.read_bytes()).hexdigest()
        )
        modified_lookup = _SnapshotReceiptLookup(modified_cut, history, delivery, action)

        def unexpected_lookup():
            pytest.fail("a corrupt logical cut reached receipt evidence")

        modified_lookup.source_probe = unexpected_lookup
        with pytest.raises(LifecycleError, match="database content differs from complete cut"):
            verify_snapshot_evidence(
                corrupt_path,
                port,
                ledger.binding,
                modified_cut,
                receipt_snapshot_lookup=modified_lookup,
            )
        assert (
            DailyMovementServiceV1(reopened).confirm(
                card.review_id, action.evidence_id, "receipt-confirm"
            )
            == result
        )
        assert DailyLedgerReaderV1(reopened).collect().balances[0].balance_amount == "92.50"


def test_receipt_snapshot_preserves_guarded_transfer_audit(live, transfer, tmp_path):
    from dataclasses import asdict

    from finance_core.lifecycle_v2.daily_v1 import (
        DailyAccountReaderV1,
        DailyAccountServiceV1,
        DailyTransferServiceV1,
    )
    from tests.test_daily_transfer_evidence_v1 import _retained_questions
    from tests.test_daily_transfer_opening_v1 import _account, _on

    ledger, port, source_id, fields_ref, history = _setup_receipt(live)
    accounts = DailyAccountReaderV1(ledger)
    daily = DailyAccountServiceV1(ledger)
    boundary = _on("2026-11-21")
    debit = _account(ledger, port, daily, accounts, "SGD", "transfer-out", boundary)
    credit = _account(ledger, port, daily, accounts, "MYR", "transfer-in", unknown_time())
    fee = transfer["fee"]["charge"]
    fee["funds_time"] = boundary
    for leg, account in (
        (transfer["outgoing"], debit),
        (transfer["incoming"], credit),
        (fee, debit),
    ):
        leg["account_ref"] = asdict(account.account_ref)
        leg["opening_ref"] = asdict(account.opening_ref)
        leg["coverage"]["binding"].update(
            {key: leg[key] for key in ("account_ref", "opening_ref", "funds_time")}
        )
    _retained_questions(ledger, port, transfer)
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "mixed-transfer-capture")
    transfers = DailyTransferServiceV1(ledger)
    proposal = transfers.propose_create(
        capture.source_id, transfer, "mixed-transfer-result", "mixed-transfer-proposal"
    )
    review = transfers.prepare_review(proposal.proposal_id, proposal.version)
    transfers.record_delivery(review.review_id, "transfer-display")
    transfers.confirm(review.review_id, "transfer-action", "mixed-transfer-confirm")
    DailyMovementServiceV1(ledger).propose_manual_receipt_expense(
        source_id, fields_ref, "mixed-receipt-proposal"
    )

    audit = ledger.reader().full_audit()
    assert audit["fact_version_count"] == 4  # Three accounts and the confirmed transfer.
    snapshot = tmp_path / "receipt-and-transfer.sqlite"
    cut = ledger.seal_snapshot(snapshot)
    lookup = _SnapshotReceiptLookup(cut, history)
    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        with receipt_snapshot_evidence_scope(conn, port, ledger.binding, cut, lookup) as bound:
            assert type(bound) is _EvidenceReadPort
            assert audit_evidence_connection(conn, bound, ledger.binding) == audit
        conn.rollback()
    assert (
        verify_snapshot_evidence(
            snapshot, port, ledger.binding, cut, receipt_snapshot_lookup=lookup
        )
        == audit
    )


def test_receipt_snapshot_qualification_is_exact_and_read_only(live, tmp_path):
    from finance_core.lifecycle_v2.receipt_snapshot_v1 import _ReceiptSnapshotScope

    ledger, port, source_id, fields_ref, history = _setup_receipt(live)
    DailyMovementServiceV1(ledger).propose_manual_receipt_expense(
        source_id, fields_ref, "unissued-receipt-proposal"
    )
    snapshot = tmp_path / "unissued-receipt-snapshot.sqlite"
    cut = ledger.seal_snapshot(snapshot)
    other = replace(cut, logical_database_sha256="1" * 64)
    lookup = _SnapshotReceiptLookup(other, history)
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        unissued = _ReceiptSnapshotScope(conn, port, ledger.binding, other, lookup, None)
        unissued.active = True
        with pytest.raises(LifecycleError, match="scope was not issued or expired"):
            unissued.read("receipt_source_v1", "receipt")
        with pytest.raises(LifecycleError, match="historical receipt snapshot evidence"):
            audit_evidence_connection(conn, unissued, ledger.binding)
        with pytest.raises(LifecycleError, match="database content differs from complete cut"):
            with receipt_snapshot_evidence_scope(conn, port, ledger.binding, other, lookup):
                pytest.fail("wrong logical cut was qualified")
        lookup.snapshot_cut = cut
        with receipt_snapshot_evidence_scope(conn, port, ledger.binding, cut, lookup) as bound:
            assert bound is port
            with pytest.raises(LifecycleError, match="port or binding differs"):
                audit_evidence_connection(conn, unissued, ledger.binding)
            with pytest.raises(LifecycleError, match="port or binding differs"):
                audit_evidence_connection(conn, bound, replace(ledger.binding, client_id="other"))
            assert audit_evidence_connection(conn, bound, ledger.binding)["fact_version_count"] == 1
        with pytest.raises(LifecycleError, match="historical receipt snapshot evidence"):
            audit_evidence_connection(conn, bound, ledger.binding)

    class UnadmittedLookup:
        @property
        def snapshot_cut(self):
            pytest.fail("a refused connection must not access lookup properties")

    with ledger._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(LifecycleError, match="exact admitted source read"):
            with receipt_snapshot_evidence_scope(
                conn, port, ledger.binding, cut, UnadmittedLookup()
            ):
                pytest.fail("a write connection was qualified")
        conn.rollback()

    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        witness = _witness_for(conn)
        resolved = set()

        class GuardedLookup:
            def __getattr__(self, name):
                assert witness.evidence_callback_token is not None
                resolved.add(name)
                return getattr(lookup, name)

        with receipt_snapshot_evidence_scope(
            conn, port, ledger.binding, cut, GuardedLookup()
        ) as bound:
            assert audit_evidence_connection(conn, bound, ledger.binding)["fact_version_count"] == 1
        assert {"snapshot_cut", "receipt_retained_manual_history_v1"} <= resolved
