"""B5.1b real local receipt intake (staging-only).

Accepts real local JPEG/PNG receipt images from an external source path,
safely copies them into the staging workspace, persists truthful local
source evidence, invokes the macOS Vision OCR boundary, and produces a
total-level expense proposal that stops at ``parsed_pending_confirmation``.

This module does NOT confirm, convert, finalize, or chain stages.  Each
lifecycle stage is a separate explicit human CLI action.

Authority boundary: this module never creates final financial facts, never
accesses ``database/finance.db``, and never bypasses the staging guard.

See ``docs/design/b5_1b_real_local_intake_v1.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrEngine,
    ReceiptOcrLimits,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.intake.receipt_ocr_proposal import (
    ReceiptTotalProposalIngestionResult,
    ingest_receipt_ocr_evidence_as_total_expense_proposal,
)
from finance_core.parser_proposals.receipt_facts_conversion import ReceiptFactsConversionCommand
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
)
from finance_core.receipt_staging_runner.models import RunnerInputManifest, RunnerWorkspace
from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SUPPORTED_MIME_BY_MAGIC: list[tuple[bytes, str, str]] = [
    (_JPEG_MAGIC, "image/jpeg", ".jpg"),
    (_PNG_MAGIC, "image/png", ".png"),
]

MAX_IMAGE_BYTES = 20_000_000  # 20 MB

SOURCE_TYPE = "local_image"
SOURCE_CHANNEL = "local_file"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LocalIntakeError(RuntimeError):
    """Base error for the B5.1b local intake boundary."""


class LocalIntakeFileError(LocalIntakeError):
    """The source image file is missing, oversized, symlinked, or not JPEG/PNG."""


class LocalIntakeCopyError(LocalIntakeError):
    """The safe copy into the workspace failed."""


class LocalIntakeEvidenceError(LocalIntakeError):
    """Local attachment evidence persistence failed."""


class LocalIntakePipelineError(LocalIntakeError):
    """A downstream boundary (OCR, proposal) rejected the operation."""


class LocalIntakePersonalOnlyError(LocalIntakeError):
    """The conversion or fact-set command violates personal-only constraints."""


class LocalLineageError(LocalIntakeError):
    """The target proposal/receipt is not a valid B5.1b local-file lineage."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalIntakeResult:
    """Evidence from the intake stage (file -> proposal)."""

    source_image_path: str
    content_hash: str
    file_size: int
    mime_type: str
    workspace_copy_path: str
    raw_intake_id: int
    raw_intake_public_id: str
    local_evidence_public_id: str
    attachment_id: int
    extraction_public_id: str
    ocr_normalized_result_hash: str
    ingestion: ReceiptTotalProposalIngestionResult
    idempotent: bool


# ---------------------------------------------------------------------------
# Safe file import
# ---------------------------------------------------------------------------


def _read_regular_file_no_follow(path: str | Path, *, max_bytes: int) -> bytes:
    """Read a regular file safely with O_NOFOLLOW, fstat, and bounded loop read.

    Guarantees:
    - Opens with O_RDONLY | O_NOFOLLOW (rejects symlinks at open time).
    - fstat verifies direct regular file.
    - Descriptor-based loop read; never Path.read_bytes().
    - Strictly bounded to max_bytes.
    - Returned length must exactly equal fstat().st_size.
    - Short read, over-limit, symlink, directory, non-regular: fail closed.
    """
    import stat as _s

    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LocalIntakeCopyError(
            f"Cannot open {path!r} safely (symlink, missing, or OS error): {exc}"
        ) from None
    try:
        st = os.fstat(fd)
        if not _s.S_ISREG(st.st_mode):
            raise LocalIntakeCopyError(f"{path!r} is not a regular file")
        expected_size = st.st_size
        if expected_size > max_bytes:
            raise LocalIntakeCopyError(f"{path!r} size {expected_size} exceeds bound {max_bytes}")
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1_048_576))
            if not chunk:
                raise LocalIntakeCopyError(
                    f"Short read on {path!r}: expected {expected_size} bytes, "
                    f"got {expected_size - remaining} before EOF"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise LocalIntakeCopyError(
                f"Read length {len(data)} != fstat size {expected_size} for {path!r}"
            )
        return data
    finally:
        os.close(fd)


def import_local_receipt_file(
    source_path: str,
    workspace: RunnerWorkspace,
    public_id_prefix: str,
) -> tuple[Path, bytes, str, int, str]:
    """Safely copy an external image into the staging workspace.

    Returns (workspace_copy_path, content_bytes, mime_type, file_size, ext).

    Security properties:
    - Opens source with O_NOFOLLOW to reject symlinks at open time.
    - Uses fstat on the opened descriptor to verify regular file and size,
      eliminating TOCTOU between is_symlink/stat/read.
    - Bounded descriptor read; fails closed if content exceeds limit.
    - Destination uses a secure temp file + fsync + atomic no-overwrite
      hard-link publish so the final name is never exposed with partial content.
    - Partial os.write is handled in a loop.
    - Replay verifies existing workspace copy with no-follow + hash check.
    - Original source path, inode, permissions, contents are never modified.
    """
    path = Path(source_path)
    if not path.is_absolute():
        raise LocalIntakeFileError(f"Source image path must be absolute: {source_path!r}")

    # Open with O_NOFOLLOW | O_RDONLY to reject symlinks atomically.
    import stat as stat_mod

    try:
        src_fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LocalIntakeFileError(
            f"Cannot open source image (symlink or missing): {exc}"
        ) from None

    try:
        # fstat on the opened descriptor: no TOCTOU.
        st = os.fstat(src_fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise LocalIntakeFileError(f"Source image path is not a regular file: {source_path!r}")
        file_size = st.st_size
        if file_size == 0:
            raise LocalIntakeFileError(f"Source image file is empty: {source_path!r}")
        if file_size > MAX_IMAGE_BYTES:
            raise LocalIntakeFileError(
                f"Source image exceeds maximum size ({file_size} > {MAX_IMAGE_BYTES} bytes)"
            )

        # Bounded descriptor read.
        chunks: list[bytes] = []
        remaining = file_size
        while remaining > 0:
            chunk = os.read(src_fd, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != file_size:
            raise LocalIntakeFileError(
                f"Source file size changed during read (expected {file_size}, got {len(content)})"
            )
    finally:
        os.close(src_fd)

    # Classify MIME by magic bytes.
    mime_type: str | None = None
    ext: str = ""
    for magic, mime, extension in _SUPPORTED_MIME_BY_MAGIC:
        if content[: len(magic)] == magic:
            mime_type = mime
            ext = extension
            break
    if mime_type is None:
        raise LocalIntakeFileError(
            f"Source image is not a supported format (JPEG or PNG): {source_path!r}"
        )

    # Atomic exclusive copy into workspace attachments directory.
    attachments_dir = Path(workspace.attachments_path)
    copy_name = f"lae_{public_id_prefix}{ext}"
    copy_path = attachments_dir / copy_name

    if copy_path.exists():
        # Idempotent replay: verify with bounded no-follow reader + hash.
        replay_content = _read_regular_file_no_follow(copy_path, max_bytes=MAX_IMAGE_BYTES)
        existing_hash = hashlib.sha256(replay_content).hexdigest()
        new_hash = hashlib.sha256(content).hexdigest()
        if existing_hash != new_hash:
            raise LocalIntakeCopyError(
                f"Workspace copy {copy_path} exists with different content hash. "
                "Same logical identity with altered content fails closed."
            )
        return copy_path, content, mime_type, file_size, ext

    # Write to a secure temp file in the same directory, fsync, then
    # atomic no-overwrite hard-link publish via os.link (never os.replace).
    import uuid as _uuid

    tmp_name = f".tmp_{copy_name}_{os.getpid()}_{_uuid.uuid4().hex[:8]}"
    tmp_path = attachments_dir / tmp_name
    try:
        tmp_fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            offset = 0
            while offset < len(content):
                written = os.write(tmp_fd, content[offset:])
                if written == 0:
                    raise LocalIntakeCopyError(
                        "os.write returned 0 during workspace copy; fail closed"
                    )
                offset += written
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        # Set final permissions on temp before publish.
        os.chmod(str(tmp_path), 0o600)

        # Atomic no-overwrite publish: os.link fails if destination exists.
        try:
            os.link(str(tmp_path), str(copy_path))
        except FileExistsError:
            # Destination appeared (race or replay). Verify safely.
            dest_content = _read_regular_file_no_follow(copy_path, max_bytes=MAX_IMAGE_BYTES)
            if hashlib.sha256(dest_content).hexdigest() != hashlib.sha256(content).hexdigest():
                raise LocalIntakeCopyError(
                    f"Workspace copy {copy_path} exists with different content hash. "
                    "Same logical identity with altered content fails closed."
                )
            # Idempotent replay: same hash, safe to proceed.
            return copy_path, content, mime_type, file_size, ext

        # Unlink temp after successful link.
        os.unlink(str(tmp_path))

        # Fsync parent directory for durability.
        try:
            dir_fd = os.open(str(attachments_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # Best-effort on platforms that don't support dir fsync.

    except LocalIntakeCopyError:
        raise
    except OSError as exc:
        raise LocalIntakeCopyError(f"Cannot write workspace copy: {exc}") from None
    finally:
        # Clean up temp file if it still exists (error path).
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass

    # Final verification using bounded no-follow reader.
    ver_content = _read_regular_file_no_follow(copy_path, max_bytes=MAX_IMAGE_BYTES)
    if len(ver_content) != file_size:
        raise LocalIntakeCopyError(
            f"Published workspace copy has wrong size ({len(ver_content)} != {file_size})"
        )
    if hashlib.sha256(ver_content).hexdigest() != hashlib.sha256(content).hexdigest():
        raise LocalIntakeCopyError("Published workspace copy hash mismatch")

    return copy_path, content, mime_type, file_size, ext


# ---------------------------------------------------------------------------
# Local attachment evidence persistence
# ---------------------------------------------------------------------------


def persist_local_attachment_evidence(
    conn: sqlite3.Connection,
    *,
    workspace: RunnerWorkspace,
    raw_intake_id: int,
    attachment_id: int,
    workspace_copy_path: str,
    original_filename: str,
    mime_type: str,
    file_size: int,
    content_hash: str,
    operator_actor_id: str,
    public_id: str,
) -> None:
    """Persist one local_attachment_source evidence row.

    Idempotent: if the public_id already exists with the same content_hash,
    this is a no-op replay.  Different content_hash under the same public_id
    fails closed.
    """
    require_staging_database(conn)

    existing = conn.execute(
        "SELECT content_hash FROM local_attachment_source WHERE public_id = ?",
        (public_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["content_hash"]) != content_hash:
            raise LocalIntakeEvidenceError(
                f"Local evidence {public_id} exists with different content hash"
            )
        return  # Idempotent replay.

    evidence_payload = json.dumps(
        {
            "workspace_copy_path": workspace_copy_path,
            "original_filename": original_filename,
            "content_hash": content_hash,
            "observed_file_size": file_size,
            "mime_type": mime_type,
            "workspace_identity": workspace.workspace_identity,
            "operator_actor_id": operator_actor_id,
        },
        sort_keys=True,
    )
    now = datetime.now(UTC).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO local_attachment_source (
                public_id, attachment_id, raw_intake_record_id,
                original_filename, declared_mime_type, workspace_copy_path,
                observed_file_size, content_hash, workspace_identity,
                operator_actor_id, source_evidence_payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                attachment_id,
                raw_intake_id,
                original_filename,
                mime_type,
                workspace_copy_path,
                file_size,
                content_hash,
                workspace.workspace_identity,
                operator_actor_id,
                evidence_payload,
                now,
            ),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Local-file lineage guard (P1-2)
# ---------------------------------------------------------------------------


def require_local_runner_receipt_proposal(
    conn: sqlite3.Connection,
    proposal_public_id: str,
    *,
    workspace: RunnerWorkspace,
    manifest: RunnerInputManifest,
) -> dict[str, object]:
    """Verify that a proposal is a valid B5.1b local-file receipt proposal.

    Returns the verified lineage row on success; raises LocalLineageError
    on any ambiguity, missing link, or non-local source.

    This is a read-only guard used by all B5.1b CLI stages. It verifies
    the complete identity chain including workspace and operator binding.
    """
    import stat as _stat_mod

    require_staging_database(conn)

    row = conn.execute(
        """
        SELECT
            po.public_id AS proposal_public_id,
            po.source_type AS proposal_source_type,
            po.source_public_id AS proposal_source_public_id,
            po.parse_status,
            ropl.extraction_id,
            roe.attachment_id,
            roe.source_attachment_hash AS extraction_hash,
            a.file_hash AS attachment_hash,
            a.file_path AS attachment_path,
            a.source_channel AS attachment_source_channel,
            ri.id AS raw_intake_id,
            ri.public_id AS raw_intake_public_id,
            ri.attachment_id AS ri_attachment_id,
            ri.source_type,
            ri.source_channel,
            ri.attachment_hash AS intake_hash,
            ls.public_id AS local_source_public_id,
            ls.content_hash AS local_source_hash,
            ls.workspace_copy_path AS local_workspace_path,
            ls.workspace_identity AS local_workspace_identity,
            ls.operator_actor_id AS local_operator_actor_id,
            ls.raw_intake_record_id AS ls_raw_intake_record_id
        FROM parser_outputs AS po
        JOIN receipt_ocr_proposal_links AS ropl
            ON ropl.parser_output_id = po.id
        JOIN receipt_ocr_extractions AS roe
            ON roe.id = ropl.extraction_id
        JOIN attachments AS a
            ON a.id = roe.attachment_id
        JOIN local_attachment_source AS ls
            ON ls.attachment_id = a.id
        JOIN raw_intake_records AS ri
            ON ri.id = ls.raw_intake_record_id
        WHERE po.public_id = ?
        """,
        (proposal_public_id,),
    ).fetchall()

    if len(row) == 0:
        proposal_exists = conn.execute(
            "SELECT 1 FROM parser_outputs WHERE public_id = ?",
            (proposal_public_id,),
        ).fetchone()
        if proposal_exists is None:
            raise LocalLineageError(f"Proposal {proposal_public_id!r} not found")
        raise LocalLineageError(
            f"Proposal {proposal_public_id!r} is not a valid B5.1b local-file "
            "lineage (missing OCR link, extraction, attachment, or local source)"
        )
    if len(row) > 1:
        raise LocalLineageError(
            f"Proposal {proposal_public_id!r} has {len(row)} local lineage rows; "
            "exactly one is required"
        )

    r = row[0]

    # (1) parser_outputs.source_public_id == raw_intake_records.public_id
    if str(r["proposal_source_public_id"] or "") != str(r["raw_intake_public_id"]):
        raise LocalLineageError(
            f"Proposal source_public_id {r['proposal_source_public_id']!r} does not "
            f"match raw intake public_id {r['raw_intake_public_id']!r}"
        )

    # (2) raw_intake_records.attachment_id == receipt_ocr_extractions.attachment_id
    if int(r["ri_attachment_id"] or 0) != int(r["attachment_id"]):
        raise LocalLineageError(
            "Raw intake attachment_id does not match OCR extraction attachment_id"
        )

    # (5) local_attachment_source.raw_intake_record_id == raw_intake_records.id
    if int(r["ls_raw_intake_record_id"]) != int(r["raw_intake_id"]):
        raise LocalLineageError(
            "local_attachment_source.raw_intake_record_id does not match raw intake id"
        )

    # (6) Raw intake source type/channel.
    if r["source_type"] != SOURCE_TYPE:
        raise LocalLineageError(
            f"Raw intake source_type is {r['source_type']!r}, expected {SOURCE_TYPE!r}"
        )
    if r["source_channel"] != SOURCE_CHANNEL:
        raise LocalLineageError(
            f"Raw intake source_channel is {r['source_channel']!r}, expected {SOURCE_CHANNEL!r}"
        )

    # (7) Hash consistency across the chain.
    extraction_hash = str(r["extraction_hash"] or "")
    attachment_hash = str(r["attachment_hash"])
    intake_hash = str(r["intake_hash"] or "")
    local_hash = str(r["local_source_hash"])
    if extraction_hash and extraction_hash != attachment_hash:
        raise LocalLineageError(
            "Extraction source_attachment_hash does not match attachment file_hash"
        )
    if intake_hash and intake_hash != attachment_hash:
        raise LocalLineageError("Raw intake attachment_hash does not match attachment file_hash")
    if local_hash != attachment_hash:
        raise LocalLineageError("local_attachment_source content_hash does not match attachment")

    # (8) attachments.file_path == local_attachment_source.workspace_copy_path
    local_path = str(r["local_workspace_path"])
    if local_path != str(r["attachment_path"]):
        raise LocalLineageError(
            "local_attachment_source workspace_copy_path does not match attachment file_path"
        )

    # (9) Workspace copy is a direct regular file in the current workspace.
    expected_dir = str(Path(workspace.attachments_path).resolve())
    actual_parent = str(Path(local_path).parent.resolve())
    if actual_parent != expected_dir:
        raise LocalLineageError(
            f"Workspace copy path parent {actual_parent!r} does not match "
            f"current workspace attachments directory {expected_dir!r}"
        )
    try:
        wc_fd = os.open(local_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise LocalLineageError(
            f"Workspace copy {local_path!r} cannot be opened (missing or symlink)"
        ) from None
    try:
        wc_st = os.fstat(wc_fd)
        if not _stat_mod.S_ISREG(wc_st.st_mode):
            raise LocalLineageError(f"Workspace copy {local_path!r} is not a regular file")
    finally:
        os.close(wc_fd)

    # (10) local_attachment_source.workspace_identity == workspace.workspace_identity
    if str(r["local_workspace_identity"]) != workspace.workspace_identity:
        raise LocalLineageError(
            f"Local evidence workspace_identity {r['local_workspace_identity']!r} "
            f"does not match current workspace {workspace.workspace_identity!r}"
        )

    # (11) local_attachment_source.operator_actor_id == manifest.operator_actor_id
    if str(r["local_operator_actor_id"]) != manifest.operator_actor_id:
        raise LocalLineageError(
            f"Local evidence operator_actor_id {r['local_operator_actor_id']!r} "
            f"does not match manifest operator {manifest.operator_actor_id!r}"
        )

    # (12) No Telegram source row allowed.
    telegram_row = conn.execute(
        "SELECT 1 FROM telegram_attachment_source WHERE attachment_id = ?",
        (int(r["attachment_id"]),),
    ).fetchone()
    if telegram_row is not None:
        raise LocalLineageError(
            f"Attachment for proposal {proposal_public_id!r} also has a "
            "Telegram source row; dual-source is rejected"
        )

    return dict(r)


def require_local_runner_receipt(
    conn: sqlite3.Connection,
    receipt_public_id: str,
    *,
    workspace: RunnerWorkspace,
    manifest: RunnerInputManifest,
) -> dict[str, object]:
    """Verify that a receipt traces back to a valid local-file conversion.

    Returns the verified lineage row on success.
    """
    require_staging_database(conn)

    receipt = conn.execute(
        "SELECT id, parser_output_id FROM receipts WHERE public_id = ?",
        (receipt_public_id,),
    ).fetchone()
    if receipt is None:
        raise LocalLineageError(f"Receipt {receipt_public_id!r} not found")
    parser_output_id = receipt["parser_output_id"]
    if parser_output_id is None:
        raise LocalLineageError(f"Receipt {receipt_public_id!r} has no source proposal")
    proposal = conn.execute(
        "SELECT public_id FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    ).fetchone()
    if proposal is None:
        raise LocalLineageError(f"Receipt {receipt_public_id!r} references missing parser output")
    return require_local_runner_receipt_proposal(
        conn, str(proposal["public_id"]), workspace=workspace, manifest=manifest
    )


# ---------------------------------------------------------------------------
# Intake stage (Stage A only)
# ---------------------------------------------------------------------------


def run_local_receipt_intake(
    conn: sqlite3.Connection,
    *,
    workspace: RunnerWorkspace,
    manifest: RunnerInputManifest,
    source_image_path: str,
    engine: ReceiptOcrEngine,
    public_id_prefix: str,
    ocr_limits: ReceiptOcrLimits | None = None,
) -> LocalIntakeResult:
    """Run the intake stage: safe copy -> evidence -> OCR -> proposal.

    Stops at ``parsed_pending_confirmation``.  Does NOT confirm, convert,
    prepare, authorize, or finalize.

    Parameters
    ----------
    conn:
        Open staging database connection.
    workspace:
        Verified B5.1a workspace result.
    manifest:
        Validated runner input manifest (provides operator_actor_id).
    source_image_path:
        Absolute path to an external JPEG or PNG file.
    engine:
        A ``ReceiptOcrEngine`` implementation (production: MacOSVisionOcrEngine).
    public_id_prefix:
        Caller-owned prefix for deterministic public IDs.
    ocr_limits:
        Optional OCR resource limits override.
    """
    require_staging_database(conn)
    _validate_public_id_prefix(public_id_prefix)

    operator_actor_id = manifest.operator_actor_id

    # -- 1. Safe file import ----------------------------------------------
    copy_path, content, mime_type, file_size, ext = import_local_receipt_file(
        source_image_path, workspace, public_id_prefix
    )
    content_hash = hashlib.sha256(content).hexdigest()

    # -- 2. Create raw intake record --------------------------------------
    raw_intake_public_id = f"raw_{public_id_prefix}"
    raw_input_text = f"local receipt image: {Path(source_image_path).name}"

    existing_intake = conn.execute(
        "SELECT id FROM raw_intake_records WHERE public_id = ?",
        (raw_intake_public_id,),
    ).fetchone()
    if existing_intake is not None:
        raw_intake_id = int(existing_intake["id"])
    else:
        with conn:
            intake_record = create_raw_intake_record(
                conn,
                raw_input_text,
                source_type=SOURCE_TYPE,
                source_channel=SOURCE_CHANNEL,
                source_metadata={
                    "content_hash": content_hash,
                    "file_size": file_size,
                    "mime_type": mime_type,
                    "operator_actor_id": operator_actor_id,
                    "workspace_identity": workspace.workspace_identity,
                },
                public_id=raw_intake_public_id,
            )
        raw_intake_id = int(intake_record["id"])

    # -- 3. Create attachments row ----------------------------------------
    attachment_id = _resolve_or_create_attachment(
        conn,
        workspace_copy_path=str(copy_path),
        content_hash=content_hash,
        mime_type=mime_type,
        original_filename=Path(source_image_path).name,
    )

    # -- 4. Link raw intake to attachment (before local evidence insert) --
    # Must happen BEFORE local_attachment_source insert because the freeze
    # trigger fires once local source evidence exists.
    conn.execute(
        "UPDATE raw_intake_records SET attachment_id = ?, attachment_path = ?, "
        "attachment_hash = ? WHERE id = ? AND attachment_id IS NULL",
        (attachment_id, str(copy_path), content_hash, raw_intake_id),
    )
    conn.commit()

    # -- 5. Persist local attachment evidence -----------------------------
    local_evidence_public_id = f"lae_{public_id_prefix}"
    persist_local_attachment_evidence(
        conn,
        workspace=workspace,
        raw_intake_id=raw_intake_id,
        attachment_id=attachment_id,
        workspace_copy_path=str(copy_path),
        original_filename=Path(source_image_path).name,
        mime_type=mime_type,
        file_size=file_size,
        content_hash=content_hash,
        operator_actor_id=operator_actor_id,
        public_id=local_evidence_public_id,
    )

    # -- 6. Set workspace copy to mode 0400 (OCR boundary requirement) ----
    os.chmod(str(copy_path), 0o400)

    # -- 7. Extract and persist OCR evidence ------------------------------
    extraction_public_id = f"rocr_{public_id_prefix}"
    limits = ocr_limits or ReceiptOcrLimits()
    try:
        ocr_result = extract_and_persist_receipt_ocr_evidence(
            conn,
            public_id=extraction_public_id,
            attachment_id=attachment_id,
            engine=engine,
            limits=limits,
        )
    except Exception as exc:
        raise LocalIntakePipelineError(f"OCR evidence extraction failed: {exc}") from exc

    # -- 8. Ingest OCR evidence as total-expense proposal -----------------
    proposal_public_id = f"prop_{public_id_prefix}"
    link_public_id = f"ropl_{public_id_prefix}"
    try:
        ingestion = ingest_receipt_ocr_evidence_as_total_expense_proposal(
            conn,
            extraction_public_id=extraction_public_id,
            proposal_public_id=proposal_public_id,
            link_public_id=link_public_id,
        )
    except Exception as exc:
        raise LocalIntakePipelineError(f"Proposal ingestion failed: {exc}") from exc

    return LocalIntakeResult(
        source_image_path=source_image_path,
        content_hash=content_hash,
        file_size=file_size,
        mime_type=mime_type,
        workspace_copy_path=str(copy_path),
        raw_intake_id=raw_intake_id,
        raw_intake_public_id=raw_intake_public_id,
        local_evidence_public_id=local_evidence_public_id,
        attachment_id=attachment_id,
        extraction_public_id=extraction_public_id,
        ocr_normalized_result_hash=ocr_result.normalized_result_hash,
        ingestion=ingestion,
        idempotent=ingestion.idempotent,
    )


# ---------------------------------------------------------------------------
# Personal-only enforcement: conversion stage
# ---------------------------------------------------------------------------


def validate_personal_conversion_command(
    manifest: RunnerInputManifest,
    command: ReceiptFactsConversionCommand,
) -> None:
    """Validate that a conversion command is personal-only (self participant only).

    Called BEFORE convert_confirmed_receipt_proposal_to_facts.

    Checks:
    - authenticated_actor_id == manifest operator
    - actor_type == "human"
    - channel == "local_file"
    - payer == self participant
    - exactly one participant, which is self and included
    """
    self_participant = manifest.self_participant
    self_id = self_participant.public_id
    operator_id = manifest.operator_actor_id

    # Actor validation.
    if command.authenticated_actor_id != operator_id:
        raise LocalIntakePersonalOnlyError(
            f"Conversion authenticated_actor_id must be the manifest operator "
            f"({operator_id!r}), got {command.authenticated_actor_id!r}"
        )
    if command.actor_type != "human":
        raise LocalIntakePersonalOnlyError(
            f"Conversion actor_type must be 'human', got {command.actor_type!r}"
        )
    if command.channel != SOURCE_CHANNEL:
        raise LocalIntakePersonalOnlyError(
            f"Conversion channel must be {SOURCE_CHANNEL!r}, got {command.channel!r}"
        )

    if command.payer_participant_public_id != self_id:
        raise LocalIntakePersonalOnlyError(
            f"Conversion payer must be the self participant ({self_id!r}), "
            f"got {command.payer_participant_public_id!r}"
        )

    participants = list(command.participants)
    if len(participants) != 1:
        raise LocalIntakePersonalOnlyError(
            f"Personal receipt conversion must have exactly one participant, "
            f"got {len(participants)}"
        )
    entry = participants[0]
    if not isinstance(entry, Mapping):
        raise LocalIntakePersonalOnlyError("Conversion participant entry must be a mapping")
    entry_id = entry.get("participant_public_id")
    entry_included = entry.get("is_included")
    if entry_id != self_id:
        raise LocalIntakePersonalOnlyError(
            f"Personal receipt participant must be {self_id!r}, got {entry_id!r}"
        )
    if not entry_included:
        raise LocalIntakePersonalOnlyError("The self participant must be included (is_included=1)")


# ---------------------------------------------------------------------------
# Personal-only enforcement: fact-set stage
# ---------------------------------------------------------------------------


def validate_personal_fact_set_command(
    conn: sqlite3.Connection,
    command: ReceiptItemAllocationFactsCommand,
    manifest: RunnerInputManifest,
) -> None:
    """Validate that a fact-set command is 100% self-allocated.

    Called BEFORE persist_receipt_item_allocation_facts.

    Enforces:
    - authenticated_actor_id == manifest operator, actor_type == human,
      channel == local_file
    - receipt has exactly one participant with is_self=true
    - every item line has exactly one allocation line
    - each allocation has exactly one participant (self)
    - share_amount == line_amount (Decimal exact equality)
    - currency consistency
    - no adjustments
    - no missing/duplicate/extra lines
    - malformed data fails closed (no continue)
    """
    from decimal import Decimal

    from finance_core.money import SignPolicy, money_decimal, validate_amount_for_currency

    require_staging_database(conn)

    # -- Actor validation --------------------------------------------------
    operator_id = manifest.operator_actor_id
    if command.authenticated_actor_id != operator_id:
        raise LocalIntakePersonalOnlyError(
            f"Fact-set authenticated_actor_id must be the manifest operator "
            f"({operator_id!r}), got {command.authenticated_actor_id!r}"
        )
    if command.actor_type != "human":
        raise LocalIntakePersonalOnlyError(
            f"Fact-set actor_type must be 'human', got {command.actor_type!r}"
        )
    if command.channel != SOURCE_CHANNEL:
        raise LocalIntakePersonalOnlyError(
            f"Fact-set channel must be {SOURCE_CHANNEL!r}, got {command.channel!r}"
        )

    # -- Receipt participant check -----------------------------------------
    receipt = conn.execute(
        "SELECT id FROM receipts WHERE public_id = ?",
        (command.receipt_public_id,),
    ).fetchone()
    if receipt is None:
        raise LocalIntakePersonalOnlyError(f"Receipt {command.receipt_public_id!r} not found")
    receipt_id = int(receipt["id"])

    participants = conn.execute(
        """
        SELECT p.public_id, p.is_self
        FROM receipt_participants AS rp
        JOIN participants AS p ON p.id = rp.participant_id
        WHERE rp.receipt_id = ?
        """,
        (receipt_id,),
    ).fetchall()

    if len(participants) != 1:
        raise LocalIntakePersonalOnlyError(
            f"Personal receipt must have exactly one participant, got {len(participants)}"
        )
    if not participants[0]["is_self"]:
        raise LocalIntakePersonalOnlyError(
            "The sole receipt participant must be the self participant"
        )
    self_id = str(participants[0]["public_id"])

    # -- Adjustments must be empty -----------------------------------------
    adjustments = list(command.adjustments)
    if adjustments:
        raise LocalIntakePersonalOnlyError(
            f"Personal fact-set must have zero adjustments, got {len(adjustments)}"
        )

    # -- Build item line map -----------------------------------------------
    items = list(command.items)
    if not items:
        raise LocalIntakePersonalOnlyError("Fact-set command has no items")

    item_lines: dict[int, tuple[Decimal, str]] = {}  # line_number -> (amount, currency)
    for item in items:
        if not isinstance(item, Mapping):
            raise LocalIntakePersonalOnlyError(
                f"Item entry must be a mapping, got {type(item).__name__}"
            )
        raw_line = item.get("line_number")
        if not isinstance(raw_line, int):
            raise LocalIntakePersonalOnlyError(
                f"Item line_number must be an int, got {type(raw_line).__name__}"
            )
        if raw_line in item_lines:
            raise LocalIntakePersonalOnlyError(f"Duplicate item line_number {raw_line}")
        raw_amount = item.get("line_amount")
        raw_currency = item.get("currency")
        if not isinstance(raw_currency, str) or not raw_currency.strip():
            raise LocalIntakePersonalOnlyError(
                f"Item line {raw_line} currency must be a non-empty string"
            )
        try:
            amount = money_decimal(raw_amount, label=f"item line {raw_line} line_amount")
            SignPolicy.STRICTLY_POSITIVE.enforce(amount, label=f"item line {raw_line} line_amount")  # type: ignore[attr-defined]
            validate_amount_for_currency(
                amount, raw_currency, label=f"item line {raw_line} line_amount"
            )
        except Exception as exc:
            raise LocalIntakePersonalOnlyError(
                f"Item line {raw_line} amount validation failed: {exc}"
            ) from None
        item_lines[raw_line] = (amount, raw_currency.strip().upper())

    # -- Validate allocations match items exactly --------------------------
    allocations = list(command.allocations)
    if len(allocations) != len(item_lines):
        raise LocalIntakePersonalOnlyError(
            f"Allocation count ({len(allocations)}) must equal item count ({len(item_lines)})"
        )

    seen_alloc_lines: set[int] = set()
    for alloc in allocations:
        if not isinstance(alloc, Mapping):
            raise LocalIntakePersonalOnlyError(
                f"Allocation entry must be a mapping, got {type(alloc).__name__}"
            )
        raw_line = alloc.get("line_number")
        if not isinstance(raw_line, int):
            raise LocalIntakePersonalOnlyError(
                f"Allocation line_number must be an int, got {type(raw_line).__name__}"
            )
        if raw_line in seen_alloc_lines:
            raise LocalIntakePersonalOnlyError(f"Duplicate allocation for line_number {raw_line}")
        seen_alloc_lines.add(raw_line)
        if raw_line not in item_lines:
            raise LocalIntakePersonalOnlyError(
                f"Allocation references unknown item line_number {raw_line}"
            )

        expected_amount, expected_currency = item_lines[raw_line]

        alloc_participants = alloc.get("participants")
        if not isinstance(alloc_participants, Sequence) or isinstance(
            alloc_participants, (str, bytes)
        ):
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} participants must be a sequence"
            )
        if len(alloc_participants) != 1:
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} must have exactly one participant, "
                f"got {len(alloc_participants)}"
            )
        alloc_entry = alloc_participants[0]
        if not isinstance(alloc_entry, Mapping):
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} participant must be a mapping"
            )
        if alloc_entry.get("participant_public_id") != self_id:
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} participant must be {self_id!r}, "
                f"got {alloc_entry.get('participant_public_id')!r}"
            )

        # Amount validation with Decimal.
        raw_share = alloc_entry.get("share_amount")
        raw_alloc_currency = alloc_entry.get("currency")
        if not isinstance(raw_alloc_currency, str) or not raw_alloc_currency.strip():
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} currency must be a non-empty string"
            )
        try:
            share = money_decimal(raw_share, label=f"allocation line {raw_line} share_amount")
        except Exception as exc:
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} share_amount invalid: {exc}"
            ) from None

        if raw_alloc_currency.strip().upper() != expected_currency:
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} currency "
                f"{raw_alloc_currency.strip().upper()!r} does not match item "
                f"currency {expected_currency!r}"
            )
        if share != expected_amount:
            raise LocalIntakePersonalOnlyError(
                f"Allocation line {raw_line} share_amount {share} does not "
                f"equal item line_amount {expected_amount}; 100% self "
                "allocation requires exact equality"
            )

    # -- All item lines must be covered ------------------------------------
    missing = set(item_lines.keys()) - seen_alloc_lines
    if missing:
        raise LocalIntakePersonalOnlyError(f"Item lines missing allocation: {sorted(missing)}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_PUBLIC_ID_PREFIX_MAX = 32


def _validate_public_id_prefix(prefix: str) -> None:
    if not prefix or not prefix.strip():
        raise LocalIntakeError("public_id_prefix must not be empty")
    if len(prefix) > _PUBLIC_ID_PREFIX_MAX:
        raise LocalIntakeError(
            f"public_id_prefix exceeds maximum length ({len(prefix)} > {_PUBLIC_ID_PREFIX_MAX})"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", prefix):
        raise LocalIntakeError(
            f"public_id_prefix {prefix!r} must be lowercase alphanumeric "
            "with underscores, starting with a letter or digit"
        )


def _resolve_or_create_attachment(
    conn: sqlite3.Connection,
    *,
    workspace_copy_path: str,
    content_hash: str,
    mime_type: str,
    original_filename: str,
) -> int:
    """Return the id of an existing matching attachment, or create one."""
    existing = conn.execute(
        "SELECT id FROM attachments WHERE file_hash = ? AND file_path = ?",
        (content_hash, workspace_copy_path),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    from uuid import uuid4

    att_public_id = f"at_{uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO attachments (
            public_id, attachment_type, file_path, original_filename,
            mime_type, file_hash, source_channel, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            att_public_id,
            "local_receipt_image",
            workspace_copy_path,
            original_filename,
            mime_type,
            content_hash,
            SOURCE_CHANNEL,
            now,
            now,
        ),
    )
    conn.commit()
    rowid = cursor.lastrowid
    if rowid is None:
        raise LocalIntakeEvidenceError("Attachment insert did not return an id")
    return int(rowid)


__all__ = [
    "LocalIntakeCopyError",
    "LocalIntakeError",
    "LocalIntakeEvidenceError",
    "LocalIntakeFileError",
    "LocalIntakePersonalOnlyError",
    "LocalIntakePipelineError",
    "LocalIntakeResult",
    "LocalLineageError",
    "import_local_receipt_file",
    "persist_local_attachment_evidence",
    "require_local_runner_receipt",
    "require_local_runner_receipt_proposal",
    "run_local_receipt_intake",
    "validate_personal_conversion_command",
    "validate_personal_fact_set_command",
]
