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

# Migration 050's immutable account/conversation/message UNIQUE index offers
# a bounded prefix lookup. Source-first join order avoids a global job scan;
# the query-plan test detects a planner regression without naming an internal
# SQLite autoindex whose ordinal might vary between platforms.
_HIGH_WATER_SQL = (
    "SELECT COALESCE(MAX(job.id), 0) "
    "FROM d2_telegram_source_contexts AS source "
    "CROSS JOIN finance_capture_jobs AS job "
    "WHERE source.telegram_account_id = ? AND source.telegram_conversation_id = ? "
    "AND source.authenticated_actor_id = ? AND source.conversation_binding_id = ? "
    "AND job.raw_intake_record_id = source.raw_intake_record_id"
)

_PAGE_SQL = (
    "SELECT job.id, job.public_id, job.raw_intake_record_id, "
    "intake.source_message_id, intake.source_channel, intake.external_source_id, "
    "source.source_identity_sha256 "
    "FROM finance_capture_jobs AS job "
    "CROSS JOIN d2_telegram_source_contexts AS source "
    "CROSS JOIN raw_intake_records AS intake "
    "WHERE job.id > ? AND job.id <= ? "
    "AND source.raw_intake_record_id = job.raw_intake_record_id "
    "AND intake.id = job.raw_intake_record_id "
    "AND source.authenticated_actor_id = ? AND source.telegram_account_id = ? "
    "AND source.telegram_conversation_id = ? AND source.conversation_binding_id = ? "
    "ORDER BY job.id LIMIT ?"
)

_CURSOR_SQL = (
    "SELECT job.id, job.public_id, job.raw_intake_record_id, "
    "intake.source_message_id, intake.source_channel, intake.external_source_id, "
    "source.source_identity_sha256 "
    "FROM finance_capture_jobs AS job "
    "JOIN d2_telegram_source_contexts AS source "
    "ON source.raw_intake_record_id = job.raw_intake_record_id "
    "JOIN raw_intake_records AS intake ON intake.id = job.raw_intake_record_id "
    "WHERE job.public_id = ? AND source.authenticated_actor_id = ? "
    "AND source.telegram_account_id = ? AND source.telegram_conversation_id = ? "
    "AND source.conversation_binding_id = ?"
)


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
        "telegram_message_id": message_id,
        "source_identity_sha256": digest,
    }


def _bound_cursor_id(conn: sqlite3.Connection, public_id: str, context: HumanActionContext) -> int:
    row = conn.execute(
        _CURSOR_SQL,
        (
            public_id,
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
        ),
    ).fetchone()
    if row is None:
        raise CaptureDiscoveryConflict("Capture discovery cursor is unavailable")
    _require_source(conn, row, context)
    return int(row["id"])


def list_capture_recovery_candidates(
    conn: sqlite3.Connection,
    *,
    context: HumanActionContext,
    after_job_public_id: str | None,
    through_job_public_id: str | None,
    limit: int,
) -> dict[str, Any]:
    """Scan one stable ID window; callers periodically restart at zero.

    Every saved job is a candidate, including an old job whose D2 acceptance,
    correction, or missing outbox row changes after its first scan. A candidate
    grants no processing, posting, or delivery authority.
    """
    require_staging_database(conn)
    if (
        (after_job_public_id is None) != (through_job_public_id is None)
        or after_job_public_id is not None
        and (not isinstance(after_job_public_id, str) or not after_job_public_id)
        or through_job_public_id is not None
        and (not isinstance(through_job_public_id, str) or not through_job_public_id)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_PAGE_SIZE
    ):
        raise ValueError("Invalid capture discovery page")
    if conn.in_transaction:
        raise RuntimeError("Capture discovery requires a fresh connection transaction")
    conn.execute("BEGIN")
    try:
        if through_job_public_id is None:
            after_id = 0
            through_id = int(
                conn.execute(
                    _HIGH_WATER_SQL,
                    (
                        context.account_id,
                        context.conversation_id,
                        context.actor_id,
                        context.binding_id,
                    ),
                ).fetchone()[0]
            )
            if through_id:
                high = conn.execute(
                    "SELECT public_id FROM finance_capture_jobs WHERE id = ?", (through_id,)
                ).fetchone()
                if high is None:
                    raise CaptureDiscoveryConflict("Capture discovery high water is unavailable")
                through_job_public_id = str(high[0])
                _bound_cursor_id(conn, through_job_public_id, context)
        else:
            assert after_job_public_id is not None
            after_id = _bound_cursor_id(conn, after_job_public_id, context)
            through_id = _bound_cursor_id(conn, through_job_public_id, context)
            if through_id < after_id:
                raise CaptureDiscoveryConflict("Capture discovery cursor order is invalid")
        rows = conn.execute(
            _PAGE_SQL,
            (
                after_id,
                through_id,
                context.actor_id,
                context.account_id,
                context.conversation_id,
                context.binding_id,
                limit + 1,
            ),
        ).fetchall()
        page = rows[:limit]
        candidates = [_require_source(conn, row, context) for row in page]
        next_after = str(page[-1]["public_id"]) if page else after_job_public_id
        conn.commit()
        return {
            "candidates": candidates,
            "after_job_public_id": next_after,
            "through_job_public_id": through_job_public_id,
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
