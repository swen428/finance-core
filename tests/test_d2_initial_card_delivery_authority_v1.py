"""D2 migration-050 initial-card and terminal-delivery authority tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from finance_core import posting_authority as posting_authority_module
from finance_core.openclaw_staging_bridge.human_actions import (
    HumanActionContext,
    HumanActionReferenceError,
    redeem_human_action_reference,
)
from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.human_drafts import (
    HumanDraftCommand,
    HumanDraftDecisionBinding,
    apply_human_draft_card,
    begin_human_draft_in_transaction,
)
from finance_core.parser_proposals.human_revision import publish_human_revision_in_transaction
from finance_core.parser_proposals.service import confirm_parser_proposal
from finance_core.posting_authority import (
    PostingAuthorityError,
    PostingReviewControl,
    begin_posting_review_delivery,
    confirm_and_post,
    finance_delivery_material_digest,
    get_status,
    prepare_posting_review,
    record_posting_review_delivery,
    replace_posting_review_delivery,
    resume_posting,
)
from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    MigrationExecutionError,
    apply_migration_paths,
)
from tests.test_parser_human_drafts_v1 import _complete_validator, _start
from tests.test_receipt_facts_conversion_v1 import seed_people, seed_receipt_proposal


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    return conn


def _seed_initial_text(conn: sqlite3.Connection) -> str:
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
    conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, parser_name, parser_version,
            raw_text, parsed_payload, parse_status
        ) VALUES ('prop_d2_initial_text', 'text', 'intake_d2_initial_text',
                  'test', '1', 'lunch 12.50', ?, 'parsed_pending_confirmation')
        """,
        (json.dumps(payload),),
    )
    parser_output_id = int(
        conn.execute(
            "SELECT id FROM parser_outputs WHERE public_id = 'prop_d2_initial_text'"
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, source_channel, raw_input, received_at,
            source_received_at, external_source_id, source_message_id,
            idempotency_key, source_content_hash, status, parser_output_id
        ) VALUES (
            'intake_d2_initial_text', 'telegram_text', 'telegram', 'lunch 12.50',
            '2026-09-21T00:00:00Z', '2026-09-21T00:00:00Z',
            'telegram:111:77', '77', 'raw-intake:telegram:111:77', ?,
            'parsed_pending_confirmation', ?
        )
        """,
        (hashlib.sha256(b"lunch 12.50").hexdigest(), parser_output_id),
    )
    conn.commit()
    return "prop_d2_initial_text"


def _migration_049_d1_review(
    monkeypatch: pytest.MonkeyPatch,
    *,
    redeemed: bool,
) -> tuple[sqlite3.Connection, str, int]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:49])
    started = _start(
        conn,
        payload={
            "intent": "personal_expense_log",
            "transaction_type": "personal_expense",
            "amount": "12.50",
            "currency": "SGD",
            "transaction_date": "2026-09-21",
            "merchant": "Kopitiam",
            "description": "Lunch",
            "category": "food",
        },
    )
    fields = {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-21",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    published = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_d2_migration_050",
            "111",
            "acct",
            "111",
            "binding",
            (
                f"资料卡编号：{started.card_generation_public_id}\r\n"
                "金额: 12.50\r\n币种：SGD\r\n日期: 2026-09-21\r\n"
                "商户: Cafe\r\n描述: Lunch\r\n分类: food"
            ),
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    proposal = conn.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (published.decision_target_proposal_public_id,),
    ).fetchone()
    parser_output_id = int(proposal["id"])
    reference_public_id = "haref_" + "9" * 32
    reference_id = int(
        conn.execute(
            """
            INSERT INTO openclaw_human_action_references (
                reference_public_id, reference_sha256, issuance_idempotency_key,
                parser_output_id, action, proposal_version, proposal_content_hash,
                authenticated_actor_id, channel, channel_account_id,
                channel_conversation_id, conversation_binding_id, ttl_seconds,
                expires_at, issued_at
            ) VALUES (?, ?, ?, ?, 'confirm', ?, ?, '111', 'telegram', 'acct',
                      '111', 'binding', 600, 2000, ?)
            """,
            (
                reference_public_id,
                hashlib.sha256(b"migration-050-reference").hexdigest(),
                "bridge-human-action-issue:" + "9" * 32,
                parser_output_id,
                published.decision_target_proposal_version,
                published.decision_target_proposal_content_hash,
                "1970-01-01T00:16:42+00:00",
            ),
        ).lastrowid
    )
    card = conn.execute(
        "SELECT cards.draft_id FROM parser_human_draft_cards AS cards "
        "WHERE cards.card_generation_public_id = ?",
        (published.card_generation_public_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO parser_human_draft_action_bindings (
            reference_id, card_generation_public_id, draft_id, parser_output_id,
            proposal_version, proposal_content_hash, authenticated_actor_id,
            telegram_account_id, telegram_conversation_id,
            conversation_binding_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, '111', 'acct', '111', 'binding', 1002)
        """,
        (
            reference_id,
            published.card_generation_public_id,
            card["draft_id"],
            parser_output_id,
            published.decision_target_proposal_version,
            published.decision_target_proposal_content_hash,
        ),
    )
    review_public_id = "d2rev_" + "8" * 30
    projection = json.dumps(
        {
            "account": "unspecified",
            "amount": "12.50",
            "currency": "SGD",
            "merchant": "Kopitiam",
            "transaction_date": "2026-09-21",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO d2_posting_reviews (
            review_public_id, review_idempotency_key, card_generation_public_id,
            parser_output_id, proposal_version, proposal_content_hash, posting_path,
            authenticated_actor_id, telegram_account_id, telegram_conversation_id,
            conversation_binding_id, visible_projection_json, visible_projection_hash,
            receipt_fact_candidate_json, expires_at, created_at
        ) VALUES (?, 'migration-050-review', ?, ?, ?, ?, 'text', '111', 'acct',
                  '111', 'binding', ?, ?, NULL, 2000, ?)
        """,
        (
            review_public_id,
            published.card_generation_public_id,
            parser_output_id,
            published.decision_target_proposal_version,
            published.decision_target_proposal_content_hash,
            projection,
            hashlib.sha256(projection.encode()).hexdigest(),
            "1970-01-01T00:16:42+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO d2_posting_review_action_bindings "
        "(review_public_id, reference_id, bound_at) VALUES (?, ?, ?)",
        (review_public_id, reference_id, "1970-01-01T00:16:42+00:00"),
    )
    if redeemed:
        conn.execute(
            "INSERT INTO openclaw_human_action_redemptions "
            "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
            "VALUES (?, ?, 700, ?)",
            (
                reference_id,
                hashlib.sha256(b"migration-050-callback").hexdigest(),
                "1970-01-01T00:16:44+00:00",
            ),
        )
        confirmation_id = "pca_d2_migration_050"
        confirm_parser_proposal(
            conn,
            parser_output_id,
            authenticated_actor_id="111",
            decision="confirmed",
            confirmation_channel="telegram",
            confirmation_public_id=confirmation_id,
            expected_content_hash=published.decision_target_proposal_content_hash,
            expected_version=published.decision_target_proposal_version,
            d1_decision_binding=HumanDraftDecisionBinding(
                reference_public_id=reference_public_id,
                card_generation_public_id=published.card_generation_public_id,
                authenticated_actor_id="111",
                telegram_account_id="acct",
                telegram_conversation_id="111",
                conversation_binding_id="binding",
            ),
            clock=lambda: "1970-01-01T00:16:44+00:00",
            _caller_owns_transaction=True,
        )
        attempt_id = "d2att_" + "7" * 30
        conn.execute(
            "INSERT INTO d2_posting_attempts "
            "(attempt_public_id, review_public_id, reference_id, posting_path, stage, "
            "stage_evidence_public_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'text', 'accepted', ?, ?, ?)",
            (
                attempt_id,
                review_public_id,
                reference_id,
                confirmation_id,
                "1970-01-01T00:16:44+00:00",
                "1970-01-01T00:16:44+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO d2_posting_decisions "
            "(decision_public_id, review_public_id, attempt_public_id, reference_id, "
            "confirmation_public_id, accepted_at, created_at) VALUES (?, ?, ?, ?, ?, 1004, ?)",
            (
                "d2dec_" + "6" * 30,
                review_public_id,
                attempt_id,
                reference_id,
                confirmation_id,
                "1970-01-01T00:16:44+00:00",
            ),
        )
    conn.commit()
    return conn, review_public_id, reference_id


def _manifest(conn: sqlite3.Connection):
    context = HumanActionContext("111", "acct", "111", "binding")
    proposal_public_id = _seed_initial_text(conn)
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-initial-text-review",
        proposal_public_id=proposal_public_id,
        admitted_source_message_id="77",
        context=context,
        clock=lambda: 1000,
    )
    manifest = begin_posting_review_delivery(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-initial-text-key",
        context=context,
        clock=lambda: 1001,
    )
    confirm = next(control for control in manifest.controls if control.action == "confirm")
    return context, review, manifest, confirm.callback_value.removeprefix("post:")


def _record_delivery(
    conn: sqlite3.Connection,
    *,
    manifest: object,
    context: HumanActionContext,
    provider_message_id: int,
    material_sha256: str | None = None,
    attempt_nonce: str | None = None,
    receipt_token_sha256: str | None = None,
    source_identity_sha256: str = "b" * 64,
    capability: str = "telegram.finance-delivery-material-v1",
    delivery_material_version: str = "finance_d2_delivery_material_v1",
    channel: str = "telegram",
    account_id: str | None = None,
    conversation_id: str | None = None,
    session_key: str | None = None,
    now: int,
) -> str:
    delivery_attempt_public_id = str(getattr(manifest, "delivery_attempt_public_id"))
    return record_posting_review_delivery(
        conn,
        attempt_nonce=(
            str(getattr(manifest, "delivery_attempt_nonce"))
            if attempt_nonce is None
            else attempt_nonce
        ),
        capability=capability,
        delivery_material_version=delivery_material_version,
        finance_delivery_material_sha256=(
            str(getattr(manifest, "finance_delivery_material_sha256"))
            if material_sha256 is None
            else material_sha256
        ),
        provider_message_id=provider_message_id,
        receipt_token_sha256=(
            hashlib.sha256(
                f"receipt:{delivery_attempt_public_id}:{provider_message_id}".encode()
            ).hexdigest()
            if receipt_token_sha256 is None
            else receipt_token_sha256
        ),
        channel=channel,
        account_id=context.account_id if account_id is None else account_id,
        conversation_id=(
            context.conversation_id if conversation_id is None else conversation_id
        ),
        session_key=context.binding_id if session_key is None else session_key,
        source_identity_sha256=source_identity_sha256,
        clock=lambda: now,
    )


def test_delivery_material_shared_golden_vector() -> None:
    controls = (
        PostingReviewControl("confirm", "Confirm", 0, 0, "post:d2act_confirm"),
        PostingReviewControl("edit", "Edit", 1, 0, "edit:d1act_edit"),
        PostingReviewControl("reject", "Reject", 1, 1, "reject:d1act_reject"),
    )
    assert finance_delivery_material_digest("D2 test €", controls) == (
        "33133daf947b4c35586040a8d9aa702ffe2da8f7336723ab6e2c481bab9cd290"
    )
    mutated = controls[:2] + (
        PostingReviewControl("reject", "Reject!", 1, 1, "reject:d1act_reject"),
    )
    assert finance_delivery_material_digest("D2 test €", mutated) != (
        "33133daf947b4c35586040a8d9aa702ffe2da8f7336723ab6e2c481bab9cd290"
    )


def test_initial_review_replay_keeps_the_original_expiry_and_identity() -> None:
    conn = _connection()
    context = HumanActionContext("111", "acct", "111", "binding")
    proposal_public_id = _seed_initial_text(conn)
    first = prepare_posting_review(
        conn,
        review_idempotency_key="d2-initial-replay",
        proposal_public_id=proposal_public_id,
        admitted_source_message_id="77",
        context=context,
        clock=lambda: 1000,
    )
    replay = prepare_posting_review(
        conn,
        review_idempotency_key="d2-initial-replay",
        proposal_public_id=proposal_public_id,
        admitted_source_message_id="77",
        context=context,
        clock=lambda: 1010,
    )
    assert replay.idempotent is True
    assert replay.review_public_id == first.review_public_id
    assert replay.initial_card_public_id == first.initial_card_public_id
    assert replay.expires_at == first.expires_at


def test_initial_text_requires_terminal_activation_and_posts_once_without_d1_edit() -> None:
    conn = _connection()
    context, review, manifest, reference = _manifest(conn)
    assert review.source_kind == "initial_proposal_card"
    assert review.card_generation_public_id is None
    assert review.initial_card_public_id is not None
    assert "Amount: 12.50" in manifest.text
    assert "Description: Lunch" in manifest.text
    assert [control.action for control in manifest.controls] == ["confirm", "edit", "reject"]
    assert get_status(
        conn, review_public_id=review.review_public_id, context=context
    ).attention_reason == "delivery_not_activated"

    with pytest.raises(PostingAuthorityError, match="not bound"):
        confirm_and_post(
            conn,
            key=b"d2-initial-text-key",
            reference=reference,
            context=context,
            callback_id="initial-before-delivery",
            callback_message_id=900,
            clock=lambda: 1002,
        )
    assert conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=900,
        context=context,
        now=1002,
    )
    assert get_status(conn, review_public_id=review.review_public_id, context=context).state == (
        "awaiting_confirmation"
    )
    with pytest.raises(PostingAuthorityError, match="activated provider message"):
        confirm_and_post(
            conn,
            key=b"d2-initial-text-key",
            reference=reference,
            context=context,
            callback_id="initial-wrong-message",
            callback_message_id=901,
            clock=lambda: 1003,
        )
    assert conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0] == 0

    result = confirm_and_post(
        conn,
        key=b"d2-initial-text-key",
        reference=reference,
        context=context,
        callback_id="initial-confirm",
        callback_message_id=900,
        clock=lambda: 1004,
    )
    assert result.state == "finalized"
    assert result.transaction_public_id is not None
    replay = confirm_and_post(
        conn,
        key=b"d2-initial-text-key",
        reference=reference,
        context=context,
        callback_id="initial-confirm",
        callback_message_id=900,
        clock=lambda: 9999,
    )
    assert replay == result
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 0


def test_delivery_receipt_and_generic_confirm_are_single_purpose_fail_closed() -> None:
    conn = _connection()
    context, review, manifest, reference = _manifest(conn)
    with pytest.raises(PostingAuthorityError, match="does not match"):
        _record_delivery(
            conn,
            manifest=manifest,
            material_sha256="0" * 64,
            provider_message_id=910,
            context=context,
            now=1002,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM d2_posting_review_delivery_activations"
    ).fetchone()[0] == 0
    _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=910,
        context=context,
        now=1002,
    )
    with pytest.raises(PostingAuthorityError, match="already consumed"):
        _record_delivery(
            conn,
            manifest=manifest,
            provider_message_id=911,
            receipt_token_sha256=hashlib.sha256(
                f"receipt:{manifest.delivery_attempt_public_id}:910".encode()
            ).hexdigest(),
            context=context,
            now=1003,
        )
    with pytest.raises(HumanActionReferenceError, match="requires_d2_redemption"):
        redeem_human_action_reference(
            conn,
            key=b"d2-initial-text-key",
            reference=reference,
            action="confirm",
            context=context,
            callback_id="generic-confirm",
            callback_message_id=910,
            clock=lambda: 1004,
        )
    assert conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert get_status(conn, review_public_id=review.review_public_id, context=context).state == (
        "awaiting_confirmation"
    )


def test_host_receipt_replay_and_duplicate_delivery_conflict_fail_closed() -> None:
    conn = _connection()
    context, review, manifest, reference = _manifest(conn)
    with pytest.raises(PostingAuthorityError, match="capability is invalid"):
        _record_delivery(
            conn,
            manifest=manifest,
            provider_message_id=912,
            capability="telegram.message-sent-v1",
            context=context,
            now=1002,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM d2_posting_review_delivery_activations"
    ).fetchone()[0] == 0

    first_observation = _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=912,
        context=context,
        now=1003,
    )
    replay_observation = _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=912,
        receipt_token_sha256=hashlib.sha256(b"same-message-new-host-receipt").hexdigest(),
        context=context,
        now=1004,
    )
    assert replay_observation == first_observation
    assert conn.execute(
        "SELECT COUNT(*) FROM d2_posting_review_delivery_observations"
    ).fetchone()[0] == 1

    with pytest.raises(PostingAuthorityError, match="multiple provider messages conflict"):
        _record_delivery(
            conn,
            manifest=manifest,
            provider_message_id=913,
            context=context,
            now=1005,
        )
    assert [
        str(row[0])
        for row in conn.execute(
            "SELECT outcome FROM d2_posting_review_delivery_observations "
            "ORDER BY observed_at"
        ).fetchall()
    ] == ["success", "conflict"]
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert status.state == "needs_attention"
    assert status.attention_reason == "delivery_conflict"
    with pytest.raises(PostingAuthorityError, match="activated provider message"):
        confirm_and_post(
            conn,
            key=b"d2-initial-text-key",
            reference=reference,
            context=context,
            callback_id="conflicted-confirm",
            callback_message_id=912,
            clock=lambda: 1006,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM openclaw_human_action_redemptions"
    ).fetchone()[0] == 0


def test_raw_callback_values_are_absent_from_durable_d2_rows() -> None:
    conn = _connection()
    _context, _review, manifest, _reference = _manifest(conn)
    durable_text = "\n".join(
        str(value)
        for table in (
            "d2_posting_review_controls",
            "d2_posting_review_delivery_attempts",
            "d2_posting_reviews",
        )
        for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
        if value is not None
    )
    for control in manifest.controls:
        assert control.callback_value not in durable_text
    assert manifest.delivery_attempt_nonce not in durable_text


def test_lost_delivery_receipt_replacement_has_one_current_successor() -> None:
    conn = _connection()
    context, review, predecessor, _reference = _manifest(conn)
    material_hash = hashlib.sha256(b"replacement-query-proof").hexdigest()
    successor = replace_posting_review_delivery(
        conn,
        predecessor_review_public_id=review.review_public_id,
        replacement_idempotency_key="replacement-unknown-1",
        replacement_material_hash=material_hash,
        reason="delivery_unknown",
        key=b"d2-initial-text-key",
        context=context,
        clock=lambda: 1010,
    )
    replay = replace_posting_review_delivery(
        conn,
        predecessor_review_public_id=review.review_public_id,
        replacement_idempotency_key="replacement-unknown-1",
        replacement_material_hash=material_hash,
        reason="delivery_unknown",
        key=b"d2-initial-text-key",
        context=context,
        clock=lambda: 1011,
    )
    assert replay.delivery_attempt_public_id == successor.delivery_attempt_public_id
    assert replay.finance_delivery_material_sha256 == successor.finance_delivery_material_sha256
    assert replay.idempotent is True
    assert get_status(
        conn, review_public_id=review.review_public_id, context=context
    ).attention_reason == "review_superseded"
    with pytest.raises(PostingAuthorityError, match="superseded"):
        _record_delivery(
            conn,
            manifest=predecessor,
            provider_message_id=919,
            context=context,
            now=1012,
        )
    with pytest.raises(PostingAuthorityError, match="compare-and-swap"):
        replace_posting_review_delivery(
            conn,
            predecessor_review_public_id=review.review_public_id,
            replacement_idempotency_key="replacement-different",
            replacement_material_hash=hashlib.sha256(b"different").hexdigest(),
            reason="delivery_unknown",
            key=b"d2-initial-text-key",
            context=context,
            clock=lambda: 1012,
        )
    _record_delivery(
        conn,
        manifest=successor,
        provider_message_id=920,
        context=context,
        now=1013,
    )
    successor_confirm = next(
        control for control in successor.controls if control.action == "confirm"
    )
    result = confirm_and_post(
        conn,
        key=b"d2-initial-text-key",
        reference=successor_confirm.callback_value.removeprefix("post:"),
        context=context,
        callback_id="successor-confirm",
        callback_message_id=920,
        clock=lambda: 1014,
    )
    assert result.state == "finalized"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_initial_personal_total_receipt_one_confirm_runs_full_financial_chain(
    tmp_path: Path,
) -> None:
    conn = _connection()
    seed_people(conn)
    parser_output_id, proposal_public_id = seed_receipt_proposal(
        conn, tmp_path, "d2_initial_receipt"
    )
    conn.execute(
        "UPDATE raw_intake_records SET source_message_id = '78', "
        "external_source_id = 'telegram:111:78' WHERE parser_output_id = ?",
        (parser_output_id,),
    )
    conn.commit()
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-initial-receipt-review",
        proposal_public_id=proposal_public_id,
        admitted_source_message_id="78",
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1100,
    )
    assert review.posting_path == "personal_receipt"
    assert "Receipt total:" in review.presentation_text
    for table in (
        "receipt_item_allocation_fact_sets",
        "authoritative_calculation_snapshots",
        "d2_conditional_authorization_proofs",
        "transactions",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    manifest = begin_posting_review_delivery(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-initial-receipt-key",
        context=context,
        clock=lambda: 1101,
    )
    _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=930,
        context=context,
        now=1102,
    )
    confirm = next(control for control in manifest.controls if control.action == "confirm")
    result = confirm_and_post(
        conn,
        key=b"d2-initial-receipt-key",
        reference=confirm.callback_value.removeprefix("post:"),
        context=context,
        callback_id="initial-receipt-confirm",
        callback_message_id=930,
        clock=lambda: 1103,
    )
    assert result.state == "finalized"
    assert result.transaction_public_id is not None
    assert conn.execute("SELECT COUNT(*) FROM receipt_item_allocation_fact_sets").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM authoritative_calculation_snapshots").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 0


def test_migration_050_fences_unredeemed_049_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, review_public_id, reference_id = _migration_049_d1_review(
        monkeypatch, redeemed=False
    )
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    purpose = conn.execute(
        "SELECT purpose FROM openclaw_human_action_reference_purposes WHERE reference_id = ?",
        (reference_id,),
    ).fetchone()[0]
    review = conn.execute(
        "SELECT source_kind, source_generation, card_generation_public_id, "
        "initial_card_public_id FROM d2_posting_reviews WHERE review_public_id = ?",
        (review_public_id,),
    ).fetchone()
    assert purpose == "d2_post_fenced_pre050_v1"
    assert tuple(review)[:2] == ("d1_human_card", 1)
    assert review["card_generation_public_id"] is not None
    assert review["initial_card_public_id"] is None
    context = HumanActionContext("111", "acct", "111", "binding")
    status = get_status(conn, review_public_id=review_public_id, context=context)
    assert status.state == "needs_attention"
    assert status.attention_reason == "delivery_not_activated"


def test_migration_050_preserves_accepted_049_chain_as_recovery_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, review_public_id, reference_id = _migration_049_d1_review(
        monkeypatch, redeemed=True
    )
    attempt_before = tuple(conn.execute("SELECT * FROM d2_posting_attempts").fetchone())
    decision_before = tuple(conn.execute("SELECT * FROM d2_posting_decisions").fetchone())
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    assert conn.execute(
        "SELECT purpose FROM openclaw_human_action_reference_purposes WHERE reference_id = ?",
        (reference_id,),
    ).fetchone()[0] == "d2_post_accepted_pre050_v1"
    assert tuple(conn.execute("SELECT * FROM d2_posting_attempts").fetchone()) == attempt_before
    assert tuple(conn.execute("SELECT * FROM d2_posting_decisions").fetchone()) == decision_before
    assert conn.execute(
        "SELECT source_kind FROM d2_posting_reviews WHERE review_public_id = ?",
        (review_public_id,),
    ).fetchone()[0] == "d1_human_card"
    recovered = resume_posting(
        conn,
        attempt_public_id=str(attempt_before[0]),
        context=HumanActionContext("111", "acct", "111", "binding"),
    )
    assert recovered.state == "finalized"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_migration_050_aborts_on_partial_redeemed_049_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _review_public_id, reference_id = _migration_049_d1_review(
        monkeypatch, redeemed=False
    )
    conn.execute(
        "INSERT INTO openclaw_human_action_redemptions "
        "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
        "VALUES (?, ?, 701, ?)",
        (
            reference_id,
            hashlib.sha256(b"partial-049").hexdigest(),
            "1970-01-01T00:16:44+00:00",
        ),
    )
    conn.commit()
    with pytest.raises(MigrationExecutionError):
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)


def test_delivery_attempt_transaction_rolls_back_or_replays_after_lost_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    context = HumanActionContext("111", "acct", "111", "binding")
    proposal_public_id = _seed_initial_text(conn)
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-delivery-crash-review",
        proposal_public_id=proposal_public_id,
        admitted_source_message_id="77",
        context=context,
        clock=lambda: 1200,
    )

    def before_commit(stage: str) -> None:
        if stage == "before_delivery_attempt_transaction_commit":
            raise RuntimeError("before-delivery-commit")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", before_commit)
    with pytest.raises(RuntimeError, match="before-delivery-commit"):
        begin_posting_review_delivery(
            conn,
            review_public_id=review.review_public_id,
            key=b"d2-delivery-crash-key",
            context=context,
            clock=lambda: 1201,
        )
    for table in (
        "openclaw_human_action_references",
        "openclaw_human_action_reference_purposes",
        "d2_posting_review_controls",
        "d2_posting_review_delivery_attempts",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    def after_commit(stage: str) -> None:
        if stage == "after_delivery_attempt_commit":
            raise RuntimeError("after-delivery-commit")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", after_commit)
    with pytest.raises(RuntimeError, match="after-delivery-commit"):
        begin_posting_review_delivery(
            conn,
            review_public_id=review.review_public_id,
            key=b"d2-delivery-crash-key",
            context=context,
            clock=lambda: 1201,
        )
    assert conn.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_review_controls").fetchone()[0] == 3
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_posting_review_delivery_attempts").fetchone()[0]
        == 1
    )
    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    replay = begin_posting_review_delivery(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-delivery-crash-key",
        context=context,
        clock=lambda: 1202,
    )
    assert replay.idempotent is True
    replay_again = begin_posting_review_delivery(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-delivery-crash-key",
        context=context,
        clock=lambda: 1203,
    )
    assert replay_again.delivery_attempt_nonce == replay.delivery_attempt_nonce


def test_initial_edit_wins_before_confirm_without_fabricating_financial_authority() -> None:
    conn = _connection()
    context, review, manifest, confirm_reference = _manifest(conn)
    _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=940,
        context=context,
        now=1300,
    )
    edit = next(control for control in manifest.controls if control.action == "edit")
    edit_reference = edit.callback_value.removeprefix("edit:")
    callback_id = "initial-edit-wins"
    redeemed = redeem_human_action_reference(
        conn,
        key=b"d2-initial-text-key",
        reference=edit_reference,
        action="edit",
        context=context,
        callback_id=callback_id,
        callback_message_id=940,
        clock=lambda: 1301,
    )
    conn.execute("BEGIN IMMEDIATE")
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE reference_public_id = ?",
        (redeemed.reference_public_id,),
    ).fetchone()
    begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=int(locked["id"]),
        reference_public_id=redeemed.reference_public_id,
        reference_integrity_material=edit_reference.encode("utf-8"),
        callback_message_id=940,
        redemption_public_id=callback_id,
        redemption_material_hash=hashlib.sha256(callback_id.encode()).hexdigest(),
        now_epoch=1301,
    )
    conn.commit()
    with pytest.raises(PostingAuthorityError, match="initial proposal review is stale"):
        confirm_and_post(
            conn,
            key=b"d2-initial-text-key",
            reference=confirm_reference,
            context=context,
            callback_id="confirm-after-edit",
            callback_message_id=940,
            clock=lambda: 1302,
        )
    assert conn.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    stale = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert stale.state == "needs_attention"
    assert stale.attention_reason == "review_stale_after_edit"


def test_initial_reject_wins_before_confirm_and_creates_no_transaction() -> None:
    conn = _connection()
    context, review, manifest, confirm_reference = _manifest(conn)
    _record_delivery(
        conn,
        manifest=manifest,
        provider_message_id=941,
        context=context,
        now=1310,
    )
    reject = next(control for control in manifest.controls if control.action == "reject")
    rejected = redeem_human_action_reference(
        conn,
        key=b"d2-initial-text-key",
        reference=reject.callback_value.removeprefix("reject:"),
        action="reject",
        context=context,
        callback_id="initial-reject-wins",
        callback_message_id=941,
        clock=lambda: 1311,
    )
    proposal_id = int(
        conn.execute(
            "SELECT id FROM parser_outputs WHERE public_id = ?",
            (rejected.proposal_public_id,),
        ).fetchone()[0]
    )
    confirm_parser_proposal(
        conn,
        proposal_id,
        authenticated_actor_id=context.actor_id,
        decision="rejected",
        confirmation_channel="telegram",
        confirmation_public_id="pca_initial_reject",
        expected_content_hash=rejected.proposal_content_hash,
        expected_version=rejected.proposal_version,
        clock=lambda: "1970-01-01T00:21:51+00:00",
    )
    with pytest.raises(HumanActionReferenceError, match="proposal_terminal"):
        confirm_and_post(
            conn,
            key=b"d2-initial-text-key",
            reference=confirm_reference,
            context=context,
            callback_id="confirm-after-reject",
            callback_message_id=941,
            clock=lambda: 1312,
        )
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert status.state == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
