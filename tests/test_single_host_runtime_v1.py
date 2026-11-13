"""Single-host scope uses real protected authority and synthetic ledger inputs."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port, ready, response

from finance_core.instance_admission import (
    InstanceAdmissionError,
    acquire_production_owner,
    admit_production_instance,
    prepare_production_profile,
)
from finance_core.instance_admission.authority import STATE_FILENAME, _digest
from finance_core.instance_admission.sessions import (
    lifecycle_read_operation,
    lifecycle_write_operation,
    production_write_operation,
)
from finance_core.lifecycle_v2 import LedgerService, LifecycleError, RuntimeBinding, VerifiedSource
from finance_core.lifecycle_v2.contracts import canonical, digest


@pytest.fixture
def runtime_config(tmp_path, monkeypatch):
    approvals = set()

    def verify(reference, action, digest):
        if action in {"prepare", "admit"}:
            return reference == "approved"
        return (reference, action, digest) in approvals

    config = synthetic_config(tmp_path, monkeypatch, verifier=verify)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    yield config, approvals
    config.close()


def approve_runtime(config, approvals, binding, operation="runtime", reference="runtime-approved"):
    state = json.loads((config.control_root / STATE_FILENAME).read_bytes())
    context = {
        "format": "single-host-runtime-admission-v1",
        "runtime_binding": asdict(binding),
    }
    approval_digest = _digest(
        {
            "binding": config._binding,
            "action": "single_host_runtime_admit_v1",
            "context": {
                "operation_id": operation,
                "before_sha256": _digest(state),
                "context": context,
            },
        }
    )
    approvals.add((reference, "single_host_runtime_admit_v1", approval_digest))


def _admit(config, approvals, environment="single-host-synthetic", *, operation="runtime"):
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", environment)
    reference = operation + "-approved"
    approve_runtime(config, approvals, binding, operation=operation, reference=reference)
    admission = admit_single_host_runtime_v1(
        config, binding, operation_id=operation, approval_ref=reference
    )
    return binding, admission


def approve_authority_change(config, approvals, action, operation, reference="approved"):
    context = None
    if action == "maintenance":
        from finance_core.instance_admission.authority import (
            _control,
            _record,
            _state,
            _verify_record_database,
        )

        with _control(config) as control:
            state = _state(config)
            record = _record(config, state)
            admission_version = next(
                entry["version"]
                for entry in state["history"]
                if entry["operation_id"] == record["admission"]
            )
            baseline_context = {
                "instance_id": config.instance_id,
                "admission": record["admission"],
                "admission_version": admission_version,
                "authority_version": state["version"],
            }
            baseline = _verify_record_database(
                config, record, control, baseline_context=baseline_context
            )
            context = {
                "hold": {
                    "operation_id": operation,
                    "admission": record["admission"],
                    "baseline": baseline,
                }
            }
    state = json.loads((config.control_root / STATE_FILENAME).read_bytes())
    approval_digest = _digest(
        {
            "binding": config._binding,
            "action": action,
            "context": {
                "operation_id": operation,
                "before_sha256": _digest(state),
                "context": context,
            },
        }
    )
    approvals.add((reference, action, approval_digest))


def _ledger_rows(service):
    names = ("sources", "proposals", "reviews", "deliveries", "facts", "decisions", "events")
    with lifecycle_read_operation(service.owner) as conn:
        return {
            name: tuple(tuple(row) for row in conn.execute("SELECT * FROM l2_" + name))
            for name in names
        }


def _runtime_service(config, binding, admission, port=None):
    owner = acquire_production_owner(config)
    return owner, LedgerService(
        owner, port or Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
    )


def _prepared_test_service(tmp_path, monkeypatch, *, instance_id="prepared-runtime-tests"):
    from lifecycle_recovery_material_support_v1 import (
        RecordingEvidencePortV1,
        SyntheticAuthenticatorV1,
    )

    approvals = set()

    def verify(reference, action, approval_digest):
        if action in {"prepare", "admit"}:
            return reference == "approved"
        return (reference, action, approval_digest) in approvals

    config = synthetic_config(tmp_path, monkeypatch, verifier=verify, instance_id=instance_id)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    owner = acquire_production_owner(config)
    authenticator = SyntheticAuthenticatorV1()
    port = RecordingEvidencePortV1(authenticator)
    service = LedgerService(owner, port, RuntimeBinding(config.instance_id, "gen1", "user", "chat"))
    return config, approvals, owner, service, port, authenticator


def _registered_journal_config(tmp_path, monkeypatch):
    from finance_core.instance_admission.schema_contract import ApplicationSchemaContract

    approvals = set()

    def verify(reference, action, approval_digest):
        if action in {"prepare", "admit"}:
            return reference == "approved"
        return (reference, action, approval_digest) in approvals

    schema = ApplicationSchemaContract(
        "single-host-runtime-journal-tests-v1",
        (
            "CREATE TABLE c1_application_journal "
            "(operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        ),
    )
    config_root = tmp_path / "journal-config"
    config_root.mkdir()
    config = synthetic_config(config_root, monkeypatch, verifier=verify, application_schema=schema)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    binding, admission = _admit(config, approvals)
    return config, binding, admission


def test_external_binding_requires_protected_admission(runtime_config):
    from finance_core.lifecycle_v2.single_host_runtime_v1 import SingleHostRuntimeAdmissionV1

    assert SingleHostRuntimeAdmissionV1 is not None
    config, _ = runtime_config
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic")
    with acquire_production_owner(config) as owner:
        with pytest.raises((InstanceAdmissionError, LifecycleError)):
            service = LedgerService(owner, Port(), binding, clock=lambda: 1792800000)
            service.capture("source1", "capture1")


@pytest.mark.parametrize("environment", ["single-host-synthetic", "single-host-real"])
def test_registered_scope_posts_once_with_exact_original_operation(runtime_config, environment):
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    config, approvals = runtime_config
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", environment)
    approve_runtime(config, approvals, binding)
    admission = admit_single_host_runtime_v1(
        config, binding, operation_id="runtime", approval_ref="runtime-approved"
    )
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        _, _, review = ready(service)
        receipt = service.confirm(review.review_id, "action1", "confirm1")
        assert service.confirm(review.review_id, "action1", "confirm1") == receipt
        assert service.reader().get(receipt.record_id)["values"]["amount"] == "12.00"
        assert service.reader().full_audit()["fact_version_count"] == 1
        text = "change amount to SGD 15.00；原因：记录有误"
        service.port.sources["edit1"] = VerifiedSource(
            "edit1",
            "user",
            "chat",
            "message-edit",
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            1792800000,
        )
        service.port.raw = response(text, "reply", {"amount": "15.00", "currency": "SGD"})
        corrected = service.prepare_correction(
            receipt.record_id,
            1,
            "obs2",
            "correction1",
            reply_evidence_id="edit1",
            reason="记录有误",
        )
        service.record_delivery(corrected.review_id, "delivery2")
        corrected_receipt = service.confirm(corrected.review_id, "action2", "confirm2")
        assert corrected_receipt.version == 2
        assert service.reader().get(receipt.record_id)["values"]["amount"] == "15.00"
        assert service.reader().full_audit()["fact_version_count"] == 2


def test_registration_requires_exact_approval_and_replays_original(runtime_config):
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    config, approvals = runtime_config
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic")
    before = (config.control_root / STATE_FILENAME).read_bytes()
    with pytest.raises(InstanceAdmissionError):
        admit_single_host_runtime_v1(config, binding, operation_id="runtime", approval_ref="wrong")
    assert (config.control_root / STATE_FILENAME).read_bytes() == before
    approve_runtime(config, approvals, binding)
    admission = admit_single_host_runtime_v1(
        config, binding, operation_id="runtime", approval_ref="runtime-approved"
    )
    registered = (config.control_root / STATE_FILENAME).read_bytes()
    assert (
        admit_single_host_runtime_v1(
            config, binding, operation_id="runtime", approval_ref="runtime-approved"
        )
        == admission
    )
    assert (config.control_root / STATE_FILENAME).read_bytes() == registered


def test_old_native_decoder_rejects_external_scope(runtime_config):
    from finance_core.lifecycle_v2._recovery_material_engine import _binding
    from finance_core.lifecycle_v2._recovery_rows_v1 import ClosureLimitsV1
    from finance_core.lifecycle_v2.recovery_material_v1 import RecoveryMaterialError

    config, _ = runtime_config
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic")
    with pytest.raises(RecoveryMaterialError, match="offline"):
        _binding(asdict(binding), ClosureLimitsV1())


@pytest.mark.parametrize(
    "environment",
    ["production", "offline-real", "Single-host-synthetic", "", None, 7],
)
def test_runtime_binding_keeps_offline_default_and_rejects_unrecognized_environment(environment):
    assert RuntimeBinding("i", "g", "p", "c").environment == "offline-synthetic"
    with pytest.raises((LifecycleError, TypeError)):
        RuntimeBinding("i", "g", "p", "c", environment)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        (
            "binding",
            RuntimeBinding(
                "synthetic-instance-v1", "wrong", "user", "chat", "single-host-synthetic"
            ),
        ),
        ("config_binding_sha256", "0" * 64),
        ("registration_operation_id", "other-operation"),
        ("registration_version", 999),
        ("registration_entry_sha256", "f" * 64),
    ],
)
def test_every_runtime_admission_dto_field_is_checked_before_sql(runtime_config, field, changed):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    forged = replace(admission, **{field: changed})
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=forged
        )
        before = _ledger_rows(service)
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-forged")
        assert _ledger_rows(service) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", "other-instance"),
        ("generation", "other-generation"),
        ("principal_id", "other-principal"),
        ("client_id", "other-client"),
        ("environment", "single-host-real"),
        ("environment", "offline-synthetic"),
    ],
)
def test_binding_tampering_and_environment_borrowing_are_refused_before_effects(
    runtime_config, field, value
):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        before = _ledger_rows(service)
        service.binding = replace(binding, **{field: value})
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-tampered")
        assert _ledger_rows(service) == before


def test_runtime_environment_variable_does_not_substitute_for_registration(
    runtime_config, monkeypatch
):
    config, _ = runtime_config
    monkeypatch.setenv("FINANCE_RUNTIME_ENVIRONMENT", "single-host-synthetic")
    binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic")
    with acquire_production_owner(config) as owner:
        service = LedgerService(owner, Port(), binding, clock=lambda: 1792800000)
        before = _ledger_rows(service)
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-env-only")
        assert _ledger_rows(service) == before


def test_same_instance_id_does_not_allow_runtime_dto_to_cross_config_roots(
    runtime_config, tmp_path, monkeypatch
):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    other_root = tmp_path / "other-config"
    other_root.mkdir()
    other = synthetic_config(other_root, monkeypatch, instance_id=config.instance_id)
    prepare_production_profile(other, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(other, operation_id="admit", approval_ref="approved")
    try:
        with acquire_production_owner(other) as owner:
            service = LedgerService(
                owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
            )
            before = _ledger_rows(service)
            with pytest.raises(InstanceAdmissionError):
                service.capture("source1", "capture-cross-config")
            assert _ledger_rows(service) == before
    finally:
        other.close()


def test_actor_evidence_tampering_does_not_create_final_facts(runtime_config):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        port = Port()
        service = LedgerService(
            owner, port, binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        _, _, review = ready(service)
        before = _ledger_rows(service)
        port.bad_actor = True
        with pytest.raises(LifecycleError, match="actor"):
            service.confirm(review.review_id, "action1", "confirm-actor-tampered")
        assert _ledger_rows(service) == before
        port.bad_actor = False


def test_all_seven_mutators_reject_registered_config_without_exact_proof_before_effects(
    runtime_config,
):
    config, approvals = runtime_config
    offline_binding = RuntimeBinding(config.instance_id, "gen1", "user", "chat")
    with acquire_production_owner(config) as owner:
        port = Port()
        service = LedgerService(owner, port, offline_binding, clock=lambda: 1792800000)
        source, proposal, review = ready(service)
        receipt = service.confirm(review.review_id, "action1", "confirm1")
        external_binding, _ = _admit(config, approvals)
        service.binding = external_binding
        actions = (
            lambda: service.capture("source1", "capture1"),
            lambda: service.propose(source.source_id, "obs1", 0, "propose1"),
            lambda: service.prepare_review(proposal.proposal_id, proposal.version),
            lambda: service.record_delivery(review.review_id, "delivery1"),
            lambda: service.confirm(review.review_id, "action1", "confirm1"),
            lambda: service.prepare_correction(
                "missing-record", 1, "missing-observation", "correction"
            ),
            lambda: service.import_verified(None),
        )
        for action in actions:
            before = _ledger_rows(service)
            with pytest.raises(InstanceAdmissionError):
                action()
            assert _ledger_rows(service) == before
        assert service.reader().get(receipt.record_id)["values"]["amount"] == "12.00"


def test_exact_shared_factories_guard_registered_owner_and_allow_declared_application_journal(
    tmp_path, monkeypatch
):
    from finance_core.instance_admission.sessions import production_read_operation
    from finance_core.lifecycle_v2._finalization_journal_v1 import PreparedFinalizationError

    config, binding, admission = _registered_journal_config(tmp_path, monkeypatch)
    try:
        with acquire_production_owner(config) as owner:
            with pytest.raises(InstanceAdmissionError):
                with production_write_operation(owner) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO c1_application_journal VALUES(?,?)", ("missing-proof", "x")
                    )
            wrong = replace(admission, registration_entry_sha256="0" * 64)
            with pytest.raises(InstanceAdmissionError):
                with lifecycle_write_operation(owner, runtime_admission=wrong) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO c1_application_journal VALUES(?,?)", ("wrong-proof", "x")
                    )
            for operation, label in (
                (production_write_operation, "production"),
                (lifecycle_write_operation, "lifecycle"),
            ):
                with operation(owner, runtime_admission=admission) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO c1_application_journal VALUES(?,?)",
                        (label, "synthetic-owned-write"),
                    )
                    conn.commit()
            with production_read_operation(owner) as conn:
                rows = conn.execute(
                    "SELECT operation_id,payload FROM c1_application_journal ORDER BY operation_id"
                ).fetchall()
                assert [tuple(row) for row in rows] == [
                    ("lifecycle", "synthetic-owned-write"),
                    ("production", "synthetic-owned-write"),
                ]
            external_service = LedgerService(
                owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
            )
            with pytest.raises(PreparedFinalizationError):
                from finance_core.lifecycle_v2.prepared_finalization_v1 import (
                    PreparedFinalizationServiceV1,
                )

                PreparedFinalizationServiceV1(
                    external_service, archive_source=object(), authenticator=object()
                )
    finally:
        config.close()


@pytest.mark.parametrize("restriction", ["maintenance", "fence"])
def test_registered_scope_rechecks_maintenance_and_fencing(runtime_config, restriction):
    from finance_core.instance_admission import (
        enter_maintenance_hold,
        fence_production_instance,
        inspect_instance_admission,
    )

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    from finance_core.instance_admission.authority import _profile

    profile = _profile(config, expected=inspect_instance_admission(config)["db_identity"])
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        with sqlite3.connect(profile.live_database) as conn:
            source_count = conn.execute("SELECT COUNT(*) FROM l2_sources").fetchone()[0]
        if restriction == "maintenance":
            approve_authority_change(config, approvals, "maintenance", "maintenance")
            enter_maintenance_hold(config, operation_id="maintenance", approval_ref="approved")
            assert inspect_instance_admission(config)["maintenance"] is not None
        else:
            approve_authority_change(config, approvals, "fence", "fence")
            fence_production_instance(config, operation_id="fence", approval_ref="approved")
            assert inspect_instance_admission(config)["state"] == "FENCED"
        before = (config.control_root / STATE_FILENAME).read_bytes()
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-after-restriction")
        with sqlite3.connect(profile.live_database) as conn:
            assert conn.execute("SELECT COUNT(*) FROM l2_sources").fetchone()[0] == source_count
        assert (config.control_root / STATE_FILENAME).read_bytes() == before
    profile.close()


def test_registered_scope_rechecks_database_schema_drift_before_effects(runtime_config):
    from finance_core.instance_admission import inspect_instance_admission
    from finance_core.instance_admission.authority import _profile

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        profile = _profile(config, expected=inspect_instance_admission(config)["db_identity"])
        try:
            with sqlite3.connect(profile.live_database) as conn:
                conn.execute("CREATE TABLE c1_unexpected_schema_drift(value TEXT)")
            with pytest.raises(InstanceAdmissionError):
                service.capture("source1", "capture-schema-drift")
            with sqlite3.connect(profile.live_database) as conn:
                assert conn.execute("SELECT COUNT(*) FROM l2_sources").fetchone()[0] == 0
        finally:
            profile.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "execute",
        "executemany",
        "python-commit",
        "sqlite-context",
        "sql-commit",
        "end",
        "outer-release",
        "executescript",
        "isolation",
        "autocommit",
    ],
)
def test_registered_scope_rechecks_authority_file_replacement_at_dynamic_write_boundaries(
    tmp_path, monkeypatch, boundary
):
    from finance_core.instance_admission.sessions import production_read_operation

    config, _, admission = _registered_journal_config(tmp_path, monkeypatch)
    state_path = config.control_root / STATE_FILENAME
    original_state = state_path.read_bytes()
    sql = "INSERT INTO c1_application_journal VALUES(?,?)"
    pending = ("pending-" + boundary, "before-authority-drift")
    blocked = ("blocked-" + boundary, "must-not-commit")
    try:
        with acquire_production_owner(config) as owner:
            with lifecycle_write_operation(owner, runtime_admission=admission) as conn:
                conn.execute(
                    "SAVEPOINT c1_runtime_outer"
                    if boundary == "outer-release"
                    else "BEGIN IMMEDIATE"
                )
                conn.execute(sql, pending)
                state_path.write_bytes(b"{}")
                try:
                    with pytest.raises(InstanceAdmissionError):
                        if boundary == "execute":
                            conn.execute(sql, blocked)
                        elif boundary == "executemany":
                            conn.executemany(sql, [blocked])
                        elif boundary == "python-commit":
                            conn.commit()
                        elif boundary == "sqlite-context":
                            conn.__exit__(None, None, None)
                        elif boundary == "sql-commit":
                            conn.execute("COMMIT")
                        elif boundary == "end":
                            conn.execute("END")
                        elif boundary == "outer-release":
                            conn.execute("RELEASE c1_runtime_outer")
                        elif boundary == "executescript":
                            conn.executescript("SELECT 1;")
                        elif boundary == "isolation":
                            conn.isolation_level = None
                        elif boundary == "autocommit":
                            conn.autocommit = True
                    assert conn.in_transaction
                finally:
                    state_path.write_bytes(original_state)
            with production_read_operation(owner) as conn:
                assert (
                    conn.execute("SELECT COUNT(*) FROM c1_application_journal").fetchone()[0] == 0
                )
    finally:
        config.close()


def test_registered_scope_rejects_closed_owner_before_write(runtime_config):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    owner, service = _runtime_service(config, binding, admission)
    owner.close()
    with pytest.raises((InstanceAdmissionError, LifecycleError)):
        service.capture("source1", "capture-after-owner-close")
    with acquire_production_owner(config) as observer:
        with lifecycle_read_operation(observer) as conn:
            assert conn.execute("SELECT COUNT(*) FROM l2_sources").fetchone()[0] == 0


def test_one_config_has_one_runtime_registration_and_concurrent_attempts_do_not_multiply(
    runtime_config,
):
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    config, approvals = runtime_config
    concurrent = [
        RuntimeBinding(config.instance_id, "gen1", "user", "chat", "single-host-synthetic"),
        RuntimeBinding(config.instance_id, "gen2", "user", "chat", "single-host-real"),
    ]
    for index, binding in enumerate(concurrent):
        approve_runtime(
            config,
            approvals,
            binding,
            operation=f"concurrent-{index}",
            reference=f"approved-{index}",
        )

    def attempt(index):
        try:
            return admit_single_host_runtime_v1(
                config,
                concurrent[index],
                operation_id=f"concurrent-{index}",
                approval_ref=f"approved-{index}",
            )
        except InstanceAdmissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert sum(result is not None for result in results) == 1
    state = json.loads((config.control_root / STATE_FILENAME).read_bytes())
    registered = [
        entry for entry in state["history"] if entry["action"] == "single_host_runtime_admit_v1"
    ]
    assert len(registered) == 1
    winner = next(index for index, result in enumerate(results) if result is not None)
    alternate = concurrent[1 - winner]
    with pytest.raises(InstanceAdmissionError):
        admit_single_host_runtime_v1(
            config,
            alternate,
            operation_id=f"concurrent-{winner}",
            approval_ref=f"approved-{winner}",
        )
    assert (
        len(
            [
                entry
                for entry in json.loads((config.control_root / STATE_FILENAME).read_bytes())[
                    "history"
                ]
                if entry["action"] == "single_host_runtime_admit_v1"
            ]
        )
        == 1
    )


def test_prepared_instance_can_register_but_remains_unwritable(tmp_path, monkeypatch):
    from finance_core.instance_admission import inspect_instance_admission

    approvals = set()

    def verify(reference, action, approval_digest):
        if action == "prepare":
            return reference == "approved"
        return (reference, action, approval_digest) in approvals

    config = synthetic_config(tmp_path, monkeypatch, verifier=verify)
    try:
        prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
        assert inspect_instance_admission(config)["state"] == "PREPARED"
        _, admission = _admit(config, approvals)
        state = inspect_instance_admission(config)
        assert state["state"] == "PREPARED"
        assert state["admission"] is None
        assert admission.registration_operation_id == "runtime"
        with pytest.raises(InstanceAdmissionError):
            acquire_production_owner(config)
    finally:
        config.close()


def test_restore_held_instance_can_register_without_changing_held_record(tmp_path, monkeypatch):
    from finance_core.instance_admission import inspect_instance_admission
    from finance_core.lifecycle_v2.recovery import prepare_restored_profile

    old_root = tmp_path / "old-profile"
    old_root.mkdir()
    old = synthetic_config(old_root, monkeypatch, instance_id="old-runtime-source")
    prepare_production_profile(old, operation_id="prepare-old", approval_ref="approved").close()
    admit_production_instance(old, operation_id="admit-old", approval_ref="approved")
    old_owner = acquire_production_owner(old)
    old_service = LedgerService(
        old_owner,
        Port(),
        RuntimeBinding(old.instance_id, "old-generation", "user", "chat"),
        clock=lambda: 1792800000,
    )
    _, _, review = ready(old_service)
    old_service.confirm(review.review_id, "action-old", "confirm-old")
    cut_path = tmp_path / "held-source.sqlite"
    cut = old_service.seal_snapshot(cut_path)
    old_owner.close()
    old.close()

    approvals = set()

    def verify(reference, action, approval_digest):
        if action in {"restore_prepare", "prepare", "admit"}:
            return reference == "approved"
        return (reference, action, approval_digest) in approvals

    target_root = tmp_path / "held-target"
    target_root.mkdir()
    target = synthetic_config(
        target_root, monkeypatch, verifier=verify, instance_id="held-runtime-target"
    )
    try:
        restore_binding = RuntimeBinding(target.instance_id, "new-generation", "user", "chat")
        prepare_restored_profile(
            target,
            cut_path,
            cut,
            new_binding=restore_binding,
            operation_id="restore-held",
            approval_ref="approved",
        )
        before = json.loads((target.control_root / STATE_FILENAME).read_bytes())
        record_before = before["instances"][target.binding_sha256]
        runtime_binding, admission = _admit(target, approvals)
        after = json.loads((target.control_root / STATE_FILENAME).read_bytes())
        assert after["instances"][target.binding_sha256] == record_before
        assert inspect_instance_admission(target)["state"] == "RESTORE_HELD"
        assert admission.binding == runtime_binding
        assert len(after["history"]) == len(before["history"]) + 1
        with pytest.raises(InstanceAdmissionError):
            acquire_production_owner(target)
    finally:
        target.close()


def test_runtime_registration_preserves_existing_maintenance_hold(runtime_config):
    from finance_core.instance_admission import enter_maintenance_hold, inspect_instance_admission

    config, approvals = runtime_config
    approve_authority_change(config, approvals, "maintenance", "maintenance-before-runtime")
    enter_maintenance_hold(
        config, operation_id="maintenance-before-runtime", approval_ref="approved"
    )
    before = inspect_instance_admission(config)
    binding, admission = _admit(config, approvals)
    after = inspect_instance_admission(config)
    assert after["state"] == before["state"] == "ACTIVE"
    assert after["maintenance"] == before["maintenance"]
    with acquire_production_owner(config) as owner:
        service = LedgerService(owner, Port(), binding, runtime_admission=admission)
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-maintenance-held")


def test_fenced_instance_cannot_receive_first_runtime_registration(runtime_config):
    from finance_core.instance_admission import (
        fence_production_instance,
        inspect_instance_admission,
    )

    config, approvals = runtime_config
    approve_authority_change(config, approvals, "fence", "fence-before-runtime")
    fence_production_instance(config, operation_id="fence-before-runtime", approval_ref="approved")
    before = (config.control_root / STATE_FILENAME).read_bytes()
    with pytest.raises(InstanceAdmissionError):
        _admit(config, approvals)
    assert (config.control_root / STATE_FILENAME).read_bytes() == before
    assert inspect_instance_admission(config)["state"] == "FENCED"


def test_pending_offline_preparation_blocks_registration_but_exact_apply_and_cancel_remain_usable(
    tmp_path, monkeypatch
):
    from lifecycle_recovery_material_support_v1 import stage_expense

    from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1

    config, approvals, owner, service, port, authenticator = _prepared_test_service(
        tmp_path, monkeypatch
    )
    api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=authenticator)
    try:
        _, _, review, _ = stage_expense(service, port, "pending-cancel", stage="delivery")
        port.register_action(service, review, "delivery-pending-cancel", "action-pending-cancel")
        cancelled_ref = api.prepare(review.review_id, "action-pending-cancel", "pending-cancel")
        binding = RuntimeBinding(
            config.instance_id, "gen1", "user", "chat", "single-host-synthetic"
        )
        approve_runtime(config, approvals, binding)
        before = (config.control_root / STATE_FILENAME).read_bytes()
        with pytest.raises(InstanceAdmissionError, match="Pending"):
            from finance_core.lifecycle_v2.single_host_runtime_v1 import (
                admit_single_host_runtime_v1,
            )

            admit_single_host_runtime_v1(
                config, binding, operation_id="runtime", approval_ref="runtime-approved"
            )
        assert (config.control_root / STATE_FILENAME).read_bytes() == before
        cancelled = api.cancel_offline(
            "pending-cancel", expected_intent_sha256=cancelled_ref.intent_sha256
        )
        assert cancelled.state == "CANCELLED_OFFLINE"
        assert not service.reader().full_audit()["fact_version_count"]

        _, _, apply_review, _ = stage_expense(service, port, "pending-apply", stage="delivery")
        port.register_action(
            service, apply_review, "delivery-pending-apply", "action-pending-apply"
        )
        apply_ref = api.prepare(apply_review.review_id, "action-pending-apply", "pending-apply")
        receipt = api.apply_offline("pending-apply", expected_intent_sha256=apply_ref.intent_sha256)
        assert receipt.record_id == apply_ref.record_id
        assert service.reader().full_audit()["fact_version_count"] == 1
    finally:
        if owner._lease.fd >= 0:
            owner.close()
        config.close()


def test_offline_prepared_native_and_content_decoders_reject_external_scope(tmp_path, monkeypatch):
    import base64

    from lifecycle_recovery_material_support_v1 import stage_expense

    from finance_core.lifecycle_v2._finalization_journal_v1 import (
        PreparedFinalizationError,
        _verify_journal,
    )
    from finance_core.lifecycle_v2._prepared_content_v1 import _validate_prepared_content_anchor_v1
    from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
    from finance_core.lifecycle_v2.recovery_material_v1 import (
        ClosureLimitsV1,
        RecoveryMaterialError,
        decode_native_closure_v1,
    )
    from finance_core.lifecycle_v2.recovery_material_v2 import decode_native_closure_v2
    from finance_core.profile_layout import profile_root

    config, _, owner, service, port, authenticator = _prepared_test_service(
        tmp_path, monkeypatch, instance_id="offline-decoder-tests"
    )
    api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=authenticator)
    try:
        _, _, review, _ = stage_expense(service, port, "offline-material", stage="delivery")
        port.register_action(
            service, review, "delivery-offline-material", "action-offline-material"
        )
        ref = api.prepare(review.review_id, "action-offline-material", "offline-material")
        packages = api.material("offline-material", expected_intent_sha256=ref.intent_sha256)
        content = api.export_content_v1(
            "offline-material", expected_intent_sha256=ref.intent_sha256
        )
        offline_binding = service.binding
        external_binding = replace(offline_binding, environment="single-host-synthetic")
        raw_v1 = json.loads(
            (
                Path(__file__).with_name("fixtures") / "native_recovery_material_v1_061.json"
            ).read_bytes()
        )
        historical_package = base64.b64decode(raw_v1["packages_b64"][-1], validate=True)
        historical = json.loads(historical_package)
        historical["identity"]["source_binding"]["environment"] = external_binding.environment
        with pytest.raises(RecoveryMaterialError, match="offline"):
            decode_native_closure_v1(canonical(historical).encode())

        segment = json.loads(packages[-1])
        segment["identity"]["source_binding"]["environment"] = external_binding.environment
        with pytest.raises(RecoveryMaterialError, match="offline"):
            decode_native_closure_v2(canonical(segment).encode())
        tampered_identity = replace(content.anchor.source_identity, source_binding=external_binding)
        tampered_anchor = replace(content.anchor, source_identity=tampered_identity)
        with pytest.raises(RecoveryMaterialError, match="offline"):
            _validate_prepared_content_anchor_v1(tampered_anchor, ClosureLimitsV1())

        service.binding = external_binding
        with pytest.raises(PreparedFinalizationError):
            api.material("offline-material", expected_intent_sha256=ref.intent_sha256)
        service.binding = offline_binding
        owner.close()
        database = profile_root(config.locator_root, config.profile_id, config.layout) / (
            "runtime/database/finance.db"
        )
        with sqlite3.connect(database) as conn:
            triggers = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='l2_finalization_intents'"
            ).fetchall()
            for trigger in triggers:
                conn.execute("DROP TRIGGER " + trigger[0])
            body = conn.execute(
                "SELECT body_json FROM l2_finalization_intents WHERE operation_id=?",
                ("offline-material",),
            ).fetchone()[0]
            value = json.loads(body)
            value["binding"]["environment"] = external_binding.environment
            changed_body = canonical(value)
            conn.execute(
                "UPDATE l2_finalization_intents SET body_json=?,intent_sha256=? "
                "WHERE operation_id=?",
                (changed_body, digest(value), "offline-material"),
            )
        with acquire_production_owner(config) as decoder_owner:
            with pytest.raises(InstanceAdmissionError):
                with lifecycle_read_operation(decoder_owner):
                    pytest.fail("production read accepted a damaged sealed ledger")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            with pytest.raises(PreparedFinalizationError):
                _verify_journal(conn)
    finally:
        if owner._lease.fd >= 0:
            owner.close()
        config.close()


def test_old_authority_bytes_and_instance_record_shape_survive_new_runtime_action(runtime_config):
    from finance_core.instance_admission import authority, inspect_instance_admission

    config, approvals = runtime_config
    state_path = config.control_root / STATE_FILENAME
    old_bytes = state_path.read_bytes()
    inspect_instance_admission(config)
    assert state_path.read_bytes() == old_bytes
    before = json.loads(old_bytes)
    original_record = before["instances"][config.binding_sha256]
    binding, _ = _admit(config, approvals)
    state = json.loads(state_path.read_bytes())
    assert set(state) == {"format", "version", "current", "instances", "history"}
    assert state["instances"][config.binding_sha256] == original_record
    assert set(original_record) == {
        "binding",
        "db_identity",
        "ledger_sha256",
        "state",
        "admission",
        "maintenance",
    }
    old_fields = {
        "version",
        "action",
        "operation_id",
        "binding",
        "approval_ref",
        "approval_digest",
        "context_digest",
        "record_identity",
        "hold_baseline",
        "previous_sha256",
        "before_sha256",
        "after_sha256",
        "entry_sha256",
    }
    assert all(set(entry) == old_fields for entry in before["history"])
    runtime_entry = state["history"][-1]
    assert set(runtime_entry) == old_fields | {"runtime_context"}
    assert runtime_entry["runtime_context"] == {
        "format": "single-host-runtime-admission-v1",
        "runtime_binding": asdict(binding),
    }
    assert set(runtime_entry["runtime_context"]) == {"format", "runtime_binding"}

    malformed = json.loads(json.dumps(state))
    malformed_entry = malformed["history"][-1]
    malformed_entry["runtime_context"]["unrecognized"] = True
    malformed_entry["context_digest"] = _digest(malformed_entry["runtime_context"])
    malformed_entry["entry_sha256"] = _digest(
        {key: value for key, value in malformed_entry.items() if key != "entry_sha256"}
    )
    with pytest.raises(InstanceAdmissionError):
        authority._validate_state(malformed)
    unknown_action = json.loads(json.dumps(state))
    unknown_action["history"][-1]["action"] = "unrecognized_runtime_action"
    unknown_action["history"][-1]["entry_sha256"] = _digest(
        {
            key: value
            for key, value in unknown_action["history"][-1].items()
            if key != "entry_sha256"
        }
    )
    with pytest.raises(InstanceAdmissionError):
        authority._validate_state(unknown_action)


def test_external_snapshot_is_read_back_and_reader_audit_stays_read_only(runtime_config, tmp_path):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        _, _, review = ready(service)
        receipt = service.confirm(review.review_id, "action1", "confirm1")
        before = _ledger_rows(service)
        destination = tmp_path / "external-runtime-snapshot.sqlite"
        cut = service.seal_snapshot(destination)
        assert destination.is_file()
        assert cut.instance_id == config.instance_id
        assert cut.generation == binding.generation
        assert cut.facts == ((receipt.record_id, 1, receipt.fact_sha256),)
        assert service.reader().full_audit()["fact_version_count"] == 1
        assert _ledger_rows(service) == before


def test_unavailable_backup_destination_does_not_block_local_confirmation(runtime_config, tmp_path):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(
            owner, Port(), binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        _, _, review = ready(service)
        unavailable = tmp_path / "missing-private-parent" / "snapshot.sqlite"
        with pytest.raises(LifecycleError, match="destination|parent"):
            service.seal_snapshot(unavailable)
        receipt = service.confirm(review.review_id, "action1", "confirm-local")
        assert service.reader().get(receipt.record_id)["values"]["amount"] == "12.00"
        assert service.reader().full_audit()["fact_version_count"] == 1
        assert not unavailable.parent.exists()


@pytest.mark.parametrize("field", ["owner", "binding", "runtime_admission"])
def test_callback_cannot_change_any_runtime_identity_field_before_commit(runtime_config, field):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        port = Port()
        service = LedgerService(
            owner, port, binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        before = _ledger_rows(service)
        original_source = port.source
        replacement = {
            "owner": None,
            "binding": replace(binding, generation="callback-drift"),
            "runtime_admission": None,
        }[field]

        def mutate_during_source(conn, evidence_id):
            setattr(service, field, replacement)
            return original_source(conn, evidence_id)

        port.source = mutate_during_source
        with pytest.raises(LifecycleError, match="Runtime identity"):
            service.capture("source1", "capture-callback-mutation")
        assert _ledger_rows(service) == before


def test_callback_thread_cannot_change_runtime_scope_and_failure_cleanup_allows_later_assignment(
    runtime_config,
):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        port = Port()
        service = LedgerService(
            owner, port, binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        before = _ledger_rows(service)
        original_source = port.source

        def mutate_from_callback_thread(conn, evidence_id):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(
                    setattr, service, "binding", replace(binding, generation="thread-drift")
                ).result()
            return original_source(conn, evidence_id)

        port.source = mutate_from_callback_thread
        with pytest.raises(LifecycleError, match="Runtime identity"):
            service.capture("source1", "capture-cross-thread-mutation")
        assert _ledger_rows(service) == before

        def fail_ordinary_callback(_conn, _evidence_id):
            raise ValueError("synthetic evidence callback failure")

        port.source = fail_ordinary_callback
        with pytest.raises(ValueError, match="synthetic evidence callback failure"):
            service.capture("source1", "capture-ordinary-callback-failure")
        service.binding = replace(binding, generation="between-calls")
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-between-call-drift")
        assert _ledger_rows(service) == before
        service.binding = binding
        port.source = original_source
        captured = service.capture("source1", "capture-after-cleanup")
        assert captured.source_id
        assert len(_ledger_rows(service)["sources"]) == 1


def test_current_read_callback_cannot_change_runtime_identity(runtime_config):
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        port = Port()
        service = LedgerService(
            owner, port, binding, clock=lambda: 1792800000, runtime_admission=admission
        )
        source = service.capture("source1", "capture-before-current-read")
        before = _ledger_rows(service)
        original_source = port.source

        def mutate_during_current_read(conn, evidence_id):
            service.runtime_admission = None
            return original_source(conn, evidence_id)

        port.source = mutate_during_current_read
        with pytest.raises(LifecycleError, match="Runtime identity"):
            service.expected_invocation(source.source_id, 0)
        assert _ledger_rows(service) == before


@pytest.mark.parametrize(
    "identity",
    ["config_identity", "core_identity", "application_schema", "profile_id", "control_root"],
)
def test_matrix_registered_reference_cannot_cross_trusted_config_identity(
    runtime_config, tmp_path, identity
):
    import shutil

    from finance_core.instance_admission.schema_contract import ApplicationSchemaContract

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        before = _ledger_rows(LedgerService(owner, Port(), binding, runtime_admission=admission))
    changes = {
        "config_identity": "other-trusted-config",
        "core_identity": "other-installed-core",
        "application_schema": ApplicationSchemaContract(
            "other-runtime-schema-v1", ("CREATE TABLE other_runtime_journal (id TEXT)",)
        ),
        "profile_id": "other-profile",
    }
    if identity == "control_root":
        other_control = tmp_path / "other-protected-control"
        other_control.mkdir(mode=0o700)
        # Copy actual synthetic control documents, not an invented authority DTO.
        # Their original protected binding still points to the original root/inode.
        for path in config.control_root.iterdir():
            if path.is_file():
                target = other_control / path.name
                shutil.copyfile(path, target)
                target.chmod(0o600)
        changes[identity] = other_control
    altered = replace(config, **{identity: changes[identity]})
    try:
        assert altered.binding_sha256 != config.binding_sha256
        with pytest.raises(InstanceAdmissionError, match="trusted composition"):
            with acquire_production_owner(altered) as owner:
                LedgerService(owner, Port(), binding, runtime_admission=admission).capture(
                    "source1", "cross-trusted-identity"
                )
    finally:
        altered.close()
    with acquire_production_owner(config) as owner:
        assert (
            _ledger_rows(LedgerService(owner, Port(), binding, runtime_admission=admission))
            == before
        )


def test_matrix_registered_operation_rejects_changed_installed_migration_identity(
    runtime_config, monkeypatch
):
    from finance_core.instance_admission import authority

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(owner, Port(), binding, runtime_admission=admission)
        before = _ledger_rows(service)
        with monkeypatch.context() as drift:
            # The installed contract observation changes; migration resources stay intact.
            drift.setattr(authority, "_migration_contract_digest", lambda: "0" * 64)
            with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
                service.capture("source1", "changed-installation")
            with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
                with lifecycle_write_operation(owner, runtime_admission=admission):
                    pytest.fail("Changed installed identity opened a registered write session")
        assert _ledger_rows(service) == before


@pytest.mark.parametrize(
    "forgery",
    [
        "dto-subclass",
        "dto-duck-type",
        "dto-equal-float-version",
        "dto-string-subclass",
        "binding-subclass",
        "binding-duck-type",
        "binding-string-subclass",
    ],
)
def test_matrix_registered_scope_requires_exact_reference_and_binding_types(
    runtime_config, forgery
):
    from types import SimpleNamespace

    from finance_core.lifecycle_v2.single_host_runtime_v1 import SingleHostRuntimeAdmissionV1

    class Text(str):
        pass

    class AdmissionSubclass(SingleHostRuntimeAdmissionV1):
        pass

    class BindingSubclass(RuntimeBinding):
        pass

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(owner, Port(), binding, runtime_admission=admission)
        before = _ledger_rows(service)
        fields = dict(vars(admission))
        if forgery == "dto-subclass":
            service.runtime_admission = AdmissionSubclass(**fields)
        elif forgery == "dto-duck-type":
            service.runtime_admission = SimpleNamespace(**fields)
        elif forgery == "dto-equal-float-version":
            forged = replace(admission, registration_version=float(admission.registration_version))
            assert forged.registration_version == admission.registration_version
            service.runtime_admission = forged
        elif forgery == "dto-string-subclass":
            service.runtime_admission = replace(
                admission, config_binding_sha256=Text(admission.config_binding_sha256)
            )
        elif forgery == "binding-subclass":
            service.binding = BindingSubclass(**asdict(binding))
        elif forgery == "binding-duck-type":
            service.binding = SimpleNamespace(**asdict(binding))
        else:
            forged_binding = replace(binding)
            object.__setattr__(forged_binding, "generation", Text(binding.generation))
            service.binding = forged_binding
        with pytest.raises(InstanceAdmissionError):
            service.capture("source1", "capture-nonexact-types")
        assert _ledger_rows(service) == before


@pytest.mark.parametrize("mode", ["prepare", "apply", "cancel"])
def test_matrix_registered_config_cannot_enter_private_offline_resolver(runtime_config, mode):
    from finance_core.instance_admission.sessions import _prepared_finalization_operation

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    authority_before = (config.control_root / STATE_FILENAME).read_bytes()
    with acquire_production_owner(config) as owner:
        service = LedgerService(owner, Port(), binding, runtime_admission=admission)
        before = _ledger_rows(service)
        with pytest.raises(InstanceAdmissionError, match="runtime admission"):
            with _prepared_finalization_operation(owner, "private-offline-operation", mode):
                pytest.fail("Registered config entered the proof-free offline resolver")
        assert _ledger_rows(service) == before
        assert (config.control_root / STATE_FILENAME).read_bytes() == authority_before


def test_matrix_registered_owner_and_issued_connection_reject_actual_fork(tmp_path, monkeypatch):
    import os
    import select
    import signal

    if not hasattr(os, "fork"):
        pytest.skip("Actual fork requires the supported POSIX host")
    config, binding, admission = _registered_journal_config(tmp_path, monkeypatch)
    try:
        with acquire_production_owner(config) as owner:
            service = LedgerService(owner, Port(), binding, runtime_admission=admission)
            with lifecycle_write_operation(owner, runtime_admission=admission) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO c1_application_journal VALUES(?,?)", ("parent-pending", "x")
                )
                read_fd, write_fd = os.pipe()
                child = os.fork()
                if child == 0:
                    os.close(read_fd)
                    outcomes = {}
                    try:
                        for name, operation in (
                            ("service", lambda: service.capture("source1", "fork-capture")),
                            (
                                "issued-sql",
                                lambda: conn.execute(
                                    "INSERT INTO c1_application_journal VALUES(?,?)",
                                    ("forked-child", "must-not-exist"),
                                ),
                            ),
                            ("issued-commit", conn.commit),
                            ("owner-close", owner.close),
                        ):
                            try:
                                operation()
                            except InstanceAdmissionError:
                                outcomes[name] = "REFUSED"
                            else:
                                outcomes[name] = "UNEXPECTED_SUCCESS"
                        os.write(write_fd, json.dumps(outcomes).encode())
                    except BaseException:
                        os._exit(2)
                    os._exit(0)
                os.close(write_fd)
                try:
                    readable, _, _ = select.select([read_fd], [], [], 10)
                    if not readable:
                        os.kill(child, signal.SIGKILL)
                    assert readable, "Forked runtime guard failed to return within ten seconds"
                    payload = os.read(read_fd, 4096)
                finally:
                    os.close(read_fd)
                    _, status = os.waitpid(child, 0)
                assert os.waitstatus_to_exitcode(status) == 0
                assert json.loads(payload) == {
                    name: "REFUSED"
                    for name in ("service", "issued-sql", "issued-commit", "owner-close")
                }
                assert conn.in_transaction
                conn.rollback()
            assert not _ledger_rows(service)["sources"]
            with lifecycle_read_operation(owner) as conn:
                assert (
                    conn.execute("SELECT COUNT(*) FROM c1_application_journal").fetchone()[0] == 0
                )
    finally:
        config.close()


def test_matrix_registered_write_rejects_atomic_authority_inode_replacement(tmp_path, monkeypatch):
    import os

    config, _, admission = _registered_journal_config(tmp_path, monkeypatch)
    state_path = config.control_root / STATE_FILENAME
    original_bytes = state_path.read_bytes()
    original_inode = state_path.stat().st_ino
    retained = state_path.with_name("matrix-retained-authority.json")
    replacement = state_path.with_name("matrix-invalid-authority.json")
    try:
        with acquire_production_owner(config) as owner:
            with lifecycle_write_operation(owner, runtime_admission=admission) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO c1_application_journal VALUES(?,?)", ("before-replacement", "x")
                )
                replacement.write_bytes(b"{}")
                replacement.chmod(0o600)
                assert replacement.stat().st_ino != original_inode
                os.replace(state_path, retained)
                os.replace(replacement, state_path)
                try:
                    assert state_path.stat().st_ino != original_inode
                    for operation in (
                        lambda: conn.execute(
                            "INSERT INTO c1_application_journal VALUES(?,?)",
                            ("after-replacement", "must-not-commit"),
                        ),
                        conn.commit,
                    ):
                        with pytest.raises(InstanceAdmissionError):
                            operation()
                        assert conn.in_transaction
                finally:
                    os.replace(retained, state_path)
            assert state_path.stat().st_ino == original_inode
            assert state_path.read_bytes() == original_bytes
            with lifecycle_read_operation(owner) as conn:
                assert (
                    conn.execute("SELECT COUNT(*) FROM c1_application_journal").fetchone()[0] == 0
                )
    finally:
        config.close()


def test_matrix_completed_registration_rejects_same_binding_under_new_operation(runtime_config):
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    state_path = config.control_root / STATE_FILENAME
    before = state_path.read_bytes()
    approve_runtime(config, approvals, binding, operation="new-operation", reference="new-approval")
    with pytest.raises(InstanceAdmissionError, match="exact original retry"):
        admit_single_host_runtime_v1(
            config, binding, operation_id="new-operation", approval_ref="new-approval"
        )
    assert state_path.read_bytes() == before
    assert (
        admit_single_host_runtime_v1(
            config, binding, operation_id="runtime", approval_ref="runtime-approved"
        )
        == admission
    )
    assert state_path.read_bytes() == before


def test_matrix_terminal_offline_intents_survive_registration_and_read_only_audit(
    tmp_path, monkeypatch
):
    from lifecycle_recovery_material_support_v1 import stage_expense

    from finance_core.lifecycle_v2._finalization_journal_v1 import _verify_journal
    from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1

    config, approvals, owner, service, port, authenticator = _prepared_test_service(
        tmp_path, monkeypatch, instance_id="terminal-offline-runtime"
    )
    api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=authenticator)
    try:
        receipts = {}
        for mode in ("cancel", "apply"):
            operation = "terminal-" + mode
            _, _, review, _ = stage_expense(service, port, operation, stage="delivery")
            port.register_action(service, review, "delivery-" + operation, "action-" + operation)
            reference = api.prepare(review.review_id, "action-" + operation, operation)
            receipts[mode] = getattr(api, mode + "_offline")(
                operation, expected_intent_sha256=reference.intent_sha256
            )
        with lifecycle_read_operation(owner) as conn:
            before_journal = {
                table: tuple(tuple(row) for row in conn.execute("SELECT * FROM " + table))
                for table in (
                    "l2_finalization_intents",
                    "l2_finalization_materials",
                    "l2_finalization_outcomes",
                )
            }
        before_rows = _ledger_rows(service)
        binding, admission = _admit(config, approvals)
        external = LedgerService(owner, port, binding, runtime_admission=admission)
        assert external.reader().full_audit()["fact_version_count"] == 1
        assert external.reader().get(receipts["apply"].record_id)["values"]["amount"] == "12.00"
        # The original offline forensic reader also retains its historical scope.
        assert service.reader().full_audit()["fact_version_count"] == 1
        with lifecycle_read_operation(owner) as conn:
            journal = _verify_journal(conn)
            assert {key: entry.outcome for key, entry in journal.items()} == {
                "terminal-cancel": "CANCELLED_OFFLINE",
                "terminal-apply": "APPLIED_OFFLINE",
            }
            assert {
                table: tuple(tuple(row) for row in conn.execute("SELECT * FROM " + table))
                for table in before_journal
            } == before_journal
        assert _ledger_rows(external) == before_rows
    finally:
        if owner._lease.fd >= 0:
            owner.close()
        config.close()


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("missing-format", "runtime context"),
        ("missing-binding", "runtime context"),
        ("missing-binding-field", "runtime binding"),
        ("unknown-format", "runtime context"),
        ("unknown-version", "runtime context"),
        ("wrong-context-digest", "context binding differs"),
        ("duplicate-config-registration", "one unfenced config"),
    ],
)
def test_matrix_malformed_runtime_history_is_refused_after_rehashing(
    runtime_config, corruption, message
):
    from finance_core.instance_admission import authority, inspect_instance_admission

    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    state_path = config.control_root / STATE_FILENAME
    original = state_path.read_bytes()
    state = json.loads(original)
    entry = state["history"][-1]
    context = entry["runtime_context"]
    if corruption == "missing-format":
        del context["format"]
    elif corruption == "missing-binding":
        del context["runtime_binding"]
    elif corruption == "missing-binding-field":
        del context["runtime_binding"]["generation"]
    elif corruption == "unknown-format":
        context["format"] = "unrecognized-runtime-admission"
    elif corruption == "unknown-version":
        context["format"] = "single-host-runtime-admission-v2"
    elif corruption == "wrong-context-digest":
        entry["context_digest"] = "0" * 64
    else:
        duplicate = dict(entry)
        duplicate["operation_id"] = "duplicate-config-registration"
        duplicate["approval_ref"] = "duplicate-approved"
        duplicate["version"] = state["version"] + 1
        duplicate["previous_sha256"] = entry["entry_sha256"]
        duplicate["before_sha256"] = _digest(authority._body(state))
        duplicate["approval_digest"] = _digest(
            {
                "binding": config._binding,
                "action": "single_host_runtime_admit_v1",
                "context": {
                    "operation_id": duplicate["operation_id"],
                    "before_sha256": _digest(state),
                    "context": context,
                },
            }
        )
        state["version"] += 1
        duplicate["after_sha256"] = _digest(authority._body(state))
        state["history"].append(duplicate)
        entry = duplicate
    if corruption != "wrong-context-digest":
        entry["context_digest"] = _digest(context)
    entry["entry_sha256"] = _digest(
        {key: value for key, value in entry.items() if key != "entry_sha256"}
    )
    malformed = authority._canonical(state)
    try:
        state_path.write_bytes(malformed)
        with pytest.raises(InstanceAdmissionError, match=message):
            inspect_instance_admission(config)
        with pytest.raises(InstanceAdmissionError, match=message):
            with acquire_production_owner(config):
                pytest.fail("Malformed runtime history acquired a real owner")
        assert state_path.read_bytes() == malformed
    finally:
        state_path.write_bytes(original)
    with acquire_production_owner(config) as owner:
        assert not _ledger_rows(LedgerService(owner, Port(), binding, runtime_admission=admission))[
            "sources"
        ]


@pytest.fixture
def review_fix_offline_case(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from lifecycle_recovery_material_support_v1 import stage_expense

    from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1

    config, _, owner, service, port, authenticator = _prepared_test_service(
        tmp_path, monkeypatch, instance_id="prepared-review-fix"
    )
    service.clock = lambda: 1792800000
    api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=authenticator)
    try:
        _, _, review, _ = stage_expense(service, port, "review-fix", stage="delivery")
        port.register_action(service, review, "delivery-review-fix", "action-review-fix")
        yield SimpleNamespace(
            config=config,
            owner=owner,
            service=service,
            port=port,
            authenticator=authenticator,
            api=api,
            review=review,
            action="action-review-fix",
            operation="review-fix-finalize",
        )
    finally:
        if owner._lease.fd >= 0:
            owner.close()
        config.close()


def _review_fix_rows(owner):
    names = (
        "sources",
        "proposals",
        "reviews",
        "deliveries",
        "facts",
        "decisions",
        "events",
        "finalization_intents",
        "finalization_materials",
        "finalization_outcomes",
    )
    with lifecycle_read_operation(owner) as conn:
        return {
            name: tuple(tuple(row) for row in conn.execute("SELECT * FROM l2_" + name))
            for name in names
        }


@pytest.mark.parametrize("field", ["binding", "owner", "runtime_admission"])
@pytest.mark.parametrize(
    ("mode", "callback"),
    [("prepare", "action"), ("apply", "action"), ("apply", "late-clock"), ("cancel", "late-clock")],
)
def test_review_fix_prepared_callback_refuses_identity_drift_and_exact_retry(
    review_fix_offline_case, field, mode, callback
):
    case = review_fix_offline_case
    api, service = case.api, case.service
    reference = None
    if mode != "prepare":
        reference = api.prepare(case.review.review_id, case.action, case.operation)
    before = _review_fix_rows(case.owner)
    state_before = (case.config.control_root / STATE_FILENAME).read_bytes()
    original_binding = service.binding
    original_action, original_clock = case.port.action, service.clock
    changed = {
        "binding": replace(original_binding, environment="single-host-real"),
        "owner": None,
        "runtime_admission": object(),
    }[field]
    calls = []

    def mutate_action(conn, evidence_id, expected):
        value = original_action(conn, evidence_id, expected)
        calls.append("action")
        setattr(service, field, changed)
        return value

    def mutate_late_clock():
        calls.append("clock")
        if len(calls) == (2 if mode == "apply" else 1):
            setattr(service, field, changed)
        return 1792800000

    if callback == "action":
        case.port.action = mutate_action
    else:
        service.clock = mutate_late_clock
    try:
        with pytest.raises(LifecycleError, match="Runtime identity cannot change"):
            if mode == "prepare":
                api.prepare(case.review.review_id, case.action, case.operation)
            else:
                getattr(api, mode + "_offline")(
                    case.operation, expected_intent_sha256=reference.intent_sha256
                )
    finally:
        case.port.action = original_action
        service.clock = original_clock
    assert calls == (
        ["action"] if callback == "action" else ["clock"] * (2 if mode == "apply" else 1)
    )
    assert service.owner is case.owner and service.binding is original_binding
    assert service.runtime_admission is None and api.service is service
    assert _review_fix_rows(case.owner) == before
    assert (case.config.control_root / STATE_FILENAME).read_bytes() == state_before
    assert api.inspect(case.operation).state == (
        "ABSENT_LOCAL" if mode == "prepare" else "PREPARED_OFFLINE"
    )
    if mode == "prepare":
        retried = api.prepare(case.review.review_id, case.action, case.operation)
        assert api.prepare(case.review.review_id, case.action, case.operation) == retried
    else:
        retried = getattr(api, mode + "_offline")(
            case.operation, expected_intent_sha256=reference.intent_sha256
        )
        assert (
            getattr(api, mode + "_offline")(
                case.operation, expected_intent_sha256=reference.intent_sha256
            )
            == retried
        )
    after = _review_fix_rows(case.owner)
    assert len(after["facts"]) == int(mode == "apply")
    assert len(after["decisions"]) == int(mode == "apply")
    assert len(after["finalization_intents"]) == 1
    assert len(after["finalization_outcomes"]) == int(mode != "prepare")


@pytest.mark.parametrize(
    "entry",
    ["inspect", "material", "export_content_v1", "prepare", "apply_offline", "cancel_offline"],
)
def test_review_fix_six_prepared_entries_pin_service_and_identity_through_actual_close(
    review_fix_offline_case, monkeypatch, entry
):
    from finance_core.instance_admission import sessions

    case = review_fix_offline_case
    api, service = case.api, case.service
    reference = api.prepare(case.review.review_id, case.action, case.operation)
    before = _review_fix_rows(case.owner)
    original_binding = service.binding
    redirected = LedgerService(
        case.owner, case.port, replace(original_binding, environment="single-host-real")
    )
    original_verify = case.authenticator.verify
    original_close = sessions._close_sqlite_handle
    observed = []

    def attempt_mutations(stage):
        changes = (
            (service, "binding", redirected.binding),
            (service, "owner", None),
            (service, "runtime_admission", object()),
        )
        for target, field, value in changes:
            try:
                setattr(target, field, value)
            except LifecycleError:
                outcome = "REFUSED"
            else:
                outcome = "UNEXPECTED_MUTATION"
            observed.append((stage, field, outcome))
        # Redirecting the public pointer is allowed, but the current operation
        # must continue using its captured original service through actual close.
        api.service = redirected

    def verify_with_mutation(**kwargs):
        original_verify(**kwargs)
        attempt_mutations("authenticator")

    def close_with_mutation(conn):
        # Record instead of asserting from actual close: an assertion here
        # would itself create an unrelated UNKNOWN connection lifetime.
        attempt_mutations("actual-close")
        observed.append(
            (
                "actual-close",
                "connection-owner",
                "ORIGINAL" if case.owner._active.connection is conn else "WRONG_OWNER",
            )
        )
        original_close(conn)

    with monkeypatch.context() as callbacks:
        callbacks.setattr(case.authenticator, "verify", verify_with_mutation)
        callbacks.setattr(sessions, "_close_sqlite_handle", close_with_mutation)
        if entry == "inspect":
            assert api.inspect(case.operation).state == "PREPARED_OFFLINE"
        elif entry == "prepare":
            assert api.prepare(case.review.review_id, case.action, case.operation) == reference
        else:
            result = getattr(api, entry)(
                case.operation, expected_intent_sha256=reference.intent_sha256
            )
            if entry == "material":
                assert result
            elif entry == "export_content_v1":
                assert result.anchor.intent_sha256 == reference.intent_sha256
            elif entry == "apply_offline":
                assert result.status == "LOCAL_POSTED"
            else:
                assert result.state == "CANCELLED_OFFLINE"
    assert {stage for stage, _, _ in observed} == {"authenticator", "actual-close"}
    assert all(
        outcome == ("ORIGINAL" if field == "connection-owner" else "REFUSED")
        for _, field, outcome in observed
    )
    assert api.service is redirected
    assert service.owner is case.owner and service.binding is original_binding
    assert service.runtime_admission is None
    if entry not in {"apply_offline", "cancel_offline"}:
        assert _review_fix_rows(case.owner) == before
    # The redirected pointer applies only to a subsequent call. Its external
    # scope must be refused until the original service is explicitly restored.
    with pytest.raises(LifecycleError, match="offline-synthetic"):
        api.inspect(case.operation)
    api.service = service
    assert api.inspect(case.operation).state in {
        "PREPARED_OFFLINE",
        "APPLIED_OFFLINE",
        "CANCELLED_OFFLINE",
    }


def test_review_fix_real_fork_refuses_before_inherited_locked_mutex(runtime_config, tmp_path):
    import os
    import select
    import signal
    import threading

    if not hasattr(os, "fork"):
        pytest.skip("Actual fork requires the supported POSIX host")
    config, approvals = runtime_config
    binding, admission = _admit(config, approvals)
    with acquire_production_owner(config) as owner:
        service = LedgerService(owner, Port(), binding, runtime_admission=admission)
        before = _review_fix_rows(owner)
        retained, release = threading.Event(), threading.Event()

        def retain_parent_lock():
            with service._runtime_scope_lock:
                retained.set()
                release.wait(10)

        holder = threading.Thread(target=retain_parent_lock)
        holder.start()
        assert retained.wait(5)
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            signal.signal(signal.SIGALRM, lambda *_: os._exit(91))
            signal.alarm(2)
            outcomes = {}
            try:
                operations = [
                    ("capture", lambda: service.capture("unused", "fork-locked")),
                    ("expected", lambda: service.expected_invocation("unused", 0)),
                    (
                        "snapshot",
                        lambda: service.seal_snapshot(tmp_path / "child-must-not-exist.sqlite"),
                    ),
                ]
                for field in ("owner", "binding", "runtime_admission"):
                    operations.extend(
                        (
                            ("set-" + field, lambda field=field: setattr(service, field, None)),
                            ("delete-" + field, lambda field=field: delattr(service, field)),
                        )
                    )
                for label, operation in operations:
                    try:
                        operation()
                    except InstanceAdmissionError:
                        outcomes[label] = "REFUSED_BEFORE_LOCK"
                    else:
                        outcomes[label] = "UNEXPECTED_SUCCESS"
                os.write(write_fd, json.dumps(outcomes).encode())
                os._exit(0)
            except BaseException:
                os._exit(92)
        os.close(write_fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 5)
            if not readable:
                os.kill(child, signal.SIGKILL)
            assert readable, "Forked child waited on an inherited mutex"
            payload = os.read(read_fd, 4096)
        finally:
            os.close(read_fd)
            _, status = os.waitpid(child, 0)
            release.set()
            holder.join(timeout=5)
        assert os.waitstatus_to_exitcode(status) == 0
        outcomes = json.loads(payload)
        assert len(outcomes) == 9
        assert set(outcomes.values()) == {"REFUSED_BEFORE_LOCK"}
        assert not holder.is_alive()
        assert not (tmp_path / "child-must-not-exist.sqlite").exists()
        assert service.owner is owner and service.binding is binding
        assert service.runtime_admission is admission
        assert service._active_runtime_scopes == 0
        owner.assert_valid()
        assert _review_fix_rows(owner) == before
