import multiprocessing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from finance_core.instance_admission import (
    InstanceAdmissionError,
    acquire_production_owner,
    admit_production_instance,
    fence_production_instance,
    prepare_production_profile,
    production_read_operation,
    production_write_operation,
    require_finance_write_connection,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database
from tests.instance_admission_support_v1 import synthetic_config


def test_real_production_schema_and_factory_witness(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            require_finance_write_connection(conn)
            from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
                TEMP_DB_MIGRATION_PATHS
            )
            with pytest.raises(StagingDatabaseError):
                require_staging_database(conn)
        with pytest.raises(InstanceAdmissionError):
            require_finance_write_connection(conn)
    raw = sqlite3.connect(profile.live_database)
    try:
        with pytest.raises(StagingDatabaseError):
            require_finance_write_connection(raw)
    finally:
        raw.close()
        profile.close()


def test_fence_is_permanent_and_reads_are_real_readonly(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    fence_production_instance(config, operation_id="fence-1", approval_ref="approved")
    with pytest.raises(InstanceAdmissionError):
        admit_production_instance(config, operation_id="admit-again", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with pytest.raises(InstanceAdmissionError):
            with production_write_operation(owner):
                pass
        with production_read_operation(owner) as conn:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM schema_migrations")


@pytest.mark.parametrize("successor", [False, True])
def test_held_then_fenced_instance_reopens_real_forensics_without_write_revival(
    tmp_path, monkeypatch, successor
):
    from finance_core.instance_admission import (
        STATE_FILENAME,
        abort_maintenance_hold,
        enter_maintenance_hold,
        inspect_instance_admission,
        inspect_maintenance_hold,
        require_finance_read_connection,
    )

    original_root = tmp_path / "original"
    original_root.mkdir(mode=0o700)
    config = synthetic_config(original_root, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('historical','Original')"
            )
            conn.commit()
        hold = enter_maintenance_hold(config, operation_id="held-original", approval_ref="approved")
        for operation in (production_read_operation, production_write_operation):
            with pytest.raises(InstanceAdmissionError):
                with operation(owner):
                    pytest.fail("ACTIVE maintenance hold issued an operation")
        fence_production_instance(config, operation_id="fence-original", approval_ref="approved")
    next_config = None
    if successor:
        next_root = tmp_path / "successor"
        next_root.mkdir(mode=0o700)
        next_config = synthetic_config(
            next_root, monkeypatch, control_root=config.control_root, instance_id="fresh-successor"
        )
        prepare_production_profile(
            next_config, operation_id="prepare-successor", approval_ref="approved"
        ).close()
        admit_production_instance(
            next_config, operation_id="admit-successor", approval_ref="approved"
        )
        monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(profile.runtime))
    expected_hold = hold["maintenance"]
    assert inspect_instance_admission(config)["state"] == "FENCED"
    assert inspect_maintenance_hold(config) == expected_hold
    history = (config.control_root / STATE_FILENAME).read_bytes()
    # Close/reacquire the actual process owner before historical SQL; successor
    # admission must not transfer the original instance's write capability.
    with acquire_production_owner(config) as reopened:
        with production_read_operation(reopened) as conn:
            require_finance_read_connection(conn)
            cursor = conn.execute("SELECT public_id,display_name FROM participants")
            assert tuple(cursor.fetchone()) == ("historical", "Original")
            cursor.close()
            assert conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0
            with pytest.raises(InstanceAdmissionError, match="ACTIVE writable"):
                require_finance_write_connection(conn)
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(
                    "INSERT INTO participants(public_id,display_name) VALUES ('revived','Refused')"
                )
            assert conn.total_changes == 0 and not conn.in_transaction
        with pytest.raises(InstanceAdmissionError):
            with production_write_operation(reopened):
                pytest.fail("FENCED retained-hold instance revived write authority")
        with pytest.raises(InstanceAdmissionError, match="cannot safely return"):
            abort_maintenance_hold(
                config,
                operation_id="forbidden-abort",
                approval_ref="approved",
                evidence_ref="synthetic-zero-effects",
            )
    assert inspect_maintenance_hold(config) == expected_hold
    assert (config.control_root / STATE_FILENAME).read_bytes() == history
    if next_config is not None:
        next_config.close()
    profile.close()
    config.close()


def test_exact_journal_resume_after_publication_failure(tmp_path, monkeypatch):
    from finance_core.instance_admission import (
        PENDING_FILENAME,
        authority,
        inspect_instance_admission,
        resume_authority_transition,
    )

    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    original_replace = authority.os.replace

    def fail_replace(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(authority.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic publication"):
        admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    assert (config.control_root / PENDING_FILENAME).is_file()
    with pytest.raises(InstanceAdmissionError, match="pending"):
        inspect_instance_admission(config)
    with pytest.raises(InstanceAdmissionError, match="exact pending"):
        resume_authority_transition(config, operation_id="wrong", approval_ref="approved")
    monkeypatch.setattr(authority.os, "replace", original_replace)
    assert (
        resume_authority_transition(config, operation_id="admit-1", approval_ref="approved")[
            "state"
        ]
        == "ACTIVE"
    )
    assert not (config.control_root / PENDING_FILENAME).exists()
    config.close()


def test_business_instance_and_owner_lifetime_are_bound(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with pytest.raises(InstanceAdmissionError, match="lifetime owner"):
            config.close()
        with production_write_operation(owner) as conn:
            require_finance_write_connection(conn, instance_id=config.instance_id)
            with pytest.raises(InstanceAdmissionError, match="another production instance"):
                require_finance_write_connection(conn, instance_id="old-instance")
            cursor = conn.execute("SELECT 1")
            with pytest.raises(InstanceAdmissionError, match="before actual operation close"):
                owner.close()
        with pytest.raises(InstanceAdmissionError):
            cursor.fetchone()
    config.close()
    config.close()
    with pytest.raises(InstanceAdmissionError):
        acquire_production_owner(config)


@pytest.mark.parametrize("payload", [b"{}", b'{"a":1,"a":2}', b"x" * 65537])
def test_invalid_current_state_never_admits_runtime(tmp_path, monkeypatch, payload):
    from finance_core.instance_admission import STATE_FILENAME, inspect_instance_admission

    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    (config.control_root / STATE_FILENAME).write_bytes(payload)
    with pytest.raises(InstanceAdmissionError):
        inspect_instance_admission(config)
    config.close()


def test_rejected_prepare_is_empty_and_existing_failed_target_is_not_adopted(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    with pytest.raises(InstanceAdmissionError, match="approval"):
        prepare_production_profile(config, operation_id="prepare-1", approval_ref="rejected")
    from finance_core.profile_layout import profile_root

    database = (
        profile_root(config.locator_root, config.profile_id, config.layout)
        / "runtime/database/finance.db"
    )
    assert not database.exists()
    database.write_bytes(b"failed bootstrap evidence")
    database.chmod(0o600)
    with pytest.raises(InstanceAdmissionError, match="blank database"):
        prepare_production_profile(config, operation_id="prepare-2", approval_ref="approved")
    assert database.read_bytes() == b"failed bootstrap evidence"
    config.close()


def test_successor_shares_global_owner_and_preserves_permanent_original_fence(
    tmp_path, monkeypatch
):
    import json

    from finance_core.instance_admission import (
        STATE_FILENAME,
        InstanceAdmissionBusy,
        inspect_instance_admission,
    )

    first_root, next_root = tmp_path / "original", tmp_path / "successor"
    first_root.mkdir(mode=0o700)
    next_root.mkdir(mode=0o700)
    original = synthetic_config(first_root, monkeypatch)
    prepare_production_profile(
        original, operation_id="prepare-original", approval_ref="approved"
    ).close()
    admit_production_instance(original, operation_id="admit-original", approval_ref="approved")
    fence_production_instance(original, operation_id="fence-original", approval_ref="approved")
    successor = synthetic_config(
        next_root,
        monkeypatch,
        control_root=original.control_root,
        instance_id="synthetic-successor-v1",
    )
    prepare_production_profile(
        successor, operation_id="prepare-successor", approval_ref="approved"
    ).close()
    admit_production_instance(successor, operation_id="admit-successor", approval_ref="approved")
    with acquire_production_owner(successor):
        with pytest.raises(InstanceAdmissionBusy):
            acquire_production_owner(original, timeout_seconds=0)
    assert inspect_instance_admission(original)["state"] == "FENCED"
    assert inspect_instance_admission(successor)["state"] == "ACTIVE"
    state = json.loads((original.control_root / STATE_FILENAME).read_bytes())
    assert len(state["history"]) == state["version"] == 5
    assert not list(original.control_root.glob("transition-*"))
    assert {entry.name for entry in original.control_root.iterdir()} == {
        STATE_FILENAME,
        ".owner.v1.lock",
        ".authority.v1.lock",
    }
    original.close()
    successor.close()


def test_internal_history_corruption_fails_closed(tmp_path, monkeypatch):
    import json

    from finance_core.instance_admission import STATE_FILENAME, inspect_instance_admission

    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    state_path = config.control_root / STATE_FILENAME
    state = json.loads(state_path.read_bytes())
    state["history"][0]["operation_id"] = "changed-history"
    state_path.write_bytes(json.dumps(state, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(InstanceAdmissionError, match="history chain"):
        inspect_instance_admission(config)
    config.close()


@pytest.mark.parametrize(
    "commit_mode",
    [
        "explicit",
        "sqlite-context",
        "sql-commit",
        "sql-end",
        "outer-release",
        "script",
        "isolation",
        "autocommit",
    ],
)
@pytest.mark.parametrize("drift", ["permissions", "main-path"])
def test_main_identity_drift_rejects_actual_commit(tmp_path, monkeypatch, commit_mode, drift):
    from finance_core.profile_paths import ProfilePathError

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            # Synthetic uncommitted schema effect will be rolled back by operation cleanup.
            conn.execute(
                "SAVEPOINT drift_outer" if commit_mode == "outer-release" else "BEGIN IMMEDIATE"
            )
            conn.execute("CREATE TABLE drift_commit_probe(value TEXT)")
            conn.execute("INSERT INTO drift_commit_probe VALUES ('never-committed')")
            saved = tmp_path / "pinned-original.sqlite"
            if drift == "permissions":
                profile.live_database.chmod(0o640)
            else:
                profile.live_database.rename(saved)
                profile.live_database.write_bytes(b"replaced database identity")
                profile.live_database.chmod(0o600)
            try:
                with pytest.raises((InstanceAdmissionError, ProfilePathError)):
                    if commit_mode == "explicit":
                        conn.commit()
                    elif commit_mode == "sqlite-context":
                        conn.__exit__(None, None, None)
                    elif commit_mode == "sql-commit":
                        conn.execute("COMMIT")
                    elif commit_mode == "sql-end":
                        conn.execute("END")
                    elif commit_mode == "outer-release":
                        conn.execute("RELEASE drift_outer")
                    elif commit_mode == "script":
                        conn.executescript("SELECT 1;")
                    elif commit_mode == "isolation":
                        conn.isolation_level = None
                    else:
                        conn.autocommit = True
                assert conn.in_transaction
            finally:
                if drift == "permissions":
                    profile.live_database.chmod(0o600)
                else:
                    profile.live_database.unlink()
                    saved.rename(profile.live_database)
        with production_read_operation(owner) as conn:
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='drift_commit_probe'"
                ).fetchone()
                is None
            )
    profile.close()
    config.close()


def test_exact_completed_transition_recovers_lost_return_without_republication(
    tmp_path, monkeypatch
):
    from finance_core.instance_admission import (
        STATE_FILENAME,
        inspect_instance_admission,
        resume_authority_transition,
    )

    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    committed = admit_production_instance(
        config, operation_id="admit-lost-return", approval_ref="approved"
    )
    before = (config.control_root / STATE_FILENAME).read_bytes()
    assert (
        resume_authority_transition(
            config, operation_id="admit-lost-return", approval_ref="approved"
        )
        == committed
    )
    assert (config.control_root / STATE_FILENAME).read_bytes() == before
    assert inspect_instance_admission(config) == committed
    with pytest.raises(InstanceAdmissionError):
        resume_authority_transition(config, operation_id="wrong", approval_ref="approved")
    with pytest.raises(InstanceAdmissionError):
        resume_authority_transition(config, operation_id="admit-lost-return", approval_ref="wrong")
    config.close()


def test_completed_recovery_uses_original_approval_binding_and_current_fenced_state(
    tmp_path, monkeypatch
):
    from finance_core.instance_admission import STATE_FILENAME, resume_authority_transition

    recorded = {}
    strict = False

    def verifier(reference, action, binding):
        if reference != "approved":
            return False
        if strict:
            return recorded.get(action) == binding
        recorded[action] = binding
        return True

    config = synthetic_config(tmp_path, monkeypatch, verifier=verifier)
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    fence_production_instance(config, operation_id="fence-1", approval_ref="approved")
    strict = True
    state = (config.control_root / STATE_FILENAME).read_bytes()
    assert (
        resume_authority_transition(config, operation_id="admit-1", approval_ref="approved")[
            "state"
        ]
        == "FENCED"
    )
    original_digest = recorded["admit"]
    recorded["admit"] = "wrong-original-binding"
    with pytest.raises(InstanceAdmissionError, match="approval binding"):
        resume_authority_transition(config, operation_id="admit-1", approval_ref="approved")
    recorded["admit"] = original_digest
    assert (config.control_root / STATE_FILENAME).read_bytes() == state
    config.close()


def test_initial_prepare_publication_crash_has_durable_empty_predecessor(tmp_path, monkeypatch):
    import json

    from finance_core.instance_admission import (
        STATE_FILENAME,
        authority,
        resume_authority_transition,
    )

    config = synthetic_config(tmp_path, monkeypatch)
    original_replace = authority.os.replace

    def lost_publication(*args, **kwargs):
        raise OSError("synthetic initial publication crash")

    monkeypatch.setattr(authority.os, "replace", lost_publication)
    with pytest.raises(OSError, match="initial publication"):
        prepare_production_profile(
            config, operation_id="prepare-initial-crash", approval_ref="approved"
        )
    initial = json.loads((config.control_root / STATE_FILENAME).read_bytes())
    assert initial["version"] == 0 and initial["instances"] == {} and initial["history"] == []
    monkeypatch.setattr(authority.os, "replace", original_replace)
    assert (
        resume_authority_transition(
            config, operation_id="prepare-initial-crash", approval_ref="approved"
        )["state"]
        == "PREPARED"
    )
    assert (
        resume_authority_transition(
            config, operation_id="prepare-initial-crash", approval_ref="approved"
        )["state"]
        == "PREPARED"
    )
    assert (
        admit_production_instance(
            config, operation_id="admit-after-resume", approval_ref="approved"
        )["state"]
        == "ACTIVE"
    )
    config.close()


@pytest.mark.parametrize("drift", ["ledger-valid-content", "manifest-content"])
def test_maintenance_abort_requires_unchanged_recorded_protected_baseline(
    tmp_path, monkeypatch, drift
):
    import json

    from finance_core.instance_admission import (
        abort_maintenance_hold,
        enter_maintenance_hold,
        inspect_maintenance_hold,
    )

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    original = enter_maintenance_hold(
        config, operation_id="hold-baseline", approval_ref="approved"
    )["maintenance"]
    if drift == "ledger-valid-content":
        raw = sqlite3.connect(profile.live_database)
        raw.execute(
            "INSERT INTO participants(public_id,display_name) "
            "VALUES ('protected-change', 'changed-protected-fact')"
        )
        raw.commit()
        raw.close()
    else:
        manifest = json.loads(profile.profile_json.read_text())
        manifest["metadata_revision"] = "changed-with-same-inode"
        profile.profile_json.write_text(json.dumps(manifest))
    with pytest.raises(InstanceAdmissionError, match="baseline"):
        abort_maintenance_hold(
            config,
            operation_id="abort-drift",
            approval_ref="approved",
            evidence_ref="exact-zero-effects-evidence",
        )
    assert inspect_maintenance_hold(config) == original
    profile.close()
    config.close()


def test_pending_safe_abort_rechecks_baseline_and_retains_exact_journal(tmp_path, monkeypatch):
    from finance_core.instance_admission import (
        PENDING_FILENAME,
        abort_maintenance_hold,
        authority,
        enter_maintenance_hold,
        resume_authority_transition,
    )

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    enter_maintenance_hold(config, operation_id="hold-1", approval_ref="approved")
    original_replace = authority.os.replace

    def fail_publication(*args, **kwargs):
        raise OSError("synthetic safe abort publication crash")

    monkeypatch.setattr(authority.os, "replace", fail_publication)
    with pytest.raises(OSError, match="publication crash"):
        abort_maintenance_hold(
            config,
            operation_id="abort-pending",
            approval_ref="approved",
            evidence_ref="bound-hold-1-evidence",
        )
    monkeypatch.setattr(authority.os, "replace", original_replace)
    journal = (config.control_root / PENDING_FILENAME).read_bytes()
    raw = sqlite3.connect(profile.live_database)
    raw.execute(
        "INSERT INTO participants(public_id,display_name) VALUES ('pending-change','changed')"
    )
    raw.commit()
    with pytest.raises(InstanceAdmissionError, match="baseline"):
        resume_authority_transition(config, operation_id="abort-pending", approval_ref="approved")
    assert (config.control_root / PENDING_FILENAME).read_bytes() == journal
    raw.execute("DELETE FROM participants WHERE public_id='pending-change'")
    raw.commit()
    raw.close()
    resumed = resume_authority_transition(
        config, operation_id="abort-pending", approval_ref="approved"
    )
    assert resumed["state"] == "ACTIVE" and resumed["maintenance"] is None
    profile.close()
    config.close()


def test_unchanged_maintenance_baseline_ignores_wal_checkpoint_and_binds_evidence_to_hold(
    tmp_path, monkeypatch
):
    from finance_core.instance_admission import (
        abort_maintenance_hold,
        enter_maintenance_hold,
        inspect_maintenance_hold,
    )

    approved_digest = None

    def verifier(ref, action, digest):
        nonlocal approved_digest
        if action != "safe_abort":
            return ref == "approved"
        if ref != "hold-1-exact-approval":
            return False
        if approved_digest is None:
            approved_digest = digest
        return digest == approved_digest

    config = synthetic_config(tmp_path, monkeypatch, verifier=verifier)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    raw = sqlite3.connect(profile.live_database)
    assert raw.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    raw.close()
    first = enter_maintenance_hold(config, operation_id="hold-1", approval_ref="approved")[
        "maintenance"
    ]
    raw = sqlite3.connect(profile.live_database)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    raw.close()
    assert (
        abort_maintenance_hold(
            config,
            operation_id="abort-1",
            approval_ref="hold-1-exact-approval",
            evidence_ref="hold-1-no-effects",
        )["maintenance"]
        is None
    )
    second = enter_maintenance_hold(config, operation_id="hold-2", approval_ref="approved")[
        "maintenance"
    ]
    assert first["baseline"]["database_sha256"] == second["baseline"]["database_sha256"]
    assert first["baseline"]["authority_version"] != second["baseline"]["authority_version"]
    with pytest.raises(InstanceAdmissionError, match="approval"):
        abort_maintenance_hold(
            config,
            operation_id="abort-wrong-hold",
            approval_ref="hold-1-exact-approval",
            evidence_ref="hold-1-no-effects",
        )
    assert inspect_maintenance_hold(config) == second
    fence_production_instance(config, operation_id="fence-held", approval_ref="approved")
    with pytest.raises(InstanceAdmissionError):
        abort_maintenance_hold(
            config,
            operation_id="abort-fenced",
            approval_ref="hold-1-exact-approval",
            evidence_ref="hold-1-no-effects",
        )
    profile.close()
    config.close()


def test_unissued_lookalike_connection_cannot_inherit_equal_hash_witness(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:

            class Lookalike(sqlite3.Connection):
                def __hash__(self):
                    return hash(conn)

                def __eq__(self, other):
                    return True

            raw = sqlite3.connect(profile.live_database, factory=Lookalike)
            try:
                assert raw is not conn
                with pytest.raises(StagingDatabaseError):
                    require_finance_write_connection(raw)
            finally:
                raw.close()
    profile.close()
    config.close()


def test_guarded_sql_preserves_default_transactions_and_rechecks_each_generator_row(
    tmp_path, monkeypatch
):
    from finance_core.profile_paths import ProfilePathError

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            assert conn.isolation_level == ""
            assert conn.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
            conn.isolation_level = ""
            conn.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('sql-commit','SQL')"
            )
            conn.execute("COMMIT")
            conn.execute("SAVEPOINT supported_outer")
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('sql-release','SQL')"
            )
            conn.execute("RELEASE supported_outer")
            for execute_script in (conn.executescript, conn.cursor().executescript):
                with pytest.raises(InstanceAdmissionError, match="executescript"):
                    execute_script("SELECT 1;")
            for mode, value in (
                ("isolation_level", None),
                ("isolation_level", "DEFERRED"),
                ("autocommit", True),
                ("autocommit", False),
            ):
                with pytest.raises(InstanceAdmissionError, match="fixed"):
                    setattr(conn, mode, value)

            callback_calls = []
            conn.row_factory = lambda cursor, row: callback_calls.append(True) or row
            try:
                with pytest.raises(InstanceAdmissionError, match="configured"):
                    conn.execute("COMMIT")
                assert callback_calls == []
            finally:
                conn.row_factory = sqlite3.Row
            conn.text_factory = bytes
            try:
                with pytest.raises(InstanceAdmissionError, match="configured"):
                    conn.execute("SELECT 1")
            finally:
                conn.text_factory = str

            def changes_context_after_first_row():
                yield ("uncommitted-first", "First")
                profile.live_database.chmod(0o640)
                yield ("uncommitted-second", "Second")

            try:
                conn.execute("BEGIN IMMEDIATE")
                with pytest.raises((InstanceAdmissionError, ProfilePathError)):
                    conn.executemany(
                        "INSERT INTO participants(public_id,display_name) VALUES (?,?)",
                        changes_context_after_first_row(),
                    )
                assert conn.in_transaction
            finally:
                profile.live_database.chmod(0o600)
        with production_read_operation(owner) as conn:
            assert [
                row[0]
                for row in conn.execute("SELECT public_id FROM participants ORDER BY public_id")
            ] == ["sql-commit", "sql-release"]
    profile.close()
    config.close()


@pytest.mark.parametrize("surface", ["connection", "cursor"])
@pytest.mark.parametrize("read_only", [False, True])
def test_script_api_refusal_preserves_caller_transaction_and_has_no_durable_effect(
    tmp_path, monkeypatch, surface, read_only
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    payload = (
        "COMMIT; CREATE TABLE forbidden_script_probe(value TEXT); "
        "INSERT INTO participants(public_id,display_name) VALUES ('script-effect','Forbidden');"
    )
    with acquire_production_owner(config) as owner:
        operation = production_read_operation if read_only else production_write_operation
        with operation(owner) as conn:
            conn.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            if not read_only:
                conn.execute(
                    "INSERT INTO participants(public_id,display_name) VALUES ('pending','Owned')"
                )
            script_api = (
                conn.executescript if surface == "connection" else conn.cursor().executescript
            )
            before = conn.total_changes
            with pytest.raises(InstanceAdmissionError, match="executescript implicit commits"):
                script_api(payload)
            assert conn.in_transaction and conn.total_changes == before
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='forbidden_script_probe'"
                ).fetchone()
                is None
            )
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == int(
                not read_only
            )
            conn.rollback()
            assert not conn.in_transaction
            conn.close()
            with pytest.raises(InstanceAdmissionError, match="Closed, forked, stale"):
                script_api(payload)
        # Operation cleanup removes the witness; the escaped API must refuse
        # the now-unissued handle as well as the closed handle checked above.
        with pytest.raises(InstanceAdmissionError, match="not issued by operation owner"):
            script_api(payload)
        # Verify after actual close using the next issued mode=ro operation;
        # never open another native main handle while the first is active.
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='forbidden_script_probe'"
                ).fetchone()
                is None
            )
    profile.close()
    config.close()


@pytest.mark.parametrize("statement", ["select", "returning"])
@pytest.mark.parametrize("explicit_close", [False, True])
def test_partial_public_cursor_finalized_before_connection_and_lease_close(
    tmp_path, monkeypatch, statement, explicit_close
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO participants(public_id,display_name) VALUES (?,?)",
                [("cursor-first", "First"), ("cursor-second", "Second")],
            )
            conn.commit()
        operation = (
            production_read_operation if statement == "select" else production_write_operation
        )
        with operation(owner) as conn:
            cursor = conn.cursor()
            if statement == "select":
                cursor.execute("SELECT public_id FROM participants ORDER BY public_id")
            else:
                conn.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "INSERT INTO participants(public_id,display_name) "
                    "VALUES ('returning-first','First'),('returning-second','Second') "
                    "RETURNING public_id"
                )
            assert cursor.fetchone() is not None
            if explicit_close:
                conn.close()
    # A separate actual SQLite handle must be able to acquire its write lock
    # while the escaped cursor remains strongly referenced by this test.
    raw = sqlite3.connect(profile.live_database, timeout=0)
    try:
        raw.execute("BEGIN EXCLUSIVE")
        raw.rollback()
        assert raw.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 2
    finally:
        raw.close()
    for reuse in (cursor.fetchone, cursor.close, lambda: cursor.execute("SELECT 1")):
        with pytest.raises(InstanceAdmissionError):
            reuse()
    profile.close()
    config.close()


def _uncertain_public_cursor_close_worker(tmp_path, close_surface, fault_kind, sender):
    import finance_core.instance_admission.sessions as sessions
    from finance_core.instance_admission import ProductionCloseUncertain

    config = synthetic_config(tmp_path, pytest.MonkeyPatch())
    prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    owner = acquire_production_owner(config)
    operation = production_read_operation(owner)
    conn = operation.__enter__()
    cursor = conn.execute("SELECT 1 UNION ALL SELECT 2")
    assert cursor.fetchone()[0] == 1
    registered = tuple(sessions._witness_for(conn).cursors.values())
    cursor_close_attempts = []
    connection_close_attempts = []

    def unknown_cursor_close(_cursor):
        cursor_close_attempts.append(_cursor)
        if fault_kind == "cursor":
            raise sqlite3.OperationalError("synthetic cursor close uncertainty")
        sqlite3.Cursor.close(_cursor)

    def unexpected_connection_close(_connection):
        connection_close_attempts.append(True)
        if fault_kind == "connection":
            raise sqlite3.OperationalError("synthetic connection close uncertainty")
        raise AssertionError("connection cannot close after cursor close uncertainty")

    sessions._close_cursor_handle = unknown_cursor_close
    sessions._close_sqlite_handle = unexpected_connection_close
    close_call = {
        "cursor": cursor.close,
        "connection": conn.close,
        "operation": lambda: operation.__exit__(None, None, None),
    }[close_surface]
    with pytest.raises(ProductionCloseUncertain):
        close_call()
    if close_surface != "operation":
        with pytest.raises(ProductionCloseUncertain):
            operation.__exit__(None, None, None)
    with pytest.raises(InstanceAdmissionError):
        owner.close()
    with pytest.raises(InstanceAdmissionError):
        conn.execute("SELECT 1")
    with pytest.raises(InstanceAdmissionError):
        config.close()
    # A separate lease descriptor must still observe the retained owner EX lock.
    with pytest.raises(InstanceAdmissionError):
        acquire_production_owner(config, timeout_seconds=0)
    witness = sessions._witness_for(conn)
    if fault_kind == "cursor":
        all_expected_closes = len(cursor_close_attempts) == 1
    else:
        # Strong ownership also retains exhausted initialization cursors.
        # Every issued cursor must be closed once before actual parent close.
        all_expected_closes = len(cursor_close_attempts) == len(registered) and {
            id(item) for item in cursor_close_attempts
        } == {id(item) for item in registered}
    sender.send(
        (
            all_expected_closes,
            len(connection_close_attempts),
            # Native status raises on an actually closed handle, but cannot
            # execute SQL after the fixed hook has latched uncertain cleanup.
            int(not conn.in_transaction),
            witness is not None and any(item is cursor for item in witness.retained_cursors),
        )
    )
    sender.close()
    os._exit(0)


@pytest.mark.parametrize(
    ("close_surface", "fault_kind"),
    [
        ("cursor", "cursor"),
        ("connection", "cursor"),
        ("operation", "cursor"),
        ("connection", "connection"),
        ("operation", "connection"),
    ],
)
def test_unknown_public_cursor_close_retains_handle_and_leases(tmp_path, close_surface, fault_kind):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_uncertain_public_cursor_close_worker,
        args=(tmp_path, close_surface, fault_kind, sender),
    )
    child.start()
    sender.close()
    try:
        assert receiver.poll(30), "isolated close proof did not return"
        assert receiver.recv() == (True, int(fault_kind == "connection"), 1, fault_kind == "cursor")
        child.join(10)
        assert child.exitcode == 0
    finally:
        receiver.close()
        if child.is_alive():
            child.kill()
            child.join(10)


@pytest.mark.parametrize("buffer_type", [bytearray, memoryview])
@pytest.mark.parametrize("adapter_type", ["original", "normalized"])
def test_binding_adapters_never_run_or_change_caller_transaction(
    tmp_path, monkeypatch, buffer_type, adapter_type
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    calls = []

    def changes_profile_after_admission(value):
        calls.append(value)
        profile.live_database.chmod(0o640)
        return "adapted-after-check"

    key = (buffer_type if adapter_type == "original" else bytes, sqlite3.PrepareProtocol)
    monkeypatch.setitem(sqlite3.adapters, key, changes_profile_after_admission)
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            for pending in (False, True):
                # Both cases are caller-owned transactions: an empty group,
                # and a group already carrying an unrelated uncommitted row.
                conn.execute("BEGIN IMMEDIATE")
                if pending:
                    conn.execute(
                        "INSERT INTO participants(public_id,display_name) "
                        "VALUES ('pending','Owned')"
                    )
                before = conn.total_changes
                for surface in ("connection", "cursor", "many", "named"):
                    value = buffer_type(b"probe")
                    sql = (
                        "WITH p(v) AS (SELECT ?) INSERT INTO participants(public_id,display_name) "
                        "SELECT 'adapter-event',v FROM p"
                    )
                    try:
                        with pytest.raises(InstanceAdmissionError, match="adapters"):
                            if surface == "connection":
                                conn.execute(sql, (value,))
                            elif surface == "cursor":
                                conn.cursor().execute(sql, [value])
                            elif surface == "many":
                                conn.executemany(sql, [(value,)])
                            else:
                                conn.execute(sql.replace("?", ":value"), {"value": value})
                    finally:
                        profile.live_database.chmod(0o600)
                    assert calls == []
                    assert conn.in_transaction
                    assert conn.total_changes == before
                if pending:
                    row = conn.execute("SELECT display_name FROM participants").fetchone()
                    assert row[0] == "Owned"
                conn.rollback()
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


def test_blob_handles_refused_without_transaction_or_durable_effects(tmp_path, monkeypatch):
    from finance_core.instance_admission import ApplicationSchemaContract

    schema = ApplicationSchemaContract(
        "blob-refusal-v1", ("CREATE TABLE app_blob_probe(id INTEGER PRIMARY KEY,payload BLOB)",)
    )
    config = synthetic_config(tmp_path, monkeypatch, application_schema=schema)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO app_blob_probe VALUES (1,zeroblob(4))")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('pending','Owned')"
            )
            before = conn.total_changes
            for readonly in (False, True):
                with pytest.raises(InstanceAdmissionError, match="blobopen"):
                    conn.blobopen("app_blob_probe", "payload", 1, readonly=readonly)
                assert conn.in_transaction and conn.total_changes == before
            conn.rollback()
        with production_read_operation(owner) as conn:
            with pytest.raises(InstanceAdmissionError, match="blobopen"):
                conn.blobopen("app_blob_probe", "payload", 1, readonly=True)
            assert conn.execute("SELECT payload FROM app_blob_probe").fetchone()[0] == b"\0" * 4
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


def test_cursor_result_factories_refuse_callbacks_and_preserve_owned_transaction(
    tmp_path, monkeypatch
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    calls = []

    def callback(cursor, row):
        calls.append(row)
        profile.live_database.chmod(0o640)
        return row

    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('pending','Owned')"
            )
            before = conn.total_changes
            cursor = conn.cursor()
            with pytest.raises(InstanceAdmissionError, match="factories"):
                cursor.row_factory = callback
            assert cursor.row_factory is sqlite3.Row
            for factory in (None, sqlite3.Row):
                cursor.row_factory = factory
                row = cursor.execute("SELECT 1").fetchone()
                assert row[0] == 1 and type(row) is (tuple if factory is None else sqlite3.Row)
            # SQLite copies the connection factory without using the cursor setter.
            conn.row_factory = callback
            try:
                copied = conn.cursor()
            finally:
                conn.row_factory = sqlite3.Row
            with pytest.raises(InstanceAdmissionError, match="factories"):
                copied.execute("SELECT 1")
            assert calls == [] and conn.in_transaction and conn.total_changes == before
            conn.rollback()
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


def test_fetchmany_refuses_index_callback_without_consuming_rows(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    calls = []

    class CallbackSize:
        def __index__(self):
            calls.append(True)
            profile.live_database.chmod(0o640)
            return 1

    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('pending','Owned')"
            )
            before = conn.total_changes
            cursor = conn.execute("SELECT 1 UNION ALL SELECT 2")
            try:
                with pytest.raises(InstanceAdmissionError, match="integer"):
                    cursor.fetchmany(CallbackSize())
            finally:
                profile.live_database.chmod(0o600)
            assert calls == [] and conn.in_transaction and conn.total_changes == before
            assert cursor.fetchmany(1)[0][0] == 1
            assert cursor.fetchmany()[0][0] == 2
            conn.rollback()
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO participants(public_id,display_name) VALUES ('idle','Idle')",
        "WITH p(v) AS (SELECT 'idle') INSERT INTO participants(public_id,display_name) "
        "SELECT v,v FROM p RETURNING public_id",
        "WITH p(v) AS (VALUES ('idle'),('idle-second')) "
        "INSERT INTO participants(public_id,display_name) SELECT v,v FROM p RETURNING public_id",
    ],
)
def test_idle_writes_refused_before_effect_and_permission_not_cached(
    tmp_path, monkeypatch, statement
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            assert not conn.in_transaction
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                conn.execute(statement)
            assert not conn.in_transaction and conn.total_changes == 0
            conn.execute("BEGIN IMMEDIATE")
            assert conn.in_transaction
            cursor = conn.execute(statement)
            cursor.close()
            conn.rollback()
            # Identical SQL must compile against the CURRENT native transaction.
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                conn.execute(statement)
            assert not conn.in_transaction
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


def test_final_shared_config_proof_still_rejects_installed_migration_drift(tmp_path, monkeypatch):
    from finance_core.instance_admission import authority, sessions

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("SELECT 'original-first' UNION ALL SELECT 'original-last'")
            before = conn.total_changes
            witness = sessions._witness_for(conn)
            with monkeypatch.context() as drift:
                # Synthetic installed-identity drift; real migration resources
                # and their historical bytes are never modified.
                drift.setattr(authority, "_migration_contract_digest", lambda: "0" * 64)
                for attempt in (
                    owner.assert_valid,
                    witness.control.assert_valid,
                    lambda: conn.execute(
                        "INSERT INTO participants(public_id,display_name) VALUES ('drift','Drift')"
                    ),
                    lambda: conn.executemany(
                        "INSERT INTO participants(public_id,display_name) VALUES (?,?)",
                        [("drift-many", "Drift")],
                    ),
                    cursor.fetchone,
                    cursor.close,
                    conn.commit,
                ):
                    with pytest.raises(
                        InstanceAdmissionError, match="Installed migration identity"
                    ):
                        attempt()
                    assert conn.in_transaction and conn.total_changes == before
            # Rejected fetch/reuse/close must leave the original row unconsumed.
            assert cursor.fetchone()[0] == "original-first"
            cursor.close()
            conn.rollback()
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


@pytest.mark.parametrize("lease_role", ["owner", "control"])
def test_different_config_lease_cannot_borrow_shared_config_proof(
    tmp_path, monkeypatch, lease_role
):
    from finance_core.instance_admission import sessions

    original_root, other_root = tmp_path / "original", tmp_path / "other"
    original_root.mkdir(mode=0o700)
    other_root.mkdir(mode=0o700)
    config = synthetic_config(original_root, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    # Construct another valid trusted composition without preparing/admitting it.
    # Restore the original runtime binding before touching the issued operation.
    other = synthetic_config(other_root, monkeypatch)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(profile.runtime))
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("SELECT 'original'")
            witness = sessions._witness_for(conn)
            lease = owner._lease if lease_role == "owner" else witness.control
            before = conn.total_changes
            with monkeypatch.context() as crossed:
                crossed.setattr(lease, "config", other)
                for attempt in (
                    lambda: conn.execute(
                        "INSERT INTO participants(public_id,display_name) "
                        "VALUES ('crossed','Crossed')"
                    ),
                    cursor.fetchone,
                    cursor.close,
                    conn.commit,
                ):
                    with pytest.raises(InstanceAdmissionError, match="same trusted configuration"):
                        attempt()
                    assert conn.in_transaction and conn.total_changes == before
            assert cursor.fetchone()[0] == "original"
            cursor.close()
            conn.rollback()
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    other.close()
    profile.close()
    config.close()


@pytest.mark.parametrize("transaction_end", ["commit", "rollback"])
def test_executemany_generator_cannot_write_after_ending_transaction(
    tmp_path, monkeypatch, transaction_end
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")

            def rows():
                yield ("first", "First")
                getattr(conn, transaction_end)()
                yield ("second", "Second")

            with pytest.raises(InstanceAdmissionError, match="explicit caller transaction"):
                conn.executemany(
                    "INSERT INTO participants(public_id,display_name) VALUES (?,?)", rows()
                )
            assert not conn.in_transaction
        with production_read_operation(owner) as conn:
            assert [row[0] for row in conn.execute("SELECT public_id FROM participants")] == (
                ["first"] if transaction_end == "commit" else []
            )
    profile.close()
    config.close()


@pytest.mark.parametrize(
    "surface", ["one", "many", "all", "iter", "next", "close", "reuse", "last"]
)
def test_late_text_factory_refused_before_callback_or_result_consumption(
    tmp_path, monkeypatch, surface
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            cursor = conn.execute("SELECT 'first' UNION ALL SELECT 'second'")
            expected = "first"
            if surface == "last":
                assert cursor.fetchone()[0] == "first"
                expected = "second"
            calls = []
            conn.text_factory = lambda value: calls.append(value) or value.decode()
            call = {
                "one": cursor.fetchone,
                "many": cursor.fetchmany,
                "all": cursor.fetchall,
                "iter": lambda: iter(cursor),
                "next": lambda: next(cursor),
                "close": cursor.close,
                "reuse": lambda: cursor.execute("SELECT 'replacement'"),
                "last": cursor.fetchone,
            }[surface]
            try:
                with pytest.raises(InstanceAdmissionError, match="configured"):
                    call()
                assert calls == []
            finally:
                conn.text_factory = str
            assert cursor.fetchone()[0] == expected
            cursor.close()
    profile.close()
    config.close()


@pytest.mark.parametrize("surface", ["fetch", "next", "close", "reuse", "drop"])
def test_pending_explicit_returning_cannot_commit_on_drift_or_cursor_gc(
    tmp_path, monkeypatch, surface
):
    import gc
    import weakref

    from finance_core.profile_paths import ProfilePathError

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "WITH p(v) AS (VALUES ('pending'),('pending-second')) "
                "INSERT INTO participants(public_id,display_name) "
                "SELECT v,v FROM p RETURNING public_id"
            )
            reference = weakref.ref(cursor)
            profile.live_database.chmod(0o640)
            try:
                if surface == "drop":
                    del cursor
                    gc.collect()
                    assert reference() is not None
                else:
                    call = {
                        "fetch": cursor.fetchall,
                        "next": lambda: next(cursor),
                        "close": cursor.close,
                        "reuse": lambda: cursor.execute("SELECT 1"),
                    }[surface]
                    with pytest.raises((InstanceAdmissionError, ProfilePathError)):
                        call()
                assert conn.in_transaction
            finally:
                profile.live_database.chmod(0o600)
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


def test_fixed_compiler_policy_and_explicit_transaction_ownership(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            assert conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
            for pragma in (
                "database_list",
                "foreign_keys",
                "busy_timeout",
                "table_info(participants)",
                "table_xinfo(participants)",
                "index_list(participants)",
                "index_info(sqlite_autoindex_participants_1)",
                "index_xinfo(sqlite_autoindex_participants_1)",
                "foreign_key_list(participants)",
                "foreign_key_check",
                "defer_foreign_keys",
            ):
                conn.execute("PRAGMA " + pragma).fetchall()
            for statement in (
                "PRAGMA journal_mode=WAL",
                "PRAGMA incremental_vacuum",
                "PRAGMA writable_schema=ON",
                "PRAGMA foreign_keys=OFF",
                "PRAGMA busy_timeout=1",
                "PRAGMA defer_foreign_keys=ON",
                "CREATE TEMP TABLE forbidden(value)",
                "CREATE VIRTUAL TABLE forbidden USING fts5(value)",
                "ATTACH DATABASE ':memory:' AS forbidden",
                "SELECT random()",
                "SELECT load_extension('x')",
            ):
                with pytest.raises(sqlite3.DatabaseError):
                    conn.execute(statement)
                assert not conn.in_transaction
            with pytest.raises(InstanceAdmissionError, match="set_authorizer"):
                conn.set_authorizer(None)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA defer_foreign_keys=ON")
            assert conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 1
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("PRAGMA defer_foreign_keys=OFF")
            conn.execute("CREATE TABLE protocol_ddl(value TEXT)")
            conn.execute(
                "CREATE TRIGGER protocol_trigger AFTER INSERT ON protocol_ddl BEGIN "
                "INSERT INTO participants(public_id,display_name) VALUES (new.value,new.value); END"
            )
            conn.execute("INSERT INTO protocol_ddl VALUES ('trigger-write')")
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1
            conn.execute("SAVEPOINT inner_owner")
            conn.execute("INSERT INTO protocol_ddl VALUES ('rolled-to-savepoint')")
            conn.execute("ROLLBACK TO inner_owner")
            conn.execute("RELEASE inner_owner")
            assert conn.in_transaction
            conn.rollback()
            assert not conn.in_transaction
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
            assert (
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='protocol_ddl'").fetchone()
                is None
            )
    profile.close()
    config.close()


def test_pending_returning_busy_commit_rollback_restart_and_context_exception(
    tmp_path, monkeypatch
):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "INSERT INTO participants(public_id,display_name) "
                "VALUES ('old','Old') RETURNING public_id"
            )
            with pytest.raises(sqlite3.OperationalError, match="statements in progress"):
                conn.commit()
            assert conn.in_transaction
            conn.rollback()
            assert not conn.in_transaction
            with pytest.raises(ValueError, match="synthetic body"):
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    context_cursor = conn.execute(
                        "INSERT INTO participants(public_id,display_name) "
                        "VALUES ('context','Context') "
                        "RETURNING public_id"
                    )
                    raise ValueError("synthetic body")
            assert not conn.in_transaction
            old.close()
            context_cursor.close()
            conn.execute("BEGIN IMMEDIATE")
            committed = conn.execute(
                "INSERT INTO participants(public_id,display_name) "
                "VALUES ('allowed','Allowed') RETURNING public_id"
            )
            assert committed.fetchall()[0][0] == "allowed"
            conn.commit()
            assert not conn.in_transaction
        with production_read_operation(owner) as conn:
            assert [row[0] for row in conn.execute("SELECT public_id FROM participants")] == [
                "allowed"
            ]
    profile.close()
    config.close()


def test_operation_exception_rolls_back_multiple_pending_cursors(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with pytest.raises(ValueError, match="operation exception"):
            with production_write_operation(owner) as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursors = [
                    conn.execute(
                        "INSERT INTO participants(public_id,display_name) VALUES (?,?) "
                        "RETURNING public_id",
                        (value, value),
                    )
                    for value in ("exception-first", "exception-second")
                ]
                raise ValueError("operation exception")
        for cursor in cursors:
            with pytest.raises(InstanceAdmissionError):
                cursor.fetchone()
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
    profile.close()
    config.close()


def _normal_exit_pending_protocol(tmp_path, fault="none", effect="before"):
    """Keep all handles alive until normal interpreter teardown, including failures."""
    import finance_core.instance_admission.sessions as sessions
    from finance_core.instance_admission import ProductionCloseUncertain

    config = synthetic_config(tmp_path, pytest.MonkeyPatch())
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    owner = acquire_production_owner(config)
    operation = production_write_operation(owner)
    conn = operation.__enter__()
    conn.execute("BEGIN IMMEDIATE")
    cursors = [
        conn.execute(
            "WITH p(v) AS (SELECT ?) INSERT INTO participants(public_id,display_name) "
            "SELECT v,v FROM p RETURNING public_id",
            (value,),
        )
        for value in ("first-pending", "second-pending")
    ]
    assert conn.in_transaction
    if fault != "none":
        name = {
            "rollback": "_rollback_sqlite_handle",
            "cursor": "_close_cursor_handle",
            "connection": "_close_sqlite_handle",
        }[fault]
        native = getattr(sessions, name)

        def uncertain(handle):
            if effect == "after":
                native(handle)
            raise sqlite3.OperationalError("synthetic unknown " + fault)

        setattr(sessions, name, uncertain)
        with pytest.raises(ProductionCloseUncertain):
            operation.__exit__(None, None, None)
        with pytest.raises(InstanceAdmissionError):
            owner.close()
        with pytest.raises(InstanceAdmissionError):
            with production_write_operation(owner):
                pytest.fail("uncertain owner issued another operation")
        with pytest.raises(InstanceAdmissionError):
            acquire_production_owner(config, timeout_seconds=0)
    return profile.live_database, config, profile, owner, operation, conn, cursors


@pytest.mark.parametrize(
    ("fault", "effect", "exit_mode"),
    [
        ("none", "before", "normal"),
        ("none", "before", "sys-exit"),
        ("rollback", "before", "normal"),
        ("rollback", "after", "normal"),
        ("cursor", "before", "normal"),
        ("cursor", "after", "normal"),
        ("connection", "before", "normal"),
        ("connection", "after", "normal"),
    ],
)
def test_pending_writes_and_unknown_cleanup_have_no_facts_after_normal_exit(
    tmp_path, fault, effect, exit_mode
):
    code = (
        "import sys; from pathlib import Path; "
        "from tests.test_instance_admission_v1 import _normal_exit_pending_protocol; "
        "pending = _normal_exit_pending_protocol(Path(sys.argv[1]),sys.argv[2],sys.argv[3]); "
        "print(pending[0], flush=True); " + ("sys.exit(0)" if exit_mode == "sys-exit" else "")
    )
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), fault, effect],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    database = Path(child.stdout.strip())
    with sqlite3.connect(database, timeout=0) as raw:
        assert raw.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
