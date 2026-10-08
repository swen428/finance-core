"""Synthetic seam unit proofs; fake decoder/engine never certify Linux support."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
from dataclasses import replace

import pytest
from PIL import Image

from finance_core.intake import receipt_media as media
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
)


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
        "packages": {"Pillow": "12.3.0", "pillow-heif": "1.8.0"},
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
    with pytest.raises(OSError):
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
    assert json.loads(output.stdout) == [sys.prefix, sys.base_prefix, "12.3.0", "1.8.0"]


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
