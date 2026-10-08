"""Immutable local image normalization and engine-only OCR evidence bundles.

This additive seam has no SQLite, proposal, posting, provider or runtime write
authority. Legacy receipt OCR attachment identity and fingerprints are untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from finance_core.intake import attachment_publication as publication
from finance_core.intake import receipt_ocr_evidence as ocr
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngine,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    ReceiptOcrSource,
)

CONTRACT = "receipt-media-ocr-v1"
SOURCE_BYTES = 20_000_000
PNG_BYTES = 20_000_000
PIXELS = 24_000_000
DECODED_BYTES = 134_217_728
TEMP_BYTES = 268_435_456
NORMALIZATION_SECONDS = 30.0
_MAX_JSON = 8_000_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION = re.compile(r"media_[A-Za-z0-9_-]{1,120}\Z")
_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_failure_injection_hook: Callable[[str], None] | None = None
_DECODER_PARAMETERS = {
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
_BOUNDS = {
    "source_bytes": SOURCE_BYTES,
    "pixels": PIXELS,
    "decoded_bytes": DECODED_BYTES,
    "png_bytes": PNG_BYTES,
    "temporary_bytes": TEMP_BYTES,
    "address_space_bytes": 536_870_912,
    "cpu_seconds": 30,
    "process_count": 16,
    "open_file_count": 64,
    "stdout_bytes": 262_144,
    "stderr_bytes": 65_536,
    "termination_grace_seconds": 0.25,
    "normalization_wall_seconds": NORMALIZATION_SECONDS,
    "concurrency": 1,
}


class ReceiptMediaError(RuntimeError):
    """Base error; malformed requests and integrity conflicts never succeed."""


class MediaIntegrityError(ReceiptMediaError):
    """Evidence, source, installed decoder or immutable identity changed."""


class MediaOperationConflictError(ReceiptMediaError):
    """An operation ID is already bound to different inputs or configuration."""


class MediaUnsupportedPlatformError(ReceiptMediaError):
    """The bounded native worker requires single-threaded Linux composition."""


@dataclass(frozen=True)
class MediaOcrReference:
    operation_id: str
    intent_sha256: str
    manifest_sha256: str
    original_sha256: str
    normalization_fingerprint: str
    png_sha256: str
    ocr_fingerprint: str
    ocr_result_sha256: str


@dataclass(frozen=True)
class MediaProcessingResult:
    operation_id: str
    status: str
    outcome_code: str
    reference: MediaOcrReference | None
    persistence_idempotent: bool


@dataclass(frozen=True)
class VerifiedMediaOcrEvidence:
    reference: MediaOcrReference
    manifest: dict[str, Any]
    blocks: tuple[ReceiptOcrBlock, ...]
    inventory: tuple[dict[str, Any], ...]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _inject(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


def _checked_json(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise MediaIntegrityError("Evidence has duplicate JSON keys")
            result[name] = value
        return result

    try:
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: None)
        if not isinstance(result, dict) or _canonical(result) != raw:
            raise MediaIntegrityError("Evidence JSON is not canonical")
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise MediaIntegrityError("Evidence JSON is malformed") from exc


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _regular(fd: int, *, mode: int, maximum: int) -> os.stat_result:
    value = os.fstat(fd)
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_IMODE(value.st_mode) != mode
        or value.st_uid != os.getuid()
        or value.st_size > maximum
    ):
        raise MediaIntegrityError("Member type, ownership, mode or size is unsafe")
    return value


def _read_member(directory: int, name: str, maximum: int) -> tuple[bytes, dict[str, Any]]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = _regular(fd, mode=0o400, maximum=maximum)
        chunks = []
        count = 0
        while chunk := os.read(fd, min(65_536, maximum + 1 - count)):
            chunks.append(chunk)
            count += len(chunk)
            if count > maximum:
                raise MediaIntegrityError("Member exceeded its bounded size")
        raw = b"".join(chunks)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise MediaIntegrityError("Member changed while being read")
        return raw, {
            "name": name,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    finally:
        os.close(fd)


def _write_member(directory: int, name: str, raw: bytes) -> dict[str, Any]:
    """Exclusive write: incomplete files are retained and never promoted on replay."""
    if len(raw) > _MAX_JSON:
        raise MediaIntegrityError("Evidence JSON exceeded its bounded size")
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("Evidence write made no progress")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)
    return {"name": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _open_operation(root: publication.StorageRootHandle, operation: str) -> int:
    fd = os.open(operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root.fd)
    value = os.fstat(fd)
    current = os.stat(operation, dir_fd=root.fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_IMODE(value.st_mode) != 0o700
        or value.st_uid != os.getuid()
        or value.st_dev != root.device
        or _identity(value) != _identity(current)
    ):
        os.close(fd)
        raise MediaIntegrityError("Operation directory custody is unsafe")
    return fd


def _platform() -> None:
    if (
        not sys.platform.startswith("linux")
        or threading.current_thread() is not threading.main_thread()
        or threading.active_count() != 1
    ):
        raise MediaUnsupportedPlatformError(
            "Media decoding requires a single-threaded Linux caller"
        )
    try:
        native_threads = len(list(Path("/proc/self/task").iterdir()))
        status = Path("/proc/self/status").read_text()
        capabilities = [
            re.search(r"^" + field + r":\s+([0-9a-fA-F]+)$", status, re.MULTILINE)
            for field in ("CapEff", "CapPrm")
        ]
        if (
            native_threads != 1
            or os.getuid() == 0
            or any(match is None or int(match[1], 16) != 0 for match in capabilities)
        ):
            raise MediaUnsupportedPlatformError(
                "Media caller requires one OS thread, nonroot UID "
                "and no permitted/effective capabilities"
            )
    except OSError as exc:
        raise MediaUnsupportedPlatformError(
            "Linux caller thread/capability evidence is unavailable"
        ) from exc


def _worker_run(
    arguments: list[str], *, pass_fds: tuple[int, ...], directory: str, deadline: float
) -> dict[str, Any]:
    _platform()
    limits = replace(
        ReceiptOcrLimits(),
        max_stdout_bytes=262_144,
        max_stderr_bytes=65_536,
        output_file_bytes=PNG_BYTES,
        open_file_count=64,
    )
    try:
        output = ocr._run_bounded_process(
            arguments,
            pass_fds=pass_fds,
            limits=limits,
            deadline=deadline,
            working_directory=directory,
            pinned_environment=True,
        )
        if output.returncode != 0:
            return {"status": "engine_failed", "outcome_code": "decoder_exit_nonzero"}
        raw = output.stdout.strip()
        return _checked_json(raw)
    except ocr.OcrDeadlineExceededError:
        return {"status": "resource_rejected", "outcome_code": "normalization_deadline"}
    except ocr.OcrResourceLimitExceededError:
        return {"status": "resource_rejected", "outcome_code": "decoder_output_limit"}
    except ocr.ReceiptOcrError:
        return {"status": "engine_failed", "outcome_code": "decoder_launch_failed"}


class ReceiptMediaProcessor:
    """Trusted fixed-root composition; requests cannot select code or limits."""

    def __init__(
        self,
        source_root: str | Path,
        evidence_root: str | Path,
        *,
        ocr_engine: ReceiptOcrEngine,
        ocr_limits: ReceiptOcrLimits = ReceiptOcrLimits(),
    ) -> None:
        if not isinstance(ocr_engine, ReceiptOcrEngine) or not isinstance(
            ocr_limits, ReceiptOcrLimits
        ):
            raise ReceiptMediaError("An explicit local OCR engine and limits are required")
        defaults = ReceiptOcrLimits()
        if any(
            getattr(ocr_limits, name) > getattr(defaults, name)
            for name in ocr_limits.__dataclass_fields__
        ):
            raise ReceiptMediaError("Media OCR limits may tighten, never enlarge, fixed defaults")
        self._source_root = Path(source_root)
        self._evidence_root = Path(evidence_root)
        identities = []
        for path in (self._source_root, self._evidence_root):
            root = publication.open_storage_root(path)
            identities.append((root.device, root.inode))
            root.close()
        if (
            self._source_root == self._evidence_root
            or self._source_root.is_relative_to(self._evidence_root)
            or self._evidence_root.is_relative_to(self._source_root)
        ):
            raise ReceiptMediaError("Source and evidence roots must be disjoint")
        self._root_identities = tuple(identities)
        self._engine, self._ocr_limits = ocr_engine, ocr_limits
        self._ocr_identity = asdict(
            ocr._validate_engine_identity(ocr_engine.identity, limits=ocr_limits)
        )
        self._worker = Path(__file__).with_name("_receipt_media_worker.py").resolve(strict=True)
        # Preserve the venv executable spelling: resolving its symlink invokes
        # the base interpreter and loses pyvenv.cfg/site-packages selection.
        self._python = Path(os.path.abspath(sys.executable))
        self._code = self._code_identity()
        observed = _worker_run(
            [str(self._python), "-I", "-B", str(self._worker), "--identity"],
            pass_fds=(),
            directory=str(self._evidence_root),
            deadline=time.monotonic() + NORMALIZATION_SECONDS,
        )
        if observed.get("status") != "identity" or not isinstance(observed.get("decoder"), dict):
            raise ReceiptMediaError(
                "Installed bounded decoder identity is unavailable: "
                + str(observed.get("outcome_code", "identity_invalid"))
            )
        self._decoder = observed["decoder"]
        if (
            self._decoder.get("python_prefix") != sys.prefix
            or self._decoder.get("python_base_prefix") != sys.base_prefix
        ):
            raise ReceiptMediaError("Decoder Python environment differs from the trusted caller")

    def _code_identity(self) -> dict[str, str]:
        binary = self._python.resolve(strict=True)
        return {
            "worker_sha256": hashlib.sha256(self._worker.read_bytes()).hexdigest(),
            "python_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "python_resolved_path": str(binary),
            "python_invocation_path": str(self._python),
        }

    def _open_root(self, index: int) -> publication.StorageRootHandle:
        root = publication.open_storage_root((self._source_root, self._evidence_root)[index])
        if (root.device, root.inode) != self._root_identities[index]:
            root.close()
            raise MediaIntegrityError("Trusted root identity changed")
        return root

    def _source(
        self, root: publication.StorageRootHandle, relative: str, size: int, digest: str
    ) -> tuple[int, list[int], tuple[int, ...]]:
        parts = Path(relative).parts
        if (
            not parts
            or Path(relative).is_absolute()
            or str(Path(relative)) != relative
            or any(part in {".", ".."} for part in parts)
            or any(ord(c) < 32 for c in relative)
        ):
            raise ReceiptMediaError("Source must be a canonical relative captured path")
        parents = [os.dup(root.fd)]
        try:
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parents[-1])
                value = os.fstat(fd)
                if (
                    stat.S_IMODE(value.st_mode) != 0o700
                    or value.st_uid != os.getuid()
                    or value.st_dev != root.device
                ):
                    os.close(fd)
                    raise MediaIntegrityError("Captured source directory is unsafe")
                parents.append(fd)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parents[-1])
            try:
                before = _regular(fd, mode=0o400, maximum=SOURCE_BYTES)
                identity = _identity(before)
                if before.st_size != size:
                    raise MediaIntegrityError("Captured source size differs from expected identity")
                self._verify_source(fd, parents[-1], parts[-1], identity, digest)
                return fd, parents, identity
            except BaseException:
                os.close(fd)
                raise
        except BaseException:
            for parent in parents:
                os.close(parent)
            raise

    @staticmethod
    def _verify_source_tree(
        root: publication.StorageRootHandle, relative: str, parents: list[int]
    ) -> None:
        publication.assert_storage_root_identity(root)
        for index, part in enumerate(Path(relative).parts[:-1]):
            current = os.stat(part, dir_fd=parents[index], follow_symlinks=False)
            opened = os.fstat(parents[index + 1])
            if (current.st_dev, current.st_ino, current.st_mode, current.st_uid) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_uid,
            ):
                raise MediaIntegrityError("Captured source directory path changed")

    @staticmethod
    def _verify_source(
        fd: int, parent: int, leaf: str, identity: tuple[int, ...], digest: str
    ) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        observed = hashlib.sha256()
        count = 0
        while chunk := os.read(fd, 65_536):
            observed.update(chunk)
            count += len(chunk)
            if count > SOURCE_BYTES:
                raise MediaIntegrityError("Captured source exceeds the fixed input bound")
        if (
            _identity(os.fstat(fd)) != identity
            or _identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != identity
            or observed.hexdigest() != digest
        ):
            raise MediaIntegrityError("Captured source bytes or path identity changed")
        os.lseek(fd, 0, os.SEEK_SET)

    def _intent(
        self,
        operation: str,
        relative: str,
        size: int,
        digest: str,
        mime: str | None,
        filename: str | None,
        received: str,
    ) -> dict[str, Any]:
        if not isinstance(operation, str) or _OPERATION.fullmatch(operation) is None:
            raise ReceiptMediaError("operation_id must be a bounded media_ ASCII token")
        if (
            not isinstance(digest, str)
            or _HASH.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > SOURCE_BYTES
        ):
            raise ReceiptMediaError("Source identity exceeds the fixed captured-input contract")
        if received not in {"local", "telegram_photo", "telegram_document"}:
            raise ReceiptMediaError("received_via is unsupported")
        if not isinstance(relative, str) or not relative or len(relative) > 4096:
            raise ReceiptMediaError("Source relative path is malformed")
        for value in (mime, filename):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or any(ord(c) < 32 for c in value)
            ):
                raise ReceiptMediaError("Received declarations are malformed")
        return {
            "contract": CONTRACT,
            "operation_id": operation,
            "source": {
                "relative_path": relative,
                "size_bytes": size,
                "sha256": digest,
                "declared_mime_type": mime,
                "original_filename": filename,
                "received_via": received,
            },
            "decoder": self._decoder,
            "code": self._code,
            "decoder_parameters": _DECODER_PARAMETERS,
            "bounds": _BOUNDS,
            "ocr_identity": self._ocr_identity,
            "ocr_limits": ocr._limits_payload(self._ocr_limits),
        }

    def process(
        self,
        *,
        operation_id: str,
        source_relative_path: str,
        expected_source_sha256: str,
        expected_source_size: int,
        declared_mime_type: str | None = None,
        original_filename: str | None = None,
        received_via: str = "local",
    ) -> MediaProcessingResult:
        intent = self._intent(
            operation_id,
            source_relative_path,
            expected_source_size,
            expected_source_sha256,
            declared_mime_type,
            original_filename,
            received_via,
        )
        source_root = self._open_root(0)
        try:
            evidence_root = self._open_root(1)
        except BaseException:
            source_root.close()
            raise
        source_fd, operation_fd = -1, -1
        parents: list[int] = []
        reference: MediaOcrReference | None
        try:
            source_fd, parents, source_identity = self._source(
                source_root, source_relative_path, expected_source_size, expected_source_sha256
            )
            self._verify_source_tree(source_root, source_relative_path, parents)
            deadline = time.monotonic() + NORMALIZATION_SECONDS
            publication.acquire_storage_root_lock(
                evidence_root, deadline=deadline, clock=time.monotonic
            )
            try:
                os.mkdir(operation_id, 0o700, dir_fd=evidence_root.fd)
                os.fsync(evidence_root.fd)
                new = True
            except FileExistsError:
                new = False
            operation_fd = _open_operation(evidence_root, operation_id)
            if not new:
                raw, _ = _read_member(operation_fd, "intent.json", _MAX_JSON)
                if raw != _canonical(intent):
                    raise MediaOperationConflictError("Operation ID is already bound differently")
                try:
                    terminal, _ = _read_member(operation_fd, "result.json", _MAX_JSON)
                except FileNotFoundError:
                    return MediaProcessingResult(
                        operation_id, "unknown", "incomplete_operation", None, True
                    )
                result = _checked_json(terminal)
                self._verify_source(
                    source_fd,
                    parents[-1],
                    Path(source_relative_path).name,
                    source_identity,
                    expected_source_sha256,
                )
                self._verify_source_tree(source_root, source_relative_path, parents)
                publication.assert_storage_root_identity(evidence_root)
                if result.get("status") == "succeeded":
                    reference = self._reference(
                        intent, result, hashlib.sha256(terminal).hexdigest()
                    )
                    self._read_verified(operation_fd, reference)
                    return MediaProcessingResult(operation_id, "succeeded", "ok", reference, True)
                self._verify_terminal_failure(operation_fd, intent, result)
                return MediaProcessingResult(
                    operation_id, result["status"], result["outcome_code"], None, True
                )
            _write_member(operation_fd, "intent.json", _canonical(intent))
            _inject("after_intent")
            original_fd = os.open(
                "original.bin",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=operation_fd,
            )
            try:
                remaining = expected_source_size
                while remaining:
                    chunk = os.read(source_fd, min(65_536, remaining))
                    if not chunk:
                        raise MediaIntegrityError("Captured source ended before its expected size")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(original_fd, view)
                        if written <= 0:
                            raise OSError("Original publication made no progress")
                        view = view[written:]
                    remaining -= len(chunk)
                os.fsync(original_fd)
                os.fchmod(original_fd, 0o400)
                os.fsync(original_fd)
                os.fsync(operation_fd)
                self._verify_source(
                    source_fd,
                    parents[-1],
                    Path(source_relative_path).name,
                    source_identity,
                    expected_source_sha256,
                )
                self._verify_source_tree(source_root, source_relative_path, parents)
                original = {
                    "name": "original.bin",
                    "size_bytes": expected_source_size,
                    "sha256": expected_source_sha256,
                }
                _inject("after_original")
                os.close(original_fd)
                original_fd = -1
                original_fd = os.open(
                    "original.bin", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=operation_fd
                )
                result = self._execute(operation_fd, intent, original, original_fd, deadline)
                self._verify_source(
                    source_fd,
                    parents[-1],
                    Path(source_relative_path).name,
                    source_identity,
                    expected_source_sha256,
                )
                self._verify_source_tree(source_root, source_relative_path, parents)
                publication.assert_storage_root_identity(source_root)
                publication.assert_storage_root_identity(evidence_root)
                _inject("before_terminal")
                terminal = _canonical(result)
                _write_member(operation_fd, "result.json", terminal)
                _inject("after_terminal")
                if result["status"] == "succeeded":
                    reference = self._reference(
                        intent, result, hashlib.sha256(terminal).hexdigest()
                    )
                    self._read_verified(operation_fd, reference)
                else:
                    reference = None
                return MediaProcessingResult(
                    operation_id, result["status"], result["outcome_code"], reference, False
                )
            finally:
                if original_fd >= 0:
                    os.close(original_fd)
        finally:
            if operation_fd >= 0:
                os.close(operation_fd)
            if source_fd >= 0:
                os.close(source_fd)
            for parent in parents:
                os.close(parent)
            publication.release_storage_root_lock(evidence_root)
            evidence_root.close()
            source_root.close()

    def _execute(
        self,
        directory: int,
        intent: dict[str, Any],
        original: dict[str, Any],
        original_fd: int,
        deadline: float,
    ) -> dict[str, Any]:
        base = {
            "contract": CONTRACT,
            "operation_id": intent["operation_id"],
            "intent_sha256": _digest(intent),
            "original": original,
        }
        declaration = {
            key: intent["source"][key] for key in ("declared_mime_type", "original_filename")
        }
        _write_member(directory, "declaration.json", _canonical(declaration))
        declaration_fd = os.open(
            "declaration.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        png_fd = os.open(
            "normalized.png",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            original_identity = _identity(_regular(original_fd, mode=0o400, maximum=SOURCE_BYTES))
            self._verify_source(
                original_fd, directory, "original.bin", original_identity, original["sha256"]
            )
            if self._code != self._code_identity():
                raise MediaIntegrityError("Trusted worker code identity changed")
            observed = _worker_run(
                [
                    str(self._python),
                    "-I",
                    "-B",
                    str(self._worker),
                    "--source-fd",
                    str(original_fd),
                    "--output-fd",
                    str(png_fd),
                    "--declaration-fd",
                    str(declaration_fd),
                ],
                pass_fds=(original_fd, png_fd, declaration_fd),
                directory=str(self._evidence_root / intent["operation_id"]),
                deadline=deadline,
            )
            self._verify_source(
                original_fd, directory, "original.bin", original_identity, original["sha256"]
            )
            if "usage" in observed:
                base["decoder_process_usage"] = observed["usage"]
            if "uid_tasks" in observed:
                base["decoder_uid_tasks_at_admission"] = observed["uid_tasks"]
            if observed.get("status") != "normalized":
                return self._failure(base, observed.get("status"), observed.get("outcome_code"))
            if observed.get("decoder") != self._decoder:
                return self._failure(base, "engine_failed", "decoder_identity_changed")
            if self._code != self._code_identity():
                raise MediaIntegrityError("Trusted worker code changed during normalization")
            if time.monotonic() >= deadline:
                return self._failure(base, "resource_rejected", "normalization_deadline")
            metadata = observed.get("normalization")
            self._validate_normalization(metadata)
            assert isinstance(metadata, dict)
            os.fsync(png_fd)
            os.fchmod(png_fd, 0o400)
            os.fsync(png_fd)
            os.fsync(directory)
            # The decoder had a write descriptor; OCR receives a newly opened
            # read-only descriptor after immutable publication.
            os.close(png_fd)
            png_fd = -1
            png_fd = os.open(
                "normalized.png", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            raw, png = _read_member(directory, "normalized.png", PNG_BYTES)
            png_identity = _identity(_regular(png_fd, mode=0o400, maximum=PNG_BYTES))
            self._verify_source(png_fd, directory, "normalized.png", png_identity, png["sha256"])
            self._verify_png_header(raw, metadata)
            self._temporary_bound(directory)
            if time.monotonic() >= deadline:
                return self._failure(base, "resource_rejected", "normalization_deadline")
            normalization = {
                "original": original,
                "png": png,
                "metadata": metadata,
                "decoder": self._decoder,
                "parameters": _DECODER_PARAMETERS,
                "bounds": _BOUNDS,
                "code": self._code,
            }
            normalization_fingerprint = _digest(normalization)
            base.update(
                {
                    "normalization": normalization,
                    "normalization_fingerprint": normalization_fingerprint,
                }
            )
            _inject("after_normalization")
            if (
                asdict(
                    ocr._validate_engine_identity(self._engine.identity, limits=self._ocr_limits)
                )
                != self._ocr_identity
            ):
                return self._failure(base, "engine_failed", "ocr_identity_changed")
            source = ReceiptOcrSource(
                png_fd,
                str(self._evidence_root / intent["operation_id"] / "normalized.png"),
                png["size_bytes"],
                png["sha256"],
                "image/png",
            )
            os.lseek(png_fd, 0, os.SEEK_SET)
            ocr_deadline = time.monotonic() + self._ocr_limits.total_timeout_seconds
            try:
                actual = self._engine.extract(
                    source, limits=self._ocr_limits, deadline=ocr_deadline
                )
                outcome = ocr._normalize_engine_result(actual, limits=self._ocr_limits)
                if time.monotonic() >= ocr_deadline:
                    return self._failure(base, "engine_failed", "ocr_deadline")
            except ocr.OcrDeadlineExceededError:
                return self._failure(base, "engine_failed", "ocr_deadline")
            except ocr.OcrResourceLimitExceededError:
                return self._failure(base, "resource_rejected", "ocr_resource_limit")
            except Exception:
                return self._failure(base, "engine_failed", "ocr_failed")
            if (
                asdict(
                    ocr._validate_engine_identity(self._engine.identity, limits=self._ocr_limits)
                )
                != self._ocr_identity
            ):
                return self._failure(base, "engine_failed", "ocr_identity_changed")
            _, reverified = _read_member(directory, "normalized.png", PNG_BYTES)
            self._verify_source(png_fd, directory, "normalized.png", png_identity, png["sha256"])
            if reverified != png:
                raise MediaIntegrityError("OCR input changed during extraction")
            original_raw, reverified_original = _read_member(
                directory, "original.bin", SOURCE_BYTES
            )
            if (
                reverified_original != original
                or hashlib.sha256(original_raw).hexdigest() != intent["source"]["sha256"]
            ):
                raise MediaIntegrityError("Original changed during extraction")
            payload = {
                "status": outcome.status.value,
                "outcome_code": outcome.outcome_code,
                "blocks": [asdict(block) for block in outcome.blocks],
            }
            result_hash = _digest(payload)
            fingerprint = _digest(
                {
                    "contract": CONTRACT,
                    "normalization_fingerprint": normalization_fingerprint,
                    "original": original,
                    "png": png,
                    "engine": self._ocr_identity,
                    "limits": ocr._limits_payload(self._ocr_limits),
                    "result_sha256": result_hash,
                    "status": payload["status"],
                }
            )
            ocr_member = _write_member(directory, "ocr.json", _canonical(payload))
            base.update(
                {
                    "ocr": {
                        "fingerprint": fingerprint,
                        "identity": self._ocr_identity,
                        "limits": ocr._limits_payload(self._ocr_limits),
                        "result_sha256": result_hash,
                        "member": ocr_member,
                    }
                }
            )
            _inject("after_ocr")
            if outcome.status not in {
                ReceiptOcrExtractionStatus.SUCCEEDED,
                ReceiptOcrExtractionStatus.NO_TEXT,
            }:
                return self._failure(base, "engine_failed", "ocr_engine_failed")
            return {**base, "status": "succeeded", "outcome_code": "ok"}
        finally:
            if png_fd >= 0:
                os.close(png_fd)
            os.close(declaration_fd)

    @staticmethod
    def _failure(base: dict[str, Any], status: Any, code: Any) -> dict[str, Any]:
        if (
            status not in {"unsupported_input", "engine_failed", "resource_rejected"}
            or not isinstance(code, str)
            or _CODE.fullmatch(code) is None
        ):
            status, code = "engine_failed", "decoder_result_invalid"
        return {**base, "status": status, "outcome_code": code}

    @staticmethod
    def _temporary_bound(directory: int) -> None:
        total = 0
        for name in os.listdir(directory):
            if name not in {
                "intent.json",
                "original.bin",
                "declaration.json",
                "normalized.png",
                "ocr.json",
                "result.json",
            }:
                raise MediaIntegrityError("Unexpected operation member is not covered by evidence")
            value = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode):
                raise MediaIntegrityError("Unexpected non-file operation member")
            total += value.st_size
        if total > TEMP_BYTES:
            raise MediaIntegrityError("Aggregate temporary space exceeded its fixed bound")

    @staticmethod
    def _validate_normalization(metadata: Any) -> None:
        if not isinstance(metadata, dict):
            raise MediaIntegrityError("Decoder omitted normalization metadata")
        dimensions = metadata.get("dimensions")
        if (
            not isinstance(dimensions, list)
            or len(dimensions) != 2
            or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in dimensions)
            or dimensions[0] * dimensions[1] > PIXELS
        ):
            raise MediaIntegrityError("Decoder geometry is invalid or exceeds its bound")
        if (
            metadata.get("frame_count") != 1
            or metadata.get("output_mode") != "RGB"
            or metadata.get("decoded_bytes") != dimensions[0] * dimensions[1] * 3
            or metadata["decoded_bytes"] > DECODED_BYTES
        ):
            raise MediaIntegrityError("Decoder output layout is invalid")
        if (
            metadata.get("alpha_policy") != "white_matte"
            or not isinstance(metadata.get("source_has_alpha"), bool)
            or metadata.get("original_bit_depth") not in {8, 10}
            or metadata.get("convert_hdr_to_8bit") is not True
            or metadata.get("detected_mime_type")
            not in {"image/jpeg", "image/png", "image/heic", "image/heif"}
        ):
            raise MediaIntegrityError("Decoder normalization policy differs from fixed parameters")

    @staticmethod
    def _verify_png_header(raw: bytes, metadata: dict[str, Any]) -> None:
        import struct
        import zlib

        if (
            len(raw) < 33
            or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or raw[8:16] != b"\x00\x00\x00\rIHDR"
            or list(struct.unpack(">II", raw[16:24])) != metadata["dimensions"]
            or raw[24:29] != b"\x08\x02\x00\x00\x00"
        ):
            raise MediaIntegrityError("Exact PNG layout differs from decoder evidence")
        offset, saw_data, ended = 8, False, False
        while offset < len(raw):
            if len(raw) - offset < 12:
                raise MediaIntegrityError("PNG chunk is incomplete")
            length = struct.unpack_from(">I", raw, offset)[0]
            if length > len(raw) - offset - 12:
                raise MediaIntegrityError("PNG chunk exceeds bounded input")
            kind = raw[offset + 4 : offset + 8]
            if kind not in {b"IHDR", b"IDAT", b"IEND"}:
                raise MediaIntegrityError("Normalized PNG forwards unexpected metadata or chunks")
            payload_end = offset + 8 + length
            crc = struct.unpack_from(">I", raw, payload_end)[0]
            if zlib.crc32(raw[offset + 4 : payload_end]) != crc:
                raise MediaIntegrityError("PNG chunk integrity differs")
            if kind == b"IDAT":
                saw_data = True
            if kind in {b"acTL", b"fcTL", b"fdAT"}:
                raise MediaIntegrityError("Normalized PNG unexpectedly has multiple frames")
            offset = payload_end + 4
            if kind == b"IEND":
                ended = length == 0 and offset == len(raw)
                break
        if not saw_data or not ended:
            raise MediaIntegrityError("Normalized PNG is incomplete or has trailing bytes")

    @staticmethod
    def _reference(
        intent: dict[str, Any], result: dict[str, Any], manifest_hash: str
    ) -> MediaOcrReference:
        try:
            return MediaOcrReference(
                intent["operation_id"],
                _digest(intent),
                manifest_hash,
                result["original"]["sha256"],
                result["normalization_fingerprint"],
                result["normalization"]["png"]["sha256"],
                result["ocr"]["fingerprint"],
                result["ocr"]["result_sha256"],
            )
        except (KeyError, TypeError) as exc:
            raise MediaIntegrityError("Successful terminal reference is incomplete") from exc

    def _verify_terminal_failure(
        self, directory: int, intent: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if (
            result.get("contract") != CONTRACT
            or result.get("operation_id") != intent["operation_id"]
            or result.get("intent_sha256") != _digest(intent)
            or result.get("status")
            not in {"unsupported_input", "engine_failed", "resource_rejected"}
            or not isinstance(result.get("outcome_code"), str)
            or _CODE.fullmatch(result["outcome_code"]) is None
        ):
            raise MediaIntegrityError("Terminal failure binding is malformed")
        _, member = _read_member(directory, "original.bin", SOURCE_BYTES)
        if (
            member != result.get("original")
            or member["sha256"] != intent["source"]["sha256"]
            or member["size_bytes"] != intent["source"]["size_bytes"]
        ):
            raise MediaIntegrityError("Failed operation original integrity differs")

    def read_verified(self, reference: MediaOcrReference) -> VerifiedMediaOcrEvidence:
        if (
            not isinstance(reference, MediaOcrReference)
            or _OPERATION.fullmatch(reference.operation_id) is None
            or any(
                not isinstance(value, str) or _HASH.fullmatch(value) is None
                for key, value in asdict(reference).items()
                if key != "operation_id"
            )
        ):
            raise ReceiptMediaError("Media reference is malformed")
        root = self._open_root(1)
        directory = -1
        try:
            publication.acquire_storage_root_lock(
                root, deadline=time.monotonic() + NORMALIZATION_SECONDS, clock=time.monotonic
            )
            directory = _open_operation(root, reference.operation_id)
            result = self._read_verified(directory, reference)
            publication.assert_storage_root_identity(root)
            return result
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise MediaIntegrityError(
                "A required exact media bundle member is missing or unsafe"
            ) from exc
        finally:
            if directory >= 0:
                os.close(directory)
            publication.release_storage_root_lock(root)
            root.close()

    def _read_verified(
        self, directory: int, reference: MediaOcrReference
    ) -> VerifiedMediaOcrEvidence:
        intent_raw, intent_member = _read_member(directory, "intent.json", _MAX_JSON)
        terminal_raw, terminal_member = _read_member(directory, "result.json", _MAX_JSON)
        intent, result = _checked_json(intent_raw), _checked_json(terminal_raw)
        if (
            intent.get("contract") != CONTRACT
            or result.get("contract") != CONTRACT
            or intent.get("operation_id") != reference.operation_id
            or result.get("operation_id") != reference.operation_id
            or result.get("status") != "succeeded"
            or result.get("intent_sha256") != _digest(intent)
            or self._reference(intent, result, terminal_member["sha256"]) != reference
        ):
            raise MediaIntegrityError(
                "Media reference does not bind the exact successful operation"
            )
        if (
            intent.get("decoder_parameters") != _DECODER_PARAMETERS
            or intent.get("bounds") != _BOUNDS
        ):
            raise MediaIntegrityError("Media reference uses an unknown normalization policy")
        declaration_raw, declaration_member = _read_member(directory, "declaration.json", 4096)
        if _checked_json(declaration_raw) != {
            key: intent["source"][key] for key in ("declared_mime_type", "original_filename")
        }:
            raise MediaIntegrityError("Decoder declaration differs from received-source evidence")
        original_raw, original = _read_member(directory, "original.bin", SOURCE_BYTES)
        png_raw, png = _read_member(directory, "normalized.png", PNG_BYTES)
        normalization = result["normalization"]
        self._validate_normalization(normalization.get("metadata"))
        self._verify_png_header(png_raw, normalization["metadata"])
        if (
            original != result["original"]
            or original != normalization["original"]
            or png != normalization["png"]
            or _digest(normalization) != reference.normalization_fingerprint
            or original["sha256"] != intent["source"]["sha256"]
            or original["size_bytes"] != intent["source"]["size_bytes"]
        ):
            raise MediaIntegrityError("Original or exact normalized PNG binding differs")
        if (
            normalization.get("decoder") != intent["decoder"]
            or normalization.get("parameters") != intent["decoder_parameters"]
            or normalization.get("bounds") != intent["bounds"]
            or normalization.get("code") != intent["code"]
        ):
            raise MediaIntegrityError("Normalization identity differs from admitted operation")
        ocr_raw, ocr_member = _read_member(directory, "ocr.json", _MAX_JSON)
        payload = _checked_json(ocr_raw)
        info = result["ocr"]
        fingerprint = _digest(
            {
                "contract": CONTRACT,
                "normalization_fingerprint": reference.normalization_fingerprint,
                "original": original,
                "png": png,
                "engine": intent["ocr_identity"],
                "limits": intent["ocr_limits"],
                "result_sha256": _digest(payload),
                "status": payload["status"],
            }
        )
        if (
            info["fingerprint"] != fingerprint
            or info["identity"] != intent["ocr_identity"]
            or info["limits"] != intent["ocr_limits"]
            or info["member"] != ocr_member
            or info["result_sha256"] != _digest(payload)
            or info["result_sha256"] != reference.ocr_result_sha256
        ):
            raise MediaIntegrityError("OCR input, engine, result or fingerprint differs")
        try:
            limits = ReceiptOcrLimits(**intent["ocr_limits"])
            blocks = tuple(ReceiptOcrBlock(**block) for block in payload["blocks"])
            actual = ocr.ReceiptOcrEngineResult(
                ReceiptOcrExtractionStatus(payload["status"]), blocks, payload["outcome_code"]
            )
            outcome = ocr._normalize_engine_result(actual, limits=limits)
            if (
                outcome.status
                not in {ReceiptOcrExtractionStatus.SUCCEEDED, ReceiptOcrExtractionStatus.NO_TEXT}
                or [asdict(block) for block in outcome.blocks] != payload["blocks"]
            ):
                raise MediaIntegrityError(
                    "Successful OCR evidence has contradictory canonical blocks"
                )
        except (KeyError, ValueError, TypeError, ocr.ReceiptOcrError) as exc:
            raise MediaIntegrityError("Canonical OCR evidence is invalid") from exc
        if hashlib.sha256(original_raw).hexdigest() != reference.original_sha256:
            raise MediaIntegrityError("Original hash differs from requested reference")
        inventory = (intent_member, original, declaration_member, png, ocr_member, terminal_member)
        self._temporary_bound(directory)
        return VerifiedMediaOcrEvidence(reference, result, blocks, inventory)


__all__ = [
    "ReceiptMediaProcessor",
    "ReceiptMediaError",
    "MediaIntegrityError",
    "MediaOperationConflictError",
    "MediaUnsupportedPlatformError",
    "MediaOcrReference",
    "MediaProcessingResult",
    "VerifiedMediaOcrEvidence",
]
