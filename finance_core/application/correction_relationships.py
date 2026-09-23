"""Fail-closed relationship eligibility for an already finalized D2 original.

Only committed effects matter. Suggestions, failed attempts and dry runs do
not create a relationship, while malformed related success claims are never
silently treated as absent.
"""

from __future__ import annotations

import json
import sqlite3
from typing import NoReturn, Protocol, cast

from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.financial_audit import verify_financial_audit_chain


class RelationshipSource(Protocol):
    @property
    def target_id(self) -> str: ...

    @property
    def route(self) -> str: ...

    @property
    def receipt_id(self) -> str | None: ...


class CorrectionRelationshipError(ValueError):
    """An active or unprovable business relationship bars correction."""

    def __init__(self, classification: str, evidence_ids: tuple[str, ...]) -> None:
        self.classification = classification
        self.evidence_ids = evidence_ids
        super().__init__(f"{classification}: {', '.join(evidence_ids)}")


def _refuse(classification: str, *ids: str) -> NoReturn:
    raise CorrectionRelationshipError(classification, tuple(sorted(set(ids))))


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
    cursor = conn.execute(sql, args)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _object(raw: object, label: str) -> dict[str, object]:
    if type(raw) is not str:
        _refuse("UNKNOWN_INTEGRITY", label)
    try:

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=pairs)
    except (TypeError, ValueError):
        _refuse("UNKNOWN_INTEGRITY", label)
    if type(value) is not dict:
        _refuse("UNKNOWN_INTEGRITY", label)
    return value


def _optional_ref(raw: object, label: str) -> str | None:
    if raw is None:
        return None
    if type(raw) is not str:
        _refuse("UNKNOWN_INTEGRITY", label)
    try:
        value = json.loads(raw)
    except ValueError:
        _refuse("UNKNOWN_INTEGRITY", label)
    if type(value) is not str or not value:
        _refuse("UNKNOWN_INTEGRITY", label)
    return value


def _base_relationships(conn: sqlite3.Connection, target_id: str) -> int:
    target = _rows(
        conn,
        """SELECT id, status, account_id, from_account_id, to_account_id,
        investment_account_id, from_participant_id, to_participant_id,
        from_amount, to_amount, exchange_rate, fee_amount, withholding_tax_amount,
        split_type, adjustment_type FROM transactions WHERE public_id = ?""",
        (target_id,),
    )
    if len(target) != 1:
        _refuse("UNKNOWN_INTEGRITY", f"transaction:{target_id}")
    record = target[0]
    if record["status"] != "active":
        _refuse("ACTIVE_RELATIONSHIP", f"transaction:{target_id}:status")
    unsupported = (
        "account_id",
        "from_account_id",
        "to_account_id",
        "investment_account_id",
        "from_participant_id",
        "to_participant_id",
        "from_amount",
        "to_amount",
        "exchange_rate",
        "fee_amount",
        "withholding_tax_amount",
        "split_type",
        "adjustment_type",
    )
    if any(record[name] is not None for name in unsupported):
        _refuse("ACTIVE_RELATIONSHIP", f"transaction:{target_id}:unsupported-fields")
    internal_id = record["id"]
    obligations = _rows(
        conn,
        "SELECT public_id FROM shared_expense_obligations "
        "WHERE shared_expense_transaction_id = ? ORDER BY public_id",
        (internal_id,),
    )
    if obligations:
        _refuse(
            "ACTIVE_RELATIONSHIP",
            *(f"shared-obligation:{row['public_id']}" for row in obligations),
        )
    links = _rows(
        conn,
        "SELECT public_id, status FROM transaction_links "
        "WHERE source_transaction_id = ? OR target_transaction_id = ? ORDER BY public_id",
        (internal_id, internal_id),
    )
    for row in links:
        classification = "ACTIVE_RELATIONSHIP" if row["status"] == "active" else "UNKNOWN_INTEGRITY"
        _refuse(classification, f"transaction-link:{row['public_id']}")
    legacy = _rows(
        conn,
        "SELECT public_id FROM reconciliation_records "
        "WHERE manual_transaction_id = ? OR generated_transaction_id = ? ORDER BY public_id",
        (internal_id, internal_id),
    )
    if legacy:
        _refuse(
            "UNKNOWN_INTEGRITY",
            *(f"legacy-reconciliation:{row['public_id']}" for row in legacy),
        )
    if type(internal_id) is not int:
        _refuse("UNKNOWN_INTEGRITY", f"transaction:{target_id}:identity")
    return internal_id


def _settlement_relationships(conn: sqlite3.Connection, receipt_id: str | None) -> None:
    if receipt_id is None:
        return
    related = _rows(
        conn,
        """SELECT DISTINCT obligations.public_id, obligations.settlement_status
        FROM settlement_obligations AS obligations
        JOIN calculation_runs AS runs ON runs.id = obligations.source_calculation_run_id
        LEFT JOIN receipts AS direct_receipt ON direct_receipt.id = runs.receipt_id
        LEFT JOIN receipt_groups AS groups ON groups.id = runs.receipt_group_id
        LEFT JOIN receipt_group_receipts AS membership ON membership.receipt_group_id = groups.id
        LEFT JOIN receipts AS group_receipt ON group_receipt.id = membership.receipt_id
        WHERE direct_receipt.public_id = ? OR group_receipt.public_id = ?
        ORDER BY obligations.public_id""",
        (receipt_id, receipt_id),
    )
    for row in related:
        classification = (
            "ACTIVE_RELATIONSHIP"
            if row["settlement_status"] in {"open", "partially_settled", "settled", "waived"}
            else "UNKNOWN_INTEGRITY"
        )
        _refuse(classification, f"settlement-obligation:{row['public_id']}")


def _resolution_relationships(conn: sqlite3.Connection, target_id: str) -> None:
    results = _rows(
        conn,
        """SELECT results.public_id AS result_id, results.audit_evidence_json,
        results.decision_public_id, results.review_queue_public_id,
        decisions.decision_action, decisions.review_queue_public_id AS decision_queue_id,
        queues.app_transaction_ref, queues.status AS queue_status
        FROM reconciliation_resolution_results AS results
        LEFT JOIN reconciliation_resolution_decisions AS decisions
          ON decisions.public_id = results.decision_public_id
        LEFT JOIN reconciliation_review_queue AS queues
          ON queues.public_id = results.review_queue_public_id
        WHERE results.success = 1 ORDER BY results.public_id""",
        (),
    )
    for row in results:
        action = row["decision_action"]
        if action not in {"confirm_match", "mark_duplicate"}:
            continue
        label = f"resolution:{row['result_id']}"
        evidence = _object(row["audit_evidence_json"], label)
        queue_ref = row["app_transaction_ref"]
        evidence_ref = evidence.get("app_txn_id")
        canonical_ref = evidence.get("canonical_app_transaction_ref")
        payload_target = evidence.get("payload_target_app_txn_id")
        ids = {value for value in (queue_ref, evidence_ref) if type(value) is str and value}
        if action == "mark_duplicate":
            duplicates = evidence.get("duplicate_app_txn_ids")
            kept = evidence.get("kept_app_txn_id")
            complete = (
                type(duplicates) is list
                and len(duplicates) >= 2
                and all(type(value) is str and value for value in duplicates)
                and len(set(duplicates)) == len(duplicates)
                and type(kept) is str
                and kept in duplicates
                and evidence.get("audit_only") is True
                and canonical_ref == kept
                and payload_target == kept
                and queue_ref == kept
            )
            if not complete:
                # Old success rows may omit secondary duplicate IDs. Their
                # relationship to any target cannot be disproved safely.
                _refuse("UNKNOWN_INTEGRITY", label)
            ids.update(cast(list[str], duplicates))
            ids.add(cast(str, kept))
        if target_id not in ids:
            continue
        if (
            row["decision_queue_id"] != row["review_queue_public_id"]
            or row["queue_status"] != "resolved"
            or type(queue_ref) is not str
            or type(evidence_ref) is not str
            or queue_ref != evidence_ref
            or canonical_ref != queue_ref
            or payload_target != queue_ref
        ):
            _refuse("UNKNOWN_INTEGRITY", label)
        _refuse("ACTIVE_RELATIONSHIP", label)


def _apply_relationships(conn: sqlite3.Connection, target_id: str) -> None:
    results = _rows(
        conn,
        """SELECT apply_id, action, payload_json, app_transaction_reference_json
        FROM reconciliation_apply_results WHERE success = 1
        AND action IN ('confirm_match', 'mark_duplicate') ORDER BY apply_id""",
        (),
    )
    for row in results:
        label = f"reconciliation-apply:{row['apply_id']}"
        payload = _object(row["payload_json"], label)
        app_ref = _optional_ref(row["app_transaction_reference_json"], label)
        action = row["action"]
        if action == "confirm_match":
            canonical_id = payload.get("app_txn_id")
            if type(canonical_id) is not str or not canonical_id:
                _refuse("UNKNOWN_INTEGRITY", label)
            if target_id not in {canonical_id, app_ref}:
                continue
            if app_ref != canonical_id or payload.get("action_type") != action:
                _refuse("UNKNOWN_INTEGRITY", label)
            _refuse("ACTIVE_RELATIONSHIP", label)
        duplicates = payload.get("duplicate_app_txn_ids")
        kept = payload.get("kept_app_txn_id")
        if (
            type(duplicates) is not list
            or len(duplicates) < 2
            or not all(type(value) is str and value for value in duplicates)
            or len(set(duplicates)) != len(duplicates)
            or type(kept) is not str
            or kept not in duplicates
            or payload.get("audit_only") is not True
        ):
            _refuse("UNKNOWN_INTEGRITY", label)
        if target_id not in set(cast(list[str], duplicates)) | {kept, app_ref}:
            continue
        if app_ref is not None and app_ref not in cast(list[str], duplicates):
            _refuse("UNKNOWN_INTEGRITY", label)
        _refuse("ACTIVE_RELATIONSHIP", label)


def _guarded_apply_relationships(conn: sqlite3.Connection, target_id: str) -> None:
    """A guarded dry-run is harmless only when its persisted claim is coherent."""
    operations = _rows(
        conn,
        """SELECT operation_result_id, execution_id, execution_status,
        guard_decision_approved, mutation_payload_json
        FROM reconciliation_guarded_apply_operation_results
        ORDER BY operation_result_id""",
        (),
    )
    for operation in operations:
        execution_id = operation["execution_id"]
        label = f"guarded-apply:{execution_id}"
        payload = _object(operation["mutation_payload_json"], label)
        app_ref = payload.get("app_transaction_ref")
        if app_ref is not None and (type(app_ref) is not str or not app_ref):
            _refuse("UNKNOWN_INTEGRITY", label)
        if app_ref != target_id:
            continue
        parent = _rows(
            conn,
            """SELECT execution_status, total_operations, operations_executed,
            operations_blocked, operations_skipped, is_dry_run, audit_trail_json
            FROM reconciliation_guarded_apply_executions WHERE execution_id = ?""",
            (execution_id,),
        )
        if len(parent) != 1:
            _refuse("UNKNOWN_INTEGRITY", label)
        execution = parent[0]
        audit = _object(execution["audit_trail_json"], label)
        if execution["is_dry_run"] != 1 or audit.get("is_dry_run") is not True:
            _refuse("UNKNOWN_INTEGRITY", label)
        siblings = [op for op in operations if op["execution_id"] == execution_id]
        statuses = [op["execution_status"] for op in siblings]
        executed = statuses.count("executed")
        blocked = statuses.count("blocked")
        skipped = len(statuses) - executed - blocked
        if (
            type(execution["total_operations"]) is not int
            or execution["total_operations"] != len(siblings)
            or execution["operations_executed"] != executed
            or execution["operations_blocked"] != blocked
            or execution["operations_skipped"] != skipped
            or execution["execution_status"] not in {
                "executed", "blocked", "partially_blocked", "unsupported", "conflict"
            }
        ):
            _refuse("UNKNOWN_INTEGRITY", label)
        if execution["execution_status"] == "executed" and executed != len(siblings):
            _refuse("UNKNOWN_INTEGRITY", label)
        if execution["execution_status"] == "blocked" and executed != 0:
            _refuse("UNKNOWN_INTEGRITY", label)
        if any(
            op["execution_status"] == "executed" and op["guard_decision_approved"] != 1
            for op in siblings
        ):
            _refuse("UNKNOWN_INTEGRITY", label)


def _final_mutation_relationships(conn: sqlite3.Connection, target_id: str) -> None:
    rows = _rows(
        conn,
        """SELECT final_mutation_id, action, status, source_app_transaction_ref,
        target_transaction_id, transaction_public_id
        FROM reconciliation_final_mutation_audit
        WHERE source_app_transaction_ref = ? OR target_transaction_id = ?
           OR transaction_public_id = ? ORDER BY final_mutation_id""",
        (target_id, target_id, target_id),
    )
    for row in rows:
        if row["status"] in {"blocked", "conflict"}:
            continue
        label = f"final-mutation:{row['final_mutation_id']}"
        if row["status"] != "finalized":
            _refuse("UNKNOWN_INTEGRITY", label)
        transaction_id = row["transaction_public_id"]
        if type(transaction_id) is not str or not transaction_id:
            _refuse("UNKNOWN_INTEGRITY", label)
        chain = verify_financial_audit_chain(
            conn, aggregate_type="transaction", aggregate_public_id=transaction_id
        )
        if not chain.valid or chain.event_count == 0:
            _refuse("UNKNOWN_INTEGRITY", label)
        event_type = (
            "reconciliation_final_transaction_created"
            if row["action"] == "create_final_transaction_proposal"
            else "reconciliation_final_transaction_adjusted"
            if row["action"] == "adjust_final_transaction_proposal"
            else None
        )
        if event_type is None:
            _refuse("UNKNOWN_INTEGRITY", label)
        matching = _rows(
            conn,
            """SELECT event_payload_json FROM financial_audit_events
            WHERE aggregate_type = 'transaction' AND aggregate_public_id = ?
              AND causation_public_id = ? AND event_type = ?""",
            (transaction_id, row["final_mutation_id"], event_type),
        )
        if len(matching) != 1:
            _refuse("UNKNOWN_INTEGRITY", label)
        try:
            payload = canonical_json_value(
                str(matching[0]["event_payload_json"]), label="final mutation audit payload"
            )
        except (TypeError, ValueError):
            _refuse("UNKNOWN_INTEGRITY", label)
        if (
            type(payload) is not dict
            or payload.get("final_mutation_id") != row["final_mutation_id"]
        ):
            _refuse("UNKNOWN_INTEGRITY", label)
        _refuse("ACTIVE_RELATIONSHIP", label)


def assert_correction_eligible(conn: sqlite3.Connection, source: RelationshipSource) -> None:
    """Return only when every direct relation is clear in this read snapshot."""
    _base_relationships(conn, source.target_id)
    _settlement_relationships(conn, source.receipt_id if source.route == "receipt" else None)
    _resolution_relationships(conn, source.target_id)
    _apply_relationships(conn, source.target_id)
    _guarded_apply_relationships(conn, source.target_id)
    _final_mutation_relationships(conn, source.target_id)


__all__ = ["CorrectionRelationshipError", "assert_correction_eligible"]
