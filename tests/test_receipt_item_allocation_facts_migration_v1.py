"""Constraint-level tests for migration 036 (IAF.1 fact-set schema).

Every test here bypasses the (not yet existing) IAF service layers and
drives raw SQL at migrated temporary databases, proving that migration
036's CHECK constraints, foreign keys (immediate and deferred), partial
unique indexes, and append-only/collision/freeze triggers each fail
closed on their own.  The frozen Section 11.1 supersession ordering of
docs/design/receipt_item_allocation_facts_boundary_v1.md is proven at
constraint level (proofs P1-P7); no IAF.2+ runtime behaviour exists or
is exercised.  Only disposable SQLite databases under tmp_path are used.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from finance_core.reconciliation.migrations import MIGRATIONS_DIR, TEMP_DB_MIGRATION_PATHS
from tests.conftest import apply_migrations, connect_temp_db
from tests.test_receipt_facts_conversion_v1 import participant_id, seed_people

pytestmark = pytest.mark.migrated_staging_snapshot

MIGRATION_036_FILENAME = "036_receipt_item_allocation_facts.sql"

# Trigger abort messages defined by migration 036.
_FACT_SET_COLLISION = "UNIQUE fact set identity collision"
_ITEM_COLLISION = "UNIQUE receipt item identity collision"
_ADJUSTMENT_COLLISION = "UNIQUE receipt adjustment identity collision"
_ALLOCATION_COLLISION = "UNIQUE allocation fact identity collision"
_SINGLE_TRANSITION = "append-only except the single NULL to successor supersession transition"
_BORN_ACTIVE = "must be inserted active with a NULL superseded_by_fact_set_public_id"
_CONVERSION_BINDING = "must bind the live conversion registry row of the same receipt"
_SUPERSEDES_LINEAGE = "must reference the same-receipt predecessor with the previous version"
_ITEM_RECEIPT_MATCH = "fact-set-bound receipt_items rows must reference a fact set registry row"
_ITEM_FREEZE = "fact-set-bound receipt_items rows are append-only"
_ADJ_RECEIPT_MATCH = "fact-set-bound receipt_adjustments rows must reference a fact set registry"
_ADJ_FREEZE = "fact-set-bound receipt_adjustments rows are append-only"
_ALLOCATION_BOUND_ITEM = "allocation facts must reference a receipt_items row bound to the same"


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConversionContext:
    """Valid FK donor chain: parser output -> authorization -> receipt -> conversion."""

    receipt_id: int
    receipt_public_id: str
    conversion_command_public_id: str
    conversion_result_hash: str


def seed_conversion_context(
    conn: sqlite3.Connection,
    suffix: str,
    *,
    currency: str = "SGD",
    net_paid: str = "30.00",
) -> ConversionContext:
    """Seed a B4.1 conversion-bound receipt entirely through direct SQL."""
    cursor = conn.execute(
        "INSERT INTO parser_outputs (public_id, source_type) VALUES (?, 'receipt_image')",
        (f"po_iaf_{suffix}",),
    )
    assert cursor.lastrowid is not None
    parser_output_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO parser_proposal_authorizations (
            confirmation_public_id, parser_output_id, proposal_content_hash,
            actor_type, authenticated_actor_id, confirmation_state,
            confirmation_channel, decided_at
        ) VALUES (?, ?, ?, 'human', 'owner', 'confirmed', 'telegram', '2026-07-28T00:00:00Z')
        """,
        (f"pca_iaf_{suffix}", parser_output_id, _hex64(f"proposal-{suffix}")),
    )
    cursor = conn.execute(
        """
        INSERT INTO receipts (
            public_id, merchant, net_paid_amount, net_paid_amount_canonical_text,
            currency, payer_participant_id, parser_output_id, status
        ) VALUES (?, 'IAF Cafe', ?, ?, ?, ?, ?, 'confirmed')
        """,
        (
            f"rcpt_iaf_{suffix}",
            net_paid,
            net_paid,
            currency,
            participant_id(conn, "person_owner"),
            parser_output_id,
        ),
    )
    assert cursor.lastrowid is not None
    receipt_id = int(cursor.lastrowid)
    conversion_result_hash = _hex64(f"conversion-result-{suffix}")
    conn.execute(
        """
        INSERT INTO receipt_proposal_conversions (
            command_public_id, parser_output_id, supersession_root_parser_output_id,
            receipt_id, confirmation_public_id, proposal_content_hash,
            command_material_hash, conversion_result_hash, actor_type,
            authenticated_actor_id, conversion_channel
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', 'owner', 'telegram')
        """,
        (
            f"rpfc_iaf_{suffix}",
            parser_output_id,
            parser_output_id,
            receipt_id,
            f"pca_iaf_{suffix}",
            _hex64(f"proposal-{suffix}"),
            _hex64(f"conversion-material-{suffix}"),
            conversion_result_hash,
        ),
    )
    conn.commit()
    return ConversionContext(
        receipt_id=receipt_id,
        receipt_public_id=f"rcpt_iaf_{suffix}",
        conversion_command_public_id=f"rpfc_iaf_{suffix}",
        conversion_result_hash=conversion_result_hash,
    )


def seed_audit_event(
    conn: sqlite3.Connection,
    event_public_id: str,
    aggregate_public_id: str,
    *,
    sequence: int = 1,
) -> None:
    """Insert a minimal, chain-shape-valid financial audit event donor row."""
    conn.execute(
        """
        INSERT INTO financial_audit_events (
            event_public_id, audit_schema_version, aggregate_type,
            aggregate_public_id, event_type, event_payload_json,
            previous_state_json, new_state_json, previous_state_hash,
            new_state_hash, previous_event_hash, event_hash, actor_type,
            actor_public_id, correlation_public_id, causation_public_id,
            sequence_number, created_at
        ) VALUES (?, 'v1', 'receipt', ?, 'receipt_item_allocation_facts_persisted',
                  '{}', '{}', '{}', ?, ?, ?, ?, 'human', 'owner', ?, ?, ?,
                  '2026-07-28T00:00:00Z')
        """,
        (
            event_public_id,
            aggregate_public_id,
            _hex64(f"{event_public_id}-prev-state"),
            _hex64(f"{event_public_id}-new-state"),
            _hex64(f"{event_public_id}-prev-event"),
            _hex64(f"{event_public_id}-event"),
            f"{event_public_id}_corr",
            f"{event_public_id}_cause",
            sequence,
        ),
    )


# Full column list of receipt_item_allocation_fact_sets so every direct
# INSERT exercises the real write shape rather than column defaults.
_FACT_SET_COLUMNS = (
    "command_public_id",
    "fact_set_public_id",
    "receipt_id",
    "version",
    "conversion_command_public_id",
    "expected_conversion_result_hash",
    "supersedes_fact_set_public_id",
    "superseded_by_fact_set_public_id",
    "command_material_hash",
    "fact_set_input_hash",
    "fact_set_result_hash",
    "canonical_fact_set_payload",
    "actor_type",
    "authenticated_actor_id",
    "channel",
    "reason",
    "audit_event_public_id",
    "schema_version",
    "created_at",
)


def make_fact_set_row(
    ctx: ConversionContext, *, suffix: str, version: int = 1, **overrides: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "command_public_id": f"riaf_{suffix}",
        "fact_set_public_id": f"rfs_{suffix}",
        "receipt_id": ctx.receipt_id,
        "version": version,
        "conversion_command_public_id": ctx.conversion_command_public_id,
        "expected_conversion_result_hash": ctx.conversion_result_hash,
        "supersedes_fact_set_public_id": None,
        "superseded_by_fact_set_public_id": None,
        "command_material_hash": _hex64(f"material-{suffix}"),
        "fact_set_input_hash": _hex64(f"input-{suffix}"),
        "fact_set_result_hash": _hex64(f"result-{suffix}"),
        "canonical_fact_set_payload": '{"items": []}',
        "actor_type": "human",
        "authenticated_actor_id": "owner",
        "channel": "telegram",
        "reason": None,
        "audit_event_public_id": f"fae_iaf_{suffix}",
        "schema_version": "v1",
        "created_at": "2026-07-28T00:00:00.000000Z",
    }
    row.update(overrides)
    return row


def insert_fact_set(
    conn: sqlite3.Connection, row: dict[str, Any], *, or_replace: bool = False
) -> None:
    columns = ", ".join(_FACT_SET_COLUMNS)
    placeholders = ", ".join("?" for _ in _FACT_SET_COLUMNS)
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    conn.execute(
        f"{verb} INTO receipt_item_allocation_fact_sets ({columns}) VALUES ({placeholders})",
        tuple(row[column] for column in _FACT_SET_COLUMNS),
    )


def seed_active_fact_set(
    conn: sqlite3.Connection,
    ctx: ConversionContext,
    *,
    suffix: str,
    version: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    """Registry row first, audit event second (the Section 13 insert order),
    then COMMIT — the deferred audit FK is satisfied at commit time."""
    row = make_fact_set_row(ctx, suffix=suffix, version=version, **overrides)
    insert_fact_set(conn, row)
    seed_audit_event(conn, row["audit_event_public_id"], ctx.receipt_public_id)
    conn.commit()
    return row


def _expect_insert_rejected(
    conn: sqlite3.Connection, ctx: ConversionContext, match: str, **overrides: Any
) -> None:
    row = make_fact_set_row(ctx, suffix="bad", **overrides)
    with pytest.raises(sqlite3.IntegrityError, match=match):
        insert_fact_set(conn, row)
    conn.rollback()


def insert_bound_item(
    conn: sqlite3.Connection,
    receipt_id: int,
    fact_set_id: str | None,
    *,
    suffix: str,
    line_number: int | None = 1,
    line_amount_canonical: str | None = "10.00",
    quantity_canonical: str | None = None,
    unit_price_canonical: str | None = None,
    currency: str = "SGD",
    or_replace: bool = False,
) -> int:
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    cursor = conn.execute(
        f"""
        {verb} INTO receipt_items (
            public_id, receipt_id, line_number, item_name, line_amount,
            currency, quantity_canonical_text, unit_price_canonical_text,
            line_amount_canonical_text, fact_set_id
        ) VALUES (?, ?, ?, 'Item', ?, ?, ?, ?, ?, ?)
        """,
        (
            f"ritem_iaf_{suffix}",
            receipt_id,
            line_number,
            line_amount_canonical if line_amount_canonical is not None else "10.00",
            currency,
            quantity_canonical,
            unit_price_canonical,
            line_amount_canonical,
            fact_set_id,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def insert_bound_adjustment(
    conn: sqlite3.Connection,
    receipt_id: int,
    fact_set_id: str | None,
    *,
    suffix: str,
    adjustment_index: int | None = 1,
    amount_canonical: str | None = "3.00",
    adjustment_type: str = "service_charge",
    direction: str = "add",
    allocation_method: str = "proportional_by_item_amount",
    description: str | None = None,
    currency: str = "SGD",
    or_replace: bool = False,
) -> int:
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    cursor = conn.execute(
        f"""
        {verb} INTO receipt_adjustments (
            public_id, receipt_id, adjustment_type, description, amount,
            currency, direction, allocation_method, adjustment_index,
            amount_canonical_text, fact_set_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"radj_iaf_{suffix}",
            receipt_id,
            adjustment_type,
            description,
            amount_canonical if amount_canonical is not None else "3.00",
            currency,
            direction,
            allocation_method,
            adjustment_index,
            amount_canonical,
            fact_set_id,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def insert_allocation_fact(
    conn: sqlite3.Connection,
    fact_set_id: str,
    receipt_item_id: int,
    participant: int,
    *,
    suffix: str,
    allocation_method: str = "manual",
    share_canonical: str | None = "5.00",
    share_mirror: Any | None = "5.00",
    or_replace: bool = False,
) -> None:
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    conn.execute(
        f"""
        {verb} INTO receipt_item_allocation_facts (
            allocation_public_id, fact_set_id, receipt_item_id,
            participant_id, allocation_method, share_amount_canonical_text,
            share_amount
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"rfsa_iaf_{suffix}",
            fact_set_id,
            receipt_item_id,
            participant,
            allocation_method,
            share_canonical,
            share_mirror,
        ),
    )


# -----------------------------------------------------------------------
# Migration and upgrade
# -----------------------------------------------------------------------


def test_migration_036_applies_with_clean_ledger_and_integrity(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    row = conn.execute(
        "SELECT migration_id, migration_filename, migration_sequence, adoption_mode "
        "FROM schema_migrations WHERE migration_id = ?",
        ("036",),
    ).fetchone()
    assert row is not None
    assert row["migration_id"] == "036"
    assert row["migration_filename"] == MIGRATION_036_FILENAME
    assert row["migration_sequence"] == 36
    assert row["adoption_mode"] == "applied"

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    assert integrity[0] == "ok"


def test_manifest_includes_036_in_exact_sequence_position() -> None:
    """Migration 036 sits at sequence 36 in the ordered manifest.

    It is no longer the final entry (migration 037 was appended), so the
    contract asserted here is its immutable position, not that it is last.
    """
    paths = tuple(TEMP_DB_MIGRATION_PATHS)
    assert paths[35] == MIGRATIONS_DIR / MIGRATION_036_FILENAME
    assert [path.name for path in paths] == sorted(path.name for path in paths)


def test_upgrade_preserves_legacy_rows_byte_identical(temp_db_path: Path) -> None:
    """Apply 001-035, seed legacy rows, upgrade to 036: no row is rewritten,
    no legacy row is adopted, and the legacy allocation table is untouched."""
    conn = connect_temp_db(temp_db_path)
    assert TEMP_DB_MIGRATION_PATHS[35].name == MIGRATION_036_FILENAME
    pre_036 = TEMP_DB_MIGRATION_PATHS[:35]  # Manifest slice keeps preflight bindings.
    apply_migrations(conn, pre_036)
    conn.commit()
    seed_people(conn)
    cursor = conn.execute(
        """
        INSERT INTO receipts (public_id, merchant, net_paid_amount, currency, payer_participant_id)
        VALUES ('rcpt_legacy_up', 'Legacy Cafe', '21.40', 'SGD', ?)
        """,
        (participant_id(conn, "person_owner"),),
    )
    receipt_id = int(cursor.lastrowid or 0)
    cursor = conn.execute(
        """
        INSERT INTO receipt_items (public_id, receipt_id, line_number, item_name,
                                   line_amount, currency)
        VALUES ('ritem_legacy_up', ?, 1, 'Legacy Item', '20.00', 'SGD')
        """,
        (receipt_id,),
    )
    item_id = int(cursor.lastrowid or 0)
    conn.execute(
        """
        INSERT INTO receipt_item_allocations (public_id, receipt_item_id, participant_id,
                                              share_amount_before_service_charge,
                                              allocation_method)
        VALUES ('ralloc_legacy_up', ?, ?, '20.00', 'equal_amount')
        """,
        (item_id, participant_id(conn, "person_alice")),
    )
    conn.execute(
        """
        INSERT INTO receipt_adjustments (public_id, receipt_id, adjustment_type, amount,
                                         currency, direction, allocation_method)
        VALUES ('radj_legacy_up', ?, 'gst', '1.40', 'SGD', 'informational', 'excluded')
        """,
        (receipt_id,),
    )
    conn.commit()

    def snapshot(table: str) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]

    before = {
        table: snapshot(table)
        for table in (
            "receipts",
            "receipt_items",
            "receipt_item_allocations",
            "receipt_adjustments",
        )
    }
    audit_count_before = conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0]
    legacy_alloc_sql_before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'receipt_item_allocations'"
    ).fetchone()[0]
    legacy_alloc_triggers_before = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'receipt_item_allocations'"
        )
    }
    conn.close()

    conn = connect_temp_db(temp_db_path)
    apply_migrations(conn)
    conn.commit()
    try:
        for table, rows_before in before.items():
            rows_after = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            assert len(rows_after) == len(rows_before)
            for old, new in zip(rows_before, rows_after):
                for column, value in old.items():
                    assert new[column] == value, (table, column)
                for column in set(new) - set(old):
                    assert new[column] is None, (table, column)
        audit_count_after = conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[
            0
        ]
        assert audit_count_after == audit_count_before
        legacy_alloc_sql_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'receipt_item_allocations'"
        ).fetchone()[0]
        assert legacy_alloc_sql_after == legacy_alloc_sql_before
        legacy_alloc_triggers_after = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'receipt_item_allocations'"
            )
        }
        assert legacy_alloc_triggers_after == legacy_alloc_triggers_before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


# -----------------------------------------------------------------------
# Schema inventory
# -----------------------------------------------------------------------


def test_schema_inventory_tables_columns_indexes_triggers(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'receipt_item_allocation_fact%'"
        )
    }
    assert tables == {"receipt_item_allocation_fact_sets", "receipt_item_allocation_facts"}

    item_columns = {row[1] for row in conn.execute("PRAGMA table_info(receipt_items)")}
    assert {
        "fact_set_id",
        "quantity_canonical_text",
        "unit_price_canonical_text",
        "line_amount_canonical_text",
    } <= item_columns

    adjustment_columns = {row[1] for row in conn.execute("PRAGMA table_info(receipt_adjustments)")}
    assert {"fact_set_id", "adjustment_index", "amount_canonical_text"} <= adjustment_columns

    index_sql = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql LIKE '%fact_set%'"
        )
    }
    one_active = index_sql["idx_receipt_item_allocation_fact_sets_one_active"]
    assert "ON receipt_item_allocation_fact_sets(receipt_id)" in one_active
    assert "WHERE superseded_by_fact_set_public_id IS NULL" in one_active
    assert "WHERE fact_set_id IS NOT NULL" in index_sql["idx_receipt_items_fact_set_line_number"]
    assert "WHERE fact_set_id IS NOT NULL" in index_sql["idx_receipt_adjustments_fact_set_index"]

    triggers = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert {
        "trg_receipt_item_allocation_fact_sets_no_delete",
        "trg_receipt_item_allocation_fact_sets_single_transition",
        "trg_receipt_item_allocation_fact_sets_no_insert_collision",
        "trg_receipt_item_allocation_fact_sets_insert_born_active",
        "trg_receipt_item_allocation_fact_sets_conversion_binding",
        "trg_receipt_item_allocation_fact_sets_supersedes_lineage",
        "trg_receipt_items_fact_set_receipt_match",
        "trg_receipt_items_fact_set_bound_freeze",
        "trg_receipt_items_fact_set_bound_no_delete",
        "trg_receipt_items_fact_set_no_insert_collision",
        "trg_receipt_items_fact_set_no_update_collision",
        "trg_receipt_adjustments_fact_set_receipt_match",
        "trg_receipt_adjustments_fact_set_bound_freeze",
        "trg_receipt_adjustments_fact_set_bound_no_delete",
        "trg_receipt_adjustments_fact_set_no_insert_collision",
        "trg_receipt_adjustments_fact_set_no_update_collision",
        "trg_receipt_item_allocation_facts_require_bound_item",
        "trg_receipt_item_allocation_facts_no_update",
        "trg_receipt_item_allocation_facts_no_delete",
        "trg_receipt_item_allocation_facts_no_insert_collision",
    } <= triggers


def test_superseded_by_fk_declared_deferred_and_supersedes_immediate(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Schema-shape regression guard against accidental conversion of the
    deferred FK into an immediate one (frozen Section 11.1 DDL)."""
    conn = migrated_temp_db_connection
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'receipt_item_allocation_fact_sets'"
    ).fetchone()[0]
    supersedes_start = sql.index("supersedes_fact_set_public_id TEXT")
    superseded_by_start = sql.index("superseded_by_fact_set_public_id TEXT")
    hash_start = sql.index("command_material_hash TEXT")
    supersedes_segment = sql[supersedes_start:superseded_by_start]
    superseded_by_segment = sql[superseded_by_start:hash_start]
    assert "DEFERRABLE" not in supersedes_segment
    assert "DEFERRABLE INITIALLY DEFERRED" in superseded_by_segment
    audit_start = sql.index("audit_event_public_id TEXT")
    schema_version_start = sql.index("schema_version TEXT")
    assert "DEFERRABLE INITIALLY DEFERRED" in sql[audit_start:schema_version_start]


# -----------------------------------------------------------------------
# Registry constraints
# -----------------------------------------------------------------------


def test_registry_accepts_valid_version_one_row(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rvalid")
    row = seed_active_fact_set(conn, ctx, suffix="rvalid")
    stored = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?",
        (row["command_public_id"],),
    ).fetchone()
    assert stored is not None
    assert stored["fact_set_public_id"] == row["fact_set_public_id"]
    assert stored["version"] == 1
    assert stored["superseded_by_fact_set_public_id"] is None
    assert stored["schema_version"] == "v1"


@pytest.mark.parametrize(
    "bad_command_id",
    [
        "rpfc_wrong_prefix",
        "riaf_",
        "riafc_",
        "riaf_" + "x" * 196,
        "riafc_" + "x" * 195,
        "riaf_bad!char",
        "RIAF_uppercase_prefix",
    ],
    ids=[
        "wrong_prefix",
        "riaf_too_short",
        "riafc_too_short",
        "riaf_too_long",
        "riafc_too_long",
        "illegal_char",
        "uppercase_prefix",
    ],
)
def test_registry_malformed_command_public_id_rejected(
    migrated_temp_db_connection: sqlite3.Connection, bad_command_id: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rcmd")
    _expect_insert_rejected(conn, ctx, "CHECK constraint failed", command_public_id=bad_command_id)


@pytest.mark.parametrize(
    "bad_fact_set_id",
    ["fs_missing_prefix", "rfs_", "rfs_" + "x" * 197, "rfs_bad!char", "RFS_UPPER"],
    ids=["wrong_prefix", "too_short", "too_long", "illegal_char", "uppercase_prefix"],
)
def test_registry_malformed_fact_set_public_id_rejected(
    migrated_temp_db_connection: sqlite3.Connection, bad_fact_set_id: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rfsid")
    _expect_insert_rejected(
        conn, ctx, "CHECK constraint failed", fact_set_public_id=bad_fact_set_id
    )


@pytest.mark.parametrize(
    "column",
    [
        "expected_conversion_result_hash",
        "command_material_hash",
        "fact_set_input_hash",
        "fact_set_result_hash",
    ],
)
@pytest.mark.parametrize(
    "bad_value",
    ["short", "A" * 64, "z" * 64, "g" * 64],
    ids=["too_short", "uppercase", "nonhex_z", "nonhex_g"],
)
def test_registry_malformed_hashes_rejected(
    migrated_temp_db_connection: sqlite3.Connection, column: str, bad_value: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rhash")
    # A malformed expected_conversion_result_hash also fails the binding
    # trigger; either abort proves fail-closed behaviour.
    _expect_insert_rejected(
        conn,
        ctx,
        f"CHECK constraint failed|{_CONVERSION_BINDING}",
        **{column: bad_value},
    )


@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        ("actor_type", "ai"),
        ("actor_type", "system"),
        ("authenticated_actor_id", "   "),
        ("authenticated_actor_id", ""),
        ("channel", ""),
        ("channel", "  "),
        ("schema_version", "v2"),
        ("version", 0),
        ("version", -1),
        ("canonical_fact_set_payload", "not-json"),
        ("reason", ""),
        ("reason", "   "),
        ("reason", "x" * 501),
    ],
)
def test_registry_scalar_constraints_rejected(
    migrated_temp_db_connection: sqlite3.Connection, column: str, bad_value: Any
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rscalar")
    _expect_insert_rejected(conn, ctx, "CHECK constraint failed", **{column: bad_value})


def test_registry_reason_boundary_500_accepted_501_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rreason")
    row = seed_active_fact_set(conn, ctx, suffix="rreason", reason="x" * 500)
    stored = conn.execute(
        "SELECT reason FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?",
        (row["command_public_id"],),
    ).fetchone()
    assert stored["reason"] == "x" * 500
    ctx_bad = seed_conversion_context(conn, "rreason2")
    _expect_insert_rejected(conn, ctx_bad, "CHECK constraint failed", reason="x" * 501)


def test_registry_version_supersedes_coupling_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rcouple")
    # version 1 must not carry a predecessor (lineage trigger or CHECK).
    _expect_insert_rejected(
        conn,
        ctx,
        f"CHECK constraint failed|{_SUPERSEDES_LINEAGE}",
        supersedes_fact_set_public_id="rfs_ghost_predecessor",
    )
    # version > 1 must carry a predecessor.
    _expect_insert_rejected(conn, ctx, "CHECK constraint failed", version=2)


def test_registry_self_reference_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rself")
    row = make_fact_set_row(ctx, suffix="rself", version=2)
    row["supersedes_fact_set_public_id"] = row["fact_set_public_id"]
    with pytest.raises(
        sqlite3.IntegrityError,
        match=f"CHECK constraint failed|{_SUPERSEDES_LINEAGE}",
    ):
        insert_fact_set(conn, row)
    conn.rollback()


def test_registry_born_active_pointer_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rborn")
    _expect_insert_rejected(
        conn, ctx, _BORN_ACTIVE, superseded_by_fact_set_public_id="rfs_ghost_successor"
    )


def test_registry_missing_references_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "rrefs")
    # Unknown receipt: the conversion-binding trigger aborts (the seeded
    # conversion row belongs to a different receipt).
    _expect_insert_rejected(conn, ctx, _CONVERSION_BINDING, receipt_id=999_999)
    # Unknown conversion command.
    _expect_insert_rejected(
        conn, ctx, _CONVERSION_BINDING, conversion_command_public_id="rpfc_never_recorded"
    )
    # Result-hash mismatch against the live conversion row.
    _expect_insert_rejected(
        conn, ctx, _CONVERSION_BINDING, expected_conversion_result_hash="d" * 64
    )
    # Unknown predecessor on a correction version.
    _expect_insert_rejected(
        conn,
        ctx,
        _SUPERSEDES_LINEAGE,
        version=2,
        supersedes_fact_set_public_id="rfs_never_recorded",
    )


def test_registry_missing_audit_event_fails_at_commit(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The audit binding FK is deferred: the insert succeeds, COMMIT fails."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "raudit")
    row = make_fact_set_row(ctx, suffix="raudit", audit_event_public_id="fae_never_recorded")
    insert_fact_set(conn, row)  # No immediate error: deferred enforcement.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        conn.commit()
    conn.rollback()
    remaining = conn.execute("SELECT COUNT(*) FROM receipt_item_allocation_fact_sets").fetchone()[0]
    assert remaining == 0


def test_registry_identity_collisions_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx_a = seed_conversion_context(conn, "rcolla")
    ctx_b = seed_conversion_context(conn, "rcollb")
    seeded = seed_active_fact_set(conn, ctx_a, suffix="rcolla")

    def rejected(ctx: ConversionContext, **overrides: Any) -> None:
        row = make_fact_set_row(ctx, suffix="rcollb", **overrides)
        with pytest.raises(sqlite3.IntegrityError, match=_FACT_SET_COLLISION):
            insert_fact_set(conn, row)
        conn.rollback()

    rejected(ctx_b, command_public_id=seeded["command_public_id"])
    rejected(ctx_b, fact_set_public_id=seeded["fact_set_public_id"])
    rejected(ctx_b, audit_event_public_id=seeded["audit_event_public_id"])
    # receipt/version duplicate and the active-slot collision (a second
    # active row for the same receipt) both abort on ctx_a's receipt.
    rejected(ctx_a)
    row = make_fact_set_row(
        ctx_a,
        suffix="rcollb2",
        version=2,
        command_public_id="riafc_rcollb2",
        supersedes_fact_set_public_id=seeded["fact_set_public_id"],
    )
    with pytest.raises(sqlite3.IntegrityError, match=_FACT_SET_COLLISION):
        insert_fact_set(conn, row)
    conn.rollback()


# -----------------------------------------------------------------------
# Canonical row shapes: items
# -----------------------------------------------------------------------


def test_bound_item_valid_row_accepted_and_legacy_row_still_valid(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "ivalid")
    row = seed_active_fact_set(conn, ctx, suffix="ivalid")
    insert_bound_item(
        conn,
        ctx.receipt_id,
        row["fact_set_public_id"],
        suffix="ivalid",
        quantity_canonical="2",
        unit_price_canonical="5.00",
        line_amount_canonical="10.00",
    )
    # Legacy row with every additive column NULL remains valid.
    insert_bound_item(
        conn,
        ctx.receipt_id,
        None,
        suffix="ivalid_legacy",
        line_number=None,
        line_amount_canonical=None,
    )
    conn.commit()
    bound = conn.execute(
        "SELECT quantity_canonical_text, line_amount_canonical_text FROM receipt_items "
        "WHERE public_id = 'ritem_iaf_ivalid'"
    ).fetchone()
    assert bound["quantity_canonical_text"] == "2"
    assert bound["line_amount_canonical_text"] == "10.00"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"line_amount_canonical": None}, "CHECK constraint failed"),
        ({"line_number": None}, "CHECK constraint failed"),
        ({"line_number": 0}, "CHECK constraint failed"),
        ({"line_number": -1}, "CHECK constraint failed"),
        ({"quantity_canonical": "0"}, "CHECK constraint failed"),
        ({"quantity_canonical": "1.5"}, "CHECK constraint failed"),
        ({"quantity_canonical": "-2"}, "CHECK constraint failed"),
        ({"quantity_canonical": "02"}, "CHECK constraint failed"),
        ({"quantity_canonical": ""}, "CHECK constraint failed"),
        ({"unit_price_canonical": "9.5"}, "CHECK constraint failed"),
        ({"unit_price_canonical": "09.50"}, "CHECK constraint failed"),
        ({"unit_price_canonical": "0.00"}, "CHECK constraint failed"),
        ({"unit_price_canonical": "abc"}, "CHECK constraint failed"),
        ({"line_amount_canonical": "10"}, "CHECK constraint failed"),
        ({"line_amount_canonical": "10.0"}, "CHECK constraint failed"),
        ({"line_amount_canonical": "0.00"}, "CHECK constraint failed"),
        ({"line_amount_canonical": "10.00 "}, "CHECK constraint failed"),
    ],
    ids=[
        "missing_line_amount_text",
        "missing_line_number",
        "zero_line_number",
        "negative_line_number",
        "zero_quantity",
        "decimal_quantity",
        "negative_quantity",
        "leading_zero_quantity",
        "empty_quantity",
        "one_decimal_unit_price",
        "leading_zero_unit_price",
        "zero_unit_price",
        "alpha_unit_price",
        "integer_line_amount_sgd",
        "one_decimal_line_amount",
        "zero_line_amount",
        "trailing_space_line_amount",
    ],
)
def test_bound_item_malformed_shapes_rejected(
    migrated_temp_db_connection: sqlite3.Connection, kwargs: dict[str, Any], match: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "ibad")
    row = seed_active_fact_set(conn, ctx, suffix="ibad")
    with pytest.raises(sqlite3.IntegrityError, match=match):
        insert_bound_item(conn, ctx.receipt_id, row["fact_set_public_id"], suffix="ibad", **kwargs)
    conn.rollback()


def test_bound_item_jpy_integer_shape(migrated_temp_db_connection: sqlite3.Connection) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "ijpy", currency="JPY", net_paid="3000")
    row = seed_active_fact_set(conn, ctx, suffix="ijpy")
    insert_bound_item(
        conn,
        ctx.receipt_id,
        row["fact_set_public_id"],
        suffix="ijpy",
        line_amount_canonical="3000",
        currency="JPY",
    )
    conn.commit()
    for bad in ("3000.00", "0", "03000"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            insert_bound_item(
                conn,
                ctx.receipt_id,
                row["fact_set_public_id"],
                suffix=f"ijpy_{bad}",
                line_number=2,
                line_amount_canonical=bad,
                currency="JPY",
            )
        conn.rollback()


def test_bound_item_cross_receipt_and_unknown_fact_set_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx_a = seed_conversion_context(conn, "ixra")
    ctx_b = seed_conversion_context(conn, "ixrb")
    row_a = seed_active_fact_set(conn, ctx_a, suffix="ixra")
    # Bound to receipt B while the fact set belongs to receipt A.
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_RECEIPT_MATCH):
        insert_bound_item(conn, ctx_b.receipt_id, row_a["fact_set_public_id"], suffix="ixr1")
    conn.rollback()
    # Unknown fact set (trigger fires even before FK enforcement).
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_RECEIPT_MATCH):
        insert_bound_item(conn, ctx_a.receipt_id, "rfs_never_recorded", suffix="ixr2")
    conn.rollback()


def test_bound_item_line_numbers_unique_within_fact_set(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "iuni")
    row = seed_active_fact_set(conn, ctx, suffix="iuni")
    insert_bound_item(conn, ctx.receipt_id, row["fact_set_public_id"], suffix="iuni1")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_COLLISION):
        insert_bound_item(conn, ctx.receipt_id, row["fact_set_public_id"], suffix="iuni2")
    conn.rollback()
    # Legacy rows may reuse line numbers freely.
    insert_bound_item(conn, ctx.receipt_id, None, suffix="iuni3", line_amount_canonical=None)
    insert_bound_item(conn, ctx.receipt_id, None, suffix="iuni4", line_amount_canonical=None)
    conn.commit()


# -----------------------------------------------------------------------
# Canonical row shapes: adjustments
# -----------------------------------------------------------------------


def test_bound_adjustment_valid_row_and_legacy_vocabulary_preserved(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "avalid")
    row = seed_active_fact_set(conn, ctx, suffix="avalid")
    insert_bound_adjustment(
        conn,
        ctx.receipt_id,
        row["fact_set_public_id"],
        suffix="avalid",
        description="Service charge 10%",
    )
    # Unbound legacy rows retain the full migration 002 vocabulary,
    # including the directions/methods bound rows may not use.
    insert_bound_adjustment(
        conn,
        ctx.receipt_id,
        None,
        suffix="avalid_legacy",
        adjustment_index=None,
        amount_canonical=None,
        direction="informational",
        allocation_method="excluded",
    )
    conn.commit()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"adjustment_index": None}, "CHECK constraint failed"),
        ({"adjustment_index": 0}, "CHECK constraint failed"),
        ({"adjustment_index": -1}, "CHECK constraint failed"),
        ({"amount_canonical": None}, "CHECK constraint failed"),
        ({"amount_canonical": "0.00"}, "CHECK constraint failed"),
        ({"amount_canonical": "3.5"}, "CHECK constraint failed"),
        ({"direction": "informational"}, "CHECK constraint failed"),
        ({"allocation_method": "excluded"}, "CHECK constraint failed"),
        ({"allocation_method": "proportional_by_net_amount"}, "CHECK constraint failed"),
        ({"description": ""}, "CHECK constraint failed"),
        ({"description": "   "}, "CHECK constraint failed"),
        ({"description": "x" * 501}, "CHECK constraint failed"),
    ],
    ids=[
        "missing_index",
        "zero_index",
        "negative_index",
        "missing_amount_text",
        "zero_amount",
        "one_decimal_amount",
        "informational_direction",
        "excluded_method",
        "proportional_by_net_amount_method",
        "empty_description",
        "blank_description",
        "description_501",
    ],
)
def test_bound_adjustment_malformed_shapes_rejected(
    migrated_temp_db_connection: sqlite3.Connection, kwargs: dict[str, Any], match: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "abad")
    row = seed_active_fact_set(conn, ctx, suffix="abad")
    with pytest.raises(sqlite3.IntegrityError, match=match):
        insert_bound_adjustment(
            conn, ctx.receipt_id, row["fact_set_public_id"], suffix="abad", **kwargs
        )
    conn.rollback()


def test_bound_adjustment_description_500_accepted_501_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "adesc")
    row = seed_active_fact_set(conn, ctx, suffix="adesc")
    insert_bound_adjustment(
        conn,
        ctx.receipt_id,
        row["fact_set_public_id"],
        suffix="adesc500",
        description="d" * 500,
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        insert_bound_adjustment(
            conn,
            ctx.receipt_id,
            row["fact_set_public_id"],
            suffix="adesc501",
            adjustment_index=2,
            description="d" * 501,
        )
    conn.rollback()


def test_bound_adjustment_cross_receipt_and_index_uniqueness(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx_a = seed_conversion_context(conn, "axra")
    ctx_b = seed_conversion_context(conn, "axrb")
    row_a = seed_active_fact_set(conn, ctx_a, suffix="axra")
    with pytest.raises(sqlite3.IntegrityError, match=_ADJ_RECEIPT_MATCH):
        insert_bound_adjustment(conn, ctx_b.receipt_id, row_a["fact_set_public_id"], suffix="axr1")
    conn.rollback()
    insert_bound_adjustment(conn, ctx_a.receipt_id, row_a["fact_set_public_id"], suffix="axr2")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match=_ADJUSTMENT_COLLISION):
        insert_bound_adjustment(conn, ctx_a.receipt_id, row_a["fact_set_public_id"], suffix="axr3")
    conn.rollback()


# -----------------------------------------------------------------------
# Canonical row shapes: allocation facts
# -----------------------------------------------------------------------


def _seed_bound_item_context(
    conn: sqlite3.Connection, suffix: str
) -> tuple[ConversionContext, dict[str, Any], int]:
    ctx = seed_conversion_context(conn, suffix)
    row = seed_active_fact_set(conn, ctx, suffix=suffix)
    item_id = insert_bound_item(conn, ctx.receipt_id, row["fact_set_public_id"], suffix=suffix)
    conn.commit()
    return ctx, row, item_id


def test_allocation_fact_manual_and_equal_amount_accepted(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _ctx, row, item_id = _seed_bound_item_context(conn, "lvalid")
    insert_allocation_fact(
        conn,
        row["fact_set_public_id"],
        item_id,
        participant_id(conn, "person_alice"),
        suffix="lvalid_manual",
    )
    # equal_amount persists no per-participant amount: both share fields NULL.
    insert_allocation_fact(
        conn,
        row["fact_set_public_id"],
        item_id,
        participant_id(conn, "person_bob"),
        suffix="lvalid_equal",
        allocation_method="equal_amount",
        share_canonical=None,
        share_mirror=None,
    )
    conn.commit()
    stored = conn.execute(
        "SELECT allocation_method, share_amount_canonical_text, share_amount "
        "FROM receipt_item_allocation_facts WHERE allocation_public_id = ?",
        ("rfsa_iaf_lvalid_equal",),
    ).fetchone()
    assert stored["allocation_method"] == "equal_amount"
    assert stored["share_amount_canonical_text"] is None
    assert stored["share_amount"] is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {"allocation_method": "manual", "share_canonical": None, "share_mirror": None},
            "CHECK constraint failed",
        ),
        (
            {
                "allocation_method": "equal_amount",
                "share_canonical": "5.00",
                "share_mirror": "5.00",
            },
            "CHECK constraint failed",
        ),
        ({"share_canonical": "5.00", "share_mirror": None}, "CHECK constraint failed"),
        (
            {"allocation_method": "equal_amount", "share_canonical": None, "share_mirror": "5.00"},
            "CHECK constraint failed",
        ),
        ({"allocation_method": "percentage"}, "CHECK constraint failed"),
        ({"allocation_method": "payer_only"}, "CHECK constraint failed"),
        ({"allocation_method": "equal_quantity"}, "CHECK constraint failed"),
        ({"share_canonical": "0.00", "share_mirror": "0.00"}, "CHECK constraint failed"),
        ({"share_canonical": "1.2.3", "share_mirror": "1.23"}, "CHECK constraint failed"),
        ({"share_canonical": "01.00", "share_mirror": "1.00"}, "CHECK constraint failed"),
        ({"share_canonical": ".50", "share_mirror": "0.50"}, "CHECK constraint failed"),
        ({"share_canonical": "5.", "share_mirror": "5"}, "CHECK constraint failed"),
        ({"share_canonical": "-5.00", "share_mirror": "-5.00"}, "CHECK constraint failed"),
    ],
    ids=[
        "manual_without_share",
        "equal_with_share",
        "text_without_mirror",
        "mirror_without_text",
        "percentage_method",
        "payer_only_method",
        "equal_quantity_method",
        "zero_share",
        "double_dot_share",
        "leading_zero_share",
        "bare_dot_prefix_share",
        "trailing_dot_share",
        "negative_share",
    ],
)
def test_allocation_fact_malformed_rows_rejected(
    migrated_temp_db_connection: sqlite3.Connection, kwargs: dict[str, Any], match: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _ctx, row, item_id = _seed_bound_item_context(conn, "lbad")
    with pytest.raises(sqlite3.IntegrityError, match=match):
        insert_allocation_fact(
            conn,
            row["fact_set_public_id"],
            item_id,
            participant_id(conn, "person_alice"),
            suffix="lbad",
            **kwargs,
        )
    conn.rollback()


def test_allocation_fact_malformed_public_id_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _ctx, row, item_id = _seed_bound_item_context(conn, "lpid")
    for bad in ("alloc_wrong_prefix", "rfsa_", "rfsa_bad!char"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                """
                INSERT INTO receipt_item_allocation_facts (
                    allocation_public_id, fact_set_id, receipt_item_id,
                    participant_id, allocation_method,
                    share_amount_canonical_text, share_amount
                ) VALUES (?, ?, ?, ?, 'manual', '5.00', '5.00')
                """,
                (bad, row["fact_set_public_id"], item_id, participant_id(conn, "person_alice")),
            )
        conn.rollback()


def test_allocation_fact_binding_consistency_enforced(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _ctx_a, row_a, item_a = _seed_bound_item_context(conn, "lbind_a")
    ctx_b, row_b, _item_b = _seed_bound_item_context(conn, "lbind_b")
    alice = participant_id(conn, "person_alice")
    # Item bound to fact set A cannot carry an allocation fact of set B.
    with pytest.raises(sqlite3.IntegrityError, match=_ALLOCATION_BOUND_ITEM):
        insert_allocation_fact(conn, row_b["fact_set_public_id"], item_a, alice, suffix="lb1")
    conn.rollback()
    # Legacy (unbound) items can never carry allocation facts.
    legacy_item = insert_bound_item(
        conn, ctx_b.receipt_id, None, suffix="lbind_legacy", line_amount_canonical=None
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match=_ALLOCATION_BOUND_ITEM):
        insert_allocation_fact(conn, row_b["fact_set_public_id"], legacy_item, alice, suffix="lb2")
    conn.rollback()
    # Nonexistent item fails the same trigger before FK enforcement.
    with pytest.raises(sqlite3.IntegrityError, match=_ALLOCATION_BOUND_ITEM):
        insert_allocation_fact(conn, row_a["fact_set_public_id"], 999_999, alice, suffix="lb3")
    conn.rollback()
    # Duplicate item/participant pair aborts via the collision trigger.
    insert_allocation_fact(conn, row_a["fact_set_public_id"], item_a, alice, suffix="lb4")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match=_ALLOCATION_COLLISION):
        insert_allocation_fact(conn, row_a["fact_set_public_id"], item_a, alice, suffix="lb5")
    conn.rollback()


# -----------------------------------------------------------------------
# Append-only / collision enforcement
# -----------------------------------------------------------------------


def test_registry_update_and_delete_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "pfr")
    row = seed_active_fact_set(conn, ctx, suffix="pfr")
    key = row["command_public_id"]
    for statement, args in [
        (
            "UPDATE receipt_item_allocation_fact_sets SET channel = 'cli' "
            "WHERE command_public_id = ?",
            (key,),
        ),
        (
            "UPDATE receipt_item_allocation_fact_sets SET fact_set_result_hash = ? "
            "WHERE command_public_id = ?",
            (_hex64("tampered"), key),
        ),
        (
            "UPDATE receipt_item_allocation_fact_sets SET rowid = rowid + 1000 "
            "WHERE command_public_id = ?",
            (key,),
        ),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match=_SINGLE_TRANSITION):
            conn.execute(statement, args)
        conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?", (key,)
        )
    conn.rollback()


def _seed_superseded_pair(
    conn: sqlite3.Connection, suffix: str
) -> tuple[ConversionContext, dict[str, Any], dict[str, Any]]:
    """Seed predecessor v1 + successor v2 through the frozen 11.1 order."""
    ctx = seed_conversion_context(conn, suffix)
    predecessor = seed_active_fact_set(conn, ctx, suffix=f"{suffix}_v1")
    successor = make_fact_set_row(
        ctx,
        suffix=f"{suffix}_v2",
        version=2,
        command_public_id=f"riafc_{suffix}_v2",
        supersedes_fact_set_public_id=predecessor["fact_set_public_id"],
    )
    cursor = conn.execute(
        "UPDATE receipt_item_allocation_fact_sets "
        "SET superseded_by_fact_set_public_id = ? "
        "WHERE fact_set_public_id = ? AND superseded_by_fact_set_public_id IS NULL",
        (successor["fact_set_public_id"], predecessor["fact_set_public_id"]),
    )
    assert cursor.rowcount == 1
    insert_fact_set(conn, successor)
    seed_audit_event(conn, successor["audit_event_public_id"], ctx.receipt_public_id, sequence=2)
    conn.commit()
    return ctx, predecessor, successor


def test_registry_single_transition_lifecycle(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx, predecessor, successor = _seed_superseded_pair(conn, "ptr")
    active = conn.execute(
        "SELECT fact_set_public_id FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? AND superseded_by_fact_set_public_id IS NULL",
        (ctx.receipt_id,),
    ).fetchall()
    assert [r["fact_set_public_id"] for r in active] == [successor["fact_set_public_id"]]

    # Second transition of the already-superseded predecessor rejected.
    with pytest.raises(sqlite3.IntegrityError, match=_SINGLE_TRANSITION):
        conn.execute(
            "UPDATE receipt_item_allocation_fact_sets "
            "SET superseded_by_fact_set_public_id = 'rfs_other' "
            "WHERE fact_set_public_id = ?",
            (predecessor["fact_set_public_id"],),
        )
    conn.rollback()
    # Clearing the pointer rejected.
    with pytest.raises(sqlite3.IntegrityError, match=_SINGLE_TRANSITION):
        conn.execute(
            "UPDATE receipt_item_allocation_fact_sets "
            "SET superseded_by_fact_set_public_id = NULL "
            "WHERE fact_set_public_id = ?",
            (predecessor["fact_set_public_id"],),
        )
    conn.rollback()
    # Self-transition rejected.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed|append-only"):
        conn.execute(
            "UPDATE receipt_item_allocation_fact_sets "
            "SET superseded_by_fact_set_public_id = fact_set_public_id "
            "WHERE fact_set_public_id = ?",
            (successor["fact_set_public_id"],),
        )
    conn.rollback()
    # Transition combined with any other column change rejected.
    with pytest.raises(sqlite3.IntegrityError, match=_SINGLE_TRANSITION):
        conn.execute(
            "UPDATE receipt_item_allocation_fact_sets "
            "SET superseded_by_fact_set_public_id = 'rfs_next', channel = 'cli' "
            "WHERE fact_set_public_id = ?",
            (successor["fact_set_public_id"],),
        )
    conn.rollback()
    # Transition to a non-rfs_ value rejected.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed|append-only"):
        conn.execute(
            "UPDATE receipt_item_allocation_fact_sets "
            "SET superseded_by_fact_set_public_id = 'bogus_shape' "
            "WHERE fact_set_public_id = ?",
            (successor["fact_set_public_id"],),
        )
    conn.rollback()


def test_registry_insert_or_replace_preserves_original_bytes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "prep")
    row = seed_active_fact_set(conn, ctx, suffix="prep")
    original = dict(
        conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?",
            (row["command_public_id"],),
        ).fetchone()
    )
    tampered = make_fact_set_row(
        ctx, suffix="prep", canonical_fact_set_payload='{"items": ["tampered"]}'
    )
    with pytest.raises(sqlite3.IntegrityError, match=_FACT_SET_COLLISION):
        insert_fact_set(conn, tampered, or_replace=True)
    conn.rollback()
    after = dict(
        conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?",
            (row["command_public_id"],),
        ).fetchone()
    )
    assert after == original


def test_collision_triggers_fire_with_pragmas_disabled(temp_db_path: Path) -> None:
    """recursive_triggers=OFF and foreign_keys=OFF must not weaken any
    collision or binding backstop: the triggers are pragma-independent."""
    conn = connect_temp_db(temp_db_path)
    apply_migrations(conn)
    conn.commit()
    seed_people(conn)
    ctx = seed_conversion_context(conn, "prag")
    row = seed_active_fact_set(conn, ctx, suffix="prag")
    item_id = insert_bound_item(conn, ctx.receipt_id, row["fact_set_public_id"], suffix="prag")
    insert_allocation_fact(
        conn,
        row["fact_set_public_id"],
        item_id,
        participant_id(conn, "person_owner"),
        suffix="prag",
    )
    conn.commit()
    conn.close()

    raw = sqlite3.connect(str(temp_db_path))
    raw.row_factory = sqlite3.Row
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match=_FACT_SET_COLLISION):
            raw.execute(
                "INSERT OR REPLACE INTO receipt_item_allocation_fact_sets ("
                + ", ".join(_FACT_SET_COLUMNS)
                + ") SELECT "
                + ", ".join(_FACT_SET_COLUMNS)
                + " FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?",
                (row["command_public_id"],),
            )
        raw.rollback()
        # Binding triggers still enforce existence with foreign_keys OFF.
        with pytest.raises(sqlite3.IntegrityError, match=_CONVERSION_BINDING):
            raw.execute(
                "INSERT INTO receipt_item_allocation_fact_sets ("
                + ", ".join(_FACT_SET_COLUMNS)
                + ") VALUES ("
                + ", ".join("?" for _ in _FACT_SET_COLUMNS)
                + ")",
                tuple(
                    make_fact_set_row(
                        ConversionContext(999_999, "rcpt_ghost", "rpfc_ghost", "e" * 64),
                        suffix="prag_ghost",
                    )[column]
                    for column in _FACT_SET_COLUMNS
                ),
            )
        raw.rollback()
        with pytest.raises(sqlite3.IntegrityError, match=_ITEM_RECEIPT_MATCH):
            raw.execute(
                "INSERT INTO receipt_items (public_id, receipt_id, line_number, item_name, "
                "line_amount, currency, line_amount_canonical_text, fact_set_id) "
                "VALUES ('ritem_prag_ghost', ?, 3, 'Ghost', '1.00', 'SGD', '1.00', "
                "'rfs_never_recorded')",
                (ctx.receipt_id,),
            )
        raw.rollback()
        with pytest.raises(sqlite3.IntegrityError, match=_ITEM_COLLISION):
            raw.execute(
                "INSERT OR REPLACE INTO receipt_items (public_id, receipt_id, line_number, "
                "item_name, line_amount, currency, line_amount_canonical_text, fact_set_id) "
                "VALUES ('ritem_iaf_prag', ?, 9, 'Overwrite', '1.00', 'SGD', '1.00', NULL)",
                (ctx.receipt_id,),
            )
        raw.rollback()
        with pytest.raises(sqlite3.IntegrityError, match=_ALLOCATION_COLLISION):
            raw.execute(
                "INSERT OR REPLACE INTO receipt_item_allocation_facts ("
                "allocation_public_id, fact_set_id, receipt_item_id, participant_id, "
                "allocation_method, share_amount_canonical_text, share_amount) "
                "VALUES ('rfsa_iaf_prag', ?, ?, "
                "(SELECT participant_id FROM receipt_item_allocation_facts "
                "WHERE allocation_public_id = 'rfsa_iaf_prag'), 'manual', '9.00', '9.00')",
                (row["fact_set_public_id"], item_id),
            )
        raw.rollback()
    finally:
        raw.close()


def test_bound_items_and_adjustments_frozen_and_adoption_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "pfrz")
    row = seed_active_fact_set(conn, ctx, suffix="pfrz")
    item_id = insert_bound_item(conn, ctx.receipt_id, row["fact_set_public_id"], suffix="pfrz")
    legacy_item = insert_bound_item(
        conn, ctx.receipt_id, None, suffix="pfrz_legacy", line_amount_canonical=None
    )
    adj_id = insert_bound_adjustment(conn, ctx.receipt_id, row["fact_set_public_id"], suffix="pfrz")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_FREEZE):
        conn.execute("UPDATE receipt_items SET item_name = 'Edited' WHERE id = ?", (item_id,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_FREEZE):
        conn.execute("UPDATE receipt_items SET rowid = rowid + 500 WHERE id = ?", (item_id,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM receipt_items WHERE id = ?", (item_id,))
    conn.rollback()
    # Legacy adoption by UPDATE rejected (NULL -> fact set).
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_FREEZE):
        conn.execute(
            "UPDATE receipt_items SET fact_set_id = ?, line_amount_canonical_text = '10.00', "
            "line_number = 7 WHERE id = ?",
            (row["fact_set_public_id"], legacy_item),
        )
    conn.rollback()
    # UPDATE OR REPLACE from a legacy row into a bound row's identity rejected.
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_COLLISION):
        conn.execute(
            "UPDATE OR REPLACE receipt_items SET public_id = 'ritem_iaf_pfrz' WHERE id = ?",
            (legacy_item,),
        )
    conn.rollback()
    # Bound insert whose identity collides with a legacy row rejected
    # (REPLACE may not delete the conflicting legacy row either).
    with pytest.raises(sqlite3.IntegrityError, match=_ITEM_COLLISION):
        conn.execute(
            "INSERT OR REPLACE INTO receipt_items (public_id, receipt_id, line_number, "
            "item_name, line_amount, currency, line_amount_canonical_text, fact_set_id) "
            "VALUES ('ritem_iaf_pfrz_legacy', ?, 2, 'Takeover', '1.00', 'SGD', '1.00', ?)",
            (ctx.receipt_id, row["fact_set_public_id"]),
        )
    conn.rollback()
    # Legacy rows keep their pre-existing update/delete behaviour.
    conn.execute("UPDATE receipt_items SET item_name = 'Legacy Edit' WHERE id = ?", (legacy_item,))
    conn.execute("DELETE FROM receipt_items WHERE id = ?", (legacy_item,))
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match=_ADJ_FREEZE):
        conn.execute(
            "UPDATE receipt_adjustments SET description = 'Edited' WHERE id = ?", (adj_id,)
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM receipt_adjustments WHERE id = ?", (adj_id,))
    conn.rollback()
    legacy_adj = insert_bound_adjustment(
        conn,
        ctx.receipt_id,
        None,
        suffix="pfrz_legacy_adj",
        adjustment_index=None,
        amount_canonical=None,
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match=_ADJ_FREEZE):
        conn.execute(
            "UPDATE receipt_adjustments SET fact_set_id = ?, adjustment_index = 9, "
            "amount_canonical_text = '3.00' WHERE id = ?",
            (row["fact_set_public_id"], legacy_adj),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match=_ADJUSTMENT_COLLISION):
        conn.execute(
            "UPDATE OR REPLACE receipt_adjustments SET public_id = 'radj_iaf_pfrz' WHERE id = ?",
            (legacy_adj,),
        )
    conn.rollback()


def test_allocation_facts_append_only(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _ctx, row, item_id = _seed_bound_item_context(conn, "pall")
    alice = participant_id(conn, "person_alice")
    insert_allocation_fact(conn, row["fact_set_public_id"], item_id, alice, suffix="pall")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE receipt_item_allocation_facts SET share_amount_canonical_text = '9.99' "
            "WHERE allocation_public_id = 'rfsa_iaf_pall'"
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM receipt_item_allocation_facts WHERE allocation_public_id = 'rfsa_iaf_pall'"
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match=_ALLOCATION_COLLISION):
        insert_allocation_fact(
            conn, row["fact_set_public_id"], item_id, alice, suffix="pall", or_replace=True
        )
    conn.rollback()


# -----------------------------------------------------------------------
# Frozen Section 11.1 ordering proofs (P1-P7)
# -----------------------------------------------------------------------


def test_transition_first_then_successor_insert_commits(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """P1: the frozen order (transition first, successor second) commits,
    leaving exactly one active version."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx, predecessor, successor = _seed_superseded_pair(conn, "o1")
    rows = conn.execute(
        "SELECT fact_set_public_id, version, superseded_by_fact_set_public_id "
        "FROM receipt_item_allocation_fact_sets WHERE receipt_id = ? ORDER BY version",
        (ctx.receipt_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["superseded_by_fact_set_public_id"] == successor["fact_set_public_id"]
    assert rows[1]["superseded_by_fact_set_public_id"] is None
    assert predecessor["fact_set_public_id"] == rows[0]["fact_set_public_id"]


def test_successor_insert_first_fails_on_one_active_index(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """P2: inserting the successor while the predecessor is still active
    violates the one-active partial unique index (via the collision
    trigger, which fires first and blocks REPLACE semantics too)."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "o2")
    predecessor = seed_active_fact_set(conn, ctx, suffix="o2_v1")
    successor = make_fact_set_row(
        ctx,
        suffix="o2_v2",
        version=2,
        command_public_id="riafc_o2_v2",
        supersedes_fact_set_public_id=predecessor["fact_set_public_id"],
    )
    with pytest.raises(sqlite3.IntegrityError, match=_FACT_SET_COLLISION):
        insert_fact_set(conn, successor)
    conn.rollback()


def test_missing_successor_fails_at_commit_and_rollback_restores(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """P4 + P5: a dangling transition is permitted mid-transaction by the
    deferred FK but COMMIT fails closed; rollback restores the predecessor
    as the single active row with a NULL pointer."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_conversion_context(conn, "o4")
    predecessor = seed_active_fact_set(conn, ctx, suffix="o4_v1")
    cursor = conn.execute(
        "UPDATE receipt_item_allocation_fact_sets "
        "SET superseded_by_fact_set_public_id = 'rfs_o4_v2_never_inserted' "
        "WHERE fact_set_public_id = ? AND superseded_by_fact_set_public_id IS NULL",
        (predecessor["fact_set_public_id"],),
    )
    assert cursor.rowcount == 1  # P1: dangling pointer accepted pre-commit.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        conn.commit()
    conn.rollback()
    restored = conn.execute(
        "SELECT superseded_by_fact_set_public_id FROM receipt_item_allocation_fact_sets "
        "WHERE fact_set_public_id = ?",
        (predecessor["fact_set_public_id"],),
    ).fetchone()
    assert restored["superseded_by_fact_set_public_id"] is None


def test_conditional_transition_on_superseded_predecessor_affects_zero_rows(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """P6: the conditional transition re-verifies NULL at execution time,
    so a lost race affects zero rows instead of overwriting the pointer."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    _ctx, predecessor, _successor = _seed_superseded_pair(conn, "o6")
    cursor = conn.execute(
        "UPDATE receipt_item_allocation_fact_sets "
        "SET superseded_by_fact_set_public_id = 'rfs_o6_racer' "
        "WHERE fact_set_public_id = ? AND superseded_by_fact_set_public_id IS NULL",
        (predecessor["fact_set_public_id"],),
    )
    assert cursor.rowcount == 0
    conn.rollback()


def test_concurrent_writers_serialize_via_begin_immediate(temp_db_path: Path) -> None:
    """P7: two raw-SQL writer connections serialize on BEGIN IMMEDIATE, so
    no interleaving can produce a second active version."""
    conn = connect_temp_db(temp_db_path)
    apply_migrations(conn)
    conn.commit()
    seed_people(conn)
    ctx = seed_conversion_context(conn, "o7")
    seed_active_fact_set(conn, ctx, suffix="o7_v1")
    conn.close()

    writer_a = sqlite3.connect(str(temp_db_path), timeout=0.05)
    writer_b = sqlite3.connect(str(temp_db_path), timeout=0.05)
    try:
        writer_a.execute("PRAGMA foreign_keys = ON")
        writer_b.execute("PRAGMA foreign_keys = ON")
        writer_a.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            writer_b.execute("BEGIN IMMEDIATE")
        writer_a.rollback()
    finally:
        writer_a.close()
        writer_b.close()


# -----------------------------------------------------------------------
# Non-effects
# -----------------------------------------------------------------------


def test_migration_036_touches_no_existing_history_tables(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Applying the full manifest on an empty database leaves every
    pre-existing authoritative table empty: 036 writes no rows anywhere."""
    conn = migrated_temp_db_connection
    for table in (
        "receipts",
        "receipt_items",
        "receipt_item_allocations",
        "receipt_adjustments",
        "receipt_proposal_conversions",
        "financial_audit_events",
        "transactions",
        "calculation_runs",
        "receipt_item_allocation_fact_sets",
        "receipt_item_allocation_facts",
    ):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, table
