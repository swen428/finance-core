"""Bounded, read-only locators for authenticated D3 capture recovery."""

from __future__ import annotations

import sqlite3
from typing import Any

from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.staging_guard import require_staging_database
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    require_telegram_source_context,
)


class CaptureDiscoveryConflict(ValueError):
    """Saved source identity is inconsistent or unavailable."""


MAX_PAGE_SIZE = 100


def _require_source(
    conn: sqlite3.Connection, row: sqlite3.Row, context: HumanActionContext
) -> dict[str, Any]:
    message_id = str(row["source_message_id"] or "")
    if (
        not message_id
        or row["source_channel"] != "telegram"
        or row["external_source_id"] != f"telegram:{context.conversation_id}:{message_id}"
    ):
        raise CaptureDiscoveryConflict("Captured Telegram source is inconsistent")
    try:
        digest = require_telegram_source_context(
            conn,
            raw_intake_record_id=int(row["raw_intake_record_id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=context.actor_id,
                account_id=context.account_id,
                conversation_id=context.conversation_id,
                binding_id=context.binding_id,
                message_id=message_id,
            ),
        )
    except TelegramSourceContextError as exc:
        raise CaptureDiscoveryConflict("Captured Telegram identity is inconsistent") from exc
    if digest != row["source_identity_sha256"]:
        raise CaptureDiscoveryConflict("Captured Telegram identity digest differs")
    return {
        "job_public_id": str(row["public_id"]),
        "job_id": int(row["id"]),
        "telegram_message_id": message_id,
        "source_identity_sha256": digest,
    }


def list_capture_recovery_candidates(
    conn: sqlite3.Connection,
    *,
    context: HumanActionContext,
    after_job_id: int,
    through_job_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """Scan one stable ID window; callers periodically restart at zero.

    Every saved job is a candidate, including an old job whose D2 acceptance,
    correction, or missing outbox row changes after its first scan. A candidate
    grants no processing, posting, or delivery authority.
    """
    require_staging_database(conn)
    if (
        isinstance(after_job_id, bool)
        or not isinstance(after_job_id, int)
        or after_job_id < 0
        or (after_job_id > 0 and through_job_id is None)
        or through_job_id is not None
        and (
            isinstance(through_job_id, bool)
            or not isinstance(through_job_id, int)
            or through_job_id < after_job_id
        )
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_PAGE_SIZE
    ):
        raise ValueError("Invalid capture discovery page")
    if conn.in_transaction:
        raise RuntimeError("Capture discovery requires a fresh connection transaction")
    conn.execute("BEGIN")
    try:
        if through_job_id is None:
            through_job_id = int(
                conn.execute("SELECT COALESCE(MAX(id), 0) FROM finance_capture_jobs").fetchone()[0]
            )
        rows = conn.execute(
            "SELECT job.id, job.public_id, job.raw_intake_record_id, "
            "intake.source_message_id, intake.source_channel, intake.external_source_id, "
            "source.source_identity_sha256 "
            "FROM finance_capture_jobs AS job "
            "JOIN raw_intake_records AS intake ON intake.id = job.raw_intake_record_id "
            "JOIN d2_telegram_source_contexts AS source "
            "ON source.raw_intake_record_id = intake.id "
            "WHERE job.id > ? AND job.id <= ? "
            "AND source.authenticated_actor_id = ? AND source.telegram_account_id = ? "
            "AND source.telegram_conversation_id = ? AND source.conversation_binding_id = ? "
            "ORDER BY job.id LIMIT ?",
            (
                after_job_id,
                through_job_id,
                context.actor_id,
                context.account_id,
                context.conversation_id,
                context.binding_id,
                limit + 1,
            ),
        ).fetchall()
        page = rows[:limit]
        candidates = [_require_source(conn, row, context) for row in page]
        next_after = int(page[-1]["id"]) if page else after_job_id
        conn.commit()
        return {
            "candidates": candidates,
            "after_job_id": next_after,
            "through_job_id": through_job_id,
            "has_more": len(rows) > limit,
        }
    except BaseException:
        conn.rollback()
        raise


def get_capture_job_for_message(
    conn: sqlite3.Connection,
    *,
    context: HumanActionContext,
    telegram_message_id: str,
) -> dict[str, Any] | None:
    """Locate only the original capture in this complete saved context."""
    require_staging_database(conn)
    if not telegram_message_id or not telegram_message_id.isdecimal():
        raise ValueError("Invalid original Telegram message ID")
    rows = conn.execute(
        "SELECT job.id, job.public_id, job.raw_intake_record_id, "
        "intake.source_message_id, intake.source_channel, intake.external_source_id, "
        "source.source_identity_sha256 "
        "FROM d2_telegram_source_contexts AS source "
        "JOIN raw_intake_records AS intake ON intake.id = source.raw_intake_record_id "
        "JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = intake.id "
        "WHERE source.authenticated_actor_id = ? AND source.telegram_account_id = ? "
        "AND source.telegram_conversation_id = ? AND source.conversation_binding_id = ? "
        "AND source.source_message_id = ? LIMIT 2",
        (
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            telegram_message_id,
        ),
    ).fetchall()
    if len(rows) > 1:
        raise CaptureDiscoveryConflict("Original message has ambiguous capture jobs")
    return _require_source(conn, rows[0], context) if rows else None
