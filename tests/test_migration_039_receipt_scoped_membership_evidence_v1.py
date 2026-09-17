"""Migration 039 (receipt-scoped membership evidence) temporary-database tests.

Covers the PR #249 acceptance-closure migration contract:

- fresh 001-039 application on an empty temporary database;
- upgrade from a valid 038-level database;
- deterministic replay and checksum/manifest ordering;
- required FK / index / trigger / CHECK / uniqueness behavior;
- invalid pre-existing (squatter) state failing closed;
- rollback with no partial schema state on failure.

Only temporary/staging databases are used; ``database/finance.db`` and seed
data are untouched.
"""

from __future__ import annotations

import sqlite3

import pytest

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    MigrationExecutionError,
    apply_migration_paths,
    build_migration_manifest,
    migration_ledger_rows,
    verify_migration_history,
)

FROZEN_TIME = "2026-07-30T00:00:00+00:00"
TABLE = "receipt_finalization_membership_evidence"
MIGRATION_039_FILENAME = "039_receipt_scoped_membership_evidence.sql"

# Test-only migration manifest scoped to 001-039. This isolates the 039
# historical behavior tests from later migrations (e.g. 040) that are
# appended to the production manifest over time.
_PATHS_THROUGH_039 = TEMP_DB_MIGRATION_PATHS[:39]


def _clock() -> str:
    return FROZEN_TIME


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_evidence_parents(conn: sqlite3.Connection) -> None:
    """Seed the minimum FK parents a membership evidence row references."""
    conn.execute("INSERT INTO participants (public_id, display_name) VALUES ('person_m39', 'M39')")
    conn.execute(
        "INSERT INTO receipt_groups (public_id, currency, status)"
        " VALUES ('rgrp_m39', 'SGD', 'settled')"
    )
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, receipt_datetime, gross_amount,"
        " subtotal_amount, net_paid_amount, currency, payer_participant_id,"
        " source_channel, raw_input, status)"
        " VALUES ('r_m39', 'M39 Mart', '2026-01-01 12:00:00', 1.00, 1.00, 1.00, 'SGD',"
        " (SELECT id FROM participants WHERE public_id = 'person_m39'),"
        " 'manual_test_case', 'test', 'confirmed')"
    )
    conn.execute(
        "INSERT INTO receipt_finalization_confirmations ("
        " confirmation_id, receipt_group_public_id, calculation_run_public_id,"
        " calculation_snapshot_id, content_hash, currency, final_total,"
        " payer_participant_public_id, participant_public_ids_json,"
        " settlement_obligations_json, actor_type, confirmation_state, created_at)"
        " VALUES ('conf_m39', 'rgrp_m39', 'calc_m39', 'snap_m39', ?, 'SGD', '1.00',"
        " 'person_m39', '[]', '[]', 'cli', 'confirmed', ?)",
        ("0" * 64, FROZEN_TIME),
    )
    conn.execute(
        "INSERT INTO receipt_finalization_authorizations ("
        " authorization_id, receipt_group_public_id, calculation_run_public_id,"
        " calculation_snapshot_id, confirmation_id, content_hash, currency, final_total,"
        " payer_participant_public_id, participant_public_ids_json,"
        " settlement_obligations_json, source_evidence_refs_json, actor_type,"
        " authorization_state, authorization_version, created_at)"
        " VALUES ('auth_m39', 'rgrp_m39', 'calc_m39', 'snap_m39', 'conf_m39', ?, 'SGD',"
        " '1.00', 'person_m39', '[]', '[]', '[]', 'cli', 'consumed', 'v1', ?)",
        ("0" * 64, FROZEN_TIME),
    )
    conn.execute(
        "INSERT INTO receipt_finalization_audit ("
        " finalization_id, idempotency_key, content_fingerprint, authorization_id,"
        " receipt_group_public_id, calculation_run_public_id, currency, total_paid,"
        " total_to_collect, payer_participant_public_id, participant_public_ids_json,"
        " settlement_public_ids_json, actor_type, status, created_at)"
        " VALUES ('fin_m39', 'idem_m39', ?, 'auth_m39', 'rgrp_m39', 'calc_m39', 'SGD',"
        " '1.00', '0.00', 'person_m39', '[]', '[]', 'cli', 'finalized', ?)",
        ("0" * 64, FROZEN_TIME),
    )


def _insert_evidence_row(
    conn: sqlite3.Connection,
    *,
    public_id: str = "rfme_m39_person",
    scope: str = "receipt",
    receipt_public_id: str | None = "r_m39",
    participant: str = "person_m39",
    role: str = "payer",
    is_included: int = 1,
) -> None:
    conn.execute(
        f"INSERT INTO {TABLE} (membership_evidence_public_id, finalization_id,"
        f" receipt_group_public_id, membership_scope, receipt_public_id,"
        f" participant_public_id, role, is_included, created_at)"
        f" VALUES (?, 'fin_m39', 'rgrp_m39', ?, ?, ?, ?, ?, ?)",
        (public_id, scope, receipt_public_id, participant, role, is_included, FROZEN_TIME),
    )


def test_fresh_database_applies_001_through_039() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, _PATHS_THROUGH_039, clock=_clock)
        rows = migration_ledger_rows(conn)
        assert rows[-1]["migration_id"] == "039"
        assert rows[-1]["migration_filename"] == MIGRATION_039_FILENAME
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        trigger_names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (TABLE,),
            ).fetchall()
        }
        assert {
            "trg_rfme_no_update",
            "trg_rfme_no_delete",
            "trg_rfme_no_insert_collision",
        } <= trigger_names
        index_names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (TABLE,),
            ).fetchall()
        }
        assert "idx_rfme_finalization_id" in index_names
        assert "idx_rfme_receipt_public_id" in index_names
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_upgrade_from_valid_038_database_applies_only_039() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, _PATHS_THROUGH_039[:38], clock=_clock)
        assert migration_ledger_rows(conn)[-1]["migration_id"] == "038"
        apply_migration_paths(conn, _PATHS_THROUGH_039, clock=_clock)
        rows = migration_ledger_rows(conn)
        assert [row["migration_id"] for row in rows][-2:] == ["038", "039"]
        assert all(row["adoption_mode"] == "applied" for row in rows)
        verify_migration_history(conn, _PATHS_THROUGH_039)
    finally:
        conn.close()


def test_replay_after_039_is_deterministic_and_checksum_stable() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, _PATHS_THROUGH_039, clock=_clock)
        first = [tuple(row) for row in migration_ledger_rows(conn)]
        apply_migration_paths(conn, _PATHS_THROUGH_039, clock=_clock)
        second = [tuple(row) for row in migration_ledger_rows(conn)]
        assert first == second
        specs = build_migration_manifest(_PATHS_THROUGH_039)
        spec_039 = next(spec for spec in specs if spec.migration_id == "039")
        assert spec_039.sequence == 39
        assert spec_039.filename == MIGRATION_039_FILENAME
        assert spec_039.checksum_sha256 == migration_ledger_rows(conn)[-1]["checksum_sha256"]
    finally:
        conn.close()


def test_evidence_rows_enforce_fk_check_unique_and_append_only() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        _seed_evidence_parents(conn)
        _insert_evidence_row(conn)

        # UPDATE and DELETE are refused by the append-only triggers.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {TABLE} SET is_included = 0")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {TABLE}")

        # One membership decision per participant per finalization.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="rfme_m39_dup")

        # INSERT OR REPLACE cannot silently replace the durable row.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT OR REPLACE INTO {TABLE} (membership_evidence_public_id,"
                f" finalization_id, receipt_group_public_id, membership_scope,"
                f" receipt_public_id, participant_public_id, role, is_included, created_at)"
                f" VALUES ('rfme_m39_person', 'fin_m39', 'rgrp_m39', 'receipt', 'r_m39',"
                f" 'person_m39', 'payer', 0, ?)",
                (FROZEN_TIME,),
            )

        # Real foreign keys: unknown parents are refused.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="rfme_m39_badfk", participant="person_ghost")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="rfme_m39_badrcpt", receipt_public_id="r_ghost")

        # Scope discriminator: receipt scope requires a receipt identity ...
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="rfme_m39_noscope", receipt_public_id=None)
        # ... and group scope forbids one.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {TABLE} (membership_evidence_public_id, finalization_id,"
                f" receipt_group_public_id, membership_scope, receipt_public_id,"
                f" participant_public_id, role, is_included, created_at)"
                f" VALUES ('rfme_m39_gscope', 'fin_m39', 'rgrp_m39', 'receipt_group',"
                f" 'r_m39', 'person_m39', 'participant', 1, ?)",
                (FROZEN_TIME,),
            )

        # Identity, role, and inclusion CHECK constraints.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="badprefix_m39")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="rfme_m39_badrole", role="cashier")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_evidence_row(conn, public_id="rfme_m39_badincl", is_included=7)
    finally:
        conn.close()


def test_squatter_table_fails_closed_with_no_partial_schema() -> None:
    """A pre-existing object squatting on the 039 name is invalid state.

    Migration 039 deliberately omits ``IF NOT EXISTS`` so it can never adopt
    an unknown table; the runner's per-migration transaction must roll back,
    leaving no 039 ledger row and no partial 039 schema objects.
    """
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:38], clock=_clock)
        conn.execute(f"CREATE TABLE {TABLE} (bogus INTEGER)")
        conn.commit()
        with pytest.raises(MigrationExecutionError):
            apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        rows = migration_ledger_rows(conn)
        assert rows[-1]["migration_id"] == "038"
        assert all(row["migration_id"] != "039" for row in rows)
        # No partial 039 objects: the squatter table survives untouched and
        # none of the 039 triggers or indexes exist.
        columns = [
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        ]
        assert columns == ["bogus"]
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'trg_rfme%' OR name LIKE 'idx_rfme%'"
        ).fetchall()
        assert leftover == []
    finally:
        conn.close()


def test_failure_inside_039_leaves_no_partial_state(tmp_path) -> None:
    """An injected mid-039 failure rolls back the whole migration atomically."""
    migrations_dir = tmp_path / "m039-partial"
    migrations_dir.mkdir()
    copied: list = []
    for source in TEMP_DB_MIGRATION_PATHS[:38]:
        target = migrations_dir / source.name
        target.write_bytes(source.read_bytes())
        copied.append(target)
    source_039 = next(p for p in TEMP_DB_MIGRATION_PATHS if p.name == MIGRATION_039_FILENAME)
    broken_039 = migrations_dir / MIGRATION_039_FILENAME
    broken_039.write_text(
        source_039.read_text(encoding="utf-8")
        + "\nINSERT INTO nonexistent_table_m39 VALUES (1);\n",
        encoding="utf-8",
    )
    copied.append(broken_039)

    from finance_core.reconciliation.migrations import (
        MIGRATION_029_FILENAME,
        MIGRATION_029_ID,
        MigrationPathManifest,
        MigrationPreflightArtifact,
    )
    from finance_core.reconciliation.migrations import (
        MIGRATION_029_PREFLIGHT_ARTIFACT as PREFLIGHT,
    )

    manifest = MigrationPathManifest(
        paths=tuple(copied),
        preflight_artifacts=(
            MigrationPreflightArtifact(
                migration_id=MIGRATION_029_ID,
                migration_filename=MIGRATION_029_FILENAME,
                path=PREFLIGHT,
            ),
        ),
    )
    conn = _connection()
    try:
        with pytest.raises(MigrationExecutionError):
            apply_migration_paths(conn, manifest, clock=_clock)
        rows = migration_ledger_rows(conn)
        assert rows[-1]["migration_id"] == "038"
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
            ).fetchone()
            is None
        )
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'trg_rfme%' OR name LIKE 'idx_rfme%'"
        ).fetchall()
        assert leftover == []
    finally:
        conn.close()
