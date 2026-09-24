"""Bounded, staging-only receipt OCR evidence extraction and persistence.

OCR text is untrusted evidence. This module never creates parser proposals,
receipt facts, calculations, transactions, settlements, or final financial
state. The public service binds a bounded engine result to one already
persisted immutable attachment and writes append-only evidence in a
service-owned SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

try:
    import resource
except ImportError:  # pragma: no cover - exercised only on unsupported platforms
    resource = None  # type: ignore[assignment]

from finance_core.staging_guard import StagingDatabaseError, require_staging_database

_CONTRACT_VERSION = "receipt-ocr-evidence-v1"
_PUBLIC_ID_PREFIX = "rocr_"
ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES = 100_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_OUTCOME_RE = re.compile(r"^[a-z0-9_]+$")
_TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ReceiptOcrError(RuntimeError):
    """Stable base error for receipt OCR evidence operations."""


class InvalidOcrConfigurationError(ReceiptOcrError):
    """The caller, limits, engine identity, or executable configuration is invalid."""


class OcrStagingDatabaseRejectedError(ReceiptOcrError):
    """The supplied SQLite connection is not an authorised staging database."""


class OcrCallerOwnedTransactionError(ReceiptOcrError):
    """The caller supplied a connection with pending work."""


class OcrAttachmentNotFoundError(ReceiptOcrError):
    """The canonical attachment row or its required source evidence is missing."""


class OcrAttachmentIntegrityConflictError(ReceiptOcrError):
    """The canonical attachment metadata and current bytes do not agree."""


class OcrUnsupportedPlatformError(ReceiptOcrError):
    """Required POSIX process or resource controls are unavailable."""


class OcrEngineLaunchError(ReceiptOcrError):
    """The configured OCR executable could not be started safely."""


class OcrDeadlineExceededError(ReceiptOcrError):
    """The absolute OCR deadline expired and the process group was terminated."""


class OcrResourceLimitExceededError(ReceiptOcrError):
    """OCR input or process output exceeded an explicit resource limit."""


class MalformedOcrOutputError(ReceiptOcrError):
    """The OCR engine returned malformed, contradictory, or unbounded output."""


class OcrIdempotencyConflictError(ReceiptOcrError):
    """A public ID or extraction fingerprint is already bound differently."""


class OcrPersistenceConflictError(ReceiptOcrError):
    """Persisted append-only OCR evidence failed replay verification."""


class OcrUnexpectedPersistenceError(ReceiptOcrError):
    """An unexpected SQLite or transaction failure occurred."""


# ---------------------------------------------------------------------------
# Immutable public contracts
# ---------------------------------------------------------------------------


def _bounded_int(name: str, value: object, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidOcrConfigurationError(f"{name} must be an integer.")
    if value <= 0 or value > maximum:
        raise InvalidOcrConfigurationError(
            f"{name} must be greater than zero and no greater than {maximum}."
        )


def _bounded_number(name: str, value: object, *, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidOcrConfigurationError(f"{name} must be a finite number.")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0 or numeric > maximum:
        raise InvalidOcrConfigurationError(
            f"{name} must be finite, greater than zero, and no greater than {maximum:g}."
        )


@dataclass(frozen=True)
class ReceiptOcrLimits:
    """Conservative limits for one complete receipt OCR operation."""

    max_attachment_bytes: int = 20_000_000
    total_timeout_seconds: float = 30.0
    termination_grace_seconds: float = 0.25
    max_stdout_bytes: int = 8_000_000
    max_stderr_bytes: int = 65_536
    max_block_count: int = 10_000
    max_text_characters_per_block: int = 4_096
    max_total_normalized_text_characters: int = 500_000
    max_page_count: int = 10
    max_coordinate_value: int = 100_000
    max_image_width: int = 50_000
    max_image_height: int = 50_000
    max_confidence_value: int = 10_000
    max_engine_identity_length: int = 128
    max_public_id_length: int = 200
    cpu_time_seconds: int = 30
    address_space_bytes: int = 536_870_912
    output_file_bytes: int = 16_000_000
    process_count: int = 16
    open_file_count: int = 256

    def __post_init__(self) -> None:
        integer_limits = (
            (
                "max_attachment_bytes",
                self.max_attachment_bytes,
                ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES,
            ),
            ("max_stdout_bytes", self.max_stdout_bytes, 50_000_000),
            ("max_stderr_bytes", self.max_stderr_bytes, 1_000_000),
            ("max_block_count", self.max_block_count, 100_000),
            (
                "max_text_characters_per_block",
                self.max_text_characters_per_block,
                65_536,
            ),
            (
                "max_total_normalized_text_characters",
                self.max_total_normalized_text_characters,
                2_000_000,
            ),
            ("max_page_count", self.max_page_count, 100),
            ("max_coordinate_value", self.max_coordinate_value, 1_000_000),
            ("max_image_width", self.max_image_width, 100_000),
            ("max_image_height", self.max_image_height, 100_000),
            ("max_confidence_value", self.max_confidence_value, 10_000),
            ("max_engine_identity_length", self.max_engine_identity_length, 256),
            ("max_public_id_length", self.max_public_id_length, 200),
            ("cpu_time_seconds", self.cpu_time_seconds, 300),
            ("address_space_bytes", self.address_space_bytes, 4_294_967_296),
            ("output_file_bytes", self.output_file_bytes, 100_000_000),
            ("process_count", self.process_count, 128),
            ("open_file_count", self.open_file_count, 1_024),
        )
        for name, value, maximum in integer_limits:
            _bounded_int(name, value, maximum=maximum)
        _bounded_number("total_timeout_seconds", self.total_timeout_seconds, maximum=300.0)
        _bounded_number("termination_grace_seconds", self.termination_grace_seconds, maximum=5.0)
        if self.termination_grace_seconds >= self.total_timeout_seconds:
            raise InvalidOcrConfigurationError(
                "termination_grace_seconds must be smaller than total_timeout_seconds."
            )


class ReceiptOcrExtractionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    NO_TEXT = "no_text"
    UNSUPPORTED_INPUT = "unsupported_input"
    ENGINE_FAILED = "engine_failed"
    RESOURCE_REJECTED = "resource_rejected"


@dataclass(frozen=True)
class ReceiptOcrEngineIdentity:
    name: str
    version: str
    binary_sha256: str
    configuration_hash: str


@dataclass(frozen=True)
class ReceiptOcrSource:
    """Service-constructed identity for the exact already-opened attachment."""

    file_descriptor: int
    attachment_path: str
    size_bytes: int
    content_hash: str
    mime_type: str


@dataclass(frozen=True)
class ReceiptOcrBlock:
    sequence_index: int
    page_index: int
    engine_block_index: int | None
    engine_paragraph_index: int | None
    engine_line_index: int | None
    engine_word_index: int | None
    text: str
    left: int
    top: int
    width: int
    height: int
    page_width: int
    page_height: int
    confidence_scaled: int | None


@dataclass(frozen=True)
class ReceiptOcrEngineResult:
    status: ReceiptOcrExtractionStatus
    blocks: tuple[ReceiptOcrBlock, ...]
    outcome_code: str


@runtime_checkable
class ReceiptOcrEngine(Protocol):
    @property
    def identity(self) -> ReceiptOcrEngineIdentity: ...

    def extract(
        self,
        source: ReceiptOcrSource,
        *,
        limits: ReceiptOcrLimits,
        deadline: float,
    ) -> ReceiptOcrEngineResult: ...


@dataclass(frozen=True)
class ReceiptOcrExtractionResult:
    public_id: str
    attachment_id: int
    attachment_hash: str
    attachment_size: int
    source_mime_type: str
    status: ReceiptOcrExtractionStatus
    engine_name: str
    engine_version: str
    engine_binary_sha256: str
    engine_configuration_hash: str
    extraction_fingerprint: str
    block_count: int
    total_normalized_text_length: int
    normalized_result_hash: str
    outcome_code: str
    persistence_idempotent: bool


# ---------------------------------------------------------------------------
# Concrete bounded Tesseract TSV adapter
# ---------------------------------------------------------------------------


class TesseractTsvOcrEngine:
    """Explicit absolute-path Tesseract adapter with POSIX resource controls."""

    def __init__(
        self,
        executable_path: str | Path,
        *,
        expected_version: str,
        language: str = "eng",
    ) -> None:
        _require_supported_process_platform()
        if not _safe_identity(expected_version, maximum=128):
            raise InvalidOcrConfigurationError("expected_version is malformed.")
        if not _safe_identity(language, maximum=32):
            raise InvalidOcrConfigurationError("language is malformed.")
        executable = _open_verified_executable(executable_path)
        try:
            path = executable.path
            binary_hash = executable.binary_hash
            file_identity = executable.identity
        finally:
            executable.close()
        config_hash = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "adapter": "tesseract-tsv-v1",
                    "arguments": ["<input-fd>", "stdout", "-l", language, "--dpi", "300", "tsv"],
                    "language": language,
                }
            )
        ).hexdigest()
        self._path = path
        self._language = language
        self._file_identity = file_identity
        self._identity = ReceiptOcrEngineIdentity(
            name="tesseract_tsv",
            version=expected_version,
            binary_sha256=binary_hash,
            configuration_hash=config_hash,
        )

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self._identity

    def extract(
        self,
        source: ReceiptOcrSource,
        *,
        limits: ReceiptOcrLimits,
        deadline: float,
    ) -> ReceiptOcrEngineResult:
        _require_supported_process_platform()
        if (
            threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            raise OcrUnsupportedPlatformError(
                "The bounded subprocess adapter requires a single-threaded POSIX caller."
            )
        if source.mime_type not in {"image/jpeg", "image/png"}:
            raise InvalidOcrConfigurationError(
                "TesseractTsvOcrEngine accepts only validated JPEG or PNG input."
            )
        executable = _open_verified_executable(
            self._path,
            expected_identity=self._file_identity,
            expected_hash=self._identity.binary_sha256,
        )
        try:
            executable_path = f"/proc/self/fd/{executable.fd}"
            try:
                with tempfile.TemporaryDirectory(prefix="receipt-ocr-") as working_directory:
                    version_output = _run_bounded_process(
                        [executable_path, "--version"],
                        pass_fds=(executable.fd,),
                        limits=limits,
                        deadline=deadline,
                        working_directory=working_directory,
                    )
                    if version_output.returncode != 0:
                        raise InvalidOcrConfigurationError(
                            "The configured OCR executable did not provide its expected version."
                        )
                    observed_version = _parse_tesseract_version(version_output.stdout)
                    if observed_version != self._identity.version:
                        raise InvalidOcrConfigurationError(
                            "The configured OCR executable version does not match expected_version."
                        )
                    fd_path = f"/dev/fd/{source.file_descriptor}"
                    process_output = _run_bounded_process(
                        [
                            executable_path,
                            fd_path,
                            "stdout",
                            "-l",
                            self._language,
                            "--dpi",
                            "300",
                            "tsv",
                        ],
                        pass_fds=(executable.fd, source.file_descriptor),
                        limits=limits,
                        deadline=deadline,
                        working_directory=working_directory,
                    )
                if process_output.returncode in {
                    -signal.SIGXCPU,
                    -getattr(signal, "SIGXFSZ", signal.SIGXCPU),
                }:
                    raise OcrResourceLimitExceededError(
                        "The OCR process exceeded an operating-system resource limit."
                    )
                if process_output.returncode != 0:
                    result = ReceiptOcrEngineResult(
                        status=ReceiptOcrExtractionStatus.ENGINE_FAILED,
                        blocks=(),
                        outcome_code="engine_exit_nonzero",
                    )
                else:
                    blocks = _parse_tesseract_tsv(process_output.stdout, limits=limits)
                    if not blocks:
                        result = ReceiptOcrEngineResult(
                            status=ReceiptOcrExtractionStatus.NO_TEXT,
                            blocks=(),
                            outcome_code="no_text",
                        )
                    else:
                        result = ReceiptOcrEngineResult(
                            status=ReceiptOcrExtractionStatus.SUCCEEDED,
                            blocks=blocks,
                            outcome_code="ok",
                        )
            except BaseException:
                _reverify_opened_executable(
                    executable,
                    expected_identity=self._file_identity,
                    expected_hash=self._identity.binary_sha256,
                )
                raise
            _reverify_opened_executable(
                executable,
                expected_identity=self._file_identity,
                expected_hash=self._identity.binary_sha256,
            )
            return result
        finally:
            executable.close()


@dataclass(frozen=True)
class _ProcessOutput:
    returncode: int
    stdout: bytes


def _require_supported_process_platform() -> None:
    required_limits = ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_NOFILE")
    if (
        os.name != "posix"
        or not sys.platform.startswith("linux")
        or resource is None
        or not hasattr(os, "killpg")
    ):
        raise OcrUnsupportedPlatformError("Required POSIX OCR process controls are unavailable.")
    if any(not hasattr(resource, name) for name in required_limits):
        raise OcrUnsupportedPlatformError("Required POSIX resource limits are unavailable.")
    if not Path("/dev/fd").is_dir():
        raise OcrUnsupportedPlatformError("The exact opened-file descriptor path is unavailable.")
    if not Path("/proc/self/fd").is_dir():
        raise OcrUnsupportedPlatformError("The executable file-descriptor path is unavailable.")


@dataclass
class _VerifiedExecutable:
    path: Path
    fd: int
    identity: tuple[int, int, int, int, int, int]
    binary_hash: str

    def close(self) -> None:
        os.close(self.fd)


def _open_verified_executable(
    executable_path: str | Path,
    *,
    expected_identity: tuple[int, int, int, int, int, int] | None = None,
    expected_hash: str | None = None,
) -> _VerifiedExecutable:
    try:
        supplied = Path(executable_path)
    except TypeError as exc:
        raise InvalidOcrConfigurationError("OCR executable path is invalid.") from exc
    if not supplied.is_absolute():
        raise InvalidOcrConfigurationError("OCR executable path must be absolute.")
    try:
        entry = os.lstat(supplied)
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise InvalidOcrConfigurationError("OCR executable is missing or inaccessible.") from exc
    if supplied != resolved or stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise InvalidOcrConfigurationError(
            "OCR executable must be a direct, non-symlink regular file."
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(supplied, flags)
    except OSError as exc:
        raise InvalidOcrConfigurationError("OCR executable could not be opened safely.") from exc
    try:
        before = os.fstat(fd)
        _validate_executable_stat(before)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        current = os.lstat(supplied)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns")
        before_identity = tuple(int(getattr(before, field)) for field in fields)
        after_identity = tuple(int(getattr(after, field)) for field in fields)
        current_identity = tuple(int(getattr(current, field)) for field in fields)
        if before_identity != after_identity or after_identity != current_identity:
            raise InvalidOcrConfigurationError(
                "OCR executable identity changed while its hash was calculated."
            )
        binary_hash = digest.hexdigest()
        if (expected_identity is not None and before_identity != expected_identity) or (
            expected_hash is not None and binary_hash != expected_hash
        ):
            raise InvalidOcrConfigurationError(
                "The configured OCR executable identity changed after validation."
            )
        os.lseek(fd, 0, os.SEEK_SET)
        return _VerifiedExecutable(
            path=resolved,
            fd=fd,
            identity=before_identity,  # type: ignore[arg-type]
            binary_hash=binary_hash,
        )
    except InvalidOcrConfigurationError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise InvalidOcrConfigurationError(
            "OCR executable changed or became inaccessible during validation."
        ) from exc
    except BaseException:
        os.close(fd)
        raise


def _validate_executable_stat(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise InvalidOcrConfigurationError("OCR executable must be a regular file.")
    mode = stat.S_IMODE(value.st_mode)
    if not mode & 0o111:
        raise InvalidOcrConfigurationError("OCR executable is not executable.")
    if mode & 0o022:
        raise InvalidOcrConfigurationError(
            "OCR executable must not be writable by group or other users."
        )
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise InvalidOcrConfigurationError(
            "OCR executable must be owned by the current service user."
        )


def _reverify_opened_executable(
    executable: _VerifiedExecutable,
    *,
    expected_identity: tuple[int, int, int, int, int, int],
    expected_hash: str,
) -> None:
    try:
        before = os.fstat(executable.fd)
        _validate_executable_stat(before)
        os.lseek(executable.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(executable.fd, 65_536)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(executable.fd)
        current = os.lstat(executable.path)
    except (OSError, InvalidOcrConfigurationError) as exc:
        raise InvalidOcrConfigurationError(
            "The configured OCR executable could not be revalidated after execution."
        ) from exc
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns")
    before_identity = tuple(int(getattr(before, field)) for field in fields)
    after_identity = tuple(int(getattr(after, field)) for field in fields)
    current_identity = tuple(int(getattr(current, field)) for field in fields)
    if (
        before_identity != expected_identity
        or after_identity != expected_identity
        or current_identity != expected_identity
        or digest.hexdigest() != expected_hash
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
    ):
        raise InvalidOcrConfigurationError(
            "The configured OCR executable identity changed during execution."
        )
    os.lseek(executable.fd, 0, os.SEEK_SET)


def _resource_limiter(limits: ReceiptOcrLimits) -> Callable[[], None]:
    def apply_limits() -> None:
        if resource is None:  # pragma: no cover - guarded before process launch
            os._exit(127)
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_time_seconds, limits.cpu_time_seconds))
        resource.setrlimit(
            resource.RLIMIT_AS, (limits.address_space_bytes, limits.address_space_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_FSIZE, (limits.output_file_bytes, limits.output_file_bytes)
        )
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.process_count, limits.process_count))
        resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_file_count, limits.open_file_count))

    return apply_limits


def _run_bounded_process(
    arguments: Sequence[str],
    *,
    pass_fds: tuple[int, ...],
    limits: ReceiptOcrLimits,
    deadline: float,
    working_directory: str,
) -> _ProcessOutput:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OcrDeadlineExceededError("The OCR deadline expired before process launch.")
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_directory,
            env={"LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
            shell=False,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            preexec_fn=_resource_limiter(limits),
        )
    except (OSError, subprocess.SubprocessError):
        raise OcrEngineLaunchError("The OCR process could not be launched safely.") from None
    try:
        stdout, _stderr = _read_process_output(process, limits=limits, deadline=deadline)
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        _terminate_remaining_process_group(process.pid, limits.termination_grace_seconds)
        return _ProcessOutput(returncode=returncode, stdout=stdout)
    except subprocess.TimeoutExpired:
        _terminate_process(process, limits.termination_grace_seconds)
        raise OcrDeadlineExceededError(
            "The OCR deadline expired and the process group was terminated."
        ) from None
    except OcrDeadlineExceededError:
        _terminate_process(process, limits.termination_grace_seconds)
        raise
    except OcrResourceLimitExceededError:
        _terminate_process(process, limits.termination_grace_seconds)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _read_process_output(
    process: subprocess.Popen[bytes],
    *,
    limits: ReceiptOcrLimits,
    deadline: float,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise OcrEngineLaunchError("The OCR process output pipes were not created.")
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout.fileno(): ("stdout", bytearray(), limits.max_stdout_bytes),
        process.stderr.fileno(): ("stderr", bytearray(), limits.max_stderr_bytes),
    }
    for fd in streams:
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OcrDeadlineExceededError(
                    "The OCR deadline expired and the process group was terminated."
                )
            events = selector.select(timeout=min(remaining, 0.05))
            for key, _mask in events:
                name, buffer, maximum = streams[key.fd]
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                buffer.extend(chunk)
                if len(buffer) > maximum:
                    raise OcrResourceLimitExceededError(
                        f"OCR {name} exceeded its configured byte limit."
                    )
        return bytes(streams[process.stdout.fileno()][1]), bytes(
            streams[process.stderr.fileno()][1]
        )
    finally:
        selector.close()


def _terminate_process(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        try:
            process.wait(timeout=min(0.02, max(0.001, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=max(0.05, grace_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    _terminate_remaining_process_group(process.pid, grace_seconds)


def _terminate_remaining_process_group(process_group: int, grace_seconds: float) -> None:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    post_kill_deadline = time.monotonic() + grace_seconds
    while time.monotonic() < post_kill_deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(min(0.01, max(0.0, post_kill_deadline - time.monotonic())))


def _parse_tesseract_version(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MalformedOcrOutputError("OCR version output is not valid UTF-8.") from None
    first_line = text.splitlines()[0] if text.splitlines() else ""
    match = re.fullmatch(r"tesseract\s+([A-Za-z0-9][A-Za-z0-9._+-]*)", first_line.strip())
    if match is None:
        raise MalformedOcrOutputError("OCR version output is malformed.")
    return match.group(1)


def _parse_tesseract_tsv(raw: bytes, *, limits: ReceiptOcrLimits) -> tuple[ReceiptOcrBlock, ...]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MalformedOcrOutputError("OCR TSV output is not valid UTF-8.") from None
    lines = text.splitlines()
    if not lines:
        return ()
    if lines[0] != _TSV_HEADER:
        raise MalformedOcrOutputError("OCR TSV header is malformed.")
    page_dimensions: dict[int, tuple[int, int]] = {}
    words: list[tuple[int, int, int, int, int, int, int, int, int, str, str]] = []
    for line in lines[1:]:
        columns = line.split("\t", 11)
        if len(columns) != 12:
            raise MalformedOcrOutputError("OCR TSV row has an unexpected column count.")
        try:
            numbers = tuple(int(value) for value in columns[:10])
        except ValueError:
            raise MalformedOcrOutputError("OCR TSV contains a malformed integer.") from None
        level, page_num, block_num, par_num, line_num, word_num, left, top, width, height = numbers
        if page_num <= 0:
            raise MalformedOcrOutputError("OCR TSV page numbering is malformed.")
        page_index = page_num - 1
        if level == 1:
            if page_index in page_dimensions:
                raise MalformedOcrOutputError("OCR TSV page dimensions are malformed.")
            if page_index >= limits.max_page_count or len(page_dimensions) >= limits.max_page_count:
                raise OcrResourceLimitExceededError("OCR page count exceeded its configured limit.")
            if width <= 0 or height <= 0:
                raise MalformedOcrOutputError("OCR TSV page dimensions are malformed.")
            if width > limits.max_image_width or height > limits.max_image_height:
                raise OcrResourceLimitExceededError(
                    "OCR page dimensions exceeded configured limits."
                )
            page_dimensions[page_index] = (width, height)
        elif level == 5 and columns[11]:
            words.append(
                (
                    page_index,
                    block_num - 1,
                    par_num - 1,
                    line_num - 1,
                    word_num - 1,
                    left,
                    top,
                    width,
                    height,
                    columns[10],
                    columns[11],
                )
            )
    if words and not page_dimensions:
        raise MalformedOcrOutputError("OCR TSV omitted page dimensions.")
    blocks: list[ReceiptOcrBlock] = []
    for sequence_index, word in enumerate(words):
        (
            page,
            block,
            paragraph,
            line_index,
            word_index,
            left,
            top,
            width,
            height,
            conf,
            value,
        ) = word
        dimensions = page_dimensions.get(page)
        if dimensions is None:
            raise MalformedOcrOutputError("OCR TSV word references an unknown page.")
        blocks.append(
            ReceiptOcrBlock(
                sequence_index=sequence_index,
                page_index=page,
                engine_block_index=block,
                engine_paragraph_index=paragraph,
                engine_line_index=line_index,
                engine_word_index=word_index,
                text=value,
                left=left,
                top=top,
                width=width,
                height=height,
                page_width=dimensions[0],
                page_height=dimensions[1],
                confidence_scaled=_tesseract_confidence(conf, limits=limits),
            )
        )
    return _normalize_blocks(blocks, limits=limits)


def _tesseract_confidence(value: str, *, limits: ReceiptOcrLimits) -> int | None:
    if value == "-1":
        return None
    try:
        confidence = Decimal(value)
    except InvalidOperation:
        raise MalformedOcrOutputError("OCR TSV confidence is malformed.") from None
    if not confidence.is_finite() or confidence < 0 or confidence > 100:
        raise MalformedOcrOutputError("OCR TSV confidence is outside 0..100.")
    scaled = int((confidence * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if scaled > limits.max_confidence_value:
        raise MalformedOcrOutputError("OCR confidence exceeds the configured scale.")
    return scaled


# ---------------------------------------------------------------------------
# Attachment authority and public service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AttachmentRecord:
    attachment_id: int
    file_path: str
    content_hash: str
    size_bytes: int
    mime_type: str


@dataclass
class _OpenedAttachment:
    record: _AttachmentRecord
    fd: int
    identity: tuple[int, int, int, int, int, int]

    def close(self) -> None:
        os.close(self.fd)


_failure_injection_hook: Callable[[str], None] | None = None
"""Private test-only failure seam at real OCR evidence write boundaries."""


def extract_and_persist_receipt_ocr_evidence(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    attachment_id: int,
    engine: ReceiptOcrEngine,
    limits: ReceiptOcrLimits = ReceiptOcrLimits(),
) -> ReceiptOcrExtractionResult:
    """Extract and append one canonical bounded OCR evidence result."""
    _validate_public_arguments(public_id, attachment_id, engine, limits)
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise OcrStagingDatabaseRejectedError(
            "Receipt OCR persistence requires an authorised staging database."
        ) from exc
    if conn.in_transaction:
        raise OcrCallerOwnedTransactionError(
            "Receipt OCR persistence requires a connection without pending work."
        )
    identity = _validate_engine_identity(engine.identity, limits=limits)
    attachment = _load_attachment_record(conn, attachment_id)
    _reject_attachment_above_absolute_ceiling(attachment)
    fingerprint = _extraction_fingerprint(attachment, identity, limits)
    existing = _lookup_existing(conn, public_id=public_id, fingerprint=fingerprint)
    _reject_preflight_conflicts(existing, public_id=public_id, fingerprint=fingerprint)

    opened = _open_and_verify_attachment(attachment)
    try:
        if existing[0] is not None:
            return _verify_persisted_result(
                conn,
                dict(existing[0]),
                expected_public_id=public_id,
                expected_fingerprint=fingerprint,
                expected_attachment=attachment,
                expected_identity=identity,
                limits=limits,
                expected_result_hash=None,
                idempotent=True,
            )

        deterministic = _deterministic_service_outcome(attachment, limits=limits)
        if deterministic is None:
            deadline = time.monotonic() + limits.total_timeout_seconds
            source = ReceiptOcrSource(
                file_descriptor=opened.fd,
                attachment_path=attachment.file_path,
                size_bytes=attachment.size_bytes,
                content_hash=attachment.content_hash,
                mime_type=attachment.mime_type,
            )
            try:
                raw_result = engine.extract(source, limits=limits, deadline=deadline)
            except ReceiptOcrError:
                raise
            except Exception:
                raise OcrEngineLaunchError("The OCR engine execution failed.") from None
            if _validate_engine_identity(engine.identity, limits=limits) != identity:
                raise InvalidOcrConfigurationError(
                    "The OCR engine identity changed during extraction."
                )
            normalized = _normalize_engine_result(raw_result, limits=limits)
        else:
            normalized = deterministic

        _reverify_opened_attachment(opened)
        return _persist_normalized_result(
            conn,
            public_id=public_id,
            attachment=attachment,
            identity=identity,
            fingerprint=fingerprint,
            normalized=normalized,
            limits=limits,
        )
    finally:
        opened.close()


@dataclass(frozen=True)
class _NormalizedOutcome:
    status: ReceiptOcrExtractionStatus
    blocks: tuple[ReceiptOcrBlock, ...]
    outcome_code: str
    total_text_length: int
    result_hash: str


def _deterministic_service_outcome(
    attachment: _AttachmentRecord,
    *,
    limits: ReceiptOcrLimits,
) -> _NormalizedOutcome | None:
    if attachment.size_bytes > limits.max_attachment_bytes:
        return _normalized_outcome(
            ReceiptOcrExtractionStatus.RESOURCE_REJECTED,
            (),
            "attachment_size_limit",
            limits=limits,
        )
    if attachment.mime_type == "application/pdf":
        return _normalized_outcome(
            ReceiptOcrExtractionStatus.UNSUPPORTED_INPUT,
            (),
            "pdf_unsupported",
            limits=limits,
        )
    return None


def _validate_public_arguments(
    public_id: object,
    attachment_id: object,
    engine: object,
    limits: object,
) -> None:
    if not isinstance(limits, ReceiptOcrLimits):
        raise InvalidOcrConfigurationError("limits must be ReceiptOcrLimits.")
    if not isinstance(public_id, str) or not public_id.startswith(_PUBLIC_ID_PREFIX):
        raise InvalidOcrConfigurationError("public_id must use the rocr_ prefix.")
    if (
        not public_id
        or len(public_id) > limits.max_public_id_length
        or not public_id.isascii()
        or re.fullmatch(r"[A-Za-z0-9_-]+", public_id) is None
    ):
        raise InvalidOcrConfigurationError("public_id is malformed or oversized.")
    if isinstance(attachment_id, bool) or not isinstance(attachment_id, int) or attachment_id <= 0:
        raise InvalidOcrConfigurationError("attachment_id must be a positive integer.")
    if not isinstance(engine, ReceiptOcrEngine):
        raise InvalidOcrConfigurationError("engine must implement ReceiptOcrEngine.")


def _validate_engine_identity(
    identity: object, *, limits: ReceiptOcrLimits
) -> ReceiptOcrEngineIdentity:
    if not isinstance(identity, ReceiptOcrEngineIdentity):
        raise InvalidOcrConfigurationError("engine identity is malformed.")
    if not _safe_identity(identity.name, maximum=limits.max_engine_identity_length):
        raise InvalidOcrConfigurationError("engine name is malformed.")
    if not _safe_identity(identity.version, maximum=limits.max_engine_identity_length):
        raise InvalidOcrConfigurationError("engine version is malformed.")
    if _SHA256_RE.fullmatch(identity.binary_sha256) is None:
        raise InvalidOcrConfigurationError("engine binary identity is malformed.")
    if _SHA256_RE.fullmatch(identity.configuration_hash) is None:
        raise InvalidOcrConfigurationError("engine configuration identity is malformed.")
    return identity


def _safe_identity(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and _SAFE_IDENTITY_RE.fullmatch(value) is not None
    )


def _load_attachment_record(conn: sqlite3.Connection, attachment_id: int) -> _AttachmentRecord:
    # Schema capability check: local_attachment_source exists only in >= 040.
    has_local_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_attachment_source'"
    ).fetchone()

    if has_local_table:
        query = """
            SELECT a.id, a.file_path, a.file_hash, a.mime_type,
                   s.observed_file_size, s.content_hash AS source_content_hash,
                   'telegram' AS source_table
            FROM attachments AS a
            INNER JOIN telegram_attachment_source AS s ON s.attachment_id = a.id
            WHERE a.id = ?
            UNION ALL
            SELECT a.id, a.file_path, a.file_hash, a.mime_type,
                   ls.observed_file_size, ls.content_hash AS source_content_hash,
                   'local' AS source_table
            FROM attachments AS a
            INNER JOIN local_attachment_source AS ls ON ls.attachment_id = a.id
            WHERE a.id = ?
        """
        params: tuple[int, ...] = (attachment_id, attachment_id)
    else:
        # Pre-040 schema: only telegram source evidence exists.
        query = """
            SELECT a.id, a.file_path, a.file_hash, a.mime_type,
                   s.observed_file_size, s.content_hash AS source_content_hash,
                   'telegram' AS source_table
            FROM attachments AS a
            INNER JOIN telegram_attachment_source AS s ON s.attachment_id = a.id
            WHERE a.id = ?
        """
        params = (attachment_id,)

    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error as exc:
        raise OcrUnexpectedPersistenceError(
            "Unable to load canonical attachment identity."
        ) from exc
    if not rows:
        raise OcrAttachmentNotFoundError(
            "Canonical attachment was not found or has no persisted source evidence."
        )
    # Reject ambiguous dual-source binding (cross-table trigger should prevent
    # this, but fail closed defensively).
    source_tables = {row["source_table"] for row in rows}
    if len(source_tables) > 1:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment has ambiguous source evidence from multiple channels."
        )
    first = rows[0]
    if first["observed_file_size"] is None:
        raise OcrAttachmentNotFoundError(
            "Canonical attachment has no persisted acquisition-size evidence."
        )
    content_hash = first["file_hash"]
    mime_type = first["mime_type"]
    file_path = first["file_path"]
    if (
        not isinstance(content_hash, str)
        or _SHA256_RE.fullmatch(content_hash) is None
        or not isinstance(mime_type, str)
        or mime_type not in {"image/jpeg", "image/png", "application/pdf"}
        or not isinstance(file_path, str)
        or not file_path
        or any(ord(character) < 32 for character in file_path)
    ):
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment metadata is malformed or unsupported."
        )
    sizes = {int(row["observed_file_size"]) for row in rows}
    source_hashes = {str(row["source_content_hash"]) for row in rows}
    if len(sizes) != 1 or sizes == {-1} or source_hashes != {content_hash}:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment source evidence is contradictory."
        )
    size = next(iter(sizes))
    if size < 0:
        raise OcrAttachmentIntegrityConflictError("Canonical attachment size is invalid.")
    return _AttachmentRecord(
        attachment_id=attachment_id,
        file_path=file_path,
        content_hash=content_hash,
        size_bytes=size,
        mime_type=mime_type,
    )


def _reject_attachment_above_absolute_ceiling(record: _AttachmentRecord) -> None:
    if record.size_bytes > ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES:
        raise OcrResourceLimitExceededError(
            "Canonical attachment metadata exceeds the absolute verification ceiling."
        )


def _open_and_verify_attachment(record: _AttachmentRecord) -> _OpenedAttachment:
    path = Path(record.file_path)
    if not path.is_absolute():
        raise OcrAttachmentIntegrityConflictError("Canonical attachment path is not absolute.")
    try:
        entry = os.lstat(path)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment path is missing or inaccessible."
        ) from exc
    if path != resolved or stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment path is not a direct regular file."
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment could not be opened without following links."
        ) from exc
    opened = _OpenedAttachment(record=record, fd=fd, identity=(0, 0, 0, 0, 0, 0))
    try:
        opened.identity = _verify_attachment_fd(opened)
        return opened
    except BaseException:
        opened.close()
        raise


def verify_telegram_original_attachment(
    conn: sqlite3.Connection, *, source_id: int, expected_hash: str
) -> None:
    """Verify current original bytes against the immutable Telegram source row.

    A capture job's database row alone cannot prove that the original remains
    recoverable after a lost ingress response. This uses the same bounded,
    no-symlink descriptor verification as receipt OCR, without running OCR.
    """
    require_staging_database(conn)
    source = conn.execute(
        "SELECT attachment_id, content_hash FROM telegram_attachment_source WHERE id = ?",
        (source_id,),
    ).fetchone()
    if source is None:
        raise OcrAttachmentNotFoundError("Telegram original source evidence is missing.")
    if source["content_hash"] != expected_hash:
        raise OcrAttachmentIntegrityConflictError("Telegram original source hash has changed.")
    record = _load_attachment_record(conn, int(source["attachment_id"]))
    if record.content_hash != expected_hash:
        raise OcrAttachmentIntegrityConflictError("Telegram original attachment hash has changed.")
    _reject_attachment_above_absolute_ceiling(record)
    opened = _open_and_verify_attachment(record)
    opened.close()


def _verify_attachment_fd(opened: _OpenedAttachment) -> tuple[int, int, int, int, int, int]:
    record = opened.record
    try:
        before = os.fstat(opened.fd)
    except OSError as exc:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment handle became invalid."
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise OcrAttachmentIntegrityConflictError("Canonical attachment is not a regular file.")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment is not owned by the current user."
        )
    if stat.S_IMODE(before.st_mode) != 0o400:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment must retain the immutable PR #217 mode 0400."
        )
    if before.st_size > ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES:
        raise OcrResourceLimitExceededError(
            "Canonical attachment bytes exceed the absolute verification ceiling."
        )
    if before.st_size != record.size_bytes:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment size differs from persisted evidence."
        )
    try:
        os.lseek(opened.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        prefix = b""
        remaining = record.size_bytes
        while remaining:
            chunk = os.read(opened.fd, min(65_536, remaining))
            if not chunk:
                raise OcrAttachmentIntegrityConflictError(
                    "Canonical attachment ended before its persisted size."
                )
            if len(prefix) < 8:
                prefix += chunk[: 8 - len(prefix)]
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(opened.fd)
        current = os.lstat(record.file_path)
    except OSError as exc:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment changed during identity verification."
        ) from exc
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns")
    before_identity = tuple(int(getattr(before, field)) for field in fields)
    after_identity = tuple(int(getattr(after, field)) for field in fields)
    current_identity = tuple(int(getattr(current, field)) for field in fields)
    if before_identity != after_identity or after_identity != current_identity:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment identity changed during verification."
        )
    if digest.hexdigest() != record.content_hash:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment hash differs from persisted evidence."
        )
    _verify_signature(prefix, record.mime_type)
    os.lseek(opened.fd, 0, os.SEEK_SET)
    return before_identity  # type: ignore[return-value]


def _reverify_opened_attachment(opened: _OpenedAttachment) -> None:
    identity = _verify_attachment_fd(opened)
    if identity != opened.identity:
        raise OcrAttachmentIntegrityConflictError(
            "Canonical attachment identity changed while OCR was running."
        )


def _verify_signature(prefix: bytes, mime_type: str) -> None:
    signatures = {
        "image/jpeg": b"\xff\xd8\xff",
        "image/png": b"\x89PNG\r\n\x1a\n",
        "application/pdf": b"%PDF-",
    }
    if not prefix.startswith(signatures[mime_type]):
        raise OcrAttachmentIntegrityConflictError(
            "Canonical MIME type conflicts with the attachment signature."
        )


# ---------------------------------------------------------------------------
# Normalization, hashes, replay, and persistence
# ---------------------------------------------------------------------------


def _normalize_engine_result(result: object, *, limits: ReceiptOcrLimits) -> _NormalizedOutcome:
    if not isinstance(result, ReceiptOcrEngineResult):
        raise MalformedOcrOutputError("OCR engine result is not the required immutable contract.")
    if result.status not in {
        ReceiptOcrExtractionStatus.SUCCEEDED,
        ReceiptOcrExtractionStatus.NO_TEXT,
        ReceiptOcrExtractionStatus.ENGINE_FAILED,
    }:
        raise MalformedOcrOutputError("OCR engine returned a service-owned status.")
    return _normalized_outcome(result.status, result.blocks, result.outcome_code, limits=limits)


def _normalized_outcome(
    status: ReceiptOcrExtractionStatus,
    blocks: Sequence[ReceiptOcrBlock],
    outcome_code: object,
    *,
    limits: ReceiptOcrLimits,
) -> _NormalizedOutcome:
    if (
        not isinstance(outcome_code, str)
        or len(outcome_code) > 64
        or _OUTCOME_RE.fullmatch(outcome_code) is None
    ):
        raise MalformedOcrOutputError("OCR outcome code is malformed.")
    normalized_blocks = _normalize_blocks(blocks, limits=limits)
    if status == ReceiptOcrExtractionStatus.SUCCEEDED and not normalized_blocks:
        raise MalformedOcrOutputError("A succeeded OCR result must contain text blocks.")
    if status != ReceiptOcrExtractionStatus.SUCCEEDED and normalized_blocks:
        raise MalformedOcrOutputError("A non-succeeded OCR result must not contain blocks.")
    total = sum(len(block.text) for block in normalized_blocks)
    payload = _result_payload(status, outcome_code, normalized_blocks)
    return _NormalizedOutcome(
        status=status,
        blocks=normalized_blocks,
        outcome_code=outcome_code,
        total_text_length=total,
        result_hash=hashlib.sha256(_canonical_json_bytes(payload)).hexdigest(),
    )


def _normalize_blocks(
    blocks: Sequence[ReceiptOcrBlock], *, limits: ReceiptOcrLimits
) -> tuple[ReceiptOcrBlock, ...]:
    if not isinstance(blocks, (tuple, list)):
        raise MalformedOcrOutputError("OCR blocks must be a bounded sequence.")
    if len(blocks) > limits.max_block_count:
        raise OcrResourceLimitExceededError("OCR block count exceeded its configured limit.")
    normalized: list[ReceiptOcrBlock] = []
    for block in blocks:
        if not isinstance(block, ReceiptOcrBlock):
            raise MalformedOcrOutputError("OCR block is malformed.")
        integer_values = {
            "sequence_index": block.sequence_index,
            "page_index": block.page_index,
            "left": block.left,
            "top": block.top,
            "width": block.width,
            "height": block.height,
            "page_width": block.page_width,
            "page_height": block.page_height,
        }
        for name, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise MalformedOcrOutputError(f"OCR block {name} must be an integer.")
        optional_indexes = (
            block.engine_block_index,
            block.engine_paragraph_index,
            block.engine_line_index,
            block.engine_word_index,
        )
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for value in optional_indexes
        ):
            raise MalformedOcrOutputError("OCR engine ordering indexes are malformed.")
        if block.sequence_index < 0 or block.page_index < 0:
            raise MalformedOcrOutputError("OCR sequence and page indexes must be non-negative.")
        if block.page_index >= limits.max_page_count:
            raise OcrResourceLimitExceededError("OCR page count exceeded its configured limit.")
        if min(block.left, block.top, block.width, block.height) < 0:
            raise MalformedOcrOutputError("OCR coordinates must be non-negative.")
        if max(block.left, block.top, block.width, block.height) > limits.max_coordinate_value:
            raise OcrResourceLimitExceededError("OCR coordinate exceeded its configured limit.")
        if (
            block.page_width <= 0
            or block.page_width > limits.max_image_width
            or block.page_height <= 0
            or block.page_height > limits.max_image_height
        ):
            raise OcrResourceLimitExceededError("OCR page dimensions exceeded configured limits.")
        if (
            block.left + block.width > block.page_width
            or block.top + block.height > block.page_height
        ):
            raise MalformedOcrOutputError("OCR coordinates extend outside the declared page.")
        confidence = block.confidence_scaled
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or confidence < 0
            or confidence > limits.max_confidence_value
        ):
            raise MalformedOcrOutputError("OCR confidence is outside its integer scale.")
        if not isinstance(block.text, str):
            raise MalformedOcrOutputError("OCR block text must be UTF-8 text.")
        text = unicodedata.normalize("NFC", block.text.replace("\r\n", "\n").replace("\r", "\n"))
        if not text or any(unicodedata.category(character) == "Cc" for character in text):
            raise MalformedOcrOutputError("OCR block text is empty or contains control characters.")
        if len(text) > limits.max_text_characters_per_block:
            raise OcrResourceLimitExceededError("OCR block text exceeded its configured limit.")
        try:
            text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise MalformedOcrOutputError("OCR block text is not valid UTF-8.") from None
        normalized.append(replace(block, text=text))
    normalized.sort(key=lambda item: item.sequence_index)
    if [item.sequence_index for item in normalized] != list(range(len(normalized))):
        raise MalformedOcrOutputError(
            "OCR sequence indexes must be unique and contiguous from zero."
        )
    total = sum(len(item.text) for item in normalized)
    if total > limits.max_total_normalized_text_characters:
        raise OcrResourceLimitExceededError("Total OCR text exceeded its configured limit.")
    return tuple(normalized)


def _result_payload(
    status: ReceiptOcrExtractionStatus,
    outcome_code: str,
    blocks: Sequence[ReceiptOcrBlock],
) -> dict[str, object]:
    return {
        "contract_version": _CONTRACT_VERSION,
        "status": status.value,
        "outcome_code": outcome_code,
        "blocks": [
            {
                "sequence_index": block.sequence_index,
                "page_index": block.page_index,
                "engine_block_index": block.engine_block_index,
                "engine_paragraph_index": block.engine_paragraph_index,
                "engine_line_index": block.engine_line_index,
                "engine_word_index": block.engine_word_index,
                "text": block.text,
                "left": block.left,
                "top": block.top,
                "width": block.width,
                "height": block.height,
                "page_width": block.page_width,
                "page_height": block.page_height,
                "confidence_scaled": block.confidence_scaled,
            }
            for block in blocks
        ],
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _extraction_fingerprint(
    attachment: _AttachmentRecord,
    identity: ReceiptOcrEngineIdentity,
    limits: ReceiptOcrLimits,
) -> str:
    material = (
        ("contract_version", _CONTRACT_VERSION),
        ("attachment_hash", attachment.content_hash),
        ("attachment_size", str(attachment.size_bytes)),
        ("source_mime_type", attachment.mime_type),
        ("engine_name", identity.name),
        ("engine_version", identity.version),
        ("engine_binary_sha256", identity.binary_sha256),
        ("engine_configuration_hash", identity.configuration_hash),
        ("effective_limits", _canonical_json_bytes(_limits_payload(limits)).decode("utf-8")),
    )
    digest = hashlib.sha256()
    for label, value in material:
        label_bytes = label.encode("ascii")
        value_bytes = value.encode("utf-8")
        digest.update(len(label_bytes).to_bytes(4, "big"))
        digest.update(label_bytes)
        digest.update(len(value_bytes).to_bytes(8, "big"))
        digest.update(value_bytes)
    return digest.hexdigest()


def _limits_payload(limits: ReceiptOcrLimits) -> dict[str, int | float]:
    return {
        name: getattr(limits, name)
        for name in limits.__dataclass_fields__
        if name != "max_public_id_length"
    }


def _lookup_existing(
    conn: sqlite3.Connection, *, public_id: str, fingerprint: str
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    try:
        by_public = conn.execute(
            "SELECT * FROM receipt_ocr_extractions WHERE public_id = ?", (public_id,)
        ).fetchone()
        by_fingerprint = conn.execute(
            "SELECT * FROM receipt_ocr_extractions WHERE extraction_fingerprint = ?",
            (fingerprint,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise OcrUnexpectedPersistenceError("Unable to inspect persisted OCR evidence.") from exc
    return by_public, by_fingerprint


def _reject_preflight_conflicts(
    existing: tuple[sqlite3.Row | None, sqlite3.Row | None],
    *,
    public_id: str,
    fingerprint: str,
) -> None:
    by_public, by_fingerprint = existing
    if by_public is not None and by_public["extraction_fingerprint"] != fingerprint:
        raise OcrIdempotencyConflictError(
            "The OCR public ID is already bound to different extraction material."
        )
    if by_fingerprint is not None and by_fingerprint["public_id"] != public_id:
        raise OcrIdempotencyConflictError(
            "The OCR extraction fingerprint is already owned by another public ID."
        )


def _persist_normalized_result(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    attachment: _AttachmentRecord,
    identity: ReceiptOcrEngineIdentity,
    fingerprint: str,
    normalized: _NormalizedOutcome,
    limits: ReceiptOcrLimits,
) -> ReceiptOcrExtractionResult:
    try:
        conn.execute("BEGIN IMMEDIATE")
        by_public, by_fingerprint = _lookup_existing(
            conn, public_id=public_id, fingerprint=fingerprint
        )
        _reject_preflight_conflicts(
            (by_public, by_fingerprint), public_id=public_id, fingerprint=fingerprint
        )
        if by_public is not None:
            result = _verify_persisted_result(
                conn,
                dict(by_public),
                expected_public_id=public_id,
                expected_fingerprint=fingerprint,
                expected_attachment=attachment,
                expected_identity=identity,
                limits=limits,
                expected_result_hash=normalized.result_hash,
                idempotent=True,
            )
            conn.commit()
            return result

        _inject_failure("before_extraction_insert")
        cursor = conn.execute(
            """
            INSERT INTO receipt_ocr_extractions (
                public_id, attachment_id, source_attachment_hash,
                source_attachment_size, source_mime_type, engine_name,
                engine_version, engine_binary_sha256, engine_configuration_hash,
                extraction_fingerprint, extraction_status, block_count,
                total_normalized_text_length, normalized_result_hash,
                sanitized_outcome_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                attachment.attachment_id,
                attachment.content_hash,
                attachment.size_bytes,
                attachment.mime_type,
                identity.name,
                identity.version,
                identity.binary_sha256,
                identity.configuration_hash,
                fingerprint,
                normalized.status.value,
                len(normalized.blocks),
                normalized.total_text_length,
                normalized.result_hash,
                normalized.outcome_code,
                datetime.now(UTC).isoformat(),
            ),
        )
        extraction_id = cursor.lastrowid
        if extraction_id is None:
            raise OcrUnexpectedPersistenceError("OCR extraction insert returned no identity.")
        _inject_failure("after_extraction_insert")
        for index, block in enumerate(normalized.blocks):
            _inject_failure(
                "during_first_block_insert" if index == 0 else "during_later_block_insert"
            )
            conn.execute(
                """
                INSERT INTO receipt_ocr_blocks (
                    extraction_id, sequence_index, page_index,
                    engine_block_index, engine_paragraph_index,
                    engine_line_index, engine_word_index, normalized_text,
                    coordinate_left, coordinate_top, coordinate_width,
                    coordinate_height, page_width, page_height, confidence_scaled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_id,
                    block.sequence_index,
                    block.page_index,
                    block.engine_block_index,
                    block.engine_paragraph_index,
                    block.engine_line_index,
                    block.engine_word_index,
                    block.text,
                    block.left,
                    block.top,
                    block.width,
                    block.height,
                    block.page_width,
                    block.page_height,
                    block.confidence_scaled,
                ),
            )
        _inject_failure("after_all_block_inserts")
        _inject_failure("before_persisted_verification")
        persisted = conn.execute(
            "SELECT * FROM receipt_ocr_extractions WHERE id = ?", (extraction_id,)
        ).fetchone()
        if persisted is None:
            raise OcrUnexpectedPersistenceError("Persisted OCR extraction disappeared.")
        result = _verify_persisted_result(
            conn,
            dict(persisted),
            expected_public_id=public_id,
            expected_fingerprint=fingerprint,
            expected_attachment=attachment,
            expected_identity=identity,
            limits=limits,
            expected_result_hash=normalized.result_hash,
            idempotent=False,
        )
        _inject_failure("before_commit")
        conn.commit()
        return result
    except (OcrIdempotencyConflictError, OcrPersistenceConflictError):
        if conn.in_transaction:
            conn.rollback()
        raise
    except OcrUnexpectedPersistenceError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise OcrUnexpectedPersistenceError(
            "Receipt OCR evidence could not be persisted atomically."
        ) from exc
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if not isinstance(exc, Exception):
            raise
        raise OcrUnexpectedPersistenceError(
            "Receipt OCR evidence persistence failed at a guarded write boundary."
        ) from exc


def _verify_persisted_result(
    conn: sqlite3.Connection,
    row: dict[str, object],
    *,
    expected_public_id: str,
    expected_fingerprint: str,
    expected_attachment: _AttachmentRecord,
    expected_identity: ReceiptOcrEngineIdentity,
    limits: ReceiptOcrLimits,
    expected_result_hash: str | None,
    idempotent: bool,
) -> ReceiptOcrExtractionResult:
    expected_fields = {
        "public_id": expected_public_id,
        "attachment_id": expected_attachment.attachment_id,
        "source_attachment_hash": expected_attachment.content_hash,
        "source_attachment_size": expected_attachment.size_bytes,
        "source_mime_type": expected_attachment.mime_type,
        "engine_name": expected_identity.name,
        "engine_version": expected_identity.version,
        "engine_binary_sha256": expected_identity.binary_sha256,
        "engine_configuration_hash": expected_identity.configuration_hash,
        "extraction_fingerprint": expected_fingerprint,
    }
    if any(row.get(name) != value for name, value in expected_fields.items()):
        raise OcrPersistenceConflictError(
            "Persisted OCR extraction identity does not match the replay command."
        )
    try:
        status = ReceiptOcrExtractionStatus(str(row["extraction_status"]))
        block_rows = conn.execute(
            """
            SELECT sequence_index, page_index, engine_block_index,
                   engine_paragraph_index, engine_line_index, engine_word_index,
                   normalized_text, coordinate_left, coordinate_top,
                   coordinate_width, coordinate_height, page_width, page_height,
                   confidence_scaled
            FROM receipt_ocr_blocks
            WHERE extraction_id = ?
            ORDER BY sequence_index
            """,
            (row["id"],),
        ).fetchall()
    except (KeyError, ValueError, sqlite3.Error) as exc:
        raise OcrPersistenceConflictError(
            "Persisted OCR extraction could not be replayed safely."
        ) from exc
    try:
        blocks = tuple(
            ReceiptOcrBlock(
                sequence_index=block["sequence_index"],
                page_index=block["page_index"],
                engine_block_index=block["engine_block_index"],
                engine_paragraph_index=block["engine_paragraph_index"],
                engine_line_index=block["engine_line_index"],
                engine_word_index=block["engine_word_index"],
                text=block["normalized_text"],
                left=block["coordinate_left"],
                top=block["coordinate_top"],
                width=block["coordinate_width"],
                height=block["coordinate_height"],
                page_width=block["page_width"],
                page_height=block["page_height"],
                confidence_scaled=block["confidence_scaled"],
            )
            for block in block_rows
        )
        normalized = _normalized_outcome(
            status, blocks, row.get("sanitized_outcome_code"), limits=limits
        )
    except (ReceiptOcrError, KeyError, TypeError, ValueError) as exc:
        raise OcrPersistenceConflictError(
            "Persisted OCR blocks are malformed or exceed their recorded contract."
        ) from exc
    deterministic = _deterministic_service_outcome(expected_attachment, limits=limits)
    if deterministic is None:
        if normalized.status not in {
            ReceiptOcrExtractionStatus.SUCCEEDED,
            ReceiptOcrExtractionStatus.NO_TEXT,
            ReceiptOcrExtractionStatus.ENGINE_FAILED,
        }:
            raise OcrPersistenceConflictError(
                "Persisted OCR status is impossible for this image replay command."
            )
    elif normalized != deterministic:
        raise OcrPersistenceConflictError(
            "Persisted OCR status contradicts the deterministic service outcome."
        )
    if (
        row.get("block_count") != len(normalized.blocks)
        or row.get("total_normalized_text_length") != normalized.total_text_length
        or row.get("normalized_result_hash") != normalized.result_hash
        or (expected_result_hash is not None and normalized.result_hash != expected_result_hash)
    ):
        raise OcrPersistenceConflictError(
            "Persisted OCR block count, ordering, or result hash is invalid."
        )
    return ReceiptOcrExtractionResult(
        public_id=expected_public_id,
        attachment_id=expected_attachment.attachment_id,
        attachment_hash=expected_attachment.content_hash,
        attachment_size=expected_attachment.size_bytes,
        source_mime_type=expected_attachment.mime_type,
        status=status,
        engine_name=expected_identity.name,
        engine_version=expected_identity.version,
        engine_binary_sha256=expected_identity.binary_sha256,
        engine_configuration_hash=expected_identity.configuration_hash,
        extraction_fingerprint=expected_fingerprint,
        block_count=len(normalized.blocks),
        total_normalized_text_length=normalized.total_text_length,
        normalized_result_hash=normalized.result_hash,
        outcome_code=normalized.outcome_code,
        persistence_idempotent=idempotent,
    )


def _inject_failure(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


__all__ = [
    "ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES",
    "InvalidOcrConfigurationError",
    "MalformedOcrOutputError",
    "OcrAttachmentIntegrityConflictError",
    "OcrAttachmentNotFoundError",
    "OcrCallerOwnedTransactionError",
    "OcrDeadlineExceededError",
    "OcrEngineLaunchError",
    "OcrIdempotencyConflictError",
    "OcrPersistenceConflictError",
    "OcrResourceLimitExceededError",
    "OcrStagingDatabaseRejectedError",
    "OcrUnexpectedPersistenceError",
    "OcrUnsupportedPlatformError",
    "ReceiptOcrBlock",
    "ReceiptOcrEngine",
    "ReceiptOcrEngineIdentity",
    "ReceiptOcrEngineResult",
    "ReceiptOcrError",
    "ReceiptOcrExtractionResult",
    "ReceiptOcrExtractionStatus",
    "ReceiptOcrLimits",
    "ReceiptOcrSource",
    "TesseractTsvOcrEngine",
    "extract_and_persist_receipt_ocr_evidence",
]
