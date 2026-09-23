"""Synthetic terminal display, one-use decision and historical seal checks."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from finance_core.application.corrections import (
    CorrectionFields,
    CorrectionPlan,
    ExpectedDecision,
    ExpectedHistory,
    SignedDecision,
)
from finance_core.calculation.authoritative_snapshot import canonical_json_bytes
from finance_core.correction_adapters import local_authority
from finance_core.correction_adapters import policy as policy_module
from finance_core.correction_adapters.local_authority import (
    LocalApprovalAuthority,
    render_plan,
)
from finance_core.correction_adapters.policy import (
    LocalPolicy,
    LocalPolicyError,
    load_policy_for_connection,
    open_local_authority_connection,
    provision,
    quarantine_incomplete_policy,
)
from finance_core.correction_adapters.wire import (
    DECISION_KEYS,
    CorrectionWireError,
    decision_signature,
    key_id,
    strict_object,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import StagingDatabaseError, create_staging_database


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _policy() -> LocalPolicy:
    key = b"K" * 32
    return LocalPolicy(
        key=key,
        key_id=key_id(key),
        realm="realm_a",
        instance_id="instance_a",
        uid=1,
        actor="actor_a",
        database_path=Path("/private/tmp/synthetic.sqlite"),
        database_dev=1,
        database_ino=2,
        database_uid=1,
        database_mode=0o600,
    )


def _plan(policy: LocalPolicy) -> CorrectionPlan:
    return CorrectionPlan(
        plan_id="corrplan_test",
        correction_id="corr_test",
        authority_id="corrauth_test",
        target_id="txn_test",
        route="text",
        expected_version=0,
        predecessor_id=None,
        predecessor_hash="a" * 64,
        before=CorrectionFields("12.50", "SGD", "2026-09-20", None),
        before_hash="b" * 64,
        after=CorrectionFields("13.50", "SGD", "2026-09-21", "Cafe"),
        after_hash="c" * 64,
        source_hash="d" * 64,
        reason="fix amount",
        actor=policy.actor,
        realm=policy.realm,
        key_id=policy.key_id,
        instance_id=policy.instance_id,
        fact_id=None,
        fact_hash=None,
        snapshot_id=None,
        snapshot_hash=None,
        receipt_json=None,
        created_at_epoch=1000,
        expires_at_epoch=1600,
        plan_hash="e" * 64,
    )


def test_terminal_decision_reconstructs_display_then_seals_one_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    plan = _plan(policy)
    monkeypatch.setattr(local_authority, "_read_policy", lambda: policy)
    nonces = iter(("2" * 64, "3" * 64))
    monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: next(nonces))
    ticks = iter((1100, 1101, 1102, 1103))
    authority = LocalApprovalAuthority(clock=lambda: next(ticks))
    terminal_in = _Terminal(f"CONFIRM {plan.plan_id} {'2' * 64}\n")
    terminal_out = _Terminal()
    signed = authority.sign_with_terminal(
        plan,
        input_stream=terminal_in,
        output_stream=terminal_out,
    )
    assert "Before amount: 12.50" in terminal_out.getvalue()
    assert "After amount: 13.50" in terminal_out.getvalue()
    assert "Before merchant: (unset)" in terminal_out.getvalue()
    verified = authority.verify_fresh(signed, ExpectedDecision(plan, plan.expires_at_epoch))
    assert verified.checked_at_epoch == 1102
    assert verified.correction_id == plan.correction_id
    seal = authority.seal_consumption(verified, "f" * 64)
    history = authority.verify_history(signed, seal, ExpectedHistory(plan, "f" * 64, 1102))
    assert history.checked_at_epoch == 1102
    assert history.decision_digest == verified.decision_digest
    # Historical verification succeeds without sampling today's clock.


def test_nonterminal_and_mismatched_challenge_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    plan = _plan(policy)
    monkeypatch.setattr(local_authority, "_read_policy", lambda: policy)
    authority = LocalApprovalAuthority(clock=lambda: 1100)
    with pytest.raises(LocalPolicyError, match="terminal"):
        authority.sign_with_terminal(plan, input_stream=io.StringIO(), output_stream=_Terminal())
    with pytest.raises(LocalPolicyError, match="terminal"):
        authority.sign_with_terminal(plan, input_stream=_Terminal(), output_stream=io.StringIO())
    monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: "2" * 64)
    with pytest.raises(LocalPolicyError, match="challenge"):
        authority.sign_with_terminal(
            plan,
            input_stream=_Terminal("yes\n"),
            output_stream=_Terminal(),
        )


def test_forged_display_hash_refuses_even_with_valid_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    plan = _plan(policy)
    monkeypatch.setattr(local_authority, "_read_policy", lambda: policy)
    monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: "2" * 64)
    authority = LocalApprovalAuthority(clock=lambda: 1100)
    signed = authority.sign_with_terminal(
        plan,
        input_stream=_Terminal(f"CONFIRM {plan.plan_id} {'2' * 64}\n"),
        output_stream=_Terminal(),
    )
    envelope = strict_object(signed.envelope_json, DECISION_KEYS)
    envelope["display_sha256"] = "0" * 64
    forged = SignedDecision(
        canonical_json_bytes(envelope).decode(), decision_signature(policy.key, envelope)
    )
    with pytest.raises(CorrectionWireError, match="display"):
        authority.verify_fresh(forged, ExpectedDecision(plan, plan.expires_at_epoch))


def test_renderer_escapes_line_and_bidi_controls_without_truncation() -> None:
    plan = replace(_plan(_policy()), reason="first\nsecond\u202eright")
    rendered = render_plan(plan).decode()
    assert "Complete reason: first\\u000asecond\\u202eright" in rendered
    assert "Complete reason: first\nsecond" not in rendered


def _empty_file_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[sqlite3.Connection, Path]:
    root = tmp_path / "runtime_root"
    (root / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(root.resolve()))
    database = tmp_path / "staging" / "ledger.sqlite"
    database.parent.mkdir(mode=0o700)
    conn = create_staging_database(database, migration_paths=TEMP_DB_MIGRATION_PATHS)
    database.chmod(0o600)
    return conn, database


def test_provision_binds_owner_key_and_exact_database_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, database = _empty_file_staging(tmp_path, monkeypatch)
    try:
        policy = provision(database, "original-actor")
        assert policy.actor == "original-actor"
        assert len(policy.key) == 32
        with pytest.raises(LocalPolicyError, match="factory-registered"):
            load_policy_for_connection(conn)
        with open_local_authority_connection() as local:
            assert load_policy_for_connection(local) == policy
            binding = LocalApprovalAuthority().current_binding(local, policy.actor)
            assert binding.instance_id == policy.instance_id
        with pytest.raises(LocalPolicyError, match="factory-registered"):
            load_policy_for_connection(local)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            local.execute("SELECT 1")
        with pytest.raises(LocalPolicyError, match="already exists"):
            provision(database, "original-actor")
        database.chmod(0o640)
        with pytest.raises(LocalPolicyError, match="witness"):
            with open_local_authority_connection():
                pass
        database.chmod(0o600)
        with open_local_authority_connection() as local:
            assert load_policy_for_connection(local) == policy
    finally:
        conn.close()


def test_copied_and_replaced_staging_instance_cannot_reuse_owner_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, database = _empty_file_staging(tmp_path, monkeypatch)
    try:
        provision(database, "original-actor")
        copied = tmp_path / "staging" / "copied.sqlite"
        shutil.copy2(database, copied)
        copied.chmod(0o600)
        copy_conn = sqlite3.connect(copied)
        try:
            with pytest.raises(LocalPolicyError, match="factory-registered"):
                load_policy_for_connection(copy_conn)
            with pytest.raises(StagingDatabaseError, match="copied or renamed"):
                from finance_core.staging_guard import require_staging_database

                require_staging_database(copy_conn)
        finally:
            copy_conn.close()
    finally:
        conn.close()

    # Replacing bytes at the approved path must also fail: the old inode is
    # part of the policy witness, even when the replacement has identical data.
    os.replace(copied, database)
    with pytest.raises(LocalPolicyError, match="changed before connection open"):
        with open_local_authority_connection():
            pass


def test_factory_rejects_path_replacement_in_open_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, database = _empty_file_staging(tmp_path, monkeypatch)
    conn.close()
    provision(database, "original-actor")
    replacement = database.with_name("replacement.sqlite")
    shutil.copy2(database, replacement)
    replacement.chmod(0o600)
    original_open = policy_module.open_staging_database

    def swap_after_sqlite_open(path: Path) -> sqlite3.Connection:
        opened = original_open(path)
        os.replace(replacement, path)
        return opened

    monkeypatch.setattr(policy_module, "open_staging_database", swap_after_sqlite_open)
    with pytest.raises(LocalPolicyError, match="unsafe|during connection open"):
        with open_local_authority_connection():
            pass


def test_registered_connection_rechecks_retained_descriptor_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, database = _empty_file_staging(tmp_path, monkeypatch)
    conn.close()
    provision(database, "original-actor")
    replacement = database.with_name("replacement.sqlite")
    shutil.copy2(database, replacement)
    replacement.chmod(0o600)
    with open_local_authority_connection() as local:
        authority = LocalApprovalAuthority()
        assert authority.current_binding(local, "original-actor").actor == "original-actor"
        os.replace(replacement, database)
        with pytest.raises(LocalPolicyError, match="unsafe|witness"):
            authority.current_binding(local, "original-actor")


def test_incomplete_first_policy_can_only_be_quarantined_with_empty_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, database = _empty_file_staging(tmp_path, monkeypatch)
    try:
        root = Path(os.environ["FINANCE_RUNTIME_ROOT"])
        directory = root / "correction_authority"
        directory.mkdir(mode=0o700)
        invalid = directory / "policy.json"
        invalid.write_bytes(b"{incomplete")
        invalid.chmod(0o600)
        retained = quarantine_incomplete_policy(database)
        assert retained.read_bytes() == b"{incomplete"
        assert not invalid.exists()
        assert provision(database, "original-actor").actor == "original-actor"
        with pytest.raises(LocalPolicyError, match="valid correction policy"):
            quarantine_incomplete_policy(database)
    finally:
        conn.close()


def test_incomplete_policy_is_retained_when_ledger_contains_an_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, database = _empty_file_staging(tmp_path, monkeypatch)
    try:
        directory = Path(os.environ["FINANCE_RUNTIME_ROOT"]) / "correction_authority"
        directory.mkdir(mode=0o700)
        invalid = directory / "policy.json"
        invalid.write_bytes(b"{incomplete")
        invalid.chmod(0o600)
        # Synthetic tamper fixture: even one orphan anchor must block recovery.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """INSERT INTO correction_targets
               (target_id,route,actor,realm,key_id,source_json,source_hash,
                original_hash,projection_json,projection_hash,created_at_epoch)
               VALUES ('txn_orphan','text','actor','realm',?,?,?,?,?,?,1)""",
            ("b" * 64, "{}", "c" * 64, "a" * 64, "{}", "d" * 64),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(LocalPolicyError, match="already contains"):
            quarantine_incomplete_policy(database)
        assert invalid.read_bytes() == b"{incomplete"
    finally:
        conn.close()
