"""Telegram attachment evidence persistence boundary.

Persists authoritative attachment evidence for a locally-acquired file,
creating one canonical ``attachments`` row and one append-only
``telegram_attachment_source`` row within a service-owned transaction.

This module does NOT perform network acquisition (see PR #217).

The boundary:
- validates the caller-supplied attachment path and file identity
- computes authoritative SHA-256 full-file content hash using a single
  open handle with stable identity checks (inode, device, size, mtime)
- creates (or reuses) an ``attachments`` row for canonical metadata
- persists Telegram-specific source identity, operation-time observed
  facts, and immutable content-binding evidence
- links raw intake, attachment, and evidence atomically
- protects raw-intake attachment authority from silent replacement
- translates raw SQLite uniqueness races to ``AttachmentEvidenceConflictError``
  and provides deterministic idempotent replay and explicit domain conflicts
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PUBLIC_ID_PREFIX = "tgae_"
_PUBLIC_ID_MAX_LEN = 200

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class AttachmentEvidenceError(ValueError):
    """Base error for attachment evidence persistence failures."""


class AttachmentFileNotFoundError(AttachmentEvidenceError):
    """The specified attachment path does not exist or is not a regular file."""


class AttachmentFileUnreadableError(AttachmentEvidenceError):
    """The attachment file exists but cannot be opened for reading."""


class AttachmentFileChangedDuringHashError(AttachmentEvidenceError):
    """The file changed (size, inode, or mtime) while its hash was being computed."""


class AttachmentEvidenceConflictError(AttachmentEvidenceError):
    """Replayed command material conflicts with a persisted record."""


class AttachmentEvidencePersistenceError(AttachmentEvidenceError):
    """An unexpected database persistence failure occurred.

    Raised when a database integrity, CHECK, FK, trigger, or unexplained
    SQLite failure is not attributable to a known identity conflict.
    The original exception is preserved through exception chaining (``__cause__``).
    """


class InvalidAttachmentIdentityError(AttachmentEvidenceError):
    """Required identity fields are missing or malformed."""


class AttachmentExpectedSizeMismatchError(AttachmentEvidenceError):
    """Declared expected_file_size does not match the observed file size."""


class AttachmentExpectedHashMismatchError(AttachmentEvidenceError):
    """Declared expected_content_hash does not match the computed hash."""


class AttachmentRawIntakeConflictError(AttachmentEvidenceConflictError):
    """The raw-intake record is already linked to a conflicting attachment."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Failure-injection seam --- tests only; never set in production.
# ---------------------------------------------------------------------------

_commit_failure_injection: str | None = None
"""When non-None, ``persist_attachment_evidence`` raises
``sqlite3.OperationalError`` with this message immediately before
``conn.commit()``, after all writes have been submitted to the
transaction.  Used by ``TestRealAtomicRollback.test_rollback_before_commit``.
"""


def _check_injected_commit_failure() -> None:
    if _commit_failure_injection is not None:
        raise sqlite3.OperationalError(_commit_failure_injection)


def persist_attachment_evidence(
    conn: sqlite3.Connection,
    attachment_path: str | Path,
    *,
    public_id: str,
    raw_intake_id: int,
    telegram_file_id: str | None = None,
    telegram_file_unique_id: str | None = None,
    original_filename: str | None = None,
    declared_mime_type: str | None = None,
    expected_file_size: int | None = None,
    expected_content_hash: str | None = None,
) -> dict[str, Any]:
    """Persist attachment evidence for an already-acquired local file.

    Creates (or reuses) one authoritative ``attachments`` row and one
    append-only ``telegram_attachment_source`` row, linked to
    *raw_intake_id*, within a service-owned ``BEGIN IMMEDIATE``.

    *public_id* is a **mandatory** caller-owned operation identity that
    must follow the ``tgae_`` prefix convention.

    Same-source replay (same *public_id* + same canonical command
    material) returns the original persisted result with
    ``idempotent=True``.

    Conflicting command material under the same *public_id* raises
    ``AttachmentEvidenceConflictError``.
    """
    require_staging_database(conn)

    _path = _resolve_canonical_path(attachment_path)
    original_path = str(attachment_path)

    _validate_public_id(public_id)
    _validate_expected_inputs(expected_file_size, expected_content_hash)

    observed_size, content_hash = _compute_stable_identity(_path)

    _check_expected_identity(observed_size, content_hash, expected_file_size, expected_content_hash)

    _validate_telegram_identity(telegram_file_id, telegram_file_unique_id)
    validate_attachment_identity_metadata(original_filename, declared_mime_type)
    _reject_caller_owned_transaction(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _persist_within_transaction(
            conn,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            original_path=original_path,
            canonical_path=str(_path),
            observed_file_size=observed_size,
            content_hash=content_hash,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
        )
        _check_injected_commit_failure()
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Public lookup
# ---------------------------------------------------------------------------


def get_attachment_evidence(
    conn: sqlite3.Connection,
    *,
    evidence_id: int | None = None,
    public_id: str | None = None,
) -> dict[str, Any] | None:
    """Look up a telegram_attachment_source record by id or public_id.

    Read-only; does not start a transaction.
    """
    if evidence_id is not None:
        row = conn.execute(
            """
            SELECT id, public_id, raw_intake_record_id, attachment_id,
                   telegram_file_id, telegram_file_unique_id,
                   original_filename, declared_mime_type,
                   original_attachment_path,
                   observed_file_size, content_hash,
                   source_evidence_payload, created_at
            FROM telegram_attachment_source
            WHERE id = ?
            """,
            (evidence_id,),
        ).fetchone()
    elif public_id is not None:
        row = conn.execute(
            """
            SELECT id, public_id, raw_intake_record_id, attachment_id,
                   telegram_file_id, telegram_file_unique_id,
                   original_filename, declared_mime_type,
                   original_attachment_path,
                   observed_file_size, content_hash,
                   source_evidence_payload, created_at
            FROM telegram_attachment_source
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()
    else:
        raise ValueError("Provide evidence_id or public_id")

    if row is None:
        return None
    return dict(row)


def get_telegram_source_evidence_for_raw_intake(
    conn: sqlite3.Connection,
    raw_intake_record_id: int,
) -> dict[str, Any] | None:
    """Read-only lookup of the earliest telegram source evidence for an intake.

    Returns the evidence row (attachment binding, content hash, durable path,
    filename/MIME declarations) or ``None`` when the intake has no persisted
    attachment evidence yet.  Does not start a transaction.
    """
    row = conn.execute(
        """
        SELECT id, public_id, raw_intake_record_id, attachment_id,
               telegram_file_id, telegram_file_unique_id,
               original_filename, declared_mime_type,
               original_attachment_path,
               observed_file_size, content_hash,
               source_evidence_payload, created_at
        FROM telegram_attachment_source
        WHERE raw_intake_record_id = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (raw_intake_record_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Internal — path resolution


def _check_path_identity(path: Path, stat_result: os.stat_result) -> None:
    """Verify the current path target matches an already-opened handle identity."""
    if not path.exists():
        raise AttachmentFileChangedDuringHashError(
            f"Attachment file no longer exists after hashing: {path}"
        )
    # Open is the only reliable way to get the current inode for the path
    try:
        current_stat = os.stat(str(path))
    except OSError as exc:
        raise AttachmentFileChangedDuringHashError(
            f"Cannot stat attachment file after hashing: {path} ({exc})"
        )
    if current_stat.st_ino != stat_result.st_ino or current_stat.st_dev != stat_result.st_dev:
        raise AttachmentFileChangedDuringHashError("Attachment file was replaced during hashing")


# ---------------------------------------------------------------------------


def _resolve_canonical_path(attachment_path: str | Path) -> Path:
    p = Path(attachment_path)
    if not p.is_absolute():
        raise AttachmentFileNotFoundError(f"Attachment path must be absolute: {attachment_path}")
    if p.is_symlink():
        raise AttachmentFileNotFoundError(
            f"Symlinks are not supported for attachment evidence: {attachment_path}"
        )
    resolved = p.resolve()
    if not resolved.exists():
        raise AttachmentFileNotFoundError(f"Attachment file does not exist: {resolved}")
    if not resolved.is_file():
        raise AttachmentFileNotFoundError(f"Attachment path is not a regular file: {resolved}")
    return resolved


# ---------------------------------------------------------------------------
# Internal — stable file identity (single open handle)
# ---------------------------------------------------------------------------


def _compute_stable_identity(path: Path) -> tuple[int, str]:
    """Compute (size, content_hash) from a single open with stability checks.
    Opens the file once. Reads pre-hash fstat, streams the content
    through SHA-256, then reads post-hash fstat. Also verifies the
    path target matches the opened inode. Raises if the file identity
    changed during the read or if the path was replaced.
    """
    try:
        fh = open(path, "rb")
    except PermissionError:
        raise AttachmentFileUnreadableError(f"Attachment file is not readable: {path}")
    except OSError as exc:
        raise AttachmentFileUnreadableError(f"Cannot open attachment file: {path} ({exc})")

    try:
        pre_stat = os.fstat(fh.fileno())

        if not stat.S_ISREG(pre_stat.st_mode):
            fh.close()
            raise AttachmentFileNotFoundError(
                f"Attachment is not a regular file (mode={pre_stat.st_mode:#o}): {path}"
            )

        sha = hashlib.sha256()
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            sha.update(chunk)

        post_stat = os.fstat(fh.fileno())
    finally:
        fh.close()

    if pre_stat.st_size != post_stat.st_size:
        raise AttachmentFileChangedDuringHashError(
            f"File size changed during hash: {pre_stat.st_size} → {post_stat.st_size}"
        )
    if pre_stat.st_ino != post_stat.st_ino:
        raise AttachmentFileChangedDuringHashError(
            f"File inode changed during hash: {pre_stat.st_ino} → {post_stat.st_ino}"
        )
    if pre_stat.st_dev != post_stat.st_dev:
        raise AttachmentFileChangedDuringHashError(
            f"File device changed during hash: {pre_stat.st_dev} → {post_stat.st_dev}"
        )
    if pre_stat.st_mtime_ns != post_stat.st_mtime_ns:
        raise AttachmentFileChangedDuringHashError("File mtime changed during hash")

    # Verify path target matches the opened handle
    _check_path_identity(path, post_stat)

    return pre_stat.st_size, sha.hexdigest()


# ---------------------------------------------------------------------------
# Internal — validation
# ---------------------------------------------------------------------------


def _validate_public_id(public_id: str) -> None:
    if not isinstance(public_id, str):
        raise InvalidAttachmentIdentityError(
            f"public_id must be a string, got {type(public_id).__name__}"
        )
    if not public_id or not public_id.strip():
        raise InvalidAttachmentIdentityError("public_id must not be empty")
    if len(public_id) > _PUBLIC_ID_MAX_LEN:
        raise InvalidAttachmentIdentityError(
            f"public_id must not exceed {_PUBLIC_ID_MAX_LEN} characters, got {len(public_id)}"
        )
    if not public_id.startswith(_PUBLIC_ID_PREFIX):
        raise InvalidAttachmentIdentityError(f"public_id must start with '{_PUBLIC_ID_PREFIX}'")
    if not public_id.isascii():
        raise InvalidAttachmentIdentityError("public_id must be ASCII")
    if any(c.isspace() for c in public_id):
        raise InvalidAttachmentIdentityError("public_id must not contain whitespace")


def _validate_expected_inputs(
    expected_file_size: int | None,
    expected_content_hash: str | None,
) -> None:
    if expected_file_size is not None and expected_file_size < 0:
        raise InvalidAttachmentIdentityError(
            f"expected_file_size must not be negative, got {expected_file_size}"
        )
    if expected_content_hash is not None:
        if (
            len(expected_content_hash) != 64
            or expected_content_hash != expected_content_hash.lower()
            or any(c not in "0123456789abcdef" for c in expected_content_hash)
        ):
            raise InvalidAttachmentIdentityError(
                "expected_content_hash must be 64 lowercase hex characters"
            )


def _check_expected_identity(
    observed_size: int,
    content_hash: str,
    expected_file_size: int | None,
    expected_content_hash: str | None,
) -> None:
    if expected_file_size is not None and observed_size != expected_file_size:
        raise AttachmentExpectedSizeMismatchError(
            f"Expected file size {expected_file_size}, observed {observed_size}"
        )
    if expected_content_hash is not None and content_hash != expected_content_hash:
        raise AttachmentExpectedHashMismatchError(
            f"Expected content hash {expected_content_hash}, observed {content_hash}"
        )


def _validate_telegram_identity(
    file_id: str | None,
    file_unique_id: str | None,
) -> None:
    if file_id is not None:
        if not isinstance(file_id, str):
            raise InvalidAttachmentIdentityError(
                f"telegram_file_id must be a string, got {type(file_id).__name__}"
            )
        if not file_id.strip():
            raise InvalidAttachmentIdentityError(
                "telegram_file_id must not be empty or whitespace-only"
            )
        if file_id.strip() != file_id:
            raise InvalidAttachmentIdentityError(
                "telegram_file_id must not have leading or trailing whitespace"
            )
    if file_unique_id is not None:
        if not isinstance(file_unique_id, str):
            raise InvalidAttachmentIdentityError(
                f"telegram_file_unique_id must be a string, got {type(file_unique_id).__name__}"
            )
        if not file_unique_id.strip():
            raise InvalidAttachmentIdentityError(
                "telegram_file_unique_id must not be empty or whitespace-only"
            )
        if file_unique_id.strip() != file_unique_id:
            raise InvalidAttachmentIdentityError(
                "telegram_file_unique_id must not have leading or trailing whitespace"
            )


# ---------------------------------------------------------------------------
# Internal — transaction ownership
# ---------------------------------------------------------------------------


def _reject_caller_owned_transaction(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise AttachmentEvidenceError(
            "Attachment evidence persistence requires a connection without pending work"
        )


# ---------------------------------------------------------------------------
# Internal — persistence (inside BEGIN IMMEDIATE)
# ---------------------------------------------------------------------------


def validate_attachment_identity_metadata(
    original_filename: str | None,
    declared_mime_type: str | None,
) -> None:
    """Reject malformed optional attachment identity metadata before persistence.

    The bridge uses this pure check before it creates a raw-intake row. Keep
    the evidence boundary's checks here so a malformed source field cannot
    leave a partial intake lineage merely because it would also be rejected
    later by evidence persistence.
    """
    if original_filename is not None:
        if not isinstance(original_filename, str):
            raise InvalidAttachmentIdentityError(
                f"original_filename must be a string, got {type(original_filename).__name__}"
            )
        if not original_filename.strip():
            raise InvalidAttachmentIdentityError(
                "original_filename must not be empty or whitespace-only"
            )
        if original_filename.strip() != original_filename:
            raise InvalidAttachmentIdentityError(
                "original_filename must not have leading or trailing whitespace"
            )
    if declared_mime_type is not None:
        if not isinstance(declared_mime_type, str):
            raise InvalidAttachmentIdentityError(
                f"declared_mime_type must be a string, got {type(declared_mime_type).__name__}"
            )
        if not declared_mime_type.strip():
            raise InvalidAttachmentIdentityError(
                "declared_mime_type must not be empty or whitespace-only"
            )
        if declared_mime_type.strip() != declared_mime_type:
            raise InvalidAttachmentIdentityError(
                "declared_mime_type must not have leading or trailing whitespace"
            )


# ---------------------------------------------------------------------------
# Internal — narrow insert seam for rollback coverage
# ---------------------------------------------------------------------------


def _insert_telegram_source_row(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    attachment_id: int,
    raw_intake_id: int,
    original_path: str,
    observed_file_size: int,
    content_hash: str,
    telegram_file_id: str | None,
    telegram_file_unique_id: str | None,
    original_filename: str | None,
    declared_mime_type: str | None,
    evidence_payload: str,
    created_at: str,
) -> int:
    """Insert a Telegram attachment source row (narrow seam for testing)."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO telegram_attachment_source (
                public_id,
                attachment_id,
                raw_intake_record_id,
                telegram_file_id,
                telegram_file_unique_id,
                original_filename,
                declared_mime_type,
                original_attachment_path,
                observed_file_size,
                content_hash,
                source_evidence_payload,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                attachment_id,
                raw_intake_id,
                telegram_file_id,
                telegram_file_unique_id,
                original_filename,
                declared_mime_type,
                original_path,
                observed_file_size,
                content_hash,
                evidence_payload,
                created_at,
            ),
        )
    except sqlite3.IntegrityError:
        raise  # let caller handle

    source_id = cursor.lastrowid
    if source_id is None:
        raise RuntimeError("Source insert did not return an id")
    return source_id


def _persist_within_transaction(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    raw_intake_id: int,
    original_path: str,
    canonical_path: str,
    observed_file_size: int,
    content_hash: str,
    telegram_file_id: str | None,
    telegram_file_unique_id: str | None,
    original_filename: str | None,
    declared_mime_type: str | None,
) -> dict[str, Any]:
    """Core persistence inside the service-owned BEGIN IMMEDIATE."""

    _verify_raw_intake_exists(conn, raw_intake_id)

    # Check for idempotent replay
    existing_source = _find_existing_source(conn, public_id)
    if existing_source is not None:
        return _handle_existing(
            conn,
            existing_source,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            original_path=original_path,
            observed_file_size=observed_file_size,
            content_hash=content_hash,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
        )

    # Check for Telegram file unique ID conflict
    if telegram_file_unique_id is not None:
        dup = conn.execute(
            """
            SELECT public_id, content_hash FROM telegram_attachment_source
            WHERE telegram_file_unique_id = ?
            """,
            (telegram_file_unique_id,),
        ).fetchone()
        if dup is not None and dup["content_hash"] != content_hash:
            raise AttachmentEvidenceConflictError(
                f"telegram_file_unique_id {telegram_file_unique_id} "
                f"already bound to content_hash {dup['content_hash']} "
                f"(row {dup['public_id']})"
            )

    # Resolve or create the authoritative attachments row
    attachment_id = _resolve_attachment_row(
        conn,
        original_path=original_path,
        canonical_path=canonical_path,
        content_hash=content_hash,
        original_filename=original_filename,
        declared_mime_type=declared_mime_type,
    )

    # Link raw_intake_records to the attachment — protect authority
    _bind_raw_intake_attachment(
        conn,
        raw_intake_id=raw_intake_id,
        attachment_id=attachment_id,
        original_path=original_path,
        content_hash=content_hash,
    )

    # Insert Telegram source row
    evidence_payload = json.dumps(
        {
            "original_attachment_path": original_path,
            "canonical_path": canonical_path,
            "observed_file_size": observed_file_size,
            "content_hash": content_hash,
            "telegram_file_id": telegram_file_id,
            "telegram_file_unique_id": telegram_file_unique_id,
            "original_filename": original_filename,
            "declared_mime_type": declared_mime_type,
        },
        sort_keys=True,
    )
    created_at = datetime.now(UTC).isoformat()

    try:
        source_id = _insert_telegram_source_row(
            conn,
            public_id=public_id,
            attachment_id=attachment_id,
            raw_intake_id=raw_intake_id,
            original_path=original_path,
            observed_file_size=observed_file_size,
            content_hash=content_hash,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            evidence_payload=evidence_payload,
            created_at=created_at,
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE constraint race — re-read and classify
        return _classify_and_handle_unique_race(
            conn,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            original_path=original_path,
            observed_file_size=observed_file_size,
            content_hash=content_hash,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            attachment_id=attachment_id,
            original_error=exc,
        )

    row = _fetch_source_row(conn, source_id)
    if row is None:
        raise RuntimeError(f"Source row not found after insert: {source_id}")

    return {"id": source_id, "idempotent": False, **row}


# ---------------------------------------------------------------------------
# Internal — resolve / reuse attachments row
# ---------------------------------------------------------------------------


def _resolve_attachment_row(
    conn: sqlite3.Connection,
    *,
    original_path: str,
    canonical_path: str,
    content_hash: str,
    original_filename: str | None,
    declared_mime_type: str | None,
) -> int:
    """Return the id of an existing matching attachment, or create one."""
    existing = conn.execute(
        "SELECT id FROM attachments WHERE file_hash = ? AND file_path = ?",
        (content_hash, original_path),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    mime = declared_mime_type or _guess_mime_from_path(canonical_path)
    att_public_id = f"at_{uuid4().hex[:12]}"
    created_at = datetime.now(UTC).isoformat()

    cursor = conn.execute(
        """
        INSERT INTO attachments (
            public_id, attachment_type, file_path, original_filename,
            mime_type, file_hash, source_channel, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            att_public_id,
            "telegram_attachment",
            original_path,
            original_filename,
            mime,
            content_hash,
            "telegram",
            created_at,
            created_at,
        ),
    )
    return cursor.lastrowid  # type: ignore[return-value]


def _guess_mime_from_path(path: str) -> str | None:
    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return mime


# ---------------------------------------------------------------------------
# Internal — idempotent replay / conflict
# ---------------------------------------------------------------------------


def _find_existing_source(conn: sqlite3.Connection, public_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, public_id, attachment_id, raw_intake_record_id,
               telegram_file_id, telegram_file_unique_id,
               original_filename, declared_mime_type,
               original_attachment_path,
               observed_file_size, content_hash,
               source_evidence_payload, created_at
        FROM telegram_attachment_source
        WHERE public_id = ?
        """,
        (public_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Internal — raw-intake attachment authority
# ---------------------------------------------------------------------------


def _bind_raw_intake_attachment(
    conn: sqlite3.Connection,
    *,
    raw_intake_id: int,
    attachment_id: int,
    original_path: str,
    content_hash: str,
) -> None:
    """Link a raw-intake record to its authoritative attachment atomically."""

    current = conn.execute(
        "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
        "FROM raw_intake_records WHERE id = ?",
        (raw_intake_id,),
    ).fetchone()
    if current is None:
        raise AttachmentEvidenceError(f"Raw intake record not found: {raw_intake_id}")

    cur_att_id = current["attachment_id"]
    cur_path = current["attachment_path"]
    cur_hash = current["attachment_hash"]

    if cur_att_id is None:
        # No attachment_id yet — classify by whether path/hash exist
        if cur_path is None and cur_hash is None:
            # Completely unbound — establish all three atomically
            conn.execute(
                "UPDATE raw_intake_records SET attachment_id = ?,"
                " attachment_path = ?, attachment_hash = ?"
                " WHERE id = ? AND attachment_id IS NULL",
                (attachment_id, original_path, content_hash, raw_intake_id),
            )
        elif cur_path is not None and cur_path != original_path:
            raise AttachmentRawIntakeConflictError(
                f"Raw intake {raw_intake_id} has existing path={cur_path} "
                f"that conflicts with requested path={original_path}"
            )
        elif cur_hash is not None and cur_hash != content_hash:
            raise AttachmentRawIntakeConflictError(
                f"Raw intake {raw_intake_id} has existing hash={cur_hash} "
                f"that conflicts with requested hash={content_hash}"
            )
        elif cur_path == original_path and cur_hash is None:
            # Single-field partial: path matches, hash is NULL → populate hash + attachment_id
            conn.execute(
                "UPDATE raw_intake_records SET attachment_id = ?, attachment_hash = ?"
                " WHERE id = ? AND attachment_id IS NULL"
                "   AND attachment_path = ? AND attachment_hash IS NULL",
                (attachment_id, content_hash, raw_intake_id, original_path),
            )
        elif cur_hash == content_hash and cur_path is None:
            # Single-field partial: hash matches, path is NULL → populate path + attachment_id
            conn.execute(
                "UPDATE raw_intake_records SET attachment_id = ?, attachment_path = ?"
                " WHERE id = ? AND attachment_id IS NULL"
                "   AND attachment_hash = ? AND attachment_path IS NULL",
                (attachment_id, original_path, raw_intake_id, content_hash),
            )
        elif cur_path == original_path and cur_hash == content_hash:
            # Evidence exists and matches — compatible bind
            conn.execute(
                "UPDATE raw_intake_records SET attachment_id = ?"
                " WHERE id = ? AND attachment_id IS NULL"
                "   AND attachment_path = ? AND attachment_hash = ?",
                (attachment_id, raw_intake_id, original_path, content_hash),
            )
        else:
            raise AttachmentRawIntakeConflictError(
                f"Raw intake {raw_intake_id} has existing evidence "
                f"(path={cur_path}, hash={cur_hash}) that conflicts with "
                f"requested (path={original_path}, hash={content_hash})"
            )
    else:
        # Already fully bound — exact match is replay, mismatch is conflict
        if cur_att_id == attachment_id and cur_path == original_path and cur_hash == content_hash:
            pass  # same attachment replay — idempotent
        else:
            raise AttachmentRawIntakeConflictError(
                f"Raw intake {raw_intake_id} already linked to attachment {cur_att_id} "
                f"(path={cur_path}, hash={cur_hash}) — "
                f"cannot bind different attachment {attachment_id} "
                f"(path={original_path}, hash={content_hash})"
            )


# ---------------------------------------------------------------------------
# Internal — UNIQUE race classification
# ---------------------------------------------------------------------------


def _classify_and_handle_unique_race(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    raw_intake_id: int,
    original_path: str,
    observed_file_size: int,
    content_hash: str,
    telegram_file_id: str | None,
    telegram_file_unique_id: str | None,
    original_filename: str | None,
    declared_mime_type: str | None,
    attachment_id: int,
    original_error: sqlite3.IntegrityError,
) -> dict[str, Any]:
    """Re-read after a UNIQUE violation and deterministically replay or conflict."""
    existing_source = conn.execute(
        """
        SELECT id, public_id, attachment_id, raw_intake_record_id,
               telegram_file_id, telegram_file_unique_id,
               original_filename, declared_mime_type,
               original_attachment_path,
               observed_file_size, content_hash,
               source_evidence_payload, created_at
        FROM telegram_attachment_source
        WHERE public_id = ?
        """,
        (public_id,),
    ).fetchone()

    if existing_source is not None:
        existing = dict(existing_source)
        return _handle_existing(
            conn,
            existing,
            public_id=public_id,
            raw_intake_id=raw_intake_id,
            original_path=original_path,
            observed_file_size=observed_file_size,
            content_hash=content_hash,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
        )

    # Check for telegram_file_unique_id conflict
    if telegram_file_unique_id is not None:
        dup = conn.execute(
            """
            SELECT public_id, content_hash FROM telegram_attachment_source
            WHERE telegram_file_unique_id = ?
            """,
            (telegram_file_unique_id,),
        ).fetchone()
        if dup is not None and dup["content_hash"] != content_hash:
            raise AttachmentEvidenceConflictError(
                f"telegram_file_unique_id {telegram_file_unique_id} "
                f"already bound to content_hash {dup['content_hash']} "
                f"(row {dup['public_id']})"
            )
        if dup is not None and dup["content_hash"] == content_hash:
            raise AttachmentEvidenceConflictError(
                f"telegram_file_unique_id {telegram_file_unique_id} "
                f"already exists with same content under public_id {dup['public_id']}"
            )

    # Check for attachment_id+raw_intake_id UNIQUE constraint
    dup2 = conn.execute(
        """
        SELECT public_id FROM telegram_attachment_source
        WHERE attachment_id = ? AND raw_intake_record_id = ?
        """,
        (attachment_id, raw_intake_id),
    ).fetchone()
    if dup2 is not None:
        raise AttachmentEvidenceConflictError(
            f"attachment {attachment_id} already linked to raw intake {raw_intake_id} "
            f"under public_id {dup2['public_id']}"
        )

    # If we still can't determine the cause, re-raise as a persistence error
    raise AttachmentEvidencePersistenceError(
        f"Unexpected persistence failure for public_id {public_id}"
    ) from original_error


def _handle_existing(
    conn: sqlite3.Connection,
    existing: dict[str, Any],
    *,
    public_id: str,
    raw_intake_id: int,
    original_path: str,
    observed_file_size: int,
    content_hash: str,
    telegram_file_id: str | None,
    telegram_file_unique_id: str | None,
    original_filename: str | None,
    declared_mime_type: str | None,
) -> dict[str, Any]:
    """Check whether the replay command matches the persisted record."""

    if existing["raw_intake_record_id"] != raw_intake_id:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different raw_intake_record_id "
            f"({existing['raw_intake_record_id']} vs {raw_intake_id})"
        )
    if existing["content_hash"] != content_hash:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different content_hash "
            f"({existing['content_hash']} vs {content_hash})"
        )
    if existing["observed_file_size"] != observed_file_size:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different observed_file_size "
            f"({existing['observed_file_size']} vs {observed_file_size})"
        )
    if existing["telegram_file_id"] != telegram_file_id:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different telegram_file_id"
        )
    if existing["telegram_file_unique_id"] != telegram_file_unique_id:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different telegram_file_unique_id"
        )
    if existing["original_attachment_path"] != original_path:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different original_attachment_path"
        )
    if existing["original_filename"] != original_filename:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different original_filename"
        )
    if existing["declared_mime_type"] != declared_mime_type:
        raise AttachmentEvidenceConflictError(
            f"public_id {public_id} exists with different declared_mime_type"
        )

    result = dict(existing)
    result["idempotent"] = True
    return result


# ---------------------------------------------------------------------------
# Internal — helpers
# ---------------------------------------------------------------------------


def _verify_raw_intake_exists(conn: sqlite3.Connection, raw_intake_id: int) -> None:
    row = conn.execute("SELECT 1 FROM raw_intake_records WHERE id = ?", (raw_intake_id,)).fetchone()
    if row is None:
        raise AttachmentEvidenceError(f"Raw intake record not found: {raw_intake_id}")


def _fetch_source_row(conn: sqlite3.Connection, source_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT public_id, attachment_id, raw_intake_record_id,
               telegram_file_id, telegram_file_unique_id,
               original_filename, declared_mime_type,
               original_attachment_path,
               observed_file_size, content_hash,
               source_evidence_payload, created_at
        FROM telegram_attachment_source
        WHERE id = ?
        """,
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)
