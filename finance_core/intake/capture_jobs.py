"""D3 durable capture jobs, bound to the existing raw source evidence.

Call ``ensure_capture_job`` inside the transaction that commits a text intake
or receipt attachment source. A job never grants confirmation or finalization
authority. The public ID is stable across process crashes and replay.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from finance_core.intake.raw_text_repository import get_raw_intake_record
from finance_core.staging_guard import require_staging_database

CaptureKind = Literal["text", "receipt_image"]


class CaptureJobConflictError(ValueError):
    """An intake/job identity is already bound to different source evidence."""


class DurableCaptureConnectionError(ValueError):
    """The SQLite connection cannot prove a durable capture commit."""


def require_durable_capture_connection(conn: sqlite3.Connection) -> None:
    """Require a disk-backed FULL-synchronous journal before capture writes."""
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()
        if journal is None or str(journal[0]).lower() not in {
            "wal",
            "delete",
            "truncate",
            "persist",
        }:
            raise DurableCaptureConnectionError(
                "Core capture journal does not support durable commits"
            )
        conn.execute("PRAGMA synchronous = FULL")
        synchronous = conn.execute("PRAGMA synchronous").fetchone()
        if synchronous is None or int(synchronous[0]) != 2:
            raise DurableCaptureConnectionError(
                "Core capture connection did not retain FULL synchronization"
            )
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise DurableCaptureConnectionError(
            "Core capture could not prove a durable SQLite commit setting"
        ) from exc
class CaptureLeaseLostError(RuntimeError):
    """The worker's epoch, owner, or unexpired lease no longer matches."""


class CaptureJobNotRunnableError(RuntimeError):
    """The job is terminal or leased by another worker."""


@dataclass(frozen=True)
class CaptureLease:
    public_id: str
    owner: str
    epoch: int
    expires_at_ms: int


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
        "j.lease_expires_at, j.last_error, j.created_at, j.updated_at, "
        "j.ocr_extraction_public_id, j.proposal_public_id, "
        "j.proposal_link_public_id, j.ai_attempt_public_id "
        "FROM finance_capture_jobs j JOIN raw_intake_records r "
        f"ON r.id = j.raw_intake_record_id WHERE {predicate}",
        (key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    job = dict(zip((column[0] for column in cursor.description), row, strict=True))
    # Existing AI preparation/claim/result services are the authority for an
    # invocation. Project their durable evidence on every replay, including
    # claims made after the local processor finished. This is read-only.
    ai = conn.execute(
        "SELECT a.attempt_public_id, c.id, r.id FROM ai_fallback_attempts a "
        "LEFT JOIN ai_fallback_invocation_claims c ON c.attempt_id = a.id "
        "LEFT JOIN ai_fallback_results r ON r.attempt_id = a.id "
        "WHERE a.raw_intake_record_id = ?",
        (job["raw_intake_record_id"],),
    ).fetchone()
    if ai is not None:
        attempt_id = str(ai[0])
        if job["ai_attempt_public_id"] not in (None, attempt_id):
            raise CaptureJobConflictError("Capture job AI attempt binding changed")
        job["ai_attempt_public_id"] = attempt_id
        job["ai_status"] = (
            "completed"
            if ai[2] is not None
            else "outcome_unknown"
            if ai[1] is not None
            else "not_started"
        )
    return job


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


def ensure_replayed_text_capture_job(
    conn: sqlite3.Connection, *, intake_id: int, ingress_identity_digest: str | None
) -> dict[str, Any]:
    """Atomically attach a D3 job to a pre-D3 text capture during replay."""
    require_staging_database(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        job = ensure_capture_job(
            conn,
            intake_id=intake_id,
            capture_kind="text",
            ingress_identity_digest=ingress_identity_digest,
        )
        conn.commit()
        return job
    except BaseException:
        conn.rollback()
        raise


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _validate_lease_inputs(owner: str, duration_ms: int) -> None:
    if not isinstance(owner, str) or not (1 <= len(owner) <= 128) or not owner.isascii():
        raise ValueError("Lease owner must be a bounded ASCII worker identity")
    if any(character.isspace() or ord(character) < 33 for character in owner):
        raise ValueError("Lease owner contains unsafe characters")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise ValueError("Lease duration must be an integer")
    if not 1_000 <= duration_ms <= 300_000:
        raise ValueError("Lease duration must be between 1 and 300 seconds")


def claim_capture_job(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    owner: str,
    duration_ms: int = 60_000,
    now_ms: int | None = None,
) -> CaptureLease:
    """Atomically claim or reclaim one runnable job with a fresh fencing epoch."""
    require_staging_database(conn)
    _validate_lease_inputs(owner, duration_ms)
    if conn.in_transaction:
        raise RuntimeError("Claim requires a connection without pending work")
    now = _now_ms() if now_ms is None else now_ms
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("Invalid lease clock")
    conn.execute("BEGIN IMMEDIATE")
    try:
        job = get_capture_job(conn, public_id=public_id)
        if job is None or job["status"] not in {"captured", "processing"}:
            raise CaptureJobNotRunnableError("Capture job is not runnable")
        expiry = job["lease_expires_at"]
        if expiry is not None and int(expiry) > now:
            raise CaptureJobNotRunnableError("Capture job has an active lease")
        epoch = int(job["lease_epoch"]) + 1
        expires_at = now + duration_ms
        cursor = conn.execute(
            "UPDATE finance_capture_jobs SET status = 'processing', "
            "lease_epoch = ?, lease_owner = ?, lease_expires_at = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE public_id = ? AND lease_epoch = ?",
            (epoch, owner, expires_at, public_id, epoch - 1),
        )
        if cursor.rowcount != 1:
            raise CaptureLeaseLostError("Capture job changed during claim")
        conn.commit()
        return CaptureLease(public_id, owner, epoch, expires_at)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def assert_capture_lease(
    conn: sqlite3.Connection, lease: CaptureLease, *, now_ms: int | None = None
) -> None:
    """Check the fence inside the caller's open write transaction."""
    require_staging_database(conn)
    if not conn.in_transaction:
        raise RuntimeError("Lease check requires the saving transaction")
    now = _now_ms() if now_ms is None else now_ms
    row = conn.execute(
        "SELECT lease_epoch, lease_owner, lease_expires_at, status "
        "FROM finance_capture_jobs WHERE public_id = ?",
        (lease.public_id,),
    ).fetchone()
    if (
        row is None
        or row[0] != lease.epoch
        or row[1] != lease.owner
        or row[2] is None
        or int(row[2]) <= now
        or row[3] != "processing"
    ):
        raise CaptureLeaseLostError("Capture lease is stale or expired")


def renew_capture_job_lease(
    conn: sqlite3.Connection,
    lease: CaptureLease,
    *,
    duration_ms: int = 60_000,
    now_ms: int | None = None,
) -> CaptureLease:
    """Renew only the still-live matching epoch; never resurrect expiry."""
    require_staging_database(conn)
    _validate_lease_inputs(lease.owner, duration_ms)
    if conn.in_transaction:
        raise RuntimeError("Renew requires a connection without pending work")
    now = _now_ms() if now_ms is None else now_ms
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert_capture_lease(conn, lease, now_ms=now)
        expiry = now + duration_ms
        conn.execute(
            "UPDATE finance_capture_jobs SET lease_expires_at = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
            (expiry, lease.public_id),
        )
        conn.commit()
        return CaptureLease(lease.public_id, lease.owner, lease.epoch, expiry)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
