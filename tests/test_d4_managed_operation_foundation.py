"""Public Bridge behavior for the synthetic managed-staging boundary."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.managed_staging_profile import bootstrap_registered_staging
from finance_core.openclaw_staging_bridge import delivery_receipt_cli, envelope, workspace_access
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge.delivery_receipt_proof import PROOF_VERSION
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import ManagedStagingProfile, ProfilePaths
from tests.test_managed_staging_profile import _blank_profile


@dataclass(frozen=True)
class ManagedWorkspace:
    profile_base: Path
    workspace_path: Path
    witness: ManagedStagingProfile


@pytest.fixture()
def managed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ManagedWorkspace]:
    support_root, profile_base, blank_value = _blank_profile(
        tmp_path, monkeypatch, profile_id="bridge-managed"
    )
    del support_root
    workspace_path = profile_base / "workspace"
    for name in ("attachments", "runtime", "evidence"):
        (workspace_path / name).mkdir(mode=0o700)
    blank = cast(ProfilePaths, blank_value)
    witness = bootstrap_registered_staging(blank)
    try:
        yield ManagedWorkspace(profile_base, workspace_path, witness)
    finally:
        witness.close()
        blank.close()


def _snapshot_tree(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    result: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result.append((relative, "symlink", 0, path.readlink().as_posix()))
        elif path.is_dir():
            result.append((relative, "directory", path.stat().st_mode & 0o777, ""))
        else:
            result.append(
                (
                    relative,
                    "file",
                    path.stat().st_mode & 0o777,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return tuple(result)


def _health(workspace_path: Path) -> support.CliOutcome:
    return support.run_cli(support.make_request("health", {"workspace_path": str(workspace_path)}))


def _assert_managed_refusal(outcome: support.CliOutcome) -> None:
    assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED, outcome.response
    assert outcome.response["error"]["code"] == bridge_errors.STAGING_REFUSED


def _insert_status_intake(workspace_path: Path) -> None:
    with workspace_access.workspace_database_session(
        workspace_path, operation_id="test-seed-status"
    ) as conn:
        conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at) "
            "VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z')",
            ("d4-managed-status", "synthetic managed-profile status check"),
        )
        conn.commit()


def test_managed_health_and_status_are_available_through_the_public_cli(
    managed_workspace: ManagedWorkspace,
) -> None:
    _insert_status_intake(managed_workspace.workspace_path)

    health = support.run_cli(
        support.make_request("health", {"workspace_path": str(managed_workspace.workspace_path)})
    )
    assert health.exit_code == bridge_errors.EXIT_OK, health.response
    assert health.response["result"]["workspace_verified"] is True
    assert health.response["result"]["database_verified"] is True

    status = support.run_cli(
        support.make_request(
            "get_status",
            {
                "workspace_path": str(managed_workspace.workspace_path),
                "intake_public_id": "d4-managed-status",
            },
        )
    )
    assert status.exit_code == bridge_errors.EXIT_OK, status.response
    assert status.response["result"]["identity_kind"] == "intake"
    assert status.response["result"]["intake_public_id"] == "d4-managed-status"


def test_every_other_bridge_command_is_refused_before_managed_effects(
    managed_workspace: ManagedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = sorted(
        envelope.ALLOWED_COMMANDS - {envelope.COMMAND_HEALTH, envelope.COMMAND_GET_STATUS}
    )
    before = _snapshot_tree(managed_workspace.profile_base)
    database_open_attempts: list[str] = []
    network_attempts: list[str] = []

    def refuse_database(*_args: object, **_kwargs: object) -> None:
        database_open_attempts.append("database")
        raise AssertionError("fixed-profile command reached a database open")

    def refuse_network(*_args: object, **_kwargs: object) -> None:
        network_attempts.append("network")
        raise AssertionError("fixed-profile command reached a network call")

    with monkeypatch.context() as guarded:
        guarded.setattr(workspace_access, "workspace_database_session", refuse_database)
        guarded.setattr(workspace_access, "open_workspace_database", refuse_database)
        guarded.setattr(socket.socket, "connect", refuse_network)
        guarded.setattr(socket, "create_connection", refuse_network)
        for index, command in enumerate(unsupported):
            arguments = {"workspace_path": str(managed_workspace.workspace_path)}
            request = support.make_request(
                command,
                arguments,
                **(
                    {"idempotency_key": f"managed-refusal-{index}"}
                    if command in envelope.MUTATING_COMMANDS
                    else {}
                ),
            )
            outcome = support.run_cli(request)
            assert outcome.exit_code == bridge_errors.EXIT_AUTHORITY_REFUSED, (
                command,
                outcome.response,
            )
            assert outcome.response["error"]["code"] == bridge_errors.STAGING_REFUSED, command

    assert database_open_attempts == []
    assert network_attempts == []
    assert _snapshot_tree(managed_workspace.profile_base) == before

    ordinary_root = tmp_path / "ordinary"
    ordinary_root.mkdir()
    ordinary = support.create_bridge_workspace(ordinary_root, name="ordinary")
    update = support.telegram_text_update("ordinary staging remains available", message_id=102)
    ordinary_capture = support.run_cli(
        support.make_request(
            "capture",
            support.authenticated_text_capture_arguments(ordinary, update),
            idempotency_key=support.canonical_capture_key(message_id=102),
        )
    )
    assert ordinary_capture.exit_code == bridge_errors.EXIT_OK, ordinary_capture.response


def test_unregistered_copied_pending_symlinked_and_mismatched_profiles_fail_closed(
    managed_workspace: ManagedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "unregistered").mkdir()
    _support_root, unregistered_base, blank_value = _blank_profile(
        tmp_path / "unregistered", monkeypatch, profile_id="bridge-unregistered"
    )
    cast(ProfilePaths, blank_value).close()
    for name in ("attachments", "runtime", "evidence"):
        (unregistered_base / "workspace" / name).mkdir(mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(unregistered_base / "runtime"))
    _assert_managed_refusal(_health(unregistered_base / "workspace"))

    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(managed_workspace.profile_base / "runtime"))
    copied_base = managed_workspace.profile_base.parent / "bridge-copied"
    shutil.copytree(managed_workspace.profile_base, copied_base)
    _assert_managed_refusal(_health(copied_base / "workspace"))

    pending = managed_workspace.profile_base / ".managed-staging.v1.pending"
    pending.write_text("synthetic pending enrollment\n", encoding="utf-8")
    pending.chmod(0o600)
    try:
        _assert_managed_refusal(_health(managed_workspace.workspace_path))
    finally:
        pending.unlink()

    symlink = tmp_path / "workspace-alias"
    symlink.symlink_to(managed_workspace.workspace_path, target_is_directory=True)
    symlinked = _health(symlink)
    assert symlinked.exit_code == bridge_errors.EXIT_VALIDATION_REFUSED
    assert symlinked.response["error"]["code"] == bridge_errors.WORKSPACE_REFUSED

    other_runtime = tmp_path / "other-runtime"
    other_runtime.mkdir(mode=0o700)
    (other_runtime / "database").mkdir(mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(other_runtime))
    _assert_managed_refusal(_health(managed_workspace.workspace_path))


def test_delivery_receipt_cli_uses_the_managed_workspace_session(
    managed_workspace: ManagedWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticated_receipt = object()
    authentication_calls: list[dict[str, object]] = []
    session_calls: list[tuple[Path, str]] = []
    observed_receipts: list[object] = []
    real_session = workspace_access.workspace_database_session

    def authenticate_receipt(**fields: object) -> object:
        authentication_calls.append(fields)
        return authenticated_receipt

    @contextmanager
    def observe_session(workspace_path: Path, *, operation_id: str) -> Iterator[sqlite3.Connection]:
        session_calls.append((workspace_path, operation_id))
        with real_session(workspace_path, operation_id=operation_id) as conn:
            yield conn

    def observe_delivery(conn: sqlite3.Connection, *, receipt: object) -> str:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1
        observed_receipts.append(receipt)
        return "delivery_observation_synthetic"

    monkeypatch.setattr(delivery_receipt_cli, "authenticate_delivery_receipt", authenticate_receipt)
    monkeypatch.setattr(
        delivery_receipt_cli.posting_authority,
        "record_posting_review_delivery",
        observe_delivery,
    )
    monkeypatch.setattr(workspace_access, "workspace_database_session", observe_session)

    payload: dict[str, object] = {
        "workspace_path": str(managed_workspace.workspace_path),
        "attempt_nonce": "d2nonce_" + "1" * 32,
        "capability": "telegram.finance-delivery-material-v1",
        "delivery_material_version": "finance_d2_delivery_material_v1",
        "delivery_material_sha256": "a" * 64,
        "provider_message_id": "synthetic-message-1",
        "receipt_token_sha256": "b" * 64,
        "channel": "telegram",
        "account_id": "synthetic-account",
        "conversation_id": "synthetic-conversation",
        "session_key": "synthetic-session",
        "source_identity_sha256": "c" * 64,
        "receipt_proof_version": PROOF_VERSION,
        "receipt_proof_sha256": "d" * 64,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = delivery_receipt_cli.execute_stream(
        io.BytesIO(json.dumps(payload).encode("utf-8")), stdout, stderr
    )

    assert exit_code == bridge_errors.EXIT_OK, (
        stderr.getvalue(),
        authentication_calls,
        session_calls,
        observed_receipts,
    )
    assert json.loads(stdout.getvalue()) == {
        "observation_public_id": "delivery_observation_synthetic",
        "status": "ok",
    }
    assert len(authentication_calls) == 1
    assert observed_receipts == [authenticated_receipt]
    assert len(session_calls) == 1
    session_path, operation_id = session_calls[0]
    assert session_path == managed_workspace.workspace_path
    assert operation_id.startswith("delivery-receipt:")
    assert len(operation_id.removeprefix("delivery-receipt:")) == 32


def test_managed_workspace_session_holds_gate_until_connection_close(
    managed_workspace: ManagedWorkspace,
) -> None:
    witness = workspace_access.managed_profile_for_workspace(managed_workspace.workspace_path)
    assert witness is not None
    connection: sqlite3.Connection | None = None
    probe = """
import sys
from finance_core.profile_gate import ProfileGateBusy, exclusive_cut
from finance_core.profile_paths import validate_registered_staging_profile
support, profile_id = sys.argv[1:3]
profile = validate_registered_staging_profile(support, profile_id)
try:
    try:
        with exclusive_cut(profile, timeout_seconds=0):
            raise SystemExit(2)
    except ProfileGateBusy:
        raise SystemExit(0)
    raise SystemExit(3)
finally:
    profile.close()
"""
    support_root = managed_workspace.profile_base.parent.parent.parent
    try:
        with workspace_access.workspace_database_session(
            managed_workspace.workspace_path, operation_id="test-session-lifetime"
        ) as active_connection:
            connection = active_connection
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    probe,
                    str(support_root),
                    managed_workspace.profile_base.name,
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert result.returncode == 0, result.stderr or result.stdout

        assert connection is not None
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with exclusive_cut(witness, timeout_seconds=0):
            pass
    finally:
        witness.close()
