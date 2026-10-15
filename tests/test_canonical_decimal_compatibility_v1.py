"""Finite historical compatibility for lossless Decimal canonicalization."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from finance_core.calculation.authoritative_snapshot import (
    build_authoritative_snapshot,
    load_snapshot_for_authoritative_use,
    persist_authoritative_snapshot,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event_atomically,
    verify_financial_audit_chain,
)
from finance_core.money import MoneyValidationError
from finance_core.reconciliation import statement_identity
from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.repository import (
    DuplicatePublicIdError,
    ReconciliationRepository,
    ReconciliationRepositoryError,
)
from finance_core.reconciliation.statement_csv import StatementCsvAdapter
from finance_core.reconciliation.statement_import import StatementImporter
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow
from finance_core.staging_guard import require_staging_database

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "canonical_decimal_baseline_v1.json").read_text(
        encoding="utf-8"
    )
)


def _table(conn: sqlite3.Connection, name: str) -> dict[str, object]:
    return {
        "columns": [row["name"] for row in conn.execute(f"PRAGMA table_info({name})")],
        "rows": [list(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")],
    }


def _statement_tables(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        name: _table(conn, name)
        for name in (
            "statement_import_batches",
            "statement_transactions",
            "statement_import_source_evidence",
            "financial_audit_events",
        )
    }


def _snapshot():
    return build_authoritative_snapshot(
        snapshot_public_id="baseline-snapshot-1",
        calculation_type="receipt_split",
        aggregate_public_id="baseline-receipt-1",
        input_payload={"amount": Decimal("12.300"), "rate": Decimal("0.6000")},
        output_payload={"total_paid": Decimal("12.30"), "owner_share": Decimal("7.38")},
        rules_payload={"rounding": "ROUND_HALF_UP", "minor_units": 2},
        money_contract_version="money-v1",
        currency_contract_version="currency-SGD-v1",
        algorithm_version="receipt-split-v3",
        source_references=("receipt:synthetic-1",),
        actor_type="system",
        actor_public_id="calculator-service",
        authorization_reference="auth-synthetic-1",
        finalization_status="finalized",
        created_at="2026-07-13T02:00:00+00:00",
    )


def _connect_existing(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    require_staging_database(conn)
    return conn


def test_old_snapshot_and_audit_bytes_survive_new_chain_and_tamper_refusal(
    migrated_temp_db_path: Path,
) -> None:
    conn = _connect_existing(migrated_temp_db_path)
    snapshot = _snapshot()
    try:
        persisted, idempotent = persist_authoritative_snapshot(conn, snapshot)
        assert persisted == snapshot and not idempotent
        assert _table(conn, "authoritative_calculation_snapshots") == GOLDEN["snapshot"]
        assert _table(conn, "financial_audit_events") == GOLDEN["audit"]
        assert load_snapshot_for_authoritative_use(conn, snapshot.snapshot_public_id) == snapshot
        old_audit = _table(conn, "financial_audit_events")["rows"][0]
        replayed, idempotent = persist_authoritative_snapshot(conn, snapshot)
        assert replayed == snapshot and idempotent
        assert _table(conn, "financial_audit_events")["rows"][0] == old_audit

        next_event, idempotent = append_financial_audit_event_atomically(
            conn,
            AuditEventCommand(
                event_public_id="fae-synthetic-followup",
                aggregate_type="calculation_snapshot",
                aggregate_public_id=snapshot.snapshot_public_id,
                event_type="snapshot_reviewed",
                event_payload={"rate": Decimal("0.0000500000000000000000000000000000")},
                previous_state=None,
                new_state={
                    "status": "reviewed",
                    "rate": Decimal("0.0000500000000000000000000000000000"),
                },
                actor_type="system",
                actor_public_id="review-service",
                correlation_public_id="baseline-receipt-1",
                causation_public_id="review-1",
                created_at="2026-07-13T02:01:00+00:00",
            ),
        )
        assert not idempotent and next_event.sequence_number == 2
        assert next_event.previous_event_hash == old_audit[11]
        assert next_event.previous_state_hash == old_audit[9]
        assert _table(conn, "financial_audit_events")["rows"][0] == old_audit
        assert not conn.in_transaction
    finally:
        conn.close()

    reopened = _connect_existing(migrated_temp_db_path)
    try:
        assert (
            load_snapshot_for_authoritative_use(reopened, snapshot.snapshot_public_id) == snapshot
        )
        assert _table(reopened, "authoritative_calculation_snapshots") == GOLDEN["snapshot"]
        assert _table(reopened, "financial_audit_events")["rows"][0] == old_audit
        chain = verify_financial_audit_chain(
            reopened,
            aggregate_type="calculation_snapshot",
            aggregate_public_id=snapshot.snapshot_public_id,
        )
        assert chain.valid and chain.event_count == 2
        # Test-only corruption proves the verifier still rejects altered old bytes.
        reopened.execute("DROP TRIGGER trg_financial_audit_events_no_update")
        reopened.execute(
            "UPDATE financial_audit_events SET event_payload_json = ? WHERE event_public_id = ?",
            (
                '{"contract_version":"finance-canonical-json-v1","value":{"changed":true}}',
                old_audit[0],
            ),
        )
        reopened.commit()
        tampered = verify_financial_audit_chain(
            reopened,
            aggregate_type="calculation_snapshot",
            aggregate_public_id=snapshot.snapshot_public_id,
        )
        assert not tampered.valid
        assert tampered.first_invalid_event_public_id == old_audit[0]
        assert tampered.reason == "Audit event hash mismatch"
    finally:
        reopened.close()


def _statement_row(amount: str) -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw="F1 synthetic",
        amount=Decimal(amount),
        currency="SGD",
        transaction_date=date(2026, 10, 15),
        statement_row_reference="line-1",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount=amount,
        raw_amount_type="debit",
        raw_row_payload={"date": "2026-10-15", "merchant": "F1 synthetic", "amount": amount},
    )


def _import(conn: sqlite3.Connection, row: StructuredStatementRow, path: Path):
    return StatementImporter(conn).import_rows(
        [row], source_type="structured_csv", source_file_path=str(path)
    )


def test_previously_accepted_rounded_statement_fails_closed_without_mutation(
    migrated_temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"date,merchant,amount\n2026-10-15,F1 synthetic,12345.67\n")
    row = _statement_row("12345.67")
    conn = _connect_existing(migrated_temp_db_path)
    current_helper = statement_identity.canonical_decimal_str

    def old_context_serializer(value: Decimal) -> str:
        # Isolated baseline behavior. The frozen identities below came from
        # a genuine service import on the pre-fix commit.
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    try:
        with localcontext() as context:
            context.prec = 6
            with monkeypatch.context() as patch:
                patch.setattr(statement_identity, "canonical_decimal_str", old_context_serializer)
                first = _import(conn, row, source)
                assert (
                    first.import_command_hash
                    == "7268958de451826f33118dadcbec80a2799e31238033860c0ea02ea68279af13"
                )
                assert (
                    first.row_set_fingerprint
                    == "81012dec59a051059e66118b984415f79cd383033e14341a86d7462e0245b439"
                )
                stored = conn.execute(
                    "SELECT amount, raw_amount, row_fingerprint FROM statement_transactions"
                ).fetchone()
                assert Decimal(str(stored["amount"])) == Decimal("12345.67")
                assert stored["raw_amount"] == "12345.67"
                assert (
                    stored["row_fingerprint"]
                    == "f3f34db40dac264ae2f7510836a7804e139a4e0369be3a9afe80aa34bb1e1f14"
                )
                ReconciliationRepository(conn).verify_statement_import_batch(first.batch_id)
                replay = _import(conn, row, source)
                assert replay.batch_id == first.batch_id and replay.idempotent_count == 1
        frozen = _statement_tables(conn)
        assert statement_identity.canonical_decimal_str is current_helper
        with localcontext() as context:
            context.prec = 6
            with pytest.raises(
                ReconciliationRepositoryError, match="generic row fingerprint mismatch"
            ):
                ReconciliationRepository(conn).verify_statement_import_batch(first.batch_id)
            with pytest.raises(DuplicatePublicIdError, match="different import command"):
                _import(conn, row, source)
        assert _statement_tables(conn) == frozen and not conn.in_transaction
    finally:
        conn.close()

    reopened = _connect_existing(migrated_temp_db_path)
    try:
        with pytest.raises(ReconciliationRepositoryError, match="generic row fingerprint mismatch"):
            ReconciliationRepository(reopened).verify_statement_import_batch(first.batch_id)
        assert _statement_tables(reopened) == frozen and not reopened.in_transaction
    finally:
        reopened.close()


def test_unaffected_statement_import_still_replays_after_reopen(
    migrated_temp_db_path: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"date,merchant,amount\n2026-10-15,F1 synthetic,12.34\n")
    row = _statement_row("12.34")
    conn = _connect_existing(migrated_temp_db_path)
    try:
        first = _import(conn, row, source)
        frozen = _statement_tables(conn)
        assert (
            first.import_command_hash
            == "1bb4d8d56cc3182584c4bdabc6616b1cd6e62493950f9427ac554869491a65e1"
        )
        ReconciliationRepository(conn).verify_statement_import_batch(first.batch_id)
        assert _import(conn, row, source).idempotent_count == 1
        assert _statement_tables(conn) == frozen and not conn.in_transaction
    finally:
        conn.close()
    reopened = _connect_existing(migrated_temp_db_path)
    try:
        ReconciliationRepository(reopened).verify_statement_import_batch(first.batch_id)
        assert _import(reopened, row, source).idempotent_count == 1
        assert _statement_tables(reopened) == frozen and not reopened.in_transaction
    finally:
        reopened.close()


def test_short_csv_exponent_is_rejected_before_import_writes(
    migrated_temp_db_path: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(
        b"transaction_date,merchant_raw,amount,currency,reference\n"
        b"2026-10-15,F1 synthetic,1e1000000000,SGD,line-1\n"
    )
    parsed = StatementCsvAdapter().parse_file_hardened(source)
    assert len(parsed.rows) == 1
    assert parsed.rows[0].amount == Decimal("1e1000000000")
    conn = _connect_existing(migrated_temp_db_path)
    try:
        frozen = _statement_tables(conn)
        with pytest.raises(MoneyValidationError, match="maximum plain-decimal length"):
            StatementImporter(conn).import_rows(
                parsed.rows,
                source_type="structured_csv",
                source_file_path=str(source),
            )
        assert _statement_tables(conn) == frozen and not conn.in_transaction
    finally:
        conn.close()


def test_distinct_long_rates_change_a_cent_and_the_payloads_that_contain_them() -> None:
    rates = (
        Decimal("0.0000499999999999999999999999999999"),
        Decimal("0.0000500000000000000000000000000000"),
    )
    snapshots = []
    totals = []
    for index, rate in enumerate(rates):
        case = {
            "currency": "SGD",
            "participants": ["A"],
            "receipts": [
                {
                    "paid_by": "A",
                    "service_charge_rate": rate,
                    "items": [{"amount": "100.00", "participants": ["A"]}],
                }
            ],
        }
        result = calculate_receipt_split(case)
        totals.append(result["total_paid"])
        snapshots.append(
            build_authoritative_snapshot(
                snapshot_public_id=f"rate-snapshot-{index}",
                calculation_type="receipt_split",
                aggregate_public_id="rate-receipt",
                input_payload=case,
                output_payload={"total_paid": result["total_paid"]},
                rules_payload={"minor_units": 2},
                money_contract_version="money-v1",
                currency_contract_version="currency-SGD-v1",
                algorithm_version="receipt-split-v3",
                source_references=("receipt:synthetic-rate",),
                actor_type="system",
                actor_public_id="calculator-service",
                authorization_reference="auth-synthetic-rate",
                finalization_status="draft",
                created_at="2026-07-13T02:00:00+00:00",
            )
        )
    assert totals == [Decimal("100.00"), Decimal("100.01")]
    assert snapshots[0].input_payload_json != snapshots[1].input_payload_json
    assert snapshots[0].input_hash != snapshots[1].input_hash
    assert snapshots[0].output_hash != snapshots[1].output_hash
    assert snapshots[0].rules_hash == snapshots[1].rules_hash
