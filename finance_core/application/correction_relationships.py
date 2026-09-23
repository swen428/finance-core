"""Fail-closed relationship eligibility for an already finalized D2 original.

Only committed effects matter. Suggestions, failed attempts and dry runs do
not create a relationship, while malformed related success claims are never
silently treated as absent.
"""

from __future__ import annotations

import json
import sqlite3
from typing import NoReturn, Protocol, cast

from finance_core.application.correction_schema import (
    CorrectionSchemaError,
    verify_correction_schema,
)
from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.reconciliation.models import ResolutionAction, validate_resolution_decision
from finance_core.reconciliation.source_binding import load_bound_queue


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


def _resolution_decision_evidence_matches(
    conn: sqlite3.Connection, decision_id: str, evidence: dict[str, object], issue_type: str
) -> bool:
    try:
        decision = conn.execute(
            "SELECT reviewer, decision_note, resolved_at "
            "FROM reconciliation_resolution_decisions WHERE public_id = ?",
            (decision_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return decision is not None and (
        evidence.get("issue_type") == issue_type
        and evidence.get("reviewer") == decision[0]
        and (evidence.get("note") or None) == decision[1]
        and (decision[2] is None or evidence.get("resolved_at") == decision[2])
    )


def _resolution_relationships(conn: sqlite3.Connection, target_id: str) -> None:
    results = _rows(
        conn,
        """SELECT results.public_id AS result_id, results.audit_evidence_json,
        results.decision_public_id, results.review_queue_public_id,
        decisions.decision_action, decisions.review_queue_public_id AS decision_queue_id,
        queues.public_id AS queue_id, queues.app_transaction_ref
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
        label = f"resolution:{row['result_id']}"
        evidence = _object(row["audit_evidence_json"], label)
        queue_ref = row["app_transaction_ref"]
        evidence_ref = evidence.get("app_txn_id")
        canonical_ref = evidence.get("canonical_app_transaction_ref")
        payload_target = evidence.get("payload_target_app_txn_id")
        evidence_action = evidence.get("resolution_action")
        duplicate_fields = ("duplicate_app_txn_ids", "kept_app_txn_id", "audit_only")
        classification_fields = (
            "canonical_app_transaction_ref",
            "payload_target_app_txn_id",
            *duplicate_fields,
        )
        if (
            type(row["decision_public_id"]) is not str
            or not row["decision_public_id"]
            or type(row["review_queue_public_id"]) is not str
            or not row["review_queue_public_id"]
            or row["decision_queue_id"] != row["review_queue_public_id"]
            or row["queue_id"] != row["review_queue_public_id"]
            or ("queue_item_id" in evidence and evidence["queue_item_id"] != row["queue_id"])
            or (queue_ref is not None and (type(queue_ref) is not str or not queue_ref))
            or (evidence_ref is not None and (type(evidence_ref) is not str or not evidence_ref))
            or queue_ref != evidence_ref
        ):
            _refuse("UNKNOWN_INTEGRITY", label)

        if action == "mark_duplicate":
            duplicates = evidence.get("duplicate_app_txn_ids")
            kept = evidence.get("kept_app_txn_id")
            if not (
                type(duplicates) is list
                and len(duplicates) >= 2
                and all(type(value) is str and value for value in duplicates)
                and len(set(duplicates)) == len(duplicates)
                and type(kept) is str
                and kept in duplicates
                and evidence.get("audit_only") is True
                and evidence_action == action
                and canonical_ref == kept
                and payload_target == kept
                and queue_ref == kept
            ):
                # Old success rows may omit secondary duplicate IDs. Their
                # relationship to any target cannot be disproved safely.
                _refuse("UNKNOWN_INTEGRITY", label)
            try:
                bound = load_bound_queue(conn, row["review_queue_public_id"])
            except (ValueError, CorrectionSchemaError):
                _refuse("UNKNOWN_INTEGRITY", label)
            if bound is not None and (
                not validate_resolution_decision(ResolutionAction.MARK_DUPLICATE, bound.issue_type)[
                    0
                ]
                or duplicates != [app.app_txn_id for app in bound.app_transactions]
                or kept != bound.app_transaction_ref
                or evidence.get("queue_item_id") != bound.queue_item_id
                or evidence.get("candidate_id") != bound.candidate_id
                or not _resolution_decision_evidence_matches(
                    conn, row["decision_public_id"], evidence, bound.issue_type.value
                )
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            if target_id in cast(list[str], duplicates):
                _refuse("ACTIVE_RELATIONSHIP", label)
            continue

        if action == "confirm_match":
            if (
                evidence_action != action
                or any(key in evidence for key in duplicate_fields)
                or type(queue_ref) is not str
                or canonical_ref != queue_ref
                or payload_target != queue_ref
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            try:
                bound = load_bound_queue(conn, row["review_queue_public_id"])
            except (ValueError, CorrectionSchemaError):
                _refuse("UNKNOWN_INTEGRITY", label)
            if bound is not None and (
                not validate_resolution_decision(ResolutionAction.CONFIRM_MATCH, bound.issue_type)[
                    0
                ]
                or queue_ref != bound.app_transaction_ref
                or evidence.get("queue_item_id") != bound.queue_item_id
                or evidence.get("candidate_id") != bound.candidate_id
                or not _resolution_decision_evidence_matches(
                    conn, row["decision_public_id"], evidence, bound.issue_type.value
                )
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            if target_id == queue_ref:
                _refuse("ACTIVE_RELATIONSHIP", label)
            continue

        if (
            action
            not in {
                "adjust_app_transaction",
                "create_missing_app_transaction",
                "mark_statement_only",
                "ignore",
                "needs_more_info",
            }
            or evidence_action != action
            or any(key in evidence for key in classification_fields)
        ):
            _refuse("UNKNOWN_INTEGRITY", label)
        # These successful decisions did not classify an app transaction.
        # Queue status can change after a later, independent decision.


def _apply_evidence_matches(
    row: dict[str, object], evidence: dict[str, object], issue_type: str
) -> bool:
    return (
        type(row.get("decision_id")) is str
        and evidence.get("decision_id") == row["decision_id"]
        and evidence.get("reviewer") == row.get("reviewer")
        and (evidence.get("note") or None) == row.get("note")
        and evidence.get("issue_type") == issue_type
    )


def _apply_relationships(conn: sqlite3.Connection, target_id: str) -> None:
    try:
        bound_required = verify_correction_schema(conn)
    except CorrectionSchemaError:
        _refuse("UNKNOWN_INTEGRITY", "reconciliation-apply:schema")
    results = _rows(
        conn,
        """SELECT * FROM reconciliation_apply_results
        WHERE success = 1 ORDER BY apply_id""",
        (),
    )
    for row in results:
        label = f"reconciliation-apply:{row['apply_id']}"
        payload = _object(row["payload_json"], label)
        evidence = _object(row["audit_evidence_json"], label)
        app_ref = _optional_ref(row["app_transaction_reference_json"], label)
        statement_ref = _optional_ref(row.get("statement_reference_json"), label)
        action = row["action"]
        payload_action = payload.get("action_type")
        evidence_action = evidence.get("resolution_action")
        evidence_ref = evidence.get("app_txn_id")
        duplicate_claim = (
            action == "mark_duplicate"
            or payload_action == "mark_duplicate"
            or evidence_action == "mark_duplicate"
            or any(
                key in payload for key in ("duplicate_app_txn_ids", "kept_app_txn_id", "audit_only")
            )
        )
        if duplicate_claim:
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
            bound = None
            if bound_required:
                queue_id = row.get("queue_item_id")
                if type(queue_id) is not str or not queue_id:
                    _refuse("UNKNOWN_INTEGRITY", label)
                try:
                    bound = load_bound_queue(conn, queue_id)
                except (ValueError, CorrectionSchemaError):
                    _refuse("UNKNOWN_INTEGRITY", label)
            if bound is not None and (
                action != ResolutionAction.MARK_DUPLICATE.value
                or not validate_resolution_decision(
                    ResolutionAction.MARK_DUPLICATE, bound.issue_type
                )[0]
                or row["candidate_id"] != bound.candidate_id
                or statement_ref != bound.statement_transaction_ref
                or app_ref != bound.app_transaction_ref
                or duplicates != [app.app_txn_id for app in bound.app_transactions]
                or kept != bound.app_transaction_ref
                or evidence.get("queue_item_id") != bound.queue_item_id
                or evidence.get("candidate_id") != bound.candidate_id
                or evidence_action != action
                or not _apply_evidence_matches(row, evidence, bound.issue_type.value)
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            ids = set(cast(list[str], duplicates))
            ids.update(
                value
                for value in (kept, app_ref, payload.get("app_txn_id"), evidence_ref)
                if type(value) is str and value
            )
            if target_id not in ids:
                continue
            if (
                action != "mark_duplicate"
                or payload_action != "mark_duplicate"
                or (evidence_action is not None and evidence_action != action)
                or (app_ref is not None and app_ref not in cast(list[str], duplicates))
                or "app_txn_id" in payload
                or (evidence_ref is not None and evidence_ref not in cast(list[str], duplicates))
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            _refuse("ACTIVE_RELATIONSHIP", label)
        if (
            action == "confirm_match"
            or payload_action == "confirm_match"
            or evidence_action == "confirm_match"
        ):
            canonical_id = payload.get("app_txn_id")
            if type(canonical_id) is not str or not canonical_id:
                _refuse("UNKNOWN_INTEGRITY", label)
            bound = None
            if bound_required:
                queue_id = row.get("queue_item_id")
                if type(queue_id) is not str or not queue_id:
                    _refuse("UNKNOWN_INTEGRITY", label)
                try:
                    bound = load_bound_queue(conn, queue_id)
                except (ValueError, CorrectionSchemaError):
                    _refuse("UNKNOWN_INTEGRITY", label)
            if bound is not None and (
                action != ResolutionAction.CONFIRM_MATCH.value
                or not validate_resolution_decision(
                    ResolutionAction.CONFIRM_MATCH, bound.issue_type
                )[0]
                or row["candidate_id"] != bound.candidate_id
                or statement_ref != bound.statement_transaction_ref
                or app_ref != bound.app_transaction_ref
                or canonical_id != bound.app_transaction_ref
                or evidence.get("queue_item_id") != bound.queue_item_id
                or evidence.get("candidate_id") != bound.candidate_id
                or evidence_action != action
                or not _apply_evidence_matches(row, evidence, bound.issue_type.value)
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            if target_id not in (canonical_id, app_ref, evidence_ref):
                continue
            if (
                app_ref != canonical_id
                or payload_action != action
                or (evidence_action is not None and evidence_action != action)
                or (evidence_ref is not None and evidence_ref != canonical_id)
            ):
                _refuse("UNKNOWN_INTEGRITY", label)
            _refuse("ACTIVE_RELATIONSHIP", label)
        payload_ref = payload.get("app_txn_id")
        if target_id in (app_ref, evidence_ref, payload_ref):
            if type(action) is not str:
                _refuse("UNKNOWN_INTEGRITY", label)
            expected_payload_action = {
                "adjust_app_transaction": "proposal",
                "create_missing_app_transaction": "proposal",
                "mark_statement_only": "mark_statement_only",
                "ignore": "ignore",
                "needs_more_info": "needs_more_info",
            }.get(action)
            if (
                expected_payload_action is None
                or payload_action != expected_payload_action
                or evidence_action != action
                or app_ref != evidence_ref
                or (action == "adjust_app_transaction" and payload_ref != app_ref)
                or (action != "adjust_app_transaction" and payload_ref is not None)
                or (
                    action in {"adjust_app_transaction", "create_missing_app_transaction"}
                    and payload.get("proposal_type") != action
                )
                or (action == "ignore" and payload.get("skipped") is not True)
                or (
                    action == "needs_more_info" and payload.get("status") != "pending_investigation"
                )
            ):
                _refuse("UNKNOWN_INTEGRITY", label)


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
            or execution["execution_status"]
            not in {"executed", "blocked", "partially_blocked", "unsupported", "conflict"}
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
