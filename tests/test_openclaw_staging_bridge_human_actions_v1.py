"""S5c durable direct-human action-reference and redemption tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import human_actions
from finance_core.parser_proposals.human_revision import publish_human_revision_in_transaction
from finance_core.receipt_staging_runner.workspace import load_callback_signing_key
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.staging_guard import create_staging_database, open_staging_database
from tests.test_parser_human_drafts_v1 import (
    _card_text,
    _complete_validator,
    _start,
)

ACTOR = "111"
ACCOUNT = "finance-account"
CONVERSATION = "111"
BINDING = "binding-1"
BATCH = "a" * 32


def _d1_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    return conn


def _d1_frame(domain: str, *fields: str) -> str:
    payload = domain.encode("ascii") + b"\0" + len(fields).to_bytes(4, "big")
    for field in fields:
        encoded = field.encode("utf-8")
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def proposal(workspace: support.BridgeWorkspace, text: str = "lunch 12.50") -> str:
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(workspace, support.telegram_text_update(text)),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    return str(capture.response["result"]["proposal_public_id"])


def issue_arguments(
    workspace: support.BridgeWorkspace,
    proposal_public_id: str,
    **overrides: object,
) -> dict[str, object]:
    review = support.run_cli(
        support.make_request(
            "get_review",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_public_id,
            },
        )
    )
    assert review.exit_code == bridge_errors.EXIT_OK, review.response
    arguments: dict[str, object] = {
        "workspace_path": str(workspace.workspace_path),
        "proposal_public_id": proposal_public_id,
        "operator_actor_id": ACTOR,
        "telegram_account_id": ACCOUNT,
        "telegram_conversation_id": CONVERSATION,
        "conversation_binding_id": BINDING,
        "reference_batch_id": BATCH,
        "token_ttl_seconds": 600,
        "expected_proposal_version": review.response["result"]["proposal_version"],
        "expected_content_hash": review.response["result"]["effective_content_hash"],
    }
    arguments.update(overrides)
    return arguments


def issue(
    workspace: support.BridgeWorkspace,
    proposal_public_id: str,
    **overrides: object,
) -> support.CliOutcome:
    batch = str(overrides.get("reference_batch_id", BATCH))
    return support.run_cli(
        support.make_request(
            "issue_human_actions",
            issue_arguments(workspace, proposal_public_id, **overrides),
            idempotency_key=support.canonical_human_action_issuance_key(batch),
        )
    )


def redeem_arguments(
    workspace: support.BridgeWorkspace,
    reference: str,
    action: str,
    callback_id: str = "callback-1",
    **overrides: object,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "workspace_path": str(workspace.workspace_path),
        "short_reference": reference,
        "action": action,
        "operator_actor_id": ACTOR,
        "telegram_account_id": ACCOUNT,
        "telegram_conversation_id": CONVERSATION,
        "conversation_binding_id": BINDING,
        "callback_id": callback_id,
        "callback_message_id": 20,
    }
    arguments.update(overrides)
    return arguments


def redeem(
    workspace: support.BridgeWorkspace,
    reference: str,
    action: str,
    callback_id: str = "callback-1",
    **overrides: object,
) -> support.CliOutcome:
    return support.run_cli(
        support.make_request(
            "redeem_human_action",
            redeem_arguments(workspace, reference, action, callback_id=callback_id, **overrides),
            idempotency_key=support.canonical_human_action_redemption_key(callback_id),
        )
    )


def test_issue_is_unpredictable_hashed_append_only_and_idempotent(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    first = issue(workspace, proposal_id)
    replay = issue(workspace, proposal_id)
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert replay.exit_code == bridge_errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"] == first.response["result"]
    actions = first.response["result"]["actions"]
    assert set(actions) == {"confirm", "edit", "reject"}
    references = {entry["reference"] for entry in actions.values()}
    assert len(references) == 3
    assert all(human_actions.reference_is_well_formed(reference) for reference in references)
    assert all(
        len(f"finance-bridge:confirm:{reference}".encode()) <= 64 for reference in references
    )

    conn = support.open_database(workspace)
    try:
        rows = conn.execute(
            "SELECT reference_sha256, issuance_idempotency_key, authenticated_actor_id, "
            "channel_account_id, channel_conversation_id, conversation_binding_id "
            "FROM openclaw_human_action_references ORDER BY action"
        ).fetchall()
        assert len(rows) == 3
        assert all(row["issuance_idempotency_key"].endswith(BATCH) for row in rows)
        assert all(row["authenticated_actor_id"] == ACTOR for row in rows)
        rendered = json.dumps([dict(row) for row in rows])
        assert all(reference not in rendered for reference in references)
    finally:
        conn.close()

    conflict = issue(workspace, proposal_id, conversation_binding_id="binding-other")
    assert conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT
    ttl_conflict = issue(workspace, proposal_id, token_ttl_seconds=601)
    assert ttl_conflict.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert ttl_conflict.response["error"]["code"] == bridge_errors.IDEMPOTENCY_CONFLICT


def test_atomic_single_use_redemption_allows_only_same_callback_replay(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    reference = issue(workspace, proposal_id).response["result"]["actions"]["confirm"]["reference"]
    first = redeem(workspace, reference, "confirm")
    replay = redeem(workspace, reference, "confirm")
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert replay.exit_code == bridge_errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"] == first.response["result"]
    material = first.response["result"]
    assert material["operator_actor_id"] == ACTOR
    assert material["decision_idempotency_key"] == f"bridge-confirm:{proposal_id}"
    assert material["final_transaction_created"] is False

    reused = redeem(workspace, reference, "confirm", callback_id="callback-2")
    assert reused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert reused.response["error"]["code"] == bridge_errors.HUMAN_ACTION_REFERENCE_REPLAYED
    reject_reference = issue(workspace, proposal_id).response["result"]["actions"]["reject"][
        "reference"
    ]
    callback_reused_for_other_reference = redeem(
        workspace, reject_reference, "reject", callback_id="callback-1"
    )
    assert callback_reused_for_other_reference.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert callback_reused_for_other_reference.response["error"]["code"] == (
        bridge_errors.HUMAN_ACTION_REFERENCE_REPLAYED
    )
    changed_message = redeem(workspace, reference, "confirm", callback_message_id=21)
    assert changed_message.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert changed_message.response["error"]["code"] == (
        bridge_errors.HUMAN_ACTION_REFERENCE_REPLAYED
    )
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 1
        )
        row = conn.execute(
            "SELECT callback_id_sha256 FROM openclaw_human_action_redemptions"
        ).fetchone()
        assert row["callback_id_sha256"] != "callback-1"
    finally:
        conn.close()


def test_redemption_validator_runs_before_append(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    reference = issue(workspace, proposal_id).response["result"]["actions"]["confirm"]["reference"]
    conn = support.open_database(workspace)
    try:
        key = load_callback_signing_key(str(workspace.workspace_path / "runtime"))

        def reject_action(_conn: object, _row: dict, _action: str) -> None:
            raise human_actions.HumanActionReferenceError("action_unavailable")

        with pytest.raises(human_actions.HumanActionReferenceError) as exc_info:
            human_actions.redeem_human_action_reference(
                conn,
                key=key,
                reference=reference,
                action="confirm",
                context=human_actions.HumanActionContext(ACTOR, ACCOUNT, CONVERSATION, BINDING),
                callback_id="validator-blocked",
                callback_message_id=20,
                action_validator=reject_action,
            )
        assert exc_info.value.reason == "action_unavailable"
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_d1_generation_binding_blocks_old_confirm_but_keeps_exact_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_draft_delivery import (
        begin_human_draft_card_delivery,
        get_human_draft_card,
        record_human_draft_card_delivery_outcome,
        reissue_human_draft_card,
    )
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        apply_human_draft_card,
    )

    conn = _d1_connection()
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    published = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_issue_generation_1",
            ACTOR,
            "acct",
            CONVERSATION,
            "binding",
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    assert published.proposal_public_id is not None
    key = b"d1-generation-binding-test-key"
    context = human_actions.HumanActionContext(ACTOR, "acct", CONVERSATION, "binding")
    generation_1_refs, replay = human_actions.issue_human_action_references(
        conn,
        key=key,
        issuance_idempotency_key="bridge-human-action-issue:" + "d" * 32,
        proposal_public_id=published.decision_target_proposal_public_id,
        expected_proposal_version=published.decision_target_proposal_version,
        expected_proposal_content_hash=published.decision_target_proposal_content_hash,
        context=context,
        ttl_seconds=600,
        allowed_actions=("confirm", "reject"),
        card_generation_public_id=published.card_generation_public_id,
        clock=lambda: 1002,
    )
    assert replay is False
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_action_bindings").fetchone()[0] == 2
    )

    attempt_id = _d1_frame("d1-card-delivery-v1", published.card_generation_public_id, "reply")
    draft_context = human_drafts.HumanDraftContext(ACTOR, "acct", CONVERSATION, "binding")
    begin_human_draft_card_delivery(
        conn,
        context=draft_context,
        card_generation_public_id=published.card_generation_public_id,
        attempt_public_id=attempt_id,
        delivery_material_hash="1" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1003,
    )
    observation_id = _d1_frame("d1-card-observation-v1", attempt_id, "initial")
    record_human_draft_card_delivery_outcome(
        conn,
        context=draft_context,
        attempt_public_id=attempt_id,
        observation_public_id=observation_id,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1004,
    )
    observed = get_human_draft_card(
        conn,
        context=draft_context,
        card_generation_public_id=published.card_generation_public_id,
    )
    generation_2 = reissue_human_draft_card(
        conn,
        context=draft_context,
        expected_current_generation_public_id=published.card_generation_public_id,
        original_operation_or_start_public_id="d1op_issue_generation_1",
        recovery_public_id=_d1_frame(
            "d1-card-recovery-v1",
            published.draft_public_id,
            "d1op_issue_generation_1",
            published.card_generation_public_id,
        ),
        recovery_material_hash="2" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )

    old_confirm = next(item for item in generation_1_refs if item.action == "confirm")
    with pytest.raises(human_actions.HumanActionReferenceError, match="generation_stale"):
        human_actions.redeem_human_action_reference(
            conn,
            key=key,
            reference=old_confirm.reference,
            action="confirm",
            context=context,
            callback_id="old-confirm",
            callback_message_id=200,
            clock=lambda: 1101,
        )
    old_reject = next(item for item in generation_1_refs if item.action == "reject")
    redeemed_reject = human_actions.redeem_human_action_reference(
        conn,
        key=key,
        reference=old_reject.reference,
        action="reject",
        context=context,
        callback_id="old-reject",
        callback_message_id=201,
        clock=lambda: 1101,
    )
    assert redeemed_reject.d1_decision_binding is not None
    assert (
        redeemed_reject.d1_decision_binding.card_generation_public_id
        == published.card_generation_public_id
    )
    with pytest.raises(human_actions.HumanActionReferenceError, match="reference_integrity"):
        human_actions.redeem_human_action_reference(
            conn,
            key=b"rotated-wrong-key",
            reference=old_reject.reference,
            action="reject",
            context=context,
            callback_id="old-reject",
            callback_message_id=201,
            clock=lambda: 1101,
        )

    generation_2_refs, _ = human_actions.issue_human_action_references(
        conn,
        key=key,
        issuance_idempotency_key="bridge-human-action-issue:" + "e" * 32,
        proposal_public_id=generation_2.decision_target_proposal_public_id,
        expected_proposal_version=generation_2.decision_target_proposal_version,
        expected_proposal_content_hash=generation_2.decision_target_proposal_content_hash,
        context=context,
        ttl_seconds=600,
        allowed_actions=("confirm",),
        card_generation_public_id=generation_2.card_generation_public_id,
        clock=lambda: 1101,
    )
    redeemed_confirm = human_actions.redeem_human_action_reference(
        conn,
        key=key,
        reference=generation_2_refs[0].reference,
        action="confirm",
        context=context,
        callback_id="new-confirm",
        callback_message_id=202,
        clock=lambda: 1102,
    )
    assert redeemed_confirm.d1_decision_binding is not None
    assert (
        redeemed_confirm.d1_decision_binding.card_generation_public_id
        == generation_2.card_generation_public_id
    )
    with pytest.raises(human_actions.HumanActionReferenceError, match="reference_integrity"):
        human_actions.redeem_human_action_reference(
            conn,
            key=b"rotated-wrong-key",
            reference=generation_2_refs[0].reference,
            action="confirm",
            context=context,
            callback_id="new-confirm",
            callback_message_id=202,
            clock=lambda: 1102,
        )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM openclaw_human_action_redemptions WHERE callback_id_sha256 = ?",
            (hashlib.sha256(b"old-confirm").hexdigest(),),
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_d1_issuance_is_atomic_and_reconstructs_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    database_path = tmp_path / "d1-issuance.sqlite"

    def open_connection() -> sqlite3.Connection:
        opened = open_staging_database(database_path)
        opened.row_factory = sqlite3.Row
        opened.execute("PRAGMA foreign_keys = ON")
        return opened

    conn = create_staging_database(database_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    conn.row_factory = sqlite3.Row
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    card = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_restart_issuance",
            ACTOR,
            "acct",
            CONVERSATION,
            "binding",
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    key = b"d1-restart-issuance-key"
    context = human_actions.HumanActionContext(ACTOR, "acct", CONVERSATION, "binding")
    common = {
        "key": key,
        "proposal_public_id": card.decision_target_proposal_public_id,
        "expected_proposal_version": card.decision_target_proposal_version,
        "expected_proposal_content_hash": card.decision_target_proposal_content_hash,
        "context": context,
        "ttl_seconds": 600,
        "allowed_actions": ("confirm", "reject"),
        "card_generation_public_id": card.card_generation_public_id,
        "clock": lambda: 1002,
    }
    conn.execute(
        """
        CREATE TEMP TRIGGER fail_d1_binding_insert
        BEFORE INSERT ON parser_human_draft_action_bindings
        BEGIN
          SELECT RAISE(ABORT, 'injected_binding_failure');
        END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected_binding_failure"):
        human_actions.issue_human_action_references(
            conn,
            issuance_idempotency_key="bridge-human-action-issue:" + "c" * 32,
            **common,
        )
    assert conn.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_action_bindings").fetchone()[0] == 0
    )
    conn.execute("DROP TRIGGER fail_d1_binding_insert")

    first, replay = human_actions.issue_human_action_references(
        conn,
        issuance_idempotency_key="bridge-human-action-issue:" + "c" * 32,
        **common,
    )
    assert replay is False
    first_material = [(item.action, item.reference) for item in first]
    conn.close()

    conn = open_connection()
    reconstructed, replay = human_actions.issue_human_action_references(
        conn,
        issuance_idempotency_key="bridge-human-action-issue:" + "c" * 32,
        **common,
    )
    assert replay is True
    assert [(item.action, item.reference) for item in reconstructed] == first_material
    resigned, replay = human_actions.issue_human_action_references(
        conn,
        issuance_idempotency_key="bridge-human-action-issue:" + "d" * 32,
        **common,
    )
    assert replay is False
    assert {item.reference for item in resigned}.isdisjoint(
        {item.reference for item in reconstructed}
    )
    assert conn.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0] == 5
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_action_bindings").fetchone()[0] == 4
    )
    conn.close()


def test_concurrent_distinct_callbacks_have_exactly_one_redemption_winner(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    reference = issue(workspace, proposal_id).response["result"]["actions"]["confirm"]["reference"]
    outcomes: list[support.CliOutcome] = []
    lock = threading.Lock()

    def worker(callback_id: str) -> None:
        outcome = redeem(workspace, reference, "confirm", callback_id=callback_id)
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(f"callback-{number}",)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcome.exit_code for outcome in outcomes) == [
        bridge_errors.EXIT_OK,
        bridge_errors.EXIT_AUTHORITY_REFUSED,
    ]
    loser = next(outcome for outcome in outcomes if outcome.exit_code != bridge_errors.EXIT_OK)
    assert loser.response["error"]["code"] == bridge_errors.HUMAN_ACTION_REFERENCE_REPLAYED
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("override", "value", "expected"),
    [
        ("operator_actor_id", "222", bridge_errors.ACTOR_MISMATCH),
        ("telegram_account_id", "other-account", bridge_errors.ACTOR_MISMATCH),
        ("telegram_account_id", "finance\x00account", bridge_errors.ACTOR_MISMATCH),
        ("telegram_conversation_id", "222", bridge_errors.ACTOR_MISMATCH),
        ("conversation_binding_id", "binding-2", bridge_errors.ACTOR_MISMATCH),
        ("conversation_binding_id", "binding\n2", bridge_errors.ACTOR_MISMATCH),
    ],
)
def test_cross_actor_conversation_account_and_binding_are_refused_before_redemption(
    workspace: support.BridgeWorkspace,
    override: str,
    value: str,
    expected: str,
) -> None:
    proposal_id = proposal(workspace)
    reference = issue(workspace, proposal_id).response["result"]["actions"]["reject"]["reference"]
    outcome = redeem(workspace, reference, "reject", **{override: value})
    assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert outcome.response["error"]["code"] == expected
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_wrong_action_malformed_and_expired_references_fail_closed(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    issued = issue(workspace, proposal_id)
    confirm_reference = issued.response["result"]["actions"]["confirm"]["reference"]
    wrong = redeem(workspace, confirm_reference, "reject")
    assert wrong.response["error"]["code"] == bridge_errors.CALLBACK_WRONG_ACTION
    malformed = redeem(workspace, "fha1_too-short", "confirm")
    assert malformed.response["error"]["code"] == bridge_errors.HUMAN_ACTION_REFERENCE_INVALID

    conn = support.open_database(workspace)
    try:
        key = load_callback_signing_key(str(workspace.workspace_path / "runtime"))
        review = support.run_cli(
            support.make_request(
                "get_review",
                {
                    "workspace_path": str(workspace.workspace_path),
                    "proposal_public_id": proposal_id,
                },
            )
        ).response["result"]

        def issuance_clock() -> int:
            assert conn.in_transaction
            return 1000

        direct, _ = human_actions.issue_human_action_references(
            conn,
            key=key,
            issuance_idempotency_key="bridge-human-action-issue:" + "b" * 32,
            proposal_public_id=proposal_id,
            expected_proposal_version=review["proposal_version"],
            expected_proposal_content_hash=review["effective_content_hash"],
            context=human_actions.HumanActionContext(ACTOR, ACCOUNT, CONVERSATION, BINDING),
            ttl_seconds=60,
            clock=issuance_clock,
        )
        expired_reference = next(item.reference for item in direct if item.action == "confirm")

        def redemption_clock() -> int:
            assert conn.in_transaction
            return 1060

        with pytest.raises(human_actions.HumanActionReferenceError) as exc_info:
            human_actions.redeem_human_action_reference(
                conn,
                key=key,
                reference=expired_reference,
                action="confirm",
                context=human_actions.HumanActionContext(ACTOR, ACCOUNT, CONVERSATION, BINDING),
                callback_id="expired-callback",
                callback_message_id=20,
                clock=redemption_clock,
            )
        assert exc_info.value.reason == "reference_expired"
    finally:
        conn.close()


def test_issuance_refuses_when_review_changes_before_reference_transaction(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    old_review = support.run_cli(
        support.make_request(
            "get_review",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_id,
            },
        )
    ).response["result"]
    edited = support.run_cli(
        support.make_request(
            "edit",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_id,
                "operator_actor_id": ACTOR,
                "proposal_version": old_review["proposal_version"],
                "content_hash": old_review["effective_content_hash"],
                "callback_token": old_review["callback_tokens"]["edit"]["token"],
                "callback_expiry": old_review["callback_tokens"]["edit"]["expiry"],
                "field_updates": {"merchant": "Changed Before Issuance"},
            },
            idempotency_key=support.canonical_edit_key(
                proposal_public_id=proposal_id,
                version=old_review["proposal_version"],
                content_hash=old_review["effective_content_hash"],
            ),
        )
    )
    assert edited.exit_code == bridge_errors.EXIT_OK, edited.response

    refused = issue(
        workspace,
        proposal_id,
        expected_proposal_version=old_review["proposal_version"],
        expected_content_hash=old_review["effective_content_hash"],
    )
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] in {
        bridge_errors.STALE_VERSION,
        bridge_errors.STALE_CONTENT_HASH,
    }
    conn = support.open_database(workspace)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_redemption_samples_expiry_only_after_waiting_for_the_sqlite_write_lock(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    review = support.run_cli(
        support.make_request(
            "get_review",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_id,
            },
        )
    ).response["result"]
    conn = support.open_database(workspace)
    try:
        key = load_callback_signing_key(str(workspace.workspace_path / "runtime"))
        issued, _ = human_actions.issue_human_action_references(
            conn,
            key=key,
            issuance_idempotency_key="bridge-human-action-issue:" + "c" * 32,
            proposal_public_id=proposal_id,
            expected_proposal_version=review["proposal_version"],
            expected_proposal_content_hash=review["effective_content_hash"],
            context=human_actions.HumanActionContext(ACTOR, ACCOUNT, CONVERSATION, BINDING),
            ttl_seconds=60,
            clock=lambda: 1000,
        )
        reference = next(item.reference for item in issued if item.action == "confirm")
    finally:
        conn.close()

    locker = support.open_database(workspace)
    locker.execute("BEGIN IMMEDIATE")
    worker_started = threading.Event()
    clock_sampled = threading.Event()
    controlled_now = [1059]
    reasons: list[str] = []

    def worker() -> None:
        worker_conn = support.open_database(workspace)
        try:
            worker_started.set()

            def after_lock_clock() -> int:
                clock_sampled.set()
                return controlled_now[0]

            human_actions.redeem_human_action_reference(
                worker_conn,
                key=key,
                reference=reference,
                action="confirm",
                context=human_actions.HumanActionContext(ACTOR, ACCOUNT, CONVERSATION, BINDING),
                callback_id="lock-crossing-expiry",
                callback_message_id=20,
                clock=after_lock_clock,
            )
        except human_actions.HumanActionReferenceError as exc:
            reasons.append(exc.reason)
        finally:
            worker_conn.close()

    thread = threading.Thread(target=worker)
    thread.start()
    assert worker_started.wait(timeout=1)
    assert not clock_sampled.wait(timeout=0.25)
    controlled_now[0] = 1060
    locker.commit()
    locker.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert clock_sampled.is_set()
    assert reasons == ["reference_expired"]
    verification = support.open_database(workspace)
    try:
        assert (
            verification.execute(
                "SELECT COUNT(*) FROM openclaw_human_action_redemptions"
            ).fetchone()[0]
            == 0
        )
    finally:
        verification.close()


def test_edit_reference_has_the_same_durable_fail_closed_boundary(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    edit_reference = issue(workspace, proposal_id).response["result"]["actions"]["edit"][
        "reference"
    ]
    first = redeem(workspace, edit_reference, "edit", callback_id="edit-callback")
    assert first.exit_code == bridge_errors.EXIT_OK, first.response
    assert first.response["result"]["action"] == "edit"
    assert first.response["result"]["proposal_public_id"] == proposal_id
    material = first.response["result"]
    assert material["decision_idempotency_key"] == (
        f"bridge-edit:{proposal_id}:v{material['proposal_version']}:{material['content_hash']}"
    )
    assert first.response["result"]["final_transaction_created"] is False
    edited = support.run_cli(
        support.make_request(
            "edit",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": proposal_id,
                "operator_actor_id": material["operator_actor_id"],
                "proposal_version": material["proposal_version"],
                "content_hash": material["content_hash"],
                "callback_token": material["callback_token"],
                "callback_expiry": material["callback_expiry"],
                "field_updates": {"merchant": "Human Updated Merchant"},
            },
            idempotency_key=material["decision_idempotency_key"],
        )
    )
    assert edited.exit_code == bridge_errors.EXIT_OK, edited.response
    assert edited.response["result"]["final_transaction_created"] is False
    conn = support.open_database(workspace)
    try:
        assert all(count == 0 for count in support.count_final_facts(conn).values())
    finally:
        conn.close()

    replay = redeem(workspace, edit_reference, "edit", callback_id="edit-callback")
    assert replay.exit_code == bridge_errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    cross_delivery = redeem(
        workspace, edit_reference, "edit", callback_id="different-edit-callback"
    )
    assert cross_delivery.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert cross_delivery.response["error"]["code"] == (
        bridge_errors.HUMAN_ACTION_REFERENCE_REPLAYED
    )


def test_redeemed_material_confirms_without_finalization_and_stale_reference_refuses(
    workspace: support.BridgeWorkspace,
) -> None:
    proposal_id = proposal(workspace)
    references = issue(workspace, proposal_id).response["result"]["actions"]
    material = redeem(workspace, references["confirm"]["reference"], "confirm").response["result"]
    decision = support.run_cli(
        support.make_request(
            "confirm",
            {
                "workspace_path": str(workspace.workspace_path),
                "proposal_public_id": material["proposal_public_id"],
                "operator_actor_id": material["operator_actor_id"],
                "proposal_version": material["proposal_version"],
                "content_hash": material["content_hash"],
                "callback_token": material["callback_token"],
                "callback_expiry": material["callback_expiry"],
            },
            idempotency_key=material["decision_idempotency_key"],
        )
    )
    assert decision.exit_code == bridge_errors.EXIT_OK
    assert decision.response["result"]["final_transaction_created"] is False
    conn = support.open_database(workspace)
    try:
        assert all(count == 0 for count in support.count_final_facts(conn).values())
    finally:
        conn.close()

    second_workspace = support.create_bridge_workspace(
        workspace.workspace_path.parent, name="stale"
    )
    second_proposal = proposal(second_workspace, "coffee 6 merchant cafe")
    stale_reference = issue(second_workspace, second_proposal).response["result"]["actions"][
        "edit"
    ]["reference"]
    review = support.run_cli(
        support.make_request(
            "get_review",
            {
                "workspace_path": str(second_workspace.workspace_path),
                "proposal_public_id": second_proposal,
            },
        )
    ).response["result"]
    edit = support.run_cli(
        support.make_request(
            "edit",
            {
                "workspace_path": str(second_workspace.workspace_path),
                "proposal_public_id": second_proposal,
                "operator_actor_id": ACTOR,
                "proposal_version": review["proposal_version"],
                "content_hash": review["effective_content_hash"],
                "callback_token": review["callback_tokens"]["edit"]["token"],
                "callback_expiry": review["callback_tokens"]["edit"]["expiry"],
                "field_updates": {"merchant": "Updated Cafe"},
            },
            idempotency_key=support.canonical_edit_key(
                proposal_public_id=second_proposal,
                version=review["proposal_version"],
                content_hash=review["effective_content_hash"],
            ),
        )
    )
    assert edit.exit_code == bridge_errors.EXIT_OK, edit.response
    stale_issuance_replay = issue(
        second_workspace,
        second_proposal,
        expected_proposal_version=review["proposal_version"],
        expected_content_hash=review["effective_content_hash"],
    )
    assert stale_issuance_replay.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert stale_issuance_replay.response["error"]["code"] in {
        bridge_errors.STALE_VERSION,
        bridge_errors.STALE_CONTENT_HASH,
    }
    stale = redeem(second_workspace, stale_reference, "edit")
    assert stale.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert stale.response["error"]["code"] in {
        bridge_errors.STALE_VERSION,
        bridge_errors.STALE_CONTENT_HASH,
    }
