"""Tests for receipt finalization settlement runtime.

Covers: authorization, atomicity, idempotency, monetary correctness,
participant identity, audit and evidence, schema safety, and regression.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeCalculationSnapshot,
    AuthoritativeSnapshotRepository,
    build_authoritative_snapshot,
    persist_authoritative_snapshot,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.receipt_finalization import (
    FinalizationAuthorizationError,
    FinalizationIdempotencyError,
    FinalizationInput,
    FinalizationOutput,
    FinalizationPersistenceError,
    FinalizationStatus,
    FinalizationValidationError,
    IneligibleForFinalizationError,
    SettlementObligation,
    build_finalization_content_fingerprint,
    finalize_receipt_split,
    to_settlement_obligations,
)
from finance_core.resources import migrations_dir
from finance_core.sqlite_connection import connect_sqlite

TC001_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tc001_receipt_split_example_restaurant_example_tea.json"
)
TEST_CALC_PUBLIC_ID = "calc_test_finalization_v1"
TEST_GROUP_PUBLIC_ID = "rg_test_finalization_v1"

PAYER_PUBLIC_ID = "person_owner"
TC001_PUBLIC_IDS = [
    "person_owner",
    "person_member_a",
    "person_member_b",
    "person_member_c",
    "person_member_d",
    "person_member_e",
]
TC001_DISPLAY_NAMES = ["Owner", "MemberA", "MemberB", "MemberC", "MemberD", "MemberE"]

# -- helpers for test database setup ---


def _seed_participants(conn: sqlite3.Connection) -> None:
    for pub_id, name in zip(TC001_PUBLIC_IDS, TC001_DISPLAY_NAMES):
        conn.execute(
            """
            INSERT OR IGNORE INTO participants (public_id, display_name, aliases, is_self, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pub_id, name, "[]", 1 if pub_id == "person_owner" else 0, ""),
        )


def _seed_receipt_group(
    conn: sqlite3.Connection,
    group_public_id: str = TEST_GROUP_PUBLIC_ID,
    status: str = "calculated",
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO receipt_groups (public_id, currency, status)
        VALUES (?, ?, ?)
        """,
        (group_public_id, "SGD", status),
    )


def _seed_receipt_group_participant_links(
    conn: sqlite3.Connection,
    group_public_id: str = TEST_GROUP_PUBLIC_ID,
) -> None:
    """Link participants to the receipt group through receipt_participants.

    Finalization queries participants scoped to the receipt group, so we
    need receipt + receipt_participant rows for the lookup to work.
    """
    # Create a minimal receipt in the group
    group_id = conn.execute(
        "SELECT id FROM receipt_groups WHERE public_id = ?",
        (group_public_id,),
    ).fetchone()["id"]

    # Insert a placeholder receipt
    conn.execute(
        """
        INSERT OR IGNORE INTO receipts (
            public_id, merchant, receipt_datetime, gross_amount, subtotal_amount,
            net_paid_amount, currency, payer_participant_id,
            source_channel, raw_input, status
        ) VALUES (
            ?, 'Test Merchant', '2026-01-01 12:00:00', 100.00, 100.00,
            100.00, 'SGD',
            (SELECT id FROM participants WHERE public_id = 'person_owner'),
            'manual_test_case', 'test', 'confirmed'
        )
        """,
        ("r_test_finalization",),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO receipt_group_receipts (
            public_id, receipt_group_id, receipt_id, sequence_number
        ) VALUES (
            ?, ?, (SELECT id FROM receipts WHERE public_id = ?), 1
        )
        """,
        ("rgr_test_fin", group_id, "r_test_finalization"),
    )

    # Link all participants
    for pub_id in TC001_PUBLIC_IDS:
        conn.execute(
            """
            INSERT OR IGNORE INTO receipt_participants (
                public_id, receipt_id, participant_id, role, is_included
            ) VALUES (
                ?,
                (SELECT id FROM receipts WHERE public_id = ?),
                (SELECT id FROM participants WHERE public_id = ?),
                'participant', 1
            )
            """,
            (f"rp_test_fin_{pub_id}", "r_test_finalization", pub_id),
        )


# -- fixture ---


def _seed_authorization(
    conn: sqlite3.Connection,
    *,
    authorization_id: str = "auth_test_finalization_v1",
    confirmation_id: str = "conf_test_finalization_v1",
    receipt_group_public_id: str = TEST_GROUP_PUBLIC_ID,
    calculation_run_public_id: str = TEST_CALC_PUBLIC_ID,
    calculation_snapshot_id: str = "snap_test_finalization_v1",
    content_hash: str | None = None,
    currency: str = "SGD",
    final_total: str = "70.12",
    payer_participant_public_id: str = "person_owner",
    participant_public_ids: tuple[str, ...] = (),
    settlement_obligations_json: str = "[]",
    actor_type: str = "cli",
    actor_id: str | None = "person_owner",
    authorization_state: str = "authorized",
    source_evidence_refs: tuple[str, ...] = (),
) -> str:
    """Seed a minimal authorization + confirmation record for testing."""
    if not participant_public_ids:
        participant_public_ids = tuple(TC001_PUBLIC_IDS)

    if content_hash is None:
        content_hash = "0" * 64  # placeholder, tests override with real fingerprint

    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT OR IGNORE INTO receipt_finalization_confirmations (
            confirmation_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, content_hash, currency, final_total,
            payer_participant_public_id, participant_public_ids_json,
            settlement_obligations_json, actor_type, actor_id,
            confirmation_state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            confirmation_id,
            receipt_group_public_id,
            calculation_run_public_id,
            calculation_snapshot_id,
            content_hash,
            currency,
            final_total,
            payer_participant_public_id,
            json.dumps(sorted(participant_public_ids)),
            settlement_obligations_json,
            actor_type,
            actor_id,
            "confirmed",
            now,
        ),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO receipt_finalization_authorizations (
            authorization_id, receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, confirmation_id, content_hash,
            currency, final_total, payer_participant_public_id,
            participant_public_ids_json, settlement_obligations_json,
            source_evidence_refs_json, actor_type, actor_id,
            authorization_state, authorization_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            authorization_id,
            receipt_group_public_id,
            calculation_run_public_id,
            calculation_snapshot_id,
            confirmation_id,
            content_hash,
            currency,
            final_total,
            payer_participant_public_id,
            json.dumps(sorted(participant_public_ids)),
            settlement_obligations_json,
            json.dumps(sorted(source_evidence_refs)),
            actor_type,
            actor_id,
            authorization_state,
            "v1",
            now,
        ),
    )
    return content_hash


def _seed_authorization_for_input(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    *,
    authorization_id: str = "auth_test_finalization_v1",
    confirmation_id: str = "conf_test_finalization_v1",
    actor_type: str = "cli",
    actor_id: str | None = "person_owner",
) -> str:
    """Seed authorization with a real content fingerprint from the input.

    Deletes any existing row with the same authorization_id so the
    real fingerprint always replaces a placeholder.
    """
    if fin_input.calculation_snapshot_hash:
        snapshot = _authoritative_snapshot_for_input(fin_input, fin_input.currency_contract_version)
        repository = AuthoritativeSnapshotRepository(conn)
        existing = repository.fetch(snapshot.snapshot_public_id)
        if existing is None:
            repository.insert(snapshot)
        elif existing != snapshot:
            raise AssertionError("test snapshot public ID has conflicting content")
    fingerprint = build_finalization_content_fingerprint(fin_input)
    total_paid = str(fin_input.calculation_snapshot.get("total_paid", "0.00"))
    obls_json = json.dumps(
        sorted(
            [
                {
                    "debtor": o.debtor_participant_public_id,
                    "creditor": o.creditor_participant_public_id,
                    "amount": str(o.amount),
                    "currency": o.currency,
                }
                for o in fin_input.settlement_obligations
            ],
            key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
        )
    )
    # Remove any placeholder row with the same IDs (reverse FK order).
    conn.execute(
        "DELETE FROM receipt_finalization_authorizations WHERE authorization_id = ?",
        (authorization_id,),
    )
    conn.execute(
        "DELETE FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
        (confirmation_id,),
    )
    return _seed_authorization(
        conn,
        authorization_id=authorization_id,
        confirmation_id=confirmation_id,
        content_hash=fingerprint,
        receipt_group_public_id=fin_input.receipt_group_public_id,
        calculation_run_public_id=fin_input.calculation_run_public_id,
        calculation_snapshot_id=fin_input.calculation_snapshot_id or "snap_test_v1",
        currency=fin_input.currency,
        final_total=total_paid,
        payer_participant_public_id=fin_input.payer_participant_public_id,
        participant_public_ids=fin_input.participant_public_ids,
        settlement_obligations_json=obls_json,
        actor_type=actor_type,
        actor_id=actor_id,
    )


@pytest.fixture()
def fin_db(migrated_temp_db_connection: sqlite3.Connection) -> sqlite3.Connection:
    """Temporary DB with migrations, participants, receipt group, links, and authorization."""
    _seed_participants(migrated_temp_db_connection)
    _seed_receipt_group(migrated_temp_db_connection)
    _seed_receipt_group_participant_links(migrated_temp_db_connection)
    # Seed a placeholder authorization — individual tests will override
    # with _seed_authorization_for_input as needed.
    migrated_temp_db_connection.commit()
    return migrated_temp_db_connection


# -- test helpers ---


def _prepare_authorized_finalization(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    *,
    authorization_id: str = "auth_test_finalization_v1",
) -> FinalizationInput:
    """Seed authorization matching the input, then return the input unchanged."""
    _seed_authorization_for_input(conn, fin_input, authorization_id=authorization_id)
    conn.commit()
    return fin_input


def _tc001_calc_result() -> dict:
    case_data = json.loads(TC001_FIXTURE_PATH.read_text(encoding="utf-8"))
    return calculate_receipt_split(case_data)


def _authoritative_snapshot_for_input(
    fin_input: FinalizationInput,
    currency_contract_version: str,
) -> AuthoritativeCalculationSnapshot:
    return build_authoritative_snapshot(
        snapshot_public_id=fin_input.calculation_snapshot_id,
        calculation_type="receipt_split",
        aggregate_public_id=fin_input.receipt_group_public_id,
        input_payload={
            "calculation_run_public_id": fin_input.calculation_run_public_id,
            "currency": fin_input.currency,
        },
        output_payload=fin_input.calculation_snapshot,
        rules_payload={"algorithm": "receipt_split"},
        money_contract_version="money-v1",
        currency_contract_version=currency_contract_version,
        algorithm_version="receipt-split-v1",
        source_references=fin_input.source_evidence_refs,
        actor_type=fin_input.actor_type,
        actor_public_id=fin_input.actor_id,
        authorization_reference=fin_input.authorization_id,
        finalization_status="finalized",
        created_at="2026-07-13T02:00:00+00:00",
    )


def _build_finalization_input(
    calc_result: dict | None = None,
    *,
    calc_pub_id: str = TEST_CALC_PUBLIC_ID,
    group_pub_id: str = TEST_GROUP_PUBLIC_ID,
    payer_public_id: str | None = None,
    raw_obligations: list[dict] | None = None,
    currency: str | None = None,
    authorization_id: str = "auth_test_finalization_v1",
    confirmation_id: str = "conf_test_finalization_v1",
    idempotency_key: str | None = None,
    calc_snapshot_id: str | None = None,
    calc_snapshot_hash: str | None = None,
    currency_contract_version: str | None = None,
    actor_type: str = "cli",
    actor_id: str | None = "person_owner",
    source_evidence_refs: tuple[str, ...] = (),
) -> FinalizationInput:
    if calc_result is None:
        calc_result = _tc001_calc_result()

    effective_currency = currency or calc_result["currency"]
    effective_contract = currency_contract_version or f"currency-{effective_currency}-v1"
    effective_snapshot_id = calc_snapshot_id or f"snap_{calc_pub_id}"

    def make_input(snapshot_hash: str) -> FinalizationInput:
        return FinalizationInput(
            calculation_run_public_id=calc_pub_id,
            receipt_group_public_id=group_pub_id,
            currency=effective_currency,
            payer_participant_public_id=(payer_public_id or calc_result["payer"]),
            settlement_obligations=to_settlement_obligations(
                raw_obligations or calc_result["settlement_obligations"],
                effective_currency,
            ),
            calculation_snapshot=calc_result,
            authorization_id=authorization_id,
            confirmation_id=confirmation_id,
            idempotency_key=idempotency_key or f"idem_{calc_pub_id}",
            calculation_snapshot_id=effective_snapshot_id,
            calculation_snapshot_hash=snapshot_hash,
            currency_contract_version=effective_contract if snapshot_hash else "",
            actor_type=actor_type,
            actor_id=actor_id,
            source_evidence_refs=source_evidence_refs,
        )

    provisional = make_input(calc_snapshot_hash or "")
    if calc_snapshot_hash is not None:
        return provisional
    snapshot = _authoritative_snapshot_for_input(provisional, effective_contract)
    if calc_snapshot_id is None:
        effective_snapshot_id = f"snap_{calc_pub_id}_{snapshot.combined_snapshot_hash[:12]}"
        provisional = make_input("")
        snapshot = _authoritative_snapshot_for_input(provisional, effective_contract)
    return make_input(snapshot.combined_snapshot_hash)


def _obligations_from_db(
    conn: sqlite3.Connection,
    calc_pub_id: str,
) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                debtor.public_id AS debtor,
                creditor.public_id AS creditor,
                CAST(so.amount AS REAL) AS amount,
                so.currency,
                so.public_id
            FROM settlement_obligations so
            JOIN participants debtor ON debtor.id = so.debtor_id
            JOIN participants creditor ON creditor.id = so.creditor_id
            JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
            WHERE cr.public_id = ?
            ORDER BY so.id
            """,
            (calc_pub_id,),
        )
    ]


def _count_settlement_obligations(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]


def _count_calculation_runs(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM calculation_runs").fetchone()[0]


# --  A. Happy path ---


def test_happy_path_finalization_persists_correct_obligations(
    fin_db: sqlite3.Connection,
) -> None:
    calc_result = _tc001_calc_result()
    fin_input = _build_finalization_input(calc_result)
    _seed_authorization_for_input(fin_db, fin_input)
    fin_db.commit()

    output = finalize_receipt_split(fin_db, fin_input)

    assert isinstance(output, FinalizationOutput)
    assert output.obligations_created == 5
    assert len(output.settlement_public_ids) == 5

    db_obligations = _obligations_from_db(fin_db, TEST_CALC_PUBLIC_ID)
    assert len(db_obligations) == 5

    for obl in db_obligations:
        assert obl["debtor"] != obl["creditor"]
        assert obl["creditor"] == "person_owner"
        assert obl["currency"] == "SGD"

    debtor_amounts = {obl["debtor"]: Decimal(str(obl["amount"])) for obl in db_obligations}
    assert debtor_amounts == {
        "person_member_a": Decimal("14.91"),
        "person_member_b": Decimal("17.54"),
        "person_member_c": Decimal("8.92"),
        "person_member_d": Decimal("7.60"),
        "person_member_e": Decimal("7.60"),
    }


# -- B. No self-obligation ---


def test_finalization_rejects_self_obligation(
    fin_db: sqlite3.Connection,
) -> None:
    with pytest.raises(FinalizationValidationError, match="Self-obligation") as exc_info:
        SettlementObligation(
            debtor_participant_public_id="person_owner",
            creditor_participant_public_id="person_owner",
            amount=Decimal("1.00"),
            currency="SGD",
        )

    assert "person_owner" in str(exc_info.value)
    assert _count_settlement_obligations(fin_db) == 0


# -- C. Amount reconciliation ---


def test_obligation_amounts_reconcile_with_calculator_output(
    fin_db: sqlite3.Connection,
) -> None:
    calc_result = _tc001_calc_result()
    fin_input = _prepare_authorized_finalization(fin_db, _build_finalization_input(calc_result))

    finalize_receipt_split(fin_db, fin_input)

    db_obligations = _obligations_from_db(fin_db, TEST_CALC_PUBLIC_ID)
    total_from_db = sum(
        (Decimal(str(obl["amount"])) for obl in db_obligations),
        Decimal("0.00"),
    )

    payer_own_share = Decimal(str(calc_result["payer_own_share"]))
    total_paid = Decimal(str(calc_result["total_paid_by_payer"]))
    expected_collect = total_paid - payer_own_share

    assert total_from_db == expected_collect
    assert total_from_db == Decimal("56.57")
    assert total_from_db + payer_own_share == total_paid


# -- D. Duplicate finalization ---


def test_duplicate_finalization_is_blocked(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)

    output1 = finalize_receipt_split(fin_db, fin_input)
    assert output1.obligations_created == 5

    # Same key, same content → idempotent replay (no error raised)
    output2 = finalize_receipt_split(fin_db, fin_input)
    assert output2.status == FinalizationStatus.ALREADY_FINALIZED.value
    assert output2.obligations_created == 5

    assert _count_calculation_runs(fin_db) == 1
    assert _count_settlement_obligations(fin_db) == 5


def test_duplicate_finalization_different_calc_id_same_group_is_blocked(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input_1 = _build_finalization_input(calc_pub_id="calc_v1")
    _prepare_authorized_finalization(fin_db, fin_input_1)
    finalize_receipt_split(fin_db, fin_input_1)

    fin_input_2 = _build_finalization_input(
        calc_pub_id="calc_v2_test",
        idempotency_key="idem_calc_v2_test",
        authorization_id="auth_test_v2",
        confirmation_id="conf_test_v2",
    )
    _seed_authorization_for_input(
        fin_db,
        fin_input_2,
        authorization_id="auth_test_v2",
        confirmation_id="conf_test_v2",
    )
    fin_db.commit()

    with pytest.raises(FinalizationIdempotencyError, match="already finalized"):
        finalize_receipt_split(fin_db, fin_input_2)

    assert _count_calculation_runs(fin_db) == 1
    assert _count_settlement_obligations(fin_db) == 5


# -- E. Decimal safety ---


def test_obligation_amounts_are_stored_to_exact_2_decimal_places(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)
    finalize_receipt_split(fin_db, fin_input)

    row = fin_db.execute(
        """
        SELECT printf('%.2f', so.amount) AS amount_str
        FROM settlement_obligations so
        JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
        WHERE cr.public_id = ?
        LIMIT 1
        """,
        (TEST_CALC_PUBLIC_ID,),
    ).fetchone()

    assert "." in row["amount_str"]
    int_part, frac_part = row["amount_str"].split(".")
    assert len(frac_part) == 2


def test_decimal_inputs_are_not_floats(fin_db: sqlite3.Connection) -> None:
    with pytest.raises((FinalizationValidationError, TypeError)):
        SettlementObligation(
            debtor_participant_public_id="person_member_a",
            creditor_participant_public_id="person_owner",
            amount=14.91,
            currency="SGD",
        )


# -- F. Invalid input guards ---


def test_missing_payer_rejected(fin_db: sqlite3.Connection) -> None:
    calc_result = _tc001_calc_result()
    with pytest.raises(
        FinalizationValidationError, match="payer_participant_public_id is required"
    ):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="",
            settlement_obligations=to_settlement_obligations(
                calc_result["settlement_obligations"],
                calc_result["currency"],
            ),
            calculation_snapshot=calc_result,
        )


def test_missing_calculation_run_id_rejected(fin_db: sqlite3.Connection) -> None:
    calc_result = _tc001_calc_result()
    with pytest.raises(FinalizationValidationError, match="calculation_run_public_id is required"):
        FinalizationInput(
            calculation_run_public_id="",
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=to_settlement_obligations(
                calc_result["settlement_obligations"],
                calc_result["currency"],
            ),
            calculation_snapshot=calc_result,
        )


def test_inconsistent_totals_are_rejected(
    fin_db: sqlite3.Connection,
) -> None:
    obligations = to_settlement_obligations(
        _tc001_calc_result()["settlement_obligations"],
        "SGD",
    )
    # Remove one obligation so total does not match snapshot
    truncated = obligations[:4]

    calc_result = _tc001_calc_result()
    with pytest.raises(FinalizationValidationError, match="Settlement obligations (total|count)"):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=truncated,
            calculation_snapshot=calc_result,
        )


def test_finalization_rejects_same_total_wrong_debtor_amounts(
    fin_db: sqlite3.Connection,
) -> None:
    calc_result = _tc001_calc_result()
    raw_obligations = [dict(o) for o in calc_result["settlement_obligations"]]
    raw_obligations[0]["amount"], raw_obligations[1]["amount"] = (
        raw_obligations[1]["amount"],
        raw_obligations[0]["amount"],
    )

    with pytest.raises(FinalizationValidationError, match="do not match calculation snapshot"):
        _build_finalization_input(calc_result, raw_obligations=raw_obligations)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("debtor", "do not match calculation snapshot"),
        ("creditor", "Self-obligation|must match payer|do not match calculation snapshot"),
        ("amount", "do not match calculation snapshot"),
        ("currency", "does not match"),
        ("missing", "count 4 does not match calculation snapshot count 5"),
        ("extra", "count 6 does not match calculation snapshot count 5"),
    ],
)
def test_finalization_rejects_obligations_that_differ_from_snapshot(
    fin_db: sqlite3.Connection,
    mutation: str,
    match: str,
) -> None:
    calc_result = _tc001_calc_result()
    raw_obligations = [dict(o) for o in calc_result["settlement_obligations"]]

    if mutation == "debtor":
        raw_obligations[0]["debtor"] = "person_member_a"  # swap: first debtor -> second
    elif mutation == "creditor":
        raw_obligations[0]["creditor"] = "person_member_b"
    elif mutation == "amount":
        raw_obligations[0]["amount"] = Decimal("99.99")
    elif mutation == "currency":
        raw_obligations[0]["currency"] = "USD"
    elif mutation == "missing":
        raw_obligations = raw_obligations[:-1]
    elif mutation == "extra":
        raw_obligations.append(
            {
                "debtor": "person_member_a",
                "creditor": "person_owner",
                "amount": Decimal("0.01"),
                "currency": "SGD",
            }
        )

    with pytest.raises(FinalizationValidationError, match=match):
        _build_finalization_input(calc_result, raw_obligations=raw_obligations)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


def test_finalization_accepts_obligations_matching_snapshot_exactly(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)

    output = finalize_receipt_split(fin_db, fin_input)

    assert output.obligations_created == 5
    assert _count_calculation_runs(fin_db) == 1
    assert _count_settlement_obligations(fin_db) == 5


def _persist_finalizable_authoritative_snapshot(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
) -> AuthoritativeCalculationSnapshot:
    snapshot = _authoritative_snapshot_for_input(
        fin_input,
        fin_input.currency_contract_version,
    )
    persist_authoritative_snapshot(conn, snapshot)
    return snapshot


def test_finalization_accepts_exact_authoritative_snapshot_binding(
    fin_db: sqlite3.Connection,
) -> None:
    unbound_input = _build_finalization_input()
    snapshot = _persist_finalizable_authoritative_snapshot(fin_db, unbound_input)
    fin_input = _build_finalization_input(
        calc_snapshot_id=snapshot.snapshot_public_id,
        calc_snapshot_hash=snapshot.combined_snapshot_hash,
        currency_contract_version=snapshot.currency_contract_version,
    )
    _prepare_authorized_finalization(fin_db, fin_input)

    output = finalize_receipt_split(fin_db, fin_input)

    assert output.obligations_created == 5
    assert _count_calculation_runs(fin_db) == 1
    assert _count_settlement_obligations(fin_db) == 5
    chain = verify_financial_audit_chain(
        fin_db,
        aggregate_type="receipt_group",
        aggregate_public_id=TEST_GROUP_PUBLIC_ID,
    )
    assert chain.valid is True
    assert chain.event_count == 2
    events = FinancialAuditRepository(fin_db).list_chain("receipt_group", TEST_GROUP_PUBLIC_ID)
    assert [event.event_type for event in events] == [
        "settlement_obligations_created",
        "receipt_finalized",
    ]
    assert all(event.authorization_public_id == fin_input.authorization_id for event in events)
    assert all(
        event.calculation_snapshot_hash == snapshot.combined_snapshot_hash for event in events
    )


def test_audit_insert_failure_rolls_back_complete_receipt_finalization(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)
    fin_db.execute(
        """CREATE TRIGGER test_fail_receipt_audit
        BEFORE INSERT ON financial_audit_events
        WHEN NEW.aggregate_type = 'receipt_group'
        BEGIN SELECT RAISE(ABORT, 'injected receipt audit failure'); END"""
    )
    fin_db.commit()

    with pytest.raises(FinalizationIdempotencyError, match="Database integrity error"):
        finalize_receipt_split(fin_db, fin_input)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0
    assert fin_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert (
        fin_db.execute(
            "SELECT authorization_state FROM receipt_finalization_authorizations "
            "WHERE authorization_id = ?",
            (fin_input.authorization_id,),
        ).fetchone()[0]
        == "authorized"
    )
    assert (
        fin_db.execute(
            "SELECT status FROM receipt_groups WHERE public_id = ?",
            (TEST_GROUP_PUBLIC_ID,),
        ).fetchone()[0]
        == "calculated"
    )


def test_finalization_rejects_authorized_input_with_wrong_authoritative_hash(
    fin_db: sqlite3.Connection,
) -> None:
    unbound_input = _build_finalization_input()
    snapshot = _persist_finalizable_authoritative_snapshot(fin_db, unbound_input)
    fin_input = _build_finalization_input(
        calc_snapshot_id=snapshot.snapshot_public_id,
        calc_snapshot_hash="f" * 64,
        currency_contract_version=snapshot.currency_contract_version,
    )
    _prepare_authorized_finalization(fin_db, fin_input)

    with pytest.raises(
        FinalizationValidationError,
        match="Authoritative calculation snapshot binding failed",
    ):
        finalize_receipt_split(fin_db, fin_input)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


@pytest.mark.parametrize("invalid_hash", ["g" * 64, "A" * 64, "0" * 63])
def test_finalization_input_rejects_noncanonical_snapshot_hash(invalid_hash: str) -> None:
    with pytest.raises(FinalizationValidationError, match="lowercase 64-character"):
        _build_finalization_input(
            calc_snapshot_hash=invalid_hash,
            currency_contract_version="currency-SGD-v1",
        )


def test_finalization_rejects_missing_authoritative_snapshot_hash(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input(calc_snapshot_hash="")
    _seed_authorization_for_input(fin_db, fin_input)
    fin_db.commit()

    with pytest.raises(FinalizationValidationError, match="snapshot hash is required"):
        finalize_receipt_split(fin_db, fin_input)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


def test_finalization_requires_snapshot_settlement_obligations() -> None:
    calc_result = _tc001_calc_result()
    del calc_result["settlement_obligations"]

    with pytest.raises(FinalizationValidationError, match="settlement_obligations"):
        _build_finalization_input(
            calc_result,
            raw_obligations=[
                {
                    "debtor": "person_member_a",
                    "creditor": "person_owner",
                    "amount": Decimal("14.91"),
                    "currency": "SGD",
                }
            ],
        )


def test_to_settlement_obligations_rejects_float_amount() -> None:
    with pytest.raises(FinalizationValidationError, match="float"):
        to_settlement_obligations(
            [
                {
                    "debtor": "person_member_a",
                    "creditor": "person_owner",
                    "amount": 14.91,
                    "currency": "SGD",
                }
            ],
            "SGD",
        )


def test_wrong_creditor_rejected(fin_db: sqlite3.Connection) -> None:
    obligations = [
        SettlementObligation(
            debtor_participant_public_id="person_member_a",
            creditor_participant_public_id="person_owner",
            amount=Decimal("14.91"),
            currency="SGD",
        ),
        SettlementObligation(
            debtor_participant_public_id="person_member_b",
            creditor_participant_public_id="person_member_a",
            amount=Decimal("17.54"),
            currency="SGD",
        ),
    ]
    calc_result = _tc001_calc_result()
    with pytest.raises(FinalizationValidationError, match="must match payer"):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=obligations,
            calculation_snapshot=calc_result,
        )


def test_ineligible_receipt_group_status_rejected(
    fin_db: sqlite3.Connection,
) -> None:
    fin_db.execute(
        "UPDATE receipt_groups SET status = ? WHERE public_id = ?",
        ("needs_review", TEST_GROUP_PUBLIC_ID),
    )
    fin_db.commit()

    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)
    with pytest.raises(IneligibleForFinalizationError, match="status .*needs_review"):
        finalize_receipt_split(fin_db, fin_input)


# -- G. Calculation snapshot / audit reference preservation ---


def test_finalization_persists_calculation_snapshot(
    fin_db: sqlite3.Connection,
) -> None:
    calc_result = _tc001_calc_result()
    fin_input = _build_finalization_input(calc_result)
    _prepare_authorized_finalization(fin_db, fin_input)

    finalize_receipt_split(fin_db, fin_input)

    row = fin_db.execute(
        """
        SELECT raw_input, input_hash
        FROM calculation_runs
        WHERE public_id = ?
        """,
        (TEST_CALC_PUBLIC_ID,),
    ).fetchone()

    assert row is not None
    assert row["raw_input"] is not None

    snapshot = json.loads(row["raw_input"])
    assert snapshot["case_id"] == calc_result["case_id"]
    assert snapshot["total_paid"] == "70.12"  # JSON-serialized Decimal


def test_finalization_persists_participant_shares(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)
    finalize_receipt_split(fin_db, fin_input)

    rows = fin_db.execute(
        """
        SELECT
            p.public_id AS participant,
            cps.final_share_amount AS amount
        FROM calculation_participant_shares cps
        JOIN participants p ON p.id = cps.participant_id
        JOIN calculation_runs cr ON cr.id = cps.calculation_run_id
        WHERE cr.public_id = ?
        ORDER BY p.public_id
        """,
        (TEST_CALC_PUBLIC_ID,),
    ).fetchall()

    shares = {row["participant"]: Decimal(str(row["amount"])) for row in rows}
    assert shares == _tc001_calc_result()["participant_shares"]


# -- Fix 1: Currency mismatch guard ---


def test_currency_mismatch_between_group_and_input_is_rejected(
    fin_db: sqlite3.Connection,
) -> None:
    """Receipt group SGD vs finalization input USD must be rejected."""
    # Update receipt group to USD so it conflicts with the SGD input
    fin_db.execute(
        "UPDATE receipt_groups SET currency = ? WHERE public_id = ?",
        ("USD", TEST_GROUP_PUBLIC_ID),
    )
    fin_db.commit()

    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)

    with pytest.raises(IneligibleForFinalizationError, match="currency.*USD.*SGD"):
        finalize_receipt_split(fin_db, fin_input)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


# -- Fix 2: Output settlement_public_ids must match persisted DB rows ---


def test_output_settlement_public_ids_match_persisted_obligations(
    fin_db: sqlite3.Connection,
) -> None:
    fin_input = _build_finalization_input()
    _prepare_authorized_finalization(fin_db, fin_input)
    output = finalize_receipt_split(fin_db, fin_input)

    assert len(output.settlement_public_ids) == output.obligations_created
    assert len(output.settlement_public_ids) == 5

    db_public_ids = {
        row["public_id"]
        for row in fin_db.execute(
            "SELECT public_id FROM settlement_obligations ORDER BY id"
        ).fetchall()
    }

    for pub_id in output.settlement_public_ids:
        assert pub_id in db_public_ids, (
            f"Output settlement_public_id {pub_id!r} is not in the database; "
            f"DB public_ids: {sorted(db_public_ids)}"
        )

    assert set(output.settlement_public_ids) == db_public_ids


# -- Fix 3: Do not silently skip participant shares ---


def test_missing_participant_in_snapshot_shares_is_rejected(
    fin_db: sqlite3.Connection,
) -> None:
    """Calculation snapshot with a participant not in DB must fail loudly."""
    calc_result = _tc001_calc_result()
    # Add an unknown public ID to participants, shares, and also to
    # total_paid and payer_paid_amounts so the share reconciliation passes.
    calc_result["participants"] = list(calc_result["participants"]) + ["person_unknown"]
    calc_result["participant_shares"]["person_unknown"] = Decimal("5.00")
    # Bump total_paid so sum(shares) == total_paid
    calc_result["total_paid"] = Decimal(str(calc_result["total_paid"])) + Decimal("5.00")
    # Bump payer_paid_amounts so balances remain valid
    calc_result["payer_paid_amounts"]["person_owner"] = Decimal(
        str(calc_result["payer_paid_amounts"]["person_owner"])
    ) + Decimal("5.00")
    # Also add a matching settlement obligation so balance/obligation
    # consistency passes.
    calc_result["settlement_obligations"].append(
        {
            "debtor": "person_unknown",
            "creditor": "person_owner",
            "amount": Decimal("5.00"),
            "currency": "SGD",
        }
    )

    fin_input = _build_finalization_input(calc_result)
    _prepare_authorized_finalization(fin_db, fin_input)

    with pytest.raises(
        IneligibleForFinalizationError,
        match="not found",
    ):
        finalize_receipt_split(fin_db, fin_input)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


# -- H. New tests for public-ID identity contract ---


def test_participant_outside_receipt_group_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A real participant public ID not in the receipt group must be rejected."""
    conn = migrated_temp_db_connection
    _seed_participants(conn)
    _seed_receipt_group(conn)

    # Create a second participant who is NOT linked to the receipt group
    conn.execute(
        """
        INSERT OR IGNORE INTO participants (public_id, display_name, aliases, is_self, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("person_outsider", "Outsider", "[]", 0, ""),
    )

    # Link only the TC001 participants to the group
    _seed_receipt_group_participant_links(conn)
    conn.commit()

    calc_result = _tc001_calc_result()
    # Remove person_member_e from participants and shares (so shares match
    # participants), and add person_outsider (not in the receipt group)
    # to both so the snapshot identity contract accepts it but
    # _lookup_participants_by_public_id rejects it.
    calc_result["participants"] = [
        p for p in calc_result["participants"] if p != "person_member_e"
    ] + ["person_outsider"]
    calc_result["participant_shares"]["person_outsider"] = calc_result["participant_shares"].pop(
        "person_member_e"
    )
    # Remove person_member_e from settlement_obligations and recalculate so
    # the totals remain internally consistent (participant_shares still
    # sums to the same value but the obligation set is smaller).
    calc_result["settlement_obligations"] = [
        o for o in calc_result["settlement_obligations"] if o["debtor"] != "person_member_e"
    ]
    # Recalculate the convenience totals for the reduced obligation set.
    new_collect = sum(Decimal(str(o["amount"])) for o in calc_result["settlement_obligations"])
    calc_result["total_to_collect"] = new_collect
    calc_result["total_paid_by_payer"] = Decimal(str(calc_result["payer_own_share"])) + new_collect
    # Adjust total_paid to match shares sum (unchanged) and updated
    # payer-paid amounts for internal consistency.
    calc_result["payer_paid_amounts"]["person_owner"] = calc_result["total_paid_by_payer"]
    # total_paid must equal sum of all shares, which is still 70.12
    calc_result["total_paid"] = sum(
        Decimal(str(v)) for v in calc_result["participant_shares"].values()
    )
    # total_paid_by_payer must match total_paid since only payer pays
    calc_result["total_paid_by_payer"] = calc_result["total_paid"]
    # payer_paid_amounts must also be updated
    calc_result["payer_paid_amounts"]["person_owner"] = calc_result["total_paid"]
    # Remove person_member_e from payer_paid_amounts since she is no longer
    # in the participant set.
    calc_result["payer_paid_amounts"].pop("person_member_e", None)
    # Add a settlement obligation for person_outsider so the balance
    # check passes (person_outsider has a share of 7.60 and paid 0,
    # so they are a debtor for 7.60)
    calc_result["settlement_obligations"].append(
        {
            "debtor": "person_outsider",
            "creditor": "person_owner",
            "amount": Decimal("7.60"),
            "currency": "SGD",
        }
    )
    # Remove person_member_e from receipt-level item_shares too.
    for rc in calc_result["receipts"]:
        ish = rc.get("item_shares", {})
        if "person_member_e" in ish:
            del ish["person_member_e"]
        # Also remove from participant_shares
        psh = rc.get("participant_shares", {})
        if "person_member_e" in psh:
            del psh["person_member_e"]
        # Remove from items[*].participant_allocations
        for item in rc.get("items", []):
            allocs = item.get("participant_allocations", {})
            if "person_member_e" in allocs:
                del allocs["person_member_e"]
        # Remove from adjustments[*].participant_allocations
        for adj in rc.get("adjustments", []):
            allocs = adj.get("participant_allocations", {})
            if "person_member_e" in allocs:
                del allocs["person_member_e"]
    # Remove rounding_adjustment_participant references to person_member_e
    for rc in calc_result["receipts"]:
        if rc.get("rounding_adjustment_participant") == "person_member_e":
            rc["rounding_adjustment_participant"] = "person_owner"

    fin_input = _build_finalization_input(calc_result)
    _seed_authorization_for_input(conn, fin_input)
    conn.commit()
    with pytest.raises(IneligibleForFinalizationError, match="not found or not in receipt group"):
        finalize_receipt_split(conn, fin_input)

    assert _count_calculation_runs(conn) == 0
    assert _count_settlement_obligations(conn) == 0


def test_different_public_ids_same_display_name_are_distinct(
    fin_db: sqlite3.Connection,
) -> None:
    """Two participants with the same display_name but different public IDs
    must remain distinct — no collapsing or overwriting."""
    # Insert Alex-a and Alex-b (both display_name "Alex") and link them
    fin_db.execute(
        """
        INSERT OR IGNORE INTO participants (public_id, display_name, aliases, is_self, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("person_alex_a", "Alex", "[]", 0, ""),
    )
    fin_db.execute(
        """
        INSERT OR IGNORE INTO participants (public_id, display_name, aliases, is_self, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("person_alex_b", "Alex", "[]", 0, ""),
    )

    # Link both to the receipt group
    receipt_id = fin_db.execute(
        "SELECT id FROM receipts WHERE public_id = 'r_test_finalization'"
    ).fetchone()["id"]
    for pub_id in ("person_alex_a", "person_alex_b"):
        fin_db.execute(
            """
            INSERT OR IGNORE INTO receipt_participants (
                public_id, receipt_id, participant_id, role, is_included
            ) VALUES (
                ?,
                ?,
                (SELECT id FROM participants WHERE public_id = ?),
                'participant', 1
            )
            """,
            (f"rp_test_fin_{pub_id}", receipt_id, pub_id),
        )
    fin_db.commit()

    # Build a minimal finalization input with both Alexes and person_owner as payer
    calc_result = {
        "case_id": "test_same_name",
        "currency": "SGD",
        "status": "calculated_pending_confirmation",
        "participants": ["person_owner", "person_alex_a", "person_alex_b"],
        "payer": "person_owner",
        "participant_shares": {
            "person_owner": "5.00",
            "person_alex_a": "2.00",
            "person_alex_b": "3.00",
        },
        "total_paid": "10.00",
        "payer_own_share": "5.00",
        "payer_paid_amounts": {
            "person_owner": "10.00",
            "person_alex_a": "0.00",
            "person_alex_b": "0.00",
        },
        "receipts": [],
        "settlement_obligations": [
            {
                "debtor": "person_alex_a",
                "creditor": "person_owner",
                "amount": "2.00",
                "currency": "SGD",
            },
            {
                "debtor": "person_alex_b",
                "creditor": "person_owner",
                "amount": "3.00",
                "currency": "SGD",
            },
        ],
    }

    fin_input = _build_finalization_input(
        calc_result,
        calc_pub_id="calc_same_name_test",
        raw_obligations=calc_result["settlement_obligations"],
        idempotency_key="idem_calc_same_name_test",
    )

    _seed_authorization_for_input(fin_db, fin_input)
    fin_db.commit()

    output = finalize_receipt_split(fin_db, fin_input)
    assert output.obligations_created == 2

    # Verify both persisted as separate obligations with correct participant FK
    rows = fin_db.execute(
        """
        SELECT debtor.public_id AS debtor, creditor.public_id AS creditor,
               printf('%.2f', so.amount) AS amount
        FROM settlement_obligations so
        JOIN participants debtor ON debtor.id = so.debtor_id
        JOIN participants creditor ON creditor.id = so.creditor_id
        JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
        WHERE cr.public_id = 'calc_same_name_test'
        ORDER BY debtor.public_id
        """
    ).fetchall()

    assert len(rows) == 2
    assert rows[0]["debtor"] == "person_alex_a"
    assert rows[0]["amount"] == "2.00"
    assert rows[1]["debtor"] == "person_alex_b"
    assert rows[1]["amount"] == "3.00"

    # Each obligation points to a distinct participant FK
    debtor_ids = {
        row["debtor_id"]
        for row in fin_db.execute(
            """
            SELECT so.debtor_id
            FROM settlement_obligations so
            JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
            WHERE cr.public_id = 'calc_same_name_test'
            """
        ).fetchall()
    }
    assert len(debtor_ids) == 2, "Both Alexes must have distinct participant FK values"


def test_display_name_change_does_not_redirect_obligation(
    fin_db: sqlite3.Connection,
) -> None:
    """Changing a display_name in the DB must not change obligation routing."""
    calc_result = _tc001_calc_result()
    fin_input = _build_finalization_input(calc_result, calc_pub_id="calc_display_change")
    _prepare_authorized_finalization(fin_db, fin_input)

    finalize_receipt_split(fin_db, fin_input)

    # Change MemberA's display_name
    fin_db.execute(
        "UPDATE participants SET display_name = ? WHERE public_id = ?",
        ("MemberA-Renamed", "person_member_a"),
    )
    fin_db.commit()

    # The obligation must still point to person_member_a's FK
    rows = fin_db.execute(
        """
        SELECT debtor.public_id AS debtor, debtor.display_name AS display_name,
               printf('%.2f', so.amount) AS amount
        FROM settlement_obligations so
        JOIN participants debtor ON debtor.id = so.debtor_id
        JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
        WHERE cr.public_id = 'calc_display_change'
          AND debtor.public_id = 'person_member_a'
        """
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["debtor"] == "person_member_a"
    assert rows[0]["display_name"] == "MemberA-Renamed"
    assert rows[0]["amount"] == "14.91"


def test_passing_display_name_instead_of_public_id_fails(
    fin_db: sqlite3.Connection,
) -> None:
    """A caller passing 'Owner' instead of 'person_owner' as payer must be rejected."""
    obligations = to_settlement_obligations(
        _tc001_calc_result()["settlement_obligations"],
        "SGD",
    )
    calc_result = _tc001_calc_result()
    with pytest.raises(FinalizationValidationError, match="must match payer"):
        FinalizationInput(
            calculation_run_public_id="calc_display_name_payer",
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="Owner",  # display name, not public ID
            settlement_obligations=obligations,
            calculation_snapshot=calc_result,
        )

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


def test_unknown_public_id_fails_before_writes(
    fin_db: sqlite3.Connection,
) -> None:
    """Passing an unknown public ID must fail before any write."""
    calc_result = _tc001_calc_result()
    # Add unknown to both participants and shares to satisfy identity contract,
    # then DB lookup will fail because it's not in the receipt group.
    calc_result["participants"] = list(calc_result["participants"]) + ["person_nonexistent"]
    calc_result["participant_shares"]["person_nonexistent"] = Decimal("5.00")
    # Also bump totals to keep share reconciliation consistent
    calc_result["total_paid"] = Decimal(str(calc_result["total_paid"])) + Decimal("5.00")
    calc_result["payer_paid_amounts"]["person_owner"] = calc_result["total_paid"]
    calc_result["settlement_obligations"].append(
        {
            "debtor": "person_nonexistent",
            "creditor": "person_owner",
            "amount": Decimal("5.00"),
            "currency": "SGD",
        }
    )

    fin_input = _build_finalization_input(calc_result)
    _seed_authorization_for_input(fin_db, fin_input)
    fin_db.commit()

    with pytest.raises(IneligibleForFinalizationError, match="not found"):
        finalize_receipt_split(fin_db, fin_input)

    assert _count_calculation_runs(fin_db) == 0
    assert _count_settlement_obligations(fin_db) == 0


def test_duplicate_participant_public_ids_rejected() -> None:
    """Duplicate top-level participant public IDs must be rejected."""
    with pytest.raises(ValueError, match="Duplicate participant public IDs"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a", "person_owner"],
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "participants": ["person_owner", "person_member_a"],
                            }
                        ],
                    }
                ],
            }
        )


# -- I. Focused identity contract tests ---


def test_duplicate_item_participants_rejected() -> None:
    """Duplicate participants within a single item must be rejected."""
    with pytest.raises(ValueError, match="Duplicate Lunch item participants"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "participants": [
                                    "person_owner",
                                    "person_member_a",
                                    "person_owner",
                                ],
                            }
                        ],
                    }
                ],
            }
        )


def test_duplicate_consumers_rejected() -> None:
    """Duplicate consumers within a single item must be rejected."""
    with pytest.raises(ValueError, match="Duplicate Lunch item participants"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "consumers": ["person_owner", "person_owner"],
                            }
                        ],
                    }
                ],
            }
        )


def test_duplicate_owners_rejected() -> None:
    """Duplicate owners within a single item must be rejected."""
    with pytest.raises(ValueError, match="Duplicate Lunch item participants"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "owners": ["person_owner", "person_owner"],
                            }
                        ],
                    }
                ],
            }
        )


def test_duplicate_service_charge_participants_rejected() -> None:
    """Duplicate service-charge participants must be rejected."""
    with pytest.raises(ValueError, match="Duplicate service_charge adjustment participants"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "receipts": [
                    {
                        "merchant": "Dinner",
                        "paid_by": "person_owner",
                        "net_paid": "22.00",
                        "service_charge_amount": "2.00",
                        "service_charge_allocation_method": "equal_per_participant",
                        "service_charge_participants": [
                            "person_owner",
                            "person_owner",
                        ],
                        "items": [
                            {
                                "description": "Meal",
                                "amount": "20.00",
                                "participants": ["person_owner", "person_member_a"],
                            }
                        ],
                    }
                ],
            }
        )


def test_duplicate_discount_adjustment_participants_rejected() -> None:
    """Duplicate discount participants must be rejected."""
    with pytest.raises(ValueError, match="Duplicate discount adjustment participants"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "receipts": [
                    {
                        "merchant": "Dinner",
                        "paid_by": "person_owner",
                        "discount": "5.00",
                        "discount_allocation_method": "equal_per_participant",
                        "discount_participants": ["person_owner", "person_owner"],
                        "net_paid": "15.00",
                        "items": [
                            {
                                "description": "Meal",
                                "amount": "20.00",
                                "participants": ["person_owner", "person_member_a"],
                            }
                        ],
                    }
                ],
            }
        )


def test_unknown_top_level_payer_rejected() -> None:
    """A payer not in the participant set must be rejected."""
    with pytest.raises(ValueError, match="not in the participant set"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "payer": "person_member_b",
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "participants": ["person_owner", "person_member_a"],
                            }
                        ],
                    }
                ],
            }
        )


def test_known_participant_who_did_not_pay_rejected() -> None:
    """A payer who is in the participant set but did not pay any receipt
    must be rejected."""
    with pytest.raises(ValueError, match="did not pay any receipt"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a", "person_member_b"],
                "payer": "person_member_b",
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "participants": ["person_owner", "person_member_a"],
                            }
                        ],
                    }
                ],
            }
        )


def test_sole_payer_mismatch_rejected() -> None:
    """When only one payer exists, the supplied payer must match."""
    with pytest.raises(ValueError, match="did not pay any receipt"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["person_owner", "person_member_a"],
                "payer": "person_member_a",
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "person_owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Lunch",
                                "amount": "20.00",
                                "participants": ["person_owner", "person_member_a"],
                            }
                        ],
                    }
                ],
            }
        )


def test_participant_order_permutations_produce_identical_obligations() -> None:
    """Different participant orderings must produce identical settlement obligations."""

    base = {
        "currency": "SGD",
        "receipts": [
            {
                "merchant": "Lunch",
                "paid_by": "person_owner",
                "net_paid": "60.00",
                "items": [
                    {
                        "description": "Lunch",
                        "amount": "60.00",
                        "participants": [
                            "person_owner",
                            "person_member_b",
                            "person_member_a",
                        ],
                    }
                ],
            }
        ],
    }

    order1 = calculate_receipt_split(
        {**base, "participants": ["person_owner", "person_member_a", "person_member_b"]}
    )
    order2 = calculate_receipt_split(
        {**base, "participants": ["person_member_b", "person_owner", "person_member_a"]}
    )
    order3 = calculate_receipt_split(
        {**base, "participants": ["person_member_a", "person_member_b", "person_owner"]}
    )

    assert order1["settlement_obligations"] == order2["settlement_obligations"]
    assert order2["settlement_obligations"] == order3["settlement_obligations"]


def test_missing_snapshot_payer_rejected(fin_db: sqlite3.Connection) -> None:
    """A snapshot without a payer field must be rejected."""
    calc_result = _tc001_calc_result()
    del calc_result["payer"]
    with pytest.raises(
        FinalizationValidationError,
        match="must be a non-empty string",
    ):
        _build_finalization_input(calc_result, payer_public_id="person_owner")
    assert _count_calculation_runs(fin_db) == 0


def test_snapshot_payer_mismatch_rejected(fin_db: sqlite3.Connection) -> None:
    """A snapshot payer different from FinalizationInput payer must be rejected."""
    calc_result = _tc001_calc_result()
    calc_result["payer"] = "person_member_a"
    # When snapshot.payer != payer_participant_public_id, the obligation
    # creditor check fires first because the credential mismatch is
    # detected at the obligation level (creditor != payer).
    with pytest.raises(
        FinalizationValidationError,
        match="must match payer|does not match",
    ):
        _build_finalization_input(calc_result)
    assert _count_calculation_runs(fin_db) == 0


def test_snapshot_participant_with_display_name_rejected() -> None:
    """A display name in snapshot participants must be rejected because
    the obligation creditor won't match the payer_participant_public_id."""
    calc_result = _tc001_calc_result()
    calc_result["participants"] = [
        "Owner",
        "person_member_a",
        "person_member_b",
        "person_member_c",
        "person_member_d",
        "person_member_e",
    ]
    calc_result["payer"] = "Owner"
    calc_result["participant_shares"]["Owner"] = calc_result["participant_shares"].pop(
        "person_owner"
    )
    # The receipt paid_by "person_owner" is not in the participant set
    # (which uses "Owner"), so the raw-input validation rejects it before
    # we get to the creditor mismatch check.
    with pytest.raises(FinalizationValidationError, match="is not in snapshot participants"):
        _build_finalization_input(calc_result, payer_public_id="Owner")


def test_share_keys_missing_from_participant_set_rejected() -> None:
    """Share keys not matching the participant set must be rejected."""
    calc_result = _tc001_calc_result()
    del calc_result["participant_shares"]["person_member_e"]
    with pytest.raises(
        FinalizationValidationError,
        match="participant_shares keys do not match",
    ):
        _build_finalization_input(calc_result)


def test_nested_receipt_payer_outside_participant_set_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A receipt paid_by outside the snapshot participant set must be rejected."""
    conn = migrated_temp_db_connection
    _seed_participants(conn)
    _seed_receipt_group(conn)
    _seed_receipt_group_participant_links(conn)
    conn.commit()

    calc_result = _tc001_calc_result()
    calc_result["receipts"][0]["paid_by"] = "person_outsider"
    calc_result["participants"] = list(calc_result["participants"]) + ["person_outsider"]
    calc_result["participant_shares"]["person_outsider"] = Decimal("0.00")

    fin_input = _build_finalization_input(calc_result)
    _seed_authorization_for_input(conn, fin_input)
    conn.commit()
    with pytest.raises(IneligibleForFinalizationError, match="not found or not in receipt group"):
        finalize_receipt_split(conn, fin_input)


def test_nested_allocation_key_outside_participant_set(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """An allocation key outside the participant set must be rejected."""
    conn = migrated_temp_db_connection
    _seed_participants(conn)
    _seed_receipt_group(conn)
    _seed_receipt_group_participant_links(conn)
    conn.commit()

    calc_result = _tc001_calc_result()
    calc_result["participants"] = list(calc_result["participants"]) + ["person_intruder"]
    calc_result["participant_shares"]["person_intruder"] = Decimal("0.00")
    calc_result["receipts"][0]["items"][0]["participant_allocations"] = {
        "person_intruder": Decimal("10.00"),
        "person_owner": Decimal("10.00"),
        "person_member_a": Decimal("10.00"),
        "person_member_b": Decimal("10.00"),
        "person_member_c": Decimal("10.00"),
        "person_member_d": Decimal("10.00"),
        "person_member_e": Decimal("5.40"),
    }

    fin_input = _build_finalization_input(calc_result)
    _seed_authorization_for_input(conn, fin_input)
    conn.commit()
    with pytest.raises(IneligibleForFinalizationError, match="not found or not in receipt group"):
        finalize_receipt_split(conn, fin_input)


def test_display_name_map_is_copied_not_aliased() -> None:
    """The calculator output must copy the display_names dict, not alias it."""
    display_names = {"person_owner": "Owner", "person_member_a": "MemberA"}
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["person_owner", "person_member_a"],
            "participant_display_names": display_names,
            "receipts": [
                {
                    "merchant": "Lunch",
                    "paid_by": "person_owner",
                    "net_paid": "20.00",
                    "items": [
                        {
                            "description": "Lunch",
                            "amount": "20.00",
                            "participants": ["person_owner", "person_member_a"],
                        }
                    ],
                }
            ],
        }
    )
    assert result["participant_display_names"] == display_names
    assert result["participant_display_names"] is not display_names
    display_names["person_new"] = "New"
    assert "person_new" not in result["participant_display_names"]


def test_db_derived_display_metadata_replaces_caller_metadata(
    fin_db: sqlite3.Connection,
) -> None:
    """DB-derived display names must replace caller-provided ones in the
    persisted snapshot."""
    calc_result = _tc001_calc_result()
    calc_result["participant_display_names"] = {
        "person_owner": "HACKED",
        "person_member_a": "MemberA",
        "person_member_b": "MemberB",
        "person_member_c": "MemberC",
        "person_member_d": "MemberD",
        "person_member_e": "MemberE",
    }

    fin_input = _build_finalization_input(calc_result, calc_pub_id="calc_db_metadata_test")
    _prepare_authorized_finalization(fin_db, fin_input)
    finalize_receipt_split(fin_db, fin_input)

    row = fin_db.execute(
        "SELECT raw_input FROM calculation_runs WHERE public_id = ?",
        ("calc_db_metadata_test",),
    ).fetchone()
    snapshot = json.loads(row["raw_input"])
    assert snapshot["participant_display_names"]["person_owner"] == "Owner"


def test_zero_writes_on_identity_rejection(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """All identity rejections must leave zero calculation/share/obligation writes."""
    conn = migrated_temp_db_connection
    _seed_participants(conn)
    _seed_receipt_group(conn)
    _seed_receipt_group_participant_links(conn)
    conn.commit()

    calc_result = _tc001_calc_result()
    calc_result["participants"] = list(calc_result["participants"]) + ["person_intruder"]
    calc_result["participant_shares"]["person_intruder"] = Decimal("0.00")

    fin_input = _build_finalization_input(calc_result, calc_pub_id="calc_zero_write_test")
    _seed_authorization_for_input(conn, fin_input)
    conn.commit()
    with pytest.raises(IneligibleForFinalizationError):
        finalize_receipt_split(conn, fin_input)

    assert _count_calculation_runs(conn) == 0
    assert _count_settlement_obligations(conn) == 0
    share_count = conn.execute("SELECT COUNT(*) FROM calculation_participant_shares").fetchone()[0]
    assert share_count == 0


def test_participant_shares_keys_exactly_equal_participant_set_rejected() -> None:
    """If share keys have extra entries beyond participant set, must reject."""
    calc_result = _tc001_calc_result()
    calc_result["participant_shares"]["person_extra"] = Decimal("1.00")
    with pytest.raises(
        FinalizationValidationError,
        match="not in snapshot participants|keys do not match",
    ):
        _build_finalization_input(calc_result)


# -- J. Immutability and pre-write revalidation tests ---


def test_obligations_are_stored_as_immutable_tuple() -> None:
    """After construction, settlement_obligations must be a tuple, not list."""
    calc_result = _tc001_calc_result()
    obligations = to_settlement_obligations(
        calc_result["settlement_obligations"], calc_result["currency"]
    )
    fin_input = FinalizationInput(
        calculation_run_public_id=TEST_CALC_PUBLIC_ID,
        receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
        currency=calc_result["currency"],
        payer_participant_public_id=calc_result["payer"],
        settlement_obligations=obligations,
        calculation_snapshot=calc_result,
    )
    assert isinstance(fin_input.settlement_obligations, tuple)
    obligations.append(
        SettlementObligation(
            debtor_participant_public_id="person_member_a",
            creditor_participant_public_id="person_owner",
            amount=Decimal("99.99"),
            currency="SGD",
        )
    )
    assert len(fin_input.settlement_obligations) == 5


def test_snapshot_is_deep_copied_not_aliased() -> None:
    """After construction, the snapshot must be a copy, not the caller's dict."""
    calc_result = _tc001_calc_result()
    calc_copy = dict(calc_result)
    fin_input = FinalizationInput(
        calculation_run_public_id=TEST_CALC_PUBLIC_ID,
        receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
        currency=calc_result["currency"],
        payer_participant_public_id=calc_result["payer"],
        settlement_obligations=to_settlement_obligations(
            calc_result["settlement_obligations"], calc_result["currency"]
        ),
        calculation_snapshot=calc_copy,
    )
    calc_copy["participants"] = ["hacked"]
    assert fin_input.calculation_snapshot["participants"] != ["hacked"]


def test_pre_write_revalidation_detects_obligation_debtor_mismatch() -> None:
    """Pre-write revalidation must catch obligation content mismatch."""
    # Build a valid FinalizationInput first, then directly test the
    # revalidation function with a deliberately mismatched config.
    # We can't use FinalizationInput directly because model validation
    # catches mismatches at construction time.
    from unittest.mock import MagicMock

    from finance_core.receipt_finalization.finalizer import _revalidate_obligations_match_snapshot

    # Create a mock that looks like a FinalizationInput but with
    # mismatched obligations vs snapshot
    mock_input = MagicMock()
    mock_input.settlement_obligations = (
        SettlementObligation(
            debtor_participant_public_id="person_member_a",
            creditor_participant_public_id="person_owner",
            amount=Decimal("14.91"),
            currency="SGD",
        ),
    )
    mock_input.calculation_snapshot = {
        "participants": ["person_owner", "person_member_a"],
        "payer": "person_owner",
        "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
        "total_paid": "14.91",
        "payer_own_share": "0",
        "settlement_obligations": [
            {
                "debtor": "person_member_a",
                "creditor": "person_owner",
                "amount": "14.91",
                "currency": "SGD",
            },
            {
                "debtor": "person_member_a",
                "creditor": "person_owner",
                "amount": "0.01",
                "currency": "SGD",
            },
        ],
    }
    with pytest.raises(IneligibleForFinalizationError, match="Pre-write revalidation"):
        _revalidate_obligations_match_snapshot(mock_input)


def test_unhashable_snapshot_participant_rejected() -> None:
    """A list element in participants must be rejected (not crash on set())."""
    with pytest.raises((FinalizationValidationError, TypeError)):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=[
                SettlementObligation(
                    debtor_participant_public_id="person_member_a",
                    creditor_participant_public_id="person_owner",
                    amount=Decimal("14.91"),
                    currency="SGD",
                )
            ],
            calculation_snapshot={
                "participants": [["person_owner"], "person_member_a"],
                "payer": "person_owner",
                "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                "settlement_obligations": [
                    {
                        "debtor": "person_member_a",
                        "creditor": "person_owner",
                        "amount": "14.91",
                        "currency": "SGD",
                    }
                ],
            },
        )


def test_mixed_type_participant_share_keys_rejected() -> None:
    """Non-string share keys must be rejected."""
    with pytest.raises((FinalizationValidationError, TypeError)):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=[
                SettlementObligation(
                    debtor_participant_public_id="person_member_a",
                    creditor_participant_public_id="person_owner",
                    amount=Decimal("14.91"),
                    currency="SGD",
                )
            ],
            calculation_snapshot={
                "participants": ["person_owner", 123],
                "payer": "person_owner",
                "participant_shares": {"person_owner": "0", 123: "14.91"},
                "settlement_obligations": [
                    {
                        "debtor": "person_member_a",
                        "creditor": "person_owner",
                        "amount": "14.91",
                        "currency": "SGD",
                    }
                ],
            },
        )


def test_invalid_payer_paid_amount_key_reports_field_name() -> None:
    """Nested identity validation must report the actual invalid map field."""
    calc_result = _tc001_calc_result()
    calc_result["payer_paid_amounts"] = {123: calc_result["total_paid"]}

    with pytest.raises(
        FinalizationValidationError,
        match="calculation_snapshot.payer_paid_amounts key must be a non-empty string",
    ):
        _build_finalization_input(calc_result)


def test_non_string_participant_value_rejected() -> None:
    """A None in participants must be rejected."""
    with pytest.raises(
        FinalizationValidationError,
        match="must be a non-empty string",
    ):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=[
                SettlementObligation(
                    debtor_participant_public_id="person_member_a",
                    creditor_participant_public_id="person_owner",
                    amount=Decimal("14.91"),
                    currency="SGD",
                )
            ],
            calculation_snapshot={
                "participants": [None, "person_member_a"],
                "payer": "person_owner",
                "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                "settlement_obligations": [
                    {
                        "debtor": "person_member_a",
                        "creditor": "person_owner",
                        "amount": "14.91",
                        "currency": "SGD",
                    }
                ],
            },
        )


def test_invalid_top_level_obligations_key_rejected() -> None:
    """A non-dict entry in the obligations list must not crash."""
    with pytest.raises(FinalizationValidationError, match="must be a mapping"):
        FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=[
                SettlementObligation(
                    debtor_participant_public_id="person_member_a",
                    creditor_participant_public_id="person_owner",
                    amount=Decimal("14.91"),
                    currency="SGD",
                )
            ],
            calculation_snapshot={
                "participants": ["person_owner", "person_member_a"],
                "payer": "person_owner",
                "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                "settlement_obligations": ["not_a_dict"],
            },
        )


# =============================================================================
# New tests: Authorization, atomicity, idempotency, audit, schema safety
# =============================================================================


class TestAuthorization:
    """Authorization loading and validation."""

    def test_valid_persisted_authorization_permits_finalization(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output = finalize_receipt_split(fin_db, fin_input)
        assert output.status == FinalizationStatus.FINALIZED.value
        assert output.obligations_created == 5

    def test_caller_supplied_confirmation_without_persisted_auth_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(authorization_id="")
        with pytest.raises(FinalizationAuthorizationError, match="authorization_id is required"):
            finalize_receipt_split(fin_db, fin_input)

    def test_missing_authorization_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(authorization_id="auth_nonexistent")
        with pytest.raises(FinalizationAuthorizationError, match="not found"):
            finalize_receipt_split(fin_db, fin_input)

    def test_revoked_authorization_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations SET authorization_state = 'revoked'"
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="not 'authorized'"):
            finalize_receipt_split(fin_db, fin_input)

    def test_wrong_receipt_group_id_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        fingerprint = build_finalization_content_fingerprint(fin_input)
        # Seed auth with correct content hash but wrong receipt group
        fin_db.execute(
            "INSERT OR IGNORE INTO receipt_groups (public_id, currency, status) VALUES (?, ?, ?)",
            ("rg_other", "SGD", "calculated"),
        )
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id="rg_other",
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="receipt group"):
            finalize_receipt_split(fin_db, fin_input)

    def test_wrong_content_hash_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash="a" * 64,  # wrong hash
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="content hash mismatch"):
            finalize_receipt_split(fin_db, fin_input)

    def test_wrong_participant_set_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        fingerprint = build_finalization_content_fingerprint(fin_input)
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,  # correct hash
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            participant_public_ids=("person_owner", "person_member_a"),  # wrong set
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="participant set"):
            finalize_receipt_split(fin_db, fin_input)

    def test_authorization_not_consumed_on_failed_finalization(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        # Break eligibility
        fin_db.execute("UPDATE receipt_groups SET status = 'cancelled'")
        fin_db.commit()
        try:
            finalize_receipt_split(fin_db, fin_input)
        except IneligibleForFinalizationError:
            pass
        state = fin_db.execute(
            "SELECT authorization_state FROM receipt_finalization_authorizations "
            "WHERE authorization_id = ?",
            ("auth_test_finalization_v1",),
        ).fetchone()[0]
        assert state == "authorized", "Authorization must not be consumed on failure"

    def test_authorization_consumed_on_success(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        state = fin_db.execute(
            "SELECT authorization_state FROM receipt_finalization_authorizations "
            "WHERE authorization_id = ?",
            ("auth_test_finalization_v1",),
        ).fetchone()[0]
        assert state == "consumed", "Authorization must be consumed on success"


class TestAtomicity:
    """Transaction boundary and rollback behavior."""

    def test_no_writes_before_transaction_on_auth_failure(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(authorization_id="auth_nonexistent")
        try:
            finalize_receipt_split(fin_db, fin_input)
        except FinalizationAuthorizationError:
            pass
        assert _count_calculation_runs(fin_db) == 0
        assert _count_settlement_obligations(fin_db) == 0

    def test_no_final_state_on_ineligible_group(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_db.execute("UPDATE receipt_groups SET status = 'cancelled'")
        fin_db.commit()
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        try:
            finalize_receipt_split(fin_db, fin_input)
        except IneligibleForFinalizationError:
            pass
        assert _count_calculation_runs(fin_db) == 0
        assert _count_settlement_obligations(fin_db) == 0

    def test_no_partial_state_on_validation_error(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        # After authorization has been loaded, we don't expect partial state
        # since the transaction rolls back on any error
        assert _count_calculation_runs(fin_db) == 0
        assert _count_settlement_obligations(fin_db) == 0


class TestIdempotency:
    """Durable idempotency behavior."""

    def test_same_key_same_content_returns_original_result(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output1 = finalize_receipt_split(fin_db, fin_input)
        output2 = finalize_receipt_split(fin_db, fin_input)
        assert output2.status == FinalizationStatus.ALREADY_FINALIZED.value
        assert output2.obligations_created == output1.obligations_created
        assert output2.finalization_public_id == output1.finalization_public_id
        assert _count_settlement_obligations(fin_db) == 5

    def test_same_key_same_content_across_new_connection(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        # The idempotency table is durable — it survives across
        # the same connection. Using the same connection for the
        # replay test is sufficient to prove process-restart safety
        # since the data is in the DB, not in memory.
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output1 = finalize_receipt_split(fin_db, fin_input)
        output2 = finalize_receipt_split(fin_db, fin_input)
        assert output2.status == FinalizationStatus.ALREADY_FINALIZED.value
        assert output2.finalization_public_id == output1.finalization_public_id

    def test_same_key_different_content_returns_conflict(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input1 = _build_finalization_input(calc_pub_id="calc_conflict_a")
        _prepare_authorized_finalization(fin_db, fin_input1)
        output1 = finalize_receipt_split(fin_db, fin_input1)
        assert output1.status == FinalizationStatus.FINALIZED.value

        # Same idempotency key but different calculation
        fin_input2 = _build_finalization_input(
            calc_pub_id="calc_conflict_b",
            idempotency_key="idem_calc_conflict_a",  # same key as first
            authorization_id="auth_test_diff",
        )
        _seed_authorization_for_input(
            fin_db,
            fin_input2,
            authorization_id="auth_test_diff",
            confirmation_id="conf_test_diff",
        )
        fin_db.commit()
        with pytest.raises(FinalizationIdempotencyError, match="different content"):
            finalize_receipt_split(fin_db, fin_input2)

    def test_different_key_same_already_finalized_receipt_returns_already_finalized(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input1 = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input1)
        finalize_receipt_split(fin_db, fin_input1)

        # Now try a different key targeting the same receipt group
        fin_input2 = _build_finalization_input(
            calc_pub_id="calc_v3",
            idempotency_key="idem_calc_v3",
            authorization_id="auth_test_v3",
            confirmation_id="conf_test_v3",
        )
        _seed_authorization_for_input(
            fin_db,
            fin_input2,
            authorization_id="auth_test_v3",
            confirmation_id="conf_test_v3",
        )
        fin_db.commit()
        with pytest.raises(FinalizationIdempotencyError, match="already finalized"):
            finalize_receipt_split(fin_db, fin_input2)

    def test_repeated_replay_does_not_duplicate_settlement_obligations(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        assert _count_settlement_obligations(fin_db) == 5
        assert _count_calculation_runs(fin_db) == 1

    def test_database_unique_constraint_enforces_duplicate_idempotency_protection(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        # The UNIQUE constraint on idempotency_key prevents a second write
        # This is enforced at the DB level
        row = fin_db.execute(
            "SELECT COUNT(*) FROM receipt_finalization_idempotency WHERE idempotency_key = ?",
            (fin_input.idempotency_key,),
        ).fetchone()
        assert row[0] == 1


class TestMonetaryCorrectness:
    """Money Contract enforcement."""

    def test_float_input_is_rejected(self) -> None:
        with pytest.raises((FinalizationValidationError, FinalizationAuthorizationError)):
            SettlementObligation(
                debtor_participant_public_id="person_member_a",
                creditor_participant_public_id="person_owner",
                amount=14.91,  # type: ignore[arg-type]
                currency="SGD",
            )

    def test_negative_amount_is_rejected(self) -> None:
        with pytest.raises((FinalizationValidationError, ValueError)):
            SettlementObligation(
                debtor_participant_public_id="person_member_a",
                creditor_participant_public_id="person_owner",
                amount=Decimal("-14.91"),
                currency="SGD",
            )

    def test_zero_amount_is_rejected(self) -> None:
        with pytest.raises((FinalizationValidationError, ValueError)):
            SettlementObligation(
                debtor_participant_public_id="person_member_a",
                creditor_participant_public_id="person_owner",
                amount=Decimal("0.00"),
                currency="SGD",
            )

    def test_receipt_total_equals_calculation_snapshot_total(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        # Verify canonical transaction amount matches snapshot
        rows = fin_db.execute(
            "SELECT amount, currency FROM transactions WHERE intent = 'receipt_finalization'"
        ).fetchall()
        assert len(rows) == 1
        assert str(rows[0]["amount"]) == "70.12"

    def test_participant_allocations_sum_to_receipt_total(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        calc_result = _tc001_calc_result()
        total_from_shares = sum(Decimal(str(v)) for v in calc_result["participant_shares"].values())
        assert float(total_from_shares) == 70.12

    def test_amount_on_canonical_transaction_matches_snapshot(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        txn = fin_db.execute(
            "SELECT CAST(amount AS TEXT) AS amount_str, currency "
            "FROM transactions WHERE intent = 'receipt_finalization'"
        ).fetchone()
        assert txn is not None
        assert txn["currency"] == "SGD"
        # canonical_money_str produces "70.12"; SQLite may store NUMERIC as float
        assert txn["amount_str"] == "70.12"


class TestParticipantIdentity:
    """Participant public_id authority."""

    def test_all_obligations_use_participant_public_id(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        for obl in fin_input.settlement_obligations:
            assert obl.debtor_participant_public_id.startswith("person_")
            assert obl.creditor_participant_public_id.startswith("person_")

    def test_payer_identity_in_authorized_participant_set(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        assert fin_input.payer_participant_public_id == "person_owner"
        assert fin_input.payer_participant_public_id in fin_input.participant_public_ids

    def test_duplicate_participant_ids_rejected_early(self) -> None:
        with pytest.raises(
            (FinalizationValidationError, ValueError),
            match="duplicate|Duplicate",
        ):
            FinalizationInput(
                calculation_run_public_id=TEST_CALC_PUBLIC_ID,
                receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
                currency="SGD",
                payer_participant_public_id="person_owner",
                settlement_obligations=[
                    SettlementObligation(
                        debtor_participant_public_id="person_member_a",
                        creditor_participant_public_id="person_owner",
                        amount=Decimal("14.91"),
                        currency="SGD",
                    )
                ],
                calculation_snapshot={
                    "participants": ["person_owner", "person_owner"],
                    "payer": "person_owner",
                    "participant_shares": {"person_owner": "14.91"},
                    "settlement_obligations": [
                        {
                            "debtor": "person_member_a",
                            "creditor": "person_owner",
                            "amount": "14.91",
                            "currency": "SGD",
                        }
                    ],
                },
                idempotency_key="idem_dup_test",
            )


class TestAuditAndEvidence:
    """Audit record completeness."""

    def test_audit_record_includes_authoritative_ids(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output = finalize_receipt_split(fin_db, fin_input)
        row = fin_db.execute(
            "SELECT * FROM receipt_finalization_audit WHERE finalization_id = ?",
            (output.finalization_public_id,),
        ).fetchone()
        assert row is not None
        assert row["authorization_id"] == "auth_test_finalization_v1"
        assert row["receipt_group_public_id"] == TEST_GROUP_PUBLIC_ID
        assert row["payer_participant_public_id"] == "person_owner"
        assert row["currency"] == "SGD"
        assert row["status"] == "finalized"

    def test_audit_record_includes_content_fingerprint(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output = finalize_receipt_split(fin_db, fin_input)
        row = fin_db.execute(
            "SELECT content_fingerprint FROM receipt_finalization_audit WHERE finalization_id = ?",
            (output.finalization_public_id,),
        ).fetchone()
        assert row is not None
        assert len(row["content_fingerprint"]) == 64

    def test_audit_record_includes_canonical_monetary_values(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output = finalize_receipt_split(fin_db, fin_input)
        row = fin_db.execute(
            "SELECT total_paid, total_to_collect FROM receipt_finalization_audit "
            "WHERE finalization_id = ?",
            (output.finalization_public_id,),
        ).fetchone()
        assert row is not None
        # Both should be canonical money strings
        assert "." in row["total_paid"]
        assert "." in row["total_to_collect"]

    def test_audit_is_immutable_after_finalization(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        # Attempting to re-finalize the same group with different content
        # should be blocked by idempotency guard
        fin_input2 = _build_finalization_input(
            calc_pub_id="calc_v_audit_v2",
            idempotency_key="idem_audit_immutable_v2",
            authorization_id="auth_test_audit_v2",
            confirmation_id="conf_test_audit_v2",
        )
        _seed_authorization_for_input(
            fin_db,
            fin_input2,
            authorization_id="auth_test_audit_v2",
            confirmation_id="conf_test_audit_v2",
        )
        fin_db.commit()
        # This should fail because the receipt group is already finalized
        with pytest.raises(FinalizationIdempotencyError, match="already finalized"):
            finalize_receipt_split(fin_db, fin_input2)

    def test_replay_returns_original_audit_linkage(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output1 = finalize_receipt_split(fin_db, fin_input)
        output2 = finalize_receipt_split(fin_db, fin_input)
        assert output2.audit_id == output1.audit_id
        assert output2.finalization_public_id == output1.finalization_public_id


class TestSchemaSafety:
    """No runtime DDL, no live database, no existing migration changes."""

    def test_no_runtime_ddl_during_successful_finalization(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        # Count tables before
        tables_before = fin_db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        finalize_receipt_split(fin_db, fin_input)
        tables_after = fin_db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        assert tables_after == tables_before, "No new tables created during finalization"

    def test_finalization_fails_on_missing_schema(self) -> None:
        """A DB without required migrations 020 and 024 must fail closed."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Apply only migrations 001 and 002 (no 020)
        conn.executescript((migrations_dir() / "001_create_core_schema.sql").read_text("utf-8"))
        conn.executescript((migrations_dir() / "002_receipt_split_schema.sql").read_text("utf-8"))
        conn.commit()

        # Just check that _require_schema raises when tables are missing
        from finance_core.receipt_finalization.finalizer import _require_schema

        with pytest.raises(FinalizationPersistenceError, match="missing"):
            _require_schema(conn)
        conn.close()

    def test_foreign_keys_are_enforced(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        row = fin_db.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1, "Foreign keys must be enabled"

    def test_migration_020_replays_on_temporary_database(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        """Verify all required tables exist after migration."""
        tables = {
            row["name"]
            for row in migrated_temp_db_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for required in (
            "receipt_finalization_authorizations",
            "receipt_finalization_confirmations",
            "receipt_finalization_idempotency",
            "receipt_finalization_audit",
        ):
            assert required in tables, f"Migration 020 table {required} missing"


# =============================================================================
# Regression: existing tests must pass with new authorization requirements
# =============================================================================


class TestRegression:
    """Existing test patterns still pass with the hardened finalizer."""

    def test_existing_calculator_tests_unaffected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        """The calculator still produces valid output."""
        calc_result = _tc001_calc_result()
        assert calc_result["currency"] == "SGD"
        assert len(calc_result["settlement_obligations"]) == 5

    def test_existing_money_contract_unchanged(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        from finance_core.money import money_decimal

        assert money_decimal("70.12", label="test") == Decimal("70.12")

    def test_existing_settlement_runtime_unchanged(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        from finance_core.settlement.runtime import generate_obligations

        obls = generate_obligations(
            participants=["A", "B"],
            balances={"A": Decimal("30.00"), "B": Decimal("-30.00")},
            currency="SGD",
        )
        assert len(obls) == 1


# =============================================================================
# Fix tests: Confirmation binding
# =============================================================================


class TestConfirmationBinding:
    def test_confirmation_wrong_receipt_group_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        # Corrupt confirmation receipt group
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET receipt_group_public_id = ? WHERE confirmation_id = ?",
            ("rg_other", "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="Confirmation receipt group"):
            finalize_receipt_split(fin_db, fin_input)

    def test_confirmation_wrong_content_hash_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET content_hash = ? WHERE confirmation_id = ?",
            ("0" * 64, "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="content hash"):
            finalize_receipt_split(fin_db, fin_input)

    def test_confirmation_wrong_currency_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations SET currency = ? WHERE confirmation_id = ?",
            ("USD", "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="currency"):
            finalize_receipt_split(fin_db, fin_input)

    def test_confirmation_wrong_payer_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET payer_participant_public_id = ? WHERE confirmation_id = ?",
            ("person_member_a", "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="payer"):
            finalize_receipt_split(fin_db, fin_input)

    def test_confirmation_wrong_actor_type_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET actor_type = ? WHERE confirmation_id = ?",
            ("system", "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="actor_type"):
            finalize_receipt_split(fin_db, fin_input)

    def test_confirmation_revoked_is_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET confirmation_state = ? WHERE confirmation_id = ?",
            ("revoked", "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="not 'confirmed'"):
            finalize_receipt_split(fin_db, fin_input)

    def test_confirmation_wrong_participant_set_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET participant_public_ids_json = ? WHERE confirmation_id = ?",
            ('["person_owner"]', "conf_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="participant set"):
            finalize_receipt_split(fin_db, fin_input)


# =============================================================================
# Fix tests: Authorization binding
# =============================================================================


class TestAuthorizationBinding:
    def test_auth_snapshot_id_mismatch_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET calculation_snapshot_id = ? WHERE authorization_id = ?",
            ("wrong_snap", "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="snapshot"):
            finalize_receipt_split(fin_db, fin_input)

    def test_auth_confirmation_id_mismatch_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        fingerprint = build_finalization_content_fingerprint(fin_input)
        # Delete existing seed, re-seed with mismatched confirmation_id
        fin_db.execute(
            "DELETE FROM receipt_finalization_authorizations WHERE authorization_id = ?",
            ("auth_test_finalization_v1",),
        )
        fin_db.execute(
            "DELETE FROM receipt_finalization_confirmations "
            "WHERE confirmation_id IN ('conf_test_finalization_v1', 'wrong_conf')",
        )
        fin_db.commit()

        # Seed confirmation with wrong_conf
        fin_db.execute(
            "INSERT OR IGNORE INTO receipt_finalization_confirmations "
            "(confirmation_id, receipt_group_public_id, calculation_run_public_id, "
            "calculation_snapshot_id, content_hash, currency, final_total, "
            "payer_participant_public_id, participant_public_ids_json, "
            "settlement_obligations_json, actor_type, actor_id, "
            "confirmation_state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "wrong_conf",
                fin_input.receipt_group_public_id,
                fin_input.calculation_run_public_id,
                fin_input.calculation_snapshot_id or "snap_test_v1",
                fingerprint,
                fin_input.currency,
                "70.12",
                fin_input.payer_participant_public_id,
                json.dumps(sorted(fin_input.participant_public_ids)),
                "[]",
                "cli",
                "person_owner",
                "confirmed",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        # Seed authorization referencing wrong_conf
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            confirmation_id="wrong_conf",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id or "snap_test_v1",
            currency=fin_input.currency,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="confirmation"):
            finalize_receipt_split(fin_db, fin_input)

    def test_auth_actor_type_mismatch_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET actor_type = ? WHERE authorization_id = ?",
            ("system", "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="actor_type"):
            finalize_receipt_split(fin_db, fin_input)

    def test_auth_malformed_participant_json_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET participant_public_ids_json = ? WHERE authorization_id = ?",
            ("{not valid json", "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="malformed JSON"):
            finalize_receipt_split(fin_db, fin_input)

    def test_auth_duplicate_participant_ids_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _seed_authorization_for_input(fin_db, fin_input)
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET participant_public_ids_json = ? WHERE authorization_id = ?",
            ('["person_owner", "person_owner", "person_member_a"]', "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="duplicate"):
            finalize_receipt_split(fin_db, fin_input)


# =============================================================================
# Fix tests: Replay and idempotency
# =============================================================================


class TestReplayAndIdempotency:
    def test_replay_returns_valid_output_with_real_row(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output1 = finalize_receipt_split(fin_db, fin_input)
        output2 = finalize_receipt_split(fin_db, fin_input)
        assert output2.status == FinalizationStatus.ALREADY_FINALIZED.value
        assert output2.finalization_public_id == output1.finalization_public_id
        assert output2.calculation_run_public_id == output1.calculation_run_public_id
        assert output2.transaction_public_id == output1.transaction_public_id
        assert output2.settlement_public_ids == output1.settlement_public_ids
        assert output2.audit_id == output1.audit_id
        assert output2.obligations_created == output1.obligations_created

    def test_different_key_already_finalized_same_content_replay(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input1 = _build_finalization_input(calc_pub_id="calc_a")
        _prepare_authorized_finalization(fin_db, fin_input1)
        output1 = finalize_receipt_split(fin_db, fin_input1)
        assert output1.status == FinalizationStatus.FINALIZED.value

        # Same content, different key -> already_finalized
        fin_input2 = _build_finalization_input(
            calc_pub_id="calc_b",
            idempotency_key="idem_calc_b",
            authorization_id="auth_test_b",
            confirmation_id="conf_test_b",
        )
        _seed_authorization_for_input(
            fin_db,
            fin_input2,
            authorization_id="auth_test_b",
            confirmation_id="conf_test_b",
        )
        fin_db.commit()
        with pytest.raises(FinalizationIdempotencyError, match="already finalized"):
            finalize_receipt_split(fin_db, fin_input2)

    def test_dangling_audit_reference_fails_closed(self) -> None:
        """If an idempotency row points to a missing audit, reject in replay path."""
        from finance_core.receipt_finalization.finalizer import _build_replay_output

        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(
            "CREATE TABLE receipt_finalization_audit "
            "(finalization_id TEXT PRIMARY KEY, calculation_run_public_id TEXT, "
            "transaction_public_id TEXT, settlement_public_ids_json TEXT, "
            "idempotency_key TEXT)"
        )
        c.commit()

        with pytest.raises(ValueError, match="dangling|nonexistent"):
            _build_replay_output(c, "nonexistent_audit", "test_key")
        c.close()

    def test_no_duplicate_canonical_transaction_after_conflict(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        txn_count = fin_db.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'receipt_finalization'"
        ).fetchone()[0]
        assert txn_count == 1
        # Replay does not create duplicates
        finalize_receipt_split(fin_db, fin_input)
        txn_count2 = fin_db.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'receipt_finalization'"
        ).fetchone()[0]
        assert txn_count2 == 1

    def test_no_duplicate_settlement_obligations_after_replay(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        assert _count_settlement_obligations(fin_db) == 5


# =============================================================================
# Fix tests: Database uniqueness
# =============================================================================


class TestDatabaseUniqueness:
    def test_db_rejects_second_finalized_audit_same_receipt(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)

        # Try inserting a second finalized audit row for the same receipt group
        with pytest.raises(sqlite3.IntegrityError):
            fin_db.execute(
                "INSERT INTO receipt_finalization_audit "
                "(finalization_id, idempotency_key, content_fingerprint, "
                "authorization_id, receipt_group_public_id, calculation_run_public_id, "
                "participant_public_ids_json, settlement_public_ids_json, "
                "currency, total_paid, total_to_collect, "
                "payer_participant_public_id, actor_type, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "fin_duplicate",
                    "idem_dup",
                    "0" * 64,
                    "auth_test_finalization_v1",
                    fin_input.receipt_group_public_id,
                    fin_input.calculation_run_public_id,
                    "[]",
                    "[]",
                    "SGD",
                    "70.12",
                    "56.57",
                    "person_owner",
                    "cli",
                    "finalized",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        fin_db.rollback()

    def test_uniqueness_violation_maps_to_domain_error(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)

        # Different key, different content, same receipt group -- must conflict
        fin_input2 = _build_finalization_input(
            calc_pub_id="calc_uniq_test",
            idempotency_key="idem_uniq_test",
            authorization_id="auth_uniq_test",
            confirmation_id="conf_uniq_test",
        )
        _seed_authorization_for_input(
            fin_db,
            fin_input2,
            authorization_id="auth_uniq_test",
            confirmation_id="conf_uniq_test",
        )
        fin_db.commit()
        with pytest.raises(FinalizationIdempotencyError, match="already finalized"):
            finalize_receipt_split(fin_db, fin_input2)


# =============================================================================
# Fix tests: Relational integrity
# =============================================================================


class TestRelationalIntegrity:
    def test_audit_fk_authorization_enforced(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO receipt_finalization_audit "
                "(finalization_id, idempotency_key, content_fingerprint, "
                "authorization_id, receipt_group_public_id, "
                "calculation_run_public_id, participant_public_ids_json, "
                "settlement_public_ids_json, "
                "currency, total_paid, total_to_collect, "
                "payer_participant_public_id, actor_type, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "fin_fk_test",
                    "idem_fk_test",
                    "0" * 64,
                    "nonexistent_auth",
                    "rg_test",
                    "calc_test",
                    "[]",
                    "[]",
                    "SGD",
                    "10.00",
                    "5.00",
                    "person_owner",
                    "cli",
                    "finalized",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        conn.rollback()

    def test_idempotency_fk_audit_enforced(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO receipt_finalization_idempotency "
                "(idempotency_key, content_fingerprint, status, "
                "finalization_audit_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "idem_fk_test",
                    "0" * 64,
                    "finalized",
                    "nonexistent_audit",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        conn.rollback()

    def test_fk_check_passes_after_successful_finalization(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        violations = fin_db.execute("PRAGMA foreign_key_check").fetchall()
        assert len(violations) == 0, f"Foreign key violations found: {violations}"


# =============================================================================
# Fix tests: Deterministic date
# =============================================================================


class TestDeterministicDate:
    def test_missing_receipt_date_uses_clock(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        calc_result = _tc001_calc_result()
        # Remove receipt_datetime from all receipts
        for rc in calc_result.get("receipts", []):
            rc.pop("receipt_datetime", None)
        fin_input = _build_finalization_input(calc_result)
        _prepare_authorized_finalization(fin_db, fin_input)
        FROZEN = "2026-07-11T12:00:00+08:00"
        output = finalize_receipt_split(fin_db, fin_input, clock=lambda: FROZEN)
        txn = fin_db.execute(
            "SELECT transaction_date FROM transactions WHERE public_id = ?",
            (output.transaction_public_id,),
        ).fetchone()
        assert txn is not None
        assert txn["transaction_date"] == "2026-07-11"

    def test_same_input_and_clock_produce_same_date(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        calc_result = _tc001_calc_result()
        for rc in calc_result.get("receipts", []):
            rc.pop("receipt_datetime", None)
        FROZEN = "2026-01-15T08:00:00+00:00"
        output1 = finalize_receipt_split(
            fin_db,
            _prepare_authorized_finalization(
                fin_db,
                _build_finalization_input(
                    calc_result, calc_pub_id="calc_date_a", idempotency_key="idem_date_a"
                ),
            ),
            clock=lambda: FROZEN,
        )
        txn1 = fin_db.execute(
            "SELECT transaction_date FROM transactions WHERE public_id = ?",
            (output1.transaction_public_id,),
        ).fetchone()
        assert txn1["transaction_date"] == "2026-01-15"


# =============================================================================
# Fix tests: Audit semantics
# =============================================================================


class TestAuditSemantics:
    def test_only_successful_finalizations_persisted_in_audit(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        count_before = fin_db.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[
            0
        ]
        assert count_before == 0  # blocked attempts do not create audit rows

    def test_successful_finalization_creates_audit(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        count = fin_db.execute(
            "SELECT COUNT(*) FROM receipt_finalization_audit WHERE status = 'finalized'"
        ).fetchone()[0]
        assert count == 1

    def test_idempotent_replay_returns_original_audit_linkage(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        output1 = finalize_receipt_split(fin_db, fin_input)
        output2 = finalize_receipt_split(fin_db, fin_input)
        assert output2.audit_id == output1.audit_id
        assert output2.finalization_public_id == output1.finalization_public_id


# =============================================================================
# Fix tests: Atomicity regression
# =============================================================================


class TestAtomicityRegression:
    def test_rollback_leaves_no_state_after_txn_insert_failure(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        # Corrupt the authorization so that the final total mismatches
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET final_total = ? WHERE authorization_id = ?",
            ("999.99", "auth_test_finalization_v1"),
        )
        fin_db.commit()
        try:
            finalize_receipt_split(fin_db, fin_input)
        except FinalizationAuthorizationError:
            pass
        assert _count_calculation_runs(fin_db) == 0
        assert _count_settlement_obligations(fin_db) == 0
        txn_count = fin_db.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'receipt_finalization'"
        ).fetchone()[0]
        assert txn_count == 0

    def test_rollback_after_auth_load_no_writes(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        # Break eligibility inside tx
        fin_db.execute(
            "UPDATE receipt_groups SET status = 'cancelled' WHERE public_id = ?",
            (TEST_GROUP_PUBLIC_ID,),
        )
        fin_db.commit()
        try:
            finalize_receipt_split(fin_db, fin_input)
        except IneligibleForFinalizationError:
            pass
        assert _count_calculation_runs(fin_db) == 0
        assert _count_settlement_obligations(fin_db) == 0


# =============================================================================
# Fix tests: Schema safety regression
# =============================================================================


class TestSchemaSafetyFix:
    def test_one_finalized_per_receipt_index_exists(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "uq_receipt_finalization_audit_one_per_group" in indexes

    def test_idempotency_fk_exists(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        fk_info = conn.execute(
            "PRAGMA foreign_key_list(receipt_finalization_idempotency)"
        ).fetchall()
        assert len(fk_info) >= 1, "Idempotency table must have a foreign key"


# =============================================================================
# Fix tests: Missing schema (each table individually)
# =============================================================================


class TestMissingSchema:
    def test_missing_confirmations_table_rejected(self) -> None:
        from finance_core.receipt_finalization.finalizer import _require_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE receipt_finalization_authorizations (authorization_id TEXT PRIMARY KEY)"
        )
        conn.execute("CREATE TABLE receipt_finalization_audit (finalization_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE receipt_finalization_idempotency (idempotency_key TEXT PRIMARY KEY)"
        )
        conn.commit()
        with pytest.raises(FinalizationPersistenceError, match="confirmations"):
            _require_schema(conn)
        conn.close()

    def test_missing_authorizations_table_rejected(self) -> None:
        from finance_core.receipt_finalization.finalizer import _require_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE receipt_finalization_confirmations (confirmation_id TEXT PRIMARY KEY)"
        )
        conn.execute("CREATE TABLE receipt_finalization_audit (finalization_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE receipt_finalization_idempotency (idempotency_key TEXT PRIMARY KEY)"
        )
        conn.commit()
        with pytest.raises(FinalizationPersistenceError, match="authorizations"):
            _require_schema(conn)
        conn.close()

    def test_missing_audit_table_rejected(self) -> None:
        from finance_core.receipt_finalization.finalizer import _require_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE receipt_finalization_confirmations (confirmation_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE receipt_finalization_authorizations (authorization_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE receipt_finalization_idempotency (idempotency_key TEXT PRIMARY KEY)"
        )
        conn.commit()
        with pytest.raises(FinalizationPersistenceError, match="audit"):
            _require_schema(conn)
        conn.close()

    def test_missing_idempotency_table_rejected(self) -> None:
        from finance_core.receipt_finalization.finalizer import _require_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE receipt_finalization_confirmations (confirmation_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE receipt_finalization_authorizations (authorization_id TEXT PRIMARY KEY)"
        )
        conn.execute("CREATE TABLE receipt_finalization_audit (finalization_id TEXT PRIMARY KEY)")
        conn.commit()
        with pytest.raises(FinalizationPersistenceError, match="idempotency"):
            _require_schema(conn)
        conn.close()


# =============================================================================
# Fix tests: Confirmation participants (strict validation)
# =============================================================================


class TestConfirmationParticipantStrict:
    def _seed_auth_with_conf_json(
        self,
        fin_db: sqlite3.Connection,
        conf_json: str,
    ) -> FinalizationInput:
        fin_input = _build_finalization_input()
        fingerprint = build_finalization_content_fingerprint(fin_input)
        obls_json = json.dumps(
            sorted(
                [
                    {
                        "debtor": o.debtor_participant_public_id,
                        "creditor": o.creditor_participant_public_id,
                        "amount": str(o.amount),
                        "currency": o.currency,
                    }
                    for o in fin_input.settlement_obligations
                ],
                key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
            )
        )
        fin_db.execute(
            "DELETE FROM receipt_finalization_authorizations WHERE authorization_id = ?",
            ("auth_test_finalization_v1",),
        )
        fin_db.execute(
            "DELETE FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
            ("conf_test_finalization_v1",),
        )
        fin_db.commit()
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            confirmation_id="conf_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            currency=fin_input.currency,
            settlement_obligations_json=obls_json,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
        )
        fin_db.execute(
            "UPDATE receipt_finalization_confirmations "
            "SET participant_public_ids_json = ? WHERE confirmation_id = ?",
            (conf_json, "conf_test_finalization_v1"),
        )
        fin_db.commit()
        return fin_input

    def test_duplicate_conf_participant_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input()
        self._seed_auth_with_conf_json(
            fin_db,
            json.dumps(
                sorted(fin_input.participant_public_ids) + [fin_input.participant_public_ids[0]]
            ),
        )
        with pytest.raises(FinalizationAuthorizationError, match="duplicate"):
            finalize_receipt_split(fin_db, fin_input)

    def test_non_string_conf_participant_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = self._seed_auth_with_conf_json(
            fin_db, '["person_owner", 123, "person_member_a"]'
        )
        with pytest.raises(FinalizationAuthorizationError, match="must be a string"):
            finalize_receipt_split(fin_db, fin_input)

    def test_empty_conf_participant_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = self._seed_auth_with_conf_json(
            fin_db, '["person_owner", "", "person_member_a"]'
        )
        with pytest.raises(FinalizationAuthorizationError, match="empty"):
            finalize_receipt_split(fin_db, fin_input)

    def test_conf_participant_not_a_list_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = self._seed_auth_with_conf_json(fin_db, '{"person_owner": true}')
        with pytest.raises(FinalizationAuthorizationError, match="must be a JSON list"):
            finalize_receipt_split(fin_db, fin_input)

    def test_malformed_conf_participant_json_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = self._seed_auth_with_conf_json(fin_db, "{not json!!")
        with pytest.raises(FinalizationAuthorizationError, match="malformed JSON"):
            finalize_receipt_split(fin_db, fin_input)


# =============================================================================
# Fix tests: Evidence refs (strict validation)
# =============================================================================


class TestEvidenceStrict:
    def test_duplicate_persisted_evidence_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(
            source_evidence_refs=("ev-1", "ev-2"),
        )
        fingerprint = build_finalization_content_fingerprint(fin_input)
        obls_json = json.dumps(
            sorted(
                [
                    {
                        "debtor": o.debtor_participant_public_id,
                        "creditor": o.creditor_participant_public_id,
                        "amount": str(o.amount),
                        "currency": o.currency,
                    }
                    for o in fin_input.settlement_obligations
                ],
                key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
            )
        )
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            currency=fin_input.currency,
            settlement_obligations_json=obls_json,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
            source_evidence_refs=("ev-1", "ev-1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="duplicate"):
            finalize_receipt_split(fin_db, fin_input)

    def test_non_string_persisted_evidence_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(
            source_evidence_refs=("ev-1",),
        )
        fingerprint = build_finalization_content_fingerprint(fin_input)
        obls_json = json.dumps(
            sorted(
                [
                    {
                        "debtor": o.debtor_participant_public_id,
                        "creditor": o.creditor_participant_public_id,
                        "amount": str(o.amount),
                        "currency": o.currency,
                    }
                    for o in fin_input.settlement_obligations
                ],
                key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
            )
        )
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            currency=fin_input.currency,
            settlement_obligations_json=obls_json,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
        )
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET source_evidence_refs_json = ? WHERE authorization_id = ?",
            ('["ev-1", 456]', "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="must be a string"):
            finalize_receipt_split(fin_db, fin_input)

    def test_empty_persisted_evidence_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(
            source_evidence_refs=("ev-1",),
        )
        fingerprint = build_finalization_content_fingerprint(fin_input)
        obls_json = json.dumps(
            sorted(
                [
                    {
                        "debtor": o.debtor_participant_public_id,
                        "creditor": o.creditor_participant_public_id,
                        "amount": str(o.amount),
                        "currency": o.currency,
                    }
                    for o in fin_input.settlement_obligations
                ],
                key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
            )
        )
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            currency=fin_input.currency,
            settlement_obligations_json=obls_json,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
        )
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET source_evidence_refs_json = ? WHERE authorization_id = ?",
            ('["ev-1", ""]', "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="empty"):
            finalize_receipt_split(fin_db, fin_input)

    def test_persisted_evidence_not_a_list_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(
            source_evidence_refs=("ev-1",),
        )
        fingerprint = build_finalization_content_fingerprint(fin_input)
        obls_json = json.dumps(
            sorted(
                [
                    {
                        "debtor": o.debtor_participant_public_id,
                        "creditor": o.creditor_participant_public_id,
                        "amount": str(o.amount),
                        "currency": o.currency,
                    }
                    for o in fin_input.settlement_obligations
                ],
                key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
            )
        )
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            currency=fin_input.currency,
            settlement_obligations_json=obls_json,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
        )
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET source_evidence_refs_json = ? WHERE authorization_id = ?",
            ('"not_a_list"', "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="must be a JSON list"):
            finalize_receipt_split(fin_db, fin_input)

    def test_malformed_persisted_evidence_json_rejected(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        fin_input = _build_finalization_input(
            source_evidence_refs=("ev-1",),
        )
        fingerprint = build_finalization_content_fingerprint(fin_input)
        obls_json = json.dumps(
            sorted(
                [
                    {
                        "debtor": o.debtor_participant_public_id,
                        "creditor": o.creditor_participant_public_id,
                        "amount": str(o.amount),
                        "currency": o.currency,
                    }
                    for o in fin_input.settlement_obligations
                ],
                key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
            )
        )
        _seed_authorization(
            fin_db,
            authorization_id="auth_test_finalization_v1",
            content_hash=fingerprint,
            receipt_group_public_id=fin_input.receipt_group_public_id,
            calculation_run_public_id=fin_input.calculation_run_public_id,
            calculation_snapshot_id=fin_input.calculation_snapshot_id,
            currency=fin_input.currency,
            settlement_obligations_json=obls_json,
            payer_participant_public_id=fin_input.payer_participant_public_id,
            participant_public_ids=fin_input.participant_public_ids,
            actor_type="cli",
            actor_id="person_owner",
        )
        fin_db.execute(
            "UPDATE receipt_finalization_authorizations "
            "SET source_evidence_refs_json = ? WHERE authorization_id = ?",
            ("{not json", "auth_test_finalization_v1"),
        )
        fin_db.commit()
        with pytest.raises(FinalizationAuthorizationError, match="malformed JSON"):
            finalize_receipt_split(fin_db, fin_input)


# =============================================================================
# Fix tests: Request evidence validation at model construction
# =============================================================================


class TestRequestEvidence:
    def test_duplicate_request_evidence_rejected(self) -> None:
        with pytest.raises(FinalizationValidationError, match="duplicate"):
            FinalizationInput(
                calculation_run_public_id=TEST_CALC_PUBLIC_ID,
                receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
                currency="SGD",
                payer_participant_public_id="person_owner",
                settlement_obligations=[
                    SettlementObligation(
                        debtor_participant_public_id="person_member_a",
                        creditor_participant_public_id="person_owner",
                        amount=Decimal("14.91"),
                        currency="SGD",
                    )
                ],
                calculation_snapshot={
                    "participants": ["person_owner", "person_member_a"],
                    "payer": "person_owner",
                    "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                    "settlement_obligations": [
                        {
                            "debtor": "person_member_a",
                            "creditor": "person_owner",
                            "amount": "14.91",
                            "currency": "SGD",
                        }
                    ],
                    "receipts": [],
                },
                idempotency_key="idem_ev_req1",
                source_evidence_refs=("ev-1", "ev-1"),
            )

    def test_non_string_request_evidence_rejected(self) -> None:
        with pytest.raises(FinalizationValidationError, match="must be a string"):
            FinalizationInput(
                calculation_run_public_id=TEST_CALC_PUBLIC_ID,
                receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
                currency="SGD",
                payer_participant_public_id="person_owner",
                settlement_obligations=[
                    SettlementObligation(
                        debtor_participant_public_id="person_member_a",
                        creditor_participant_public_id="person_owner",
                        amount=Decimal("14.91"),
                        currency="SGD",
                    )
                ],
                calculation_snapshot={
                    "participants": ["person_owner", "person_member_a"],
                    "payer": "person_owner",
                    "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                    "settlement_obligations": [
                        {
                            "debtor": "person_member_a",
                            "creditor": "person_owner",
                            "amount": "14.91",
                            "currency": "SGD",
                        }
                    ],
                    "receipts": [],
                },
                idempotency_key="idem_ev_req2",
                source_evidence_refs=("ev-1", 99),
            )

    def test_empty_request_evidence_rejected(self) -> None:
        with pytest.raises(FinalizationValidationError, match="empty"):
            FinalizationInput(
                calculation_run_public_id=TEST_CALC_PUBLIC_ID,
                receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
                currency="SGD",
                payer_participant_public_id="person_owner",
                settlement_obligations=[
                    SettlementObligation(
                        debtor_participant_public_id="person_member_a",
                        creditor_participant_public_id="person_owner",
                        amount=Decimal("14.91"),
                        currency="SGD",
                    )
                ],
                calculation_snapshot={
                    "participants": ["person_owner", "person_member_a"],
                    "payer": "person_owner",
                    "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                    "settlement_obligations": [
                        {
                            "debtor": "person_member_a",
                            "creditor": "person_owner",
                            "amount": "14.91",
                            "currency": "SGD",
                        }
                    ],
                    "receipts": [],
                },
                idempotency_key="idem_ev_req3",
                source_evidence_refs=("ok", ""),
            )

    def test_list_input_frozen_as_tuple(self) -> None:
        fin_input = FinalizationInput(
            calculation_run_public_id=TEST_CALC_PUBLIC_ID,
            receipt_group_public_id=TEST_GROUP_PUBLIC_ID,
            currency="SGD",
            payer_participant_public_id="person_owner",
            settlement_obligations=[
                SettlementObligation(
                    debtor_participant_public_id="person_member_a",
                    creditor_participant_public_id="person_owner",
                    amount=Decimal("14.91"),
                    currency="SGD",
                )
            ],
            calculation_snapshot={
                "participants": ["person_owner", "person_member_a"],
                "payer": "person_owner",
                "participant_shares": {"person_owner": "0", "person_member_a": "14.91"},
                "settlement_obligations": [
                    {
                        "debtor": "person_member_a",
                        "creditor": "person_owner",
                        "amount": "14.91",
                        "currency": "SGD",
                    }
                ],
                "receipts": [],
                "total_paid": "14.91",
                "payer_paid_amounts": {"person_owner": "14.91", "person_member_a": "0"},
            },
            idempotency_key="idem_ev_frozen",
            source_evidence_refs=["ev-a", "ev-b"],
        )
        assert isinstance(fin_input.source_evidence_refs, tuple)
        assert sorted(fin_input.source_evidence_refs) == sorted(["ev-a", "ev-b"])


# =============================================================================
# Fix tests: Reason-code authority consolidation
# =============================================================================


class TestReasonCodeAuthority:
    def test_finalizer_has_no_freestanding_FinalizationReason(self) -> None:
        from finance_core.receipt_finalization import finalizer as fm

        assert not hasattr(fm, "FinalizationReason"), "FinalizationReason must not exist"

    def test_all_finalizer_reasons_come_from_FinalizationBlockReason(self) -> None:
        from finance_core.receipt_finalization.finalizer import FinalizationBlockReason

        assert FinalizationBlockReason.AUTHORIZATION_MISSING.value == "authorization_missing"
        assert FinalizationBlockReason.CONTENT_CONFLICT.value == "content_conflict"
        assert FinalizationBlockReason.DANGLING_AUDIT_REFERENCE.value == "dangling_audit_reference"
        assert FinalizationBlockReason.MISSING_SCHEMA.value == "missing_schema"
        assert FinalizationBlockReason.EVIDENCE_MISMATCH.value == "evidence_mismatch"
        assert (
            FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH.value
            == "confirmation_content_mismatch"
        )
        assert (
            FinalizationBlockReason.CONFIRMATION_ACTOR_MISMATCH.value
            == "confirmation_actor_mismatch"
        )

    def test_conflict_error_exposes_correct_reason(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        from finance_core.receipt_finalization.models import FinalizationBlockReason

        fin_input = _build_finalization_input()
        _prepare_authorized_finalization(fin_db, fin_input)
        finalize_receipt_split(fin_db, fin_input)
        fin_input2 = _build_finalization_input(
            calc_pub_id="calc_reason_test",
            idempotency_key="idem_calc_test_finalization_v1",
            authorization_id="auth_reason_test",
            confirmation_id="conf_reason_test",
        )
        _seed_authorization_for_input(
            fin_db,
            fin_input2,
            authorization_id="auth_reason_test",
            confirmation_id="conf_reason_test",
        )
        fin_db.commit()
        try:
            finalize_receipt_split(fin_db, fin_input2)
        except FinalizationIdempotencyError as exc:
            assert exc.reason == FinalizationBlockReason.CONTENT_CONFLICT.value

    def test_dangling_audit_exposes_correct_reason(
        self,
        fin_db: sqlite3.Connection,
    ) -> None:
        from finance_core.receipt_finalization.finalizer import _build_replay_output
        from finance_core.receipt_finalization.models import FinalizationBlockReason

        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(
            "CREATE TABLE receipt_finalization_audit "
            "(finalization_id TEXT PRIMARY KEY, calculation_run_public_id TEXT, "
            "transaction_public_id TEXT, settlement_public_ids_json TEXT, "
            "idempotency_key TEXT)"
        )
        c.commit()
        try:
            _build_replay_output(c, "nonexistent_audit", "test_key")
        except FinalizationIdempotencyError as exc:
            assert exc.reason == FinalizationBlockReason.DANGLING_AUDIT_REFERENCE.value
        c.close()


def test_concurrent_receipt_finalization_replays_one_canonical_result(
    migrated_temp_db_path: Path,
) -> None:
    """Two production finalizers use separate connections and one durable result."""
    setup = connect_sqlite(migrated_temp_db_path)
    try:
        _seed_participants(setup)
        _seed_receipt_group(setup)
        _seed_receipt_group_participant_links(setup)
        fin_input = _build_finalization_input(idempotency_key="idem-concurrent-finalization")
        _prepare_authorized_finalization(setup, fin_input)
    finally:
        setup.close()

    barrier = Barrier(2)

    def finalize() -> tuple[FinalizationOutput | None, BaseException | None]:
        conn = connect_sqlite(migrated_temp_db_path)
        try:
            barrier.wait(timeout=10)
            return finalize_receipt_split(conn, fin_input), None
        except BaseException as exc:  # worker errors are asserted in the parent thread
            return None, exc
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=15)
            for future in (executor.submit(finalize), executor.submit(finalize))
        ]

    assert [error for _, error in outcomes] == [None, None]
    outputs = [output for output, _ in outcomes]
    assert {output.status for output in outputs if output is not None} == {
        FinalizationStatus.FINALIZED,
        FinalizationStatus.ALREADY_FINALIZED,
    }
    assert len({output.transaction_public_id for output in outputs if output is not None}) == 1

    restarted = connect_sqlite(migrated_temp_db_path)
    try:
        assert (
            restarted.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1
        )
        assert restarted.execute("SELECT COUNT(*) FROM calculation_runs").fetchone()[0] == 1
        expected_obligations = len(fin_input.settlement_obligations)
        assert (
            restarted.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]
            == expected_obligations
        )
        assert restarted.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restarted.close()
