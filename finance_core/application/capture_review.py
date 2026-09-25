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
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.posting_authority import (
    PostingAuthorityError,
    PreparedPostingReview,
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
            or int(row["expires_at"]) <= int(time.time())
        ):
            raise CaptureReviewConflictError("Durable review proposal is stale")
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
        or job["status"] not in {"processing", "awaiting_user"}
        or not job["proposal_public_id"]
        or job["lease_owner"] is not None
        or job["lease_expires_at"] is not None
    ):
        raise CaptureReviewConflictError("Capture job is not ready for review")
    context, message_id = _source_context(conn, job)
    proposal_public_id = str(job["proposal_public_id"])
    try:
        review = prepare_posting_review(
            conn,
            review_idempotency_key=_review_key(job_public_id),
            context=context,
            proposal_public_id=proposal_public_id,
            admitted_source_message_id=message_id,
        )
    except PostingAuthorityError as exc:
        if str(exc) != "proposal is not eligible for D2 posting":
            raise
        return (
            _mark_review_requires_edit(
                conn, job_public_id=job_public_id, proposal_public_id=proposal_public_id
            ),
            None,
            False,
        )
    updated = _promote_review_status(
        conn,
        job_public_id=job_public_id,
        proposal_public_id=proposal_public_id,
        review=review,
        context=context,
        message_id=message_id,
    )
    return updated, review, review.idempotent
