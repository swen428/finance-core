"""D2 one-confirmation authority tests; synthetic in-memory databases only."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_core import posting_authority as posting_authority_module
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.human_drafts import (
    HumanDraftCommand,
    apply_human_draft_card,
    begin_human_draft_in_transaction,
)
from finance_core.parser_proposals.human_revision import publish_human_revision_in_transaction
from finance_core.posting_authority import (
    PostingAuthorityError,
    confirm_and_post,
    get_status,
    issue_posting_review_actions,
    prepare_posting_review,
    resume_posting,
)
from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeAuthorizationConflictError,
    authorize_receipt_finalization,
    prepare_receipt_calculation,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_parser_human_drafts_v1 import _complete_validator, _start
from tests.test_receipt_facts_conversion_v1 import seed_people, seed_receipt_proposal


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    return conn


def test_migration_049_inventory_and_pre_d2_database_fail_closed() -> None:
    pre_d2 = sqlite3.connect(":memory:")
    pre_d2.row_factory = sqlite3.Row
    pre_d2.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(pre_d2, TEMP_DB_MIGRATION_PATHS[:48])
    with pytest.raises(PostingAuthorityError, match="migration 049 is missing or incomplete"):
        get_status(pre_d2, review_public_id="d2rev_" + "0" * 30)
    pre_d2.close()

    current = _connection()
    tables = {
        str(row[0])
        for row in current.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'd2_%'"
        )
    }
    assert tables == {
        "d2_posting_reviews",
        "d2_posting_review_action_bindings",
        "d2_posting_attempts",
        "d2_posting_decisions",
        "d2_posting_receipt_evidence",
        "d2_conditional_authorization_proofs",
        "d2_posting_attempt_events",
    }
    current.close()


def _published_text_card(monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, object]:
    conn = _connection()
    started = _start(
        conn,
        payload={
            "intent": "personal_expense_log",
            "transaction_type": "personal_expense",
            "amount": "12.50",
            "currency": "SGD",
            "transaction_date": "2026-09-13",
            "merchant": "Kopitiam",
            "description": "Lunch",
            "category": "food",
        },
    )
    fields = {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-13",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    text = (
        f"资料卡编号：{started.card_generation_public_id}\r\n金额: 12.50\r\n币种：SGD\r\n"
        "日期: 2026-09-13\r\n商户: Cafe\r\n描述: Lunch\r\n分类: food"
    )
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    published = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_d2_text_publish",
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    return conn, published


def test_text_confirm_posts_once_and_exact_replay_returns_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-review-1",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    assert review.posting_path == "text"
    changes_before_status = conn.total_changes
    assert get_status(conn, review_public_id=review.review_public_id).state == (
        "awaiting_confirmation"
    )
    assert conn.total_changes == changes_before_status
    for table in (
        "transactions",
        "receipt_item_allocation_fact_sets",
        "receipt_finalization_authorizations",
        "d2_posting_decisions",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    key = b"d2-one-confirmation-test-key"
    issued, replayed_issue = issue_posting_review_actions(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        clock=lambda: 1003,
    )
    assert replayed_issue is False
    status = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-callback-1",
        callback_message_id=200,
        clock=lambda: 1004,
    )
    assert status.state == "finalized"
    assert status.transaction_public_id is not None
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_decisions").fetchone()[0] == 1

    replay = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-callback-1",
        callback_message_id=200,
        clock=lambda: 9999,
    )
    assert replay == status
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.close()


def test_changed_callback_cannot_reuse_an_accepted_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-review-2",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"d2-one-confirmation-test-key"
    issued, _ = issue_posting_review_actions(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        clock=lambda: 1003,
    )
    confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-callback-original",
        callback_message_id=200,
        clock=lambda: 1004,
    )
    with pytest.raises(PostingAuthorityError, match="does not match durable authority"):
        confirm_and_post(
            conn,
            key=key,
            reference=issued.reference,
            context=context,
            callback_id="d2-callback-changed",
            callback_message_id=200,
            clock=lambda: 1005,
        )
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.close()


def _published_receipt_card(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    seed_people(conn)
    parser_output_id, _proposal_public_id = seed_receipt_proposal(conn, tmp_path, "d2_personal")
    content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
    reference_material = b"d2-receipt-edit-reference"
    conn.execute(
        """
        INSERT INTO openclaw_human_action_references (
            reference_public_id, reference_sha256, issuance_idempotency_key,
            parser_output_id, action, proposal_version, proposal_content_hash,
            authenticated_actor_id, channel, channel_account_id,
            channel_conversation_id, conversation_binding_id, ttl_seconds,
            expires_at, issued_at
        ) VALUES (?, ?, ?, ?, 'edit', 0, ?, '111', 'telegram', 'acct', '111',
                  'binding', 600, 2000, '1970-01-01T00:16:40+00:00')
        """,
        (
            "haref_1234567890abcdef1234567890abcdef",
            hashlib.sha256(reference_material).hexdigest(),
            "bridge-human-action-issue:" + "9" * 32,
            parser_output_id,
            content_hash,
        ),
    )
    conn.commit()
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE action = 'edit'"
    ).fetchone()
    assert locked is not None
    redemption_hash = hashlib.sha256(b"d2-receipt-start").hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO openclaw_human_action_redemptions "
        "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
        "VALUES (?, ?, 100, '1970-01-01T00:16:40+00:00')",
        (locked["id"], redemption_hash),
    )
    started = begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=locked["id"],
        reference_public_id=locked["reference_public_id"],
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id="d1start_d2_receipt",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    conn.commit()
    fields = {
        "amount": "12.34",
        "currency": "SGD",
        "transaction_date": "2026-07-20",
        "merchant": "COLD STORAGE LTD",
        "description": "",
        "category": "",
    }
    text = (
        f"资料卡编号：{started.card_generation_public_id}\r\n金额: 12.34\r\n币种：SGD\r\n"
        "日期: 2026-07-20\r\n商户: COLD STORAGE LTD\r\n描述: \r\n分类: "
    )

    def receipt_validator(current_payload: object, supplied: object, **_kwargs: object) -> object:
        current = dict(current_payload)  # type: ignore[arg-type]
        updates = {
            key: (None if value == "" else value)
            for key, value in dict(supplied).items()  # type: ignore[arg-type]
        }
        payload = {**current, **updates}
        changed = tuple(sorted(key for key, value in updates.items() if current.get(key) != value))
        return SimpleNamespace(
            canonical_payload=payload,
            changed_fields=changed,
            completeness="publishable",
            reason_contributors=(),
            unresolved_flags=(),
            explicit_clears={},
        )

    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", receipt_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    return apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_d2_receipt_publish",
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )


def test_personal_receipt_one_confirm_binds_d1b_snapshot_d2b_and_finalizes_once(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-personal-receipt-review",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    assert review.posting_path == "personal_receipt"
    assert conn.execute("SELECT COUNT(*) FROM receipt_item_allocation_fact_sets").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0] == 0
    )

    key = b"d2-personal-receipt-key"
    issued, _ = issue_posting_review_actions(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        clock=lambda: 1003,
    )
    first = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-receipt-callback",
        callback_message_id=300,
        clock=lambda: 1004,
    )
    assert first.state == "finalized"
    assert first.transaction_public_id is not None
    authorization = conn.execute(
        "SELECT authorization_version FROM receipt_finalization_authorizations"
    ).fetchone()
    assert authorization["authorization_version"] == "d2_conditional_v1"
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0] == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_receipt_evidence").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1

    replay = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-receipt-callback",
        callback_message_id=300,
        clock=lambda: 9999,
    )
    assert replay == first
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1

    for table in (
        "d2_posting_reviews",
        "d2_posting_review_action_bindings",
        "d2_posting_attempts",
        "d2_posting_decisions",
        "d2_posting_receipt_evidence",
        "d2_conditional_authorization_proofs",
        "d2_posting_attempt_events",
    ):
        columns = [str(column[1]) for column in conn.execute(f"PRAGMA table_info({table})")]
        projection = ", ".join(columns)
        with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({projection}) "
                f"SELECT {projection} FROM {table} LIMIT 1"
            )
        conn.rollback()


@pytest.mark.parametrize(
    "failure_stage",
    (
        "after_confirmation_commit",
        "after_conversion_commit",
        "after_fact_set_commit",
        "after_snapshot_commit",
        "after_conditional_authorization_commit",
        "after_receipt_finalization_commit",
    ),
)
def test_receipt_crash_boundaries_resume_without_second_confirmation_or_duplicate(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key=f"d2-crash-{failure_stage}",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    key = b"d2-receipt-crash-key"
    issued, _ = issue_posting_review_actions(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        clock=lambda: 1003,
    )

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected:{stage}")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", fail)
    with pytest.raises(RuntimeError, match=f"injected:{failure_stage}"):
        confirm_and_post(
            conn,
            key=key,
            reference=issued.reference,
            context=context,
            callback_id=f"callback-{failure_stage}",
            callback_message_id=400,
            clock=lambda: 1004,
        )
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    recovered = resume_posting(conn, attempt_public_id=attempt_id)
    assert recovered.state == "finalized"
    assert recovered.transaction_public_id is not None
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_decisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1


def test_d2_refuses_to_adopt_manual_receipt_authorization(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-manual-authority-conflict",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    key = b"d2-manual-conflict-key"
    issued, _ = issue_posting_review_actions(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        clock=lambda: 1003,
    )

    def fail_after_snapshot(stage: str) -> None:
        if stage == "after_snapshot_commit":
            raise RuntimeError("snapshot-stopped")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", fail_after_snapshot)
    with pytest.raises(RuntimeError, match="snapshot-stopped"):
        confirm_and_post(
            conn,
            key=key,
            reference=issued.reference,
            context=context,
            callback_id="manual-conflict-callback",
            callback_message_id=500,
            clock=lambda: 1004,
        )
    receipt_public_id = str(conn.execute("SELECT public_id FROM receipts").fetchone()[0])
    prepared = prepare_receipt_calculation(
        conn,
        receipt_public_id,
        actor_type="system",
        actor_id="d2-posting-authority",
    )
    authorize_receipt_finalization(conn, prepared, actor_id="111")
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    with pytest.raises(BridgeAuthorizationConflictError, match="uses version 'v1'"):
        resume_posting(conn, attempt_public_id=attempt_id)
    attention = get_status(conn, review_public_id=review.review_public_id)
    assert attention.state == "needs_attention"
    assert attention.attention_reason == "conditional_authorization_conflict"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0] == 0
    )
