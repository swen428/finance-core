"""Round 3 recovery regressions through real synthetic owners and held profiles."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port

from finance_core.instance_admission import (
    PENDING_FILENAME,
    InstanceAdmissionError,
    acquire_production_owner,
    admit_production_instance,
    authority,
    fence_production_instance,
    inspect_instance_admission,
    prepare_production_profile,
    resume_authority_transition,
)
from finance_core.instance_admission.authority import _logical_database_digest
from finance_core.lifecycle_v2 import LedgerService, LifecycleError, RuntimeBinding, SnapshotCut
from finance_core.lifecycle_v2 import recovery as recovery_module
from finance_core.lifecycle_v2.contracts import canonical, cut_ref
from finance_core.lifecycle_v2.recovery import (
    OldWriterIsolationReceipt,
    RecoveryAuthority,
    designate_restore_successor,
    prepare_restored_profile,
    promote_with_isolation_receipt,
    resume_held_restore,
    resume_restore_designation,
    resume_restore_prepare,
    resume_restore_promotion,
    verify_held_profile_evidence,
)


@pytest.fixture
def original(tmp_path, monkeypatch, request):
    from finance_core.instance_admission.schema_contract import ApplicationSchemaContract

    schema = (
        ApplicationSchemaContract(
            "manual-restore-evidence-tests",
            ("CREATE TABLE manual_app_evidence (reference TEXT PRIMARY KEY)",),
        )
        if getattr(request, "param", False)
        else None
    )
    config = synthetic_config(tmp_path, monkeypatch, application_schema=schema)
    prepare_production_profile(
        config, operation_id="prepare-original", approval_ref="approved"
    ).close()
    admit_production_instance(config, operation_id="admit-original", approval_ref="approved")
    owner = acquire_production_owner(config)
    port = Port()
    service = LedgerService(
        owner,
        port,
        RuntimeBinding(config.instance_id, "old-generation", "user", "chat"),
        clock=lambda: 1792800000,
    )
    yield config, owner, service, port
    if owner._lease.fd >= 0:
        owner.close()
    config.close()


def _cut(service: LedgerService, tmp_path: Path):
    path = tmp_path / "sealed-v3.sqlite"
    return service.seal_snapshot(path), path


def _target(tmp_path, monkeypatch, cut, path, *, name="target", operation="prepare-target"):
    root = tmp_path / name
    root.mkdir(mode=0o700)
    config = synthetic_config(root, monkeypatch, instance_id=name)
    binding = RuntimeBinding(name, "new-generation", "user", "chat")
    held = prepare_restored_profile(
        config,
        path,
        cut,
        new_binding=binding,
        operation_id=operation,
        approval_ref="approved",
    )
    return config, binding, held


def _fence(config, owner):
    fence_production_instance(config, operation_id="fence-original", approval_ref="approved")
    owner.close()


def test_v3_cut_and_receipt_roundtrip_rejects_historical_or_changed_target(
    original,
    tmp_path,
    monkeypatch,
):
    old, owner, service, _ = original
    cut, path = _cut(service, tmp_path)
    values = json.loads(json.dumps(asdict(cut)))
    assert SnapshotCut.from_dict(values) == cut
    assert cut_ref(cut)["logical_database_sha256"] == cut.logical_database_sha256
    with pytest.raises(LifecycleError):
        SnapshotCut.from_dict(
            {key: value for key, value in values.items() if key != "logical_database_sha256"}
        )
    with pytest.raises(LifecycleError):
        SnapshotCut.from_dict({**values, "commit_seq": True})
    with pytest.raises(LifecycleError):
        replace(cut, facts=(("z", 1, "0" * 64), ("a", 1, "0" * 64)))
    _fence(old, owner)
    first, binding, held = _target(tmp_path, monkeypatch, cut, path)
    second, _, other = _target(
        tmp_path, monkeypatch, cut, path, name="second-target", operation="prepare-second"
    )
    recovery = RecoveryAuthority("round3-synthetic", b"r" * 32)
    first_receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate-first", approval_ref="approved"
    )
    assert (
        recovery.issue_old_writer_isolation(
            old, held, operation_id="designate-first", approval_ref="approved"
        )
        == first_receipt
    )
    serialized = json.loads(json.dumps(asdict(first_receipt)))
    assert recovery.verify_old_writer_isolation(OldWriterIsolationReceipt(**serialized), held)
    historical = json.loads(first_receipt.material_json)
    historical["schema"] = "old-writer-isolation-v2"
    historical_json = canonical(historical)
    historical_receipt = OldWriterIsolationReceipt(
        historical_json,
        hmac.new(recovery.key, historical_json.encode(), hashlib.sha256).hexdigest(),
    )
    with pytest.raises(LifecycleError, match="exact held target"):
        recovery.verify_old_writer_isolation(historical_receipt, held)
    with pytest.raises(LifecycleError, match="already designated"):
        designate_restore_successor(
            old, other, operation_id="designate-second", approval_ref="approved"
        )
    with pytest.raises(LifecycleError, match="target"):
        recovery.verify_old_writer_isolation(first_receipt, other)
    monkeypatch.setenv(
        "FINANCE_RUNTIME_ROOT", str(Path(first._binding["profile_root"]) / "runtime")
    )
    assert inspect_instance_admission(first)["state"] == "RESTORE_HELD"
    assert binding == held.new_binding
    first.close()
    second.close()


def test_receipt_promotion_rechecks_held_content_and_completed_return(
    original,
    tmp_path,
    monkeypatch,
):
    old, owner, service, port = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    recovery = RecoveryAuthority("round3-synthetic", b"r" * 32)
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate", approval_ref="approved"
    )
    verify_held_profile_evidence(held, port, binding)
    restored = resume_held_restore(
        new,
        SnapshotCut.from_dict(json.loads(json.dumps(asdict(cut)))),
        OldWriterIsolationReceipt(**json.loads(json.dumps(asdict(receipt)))),
        recovery,
    )
    assert restored == held
    assert inspect_instance_admission(new)["state"] == "RESTORE_HELD"
    result = promote_with_isolation_receipt(
        restored,
        receipt,
        recovery,
        operation_id="promote",
        approval_ref="approved",
        port=port,
        binding=binding,
    )
    assert result["state"] == "ACTIVE"
    first_version = authority._state(new)["version"]
    assert (
        promote_with_isolation_receipt(
            restored,
            receipt,
            recovery,
            operation_id="promote",
            approval_ref="approved",
            port=port,
            binding=binding,
        )["state"]
        == "ACTIVE"
    )
    assert authority._state(new)["version"] == first_version
    fence_production_instance(new, operation_id="fence-restored", approval_ref="approved")
    assert (
        promote_with_isolation_receipt(
            restored,
            receipt,
            recovery,
            operation_id="promote",
            approval_ref="approved",
            port=port,
            binding=binding,
        )["state"]
        == "FENCED"
    )
    assert authority._state(new)["version"] == first_version + 1
    new.close()


def test_held_content_change_after_diagnostic_cannot_be_promoted(
    original,
    tmp_path,
    monkeypatch,
):
    old, owner, service, port = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    recovery = RecoveryAuthority("round3-synthetic", b"r" * 32)
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate", approval_ref="approved"
    )
    verify_held_profile_evidence(held, port, binding)
    database = Path(new._binding["profile_root"]) / "runtime/database/finance.db"
    offline = sqlite3.connect(database)
    try:
        offline.execute(
            "INSERT INTO participants(public_id,display_name) VALUES ('offline','Tampered')"
        )
        offline.commit()
    finally:
        offline.close()
    with pytest.raises(LifecycleError, match="database content"):
        promote_with_isolation_receipt(
            held,
            receipt,
            recovery,
            operation_id="promote",
            approval_ref="approved",
            port=port,
            binding=binding,
        )
    assert inspect_instance_admission(new)["state"] == "RESTORE_HELD"
    new.close()


def test_pending_prepare_requires_dedicated_content_recheck(
    original,
    tmp_path,
    monkeypatch,
):
    _, _, service, _ = original
    cut, path = _cut(service, tmp_path)
    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    config = synthetic_config(target_root, monkeypatch, instance_id="target")
    binding = RuntimeBinding("target", "new-generation", "user", "chat")
    with patch.object(authority, "_publish", side_effect=OSError("synthetic prepare publish")):
        with pytest.raises(OSError, match="synthetic prepare publish"):
            prepare_restored_profile(
                config,
                path,
                cut,
                new_binding=binding,
                operation_id="prepare-target",
                approval_ref="approved",
            )
    assert (config.control_root / PENDING_FILENAME).exists()
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(config, operation_id="prepare-target", approval_ref="approved")
    held = resume_restore_prepare(
        config, cut, binding, operation_id="prepare-target", approval_ref="approved"
    )
    assert held.status == "RESTORE_HELD"
    assert inspect_instance_admission(config)["state"] == "RESTORE_HELD"
    config.close()


def test_pending_designation_and_admission_each_reverify_before_publish(
    original,
    tmp_path,
    monkeypatch,
):
    old, owner, service, port = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    recovery = RecoveryAuthority("round3-synthetic", b"r" * 32)
    with patch.object(
        recovery_module, "_publish", side_effect=OSError("synthetic designate publish")
    ):
        with pytest.raises(OSError, match="synthetic designate publish"):
            designate_restore_successor(
                old, held, operation_id="designate", approval_ref="approved"
            )
    assert (old.control_root / PENDING_FILENAME).exists()
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(old, operation_id="designate", approval_ref="approved")
    designation = resume_restore_designation(
        old, held, operation_id="designate", approval_ref="approved"
    )
    assert designation.material["target"]["binding_sha256"] == new.binding_sha256
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate", approval_ref="approved"
    )
    with patch.object(recovery_module, "_publish", side_effect=OSError("synthetic admit publish")):
        with pytest.raises(OSError, match="synthetic admit publish"):
            promote_with_isolation_receipt(
                held,
                receipt,
                recovery,
                operation_id="promote",
                approval_ref="approved",
                port=port,
                binding=binding,
            )
    assert (new.control_root / PENDING_FILENAME).exists()
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(new, operation_id="promote", approval_ref="approved")
    assert (
        resume_restore_promotion(
            held,
            operation_id="promote",
            approval_ref="approved",
            port=port,
            binding=binding,
            receipt=receipt,
            recovery_authority=recovery,
        )["state"]
        == "ACTIVE"
    )
    new.close()


def test_state_replaced_but_pending_remains_denied_and_resume_rechecks_content(
    original,
    tmp_path,
    monkeypatch,
):
    old, owner, service, port = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    recovery_authority = RecoveryAuthority("round3-synthetic", b"r" * 32)
    receipt = recovery_authority.issue_old_writer_isolation(
        old, held, operation_id="designate", approval_ref="approved"
    )
    original_unlink = authority.os.unlink

    def fail_pending_delete(path, *args, **kwargs):
        if path == PENDING_FILENAME:
            raise OSError("synthetic pending removal failure")
        return original_unlink(path, *args, **kwargs)

    with patch.object(authority.os, "unlink", side_effect=fail_pending_delete):
        with pytest.raises(OSError, match="pending removal"):
            promote_with_isolation_receipt(
                held,
                receipt,
                recovery_authority,
                operation_id="promote",
                approval_ref="approved",
                port=port,
                binding=binding,
            )
    assert (new.control_root / PENDING_FILENAME).exists()
    assert (
        authority._read(new, authority.STATE_FILENAME)["instances"][new.binding_sha256]["state"]
        == "ACTIVE"
    )
    with pytest.raises(InstanceAdmissionError, match="pending"):
        inspect_instance_admission(new)
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(new, operation_id="promote", approval_ref="approved")
    database = Path(new._binding["profile_root"]) / "runtime/database/finance.db"
    offline = sqlite3.connect(database)
    try:
        offline.execute(
            "INSERT INTO participants(public_id,display_name) VALUES ('later','Changed')"
        )
        offline.commit()
    finally:
        offline.close()
    with pytest.raises(LifecycleError, match="database content"):
        resume_restore_promotion(
            held,
            operation_id="promote",
            approval_ref="approved",
            port=port,
            binding=binding,
            receipt=receipt,
            recovery_authority=recovery_authority,
        )
    assert (new.control_root / PENDING_FILENAME).exists()
    new.close()


def test_close_unknown_keeps_pending_and_leases_in_child_process(tmp_path):
    child_root = tmp_path / "close-child"
    script = r"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "tests"))
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port
from finance_core.instance_admission import (
    PENDING_FILENAME, acquire_production_owner, admit_production_instance,
    fence_production_instance, prepare_production_profile,
)
from finance_core.instance_admission import authority
from finance_core.lifecycle_v2 import LedgerService, RuntimeBinding
from finance_core.lifecycle_v2 import recovery
class Environment:
    def setenv(self, key, value):
        os.environ[key] = value
root = Path(sys.argv[1])
root.mkdir(mode=0o700)
config = synthetic_config(root, Environment())
prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
admit_production_instance(config, operation_id="admit", approval_ref="approved")
owner = acquire_production_owner(config)
service = LedgerService(owner, Port(), RuntimeBinding(config.instance_id, "old", "user", "chat"))
cut = service.seal_snapshot(root / "sealed.sqlite")
fence_production_instance(config, operation_id="fence", approval_ref="approved")
owner.close()
newroot = root / "new"
newroot.mkdir(mode=0o700)
new = synthetic_config(newroot, Environment(), instance_id="new")
held = recovery.prepare_restored_profile(new, root / "sealed.sqlite", cut,
    new_binding=RuntimeBinding("new", "new", "user", "chat"),
    operation_id="held", approval_ref="approved")
with patch.object(recovery, "_close_sqlite_handle", side_effect=OSError("synthetic close unknown")):
    try:
        recovery.designate_restore_successor(config, held,
            operation_id="designate", approval_ref="approved")
    except Exception as exc:
        assert "actual close unknown" in str(exc)
    else:
        raise AssertionError("Unknown close did not fail")
assert (config.control_root / PENDING_FILENAME).exists()
try:
    authority._state(config)
except Exception as exc:
    assert "pending" in str(exc)
else:
    raise AssertionError("Pending authority became usable")
print("CLOSE_UNKNOWN_HELD")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(child_root)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLOSE_UNKNOWN_HELD" in result.stdout


@pytest.mark.parametrize("later_legacy_write", [False, True])
def test_mixed_legacy_lifecycle_cut_and_later_legacy_invalidation(
    tmp_path, monkeypatch, later_legacy_write
):
    """A real PostingService finalization, not direct SQL, expires the old cut."""
    from dataclasses import replace as replace_binding

    import test_application_posting_recovery_v1 as posting_fixtures
    from instance_admission_business_support_v1 import (
        _production_config,
        production_fixture_persistence,
        run_production_fixture,
    )

    from finance_core.instance_admission import production_write_operation

    root = tmp_path / "business-old"
    root.mkdir(mode=0o700)
    old, base = _production_config(root)
    with patch.dict(os.environ, {"FINANCE_RUNTIME_ROOT": str(base / "runtime")}):
        prepare_production_profile(
            old, operation_id="prepare-business", approval_ref="approved"
        ).close()
        admit_production_instance(old, operation_id="admit-business", approval_ref="approved")
        owner = acquire_production_owner(old)
        business_binding = replace_binding(posting_fixtures.BINDING, instance_id=old.instance_id)
        with patch.object(posting_fixtures, "BINDING", business_binding):

            def post(decision_id):
                with production_write_operation(owner) as conn:
                    with production_fixture_persistence(conn, posting_fixtures):
                        posting, review, _, _, decision, _ = run_production_fixture(
                            conn, posting_fixtures._prepare_text_subject, decision_id
                        )
                        result = posting.submit_post(review.review_id, decision)
                        assert result.transaction_public_id

            post("round3-before")
            service = LedgerService(
                owner, Port(), RuntimeBinding(old.instance_id, "old-generation", "user", "chat")
            )
            from test_lifecycle_v2 import ready

            _, _, review = ready(service)
            fact = service.confirm(review.review_id, "mixed-action", "mixed-confirm")
            cut = service.seal_snapshot(tmp_path / "legacy-before.sqlite")
            if later_legacy_write:
                post("round3-later")
        fence_production_instance(old, operation_id="fence-business", approval_ref="approved")
        owner.close()
    next_root = tmp_path / "business-new"
    next_root.mkdir(mode=0o700)
    new, next_base = _production_config(next_root, instance_id="business-new")
    with patch.dict(os.environ, {"FINANCE_RUNTIME_ROOT": str(next_base / "runtime")}):
        held = prepare_restored_profile(
            new,
            tmp_path / "legacy-before.sqlite",
            cut,
            new_binding=RuntimeBinding(new.instance_id, "new-generation", "user", "chat"),
            operation_id="held-business",
            approval_ref="approved",
        )
        recovery = RecoveryAuthority("mixed-synthetic", b"r" * 32)
        if later_legacy_write:
            with pytest.raises(LifecycleError, match="database content"):
                designate_restore_successor(
                    old, held, operation_id="designate-business", approval_ref="approved"
                )
            with pytest.raises(LifecycleError, match="database content"):
                recovery.issue_old_writer_isolation(
                    old, held, operation_id="sign-business", approval_ref="approved"
                )
        else:
            receipt = recovery.issue_old_writer_isolation(
                old, held, operation_id="sign-business", approval_ref="approved"
            )
            assert (
                promote_with_isolation_receipt(
                    held,
                    receipt,
                    recovery,
                    operation_id="promote-business",
                    approval_ref="approved",
                    port=service.port,
                    binding=held.new_binding,
                )["state"]
                == "ACTIVE"
            )
            restored_owner = acquire_production_owner(new)
            try:
                from finance_core.instance_admission import production_read_operation

                with production_read_operation(restored_owner) as conn:
                    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
                restored_service = LedgerService(restored_owner, service.port, held.new_binding)
                assert restored_service.reader().get(fact.record_id)["values"]["amount"] == "12.00"
                assert restored_service.reader().full_audit()["fact_version_count"] == 1
            finally:
                restored_owner.close()
        assert inspect_instance_admission(old)["state"] == "FENCED"
        assert inspect_instance_admission(new)["state"] == (
            "RESTORE_HELD" if later_legacy_write else "ACTIVE"
        )
    new.close()
    old.close()


def test_logical_digest_covers_storage_types_hidden_rowid_and_sequence():
    """Generic existing tables are included, even without an l2 fact change."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE app_values(value)")
        conn.execute("INSERT INTO app_values(value) VALUES(X'6162')")
        conn.execute("INSERT INTO app_values(value) VALUES(1.25)")
        conn.execute("CREATE TABLE app_duplicates(value TEXT)")
        conn.executemany("INSERT INTO app_duplicates(value) VALUES(?)", [("same",), ("same",)])
        conn.execute("CREATE TABLE app_auto(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
        conn.execute("INSERT INTO app_auto(value) VALUES('one')")
        conn.commit()
        original = _logical_database_digest(conn)
        mutations = (
            "UPDATE app_values SET value='ab' WHERE typeof(value)='blob'",
            "UPDATE app_values SET value=1.5 WHERE typeof(value)='real'",
            "DELETE FROM app_duplicates WHERE rowid=1",
            "UPDATE sqlite_sequence SET seq=2 WHERE name='app_auto'",
        )
        for sql in mutations:
            conn.execute("SAVEPOINT probe")
            conn.execute(sql)
            if sql.startswith("DELETE"):
                conn.execute("INSERT INTO app_duplicates(value) VALUES('same')")
            assert _logical_database_digest(conn) != original
            conn.execute("ROLLBACK TO probe")
            conn.execute("RELEASE probe")
        assert _logical_database_digest(conn) == original
    finally:
        conn.close()


@pytest.mark.parametrize("route", ["live", "receipt"])
def test_same_control_lineage_recovery_then_fresh_writes(original, tmp_path, monkeypatch, route):
    from test_lifecycle_v2 import TEXT, VerifiedSource, ready, response

    from finance_core.lifecycle_v2.recovery import promote_restored_profile

    old, owner, service, port = original
    _, _, old_review = ready(service)
    old_fact = service.confirm(old_review.review_id, "old-action", "old-confirm")
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    root = tmp_path / "same-lineage"
    root.mkdir(mode=0o700)
    new = synthetic_config(
        root, monkeypatch, instance_id="same-lineage", control_root=old.control_root
    )
    binding = RuntimeBinding(new.instance_id, "new-generation", "user", "chat")
    held = prepare_restored_profile(
        new,
        path,
        cut,
        new_binding=binding,
        operation_id="prepare-successor",
        approval_ref="approved",
    )
    arguments = dict(
        designation_operation_id="designate-successor",
        designation_approval_ref="approved",
        operation_id="admit-successor",
        approval_ref="approved",
        port=port,
        binding=binding,
    )
    if route == "live":

        def promote():
            return promote_restored_profile(held, old, **arguments)
    else:
        recovery = RecoveryAuthority("same-lineage", b"r" * 32)
        receipt = recovery.issue_old_writer_isolation(
            old, held, operation_id="designate-successor", approval_ref="approved"
        )

        def promote():
            return promote_with_isolation_receipt(
                held,
                receipt,
                recovery,
                operation_id="admit-successor",
                approval_ref="approved",
                port=port,
                binding=binding,
            )

    assert promote()["state"] == "ACTIVE"
    version = authority._state(new)["version"]
    new_owner = acquire_production_owner(new)
    try:
        restored_service = LedgerService(new_owner, port, binding, clock=lambda: 1792800000)
        assert restored_service.reader().get(old_fact.record_id)["values"]["amount"] == "12.00"
        with pytest.raises(LifecycleError):
            restored_service.confirm(old_review.review_id, "old-action", "old-confirm")
        port.sources["fresh-source"] = VerifiedSource(
            "fresh-source",
            "user",
            "chat",
            "fresh-message",
            TEXT,
            hashlib.sha256(TEXT.encode()).hexdigest(),
            1792800000,
        )
        source = restored_service.capture("fresh-source", "fresh-capture")
        proposal = restored_service.propose(
            source.source_id, "fresh-observation", 0, "fresh-propose"
        )
        review = restored_service.prepare_review(proposal.proposal_id, proposal.version)
        restored_service.record_delivery(review.review_id, "fresh-delivery")
        posted = restored_service.confirm(review.review_id, "fresh-action", "fresh-confirm")
        text = "change amount to SGD 15.00; reason: correction"
        port.sources["fresh-edit"] = VerifiedSource(
            "fresh-edit",
            "user",
            "chat",
            "edit-message",
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            1792800000,
        )
        port.raw = response(text, "reply", {"amount": "15.00", "currency": "SGD"})
        correction = restored_service.prepare_correction(
            posted.record_id, 1, "edit-observation", "edit-propose", "fresh-edit", "correction"
        )
        restored_service.record_delivery(correction.review_id, "edit-delivery")
        corrected = restored_service.confirm(correction.review_id, "edit-action", "edit-confirm")
        assert corrected.version == 2
        assert restored_service.reader().full_audit()["fact_version_count"] == 3
    finally:
        new_owner.close()
    assert promote()["state"] == "ACTIVE"
    assert authority._state(new)["version"] == version
    new_owner = acquire_production_owner(new)
    try:
        assert (
            LedgerService(new_owner, port, binding)
            .reader()
            .get(posted.record_id)["values"]["amount"]
            == "15.00"
        )
    finally:
        new_owner.close()
    new.close()


def test_forged_recovery_history_rejected_even_with_rehashed_entry(original, tmp_path, monkeypatch):
    old, owner, service, _ = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    new, _, held = _target(tmp_path, monkeypatch, cut, path)
    designate_restore_successor(old, held, operation_id="designate", approval_ref="approved")
    state = authority._state(old)
    for mutation in (
        "missing-target",
        "changed-target-binding",
        "wrong-context-digest",
        "extra-field",
    ):
        forged = json.loads(json.dumps(state))
        entry = forged["history"][-1]
        context = entry["recovery_context"]
        if mutation == "missing-target":
            del context["designation"]["target"]
        elif mutation == "changed-target-binding":
            context["designation"]["target"]["binding_sha256"] = "0" * 64
        elif mutation == "extra-field":
            context["unrecognized"] = True
        if mutation != "wrong-context-digest":
            entry["context_digest"] = authority._digest(context)
        else:
            entry["context_digest"] = "0" * 64
        entry["entry_sha256"] = authority._digest(
            {k: v for k, v in entry.items() if k != "entry_sha256"}
        )
        with pytest.raises(InstanceAdmissionError):
            authority._validate_state(forged)
    assert authority._validate_state(state) == state
    new.close()


def test_json_receipt_reconstructs_exact_target_in_restarted_process(
    original, tmp_path, monkeypatch
):
    old, owner, service, _ = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    recovery = RecoveryAuthority("round3-synthetic", b"r" * 32)
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate", approval_ref="approved"
    )
    material = dict(
        cut=asdict(cut), receipt=asdict(receipt), binding=asdict(binding), config=new._binding
    )
    payload = tmp_path / "restart-material.json"
    payload.write_text(json.dumps(material))
    payload.chmod(0o600)
    new.close()
    # Remove access to both old authority and old database before process startup.
    old_database = Path(old._binding["profile_root"]) / "runtime/database/finance.db"
    old_database.rename(old_database.with_suffix(".unavailable"))
    old_control = old.control_root
    old.close()
    old_control.rename(old_control.with_name("old-control-unavailable"))
    script = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_lifecycle_v2 import Port
from finance_core.instance_admission import (
    TrustedInstanceConfig, acquire_production_owner, inspect_instance_admission,
)
from finance_core.lifecycle_v2 import LedgerService, SnapshotCut, RuntimeBinding
from finance_core.lifecycle_v2.recovery import (
    RecoveryAuthority, OldWriterIsolationReceipt, resume_held_restore,
    promote_with_isolation_receipt,
)
m = json.loads(Path(sys.argv[1]).read_text())
b = m["config"]
profile = Path(b["profile_root"])
support = profile.parents[2] if b["layout"] == "mac" else profile.parents[1]
os.environ["FINANCE_RUNTIME_ROOT"] = str(profile / "runtime")
c = TrustedInstanceConfig(Path(b["control_root"]), support, b["profile_id"], layout=b["layout"],
    instance_id=b["instance_id"], config_identity=b["config_identity"],
    core_identity=b["core_identity"],
    approval_verifier=lambda ref, action, binding: ref == "approved")
cut = SnapshotCut.from_dict(m["cut"])
r = OldWriterIsolationReceipt(**m["receipt"])
a = RecoveryAuthority("round3-synthetic", b"r" * 32)
held = resume_held_restore(c, cut, r, a)
assert inspect_instance_admission(c)["state"] == "RESTORE_HELD"
binding = RuntimeBinding(**m["binding"])
assert held.new_binding == binding
assert promote_with_isolation_receipt(held, r, a, operation_id="promote-restarted",
    approval_ref="approved", port=Port(), binding=binding)["state"] == "ACTIVE"
o = acquire_production_owner(c)
assert LedgerService(o, Port(), binding).reader().full_audit()["fact_version_count"] == 0
o.close()
c.close()
print("RESTARTED_EXACT_TARGET_ACTIVE")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(payload)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTARTED_EXACT_TARGET_ACTIVE" in result.stdout


@pytest.mark.parametrize("route", ["live", "receipt"])
@pytest.mark.parametrize("shared_control", [False, True], ids=["separate-root", "shared-root"])
@pytest.mark.parametrize("interruption", ["before-publish", "pending-unlink"])
def test_pending_promotion_resume_authenticates_exact_request_and_rechecks_target(
    original, tmp_path, monkeypatch, route, shared_control, interruption
):
    from finance_core.lifecycle_v2.recovery import promote_restored_profile

    old, owner, service, port = original
    service.capture("source1", "capture-before-recovery")
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    root = tmp_path / "resume-target"
    root.mkdir(mode=0o700)
    new = synthetic_config(
        root,
        monkeypatch,
        instance_id="resume-target",
        control_root=old.control_root if shared_control else None,
    )
    binding = RuntimeBinding(new.instance_id, "new-generation", "user", "chat")
    held = prepare_restored_profile(
        new, path, cut, new_binding=binding, operation_id="prepare-resume", approval_ref="approved"
    )
    recovery = RecoveryAuthority("pending-resume", b"r" * 32)
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate-resume", approval_ref="approved"
    )
    common = dict(
        operation_id="promote-resume", approval_ref="approved", port=port, binding=binding
    )
    route_args = (
        dict(old_config=old)
        if route == "live"
        else dict(receipt=receipt, recovery_authority=recovery)
    )

    def promote():
        if route == "live":
            return promote_restored_profile(
                held,
                old,
                designation_operation_id="designate-resume",
                designation_approval_ref="approved",
                **common,
            )
        return promote_with_isolation_receipt(held, receipt, recovery, **common)

    original_unlink = authority.os.unlink

    def fail_pending_delete(filename, *args, **kwargs):
        if filename == PENDING_FILENAME:
            raise OSError("synthetic interrupted promotion")
        return original_unlink(filename, *args, **kwargs)

    if interruption == "before-publish":
        failure = patch.object(
            recovery_module, "_publish", side_effect=OSError("synthetic interrupted promotion")
        )
    else:
        failure = patch.object(authority.os, "unlink", side_effect=fail_pending_delete)
    with failure, pytest.raises(OSError, match="synthetic interrupted promotion"):
        promote()
    pending = new.control_root / PENDING_FILENAME
    pending_bytes = pending.read_bytes()
    published = authority._read(new, authority.STATE_FILENAME)
    expected = "RESTORE_HELD" if interruption == "before-publish" else "ACTIVE"
    assert published["instances"][new.binding_sha256]["state"] == expected
    with pytest.raises(InstanceAdmissionError, match="pending"):
        acquire_production_owner(new)
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(new, operation_id="promote-resume", approval_ref="approved")

    def resume(restored=held, **changed):
        return resume_restore_promotion(restored, **(common | changed), **route_args)

    for changed in (
        dict(operation_id="wrong-operation"),
        dict(approval_ref="wrong-approval"),
        dict(binding=replace(binding, generation="wrong-generation")),
    ):
        with pytest.raises((LifecycleError, InstanceAdmissionError)):
            resume(**changed)
        assert pending.read_bytes() == pending_bytes
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        resume(replace(held, cut=replace(cut, logical_database_sha256="0" * 64)))
    assert pending.read_bytes() == pending_bytes
    altered = json.loads(pending_bytes)
    altered["context"]["target_sha256"] = "0" * 64
    pending.write_text(canonical(altered))
    altered_bytes = pending.read_bytes()
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        resume()
    assert pending.read_bytes() == altered_bytes
    pending.write_bytes(pending_bytes)

    database = Path(new._binding["profile_root"]) / "runtime/database/finance.db"
    database_bytes = database.read_bytes()
    with sqlite3.connect(database) as offline:
        offline.execute(
            "INSERT INTO participants(public_id,display_name) VALUES ('changed','Changed')"
        )
    offline.close()
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        resume()
    assert pending.read_bytes() == pending_bytes
    # Restore synthetic bytes in place, preserving the original pinned inode.
    with database.open("r+b") as restored_database:
        restored_database.write(database_bytes)
        restored_database.truncate()
    saved = database.with_suffix(".original-inode")
    database.rename(saved)
    database.write_bytes(database_bytes)
    database.chmod(0o600)
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        resume()
    assert pending.read_bytes() == pending_bytes
    database.unlink()
    saved.rename(database)

    assert resume()["state"] == "ACTIVE"
    assert not pending.exists()
    state = authority._state(new)
    assert sum(entry["action"] == "restore_admit" for entry in state["history"]) == 1
    assert (
        sum(entry["action"] == "recovery_designate" for entry in authority._state(old)["history"])
        == 1
    )
    assert resume()["state"] == "ACTIVE"
    assert authority._state(new) == state
    fence_production_instance(new, operation_id="fence-after-resume", approval_ref="approved")
    fenced = authority._state(new)
    assert resume()["state"] == "FENCED"
    assert authority._state(new) == fenced
    new.close()


@pytest.mark.parametrize("route", ["live", "receipt"])
@pytest.mark.parametrize("shared_control", [False, True], ids=["separate-root", "shared-root"])
@pytest.mark.parametrize("interruption", ["before-publish", "pending-unlink", "completed"])
def test_public_restore_reconstruction_after_process_loss(
    original, tmp_path, monkeypatch, route, shared_control, interruption
):
    from finance_core.lifecycle_v2.recovery import promote_restored_profile

    old, owner, service, port = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    root = tmp_path / "cold-target"
    root.mkdir(mode=0o700)
    new = synthetic_config(
        root,
        monkeypatch,
        instance_id="cold-target",
        control_root=old.control_root if shared_control else None,
    )
    binding = RuntimeBinding(new.instance_id, "new-generation", "user", "chat")
    held = prepare_restored_profile(
        new, path, cut, new_binding=binding, operation_id="prepare-cold", approval_ref="approved"
    )
    recovery = RecoveryAuthority("cold-resume", b"r" * 32)
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate-cold", approval_ref="approved"
    )
    common = dict(operation_id="promote-cold", approval_ref="approved", port=port, binding=binding)

    def promote():
        if route == "live":
            return promote_restored_profile(
                held,
                old,
                designation_operation_id="designate-cold",
                designation_approval_ref="approved",
                **common,
            )
        return promote_with_isolation_receipt(held, receipt, recovery, **common)

    unlink = authority.os.unlink

    def fail_pending_delete(filename, *args, **kwargs):
        if filename == PENDING_FILENAME:
            raise OSError("synthetic cold interruption")
        return unlink(filename, *args, **kwargs)

    if interruption == "completed":
        assert promote()["state"] == "ACTIVE"
    else:
        failure = (
            patch.object(
                recovery_module, "_publish", side_effect=OSError("synthetic cold interruption")
            )
            if interruption == "before-publish"
            else patch.object(authority.os, "unlink", side_effect=fail_pending_delete)
        )
        with failure, pytest.raises(OSError, match="synthetic cold interruption"):
            promote()
    material = {
        "cut": asdict(cut),
        "receipt": asdict(receipt),
        "route": route,
        "old": old._binding,
        "new": new._binding,
        "old_locator": str(old.locator_root),
        "new_locator": str(new.locator_root),
        "expected_status": "ACTIVE" if interruption == "completed" else "RESTORE_ADMIT_PENDING",
        "approvals": [
            [entry["approval_ref"], entry["action"], entry["approval_digest"]]
            for state in (
                authority._read(old, authority.STATE_FILENAME),
                authority._read(new, PENDING_FILENAME)["after"]
                if (new.control_root / PENDING_FILENAME).exists()
                else authority._state(new),
            )
            for entry in state["history"]
        ],
    }
    payload = tmp_path / "cold-reconstruction.json"
    payload.write_text(json.dumps(material))
    payload.chmod(0o600)
    new.close()
    old.close()
    if route == "receipt" and not shared_control:
        old.control_root.rename(old.control_root.with_name("unavailable-original-control"))
        old_database = Path(old._binding["profile_root"]) / "runtime/database/finance.db"
        old_database.rename(old_database.with_suffix(".unavailable"))
    script = r"""
import json, os, sqlite3, sys
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_lifecycle_v2 import Port
from finance_core.instance_admission import (
    TrustedInstanceConfig, InstanceAdmissionError, PENDING_FILENAME, authority,
    acquire_production_owner, fence_production_instance,
)
from finance_core.lifecycle_v2 import LedgerService, LifecycleError, SnapshotCut
from finance_core.lifecycle_v2.recovery import (
    RecoveryAuthority, OldWriterIsolationReceipt, resume_held_restore,
    resume_restore_promotion,
)
m = json.loads(Path(sys.argv[1]).read_text())
phase = sys.argv[2] if len(sys.argv) > 2 else ""
os.environ.pop("FINANCE_RUNTIME_ROOT", None)
os.environ["FINANCE_RUNTIME_ROOT"] = str(Path(m["new"]["profile_root"]) / "runtime")
def config(binding, locator):
    return TrustedInstanceConfig(
        Path(binding["control_root"]), Path(locator), binding["profile_id"],
        layout=binding["layout"], instance_id=binding["instance_id"],
        config_identity=binding["config_identity"], core_identity=binding["core_identity"],
        approval_verifier=lambda ref, action, digest: (
            [ref, action, digest] in m["approvals"]
            if action in {"restore_prepare", "restore_admit", "recovery_designate"}
            else ref == "approved"
        ),
    )
new = config(m["new"], m["new_locator"])
old = config(m["old"], m["old_locator"]) if m["route"] == "live" else None
cut = SnapshotCut.from_dict(m["cut"])
r = OldWriterIsolationReceipt(**m["receipt"])
a = RecoveryAuthority("cold-resume", b"r" * 32)
route_args = dict(old_config=old) if old is not None else dict(receipt=r, recovery_authority=a)
state_bytes = (new.control_root / authority.STATE_FILENAME).read_bytes()
pending = new.control_root / PENDING_FILENAME
pending_bytes = pending.read_bytes() if pending.exists() else None
held = resume_held_restore(new, cut, **route_args)
expected_status = {"active": "ACTIVE", "fenced": "FENCED"}.get(phase, m["expected_status"])
assert held.status == expected_status
assert (new.control_root / authority.STATE_FILENAME).read_bytes() == state_bytes
assert (pending.read_bytes() if pending.exists() else None) == pending_bytes
for bad in (
    {}, dict(receipt=r), dict(recovery_authority=a),
    dict(receipt=r, recovery_authority=a, old_config=new),
):
    try:
        resume_held_restore(new, cut, **bad)
    except LifecycleError:
        pass
    else:
        raise AssertionError("Incomplete or mixed reconstruction route accepted")
def denied(call):
    try:
        call()
    except (LifecycleError, InstanceAdmissionError):
        pass
    else:
        raise AssertionError("Changed reconstruction metadata accepted")
wrong_config = config(dict(m["new"], config_identity="wrong-composition"), m["new_locator"])
denied(lambda: resume_held_restore(wrong_config, cut, **route_args))
wrong_config.close()
for bad_receipt, bad_authority in (
    (replace(r, signature="0" * 64), a),
    (r, RecoveryAuthority("wrong-namespace", b"r" * 32)),
    (r, RecoveryAuthority("cold-resume", b"x" * 32)),
):
    denied(lambda: resume_held_restore(new, cut, bad_receipt, bad_authority))
if old is not None:
    # A valid receipt for this target cannot replace a persisted live route.
    denied(lambda: resume_held_restore(new, cut, r, a))
database = Path(new._binding["profile_root"]) / "runtime/database/finance.db"
database_bytes = database.read_bytes()
saved = database.with_suffix(".saved-inode")
database.rename(saved)
database.write_bytes(database_bytes)
database.chmod(0o600)
denied(lambda: resume_held_restore(new, cut, **route_args))
database.unlink()
saved.rename(database)
# Tamper with durable preparation rather than only caller-supplied arguments.
metadata_path = (
    pending if pending_bytes is not None else new.control_root / authority.STATE_FILENAME
)
metadata_bytes = metadata_path.read_bytes()
for damage in ("generation", "principal_id", "client_id", "prepare-entry", "old-prepare"):
    changed = json.loads(metadata_bytes)
    history = changed["after"]["history"] if pending_bytes is not None else changed["history"]
    prepare = next(entry for entry in history if entry["action"] == "restore_prepare")
    if damage == "prepare-entry":
        prepare["entry_sha256"] = "0" * 64
    elif damage == "old-prepare":
        del prepare["recovery_context"]
    else:
        prepare["recovery_context"]["runtime_binding"][damage] = "wrong-identity"
    metadata_path.write_text(json.dumps(changed))
    altered = metadata_path.read_bytes()
    denied(lambda: resume_held_restore(new, cut, **route_args))
    assert metadata_path.read_bytes() == altered
    metadata_path.write_bytes(metadata_bytes)
try:
    resume_held_restore(new, replace(cut, logical_database_sha256="0" * 64), **route_args)
except (LifecycleError, InstanceAdmissionError):
    pass
else:
    raise AssertionError("Changed reconstruction cut accepted")
if pending_bytes is not None:
    changed = json.loads(pending_bytes)
    changed["context"]["target_sha256"] = "0" * 64
    pending.write_text(json.dumps(changed))
    altered_bytes = pending.read_bytes()
    try:
        resume_held_restore(new, cut, **route_args)
    except (LifecycleError, InstanceAdmissionError):
        pass
    else:
        raise AssertionError("Changed pending context accepted")
    assert pending.read_bytes() == altered_bytes
    pending.write_bytes(pending_bytes)
    changed = json.loads(pending_bytes)
    changed["action"] = "maintenance"
    pending.write_text(json.dumps(changed))
    denied(lambda: resume_held_restore(new, cut, **route_args))
    pending.write_bytes(pending_bytes)
# Rebuild the complete synthetic evidence fixture in every cold process, and
# share it between promotion checks and the later lawful capture.
port = Port()
port.sources["later-source"] = replace(
    port.sources["source1"], evidence_id="later-source", source_key="later-message"
)
args = dict(
    operation_id="promote-cold", approval_ref="approved", port=port, binding=held.new_binding
)
if pending_bytes is not None:
    for change in (
        dict(operation_id="wrong-operation"), dict(approval_ref="wrong-approval"),
        dict(binding=replace(held.new_binding, generation="wrong-generation")),
    ):
        denied(lambda: resume_restore_promotion(held, **(args | change), **route_args))
        assert pending.read_bytes() == pending_bytes
    with sqlite3.connect(database) as offline:
        offline.execute(
            "INSERT INTO participants(public_id,display_name) "
            "VALUES ('after-reconstruction','Changed')"
        )
    offline.close()
    assert resume_held_restore(new, cut, **route_args).status == "RESTORE_ADMIT_PENDING"
    denied(lambda: resume_restore_promotion(held, **args, **route_args))
    assert pending.read_bytes() == pending_bytes
    with database.open("r+b") as restored_database:
        restored_database.write(database_bytes)
        restored_database.truncate()
    held = replace(held, status="ACTIVE")  # Advisory status cannot skip protected checks.
result = resume_restore_promotion(held, **args, **route_args)
assert result["state"] == ("FENCED" if phase == "fenced" else "ACTIVE")
assert not pending.exists()
version = authority._state(new)["version"]
if phase:
    assert (new.control_root / authority.STATE_FILENAME).read_bytes() == state_bytes
    if phase == "active":
        fence_production_instance(new, operation_id="fence-cold", approval_ref="approved")
    if old is not None:
        old.close()
    new.close()
    print("COLD_PUBLIC_RECONSTRUCTION_PASSED")
    sys.exit(0)
os.environ["FINANCE_RUNTIME_ROOT"] = str(Path(new._binding["profile_root"]) / "runtime")
owner = acquire_production_owner(new)
service = LedgerService(owner, port, held.new_binding)
source = service.capture("later-source", "later-capture")
owner.close()
# Current content legitimately differs from the old cut; completed observation
# must authenticate metadata without imposing the historical cut on current data.
observed = resume_held_restore(new, cut, **route_args)
assert observed.status == "ACTIVE"
assert resume_restore_promotion(observed, **args, **route_args)["state"] == "ACTIVE"
assert authority._state(new)["version"] == version
if old is not None:
    old.close()
new.close()
print("COLD_PUBLIC_RECONSTRUCTION_PASSED")
"""
    for phase in ("", "active", "fenced"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(payload), phase],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "COLD_PUBLIC_RECONSTRUCTION_PASSED" in result.stdout


@pytest.mark.parametrize("shared_control", [False, True], ids=["separate-root", "shared-root"])
@pytest.mark.parametrize("interruption", ["before-publish", "pending-unlink"])
def test_each_root_resumes_interrupted_preparation_and_designation(
    original, tmp_path, monkeypatch, shared_control, interruption
):
    old, owner, service, _ = original
    cut, path = _cut(service, tmp_path)
    _fence(old, owner)
    root = tmp_path / "designation-target"
    root.mkdir(mode=0o700)
    new = synthetic_config(
        root,
        monkeypatch,
        instance_id="designation-target",
        control_root=old.control_root if shared_control else None,
    )
    binding = RuntimeBinding(new.instance_id, "new-generation", "user", "chat")
    original_unlink = authority.os.unlink

    def fail_pending_delete(filename, *args, **kwargs):
        if filename == PENDING_FILENAME:
            raise OSError("synthetic interrupted recovery phase")
        return original_unlink(filename, *args, **kwargs)

    def interrupt(module):
        if interruption == "before-publish":
            return patch.object(
                module, "_publish", side_effect=OSError("synthetic interrupted recovery phase")
            )
        return patch.object(authority.os, "unlink", side_effect=fail_pending_delete)

    # Cover preparation under both root layouts and both journal cut points too;
    # admission has the independent live/receipt/root/interruption matrix above.
    with interrupt(authority), pytest.raises(OSError, match="synthetic interrupted recovery phase"):
        prepare_restored_profile(
            new,
            path,
            cut,
            new_binding=binding,
            operation_id="prepare-designation-target",
            approval_ref="approved",
        )
    prepared_pending = new.control_root / PENDING_FILENAME
    prepared_bytes = prepared_pending.read_bytes()
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(
            new, operation_id="prepare-designation-target", approval_ref="approved"
        )
    assert prepared_pending.read_bytes() == prepared_bytes
    held = resume_restore_prepare(
        new, cut, binding, operation_id="prepare-designation-target", approval_ref="approved"
    )
    assert not prepared_pending.exists()
    assert (
        resume_restore_prepare(
            new, cut, binding, operation_id="prepare-designation-target", approval_ref="approved"
        )
        == held
    )
    other_config, _, other_target = _target(
        tmp_path, monkeypatch, cut, path, name="other-designation-target", operation="prepare-other"
    )
    recovery = RecoveryAuthority("designation-interruption", b"r" * 32)
    with (
        interrupt(recovery_module),
        pytest.raises(OSError, match="synthetic interrupted recovery phase"),
    ):
        # A signing attempt cannot return any receipt while publication is unknown.
        recovery.issue_old_writer_isolation(
            old, held, operation_id="designate-interrupted", approval_ref="approved"
        )
    pending = old.control_root / PENDING_FILENAME
    pending_bytes = pending.read_bytes()
    journal = json.loads(pending_bytes)
    assert journal["action"] == "recovery_designate"
    published = authority._read(old, authority.STATE_FILENAME)
    assert published == (
        authority._validate_state(journal["after"], prefix_version=journal["after"]["version"] - 1)
        if interruption == "before-publish"
        else journal["after"]
    )
    for config in (old, new):
        with pytest.raises(InstanceAdmissionError):
            acquire_production_owner(config)
    with pytest.raises(InstanceAdmissionError, match="dedicated"):
        resume_authority_transition(
            old, operation_id="designate-interrupted", approval_ref="approved"
        )

    def resume(restored=held, operation_id="designate-interrupted", approval_ref="approved"):
        return resume_restore_designation(
            old, restored, operation_id=operation_id, approval_ref=approval_ref
        )

    for changed in (
        dict(operation_id="wrong-operation"),
        dict(approval_ref="wrong-approval"),
        dict(restored=replace(held, cut=replace(cut, logical_database_sha256="0" * 64))),
        dict(restored=other_target),
    ):
        with pytest.raises((LifecycleError, InstanceAdmissionError)):
            resume(**changed)
        assert pending.read_bytes() == pending_bytes
    altered = json.loads(pending_bytes)
    altered["context"]["designation"]["target"]["binding_sha256"] = "0" * 64
    pending.write_text(canonical(altered))
    altered_bytes = pending.read_bytes()
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        resume()
    assert pending.read_bytes() == altered_bytes
    pending.write_bytes(pending_bytes)
    source = Path(old._binding["profile_root"]) / "runtime/database/finance.db"
    source_bytes = source.read_bytes()
    with sqlite3.connect(source) as offline:
        offline.execute(
            "INSERT INTO participants(public_id,display_name) VALUES ('changed-old','Changed')"
        )
    offline.close()
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        resume()
    assert pending.read_bytes() == pending_bytes
    with source.open("r+b") as restored_source:
        restored_source.write(source_bytes)
        restored_source.truncate()

    from finance_core.instance_admission import TrustedInstanceConfig

    closed_target = TrustedInstanceConfig(
        new.control_root,
        new.locator_root,
        new.profile_id,
        layout=new.layout,
        instance_id=new.instance_id,
        config_identity=new.config_identity,
        core_identity=new.core_identity,
        application_schema=new.application_schema,
        approval_verifier=new.approval_verifier,
    )
    assert closed_target.binding_sha256 == new.binding_sha256
    closed_target.close()
    with pytest.raises(InstanceAdmissionError, match="process/fork"):
        resume(replace(held, config=closed_target))
    assert pending.read_bytes() == pending_bytes

    # Retain only the original public preparation descriptor fields and trusted
    # composition inputs; do not rebuild a target by decoding private history.
    material = {
        "old_binding": old._binding,
        "old_locator": str(old.locator_root),
        "new_binding": new._binding,
        "new_locator": str(new.locator_root),
        "cut": asdict(cut),
        "runtime_binding": asdict(binding),
        "prepare_operation": held.restore_prepare_operation_id,
        "prepare_entry": held.restore_prepare_entry_sha256,
        "approval_digest": journal["approval_digest"],
    }
    payload = tmp_path / "designation-restart.json"
    payload.write_text(json.dumps(material))
    payload.chmod(0o600)
    child = r"""
import json, os, sys
from dataclasses import asdict
from pathlib import Path
from finance_core.instance_admission import TrustedInstanceConfig, authority, PENDING_FILENAME
from finance_core.lifecycle_v2 import RuntimeBinding, SnapshotCut
from finance_core.lifecycle_v2.recovery import (
    RestoredProfile, RecoveryAuthority, resume_restore_designation,
)
m = json.loads(Path(sys.argv[1]).read_text())
os.environ["FINANCE_RUNTIME_ROOT"] = str(Path(m["new_binding"]["profile_root"]) / "runtime")
def config(binding, locator):
    return TrustedInstanceConfig(
        Path(binding["control_root"]), Path(locator), binding["profile_id"],
        layout=binding["layout"], instance_id=binding["instance_id"],
        config_identity=binding["config_identity"], core_identity=binding["core_identity"],
        approval_verifier=lambda ref, action, digest: (
            ref == "approved" and action == "recovery_designate" and digest == m["approval_digest"]
        ),
    )
old = config(m["old_binding"], m["old_locator"])
new = config(m["new_binding"], m["new_locator"])
held = RestoredProfile(
    new, SnapshotCut.from_dict(m["cut"]), RuntimeBinding(**m["runtime_binding"]),
    m["prepare_operation"], m["prepare_entry"],
)
designation = resume_restore_designation(
    old, held, operation_id="designate-interrupted", approval_ref="approved"
)
assert not (old.control_root / PENDING_FILENAME).exists()
assert authority._state(old)["history"][-1]["entry_sha256"] == designation.entry_sha256
signer = RecoveryAuthority("designation-interruption", b"r" * 32)
receipt = signer.issue_old_writer_isolation(
    old, held, operation_id="designate-interrupted", approval_ref="approved"
)
assert signer.issue_old_writer_isolation(
    old, held, operation_id="designate-interrupted", approval_ref="approved"
) == receipt
print(json.dumps({"designation": asdict(designation), "receipt": asdict(receipt)}))
new.close()
old.close()
"""
    restarted = subprocess.run(
        [sys.executable, "-c", child, str(payload)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    restarted_result = json.loads(restarted.stdout)

    designation = resume()
    assert not pending.exists()
    state = authority._state(old)
    assert sum(entry["action"] == "recovery_designate" for entry in state["history"]) == 1
    assert designation.material["target"]["binding_sha256"] == new.binding_sha256
    assert asdict(designation) == restarted_result["designation"]
    assert resume() == designation
    assert authority._state(old) == state
    receipt = recovery.issue_old_writer_isolation(
        old, held, operation_id="designate-interrupted", approval_ref="approved"
    )
    assert asdict(receipt) == restarted_result["receipt"]
    restarted_signer = RecoveryAuthority("designation-interruption", b"r" * 32)
    assert (
        restarted_signer.issue_old_writer_isolation(
            old, held, operation_id="designate-interrupted", approval_ref="approved"
        )
        == receipt
    )
    assert (
        recovery.verify_old_writer_isolation(receipt, held).entry_sha256 == designation.entry_sha256
    )
    assert authority._state(old) == state
    with pytest.raises(LifecycleError, match="already designated"):
        designate_restore_successor(
            old, other_target, operation_id="cannot-retarget", approval_ref="approved"
        )
    new.close()
    other_config.close()


@pytest.mark.parametrize("original", [False, True], indirect=True, ids=["core", "application"])
def test_manual_single_host_recovery_binds_isolation_and_only_new_writer(
    original, tmp_path, monkeypatch
):
    from test_lifecycle_v2 import TEXT, VerifiedSource, ready

    from finance_core.lifecycle_v2.recovery import (
        HostIsolationV1,
        manual_single_host_promotion_context_v1,
        prepare_manual_single_host_restore_v1,
        promote_manual_single_host_restore_v1,
        reopen_manual_single_host_restore_v1,
    )
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    old, old_owner, old_service, port = original
    _, _, old_review = ready(old_service)
    old_fact = old_service.confirm(old_review.review_id, "old-action", "old-confirm")
    cut, path = _cut(old_service, tmp_path)
    root = tmp_path / "manual-single-host"
    root.mkdir(mode=0o700)
    approved = {}

    def verifier(reference, action, request_digest):
        if action != "restore_admit":
            return reference == "approved"
        return approved.get(reference) == request_digest

    target = synthetic_config(
        root,
        monkeypatch,
        instance_id="manual-target",
        verifier=verifier,
        application_schema=old.application_schema,
    )
    binding = RuntimeBinding(
        target.instance_id, "new-generation", "user", "chat", "single-host-synthetic"
    )
    for changed_cut in (
        replace(cut, migration_identity="different-migration"),
        replace(cut, application_schema_digest="f" * 64),
    ):
        with pytest.raises(LifecycleError, match="migration and schema"):
            prepare_manual_single_host_restore_v1(
                target,
                path,
                changed_cut,
                new_binding=binding,
                operation_id="wrong-prepare",
                approval_ref="approved",
            )
    held = prepare_manual_single_host_restore_v1(
        target,
        path,
        cut,
        new_binding=binding,
        operation_id="prepare-manual",
        approval_ref="approved",
    )
    registration = admit_single_host_runtime_v1(
        target, binding, operation_id="register-manual", approval_ref="approved"
    )
    isolation = HostIsolationV1(
        provider="synthetic-operator",
        host_id="old-host",
        source_instance_id=cut.instance_id,
        source_binding_sha256=cut.source_binding_sha256,
        stopped_observed_at=1792800200,
        evidence_ref="manual-stop-record",
        evidence_sha256=hashlib.sha256(b"synthetic stop evidence").hexdigest(),
        stopped_record="Shutdown verified after the writer closed",
        restart_disabled_record="Automatic restart disabled",
        keep_disabled_instruction="Keep old host and its runtime disabled",
    )
    options = dict(
        isolation=isolation,
        runtime_admission=registration,
        cut_observed_at=1792800000,
        accept_loss_through=1792800200,
    )

    def audit_application(connection, exact_cut, audit_core):
        assert exact_cut == cut
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO manual_app_evidence VALUES ('forbidden')")
        audit_core(port, old_service.binding)
        return authority._digest(
            [
                list(r)
                for r in connection.execute(
                    "SELECT reference FROM manual_app_evidence ORDER BY reference"
                )
            ]
        )

    application_options = {}
    if old.application_schema is not None:
        options["application_history_sha256"] = authority._digest([])
        application_options["audit_application_history"] = audit_application

    def promote(**overrides):
        return promote_manual_single_host_restore_v1(
            held,
            operation_id="activate-manual",
            approval_ref="manual-approval",
            port=port,
            binding=binding,
            **(options | application_options | overrides),
        )

    if old.application_schema is not None:
        with pytest.raises(LifecycleError, match="application history verifier"):
            promote(audit_application_history=None)
        with pytest.raises(LifecycleError, match="omitted Core evidence"):
            promote(audit_application_history=lambda connection, cut, audit: authority._digest([]))

    with pytest.raises(InstanceAdmissionError, match="approval"):
        promote()
    with pytest.raises(InstanceAdmissionError, match="Owner requires"):
        acquire_production_owner(target)

    # Exercise real local writer closure/fencing as the synthetic provider action.
    # The manual target has an independent, empty authority lineage.
    with monkeypatch.context() as old_runtime:
        old_runtime.setenv(
            "FINANCE_RUNTIME_ROOT", str(Path(old._binding["profile_root"]) / "runtime")
        )
        _fence(old, old_owner)
    context = manual_single_host_promotion_context_v1(held, **options)
    approved["manual-approval"] = authority._digest(
        {
            "binding": target._binding,
            "action": "restore_admit",
            "context": {
                "operation_id": "activate-manual",
                "before_sha256": authority._digest(authority._state(target)),
                "context": context,
            },
        }
    )
    before = authority._state(target)
    if old.application_schema is not None:

        def changed_history(connection, exact_cut, audit_core):
            audit_application(connection, exact_cut, audit_core)
            return "c" * 64

        with pytest.raises(LifecycleError, match="history differs"):
            promote(audit_application_history=changed_history)
    with pytest.raises(InstanceAdmissionError, match="approval"):
        promote(isolation=replace(isolation, evidence_sha256="f" * 64))
    with pytest.raises(InstanceAdmissionError, match="loss interval"):
        promote(accept_loss_through=1792800150)
    with pytest.raises(InstanceAdmissionError, match="approval"):
        promote(accept_loss_through=1792800250)
    with pytest.raises(InstanceAdmissionError, match="registration"):
        promote(runtime_admission=replace(registration, registration_entry_sha256="b" * 64))
    assert authority._state(target) == before

    with patch.object(recovery_module, "_publish", side_effect=OSError("interrupted publication")):
        with pytest.raises(OSError, match="interrupted publication"):
            promote()
    assert (target.control_root / PENDING_FILENAME).exists()
    with pytest.raises(InstanceAdmissionError):
        acquire_production_owner(target)
    reopened = reopen_manual_single_host_restore_v1(
        target, cut, binding, prepare_operation_id="prepare-manual"
    )
    assert reopened == held
    held = reopened
    with pytest.raises((LifecycleError, InstanceAdmissionError)):
        promote(isolation=replace(isolation, evidence_sha256="f" * 64))
    assert promote()["state"] == "ACTIVE"
    assert promote()["state"] == "ACTIVE"
    assert authority._state(target)["version"] == before["version"] + 1

    with acquire_production_owner(target) as owner:
        restored = LedgerService(
            owner, port, binding, runtime_admission=registration, clock=lambda: 1792800300
        )
        assert restored.reader().get(old_fact.record_id)["values"]["amount"] == "12.00"
        with pytest.raises(LifecycleError):
            restored.confirm(old_review.review_id, "old-action", "old-confirm")
        port.sources["fresh-source"] = VerifiedSource(
            "fresh-source",
            "user",
            "chat",
            "fresh-message",
            TEXT,
            hashlib.sha256(TEXT.encode()).hexdigest(),
            1792800300,
        )
        source = restored.capture("fresh-source", "new-capture")
        proposal = restored.propose(source.source_id, "new-observation", 0, "new-propose")
        review = restored.prepare_review(proposal.proposal_id, proposal.version)
        restored.record_delivery(review.review_id, "new-delivery")
        receipt = restored.confirm(review.review_id, "new-action", "new-confirm")
        assert restored.reader().get(receipt.record_id)["values"]["amount"] == "12.00"
    assert promote()["state"] == "ACTIVE"
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(Path(old._binding["profile_root"]) / "runtime"))
    with acquire_production_owner(old) as owner:
        still_old = LedgerService(owner, port, old_service.binding, clock=lambda: 1792800300)
        with pytest.raises(InstanceAdmissionError):
            still_old.capture("fresh-source", "old-capture")
    target.close()


def test_manual_restore_requires_generation_absent_from_archived_sources(
    original, tmp_path, monkeypatch
):
    from test_lifecycle_v2 import ready

    from finance_core.lifecycle_v2.recovery import (
        HostIsolationV1,
        prepare_manual_single_host_restore_v1,
        promote_manual_single_host_restore_v1,
    )
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    old, owner, archived_service, port = original
    ready(archived_service)
    newer_binding = replace(archived_service.binding, generation="latest-source-generation")
    newer_service = LedgerService(owner, port, newer_binding, clock=lambda: 1792800000)
    cut, path = _cut(newer_service, tmp_path)
    _fence(old, owner)

    root = tmp_path / "history-generation"
    root.mkdir(mode=0o700)
    config = synthetic_config(root, monkeypatch, instance_id="history-generation")
    binding = RuntimeBinding(
        config.instance_id,
        archived_service.binding.generation,
        "user",
        "chat",
        "single-host-synthetic",
    )
    with config:
        restored = prepare_manual_single_host_restore_v1(
            config, path, cut, new_binding=binding, operation_id="prepare", approval_ref="approved"
        )
        registered = admit_single_host_runtime_v1(
            config, binding, operation_id="register", approval_ref="approved"
        )
        observed_stop = 1792800200
        isolation = HostIsolationV1(
            "fixture-provider",
            "fixture-old-host",
            cut.instance_id,
            cut.source_binding_sha256,
            observed_stop,
            "fixture-stop",
            "a" * 64,
            "Stopped",
            "Restart disabled",
            "Keep the old host disabled",
        )
        before = authority._state(config)
        with pytest.raises(LifecycleError, match="generation occurs in archived ledger history"):
            promote_manual_single_host_restore_v1(
                restored,
                isolation=isolation,
                runtime_admission=registered,
                cut_observed_at=1792800000,
                accept_loss_through=observed_stop,
                operation_id="activate",
                approval_ref="approved",
                port=port,
                binding=binding,
            )
        assert authority._state(config) == before
        assert inspect_instance_admission(config)["state"] == "RESTORE_HELD"


def _manual_historical_target(tmp_path, monkeypatch, source_config, cut, path, name, clock):
    from finance_core.lifecycle_v2.recovery import (
        HostIsolationV1,
        prepare_manual_single_host_restore_v1,
    )
    from finance_core.lifecycle_v2.single_host_runtime_v1 import admit_single_host_runtime_v1

    root = tmp_path / name
    root.mkdir(mode=0o700)
    target = synthetic_config(
        root, monkeypatch, instance_id=name, application_schema=source_config.application_schema
    )
    binding = RuntimeBinding(
        target.instance_id, name + "-generation", "user", "chat", "single-host-synthetic"
    )
    held = prepare_manual_single_host_restore_v1(
        target, path, cut, new_binding=binding, operation_id="prepare", approval_ref="approved"
    )
    admission = admit_single_host_runtime_v1(
        target, binding, operation_id="registration", approval_ref="approved"
    )
    isolation = HostIsolationV1(
        "synthetic",
        "old-host",
        cut.instance_id,
        cut.source_binding_sha256,
        clock,
        "record",
        "c" * 64,
        "stopped",
        "restart disabled",
        "keep disabled",
    )
    kwargs = dict(
        isolation=isolation,
        runtime_admission=admission,
        cut_observed_at=clock,
        accept_loss_through=clock,
        operation_id="activation",
        approval_ref="approved",
        port=None,
        binding=binding,
    )
    return target, held, kwargs


@pytest.mark.parametrize("original", [True], indirect=True)
def test_manual_recovery_reads_original_accounts_and_posts_with_new_generation(
    original, tmp_path, monkeypatch
):
    from finance_core.lifecycle_v2.daily_v1 import DailyAccountReaderV1, DailyAccountServiceV1
    from finance_core.lifecycle_v2.recovery import promote_manual_single_host_restore_v1
    from tests.daily_account_support_v1 import NOW, DailyPort
    from tests.test_daily_accounts_lifecycle_v1 import create

    old, old_owner, source, _ = original
    old_port = source.port = DailyPort()
    source.clock = lambda: NOW
    _, _, old_review, old_fact = create(source, old_port, DailyAccountServiceV1(source))
    audit = source.reader().full_audit()
    cut, path = _cut(source, tmp_path)
    _fence(old, old_owner)
    target, held, kwargs = _manual_historical_target(
        tmp_path, monkeypatch, old, cut, path, "restored-history", NOW
    )
    binding, admission = kwargs["binding"], kwargs["runtime_admission"]
    history_hash = authority._digest([])

    def audit_application(connection, exact_cut, audit_core):
        assert exact_cut == cut
        assert connection.in_transaction
        assert audit_core(old_port, source.binding) == audit
        return history_hash

    from finance_core.lifecycle_v2.historical_evidence_v1 import (
        HistoricalEvidenceV1,
        HistoricalGenerationV1,
    )

    archived = HistoricalGenerationV1(source.binding, cut, history_hash, audit["audit_sha256"])
    kwargs.update(
        application_history_sha256=history_hash, audit_application_history=audit_application
    )
    with pytest.raises(LifecycleError, match="event"):
        promote_manual_single_host_restore_v1(
            held, historical_generations=(replace(archived, event_sha256="f" * 64),), **kwargs
        )

    def commits_during_held_audit(connection, exact_cut, audit_core):
        with pytest.raises(sqlite3.DatabaseError):
            connection.commit()
        return audit_application(connection, exact_cut, audit_core)

    with pytest.raises(LifecycleError, match="read-only scope"):
        promote_manual_single_host_restore_v1(
            held,
            historical_generations=(archived,),
            **(kwargs | {"audit_application_history": commits_during_held_audit}),
        )
    assert inspect_instance_admission(target)["state"] == "RESTORE_HELD"
    assert (
        promote_manual_single_host_restore_v1(held, historical_generations=(archived,), **kwargs)[
            "state"
        ]
        == "ACTIVE"
    )
    new_port = DailyPort()
    new_port.sources.clear()
    new_port.add_source("source2", "Create another MYR bank account")
    with acquire_production_owner(target) as owner:

        def service(historical_generation=archived, historical_port=old_port):
            return LedgerService(
                owner,
                new_port,
                binding,
                clock=lambda: NOW,
                runtime_admission=admission,
                historical_evidence=(HistoricalEvidenceV1(historical_generation, historical_port),),
            )

        with pytest.raises(LifecycleError, match="commitment"):
            service(replace(archived, application_history_sha256="0" * 64)).reader().full_audit()
        restored = service()
        daily = DailyAccountServiceV1(restored)
        reader = DailyAccountReaderV1(restored)
        account = old_fact.account_ref.account_id
        assert reader.get_account(account).opening_amount == "12.30"
        with pytest.raises(LifecycleError):
            daily.confirm(old_review.review_id, "action1", "confirm1")
        _, _, fresh_review, fresh = create(restored, new_port, daily, suffix="2")
        assert fresh_review.material["binding"] == asdict(binding)
        assert reader.get_account(fresh.account_ref.account_id).opening_amount == "12.30"
        current_audit = restored.reader().full_audit()
        assert current_audit["fact_version_count"] == 2
        second_path = tmp_path / "second-archive.sqlite"
        second_cut = restored.seal_snapshot(second_path)
        from finance_core.instance_admission.sessions import lifecycle_read_operation
        from finance_core.lifecycle_v2.historical_evidence_v1 import (
            audit_runtime_archive_evidence_connection_v1,
        )

        with lifecycle_read_operation(owner) as conn:
            conn.execute("BEGIN")
            try:
                assert (
                    audit_runtime_archive_evidence_connection_v1(
                        conn, restored, new_port, binding, second_cut
                    )
                    == current_audit
                )
            finally:
                conn.rollback()
        fence_production_instance(target, operation_id="fence", approval_ref="approved")

    # A later active record in the same authority root changes the current
    # pointer. The fenced restored owner still holds its own forensic rights.
    with monkeypatch.context() as another_runtime:
        replacement_root = tmp_path / "replacement-in-source-lineage"
        replacement_root.mkdir(mode=0o700)
        replacement = synthetic_config(
            replacement_root,
            another_runtime,
            instance_id="replacement-in-source-lineage",
            control_root=target.control_root,
        )
        prepare_production_profile(
            replacement, operation_id="replacement-prepare", approval_ref="approved"
        ).close()
        admit_production_instance(
            replacement, operation_id="replacement-admit", approval_ref="approved"
        )
        assert authority._state(replacement)["current"] == replacement.binding_sha256
        replacement.close()

    with acquire_production_owner(target) as fenced_owner:
        forensic = LedgerService(
            fenced_owner,
            new_port,
            binding,
            runtime_admission=admission,
            historical_evidence=(HistoricalEvidenceV1(archived, old_port),),
        )
        assert forensic.reader().full_audit() == current_audit
        with pytest.raises(InstanceAdmissionError):
            forensic.capture("source2", "no-fenced-write")

    second, held_again, again_kwargs = _manual_historical_target(
        tmp_path, monkeypatch, target, second_cut, second_path, "second-restored-history", NOW
    )
    from finance_core.lifecycle_v2.historical_evidence_v1 import historical_generations_sha256_v1

    first_evidence = HistoricalEvidenceV1(archived, old_port)
    parent = historical_generations_sha256_v1((archived,))
    second_history_hash = authority._digest(["new signed history"])
    second_archived = HistoricalGenerationV1(
        binding, second_cut, second_history_hash, current_audit["audit_sha256"]
    )

    def audit_again(connection, exact_cut, audit_core, proof):
        assert exact_cut == second_cut
        assert (
            audit_core(
                new_port,
                binding,
                historical_evidence=(first_evidence,),
                parent_history_sha256=proof,
            )
            == current_audit
        )
        return second_history_hash

    again_kwargs["application_history_sha256"] = second_history_hash
    ancestry = (archived, second_archived)
    with pytest.raises(LifecycleError, match="omitted approved ancestry"):
        promote_manual_single_host_restore_v1(
            held_again,
            historical_generations=ancestry,
            audit_application_history=lambda c, cut, audit: audit_again(c, cut, audit, None),
            **again_kwargs,
        )
    assert (
        promote_manual_single_host_restore_v1(
            held_again,
            historical_generations=ancestry,
            audit_application_history=lambda c, cut, audit: audit_again(c, cut, audit, parent),
            **again_kwargs,
        )["state"]
        == "ACTIVE"
    )
    with acquire_production_owner(second) as owner:
        third = LedgerService(
            owner,
            DailyPort(),
            again_kwargs["binding"],
            runtime_admission=again_kwargs["runtime_admission"],
            historical_evidence=(first_evidence, HistoricalEvidenceV1(second_archived, new_port)),
        )
        assert third.reader().full_audit() == current_audit
    second.close()
    target.close()


@pytest.mark.parametrize("original", [True], indirect=True)
def test_manual_restore_reads_old_pending_but_cannot_review_or_deliver_it(
    original, tmp_path, monkeypatch
):
    from test_lifecycle_v2 import TEXT, VerifiedSource, ready, response

    from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1, EffectiveReadError
    from finance_core.lifecycle_v2.historical_evidence_v1 import (
        HistoricalEvidenceV1,
        HistoricalGenerationV1,
    )
    from finance_core.lifecycle_v2.recovery import promote_manual_single_host_restore_v1

    config, owner, old, old_port = original
    now = 1792800000

    def source(port, evidence_id, body):
        port.sources[evidence_id] = VerifiedSource(
            evidence_id,
            "user",
            "chat",
            "message-" + evidence_id,
            body,
            hashlib.sha256(body.encode()).hexdigest(),
            now,
        )

    posted_source, _, review = ready(old)
    fact = old.confirm(review.review_id, "confirmed-action", "confirmed")
    source(old_port, "pending", TEXT)
    pending_source = old.capture("pending", "capture-pending")
    pending = old.propose(pending_source.source_id, "pending-observation", 0, "propose-pending")
    pending_card = old.prepare_review(pending.proposal_id, pending.version)

    reply = "Change to SGD 15.00; reason: correction"
    source(old_port, "old-reply", reply)
    old_port.raw = response(reply, "reply", {"amount": "15.00", "currency": "SGD"})
    with old._write_operation() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old_correction = old._propose(
            conn,
            posted_source.source_id,
            "old-correction",
            fact.version,
            "propose-old-correction",
            record_id=fact.record_id,
            reply_evidence_id="old-reply",
            correction_reason="correction",
        )
        conn.commit()
    old_audit = old.reader().full_audit()
    cut, path = _cut(old, tmp_path)
    _fence(config, owner)
    target, held, kwargs = _manual_historical_target(
        tmp_path, monkeypatch, config, cut, path, "restored-native-pending", now
    )
    history_hash = authority._digest(["historical native"])
    archived = HistoricalGenerationV1(old.binding, cut, history_hash, old_audit["audit_sha256"])

    def audit_application(connection, exact_cut, audit_core):
        assert connection.in_transaction and exact_cut == cut
        assert audit_core(old_port, old.binding) == old_audit
        return history_hash

    assert (
        promote_manual_single_host_restore_v1(
            held,
            historical_generations=(archived,),
            application_history_sha256=history_hash,
            audit_application_history=audit_application,
            **kwargs,
        )["state"]
        == "ACTIVE"
    )
    with acquire_production_owner(target) as current_owner:
        unqualified = LedgerService(current_owner, old_port, kwargs["binding"])
        with pytest.raises(InstanceAdmissionError, match="runtime admission"):
            unqualified.reader().full_audit()
        with pytest.raises(InstanceAdmissionError, match="runtime admission"):
            unqualified.reader().get(fact.record_id)
        with pytest.raises((EffectiveReadError, InstanceAdmissionError)):
            EffectiveLedgerReaderV1(unqualified).collect()

        current_port = Port()
        current_port.sources.clear()
        source(current_port, "fresh-reply", reply)
        current_port.raw = old_port.raw
        current = LedgerService(
            current_owner,
            current_port,
            kwargs["binding"],
            clock=lambda: now,
            runtime_admission=kwargs["runtime_admission"],
            historical_evidence=(HistoricalEvidenceV1(archived, old_port),),
        )
        assert current.reader().pending()[0].review_id == pending_card.review_id
        with patch.object(current_port, "delivery", side_effect=AssertionError("delivery invoked")):
            with pytest.raises(LifecycleError, match="archived generation"):
                current.record_delivery(pending_card.review_id, "late-delivery")
        with patch.object(old_port, "provider", side_effect=AssertionError("provider invoked")):
            with pytest.raises(LifecycleError, match="archived generation"):
                current.prepare_review(old_correction.proposal_id, old_correction.version)
        assert current.reader().full_audit() == old_audit

        expected = current.expected_invocation(
            posted_source.source_id, fact.version, fact.record_id, "fresh-reply", "correction"
        )
        assert expected.binding == kwargs["binding"]
        assert expected.projection["current_values"]["amount"] == "12.00"
        corrected = current.prepare_correction(
            fact.record_id,
            fact.version,
            "fresh-obs",
            "propose-fresh-correction",
            "fresh-reply",
            "correction",
        )
        current.record_delivery(corrected.review_id, "fresh-delivery")
        current.confirm(corrected.review_id, "fresh-action", "confirm-fresh-correction")
        assert current.reader().get(fact.record_id)["values"]["amount"] == "15.00"
    target.close()


def test_detached_historical_audit_requires_core_owned_exact_read(original, tmp_path):
    from finance_core.lifecycle_v2.historical_evidence_v1 import (
        audit_archive_evidence_connection_v1,
        historical_generations_sha256_v1,
        open_archive_read_connection_v1,
    )

    _, _, service, port = original
    cut, path = _cut(service, tmp_path)
    kwargs = {"parent_history_sha256": historical_generations_sha256_v1(())}
    with open_archive_read_connection_v1(path, database_sha256=cut.database_sha256) as conn:
        assert conn.in_transaction and conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            audit_archive_evidence_connection_v1(conn, port, service.binding, cut, **kwargs)
            == service.reader().full_audit()
        )

    with pytest.raises(LifecycleError, match="Core-issued"):
        audit_archive_evidence_connection_v1(conn, port, service.binding, cut, **kwargs)
    with pytest.raises(LifecycleError, match="database digest"):
        with open_archive_read_connection_v1(path, database_sha256="e" * 64):
            pytest.fail("Opened archive at the wrong digest")
