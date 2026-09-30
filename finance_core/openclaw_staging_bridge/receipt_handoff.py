"""Receipt-image local handoff validation and durable publication (S2).

The channel plugin downloads the media; the bridge receives it only as a
local handoff file inside the staging workspace handoff directory.  This
module validates the handoff file (size, symlink, JPEG/PNG signature,
extension/MIME consistency), then publishes it through the shared bounded
no-overwrite publication seam extracted from the Telegram attachment
acquisition saga, and finally persists evidence through the existing
``persist_attachment_evidence`` boundary.  The handoff file remains bounded
staging evidence for replay and recovery; this boundary never performs a
per-file final unlink.  No network acquisition, no bot token.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from finance_core.intake import attachment_publication as publication
from finance_core.intake.attachment_evidence import (
    AttachmentEvidenceConflictError,
    AttachmentEvidenceError,
    get_attachment_evidence,
    persist_attachment_evidence,
)
from finance_core.openclaw_staging_bridge import errors
from finance_core.sqlite_connection import (
    DEFAULT_BUSY_TIMEOUT_MILLISECONDS,
    configure_sqlite_connection,
)

MAX_HANDOFF_BYTES = 10_000_000
_HANDOFF_READ_CHUNK = 1_048_576
_PUBLICATION_DEADLINE_SECONDS = 30.0


@dataclass(frozen=True)
class HandoffResult:
    """Durable publication outcome for one handoff file."""

    final_path: str
    observed_file_size: int
    content_hash: str
    mime_type: str
    canonical_extension: str
    durable_file_reused: bool
    persistence_result: dict[str, Any]
    persistence_idempotent: bool


def _handoff_error(code: str, message: str) -> errors.BridgeError:
    return errors.bridge_error(code, message, errors.EXIT_VALIDATION_REFUSED)


def _read_handoff_descriptor(fd: int) -> tuple[bytes, int]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise _handoff_error(errors.HANDOFF_REFUSED, "Handoff file is not a regular file.")
    size = before.st_size
    if size == 0:
        raise _handoff_error(errors.HANDOFF_REFUSED, "Handoff file is empty.")
    if size > MAX_HANDOFF_BYTES:
        raise _handoff_error(errors.HANDOFF_REFUSED, "Handoff file exceeds the bounded size limit.")
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, _HANDOFF_READ_CHUNK))
        if not chunk:
            raise _handoff_error(
                errors.HANDOFF_REFUSED, "Handoff file read ended before its declared size."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    after = os.fstat(fd)
    if (
        len(content) != size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
    ):
        raise _handoff_error(errors.HANDOFF_REFUSED, "Handoff file size changed during read.")
    return content, size


def read_handoff_file(handoff_path: Path) -> tuple[bytes, int]:
    """Read the handoff file safely: O_NOFOLLOW, fstat, bounded loop read."""
    try:
        fd = os.open(str(handoff_path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise errors.bridge_error(
            errors.HANDOFF_NOT_FOUND,
            "Handoff file does not exist in the handoff directory.",
            errors.EXIT_VALIDATION_REFUSED,
        ) from None
    except OSError as exc:
        raise _handoff_error(
            errors.HANDOFF_REFUSED, f"Handoff file cannot be opened safely: {exc}"
        ) from None
    try:
        return _read_handoff_descriptor(fd)
    finally:
        os.close(fd)


def read_handoff_descriptor(fd: int) -> tuple[bytes, int]:
    """Read a caller-inherited regular handoff descriptor without reopening its path."""
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise _handoff_error(errors.HANDOFF_REFUSED, "Handoff descriptor is invalid.")
    try:
        inherited_status = os.fstat(fd)
        if (
            not stat.S_ISREG(inherited_status.st_mode)
            or stat.S_IMODE(inherited_status.st_mode) != 0o600
        ):
            raise _handoff_error(
                errors.HANDOFF_REFUSED,
                "Inherited handoff descriptor must reference a private 0600 regular file.",
            )
        duplicate = os.dup(fd)
    except OSError as exc:
        raise _handoff_error(
            errors.HANDOFF_REFUSED, f"Handoff descriptor cannot be duplicated safely: {exc}"
        ) from None
    try:
        return _read_handoff_descriptor(duplicate)
    finally:
        os.close(duplicate)


def _deadline_error(phase: str) -> errors.BridgeError:
    return errors.bridge_error(
        errors.DEADLINE_EXCEEDED,
        f"Attachment publication deadline expired during {phase}.",
        errors.EXIT_DEADLINE_EXCEEDED,
    )


def _persist_with_owner_budget(
    conn: sqlite3.Connection,
    final_path: str,
    *,
    public_id: str,
    raw_intake_id: int,
    original_filename: str | None,
    declared_mime_type: str | None,
    expected_file_size: int,
    expected_content_hash: str,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None,
    deadline: float,
    owner_deadline_monotonic: float | None,
    clock: Callable[[], float],
    phase: str,
) -> dict[str, Any]:
    """Use the enclosing managed budget for either evidence write transaction."""
    busy_budget_capped = False
    if owner_deadline_monotonic is not None:
        remaining_ms = int((deadline - clock()) * 1_000)
        if remaining_ms < 1:
            raise _deadline_error(phase)
        budget_ms = min(DEFAULT_BUSY_TIMEOUT_MILLISECONDS, remaining_ms)
        configure_sqlite_connection(conn, timeout_seconds=budget_ms / 1_000)
        busy_budget_capped = budget_ms < DEFAULT_BUSY_TIMEOUT_MILLISECONDS
    try:
        return persist_attachment_evidence(
            conn,
            final_path,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            expected_file_size=expected_file_size,
            expected_content_hash=expected_content_hash,
            persistence_effect=persistence_effect,
        )
    except sqlite3.OperationalError as exc:
        if busy_budget_capped and getattr(exc, "sqlite_errorcode", None) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            raise _deadline_error(phase) from exc
        raise


def _translate_publication_error(exc: publication.AttachmentPublicationError) -> errors.BridgeError:
    if isinstance(exc, publication.DurableFileIntegrityConflictError):
        return errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "Pre-existing durable target does not match its content identity.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    if isinstance(exc, publication.ContentSignatureMismatchError):
        return _handoff_error(errors.HANDOFF_REFUSED, str(exc))
    if isinstance(
        exc, (publication.UnsupportedMimeTypeError, publication.UnsupportedFilenameExtensionError)
    ):
        return _handoff_error(errors.HANDOFF_REFUSED, str(exc))
    if isinstance(exc, publication.TemporaryFileCleanupError):
        return errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "Handoff temporary cleanup failed; residue may remain.",
            errors.EXIT_INTERNAL,
        )
    return errors.bridge_error(
        errors.HANDOFF_REFUSED,
        f"Durable publication failed: {exc}",
        errors.EXIT_INTERNAL,
    )


def validate_receipt_handoff_metadata(
    content: bytes,
    *,
    original_filename: str | None,
    declared_mime_type: str | None,
) -> publication.DetectedType:
    """Validate pure source metadata against already-read receipt bytes.

    This function performs no filesystem publication or SQLite work. The
    command handler calls it after its one bounded handoff read and before it
    persists raw intake, so invalid filename or MIME metadata creates no
    partial durable lineage.
    """
    if declared_mime_type is not None and declared_mime_type not in {
        "image/jpeg",
        "image/png",
    }:
        raise _handoff_error(
            errors.HANDOFF_REFUSED,
            "declared_mime_type must be image/jpeg or image/png.",
        )
    try:
        detected = publication.detect_content_type(content[:16], observed_size=len(content))
        if detected.mime_type not in {"image/jpeg", "image/png"}:
            raise publication.ContentSignatureMismatchError(
                "Receipt handoff accepts only JPEG or PNG content."
            )
        publication.validate_content_evidence(
            detected,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
        )
    except publication.AttachmentPublicationError as exc:
        raise _translate_publication_error(exc) from exc
    return detected


def publish_receipt_handoff(
    conn: sqlite3.Connection,
    *,
    workspace: Path,
    handoff_path: Path,
    attachment_evidence_public_id: str,
    raw_intake_id: int,
    original_filename: str | None,
    declared_mime_type: str | None,
    preloaded_content: bytes | None = None,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
    owner_deadline_monotonic: float | None = None,
) -> HandoffResult:
    """Validate, durably publish, and persist one receipt handoff file.

    Replay semantics mirror the acquisition saga: an already-persisted
    evidence row replays through the persisted-row verification path; a
    verified content-addressed orphan (crash between publication and
    persistence) is reused instead of re-publishing.

    ``preloaded_content`` carries the exact bytes the caller already read
    from the handoff file (for the intake content-hash binding); when
    supplied, publication uses those bytes instead of re-reading the file,
    so the bound hash and the published bytes always come from one read.
    """
    storage_root = workspace / "attachments"
    clock = time.monotonic
    deadline = clock() + _PUBLICATION_DEADLINE_SECONDS
    if owner_deadline_monotonic is not None:
        deadline = min(deadline, owner_deadline_monotonic)

    def check_owner_deadline(phase: str) -> None:
        if owner_deadline_monotonic is not None and clock() >= deadline:
            raise _deadline_error(phase)

    root: publication.StorageRootHandle | None = None
    root_locked = False
    temp_name: str | None = None

    def release_managed_replay_lock() -> None:
        nonlocal root, root_locked
        check_owner_deadline("attachment evidence replay")
        if owner_deadline_monotonic is not None:
            assert root is not None and root_locked
            replay_root = root
            root = None
            root_locked = False
            try:
                publication.release_storage_root_lock(replay_root)
            finally:
                # A failed explicit unlock still releases the advisory lock
                # before the service reopens and verifies the durable file.
                replay_root.close()

    try:
        check_owner_deadline("storage-root open")
        root = publication.open_storage_root(storage_root)
        publication.acquire_storage_root_lock(
            root,
            deadline=deadline,
            clock=clock,
            deadline_error_factory=_deadline_error,
        )
        root_locked = True
        check_owner_deadline("handoff verification")

        # Replay path first: an already-persisted evidence row replays from
        # durable truth and never requires the handoff file again.
        existing = get_attachment_evidence(conn, public_id=attachment_evidence_public_id)
        if existing is not None:
            return _replay_persisted_handoff(
                conn,
                root=root,
                existing=existing,
                attachment_evidence_public_id=attachment_evidence_public_id,
                raw_intake_id=raw_intake_id,
                original_filename=original_filename,
                declared_mime_type=declared_mime_type,
                persistence_effect=persistence_effect,
                deadline=deadline,
                owner_deadline_monotonic=owner_deadline_monotonic,
                clock=clock,
                before_persist=release_managed_replay_lock,
            )

        if preloaded_content is not None:
            content = preloaded_content
            observed_size = len(content)
        else:
            check_owner_deadline("handoff read")
            content, observed_size = read_handoff_file(handoff_path)
            check_owner_deadline("handoff read")
        content_hash = hashlib.sha256(content).hexdigest()

        detected = validate_receipt_handoff_metadata(
            content,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
        )

        check_owner_deadline("temporary publication")
        temp_name, temp_fd = publication.create_private_temp(root)
        try:
            offset = 0
            while offset < len(content):
                check_owner_deadline("temporary publication")
                chunk_end = (
                    min(offset + 1_048_576, len(content))
                    if owner_deadline_monotonic is not None
                    else len(content)
                )
                written = os.write(temp_fd, content[offset:chunk_end])
                if written <= 0:
                    raise publication.TemporaryFileError(
                        "Handoff temporary write returned no progress."
                    )
                offset += written
            check_owner_deadline("temporary sync")
            os.fsync(temp_fd)
            os.fchmod(temp_fd, 0o400)
            os.fsync(temp_fd)
            check_owner_deadline("durable publication")
        except OSError as exc:
            raise publication.TemporaryFileError(f"Handoff temporary write failed: {exc}") from exc
        finally:
            os.close(temp_fd)

        final_path, reused = publication.publish_no_overwrite(
            root,
            temp_name=temp_name,
            observed_size=observed_size,
            content_hash=content_hash,
            detected=detected,
            deadline=deadline,
            clock=clock,
            deadline_error_factory=_deadline_error,
        )
        cleanup_name = temp_name
        temp_name = None
        publication.cleanup_temp(root, cleanup_name)
    except publication.AttachmentPublicationError as exc:
        if temp_name is not None and root is not None:
            try:
                publication.cleanup_after_failure(root, temp_name, exc)
            except BaseExceptionGroup:
                pass
        raise _translate_publication_error(exc) from exc
    finally:
        if root is not None:
            if root_locked:
                publication.release_storage_root_lock(root)
            root.close()

    try:
        persistence_result = _persist_with_owner_budget(
            conn,
            final_path,
            public_id=attachment_evidence_public_id,
            raw_intake_id=raw_intake_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            expected_file_size=observed_size,
            expected_content_hash=content_hash,
            persistence_effect=persistence_effect,
            deadline=deadline,
            owner_deadline_monotonic=owner_deadline_monotonic,
            clock=clock,
            phase="attachment evidence persistence",
        )
    except AttachmentEvidenceConflictError as exc:
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Attachment evidence public ID conflicts with the replayed handoff command.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc
    except AttachmentEvidenceError as exc:
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            f"Attachment evidence persistence failed: {exc}",
            errors.EXIT_INTERNAL,
        ) from exc
    return HandoffResult(
        final_path=final_path,
        observed_file_size=observed_size,
        content_hash=content_hash,
        mime_type=detected.mime_type,
        canonical_extension=detected.extension,
        durable_file_reused=reused,
        persistence_result=dict(persistence_result),
        persistence_idempotent=bool(persistence_result["idempotent"]),
    )


def _replay_persisted_handoff(
    conn: sqlite3.Connection,
    *,
    root: publication.StorageRootHandle,
    existing: dict[str, Any],
    attachment_evidence_public_id: str,
    raw_intake_id: int,
    original_filename: str | None,
    declared_mime_type: str | None,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None,
    deadline: float,
    owner_deadline_monotonic: float | None,
    clock: Callable[[], float],
    before_persist: Callable[[], None],
) -> HandoffResult:
    """Replay through the persisted-row seam: verify durable bytes, re-persist."""
    expected_fields = {
        "raw_intake_record_id": raw_intake_id,
        "original_filename": original_filename,
        "declared_mime_type": declared_mime_type,
    }
    for field, value in expected_fields.items():
        if field == "original_filename" and value is None:
            # A descriptor-bound retained-slot replay may outlive the channel
            # media metadata.  The persisted evidence row is authoritative for
            # this optional display-only name; an explicitly supplied value is
            # still compared exactly below.
            continue
        if existing[field] != value:
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                f"Persisted attachment evidence conflicts with replay field {field}.",
                errors.EXIT_AUTHORITY_REFUSED,
            )

    try:
        stored_path, observed_size, content_hash, detected = (
            publication.verify_persisted_durable_replay(root, existing)
        )
    except publication.DurableContractError as exc:
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "Persisted attachment evidence does not satisfy the durable-storage contract.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc

    before_persist()
    try:
        result = _persist_with_owner_budget(
            conn,
            stored_path,
            public_id=attachment_evidence_public_id,
            raw_intake_id=raw_intake_id,
            original_filename=(
                existing["original_filename"] if original_filename is None else original_filename
            ),
            declared_mime_type=declared_mime_type,
            expected_file_size=observed_size,
            expected_content_hash=content_hash,
            persistence_effect=persistence_effect,
            deadline=deadline,
            owner_deadline_monotonic=owner_deadline_monotonic,
            clock=clock,
            phase="attachment evidence replay",
        )
    except AttachmentEvidenceConflictError as exc:
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Attachment evidence replay refused as a deterministic conflict.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc
    except AttachmentEvidenceError as exc:
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            f"Attachment evidence replay failed: {exc}",
            errors.EXIT_INTERNAL,
        ) from exc

    return HandoffResult(
        final_path=stored_path,
        observed_file_size=observed_size,
        content_hash=content_hash,
        mime_type=detected.mime_type,
        canonical_extension=detected.extension,
        durable_file_reused=True,
        persistence_result=dict(result),
        persistence_idempotent=bool(result["idempotent"]),
    )


__all__ = [
    "MAX_HANDOFF_BYTES",
    "HandoffResult",
    "publish_receipt_handoff",
    "read_handoff_descriptor",
    "read_handoff_file",
    "validate_receipt_handoff_metadata",
]
