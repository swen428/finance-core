"""Transaction boundaries for D1 card delivery observation and bounded reissue."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from typing import cast

from finance_core.parser_proposals.human_drafts import (
    HumanDraftContext,
    HumanDraftError,
    HumanDraftResult,
    _begin_immediate,
    _context_from_row,
    _encode_material,
    _prefixed_id,
    _result_for,
    _valid_hash,
)


def _framed_hash(domain: str, *fields: str) -> str:
    payload = domain.encode("ascii") + b"\x00" + len(fields).to_bytes(4, "big")
    for field in fields:
        encoded = field.encode("utf-8")
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def _load_card_and_draft(
    conn: sqlite3.Connection, card_public_id: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    card = conn.execute(
        "SELECT * FROM parser_human_draft_cards WHERE card_generation_public_id = ?",
        (card_public_id,),
    ).fetchone()
    if card is None:
        raise HumanDraftError("card_missing")
    draft = conn.execute(
        "SELECT * FROM parser_human_drafts WHERE id = ?", (card["draft_id"],)
    ).fetchone()
    if draft is None:
        raise HumanDraftError("draft_missing")
    return card, draft


def _require_context(draft: sqlite3.Row, context: HumanDraftContext) -> None:
    if _context_from_row(draft) != context:
        raise HumanDraftError("actor_or_context_mismatch")


def _origin_operation(conn: sqlite3.Connection, card: sqlite3.Row) -> sqlite3.Row:
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE id = ?",
        (card["original_operation_id"],),
    ).fetchone()
    if operation is None:
        raise HumanDraftError("operation_missing")
    return operation


def get_human_draft_card(
    conn: sqlite3.Connection,
    *,
    context: HumanDraftContext,
    operation_public_id: str | None = None,
    source_edit_reference_public_id: str | None = None,
    card_generation_public_id: str | None = None,
    attempt_public_id: str | None = None,
) -> HumanDraftResult:
    """Resolve a historical result only from mutually consistent durable identities."""
    if not any(
        (
            operation_public_id,
            source_edit_reference_public_id,
            card_generation_public_id,
            attempt_public_id,
        )
    ):
        raise HumanDraftError("durable_identity_required")
    candidates: list[str] = []
    selected_operation: sqlite3.Row | None = None
    selected_attempt: sqlite3.Row | None = None
    if operation_public_id is not None:
        selected_operation = conn.execute(
            "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
            (operation_public_id,),
        ).fetchone()
        if selected_operation is None:
            raise HumanDraftError("operation_missing")
        candidates.append(str(selected_operation["result_card_generation_public_id"]))
    if source_edit_reference_public_id is not None:
        row = conn.execute(
            """
            SELECT operations.*
            FROM parser_human_draft_operations AS operations
            JOIN parser_human_drafts AS drafts ON drafts.id = operations.draft_id
            WHERE drafts.source_reference_public_id = ? AND operations.operation_type = 'start'
            """,
            (source_edit_reference_public_id,),
        ).fetchone()
        if row is None:
            raise HumanDraftError("source_reference_missing")
        if selected_operation is not None and selected_operation["id"] != row["id"]:
            raise HumanDraftError("durable_identity_mismatch")
        selected_operation = row
        candidates.append(str(row["result_card_generation_public_id"]))
    if card_generation_public_id is not None:
        candidates.append(card_generation_public_id)
    if attempt_public_id is not None:
        row = conn.execute(
            "SELECT * FROM parser_human_draft_card_delivery_attempts WHERE attempt_public_id = ?",
            (attempt_public_id,),
        ).fetchone()
        if row is None:
            raise HumanDraftError("attempt_missing")
        selected_attempt = row
        candidates.append(str(row["card_generation_public_id"]))
    if len(set(candidates)) != 1:
        raise HumanDraftError("durable_identity_mismatch")
    card, draft = _load_card_and_draft(conn, candidates[0])
    _require_context(draft, context)
    if selected_attempt is not None and _context_from_row(selected_attempt) != context:
        raise HumanDraftError("attempt_context")
    origin = _origin_operation(conn, card)
    if selected_operation is None:
        selected_operation = origin
    elif (
        int(selected_operation["draft_id"]) != int(draft["id"])
        or selected_operation["result_card_generation_public_id"]
        != card["card_generation_public_id"]
        or _context_from_row(selected_operation) != context
    ):
        raise HumanDraftError("durable_identity_mismatch")
    return _result_for(
        conn,
        draft,
        selected_operation,
        card_public_id=str(card["card_generation_public_id"]),
    )


def begin_human_draft_card_delivery(
    conn: sqlite3.Connection,
    *,
    context: HumanDraftContext,
    card_generation_public_id: str,
    attempt_public_id: str,
    delivery_material_hash: str,
    transport_mode: str,
    outbound_target_message_id: str | None,
    now_epoch: int,
) -> str:
    if transport_mode not in {"replace", "reply"}:
        raise HumanDraftError("transport_mode_invalid")
    if not _valid_hash(delivery_material_hash) or now_epoch <= 0:
        raise HumanDraftError("delivery_material_invalid")
    expected_id = _framed_hash("d1-card-delivery-v1", card_generation_public_id, transport_mode)
    if not hmac.compare_digest(attempt_public_id, expected_id):
        raise HumanDraftError("attempt_identity")
    _begin_immediate(conn)
    try:
        exact = conn.execute(
            "SELECT * FROM parser_human_draft_card_delivery_attempts WHERE attempt_public_id = ?",
            (attempt_public_id,),
        ).fetchone()
        if exact is not None:
            if (
                exact["card_generation_public_id"] != card_generation_public_id
                or exact["delivery_identity"] != expected_id
                or not hmac.compare_digest(
                    str(exact["delivery_material_hash"]), delivery_material_hash
                )
                or exact["transport_mode"] != transport_mode
                or exact["outbound_target_message_id"] != outbound_target_message_id
                or _context_from_row(exact) != context
            ):
                raise HumanDraftError("attempt_conflict")
            conn.commit()
            return str(exact["attempt_public_id"])
        card, draft = _load_card_and_draft(conn, card_generation_public_id)
        _require_context(draft, context)
        if draft["state"] != "active":
            raise HumanDraftError("draft_terminal")
        if now_epoch < int(card["issued_at"]):
            raise HumanDraftError("attempt_time_regression")
        if int(draft["expires_at"]) <= now_epoch or int(card["expires_at"]) <= now_epoch:
            raise HumanDraftError("card_expired")
        if draft["current_card_generation_public_id"] != card_generation_public_id:
            raise HumanDraftError("stale_card")
        existing = conn.execute(
            """
            SELECT * FROM parser_human_draft_card_delivery_attempts
            WHERE card_generation_public_id = ? AND transport_mode = ?
            """,
            (card_generation_public_id, transport_mode),
        ).fetchone()
        if existing is not None:
            if (
                existing["attempt_public_id"] != attempt_public_id
                or existing["delivery_identity"] != expected_id
                or not hmac.compare_digest(
                    str(existing["delivery_material_hash"]), delivery_material_hash
                )
                or existing["outbound_target_message_id"] != outbound_target_message_id
                or _context_from_row(existing) != context
            ):
                raise HumanDraftError("attempt_conflict")
            conn.commit()
            return str(existing["attempt_public_id"])
        conn.execute(
            """
            INSERT INTO parser_human_draft_card_delivery_attempts (
                attempt_public_id, card_generation_public_id, delivery_identity,
                delivery_material_hash, authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, transport_mode,
                outbound_target_message_id, attempted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_public_id,
                card_generation_public_id,
                expected_id,
                delivery_material_hash,
                context.authenticated_actor_id,
                context.telegram_account_id,
                context.telegram_conversation_id,
                context.conversation_binding_id,
                transport_mode,
                outbound_target_message_id,
                now_epoch,
            ),
        )
        conn.commit()
        return attempt_public_id
    except Exception:
        conn.rollback()
        raise


def record_human_draft_card_delivery_outcome(
    conn: sqlite3.Connection,
    *,
    context: HumanDraftContext,
    attempt_public_id: str,
    observation_public_id: str,
    outcome: str,
    error_code: str | None,
    outbound_message_id: str | None,
    trusted_receipt_hash: str | None,
    now_epoch: int,
) -> str:
    if outcome not in {"success", "failure", "unknown"} or now_epoch <= 0:
        raise HumanDraftError("delivery_outcome_invalid")
    if outcome == "success" or trusted_receipt_hash is not None or outbound_message_id is not None:
        raise HumanDraftError("trusted_receipt_unverifiable")
    if outcome == "failure" and not error_code:
        raise HumanDraftError("failure_code_required")
    _begin_immediate(conn)
    try:
        existing = conn.execute(
            "SELECT * FROM parser_human_draft_card_delivery_outcomes "
            "WHERE observation_public_id = ?",
            (observation_public_id,),
        ).fetchone()
        if existing is not None:
            expected_existing_id = _framed_hash(
                "d1-card-observation-v1",
                str(existing["attempt_public_id"]),
                str(existing["observation_slot"]),
            )
            if (
                not hmac.compare_digest(observation_public_id, expected_existing_id)
                or existing["observation_slot"] not in {"initial", "resolution"}
                or existing["attempt_public_id"] != attempt_public_id
                or existing["outcome"] != outcome
                or existing["error_code"] != error_code
                or existing["outbound_message_id"] != outbound_message_id
                or existing["trusted_receipt_hash"] != trusted_receipt_hash
            ):
                raise HumanDraftError("observation_conflict")
            attempt = conn.execute(
                "SELECT * FROM parser_human_draft_card_delivery_attempts "
                "WHERE attempt_public_id = ?",
                (attempt_public_id,),
            ).fetchone()
            if attempt is None or _context_from_row(attempt) != context:
                raise HumanDraftError("observation_conflict")
            _card, draft = _load_card_and_draft(conn, str(attempt["card_generation_public_id"]))
            _require_context(draft, context)
            conn.commit()
            return observation_public_id
        attempt = conn.execute(
            "SELECT * FROM parser_human_draft_card_delivery_attempts WHERE attempt_public_id = ?",
            (attempt_public_id,),
        ).fetchone()
        if attempt is None:
            raise HumanDraftError("attempt_missing")
        _card, draft = _load_card_and_draft(conn, str(attempt["card_generation_public_id"]))
        _require_context(draft, context)
        if _context_from_row(attempt) != context:
            raise HumanDraftError("attempt_context")
        if now_epoch < int(attempt["attempted_at"]):
            raise HumanDraftError("observation_time_regression")
        prior = conn.execute(
            "SELECT * FROM parser_human_draft_card_delivery_outcomes "
            "WHERE attempt_public_id = ? ORDER BY id",
            (attempt_public_id,),
        ).fetchall()
        slot = "initial" if not prior else "resolution"
        expected_id = _framed_hash("d1-card-observation-v1", attempt_public_id, slot)
        if not hmac.compare_digest(observation_public_id, expected_id):
            raise HumanDraftError("observation_identity")
        if len(prior) >= 2:
            raise HumanDraftError("observation_limit")
        if prior and prior[0]["outcome"] != "unknown":
            raise HumanDraftError("observation_terminal")
        if prior and now_epoch < int(prior[0]["observed_at"]):
            raise HumanDraftError("observation_time_regression")
        conn.execute(
            """
            INSERT INTO parser_human_draft_card_delivery_outcomes (
                observation_public_id, attempt_public_id, observation_slot,
                outcome, error_code, outbound_message_id, trusted_receipt_hash, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_public_id,
                attempt_public_id,
                slot,
                outcome,
                error_code,
                outbound_message_id,
                trusted_receipt_hash,
                now_epoch,
            ),
        )
        conn.commit()
        return observation_public_id
    except Exception:
        conn.rollback()
        raise


def reissue_human_draft_card(
    conn: sqlite3.Connection,
    *,
    context: HumanDraftContext,
    expected_current_generation_public_id: str,
    original_operation_or_start_public_id: str,
    recovery_public_id: str,
    recovery_material_hash: str,
    queried_delivery_state_hash: str,
    reason: str,
    now_epoch: int,
) -> HumanDraftResult:
    if reason not in {"failure", "expiry", "unknown_after_query"}:
        raise HumanDraftError("recovery_reason_invalid")
    if not _valid_hash(recovery_material_hash) or not _valid_hash(queried_delivery_state_hash):
        raise HumanDraftError("recovery_material_invalid")
    _begin_immediate(conn)
    try:
        expected_card, draft = _load_card_and_draft(conn, expected_current_generation_public_id)
        _require_context(draft, context)
        if draft["state"] != "active":
            raise HumanDraftError("draft_terminal")
        if int(draft["expires_at"]) <= now_epoch:
            raise HumanDraftError("draft_expired")
        expected_recovery_id = _framed_hash(
            "d1-card-recovery-v1",
            str(draft["draft_public_id"]),
            original_operation_or_start_public_id,
            expected_current_generation_public_id,
        )
        if not hmac.compare_digest(recovery_public_id, expected_recovery_id):
            raise HumanDraftError("recovery_identity")
        existing = conn.execute(
            "SELECT * FROM parser_human_draft_cards WHERE recovery_public_id = ?",
            (recovery_public_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["predecessor_card_id"] != expected_card["id"]
                or existing["recovery_material_hash"] != recovery_material_hash
                or existing["recovery_delivery_state_hash"] != queried_delivery_state_hash
                or existing["recovery_reason"] != reason
            ):
                raise HumanDraftError("recovery_conflict")
            operation = _origin_operation(conn, existing)
            result = _result_for(
                conn,
                draft,
                operation,
                card_public_id=str(existing["card_generation_public_id"]),
            )
            conn.commit()
            from dataclasses import replace

            return replace(result, idempotent_replay=True)
        if draft["current_card_generation_public_id"] != expected_current_generation_public_id:
            raise HumanDraftError("stale_card")
        operation = _origin_operation(conn, expected_card)
        if operation["operation_public_id"] != original_operation_or_start_public_id:
            raise HumanDraftError("operation_identity_mismatch")
        queried = _result_for(
            conn,
            draft,
            operation,
            card_public_id=expected_current_generation_public_id,
        )
        if not hmac.compare_digest(queried.delivery_state_hash, queried_delivery_state_hash):
            raise HumanDraftError("delivery_state_changed")
        if reason == "failure" and queried.delivery_state != "failure":
            raise HumanDraftError("recovery_evidence_missing")
        if reason == "unknown_after_query" and queried.delivery_state != "unknown":
            raise HumanDraftError("recovery_evidence_missing")
        if reason == "expiry" and int(expected_card["expires_at"]) > now_epoch:
            raise HumanDraftError("recovery_evidence_missing")
        evidence_times = [int(expected_card["issued_at"])]
        evidence_times.extend(
            cast(int, attempt["attempted_at"]) for attempt in queried.delivery_attempts
        )
        evidence_times.extend(
            cast(int, outcome["observed_at"]) for outcome in queried.delivery_outcomes
        )
        if now_epoch < max(evidence_times):
            raise HumanDraftError("recovery_time_regression")
        successor_id = _prefixed_id("d1card_", "d1-card-reissue-generation-v1", recovery_public_id)
        action_batch_id = hashlib.sha256(
            _encode_material("d1-card-action-issue-v1", successor_id)
        ).hexdigest()
        conn.execute(
            """
            INSERT INTO parser_human_draft_cards (
                card_generation_public_id, draft_id, draft_version, draft_content_hash,
                field_values_json, parser_output_id, proposal_version,
                proposal_content_hash, decision_target_parser_output_id,
                decision_target_proposal_version, decision_target_proposal_content_hash,
                language, format_version, action_issue_batch_id, predecessor_card_id,
                original_operation_id, recovery_public_id, recovery_material_hash,
                recovery_delivery_state_hash, recovery_reason,
                authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, expires_at, issued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                successor_id,
                expected_card["draft_id"],
                expected_card["draft_version"],
                expected_card["draft_content_hash"],
                expected_card["field_values_json"],
                expected_card["parser_output_id"],
                expected_card["proposal_version"],
                expected_card["proposal_content_hash"],
                expected_card["decision_target_parser_output_id"],
                expected_card["decision_target_proposal_version"],
                expected_card["decision_target_proposal_content_hash"],
                expected_card["language"],
                expected_card["format_version"],
                action_batch_id,
                expected_card["id"],
                expected_card["original_operation_id"],
                recovery_public_id,
                recovery_material_hash,
                queried_delivery_state_hash,
                reason,
                context.authenticated_actor_id,
                context.telegram_account_id,
                context.telegram_conversation_id,
                context.conversation_binding_id,
                min(int(draft["expires_at"]), now_epoch + 300),
                now_epoch,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE parser_human_drafts
            SET current_card_generation_public_id = ?, updated_at = max(updated_at, ?)
            WHERE id = ? AND state = 'active' AND current_card_generation_public_id = ?
              AND current_draft_version = ? AND current_draft_content_hash = ?
            """,
            (
                successor_id,
                now_epoch,
                draft["id"],
                expected_current_generation_public_id,
                draft["current_draft_version"],
                draft["current_draft_content_hash"],
            ),
        )
        if cursor.rowcount != 1:
            raise HumanDraftError("draft_race")
        refreshed = conn.execute(
            "SELECT * FROM parser_human_drafts WHERE id = ?", (draft["id"],)
        ).fetchone()
        assert refreshed is not None
        result = _result_for(conn, refreshed, operation, card_public_id=successor_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


__all__ = [
    "begin_human_draft_card_delivery",
    "get_human_draft_card",
    "record_human_draft_card_delivery_outcome",
    "reissue_human_draft_card",
]
