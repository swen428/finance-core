"""Synthetic seam unit proofs; fake decoder/engine never certify Linux support."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import warnings
import zlib
from dataclasses import asdict, replace
from decimal import localcontext

import pytest
from PIL import Image

from finance_core.intake import receipt_media as media
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
)
from tests.test_lifecycle_v2 import live as live


class RecordingOcr:
    identity = ReceiptOcrEngineIdentity("synthetic", "1", "a" * 64, "b" * 64)

    def __init__(self):
        self.calls = 0
        self.seen = []
        self.fail = False

    def extract(self, source, *, limits, deadline):
        self.calls += 1
        self.seen.append(
            (source.content_hash, source.mime_type, os.read(source.file_descriptor, 100_000))
        )
        if self.fail:
            raise RuntimeError("Synthetic fault")
        block = ReceiptOcrBlock(0, 0, 0, 0, 0, 0, "TOTAL 12.50", 0, 0, 2, 2, 4, 3, 9500)
        return ReceiptOcrEngineResult(ReceiptOcrExtractionStatus.SUCCEEDED, (block,), "ok")


@pytest.fixture
def seam(tmp_path, monkeypatch):
    source = tmp_path / "source"
    evidence = tmp_path / "evidence"
    source.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    image = Image.new("RGB", (4, 3), (20, 40, 80))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    raw = buffer.getvalue()
    original = source / "capture.png"
    original.write_bytes(raw)
    original.chmod(0o400)
    worker_calls = []
    identity = {
        "packages": {"Pillow": "13.0.0", "pillow-heif": "1.8.0"},
        "installed_files_sha256": "c" * 64,
        "python_prefix": sys.prefix,
        "python_base_prefix": sys.base_prefix,
    }

    def worker(arguments, *, pass_fds, directory, deadline):
        if "--identity" in arguments:
            return {"status": "identity", "decoder": identity}
        worker_calls.append(arguments)
        output = int(arguments[arguments.index("--output-fd") + 1])
        os.write(output, raw)
        return {
            "status": "normalized",
            "decoder": identity,
            "normalization": {
                "detected_mime_type": "image/png",
                "source_dimensions": [4, 3],
                "dimensions": [4, 3],
                "original_bit_depth": 8,
                "output_mode": "RGB",
                "alpha_policy": "white_matte",
                "source_has_alpha": False,
                "convert_hdr_to_8bit": True,
                "container_transformations": [],
                "exif_orientation": None,
                "xmp_orientation": None,
                "orientation_policy": "raster_exif_once_metadata_removed",
                "color_profile": {},
                "frame_count": 1,
                "decoded_bytes": 36,
            },
        }

    monkeypatch.setattr(media, "_worker_run", worker)
    engine = RecordingOcr()
    processor = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    request = {
        "operation_id": "media_unit",
        "source_relative_path": "capture.png",
        "expected_source_sha256": hashlib.sha256(raw).hexdigest(),
        "expected_source_size": len(raw),
        "declared_mime_type": "image/png",
        "original_filename": "capture.png",
        "received_via": "telegram_photo",
    }
    return processor, engine, request, source, evidence, raw, worker_calls


def _complete_ocr_result(
    _source, *, limits, deadline, merchant="TEST SHOP", total="TOTAL RM 12.34"
):
    lines = (merchant, "Date 2026-10-01", total)
    blocks = tuple(
        ReceiptOcrBlock(i, 0, 0, 0, i, 0, line, 0, 0, 2, 2, 4, 3, 9500)
        for i, line in enumerate(lines)
    )
    return ReceiptOcrEngineResult(ReceiptOcrExtractionStatus.SUCCEEDED, blocks, "ok")


def test_recorded_receipt_fields_are_bound_to_one_original_message(seam, live, monkeypatch):
    from finance_core.lifecycle_v2 import (
        LifecycleError,
        VerifiedReceiptSourceV1,
        receipt_media_operation_id_v1,
    )
    from finance_core.lifecycle_v2.contracts import digest
    from finance_core.lifecycle_v2.receipt_draft_v1 import VerifiedReceiptManualReplyV1

    processor, engine, request, source_root, evidence_root, original, _ = seam
    service, port, _ = live

    monkeypatch.setattr(engine, "extract", _complete_ocr_result)

    def receipt(conn, evidence_id):
        return VerifiedReceiptSourceV1(
            evidence_id,
            "user",
            "chat",
            "receipt:v1:" + evidence_id,
            "",
            hashlib.sha256(original).hexdigest(),
            1792800000,
            len(original),
            hashlib.sha256(evidence_id.encode()).hexdigest(),
        )

    port.receipt_source_v1 = receipt
    manual_replies = ()

    def receipt_replies(connection, source):
        assert connection.in_transaction
        return manual_replies if source.evidence_id == "message-1" else ()

    port.receipt_manual_replies_v1 = receipt_replies
    first = service.capture_receipt_v1("message-1", "capture-receipt-1")
    second = service.capture_receipt_v1("message-2", "capture-receipt-2")
    reader = service.reader()
    job = receipt_media_operation_id_v1(reader.receipt_source_v1(first.source_id))
    captured = processor.process(**(request | {"operation_id": job}))
    assert captured.reference is not None
    result = reader.receipt_fields_v1(first.source_id, processor, captured.reference)
    assert result.source_id == first.source_id
    assert result.original_message_sha256 == receipt(None, "message-1").original_message_sha256
    assert result.suggestions.reference == captured.reference
    fields = result.suggestions.fields
    assert fields.amount.value == "12.34" and fields.currency.value == "MYR"
    assert fields.merchant.value == "TEST SHOP"
    assert fields.transaction_date.value == "2026-10-01"
    assert fields.amount.block_sequence_indexes == (2,)

    missing = reader.receipt_whole_draft_v1(first.source_id)
    assert missing.fields == ()
    assert missing.missing_receipt_fields == ("merchant", "date", "amount", "currency")
    proposed = reader.receipt_whole_draft_v1(
        first.source_id, processor=processor, reference=captured.reference
    )
    assert proposed.ocr == result and proposed.manual is None
    assert proposed.missing_receipt_fields == ()
    assert [(f.name, f.value, f.origin) for f in proposed.fields] == [
        ("merchant", "TEST SHOP", "ocr"),
        ("date", "2026-10-01", "ocr"),
        ("amount", "12.34", "ocr"),
        ("currency", "MYR", "ocr"),
    ]
    audit = reader.full_audit()
    assert reader.receipt_payment_handoff_v1(first.source_id) is None
    handoff = reader.receipt_payment_handoff_v1(
        first.source_id, processor=processor, reference=captured.reference
    )
    assert handoff.state == "NO_ACCOUNT_OPTIONS"
    assert handoff.missing_payment_fields == ("account_id", "account_amount")
    assert handoff.draft == proposed
    manual_reply = VerifiedReceiptManualReplyV1(
        "message-1",
        digest(asdict(proposed.source)),
        "user",
        "chat",
        "receipt-fields-1",
        10,
        11,
        21,
        1792800000,
        "receipt-response-1",
        "b" * 64,
        "商家：Sunny Shop\n币种：JPY",
    )
    manual_replies = (manual_reply,)
    awaiting = reader.receipt_whole_draft_v1(
        first.source_id, processor=processor, reference=captured.reference
    )
    assert awaiting.missing_receipt_fields == ("amount",)
    assert awaiting.manual.manual_reply == manual_reply
    assert awaiting.ocr.suggestions.fields.amount.block_sequence_indexes == (2,)
    assert (
        reader.receipt_payment_handoff_v1(
            first.source_id, processor=processor, reference=captured.reference
        )
        is None
    )
    supplement = replace(
        manual_reply,
        reply_message_id=12,
        reply_update_id=22,
        response_id="receipt-response-2",
        response_sha256="c" * 64,
        raw_text="总额：1200",
    )
    manual_replies = (manual_reply, supplement)
    finished = reader.receipt_whole_draft_v1(
        first.source_id, processor=processor, reference=captured.reference
    )
    assert finished.ocr == result and finished.manual.manual_supplements == (supplement,)
    assert finished.missing_receipt_fields == ()
    assert [(f.name, f.value, f.origin) for f in finished.fields] == [
        ("merchant", "Sunny Shop", "manual"),
        ("date", "2026-10-01", "ocr"),
        ("amount", "1200", "manual"),
        ("currency", "JPY", "manual"),
    ]
    supplemented = reader.receipt_payment_handoff_v1(
        first.source_id, processor=processor, reference=captured.reference
    )
    assert supplemented.draft == finished
    assert supplemented.read_identity == handoff.read_identity
    assert reader.receipt_whole_draft_v1(first.source_id).missing_receipt_fields == ("date",)
    with pytest.raises(media.MediaIntegrityError, match="Verified original size differs"):
        processor.read_total_fields_v1(captured.reference, expected_original_size=len(original) + 1)
    with pytest.raises(LifecycleError, match="different receipt source"):
        reader.receipt_fields_v1(second.source_id, processor, captured.reference)
    with pytest.raises(LifecycleError, match="different receipt source"):
        reader.receipt_whole_draft_v1(
            second.source_id, processor=processor, reference=captured.reference
        )
    with pytest.raises(LifecycleError, match="different receipt source"):
        reader.receipt_payment_handoff_v1(
            second.source_id, processor=processor, reference=captured.reference
        )
    with pytest.raises(LifecycleError, match="processor and reference together"):
        reader.receipt_whole_draft_v1(first.source_id, reference=captured.reference)
    archive_root = evidence_root.parent / "retained"
    archive_root.mkdir(mode=0o700)
    shutil.copytree(evidence_root / job, archive_root / job)
    evidence_root.rename(evidence_root.with_name("disconnected-evidence"))
    source_root.rename(source_root.with_name("disconnected-original"))
    assert reader.receipt_retained_fields_v1(first.source_id, archive_root, captured.reference) == (
        result
    )
    with pytest.raises(LifecycleError, match="different receipt source"):
        reader.receipt_retained_fields_v1(second.source_id, archive_root, captured.reference)
    assert (
        reader.receipt_retained_whole_draft_v1(first.source_id, archive_root, captured.reference)
        == finished
    )
    with pytest.raises(LifecycleError, match="different receipt source"):
        reader.receipt_retained_whole_draft_v1(second.source_id, archive_root, captured.reference)
    assert reader.full_audit() == audit and audit["fact_version_count"] == 0


def test_receipt_review_binds_payment_to_the_exact_verified_whole_draft(seam, live, monkeypatch):
    from finance_core.lifecycle_v2 import LifecycleError, receipt_media_operation_id_v1
    from finance_core.lifecycle_v2.contracts import digest
    from finance_core.lifecycle_v2.daily_v1 import DailyAccountServiceV1
    from finance_core.lifecycle_v2.receipt_review_fields_v1 import (
        VerifiedReceiptReviewFieldsReplyV1,
        receipt_review_fields_target_v1,
    )
    from finance_core.lifecycle_v2.receipt_review_preparation_v1 import (
        ReceiptReviewReaderV1,
        VerifiedReceiptPaymentReplyV1,
    )
    from tests.daily_account_support_v1 import NOW, command
    from tests.test_daily_accounts_lifecycle_v1 import create
    from tests.test_receipt_review_preparation_v1 import PaymentPort, payment_for_handoff

    processor, engine, request, source_root, evidence_root, original, _ = seam
    monkeypatch.setattr(engine, "extract", _complete_ocr_result)
    ledger, _, _ = live
    port = ledger.port = PaymentPort()
    port.receipt = replace(
        port.receipt,
        original_sha256=hashlib.sha256(original).hexdigest(),
        original_size_bytes=len(original),
    )
    port.replies = ()
    ledger.clock = lambda: NOW
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    daily = DailyAccountServiceV1(ledger)
    fields = {"name": "Wallet", "account_type": "cash", "currency": "SGD", "opening": None}
    create(ledger, port, daily, command(fields=fields))
    operation = receipt_media_operation_id_v1(port.receipt)
    reference = processor.process(**(request | {"operation_id": operation})).reference
    assert reference is not None
    media_input = {"processor": processor, "reference": reference}
    read = ReceiptReviewReaderV1(ledger)
    audit = ledger.reader().full_audit()
    assert read.prepare(source.source_id, **media_input) is None
    handoff = ledger.reader().receipt_payment_handoff_v1(source.source_id, **media_input)
    assert handoff.draft.manual is None
    port.payment = payment_for_handoff(source, handoff)
    prepared = read.prepare(source.source_id, **media_input)
    assert prepared.draft == handoff.draft
    assert prepared.draft.ocr.suggestions.reference == reference
    assert (prepared.original_amount, prepared.original_currency) == ("12.34", "MYR")
    assert (prepared.account_amount, prepared.account_currency) == ("10.37", "SGD")
    assert read.prepare(source.source_id) is None
    reply = VerifiedReceiptPaymentReplyV1(
        "category-question",
        "5" * 64,
        4004,
        NOW + 2,
        27,
        94,
        NOW + 3,
        "category-poll",
        "6" * 64,
        "分类：餐饮",
    )
    port.review_reply = VerifiedReceiptReviewFieldsReplyV1(
        receipt_review_fields_target_v1(prepared), reply, "user", "chat"
    )
    completed = read.completed(source.source_id, **media_input)
    assert completed.preparation == prepared and completed.category == "餐饮"
    assert completed.missing_movement_fields == ()

    archive_root = evidence_root.parent / "retained-payment"
    archive_root.mkdir(mode=0o700)
    shutil.copytree(evidence_root / operation, archive_root / operation)
    evidence_root.rename(evidence_root.with_name("disconnected-payment-evidence"))
    source_root.rename(source_root.with_name("disconnected-payment-original"))
    archive_input = {"retained_evidence_root": archive_root, "reference": reference}
    with pytest.raises(LifecycleError, match="only the retained receipt evidence"):
        read.prepare(source.source_id, retained_evidence_root=archive_root, **media_input)
    with pytest.raises(LifecycleError, match="only the retained receipt evidence"):
        read.prepare(source.source_id, retained_evidence_root=archive_root)
    assert ledger.reader().receipt_payment_handoff_v1(source.source_id, **archive_input) == handoff
    assert read.prepare(source.source_id, **archive_input) == prepared
    assert read.completed(source.source_id, **archive_input) == completed
    candidate = read.expense_candidate(source.source_id, **archive_input)
    assert candidate.original_media_sha256 == reference.original_sha256
    assert candidate.original_message_sha256 == prepared.source.original_message_sha256
    assert candidate.source_sha256 == digest(asdict(prepared.source))
    assert candidate.preparation_sha256 == prepared.preparation_sha256
    assert candidate.fields_reply_sha256 == digest(asdict(completed.evidence))
    values = json.loads(candidate.values_json)
    assert (values["original_amount"], values["original_currency"]) == ("12.34", "MYR")
    assert (values["account_amount"], values["account_currency"]) == ("10.37", "SGD")
    assert values["category"] == "餐饮" and values["funds_time"]["precision"] == "unknown"

    port.replies = (
        replace(
            port.answer,
            source_sha256=digest(asdict(port.receipt)),
            raw_text="商家：Corrected Shop",
        ),
    )
    with pytest.raises(LifecycleError, match="differs from original draft"):
        read.prepare(source.source_id, **archive_input)
    assert ledger.reader().full_audit() == audit and audit["fact_version_count"] == 1


def test_ocr_draft_allows_manual_repair_before_payment_and_review(seam, live, monkeypatch):
    from functools import partial

    from finance_core.lifecycle_v2 import receipt_media_operation_id_v1
    from finance_core.lifecycle_v2.contracts import digest
    from finance_core.lifecycle_v2.daily_v1 import DailyAccountServiceV1
    from finance_core.lifecycle_v2.receipt_review_preparation_v1 import ReceiptReviewReaderV1
    from tests.daily_account_support_v1 import NOW, command
    from tests.test_daily_accounts_lifecycle_v1 import create
    from tests.test_receipt_review_preparation_v1 import PaymentPort, payment_for_handoff

    processor, engine, request, _, _, original, _ = seam
    merchant = "SHOP\u200dNAME"
    monkeypatch.setattr(
        engine,
        "extract",
        partial(_complete_ocr_result, merchant=merchant, total="TOTAL JPY 1200"),
    )
    ledger, _, _ = live
    port = ledger.port = PaymentPort()
    port.receipt = replace(
        port.receipt,
        original_sha256=hashlib.sha256(original).hexdigest(),
        original_size_bytes=len(original),
    )
    port.replies = ()
    ledger.clock = lambda: NOW
    source = ledger.capture_receipt_v1("receipt", "capture-receipt")
    daily = DailyAccountServiceV1(ledger)
    fields = {"name": "Wallet", "account_type": "cash", "currency": "SGD", "opening": None}
    create(ledger, port, daily, command(fields=fields))
    operation = receipt_media_operation_id_v1(port.receipt)
    reference = processor.process(**(request | {"operation_id": operation})).reference
    assert reference is not None
    media_input = {"processor": processor, "reference": reference}
    reader = ledger.reader()
    review = ReceiptReviewReaderV1(ledger)

    original_draft = reader.receipt_whole_draft_v1(source.source_id, **media_input)
    assert original_draft.ocr.suggestions.fields.merchant.value == merchant
    assert original_draft.missing_receipt_fields == ("merchant",)
    assert reader.receipt_payment_handoff_v1(source.source_id, **media_input) is None

    first_reply = replace(
        port.answer,
        source_sha256=digest(asdict(port.receipt)),
        raw_text="商家：Sunny Shop\n总额：12.34",
    )
    port.replies = (first_reply,)
    incomplete = reader.receipt_whole_draft_v1(source.source_id, **media_input)
    assert incomplete.missing_receipt_fields == ("amount",)
    assert reader.receipt_payment_handoff_v1(source.source_id, **media_input) is None
    assert review.prepare(source.source_id, **media_input) is None

    corrected_reply = replace(
        first_reply,
        reply_message_id=first_reply.reply_message_id + 1,
        reply_update_id=first_reply.reply_update_id + 1,
        response_id="receipt-corrected-amount",
        response_sha256="e" * 64,
        raw_text="总额：1200",
    )
    port.replies = (first_reply, corrected_reply)
    combined = reader.receipt_whole_draft_v1(source.source_id, **media_input)
    assert combined.ocr == original_draft.ocr
    assert combined.manual.manual_reply == first_reply
    assert combined.manual.manual_supplements == (corrected_reply,)
    assert combined.missing_receipt_fields == ()
    assert [
        (field.name, field.value, field.origin)
        for field in combined.fields
        if field.name in {"amount", "currency"}
    ] == [("amount", "1200", "manual"), ("currency", "JPY", "ocr")]
    handoff = reader.receipt_payment_handoff_v1(source.source_id, **media_input)
    assert handoff.draft == combined
    port.payment = payment_for_handoff(source, handoff)
    prepared = review.prepare(source.source_id, **media_input)
    assert prepared.draft == combined
    assert (prepared.merchant, prepared.original_amount, prepared.original_currency) == (
        "Sunny Shop",
        "1200",
        "JPY",
    )
    assert (prepared.account_amount, prepared.account_currency) == ("10.37", "SGD")
    assert reader.full_audit()["fact_version_count"] == 1


def test_exact_png_ocr_reference_and_idempotent_replay(seam):
    processor, engine, request, source, evidence, raw, worker_calls = seam
    first = processor.process(**request)
    assert first.status == "succeeded"
    assert first.reference is not None
    verified = processor.read_verified(first.reference)
    assert verified.blocks[0].text == "TOTAL 12.50"
    assert engine.seen == [(hashlib.sha256(raw).hexdigest(), "image/png", raw)]
    assert {item["name"] for item in verified.inventory} == {
        "intent.json",
        "original.bin",
        "declaration.json",
        "normalized.png",
        "ocr.json",
        "result.json",
    }
    assert (
        (evidence / "media_unit" / "original.bin").read_bytes()
        == (source / "capture.png").read_bytes()
        == raw
    )
    assert all(
        (evidence / "media_unit" / item["name"]).stat().st_mode & 0o777 == 0o400
        for item in verified.inventory
    )
    second = processor.process(**request)
    assert second.reference == first.reference
    assert second.persistence_idempotent
    assert engine.calls == len(worker_calls) == 1


def test_read_retained_fields_with_the_producer_and_decoder_unavailable(seam, monkeypatch):
    processor, engine, request, source, evidence, raw, worker_calls = seam
    reference = processor.process(**request).reference
    assert reference is not None
    expected = processor.read_total_fields_v1(reference, expected_original_size=len(raw))
    staged = evidence.parent / "detached"
    staged.mkdir(mode=0o700)
    operation = staged / reference.operation_id
    shutil.copytree(evidence / reference.operation_id, operation)
    source.rename(source.with_name("source-removed"))
    evidence.rename(evidence.with_name("producer-removed"))

    def refuse_worker(*_args, **_kwargs):
        raise AssertionError("Archive verification must not start a decoder")

    monkeypatch.setattr(media, "_worker_run", refuse_worker)
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in operation.iterdir()
    }
    retained = media.read_retained_media_total_fields_v1(
        staged, reference, expected_original_size=len(raw)
    )
    assert retained == expected
    assert engine.calls == len(worker_calls) == 1
    assert list(staged.iterdir()) == [operation]
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in operation.iterdir()
    } == before
    with pytest.raises(media.MediaIntegrityError, match="Verified original size differs"):
        media.read_retained_media_total_fields_v1(
            staged, reference, expected_original_size=len(raw) + 1
        )
    (operation / "ocr.json").unlink()
    with pytest.raises(media.MediaIntegrityError, match="member is missing"):
        media.read_retained_media_total_fields_v1(
            staged, reference, expected_original_size=len(raw)
        )


def test_media_reads_reject_inexact_references_and_staged_manifest_tampering(seam):
    processor, _, request, _, evidence, raw, _ = seam
    reference = processor.process(**request).reference
    assert reference is not None

    class ReferenceSubclass(media.MediaOcrReference):
        pass

    class HashSubclass(str):
        pass

    for malformed in (
        ReferenceSubclass(**asdict(reference)),
        replace(reference, manifest_sha256=HashSubclass(reference.manifest_sha256)),
    ):
        with pytest.raises(media.ReceiptMediaError, match="Media reference is malformed"):
            processor.read_verified(malformed)
        with pytest.raises(media.ReceiptMediaError, match="Media reference is malformed"):
            media.read_retained_media_total_fields_v1(
                evidence, malformed, expected_original_size=len(raw)
            )

    manifest = evidence / reference.operation_id / "result.json"
    manifest.chmod(0o600)
    manifest.write_bytes(b'{"nested":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}")
    manifest.chmod(0o400)
    with pytest.raises(media.MediaIntegrityError, match="does not bind"):
        media.read_retained_media_total_fields_v1(
            evidence, reference, expected_original_size=len(raw)
        )


@pytest.mark.parametrize(
    "sample",
    [
        "receipt",
        "joined_rm",
        "matching_currencies",
        "grouped_total",
        "whole_total",
        "colon_after_label",
        "no_text",
        "conflicting_totals",
        "incomplete_tsv",
        "signed_before_rm",
        "parenthesized",
        "parenthesized_currency_after",
        "trailing_minus",
        "trailing_minus_with_text",
        "trailing_minus_before_currency",
        "fullwidth_minus",
        "split_decimal",
        "split_integer",
        "conflicting_currencies",
        "conflicting_amounts_on_line",
        "amount_too_large",
        "malformed_scale",
        "malformed_group",
        "time_like_amount",
        "percentage",
        "unknown_currency_affix",
        "parenthesized_currency_conflict",
        "punctuated_currency_conflict",
        "negative_dash",
    ],
)
def test_read_total_fields_from_verified_local_receipt(seam, monkeypatch, sample):
    _, engine, request, source, evidence, raw, worker_calls = seam
    calls = []

    def extract(ocr_source, *, limits, deadline):
        calls.append(ocr_source.content_hash)
        if sample == "no_text":
            return ReceiptOcrEngineResult(ReceiptOcrExtractionStatus.NO_TEXT, (), "no_text")
        total = {
            "joined_rm": "TOTAL RM12.34",
            "matching_currencies": "TOTAL RM 12.34 MYR",
            "grouped_total": "TOTAL RM 1,234.50",
            "whole_total": "TOTAL RM 12",
            "colon_after_label": "fit GRAND TOTAL: RM12.34",
            "signed_before_rm": "TOTAL - RM12.34",
            "parenthesized": "TOTAL ( RM12.34 )",
            "parenthesized_currency_after": "TOTAL ( 12.34 MYR )",
            "trailing_minus": "TOTAL RM12.34-",
            "trailing_minus_with_text": "TOTAL RM 12.34- (REFUND)",
            "trailing_minus_before_currency": "TOTAL RM 12.34- MYR",
            "fullwidth_minus": "TOTAL － RM12.34",
            "split_decimal": "TOTAL RM 12 . 34",
            "split_integer": "TOTAL RM 1 234",
            "conflicting_currencies": "TOTAL RM 12.34 USD",
            "conflicting_amounts_on_line": "TOTAL RM 12.34 RM 15.00",
            "amount_too_large": "TOTAL RM 999999999999999999999999999.00",
            "malformed_scale": "TOTAL RM 12.345",
            "malformed_group": "TOTAL RM 12,34",
            "time_like_amount": "TOTAL RM 12:34",
            "percentage": "TOTAL RM 12.34%",
            "unknown_currency_affix": "TOTAL AUS$12.34",
            "parenthesized_currency_conflict": "TOTAL RM 12.34 (USD)",
            "punctuated_currency_conflict": "TOTAL USD: 12.34 RM",
            "negative_dash": "TOTAL RM 12.34– (REFUND)",
        }.get(sample, "TOTAL RM 12.34")
        lines = ["TEST SHOP", "Date 2026-10-01", total]
        if sample in {
            "conflicting_totals",
            "conflicting_amounts_on_line",
            "split_decimal",
            "malformed_scale",
            "malformed_group",
        }:
            lines.append("TOTAL RM 15.00")
        blocks = []
        for i, line in enumerate(lines):
            words = line.split() if i == 2 and sample.startswith("split_") else [line]
            for word_index, word in enumerate(words):
                blocks.append(
                    ReceiptOcrBlock(
                        len(blocks), 0, 0, 0, i, word_index, word, 0, 0, 2, 2, 4, 3, 9500
                    )
                )
        if sample == "incomplete_tsv":
            blocks[-1] = replace(blocks[-1], engine_line_index=None)
        return ReceiptOcrEngineResult(ReceiptOcrExtractionStatus.SUCCEEDED, tuple(blocks), "ok")

    monkeypatch.setattr(engine, "extract", extract)
    processor = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    result = processor.process(**request)
    assert result.reference is not None
    original = processor.read_verified(result.reference)
    if sample == "incomplete_tsv":
        with pytest.raises(media.ReceiptMediaError, match="no complete TSV layout"):
            processor.read_total_fields_v1(result.reference)
        assert processor.read_verified(result.reference) == original
        assert len(calls) == len(worker_calls) == 1
        return
    with localcontext() as context:
        if sample == "joined_rm":
            context.prec = 3
        suggested = processor.read_total_fields_v1(result.reference)
    assert suggested.reference == result.reference
    assert suggested.reference.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert suggested.parser_version == "media-fields-v1"
    fields = suggested.fields
    if sample == "no_text":
        assert suggested.ocr_status is ReceiptOcrExtractionStatus.NO_TEXT
        for field in (fields.amount, fields.currency, fields.transaction_date, fields.merchant):
            assert field.value is None and not field.block_sequence_indexes
        assert fields.ambiguity_flags == ("ocr_no_text",)
    else:
        assert suggested.ocr_status is ReceiptOcrExtractionStatus.SUCCEEDED
        assert fields.merchant.value == "TEST SHOP"
        assert fields.merchant.block_sequence_indexes == (0,)
        assert fields.transaction_date.value == "2026-10-01"
        assert fields.transaction_date.block_sequence_indexes == (1,)
        expected = {
            "receipt": ("12.34", "TOTAL RM 12.34"),
            "joined_rm": ("12.34", "TOTAL RM12.34"),
            "matching_currencies": ("12.34", "TOTAL RM 12.34 MYR"),
            "grouped_total": ("1234.50", "TOTAL RM 1,234.50"),
            "whole_total": ("12.00", "TOTAL RM 12"),
            "colon_after_label": ("12.34", "fit GRAND TOTAL: RM12.34"),
        }
        if sample in expected:
            assert (fields.amount.value, fields.amount.excerpt) == expected[sample]
            assert fields.currency.value == "MYR"
            assert fields.amount.block_sequence_indexes == (2,)
            assert fields.currency.block_sequence_indexes == (2,)
            assert not fields.ambiguity_flags
        elif sample in {"conflicting_totals", "conflicting_currencies"}:
            assert fields.amount.value is None and fields.currency.value is None
            assert fields.ambiguity_flags == ("conflicting_total_candidates",)
        elif sample == "amount_too_large":
            assert fields.amount.value is None and fields.currency.value == "MYR"
            assert fields.ambiguity_flags == ("total_amount_invalid",)
        else:
            assert fields.amount.value is None and fields.currency.value is None
            assert fields.ambiguity_flags == ("total_amount_invalid",)
    assert processor.read_verified(result.reference) == original
    with pytest.raises(media.MediaIntegrityError):
        processor.read_total_fields_v1(replace(result.reference, original_sha256="f" * 64))
    assert len(calls) == len(worker_calls) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("declared_mime_type", "image/jpeg"),
        ("original_filename", "different.png"),
        ("received_via", "telegram_document"),
    ],
)
def test_changed_operation_material_refuses_reuse(seam, key, value):
    processor, engine, request, _, _, _, worker_calls = seam
    processor.process(**request)
    with pytest.raises(media.MediaOperationConflictError):
        processor.process(**{**request, key: value})
    assert engine.calls == len(worker_calls) == 1


def test_current_original_mutation_refuses_replay(seam):
    processor, _, request, source, _, raw, _ = seam
    processor.process(**request)
    original = source / "capture.png"
    original.chmod(0o600)
    original.write_bytes(raw[:-1] + b"x")
    original.chmod(0o400)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)


@pytest.mark.parametrize(
    "member", ["original.bin", "normalized.png", "ocr.json", "result.json", "intent.json"]
)
def test_tampered_bundle_member_refused(seam, member):
    processor, _, request, _, evidence, _, _ = seam
    reference = processor.process(**request).reference
    path = evidence / "media_unit" / member
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"x")
    path.chmod(0o400)
    with pytest.raises(media.MediaIntegrityError):
        processor.read_verified(reference)


def test_wrong_missing_and_forged_reference_refused(seam):
    processor, _, request, _, _, _, _ = seam
    reference = processor.process(**request).reference
    with pytest.raises(media.MediaIntegrityError):
        processor.read_verified(replace(reference, png_sha256="d" * 64))
    with pytest.raises(media.MediaIntegrityError):
        processor.read_verified(replace(reference, operation_id="media_absent"))
    with pytest.raises(media.ReceiptMediaError):
        processor.read_verified({"operation_id": "media_unit"})


def test_terminal_ocr_failure_is_preserved_without_retry(seam):
    processor, engine, request, _, evidence, raw, worker_calls = seam
    engine.fail = True
    first = processor.process(**request)
    assert (first.status, first.outcome_code, first.reference) == (
        "engine_failed",
        "ocr_failed",
        None,
    )
    terminal = (evidence / "media_unit" / "result.json").read_bytes()
    engine.fail = False
    second = processor.process(**request)
    assert second.status == first.status
    assert second.persistence_idempotent
    assert engine.calls == len(worker_calls) == 1
    assert (evidence / "media_unit" / "result.json").read_bytes() == terminal
    assert (evidence / "media_unit" / "original.bin").read_bytes() == raw


@pytest.mark.parametrize(
    "stage",
    ["after_intent", "after_original", "after_normalization", "after_ocr", "before_terminal"],
)
def test_interrupted_operation_unknown_never_reruns(seam, monkeypatch, stage):
    processor, engine, request, source, evidence, raw, worker_calls = seam

    def interrupt(observed):
        if observed == stage:
            raise KeyboardInterrupt("Synthetic crash")

    monkeypatch.setattr(media, "_failure_injection_hook", interrupt)
    with pytest.raises(KeyboardInterrupt):
        processor.process(**request)
    calls = (engine.calls, len(worker_calls))
    monkeypatch.setattr(media, "_failure_injection_hook", None)
    second = processor.process(**request)
    assert (second.status, second.outcome_code) == ("unknown", "incomplete_operation")
    assert (engine.calls, len(worker_calls)) == calls
    assert (source / "capture.png").read_bytes() == raw
    assert not (evidence / "media_unit" / "result.json").exists()


def test_lost_success_response_replays_complete_evidence(seam, monkeypatch):
    processor, engine, request, _, _, _, worker_calls = seam

    def interrupt(stage):
        if stage == "after_terminal":
            raise KeyboardInterrupt("Synthetic response loss")

    monkeypatch.setattr(media, "_failure_injection_hook", interrupt)
    with pytest.raises(KeyboardInterrupt):
        processor.process(**request)
    monkeypatch.setattr(media, "_failure_injection_hook", None)
    recovered = processor.process(**request)
    assert recovered.status == "succeeded" and recovered.persistence_idempotent
    assert engine.calls == len(worker_calls) == 1


def test_source_path_symlink_and_root_replacement_refuse(seam):
    processor, _, request, source, evidence, _, _ = seam
    (source / "linked.png").symlink_to("capture.png")
    with pytest.raises(OSError):
        processor.process(**{**request, "source_relative_path": "linked.png"})
    moved = evidence.with_name("old-evidence")
    evidence.rename(moved)
    evidence.mkdir(mode=0o700)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)


def test_operation_symlink_cannot_be_replayed(seam):
    processor, _, request, _, evidence, _, _ = seam
    (evidence / "outside").mkdir(mode=0o700)
    (evidence / "media_unit").symlink_to(evidence / "outside")
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert not list((evidence / "outside").iterdir())


def test_resource_and_identity_faults_retained(seam, monkeypatch):
    processor, engine, request, _, evidence, raw, _ = seam
    monkeypatch.setattr(
        media,
        "_worker_run",
        lambda *args, **kwargs: {
            "status": "resource_rejected",
            "outcome_code": "normalization_deadline",
        },
    )
    result = processor.process(**request)
    assert (result.status, result.outcome_code) == ("resource_rejected", "normalization_deadline")
    assert engine.calls == 0
    assert (evidence / "media_unit" / "original.bin").read_bytes() == raw
    assert (
        json.loads((evidence / "media_unit" / "result.json").read_bytes())["original"]["sha256"]
        == request["expected_source_sha256"]
    )


def test_request_cannot_choose_decoder_budget_or_engine(seam):
    processor, _, request, _, _, _, _ = seam
    with pytest.raises(TypeError):
        processor.process(**{**request, "interpreter": "/bin/sh"})
    with pytest.raises(media.ReceiptMediaError):
        processor.process(**{**request, "expected_source_size": media.SOURCE_BYTES + 1})
    with pytest.raises(media.ReceiptMediaError):
        processor.process(**{**request, "source_relative_path": "../capture.png"})


def test_legacy_ocr_fingerprint_contract_unchanged():
    from finance_core.intake import receipt_ocr_evidence as legacy

    attachment = legacy._AttachmentRecord(1, "/synthetic/original.png", "c" * 64, 100, "image/png")
    identity = ReceiptOcrEngineIdentity("synthetic", "1", "a" * 64, "b" * 64)
    expected = legacy._extraction_fingerprint(attachment, identity, legacy.ReceiptOcrLimits())
    assert expected == "e46235eee8d74b09558e75deb02f593cc05e16e9a40ae96bbe0dba34fd007eb5"


def test_ocr_receives_read_only_exact_descriptor(seam, monkeypatch):
    processor, engine, request, _, _, _, _ = seam
    ordinary = engine.extract

    def inspect(source, *, limits, deadline):
        with pytest.raises(OSError):
            os.write(source.file_descriptor, b"cannot mutate PNG")
        return ordinary(source, limits=limits, deadline=deadline)

    monkeypatch.setattr(engine, "extract", inspect)
    assert processor.process(**request).status == "succeeded"


def test_nested_source_directory_replacement_not_hidden_by_held_fd(seam, monkeypatch):
    processor, _, request, source, evidence, _, _ = seam
    directory = source / "nested"
    directory.mkdir(mode=0o700)
    (source / "capture.png").rename(directory / "capture.png")
    request = {**request, "source_relative_path": "nested/capture.png"}

    def replace_directory(stage):
        if stage == "after_normalization":
            directory.rename(source / "old-nested")
            directory.mkdir(mode=0o700)

    monkeypatch.setattr(media, "_failure_injection_hook", replace_directory)
    with pytest.raises((media.MediaIntegrityError, OSError)):
        processor.process(**request)
    assert not (evidence / "media_unit" / "result.json").exists()


def test_fifo_source_refused_without_waiting_for_writer(seam):
    processor, _, request, source, _, _, _ = seam
    os.mkfifo(source / "pipe.png", mode=0o400)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**{**request, "source_relative_path": "pipe.png"})


def test_decoder_identity_change_retains_original_without_ocr(seam, monkeypatch):
    processor, engine, request, _, evidence, raw, _ = seam
    ordinary = media._worker_run

    def changed(*args, **kwargs):
        result = ordinary(*args, **kwargs)
        result["decoder"] = {"unexpected": "decoder"}
        return result

    monkeypatch.setattr(media, "_worker_run", changed)
    result = processor.process(**request)
    assert (result.status, result.outcome_code) == ("engine_failed", "decoder_identity_changed")
    assert engine.calls == 0
    assert (evidence / "media_unit" / "original.bin").read_bytes() == raw


def test_ocr_identity_changes_during_operation_fail_closed(seam, monkeypatch):
    processor, engine, request, _, _, _, _ = seam
    ordinary = engine.extract

    def changed(source, *, limits, deadline):
        result = ordinary(source, limits=limits, deadline=deadline)
        engine.identity = replace(engine.identity, configuration_hash="e" * 64)
        return result

    monkeypatch.setattr(engine, "extract", changed)
    result = processor.process(**request)
    assert (result.status, result.outcome_code, result.reference) == (
        "engine_failed",
        "ocr_identity_changed",
        None,
    )


@pytest.mark.parametrize(
    "code", ["normalization_deadline", "decoder_output_limit", "decoder_exit_nonzero"]
)
def test_terminal_decoder_failures_never_retry(seam, monkeypatch, code):
    processor, engine, request, _, evidence, raw, _ = seam
    calls = []

    def fault(*args, **kwargs):
        calls.append(1)
        return {"status": "resource_rejected", "outcome_code": code}

    monkeypatch.setattr(media, "_worker_run", fault)
    first = processor.process(**request)
    second = processor.process(**request)
    assert first.outcome_code == second.outcome_code == code
    assert second.persistence_idempotent
    assert calls == [1] and engine.calls == 0
    assert (evidence / "media_unit" / "original.bin").read_bytes() == raw


def test_private_worker_geometry_and_container_bounds_are_explicit():
    from finance_core.intake import _receipt_media_worker as worker

    worker.geometry(4000, 6000, channels=4)
    with pytest.raises(worker.Refused) as too_many_pixels:
        worker.geometry(4000, 6001)
    assert too_many_pixels.value.code == "decoded_pixel_limit"
    with pytest.raises(worker.Refused) as too_many_bytes:
        worker.geometry(1000, 1000, channels=200)
    assert too_many_bytes.value.code == "decoded_byte_limit"
    with pytest.raises(worker.Refused) as malformed:
        worker.heif_brands(b"\0\0\0\x20ftypheic")
    assert malformed.value.code == "malformed_container"


def test_bounded_process_errors_map_to_retained_stage_codes(monkeypatch, tmp_path):
    from finance_core.intake import receipt_ocr_evidence as legacy

    monkeypatch.setattr(media, "_platform", lambda: None)

    def expired(*args, **kwargs):
        raise legacy.OcrDeadlineExceededError("Synthetic bounded process deadline")

    monkeypatch.setattr(legacy, "_run_bounded_process", expired)
    result = media._worker_run([], pass_fds=(), directory=str(tmp_path), deadline=1)
    assert result == {"status": "resource_rejected", "outcome_code": "normalization_deadline"}


def test_reference_inventory_rejects_uncovered_files(seam):
    processor, _, request, _, evidence, _, _ = seam
    reference = processor.process(**request).reference
    (evidence / "media_unit" / "uncovered.bin").write_bytes(b"extra source")
    with pytest.raises(media.MediaIntegrityError):
        processor.read_verified(reference)


def test_completed_ocr_fingerprint_binds_actual_result_not_only_input(seam, monkeypatch):
    processor, engine, request, _, _, _, _ = seam
    first = processor.process(**request).reference
    ordinary = engine.extract

    def different(source, *, limits, deadline):
        result = ordinary(source, limits=limits, deadline=deadline)
        return replace(result, blocks=(replace(result.blocks[0], text="TOTAL 24.00"),))

    monkeypatch.setattr(engine, "extract", different)
    second = processor.process(**{**request, "operation_id": "media_other_result"}).reference
    assert first.normalization_fingerprint == second.normalization_fingerprint
    assert first.ocr_result_sha256 != second.ocr_result_sha256
    assert first.ocr_fingerprint != second.ocr_fingerprint


def test_exact_copy_to_new_trusted_root_reopens_reference(seam):
    processor, engine, request, source, evidence, _, _ = seam
    reference = processor.process(**request).reference
    copied = evidence.with_name("copied-evidence")
    shutil.copytree(evidence, copied)
    reopened = media.ReceiptMediaProcessor(source, copied, ocr_engine=engine)
    verified = reopened.read_verified(reference)
    assert verified.reference == reference
    assert verified.blocks[0].text == "TOTAL 12.50"
    assert reopened.process(**request).persistence_idempotent
    # Projections are ordinary copies; the reference must be reopened.
    verified.manifest["normalization"]["png"]["sha256"] = "0" * 64
    assert reopened.read_verified(reference).manifest["normalization"]["png"]["sha256"] != "0" * 64


@pytest.mark.parametrize("configuration", ["engine", "limits"])
def test_changed_trusted_ocr_configuration_cannot_reuse_prior_operation(seam, configuration):
    processor, engine, request, source, evidence, _, _ = seam
    prior = processor.process(**request).reference
    options = {}
    if configuration == "engine":
        engine.identity = replace(engine.identity, configuration_hash="f" * 64)
    else:
        options["ocr_limits"] = replace(media.ReceiptOcrLimits(), max_stdout_bytes=4_000_000)
    different = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine, **options)
    with pytest.raises(media.MediaOperationConflictError):
        different.process(**request)
    new = different.process(**{**request, "operation_id": "media_new_config"}).reference
    assert prior.ocr_fingerprint != new.ocr_fingerprint


def test_processing_has_no_sqlite_side_effects(seam, tmp_path):
    processor, _, request, _, _, _, _ = seam
    database = tmp_path / "sentinel.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('unchanged')")
    before = database.read_bytes()
    processor.process(**request)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM sentinel").fetchall() == [("unchanged",)]


def test_actual_root_owner_lock_serializes_before_effects(seam, monkeypatch, tmp_path):
    processor, _, request, _, evidence, _, _ = seam
    root = media.publication.open_storage_root(evidence)
    media.publication.acquire_storage_root_lock(
        root, deadline=media.time.monotonic() + 1, clock=media.time.monotonic
    )
    report = tmp_path / "lock-observation"
    child = os.fork()
    if child == 0:
        try:
            monkeypatch.setattr(media, "NORMALIZATION_SECONDS", 0.03)
            try:
                processor.process(**request)
                report.write_text("unexpected success")
            except media.publication.DurablePublicationError:
                report.write_text("bounded lock refusal")
        finally:
            os._exit(0)
    try:
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert report.read_text() == "bounded lock refusal"
        assert not (evidence / "media_unit").exists()
    finally:
        media.publication.release_storage_root_lock(root)
        root.close()
    assert processor.process(**request).status == "succeeded"


@pytest.mark.parametrize(
    ("member", "earlier_failure"),
    [
        ("claim", None),
        ("intent.json", None),
        ("original.bin", None),
        ("original.bin", "after_original"),
        ("declaration.json", None),
        ("normalized.png", None),
        ("normalized.png", "decoder"),
        ("ocr.json", None),
        ("ocr.json", "probe-unavailable"),
        ("ocr.json", "interrupt-before-close"),
        ("ocr.json", "interrupt-close"),
        ("ocr.json", "interrupt-probe"),
        ("result.json", None),
        ("result.json", "ocr"),
        ("result.json", "masked-create"),
        ("result.json", "masked-create-ocr"),
    ],
)
def test_unknown_writer_close_retains_original_processing_custody(
    seam, monkeypatch, member, earlier_failure
):
    processor, engine, request, _, evidence, raw, workers = seam
    operation = request["operation_id"]
    if member == "claim":
        member = media._claim_name(operation)
    if earlier_failure == "decoder":

        def fail_decoder(*args, **kwargs):
            raise RuntimeError("synthetic decoder exception")

        monkeypatch.setattr(media, "_worker_run", fail_decoder)
    if earlier_failure == "after_original":

        def fail_original(stage):
            if stage == earlier_failure:
                raise RuntimeError("synthetic original interruption")

        monkeypatch.setattr(media, "_inject", fail_original)
    engine.fail = earlier_failure in {"ocr", "masked-create-ocr"}
    ordinary_open, ordinary_close, ordinary_fstat, ordinary_sync = (
        os.open,
        os.close,
        os.fstat,
        os.fsync,
    )
    acquire = media.publication.acquire_storage_root_lock
    release = media.publication.release_storage_root_lock
    writers, attempts, roots, released = [], [], [], []
    events, interruptions = [], []

    def observe_open(name, flags, *args, **kwargs):
        if (
            name == member
            and flags & os.O_EXCL
            and earlier_failure in {"masked-create", "masked-create-ocr"}
        ):
            old_umask = os.umask(0o200)
            try:
                fd = ordinary_open(name, flags, *args, **kwargs)
            finally:
                os.umask(old_umask)
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0
        else:
            fd = ordinary_open(name, flags, *args, **kwargs)
        if name == member and flags & os.O_EXCL:
            writers.append((fd, kwargs["dir_fd"], os.fstat(fd)))
        return fd

    def fault_close(fd):
        if writers and fd == writers[0][0]:
            attempts.append(fd)
            events.append("close")
            if len(attempts) == 1:
                if earlier_failure == "interrupt-close":
                    raise KeyboardInterrupt
                raise OSError(errno.EIO, "synthetic error with the writer still open")
        ordinary_close(fd)

    def fault_probe(fd):
        if writers and fd == writers[0][0]:
            if earlier_failure == "interrupt-before-close" and not interruptions:
                interruptions.append(fd)
                raise KeyboardInterrupt
            if attempts and earlier_failure == "probe-unavailable":
                raise OSError(errno.EIO, "synthetic error probing the writer")
            if attempts and earlier_failure == "interrupt-probe":
                raise KeyboardInterrupt
        return ordinary_fstat(fd)

    def observe_sync(fd):
        ordinary_sync(fd)
        if writers and fd == writers[0][1]:
            events.append("directory-sync")

    def observe_acquire(root, **kwargs):
        acquire(root, **kwargs)
        roots.append(root)

    def observe_release(root):
        released.append(root)
        release(root)

    with monkeypatch.context() as injection:
        injection.setattr(os, "open", observe_open)
        injection.setattr(os, "close", fault_close)
        injection.setattr(os, "fstat", fault_probe)
        injection.setattr(os, "fsync", observe_sync)
        injection.setattr(media.publication, "acquire_storage_root_lock", observe_acquire)
        injection.setattr(media.publication, "release_storage_root_lock", observe_release)
        with pytest.raises(media.MediaWriterCloseUnknown) as caught:
            processor.process(**request)
    held = caught.value
    writer_fd, directory, writer_identity = writers[0]
    try:
        assert held.operation_id == operation and held.member == member
        assert held.writer_fd == writer_fd
        if earlier_failure == "interrupt-before-close":
            assert interruptions == [writer_fd] and not attempts
        else:
            assert attempts == [writer_fd]
        if member.endswith(".claim.json"):
            assert events == ["directory-sync", "close"]
        assert os.path.samestat(os.fstat(writer_fd), writer_identity)
        assert len(roots) == 1 and not released
        assert os.path.samestat(os.fstat(roots[0].fd), evidence.stat())
        writable_directory = evidence if member.endswith(".claim.json") else evidence / operation
        assert os.path.samestat(os.fstat(directory), writable_directory.stat())
        terminal = evidence / operation / "result.json"
        if member == "result.json":
            assert stat.S_IMODE(terminal.stat().st_mode) == 0o600
        else:
            assert not terminal.exists()
        independent_fd = ordinary_open(evidence, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(independent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            ordinary_close(independent_fd)
    finally:
        # Test-only simulation of the OS dropping process custody on exit.
        ordinary_close(writer_fd)
        assert held._custody is not None
        held._custody.close()
    assert released == roots
    calls = (len(workers), engine.calls)
    replay = processor.process(**request)
    assert (replay.status, replay.outcome_code) == ("unknown", "incomplete_operation")
    assert replay.persistence_idempotent and replay.reference is None
    assert (len(workers), engine.calls) == calls
    if member == "result.json" and earlier_failure is None:
        bundle = evidence / operation
        intent = json.loads((bundle / "intent.json").read_bytes())
        terminal = (bundle / member).read_bytes()
        reference = processor._reference(
            intent, json.loads(terminal), hashlib.sha256(terminal).hexdigest()
        )
        with pytest.raises(media.MediaIntegrityError):
            processor.read_verified(reference)
        with pytest.raises(media.MediaIntegrityError):
            media.read_retained_media_total_fields_v1(
                evidence, reference, expected_original_size=len(raw)
            )


@pytest.mark.parametrize("reused", [False, True])
def test_known_writer_close_error_never_retries_or_publishes_terminal(seam, monkeypatch, reused):
    processor, engine, request, source, evidence, _, _ = seam
    ordinary_open, ordinary_close = os.open, os.close
    writers, replacement = [], []
    problem = OSError(errno.EIO, "synthetic error after the writer closed")

    def observe_open(name, flags, *args, **kwargs):
        fd = ordinary_open(name, flags, *args, **kwargs)
        if name == "result.json" and flags & os.O_EXCL:
            writers.append(fd)
        return fd

    def fault_close(fd):
        if writers == [fd]:
            writers.append(fd)
            ordinary_close(fd)
            if reused:
                other = ordinary_open(source / "capture.png", os.O_RDONLY)
                try:
                    if other != fd:
                        os.dup2(other, fd)
                finally:
                    if other != fd:
                        ordinary_close(other)
                replacement.append(fd)
            raise problem
        ordinary_close(fd)

    with monkeypatch.context() as injection:
        injection.setattr(os, "open", observe_open)
        injection.setattr(os, "close", fault_close)
        with pytest.raises(OSError) as caught:
            processor.process(**request)
    try:
        assert caught.value is problem
        terminal = evidence / request["operation_id"] / "result.json"
        assert stat.S_IMODE(terminal.stat().st_mode) == 0o600
        if reused:
            assert os.path.samestat(os.fstat(replacement[0]), (source / "capture.png").stat())
        replay = processor.process(**request)
        assert replay.status == "unknown" and engine.calls == 1
    finally:
        for fd in replacement:
            ordinary_close(fd)


def test_terminal_publication_never_replaces_an_existing_name(seam, monkeypatch):
    processor, engine, request, _, evidence, _, _ = seam
    bundle = evidence / request["operation_id"]
    occupied = b"controlled existing terminal"

    def occupy_terminal(stage):
        if stage == "before_terminal":
            (bundle / "result.json").write_bytes(occupied)
            (bundle / "result.json").chmod(0o400)

    monkeypatch.setattr(media, "_inject", occupy_terminal)
    with pytest.raises(FileExistsError):
        processor.process(**request)
    assert (bundle / "result.json").read_bytes() == occupied
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert engine.calls == 1


@pytest.mark.parametrize("checkpoint", ["before-seal", "after-seal-sync", "none"])
@pytest.mark.parametrize("ocr_fails", [False, True])
def test_terminal_seals_the_exact_read_only_descriptor_after_close(
    seam, monkeypatch, checkpoint, ocr_fails
):
    processor, engine, request, _, evidence, _, _ = seam
    terminal = evidence / request["operation_id"] / "result.json"
    engine.fail = ocr_fails
    ordinary_open, ordinary_close = os.open, os.close
    ordinary_chmod, ordinary_sync = os.fchmod, os.fsync
    writers, closed, sealed = [], [], []

    def observe_open(name, flags, *args, **kwargs):
        fd = ordinary_open(name, flags, *args, **kwargs)
        if name == terminal.name and flags & os.O_EXCL:
            writers.append((fd, os.fstat(fd)))
        return fd

    def observe_close(fd):
        is_writer = (
            writers and fd == writers[0][0] and os.path.samestat(os.fstat(fd), writers[0][1])
        )
        ordinary_close(fd)
        if is_writer:
            closed.append(fd)

    def verify_seal(fd, mode):
        if mode == 0o400 and writers and os.path.samestat(os.fstat(fd), writers[0][1]):
            assert closed == [writers[0][0]]
            assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            assert os.path.samestat(os.fstat(fd), terminal.stat())
            if checkpoint == "before-seal":
                raise OSError(errno.EIO, "synthetic interruption before seal")
            sealed.append(fd)
        ordinary_chmod(fd, mode)

    def sync(fd):
        if checkpoint == "after-seal-sync" and sealed == [fd]:
            raise OSError(errno.EIO, "synthetic failure synchronizing the sealed terminal")
        ordinary_sync(fd)

    expected = "engine_failed" if ocr_fails else "succeeded"
    with monkeypatch.context() as injection:
        injection.setattr(os, "open", observe_open)
        injection.setattr(os, "close", observe_close)
        injection.setattr(os, "fchmod", verify_seal)
        injection.setattr(os, "fsync", sync)
        if checkpoint == "none":
            assert processor.process(**request).status == expected
        else:
            with pytest.raises(OSError, match="synthetic"):
                processor.process(**request)

    unsealed = checkpoint == "before-seal"
    assert stat.S_IMODE(terminal.stat().st_mode) == (0o600 if unsealed else 0o400)
    replay = processor.process(**request)
    assert replay.status == ("unknown" if unsealed else expected)
    assert replay.persistence_idempotent and engine.calls == 1


def test_worker_wrapper_preserves_exact_absolute_deadline_and_fixed_limits(monkeypatch, tmp_path):
    from finance_core.intake import receipt_ocr_evidence as legacy

    monkeypatch.setattr(media, "_platform", lambda: None)
    captured = {}

    def process(arguments, **kwargs):
        captured.update(kwargs)
        return legacy._ProcessOutput(0, b'{"outcome_code":"test","status":"engine_failed"}')

    monkeypatch.setattr(legacy, "_run_bounded_process", process)
    media._worker_run(["fixed-helper"], pass_fds=(8, 9), directory=str(tmp_path), deadline=123.5)
    assert captured["deadline"] == 123.5
    assert captured["pass_fds"] == (8, 9)
    assert captured["working_directory"] == str(tmp_path)
    limits = captured["limits"]
    assert (limits.cpu_time_seconds, limits.address_space_bytes, limits.output_file_bytes) == (
        30,
        536_870_912,
        20_000_000,
    )
    assert (limits.process_count, limits.open_file_count) == (16, 64)
    assert (limits.max_stdout_bytes, limits.max_stderr_bytes) == (262_144, 65_536)


def test_local_decoder_helper_alpha_and_metadata_policy_only(tmp_path):
    """Actual small codec helper proof, not Linux process/installation acceptance."""
    from finance_core.intake import _receipt_media_worker as worker

    image = Image.new("RGBA", (2, 1))
    image.putdata([(5, 10, 15, 0), (10, 20, 30, 128)])
    exif = Image.Exif()
    exif[274] = 1
    original = tmp_path / "input.png"
    image.save(original, exif=exif, icc_profile=b"synthetic retained profile")
    output = tmp_path / "output.png"
    source_fd = os.open(original, os.O_RDONLY)
    output_fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        metadata = worker.normalize(
            source_fd,
            output_fd,
            {"declared_mime_type": "image/png", "original_filename": "receipt.camera.2026.png"},
        )
    finally:
        os.close(source_fd)
        os.close(output_fd)
    with Image.open(output) as decoded:
        assert [decoded.getpixel((0, 0)), decoded.getpixel((1, 0))] == [
            (255, 255, 255),
            (132, 137, 142),
        ]
        assert not decoded.info and not decoded.getexif()
    assert metadata["source_has_alpha"] and metadata["alpha_policy"] == "white_matte"
    assert (
        metadata["color_profile"]["icc_profile_sha256"]
        == hashlib.sha256(b"synthetic retained profile").hexdigest()
    )


def test_invocation_preserves_actual_venv_import_environment(seam):
    """Real selected interpreter import proof, without Linux admission claims."""
    processor, _, _, _, _, _, _ = seam
    assert str(processor._python) == os.path.abspath(sys.executable)
    output = subprocess.run(
        [
            str(processor._python),
            "-I",
            "-B",
            "-c",
            "import json, sys, PIL, pillow_heif; "
            "print(json.dumps([sys.prefix, sys.base_prefix, PIL.__version__, "
            "pillow_heif.__version__]))",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    assert json.loads(output.stdout) == [sys.prefix, sys.base_prefix, "13.0.0", "1.8.0"]


def test_local_xmp_only_is_not_synthesized_exif(tmp_path):
    from finance_core.intake import _receipt_media_worker as worker

    original = tmp_path / "xmp.jpg"
    Image.new("RGB", (4, 3)).save(original, xmp=b'<rdf tiff:Orientation="6"/>')
    with Image.open(original) as image:
        assert image.info.get("exif") is None
        assert image.getexif()[274] == 6  # Actual Pillow synthesis being excluded.
        assert worker.orientation(image.info) == (None, 6)
    source_fd = os.open(original, os.O_RDONLY)
    output_fd = os.open(tmp_path / "output.png", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with pytest.raises(worker.Refused) as rejected:
            worker.normalize(source_fd, output_fd, {})
        assert (rejected.value.status, rejected.value.code) == (
            "unsupported_input",
            "ambiguous_orientation",
        )
    finally:
        os.close(source_fd)
        os.close(output_fd)


@pytest.mark.parametrize("height", [5000, 10000], ids=["pixel_warning", "pixel_error"])
def test_local_over_pixel_png_has_resource_refusal_without_decode(tmp_path, height):
    """Use a valid pixel stream without allocating an oversized raster; not Linux proof."""
    from finance_core.intake import _receipt_media_worker as worker

    def png_chunk(tag, payload):
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    width = 6000
    compressor = zlib.compressobj()
    row = b"\0" + bytes(width * 3)
    compressed = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", compressed)
        + png_chunk(b"IEND", b"")
    )
    original = tmp_path / "oversized.png"
    output = tmp_path / "output.png"
    original.write_bytes(raw)
    source_fd = os.open(original, os.O_RDONLY)
    output_fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    old_pixel_limit = Image.MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings(), pytest.raises(worker.Refused) as rejected:
            worker.normalize(source_fd, output_fd, {})
        assert (rejected.value.status, rejected.value.code) == (
            "resource_rejected",
            "decoded_pixel_limit",
        )
        assert original.read_bytes() == raw
        assert output.stat().st_size == 0
    finally:
        Image.MAX_IMAGE_PIXELS = old_pixel_limit
        os.close(source_fd)
        os.close(output_fd)


def test_local_post_idat_exif_is_observed_before_transform(tmp_path):
    from finance_core.intake import _receipt_media_worker as worker

    original = tmp_path / "late.png"
    image = Image.new("RGB", (4, 3))
    image.putpixel((0, 0), (255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", exif=exif)
    raw = buffer.getvalue()
    chunks, offset = [], 8
    while offset < len(raw):
        length = struct.unpack_from(">I", raw, offset)[0] + 12
        chunks.append(raw[offset : offset + length])
        offset += length
    late_exif = next(chunk for chunk in chunks if chunk[4:8] == b"eXIf")
    original.write_bytes(
        raw[:8]
        + b"".join(chunk for chunk in chunks if chunk[4:8] not in {b"eXIf", b"IEND"})
        + late_exif
        + chunks[-1]
    )
    with Image.open(original) as loaded:
        assert "exif" not in loaded.info
        loaded.load()
        assert worker.orientation(loaded.info) == (6, None)
    source_fd = os.open(original, os.O_RDONLY)
    output = tmp_path / "output.png"
    output_fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        metadata = worker.normalize(source_fd, output_fd, {})
    finally:
        os.close(source_fd)
        os.close(output_fd)
    assert metadata["dimensions"] == [3, 4] and metadata["exif_orientation"] == 6
    with Image.open(output) as normalized:
        assert normalized.getpixel((2, 0)) == (255, 0, 0)
        assert not normalized.info


@pytest.mark.parametrize("uid,tasks,capabilities", [(0, 1, 0), (501, 2, 0), (501, 1, 1)])
def test_linux_caller_admission_refuses_root_native_threads_and_capabilities(
    monkeypatch, uid, tasks, capabilities
):
    from pathlib import Path

    monkeypatch.setattr(media.sys, "platform", "linux")
    monkeypatch.setattr(media.os, "getuid", lambda: uid)
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([None] * tasks))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self: f"CapEff:\t{capabilities:016x}\nCapPrm:\t{capabilities:016x}\n",
    )
    with pytest.raises(media.MediaUnsupportedPlatformError):
        media._platform()


def test_linux_caller_admission_requires_observable_os_state(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(media.sys, "platform", "linux")

    def unavailable(self):
        raise OSError("Synthetic unavailable /proc")

    monkeypatch.setattr(Path, "iterdir", unavailable)
    with pytest.raises(media.MediaUnsupportedPlatformError, match="evidence is unavailable"):
        media._platform()


def test_second_root_admission_failure_closes_first_root(seam, monkeypatch):
    processor, _, request, _, _, _, _ = seam
    ordinary = processor._open_root
    opened = []

    def root(index):
        if index == 1:
            raise media.MediaIntegrityError("Synthetic evidence-root refusal")
        handle = ordinary(index)
        opened.append(handle.fd)
        return handle

    monkeypatch.setattr(processor, "_open_root", root)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def _replace_operation(evidence, replacement):
    operation = evidence / "media_unit"
    moved = evidence / "moved-operation"
    operation.rename(moved)
    if replacement == "empty":
        operation.mkdir(mode=0o700)
    elif replacement == "symlink":
        operation.symlink_to(moved, target_is_directory=True)
    return moved


@pytest.mark.parametrize("replacement", ["missing", "empty", "symlink"])
@pytest.mark.parametrize(
    "stage",
    ["after_original", "after_normalization", "after_ocr", "before_terminal", "after_terminal"],
)
def test_operation_custody_loss_never_returns_consumable_success(
    seam, monkeypatch, stage, replacement
):
    processor, engine, request, _, evidence, raw, worker_calls = seam

    def replace_operation(observed):
        if observed == stage:
            _replace_operation(evidence, replacement)

    monkeypatch.setattr(media, "_failure_injection_hook", replace_operation)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert (evidence / "moved-operation" / "original.bin").read_bytes() == raw
    if stage in {"after_original", "after_normalization"}:
        assert engine.calls == 0
    assert len(worker_calls) == (0 if stage == "after_original" else 1)
    assert not (evidence / "media_unit" / "result.json").exists() or replacement == "symlink"


@pytest.mark.parametrize("replacement", ["missing", "empty", "symlink"])
def test_reader_rechecks_operation_custody_before_return(seam, monkeypatch, replacement):
    processor, _, request, _, evidence, _, _ = seam
    reference = processor.process(**request).reference
    ordinary = processor._read_verified

    def moved(directory, requested):
        projection = ordinary(directory, requested)
        _replace_operation(evidence, replacement)
        return projection

    monkeypatch.setattr(processor, "_read_verified", moved)
    with pytest.raises(media.MediaIntegrityError):
        processor.read_verified(reference)


@pytest.mark.parametrize("missing", ["declaration.json", "normalized.png", "ocr.json"])
def test_failed_terminal_replay_requires_recorded_completed_stages(seam, monkeypatch, missing):
    processor, engine, request, _, evidence, _, worker_calls = seam

    def failed(source, *, limits, deadline):
        engine.calls += 1
        return ReceiptOcrEngineResult(
            ReceiptOcrExtractionStatus.ENGINE_FAILED, (), "actual_failure"
        )

    monkeypatch.setattr(engine, "extract", failed)
    terminal = processor.process(**request)
    assert terminal.status == "engine_failed"
    assert processor.process(**request).status == "engine_failed"
    assert engine.calls == len(worker_calls) == 1
    (evidence / "media_unit" / missing).unlink()
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert engine.calls == len(worker_calls) == 1


@pytest.mark.parametrize("replacement", ["missing", "empty", "symlink"])
@pytest.mark.parametrize("state", ["unknown", "succeeded", "engine_failed"])
def test_every_replay_return_rechecks_operation_custody(seam, monkeypatch, state, replacement):
    processor, engine, request, _, evidence, _, worker_calls = seam
    if state == "unknown":

        def interrupt(stage):
            if stage == "after_intent":
                raise KeyboardInterrupt("Synthetic interrupted intent")

        monkeypatch.setattr(media, "_failure_injection_hook", interrupt)
        with pytest.raises(KeyboardInterrupt):
            processor.process(**request)
        monkeypatch.setattr(media, "_failure_injection_hook", None)
        ordinary = media._read_member

        def read(directory, name, maximum):
            if name == "result.json":
                _replace_operation(evidence, replacement)
            return ordinary(directory, name, maximum)

        monkeypatch.setattr(media, "_read_member", read)
    else:
        engine.fail = state == "engine_failed"
        processor.process(**request)
        name = "_read_verified" if state == "succeeded" else "_verify_terminal_failure"
        ordinary = getattr(processor, name)

        def verify(*args):
            value = ordinary(*args)
            _replace_operation(evidence, replacement)
            return value

        monkeypatch.setattr(processor, name, verify)
    prior = (engine.calls, len(worker_calls))
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert prior == (engine.calls, len(worker_calls))


@pytest.mark.parametrize("replacement", ["missing", "empty", "symlink"])
def test_operation_moved_during_engine_call_refuses_publication(seam, monkeypatch, replacement):
    processor, engine, request, _, evidence, _, _ = seam
    ordinary = engine.extract

    def moved(source, *, limits, deadline):
        result = ordinary(source, limits=limits, deadline=deadline)
        _replace_operation(evidence, replacement)
        return result

    monkeypatch.setattr(engine, "extract", moved)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert not (evidence / "moved-operation" / "ocr.json").exists()
    assert not (evidence / "moved-operation" / "result.json").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "png_bytes",
        "ocr_bytes",
        "normalization_fingerprint",
        "parameters",
        "ocr_fingerprint",
        "ocr_status",
    ],
)
def test_failed_replay_checks_recorded_stage_integrity_and_links(seam, monkeypatch, damage):
    processor, engine, request, _, evidence, _, worker_calls = seam

    def failed(source, *, limits, deadline):
        engine.calls += 1
        return ReceiptOcrEngineResult(
            ReceiptOcrExtractionStatus.ENGINE_FAILED, (), "actual_failure"
        )

    monkeypatch.setattr(engine, "extract", failed)
    processor.process(**request)
    directory = evidence / "media_unit"
    terminal = directory / "result.json"
    record = json.loads(terminal.read_bytes())
    if damage in {"png_bytes", "ocr_bytes"}:
        path = directory / ("normalized.png" if damage == "png_bytes" else "ocr.json")
        path.chmod(0o600)
        path.write_bytes(b"corrupt recorded bytes")
        path.chmod(0o400)
    else:
        if damage == "normalization_fingerprint":
            record[damage] = "d" * 64
        elif damage == "parameters":
            record["normalization"]["parameters"]["alpha_policy"] = "changed"
            record["normalization_fingerprint"] = media._digest(record["normalization"])
        elif damage == "ocr_fingerprint":
            record["ocr"]["fingerprint"] = "d" * 64
        else:
            path = directory / "ocr.json"
            payload = json.loads(path.read_bytes())
            payload["status"] = "succeeded"
            path.chmod(0o600)
            path.write_bytes(media._canonical(payload))
            path.chmod(0o400)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                _, member = media._read_member(directory_fd, "ocr.json", media._MAX_JSON)
            finally:
                os.close(directory_fd)
            intent = json.loads((directory / "intent.json").read_bytes())
            record["ocr"]["member"] = member
            record["ocr"]["result_sha256"] = media._digest(payload)
            record["ocr"]["fingerprint"] = media._digest(
                {
                    "contract": media.CONTRACT,
                    "normalization_fingerprint": record["normalization_fingerprint"],
                    "original": record["original"],
                    "png": record["normalization"]["png"],
                    "engine": intent["ocr_identity"],
                    "limits": intent["ocr_limits"],
                    "result_sha256": media._digest(payload),
                    "status": payload["status"],
                }
            )
        terminal.chmod(0o600)
        terminal.write_bytes(media._canonical(record))
        terminal.chmod(0o400)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    assert engine.calls == len(worker_calls) == 1


def test_unrecorded_partial_decoder_residue_has_no_completeness_claim_or_retry(seam, monkeypatch):
    processor, engine, request, _, evidence, _, worker_calls = seam
    ordinary = media._worker_run

    def partial(*args, **kwargs):
        ordinary(*args, **kwargs)  # Unit decoder writes a partial work member.
        return {"status": "engine_failed", "outcome_code": "decoder_exit_nonzero"}

    monkeypatch.setattr(media, "_worker_run", partial)
    first = processor.process(**request)
    assert first.status == "engine_failed"
    manifest = json.loads((evidence / "media_unit" / "result.json").read_bytes())
    assert "normalization" not in manifest and "ocr" not in manifest
    assert processor.process(**request).persistence_idempotent
    (evidence / "media_unit" / "normalized.png").unlink()
    assert processor.process(**request).persistence_idempotent
    assert engine.calls == 0 and len(worker_calls) == 1


@pytest.mark.parametrize("stage", ["after_claim_create", "after_claim_write", "after_claim_seal"])
def test_interrupted_claim_never_creates_directory_or_reruns(seam, monkeypatch, stage):
    processor, engine, request, source, evidence, _, worker_calls = seam

    def interrupt(observed):
        if observed == stage:
            raise KeyboardInterrupt("Synthetic claim publication crash")

    monkeypatch.setattr(media, "_failure_injection_hook", interrupt)
    with pytest.raises(KeyboardInterrupt):
        processor.process(**request)
    claim = evidence / "media_unit.claim.json"
    assert claim.exists() and not (evidence / "media_unit").exists()
    retained = claim.read_bytes()
    monkeypatch.setattr(media, "_failure_injection_hook", None)
    fresh = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    if stage == "after_claim_seal":
        assert fresh.process(**request).status == "unknown"
    else:
        with pytest.raises(media.MediaIntegrityError):
            fresh.process(**request)
    assert claim.read_bytes() == retained
    assert not (evidence / "media_unit").exists()
    assert engine.calls == len(worker_calls) == 0


def test_fresh_instance_cannot_recreate_moved_claimed_operation(seam):
    processor, engine, request, source, evidence, _, worker_calls = seam
    reference = processor.process(**request).reference
    _replace_operation(evidence, "missing")
    fresh = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    result = fresh.process(**request)
    assert result.status == "unknown" and result.reference is None
    assert not (evidence / "media_unit").exists()
    assert engine.calls == len(worker_calls) == 1
    with pytest.raises(media.MediaIntegrityError):
        fresh.read_verified(reference)


@pytest.mark.parametrize("damage", ["missing", "symlink", "tampered", "unsafe_mode"])
def test_processing_claim_missing_or_changed_refuses_without_retry(seam, damage):
    processor, engine, request, source, evidence, _, worker_calls = seam
    processor.process(**request)
    claim = evidence / "media_unit.claim.json"
    if damage in {"missing", "symlink"}:
        claim.unlink()
        if damage == "symlink":
            claim.symlink_to(evidence / "media_unit" / "intent.json")
    elif damage == "tampered":
        claim.chmod(0o600)
        claim.write_bytes(b"tampered claim")
        claim.chmod(0o400)
    else:
        claim.chmod(0o600)
    fresh = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    with pytest.raises(media.MediaIntegrityError):
        fresh.process(**request)
    assert engine.calls == len(worker_calls) == 1


def test_bundle_copy_without_claim_is_reader_only(seam):
    processor, engine, request, source, evidence, _, worker_calls = seam
    reference = processor.process(**request).reference
    copied = evidence.with_name("six-member-copy")
    copied.mkdir(mode=0o700)
    shutil.copytree(evidence / "media_unit", copied / "media_unit")
    fresh = media.ReceiptMediaProcessor(source, copied, ocr_engine=engine)
    assert fresh.read_verified(reference).reference == reference
    with pytest.raises(media.MediaIntegrityError):
        fresh.process(**request)
    assert not (copied / "media_unit.claim.json").exists()
    assert engine.calls == len(worker_calls) == 1


@pytest.mark.parametrize(
    "stage",
    ["after_intent", "after_normalization", "after_ocr", "before_terminal", "after_terminal"],
)
@pytest.mark.parametrize("damage", ["missing", "symlink", "bytes", "same_bytes_new_inode"])
def test_held_processing_claim_is_rechecked_at_boundaries(seam, monkeypatch, stage, damage):
    processor, engine, request, _, evidence, _, _ = seam

    def alter(observed):
        if observed != stage:
            return
        claim = evidence / "media_unit.claim.json"
        if damage == "same_bytes_new_inode":
            retained = claim.read_bytes()
            claim.rename(evidence / "old-claim.json")
            claim.write_bytes(retained)
            claim.chmod(0o400)
        elif damage in {"missing", "symlink"}:
            claim.unlink()
            if damage == "symlink":
                claim.symlink_to(evidence / "media_unit" / "intent.json")
        else:
            claim.chmod(0o600)
            claim.write_bytes(b"changed during operation")
            claim.chmod(0o400)

    monkeypatch.setattr(media, "_failure_injection_hook", alter)
    with pytest.raises(media.MediaIntegrityError):
        processor.process(**request)
    if stage in {"after_intent", "after_normalization"}:
        assert engine.calls == 0


def test_claim_only_unknown_binds_same_intent_and_refuses_changed_request(seam, monkeypatch):
    processor, engine, request, source, evidence, _, worker_calls = seam

    def interrupted(stage):
        if stage == "after_claim_seal":
            raise KeyboardInterrupt("Synthetic durable reservation only")

    monkeypatch.setattr(media, "_failure_injection_hook", interrupted)
    with pytest.raises(KeyboardInterrupt):
        processor.process(**request)
    claim = evidence / "media_unit.claim.json"
    assert json.loads(claim.read_bytes()) == {
        "contract": "receipt-media-operation-claim-v1",
        "operation_id": "media_unit",
        "intent_sha256": media._digest(
            processor._intent(
                request["operation_id"],
                request["source_relative_path"],
                request["expected_source_size"],
                request["expected_source_sha256"],
                request["declared_mime_type"],
                request["original_filename"],
                request["received_via"],
            )
        ),
    }
    retained = claim.read_bytes()
    monkeypatch.setattr(media, "_failure_injection_hook", None)
    fresh = media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    assert fresh.process(**request).status == "unknown"
    with pytest.raises(media.MediaOperationConflictError):
        fresh.process(**{**request, "original_filename": "changed.png"})
    assert claim.read_bytes() == retained
    assert engine.calls == len(worker_calls) == 0


def _synthetic_historical_bundle(seam, monkeypatch):
    """Synthetic identity fixture, not actual old decoder/OS qualification."""
    processor, _, request, _, _, _, _ = seam
    original_parameters = media._DECODER_PARAMETERS
    original_packages = processor._decoder["packages"]
    monkeypatch.setattr(media, "_DECODER_PARAMETERS", media._HISTORICAL_DECODER_PARAMETERS)
    processor._decoder["packages"] = {"Pillow": "12.3.0", "pillow-heif": "1.8.0"}
    try:
        return processor.process(**request).reference
    finally:
        monkeypatch.setattr(media, "_DECODER_PARAMETERS", original_parameters)
        processor._decoder["packages"] = original_packages


def test_historical_decoder_profile_is_read_only_without_worker_or_ocr(seam, monkeypatch):
    processor, engine, request, _, evidence, _, worker_calls = seam
    reference = _synthetic_historical_bundle(seam, monkeypatch)
    retained = {p.name: p.read_bytes() for p in (evidence / "media_unit").iterdir()}
    verified = processor.read_verified(reference)
    assert verified.blocks[0].text == "TOTAL 12.50"
    assert verified.manifest["normalization"]["parameters"]["Pillow"] == "12.3.0"
    with pytest.raises(media.MediaOperationConflictError):
        processor.process(**request)
    assert engine.calls == len(worker_calls) == 1
    assert retained == {p.name: p.read_bytes() for p in (evidence / "media_unit").iterdir()}
    current = processor.process(**{**request, "operation_id": "media_current"})
    assert current.reference.normalization_fingerprint != reference.normalization_fingerprint
    assert (
        processor.read_verified(current.reference).manifest["normalization"]["parameters"]["Pillow"]
        == "13.0.0"
    )


@pytest.mark.parametrize(
    "member",
    [
        "original.bin",
        "normalized.png",
        "ocr.json",
        "intent.json",
        "result.json",
        "declaration.json",
    ],
)
def test_historical_decoder_profile_keeps_all_member_integrity_checks(seam, monkeypatch, member):
    processor, engine, _, _, evidence, _, worker_calls = seam
    reference = _synthetic_historical_bundle(seam, monkeypatch)
    path = evidence / "media_unit" / member
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"corrupt")
    path.chmod(0o400)
    with pytest.raises(media.MediaIntegrityError):
        processor.read_verified(reference)
    assert engine.calls == len(worker_calls) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("Pillow", "12.2.0"),
        ("decode_threads", True),
        ("decode_threads", 1.0),
        ("security_limits_disabled", 0),
        ("allowed_heif_bit_depths", [8.0, 10]),
        ("output_mode", "RGBA"),
        ("extra", 1),
    ],
)
def test_historical_profile_requires_exact_full_typed_policy(seam, monkeypatch, key, value):
    processor, _, _, _, evidence, _, _ = seam
    _synthetic_historical_bundle(seam, monkeypatch)
    directory = evidence / "media_unit"
    intent = json.loads((directory / "intent.json").read_bytes())
    result = json.loads((directory / "result.json").read_bytes())
    intent["decoder_parameters"][key] = value
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(media.MediaIntegrityError, match="unknown normalization policy"):
            processor._verify_completed_normalization(fd, intent, result, result["original"])
    finally:
        os.close(fd)


def test_old_decoder_installation_cannot_be_selected_for_new_execution(seam, monkeypatch):
    _, engine, _, source, evidence, _, worker_calls = seam
    original = media._worker_run

    def old_identity(*args, **kwargs):
        response = original(*args, **kwargs)
        response["decoder"]["packages"] = {"Pillow": "12.3.0", "pillow-heif": "1.8.0"}
        return response

    monkeypatch.setattr(media, "_worker_run", old_identity)
    with pytest.raises(media.ReceiptMediaError):
        media.ReceiptMediaProcessor(source, evidence, ocr_engine=engine)
    assert engine.calls == len(worker_calls) == 0


@pytest.mark.parametrize("state", ["failure", "unknown"])
def test_old_incomplete_or_failed_operations_conflict_without_retry(seam, monkeypatch, state):
    processor, engine, request, _, evidence, _, worker_calls = seam
    original_parameters = media._DECODER_PARAMETERS
    packages = processor._decoder["packages"]
    monkeypatch.setattr(media, "_DECODER_PARAMETERS", media._HISTORICAL_DECODER_PARAMETERS)
    processor._decoder["packages"] = {"Pillow": "12.3.0", "pillow-heif": "1.8.0"}
    if state == "failure":
        engine.fail = True
        assert processor.process(**request).status == "engine_failed"
    else:

        def interrupt(stage):
            if stage == "after_normalization":
                raise KeyboardInterrupt("synthetic old interruption")

        monkeypatch.setattr(media, "_failure_injection_hook", interrupt)
        with pytest.raises(KeyboardInterrupt):
            processor.process(**request)
        monkeypatch.setattr(media, "_failure_injection_hook", None)
    monkeypatch.setattr(media, "_DECODER_PARAMETERS", original_parameters)
    processor._decoder["packages"] = packages
    retained = {p.name: p.read_bytes() for p in (evidence / "media_unit").iterdir()}
    counts = (engine.calls, len(worker_calls))
    with pytest.raises(media.MediaOperationConflictError):
        processor.process(**request)
    assert (engine.calls, len(worker_calls)) == counts
    assert retained == {p.name: p.read_bytes() for p in (evidence / "media_unit").iterdir()}


def test_apng_background_disposal_is_checked_before_canvas_allocation(tmp_path):
    """Bounded child + fill tripwire; never allocate the declared large canvas."""
    script = r"""
import io, json, os, resource, struct, tempfile, zlib
from PIL import Image
from finance_core.intake import _receipt_media_worker as worker
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
def chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag+payload) & 0xffffffff))
def apng(width, height):
    compressor = zlib.compressobj()
    row = b"\0" + bytes(width * 4)
    data = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", width,height,8,6,0,0,0))
      +chunk(b"acTL", struct.pack(">II",2,0))
      +chunk(b"fcTL", struct.pack(">IIIIIHHBB",0,width,height,0,0,1,10,1,0))
      +chunk(b"IDAT", data)
      +chunk(b"IEND",b""))
original_fill = Image.core.fill
fills=[]
def refuse_fill(*args, **kwargs):
    fills.append(repr(args[:2]))
    raise AssertionError("canvas allocation attempted before resource check")
Image.core.fill=refuse_fill
Image.MAX_IMAGE_PIXELS=worker.MAX_PIXELS
try:
    Image.open(io.BytesIO(apng(6000,10000)), formats=["PNG"])
except Image.DecompressionBombError:
    assert not fills
else:
    raise AssertionError("oversized APNG was not rejected")
Image.core.fill = original_fill
with tempfile.TemporaryDirectory() as directory:
    source = os.path.join(directory, "small.png")
    output = os.path.join(directory, "output.png")
    Image.new("RGBA", (2, 1), (1, 2, 3, 255)).save(
        source, save_all=True,
        append_images=[Image.new("RGBA", (2, 1), (3, 2, 1, 255))],
        disposal=1, duration=100)
    source_fd = os.open(source, os.O_RDONLY)
    output_fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        worker.normalize(source_fd, output_fd, {})
    except worker.Refused as exc:
        assert (exc.status, exc.code) == ("unsupported_input", "multiple_frames")
        assert os.fstat(output_fd).st_size == 0
    else:
        raise AssertionError("small APNG was admitted")
    finally:
        os.close(source_fd)
        os.close(output_fd)
print(json.dumps({"oversized_apng":"bomb_error_before_fill", "fill_calls":len(fills),
                  "small_apng":"multiple_frames"}))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "oversized_apng": "bomb_error_before_fill",
        "fill_calls": 0,
        "small_apng": "multiple_frames",
    }


@pytest.mark.parametrize(
    "key,value",
    [("decode_threads", True), ("decode_threads", 1.0), ("security_limits_disabled", 0)],
)
def test_normalization_copy_cannot_alias_typed_historical_parameters(seam, monkeypatch, key, value):
    processor, _, _, _, evidence, _, _ = seam
    _synthetic_historical_bundle(seam, monkeypatch)
    directory = evidence / "media_unit"
    intent = json.loads((directory / "intent.json").read_bytes())
    result = json.loads((directory / "result.json").read_bytes())
    result["normalization"]["parameters"][key] = value
    result["normalization_fingerprint"] = media._digest(result["normalization"])
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(media.MediaIntegrityError, match="Normalization identity differs"):
            processor._verify_completed_normalization(fd, intent, result, result["original"])
    finally:
        os.close(fd)
