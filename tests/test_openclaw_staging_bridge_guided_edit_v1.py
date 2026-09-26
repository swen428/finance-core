"""Durable structured Telegram guided-edit acceptance tests."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import (
    commands,
    guided_edit,
    human_actions,
    identity,
    ocr_boundary,
)
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.parser_proposals.completion import complete_proposal
from tests.test_receipt_ocr_proposal_ingestion_v1 import _sgd_blocks

ACTOR = "111"
ACCOUNT = "finance-account"
CONVERSATION = "111"
BINDING = "binding-1"


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _context(workspace: support.BridgeWorkspace) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "operator_actor_id": ACTOR,
        "telegram_account_id": ACCOUNT,
        "telegram_conversation_id": CONVERSATION,
        "conversation_binding_id": BINDING,
    }


def _prepare_edit_redemption(
    workspace: support.BridgeWorkspace,
    *,
    callback_id: str = "guided-edit-callback",
) -> tuple[str, dict[str, object]]:
    capture_arguments = support.authenticated_text_capture_arguments(
        workspace, support.telegram_text_update("lunch 12.50")
    )
    capture_arguments.pop("kind")
    capture = support.run_cli(
        support.make_request(
            "capture_interaction",
            capture_arguments,
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK, capture.response
    processed = support.process_captured_text(workspace, capture)
    assert processed.exit_code == bridge_errors.EXIT_OK, processed.response
    proposal = str(processed.response["result"]["capture_job"]["proposal_public_id"])
    review = support.run_cli(
        support.make_request(
            "get_review",
            {"workspace_path": str(workspace.workspace_path), "proposal_public_id": proposal},
        )
    ).response["result"]
    batch = "d" * 32
    issued = support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **_context(workspace),
                "proposal_public_id": proposal,
                "reference_batch_id": batch,
                "token_ttl_seconds": 600,
                "expected_proposal_version": review["proposal_version"],
                "expected_content_hash": review["effective_content_hash"],
            },
            idempotency_key=support.canonical_human_action_issuance_key(batch),
        )
    )
    reference = issued.response["result"]["actions"]["edit"]["reference"]
    redemption_request = support.make_request(
        "redeem_human_action",
        {
            **_context(workspace),
            "short_reference": reference,
            "action": "edit",
            "callback_id": callback_id,
            "callback_message_id": 20,
        },
        idempotency_key=support.canonical_human_action_redemption_key(callback_id),
    )
    return proposal, redemption_request


def _begin(workspace: support.BridgeWorkspace) -> tuple[str, str, dict[str, object]]:
    proposal, redemption_request = _prepare_edit_redemption(workspace)
    redeemed = support.run_cli(redemption_request)
    assert redeemed.exit_code == bridge_errors.EXIT_OK, redeemed.response
    session = str(redeemed.response["result"]["guided_edit_session_public_id"])
    return proposal, session, redemption_request


def _capture_routed_message(
    workspace: support.BridgeWorkspace, text: str, message_id: int
) -> dict[str, object]:
    """Freeze one Telegram route before a fresh guided or D1 business write."""
    with support.open_database(workspace) as conn:
        existing = conn.execute(
            "SELECT r.operation_key FROM finance_capture_interaction_routes r "
            "WHERE r.authenticated_actor_id = ? AND r.telegram_account_id = ? "
            "AND r.telegram_conversation_id = ? AND r.conversation_binding_id = ? "
            "AND r.telegram_message_id = ?",
            (ACTOR, ACCOUNT, CONVERSATION, BINDING, message_id),
        ).fetchone()
    if existing is not None:
        return {"operation_key": existing[0]}
    arguments = support.authenticated_text_capture_arguments(
        workspace, support.telegram_text_update(text, message_id=message_id)
    )
    arguments.pop("kind")
    captured = support.run_cli(
        support.make_request(
            "capture_interaction",
            arguments,
            idempotency_key=support.canonical_capture_key(message_id=message_id),
        )
    )
    assert captured.exit_code == bridge_errors.EXIT_OK, captured.response
    return dict(captured.response["result"]["interaction_route"])


def _apply(
    workspace: support.BridgeWorkspace,
    session: str,
    message_id: int,
    field: str,
    value: str,
) -> support.CliOutcome:
    if "\n" not in value and "\r" not in value:
        _capture_routed_message(workspace, f"{field}={value}", message_id)
    return support.run_cli(
        support.make_request(
            "apply_guided_edit_update",
            {
                **_context(workspace),
                "session_public_id": session,
                "telegram_message_id": message_id,
                "field_name": field,
                "field_value": value,
            },
            idempotency_key=f"bridge-guided-edit-update:{session}:{message_id}",
        )
    )


def _get(workspace: support.BridgeWorkspace, message_id: int | None = None) -> support.CliOutcome:
    return support.run_cli(
        support.make_request(
            "get_guided_edit_session",
            {
                **_context(workspace),
                **({} if message_id is None else {"telegram_message_id": message_id}),
            },
        )
    )


def _draft_from_redemption(
    workspace: support.BridgeWorkspace, redemption: dict[str, object]
) -> dict[str, object]:
    outcome = support.run_cli(redemption)
    assert outcome.exit_code == bridge_errors.EXIT_OK, outcome.response
    return dict(outcome.response["result"]["human_draft_card"])


def _whole_card_text(card_public_id: str, *, merchant: str = "Example Cafe") -> str:
    return "\n".join(
        (
            f"Card Ref: {card_public_id}",
            "Amount: 12.50",
            "Currency: SGD",
            "Date: 2026-09-19",
            f"Merchant: {merchant}",
            "Description: Lunch",
            "Category: Food",
        )
    )


def _apply_whole_card(
    workspace: support.BridgeWorkspace,
    card_public_id: str,
    *,
    message_id: int = 30,
    merchant: str = "Example Cafe",
    operation_public_id: str | None = None,
    idempotency_key: str | None = None,
) -> support.CliOutcome:
    raw_card_text = _whole_card_text(card_public_id, merchant=merchant)
    route = _capture_routed_message(workspace, raw_card_text, message_id)
    operation_id = operation_public_id or str(route["operation_key"])
    return support.run_cli(
        support.make_request(
            "apply_human_draft_card",
            {
                **_context(workspace),
                "card_generation_public_id": card_public_id,
                "telegram_message_id": message_id,
                "operation_public_id": operation_id,
                "raw_card_text": raw_card_text,
                "field_values": {
                    "amount": "12.50",
                    "currency": "SGD",
                    "transaction_date": "2026-09-19",
                    "merchant": merchant,
                    "description": "Lunch",
                    "category": "Food",
                },
            },
            idempotency_key=idempotency_key or f"bridge-human-draft-apply:{operation_id}",
        )
    )


def _get_human_draft_card(
    workspace: support.BridgeWorkspace, **identities: object
) -> support.CliOutcome:
    return support.run_cli(
        support.make_request(
            "get_human_draft_card",
            {**_context(workspace), **identities},
        )
    )


def _framed_hash(domain: str, *fields: str) -> str:
    payload = domain.encode("ascii") + b"\x00" + len(fields).to_bytes(4, "big")
    for field in fields:
        encoded = field.encode("utf-8")
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def _issue_d1_actions(
    workspace: support.BridgeWorkspace, card: dict[str, object]
) -> support.CliOutcome:
    proposal_public_id = card["proposal_public_id"] or card["decision_target_proposal_public_id"]
    proposal_version = (
        card["proposal_version"]
        if card["proposal_version"] is not None
        else card["decision_target_proposal_version"]
    )
    content_hash = card["proposal_content_hash"] or card["decision_target_proposal_content_hash"]
    return support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **_context(workspace),
                "proposal_public_id": proposal_public_id,
                "card_generation_public_id": card["card_generation_public_id"],
                "reference_batch_id": card["action_issue_batch_id"],
                "token_ttl_seconds": 300,
                "expected_proposal_version": proposal_version,
                "expected_content_hash": content_hash,
            },
            idempotency_key=support.canonical_human_action_issuance_key(
                str(card["action_issue_batch_id"])
            ),
        )
    )


def _redeem_d1_action(
    workspace: support.BridgeWorkspace,
    issued: support.CliOutcome,
    *,
    action: str,
    callback_id: str,
) -> support.CliOutcome:
    return support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": issued.response["result"]["actions"][action]["reference"],
                "action": action,
                "callback_id": callback_id,
                "callback_message_id": 40,
            },
            idempotency_key=support.canonical_human_action_redemption_key(callback_id),
        )
    )


def _decide_d1(
    workspace: support.BridgeWorkspace,
    redeemed: support.CliOutcome,
    *,
    action: str,
    reference_public_id: str | None = None,
    binding_id: str = BINDING,
) -> support.CliOutcome:
    material = redeemed.response["result"]
    binding = material["d1_decision_binding"]
    return support.run_cli(
        support.make_request(
            action,
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": material["proposal_public_id"],
                "operator_actor_id": ACTOR,
                "proposal_version": material["proposal_version"],
                "content_hash": material["content_hash"],
                "callback_token": material["callback_token"],
                "callback_expiry": material["callback_expiry"],
                "d1_reference_public_id": reference_public_id or binding["reference_public_id"],
                "telegram_account_id": ACCOUNT,
                "telegram_conversation_id": CONVERSATION,
                "conversation_binding_id": binding_id,
            },
            idempotency_key=material["decision_idempotency_key"],
        )
    )


def _decide_without_d1_binding(
    workspace: support.BridgeWorkspace,
    redeemed: support.CliOutcome,
    *,
    action: str,
) -> support.CliOutcome:
    material = redeemed.response["result"]
    return support.run_cli(
        support.make_request(
            action,
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": material["proposal_public_id"],
                "operator_actor_id": ACTOR,
                "proposal_version": material["proposal_version"],
                "content_hash": material["content_hash"],
                "callback_token": material["callback_token"],
                "callback_expiry": material["callback_expiry"],
            },
            idempotency_key=material["decision_idempotency_key"],
        )
    )


def _complete(
    workspace: support.BridgeWorkspace, session: str, message_id: int
) -> support.CliOutcome:
    _capture_routed_message(workspace, "完成", message_id)
    return support.run_cli(
        support.make_request(
            "complete_guided_edit",
            {
                **_context(workspace),
                "session_public_id": session,
                "telegram_message_id": message_id,
            },
            idempotency_key=f"bridge-guided-edit-complete:{session}:{message_id}",
        )
    )


def test_edit_redemption_atomically_starts_durable_session(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, session, redemption = _begin(workspace)
    redeemed = support.run_cli(redemption)
    draft = redeemed.response["result"]["human_draft_card"]
    assert draft["draft_version"] == 0
    assert draft["completeness"] == "incomplete"
    assert draft["card_generation_public_id"].startswith("d1card_")
    assert draft["current_card_generation_public_id"] == draft["card_generation_public_id"]
    assert draft["confirm_available"] is False
    assert draft["reject_available"] is True
    assert draft["final_transaction_created"] is False
    assert redeemed.response["idempotent_replay"] is True
    active = _get(workspace)
    assert active.exit_code == bridge_errors.EXIT_OK
    assert active.response["result"] == {
        "active": True,
        "session_status": "active",
        "session_public_id": session,
        "proposal_public_id": proposal,
        "proposal_version": 0,
        "effective_content_hash": active.response["result"]["effective_content_hash"],
        "expires_at": active.response["result"]["expires_at"],
        "recovery_required": False,
        "final_transaction_created": False,
    }
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM openclaw_guided_edit_sessions").fetchone()[0] == 1
        assert conn.execute("SELECT event_type FROM openclaw_guided_edit_events").fetchone()[0] == (
            "started"
        )
        assert conn.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        start = conn.execute(
            "SELECT start_redemption_public_id FROM parser_human_drafts"
        ).fetchone()[0]
        operation_id = conn.execute(
            "SELECT operation_public_id FROM parser_human_draft_operations "
            "WHERE operation_type = 'start'"
        ).fetchone()[0]
        assert start == operation_id
        assert start.startswith("d1start_")
        assert start != "guided-edit-callback"
        assert draft["original_operation_or_start_public_id"] == start
    finally:
        conn.close()


def test_exact_replay_of_pre_d1_redemption_backfills_the_missing_draft(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, redemption = _prepare_edit_redemption(
        workspace,
        callback_id="pre-d1-redemption-replay",
    )
    original = commands.begin_human_draft_in_transaction
    monkeypatch.setattr(commands, "begin_human_draft_in_transaction", lambda *args, **kwargs: None)
    historical = support.run_cli(redemption)
    assert historical.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 0
    finally:
        conn.close()

    monkeypatch.setattr(commands, "begin_human_draft_in_transaction", original)
    replay = support.run_cli(redemption)
    assert replay.exit_code == bridge_errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"]["human_draft_card"]["draft_version"] == 0


def test_edit_redemption_and_d1_start_roll_back_together_on_failure(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, redemption = _prepare_edit_redemption(
        workspace,
        callback_id="d1-start-rollback",
    )
    original = commands.begin_human_draft_in_transaction

    def fail_after_start(*args: object, **kwargs: object) -> object:
        original(*args, **kwargs)
        raise RuntimeError("injected post-start failure")

    monkeypatch.setattr(commands, "begin_human_draft_in_transaction", fail_after_start)
    failed = support.run_cli(redemption)
    assert failed.exit_code == bridge_errors.EXIT_INTERNAL
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM openclaw_guided_edit_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 0
    finally:
        conn.close()

    monkeypatch.setattr(commands, "begin_human_draft_in_transaction", original)
    recovered = support.run_cli(redemption)
    assert recovered.exit_code == bridge_errors.EXIT_OK, recovered.response
    assert recovered.response["idempotent_replay"] is False


def test_whole_card_command_publishes_one_complete_unconfirmed_revision(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    result = _apply_whole_card(workspace, str(initial["card_generation_public_id"]))
    assert result.exit_code == bridge_errors.EXIT_OK, result.response
    card = result.response["result"]
    assert card["draft_version"] == 1
    assert card["completeness"] == "complete"
    assert card["operation_outcome"] == "accepted"
    assert card["proposal_public_id"].startswith("po_d1_")
    assert card["proposal_version"] == 0
    assert card["confirm_available"] is True
    assert card["reject_available"] is True
    assert card["final_transaction_created"] is False
    assert card["field_values"] == {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-19",
        "merchant": "Example Cafe",
        "description": "Lunch",
        "category": "Food",
    }
    assert card["human_reply_evidence_public_id"].startswith("d1evidence_")
    assert card["card_generation_public_id"] != initial["card_generation_public_id"]
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_publications").fetchone()[0] == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        conn.close()


def test_plugin_shaped_bilingual_whole_cards_preserve_exact_utf8_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    card_0 = str(initial["card_generation_public_id"])
    chinese = "\n".join(
        (
            f"资料卡编号：{card_0}",
            "金额：12.50",
            "币种：SGD",
            "日期：2026-09-19",
            "商户：示例：咖啡店",
            "描述：午餐",
            "分类：餐饮",
        )
    )
    fields_1 = {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-19",
        "merchant": "示例：咖啡店",
        "description": "午餐",
        "category": "餐饮",
    }
    operation_1 = str(_capture_routed_message(workspace, chinese, 30)["operation_key"])
    first = support.run_cli(
        support.make_request(
            "apply_human_draft_card",
            {
                **_context(workspace),
                "card_generation_public_id": card_0,
                "telegram_message_id": 30,
                "operation_public_id": operation_1,
                "raw_card_text": chinese,
                "field_values": fields_1,
            },
            idempotency_key=f"bridge-human-draft-apply:{operation_1}",
        )
    )
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert first.response["result"]["field_values"] == fields_1

    card_1 = str(first.response["result"]["card_generation_public_id"])
    mixed = "\r\n".join(
        (
            f"cArD rEf : {card_1}",
            "金额: 12.50",
            "CURRENCY：SGD",
            "日期: 2026-09-20",
            "Merchant: Branch: Two",
            "描述：午餐",
            "CATEGORY: 餐饮",
        )
    )
    fields_2 = {
        **fields_1,
        "transaction_date": "2026-09-20",
        "merchant": "Branch: Two",
    }
    operation_2 = str(_capture_routed_message(workspace, mixed, 31)["operation_key"])
    second = support.run_cli(
        support.make_request(
            "apply_human_draft_card",
            {
                **_context(workspace),
                "card_generation_public_id": card_1,
                "telegram_message_id": 31,
                "operation_public_id": operation_2,
                "raw_card_text": mixed,
                "field_values": fields_2,
            },
            idempotency_key=f"bridge-human-draft-apply:{operation_2}",
        )
    )
    assert second.exit_code == bridge_errors.EXIT_OK, second.response
    assert second.response["result"]["field_values"] == fields_2

    conn = support.open_database(workspace)
    try:
        evidence = conn.execute(
            "SELECT operations.operation_public_id, evidence.raw_utf8, evidence.sha256 "
            "FROM parser_human_draft_operations AS operations "
            "JOIN parser_human_draft_reply_evidence AS evidence "
            "ON evidence.id = operations.human_reply_evidence_id "
            "WHERE operations.operation_public_id IN (?, ?) ORDER BY operations.id",
            (operation_1, operation_2),
        ).fetchall()
        assert [(row[0], bytes(row[1])) for row in evidence] == [
            (operation_1, chinese.encode("utf-8")),
            (operation_2, mixed.encode("utf-8")),
        ]
        assert all(row[2] == hashlib.sha256(bytes(row[1])).hexdigest() for row in evidence)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        conn.close()


def test_whole_card_exact_replay_and_read_only_recovery_preserve_one_result(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    card_id = str(initial["card_generation_public_id"])
    first = _apply_whole_card(workspace, card_id)
    replay = _apply_whole_card(workspace, card_id)
    operation_id = str(
        _capture_routed_message(workspace, _whole_card_text(card_id), 30)["operation_key"]
    )
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert replay.exit_code == bridge_errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"] == {
        **first.response["result"],
        "idempotent_replay": True,
    }

    recovered = _get_human_draft_card(
        workspace,
        operation_public_id=operation_id,
        card_generation_public_id=first.response["result"]["card_generation_public_id"],
    )
    assert recovered.exit_code == bridge_errors.EXIT_OK, recovered.response
    assert recovered.response["result"] == first.response["result"]

    conflict = _apply_whole_card(workspace, card_id, merchant="Different Cafe")
    assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert conflict.response["error"]["code"] == bridge_errors.HUMAN_DRAFT_CONFLICT

    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_publications").fetchone()[0] == 1
        )
    finally:
        conn.close()


def test_whole_card_raw_overflow_is_refused_before_d1_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    operation_id = "d1op_overflow"
    request = support.make_request(
        "apply_human_draft_card",
        {
            **_context(workspace),
            "card_generation_public_id": initial["card_generation_public_id"],
            "telegram_message_id": 30,
            "operation_public_id": operation_id,
            "raw_card_text": "x" * 16_385,
            "field_values": dict(initial["field_values"]),
        },
        idempotency_key=f"bridge-human-draft-apply:{operation_id}",
    )
    refused = support.run_cli(request)
    assert refused.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.HUMAN_DRAFT_ARGUMENTS_REFUSED
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == 1
    finally:
        conn.close()


def test_delivery_unknown_query_and_reissue_are_restart_safe(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    accepted = _apply_whole_card(workspace, str(initial["card_generation_public_id"]))
    assert accepted.exit_code == bridge_errors.EXIT_OK, accepted.response
    card = accepted.response["result"]
    card_id = str(card["card_generation_public_id"])
    generation_1_actions = _issue_d1_actions(workspace, card)
    assert generation_1_actions.exit_code == bridge_errors.EXIT_OK, generation_1_actions.response
    attempt_id = _framed_hash("d1-card-delivery-v1", card_id, "reply")
    attempt = support.run_cli(
        support.make_request(
            "begin_human_draft_card_delivery",
            {
                **_context(workspace),
                "card_generation_public_id": card_id,
                "attempt_public_id": attempt_id,
                "delivery_material_hash": "a" * 64,
                "transport_mode": "reply",
            },
            idempotency_key=f"bridge-human-draft-delivery:{attempt_id}",
        )
    )
    assert attempt.exit_code == bridge_errors.EXIT_OK, attempt.response
    assert attempt.response["result"] == {"attempt_public_id": attempt_id}
    attempt_replay = support.run_cli(
        support.make_request(
            "begin_human_draft_card_delivery",
            {
                **_context(workspace),
                "card_generation_public_id": card_id,
                "attempt_public_id": attempt_id,
                "delivery_material_hash": "a" * 64,
                "transport_mode": "reply",
            },
            idempotency_key=f"bridge-human-draft-delivery:{attempt_id}",
        )
    )
    assert attempt_replay.exit_code == bridge_errors.EXIT_OK, attempt_replay.response
    assert attempt_replay.response["idempotent_replay"] is True

    observation_id = _framed_hash("d1-card-observation-v1", attempt_id, "initial")
    unknown = support.run_cli(
        support.make_request(
            "record_human_draft_card_delivery_outcome",
            {
                **_context(workspace),
                "attempt_public_id": attempt_id,
                "observation_public_id": observation_id,
                "outcome": "unknown",
                "error_code": None,
                "outbound_message_id": None,
                "trusted_receipt_hash": None,
            },
            idempotency_key=f"bridge-human-draft-observation:{observation_id}",
        )
    )
    assert unknown.exit_code == bridge_errors.EXIT_OK, unknown.response
    assert unknown.response["result"] == {"observation_public_id": observation_id}
    unknown_replay = support.run_cli(
        support.make_request(
            "record_human_draft_card_delivery_outcome",
            {
                **_context(workspace),
                "attempt_public_id": attempt_id,
                "observation_public_id": observation_id,
                "outcome": "unknown",
                "error_code": None,
                "outbound_message_id": None,
                "trusted_receipt_hash": None,
            },
            idempotency_key=f"bridge-human-draft-observation:{observation_id}",
        )
    )
    assert unknown_replay.exit_code == bridge_errors.EXIT_OK, unknown_replay.response
    assert unknown_replay.response["idempotent_replay"] is True

    queried = _get_human_draft_card(workspace, attempt_public_id=attempt_id)
    assert queried.exit_code == bridge_errors.EXIT_OK, queried.response
    assert queried.response["result"]["delivery_state"] == "unknown"
    assert len(queried.response["result"]["delivery_attempts"]) == 1
    assert len(queried.response["result"]["delivery_outcomes"]) == 1

    recovery_id = _framed_hash(
        "d1-card-recovery-v1",
        str(card["draft_public_id"]),
        str(card["original_operation_or_start_public_id"]),
        card_id,
    )
    reissue_request = support.make_request(
        "reissue_human_draft_card",
        {
            **_context(workspace),
            "expected_current_generation_public_id": card_id,
            "original_operation_or_start_public_id": card["original_operation_or_start_public_id"],
            "recovery_public_id": recovery_id,
            "recovery_material_hash": "b" * 64,
            "queried_delivery_state_hash": queried.response["result"]["delivery_state_hash"],
            "reason": "unknown_after_query",
        },
        idempotency_key=f"bridge-human-draft-reissue:{recovery_id}",
    )
    reissued = support.run_cli(reissue_request)
    replay = support.run_cli(reissue_request)
    assert reissued.exit_code == bridge_errors.EXIT_OK, reissued.response
    assert replay.exit_code == bridge_errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert reissued.response["result"]["card_generation_public_id"] != card_id
    assert (
        reissued.response["result"]["current_card_generation_public_id"]
        == reissued.response["result"]["card_generation_public_id"]
    )
    assert reissued.response["result"]["final_transaction_created"] is False

    stale_generation = _redeem_d1_action(
        workspace,
        generation_1_actions,
        action="confirm",
        callback_id="d1-stale-generation-confirm",
    )
    assert stale_generation.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert stale_generation.response["error"]["code"] == (
        bridge_errors.HUMAN_ACTION_REFERENCE_INVALID
    )

    generation_2_actions = _issue_d1_actions(workspace, reissued.response["result"])
    assert generation_2_actions.exit_code == bridge_errors.EXIT_OK, generation_2_actions.response
    generation_2_confirm = _redeem_d1_action(
        workspace,
        generation_2_actions,
        action="confirm",
        callback_id="d1-current-generation-confirm",
    )
    assert generation_2_confirm.exit_code == bridge_errors.EXIT_OK, generation_2_confirm.response
    confirmed = _decide_d1(workspace, generation_2_confirm, action="confirm")
    assert confirmed.exit_code == bridge_errors.EXIT_OK, confirmed.response


@pytest.mark.parametrize(
    ("reason", "outcome"),
    (
        ("failure", "failure"),
        ("unknown_after_query", "unknown"),
        ("expiry", None),
    ),
)
def test_initial_card_reissue_uses_only_public_bridge_identity(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    outcome: str | None,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    card = _draft_from_redemption(workspace, redemption)
    card_id = str(card["card_generation_public_id"])
    original_id = str(card["original_operation_or_start_public_id"])
    assert original_id.startswith("d1start_")
    assert original_id != "guided-edit-callback"

    attempt_id = _framed_hash("d1-card-delivery-v1", card_id, "reply")
    attempt = support.run_cli(
        support.make_request(
            "begin_human_draft_card_delivery",
            {
                **_context(workspace),
                "card_generation_public_id": card_id,
                "attempt_public_id": attempt_id,
                "delivery_material_hash": "c" * 64,
                "transport_mode": "reply",
            },
            idempotency_key=f"bridge-human-draft-delivery:{attempt_id}",
        )
    )
    assert attempt.exit_code == bridge_errors.EXIT_OK, attempt.response
    if outcome is not None:
        observation_id = _framed_hash("d1-card-observation-v1", attempt_id, "initial")
        observed = support.run_cli(
            support.make_request(
                "record_human_draft_card_delivery_outcome",
                {
                    **_context(workspace),
                    "attempt_public_id": attempt_id,
                    "observation_public_id": observation_id,
                    "outcome": outcome,
                    "error_code": "transport_failure" if outcome == "failure" else None,
                    "outbound_message_id": None,
                    "trusted_receipt_hash": None,
                },
                idempotency_key=f"bridge-human-draft-observation:{observation_id}",
            )
        )
        assert observed.exit_code == bridge_errors.EXIT_OK, observed.response

    queried = _get_human_draft_card(workspace, attempt_public_id=attempt_id)
    assert queried.exit_code == bridge_errors.EXIT_OK, queried.response
    assert queried.response["result"]["original_operation_or_start_public_id"] == original_id
    if reason == "expiry":
        conn = support.open_database(workspace)
        try:
            expires_at = int(
                conn.execute(
                    "SELECT expires_at FROM parser_human_draft_cards "
                    "WHERE card_generation_public_id = ?",
                    (card_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()

        class _AfterCardExpiry(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime.fromtimestamp(expires_at + 1, tz=UTC)

        monkeypatch.setattr(commands, "datetime", _AfterCardExpiry)

    recovery_id = _framed_hash(
        "d1-card-recovery-v1",
        str(card["draft_public_id"]),
        original_id,
        card_id,
    )
    reissued = support.run_cli(
        support.make_request(
            "reissue_human_draft_card",
            {
                **_context(workspace),
                "expected_current_generation_public_id": card_id,
                "original_operation_or_start_public_id": original_id,
                "recovery_public_id": recovery_id,
                "recovery_material_hash": "d" * 64,
                "queried_delivery_state_hash": queried.response["result"]["delivery_state_hash"],
                "reason": reason,
            },
            idempotency_key=f"bridge-human-draft-reissue:{recovery_id}",
        )
    )
    assert reissued.exit_code == bridge_errors.EXIT_OK, reissued.response
    assert reissued.response["result"]["card_generation_public_id"] != card_id


def test_generation_bound_actions_redeem_and_confirm_with_durable_binding(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    accepted = _apply_whole_card(workspace, str(initial["card_generation_public_id"]))
    assert accepted.exit_code == bridge_errors.EXIT_OK, accepted.response
    card = accepted.response["result"]
    issued = support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **_context(workspace),
                "proposal_public_id": card["proposal_public_id"],
                "card_generation_public_id": card["card_generation_public_id"],
                "reference_batch_id": card["action_issue_batch_id"],
                "token_ttl_seconds": 300,
                "expected_proposal_version": card["proposal_version"],
                "expected_content_hash": card["proposal_content_hash"],
            },
            idempotency_key=support.canonical_human_action_issuance_key(
                card["action_issue_batch_id"]
            ),
        )
    )
    assert issued.exit_code == bridge_errors.EXIT_OK, issued.response
    assert set(issued.response["result"]["actions"]) == {"confirm", "edit", "reject"}
    assert (
        issued.response["result"]["card_generation_public_id"] == card["card_generation_public_id"]
    )

    callback_id = "d1-confirm-callback"
    redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": issued.response["result"]["actions"]["confirm"]["reference"],
                "action": "confirm",
                "callback_id": callback_id,
                "callback_message_id": 40,
            },
            idempotency_key=support.canonical_human_action_redemption_key(callback_id),
        )
    )
    assert redeemed.exit_code == bridge_errors.EXIT_OK, redeemed.response
    binding = redeemed.response["result"]["d1_decision_binding"]
    assert binding["card_generation_public_id"] == card["card_generation_public_id"]
    assert binding["authenticated_actor_id"] == ACTOR

    decision = support.run_cli(
        support.make_request(
            "confirm",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": redeemed.response["result"]["proposal_public_id"],
                "operator_actor_id": ACTOR,
                "proposal_version": redeemed.response["result"]["proposal_version"],
                "content_hash": redeemed.response["result"]["content_hash"],
                "callback_token": redeemed.response["result"]["callback_token"],
                "callback_expiry": redeemed.response["result"]["callback_expiry"],
                "d1_reference_public_id": binding["reference_public_id"],
                "telegram_account_id": ACCOUNT,
                "telegram_conversation_id": CONVERSATION,
                "conversation_binding_id": BINDING,
            },
            idempotency_key=redeemed.response["result"]["decision_idempotency_key"],
        )
    )
    assert decision.exit_code == bridge_errors.EXIT_OK, decision.response
    assert decision.response["result"]["decision"] == "confirmed"
    assert decision.response["result"]["final_transaction_created"] is False
    replayed_decision = _decide_d1(workspace, redeemed, action="confirm")
    assert replayed_decision.exit_code == bridge_errors.EXIT_OK, replayed_decision.response
    assert replayed_decision.response["idempotent_replay"] is True
    unbound_replay = _decide_without_d1_binding(workspace, redeemed, action="confirm")
    assert unbound_replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert unbound_replay.response["error"]["code"] == bridge_errors.LIFECYCLE_CONFLICT
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "confirmed"
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0] == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parser_human_draft_operations "
                "WHERE operation_type = 'confirmed'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("first_mode", ("legacy", "d1"))
def test_legacy_and_d1_issuance_resolve_private_key_collisions(
    workspace: support.BridgeWorkspace,
    first_mode: str,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    card = _draft_from_redemption(workspace, redemption)
    d1_batch = str(card["action_issue_batch_id"])
    callback_key = commands._load_callback_key(workspace.workspace_path)
    first_private_key = commands._persisted_human_action_issuance_keys(d1_batch, key=callback_key)[
        0
    ]
    legacy_batch = first_private_key.removeprefix("bridge-human-action-issue:")
    assert len(legacy_batch) == 32

    legacy_request = support.make_request(
        "issue_human_actions",
        {
            **_context(workspace),
            "proposal_public_id": card["decision_target_proposal_public_id"],
            "reference_batch_id": legacy_batch,
            "token_ttl_seconds": 300,
            "expected_proposal_version": card["decision_target_proposal_version"],
            "expected_content_hash": card["decision_target_proposal_content_hash"],
        },
        idempotency_key=support.canonical_human_action_issuance_key(legacy_batch),
    )

    def issue(mode: str) -> support.CliOutcome:
        if mode == "legacy":
            return support.run_cli(legacy_request)
        return _issue_d1_actions(workspace, card)

    second_mode = "d1" if first_mode == "legacy" else "legacy"
    first = issue(first_mode)
    second = issue(second_mode)
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert second.exit_code == bridge_errors.EXIT_OK, second.response
    assert issue(first_mode).response["idempotent_replay"] is True
    assert issue(second_mode).response["idempotent_replay"] is True

    conn = support.open_database(workspace)
    try:
        rows = conn.execute(
            "SELECT DISTINCT refs.issuance_idempotency_key, "
            "bindings.card_generation_public_id "
            "FROM openclaw_human_action_references AS refs "
            "LEFT JOIN parser_human_draft_action_bindings AS bindings "
            "ON bindings.reference_id = refs.id"
        ).fetchall()
        legacy_keys = {row[0] for row in rows if row[1] is None}
        d1_keys = {row[0] for row in rows if row[1] == card["card_generation_public_id"]}
        assert len(legacy_keys) == 2
        assert len(d1_keys) == 1
        assert legacy_keys.isdisjoint(d1_keys)
    finally:
        conn.close()


def test_d1_issuance_fails_closed_for_mixed_partial_binding_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    card = _draft_from_redemption(workspace, redemption)
    callback_key = commands._load_callback_key(workspace.workspace_path)
    first_private_key = commands._persisted_human_action_issuance_keys(
        str(card["action_issue_batch_id"]), key=callback_key
    )[0]
    legacy_batch = first_private_key.removeprefix("bridge-human-action-issue:")
    legacy_request = support.make_request(
        "issue_human_actions",
        {
            **_context(workspace),
            "proposal_public_id": card["decision_target_proposal_public_id"],
            "reference_batch_id": legacy_batch,
            "token_ttl_seconds": 300,
            "expected_proposal_version": card["decision_target_proposal_version"],
            "expected_content_hash": card["decision_target_proposal_content_hash"],
        },
        idempotency_key=support.canonical_human_action_issuance_key(legacy_batch),
    )
    legacy = support.run_cli(legacy_request)
    assert legacy.exit_code == bridge_errors.EXIT_OK, legacy.response

    conn = support.open_database(workspace)
    try:
        reference_id = int(
            conn.execute(
                "SELECT id FROM openclaw_human_action_references "
                "WHERE issuance_idempotency_key = ? AND action = 'edit'",
                (first_private_key,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO parser_human_draft_action_bindings (
                reference_id, card_generation_public_id, draft_id,
                parser_output_id, proposal_version, proposal_content_hash,
                authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, created_at
            )
            SELECT ?, cards.card_generation_public_id, cards.draft_id,
                   cards.decision_target_parser_output_id,
                   cards.decision_target_proposal_version,
                   cards.decision_target_proposal_content_hash,
                   cards.authenticated_actor_id, cards.telegram_account_id,
                   cards.telegram_conversation_id, cards.conversation_binding_id,
                   cards.issued_at
            FROM parser_human_draft_cards AS cards
            WHERE cards.card_generation_public_id = ?
            """,
            (reference_id, card["card_generation_public_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    refused = _issue_d1_actions(workspace, card)
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    legacy_refused = support.run_cli(legacy_request)
    assert legacy_refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert legacy_refused.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0] == 6
        )
    finally:
        conn.close()


def test_d1_confirm_requires_the_exact_redeemed_durable_binding(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    accepted = _apply_whole_card(workspace, str(initial["card_generation_public_id"]))
    card = accepted.response["result"]
    issued = _issue_d1_actions(workspace, card)
    assert issued.exit_code == bridge_errors.EXIT_OK, issued.response
    redeemed = _redeem_d1_action(
        workspace,
        issued,
        action="confirm",
        callback_id="d1-binding-required",
    )
    assert redeemed.exit_code == bridge_errors.EXIT_OK, redeemed.response
    material = redeemed.response["result"]

    missing = support.run_cli(
        support.make_request(
            "confirm",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": material["proposal_public_id"],
                "operator_actor_id": ACTOR,
                "proposal_version": material["proposal_version"],
                "content_hash": material["content_hash"],
                "callback_token": material["callback_token"],
                "callback_expiry": material["callback_expiry"],
            },
            idempotency_key=material["decision_idempotency_key"],
        )
    )
    assert missing.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert missing.response["error"]["code"] == bridge_errors.LIFECYCLE_CONFLICT

    forged = _decide_d1(
        workspace,
        redeemed,
        action="confirm",
        reference_public_id="haref_" + "0" * 32,
    )
    assert forged.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert forged.response["error"]["code"] == bridge_errors.HUMAN_DRAFT_AUTHORITY_REFUSED

    correct = _decide_d1(workspace, redeemed, action="confirm")
    assert correct.exit_code == bridge_errors.EXIT_OK, correct.response
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0] == 1
        )
        assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "confirmed"
    finally:
        conn.close()


def test_incomplete_d1_card_issues_no_confirm_and_rejects_atomically(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    assert initial["completeness"] == "incomplete"
    issued = _issue_d1_actions(workspace, initial)
    assert issued.exit_code == bridge_errors.EXIT_OK, issued.response
    assert set(issued.response["result"]["actions"]) == {"reject"}
    redeemed = _redeem_d1_action(
        workspace,
        issued,
        action="reject",
        callback_id="d1-incomplete-reject",
    )
    assert redeemed.exit_code == bridge_errors.EXIT_OK, redeemed.response
    rejected = _decide_d1(workspace, redeemed, action="reject")
    assert rejected.exit_code == bridge_errors.EXIT_OK, rejected.response
    assert rejected.response["result"]["decision"] == "rejected"
    assert rejected.response["result"]["final_transaction_created"] is False
    replayed_reject = _decide_d1(workspace, redeemed, action="reject")
    assert replayed_reject.exit_code == bridge_errors.EXIT_OK, replayed_reject.response
    assert replayed_reject.response["idempotent_replay"] is True
    unbound_replay = _decide_without_d1_binding(workspace, redeemed, action="reject")
    assert unbound_replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert unbound_replay.response["error"]["code"] == bridge_errors.LIFECYCLE_CONFLICT
    conn = support.open_database(workspace)
    try:
        authorization = conn.execute(
            "SELECT confirmation_state FROM parser_proposal_authorizations"
        ).fetchone()
        assert authorization[0] == "rejected"
        assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "rejected"
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        before_counts = (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0],
        )
    finally:
        conn.close()

    expiry = int(redeemed.response["result"]["callback_expiry"])

    class _ExpiredDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return datetime.fromtimestamp(expiry + 1, tz=UTC)

    monkeypatch.setattr(commands, "datetime", _ExpiredDateTime)
    expired_replay = _decide_d1(workspace, redeemed, action="reject")
    assert expired_replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert expired_replay.response["error"]["code"] == bridge_errors.CALLBACK_EXPIRED
    conn = support.open_database(workspace)
    try:
        after_counts = (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0],
        )
        assert after_counts == before_counts
    finally:
        conn.close()


def test_multiple_updates_survive_process_boundaries_and_complete_for_fresh_review(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, redemption = _begin(workspace)
    first = _apply(workspace, session, 30, "merchant", "Example Cafe")
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert first.response["result"]["proposal_version"] == 0
    assert first.response["result"]["edit_kind"] == "d1_compatibility"
    assert first.response["result"]["final_transaction_created"] is False

    # Every CLI call reopens the same staging database, exercising restart-safe state.
    active = _get(workspace)
    assert active.response["result"]["proposal_version"] == 0
    second = _apply(workspace, session, 31, "transaction_date", "2026-09-03")
    assert second.exit_code == bridge_errors.EXIT_OK, second.response
    assert second.response["result"]["proposal_version"] == 0
    compatibility_card = second.response["result"]["human_draft_card"]
    assert compatibility_card["field_values"]["merchant"] == "Example Cafe"
    assert compatibility_card["field_values"]["transaction_date"] == "2026-09-03"

    complete = _complete(workspace, session, 32)
    assert complete.exit_code == bridge_errors.EXIT_OK, complete.response
    assert complete.response["result"]["active"] is False
    assert complete.response["result"]["proposal_version"] == 0
    assert _get(workspace).response["result"] == {
        "active": False,
        "session_status": "inactive",
        "final_transaction_created": False,
    }
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM parser_proposal_completions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parser_human_draft_operations "
                "WHERE operation_type = 'accepted'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()
    stale_card = support.run_cli(redemption)
    assert stale_card.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert stale_card.response["error"]["code"] == bridge_errors.PROPOSAL_TERMINAL_STATE


def test_same_message_replays_but_conflicting_material_fails_closed(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    first = _apply(workspace, session, 30, "description", "Lunch")
    replay = _apply(workspace, session, 30, "description", "Lunch")
    conflict = _apply(workspace, session, 30, "description", "Dinner")
    assert first.exit_code == bridge_errors.EXIT_OK
    assert replay.exit_code == bridge_errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT


def test_message_claims_are_ordered_and_one_operation_per_message(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    applied = _apply(workspace, session, 31, "merchant", "Newest")
    stale = _apply(workspace, session, 30, "merchant", "Older")
    reused_for_completion = _complete(workspace, session, 31)

    assert applied.exit_code == bridge_errors.EXIT_OK
    assert stale.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert stale.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    assert reused_for_completion.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert reused_for_completion.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    assert _get(workspace).response["result"]["proposal_version"] == 0


def test_completion_replay_is_discoverable_only_by_exact_message(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    completed = _complete(workspace, session, 30)
    replay_lookup = _get(workspace, 30)
    other_lookup = _get(workspace, 31)
    replay = _complete(workspace, session, 30)

    assert completed.exit_code == bridge_errors.EXIT_OK
    assert len(completed.response["result"]["review_batch_id"]) == 32
    assert replay_lookup.response["result"]["session_status"] == "completed_replay"
    assert replay_lookup.response["result"]["session_public_id"] == session
    assert other_lookup.response["result"]["session_status"] == "inactive"
    assert replay.exit_code == bridge_errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    assert (
        replay.response["result"]["review_batch_id"]
        == completed.response["result"]["review_batch_id"]
    )


def test_expired_review_replay_claims_one_new_redeemable_generation(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    completed = _complete(workspace, session, 30)
    result = completed.response["result"]
    first_batch = str(result["review_batch_id"])
    context = human_actions.HumanActionContext(
        actor_id=ACTOR,
        account_id=ACCOUNT,
        conversation_id=CONVERSATION,
        binding_id=BINDING,
    )
    key = (workspace.workspace_path / "runtime" / "callback_signing.key").read_bytes()
    conn = support.open_database(workspace)
    try:
        first_refs, _ = human_actions.issue_human_action_references(
            conn,
            key=key,
            issuance_idempotency_key=support.canonical_human_action_issuance_key(first_batch),
            proposal_public_id=str(result["proposal_public_id"]),
            expected_proposal_version=int(result["proposal_version"]),
            expected_proposal_content_hash=str(result["effective_content_hash"]),
            context=context,
            ttl_seconds=600,
            clock=lambda: 1_000,
        )
        with pytest.raises(human_actions.HumanActionReferenceError) as expiring:
            human_actions.issue_human_action_references(
                conn,
                key=key,
                issuance_idempotency_key=(support.canonical_human_action_issuance_key(first_batch)),
                proposal_public_id=str(result["proposal_public_id"]),
                expected_proposal_version=int(result["proposal_version"]),
                expected_proposal_content_hash=str(result["effective_content_hash"]),
                context=context,
                ttl_seconds=600,
                minimum_remaining_seconds=60,
                clock=lambda: 1_540,
            )
        assert expiring.value.reason == "reference_expiring"
    finally:
        conn.close()

    monkeypatch.setattr(guided_edit, "_now_epoch", lambda: 1_601)
    original_begin = guided_edit._begin
    claim_barrier = threading.Barrier(2)

    def concurrent_begin(conn: sqlite3.Connection) -> None:
        claim_barrier.wait(timeout=5)
        original_begin(conn)

    monkeypatch.setattr(guided_edit, "_begin", concurrent_begin)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_complete, workspace, session, 30) for _ in range(2)]
        renewed, concurrent_replay = [future.result(timeout=10) for future in futures]
    monkeypatch.setattr(guided_edit, "_begin", original_begin)
    second_batch = str(renewed.response["result"]["review_batch_id"])
    assert second_batch != first_batch
    assert concurrent_replay.response["result"]["review_batch_id"] == second_batch

    conn = support.open_database(workspace)
    try:
        second_refs, _ = human_actions.issue_human_action_references(
            conn,
            key=key,
            issuance_idempotency_key=support.canonical_human_action_issuance_key(second_batch),
            proposal_public_id=str(result["proposal_public_id"]),
            expected_proposal_version=int(result["proposal_version"]),
            expected_proposal_content_hash=str(result["effective_content_hash"]),
            context=context,
            ttl_seconds=600,
            clock=lambda: 1_601,
        )
        old_reject = next(item for item in first_refs if item.action == "reject")
        new_reject = next(item for item in second_refs if item.action == "reject")
        with pytest.raises(human_actions.HumanActionReferenceError) as expired:
            human_actions.redeem_human_action_reference(
                conn,
                key=key,
                reference=old_reject.reference,
                action="reject",
                context=context,
                callback_id="expired-review-generation",
                callback_message_id=31,
                clock=lambda: 1_601,
            )
        assert expired.value.reason == "reference_expired"
        redeemed = human_actions.redeem_human_action_reference(
            conn,
            key=key,
            reference=new_reject.reference,
            action="reject",
            context=context,
            callback_id="current-review-generation",
            callback_message_id=31,
            clock=lambda: 1_601,
        )
        assert redeemed.action == "reject"
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_guided_edit_review_generations").fetchone()[
                0
            ]
            == 2
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE openclaw_guided_edit_review_generations SET generation = generation + 1"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM openclaw_guided_edit_review_generations")
        conn.rollback()
    finally:
        conn.close()


def test_consumed_edit_reference_forces_fresh_review_generation(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, session, _redemption = _begin(workspace)
    completed = _complete(workspace, session, 30).response["result"]

    def issue_batch(batch_id: str) -> support.CliOutcome:
        return support.run_cli(
            support.make_request(
                "issue_human_actions",
                {
                    **_context(workspace),
                    "proposal_public_id": proposal,
                    "reference_batch_id": batch_id,
                    "token_ttl_seconds": 600,
                    "minimum_remaining_seconds": 60,
                    "require_unconsumed_replay": True,
                    "expected_proposal_version": completed["proposal_version"],
                    "expected_content_hash": completed["effective_content_hash"],
                },
                idempotency_key=support.canonical_human_action_issuance_key(batch_id),
            )
        )

    def issue_edit_reference(batch_id: str) -> str:
        issued = issue_batch(batch_id)
        assert issued.exit_code == bridge_errors.EXIT_OK, issued.response
        return str(issued.response["result"]["actions"]["edit"]["reference"])

    first_batch = str(completed["review_batch_id"])
    first_reference = issue_edit_reference(first_batch)
    claimed_before_consumption = _complete(workspace, session, 30).response["result"]
    assert claimed_before_consumption["review_batch_id"] == first_batch
    first_callback_id = "consume-first-guided-review-edit"
    first_redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": first_reference,
                "action": "edit",
                "callback_id": first_callback_id,
                "callback_message_id": 31,
            },
            idempotency_key=support.canonical_human_action_redemption_key(first_callback_id),
        )
    )
    assert first_redeemed.exit_code == bridge_errors.EXIT_OK, first_redeemed.response

    refused_consumed_batch = issue_batch(first_batch)
    assert refused_consumed_batch.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused_consumed_batch.response["error"]["code"] == (bridge_errors.CALLBACK_EXPIRED)

    replayed_completion = _complete(workspace, session, 30).response["result"]
    second_batch = str(replayed_completion["review_batch_id"])
    assert second_batch != first_batch
    second_reference = issue_edit_reference(second_batch)
    second_callback_id = "consume-renewed-guided-review-edit"
    second_redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": second_reference,
                "action": "edit",
                "callback_id": second_callback_id,
                "callback_message_id": 32,
            },
            idempotency_key=support.canonical_human_action_redemption_key(second_callback_id),
        )
    )
    assert second_redeemed.exit_code == bridge_errors.EXIT_OK, second_redeemed.response


def test_new_session_inherits_context_high_water_and_old_completion_wins_lookup(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, first_session, _redemption = _begin(workspace)
    assert _complete(workspace, first_session, 200).exit_code == bridge_errors.EXIT_OK
    review = support.run_cli(
        support.make_request(
            "get_review",
            {"workspace_path": str(workspace.workspace_path), "proposal_public_id": proposal},
        )
    ).response["result"]
    batch = "a" * 32
    issued = support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **_context(workspace),
                "proposal_public_id": proposal,
                "reference_batch_id": batch,
                "token_ttl_seconds": 600,
                "expected_proposal_version": review["proposal_version"],
                "expected_content_hash": review["effective_content_hash"],
            },
            idempotency_key=support.canonical_human_action_issuance_key(batch),
        )
    )
    callback_id = "older-card-new-session"
    redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": issued.response["result"]["actions"]["edit"]["reference"],
                "action": "edit",
                "callback_id": callback_id,
                "callback_message_id": 150,
            },
            idempotency_key=support.canonical_human_action_redemption_key(callback_id),
        )
    )
    second_session = str(redeemed.response["result"]["guided_edit_session_public_id"])
    assert second_session != first_session

    old_completion_lookup = _get(workspace, 200)
    late_update = _apply(workspace, second_session, 200, "merchant", "Wrong Proposal")
    late_completion = _complete(workspace, second_session, 200)
    assert old_completion_lookup.response["result"]["session_public_id"] == first_session
    assert old_completion_lookup.response["result"]["session_status"] == "completed_replay"
    assert late_update.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert late_update.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    assert late_completion.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert late_completion.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    active = _get(workspace).response["result"]
    assert active["session_public_id"] == second_session
    assert active["proposal_version"] == 0


def test_core_replay_crossing_expiry_is_settled_as_applied(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, session, _redemption = _begin(workspace)
    active = _get(workspace).response["result"]
    operation_key = support.canonical_edit_key(
        proposal_public_id=proposal,
        version=int(active["proposal_version"]),
        content_hash=str(active["effective_content_hash"]),
    )
    _capture_routed_message(workspace, "merchant=Committed During Race", 30)
    conn = support.open_database(workspace)
    try:
        row = conn.execute(
            "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session,),
        ).fetchone()
        pending = guided_edit.request_update(
            conn,
            dict(row),
            message_id=30,
            operation_key=operation_key,
            field_name="merchant",
            field_value="Committed During Race",
        )
    finally:
        conn.close()

    missed = threading.Event()
    core_committed = threading.Event()
    original_pending_core_state = guided_edit.pending_core_state
    first_lookup = True

    def delayed_first_lookup(
        conn: object, current: dict[str, object]
    ) -> tuple[int, int, str] | None:
        nonlocal first_lookup
        if first_lookup:
            first_lookup = False
            result = original_pending_core_state(conn, current)  # type: ignore[arg-type]
            assert result is None
            missed.set()
            assert core_committed.wait(timeout=5)
            return None
        return original_pending_core_state(conn, current)  # type: ignore[arg-type]

    monkeypatch.setattr(guided_edit, "pending_core_state", delayed_first_lookup)
    monkeypatch.setattr(
        guided_edit,
        "_now_epoch",
        lambda: int(active["expires_at"]) + 1,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        replay_future = executor.submit(
            _apply, workspace, session, 30, "merchant", "Committed During Race"
        )
        assert missed.wait(timeout=5)
        writer = support.open_database(workspace)
        try:
            complete_proposal(
                writer,
                int(pending["current_parser_output_id"]),
                actor=ACTOR,
                expected_content_hash=str(pending["current_content_hash"]),
                field_updates={"merchant": "Committed During Race"},
                completion_public_id=identity.completion_public_id(operation_key),
                completion_channel="openclaw_staging_bridge",
            )
        finally:
            writer.close()
        core_committed.set()
        replay = replay_future.result(timeout=5)

    assert replay.exit_code == bridge_errors.EXIT_OK, replay.response
    conn = support.open_database(workspace)
    try:
        state = conn.execute(
            "SELECT current_proposal_version, pending_message_id "
            "FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session,),
        ).fetchone()
        assert tuple(state) == (1, None)
        assert (
            conn.execute(
                "SELECT event_type FROM openclaw_guided_edit_events "
                "WHERE telegram_message_id = 30 ORDER BY sequence_number DESC LIMIT 1"
            ).fetchone()[0]
            == "update_applied"
        )
    finally:
        conn.close()


def test_refusal_settlement_rechecks_core_result_under_write_lock(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, session, _redemption = _begin(workspace)
    active = _get(workspace).response["result"]
    operation_key = support.canonical_edit_key(
        proposal_public_id=proposal,
        version=0,
        content_hash=str(active["effective_content_hash"]),
    )
    _capture_routed_message(workspace, "category=Meals", 30)
    conn = support.open_database(workspace)
    try:
        session_row = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session,),
            ).fetchone()
        )
        pending = guided_edit.request_update(
            conn,
            session_row,
            message_id=30,
            operation_key=operation_key,
            field_name="category",
            field_value="Meals",
        )
        assert guided_edit.pending_core_state(conn, pending) is None
        complete_proposal(
            conn,
            int(pending["current_parser_output_id"]),
            actor=ACTOR,
            expected_content_hash=str(pending["current_content_hash"]),
            field_updates={"category": "Meals"},
            completion_public_id=identity.completion_public_id(operation_key),
            completion_channel="openclaw_staging_bridge",
        )
        guided_edit.record_update_refused(conn, pending, bridge_errors.CALLBACK_EXPIRED)
        event = conn.execute(
            "SELECT event_type, refusal_code FROM openclaw_guided_edit_events "
            "WHERE telegram_message_id = 30 ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
        assert tuple(event) == ("update_applied", None)
    finally:
        conn.close()


def test_stale_refusal_settler_cannot_clear_a_later_pending_message(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, session, _redemption = _begin(workspace)
    active = _get(workspace).response["result"]
    first_operation_key = support.canonical_edit_key(
        proposal_public_id=proposal,
        version=0,
        content_hash=str(active["effective_content_hash"]),
    )
    _capture_routed_message(workspace, "category=Invalid First", 30)
    _capture_routed_message(workspace, "merchant=Valid Second", 31)
    conn = support.open_database(workspace)
    try:
        session_row = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session,),
            ).fetchone()
        )
        first_pending = guided_edit.request_update(
            conn,
            session_row,
            message_id=30,
            operation_key=first_operation_key,
            field_name="category",
            field_value="Invalid First",
        )
        guided_edit.record_update_refused(conn, first_pending, bridge_errors.ARGUMENTS_REFUSED)
        current = dict(
            conn.execute(
                "SELECT sessions.*, proposals.public_id AS proposal_public_id "
                "FROM openclaw_guided_edit_sessions AS sessions "
                "JOIN parser_outputs AS proposals "
                "ON proposals.id = sessions.current_parser_output_id "
                "WHERE sessions.session_public_id = ?",
                (session,),
            ).fetchone()
        )
        second_pending = guided_edit.request_update(
            conn,
            current,
            message_id=31,
            operation_key=first_operation_key,
            field_name="merchant",
            field_value="Valid Second",
        )

        guided_edit.record_update_refused(conn, first_pending, bridge_errors.ARGUMENTS_REFUSED)
        after = conn.execute(
            "SELECT pending_message_id, pending_field_name, pending_field_value_json "
            "FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session,),
        ).fetchone()
        assert tuple(after) == (
            31,
            "merchant",
            second_pending["pending_field_value_json"],
        )
        events = conn.execute(
            "SELECT telegram_message_id, event_type FROM openclaw_guided_edit_events "
            "WHERE telegram_message_id IN (30, 31) ORDER BY sequence_number"
        ).fetchall()
        assert [tuple(row) for row in events] == [
            (30, "update_requested"),
            (30, "update_refused"),
            (31, "update_requested"),
        ]
    finally:
        conn.close()


def test_stale_applied_settler_cannot_clear_a_later_pending_message(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, session, _redemption = _begin(workspace)
    active = _get(workspace).response["result"]
    first_operation_key = support.canonical_edit_key(
        proposal_public_id=proposal,
        version=0,
        content_hash=str(active["effective_content_hash"]),
    )
    _capture_routed_message(workspace, "category=Meals", 30)
    _capture_routed_message(workspace, "merchant=Valid Second", 31)
    conn = support.open_database(workspace)
    try:
        session_row = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session,),
            ).fetchone()
        )
        first_pending = guided_edit.request_update(
            conn,
            session_row,
            message_id=30,
            operation_key=first_operation_key,
            field_name="category",
            field_value="Meals",
        )
        complete_proposal(
            conn,
            int(first_pending["current_parser_output_id"]),
            actor=ACTOR,
            expected_content_hash=str(first_pending["current_content_hash"]),
            field_updates={"category": "Meals"},
            completion_public_id=identity.completion_public_id(first_operation_key),
            completion_channel="openclaw_staging_bridge",
        )
        first_state = guided_edit.pending_core_state(conn, first_pending)
        assert first_state is not None
        guided_edit.record_update_applied(
            conn,
            first_pending,
            parser_output_id=first_state[0],
            proposal_version=first_state[1],
            content_hash=first_state[2],
        )
        current = dict(
            conn.execute(
                "SELECT sessions.*, proposals.public_id AS proposal_public_id "
                "FROM openclaw_guided_edit_sessions AS sessions "
                "JOIN parser_outputs AS proposals "
                "ON proposals.id = sessions.current_parser_output_id "
                "WHERE sessions.session_public_id = ?",
                (session,),
            ).fetchone()
        )
        second_operation_key = support.canonical_edit_key(
            proposal_public_id=proposal,
            version=int(current["current_proposal_version"]),
            content_hash=str(current["current_content_hash"]),
        )
        second_pending = guided_edit.request_update(
            conn,
            current,
            message_id=31,
            operation_key=second_operation_key,
            field_name="merchant",
            field_value="Valid Second",
        )

        guided_edit.record_update_applied(
            conn,
            first_pending,
            parser_output_id=first_state[0],
            proposal_version=first_state[1],
            content_hash=first_state[2],
        )
        after = conn.execute(
            "SELECT pending_message_id, pending_operation_key, pending_field_name, "
            "pending_field_value_json FROM openclaw_guided_edit_sessions "
            "WHERE session_public_id = ?",
            (session,),
        ).fetchone()
        assert tuple(after) == (
            31,
            second_operation_key,
            "merchant",
            second_pending["pending_field_value_json"],
        )
        events = conn.execute(
            "SELECT telegram_message_id, event_type FROM openclaw_guided_edit_events "
            "WHERE telegram_message_id IN (30, 31) ORDER BY sequence_number"
        ).fetchall()
        assert [tuple(row) for row in events] == [
            (30, "update_requested"),
            (30, "update_applied"),
            (31, "update_requested"),
        ]
    finally:
        conn.close()


def test_committed_edit_with_lost_session_ack_is_recovered_idempotently(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    original = guided_edit.record_update_applied
    calls = 0

    def lose_first_ack(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic crash after authoritative edit commit")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(guided_edit, "record_update_applied", lose_first_ack)
    unknown = _apply(workspace, session, 30, "category", "Meals")
    assert unknown.exit_code == bridge_errors.EXIT_INTERNAL
    assert _get(workspace).response["result"]["recovery_required"] is True

    recovered = _apply(workspace, session, 30, "category", "Meals")
    assert recovered.exit_code == bridge_errors.EXIT_OK, recovered.response
    assert recovered.response["idempotent_replay"] is True
    assert _get(workspace).response["result"]["recovery_required"] is False


def test_committed_edit_recovery_remains_available_after_expiry(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    expires_at = int(_get(workspace).response["result"]["expires_at"])
    original = guided_edit.record_update_applied
    calls = 0

    def lose_first_ack(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic lost session acknowledgement")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(guided_edit, "record_update_applied", lose_first_ack)
    unknown = _apply(workspace, session, 30, "category", "Meals")
    assert unknown.exit_code == bridge_errors.EXIT_INTERNAL
    monkeypatch.setattr(guided_edit, "_now_epoch", lambda: expires_at + 1)

    recovered = _apply(workspace, session, 30, "category", "Meals")
    different = _apply(workspace, session, 31, "category", "Travel")
    assert recovered.exit_code == bridge_errors.EXIT_OK, recovered.response
    assert recovered.response["idempotent_replay"] is True
    assert different.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert different.response["error"]["code"] == bridge_errors.CALLBACK_EXPIRED


def test_expiry_between_claim_and_core_write_refuses_without_financial_mutation(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    expires_at = int(_get(workspace).response["result"]["expires_at"])
    times = iter((expires_at - 1, expires_at))
    monkeypatch.setattr(guided_edit, "_now_epoch", lambda: next(times))

    refused = _apply(workspace, session, 30, "merchant", "Too Late")
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.CALLBACK_EXPIRED
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM parser_proposal_completions").fetchone()[0] == 0
        event = conn.execute(
            "SELECT event_type, refusal_code FROM openclaw_guided_edit_events "
            "WHERE telegram_message_id = 30 ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
        assert tuple(event) == ("update_refused", bridge_errors.CALLBACK_EXPIRED)
    finally:
        conn.close()


def test_direct_guided_command_rejects_control_characters(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    refused = _apply(workspace, session, 30, "merchant", "Safe\nForged")
    assert refused.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.ARGUMENTS_REFUSED
    assert _get(workspace).response["result"]["proposal_version"] == 0


def test_completion_rechecks_proposal_snapshot_inside_transaction(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    conn = support.open_database(workspace)
    try:
        row = conn.execute(
            "SELECT current_parser_output_id, current_content_hash "
            "FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session,),
        ).fetchone()
        complete_proposal(
            conn,
            int(row[0]),
            actor=ACTOR,
            expected_content_hash=str(row[1]),
            field_updates={"merchant": "External Change"},
            completion_public_id="pco_external_guided_edit_test",
            completion_channel="test",
        )
    finally:
        conn.close()

    refused = _complete(workspace, session, 30)
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.STALE_CONTENT_HASH


def test_expired_uncommitted_pending_is_audited_and_fresh_session_can_start(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal, session, _redemption = _begin(workspace)
    active = _get(workspace).response["result"]
    expires_at = int(active["expires_at"])
    _capture_routed_message(workspace, "merchant=Never Committed", 30)
    conn = support.open_database(workspace)
    try:
        row = conn.execute(
            "SELECT sessions.*, proposals.public_id AS proposal_public_id "
            "FROM openclaw_guided_edit_sessions AS sessions "
            "JOIN parser_outputs AS proposals ON proposals.id = sessions.current_parser_output_id "
            "WHERE sessions.session_public_id = ?",
            (session,),
        ).fetchone()
        guided_edit.request_update(
            conn,
            dict(row),
            message_id=30,
            operation_key=(
                f"bridge-edit:{proposal}:v{active['proposal_version']}:"
                f"{active['effective_content_hash']}"
            ),
            field_name="merchant",
            field_value="Never Committed",
        )
    finally:
        conn.close()

    review = support.run_cli(
        support.make_request(
            "get_review",
            {"workspace_path": str(workspace.workspace_path), "proposal_public_id": proposal},
        )
    ).response["result"]
    batch = "f" * 32
    issued = support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **_context(workspace),
                "proposal_public_id": proposal,
                "reference_batch_id": batch,
                "token_ttl_seconds": 1200,
                "expected_proposal_version": review["proposal_version"],
                "expected_content_hash": review["effective_content_hash"],
            },
            idempotency_key=support.canonical_human_action_issuance_key(batch),
        )
    )
    monkeypatch.setattr(guided_edit, "_now_epoch", lambda: expires_at + 1)
    callback_id = "fresh-guided-edit-callback"
    redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": issued.response["result"]["actions"]["edit"]["reference"],
                "action": "edit",
                "callback_id": callback_id,
                "callback_message_id": 40,
            },
            idempotency_key=support.canonical_human_action_redemption_key(callback_id),
        )
    )
    assert redeemed.exit_code == bridge_errors.EXIT_OK, redeemed.response
    fresh_session = str(redeemed.response["result"]["guided_edit_session_public_id"])
    assert fresh_session != session
    stale_old_message = _apply(workspace, fresh_session, 30, "merchant", "Late Old Edit")
    assert stale_old_message.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert stale_old_message.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT

    conn = support.open_database(workspace)
    try:
        old = conn.execute(
            "SELECT status, pending_message_id FROM openclaw_guided_edit_sessions "
            "WHERE session_public_id = ?",
            (session,),
        ).fetchone()
        assert tuple(old) == ("abandoned", None)
        refused = conn.execute(
            "SELECT refusal_code FROM openclaw_guided_edit_events "
            "WHERE session_id = (SELECT id FROM openclaw_guided_edit_sessions "
            "WHERE session_public_id = ?) AND event_type = 'update_refused'",
            (session,),
        ).fetchone()
        assert refused[0] == bridge_errors.CALLBACK_EXPIRED
    finally:
        conn.close()


def test_text_monetary_edit_maps_to_d1_without_legacy_completion_rows(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    accepted = _apply(workspace, session, 30, "amount", "321.89")
    replay = _apply(workspace, session, 30, "amount", "321.89")
    assert accepted.exit_code == bridge_errors.EXIT_OK, accepted.response
    assert accepted.response["result"]["edit_kind"] == "d1_compatibility"
    assert accepted.response["result"]["human_draft_card"]["field_values"]["amount"] == "321.89"
    assert replay.exit_code == bridge_errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    active = _get(workspace)
    assert active.response["result"]["active"] is True
    assert active.response["result"]["proposal_version"] == 0
    assert active.response["result"]["recovery_required"] is False
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM parser_proposal_completions").fetchone()[0] == 0
    finally:
        conn.close()


def _begin_receipt_session(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    support.write_handoff_file(workspace, "guided-receipt.jpg", support.JPEG_BYTES)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="guided-receipt.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    engine = support.FakeOcrEngine(blocks=_sgd_blocks())
    monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: engine)
    proposed = support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": capture.response["result"]["intake_public_id"],
            },
            idempotency_key=support.canonical_propose_key(
                capture.response["result"]["intake_public_id"]
            ),
        )
    )
    original = str(proposed.response["result"]["proposal_public_id"])

    # Start a session for the receipt proposal using the same durable action boundary.
    review = support.run_cli(
        support.make_request(
            "get_review",
            {"workspace_path": str(workspace.workspace_path), "proposal_public_id": original},
        )
    ).response["result"]
    batch = "e" * 32
    issued = support.run_cli(
        support.make_request(
            "issue_human_actions",
            {
                **_context(workspace),
                "proposal_public_id": original,
                "reference_batch_id": batch,
                "token_ttl_seconds": 600,
                "expected_proposal_version": review["proposal_version"],
                "expected_content_hash": review["effective_content_hash"],
            },
            idempotency_key=support.canonical_human_action_issuance_key(batch),
        )
    )
    callback_id = "guided-receipt-callback"
    redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": issued.response["result"]["actions"]["edit"]["reference"],
                "action": "edit",
                "callback_id": callback_id,
                "callback_message_id": 21,
            },
            idempotency_key=support.canonical_human_action_redemption_key(callback_id),
        )
    )
    session = str(redeemed.response["result"]["guided_edit_session_public_id"])
    return original, session


def test_receipt_amount_edit_uses_supersession_and_keeps_session_on_replacement(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, session = _begin_receipt_session(workspace, monkeypatch)
    corrected = _apply(workspace, session, 30, "amount", "56.78")
    assert corrected.exit_code == bridge_errors.EXIT_OK, corrected.response
    replacement = corrected.response["result"]["proposal_public_id"]
    assert corrected.response["result"]["edit_kind"] == "d1_compatibility"
    assert replacement != original
    active = _get(workspace).response["result"]
    assert active["proposal_public_id"] == replacement
    assert active["proposal_version"] == 0
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute(
                "SELECT parse_status FROM parser_outputs WHERE public_id = ?", (original,)
            ).fetchone()[0]
            == "superseded"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parser_human_draft_operations "
                "WHERE operation_type = 'accepted'"
            ).fetchone()[0]
            == 1
        )
        assert all(count == 0 for count in support.count_final_facts(conn).values())
    finally:
        conn.close()


def test_receipt_edit_expiry_guard_runs_under_authoritative_write_lock(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, session = _begin_receipt_session(workspace, monkeypatch)
    expires_at = int(_get(workspace).response["result"]["expires_at"])
    times = iter((expires_at - 1, expires_at))
    monkeypatch.setattr(guided_edit, "_now_epoch", lambda: next(times))

    refused = _apply(workspace, session, 30, "amount", "56.78")
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.CALLBACK_EXPIRED
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_revisions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT parse_status FROM parser_outputs WHERE public_id = ?", (original,)
            ).fetchone()[0]
            != "superseded"
        )
    finally:
        conn.close()


def test_session_is_bound_to_exact_private_conversation_context(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    wrong = support.run_cli(
        support.make_request(
            "apply_guided_edit_update",
            {
                **_context(workspace),
                "conversation_binding_id": "binding-other",
                "session_public_id": session,
                "telegram_message_id": 30,
                "field_name": "merchant",
                "field_value": "Nope",
            },
            idempotency_key=f"bridge-guided-edit-update:{session}:30",
        )
    )
    assert wrong.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert _get(workspace).response["result"]["proposal_version"] == 0
