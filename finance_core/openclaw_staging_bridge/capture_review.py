"""Recover one D2 initial review from a durably captured D3 proposal.

The D2 review transaction commits separately from the capture-job status.
The stable job key and the verified review/card rows make a crash in between
safe to replay without creating a second card or granting posting authority.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from typing import Any

from finance_core.intake.capture_jobs import get_capture_job
from finance_core.openclaw_staging_bridge.capture_results import recover_capture_result
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.posting_authority import (
    PostingAuthorityError,
    PreparedPostingReview,
    get_status,
    prepare_posting_review,
)
from finance_core.staging_guard import require_staging_database
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    require_telegram_source_context,
)


class CaptureReviewConflictError(RuntimeError):
    """The captured proposal, source, or durable review has drifted."""


class _ReviewExpiringError(CaptureReviewConflictError):
    """The original immutable card has too little safe lifetime left."""


_MIN_REVIEW_REMAINING_SECONDS = 60


def _review_key(job_public_id: str) -> str:
    return "bridge-d3-review:" + hashlib.sha256(job_public_id.encode("utf-8")).hexdigest()


def _source_context(
    conn: sqlite3.Connection, job: dict[str, Any]
) -> tuple[HumanActionContext, str]:
    intake_id = int(job["raw_intake_record_id"])
    source = conn.execute(
        "SELECT r.source_channel, r.external_source_id, r.source_message_id, "
        "r.parser_output_id, p.public_id AS proposal_public_id, "
        "c.authenticated_actor_id, c.telegram_account_id, "
        "c.telegram_conversation_id, c.conversation_binding_id, "
        "c.source_message_id AS context_message_id "
        "FROM raw_intake_records r "
        "LEFT JOIN parser_outputs p ON p.id = r.parser_output_id "
        "LEFT JOIN d2_telegram_source_contexts c ON c.raw_intake_record_id = r.id "
        "WHERE r.id = ?",
        (intake_id,),
    ).fetchone()
    if source is None or source["context_message_id"] is None:
        raise CaptureReviewConflictError("Captured Telegram source context is unavailable")
    message_id = str(source["context_message_id"])
    conversation_id = str(source["telegram_conversation_id"])
    if (
        source["source_channel"] != "telegram"
        or source["source_message_id"] != message_id
        or source["external_source_id"] != f"telegram:{conversation_id}:{message_id}"
        or source["proposal_public_id"] != job["proposal_public_id"]
    ):
        raise CaptureReviewConflictError("Captured source or proposal binding changed")
    context = TelegramSourceContext(
        authenticated_actor_id=str(source["authenticated_actor_id"]),
        account_id=str(source["telegram_account_id"]),
        conversation_id=conversation_id,
        binding_id=str(source["conversation_binding_id"]),
        message_id=message_id,
    )
    try:
        require_telegram_source_context(conn, raw_intake_record_id=intake_id, context=context)
    except TelegramSourceContextError as exc:
        raise CaptureReviewConflictError("Captured source identity is unavailable") from exc
    return (
        HumanActionContext(
            actor_id=context.authenticated_actor_id,
            account_id=context.account_id,
            conversation_id=context.conversation_id,
            binding_id=context.binding_id,
        ),
        message_id,
    )


def _promote_review_status(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    proposal_public_id: str,
    review: PreparedPostingReview,
    context: HumanActionContext,
    message_id: str,
) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        job = get_capture_job(conn, public_id=job_public_id)
        if (
            job is None
            or job["status"] not in {"processing", "awaiting_user"}
            or job["proposal_public_id"] != proposal_public_id
            or job["lease_owner"] is not None
            or job["lease_expires_at"] is not None
        ):
            raise CaptureReviewConflictError("Capture job is not ready for review")
        actual_context, actual_message_id = _source_context(conn, job)
        if actual_context != context or actual_message_id != message_id:
            raise CaptureReviewConflictError("Captured review context changed")
        row = conn.execute(
            "SELECT r.review_public_id, r.review_idempotency_key, r.source_kind, "
            "r.parser_output_id, r.authenticated_actor_id, r.telegram_account_id, "
            "r.telegram_conversation_id, r.conversation_binding_id, "
            "r.visible_projection_hash, r.initial_card_public_id, "
            "r.proposal_version, r.proposal_content_hash, r.expires_at, "
            "c.raw_intake_record_id, c.admitted_source_message_id, "
            "c.admitted_source_identity_sha256, c.presentation_text, "
            "c.presentation_text_hash, c.proposal_version AS card_version, "
            "c.proposal_content_hash AS card_content_hash "
            "FROM d2_posting_reviews r JOIN d2_initial_proposal_cards c "
            "ON c.initial_card_public_id = r.initial_card_public_id "
            "WHERE r.review_idempotency_key = ?",
            (_review_key(job_public_id),),
        ).fetchone()
        if row is None:
            raise CaptureReviewConflictError("Durable D2 review/card is unavailable")
        source_identity = require_telegram_source_context(
            conn,
            raw_intake_record_id=int(job["raw_intake_record_id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=context.actor_id,
                account_id=context.account_id,
                conversation_id=context.conversation_id,
                binding_id=context.binding_id,
                message_id=message_id,
            ),
        )
        presentation_hash = hashlib.sha256(
            str(row["presentation_text"]).encode("utf-8")
        ).hexdigest()
        if (
            row["review_public_id"] != review.review_public_id
            or row["source_kind"] != "initial_proposal_card"
            or int(row["raw_intake_record_id"]) != int(job["raw_intake_record_id"])
            or row["admitted_source_message_id"] != message_id
            or not hmac.compare_digest(str(row["admitted_source_identity_sha256"]), source_identity)
            or row["initial_card_public_id"] != review.initial_card_public_id
            or row["visible_projection_hash"] != review.visible_projection_hash
            or row["presentation_text"] != review.presentation_text
            or not hmac.compare_digest(str(row["presentation_text_hash"]), presentation_hash)
            or (
                row["authenticated_actor_id"],
                row["telegram_account_id"],
                row["telegram_conversation_id"],
                row["conversation_binding_id"],
            )
            != (context.actor_id, context.account_id, context.conversation_id, context.binding_id)
        ):
            raise CaptureReviewConflictError("Durable D2 review/card binding changed")
        proposal = conn.execute(
            "SELECT id, parse_status FROM parser_outputs WHERE public_id = ?",
            (proposal_public_id,),
        ).fetchone()
        if proposal is None or int(row["parser_output_id"]) != int(proposal["id"]):
            raise CaptureReviewConflictError("Durable review proposal binding changed")
        current_hash = compute_effective_proposal_content_hash(conn, {"id": int(proposal["id"])})
        if (
            proposal["parse_status"] != "parsed_pending_confirmation"
            or int(row["proposal_version"]) != review.proposal_version
            or int(row["card_version"]) != review.proposal_version
            or not hmac.compare_digest(str(row["proposal_content_hash"]), current_hash)
            or not hmac.compare_digest(str(row["card_content_hash"]), current_hash)
        ):
            raise CaptureReviewConflictError("Durable review proposal is stale")
        if int(row["expires_at"]) <= int(time.time()) + _MIN_REVIEW_REMAINING_SECONDS:
            raise _ReviewExpiringError("Durable review card is expiring")
        if job["status"] == "processing":
            conn.execute(
                "UPDATE finance_capture_jobs SET status = 'awaiting_user', "
                "updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
                (job_public_id,),
            )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    updated = get_capture_job(conn, public_id=job_public_id)
    assert updated is not None
    return updated


def _existing_review_disposition(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    proposal_public_id: str,
    context: HumanActionContext,
    message_id: str,
) -> tuple[dict[str, Any], None, bool] | None:
    """Resolve an existing card under one write lock before replaying its key.

    The immutable D2 initial card cannot be extended here. An unaccepted card
    that has fewer than 60 seconds left becomes a queryable stop. Any accepted
    decision remains with D2; final results are verified by the existing
    result locator and outbox recovery, never recreated from the card.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        job = get_capture_job(conn, public_id=job_public_id)
        if (
            job is None
            or job["proposal_public_id"] != proposal_public_id
            or job["status"]
            not in {"processing", "awaiting_user", "needs_attention", "result_ready"}
            or job["lease_owner"] is not None
            or job["lease_expires_at"] is not None
        ):
            raise CaptureReviewConflictError("Capture job changed during review recovery")
        if job["status"] == "needs_attention" and job["last_error"] != "review_expired":
            raise CaptureReviewConflictError("Capture job needs another kind of attention")
        actual_context, actual_message_id = _source_context(conn, job)
        if actual_context != context or actual_message_id != message_id:
            raise CaptureReviewConflictError("Captured review context changed")
        row = conn.execute(
            "SELECT r.review_public_id, r.source_kind, r.parser_output_id, "
            "r.initial_card_public_id, r.authenticated_actor_id, "
            "r.telegram_account_id, r.telegram_conversation_id, "
            "r.conversation_binding_id, r.proposal_content_hash, r.expires_at, "
            "c.card_idempotency_key, c.raw_intake_record_id, "
            "c.admitted_source_message_id, c.admitted_source_identity_sha256, "
            "c.proposal_content_hash AS card_content_hash, "
            "c.expires_at AS card_expires_at, p.public_id AS proposal_public_id "
            "FROM d2_posting_reviews r "
            "LEFT JOIN d2_initial_proposal_cards c "
            "ON c.initial_card_public_id = r.initial_card_public_id "
            "JOIN parser_outputs p ON p.id = r.parser_output_id "
            "WHERE r.review_idempotency_key = ?",
            (_review_key(job_public_id),),
        ).fetchone()
        if row is None:
            if job["status"] in {"needs_attention", "result_ready"}:
                raise CaptureReviewConflictError("Durable D2 review/card is unavailable")
            conn.commit()
            return None
        source_identity = require_telegram_source_context(
            conn,
            raw_intake_record_id=int(job["raw_intake_record_id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=context.actor_id,
                account_id=context.account_id,
                conversation_id=context.conversation_id,
                binding_id=context.binding_id,
                message_id=message_id,
            ),
        )
        if (
            row["source_kind"] != "initial_proposal_card"
            or row["initial_card_public_id"] is None
            or row["card_idempotency_key"] != f"initial:{_review_key(job_public_id)}"
            or row["proposal_public_id"] != proposal_public_id
            or int(row["raw_intake_record_id"] or 0) != int(job["raw_intake_record_id"])
            or row["admitted_source_message_id"] != message_id
            or not hmac.compare_digest(str(row["admitted_source_identity_sha256"]), source_identity)
            or row["proposal_content_hash"] != row["card_content_hash"]
            or int(row["expires_at"]) != int(row["card_expires_at"] or 0)
            or (
                row["authenticated_actor_id"],
                row["telegram_account_id"],
                row["telegram_conversation_id"],
                row["conversation_binding_id"],
            )
            != (context.actor_id, context.account_id, context.conversation_id, context.binding_id)
        ):
            raise CaptureReviewConflictError("Durable D2 review/card binding changed")
        attempt = conn.execute(
            "SELECT stage FROM d2_posting_attempts WHERE review_public_id = ?",
            (row["review_public_id"],),
        ).fetchone()
        if attempt is not None:
            status = get_status(
                conn, review_public_id=str(row["review_public_id"]), context=context
            )
            if status.state == "finalized":
                recover_capture_result(
                    conn,
                    job_public_id=job_public_id,
                    review_public_id=str(row["review_public_id"]),
                    context=context,
                )
            # The canonical D2 posting may still be progressing or require its
            # own attention. Never turn that accepted decision into an expired
            # unaccepted card, and leave reply enqueue to its existing locator.
            conn.commit()
            return job, None, True
        if int(row["expires_at"]) > int(time.time()) + _MIN_REVIEW_REMAINING_SECONDS:
            if job["status"] in {"needs_attention", "result_ready"}:
                raise CaptureReviewConflictError("Capture job review state changed")
            conn.commit()
            return None
        if job["status"] == "result_ready":
            raise CaptureReviewConflictError("Result-ready job has no accepted posting")
        if job["status"] != "needs_attention":
            updated = conn.execute(
                "UPDATE finance_capture_jobs SET status = 'needs_attention', "
                "last_error = 'review_expired', updated_at = CURRENT_TIMESTAMP "
                "WHERE public_id = ? AND status IN ('processing', 'awaiting_user') "
                "AND proposal_public_id = ? AND lease_owner IS NULL",
                (job_public_id, proposal_public_id),
            )
            if updated.rowcount != 1:
                raise CaptureReviewConflictError("Capture job changed during review expiry")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    final_job = get_capture_job(conn, public_id=job_public_id)
    assert final_job is not None
    return final_job, None, True


def _mark_review_requires_edit(
    conn: sqlite3.Connection, *, job_public_id: str, proposal_public_id: str
) -> dict[str, Any]:
    """Persist a queryable stop for proposals D2 cannot yet turn into cards."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        job = get_capture_job(conn, public_id=job_public_id)
        if (
            job is None
            or job["status"] != "processing"
            or job["proposal_public_id"] != proposal_public_id
            or job["lease_owner"] is not None
            or job["lease_expires_at"] is not None
        ):
            raise CaptureReviewConflictError("Capture job changed during review preparation")
        _source_context(conn, job)
        conn.execute(
            "UPDATE finance_capture_jobs SET status = 'needs_attention', "
            "last_error = 'review_requires_edit', "
            "updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
            (job_public_id,),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    updated = get_capture_job(conn, public_id=job_public_id)
    assert updated is not None
    return updated


def ensure_capture_review(
    conn: sqlite3.Connection, *, job_public_id: str
) -> tuple[dict[str, Any], PreparedPostingReview | None, bool]:
    """Persist or find the same D2 review, then verify before awaiting user."""
    require_staging_database(conn)
    if conn.in_transaction:
        raise RuntimeError("Capture review requires a connection without pending work")
    job = get_capture_job(conn, public_id=job_public_id)
    if (
        job is None
        or job["status"] not in {"processing", "awaiting_user", "needs_attention", "result_ready"}
        or not job["proposal_public_id"]
        or job["lease_owner"] is not None
        or job["lease_expires_at"] is not None
    ):
        raise CaptureReviewConflictError("Capture job is not ready for review")
    context, message_id = _source_context(conn, job)
    proposal_public_id = str(job["proposal_public_id"])
    existing = _existing_review_disposition(
        conn,
        job_public_id=job_public_id,
        proposal_public_id=proposal_public_id,
        context=context,
        message_id=message_id,
    )
    if existing is not None:
        return existing
    if job["status"] not in {"processing", "awaiting_user"}:
        raise CaptureReviewConflictError("Capture job is not ready for review")
    try:
        review = prepare_posting_review(
            conn,
            review_idempotency_key=_review_key(job_public_id),
            context=context,
            proposal_public_id=proposal_public_id,
            admitted_source_message_id=message_id,
        )
    except PostingAuthorityError as exc:
        # A D2 decision may have committed after the first read. Inspect the
        # same durable key before interpreting the preparer's error as a new
        # proposal that merely needs editing.
        existing = _existing_review_disposition(
            conn,
            job_public_id=job_public_id,
            proposal_public_id=proposal_public_id,
            context=context,
            message_id=message_id,
        )
        if existing is not None:
            return existing
        if str(exc) == "initial card expired":
            raise
        if str(exc) != "proposal is not eligible for D2 posting":
            raise
        if (
            conn.execute(
                "SELECT 1 FROM d2_posting_reviews WHERE review_idempotency_key = ?",
                (_review_key(job_public_id),),
            ).fetchone()
            is not None
        ):
            raise
        return (
            _mark_review_requires_edit(
                conn, job_public_id=job_public_id, proposal_public_id=proposal_public_id
            ),
            None,
            False,
        )
    try:
        updated = _promote_review_status(
            conn,
            job_public_id=job_public_id,
            proposal_public_id=proposal_public_id,
            review=review,
            context=context,
            message_id=message_id,
        )
    except CaptureReviewConflictError:
        existing = _existing_review_disposition(
            conn,
            job_public_id=job_public_id,
            proposal_public_id=proposal_public_id,
            context=context,
            message_id=message_id,
        )
        if existing is not None:
            return existing
        raise
    return updated, review, review.idempotent
