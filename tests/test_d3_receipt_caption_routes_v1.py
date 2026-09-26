"""Receipt captions cannot smuggle a human control message into OCR intake."""

from __future__ import annotations

from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import commands, errors


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _receipt_request(workspace: support.BridgeWorkspace, caption: str) -> dict:
    support.write_handoff_file(workspace, "caption.jpg", support.JPEG_BYTES)
    arguments = support.capture_receipt_arguments(
        workspace,
        handoff_filename="caption.jpg",
        caption=caption,
        declared_mime_type="image/jpeg",
    )
    arguments.update(
        {
            "authenticated_actor_id": "111",
            "telegram_account_id": "finance-account",
            "telegram_conversation_id": "111",
            "conversation_binding_id": "binding-1",
            "finance_ingress": {
                "channel": "telegram",
                "accountId": "finance-account",
                "updateId": 1,
                "chatId": 111,
                "messageId": 20,
                "senderId": 111,
                "payloadSha256": "a" * 64,
                "bindingId": "binding-1",
                "attachmentSha256": support.sha256_hex(support.JPEG_BYTES),
            },
        }
    )
    return support.make_request(
        "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=20)
    )


@pytest.mark.parametrize(
    "caption",
    ["Amount: 12", "amount=12", "完成", "Card Ref: d1card_" + "a" * 32],
)
def test_new_receipt_control_caption_is_refused_before_intake(
    workspace: support.BridgeWorkspace, caption: str
) -> None:
    refused = support.run_cli(_receipt_request(workspace, caption))
    assert refused.exit_code == errors.EXIT_VALIDATION_REFUSED, refused.response
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_jobs").fetchone()[0] == 0


def test_historical_receipt_replay_keeps_original_caption_without_reclassification(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _receipt_request(workspace, "Amount: 12")
    with monkeypatch.context() as patch:
        patch.setattr(commands, "classify_control_text_shape", lambda _text: ("ordinary", _text))
        first = support.run_cli(request)
    assert first.exit_code == errors.EXIT_OK, first.response
    replay = support.run_cli(request)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert (
        replay.response["result"]["intake_public_id"]
        == first.response["result"]["intake_public_id"]
    )
