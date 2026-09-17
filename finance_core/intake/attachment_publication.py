"""Shared bounded durable attachment publication boundary.

This module is the behavior-preserving extraction of the crash-safe durable
publication seam proven by the Telegram attachment acquisition saga
(``finance_core/intake/telegram_attachment_acquisition.py``).  It owns:

- storage-root validation, identity pinning, and the cross-process advisory
  publication lock;
- private temporary-file creation and cleanup;
- minimum content-signature classification (PDF/JPEG/PNG);
- atomic content-addressed no-overwrite publication with directory fsync;
- durable-target integrity verification and the persisted-row replay path
  that reuses verified content-addressed bytes (including orphans left by a
  crash between publication and database persistence).

Publication and database persistence remain a caller-owned saga: this module
performs no SQLite work.  Callers persist evidence only after publication
succeeds, and a later replay may reuse either the already-persisted durable
bytes or a verified orphan.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT_LOCK_POLL_SECONDS = 0.01


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class AttachmentPublicationError(RuntimeError):
    """Base error for the shared durable attachment publication boundary."""


class UnsafeStorageRootError(AttachmentPublicationError):
    """The durable-storage root does not satisfy the private-directory contract."""


class TemporaryFileError(AttachmentPublicationError):
    """Private temporary-file creation, write, flush, or sync failed."""


class TemporaryFileCleanupError(TemporaryFileError):
    """Temporary cleanup failed and residue status is explicitly reported."""

    def __init__(self, message: str, *, residue_path: str | None) -> None:
        super().__init__(message)
        self.residue_path = residue_path


class DurablePublicationError(AttachmentPublicationError):
    """Atomic durable publication or containing-directory sync failed."""


class DurableFileIntegrityConflictError(DurablePublicationError):
    """A pre-existing durable target does not match its content identity."""


class UnsupportedMimeTypeError(AttachmentPublicationError):
    """A declared MIME type is outside the supported receipt types."""


class UnsupportedFilenameExtensionError(AttachmentPublicationError):
    """The source filename has an unsupported or misleading effective extension."""


class ContentSignatureMismatchError(AttachmentPublicationError):
    """Observed content is unsupported or contradicts declared source evidence."""


class DurableContractError(AttachmentPublicationError):
    """Internal exact durable root/shard/file contract violation."""


# ---------------------------------------------------------------------------
# Content classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedType:
    """One supported minimum content signature."""

    mime_type: str
    extension: str


PDF = DetectedType("application/pdf", ".pdf")
JPEG = DetectedType("image/jpeg", ".jpg")
PNG = DetectedType("image/png", ".png")

SUPPORTED_MIME_TYPES: dict[str, DetectedType] = {
    PDF.mime_type: PDF,
    JPEG.mime_type: JPEG,
    PNG.mime_type: PNG,
}
SUPPORTED_EXTENSIONS: dict[str, DetectedType] = {
    ".pdf": PDF,
    ".jpg": JPEG,
    ".jpeg": JPEG,
    ".png": PNG,
}
_MISLEADING_INNER_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".exe",
        ".com",
        ".scr",
        ".msi",
        ".bat",
        ".cmd",
        ".sh",
        ".js",
        ".html",
        ".htm",
        ".json",
        ".zip",
    }
)


def normalize_mime(value: str) -> str:
    base = value.split(";", 1)[0].strip().lower()
    if not base or "/" not in base or any(character.isspace() for character in base):
        raise UnsupportedMimeTypeError("MIME type is malformed or unsupported.")
    return base


def expected_type_from_mime(value: str) -> DetectedType:
    normalized = normalize_mime(value)
    detected = SUPPORTED_MIME_TYPES.get(normalized)
    if detected is None:
        raise UnsupportedMimeTypeError("Declared MIME type is unsupported.")
    return detected


def expected_type_from_filename(value: str) -> DetectedType | None:
    basename = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffixes = [suffix.lower() for suffix in Path(basename).suffixes]
    if not suffixes:
        return None
    final = suffixes[-1]
    expected = SUPPORTED_EXTENSIONS.get(final)
    if expected is None:
        raise UnsupportedFilenameExtensionError(
            "Source filename has an unsupported effective extension."
        )
    if any(suffix in _MISLEADING_INNER_EXTENSIONS for suffix in suffixes[:-1]):
        raise UnsupportedFilenameExtensionError(
            "Source filename contains a misleading double extension."
        )
    return expected


def detect_content_type(prefix: bytes, *, observed_size: int) -> DetectedType:
    if observed_size == 0:
        raise ContentSignatureMismatchError("Empty attachment content is unsupported.")
    if prefix.startswith(b"%PDF-"):
        return PDF
    if prefix.startswith(b"\xff\xd8\xff"):
        return JPEG
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    raise ContentSignatureMismatchError("Attachment content signature is unsupported.")


def validate_content_evidence(
    detected: DetectedType,
    *,
    original_filename: str | None,
    declared_mime_type: str | None,
    http_mime_type: str | None = None,
) -> None:
    if declared_mime_type is not None:
        declared = expected_type_from_mime(declared_mime_type)
        if declared != detected:
            raise ContentSignatureMismatchError(
                "Declared MIME type conflicts with the observed content signature."
            )
    if original_filename is not None:
        filename_type = expected_type_from_filename(original_filename)
        if filename_type is not None and filename_type != detected:
            raise ContentSignatureMismatchError(
                "Source filename extension conflicts with the observed content signature."
            )
    if http_mime_type not in {None, "application/octet-stream", detected.mime_type}:
        raise ContentSignatureMismatchError(
            "Declared HTTP Content-Type conflicts with the observed content signature."
        )


# ---------------------------------------------------------------------------
# Failure injection seam --- tests only; never set in production.
# ---------------------------------------------------------------------------

_failure_injection_hook: Callable[[str], None] | None = None


def _run_injected_failure(stage: str, error_type: type[AttachmentPublicationError]) -> None:
    if _failure_injection_hook is None:
        return
    try:
        _failure_injection_hook(stage)
    except AttachmentPublicationError:
        raise
    except Exception as exc:
        raise error_type(f"Injected failure at controlled stage {stage}.") from exc


# ---------------------------------------------------------------------------
# Deadline helper
# ---------------------------------------------------------------------------

DeadlineErrorFactory = Callable[[str], Exception]


def _default_deadline_error(phase: str) -> Exception:
    return DurablePublicationError(f"Durable publication deadline expired during {phase}.")


def remaining_timeout(
    deadline: float,
    clock: Callable[[], float],
    phase: str,
    *,
    deadline_error_factory: DeadlineErrorFactory = _default_deadline_error,
) -> float:
    remaining = deadline - clock()
    if not math.isfinite(remaining) or remaining <= 0:
        raise deadline_error_factory(phase)
    return remaining


# ---------------------------------------------------------------------------
# Storage root handle
# ---------------------------------------------------------------------------


@dataclass
class StorageRootHandle:
    """An identity-pinned open descriptor for a validated private storage root."""

    path: Path
    fd: int
    device: int
    inode: int

    def close(self) -> None:
        os.close(self.fd)


def open_storage_root(storage_root: str | Path) -> StorageRootHandle:
    try:
        supplied = Path(storage_root)
    except TypeError as exc:
        raise UnsafeStorageRootError("storage_root must be a filesystem path.") from exc
    if not supplied.is_absolute():
        raise UnsafeStorageRootError("storage_root must be absolute.")
    try:
        entry_stat = os.lstat(supplied)
    except OSError as exc:
        raise UnsafeStorageRootError("storage_root does not exist or is inaccessible.") from exc
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise UnsafeStorageRootError("storage_root must be a direct directory, not a symlink.")
    mode = stat.S_IMODE(entry_stat.st_mode)
    if mode & 0o077:
        raise UnsafeStorageRootError("storage_root must not grant group or other permissions.")
    if hasattr(os, "getuid") and entry_stat.st_uid != os.getuid():
        raise UnsafeStorageRootError("storage_root must be owned by the current user.")
    resolved = supplied.resolve(strict=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(supplied, flags)
    except OSError as exc:
        raise UnsafeStorageRootError("storage_root could not be opened safely.") from exc
    opened_stat = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened_stat.st_mode)
        or opened_stat.st_dev != entry_stat.st_dev
        or opened_stat.st_ino != entry_stat.st_ino
    ):
        os.close(fd)
        raise UnsafeStorageRootError("storage_root identity changed during validation.")
    return StorageRootHandle(
        path=resolved,
        fd=fd,
        device=opened_stat.st_dev,
        inode=opened_stat.st_ino,
    )


def assert_storage_root_identity(root: StorageRootHandle) -> None:
    try:
        current = os.fstat(root.fd)
        current_path = os.lstat(root.path)
    except OSError as exc:
        raise UnsafeStorageRootError("storage_root path became inaccessible.") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != root.device
        or current.st_ino != root.inode
        or stat.S_IMODE(current.st_mode) & 0o077
        or (hasattr(os, "getuid") and current.st_uid != os.getuid())
        or stat.S_ISLNK(current_path.st_mode)
        or not stat.S_ISDIR(current_path.st_mode)
        or current_path.st_dev != root.device
        or current_path.st_ino != root.inode
    ):
        raise UnsafeStorageRootError("storage_root identity changed during acquisition.")


def acquire_storage_root_lock(
    root: StorageRootHandle,
    *,
    deadline: float,
    clock: Callable[[], float],
    deadline_error_factory: DeadlineErrorFactory = _default_deadline_error,
) -> None:
    """Serialize root publication across processes without a persistent lock file."""
    assert_storage_root_identity(root)
    while True:
        try:
            fcntl.flock(root.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            remaining = remaining_timeout(
                deadline,
                clock,
                "storage publication lock",
                deadline_error_factory=deadline_error_factory,
            )
            time.sleep(min(_ROOT_LOCK_POLL_SECONDS, remaining))
        except OSError as exc:
            raise UnsafeStorageRootError(
                "storage_root does not support the required process lock."
            ) from exc
    assert_storage_root_identity(root)


def release_storage_root_lock(root: StorageRootHandle) -> None:
    try:
        fcntl.flock(root.fd, fcntl.LOCK_UN)
    except OSError:
        # Closing the descriptor immediately afterwards releases the advisory
        # lock even when an explicit unlock cannot be observed successfully.
        pass


# ---------------------------------------------------------------------------
# Private temporary files
# ---------------------------------------------------------------------------


def create_private_temp(
    root: StorageRootHandle, *, temp_name_prefix: str = ".attachment-publication-"
) -> tuple[str, int]:
    assert_storage_root_identity(root)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(128):
        name = f"{temp_name_prefix}{secrets.token_hex(16)}"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=root.fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise TemporaryFileError("Private temporary file creation failed.") from exc
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
            os.close(fd)
            primary = TemporaryFileError("Private temporary file permissions are unsafe.")
            cleanup_after_failure(root, name, primary)
            raise primary
        return name, fd
    raise TemporaryFileError("Unable to allocate an exclusive private temporary file.")


def cleanup_temp(root: StorageRootHandle, temp_name: str) -> None:
    residue_path = str(root.path / temp_name)
    try:
        os.unlink(temp_name, dir_fd=root.fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TemporaryFileCleanupError(
            "Private temporary file cleanup failed; residue remains at residue_path.",
            residue_path=residue_path,
        ) from exc
    try:
        os.fsync(root.fd)
    except OSError as exc:
        raise TemporaryFileCleanupError(
            "Private temporary file was unlinked but cleanup durability could not be confirmed.",
            residue_path=None,
        ) from exc


def cleanup_after_failure(
    root: StorageRootHandle,
    temp_name: str,
    primary_error: BaseException,
) -> None:
    try:
        cleanup_temp(root, temp_name)
    except TemporaryFileCleanupError as cleanup_error:
        raise BaseExceptionGroup(
            "Attachment publication failed and temporary cleanup also failed.",
            [primary_error, cleanup_error],
        ) from None


# ---------------------------------------------------------------------------
# Private shards and atomic no-overwrite publication
# ---------------------------------------------------------------------------


def ensure_private_shard(root: StorageRootHandle, shard: str) -> tuple[int, bool]:
    assert_storage_root_identity(root)
    created = False
    try:
        os.mkdir(shard, 0o700, dir_fd=root.fd)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise DurablePublicationError("Durable shard directory creation failed.") from exc
    try:
        shard_fd = open_private_shard(root, shard)
    except DurableContractError as exc:
        raise DurablePublicationError("Durable shard directory is unsafe.") from exc
    return shard_fd, created


def open_private_shard(root: StorageRootHandle, shard: str) -> int:
    assert_storage_root_identity(root)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        shard_fd = os.open(shard, flags, dir_fd=root.fd)
    except OSError as exc:
        raise DurableContractError("Durable shard directory is unsafe.") from exc
    try:
        shard_stat = os.fstat(shard_fd)
    except OSError as exc:
        os.close(shard_fd)
        raise DurableContractError("Durable shard directory is unsafe.") from exc
    if (
        not stat.S_ISDIR(shard_stat.st_mode)
        or shard_stat.st_dev != root.device
        or stat.S_IMODE(shard_stat.st_mode) != 0o700
        or (hasattr(os, "getuid") and shard_stat.st_uid != os.getuid())
    ):
        os.close(shard_fd)
        raise DurableContractError("Durable shard directory is unsafe.")
    try:
        assert_shard_path_identity(root, shard, shard_fd)
    except DurableContractError:
        os.close(shard_fd)
        raise
    return shard_fd


def assert_shard_path_identity(root: StorageRootHandle, shard: str, shard_fd: int) -> None:
    try:
        opened = os.fstat(shard_fd)
        current = os.stat(shard, dir_fd=root.fd, follow_symlinks=False)
    except OSError as exc:
        raise DurableContractError("Durable shard path changed during validation.") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
        or current.st_mode != opened.st_mode
        or current.st_uid != opened.st_uid
    ):
        raise DurableContractError("Durable shard path changed during validation.")


def publish_no_overwrite(
    root: StorageRootHandle,
    *,
    temp_name: str,
    observed_size: int,
    content_hash: str,
    detected: DetectedType,
    deadline: float,
    clock: Callable[[], float],
    deadline_error_factory: DeadlineErrorFactory = _default_deadline_error,
) -> tuple[str, bool]:
    """Atomically publish temp content at its content-addressed path.

    Returns ``(final_path, reused)``.  ``reused`` is True when a verified
    durable target with identical identity already existed (orphan reuse).
    """
    shard = content_hash[:2]
    final_name = f"{content_hash}{detected.extension}"
    final_path = root.path / shard / final_name
    if not final_path.is_relative_to(root.path):
        raise DurablePublicationError("Generated durable path escaped storage_root.")
    shard_fd, shard_created = ensure_private_shard(root, shard)
    try:
        if shard_created:
            try:
                os.fsync(root.fd)
            except OSError as exc:
                raise DurablePublicationError(
                    "Synchronizing the storage root directory failed."
                ) from exc
        remaining_timeout(
            deadline,
            clock,
            "atomic durable publication",
            deadline_error_factory=deadline_error_factory,
        )
        reused = False
        try:
            os.link(
                temp_name,
                final_name,
                src_dir_fd=root.fd,
                dst_dir_fd=shard_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            verify_existing_durable(
                shard_fd,
                final_name=final_name,
                observed_size=observed_size,
                content_hash=content_hash,
                detected=detected,
                expected_device=root.device,
            )
            reused = True
        except OSError as exc:
            raise DurablePublicationError(
                "Atomic no-overwrite durable publication failed."
            ) from exc
        remaining_timeout(
            deadline,
            clock,
            "atomic durable publication",
            deadline_error_factory=deadline_error_factory,
        )
        try:
            os.fsync(shard_fd)
        except OSError as exc:
            raise DurablePublicationError(
                "Synchronizing the durable attachment directory failed."
            ) from exc
        remaining_timeout(
            deadline,
            clock,
            "durable directory synchronization",
            deadline_error_factory=deadline_error_factory,
        )
        _run_injected_failure("after_durable_publication", DurablePublicationError)
        try:
            assert_shard_path_identity(root, shard, shard_fd)
        except DurableContractError as exc:
            raise DurablePublicationError("Durable shard path changed during publication.") from exc
        return str(final_path), reused
    finally:
        os.close(shard_fd)


def verify_existing_durable(
    shard_fd: int,
    *,
    final_name: str,
    observed_size: int,
    content_hash: str,
    detected: DetectedType,
    expected_device: int,
) -> None:
    try:
        observed_type = verify_durable_file(
            shard_fd,
            final_name=final_name,
            observed_size=observed_size,
            content_hash=content_hash,
            expected_device=expected_device,
        )
    except DurableContractError as exc:
        raise DurableFileIntegrityConflictError(
            "Pre-existing durable target does not satisfy its content identity."
        ) from exc
    if observed_type != detected:
        raise DurableFileIntegrityConflictError(
            "Pre-existing durable target does not match its content type."
        )


def verify_durable_file(
    shard_fd: int,
    *,
    final_name: str,
    observed_size: int,
    content_hash: str,
    expected_device: int,
) -> DetectedType:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(final_name, flags, dir_fd=shard_fd)
    except OSError as exc:
        raise DurableContractError("Durable target is not a safe regular file.") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != expected_device
            or before.st_size != observed_size
            or stat.S_IMODE(before.st_mode) != 0o400
            or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        ):
            raise DurableContractError(
                "Durable target has conflicting identity, ownership, or permissions."
            )
        digest = hashlib.sha256()
        prefix = bytearray()
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            if len(prefix) < 16:
                prefix.extend(chunk[: 16 - len(prefix)])
            digest.update(chunk)
        after = os.fstat(fd)
        try:
            path_after = os.stat(final_name, dir_fd=shard_fd, follow_symlinks=False)
        except OSError as exc:
            raise DurableContractError(
                "Durable target path changed during identity verification."
            ) from exc
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_uid != after.st_uid
            or digest.hexdigest() != content_hash
            or path_after.st_dev != after.st_dev
            or path_after.st_ino != after.st_ino
            or path_after.st_mode != after.st_mode
            or path_after.st_size != after.st_size
            or path_after.st_mtime_ns != after.st_mtime_ns
            or path_after.st_uid != after.st_uid
        ):
            raise DurableContractError(
                "Durable target changed or does not match its content identity."
            )
        try:
            return detect_content_type(bytes(prefix), observed_size=after.st_size)
        except AttachmentPublicationError as exc:
            raise DurableContractError(
                "Durable target does not satisfy the supported signature contract."
            ) from exc
    except DurableContractError:
        raise
    except OSError as exc:
        raise DurableContractError("Durable target could not be verified safely.") from exc
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Persisted-row replay path
# ---------------------------------------------------------------------------


def verify_persisted_durable_replay(
    root: StorageRootHandle,
    existing: Mapping[str, Any],
) -> tuple[str, int, str, DetectedType]:
    """Verify a persisted evidence row's durable bytes and return its identity.

    Returns ``(stored_path, observed_size, content_hash, detected_type)``.
    """
    assert_storage_root_identity(root)
    stored_value = existing.get("original_attachment_path")
    observed_value = existing.get("observed_file_size")
    hash_value = existing.get("content_hash")
    if not isinstance(stored_value, str) or not stored_value:
        raise DurableContractError("Persisted durable path is malformed.")
    if (
        isinstance(observed_value, bool)
        or not isinstance(observed_value, int)
        or observed_value < 0
    ):
        raise DurableContractError("Persisted durable size is malformed.")
    if (
        not isinstance(hash_value, str)
        or len(hash_value) != 64
        or hash_value != hash_value.lower()
        or any(character not in "0123456789abcdef" for character in hash_value)
    ):
        raise DurableContractError("Persisted durable hash is malformed.")

    stored_path = Path(stored_value)
    shard_name = hash_value[:2]
    expected_parent = root.path / shard_name
    if (
        not stored_path.is_absolute()
        or stored_value != str(stored_path)
        or stored_path.parent != expected_parent
        or stored_path.name in {"", ".", ".."}
    ):
        raise DurableContractError(
            "Persisted durable path is outside the expected content-addressed shard."
        )

    shard_fd = open_private_shard(root, shard_name)
    try:
        detected = verify_durable_file(
            shard_fd,
            final_name=stored_path.name,
            observed_size=observed_value,
            content_hash=hash_value,
            expected_device=root.device,
        )
        assert_shard_path_identity(root, shard_name, shard_fd)
    finally:
        os.close(shard_fd)

    expected_path = root.path / shard_name / f"{hash_value}{detected.extension}"
    if stored_value != str(expected_path):
        raise DurableContractError(
            "Persisted durable path is not the canonical content-addressed path."
        )
    return stored_value, observed_value, hash_value, detected


__all__ = [
    "AttachmentPublicationError",
    "ContentSignatureMismatchError",
    "DeadlineErrorFactory",
    "DetectedType",
    "DurableContractError",
    "DurableFileIntegrityConflictError",
    "DurablePublicationError",
    "JPEG",
    "PDF",
    "PNG",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_MIME_TYPES",
    "StorageRootHandle",
    "TemporaryFileCleanupError",
    "TemporaryFileError",
    "UnsafeStorageRootError",
    "UnsupportedFilenameExtensionError",
    "UnsupportedMimeTypeError",
    "acquire_storage_root_lock",
    "assert_shard_path_identity",
    "assert_storage_root_identity",
    "cleanup_after_failure",
    "cleanup_temp",
    "create_private_temp",
    "detect_content_type",
    "ensure_private_shard",
    "expected_type_from_filename",
    "expected_type_from_mime",
    "normalize_mime",
    "open_private_shard",
    "open_storage_root",
    "publish_no_overwrite",
    "release_storage_root_lock",
    "remaining_timeout",
    "validate_content_evidence",
    "verify_durable_file",
    "verify_existing_durable",
    "verify_persisted_durable_replay",
]
