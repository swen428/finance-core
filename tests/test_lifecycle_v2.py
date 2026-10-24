"""Synthetic proof tests through actual current-instance guarded sessions."""

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from instance_admission_support_v1 import synthetic_config

from finance_core.instance_admission import (
    acquire_production_owner,
    admit_production_instance,
    fence_production_instance,
    inspect_instance_admission,
    prepare_production_profile,
)
from finance_core.lifecycle_v2 import (
    LedgerService,
    LifecycleError,
    RuntimeBinding,
    VerifiedAction,
    VerifiedDelivery,
    VerifiedObservation,
    VerifiedSource,
)
from finance_core.lifecycle_v2.contracts import digest

TEXT = "2026-10-24 Cafe SGD 12.00"


def response(text=TEXT, ref="source", fields=None):
    fields = fields or {
        "amount": "12.00",
        "currency": "SGD",
        "merchant": "Cafe",
        "transaction_date": "2026-10-24",
    }
    operations = []
    for i, (field, value) in enumerate(fields.items()):
        quote = value
        op = {
            "operation_id": f"o{i}",
            "op": "set",
            "field": field,
            "evidence": [
                {
                    "ref": ref,
                    "start": text.index(quote),
                    "end": text.index(quote) + len(quote),
                    "quote": quote,
                    "role": "replacement_value",
                }
            ],
        }
        if field == "transaction_date":
            op["date_expression"] = {"kind": "absolute_date", "year": 2026, "month": 10, "day": 24}
        else:
            op["value"] = value
        operations.append(op)
    return json.dumps(
        {
            "schema": "finance-independent-ai-result-v2",
            "outcome": "proposal",
            "intent": "personal_expense",
            "operations": operations,
            "unresolved": [],
            "coverage": [
                {
                    "start": 0,
                    "end": len(text),
                    "label": "resolved_instruction",
                    "operation_ids": [o["operation_id"] for o in operations],
                    "question_ids": [],
                }
            ],
        }
    )


class Port:
    def __init__(self):
        self.sources = {
            "source1": VerifiedSource(
                "source1",
                "wen",
                "chat",
                "message-1",
                TEXT,
                hashlib.sha256(TEXT.encode()).hexdigest(),
                1792800000,
            )
        }
        self.bad_actor = False
        self.raw = response()
        self.observations = {}

    def source(self, conn, id):
        return self.sources[id]

    def provider(self, conn, id, expected):
        if id not in self.observations:
            self.observations[id] = VerifiedObservation(
                id,
                expected.source_id,
                "wen",
                self.raw,
                digest(asdict(expected)),
                expected.projection["reply_text"],
            )
        return self.observations[id]

    def delivery(self, conn, id, expected):
        return VerifiedDelivery(id, expected.review_id, expected.review_sha256, "wen", "chat")

    def action(self, conn, id, expected):
        return VerifiedAction(
            id,
            expected.review_id,
            expected.review_sha256,
            expected.delivery_evidence_id,
            "other" if self.bad_actor else "wen",
            "chat",
        )


@pytest.fixture
def live(tmp_path, monkeypatch):
    cfg = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
    owner = acquire_production_owner(cfg)
    port = Port()
    service = LedgerService(
        owner,
        port,
        RuntimeBinding(cfg.instance_id, "gen1", "wen", "chat"),
        clock=lambda: 1792800000,
    )
    yield service, port, cfg
    owner.close()
    cfg.close()


def ready(service):
    source = service.capture("source1", "capture1")
    proposal = service.propose(source.source_id, "obs1", 0, "propose1")
    review = service.prepare_review(proposal.proposal_id, proposal.version)
    service.record_delivery(review.review_id, "delivery1")
    return source, proposal, review


def test_actual_owner_posting_replay_wrong_actor_and_audit(live, tmp_path):
    service, port, cfg = live
    source, proposal, review = ready(service)
    port.bad_actor = True
    with pytest.raises(LifecycleError, match="actor"):
        service.confirm(review.review_id, "action1", "confirm1")
    port.bad_actor = False
    receipt = service.confirm(review.review_id, "action1", "confirm1")
    assert service.confirm(review.review_id, "action1", "confirm1") == receipt
    with pytest.raises(Exception):
        service.confirm(review.review_id, "action1", "confirm-other")
    assert service.reader().get(receipt.record_id)["values"]["amount"] == "12.00"
    assert service.reader().full_audit()["fact_version_count"] == 1
    assert service.capture("source1", "capture1") == source
    assert service.propose(source.source_id, "obs1", 0, "propose1") == proposal
    cut = service.seal_snapshot(tmp_path / "snapshot.sqlite")
    assert cut.facts == ((receipt.record_id, 1, receipt.fact_sha256),)
    assert inspect_instance_admission(cfg)["state"] == "ACTIVE"


def test_correction_is_new_version_and_old_review_cannot_post(live):
    service, port, _ = live
    source, _, review = ready(service)
    posted = service.confirm(review.review_id, "action1", "confirm1")
    text = "change amount to SGD 15.00；原因：记录有误"
    port.sources["edit1"] = VerifiedSource(
        "edit1",
        "wen",
        "chat",
        "message-edit",
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        1792800000,
    )
    port.raw = response(text, "reply", {"amount": "15.00", "currency": "SGD"})
    correction = service.prepare_correction(
        posted.record_id,
        1,
        "obs2",
        "correction1",
        reply_evidence_id="edit1",
        reason="记录有误",
    )
    service.record_delivery(correction.review_id, "delivery2")
    receipt = service.confirm(correction.review_id, "action2", "confirm2")
    assert receipt.version == 2
    assert service.reader().get(posted.record_id)["values"]["amount"] == "15.00"
    assert service.reader().full_audit()["fact_version_count"] == 2
    assert len(service.reader().recent()) == 1
    with pytest.raises(LifecycleError):
        service.confirm(review.review_id, "another", "another")


def test_source_tampering_and_offline_profile_cannot_escalate(live):
    service, port, _ = live
    source, _, _ = ready(service)
    port.sources["source1"] = replace(port.sources["source1"], raw_input=TEXT + "tampered")
    with pytest.raises(LifecycleError, match="changed"):
        service.expected_invocation(source.source_id, 1)
    with pytest.raises(LifecycleError):
        RuntimeBinding("i", "g", "p", "c", "production")


def test_restore_is_held_until_actual_old_fence(live, tmp_path, monkeypatch):
    from finance_core.lifecycle_v2.recovery import (
        prepare_restored_profile,
        promote_restored_profile,
    )

    service, _, old_config = live
    cut = service.seal_snapshot(tmp_path / "restore.sqlite")
    other = tmp_path / "second"
    other.mkdir(mode=0o700)
    new_config = synthetic_config(other, monkeypatch, instance_id="new-instance")
    restored = prepare_restored_profile(
        new_config,
        tmp_path / "restore.sqlite",
        cut,
        operation_id="restore-prepare",
        approval_ref="approved",
    )
    assert restored.status == "RESTORE_HELD"
    assert inspect_instance_admission(new_config)["state"] == "RESTORE_HELD"
    with pytest.raises(Exception):
        admit_production_instance(new_config, operation_id="bypass", approval_ref="approved")
    with pytest.raises(LifecycleError, match="fenced"):
        promote_restored_profile(
            restored, old_config, operation_id="new-admit", approval_ref="approved"
        )
    from finance_core.profile_layout import profile_root

    monkeypatch.setenv(
        "FINANCE_RUNTIME_ROOT",
        str(profile_root(old_config.locator_root, "synthetic", old_config.layout) / "runtime"),
    )
    fence_production_instance(old_config, operation_id="actual-old-fence", approval_ref="approved")
    monkeypatch.setenv(
        "FINANCE_RUNTIME_ROOT",
        str(profile_root(new_config.locator_root, "synthetic", new_config.layout) / "runtime"),
    )
    result = promote_restored_profile(
        restored, old_config, operation_id="new-admit", approval_ref="approved"
    )
    assert result["state"] == "ACTIVE"
    new_config.close()


def test_operation_reuses_only_immutable_artifacts_and_keeps_dynamic_authority(live, monkeypatch):
    from finance_core.instance_admission import authority
    from finance_core.instance_admission.sessions import lifecycle_write_operation

    service, _, _ = live
    original = authority._migration_contract_digest
    calls = []

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(authority, "_migration_contract_digest", counted)
    with lifecycle_write_operation(service.owner) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SELECT count(*) FROM l2_events").fetchone()
        conn.execute("SELECT count(*) FROM l2_sources").fetchone()
        assert len(calls) == 1
        from finance_core.instance_admission import sessions

        original_state = sessions._state

        def fenced(config):
            state = original_state(config)
            state["instances"][config.binding_sha256]["state"] = "FENCED"
            return state

        monkeypatch.setattr(sessions, "_state", fenced)
        with pytest.raises(Exception, match="ACTIVE"):
            conn.execute("SELECT 1").fetchone()
        monkeypatch.setattr(sessions, "_state", original_state)
    with lifecycle_write_operation(service.owner):
        pass
    assert len(calls) == 2


def test_reader_rejects_self_consistent_rehashed_unapproved_fact(live):
    import sqlite3

    from finance_core.lifecycle_v2.contracts import canonical
    from finance_core.lifecycle_v2.service import _fact_material
    from finance_core.profile_layout import profile_root

    service, _, cfg = live
    _, _, review = ready(service)
    receipt = service.confirm(review.review_id, "action1", "confirm1")
    db = profile_root(cfg.locator_root, cfg.profile_id, cfg.layout) / "runtime/database/finance.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    triggers = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN "
        "('l2_facts_no_update','l2_events_no_update')"
    ).fetchall()
    for trigger in triggers:
        conn.execute("DROP TRIGGER " + trigger["name"])
    fact = conn.execute("SELECT * FROM l2_facts WHERE record_id=?", (receipt.record_id,)).fetchone()
    values = json.loads(fact["values_json"])
    values["amount"] = "999.00"
    sha = digest(
        _fact_material(
            fact["record_id"], 1, fact["economic_event_id"], fact["source_id"], values, None
        )
    )
    conn.execute(
        "UPDATE l2_facts SET values_json=?,fact_sha256=? WHERE record_id=?",
        (canonical(values), sha, receipt.record_id),
    )
    event = conn.execute("SELECT * FROM l2_events WHERE seq=?", (fact["commit_seq"],)).fetchone()
    payload = json.loads(event["payload_json"])
    payload["fact_sha256"] = sha
    eventsha = digest(
        [
            event["seq"],
            event["operation_id"],
            event["kind"],
            event["entity_id"],
            payload,
            event["previous_sha256"],
        ]
    )
    conn.execute(
        "UPDATE l2_events SET payload_json=?,event_sha256=? WHERE seq=?",
        (canonical(payload), eventsha, event["seq"]),
    )
    for trigger in triggers:
        conn.execute(trigger["sql"])
    conn.commit()
    conn.close()
    with pytest.raises(LifecycleError, match="confirmed review"):
        service.reader().get(receipt.record_id)


def test_signed_fence_receipt_allows_restore_without_old_root(tmp_path, monkeypatch):
    from finance_core.lifecycle_v2.recovery import (
        OldWriterIsolationReceipt,
        RecoveryAuthority,
        prepare_restored_profile,
        promote_with_isolation_receipt,
    )

    oldroot = tmp_path / "old"
    oldroot.mkdir(mode=0o700)
    cfg = synthetic_config(oldroot, monkeypatch)
    prepare_production_profile(cfg, operation_id="prepare-old", approval_ref="approved").close()
    admit_production_instance(cfg, operation_id="admit-old", approval_ref="approved")
    owner = acquire_production_owner(cfg)
    service = LedgerService(
        owner,
        Port(),
        RuntimeBinding(cfg.instance_id, "g1", "wen", "chat"),
        clock=lambda: 1792800000,
    )
    _, _, review = ready(service)
    posted = service.confirm(review.review_id, "action", "commit")
    cut = service.seal_snapshot(tmp_path / "sealed.sqlite")
    fence_production_instance(cfg, operation_id="fence-old", approval_ref="approved")
    owner.close()
    auth = RecoveryAuthority("independent-recovery", b"r" * 32)
    isolation = auth.issue_old_writer_isolation(cfg, cut, new_instance_id="new-restore")
    cfg.close()
    oldroot.rename(tmp_path / "unavailable-old")
    newroot = tmp_path / "new"
    newroot.mkdir(mode=0o700)
    new = synthetic_config(newroot, monkeypatch, instance_id="new-restore")
    held = prepare_restored_profile(
        new, tmp_path / "sealed.sqlite", cut, operation_id="held", approval_ref="approved"
    )
    with pytest.raises(LifecycleError, match="signature"):
        promote_with_isolation_receipt(
            held,
            OldWriterIsolationReceipt(isolation.material_json, "0" * 64),
            auth,
            operation_id="bad",
            approval_ref="approved",
        )
    from finance_core.lifecycle_v2.recovery import verify_held_profile_evidence

    new_binding = RuntimeBinding(new.instance_id, "g2", "wen", "chat")
    with pytest.raises(LifecycleError, match="held source/provider/action"):
        promote_with_isolation_receipt(
            held, isolation, auth, operation_id="early", approval_ref="approved"
        )
    verify_held_profile_evidence(held, service.port, new_binding)
    promote_with_isolation_receipt(
        held, isolation, auth, operation_id="promote", approval_ref="approved"
    )
    restored_owner = acquire_production_owner(new)
    # Actual retained proof port must remain available independently of oldroot.
    restored_service = LedgerService(
        restored_owner, service.port, RuntimeBinding(new.instance_id, "g2", "wen", "chat")
    )
    assert restored_service.reader().get(posted.record_id)["values"]["amount"] == "12.00"
    restored_owner.close()
    new.close()


def test_expired_review_and_wrong_proposal_version_fail_closed(live):
    service, _, _ = live
    _, proposal, review = ready(service)
    with pytest.raises(LifecycleError, match="version"):
        service.prepare_review(proposal.proposal_id, 2)
    service.clock = lambda: 1792800600
    with pytest.raises(LifecycleError, match="altered"):
        service.confirm(review.review_id, "action-expired", "confirm-expired")
    assert service.reader().recent() == []
    assert service.reader().full_audit()["fact_version_count"] == 0


def test_concurrent_edit_invalidates_old_card_and_retains_unused_history(live):
    service, port, _ = live
    source, proposal, review = ready(service)
    text = "change amount to SGD 13.00"
    port.sources["edit1"] = VerifiedSource(
        "edit1",
        "wen",
        "chat",
        "edit-message",
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        1792800000,
    )
    port.raw = response(text, "reply", {"amount": "13.00", "currency": "SGD"})
    changed = service.propose(
        source.source_id, "edit-observation", 1, "edit-propose", reply_evidence_id="edit1"
    )
    with pytest.raises(LifecycleError, match="Stale"):
        service.confirm(review.review_id, "old-action", "old-confirm")
    with pytest.raises(LifecycleError, match="Concurrent"):
        service.expected_invocation(source.source_id, 1, reply_evidence_id="edit1")
    latest = service.prepare_review(changed.proposal_id, changed.version)
    assert [r.review_id for r in service.reader().pending()] == [latest.review_id]
    assert service.reader().full_audit()["fact_version_count"] == 0
    assert service.propose(source.source_id, "obs1", 0, "propose1") == proposal


def test_061_upgrade_preserves_complete_old_ledger_and_native_rows():
    import sqlite3

    from finance_core.reconciliation.migrations import (
        TEMP_DB_MIGRATION_PATHS,
        apply_migration_paths,
        migration_ledger_rows,
        verify_migration_history,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:60])
    old = migration_ledger_rows(conn)
    conn.execute(
        "INSERT INTO participants(public_id,display_name) VALUES('old-person','Preserved')"
    )
    conn.commit()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[60:])
    assert migration_ledger_rows(conn)[:60] == old
    assert migration_ledger_rows(conn)[-1]["migration_id"] == "061"
    assert (
        conn.execute(
            "SELECT display_name FROM participants WHERE public_id='old-person'"
        ).fetchone()[0]
        == "Preserved"
    )
    assert conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 0
    verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    conn.close()
