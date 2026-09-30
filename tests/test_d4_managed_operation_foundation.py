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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator, cast

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.intake.capture_jobs import ensure_capture_job
from finance_core.intake.interaction_routes import freeze_interaction_route
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.telegram_text_adapter import validate_telegram_text_update
from finance_core.managed_staging_profile import bootstrap_registered_staging
from finance_core.openclaw_staging_bridge import (
    delivery_receipt_cli,
    envelope,
    human_actions,
    workspace_access,
)
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge.delivery_receipt_proof import PROOF_VERSION
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import ManagedStagingProfile, ProfilePaths
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    record_telegram_source_context,
)
from tests.test_managed_staging_profile import _blank_profile


@dataclass(frozen=True)
class ManagedWorkspace:
    profile_base: Path
    workspace_path: Path
    witness: ManagedStagingProfile


_MANAGED_READ_COMMANDS = frozenset(
    {
        envelope.COMMAND_GET_INTERACTION_ROUTE,
        envelope.COMMAND_LIST_CAPTURE_RECOVERY_CANDIDATES,
        envelope.COMMAND_GET_CAPTURE_JOB_FOR_MESSAGE,
        envelope.COMMAND_GET_GUIDED_EDIT_SESSION,
        envelope.COMMAND_GET_HUMAN_DRAFT_CARD,
    }
)

_S1C_A_COMMANDS = frozenset(
    {
        envelope.COMMAND_CAPTURE_INTERACTION,
        envelope.COMMAND_GET_REVIEW,
        envelope.COMMAND_CONFIRM,
        envelope.COMMAND_EDIT,
        envelope.COMMAND_REJECT,
        envelope.COMMAND_ISSUE_HUMAN_ACTIONS,
        envelope.COMMAND_REDEEM_HUMAN_ACTION,
        envelope.COMMAND_APPLY_GUIDED_EDIT_UPDATE,
        envelope.COMMAND_COMPLETE_GUIDED_EDIT,
        envelope.COMMAND_APPLY_HUMAN_DRAFT_CARD,
        envelope.COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY,
        envelope.COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
        envelope.COMMAND_REISSUE_HUMAN_DRAFT_CARD,
    }
)

_S1C_B_COMMANDS = frozenset(
    {
        envelope.COMMAND_PREPARE_POSTING_REVIEW,
        envelope.COMMAND_ISSUE_POSTING_REVIEW_ACTIONS,
        envelope.COMMAND_CONFIRM_AND_POST,
        envelope.COMMAND_RESUME_POSTING,
        envelope.COMMAND_FINALIZE,
        envelope.COMMAND_PREPARE_RECEIPT_COMPLETION,
        envelope.COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW,
        envelope.COMMAND_AUTHORIZE_FINALIZATION,
        envelope.COMMAND_APPLY_FACT_SET,
    }
)

_MANAGED_DATABASE_COMMANDS = frozenset(
    {
        envelope.COMMAND_HEALTH,
        envelope.COMMAND_GET_STATUS,
        *_MANAGED_READ_COMMANDS,
        *_S1C_A_COMMANDS,
        *_S1C_B_COMMANDS,
    }
)


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


def _read_arguments(command: str, workspace: Path) -> dict[str, object]:
    arguments: dict[str, object] = {
        "workspace_path": str(workspace),
        "operator_actor_id": "111",
        "telegram_account_id": "finance-account",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "binding-1",
    }
    if command in {
        envelope.COMMAND_GET_INTERACTION_ROUTE,
        envelope.COMMAND_GET_CAPTURE_JOB_FOR_MESSAGE,
    }:
        arguments["telegram_message_id"] = 177
    elif command == envelope.COMMAND_LIST_CAPTURE_RECOVERY_CANDIDATES:
        arguments["limit"] = 5
    elif command == envelope.COMMAND_GET_HUMAN_DRAFT_CARD:
        arguments["operation_public_id"] = "synthetic-missing-operation"
    return arguments


@pytest.mark.parametrize("command", sorted(_MANAGED_READ_COMMANDS))
def test_managed_read_commands_match_ordinary_missing_state_and_close_session(
    managed_workspace: ManagedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    ordinary_root = tmp_path / "ordinary-read"
    ordinary_root.mkdir()
    ordinary = support.create_bridge_workspace(ordinary_root, name="ordinary-read")
    ordinary_outcome = support.run_cli(
        support.make_request(command, _read_arguments(command, ordinary.workspace_path))
    )

    connections: list[sqlite3.Connection] = []
    operations: list[str] = []
    real_session = workspace_access.workspace_database_session

    class ObservedSession:
        def __init__(
            self,
            workspace: Path,
            *,
            operation_id: str,
            deadline: workspace_access.SessionDeadline | None = None,
        ) -> None:
            operations.append(operation_id)
            self.inner = real_session(workspace, operation_id=operation_id, deadline=deadline)
            self.conn: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.conn = self.inner.__enter__()
            connections.append(self.conn)
            return self.conn

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            assert self.conn is not None
            assert self.conn.total_changes == 0, "read handler changed business rows"
            self.inner.__exit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(workspace_access, "workspace_database_session", ObservedSession)
    managed_outcome = support.run_cli(
        support.make_request(command, _read_arguments(command, managed_workspace.workspace_path))
    )

    assert managed_outcome.exit_code == ordinary_outcome.exit_code, (
        command,
        managed_outcome.response,
        ordinary_outcome.response,
    )
    if managed_outcome.exit_code == bridge_errors.EXIT_OK:
        assert managed_outcome.response["result"] == ordinary_outcome.response["result"]
    else:
        assert (
            managed_outcome.response["error"]["code"] == ordinary_outcome.response["error"]["code"]
        )
    assert len(operations) == len(connections) == 1
    assert operations[0].startswith("bridge:")
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


def test_managed_capture_reads_preserve_authenticated_source_isolation(
    managed_workspace: ManagedWorkspace,
) -> None:
    workspace = managed_workspace.workspace_path
    update = support.telegram_text_update("synthetic lunch 12.50", message_id=177)
    validated = validate_telegram_text_update(update)
    source_context = TelegramSourceContext(
        authenticated_actor_id="111",
        account_id="finance-account",
        conversation_id="111",
        binding_id="binding-1",
        message_id="177",
    )
    with workspace_access.workspace_database_session(
        workspace, operation_id="test-seed-authenticated-capture"
    ) as conn:
        conn.execute("BEGIN IMMEDIATE")
        intake = create_raw_intake_record(
            conn,
            validated.text,
            source_channel="telegram",
            source_metadata=validated.source_metadata,
        )
        record_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=source_context,
            captured_at=str(intake["received_at"]),
        )
        job = ensure_capture_job(conn, intake_id=int(intake["id"]), capture_kind="text")
        freeze_interaction_route(
            conn,
            job_public_id=str(job["public_id"]),
            text=validated.text,
            context=human_actions.HumanActionContext(
                actor_id="111",
                account_id="finance-account",
                conversation_id="111",
                binding_id="binding-1",
            ),
            message_id=177,
        )
        conn.commit()

    for binding, visible in (("binding-1", True), ("other-binding", False)):
        context = {
            "workspace_path": str(workspace),
            "operator_actor_id": "111",
            "telegram_account_id": "finance-account",
            "telegram_conversation_id": "111",
            "conversation_binding_id": binding,
        }
        route = support.run_cli(
            support.make_request("get_interaction_route", {**context, "telegram_message_id": 177})
        )
        assert route.exit_code == bridge_errors.EXIT_OK, route.response
        assert route.response["result"]["found"] is visible
        if visible:
            assert route.response["result"]["interaction_route"]["route_kind"] == "initial_intake"

        discovered = support.run_cli(
            support.make_request("list_capture_recovery_candidates", {**context, "limit": 5})
        )
        assert discovered.exit_code == bridge_errors.EXIT_OK, discovered.response
        candidates = discovered.response["result"]["candidates"]
        assert [item["job_public_id"] for item in candidates] == (
            [job["public_id"]] if visible else []
        )

        located = support.run_cli(
            support.make_request(
                "get_capture_job_for_message", {**context, "telegram_message_id": 177}
            )
        )
        assert located.exit_code == bridge_errors.EXIT_OK, located.response
        candidate = located.response["result"]["candidate"]
        assert (candidate is not None) is visible
        if visible:
            assert candidate["job_public_id"] == job["public_id"]


def test_every_other_bridge_command_is_refused_before_managed_effects(
    managed_workspace: ManagedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = sorted(
        envelope.ALLOWED_COMMANDS
        - _MANAGED_DATABASE_COMMANDS
        - {envelope.COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2}
    )
    assert len(_MANAGED_DATABASE_COMMANDS) == 29
    assert len(unsupported) == 13
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


@pytest.mark.parametrize("command", ["health", "get_interaction_route"])
def test_managed_read_obeys_bridge_deadline_while_child_holds_exclusive_gate(
    managed_workspace: ManagedWorkspace, command: str
) -> None:
    ready_path = managed_workspace.profile_base.parent / "exclusive-gate-ready"
    probe = """
import sys
import time
from pathlib import Path
from finance_core.profile_gate import exclusive_cut
from finance_core.profile_paths import validate_registered_staging_profile
support, profile_id, ready_path = sys.argv[1:4]
profile = validate_registered_staging_profile(support, profile_id)
try:
    with exclusive_cut(profile, timeout_seconds=0):
        Path(ready_path).write_text("locked\\n", encoding="utf-8")
        time.sleep(5)
finally:
    profile.close()
"""
    support_root = managed_workspace.profile_base.parent.parent.parent
    locker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            probe,
            str(support_root),
            managed_workspace.profile_base.name,
            str(ready_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < ready_deadline:
            if locker.poll() is not None:
                stdout, stderr = locker.communicate()
                pytest.fail(f"exclusive gate child exited early: {stderr or stdout}")
            time.sleep(0.01)
        assert ready_path.exists(), "child did not acquire the exclusive profile gate"

        started = time.monotonic()
        outcome = support.run_cli(
            support.make_request(
                command,
                (
                    {"workspace_path": str(managed_workspace.workspace_path)}
                    if command == "health"
                    else _read_arguments(command, managed_workspace.workspace_path)
                ),
            ),
            deadline_seconds=0.2,
        )
        elapsed = time.monotonic() - started

        assert outcome.exit_code == bridge_errors.EXIT_DEADLINE_EXCEEDED, outcome.response
        assert outcome.response["error"]["code"] == bridge_errors.DEADLINE_EXCEEDED
        assert elapsed < 1.0, f"Bridge exceeded its short deadline: {elapsed:.3f}s"
    finally:
        if locker.poll() is None:
            locker.terminate()
        stdout, stderr = locker.communicate(timeout=5)
        assert locker.returncode == 0 or locker.returncode == -15, stderr or stdout
