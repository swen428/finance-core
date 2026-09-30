"""S3-A managed public capture and legacy-path refusal proofs.

All data and files in this module are synthetic and live under pytest's
temporary directory. Receipt capture stops at durable evidence and a capture
job; it does not invoke OCR, a provider, or final-fact processing.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake import attachment_publication
from finance_core.intake.raw_text_repository import TELEGRAM_TEXT, create_raw_intake_record
from finance_core.intake.telegram_attachment_acquisition import (
    StagingDatabaseRejectedError,
    acquire_and_persist_telegram_attachment,
)
from finance_core.openclaw_staging_bridge import envelope, errors, receipt_handoff, workspace_access
from finance_core.receipt_staging_runner.local_intake import (
    import_local_receipt_file,
    run_local_receipt_intake,
)
from finance_core.receipt_staging_runner.models import parse_runner_manifest
from finance_core.receipt_staging_runner.workspace import create_runner_workspace
from finance_core.staging_guard import StagingDatabaseError
from tests import test_receipt_staging_runner_local_intake_v1 as runner_tests
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests import test_telegram_attachment_acquisition as acquisition_tests
from tests.s1cc_managed_test_support import assert_exclusive_gate_released

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)

_COUNTED_TABLES = (
    "raw_intake_records",
    "d2_telegram_source_contexts",
    "telegram_attachment_source",
    "attachments",
    "finance_capture_jobs",
    "finance_capture_interaction_routes",
    "receipt_ocr_extractions",
    "parser_outputs",
)


def _source_fields(
    *, account_id: str = s1ca.ACCOUNT, binding_id: str = s1ca.BINDING
) -> dict[str, object]:
    return {
        "authenticated_actor_id": s1ca.ACTOR,
        "telegram_account_id": account_id,
        "telegram_conversation_id": s1ca.CONVERSATION,
        "conversation_binding_id": binding_id,
    }


def _write_managed_handoff(
    workspace: s1ca.ManagedBridgeWorkspace, filename: str, content: bytes
) -> Path:
    handoff_dir = workspace.workspace_path / "handoff"
    handoff_dir.mkdir(mode=0o700, exist_ok=True)
    path = handoff_dir / filename
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _receipt_arguments(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    message_id: int,
    filename: str,
    content: bytes = support.JPEG_BYTES,
    caption: str = "Receipt from the synthetic cafe",
    account_id: str = s1ca.ACCOUNT,
    binding_id: str = s1ca.BINDING,
) -> dict[str, object]:
    update_id = 10_000 + message_id
    arguments = support.capture_receipt_arguments(
        workspace,  # type: ignore[arg-type]
        handoff_filename=filename,
        update_id=update_id,
        message_id=message_id,
        chat_id=support.SYNTHETIC_CHAT_ID,
        caption=caption,
        declared_mime_type="image/jpeg",
        original_filename=filename,
        sender_id=support.SYNTHETIC_SENDER_ID,
    )
    arguments.update(_source_fields(account_id=account_id, binding_id=binding_id))
    arguments["finance_ingress"] = {
        "channel": "telegram",
        "accountId": account_id,
        "updateId": update_id,
        "chatId": support.SYNTHETIC_CHAT_ID,
        "messageId": message_id,
        "senderId": support.SYNTHETIC_SENDER_ID,
        "payloadSha256": hashlib.sha256(
            f"synthetic-receipt:{message_id}:{caption}".encode("utf-8")
        ).hexdigest(),
        "bindingId": binding_id,
        "attachmentSha256": support.sha256_hex(content),
    }
    return arguments


def _receipt_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    message_id: int,
    filename: str,
    content: bytes = support.JPEG_BYTES,
    caption: str = "Receipt from the synthetic cafe",
    account_id: str = s1ca.ACCOUNT,
    binding_id: str = s1ca.BINDING,
) -> dict[str, Any]:
    return s1ca._request(
        workspace,
        envelope.COMMAND_CAPTURE,
        _receipt_arguments(
            workspace,
            message_id=message_id,
            filename=filename,
            content=content,
            caption=caption,
            account_id=account_id,
            binding_id=binding_id,
        ),
        idempotency_key=support.canonical_capture_key(
            chat_id=support.SYNTHETIC_CHAT_ID, message_id=message_id
        ),
    )


def _text_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    message_id: int,
    text: str = "lunch at the synthetic cafe 12.34",
) -> dict[str, Any]:
    update_id = 20_000 + message_id
    update = support.telegram_text_update(
        text,
        update_id=update_id,
        message_id=message_id,
        chat_id=support.SYNTHETIC_CHAT_ID,
        sender_id=support.SYNTHETIC_SENDER_ID,
    )
    arguments = support.authenticated_text_capture_arguments(
        workspace,  # type: ignore[arg-type]
        update,
        account_id=s1ca.ACCOUNT,
        binding_id=s1ca.BINDING,
        payload_sha256=hashlib.sha256(f"synthetic-text:{text}".encode()).hexdigest(),
    )
    return s1ca._request(
        workspace,
        envelope.COMMAND_CAPTURE,
        arguments,
        idempotency_key=support.canonical_capture_key(
            chat_id=support.SYNTHETIC_CHAT_ID, message_id=message_id
        ),
    )


def _row_counts(workspace: s1ca.ManagedBridgeWorkspace) -> dict[str, int]:
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s3a-row-counts"
    ) as conn:
        available = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        counts = {
            table: (
                int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                if table in available
                else 0
            )
            for table in _COUNTED_TABLES
        }
        counts["final_facts"] = sum(support.count_final_facts(conn).values())
        return counts


def _attachment_files(workspace: s1ca.ManagedBridgeWorkspace) -> tuple[tuple[str, str], ...]:
    root = workspace.workspace_path / "attachments"
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def _assert_generic_refusal(
    outcome: support.CliOutcome,
    *,
    code: str,
    exit_code: int,
    private_values: tuple[str, ...],
) -> None:
    assert outcome.exit_code == exit_code, outcome.response
    assert outcome.response["error"]["code"] == code
    assert "result" not in outcome.response
    rendered = json.dumps(outcome.response, sort_keys=True)
    for value in private_values:
        assert value not in rendered


def test_managed_public_text_capture_and_exact_replay(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    request = _text_request(managed_workspace, message_id=301)
    first = s1ca._run(managed_workspace, request, expected_sessions=2)
    assert first.exit_code == errors.EXIT_OK, first.response
    first_result = first.response["result"]
    assert first_result["interaction_route"]["route_kind"] == "initial_intake"
    assert first_result["capture_job"]["capture_kind"] == "text"
    assert first_result["final_transaction_created"] is False

    replay = s1ca._run(managed_workspace, request, expected_sessions=2)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"]["intake_public_id"] == first_result["intake_public_id"]
    assert (
        replay.response["result"]["capture_job"]["public_id"]
        == first_result["capture_job"]["public_id"]
    )
    counts = _row_counts(managed_workspace)
    assert counts["raw_intake_records"] == 1
    assert counts["d2_telegram_source_contexts"] == 1
    assert counts["finance_capture_jobs"] == 1
    assert counts["finance_capture_interaction_routes"] == 1
    assert counts["parser_outputs"] == 0
    assert counts["final_facts"] == 0
    assert_exclusive_gate_released(managed_workspace)


def test_managed_public_receipt_capture_and_exact_replay(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "managed.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=302, filename=handoff.name)

    first = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert first.exit_code == errors.EXIT_OK, first.response
    result = first.response["result"]
    assert result["capture_kind"] == "receipt_image"
    assert result["capture_job"]["capture_kind"] == "receipt_image"
    assert result["attachment_content_hash"] == support.sha256_hex(support.JPEG_BYTES)
    assert result["final_transaction_created"] is False
    assert str(managed_workspace.workspace_path) not in json.dumps(first.response)
    canonical = (
        managed_workspace.workspace_path
        / "attachments"
        / support.sha256_hex(support.JPEG_BYTES)[:2]
        / f"{support.sha256_hex(support.JPEG_BYTES)}.jpg"
    )
    assert canonical.read_bytes() == support.JPEG_BYTES
    assert handoff.read_bytes() == support.JPEG_BYTES
    assert _row_counts(managed_workspace) == {
        "raw_intake_records": 1,
        "d2_telegram_source_contexts": 1,
        "telegram_attachment_source": 1,
        "attachments": 1,
        "finance_capture_jobs": 1,
        "finance_capture_interaction_routes": 0,
        "receipt_ocr_extractions": 0,
        "parser_outputs": 0,
        "final_facts": 0,
    }

    replay = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"]["intake_public_id"] == result["intake_public_id"]
    assert (
        replay.response["result"]["attachment_evidence_public_id"]
        == result["attachment_evidence_public_id"]
    )
    assert _row_counts(managed_workspace)["telegram_attachment_source"] == 1
    assert _row_counts(managed_workspace)["finance_capture_jobs"] == 1
    assert _attachment_files(managed_workspace) == (
        (
            f"{support.sha256_hex(support.JPEG_BYTES)[:2]}/{support.sha256_hex(support.JPEG_BYTES)}.jpg",
            support.sha256_hex(support.JPEG_BYTES),
        ),
    )
    assert_exclusive_gate_released(managed_workspace)


def test_managed_receipt_wrong_source_and_changed_caption_are_read_only_refusals(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "bound.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=303, filename=handoff.name)
    created = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert created.exit_code == errors.EXIT_OK, created.response
    original_state = _row_counts(managed_workspace)
    original_files = _attachment_files(managed_workspace)

    wrong_source = _receipt_request(
        managed_workspace,
        message_id=303,
        filename=handoff.name,
        account_id="other-synthetic-account",
        binding_id="other-synthetic-binding",
    )
    refused_source = s1ca._run(managed_workspace, wrong_source, expected_sessions=1)
    _assert_generic_refusal(
        refused_source,
        code=errors.IDEMPOTENCY_CONFLICT,
        exit_code=errors.EXIT_AUTHORITY_REFUSED,
        private_values=(str(managed_workspace.workspace_path), handoff.name),
    )
    assert _row_counts(managed_workspace) == original_state
    assert _attachment_files(managed_workspace) == original_files
    assert_exclusive_gate_released(managed_workspace)

    changed_caption = _receipt_request(
        managed_workspace,
        message_id=303,
        filename=handoff.name,
        caption="Receipt for a different synthetic cafe",
    )
    refused_caption = s1ca._run(managed_workspace, changed_caption, expected_sessions=1)
    _assert_generic_refusal(
        refused_caption,
        code=errors.IDEMPOTENCY_CONFLICT,
        exit_code=errors.EXIT_AUTHORITY_REFUSED,
        private_values=(str(managed_workspace.workspace_path), handoff.name),
    )
    assert _row_counts(managed_workspace) == original_state
    assert _attachment_files(managed_workspace) == original_files

    replay = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert_exclusive_gate_released(managed_workspace)


@pytest.mark.parametrize(
    ("case", "expected_code", "expected_exit"),
    [
        ("wrong_hash", errors.IDEMPOTENCY_CONFLICT, errors.EXIT_AUTHORITY_REFUSED),
        ("missing_handoff", errors.HANDOFF_NOT_FOUND, errors.EXIT_VALIDATION_REFUSED),
        ("control_caption", errors.ARGUMENTS_REFUSED, errors.EXIT_VALIDATION_REFUSED),
    ],
)
def test_managed_receipt_pre_effect_refusals_leave_no_durable_rows_or_files(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    case: str,
    expected_code: str,
    expected_exit: int,
) -> None:
    filename = f"{case}.jpg"
    if case != "missing_handoff":
        _write_managed_handoff(managed_workspace, filename, support.JPEG_BYTES)
    request = _receipt_request(
        managed_workspace,
        message_id=304,
        filename=filename,
        caption=("Amount: 12" if case == "control_caption" else "Receipt from the synthetic cafe"),
    )
    if case == "wrong_hash":
        ingress = request["arguments"]["finance_ingress"]
        assert isinstance(ingress, dict)
        ingress["attachmentSha256"] = "0" * 64

    before = len(managed_workspace.sessions)
    outcome = s1ca._run(managed_workspace, request, expected_sessions=1)
    _assert_generic_refusal(
        outcome,
        code=expected_code,
        exit_code=expected_exit,
        private_values=(str(managed_workspace.workspace_path), filename),
    )
    assert managed_workspace.sessions[before].total_changes == 0
    assert _row_counts(managed_workspace) == {
        "raw_intake_records": 0,
        "d2_telegram_source_contexts": 0,
        "telegram_attachment_source": 0,
        "attachments": 0,
        "finance_capture_jobs": 0,
        "finance_capture_interaction_routes": 0,
        "receipt_ocr_extractions": 0,
        "parser_outputs": 0,
        "final_facts": 0,
    }
    assert _attachment_files(managed_workspace) == ()
    if case != "missing_handoff":
        assert (managed_workspace.workspace_path / "handoff" / filename).read_bytes() == (
            support.JPEG_BYTES
        )
    assert_exclusive_gate_released(managed_workspace)


def test_concurrent_managed_receipt_capture_creates_one_durable_lineage(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "concurrent.jpg", support.JPEG_BYTES)
    first_request = _receipt_request(managed_workspace, message_id=305, filename=handoff.name)
    second_request = _receipt_request(managed_workspace, message_id=305, filename=handoff.name)
    assert first_request["request_id"] != second_request["request_id"]

    before = len(managed_workspace.sessions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(support.run_cli, (first_request, second_request)))
    assert all(outcome.exit_code == errors.EXIT_OK for outcome in outcomes), [
        outcome.response for outcome in outcomes
    ]
    assert len({outcome.response["result"]["intake_public_id"] for outcome in outcomes}) == 1
    assert len(managed_workspace.sessions[before:]) == 2
    for observed in managed_workspace.sessions[before:]:
        with pytest.raises(sqlite3.ProgrammingError):
            observed.connection.execute("SELECT 1")
    counts = _row_counts(managed_workspace)
    assert counts["raw_intake_records"] == 1
    assert counts["d2_telegram_source_contexts"] == 1
    assert counts["telegram_attachment_source"] == 1
    assert counts["attachments"] == 1
    assert counts["finance_capture_jobs"] == 1
    assert counts["final_facts"] == 0
    assert len(_attachment_files(managed_workspace)) == 1
    assert_exclusive_gate_released(managed_workspace)


def test_managed_receipt_reuses_published_orphan_after_persistence_crash(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "orphan.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=306, filename=handoff.name)
    real_publish = attachment_publication.publish_no_overwrite

    def crash_after_publish(root: object, **kwargs: object) -> tuple[str, bool]:
        real_publish(root, **kwargs)  # type: ignore[arg-type]
        raise attachment_publication.DurablePublicationError("synthetic crash after publication")

    with monkeypatch.context() as patch:
        patch.setattr(attachment_publication, "publish_no_overwrite", crash_after_publish)
        first = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert first.exit_code == errors.EXIT_INTERNAL, first.response
    assert first.response["error"]["code"] == errors.HANDOFF_REFUSED
    assert handoff.read_bytes() == support.JPEG_BYTES
    counts = _row_counts(managed_workspace)
    assert counts["raw_intake_records"] == 1
    assert counts["d2_telegram_source_contexts"] == 1
    assert counts["telegram_attachment_source"] == 0
    assert counts["finance_capture_jobs"] == 0
    assert len(_attachment_files(managed_workspace)) == 1
    assert_exclusive_gate_released(managed_workspace)

    replay = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["result"]["durable_file_reused"] is True
    assert replay.response["result"]["capture_job"]["capture_kind"] == "receipt_image"
    assert _row_counts(managed_workspace)["telegram_attachment_source"] == 1
    assert _row_counts(managed_workspace)["finance_capture_jobs"] == 1
    assert handoff.read_bytes() == support.JPEG_BYTES
    assert_exclusive_gate_released(managed_workspace)


def test_managed_receipt_lost_response_after_evidence_commit_replays_once(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "lost-response.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=307, filename=handoff.name)
    real_persist = receipt_handoff.persist_attachment_evidence

    def lose_response(*args: object, **kwargs: object) -> object:
        real_persist(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("synthetic lost response after evidence commit")

    with monkeypatch.context() as patch:
        patch.setattr(receipt_handoff, "persist_attachment_evidence", lose_response)
        first = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert first.exit_code == errors.EXIT_INTERNAL, first.response
    assert handoff.read_bytes() == support.JPEG_BYTES
    assert _row_counts(managed_workspace)["telegram_attachment_source"] == 1
    assert _row_counts(managed_workspace)["finance_capture_jobs"] == 1
    assert_exclusive_gate_released(managed_workspace)

    replay = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["idempotent_replay"] is True
    assert _row_counts(managed_workspace)["raw_intake_records"] == 1
    assert _row_counts(managed_workspace)["telegram_attachment_source"] == 1
    assert _row_counts(managed_workspace)["finance_capture_jobs"] == 1
    assert handoff.read_bytes() == support.JPEG_BYTES
    assert_exclusive_gate_released(managed_workspace)


def _wait_for_child_ready(process: subprocess.Popen[str], ready_path: Path) -> None:
    deadline = time.monotonic() + 4.0
    while not ready_path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"lock-holder exited before becoming ready: {stderr or stdout}")
        time.sleep(0.01)
    assert ready_path.exists(), "lock-holder did not acquire its lock before the bounded timeout"


def _stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    stdout, stderr = process.communicate(timeout=4)
    assert process.returncode in (0, -15), stderr or stdout


def test_managed_receipt_refuses_before_effects_when_profile_gate_is_held(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    tmp_path: Path,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "profile-gate.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=308, filename=handoff.name)
    ready_path = tmp_path / "profile-gate-ready"
    child = """
import sys
import time
from pathlib import Path
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import validate_registered_staging_profile
support_root, profile_id, ready = sys.argv[1:4]
profile = validate_registered_staging_profile(support_root, profile_id)
try:
    with exclusive_cut(profile, timeout_seconds=0):
        Path(ready).write_text("held", encoding="utf-8")
        time.sleep(10)
finally:
    profile.close()
"""
    source_root = Path(__file__).resolve().parents[1]
    support_root = managed_workspace.profile_base.parent.parent.parent
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child,
            str(support_root),
            managed_workspace.profile_base.name,
            str(ready_path),
        ],
        cwd=source_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_child_ready(process, ready_path)
        before_sessions = len(managed_workspace.sessions)
        started = time.monotonic()
        outcome = support.run_cli(
            request,
            deadline_seconds=0.4,
        )
        elapsed = time.monotonic() - started
        assert managed_workspace.sessions[before_sessions:] == []
        assert outcome.exit_code == errors.EXIT_DEADLINE_EXCEEDED, outcome.response
        assert outcome.response["error"]["code"] == errors.DEADLINE_EXCEEDED
        assert elapsed < 2.0, f"capture outlived its short profile-gate deadline: {elapsed:.3f}s"
    finally:
        _stop_child(process)

    assert _row_counts(managed_workspace)["raw_intake_records"] == 0
    assert _attachment_files(managed_workspace) == ()
    assert handoff.read_bytes() == support.JPEG_BYTES
    assert_exclusive_gate_released(managed_workspace)
    retry = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert retry.exit_code == errors.EXIT_OK, retry.response


def test_managed_publication_lock_wait_uses_remaining_bridge_deadline(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "publication-lock.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=309, filename=handoff.name)
    storage_root = managed_workspace.workspace_path / "attachments"
    lock_fd = os.open(storage_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(receipt_handoff, "_PUBLICATION_DEADLINE_SECONDS", 2.0)
    try:
        started = time.monotonic()
        outcome = support.run_cli(request, deadline_seconds=0.5)
        elapsed = time.monotonic() - started
        assert outcome.exit_code == errors.EXIT_DEADLINE_EXCEEDED, outcome.response
        assert outcome.response["error"]["code"] == errors.DEADLINE_EXCEEDED
        assert elapsed < 1.5, f"publication lock outlived its owner deadline: {elapsed:.3f}s"
        counts = _row_counts(managed_workspace)
        assert counts["raw_intake_records"] == 1
        assert counts["d2_telegram_source_contexts"] == 1
        assert counts["telegram_attachment_source"] == 0
        assert counts["attachments"] == 0
        assert counts["finance_capture_jobs"] == 0
        assert counts["final_facts"] == 0
        assert _attachment_files(managed_workspace) == ()
        assert handoff.read_bytes() == support.JPEG_BYTES
        assert_exclusive_gate_released(managed_workspace)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    retry = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert retry.exit_code == errors.EXIT_OK, retry.response
    assert _row_counts(managed_workspace)["telegram_attachment_source"] == 1
    assert_exclusive_gate_released(managed_workspace)


def _spawn_sqlite_writer(db_path: Path, ready_path: Path) -> subprocess.Popen[str]:
    writer = """
import sqlite3
import sys
import time
from pathlib import Path
database, ready = sys.argv[1:3]
connection = sqlite3.connect(database, timeout=1.0)
try:
    connection.execute("BEGIN IMMEDIATE")
    Path(ready).write_text("held", encoding="utf-8")
    time.sleep(10)
finally:
    connection.rollback()
    connection.close()
"""
    return subprocess.Popen(
        [sys.executable, "-c", writer, str(db_path), str(ready_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_managed_receipt_sqlite_busy_wait_uses_remaining_bridge_budget(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = _write_managed_handoff(managed_workspace, "busy-writer.jpg", support.JPEG_BYTES)
    request = _receipt_request(managed_workspace, message_id=310, filename=handoff.name)
    db_path = workspace_access.database_path_for(managed_workspace.workspace_path)
    ready_path = tmp_path / "sqlite-writer-ready"
    real_persist = receipt_handoff.persist_attachment_evidence
    real_configure = receipt_handoff.configure_sqlite_connection
    configured_timeouts: list[float] = []
    writers: list[subprocess.Popen[str]] = []

    def configure_with_observation(conn: sqlite3.Connection, *, timeout_seconds: float) -> None:
        configured_timeouts.append(timeout_seconds)
        real_configure(conn, timeout_seconds=timeout_seconds)

    def persist_while_separate_process_holds_writer(*args: object, **kwargs: object) -> object:
        writer = _spawn_sqlite_writer(db_path, ready_path)
        writers.append(writer)
        try:
            _wait_for_child_ready(writer, ready_path)
            return real_persist(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            _stop_child(writer)

    before = len(managed_workspace.sessions)
    started = time.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(receipt_handoff, "configure_sqlite_connection", configure_with_observation)
        patch.setattr(
            receipt_handoff,
            "persist_attachment_evidence",
            persist_while_separate_process_holds_writer,
        )
        outcome = support.run_cli(request, deadline_seconds=0.9)
    elapsed = time.monotonic() - started

    assert outcome.exit_code == errors.EXIT_DEADLINE_EXCEEDED, outcome.response
    assert outcome.response["error"]["code"] == errors.DEADLINE_EXCEEDED
    assert configured_timeouts and 0 < configured_timeouts[0] < 0.9
    assert len(writers) == 1
    assert elapsed < 4.5, (
        f"SQLite used a fresh timeout instead of the Bridge budget: {elapsed:.3f}s"
    )
    opened = managed_workspace.sessions[before:]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].connection.execute("SELECT 1")
    counts = _row_counts(managed_workspace)
    assert counts["raw_intake_records"] == 1
    assert counts["d2_telegram_source_contexts"] == 1
    assert counts["telegram_attachment_source"] == 0
    assert counts["attachments"] == 0
    assert counts["finance_capture_jobs"] == 0
    assert counts["final_facts"] == 0
    assert handoff.read_bytes() == support.JPEG_BYTES
    assert len(_attachment_files(managed_workspace)) == 1
    assert_exclusive_gate_released(managed_workspace)

    replay = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert replay.exit_code == errors.EXIT_OK, replay.response
    assert replay.response["result"]["durable_file_reused"] is True
    assert _row_counts(managed_workspace)["telegram_attachment_source"] == 1
    assert _row_counts(managed_workspace)["finance_capture_jobs"] == 1
    assert_exclusive_gate_released(managed_workspace)


def _seed_managed_raw_intake(workspace: s1ca.ManagedBridgeWorkspace) -> int:
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s3a-seed-legacy-intake"
    ) as conn:
        intake = create_raw_intake_record(
            conn,
            "synthetic raw intake for legacy boundary refusal",
            source_type=TELEGRAM_TEXT,
            source_channel="telegram",
            source_metadata={"chat_id": "111", "message_id": "999"},
            public_id="raw_s3a_legacy_boundary",
        )
        conn.commit()
        return int(intake["id"])


def test_legacy_telegram_downloader_refuses_managed_connection_before_transport(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    raw_intake_id = _seed_managed_raw_intake(managed_workspace)
    transport = acquisition_tests.FakeTransport()
    storage_root = managed_workspace.workspace_path / "attachments"
    before_counts = _row_counts(managed_workspace)
    before_files = _attachment_files(managed_workspace)
    before = len(managed_workspace.sessions)

    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s3a-managed-downloader-refusal"
    ) as conn:
        with pytest.raises(StagingDatabaseRejectedError, match="authorised staging database"):
            acquire_and_persist_telegram_attachment(
                conn,
                transport=transport,
                storage_root=storage_root,
                public_id="tgae_s3a_managed_boundary",
                raw_intake_id=raw_intake_id,
                telegram_file_id="synthetic-file-id",
                telegram_file_unique_id="synthetic-file-unique-id",
                original_filename="receipt.pdf",
                declared_mime_type="application/pdf",
            )

    opened = managed_workspace.sessions[before:]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].connection.execute("SELECT 1")
    assert transport.metadata_calls == 0
    assert transport.download_calls == 0
    assert _row_counts(managed_workspace) == before_counts
    assert _attachment_files(managed_workspace) == before_files
    assert_exclusive_gate_released(managed_workspace)


def test_legacy_manual_runner_refuses_managed_connection_before_copy_or_ocr(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = parse_runner_manifest(runner_tests._manifest_bytes())
    runner_workspace = create_runner_workspace(str(tmp_path / "legacy-runner"), manifest)
    source = tmp_path / "synthetic-receipt.jpg"
    source.write_bytes(support.JPEG_BYTES)
    copy_calls: list[str] = []
    extract_calls: list[str] = []

    def observe_copy(*args: object, **kwargs: object) -> object:
        copy_calls.append("called")
        return import_local_receipt_file(*args, **kwargs)  # type: ignore[arg-type]

    engine = runner_tests._ok_engine()

    def forbidden_extract(*args: object, **kwargs: object) -> object:
        extract_calls.append("called")
        raise AssertionError("managed refusal must happen before OCR")

    monkeypatch.setattr(
        "finance_core.receipt_staging_runner.local_intake.import_local_receipt_file",
        observe_copy,
    )
    monkeypatch.setattr(engine, "extract", forbidden_extract)
    before_counts = _row_counts(managed_workspace)
    before = len(managed_workspace.sessions)

    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s3a-managed-runner-refusal"
    ) as conn:
        with pytest.raises(StagingDatabaseError, match="managed staging profile"):
            run_local_receipt_intake(
                conn,
                workspace=runner_workspace,
                manifest=manifest,
                source_image_path=str(source),
                engine=engine,
                public_id_prefix="s3a_managed_refusal",
            )

    opened = managed_workspace.sessions[before:]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].connection.execute("SELECT 1")
    assert copy_calls == []
    assert extract_calls == []
    assert _row_counts(managed_workspace) == before_counts
    assert list(Path(runner_workspace.attachments_path).iterdir()) == []
    assert source.read_bytes() == support.JPEG_BYTES
    assert_exclusive_gate_released(managed_workspace)
