"""Bounded macOS Apple Vision receipt OCR engine (B2, staging-only).

This module provides ``MacOSVisionOcrEngine``, a frozen, replaceable
implementation of the ``ReceiptOcrEngine`` protocol from
``finance_core.intake.receipt_ocr_evidence``.  It drives a committed native Swift
helper (``native/macos_vision_receipt_ocr/main.swift``) that runs Apple
Vision ``VNRecognizeTextRequest`` locally on-device.

Authority boundary: OCR output is untrusted source evidence only.  This
engine never creates parser proposals, confirmations, conversions, receipt
facts, calculations, transactions, settlement obligations, reconciliation
state, or any other final financial state.  Persistence, replay, fingerprint,
and idempotency remain owned by ``extract_and_persist_receipt_ocr_evidence``
and migration 032.

Platform scope: Darwin on Apple Silicon arm64 only.  Every other platform
fails closed with ``OcrUnsupportedPlatformError``.  The engine is
staging-only and locally buildable; it is not production-enabled.

Process-safety summary (differences from the Linux Tesseract adapter):

- macOS has no ``/proc/self/fd`` executable strategy.  The verified helper
  binary is copied once per run into a private ``0700`` directory and the
  private copy (mode ``0500``) is executed.  Both the configured binary and
  the executed copy are hash-verified before and after execution.
- Darwin does not reliably enforce ``RLIMIT_AS``.  Address-space control is
  replaced by a parent-observed direct-helper resident-memory ceiling
  (``proc_pid_rusage`` / ``RUSAGE_INFO_V4``) using the configured
  ``address_space_bytes`` budget.  This is a polling-based parent-observed
  bound, not a kernel-enforced hard limit; Apple framework/XPC resource use
  outside the direct helper process cannot be represented as a Linux-style
  process-tree ``RLIMIT_AS`` guarantee.
- ``RLIMIT_CPU``, ``RLIMIT_FSIZE``, ``RLIMIT_NPROC``, and ``RLIMIT_NOFILE``
  are enforced in the child.  No shell, no ``PATH`` lookup, fixed argv,
  ``stdin=DEVNULL``, ``close_fds=True``, a private working directory, and a
  minimal explicit environment stripped of HOME-derived, proxy, credential,
  Python, Swift, and DYLD injection variables.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - exercised only on unsupported platforms
    resource = None  # type: ignore[assignment]

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
    ReceiptOcrError,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    ReceiptOcrSource,
)


class OcrProcessCleanupError(ReceiptOcrError):
    """A terminated OCR process group could not be confirmed reclaimed."""


# ---------------------------------------------------------------------------
# Protocol constants (bound into the configuration hash)
# ---------------------------------------------------------------------------

HELPER_PROTOCOL_VERSION = 1
HELPER_NAME = "macos_vision_receipt_ocr"
VISION_REQUEST_REVISION = 3
RECOGNITION_LEVEL = "accurate"
USES_LANGUAGE_CORRECTION = False
_ADAPTER_LABEL = "macos-vision-v1"
MAX_LANGUAGE_COUNT = 8
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-[A-Z]{2})?$")
# The authoritative set of recognition languages is the host's real Vision
# capability for the pinned revision/level, reported by the native helper's
# --identity command and validated at construction. No hardcoded allowlist is
# used, so a language Vision would reject is never accepted, and runtime
# capability drift changes the configuration hash rather than being reused.
# Normalized Vision bounding boxes may drift insignificantly past [0, 1]
# because of floating-point edge rounding.  Drift beyond this epsilon is a
# material geometry violation and fails closed.
_GEOMETRY_DRIFT_EPSILON = Decimal("0.001")

# ---------------------------------------------------------------------------
# Resident-memory observation (Darwin libproc)
# ---------------------------------------------------------------------------

_RUSAGE_INFO_V4 = 4


class _RusageInfoV4(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
        ("ri_flags", ctypes.c_uint64),
    ]


_LIBPROC: ctypes.CDLL | None = None
"""Loaded Darwin libproc handle, or None when unavailable."""

if sys.platform == "darwin":  # pragma: no branch - platform-conditional load
    try:
        _candidate = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        _candidate.proc_pid_rusage.restype = ctypes.c_int
        _candidate.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_RusageInfoV4),
        ]
        _smoke = _RusageInfoV4()
        if _candidate.proc_pid_rusage(os.getpid(), _RUSAGE_INFO_V4, ctypes.byref(_smoke)) == 0:
            _LIBPROC = _candidate
    except OSError:  # pragma: no cover - system library normally present
        _LIBPROC = None


def _observe_child_resident_bytes(pid: int) -> int | None:
    """Return the direct child's resident bytes, or None when it has exited."""
    if _LIBPROC is None:
        return None
    info = _RusageInfoV4()
    if _LIBPROC.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(info)) != 0:
        return None
    return int(info.ri_resident_size)


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


def _require_macos_vision_platform() -> None:
    """Fail closed unless Darwin/arm64 process and memory controls exist."""
    if sys.platform != "darwin":
        raise OcrUnsupportedPlatformError("The macOS Vision OCR engine requires Darwin.")
    if platform.machine() != "arm64":
        raise OcrUnsupportedPlatformError(
            "The macOS Vision OCR engine requires Apple Silicon arm64."
        )
    if resource is None or not hasattr(os, "killpg"):
        raise OcrUnsupportedPlatformError("Required POSIX OCR process controls are unavailable.")
    for name in ("RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_NOFILE"):
        if not hasattr(resource, name):
            raise OcrUnsupportedPlatformError("Required POSIX resource limits are unavailable.")
    if _LIBPROC is None:
        raise OcrUnsupportedPlatformError(
            "Parent-observed resident memory monitoring is unavailable on this host."
        )


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def _validate_language_format(languages: object) -> tuple[str, ...]:
    """Validate the shape of the ordered Vision recognition language list.

    Membership in the host's real Vision capability is checked separately at
    construction against the helper-reported supported languages.
    """
    if isinstance(languages, str) or not isinstance(languages, Sequence):
        raise InvalidOcrConfigurationError("languages must be an ordered sequence.")
    values = tuple(languages)
    if not values:
        raise InvalidOcrConfigurationError("languages must not be empty.")
    if len(values) > MAX_LANGUAGE_COUNT:
        raise InvalidOcrConfigurationError(
            f"languages must not contain more than {MAX_LANGUAGE_COUNT} entries."
        )
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or _LANGUAGE_TAG_RE.fullmatch(value) is None:
            raise InvalidOcrConfigurationError("language tag is malformed.")
        if value in seen:
            raise InvalidOcrConfigurationError("languages must not contain duplicates.")
        seen.add(value)
    return values


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _configuration_hash(
    *,
    languages: tuple[str, ...],
    macos_version: str,
    darwin_release: str,
    machine: str,
    supported_languages_hash: str,
) -> str:
    material = {
        "adapter": _ADAPTER_LABEL,
        "helper_protocol_version": HELPER_PROTOCOL_VERSION,
        "helper_name": HELPER_NAME,
        "languages": list(languages),
        "vision_request_revision": VISION_REQUEST_REVISION,
        "recognition_level": RECOGNITION_LEVEL,
        "uses_language_correction": USES_LANGUAGE_CORRECTION,
        "arguments": [
            "--ocr",
            "<input-fd>",
            "<max-attachment-bytes>",
            "<languages>",
            "<max-blocks>",
            "<max-text-chars-per-block>",
            "<max-image-width>",
            "<max-image-height>",
            "<max-total-text-chars>",
        ],
        "platform": "darwin",
        "machine": machine,
        "darwin_release": darwin_release,
        "macos_version": macos_version,
        "supported_languages_hash": supported_languages_hash,
    }
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def _supported_languages_hash(supported_languages: Sequence[str]) -> str:
    """Stable hash of the host's real Vision language capability."""
    return hashlib.sha256(_canonical_json_bytes(sorted(supported_languages))).hexdigest()


# ---------------------------------------------------------------------------
# Executable verification and private per-run copy
# ---------------------------------------------------------------------------


@dataclass
class _VerifiedExecutable:
    path: Path
    fd: int
    identity: tuple[int, int, int, int, int, int]
    binary_hash: str

    def close(self) -> None:
        os.close(self.fd)


def _validate_executable_stat(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise InvalidOcrConfigurationError("OCR executable must be a regular file.")
    mode = stat.S_IMODE(value.st_mode)
    if not mode & 0o100:
        raise InvalidOcrConfigurationError("OCR executable is not executable.")
    if mode & 0o033:
        raise InvalidOcrConfigurationError(
            "OCR executable must not be writable or executable by group or other users."
        )
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise InvalidOcrConfigurationError(
            "OCR executable must be owned by the current service user."
        )


def _open_verified_executable(
    executable_path: str | Path,
    *,
    expected_identity: tuple[int, int, int, int, int, int] | None = None,
    expected_hash: str | None = None,
) -> _VerifiedExecutable:
    """Open, identity-bind, and hash the configured helper binary safely."""
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


def _reverify_opened_executable(
    executable: _VerifiedExecutable,
    *,
    expected_identity: tuple[int, int, int, int, int, int],
    expected_hash: str,
) -> None:
    """Revalidate the configured binary identity after execution."""
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


def _copy_verified_executable(executable: _VerifiedExecutable, destination: Path) -> None:
    """Copy the opened verified binary into the private run directory.

    After the copy completes, the destination's actual bytes are rehashed and
    compared to the verified binary hash so a short or partial write cannot
    produce an executable copy that differs from the verified source.
    """
    os.lseek(executable.fd, 0, os.SEEK_SET)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
    try:
        while True:
            chunk = os.read(executable.fd, 65_536)
            if not chunk:
                break
            written = 0
            while written < len(chunk):
                written += os.write(fd, chunk[written:])
    finally:
        os.close(fd)
        os.lseek(executable.fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    with open(destination, "rb") as handle:
        while chunk := handle.read(65_536):
            digest.update(chunk)
    if digest.hexdigest() != executable.binary_hash:
        raise InvalidOcrConfigurationError(
            "The private OCR helper copy does not match the verified binary."
        )
    copied = os.lstat(destination)
    _validate_executable_stat(copied)


def _reverify_private_copy(destination: Path, *, expected_hash: str) -> None:
    digest = hashlib.sha256()
    try:
        with open(destination, "rb") as handle:
            while chunk := handle.read(65_536):
                digest.update(chunk)
        current = os.lstat(destination)
    except OSError as exc:
        raise InvalidOcrConfigurationError(
            "The private OCR helper copy could not be revalidated after execution."
        ) from exc
    _validate_executable_stat(current)
    if digest.hexdigest() != expected_hash:
        raise InvalidOcrConfigurationError(
            "The private OCR helper copy identity changed during execution."
        )


# ---------------------------------------------------------------------------
# Bounded process control
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProcessOutput:
    returncode: int
    stdout: bytes


def _resource_limiter(limits: ReceiptOcrLimits) -> Callable[[], None]:
    def apply_limits() -> None:
        if resource is None:  # pragma: no cover - guarded before process launch
            os._exit(127)
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_time_seconds, limits.cpu_time_seconds))
        resource.setrlimit(
            resource.RLIMIT_FSIZE, (limits.output_file_bytes, limits.output_file_bytes)
        )
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.process_count, limits.process_count))
        resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_file_count, limits.open_file_count))
        # RLIMIT_AS is intentionally not set: Darwin does not reliably
        # enforce it.  The parent-observed resident-memory ceiling below
        # provides the direct-helper memory bound on macOS.

    return apply_limits


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
    # Fail closed: the process group must be confirmed gone after SIGKILL and
    # the grace period. A still-existing group is never reported as reclaimed.
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return
    raise OcrProcessCleanupError(
        "The OCR process group could not be confirmed terminated after SIGKILL."
    )


def _read_process_output(
    process: subprocess.Popen[bytes],
    *,
    limits: ReceiptOcrLimits,
    deadline: float,
) -> tuple[bytes, bytes]:
    """Read bounded stdout/stderr while polling the child's resident memory."""
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
            resident = _observe_child_resident_bytes(process.pid)
            if resident is None:
                # A None probe is only benign once the child has confirmed
                # exit. While the child is still alive, the configured memory
                # bound cannot be verified, so fail closed.
                if process.poll() is None:
                    raise OcrResourceLimitExceededError(
                        "OCR helper resident memory could not be observed; "
                        "the configured memory bound cannot be enforced."
                    )
            elif resident > limits.address_space_bytes:
                raise OcrResourceLimitExceededError(
                    "OCR helper resident memory exceeded its configured budget."
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


# ---------------------------------------------------------------------------
# Strict helper output parsing
# ---------------------------------------------------------------------------


def _reject_json_constant(name: str) -> Any:
    raise MalformedOcrOutputError("OCR helper output contains a non-finite JSON constant.")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedOcrOutputError("OCR helper output contains duplicate JSON keys.")
        result[key] = value
    return result


def _parse_helper_json(raw: bytes, *, limits: ReceiptOcrLimits) -> dict[str, Any]:
    """Decode and strictly validate the single JSON result from the helper."""
    if len(raw) > limits.max_stdout_bytes:
        raise OcrResourceLimitExceededError("OCR output exceeded its configured byte limit.")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MalformedOcrOutputError("OCR helper output is not valid UTF-8.") from None
    try:
        payload = json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, MalformedOcrOutputError, RecursionError):
        raise MalformedOcrOutputError("OCR helper output is not strict JSON.") from None
    if not isinstance(payload, dict):
        raise MalformedOcrOutputError("OCR helper output must be a JSON object.")
    return payload


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str]) -> None:
    keys = set(payload)
    if keys != expected:
        raise MalformedOcrOutputError("OCR helper output has missing or unknown fields.")


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedOcrOutputError(f"OCR helper field {key} must be an integer.")
    return value


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise MalformedOcrOutputError(f"OCR helper field {key} must be a string.")
    return value


def _require_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise MalformedOcrOutputError(f"OCR helper field {key} must be a boolean.")
    return value


_IDENTITY_KEYS = frozenset(
    {
        "protocol_version",
        "helper_name",
        "helper_version",
        "vision_request_revision",
        "recognition_level",
        "uses_language_correction",
        "supported_languages",
    }
)


def _verify_helper_identity(
    raw: bytes, *, expected_version: str, limits: ReceiptOcrLimits
) -> dict[str, Any]:
    """Validate the helper identity command and return the verified payload.

    The payload includes the host's real Vision language capability
    (``supported_languages``) used for language validation and config binding.
    """
    payload = _parse_helper_json(raw, limits=limits)
    _require_exact_keys(payload, _IDENTITY_KEYS)
    if _require_int(payload, "protocol_version") != HELPER_PROTOCOL_VERSION:
        raise InvalidOcrConfigurationError("OCR helper protocol version is unsupported.")
    if _require_str(payload, "helper_name") != HELPER_NAME:
        raise InvalidOcrConfigurationError("OCR helper name does not match the configured engine.")
    observed_version = _require_str(payload, "helper_version")
    if observed_version != expected_version or not _SAFE_IDENTITY_RE.fullmatch(observed_version):
        raise InvalidOcrConfigurationError(
            "The configured OCR executable version does not match expected_version."
        )
    if _require_int(payload, "vision_request_revision") != VISION_REQUEST_REVISION:
        raise InvalidOcrConfigurationError("OCR helper Vision request revision is unsupported.")
    if _require_str(payload, "recognition_level") != RECOGNITION_LEVEL:
        raise InvalidOcrConfigurationError("OCR helper recognition level is unsupported.")
    if _require_bool(payload, "uses_language_correction") is not USES_LANGUAGE_CORRECTION:
        raise InvalidOcrConfigurationError(
            "OCR helper language correction must remain disabled for the v1 boundary."
        )
    supported = payload["supported_languages"]
    if (
        not isinstance(supported, list)
        or not supported
        or any(not isinstance(item, str) or not item for item in supported)
    ):
        raise InvalidOcrConfigurationError(
            "OCR helper reported an invalid Vision language capability."
        )
    return payload


def _run_identity_query(
    executable: _VerifiedExecutable,
    *,
    expected_version: str,
    limits: ReceiptOcrLimits,
    deadline: float,
) -> dict[str, Any]:
    """Run the bounded ``--identity`` command from a verified private copy.

    Returns the verified identity payload, including the host's real Vision
    language capability. Raises fail-closed on any protocol, identity, or
    configuration violation.
    """
    with tempfile.TemporaryDirectory(prefix="receipt-ocr-vision-") as working_directory:
        os.chmod(working_directory, 0o700)
        copy_path = Path(working_directory) / "helper"
        _copy_verified_executable(executable, copy_path)
        try:
            version_output = _run_bounded_process(
                [str(copy_path), "--identity"],
                pass_fds=(),
                limits=limits,
                deadline=deadline,
                working_directory=working_directory,
            )
        finally:
            _reverify_private_copy(copy_path, expected_hash=executable.binary_hash)
    if version_output.returncode != 0:
        raise InvalidOcrConfigurationError(
            "The configured OCR executable did not provide its expected identity."
        )
    return _verify_helper_identity(
        version_output.stdout, expected_version=expected_version, limits=limits
    )


# ---------------------------------------------------------------------------
# Block conversion: Vision normalized lower-left -> top-left integer pixels
# ---------------------------------------------------------------------------

_OCR_KEYS = frozenset(
    {
        "protocol_version",
        "status",
        "outcome_code",
        "page_width",
        "page_height",
        "orientation",
        "observations",
    }
)
_OBSERVATION_KEYS = frozenset({"index", "text", "confidence", "bounding_box"})


def scale_vision_confidence(confidence: Decimal, *, limits: ReceiptOcrLimits) -> int:
    """Deterministically scale a Vision 0..1 confidence into 0..10,000."""
    if not isinstance(confidence, Decimal) or not confidence.is_finite():
        raise MalformedOcrOutputError("OCR confidence is malformed.")
    if confidence < 0 or confidence > 1:
        raise MalformedOcrOutputError("OCR confidence is outside 0..1.")
    scaled = int((confidence * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if scaled < 0 or scaled > limits.max_confidence_value:
        raise MalformedOcrOutputError("OCR confidence exceeds the configured scale.")
    return scaled


def convert_vision_bounding_box(
    box: tuple[Decimal, Decimal, Decimal, Decimal],
    *,
    page_width: int,
    page_height: int,
) -> tuple[int, int, int, int]:
    """Convert one Vision normalized lower-left box into top-left pixels.

    Vision reports ``(x, y, width, height)`` normalized to [0, 1] with the
    origin at the lower-left of the orientation-corrected image.  The
    evidence contract stores non-negative integer pixels with a top-left
    origin.  Left and top edges use deterministic floor; right and bottom
    edges use deterministic ceil so no part of the text region is lost.
    Insignificant floating-point edge drift (at most ``0.001`` normalized)
    is clamped; materially out-of-range or zero-area geometry fails closed.
    """
    x, y, width, height = box
    for value in box:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise MalformedOcrOutputError("OCR geometry is malformed.")
    if (
        x < -_GEOMETRY_DRIFT_EPSILON
        or y < -_GEOMETRY_DRIFT_EPSILON
        or width < 0
        or height < 0
        or x + width > 1 + _GEOMETRY_DRIFT_EPSILON
        or y + height > 1 + _GEOMETRY_DRIFT_EPSILON
    ):
        raise MalformedOcrOutputError("OCR geometry is materially out of range.")
    left_norm = max(x, Decimal(0))
    bottom_norm = max(y, Decimal(0))
    right_norm = min(x + width, Decimal(1))
    top_norm = min(y + height, Decimal(1))
    left = int((left_norm * page_width).to_integral_value(rounding=ROUND_FLOOR))
    top = int(((1 - top_norm) * page_height).to_integral_value(rounding=ROUND_FLOOR))
    right = int((right_norm * page_width).to_integral_value(rounding=ROUND_CEILING))
    bottom = int(((1 - bottom_norm) * page_height).to_integral_value(rounding=ROUND_CEILING))
    left = max(0, min(left, page_width))
    top = max(0, min(top, page_height))
    right = max(0, min(right, page_width))
    bottom = max(0, min(bottom, page_height))
    box_width = right - left
    box_height = bottom - top
    if box_width <= 0 or box_height <= 0:
        raise MalformedOcrOutputError("OCR geometry has zero area after conversion.")
    return left, top, box_width, box_height


def _block_sort_key(block: ReceiptOcrBlock) -> tuple[int, int, int, int, str, int]:
    confidence = block.confidence_scaled if block.confidence_scaled is not None else -1
    return (block.top, block.left, block.width, block.height, block.text, confidence)


def convert_vision_observations(
    payload: dict[str, Any],
    *,
    limits: ReceiptOcrLimits,
) -> tuple[ReceiptOcrBlock, ...]:
    """Validate strict helper OCR output and produce normalized blocks."""
    _require_exact_keys(payload, _OCR_KEYS)
    if _require_int(payload, "protocol_version") != HELPER_PROTOCOL_VERSION:
        raise MalformedOcrOutputError("OCR helper protocol version is unsupported.")
    status = _require_str(payload, "status")
    outcome_code = _require_str(payload, "outcome_code")
    if status not in {"ok", "no_text"} or outcome_code != status:
        raise MalformedOcrOutputError("OCR helper status is invalid or contradictory.")
    page_width = _require_int(payload, "page_width")
    page_height = _require_int(payload, "page_height")
    orientation = _require_int(payload, "orientation")
    if page_width <= 0 or page_width > limits.max_image_width:
        raise OcrResourceLimitExceededError("OCR page dimensions exceeded configured limits.")
    if page_height <= 0 or page_height > limits.max_image_height:
        raise OcrResourceLimitExceededError("OCR page dimensions exceeded configured limits.")
    if orientation < 1 or orientation > 8:
        raise MalformedOcrOutputError("OCR image orientation is invalid.")
    observations = payload["observations"]
    if not isinstance(observations, list):
        raise MalformedOcrOutputError("OCR observations must be a JSON array.")
    if status == "no_text" and observations:
        raise MalformedOcrOutputError("A no_text result must not contain observations.")
    if status == "ok" and not observations:
        raise MalformedOcrOutputError("An ok result must contain observations.")
    if len(observations) > limits.max_block_count:
        raise OcrResourceLimitExceededError("OCR block count exceeded its configured limit.")

    blocks: list[ReceiptOcrBlock] = []
    total_text = 0
    for observation in observations:
        if not isinstance(observation, dict):
            raise MalformedOcrOutputError("OCR observation must be a JSON object.")
        _require_exact_keys(observation, _OBSERVATION_KEYS)
        index = _require_int(observation, "index")
        if index < 0:
            raise MalformedOcrOutputError("OCR observation index must be non-negative.")
        text = _require_str(observation, "text")
        normalized_text = unicodedata.normalize(
            "NFC", text.replace("\r\n", "\n").replace("\r", "\n")
        )
        if not normalized_text or any(
            unicodedata.category(character) == "Cc" for character in normalized_text
        ):
            raise MalformedOcrOutputError(
                "OCR observation text is empty or contains control characters."
            )
        if len(normalized_text) > limits.max_text_characters_per_block:
            raise OcrResourceLimitExceededError("OCR block text exceeded its configured limit.")
        total_text += len(normalized_text)
        if total_text > limits.max_total_normalized_text_characters:
            raise OcrResourceLimitExceededError("Total OCR text exceeded its configured limit.")
        confidence = observation["confidence"]
        if not isinstance(confidence, Decimal):
            raise MalformedOcrOutputError("OCR confidence must be a JSON number.")
        box = observation["bounding_box"]
        if not isinstance(box, list) or len(box) != 4:
            raise MalformedOcrOutputError("OCR bounding box must have exactly four numbers.")
        box_values = tuple(
            value if isinstance(value, Decimal) else _reject_non_float_box(value) for value in box
        )
        left, top, width, height = convert_vision_bounding_box(
            box_values,  # type: ignore[arg-type]
            page_width=page_width,
            page_height=page_height,
        )
        blocks.append(
            ReceiptOcrBlock(
                sequence_index=0,
                page_index=0,
                engine_block_index=index,
                engine_paragraph_index=None,
                engine_line_index=None,
                engine_word_index=None,
                text=normalized_text,
                left=left,
                top=top,
                width=width,
                height=height,
                page_width=page_width,
                page_height=page_height,
                confidence_scaled=scale_vision_confidence(confidence, limits=limits),
            )
        )

    # Deterministic top-to-bottom, left-to-right ordering with geometry,
    # text, and confidence tie-breakers; then contiguous sequence indexes.
    blocks.sort(key=_block_sort_key)
    ordered = tuple(
        ReceiptOcrBlock(
            sequence_index=sequence_index,
            page_index=block.page_index,
            engine_block_index=block.engine_block_index,
            engine_paragraph_index=block.engine_paragraph_index,
            engine_line_index=block.engine_line_index,
            engine_word_index=block.engine_word_index,
            text=block.text,
            left=block.left,
            top=block.top,
            width=block.width,
            height=block.height,
            page_width=block.page_width,
            page_height=block.page_height,
            confidence_scaled=block.confidence_scaled,
        )
        for sequence_index, block in enumerate(blocks)
    )
    return ordered


def _reject_non_float_box(value: object) -> Decimal:
    raise MalformedOcrOutputError("OCR bounding box values must be JSON numbers.")


def _classify_helper_exit(
    process_output: _ProcessOutput, *, limits: ReceiptOcrLimits
) -> ReceiptOcrEngineResult:
    """Map the helper exit code onto the evidence contract, fail-closed.

    - ``0``: parse the strict JSON result into succeeded/no_text evidence.
    - ``1``: a genuine OCR/image/Vision runtime failure -> persistable
      ``engine_failed`` evidence.
    - ``2``: a usage/argument/protocol/configuration error (including an
      unsupported language) -> fail closed, never persisted as engine_failed.
    - ``3``/``4``: an input or output resource-bound violation -> fail closed
      with ``OcrResourceLimitExceededError``, never persisted as engine_failed.
    - signal death or any other code -> fail closed.
    """
    returncode = process_output.returncode
    if returncode in {
        -signal.SIGXCPU,
        -getattr(signal, "SIGXFSZ", signal.SIGXCPU),
    }:
        raise OcrResourceLimitExceededError(
            "The OCR process exceeded an operating-system resource limit."
        )
    if returncode == 0:
        payload = _parse_helper_json(process_output.stdout, limits=limits)
        blocks = convert_vision_observations(payload, limits=limits)
        if not blocks:
            return ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.NO_TEXT,
                blocks=(),
                outcome_code="no_text",
            )
        return ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks=blocks,
            outcome_code="ok",
        )
    if returncode == 1:
        return ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.ENGINE_FAILED,
            blocks=(),
            outcome_code="engine_exit_nonzero",
        )
    if returncode == 2:
        raise InvalidOcrConfigurationError(
            "The OCR helper reported a configuration or protocol error."
        )
    if returncode in (3, 4):
        raise OcrResourceLimitExceededError("The OCR helper reported a resource bound violation.")
    raise OcrEngineLaunchError("The OCR helper exited with an unexpected status.")


# ---------------------------------------------------------------------------
# Public engine
# ---------------------------------------------------------------------------


class MacOSVisionOcrEngine:
    """Bounded Apple Vision adapter with a verified private per-run copy."""

    def __init__(
        self,
        executable_path: str | Path,
        *,
        expected_version: str,
        languages: Sequence[str] = ("en-US",),
    ) -> None:
        _require_macos_vision_platform()
        if (
            threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            raise OcrUnsupportedPlatformError(
                "The bounded subprocess adapter requires a single-threaded POSIX caller."
            )
        if not isinstance(expected_version, str) or not _SAFE_IDENTITY_RE.fullmatch(
            expected_version
        ):
            raise InvalidOcrConfigurationError("expected_version is malformed.")
        if len(expected_version) > 128:
            raise InvalidOcrConfigurationError("expected_version is malformed.")
        validated_languages = _validate_language_format(languages)
        limits = ReceiptOcrLimits()
        deadline = time.monotonic() + limits.total_timeout_seconds
        executable = _open_verified_executable(executable_path)
        try:
            path = executable.path
            binary_hash = executable.binary_hash
            file_identity = executable.identity
            # Query the host's real Vision capability before accepting the
            # configuration; this also confirms the helper protocol.
            identity_payload = _run_identity_query(
                executable,
                expected_version=expected_version,
                limits=limits,
                deadline=deadline,
            )
            _reverify_opened_executable(
                executable, expected_identity=file_identity, expected_hash=binary_hash
            )
        finally:
            executable.close()
        supported_set = set(identity_payload["supported_languages"])
        for language in validated_languages:
            if language not in supported_set:
                raise InvalidOcrConfigurationError(
                    f"language '{language}' is not supported by the host Vision capability."
                )
        capability_hash = _supported_languages_hash(identity_payload["supported_languages"])
        config_hash = _configuration_hash(
            languages=validated_languages,
            macos_version=platform.mac_ver()[0],
            darwin_release=platform.release(),
            machine=platform.machine(),
            supported_languages_hash=capability_hash,
        )
        self._path = path
        self._languages = validated_languages
        self._file_identity = file_identity
        self._supported_languages_hash = capability_hash
        self._identity = ReceiptOcrEngineIdentity(
            name="macos_vision",
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
        _require_macos_vision_platform()
        if (
            threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            raise OcrUnsupportedPlatformError(
                "The bounded subprocess adapter requires a single-threaded POSIX caller."
            )
        if source.mime_type not in {"image/jpeg", "image/png"}:
            raise InvalidOcrConfigurationError(
                "MacOSVisionOcrEngine accepts only validated JPEG or PNG input."
            )
        executable = _open_verified_executable(
            self._path,
            expected_identity=self._file_identity,
            expected_hash=self._identity.binary_sha256,
        )
        try:
            # Re-verify identity and the real language capability; drift since
            # construction fails closed rather than silently reusing evidence.
            identity_payload = _run_identity_query(
                executable,
                expected_version=self._identity.version,
                limits=limits,
                deadline=deadline,
            )
            if (
                _supported_languages_hash(identity_payload["supported_languages"])
                != self._supported_languages_hash
            ):
                raise InvalidOcrConfigurationError(
                    "OCR helper Vision language capability changed during execution."
                )
            # Run OCR from a fresh verified private copy.
            with tempfile.TemporaryDirectory(prefix="receipt-ocr-vision-") as working_directory:
                os.chmod(working_directory, 0o700)
                copy_path = Path(working_directory) / "helper"
                _copy_verified_executable(executable, copy_path)
                try:
                    process_output = _run_bounded_process(
                        [
                            str(copy_path),
                            "--ocr",
                            str(source.file_descriptor),
                            str(limits.max_attachment_bytes),
                            ",".join(self._languages),
                            str(limits.max_block_count),
                            str(limits.max_text_characters_per_block),
                            str(limits.max_image_width),
                            str(limits.max_image_height),
                            str(limits.max_total_normalized_text_characters),
                        ],
                        pass_fds=(source.file_descriptor,),
                        limits=limits,
                        deadline=deadline,
                        working_directory=working_directory,
                    )
                finally:
                    _reverify_private_copy(copy_path, expected_hash=self._identity.binary_sha256)
            result = _classify_helper_exit(process_output, limits=limits)
            _reverify_opened_executable(
                executable,
                expected_identity=self._file_identity,
                expected_hash=self._identity.binary_sha256,
            )
            return result
        except BaseException:
            _reverify_opened_executable(
                executable,
                expected_identity=self._file_identity,
                expected_hash=self._identity.binary_sha256,
            )
            raise
        finally:
            executable.close()


__all__ = [
    "HELPER_NAME",
    "HELPER_PROTOCOL_VERSION",
    "MacOSVisionOcrEngine",
    "MAX_LANGUAGE_COUNT",
    "OcrProcessCleanupError",
    "RECOGNITION_LEVEL",
    "USES_LANGUAGE_CORRECTION",
    "VISION_REQUEST_REVISION",
    "convert_vision_bounding_box",
    "convert_vision_observations",
    "scale_vision_confidence",
]
