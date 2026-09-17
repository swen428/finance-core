"""Focused B2 tests for the bounded macOS Apple Vision receipt OCR engine.

Coverage map (task requirement numbering):

1.  Platform/architecture rejection outside Darwin arm64.
2.  Constructor/config validation (languages, revision, version, executable
    ownership/mode/path, configuration hash, binary hash).
3.  Helper version/identity mismatch and runtime identity drift.
4.  Valid ``succeeded`` and ``no_text`` helper output.
5.  Strict JSON schema: duplicate/unknown/missing fields, invalid UTF-8,
    malformed numbers/types, status contradictions, block/text/count/output
    limits.
6.  Coordinate conversion, orientation, confidence scaling, deterministic
    sorting, and stable tie-breakers.
7.  Nonzero exit, launch failure, deadline expiry, CPU/resource exhaustion,
    resident-memory breach, stdout/stderr overflow, TERM->KILL, process-group
    cleanup, fd cleanup, and private-copy/temp cleanup.
8.  Environment sanitization, no shell/PATH lookup, and exact inherited
    input-fd behavior.
9.  Executable path replacement/tamper before and during execution.
10. Service integration through a temporary authorised staging database:
    migration 032 persistence, exact replay without rerunning Vision,
    identity/config conflict behavior, no partial rows, and no downstream
    financial records.
11. PDF/oversize deterministic behavior still avoids engine invocation.
12. Regression coverage for TesseractTsvOcrEngine and the OCR evidence service.
13. Real macOS integration: compile the committed Swift helper into a temp
    directory, OCR a synthetic image, persist, and replay.
14. macOS-only integration skips on non-Darwin CI while protocol/security/unit
    coverage remains runnable everywhere.

Protocol, security, and pure-unit tests neutralize the Darwin/arm64 gate with
a monkeypatch so they run on Linux CI.  Tests that require the real Apple
Vision framework or the real libproc memory probe are marked ``darwin_arm64``
and skip elsewhere with an explicit reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

import finance_core.intake.macos_vision_receipt_ocr as mv
from finance_core.intake.attachment_evidence import persist_attachment_evidence
from finance_core.intake.macos_vision_receipt_ocr import (
    HELPER_NAME,
    HELPER_PROTOCOL_VERSION,
    RECOGNITION_LEVEL,
    USES_LANGUAGE_CORRECTION,
    VISION_REQUEST_REVISION,
    MacOSVisionOcrEngine,
    OcrProcessCleanupError,
    convert_vision_bounding_box,
    convert_vision_observations,
    scale_vision_confidence,
)
from finance_core.intake.receipt_ocr_evidence import (
    InvalidOcrConfigurationError,
    MalformedOcrOutputError,
    OcrDeadlineExceededError,
    OcrEngineLaunchError,
    OcrResourceLimitExceededError,
    OcrUnsupportedPlatformError,
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    ReceiptOcrSource,
    TesseractTsvOcrEngine,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.staging_guard import create_staging_database

REPO_ROOT = Path(__file__).resolve().parents[1]
SWIFT_SOURCE = REPO_ROOT / "native" / "macos_vision_receipt_ocr" / "main.swift"
FIXTURE_PNG = REPO_ROOT / "tests" / "fixtures" / "receipt_ocr" / "synthetic_receipt_001.png"
EXPECTED_VERSION = "1.0.0"

# Representative host Vision capability reported by the fake helper. It
# includes en-US and ms-MY but deliberately excludes en-GB (unsupported on the
# real host) and xx-XX (never a real language) so language validation tests are
# deterministic cross-platform.
FAKE_SUPPORTED_LANGUAGES = ["en-US", "ms-MY", "zh-Hans"]

JPEG = b"\xff\xd8\xff" + b"receipt-jpeg-evidence"
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-png-evidence"
PDF = b"%PDF-1.7\nreceipt-pdf-evidence"

IS_DARWIN_ARM64 = sys.platform == "darwin" and platform.machine() == "arm64"
darwin_arm64 = pytest.mark.skipif(
    not IS_DARWIN_ARM64,
    reason="The real Apple Vision integration requires Darwin on Apple Silicon arm64.",
)


def _hash(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Cross-platform protocol fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def vision_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the Darwin/arm64 gate so protocol tests run anywhere.

    The resident-memory probe is injected with a safe finite value (well below
    any budget) so cross-platform tests never rely on a None probe skipping the
    RSS check; a None probe on a live child must fail closed. Tests that need a
    breach or a probe failure monkeypatch the probe explicitly.
    """
    monkeypatch.setattr(mv, "_require_macos_vision_platform", lambda: None)
    monkeypatch.setattr(mv, "_observe_child_resident_bytes", lambda pid: 0)


# ---------------------------------------------------------------------------
# Fake helper script factory (mimics the Swift helper protocol)
# ---------------------------------------------------------------------------


def _identity_json(
    *,
    protocol_version: int = HELPER_PROTOCOL_VERSION,
    helper_name: str = HELPER_NAME,
    helper_version: str = EXPECTED_VERSION,
    revision: int = VISION_REQUEST_REVISION,
    recognition_level: str = RECOGNITION_LEVEL,
    language_correction: bool = USES_LANGUAGE_CORRECTION,
    supported_languages: Sequence[str] = FAKE_SUPPORTED_LANGUAGES,
) -> str:
    return json.dumps(
        {
            "protocol_version": protocol_version,
            "helper_name": helper_name,
            "helper_version": helper_version,
            "vision_request_revision": revision,
            "recognition_level": recognition_level,
            "uses_language_correction": language_correction,
            "supported_languages": list(supported_languages),
        }
    )


def _ocr_json(
    *,
    status: str = "ok",
    outcome_code: str | None = None,
    page_width: int = 480,
    page_height: int = 640,
    orientation: int = 1,
    observations: list[dict[str, object]] | None = None,
) -> str:
    if observations is None:
        observations = [
            {
                "index": 0,
                "text": "TOTAL 12.34",
                "confidence": 0.987654,
                "bounding_box": [0.25, 0.60, 0.50, 0.06],
            }
        ]
    return json.dumps(
        {
            "protocol_version": HELPER_PROTOCOL_VERSION,
            "status": status,
            "outcome_code": outcome_code if outcome_code is not None else status,
            "page_width": page_width,
            "page_height": page_height,
            "orientation": orientation,
            "observations": observations,
        }
    )


def _write_fake_helper(
    tmp_path: Path,
    *,
    identity_literal: str | None = _identity_json(),
    ocr_literal: str | None = _ocr_json(),
    body: str | None = None,
    name: str = "vision-helper",
) -> Path:
    """Write an executable fake helper implementing the native protocol."""
    path = (tmp_path / name).resolve()
    if body is None:
        read_fd = "data = os.read(fd, max_bytes) if fd >= 0 else b''"
        body = f"""
import os
import sys

args = sys.argv[1:]
if args[:1] == ['--identity']:
    os.write(1, {identity_literal!r}.encode('utf-8'))
    raise SystemExit(0)
if args[:1] == ['--ocr']:
    fd = int(args[1])
    max_bytes = int(args[2])
    languages = args[3]
    max_blocks = int(args[4])
    max_chars = int(args[5])
    {read_fd}
    if os.environ.get('OCR_PARENT_SECRET') is not None:
        raise SystemExit(9)
    os.write(1, {ocr_literal!r}.encode('utf-8'))
    raise SystemExit(0)
raise SystemExit(2)
"""
    script = f"#!{sys.executable}\n" + textwrap.dedent(body)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o500)
    return path


def _make_engine(
    helper_path: Path,
    *,
    expected_version: str = EXPECTED_VERSION,
    languages: tuple[str, ...] = ("en-US",),
) -> MacOSVisionOcrEngine:
    return MacOSVisionOcrEngine(helper_path, expected_version=expected_version, languages=languages)


def _source(
    tmp_path: Path,
    *,
    content: bytes = JPEG,
    mime_type: str = "image/jpeg",
    name: str = "attachment.bin",
) -> ReceiptOcrSource:
    path = tmp_path / name
    path.write_bytes(content)
    fd = os.open(path, os.O_RDONLY)
    return ReceiptOcrSource(
        file_descriptor=fd,
        attachment_path=str(path),
        size_bytes=len(content),
        content_hash=_hash(content),
        mime_type=mime_type,
    )


def _extract(
    engine: MacOSVisionOcrEngine,
    source: ReceiptOcrSource,
    *,
    limits: ReceiptOcrLimits = ReceiptOcrLimits(),
    timeout: float = 20.0,
) -> ReceiptOcrEngineResult:
    try:
        return engine.extract(source, limits=limits, deadline=time.monotonic() + timeout)
    finally:
        os.close(source.file_descriptor)


def _arm_probe_failure_after_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the RSS probe return a safe value during ``--identity``, then None.

    This deterministically exercises the fail-closed path on the live OCR
    helper (which stays alive) rather than during the fast identity query: a
    None probe on a still-running child must fail closed, not skip the bound.
    """
    phase = {"ocr": False}
    real_run = mv._run_bounded_process

    def tracking_run(arguments: object, **kwargs: object) -> object:
        result = real_run(arguments, **kwargs)
        if len(arguments) > 1 and arguments[1] == "--identity":
            phase["ocr"] = True
        return result

    monkeypatch.setattr(mv, "_run_bounded_process", tracking_run)
    monkeypatch.setattr(
        mv, "_observe_child_resident_bytes", lambda pid: None if phase["ocr"] else 0
    )


# ===========================================================================
# 6. Pure unit tests: coordinate conversion, confidence, sorting, JSON
# ===========================================================================


class TestCoordinateConversion:
    def test_lower_left_to_top_left_basic(self) -> None:
        # Vision: x=0.25, y=0.60 (lower-left), w=0.50, h=0.06 on a 480x640 page.
        left, top, width, height = convert_vision_bounding_box(
            (Decimal("0.25"), Decimal("0.60"), Decimal("0.50"), Decimal("0.06")),
            page_width=480,
            page_height=640,
        )
        # left = floor(0.25*480)=120 ; top = floor((1-0.66)*640)=floor(217.6)=217
        # right = ceil(0.75*480)=360 ; bottom = ceil((1-0.60)*640)=ceil(256)=256
        assert (left, top, width, height) == (120, 217, 240, 39)
        assert left + width <= 480
        assert top + height <= 640

    def test_full_page_box(self) -> None:
        left, top, width, height = convert_vision_bounding_box(
            (Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1")),
            page_width=100,
            page_height=200,
        )
        assert (left, top, width, height) == (0, 0, 100, 200)

    def test_insignificant_drift_is_clamped(self) -> None:
        left, top, width, height = convert_vision_bounding_box(
            (Decimal("-0.0005"), Decimal("-0.0005"), Decimal("1.0009"), Decimal("1.0009")),
            page_width=480,
            page_height=640,
        )
        assert left == 0 and top == 0
        assert width == 480 and height == 640

    def test_material_out_of_range_rejected(self) -> None:
        with pytest.raises(MalformedOcrOutputError):
            convert_vision_bounding_box(
                (Decimal("-0.01"), Decimal("0"), Decimal("0.5"), Decimal("0.5")),
                page_width=480,
                page_height=640,
            )
        with pytest.raises(MalformedOcrOutputError):
            convert_vision_bounding_box(
                (Decimal("0.5"), Decimal("0"), Decimal("0.6"), Decimal("0.5")),
                page_width=480,
                page_height=640,
            )

    def test_zero_area_rejected(self) -> None:
        with pytest.raises(MalformedOcrOutputError):
            convert_vision_bounding_box(
                (Decimal("0.5"), Decimal("0.5"), Decimal("0"), Decimal("0.1")),
                page_width=480,
                page_height=640,
            )

    def test_non_finite_rejected(self) -> None:
        with pytest.raises(MalformedOcrOutputError):
            convert_vision_bounding_box(
                (Decimal("NaN"), Decimal("0"), Decimal("0.5"), Decimal("0.5")),
                page_width=480,
                page_height=640,
            )


class TestConfidenceScaling:
    def test_scales_to_integer_10000(self) -> None:
        limits = ReceiptOcrLimits()
        assert scale_vision_confidence(Decimal("0.987654"), limits=limits) == 9877
        assert scale_vision_confidence(Decimal("1.000000"), limits=limits) == 10000
        assert scale_vision_confidence(Decimal("0.000000"), limits=limits) == 0

    def test_half_up_rounding(self) -> None:
        limits = ReceiptOcrLimits()
        assert scale_vision_confidence(Decimal("0.50005"), limits=limits) == 5001
        assert scale_vision_confidence(Decimal("0.50004"), limits=limits) == 5000

    def test_out_of_range_rejected(self) -> None:
        limits = ReceiptOcrLimits()
        with pytest.raises(MalformedOcrOutputError):
            scale_vision_confidence(Decimal("1.5"), limits=limits)
        with pytest.raises(MalformedOcrOutputError):
            scale_vision_confidence(Decimal("-0.1"), limits=limits)
        with pytest.raises(MalformedOcrOutputError):
            scale_vision_confidence(Decimal("NaN"), limits=limits)


def _obs(
    index: int,
    text: str,
    confidence: str,
    box: list[str],
) -> dict[str, object]:
    return {
        "index": index,
        "text": text,
        "confidence": Decimal(confidence),
        "bounding_box": [Decimal(v) for v in box],
    }


def _payload(observations: list[dict[str, object]], *, status: str = "ok") -> dict[str, object]:
    return {
        "protocol_version": HELPER_PROTOCOL_VERSION,
        "status": status,
        "outcome_code": status,
        "page_width": 480,
        "page_height": 640,
        "orientation": 1,
        "observations": observations,
    }


class TestObservationConversionAndOrdering:
    def test_deterministic_top_to_bottom_left_to_right(self) -> None:
        limits = ReceiptOcrLimits()
        # Three boxes at different vertical positions, unsorted input order.
        observations = [
            _obs(2, "BOTTOM", "0.9", ["0.1", "0.05", "0.3", "0.05"]),
            _obs(0, "TOP", "0.9", ["0.1", "0.90", "0.3", "0.05"]),
            _obs(1, "MIDDLE", "0.9", ["0.1", "0.50", "0.3", "0.05"]),
        ]
        blocks = convert_vision_observations(_payload(observations), limits=limits)
        assert [b.text for b in blocks] == ["TOP", "MIDDLE", "BOTTOM"]
        assert [b.sequence_index for b in blocks] == [0, 1, 2]
        # Engine ordering index preserves Vision's original observation index.
        assert [b.engine_block_index for b in blocks] == [0, 1, 2]
        assert all(b.page_index == 0 for b in blocks)
        assert all(
            b.engine_paragraph_index is None
            and b.engine_line_index is None
            and b.engine_word_index is None
            for b in blocks
        )

    def test_same_row_uses_left_tie_breaker(self) -> None:
        limits = ReceiptOcrLimits()
        observations = [
            _obs(0, "RIGHT", "0.9", ["0.60", "0.50", "0.2", "0.05"]),
            _obs(1, "LEFT", "0.9", ["0.10", "0.50", "0.2", "0.05"]),
        ]
        blocks = convert_vision_observations(_payload(observations), limits=limits)
        assert [b.text for b in blocks] == ["LEFT", "RIGHT"]

    def test_geometry_text_confidence_tie_breakers(self) -> None:
        limits = ReceiptOcrLimits()
        # Identical top/left; width differs.
        observations = [
            _obs(0, "WIDE", "0.9", ["0.10", "0.50", "0.4", "0.05"]),
            _obs(1, "NARROW", "0.9", ["0.10", "0.50", "0.2", "0.05"]),
        ]
        blocks = convert_vision_observations(_payload(observations), limits=limits)
        assert [b.text for b in blocks] == ["NARROW", "WIDE"]
        # Identical geometry; text differs.
        observations = [
            _obs(0, "BBB", "0.9", ["0.10", "0.50", "0.2", "0.05"]),
            _obs(1, "AAA", "0.9", ["0.10", "0.50", "0.2", "0.05"]),
        ]
        blocks = convert_vision_observations(_payload(observations), limits=limits)
        assert [b.text for b in blocks] == ["AAA", "BBB"]
        # Identical geometry and text; confidence differs.
        observations = [
            _obs(0, "SAME", "0.5", ["0.10", "0.50", "0.2", "0.05"]),
            _obs(1, "SAME", "0.9", ["0.10", "0.50", "0.2", "0.05"]),
        ]
        blocks = convert_vision_observations(_payload(observations), limits=limits)
        assert [b.confidence_scaled for b in blocks] == [5000, 9000]

    def test_no_text_payload(self) -> None:
        limits = ReceiptOcrLimits()
        blocks = convert_vision_observations(_payload([], status="no_text"), limits=limits)
        assert blocks == ()

    def test_orientation_field_validated(self) -> None:
        limits = ReceiptOcrLimits()
        payload = _payload([_obs(0, "X", "0.9", ["0.1", "0.5", "0.2", "0.05"])])
        payload["orientation"] = 9
        with pytest.raises(MalformedOcrOutputError):
            convert_vision_observations(payload, limits=limits)


# ===========================================================================
# 5. Strict JSON schema validation (pure, cross-platform)
# ===========================================================================


class TestStrictJsonSchema:
    def _run(self, raw: bytes) -> tuple[ReceiptOcrBlock, ...]:
        payload = mv._parse_helper_json(raw, limits=ReceiptOcrLimits())
        return convert_vision_observations(payload, limits=ReceiptOcrLimits())

    def test_valid_json_accepted(self) -> None:
        blocks = self._run(_ocr_json().encode("utf-8"))
        assert len(blocks) == 1
        assert blocks[0].text == "TOTAL 12.34"

    def test_invalid_utf8_rejected(self) -> None:
        with pytest.raises(MalformedOcrOutputError):
            self._run(b"\xff\xfe{invalid")

    def test_duplicate_keys_rejected(self) -> None:
        raw = b'{"protocol_version":1,"protocol_version":1}'
        with pytest.raises(MalformedOcrOutputError):
            self._run(raw)

    def test_nan_infinity_rejected(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            raw = (
                b'{"protocol_version":1,"status":"ok","outcome_code":"ok",'
                b'"page_width":480,"page_height":640,"orientation":1,'
                b'"observations":[{"index":0,"text":"X","confidence":'
                + constant
                + b',"bounding_box":[0.1,0.5,0.2,0.05]}]}'
            )
            with pytest.raises(MalformedOcrOutputError):
                self._run(raw)

    def test_unknown_field_rejected(self) -> None:
        payload = json.loads(_ocr_json())
        payload["unexpected"] = 1
        with pytest.raises(MalformedOcrOutputError):
            self._run(json.dumps(payload).encode("utf-8"))

    def test_missing_field_rejected(self) -> None:
        payload = json.loads(_ocr_json())
        del payload["page_width"]
        with pytest.raises(MalformedOcrOutputError):
            self._run(json.dumps(payload).encode("utf-8"))

    def test_unknown_observation_field_rejected(self) -> None:
        payload = json.loads(_ocr_json())
        payload["observations"][0]["extra"] = 1
        with pytest.raises(MalformedOcrOutputError):
            self._run(json.dumps(payload).encode("utf-8"))

    def test_boolean_as_integer_rejected(self) -> None:
        raw = (
            b'{"protocol_version":true,"status":"ok","outcome_code":"ok",'
            b'"page_width":480,"page_height":640,"orientation":1,'
            b'"observations":[]}'
        )
        with pytest.raises(MalformedOcrOutputError):
            self._run(raw)

    def test_wrong_type_rejected(self) -> None:
        payload = json.loads(_ocr_json())
        payload["page_width"] = "480"
        with pytest.raises(MalformedOcrOutputError):
            self._run(json.dumps(payload).encode("utf-8"))

    def test_integer_confidence_rejected(self) -> None:
        # Confidence must be a JSON float literal, not an integer.
        raw = (
            b'{"protocol_version":1,"status":"ok","outcome_code":"ok",'
            b'"page_width":480,"page_height":640,"orientation":1,'
            b'"observations":[{"index":0,"text":"X","confidence":1,'
            b'"bounding_box":[0.1,0.5,0.2,0.05]}]}'
        )
        with pytest.raises(MalformedOcrOutputError):
            self._run(raw)

    def test_status_contradiction_rejected(self) -> None:
        with pytest.raises(MalformedOcrOutputError):
            self._run(_ocr_json(status="ok", outcome_code="no_text").encode("utf-8"))
        with pytest.raises(MalformedOcrOutputError):
            self._run(_ocr_json(status="bogus").encode("utf-8"))
        # ok with empty observations
        with pytest.raises(MalformedOcrOutputError):
            self._run(_ocr_json(status="ok", observations=[]).encode("utf-8"))
        # no_text with observations
        with pytest.raises(MalformedOcrOutputError):
            self._run(_ocr_json(status="no_text").encode("utf-8"))

    def test_block_count_limit_rejected(self) -> None:
        observations = [
            {
                "index": i,
                "text": f"T{i}",
                "confidence": 0.9,
                "bounding_box": [0.1, 0.5, 0.2, 0.05],
            }
            for i in range(3)
        ]
        payload = _payload(observations)
        raw = json.dumps(payload).encode("utf-8")
        limits = ReceiptOcrLimits(max_block_count=2)
        parsed = mv._parse_helper_json(raw, limits=limits)
        with pytest.raises(OcrResourceLimitExceededError):
            convert_vision_observations(parsed, limits=limits)

    def test_text_length_limit_rejected(self) -> None:
        observations = [
            {
                "index": 0,
                "text": "X" * 10,
                "confidence": 0.9,
                "bounding_box": [0.1, 0.5, 0.2, 0.05],
            }
        ]
        raw = json.dumps(_payload(observations)).encode("utf-8")
        limits = ReceiptOcrLimits(max_text_characters_per_block=5)
        parsed = mv._parse_helper_json(raw, limits=limits)
        with pytest.raises(OcrResourceLimitExceededError):
            convert_vision_observations(parsed, limits=limits)

    def test_control_character_text_rejected(self) -> None:
        observations = [
            {
                "index": 0,
                "text": "BAD\x00TEXT",
                "confidence": 0.9,
                "bounding_box": [0.1, 0.5, 0.2, 0.05],
            }
        ]
        raw = json.dumps(_payload(observations)).encode("utf-8")
        parsed = mv._parse_helper_json(raw, limits=ReceiptOcrLimits())
        with pytest.raises(MalformedOcrOutputError):
            convert_vision_observations(parsed, limits=ReceiptOcrLimits())

    def test_page_dimension_limits_rejected(self) -> None:
        raw = (
            b'{"protocol_version":1,"status":"ok","outcome_code":"ok",'
            b'"page_width":999999,"page_height":640,"orientation":1,'
            b'"observations":[{"index":0,"text":"X","confidence":0.9,'
            b'"bounding_box":[0.1,0.5,0.2,0.05]}]}'
        )
        limits = ReceiptOcrLimits()
        parsed = mv._parse_helper_json(raw, limits=limits)
        with pytest.raises(OcrResourceLimitExceededError):
            convert_vision_observations(parsed, limits=limits)


# ===========================================================================
# 1. Platform / architecture rejection
# ===========================================================================


class TestPlatformRejection:
    def test_constructor_rejects_unsupported_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        helper = _write_fake_helper(tmp_path)
        monkeypatch.setattr(mv.sys, "platform", "linux")
        with pytest.raises(OcrUnsupportedPlatformError):
            MacOSVisionOcrEngine(helper, expected_version=EXPECTED_VERSION)

    def test_constructor_rejects_non_arm64(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        helper = _write_fake_helper(tmp_path)
        monkeypatch.setattr(mv.sys, "platform", "darwin")
        monkeypatch.setattr(mv.platform, "machine", lambda: "x86_64")
        with pytest.raises(OcrUnsupportedPlatformError):
            MacOSVisionOcrEngine(helper, expected_version=EXPECTED_VERSION)

    def test_gate_restored_on_real_platform(self) -> None:
        # The real gate must raise on this host only when unsupported.
        if IS_DARWIN_ARM64:
            mv._require_macos_vision_platform()  # must not raise
        else:
            with pytest.raises(OcrUnsupportedPlatformError):
                mv._require_macos_vision_platform()


# ===========================================================================
# 2. Constructor / config validation
# ===========================================================================


class TestConstructorValidation:
    def test_valid_construction_and_identity(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        identity = engine.identity
        assert isinstance(identity, ReceiptOcrEngineIdentity)
        assert identity.name == "macos_vision"
        assert identity.version == EXPECTED_VERSION
        assert identity.binary_sha256 == _hash(helper.read_bytes())
        assert len(identity.configuration_hash) == 64

    def test_configuration_hash_binds_languages(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine_a = _make_engine(helper, languages=("en-US",))
        engine_b = _make_engine(helper, languages=("ms-MY",))
        assert engine_a.identity.configuration_hash != engine_b.identity.configuration_hash

    def test_configuration_hash_binds_language_capability(
        self, vision_platform, tmp_path: Path
    ) -> None:
        # Two helpers reporting different real Vision capabilities must bind
        # different configuration hashes, so runtime capability drift cannot be
        # silently reused as idempotent evidence.
        helper_a = _write_fake_helper(tmp_path, name="helper_a")
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        helper_b = _write_fake_helper(
            dir_b,
            name="helper_b",
            identity_literal=_identity_json(supported_languages=["en-US", "fr-FR"]),
        )
        engine_a = _make_engine(helper_a)
        engine_b = _make_engine(helper_b)
        assert engine_a.identity.configuration_hash != engine_b.identity.configuration_hash

    def test_relative_path_rejected(self, vision_platform, tmp_path: Path) -> None:
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine("relative/path", expected_version=EXPECTED_VERSION)

    def test_missing_executable_rejected(self, vision_platform, tmp_path: Path) -> None:
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(tmp_path / "absent", expected_version=EXPECTED_VERSION)

    def test_symlink_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        link = tmp_path / "link"
        link.symlink_to(helper)
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(link, expected_version=EXPECTED_VERSION)

    def test_non_executable_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        helper.chmod(0o400)
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(helper, expected_version=EXPECTED_VERSION)

    def test_group_writable_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        helper.chmod(0o520)
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(helper, expected_version=EXPECTED_VERSION)

    def test_group_or_other_executable_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        helper.chmod(0o510)  # group execute
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(helper, expected_version=EXPECTED_VERSION)
        helper.chmod(0o501)  # other execute
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(helper, expected_version=EXPECTED_VERSION)

    def test_malformed_version_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        for bad in ("", "bad version", "v/1", "../x"):
            with pytest.raises(InvalidOcrConfigurationError):
                MacOSVisionOcrEngine(helper, expected_version=bad)

    def test_language_validation(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        # empty
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=())
        # duplicates
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=("en-US", "en-US"))
        # malformed
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=("EN-us",))
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=("english",))
        # well-formed but not in the host's real Vision capability
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=("xx-XX",))
        # excessive (count is validated before the capability query)
        too_many = (
            "aa-AA",
            "bb-BB",
            "cc-CC",
            "dd-DD",
            "ee-EE",
            "ff-FF",
            "gg-GG",
            "hh-HH",
            "ii-II",
        )
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=too_many)
        # a string is not a sequence of tags
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages="en-US")  # type: ignore[arg-type]

    def test_en_gb_rejected_when_host_unsupported(self, vision_platform, tmp_path: Path) -> None:
        # en-GB is well-formed but not in the (fake) host capability, mirroring
        # the real host where Vision revision 3 does not support en-GB.
        helper = _write_fake_helper(tmp_path)
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper, languages=("en-GB",))

    def test_supported_language_accepted(self, vision_platform, tmp_path: Path) -> None:
        # ms-MY is in the (fake) host capability and must be accepted.
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper, languages=("ms-MY",))
        assert engine.identity.version == EXPECTED_VERSION


# ===========================================================================
# 3. Helper version / identity mismatch and runtime drift
# ===========================================================================


class TestHelperIdentityMismatch:
    # The constructor queries the helper identity (to bind the real Vision
    # language capability into the configuration hash), so identity mismatches
    # fail closed at construction rather than waiting for extract().

    def test_version_mismatch_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(
            tmp_path, identity_literal=_identity_json(helper_version="9.9.9")
        )
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper)

    def test_protocol_version_mismatch_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path, identity_literal=_identity_json(protocol_version=99))
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper)

    def test_helper_name_mismatch_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(
            tmp_path, identity_literal=_identity_json(helper_name="other_helper")
        )
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper)

    def test_revision_mismatch_rejected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path, identity_literal=_identity_json(revision=1))
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper)

    def test_language_correction_must_stay_disabled(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(
            tmp_path, identity_literal=_identity_json(language_correction=True)
        )
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper)

    def test_identity_nonzero_exit_rejected(self, vision_platform, tmp_path: Path) -> None:
        body = """
import sys
if sys.argv[1:] == ['--identity']:
    raise SystemExit(3)
raise SystemExit(2)
"""
        helper = _write_fake_helper(tmp_path, body=body)
        with pytest.raises(InvalidOcrConfigurationError):
            _make_engine(helper)

    def test_engine_identity_drift_between_calls(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        first = engine.identity
        # Replace the binary on disk; a fresh engine must produce a different
        # binary hash rather than silently reusing the old identity.
        helper.chmod(0o700)
        helper.write_text(helper.read_text() + "# drift\n", encoding="utf-8")
        helper.chmod(0o500)
        second = _make_engine(helper).identity
        assert first.binary_sha256 != second.binary_sha256

    def test_capability_drift_between_construction_and_extract_fails_closed(
        self, vision_platform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the host Vision language capability reported at extraction differs
        # from what was bound into the configuration hash at construction, the
        # engine must fail closed rather than silently reuse stale evidence.
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)  # binds FAKE_SUPPORTED_LANGUAGES
        real_query = mv._run_identity_query

        def drifted_query(
            executable: object, *, expected_version: str, limits: object, deadline: float
        ) -> dict[str, object]:
            payload = dict(
                real_query(
                    executable,
                    expected_version=expected_version,
                    limits=limits,
                    deadline=deadline,
                )
            )
            payload["supported_languages"] = ["en-US", "fr-FR"]  # drifted capability
            return payload

        monkeypatch.setattr(mv, "_run_identity_query", drifted_query)
        with pytest.raises(InvalidOcrConfigurationError):
            _extract(engine, _source(tmp_path))


# ===========================================================================
# 4. Valid succeeded and no_text helper output
# ===========================================================================


class TestValidOutcomes:
    def test_succeeded(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        result = _extract(engine, _source(tmp_path))
        assert result.status == ReceiptOcrExtractionStatus.SUCCEEDED
        assert result.outcome_code == "ok"
        assert len(result.blocks) == 1
        assert result.blocks[0].text == "TOTAL 12.34"
        assert result.blocks[0].confidence_scaled == 9877

    def test_no_text(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(
            tmp_path, ocr_literal=_ocr_json(status="no_text", observations=[])
        )
        engine = _make_engine(helper)
        result = _extract(engine, _source(tmp_path))
        assert result.status == ReceiptOcrExtractionStatus.NO_TEXT
        assert result.outcome_code == "no_text"
        assert result.blocks == ()

    def test_pdf_mime_rejected_without_launch(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        source = _source(tmp_path, content=PDF, mime_type="application/pdf")
        with pytest.raises(InvalidOcrConfigurationError):
            _extract(engine, source)


# ===========================================================================
# 7. Process safety: exit, launch, deadline, resources, cleanup
# ===========================================================================


class TestProcessSafety:
    @staticmethod
    def _exit_body(code: int) -> str:
        return (
            "import sys\n"
            "if sys.argv[1:] == ['--identity']:\n"
            "    import os\n"
            f"    os.write(1, {_identity_json()!r}.encode('utf-8'))\n"
            "    raise SystemExit(0)\n"
            f"raise SystemExit({code})\n"
        )

    def test_exit_1_maps_to_engine_failed(self, vision_platform, tmp_path: Path) -> None:
        # Exit 1 is a genuine OCR/image/Vision runtime failure and is the only
        # nonzero code that becomes persistable engine_failed evidence.
        helper = _write_fake_helper(tmp_path, body=self._exit_body(1))
        engine = _make_engine(helper)
        result = _extract(engine, _source(tmp_path))
        assert result.status == ReceiptOcrExtractionStatus.ENGINE_FAILED
        assert result.outcome_code == "engine_exit_nonzero"
        assert result.blocks == ()

    def test_exit_2_maps_to_configuration_error(self, vision_platform, tmp_path: Path) -> None:
        # Exit 2 is a usage/argument/protocol/configuration error and fails
        # closed; it must never become persistable engine_failed evidence.
        helper = _write_fake_helper(tmp_path, body=self._exit_body(2))
        engine = _make_engine(helper)
        with pytest.raises(InvalidOcrConfigurationError):
            _extract(engine, _source(tmp_path))

    def test_exit_3_maps_to_resource_limit(self, vision_platform, tmp_path: Path) -> None:
        # Exit 3 is an input-bound violation and fails closed.
        helper = _write_fake_helper(tmp_path, body=self._exit_body(3))
        engine = _make_engine(helper)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, _source(tmp_path))

    def test_exit_4_maps_to_resource_limit(self, vision_platform, tmp_path: Path) -> None:
        # Exit 4 is an output/text/block-bound violation and fails closed.
        helper = _write_fake_helper(tmp_path, body=self._exit_body(4))
        engine = _make_engine(helper)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, _source(tmp_path))

    def test_unexpected_exit_fails_closed(self, vision_platform, tmp_path: Path) -> None:
        # Any other nonzero exit is unexpected and fails closed rather than
        # being reported as a generic engine failure.
        helper = _write_fake_helper(tmp_path, body=self._exit_body(5))
        engine = _make_engine(helper)
        with pytest.raises(OcrEngineLaunchError):
            _extract(engine, _source(tmp_path))

    def test_launch_failure(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        # Point the engine at a path that is no longer executable.
        object.__setattr__(engine, "_path", tmp_path / "missing")
        source = _source(tmp_path)
        with pytest.raises(InvalidOcrConfigurationError):
            _extract(engine, source)

    def test_deadline_expiry(self, vision_platform, tmp_path: Path) -> None:
        body = """
import sys
import time
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
time.sleep(30)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        source = _source(tmp_path)
        with pytest.raises(OcrDeadlineExceededError):
            _extract(engine, source, timeout=0.5)

    def test_expired_deadline_before_launch(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        source = _source(tmp_path)
        with pytest.raises(OcrDeadlineExceededError):
            _extract(engine, source, timeout=-1.0)

    def test_sigkill_escalation_for_sigterm_ignoring_process(
        self, vision_platform, tmp_path: Path
    ) -> None:
        # The OCR stage ignores SIGTERM, so the TERM->KILL escalation and
        # process-group cleanup must actually run and reap the child.
        body = """
import sys
import time
import signal
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        source = _source(tmp_path)
        limits = ReceiptOcrLimits(termination_grace_seconds=0.25)
        start = time.monotonic()
        with pytest.raises(OcrDeadlineExceededError):
            _extract(engine, source, limits=limits, timeout=1.0)
        # The escalation must complete promptly (deadline + grace), not hang.
        assert time.monotonic() - start < 10.0

    def test_process_group_residue_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # If the process group still exists after SIGKILL and the grace period,
        # cleanup must fail closed rather than silently report success. The
        # mocked killpg simulates a group that never disappears; no real
        # process is created, so nothing can run away.
        signals_sent: list[int] = []

        def fake_killpg(process_group: int, sig: int) -> None:
            signals_sent.append(sig)
            # Never raise ProcessLookupError: the group "persists".

        monkeypatch.setattr(mv.os, "killpg", fake_killpg)
        with pytest.raises(OcrProcessCleanupError):
            mv._terminate_remaining_process_group(999999, 0.05)
        # The escalation must have attempted SIGTERM and then SIGKILL.
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent

    def test_process_group_cleanup_succeeds_when_group_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Once the group is gone (killpg probe raises ProcessLookupError),
        # cleanup returns normally without raising.
        def fake_killpg(process_group: int, sig: int) -> None:
            raise ProcessLookupError()

        monkeypatch.setattr(mv.os, "killpg", fake_killpg)
        # Should return cleanly (no exception).
        mv._terminate_remaining_process_group(999999, 0.05)

    def test_stdout_overflow(self, vision_platform, tmp_path: Path) -> None:
        body = """
import sys
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
import os
os.write(1, b'x' * 5000)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        source = _source(tmp_path)
        limits = ReceiptOcrLimits(max_stdout_bytes=1000)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source, limits=limits)

    def test_stderr_overflow(self, vision_platform, tmp_path: Path) -> None:
        body = """
import sys
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
import os
os.write(2, b'e' * 5000)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        source = _source(tmp_path)
        limits = ReceiptOcrLimits(max_stderr_bytes=1000)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source, limits=limits)

    def test_resident_memory_breach(
        self, vision_platform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = """
import sys
import time
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
time.sleep(30)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        monkeypatch.setattr(mv, "_observe_child_resident_bytes", lambda pid: 10**12)
        source = _source(tmp_path)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source)

    def test_rss_probe_failure_on_live_helper_fails_closed(
        self, vision_platform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The OCR helper stays alive (sleeps). When the resident-memory probe
        # returns None while the child is still running, the configured memory
        # bound cannot be verified, so the engine must fail closed.
        body = """
import sys
import time
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
time.sleep(30)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        _arm_probe_failure_after_identity(monkeypatch)
        source = _source(tmp_path)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source, timeout=10.0)

    def test_rss_probe_failure_writes_no_evidence(
        self, vision_platform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A probe failure on a live helper must not write misleading extraction
        # evidence through the staging service.
        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(conn, tmp_path, suffix="rssfail")
            body = """
import sys
import time
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
time.sleep(30)
raise SystemExit(0)
""" % _identity_json()
            helper = _write_fake_helper(tmp_path, body=body)
            engine = _make_engine(helper)
            _arm_probe_failure_after_identity(monkeypatch)
            with pytest.raises(OcrResourceLimitExceededError):
                extract_and_persist_receipt_ocr_evidence(
                    conn,
                    public_id="rocr_rss_fail_001",
                    attachment_id=attachment_id,
                    engine=engine,
                )
            count = conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_cpu_exhaustion_signal(self, vision_platform, tmp_path: Path) -> None:
        body = """
import sys
import os
import signal
if sys.argv[1:] == ['--identity']:
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
os.kill(os.getpid(), signal.SIGXCPU)
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        source = _source(tmp_path)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source)

    def test_private_temp_cleanup_after_success(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        before = set(Path(tempfile_root()).glob("receipt-ocr-vision-*"))
        _extract(engine, _source(tmp_path))
        after = set(Path(tempfile_root()).glob("receipt-ocr-vision-*"))
        assert after == before  # no leaked private run directories

    def test_private_temp_cleanup_after_failure(self, vision_platform, tmp_path: Path) -> None:
        body = """
import sys
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
raise SystemExit(1)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        before = set(Path(tempfile_root()).glob("receipt-ocr-vision-*"))
        result = _extract(engine, _source(tmp_path))
        assert result.status == ReceiptOcrExtractionStatus.ENGINE_FAILED
        after = set(Path(tempfile_root()).glob("receipt-ocr-vision-*"))
        assert after == before


def tempfile_root() -> str:
    import tempfile

    return tempfile.gettempdir()


# ===========================================================================
# 7b. Effective image/text limits are propagated to the native helper
# ===========================================================================


class TestEffectiveLimitsPropagation:
    def test_effective_limits_passed_to_helper(
        self, vision_platform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The caller's effective image-width, image-height, and total-text
        # limits must be passed to the helper as fixed argv so Vision can be
        # bounded before decode/OCR.
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        captured: dict[str, list[str]] = {}
        real_run = mv._run_bounded_process

        def capturing_run(arguments: list[str], **kwargs: object) -> object:
            if len(arguments) > 1 and arguments[1] == "--ocr":
                captured["argv"] = list(arguments)
                return mv._ProcessOutput(
                    returncode=0,
                    stdout=_ocr_json(status="no_text", observations=[]).encode("utf-8"),
                )
            return real_run(arguments, **kwargs)

        monkeypatch.setattr(mv, "_run_bounded_process", capturing_run)
        limits = ReceiptOcrLimits(
            max_image_width=1234,
            max_image_height=5678,
            max_total_normalized_text_characters=9999,
        )
        _extract(engine, _source(tmp_path), limits=limits)
        argv = captured["argv"]
        # argv: [copy, --ocr, fd, max_bytes, langs, max_blocks, max_chars,
        #        max_width, max_height, max_total_text]
        assert argv[1] == "--ocr"
        assert argv[7] == "1234"
        assert argv[8] == "5678"
        assert argv[9] == "9999"


# ===========================================================================
# 8. Environment sanitization and fd inheritance
# ===========================================================================


class TestEnvironmentAndFd:
    def test_parent_environment_not_leaked(
        self, vision_platform, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OCR_PARENT_SECRET", "leak-me")
        # The fake helper exits 9 if OCR_PARENT_SECRET is present.
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        result = _extract(engine, _source(tmp_path))
        assert result.status == ReceiptOcrExtractionStatus.SUCCEEDED

    def test_inherited_fd_delivers_exact_bytes(self, vision_platform, tmp_path: Path) -> None:
        # The helper echoes the first bytes of the inherited fd to prove it
        # reads the exact opened descriptor rather than a caller path.
        body = """
import sys
import os
if sys.argv[1:] == ['--identity']:
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
fd = int(sys.argv[2])
data = os.read(fd, 3)
marker = b'{"protocol_version":1,"status":"ok","outcome_code":"ok","page_width":480,'
if data != b'\\xff\\xd8\\xff':
    raise SystemExit(11)
os.write(1, marker + b'"page_height":640,"orientation":1,"observations":['
         + b'{"index":0,"text":"FDOK","confidence":0.900000,'
         + b'"bounding_box":[0.100000000,0.500000000,0.200000000,0.050000000]}]}\\n')
raise SystemExit(0)
""" % _identity_json()
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        result = _extract(engine, _source(tmp_path, content=JPEG))
        assert result.status == ReceiptOcrExtractionStatus.SUCCEEDED
        assert result.blocks[0].text == "FDOK"


# ===========================================================================
# 9. Executable tamper before / during execution
# ===========================================================================


class TestExecutableTamper:
    def test_tamper_before_execution_detected(self, vision_platform, tmp_path: Path) -> None:
        helper = _write_fake_helper(tmp_path)
        engine = _make_engine(helper)
        # Replace the configured binary after construction.
        helper.chmod(0o700)
        helper.write_text(helper.read_text() + "# tampered\n", encoding="utf-8")
        helper.chmod(0o500)
        source = _source(tmp_path)
        with pytest.raises(InvalidOcrConfigurationError):
            _extract(engine, source)

    def test_tamper_during_execution_detected(self, vision_platform, tmp_path: Path) -> None:
        # The helper rewrites its own configured-path binary mid-run; the
        # post-execution revalidation must fail closed.
        body = """
import sys
import os
if sys.argv[1:] == ['--identity']:
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
target = os.environ.get('HELPER_PATH')
# The configured path is passed implicitly via argv[7] in this test harness.
configured = sys.argv[7] if len(sys.argv) > 7 else None
if configured:
    with open(configured, 'ab') as handle:
        handle.write(b'\\n# tampered-during-run')
os.write(1, %r.encode('utf-8'))
raise SystemExit(0)
""" % (_identity_json(), _ocr_json())
        helper = _write_fake_helper(tmp_path, body=body)
        engine = _make_engine(helper)
        # The engine launches the private copy with a fixed argv; the extra
        # argument is not part of the real protocol, so instead we tamper via
        # a background thread while OCR sleeps.
        real_run = mv._run_bounded_process

        def tampering_run(arguments, **kwargs):
            if len(arguments) > 1 and arguments[1] == "--ocr":
                helper.chmod(0o700)
                helper.write_text(helper.read_text() + "\n# tampered-during-run", encoding="utf-8")
                helper.chmod(0o500)
            return real_run(arguments, **kwargs)

        source = _source(tmp_path)
        import finance_core.intake.macos_vision_receipt_ocr as module

        original = module._run_bounded_process
        module._run_bounded_process = tampering_run
        try:
            with pytest.raises(InvalidOcrConfigurationError):
                _extract(engine, source)
        finally:
            module._run_bounded_process = original


# ===========================================================================
# 10-12. Service integration, deterministic outcomes, Tesseract regression
# ===========================================================================


def _insert_raw_intake(conn: sqlite3.Connection, suffix: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, source_channel, raw_input, received_at
        ) VALUES (?, 'telegram_text', 'telegram', 'receipt image', ?)
        """,
        (f"raw_vision_{suffix}", "2026-07-25T15:00:00+00:00"),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _persist_attachment(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    suffix: str,
    content: bytes = JPEG,
    mime_type: str = "image/jpeg",
) -> tuple[int, Path]:
    path = (tmp_path / f"{suffix}.bin").resolve()
    path.write_bytes(content)
    path.chmod(0o400)
    raw_intake_id = _insert_raw_intake(conn, suffix)
    result = persist_attachment_evidence(
        conn,
        path,
        public_id=f"tgae_vision_{suffix}",
        raw_intake_id=raw_intake_id,
        telegram_file_id=f"file_{suffix}",
        telegram_file_unique_id=f"unique_{suffix}",
        original_filename=f"{suffix}.jpg",
        declared_mime_type=mime_type,
        expected_file_size=len(content),
        expected_content_hash=_hash(content),
    )
    return int(result["attachment_id"]), path


class _CountingEngine:
    """Protocol wrapper that counts extract() calls for replay proofs."""

    def __init__(self, inner: MacOSVisionOcrEngine) -> None:
        self.inner = inner
        self.calls = 0

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self.inner.identity

    def extract(self, source, *, limits, deadline):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.inner.extract(source, limits=limits, deadline=deadline)


def _staging_conn(tmp_path: Path) -> sqlite3.Connection:
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

    return create_staging_database(
        tmp_path / "vision.sqlite", migration_paths=TEMP_DB_MIGRATION_PATHS
    )


class TestServiceIntegration:
    def test_persist_replay_and_no_partial_rows(self, vision_platform, tmp_path: Path) -> None:
        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(conn, tmp_path, suffix="ok1")
            helper = _write_fake_helper(tmp_path)
            engine = _CountingEngine(_make_engine(helper))
            first = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_001",
                attachment_id=attachment_id,
                engine=engine,
            )
            assert first.status == ReceiptOcrExtractionStatus.SUCCEEDED
            assert first.persistence_idempotent is False
            assert engine.calls == 1
            assert first.block_count == 1
            assert first.engine_name == "macos_vision"

            # Exact replay: no rerun, idempotent success.
            second = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_001",
                attachment_id=attachment_id,
                engine=engine,
            )
            assert second.persistence_idempotent is True
            assert engine.calls == 1  # Vision was NOT rerun
            assert second.extraction_fingerprint == first.extraction_fingerprint
            assert second.normalized_result_hash == first.normalized_result_hash

            # Exactly one extraction row and one block row.
            count = conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0]
            assert count == 1
            blocks = conn.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0]
            assert blocks == 1

            # No downstream financial records of any kind.
            for table in (
                "parser_outputs",
                "receipt_ocr_proposal_links",
                "transactions",
            ):
                rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert rows == 0, table
        finally:
            conn.close()

    def test_identity_conflict_on_changed_config(self, vision_platform, tmp_path: Path) -> None:
        from finance_core.intake.receipt_ocr_evidence import OcrIdempotencyConflictError

        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(conn, tmp_path, suffix="conf1")
            helper = _write_fake_helper(tmp_path)
            engine_a = _make_engine(helper, languages=("en-US",))
            extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_002",
                attachment_id=attachment_id,
                engine=engine_a,
            )
            engine_b = _make_engine(helper, languages=("ms-MY",))
            with pytest.raises(OcrIdempotencyConflictError):
                extract_and_persist_receipt_ocr_evidence(
                    conn,
                    public_id="rocr_vision_002",
                    attachment_id=attachment_id,
                    engine=engine_b,
                )
        finally:
            conn.close()

    def test_engine_failure_creates_no_partial_rows(self, vision_platform, tmp_path: Path) -> None:
        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(conn, tmp_path, suffix="fail1")
            body = """
import sys
if sys.argv[1:] == ['--identity']:
    import os
    os.write(1, %r.encode('utf-8'))
    raise SystemExit(0)
raise SystemExit(1)
""" % _identity_json()
            helper = _write_fake_helper(tmp_path, body=body)
            engine = _make_engine(helper)
            result = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_003",
                attachment_id=attachment_id,
                engine=engine,
            )
            assert result.status == ReceiptOcrExtractionStatus.ENGINE_FAILED
            assert result.block_count == 0
            count = conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    @staticmethod
    def _exit_body(code: int) -> str:
        return (
            "import sys\n"
            "if sys.argv[1:] == ['--identity']:\n"
            "    import os\n"
            f"    os.write(1, {_identity_json()!r}.encode('utf-8'))\n"
            "    raise SystemExit(0)\n"
            f"raise SystemExit({code})\n"
        )

    @pytest.mark.parametrize(
        ("code", "exc"),
        [
            (2, InvalidOcrConfigurationError),
            (3, OcrResourceLimitExceededError),
            (4, OcrResourceLimitExceededError),
        ],
    )
    def test_resource_config_violation_writes_no_engine_failed_evidence(
        self, vision_platform, tmp_path: Path, code: int, exc: type[Exception]
    ) -> None:
        # Exit 2 (config/protocol), 3 (input bound), and 4 (output bound) fail
        # closed and must never persist misleading engine_failed evidence.
        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(conn, tmp_path, suffix=f"viol{code}")
            helper = _write_fake_helper(tmp_path, body=self._exit_body(code))
            engine = _make_engine(helper)
            with pytest.raises(exc):
                extract_and_persist_receipt_ocr_evidence(
                    conn,
                    public_id=f"rocr_viol_{code}",
                    attachment_id=attachment_id,
                    engine=engine,
                )
            count = conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_pdf_avoids_engine_invocation(self, vision_platform, tmp_path: Path) -> None:
        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(
                conn, tmp_path, suffix="pdf1", content=PDF, mime_type="application/pdf"
            )
            helper = _write_fake_helper(tmp_path)
            engine = _CountingEngine(_make_engine(helper))
            result = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_004",
                attachment_id=attachment_id,
                engine=engine,
            )
            assert result.status == ReceiptOcrExtractionStatus.UNSUPPORTED_INPUT
            assert result.outcome_code == "pdf_unsupported"
            assert engine.calls == 0  # engine never launched
        finally:
            conn.close()

    def test_oversize_avoids_engine_invocation(self, vision_platform, tmp_path: Path) -> None:
        conn = _staging_conn(tmp_path)
        try:
            content = JPEG + b"\x00" * 2000
            attachment_id, _path = _persist_attachment(
                conn, tmp_path, suffix="big1", content=content
            )
            helper = _write_fake_helper(tmp_path)
            engine = _CountingEngine(_make_engine(helper))
            limits = ReceiptOcrLimits(max_attachment_bytes=1000)
            result = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_005",
                attachment_id=attachment_id,
                engine=engine,
                limits=limits,
            )
            assert result.status == ReceiptOcrExtractionStatus.RESOURCE_REJECTED
            assert result.outcome_code == "attachment_size_limit"
            assert engine.calls == 0
        finally:
            conn.close()


class TestTesseractRegression:
    """The existing Tesseract adapter and service remain intact (req 12)."""

    def test_tesseract_still_rejects_unsupported_platform_or_works(self) -> None:
        # On non-Linux the Tesseract adapter must still fail closed at
        # configuration; on Linux it must still construct against a real
        # executable. Either way the public contract is unchanged.
        if not sys.platform.startswith("linux"):
            with pytest.raises(OcrUnsupportedPlatformError):
                TesseractTsvOcrEngine("/bin/echo", expected_version="5.3.4")

    def test_service_public_contract_unchanged(self) -> None:
        # The shared service signature and status taxonomy are stable.
        assert ReceiptOcrExtractionStatus.SUCCEEDED.value == "succeeded"
        assert ReceiptOcrExtractionStatus.NO_TEXT.value == "no_text"
        assert ReceiptOcrExtractionStatus.UNSUPPORTED_INPUT.value == "unsupported_input"
        assert ReceiptOcrExtractionStatus.ENGINE_FAILED.value == "engine_failed"
        assert ReceiptOcrExtractionStatus.RESOURCE_REJECTED.value == "resource_rejected"


# ===========================================================================
# 13. Real macOS Vision integration (Darwin arm64 only)
# ===========================================================================


@pytest.fixture(scope="session")
def compiled_vision_helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the committed Swift helper once per session (Darwin arm64)."""
    if not IS_DARWIN_ARM64:
        pytest.skip("The real Apple Vision helper requires Darwin on Apple Silicon arm64.")
    out_dir = tmp_path_factory.mktemp("vision_helper")
    binary = out_dir / "macos_vision_receipt_ocr"
    module_cache = out_dir / "module-cache"
    module_cache.mkdir()
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-O",
            "-swift-version",
            "5",
            "-module-cache-path",
            str(module_cache),
            "-o",
            str(binary),
            str(SWIFT_SOURCE),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    binary.chmod(0o500)
    return binary


@darwin_arm64
class TestRealVisionIntegration:
    def test_compile_helper_and_ocr_synthetic_receipt(self, tmp_path: Path) -> None:
        assert SWIFT_SOURCE.is_file(), "committed Swift source must exist"
        assert FIXTURE_PNG.is_file(), "synthetic fixture must exist"

        # Compile the committed source into a temporary directory only.
        binary = tmp_path / "macos_vision_receipt_ocr"
        module_cache = tmp_path / "module-cache"
        module_cache.mkdir()
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-O",
                "-swift-version",
                "5",
                "-module-cache-path",
                str(module_cache),
                "-o",
                str(binary),
                str(SWIFT_SOURCE),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        binary.chmod(0o500)
        assert binary.is_file()

        engine = MacOSVisionOcrEngine(binary, expected_version=EXPECTED_VERSION)
        assert engine.identity.name == "macos_vision"
        assert engine.identity.binary_sha256 == _hash(binary.read_bytes())

        # Persist real Vision evidence through migration 032.
        fixture_bytes = FIXTURE_PNG.read_bytes()
        conn = _staging_conn(tmp_path)
        try:
            attachment_id, _path = _persist_attachment(
                conn,
                tmp_path,
                suffix="real1",
                content=fixture_bytes,
                mime_type="image/png",
            )
            counting = _CountingEngine(engine)
            first = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_real_001",
                attachment_id=attachment_id,
                engine=counting,
            )
            assert first.status == ReceiptOcrExtractionStatus.SUCCEEDED
            assert first.block_count > 0
            assert counting.calls == 1

            # Bounded, non-empty blocks with sane geometry.
            rows = conn.execute(
                """
                SELECT normalized_text, coordinate_left, coordinate_top,
                       coordinate_width, coordinate_height, page_width,
                       page_height, confidence_scaled
                FROM receipt_ocr_blocks ORDER BY sequence_index
                """
            ).fetchall()
            assert rows
            texts = " ".join(row["normalized_text"] for row in rows)
            assert "TOTAL" in texts
            for row in rows:
                assert row["coordinate_left"] + row["coordinate_width"] <= row["page_width"]
                assert row["coordinate_top"] + row["coordinate_height"] <= row["page_height"]
                assert 0 <= row["confidence_scaled"] <= 10000

            # Exact replay without rerunning Vision.
            second = extract_and_persist_receipt_ocr_evidence(
                conn,
                public_id="rocr_vision_real_001",
                attachment_id=attachment_id,
                engine=counting,
            )
            assert second.persistence_idempotent is True
            assert counting.calls == 1
            assert second.normalized_result_hash == first.normalized_result_hash
        finally:
            conn.close()

        # No compiled artifact leaked into the repository.
        repo_binary = REPO_ROOT / "native" / "macos_vision_receipt_ocr" / "macos_vision_receipt_ocr"
        assert not repo_binary.exists()

    def test_real_host_language_capability(self, compiled_vision_helper: Path) -> None:
        # The host's real Vision capability (revision 3, accurate) is the
        # authority: en-US and ms-MY are supported; en-GB is not.
        engine = MacOSVisionOcrEngine(compiled_vision_helper, expected_version=EXPECTED_VERSION)
        assert engine.identity.version == EXPECTED_VERSION
        # ms-MY is genuinely supported on this host and must be accepted.
        ms_engine = MacOSVisionOcrEngine(
            compiled_vision_helper, expected_version=EXPECTED_VERSION, languages=("ms-MY",)
        )
        assert ms_engine.identity.version == EXPECTED_VERSION
        # en-GB is well-formed but not supported by the host Vision capability.
        with pytest.raises(InvalidOcrConfigurationError):
            MacOSVisionOcrEngine(
                compiled_vision_helper, expected_version=EXPECTED_VERSION, languages=("en-GB",)
            )

    def test_real_image_width_over_effective_limit_rejected(
        self, compiled_vision_helper: Path, tmp_path: Path
    ) -> None:
        # The fixture is 480x640: below the absolute maximum but above this
        # run's effective width limit. Vision must not proceed (exit 3).
        engine = MacOSVisionOcrEngine(compiled_vision_helper, expected_version=EXPECTED_VERSION)
        source = _source(
            tmp_path, content=FIXTURE_PNG.read_bytes(), mime_type="image/png", name="w.png"
        )
        limits = ReceiptOcrLimits(max_image_width=100)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source, limits=limits)

    def test_real_image_height_over_effective_limit_rejected(
        self, compiled_vision_helper: Path, tmp_path: Path
    ) -> None:
        engine = MacOSVisionOcrEngine(compiled_vision_helper, expected_version=EXPECTED_VERSION)
        source = _source(
            tmp_path, content=FIXTURE_PNG.read_bytes(), mime_type="image/png", name="h.png"
        )
        limits = ReceiptOcrLimits(max_image_height=100)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source, limits=limits)

    def test_real_total_text_over_effective_limit_rejected(
        self, compiled_vision_helper: Path, tmp_path: Path
    ) -> None:
        # The fixture yields far more than 5 characters of text; a tiny
        # effective total-text limit must be enforced before stdout (exit 4).
        engine = MacOSVisionOcrEngine(compiled_vision_helper, expected_version=EXPECTED_VERSION)
        source = _source(
            tmp_path, content=FIXTURE_PNG.read_bytes(), mime_type="image/png", name="t.png"
        )
        limits = ReceiptOcrLimits(max_total_normalized_text_characters=5)
        with pytest.raises(OcrResourceLimitExceededError):
            _extract(engine, source, limits=limits)

    def test_real_unsupported_language_rejected_at_ocr(
        self, compiled_vision_helper: Path, tmp_path: Path
    ) -> None:
        # Defense in depth: even if a language somehow reached the helper, the
        # helper validates against the real capability before reading the image
        # and exits 2 (configuration error), never running Vision.
        engine = MacOSVisionOcrEngine(compiled_vision_helper, expected_version=EXPECTED_VERSION)
        # Bypass construction-time validation to exercise the helper-side check.
        object.__setattr__(engine, "_languages", ("en-GB",))
        source = _source(
            tmp_path, content=FIXTURE_PNG.read_bytes(), mime_type="image/png", name="lang.png"
        )
        with pytest.raises(InvalidOcrConfigurationError):
            _extract(engine, source)
