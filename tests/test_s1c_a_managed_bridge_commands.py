"""S1C-A managed Bridge command proofs on disposable synthetic profiles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Iterator, cast

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.managed_staging_profile import bootstrap_registered_staging
from finance_core.openclaw_staging_bridge import envelope, errors, workspace_access
from finance_core.parser_proposals.ai_fallback import (
    claim_ai_fallback_invocation,
    prepare_ai_fallback,
    record_ai_fallback_result,
)
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import ManagedStagingProfile, ProfilePaths
from finance_core.receipt_staging_runner.workspace import generate_callback_signing_key
from tests.test_ai_fallback_service_v1 import _response_body
from tests.test_managed_staging_profile import _blank_profile

ACTOR = "111"
ACCOUNT = "acct"
CONVERSATION = "111"
BINDING = "binding"
OTHER_ACTOR = "222"
OTHER_ACCOUNT = "acct-222"
OTHER_BINDING = "binding-222"
SEEDED_MESSAGE_ID = "77"
SEEDED_PROPOSAL_ID = "prop_s1c_a_seed_text"


@dataclass
class SessionObservation:
    workspace_path: Path
    operation_id: str
    connection: sqlite3.Connection
    total_changes: int | None = None


@dataclass
class ManagedBridgeWorkspace:
    profile_base: Path
    workspace_path: Path
    witness: ManagedStagingProfile
    sessions: list[SessionObservation] = field(default_factory=list)


@pytest.fixture()
def managed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ManagedBridgeWorkspace]:
    support_root, profile_base, blank_value = _blank_profile(
        tmp_path, monkeypatch, profile_id="s1c-a-managed"
    )
    del support_root
    workspace_path = profile_base / "workspace"
    for name in ("attachments", "runtime", "evidence"):
        (workspace_path / name).mkdir(mode=0o700)
    blank = cast(ProfilePaths, blank_value)
    witness = bootstrap_registered_staging(blank)
    generate_callback_signing_key(str(workspace_path / "runtime"))

    sessions: list[SessionObservation] = []
    real_session = workspace_access.workspace_database_session

    def observe_session(
        path: Path,
        *,
        operation_id: str,
        deadline: workspace_access.SessionDeadline | None = None,
    ) -> AbstractContextManager[sqlite3.Connection]:
        session = real_session(path, operation_id=operation_id, deadline=deadline)

        class ObservedSession(AbstractContextManager[sqlite3.Connection]):
            def __init__(self) -> None:
                self.connection: sqlite3.Connection | None = None
                self.observation: SessionObservation | None = None

            def __enter__(self) -> sqlite3.Connection:
                self.connection = session.__enter__()
                self.observation = SessionObservation(path, operation_id, self.connection)
                sessions.append(self.observation)
                return self.connection

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: TracebackType | None,
            ) -> bool | None:
                if self.connection is not None and self.observation is not None:
                    self.observation.total_changes = self.connection.total_changes
                session.__exit__(exc_type, exc_value, traceback)
                return None

        return ObservedSession()

    monkeypatch.setattr(workspace_access, "workspace_database_session", observe_session)
    try:
        yield ManagedBridgeWorkspace(profile_base, workspace_path, witness, sessions)
    finally:
        witness.close()
        blank.close()


def _seed_proposal(
    workspace: ManagedBridgeWorkspace,
    *,
    source_text: str = "lunch 12.50",
    amount: str = "12.50",
    transaction_date: str | None = "2026-09-21",
) -> str:
    """Attach a synthetic proposal to a real, routed S1C-A text capture.

    The S3 parser command remains refused.  The test seeds only the proposal
    result after public capture_interaction has persisted an initial-intake
    route, then binds that result through the migration's permitted pointer.
    """
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": amount,
        "currency": "SGD",
        "transaction_date": transaction_date,
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    captured, _capture_request = _capture_route(
        workspace, source_text, message_id=int(SEEDED_MESSAGE_ID)
    )
    assert captured.exit_code == errors.EXIT_OK, captured.response
    captured_result = captured.response["result"]
    assert captured_result["interaction_route"]["route_kind"] == "initial_intake"
    intake_public_id = str(captured_result["intake_public_id"])

    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-seed-s1c-a-proposal"
    ) as conn:
        conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, parser_name, parser_version,
                raw_text, parsed_payload, parse_status
            ) VALUES (?, 'telegram_text', ?, 'test', '1', ?, ?,
                      'parsed_pending_confirmation')
            """,
            (SEEDED_PROPOSAL_ID, intake_public_id, source_text, json.dumps(payload)),
        )
        parser_output_id = int(
            conn.execute(
                "SELECT id FROM parser_outputs WHERE public_id = ?", (SEEDED_PROPOSAL_ID,)
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE raw_intake_records
            SET parser_output_id = ?, status = 'parsed_pending_confirmation'
            WHERE public_id = ?
            """,
            (parser_output_id, intake_public_id),
        )
        assert conn.execute("SELECT changes()").fetchone()[0] == 1
        conn.commit()
    return SEEDED_PROPOSAL_ID


def _seed_managed_ai_fallback_child(workspace: ManagedBridgeWorkspace) -> str:
    """Record a synthetic, provider-free AI result for a captured Telegram source."""
    _seed_proposal(
        workspace,
        source_text="paid SGD 12.34 at Cafe",
        amount="12.34",
        transaction_date=None,
    )
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-seed-s1c-a-ai-fallback"
    ) as conn:
        parent = conn.execute(
            "SELECT id, source_public_id, parsed_payload, normalized_payload "
            "FROM parser_outputs WHERE public_id = ?",
            (SEEDED_PROPOSAL_ID,),
        ).fetchone()
        assert parent is not None
        parsed_payload = json.loads(parent["parsed_payload"])
        for column in ("parsed_payload", "normalized_payload"):
            raw_payload = parent[column]
            payload = json.loads(raw_payload) if raw_payload is not None else parsed_payload.copy()
            payload.update({"description": None, "account": None, "category": None})
            conn.execute(
                f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), parent["id"]),
            )
        conn.commit()

        attempt = prepare_ai_fallback(
            conn, intake_public_id=str(parent["source_public_id"]), now_ms=100_000
        )
        claim = claim_ai_fallback_invocation(
            conn, attempt_public_id=str(attempt["attempt_public_id"]), now_ms=100_001
        )
        _synthetic_body, result_arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=str(attempt["attempt_public_id"]),
            transport_outcome="response_received",
            arguments=result_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created", (
            result["result_status"],
            result["non_child_reason"],
        )
        return str(result["proposal_public_id"])


def _request(
    workspace: ManagedBridgeWorkspace,
    command: str,
    arguments: dict[str, object],
    *,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    return support.make_request(
        command,
        {"workspace_path": str(workspace.workspace_path), **arguments},
        idempotency_key=idempotency_key,
    )


def _run(
    workspace: ManagedBridgeWorkspace,
    request: dict[str, object],
    *,
    expected_sessions: int | None = 1,
) -> support.CliOutcome:
    before = len(workspace.sessions)
    outcome = support.run_cli(request)
    opened = workspace.sessions[before:]
    if expected_sessions is not None:
        assert len(opened) == expected_sessions, (
            request["command"],
            outcome.response,
            len(opened),
        )
    for observation in opened:
        assert observation.workspace_path == workspace.workspace_path
        assert observation.operation_id.startswith("bridge:")
        with pytest.raises(sqlite3.ProgrammingError):
            observation.connection.execute("SELECT 1")
    return outcome


def _read_one(workspace: ManagedBridgeWorkspace, sql: str, parameters: tuple[object, ...] = ()):
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-inspect-s1c-a"
    ) as conn:
        return conn.execute(sql, parameters).fetchone()


def _context(
    workspace: ManagedBridgeWorkspace,
    *,
    actor_id: str = ACTOR,
    account_id: str = ACCOUNT,
    conversation_id: str | None = None,
    binding_id: str = BINDING,
) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "operator_actor_id": actor_id,
        "telegram_account_id": account_id,
        "telegram_conversation_id": actor_id if conversation_id is None else conversation_id,
        "conversation_binding_id": binding_id,
    }


def _review_request(
    workspace: ManagedBridgeWorkspace,
    proposal_id: str,
    *,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {"proposal_public_id": proposal_id}
    if context is not None:
        arguments.update(context)
    return _request(workspace, envelope.COMMAND_GET_REVIEW, arguments)


def _review(
    workspace: ManagedBridgeWorkspace,
    proposal_id: str,
    *,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    outcome = _run(
        workspace,
        _review_request(workspace, proposal_id, context=context or _context(workspace)),
    )
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    return outcome.response["result"]


def _issue_actions(
    workspace: ManagedBridgeWorkspace,
    proposal_id: str,
    review: dict[str, object],
    *,
    batch_id: str = "a" * 32,
    card_generation_id: str | None = None,
    context: dict[str, object] | None = None,
) -> tuple[support.CliOutcome, dict[str, object]]:
    arguments: dict[str, object] = {
        **(context or _context(workspace)),
        "proposal_public_id": proposal_id,
        "reference_batch_id": batch_id,
        "token_ttl_seconds": 600,
        "expected_proposal_version": review["proposal_version"],
        "expected_content_hash": review["effective_content_hash"],
    }
    if card_generation_id is not None:
        arguments["card_generation_public_id"] = card_generation_id
    request = _request(
        workspace,
        envelope.COMMAND_ISSUE_HUMAN_ACTIONS,
        arguments,
        idempotency_key=support.canonical_human_action_issuance_key(batch_id),
    )
    return _run(workspace, request), request


def _reject_managed_ai_proposal(
    workspace: ManagedBridgeWorkspace,
    proposal_id: str,
    *,
    batch_id: str,
    callback_id: str,
) -> tuple[
    dict[str, object],
    support.CliOutcome,
    support.CliOutcome,
    dict[str, object],
    support.CliOutcome,
    support.CliOutcome,
]:
    review = _review(workspace, proposal_id, context=_context(workspace))
    assert review["callback_tokens"] is None
    issued, _issue_request = _issue_actions(workspace, proposal_id, review, batch_id=batch_id)
    assert issued.exit_code == errors.EXIT_OK, issued.response
    assert set(issued.response["result"]["actions"]) == {"reject"}

    callback_arguments = {
        **_context(workspace),
        "short_reference": issued.response["result"]["actions"]["reject"]["reference"],
        "action": "reject",
        "callback_id": callback_id,
        "callback_message_id": 21,
    }
    callback_request = _request(
        workspace,
        envelope.COMMAND_REDEEM_HUMAN_ACTION,
        callback_arguments,
        idempotency_key=support.canonical_human_action_redemption_key(callback_id),
    )
    redeemed = _run(workspace, callback_request)
    assert redeemed.exit_code == errors.EXIT_OK, redeemed.response
    redeemed_result = redeemed.response["result"]

    decision_arguments = {
        **_context(workspace),
        "proposal_public_id": proposal_id,
        "operator_actor_id": redeemed_result["operator_actor_id"],
        "proposal_version": redeemed_result["proposal_version"],
        "content_hash": redeemed_result["content_hash"],
        "callback_token": redeemed_result["callback_token"],
        "callback_expiry": redeemed_result["callback_expiry"],
    }
    decision_request = _request(
        workspace,
        envelope.COMMAND_REJECT,
        decision_arguments,
        idempotency_key=str(redeemed_result["decision_idempotency_key"]),
    )
    decision = _run(workspace, decision_request)
    assert decision.exit_code == errors.EXIT_OK, decision.response
    replay = _run(workspace, decision_request)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    return review, issued, redeemed, decision_request, decision, replay


def _redeem_edit(
    workspace: ManagedBridgeWorkspace,
    issued: support.CliOutcome,
    *,
    callback_id: str = "s1c-a-edit-callback",
) -> tuple[support.CliOutcome, dict[str, object]]:
    arguments = {
        **_context(workspace),
        "short_reference": issued.response["result"]["actions"]["edit"]["reference"],
        "action": "edit",
        "callback_id": callback_id,
        "callback_message_id": 20,
    }
    request = _request(
        workspace,
        envelope.COMMAND_REDEEM_HUMAN_ACTION,
        arguments,
        idempotency_key=support.canonical_human_action_redemption_key(callback_id),
    )
    return _run(workspace, request), request


def _capture_route(
    workspace: ManagedBridgeWorkspace,
    text: str,
    *,
    message_id: int,
    actor_id: str = ACTOR,
    account_id: str = ACCOUNT,
    conversation_id: str | None = None,
    binding_id: str = BINDING,
) -> tuple[support.CliOutcome, dict[str, object]]:
    resolved_conversation_id = actor_id if conversation_id is None else conversation_id
    update = support.telegram_text_update(
        text,
        update_id=1000 + message_id,
        message_id=message_id,
        chat_id=int(resolved_conversation_id),
        sender_id=int(actor_id),
    )
    arguments = support.authenticated_text_capture_arguments(
        workspace,
        update,
        account_id=account_id,
        binding_id=binding_id,
    )
    arguments.pop("kind")
    request = _request(
        workspace,
        envelope.COMMAND_CAPTURE_INTERACTION,
        arguments,
        idempotency_key=support.canonical_capture_key(
            chat_id=int(resolved_conversation_id), message_id=message_id
        ),
    )
    return _run(workspace, request), request


def _start_guided_edit(
    workspace: ManagedBridgeWorkspace,
) -> tuple[str, support.CliOutcome, dict[str, object]]:
    proposal_id = _seed_proposal(workspace)
    review = _review(workspace, proposal_id)
    issued, _ = _issue_actions(workspace, proposal_id, review)
    assert issued.exit_code == errors.EXIT_OK, issued.response
    redeemed, redemption_request = _redeem_edit(workspace, issued)
    assert redeemed.exit_code == errors.EXIT_OK, redeemed.response
    return proposal_id, redeemed, redemption_request


def _decision_arguments(
    review: dict[str, object],
    action: str,
    *,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    callback_tokens = cast(dict[str, dict[str, object]], review["callback_tokens"])
    entry = callback_tokens[action]
    arguments: dict[str, object] = {
        "proposal_public_id": review["proposal_public_id"],
        "operator_actor_id": ACTOR,
        "proposal_version": review["proposal_version"],
        "content_hash": review["effective_content_hash"],
        "callback_token": entry["token"],
        "callback_expiry": entry["expiry"],
    }
    if context is not None:
        arguments.update({key: value for key, value in context.items() if key != "workspace_path"})
    else:
        arguments.update(
            {
                "telegram_account_id": ACCOUNT,
                "telegram_conversation_id": CONVERSATION,
                "conversation_binding_id": BINDING,
            }
        )
    return arguments


def _decision_key(action: str, review: dict[str, object]) -> str:
    if action == "edit":
        return support.canonical_edit_key(
            proposal_public_id=str(review["proposal_public_id"]),
            version=cast(int, review["proposal_version"]),
            content_hash=str(review["effective_content_hash"]),
        )
    return support.canonical_decision_key(
        action=action, proposal_public_id=str(review["proposal_public_id"])
    )


@pytest.mark.parametrize("action", ("confirm", "edit", "reject"))
def test_managed_decision_commands_replay_refuse_conflicts_and_create_no_final_fact(
    managed_workspace: ManagedBridgeWorkspace,
    action: str,
) -> None:
    proposal_id = _seed_proposal(managed_workspace)
    review = _review(managed_workspace, proposal_id)
    arguments = _decision_arguments(review, action)
    if action == "edit":
        arguments["field_updates"] = {"merchant": "Synthetic Cafe"}
    key = _decision_key(action, review)
    request = _request(managed_workspace, action, arguments, idempotency_key=key)

    first = _run(managed_workspace, request)
    replay = _run(managed_workspace, request)
    assert first.exit_code == errors.EXIT_OK, first.response
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert first.response["result"]["final_transaction_created"] is False

    if action == "edit":
        changed = dict(arguments)
        changed["field_updates"] = {"merchant": "Conflicting Cafe"}
        conflict = _run(
            managed_workspace,
            _request(managed_workspace, action, changed, idempotency_key=key),
        )
        assert conflict.exit_code == errors.EXIT_AUTHORITY_REFUSED
        assert conflict.response["error"]["code"] == errors.IDEMPOTENCY_CONFLICT
    else:
        opposite = "reject" if action == "confirm" else "confirm"
        contradictory = _run(
            managed_workspace,
            _request(
                managed_workspace,
                opposite,
                _decision_arguments(review, opposite),
                idempotency_key=_decision_key(opposite, review),
            ),
        )
        assert contradictory.exit_code == errors.EXIT_AUTHORITY_REFUSED
        assert contradictory.response["error"]["code"] == errors.LIFECYCLE_CONFLICT

    assert _read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0


@pytest.mark.parametrize("action", ("confirm", "edit", "reject"))
def test_managed_decisions_without_source_context_refuse_without_writes_or_details(
    managed_workspace: ManagedBridgeWorkspace,
    action: str,
) -> None:
    proposal_id = _seed_proposal(managed_workspace)
    review = _review(managed_workspace, proposal_id)
    arguments = _decision_arguments(review, action, context={})
    if action == "edit":
        arguments["field_updates"] = {"merchant": "Synthetic Updated Cafe"}

    refused = _assert_refused_without_writes(
        managed_workspace,
        _request(
            managed_workspace,
            action,
            arguments,
            idempotency_key=_decision_key(action, review),
        ),
        expected_exit_codes=(errors.EXIT_VALIDATION_REFUSED,),
    )

    assert refused.response["error"]["code"] == errors.ARGUMENTS_REFUSED
    response_text = json.dumps(refused.response, sort_keys=True)
    assert proposal_id not in response_text
    assert review["effective_content_hash"] not in response_text
    assert review["callback_tokens"][action]["token"] not in response_text
    assert "callback_tokens" not in response_text
    assert "confirmation_id" not in response_text


def test_managed_capture_interaction_replays_without_running_s3_proposal_processing(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    captured, request = _capture_route(
        managed_workspace,
        "synthetic interaction only",
        message_id=90,
    )
    replay = _run(managed_workspace, request)
    assert captured.exit_code == errors.EXIT_OK, captured.response
    assert captured.response["result"]["proposal_public_id"] is None
    assert captured.response["result"]["capture_job"]["proposal_public_id"] is None
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert (
        replay.response["result"]["capture_job"]["public_id"]
        == captured.response["result"]["capture_job"]["public_id"]
    )
    intake_id = captured.response["result"]["intake_public_id"]
    assert (
        _read_one(
            managed_workspace,
            "SELECT COUNT(*) FROM raw_intake_records WHERE public_id = ?",
            (intake_id,),
        )[0]
        == 1
    )
    assert _read_one(managed_workspace, "SELECT COUNT(*) FROM parser_outputs")[0] == 0


def test_managed_get_review_is_read_only_and_refuses_missing_proposal(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    proposal_id = _seed_proposal(managed_workspace)
    first = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_GET_REVIEW,
            {**_context(managed_workspace), "proposal_public_id": proposal_id},
        ),
    )
    second = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_GET_REVIEW,
            {**_context(managed_workspace), "proposal_public_id": proposal_id},
        ),
    )
    assert first.exit_code == errors.EXIT_OK, first.response
    assert second.exit_code == errors.EXIT_OK, second.response
    assert managed_workspace.sessions[-2].total_changes == 0
    assert managed_workspace.sessions[-1].total_changes == 0
    for field_name in ("proposal_public_id", "proposal_version", "effective_content_hash"):
        assert first.response["result"][field_name] == second.response["result"][field_name]

    missing = _assert_refused_without_writes(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_GET_REVIEW,
            {
                **_context(managed_workspace),
                "proposal_public_id": "prop_s1c_a_missing",
            },
        ),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert missing.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    profile = workspace_access.managed_profile_for_workspace(managed_workspace.workspace_path)
    assert profile is not None
    try:
        with exclusive_cut(profile, timeout_seconds=0):
            pass
    finally:
        profile.close()


def test_managed_review_binds_initial_and_d1_proposals_to_frozen_source_context(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    proposal_id = _create_d1_child_proposal(managed_workspace)
    _capture_other_actor(managed_workspace)

    owner_context = _context(managed_workspace)
    first = _review(managed_workspace, proposal_id, context=owner_context)
    same_context_replay = _review(managed_workspace, proposal_id, context=owner_context)
    assert first["proposal_public_id"] == proposal_id
    assert first["callback_tokens"] is not None
    assert same_context_replay["proposal_public_id"] == proposal_id
    assert same_context_replay["callback_tokens"] is not None

    lineage = _read_one(
        managed_workspace,
        """
        SELECT child.id, child.parent_parser_output_id, parent.id,
               parent.source_public_id, child.source_public_id,
               intake.public_id, intake.parser_output_id,
               source.authenticated_actor_id, source.telegram_account_id,
               source.telegram_conversation_id, source.conversation_binding_id,
               source.source_message_id, source.source_identity_sha256
        FROM parser_outputs AS child
        JOIN parser_outputs AS parent ON parent.id = child.parent_parser_output_id
        JOIN raw_intake_records AS intake ON intake.public_id = child.source_public_id
        JOIN d2_telegram_source_contexts AS source
          ON source.raw_intake_record_id = intake.id
        WHERE child.public_id = ?
        """,
        (proposal_id,),
    )
    assert lineage is not None
    assert lineage[0] == lineage[6]  # intake points to the current D1 child
    assert lineage[1] == lineage[2]  # child points to its parent proposal
    assert lineage[3] == lineage[4] == lineage[5]  # source identity is preserved
    assert lineage[7] == ACTOR
    assert lineage[8] == ACCOUNT
    assert lineage[9] == CONVERSATION
    assert lineage[10] == BINDING
    assert lineage[11] == SEEDED_MESSAGE_ID
    assert len(str(lineage[12])) == 64

    attacker_context = _context(
        managed_workspace,
        actor_id=OTHER_ACTOR,
        account_id=OTHER_ACCOUNT,
        conversation_id=OTHER_ACTOR,
        binding_id=OTHER_BINDING,
    )
    wrong_contexts: tuple[tuple[str, dict[str, object] | None], ...] = (
        ("missing", None),
        ("partial", {"operator_actor_id": ACTOR}),
        ("other-actor-and-conversation", attacker_context),
        (
            "wrong-account",
            _context(managed_workspace, account_id="another-account"),
        ),
        (
            "wrong-conversation",
            _context(managed_workspace, conversation_id=OTHER_ACTOR),
        ),
        (
            "wrong-binding",
            _context(managed_workspace, binding_id="another-binding"),
        ),
    )
    for name, supplied_context in wrong_contexts:
        refused = _assert_refused_without_writes(
            managed_workspace,
            _review_request(managed_workspace, proposal_id, context=supplied_context),
        )
        assert refused.exit_code in (
            errors.EXIT_AUTHORITY_REFUSED,
            errors.EXIT_VALIDATION_REFUSED,
        ), (
            name,
            refused.response,
        )
        refused_json = json.dumps(refused.response, sort_keys=True)
        assert proposal_id not in refused_json
        assert "callback_tokens" not in refused_json


def test_managed_source_context_refuses_orphan_proposal_without_origin_or_digest(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    proposal_id = _seed_orphan_proposal(managed_workspace)
    refused = _assert_refused_without_writes(
        managed_workspace,
        _review_request(
            managed_workspace,
            proposal_id,
            context=_context(managed_workspace),
        ),
    )
    assert refused.exit_code == errors.EXIT_AUTHORITY_REFUSED


def test_managed_review_action_and_decision_accepts_sealed_ai_fallback_child(
    managed_workspace: ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Synthetic S1C-A review and decision must not call a provider")

    monkeypatch.setattr("socket.socket.connect", refuse_network)
    monkeypatch.setattr("socket.create_connection", refuse_network)

    proposal_id = _seed_managed_ai_fallback_child(managed_workspace)
    review, issued, redeemed, _decision_request, decision, replay = _reject_managed_ai_proposal(
        managed_workspace,
        proposal_id,
        batch_id="d" * 32,
        callback_id="s1c-a-ai-fallback-reject",
    )

    assert review["proposal_public_id"] == proposal_id
    assert issued.response["result"]["proposal_public_id"] == proposal_id
    assert redeemed.response["result"]["action"] == "reject"
    assert decision.response["result"]["decision"] == "rejected"
    assert decision.response["result"]["final_transaction_created"] is False
    assert replay.response["result"] == decision.response["result"]
    assert _read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0


def test_managed_unverified_same_source_child_blocks_issue_and_decision_replay(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    proposal_id = _seed_managed_ai_fallback_child(managed_workspace)
    review, _issued, _redeemed, decision_request, _decision, _replay = _reject_managed_ai_proposal(
        managed_workspace,
        proposal_id,
        batch_id="e" * 32,
        callback_id="s1c-a-ai-fallback-replay",
    )
    forged_id = _forge_same_source_child_without_d1_publication(managed_workspace, proposal_id)

    refused_review = _assert_refused_without_writes(
        managed_workspace,
        _review_request(managed_workspace, forged_id, context=_context(managed_workspace)),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert refused_review.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    assert "callback_tokens" not in json.dumps(refused_review.response, sort_keys=True)

    issue_request = _request(
        managed_workspace,
        envelope.COMMAND_ISSUE_HUMAN_ACTIONS,
        {
            **_context(managed_workspace),
            "proposal_public_id": forged_id,
            "reference_batch_id": "f" * 32,
            "token_ttl_seconds": 600,
            "expected_proposal_version": review["proposal_version"],
            "expected_content_hash": review["effective_content_hash"],
        },
        idempotency_key=support.canonical_human_action_issuance_key("f" * 32),
    )
    refused_issue = _assert_refused_without_writes(
        managed_workspace,
        issue_request,
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert refused_issue.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    assert "actions" not in json.dumps(refused_issue.response, sort_keys=True)

    forged_decision_arguments = dict(decision_request["arguments"])
    forged_decision_arguments["proposal_public_id"] = forged_id
    forged_decision = _request(
        managed_workspace,
        envelope.COMMAND_REJECT,
        forged_decision_arguments,
        idempotency_key=support.canonical_decision_key(
            action="reject", proposal_public_id=forged_id
        ),
    )
    refused_forged_decision = _assert_refused_without_writes(
        managed_workspace,
        forged_decision,
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert refused_forged_decision.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE

    refused_replay = _assert_refused_without_writes(
        managed_workspace,
        decision_request,
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert refused_replay.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE


def test_managed_review_without_source_context_hides_proposal_presence(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    existing_proposal_id = _seed_proposal(managed_workspace)
    existing_refusal = _assert_refused_without_writes(
        managed_workspace,
        _review_request(managed_workspace, existing_proposal_id),
    )
    missing_refusal = _assert_refused_without_writes(
        managed_workspace,
        _review_request(managed_workspace, "prop_s1c_a_missing_without_context"),
    )

    existing_error = existing_refusal.response["error"]
    missing_error = missing_refusal.response["error"]
    assert existing_refusal.exit_code == missing_refusal.exit_code
    assert existing_error["code"] == missing_error["code"]
    assert existing_error["message"] == missing_error["message"]


def test_managed_review_with_foreign_context_hides_proposal_presence(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    existing_proposal_id = _seed_proposal(managed_workspace)
    owner_review = _review(managed_workspace, existing_proposal_id)
    foreign_context = _context(
        managed_workspace,
        actor_id=OTHER_ACTOR,
        account_id=OTHER_ACCOUNT,
        conversation_id=OTHER_ACTOR,
        binding_id=OTHER_BINDING,
    )
    existing_refusal = _assert_refused_without_writes(
        managed_workspace,
        _review_request(
            managed_workspace,
            existing_proposal_id,
            context=foreign_context,
        ),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    missing_proposal_id = "prop_s1c_a_missing_foreign_context"
    missing_refusal = _assert_refused_without_writes(
        managed_workspace,
        _review_request(
            managed_workspace,
            missing_proposal_id,
            context=foreign_context,
        ),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )

    existing_error = existing_refusal.response["error"]
    missing_error = missing_refusal.response["error"]
    assert existing_refusal.exit_code == missing_refusal.exit_code
    assert existing_error["code"] == missing_error["code"] == errors.PROPOSAL_UNAVAILABLE
    assert existing_error["message"] == missing_error["message"]
    for proposal_id, refusal in (
        (existing_proposal_id, existing_refusal),
        (missing_proposal_id, missing_refusal),
    ):
        response_text = json.dumps(refusal.response, sort_keys=True)
        assert proposal_id not in response_text
        assert owner_review["effective_content_hash"] not in response_text
        assert owner_review["callback_tokens"]["confirm"]["token"] not in response_text
        assert "callback_tokens" not in response_text
        assert "actions" not in response_text


def test_ordinary_get_review_preserves_legacy_request_without_source_context(
    tmp_path: Path,
) -> None:
    ordinary = support.create_bridge_workspace(tmp_path, name="s1c-a-ordinary-review")
    proposal_id = "prop_s1c_a_ordinary_compat"
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-21",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    with workspace_access.workspace_database_session(
        ordinary.workspace_path, operation_id="test-seed-ordinary-review"
    ) as conn:
        conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, parser_name, parser_version,
                raw_text, parsed_payload, parse_status
            ) VALUES (?, 'text', 'ordinary-synthetic-intake', 'test', '1', 'lunch 12.50', ?,
                      'parsed_pending_confirmation')
            """,
            (proposal_id, json.dumps(payload)),
        )
        conn.commit()

    outcome = support.run_cli(
        support.make_request(
            envelope.COMMAND_GET_REVIEW,
            {
                "workspace_path": str(ordinary.workspace_path),
                "proposal_public_id": proposal_id,
            },
        )
    )
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    assert outcome.response["result"]["proposal_public_id"] == proposal_id
    assert outcome.response["result"]["callback_tokens"] is not None


@pytest.mark.parametrize("action", ("confirm", "edit", "reject"))
def test_managed_decisions_and_reference_issuance_cannot_cross_source_context(
    managed_workspace: ManagedBridgeWorkspace,
    action: str,
) -> None:
    proposal_id = _seed_proposal(managed_workspace)
    owner_context = _context(managed_workspace)
    review = _review(managed_workspace, proposal_id, context=owner_context)
    _capture_other_actor(managed_workspace)
    attacker_context = _context(
        managed_workspace,
        actor_id=OTHER_ACTOR,
        account_id=OTHER_ACCOUNT,
        conversation_id=OTHER_ACTOR,
        binding_id=OTHER_BINDING,
    )

    before_issue = _authority_snapshot(managed_workspace)
    issue_request = _request(
        managed_workspace,
        envelope.COMMAND_ISSUE_HUMAN_ACTIONS,
        {
            **attacker_context,
            "proposal_public_id": proposal_id,
            "reference_batch_id": "b" * 32,
            "token_ttl_seconds": 600,
            "expected_proposal_version": review["proposal_version"],
            "expected_content_hash": review["effective_content_hash"],
        },
        idempotency_key=support.canonical_human_action_issuance_key("b" * 32),
    )
    issue_refused = _assert_refused_without_writes(
        managed_workspace,
        issue_request,
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert "actions" not in issue_refused.response
    assert _authority_snapshot(managed_workspace) == before_issue

    decision_arguments = _decision_arguments(review, action, context=attacker_context)
    if action == "edit":
        decision_arguments["field_updates"] = {"merchant": "Attacker Cafe"}
    decision_refused = _assert_refused_without_writes(
        managed_workspace,
        _request(
            managed_workspace,
            action,
            decision_arguments,
            idempotency_key=_decision_key(action, review),
        ),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    assert "confirmation_id" not in decision_refused.response
    assert (
        _read_one(
            managed_workspace,
            "SELECT COUNT(*) FROM parser_proposal_confirmations WHERE parser_output_id = "
            "(SELECT id FROM parser_outputs WHERE public_id = ?)",
            (proposal_id,),
        )[0]
        == 0
    )
    assert (
        _read_one(
            managed_workspace,
            "SELECT COUNT(*) FROM transactions",
        )[0]
        == 0
    )


def test_managed_human_action_issue_and_redemption_are_context_bound_and_replayable(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    proposal_id = _seed_proposal(managed_workspace)
    review = _review(managed_workspace, proposal_id)
    issued, issue_request = _issue_actions(managed_workspace, proposal_id, review)
    issue_replay = _run(managed_workspace, issue_request)
    assert issued.exit_code == errors.EXIT_OK, issued.response
    assert issue_replay.exit_code == errors.EXIT_OK, issue_replay.response
    assert issue_replay.response["idempotent_replay"] is True
    assert issue_replay.response["result"]["actions"] == issued.response["result"]["actions"]

    issued_edit = issued.response["result"]["actions"]["edit"]["reference"]
    wrong_context = {
        **_context(managed_workspace),
        "short_reference": issued_edit,
        "action": "edit",
        "callback_id": "wrong-binding-attempt",
        "callback_message_id": 21,
        "conversation_binding_id": "other-binding",
    }
    refused = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_REDEEM_HUMAN_ACTION,
            wrong_context,
            idempotency_key=support.canonical_human_action_redemption_key("wrong-binding-attempt"),
        ),
    )
    assert refused.exit_code == errors.EXIT_AUTHORITY_REFUSED

    redeemed, redemption_request = _redeem_edit(managed_workspace, issued)
    replay = _run(managed_workspace, redemption_request)
    assert redeemed.exit_code == errors.EXIT_OK, redeemed.response
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert (
        replay.response["result"]["guided_edit_session_public_id"]
        == (redeemed.response["result"]["guided_edit_session_public_id"])
    )
    second_callback = {
        **_context(managed_workspace),
        "short_reference": issued_edit,
        "action": "edit",
        "callback_id": "different-callback-attempt",
        "callback_message_id": 22,
    }
    replayed_reference = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_REDEEM_HUMAN_ACTION,
            second_callback,
            idempotency_key=support.canonical_human_action_redemption_key(
                "different-callback-attempt"
            ),
        ),
    )
    assert replayed_reference.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert (
        _read_one(managed_workspace, "SELECT COUNT(*) FROM openclaw_human_action_redemptions")[0]
        == 1
    )


def test_managed_guided_edit_update_and_completion_replay_on_public_routes(
    managed_workspace: ManagedBridgeWorkspace,
) -> None:
    _proposal_id, redeemed, _redemption_request = _start_guided_edit(managed_workspace)
    session_id = redeemed.response["result"]["guided_edit_session_public_id"]

    routed, _ = _capture_route(managed_workspace, "merchant=Synthetic Cafe", message_id=90)
    assert routed.exit_code == errors.EXIT_OK, routed.response
    update_arguments = {
        **_context(managed_workspace),
        "session_public_id": session_id,
        "telegram_message_id": 90,
        "field_name": "merchant",
        "field_value": "Synthetic Cafe",
    }
    update_key = f"bridge-guided-edit-update:{session_id}:90"
    update_request = _request(
        managed_workspace,
        envelope.COMMAND_APPLY_GUIDED_EDIT_UPDATE,
        update_arguments,
        idempotency_key=update_key,
    )
    first = _run(managed_workspace, update_request)
    replay = _run(managed_workspace, update_request)
    assert first.exit_code == errors.EXIT_OK, first.response
    assert first.response["result"]["final_transaction_created"] is False
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    conflict_arguments = {**update_arguments, "field_value": "Other Cafe"}
    conflict = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_APPLY_GUIDED_EDIT_UPDATE,
            conflict_arguments,
            idempotency_key=update_key,
        ),
    )
    assert conflict.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert conflict.response["error"]["code"] == errors.IDEMPOTENCY_CONFLICT

    completed_route, _ = _capture_route(managed_workspace, "完成", message_id=91)
    assert completed_route.exit_code == errors.EXIT_OK, completed_route.response
    complete_request = _request(
        managed_workspace,
        envelope.COMMAND_COMPLETE_GUIDED_EDIT,
        {
            **_context(managed_workspace),
            "session_public_id": session_id,
            "telegram_message_id": 91,
        },
        idempotency_key=f"bridge-guided-edit-complete:{session_id}:91",
    )
    complete = _run(managed_workspace, complete_request)
    complete_replay = _run(managed_workspace, complete_request)
    assert complete.exit_code == errors.EXIT_OK, complete.response
    assert complete.response["result"]["active"] is False
    assert complete_replay.exit_code == errors.EXIT_OK, complete_replay.response
    assert complete_replay.response["idempotent_replay"] is True
    assert _read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0


def _framed_hash(domain: str, *fields: str) -> str:
    payload = domain.encode("ascii") + b"\x00" + len(fields).to_bytes(4, "big")
    for item in fields:
        encoded = item.encode("utf-8")
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def _whole_card_text(card_id: str) -> str:
    return "\n".join(
        (
            f"Card Ref: {card_id}",
            "Amount: 12.50",
            "Currency: SGD",
            "Date: 2026-09-19",
            "Merchant: Synthetic Cafe",
            "Description: Lunch",
            "Category: Food",
        )
    )


def _create_d1_child_proposal(workspace: ManagedBridgeWorkspace) -> str:
    _parent_id, redeemed, _ = _start_guided_edit(workspace)
    card = redeemed.response["result"]["human_draft_card"]
    card_id = str(card["card_generation_public_id"])
    raw_text = _whole_card_text(card_id)
    route, _ = _capture_route(workspace, raw_text, message_id=90)
    assert route.exit_code == errors.EXIT_OK, route.response
    operation_id = str(route.response["result"]["interaction_route"]["operation_key"])
    applied = _run(
        workspace,
        _request(
            workspace,
            envelope.COMMAND_APPLY_HUMAN_DRAFT_CARD,
            {
                **_context(workspace),
                "card_generation_public_id": card_id,
                "telegram_message_id": 90,
                "operation_public_id": operation_id,
                "raw_card_text": raw_text,
                "field_values": {
                    "amount": "12.50",
                    "currency": "SGD",
                    "transaction_date": "2026-09-19",
                    "merchant": "Synthetic Cafe",
                    "description": "Lunch",
                    "category": "Food",
                },
            },
            idempotency_key=f"bridge-human-draft-apply:{operation_id}",
        ),
    )
    assert applied.exit_code == errors.EXIT_OK, applied.response
    child_id = str(applied.response["result"]["proposal_public_id"])
    assert child_id.startswith("po_d1_")
    return child_id


def _capture_other_actor(workspace: ManagedBridgeWorkspace) -> None:
    outcome, _ = _capture_route(
        workspace,
        "separate synthetic source for actor 222",
        message_id=78,
        actor_id=OTHER_ACTOR,
        account_id=OTHER_ACCOUNT,
        conversation_id=OTHER_ACTOR,
        binding_id=OTHER_BINDING,
    )
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    assert outcome.response["result"]["proposal_public_id"] is None


_AUTHORITY_SNAPSHOT_TABLES = (
    "raw_intake_records",
    "parser_outputs",
    "d2_telegram_source_contexts",
    "parser_proposal_events",
    "parser_proposal_confirmations",
    "parser_proposal_completions",
    "parser_human_draft_operations",
    "parser_human_draft_publications",
    "openclaw_human_action_references",
    "openclaw_human_action_redemptions",
    "financial_audit_events",
    "transactions",
)


def _authority_snapshot(workspace: ManagedBridgeWorkspace) -> tuple[tuple[str, int], ...]:
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-authority-snapshot"
    ) as conn:
        return tuple(
            (table, int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
            for table in _AUTHORITY_SNAPSHOT_TABLES
        )


def _assert_refused_without_writes(
    workspace: ManagedBridgeWorkspace,
    request: dict[str, object],
    *,
    expected_exit_codes: tuple[int, ...] = (
        errors.EXIT_AUTHORITY_REFUSED,
        errors.EXIT_VALIDATION_REFUSED,
    ),
) -> support.CliOutcome:
    before_counts = _authority_snapshot(workspace)
    before_sessions = len(workspace.sessions)
    outcome = _run(workspace, request, expected_sessions=None)
    assert outcome.exit_code in expected_exit_codes, outcome.response
    assert "result" not in outcome.response
    for observation in workspace.sessions[before_sessions:]:
        assert observation.total_changes == 0
    assert _authority_snapshot(workspace) == before_counts
    return outcome


def _seed_orphan_proposal(workspace: ManagedBridgeWorkspace) -> str:
    proposal_id = "prop_s1c_a_without_source_lineage"
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-21",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-seed-orphan-proposal"
    ) as conn:
        conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, parser_name, parser_version,
                raw_text, parsed_payload, parse_status
            ) VALUES (?, 'text', 'missing-synthetic-source', 'test', '1', 'lunch 12.50', ?,
                      'parsed_pending_confirmation')
            """,
            (proposal_id, json.dumps(payload)),
        )
        conn.commit()
    return proposal_id


def _forge_same_source_child_without_d1_publication(
    workspace: ManagedBridgeWorkspace,
    parent_public_id: str,
) -> str:
    """Model an invalid D1 pointer edge after bypassing its synthetic DB guard."""
    trigger_name = "trg_ai_fallback_raw_intake_no_lineage_escape"
    child_public_id = "prop_s1c_a_forged_same_source_child"
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-forge-s1c-a-unverified-child"
    ) as conn:
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        assert trigger is not None and trigger["sql"] is not None
        parent = conn.execute(
            "SELECT id, source_type, source_public_id, raw_text, parsed_payload, "
            "normalized_payload FROM parser_outputs WHERE public_id = ?",
            (parent_public_id,),
        ).fetchone()
        assert parent is not None

        conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, parser_name, parser_version,
                raw_text, parsed_payload, normalized_payload, parse_status,
                parent_parser_output_id
            ) VALUES (?, ?, ?, 'test-forged-child', '1', ?, ?, ?,
                      'parsed_pending_confirmation', ?)
            """,
            (
                child_public_id,
                parent["source_type"],
                parent["source_public_id"],
                parent["raw_text"],
                parent["parsed_payload"],
                parent["normalized_payload"],
                parent["id"],
            ),
        )
        child = conn.execute(
            "SELECT id FROM parser_outputs WHERE public_id = ?", (child_public_id,)
        ).fetchone()
        assert child is not None
        updated = conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? "
            "WHERE public_id = ? AND parser_output_id = ?",
            (child["id"], parent["source_public_id"], parent["id"]),
        )
        assert updated.rowcount == 1
        conn.commit()
        conn.execute(str(trigger["sql"]))
        conn.commit()
    return child_public_id


def test_managed_draft_card_delivery_unknown_and_reissue_stay_local(
    managed_workspace: ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("S1C-A delivery bookkeeping must never send")

    monkeypatch.setattr("socket.socket.connect", refuse_network)
    monkeypatch.setattr("socket.create_connection", refuse_network)

    _proposal_id, redeemed, _ = _start_guided_edit(managed_workspace)
    initial_card = redeemed.response["result"]["human_draft_card"]
    card_id = initial_card["card_generation_public_id"]
    card_text = _whole_card_text(card_id)
    route, _ = _capture_route(managed_workspace, card_text, message_id=90)
    assert route.exit_code == errors.EXIT_OK, route.response
    operation_id = route.response["result"]["interaction_route"]["operation_key"]
    fields = {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-19",
        "merchant": "Synthetic Cafe",
        "description": "Lunch",
        "category": "Food",
    }
    apply_request = _request(
        managed_workspace,
        envelope.COMMAND_APPLY_HUMAN_DRAFT_CARD,
        {
            **_context(managed_workspace),
            "card_generation_public_id": card_id,
            "telegram_message_id": 90,
            "operation_public_id": operation_id,
            "raw_card_text": card_text,
            "field_values": fields,
        },
        idempotency_key=f"bridge-human-draft-apply:{operation_id}",
    )
    applied = _run(managed_workspace, apply_request)
    apply_replay = _run(managed_workspace, apply_request)
    assert applied.exit_code == errors.EXIT_OK, applied.response
    assert applied.response["result"]["completeness"] == "complete"
    assert applied.response["result"]["final_transaction_created"] is False
    assert apply_replay.exit_code == errors.EXIT_OK, apply_replay.response
    assert apply_replay.response["idempotent_replay"] is True
    card = applied.response["result"]
    card_id = card["card_generation_public_id"]

    attempt_id = _framed_hash("d1-card-delivery-v1", card_id, "reply")
    attempt_arguments = {
        **_context(managed_workspace),
        "card_generation_public_id": card_id,
        "attempt_public_id": attempt_id,
        "delivery_material_hash": "a" * 64,
        "transport_mode": "reply",
    }
    attempt_request = _request(
        managed_workspace,
        envelope.COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY,
        attempt_arguments,
        idempotency_key=f"bridge-human-draft-delivery:{attempt_id}",
    )
    attempt = _run(managed_workspace, attempt_request)
    attempt_replay = _run(managed_workspace, attempt_request)
    assert attempt.exit_code == errors.EXIT_OK, attempt.response
    assert attempt_replay.exit_code == errors.EXIT_OK, attempt_replay.response
    assert attempt_replay.response["idempotent_replay"] is True

    claimed_success_id = _framed_hash("d1-card-observation-v1", attempt_id, "unverified-success")
    claimed_success_arguments = {
        **_context(managed_workspace),
        "attempt_public_id": attempt_id,
        "observation_public_id": claimed_success_id,
        "outcome": "success",
        "error_code": None,
        "outbound_message_id": "synthetic-unverified-outbound-id",
        "trusted_receipt_hash": "c" * 64,
    }
    claimed_success = _assert_refused_without_writes(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
            claimed_success_arguments,
            idempotency_key=f"bridge-human-draft-observation:{claimed_success_id}",
        ),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    claimed_success_error = cast(dict[str, object], claimed_success.response["error"])
    assert claimed_success_error["code"] in {
        errors.HUMAN_DRAFT_AUTHORITY_REFUSED,
        errors.HUMAN_DRAFT_CONFLICT,
    }

    claimed_receipt_id = _framed_hash("d1-card-observation-v1", attempt_id, "unverified-receipt")
    claimed_receipt_arguments = {
        **_context(managed_workspace),
        "attempt_public_id": attempt_id,
        "observation_public_id": claimed_receipt_id,
        "outcome": "unknown",
        "error_code": None,
        "outbound_message_id": None,
        "trusted_receipt_hash": "d" * 64,
    }
    claimed_receipt = _assert_refused_without_writes(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
            claimed_receipt_arguments,
            idempotency_key=f"bridge-human-draft-observation:{claimed_receipt_id}",
        ),
        expected_exit_codes=(errors.EXIT_AUTHORITY_REFUSED,),
    )
    claimed_receipt_error = cast(dict[str, object], claimed_receipt.response["error"])
    assert claimed_receipt_error["code"] in {
        errors.HUMAN_DRAFT_AUTHORITY_REFUSED,
        errors.HUMAN_DRAFT_CONFLICT,
    }

    observation_id = _framed_hash("d1-card-observation-v1", attempt_id, "initial")
    outcome_arguments = {
        **_context(managed_workspace),
        "attempt_public_id": attempt_id,
        "observation_public_id": observation_id,
        "outcome": "unknown",
        "error_code": None,
        "outbound_message_id": None,
        "trusted_receipt_hash": None,
    }
    outcome_request = _request(
        managed_workspace,
        envelope.COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
        outcome_arguments,
        idempotency_key=f"bridge-human-draft-observation:{observation_id}",
    )
    recorded = _run(managed_workspace, outcome_request)
    outcome_replay = _run(managed_workspace, outcome_request)
    assert recorded.exit_code == errors.EXIT_OK, recorded.response
    assert outcome_replay.exit_code == errors.EXIT_OK, outcome_replay.response
    assert outcome_replay.response["idempotent_replay"] is True
    conflict_outcome = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
            {**outcome_arguments, "outcome": "failure", "error_code": "synthetic_failure"},
            idempotency_key=f"bridge-human-draft-observation:{observation_id}",
        ),
    )
    assert conflict_outcome.exit_code == errors.EXIT_AUTHORITY_REFUSED

    queried = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_GET_HUMAN_DRAFT_CARD,
            {**_context(managed_workspace), "attempt_public_id": attempt_id},
        ),
    )
    assert queried.exit_code == errors.EXIT_OK, queried.response
    assert queried.response["result"]["delivery_state"] == "unknown"
    recovery_id = _framed_hash(
        "d1-card-recovery-v1",
        str(card["draft_public_id"]),
        str(card["original_operation_or_start_public_id"]),
        str(card_id),
    )
    reissue_arguments = {
        **_context(managed_workspace),
        "expected_current_generation_public_id": card_id,
        "original_operation_or_start_public_id": card["original_operation_or_start_public_id"],
        "recovery_public_id": recovery_id,
        "recovery_material_hash": "b" * 64,
        "queried_delivery_state_hash": queried.response["result"]["delivery_state_hash"],
        "reason": "unknown_after_query",
    }
    reissue_request = _request(
        managed_workspace,
        envelope.COMMAND_REISSUE_HUMAN_DRAFT_CARD,
        reissue_arguments,
        idempotency_key=f"bridge-human-draft-reissue:{recovery_id}",
    )
    reissued = _run(managed_workspace, reissue_request)
    reissue_replay = _run(managed_workspace, reissue_request)
    assert reissued.exit_code == errors.EXIT_OK, reissued.response
    assert reissue_replay.exit_code == errors.EXIT_OK, reissue_replay.response
    assert reissue_replay.response["idempotent_replay"] is True
    assert reissued.response["result"]["card_generation_public_id"] != card_id
    assert reissued.response["result"]["final_transaction_created"] is False
    assert _read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0
