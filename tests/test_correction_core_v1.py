"""Synthetic text correction transaction, replay and history checks."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from finance_core.application.corrections import (
    ConsumptionSeal,
    CorrectionConflict,
    CorrectionFields,
    CorrectionIntegrityError,
    CorrectionService,
    ExpectedDecision,
    ExpectedHistory,
    SignedDecision,
    TrustedApprovalBinding,
    VerifiedApprovalHistory,
    VerifiedDecision,
    VerifiedOriginalSource,
)
from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeSnapshotRepository,
    build_authoritative_snapshot,
    canonical_json_text,
    canonical_json_value,
)
from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths


class _Source:
    def verify_original(
        self, connection: sqlite3.Connection, target_id: str
    ) -> VerifiedOriginalSource:
        row = connection.execute(
            "SELECT public_id, transaction_date, amount, currency, merchant, raw_input "
            "FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert row is not None
        assert row[5] == "original receipt-free raw input"
        fields = CorrectionFields("12.34", "SGD", "2026-09-23", None)
        assert (row[0], row[1], str(row[2]), row[3], row[4]) == (
            target_id,
            fields.transaction_date,
            fields.amount,
            fields.currency,
            fields.merchant,
        )
        source_json = canonical_json_text(
            {
                "target_id": target_id,
                "raw_sha256": hashlib.sha256(row[5].encode()).hexdigest(),
            }
        )
        return VerifiedOriginalSource(
            target_id=target_id,
            route="text",
            actor="synthetic-actor",
            fields=fields,
            source_hash=hashlib.sha256(source_json.encode()).hexdigest(),
            original_hash="a" * 64,
            original_projection_hash=hashlib.sha256(
                canonical_json_text(fields.as_dict()).encode()
            ).hexdigest(),
            source_json=source_json,
            evidence_refs=("synthetic:raw-input",),
        )


class _ReceiptSource:
    def __init__(self, original_snapshot_hash: str) -> None:
        self.original_snapshot_hash = original_snapshot_hash

    def verify_original(
        self, connection: sqlite3.Connection, target_id: str
    ) -> VerifiedOriginalSource:
        row = connection.execute(
            "SELECT transaction_date, amount, currency, merchant, raw_input "
            "FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("2026-09-23", 12.34, "SGD", "Cafe", "original receipt raw input")
        fields = CorrectionFields("12.34", "SGD", "2026-09-23", "Cafe")
        source_json = canonical_json_text(
            {
                "target_id": target_id,
                "raw_sha256": hashlib.sha256(row[4].encode()).hexdigest(),
                "original_snapshot_hash": self.original_snapshot_hash,
            }
        )
        return VerifiedOriginalSource(
            target_id=target_id,
            route="receipt",
            actor="synthetic-actor",
            fields=fields,
            source_hash=hashlib.sha256(source_json.encode()).hexdigest(),
            original_hash="a" * 64,
            original_projection_hash=hashlib.sha256(
                canonical_json_text(fields.as_dict()).encode()
            ).hexdigest(),
            source_json=source_json,
            evidence_refs=("synthetic:receipt",),
            receipt_id="receipt-1",
            fact_set_id="facts-1",
            fact_set_version=1,
            fact_input_hash="b" * 64,
            fact_result_hash="c" * 64,
            snapshot_id="original-snapshot",
            snapshot_hash=self.original_snapshot_hash,
            payer_id="payer-1",
            aggregate_id="group-1",
        )


class _Authority:
    def __init__(self) -> None:
        self.epoch = 1_795_000_000
        self.instance_id = "instance-1"

    def current_binding(
        self, connection: sqlite3.Connection, expected_actor: str
    ) -> TrustedApprovalBinding:
        assert connection.in_transaction
        return TrustedApprovalBinding(expected_actor, "a" * 64, "synthetic-realm", self.instance_id)

    def verify_fresh(
        self, signed_decision: SignedDecision, expected_decision: ExpectedDecision
    ) -> VerifiedDecision:
        envelope = canonical_json_value(signed_decision.envelope_json)
        assert isinstance(envelope, dict)
        assert envelope["plan_hash"] == expected_decision.plan.plan_hash
        digest = hashlib.sha256(
            (signed_decision.envelope_json + signed_decision.signature).encode()
        ).hexdigest()
        return VerifiedDecision(envelope, digest, self.epoch, expected_decision.plan.correction_id)

    def seal_consumption(
        self, verified_decision: VerifiedDecision, result_core_hash: str
    ) -> ConsumptionSeal:
        return ConsumptionSeal(
            canonical_json_text(
                {
                    "decision_digest": verified_decision.decision_digest,
                    "result_core_hash": result_core_hash,
                    "checked_at_epoch": verified_decision.checked_at_epoch,
                }
            ),
            "b" * 64,
        )

    def verify_history(
        self,
        decision: SignedDecision,
        consumption: ConsumptionSeal,
        expected_history: ExpectedHistory,
    ) -> VerifiedApprovalHistory:
        material = canonical_json_value(consumption.material_json)
        assert isinstance(material, dict)
        assert material["result_core_hash"] == expected_history.result_core_hash
        assert material["checked_at_epoch"] == expected_history.checked_at_epoch
        digest = hashlib.sha256((decision.envelope_json + decision.signature).encode()).hexdigest()
        assert material["decision_digest"] == digest
        return VerifiedApprovalHistory(digest, expected_history.checked_at_epoch)


def _fixture() -> tuple[sqlite3.Connection, CorrectionService, _Authority]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.execute(
        """INSERT INTO transactions (
            public_id,intent,intent_type,transaction_date,status,amount,currency,merchant,raw_input
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "txn-1",
            "Expense",
            "Manual",
            "2026-09-23",
            "active",
            "12.34",
            "SGD",
            None,
            "original receipt-free raw input",
        ),
    )
    conn.commit()
    authority = _Authority()
    return conn, CorrectionService(conn, _Source(), authority), authority


def _receipt_fixture() -> tuple[sqlite3.Connection, CorrectionService, _Authority]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.execute(
        "INSERT INTO participants (public_id,display_name,is_self,is_active) VALUES (?,?,1,1)",
        ("payer-1", "Payer"),
    )
    conn.execute(
        """INSERT INTO transactions (
            public_id,intent,intent_type,transaction_date,status,amount,currency,merchant,raw_input
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "txn-receipt",
            "Expense",
            "Manual",
            "2026-09-23",
            "active",
            "12.34",
            "SGD",
            "Cafe",
            "original receipt raw input",
        ),
    )
    original = build_authoritative_snapshot(
        snapshot_public_id="original-snapshot",
        calculation_type="receipt_split",
        aggregate_public_id="group-1",
        input_payload={"original": True},
        output_payload={"total_paid": "12.34"},
        rules_payload={"original": True},
        money_contract_version="money-v1",
        currency_contract_version="currency-SGD-v1",
        algorithm_version="receipt-split-v1",
        source_references=("synthetic:receipt",),
        actor_type="human",
        actor_public_id="synthetic-actor",
        authorization_reference="original-auth",
        finalization_status="finalized",
        created_at="2026-09-23T00:00:00.000000Z",
    )
    AuthoritativeSnapshotRepository(conn).insert(original)
    conn.commit()
    authority = _Authority()
    source = _ReceiptSource(original.combined_snapshot_hash)
    return conn, CorrectionService(conn, source, authority), authority


def _decision(plan: object, epoch: int) -> SignedDecision:
    return SignedDecision(
        canonical_json_text(
            {
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "authority_id": plan.authority_id,
                "target_id": plan.target_id,
                "source_hash": plan.source_hash,
                "actor": plan.actor,
                "instance_id": plan.instance_id,
                "issued_at_epoch": epoch,
                "expires_at_epoch": epoch + 10,
                "nonce": plan.plan_hash,
            }
        ),
        "d" * 64,
    )


def test_text_null_merchant_amount_correction_replay_and_append_only() -> None:
    conn, service, authority = _fixture()
    try:
        authority.epoch = 1_795_000_000
        # Preview creation time is local; keep the test clock within its 600-second plan.
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            plan = service.preview("txn-1", {"amount": "15.00"}, "Corrected total")
        finally:
            core.time.time = old_time
        assert plan.before.merchant is None
        assert plan.after.merchant is None
        decision = _decision(plan, authority.epoch)
        first = service.apply(plan.plan_id, decision)
        assert first.current.version == 1
        assert first.current.fields.amount == "15.00"
        assert first.current.fields.merchant is None
        original = conn.execute(
            "SELECT amount, raw_input FROM transactions WHERE public_id='txn-1'"
        ).fetchone()
        assert tuple(original) == (
            12.34,
            "original receipt-free raw input",
        )
        duplicate = service.apply(plan.plan_id, decision)
        assert duplicate.recovered
        assert duplicate.applied.correction_id == first.applied.correction_id
        assert service.recover(plan.plan_id) == duplicate
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE correction_versions SET reason = 'rewrite' WHERE correction_id = ?",
                (plan.correction_id,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_deleted_correction_rows_cannot_reveal_original_money() -> None:
    conn, service, authority = _fixture()
    try:
        plan = service.preview("txn-1", {"amount": "15"}, "Correct amount")
        authority.epoch = plan.created_at_epoch + 1
        assert service.apply(plan.plan_id, _decision(plan, authority.epoch)).current.version == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM financial_audit_events "
                "WHERE aggregate_type='transaction' AND aggregate_public_id='txn-1' "
                "AND event_type='transaction_correction_applied'"
            ).fetchone()[0]
            == 1
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        saved_triggers = []
        for table in (
            "correction_receipt_facts",
            "correction_versions",
            "correction_authorities",
            "correction_plans",
            "correction_targets",
        ):
            trigger = f"trg_{table}_no_delete"
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
            ).fetchone()[0]
            saved_triggers.append(definition)
            conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute(f"DELETE FROM {table}")
        for definition in saved_triggers:
            conn.execute(definition)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(CorrectionIntegrityError, match="audit events"):
            service.lookup("txn-1")
    finally:
        conn.close()


def test_orphan_correction_audit_rejected_but_original_audit_preserved() -> None:
    conn, service, _authority = _fixture()
    try:
        for event_type, causation in (
            ("transaction_created", "original-txn-1"),
            ("transaction_correction_applied", "orphan-correction"),
        ):
            command = AuditEventCommand(
                event_public_id=derive_audit_event_public_id(
                    aggregate_type="transaction",
                    aggregate_public_id="txn-1",
                    event_type=event_type,
                    causation_public_id=causation,
                ),
                aggregate_type="transaction",
                aggregate_public_id="txn-1",
                event_type=event_type,
                event_payload={"synthetic": True},
                new_state={"amount": "12.34"},
                actor_type="human",
                actor_public_id="synthetic-actor",
                correlation_public_id="txn-1",
                causation_public_id=causation,
                created_at="2026-09-23T00:00:00+00:00",
            )
            conn.execute("BEGIN")
            append_financial_audit_event(conn, command)
            conn.commit()
            if event_type == "transaction_created":
                assert service.lookup("txn-1").version == 0
        with pytest.raises(CorrectionIntegrityError, match="audit events"):
            service.lookup("txn-1")
    finally:
        conn.close()


def test_extra_correction_audit_after_valid_version_is_rejected() -> None:
    conn, service, authority = _fixture()
    try:
        plan = service.preview("txn-1", {"amount": "15"}, "Correct amount")
        authority.epoch = plan.created_at_epoch + 1
        service.apply(plan.plan_id, _decision(plan, authority.epoch))
        assert service.lookup("txn-1").version == 1
        orphan = "orphan-correction"
        command = AuditEventCommand(
            event_public_id=derive_audit_event_public_id(
                aggregate_type="transaction",
                aggregate_public_id="txn-1",
                event_type="transaction_correction_applied",
                causation_public_id=orphan,
            ),
            aggregate_type="transaction",
            aggregate_public_id="txn-1",
            event_type="transaction_correction_applied",
            event_payload={"synthetic": True},
            new_state={"amount": "15.00"},
            actor_type="human",
            actor_public_id="synthetic-actor",
            correlation_public_id="txn-1",
            causation_public_id=orphan,
            created_at="2026-09-23T00:00:00+00:00",
        )
        conn.execute("BEGIN")
        append_financial_audit_event(conn, command)
        conn.commit()
        with pytest.raises(CorrectionIntegrityError, match="audit events"):
            service.lookup("txn-1")
    finally:
        conn.close()


def test_stale_plan_and_failure_before_commit_leave_no_partial_authority() -> None:
    conn, service, authority = _fixture()
    try:
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            first = service.preview("txn-1", {"amount": "15"}, "First correction")
            stale = service.preview("txn-1", {"amount": "16"}, "Competing correction")
        finally:
            core.time.time = old_time
        service.apply(first.plan_id, _decision(first, authority.epoch))
        with pytest.raises(CorrectionConflict):
            service.apply(stale.plan_id, _decision(stale, authority.epoch))
        assert conn.execute("SELECT COUNT(*) FROM correction_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM correction_authorities").fetchone()[0] == 1
        assert service.recover(stale.plan_id) is None
    finally:
        conn.close()


def test_two_versions_and_old_reply_recovery_reports_current_second_version() -> None:
    conn, service, authority = _fixture()
    try:
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            first = service.preview("txn-1", {"amount": "15"}, "Correct amount")
            service.apply(first.plan_id, _decision(first, authority.epoch))
            second = service.preview("txn-1", {"date": "2026-09-24"}, "Correct date")
            service.apply(second.plan_id, _decision(second, authority.epoch))
        finally:
            core.time.time = old_time
        recovered = service.apply(first.plan_id, _decision(first, authority.epoch))
        assert recovered.recovered
        assert recovered.applied.version == 1
        assert recovered.current.version == 2
        assert recovered.current.fields.amount == "15.00"
        assert recovered.current.fields.transaction_date == "2026-09-24"
        original_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE public_id='txn-1'"
        ).fetchone()[0]
        assert original_count == 1
        assert conn.execute("SELECT COUNT(*) FROM correction_versions").fetchone()[0] == 2
    finally:
        conn.close()


def test_read_paths_reject_changed_current_instance() -> None:
    conn, service, authority = _fixture()
    try:
        plan = service.preview("txn-1", {"amount": "15"}, "Correct amount")
        authority.epoch = plan.created_at_epoch + 1
        service.apply(plan.plan_id, _decision(plan, authority.epoch))
        authority.instance_id = "instance-2"
        with pytest.raises((CorrectionConflict, ValueError), match="instance|binding"):
            service.lookup("txn-1")
        with pytest.raises((CorrectionConflict, ValueError), match="instance|binding"):
            service.read_plan(plan.plan_id)
        with pytest.raises((CorrectionConflict, ValueError), match="instance|binding"):
            service.recover(plan.plan_id)
    finally:
        conn.close()


def test_receipt_date_only_adds_complete_fact_and_snapshot_with_one_apply_epoch() -> None:
    conn, service, authority = _receipt_fixture()
    try:
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            plan = service.preview("txn-receipt", {"date": "2026-09-24"}, "Correct date")
        finally:
            core.time.time = old_time
        assert plan.before.amount == plan.after.amount == "12.34"
        assert conn.execute("SELECT COUNT(*) FROM correction_receipt_facts").fetchone()[0] == 0
        applied = service.apply(plan.plan_id, _decision(plan, authority.epoch))
        assert applied.current.version == 1
        fact = conn.execute(
            "SELECT fact_json,snapshot_id,snapshot_created_at FROM correction_receipt_facts"
        ).fetchone()
        payload = canonical_json_value(fact[0])
        assert payload["effective_fields"]["transaction_date"] == "2026-09-24"
        assert len(payload["lines"]) == 1
        assert fact[1] == plan.snapshot_id
        assert fact[2] == core._utc(authority.epoch)
        assert service.lookup("txn-receipt").fields.amount == "12.34"
        assert (
            conn.execute(
                "SELECT amount FROM transactions WHERE public_id='txn-receipt'"
            ).fetchone()[0]
            == 12.34
        )
    finally:
        conn.close()


def test_receipt_second_merchant_snapshot_links_immediate_predecessor_and_survives_retirement() -> (
    None
):
    conn, service, authority = _receipt_fixture()
    try:
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            first = service.preview("txn-receipt", {"date": "2026-09-24"}, "Correct date")
            service.apply(first.plan_id, _decision(first, authority.epoch))
            second = service.preview("txn-receipt", {"merchant": "New Cafe"}, "Correct merchant")
            service.apply(second.plan_id, _decision(second, authority.epoch))
        finally:
            core.time.time = old_time
        assert second.snapshot_id != first.snapshot_id
        assert second.after.merchant == "New Cafe"
        previous = conn.execute(
            "SELECT previous_snapshot_id,previous_snapshot_hash FROM correction_receipt_facts "
            "WHERE correction_id = ?",
            (second.correction_id,),
        ).fetchone()
        assert tuple(previous) == (first.snapshot_id, first.snapshot_hash)
        conn.execute("UPDATE participants SET is_active=0 WHERE public_id='payer-1'")
        conn.commit()
        current = service.lookup("txn-receipt")
        assert current.version == 2
        assert current.fields.merchant == "New Cafe"
    finally:
        conn.close()


def test_expired_decision_and_caller_transaction_do_not_consume_authority() -> None:
    conn, service, authority = _fixture()
    try:
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            plan = service.preview("txn-1", {"amount": "15"}, "Correct total")
        finally:
            core.time.time = old_time
        authority.epoch += 11
        with pytest.raises(Exception, match="Decision evidence"):
            service.apply(plan.plan_id, _decision(plan, plan.created_at_epoch))
        assert conn.execute("SELECT COUNT(*) FROM correction_authorities").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM correction_versions").fetchone()[0] == 0
        conn.execute("BEGIN")
        with pytest.raises(CorrectionConflict, match="idle connection"):
            service.apply(plan.plan_id, _decision(plan, plan.created_at_epoch))
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()


def test_failure_after_version_insert_rolls_back_and_reply_loss_recovers() -> None:
    conn, service, authority = _fixture()
    try:
        import finance_core.application.corrections as core

        old_time = core.time.time
        core.time.time = lambda: authority.epoch
        try:
            plan = service.preview("txn-1", {"amount": "15"}, "Correct total")
        finally:
            core.time.time = old_time
        decision = _decision(plan, authority.epoch)
        original_insert = service._insert_authority
        service._insert_authority = lambda *args: (_ for _ in ()).throw(RuntimeError("injected"))
        try:
            with pytest.raises(RuntimeError, match="injected"):
                service.apply(plan.plan_id, decision)
        finally:
            service._insert_authority = original_insert
        assert conn.execute("SELECT COUNT(*) FROM correction_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM correction_authorities").fetchone()[0] == 0
        original_lookup = service.lookup
        service.lookup = lambda *args: (_ for _ in ()).throw(RuntimeError("reply lost"))
        try:
            with pytest.raises(RuntimeError, match="reply lost"):
                service.apply(plan.plan_id, decision)
        finally:
            service.lookup = original_lookup
        recovered = service.recover(plan.plan_id)
        assert recovered is not None and recovered.current.version == 1
        assert recovered.applied.correction_id == plan.correction_id
    finally:
        conn.close()


def test_explicit_currency_amount_precision_precedes_date_and_reason() -> None:
    conn, service, _ = _fixture()
    try:
        with pytest.raises(Exception, match="Invalid currency"):
            service.preview("txn-1", {"currency": "BAD", "amount": "1.001", "date": "invalid"}, "")
        with pytest.raises(Exception, match="precision"):
            service.preview("txn-1", {"currency": "SGD", "amount": "1.001", "date": "invalid"}, "")
        assert conn.execute("SELECT COUNT(*) FROM correction_plans").fetchone()[0] == 0
    finally:
        conn.close()
