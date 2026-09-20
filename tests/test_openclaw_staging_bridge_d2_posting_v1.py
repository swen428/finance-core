"""D2 Bridge orchestration tests using only synthetic staging workspaces."""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import delivery_receipt_cli
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card
from finance_core.parser_proposals.human_revision import publish_human_revision_in_transaction
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    record_telegram_source_context,
)
from tests.test_parser_human_drafts_v1 import _complete_validator
from tests.test_receipt_facts_conversion_v1 import seed_people, seed_receipt_proposal

ACTOR = "111"
ACCOUNT = "finance-account"
CONVERSATION = "111"
BINDING = "binding-1"


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _published_text_card(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, int, str, str]:
    conn = support.open_database(workspace)
    now = int(time.time())
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-13",
        "merchant": "Kopitiam",
        "description": "Lunch",
        "category": "food",
    }
    conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parser_name, parser_version, raw_text, "
        "parsed_payload, parse_status) VALUES "
        "('prop_d2_bridge', 'text', 'intake_d2_bridge', 'test', '1', 'lunch', ?, "
        "'parsed_pending_confirmation')",
        (json.dumps(payload),),
    )
    parser_output_id = int(
        conn.execute("SELECT id FROM parser_outputs WHERE public_id = 'prop_d2_bridge'").fetchone()[
            0
        ]
    )
    conn.execute(
        "INSERT INTO raw_intake_records "
        "(public_id, source_type, raw_input, received_at, status, parser_output_id) "
        "VALUES ('intake_d2_bridge', 'telegram_text', 'lunch', datetime('now'), "
        "'parsed_pending_confirmation', ?)",
        (parser_output_id,),
    )
    content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
    reference_material = b"d2-bridge-start-reference"
    conn.execute(
        "INSERT INTO openclaw_human_action_references "
        "(reference_public_id, reference_sha256, issuance_idempotency_key, parser_output_id, "
        "action, proposal_version, proposal_content_hash, authenticated_actor_id, channel, "
        "channel_account_id, channel_conversation_id, conversation_binding_id, ttl_seconds, "
        "expires_at, issued_at) VALUES (?, ?, ?, ?, 'edit', 0, ?, ?, 'telegram', ?, ?, ?, "
        "3600, ?, datetime('now'))",
        (
            "haref_d2b10123456789abcdef0123456789ab",
            hashlib.sha256(reference_material).hexdigest(),
            "bridge-human-action-issue:" + "a" * 32,
            parser_output_id,
            content_hash,
            ACTOR,
            ACCOUNT,
            CONVERSATION,
            BINDING,
            now + 3600,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE action = 'edit'"
    ).fetchone()
    assert row is not None
    callback_hash = hashlib.sha256(b"d2-bridge-start").hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE id = ?", (row["id"],)
    ).fetchone()
    conn.execute(
        "INSERT INTO openclaw_human_action_redemptions "
        "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
        "VALUES (?, ?, 100, datetime('now'))",
        (row["id"], callback_hash),
    )
    started = human_drafts.begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=int(row["id"]),
        reference_public_id=str(row["reference_public_id"]),
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id="d1start_d2_bridge",
        redemption_material_hash=callback_hash,
        now_epoch=now,
    )
    conn.commit()
    fields = {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-09-13",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    text = (
        f"Card Ref: {started.card_generation_public_id}\nAmount: 12.50\nCurrency: SGD\n"
        "Date: 2026-09-13\nMerchant: Cafe\nDescription: Lunch\nCategory: food"
    )
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: now + 1)
    published = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_d2_bridge_publish",
            ACTOR,
            ACCOUNT,
            CONVERSATION,
            BINDING,
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    conn.close()
    return (
        published.card_generation_public_id,
        published.proposal_public_id,
        published.proposal_version,
        published.proposal_content_hash,
        published.action_issue_batch_id,
    )


def _context(workspace: support.BridgeWorkspace) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "operator_actor_id": ACTOR,
        "telegram_account_id": ACCOUNT,
        "telegram_conversation_id": CONVERSATION,
        "conversation_binding_id": BINDING,
    }


def test_one_confirm_posts_once_and_status_recovers_same_result(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_id, proposal_id, version, content_hash, batch_id = _published_text_card(
        workspace, monkeypatch
    )
    context = _context(workspace)
    prepared = support.run_cli(
        support.make_request(
            "prepare_posting_review",
            {**context, "card_generation_public_id": card_id},
            idempotency_key=support.canonical_prepare_posting_review_key(card_id),
        )
    )
    assert prepared.exit_code == bridge_errors.EXIT_OK, prepared.response
    review = prepared.response["result"]
    assert review["proposal_public_id"] == proposal_id
    assert review["visible_projection"] == {
        "account": "unspecified",
        "amount": "12.50",
        "currency": "SGD",
        "merchant": "Cafe",
        "transaction_date": "2026-09-13",
    }
    review_id = str(review["review_public_id"])

    confirm_action = support.run_cli(
        support.make_request(
            "issue_posting_review_actions",
            {**context, "posting_review_public_id": review_id},
            idempotency_key=support.canonical_issue_posting_review_actions_key(review_id),
        )
    )
    assert confirm_action.exit_code == bridge_errors.EXIT_OK, confirm_action.response
    manifest = confirm_action.response["result"]
    assert [control["action"] for control in manifest["controls"]] == [
        "confirm",
        "edit",
        "reject",
    ]
    reference = str(manifest["controls"][0]["callback_value"]).removeprefix("post:")
    receipt_stdout = io.StringIO()
    receipt_stderr = io.StringIO()
    receipt_exit = delivery_receipt_cli.execute_stream(
        io.BytesIO(
            json.dumps(
                {
                    "workspace_path": str(workspace.workspace_path),
                    "attempt_nonce": manifest["delivery_attempt_nonce"],
                    "capability": "telegram.finance-delivery-material-v1",
                    "delivery_material_version": "finance_d2_delivery_material_v1",
                    "delivery_material_sha256": manifest[
                        "finance_delivery_material_sha256"
                    ],
                    "provider_message_id": "200",
                    "receipt_token_sha256": hashlib.sha256(
                        b"bridge-receipt-200"
                    ).hexdigest(),
                    "channel": "telegram",
                    "account_id": ACCOUNT,
                    "conversation_id": CONVERSATION,
                    "session_key": BINDING,
                    "source_identity_sha256": "b" * 64,
                }
            ).encode()
        ),
        receipt_stdout,
        receipt_stderr,
    )
    assert receipt_exit == 0, receipt_stderr.getvalue()
    assert json.loads(receipt_stdout.getvalue())["status"] == "ok"

    awaiting = support.run_cli(
        support.make_request("get_status", {**context, "short_reference": reference})
    )
    assert awaiting.response["result"]["state"] == "awaiting_confirmation"
    assert awaiting.response["result"]["final_transaction_created"] is False

    callback_id = "d2-bridge-confirm-1"
    confirmed = support.run_cli(
        support.make_request(
            "confirm_and_post",
            {
                **context,
                "short_reference": reference,
                "callback_id": callback_id,
                "callback_message_id": 200,
            },
            idempotency_key=support.canonical_confirm_and_post_key(callback_id),
        )
    )
    assert confirmed.exit_code == bridge_errors.EXIT_OK, confirmed.response
    result = confirmed.response["result"]
    assert result["state"] == "finalized"
    assert result["final_transaction_created"] is True
    assert result["amount"] == "12.50"
    assert result["currency"] == "SGD"
    assert result["account"] == "unspecified"

    recovered = support.run_cli(
        support.make_request("get_status", {**context, "short_reference": reference})
    )
    assert recovered.exit_code == bridge_errors.EXIT_OK
    assert recovered.response["result"]["transaction_public_id"] == result["transaction_public_id"]
    replay = support.run_cli(
        support.make_request(
            "confirm_and_post",
            {
                **context,
                "short_reference": reference,
                "callback_id": callback_id,
                "callback_message_id": 200,
            },
            idempotency_key=support.canonical_confirm_and_post_key(callback_id),
        )
    )
    assert replay.exit_code == bridge_errors.EXIT_OK
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"]["transaction_public_id"] == result["transaction_public_id"]

    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM d2_posting_decisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_personal_total_receipt_uses_python_card_and_posts_once(
    workspace: support.BridgeWorkspace,
) -> None:
    conn = support.open_database(workspace)
    try:
        seed_people(conn)
        parser_output_id, proposal_public_id = seed_receipt_proposal(
            conn,
            workspace.workspace_path.parent,
            "d2_bridge_personal_receipt",
        )
        conn.execute(
            "UPDATE raw_intake_records SET source_message_id = '78', "
            "external_source_id = 'telegram:111:78' WHERE parser_output_id = ?",
            (parser_output_id,),
        )
        intake = conn.execute(
            "SELECT id FROM raw_intake_records WHERE parser_output_id = ?",
            (parser_output_id,),
        ).fetchone()
        assert intake is not None
        record_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=ACTOR,
                account_id=ACCOUNT,
                conversation_id=CONVERSATION,
                binding_id=BINDING,
                message_id="78",
            ),
            captured_at="2026-09-21T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()

    context = _context(workspace)
    prepared = support.run_cli(
        support.make_request(
            "prepare_posting_review",
            {
                **context,
                "proposal_public_id": proposal_public_id,
                "admitted_source_message_id": "78",
            },
            idempotency_key=(
                f"bridge-d2-prepare-initial:{proposal_public_id}:78"
            ),
        )
    )
    assert prepared.exit_code == bridge_errors.EXIT_OK, prepared.response
    review = prepared.response["result"]
    assert review["posting_path"] == "personal_receipt"
    assert review["initial_card_public_id"].startswith("d2card_")
    for line in (
        "Source: Receipt",
        "Receipt total:",
        "Your share:",
        "Collectible from others: 0.00",
        "Settlement obligations: none",
        "No itemization, tax, fee, or shared allocation will be inferred.",
    ):
        assert line in review["presentation_text"]

    conn = support.open_database(workspace)
    try:
        for table in (
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
            "d2_conditional_authorization_proofs",
            "transactions",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()

    review_id = str(review["review_public_id"])
    issued = support.run_cli(
        support.make_request(
            "issue_posting_review_actions",
            {**context, "posting_review_public_id": review_id},
            idempotency_key=support.canonical_issue_posting_review_actions_key(review_id),
        )
    )
    assert issued.exit_code == bridge_errors.EXIT_OK, issued.response
    manifest = issued.response["result"]
    assert manifest["text"] == review["presentation_text"]
    reference = str(manifest["controls"][0]["callback_value"]).removeprefix("post:")

    receipt_stdout = io.StringIO()
    receipt_stderr = io.StringIO()
    receipt_exit = delivery_receipt_cli.execute_stream(
        io.BytesIO(
            json.dumps(
                {
                    "workspace_path": str(workspace.workspace_path),
                    "attempt_nonce": manifest["delivery_attempt_nonce"],
                    "capability": "telegram.finance-delivery-material-v1",
                    "delivery_material_version": "finance_d2_delivery_material_v1",
                    "delivery_material_sha256": manifest[
                        "finance_delivery_material_sha256"
                    ],
                    "provider_message_id": "930",
                    "receipt_token_sha256": hashlib.sha256(
                        b"bridge-receipt-930"
                    ).hexdigest(),
                    "channel": "telegram",
                    "account_id": ACCOUNT,
                    "conversation_id": CONVERSATION,
                    "session_key": BINDING,
                    "source_identity_sha256": "c" * 64,
                }
            ).encode()
        ),
        receipt_stdout,
        receipt_stderr,
    )
    assert receipt_exit == bridge_errors.EXIT_OK, receipt_stderr.getvalue()

    callback_id = "d2-bridge-receipt-confirm"
    confirmed = support.run_cli(
        support.make_request(
            "confirm_and_post",
            {
                **context,
                "short_reference": reference,
                "callback_id": callback_id,
                "callback_message_id": 930,
            },
            idempotency_key=support.canonical_confirm_and_post_key(callback_id),
        )
    )
    assert confirmed.exit_code == bridge_errors.EXIT_OK, confirmed.response
    result = confirmed.response["result"]
    assert result["state"] == "finalized"
    assert result["final_transaction_created"] is True

    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM receipt_item_allocation_fact_sets"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM authoritative_calculation_snapshots"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM d2_conditional_authorization_proofs"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_posting_status_reference_is_context_bound(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_id, _proposal_id, _version, _content_hash, _batch_id = _published_text_card(
        workspace, monkeypatch
    )
    context = _context(workspace)
    prepared = support.run_cli(
        support.make_request(
            "prepare_posting_review",
            {**context, "card_generation_public_id": card_id},
            idempotency_key=support.canonical_prepare_posting_review_key(card_id),
        )
    )
    review_id = str(prepared.response["result"]["review_public_id"])
    issued = support.run_cli(
        support.make_request(
            "issue_posting_review_actions",
            {**context, "posting_review_public_id": review_id},
            idempotency_key=support.canonical_issue_posting_review_actions_key(review_id),
        )
    )
    reference = str(issued.response["result"]["controls"][0]["callback_value"]).removeprefix(
        "post:"
    )
    refused = support.run_cli(
        support.make_request(
            "get_status",
            {**context, "conversation_binding_id": "binding-other", "short_reference": reference},
        )
    )
    assert refused.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == bridge_errors.FINALIZATION_REFUSED
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        conn.close()
