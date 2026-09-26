"""A frozen Telegram route is required by every new text/control write."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.capture_jobs import ensure_capture_job
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.telegram_text_adapter import (
    process_openclaw_telegram_text_message,
    process_telegram_text_update,
    validate_telegram_text_update,
)
from finance_core.openclaw_staging_bridge import errors, identity
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    record_telegram_source_context,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _context(workspace: support.BridgeWorkspace) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "operator_actor_id": "111",
        "telegram_account_id": "finance-account",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "binding-1",
    }


def _capture(
    workspace: support.BridgeWorkspace,
    text: str,
    *,
    message_id: int,
    command: str = "capture_interaction",
) -> support.CliOutcome:
    update = support.telegram_text_update(text, message_id=message_id)
    arguments = support.capture_text_arguments(workspace, update)
    if command == "capture_interaction":
        arguments.pop("kind")
    arguments.update(
        {
            "authenticated_actor_id": "111",
            "telegram_account_id": "finance-account",
            "telegram_conversation_id": "111",
            "conversation_binding_id": "binding-1",
            "finance_ingress": {
                "channel": "telegram",
                "accountId": "finance-account",
                "updateId": 1,
                "chatId": 111,
                "messageId": message_id,
                "senderId": 111,
                "payloadSha256": "a" * 64,
                "bindingId": "binding-1",
            },
        }
    )
    return support.run_cli(
        support.make_request(
            command,
            arguments,
            idempotency_key=support.canonical_capture_key(message_id=message_id),
        )
    )


@pytest.mark.parametrize(
    "text",
    [
        "lunch 12.50",
        "完成",
        "Card Ref: d1card_" + "a" * 32 + "\nAmount: 12.50",
    ],
)
@pytest.mark.parametrize("entry", ["bot_api", "openclaw"])
def test_legacy_public_telegram_entry_cannot_bypass_d3_route(
    workspace: support.BridgeWorkspace, text: str, entry: str
) -> None:
    with support.open_database(workspace) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="route"):
            update = support.telegram_text_update(text)
            if entry == "bot_api":
                process_telegram_text_update(conn, update)
            else:
                process_openclaw_telegram_text_message(conn, update["message"])
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0


def test_status_does_not_disclose_frozen_route_without_context(
    workspace: support.BridgeWorkspace,
) -> None:
    captured = _capture(workspace, "lunch 12.50", message_id=26)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    intake_id = captured.response["result"]["intake_public_id"]
    job_id = captured.response["result"]["capture_job"]["public_id"]
    for key, value in (("intake_public_id", intake_id), ("job_public_id", job_id)):
        status = support.run_cli(
            support.make_request(
                "get_status", {"workspace_path": str(workspace.workspace_path), key: value}
            )
        )
        assert status.exit_code == errors.EXIT_OK, status.response
        assert "interaction_route" not in status.response["result"]
    wrong_context = {**_context(workspace), "conversation_binding_id": "other-binding"}
    denied = support.run_cli(
        support.make_request("get_interaction_route", {**wrong_context, "telegram_message_id": 26})
    )
    assert denied.exit_code == errors.EXIT_OK
    assert denied.response["result"]["found"] is False
    allowed = support.run_cli(
        support.make_request(
            "get_interaction_route", {**_context(workspace), "telegram_message_id": 26}
        )
    )
    assert allowed.exit_code == errors.EXIT_OK
    assert allowed.response["result"]["interaction_route"]["route_kind"] == "initial_intake"


def _begin_guided(workspace: support.BridgeWorkspace) -> tuple[str, str]:
    capture = _capture(workspace, "lunch 12.50", message_id=10)
    assert capture.exit_code == errors.EXIT_OK, capture.response
    job_id = capture.response["result"]["capture_job"]["public_id"]
    processed = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert processed.exit_code == errors.EXIT_OK, processed.response
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
    assert issued.exit_code == errors.EXIT_OK, issued.response
    reference = issued.response["result"]["actions"]["edit"]["reference"]
    redeemed = support.run_cli(
        support.make_request(
            "redeem_human_action",
            {
                **_context(workspace),
                "short_reference": reference,
                "action": "edit",
                "callback_id": "guided-route-callback",
                "callback_message_id": 20,
            },
            idempotency_key=support.canonical_human_action_redemption_key("guided-route-callback"),
        )
    )
    assert redeemed.exit_code == errors.EXIT_OK, redeemed.response
    session = str(redeemed.response["result"]["guided_edit_session_public_id"])
    card = str(redeemed.response["result"]["human_draft_card"]["card_generation_public_id"])
    return session, card


def test_legacy_text_capture_uses_same_route_and_never_parses_inline(
    workspace: support.BridgeWorkspace,
) -> None:
    control = _capture(workspace, "完成", message_id=21, command="capture")
    assert control.exit_code == errors.EXIT_OK, control.response
    result = control.response["result"]
    assert result["interaction_route"]["route_kind"] == "control_refused"
    assert result["proposal_public_id"] is None
    assert result["capture_job"]["status"] == "needs_attention"
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
    replay = _capture(workspace, "完成", message_id=21, command="capture")
    assert replay.exit_code == errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True


def test_fresh_unauthenticated_legacy_capture_cannot_create_no_route_job(
    workspace: support.BridgeWorkspace,
) -> None:
    result = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace, support.telegram_text_update("lunch 12.50", message_id=22)
            ),
            idempotency_key=support.canonical_capture_key(message_id=22),
        )
    )
    assert result.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_jobs").fetchone()[0] == 0


def test_exact_historical_no_route_capture_is_read_only_replay(
    workspace: support.BridgeWorkspace,
) -> None:
    update = support.telegram_text_update("lunch 12.50", message_id=23)
    validated = validate_telegram_text_update(update)
    ingress = {
        "channel": "telegram",
        "accountId": "finance-account",
        "updateId": 1,
        "chatId": 111,
        "messageId": 23,
        "senderId": 111,
        "payloadSha256": "a" * 64,
        "bindingId": "binding-1",
    }
    digest = hashlib.sha256(
        json.dumps(ingress, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with support.open_database(workspace) as conn:
        conn.execute("BEGIN IMMEDIATE")
        intake = create_raw_intake_record(
            conn,
            validated.text,
            source_channel="telegram",
            source_metadata=validated.source_metadata,
        )
        record_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=TelegramSourceContext(
                authenticated_actor_id="111",
                account_id="finance-account",
                conversation_id="111",
                binding_id="binding-1",
                message_id="23",
            ),
            captured_at=str(intake["received_at"]),
        )
        ensure_capture_job(
            conn,
            intake_id=int(intake["id"]),
            capture_kind="text",
            ingress_identity_digest=digest,
        )
        conn.commit()
    replay = _capture(workspace, "lunch 12.50", message_id=23, command="capture")
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    with support.open_database(workspace) as conn:
        routes = conn.execute("SELECT COUNT(*) FROM finance_capture_interaction_routes")
        assert routes.fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0


def test_historical_capture_with_saved_context_requires_that_context_on_replay(
    workspace: support.BridgeWorkspace,
) -> None:
    update = support.telegram_text_update("old lunch 12.50", message_id=25)
    validated = validate_telegram_text_update(update)
    with support.open_database(workspace) as conn:
        conn.execute("BEGIN IMMEDIATE")
        intake = create_raw_intake_record(
            conn,
            validated.text,
            source_channel="telegram",
            source_metadata=validated.source_metadata,
        )
        record_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=TelegramSourceContext(
                authenticated_actor_id="111",
                account_id="finance-account",
                conversation_id="111",
                binding_id="binding-1",
                message_id="25",
            ),
            captured_at=str(intake["received_at"]),
        )
        ensure_capture_job(conn, intake_id=int(intake["id"]), capture_kind="text")
        conn.commit()
    missing_context = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(workspace, update),
            idempotency_key=support.canonical_capture_key(message_id=25),
        )
    )
    assert missing_context.exit_code == errors.EXIT_AUTHORITY_REFUSED
    exact_arguments = support.authenticated_text_capture_arguments(workspace, update)
    exact_arguments.pop("finance_ingress")
    exact = support.run_cli(
        support.make_request(
            "capture",
            exact_arguments,
            idempotency_key=support.canonical_capture_key(message_id=25),
        )
    )
    assert exact.exit_code == errors.EXIT_OK, exact.response
    assert exact.response["idempotent_replay"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_account_id", "finance account"),
        ("conversation_binding_id", "b" * 201),
        ("authenticated_actor_id", "0111"),
    ],
)
def test_capture_context_uses_direct_human_canonical_bounds(
    workspace: support.BridgeWorkspace, field: str, value: str
) -> None:
    update = support.telegram_text_update("lunch 12.50", message_id=24)
    args = support.capture_text_arguments(workspace, update)
    args.pop("kind")
    args.update(
        {
            "authenticated_actor_id": "111",
            "telegram_account_id": "finance-account",
            "telegram_conversation_id": "111",
            "conversation_binding_id": "binding-1",
            "finance_ingress": {
                "channel": "telegram",
                "accountId": "finance-account",
                "updateId": 1,
                "chatId": 111,
                "messageId": 24,
                "senderId": 111,
                "payloadSha256": "a" * 64,
                "bindingId": "binding-1",
            },
        }
    )
    args[field] = value
    outcome = support.run_cli(
        support.make_request(
            "capture_interaction",
            args,
            idempotency_key=support.canonical_capture_key(message_id=24),
        )
    )
    assert outcome.exit_code != errors.EXIT_OK
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0


def test_guided_complete_cannot_consume_fabricated_whole_card_route(
    workspace: support.BridgeWorkspace,
) -> None:
    session, card = _begin_guided(workspace)
    fabricated = _capture(
        workspace,
        f"Card Ref: {card}\nAmount: 12.50\nCurrency: SGD\nDate: 2026-09-26"
        "\nMerchant: Shop\nDescription: Lunch\nCategory: Food",
        message_id=21,
    )
    assert fabricated.exit_code == errors.EXIT_OK, fabricated.response
    assert fabricated.response["result"]["interaction_route"]["route_kind"] == "whole_card"
    completed = support.run_cli(
        support.make_request(
            "complete_guided_edit",
            {**_context(workspace), "session_public_id": session, "telegram_message_id": 21},
            idempotency_key=f"bridge-guided-edit-complete:{session}:21",
        )
    )
    assert completed.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        status = conn.execute(
            "SELECT status FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session,),
        ).fetchone()[0]
    assert status == "active"


def test_d1_card_requires_matching_frozen_text_and_operation(
    workspace: support.BridgeWorkspace,
) -> None:
    _session, card = _begin_guided(workspace)
    raw_text = (
        f"Card Ref: {card}\nAmount: 12.50\nCurrency: SGD\nDate: 2026-09-26"
        "\nMerchant: Shop\nDescription: Lunch\nCategory: Food"
    )
    fields = {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-26",
        "merchant": "Shop",
        "description": "Lunch",
        "category": "Food",
    }

    def apply(operation: str) -> support.CliOutcome:
        return support.run_cli(
            support.make_request(
                "apply_human_draft_card",
                {
                    **_context(workspace),
                    "card_generation_public_id": card,
                    "telegram_message_id": 30,
                    "operation_public_id": operation,
                    "raw_card_text": raw_text,
                    "field_values": fields,
                },
                idempotency_key=f"bridge-human-draft-apply:{operation}",
            )
        )

    assert apply("d1op_fabricated").exit_code == errors.EXIT_AUTHORITY_REFUSED
    frozen = _capture(workspace, raw_text, message_id=30)
    assert frozen.exit_code == errors.EXIT_OK, frozen.response
    route = frozen.response["result"]["interaction_route"]
    assert route["route_kind"] == "whole_card"
    assert apply("d1op_fabricated").exit_code == errors.EXIT_AUTHORITY_REFUSED
    accepted = apply(str(route["operation_key"]))
    assert accepted.exit_code == errors.EXIT_OK, accepted.response
    replay = apply(str(route["operation_key"]))
    assert replay.exit_code == errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True


def test_guided_update_and_completion_consume_their_frozen_routes(
    workspace: support.BridgeWorkspace,
) -> None:
    session, _card = _begin_guided(workspace)
    update_args = {
        **_context(workspace),
        "session_public_id": session,
        "telegram_message_id": 21,
        "field_name": "description",
        "field_value": "Dinner",
    }
    update_key = f"bridge-guided-edit-update:{session}:21"
    missing = support.run_cli(
        support.make_request("apply_guided_edit_update", update_args, idempotency_key=update_key)
    )
    assert missing.exit_code == errors.EXIT_AUTHORITY_REFUSED
    captured = _capture(workspace, "description=Dinner", message_id=21)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    assert captured.response["result"]["interaction_route"]["route_kind"] == "guided_update"
    updated = support.run_cli(
        support.make_request("apply_guided_edit_update", update_args, idempotency_key=update_key)
    )
    assert updated.exit_code == errors.EXIT_OK, updated.response

    completed_args = {
        **_context(workspace),
        "session_public_id": session,
        "telegram_message_id": 22,
    }
    complete_key = f"bridge-guided-edit-complete:{session}:22"
    missing_complete = support.run_cli(
        support.make_request("complete_guided_edit", completed_args, idempotency_key=complete_key)
    )
    assert missing_complete.exit_code == errors.EXIT_AUTHORITY_REFUSED
    frozen_complete = _capture(workspace, "完成", message_id=22)
    assert frozen_complete.exit_code == errors.EXIT_OK, frozen_complete.response
    assert frozen_complete.response["result"]["interaction_route"]["route_kind"] == (
        "guided_complete"
    )
    completed = support.run_cli(
        support.make_request("complete_guided_edit", completed_args, idempotency_key=complete_key)
    )
    assert completed.exit_code == errors.EXIT_OK, completed.response
