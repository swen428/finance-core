"""B4.2 read-only receipt calculator-readiness reporting tests.

Covers Slice B4.2 of
``docs/design/receipt_proposal_to_facts_conversion_v1.md``: a real B4.1
conversion in a temporary migrated staging database reports **not
calculator-ready** with deterministic stable reason codes (approved
Decision D2); untrusted, corrupt, or contradictory persisted state fails
closed with typed errors; and the boundary is proven SELECT-only with zero
effects on every success and failure path.

Persisted-drift fixtures reuse the established B4.1 technique: migration
035 protection triggers are dropped (and ``PRAGMA ignore_check_constraints``
enabled where a schema CHECK would otherwise block the corruption vector)
so the readiness boundary is exercised against states that bypassed the
guarded write path.  All fixtures are synthetic and privacy-safe; no live
database or seed data is touched.
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from finance_core.calculators.receipt_calculator_readiness import (
    REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS,
    REASON_NO_AUTHORITATIVE_ITEM_FACTS,
    ReadinessStagingDatabaseRejectedError,
    ReceiptCalculatorReadinessReport,
    ReceiptFactsIntegrityError,
    ReceiptNotFoundError,
    UnsupportedReceiptProvenanceError,
    report_receipt_calculator_readiness,
)
from finance_core.parser_proposals.receipt_facts_conversion import derive_receipt_public_id
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsResult,
    derive_item_public_id,
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_receipt_facts_conversion_v1 import (
    command,
    convert,
    entries,
    seed_confirmed_receipt_proposal,
    seed_people,
    table_counts,
)
from tests.test_receipt_item_allocation_facts_service_v1 import (
    ConvertedReceipt,
    iaf_command,
    setup_receipt,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import (
    correction_command,
    replacement_allocations,
    replacement_items,
)

EXPECTED_NOT_READY_REASONS = (
    REASON_NO_AUTHORITATIVE_ITEM_FACTS,
    REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def all_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[str, ...]]]:
    """Value-and-storage-class snapshot of every user table (rowid included).

    ``repr`` discriminates SQLite storage classes that Python equality
    conflates (``1 == 1.0 == True``), so an INTEGER→REAL rewrite of a
    monetary column cannot evade the zero-effect assertions.
    """
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: [
            tuple(repr(value) for value in row)
            for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in tables
    }


def report_with_zero_effects(
    conn: sqlite3.Connection, receipt_public_id: str
) -> ReceiptCalculatorReadinessReport:
    """Run one readiness read and assert zero persisted effects either way."""
    before_counts = table_counts(conn)
    before_rows = all_rows(conn)
    try:
        return report_receipt_calculator_readiness(conn, receipt_public_id)
    finally:
        assert table_counts(conn) == before_counts
        assert all_rows(conn) == before_rows


def convert_receipt(conn: sqlite3.Connection, tmp_path: Path, suffix: str) -> tuple[str, int, str]:
    """Run one real B4.1 conversion; return (receipt_public_id, receipt_id, command_id)."""
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, suffix)
    membership = entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 0))
    result = convert(conn, command(suffix, public_id, expected, participants=membership))
    assert result.idempotent is False
    return result.receipt_public_id, result.receipt_id, result.command_public_id


def corrupt_receipt_row(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> None:
    """Forge persisted drift past the migration 035 guards (fixture surgery)."""
    conn.execute("DROP TRIGGER IF EXISTS trg_receipts_conversion_bound_freeze")
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")


# ---------------------------------------------------------------------------
# Happy path: a real B4.1 conversion is valid but not calculator-ready
# ---------------------------------------------------------------------------


def test_real_b41_conversion_reports_not_ready(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _receipt_id, command_id = convert_receipt(conn, tmp_path, "ready1")

    report = report_with_zero_effects(conn, receipt_public_id)

    assert report == ReceiptCalculatorReadinessReport(
        receipt_public_id=receipt_public_id,
        conversion_command_public_id=command_id,
        is_calculator_ready=False,
        not_ready_reasons=EXPECTED_NOT_READY_REASONS,
    )
    assert report.receipt_public_id == derive_receipt_public_id(command_id)
    assert not conn.in_transaction


def test_reason_codes_are_stable_contract_values() -> None:
    """Reason codes are a stable external contract; renames are breaking."""
    assert REASON_NO_AUTHORITATIVE_ITEM_FACTS == "no_authoritative_item_facts"
    assert REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS == "no_authoritative_allocation_facts"


def test_report_is_immutable(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "frozen1")
    report = report_receipt_calculator_readiness(conn, receipt_public_id)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.is_calculator_ready = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Determinism and replay
# ---------------------------------------------------------------------------


def test_exact_replay_produces_identical_report(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "replay1")
    first = report_with_zero_effects(conn, receipt_public_id)
    second = report_with_zero_effects(conn, receipt_public_id)
    assert first == second
    assert first.not_ready_reasons == second.not_ready_reasons == EXPECTED_NOT_READY_REASONS


def test_fresh_independent_connection_produces_identical_report(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "fresh1")
    first = report_receipt_calculator_readiness(conn, receipt_public_id)

    fresh = connect_temp_db(migrated_temp_db_path)
    try:
        second = report_with_zero_effects(fresh, receipt_public_id)
    finally:
        fresh.close()
    assert first == second


def test_connection_without_row_factory_supported(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "norow1")
    expected = report_receipt_calculator_readiness(conn, receipt_public_id)

    plain = sqlite3.connect(str(migrated_temp_db_path))
    try:
        assert plain.row_factory is None
        assert report_receipt_calculator_readiness(plain, receipt_public_id) == expected
        assert not plain.in_transaction
    finally:
        plain.close()


# ---------------------------------------------------------------------------
# Read-only and non-effect guarantees
# ---------------------------------------------------------------------------


def test_caller_owned_read_transaction_is_preserved(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "callertx")
    conn.execute("BEGIN")
    try:
        assert conn.in_transaction
        report = report_receipt_calculator_readiness(conn, receipt_public_id)
        # The caller-owned transaction is neither committed nor rolled back.
        assert conn.in_transaction
        assert report.not_ready_reasons == EXPECTED_NOT_READY_REASONS
    finally:
        conn.rollback()
    assert not conn.in_transaction


def test_works_under_query_only_pragma(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "queryonly")
    conn.execute("PRAGMA query_only = ON")
    try:
        # Sanity: writes really are impossible on this connection now.
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE receipts SET notes = 'x'")
        report = report_receipt_calculator_readiness(conn, receipt_public_id)
        assert report.is_calculator_ready is False
    finally:
        conn.execute("PRAGMA query_only = OFF")


# ---------------------------------------------------------------------------
# Typed not-found and unsupported provenance
# ---------------------------------------------------------------------------


def test_unknown_receipt_raises_typed_not_found(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(ReceiptNotFoundError):
        report_with_zero_effects(conn, "rcpt_does_not_exist")
    with pytest.raises(ReceiptNotFoundError):
        report_with_zero_effects(conn, "")
    with pytest.raises(ReceiptNotFoundError):
        report_with_zero_effects(conn, None)  # type: ignore[arg-type]
    assert not conn.in_transaction


def test_legacy_receipt_never_trusted_from_canonical_text_alone(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A non-registry receipt is unsupported even with canonical text populated."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    payer = conn.execute("SELECT id FROM participants WHERE public_id = 'person_owner'").fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, net_paid_amount, "
        "net_paid_amount_canonical_text, currency, payer_participant_id) "
        "VALUES ('rcpt_legacy_shape', 'LEGACY CAFE', 12.34, '12.34', 'SGD', ?)",
        (payer,),
    )
    conn.commit()

    with pytest.raises(UnsupportedReceiptProvenanceError):
        report_with_zero_effects(conn, "rcpt_legacy_shape")


def test_legacy_numeric_only_receipt_is_unsupported(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    payer = conn.execute("SELECT id FROM participants WHERE public_id = 'person_owner'").fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, net_paid_amount, currency, "
        "payer_participant_id) VALUES ('rcpt_legacy_plain', 'OLD ROW', 9.99, 'SGD', ?)",
        (payer,),
    )
    conn.commit()

    with pytest.raises(UnsupportedReceiptProvenanceError):
        report_with_zero_effects(conn, "rcpt_legacy_plain")


# ---------------------------------------------------------------------------
# Monetary corruption fails closed (never an ordinary not-ready report)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        ("net_paid_amount_canonical_text", None, "missing the authoritative"),
        ("net_paid_amount_canonical_text", "012.34", "byte-exact canonical"),
        ("net_paid_amount_canonical_text", "12.3", "byte-exact canonical"),
        ("net_paid_amount_canonical_text", "12.345", "Money Contract"),
        ("net_paid_amount_canonical_text", "-12.34", "strictly positive"),
        ("net_paid_amount_canonical_text", "0.00", "strictly positive"),
        ("net_paid_amount_canonical_text", "-0.00", "strictly positive"),
        ("net_paid_amount_canonical_text", "1E2", "Money Contract"),
        ("net_paid_amount_canonical_text", "abc", "Money Contract"),
        ("currency", "THB", "Money Contract"),
        ("currency", "sgd", "canonical form"),
    ],
)
def test_corrupt_canonical_monetary_state_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    column: str,
    value: Any,
    match: str,
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "moncorrupt")
    corrupt_receipt_row(conn, f"UPDATE receipts SET {column} = ? WHERE id = ?", (value, receipt_id))

    with pytest.raises(ReceiptFactsIntegrityError, match=match):
        report_with_zero_effects(conn, receipt_public_id)


@pytest.mark.parametrize(
    "mirror_value",
    ["12.35", "not-a-number"],
    ids=["decimal-drift", "lossy-text-storage"],
)
def test_numeric_mirror_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, mirror_value: str
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "mirrordrift")
    # Plain UPDATE routes the bound value through the column's NUMERIC
    # affinity: numeric-looking text is stored as REAL drift, while
    # non-numeric text stays TEXT — the lossy storage class the decoder
    # must refuse.
    corrupt_receipt_row(
        conn,
        "UPDATE receipts SET net_paid_amount = ? WHERE id = ?",
        (mirror_value, receipt_id),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="mirror"):
        report_with_zero_effects(conn, receipt_public_id)


# ---------------------------------------------------------------------------
# Registry/receipt identity and lineage drift fails closed
# ---------------------------------------------------------------------------


def test_receipt_public_id_derivation_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    _, receipt_id, _ = convert_receipt(conn, tmp_path, "iddrift")
    forged = "rcpt_" + "f" * 32
    corrupt_receipt_row(
        conn, "UPDATE receipts SET public_id = ? WHERE id = ?", (forged, receipt_id)
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="deterministic derivation"):
        report_with_zero_effects(conn, forged)


def test_parser_output_lineage_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "lineagedrift")
    corrupt_receipt_row(
        conn, "UPDATE receipts SET parser_output_id = NULL WHERE id = ?", (receipt_id,)
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="parser output lineage"):
        report_with_zero_effects(conn, receipt_public_id)


def test_lifecycle_status_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """No guarded lifecycle boundary exists yet, so status drift is untrusted."""
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "statusdrift")
    # status is lifecycle-mutable at the schema level; no trigger drop needed.
    conn.execute("UPDATE receipts SET status = 'voided' WHERE id = ?", (receipt_id,))
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="'confirmed'"):
        report_with_zero_effects(conn, receipt_public_id)


def test_never_written_column_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "notesdrift")
    conn.execute("UPDATE receipts SET notes = 'ad-hoc note' WHERE id = ?", (receipt_id,))
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="never writes"):
        report_with_zero_effects(conn, receipt_public_id)


# ---------------------------------------------------------------------------
# Payer / membership facts fail closed
# ---------------------------------------------------------------------------


def drop_membership_guards(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS trg_receipt_participants_conversion_bound_freeze")
    conn.execute("DROP TRIGGER IF EXISTS trg_receipt_participants_conversion_bound_no_delete")


def test_missing_membership_rows_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "nomembers")
    drop_membership_guards(conn)
    conn.execute("DELETE FROM receipt_participants WHERE receipt_id = ?", (receipt_id,))
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="no membership rows"):
        report_with_zero_effects(conn, receipt_public_id)


def test_missing_payer_membership_row_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "nopayer")
    drop_membership_guards(conn)
    conn.execute(
        "UPDATE receipt_participants SET role = 'participant' "
        "WHERE receipt_id = ? AND role = 'payer'",
        (receipt_id,),
    )
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="exactly one payer"):
        report_with_zero_effects(conn, receipt_public_id)


def test_contradictory_role_inclusion_facts_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "rolecontra")
    drop_membership_guards(conn)
    conn.execute(
        "UPDATE receipt_participants SET role = 'excluded' "
        "WHERE receipt_id = ? AND role = 'participant' AND is_included = 1",
        (receipt_id,),
    )
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="contradictory"):
        report_with_zero_effects(conn, receipt_public_id)


def test_membership_referencing_missing_participant_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "ghostmember")
    conn.close()

    # A separate connection without foreign-key enforcement forges the
    # orphaned-membership state the FK would normally prevent.
    forger = sqlite3.connect(str(migrated_temp_db_path))
    forger.row_factory = sqlite3.Row
    try:
        forger.execute("DELETE FROM participants WHERE public_id = 'person_alice'")
        forger.commit()
        with pytest.raises(ReceiptFactsIntegrityError, match="do not exist"):
            report_with_zero_effects(forger, receipt_public_id)
    finally:
        forger.close()


# ---------------------------------------------------------------------------
# Unexpected item/allocation/adjustment state never becomes ready
# ---------------------------------------------------------------------------


def add_adhoc_item(conn: sqlite3.Connection, receipt_id: int, suffix: str) -> int:
    cursor = conn.execute(
        "INSERT INTO receipt_items (public_id, receipt_id, item_name, "
        "line_amount, currency) VALUES (?, ?, 'AD HOC ITEM', 12.34, 'SGD')",
        (f"rcit_adhoc_{suffix}", receipt_id),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def test_adhoc_item_rows_are_rejected_not_ready_speculation(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "adhocitem")
    add_adhoc_item(conn, receipt_id, "only")

    with pytest.raises(ReceiptFactsIntegrityError, match="no guarded boundary"):
        report_with_zero_effects(conn, receipt_public_id)


def test_adhoc_item_and_allocation_rows_never_report_ready(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "adhocalloc")
    item_id = add_adhoc_item(conn, receipt_id, "alloc")
    participant = conn.execute(
        "SELECT id FROM participants WHERE public_id = 'person_owner'"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO receipt_item_allocations (public_id, receipt_item_id, "
        "participant_id, share_amount_before_service_charge, allocation_method) "
        "VALUES ('rcia_adhoc', ?, ?, 12.34, 'equal_amount')",
        (item_id, participant),
    )
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="no guarded boundary"):
        report_with_zero_effects(conn, receipt_public_id)


def test_adhoc_adjustment_rows_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "adhocadj")
    conn.execute(
        "INSERT INTO receipt_adjustments (public_id, receipt_id, adjustment_type, "
        "amount, currency, direction, allocation_method) "
        "VALUES ('rcad_adhoc', ?, 'service_charge', 1.00, 'SGD', 'add', "
        "'proportional_by_item_amount')",
        (receipt_id,),
    )
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="no guarded boundary"):
        report_with_zero_effects(conn, receipt_public_id)


# ---------------------------------------------------------------------------
# Database safety: staging guard protects live and copied databases
# ---------------------------------------------------------------------------


def test_plain_database_rejected(tmp_path: Path) -> None:
    plain_path = tmp_path / "plain_untrusted.sqlite"
    conn = sqlite3.connect(str(plain_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ReadinessStagingDatabaseRejectedError):
            report_receipt_calculator_readiness(conn, "rcpt_any")
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


def test_copied_staging_database_rejected(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    copied_path = tmp_path / "copied_staging.sqlite"
    shutil.copy2(migrated_temp_db_path, copied_path)
    conn = sqlite3.connect(str(copied_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ReadinessStagingDatabaseRejectedError):
            report_receipt_calculator_readiness(conn, "rcpt_any")
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_live_database_rejected_via_readonly_connection() -> None:
    conn = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ReadinessStagingDatabaseRejectedError):
            report_receipt_calculator_readiness(conn, "rcpt_any")
        assert not conn.in_transaction
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Shared-helper extraction regression: B4.1 behaviour is unchanged
# ---------------------------------------------------------------------------


def test_shared_mirror_decoder_is_the_b41_contract_function() -> None:
    """B4.2 consumes the exact B4.1 mirror-decoding function, not a copy."""
    import finance_core.calculators.receipt_calculator_readiness as readiness
    from finance_core.parser_proposals.receipt_facts_conversion import decimal_from_numeric_mirror

    assert readiness.decimal_from_numeric_mirror is decimal_from_numeric_mirror


def test_conversion_then_readiness_replay_and_conversion_replay_agree(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Readiness reads do not disturb B4.1 idempotent replay behaviour."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "sharedreg")
    cmd = command("sharedreg", public_id, expected)
    first = convert(conn, cmd)

    report_before = report_receipt_calculator_readiness(conn, first.receipt_public_id)
    replay = convert(conn, cmd)
    report_after = report_receipt_calculator_readiness(conn, first.receipt_public_id)

    assert replay.idempotent is True
    assert dataclasses.asdict(replay) == {
        **dataclasses.asdict(first),
        "idempotent": True,
    }
    assert report_before == report_after
    assert pid == first.parser_output_id


# ---------------------------------------------------------------------------
# Zero effects hold on the failure paths exercised above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failing_call",
    [
        lambda conn: report_receipt_calculator_readiness(conn, "rcpt_missing"),
        lambda conn: report_receipt_calculator_readiness(conn, ""),
    ],
    ids=["not-found", "empty-identity"],
)
def test_failure_paths_leave_database_byte_identical(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    failing_call: Callable[[sqlite3.Connection], Any],
) -> None:
    conn = migrated_temp_db_connection
    convert_receipt(conn, tmp_path, "zeroeffect")
    before_counts = table_counts(conn)
    before_rows = all_rows(conn)

    with pytest.raises(ReceiptNotFoundError):
        failing_call(conn)

    assert table_counts(conn) == before_counts
    assert all_rows(conn) == before_rows
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# Registry-side drift and lineage resolution fail closed (review round 1)
# ---------------------------------------------------------------------------


def corrupt_registry_row(db_path: Path, sql: str, params: tuple[Any, ...]) -> None:
    """Forge registry drift past the append-only guards (fixture surgery).

    Uses a separate plain connection: foreign keys default OFF there, so
    FK-violating drift vectors are reachable, exactly like historical
    corruption would be.
    """
    forger = sqlite3.connect(str(db_path))
    try:
        forger.execute("DROP TRIGGER IF EXISTS trg_receipt_proposal_conversions_no_update")
        forger.execute("PRAGMA ignore_check_constraints = ON")
        forger.execute(sql, params)
        forger.commit()
    finally:
        forger.close()


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        ("command_public_id", "xpfc_bad", "rpfc_ identity pattern"),
        ("command_public_id", "rpfc_forged", "deterministic derivation"),
        ("parser_output_id", 999999, "parser output lineage"),
        ("schema_version", "v2", "schema_version"),
        ("actor_type", "agent", "actor_type"),
        ("authenticated_actor_id", "   ", "missing or empty"),
        ("conversion_result_hash", "XYZ", "64-hex"),
        ("proposal_content_hash", "a" * 64, "confirmation binding"),
        ("confirmation_public_id", "pca_ghost", "confirmation authorization row"),
    ],
)
def test_registry_row_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
    column: str,
    value: Any,
    match: str,
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, command_id = convert_receipt(conn, tmp_path, "regdrift")
    corrupt_registry_row(
        migrated_temp_db_path,
        f"UPDATE receipt_proposal_conversions SET {column} = ? WHERE command_public_id = ?",
        (value, command_id),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match=match):
        report_with_zero_effects(conn, receipt_public_id)


def test_dangling_parser_output_lineage_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """Registry and receipt agree on a parser output that no longer exists."""
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "ghostproposal")
    parser_output_id = conn.execute(
        "SELECT parser_output_id FROM receipts WHERE id = ?", (receipt_id,)
    ).fetchone()["parser_output_id"]
    forger = sqlite3.connect(str(migrated_temp_db_path))
    try:
        forger.execute("DELETE FROM parser_outputs WHERE id = ?", (parser_output_id,))
        forger.commit()
    finally:
        forger.close()

    with pytest.raises(ReceiptFactsIntegrityError, match="parser_outputs row"):
        report_with_zero_effects(conn, receipt_public_id)


# ---------------------------------------------------------------------------
# Additional payer/membership drift vectors (review round 1)
# ---------------------------------------------------------------------------


def test_two_payer_membership_rows_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "twopayers")
    drop_membership_guards(conn)
    conn.execute(
        "UPDATE receipt_participants SET role = 'payer' WHERE receipt_id = ? AND role = 'excluded'",
        (receipt_id,),
    )
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="exactly one payer"):
        report_with_zero_effects(conn, receipt_public_id)


def test_payer_membership_row_mismatch_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "payermismatch")
    cursor = conn.execute(
        "INSERT INTO participants (public_id, display_name, is_self) "
        "VALUES ('person_carol', 'Carol', 0)"
    )
    carol_id = cursor.lastrowid
    drop_membership_guards(conn)
    conn.execute(
        "UPDATE receipt_participants SET participant_id = ? "
        "WHERE receipt_id = ? AND role = 'payer'",
        (carol_id, receipt_id),
    )
    conn.commit()

    with pytest.raises(ReceiptFactsIntegrityError, match="payer row does not match"):
        report_with_zero_effects(conn, receipt_public_id)


def test_invalid_is_included_value_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, receipt_id, _ = convert_receipt(conn, tmp_path, "badinclusion")
    drop_membership_guards(conn)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        conn.execute(
            "UPDATE receipt_participants SET is_included = 2 "
            "WHERE receipt_id = ? AND role = 'participant'",
            (receipt_id,),
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(ReceiptFactsIntegrityError, match="explicit 0 or 1"):
        report_with_zero_effects(conn, receipt_public_id)


def test_dangling_payer_participant_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "ghostpayer")
    forger = sqlite3.connect(str(migrated_temp_db_path))
    try:
        forger.execute("DELETE FROM participants WHERE public_id = 'person_owner'")
        forger.commit()
    finally:
        forger.close()

    with pytest.raises(ReceiptFactsIntegrityError, match="payer_participant_id does not resolve"):
        report_with_zero_effects(conn, receipt_public_id)


# ---------------------------------------------------------------------------
# Row-shape hardening and renamed-database rejection (review rounds 2/3)
# ---------------------------------------------------------------------------


def test_row_to_dict_supports_mappings_and_rejects_unknown_shapes() -> None:
    from finance_core.calculators.receipt_calculator_readiness import (
        ReceiptCalculatorReadinessError,
        _row_to_dict,
    )

    assert _row_to_dict((1, 2), ["a", "b"]) == {"a": 1, "b": 2}
    assert _row_to_dict({"a": 1, "b": 2}, ["a", "b"]) == {"a": 1, "b": 2}
    with pytest.raises(ReceiptCalculatorReadinessError, match="partial row"):
        _row_to_dict({"a": 1}, ["a", "b"])
    with pytest.raises(ReceiptCalculatorReadinessError, match="Unsupported sqlite3 row_factory"):
        _row_to_dict(object(), ["a"])


def test_renamed_staging_database_rejected(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    """A staging database renamed to look like the live database is rejected."""
    renamed_path = tmp_path / "finance.db"
    shutil.copy2(migrated_temp_db_path, renamed_path)
    conn = sqlite3.connect(str(renamed_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ReadinessStagingDatabaseRejectedError):
            report_receipt_calculator_readiness(conn, "rcpt_any")
        assert not conn.in_transaction
    finally:
        conn.close()


def test_shared_never_written_columns_are_the_b41_contract_tuple() -> None:
    """B4.2 consumes the exact B4.1 never-written-columns contract, not a copy."""
    import finance_core.calculators.receipt_calculator_readiness as readiness
    from finance_core.parser_proposals.receipt_facts_conversion import NEVER_WRITTEN_RECEIPT_COLUMNS

    assert readiness.NEVER_WRITTEN_RECEIPT_COLUMNS is NEVER_WRITTEN_RECEIPT_COLUMNS


def test_registry_schema_version_check_uses_b41_constant() -> None:
    """The registry version gate is the B4.1 constant, not the report version."""
    import finance_core.calculators.receipt_calculator_readiness as readiness
    from finance_core.parser_proposals.receipt_facts_conversion import CONVERSION_SCHEMA_VERSION

    assert readiness.CONVERSION_SCHEMA_VERSION is CONVERSION_SCHEMA_VERSION
    # The report contract version is a deliberately distinct namespace; it
    # may diverge from the registry version in the future without silently
    # rejecting valid v1 registry rows.
    assert readiness.READINESS_SCHEMA_VERSION == "v1"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("actor_type", "agent"),
        ("parser_output_id", 999999),
    ],
    ids=["actor-drift", "proposal-repoint"],
)
def test_confirmation_record_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
    column: str,
    value: Any,
) -> None:
    """A forged confirmation authorization row breaks the registry binding."""
    conn = migrated_temp_db_connection
    receipt_public_id, _, command_id = convert_receipt(conn, tmp_path, "confdrift")
    confirmation_id = conn.execute(
        "SELECT confirmation_public_id FROM receipt_proposal_conversions "
        "WHERE command_public_id = ?",
        (command_id,),
    ).fetchone()["confirmation_public_id"]
    corrupt_registry_row(
        migrated_temp_db_path,
        f"UPDATE parser_proposal_authorizations SET {column} = ? WHERE confirmation_public_id = ?",
        (value, confirmation_id),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="confirmation binding"):
        report_with_zero_effects(conn, receipt_public_id)


# ---------------------------------------------------------------------------
# IAF.5 positive readiness (design Section 15, approved IA-D10)
# ---------------------------------------------------------------------------


def persist_fact_set(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str, **overrides: Any
) -> tuple[ConvertedReceipt, ReceiptItemAllocationFactsResult]:
    """Create a real receipt and one human-authored active fact set."""
    ctx = setup_receipt(conn, tmp_path, suffix)
    result = persist_receipt_item_allocation_facts(conn, iaf_command(suffix, ctx, **overrides))
    return ctx, result


def forge(
    conn: sqlite3.Connection, drop: tuple[str, ...], sql: str, params: tuple[Any, ...]
) -> None:
    """Bypass the append-only guards to plant persisted drift (fixture surgery)."""
    for name in drop:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")


def test_active_fact_set_reports_ready_with_exact_additive_fields(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_fact_set(conn, tmp_path, "posready")

    report = report_with_zero_effects(conn, ctx.receipt_public_id)

    assert report.is_calculator_ready is True
    assert report.not_ready_reasons == ()
    assert report.receipt_public_id == ctx.receipt_public_id
    assert report.conversion_command_public_id == ctx.conversion_command_public_id
    assert report.active_fact_set_public_id == result.fact_set_public_id
    assert report.active_fact_set_version == 1
    assert report.fact_set_result_hash == result.fact_set_result_hash
    assert report.item_count == 2
    assert report.allocation_count == 4
    assert report.adjustment_count == 1
    assert report.currency == "SGD"
    assert report.net_paid_amount_canonical_text == "12.34"
    assert not conn.in_transaction


def test_zero_adjustment_count_boundary_reports_ready(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A single-item, no-adjustment fact set reconciles and is ready."""
    conn = migrated_temp_db_connection
    ctx, result = persist_fact_set(
        conn,
        tmp_path,
        "zeroadj",
        items=replacement_items(),
        allocations=replacement_allocations(),
        adjustments=[],
    )

    report = report_with_zero_effects(conn, ctx.receipt_public_id)

    assert report.is_calculator_ready is True
    assert report.item_count == 1
    assert report.allocation_count == 2
    assert report.adjustment_count == 0
    assert report.fact_set_result_hash == result.fact_set_result_hash


def test_ready_report_is_immutable_and_deterministic_across_reconnect(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_fact_set(conn, tmp_path, "posdet")

    first = report_with_zero_effects(conn, ctx.receipt_public_id)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.is_calculator_ready = False  # type: ignore[misc]
    second = report_with_zero_effects(conn, ctx.receipt_public_id)
    assert first == second

    fresh = connect_temp_db(migrated_temp_db_path)
    try:
        assert report_with_zero_effects(fresh, ctx.receipt_public_id) == first
    finally:
        fresh.close()


def test_ready_under_query_only_pragma(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_fact_set(conn, tmp_path, "posqonly")
    conn.execute("PRAGMA query_only = ON")
    try:
        report = report_receipt_calculator_readiness(conn, ctx.receipt_public_id)
        assert report.is_calculator_ready is True
    finally:
        conn.execute("PRAGMA query_only = OFF")


def test_ready_preserves_caller_owned_read_transaction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_fact_set(conn, tmp_path, "postx")
    conn.execute("BEGIN")
    try:
        assert conn.in_transaction
        report = report_receipt_calculator_readiness(conn, ctx.receipt_public_id)
        assert conn.in_transaction
        assert report.is_calculator_ready is True
    finally:
        conn.rollback()
    assert not conn.in_transaction


def test_ready_does_not_invoke_the_calculator(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness is SELECT-only and never runs the deterministic calculator."""
    import finance_core.calculators.receipt_split_calculator as calc

    calls: list[Any] = []
    original = calc.calculate_receipt_split

    def spy(case_data: Any) -> Any:
        calls.append(case_data)
        return original(case_data)

    monkeypatch.setattr(calc, "calculate_receipt_split", spy)
    conn = migrated_temp_db_connection
    ctx, _ = persist_fact_set(conn, tmp_path, "posnospy")

    report = report_with_zero_effects(conn, ctx.receipt_public_id)

    assert report.is_calculator_ready is True
    assert calls == []


def test_supersession_reports_only_the_new_active_version(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, v1 = persist_fact_set(conn, tmp_path, "possup")
    v2 = supersede_receipt_item_allocation_facts(conn, correction_command("possup_v2", ctx, v1))

    report = report_with_zero_effects(conn, ctx.receipt_public_id)

    assert report.is_calculator_ready is True
    assert report.active_fact_set_public_id == v2.fact_set_public_id
    assert report.active_fact_set_version == 2
    assert report.fact_set_result_hash == v2.fact_set_result_hash
    assert report.item_count == 1
    assert report.allocation_count == 2
    assert report.adjustment_count == 0


def test_forged_two_active_state_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, v1 = persist_fact_set(conn, tmp_path, "postwo")
    supersede_receipt_item_allocation_facts(conn, correction_command("postwo_v2", ctx, v1))
    # Force the superseded v1 registry row back to active alongside v2.
    forge(
        conn,
        (
            "trg_receipt_item_allocation_fact_sets_single_transition",
            "idx_receipt_item_allocation_fact_sets_one_active",
        ),
        "UPDATE receipt_item_allocation_fact_sets "
        "SET superseded_by_fact_set_public_id = NULL WHERE fact_set_public_id = ?",
        (v1.fact_set_public_id,),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="active versions"):
        report_with_zero_effects(conn, ctx.receipt_public_id)


def test_forged_input_hash_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_fact_set(conn, tmp_path, "poshash")
    forge(
        conn,
        ("trg_receipt_item_allocation_fact_sets_single_transition",),
        "UPDATE receipt_item_allocation_fact_sets SET fact_set_input_hash = ? "
        "WHERE fact_set_public_id = ?",
        ("0" * 64, result.fact_set_public_id),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="service-depth"):
        report_with_zero_effects(conn, ctx.receipt_public_id)


def test_forged_result_hash_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_fact_set(conn, tmp_path, "posres")
    forge(
        conn,
        ("trg_receipt_item_allocation_fact_sets_single_transition",),
        "UPDATE receipt_item_allocation_fact_sets SET fact_set_result_hash = ? "
        "WHERE fact_set_public_id = ?",
        ("f" * 64, result.fact_set_public_id),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="service-depth"):
        report_with_zero_effects(conn, ctx.receipt_public_id)


def test_forged_canonical_text_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_fact_set(conn, tmp_path, "postext")
    item_public_id = derive_item_public_id(result.fact_set_public_id, 1)
    forge(
        conn,
        ("trg_receipt_items_fact_set_bound_freeze",),
        "UPDATE receipt_items SET line_amount_canonical_text = '9.99' WHERE public_id = ?",
        (item_public_id,),
    )

    with pytest.raises(ReceiptFactsIntegrityError, match="service-depth"):
        report_with_zero_effects(conn, ctx.receipt_public_id)


def test_null_fact_set_id_adhoc_item_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_fact_set(conn, tmp_path, "posadhoc")
    add_adhoc_item(conn, ctx.receipt_id, "posadhoc")

    with pytest.raises(ReceiptFactsIntegrityError, match="service-depth"):
        report_with_zero_effects(conn, ctx.receipt_public_id)


def test_forged_membership_exclusion_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An allocation to a member forged to excluded fails closed."""
    conn = migrated_temp_db_connection
    ctx, _ = persist_fact_set(conn, tmp_path, "posmember")
    forge(
        conn,
        ("trg_receipt_participants_conversion_bound_freeze",),
        "UPDATE receipt_participants SET is_included = 0, role = 'excluded' "
        "WHERE receipt_id = ? AND participant_id = "
        "(SELECT id FROM participants WHERE public_id = 'person_alice')",
        (ctx.receipt_id,),
    )

    with pytest.raises(ReceiptFactsIntegrityError):
        report_with_zero_effects(conn, ctx.receipt_public_id)


def test_ready_report_default_additive_fields_are_none_without_fact_set(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The backward-compatible not-ready report leaves additive fields None."""
    conn = migrated_temp_db_connection
    receipt_public_id, _, _ = convert_receipt(conn, tmp_path, "posnone")

    report = report_with_zero_effects(conn, receipt_public_id)

    assert report.is_calculator_ready is False
    assert report.not_ready_reasons == EXPECTED_NOT_READY_REASONS
    assert report.active_fact_set_public_id is None
    assert report.active_fact_set_version is None
    assert report.fact_set_result_hash is None
    assert report.item_count is None
    assert report.allocation_count is None
    assert report.adjustment_count is None
    assert report.currency is None
    assert report.net_paid_amount_canonical_text is None
