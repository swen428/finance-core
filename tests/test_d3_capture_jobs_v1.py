"""D3 capture source/job transaction and replay tests on synthetic databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.capture_jobs import ensure_capture_job, get_capture_job
from finance_core.intake.raw_text_repository import (
    RawIntakeIdempotencyConflictError,
    create_raw_intake_record,
)
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


def test_capture_refuses_non_durable_sqlite_journal(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "unsafe.db")
    try:
        assert conn.execute("PRAGMA journal_mode = OFF").fetchone()[0] == "off"
        with pytest.raises(errors.BridgeError):
            commands._require_durable_capture_connection(conn)
    finally:
        conn.close()


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


def test_text_adapter_replay_after_job_preserves_typed_idempotency(
    workspace: support.BridgeWorkspace,
) -> None:
    first = support.run_cli(_text_request(workspace))
    assert first.exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        same = process_telegram_text_update(conn, support.telegram_text_update("lunch 12.50"))
        assert same["intake"]["public_id"] == first.response["result"]["intake_public_id"]
        assert same["parser_output"]["public_id"] == first.response["result"]["proposal_public_id"]
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(conn, support.telegram_text_update("lunch 13.50"))
        assert conn.execute("SELECT count(*) FROM raw_intake_records").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_text_job_collision_does_not_relabel_public_id_conflict_as_idempotency(
    workspace: support.BridgeWorkspace,
) -> None:
    first = support.run_cli(_text_request(workspace))
    assert first.exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
            create_raw_intake_record(
                conn,
                "different message",
                source_channel="telegram",
                source_metadata={"chat_id": "111", "message_id": "999"},
                public_id=first.response["result"]["intake_public_id"],
            )


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


@pytest.mark.parametrize("handoff_missing", [False, True])
@pytest.mark.parametrize(
    "first_caption,replay_caption",
    [
        ("meal receipt", "changed receipt"),
        ("meal receipt", None),
        (None, "meal receipt"),
    ],
)
def test_receipt_replay_rejects_changed_caption_even_without_handoff(
    workspace: support.BridgeWorkspace,
    handoff_missing: bool,
    first_caption: str | None,
    replay_caption: str | None,
) -> None:
    handoff = support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    first = _receipt_request(workspace)
    if first_caption is not None:
        first["arguments"]["caption"] = first_caption
    assert support.run_cli(first).exit_code == errors.EXIT_OK
    if handoff_missing:
        handoff.unlink()
    replay = _receipt_request(workspace)
    if replay_caption is not None:
        replay["arguments"]["caption"] = replay_caption
    refusal = support.run_cli(replay)
    assert refusal.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert refusal.response["error"]["code"] == errors.IDEMPOTENCY_CONFLICT
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1
        expected = first_caption or "[telegram receipt image]"
        assert conn.execute("SELECT raw_input FROM raw_intake_records").fetchone()[0] == expected


def test_receipt_replay_same_canonical_caption_succeeds_without_handoff(
    workspace: support.BridgeWorkspace,
) -> None:
    handoff = support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    assert support.run_cli(_receipt_request(workspace)).exit_code == errors.EXIT_OK
    handoff.unlink()
    replay = _receipt_request(workspace)
    replay["arguments"]["caption"] = "[telegram receipt image]"
    assert support.run_cli(replay).exit_code == errors.EXIT_OK


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
    assert status.response["result"]["capture_attachment_integrity"] == "verified"
    assert (
        job["ingress_identity_digest"]
        == capture.response["result"]["capture_job"]["ingress_identity_digest"]
    )
    assert job["attachment_content_hash"] == support.sha256_hex(support.JPEG_BYTES)
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_receipt_status_does_not_claim_missing_original(
    workspace: support.BridgeWorkspace, damage: str
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    capture = support.run_cli(_receipt_request(workspace))
    assert capture.exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        original = Path(
            conn.execute("SELECT attachment_path FROM raw_intake_records").fetchone()[0]
        )
    if damage == "missing":
        original.unlink()
    else:
        original.chmod(0o600)
        original.write_bytes(b"changed original")
        original.chmod(0o400)
    status = support.run_cli(
        support.make_request(
            "get_status",
            {
                "workspace_path": str(workspace.workspace_path),
                "job_public_id": capture.response["result"]["capture_job"]["public_id"],
            },
        )
    )
    assert status.exit_code == errors.EXIT_OK
    assert status.response["result"]["capture_attachment_integrity"] == "missing"
    assert (
        status.response["result"]["capture_job"]["public_id"]
        == (capture.response["result"]["capture_job"]["public_id"])
    )


def test_receipt_status_rejects_source_linked_to_another_intake(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    first = support.run_cli(_receipt_request(workspace))
    second_request = support.make_request(
        "capture",
        support.capture_receipt_arguments(workspace, handoff_filename="d3.jpg", message_id=21),
        idempotency_key=support.canonical_capture_key(message_id=21),
    )
    second = support.run_cli(second_request)
    assert first.exit_code == second.exit_code == errors.EXIT_OK
    first_job = first.response["result"]["capture_job"]
    second_source_id = second.response["result"]["capture_job"]["attachment_evidence_id"]
    original_get_job = commands.get_capture_job

    def corrupted_job(*args: object, **kwargs: object) -> dict | None:
        job = original_get_job(*args, **kwargs)
        if job is not None and job["public_id"] == first_job["public_id"]:
            return {**job, "attachment_evidence_id": second_source_id}
        return job

    monkeypatch.setattr(commands, "get_capture_job", corrupted_job)
    status = support.run_cli(
        support.make_request(
            "get_status",
            {
                "workspace_path": str(workspace.workspace_path),
                "job_public_id": first_job["public_id"],
            },
        )
    )
    assert status.exit_code == errors.EXIT_OK
    assert status.response["result"]["capture_attachment_integrity"] == "missing"


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
        tmp_path / "prior.sqlite", migration_paths=TEMP_DB_MIGRATION_PATHS[:51]
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


@pytest.mark.parametrize(
    "column,value",
    [
        ("id", 999999),
        ("public_id", "fcj_changed"),
        ("raw_intake_record_id", 999999),
        ("capture_kind", "receipt_image"),
        ("intake_fingerprint", "a" * 64),
        ("attachment_evidence_id", 999999),
        ("attachment_content_hash", "b" * 64),
        ("ingress_identity_digest", "c" * 64),
        ("created_at", "1900-01-01"),
    ],
)
def test_migration_052_freezes_job_identity_but_allows_processing_state(
    workspace: support.BridgeWorkspace, column: str, value: object
) -> None:
    capture = support.run_cli(_text_request(workspace))
    assert capture.exit_code == errors.EXIT_OK
    job_id = capture.response["result"]["capture_job"]["public_id"]
    with support.open_database(workspace) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="capture job source identity"):
            conn.execute(
                f"UPDATE finance_capture_jobs SET {column} = ? WHERE public_id = ?",
                (value, job_id),
            )
        conn.execute(
            "UPDATE finance_capture_jobs SET status = 'processing', "
            "lease_epoch = 1, lease_owner = 'worker', lease_expires_at = 123, "
            "updated_at = 'later' WHERE public_id = ?",
            (job_id,),
        )
        assert tuple(
            conn.execute(
                "SELECT status, lease_epoch FROM finance_capture_jobs WHERE public_id = ?",
                (job_id,),
            ).fetchone()
        ) == ("processing", 1)


def test_migration_052_prevents_job_and_intake_delete_or_replace(
    workspace: support.BridgeWorkspace,
) -> None:
    capture = support.run_cli(_text_request(workspace))
    assert capture.exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        job_id = capture.response["result"]["capture_job"]["public_id"]
        intake_id = capture.response["result"]["intake_public_id"]
        for sql, key, message in [
            ("DELETE FROM finance_capture_jobs WHERE public_id = ?", job_id, "capture job cannot"),
            ("DELETE FROM raw_intake_records WHERE public_id = ?", intake_id, "raw intake cannot"),
            (
                "INSERT OR REPLACE INTO finance_capture_jobs SELECT * "
                "FROM finance_capture_jobs WHERE public_id = ?",
                job_id,
                "capture job identity collision",
            ),
            (
                "INSERT OR REPLACE INTO raw_intake_records SELECT * "
                "FROM raw_intake_records WHERE public_id = ?",
                intake_id,
                "capture raw intake identity collision",
            ),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=message):
                conn.execute(sql, (key,))
        key = conn.execute(
            "SELECT idempotency_key FROM raw_intake_records WHERE public_id = ?",
            (intake_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="idempotency_key collision"):
            conn.execute(
                "INSERT OR REPLACE INTO raw_intake_records "
                "(public_id, source_type, source_channel, raw_input, received_at, "
                "idempotency_key) "
                "VALUES ('replace_attempt', 'telegram_text', 'telegram', "
                "'changed', '2026-01-01', ?)",
                (key,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="capture raw intake source"):
            conn.execute(
                "UPDATE raw_intake_records SET raw_input = 'changed caption' WHERE public_id = ?",
                (intake_id,),
            )
        conn.execute(
            "UPDATE raw_intake_records SET status = 'parsed_pending_confirmation' "
            "WHERE public_id = ?",
            (intake_id,),
        )
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_jobs").fetchone()[0] == 1


def test_migration_052_seals_receipt_caption_before_proposal(
    workspace: support.BridgeWorkspace,
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    capture = support.run_cli(_receipt_request(workspace))
    assert capture.exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        intake_id = capture.response["result"]["intake_public_id"]
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
                (intake_id,),
            ).fetchone()[0]
            is None
        )
        with pytest.raises(sqlite3.IntegrityError, match="capture raw intake source"):
            conn.execute(
                "UPDATE raw_intake_records SET raw_input = 'changed caption' WHERE public_id = ?",
                (intake_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="capture raw intake identity collision"):
            conn.execute(
                "INSERT OR REPLACE INTO raw_intake_records SELECT * "
                "FROM raw_intake_records WHERE public_id = ?",
                (intake_id,),
            )


@pytest.mark.parametrize("case", ["null_hash", "wrong_intake"])
def test_migration_052_rejects_receipt_job_without_matching_source(
    workspace: support.BridgeWorkspace, case: str
) -> None:
    support.write_handoff_file(workspace, "d3.jpg", support.JPEG_BYTES)
    assert support.run_cli(_receipt_request(workspace)).exit_code == errors.EXIT_OK
    with support.open_database(workspace) as conn:
        source_id, source_hash, attachment_id = conn.execute(
            "SELECT id, content_hash, attachment_id FROM telegram_attachment_source"
        ).fetchone()
        conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at, "
            "content_fingerprint, attachment_id, attachment_hash) "
            "VALUES ('other_intake', 'telegram_image', 'telegram', 'image', "
            "'2026-01-01', ?, ?, ?)",
            ("a" * 64, attachment_id, source_hash),
        )
        other_intake_id = conn.execute(
            "SELECT id FROM raw_intake_records WHERE public_id = 'other_intake'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO telegram_attachment_source "
            "(public_id, attachment_id, raw_intake_record_id, "
            "original_attachment_path, observed_file_size, content_hash, "
            "source_evidence_payload) "
            "SELECT 'other_source', attachment_id, ?, original_attachment_path, "
            "observed_file_size, content_hash, source_evidence_payload "
            "FROM telegram_attachment_source WHERE id = ?",
            (other_intake_id, source_id),
        )
        own_source_id = conn.execute(
            "SELECT id FROM telegram_attachment_source WHERE public_id = 'other_source'"
        ).fetchone()[0]
        submitted_hash = None if case == "null_hash" else source_hash
        submitted_source_id = own_source_id if case == "null_hash" else source_id
        if case == "null_hash":
            # Isolate the table CHECK; SQLite runs BEFORE INSERT triggers first.
            conn.execute("DROP TRIGGER trg_finance_capture_jobs_require_receipt_source")
        with pytest.raises(sqlite3.IntegrityError) as rejected:
            conn.execute(
                "INSERT INTO finance_capture_jobs "
                "(public_id, raw_intake_record_id, capture_kind, intake_fingerprint, "
                "attachment_evidence_id, attachment_content_hash) "
                "VALUES (?, ?, 'receipt_image', ?, ?, ?)",
                (case, other_intake_id, "a" * 64, submitted_source_id, submitted_hash),
            )
        if case == "null_hash":
            assert "CHECK constraint failed" in str(rejected.value)
        else:
            assert "receipt capture job source linkage mismatch" in str(rejected.value)
        assert conn.execute("SELECT count(*) FROM finance_capture_jobs").fetchone()[0] == 1
