import sqlite3

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
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 60
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
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM schema_migrations")


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


@pytest.mark.parametrize("commit_mode", ["explicit", "sqlite-context"])
@pytest.mark.parametrize("drift", ["permissions", "main-path"])
def test_main_identity_drift_rejects_actual_commit(tmp_path, monkeypatch, commit_mode, drift):
    from finance_core.profile_paths import ProfilePathError

    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            # Synthetic uncommitted schema effect will be rolled back by operation cleanup.
            conn.execute("BEGIN IMMEDIATE")
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
                    else:
                        conn.__exit__(None, None, None)
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
