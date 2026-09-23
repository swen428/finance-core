"""Synthetic, isolated personal-receipt correction material checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from finance_core.application.correction_receipts import (
    CorrectionReceiptError,
    prepare_receipt_material,
    verify_historical_receipt_material,
)
from finance_core.application.correction_relationships import (
    CorrectionRelationshipError,
    assert_correction_eligible,
)
from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeSnapshotRepository,
    build_authoritative_snapshot,
    canonical_json_bytes,
    canonical_json_value,
)


@dataclass(frozen=True)
class _Fields:
    amount: str = "12.34"
    currency: str = "SGD"
    transaction_date: str = "2026-09-23"
    merchant: str | None = "Cafe"


@dataclass(frozen=True)
class _Source:
    target_id: str
    route: str
    actor: str
    source_hash: str
    original_hash: str
    evidence_refs: tuple[str, ...]
    receipt_id: str
    fact_set_id: str
    fact_set_version: int
    fact_input_hash: str
    fact_result_hash: str
    snapshot_id: str
    snapshot_hash: str
    payer_id: str
    aggregate_id: str


def _fixture() -> tuple[sqlite3.Connection, _Source]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE participants (public_id TEXT PRIMARY KEY, is_self INTEGER, is_active INTEGER)"
    )
    conn.execute("INSERT INTO participants VALUES ('payer-1', 1, 1)")
    conn.execute(
        """CREATE TABLE authoritative_calculation_snapshots (
        snapshot_public_id TEXT PRIMARY KEY, snapshot_schema_version TEXT,
        calculation_type TEXT, aggregate_public_id TEXT, input_payload_json TEXT,
        output_payload_json TEXT, rules_payload_json TEXT, input_hash TEXT,
        output_hash TEXT, rules_hash TEXT, combined_snapshot_hash TEXT,
        money_contract_version TEXT, currency_contract_version TEXT,
        algorithm_version TEXT, source_references_json TEXT,
        previous_snapshot_public_id TEXT, actor_type TEXT, actor_public_id TEXT,
        authorization_reference TEXT, finalization_status TEXT, created_at TEXT
        )"""
    )
    original = build_authoritative_snapshot(
        snapshot_public_id="snap-original",
        calculation_type="receipt_split",
        aggregate_public_id="group-1",
        input_payload={"synthetic": "original"},
        output_payload={"total_paid": "10.00"},
        rules_payload={"algorithm": "receipt_split"},
        money_contract_version="money-v1",
        currency_contract_version="currency-SGD-v1",
        algorithm_version="receipt-split-v1",
        source_references=("receipt:receipt-1",),
        actor_type="human",
        actor_public_id="actor-1",
        authorization_reference="auth-original",
        finalization_status="finalized",
        created_at="2026-09-22T00:00:00.000000Z",
    )
    AuthoritativeSnapshotRepository(conn).insert(original)
    source = _Source(
        target_id="txn-1",
        route="receipt",
        actor="actor-1",
        source_hash="a" * 64,
        original_hash="b" * 64,
        evidence_refs=("original-evidence:1",),
        receipt_id="receipt-1",
        fact_set_id="facts-1",
        fact_set_version=1,
        fact_input_hash="c" * 64,
        fact_result_hash="d" * 64,
        snapshot_id="snap-original",
        snapshot_hash=original.combined_snapshot_hash,
        payer_id="payer-1",
        aggregate_id="group-1",
    )
    return conn, source


def _material(conn: sqlite3.Connection, source: _Source, fields: _Fields, created_at: str):
    return prepare_receipt_material(
        conn,
        source,
        fields,
        "correction-fact-1",
        "snap-correction-1",
        "auth-correction-1",
        "plan-1",
        source.snapshot_id,
        source.snapshot_hash,
        created_at,
    )


def test_receipt_material_binds_full_personal_total_and_preserves_preview_hash() -> None:
    conn, source = _fixture()
    try:
        preview = _material(conn, source, _Fields(), "2026-09-23T00:00:00.000000Z")
        applied = _material(conn, source, _Fields(), "2026-09-23T01:02:03.000000Z")
        assert preview.fact_json == applied.fact_json
        assert preview.fact_hash == applied.fact_hash
        assert preview.snapshot.combined_snapshot_hash == applied.snapshot.combined_snapshot_hash
        assert preview.snapshot.created_at != applied.snapshot.created_at
        assert (
            preview.fact_hash
            == hashlib.sha256(
                canonical_json_bytes(canonical_json_value(preview.fact_json))
            ).hexdigest()
        )
        fact = canonical_json_value(preview.fact_json)
        assert fact["lines"][0]["quantity"] is None
        assert fact["lines"][0]["unit_price"] is None
        assert fact["lines"][0]["allocations"] == {"payer-1": "12.34"}
        assert "snap-correction-1" not in preview.fact_json
        output = canonical_json_value(preview.calculator_output_json)
        assert output["total_paid"] == output["payer_own_share"] == "12.34"
        assert output["total_to_collect"] == "0.00"
        assert output["settlement_obligations"] == []
        assert preview.snapshot.previous_snapshot_public_id == source.snapshot_id
        assert preview.snapshot.authorization_reference == "auth-correction-1"
    finally:
        conn.close()


def test_receipt_date_only_still_builds_new_snapshot_and_amount_changes_fact() -> None:
    conn, source = _fixture()
    try:
        original = _material(conn, source, _Fields(), "2026-09-23T00:00:00.000000Z")
        changed_date = _material(
            conn,
            source,
            replace(_Fields(), transaction_date="2026-09-24"),
            "2026-09-23T00:00:00.000000Z",
        )
        changed_amount = _material(
            conn,
            source,
            replace(_Fields(), amount="14.00"),
            "2026-09-23T00:00:00.000000Z",
        )
        assert (
            original.snapshot.combined_snapshot_hash != changed_date.snapshot.combined_snapshot_hash
        )
        assert original.fact_hash != changed_date.fact_hash
        assert canonical_json_value(changed_amount.calculator_output_json)["total_paid"] == "14.00"
    finally:
        conn.close()


def test_receipt_material_refuses_lost_self_or_wrong_previous_snapshot() -> None:
    conn, source = _fixture()
    try:
        conn.execute("UPDATE participants SET is_active = 0 WHERE public_id = 'payer-1'")
        with pytest.raises(CorrectionReceiptError, match="unique active self"):
            _material(conn, source, _Fields(), "2026-09-23T00:00:00.000000Z")
        conn.execute("UPDATE participants SET is_active = 1 WHERE public_id = 'payer-1'")
        with pytest.raises(CorrectionReceiptError, match="hash changed"):
            prepare_receipt_material(
                conn,
                source,
                _Fields(),
                "fact-1",
                "snap-1",
                "auth-1",
                "plan-1",
                source.snapshot_id,
                "0" * 64,
                "2026-09-23T00:00:00.000000Z",
            )
    finally:
        conn.close()


def test_historical_receipt_uses_frozen_self_after_participant_retirement() -> None:
    conn, source = _fixture()
    try:
        applied = _material(conn, source, _Fields(), "2026-09-23T01:02:03.000000Z")
        AuthoritativeSnapshotRepository(conn).insert(applied.snapshot)
        conn.execute("UPDATE participants SET is_active = 0 WHERE public_id = 'payer-1'")
        verified = verify_historical_receipt_material(
            conn,
            source,
            _Fields(),
            "correction-fact-1",
            "snap-correction-1",
            "auth-correction-1",
            "plan-1",
            source.snapshot_id,
            source.snapshot_hash,
            applied.fact_json,
            applied.fact_hash,
            applied.snapshot.combined_snapshot_hash,
            applied.snapshot.created_at,
            applied.frozen_self_json,
        )
        assert verified == applied
        with pytest.raises(CorrectionReceiptError, match="historical receipt fact"):
            verify_historical_receipt_material(
                conn,
                source,
                _Fields(),
                "correction-fact-1",
                "snap-correction-1",
                "auth-correction-1",
                "plan-1",
                source.snapshot_id,
                source.snapshot_hash,
                applied.fact_json,
                "0" * 64,
                applied.snapshot.combined_snapshot_hash,
                applied.snapshot.created_at,
                applied.frozen_self_json,
            )
    finally:
        conn.close()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, public_id TEXT, status TEXT,
            account_id INTEGER, from_account_id INTEGER, to_account_id INTEGER,
            investment_account_id INTEGER, from_participant_id INTEGER,
            to_participant_id INTEGER, from_amount TEXT, to_amount TEXT,
            exchange_rate TEXT, fee_amount TEXT, withholding_tax_amount TEXT,
            split_type TEXT, adjustment_type TEXT
        );
        CREATE TABLE shared_expense_obligations (
            public_id TEXT, shared_expense_transaction_id INTEGER
        );
        CREATE TABLE transaction_links (
            public_id TEXT, status TEXT,
            source_transaction_id INTEGER, target_transaction_id INTEGER
        );
        CREATE TABLE reconciliation_records (
            public_id TEXT, manual_transaction_id INTEGER, generated_transaction_id INTEGER
        );
        CREATE TABLE receipts (id INTEGER PRIMARY KEY, public_id TEXT);
        CREATE TABLE receipt_groups (id INTEGER PRIMARY KEY, public_id TEXT);
        CREATE TABLE receipt_group_receipts (
            receipt_group_id INTEGER, receipt_id INTEGER
        );
        CREATE TABLE calculation_runs (
            id INTEGER PRIMARY KEY, receipt_id INTEGER, receipt_group_id INTEGER
        );
        CREATE TABLE settlement_obligations (
            public_id TEXT, settlement_status TEXT, source_calculation_run_id INTEGER
        );
        CREATE TABLE reconciliation_review_queue (
            public_id TEXT, app_transaction_ref TEXT, status TEXT
        );
        CREATE TABLE reconciliation_resolution_decisions (
            public_id TEXT, review_queue_public_id TEXT, decision_action TEXT
        );
        CREATE TABLE reconciliation_resolution_results (
            public_id TEXT, audit_evidence_json TEXT, decision_public_id TEXT,
            review_queue_public_id TEXT, success INTEGER
        );
        CREATE TABLE reconciliation_apply_results (
            apply_id TEXT, action TEXT, payload_json TEXT,
            app_transaction_reference_json TEXT, success INTEGER
        );
        CREATE TABLE reconciliation_final_mutation_audit (
            final_mutation_id TEXT, action TEXT, status TEXT,
            source_app_transaction_ref TEXT, target_transaction_id TEXT,
            transaction_public_id TEXT
        );
        CREATE TABLE reconciliation_guarded_apply_executions (
            execution_id TEXT PRIMARY KEY, execution_status TEXT,
            total_operations INTEGER, operations_executed INTEGER,
            operations_blocked INTEGER, operations_skipped INTEGER,
            is_dry_run INTEGER, audit_trail_json TEXT
        );
        CREATE TABLE reconciliation_guarded_apply_operation_results (
            operation_result_id TEXT PRIMARY KEY, execution_id TEXT,
            execution_status TEXT, guard_decision_approved INTEGER,
            mutation_type TEXT, mutation_payload_json TEXT
        );"""
    )
    conn.execute("INSERT INTO transactions (id, public_id, status) VALUES (1, 'txn-1', 'active')")
    return conn


def _source(route: str = "text") -> SimpleNamespace:
    return SimpleNamespace(
        target_id="txn-1",
        route=route,
        receipt_id="receipt-1" if route == "receipt" else None,
    )


def _guarded_claim(
    conn: sqlite3.Connection, *, claim_id: str, target_id: str,
    dry_run: int = 1, execution_status: str = "executed",
    operation_status: str = "executed", payload: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO reconciliation_guarded_apply_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim_id, execution_status, 1,
            1 if execution_status == "executed" else 0,
            1 if execution_status == "blocked" else 0,
            0, dry_run, json.dumps({"is_dry_run": bool(dry_run)}),
        ),
    )
    conn.execute(
        "INSERT INTO reconciliation_guarded_apply_operation_results VALUES (?, ?, ?, ?, ?, ?)",
        (
            f"op-{claim_id}", claim_id, operation_status, 1,
            "confirm_match",
            payload if payload is not None else json.dumps({"app_transaction_ref": target_id}),
        ),
    )


def test_valid_guarded_dry_run_and_unrelated_claim_leave_target_clear() -> None:
    conn = _conn()
    try:
        _guarded_claim(conn, claim_id="dry-target", target_id="txn-1")
        _guarded_claim(conn, claim_id="dry-other", target_id="txn-other")
        _guarded_claim(conn, claim_id="non-dry-other", target_id="txn-other", dry_run=0)
        assert_correction_eligible(conn, _source())
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("dry_run", "execution_status", "operation_status", "payload"),
    [
        (0, "executed", "executed", None),
        (1, "executed", "blocked", None),
        (1, "executed", "executed", '{"app_transaction_ref":"txn-1",'),
    ],
)
def test_related_guarded_claim_with_impossible_or_malformed_state_is_unknown(
    dry_run: int, execution_status: str, operation_status: str, payload: str | None,
) -> None:
    conn = _conn()
    try:
        _guarded_claim(
            conn, claim_id="bad-target", target_id="txn-1", dry_run=dry_run,
            execution_status=execution_status, operation_status=operation_status,
            payload=payload,
        )
        with pytest.raises(CorrectionRelationshipError) as blocked:
            assert_correction_eligible(conn, _source())
        assert blocked.value.classification == "UNKNOWN_INTEGRITY"
        assert blocked.value.evidence_ids == ("guarded-apply:bad-target",)
    finally:
        conn.close()


def test_clear_target_ignores_proposals_failed_results_and_blocked_attempts() -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO reconciliation_apply_results VALUES (?, ?, ?, ?, ?)",
            ("failed-1", "mark_duplicate", "{}", None, 0),
        )
        conn.execute(
            "INSERT INTO reconciliation_final_mutation_audit VALUES (?, ?, ?, ?, ?, ?)",
            ("blocked-1", "adjust_final_transaction_proposal", "blocked", "txn-1", None, None),
        )
        assert_correction_eligible(conn, _source())
    finally:
        conn.close()


def test_shared_obligation_and_unproven_link_refuse_distinctly() -> None:
    conn = _conn()
    try:
        conn.execute("INSERT INTO shared_expense_obligations VALUES ('share-1', 1)")
        with pytest.raises(CorrectionRelationshipError) as active:
            assert_correction_eligible(conn, _source())
        assert active.value.classification == "ACTIVE_RELATIONSHIP"
        assert active.value.evidence_ids == ("shared-obligation:share-1",)
        conn.execute("DELETE FROM shared_expense_obligations")
        conn.execute("INSERT INTO transaction_links VALUES ('link-1', 'archived', 1, 2)")
        with pytest.raises(CorrectionRelationshipError) as unknown:
            assert_correction_eligible(conn, _source())
        assert unknown.value.classification == "UNKNOWN_INTEGRITY"
    finally:
        conn.close()


def test_duplicate_secondary_reference_is_active_and_missing_list_is_unknown() -> None:
    conn = _conn()
    try:
        payload = {
            "action_type": "mark_duplicate",
            "duplicate_app_txn_ids": ["kept-1", "txn-1"],
            "kept_app_txn_id": "kept-1",
            "audit_only": True,
        }
        conn.execute(
            "INSERT INTO reconciliation_apply_results VALUES (?, ?, ?, ?, ?)",
            ("apply-1", "mark_duplicate", json.dumps(payload), json.dumps("kept-1"), 1),
        )
        with pytest.raises(CorrectionRelationshipError) as active:
            assert_correction_eligible(conn, _source())
        assert active.value.classification == "ACTIVE_RELATIONSHIP"
        assert active.value.evidence_ids == ("reconciliation-apply:apply-1",)
        conn.execute("DELETE FROM reconciliation_apply_results")
        conn.execute(
            "INSERT INTO reconciliation_apply_results VALUES (?, ?, ?, ?, ?)",
            ("apply-bad", "mark_duplicate", "{}", None, 1),
        )
        with pytest.raises(CorrectionRelationshipError) as unknown:
            assert_correction_eligible(conn, _source())
        assert unknown.value.classification == "UNKNOWN_INTEGRITY"
    finally:
        conn.close()


def test_receipt_settlement_lineage_and_incomplete_old_duplicate_refuse() -> None:
    conn = _conn()
    try:
        conn.execute("INSERT INTO receipts VALUES (10, 'receipt-1')")
        conn.execute("INSERT INTO receipt_groups VALUES (20, 'group-1')")
        conn.execute("INSERT INTO receipt_group_receipts VALUES (20, 10)")
        conn.execute("INSERT INTO calculation_runs VALUES (30, NULL, 20)")
        conn.execute("INSERT INTO settlement_obligations VALUES ('settle-1', 'open', 30)")
        with pytest.raises(CorrectionRelationshipError) as active:
            assert_correction_eligible(conn, _source("receipt"))
        assert active.value.classification == "ACTIVE_RELATIONSHIP"
        conn.execute("DELETE FROM settlement_obligations")
        conn.execute(
            "INSERT INTO reconciliation_review_queue VALUES ('queue-1', 'kept-1', 'resolved')"
        )
        conn.execute(
            "INSERT INTO reconciliation_resolution_decisions VALUES "
            "('decision-1', 'queue-1', 'mark_duplicate')"
        )
        conn.execute(
            "INSERT INTO reconciliation_resolution_results VALUES (?, ?, ?, ?, ?)",
            ("result-1", json.dumps({"app_txn_id": "kept-1"}), "decision-1", "queue-1", 1),
        )
        with pytest.raises(CorrectionRelationshipError) as unknown:
            assert_correction_eligible(conn, _source("receipt"))
        assert unknown.value.classification == "UNKNOWN_INTEGRITY"
    finally:
        conn.close()


def test_successful_resolution_requires_complete_matching_target_claim() -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO reconciliation_review_queue VALUES ('queue-1', 'txn-1', 'resolved')"
        )
        conn.execute(
            "INSERT INTO reconciliation_resolution_decisions VALUES "
            "('decision-1', 'queue-1', 'confirm_match')"
        )
        evidence = {
            "app_txn_id": "txn-1",
            "canonical_app_transaction_ref": "txn-1",
            "payload_target_app_txn_id": "txn-1",
        }
        conn.execute(
            "INSERT INTO reconciliation_resolution_results VALUES (?, ?, ?, ?, ?)",
            ("result-1", json.dumps(evidence), "decision-1", "queue-1", 1),
        )
        with pytest.raises(CorrectionRelationshipError) as active:
            assert_correction_eligible(conn, _source())
        assert active.value.classification == "ACTIVE_RELATIONSHIP"
        evidence["payload_target_app_txn_id"] = "other-transaction"
        conn.execute(
            "UPDATE reconciliation_resolution_results SET audit_evidence_json = ?",
            (json.dumps(evidence),),
        )
        with pytest.raises(CorrectionRelationshipError) as unknown:
            assert_correction_eligible(conn, _source())
        assert unknown.value.classification == "UNKNOWN_INTEGRITY"
    finally:
        conn.close()
