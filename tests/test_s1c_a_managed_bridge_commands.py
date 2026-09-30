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
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import ManagedStagingProfile, ProfilePaths
from finance_core.receipt_staging_runner.workspace import generate_callback_signing_key
from tests.test_managed_staging_profile import _blank_profile

ACTOR = "111"
ACCOUNT = "acct"
CONVERSATION = "111"
BINDING = "binding"
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


def _seed_proposal(workspace: ManagedBridgeWorkspace) -> str:
    """Attach a synthetic proposal to a real, routed S1C-A text capture.

    The S3 parser command remains refused.  The test seeds only the proposal
    result after public capture_interaction has persisted an initial-intake
    route, then binds that result through the migration's permitted pointer.
    """
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
    captured, _capture_request = _capture_route(
        workspace, "lunch 12.50", message_id=int(SEEDED_MESSAGE_ID)
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
            ) VALUES (?, 'telegram_text', ?, 'test', '1', 'lunch 12.50', ?,
                      'parsed_pending_confirmation')
            """,
            (SEEDED_PROPOSAL_ID, intake_public_id, json.dumps(payload)),
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


def _run(workspace: ManagedBridgeWorkspace, request: dict[str, object]) -> support.CliOutcome:
    before = len(workspace.sessions)
    outcome = support.run_cli(request)
    opened = workspace.sessions[before:]
    assert len(opened) == 1, (request["command"], outcome.response, len(opened))
    observation = opened[0]
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


def _context(workspace: ManagedBridgeWorkspace) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "operator_actor_id": ACTOR,
        "telegram_account_id": ACCOUNT,
        "telegram_conversation_id": CONVERSATION,
        "conversation_binding_id": BINDING,
    }


def _review(workspace: ManagedBridgeWorkspace, proposal_id: str) -> dict[str, object]:
    outcome = _run(
        workspace,
        _request(
            workspace,
            envelope.COMMAND_GET_REVIEW,
            {"proposal_public_id": proposal_id},
        ),
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
) -> tuple[support.CliOutcome, dict[str, object]]:
    arguments: dict[str, object] = {
        **_context(workspace),
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
) -> tuple[support.CliOutcome, dict[str, object]]:
    update = support.telegram_text_update(
        text,
        update_id=1000 + message_id,
        message_id=message_id,
    )
    arguments = support.authenticated_text_capture_arguments(
        workspace,
        update,
        account_id=ACCOUNT,
        binding_id=BINDING,
    )
    arguments.pop("kind")
    request = _request(
        workspace,
        envelope.COMMAND_CAPTURE_INTERACTION,
        arguments,
        idempotency_key=support.canonical_capture_key(message_id=message_id),
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


def _decision_arguments(review: dict[str, object], action: str) -> dict[str, object]:
    callback_tokens = cast(dict[str, dict[str, object]], review["callback_tokens"])
    entry = callback_tokens[action]
    return {
        "proposal_public_id": review["proposal_public_id"],
        "operator_actor_id": ACTOR,
        "proposal_version": review["proposal_version"],
        "content_hash": review["effective_content_hash"],
        "callback_token": entry["token"],
        "callback_expiry": entry["expiry"],
    }


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
            {"proposal_public_id": proposal_id},
        ),
    )
    second = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_GET_REVIEW,
            {"proposal_public_id": proposal_id},
        ),
    )
    assert first.exit_code == errors.EXIT_OK, first.response
    assert second.exit_code == errors.EXIT_OK, second.response
    assert managed_workspace.sessions[-2].total_changes == 0
    assert managed_workspace.sessions[-1].total_changes == 0
    for field_name in ("proposal_public_id", "proposal_version", "effective_content_hash"):
        assert first.response["result"][field_name] == second.response["result"][field_name]

    missing = _run(
        managed_workspace,
        _request(
            managed_workspace,
            envelope.COMMAND_GET_REVIEW,
            {"proposal_public_id": "prop_s1c_a_missing"},
        ),
    )
    assert missing.exit_code == errors.EXIT_VALIDATION_REFUSED
    assert missing.response["error"]["code"] == errors.PROPOSAL_NOT_FOUND
    profile = workspace_access.managed_profile_for_workspace(managed_workspace.workspace_path)
    assert profile is not None
    try:
        with exclusive_cut(profile, timeout_seconds=0):
            pass
    finally:
        profile.close()


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
