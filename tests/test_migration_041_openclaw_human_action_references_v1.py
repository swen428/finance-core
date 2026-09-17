"""Migration 041 durable human-action reference tests on temporary databases."""

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

PATHS_THROUGH_041 = TEMP_DB_MIGRATION_PATHS[:41]
REFERENCE_TABLE = "openclaw_human_action_references"
REDEMPTION_TABLE = "openclaw_human_action_redemptions"


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def seed_reference(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO parser_outputs (public_id, source_type) VALUES ('parser_output_m41', 'text')"
    )
    conn.execute(
        f"""
        INSERT INTO {REFERENCE_TABLE} (
            reference_public_id, reference_sha256, issuance_idempotency_key,
            parser_output_id, action, proposal_version, proposal_content_hash,
            authenticated_actor_id, channel, channel_account_id,
            channel_conversation_id, conversation_binding_id, ttl_seconds,
            expires_at, issued_at
        ) VALUES (
            'haref_0123456789abcdef0123456789abcdef', ?,
            'bridge-human-action-issue:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            (SELECT id FROM parser_outputs WHERE public_id = 'parser_output_m41'),
            'confirm', 0, ?, '111', 'telegram', 'finance-account', '111',
            'binding-m41', 600, 2000000000, '2026-08-15T00:00:00+00:00'
        )
        """,
        ("1" * 64, "2" * 64),
    )


def test_fresh_upgrade_replay_and_manifest_are_deterministic() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_041)
        rows = migration_ledger_rows(conn)
        assert rows[-1]["migration_id"] == "041"
        assert rows[-1]["migration_filename"] == "041_openclaw_human_action_references.sql"
        verify_migration_history(conn, PATHS_THROUGH_041)
        first = [tuple(row) for row in rows]
        apply_migration_paths(conn, PATHS_THROUGH_041)
        assert [tuple(row) for row in migration_ledger_rows(conn)] == first
        manifest = build_migration_manifest(PATHS_THROUGH_041)
        assert manifest[-1].sequence == 41
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    upgraded = connection()
    try:
        apply_migration_paths(upgraded, PATHS_THROUGH_041[:40])
        assert migration_ledger_rows(upgraded)[-1]["migration_id"] == "040"
        apply_migration_paths(upgraded, PATHS_THROUGH_041)
        assert migration_ledger_rows(upgraded)[-1]["migration_id"] == "041"
    finally:
        upgraded.close()


def test_reference_and_redemption_constraints_are_append_only_and_single_use() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_041)
        seed_reference(conn)
        reference_id = conn.execute(f"SELECT id FROM {REFERENCE_TABLE}").fetchone()[0]
        conn.execute(
            f"INSERT INTO {REDEMPTION_TABLE} "
            "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
            "VALUES (?, ?, 20, '2026-08-15T00:01:00+00:00')",
            (reference_id, "3" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {REFERENCE_TABLE} SET expires_at = 2000000001")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {REFERENCE_TABLE}")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {REDEMPTION_TABLE} SET callback_message_id = 21")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {REDEMPTION_TABLE}")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {REDEMPTION_TABLE} "
                "(reference_id, callback_id_sha256, callback_message_id, redeemed_at) "
                "VALUES (?, ?, 21, '2026-08-15T00:02:00+00:00')",
                (reference_id, "4" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {REFERENCE_TABLE} (reference_public_id, reference_sha256, "
                "issuance_idempotency_key, parser_output_id, action, proposal_version, "
                "proposal_content_hash, authenticated_actor_id, channel, channel_account_id, "
                "channel_conversation_id, conversation_binding_id, ttl_seconds, "
                "expires_at, issued_at) "
                "VALUES ('haref_1123456789abcdef0123456789abcdef', ?, "
                "'bridge-human-action-issue:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "(SELECT id FROM parser_outputs WHERE public_id = 'parser_output_m41'), "
                "'confirm', 0, ?, '111', 'telegram', 'finance-account', '111', "
                "'binding-m41', 600, 2000000000, '2026-08-15T00:00:00+00:00')",
                ("5" * 64, "2" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {REFERENCE_TABLE} (reference_public_id, reference_sha256, "
                "issuance_idempotency_key, parser_output_id, action, proposal_version, "
                "proposal_content_hash, authenticated_actor_id, channel, channel_account_id, "
                "channel_conversation_id, conversation_binding_id, ttl_seconds, "
                "expires_at, issued_at) "
                f"SELECT 'haref_2123456789abcdef0123456789abcdef', ?, "
                "'bridge-human-action-issue:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
                "parser_output_id, 'edit', proposal_version, proposal_content_hash, "
                "'222', channel, channel_account_id, '111', conversation_binding_id, "
                f"ttl_seconds, expires_at, issued_at FROM {REFERENCE_TABLE} LIMIT 1",
                ("6" * 64,),
            )
    finally:
        conn.close()


def test_squatter_table_fails_closed_without_041_ledger_or_partial_objects() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_041[:40])
        conn.execute(f"CREATE TABLE {REFERENCE_TABLE} (bogus INTEGER)")
        conn.commit()
        with pytest.raises(MigrationExecutionError):
            apply_migration_paths(conn, PATHS_THROUGH_041)
        assert migration_ledger_rows(conn)[-1]["migration_id"] == "040"
        assert [row["name"] for row in conn.execute(f"PRAGMA table_info({REFERENCE_TABLE})")] == [
            "bogus"
        ]
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (REDEMPTION_TABLE,)
            ).fetchone()
            is None
        )
    finally:
        conn.close()
