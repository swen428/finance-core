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
def original(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(
        config, operation_id="prepare-original", approval_ref="approved"
    ).close()
    admit_production_instance(config, operation_id="admit-original", approval_ref="approved")
    owner = acquire_production_owner(config)
    port = Port()
    service = LedgerService(
        owner,
        port,
        RuntimeBinding(config.instance_id, "old-generation", "wen", "chat"),
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
    binding = RuntimeBinding(name, "new-generation", "wen", "chat")
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
    binding = RuntimeBinding("target", "new-generation", "wen", "chat")
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
service = LedgerService(owner, Port(), RuntimeBinding(config.instance_id, "old", "wen", "chat"))
cut = service.seal_snapshot(root / "sealed.sqlite")
fence_production_instance(config, operation_id="fence", approval_ref="approved")
owner.close()
newroot = root / "new"
newroot.mkdir(mode=0o700)
new = synthetic_config(newroot, Environment(), instance_id="new")
held = recovery.prepare_restored_profile(new, root / "sealed.sqlite", cut,
    new_binding=RuntimeBinding("new", "new", "wen", "chat"),
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
                owner, Port(), RuntimeBinding(old.instance_id, "old-generation", "wen", "chat")
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
            new_binding=RuntimeBinding(new.instance_id, "new-generation", "wen", "chat"),
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
    binding = RuntimeBinding(new.instance_id, "new-generation", "wen", "chat")
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
            "wen",
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
            "wen",
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
    binding = RuntimeBinding(new.instance_id, "new-generation", "wen", "chat")
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
    binding = RuntimeBinding(new.instance_id, "new-generation", "wen", "chat")
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
args = dict(
    operation_id="promote-cold", approval_ref="approved", port=Port(), binding=held.new_binding
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
port = Port()
port.sources["later-source"] = replace(
    port.sources["source1"], evidence_id="later-source", source_key="later-message"
)
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
    binding = RuntimeBinding(new.instance_id, "new-generation", "wen", "chat")
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
