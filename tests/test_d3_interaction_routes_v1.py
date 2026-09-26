"""Synthetic adoption tests for frozen Telegram interaction routing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake import interaction_routes
from finance_core.intake.capture_jobs import ensure_capture_job
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.intake.telegram_text_adapter import validate_telegram_text_update
from finance_core.openclaw_staging_bridge import (
    commands,
    errors,
    guided_edit,
    human_actions,
    identity,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.human_drafts import (
    HumanDraftCommand,
    HumanDraftError,
    apply_human_draft_card,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_openclaw_staging_bridge_guided_edit_v1 import _apply, _begin, _complete
from tests.test_parser_human_drafts_v1 import _card_text, _start

PRE_055_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS[
    : next(
        index
        for index, path in enumerate(TEMP_DB_MIGRATION_PATHS)
        if path.name == "055_d3_interaction_routes.sql"
    )
]


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


def _pre_055_pending_workspace(
    tmp_path: Path, *, field_name: str = "merchant", field_value: str = "Cafe Two"
) -> tuple[support.BridgeWorkspace, str]:
    """Create an actual pre-cutover pending claim, then migrate that same DB."""
    workspace = support.create_bridge_workspace(tmp_path, migration_paths=PRE_055_MIGRATION_PATHS)
    session_id = "gedit_" + "a" * 32
    with support.open_database(workspace) as conn:
        result = process_raw_text_input(conn, "Lunch SGD 12.00")
        proposal = result["parser_output"]
        content_hash = compute_effective_proposal_content_hash(conn, {"id": proposal["id"]})
        conn.execute(
            "INSERT INTO openclaw_human_action_references "
            "(reference_public_id, reference_sha256, issuance_idempotency_key, "
            "parser_output_id, action, proposal_version, proposal_content_hash, "
            "authenticated_actor_id, channel, channel_account_id, "
            "channel_conversation_id, conversation_binding_id, ttl_seconds, "
            "expires_at, issued_at) VALUES (?, ?, ?, ?, 'edit', 0, ?, "
            "'111', 'telegram', 'finance-account', '111', 'binding-1', "
            "600, 4102444800, '2026-09-26')",
            (
                "haref_" + "a" * 32,
                "b" * 64,
                "bridge-human-action-issue:" + "c" * 32,
                proposal["id"],
                content_hash,
            ),
        )
        reference_id = conn.execute("SELECT id FROM openclaw_human_action_references").fetchone()[0]
        conn.execute(
            "INSERT INTO openclaw_guided_edit_sessions "
            "(session_public_id, source_reference_id, current_parser_output_id, "
            "current_proposal_version, current_content_hash, authenticated_actor_id, "
            "channel_account_id, channel_conversation_id, conversation_binding_id, "
            "status, expires_at, last_claimed_message_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, '111', 'finance-account', '111', 'binding-1', "
            "'active', 4102444800, 20, '2026-09-26', '2026-09-26')",
            (session_id, reference_id, proposal["id"], content_hash),
        )
        conn.commit()
        session = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session_id,),
            ).fetchone()
        )
        guided_edit.request_update(
            conn,
            session,
            message_id=21,
            operation_key=commands.canonical_edit_key(
                proposal_public_id=str(proposal["public_id"]),
                version=0,
                content_hash=content_hash,
            ),
            field_name=field_name,
            field_value=field_value,
        )
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finance_legacy_guided_pending_admissions"
            ).fetchone()[0]
            == 1
        )
    return workspace, session_id


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


def test_initial_route_cannot_bind_a_parser_from_another_source(
    workspace: support.BridgeWorkspace,
) -> None:
    captured = support.run_cli(_request(workspace, "lunch 12.50"))
    assert captured.exit_code == errors.EXIT_OK, captured.response
    intake_id = captured.response["result"]["intake_public_id"]
    with support.open_database(workspace) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="parser source type mismatch"):
            conn.execute(
                "INSERT INTO parser_outputs "
                "(public_id, source_type, source_public_id, parse_status) "
                "VALUES ('wrong_type_route', 'manual_entry', ?, "
                "'parsed_pending_confirmation')",
                (intake_id,),
            )
        conn.execute(
            "INSERT INTO parser_outputs "
            "(public_id, source_type, source_public_id, parse_status) "
            "VALUES ('unrelated_route', 'manual_entry', 'unrelated_source', "
            "'parsed_pending_confirmation')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="pointer source mismatch"):
            conn.execute(
                "UPDATE raw_intake_records SET parser_output_id = "
                "(SELECT id FROM parser_outputs WHERE public_id = 'unrelated_route') "
                "WHERE public_id = ?",
                (intake_id,),
            )
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
                (intake_id,),
            ).fetchone()[0]
            is None
        )


def test_unbound_parser_source_link_cannot_be_replaced(
    workspace: support.BridgeWorkspace,
) -> None:
    captured = support.run_cli(_request(workspace, "lunch 12.50"))
    assert captured.exit_code == errors.EXIT_OK, captured.response
    intake_id = captured.response["result"]["intake_public_id"]
    with support.open_database(workspace) as conn:
        conn.execute(
            "INSERT INTO parser_outputs "
            "(public_id, source_type, source_public_id, parse_status) "
            "VALUES ('unbound_telegram_parser', 'telegram_text', ?, "
            "'parsed_pending_confirmation')",
            (intake_id,),
        )
        parser_id = conn.execute(
            "SELECT id FROM parser_outputs WHERE public_id = 'unbound_telegram_parser'"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?", (intake_id,)
            ).fetchone()[0]
            is None
        )
        conn.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            conn.execute(
                "INSERT OR REPLACE INTO parser_outputs "
                "(id, public_id, source_type, source_public_id, parse_status) "
                "VALUES (?, 'replacement_parser', 'manual_entry', 'other', "
                "'parsed_pending_confirmation')",
                (parser_id,),
            )
        assert (
            conn.execute(
                "SELECT source_public_id FROM parser_outputs WHERE id = ?", (parser_id,)
            ).fetchone()[0]
            == intake_id
        )


@pytest.mark.parametrize("replacement", ["insert", "update"])
@pytest.mark.parametrize("collision_key", ["id", "rowid", "public_id"])
def test_parser_conflict_replace_cannot_change_bound_telegram_source(
    workspace: support.BridgeWorkspace, replacement: str, collision_key: str
) -> None:
    captured = support.run_cli(_request(workspace, "lunch 12.50"))
    assert captured.exit_code == errors.EXIT_OK, captured.response
    job_id = captured.response["result"]["capture_job"]["public_id"]
    processed = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert processed.exit_code == errors.EXIT_OK, processed.response
    with support.open_database(workspace) as conn:
        conn.execute("PRAGMA recursive_triggers = OFF")
        source = conn.execute(
            "SELECT public_id, parser_output_id FROM raw_intake_records WHERE public_id = ?",
            (captured.response["result"]["intake_public_id"],),
        ).fetchone()
        assert source["parser_output_id"] is not None
        parser_id = int(source["parser_output_id"])
        original = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?", (parser_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO parser_outputs "
            "(public_id, source_type, source_public_id, parse_status) "
            "VALUES ('unrelated_manual_parser', 'manual_entry', 'other', "
            "'parsed_pending_confirmation')"
        )
        expected_error = (
            "FOREIGN KEY constraint failed"
            if replacement == "insert" and collision_key in {"id", "rowid"}
            else "Telegram text parser identity collision"
        )
        with pytest.raises(sqlite3.IntegrityError, match=expected_error):
            if replacement == "insert":
                if collision_key in {"id", "rowid"}:
                    conn.execute(
                        "INSERT OR REPLACE INTO parser_outputs "
                        f"({collision_key}, public_id, source_type, "
                        "source_public_id, parse_status) "
                        "VALUES (?, 'replacement_parser', 'manual_entry', 'other', "
                        "'parsed_pending_confirmation')",
                        (parser_id,),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO parser_outputs "
                        "(public_id, source_type, source_public_id, parse_status) "
                        "VALUES (?, 'manual_entry', 'other', 'parsed_pending_confirmation')",
                        (original["public_id"],),
                    )
            else:
                conn.execute(
                    f"UPDATE OR REPLACE parser_outputs SET {collision_key} = ? "
                    "WHERE public_id = 'unrelated_manual_parser'",
                    (parser_id if collision_key != "public_id" else original["public_id"],),
                )
        bound = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (parser_id,)).fetchone()
        assert bound["public_id"] == original["public_id"]
        assert bound["source_type"] == "telegram_text"
        assert bound["source_public_id"] == source["public_id"]
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
                (source["public_id"],),
            ).fetchone()[0]
            == parser_id
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


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
        ("description=d1card_" + "a" * 32, "control_refused", "ambiguous_control"),
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
    tmp_path: Path,
) -> None:
    workspace, session = _pre_055_pending_workspace(tmp_path)
    request = _request(
        workspace,
        "merchant=Wrong Cafe",
        message_id=21,
        account="finance-account",
        binding="binding-1",
    )
    captured = support.run_cli(request)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "control_refused"
    assert route["refusal_code"] == "historical_guided_mismatch"
    assert support.run_cli(request).response["result"]["interaction_route"] == route


def test_historical_guided_exact_applied_message_recovers_original_route(
    tmp_path: Path,
) -> None:
    workspace, session = _pre_055_pending_workspace(tmp_path)
    request = _request(
        workspace,
        "merchant=Cafe Two",
        message_id=21,
        account="finance-account",
        binding="binding-1",
    )
    captured = support.run_cli(request)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "guided_update"
    assert route["guided_session_public_id"] == session
    assert route["operation_key"] == f"bridge-guided-edit-update:{session}:21"
    assert route["field_name"] == "merchant"
    assert route["field_value_json"] == '"Cafe Two"'
    applied = _apply(workspace, session, 21, "merchant", "Cafe Two")
    assert applied.exit_code == errors.EXIT_OK, applied.response
    assert applied.response["result"]["final_transaction_created"] is False
    replay = _apply(workspace, session, 21, "merchant", "Cafe Two")
    assert replay.exit_code == errors.EXIT_OK, replay.response
    with support.open_database(workspace) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM openclaw_guided_edit_events "
                "WHERE event_type = 'update_applied'"
            ).fetchone()[0]
            == 1
        )


def test_historical_guided_card_marker_replay_recovers_original_update(tmp_path: Path) -> None:
    value = "d1card_" + "a" * 32
    workspace, session = _pre_055_pending_workspace(
        tmp_path, field_name="description", field_value=value
    )
    captured = support.run_cli(
        _request(
            workspace,
            f"description={value}",
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    assert captured.response["result"]["interaction_route"]["route_kind"] == "guided_update"
    applied = _apply(workspace, session, 21, "description", value)
    assert applied.exit_code == errors.EXIT_OK, applied.response
    assert _apply(workspace, session, 21, "description", value).exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM openclaw_guided_edit_events "
                "WHERE event_type = 'update_applied'"
            ).fetchone()[0]
            == 1
        )
        assert support.count_final_facts(conn)["transactions"] == 0


@pytest.mark.parametrize(
    ("persisted_value", "replayed_text"),
    [
        ("A=B", "description=A=B"),
        ("Cafe", "description=Cafe\u00a0"),
        ("A＝B", "description=A＝B"),
        ("界" * 600, "description=" + "界" * 600),
        ("  spaced  ", "description=  spaced  "),
    ],
)
def test_historical_guided_replays_original_legacy_value(
    tmp_path: Path, persisted_value: str, replayed_text: str
) -> None:
    workspace, session = _pre_055_pending_workspace(
        tmp_path, field_name="description", field_value=persisted_value
    )
    captured = support.run_cli(
        _request(
            workspace,
            replayed_text,
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "guided_update"
    assert route["guided_session_public_id"] == session
    assert route["field_name"] == "description"
    assert json.loads(route["field_value_json"]) == persisted_value
    applied = _apply(workspace, session, 21, "description", persisted_value)
    assert applied.exit_code == errors.EXIT_OK, applied.response
    assert applied.response["result"]["final_transaction_created"] is False
    with support.open_database(workspace) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM openclaw_guided_edit_events "
                "WHERE event_type = 'update_applied'"
            ).fetchone()[0]
            == 1
        )
        assert support.count_final_facts(conn)["transactions"] == 0


def test_historical_guided_changed_legacy_value_is_refused(tmp_path: Path) -> None:
    workspace, _session = _pre_055_pending_workspace(
        tmp_path, field_name="description", field_value="A=B"
    )
    captured = support.run_cli(
        _request(
            workspace,
            "description=A=C",
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "control_refused"
    assert route["refusal_code"] == "historical_guided_mismatch"
    assert route["guided_session_public_id"] == "gedit_" + "a" * 32


def test_historical_guided_card_marker_changed_value_is_refused(tmp_path: Path) -> None:
    value = "d1card_" + "a" * 32
    workspace, _session = _pre_055_pending_workspace(
        tmp_path, field_name="description", field_value=value
    )
    changed = support.run_cli(
        _request(
            workspace,
            "description=d1card_" + "b" * 32,
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert changed.exit_code == errors.EXIT_OK, changed.response
    route = changed.response["result"]["interaction_route"]
    assert route["route_kind"] == "control_refused"
    assert route["refusal_code"] == "historical_guided_mismatch"


def test_post_cutover_guided_pending_cannot_be_forged_without_route(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal, session_id, _redemption = _begin(workspace)
    with support.open_database(workspace) as conn:
        session = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session_id,),
            ).fetchone()
        )
        operation_key = commands.canonical_edit_key(
            proposal_public_id=proposal,
            version=int(session["current_proposal_version"]),
            content_hash=str(session["current_content_hash"]),
        )
        with pytest.raises(guided_edit.GuidedEditError, match="interaction_route_required"):
            guided_edit.request_update(
                conn,
                session,
                message_id=21,
                operation_key=operation_key,
                field_name="amount",
                field_value="13.00",
            )
        with pytest.raises(sqlite3.IntegrityError, match="requires a frozen route"):
            conn.execute(
                "UPDATE openclaw_guided_edit_sessions SET pending_message_id = 21, "
                "pending_operation_key = ?, pending_field_name = 'amount', "
                "pending_field_value_json = '\"13.00\"', last_claimed_message_id = 21 "
                "WHERE session_public_id = ?",
                (operation_key, session_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="event requires a frozen route"):
            conn.execute(
                "INSERT INTO openclaw_guided_edit_events "
                "(event_public_id, session_id, sequence_number, event_type, "
                "telegram_message_id, operation_key, field_name, field_value_json, "
                "created_at) VALUES (?, ?, 2, 'update_requested', 21, ?, 'amount', "
                "'\"13.00\"', '2026-09-26')",
                ("geditev_" + "e" * 32, session["id"], operation_key),
            )
        conn.rollback()
        context = human_actions.HumanActionContext(
            actor_id="111",
            account_id="finance-account",
            conversation_id="111",
            binding_id="binding-1",
        )
        with pytest.raises(guided_edit.GuidedEditError, match="interaction_route_required"):
            guided_edit.complete_session(conn, session, context=context, message_id=21)
        with pytest.raises(sqlite3.IntegrityError, match="completion requires a frozen route"):
            conn.execute(
                "UPDATE openclaw_guided_edit_sessions SET status = 'completed', "
                "completed_message_id = 21, last_claimed_message_id = 21 "
                "WHERE session_public_id = ?",
                (session_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="event requires a frozen route"):
            conn.execute(
                "INSERT INTO openclaw_guided_edit_events "
                "(event_public_id, session_id, sequence_number, event_type, "
                "telegram_message_id, created_at) "
                "VALUES (?, ?, 2, 'completed', 21, '2026-09-26')",
                ("geditev_" + "f" * 32, session["id"]),
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finance_legacy_guided_pending_admissions"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT pending_message_id FROM openclaw_guided_edit_sessions "
                "WHERE session_public_id = ?",
                (session_id,),
            ).fetchone()[0]
            is None
        )
        assert support.count_final_facts(conn)["transactions"] == 0


def test_post_cutover_d1_direct_api_cannot_use_old_lineage_for_new_reply(
    temp_db_connection: sqlite3.Connection,
) -> None:
    conn = temp_db_connection
    apply_migration_paths(conn, PRE_055_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    command = HumanDraftCommand(
        card_generation_public_id=started.card_generation_public_id,
        telegram_message_id=101,
        operation_public_id="d1op_unrouted_101",
        authenticated_actor_id="111",
        telegram_account_id="acct",
        telegram_conversation_id="111",
        conversation_binding_id="binding",
        raw_card_text=text,
        field_values=fields,
    )
    before = conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
    for validator in (None, lambda _conn: None):
        published = False

        def publisher(*_args: object) -> None:
            nonlocal published
            published = True

        with pytest.raises(HumanDraftError, match="interaction_route_required"):
            apply_human_draft_card(conn, command, publish=publisher, authority_validator=validator)
        assert not published
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == before
    )
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    draft_id = conn.execute("SELECT id FROM parser_human_drafts").fetchone()[0]
    for verb in ("INSERT", "INSERT OR REPLACE"):
        with pytest.raises(sqlite3.IntegrityError, match="requires frozen route"):
            conn.execute(
                f"{verb} INTO parser_human_draft_reply_evidence "
                "(evidence_public_id, draft_id, raw_utf8, encoding, format_version, "
                "byte_length, sha256, authenticated_actor_id, telegram_account_id, "
                "telegram_conversation_id, conversation_binding_id, telegram_message_id, "
                "received_at) VALUES (?, ?, ?, 'UTF-8', 'd1-human-reply-v1', ?, ?, "
                "'111', 'acct', '111', 'binding', 101, 1001)",
                (
                    "d1evidence_" + "e" * 32,
                    draft_id,
                    text.encode(),
                    len(text.encode()),
                    hashlib.sha256(text.encode()).hexdigest(),
                ),
            )


def test_pre_cutover_d1_operation_replays_exactly_after_upgrade(
    temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts

    conn = temp_db_connection
    apply_migration_paths(conn, PRE_055_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    command = HumanDraftCommand(
        card_generation_public_id=started.card_generation_public_id,
        telegram_message_id=101,
        operation_public_id="d1op_historical_101",
        authenticated_actor_id="111",
        telegram_account_id="acct",
        telegram_conversation_id="111",
        conversation_binding_id="binding",
        raw_card_text=text,
        field_values=fields,
    )

    def validate(payload: dict[str, object], supplied: dict[str, str], **_kwargs: object) -> object:
        return SimpleNamespace(
            canonical_payload={**payload, **supplied},
            changed_fields=("merchant",),
            completeness="incomplete",
            reason_contributors=(),
            unresolved_flags=("missing_source",),
            explicit_clears={},
        )

    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", validate)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    first = apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert first.operation_outcome == "accepted"
    before = conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    replay = apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert replay.idempotent_replay
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == before
    )
    with pytest.raises(HumanDraftError, match="operation_conflict"):
        apply_human_draft_card(
            conn,
            HumanDraftCommand(**{**command.__dict__, "raw_card_text": text + " "}),
            publish=lambda *_: None,
        )


def test_pre_cutover_pending_guided_d1_recovers_without_new_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts

    workspace = support.create_bridge_workspace(tmp_path, migration_paths=PRE_055_MIGRATION_PATHS)
    monkeypatch.setattr(guided_edit, "_now_epoch", lambda: 1001)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    session_id = "gedit_" + "b" * 32
    with support.open_database(workspace) as conn:
        _start(
            conn,
            source_type="telegram_text",
            payload={
                "intent": "personal_expense",
                "amount": "12.50",
                "currency": "SGD",
                "transaction_date": "2026-09-13",
                "merchant": "Kopitiam",
                "description": "Lunch",
                "category": "food",
            },
        )
        conn.execute(
            "UPDATE raw_intake_records SET source_channel = 'telegram' "
            "WHERE public_id = 'intake_d1'"
        )
        reference = conn.execute(
            "SELECT id, parser_output_id, proposal_content_hash "
            "FROM openclaw_human_action_references WHERE action = 'edit'"
        ).fetchone()
        conn.execute(
            "INSERT INTO openclaw_guided_edit_sessions "
            "(session_public_id, source_reference_id, current_parser_output_id, "
            "current_proposal_version, current_content_hash, authenticated_actor_id, "
            "channel_account_id, channel_conversation_id, conversation_binding_id, "
            "status, expires_at, last_claimed_message_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, '111', 'acct', '111', 'binding', "
            "'active', 2000, 100, '1970-01-01', '1970-01-01')",
            (
                session_id,
                reference["id"],
                reference["parser_output_id"],
                reference["proposal_content_hash"],
            ),
        )
        conn.commit()
        session = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session_id,),
            ).fetchone()
        )
        guided_edit.request_update(
            conn,
            session,
            message_id=101,
            operation_key=commands.canonical_edit_key(
                proposal_public_id="prop_d1_source",
                version=0,
                content_hash=str(reference["proposal_content_hash"]),
            ),
            field_name="merchant",
            field_value="Cafe Two",
        )
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM finance_legacy_guided_pending_admissions"
            ).fetchone()[0]
            == 1
        )
    request = support.make_request(
        "apply_guided_edit_update",
        {
            "workspace_path": str(workspace.workspace_path),
            "operator_actor_id": "111",
            "telegram_account_id": "acct",
            "telegram_conversation_id": "111",
            "conversation_binding_id": "binding",
            "session_public_id": session_id,
            "telegram_message_id": 101,
            "field_name": "merchant",
            "field_value": "Cafe Two",
        },
        idempotency_key=f"bridge-guided-edit-update:{session_id}:101",
    )
    recovered = support.run_cli(request)
    assert recovered.exit_code == errors.EXIT_OK, recovered.response
    assert support.run_cli(request).exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM finance_capture_interaction_routes").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parser_human_draft_operations "
                "WHERE operation_type IN ('accepted', 'noop', 'refused')"
            ).fetchone()[0]
            == 1
        )


def test_guided_route_rejects_noncanonical_execution_key(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, session_id, _redemption = _begin(workspace)
    captured = support.run_cli(
        _request(
            workspace,
            "merchant=Cafe Two",
            message_id=21,
            account="finance-account",
            binding="binding-1",
        )
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    assert captured.response["result"]["interaction_route"]["route_kind"] == "guided_update"
    with support.open_database(workspace) as conn:
        session = dict(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
                (session_id,),
            ).fetchone()
        )
        with pytest.raises(guided_edit.GuidedEditError, match="interaction_route_required"):
            guided_edit.request_update(
                conn,
                session,
                message_id=21,
                operation_key="wrong-execution-key",
                field_name="merchant",
                field_value="Cafe Two",
            )
        with pytest.raises(sqlite3.IntegrityError, match="requires a frozen route"):
            conn.execute(
                "UPDATE openclaw_guided_edit_sessions SET pending_message_id = 21, "
                "pending_operation_key = 'wrong-execution-key', "
                "pending_field_name = 'merchant', pending_field_value_json = '\"Cafe Two\"', "
                "last_claimed_message_id = 21 WHERE session_public_id = ?",
                (session_id,),
            )


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
