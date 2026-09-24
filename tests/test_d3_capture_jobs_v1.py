"""D3 capture source/job transaction and replay tests on synthetic databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.capture_jobs import ensure_capture_job, get_capture_job
from finance_core.intake.telegram_text_adapter import process_telegram_text_update
from finance_core.openclaw_staging_bridge import commands, errors, identity
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.staging_guard import create_staging_database


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _text_request(workspace: support.BridgeWorkspace, text: str = "lunch 12.50") -> dict:
    return support.make_request(
        "capture",
        support.capture_text_arguments(workspace, support.telegram_text_update(text)),
        idempotency_key=support.canonical_capture_key(message_id=10),
    )


def _receipt_request(workspace: support.BridgeWorkspace) -> dict:
    return support.make_request(
        "capture",
        support.capture_receipt_arguments(workspace, handoff_filename="d3.jpg"),
        idempotency_key=support.canonical_capture_key(message_id=20),
    )


def _ingress(*, message_id: int, attachment_hash: str | None = None) -> dict:
    identity = {
        "channel": "telegram",
        "accountId": "finance",
        "updateId": 41,
        "chatId": 111,
        "messageId": message_id,
        "senderId": 111,
        "bindingId": "bind-1",
        "payloadSha256": "a" * 64,
    }
    if attachment_hash is not None:
        identity["attachmentSha256"] = attachment_hash
    return identity


def _with_authenticated_ingress(request: dict, ingress: dict) -> dict:
    request["arguments"].update(
        {
            "authenticated_actor_id": "111",
            "telegram_account_id": "finance",
            "telegram_conversation_id": "111",
            "conversation_binding_id": "bind-1",
            "finance_ingress": ingress,
        }
    )
    if request["arguments"]["kind"] == "receipt_image":
        request["arguments"]["telegram_update_id"] = 41
    else:
        request["arguments"]["telegram_update"]["update_id"] = 41
    return request


def test_text_job_commits_with_raw_intake_and_replays_same_identity(
    workspace: support.BridgeWorkspace,
) -> None:
    first = support.run_cli(_text_request(workspace))
    replay = support.run_cli(_text_request(workspace))
    assert first.exit_code == replay.exit_code == errors.EXIT_OK
    first_result = first.response["result"]
    replay_result = replay.response["result"]
    assert first_result["capture_job"]["status"] == "captured"
    assert replay_result["capture_job"]["public_id"] == first_result["capture_job"]["public_id"]
    status = support.run_cli(
        support.make_request(
            "get_status",
            {
                "workspace_path": str(workspace.workspace_path),
                "job_public_id": first_result["capture_job"]["public_id"],
            },
        )
    )
    assert status.exit_code == errors.EXIT_OK
    assert (
        status.response["result"]["capture_job"]["intake_public_id"]
        == first_result["intake_public_id"]
    )
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_text_job_write_failure_rolls_back_raw_intake(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = commands.ensure_capture_job

    def fail_job(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("synthetic job disk failure")

    monkeypatch.setattr(commands, "ensure_capture_job", fail_job)
    failure = support.run_cli(_text_request(workspace))
    assert failure.exit_code != errors.EXIT_OK
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM raw_intake_records").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 0
    monkeypatch.setattr(commands, "ensure_capture_job", original)
    assert support.run_cli(_text_request(workspace)).exit_code == errors.EXIT_OK


def test_receipt_job_and_attachment_source_commit_together_and_replay(
    workspace: support.BridgeWorkspace,
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    first = support.run_cli(_receipt_request(workspace))
    replay = support.run_cli(_receipt_request(workspace))
    assert first.exit_code == replay.exit_code == errors.EXIT_OK
    first_job = first.response["result"]["capture_job"]
    assert first_job["status"] == "captured"
    assert first_job["attachment_content_hash"] == support.sha256_hex(support.JPEG_BYTES)
    assert replay.response["result"]["capture_job"]["public_id"] == first_job["public_id"]
    with support.open_database(workspace) as conn:
        job = get_capture_job(conn, public_id=first_job["public_id"])
        assert job is not None and job["attachment_evidence_id"] is not None
        assert conn.execute("SELECT count(*) FROM telegram_attachment_source").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_receipt_lost_capture_reply_recovers_by_stable_intake_without_handoff(
    workspace: support.BridgeWorkspace,
) -> None:
    handoff = support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    ingress = _ingress(message_id=20, attachment_hash=support.sha256_hex(support.JPEG_BYTES))
    capture = support.run_cli(_with_authenticated_ingress(_receipt_request(workspace), ingress))
    assert capture.exit_code == errors.EXIT_OK
    # Simulate a lost capture response and an expired local media handoff.
    handoff.unlink()
    stable_intake_id = identity.capture_identities(support.canonical_capture_key(message_id=20))[
        "raw_intake_public_id"
    ]
    status = support.run_cli(
        support.make_request(
            "get_status",
            {"workspace_path": str(workspace.workspace_path), "intake_public_id": stable_intake_id},
        )
    )
    assert status.exit_code == errors.EXIT_OK
    job = status.response["result"]["capture_job"]
    assert (
        job["ingress_identity_digest"]
        == capture.response["result"]["capture_job"]["ingress_identity_digest"]
    )
    assert job["attachment_content_hash"] == support.sha256_hex(support.JPEG_BYTES)
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_receipt_job_write_failure_keeps_original_without_false_adoption(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    original = commands.ensure_capture_job

    def fail_job(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("synthetic job disk failure")

    monkeypatch.setattr(commands, "ensure_capture_job", fail_job)
    failure = support.run_cli(_receipt_request(workspace))
    assert failure.exit_code != errors.EXIT_OK
    with support.open_database(workspace) as conn:
        # Raw intake is a preceding durable boundary; source/job are atomic.
        assert conn.execute("SELECT count(*) FROM raw_intake_records").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM telegram_attachment_source").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 0
    monkeypatch.setattr(commands, "ensure_capture_job", original)
    recovered = support.run_cli(_receipt_request(workspace))
    assert recovered.exit_code == errors.EXIT_OK
    assert recovered.response["result"]["capture_job"]["status"] == "captured"


def test_authenticated_ingress_digest_is_immutable_and_photo_hash_is_checked(
    workspace: support.BridgeWorkspace,
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    ingress = _ingress(message_id=20, attachment_hash=support.sha256_hex(support.JPEG_BYTES))
    request = _with_authenticated_ingress(_receipt_request(workspace), ingress)
    first = support.run_cli(request)
    assert first.exit_code == errors.EXIT_OK, first.response
    expected_digest = hashlib.sha256(
        json.dumps(ingress, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert first.response["result"]["capture_job"]["ingress_identity_digest"] == expected_digest
    replay = support.run_cli(request)
    assert (
        replay.response["result"]["capture_job"]["public_id"]
        == first.response["result"]["capture_job"]["public_id"]
    )
    changed = _with_authenticated_ingress(_receipt_request(workspace), dict(ingress))
    changed["arguments"]["finance_ingress"]["payloadSha256"] = "b" * 64
    refusal = support.run_cli(changed)
    assert refusal.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert refusal.response["error"]["code"] == errors.IDEMPOTENCY_CONFLICT
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_bad_photo_ingress_hash_prevents_job_and_attachment_commit(
    workspace: support.BridgeWorkspace,
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    request = _with_authenticated_ingress(
        _receipt_request(workspace), _ingress(message_id=20, attachment_hash="b" * 64)
    )
    refusal = support.run_cli(request)
    assert refusal.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM telegram_attachment_source").fetchone()[0] == 0


def test_text_ingress_requires_matching_authenticated_actor(
    workspace: support.BridgeWorkspace,
) -> None:
    request = _with_authenticated_ingress(_text_request(workspace), _ingress(message_id=10))
    request["arguments"]["finance_ingress"]["senderId"] = 112
    refusal = support.run_cli(request)
    assert refusal.exit_code == errors.EXIT_AUTHORITY_REFUSED
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM raw_intake_records").fetchone()[0] == 0


def test_text_ingress_replay_requires_same_host_update_digest(
    workspace: support.BridgeWorkspace,
) -> None:
    request = _with_authenticated_ingress(_text_request(workspace), _ingress(message_id=10))
    first = support.run_cli(request)
    assert first.exit_code == errors.EXIT_OK
    assert first.response["result"]["capture_job"]["ingress_identity_digest"] is not None
    changed = _with_authenticated_ingress(_text_request(workspace), _ingress(message_id=10))
    changed["arguments"]["finance_ingress"]["updateId"] = 42
    changed["arguments"]["telegram_update"]["update_id"] = 42
    refusal = support.run_cli(changed)
    assert refusal.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert refusal.response["error"]["code"] == errors.IDEMPOTENCY_CONFLICT
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_migration_052_adds_jobs_without_changing_prior_raw_intake(tmp_path: Path) -> None:
    conn = create_staging_database(
        tmp_path / "prior.sqlite", migration_paths=TEMP_DB_MIGRATION_PATHS[:-1]
    )
    try:
        result = process_telegram_text_update(conn, support.telegram_text_update("meal 7.00"))
        intake = result["intake"]
        old_row = conn.execute(
            "SELECT id, public_id, raw_input, content_fingerprint FROM raw_intake_records "
            "WHERE id = ?",
            (intake["id"],),
        ).fetchone()
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        conn.execute("BEGIN IMMEDIATE")
        job = ensure_capture_job(conn, intake_id=int(intake["id"]), capture_kind="text")
        conn.commit()
        assert job["status"] == "captured"
        assert (
            conn.execute(
                "SELECT id, public_id, raw_input, content_fingerprint FROM raw_intake_records "
                "WHERE id = ?",
                (intake["id"],),
            ).fetchone()
            == old_row
        )
    finally:
        conn.close()
