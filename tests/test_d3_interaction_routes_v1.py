"""Synthetic adoption tests for frozen Telegram interaction routing."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import commands, errors, identity
from tests.test_openclaw_staging_bridge_guided_edit_v1 import _apply, _begin, _complete


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _request(
    workspace: support.BridgeWorkspace, text: str, *, message_id: int = 10,
    ingress_hash: str = "a" * 64, account: str = "finance", binding: str = "bind-1",
) -> dict:
    payload = support.telegram_text_update(text, message_id=message_id)
    payload["update_id"] = 41
    args = support.capture_text_arguments(workspace, payload)
    args.pop("kind")
    args.update({
        "authenticated_actor_id": "111",
        "telegram_account_id": account,
        "telegram_conversation_id": "111",
        "conversation_binding_id": binding,
        "finance_ingress": {
            "channel": "telegram", "accountId": account, "updateId": 41,
            "chatId": 111, "messageId": message_id, "senderId": 111,
            "bindingId": binding, "payloadSha256": ingress_hash,
        },
    })
    return support.make_request(
        "capture_interaction", args,
        idempotency_key=support.canonical_capture_key(message_id=message_id),
    )


def test_initial_text_is_adopted_without_parser_then_worker_parses(
    workspace: support.BridgeWorkspace,
) -> None:
    request = _request(workspace, "lunch 12.50")
    first = support.run_cli(request)
    assert first.exit_code == errors.EXIT_OK, first.response
    result = first.response["result"]
    assert result["interaction_route"]["route_kind"] == "initial_intake"
    assert result["interaction_route"]["raw_text_sha256"] == hashlib.sha256(
        b"lunch 12.50"
    ).hexdigest()
    assert result["capture_job"]["status"] == "captured"
    with support.open_database(workspace) as conn:
        intake = conn.execute(
            "SELECT parser_output_id, raw_input FROM raw_intake_records WHERE public_id = ?",
            (result["intake_public_id"],),
        ).fetchone()
        assert intake[0] is None and intake[1] == "lunch 12.50"
    replay = support.run_cli(request)
    assert replay.exit_code == errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"]["interaction_route"] == result["interaction_route"]

    processed = support.run_cli(support.make_request(
        "process_capture_job",
        {"workspace_path": str(workspace.workspace_path),
         "job_public_id": result["capture_job"]["public_id"]},
        idempotency_key="fcp_" + identity.canonical_digest(
            "finance-process-capture-job-v1", result["capture_job"]["public_id"]
        ),
    ))
    assert processed.exit_code == errors.EXIT_OK, processed.response
    assert processed.response["result"]["capture_job"]["proposal_public_id"] is not None


@pytest.mark.parametrize(
    ("text", "route_kind", "refusal"),
    [
        ("完成", "control_refused", "no_guided_session"),
        ("Amount=12", "control_refused", "no_guided_session"),
        ("Card Ref: bad\nAmount: 12", "control_refused", "invalid_whole_card"),
        ("Card Ref: d1card_" + "a" * 32 + "\nAmount: 12", "whole_card", None),
    ],
)
def test_control_text_never_enters_parser(
    workspace: support.BridgeWorkspace, text: str, route_kind: str, refusal: str | None,
) -> None:
    captured = support.run_cli(_request(workspace, text))
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == route_kind
    assert route["refusal_code"] == refusal
    if refusal is not None:
        assert captured.response["result"]["capture_job"]["status"] == "needs_attention"
    job_id = captured.response["result"]["capture_job"]["public_id"]
    blocked = support.run_cli(support.make_request(
        "process_capture_job",
        {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
        idempotency_key="fcp_" + identity.canonical_digest(
            "finance-process-capture-job-v1", job_id
        ),
    ))
    assert blocked.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        intake_id = captured.response["result"]["intake_public_id"]
        with pytest.raises(sqlite3.IntegrityError, match="cannot enter parser"):
            conn.execute(
                "INSERT INTO parser_outputs "
                "(public_id, source_type, source_public_id, parse_status) "
                "VALUES (?, 'telegram_text', ?, 'parsed_pending_confirmation')",
                ("forbidden_proposal", intake_id),
            )


def test_replay_conflict_preserves_original_route_and_source(
    workspace: support.BridgeWorkspace,
) -> None:
    original = support.run_cli(_request(workspace, "完成"))
    assert original.exit_code == errors.EXIT_OK
    for replay in (
        _request(workspace, "lunch 12.50"),
        _request(workspace, "完成", ingress_hash="b" * 64),
    ):
        refusal = support.run_cli(replay)
        assert refusal.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        assert conn.execute(
            "SELECT raw_input FROM raw_intake_records"
        ).fetchone()[0] == "完成"
        assert conn.execute(
            "SELECT route_kind FROM finance_capture_interaction_routes"
        ).fetchone()[0] == "control_refused"
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_route_insert_failure_rolls_back_intake_job_and_source(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_route(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic route boundary failure")

    monkeypatch.setattr(commands, "freeze_interaction_route", fail_route)
    failed = support.run_cli(_request(workspace, "lunch 12.50"))
    assert failed.exit_code == errors.EXIT_INTERNAL
    with support.open_database(workspace) as conn:
        for table in (
            "raw_intake_records", "d2_telegram_source_contexts",
            "finance_capture_jobs", "finance_capture_interaction_routes",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_route_table_refuses_update_delete_and_replace(
    workspace: support.BridgeWorkspace,
) -> None:
    assert support.run_cli(_request(workspace, "完成")).exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        for statement in (
            "UPDATE finance_capture_interaction_routes SET route_kind = 'initial_intake'",
            "DELETE FROM finance_capture_interaction_routes",
            "INSERT OR REPLACE INTO finance_capture_interaction_routes "
            "SELECT * FROM finance_capture_interaction_routes",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)


def test_guided_route_is_frozen_and_found_by_original_operation(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    captured = support.run_cli(_request(
        workspace, "金额=13.00", message_id=21,
        account="finance-account", binding="binding-1",
    ))
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "guided_update"
    assert route["guided_session_public_id"] == session
    assert route["field_name"] == "amount"
    assert route["field_value_json"] == '"13.00"'
    assert route["operation_key"] == f"bridge-guided-edit-update:{session}:21"
    lookup = support.run_cli(support.make_request(
        "get_interaction_route",
        {"workspace_path": str(workspace.workspace_path),
         "operator_actor_id": "111", "telegram_account_id": "finance-account",
         "telegram_conversation_id": "111", "conversation_binding_id": "binding-1",
         "operation_key": route["operation_key"]},
    ))
    assert lookup.exit_code == errors.EXIT_OK
    assert lookup.response["result"]["interaction_route"] == route
    assert _apply(workspace, session, 21, "amount", "13.00").exit_code == errors.EXIT_OK
    assert _complete(workspace, session, 22).exit_code == errors.EXIT_OK
    after_completion = support.run_cli(support.make_request(
        "get_interaction_route",
        {"workspace_path": str(workspace.workspace_path),
         "operator_actor_id": "111", "telegram_account_id": "finance-account",
         "telegram_conversation_id": "111", "conversation_binding_id": "binding-1",
         "telegram_message_id": 21},
    ))
    assert after_completion.exit_code == errors.EXIT_OK
    assert after_completion.response["result"]["interaction_route"] == route
    assert support.run_cli(_request(
        workspace, "金额=13.00", message_id=21,
        account="finance-account", binding="binding-1",
    )).response["result"]["interaction_route"] == route
    with support.open_database(workspace) as conn:
        row = conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
            (captured.response["result"]["intake_public_id"],),
        ).fetchone()
        assert row[0] is None
