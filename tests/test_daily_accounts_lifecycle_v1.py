"""Actual admitted-owner daily account/opening lifecycle boundaries."""

import importlib
import json

import pytest

from finance_core.instance_admission import InstanceAdmissionError
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1, EffectiveReadError
from tests.daily_account_support_v1 import NOW, DailyPort, command, unknown_time
from tests.test_lifecycle_v2 import live as live


def setup(live):
    ledger, _, cfg = live
    ledger.port = DailyPort()
    ledger.clock = lambda: NOW
    api = importlib.import_module("finance_core.lifecycle_v2.daily_v1")
    return (
        ledger,
        ledger.port,
        api.DailyAccountServiceV1(ledger),
        api.DailyAccountReaderV1(ledger),
        cfg,
    )


def create(ledger, port, daily, cmd=None, suffix="1"):
    source = ledger.capture("source" + suffix, "capture" + suffix)
    port.commands["obs" + suffix] = command() if cmd is None else cmd
    proposal = daily.propose_account_create(source.source_id, "obs" + suffix, 0, "propose" + suffix)
    review = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(review.review_id, "delivery" + suffix)
    receipt = daily.confirm(review.review_id, "action" + suffix, "confirm" + suffix)
    return source, proposal, review, receipt


def change(port, daily, view, kind, fields, identity, reason=None):
    text = "Change account; reason: " + (reason or "explicit account update")
    port.add_source(identity, text)
    span = [text.index(reason), len(text)] if reason else None
    port.commands[identity] = command(kind, fields, span)
    proposal = daily.propose_account_change(
        view.account_id,
        identity,
        view.bundle_ref,
        "propose-" + identity,
        reply_evidence_id=identity,
        command_kind=kind,
    )
    review = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(review.review_id, "delivery-" + identity)
    return daily.confirm(review.review_id, "action-" + identity, "confirm-" + identity)


def test_one_complete_card_one_fact_decision_and_event(live):
    ledger, port, daily, reader, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs1"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs1", 0, "propose1")
    assert reader.collect_accounts().accounts == ()
    review = daily.prepare_review(proposal.proposal_id, proposal.version)
    assert review.material["after_values"]["opening"]["amount"] == "12.30"
    with pytest.raises(LifecycleError):
        daily.confirm(review.review_id, "action1", "confirm1")
    daily.record_delivery(review.review_id, "delivery1")
    receipt = daily.confirm(review.review_id, "action1", "confirm1")
    view = reader.get_account(receipt.account_ref.account_id)
    assert view.opening_amount == "12.30" and view.currency == "MYR"
    with lifecycle_read_operation(ledger.owner) as conn:
        assert tuple(
            conn.execute(
                "SELECT (SELECT count(*) FROM l2_facts),(SELECT count(*) FROM l2_decisions),"
                "(SELECT count(*) FROM l2_events WHERE kind='confirm')"
            ).fetchone()
        ) == (1, 1, 1)
    assert daily.confirm(review.review_id, "action1", "confirm1") == receipt
    assert (
        daily.lookup_commit_receipt(
            "confirm1", review_id=review.review_id, action_evidence_id="action1"
        )
        == receipt
    )
    assert ledger.reader().full_audit()["fact_version_count"] == 1


def test_rename_void_and_fresh_opening_keep_separate_identities_and_original_receipt(live):
    ledger, port, daily, reader, _ = setup(live)
    _, _, review, first = create(ledger, port, daily)
    account = first.account_ref.account_id
    view = reader.get_account(account)
    opening = view.opening_ref
    change(port, daily, view, "account_rename", {"name": "Renamed"}, "rename")
    view = reader.get_account(account)
    assert view.opening_ref == opening and view.name == "Renamed"
    change(
        port,
        daily,
        view,
        "opening_correct",
        {"amount": "-300", "as_of": unknown_time()},
        "correct",
        "wrong amount",
    )
    view = reader.get_account(account)
    assert view.opening_amount == "-300.00" and view.opening_ref.opening_version == 2
    change(port, daily, view, "opening_void", {}, "void", "wrong opening")
    view = reader.get_account(account)
    assert view.opening_amount is None and view.opening_status == "UNKNOWN"
    change(
        port, daily, view, "opening_set", {"amount": "0", "as_of": unknown_time()}, "new-opening"
    )
    view = reader.get_account(account)
    assert (
        view.opening_amount == "0.00"
        and view.opening_ref.opening_event_id != opening.opening_event_id
    )
    assert len(reader.opening_history(account, opening.opening_event_id)) == 3
    assert len(reader.account_history(account)) == 5
    assert (
        daily.lookup_commit_receipt(
            "confirm1", review_id=review.review_id, action_evidence_id="action1"
        )
        == first
    )
    assert ledger.reader().full_audit()["fact_version_count"] == 5


def test_old_read_and_confirmation_formats_refuse_daily_and_snapshot_audits_copy(live, tmp_path):
    ledger, port, daily, reader, _ = setup(live)
    _, _, review, receipt = create(ledger, port, daily)
    for call in (
        lambda: ledger.reader().get(receipt.account_ref.account_id),
        lambda: ledger.reader().recent(),
        lambda: ledger.confirm(review.review_id, "action1", "confirm1"),
        lambda: ledger.record_delivery(review.review_id, "delivery1"),
    ):
        with pytest.raises(LifecycleError, match="unsupported_record_format"):
            call()
    with pytest.raises(EffectiveReadError, match="Unsupported"):
        EffectiveLedgerReaderV1(ledger).collect()
    cut = ledger.seal_snapshot(tmp_path / "daily-snapshot.sqlite")
    assert len(cut.facts) == 1 and cut.facts[0][0] == receipt.account_ref.account_id


def test_absent_old_commit_lookup_keeps_original_none_contract(live):
    ledger, _, _, _, _ = setup(live)
    assert (
        ledger.lookup_commit_receipt(
            "absent", review_id="missing-review", action_evidence_id="missing-action"
        )
        is None
    )


def test_draft_source_reply_append_and_snapshot_are_not_final_accounts(live, tmp_path):
    ledger, port, daily, reader, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["draft1"] = command(fields={"name": "Daily", "currency": "MYR"})
    first = daily.propose_account_create(source.source_id, "draft1", 0, "draft-op1")
    assert daily.pending()[0]["values"]["missing_fields"] == ["account_type", "opening"]
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        daily.prepare_review(first.proposal_id, first.version)
    with pytest.raises(LifecycleError, match="reply"):
        daily.propose_account_create(source.source_id, "draft2", 1, "draft-op2")
    port.add_source("reply2", "Use a bank account, opening unknown")
    port.commands["draft2"] = command(
        fields={"name": "Daily", "account_type": "bank", "currency": "MYR"}
    )
    second = daily.propose_account_create(
        source.source_id, "draft2", 1, "draft-op2", reply_evidence_id="reply2"
    )
    assert second.version == 2
    assert daily.pending()[0]["values"]["missing_fields"] == ["opening"]
    assert reader.collect_accounts().accounts == ()
    cut = ledger.seal_snapshot(tmp_path / "draft.sqlite")
    assert cut.facts == ()
    assert daily.propose_account_create(source.source_id, "draft1", 0, "draft-op1") == first
    port.add_source("reply3", "Confirm opening unknown")
    port.commands["draft3"] = command(
        fields={"name": "Daily", "account_type": "bank", "currency": "MYR", "opening": None}
    )
    third = daily.propose_account_create(
        source.source_id, "draft3", 2, "draft-op3", reply_evidence_id="reply3"
    )
    review = daily.prepare_review(third.proposal_id, third.version)
    daily.record_delivery(review.review_id, "delivery3")
    result = daily.confirm(review.review_id, "action3", "commit3")
    assert reader.get_account(result.account_ref.account_id).opening_amount is None
    assert reader.get_account(result.account_ref.account_id).opening_ref is None
    with lifecycle_read_operation(ledger.owner) as conn:
        assert conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM l2_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 1


def test_pending_daily_never_enters_old_prepared_recovery_scope(live):
    from finance_core.lifecycle_v2 import _recovery_material_engine as engine
    from finance_core.lifecycle_v2._recovery_rows_v1 import ClosureLimitsV1, read_rows
    from finance_core.lifecycle_v2.recovery_material_v1 import RecoveryMaterialError

    ledger, port, daily, _, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["draft"] = command(fields={"name": "Daily"})
    daily.propose_account_create(source.source_id, "draft", 0, "draft-op")
    with lifecycle_read_operation(ledger.owner) as conn:
        with pytest.raises(RecoveryMaterialError, match="Daily"):
            engine._scope(conn, read_rows(conn, ClosureLimitsV1()))


def test_stale_card_wrong_actor_expiry_and_exact_expired_retry(live):
    ledger, port, daily, reader, _ = setup(live)
    _, _, review, first = create(ledger, port, daily)
    account = first.account_ref.account_id
    view = reader.get_account(account)
    port.add_source("rename", "Rename account")
    port.commands["rename"] = command("account_rename", {"name": "Other"})
    proposal = daily.propose_account_change(
        account,
        "rename",
        view.bundle_ref,
        "rename-op",
        reply_evidence_id="rename",
        command_kind="account_rename",
    )
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(card.review_id, "rename-delivery")
    port.bad_actor = True
    with pytest.raises(LifecycleError, match="actor"):
        daily.confirm(card.review_id, "bad-action", "bad-op")
    port.bad_actor = False
    ledger.clock = lambda: NOW + 601
    with pytest.raises(LifecycleError, match="expiry"):
        daily.confirm(card.review_id, "late-action", "late-op")
    assert daily.confirm(review.review_id, "action1", "confirm1") == first
    ledger.clock = lambda: NOW
    second = daily.confirm(card.review_id, "rename-action", "rename-confirm")
    assert second.receipt.version == 2
    with pytest.raises(LifecycleError):
        daily.confirm(card.review_id, "different-action", "different-operation")


def test_three_appends_rollback_together_and_same_operation_can_retry(live, monkeypatch):
    from finance_core.lifecycle_v2 import _finalization_plan_v1 as plan_module

    ledger, port, daily, reader, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    review = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(review.review_id, "delivery")

    def partial(conn, plan):
        conn.execute("INSERT INTO l2_events VALUES(?,?,?,?,?,?,?)", plan.event)
        conn.execute("INSERT INTO l2_facts VALUES(?,?,?,?,?,?,?,?,?)", plan.fact)
        raise RuntimeError("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(plan_module, "_append_finalization", partial)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            daily.confirm(review.review_id, "action", "commit")
    assert reader.collect_accounts().accounts == ()
    result = daily.confirm(review.review_id, "action", "commit")
    assert result.receipt.version == 1
    assert ledger.reader().full_audit()["fact_version_count"] == 1


def test_action_callback_cannot_change_runtime_or_use_switched_port(live):
    ledger, port, daily, reader, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    review = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(review.review_id, "delivery")
    replacement = DailyPort()

    def switch(conn, identity, expected):
        ledger.port = replacement

    port.on_action = switch
    result = daily.confirm(review.review_id, "action", "commit")
    # One captured port completed the operation; replacing the parent reference
    # cannot mix another port into source revalidation or actual close.
    assert result.receipt.version == 1 and "action" in port.actions and not replacement.actions
    ledger.port = port
    port.on_action = None
    assert reader.get_account(result.account_ref.account_id).opening_amount == "12.30"


def test_mixed_old_expense_stays_unassigned_and_complete_filter_cannot_hide_daily(
    live, monkeypatch
):
    from finance_core.instance_admission.sessions import _witness_for
    from finance_core.lifecycle_v2.effective_v1 import EffectiveFilterV1, ReadLimitsV1
    from finance_core.reconciliation.native_lifecycle_v1 import NativeReconciliationAdapterV1
    from tests.test_lifecycle_v2 import response

    ledger, port, daily, reader, _ = setup(live)
    raw = "2026-10-24 Cafe SGD 12.00"
    port.add_source("old-source", raw)
    port.native_raw["old-observation"] = response(raw)
    old = ledger.capture("old-source", "old-capture")
    proposal = ledger.propose(old.source_id, "old-observation", 0, "old-propose")
    card = ledger.prepare_review(proposal.proposal_id, proposal.version)
    ledger.record_delivery(card.review_id, "old-delivery")
    old_receipt = ledger.confirm(card.review_id, "old-action", "old-confirm")
    original = EffectiveLedgerReaderV1(ledger).collect()
    _, _, _, daily_receipt = create(ledger, port, daily)
    result = reader.collect_accounts()
    assert result.unassigned_personal_expense_count == 1
    assert result.completeness_gaps == ("unassigned_personal_expense",)
    assert len(result.accounts) == 1 and result.accounts[0].opening_amount == "12.30"
    assert ledger.reader().get(old_receipt.record_id)["values"]["currency"] == "SGD"
    with pytest.raises(EffectiveReadError, match="Unsupported"):
        EffectiveLedgerReaderV1(ledger).collect(EffectiveFilterV1(currency="SGD"))
    compatibility = EffectiveLedgerReaderV1.for_daily_ledger(ledger)
    callbacks = []
    original_source = port.source

    def observe_source(connection, identity):
        witness = _witness_for(connection)
        callbacks.append(
            (id(connection), connection.in_transaction, witness.evidence_callback_token is not None)
        )
        return original_source(connection, identity)

    with monkeypatch.context() as capture:
        capture.setattr(port, "source", observe_source)
        observed = compatibility.collect()
        assert len(callbacks) >= 2 and len(set(callbacks)) == 1
        assert callbacks[0][1:] == (True, True)
        callbacks.clear()
        assert compatibility.source(old.source_id) == port.sources["old-source"]
        assert len(callbacks) >= 2 and len(set(callbacks)) == 1
        assert callbacks[0][1:] == (True, True)

    assert observed.facts == original.facts
    assert original.scope == "native-lifecycle-v2"
    assert observed.scope == "native-lifecycle-v2-original-expenses"
    assert observed.read_identity.event_seq > original.read_identity.event_seq
    assert compatibility.get(old_receipt.record_id) == original.facts[0]
    assert compatibility.get(daily_receipt.receipt.record_id) is None
    summary = compatibility.summary()
    assert summary.scope == observed.scope and summary.items[0].total_amount == "12.00"
    exported = json.loads(compatibility.export_json())
    assert exported["scope"] == observed.scope and exported["facts"][0]["amount"] == "12.00"
    candidates = NativeReconciliationAdapterV1(compatibility).fetch_candidates()
    assert candidates.scope == observed.scope
    assert [item.fact_ref.record_id for item in candidates.candidates] == [old_receipt.record_id]
    with pytest.raises(EffectiveReadError) as limited:
        compatibility.collect(EffectiveFilterV1(currency="SGD"), ReadLimitsV1(max_records=1))
    assert limited.value.code == "complete_read_limit_exceeded"

    reply = "change amount to SGD 15.00\n原因：记录有误"
    port.add_source("amount-reply", reply)
    port.native_raw["amount-observation"] = response(
        reply, "reply", {"amount": "15.00", "currency": "SGD"}
    )
    target = compatibility.get(old_receipt.record_id)
    corrected = ledger.prepare_correction(
        target.record_id,
        target.version,
        "amount-observation",
        "prepare-amount",
        reply_evidence_id="amount-reply",
        reason="记录有误",
    )
    ledger.record_delivery(corrected.review_id, "amount-delivery")
    updated = ledger.confirm(corrected.review_id, "amount-action", "amount-confirm")
    history = compatibility.history(old_receipt.record_id)
    assert history.scope == observed.scope
    assert [(item.version, item.amount) for item in history.versions] == [
        (1, "12.00"),
        (2, "15.00"),
    ]
    assert history.versions[-1].fact_sha256 == updated.fact_sha256
    assert compatibility.summary().items[0].total_amount == "15.00"
    assert reader.collect_accounts().accounts[0].opening_amount == "12.30"

    with pytest.raises(LifecycleError, match="unsupported_record_format"):
        ledger.prepare_correction(
            daily_receipt.receipt.record_id,
            1,
            "wrong-route",
            "wrong-op",
            reply_evidence_id="source1",
            reason="wrong",
        )


def test_concurrent_confirmations_only_append_one_new_version(live):
    from concurrent.futures import ThreadPoolExecutor

    ledger, port, daily, reader, _ = setup(live)
    _, _, _, first = create(ledger, port, daily)
    view = reader.get_account(first.account_ref.account_id)
    port.add_source("rename", "Rename account")
    port.commands["rename"] = command("account_rename", {"name": "Renamed"})
    proposal = daily.propose_account_change(
        view.account_id,
        "rename",
        view.bundle_ref,
        "rename-propose",
        reply_evidence_id="rename",
        command_kind="account_rename",
    )
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(card.review_id, "rename-delivery")

    def submit(number):
        try:
            return daily.confirm(
                card.review_id, "rename-action-" + str(number), "rename-confirm-" + str(number)
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (1, 2)))
    assert sum(hasattr(r, "receipt") for r in results) == 1
    assert reader.get_account(view.account_id).bundle_ref.version == 2
    assert ledger.reader().full_audit()["fact_version_count"] == 2


def test_action_callback_state_mutation_rolls_back_before_final_append(live):
    from finance_core.lifecycle_v2.service import _event

    ledger, port, daily, reader, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(card.review_id, "delivery")
    port.on_action = lambda conn, *args: _event(
        conn, "callback-operation", "unexpected", "fake", {}
    )
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        daily.confirm(card.review_id, "action", "commit")
    port.on_action = None
    assert reader.collect_accounts().accounts == ()
    with lifecycle_read_operation(ledger.owner) as conn:
        assert not conn.execute(
            "SELECT 1 FROM l2_events WHERE operation_id='callback-operation'"
        ).fetchone()


def test_tampered_draft_self_hash_cannot_hide_from_other_account_or_snapshot(live, tmp_path):
    from finance_core.lifecycle_v2.contracts import canonical, digest
    from tests.test_lifecycle_v2 import _adversarial_database

    ledger, port, daily, reader, _ = setup(live)
    _, _, _, first = create(ledger, port, daily)
    port.add_source("source2", "Create another account")
    source = ledger.capture("source2", "capture2")
    port.commands["draft2"] = command(fields={"name": "Other"})
    proposal = daily.propose_account_create(source.source_id, "draft2", 0, "draft2-op")
    with _adversarial_database(ledger) as conn:
        row = conn.execute(
            "SELECT * FROM l2_proposals WHERE proposal_id=?", (proposal.proposal_id,)
        ).fetchone()
        value = json.loads(row["values_json"])
        value["missing_fields"] = ["invented"]
        sealed = json.loads(row["observation_json"])
        material = [
            row["source_id"],
            row["version"],
            sealed["observation"],
            value,
            row["expected_version"],
            row["record_id"],
        ]
        event = conn.execute("SELECT * FROM l2_events WHERE operation_id='draft2-op'").fetchone()

        def mutate():
            conn.execute(
                "UPDATE l2_proposals SET values_json=?,proposal_sha256=? WHERE proposal_id=?",
                (canonical(value), digest(material), proposal.proposal_id),
            )
            conn.execute(
                "UPDATE l2_events SET payload_json=?,event_sha256=? WHERE seq=?",
                (
                    canonical(material),
                    digest(
                        [
                            event["seq"],
                            event["operation_id"],
                            event["kind"],
                            event["entity_id"],
                            material,
                            event["previous_sha256"],
                        ]
                    ),
                    event["seq"],
                ),
            )

        mutate()
    with pytest.raises(LifecycleError, match="compiled values"):
        reader.get_account(first.account_ref.account_id)
    with pytest.raises(LifecycleError, match="compiled values"):
        ledger.seal_snapshot(tmp_path / "damaged.sqlite")


def test_unknown_daily_fact_format_blocks_whole_registered_domain(live):
    from finance_core.lifecycle_v2.contracts import canonical
    from tests.test_lifecycle_v2 import _adversarial_database

    ledger, port, daily, reader, _ = setup(live)
    _, _, _, first = create(ledger, port, daily)
    with _adversarial_database(ledger) as conn:
        row = conn.execute("SELECT * FROM l2_facts LIMIT 1").fetchone()
        values = json.loads(row["values_json"])
        values["format"] = "future-unsupported-financial-type"
        conn.execute("UPDATE l2_facts SET values_json=?", (canonical(values),))
    with pytest.raises(LifecycleError, match="unsupported_record_format"):
        reader.collect_accounts()
    port.add_source("source2", "Create account")
    source = ledger.capture("source2", "capture2")
    port.commands["obs2"] = command()
    with pytest.raises(LifecycleError, match="unsupported_record_format"):
        daily.propose_account_create(source.source_id, "obs2", 0, "propose2")


def test_proposal_cannot_commit_a_self_unreadable_large_envelope(live):
    ledger, port, daily, _, _ = setup(live)
    port.add_source("source1", "\x00" * 40000)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    with pytest.raises(LifecycleError, match="Bounded"):
        daily.propose_account_create(source.source_id, "obs", 0, "propose")
    with lifecycle_read_operation(ledger.owner) as conn:
        assert conn.execute("SELECT count(*) FROM l2_sources").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] == 0


def test_supplement_cannot_reuse_an_old_reply_as_new_input(live):
    ledger, port, daily, _, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["draft"] = command(fields={"name": "Daily"})
    daily.propose_account_create(source.source_id, "draft", 0, "draft-op")
    port.add_source("reply", "Use MYR")
    port.commands["second"] = command(fields={"name": "Daily", "currency": "MYR"})
    daily.propose_account_create(
        source.source_id, "second", 1, "second-op", reply_evidence_id="reply"
    )
    port.commands["third"] = command()
    with pytest.raises(LifecycleError, match="reply.*used"):
        daily.propose_account_create(
            source.source_id, "third", 2, "third-op", reply_evidence_id="reply"
        )


def test_historical_evidence_callback_cannot_mutate_during_prewrite_audit(live):
    from finance_core.lifecycle_v2.service import _event

    ledger, port, daily, reader, _ = setup(live)
    _, _, _, first = create(ledger, port, daily)
    view = reader.get_account(first.account_ref.account_id)
    port.add_source("rename", "Rename account")
    port.commands["rename"] = command("account_rename", {"name": "Other"})
    inserted = []

    def mutate_once(conn, identity, expected):
        if not inserted:
            _event(conn, "bad-audit-callback", "unexpected", "foreign", {})
            inserted.append(True)

    port.on_action = mutate_once
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        daily.propose_account_change(
            view.account_id,
            "rename",
            view.bundle_ref,
            "rename-op",
            reply_evidence_id="rename",
            command_kind="account_rename",
        )
    port.on_action = None
    assert reader.get_account(view.account_id).bundle_ref.version == 1
    with lifecycle_read_operation(ledger.owner) as conn:
        assert not conn.execute(
            "SELECT 1 FROM l2_events WHERE operation_id='bad-audit-callback'"
        ).fetchone()
        assert conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] == 1


def test_audit_callback_cannot_append_unowned_rows_without_an_event(live):
    ledger, port, daily, reader, _ = setup(live)
    _, _, _, first = create(ledger, port, daily)
    view = reader.get_account(first.account_ref.account_id)
    port.add_source("rename", "Rename account")
    port.commands["rename"] = command("account_rename", {"name": "Other"})
    inserted = []

    def orphan_once(conn, identity, expected):
        if not inserted:
            conn.execute(
                "INSERT INTO l2_sources SELECT 'orphan-source','orphan-key','orphan-evidence',"
                "principal_id,client_id,raw_input,original_sha256,"
                "source_sha256,generation,created_at FROM l2_sources LIMIT 1"
            )
            inserted.append(True)

    port.on_action = orphan_once
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        daily.propose_account_change(
            view.account_id,
            "rename",
            view.bundle_ref,
            "rename-op",
            reply_evidence_id="rename",
            command_kind="account_rename",
        )
    port.on_action = None
    assert reader.get_account(view.account_id).bundle_ref.version == 1
    with lifecycle_read_operation(ledger.owner) as conn:
        assert conn.execute("SELECT count(*) FROM l2_sources").fetchone()[0] == 1


@pytest.mark.parametrize("boundary", ["commit", "rollback"])
def test_evidence_callback_cannot_end_and_reopen_the_outer_transaction(live, boundary):
    ledger, port, daily, _, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(card.review_id, "delivery")
    observed = {}

    def reenter(conn, identity, expected):
        before = (
            conn.in_transaction,
            conn.execute("SELECT count(*),max(seq) FROM l2_events").fetchone(),
        )
        try:
            getattr(conn, boundary)()
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            observed["guard_error"] = (type(exc).__name__, str(exc))
            raise
        after = (
            conn.in_transaction,
            conn.execute("SELECT count(*),max(seq) FROM l2_events").fetchone(),
        )
        observed.update(
            {
                "guard_allowed": True,
                "same_transaction_flag": before[0] == after[0],
                "same_event_inventory": tuple(before[1]) == tuple(after[1]),
            }
        )

    port.on_action = reenter
    try:
        with pytest.raises(InstanceAdmissionError, match="read-only scope"):
            daily.confirm(card.review_id, "action", "commit")
        assert "guard_error" in observed and not observed.get("guard_allowed")
        with lifecycle_read_operation(ledger.owner) as conn:
            assert conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM l2_decisions").fetchone()[0] == 0
    finally:
        print("TRANSACTION_REENTRY_REPRO", boundary, observed)


def test_unknown_opening_is_set_later_and_duplicate_names_never_merge(live):
    ledger, port, daily, reader, _ = setup(live)
    unknown = command(
        fields={"name": "Daily", "account_type": "cash", "currency": "MYR", "opening": None}
    )
    _, _, _, first = create(ledger, port, daily, unknown)
    initial = reader.get_account(first.account_ref.account_id)
    assert initial.opening_ref is None and initial.opening_amount is None
    assert initial.bundle_event_id == "message-source1"
    port.add_source("source2", "Create another account named Daily")
    _, _, _, second = create(ledger, port, daily, unknown, "2")
    assert first.account_ref.account_id != second.account_ref.account_id
    assert len(reader.collect_accounts().accounts) == 2
    change(
        port,
        daily,
        initial,
        "opening_set",
        {"amount": "0", "as_of": unknown_time()},
        "first-opening",
    )
    after = reader.get_account(first.account_ref.account_id)
    assert after.account_ref == initial.account_ref and after.opening_amount == "0.00"
    assert reader.get_account(second.account_ref.account_id).opening_ref is None
    from finance_core.lifecycle_v2.daily_v1 import DailyReadLimitsV1

    with pytest.raises(LifecycleError, match="complete_read_limit_exceeded"):
        reader.get_account(first.account_ref.account_id, limits=DailyReadLimitsV1(max_records=1))


def inventory(ledger):
    with lifecycle_read_operation(ledger.owner) as conn:
        return tuple(
            conn.execute(
                "SELECT (SELECT count(*) FROM l2_events),(SELECT count(*) FROM l2_reviews),"
                "(SELECT count(*) FROM l2_deliveries),(SELECT count(*) FROM l2_facts),"
                "(SELECT count(*) FROM l2_decisions)"
            ).fetchone()
        )


@pytest.mark.parametrize("kind", ["account_create", "account_rename"])
def test_current_review_binding_mismatch_refuses_without_append(live, kind):
    from dataclasses import replace

    ledger, port, daily, reader, _ = setup(live)
    if kind == "account_create":
        source = ledger.capture("source1", "capture1")
        port.commands["obs"] = command()
        proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    else:
        _, _, _, first = create(ledger, port, daily)
        view = reader.get_account(first.account_ref.account_id)
        port.add_source("rename", "Rename account")
        port.commands["rename"] = command("account_rename", {"name": "Other"})
        proposal = daily.propose_account_change(
            view.account_id,
            "rename",
            view.bundle_ref,
            "rename-propose",
            reply_evidence_id="rename",
            command_kind="account_rename",
        )
    original = ledger.binding
    before = inventory(ledger)
    ledger.binding = replace(original, generation="gen2")
    with pytest.raises(LifecycleError, match="binding"):
        daily.prepare_review(proposal.proposal_id, proposal.version)
    assert inventory(ledger) == before
    assert daily.pending()
    ledger.binding = original
    review = daily.prepare_review(proposal.proposal_id, proposal.version)
    assert review.material["binding"]["generation"] == original.generation
    ledger.reader().full_audit()


def test_current_delivery_binding_mismatch_refuses_without_append(live):
    from dataclasses import replace

    ledger, port, daily, _, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    before = inventory(ledger)
    original = ledger.binding
    ledger.binding = replace(original, generation="gen2")
    with pytest.raises(LifecycleError, match="binding"):
        daily.record_delivery(card.review_id, "delivery")
    assert inventory(ledger) == before
    assert daily.prepare_review(proposal.proposal_id, proposal.version) == card
    ledger.binding = original
    daily.record_delivery(card.review_id, "delivery")
    ledger.reader().full_audit()


def test_receipt_identifiers_are_checked_before_any_outcome_lookup(live):
    ledger, port, daily, _, _ = setup(live)
    source = ledger.capture("source1", "capture1")
    port.commands["obs"] = command()
    proposal = daily.propose_account_create(source.source_id, "obs", 0, "propose")
    card = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(card.review_id, "delivery")
    receipt = daily.confirm(card.review_id, "action", "1")
    before = inventory(ledger)
    for bad in (1, True, None, b"1", [], "", " 1", "1 ", "x" * 257):
        for operation in (
            lambda: daily.lookup_commit_receipt(
                bad, review_id=card.review_id, action_evidence_id="action"
            ),
            lambda: daily.confirm(card.review_id, "action", bad),
            lambda: daily.lookup_commit_receipt(
                "absent", review_id=bad, action_evidence_id="action"
            ),
            lambda: daily.lookup_commit_receipt(
                "absent", review_id=card.review_id, action_evidence_id=bad
            ),
            lambda: daily.confirm(bad, "action", "absent"),
            lambda: daily.confirm(card.review_id, bad, "absent"),
        ):
            with pytest.raises(LifecycleError, match="id"):
                operation()
    assert (
        daily.lookup_commit_receipt("1", review_id=card.review_id, action_evidence_id="action")
        == receipt
    )
    assert daily.confirm(card.review_id, "action", "1") == receipt
    assert (
        daily.lookup_commit_receipt("absent", review_id=card.review_id, action_evidence_id="action")
        is None
    )
    assert inventory(ledger) == before


def test_original_prefix_remains_valid_after_later_proposals_and_generation(live, tmp_path):
    from dataclasses import replace

    ledger, port, daily, reader, _ = setup(live)
    _, _, original_card, first = create(ledger, port, daily)
    account = first.account_ref.account_id
    original_binding = ledger.binding
    ledger.binding = replace(original_binding, generation="gen2")
    second = change(
        port, daily, reader.get_account(account), "account_rename", {"name": "Current"}, "rename"
    )
    assert second.receipt.version == 2
    # A later ordinary pending proposal/card must not redefine earlier prefixes.
    view = reader.get_account(account)
    raw = "Correct opening; reason: corrected statement"
    port.add_source("pending", raw)
    reason = [raw.index("corrected statement"), len(raw)]
    port.commands["pending"] = command(
        "opening_correct", {"amount": "18.00", "as_of": unknown_time()}, reason
    )
    proposal = daily.propose_account_change(
        account,
        "pending",
        view.bundle_ref,
        "pending-propose",
        reply_evidence_id="pending",
        command_kind="opening_correct",
    )
    pending_card = daily.prepare_review(proposal.proposal_id, proposal.version)
    daily.record_delivery(pending_card.review_id, "pending-delivery")
    assert reader.get_account(account).bundle_ref.version == 2
    assert len(daily.pending()) == 1
    assert ledger.reader().full_audit()["fact_version_count"] == 2
    # Exact outcome recovery keeps its original binding contract. Historical
    # account reads above remain available in the newer admitted generation.
    ledger.binding = original_binding
    assert (
        daily.lookup_commit_receipt(
            "confirm1", review_id=original_card.review_id, action_evidence_id="action1"
        )
        == first
    )
    assert daily.confirm(original_card.review_id, "action1", "confirm1") == first
    assert (
        daily.record_delivery(original_card.review_id, "delivery1").material
        == original_card.material
    )
    assert ledger.reader().full_audit()["fact_version_count"] == 2
    # Inspect supported state read-only, using the audit's eligibility check.
    from finance_core.lifecycle_v2.daily_v1._prefix_v1 import validate_eligibility
    from finance_core.lifecycle_v2.daily_v1.accounts import _event_seq, _state_at
    from finance_core.lifecycle_v2.daily_v1.contracts import decode_expected

    with lifecycle_read_operation(ledger.owner) as conn:
        conn.execute("BEGIN")
        old_proposal = conn.execute(
            "SELECT * FROM l2_proposals WHERE proposal_id=?", (original_card.proposal_id,)
        ).fetchone()
        old_expected = decode_expected(json.loads(old_proposal["observation_json"])["expected"])
        original_context = _state_at(conn, old_expected, first.receipt.commit_seq)
        original_proposal = (old_proposal["proposal_id"], old_proposal["version"])
        assert original_context == (None, original_proposal)
        validate_eligibility(
            original_context[0],
            old_expected.target_bundle_ref,
            original_context[1],
            original_proposal,
        )
        latest_row = conn.execute(
            "SELECT * FROM l2_proposals WHERE proposal_id=?", (proposal.proposal_id,)
        ).fetchone()
        latest_expected = decode_expected(json.loads(latest_row["observation_json"])["expected"])
        delivery_seq = _event_seq(conn, "delivery", pending_card.review_id)
        latest_context = _state_at(conn, latest_expected, delivery_seq)
        latest_proposal = (proposal.proposal_id, proposal.version)
        validate_eligibility(
            latest_context[0], latest_expected.target_bundle_ref, latest_context[1], latest_proposal
        )
        with pytest.raises(LifecycleError, match="stale_proposal"):
            validate_eligibility(
                latest_context[0],
                latest_expected.target_bundle_ref,
                latest_context[1],
                original_proposal,
            )
        assert _state_at(conn, latest_expected) == latest_context
        conn.rollback()
    cut = ledger.seal_snapshot(tmp_path / "later-proposal.sqlite3")
    assert cut.facts[0][1] == 2
