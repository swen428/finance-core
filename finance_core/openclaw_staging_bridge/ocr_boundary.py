"""Trusted workspace OCR resolution for the staging receipt proposal path.

Requests cannot choose executables, models, arguments or environments. The
trusted runtime/ocr_engine.json v1 contract retains macOS Vision resolution;
v2 explicitly selects the pinned Linux Tesseract adapter. Missing, malformed
or mismatched identities fail closed. The module-level factory remains the
existing deterministic test seam; production resolution performs no download.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrEngine,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrLimits,
    ReceiptOcrSource,
)
from finance_core.openclaw_staging_bridge import errors
from finance_core.parser_proposals.receipt_total_parser import (
    PARSER_CONTRACT_VERSION_DEFAULT,
    PARSER_CONTRACT_VERSION_TSV_HIERARCHY,
    PARSER_VERSION,
    PARSER_VERSION_TSV_HIERARCHY,
)

OCR_ENGINE_CONFIG_FILENAME = "ocr_engine.json"
OCR_ENGINE_CONFIG_SCHEMA_VERSION = "v1"
LINUX_OCR_ENGINE_CONFIG_SCHEMA_VERSION = "v2"
_MAX_CONFIG_BYTES = 4_096
_MAX_HELPER_PATH_LENGTH = 1_024
_MAX_LANGUAGE_COUNT = 8
_MAX_LANGUAGE_LENGTH = 16
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _unavailable(message: str) -> errors.BridgeError:
    return errors.bridge_error(
        errors.OCR_ENGINE_UNAVAILABLE,
        message,
        errors.EXIT_VALIDATION_REFUSED,
        retryable=False,
    )


def _load_workspace_engine_config(workspace: Path) -> dict[str, Any]:
    """Read the trusted runtime OCR configuration without following symlinks."""
    config_path = workspace / "runtime" / OCR_ENGINE_CONFIG_FILENAME
    try:
        fd = os.open(str(config_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise _unavailable(
            "No trusted OCR engine configuration is wired into the workspace "
            "runtime; production helper wiring belongs to a separately "
            "authorized deployment stage and never comes from the request "
            "envelope."
        ) from None
    except OSError as exc:
        raise _unavailable(f"OCR engine configuration cannot be opened safely: {exc}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _unavailable("OCR engine configuration must be a regular file.")
        if st.st_size == 0 or st.st_size > _MAX_CONFIG_BYTES:
            raise _unavailable("OCR engine configuration has an unsafe size.")
        chunks = bytearray()
        while len(chunks) <= _MAX_CONFIG_BYTES:
            chunk = os.read(fd, _MAX_CONFIG_BYTES + 1 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        raw = bytes(chunks)
        after = os.fstat(fd)
        current = os.lstat(config_path)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")

        def identity(value: os.stat_result) -> tuple[int, ...]:
            return tuple(int(getattr(value, field)) for field in fields)

        stable = identity(st) == identity(after) == identity(current) and len(raw) == st.st_size
    except OSError as exc:
        raise _unavailable(
            "OCR configuration changed or became inaccessible during reading."
        ) from exc
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _unavailable("OCR engine configuration is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise _unavailable("OCR engine configuration must be a JSON object.")
    if payload.get("schema_version") == LINUX_OCR_ENGINE_CONFIG_SCHEMA_VERSION:
        if not stable or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise _unavailable(
                "Linux OCR configuration must be stable and service-owned with safe permissions."
            )
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate OCR configuration key.")
        result[key] = value
    return result


@dataclass(frozen=True)
class _PinnedLinuxReceiptEngine:
    """Capability created only from this resolver's verified pinned configuration.

    Keep the selected parser bound to the same engine used for extraction. No
    independent configuration read, platform/name inference or request switch.
    """

    engine: ReceiptOcrEngine

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self.engine.identity

    def extract(
        self, source: ReceiptOcrSource, *, limits: ReceiptOcrLimits, deadline: float
    ) -> ReceiptOcrEngineResult:
        return self.engine.extract(source, limits=limits, deadline=deadline)


def receipt_parser_identity(engine: ReceiptOcrEngine) -> tuple[str, str]:
    """Select v2 only for the capability built by the trusted Linux resolver."""
    if type(engine) is _PinnedLinuxReceiptEngine:
        return PARSER_VERSION_TSV_HIERARCHY, PARSER_CONTRACT_VERSION_TSV_HIERARCHY
    return PARSER_VERSION, PARSER_CONTRACT_VERSION_DEFAULT


def _resolve_linux_engine(payload: dict[str, Any]) -> ReceiptOcrEngine:
    from finance_core.intake.receipt_ocr_evidence import TesseractTsvOcrEngine
    from finance_core.intake.tesseract_resources import (
        PinnedTesseractResources,
        TesseractLanguageResource,
    )

    expected_keys = {
        "schema_version",
        "engine",
        "helper_path",
        "expected_version",
        "binary_sha256",
        "tessdata_directory",
        "language_resources",
    }
    if set(payload) != expected_keys or payload.get("engine") != "tesseract_tsv":
        raise _unavailable("Linux OCR configuration must contain exactly its fixed v2 contract.")
    if not sys.platform.startswith("linux"):
        raise _unavailable("The v2 Tesseract resolver requires Linux.")
    for key in ("helper_path", "tessdata_directory"):
        value = payload[key]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_HELPER_PATH_LENGTH
            or not os.path.isabs(value)
        ):
            raise _unavailable("Linux OCR configuration paths must be bounded absolute paths.")
    if not isinstance(payload["expected_version"], str) or not _SAFE_VERSION_RE.fullmatch(
        payload["expected_version"]
    ):
        raise _unavailable("Linux OCR version is malformed.")
    if not isinstance(payload["binary_sha256"], str) or not _SHA256_HEX_RE.fullmatch(
        payload["binary_sha256"]
    ):
        raise _unavailable("Linux OCR requires a binary SHA-256.")
    resources = payload["language_resources"]
    if (
        not isinstance(resources, list)
        or len(resources) != 2
        or any(
            not isinstance(item, dict) or set(item) != {"language", "size_bytes", "sha256"}
            for item in resources
        )
    ):
        raise _unavailable("Linux OCR requires exactly two explicit resource identities.")
    try:
        descriptor = PinnedTesseractResources(
            Path(payload["tessdata_directory"]),
            tuple(TesseractLanguageResource(**item) for item in resources),
        )
        engine = TesseractTsvOcrEngine(
            payload["helper_path"],
            expected_version=payload["expected_version"],
            language="eng+chi_sim",
            pinned_resources=descriptor,
        )
        if engine.identity.binary_sha256 != payload["binary_sha256"]:
            raise ValueError("Binary hash mismatch.")
    except Exception as exc:
        raise _unavailable("Trusted Linux OCR resources failed identity verification.") from exc
    return _PinnedLinuxReceiptEngine(engine)


def _validated_engine_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the minimal v1 configuration contract fail-closed."""
    if payload.get("schema_version") != OCR_ENGINE_CONFIG_SCHEMA_VERSION:
        raise _unavailable("OCR engine configuration schema_version must be 'v1'.")
    helper_path = payload.get("helper_path")
    if not isinstance(helper_path, str) or not helper_path:
        raise _unavailable("OCR engine configuration must carry a non-empty helper_path.")
    if len(helper_path) > _MAX_HELPER_PATH_LENGTH:
        raise _unavailable("OCR engine configuration helper_path exceeds the bounded length.")
    if not os.path.isabs(helper_path):
        raise _unavailable("OCR engine configuration helper_path must be an absolute path.")
    expected_version = payload.get("expected_version")
    if not isinstance(expected_version, str) or not _SAFE_VERSION_RE.fullmatch(expected_version):
        raise _unavailable("OCR engine configuration expected_version is malformed.")

    resolved = Path(helper_path)
    try:
        st = os.lstat(str(resolved))
    except OSError:
        raise _unavailable("Configured OCR helper does not exist.") from None
    if stat.S_ISLNK(st.st_mode):
        raise _unavailable("Configured OCR helper must not be a symbolic link.")
    if not stat.S_ISREG(st.st_mode):
        raise _unavailable("Configured OCR helper must be a regular file.")
    if st.st_mode & 0o111 == 0:
        raise _unavailable("Configured OCR helper is not executable.")
    if st.st_mode & 0o022 != 0:
        raise _unavailable("Configured OCR helper is writable by group or other.")

    binary_sha256 = payload.get("binary_sha256")
    if binary_sha256 is not None and (
        not isinstance(binary_sha256, str) or not _SHA256_HEX_RE.fullmatch(binary_sha256)
    ):
        raise _unavailable("OCR engine configuration binary_sha256 is malformed.")

    languages_value = payload.get("languages", ["en-US"])
    if (
        not isinstance(languages_value, list)
        or not 1 <= len(languages_value) <= _MAX_LANGUAGE_COUNT
    ):
        raise _unavailable("OCR engine configuration languages must be a bounded non-empty list.")
    languages: list[str] = []
    for entry in languages_value:
        if not isinstance(entry, str) or not entry or len(entry) > _MAX_LANGUAGE_LENGTH:
            raise _unavailable("OCR engine configuration languages are malformed.")
        languages.append(entry)

    return {
        "helper_path": helper_path,
        "expected_version": expected_version,
        "binary_sha256": binary_sha256,
        "languages": tuple(languages),
    }


def resolve_workspace_ocr_engine(workspace: Path) -> ReceiptOcrEngine:
    """Resolve the trusted OCR engine from the workspace runtime boundary.

    The configured helper must satisfy the existing
    ``MacOSVisionOcrEngine`` identity contract: the bounded ``--identity``
    subprocess protocol, the expected version, the binary SHA-256, and the
    configuration hash.  Any absence or verification failure fails closed
    with ``OCR_ENGINE_UNAVAILABLE``; the request envelope can never supply
    or influence the executable.
    """
    payload = _load_workspace_engine_config(workspace)
    if payload.get("schema_version") == LINUX_OCR_ENGINE_CONFIG_SCHEMA_VERSION:
        return _resolve_linux_engine(payload)
    config = _validated_engine_config(payload)
    from finance_core.intake.macos_vision_receipt_ocr import MacOSVisionOcrEngine

    try:
        engine = MacOSVisionOcrEngine(
            config["helper_path"],
            expected_version=config["expected_version"],
            languages=config["languages"],
        )
    except Exception as exc:
        raise _unavailable(
            "Trusted OCR helper failed the identity/version verification contract: "
            f"{type(exc).__name__}."
        ) from exc
    pinned = config["binary_sha256"]
    if pinned is not None and engine.identity.binary_sha256 != pinned:
        raise _unavailable(
            "Trusted OCR helper binary hash does not match the pinned configuration identity."
        )
    return engine


# Test seam: replace with a deterministic ReceiptOcrEngine factory.  The
# production default resolves through the trusted workspace runtime boundary.
engine_factory: Callable[[Path], ReceiptOcrEngine] = resolve_workspace_ocr_engine


def build_ocr_engine(workspace: Path) -> ReceiptOcrEngine:
    return engine_factory(workspace)


__all__ = [
    "OCR_ENGINE_CONFIG_FILENAME",
    "OCR_ENGINE_CONFIG_SCHEMA_VERSION",
    "LINUX_OCR_ENGINE_CONFIG_SCHEMA_VERSION",
    "build_ocr_engine",
    "engine_factory",
    "resolve_workspace_ocr_engine",
]
