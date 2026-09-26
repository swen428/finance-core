"""Focused authority and atomicity coverage for parser confirmation UoWs."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finance_core.openclaw_staging_bridge import human_actions
from finance_core.parser_proposals.confirmation import confirm_proposal, reject_proposal
from finance_core.parser_proposals.content_hash import compute_proposal_content_hash
from finance_core.parser_proposals.conversion import (
    MissingConfirmationRecordError,
    ProposalConversionError,
    StaleProposalConfirmationError,
    convert_confirmed_proposal_to_transaction,
)
from finance_core.parser_proposals.human_drafts import (
    HumanDraftError,
)
from finance_core.parser_proposals.human_revision import publish_human_revision_in_transaction
from finance_core.parser_proposals.repository import (
    CanonicalTransactionRepository,
    ParserAuthorizationRepository,
    ParserConversionRepository,
    ParserProposalRepository,
)
from finance_core.parser_proposals.service import (
    ParserConfirmationError,
    UnauthorizedConfirmationActorError,
    confirm_parser_proposal,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.staging_guard import create_staging_database, open_staging_database
from tests.test_parser_human_drafts_v1 import (
    _card_text,
    _complete_validator,
    _start,
)

LEGACY_D1_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS[:-1]


def _proposal(
    conn: sqlite3.Connection, *, amount: object = "6.40", currency: object = "SGD"
) -> int:
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": amount,
        "currency": currency,
        "transaction_date": "2026-07-12",
        "merchant": "Coffee Shop",
        "description": "coffee",
    }
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
          public_id, source_type, parser_name, parser_version, raw_text,
          parsed_payload, normalized_payload, parse_status
        ) VALUES (?, 'telegram_text', 'pytest', 'v1', 'Coffee SGD 6.40', ?, ?,
                  'parsed_pending_confirmation')
        """,
        (f"parser_auth_{cursor_suffix(conn)}", json.dumps(payload), json.dumps(payload)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def cursor_suffix(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0]) + 1


def _d1_frame(domain: str, *fields: str) -> str:
    payload = domain.encode("ascii") + b"\0" + len(fields).to_bytes(4, "big")
    for field in fields:
        encoded = field.encode("utf-8")
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def _published_d1_card(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    return apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_authorize_generation_1",
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )


@pytest.fixture()
def legacy_d1_connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = create_staging_database(
        tmp_path / "legacy-d1.sqlite", migration_paths=LEGACY_D1_MIGRATION_PATHS
    )
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _redeemed_d1_action(
    conn: sqlite3.Connection,
    *,
    card,
    action: str,
    issuance_suffix: str,
):
    key = b"d1-authoritative-decision-test-key"
    context = human_actions.HumanActionContext("111", "acct", "111", "binding")
    issued, _ = human_actions.issue_human_action_references(
        conn,
        key=key,
        issuance_idempotency_key="bridge-human-action-issue:" + issuance_suffix * 32,
        proposal_public_id=card.decision_target_proposal_public_id,
        expected_proposal_version=card.decision_target_proposal_version,
        expected_proposal_content_hash=card.decision_target_proposal_content_hash,
        context=context,
        ttl_seconds=600,
        allowed_actions=(action,),
        card_generation_public_id=card.card_generation_public_id,
        clock=lambda: 1002,
    )
    return human_actions.redeem_human_action_reference(
        conn,
        key=key,
        reference=issued[0].reference,
        action=action,
        context=context,
        callback_id=f"d1-{action}-callback",
        callback_message_id=200,
        clock=lambda: 1003,
    )


def _database_dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def test_human_confirmation_is_authoritative_and_conversion_is_canonical(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    confirmation = confirm_proposal(
        migrated_temp_db_connection,
        parser_output_id,
        actor="owner-authenticated",
        confirmation_public_id="pca_test_human",
    )
    result = convert_confirmed_proposal_to_transaction(
        migrated_temp_db_connection, parser_output_id
    )

    authorization = migrated_temp_db_connection.execute(
        "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?",
        (parser_output_id,),
    ).fetchone()
    audit = migrated_temp_db_connection.execute(
        "SELECT * FROM parser_proposal_conversion_audit WHERE parser_output_id = ?",
        (parser_output_id,),
    ).fetchone()
    assert authorization["actor_type"] == "human"
    assert authorization["authenticated_actor_id"] == "owner-authenticated"
    assert authorization["proposal_content_hash"] == confirmation["proposal_content_hash"]
    assert audit["transaction_id"] == result["transaction_id"]
    assert audit["confirmation_public_id"] == "pca_test_human"
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE id = ?", (result["transaction_id"],)
        ).fetchone()[0]
        == 1
    )


def test_d1_confirm_requires_redeemed_current_generation_and_closes_head_atomically(
    legacy_d1_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import service

    migrated_temp_db_connection = legacy_d1_connection
    card = _published_d1_card(migrated_temp_db_connection, monkeypatch)
    proposal_id = migrated_temp_db_connection.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (card.decision_target_proposal_public_id,),
    ).fetchone()[0]
    with pytest.raises(ParserConfirmationError, match="decision_binding_required"):
        confirm_proposal(
            migrated_temp_db_connection,
            proposal_id,
            actor="111",
            confirmation_public_id="pca_d1_unbound",
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations"
        ).fetchone()[0]
        == 0
    )

    redeemed = _redeemed_d1_action(
        migrated_temp_db_connection,
        card=card,
        action="confirm",
        issuance_suffix="1",
    )
    assert redeemed.d1_decision_binding is not None
    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:16:44+00:00")
    confirmed = confirm_proposal(
        migrated_temp_db_connection,
        proposal_id,
        actor="111",
        confirmation_public_id="pca_d1_confirm",
        d1_decision_binding=redeemed.d1_decision_binding,
    )
    replay = confirm_proposal(
        migrated_temp_db_connection,
        proposal_id,
        actor="111",
        confirmation_public_id="pca_d1_confirm",
        d1_decision_binding=redeemed.d1_decision_binding,
    )
    assert confirmed["idempotent"] is False
    assert replay["idempotent"] is True
    assert (
        migrated_temp_db_connection.execute("SELECT state FROM parser_human_drafts").fetchone()[0]
        == "confirmed"
    )
    operation = migrated_temp_db_connection.execute(
        "SELECT operation_type, decision_public_id, action_reference_id "
        "FROM parser_human_draft_operations WHERE operation_type = 'confirmed'"
    ).fetchone()
    reference_id = migrated_temp_db_connection.execute(
        "SELECT id FROM openclaw_human_action_references WHERE action = 'confirm'"
    ).fetchone()[0]
    assert tuple(operation) == ("confirmed", "pca_d1_confirm", reference_id)
    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:33:21+00:00")
    with pytest.raises(ParserConfirmationError, match="decision_binding_stale"):
        confirm_proposal(
            migrated_temp_db_connection,
            proposal_id,
            actor="111",
            confirmation_public_id="pca_d1_confirm",
            d1_decision_binding=redeemed.d1_decision_binding,
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations WHERE operation_type = 'confirmed'"
        ).fetchone()[0]
        == 1
    )
    from finance_core.parser_proposals.human_draft_delivery import reissue_human_draft_card
    from finance_core.parser_proposals.human_drafts import HumanDraftContext

    with pytest.raises(HumanDraftError, match="draft_terminal"):
        reissue_human_draft_card(
            migrated_temp_db_connection,
            context=HumanDraftContext("111", "acct", "111", "binding"),
            expected_current_generation_public_id=card.card_generation_public_id,
            original_operation_or_start_public_id="d1op_authorize_generation_1",
            recovery_public_id=_d1_frame(
                "d1-card-recovery-v1",
                card.draft_public_id,
                "d1op_authorize_generation_1",
                card.card_generation_public_id,
            ),
            recovery_material_hash="8" * 64,
            queried_delivery_state_hash="9" * 64,
            reason="unknown_after_query",
            now_epoch=2002,
        )


def test_d1_confirm_rechecks_generation_after_redemption_before_decision(
    legacy_d1_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import service
    from finance_core.parser_proposals.human_draft_delivery import (
        begin_human_draft_card_delivery,
        get_human_draft_card,
        record_human_draft_card_delivery_outcome,
        reissue_human_draft_card,
    )
    from finance_core.parser_proposals.human_drafts import HumanDraftContext

    migrated_temp_db_connection = legacy_d1_connection
    card = _published_d1_card(migrated_temp_db_connection, monkeypatch)
    redeemed = _redeemed_d1_action(
        migrated_temp_db_connection,
        card=card,
        action="confirm",
        issuance_suffix="3",
    )
    assert redeemed.d1_decision_binding is not None
    context = HumanDraftContext("111", "acct", "111", "binding")
    attempt_id = _d1_frame("d1-card-delivery-v1", card.card_generation_public_id, "reply")
    begin_human_draft_card_delivery(
        migrated_temp_db_connection,
        context=context,
        card_generation_public_id=card.card_generation_public_id,
        attempt_public_id=attempt_id,
        delivery_material_hash="3" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1004,
    )
    observation_id = _d1_frame("d1-card-observation-v1", attempt_id, "initial")
    record_human_draft_card_delivery_outcome(
        migrated_temp_db_connection,
        context=context,
        attempt_public_id=attempt_id,
        observation_public_id=observation_id,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1005,
    )
    observed = get_human_draft_card(
        migrated_temp_db_connection,
        context=context,
        card_generation_public_id=card.card_generation_public_id,
    )
    reissue_human_draft_card(
        migrated_temp_db_connection,
        context=context,
        expected_current_generation_public_id=card.card_generation_public_id,
        original_operation_or_start_public_id="d1op_authorize_generation_1",
        recovery_public_id=_d1_frame(
            "d1-card-recovery-v1",
            card.draft_public_id,
            "d1op_authorize_generation_1",
            card.card_generation_public_id,
        ),
        recovery_material_hash="4" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )
    proposal_id = migrated_temp_db_connection.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (card.decision_target_proposal_public_id,),
    ).fetchone()[0]
    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:18:22+00:00")
    with pytest.raises(ParserConfirmationError, match="decision_binding_stale"):
        confirm_proposal(
            migrated_temp_db_connection,
            proposal_id,
            actor="111",
            confirmation_public_id="pca_d1_raced",
            d1_decision_binding=redeemed.d1_decision_binding,
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations"
        ).fetchone()[0]
        == 0
    )


def test_d1_incomplete_reject_closes_head_and_expired_replay_fails_closed(
    legacy_d1_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import service

    migrated_temp_db_connection = legacy_d1_connection
    incomplete = _start(
        migrated_temp_db_connection,
        payload={
            "intent": "personal_expense",
            "transaction_date": "2026-09-13",
            "merchant": "Kopitiam",
            "description": "Lunch",
            "category": "food",
        },
    )
    redeemed = _redeemed_d1_action(
        migrated_temp_db_connection,
        card=incomplete,
        action="reject",
        issuance_suffix="2",
    )
    assert redeemed.d1_decision_binding is not None
    proposal_id = migrated_temp_db_connection.execute(
        "SELECT decision_target_parser_output_id FROM parser_human_drafts"
    ).fetchone()[0]
    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:16:44+00:00")
    rejected = reject_proposal(
        migrated_temp_db_connection,
        proposal_id,
        actor="111",
        confirmation_public_id="pca_d1_reject",
        d1_decision_binding=redeemed.d1_decision_binding,
    )
    assert rejected["to_status"] == "rejected"
    assert (
        migrated_temp_db_connection.execute("SELECT state FROM parser_human_drafts").fetchone()[0]
        == "rejected"
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations WHERE operation_type = 'rejected'"
        ).fetchone()[0]
        == 1
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations "
            "WHERE confirmation_state = 'confirmed'"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    )

    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:33:21+00:00")
    with pytest.raises(ParserConfirmationError, match="reject_binding_stale"):
        reject_proposal(
            migrated_temp_db_connection,
            proposal_id,
            actor="111",
            confirmation_public_id="pca_d1_reject",
            d1_decision_binding=redeemed.d1_decision_binding,
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations WHERE operation_type = 'rejected'"
        ).fetchone()[0]
        == 1
    )


def test_d1_forged_cross_context_action_and_target_bindings_write_nothing(
    legacy_d1_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import service

    migrated_temp_db_connection = legacy_d1_connection
    card = _published_d1_card(migrated_temp_db_connection, monkeypatch)
    proposal_id = migrated_temp_db_connection.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (card.decision_target_proposal_public_id,),
    ).fetchone()[0]
    confirmed = _redeemed_d1_action(
        migrated_temp_db_connection, card=card, action="confirm", issuance_suffix="4"
    ).d1_decision_binding
    rejected = _redeemed_d1_action(
        migrated_temp_db_connection, card=card, action="reject", issuance_suffix="5"
    ).d1_decision_binding
    assert confirmed is not None and rejected is not None
    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:16:44+00:00")
    before = _database_dump(migrated_temp_db_connection)
    invalid = (
        replace(confirmed, reference_public_id="fha_ref_missing"),
        replace(confirmed, card_generation_public_id="d1card_missing"),
        replace(confirmed, authenticated_actor_id="attacker"),
        replace(confirmed, telegram_account_id="other-account"),
        replace(confirmed, telegram_conversation_id="other-conversation"),
        replace(confirmed, conversation_binding_id="other-binding"),
        rejected,
    )
    for index, binding in enumerate(invalid):
        with pytest.raises(ParserConfirmationError, match="decision_binding"):
            confirm_proposal(
                migrated_temp_db_connection,
                proposal_id,
                actor="111",
                confirmation_public_id=f"pca_d1_invalid_{index}",
                d1_decision_binding=binding,
            )
        assert _database_dump(migrated_temp_db_connection) == before

    non_d1_id = _proposal(migrated_temp_db_connection)
    before_wrong_target = _database_dump(migrated_temp_db_connection)
    with pytest.raises(ParserConfirmationError, match="decision_binding_invalid"):
        confirm_parser_proposal(
            migrated_temp_db_connection,
            non_d1_id,
            authenticated_actor_id="111",
            confirmation_public_id="pca_d1_wrong_target",
            d1_decision_binding=confirmed,
        )
    assert _database_dump(migrated_temp_db_connection) == before_wrong_target


@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_d1_terminal_cleanup_failure_rolls_back_every_decision_write(
    legacy_d1_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    from finance_core.parser_proposals import service

    migrated_temp_db_connection = legacy_d1_connection
    card = _published_d1_card(migrated_temp_db_connection, monkeypatch)
    proposal_id = migrated_temp_db_connection.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (card.decision_target_proposal_public_id,),
    ).fetchone()[0]
    redeemed = _redeemed_d1_action(
        migrated_temp_db_connection,
        card=card,
        action="confirm" if decision == "confirmed" else "reject",
        issuance_suffix="6" if decision == "confirmed" else "7",
    )
    assert redeemed.d1_decision_binding is not None
    helper_name = (
        "confirm_active_human_draft_in_transaction"
        if decision == "confirmed"
        else "reject_active_human_draft_in_transaction"
    )
    original = getattr(service, helper_name)

    def fail_after_terminal_cleanup(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise HumanDraftError("injected_terminal_cleanup_failure")

    monkeypatch.setattr(service, helper_name, fail_after_terminal_cleanup)
    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:16:44+00:00")
    before = _database_dump(migrated_temp_db_connection)
    with pytest.raises(ParserConfirmationError, match="injected_terminal_cleanup_failure"):
        confirm_parser_proposal(
            migrated_temp_db_connection,
            proposal_id,
            authenticated_actor_id="111",
            decision=decision,
            confirmation_public_id=f"pca_d1_rollback_{decision}",
            d1_decision_binding=redeemed.d1_decision_binding,
        )
    assert _database_dump(migrated_temp_db_connection) == before


@pytest.mark.parametrize("action", ["confirm", "reject"])
@pytest.mark.parametrize("expiry_boundary", ["reference", "generation"])
def test_d1_decision_refuses_both_expiry_boundaries_at_equality_without_writes(
    legacy_d1_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expiry_boundary: str,
) -> None:
    from finance_core.parser_proposals import service

    migrated_temp_db_connection = legacy_d1_connection
    card = _published_d1_card(migrated_temp_db_connection, monkeypatch)
    key = b"d1-expiry-boundary-test-key"
    context = human_actions.HumanActionContext("111", "acct", "111", "binding")
    ttl = 60 if expiry_boundary == "reference" else 1200
    issued, _ = human_actions.issue_human_action_references(
        migrated_temp_db_connection,
        key=key,
        issuance_idempotency_key=(
            "bridge-human-action-issue:"
            + ("9" if action == "confirm" else "f") * 31
            + ("1" if expiry_boundary == "reference" else "2")
        ),
        proposal_public_id=card.decision_target_proposal_public_id,
        expected_proposal_version=card.decision_target_proposal_version,
        expected_proposal_content_hash=card.decision_target_proposal_content_hash,
        context=context,
        ttl_seconds=ttl,
        allowed_actions=(action,),
        card_generation_public_id=card.card_generation_public_id,
        clock=lambda: 1002,
    )
    redeemed = human_actions.redeem_human_action_reference(
        migrated_temp_db_connection,
        key=key,
        reference=issued[0].reference,
        action=action,
        context=context,
        callback_id=f"expiry-{action}-{expiry_boundary}",
        callback_message_id=200,
        clock=lambda: 1003,
    )
    assert redeemed.d1_decision_binding is not None
    row = migrated_temp_db_connection.execute(
        """
        SELECT refs.expires_at AS reference_expires_at, cards.expires_at AS card_expires_at
        FROM openclaw_human_action_references AS refs
        JOIN parser_human_draft_action_bindings AS bindings ON bindings.reference_id = refs.id
        JOIN parser_human_draft_cards AS cards
          ON cards.card_generation_public_id = bindings.card_generation_public_id
        WHERE refs.reference_public_id = ?
        """,
        (redeemed.d1_decision_binding.reference_public_id,),
    ).fetchone()
    if expiry_boundary == "reference":
        assert int(row["reference_expires_at"]) < int(row["card_expires_at"])
    else:
        assert int(row["card_expires_at"]) < int(row["reference_expires_at"])
    boundary = int(
        row["reference_expires_at"] if expiry_boundary == "reference" else row["card_expires_at"]
    )
    monkeypatch.setattr(
        service,
        "_now",
        lambda _clock: datetime.fromtimestamp(boundary, UTC).isoformat(),
    )
    proposal_id = migrated_temp_db_connection.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (card.decision_target_proposal_public_id,),
    ).fetchone()[0]
    before = _database_dump(migrated_temp_db_connection)
    with pytest.raises(
        ParserConfirmationError,
        match="decision_binding_stale" if action == "confirm" else "reject_binding_stale",
    ):
        confirm_parser_proposal(
            migrated_temp_db_connection,
            proposal_id,
            authenticated_actor_id="111",
            decision="confirmed" if action == "confirm" else "rejected",
            confirmation_public_id=f"pca_d1_expiry_{action}_{expiry_boundary}",
            d1_decision_binding=redeemed.d1_decision_binding,
        )
    assert _database_dump(migrated_temp_db_connection) == before


def test_d1_confirm_and_generation_reissue_have_exactly_one_two_connection_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import service
    from finance_core.parser_proposals.human_draft_delivery import (
        begin_human_draft_card_delivery,
        get_human_draft_card,
        record_human_draft_card_delivery_outcome,
        reissue_human_draft_card,
    )
    from finance_core.parser_proposals.human_drafts import HumanDraftContext

    database_path = tmp_path / "d1-confirm-reissue-race.sqlite"
    setup = create_staging_database(database_path, migration_paths=LEGACY_D1_MIGRATION_PATHS)
    setup.row_factory = sqlite3.Row
    card = _published_d1_card(setup, monkeypatch)
    proposal_id = setup.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (card.decision_target_proposal_public_id,),
    ).fetchone()[0]
    redeemed = _redeemed_d1_action(setup, card=card, action="confirm", issuance_suffix="8")
    assert redeemed.d1_decision_binding is not None
    context = HumanDraftContext("111", "acct", "111", "binding")
    attempt_id = _d1_frame("d1-card-delivery-v1", card.card_generation_public_id, "reply")
    begin_human_draft_card_delivery(
        setup,
        context=context,
        card_generation_public_id=card.card_generation_public_id,
        attempt_public_id=attempt_id,
        delivery_material_hash="a" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1004,
    )
    observation_id = _d1_frame("d1-card-observation-v1", attempt_id, "initial")
    record_human_draft_card_delivery_outcome(
        setup,
        context=context,
        attempt_public_id=attempt_id,
        observation_public_id=observation_id,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1005,
    )
    observed = get_human_draft_card(
        setup,
        context=context,
        card_generation_public_id=card.card_generation_public_id,
    )
    setup.close()

    monkeypatch.setattr(service, "_now", lambda _clock: "1970-01-01T00:18:20+00:00")
    barrier = threading.Barrier(3)
    outcomes: list[tuple[str, bool, str | None]] = []
    outcome_lock = threading.Lock()

    def open_connection() -> sqlite3.Connection:
        opened = open_staging_database(database_path)
        opened.row_factory = sqlite3.Row
        opened.execute("PRAGMA foreign_keys = ON")
        return opened

    def confirm_worker() -> None:
        conn = open_connection()
        barrier.wait()
        try:
            confirm_proposal(
                conn,
                proposal_id,
                actor="111",
                confirmation_public_id="pca_d1_race_confirm",
                d1_decision_binding=redeemed.d1_decision_binding,
            )
            result = ("confirm", True, None)
        except Exception as exc:  # noqa: BLE001 - the losing boundary is asserted below
            result = ("confirm", False, str(exc))
        finally:
            conn.close()
        with outcome_lock:
            outcomes.append(result)

    def reissue_worker() -> None:
        conn = open_connection()
        barrier.wait()
        try:
            reissue_human_draft_card(
                conn,
                context=context,
                expected_current_generation_public_id=card.card_generation_public_id,
                original_operation_or_start_public_id="d1op_authorize_generation_1",
                recovery_public_id=_d1_frame(
                    "d1-card-recovery-v1",
                    card.draft_public_id,
                    "d1op_authorize_generation_1",
                    card.card_generation_public_id,
                ),
                recovery_material_hash="b" * 64,
                queried_delivery_state_hash=observed.delivery_state_hash,
                reason="unknown_after_query",
                now_epoch=1100,
            )
            result = ("reissue", True, None)
        except Exception as exc:  # noqa: BLE001 - the losing boundary is asserted below
            result = ("reissue", False, str(exc))
        finally:
            conn.close()
        with outcome_lock:
            outcomes.append(result)

    threads = [threading.Thread(target=confirm_worker), threading.Thread(target=reissue_worker)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len([outcome for outcome in outcomes if outcome[1]]) == 1, outcomes
    winner = next(outcome for outcome in outcomes if outcome[1])[0]
    loser = next(outcome for outcome in outcomes if not outcome[1])
    if winner == "confirm":
        assert loser == ("reissue", False, "draft_terminal")
    else:
        assert loser == ("confirm", False, "decision_binding_stale")
    verify = open_connection()
    try:
        state = verify.execute("SELECT state FROM parser_human_drafts").fetchone()[0]
        card_count = verify.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0]
        decision_count = verify.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations"
        ).fetchone()[0]
        terminal_count = verify.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations "
            "WHERE operation_type IN ('confirmed', 'rejected')"
        ).fetchone()[0]
        if winner == "confirm":
            assert (state, card_count, decision_count, terminal_count) == ("confirmed", 2, 1, 1)
        else:
            assert (state, card_count, decision_count, terminal_count) == ("active", 3, 0, 0)
        assert verify.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        verify.close()


def test_authorization_migration_schema_has_clean_foreign_keys_and_integrity(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    tables = {
        row[0]
        for row in migrated_temp_db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"parser_proposal_authorizations", "parser_proposal_conversion_audit"} <= tables
    assert migrated_temp_db_connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated_temp_db_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_authorization_migration_applies_after_the_previous_schema_version(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "parser_authorization_migration.sqlite")
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:-1])
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[-1:])
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'parser_proposal_authorizations'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


@pytest.mark.parametrize("actor_type", ["ai", "system", "automation", "test", "unknown"])
def test_non_human_actor_cannot_authorize(
    migrated_temp_db_connection: sqlite3.Connection, actor_type: str
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    with pytest.raises(UnauthorizedConfirmationActorError):
        confirm_proposal(
            migrated_temp_db_connection, parser_output_id, actor="not-human", actor_type=actor_type
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations"
        ).fetchone()[0]
        == 0
    )


def test_legacy_confirmation_never_authorizes_conversion(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    migrated_temp_db_connection.execute(
        "UPDATE parser_outputs SET parse_status = 'confirmed' WHERE id = ?", (parser_output_id,)
    )
    migrated_temp_db_connection.execute(
        "INSERT INTO parser_proposal_confirmations "
        "(parser_output_id, decision, decided_by) VALUES (?, 'confirmed', 'legacy')",
        (parser_output_id,),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(MissingConfirmationRecordError):
        convert_confirmed_proposal_to_transaction(migrated_temp_db_connection, parser_output_id)
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    )


@pytest.mark.parametrize("actor_id", ["", "   "])
def test_empty_authenticated_actor_id_cannot_authorize(
    migrated_temp_db_connection: sqlite3.Connection, actor_id: str
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    with pytest.raises(UnauthorizedConfirmationActorError):
        confirm_proposal(migrated_temp_db_connection, parser_output_id, actor=actor_id)


def test_stale_authorization_is_rejected_after_material_proposal_change(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    confirm_proposal(migrated_temp_db_connection, parser_output_id, actor="owner")
    row = migrated_temp_db_connection.execute(
        "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (parser_output_id,)
    ).fetchone()
    payload = json.loads(row[0])
    payload["amount"] = "7.40"
    migrated_temp_db_connection.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload), parser_output_id),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(StaleProposalConfirmationError):
        convert_confirmed_proposal_to_transaction(migrated_temp_db_connection, parser_output_id)
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    )


def test_revoked_authorization_cannot_convert(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    confirm_proposal(migrated_temp_db_connection, parser_output_id, actor="owner")
    migrated_temp_db_connection.execute(
        """
        UPDATE parser_proposal_authorizations
        SET confirmation_state = 'revoked', revoked_at = '2026-07-12T00:00:00+00:00'
        WHERE parser_output_id = ?
        """,
        (parser_output_id,),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(MissingConfirmationRecordError):
        convert_confirmed_proposal_to_transaction(migrated_temp_db_connection, parser_output_id)


def test_equivalent_money_representations_have_the_same_hash(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection, amount="6.4")
    proposal = ParserProposalRepository(migrated_temp_db_connection).get(parser_output_id)
    assert proposal is not None
    first = compute_proposal_content_hash(migrated_temp_db_connection, proposal)
    payload = json.loads(proposal["parsed_payload"])
    payload["amount"] = "6.40"
    migrated_temp_db_connection.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload), parser_output_id),
    )
    migrated_temp_db_connection.commit()
    updated = ParserProposalRepository(migrated_temp_db_connection).get(parser_output_id)
    assert updated is not None
    assert compute_proposal_content_hash(migrated_temp_db_connection, updated) == first


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (6.4, "SGD"),
        (True, "SGD"),
        ("NaN", "SGD"),
        ("Infinity", "SGD"),
        ("-Infinity", "SGD"),
        ("0", "SGD"),
        ("-0.01", "SGD"),
        ("6.401", "SGD"),
        ("1.5", "JPY"),
        ("1.00", "XXX"),
        ("1.00", "S1D"),
    ],
)
def test_conversion_uses_money_contract_to_reject_unsafe_values(
    migrated_temp_db_connection: sqlite3.Connection, amount: object, currency: object
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection, amount=amount, currency=currency)
    confirm_proposal(migrated_temp_db_connection, parser_output_id, actor="owner")
    with pytest.raises(ProposalConversionError) as exc_info:
        convert_confirmed_proposal_to_transaction(migrated_temp_db_connection, parser_output_id)
    assert "monetary" in str(exc_info.value).lower()
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    )


@pytest.mark.parametrize(
    ("amount", "currency", "expected_amount"),
    [("6.4", "sgd", "6.40"), ("100", "JPY", "100")],
)
def test_conversion_persists_money_contract_canonical_amount(
    migrated_temp_db_connection: sqlite3.Connection,
    amount: str,
    currency: str,
    expected_amount: str,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection, amount=amount, currency=currency)
    confirm_proposal(migrated_temp_db_connection, parser_output_id, actor="owner")
    result = convert_confirmed_proposal_to_transaction(
        migrated_temp_db_connection, parser_output_id
    )
    transaction = migrated_temp_db_connection.execute(
        "SELECT amount, total_amount, currency, notes FROM transactions WHERE id = ?",
        (result["transaction_id"],),
    ).fetchone()
    assert transaction["amount"] == transaction["total_amount"]
    assert transaction["currency"] == currency.upper()
    assert json.loads(transaction["notes"])["canonical_amount"] == expected_amount


def test_confirmation_replay_binds_actor_id_public_id_and_channel(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    first = confirm_proposal(
        migrated_temp_db_connection,
        parser_output_id,
        actor="owner",
        confirmation_public_id="pca_replay",
        confirmation_channel="mobile_app",
    )
    replay = confirm_proposal(
        migrated_temp_db_connection,
        parser_output_id,
        actor="owner",
        confirmation_public_id="pca_replay",
        confirmation_channel="mobile_app",
    )
    assert replay["idempotent"] is True
    assert replay["confirmation_id"] == first["confirmation_id"]
    with pytest.raises(ParserConfirmationError, match="actor"):
        confirm_proposal(
            migrated_temp_db_connection,
            parser_output_id,
            actor="another-human",
            confirmation_public_id="pca_replay",
            confirmation_channel="mobile_app",
        )
    with pytest.raises(ParserConfirmationError, match="ID"):
        confirm_proposal(
            migrated_temp_db_connection,
            parser_output_id,
            actor="owner",
            confirmation_public_id="pca_other",
            confirmation_channel="mobile_app",
        )
    with pytest.raises(ParserConfirmationError, match="channel"):
        confirm_proposal(
            migrated_temp_db_connection,
            parser_output_id,
            actor="owner",
            confirmation_public_id="pca_replay",
            confirmation_channel="cli",
        )


def test_confirmation_failure_rolls_back_all_writes(
    migrated_temp_db_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)

    def fail_status(self: ParserProposalRepository, parser_output_id: int, status: str) -> None:
        database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()[2]
        external = sqlite3.connect(database_path)
        try:
            count = external.execute(
                "SELECT COUNT(*) FROM parser_proposal_authorizations"
            ).fetchone()[0]
            assert count == 0
        finally:
            external.close()
        raise RuntimeError("injected status failure")

    monkeypatch.setattr(ParserProposalRepository, "update_status", fail_status)
    with pytest.raises(RuntimeError, match="injected status failure"):
        confirm_proposal(migrated_temp_db_connection, parser_output_id, actor="owner")
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_events"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (parser_output_id,)
        ).fetchone()[0]
        == "parsed_pending_confirmation"
    )


def test_conversion_audit_failure_rolls_back_canonical_transaction(
    migrated_temp_db_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser_output_id = _proposal(migrated_temp_db_connection)
    confirm_proposal(migrated_temp_db_connection, parser_output_id, actor="owner")

    def fail_audit(self: ParserConversionRepository, **kwargs: object) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(ParserConversionRepository, "insert", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        convert_confirmed_proposal_to_transaction(migrated_temp_db_connection, parser_output_id)
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM parser_proposal_conversion_audit"
        ).fetchone()[0]
        == 0
    )


def test_repositories_are_transaction_neutral_and_do_not_run_runtime_ddl() -> None:
    for repository in (
        ParserProposalRepository,
        ParserAuthorizationRepository,
        ParserConversionRepository,
        CanonicalTransactionRepository,
    ):
        source = inspect.getsource(repository)
        assert ".commit(" not in source
        assert ".rollback(" not in source
        assert "BEGIN " not in source
        assert "CREATE TABLE" not in source
