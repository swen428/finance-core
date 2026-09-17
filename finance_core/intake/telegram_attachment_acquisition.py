"""Bounded Telegram attachment acquisition and durable-storage boundary.

This module validates caller and Telegram identity, streams an explicitly
bounded response into a private temporary file, classifies the minimum content
signature, publishes immutable content-addressed bytes without overwrite, and
then delegates all database writes to ``persist_attachment_evidence``.

Filesystem publication and SQLite persistence intentionally form a crash-safe
saga, not one atomic transaction.  A persistence failure after publication can
leave a verified content-addressed orphan that a later replay may reuse.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from finance_core.intake import attachment_publication as _publication
from finance_core.intake.attachment_evidence import (
    AttachmentEvidenceConflictError,
    AttachmentEvidenceError,
    AttachmentExpectedHashMismatchError,
    AttachmentExpectedSizeMismatchError,
    AttachmentFileChangedDuringHashError,
    AttachmentFileNotFoundError,
    get_attachment_evidence,
    persist_attachment_evidence,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

_PUBLIC_ID_PREFIX = "tgae_"
_PUBLIC_ID_MAX_LENGTH = 200
_TELEGRAM_ID_MAX_LENGTH = 512
_MAX_FILE_BYTES = 100_000_000
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_METADATA_BYTES = 1_048_576
_MAX_CHUNK_BYTES = 1_048_576
_MAX_HEADER_BYTES = 65_536
_MAX_REMOTE_PATH_LENGTH = 4_096
_ORIGINAL_FILENAME_MAX_LENGTH = 1_024
_DECLARED_MIME_TYPE_MAX_LENGTH = 255


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class TelegramAttachmentAcquisitionError(RuntimeError):
    """Base error for bounded Telegram attachment acquisition failures."""


class InvalidAcquisitionConfigurationError(TelegramAttachmentAcquisitionError):
    """Acquisition limits or a required caller argument are invalid."""


class InvalidTelegramIdentityError(TelegramAttachmentAcquisitionError):
    """A Telegram file identity is missing, malformed, or contradictory."""


class StagingDatabaseRejectedError(TelegramAttachmentAcquisitionError):
    """The supplied database is not an authorised staging database."""


class CallerOwnedTransactionError(TelegramAttachmentAcquisitionError):
    """The caller supplied a connection with an active transaction."""


class RawIntakeNotFoundError(TelegramAttachmentAcquisitionError):
    """The required raw-intake record does not exist."""


# The storage, temporary-file, publication, and content-classification errors
# are defined by the shared bounded publication seam.  They are re-declared
# here as multiple-inheritance subclasses so the stable public acquisition
# taxonomy remains a ``TelegramAttachmentAcquisitionError`` family while the
# shared seam keeps its own root for non-Telegram callers.


class UnsafeStorageRootError(
    _publication.UnsafeStorageRootError, TelegramAttachmentAcquisitionError
):
    """The durable-storage root does not satisfy the private-directory contract."""


class TelegramMetadataRequestError(TelegramAttachmentAcquisitionError):
    """Telegram file metadata could not be obtained."""


class MalformedTelegramMetadataError(TelegramAttachmentAcquisitionError):
    """Telegram returned malformed or contradictory file metadata."""


class InvalidRemoteFilePathError(TelegramAttachmentAcquisitionError):
    """Telegram returned an unsafe remote API-relative file path."""


class TelegramHttpResponseError(TelegramAttachmentAcquisitionError):
    """A Telegram HTTP response failed its bounded response contract."""


class TelegramRedirectError(TelegramHttpResponseError):
    """A token-bearing Telegram request attempted to redirect."""


class TelegramDownloadTimeoutError(TelegramAttachmentAcquisitionError):
    """The total monotonic acquisition deadline expired."""


class TelegramDeclaredFileTooLargeError(TelegramAttachmentAcquisitionError):
    """Telegram metadata or a response header declared an oversized file."""


class TelegramStreamedFileTooLargeError(TelegramAttachmentAcquisitionError):
    """Actual streamed bytes exceeded the configured byte limit."""


class TelegramTruncatedDownloadError(TelegramHttpResponseError):
    """The response ended before its valid Content-Length was received."""


class UnsupportedMimeTypeError(
    _publication.UnsupportedMimeTypeError, TelegramAttachmentAcquisitionError
):
    """A declared or HTTP MIME type is outside the supported receipt types."""


class UnsupportedFilenameExtensionError(
    _publication.UnsupportedFilenameExtensionError, TelegramAttachmentAcquisitionError
):
    """The source filename has an unsupported or misleading effective extension."""


class ContentSignatureMismatchError(
    _publication.ContentSignatureMismatchError, TelegramAttachmentAcquisitionError
):
    """Observed content is unsupported or contradicts declared source evidence."""


class TemporaryFileError(_publication.TemporaryFileError, TelegramAttachmentAcquisitionError):
    """Private temporary-file creation, write, flush, or sync failed."""


class TemporaryFileCleanupError(_publication.TemporaryFileCleanupError, TemporaryFileError):
    """Temporary cleanup failed and residue status is explicitly reported."""

    def __init__(self, message: str, *, residue_path: str | None) -> None:
        super().__init__(message, residue_path=residue_path)


class DurablePublicationError(
    _publication.DurablePublicationError, TelegramAttachmentAcquisitionError
):
    """Atomic durable publication or containing-directory sync failed."""


class DurableFileIntegrityConflictError(
    _publication.DurableFileIntegrityConflictError, DurablePublicationError
):
    """A pre-existing durable target does not match its content identity."""


class AcquisitionReplayConflictError(TelegramAttachmentAcquisitionError):
    """A persisted public ID conflicts with the replayed acquisition command."""


class AcquisitionReplayIntegrityError(TelegramAttachmentAcquisitionError):
    """A persisted replay points to missing, changed, or unsupported durable bytes."""


class AttachmentEvidenceHandoffConflictError(TelegramAttachmentAcquisitionError):
    """PR #216 rejected the acquired bytes as a deterministic evidence conflict."""


class UnexpectedAttachmentPersistenceError(TelegramAttachmentAcquisitionError):
    """PR #216 persistence failed for a reason other than a proven identity conflict."""


# Alias of the shared internal exact durable root/shard/file contract error so
# existing catch sites keep their exact spelling.
_DurableContractError = _publication.DurableContractError


# ---------------------------------------------------------------------------
# Public immutable contracts
# ---------------------------------------------------------------------------


def _validate_bounded_int(name: str, value: object, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidAcquisitionConfigurationError(f"{name} must be an integer.")
    if value <= 0 or value > maximum:
        raise InvalidAcquisitionConfigurationError(
            f"{name} must be greater than zero and no greater than {maximum}."
        )


def _validate_bounded_number(name: str, value: object, *, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAcquisitionConfigurationError(f"{name} must be a finite number.")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0 or numeric > maximum:
        raise InvalidAcquisitionConfigurationError(
            f"{name} must be finite, greater than zero, and no greater than {maximum:g}."
        )


@dataclass(frozen=True)
class TelegramAttachmentAcquisitionLimits:
    """Validated conservative limits for one acquisition operation."""

    max_file_bytes: int = 20_000_000
    total_timeout_seconds: float = 30.0
    metadata_response_max_bytes: int = 65_536
    download_chunk_bytes: int = 65_536
    response_headers_max_bytes: int = 16_384
    remote_path_max_length: int = 1_024

    def __post_init__(self) -> None:
        _validate_bounded_int("max_file_bytes", self.max_file_bytes, maximum=_MAX_FILE_BYTES)
        _validate_bounded_number(
            "total_timeout_seconds",
            self.total_timeout_seconds,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        _validate_bounded_int(
            "metadata_response_max_bytes",
            self.metadata_response_max_bytes,
            maximum=_MAX_METADATA_BYTES,
        )
        _validate_bounded_int(
            "download_chunk_bytes",
            self.download_chunk_bytes,
            maximum=_MAX_CHUNK_BYTES,
        )
        _validate_bounded_int(
            "response_headers_max_bytes",
            self.response_headers_max_bytes,
            maximum=_MAX_HEADER_BYTES,
        )
        _validate_bounded_int(
            "remote_path_max_length",
            self.remote_path_max_length,
            maximum=_MAX_REMOTE_PATH_LENGTH,
        )


@dataclass(frozen=True)
class TelegramFileMetadata:
    """Sanitized result of Telegram ``getFile`` metadata resolution."""

    file_path: str
    file_size: int | None = None
    file_id: str | None = None
    file_unique_id: str | None = None


@runtime_checkable
class TelegramDownloadResponse(Protocol):
    """Minimal bounded streaming response exposed by an injected transport."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def read(self, max_bytes: int, *, timeout_seconds: float) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class TelegramAttachmentTransport(Protocol):
    """Replaceable transport protocol used by the public acquisition service."""

    def get_file_metadata(
        self,
        file_id: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_header_bytes: int,
    ) -> TelegramFileMetadata: ...

    def open_file_download(
        self,
        file_path: str,
        *,
        timeout_seconds: float,
        max_header_bytes: int,
    ) -> TelegramDownloadResponse: ...


@dataclass(frozen=True)
class TelegramAttachmentAcquisitionResult:
    """Sanitized acquisition, publication, and PR #216 persistence outcome."""

    attachment_path: str
    observed_file_size: int
    content_hash: str
    detected_mime_type: str
    canonical_extension: str
    network_download_occurred: bool
    durable_file_reused: bool
    persistence_result: Mapping[str, Any]
    persistence_idempotent: bool


# Shared seam aliases: the durable publication machinery lives in the shared
# bounded attachment publication module; this saga keeps its public flow.
_DetectedType = _publication.DetectedType
_StorageRootHandle = _publication.StorageRootHandle
_PDF = _publication.PDF
_JPEG = _publication.JPEG
_PNG = _publication.PNG
_SUPPORTED_MIME_TYPES = _publication.SUPPORTED_MIME_TYPES
_SUPPORTED_EXTENSIONS = _publication.SUPPORTED_EXTENSIONS
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


# Failure injection is private and test-only.  Production callers must never set it.
_failure_injection_hook: Callable[[str], None] | None = None

_lock_registry_guard = threading.Lock()
_operation_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()


def acquire_and_persist_telegram_attachment(
    conn: sqlite3.Connection,
    *,
    transport: TelegramAttachmentTransport,
    storage_root: str | Path,
    public_id: str,
    raw_intake_id: int,
    telegram_file_id: str,
    telegram_file_unique_id: str,
    original_filename: str | None = None,
    declared_mime_type: str | None = None,
    limits: TelegramAttachmentAcquisitionLimits = TelegramAttachmentAcquisitionLimits(),
    _clock: Callable[[], float] = time.monotonic,
) -> TelegramAttachmentAcquisitionResult:
    """Acquire, durably publish, and persist one Telegram attachment.

    The function rejects live/untrusted databases and active caller-owned
    transactions before network or filesystem side effects.  It never holds a
    SQLite transaction while performing network, hashing, or filesystem work.
    """
    _validate_public_arguments(
        public_id=public_id,
        raw_intake_id=raw_intake_id,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        original_filename=original_filename,
        declared_mime_type=declared_mime_type,
        limits=limits,
        clock=_clock,
    )
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise StagingDatabaseRejectedError(
            "Attachment acquisition requires an authorised staging database."
        ) from exc
    if conn.in_transaction:
        raise CallerOwnedTransactionError(
            "Attachment acquisition requires a connection without pending work."
        )
    _require_raw_intake(conn, raw_intake_id)

    operation_lock = _get_operation_lock(public_id)
    with operation_lock:
        root = _open_storage_root(storage_root)
        root_locked = False
        temp_name: str | None = None
        try:
            deadline = _clock() + limits.total_timeout_seconds
            _acquire_storage_root_lock(root, deadline=deadline, clock=_clock)
            root_locked = True
            try:
                existing = get_attachment_evidence(conn, public_id=public_id)
            except sqlite3.Error as exc:
                raise UnexpectedAttachmentPersistenceError(
                    "Unable to inspect persisted attachment evidence before acquisition."
                ) from exc
            if existing is not None:
                return _replay_persisted_acquisition(
                    conn,
                    root=root,
                    existing=existing,
                    public_id=public_id,
                    raw_intake_id=raw_intake_id,
                    telegram_file_id=telegram_file_id,
                    telegram_file_unique_id=telegram_file_unique_id,
                    original_filename=original_filename,
                    declared_mime_type=declared_mime_type,
                )

            temp_name, temp_fd = _create_private_temp(root)
            try:
                _run_injected_failure("after_temporary_file_creation", TemporaryFileError)
                metadata = _get_metadata(
                    transport,
                    telegram_file_id=telegram_file_id,
                    telegram_file_unique_id=telegram_file_unique_id,
                    limits=limits,
                    deadline=deadline,
                    clock=_clock,
                )
                response = _open_download_response(
                    transport,
                    metadata.file_path,
                    limits=limits,
                    deadline=deadline,
                    clock=_clock,
                )
            except BaseException:
                os.close(temp_fd)
                raise
            try:
                observed_size, content_hash, detected = _stream_and_sync(
                    response,
                    temp_fd=temp_fd,
                    metadata=metadata,
                    original_filename=original_filename,
                    declared_mime_type=declared_mime_type,
                    limits=limits,
                    deadline=deadline,
                    clock=_clock,
                )
            except BaseException:
                try:
                    response.close()
                except Exception:
                    pass
                raise
            try:
                response.close()
            except Exception as exc:
                raise TelegramHttpResponseError(
                    "Telegram download response could not be closed cleanly."
                ) from exc

            _remaining_timeout(deadline, _clock, "durable publication")
            _run_injected_failure("before_durable_publication", DurablePublicationError)
            final_path, reused = _publish_no_overwrite(
                root,
                temp_name=temp_name,
                observed_size=observed_size,
                content_hash=content_hash,
                detected=detected,
                deadline=deadline,
                clock=_clock,
            )
            cleanup_name = temp_name
            temp_name = None
            _cleanup_temp(root, cleanup_name)

            _run_injected_failure(
                "before_attachment_evidence_persistence",
                UnexpectedAttachmentPersistenceError,
            )
            persistence_result = _persist_acquired_evidence(
                conn,
                final_path=final_path,
                public_id=public_id,
                raw_intake_id=raw_intake_id,
                telegram_file_id=telegram_file_id,
                telegram_file_unique_id=telegram_file_unique_id,
                original_filename=original_filename,
                declared_mime_type=declared_mime_type,
                observed_size=observed_size,
                content_hash=content_hash,
            )
            return _make_result(
                attachment_path=final_path,
                observed_size=observed_size,
                content_hash=content_hash,
                detected=detected,
                network_download_occurred=True,
                durable_file_reused=reused,
                persistence_result=persistence_result,
            )
        except BaseException as primary_error:
            if temp_name is not None:
                _cleanup_after_failure(root, temp_name, primary_error)
            raise
        finally:
            if root_locked:
                _release_storage_root_lock(root)
            root.close()


# ---------------------------------------------------------------------------
# Validation and replay
# ---------------------------------------------------------------------------


def _validate_public_arguments(
    *,
    public_id: object,
    raw_intake_id: object,
    telegram_file_id: object,
    telegram_file_unique_id: object,
    original_filename: object,
    declared_mime_type: object,
    limits: object,
    clock: object,
) -> None:
    if not isinstance(limits, TelegramAttachmentAcquisitionLimits):
        raise InvalidAcquisitionConfigurationError(
            "limits must be a TelegramAttachmentAcquisitionLimits instance."
        )
    if not callable(clock):
        raise InvalidAcquisitionConfigurationError("The monotonic clock must be callable.")
    if not isinstance(public_id, str):
        raise InvalidAcquisitionConfigurationError("public_id must be a string.")
    if (
        not public_id
        or not public_id.strip()
        or len(public_id) > _PUBLIC_ID_MAX_LENGTH
        or not public_id.startswith(_PUBLIC_ID_PREFIX)
        or not public_id.isascii()
        or any(character.isspace() for character in public_id)
    ):
        raise InvalidAcquisitionConfigurationError(
            "public_id must satisfy the PR #216 tgae_ identity contract."
        )
    if isinstance(raw_intake_id, bool) or not isinstance(raw_intake_id, int):
        raise InvalidAcquisitionConfigurationError("raw_intake_id must be an integer.")
    if raw_intake_id <= 0:
        raise InvalidAcquisitionConfigurationError("raw_intake_id must be positive.")
    _validate_telegram_identity_value("telegram_file_id", telegram_file_id)
    _validate_telegram_identity_value("telegram_file_unique_id", telegram_file_unique_id)
    _validate_optional_source_string("original_filename", original_filename)
    _validate_optional_source_string("declared_mime_type", declared_mime_type)
    if isinstance(original_filename, str):
        _expected_type_from_filename(original_filename)
    if isinstance(declared_mime_type, str):
        _expected_type_from_mime(declared_mime_type)


def _validate_telegram_identity_value(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise InvalidTelegramIdentityError(f"{name} must be a string.")
    if (
        not value
        or value.strip() != value
        or len(value) > _TELEGRAM_ID_MAX_LENGTH
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise InvalidTelegramIdentityError(f"{name} is malformed.")


def _validate_optional_source_string(name: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise InvalidAcquisitionConfigurationError(f"{name} must be a string when present.")
    if not value or value.strip() != value or "\x00" in value:
        raise InvalidAcquisitionConfigurationError(f"{name} is malformed.")
    if any(ord(character) < 0x20 and character not in "\t" for character in value):
        raise InvalidAcquisitionConfigurationError(f"{name} contains control characters.")
    maximum = (
        _ORIGINAL_FILENAME_MAX_LENGTH
        if name == "original_filename"
        else _DECLARED_MIME_TYPE_MAX_LENGTH
    )
    if len(value) > maximum:
        raise InvalidAcquisitionConfigurationError(f"{name} exceeds its length limit.")


def _require_raw_intake(conn: sqlite3.Connection, raw_intake_id: int) -> None:
    try:
        row = conn.execute(
            "SELECT 1 FROM raw_intake_records WHERE id = ?", (raw_intake_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise UnexpectedAttachmentPersistenceError(
            "Unable to verify the required raw-intake record."
        ) from exc
    if row is None:
        raise RawIntakeNotFoundError("The required raw-intake record does not exist.")


def _get_operation_lock(public_id: str) -> threading.Lock:
    with _lock_registry_guard:
        lock = _operation_locks.get(public_id)
        if lock is None:
            lock = threading.Lock()
            _operation_locks[public_id] = lock
        return lock


def _replay_persisted_acquisition(
    conn: sqlite3.Connection,
    *,
    root: _StorageRootHandle,
    existing: Mapping[str, Any],
    public_id: str,
    raw_intake_id: int,
    telegram_file_id: str,
    telegram_file_unique_id: str,
    original_filename: str | None,
    declared_mime_type: str | None,
) -> TelegramAttachmentAcquisitionResult:
    expected = {
        "raw_intake_record_id": raw_intake_id,
        "telegram_file_id": telegram_file_id,
        "telegram_file_unique_id": telegram_file_unique_id,
        "original_filename": original_filename,
        "declared_mime_type": declared_mime_type,
    }
    for field, value in expected.items():
        if existing[field] != value:
            raise AcquisitionReplayConflictError(
                f"Persisted public_id conflicts with replay field {field}."
            )

    try:
        stored_path, observed_size, content_hash, detected = _verify_persisted_durable_replay(
            root,
            existing,
        )
    except _DurableContractError as exc:
        raise AcquisitionReplayIntegrityError(
            "Persisted evidence does not satisfy the PR #217 durable-storage contract."
        ) from exc

    try:
        result = persist_attachment_evidence(
            conn,
            stored_path,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            expected_file_size=observed_size,
            expected_content_hash=content_hash,
        )
    except (
        AttachmentFileNotFoundError,
        AttachmentFileChangedDuringHashError,
        AttachmentExpectedSizeMismatchError,
        AttachmentExpectedHashMismatchError,
    ) as exc:
        raise AcquisitionReplayIntegrityError(
            "Persisted attachment bytes are missing or no longer match their identity."
        ) from exc
    except AttachmentEvidenceConflictError as exc:
        raise AttachmentEvidenceHandoffConflictError(
            "PR #216 rejected the replay as an attachment evidence conflict."
        ) from exc
    except AttachmentEvidenceError as exc:
        raise UnexpectedAttachmentPersistenceError(
            "PR #216 could not revalidate persisted attachment evidence."
        ) from exc
    except Exception as exc:
        raise UnexpectedAttachmentPersistenceError(
            "Unexpected persistence failure while replaying attachment evidence."
        ) from exc

    return _make_result(
        attachment_path=stored_path,
        observed_size=observed_size,
        content_hash=content_hash,
        detected=detected,
        network_download_occurred=False,
        durable_file_reused=True,
        persistence_result=result,
    )


# ---------------------------------------------------------------------------
# Telegram metadata, remote path, response, and streaming controls
# ---------------------------------------------------------------------------


def _get_metadata(
    transport: TelegramAttachmentTransport,
    *,
    telegram_file_id: str,
    telegram_file_unique_id: str,
    limits: TelegramAttachmentAcquisitionLimits,
    deadline: float,
    clock: Callable[[], float],
) -> TelegramFileMetadata:
    remaining = _remaining_timeout(deadline, clock, "Telegram metadata request")
    try:
        metadata = transport.get_file_metadata(
            telegram_file_id,
            timeout_seconds=remaining,
            max_response_bytes=limits.metadata_response_max_bytes,
            max_header_bytes=limits.response_headers_max_bytes,
        )
    except TelegramAttachmentAcquisitionError:
        raise
    except (TimeoutError, OSError) as exc:
        if isinstance(exc, TimeoutError):
            raise TelegramDownloadTimeoutError(
                "Telegram metadata request exceeded the acquisition deadline."
            ) from exc
        raise TelegramMetadataRequestError("Telegram metadata request failed.") from exc
    except Exception as exc:
        raise TelegramMetadataRequestError("Telegram metadata request failed.") from exc
    _remaining_timeout(deadline, clock, "Telegram metadata response")
    if not isinstance(metadata, TelegramFileMetadata):
        raise MalformedTelegramMetadataError(
            "Telegram transport returned an invalid metadata result."
        )
    remote_path = _validate_remote_file_path(
        metadata.file_path, max_length=limits.remote_path_max_length
    )
    if metadata.file_id is not None and metadata.file_id != telegram_file_id:
        raise InvalidTelegramIdentityError("Telegram metadata returned a different file_id.")
    if metadata.file_unique_id is not None and metadata.file_unique_id != telegram_file_unique_id:
        raise InvalidTelegramIdentityError("Telegram metadata returned a different file_unique_id.")
    if metadata.file_size is not None:
        if isinstance(metadata.file_size, bool) or not isinstance(metadata.file_size, int):
            raise MalformedTelegramMetadataError(
                "Telegram metadata file_size must be a non-negative integer."
            )
        if metadata.file_size < 0:
            raise MalformedTelegramMetadataError(
                "Telegram metadata file_size must be a non-negative integer."
            )
        if metadata.file_size > limits.max_file_bytes:
            raise TelegramDeclaredFileTooLargeError(
                "Telegram metadata declares a file above the configured byte limit."
            )
    return TelegramFileMetadata(
        file_path=remote_path,
        file_size=metadata.file_size,
        file_id=metadata.file_id,
        file_unique_id=metadata.file_unique_id,
    )


def _validate_remote_file_path(value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise InvalidRemoteFilePathError("Telegram file_path is missing or too long.")
    if (
        value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or "?" in value
        or "#" in value
        or "%" in value
        or ":" in value
        or "://" in value
        or value.startswith("//")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise InvalidRemoteFilePathError("Telegram file_path is not a safe relative path.")
    components = value.split("/")
    if any(
        component in {"", ".", ".."} or len(component.encode("utf-8")) > 255
        for component in components
    ):
        raise InvalidRemoteFilePathError("Telegram file_path contains an unsafe component.")
    return "/".join(components)


def _open_download_response(
    transport: TelegramAttachmentTransport,
    file_path: str,
    *,
    limits: TelegramAttachmentAcquisitionLimits,
    deadline: float,
    clock: Callable[[], float],
) -> TelegramDownloadResponse:
    remaining = _remaining_timeout(deadline, clock, "Telegram download request")
    try:
        response = transport.open_file_download(
            file_path,
            timeout_seconds=remaining,
            max_header_bytes=limits.response_headers_max_bytes,
        )
    except TelegramAttachmentAcquisitionError:
        raise
    except TimeoutError as exc:
        raise TelegramDownloadTimeoutError(
            "Telegram download request exceeded the acquisition deadline."
        ) from exc
    except Exception as exc:
        raise TelegramHttpResponseError("Telegram download request failed.") from exc
    _remaining_timeout(deadline, clock, "Telegram download response")
    try:
        status_code = response.status_code
    except Exception as exc:
        try:
            response.close()
        except Exception:
            pass
        raise TelegramHttpResponseError(
            "Telegram download response did not expose a valid status."
        ) from exc
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        _close_response_quietly(response)
        raise TelegramHttpResponseError("Telegram download response status is malformed.")
    if 300 <= status_code < 400:
        _close_response_quietly(response)
        raise TelegramRedirectError("Telegram file download redirects are forbidden.")
    if not 200 <= status_code < 300:
        _close_response_quietly(response)
        raise TelegramHttpResponseError(
            f"Telegram file download returned HTTP status {status_code}."
        )
    return response


def _close_response_quietly(response: TelegramDownloadResponse) -> None:
    try:
        response.close()
    except Exception:
        pass


def _stream_and_sync(
    response: TelegramDownloadResponse,
    *,
    temp_fd: int,
    metadata: TelegramFileMetadata,
    original_filename: str | None,
    declared_mime_type: str | None,
    limits: TelegramAttachmentAcquisitionLimits,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[int, str, _DetectedType]:
    try:
        content_length, http_mime = _validated_response_headers(
            response.headers,
            max_header_bytes=limits.response_headers_max_bytes,
            max_file_bytes=limits.max_file_bytes,
        )
    except BaseException:
        os.close(temp_fd)
        raise
    if (
        metadata.file_size is not None
        and content_length is not None
        and metadata.file_size != content_length
    ):
        os.close(temp_fd)
        raise TelegramHttpResponseError(
            "Telegram Content-Length contradicts the resolved metadata size."
        )
    digest = hashlib.sha256()
    prefix = bytearray()
    observed_size = 0
    wrote_first_chunk = False
    try:
        file_handle = os.fdopen(temp_fd, "wb")
    except OSError as exc:
        os.close(temp_fd)
        raise TemporaryFileError("Private temporary file could not be opened.") from exc
    try:
        while True:
            remaining = _remaining_timeout(deadline, clock, "Telegram download chunk")
            read_size = min(
                limits.download_chunk_bytes,
                limits.max_file_bytes - observed_size + 1,
            )
            try:
                chunk = response.read(read_size, timeout_seconds=remaining)
            except TimeoutError as exc:
                raise TelegramDownloadTimeoutError(
                    "Telegram download exceeded the acquisition deadline."
                ) from exc
            except TelegramAttachmentAcquisitionError:
                raise
            except Exception as exc:
                raise TelegramHttpResponseError("Telegram download stream failed.") from exc
            _remaining_timeout(deadline, clock, "Telegram download chunk")
            if not isinstance(chunk, bytes):
                raise TelegramHttpResponseError(
                    "Telegram download stream returned a non-bytes chunk."
                )
            if not chunk:
                break
            next_size = observed_size + len(chunk)
            if next_size > limits.max_file_bytes:
                raise TelegramStreamedFileTooLargeError(
                    "Actual Telegram bytes exceed the configured byte limit."
                )
            if content_length is not None and next_size > content_length:
                raise TelegramHttpResponseError(
                    "Actual Telegram bytes exceed the declared Content-Length."
                )
            if not wrote_first_chunk:
                _run_injected_failure("before_first_temporary_write", TemporaryFileError)
            try:
                written = file_handle.write(chunk)
            except OSError as exc:
                raise TemporaryFileError("Writing the private temporary file failed.") from exc
            if written != len(chunk):
                raise TemporaryFileError("Writing the private temporary file was incomplete.")
            wrote_first_chunk = True
            if len(prefix) < 16:
                prefix.extend(chunk[: 16 - len(prefix)])
            digest.update(chunk)
            observed_size = next_size
            _run_injected_failure("after_temporary_write", TemporaryFileError)

        if content_length is not None and observed_size < content_length:
            raise TelegramTruncatedDownloadError(
                "Telegram download ended before its declared Content-Length."
            )
        detected = _detect_content_type(bytes(prefix), observed_size=observed_size)
        _validate_content_evidence(
            detected,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            http_mime_type=http_mime,
        )
        _run_injected_failure("before_temporary_flush", TemporaryFileError)
        try:
            file_handle.flush()
        except OSError as exc:
            raise TemporaryFileError("Flushing the private temporary file failed.") from exc
        _remaining_timeout(deadline, clock, "temporary file flush")
        _run_injected_failure("before_temporary_fsync", TemporaryFileError)
        try:
            os.fsync(file_handle.fileno())
            os.fchmod(file_handle.fileno(), 0o400)
            os.fsync(file_handle.fileno())
        except OSError as exc:
            raise TemporaryFileError("Synchronizing the private temporary file failed.") from exc
        _remaining_timeout(deadline, clock, "temporary file synchronization")
    finally:
        file_handle.close()
    return observed_size, digest.hexdigest(), detected


def _validated_response_headers(
    headers: Mapping[str, str],
    *,
    max_header_bytes: int,
    max_file_bytes: int,
) -> tuple[int | None, str | None]:
    if not isinstance(headers, Mapping):
        raise TelegramHttpResponseError("Telegram response headers are malformed.")
    total = 0
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TelegramHttpResponseError("Telegram response headers are malformed.")
        total += len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
        if total > max_header_bytes:
            raise TelegramHttpResponseError("Telegram response headers exceed the byte limit.")
        lowered = name.lower()
        if lowered in normalized:
            raise TelegramHttpResponseError("Telegram response contains duplicate headers.")
        normalized[lowered] = value.strip()

    content_length: int | None = None
    if "content-length" in normalized:
        raw_length = normalized["content-length"]
        if not raw_length or not raw_length.isascii() or not raw_length.isdecimal():
            raise TelegramHttpResponseError("Telegram Content-Length is malformed.")
        content_length = int(raw_length)
        if content_length > max_file_bytes:
            raise TelegramDeclaredFileTooLargeError(
                "Telegram Content-Length exceeds the configured byte limit."
            )

    http_mime: str | None = None
    if "content-type" in normalized:
        http_mime = _normalize_mime(normalized["content-type"])
        if http_mime not in {*_SUPPORTED_MIME_TYPES, "application/octet-stream"}:
            raise UnsupportedMimeTypeError("Telegram HTTP Content-Type is unsupported.")
    return content_length, http_mime


def _remaining_timeout(deadline: float, clock: Callable[[], float], phase: str) -> float:
    remaining = deadline - clock()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TelegramDownloadTimeoutError(
            f"Attachment acquisition deadline expired during {phase}."
        )
    return remaining


# ---------------------------------------------------------------------------
# Content classification
# ---------------------------------------------------------------------------


def _normalize_mime(value: str) -> str:
    try:
        return _publication.normalize_mime(value)
    except _publication.UnsupportedMimeTypeError as exc:
        raise _translate(exc) from exc


def _expected_type_from_mime(value: str) -> _DetectedType:
    try:
        return _publication.expected_type_from_mime(value)
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _expected_type_from_filename(value: str) -> _DetectedType | None:
    try:
        return _publication.expected_type_from_filename(value)
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _detect_content_type(prefix: bytes, *, observed_size: int) -> _DetectedType:
    try:
        return _publication.detect_content_type(prefix, observed_size=observed_size)
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _validate_content_evidence(
    detected: _DetectedType,
    *,
    original_filename: str | None,
    declared_mime_type: str | None,
    http_mime_type: str | None,
) -> None:
    try:
        _publication.validate_content_evidence(
            detected,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            http_mime_type=http_mime_type,
        )
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


_PUBLICATION_ERROR_TRANSLATION: tuple[
    tuple[type[_publication.AttachmentPublicationError], type[TelegramAttachmentAcquisitionError]],
    ...,
] = (
    (_publication.DurableFileIntegrityConflictError, DurableFileIntegrityConflictError),
    (_publication.DurablePublicationError, DurablePublicationError),
    (_publication.TemporaryFileCleanupError, TemporaryFileCleanupError),
    (_publication.TemporaryFileError, TemporaryFileError),
    (_publication.UnsafeStorageRootError, UnsafeStorageRootError),
    (_publication.UnsupportedMimeTypeError, UnsupportedMimeTypeError),
    (_publication.UnsupportedFilenameExtensionError, UnsupportedFilenameExtensionError),
    (_publication.ContentSignatureMismatchError, ContentSignatureMismatchError),
)


def _translate(
    exc: _publication.AttachmentPublicationError,
) -> TelegramAttachmentAcquisitionError:
    """Re-express a shared seam error in the stable acquisition taxonomy.

    Errors already in the acquisition family (for example injected failures)
    pass through unchanged.
    """
    if isinstance(exc, TelegramAttachmentAcquisitionError):
        return exc
    if isinstance(exc, _publication.TemporaryFileCleanupError):
        return TemporaryFileCleanupError(str(exc), residue_path=exc.residue_path)
    for shared_type, acquisition_type in _PUBLICATION_ERROR_TRANSLATION:
        if isinstance(exc, shared_type):
            return acquisition_type(str(exc))
    return TelegramAttachmentAcquisitionError(str(exc))


# ---------------------------------------------------------------------------
# Storage root, temporary file, and atomic no-overwrite publication
#
# The mechanics live in the shared bounded attachment publication seam; the
# wrappers below preserve this saga's stable error taxonomy and deadline
# semantics.
# ---------------------------------------------------------------------------


def _deadline_error(phase: str) -> TelegramDownloadTimeoutError:
    return TelegramDownloadTimeoutError(f"Attachment acquisition deadline expired during {phase}.")


def _open_storage_root(storage_root: str | Path) -> _StorageRootHandle:
    try:
        return _publication.open_storage_root(storage_root)
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _assert_storage_root_identity(root: _StorageRootHandle) -> None:
    try:
        _publication.assert_storage_root_identity(root)
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _acquire_storage_root_lock(
    root: _StorageRootHandle,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    """Serialize root publication across processes without a persistent lock file."""
    try:
        _publication.acquire_storage_root_lock(
            root,
            deadline=deadline,
            clock=clock,
            deadline_error_factory=_deadline_error,
        )
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _release_storage_root_lock(root: _StorageRootHandle) -> None:
    _publication.release_storage_root_lock(root)


def _create_private_temp(root: _StorageRootHandle) -> tuple[str, int]:
    try:
        name, fd = _publication.create_private_temp(root, temp_name_prefix=".telegram-acquisition-")
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc
    return name, fd


def _publish_no_overwrite(
    root: _StorageRootHandle,
    *,
    temp_name: str,
    observed_size: int,
    content_hash: str,
    detected: _DetectedType,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[str, bool]:
    try:
        return _publication.publish_no_overwrite(
            root,
            temp_name=temp_name,
            observed_size=observed_size,
            content_hash=content_hash,
            detected=detected,
            deadline=deadline,
            clock=clock,
            deadline_error_factory=_deadline_error,
        )
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _cleanup_temp(root: _StorageRootHandle, temp_name: str) -> None:
    try:
        _publication.cleanup_temp(root, temp_name)
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


def _cleanup_after_failure(
    root: _StorageRootHandle,
    temp_name: str,
    primary_error: BaseException,
) -> None:
    try:
        _publication.cleanup_after_failure(root, temp_name, primary_error)
    except BaseExceptionGroup as group:
        translated = [
            _translate(member)
            if isinstance(member, _publication.AttachmentPublicationError)
            else member
            for member in group.exceptions
        ]
        raise BaseExceptionGroup(
            "Attachment acquisition failed and temporary cleanup also failed.",
            translated,
        ) from None


def _verify_persisted_durable_replay(
    root: _StorageRootHandle,
    existing: Mapping[str, Any],
) -> tuple[str, int, str, _DetectedType]:
    try:
        return _publication.verify_persisted_durable_replay(root, existing)
    except _publication.DurableContractError:
        # Callers translate the exact contract error into the stable replay
        # integrity refusal.
        raise
    except _publication.AttachmentPublicationError as exc:
        raise _translate(exc) from exc


# ---------------------------------------------------------------------------
# PR #216 handoff and results
# ---------------------------------------------------------------------------


def _persist_acquired_evidence(
    conn: sqlite3.Connection,
    *,
    final_path: str,
    public_id: str,
    raw_intake_id: int,
    telegram_file_id: str,
    telegram_file_unique_id: str,
    original_filename: str | None,
    declared_mime_type: str | None,
    observed_size: int,
    content_hash: str,
) -> dict[str, Any]:
    try:
        return persist_attachment_evidence(
            conn,
            final_path,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            expected_file_size=observed_size,
            expected_content_hash=content_hash,
        )
    except AttachmentEvidenceConflictError as exc:
        raise AttachmentEvidenceHandoffConflictError(
            "PR #216 rejected acquired attachment evidence as a deterministic conflict."
        ) from exc
    except AttachmentEvidenceError as exc:
        raise UnexpectedAttachmentPersistenceError(
            "PR #216 could not persist acquired attachment evidence."
        ) from exc
    except Exception as exc:
        raise UnexpectedAttachmentPersistenceError(
            "Unexpected failure while persisting acquired attachment evidence."
        ) from exc


def _make_result(
    *,
    attachment_path: str,
    observed_size: int,
    content_hash: str,
    detected: _DetectedType,
    network_download_occurred: bool,
    durable_file_reused: bool,
    persistence_result: Mapping[str, Any],
) -> TelegramAttachmentAcquisitionResult:
    immutable_persistence = MappingProxyType(dict(persistence_result))
    return TelegramAttachmentAcquisitionResult(
        attachment_path=attachment_path,
        observed_file_size=observed_size,
        content_hash=content_hash,
        detected_mime_type=detected.mime_type,
        canonical_extension=detected.extension,
        network_download_occurred=network_download_occurred,
        durable_file_reused=durable_file_reused,
        persistence_result=immutable_persistence,
        persistence_idempotent=bool(persistence_result["idempotent"]),
    )


def _run_injected_failure(stage: str, error_type: type[TelegramAttachmentAcquisitionError]) -> None:
    if _failure_injection_hook is None:
        return
    try:
        _failure_injection_hook(stage)
    except TelegramAttachmentAcquisitionError:
        raise
    except Exception as exc:
        raise error_type(f"Injected failure at controlled stage {stage}.") from exc


def _proxy_publication_failure(stage: str) -> None:
    """Route shared-seam injection stages through this module's test hook.

    The saga's historical failure-injection stages inside durable publication
    remain controlled by ``telegram_attachment_acquisition._failure_injection_hook``.
    """
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


_publication._failure_injection_hook = _proxy_publication_failure


__all__ = [
    "AcquisitionReplayConflictError",
    "AcquisitionReplayIntegrityError",
    "AttachmentEvidenceHandoffConflictError",
    "CallerOwnedTransactionError",
    "ContentSignatureMismatchError",
    "DurableFileIntegrityConflictError",
    "DurablePublicationError",
    "InvalidAcquisitionConfigurationError",
    "InvalidRemoteFilePathError",
    "InvalidTelegramIdentityError",
    "MalformedTelegramMetadataError",
    "RawIntakeNotFoundError",
    "StagingDatabaseRejectedError",
    "TelegramAttachmentAcquisitionError",
    "TelegramAttachmentAcquisitionLimits",
    "TelegramAttachmentAcquisitionResult",
    "TelegramAttachmentTransport",
    "TelegramDeclaredFileTooLargeError",
    "TelegramDownloadResponse",
    "TelegramDownloadTimeoutError",
    "TelegramFileMetadata",
    "TelegramHttpResponseError",
    "TelegramMetadataRequestError",
    "TelegramRedirectError",
    "TelegramStreamedFileTooLargeError",
    "TelegramTruncatedDownloadError",
    "TemporaryFileCleanupError",
    "TemporaryFileError",
    "UnexpectedAttachmentPersistenceError",
    "UnsafeStorageRootError",
    "UnsupportedFilenameExtensionError",
    "UnsupportedMimeTypeError",
    "acquire_and_persist_telegram_attachment",
]
