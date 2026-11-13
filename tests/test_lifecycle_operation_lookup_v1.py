"""Exact original receipts through real protected read-only lifecycle snapshots."""

import json
import multiprocessing as mp
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from instance_admission_support_v1 import synthetic_config
from test_instance_admission_lifecycle_v1 import _receive, _start_spawn_process
from test_lifecycle_v2 import Port, _adversarial_database, _correct, ready
from test_single_host_runtime_v1 import _admit

from finance_core.instance_admission import (
    InstanceAdmissionError,
    ProductionCloseUncertain,
    acquire_production_owner,
    admit_production_instance,
    prepare_production_profile,
)
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import (
    LedgerService,
    LifecycleError,
    RuntimeBinding,
    VerifiedObservation,
    VerifiedSource,
)


@pytest.fixture(params=["offline-synthetic", "single-host-synthetic", "single-host-real"])
def lookup_case(tmp_path, monkeypatch, request):
    approvals = set()

    def verify(reference, action, value):
        if action in {"prepare", "admit"}:
            return reference == "approved"
        return (reference, action, value) in approvals

    config = synthetic_config(tmp_path, monkeypatch, verifier=verify)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    admission = None
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat")
    if request.param != "offline-synthetic":
        binding, admission = _admit(config, approvals, request.param)
    owner = acquire_production_owner(config)
    service = LedgerService(
        owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
    )
    try:
        yield SimpleNamespace(service=service, config=config, owner=owner, approvals=approvals)
    finally:
        if owner._lease.fd >= 0:
            owner.close()
        config.close()


def _rows(owner):
    with lifecycle_read_operation(owner) as conn:
        return {
            table: tuple(tuple(row) for row in conn.execute("SELECT * FROM l2_" + table))
            for table in (
                "sources",
                "proposals",
                "reviews",
                "deliveries",
                "facts",
                "decisions",
                "events",
            )
        }


def test_lookup_absent_receipt_is_read_only(lookup_case):
    service = lookup_case.service
    before = _rows(lookup_case.owner)
    assert (
        service.lookup_commit_receipt("never-started", review_id="", action_evidence_id="") is None
    )
    assert _rows(lookup_case.owner) == before


def test_lookup_unconfirmed_review_never_creates_a_fact(lookup_case):
    service = lookup_case.service
    _, _, review = ready(service)
    before = _rows(lookup_case.owner)
    for _ in range(2):
        assert (
            service.lookup_commit_receipt(
                "confirm-not-started", review_id=review.review_id, action_evidence_id="action1"
            )
            is None
        )
    assert _rows(lookup_case.owner) == before
    assert not before["facts"] and not before["decisions"]


def test_lookup_original_receipt_survives_correction_expiry_and_owner_restart(lookup_case):
    case = lookup_case
    _, _, review = ready(case.service)
    original = case.service.confirm(review.review_id, "action1", "confirm1")
    _, corrected = _correct(case.service, case.service.port, original)
    before = _rows(case.owner)
    case.owner.close()
    with acquire_production_owner(case.config) as owner:
        reopened = LedgerService(
            owner,
            case.service.port,
            case.service.binding,
            clock=lambda: 1792800601,
            runtime_admission=case.service.runtime_admission,
        )
        assert (
            reopened.lookup_commit_receipt(
                "confirm1", review_id=review.review_id, action_evidence_id="action1"
            )
            == original
        )
        assert reopened.reader().get(original.record_id)["version"] == corrected.version == 2
        assert _rows(owner) == before


def test_lookup_refuses_wrong_triple_and_other_event_kinds(lookup_case):
    service = lookup_case.service
    _, _, review = ready(service)
    original = service.confirm(review.review_id, "action1", "confirm1")
    before = _rows(lookup_case.owner)
    for review_id, action in ((review.review_id + "other", "action1"), (review.review_id, "other")):
        with pytest.raises(LifecycleError, match="replay mismatch"):
            service.lookup_commit_receipt(
                "confirm1", review_id=review_id, action_evidence_id=action
            )
    for operation in ("capture1", "propose1"):
        with pytest.raises(LifecycleError, match="event kind"):
            service.lookup_commit_receipt(
                operation, review_id=review.review_id, action_evidence_id="action1"
            )
    assert (
        service.lookup_commit_receipt(
            "different-operation", review_id=review.review_id, action_evidence_id="action1"
        )
        is None
    )
    assert (
        service.lookup_commit_receipt(
            "confirm1", review_id=review.review_id, action_evidence_id="action1"
        )
        == original
    )
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize("lookup_case", ["single-host-synthetic"], indirect=True)
@pytest.mark.parametrize(
    ("operation", "review_identity", "action"),
    [
        ("empty-action", "", ""),
        (" operation whitespace ", " review whitespace ", " action whitespace "),
        ("确认-é", "评价-é", "动作-e\u0301"),
        ("x" * 256, "界" * 248, "人" * 1000),
        ("界" * 85 + "a", None, "action-at-byte-boundary"),
    ],
)
def test_lookup_preserves_actual_accepted_identifier_strings(
    lookup_case, monkeypatch, operation, review_identity, action
):
    import finance_core.lifecycle_v2.service as module

    service = lookup_case.service
    original_id = module._id
    if review_identity is not None:
        # Synthetic historical identity fixture: vary only the generated review
        # ID, then require the unchanged public prepare/confirm flow to accept
        # it before claiming lookup compatibility for the resulting real rows.
        monkeypatch.setattr(
            module,
            "_id",
            lambda prefix: review_identity if prefix == "review" else original_id(prefix),
        )
    _, _, review = ready(service)
    receipt = service.confirm(review.review_id, action, operation)
    before = _rows(lookup_case.owner)
    assert (
        service.lookup_commit_receipt(
            operation, review_id=review.review_id, action_evidence_id=action
        )
        == receipt
    )
    with pytest.raises(LifecycleError, match="replay mismatch"):
        service.lookup_commit_receipt(
            operation, review_id=review.review_id, action_evidence_id=action + " "
        )
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize("lookup_case", ["offline-synthetic"], indirect=True)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", None),
        ("operation_id", ""),
        ("operation_id", b"operation"),
        ("operation_id", "a" * 257),
        ("operation_id", "界" * 86),
        ("operation_id", "\ud800"),
        ("review_id", None),
        ("action_evidence_id", 1),
    ],
)
def test_lookup_invalid_identifier_domain_refuses_before_effects(lookup_case, field, value):
    arguments = {"operation_id": "absent", "review_id": "", "action_evidence_id": ""}
    arguments[field] = value
    before = _rows(lookup_case.owner)
    with pytest.raises(LifecycleError):
        lookup_case.service.lookup_commit_receipt(**arguments)
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize(
    "lookup_case", ["single-host-synthetic", "single-host-real"], indirect=True
)
def test_lookup_requires_current_exact_runtime_scope(lookup_case):
    service = lookup_case.service
    _, _, review = ready(service)
    service.confirm(review.review_id, "action1", "confirm1")
    before = _rows(lookup_case.owner)
    original_binding, original_admission = service.binding, service.runtime_admission
    for binding, admission in (
        (original_binding, None),
        (replace(original_binding, environment="offline-synthetic"), original_admission),
        (original_binding, replace(original_admission, registration_entry_sha256="0" * 64)),
    ):
        service.binding, service.runtime_admission = binding, admission
        with pytest.raises(InstanceAdmissionError):
            service.lookup_commit_receipt(
                "confirm1", review_id=review.review_id, action_evidence_id="action1"
            )
    service.binding, service.runtime_admission = original_binding, original_admission
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize("lookup_case", ["single-host-synthetic"], indirect=True)
@pytest.mark.parametrize("field", ["owner", "binding", "runtime_admission"])
def test_lookup_callback_keeps_runtime_identity_and_allows_exact_retry(lookup_case, field):
    service = lookup_case.service
    _, _, review = ready(service)
    original = service.confirm(review.review_id, "action1", "confirm1")
    before = _rows(lookup_case.owner)
    original_source = service.port.source
    changes = {
        "owner": None,
        "binding": replace(service.binding, generation="drift"),
        "runtime_admission": None,
    }

    def mutate(conn, evidence_id):
        evidence = original_source(conn, evidence_id)
        setattr(service, field, changes[field])
        return evidence

    service.port.source = mutate
    try:
        with pytest.raises(LifecycleError, match="Runtime identity"):
            service.lookup_commit_receipt(
                "confirm1", review_id=review.review_id, action_evidence_id="action1"
            )
    finally:
        service.port.source = original_source
    assert (
        service.lookup_commit_receipt(
            "confirm1", review_id=review.review_id, action_evidence_id="action1"
        )
        == original
    )
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize("lookup_case", ["offline-synthetic"], indirect=True)
def test_lookup_uses_actual_readonly_uri_and_one_audited_snapshot(lookup_case, monkeypatch):
    from finance_core.instance_admission import sessions
    from finance_core.lifecycle_v2 import _finalization_plan_v1, reader

    service = lookup_case.service
    _, _, review = ready(service)
    receipt = service.confirm(review.review_id, "action1", "confirm1")
    before = _rows(lookup_case.owner)
    connect, audit, confirmed = (
        sqlite3.connect,
        reader.audit_evidence_connection,
        _finalization_plan_v1._confirmed_receipt,
    )
    source = service.port.source
    uris, points, source_calls = [], [], []

    def actual_connect(*args, **kwargs):
        if kwargs.get("factory") is sessions._ProductionConnection:
            uris.append((args[0], kwargs.get("uri")))
        return connect(*args, **kwargs)

    def audited(conn, *args):
        assert conn.in_transaction and sessions._witness_for(conn).read_only
        points.append(("audit", conn))
        return audit(conn, *args)

    def original_receipt(actual_service, conn, *args):
        assert actual_service is service and conn is points[0][1] and conn.in_transaction
        points.append(("receipt", conn))
        return confirmed(actual_service, conn, *args)

    def readonly_source(conn, evidence_id):
        source_calls.append(conn)
        assert conn.in_transaction and conn is points[0][1]
        with pytest.raises((InstanceAdmissionError, sqlite3.DatabaseError)):
            conn.execute(
                "INSERT INTO participants(public_id,display_name) VALUES ('lookup','forbidden')"
            )
        return source(conn, evidence_id)

    with monkeypatch.context() as observe:
        observe.setattr(sqlite3, "connect", actual_connect)
        observe.setattr(reader, "audit_evidence_connection", audited)
        observe.setattr(_finalization_plan_v1, "_confirmed_receipt", original_receipt)
        observe.setattr(service.port, "source", readonly_source)
        assert (
            service.lookup_commit_receipt(
                "confirm1", review_id=review.review_id, action_evidence_id="action1"
            )
            == receipt
        )
    assert len(uris) == 1 and uris[0][0].endswith("?mode=ro") and uris[0][1] is True
    assert [kind for kind, _ in points] == ["audit", "receipt"] and source_calls
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        sqlite3.Connection.execute(points[0][1], "SELECT 1")
    assert lookup_case.owner._active is None and not lookup_case.owner._opening
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize("lookup_case", ["offline-synthetic"], indirect=True)
@pytest.mark.parametrize("damage", ["source-row", "decision-row", "unrelated-capture"])
def test_lookup_full_audit_refuses_tamper_even_for_absence(lookup_case, damage):
    service = lookup_case.service
    _, _, review = ready(service)
    service.confirm(review.review_id, "action1", "confirm1")
    if damage == "unrelated-capture":
        service.port.sources["extra"] = replace(
            service.port.sources["source1"], evidence_id="extra", source_key="extra-message"
        )
        service.capture("extra", "capture-extra")
    with _adversarial_database(service) as conn:
        if damage == "source-row":
            conn.execute("UPDATE l2_sources SET raw_input='tampered'")
        elif damage == "decision-row":
            conn.execute("DELETE FROM l2_decisions")
        else:
            conn.execute("DELETE FROM l2_events WHERE operation_id='capture-extra'")
    before = _rows(lookup_case.owner)
    for operation in ("confirm1", "fully-absent"):
        with pytest.raises(LifecycleError):
            service.lookup_commit_receipt(
                operation, review_id=review.review_id, action_evidence_id="action1"
            )
    assert _rows(lookup_case.owner) == before


@pytest.mark.parametrize("lookup_case", ["offline-synthetic"], indirect=True)
def test_lookup_equal_string_subclasses_do_not_replace_exact_identifiers(lookup_case):
    class Text(str):
        pass

    values = {"operation_id": "absent", "review_id": "", "action_evidence_id": ""}
    for field in values:
        arguments = dict(values, **{field: Text(values[field])})
        with pytest.raises(LifecycleError):
            lookup_case.service.lookup_commit_receipt(**arguments)
    assert not _rows(lookup_case.owner)["facts"]


def _lookup_restarted_child(material, sender):
    from test_single_host_runtime_faults_v1 import _admit as readmission
    from test_single_host_runtime_faults_v1 import _rebuild

    config, binding = _rebuild(material)
    admission = None if binding.environment == "offline-synthetic" else readmission(config, binding)
    port = Port()
    port.sources = {key: VerifiedSource(**value) for key, value in material["port_sources"].items()}
    port.observations = {
        key: VerifiedObservation(**value) for key, value in material["port_observations"].items()
    }
    port.raw = material["port_raw"]
    try:
        with acquire_production_owner(config) as owner:
            service = LedgerService(
                owner, port, binding, clock=lambda: 1792800601, runtime_admission=admission
            )
            before = _rows(owner)
            receipt = service.lookup_commit_receipt(
                "confirm1", review_id=material["review_id"], action_evidence_id="action1"
            )
            assert (
                service.lookup_commit_receipt(
                    "confirm1", review_id=material["review_id"], action_evidence_id="action1"
                )
                == receipt
            )
            assert service.reader().get(receipt.record_id)["version"] == 2
            assert _rows(owner) == before
            sender.send((asdict(receipt), os.getpid(), len(before["facts"])))
    finally:
        config.close()


@pytest.mark.parametrize(
    "lookup_case", ["offline-synthetic", "single-host-synthetic"], indirect=True
)
def test_lookup_reconstructs_real_owner_and_original_receipt_in_independent_process(lookup_case):
    from test_single_host_runtime_faults_v1 import _material

    case = lookup_case
    _, _, review = ready(case.service)
    original = case.service.confirm(review.review_id, "action1", "confirm1")
    _correct(case.service, case.service.port, original)
    material = _material(
        case.config, case.approvals, case.service.binding, case.service.runtime_admission
    )
    material.update(
        review_id=review.review_id,
        port_sources={key: asdict(value) for key, value in case.service.port.sources.items()},
        port_observations={
            key: asdict(value) for key, value in case.service.port.observations.items()
        },
        port_raw=case.service.port.raw,
    )
    # Cross the process boundary with serialized primitive data, never parent
    # owners, connections, service instances or an inherited admission object.
    material = json.loads(json.dumps(material))
    case.owner.close()
    child, receiver = _start_spawn_process(_lookup_restarted_child, (material,))
    result = _receive(receiver, child, timeout=30)
    assert child.exitcode == 0 and result == (asdict(original), result[1], 2)
    assert result[1] != os.getpid()


def _lookup_fault_child(root, fault, channel):
    from test_single_host_runtime_faults_v1 import (
        _admit as readmission,
    )
    from test_single_host_runtime_faults_v1 import (
        _approve_runtime,
        _Environment,
        _material,
        _new_config,
    )

    from finance_core.instance_admission import sessions

    config, approvals = _new_config(Path(root), _Environment())
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic")
    _approve_runtime(config, approvals, binding)
    admission = readmission(config, binding)
    owner = acquire_production_owner(config)
    service = LedgerService(
        owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
    )
    _, _, review = ready(service)
    receipt = service.confirm(review.review_id, "action1", "confirm1")
    material = _material(config, approvals, binding, admission)
    material.update(review_id=review.review_id, original_receipt=asdict(receipt))
    close = sessions._close_sqlite_handle

    def fail(_conn):
        raise sqlite3.OperationalError("actual lookup " + fault)

    def close_then_fail(conn):
        close(conn)
        fail(conn)

    with pytest.MonkeyPatch.context() as faulting:
        if fault == "rollback":
            faulting.setattr(sessions._ProductionConnection, "rollback", fail)
            faulting.setattr(sessions, "_rollback_sqlite_handle", fail)
        else:
            faulting.setattr(
                sessions,
                "_close_sqlite_handle",
                close_then_fail if fault == "close-after" else fail,
            )
        try:
            service.lookup_commit_receipt(
                "confirm1", review_id=review.review_id, action_evidence_id="action1"
            )
        except ProductionCloseUncertain as error:
            assert error.owner is owner and error.connection is owner._active.connection
        else:
            raise AssertionError("A receipt escaped an unproved rollback/actual close")
    witness = owner._active
    assert owner._uncertain and witness is not None and witness in sessions._UNCERTAIN
    assert witness.read_only and witness.closing and not witness.closed
    for descriptor in (owner._lease.fd, witness.control.fd, witness.gate.fileno()):
        os.fstat(descriptor)
    witness.profile.revalidate()
    for operation in (owner.close, lambda: acquire_production_owner(config, timeout_seconds=0)):
        with pytest.raises(InstanceAdmissionError):
            operation()
    channel.send((material, os.getpid()))
    os._exit(0 if channel.recv() == "release" else 3)


@pytest.mark.parametrize("fault", ["rollback", "close-before", "close-after"])
def test_lookup_actual_rollback_or_close_unknown_retains_owner_and_leases(tmp_path, fault):
    from test_single_host_runtime_faults_v1 import (
        _admit as readmission,
    )
    from test_single_host_runtime_faults_v1 import (
        _probe_retained_leases,
        _rebuild,
    )

    context = mp.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=_lookup_fault_child, args=(tmp_path, fault, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(30), f"Lookup fault worker failed (exit={worker.exitcode})"
        material, worker_pid = parent.recv()
        assert worker.is_alive()
        probe, receiver = _start_spawn_process(_probe_retained_leases, (material,))
        retained = _receive(receiver, probe, timeout=30)
        assert probe.exitcode == 0 and retained[0] == "independent-owner-and-control-busy"
        assert worker.is_alive() and retained[1] != worker_pid
        parent.send("release")
        worker.join(30)
        assert worker.exitcode == 0 and not worker.is_alive()
        with pytest.MonkeyPatch.context() as runtime_environment:
            runtime_environment.setenv(
                "FINANCE_RUNTIME_ROOT", str(Path(material["config"]["profile_root"]) / "runtime")
            )
            config, binding = _rebuild(material)
            try:
                admission = readmission(config, binding)
                with acquire_production_owner(config) as owner:
                    service = LedgerService(
                        owner,
                        Port(),
                        binding,
                        clock=lambda: 1792800601,
                        runtime_admission=admission,
                    )
                    assert (
                        asdict(
                            service.lookup_commit_receipt(
                                "confirm1",
                                review_id=material["review_id"],
                                action_evidence_id="action1",
                            )
                        )
                        == material["original_receipt"]
                    )
                    assert len(_rows(owner)["facts"]) == 1
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
        assert not worker.is_alive()
