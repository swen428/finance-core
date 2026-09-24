"""D3 durable capture jobs, bound to the existing raw source evidence.

Call ``ensure_capture_job`` inside the transaction that commits a text intake
or receipt attachment source. A job never grants confirmation or finalization
authority. The public ID is stable across process crashes and replay.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Literal

from finance_core.intake.raw_text_repository import get_raw_intake_record
from finance_core.staging_guard import require_staging_database

CaptureKind = Literal["text", "receipt_image"]


class CaptureJobConflictError(ValueError):
    """An intake/job identity is already bound to different source evidence."""


def capture_job_public_id(intake_public_id: str) -> str:
    if not intake_public_id or len(intake_public_id) > 200:
        raise ValueError("Invalid intake public ID")
    material = ("finance-capture-job-v1\0" + intake_public_id).encode("utf-8")
    return "fcj_" + hashlib.sha256(material).hexdigest()[:40]


def get_capture_job(
    conn: sqlite3.Connection,
    *,
    public_id: str | None = None,
    intake_public_id: str | None = None,
) -> dict[str, Any] | None:
    """Read the job and original source identity, without changing state."""
    require_staging_database(conn)
    if (public_id is None) == (intake_public_id is None):
        raise ValueError("Provide exactly one job or intake public ID")
    predicate = "j.public_id = ?" if public_id is not None else "r.public_id = ?"
    key = public_id if public_id is not None else intake_public_id
    cursor = conn.execute(
        "SELECT j.public_id, r.public_id AS intake_public_id, "
        "j.raw_intake_record_id, j.capture_kind, j.intake_fingerprint, "
        "j.attachment_evidence_id, j.attachment_content_hash, j.status, "
        "j.ingress_identity_digest, "
        "j.ai_status, j.reply_status, j.lease_epoch, j.lease_owner, "
        "j.lease_expires_at, j.last_error, j.created_at, j.updated_at "
        "FROM finance_capture_jobs j JOIN raw_intake_records r "
        f"ON r.id = j.raw_intake_record_id WHERE {predicate}",
        (key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def ensure_capture_job(
    conn: sqlite3.Connection,
    *,
    intake_id: int,
    capture_kind: CaptureKind,
    attachment_evidence_id: int | None = None,
    ingress_identity_digest: str | None = None,
) -> dict[str, Any]:
    """Insert once in the caller-owned transaction, or verify exact replay.

    Receipt creation requires a committed or same-transaction immutable source
    row and a linked durable attachment. The caller owns commit/rollback.
    """
    require_staging_database(conn)
    if not conn.in_transaction:
        raise RuntimeError("Capture job requires the source transaction")
    if capture_kind not in {"text", "receipt_image"}:
        raise ValueError("Unsupported capture kind")
    intake = get_raw_intake_record(conn, intake_id)
    if intake is None or intake["source_channel"] != "telegram":
        raise ValueError("Telegram raw intake is required")
    fingerprint = intake["content_fingerprint"]
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("Intake has no stable content fingerprint")
    attachment_hash: str | None = None
    if capture_kind == "receipt_image":
        if attachment_evidence_id is None:
            raise ValueError("Receipt job requires durable attachment evidence")
        source = conn.execute(
            "SELECT content_hash, attachment_id FROM telegram_attachment_source "
            "WHERE id = ? AND raw_intake_record_id = ?",
            (attachment_evidence_id, intake_id),
        ).fetchone()
        if source is None or intake["attachment_id"] != source[1]:
            raise ValueError("Receipt attachment evidence is not linked to intake")
        attachment_hash = str(source[0])
        if intake["attachment_hash"] != attachment_hash or not intake["attachment_path"]:
            raise ValueError("Receipt original is not durably linked")
    elif attachment_evidence_id is not None:
        raise ValueError("Text job cannot name attachment evidence")
    if ingress_identity_digest is not None and (
        len(ingress_identity_digest) != 64
        or any(character not in "0123456789abcdef" for character in ingress_identity_digest)
    ):
        raise ValueError("Ingress identity digest must be lowercase SHA-256")

    job_id = capture_job_public_id(str(intake["public_id"]))
    existing = get_capture_job(conn, intake_public_id=str(intake["public_id"]))
    if existing is not None:
        expected = {
            "public_id": job_id,
            "capture_kind": capture_kind,
            "intake_fingerprint": fingerprint,
            "attachment_evidence_id": attachment_evidence_id,
            "attachment_content_hash": attachment_hash,
            "ingress_identity_digest": ingress_identity_digest,
        }
        if any(existing[field] != value for field, value in expected.items()):
            raise CaptureJobConflictError("Capture job is bound to different source evidence")
        return existing

    try:
        conn.execute(
            "INSERT INTO finance_capture_jobs "
            "(public_id, raw_intake_record_id, capture_kind, intake_fingerprint, "
            "attachment_evidence_id, attachment_content_hash, ingress_identity_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                intake_id,
                capture_kind,
                fingerprint,
                attachment_evidence_id,
                attachment_hash,
                ingress_identity_digest,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise CaptureJobConflictError("Capture job identity already exists") from exc
    created = get_capture_job(conn, public_id=job_id)
    assert created is not None
    return created
