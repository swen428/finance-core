"""Separate-connection concurrency, crash-restart, and replay safety.

Every race in this module opens one SQLite connection per worker.  Service
tests exercise the production ``BEGIN IMMEDIATE`` boundaries; constraint tests
exercise the database authorities that make duplicate terminal writes lose.
Crash tests close the failed connection and inspect state after reconnecting.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Callable

import pytest

from finance_core.parser_proposals.confirmation import confirm_proposal, reject_proposal
from finance_core.parser_proposals.conversion import convert_confirmed_proposal_to_transaction
from finance_core.parser_proposals.service import ParserConfirmationError
from finance_core.reconciliation.repository import DuplicatePublicIdError
from finance_core.reconciliation.statement_import import StatementImporter, StructuredStatementRow
from finance_core.sqlite_connection import connect_sqlite

_STATEMENT_FINGERPRINT = "a" * 64


def _fingerprint(value: str) -> str:
    if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _RaceOutcome:
    value: object | None = None
    error: BaseException | None = None


def _race(
    database: Path,
    first: Callable[[sqlite3.Connection], object],
    second: Callable[[sqlite3.Connection], object] | None = None,
    *,
    timeout_seconds: float = 5.0,
) -> tuple[_RaceOutcome, _RaceOutcome]:
    """Run two commands with independently opened, configured connections."""
    barrier = Barrier(2)

    def run(command: Callable[[sqlite3.Connection], object]) -> _RaceOutcome:
        conn = connect_sqlite(database, timeout_seconds=timeout_seconds)
        try:
            barrier.wait(timeout=10)
            return _RaceOutcome(value=command(conn))
        except BaseException as exc:  # result object intentionally captures worker errors
            if conn.in_transaction:
                conn.rollback()
            return _RaceOutcome(error=exc)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        commands = (first, second or first)
        futures = [executor.submit(run, command) for command in commands]
        return futures[0].result(timeout=15), futures[1].result(timeout=15)


def _inspect(database: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = connect_sqlite(database)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _insert_parser_proposal(conn: sqlite3.Connection, public_id: str = "parser-race-001") -> int:
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": "6.40",
        "currency": "SGD",
        "transaction_date": "2026-07-12",
        "merchant": "Coffee Shop",
        "description": "coffee",
    }
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
          public_id, source_type, parser_name, parser_version, raw_text,
          parsed_payload, normalized_payload, parse_status
        ) VALUES (?, 'telegram_text', 'pytest', 'v1', 'Coffee SGD 6.40', ?, ?,
                  'parsed_pending_confirmation')
        """,
        (public_id, json.dumps(payload), json.dumps(payload)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _statement_row(
    *,
    merchant: str = "Race Merchant",
    amount: str = "10.00",
    fingerprint: str = _STATEMENT_FINGERPRINT,
) -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw=merchant,
        amount=Decimal(amount),
        currency="SGD",
        transaction_date=date(2026, 7, 12),
        posted_date=date(2026, 7, 13),
        statement_row_reference="line-1",
        raw_row_payload={"line": 1, "merchant": merchant, "amount": amount},
        row_fingerprint=_fingerprint(fingerprint),
    )


def _import(
    conn: sqlite3.Connection,
    row: StructuredStatementRow,
    *,
    public_id: str = "statement-race-001",
) -> object:
    return StatementImporter(conn).import_rows(
        [row],
        source_type="bank_statement",
        public_id=public_id,
        source_file_hash="a" * 64,
    )


def test_two_concurrent_identical_confirmations_replay_one_authorization(
    migrated_temp_db_path: Path,
) -> None:
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        parser_output_id = _insert_parser_proposal(setup)
    finally:
        setup.close()

    def command(conn: sqlite3.Connection) -> object:
        return confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="parser-confirm-race",
        )

    outcomes = _race(migrated_temp_db_path, command)
    assert all(outcome.error is None for outcome in outcomes)
    assert sorted(bool(outcome.value["idempotent"]) for outcome in outcomes) == [False, True]  # type: ignore[index]
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?",
                (parser_output_id,),
            )
        )
        == 1
    )
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM parser_proposal_events WHERE parser_output_id = ?",
                (parser_output_id,),
            )
        )
        == 1
    )


def test_concurrent_confirmed_and_rejected_terminal_decisions_cannot_both_win(
    migrated_temp_db_path: Path,
) -> None:
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        parser_output_id = _insert_parser_proposal(setup, "parser-terminal-race")
    finally:
        setup.close()

    outcomes = _race(
        migrated_temp_db_path,
        lambda conn: confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="terminal-confirm",
        ),
        lambda conn: reject_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="terminal-reject",
        ),
    )
    assert sum(outcome.error is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome.error, ParserConfirmationError) for outcome in outcomes) == 1
    authorization = _inspect(
        migrated_temp_db_path,
        "SELECT confirmation_state FROM parser_proposal_authorizations WHERE parser_output_id = ?",
        (parser_output_id,),
    )
    proposal = _inspect(
        migrated_temp_db_path,
        "SELECT parse_status FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )
    assert len(authorization) == 1
    assert authorization[0]["confirmation_state"] in {"confirmed", "rejected"}
    assert proposal[0]["parse_status"] == authorization[0]["confirmation_state"]


def test_two_concurrent_parser_conversions_create_one_canonical_transaction(
    migrated_temp_db_path: Path,
) -> None:
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        parser_output_id = _insert_parser_proposal(setup, "parser-conversion-race")
        confirm_proposal(
            setup,
            parser_output_id,
            actor="owner",
            confirmation_public_id="conversion-confirm",
        )
    finally:
        setup.close()

    outcomes = _race(
        migrated_temp_db_path,
        lambda conn: convert_confirmed_proposal_to_transaction(conn, parser_output_id),
    )
    assert all(outcome.error is None for outcome in outcomes)
    results = [outcome.value for outcome in outcomes]
    assert {result["transaction_public_id"] for result in results}.__len__() == 1  # type: ignore[index]
    assert sorted(bool(result["idempotent"]) for result in results) == [False, True]  # type: ignore[index]
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM transactions WHERE parser_output_id = ?",
                (parser_output_id,),
            )
        )
        == 1
    )
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM parser_proposal_conversion_audit WHERE parser_output_id = ?",
                (parser_output_id,),
            )
        )
        == 1
    )


def test_two_concurrent_identical_statement_imports_commit_one_batch_and_row(
    migrated_temp_db_path: Path,
) -> None:
    outcomes = _race(
        migrated_temp_db_path,
        lambda conn: _import(conn, _statement_row()),
    )
    assert all(outcome.error is None for outcome in outcomes)
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM statement_import_batches WHERE public_id = 'statement-race-001'",
            )
        )
        == 1
    )
    rows = _inspect(
        migrated_temp_db_path,
        """SELECT st.*
             FROM statement_transactions AS st
             JOIN statement_import_batches AS sib ON sib.id = st.batch_id
            WHERE sib.public_id = 'statement-race-001'""",
    )
    assert len(rows) == 1
    assert rows[0]["row_fingerprint"] != _STATEMENT_FINGERPRINT
    assert rows[0]["row_fingerprint_version"] == "statement-row-fingerprint-v1"
    assert rows[0]["external_row_fingerprint"] == _STATEMENT_FINGERPRINT
    assert rows[0]["external_row_fingerprint_version"] == "caller-supplied-sha256-v1"
    assert json.loads(rows[0]["raw_row_payload_json"])["merchant"] == "Race Merchant"


def test_same_source_fingerprint_with_changed_content_is_a_stable_conflict(
    migrated_temp_db_path: Path,
) -> None:
    outcomes = _race(
        migrated_temp_db_path,
        lambda conn: _import(conn, _statement_row(amount="10.00")),
        lambda conn: _import(conn, _statement_row(amount="11.00")),
    )
    assert sum(outcome.error is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome.error, DuplicatePublicIdError) for outcome in outcomes) == 1
    rows = _inspect(
        migrated_temp_db_path,
        """SELECT st.amount, st.raw_row_payload_json,
                  st.row_fingerprint, st.external_row_fingerprint
             FROM statement_transactions AS st
             JOIN statement_import_batches AS sib ON sib.id = st.batch_id
            WHERE sib.public_id = 'statement-race-001'""",
    )
    assert len(rows) == 1
    assert rows[0]["row_fingerprint"] != _STATEMENT_FINGERPRINT
    assert rows[0]["external_row_fingerprint"] == _STATEMENT_FINGERPRINT
    assert str(rows[0]["amount"]) in {"10", "11"}


def test_concurrent_receipt_finalization_audit_append_has_one_successor(
    migrated_temp_db_path: Path,
) -> None:
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        content_hash = "b" * 64
        setup.execute(
            """INSERT INTO receipt_finalization_confirmations (
            confirmation_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, content_hash, currency, final_total,
            payer_participant_public_id, actor_type, actor_id, confirmation_state, created_at
            ) VALUES ('conf-race', 'group-race', 'calc-race', 'snap-race', ?, 'SGD',
                      '10.00', 'person-owner', 'human', 'owner', 'confirmed', '2026-07-13')""",
            (content_hash,),
        )
        setup.execute(
            """INSERT INTO receipt_finalization_authorizations (
            authorization_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, confirmation_id, content_hash, currency, final_total,
            payer_participant_public_id, actor_type, actor_id, authorization_state, created_at
            ) VALUES ('auth-race', 'group-race', 'calc-race', 'snap-race', 'conf-race', ?,
                      'SGD', '10.00', 'person-owner', 'human', 'owner',
                      'authorized', '2026-07-13')""",
            (content_hash,),
        )
        setup.commit()
    finally:
        setup.close()

    def append(conn: sqlite3.Connection, suffix: str) -> object:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """INSERT INTO receipt_finalization_audit (
            finalization_id, idempotency_key, content_fingerprint, authorization_id,
            confirmation_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, currency, total_paid, total_to_collect,
            payer_participant_public_id, actor_type, actor_id, status, created_at
            ) VALUES (?, ?, ?, 'auth-race', 'conf-race', 'group-race', 'calc-race',
                      'snap-race', 'SGD', '10.00', '5.00', 'person-owner', 'human', 'owner',
                      'finalized', '2026-07-13')""",
            (f"finalization-{suffix}", f"idem-{suffix}", "b" * 64),
        )
        conn.commit()
        return cursor.lastrowid

    outcomes = _race(
        migrated_temp_db_path,
        lambda conn: append(conn, "a"),
        lambda conn: append(conn, "b"),
    )
    assert sum(outcome.error is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome.error, sqlite3.IntegrityError) for outcome in outcomes) == 1
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM receipt_finalization_audit "
                "WHERE receipt_group_public_id = 'group-race'",
            )
        )
        == 1
    )


def test_two_valid_looking_attempts_cannot_consume_one_authorization_twice(
    migrated_temp_db_path: Path,
) -> None:
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        content_hash = "c" * 64
        setup.execute(
            """INSERT INTO receipt_finalization_confirmations (
            confirmation_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, content_hash, currency, final_total,
            payer_participant_public_id, actor_type, confirmation_state, created_at
            ) VALUES ('conf-consume', 'group-consume', 'calc-consume', 'snap-consume', ?,
                      'SGD', '10.00', 'person-owner', 'human', 'confirmed', '2026-07-13')""",
            (content_hash,),
        )
        setup.execute(
            """INSERT INTO receipt_finalization_authorizations (
            authorization_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, confirmation_id, content_hash, currency, final_total,
            payer_participant_public_id, actor_type, authorization_state, created_at
            ) VALUES ('auth-consume', 'group-consume', 'calc-consume', 'snap-consume',
                      'conf-consume', ?, 'SGD', '10.00', 'person-owner', 'human',
                      'authorized', '2026-07-13')""",
            (content_hash,),
        )
        setup.commit()
    finally:
        setup.close()

    def consume(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET authorization_state = 'consumed' "
            "WHERE authorization_id = 'auth-consume' AND authorization_state = 'authorized'"
        )
        conn.commit()
        return cursor.rowcount

    outcomes = _race(migrated_temp_db_path, consume)
    assert all(outcome.error is None for outcome in outcomes)
    assert sorted(int(outcome.value) for outcome in outcomes) == [0, 1]
    state = _inspect(
        migrated_temp_db_path,
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = 'auth-consume'",
    )
    assert state[0]["authorization_state"] == "consumed"


def test_concurrent_settlement_generation_keeps_one_deterministic_obligation(
    migrated_temp_db_path: Path,
) -> None:
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        setup.execute(
            "INSERT INTO participants (public_id, display_name) VALUES ('debtor-race', 'Debtor')"
        )
        debtor_id = int(setup.execute("SELECT last_insert_rowid()").fetchone()[0])
        setup.execute(
            "INSERT INTO participants (public_id, display_name) "
            "VALUES ('creditor-race', 'Creditor')"
        )
        creditor_id = int(setup.execute("SELECT last_insert_rowid()").fetchone()[0])
        setup.execute(
            "INSERT INTO receipt_groups (public_id, currency) VALUES ('group-settlement', 'SGD')"
        )
        group_id = int(setup.execute("SELECT last_insert_rowid()").fetchone()[0])
        setup.execute(
            """INSERT INTO calculation_runs
            (public_id, calculation_version, scope_type, receipt_group_id, currency)
            VALUES ('calc-settlement-race', 'v1', 'receipt_group', ?, 'SGD')""",
            (group_id,),
        )
        run_id = int(setup.execute("SELECT last_insert_rowid()").fetchone()[0])
        setup.commit()
    finally:
        setup.close()

    def generate(conn: sqlite3.Connection) -> object:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """INSERT INTO settlement_obligations
            (public_id, debtor_id, creditor_id, amount, currency, source_calculation_run_id)
            VALUES ('obligation-deterministic-race', ?, ?, '5.00', 'SGD', ?)""",
            (debtor_id, creditor_id, run_id),
        )
        conn.commit()
        return cursor.lastrowid

    outcomes = _race(migrated_temp_db_path, generate)
    assert sum(outcome.error is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome.error, sqlite3.IntegrityError) for outcome in outcomes) == 1
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM settlement_obligations "
                "WHERE public_id = 'obligation-deterministic-race'",
            )
        )
        == 1
    )


def test_concurrent_final_mutation_idempotency_append_has_one_winner(
    migrated_temp_db_path: Path,
) -> None:
    def append(conn: sqlite3.Connection, suffix: str) -> object:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """INSERT INTO reconciliation_final_mutation_audit (
            final_mutation_id, idempotency_key, idempotency_fingerprint, operation_id,
            plan_id, proposal_id, guard_decision_proposal_id, guarded_execution_id,
            guarded_execution_idempotency_key, human_confirmation_id, actor_type,
            action, status, created_at
            ) VALUES (?, 'final-mutation-idem-race', ?, 'op-race', 'plan-race',
                      'proposal-race', 'guard-race', 'execution-race', 'execution-idem-race',
                      'human-confirm-race', 'human', 'create_final_transaction_proposal',
                      'finalized', '2026-07-13')""",
            (f"mutation-{suffix}", f"fingerprint-{suffix}"),
        )
        conn.commit()
        return cursor.lastrowid

    outcomes = _race(
        migrated_temp_db_path,
        lambda conn: append(conn, "a"),
        lambda conn: append(conn, "b"),
    )
    assert sum(outcome.error is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome.error, sqlite3.IntegrityError) for outcome in outcomes) == 1
    assert (
        len(
            _inspect(
                migrated_temp_db_path,
                "SELECT * FROM reconciliation_final_mutation_audit "
                "WHERE idempotency_key = 'final-mutation-idem-race'",
            )
        )
        == 1
    )


def test_database_busy_is_stable_and_retry_succeeds_after_lock_release(
    migrated_temp_db_path: Path,
) -> None:
    blocker = connect_sqlite(migrated_temp_db_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        contender = connect_sqlite(migrated_temp_db_path, timeout_seconds=0.05)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                _import(
                    contender,
                    _statement_row(fingerprint="busy-row"),
                    public_id="busy-batch",
                )
            assert contender.in_transaction is False
        finally:
            contender.close()
    finally:
        blocker.rollback()
        blocker.close()

    retry = connect_sqlite(migrated_temp_db_path)
    try:
        result = _import(retry, _statement_row(fingerprint="busy-row"), public_id="busy-batch")
        assert result.row_count == 1
    finally:
        retry.close()


@pytest.mark.parametrize("crash_point", ["after_batch", "before_commit"])
def test_crash_inside_import_rolls_back_and_restart_retry_has_no_partial_state(
    migrated_temp_db_path: Path,
    crash_point: str,
) -> None:
    class InjectedCrash(RuntimeError):
        pass

    def crash() -> None:
        raise InjectedCrash(crash_point)

    failing = connect_sqlite(migrated_temp_db_path)
    try:
        importer = StatementImporter(
            failing,
            _test_post_batch_hook=crash if crash_point == "after_batch" else None,
            _test_pre_commit_hook=crash if crash_point == "before_commit" else None,
        )
        with pytest.raises(InjectedCrash, match=crash_point):
            importer.import_rows(
                [
                    _statement_row(merchant="First", fingerprint=f"{crash_point}-1"),
                    _statement_row(merchant="Child", fingerprint=f"{crash_point}-2"),
                ],
                source_type="bank_statement",
                public_id=f"crash-{crash_point}",
                source_file_hash="b" * 64,
            )
    finally:
        failing.close()

    assert (
        _inspect(
            migrated_temp_db_path,
            "SELECT * FROM statement_import_batches WHERE public_id = ?",
            (f"crash-{crash_point}",),
        )
        == []
    )
    assert (
        _inspect(
            migrated_temp_db_path,
            "SELECT * FROM statement_transactions WHERE merchant_raw IN ('First', 'Child')",
        )
        == []
    )

    restarted = connect_sqlite(migrated_temp_db_path)
    try:
        result = StatementImporter(restarted).import_rows(
            [
                _statement_row(merchant="First", fingerprint=f"{crash_point}-1"),
                _statement_row(merchant="Child", fingerprint=f"{crash_point}-2"),
            ],
            source_type="bank_statement",
            public_id=f"crash-{crash_point}",
            source_file_hash="b" * 64,
        )
        assert len(result.inserted_ids) == 2
    finally:
        restarted.close()


def test_durable_commit_lost_response_replays_canonical_result_after_restart(
    migrated_temp_db_path: Path,
) -> None:
    first = connect_sqlite(migrated_temp_db_path)
    try:
        committed = _import(
            first,
            _statement_row(fingerprint="lost-response-row"),
            public_id="lost-response-batch",
        )
        committed_batch_id = committed.batch_id
        # Simulate process loss after commit by discarding the returned object.
    finally:
        first.close()

    restarted = connect_sqlite(migrated_temp_db_path)
    try:
        replay = _import(
            restarted,
            _statement_row(fingerprint="lost-response-row"),
            public_id="lost-response-batch",
        )
        assert replay.batch_id == committed_batch_id
        assert replay.inserted_ids == []
        assert replay.idempotent_count == 1
    finally:
        restarted.close()

    rows = _inspect(
        migrated_temp_db_path,
        """SELECT st.amount, st.currency, st.raw_row_payload_json,
                  st.row_fingerprint, st.external_row_fingerprint
             FROM statement_transactions AS st
             JOIN statement_import_batches AS sib ON sib.id = st.batch_id
            WHERE sib.public_id = 'lost-response-batch'""",
    )
    assert len(rows) == 1
    assert rows[0]["row_fingerprint"] != _fingerprint("lost-response-row")
    assert rows[0]["external_row_fingerprint"] == _fingerprint("lost-response-row")
    assert str(rows[0]["amount"]) == "10"
    assert rows[0]["currency"] == "SGD"
    assert json.loads(rows[0]["raw_row_payload_json"])["amount"] == "10.00"


def test_pre_transaction_failure_creates_no_financial_state(
    migrated_temp_db_path: Path,
) -> None:
    conn = connect_sqlite(migrated_temp_db_path)
    try:
        with pytest.raises(ValueError, match="rows must not be empty"):
            StatementImporter(conn).import_rows([], source_type="bank_statement")
        assert conn.in_transaction is False
    finally:
        conn.close()
    assert _inspect(migrated_temp_db_path, "SELECT * FROM statement_import_batches") == []
    assert _inspect(migrated_temp_db_path, "SELECT * FROM statement_transactions") == []


def test_restart_verification_has_clean_integrity_foreign_keys_and_no_orphans(
    migrated_temp_db_path: Path,
) -> None:
    conn = connect_sqlite(migrated_temp_db_path)
    try:
        _import(
            conn,
            _statement_row(fingerprint="integrity-restart-row"),
            public_id="integrity-restart-batch",
        )
    finally:
        conn.close()

    restarted = connect_sqlite(migrated_temp_db_path)
    try:
        assert restarted.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restarted.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            restarted.execute(
                """SELECT COUNT(*) FROM statement_transactions st
            LEFT JOIN statement_import_batches b ON b.id = st.batch_id
            WHERE b.id IS NULL"""
            ).fetchone()[0]
            == 0
        )
    finally:
        restarted.close()


def test_concurrency_suite_never_targets_live_database(migrated_temp_db_path: Path) -> None:
    live_path = Path(__file__).resolve().parents[1] / "database" / "finance.db"
    assert migrated_temp_db_path.resolve() != live_path.resolve()
