"""OCR engine boundary for the bridge receipt propose path.

The CLI never executes a caller-chosen executable: the v1 request envelope
carries no OCR helper path or version.  The production engine is resolved
exclusively from the trusted workspace runtime boundary
(``runtime/ocr_engine.json``), a file owned by the separately authorized S6
deployment stage and never supplied by the request envelope.  The resolver
validates the configured absolute helper path and its file safety
attributes, then defers to the existing ``MacOSVisionOcrEngine``
identity/version/hash protocol (bounded ``--identity`` query, binary
SHA-256, configuration hash, tamper re-verification).  Until a valid
trusted configuration exists, the boundary fails closed with
``OCR_ENGINE_UNAVAILABLE``.  Tests replace the module-level factory with a
deterministic engine seam; this seam is the only engine injection point and
never touches credentials or network.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable

from finance_core.intake.receipt_ocr_evidence import ReceiptOcrEngine
from finance_core.openclaw_staging_bridge import errors

OCR_ENGINE_CONFIG_FILENAME = "ocr_engine.json"
OCR_ENGINE_CONFIG_SCHEMA_VERSION = "v1"
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
        fd = os.open(str(config_path), os.O_RDONLY | os.O_NOFOLLOW)
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
        raw = os.read(fd, _MAX_CONFIG_BYTES)
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _unavailable("OCR engine configuration is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise _unavailable("OCR engine configuration must be a JSON object.")
    return payload


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
    config = _validated_engine_config(_load_workspace_engine_config(workspace))
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
    "build_ocr_engine",
    "engine_factory",
    "resolve_workspace_ocr_engine",
]
