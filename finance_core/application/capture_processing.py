"""Leased local D3 processing of durably captured Telegram source evidence.

The service performs no network call, AI invocation, reply, confirmation, or
financial finalization. Receipt OCR runs outside a SQLite write lock; both OCR
and proposal persistence check the lease epoch inside their own write unit of
work. A crashed worker can resume by the stable job and stage identities.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from finance_core.intake.capture_jobs import (
    CaptureLease,
    CaptureLeaseLostError,
    assert_capture_lease,
    get_capture_job,
)
from finance_core.intake.receipt_ocr_evidence import (
    OcrDeadlineExceededError,
    ReceiptOcrEngine,
    ReceiptOcrError,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.intake.receipt_ocr_proposal import (
    ReceiptOcrProposalError,
    ingest_receipt_ocr_evidence_as_total_expense_proposal,
)
from finance_core.staging_guard import require_staging_database


class CaptureProcessingConflictError(RuntimeError):
    """The durable stage bindings do not match the captured source."""


OCR_TIMEOUT_MAX_ATTEMPTS = 3
OCR_TIMEOUT_BACKOFF_MS = (10_000, 30_000)


def _stage_ids(job: dict[str, Any]) -> tuple[str, str, str]:
    public_id = str(job["public_id"])
    suffix = public_id.removeprefix("fcj_")
    if (
        public_id != "fcj_" + suffix
        or len(suffix) != 40
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise CaptureProcessingConflictError("Malformed capture job identity")
    intake_public_id = str(job["intake_public_id"])
    # D3-2 capture retains the bridge's established receipt-stage identities.
    # Reusing them makes a prior synchronous `propose` and a later worker
    # recovery the same operation, never two competing OCR fingerprints.
    bridge_suffix = intake_public_id.removeprefix("raw_intake_bridge_")
    if (
        intake_public_id == "raw_intake_bridge_" + bridge_suffix
        and len(bridge_suffix) == 32
        and all(character in "0123456789abcdef" for character in bridge_suffix)
    ):
        return (
            f"rocr_bridge_{bridge_suffix}",
            f"prop_bridge_{bridge_suffix}",
            f"ropl_bridge_{bridge_suffix}",
        )
    return (f"rocr_d3_{suffix}", f"prop_d3_{suffix}", f"ropl_d3_{suffix}")


def _begin_checked(conn: sqlite3.Connection, lease: CaptureLease) -> dict[str, Any]:
    if conn.in_transaction:
        raise RuntimeError("Capture processing requires a connection without pending work")
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert_capture_lease(conn, lease)
        job = get_capture_job(conn, public_id=lease.public_id)
        if job is None:
            raise CaptureProcessingConflictError("Capture job vanished")
        return job
    except BaseException:
        conn.rollback()
        raise


def _ai_state(conn: sqlite3.Connection, raw_intake_record_id: int) -> tuple[str | None, str]:
    row = conn.execute(
        "SELECT a.attempt_public_id, c.id, r.id FROM ai_fallback_attempts a "
        "LEFT JOIN ai_fallback_invocation_claims c ON c.attempt_id = a.id "
        "LEFT JOIN ai_fallback_results r ON r.attempt_id = a.id "
        "WHERE a.raw_intake_record_id = ?",
        (raw_intake_record_id,),
    ).fetchone()
    if row is None:
        return None, "not_started"
    if row[2] is not None:
        return str(row[0]), "completed"
    if row[1] is not None:
        # A durable invocation claim may have crossed the provider boundary.
        # Never issue or retry a model call from the recovery processor.
        return str(row[0]), "outcome_unknown"
    return str(row[0]), "not_started"


def _finish(
    conn: sqlite3.Connection,
    lease: CaptureLease,
    *,
    status: str,
    last_error: str | None = None,
) -> None:
    assert_capture_lease(conn, lease)
    job = get_capture_job(conn, public_id=lease.public_id)
    if job is None:
        raise CaptureProcessingConflictError("Capture job vanished")
    attempt_id, ai_status = _ai_state(conn, int(job["raw_intake_record_id"]))
    bound_attempt = job["ai_attempt_public_id"]
    if bound_attempt is not None and bound_attempt != attempt_id:
        raise CaptureProcessingConflictError("AI attempt identity changed")
    conn.execute(
        "UPDATE finance_capture_jobs SET status = ?, ai_status = ?, "
        "ai_attempt_public_id = ?, lease_owner = NULL, lease_expires_at = NULL, "
        "last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
        (status, ai_status, attempt_id, last_error, lease.public_id),
    )


def _mark_attention(
    conn: sqlite3.Connection, lease: CaptureLease, *, reason: str
) -> dict[str, Any]:
    _begin_checked(conn, lease)
    try:
        _finish(conn, lease, status="needs_attention", last_error=reason)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    result = get_capture_job(conn, public_id=lease.public_id)
    assert result is not None
    return result


def _defer_ocr_timeout(conn: sqlite3.Connection, lease: CaptureLease) -> dict[str, Any]:
    """Release only this live claim after a bounded local OCR deadline."""
    job = _begin_checked(conn, lease)
    try:
        count = int(job["ocr_retry_count"]) + 1
        if count >= OCR_TIMEOUT_MAX_ATTEMPTS:
            _finish(conn, lease, status="needs_attention", last_error="ocr_timeout_exhausted")
            not_before_ms = 0
        else:
            _finish(conn, lease, status="processing", last_error="ocr_timeout_retry_pending")
            not_before_ms = time.time_ns() // 1_000_000 + OCR_TIMEOUT_BACKOFF_MS[count - 1]
        conn.execute(
            "UPDATE finance_capture_jobs SET ocr_retry_count = ?, "
            "ocr_retry_not_before_ms = ? WHERE public_id = ?",
            (count, not_before_ms, lease.public_id),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    result = get_capture_job(conn, public_id=lease.public_id)
    assert result is not None
    return result


def _fence_cause(exc: BaseException) -> CaptureLeaseLostError | None:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, CaptureLeaseLostError):
            return current
        current = current.__cause__
    return None


def process_claimed_capture_job(
    conn: sqlite3.Connection,
    *,
    lease: CaptureLease,
    engine: ReceiptOcrEngine | None = None,
) -> dict[str, Any]:
    """Resume local stages under a previously claimed, unexpired lease."""
    require_staging_database(conn)
    job = _begin_checked(conn, lease)
    try:
        if job["capture_kind"] == "text":
            row = conn.execute(
                "SELECT p.public_id FROM raw_intake_records r "
                "JOIN parser_outputs p ON p.id = r.parser_output_id "
                "WHERE r.id = ? AND p.source_public_id = r.public_id",
                (job["raw_intake_record_id"],),
            ).fetchone()
            if row is None:
                _finish(conn, lease, status="needs_attention", last_error="missing_text_proposal")
            else:
                proposal_id = str(row[0])
                if job["proposal_public_id"] not in (None, proposal_id):
                    raise CaptureProcessingConflictError("Text proposal binding changed")
                conn.execute(
                    "UPDATE finance_capture_jobs SET proposal_public_id = ? WHERE public_id = ?",
                    (proposal_id, lease.public_id),
                )
                # A proposal alone is not a review card. Keep the job
                # runnable until the later delivery stage persists one.
                _finish(conn, lease, status="processing")
            conn.commit()
            result = get_capture_job(conn, public_id=lease.public_id)
            assert result is not None
            return result

        if job["capture_kind"] != "receipt_image":
            raise CaptureProcessingConflictError("Unsupported capture kind")
        if engine is None:
            raise ValueError("Receipt processing requires a local OCR engine")
        extraction_id, proposal_id, link_id = _stage_ids(job)
        for name, expected in (
            ("ocr_extraction_public_id", extraction_id),
            ("proposal_public_id", proposal_id),
            ("proposal_link_public_id", link_id),
        ):
            if job[name] not in (None, expected):
                raise CaptureProcessingConflictError(f"{name} binding changed")
        conn.execute(
            "UPDATE finance_capture_jobs SET ocr_extraction_public_id = ?, "
            "proposal_public_id = ?, proposal_link_public_id = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
            (extraction_id, proposal_id, link_id, lease.public_id),
        )
        source = conn.execute(
            "SELECT attachment_id, content_hash FROM telegram_attachment_source "
            "WHERE id = ? AND raw_intake_record_id = ?",
            (job["attachment_evidence_id"], job["raw_intake_record_id"]),
        ).fetchone()
        if source is None or source[1] != job["attachment_content_hash"]:
            raise CaptureProcessingConflictError("Captured attachment source changed")
        attachment_id = int(source[0])
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise

    try:
        extraction = extract_and_persist_receipt_ocr_evidence(
            conn,
            public_id=extraction_id,
            attachment_id=attachment_id,
            engine=engine,
            before_commit=lambda tx: assert_capture_lease(tx, lease),
        )
    except ReceiptOcrError as exc:
        fence = _fence_cause(exc)
        if fence is not None:
            raise fence from exc
        if isinstance(exc, OcrDeadlineExceededError):
            return _defer_ocr_timeout(conn, lease)
        return _mark_attention(conn, lease, reason="ocr_failed")

    # The OCR service can return previously persisted evidence before opening
    # a write transaction. Its result is bound to this worker only when the
    # next stage's saving transaction checks the same epoch.
    try:
        ingest_receipt_ocr_evidence_as_total_expense_proposal(
            conn,
            extraction_public_id=extraction.public_id,
            proposal_public_id=proposal_id,
            link_public_id=link_id,
            before_commit=lambda tx: _finish(tx, lease, status="processing"),
        )
    except ReceiptOcrProposalError as exc:
        fence = _fence_cause(exc)
        if fence is not None:
            raise fence from exc
        return _mark_attention(conn, lease, reason="proposal_failed")
    result = get_capture_job(conn, public_id=lease.public_id)
    assert result is not None
    return result
