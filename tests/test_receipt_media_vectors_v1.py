"""Actual receipt-media vector matrix; only its designated Linux lane executes it.

The archive test is platform-independent. The media cases require the bounded
single-thread Linux worker and the existing pinned Tesseract configuration;
this module never treats a mocked decoder or OCR engine as Linux evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

ARCHIVE = Path(__file__).parent / "fixtures" / "receipt_media_v1" / "vectors.zip"
MAX_ARCHIVE_BYTES = 1_000_000
MAX_ARCHIVE_MEMBERS = 64
MAX_MEMBER_BYTES = 20_000_000
MAX_UNCOMPRESSED_BYTES = 5_000_000
MAX_MANIFEST_BYTES = 512_000

CASE_IDS = (
    "apng_two_frames",
    "heif_10bit_receipt",
    "heif_12mp",
    "heif_24mp",
    "heif_48mp",
    "heif_8bit_receipt",
    "heif_conflict_exif_xmp",
    "heif_corrupt",
    "heif_declared_type_mismatch",
    "heif_duplicate_event_first",
    "heif_duplicate_event_second",
    "heif_exact_replay",
    "heif_filename_with_periods",
    "heif_imir_0",
    "heif_imir_1",
    "heif_invalid_orientation_9",
    "heif_irot_1",
    "heif_irot_3",
    "heif_metadata_orientation_6",
    "heif_multi",
    "heif_pattern",
    "heif_pattern_generic",
    "heif_sequence_brand",
    "heif_truncated",
    "heif_xmp_metadata_orientation_6",
    "jpeg_conflict_exif_xmp",
    "jpeg_exif_1",
    "jpeg_exif_2",
    "jpeg_exif_3",
    "jpeg_exif_4",
    "jpeg_exif_5",
    "jpeg_exif_6",
    "jpeg_exif_7",
    "jpeg_exif_8",
    "jpeg_invalid_orientation",
    "jpeg_xmp_only",
    "png_alpha_white_matte",
    "png_disguised_as_heic",
    "png_exif_1",
    "png_exif_2",
    "png_exif_3",
    "png_exif_4",
    "png_exif_5",
    "png_exif_6",
    "png_exif_7",
    "png_exif_8",
    "png_exif_post_idat_orientation_6",
    "png_filename_type_mismatch",
    "unsupported_gif",
)

PAYLOAD_MEMBERS = frozenset(
    {
        "apng_two_frames.png",
        "heif_10bit_receipt.heic",
        "heif_12mp.heic",
        "heif_24mp.heic",
        "heif_48mp.heic",
        "heif_8bit_receipt.heic",
        "heif_conflict_exif_xmp.heic",
        "heif_corrupt.heic",
        "heif_invalid_orientation_9.heic",
        "heif_metadata_orientation_6.heic",
        "heif_multi.heic",
        "heif_pattern.heic",
        "heif_pattern_generic.heif",
        "heif_pattern_imir_0.heic",
        "heif_pattern_imir_1.heic",
        "heif_pattern_irot_1.heic",
        "heif_pattern_irot_3.heic",
        "heif_sequence_brand.heic",
        "heif_truncated.heic",
        "heif_xmp_metadata_orientation_6.heic",
        "jpeg_conflict_exif_xmp.jpg",
        "jpeg_exif_1.jpg",
        "jpeg_exif_2.jpg",
        "jpeg_exif_3.jpg",
        "jpeg_exif_4.jpg",
        "jpeg_exif_5.jpg",
        "jpeg_exif_6.jpg",
        "jpeg_exif_7.jpg",
        "jpeg_exif_8.jpg",
        "jpeg_invalid_orientation.jpg",
        "jpeg_xmp_only.jpg",
        "orientation_base.png",
        "png_alpha_white_matte.png",
        "png_exif_1.png",
        "png_exif_2.png",
        "png_exif_3.png",
        "png_exif_4.png",
        "png_exif_5.png",
        "png_exif_6.png",
        "png_exif_7.png",
        "png_exif_8.png",
        "png_exif_post_idat_orientation_6.png",
        "synthetic_receipt_10bit_source.png",
        "unsupported.gif",
    }
)


@dataclass(frozen=True)
class VectorArchive:
    manifest: dict[str, Any]
    payloads: dict[str, bytes]
    archive_sha256: str


@dataclass(frozen=True)
class ActualLane:
    evidence_root: Path
    run_root: Path
    ocr_engine: Any
    ocr_config_sha256: str


class ObservedOcrEngine:
    """Record the real engine input, then delegate to pinned Tesseract."""

    def __init__(self, engine: Any):
        self._engine = engine
        self.invocations: list[dict[str, Any]] = []

    @property
    def identity(self) -> Any:
        return self._engine.identity

    def extract(self, source: Any, *, limits: Any, deadline: float) -> Any:
        raw = os.pread(source.file_descriptor, source.size_bytes + 1, 0)
        invocation: dict[str, Any] = {
            "input_path_name": Path(source.attachment_path).name,
            "mime_type": source.mime_type,
            "size_bytes": source.size_bytes,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "read_bytes": len(raw),
            "engine_name": self.identity.name,
        }
        self.invocations.append(invocation)
        result = self._engine.extract(source, limits=limits, deadline=deadline)
        invocation["result_status"] = result.status.value
        invocation["result_outcome_code"] = result.outcome_code
        invocation["block_count"] = len(result.blocks)
        return result


def _strict_json(raw: bytes) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def _read_bounded_zip() -> VectorArchive:
    archive_stat = ARCHIVE.lstat()
    assert stat.S_ISREG(archive_stat.st_mode) and not stat.S_ISLNK(archive_stat.st_mode)
    archive_raw = ARCHIVE.read_bytes()
    assert 0 < len(archive_raw) <= MAX_ARCHIVE_BYTES
    with zipfile.ZipFile(ARCHIVE) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        assert len(entries) <= MAX_ARCHIVE_MEMBERS
        assert len(names) == len(set(names)), "ZIP member names must be unique"
        assert set(names) == PAYLOAD_MEMBERS | {"manifest.json"}
        assert sum(entry.file_size for entry in entries) <= MAX_UNCOMPRESSED_BYTES

        for entry in entries:
            assert not entry.is_dir()
            assert entry.filename and Path(entry.filename).name == entry.filename
            assert "/" not in entry.filename and "\\" not in entry.filename
            assert entry.filename not in {".", ".."}
            assert entry.flag_bits & 0x1 == 0, "encrypted ZIP entries are unsupported"
            unix_mode = (entry.external_attr >> 16) & 0xFFFF
            assert not stat.S_ISLNK(unix_mode), "symlink ZIP members are forbidden"
            assert 0 < entry.file_size <= MAX_MEMBER_BYTES
            if entry.filename == "manifest.json":
                assert entry.file_size <= MAX_MANIFEST_BYTES

        manifest = _strict_json(archive.read("manifest.json"))
        assert set(manifest) == {
            "cases",
            "corner_colors_rgb",
            "exif_orientation_oracle",
            "generator",
            "members",
            "provenance",
            "real_or_personal_data",
            "schema_version",
            "synthetic_only",
        }
        assert manifest["schema_version"] == "receipt-media-vector-manifest-v1"
        assert manifest["synthetic_only"] is True
        assert manifest["real_or_personal_data"] is False
        assert set(manifest["cases"]) == set(CASE_IDS)
        assert set(manifest["members"]) == PAYLOAD_MEMBERS

        # The public provenance describes the fixed synthetic assets only; it
        # contains no private evidence paths, task names or prior run names.
        serialized = json.dumps(manifest, sort_keys=True, ensure_ascii=True).lower()
        for forbidden in (
            "/private/",
            "finance-independent-pr02",
            "linux-attempt4",
        ):
            assert forbidden not in serialized
        assert "alpha_generation" in manifest["provenance"]
        assert "filename_generation" in manifest["provenance"]

        payloads: dict[str, bytes] = {}
        for name in sorted(PAYLOAD_MEMBERS):
            raw = archive.read(name)
            expected = manifest["members"][name]
            assert set(expected) == {"sha256", "size_bytes"}
            assert len(raw) == expected["size_bytes"]
            assert hashlib.sha256(raw).hexdigest() == expected["sha256"]
            payloads[name] = raw

    for case_id, case in manifest["cases"].items():
        assert isinstance(case, dict)
        assert case["member"] in payloads
        assert set(case["expected"]) == {"status", "outcome_code"}
    return VectorArchive(manifest, payloads, hashlib.sha256(archive_raw).hexdigest())


@pytest.fixture(scope="session")
def vector_archive() -> VectorArchive:
    return _read_bounded_zip()


@pytest.fixture(scope="session")
def actual_linux_lane(tmp_path_factory: pytest.TempPathFactory) -> ActualLane:
    if os.environ.get("FINANCE_LINUX_MEDIA_REQUIRED") != "1":
        pytest.skip("Actual media vectors run only in the required pinned Ubuntu lane.")

    assert sys.platform == "linux", "Required media lane must execute on Linux"
    assert platform.machine() == "x86_64", "Required media lane must execute on x86_64"
    os_release = platform.freedesktop_os_release()
    assert (os_release.get("ID"), os_release.get("VERSION_ID")) == (
        "ubuntu",
        "24.04",
    ), "Required media lane must execute on Ubuntu 24.04"
    assert hasattr(os, "geteuid") and os.geteuid() != 0, (
        "The bounded worker requires a dedicated non-root Linux UID"
    )

    config_text = os.environ.get("FINANCE_LINUX_OCR_CONFIG")
    assert config_text, "Pinned FINANCE_LINUX_OCR_CONFIG is mandatory in this lane"
    config_path = Path(config_text)
    assert config_path.is_absolute() and config_path.is_file()
    config_stat = config_path.lstat()
    assert stat.S_ISREG(config_stat.st_mode) and not stat.S_ISLNK(config_stat.st_mode)
    assert config_stat.st_uid == os.getuid() and config_stat.st_mode & 0o022 == 0
    config_raw = config_path.read_bytes()
    assert 0 < len(config_raw) <= 65_536

    evidence_text = os.environ.get("FINANCE_LINUX_MEDIA_EVIDENCE")
    assert evidence_text, "FINANCE_LINUX_MEDIA_EVIDENCE is mandatory in this lane"
    evidence_root = Path(evidence_text)
    assert evidence_root.is_absolute()
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence_stat = evidence_root.lstat()
    assert stat.S_ISDIR(evidence_stat.st_mode) and not stat.S_ISLNK(evidence_stat.st_mode)
    assert evidence_stat.st_uid == os.getuid()
    evidence_root.chmod(0o700)
    runs_root = evidence_root / "runs"
    try:
        runs_root.mkdir(mode=0o700)
    except FileExistsError:
        runs_stat = runs_root.lstat()
        assert stat.S_ISDIR(runs_stat.st_mode) and not stat.S_ISLNK(runs_stat.st_mode)
        assert runs_stat.st_uid == os.getuid()
        runs_root.chmod(0o700)
    run_root = runs_root / f"run-{uuid.uuid4().hex}"
    run_root.mkdir(mode=0o700)

    config_workspace = tmp_path_factory.mktemp("receipt-media-ocr-config")
    runtime = config_workspace / "runtime"
    runtime.mkdir(mode=0o700)
    target_config = runtime / "ocr_engine.json"
    fd = os.open(
        target_config,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(config_raw)
        while view:
            written = os.write(fd, view)
            assert written > 0
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

    from finance_core.openclaw_staging_bridge import ocr_boundary

    engine = ocr_boundary.resolve_workspace_ocr_engine(config_workspace)
    assert engine.identity.name == "tesseract_tsv"
    return ActualLane(
        evidence_root,
        run_root,
        engine,
        hashlib.sha256(config_raw).hexdigest(),
    )


@pytest.fixture
def traced_decoder_worker(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from finance_core.intake import receipt_media

    calls: list[dict[str, Any]] = []
    original = receipt_media._worker_run

    def observed(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        if "--source-fd" in arguments:
            calls.append({"worker": Path(arguments[3]).name, "operation_decode": True})
        return original(arguments, **kwargs)

    monkeypatch.setattr(receipt_media, "_worker_run", observed)
    return calls


def _write_captured(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            assert written > 0
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_proof(path: Path, proof: dict[str, Any]) -> None:
    raw = (json.dumps(proof, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("proof write made no progress")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)


def _canonical_digest(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _assert_bounds(normalization: dict[str, Any]) -> None:
    bounds = normalization["bounds"]
    assert bounds == {
        "source_bytes": 20_000_000,
        "pixels": 24_000_000,
        "decoded_bytes": 134_217_728,
        "png_bytes": 20_000_000,
        "temporary_bytes": 268_435_456,
        "address_space_bytes": 536_870_912,
        "cpu_seconds": 30,
        "process_count": 16,
        "open_file_count": 64,
        "stdout_bytes": 262_144,
        "stderr_bytes": 65_536,
        "termination_grace_seconds": 0.25,
        "normalization_wall_seconds": 30.0,
        "concurrency": 1,
    }
    parameters = normalization["parameters"]
    assert parameters == {
        "Pillow": "12.3.0",
        "pillow-heif": "1.8.0",
        "hevc_decoder": "libde265",
        "decode_threads": 1,
        "security_limits_disabled": False,
        "convert_hdr_to_8bit": True,
        "allowed_heif_bit_depths": [8, 10],
        "output_mode": "RGB",
        "alpha_policy": "white_matte",
        "png_compression_level": 6,
        "png_optimize": False,
        "resize": False,
        "orientation_policy": "container_once_exif_metadata_only_refused",
    }


def _assert_corner_oracle(
    case: dict[str, Any], png_raw: bytes, metadata: dict[str, Any]
) -> dict[str, Any]:
    from io import BytesIO

    from PIL import Image

    oracle = case["oracle"]
    with Image.open(BytesIO(png_raw)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert list(image.size) == oracle["dimensions"]
        assert metadata["dimensions"] == oracle["dimensions"]
        inset_x, inset_y = oracle["sample_points_inset"]
        points = {
            "tl": (inset_x, inset_y),
            "tr": (image.width - 1 - inset_x, inset_y),
            "bl": (inset_x, image.height - 1 - inset_y),
            "br": (image.width - 1 - inset_x, image.height - 1 - inset_y),
        }
        observed: dict[str, list[int]] = {}
        for name, point in points.items():
            pixel = image.getpixel(point)
            assert isinstance(pixel, tuple) and len(pixel) == 3
            observed[name] = list(pixel)
    tolerance = oracle["channel_tolerance"]
    for corner, expected in oracle["corners_rgb"].items():
        assert all(
            abs(actual - wanted) <= tolerance
            for actual, wanted in zip(observed[corner], expected, strict=True)
        ), (corner, observed[corner], expected, tolerance)
    return observed


def _assert_pixel_points(
    oracle: dict[str, Any], png_raw: bytes, metadata: dict[str, Any]
) -> dict[str, list[int]]:
    from io import BytesIO

    from PIL import Image

    observed: dict[str, list[int]] = {}
    pixel_oracles = oracle.get("pixels_rgb", oracle)
    with Image.open(BytesIO(png_raw)) as image:
        if "dimensions" in oracle:
            assert list(image.size) == oracle["dimensions"]
        assert image.mode == "RGB"
        for label, point in pixel_oracles.items():
            pixel = image.getpixel(tuple(point["point"]))
            assert isinstance(pixel, tuple) and len(pixel) == 3
            actual = list(pixel)
            wanted = point["rgb"]
            tolerance = point.get("tolerance", oracle.get("channel_tolerance", 0))
            assert all(
                abs(channel - expected) <= tolerance
                for channel, expected in zip(actual, wanted, strict=True)
            ), (label, actual, wanted, tolerance)
            observed[label] = actual
    if "dimensions" in oracle:
        assert metadata["dimensions"] == oracle["dimensions"]
    return observed


def _execute_operation(
    *,
    processor: Any,
    engine: ObservedOcrEngine,
    worker_calls: list[dict[str, Any]],
    source_relative_path: str,
    operation_id: str,
    case: dict[str, Any],
    source_raw: bytes,
    source_path: Path,
    operation_evidence: Path,
    vector_archive_sha256: str,
    ocr_config_sha256: str,
    pixel_reference_source: bytes | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    call_start = len(engine.invocations)
    worker_start = len(worker_calls)
    result = processor.process(
        operation_id=operation_id,
        source_relative_path=source_relative_path,
        expected_source_sha256=hashlib.sha256(source_raw).hexdigest(),
        expected_source_size=len(source_raw),
        declared_mime_type=case.get("declared_mime_type"),
        original_filename=case.get("original_filename"),
        received_via="local",
    )
    assert source_path.read_bytes() == source_raw
    operation_dir = operation_evidence / operation_id
    assert operation_dir.is_dir() and not operation_dir.is_symlink()
    copied_original = operation_dir / "original.bin"
    assert copied_original.read_bytes() == source_raw

    record: dict[str, Any] = {
        "case_id": operation_id.removeprefix("media_vector_"),
        "source_sha256": hashlib.sha256(source_raw).hexdigest(),
        "source_size_bytes": len(source_raw),
        "source_bytes_unchanged": source_path.read_bytes() == source_raw,
        "copied_original_sha256": hashlib.sha256(copied_original.read_bytes()).hexdigest(),
        "expected": case["expected"],
        "result": {
            "operation_id": result.operation_id,
            "status": result.status,
            "outcome_code": result.outcome_code,
            "persistence_idempotent": result.persistence_idempotent,
            "reference": asdict(result.reference) if result.reference is not None else None,
        },
        "decoder_worker_process_count": len(worker_calls) - worker_start,
        "ocr_invocations": engine.invocations[call_start:],
        "archive_sha256": vector_archive_sha256,
        "ocr_configuration_sha256": ocr_config_sha256,
        "actual_tesseract": engine.identity.name == "tesseract_tsv",
        "actual_bounded_linux_worker": True,
    }
    terminal_raw = (operation_dir / "result.json").read_bytes()
    terminal = _strict_json(terminal_raw)
    assert (result.status, result.outcome_code) == (
        case["expected"]["status"],
        case["expected"]["outcome_code"],
    )
    assert terminal["status"] == result.status
    assert terminal["outcome_code"] == result.outcome_code
    record["terminal_result_sha256"] = hashlib.sha256(terminal_raw).hexdigest()
    usage = terminal.get("decoder_process_usage")
    assert isinstance(usage, dict), "worker resource usage must be retained for every case"
    assert usage["wall_seconds"] <= 30.5
    assert usage["user_cpu_seconds"] + usage["system_cpu_seconds"] <= 30.5
    assert usage["max_rss_kib"] <= 512 * 1024
    record["decoder_process_usage"] = usage
    intent = _strict_json((operation_dir / "intent.json").read_bytes())
    record["decoder_identity"] = intent["decoder"]
    record["decoder_parameters"] = intent["decoder_parameters"]
    record["resource_bounds"] = intent["bounds"]
    assert record["decoder_worker_process_count"] == 1

    if result.status != "succeeded":
        assert result.reference is None
        assert len(engine.invocations) == call_start
        assert len(worker_calls) - worker_start == 1
        assert not (operation_dir / "ocr.json").exists()
        record["ocr_status"] = "NOT_RUN"
        return result, None, record

    assert result.reference is not None
    verified = processor.read_verified(result.reference)
    expected_inventory = {
        "intent.json",
        "original.bin",
        "declaration.json",
        "normalized.png",
        "ocr.json",
        "result.json",
    }
    assert {member["name"] for member in verified.inventory} == expected_inventory
    assert len(verified.inventory) == len(expected_inventory)
    assert len(engine.invocations) == call_start + 1
    assert len(worker_calls) - worker_start == 1
    invocation = engine.invocations[-1]
    normalization = verified.manifest["normalization"]
    png = normalization["png"]
    assert invocation["input_path_name"] == "normalized.png"
    assert invocation["mime_type"] == "image/png"
    assert invocation["size_bytes"] == png["size_bytes"]
    assert invocation["read_bytes"] == png["size_bytes"]
    assert invocation["sha256"] == png["sha256"] == result.reference.png_sha256
    assert invocation["engine_name"] == "tesseract_tsv"
    _assert_bounds(normalization)

    metadata = normalization["metadata"]
    assert metadata["output_mode"] == "RGB"
    assert metadata["frame_count"] == 1
    assert metadata["convert_hdr_to_8bit"] is True
    assert metadata["alpha_policy"] == "white_matte"
    assert normalization["decoder"]["packages"] == {
        "Pillow": "12.3.0",
        "pillow-heif": "1.8.0",
    }
    assert verified.manifest["decoder_process_usage"] == usage
    record["normalization"] = normalization
    record["normalization_metadata"] = metadata
    record["normalized_png_sha256"] = png["sha256"]
    record["normalized_png_size_bytes"] = png["size_bytes"]
    record["actual_ocr_input"] = invocation

    png_raw = (operation_dir / "normalized.png").read_bytes()
    assert hashlib.sha256(png_raw).hexdigest() == png["sha256"]
    oracle_observed: dict[str, Any] = {}
    if "corners_rgb" in case.get("oracle", {}):
        oracle_observed["corners_rgb"] = _assert_corner_oracle(case, png_raw, metadata)
    if "pixels_rgb" in case.get("oracle", {}):
        oracle_observed["pixels_rgb"] = _assert_pixel_points(case["oracle"], png_raw, metadata)
    if "pixel_oracle" in case:
        oracle_observed["pixel_oracle"] = _assert_pixel_points(
            case["pixel_oracle"], png_raw, metadata
        )
    if oracle_observed:
        record["known_pixel_oracle_observed"] = oracle_observed

    if case["kind"] in {"jpeg_exif", "png_exif", "png_exif_post_idat"}:
        assert metadata["exif_orientation"] == case["exif_orientation"]
        assert metadata["orientation_policy"] == "raster_exif_once_metadata_removed"
        assert metadata["source_dimensions"] == [160, 112]
        assert metadata["dimensions"] == case["oracle"]["dimensions"]
    if case["kind"] == "heif_container_transform":
        assert metadata["container_transformations"] == case["transformations"]
        assert metadata["orientation_policy"] == "heif_container_once_metadata_removed"
    if case["kind"] == "png_alpha_white_matte":
        from io import BytesIO

        from PIL import Image

        assert metadata["source_has_alpha"] is True
        assert metadata["alpha_policy"] == case["oracle"]["alpha_policy"] == "white_matte"
        with Image.open(BytesIO(source_raw)) as original_image:
            assert original_image.mode == "RGBA"
            assert original_image.getpixel((20, 20)) == (0, 0, 0, 0)
            assert original_image.getpixel((80, 20)) == (10, 20, 30, 128)
    if case["kind"] == "heif_10bit_receipt":
        from io import BytesIO

        from PIL import Image

        assert metadata["original_bit_depth"] == case["original_bit_depth"] == 10
        assert metadata["output_mode"] == case["output_mode"] == "RGB"
        assert case["original_bit_depth"] <= 10
        assert normalization["parameters"]["convert_hdr_to_8bit"] is True
        assert pixel_reference_source is not None
        with Image.open(BytesIO(pixel_reference_source)) as source_raster:
            for point in case["pixel_oracle"].values():
                assert source_raster.getpixel(tuple(point["point"])) == tuple(point["rgb"])
        record["independent_source_pixel_oracle_verified"] = True
    if case["kind"] == "heif_large_supported":
        assert metadata["source_dimensions"] == case["dimensions"]
        assert metadata["dimensions"] == case["dimensions"]
        assert normalization["parameters"]["resize"] is False

    ocr_raw = (operation_dir / "ocr.json").read_bytes()
    ocr_payload = _strict_json(ocr_raw)
    ocr_record = verified.manifest["ocr"]
    result_sha256 = _canonical_digest(ocr_payload)
    assert (
        hashlib.sha256(ocr_raw).hexdigest()
        == hashlib.sha256(
            json.dumps(
                ocr_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()
    )
    assert result_sha256 == ocr_record["result_sha256"] == result.reference.ocr_result_sha256
    expected_fingerprint = _canonical_digest(
        {
            "contract": "receipt-media-ocr-v1",
            "normalization_fingerprint": result.reference.normalization_fingerprint,
            "original": normalization["original"],
            "png": normalization["png"],
            "engine": ocr_record["identity"],
            "limits": ocr_record["limits"],
            "result_sha256": result_sha256,
            "status": ocr_payload["status"],
        }
    )
    assert expected_fingerprint == ocr_record["fingerprint"]
    assert result.reference.ocr_fingerprint == expected_fingerprint
    assert [asdict(block) for block in verified.blocks] == ocr_payload["blocks"]
    record["ocr_status"] = ocr_payload["status"]
    record["ocr_outcome_code"] = ocr_payload["outcome_code"]
    record["ocr_result_sha256"] = result_sha256
    record["completed_ocr_fingerprint"] = expected_fingerprint
    record["ocr_block_count"] = len(verified.blocks)

    text = " ".join(block.text for block in verified.blocks).upper()
    compact_text = "".join(text.split())
    for token in case.get("ocr_contains", []):
        expected = "".join(token.upper().split())
        assert expected in compact_text, (token, text)
    if case.get("ocr_contains"):
        assert ocr_payload["status"] == "succeeded"
    return result, verified, record


def test_vector_archive_is_bounded_synthetic_and_hash_verified(
    vector_archive: VectorArchive,
) -> None:
    from finance_core.intake import receipt_media

    assert len(CASE_IDS) == 49
    assert len(vector_archive.manifest["cases"]) == 49
    assert len(vector_archive.payloads) == 44
    assert vector_archive.archive_sha256 == hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    assert set(vector_archive.manifest["exif_orientation_oracle"]) == {
        str(value) for value in range(1, 9)
    }
    for image_format in ("jpeg_exif", "png_exif"):
        expected = {f"{image_format}_{value}" for value in range(1, 9)}
        assert {case_id for case_id in CASE_IDS if case_id in expected} == expected

    actual_policy = {
        "bounds": receipt_media._BOUNDS,
        "parameters": receipt_media._DECODER_PARAMETERS,
    }
    _assert_bounds(actual_policy)

    extra_bound = {**receipt_media._BOUNDS, "unexpected": 1}
    with pytest.raises(AssertionError):
        _assert_bounds({**actual_policy, "bounds": extra_bound})

    missing_bound = {
        key: value for key, value in receipt_media._BOUNDS.items() if key != "stdout_bytes"
    }
    with pytest.raises(AssertionError):
        _assert_bounds({**actual_policy, "bounds": missing_bound})

    looser_bound = {
        **receipt_media._BOUNDS,
        "source_bytes": receipt_media._BOUNDS["source_bytes"] + 1,
    }
    with pytest.raises(AssertionError):
        _assert_bounds({**actual_policy, "bounds": looser_bound})


@pytest.mark.parametrize("case_id", CASE_IDS, ids=CASE_IDS)
def test_actual_linux_receipt_media_vector(
    case_id: str,
    vector_archive: VectorArchive,
    actual_linux_lane: ActualLane,
    traced_decoder_worker: list[dict[str, Any]],
) -> None:
    from finance_core.intake.receipt_media import ReceiptMediaProcessor

    case = vector_archive.manifest["cases"][case_id]
    source_raw = vector_archive.payloads[case["member"]]
    case_root = actual_linux_lane.run_root / "cases" / case_id
    cases_root = actual_linux_lane.run_root / "cases"
    cases_root.mkdir(mode=0o700, exist_ok=True)
    case_root.mkdir(mode=0o700)
    source_root = case_root / "source"
    evidence_root = case_root / "evidence"
    source_root.mkdir(mode=0o700)
    evidence_root.mkdir(mode=0o700)
    engine = ObservedOcrEngine(actual_linux_lane.ocr_engine)
    if case_id == "heif_duplicate_event_first":
        first_source = source_root / "captured-first.bin"
        second_source = source_root / "captured-second.bin"
        _write_captured(first_source, source_raw)
        _write_captured(second_source, source_raw)
    else:
        source_path = source_root / "captured.bin"
        _write_captured(source_path, source_raw)

    proof: dict[str, Any] = {
        "case_id": case_id,
        "manifest_case": case,
        "source_member": case["member"],
        "source_sha256": hashlib.sha256(source_raw).hexdigest(),
        "source_size_bytes": len(source_raw),
        "vector_archive_sha256": vector_archive.archive_sha256,
        "ocr_configuration_sha256": actual_linux_lane.ocr_config_sha256,
        "actual_linux": True,
        "actual_bounded_worker": False,
        "actual_tesseract_required": True,
        "source_bytes_unchanged": False,
    }
    try:
        processor = ReceiptMediaProcessor(source_root, evidence_root, ocr_engine=engine)
        if case_id == "heif_duplicate_event_first":
            first = vector_archive.manifest["cases"]["heif_duplicate_event_first"]
            second = vector_archive.manifest["cases"]["heif_duplicate_event_second"]
            first_result, first_verified, first_record = _execute_operation(
                processor=processor,
                engine=engine,
                worker_calls=traced_decoder_worker,
                source_relative_path="captured-first.bin",
                operation_id="media_vector_heif_duplicate_event_first",
                case=first,
                source_raw=source_raw,
                source_path=first_source,
                operation_evidence=evidence_root,
                vector_archive_sha256=vector_archive.archive_sha256,
                ocr_config_sha256=actual_linux_lane.ocr_config_sha256,
            )
            second_result, second_verified, second_record = _execute_operation(
                processor=processor,
                engine=engine,
                worker_calls=traced_decoder_worker,
                source_relative_path="captured-second.bin",
                operation_id="media_vector_heif_duplicate_event_second",
                case=second,
                source_raw=source_raw,
                source_path=second_source,
                operation_evidence=evidence_root,
                vector_archive_sha256=vector_archive.archive_sha256,
                ocr_config_sha256=actual_linux_lane.ocr_config_sha256,
            )
            assert first_result.reference is not None and second_result.reference is not None
            assert first_verified is not None and second_verified is not None
            assert first_result.reference.operation_id != second_result.reference.operation_id
            assert first_result.reference != second_result.reference
            assert first_result.reference.original_sha256 == second_result.reference.original_sha256
            assert first_result.reference.png_sha256 == second_result.reference.png_sha256
            assert len(traced_decoder_worker) == 2
            assert len(engine.invocations) == 2
            proof["duplicate_source_distinct_events"] = {
                "same_original_sha256": first_result.reference.original_sha256,
                "first_reference": asdict(first_result.reference),
                "second_reference": asdict(second_result.reference),
                "first_case_proof": first_record,
                "second_case_proof": second_record,
            }
            proof["source_bytes_unchanged"] = (
                first_source.read_bytes() == source_raw and second_source.read_bytes() == source_raw
            )
        else:
            operation_id = f"media_vector_{case_id}"
            result, verified, operation_record = _execute_operation(
                processor=processor,
                engine=engine,
                worker_calls=traced_decoder_worker,
                source_relative_path="captured.bin",
                operation_id=operation_id,
                case=case,
                source_raw=source_raw,
                source_path=source_path,
                operation_evidence=evidence_root,
                vector_archive_sha256=vector_archive.archive_sha256,
                ocr_config_sha256=actual_linux_lane.ocr_config_sha256,
                pixel_reference_source=(
                    vector_archive.payloads["synthetic_receipt_10bit_source.png"]
                    if "pixel_oracle" in case
                    else None
                ),
            )
            proof["operation"] = operation_record
            proof["source_bytes_unchanged"] = source_path.read_bytes() == source_raw
            if case["kind"] == "exact_operation_replay":
                assert result.reference is not None and verified is not None
                calls_before_replay = len(engine.invocations)
                workers_before_replay = len(traced_decoder_worker)
                replay = processor.process(
                    operation_id=operation_id,
                    source_relative_path="captured.bin",
                    expected_source_sha256=hashlib.sha256(source_raw).hexdigest(),
                    expected_source_size=len(source_raw),
                    declared_mime_type=case["declared_mime_type"],
                    original_filename=case["original_filename"],
                    received_via="local",
                )
                assert replay.reference == result.reference
                assert replay.reference is not None
                assert replay.persistence_idempotent is True
                assert len(engine.invocations) == calls_before_replay
                assert len(traced_decoder_worker) == workers_before_replay
                assert processor.read_verified(replay.reference) == verified
                proof["replay"] = {
                    "same_reference": True,
                    "persistence_idempotent": replay.persistence_idempotent,
                    "additional_worker_calls": len(traced_decoder_worker) - workers_before_replay,
                    "additional_ocr_calls": len(engine.invocations) - calls_before_replay,
                }
    finally:
        proof["decoder_worker_process_count"] = len(traced_decoder_worker)
        proof["actual_tesseract_calls"] = len(engine.invocations)
        proof["actual_tesseract"] = engine.identity.name == "tesseract_tsv"
        proof["actual_tesseract_invoked"] = bool(engine.invocations)
        proof["actual_bounded_worker"] = bool(traced_decoder_worker)
        proof["actual_bounded_worker_executed"] = bool(traced_decoder_worker)
        if case_id == "heif_duplicate_event_first":
            proof["source_bytes_unchanged"] = (
                first_source.read_bytes() == source_raw and second_source.read_bytes() == source_raw
            )
        else:
            proof["source_bytes_unchanged"] = source_path.read_bytes() == source_raw
        proof["test_platform"] = {
            "system": sys.platform,
            "machine": platform.machine(),
            "uid_non_root": bool(hasattr(os, "geteuid") and os.geteuid() != 0),
        }
        _write_proof(case_root / "proof.json", proof)
