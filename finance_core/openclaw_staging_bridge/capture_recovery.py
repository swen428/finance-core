"""Authenticated, one-step local recovery for a durable D3 capture.

The capture job is a locator, not financial authority.  D2 owns accepted
posting attempts, and the existing result verifier owns committed facts.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterator

from finance_core.application.corrections import CorrectionService
from finance_core.intake.capture_jobs import get_capture_job
from finance_core.intake.interaction_routes import get_interaction_route
from finance_core.openclaw_staging_bridge.capture_reply_outbox import ensure_result_reply
from finance_core.openclaw_staging_bridge.capture_results import (
    CaptureResultUnavailable,
    recover_capture_result,
)
from finance_core.openclaw_staging_bridge.capture_review import ensure_capture_review
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals.human_draft_delivery import get_human_draft_card
from finance_core.parser_proposals.human_drafts import HumanDraftContext, HumanDraftError
from finance_core.posting_authority import get_status, prepare_posting_review, resume_posting
from finance_core.staging_guard import require_staging_database
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    require_telegram_source_context,
)


class CaptureRecoveryUnavailable(ValueError):
    """The requested job, frozen route, or complete source identity is untrusted."""


class CaptureRecoveryConflict(RuntimeError):
    """Durable recovery evidence is ambiguous or changed."""


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
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


def _source(
    conn: sqlite3.Connection, job: dict[str, Any], context: HumanActionContext
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    intake = conn.execute(
        "SELECT source_type, source_channel, external_source_id, source_message_id, "
        "raw_input, parser_output_id FROM raw_intake_records WHERE id = ?",
        (job["raw_intake_record_id"],),
    ).fetchone()
    if intake is None:
        raise CaptureRecoveryUnavailable("Captured source is missing")
    source = dict(intake)
    message_id = str(source["source_message_id"] or "")
    if (
        source["source_channel"] != "telegram"
        or not message_id
        or source["external_source_id"] != f"telegram:{context.conversation_id}:{message_id}"
    ):
        raise CaptureRecoveryUnavailable("Captured Telegram source is inconsistent")
    try:
        require_telegram_source_context(
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
    except TelegramSourceContextError as exc:
        raise CaptureRecoveryUnavailable("Capture identity does not match") from exc
    route = get_interaction_route(conn, str(job["public_id"]))
    if route is not None:
        raw = source["raw_input"]
        if (
            job["capture_kind"] != "text"
            or not isinstance(raw, str)
            or route["raw_text_sha256"] != hashlib.sha256(raw.encode()).hexdigest()
            or str(route["telegram_message_id"]) != message_id
            or (
                route["authenticated_actor_id"],
                route["telegram_account_id"],
                route["telegram_conversation_id"],
                route["conversation_binding_id"],
            )
            != (context.actor_id, context.account_id, context.conversation_id, context.binding_id)
        ):
            raise CaptureRecoveryUnavailable("Frozen interaction route differs from source")
    elif job["capture_kind"] == "text":
        # Old captured proposals admitted by migration 055 are the only text
        # records without a route that may enter this recovery service.
        legacy = conn.execute(
            "SELECT admission.admitted_parser_output_id "
            "FROM finance_legacy_text_lineage_admissions admission "
            "JOIN parser_outputs proposal "
            "ON proposal.id = admission.admitted_parser_output_id "
            "AND proposal.source_public_id = admission.source_public_id "
            "WHERE admission.raw_intake_record_id = ? AND admission.source_public_id = ?",
            (job["raw_intake_record_id"], job["intake_public_id"]),
        ).fetchone()
        if legacy is None:
            raise CaptureRecoveryUnavailable("Text capture has no frozen route or legacy admission")
    return source, route


def _source_job_for_control(
    conn: sqlite3.Connection, route: dict[str, Any], control_job: dict[str, Any]
) -> dict[str, Any] | None:
    kind = str(route["route_kind"])
    if kind == "initial_intake":
        return control_job
    if kind == "whole_card":
        row = conn.execute(
            "SELECT d.source_raw_intake_id FROM parser_human_draft_cards c "
            "JOIN parser_human_drafts d ON d.id = c.draft_id "
            "WHERE c.card_generation_public_id = ?",
            (route["card_generation_public_id"],),
        ).fetchone()
    elif kind in {"guided_update", "guided_complete"}:
        anchor = conn.execute(
            "SELECT ref.parser_output_id, proposal.source_public_id "
            "FROM openclaw_guided_edit_sessions s "
            "JOIN openclaw_human_action_references ref ON ref.id = s.source_reference_id "
            "JOIN parser_outputs proposal ON proposal.id = ref.parser_output_id "
            "WHERE s.session_public_id = ?",
            (route["guided_session_public_id"],),
        ).fetchone()
        if anchor is None:
            raise CaptureRecoveryConflict("Frozen guided session has no source reference")
        rows = conn.execute(
            "SELECT id FROM raw_intake_records WHERE public_id = ? "
            "UNION SELECT source_raw_intake_id FROM parser_human_drafts "
            "WHERE source_raw_intake_id IS NOT NULL AND "
            "(source_parser_output_id = ? OR current_parser_output_id = ? "
            "OR decision_target_parser_output_id = ?)",
            (
                anchor["source_public_id"],
                anchor["parser_output_id"],
                anchor["parser_output_id"],
                anchor["parser_output_id"],
            ),
        ).fetchall()
        if len(rows) != 1:
            raise CaptureRecoveryConflict("Guided source lineage is missing or ambiguous")
        row = rows[0]
    else:
        return None
    if row is None or row[0] is None:
        raise CaptureRecoveryConflict("Frozen D1 card has no original source")
    source = conn.execute(
        "SELECT public_id FROM finance_capture_jobs WHERE raw_intake_record_id = ?",
        (row[0],),
    ).fetchone()
    if source is None:
        raise CaptureRecoveryConflict("Frozen D1 card has no source capture job")
    return get_capture_job(conn, public_id=str(source[0]))


def _interaction_outcome(conn: sqlite3.Connection, route: dict[str, Any] | None) -> str:
    if route is None or route["route_kind"] == "initial_intake":
        return "initial_intake"
    kind = str(route["route_kind"])
    if kind == "control_refused":
        return "refused"
    if kind == "whole_card":
        row = conn.execute(
            "SELECT operation_outcome FROM parser_human_draft_operations "
            "WHERE operation_public_id = ?",
            (route["operation_key"],),
        ).fetchone()
        return "pending" if row is None else str(row[0])
    if kind in {"guided_update", "guided_complete"}:
        row = conn.execute(
            "SELECT e.event_type, e.field_name, e.field_value_json "
            "FROM openclaw_guided_edit_events e "
            "JOIN openclaw_guided_edit_sessions s ON s.id = e.session_id "
            "WHERE s.session_public_id = ? AND e.telegram_message_id = ? "
            "ORDER BY e.sequence_number DESC LIMIT 1",
            (route["guided_session_public_id"], route["telegram_message_id"]),
        ).fetchone()
        if row is None:
            return "pending"
        if kind == "guided_update":
            if (
                row["event_type"] not in {"update_requested", "update_applied", "update_refused"}
                or row["field_name"] != route["field_name"]
                or row["field_value_json"] != route["field_value_json"]
            ):
                raise CaptureRecoveryConflict("Guided event differs from frozen operation")
        elif row["event_type"] != "completed":
            raise CaptureRecoveryConflict("Guided completion differs from frozen operation")
        return str(row["event_type"])
    raise CaptureRecoveryConflict("Unknown frozen route")


def _guided_completion_batch(
    conn: sqlite3.Connection, route: dict[str, Any]
) -> tuple[str | None, bool]:
    """Project the same usable review generation that guided completion claims."""
    from finance_core.openclaw_staging_bridge.guided_edit import (
        REVIEW_REFERENCE_MIN_REMAINING_SECONDS,
    )

    session = conn.execute(
        "SELECT id, status, completed_message_id FROM openclaw_guided_edit_sessions "
        "WHERE session_public_id = ?",
        (route["guided_session_public_id"],),
    ).fetchone()
    if (
        session is None
        or session["status"] != "completed"
        or session["completed_message_id"] != route["telegram_message_id"]
    ):
        raise CaptureRecoveryConflict("Guided completion session differs from frozen route")
    latest = conn.execute(
        "SELECT generations.reference_batch_id, COUNT(refs.id) AS reference_count, "
        "MIN(refs.expires_at) AS min_reference_expiry, "
        "COUNT(redemptions.id) AS redemption_count "
        "FROM openclaw_guided_edit_review_generations generations "
        "LEFT JOIN openclaw_human_action_references refs "
        "ON refs.issuance_idempotency_key = "
        "'bridge-human-action-issue:' || generations.reference_batch_id "
        "LEFT JOIN openclaw_human_action_redemptions redemptions "
        "ON redemptions.reference_id = refs.id "
        "WHERE generations.session_id = ? GROUP BY generations.id "
        "ORDER BY generations.generation DESC LIMIT 1",
        (session["id"],),
    ).fetchone()
    if latest is None:
        return None, True
    reusable = int(latest["reference_count"]) == 0 or (
        int(latest["redemption_count"]) == 0
        and int(latest["min_reference_expiry"])
        > int(time.time()) + REVIEW_REFERENCE_MIN_REMAINING_SECONDS
    )
    return str(latest["reference_batch_id"]), not reusable


def _reviews(conn: sqlite3.Connection, source_job: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.review_public_id, r.source_kind, r.parser_output_id, "
        "r.card_generation_public_id, proposal.public_id AS proposal_public_id, "
        "a.attempt_public_id, a.stage, a.transaction_public_id "
        "FROM d2_posting_reviews r "
        "JOIN parser_outputs proposal ON proposal.id = r.parser_output_id "
        "LEFT JOIN d2_posting_attempts a ON a.review_public_id = r.review_public_id "
        "LEFT JOIN parser_human_draft_cards c "
        "ON c.card_generation_public_id = r.card_generation_public_id "
        "LEFT JOIN parser_human_drafts d ON d.id = c.draft_id "
        "LEFT JOIN d2_initial_proposal_cards i "
        "ON i.initial_card_public_id = r.initial_card_public_id "
        "WHERE (r.source_kind = 'd1_human_card' AND d.source_raw_intake_id = ?) "
        "OR (r.source_kind = 'initial_proposal_card' AND i.raw_intake_record_id = ?)",
        (source_job["raw_intake_record_id"], source_job["raw_intake_record_id"]),
    ).fetchall()
    return [dict(row) for row in rows]


def _child_review_candidate(
    conn: sqlite3.Connection,
    *,
    job: dict[str, Any],
    context: HumanActionContext,
    original_message_id: str,
) -> tuple[str, str, str] | None:
    """Find a current D1 card or sealed AI child without changing job identity."""
    drafts = conn.execute(
        "SELECT d.current_card_generation_public_id, d.state, d.expires_at, "
        "c.expires_at AS card_expires_at, op.result_completeness "
        "FROM parser_human_drafts d "
        "JOIN parser_human_draft_cards c "
        "ON c.card_generation_public_id = d.current_card_generation_public_id "
        "JOIN parser_human_draft_operations op ON op.id = c.original_operation_id "
        "WHERE d.source_raw_intake_id = ? AND d.state = 'active' "
        "AND d.authenticated_actor_id = ? "
        "AND d.telegram_account_id = ? AND d.telegram_conversation_id = ? "
        "AND d.conversation_binding_id = ?",
        (
            job["raw_intake_record_id"],
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
        ),
    ).fetchall()
    if len(drafts) > 1:
        raise CaptureRecoveryConflict("Multiple D1 child cards claim one source")
    if drafts:
        card = drafts[0]
        now = int(time.time())
        if (
            card["state"] == "active"
            and card["result_completeness"] == "complete"
            and int(card["expires_at"]) > now
            and int(card["card_expires_at"]) > now
        ):
            return "d1_human_card", str(card["current_card_generation_public_id"]), ""
        return None
    if job["capture_kind"] != "text" or job["ai_attempt_public_id"] is None:
        return None
    child = conn.execute(
        "SELECT child.public_id FROM ai_fallback_attempts attempt "
        "JOIN parser_outputs parent ON parent.id = attempt.parent_parser_output_id "
        "JOIN ai_fallback_results result ON result.attempt_id = attempt.id "
        "AND result.result_status = 'proposal_created' "
        "JOIN ai_fallback_proposal_links link ON link.result_id = result.id "
        "JOIN parser_outputs child ON child.id = link.parser_output_id "
        "AND child.parent_parser_output_id = parent.id "
        "JOIN raw_intake_records raw ON raw.id = attempt.raw_intake_record_id "
        "AND raw.parser_output_id = child.id "
        "WHERE attempt.raw_intake_record_id = ? AND attempt.attempt_public_id = ? "
        "AND parent.public_id = ? AND raw.source_message_id = ? "
        "AND child.parse_status = 'parsed_pending_confirmation'",
        (
            job["raw_intake_record_id"],
            job["ai_attempt_public_id"],
            job["proposal_public_id"],
            original_message_id,
        ),
    ).fetchone()
    if child is None:
        return None
    return "ai_initial_proposal", str(child["public_id"]), original_message_id


def _recovery(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    context: HumanActionContext,
    correction_service: CorrectionService | None,
) -> dict[str, object]:
    require_staging_database(conn)
    if not isinstance(context, HumanActionContext):
        raise CaptureRecoveryUnavailable("Complete human context is required")
    job = get_capture_job(conn, public_id=job_public_id)
    if job is None:
        raise CaptureRecoveryUnavailable("Capture job not found")
    intake, route = _source(conn, job, context)
    route_kind = (
        "receipt_intake"
        if route is None and job["capture_kind"] == "receipt_image"
        else ("legacy_initial_intake" if route is None else str(route["route_kind"]))
    )
    response: dict[str, object] = {
        "job_public_id": job_public_id,
        "source_job_public_id": None,
        "route_kind": route_kind,
        "interaction_outcome": _interaction_outcome(conn, route),
        "financial_state": "unposted",
        "capture_status": job["status"],
        "ai_status": job["ai_status"],
        "proposal_public_id": job["proposal_public_id"],
        "ocr_retry_count": job["ocr_retry_count"],
        "ocr_retry_not_before_ms": job["ocr_retry_not_before_ms"],
        "last_error": job["last_error"],
        "route_operation_key": None if route is None else route["operation_key"],
        "route_session_public_id": None if route is None else route["guided_session_public_id"],
        "route_message_id": None if route is None else route["telegram_message_id"],
        "route_card_public_id": None if route is None else route["card_generation_public_id"],
        "next_action": "none",
        "review_public_id": None,
        "attempt_public_id": None,
        "result": None,
        "reply_outbox": [],
    }
    if route is not None and route_kind == "control_refused":
        response["financial_state"] = "unknown"
        response["next_action"] = "attention_required"
        return response
    source_job = job if route is None else _source_job_for_control(conn, route, job)
    if source_job is None:
        response["next_action"] = "d1_command_required"
        return response
    response["source_job_public_id"] = source_job["public_id"]
    source_intake = intake if source_job is job else _source(conn, source_job, context)[0]
    if route_kind == "whole_card" and route is not None:
        response["interaction_operation_public_id"] = route["operation_key"]
        if response["interaction_outcome"] != "pending":
            try:
                control_result = get_human_draft_card(
                    conn,
                    context=HumanDraftContext(
                        authenticated_actor_id=context.actor_id,
                        telegram_account_id=context.account_id,
                        telegram_conversation_id=context.conversation_id,
                        conversation_binding_id=context.binding_id,
                    ),
                    operation_public_id=str(route["operation_key"]),
                )
            except HumanDraftError as exc:
                raise CaptureRecoveryConflict("D1 control result cannot be verified") from exc
            bound = conn.execute(
                "SELECT 1 FROM parser_human_drafts WHERE draft_public_id = ? "
                "AND source_raw_intake_id = ?",
                (control_result.draft_public_id, source_job["raw_intake_record_id"]),
            ).fetchone()
            if bound is None:
                raise CaptureRecoveryConflict("D1 result is not bound to source job")
            response["interaction_card_public_id"] = control_result.card_generation_public_id
            response["interaction_result"] = asdict(control_result)
    reviews = _reviews(conn, source_job)
    # A superseded initial review may coexist with a D1 child. A committed or
    # accepted decision wins; never select a second review by row order.
    ranked = sorted(
        reviews,
        key=lambda r: (r["attempt_public_id"] is not None, r["source_kind"] == "d1_human_card"),
        reverse=True,
    )
    if len(ranked) > 1 and ranked[0]["attempt_public_id"] and ranked[1]["attempt_public_id"]:
        raise CaptureRecoveryConflict("Multiple accepted reviews claim one source")
    review = ranked[0] if ranked else None
    if review is not None and review["attempt_public_id"] is None:
        candidate = _child_review_candidate(
            conn,
            job=source_job,
            context=context,
            original_message_id=str(source_intake["source_message_id"]),
        )
        if candidate is not None:
            kind, target, _message = candidate
            matching = [
                row
                for row in ranked
                if row["attempt_public_id"] is None
                and (
                    kind == "d1_human_card"
                    and row["source_kind"] == "d1_human_card"
                    and row["card_generation_public_id"] == target
                    or kind == "ai_initial_proposal"
                    and row["source_kind"] == "initial_proposal_card"
                    and row["proposal_public_id"] == target
                )
            ]
            if len(matching) > 1:
                raise CaptureRecoveryConflict("Multiple current child reviews claim one source")
            review = matching[0] if matching else None
    if (
        route_kind == "guided_complete"
        and route is not None
        and response["interaction_outcome"] == "completed"
    ):
        batch_id, needs_claim = _guided_completion_batch(conn, route)
        response["interaction_review_batch_id"] = batch_id
        if needs_claim and (review is None or review["attempt_public_id"] is None):
            response["next_action"] = "guided_command_required"
            return response
    if review is None:
        if route_kind in {"initial_intake", "legacy_initial_intake", "receipt_intake"}:
            linked = (
                conn.execute(
                    "SELECT p.id FROM parser_outputs p JOIN raw_intake_records raw "
                    "ON raw.parser_output_id = p.id "
                    "WHERE raw.id = ? AND p.public_id = ? "
                    "AND p.source_public_id = raw.public_id",
                    (job["raw_intake_record_id"], job["proposal_public_id"]),
                ).fetchone()
                if job["proposal_public_id"]
                else None
            )
            child = conn.execute(
                "SELECT 1 FROM parser_human_drafts "
                "WHERE source_raw_intake_id = ? OR source_parser_output_id = ? LIMIT 1",
                (job["raw_intake_record_id"], None if linked is None else linked[0]),
            ).fetchone()
            candidate = (
                _child_review_candidate(
                    conn,
                    job=job,
                    context=context,
                    original_message_id=str(intake["source_message_id"]),
                )
                if child is not None or job["ai_attempt_public_id"] is not None
                else None
            )
            if candidate is not None:
                response["next_action"] = "prepare_child_review"
                response["child_review_kind"] = candidate[0]
                response["child_review_target"] = candidate[1]
                response["child_source_message_id"] = candidate[2]
                return response
            if job["ai_attempt_public_id"] is not None:
                ai = conn.execute(
                    "SELECT result.result_status, claim.id AS claim_id "
                    "FROM ai_fallback_attempts attempt "
                    "LEFT JOIN ai_fallback_invocation_claims claim "
                    "ON claim.attempt_id = attempt.id "
                    "LEFT JOIN ai_fallback_results result ON result.attempt_id = attempt.id "
                    "WHERE attempt.raw_intake_record_id = ? AND attempt.attempt_public_id = ?",
                    (job["raw_intake_record_id"], job["ai_attempt_public_id"]),
                ).fetchone()
                if ai is None:
                    raise CaptureRecoveryConflict("Bound AI attempt is missing")
                response["ai_result_status"] = ai["result_status"]
                response["next_action"] = (
                    "ai_outcome_unknown"
                    if ai["result_status"] is None and ai["claim_id"] is not None
                    else "ai_prepared_attention"
                    if ai["result_status"] is None
                    else "ai_child_lineage_attention"
                    if ai["result_status"] == "proposal_created"
                    else "ai_non_child_result"
                )
                return response
            response["next_action"] = (
                "ai_outcome_unknown"
                if job["ai_status"] == "outcome_unknown"
                else "child_review_required"
                if child is not None or job["ai_attempt_public_id"] is not None
                else "prepare_initial_review"
                if linked is not None
                and job["ai_attempt_public_id"] is None
                and job["status"] in {"processing", "awaiting_user"}
                else "attention_required"
                if job["status"] in {"needs_attention", "result_ready"}
                else "capture_retry_deferred"
                if int(job["ocr_retry_not_before_ms"]) > time.time_ns() // 1_000_000
                else "capture_in_progress"
                if job["lease_expires_at"] is not None
                and int(job["lease_expires_at"]) > time.time_ns() // 1_000_000
                else "attention_required"
                if job["proposal_public_id"]
                else "capture_processing_required"
            )
        else:
            if route_kind in {"guided_update", "guided_complete"} and response[
                "interaction_outcome"
            ] in {"pending", "update_requested"}:
                response["next_action"] = "guided_command_required"
            elif route_kind == "whole_card" and response["interaction_outcome"] == "pending":
                response["next_action"] = "d1_command_required"
            else:
                candidate = _child_review_candidate(
                    conn,
                    job=source_job,
                    context=context,
                    original_message_id=str(source_intake["source_message_id"]),
                )
                if candidate is not None:
                    response["next_action"] = "prepare_child_review"
                    response["child_review_kind"] = candidate[0]
                    response["child_review_target"] = candidate[1]
                    response["child_source_message_id"] = candidate[2]
                else:
                    response["next_action"] = "none"
        return response
    review_id = str(review["review_public_id"])
    response["review_public_id"] = review_id
    response["attempt_public_id"] = review["attempt_public_id"]
    status = get_status(conn, review_public_id=review_id, context=context)
    response["posting_state"] = status.state
    response["financial_state"] = status.state
    response["attention_reason"] = status.attention_reason
    if status.state == "finalized" and review["stage"] != "finalized":
        response["next_action"] = "resume_accepted_posting"
        return response
    if status.state == "finalized" or status.attention_reason == "local_current_lookup_required":
        try:
            result = recover_capture_result(
                conn,
                job_public_id=str(source_job["public_id"]),
                review_public_id=review_id,
                context=context,
                correction_service=correction_service,
            )
        except CaptureResultUnavailable:
            response["next_action"] = "trusted_result_verifier_required"
            return response
        response["result"] = result
        outbox = [
            dict(row)
            for row in conn.execute(
                "SELECT public_id, result_kind, result_public_id, status, send_attempt_count "
                "FROM finance_capture_reply_outbox WHERE job_public_id = ? "
                "ORDER BY created_at, public_id",
                (source_job["public_id"],),
            )
        ]
        response["reply_outbox"] = outbox
        correction_rows = conn.execute(
            "SELECT plan_id, correction_id FROM correction_versions "
            "WHERE target_id = ? ORDER BY version",
            (result["result_public_id"],),
        ).fetchall()
        response["financial_state"] = "corrected" if correction_rows else "finalized"
        candidates: list[tuple[str, str, str | None]] = [
            ("posting", str(result["result_public_id"]), None),
            *(
                ("correction", str(row["correction_id"]), str(row["plan_id"]))
                for row in correction_rows
            ),
        ]
        existing = {(row["result_kind"], row["result_public_id"]) for row in outbox}
        for kind, identity, plan in candidates:
            if (kind, identity) not in existing:
                response["next_action"] = "enqueue_existing_result"
                response["missing_result_kind"] = kind
                response["missing_result_public_id"] = identity
                response["correction_plan_id"] = plan
                return response
        response["next_action"] = (
            "guided_command_required"
            if route_kind in {"guided_update", "guided_complete"}
            and response["interaction_outcome"] in {"pending", "update_requested"}
            else "d1_command_required"
            if route_kind == "whole_card" and response["interaction_outcome"] == "pending"
            else "none"
        )
        return response
    if status.state == "posting" and status.attempt_public_id:
        response["next_action"] = "resume_accepted_posting"
    elif (
        status.attention_reason == "review_stale_after_edit"
        and route_kind == "whole_card"
        and response["interaction_outcome"] in {"pending", "update_requested"}
    ):
        response["next_action"] = "d1_command_required"
    elif status.state == "needs_attention":
        response["next_action"] = "attention_required"
    elif (
        route_kind in {"guided_update", "guided_complete"}
        and response["interaction_outcome"] == "pending"
    ):
        response["next_action"] = "guided_command_required"
    elif route_kind == "whole_card" and response["interaction_outcome"] == "pending":
        response["next_action"] = "d1_command_required"
    else:
        response["next_action"] = "await_human_confirmation"
    return response


def get_capture_recovery(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    context: HumanActionContext,
    correction_service: CorrectionService | None = None,
) -> dict[str, object]:
    """Read a verified recovery view without creating authority or a reply."""
    with _read_snapshot(conn):
        return _recovery(
            conn,
            job_public_id=job_public_id,
            context=context,
            correction_service=correction_service,
        )


def resume_capture_recovery(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    context: HumanActionContext,
    correction_service: CorrectionService | None = None,
    expected_view: dict[str, object] | None = None,
) -> dict[str, object]:
    """Advance at most one already-local stage, then return fresh status."""
    if conn.in_transaction:
        raise CaptureRecoveryConflict("Recovery requires a fresh connection transaction")
    before = get_capture_recovery(
        conn, job_public_id=job_public_id, context=context, correction_service=correction_service
    )
    if expected_view is not None and before != expected_view:
        before["performed_action"] = "none"
        before["stale_recovery_step"] = True
        return before
    action = before["next_action"]
    source_job_id = before["source_job_public_id"]
    if action == "prepare_initial_review" and isinstance(source_job_id, str):
        ensure_capture_review(conn, job_public_id=source_job_id)
    elif action == "prepare_child_review":
        child_kind = before.get("child_review_kind")
        target = before.get("child_review_target")
        if not isinstance(target, str):
            raise CaptureRecoveryConflict("Child review target is missing")
        if child_kind == "d1_human_card":
            prepare_posting_review(
                conn,
                review_idempotency_key=f"bridge-d3-d1-child-review:{target}",
                context=context,
                card_generation_public_id=target,
            )
        elif child_kind == "ai_initial_proposal":
            message_id = before.get("child_source_message_id")
            if not isinstance(message_id, str) or not message_id:
                raise CaptureRecoveryConflict("AI child source message is missing")
            prepare_posting_review(
                conn,
                review_idempotency_key=f"bridge-d3-ai-child-review:{source_job_id}",
                context=context,
                proposal_public_id=target,
                admitted_source_message_id=message_id,
            )
        else:
            raise CaptureRecoveryConflict("Child review kind is unsupported")
    elif action == "resume_accepted_posting":
        attempt_id = before["attempt_public_id"]
        if not isinstance(attempt_id, str):
            raise CaptureRecoveryConflict("Accepted attempt identity is missing")
        resume_posting(conn, attempt_public_id=attempt_id, context=context)
    elif action == "enqueue_existing_result" and isinstance(source_job_id, str):
        review_id = before["review_public_id"]
        if not isinstance(review_id, str):
            raise CaptureRecoveryConflict("Committed review identity is missing")
        plan = before.get("correction_plan_id")
        ensure_result_reply(
            conn,
            job_public_id=source_job_id,
            review_public_id=review_id,
            context=context,
            correction_service=correction_service,
            correction_plan_id=plan if isinstance(plan, str) else None,
        )
    after = get_capture_recovery(
        conn, job_public_id=job_public_id, context=context, correction_service=correction_service
    )
    after["performed_action"] = (
        action
        if action
        in {
            "prepare_initial_review",
            "prepare_child_review",
            "resume_accepted_posting",
            "enqueue_existing_result",
        }
        else "none"
    )
    return after
