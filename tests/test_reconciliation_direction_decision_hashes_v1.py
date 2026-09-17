"""High-risk reconciliation direction compatibility and decision-hash contract."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.reconciliation.decision import verify_persisted_decision_hash
from finance_core.reconciliation.matcher import match_statement
from finance_core.reconciliation.models import (
    InternalCandidate,
    MatchResult,
    MatchStatus,
    ReasonCode,
    ReconciliationTransactionType,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.repository import (
    DuplicatePublicIdError,
    ReconciliationRepository,
    ReconciliationRepositoryError,
)
from finance_core.reconciliation.service import ReconciliationService


def _statement(**changes: object) -> StatementTransaction:
    values: dict[str, object] = {
        "transaction_date": date(2026, 7, 1),
        "posted_date": date(2026, 7, 2),
        "merchant_raw": "Apple Store",
        "amount": Decimal("12.30"),
        "currency": "SGD",
        "public_id": "statement-row-1",
        "row_fingerprint": "row-fingerprint-1",
        "source_content_hash": "a" * 64,
        "statement_row_reference": "page=1,row=1",
        "amount_direction": StatementAmountDirection.DEBIT,
        "raw_amount": "12.30",
        "raw_amount_type": "debit",
    }
    values.update(changes)
    return StatementTransaction(**values)  # type: ignore[arg-type]


def _candidate(**changes: object) -> InternalCandidate:
    values: dict[str, object] = {
        "internal_id": "transaction-1",
        "transaction_date": date(2026, 7, 1),
        "posted_date": date(2026, 7, 2),
        "merchant": "Apple Store",
        "amount": Decimal("12.30"),
        "currency": "SGD",
        "transaction_type": ReconciliationTransactionType.EXPENSE,
        "source_type": "expense",
        "evidence_reference": "transactions:1",
    }
    values.update(changes)
    return InternalCandidate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("direction", "transaction_type"),
    [
        (StatementAmountDirection.DEBIT, ReconciliationTransactionType.EXPENSE),
        (StatementAmountDirection.CREDIT, ReconciliationTransactionType.INCOME),
        (StatementAmountDirection.REFUND, ReconciliationTransactionType.REFUND),
        (StatementAmountDirection.REVERSAL, ReconciliationTransactionType.REVERSAL),
        (StatementAmountDirection.CHARGEBACK, ReconciliationTransactionType.CHARGEBACK),
        (StatementAmountDirection.PAYMENT, ReconciliationTransactionType.CARD_PAYMENT),
        (StatementAmountDirection.CARD_PAYMENT, ReconciliationTransactionType.CARD_PAYMENT),
        (StatementAmountDirection.TRANSFER_IN, ReconciliationTransactionType.TRANSFER_IN),
        (StatementAmountDirection.TRANSFER_OUT, ReconciliationTransactionType.TRANSFER_OUT),
        (StatementAmountDirection.FEE, ReconciliationTransactionType.FEE),
        (
            StatementAmountDirection.INTEREST_DEBIT,
            ReconciliationTransactionType.INTEREST_DEBIT,
        ),
        (
            StatementAmountDirection.INTEREST_CREDIT,
            ReconciliationTransactionType.INTEREST_CREDIT,
        ),
        (
            StatementAmountDirection.CASH_WITHDRAWAL,
            ReconciliationTransactionType.CASH_WITHDRAWAL,
        ),
        (
            StatementAmountDirection.CASH_DEPOSIT,
            ReconciliationTransactionType.CASH_DEPOSIT,
        ),
    ],
)
def test_supported_direction_type_matrix_matches(
    direction: StatementAmountDirection,
    transaction_type: ReconciliationTransactionType,
) -> None:
    raw_type = (
        "debit"
        if direction
        in {
            StatementAmountDirection.DEBIT,
            StatementAmountDirection.PAYMENT,
            StatementAmountDirection.CARD_PAYMENT,
            StatementAmountDirection.TRANSFER_OUT,
            StatementAmountDirection.FEE,
            StatementAmountDirection.INTEREST_DEBIT,
            StatementAmountDirection.CASH_WITHDRAWAL,
        }
        else "credit"
    )
    result = match_statement(
        _statement(amount_direction=direction, raw_amount_type=raw_type),
        [_candidate(transaction_type=transaction_type)],
    )
    assert result.status is MatchStatus.MATCHED
    assert ReasonCode.DIRECTION_TYPE_COMPATIBLE in result.reasons


@pytest.mark.parametrize(
    ("direction", "transaction_type"),
    [
        (StatementAmountDirection.CREDIT, ReconciliationTransactionType.EXPENSE),
        (StatementAmountDirection.DEBIT, ReconciliationTransactionType.REFUND),
        (StatementAmountDirection.REFUND, ReconciliationTransactionType.EXPENSE),
        (StatementAmountDirection.REVERSAL, ReconciliationTransactionType.EXPENSE),
        (StatementAmountDirection.PAYMENT, ReconciliationTransactionType.EXPENSE),
        (StatementAmountDirection.TRANSFER_IN, ReconciliationTransactionType.EXPENSE),
        (StatementAmountDirection.TRANSFER_OUT, ReconciliationTransactionType.EXPENSE),
    ],
)
def test_incompatible_direction_is_a_hard_gate(
    direction: StatementAmountDirection,
    transaction_type: ReconciliationTransactionType,
) -> None:
    result = match_statement(
        _statement(amount_direction=direction, raw_amount_type=None),
        [_candidate(transaction_type=transaction_type)],
    )
    assert result.status is MatchStatus.NEEDS_REVIEW
    assert result.reasons == (ReasonCode.DIRECTION_TYPE_INCOMPATIBLE,)
    assert "exact_amount_match" not in result.evidence.hard_gate_results
    assert not any(gate.startswith("date_compatible") for gate in result.evidence.hard_gate_results)
    assert "merchant_compatible" not in result.evidence.hard_gate_results


def test_high_similarity_cannot_override_direction_incompatibility() -> None:
    result = match_statement(
        _statement(amount_direction=StatementAmountDirection.REFUND, raw_amount_type="credit"),
        [_candidate(transaction_type=ReconciliationTransactionType.EXPENSE)],
        merchant_similarity_threshold=0,
    )
    assert result.status is MatchStatus.NEEDS_REVIEW
    assert result.evidence.merchant_similarity is None


def test_missing_unknown_invalid_zero_and_sign_contradiction_fail_closed() -> None:
    candidate = _candidate()
    missing = match_statement(_statement(amount_direction=None, raw_amount_type=None), [candidate])
    unknown = match_statement(
        _statement(amount_direction=StatementAmountDirection.UNKNOWN, raw_amount_type=None),
        [candidate],
    )
    zero = match_statement(
        _statement(amount=Decimal("0"), raw_amount="0", raw_amount_type="debit"),
        [candidate],
    )
    contradiction = match_statement(
        _statement(raw_amount_type="credit"),
        [candidate],
    )
    assert missing.reasons == (ReasonCode.MISSING_DIRECTION,)
    assert unknown.reasons == (ReasonCode.UNKNOWN_DIRECTION,)
    assert zero.reasons == (ReasonCode.ZERO_AMOUNT_REQUIRES_REVIEW,)
    assert contradiction.reasons == (ReasonCode.AMOUNT_SIGN_CONTRADICTION,)
    with pytest.raises(ValueError, match="Invalid statement amount direction"):
        _statement(amount_direction="sideways")


def test_identical_material_produces_identical_verified_hash() -> None:
    first = match_statement(_statement(), [_candidate()])
    second = match_statement(_statement(), [_candidate()])
    assert first.decision_hash == second.decision_hash
    assert first.candidate_set_fingerprint == second.candidate_set_fingerprint
    assert verify_persisted_decision_hash(
        first.decision_hash or "",
        first.decision_material_json or "",
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"amount_direction": StatementAmountDirection.REFUND, "raw_amount_type": "credit"},
        {"amount": Decimal("12.31")},
        {"currency": "USD"},
        {"raw_amount": "-12.30", "raw_amount_type": None},
        {"transaction_date": date(2026, 7, 3)},
        {"posted_date": date(2026, 7, 4)},
        {"merchant_normalized": "apple-store-v2"},
    ],
)
def test_statement_material_change_changes_decision_hash(changed: dict[str, object]) -> None:
    baseline = match_statement(_statement(), [_candidate()])
    modified = match_statement(_statement(**changed), [_candidate()])
    assert modified.decision_hash != baseline.decision_hash


def test_candidate_set_score_threshold_and_rule_versions_change_hash() -> None:
    statement = _statement(merchant_raw="Apple Store Online", merchant_normalized=None)
    candidate = _candidate(merchant="Apple Store")
    baseline = match_statement(statement, [candidate], merchant_similarity_threshold=0.5)
    score_changed = match_statement(
        statement,
        [_candidate(merchant="Apple Online")],
        merchant_similarity_threshold=0.5,
    )
    candidate_set_changed = match_statement(
        statement,
        [candidate, _candidate(internal_id="transaction-2", amount=Decimal("99.00"))],
        merchant_similarity_threshold=0.5,
    )
    threshold_changed = match_statement(
        statement,
        [candidate],
        merchant_similarity_threshold=0.4,
    )
    matcher_changed = match_statement(statement, [candidate], matcher_version="matcher-v-next")
    compatibility_changed = match_statement(
        statement,
        [candidate],
        compatibility_version="compatibility-v-next",
    )
    normalization_changed = match_statement(
        statement,
        [candidate],
        merchant_normalization_version="merchant-normalization-v-next",
    )
    approved = match_statement(
        statement,
        [candidate],
        authorization_public_id="authorization-1",
    )
    assert score_changed.decision_hash != baseline.decision_hash
    assert candidate_set_changed.candidate_set_fingerprint != baseline.candidate_set_fingerprint
    assert candidate_set_changed.decision_hash != baseline.decision_hash
    assert threshold_changed.decision_hash != baseline.decision_hash
    assert matcher_changed.decision_hash != baseline.decision_hash
    assert compatibility_changed.decision_hash != baseline.decision_hash
    assert normalization_changed.decision_hash != baseline.decision_hash
    assert approved.decision_hash != baseline.decision_hash


def test_tampered_material_or_hash_fails_verification() -> None:
    result = match_statement(_statement(), [_candidate()])
    assert result.decision_hash and result.decision_material_json
    assert not verify_persisted_decision_hash("0" * 64, result.decision_material_json)
    tampered = result.decision_material_json.replace("matched", "no_match", 1)
    assert not verify_persisted_decision_hash(result.decision_hash, tampered)


def _seed_service_batch(conn: sqlite3.Connection) -> int:
    conn.execute(
        """INSERT INTO statement_import_batches
        (public_id, source_type, currency, source_file_hash)
        VALUES ('batch-decision', 'bank_statement', 'SGD', ?)""",
        ("a" * 64,),
    )
    batch_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """INSERT INTO statement_transactions
        (public_id, batch_id, transaction_date, posted_date, merchant_raw,
         amount, currency, statement_row_reference, row_fingerprint,
         amount_direction, raw_amount, raw_amount_type)
        VALUES ('statement-row-1', ?, '2026-07-01', '2026-07-02', 'Apple Store',
                '12.30', 'SGD', 'page=1,row=1', 'row-fingerprint-1',
                'debit', '12.30', 'debit')""",
        (batch_id,),
    )
    conn.commit()
    return batch_id


def test_service_persists_verified_hash_and_atomic_audit(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    summary = ReconciliationService(
        ReconciliationRepository(migrated_temp_db_connection)
    ).run_reconciliation_for_batch(
        batch_id,
        [_candidate()],
        "run-decision",
    )
    assert summary.matched_count == 1
    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_match_results"
    ).fetchone()
    assert verify_persisted_decision_hash(row["decision_hash"], row["decision_material_json"])
    event = migrated_temp_db_connection.execute(
        """SELECT * FROM financial_audit_events
        WHERE aggregate_type = 'reconciliation_match_result'"""
    ).fetchone()
    assert event["aggregate_public_id"] == row["public_id"]
    assert row["decision_hash"] in event["event_payload_json"]


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("public_id", "tampered-decision-public-id"),
        ("run_id", 999_999),
        ("statement_transaction_id", 999_999),
        ("internal_candidate_id", "transaction-tampered"),
        ("match_status", "no_match"),
        ("reason_codes_json", '["no_candidates"]'),
        ("evidence_json", "{}"),
        ("amount_delta", "99.99"),
        ("date_delta_days", 99),
        ("merchant_similarity", 0.01),
        ("needs_review", 1),
        ("decision_contract_version", "reconciliation-decision-tampered"),
        ("matcher_version", "reconciliation-matcher-tampered"),
        ("compatibility_version", "reconciliation-compatibility-tampered"),
        ("merchant_normalization_version", "merchant-normalization-tampered"),
        ("candidate_set_fingerprint", "b" * 64),
        ("decision_hash", "c" * 64),
        ("decision_material_json", '{"tampered":true}'),
        ("authorization_public_id", "authorization-tampered"),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("updated_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_authoritative_reconciliation_decision_is_append_only(
    migrated_temp_db_connection: sqlite3.Connection,
    column: str,
    replacement: object,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    ReconciliationService(
        ReconciliationRepository(migrated_temp_db_connection)
    ).run_reconciliation_for_batch(
        batch_id,
        [_candidate()],
        f"run-immutable-{column}",
    )
    row_id = migrated_temp_db_connection.execute(
        "SELECT id FROM reconciliation_match_results"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="authoritative reconciliation decision"):
        migrated_temp_db_connection.execute(
            f"UPDATE reconciliation_match_results SET {column} = ? WHERE id = ?",
            (replacement, row_id),
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("match_status", "no_match"),
        ("amount_delta", "99.99"),
        ("date_delta_days", 99),
        ("merchant_similarity", 0.01),
        ("evidence_json", "{}"),
    ],
)
def test_authoritative_reload_reconstructs_from_relational_columns_and_audit(
    migrated_temp_db_connection: sqlite3.Connection,
    column: str,
    replacement: object,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    summary = ReconciliationService(
        ReconciliationRepository(migrated_temp_db_connection)
    ).run_reconciliation_for_batch(batch_id, [_candidate()], "run-reload-verification")
    decision = migrated_temp_db_connection.execute(
        "SELECT public_id FROM reconciliation_match_results WHERE run_id = ?",
        (summary.run_id,),
    ).fetchone()
    assert decision is not None
    assert verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="reconciliation_match_result",
        aggregate_public_id=decision["public_id"],
    ).valid

    migrated_temp_db_connection.execute(
        "DROP TRIGGER trg_authoritative_reconciliation_decision_no_update"
    )
    migrated_temp_db_connection.execute(
        f"UPDATE reconciliation_match_results SET {column} = ? WHERE run_id = ?",
        (replacement, summary.run_id),
    )

    with pytest.raises(ReconciliationRepositoryError, match="decision integrity"):
        ReconciliationRepository(migrated_temp_db_connection).get_match_results_for_run(
            summary.run_id
        )


def test_authoritative_reconciliation_decision_cannot_be_deleted(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    ReconciliationService(
        ReconciliationRepository(migrated_temp_db_connection)
    ).run_reconciliation_for_batch(
        batch_id,
        [_candidate()],
        "run-no-delete",
    )
    with pytest.raises(sqlite3.IntegrityError, match="authoritative reconciliation decision"):
        migrated_temp_db_connection.execute("DELETE FROM reconciliation_match_results")


def test_legacy_reconciliation_result_cannot_be_promoted_in_place(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    repo = ReconciliationRepository(migrated_temp_db_connection)
    run_id = repo.create_reconciliation_run("run-legacy-no-promotion", batch_id=batch_id)
    statement_id = int(
        migrated_temp_db_connection.execute(
            "SELECT id FROM statement_transactions WHERE public_id = 'statement-row-1'"
        ).fetchone()[0]
    )
    migrated_temp_db_connection.execute(
        """INSERT INTO reconciliation_match_results
        (public_id, run_id, statement_transaction_id, match_status,
         reason_codes_json, evidence_json, needs_review)
        VALUES ('legacy-decision', ?, ?, 'no_match', '[]', '{}', 1)""",
        (run_id, statement_id),
    )

    with pytest.raises(sqlite3.IntegrityError, match="authoritative reconciliation decision"):
        migrated_temp_db_connection.execute(
            "UPDATE reconciliation_match_results SET decision_hash = ? WHERE public_id = ?",
            ("a" * 64, "legacy-decision"),
        )


def test_repository_identical_replay_and_conflicting_replay(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    repo = ReconciliationRepository(migrated_temp_db_connection)
    run_id = repo.create_reconciliation_run("run-replay", batch_id=batch_id)
    statement_id = int(
        migrated_temp_db_connection.execute(
            "SELECT id FROM statement_transactions WHERE public_id = 'statement-row-1'"
        ).fetchone()[0]
    )
    statement = _statement(source_batch_id=str(batch_id))
    result = match_statement(statement, [_candidate()])
    first = repo.save_match_result(run_id, statement_id, result, public_id="decision-replay")
    second = repo.save_match_result(run_id, statement_id, result, public_id="decision-replay")
    assert first == second
    conflict = match_statement(statement, [_candidate(internal_id="transaction-2")])
    with pytest.raises(DuplicatePublicIdError, match="Conflicting reconciliation decision"):
        repo.save_match_result(run_id, statement_id, conflict, public_id="decision-replay")


def _seed_concurrent_decision_context(db_path: Path) -> tuple[int, int, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        batch_id = _seed_service_batch(conn)
        repo = ReconciliationRepository(conn)
        run_id = repo.create_reconciliation_run("run-concurrent", batch_id=batch_id)
        statement_id = int(
            conn.execute(
                "SELECT id FROM statement_transactions WHERE public_id = 'statement-row-1'"
            ).fetchone()[0]
        )
        conn.commit()
        return run_id, statement_id, batch_id
    finally:
        conn.close()


def _concurrent_save(
    db_path: Path,
    barrier: threading.Barrier,
    run_id: int,
    statement_id: int,
    result: MatchResult,
    public_id: str,
) -> int:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        barrier.wait(timeout=5)
        conn.execute("BEGIN IMMEDIATE")
        result_id = ReconciliationRepository(conn).save_match_result(
            run_id,
            statement_id,
            result,
            public_id=public_id,
        )
        conn.commit()
        return result_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def test_concurrent_identical_persistence_returns_one_canonical_decision(
    migrated_temp_db_path: Path,
) -> None:
    run_id, statement_id, batch_id = _seed_concurrent_decision_context(migrated_temp_db_path)
    result = match_statement(_statement(source_batch_id=str(batch_id)), [_candidate()])
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _concurrent_save,
                migrated_temp_db_path,
                barrier,
                run_id,
                statement_id,
                result,
                "decision-concurrent-identical",
            )
            for _ in range(2)
        ]
        ids = [future.result(timeout=15) for future in futures]
    assert ids[0] == ids[1]
    conn = sqlite3.connect(migrated_temp_db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM reconciliation_match_results "
                "WHERE public_id = 'decision-concurrent-identical'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_concurrent_conflicting_persistence_has_one_winner_and_one_conflict(
    migrated_temp_db_path: Path,
) -> None:
    run_id, statement_id, batch_id = _seed_concurrent_decision_context(migrated_temp_db_path)
    statement = _statement(source_batch_id=str(batch_id))
    first = match_statement(statement, [_candidate()])
    second = match_statement(
        statement,
        [_candidate(internal_id="transaction-2")],
    )
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _concurrent_save,
                migrated_temp_db_path,
                barrier,
                run_id,
                statement_id,
                result,
                "decision-concurrent-conflict",
            )
            for result in (first, second)
        ]
        outcomes: list[int | Exception] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=15))
            except Exception as exc:  # noqa: PERF203 - two deterministic futures only
                outcomes.append(exc)
    assert sum(isinstance(outcome, int) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], DuplicatePublicIdError)


def test_audit_append_failure_rolls_back_decision_batch(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch_id = _seed_service_batch(migrated_temp_db_connection)
    migrated_temp_db_connection.execute(
        """CREATE TRIGGER test_fail_reconciliation_decision_audit
        BEFORE INSERT ON financial_audit_events
        WHEN NEW.aggregate_type = 'reconciliation_match_result'
        BEGIN SELECT RAISE(ABORT, 'injected reconciliation audit failure'); END"""
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected reconciliation audit failure"):
        ReconciliationService(
            ReconciliationRepository(migrated_temp_db_connection)
        ).run_reconciliation_for_batch(batch_id, [_candidate()], "run-audit-failure")
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM reconciliation_match_results"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events"
        ).fetchone()[0]
        == 0
    )


def test_restart_reload_integrity_and_foreign_keys(
    migrated_temp_db_path: Path,
) -> None:
    conn = sqlite3.connect(migrated_temp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        batch_id = _seed_service_batch(conn)
        ReconciliationService(ReconciliationRepository(conn)).run_reconciliation_for_batch(
            batch_id,
            [_candidate()],
            "run-restart",
        )
    finally:
        conn.close()
    reopened = sqlite3.connect(migrated_temp_db_path)
    reopened.row_factory = sqlite3.Row
    try:
        row = reopened.execute("SELECT * FROM reconciliation_match_results").fetchone()
        assert verify_persisted_decision_hash(row["decision_hash"], row["decision_material_json"])
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()
