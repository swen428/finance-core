"""Historical readback of exact signed OCR and manual receipt inputs."""

import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from functools import partial
from hashlib import sha256
from shutil import copytree

import pytest

from finance_core.instance_admission.sessions import _witness_for
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
)
from finance_core.lifecycle_v2 import LifecycleError, receipt_media_operation_id_v1
from finance_core.lifecycle_v2.contracts import digest
from finance_core.lifecycle_v2.daily_v1 import (
    DailyAccountServiceV1,
    DailyLedgerReaderV1,
    DailyMovementServiceV1,
)
from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1, EffectiveReadError
from finance_core.lifecycle_v2.reader import SnapshotEvidenceVerifier
from finance_core.lifecycle_v2.receipt_posting_v1 import (
    VerifiedReceiptCardDeliveryV1,
    VerifiedReceiptConfirmActionV1,
)
from finance_core.lifecycle_v2.receipt_retained_manual_v1 import ReceiptJournalReplyRefV1
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
from tests.daily_account_support_v1 import NOW, command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import create
from tests.test_lifecycle_v2_round3_recovery import _fence, _manual_historical_target
from tests.test_lifecycle_v2_round3_recovery import original as original
from tests.test_manual_receipt_posting_v1 import _position, _SnapshotReceiptLookup
from tests.test_receipt_media_v1 import _complete_ocr_result
from tests.test_receipt_media_v1 import seam as seam
from tests.test_receipt_review_preparation_v1 import PaymentPort, payment_for_handoff


@pytest.mark.parametrize("original", [True], indirect=True)
@pytest.mark.parametrize("case", ["complete", "partial", "no_text"])
def test_retained_ocr_expense_rebuilds_exact_fields_with_optional_manual_replies(
    seam, original, monkeypatch, case, tmp_path
):
    from finance_core.lifecycle_v2.receipt_retained_ocr_v1 import (
        ReceiptOcrMemberIdentityV1,
        RetainedReceiptOcrEvidenceV1,
        VerifiedReceiptOcrHistoryV1,
        VerifiedReceiptOcrRegistrationV1,
        _reconstruct_ocr_expense,
    )

    processor, engine, request, input_root, live_root, receipt_bytes, worker_calls = seam
    if case == "no_text":
        monkeypatch.setattr(
            engine,
            "extract",
            lambda *args, **kwargs: ReceiptOcrEngineResult(
                ReceiptOcrExtractionStatus.NO_TEXT, (), "no_text"
            ),
        )
    else:
        merchant = "SHOP\u200dNAME" if case == "partial" else "TEST SHOP"
        monkeypatch.setattr(
            engine,
            "extract",
            partial(_complete_ocr_result, merchant=merchant, total="TOTAL SGD 8.00"),
        )
    original_config, original_owner, ledger, _ = original
    port = ledger.port = PaymentPort()
    port.receipt = replace(
        port.receipt,
        original_sha256=sha256(receipt_bytes).hexdigest(),
        original_size_bytes=len(receipt_bytes),
    )
    port.replies = ()
    ledger.clock = lambda: NOW + 10
    source = ledger.capture_receipt_v1("receipt", "capture-retained-ocr")
    create(
        ledger,
        port,
        DailyAccountServiceV1(ledger),
        command(
            fields={
                "name": "Wallet",
                "account_type": "cash",
                "currency": "SGD",
                "opening": {
                    "amount": "100",
                    "as_of": {**unknown_time(), "precision": "date", "local_date": "2026-11-18"},
                },
            }
        ),
    )
    operation = receipt_media_operation_id_v1(port.receipt)
    reference = processor.process(**(request | {"operation_id": operation})).reference
    assert reference is not None
    media_input = {"processor": processor, "reference": reference}
    if case != "complete":
        reply_text = (
            "商家：Sunny Shop"
            if case == "partial"
            else ("商家：Sunny Shop\n日期：2026-10-01\n总额：8.00\n币种：SGD")
        )
        port.replies = (
            replace(
                port.answer,
                source_sha256=digest(asdict(port.receipt)),
                raw_text=reply_text,
            ),
        )
    reader = ReceiptReviewReaderV1(ledger)
    handoff = ledger.reader().receipt_payment_handoff_v1(source.source_id, **media_input)
    payment = port.payment = payment_for_handoff(source, handoff, account_debit="7.50")
    prepared = reader.prepare(source.source_id, **media_input)
    reply = VerifiedReceiptPaymentReplyV1(
        "ocr-category-question",
        "5" * 64,
        4004,
        NOW + 2,
        27,
        94,
        NOW + 3,
        "ocr-category-poll",
        "6" * 64,
        "分类：餐饮\n期初余额：未包含",
    )
    fields = port.review_reply = VerifiedReceiptReviewFieldsReplyV1(
        receipt_review_fields_target_v1(prepared), reply, "user", "chat"
    )
    positions = tuple(
        _position(sequence, response)
        for sequence, response in enumerate(
            (*port.replies, payment.selection, payment.debit, reply), 4
        )
    )
    registered = VerifiedReceiptOcrRegistrationV1(
        source_evidence_id=port.receipt.evidence_id,
        source_id=source.source_id,
        original_message_sha256=port.receipt.original_message_sha256,
        prepared_evidence_id="signed-ocr-prepared",
        prepared_evidence_sha256="1" * 64,
        registration_evidence_id="signed-ocr-closed",
        registration_evidence_sha256="2" * 64,
        reference=reference,
        parser_version=prepared.draft.ocr.suggestions.parser_version,
        members=tuple(
            ReceiptOcrMemberIdentityV1(**item)
            for item in sorted(
                processor.read_verified(reference).inventory, key=lambda item: item["name"]
            )
        ),
    )
    history = VerifiedReceiptOcrHistoryV1(
        port.receipt,
        ReceiptJournalReplyRefV1(1, "source-poll", "a" * 64, 19, 89),
        port.replies,
        payment,
        fields,
        positions,
        registered,
    )
    expected = reader.expense_confirmation_preview(source.source_id, **media_input)
    readback = RetainedReceiptOcrEvidenceV1(history, str(live_root))

    def retained(conn, evidence_id, fields_reply):
        assert _witness_for(conn).evidence_callback_token is not None
        assert evidence_id == port.receipt.evidence_id and fields_reply == positions[-1]
        return readback

    port.receipt_retained_ocr_history_v1 = retained
    live_result = reader.retained_ocr_expense(source.source_id, positions[-1])
    assert live_result.preview == expected
    assert live_result.history == history
    assert live_result.draft == handoff.draft
    origins = {field.name: field.origin for field in live_result.draft.fields}
    assert origins["merchant"] == ("ocr" if case == "complete" else "manual")
    assert origins["amount"] == ("manual" if case == "no_text" else "ocr")
    if case != "complete":
        assert live_result.draft.manual.manual_reply == port.replies[0]
    assert ledger.reader().full_audit()["fact_version_count"] == 1

    delivery, action = _post_ocr_receipt(ledger, port, source.source_id, positions[-1])
    audit = ledger.reader().full_audit()

    snapshot = tmp_path / "retained-ocr.sqlite"
    cut = ledger.seal_snapshot(snapshot)
    archived = tmp_path / "archived-ocr"
    archived.mkdir(mode=0o700)
    copytree(live_root / operation, archived / operation)
    live_root.rename(tmp_path / "original-processing-root-disconnected")
    input_root.rename(tmp_path / "original-input-root-disconnected")
    readback = RetainedReceiptOcrEvidenceV1(history, str(archived))
    lookup = _SnapshotReceiptLookup(cut, history, delivery, action)
    lookup.receipt_retained_ocr_history_v1 = lambda identity, position: readback
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
        copy.row_factory = sqlite3.Row
        copy.execute("PRAGMA query_only=ON")
        copy.execute("BEGIN")
        with receipt_snapshot_evidence_scope(copy, port, ledger.binding, cut, lookup) as bound:
            verifier = SnapshotEvidenceVerifier(bound, ledger.binding)
            result = _reconstruct_ocr_expense(
                verifier, copy, source.source_id, positions[-1], ledger_cut=live_result.ledger_cut
            )
            assert result == live_result

    lookup.receipt_ocr_fields_reply_at_v1 = lookup.receipt_manual_fields_reply_at_v1
    assert (
        verify_snapshot_evidence(
            snapshot, port, ledger.binding, cut, receipt_snapshot_lookup=lookup
        )
        == audit
    )

    if case == "complete":
        declaration = next(item for item in registered.members if item.name == "declaration.json")
        modified = replace(declaration, sha256="f" * 64)
        bad_members = tuple(
            modified if item == declaration else item for item in registered.members
        )
        readback = RetainedReceiptOcrEvidenceV1(
            replace(history, registration=replace(registered, members=bad_members)), str(archived)
        )
        with pytest.raises(LifecycleError, match="OCR members differ from the signed registration"):
            reader.retained_ocr_expense(source.source_id, positions[-1])
        readback = RetainedReceiptOcrEvidenceV1(history, str(archived) + "\0")
        with pytest.raises(LifecycleError, match="Retained receipt OCR root cannot be verified"):
            reader.retained_ocr_expense(source.source_id, positions[-1])
        readback = RetainedReceiptOcrEvidenceV1(history, str(archived))
        archived.chmod(0o750)
        try:
            with pytest.raises(EffectiveReadError) as unsafe_root:
                EffectiveLedgerReaderV1.for_daily_ledger(ledger).collect()
            assert unsafe_root.value.code == "fact_evidence_invalid"
        finally:
            archived.chmod(0o700)
        original_file = archived / operation / "original.bin"
        original_file.chmod(0o600)
        original_file.write_bytes(b"substituted original receipt bytes")
        original_file.chmod(0o400)
        with pytest.raises(
            LifecycleError, match="Retained receipt OCR evidence cannot be verified"
        ):
            reader.retained_ocr_expense(source.source_id, positions[-1])
        with pytest.raises(EffectiveReadError) as refused:
            EffectiveLedgerReaderV1.for_daily_ledger(ledger).collect()
        assert refused.value.code == "fact_evidence_invalid"
        original_file.chmod(0o600)
        original_file.write_bytes(receipt_bytes)
        original_file.chmod(0o400)
        _fence(original_config, original_owner)
        _verify_activated_receipt(
            tmp_path,
            monkeypatch,
            original_config,
            ledger,
            port,
            snapshot,
            cut,
            audit,
            lookup,
            source.source_id,
            action,
        )
    assert len(worker_calls) == 1


def _verify_activated_receipt(
    tmp_path,
    monkeypatch,
    config,
    ledger,
    port,
    snapshot,
    cut,
    audit,
    lookup,
    source_id,
    action,
):
    from finance_core.instance_admission import InstanceAdmissionError, acquire_production_owner
    from finance_core.lifecycle_v2.historical_evidence_v1 import (
        HistoricalEvidenceV1,
        HistoricalGenerationV1,
    )
    from finance_core.lifecycle_v2.recovery import promote_manual_single_host_restore_v1
    from finance_core.lifecycle_v2.service import LedgerService

    target, held, options = _manual_historical_target(
        tmp_path, monkeypatch, config, cut, snapshot, "restored-ocr", NOW + 10
    )
    history = digest(["verified OCR members and original transport"])
    archived = HistoricalGenerationV1(ledger.binding, cut, history, audit["audit_sha256"])

    def audit_application(connection, exact_cut, audit_core):
        assert connection.in_transaction and exact_cut == cut
        assert audit_core(port, ledger.binding, receipt_snapshot_lookup=lookup) == audit
        return history

    assert (
        promote_manual_single_host_restore_v1(
            held,
            historical_generations=(archived,),
            application_history_sha256=history,
            audit_application_history=audit_application,
            **options,
        )["state"]
        == "ACTIVE"
    )
    with acquire_production_owner(target) as owner:
        restored = LedgerService(
            owner,
            PaymentPort(),
            options["binding"],
            runtime_admission=options["runtime_admission"],
            historical_evidence=(HistoricalEvidenceV1(archived, port),),
        )
        original_read = port.receipt_retained_ocr_history_v1
        assert restored.reader().receipt_source_v1(source_id) == port.receipt
        assert restored.reader().full_audit() == audit
        view = DailyLedgerReaderV1(restored).collect()
        assert view.movements[0].values["account_amount"] == "7.50"
        with pytest.raises(LifecycleError):
            DailyMovementServiceV1(restored).confirm(
                action.review_id, action.evidence_id, "confirm-ocr-receipt"
            )

        def attempted_commit(conn, identity, fields_reply):
            assert _witness_for(conn).owner is owner
            with pytest.raises(InstanceAdmissionError):
                conn.commit()
            return original_read(conn, identity, fields_reply)

        port.receipt_retained_ocr_history_v1 = attempted_commit
        with pytest.raises(InstanceAdmissionError, match="read-only scope"):
            restored.reader().full_audit()
        port.receipt_retained_ocr_history_v1 = original_read

    target.close()


def _post_ocr_receipt(ledger, port, source_id, fields_reply):
    daily = DailyMovementServiceV1(ledger)
    port.receipt_ocr_fields_reply_at_v1 = lambda conn, identity, through: fields_reply
    proposed = daily.propose_ocr_receipt_expense(source_id, fields_reply, "propose-ocr-receipt")
    assert proposed.version == 1
    assert (
        daily.propose_ocr_receipt_expense(source_id, fields_reply, "propose-ocr-receipt")
        == proposed
    )
    with pytest.raises(LifecycleError, match="exact typed predecessor"):
        daily.propose_manual_receipt_expense(source_id, fields_reply, "downgrade-ocr-receipt")
    card = daily.prepare_review(proposed.proposal_id, proposed.version)
    assert card.material["format"] == "finance-daily-ocr-receipt-review-v1"
    assert card.material["after_values"]["original_amount"] == "8.00"
    assert card.material["after_values"]["account_amount"] == "7.50"
    delivery = VerifiedReceiptCardDeliveryV1(
        "ocr-delivery",
        card.review_id,
        card.review_sha256,
        "user",
        "chat",
        fields_reply.sequence + 1,
        "ocr-card-response",
        "7" * 64,
        4005,
        NOW + 10,
    )
    port.receipt_card_delivery_v1 = lambda conn, identity: delivery
    daily.record_delivery(card.review_id, delivery.evidence_id)
    action = VerifiedReceiptConfirmActionV1(
        "ocr-approval",
        card.review_id,
        card.review_sha256,
        delivery.evidence_id,
        "user",
        "chat",
        fields_reply,
        delivery.response_id,
        delivery.response_sha256,
        delivery.message_id,
        NOW + 10,
    )
    port.receipt_confirm_action_v1 = lambda conn, identity: action
    with pytest.raises(LifecycleError, match="separate original receipt confirmation"):
        daily.confirm(card.review_id, action.evidence_id, "confirm-ocr-receipt")
    before = DailyLedgerReaderV1(ledger).collect()
    assert before.movements == () and before.balances[0].balance_amount == "100.00"
    action = replace(
        action,
        position=ReceiptJournalReplyRefV1(
            fields_reply.sequence + 2, "ocr-confirm-poll", "8" * 64, 28, 95
        ),
    )
    confirmed = daily.confirm(card.review_id, action.evidence_id, "confirm-ocr-receipt")
    assert daily.confirm(card.review_id, action.evidence_id, "confirm-ocr-receipt") == confirmed
    after = DailyLedgerReaderV1(ledger).collect()
    assert len(after.movements) == 1
    assert after.expense_by_currency == (("SGD", "8.00"),)
    assert after.balances[0].balance_amount == "92.50"
    return delivery, action
