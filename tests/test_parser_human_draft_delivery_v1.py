"""D1 append-only card-delivery and bounded reissue tests."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from finance_core.parser_proposals.human_draft_delivery import (
    begin_human_draft_card_delivery,
    get_human_draft_card,
    record_human_draft_card_delivery_outcome,
    reissue_human_draft_card,
)
from finance_core.parser_proposals.human_drafts import HumanDraftContext, HumanDraftError
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_parser_human_drafts_v1 import (
    _assert_insert_or_replace_refused,
    _insert_decision,
    _start,
)

LEGACY_D1_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS[:-1]

CONTEXT = HumanDraftContext("111", "acct", "111", "binding")


def _frame(domain: str, *fields: str) -> str:
    payload = domain.encode("ascii") + b"\0" + len(fields).to_bytes(4, "big")
    for field in fields:
        encoded = field.encode("utf-8")
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def _connection(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_delivery_attempt_outcome_projection_and_exact_replay() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation = started.card_generation_public_id
    initial = get_human_draft_card(conn, context=CONTEXT, card_generation_public_id=generation)
    assert initial.delivery_state == "not_attempted"
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    assert (
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation,
            attempt_public_id=attempt_id,
            delivery_material_hash="1" * 64,
            transport_mode="reply",
            outbound_target_message_id=None,
            now_epoch=1001,
        )
        == attempt_id
    )
    assert (
        get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_id).delivery_state
        == "unknown"
    )
    observation_id = _frame("d1-card-observation-v1", attempt_id, "initial")
    with pytest.raises(HumanDraftError, match="trusted_receipt_unverifiable"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=observation_id,
            outcome="success",
            error_code=None,
            outbound_message_id="telegram-900",
            trusted_receipt_hash="2" * 64,
            now_epoch=1002,
        )
    assert (
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=observation_id,
            outcome="failure",
            error_code="transport_failed",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1002,
        )
        == observation_id
    )
    result = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_id)
    assert result.delivery_state == "failure"
    assert result.delivery_attempts[0]["attempt_public_id"] == attempt_id
    assert result.delivery_outcomes[0]["observation_public_id"] == observation_id
    _assert_insert_or_replace_refused(
        conn,
        (
            "parser_human_draft_card_delivery_attempts",
            "parser_human_draft_card_delivery_outcomes",
        ),
    )
    assert (
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=observation_id,
            outcome="failure",
            error_code="transport_failed",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1999,
        )
        == observation_id
    )
    with pytest.raises(HumanDraftError, match="observation_conflict"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=observation_id,
            outcome="failure",
            error_code="timeout",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1002,
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE parser_human_draft_card_delivery_outcomes SET observed_at = 1003")
    conn.rollback()
    conn.close()


def test_delivery_rejects_wrong_slot_and_fake_success_receipt() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "replace")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="3" * 64,
        transport_mode="replace",
        outbound_target_message_id="old-1",
        now_epoch=1001,
    )
    with pytest.raises(HumanDraftError, match="observation_identity"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id="4" * 64,
            outcome="unknown",
            error_code=None,
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1002,
        )
    valid_id = _frame("d1-card-observation-v1", attempt_id, "initial")
    with pytest.raises(HumanDraftError, match="trusted_receipt_unverifiable"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=valid_id,
            outcome="success",
            error_code=None,
            outbound_message_id="made-up",
            trusted_receipt_hash=None,
            now_epoch=1002,
        )
    conn.close()


@pytest.mark.parametrize("mode", ["noop", "refused"])
def test_operation_identity_recovers_committed_noop_or_refusal(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reused historical card must remain queryable by the committed operation ID."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(
        conn,
        payload={
            "intent": "personal_expense",
            "amount": "12.50",
            "currency": "USD",
            "transaction_date": "2026-09-13",
            "merchant": "Kopitiam",
            "description": "Lunch",
            "category": "food",
        },
    )
    fields = {
        key: "" if value is None else str(value) for key, value in started.field_values.items()
    }
    text = (
        f"资料卡编号：{started.card_generation_public_id}\r\n金额: {fields['amount']}\r\n"
        f"币种：{fields['currency']}\r\n日期: {fields['transaction_date']}\r\n"
        f"商户: {fields['merchant']}\r\n描述: {fields['description']}\r\n"
        f"分类: {fields['category']}"
    )
    if mode == "refused":
        fields["transaction_date"] = "2026-02-30"
        text = text.replace("日期: 2026-09-13", "日期: 2026-02-30")
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    operation_id = f"d1op_recover_{mode}"
    result = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            operation_id,
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=lambda *_args, **_kwargs: None,
    )
    assert result.operation_outcome == mode, result.refusal_code
    recovered = get_human_draft_card(conn, context=CONTEXT, operation_public_id=operation_id)
    assert recovered.operation_outcome == mode
    assert recovered.card_generation_public_id == started.card_generation_public_id
    conn.close()


def test_delivery_attempt_context_is_closed_to_its_card() -> None:
    """A forged context must not enter the append-only delivery audit chain."""
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO parser_human_draft_card_delivery_attempts (
                attempt_public_id, card_generation_public_id, delivery_identity,
                delivery_material_hash, authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, transport_mode,
                outbound_target_message_id, attempted_at
            ) VALUES (?, ?, ?, ?, 'attacker', 'wrong', 'wrong', 'wrong', 'reply', NULL, 1001)
            """,
            ("a" * 64, generation, "b" * 64, "c" * 64),
        )
        conn.commit()
    conn.rollback()
    conn.close()


def test_delivery_resolution_rejects_regressed_wall_clock() -> None:
    """Causal resolution cannot be hidden behind an earlier wall-clock timestamp."""
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="d" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    initial_id = _frame("d1-card-observation-v1", attempt_id, "initial")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=attempt_id,
        observation_public_id=initial_id,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1200,
    )
    resolution_id = _frame("d1-card-observation-v1", attempt_id, "resolution")
    with pytest.raises(HumanDraftError, match="observation_time_regression"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=resolution_id,
            outcome="failure",
            error_code="transport_failed",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1100,
        )
    assert (
        get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_id).delivery_state
        == "unknown"
    )
    conn.close()


def test_latest_delivery_attempt_controls_projection_and_recovery_reason() -> None:
    """An older attempt's resolution cannot override a later durable attempt."""
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation = started.card_generation_public_id

    replace_attempt = _frame("d1-card-delivery-v1", generation, "replace")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=replace_attempt,
        delivery_material_hash="1" * 64,
        transport_mode="replace",
        outbound_target_message_id="old-telegram-message",
        now_epoch=1200,
    )
    replace_initial = _frame("d1-card-observation-v1", replace_attempt, "initial")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=replace_attempt,
        observation_public_id=replace_initial,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1201,
    )
    replace_resolution = _frame("d1-card-observation-v1", replace_attempt, "resolution")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=replace_attempt,
        observation_public_id=replace_resolution,
        outcome="failure",
        error_code="replace_failed",
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1202,
    )

    reply_attempt = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=reply_attempt,
        delivery_material_hash="2" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1100,
    )
    begun = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=reply_attempt)
    assert begun.delivery_state == "unknown"
    with pytest.raises(HumanDraftError, match="recovery_evidence_missing"):
        reissue_human_draft_card(
            conn,
            context=CONTEXT,
            expected_current_generation_public_id=generation,
            original_operation_or_start_public_id="d1start_callback_a",
            recovery_public_id=_frame(
                "d1-card-recovery-v1",
                begun.draft_public_id,
                "d1start_callback_a",
                generation,
            ),
            recovery_material_hash="3" * 64,
            queried_delivery_state_hash=begun.delivery_state_hash,
            reason="failure",
            now_epoch=1300,
        )

    reply_initial = _frame("d1-card-observation-v1", reply_attempt, "initial")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=reply_attempt,
        observation_public_id=reply_initial,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1101,
    )
    observed = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=reply_attempt)
    assert observed.delivery_state == "unknown"
    assert [row["attempt_public_id"] for row in observed.delivery_attempts] == [
        replace_attempt,
        reply_attempt,
    ]
    assert [row["observation_public_id"] for row in observed.delivery_outcomes] == [
        replace_initial,
        replace_resolution,
        reply_initial,
    ]
    conn.close()


def test_delivery_rejects_preexisting_cross_context_attempt_defensively() -> None:
    """Repository reads/writes must fail closed even for a legacy-corrupt attempt row."""
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        INSERT INTO parser_human_draft_card_delivery_attempts (
            attempt_public_id, card_generation_public_id, delivery_identity,
            delivery_material_hash, authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id, transport_mode,
            outbound_target_message_id, attempted_at
        ) VALUES (?, ?, ?, ?, 'attacker', 'wrong', 'wrong', 'wrong', 'reply', NULL, 1001)
        """,
        ("a" * 64, generation, "b" * 64, "c" * 64),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(HumanDraftError, match="attempt_context"):
        get_human_draft_card(conn, context=CONTEXT, attempt_public_id="a" * 64)
    with pytest.raises(HumanDraftError, match="attempt_context"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id="a" * 64,
            observation_public_id=_frame("d1-card-observation-v1", "a" * 64, "initial"),
            outcome="failure",
            error_code="transport_failed",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1002,
        )
    conn.close()


def test_delivery_schema_rejects_resolution_earlier_than_initial() -> None:
    """Direct SQL cannot make wall-clock order contradict causal observation slots."""
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="e" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    conn.execute(
        """
        INSERT INTO parser_human_draft_card_delivery_outcomes (
            observation_public_id, attempt_public_id, observation_slot, outcome,
            error_code, outbound_message_id, trusted_receipt_hash, observed_at
        ) VALUES (?, ?, 'initial', 'unknown', NULL, NULL, NULL, 1200)
        """,
        (_frame("d1-card-observation-v1", attempt_id, "initial"), attempt_id),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="delivery resolution time"):
        conn.execute(
            """
            INSERT INTO parser_human_draft_card_delivery_outcomes (
                observation_public_id, attempt_public_id, observation_slot, outcome,
                error_code, outbound_message_id, trusted_receipt_hash, observed_at
            ) VALUES (?, ?, 'resolution', 'failure', 'transport_failed', NULL, NULL, 1100)
            """,
            (_frame("d1-card-observation-v1", attempt_id, "resolution"), attempt_id),
        )
    conn.rollback()
    conn.close()


def test_delivery_repository_rejects_attempt_before_card_issue() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")

    with pytest.raises(HumanDraftError, match="attempt_time_regression"):
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation,
            attempt_public_id=attempt_id,
            delivery_material_hash="1" * 64,
            transport_mode="reply",
            outbound_target_message_id=None,
            now_epoch=999,
        )

    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_card_delivery_attempts").fetchone()[0]
        == 0
    )
    conn.close()


def test_delivery_repository_rejects_outcome_before_attempt() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="2" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1100,
    )

    with pytest.raises(HumanDraftError, match="observation_time_regression"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=_frame("d1-card-observation-v1", attempt_id, "initial"),
            outcome="failure",
            error_code="transport_failed",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1099,
        )

    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_card_delivery_outcomes").fetchone()[0]
        == 0
    )
    conn.close()


def test_delivery_repository_rejects_successor_before_consumed_evidence() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation = started.card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="3" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1100,
    )
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=attempt_id,
        observation_public_id=_frame("d1-card-observation-v1", attempt_id, "initial"),
        outcome="failure",
        error_code="transport_failed",
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1200,
    )
    queried = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_id)
    recovery_id = _frame(
        "d1-card-recovery-v1", queried.draft_public_id, "d1start_callback_a", generation
    )

    with pytest.raises(HumanDraftError, match="recovery_time_regression"):
        reissue_human_draft_card(
            conn,
            context=CONTEXT,
            expected_current_generation_public_id=generation,
            original_operation_or_start_public_id="d1start_callback_a",
            recovery_public_id=recovery_id,
            recovery_material_hash="4" * 64,
            queried_delivery_state_hash=queried.delivery_state_hash,
            reason="failure",
            now_epoch=1100,
        )

    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT current_card_generation_public_id FROM parser_human_drafts"
        ).fetchone()[0]
        == generation
    )
    conn.close()


def test_delivery_schema_rejects_attempt_before_card_issue() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id

    with pytest.raises(sqlite3.IntegrityError, match="delivery attempt time precedes card issue"):
        conn.execute(
            """
            INSERT INTO parser_human_draft_card_delivery_attempts (
                attempt_public_id, card_generation_public_id, delivery_identity,
                delivery_material_hash, authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, transport_mode,
                outbound_target_message_id, attempted_at
            ) VALUES (?, ?, ?, ?, '111', 'acct', '111', 'binding', 'reply', NULL, 999)
            """,
            ("1" * 64, generation, "2" * 64, "3" * 64),
        )

    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_card_delivery_attempts").fetchone()[0]
        == 0
    )
    conn.close()


def test_delivery_schema_rejects_outcome_before_attempt() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="5" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1100,
    )

    with pytest.raises(sqlite3.IntegrityError, match="delivery observation time precedes attempt"):
        conn.execute(
            """
            INSERT INTO parser_human_draft_card_delivery_outcomes (
                observation_public_id, attempt_public_id, observation_slot, outcome,
                error_code, outbound_message_id, trusted_receipt_hash, observed_at
            ) VALUES (?, ?, 'initial', 'failure', 'transport_failed', NULL, NULL, 1099)
            """,
            ("6" * 64, attempt_id),
        )

    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_card_delivery_outcomes").fetchone()[0]
        == 0
    )
    conn.close()


def test_delivery_schema_rejects_successor_before_consumed_evidence() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="7" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1100,
    )
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=attempt_id,
        observation_public_id=_frame("d1-card-observation-v1", attempt_id, "initial"),
        outcome="failure",
        error_code="transport_failed",
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1200,
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="recovery card time precedes delivery evidence"
    ):
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
            )
            SELECT ?, draft_id, draft_version, draft_content_hash, field_values_json,
                   parser_output_id, proposal_version, proposal_content_hash,
                   decision_target_parser_output_id, decision_target_proposal_version,
                   decision_target_proposal_content_hash, language, format_version, ?, id,
                   original_operation_id, ?, ?, ?, 'failure', authenticated_actor_id,
                   telegram_account_id, telegram_conversation_id, conversation_binding_id,
                   min(
                       (SELECT expires_at FROM parser_human_drafts
                        WHERE id = parser_human_draft_cards.draft_id),
                       1400
                   ), 1100
            FROM parser_human_draft_cards
            WHERE card_generation_public_id = ?
            """,
            (
                "d1card_" + "8" * 32,
                "9" * 64,
                "a" * 64,
                "b" * 64,
                "c" * 64,
                generation,
            ),
        )

    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    conn.close()


def test_unknown_delivery_reissue_is_one_successor_and_exact_replay() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation = started.card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="5" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    observed = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_id)
    recovery_id = _frame(
        "d1-card-recovery-v1",
        observed.draft_public_id,
        "d1start_callback_a",
        generation,
    )
    recovered = reissue_human_draft_card(
        conn,
        context=CONTEXT,
        expected_current_generation_public_id=generation,
        original_operation_or_start_public_id="d1start_callback_a",
        recovery_public_id=recovery_id,
        recovery_material_hash="6" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )
    assert recovered.card_generation_public_id != generation
    assert recovered.current_card_generation_public_id == recovered.card_generation_public_id
    replay = reissue_human_draft_card(
        conn,
        context=CONTEXT,
        expected_current_generation_public_id=generation,
        original_operation_or_start_public_id="d1start_callback_a",
        recovery_public_id=recovery_id,
        recovery_material_hash="6" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )
    assert replay.card_generation_public_id == recovered.card_generation_public_id
    assert replay.idempotent_replay
    assert (
        get_human_draft_card(
            conn, context=CONTEXT, card_generation_public_id=generation
        ).current_card_generation_public_id
        == recovered.card_generation_public_id
    )
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 2
    conn.close()


def test_exact_attempt_and_outcome_replay_survive_terminal_head_and_later_clock() -> None:
    from finance_core.parser_proposals.human_drafts import reject_active_human_draft_in_transaction

    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation = started.card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="7" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    observation_id = _frame("d1-card-observation-v1", attempt_id, "initial")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=attempt_id,
        observation_public_id=observation_id,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1002,
    )
    proposal_id = conn.execute(
        "SELECT decision_target_parser_output_id FROM parser_human_drafts"
    ).fetchone()[0]
    _insert_decision(
        conn,
        decision_public_id="decision-terminal-1",
        state="rejected",
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=proposal_id,
        authenticated_actor_id="111",
        decision_public_id="decision-terminal-1",
        decision_binding=None,
        now_epoch=1100,
    )
    conn.commit()
    assert (
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation,
            attempt_public_id=attempt_id,
            delivery_material_hash="7" * 64,
            transport_mode="reply",
            outbound_target_message_id=None,
            now_epoch=1999,
        )
        == attempt_id
    )
    assert (
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=observation_id,
            outcome="unknown",
            error_code=None,
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1999,
        )
        == observation_id
    )
    with pytest.raises(HumanDraftError, match="attempt_conflict"):
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation,
            attempt_public_id=attempt_id,
            delivery_material_hash="8" * 64,
            transport_mode="reply",
            outbound_target_message_id=None,
            now_epoch=1999,
        )
    conn.close()


def test_controlled_persisted_success_is_read_only_projection() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    generation = _start(conn).card_generation_public_id
    attempt_id = _frame("d1-card-delivery-v1", generation, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation,
        attempt_public_id=attempt_id,
        delivery_material_hash="9" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    observation_id = _frame("d1-card-observation-v1", attempt_id, "initial")
    conn.execute(
        """
        INSERT INTO parser_human_draft_card_delivery_outcomes (
            observation_public_id, attempt_public_id, observation_slot, outcome,
            outbound_message_id, trusted_receipt_hash, observed_at
        ) VALUES (?, ?, 'initial', 'success', 'controlled-fixture', ?, 1002)
        """,
        (observation_id, attempt_id, "a" * 64),
    )
    conn.commit()
    result = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_id)
    assert result.delivery_state == "success"
    with pytest.raises(HumanDraftError, match="trusted_receipt_unverifiable"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_id,
            observation_public_id=observation_id,
            outcome="success",
            error_code=None,
            outbound_message_id="controlled-fixture",
            trusted_receipt_hash="a" * 64,
            now_epoch=1002,
        )
    conn.close()


def test_late_g1_resolution_stays_historical_after_g2_reissue() -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation_1 = started.card_generation_public_id
    attempt_1 = _frame("d1-card-delivery-v1", generation_1, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation_1,
        attempt_public_id=attempt_1,
        delivery_material_hash="b" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    initial_observation = _frame("d1-card-observation-v1", attempt_1, "initial")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=attempt_1,
        observation_public_id=initial_observation,
        outcome="unknown",
        error_code=None,
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1002,
    )
    observed = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_1)
    recovery_id = _frame(
        "d1-card-recovery-v1", observed.draft_public_id, "d1start_callback_a", generation_1
    )
    generation_2 = reissue_human_draft_card(
        conn,
        context=CONTEXT,
        expected_current_generation_public_id=generation_1,
        original_operation_or_start_public_id="d1start_callback_a",
        recovery_public_id=recovery_id,
        recovery_material_hash="c" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )
    resolution = _frame("d1-card-observation-v1", attempt_1, "resolution")
    record_human_draft_card_delivery_outcome(
        conn,
        context=CONTEXT,
        attempt_public_id=attempt_1,
        observation_public_id=resolution,
        outcome="failure",
        error_code="late_transport_failure",
        outbound_message_id=None,
        trusted_receipt_hash=None,
        now_epoch=1200,
    )
    historical = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_1)
    current = get_human_draft_card(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation_2.card_generation_public_id,
    )
    assert historical.delivery_state == "failure"
    assert len(historical.delivery_outcomes) == 2
    assert current.current_card_generation_public_id == generation_2.card_generation_public_id
    assert current.delivery_state == "not_attempted"
    third = _frame("d1-card-observation-v1", attempt_1, "third")
    with pytest.raises(HumanDraftError, match="observation_identity|observation_limit"):
        record_human_draft_card_delivery_outcome(
            conn,
            context=CONTEXT,
            attempt_public_id=attempt_1,
            observation_public_id=third,
            outcome="failure",
            error_code="third",
            outbound_message_id=None,
            trusted_receipt_hash=None,
            now_epoch=1300,
        )
    conn.close()


def _create_durable_issuer_stub(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE issuer_stub_batches (
            action_issue_batch_id TEXT PRIMARY KEY,
            card_generation_public_id TEXT NOT NULL UNIQUE,
            result_hash TEXT NOT NULL
        ) STRICT;
        CREATE TABLE issuer_stub_references (
            reference_public_id TEXT PRIMARY KEY,
            action_issue_batch_id TEXT NOT NULL,
            FOREIGN KEY (action_issue_batch_id)
                REFERENCES issuer_stub_batches(action_issue_batch_id)
        ) STRICT;
        CREATE TABLE issuer_stub_bindings (
            action_issue_batch_id TEXT PRIMARY KEY,
            reference_public_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY (action_issue_batch_id)
                REFERENCES issuer_stub_batches(action_issue_batch_id),
            FOREIGN KEY (reference_public_id)
                REFERENCES issuer_stub_references(reference_public_id)
        ) STRICT;
        """
    )
    conn.commit()


def _issue_stub_batch(
    conn: sqlite3.Connection,
    *,
    card_generation_public_id: str,
    action_issue_batch_id: str,
    fail_during: bool = False,
) -> tuple[str, str]:
    result_hash = _frame(
        "d1-test-issuer-result-v1", card_generation_public_id, action_issue_batch_id
    )
    reference_public_id = _frame("d1-test-issuer-reference-v1", action_issue_batch_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """
            SELECT batches.result_hash, bindings.reference_public_id
            FROM issuer_stub_batches AS batches
            JOIN issuer_stub_bindings AS bindings
              ON bindings.action_issue_batch_id = batches.action_issue_batch_id
            WHERE batches.action_issue_batch_id = ?
              AND batches.card_generation_public_id = ?
            """,
            (action_issue_batch_id, card_generation_public_id),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return str(existing["result_hash"]), str(existing["reference_public_id"])
        conn.execute(
            "INSERT INTO issuer_stub_batches VALUES (?, ?, ?)",
            (action_issue_batch_id, card_generation_public_id, result_hash),
        )
        conn.execute(
            "INSERT INTO issuer_stub_references VALUES (?, ?)",
            (reference_public_id, action_issue_batch_id),
        )
        if fail_during:
            raise RuntimeError("injected-during-action-issuance")
        conn.execute(
            "INSERT INTO issuer_stub_bindings VALUES (?, ?)",
            (action_issue_batch_id, reference_public_id),
        )
        conn.commit()
        return result_hash, reference_public_id
    except Exception:
        conn.rollback()
        raise


@pytest.mark.parametrize("crash_boundary", ["before", "during", "after"])
def test_g2_durable_issuer_stub_recovers_exact_batch_at_every_crash_boundary(
    tmp_path: Path,
    crash_boundary: str,
) -> None:
    """Replacing the durable stub with a disconnected callback loses crash evidence."""
    path = tmp_path / f"issuer-{crash_boundary}.db"
    conn = _connection(path)
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    _create_durable_issuer_stub(conn)
    started = _start(conn)
    generation_1 = started.card_generation_public_id
    attempt_1 = _frame("d1-card-delivery-v1", generation_1, "reply")
    begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation_1,
        attempt_public_id=attempt_1,
        delivery_material_hash="d" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    observed = get_human_draft_card(conn, context=CONTEXT, attempt_public_id=attempt_1)
    recovery_id = _frame(
        "d1-card-recovery-v1", observed.draft_public_id, "d1start_callback_a", generation_1
    )
    generation_2 = reissue_human_draft_card(
        conn,
        context=CONTEXT,
        expected_current_generation_public_id=generation_1,
        original_operation_or_start_public_id="d1start_callback_a",
        recovery_public_id=recovery_id,
        recovery_material_hash="e" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )
    baseline = conn.execute(
        """
        SELECT current_draft_version, current_draft_content_hash,
               current_parser_output_id, current_proposal_version,
               current_proposal_content_hash, last_claimed_message_id,
               current_card_generation_public_id
        FROM parser_human_drafts
        """
    ).fetchone()
    baseline_publications = conn.execute(
        "SELECT COUNT(*) FROM parser_human_draft_publications"
    ).fetchone()[0]
    baseline_real_references = conn.execute(
        "SELECT COUNT(*) FROM openclaw_human_action_references"
    ).fetchone()[0]
    baseline_real_bindings = conn.execute(
        "SELECT COUNT(*) FROM parser_human_draft_action_bindings"
    ).fetchone()[0]

    if crash_boundary == "during":
        with pytest.raises(RuntimeError, match="injected-during-action-issuance"):
            _issue_stub_batch(
                conn,
                card_generation_public_id=generation_2.card_generation_public_id,
                action_issue_batch_id=generation_2.action_issue_batch_id,
                fail_during=True,
            )
    elif crash_boundary == "after":
        _issue_stub_batch(
            conn,
            card_generation_public_id=generation_2.card_generation_public_id,
            action_issue_batch_id=generation_2.action_issue_batch_id,
        )
        with pytest.raises(RuntimeError, match="injected-after-action-issuance-commit"):
            raise RuntimeError("injected-after-action-issuance-commit")
    conn.close()

    restarted = _connection(path)
    resumed = get_human_draft_card(
        restarted,
        context=CONTEXT,
        card_generation_public_id=generation_2.card_generation_public_id,
    )
    if crash_boundary in {"before", "during"}:
        assert restarted.execute("SELECT COUNT(*) FROM issuer_stub_batches").fetchone()[0] == 0
        assert restarted.execute("SELECT COUNT(*) FROM issuer_stub_references").fetchone()[0] == 0
        assert restarted.execute("SELECT COUNT(*) FROM issuer_stub_bindings").fetchone()[0] == 0
    first = _issue_stub_batch(
        restarted,
        card_generation_public_id=resumed.card_generation_public_id,
        action_issue_batch_id=resumed.action_issue_batch_id,
    )
    replay = _issue_stub_batch(
        restarted,
        card_generation_public_id=resumed.card_generation_public_id,
        action_issue_batch_id=resumed.action_issue_batch_id,
    )
    assert first == replay
    assert restarted.execute("SELECT COUNT(*) FROM issuer_stub_batches").fetchone()[0] == 1
    assert restarted.execute("SELECT COUNT(*) FROM issuer_stub_references").fetchone()[0] == 1
    assert restarted.execute("SELECT COUNT(*) FROM issuer_stub_bindings").fetchone()[0] == 1
    assert resumed.action_issuance_state == "not_issued"
    assert restarted.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 2
    assert (
        restarted.execute(
            "SELECT COUNT(*) FROM parser_human_draft_cards WHERE predecessor_card_id IS NOT NULL"
        ).fetchone()[0]
        == 1
    )
    after = restarted.execute(
        """
        SELECT current_draft_version, current_draft_content_hash,
               current_parser_output_id, current_proposal_version,
               current_proposal_content_hash, last_claimed_message_id,
               current_card_generation_public_id
        FROM parser_human_drafts
        """
    ).fetchone()
    assert tuple(after) == tuple(baseline)
    assert (
        restarted.execute("SELECT COUNT(*) FROM parser_human_draft_publications").fetchone()[0]
        == baseline_publications
    )
    assert (
        restarted.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0]
        == baseline_real_references
    )
    assert (
        restarted.execute("SELECT COUNT(*) FROM parser_human_draft_action_bindings").fetchone()[0]
        == baseline_real_bindings
    )
    assert resumed.card_generation_public_id == generation_2.card_generation_public_id
    assert resumed.action_issue_batch_id == generation_2.action_issue_batch_id
    assert resumed.current_card_generation_public_id == generation_2.card_generation_public_id
    assert resumed.draft_version == started.draft_version
    restarted.close()


def test_two_attempt_slots_are_bounded_and_terminal_head_invalidates_all_generations() -> None:
    from finance_core.parser_proposals.human_drafts import reject_active_human_draft_in_transaction

    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    started = _start(conn)
    generation_1 = started.card_generation_public_id
    for mode, material, target in (
        ("reply", "1" * 64, None),
        ("replace", "2" * 64, "old-message"),
    ):
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation_1,
            attempt_public_id=_frame("d1-card-delivery-v1", generation_1, mode),
            delivery_material_hash=material,
            transport_mode=mode,
            outbound_target_message_id=target,
            now_epoch=1001,
        )
    with pytest.raises(HumanDraftError, match="attempt_conflict"):
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation_1,
            attempt_public_id=_frame("d1-card-delivery-v1", generation_1, "reply"),
            delivery_material_hash="3" * 64,
            transport_mode="reply",
            outbound_target_message_id=None,
            now_epoch=1002,
        )
    observed = get_human_draft_card(conn, context=CONTEXT, card_generation_public_id=generation_1)
    recovery_id = _frame(
        "d1-card-recovery-v1", observed.draft_public_id, "d1start_callback_a", generation_1
    )
    generation_2 = reissue_human_draft_card(
        conn,
        context=CONTEXT,
        expected_current_generation_public_id=generation_1,
        original_operation_or_start_public_id="d1start_callback_a",
        recovery_public_id=recovery_id,
        recovery_material_hash="4" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    ).card_generation_public_id
    proposal_id = conn.execute(
        "SELECT decision_target_parser_output_id FROM parser_human_drafts"
    ).fetchone()[0]
    high_water_before = conn.execute(
        "SELECT last_claimed_message_id FROM parser_human_drafts"
    ).fetchone()[0]
    _insert_decision(
        conn,
        decision_public_id="decision-terminal-all-generations",
        state="rejected",
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=proposal_id,
        authenticated_actor_id="111",
        decision_public_id="decision-terminal-all-generations",
        decision_binding=None,
        now_epoch=1200,
    )
    conn.commit()
    assert begin_human_draft_card_delivery(
        conn,
        context=CONTEXT,
        card_generation_public_id=generation_1,
        attempt_public_id=_frame("d1-card-delivery-v1", generation_1, "reply"),
        delivery_material_hash="1" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1201,
    )
    with pytest.raises(HumanDraftError, match="draft_terminal"):
        begin_human_draft_card_delivery(
            conn,
            context=CONTEXT,
            card_generation_public_id=generation_2,
            attempt_public_id=_frame("d1-card-delivery-v1", generation_2, "reply"),
            delivery_material_hash="5" * 64,
            transport_mode="reply",
            outbound_target_message_id=None,
            now_epoch=1201,
        )
    with pytest.raises(HumanDraftError, match="draft_terminal"):
        reissue_human_draft_card(
            conn,
            context=CONTEXT,
            expected_current_generation_public_id=generation_1,
            original_operation_or_start_public_id="d1start_callback_a",
            recovery_public_id=recovery_id,
            recovery_material_hash="4" * 64,
            queried_delivery_state_hash=observed.delivery_state_hash,
            reason="unknown_after_query",
            now_epoch=1201,
        )
    high_water_after = conn.execute(
        "SELECT last_claimed_message_id FROM parser_human_drafts"
    ).fetchone()[0]
    assert high_water_after == high_water_before
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 2
    conn.close()
