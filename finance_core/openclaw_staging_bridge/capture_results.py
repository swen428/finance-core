"""Read-only D2/D2b result recovery for one durable D3 capture job.

This module never confirms, resumes posting, applies corrections, or invokes a
provider. The caller supplies the trusted D2b verifier when corrections exist.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from finance_core.application.corrections import CorrectionService, EffectiveTransaction
from finance_core.intake.capture_jobs import get_capture_job
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.posting_authority import PostingStatus, get_status
from finance_core.staging_guard import require_staging_database


class CaptureResultUnavailable(ValueError):
    """The requested result cannot be tied to this job and verified."""


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
        if owned:
            conn.commit()
    except BaseException:
        if owned and conn.in_transaction:
            conn.rollback()
        raise


def _bound_review(
    conn: sqlite3.Connection,
    job_public_id: str,
    review_public_id: str,
    context: HumanActionContext,
) -> None:
    job = get_capture_job(conn, public_id=job_public_id)
    if job is None:
        raise CaptureResultUnavailable("Capture job not found")
    row = conn.execute(
        "SELECT reviews.authenticated_actor_id, reviews.telegram_account_id, "
        "reviews.telegram_conversation_id, reviews.conversation_binding_id "
        "FROM d2_posting_reviews AS reviews "
        "LEFT JOIN parser_human_draft_cards AS cards "
        "ON cards.card_generation_public_id = reviews.card_generation_public_id "
        "LEFT JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id "
        "LEFT JOIN d2_initial_proposal_cards AS initial "
        "ON initial.initial_card_public_id = reviews.initial_card_public_id "
        "WHERE reviews.review_public_id = ? AND "
        "((reviews.source_kind = 'd1_human_card' "
        "AND drafts.source_raw_intake_id = ?) OR "
        "(reviews.source_kind = 'initial_proposal_card' "
        "AND initial.raw_intake_record_id = ?))",
        (review_public_id, job["raw_intake_record_id"], job["raw_intake_record_id"]),
    ).fetchone()
    if row is None or tuple(row) != (
        context.actor_id,
        context.account_id,
        context.conversation_id,
        context.binding_id,
    ):
        raise CaptureResultUnavailable("Posting review is not bound to this capture and actor")


def _current(current: EffectiveTransaction) -> dict[str, object]:
    return {
        "transaction_public_id": current.target_id,
        "correction_public_id": (current.history[-1].correction_id if current.history else None),
        "correction_version": current.version,
        "amount": current.fields.amount,
        "currency": current.fields.currency,
        "transaction_date": current.fields.transaction_date,
        "merchant": current.fields.merchant,
    }


def recover_capture_result(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    review_public_id: str,
    context: HumanActionContext,
    correction_service: CorrectionService | None = None,
    correction_plan_id: str | None = None,
) -> dict[str, object]:
    """Verify a committed result and current effective head in one snapshot.

    An old correction is labelled historical; the current amount always comes
    from the D2b verifier. A missing or unprovable result raises and cannot be
    enqueued as a successful reply.
    """
    require_staging_database(conn)
    with _snapshot(conn):
        _bound_review(conn, job_public_id, review_public_id, context)
        status: PostingStatus = get_status(conn, review_public_id=review_public_id, context=context)
        attempt = conn.execute(
            "SELECT transaction_public_id FROM d2_posting_attempts "
            "WHERE review_public_id = ? AND stage = 'finalized'",
            (review_public_id,),
        ).fetchone()
        target = None if attempt is None else attempt[0]
        if not isinstance(target, str) or not target:
            raise CaptureResultUnavailable("No committed posting result exists")
        if correction_plan_id is not None:
            if correction_service is None or correction_service.connection is not conn:
                raise CaptureResultUnavailable("Trusted correction verifier is required")
            recovered = correction_service.recover(correction_plan_id)
            if recovered is None or recovered.current.target_id != target:
                raise CaptureResultUnavailable("Correction is absent or belongs to another job")
            return {
                "result_kind": "correction",
                "result_public_id": recovered.applied.correction_id,
                "review_public_id": review_public_id,
                "historical_correction_public_id": recovered.applied.correction_id,
                "historical_version": recovered.applied.version,
                "current": _current(recovered.current),
            }
        if status.state == "finalized" and status.transaction_public_id == target:
            return {
                "result_kind": "posting",
                "result_public_id": target,
                "review_public_id": review_public_id,
                "current": {
                    "transaction_public_id": target,
                    "correction_public_id": None,
                    "correction_version": 0,
                    "amount": status.amount,
                    "currency": status.currency,
                    "transaction_date": status.transaction_date,
                    "merchant": status.merchant,
                },
            }
        if status.attention_reason != "local_current_lookup_required":
            raise CaptureResultUnavailable("Posting result needs authority review")
        if correction_service is None or correction_service.connection is not conn:
            raise CaptureResultUnavailable("Trusted correction verifier is required")
        current = correction_service.lookup_in_snapshot(conn, target)
        if not current.history:
            raise CaptureResultUnavailable("Correction head is not provable")
        return {
            "result_kind": "posting",
            "result_public_id": target,
            "review_public_id": review_public_id,
            "current": _current(current),
        }
