"""C1 durable publication, cold replay and actual UNKNOWN lifetime proofs."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest
from instance_admission_support_v1 import synthetic_config
from test_instance_admission_lifecycle_v1 import (
    _crash_child_result,
    _receive,
    _start_spawn_process,
)
from test_lifecycle_v2 import Port, ready

from finance_core.instance_admission import (
    PENDING_FILENAME,
    STATE_FILENAME,
    ApplicationSchemaContract,
    InstanceAdmissionBusy,
    InstanceAdmissionError,
    ProductionCloseUncertain,
    TrustedInstanceConfig,
    acquire_production_owner,
    admit_production_instance,
    authority,
    fence_production_instance,
    inspect_instance_admission,
    prepare_production_profile,
    production_read_operation,
    resume_authority_transition,
)
from finance_core.instance_admission.authority import _canonical, _control, _digest
from finance_core.instance_admission.sessions import lifecycle_write_operation
from finance_core.lifecycle_v2 import LedgerService, LifecycleError, RuntimeBinding
from finance_core.lifecycle_v2.contracts import CommitReceipt
from finance_core.lifecycle_v2.single_host_runtime_v1 import (
    admit_single_host_runtime_v1,
)

_ACTION = "single_host_runtime_admit_v1"
_CLOCK = 1792800000
_SCHEMA = ApplicationSchemaContract(
    "runtime-fault-probe-v1", ("CREATE TABLE runtime_fault_probe (value TEXT NOT NULL)",)
)
_STAGES = {
    "journal-create": 81,
    "journal-write": 82,
    "journal-file-fsync": 83,
    "journal-dir-fsync": 84,
    "state-write": 85,
    "state-file-fsync": 86,
    "state-dir-fsync-before-replace": 87,
    "state-replace": 88,
    "state-dir-fsync-after-replace": 89,
    "cleanup": 90,
    "before-return": 91,
}


class _Environment:
    def setenv(self, key, value):
        os.environ[key] = value


def _new_config(root, monkeypatch, *, application_schema=None):
    approvals = set()

    def verify(reference, action, digest):
        if action in {"prepare", "admit"}:
            return reference == "approved"
        return (reference, action, digest) in approvals

    config = synthetic_config(
        root, monkeypatch, verifier=verify, application_schema=application_schema
    )
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    return config, approvals


def _approve(config, approvals, *, action, operation, reference, context=None):
    state = authority._state(config)
    approvals.add(
        (
            reference,
            action,
            _digest(
                {
                    "binding": config._binding,
                    "action": action,
                    "context": {
                        "operation_id": operation,
                        "before_sha256": _digest(state),
                        "context": context,
                    },
                }
            ),
        )
    )


def _approve_runtime(config, approvals, binding):
    _approve(
        config,
        approvals,
        action=_ACTION,
        operation="runtime",
        reference="runtime-approved",
        context={"format": "single-host-runtime-admission-v1", "runtime_binding": asdict(binding)},
    )


def _material(config, approvals, binding, admission=None):
    history = authority._state(config)["history"]
    return {
        "config": config._binding,
        "locator": str(config.locator_root),
        "binding": asdict(binding),
        "admission": None if admission is None else asdict(admission),
        "application_schema": (
            None if config.application_schema is None else asdict(config.application_schema)
        ),
        "approvals": [list(value) for value in approvals]
        + [[entry["approval_ref"], entry["action"], entry["approval_digest"]] for entry in history],
    }


def _rebuild(material):
    """Fresh process composition uses persisted identity and exact approval triples."""
    binding = material["config"]
    os.environ["FINANCE_RUNTIME_ROOT"] = str(Path(binding["profile_root"]) / "runtime")
    schema = material["application_schema"]
    config = TrustedInstanceConfig(
        Path(binding["control_root"]),
        Path(material["locator"]),
        binding["profile_id"],
        layout=binding["layout"],
        instance_id=binding["instance_id"],
        config_identity=binding["config_identity"],
        core_identity=binding["core_identity"],
        application_schema=(
            None
            if schema is None
            else ApplicationSchemaContract(schema["schema_id"], tuple(schema["ddl_statements"]))
        ),
        approval_verifier=lambda ref, action, digest: (
            [ref, action, digest] in material["approvals"]
        ),
    )
    assert config._binding == binding
    runtime = RuntimeBinding(**material["binding"])
    return config, runtime


def _admit(config, binding):
    return admit_single_host_runtime_v1(
        config, binding, operation_id="runtime", approval_ref="runtime-approved"
    )


def _crash_runtime_publication(material, stage, sender):
    """Exit after the real filesystem boundary, without returning an admission."""
    config, binding = _rebuild(material)
    root = config.control_root.stat()
    identity = (root.st_dev, root.st_ino)
    real_open, real_write, real_fsync = os.open, os.write, os.fsync
    real_replace, real_unlink = os.replace, os.unlink
    fd_roles = {}
    pending_synced = candidate_synced = candidate_replaced = False

    def in_control(fd):
        if fd is None:
            return False
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino) == identity

    def stop():
        os._exit(_STAGES[stage])

    def opened(path, flags, mode=0o777, *args, **kwargs):
        fd = real_open(path, flags, mode, *args, **kwargs)
        if in_control(kwargs.get("dir_fd")) and path in {PENDING_FILENAME, "authority.v1.next"}:
            fd_roles[fd] = path
            if path == PENDING_FILENAME and stage == "journal-create":
                stop()
        return fd

    def written(fd, payload):
        size = real_write(fd, payload)
        if (fd_roles.get(fd), stage) in {
            (PENDING_FILENAME, "journal-write"),
            ("authority.v1.next", "state-write"),
        }:
            stop()
        return size

    def synced(fd):
        nonlocal pending_synced, candidate_synced
        real_fsync(fd)
        role = fd_roles.get(fd)
        if role == PENDING_FILENAME:
            pending_synced = True
            if stage == "journal-file-fsync":
                stop()
        elif role == "authority.v1.next":
            candidate_synced = True
            if stage == "state-file-fsync":
                stop()
        elif in_control(fd):
            if pending_synced and stage == "journal-dir-fsync":
                stop()
            if candidate_synced and not candidate_replaced:
                if stage == "state-dir-fsync-before-replace":
                    stop()
            if candidate_replaced and stage == "state-dir-fsync-after-replace":
                stop()

    def replaced(src, dst, *args, **kwargs):
        nonlocal candidate_replaced
        result = real_replace(src, dst, *args, **kwargs)
        if src == "authority.v1.next" and dst == STATE_FILENAME:
            candidate_replaced = True
            if stage == "state-replace":
                stop()
        return result

    def unlinked(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == PENDING_FILENAME and in_control(kwargs.get("dir_fd")) and stage == "cleanup":
            stop()
        return result

    real_publish = authority._publish

    def published(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        if stage == "before-return":
            stop()
        return result

    with (
        patch.object(os, "open", opened),
        patch.object(os, "write", written),
        patch.object(os, "fsync", synced),
        patch.object(os, "replace", replaced),
        patch.object(os, "unlink", unlinked),
        patch.object(authority, "_publish", published),
    ):
        _admit(config, binding)
    sender.send(("unexpected-return", stage))


@pytest.mark.parametrize("stage", _STAGES)
def test_runtime_publication_crash_resumes_exactly_once(tmp_path, monkeypatch, stage):
    config, approvals = _new_config(tmp_path, monkeypatch)
    try:
        binding = RuntimeBinding(
            config.instance_id, "gen1", "user", "chat", "single-host-synthetic"
        )
        _approve_runtime(config, approvals, binding)
        material = _material(config, approvals, binding)
        state_path, pending = (
            config.control_root / STATE_FILENAME,
            config.control_root / PENDING_FILENAME,
        )
        before = state_path.read_bytes()
        process, receiver = _start_spawn_process(_crash_runtime_publication, (material, stage))
        _crash_child_result(process, receiver, _STAGES[stage])
        crashed = state_path.read_bytes()
        if stage == "journal-create":
            assert crashed == before and pending.read_bytes() == b""
            for action in (
                lambda: inspect_instance_admission(config),
                lambda: acquire_production_owner(config, timeout_seconds=0),
                lambda: resume_authority_transition(
                    config, operation_id="runtime", approval_ref="runtime-approved"
                ),
            ):
                with pytest.raises(InstanceAdmissionError):
                    action()
            assert state_path.read_bytes() == before and pending.read_bytes() == b""
            return
        pending_expected = stage not in {"cleanup", "before-return"}
        assert pending.exists() is pending_expected
        replaced = stage in {
            "state-replace",
            "state-dir-fsync-after-replace",
            "cleanup",
            "before-return",
        }
        assert (crashed != before) is replaced
        if pending_expected:
            for action in (
                lambda: inspect_instance_admission(config),
                lambda: acquire_production_owner(config, timeout_seconds=0),
                lambda: _admit(config, binding),
            ):
                with pytest.raises(InstanceAdmissionError):
                    action()
        pending_bytes = pending.read_bytes() if pending.exists() else None
        for operation, reference in (("wrong-runtime", "runtime-approved"), ("runtime", "wrong")):
            with pytest.raises(InstanceAdmissionError):
                resume_authority_transition(config, operation_id=operation, approval_ref=reference)
            assert state_path.read_bytes() == crashed
            assert (pending.read_bytes() if pending.exists() else None) == pending_bytes
        resumed = resume_authority_transition(
            config, operation_id="runtime", approval_ref="runtime-approved"
        )
        assert resumed["state"] == "ACTIVE" and not pending.exists()
        admission = _admit(config, binding)
        durable = state_path.read_bytes()
        state = json.loads(durable)
        assert state["version"] == json.loads(before)["version"] + 1
        assert sum(entry["action"] == _ACTION for entry in state["history"]) == 1
        assert sum(entry["operation_id"] == "runtime" for entry in state["history"]) == 1
        assert _admit(config, binding) == admission
        assert (
            resume_authority_transition(
                config, operation_id="runtime", approval_ref="runtime-approved"
            )
            == resumed
        )
        assert state_path.read_bytes() == durable
        with acquire_production_owner(config) as owner:
            with lifecycle_write_operation(owner, runtime_admission=admission) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.commit()
        if stage == "before-return":
            _approve(
                config, approvals, action="fence", operation="fence", reference="fence-approved"
            )
            fence_production_instance(config, operation_id="fence", approval_ref="fence-approved")
            assert _admit(config, binding) == admission
            with acquire_production_owner(config) as owner:
                with pytest.raises(InstanceAdmissionError):
                    with lifecycle_write_operation(owner, runtime_admission=admission):
                        pytest.fail("Exact lost-return retry revived a fenced writer")
    finally:
        config.close()


@pytest.mark.parametrize("damage", ["context", "suffix", "candidate"])
def test_pending_runtime_resume_preserves_tampered_evidence(tmp_path, monkeypatch, damage):
    config, approvals = _new_config(tmp_path, monkeypatch)
    try:
        binding = RuntimeBinding(
            config.instance_id, "gen1", "user", "chat", "single-host-synthetic"
        )
        _approve_runtime(config, approvals, binding)
        material = _material(config, approvals, binding)
        before = (config.control_root / STATE_FILENAME).read_bytes()
        process, receiver = _start_spawn_process(
            _crash_runtime_publication, (material, "journal-file-fsync")
        )
        _crash_child_result(process, receiver, _STAGES["journal-file-fsync"])
        pending = config.control_root / PENDING_FILENAME
        journal = json.loads(pending.read_bytes())
        candidate = config.control_root / "authority.v1.next"
        if damage == "context":
            journal["context"]["runtime_binding"]["generation"] = "another-generation"
            pending.write_bytes(_canonical(journal))
        elif damage == "suffix":
            suffix = journal["after"]["history"][-1]
            suffix["runtime_context"]["runtime_binding"]["generation"] = "another-generation"
            suffix["context_digest"] = _digest(suffix["runtime_context"])
            suffix["entry_sha256"] = _digest(
                {key: value for key, value in suffix.items() if key != "entry_sha256"}
            )
            pending.write_bytes(_canonical(journal))
        else:
            candidate.write_bytes(b"{}")
            candidate.chmod(0o600)
        pending_bytes = pending.read_bytes()
        candidate_bytes = candidate.read_bytes() if candidate.exists() else None
        with pytest.raises(InstanceAdmissionError):
            resume_authority_transition(
                config, operation_id="runtime", approval_ref="runtime-approved"
            )
        assert pending.read_bytes() == pending_bytes
        assert (candidate.read_bytes() if candidate.exists() else None) == candidate_bytes
        assert (config.control_root / STATE_FILENAME).read_bytes() == before
    finally:
        config.close()


def _commit_then_die(material, sender):
    import finance_core.instance_admission.sessions as sessions

    config, binding = _rebuild(material)
    admission = _admit(config, binding)
    assert asdict(admission) == material["admission"]
    owner = acquire_production_owner(config)
    service = LedgerService(
        owner, Port(), binding, clock=lambda: _CLOCK, runtime_admission=admission
    )
    original_commit = sessions._ProductionConnection.commit

    def commit_and_exit(conn):
        original_commit(conn)
        # Actual durable financial commit completed, but confirm has not returned
        # and Python never gets to run operation/owner cleanup.
        os._exit(74)

    with patch.object(sessions._ProductionConnection, "commit", commit_and_exit):
        service.confirm(material["review_id"], "action1", "confirm1")
    sender.send(("unexpected-return", None))


def _resolve_committed_in_fresh_process(material, sender):
    config, binding = _rebuild(material)
    durable = (config.control_root / STATE_FILENAME).read_bytes()
    admission = _admit(config, binding)
    assert asdict(admission) == material["admission"]
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: _CLOCK + 601, runtime_admission=admission
        )
        with production_read_operation(owner) as conn:
            fact = dict(conn.execute("SELECT * FROM l2_facts").fetchone())
            before = tuple(
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("l2_facts", "l2_decisions", "l2_events")
            )
            assert (
                conn.execute("SELECT count(*) FROM l2_events WHERE kind='confirm'").fetchone()[0]
                == 1
            )
        original = CommitReceipt(
            fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
        )
        assert service.confirm(material["review_id"], "action1", "confirm1") == original
        assert service.confirm(material["review_id"], "action1", "confirm1") == original
        with pytest.raises(LifecycleError):
            service.confirm(material["review_id"], "wrong-action", "confirm1")
        with pytest.raises(LifecycleError):
            service.confirm(material["review_id"], "action1", "different-operation")
        with production_read_operation(owner) as conn:
            after = tuple(
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("l2_facts", "l2_decisions", "l2_events")
            )
        assert after == before and before[:2] == (1, 1)
        assert service.reader().full_audit()["fact_version_count"] == 1
        assert service.reader().get(original.record_id)["values"]["amount"] == "12.00"
    assert (config.control_root / STATE_FILENAME).read_bytes() == durable
    config.close()
    sender.send(("resolved-exactly-once", before, after, asdict(original), os.getpid()))


@pytest.mark.parametrize("environment", ["single-host-synthetic", "single-host-real"])
def test_commit_reply_loss_resolves_in_independent_process(tmp_path, monkeypatch, environment):
    config, approvals = _new_config(tmp_path, monkeypatch)
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", environment)
    _approve_runtime(config, approvals, binding)
    admission = _admit(config, binding)
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: _CLOCK, runtime_admission=admission
        )
        _, _, review = ready(service)
    material = _material(config, approvals, binding, admission)
    material["review_id"] = review.review_id
    # Serializing and decoding the transport material excludes inherited DTOs.
    material = json.loads(json.dumps(material))
    config.close()
    process, receiver = _start_spawn_process(_commit_then_die, (material,))
    _crash_child_result(process, receiver, 74)
    restarted, receiver = _start_spawn_process(_resolve_committed_in_fresh_process, (material,))
    result = _receive(receiver, restarted, timeout=30)
    assert restarted.exitcode == 0
    assert result[0] == "resolved-exactly-once" and result[1] == result[2]
    assert result[1][:2] == (1, 1) and result[4] != os.getpid()


def _unknown_close_worker(root, fault, channel):
    import finance_core.instance_admission.sessions as sessions

    config, approvals = _new_config(Path(root), _Environment(), application_schema=_SCHEMA)
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic")
    _approve_runtime(config, approvals, binding)
    admission = _admit(config, binding)
    material = _material(config, approvals, binding, admission)
    owner = acquire_production_owner(config)
    operation = lifecycle_write_operation(owner, runtime_admission=admission)
    conn = operation.__enter__()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO runtime_fault_probe VALUES ('uncommitted')")
    assert conn.in_transaction and conn.total_changes == 1

    def fail(_conn):
        raise sqlite3.OperationalError(f"synthetic actual {fault} UNKNOWN")

    boundary = "_close_sqlite_handle" if fault == "close" else "_rollback_sqlite_handle"
    with patch.object(sessions, boundary, fail):
        try:
            operation.__exit__(None, None, None)
        except ProductionCloseUncertain as error:
            assert error.owner is owner and error.connection is conn
        else:
            raise AssertionError("UNKNOWN close returned success")
    witness = owner._active
    assert witness is not None and owner._uncertain and witness.closing and not witness.closed
    assert witness.connection is conn and witness in sessions._UNCERTAIN
    assert conn.in_transaction is (fault == "rollback")
    for fd in (owner._lease.fd, witness.control.fd, witness.gate.fileno()):
        os.fstat(fd)
    witness.profile.revalidate()
    for action in (
        owner.close,
        lambda: conn.execute("SELECT 1"),
        lambda: lifecycle_write_operation(owner, runtime_admission=admission).__enter__(),
        lambda: acquire_production_owner(config, timeout_seconds=0),
    ):
        with pytest.raises(InstanceAdmissionError):
            action()
    channel.send(("retained-actual-owner-and-handles", material, fault, os.getpid()))
    os._exit(0 if channel.recv() == "release" else 3)


def _probe_retained_leases(material, sender):
    config, _ = _rebuild(material)
    with pytest.raises(InstanceAdmissionBusy):
        acquire_production_owner(config, timeout_seconds=0)
    with pytest.raises(InstanceAdmissionBusy):
        with _control(config, timeout_seconds=0):
            raise AssertionError("UNKNOWN operation released shared control lease")
    config.close()
    sender.send(("independent-owner-and-control-busy", os.getpid()))


@pytest.mark.parametrize("fault", ["close", "rollback"])
def test_actual_unknown_retains_owner_handles_and_leases_until_process_exit(tmp_path, fault):
    context = mp.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=_unknown_close_worker, args=(tmp_path, fault, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(30), f"UNKNOWN worker produced no result (exit={worker.exitcode})"
        retained = parent.recv()
        assert retained[0] == "retained-actual-owner-and-handles" and retained[2] == fault
        assert worker.is_alive()
        material = retained[1]
        probe, receiver = _start_spawn_process(_probe_retained_leases, (material,))
        result = _receive(receiver, probe, timeout=30)
        assert probe.exitcode == 0 and result[0] == "independent-owner-and-control-busy"
        assert worker.is_alive() and result[1] != retained[3]
        parent.send("release")
        worker.join(30)
        assert not worker.is_alive() and worker.exitcode == 0
        with pytest.MonkeyPatch.context() as runtime_environment:
            runtime_environment.setenv(
                "FINANCE_RUNTIME_ROOT", str(Path(material["config"]["profile_root"]) / "runtime")
            )
            config, binding = _rebuild(material)
            try:
                admission = _admit(config, binding)
                assert asdict(admission) == material["admission"]
                with acquire_production_owner(config) as owner:
                    with production_read_operation(owner) as conn:
                        assert (
                            conn.execute("SELECT count(*) FROM runtime_fault_probe").fetchone()[0]
                            == 0
                        )
            finally:
                config.close()
    finally:
        parent.close()
        if worker.is_alive():
            worker.terminate()
        worker.join(10)
        if worker.is_alive():
            worker.kill()
            worker.join(10)
        assert not worker.is_alive(), "UNKNOWN worker remained live after test cleanup"
