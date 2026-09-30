"""Managed synthetic correction and authenticated delivery CLI proofs."""

from __future__ import annotations

import hmac
import io
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from s1d1_managed_test_support import (
    acquire_exclusive_profile_gate,
    assert_correction_ledger_empty,
    caller_connection_must_not_be_registered,
    correction_ledger_counts,
    create_posted_managed_transaction,
    execute_delivery_receipt,
    prepare_managed_delivery,
    resign_delivery_receipt,
    table_counts,
)

from finance_core.application import corrections as correction_application
from finance_core.correction_adapters import cli as correction_cli
from finance_core.correction_adapters import local_authority
from finance_core.correction_adapters import policy as correction_policy
from finance_core.correction_adapters.local_authority import LocalApprovalAuthority
from finance_core.managed_staging_profile import bootstrap_registered_staging
from finance_core.openclaw_staging_bridge import errors, workspace_access
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests.test_managed_staging_profile import _blank_profile

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _invoke_correction_cli(
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, str, str]:
    exit_code = correction_cli.main(list(arguments))
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _assert_connection_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def _assert_correction_rows(
    workspace: s1ca.ManagedBridgeWorkspace,
    transaction_id: str,
) -> None:
    assert correction_ledger_counts(workspace)["correction_versions"] == 1
    assert correction_ledger_counts(workspace)["correction_authorities"] == 1
    assert table_counts(workspace, ("transactions",))["transactions"] == 1
    audit = s1ca._read_one(
        workspace,
        """
        SELECT COUNT(*) FROM financial_audit_events
        WHERE aggregate_type = 'transaction'
          AND aggregate_public_id = ?
          AND event_type = 'transaction_correction_applied'
        """,
        (transaction_id,),
    )
    assert audit is not None and int(audit[0]) == 1


def _assert_no_correction_commit(
    workspace: s1ca.ManagedBridgeWorkspace,
    transaction_id: str,
) -> None:
    counts = correction_ledger_counts(workspace)
    assert counts["correction_versions"] == 0
    assert counts["correction_authorities"] == 0
    assert counts["correction_receipt_facts"] == 0
    audit = s1ca._read_one(
        workspace,
        """
        SELECT COUNT(*) FROM financial_audit_events
        WHERE aggregate_type = 'transaction'
          AND aggregate_public_id = ?
          AND event_type = 'transaction_correction_applied'
        """,
        (transaction_id,),
    )
    assert audit is not None and int(audit[0]) == 0


def _preview_managed_correction(
    capsys: pytest.CaptureFixture[str],
    transaction_id: str,
    amount: str,
) -> dict[str, Any]:
    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "preview",
        transaction_id,
        "--reason",
        "synthetic S1D-1 correction",
        "--amount",
        amount,
    )
    assert exit_code == 0, (stdout, stderr)
    return json.loads(stdout)


def _assert_provisioned_over_nonempty_finance_ledger(
    workspace: s1ca.ManagedBridgeWorkspace,
    transaction_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = workspace_access.database_path_for(workspace.workspace_path)
    assert database.name == "staging.sqlite"
    assert database.is_absolute()
    assert table_counts(workspace, ("transactions",))["transactions"] == 1
    transaction_row = s1ca._read_one(
        workspace,
        "SELECT COUNT(*) FROM transactions WHERE public_id = ?",
        (transaction_id,),
    )
    assert transaction_row is not None and int(transaction_row[0]) == 1
    assert_correction_ledger_empty(workspace)

    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "provision",
        "--database",
        str(database),
        "--actor",
        s1ca.ACTOR,
    )
    assert exit_code == 0, (stdout, stderr)
    assert json.loads(stdout)["status"] == "provisioned"
    caller_connection_must_not_be_registered(workspace)


def _connection_observer(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    connections: list[sqlite3.Connection] = []
    real_factory = correction_cli.open_local_authority_connection

    def observed_factory() -> Any:
        from contextlib import contextmanager

        @contextmanager
        def observe():
            with real_factory() as connection:
                connections.append(connection)
                yield connection

        return observe()

    monkeypatch.setattr(correction_cli, "open_local_authority_connection", observed_factory)
    return connections


def _prepare_real_terminal_signer(
    workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    connections: list[sqlite3.Connection],
    *,
    before_sign: Any | None = None,
    after_sign: Any | None = None,
) -> list[str]:
    sign_calls: list[str] = []
    real_sign = LocalApprovalAuthority.sign_with_terminal
    next_token = iter(f"{value:064x}" for value in range(1, 65))

    def sign_after_phase_a_closed(
        authority: LocalApprovalAuthority,
        plan: Any,
    ) -> Any:
        assert connections, "confirm must read its persisted plan before terminal review"
        for connection in connections:
            _assert_connection_closed(connection)
        acquire_exclusive_profile_gate(workspace)
        sign_calls.append(str(plan.plan_id))
        if before_sign is not None:
            before_sign(authority, plan)
        challenge = next(next_token)
        tokens = iter((challenge, next(next_token)))
        with monkeypatch.context() as scoped:
            scoped.setattr(local_authority.secrets, "token_hex", lambda _count: next(tokens))
            signed = real_sign(
                authority,
                plan,
                input_stream=_Terminal(f"CONFIRM {plan.plan_id} {challenge}\n"),
                output_stream=_Terminal(),
            )
        if after_sign is not None:
            after_sign(authority, plan, signed)
        return signed

    monkeypatch.setattr(LocalApprovalAuthority, "sign_with_terminal", sign_after_phase_a_closed)
    return sign_calls


def _policy_file(runtime_root: Any) -> Any:
    return runtime_root / "correction_authority" / "policy.json"


def _assert_secret_file_bytes_unchanged(actual: bytes, expected: bytes) -> None:
    assert len(actual) == len(expected), "retained policy file length changed"
    assert hmac.compare_digest(actual, expected), "retained policy file bytes changed"


def test_managed_provision_refuses_runtime_mismatch_and_unregistered_fixed_profile(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = workspace_access.database_path_for(managed_workspace.workspace_path)
    registered_runtime = managed_workspace.profile_base / "runtime"
    wrong_runtime = tmp_path / "wrong-runtime"
    wrong_runtime.mkdir(mode=0o700)
    (wrong_runtime / "database").mkdir(mode=0o700)

    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(wrong_runtime))
    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "provision",
        "--database",
        str(database),
        "--actor",
        s1ca.ACTOR,
    )
    assert exit_code == 2
    assert not stdout
    assert "correction refused:" in stderr
    assert not _policy_file(wrong_runtime).exists()
    assert not _policy_file(registered_runtime).exists()

    unregistered_root = tmp_path / "unregistered"
    unregistered_root.mkdir(mode=0o700)
    _support_root, copied_profile_base, blank_profile = _blank_profile(
        unregistered_root,
        monkeypatch,
        profile_id="s1d1-unregistered-copy",
    )
    del _support_root
    try:
        copied_workspace = copied_profile_base / "workspace"
        copied_database = workspace_access.database_path_for(copied_workspace)
        shutil.copy2(database, copied_database)
        assert copied_database.name == "staging.sqlite"
        assert copied_database.is_file()

        copied_runtime = copied_profile_base / "runtime"
        exit_code, stdout, stderr = _invoke_correction_cli(
            capsys,
            "provision",
            "--database",
            str(copied_database),
            "--actor",
            s1ca.ACTOR,
        )
        assert exit_code == 2
        assert not stdout
        assert "correction refused:" in stderr
        assert not _policy_file(copied_runtime).exists()
    finally:
        blank_profile.close()


def test_managed_reprovision_refuses_nonempty_correction_ledger_after_policy_retained(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_posted_managed_transaction(managed_workspace, "text")
    transaction_id = source.transaction_public_id
    _assert_provisioned_over_nonempty_finance_ledger(managed_workspace, transaction_id, capsys)

    connections = _connection_observer(monkeypatch)
    plan = _preview_managed_correction(capsys, transaction_id, "13.50")
    sign_calls = _prepare_real_terminal_signer(managed_workspace, monkeypatch, connections)
    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "confirm",
        str(plan["plan_id"]),
    )
    assert exit_code == 0, (stdout, stderr)
    assert sign_calls == [str(plan["plan_id"])]
    assert correction_ledger_counts(managed_workspace)["correction_versions"] == 1
    assert correction_ledger_counts(managed_workspace)["correction_authorities"] == 1

    runtime_root = managed_workspace.profile_base / "runtime"
    database = workspace_access.database_path_for(managed_workspace.workspace_path)
    policy_path = _policy_file(runtime_root)
    retained_policy_path = policy_path.with_name("policy.json.retained-ledger-evidence")
    original_policy_bytes = policy_path.read_bytes()
    policy_path.rename(retained_policy_path)
    assert not policy_path.exists()
    _assert_secret_file_bytes_unchanged(retained_policy_path.read_bytes(), original_policy_bytes)

    before_domain_counts = table_counts(managed_workspace)
    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "provision",
        "--database",
        str(database),
        "--actor",
        s1ca.ACTOR,
    )
    assert exit_code == 2
    assert not stdout
    assert "correction ledger already contains authority" in stderr.lower()
    assert not policy_path.exists()
    assert table_counts(managed_workspace) == before_domain_counts
    _assert_secret_file_bytes_unchanged(retained_policy_path.read_bytes(), original_policy_bytes)
    acquire_exclusive_profile_gate(managed_workspace)


def test_managed_first_policy_partial_write_is_retained_and_fails_closed(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_posted_managed_transaction(managed_workspace, "text")
    database = workspace_access.database_path_for(managed_workspace.workspace_path)
    runtime_root = managed_workspace.profile_base / "runtime"
    policy_path = _policy_file(runtime_root)
    before_domain_counts = table_counts(managed_workspace)
    assert_correction_ledger_empty(managed_workspace)

    real_os_open = correction_policy.os.open
    real_os_write = correction_policy.os.write
    policy_create_flags: list[int] = []
    policy_fd_identities: list[tuple[int, int]] = []
    policy_write_attempt_lengths: list[int] = []
    partial_policy_bytes = bytearray()

    def observe_open(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_os_open(file, flags, *args, **kwargs)
        try:
            requested_path = Path(os.fsdecode(os.fspath(file)))
        except (TypeError, ValueError):
            return descriptor
        if requested_path == policy_path:
            policy_create_flags.append(flags)
            opened = os.fstat(descriptor)
            current_path = policy_path.lstat()
            policy_fd_identities.append((opened.st_dev, opened.st_ino))
            assert (opened.st_dev, opened.st_ino) == (current_path.st_dev, current_path.st_ino)
        return descriptor

    def fail_after_real_policy_prefix(descriptor: int, data: bytes) -> int:
        opened = os.fstat(descriptor)
        try:
            current_path = policy_path.lstat()
        except FileNotFoundError:
            return real_os_write(descriptor, data)
        if (opened.st_dev, opened.st_ino) != (current_path.st_dev, current_path.st_ino):
            return real_os_write(descriptor, data)

        policy_write_attempt_lengths.append(len(data))
        if len(policy_write_attempt_lengths) == 1:
            prefix_length = max(1, len(data) // 3)
            prefix = bytes(data[:prefix_length])
            actual_count = real_os_write(descriptor, prefix)
            partial_policy_bytes.extend(prefix[:actual_count])
            return actual_count
        raise OSError("synthetic policy partial-write interruption")

    with monkeypatch.context() as scoped:
        scoped.setattr(correction_policy.os, "open", observe_open)
        scoped.setattr(correction_policy.os, "write", fail_after_real_policy_prefix)
        exit_code, stdout, stderr = _invoke_correction_cli(
            capsys,
            "provision",
            "--database",
            str(database),
            "--actor",
            s1ca.ACTOR,
        )

    assert exit_code == 2
    assert not stdout
    assert "synthetic policy partial-write interruption" in stderr
    assert len(policy_create_flags) == 1
    assert policy_create_flags[0] & os.O_CREAT
    assert policy_create_flags[0] & os.O_EXCL
    assert len(policy_fd_identities) == 1
    assert len(policy_write_attempt_lengths) == 2
    assert partial_policy_bytes
    assert policy_path.exists()
    retained_partial_bytes = policy_path.read_bytes()
    _assert_secret_file_bytes_unchanged(retained_partial_bytes, bytes(partial_policy_bytes))
    assert table_counts(managed_workspace) == before_domain_counts
    acquire_exclusive_profile_gate(managed_workspace)

    with pytest.raises(correction_policy.LocalPolicyError, match="policy"):
        with correction_policy.open_local_authority_connection():
            pytest.fail("a partial owner policy must not yield an authority connection")
    assert table_counts(managed_workspace) == before_domain_counts
    _assert_secret_file_bytes_unchanged(policy_path.read_bytes(), retained_partial_bytes)

    retry_exit, retry_stdout, retry_stderr = _invoke_correction_cli(
        capsys,
        "provision",
        "--database",
        str(database),
        "--actor",
        s1ca.ACTOR,
    )
    assert retry_exit == 2
    assert not retry_stdout
    assert "policy already exists" in retry_stderr.lower()
    assert table_counts(managed_workspace) == before_domain_counts
    _assert_secret_file_bytes_unchanged(policy_path.read_bytes(), retained_partial_bytes)

    with pytest.raises(
        correction_policy.LocalPolicyError,
        match="managed correction policy quarantine is unavailable",
    ):
        correction_policy.quarantine_incomplete_policy(database)
    assert table_counts(managed_workspace) == before_domain_counts
    _assert_secret_file_bytes_unchanged(policy_path.read_bytes(), retained_partial_bytes)
    acquire_exclusive_profile_gate(managed_workspace)


def test_managed_current_binding_does_not_reopen_main_database(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_posted_managed_transaction(managed_workspace, "text")
    _assert_provisioned_over_nonempty_finance_ledger(
        managed_workspace,
        source.transaction_public_id,
        capsys,
    )

    witness_calls: list[Any] = []
    real_database_witness = correction_policy._database_witness

    def observe_database_witness(path: Any) -> tuple[int, int, int, int]:
        witness_calls.append(path)
        return real_database_witness(path)

    with correction_policy.open_local_authority_connection() as connection:
        monkeypatch.setattr(correction_policy, "_database_witness", observe_database_witness)
        binding = LocalApprovalAuthority().current_binding(connection, s1ca.ACTOR)
        assert binding.actor == s1ca.ACTOR
        assert binding.key_id
        assert binding.realm
        assert binding.instance_id
        assert witness_calls == []

    assert witness_calls == []


def test_managed_confirm_rechecks_stale_predecessor_after_terminal_wait(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_posted_managed_transaction(managed_workspace, "text")
    transaction_id = source.transaction_public_id
    _assert_provisioned_over_nonempty_finance_ledger(managed_workspace, transaction_id, capsys)
    connections = _connection_observer(monkeypatch)
    stale_plan = _preview_managed_correction(capsys, transaction_id, "13.50")
    winner_plan = _preview_managed_correction(capsys, transaction_id, "14.50")

    nested_confirm: list[tuple[int, str, str]] = []

    def apply_newer_plan_while_old_decision_waits(
        _authority: LocalApprovalAuthority,
        plan: Any,
    ) -> None:
        if plan.plan_id != stale_plan["plan_id"]:
            return
        nested_confirm.append(
            _invoke_correction_cli(capsys, "confirm", str(winner_plan["plan_id"]))
        )

    sign_calls = _prepare_real_terminal_signer(
        managed_workspace,
        monkeypatch,
        connections,
        before_sign=apply_newer_plan_while_old_decision_waits,
    )
    stale_exit, stale_stdout, stale_stderr = _invoke_correction_cli(
        capsys,
        "confirm",
        str(stale_plan["plan_id"]),
    )

    assert len(nested_confirm) == 1
    winner_exit, winner_stdout, winner_stderr = nested_confirm[0]
    assert winner_exit == 0, (winner_stdout, winner_stderr)
    assert stale_exit == 2
    assert not stale_stdout
    assert "stale predecessor" in stale_stderr.lower()
    assert set(sign_calls) == {str(stale_plan["plan_id"]), str(winner_plan["plan_id"])}
    assert correction_ledger_counts(managed_workspace)["correction_versions"] == 1
    assert correction_ledger_counts(managed_workspace)["correction_authorities"] == 1
    assert (
        s1ca._read_one(
            managed_workspace,
            "SELECT COUNT(*) FROM correction_authorities WHERE plan_id = ?",
            (str(stale_plan["plan_id"]),),
        )[0]
        == 0
    )
    _assert_correction_rows(managed_workspace, transaction_id)
    for connection in connections:
        _assert_connection_closed(connection)

    exit_code, stdout, stderr = _invoke_correction_cli(capsys, "show", transaction_id)
    assert exit_code == 0, (stdout, stderr)
    assert json.loads(stdout)["fields"]["amount"] == "14.50"


def test_managed_confirm_rejects_policy_instance_switch_during_terminal_wait(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_posted_managed_transaction(managed_workspace, "text")
    transaction_id = source.transaction_public_id
    _assert_provisioned_over_nonempty_finance_ledger(managed_workspace, transaction_id, capsys)
    original_runtime = managed_workspace.profile_base / "runtime"
    plan = _preview_managed_correction(capsys, transaction_id, "13.50")

    other_profile_root = tmp_path / "other-profile"
    other_profile_root.mkdir(mode=0o700)
    support_root, other_profile_base, other_blank = _blank_profile(
        other_profile_root,
        monkeypatch,
        profile_id="s1d1-other-managed",
    )
    del support_root
    other_workspace_path = other_profile_base / "workspace"
    for directory in ("attachments", "runtime", "evidence"):
        (other_workspace_path / directory).mkdir(mode=0o700)
    other_witness = bootstrap_registered_staging(other_blank)
    other_workspace = s1ca.ManagedBridgeWorkspace(
        other_profile_base,
        other_workspace_path,
        other_witness,
    )
    try:
        other_database = workspace_access.database_path_for(other_workspace_path)
        other_runtime = other_profile_base / "runtime"
        provision_exit, provision_stdout, provision_stderr = _invoke_correction_cli(
            capsys,
            "provision",
            "--database",
            str(other_database),
            "--actor",
            s1ca.ACTOR,
        )
        assert provision_exit == 0, (provision_stdout, provision_stderr)
        assert json.loads(provision_stdout)["status"] == "provisioned"

        monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(original_runtime))
        connections = _connection_observer(monkeypatch)
        switched: list[bool] = []

        def switch_policy_after_real_signature(
            _authority: LocalApprovalAuthority,
            signed_plan: Any,
            _signed_decision: Any,
        ) -> None:
            if signed_plan.plan_id == plan["plan_id"] and not switched:
                monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(other_runtime))
                switched.append(True)

        sign_calls = _prepare_real_terminal_signer(
            managed_workspace,
            monkeypatch,
            connections,
            after_sign=switch_policy_after_real_signature,
        )
        first_exit, first_stdout, first_stderr = _invoke_correction_cli(
            capsys,
            "confirm",
            str(plan["plan_id"]),
        )
        assert first_exit == 2
        assert not first_stdout
        assert "correction refused:" in first_stderr
        assert switched == [True]
        assert table_counts(other_workspace, ("correction_versions", "correction_authorities")) == {
            "correction_versions": 0,
            "correction_authorities": 0,
        }

        monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(original_runtime))
        _assert_no_correction_commit(managed_workspace, transaction_id)
        second_exit, second_stdout, second_stderr = _invoke_correction_cli(
            capsys,
            "confirm",
            str(plan["plan_id"]),
        )
        assert second_exit == 0, (second_stdout, second_stderr)
        assert len(sign_calls) == 2
        assert sign_calls == [str(plan["plan_id"]), str(plan["plan_id"])]
        assert correction_ledger_counts(managed_workspace)["correction_versions"] == 1
        with monkeypatch.context() as scoped:
            scoped.setenv("FINANCE_RUNTIME_ROOT", str(other_runtime))
            assert table_counts(
                other_workspace,
                ("correction_versions", "correction_authorities"),
            ) == {
                "correction_versions": 0,
                "correction_authorities": 0,
            }
        for connection in connections:
            _assert_connection_closed(connection)
    finally:
        other_witness.close()
        other_blank.close()


def test_managed_confirm_rechecks_expiry_after_real_terminal_signature(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_posted_managed_transaction(managed_workspace, "text")
    transaction_id = source.transaction_public_id
    _assert_provisioned_over_nonempty_finance_ledger(managed_workspace, transaction_id, capsys)

    class ControlledClock:
        now = 1_800_000_000

    clock = ControlledClock()
    monkeypatch.setattr(
        correction_application,
        "time",
        SimpleNamespace(time=lambda: clock.now),
    )
    monkeypatch.setattr(
        local_authority,
        "time",
        SimpleNamespace(time=lambda: clock.now),
    )
    connections = _connection_observer(monkeypatch)
    plan = _preview_managed_correction(capsys, transaction_id, "13.50")

    def expire_after_real_signature(
        _authority: LocalApprovalAuthority,
        signed_plan: Any,
        _signed_decision: Any,
    ) -> None:
        clock.now = int(signed_plan.expires_at_epoch) + 1

    _prepare_real_terminal_signer(
        managed_workspace,
        monkeypatch,
        connections,
        after_sign=expire_after_real_signature,
    )
    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "confirm",
        str(plan["plan_id"]),
    )
    assert exit_code == 2
    assert not stdout
    assert "correction refused:" in stderr
    assert any(fragment in stderr.lower() for fragment in ("expired", "stale", "lifetime"))
    _assert_no_correction_commit(managed_workspace, transaction_id)
    for connection in connections:
        _assert_connection_closed(connection)


@pytest.mark.parametrize("source_kind", ("text", "receipt"))
def test_managed_correction_cli_text_and_personal_receipt_recover_lost_response(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_kind: str,
) -> None:
    """Exercise managed provision, real terminal wait, reopen/apply and recovery."""
    source = create_posted_managed_transaction(managed_workspace, source_kind)
    transaction_id = source.transaction_public_id
    _assert_provisioned_over_nonempty_finance_ledger(
        managed_workspace,
        transaction_id,
        capsys,
    )

    connections = _connection_observer(monkeypatch)
    expected_before = "12.50" if source_kind == "text" else "12.34"
    expected_route = "text" if source_kind == "text" else "receipt"
    exit_code, stdout, stderr = _invoke_correction_cli(capsys, "show", transaction_id)
    assert exit_code == 0, (stdout, stderr)
    original = json.loads(stdout)
    assert original["route"] == expected_route
    assert original["version"] == 0
    assert original["fields"]["amount"] == expected_before
    assert len(connections) == 1
    _assert_connection_closed(connections[-1])

    exit_code, stdout, stderr = _invoke_correction_cli(
        capsys,
        "preview",
        transaction_id,
        "--reason",
        "synthetic S1D-1 correction",
        "--amount",
        "13.50",
    )
    assert exit_code == 0, (stdout, stderr)
    plan = json.loads(stdout)
    assert plan["route"] == expected_route
    assert plan["target_id"] == transaction_id
    assert plan["source_hash"] == original["original_source"]["source_hash"]
    assert len(connections) == 2
    _assert_connection_closed(connections[-1])
    if source_kind == "receipt":
        assert plan["fact_id"] and plan["fact_hash"]
        assert plan["snapshot_id"] and plan["snapshot_hash"]

    sign_calls = _prepare_real_terminal_signer(managed_workspace, monkeypatch, connections)
    assert connections

    def lose_apply_response(_value: object) -> None:
        raise OSError("synthetic correction CLI response loss")

    with monkeypatch.context() as scoped:
        scoped.setattr(correction_cli, "_emit", lose_apply_response)
        lost_exit, lost_stdout, lost_stderr = _invoke_correction_cli(
            capsys,
            "confirm",
            str(plan["plan_id"]),
        )
    assert lost_exit == 2
    assert not lost_stdout
    assert "synthetic correction CLI response loss" in lost_stderr
    assert sign_calls == [str(plan["plan_id"])]
    assert len(connections) == 4
    for connection in connections:
        _assert_connection_closed(connection)
    _assert_correction_rows(managed_workspace, transaction_id)

    recovered_exit, recovered_stdout, recovered_stderr = _invoke_correction_cli(
        capsys,
        "confirm",
        str(plan["plan_id"]),
    )
    assert recovered_exit == 0, (recovered_stdout, recovered_stderr)
    recovered = json.loads(recovered_stdout)
    assert recovered["recovered"] is True
    assert recovered["applied"]["plan_id"] == plan["plan_id"]
    assert recovered["current"]["version"] == 1
    assert recovered["current"]["fields"]["amount"] == "13.50"
    assert recovered["current"]["original_source"]["source_hash"] == plan["source_hash"]
    assert sign_calls == [str(plan["plan_id"])]
    assert len(connections) == 5
    _assert_connection_closed(connections[-1])
    _assert_correction_rows(managed_workspace, transaction_id)

    if source_kind == "receipt":
        assert recovered["applied"]["snapshot_id"] == plan["snapshot_id"]
        assert recovered["applied"]["snapshot_hash"] == plan["snapshot_hash"]
        assert correction_ledger_counts(managed_workspace)["correction_receipt_facts"] == 1
    else:
        assert correction_ledger_counts(managed_workspace)["correction_receipt_facts"] == 0

    exit_code, stdout, stderr = _invoke_correction_cli(capsys, "show", transaction_id)
    assert exit_code == 0, (stdout, stderr)
    assert json.loads(stdout)["fields"]["amount"] == "13.50"
    _assert_connection_closed(connections[-1])


def test_managed_delivery_receipt_cli_replays_once_and_refuses_bad_binding_and_token_conflict(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    """Use real signed Core receipts; reject invalid proof/source without new rows."""
    pending = prepare_managed_delivery(managed_workspace, "text")
    payload = pending.signed_payload
    table_names = (
        "d2_posting_review_delivery_observations",
        "d2_posting_review_delivery_activations",
        "transactions",
    )
    before = table_counts(managed_workspace, table_names)

    # Bad HMAC must refuse before opening a managed database session.
    session_count = len(managed_workspace.sessions)
    tampered = {**payload, "attempt_nonce": "d2nonce_" + "9" * 32}
    exit_code, response, _stderr = execute_delivery_receipt(managed_workspace, tampered)
    assert exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert response is None
    assert len(managed_workspace.sessions) == session_count
    assert table_counts(managed_workspace, table_names) == before

    # A valid HMAC over another binding reaches Core, then fails source
    # binding with a real managed session and no new observation/activation.
    wrong_binding = resign_delivery_receipt(
        managed_workspace,
        payload,
        session_key="different-managed-binding",
    )
    session_count = len(managed_workspace.sessions)
    exit_code, response, _stderr = execute_delivery_receipt(managed_workspace, wrong_binding)
    assert exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert response is None
    assert len(managed_workspace.sessions) == session_count + 1
    assert table_counts(managed_workspace, table_names) == before

    # The exact signed receipt survives a fresh CLI session and returns the
    # same durable observation identity without duplicating activation.
    first_code, first, first_err = execute_delivery_receipt(managed_workspace, payload)
    replay_code, replay, replay_err = execute_delivery_receipt(managed_workspace, payload)
    assert first_code == errors.EXIT_OK, first_err
    assert replay_code == errors.EXIT_OK, replay_err
    assert first is not None and replay is not None
    assert first["observation_public_id"] == replay["observation_public_id"]
    after = table_counts(managed_workspace, table_names)
    assert after["d2_posting_review_delivery_observations"] == 1
    assert after["d2_posting_review_delivery_activations"] == 1
    assert after["transactions"] == 0

    # Re-sign a conflicting provider result with the already-consumed token.
    # Core must refuse it without changing the accepted observation or activation.
    same_token_conflict = resign_delivery_receipt(
        managed_workspace,
        payload,
        provider_message_id="931",
        receipt_token_sha256=str(payload["receipt_token_sha256"]),
    )
    session_count = len(managed_workspace.sessions)
    conflict_code, conflict_response, _conflict_err = execute_delivery_receipt(
        managed_workspace,
        same_token_conflict,
    )
    assert conflict_code == errors.EXIT_AUTHORITY_REFUSED
    assert conflict_response is None
    assert len(managed_workspace.sessions) == session_count + 1
    assert table_counts(managed_workspace, table_names) == after
