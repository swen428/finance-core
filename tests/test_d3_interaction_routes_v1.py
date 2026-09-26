"""Synthetic adoption tests for frozen Telegram interaction routing."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake import interaction_routes
from finance_core.intake.capture_jobs import ensure_capture_job
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.telegram_text_adapter import validate_telegram_text_update
from finance_core.openclaw_staging_bridge import (
    commands,
    errors,
    guided_edit,
    human_actions,
    identity,
)
from tests.test_openclaw_staging_bridge_guided_edit_v1 import _apply, _begin, _complete


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _request(
    workspace: support.BridgeWorkspace,
    text: str,
    *,
    message_id: int = 10,
    ingress_hash: str = "a" * 64,
    account: str = "finance",
    binding: str = "bind-1",
) -> dict:
    payload = support.telegram_text_update(text, message_id=message_id)
    payload["update_id"] = 41
    args = support.capture_text_arguments(workspace, payload)
    args.pop("kind")
    args.update(
        {
            "authenticated_actor_id": "111",
            "telegram_account_id": account,
            "telegram_conversation_id": "111",
            "conversation_binding_id": binding,
            "finance_ingress": {
                "channel": "telegram",
                "accountId": account,
                "updateId": 41,
                "chatId": 111,
                "messageId": message_id,
                "senderId": 111,
                "bindingId": binding,
                "payloadSha256": ingress_hash,
            },
        }
    )
    return support.make_request(
        "capture_interaction",
        args,
        idempotency_key=support.canonical_capture_key(message_id=message_id),
    )


def _full_card(reference: str) -> str:
    return (
        f"Card Ref: {reference}\nAmount: 12\nCurrency: USD\nDate: 2026-09-26"
        "\nMerchant: Shop\nDescription: Lunch\nCategory: Meals"
    )


def _stage_pre_route_guided_update(workspace: support.BridgeWorkspace, session_id: str) -> None:
    """Model an update accepted before D3 began freezing interaction routes."""
    context = human_actions.HumanActionContext(
        actor_id="111",
        account_id="finance-account",
        conversation_id="111",
        binding_id="binding-1",
    )
    with support.open_database(workspace) as conn:
        session = guided_edit.get_session_by_public_id(conn, session_id, context)
        assert session is not None
        pending = guided_edit.request_update(
            conn,
            session,
            message_id=21,
            operation_key=commands.canonical_edit_key(
                proposal_public_id=str(session["proposal_public_id"]),
                version=int(session["current_proposal_version"]),
                content_hash=str(session["current_content_hash"]),
            ),
            field_name="amount",
            field_value="13.00",
        )
        commands._pending_guided_update(conn, pending)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finance_capture_interaction_routes "
                "WHERE telegram_message_id = 21"
            ).fetchone()[0]
            == 0
        )


def test_initial_text_is_adopted_without_parser_then_worker_parses(
    workspace: support.BridgeWorkspace,
) -> None:
    request = _request(workspace, "lunch 12.50")
    first = support.run_cli(request)
    assert first.exit_code == errors.EXIT_OK, first.response
    result = first.response["result"]
    assert result["interaction_route"]["route_kind"] == "initial_intake"
    assert (
        result["interaction_route"]["raw_text_sha256"] == hashlib.sha256(b"lunch 12.50").hexdigest()
    )
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

    processed = support.run_cli(
        support.make_request(
            "process_capture_job",
            {
                "workspace_path": str(workspace.workspace_path),
                "job_public_id": result["capture_job"]["public_id"],
            },
            idempotency_key="fcp_"
            + identity.canonical_digest(
                "finance-process-capture-job-v1", result["capture_job"]["public_id"]
            ),
        )
    )
    assert processed.exit_code == errors.EXIT_OK, processed.response
    assert processed.response["result"]["capture_job"]["proposal_public_id"] is not None


@pytest.mark.parametrize(
    ("text", "route_kind", "refusal"),
    [
        ("完成", "control_refused", "no_guided_session"),
        ("foo=12", "control_refused", "no_guided_session"),
        ("foo=12=3", "control_refused", "no_guided_session"),
        ("Amount=12", "control_refused", "no_guided_session"),
        ("Amount=12=3", "control_refused", "no_guided_session"),
        ("金额=", "control_refused", "no_guided_session"),
        ("金额＝12", "control_refused", "no_guided_session"),
        ("Card Ref: bad\nAmount: 12", "control_refused", "invalid_whole_card"),
        ("Amount: 12", "control_refused", "invalid_whole_card"),
        ("金额 12", "control_refused", "invalid_whole_card"),
        ("金额\u3000:12", "control_refused", "invalid_whole_card"),
        ("Card\u00a0Ref: d1card_" + "a" * 32, "control_refused", "invalid_whole_card"),
        ("Card Ref: d1card_" + "a" * 32, "control_refused", "invalid_whole_card"),
        (
            "Card Ref: d1card_" + "a" * 32 + "\nUnknown: 12",
            "control_refused",
            "invalid_whole_card",
        ),
        (
            "Card Ref: d1card_" + "a" * 32 + "\nAmount: 12\nfoo=12",
            "control_refused",
            "ambiguous_control",
        ),
        ("Card Ref d1card_" + "a" * 32, "control_refused", "invalid_whole_card"),
        (
            "note\rCard Ref: d1card_" + "a" * 32 + "\rAmount: 12",
            "control_refused",
            "invalid_whole_card",
        ),
        (
            "\rCard Ref: d1card_" + "a" * 32 + "\rAmount: 12",
            "control_refused",
            "invalid_whole_card",
        ),
        ("\u0085Card Ref: d1card_" + "a" * 32, "control_refused", "invalid_control_text"),
        ("\u2028Card Ref: d1card_" + "a" * 32, "control_refused", "invalid_control_text"),
        (
            "Card Ref: d1card_" + "a" * 32 + "\nAmount: 12",
            "control_refused",
            "invalid_whole_card",
        ),
        (_full_card("d1card_" + "a" * 32), "whole_card", None),
    ],
)
def test_control_text_never_enters_parser(
    workspace: support.BridgeWorkspace,
    text: str,
    route_kind: str,
    refusal: str | None,
) -> None:
    captured = support.run_cli(_request(workspace, text))
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == route_kind
    assert route["refusal_code"] == refusal
    if refusal is not None:
        assert captured.response["result"]["capture_job"]["status"] == "needs_attention"
    job_id = captured.response["result"]["capture_job"]["public_id"]
    blocked = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert blocked.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        intake_id = captured.response["result"]["intake_public_id"]
        with pytest.raises(sqlite3.IntegrityError, match="route before parser"):
            conn.execute(
                "INSERT INTO parser_outputs "
                "(public_id, source_type, source_public_id, parse_status) "
                "VALUES (?, 'telegram_text', ?, 'parsed_pending_confirmation')",
                ("forbidden_proposal", intake_id),
            )


@pytest.mark.parametrize("expiry_offset", [0, 1])
def test_expired_guided_session_does_not_capture_new_expense_as_control(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    expiry_offset: int,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    with support.open_database(workspace) as conn:
        expires_at = conn.execute(
            "SELECT expires_at FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session,),
        ).fetchone()[0]
    monkeypatch.setattr(interaction_routes, "_now_epoch", lambda: expires_at + expiry_offset)
    request = _request(
        workspace,
        "lunch 12.50",
        message_id=31,
        account="finance-account",
        binding="binding-1",
    )
    captured = support.run_cli(request)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    assert captured.response["result"]["interaction_route"]["route_kind"] == "initial_intake"
    assert (
        support.run_cli(request).response["result"]["interaction_route"]
        == (captured.response["result"]["interaction_route"])
    )


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ("foo=12", "invalid_guided_control"),
        ("description=a\nb", "invalid_guided_control"),
        ("description=a\tb", "invalid_guided_control"),
        ("description=a\u00a0b", "invalid_guided_control"),
        ("description=" + "a" * 1025, "invalid_guided_control"),
        ("description=a\u200bb", "invalid_control_text"),
        ("description=a\u2028b", "invalid_control_text"),
    ],
)
def test_active_guided_session_refuses_malformed_or_ambiguous_text(
    workspace: support.BridgeWorkspace, text: str, refusal: str
) -> None:
    _proposal, _session, _redemption = _begin(workspace)
    captured = support.run_cli(
        _request(workspace, text, message_id=21, account="finance-account", binding="binding-1")
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "control_refused"
    assert route["refusal_code"] == refusal
    job_id = captured.response["result"]["capture_job"]["public_id"]
    blocked = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert blocked.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        intake_id = captured.response["result"]["intake_public_id"]
        count = conn.execute(
            "SELECT COUNT(*) FROM parser_outputs WHERE source_public_id = ?", (intake_id,)
        ).fetchone()[0]
        assert count == 0


def test_active_guided_value_accepts_exact_1024_byte_limit(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    captured = support.run_cli(
        _request(
            workspace,
            "description=" + "a" * 1024,
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "guided_update"
    assert route["guided_session_public_id"] == session


def test_historical_guided_message_must_match_original_operation_content(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    _stage_pre_route_guided_update(workspace, session)
    request = _request(
        workspace, "amount=99.00", message_id=21, account="finance-account", binding="binding-1"
    )
    captured = support.run_cli(request)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "control_refused"
    assert route["refusal_code"] == "historical_guided_mismatch"
    assert support.run_cli(request).response["result"]["interaction_route"] == route


def test_historical_guided_exact_applied_message_recovers_original_route(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    _stage_pre_route_guided_update(workspace, session)
    request = _request(
        workspace, "amount=13.00", message_id=21, account="finance-account", binding="binding-1"
    )
    captured = support.run_cli(request)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "guided_update"
    assert route["guided_session_public_id"] == session
    assert route["operation_key"] == f"bridge-guided-edit-update:{session}:21"
    assert route["field_name"] == "amount"
    assert route["field_value_json"] == '"13.00"'


@pytest.mark.parametrize(
    "separator",
    [
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\u0085",
        "\u2028",
        "\u2029",
        "\u200b",
        "\u200d",
        "\ufeff",
    ],
)
def test_control_shape_rejects_ambiguous_unicode_or_line_separator(separator: str) -> None:
    shape, normalized = interaction_routes.classify_control_text_shape(
        "Card Ref: d1card_" + "a" * 32 + separator + "Amount: 12"
    )
    assert (shape, normalized) == ("invalid", None)


@pytest.mark.parametrize("media_field", ["photo", "document", "video_note"])
@pytest.mark.parametrize("normalized", [False, True])
def test_text_with_media_is_refused_before_adoption(
    workspace: support.BridgeWorkspace,
    media_field: str,
    normalized: bool,
) -> None:
    request = _request(workspace, "lunch 12.50")
    args = request["arguments"]
    if normalized:
        args["telegram_message"] = args.pop("telegram_update")["message"]
    message = args["telegram_message"] if normalized else args["telegram_update"]["message"]
    message[media_field] = []
    refusal = support.run_cli(request)
    assert refusal.exit_code == errors.EXIT_VALIDATION_REFUSED, refusal.response
    with support.open_database(workspace) as conn:
        for table in (
            "raw_intake_records",
            "d2_telegram_source_contexts",
            "finance_capture_jobs",
            "finance_capture_interaction_routes",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_interaction_requires_non_null_finance_ingress(
    workspace: support.BridgeWorkspace,
) -> None:
    request = _request(workspace, "lunch 12.50")
    request["arguments"]["finance_ingress"] = None
    refusal = support.run_cli(request)
    assert refusal.exit_code == errors.EXIT_VALIDATION_REFUSED, refusal.response
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0


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
        assert conn.execute("SELECT raw_input FROM raw_intake_records").fetchone()[0] == "完成"
        assert (
            conn.execute("SELECT route_kind FROM finance_capture_interaction_routes").fetchone()[0]
            == "control_refused"
        )
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_authenticated_job_without_route_cannot_enter_parser(
    workspace: support.BridgeWorkspace,
) -> None:
    update = support.telegram_text_update("Amount: 12", message_id=77)
    validated = validate_telegram_text_update(update)
    with support.open_database(workspace) as conn:
        conn.execute("BEGIN IMMEDIATE")
        intake = create_raw_intake_record(
            conn,
            validated.text,
            source_type="telegram_text",
            source_channel="telegram",
            source_metadata=validated.source_metadata,
        )
        ensure_capture_job(
            conn,
            intake_id=int(intake["id"]),
            capture_kind="text",
            ingress_identity_digest="a" * 64,
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM finance_capture_interaction_routes").fetchone()[0]
            == 0
        )
        with pytest.raises(sqlite3.IntegrityError, match="route before parser"):
            conn.execute(
                "INSERT INTO parser_outputs "
                "(public_id, source_type, source_public_id, parse_status) "
                "VALUES (?, 'telegram_text', ?, 'parsed_pending_confirmation')",
                ("forbidden_unrouted", intake["public_id"]),
            )


def test_route_insert_failure_rolls_back_intake_job_and_source(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_route(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic route boundary failure")

    monkeypatch.setattr(commands, "freeze_interaction_route", fail_route)
    failed = support.run_cli(_request(workspace, "lunch 12.50"))
    assert failed.exit_code == errors.EXIT_INTERNAL
    with support.open_database(workspace) as conn:
        for table in (
            "raw_intake_records",
            "d2_telegram_source_contexts",
            "finance_capture_jobs",
            "finance_capture_interaction_routes",
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


def test_duplicate_operation_cannot_replace_a_different_frozen_route(
    workspace: support.BridgeWorkspace,
) -> None:
    card = "d1card_" + "a" * 32
    first = support.run_cli(_request(workspace, _full_card(card)))
    assert first.exit_code == errors.EXIT_OK, first.response
    original = first.response["result"]["interaction_route"]
    second = _request(workspace, "lunch 13", message_id=22)
    second["command"] = "capture"
    second["arguments"]["kind"] = "text"
    legacy = support.run_cli(second)
    assert legacy.exit_code == errors.EXIT_OK, legacy.response
    other_job = legacy.response["result"]["capture_job"]["public_id"]
    with support.open_database(workspace) as conn:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="cannot replace evidence"):
            conn.execute(
                "INSERT OR REPLACE INTO finance_capture_interaction_routes "
                "(job_public_id, route_kind, raw_text_sha256, authenticated_actor_id, "
                "telegram_account_id, telegram_conversation_id, conversation_binding_id, "
                "telegram_message_id, card_generation_public_id, operation_key) "
                "VALUES (?, 'whole_card', ?, '111', 'finance', '111', 'bind-1', 22, ?, ?)",
                (
                    other_job,
                    hashlib.sha256(b"lunch 13").hexdigest(),
                    card,
                    original["operation_key"],
                ),
            )
        saved = conn.execute(
            "SELECT job_public_id, operation_key FROM finance_capture_interaction_routes"
        ).fetchall()
        assert len(saved) == 2
        assert any(
            row["job_public_id"] == original["job_public_id"]
            and row["operation_key"] == original["operation_key"]
            for row in saved
        )


@pytest.mark.parametrize("rowid_alias", ["rowid", "oid", "_rowid_"])
def test_hidden_rowid_cannot_replace_a_frozen_route(
    workspace: support.BridgeWorkspace,
    rowid_alias: str,
) -> None:
    first = support.run_cli(_request(workspace, "完成"))
    assert first.exit_code == errors.EXIT_OK, first.response
    original = first.response["result"]["interaction_route"]
    second = _request(workspace, "lunch 13", message_id=22)
    second["command"] = "capture"
    second["arguments"]["kind"] = "text"
    legacy = support.run_cli(second)
    assert legacy.exit_code == errors.EXIT_OK, legacy.response
    other_job = legacy.response["result"]["capture_job"]["public_id"]
    with support.open_database(workspace) as conn:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'finance_capture_interaction_routes'"
        ).fetchone()[0]
        assert "WITHOUT ROWID" in schema
        with pytest.raises(sqlite3.OperationalError, match="no column named"):
            conn.execute(
                "INSERT OR REPLACE INTO finance_capture_interaction_routes "
                f"({rowid_alias}, job_public_id, route_kind, raw_text_sha256, "
                "authenticated_actor_id, "
                "telegram_account_id, telegram_conversation_id, conversation_binding_id, "
                "telegram_message_id) "
                "VALUES (1, ?, 'initial_intake', ?, '111', 'finance', '111', 'bind-1', 22)",
                (other_job, hashlib.sha256(b"lunch 13").hexdigest()),
            )
        saved = conn.execute(
            "SELECT job_public_id, route_kind FROM finance_capture_interaction_routes"
        ).fetchall()
        assert len(saved) == 2
        assert any(
            row["job_public_id"] == original["job_public_id"]
            and row["route_kind"] == "control_refused"
            for row in saved
        )

    next_message = support.run_cli(_request(workspace, "lunch 14", message_id=23))
    assert next_message.exit_code == errors.EXIT_OK, next_message.response
    assert next_message.response["result"]["interaction_route"]["route_kind"] == "initial_intake"


def test_guided_route_is_frozen_and_found_by_original_operation(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    captured = support.run_cli(
        _request(
            workspace,
            "金额=13.00",
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "guided_update"
    assert route["guided_session_public_id"] == session
    assert route["field_name"] == "amount"
    assert route["field_value_json"] == '"13.00"'
    assert route["operation_key"] == f"bridge-guided-edit-update:{session}:21"
    lookup = support.run_cli(
        support.make_request(
            "get_interaction_route",
            {
                "workspace_path": str(workspace.workspace_path),
                "operator_actor_id": "111",
                "telegram_account_id": "finance-account",
                "telegram_conversation_id": "111",
                "conversation_binding_id": "binding-1",
                "operation_key": route["operation_key"],
            },
        )
    )
    assert lookup.exit_code == errors.EXIT_OK
    assert lookup.response["result"]["interaction_route"] == route
    assert _apply(workspace, session, 21, "amount", "13.00").exit_code == errors.EXIT_OK
    assert _complete(workspace, session, 22).exit_code == errors.EXIT_OK
    after_completion = support.run_cli(
        support.make_request(
            "get_interaction_route",
            {
                "workspace_path": str(workspace.workspace_path),
                "operator_actor_id": "111",
                "telegram_account_id": "finance-account",
                "telegram_conversation_id": "111",
                "conversation_binding_id": "binding-1",
                "telegram_message_id": 21,
            },
        )
    )
    assert after_completion.exit_code == errors.EXIT_OK
    assert after_completion.response["result"]["interaction_route"] == route
    assert (
        support.run_cli(
            _request(
                workspace,
                "金额=13.00",
                message_id=21,
                account="finance-account",
                binding="binding-1",
            )
        ).response["result"]["interaction_route"]
        == route
    )
    with support.open_database(workspace) as conn:
        row = conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
            (captured.response["result"]["intake_public_id"],),
        ).fetchone()
        assert row[0] is None
