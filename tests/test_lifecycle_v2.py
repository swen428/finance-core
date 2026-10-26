"""Synthetic proof tests through actual current-instance guarded sessions."""

import hashlib
import json
from contextlib import contextmanager
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
    if owner._lease.fd >= 0:
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


def test_reader_source_returns_authenticated_native_input_and_rejects_missing(live):
    service, port, _ = live
    source = service.capture("source1", "capture1")
    reader = service.reader()
    before = reader.full_audit()
    assert reader.source(source.source_id) == port.sources["source1"]
    with pytest.raises(LifecycleError, match="Missing l2_sources identity"):
        reader.source("missing-source")
    assert reader.full_audit() == before
    assert port.observations == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_input", TEXT + "tampered"),
        ("original_sha256", "0" * 64),
        ("principal_id", "other-principal"),
        ("client_id", "other-client"),
        ("source_timestamp", 1792800001),
        ("timezone", "UTC"),
    ],
)
def test_reader_source_rejects_native_evidence_drift(live, field, value):
    service, port, _ = live
    source = service.capture("source1", "capture1")
    port.sources["source1"] = replace(port.sources["source1"], **{field: value})
    with pytest.raises(
        LifecycleError, match="actor/client mismatch|Source/original evidence changed"
    ):
        service.reader().source(source.source_id)


def test_reader_source_can_read_archived_generation_without_new_proposal_authority(live):
    service, port, cfg = live
    source = service.capture("source1", "capture1")
    before = service.reader().full_audit()
    successor = LedgerService(
        service.owner,
        port,
        RuntimeBinding(cfg.instance_id, "gen2", "wen", "chat"),
        clock=service.clock,
    )
    assert successor.reader().source(source.source_id) == port.sources["source1"]
    with pytest.raises(LifecycleError, match="Source belongs to archived generation"):
        successor.propose(source.source_id, "archived-observation", 0, "archived-proposal")
    assert successor.reader().full_audit() == before
    assert port.observations == {}


def test_restore_is_held_until_actual_old_fence(live, tmp_path, monkeypatch):
    from finance_core.lifecycle_v2.recovery import (
        prepare_restored_profile,
        promote_restored_profile,
    )

    service, port, old_config = live
    cut = service.seal_snapshot(tmp_path / "restore.sqlite")
    other = tmp_path / "second"
    other.mkdir(mode=0o700)
    new_config = synthetic_config(other, monkeypatch, instance_id="new-instance")
    new_binding = RuntimeBinding(new_config.instance_id, "gen2", "wen", "chat")
    restored = prepare_restored_profile(
        new_config,
        tmp_path / "restore.sqlite",
        cut,
        new_binding=new_binding,
        operation_id="restore-prepare",
        approval_ref="approved",
    )
    assert restored.status == "RESTORE_HELD"
    assert inspect_instance_admission(new_config)["state"] == "RESTORE_HELD"
    with pytest.raises(Exception):
        admit_production_instance(new_config, operation_id="bypass", approval_ref="approved")
    service.owner.close()
    with pytest.raises(LifecycleError, match="fenced"):
        promote_restored_profile(
            restored,
            old_config,
            designation_operation_id="designate",
            designation_approval_ref="approved",
            operation_id="new-admit",
            approval_ref="approved",
            port=port,
            binding=new_binding,
        )
    from finance_core.profile_layout import profile_root

    monkeypatch.setenv(
        "FINANCE_RUNTIME_ROOT",
        str(profile_root(old_config.locator_root, "synthetic", old_config.layout) / "runtime"),
    )
    service.owner = acquire_production_owner(old_config)
    fence_production_instance(old_config, operation_id="actual-old-fence", approval_ref="approved")
    monkeypatch.setenv(
        "FINANCE_RUNTIME_ROOT",
        str(profile_root(new_config.locator_root, "synthetic", new_config.layout) / "runtime"),
    )
    from finance_core.instance_admission import InstanceAdmissionBusy

    with pytest.raises(InstanceAdmissionBusy):
        promote_restored_profile(
            restored,
            old_config,
            designation_operation_id="designate",
            designation_approval_ref="approved",
            operation_id="owner-still-held",
            approval_ref="approved",
            port=port,
            binding=new_binding,
        )
    assert inspect_instance_admission(new_config)["state"] == "RESTORE_HELD"
    assert __import__("os").environ["FINANCE_RUNTIME_ROOT"] == str(
        profile_root(new_config.locator_root, "synthetic", new_config.layout) / "runtime"
    )
    service.owner.close()
    result = promote_restored_profile(
        restored,
        old_config,
        designation_operation_id="designate",
        designation_approval_ref="approved",
        operation_id="new-admit",
        approval_ref="approved",
        port=port,
        binding=new_binding,
    )
    assert result["state"] == "ACTIVE"
    new_config.close()


def test_operation_reuses_only_immutable_artifacts_and_keeps_dynamic_authority(live, monkeypatch):
    from finance_core.instance_admission import InstanceAdmissionError, authority, sessions
    from finance_core.instance_admission.sessions import lifecycle_write_operation

    service, _, _ = live
    original = authority._migration_contract_digest
    calls = []

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(authority, "_migration_contract_digest", counted)
    with lifecycle_write_operation(service.owner) as conn:
        # Fresh composition/lease checks precede the operation-local witness.
        # Their exact count is an implementation detail; guarded SQL reuses it.
        entry_calls = len(calls)
        assert entry_calls > 0
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SELECT count(*) FROM l2_events").fetchone()
        conn.execute("SELECT count(*) FROM l2_sources").fetchone()
        assert len(calls) == entry_calls
        original_state = sessions._state

        def fenced(config, **kwargs):
            state = original_state(config, **kwargs)
            state["instances"][config.binding_sha256]["state"] = "FENCED"
            return state

        with monkeypatch.context() as changed_authority:
            changed_authority.setattr(sessions, "_state", fenced)
            with pytest.raises(InstanceAdmissionError, match="ACTIVE"):
                conn.execute("SELECT 1").fetchone()
        assert len(calls) == entry_calls

    previous_calls = len(calls)
    with lifecycle_write_operation(service.owner) as conn:
        next_entry_calls = len(calls)
        assert next_entry_calls > previous_calls
        conn.execute("BEGIN IMMEDIATE")
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert len(calls) == next_entry_calls

    def drifted():
        calls.append(1)
        return "0" * 64

    previous_calls = len(calls)
    with monkeypatch.context() as changed_installation:
        changed_installation.setattr(authority, "_migration_contract_digest", drifted)
        with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
            with lifecycle_write_operation(service.owner):
                pytest.fail("A new operation reused the exited installation proof")
    assert len(calls) > previous_calls


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
    newroot = tmp_path / "new"
    newroot.mkdir(mode=0o700)
    new = synthetic_config(newroot, monkeypatch, instance_id="new-restore")
    new_binding = RuntimeBinding(new.instance_id, "g2", "wen", "chat")
    held = prepare_restored_profile(
        new,
        tmp_path / "sealed.sqlite",
        cut,
        new_binding=new_binding,
        operation_id="held",
        approval_ref="approved",
    )
    isolation = auth.issue_old_writer_isolation(
        cfg, held, operation_id="designate", approval_ref="approved"
    )
    cfg.close()
    oldroot.rename(tmp_path / "unavailable-old")
    with pytest.raises(LifecycleError, match="signature"):
        promote_with_isolation_receipt(
            held,
            OldWriterIsolationReceipt(isolation.material_json, "0" * 64),
            auth,
            operation_id="bad",
            approval_ref="approved",
            port=service.port,
            binding=new_binding,
        )
    from finance_core.lifecycle_v2.recovery import verify_held_profile_evidence

    verify_held_profile_evidence(held, service.port, new_binding)
    service.port.bad_actor = True
    with pytest.raises(LifecycleError):
        promote_with_isolation_receipt(
            held,
            isolation,
            auth,
            operation_id="early",
            approval_ref="approved",
            port=service.port,
            binding=new_binding,
        )
    assert inspect_instance_admission(new)["state"] == "RESTORE_HELD"
    service.port.bad_actor = False
    promote_with_isolation_receipt(
        held,
        isolation,
        auth,
        operation_id="promote",
        approval_ref="approved",
        port=service.port,
        binding=new_binding,
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


def _correct(service, port, posted):
    text = "change amount to SGD 15.00\n原因：记录有误"
    port.sources["edit-new"] = VerifiedSource(
        "edit-new",
        "wen",
        "chat",
        "edit-new",
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        1792800000,
    )
    port.raw = response(text, "reply", {"amount": "15.00", "currency": "SGD"})
    review = service.prepare_correction(
        posted.record_id,
        posted.version,
        "obs-correct",
        "prepare-correct",
        reply_evidence_id="edit-new",
        reason="记录有误",
    )
    service.record_delivery(review.review_id, "delivery-correct")
    return review, service.confirm(review.review_id, "action-correct", "commit-correct")


def test_committed_replay_survives_expiry_correction_and_owner_restart(live):
    service, port, cfg = live
    _, _, review = ready(service)
    original = service.confirm(review.review_id, "action1", "confirm1")
    _, corrected = _correct(service, port, original)
    assert corrected.version == 2
    service.owner.close()
    replacement = acquire_production_owner(cfg)
    reopened = LedgerService(replacement, port, service.binding, clock=lambda: 1792800601)
    try:
        assert reopened.confirm(review.review_id, "action1", "confirm1") == original
        assert reopened.reader().get(original.record_id)["version"] == 2
        with pytest.raises(LifecycleError, match="replay mismatch"):
            reopened.confirm(review.review_id, "another-action", "confirm1")
        port.bad_actor = True
        with pytest.raises(LifecycleError, match="authenticated human"):
            reopened.confirm(review.review_id, "action1", "confirm1")
        port.bad_actor = False
        with pytest.raises(LifecycleError):
            reopened.confirm(review.review_id, "action1", "fresh-operation")
    finally:
        replacement.close()


@contextmanager
def _adversarial_database(service):
    """Synthetic disk corruption, restoring exact schema before guarded re-open."""
    import sqlite3

    from finance_core.profile_layout import profile_root

    cfg = service.owner.config
    db = profile_root(cfg.locator_root, cfg.profile_id, cfg.layout) / "runtime/database/finance.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    triggers = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'l2_*'"
    ).fetchall()
    try:
        for trigger in triggers:
            conn.execute("DROP TRIGGER " + trigger["name"])
        yield conn
        for trigger in triggers:
            conn.execute(trigger["sql"])
        conn.commit()
    finally:
        conn.close()


def _rehash_audit(conn):

    previous = None
    for event in conn.execute("SELECT * FROM l2_events ORDER BY seq").fetchall():
        sha = digest(
            [
                event["seq"],
                event["operation_id"],
                event["kind"],
                event["entity_id"],
                json.loads(event["payload_json"]),
                previous,
            ]
        )
        conn.execute(
            "UPDATE l2_events SET previous_sha256=?,event_sha256=? WHERE seq=?",
            (previous, sha, event["seq"]),
        )
        previous = sha


@pytest.mark.parametrize(
    "damage",
    [
        "all-facts",
        "latest-fact",
        "fact-and-decision",
        "decision",
        "event",
        "review",
        "delivery",
        "proposal",
        "source",
    ],
)
def test_full_audit_rejects_missing_or_forged_owned_inventory(live, tmp_path, damage):

    service, port, _ = live
    _, _, review = ready(service)
    original = service.confirm(review.review_id, "action1", "confirm1")
    _, latest = _correct(service, port, original)
    with _adversarial_database(service) as conn:
        if damage == "all-facts":
            conn.execute("DELETE FROM l2_facts")
        elif damage == "latest-fact":
            conn.execute("DELETE FROM l2_facts WHERE version=2")
        elif damage == "fact-and-decision":
            conn.execute("DELETE FROM l2_decisions WHERE version=2")
            conn.execute("DELETE FROM l2_facts WHERE version=2")
        elif damage == "decision":
            conn.execute("UPDATE l2_decisions SET operation_id='forged-operation' WHERE version=2")
        elif damage == "event":
            conn.execute(
                "UPDATE l2_events SET entity_id='forged-record' WHERE seq=?", (latest.commit_seq,)
            )
            _rehash_audit(conn)
        else:
            table = {
                "review": "l2_reviews",
                "delivery": "l2_deliveries",
                "proposal": "l2_proposals",
                "source": "l2_sources",
            }[damage]
            conn.execute("DELETE FROM " + table)
    with pytest.raises(LifecycleError, match="inventory"):
        service.reader().full_audit()
    with pytest.raises(LifecycleError, match="inventory"):
        service.seal_snapshot(tmp_path / "bad-snapshot.sqlite")


@pytest.mark.parametrize("later", ["post", "correction"])
def test_generic_promotion_rejects_actual_fenced_stale_cut(live, tmp_path, monkeypatch, later):
    from finance_core.lifecycle_v2.recovery import (
        prepare_restored_profile,
        promote_restored_profile,
        verify_held_profile_evidence,
    )
    from finance_core.profile_layout import profile_root

    service, port, old = live
    posted = None
    if later == "correction":
        _, _, review = ready(service)
        posted = service.confirm(review.review_id, "action1", "confirm1")
    cut = service.seal_snapshot(tmp_path / "stale.sqlite")
    if later == "post":
        _, _, review = ready(service)
        service.confirm(review.review_id, "action1", "confirm1")
    else:
        _correct(service, port, posted)
    fence_production_instance(old, operation_id="fence-stale", approval_ref="approved")
    service.owner.close()
    other = tmp_path / "restore"
    other.mkdir(mode=0o700)
    new = synthetic_config(other, monkeypatch, instance_id="new-stale-instance")
    new_binding = RuntimeBinding(new.instance_id, "new-gen", "wen", "chat")
    held = prepare_restored_profile(
        new,
        tmp_path / "stale.sqlite",
        cut,
        new_binding=new_binding,
        operation_id="prepare-held",
        approval_ref="approved",
    )
    verify_held_profile_evidence(held, port, new_binding)
    with pytest.raises(LifecycleError, match="cut"):
        promote_restored_profile(
            held,
            old,
            designation_operation_id="designate-stale",
            designation_approval_ref="approved",
            operation_id="promote-stale",
            approval_ref="approved",
            port=port,
            binding=new_binding,
        )
    assert inspect_instance_admission(new)["state"] == "RESTORE_HELD"
    assert __import__("os").environ["FINANCE_RUNTIME_ROOT"] == str(
        profile_root(new.locator_root, new.profile_id, new.layout) / "runtime"
    )
    new.close()


def test_record_inventory_uses_indexed_selected_record_queries(live):
    from finance_core.instance_admission.sessions import lifecycle_read_operation

    service, _, _ = live
    _, _, review = ready(service)
    posted = service.confirm(review.review_id, "action1", "confirm1")
    with lifecycle_read_operation(service.owner) as conn:
        plans = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM l2_events WHERE entity_id=? "
            "AND kind IN ('confirm','import')",
            (posted.record_id,),
        ).fetchall()
        detail = " ".join(row[3] for row in plans)
        assert "SEARCH l2_events USING INDEX l2_events_entity" in detail
        assert "SCAN l2_events" not in detail
