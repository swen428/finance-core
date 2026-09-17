"""Focused fail-closed tests for persisted reconciliation final authorization."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from finance_core.money import canonical_decimal_str, canonical_money_str
from finance_core.reconciliation.final_mutation_authorization import (
    AuthorizationMalformedError,
    AuthorizationMissingError,
    GuardDecisionMismatchError,
    GuardedExecutionMismatchError,
    HumanConfirmationContentMismatchError,
    HumanConfirmationMissingError,
    HumanConfirmationOperationMismatchError,
    HumanConfirmationStateDeniedError,
    HumanConfirmationSubjectMismatchError,
    build_final_mutation_content_hash,
    load_persisted_final_mutation_authorization,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuard,
    FinalMutationProposal,
)
from finance_core.resources import migrations_dir


def _proposal(**changes: object) -> FinalMutationProposal:
    values: dict[str, Any] = {
        "proposal_id": "proposal-1",
        "action": FinalMutationAction.CREATE_FINAL_TRANSACTION,
        "amount": Decimal("10.00"),
        "currency": "SGD",
        "merchant": "Merchant",
        "transaction_date": date(2026, 7, 11),
        "source_statement_ref": "statement-1",
        "evidence_refs": ("evidence-1",),
    }
    values.update(changes)
    return FinalMutationProposal(**values)


def _legacy_v1_content_hash(proposal: FinalMutationProposal) -> str:
    """Reproduce the pre-PR-209 content hash independently from production."""
    import hashlib

    if proposal.amount is None:
        canonical_amount: str | None = None
    elif proposal.currency:
        canonical_amount = canonical_money_str(proposal.amount, proposal.currency)
    else:
        canonical_amount = canonical_decimal_str(proposal.amount)
    payload: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "action": proposal.action.value,
        "amount": canonical_amount,
        "currency": proposal.currency,
        "merchant": proposal.merchant,
        "transaction_date": (
            proposal.transaction_date.isoformat() if proposal.transaction_date is not None else None
        ),
        "target_transaction_id": proposal.target_transaction_id,
        "suggested_fields": dict(sorted(proposal.suggested_fields.items())),
        "source_statement_ref": proposal.source_statement_ref,
        "source_app_transaction_ref": proposal.source_app_transaction_ref,
        "evidence_refs": sorted(proposal.evidence_refs),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for filename in (
        "013_reconciliation_final_mutation_guard_decisions.sql",
        "014_reconciliation_guarded_apply_execution_persistence.sql",
        "018_reconciliation_final_mutation_authorization.sql",
    ):
        conn.executescript((migrations_dir() / filename).read_text())
    return conn


def _seed(conn: sqlite3.Connection, proposal: FinalMutationProposal, **overrides: object) -> None:
    values: dict[str, Any] = {
        "authorization_id": "authorization-1",
        "subject_id": "operation-1",
        "plan_id": "plan-1",
        "confirmation_id": "confirmation-1",
        "authorization_state": "authorized",
        "confirmation_state": "confirmed",
        "content_hash": build_final_mutation_content_hash(proposal),
    }
    values.update(overrides)
    guard_key = "guard-key-1"
    execution_id = "execution-1"
    preview = FinalMutationGuard().evaluate(proposal).preview
    assert preview is not None
    preview_payload = {
        "proposal_id": preview.proposal_id,
        "action": preview.action.value,
        "amount": preview.amount,
        "currency": preview.currency,
        "merchant": preview.merchant,
        "transaction_date": preview.transaction_date,
        "target_transaction_id": preview.target_transaction_id,
        "suggested_fields": dict(sorted(preview.suggested_fields.items())),
        "source_statement_ref": preview.source_statement_ref,
        "source_app_transaction_ref": preview.source_app_transaction_ref,
        "evidence_refs": sorted(preview.evidence_refs),
        "is_dry_run": preview.is_dry_run,
    }
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_confirmations
        (confirmation_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, content_hash, confirmation_state, confirmed_by)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, ?, 'test')""",
        (
            values["confirmation_id"],
            values["subject_id"],
            proposal.action.value,
            proposal.proposal_id,
            values["plan_id"],
            values["content_hash"],
            values["confirmation_state"],
        ),
    )
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_guard_decisions
        (proposal_id, idempotency_key, action, approved, blocked_reasons_json,
         preview_json, evidence_refs_json, guard_version, actor_type)
        VALUES (?, ?, ?, 1, '[]', ?, ?, 'v1', 'test')""",
        (
            proposal.proposal_id,
            guard_key,
            proposal.action.value,
            json.dumps(preview_payload),
            json.dumps(sorted(proposal.evidence_refs)),
        ),
    )
    conn.execute(
        """INSERT INTO reconciliation_guarded_apply_executions
        (execution_id, plan_id, idempotency_key, execution_fingerprint,
         execution_status, total_operations, operations_executed,
         operations_blocked, operations_skipped, guard_decision_refs_json,
         audit_trail_json, is_dry_run, executed_at)
        VALUES (?, ?, 'execution-key-1', 'fingerprint', 'executed', 1, 1, 0, 0,
                '[]', '{}', 1, '2026-07-11T00:00:00Z')""",
        (execution_id, values["plan_id"]),
    )
    conn.execute(
        """INSERT INTO reconciliation_guarded_apply_operation_results
        (operation_result_id, execution_id, operation_id, decision_id,
         execution_status, guard_decision_approved,
         guard_decision_idempotency_key, guard_blocked_reasons_json,
         mutation_type, mutation_payload_json)
        VALUES ('result-1', ?, ?, 'decision-1', 'executed', 1, ?, '[]', ?, '{}')""",
        (execution_id, values["subject_id"], guard_key, proposal.action.value),
    )
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_authorizations
        (authorization_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, guard_decision_idempotency_key, guarded_execution_id,
         human_confirmation_id, content_hash, authorization_state)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["authorization_id"],
            values["subject_id"],
            proposal.action.value,
            proposal.proposal_id,
            values["plan_id"],
            guard_key,
            execution_id,
            values["confirmation_id"],
            values["content_hash"],
            values["authorization_state"],
        ),
    )
    conn.commit()


def _load(conn: sqlite3.Connection, proposal: FinalMutationProposal):
    return load_persisted_final_mutation_authorization(
        conn,
        authorization_id="authorization-1",
        plan_id="plan-1",
        operation_id="operation-1",
        proposal=proposal,
        human_confirmation_id="confirmation-1",
    )


def _rewrite_preview_as_legacy_v1(
    conn: sqlite3.Connection,
    proposal: FinalMutationProposal,
) -> dict[str, object]:
    row = conn.execute(
        "SELECT preview_json FROM reconciliation_final_mutation_guard_decisions"
    ).fetchone()
    preview = json.loads(row[0])
    preview["amount"] = str(proposal.amount) if proposal.amount is not None else None
    preview["currency"] = proposal.currency
    conn.execute(
        "UPDATE reconciliation_final_mutation_guard_decisions SET preview_json = ?",
        (json.dumps(preview),),
    )
    return preview


def test_loads_exact_persisted_authorization() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    assert _load(conn, proposal).authorization_id == "authorization-1"


@pytest.mark.parametrize("state", ["rejected", "revoked", "superseded", "expired", "cancelled"])
def test_denied_confirmation_states_fail_closed(state: str) -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal, confirmation_state=state)
    with pytest.raises(HumanConfirmationStateDeniedError):
        _load(conn, proposal)


def test_missing_authorization_fails_closed() -> None:
    with pytest.raises(AuthorizationMissingError):
        _load(_connection(), _proposal())


def test_content_mismatch_fails_closed() -> None:
    conn = _connection()
    _seed(conn, _proposal())
    with pytest.raises(HumanConfirmationContentMismatchError):
        _load(conn, _proposal(amount=Decimal("11.00")))


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "USD"},
        {"merchant": "Other merchant"},
        {"transaction_date": date(2026, 7, 12)},
        {"source_statement_ref": "other-statement"},
        {"source_app_transaction_ref": "other-app"},
        {"evidence_refs": ("other-evidence",)},
    ],
)
def test_create_content_fields_are_exactly_bound(changes: dict[str, object]) -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    with pytest.raises(HumanConfirmationContentMismatchError):
        _load(conn, _proposal(**changes))


def test_adjust_target_and_suggested_fields_are_exactly_bound() -> None:
    conn = _connection()
    proposal = _proposal(
        action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
        amount=None,
        currency=None,
        merchant=None,
        transaction_date=None,
        target_transaction_id="transaction-1",
        suggested_fields={"amount": "10.00"},
        source_statement_ref=None,
        source_app_transaction_ref="app-1",
    )
    _seed(conn, proposal)
    with pytest.raises(HumanConfirmationContentMismatchError):
        _load(conn, replace(proposal, target_transaction_id="transaction-2"))
    with pytest.raises(HumanConfirmationContentMismatchError):
        _load(conn, replace(proposal, suggested_fields={"amount": "11.00"}))


@pytest.mark.parametrize(
    "proposal",
    [
        _proposal(proposal_id="other-proposal"),
        _proposal(action=FinalMutationAction.ADJUST_FINAL_TRANSACTION),
    ],
)
def test_operation_mismatch_fails_closed(proposal: FinalMutationProposal) -> None:
    conn = _connection()
    _seed(conn, _proposal())
    with pytest.raises(HumanConfirmationOperationMismatchError):
        _load(conn, proposal)


def test_malformed_guard_json_fails_closed() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    conn.execute(
        "UPDATE reconciliation_final_mutation_guard_decisions SET preview_json = 'not-json'"
    )
    with pytest.raises(AuthorizationMalformedError):
        _load(conn, proposal)


def test_create_preview_money_mismatch_fails_closed() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    row = conn.execute(
        "SELECT preview_json FROM reconciliation_final_mutation_guard_decisions"
    ).fetchone()
    preview = json.loads(row[0])
    preview["amount"] = "10.01"
    conn.execute(
        "UPDATE reconciliation_final_mutation_guard_decisions SET preview_json = ?",
        (json.dumps(preview),),
    )

    with pytest.raises(GuardDecisionMismatchError):
        _load(conn, proposal)


@pytest.mark.parametrize(
    ("legacy_proposal", "current_proposal"),
    [
        (
            _proposal(amount=Decimal("12.3"), currency="SGD"),
            _proposal(amount=Decimal("12.30"), currency="SGD"),
        ),
        (
            _proposal(amount=Decimal("100.0"), currency="JPY"),
            _proposal(amount=Decimal("100"), currency="JPY"),
        ),
        (
            _proposal(amount=Decimal("12.3"), currency=" sgd "),
            _proposal(amount=Decimal("12.30"), currency="SGD"),
        ),
    ],
)
def test_valid_legacy_v1_hash_and_preview_are_narrowly_accepted(
    legacy_proposal: FinalMutationProposal,
    current_proposal: FinalMutationProposal,
) -> None:
    conn = _connection()
    legacy_hash = _legacy_v1_content_hash(legacy_proposal)
    _seed(conn, legacy_proposal, content_hash=legacy_hash)
    _rewrite_preview_as_legacy_v1(conn, legacy_proposal)

    assert _load(conn, current_proposal).authorization_id == "authorization-1"


def test_incomplete_legacy_v1_preview_fails_closed() -> None:
    conn = _connection()
    proposal = _proposal(amount=Decimal("12.3"))
    _seed(conn, proposal, content_hash=_legacy_v1_content_hash(proposal))
    preview = _rewrite_preview_as_legacy_v1(conn, proposal)
    del preview["merchant"]
    conn.execute(
        "UPDATE reconciliation_final_mutation_guard_decisions SET preview_json = ?",
        (json.dumps(preview),),
    )

    with pytest.raises(GuardDecisionMismatchError):
        _load(conn, _proposal(amount=Decimal("12.30")))


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (10.0, "SGD"),
        ("NaN", "SGD"),
        ("12.30", "XYZ"),
    ],
)
def test_malformed_legacy_v1_preview_money_fails_closed(
    amount: object,
    currency: object,
) -> None:
    conn = _connection()
    proposal = _proposal(amount=Decimal("12.3"))
    _seed(conn, proposal, content_hash=_legacy_v1_content_hash(proposal))
    preview = _rewrite_preview_as_legacy_v1(conn, proposal)
    preview["amount"] = amount
    preview["currency"] = currency
    conn.execute(
        "UPDATE reconciliation_final_mutation_guard_decisions SET preview_json = ?",
        (json.dumps(preview),),
    )

    with pytest.raises(GuardDecisionMismatchError):
        _load(conn, _proposal(amount=Decimal("12.30")))


def test_execution_guard_link_mismatch_fails_closed() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    conn.execute(
        "UPDATE reconciliation_guarded_apply_operation_results "
        "SET guard_decision_idempotency_key = 'other-key'"
    )
    with pytest.raises(GuardedExecutionMismatchError):
        _load(conn, proposal)


def test_missing_persisted_confirmation_fails_closed_when_corrupt_fixture_bypasses_fk() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM reconciliation_final_mutation_confirmations")
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(HumanConfirmationMissingError):
        _load(conn, proposal)


def test_confirmation_foreign_key_rejects_nonexistent_confirmation() -> None:
    conn = _connection()
    proposal = _proposal()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO reconciliation_final_mutation_authorizations
            (authorization_id, subject_type, subject_id, operation_type, proposal_id,
             plan_id, guard_decision_idempotency_key, guarded_execution_id,
             human_confirmation_id, content_hash, authorization_state)
            VALUES ('bad-auth', 'reconciliation_apply_operation', 'operation-1',
                    'create_final_transaction_proposal', 'proposal-1', 'plan-1',
                    'missing-guard', 'missing-execution', 'missing-confirmation',
                    ?, 'authorized')""",
            (build_final_mutation_content_hash(proposal),),
        )


def test_confirmation_subject_and_operation_mismatches_fail_closed() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    conn.execute(
        "UPDATE reconciliation_final_mutation_confirmations SET subject_id = 'other-operation'"
    )
    with pytest.raises(HumanConfirmationSubjectMismatchError):
        _load(conn, proposal)

    conn.execute(
        "UPDATE reconciliation_final_mutation_confirmations "
        "SET subject_id = 'operation-1', operation_type = 'adjust_final_transaction_proposal'"
    )
    with pytest.raises(HumanConfirmationOperationMismatchError):
        _load(conn, proposal)


def test_confirmation_content_and_guard_mismatches_fail_closed() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    conn.execute(
        "UPDATE reconciliation_final_mutation_confirmations SET content_hash = ?",
        ("0" * 64,),
    )
    with pytest.raises(HumanConfirmationContentMismatchError):
        _load(conn, proposal)

    conn.execute(
        "UPDATE reconciliation_final_mutation_confirmations SET content_hash = ?",
        (build_final_mutation_content_hash(proposal),),
    )
    conn.execute("UPDATE reconciliation_final_mutation_guard_decisions SET approved = 0")
    with pytest.raises(GuardDecisionMismatchError):
        _load(conn, proposal)


def test_canonical_money_hash_equivalence_and_difference() -> None:
    assert (
        build_final_mutation_content_hash(_proposal())
        == "b23f50abf779e65e6ea363ebe1750503881ef3b766ebd9a057fa9f2fddacdf4f"
    )
    assert build_final_mutation_content_hash(_proposal(amount=Decimal("10.0"))) == (
        build_final_mutation_content_hash(_proposal(amount=Decimal("10.00")))
    )
    assert build_final_mutation_content_hash(_proposal(amount=Decimal("10"), currency="JPY")) == (
        build_final_mutation_content_hash(_proposal(amount=Decimal("10.0"), currency="JPY"))
    )
    assert build_final_mutation_content_hash(_proposal(amount=Decimal("10.00"))) != (
        build_final_mutation_content_hash(_proposal(amount=Decimal("10.01")))
    )


def test_active_confirmation_and_authorization_uniqueness_and_integrity() -> None:
    conn = _connection()
    proposal = _proposal()
    _seed(conn, proposal)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO reconciliation_final_mutation_confirmations
            (confirmation_id, subject_type, subject_id, operation_type, proposal_id,
             plan_id, content_hash, confirmation_state, confirmed_by)
            VALUES ('confirmation-2', 'reconciliation_apply_operation', 'operation-1',
                    'create_final_transaction_proposal', 'proposal-1', 'plan-1', ?,
                    'confirmed', 'test')""",
            (build_final_mutation_content_hash(proposal),),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO reconciliation_final_mutation_authorizations
            (authorization_id, subject_type, subject_id, operation_type, proposal_id,
             plan_id, guard_decision_idempotency_key, guarded_execution_id,
             human_confirmation_id, content_hash, authorization_state)
            VALUES ('authorization-2', 'reconciliation_apply_operation', 'operation-1',
                    'create_final_transaction_proposal', 'proposal-1', 'plan-1',
                    'guard-key-1', 'execution-1', 'confirmation-1', ?, 'authorized')""",
            (build_final_mutation_content_hash(proposal),),
        )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
