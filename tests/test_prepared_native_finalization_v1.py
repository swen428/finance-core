"""Actual synthetic owners, durable exact preparation, and bypass/fault refusals."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

import pytest
from lifecycle_recovery_material_support_v1 import make_service, stage_expense

from finance_core.instance_admission import InstanceAdmissionError, acquire_production_owner
from finance_core.instance_admission.sessions import (
    lifecycle_read_operation,
    production_write_operation,
)
from finance_core.lifecycle_v2 import LedgerService, LifecycleError
from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1
from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
from finance_core.lifecycle_v2.recovery_material_v2 import (
    ClosureFrontierV2,
    check_native_closure_v2,
    decode_native_closure_v2,
)


@pytest.fixture
def live(tmp_path, monkeypatch):
    service, port, cfg, authenticator = make_service(tmp_path, monkeypatch)
    api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=authenticator)
    yield service, port, cfg, authenticator, api
    if service.owner._lease.fd >= 0:
        service.owner.close()
    cfg.close()


def stage(service, port, key="expense"):
    source, proposal, review, _ = stage_expense(service, port, key, stage="delivery")
    port.register_action(service, review, "delivery-" + key, "action-" + key)
    return source, proposal, review


def native_rows(service):
    with lifecycle_read_operation(service.owner) as conn:
        return {
            name: tuple(tuple(row) for row in conn.execute("SELECT * FROM l2_" + name))
            for name in (
                "sources",
                "proposals",
                "reviews",
                "deliveries",
                "facts",
                "decisions",
                "events",
            )
        }


def prepare(live, key="expense", operation="finalize"):
    service, port, _, _, api = live
    source, proposal, review = stage(service, port, key)
    ref = api.prepare(review.review_id, "action-" + key, operation)
    return source, proposal, review, ref


def test_prepare_source_has_no_fact_detached_material_and_exact_apply(live):
    service, port, _, authenticator, api = live
    _, _, review = stage(service, port)
    before = native_rows(service)
    ref = api.prepare(review.review_id, "action-expense", "finalize")
    assert native_rows(service) == before
    assert not EffectiveLedgerReaderV1(service).collect().facts
    assert api.prepare(review.review_id, "action-expense", "finalize") == ref
    packages = api.material("finalize", expected_intent_sha256=ref.intent_sha256)
    tail = decode_native_closure_v2(packages[-1])
    checked = check_native_closure_v2(
        packages,
        authenticator=authenticator,
        expected_identity=tail.identity,
        expected_frontier=ClosureFrontierV2(
            hashlib.sha256(packages[-1]).hexdigest(), tail.last_event_seq, tail.last_event_sha256
        ),
    )
    assert checked.current_values[0][0] == ref.record_id
    assert checked.current_values[0][3] == ref.fact_sha256
    receipt = api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    assert receipt.record_id == ref.record_id
    assert receipt.fact_sha256 == ref.fact_sha256
    assert receipt.commit_seq == ref.commit_seq
    assert api.inspect("finalize").state == "APPLIED_OFFLINE"
    assert api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256) == receipt
    assert service.confirm(review.review_id, "action-expense", "finalize") == receipt
    assert api.material("finalize", expected_intent_sha256=ref.intent_sha256) == packages


def test_pending_blocks_every_supported_writer_but_history_remains_readable(live):
    service, port, _, _, api = live
    _, _, review, ref = prepare(live)
    blocked = (
        lambda: service.capture("missing", "other-capture"),
        lambda: service.propose("missing", "missing", 0, "other-propose"),
        lambda: service.prepare_review("missing", 1),
        lambda: service.record_delivery(review.review_id, "delivery-expense"),
        lambda: service.confirm(review.review_id, "action-expense", "other-confirm"),
        lambda: service.prepare_correction("missing", 1, "missing", "other-correction"),
        lambda: service.import_verified(None),
    )
    for action in blocked:
        with pytest.raises(InstanceAdmissionError, match="Pending exact"):
            action()
    with pytest.raises(InstanceAdmissionError, match="Pending exact"):
        with production_write_operation(service.owner):
            raise AssertionError("ordinary connection escaped pending gate")
    assert api.inspect("finalize").prepared == ref
    assert api.inspect("unknown").state == "ABSENT_LOCAL"
    assert not EffectiveLedgerReaderV1(service).collect().facts


def test_ordinary_writer_cannot_insert_or_replace_control_rows(live):
    import sqlite3

    service, _, _, _, _ = live
    with production_write_operation(service.owner) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for table in (
            "l2_finalization_intents",
            "l2_finalization_materials",
            "l2_finalization_outcomes",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute("DELETE FROM " + table)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("INSERT INTO l2_finalization_materials VALUES(?,?)", ("0" * 64, b"x"))
        conn.rollback()


def test_cancel_is_permanent_for_review_action_but_not_economic_source(live):
    from lifecycle_recovery_material_support_v1 import response

    service, port, _, _, api = live
    source, proposal, review, ref = prepare(live)
    packages = api.material("finalize", expected_intent_sha256=ref.intent_sha256)
    before = native_rows(service)
    state = api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    assert state.state == "CANCELLED_OFFLINE"
    assert native_rows(service) == before
    assert api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256) == state
    for action in (
        lambda: api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256),
        lambda: service.confirm(review.review_id, "action-expense", "finalize"),
        lambda: service.confirm(review.review_id, "action-expense", "fresh-operation"),
        lambda: api.prepare(review.review_id, "action-expense", "fresh-preparation"),
    ):
        with pytest.raises(LifecycleError):
            action()
    reply = "change amount to SGD 14.00"
    port.register_source("new-reply", reply)
    expected = service.expected_invocation(
        source.source_id, proposal.version, reply_evidence_id="new-reply"
    )
    port.register_provider(
        "new-observation",
        expected,
        response(reply, "reply", {"amount": "14.00", "currency": "SGD"}),
    )
    next_proposal = service.propose(
        source.source_id,
        "new-observation",
        proposal.version,
        "new-proposal",
        reply_evidence_id="new-reply",
    )
    next_review = service.prepare_review(next_proposal.proposal_id, next_proposal.version)
    port.register_delivery(service, next_review, "new-delivery")
    service.record_delivery(next_review.review_id, "new-delivery")
    port.register_action(service, next_review, "new-delivery", "new-action")
    # The cancelled projected tail never became source history.
    with pytest.raises(LifecycleError, match="actual committed source history"):
        api.prepare(next_review.review_id, "new-action", "new-finalize", previous_packages=packages)
    replacement = api.prepare(next_review.review_id, "new-action", "new-finalize")
    assert replacement.economic_event_id == ref.economic_event_id
    assert replacement.version == 1
    api.apply_offline("new-finalize", expected_intent_sha256=replacement.intent_sha256)
    assert len(EffectiveLedgerReaderV1(service).collect().facts) == 1


def test_expired_prepare_keeps_pending_and_can_cancel_but_historical_apply_replays(live):
    service, _, _, _, api = live
    _, _, _, ref = prepare(live)
    service.clock = lambda: 1792800600
    with pytest.raises(LifecycleError, match="binding or was altered"):
        api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    assert api.inspect("finalize").state == "PREPARED_OFFLINE"
    assert (
        api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256).state
        == "CANCELLED_OFFLINE"
    )


@pytest.mark.parametrize(
    "damage", ["missing-action", "false-authenticator", "wrong-action", "wrong-hash"]
)
def test_preparation_refuses_untrusted_input_without_partial_rows(live, damage):
    service, port, _, _, api = live
    _, _, review = stage(service, port)
    before = native_rows(service)
    if damage == "missing-action":
        del port.archives[("action", "action-expense")]
    if damage == "false-authenticator":
        api.authenticator = type("FalseVerifier", (), {"verify": lambda self, **kwargs: False})()
    action = "nonexistent-action" if damage == "wrong-action" else "action-expense"
    if damage == "wrong-hash":
        ref = api.prepare(review.review_id, action, "finalize")
        for operation in (api.material, api.apply_offline, api.cancel_offline):
            with pytest.raises(LifecycleError):
                operation("finalize", expected_intent_sha256="0" * 64)
        assert api.inspect("finalize").prepared == ref
        return
    with pytest.raises((LifecycleError, KeyError)):
        api.prepare(review.review_id, action, "finalize")
    assert native_rows(service) == before
    with lifecycle_read_operation(service.owner) as conn:
        assert conn.execute("SELECT count(*) FROM l2_finalization_intents").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM l2_finalization_materials").fetchone()[0] == 0


def test_apply_then_cancel_loses_and_expired_historical_receipt_stays_readable(live):
    service, _, _, _, api = live
    _, _, review, ref = prepare(live)
    receipt = api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    service.clock = lambda: 1792809999
    assert api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256) == receipt
    assert service.confirm(review.review_id, "action-expense", "finalize") == receipt
    with pytest.raises(LifecycleError, match="cannot be cancelled"):
        api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    with pytest.raises(LifecycleError):
        api.prepare(review.review_id, "action-expense", "finalize-old")


def test_prepared_material_rebuilds_after_source_database_and_port_destroyed(live):
    from pathlib import Path

    service, port, cfg, authenticator, api = live
    _, _, _, ref = prepare(live)
    packages = api.material("finalize", expected_intent_sha256=ref.intent_sha256)
    tail = decode_native_closure_v2(packages[-1])
    identity = tail.identity
    frontier = ClosureFrontierV2(
        hashlib.sha256(packages[-1]).hexdigest(), tail.last_event_seq, tail.last_event_sha256
    )
    source = Path(cfg._binding["profile_root"]) / "runtime/database/finance.db"
    service.owner.close()
    port.destroy()
    source.unlink()
    cfg.close()
    assert not source.exists()
    checked = check_native_closure_v2(
        packages,
        authenticator=authenticator,
        expected_identity=identity,
        expected_frontier=frontier,
    )
    assert checked.current_values[0][0] == ref.record_id
    assert checked.current_values[0][3] == ref.fact_sha256


def test_preparation_apply_failure_rolls_back_native_rows_and_outcome(live, monkeypatch):
    import finance_core.lifecycle_v2.prepared_finalization_v1 as module

    service, _, _, _, api = live
    _, _, _, ref = prepare(live)
    before = native_rows(service)
    append = module._append_finalization

    def fail_after_append(conn, plan):
        append(conn, plan)
        raise RuntimeError("injected between native append and outcome")

    monkeypatch.setattr(module, "_append_finalization", fail_after_append)
    with pytest.raises(RuntimeError, match="between native"):
        api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    assert native_rows(service) == before
    assert api.inspect("finalize").state == "PREPARED_OFFLINE"
    monkeypatch.setattr(module, "_append_finalization", append)
    assert (
        api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256).fact_sha256
        == ref.fact_sha256
    )


def test_preparation_commit_failure_rolls_back_material_and_intent(live, monkeypatch):
    import finance_core.instance_admission.sessions as sessions

    service, port, _, _, api = live
    _, _, review = stage(service, port)
    original = sessions._ProductionConnection.commit

    def fail_commit(conn):
        raise RuntimeError("injected before prepare commit")

    monkeypatch.setattr(sessions._ProductionConnection, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="before prepare commit"):
        api.prepare(review.review_id, "action-expense", "finalize")
    monkeypatch.setattr(sessions._ProductionConnection, "commit", original)
    assert api.inspect("finalize").state == "ABSENT_LOCAL"
    with lifecycle_read_operation(service.owner) as conn:
        assert conn.execute("SELECT count(*) FROM l2_finalization_materials").fetchone()[0] == 0
    assert api.prepare(review.review_id, "action-expense", "finalize").operation_id == "finalize"


def test_real_new_process_reopens_same_preparation_and_applies_once(live, tmp_path):
    import json
    import os
    import pickle
    import subprocess
    import sys
    from pathlib import Path

    service, port, cfg, _, api = live
    _, _, review, ref = prepare(live)
    packages = api.material("finalize", expected_intent_sha256=ref.intent_sha256)
    values = {
        name: getattr(cfg, name)
        for name in (
            "control_root",
            "locator_root",
            "profile_id",
            "layout",
            "instance_id",
            "config_identity",
            "core_identity",
        )
    }
    fixture = tmp_path / "synthetic-restart.pickle"
    fixture.write_bytes(pickle.dumps((values, port, service.binding, ref, packages)))
    service.owner.close()
    script = r"""
import json, os, pickle, sys
from dataclasses import asdict
from pathlib import Path
from finance_core.instance_admission import TrustedInstanceConfig, acquire_production_owner
from finance_core.lifecycle_v2 import LedgerService
from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
values,port,binding,ref,packages=pickle.loads(Path(sys.argv[1]).read_bytes())
from finance_core.profile_layout import profile_root
os.environ['FINANCE_RUNTIME_ROOT']=str(profile_root(values['locator_root'],values['profile_id'],values['layout'])/'runtime')
config=TrustedInstanceConfig(**values,approval_verifier=lambda *args:True)
owner=acquire_production_owner(config)
service=LedgerService(owner,port,binding,clock=lambda:1792800000)
api=PreparedFinalizationServiceV1(service,archive_source=port,authenticator=port.authenticator)
assert api.inspect(ref.operation_id).prepared==ref
assert api.prepare(ref.review_id,ref.action_evidence_id,ref.operation_id)==ref
assert api.material(ref.operation_id,expected_intent_sha256=ref.intent_sha256)==packages
receipt=api.apply_offline(ref.operation_id,expected_intent_sha256=ref.intent_sha256)
assert receipt.record_id==ref.record_id and receipt.fact_sha256==ref.fact_sha256
assert api.apply_offline(ref.operation_id,expected_intent_sha256=ref.intent_sha256)==receipt
print(json.dumps(asdict(receipt),sort_keys=True))
owner.close();config.close()
"""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "tests"), str(root))))
    result = subprocess.run(
        [sys.executable, "-c", script, str(fixture)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    service.owner = acquire_production_owner(cfg)
    state = api.inspect("finalize")
    assert state.state == "APPLIED_OFFLINE"
    assert json.loads(result.stdout) == asdict(state.receipt)
    assert service.confirm(review.review_id, "action-expense", "finalize") == state.receipt


@pytest.mark.parametrize("phase", ["prepare", "apply", "cancel"])
@pytest.mark.parametrize("fault", ["commit-after", "close", "cleanup"])
def test_unknown_commit_actual_close_and_cleanup_never_release_result(tmp_path, phase, fault):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import os,sqlite3,sys
from pathlib import Path
from lifecycle_recovery_material_support_v1 import make_service,stage_expense
from finance_core.instance_admission import InstanceAdmissionError, acquire_production_owner
from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
import finance_core.instance_admission.sessions as sessions
class Environment:
    def setenv(self,key,value): os.environ[key]=value
service,port,cfg,auth=make_service(Path(sys.argv[1]),Environment())
_,_,review,_=stage_expense(service,port,'expense',stage='delivery')
port.register_action(service,review,'delivery-expense','action-expense')
api=PreparedFinalizationServiceV1(service,archive_source=port,authenticator=auth)
phase,fault=sys.argv[2:]
if phase!='prepare': ref=api.prepare(review.review_id,'action-expense','finalize')
original_commit=sessions._ProductionConnection.commit
if fault=='commit-after':
    def fail(conn):
        original_commit(conn)
        raise sqlite3.OperationalError('injected after actual COMMIT')
    sessions._ProductionConnection.commit=fail
elif fault=='close':
    def fail(*args): raise sqlite3.OperationalError('injected actual close')
    sessions._close_sqlite_handle=fail
elif fault=='cursor-close':
    def fail(*args): raise sqlite3.OperationalError('injected actual cursor close')
    sessions._close_cursor_handle=fail
elif fault=='rollback':
    def before_commit(conn): raise RuntimeError('injected before actual COMMIT')
    def fail(*args): raise sqlite3.OperationalError('injected actual rollback')
    sessions._ProductionConnection.commit=before_commit
    sessions._rollback_sqlite_handle=fail
else:
    original=sessions.GateLease.close
    def fail(gate): raise RuntimeError('injected gate cleanup')
    sessions.GateLease.close=fail
try:
    if phase=='prepare': api.prepare(review.review_id,'action-expense','finalize')
    elif phase=='apply': api.apply_offline('finalize',expected_intent_sha256=ref.intent_sha256)
    else: api.cancel_offline('finalize',expected_intent_sha256=ref.intent_sha256)
except (InstanceAdmissionError,RuntimeError): pass
else: raise AssertionError('Result escaped uncertain commit/close/cleanup')
assert service.owner._uncertain
for action in (service.owner.close,lambda:acquire_production_owner(cfg,timeout_seconds=0)):
    try: action()
    except InstanceAdmissionError: pass
    else: raise AssertionError('Unknown owner custody was released')
os._exit(0)
"""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "tests"), str(root))))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), phase, fault],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_binding_cannot_resolve_original_pending_intent(live):
    from dataclasses import replace

    service, port, _, auth, api = live
    _, _, _, ref = prepare(live)
    other = LedgerService(
        service.owner,
        port,
        replace(service.binding, generation="other-generation"),
        clock=service.clock,
    )
    resolver = PreparedFinalizationServiceV1(other, archive_source=port, authenticator=auth)
    assert resolver.inspect("finalize").prepared == ref
    for action in (resolver.apply_offline, resolver.cancel_offline):
        with pytest.raises(LifecycleError, match="another runtime"):
            action("finalize", expected_intent_sha256=ref.intent_sha256)
    assert api.inspect("finalize").state == "PREPARED_OFFLINE"


def test_pending_complete_cut_is_readable_but_both_promotions_and_resume_stay_held(
    live, tmp_path, monkeypatch
):
    from test_lifecycle_v2_round3_recovery import _cut, _fence, _target

    from finance_core.instance_admission import inspect_instance_admission
    from finance_core.lifecycle_v2.recovery import (
        RecoveryAuthority,
        promote_restored_profile,
        promote_with_isolation_receipt,
        resume_restore_promotion,
        verify_held_profile_evidence,
        verify_held_profile_evidence_content,
    )

    service, port, old, _, _ = live
    prepare(live)
    cut, path = _cut(service, tmp_path)
    _fence(old, service.owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    try:
        recovery = RecoveryAuthority("synthetic-preparation-recovery", b"r" * 32)
        receipt = recovery.issue_old_writer_isolation(
            old, held, operation_id="designate", approval_ref="approved"
        )
        verify_held_profile_evidence(held, port, binding)
        verify_held_profile_evidence_content(held)
        operations = (
            lambda: promote_with_isolation_receipt(
                held,
                receipt,
                recovery,
                operation_id="promote",
                approval_ref="approved",
                port=port,
                binding=binding,
            ),
            lambda: promote_restored_profile(
                held,
                old,
                designation_operation_id="designate",
                designation_approval_ref="approved",
                operation_id="promote",
                approval_ref="approved",
                port=port,
                binding=binding,
            ),
            lambda: resume_restore_promotion(
                held,
                operation_id="promote",
                approval_ref="approved",
                port=port,
                binding=binding,
                receipt=receipt,
                recovery_authority=recovery,
            ),
        )
        for operation in operations:
            with pytest.raises(LifecycleError, match="keeps recovery held"):
                operation()
            assert inspect_instance_admission(new)["state"] == "RESTORE_HELD"
    finally:
        new.close()


def test_correction_material_and_current_facts_keep_one_economic_event(live):
    from lifecycle_recovery_material_support_v1 import response

    service, port, _, auth, api = live
    source, _, _, ref = prepare(live)
    api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    first = api.material("finalize", expected_intent_sha256=ref.intent_sha256)
    reply = "change amount to SGD 15.00\n原因：记录有误"
    port.register_source("correction-reply", reply)
    expected = service.expected_invocation(
        source.source_id, ref.version, ref.record_id, "correction-reply", "记录有误"
    )
    port.register_provider(
        "correct-observation",
        expected,
        response(reply, "reply", {"amount": "15.00", "currency": "SGD"}),
    )
    review = service.prepare_correction(
        ref.record_id,
        ref.version,
        "correct-observation",
        "correct-proposal",
        reply_evidence_id="correction-reply",
        reason="记录有误",
    )
    port.register_delivery(service, review, "correct-delivery")
    service.record_delivery(review.review_id, "correct-delivery")
    port.register_action(service, review, "correct-delivery", "correct-action")
    changed = api.prepare(
        review.review_id, "correct-action", "correct-finalize", previous_packages=first
    )
    assert changed.record_id == ref.record_id and changed.economic_event_id == ref.economic_event_id
    assert changed.version == 2
    assert EffectiveLedgerReaderV1(service).collect().facts[0].version == 1
    packages = api.material("correct-finalize", expected_intent_sha256=changed.intent_sha256)
    tail = decode_native_closure_v2(packages[-1])
    result = check_native_closure_v2(
        packages,
        authenticator=auth,
        expected_identity=tail.identity,
        expected_frontier=ClosureFrontierV2(
            hashlib.sha256(packages[-1]).hexdigest(), tail.last_event_seq, tail.last_event_sha256
        ),
    )
    assert result.inventory.fact_version_count == 2
    assert len(result.current_values) == 1 and result.current_values[0][2] == 2
    api.apply_offline("correct-finalize", expected_intent_sha256=changed.intent_sha256)
    effective = EffectiveLedgerReaderV1(service).collect()
    assert len(effective.facts) == 1 and effective.facts[0].version == 2


def test_ordinary_control_table_ddl_targets_are_refused(live):
    import sqlite3

    service, _, _, _, _ = live
    statements = (
        "CREATE INDEX forbidden_control_idx ON l2_finalization_intents(review_id)",
        "CREATE TRIGGER forbidden_control_trigger AFTER INSERT ON l2_finalization_intents "
        "BEGIN SELECT 1; END",
        "DROP TRIGGER l2_finalization_intents_no_update",
        "ALTER TABLE l2_finalization_intents ADD COLUMN unsafe TEXT",
    )
    with production_write_operation(service.owner) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute(statement)
        # Existing non-control DDL behavior remains transactional and usable.
        conn.execute("CREATE INDEX temporary_normal_index ON l2_sources(created_at)")
        conn.rollback()


def test_completed_promotion_replay_audits_current_journal_after_lawful_correction(
    live, tmp_path, monkeypatch
):
    from lifecycle_recovery_material_support_v1 import correct_expense
    from test_lifecycle_v2_round3_recovery import _cut, _fence, _target

    from finance_core.instance_admission import authority
    from finance_core.lifecycle_v2.contracts import canonical
    from finance_core.lifecycle_v2.recovery import RecoveryAuthority, promote_with_isolation_receipt

    service, port, old, _, api = live
    source, _, _, ref = prepare(live)
    original = api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    cut, path = _cut(service, tmp_path)
    _fence(old, service.owner)
    new, binding, held = _target(tmp_path, monkeypatch, cut, path)
    target_owner = None
    try:
        recovery = RecoveryAuthority("synthetic-completed-replay", b"r" * 32)
        receipt = recovery.issue_old_writer_isolation(
            old, held, operation_id="designate", approval_ref="approved"
        )
        kwargs = {
            "operation_id": "promote",
            "approval_ref": "approved",
            "port": port,
            "binding": binding,
        }
        assert (
            promote_with_isolation_receipt(held, receipt, recovery, **kwargs)["state"] == "ACTIVE"
        )
        target_owner = acquire_production_owner(new)
        target = LedgerService(target_owner, port, binding, clock=lambda: 1792800000)
        _, corrected = correct_expense(target, port, source, original)
        assert corrected.version == 2
        target_owner.close()
        before = canonical(authority._state(new))
        assert (
            promote_with_isolation_receipt(held, receipt, recovery, **kwargs)["state"] == "ACTIVE"
        )
        assert canonical(authority._state(new)) == before
    finally:
        if target_owner is not None and target_owner._lease.fd >= 0:
            target_owner.close()
        new.close()


def test_terminal_journal_limits_do_not_cap_later_actual_native_growth(live):
    from finance_core.lifecycle_v2._finalization_journal_v1 import _verify_journal
    from finance_core.lifecycle_v2.recovery_material_v1 import ClosureLimitsV1

    service, port, _, _, api = live
    _, _, _, ref = prepare(live)
    api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    # This terminal's projected chain has five events; later ordinary captures
    # are outside that retained historical prefix and its material capacity.
    for index in range(3):
        key = "later-" + str(index)
        port.register_source(key, "dormant later source " + str(index))
        service.capture(key, "capture-" + key)
    with lifecycle_read_operation(service.owner) as conn:
        conn.execute("BEGIN")
        try:
            journal = _verify_journal(conn, limits=ClosureLimitsV1(max_events=5))
            assert journal["finalize"].outcome == "APPLIED_OFFLINE"
        finally:
            conn.rollback()
    assert api.inspect("finalize").prepared == ref


@pytest.mark.parametrize(("phase", "fault"), [("prepare", "cursor-close"), ("apply", "rollback")])
def test_preparation_cursor_and_rollback_unknown_retain_custody(tmp_path, phase, fault):
    test_unknown_commit_actual_close_and_cleanup_never_release_result(tmp_path, phase, fault)


@pytest.mark.parametrize("writer", ["capture", "propose"])
def test_cancelled_operation_is_reserved_at_every_native_event_append(live, writer):
    from lifecycle_recovery_material_support_v1 import response

    service, port, _, _, api = live
    _, _, _, ref = prepare(live)
    assert (
        api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256).state
        == "CANCELLED_OFFLINE"
    )
    if writer == "capture":
        port.register_source("later", "2026-10-24 Cafe SGD 9.00")

        def append(operation):
            return service.capture("later", operation)
    else:
        source, _, _, _ = stage_expense(service, port, "later", amount="9.00", stage="capture")
        expected = service.expected_invocation(source.source_id, 0)
        port.register_provider(
            "later-observation",
            expected,
            response(
                "2026-10-24 Cafe SGD 9.00",
                fields={
                    "amount": "9.00",
                    "currency": "SGD",
                    "merchant": "Cafe",
                    "transaction_date": "2026-10-24",
                },
            ),
        )

        def append(operation):
            return service.propose(source.source_id, "later-observation", 0, operation)

    before = native_rows(service)
    with pytest.raises(LifecycleError, match="reserved"):
        append("finalize")
    assert native_rows(service) == before
    assert api.inspect("finalize").state == "CANCELLED_OFFLINE"
    append("later-" + writer)
    assert api.inspect("finalize").state == "CANCELLED_OFFLINE"


def test_reserved_event_sql_guard_is_exact_pending_only_and_rejects_cancelled_reuse(live):
    import json
    import sqlite3

    from finance_core.instance_admission.sessions import _prepared_finalization_operation

    service, _, _, _, api = live
    _, _, _, ref = prepare(live)
    before = native_rows(service)
    with lifecycle_read_operation(service.owner) as conn:
        body = json.loads(
            conn.execute(
                "SELECT body_json FROM l2_finalization_intents WHERE operation_id=?", ("finalize",)
            ).fetchone()[0]
        )
    columns = (
        "seq",
        "operation_id",
        "kind",
        "entity_id",
        "payload_json",
        "previous_sha256",
        "event_sha256",
    )
    planned = tuple(body["append"]["event"][column] for column in columns)
    assert planned[5] is not None
    mutations = (
        (0, planned[0] + 1),
        (2, "capture"),
        (3, "different-record"),
        (4, planned[4] + " "),
        (5, None),
        (6, "f" * 64),
    )
    with _prepared_finalization_operation(service.owner, "finalize", "apply") as conn:
        conn.execute("BEGIN IMMEDIATE")
        for verb in ("INSERT", "INSERT OR IGNORE", "INSERT OR REPLACE"):
            for index, value in mutations:
                changed = list(planned)
                changed[index] = value
                with pytest.raises(sqlite3.IntegrityError, match="reserved"):
                    conn.execute(verb + " INTO l2_events VALUES(?,?,?,?,?,?,?)", changed)
        conn.rollback()
    assert native_rows(service) == before
    assert api.inspect("finalize").state == "PREPARED_OFFLINE"
    api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    with production_write_operation(service.owner) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for verb in ("INSERT", "INSERT OR IGNORE", "INSERT OR REPLACE"):
            with pytest.raises(sqlite3.IntegrityError, match="reserved"):
                conn.execute(verb + " INTO l2_events VALUES(?,?,?,?,?,?,?)", planned)
        conn.rollback()
    assert native_rows(service) == before
    assert api.inspect("finalize").state == "CANCELLED_OFFLINE"


def test_reserved_event_guard_survives_ordinary_schema_operations(live):
    import sqlite3

    service, _, _, _, api = live
    _, _, _, ref = prepare(live)
    api.cancel_offline("finalize", expected_intent_sha256=ref.intent_sha256)
    before = native_rows(service)
    statements = (
        "DROP TRIGGER l2_events_finalization_reservation",
        "DROP TRIGGER l2_events_no_update",
        "DROP TRIGGER l2_events_no_delete",
        "ALTER TABLE l2_events RENAME TO unrelated_event_table",
        "DROP TABLE l2_events",
        "CREATE TRIGGER unrelated_event_trigger BEFORE INSERT ON l2_events BEGIN SELECT 1; END",
        "CREATE INDEX unrelated_event_index ON l2_events(kind)",
        "DROP INDEX l2_events_entity",
    )
    with production_write_operation(service.owner) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute(statement)
        conn.execute("CREATE INDEX temporary_source_index ON l2_sources(created_at)")
        conn.rollback()
    assert native_rows(service) == before
    assert api.inspect("finalize").state == "CANCELLED_OFFLINE"


def test_reserved_event_schema_guard_uses_sqlite_canonical_names(live):
    import sqlite3

    service, _, _, _, _ = live
    statements = (
        "DROP TRIGGER MAIN.L2_EVENTS_FINALIZATION_RESERVATION",
        'DROP TRIGGER "main"."l2_events_no_update"',
        "ALTER TABLE MAIN.L2_EVENTS RENAME TO unrelated_event_table",
        "DROP INDEX MAIN.L2_EVENTS_ENTITY",
        "CREATE INDEX main.UNRELATED_EVENT_INDEX ON L2_EVENTS(kind)",
    )
    with production_write_operation(service.owner) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute(statement)
        conn.execute("CREATE INDEX main.UNRELATED_SOURCE_INDEX ON L2_SOURCES(created_at)")
        conn.rollback()
