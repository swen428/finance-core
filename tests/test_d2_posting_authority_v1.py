"""D2 one-confirmation authority tests; synthetic in-memory databases only."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from finance_core import posting_authority as posting_authority_module
from finance_core.openclaw_staging_bridge.delivery_receipt_proof import (
    authenticate_delivery_receipt,
    receipt_proof_sha256,
)
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
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
    begin_posting_review_delivery,
    confirm_and_post,
    get_status,
    get_status_by_reference,
    issue_posting_review_actions,
    prepare_posting_review,
    record_posting_review_delivery,
    resume_posting,
)
from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeAuthorizationConflictError,
    authorize_d2_conditional_receipt_finalization,
    authorize_receipt_finalization,
    prepare_receipt_calculation,
)
from finance_core.receipt_staging_runner.workspace import load_delivery_receipt_signing_key
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.sqlite_connection import ForeignKeysDisabledError
from finance_core.staging_guard import StagingDatabaseError, create_staging_database
from tests.test_parser_human_drafts_v1 import _complete_validator, _start
from tests.test_receipt_facts_conversion_v1 import seed_people, seed_receipt_proposal


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    return conn


def _file_connection(tmp_path: Path) -> sqlite3.Connection:
    workspace = (tmp_path / "workspace").resolve()
    runtime = workspace / "runtime"
    database = workspace / "database"
    runtime.mkdir(parents=True, mode=0o700)
    database.mkdir(mode=0o700)
    key_path = runtime / "delivery_receipt_signing.key"
    key_path.write_bytes(b"p" * 32)
    key_path.chmod(0o600)
    return create_staging_database(
        database / "staging.sqlite",
        migration_paths=TEMP_DB_MIGRATION_PATHS,
    )


@pytest.fixture()
def migrated_temp_db_connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = _file_connection(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def _issue_and_activate(
    conn: sqlite3.Connection,
    *,
    review_public_id: str,
    key: bytes,
    context: HumanActionContext,
    provider_message_id: int,
    issue_time: int = 1003,
) -> tuple[object, bool]:
    manifest = begin_posting_review_delivery(
        conn,
        review_public_id=review_public_id,
        key=key,
        context=context,
        clock=lambda: issue_time,
    )
    database_path = Path(
        str(next(row for row in conn.execute("PRAGMA database_list") if str(row[1]) == "main")[2])
    )
    workspace = database_path.parent.parent
    signing_key = load_delivery_receipt_signing_key(str(workspace / "runtime"))
    fields = {
        "workspace_path": str(workspace),
        "attempt_nonce": manifest.delivery_attempt_nonce,
        "capability": "telegram.finance-delivery-material-v1",
        "delivery_material_version": "finance_d2_delivery_material_v1",
        "delivery_material_sha256": manifest.finance_delivery_material_sha256,
        "provider_message_id": provider_message_id,
        "receipt_token_sha256": hashlib.sha256(
            f"receipt:{provider_message_id}".encode()
        ).hexdigest(),
        "channel": "telegram",
        "account_id": context.account_id,
        "conversation_id": context.conversation_id,
        "session_key": context.binding_id,
        "source_identity_sha256": "b" * 64,
    }
    record_posting_review_delivery(
        conn,
        receipt=authenticate_delivery_receipt(
            receipt_proof_sha256_value=receipt_proof_sha256(
                signing_key=signing_key,
                **fields,
            ),
            **fields,
        ),
        clock=lambda: issue_time + 1,
    )
    confirm = next(control for control in manifest.controls if control.action == "confirm")
    return (
        SimpleNamespace(reference=confirm.callback_value.removeprefix("post:")),
        manifest.idempotent,
    )


def test_migration_050_inventory_and_pre_d2_database_fail_closed() -> None:
    pre_d2 = sqlite3.connect(":memory:")
    pre_d2.row_factory = sqlite3.Row
    pre_d2.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(pre_d2, TEMP_DB_MIGRATION_PATHS[:48])
    with pytest.raises(PostingAuthorityError, match="migration 050 is missing or incomplete"):
        get_status(
            pre_d2,
            review_public_id="d2rev_" + "0" * 30,
            context=HumanActionContext("111", "acct", "111", "binding"),
        )
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
        "d2_telegram_source_contexts",
        "d2_initial_proposal_cards",
        "d2_posting_review_action_bindings",
        "d2_posting_review_controls",
        "d2_posting_review_delivery_attempts",
        "d2_posting_review_delivery_observations",
        "d2_posting_review_delivery_activations",
        "d2_posting_review_delivery_conflicts",
        "d2_posting_review_supersessions",
        "d2_posting_attempts",
        "d2_posting_decisions",
        "d2_posting_receipt_evidence",
        "d2_conditional_authorization_proofs",
        "d2_posting_attempt_events",
    }
    current.close()


def test_migration_050_missing_trigger_fails_closed() -> None:
    conn = _connection()
    conn.execute("DROP TRIGGER trg_d2_posting_attempts_guarded_update")
    with pytest.raises(PostingAuthorityError, match="migration 050 is missing or incomplete"):
        get_status(
            conn,
            review_public_id="d2rev_" + "0" * 30,
            context=HumanActionContext("111", "acct", "111", "binding"),
        )
    conn.close()


def _published_text_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[sqlite3.Connection, object]:
    conn = _file_connection(tmp_path)
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
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
    assert get_status(conn, review_public_id=review.review_public_id, context=context).state == (
        "needs_attention"
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
    issued, replayed_issue = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=200,
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
    assert (
        status.amount,
        status.currency,
        status.transaction_date,
        status.merchant,
        status.account,
    ) == (
        "12.50",
        "SGD",
        "2026-09-13",
        "Cafe",
        "unspecified",
    )
    assert get_status_by_reference(conn, reference=issued.reference, context=context) == status
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-review-2",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"d2-one-confirmation-test-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=200,
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


def test_text_committed_result_is_visible_before_attempt_catchup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-result-recovery",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"d2-text-result-recovery-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=210,
    )

    def fail(stage: str) -> None:
        if stage == "after_text_finalization_commit":
            raise RuntimeError("text-finalization-committed")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", fail)
    with pytest.raises(RuntimeError, match="text-finalization-committed"):
        confirm_and_post(
            conn,
            key=key,
            reference=issued.reference,
            context=context,
            callback_id="d2-text-result-callback",
            callback_message_id=210,
            clock=lambda: 1004,
        )
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    before = conn.total_changes
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert conn.total_changes == before
    assert status.state == "finalized"
    assert (
        status.transaction_public_id
        == conn.execute("SELECT public_id FROM transactions").fetchone()[0]
    )

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    recovered = resume_posting(conn, attempt_public_id=attempt_id, context=context)
    assert recovered == status
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.close()


def test_text_status_original_is_immutable_after_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-transaction-drift",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-text-transaction-drift-key",
        context=context,
        provider_message_id=211,
    )
    assert (
        confirm_and_post(
            conn,
            key=b"d2-text-transaction-drift-key",
            reference=issued.reference,
            context=context,
            callback_id="d2-text-transaction-drift-callback",
            callback_message_id=211,
            clock=lambda: 1004,
        ).state
        == "finalized"
    )
    with pytest.raises(sqlite3.IntegrityError, match="finalized D2 transaction is immutable"):
        conn.execute("UPDATE transactions SET amount = 99.99 WHERE parser_output_id IS NOT NULL")
    conn.rollback()
    before = conn.total_changes
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert conn.total_changes == before
    assert status.state == "finalized"
    assert status.amount != "99.99"


def test_text_status_never_returns_original_money_after_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-corrected-status",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-text-corrected-status-key",
        context=context,
        provider_message_id=211,
    )
    original = confirm_and_post(
        conn,
        key=b"d2-text-corrected-status-key",
        reference=issued.reference,
        context=context,
        callback_id="d2-text-corrected-status-callback",
        callback_message_id=211,
        clock=lambda: 1004,
    )
    assert original.state == "finalized"
    monkeypatch.setattr(
        "finance_core.application.correction_schema.has_committed_correction",
        lambda _conn, target: target == original.transaction_public_id,
    )
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert status.state == "needs_attention"
    assert status.attention_reason == "local_current_lookup_required"
    assert status.transaction_public_id is None
    assert (status.amount, status.currency, status.transaction_date, status.merchant) == (
        None, None, None, None
    )


def test_finalized_catchup_revalidates_under_write_lock_before_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-catchup-race",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"d2-text-catchup-race-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=213,
    )

    def crash_after_financial_commit(stage: str) -> None:
        if stage == "after_text_finalization_commit":
            raise RuntimeError("text-financial-commit")

    monkeypatch.setattr(
        posting_authority_module, "_failure_injection_hook", crash_after_financial_commit
    )
    with pytest.raises(RuntimeError, match="text-financial-commit"):
        confirm_and_post(
            conn,
            key=key,
            reference=issued.reference,
            context=context,
            callback_id="d2-text-catchup-race-callback",
            callback_message_id=213,
            clock=lambda: 1004,
        )
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    events_before = conn.execute("SELECT COUNT(*) FROM d2_posting_attempt_events").fetchone()[0]

    def drift_before_lock(stage: str) -> None:
        if stage == "before_finalized_catchup_lock":
            conn.execute("UPDATE transactions SET amount = 99.99")
            conn.commit()

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", drift_before_lock)
    status = resume_posting(conn, attempt_public_id=attempt_id, context=context)
    assert status.state == "needs_attention"
    assert status.attention_reason == "financial_authority_mismatch"
    attempt = conn.execute(
        "SELECT stage, transaction_public_id FROM d2_posting_attempts WHERE attempt_public_id = ?",
        (attempt_id,),
    ).fetchone()
    assert attempt["stage"] == "accepted"
    assert attempt["transaction_public_id"] is None
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_posting_attempt_events").fetchone()[0]
        == events_before
    )


def test_initial_text_finalization_uses_atomic_verified_catchup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-initial-text-catchup-race",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"d2-initial-text-catchup-race-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=214,
    )

    def drift_after_financial_commit(stage: str) -> None:
        if stage == "after_text_finalization_commit":
            conn.execute("UPDATE transactions SET amount = 99.99")
            conn.commit()

    monkeypatch.setattr(
        posting_authority_module, "_failure_injection_hook", drift_after_financial_commit
    )
    status = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-initial-text-catchup-race-callback",
        callback_message_id=214,
        clock=lambda: 1004,
    )
    assert status.state == "needs_attention"
    attempt = conn.execute("SELECT stage FROM d2_posting_attempts").fetchone()
    assert attempt["stage"] == "accepted"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM d2_posting_attempt_events WHERE to_stage = 'finalized'"
        ).fetchone()[0]
        == 0
    )


def test_resume_requires_complete_d2_decision_before_text_financial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-missing-decision",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-text-missing-decision-key",
        context=context,
        provider_message_id=215,
    )
    ref = conn.execute(
        "SELECT refs.* FROM openclaw_human_action_references AS refs "
        "JOIN d2_posting_review_action_bindings AS bindings ON bindings.reference_id = refs.id "
        "WHERE bindings.review_public_id = ?",
        (review.review_public_id,),
    ).fetchone()
    assert ref is not None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO openclaw_human_action_redemptions "
        "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
        "VALUES (?, ?, 212, '1970-01-01T00:16:44+00:00')",
        (ref["id"], hashlib.sha256(b"missing-decision").hexdigest()),
    )
    confirmation_id = "pca_d2_missing_decision"
    confirm_parser_proposal(
        conn,
        int(ref["parser_output_id"]),
        authenticated_actor_id="111",
        decision="confirmed",
        confirmation_channel="telegram",
        confirmation_public_id=confirmation_id,
        expected_content_hash=str(ref["proposal_content_hash"]),
        expected_version=int(ref["proposal_version"]),
        d1_decision_binding=HumanDraftDecisionBinding(
            reference_public_id=str(ref["reference_public_id"]),
            card_generation_public_id=published.card_generation_public_id,
            authenticated_actor_id="111",
            telegram_account_id="acct",
            telegram_conversation_id="111",
            conversation_binding_id="binding",
        ),
        clock=lambda: "1970-01-01T00:16:44+00:00",
        _caller_owns_transaction=True,
    )
    attempt_id = "d2att_" + "a" * 30
    conn.execute(
        "INSERT INTO d2_posting_attempts "
        "(attempt_public_id, review_public_id, reference_id, posting_path, stage, "
        "stage_evidence_public_id, created_at, updated_at) "
        "VALUES (?, ?, ?, 'text', 'accepted', ?, ?, ?)",
        (
            attempt_id,
            review.review_public_id,
            ref["id"],
            confirmation_id,
            "1970-01-01T00:16:44+00:00",
            "1970-01-01T00:16:44+00:00",
        ),
    )
    conn.commit()
    with pytest.raises(PostingAuthorityError, match="posting authority unavailable"):
        resume_posting(conn, attempt_public_id=attempt_id, context=context)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_status_refuses_finalized_coordination_row_with_wrong_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-false-finalized",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-text-false-finalized-key",
        context=context,
        provider_message_id=215,
    )

    def fail(stage: str) -> None:
        if stage == "after_text_finalization_commit":
            raise RuntimeError("text-committed-before-coordination")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", fail)
    with pytest.raises(RuntimeError, match="text-committed-before-coordination"):
        confirm_and_post(
            conn,
            key=b"d2-text-false-finalized-key",
            reference=issued.reference,
            context=context,
            callback_id="d2-text-false-finalized-callback",
            callback_message_id=215,
            clock=lambda: 1004,
        )
    canonical = dict(conn.execute("SELECT * FROM transactions").fetchone())
    canonical.pop("id")
    canonical["public_id"] = "txn_unrelated_d2_coordination"
    columns = tuple(canonical)
    conn.execute(
        f"INSERT INTO transactions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(canonical[column] for column in columns),
    )
    attempt = conn.execute("SELECT * FROM d2_posting_attempts").fetchone()
    conn.execute(
        "UPDATE d2_posting_attempts SET stage = 'finalized', row_version = ?, "
        "transaction_public_id = ?, attention_reason = NULL, "
        "stage_evidence_public_id = ?, updated_at = ? WHERE attempt_public_id = ?",
        (
            int(attempt["row_version"]) + 1,
            canonical["public_id"],
            canonical["public_id"],
            "1970-01-01T00:20:00+00:00",
            attempt["attempt_public_id"],
        ),
    )
    conn.commit()
    before = conn.total_changes
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert conn.total_changes == before
    assert status.state == "needs_attention"
    assert status.transaction_public_id is None
    assert status.attention_reason == "financial_authority_mismatch"
    conn.close()


def test_exact_confirm_replay_inside_redemption_transaction_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-text-lock-race",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"d2-text-lock-race-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=220,
    )
    monkeypatch.setattr(
        posting_authority_module,
        "_accepted_attempt_for_callback",
        lambda *_args, **_kwargs: None,
    )
    first = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-lock-race-callback",
        callback_message_id=220,
        clock=lambda: 1004,
    )
    replay = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-lock-race-callback",
        callback_message_id=220,
        clock=lambda: 1005,
    )
    assert replay == first
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM openclaw_human_action_redemptions AS redemptions "
            "JOIN openclaw_human_action_references AS refs ON refs.id = redemptions.reference_id "
            "WHERE refs.action = 'confirm'"
        ).fetchone()[0]
        == 1
    )
    for table in ("d2_posting_attempts", "d2_posting_decisions", "transactions"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    conn.close()


def test_status_and_resume_reject_cross_context_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-cross-context",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-cross-context-key",
        context=context,
        provider_message_id=230,
    )

    def fail(stage: str) -> None:
        if stage == "after_confirmation_commit":
            raise RuntimeError("confirmation-committed")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", fail)
    with pytest.raises(RuntimeError, match="confirmation-committed"):
        confirm_and_post(
            conn,
            key=b"d2-cross-context-key",
            reference=issued.reference,
            context=context,
            callback_id="d2-cross-context-callback",
            callback_message_id=230,
            clock=lambda: 1004,
        )
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    for wrong in (
        HumanActionContext("222", "acct", "222", "binding"),
        HumanActionContext("111", "other-account", "111", "binding"),
        HumanActionContext("111", "acct", "111", "other-binding"),
    ):
        before = conn.total_changes
        with pytest.raises(PostingAuthorityError, match="posting authority unavailable"):
            get_status(conn, review_public_id=review.review_public_id, context=wrong)
        with pytest.raises(PostingAuthorityError, match="posting authority unavailable"):
            resume_posting(conn, attempt_public_id=attempt_id, context=wrong)
        assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    conn.close()


def test_issue_actions_requires_staging_and_foreign_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-issue-guards",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    before_references = conn.execute(
        "SELECT COUNT(*) FROM openclaw_human_action_references"
    ).fetchone()[0]
    before_bindings = conn.execute(
        "SELECT COUNT(*) FROM d2_posting_review_action_bindings"
    ).fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(ForeignKeysDisabledError):
        issue_posting_review_actions(
            conn,
            review_public_id=review.review_public_id,
            key=b"d2-issue-guard-key",
            context=context,
            clock=lambda: 1003,
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0]
        == before_references
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_posting_review_action_bindings").fetchone()[0]
        == before_bindings
    )
    conn.execute("PRAGMA foreign_keys = ON")

    untrusted = sqlite3.connect(tmp_path / "untrusted-copy.sqlite")
    untrusted.row_factory = sqlite3.Row
    conn.backup(untrusted)
    with pytest.raises(StagingDatabaseError):
        issue_posting_review_actions(
            untrusted,
            review_public_id=review.review_public_id,
            key=b"d2-issue-guard-key",
            context=context,
            clock=lambda: 1003,
        )
    assert (
        untrusted.execute("SELECT COUNT(*) FROM openclaw_human_action_references").fetchone()[0]
        == before_references
    )
    assert (
        untrusted.execute("SELECT COUNT(*) FROM d2_posting_review_action_bindings").fetchone()[0]
        == before_bindings
    )
    untrusted.close()
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


def test_personal_receipt_requires_unique_active_self_at_review(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    with pytest.raises(PostingAuthorityError, match="unique active self"):
        prepare_posting_review(
            conn,
            review_idempotency_key="d2-nonself-review",
            card_generation_public_id=published.card_generation_public_id,
            context=context,
            receipt_payer_participant_public_id="person_alice",
            clock=lambda: 1002,
        )

    conn.execute("UPDATE participants SET is_active = 0 WHERE public_id = 'person_owner'")
    conn.commit()
    with pytest.raises(PostingAuthorityError, match="unique active self"):
        prepare_posting_review(
            conn,
            review_idempotency_key="d2-inactive-self-review",
            card_generation_public_id=published.card_generation_public_id,
            context=context,
            receipt_payer_participant_public_id="person_owner",
            clock=lambda: 1002,
        )

    conn.execute("UPDATE participants SET is_active = 1 WHERE public_id = 'person_owner'")
    conn.execute("UPDATE participants SET is_self = 1 WHERE public_id = 'person_alice'")
    conn.commit()
    with pytest.raises(PostingAuthorityError, match="unique active self"):
        prepare_posting_review(
            conn,
            review_idempotency_key="d2-multiple-self-review",
            card_generation_public_id=published.card_generation_public_id,
            context=context,
            receipt_payer_participant_public_id="person_owner",
            clock=lambda: 1002,
        )
    assert conn.execute("SELECT COUNT(*) FROM d2_posting_reviews").fetchone()[0] == 0


def test_personal_identity_drift_before_confirm_creates_no_decision_or_financial_fact(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-self-drift-before-confirm",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-self-drift-key",
        context=context,
        provider_message_id=290,
    )
    redemptions_before = conn.execute(
        "SELECT COUNT(*) FROM openclaw_human_action_redemptions"
    ).fetchone()[0]
    conn.execute("UPDATE participants SET is_active = 0 WHERE public_id = 'person_owner'")
    conn.commit()
    with pytest.raises(PostingAuthorityError, match="unique active self"):
        confirm_and_post(
            conn,
            key=b"d2-self-drift-key",
            reference=issued.reference,
            context=context,
            callback_id="d2-self-drift-callback",
            callback_message_id=290,
            clock=lambda: 1004,
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM openclaw_human_action_redemptions").fetchone()[0]
        == redemptions_before
    )
    for table in (
        "d2_posting_decisions",
        "receipt_item_allocation_fact_sets",
        "receipt_finalization_authorizations",
        "transactions",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


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
        clock=lambda: 1002,
    )
    assert review.posting_path == "personal_receipt"
    assert conn.execute("SELECT COUNT(*) FROM receipt_item_allocation_fact_sets").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0] == 0
    )

    key = b"d2-personal-receipt-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=300,
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
    proof = conn.execute(
        "SELECT proofs.reviewed_projection_hash, proofs.snapshot_projection_hash, "
        "reviews.visible_projection_hash FROM d2_conditional_authorization_proofs AS proofs "
        "JOIN d2_posting_reviews AS reviews "
        "ON reviews.review_public_id = proofs.review_public_id"
    ).fetchone()
    assert proof["reviewed_projection_hash"] == proof["visible_projection_hash"]
    assert proof["snapshot_projection_hash"] == proof["visible_projection_hash"]
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
    attempt = conn.execute("SELECT * FROM d2_posting_attempts").fetchone()
    events = conn.execute("SELECT * FROM d2_posting_attempt_events ORDER BY row_version").fetchall()
    assert len(events) == int(attempt["row_version"]) + 1
    assert events[-1]["to_stage"] == attempt["stage"]
    assert events[-1]["transaction_public_id"] == attempt["transaction_public_id"]
    with pytest.raises(sqlite3.IntegrityError, match="contradicts attempt state"):
        conn.execute(
            "INSERT INTO d2_posting_attempt_events "
            "(event_public_id, attempt_public_id, from_stage, to_stage, row_version, "
            "evidence_public_id, transaction_public_id, attention_reason, created_at) "
            "VALUES (?, ?, 'finalized', 'finalized', ?, ?, ?, NULL, ?)",
            (
                "d2evt_" + "f" * 30,
                attempt["attempt_public_id"],
                int(attempt["row_version"]) + 1,
                events[-1]["evidence_public_id"],
                attempt["transaction_public_id"],
                attempt["updated_at"],
            ),
        )
    conn.rollback()

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
        with pytest.raises(
            sqlite3.IntegrityError,
            match="identity collision|must begin accepted|contradicts attempt state",
        ):
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({projection}) "
                f"SELECT {projection} FROM {table} LIMIT 1"
            )
        conn.rollback()


def test_finalized_personal_receipt_uses_frozen_participant_authority(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-frozen-participant-authority",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    key = b"d2-frozen-participant-authority-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=310,
    )
    first = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-frozen-participant-authority-callback",
        callback_message_id=310,
        clock=lambda: 1004,
    )
    assert first.state == "finalized"
    conn.execute("UPDATE participants SET is_active = 0 WHERE public_id = 'person_owner'")
    conn.execute("UPDATE participants SET is_self = 1 WHERE public_id = 'person_alice'")
    conn.commit()
    before = conn.total_changes
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert conn.total_changes == before
    assert status == first
    replay = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-frozen-participant-authority-callback",
        callback_message_id=310,
        clock=lambda: 9999,
    )
    assert replay == first
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_receipt_status_original_is_immutable_after_finalization(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-receipt-transaction-drift",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"d2-receipt-transaction-drift-key",
        context=context,
        provider_message_id=311,
    )
    assert (
        confirm_and_post(
            conn,
            key=b"d2-receipt-transaction-drift-key",
            reference=issued.reference,
            context=context,
            callback_id="d2-receipt-transaction-drift-callback",
            callback_message_id=311,
            clock=lambda: 1004,
        ).state
        == "finalized"
    )
    with pytest.raises(sqlite3.IntegrityError, match="finalized D2 transaction is immutable"):
        conn.execute("UPDATE transactions SET amount = 99.99")
    conn.rollback()
    before = conn.total_changes
    status = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert conn.total_changes == before
    assert status.state == "finalized"
    assert status.amount != "99.99"


def test_receipt_finalization_catchup_ignores_later_participant_flag_changes(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-finalization-catchup-frozen-participant",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    key = b"d2-finalization-catchup-frozen-participant-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=312,
    )

    def fail(stage: str) -> None:
        if stage == "after_receipt_finalization_commit":
            raise RuntimeError("receipt-finalization-committed")

    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", fail)
    with pytest.raises(RuntimeError, match="receipt-finalization-committed"):
        confirm_and_post(
            conn,
            key=key,
            reference=issued.reference,
            context=context,
            callback_id="d2-finalization-catchup-frozen-participant-callback",
            callback_message_id=312,
            clock=lambda: 1004,
        )
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    assert (
        conn.execute(
            "SELECT stage FROM d2_posting_attempts WHERE attempt_public_id = ?", (attempt_id,)
        ).fetchone()[0]
        == "conditional_authorization_persisted"
    )
    conn.execute("UPDATE participants SET is_active = 0 WHERE public_id = 'person_owner'")
    conn.execute("UPDATE participants SET is_self = 1 WHERE public_id = 'person_alice'")
    conn.commit()
    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    recovered = resume_posting(conn, attempt_public_id=attempt_id, context=context)
    assert recovered.state == "finalized"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    attempt = conn.execute(
        "SELECT stage, stage_evidence_public_id FROM d2_posting_attempts "
        "WHERE attempt_public_id = ?",
        (attempt_id,),
    ).fetchone()
    assert attempt["stage"] == "finalized"
    assert (
        attempt["stage_evidence_public_id"]
        == conn.execute("SELECT finalization_id FROM receipt_finalization_audit").fetchone()[0]
    )


def test_initial_receipt_finalization_uses_atomic_verified_catchup(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2-initial-receipt-catchup-race",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        receipt_payer_participant_public_id="person_owner",
        clock=lambda: 1002,
    )
    key = b"d2-initial-receipt-catchup-race-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=313,
    )

    def drift_after_financial_commit(stage: str) -> None:
        if stage == "after_receipt_finalization_commit":
            conn.execute("UPDATE transactions SET amount = 99.99")
            conn.commit()

    monkeypatch.setattr(
        posting_authority_module, "_failure_injection_hook", drift_after_financial_commit
    )
    status = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2-initial-receipt-catchup-race-callback",
        callback_message_id=313,
        clock=lambda: 1004,
    )
    assert status.state == "needs_attention"
    attempt = conn.execute("SELECT stage FROM d2_posting_attempts").fetchone()
    assert attempt["stage"] == "conditional_authorization_persisted"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM d2_posting_attempt_events WHERE to_stage = 'finalized'"
        ).fetchone()[0]
        == 0
    )


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
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=400,
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
    if failure_stage == "after_receipt_finalization_commit":
        before = conn.total_changes
        committed = get_status(conn, review_public_id=review.review_public_id, context=context)
        assert conn.total_changes == before
        assert committed.state == "finalized"
        assert (
            committed.transaction_public_id
            == conn.execute("SELECT public_id FROM transactions").fetchone()[0]
        )
    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    recovered = resume_posting(conn, attempt_public_id=attempt_id, context=context)
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
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=500,
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
    attempt_id = str(
        conn.execute("SELECT attempt_public_id FROM d2_posting_attempts").fetchone()[0]
    )
    posting_authority_module._advance_attempt(
        conn,
        attempt_id=attempt_id,
        expected_stage="fact_set_persisted",
        new_stage="snapshot_persisted",
        evidence_public_id=prepared.calculation_snapshot_id,
    )
    decision = conn.execute(
        "SELECT decision_public_id, review_public_id FROM d2_posting_decisions"
    ).fetchone()
    with pytest.raises(BridgeAuthorizationConflictError, match="contradictory"):
        authorize_d2_conditional_receipt_finalization(
            conn,
            prepared,
            actor_id="unaccepted_actor",
            decision_public_id=str(decision["decision_public_id"]),
            review_public_id=str(decision["review_public_id"]),
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM receipt_finalization_authorizations").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0] == 0
    )
    authorize_receipt_finalization(conn, prepared, actor_id="111")
    monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
    with pytest.raises(BridgeAuthorizationConflictError, match="uses version 'v1'"):
        resume_posting(conn, attempt_public_id=attempt_id, context=context)
    attention = get_status(conn, review_public_id=review.review_public_id, context=context)
    assert attention.state == "needs_attention"
    assert attention.attention_reason == "conditional_authorization_conflict"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM d2_conditional_authorization_proofs").fetchone()[0] == 0
    )
