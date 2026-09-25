"""Durable delivery state for verified D3 results; no transport is invoked."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from typing import Any

from finance_core.application.corrections import CorrectionService
from finance_core.intake.capture_jobs import (
    get_capture_job,
    require_durable_capture_connection,
)
from finance_core.openclaw_staging_bridge.capture_results import (
    CaptureResultUnavailable,
    recover_capture_result,
)
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.staging_guard import require_staging_database


class ReplyOutboxConflict(ValueError):
    """A send state changed or the caller requested an unsafe automatic retry."""


def _public_id(job_public_id: str, kind: str, result_public_id: str) -> str:
    material = f"finance-capture-reply-v1\0{job_public_id}\0{kind}\0{result_public_id}"
    return "fcr_" + hashlib.sha256(material.encode()).hexdigest()[:40]


def _row(conn: sqlite3.Connection, public_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT public_id, job_public_id, result_kind, result_public_id, "
        "status, send_attempt_nonce, send_attempt_count, created_at, updated_at "
        "FROM finance_capture_reply_outbox WHERE public_id = ?",
        (public_id,),
    )
    result = cursor.fetchone()
    if result is None:
        raise ReplyOutboxConflict("Reply outbox row is missing")
    return dict(zip((column[0] for column in cursor.description), result, strict=True))


def _sync_job_reply_status(conn: sqlite3.Connection, job_public_id: str) -> None:
    """Keep the job summary conservative when reply receipts arrive out of order."""
    conn.execute(
        "UPDATE finance_capture_jobs SET reply_status = CASE "
        "WHEN EXISTS (SELECT 1 FROM finance_capture_reply_outbox "
        "WHERE job_public_id = ? AND status = 'pending') THEN 'pending' "
        "WHEN EXISTS (SELECT 1 FROM finance_capture_reply_outbox "
        "WHERE job_public_id = ? AND status = 'outcome_unknown') THEN 'outcome_unknown' "
        "ELSE 'sent' END, updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
        (job_public_id, job_public_id, job_public_id),
    )


def ensure_result_reply(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    review_public_id: str,
    context: HumanActionContext,
    correction_service: CorrectionService | None = None,
    correction_plan_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, object]]:
    """Enqueue one existing result and return a freshly verified reply view.

    A replay never inserts a second row. Financial state is read only. The
    reply view is not cached because later D2b corrections can supersede it.
    """
    require_staging_database(conn)
    if conn.in_transaction:
        raise RuntimeError("Outbox preparation requires a fresh transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = recover_capture_result(
            conn,
            job_public_id=job_public_id,
            review_public_id=review_public_id,
            context=context,
            correction_service=correction_service,
            correction_plan_id=correction_plan_id,
        )
        kind = str(result["result_kind"])
        identity = str(result["result_public_id"])
        public_id = _public_id(job_public_id, kind, identity)
        conn.execute(
            "INSERT INTO finance_capture_reply_outbox "
            "(public_id, job_public_id, result_kind, result_public_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(job_public_id, result_kind, result_public_id) "
            "DO NOTHING",
            (public_id, job_public_id, kind, identity),
        )
        row = _row(conn, public_id)
        if (row["job_public_id"], row["result_kind"], row["result_public_id"]) != (
            job_public_id,
            kind,
            identity,
        ):
            raise ReplyOutboxConflict("Reply identity collision")
        updated = conn.execute(
            "UPDATE finance_capture_jobs SET status = 'result_ready', "
            "updated_at = CURRENT_TIMESTAMP WHERE public_id = ? "
            "AND lease_owner IS NULL AND status IN "
            "('processing', 'awaiting_user', 'needs_attention', 'result_ready')",
            (job_public_id,),
        )
        if updated.rowcount != 1:
            raise ReplyOutboxConflict("Capture job still has active processing")
        _sync_job_reply_status(conn, job_public_id)
        conn.commit()
        return row, result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def begin_reply_attempt(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    review_public_id: str,
    context: HumanActionContext,
    correction_service: CorrectionService | None = None,
    correction_plan_id: str | None = None,
    retry_unknown: bool = False,
) -> tuple[dict[str, Any], dict[str, object]]:
    """Mark outcome unknown *before* the caller crosses the send boundary.

    The caller must explicitly opt into redelivery after a lost send receipt.
    Every attempt receives a fresh verified view of the current correction head.
    """
    require_staging_database(conn)
    if conn.in_transaction:
        raise RuntimeError("Reply claim requires a fresh transaction")
    # The next commit is the last local barrier before an external send. In
    # WAL/NORMAL, a returned commit is not sufficient evidence after power
    # loss. Do not hand a send nonce to the caller until FULL is verified.
    require_durable_capture_connection(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _row(conn, public_id)
        if row["result_kind"] == "correction" and correction_plan_id is None:
            persisted_plan = conn.execute(
                "SELECT plan_id FROM correction_versions WHERE correction_id = ?",
                (row["result_public_id"],),
            ).fetchone()
            if persisted_plan is None:
                raise ReplyOutboxConflict("Correction result has no committed plan")
            correction_plan_id = str(persisted_plan[0])
        result = recover_capture_result(
            conn,
            job_public_id=str(row["job_public_id"]),
            review_public_id=review_public_id,
            context=context,
            correction_service=correction_service,
            correction_plan_id=correction_plan_id,
        )
        if (row["result_kind"], row["result_public_id"]) != (
            result["result_kind"],
            result["result_public_id"],
        ):
            raise ReplyOutboxConflict("Reply result identity changed")
        if row["status"] != "pending" and not (
            row["status"] == "outcome_unknown" and retry_unknown
        ):
            raise ReplyOutboxConflict("Reply is sent or has an unknown delivery outcome")
        nonce = secrets.token_hex(32)
        conn.execute(
            "UPDATE finance_capture_reply_outbox SET status = 'outcome_unknown', "
            "send_attempt_nonce = ?, send_attempt_count = send_attempt_count + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
            (nonce, public_id),
        )
        _sync_job_reply_status(conn, str(row["job_public_id"]))
        conn.commit()
        return _row(conn, public_id), result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def record_reply_sent(
    conn: sqlite3.Connection, *, public_id: str, send_attempt_nonce: str
) -> dict[str, Any]:
    """Record a transport receipt for exactly the last persisted attempt."""
    require_staging_database(conn)
    if conn.in_transaction:
        raise RuntimeError("Reply receipt requires a fresh transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _row(conn, public_id)
        if row["send_attempt_nonce"] != send_attempt_nonce or row["status"] not in {
            "outcome_unknown",
            "sent",
        }:
            raise ReplyOutboxConflict("Reply attempt nonce is stale")
        if row["status"] == "outcome_unknown":
            conn.execute(
                "UPDATE finance_capture_reply_outbox SET status = 'sent', "
                "updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
                (public_id,),
            )
            _sync_job_reply_status(conn, str(row["job_public_id"]))
        conn.commit()
        return _row(conn, public_id)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def get_reply(conn: sqlite3.Connection, *, public_id: str) -> dict[str, Any]:
    """Read delivery state without initiating a new send."""
    require_staging_database(conn)
    return _row(conn, public_id)


def reconcile_capture_result_replies(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    correction_service: CorrectionService | None = None,
) -> list[dict[str, Any]]:
    """Find committed D2/D2b results missed by a crash before enqueue.

    This is safe for startup or periodic calls. It only examines existing
    finalized attempts and committed correction versions, and every candidate
    is independently reverified before an outbox row is inserted. The caller
    owns scheduling; this function has no worker, provider, or send loop.
    """
    require_staging_database(conn)
    if conn.in_transaction:
        raise RuntimeError("Reconciliation requires a fresh connection transaction")
    job = get_capture_job(conn, public_id=job_public_id)
    if job is None:
        raise CaptureResultUnavailable("Capture job not found")
    reviews = conn.execute(
        "SELECT reviews.review_public_id, reviews.authenticated_actor_id, "
        "reviews.telegram_account_id, reviews.telegram_conversation_id, "
        "reviews.conversation_binding_id, attempts.transaction_public_id "
        "FROM d2_posting_attempts AS attempts "
        "JOIN d2_posting_reviews AS reviews "
        "ON reviews.review_public_id = attempts.review_public_id "
        "LEFT JOIN parser_human_draft_cards AS cards "
        "ON cards.card_generation_public_id = reviews.card_generation_public_id "
        "LEFT JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id "
        "LEFT JOIN d2_initial_proposal_cards AS initial "
        "ON initial.initial_card_public_id = reviews.initial_card_public_id "
        "WHERE attempts.stage = 'finalized' AND "
        "((reviews.source_kind = 'd1_human_card' "
        "AND drafts.source_raw_intake_id = ?) OR "
        "(reviews.source_kind = 'initial_proposal_card' "
        "AND initial.raw_intake_record_id = ?))",
        (job["raw_intake_record_id"], job["raw_intake_record_id"]),
    ).fetchall()
    if len(reviews) > 1:
        raise CaptureResultUnavailable("Multiple committed postings claim one capture job")
    if not reviews:
        return []
    review = reviews[0]
    context = HumanActionContext(
        actor_id=str(review["authenticated_actor_id"]),
        account_id=str(review["telegram_account_id"]),
        conversation_id=str(review["telegram_conversation_id"]),
        binding_id=str(review["conversation_binding_id"]),
    )
    review_id = str(review["review_public_id"])
    target = str(review["transaction_public_id"])
    plans = [
        str(row[0])
        for row in conn.execute(
            "SELECT plan_id FROM correction_versions WHERE target_id = ? ORDER BY version",
            (target,),
        )
    ]
    # Do not partially announce a corrected posting when the D2b verifier is
    # unavailable. Historical correction replies are also verified below.
    if plans and (correction_service is None or correction_service.connection is not conn):
        raise CaptureResultUnavailable("Trusted correction verifier is required")
    candidates = [None, *plans]
    # Preflight every candidate without writes. The enqueue call rechecks in
    # its own write transaction, closing races with a newer correction.
    for plan_id in candidates:
        recover_capture_result(
            conn,
            job_public_id=job_public_id,
            review_public_id=review_id,
            context=context,
            correction_service=correction_service,
            correction_plan_id=plan_id,
        )
    result_rows: list[dict[str, Any]] = []
    for plan_id in candidates:
        row, _ = ensure_result_reply(
            conn,
            job_public_id=job_public_id,
            review_public_id=review_id,
            context=context,
            correction_service=correction_service,
            correction_plan_id=plan_id,
        )
        result_rows.append(row)
    return result_rows
