"""Constraint-level backstop tests for migration 035 (B4.1 conversion registry).

Every test here bypasses the conversion service entirely and drives raw SQL
at the migrated schema, proving that migration 035's CHECK constraints,
foreign keys, uniqueness backstops, the receipts partial unique index, and
the append-only triggers each fail closed on their own even if a
service-level guard were bypassed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.test_receipt_facts_conversion_v1 import (
    participant_id,
    seed_confirmed_receipt_proposal,
    seed_people,
)

pytestmark = pytest.mark.migrated_staging_snapshot

# Full column list of receipt_proposal_conversions so every direct INSERT
# exercises the real write shape rather than relying on column defaults.
_REGISTRY_COLUMNS = (
    "command_public_id",
    "parser_output_id",
    "supersession_root_parser_output_id",
    "receipt_id",
    "confirmation_public_id",
    "proposal_content_hash",
    "command_material_hash",
    "conversion_result_hash",
    "actor_type",
    "authenticated_actor_id",
    "conversion_channel",
    "reason",
    "schema_version",
    "created_at",
)

# Messages raised by the migration 035 BEFORE INSERT collision triggers,
# which fire before UNIQUE conflict resolution (so they also stop
# INSERT OR REPLACE's implicit DELETE from rewriting history).
_CONVERSION_COLLISION = "UNIQUE conversion identity collision"
_AUDIT_COLLISION = "UNIQUE audit event identity collision"


def _insert_receipt(
    conn: sqlite3.Connection, suffix: str, parser_output_id: int | None = None
) -> int:
    """Insert a minimal receipts fact row and return its rowid.

    The canonical monetary text is populated because registry rows may only
    bind receipts that carry a schema-valid canonical amount (round 3).
    """
    cursor = conn.execute(
        """
        INSERT INTO receipts (
            public_id, merchant, net_paid_amount, net_paid_amount_canonical_text,
            currency, payer_participant_id, parser_output_id, status
        ) VALUES (?, 'Backstop Cafe', '10.00', '10.00', 'SGD', ?, ?, 'confirmed')
        """,
        (f"rcpt_{suffix}", participant_id(conn, "person_owner"), parser_output_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def seed_backstop_context(conn: sqlite3.Connection, tmp_path: Path, suffix: str) -> dict[str, Any]:
    """Seed one confirmed proposal + one receipt row as valid FK donors."""
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, suffix)
    receipt_id = _insert_receipt(conn, suffix)
    return {
        "command_public_id": f"rpfc_{suffix}",
        "parser_output_id": pid,
        "receipt_id": receipt_id,
        "confirmation_public_id": f"pca_{suffix}",
    }


def _insert_registry(
    conn: sqlite3.Connection,
    ctx: dict[str, Any],
    *,
    or_replace: bool = False,
    **overrides: Any,
) -> None:
    """Full-column INSERT into the registry; ctx supplies valid FK targets."""
    row: dict[str, Any] = {
        "command_public_id": ctx["command_public_id"],
        "parser_output_id": ctx["parser_output_id"],
        "supersession_root_parser_output_id": ctx["parser_output_id"],
        "receipt_id": ctx["receipt_id"],
        "confirmation_public_id": ctx["confirmation_public_id"],
        "proposal_content_hash": "a" * 64,
        "command_material_hash": "b" * 64,
        "conversion_result_hash": "c" * 64,
        "actor_type": "human",
        "authenticated_actor_id": "owner",
        "conversion_channel": "telegram",
        "reason": None,
        "schema_version": "v1",
        "created_at": "2026-07-25T00:00:00.000000Z",
    }
    row.update(overrides)
    columns = ", ".join(_REGISTRY_COLUMNS)
    placeholders = ", ".join("?" for _ in _REGISTRY_COLUMNS)
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    conn.execute(
        f"{verb} INTO receipt_proposal_conversions ({columns}) VALUES ({placeholders})",
        tuple(row[column] for column in _REGISTRY_COLUMNS),
    )


def _expect_rejected(
    conn: sqlite3.Connection, ctx: dict[str, Any], match: str, **overrides: Any
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=match):
        _insert_registry(conn, ctx, **overrides)
    conn.rollback()


def test_schema_backstop_accepts_valid_direct_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Positive control: the helper row satisfies every 035 constraint."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkok")
    _insert_registry(conn, ctx)
    conn.commit()
    row = conn.execute(
        "SELECT schema_version, actor_type FROM receipt_proposal_conversions "
        "WHERE command_public_id = ?",
        (ctx["command_public_id"],),
    ).fetchone()
    assert row is not None
    assert row["schema_version"] == "v1"
    assert row["actor_type"] == "human"


@pytest.mark.parametrize(
    ("column", "bogus"),
    [
        ("parser_output_id", 999_999),
        ("supersession_root_parser_output_id", 999_999),
        ("receipt_id", 999_999),
        ("confirmation_public_id", "pca_never_recorded"),
    ],
)
def test_schema_backstop_foreign_keys_reject_unknown_references(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    column: str,
    bogus: Any,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkfk")
    _expect_rejected(conn, ctx, "FOREIGN KEY constraint failed", **{column: bogus})


@pytest.mark.parametrize(
    "bad_command_id",
    [
        "xpfc_wrong_prefix",
        "rpfc_",
        "rpfc_" + "x" * 196,
        "rpfc_bad!char",
        "RPFC_uppercase_prefix",
    ],
    ids=["wrong_prefix", "too_short", "too_long", "illegal_char", "uppercase_prefix"],
)
def test_schema_backstop_malformed_command_public_id_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, bad_command_id: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkcmd")
    _expect_rejected(conn, ctx, "CHECK constraint failed", command_public_id=bad_command_id)


@pytest.mark.parametrize(
    "column",
    ["proposal_content_hash", "command_material_hash", "conversion_result_hash"],
)
@pytest.mark.parametrize(
    "bad_value",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64],
    ids=["uppercase", "too_short", "too_long", "non_hex"],
)
def test_schema_backstop_hash_columns_reject_invalid_values(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    column: str,
    bad_value: str,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkhash")
    _expect_rejected(conn, ctx, "CHECK constraint failed", **{column: bad_value})


def test_schema_backstop_non_human_actor_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkactor")
    _expect_rejected(conn, ctx, "CHECK constraint failed", actor_type="agent")


@pytest.mark.parametrize(
    ("column", "blank"),
    [
        ("authenticated_actor_id", "   "),
        ("conversion_channel", ""),
    ],
)
def test_schema_backstop_blank_required_fields_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    column: str,
    blank: str,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkblank")
    _expect_rejected(conn, ctx, "CHECK constraint failed", **{column: blank})


def test_schema_backstop_unsupported_schema_version_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkver")
    _expect_rejected(conn, ctx, "CHECK constraint failed", schema_version="v2")


def test_schema_backstop_uniqueness_constraints(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Each uniqueness backstop fires independently against a persisted row."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx_a = seed_backstop_context(conn, tmp_path, "bkuniq_a")
    ctx_b = seed_backstop_context(conn, tmp_path, "bkuniq_b")
    _insert_registry(conn, ctx_a)
    conn.commit()

    collisions = [
        ("command_public_id", ctx_a["command_public_id"]),
        ("parser_output_id", ctx_a["parser_output_id"]),
        ("supersession_root_parser_output_id", ctx_a["parser_output_id"]),
        ("receipt_id", ctx_a["receipt_id"]),
        ("confirmation_public_id", ctx_a["confirmation_public_id"]),
    ]
    for column, colliding_value in collisions:
        # The migration 035 BEFORE INSERT collision trigger fires ahead of
        # the UNIQUE backstops, so every identity collision surfaces with
        # the append-only collision message.
        _expect_rejected(
            conn,
            ctx_b,
            _CONVERSION_COLLISION,
            **{column: colliding_value},
        )

    # Positive control: the second context's fully distinct row still lands.
    _insert_registry(conn, ctx_b)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0]
    assert count == 2


def test_schema_backstop_receipts_partial_unique_index(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """One receipt fact row per proposal; NULL parser_output_id stays exempt."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkidx")
    pid = int(ctx["parser_output_id"])
    _insert_receipt(conn, "bkidx_bound", parser_output_id=pid)
    with pytest.raises(
        sqlite3.IntegrityError,
        match="UNIQUE constraint failed: receipts.parser_output_id",
    ):
        _insert_receipt(conn, "bkidx_dup", parser_output_id=pid)
    conn.rollback()
    # NULL parser_output_id rows are outside the partial index: the seed
    # context already inserted one, and a second NULL row must still land.
    _insert_receipt(conn, "bkidx_null_2", parser_output_id=None)
    conn.commit()


def test_schema_backstop_append_only_triggers(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bkao")
    _insert_registry(conn, ctx)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE receipt_proposal_conversions SET reason = 'rewritten' "
            "WHERE command_public_id = ?",
            (ctx["command_public_id"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM receipt_proposal_conversions WHERE command_public_id = ?",
            (ctx["command_public_id"],),
        )
    conn.rollback()
    row = conn.execute(
        "SELECT reason FROM receipt_proposal_conversions WHERE command_public_id = ?",
        (ctx["command_public_id"],),
    ).fetchone()
    assert row is not None
    assert row["reason"] is None


# ---------------------------------------------------------------------------
# INSERT OR REPLACE collision protection (migration 035, round 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "command_public_id",
        "parser_output_id",
        "supersession_root_parser_output_id",
        "receipt_id",
        "confirmation_public_id",
    ],
)
def test_registry_insert_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, column: str
) -> None:
    """OR REPLACE cannot rewrite history through any of the five identities.

    With ``recursive_triggers=0`` (the SQLite default) REPLACE's implicit
    DELETE bypasses the BEFORE DELETE trigger; the BEFORE INSERT collision
    trigger fires ahead of conflict resolution and aborts regardless.
    """
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    ctx_a = seed_backstop_context(conn, tmp_path, "bkrep_a")
    ctx_b = seed_backstop_context(conn, tmp_path, "bkrep_b")
    _insert_registry(conn, ctx_a)
    conn.commit()

    collision_values = {
        "command_public_id": ctx_a["command_public_id"],
        "parser_output_id": ctx_a["parser_output_id"],
        "supersession_root_parser_output_id": ctx_a["parser_output_id"],
        "receipt_id": ctx_a["receipt_id"],
        "confirmation_public_id": ctx_a["confirmation_public_id"],
    }
    with pytest.raises(sqlite3.IntegrityError, match=_CONVERSION_COLLISION):
        _insert_registry(conn, ctx_b, or_replace=True, **{column: collision_values[column]})
    conn.rollback()

    # The original registry row survives untouched and remains the only row.
    row = conn.execute(
        "SELECT proposal_content_hash, command_material_hash, conversion_result_hash "
        "FROM receipt_proposal_conversions WHERE command_public_id = ?",
        (ctx_a["command_public_id"],),
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("a" * 64, "b" * 64, "c" * 64)
    count = conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0]
    assert count == 1


# Full column list of financial_audit_events (migration 025) for direct
# INSERT shaping in trigger tests; chain semantics are not exercised here.
_AUDIT_COLUMNS = (
    "event_public_id",
    "audit_schema_version",
    "aggregate_type",
    "aggregate_public_id",
    "event_type",
    "event_payload_json",
    "previous_state_json",
    "new_state_json",
    "previous_state_hash",
    "new_state_hash",
    "previous_event_hash",
    "event_hash",
    "actor_type",
    "actor_public_id",
    "authorization_public_id",
    "source_evidence_refs_json",
    "correlation_public_id",
    "causation_public_id",
    "sequence_number",
    "created_at",
)


def _insert_audit_event(
    conn: sqlite3.Connection, *, or_replace: bool = False, **overrides: Any
) -> None:
    row: dict[str, Any] = {
        "event_public_id": "fae_bk_next",
        "audit_schema_version": "v1",
        "aggregate_type": "receipt",
        "aggregate_public_id": "rcpt_bk_other",
        "event_type": "receipt_proposal_converted_to_facts",
        "event_payload_json": "{}",
        "previous_state_json": "null",
        "new_state_json": "{}",
        "previous_state_hash": "4" * 64,
        "new_state_hash": "5" * 64,
        "previous_event_hash": "3" * 64,
        "event_hash": "2" * 64,
        "actor_type": "human",
        "actor_public_id": "owner",
        "authorization_public_id": None,
        "source_evidence_refs_json": "[]",
        "correlation_public_id": "rcpt_bk_other",
        "causation_public_id": "cmd_bk_next",
        "sequence_number": 1,
        "created_at": "2026-07-25T00:00:00.000000Z",
    }
    row.update(overrides)
    columns = ", ".join(_AUDIT_COLUMNS)
    placeholders = ", ".join("?" for _ in _AUDIT_COLUMNS)
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    conn.execute(
        f"{verb} INTO financial_audit_events ({columns}) VALUES ({placeholders})",
        tuple(row[column] for column in _AUDIT_COLUMNS),
    )


def _seed_base_audit_event(conn: sqlite3.Connection) -> None:
    _insert_audit_event(
        conn,
        event_public_id="fae_bk_base",
        aggregate_public_id="rcpt_bk_audit",
        previous_event_hash="0" * 64,
        event_hash="1" * 64,
        correlation_public_id="rcpt_bk_audit",
        causation_public_id="cmd_bk_base",
    )
    conn.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_public_id": "fae_bk_base"},
        {"event_hash": "1" * 64},
        {"aggregate_public_id": "rcpt_bk_audit", "sequence_number": 1},
        {
            "aggregate_public_id": "rcpt_bk_audit",
            "previous_event_hash": "0" * 64,
            "sequence_number": 2,
        },
        {
            "aggregate_public_id": "rcpt_bk_audit",
            "causation_public_id": "cmd_bk_base",
            "sequence_number": 2,
        },
    ],
    ids=[
        "event_public_id",
        "event_hash",
        "aggregate_sequence",
        "aggregate_previous_hash",
        "aggregate_type_causation",
    ],
)
def test_audit_insert_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection, overrides: dict[str, Any]
) -> None:
    """Every audit event identity collision aborts even under OR REPLACE."""
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    _seed_base_audit_event(conn)

    with pytest.raises(sqlite3.IntegrityError, match=_AUDIT_COLLISION):
        _insert_audit_event(conn, or_replace=True, **overrides)
    conn.rollback()

    # The seeded event survives untouched, and a plain non-colliding
    # insert for a different aggregate still lands.
    row = conn.execute(
        "SELECT event_hash, sequence_number FROM financial_audit_events "
        "WHERE event_public_id = 'fae_bk_base'"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("1" * 64, 1)
    _insert_audit_event(conn)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# Raw intake source identity freeze (migration 035, round 2)
# ---------------------------------------------------------------------------


def test_raw_intake_source_identity_frozen_after_lineage(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkfrz")
    conn.commit()

    frozen_updates = [
        ("public_id", "raw_bkfrz_renamed"),
        ("raw_input", "tampered"),
        ("source_content_hash", "sha256:" + "d" * 64),
        ("content_fingerprint", "f" * 64),
        ("fingerprint_version", "v9"),
    ]
    for column, value in frozen_updates:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="frozen once receipt proposal lineage is established",
        ):
            conn.execute(
                f"UPDATE raw_intake_records SET {column} = ? WHERE parser_output_id = ?",
                (value, pid),
            )
        conn.rollback()

    # Lifecycle-only columns stay updatable after lineage is established.
    conn.execute(
        "UPDATE raw_intake_records SET normalized_text = 'normalized' WHERE parser_output_id = ?",
        (pid,),
    )
    conn.rollback()
    conn.execute(
        "UPDATE raw_intake_records SET status = 'confirmed' WHERE parser_output_id = ?",
        (pid,),
    )
    conn.rollback()


def test_raw_intake_source_identity_updatable_before_lineage(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Intake-time backfills stay legal while no proposal lineage exists."""
    conn = migrated_temp_db_connection
    conn.execute(
        "INSERT INTO raw_intake_records "
        "(public_id, source_type, source_channel, raw_input, received_at) "
        "VALUES ('raw_bkfree', 'telegram_text', 'telegram', 'original', "
        "'2026-07-25T00:00:00.000000Z')"
    )
    conn.execute(
        "UPDATE raw_intake_records SET raw_input = 'backfilled', "
        "source_content_hash = 'sha256:abc' WHERE public_id = 'raw_bkfree'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT raw_input, source_content_hash FROM raw_intake_records "
        "WHERE public_id = 'raw_bkfree'"
    ).fetchone()
    assert tuple(row) == ("backfilled", "sha256:abc")


# ---------------------------------------------------------------------------
# Raw intake INSERT OR REPLACE / DELETE protection (migration 035, round 3)
# ---------------------------------------------------------------------------

_RAW_INTAKE_COLLISION = "UNIQUE raw intake identity collision"


def _bound_raw_intake_row(conn: sqlite3.Connection, pid: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, public_id, idempotency_key FROM raw_intake_records WHERE parser_output_id = ?",
        (pid,),
    ).fetchone()
    assert row is not None
    return row


def _insert_raw_intake(
    conn: sqlite3.Connection, public_id: str, *, or_replace: bool = False, **overrides: Any
) -> None:
    row: dict[str, Any] = {
        "id": None,
        "public_id": public_id,
        "source_type": "telegram_text",
        "source_channel": "telegram",
        "raw_input": "replacement body",
        "received_at": "2026-07-26T00:00:00.000000Z",
        "idempotency_key": None,
    }
    row.update(overrides)
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    conn.execute(
        f"{verb} INTO raw_intake_records "
        "(id, public_id, source_type, source_channel, raw_input, received_at, "
        "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row["public_id"],
            row["source_type"],
            row["source_channel"],
            row["raw_input"],
            row["received_at"],
            row["idempotency_key"],
        ),
    )


@pytest.mark.parametrize("identity", ["id", "public_id", "idempotency_key"])
def test_raw_intake_insert_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, identity: str
) -> None:
    """OR REPLACE cannot rewrite a lineage-bound raw intake row.

    With ``recursive_triggers=0`` (the SQLite default) REPLACE's implicit
    DELETE bypasses BEFORE DELETE triggers; the BEFORE INSERT collision
    trigger fires ahead of conflict resolution and aborts regardless of
    which UNIQUE identity (rowid, public_id, idempotency_key) collides.
    """
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrioR")
    bound = _bound_raw_intake_row(conn, pid)
    # idempotency_key is a lifecycle column: give the bound row one so the
    # partial UNIQUE index identity is exercised too.
    conn.execute(
        "UPDATE raw_intake_records SET idempotency_key = 'idem_bkrior' WHERE id = ?",
        (bound["id"],),
    )
    conn.commit()
    original = conn.execute(
        "SELECT public_id, raw_input FROM raw_intake_records WHERE id = ?",
        (bound["id"],),
    ).fetchone()

    new_public_id, collision = {
        "id": ("raw_bkrior_new", {"id": bound["id"]}),
        "public_id": (str(bound["public_id"]), {}),
        "idempotency_key": ("raw_bkrior_new", {"idempotency_key": "idem_bkrior"}),
    }[identity]
    with pytest.raises(sqlite3.IntegrityError, match=_RAW_INTAKE_COLLISION):
        _insert_raw_intake(conn, new_public_id, or_replace=True, **collision)
    conn.rollback()

    survived = conn.execute(
        "SELECT public_id, raw_input FROM raw_intake_records WHERE id = ?",
        (bound["id"],),
    ).fetchone()
    assert tuple(survived) == tuple(original)


def test_raw_intake_lineage_bound_delete_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkridel")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM raw_intake_records WHERE parser_output_id = ?", (pid,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM raw_intake_records")
    conn.rollback()


def test_raw_intake_unbound_rows_keep_replace_and_delete(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Rows without proposal lineage keep their permissive intake semantics."""
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    _insert_raw_intake(conn, "raw_bkriplain", raw_input="original")
    conn.commit()
    # Plain non-colliding insert stays legal.
    _insert_raw_intake(conn, "raw_bkriplain2")
    # OR REPLACE against an unbound row stays legal (out of freeze scope).
    _insert_raw_intake(conn, "raw_bkriplain", or_replace=True, raw_input="rewritten")
    conn.execute("DELETE FROM raw_intake_records WHERE public_id = 'raw_bkriplain2'")
    conn.commit()
    row = conn.execute(
        "SELECT raw_input FROM raw_intake_records WHERE public_id = 'raw_bkriplain'"
    ).fetchone()
    assert row[0] == "rewritten"


# ---------------------------------------------------------------------------
# Raw intake lineage pointer protection (migration 035, round 3)
# ---------------------------------------------------------------------------

_POINTER_FROZEN = "cannot be detached or retargeted"


def _insert_parser_output(
    conn: sqlite3.Connection,
    public_id: str,
    *,
    source_public_id: str | None = None,
    parent_parser_output_id: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parse_status, parent_parser_output_id) "
        "VALUES (?, 'telegram_text', ?, 'parsed', ?)",
        (public_id, source_public_id, parent_parser_output_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_raw_intake_pointer_detach_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrdet")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match=_POINTER_FROZEN):
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = NULL WHERE parser_output_id = ?",
            (pid,),
        )
    conn.rollback()


def test_raw_intake_detach_then_mutate_sequence_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The reproduced bypass (detach, rename, rewrite) is blocked at step one."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrseq")
    bound = _bound_raw_intake_row(conn, pid)
    conn.commit()

    # Step 1: detach the pointer - rejected.
    with pytest.raises(sqlite3.IntegrityError, match=_POINTER_FROZEN):
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = NULL WHERE id = ?",
            (bound["id"],),
        )
    conn.rollback()
    # Steps 2-3 stay independently rejected: identity and content are frozen
    # regardless of any pointer manipulation attempt.
    with pytest.raises(sqlite3.IntegrityError, match="frozen once receipt proposal"):
        conn.execute(
            "UPDATE raw_intake_records SET public_id = 'raw_bkrseq_renamed' WHERE id = ?",
            (bound["id"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="frozen once receipt proposal"):
        conn.execute(
            "UPDATE raw_intake_records SET raw_input = 'tampered', "
            "source_content_hash = 'sha256:evil' WHERE id = ?",
            (bound["id"],),
        )
    conn.rollback()


def test_raw_intake_pointer_retarget_to_unrelated_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrret")
    unrelated = _insert_parser_output(conn, "po_bkrret_unrelated")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match=_POINTER_FROZEN):
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE parser_output_id = ?",
            (unrelated, pid),
        )
    conn.rollback()


def test_raw_intake_pointer_first_bind_and_supersession_repoint_allowed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Positive controls: first legal bind and direct-child repoint survive."""
    conn = migrated_temp_db_connection
    seed_people(conn)

    # First bind: an unbound intake row may establish its pointer once.
    _insert_raw_intake(conn, "raw_bkrbind", raw_input="fresh body")
    fresh_po = _insert_parser_output(conn, "po_bkrbind", source_public_id="raw_bkrbind")
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ?, "
        "status = 'parsed_pending_confirmation' WHERE public_id = 'raw_bkrbind'",
        (fresh_po,),
    )
    conn.commit()

    # Supersession repoint: retargeting to a direct child of the current
    # pointer mirrors the guarded revision workflow and stays legal.
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrchild")
    bound = _bound_raw_intake_row(conn, pid)
    parent = conn.execute(
        "SELECT source_type, source_public_id FROM parser_outputs WHERE id = ?", (pid,)
    ).fetchone()
    cursor = conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parse_status, parent_parser_output_id) "
        "VALUES ('po_bkrchild_repl', ?, ?, 'parsed', ?)",
        (parent["source_type"], parent["source_public_id"], pid),
    )
    child = int(cursor.lastrowid or 0)
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ?, "
        "status = 'parsed_pending_confirmation' WHERE id = ?",
        (child, bound["id"]),
    )
    conn.commit()
    row = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE id = ?", (bound["id"],)
    ).fetchone()
    assert row[0] == child


# ---------------------------------------------------------------------------
# Conversion-bound receipt facts immutability (migration 035, round 3)
# ---------------------------------------------------------------------------

_RECEIPT_FROZEN = "conversion-bound receipt facts are immutable"


def _seed_conversion_bound_receipt(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> dict[str, Any]:
    """Registry-bound receipt via direct SQL: the schema-level binding."""
    ctx = seed_backstop_context(conn, tmp_path, suffix)
    _insert_registry(conn, ctx)
    conn.commit()
    return ctx


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("public_id", "rcpt_bkfrz_renamed"),
        ("merchant", "Tampered Cafe"),
        ("receipt_datetime", "2026-07-26T09:00:00.000000Z"),
        ("net_paid_amount", "88.88"),
        ("net_paid_amount_canonical_text", "99.99"),
        ("currency", "USD"),
        ("payer_participant_id", None),  # filled with person_alice below
        ("source_channel", "tampered"),
        ("raw_input", "tampered raw"),
        ("attachment_id", None),  # filled with a real attachment below
        ("attachment_path", "/tmp/tampered.jpg"),
        ("ocr_confidence", 0.5),
        ("parser_output_id", None),  # filled with a fresh parser output below
    ],
)
def test_conversion_bound_receipt_update_rejected_per_column(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    column: str,
    value: Any,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_conversion_bound_receipt(conn, tmp_path, "bkrcfrz")
    if column == "payer_participant_id":
        value = participant_id(conn, "person_alice")
    elif column == "attachment_id":
        cursor = conn.execute(
            "INSERT INTO attachments (public_id, attachment_type, file_path) "
            "VALUES ('att_bkrcfrz_new', 'receipt_image', '/tmp/bkrcfrz_new.jpg')"
        )
        value = int(cursor.lastrowid or 0)
    elif column == "parser_output_id":
        value = _insert_parser_output(conn, "po_bkrcfrz_new")

    with pytest.raises(sqlite3.IntegrityError, match=_RECEIPT_FROZEN):
        conn.execute(
            f"UPDATE receipts SET {column} = ? WHERE id = ?",
            (value, ctx["receipt_id"]),
        )
    conn.rollback()


def test_conversion_bound_receipt_delete_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_conversion_bound_receipt(conn, tmp_path, "bkrcdel")

    with pytest.raises(sqlite3.IntegrityError, match=_RECEIPT_FROZEN):
        conn.execute("DELETE FROM receipts WHERE id = ?", (ctx["receipt_id"],))
    conn.rollback()


@pytest.mark.parametrize("identity", ["id", "public_id", "parser_output_id"])
def test_conversion_bound_receipt_insert_or_replace_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, identity: str
) -> None:
    """OR REPLACE cannot rewrite a registry-bound receipt via any identity."""
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrcrep")
    receipt_id = _insert_receipt(conn, "bkrcrep", parser_output_id=pid)
    ctx = {
        "command_public_id": "rpfc_bkrcrep",
        "parser_output_id": pid,
        "receipt_id": receipt_id,
        "confirmation_public_id": "pca_bkrcrep",
    }
    _insert_registry(conn, ctx)
    conn.commit()

    collision_sql = {
        "id": ("id, public_id", f"{receipt_id}, 'rcpt_bkrcrep_new'"),
        "public_id": ("public_id", "'rcpt_bkrcrep'"),
        "parser_output_id": ("public_id, parser_output_id", f"'rcpt_bkrcrep_new', {pid}"),
    }[identity]
    with pytest.raises(sqlite3.IntegrityError, match=_RECEIPT_FROZEN):
        conn.execute(
            f"INSERT OR REPLACE INTO receipts ({collision_sql[0]}, merchant, "
            "net_paid_amount, net_paid_amount_canonical_text, currency, "
            "payer_participant_id, status) "
            f"VALUES ({collision_sql[1]}, 'Rewritten Cafe', '1.00', '1.00', 'SGD', "
            f"{participant_id(conn, 'person_owner')}, 'confirmed')"
        )
    conn.rollback()

    row = conn.execute(
        "SELECT merchant, net_paid_amount_canonical_text FROM receipts WHERE id = ?",
        (receipt_id,),
    ).fetchone()
    assert tuple(row) == ("Backstop Cafe", "10.00")


def test_legacy_unbound_receipt_keeps_update_and_delete(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Receipts outside the conversion registry keep their legacy behaviour."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    receipt_id = _insert_receipt(conn, "bkrclegacy")
    conn.commit()
    conn.execute(
        "UPDATE receipts SET merchant = 'Renamed Cafe', net_paid_amount = '12.00' WHERE id = ?",
        (receipt_id,),
    )
    conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.commit()


def test_conversion_bound_receipt_lifecycle_columns_stay_mutable(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Status/notes stay open for future guarded finalization workflows."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_conversion_bound_receipt(conn, tmp_path, "bkrclife")
    conn.execute(
        "UPDATE receipts SET status = 'needs_review', notes = 'flagged' WHERE id = ?",
        (ctx["receipt_id"],),
    )
    conn.rollback()


# ---------------------------------------------------------------------------
# Exact canonical monetary text column (migration 035, rounds 2-3)
# ---------------------------------------------------------------------------


def _insert_receipt_with_canonical(
    conn: sqlite3.Connection, value: str | None, currency: str = "SGD"
) -> None:
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, net_paid_amount, currency, "
        "payer_participant_id, status, net_paid_amount_canonical_text) "
        "VALUES ('rcpt_bkcanon', 'Canonical Cafe', '10.00', ?, ?, 'confirmed', ?)",
        (currency, participant_id(conn, "person_owner"), value),
    )


@pytest.mark.parametrize(
    ("currency", "value"),
    [
        ("SGD", "12.34"),
        ("SGD", "0.05"),
        ("SGD", "123456789.12"),
        ("USD", "1.00"),
        ("EUR", "0.01"),
        ("GBP", "10.99"),
        ("AUD", "7.50"),
        ("CNY", "88.88"),
        ("HKD", "100.00"),
        ("JPY", "100"),
        ("JPY", "5"),
        ("JPY", "0"),
    ],
)
def test_receipts_canonical_text_accepts_currency_scale(
    migrated_temp_db_connection: sqlite3.Connection, currency: str, value: str
) -> None:
    """Canonical text in exact minor-unit scale for its currency is stored."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    _insert_receipt_with_canonical(conn, value, currency)
    conn.commit()
    row = conn.execute(
        "SELECT net_paid_amount_canonical_text FROM receipts WHERE public_id = 'rcpt_bkcanon'"
    ).fetchone()
    # TEXT affinity: the canonical string is stored byte-exact.
    assert row[0] == value


def test_receipts_canonical_text_null_stays_legal_for_legacy_rows(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _insert_receipt_with_canonical(conn, None, "SGD")
    conn.commit()


@pytest.mark.parametrize(
    ("currency", "value"),
    [
        # Structural garbage (round 2 coverage retained).
        ("SGD", ""),
        ("SGD", ".5"),
        ("SGD", "10."),
        ("SGD", "1.2.3"),
        ("SGD", "10,00"),
        ("SGD", "abc"),
        ("SGD", "-5"),
        ("SGD", "1e5"),
        ("SGD", "+3"),
        ("SGD", " 10.00"),
        # Currency-scale and canonical-form violations (round 3).
        ("SGD", "1.234"),
        ("SGD", "1.2"),
        ("SGD", "01.00"),
        ("SGD", "00.10"),
        ("SGD", "10"),
        ("SGD", "123456789.123456"),
        ("JPY", "10.50"),
        ("JPY", "10.00"),
        ("JPY", "010"),
        # Unknown currencies fail closed instead of accepting any shape.
        ("XXX", "10.00"),
    ],
)
def test_receipts_canonical_text_rejects_non_canonical_text(
    migrated_temp_db_connection: sqlite3.Connection, currency: str, value: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_receipt_with_canonical(conn, value, currency)
    conn.rollback()


def test_registry_rejects_receipt_without_canonical_amount(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A registry row may only bind receipts carrying canonical monetary text."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = seed_backstop_context(conn, tmp_path, "bknocanon")
    cursor = conn.execute(
        "INSERT INTO receipts (public_id, merchant, net_paid_amount, currency, "
        "payer_participant_id, status) "
        "VALUES ('rcpt_bknocanon_bare', 'Bare Cafe', '10.00', 'SGD', ?, 'confirmed')",
        (participant_id(conn, "person_owner"),),
    )
    assert cursor.lastrowid is not None
    with pytest.raises(sqlite3.IntegrityError, match="canonical monetary text"):
        _insert_registry(conn, ctx, receipt_id=int(cursor.lastrowid))
    conn.rollback()


# ---------------------------------------------------------------------------
# UPDATE OR REPLACE collision protection (migration 035, round 4)
# ---------------------------------------------------------------------------

# UPDATE OR REPLACE resolves a UNIQUE collision exactly like INSERT OR
# REPLACE: an implicit DELETE of the *other* row that, with
# recursive_triggers off, bypasses its BEFORE DELETE trigger.  The BEFORE
# UPDATE triggers of the attacked row do not help because the attacker row
# is unbound.  The round-4 BEFORE UPDATE collision guards abort whenever a
# NEW identity collides with a different protected row, and the identity
# freezes now cover the bound row's own integer primary key too.


def _raw_intake_row_snapshot(conn: sqlite3.Connection, row_id: int) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT rowid, * FROM raw_intake_records WHERE rowid = ?", (row_id,)
    ).fetchone()
    assert row is not None
    return tuple(row)


def _receipt_row_snapshot(conn: sqlite3.Connection, row_id: int) -> tuple[Any, ...]:
    row = conn.execute("SELECT rowid, * FROM receipts WHERE rowid = ?", (row_id,)).fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.parametrize("fk_enabled", [True, False])
@pytest.mark.parametrize("identity", ["id", "public_id", "idempotency_key"])
def test_raw_intake_update_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    identity: str,
    fk_enabled: bool,
) -> None:
    """An unbound row cannot be UPDATE OR REPLACE'd onto a bound identity.

    Reproduced bypass: with recursive_triggers=0 (and even foreign_keys=0)
    UPDATE OR REPLACE from an unbound attacker row deletes the
    lineage-bound target row without firing its DELETE trigger.  The
    BEFORE UPDATE collision guard must abort before conflict resolution.
    """
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkriuor")
    bound = _bound_raw_intake_row(conn, pid)
    conn.execute(
        "UPDATE raw_intake_records SET idempotency_key = 'idem_bkriuor' WHERE id = ?",
        (bound["id"],),
    )
    _insert_raw_intake(conn, "raw_bkriuor_attacker", raw_input="attacker body")
    conn.commit()
    bound_before = _raw_intake_row_snapshot(conn, int(bound["id"]))

    set_sql, params = {
        "id": ("id = ?", (int(bound["id"]),)),
        "public_id": ("public_id = ?", (str(bound["public_id"]),)),
        "idempotency_key": ("idempotency_key = ?", ("idem_bkriuor",)),
    }[identity]
    if not fk_enabled:
        conn.execute("PRAGMA foreign_keys = OFF")
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
    try:
        with pytest.raises(sqlite3.IntegrityError, match=_RAW_INTAKE_COLLISION):
            conn.execute(
                f"UPDATE OR REPLACE raw_intake_records SET {set_sql} "
                "WHERE public_id = 'raw_bkriuor_attacker'",
                params,
            )
        conn.rollback()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # The protected row survives byte-identical and its lineage references
    # (pointer and parser output source binding) stay intact.
    assert _raw_intake_row_snapshot(conn, int(bound["id"])) == bound_before
    lineage = conn.execute(
        "SELECT COUNT(*) FROM parser_outputs WHERE id = ? AND source_public_id = ?",
        (pid, bound["public_id"]),
    ).fetchone()
    assert int(lineage[0]) == 1


def test_raw_intake_bound_row_own_id_frozen(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A lineage-bound row's integer primary key itself is frozen."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkriid")
    bound = _bound_raw_intake_row(conn, pid)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        conn.execute("UPDATE raw_intake_records SET id = 987654 WHERE id = ?", (bound["id"],))
    conn.rollback()


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid"])
def test_raw_intake_bound_row_rowid_alias_frozen(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, alias: str
) -> None:
    """R4 review: a rowid-alias UPDATE must not bypass the identity freeze.

    ``UPDATE ... SET rowid = ...`` changes the INTEGER PRIMARY KEY without
    naming ``id`` in the SET list, so a ``BEFORE UPDATE OF id`` trigger
    would never fire; the freeze trigger must be a plain BEFORE UPDATE.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrira")
    bound = _bound_raw_intake_row(conn, pid)
    conn.commit()
    before = _raw_intake_row_snapshot(conn, bound["id"])

    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        conn.execute(
            f"UPDATE raw_intake_records SET {alias} = 987654 WHERE id = ?",
            (bound["id"],),
        )
    conn.rollback()
    assert _raw_intake_row_snapshot(conn, bound["id"]) == before


@pytest.mark.parametrize("fk_enabled", [True, False])
@pytest.mark.parametrize("identity", ["id", "public_id", "parser_output_id"])
def test_receipts_update_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    identity: str,
    fk_enabled: bool,
) -> None:
    """An unbound receipt cannot be UPDATE OR REPLACE'd onto a bound identity."""
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    pid, _public_id, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "bkrcuor")
    receipt_id = _insert_receipt(conn, "bkrcuor", parser_output_id=pid)
    ctx = {
        "command_public_id": "rpfc_bkrcuor",
        "parser_output_id": pid,
        "receipt_id": receipt_id,
        "confirmation_public_id": "pca_bkrcuor",
    }
    _insert_registry(conn, ctx)
    attacker_id = _insert_receipt(conn, "bkrcuor_attacker")
    conn.commit()
    bound_before = _receipt_row_snapshot(conn, receipt_id)
    registry_before = tuple(
        conn.execute(
            "SELECT * FROM receipt_proposal_conversions WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
    )

    set_sql, params = {
        "id": ("id = ?", (receipt_id,)),
        "public_id": ("public_id = ?", ("rcpt_bkrcuor",)),
        "parser_output_id": ("parser_output_id = ?", (pid,)),
    }[identity]
    if not fk_enabled:
        conn.execute("PRAGMA foreign_keys = OFF")
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
    try:
        with pytest.raises(sqlite3.IntegrityError, match=_RECEIPT_FROZEN):
            conn.execute(
                f"UPDATE OR REPLACE receipts SET {set_sql} WHERE id = ?",
                (*params, attacker_id),
            )
        conn.rollback()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # Bound facts byte-identical and the registry binding untouched.
    assert _receipt_row_snapshot(conn, receipt_id) == bound_before
    registry_after = tuple(
        conn.execute(
            "SELECT * FROM receipt_proposal_conversions WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
    )
    assert registry_after == registry_before


def test_receipt_bound_row_own_id_frozen(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A conversion-bound receipt's integer primary key itself is frozen."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_conversion_bound_receipt(conn, tmp_path, "bkrcid")

    with pytest.raises(sqlite3.IntegrityError, match=_RECEIPT_FROZEN):
        conn.execute("UPDATE receipts SET id = 987654 WHERE id = ?", (ctx["receipt_id"],))
    conn.rollback()


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid"])
def test_receipt_bound_row_rowid_alias_frozen(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, alias: str
) -> None:
    """R4 review: a rowid-alias UPDATE must not bypass the receipt freeze.

    Changing a bound receipt's INTEGER PRIMARY KEY via ``SET rowid =``
    would detach it from every ``receipt_id``-keyed conversion guard, so
    the freeze trigger must fire on plain BEFORE UPDATE, not an OF list.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_conversion_bound_receipt(conn, tmp_path, f"bkrra{alias.strip('_')}")
    before = _receipt_row_snapshot(conn, ctx["receipt_id"])

    with pytest.raises(sqlite3.IntegrityError, match=_RECEIPT_FROZEN):
        conn.execute(
            f"UPDATE receipts SET {alias} = 987654 WHERE id = ?",
            (ctx["receipt_id"],),
        )
    conn.rollback()
    assert _receipt_row_snapshot(conn, ctx["receipt_id"]) == before


# ---------------------------------------------------------------------------
# Conversion-bound receipt_participants immutability (migration 035, round 4)
# ---------------------------------------------------------------------------

# Membership rows written by the conversion are authoritative Facts: once
# their receipt is registry-bound, no INSERT/UPDATE/DELETE may touch the
# membership set, and REPLACE conflict resolution (implicit DELETE with
# recursive_triggers off) must abort before it can rewrite a bound row on
# any UNIQUE identity: rowid primary key, public_id, or the
# (receipt_id, participant_id) pair.  Unbound legacy receipts keep their
# existing mutable membership behaviour.

_MEMBERSHIP_FROZEN = "conversion-bound receipt membership is immutable"
_MEMBERSHIP_COLLISION = "UNIQUE receipt membership identity collision"


def _insert_membership(
    conn: sqlite3.Connection,
    public_id: str,
    receipt_id: int,
    participant_public_id: str,
    role: str,
    is_included: int,
    *,
    or_replace: bool = False,
) -> int:
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    cursor = conn.execute(
        f"""
        {verb} INTO receipt_participants (
            public_id, receipt_id, participant_id, role, is_included
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (public_id, receipt_id, participant_id(conn, participant_public_id), role, is_included),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _membership_row_snapshot(conn: sqlite3.Connection, row_id: int) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT rowid, * FROM receipt_participants WHERE rowid = ?", (row_id,)
    ).fetchone()
    assert row is not None
    return tuple(row)


def _seed_bound_membership(conn: sqlite3.Connection, tmp_path: Path, suffix: str) -> dict[str, Any]:
    """Registry-bound receipt with membership rows via direct SQL.

    Mirrors the conversion write order: membership rows are inserted before
    the registry binding row (the service writes receipt -> participants ->
    registry), so the bound-membership no-insert backstop cannot fire here.
    """
    ctx = seed_backstop_context(conn, tmp_path, suffix)
    ctx["payer_membership_id"] = _insert_membership(
        conn, f"rcpp_{suffix}_payer", ctx["receipt_id"], "person_owner", "payer", 1
    )
    ctx["member_membership_id"] = _insert_membership(
        conn, f"rcpp_{suffix}_alice", ctx["receipt_id"], "person_alice", "participant", 1
    )
    ctx["member_public_id"] = f"rcpp_{suffix}_alice"
    _insert_registry(conn, ctx)
    conn.commit()
    return ctx


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("id", 987654),
        ("public_id", "rcpp_bkmupd_forged"),
        ("role", "observer"),
        ("is_included", 0),
        ("notes", "tampered"),
    ],
)
def test_bound_membership_update_rejected_per_column(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    column: str,
    value: Any,
) -> None:
    """Every UPDATE against a bound membership row fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_bound_membership(conn, tmp_path, "bkmupd")
    before = _membership_row_snapshot(conn, ctx["member_membership_id"])

    with pytest.raises(sqlite3.IntegrityError, match=_MEMBERSHIP_FROZEN):
        conn.execute(
            f"UPDATE receipt_participants SET {column} = ? WHERE id = ?",
            (value, ctx["member_membership_id"]),
        )
    conn.rollback()
    assert _membership_row_snapshot(conn, ctx["member_membership_id"]) == before


def test_bound_membership_participant_id_update_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Repointing a bound membership row at another participant fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_bound_membership(conn, tmp_path, "bkmpid")

    with pytest.raises(sqlite3.IntegrityError, match=_MEMBERSHIP_FROZEN):
        conn.execute(
            "UPDATE receipt_participants SET participant_id = ? WHERE id = ?",
            (participant_id(conn, "person_bob"), ctx["member_membership_id"]),
        )
    conn.rollback()


def test_bound_membership_delete_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Deleting any bound membership row fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_bound_membership(conn, tmp_path, "bkmdel")

    with pytest.raises(sqlite3.IntegrityError, match=_MEMBERSHIP_FROZEN):
        conn.execute(
            "DELETE FROM receipt_participants WHERE id = ?",
            (ctx["member_membership_id"],),
        )
    conn.rollback()


def test_bound_membership_extra_insert_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Adding a membership row to a registry-bound receipt fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    ctx = _seed_bound_membership(conn, tmp_path, "bkmins")

    with pytest.raises(sqlite3.IntegrityError, match=_MEMBERSHIP_FROZEN):
        _insert_membership(
            conn, "rcpp_bkmins_extra", ctx["receipt_id"], "person_bob", "participant", 1
        )
    conn.rollback()


@pytest.mark.parametrize("fk_enabled", [True, False])
@pytest.mark.parametrize("identity", ["id", "public_id", "pair"])
def test_membership_insert_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    identity: str,
    fk_enabled: bool,
) -> None:
    """INSERT OR REPLACE cannot rewrite a bound membership row.

    With recursive_triggers=0 (and even foreign_keys=0), REPLACE's implicit
    DELETE of the collided bound row bypasses DELETE triggers; the BEFORE
    INSERT collision guard must abort before conflict resolution.
    """
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    ctx = _seed_bound_membership(conn, tmp_path, "bkmior")
    unbound_receipt_id = _insert_receipt(conn, "bkmior_unbound")
    conn.commit()
    bound_before = _membership_row_snapshot(conn, ctx["member_membership_id"])

    if not fk_enabled:
        conn.execute("PRAGMA foreign_keys = OFF")
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
    try:
        with pytest.raises(sqlite3.IntegrityError, match="receipt membership"):
            if identity == "id":
                conn.execute(
                    """
                    INSERT OR REPLACE INTO receipt_participants (
                        id, public_id, receipt_id, participant_id, role, is_included
                    ) VALUES (?, 'rcpp_bkmior_attacker', ?, ?, 'participant', 1)
                    """,
                    (
                        ctx["member_membership_id"],
                        unbound_receipt_id,
                        participant_id(conn, "person_bob"),
                    ),
                )
            elif identity == "public_id":
                _insert_membership(
                    conn,
                    ctx["member_public_id"],
                    unbound_receipt_id,
                    "person_bob",
                    "participant",
                    1,
                    or_replace=True,
                )
            else:
                _insert_membership(
                    conn,
                    "rcpp_bkmior_attacker",
                    ctx["receipt_id"],
                    "person_alice",
                    "participant",
                    0,
                    or_replace=True,
                )
        conn.rollback()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    assert _membership_row_snapshot(conn, ctx["member_membership_id"]) == bound_before


@pytest.mark.parametrize("fk_enabled", [True, False])
@pytest.mark.parametrize("identity", ["id", "public_id", "pair"])
def test_membership_update_or_replace_rejected_per_identity(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    identity: str,
    fk_enabled: bool,
) -> None:
    """An unbound membership row cannot be UPDATE OR REPLACE'd onto a bound identity."""
    conn = migrated_temp_db_connection
    conn.execute("PRAGMA recursive_triggers = 0")
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 0
    seed_people(conn)
    ctx = _seed_bound_membership(conn, tmp_path, "bkmuor")
    unbound_receipt_id = _insert_receipt(conn, "bkmuor_unbound")
    attacker_id = _insert_membership(
        conn, "rcpp_bkmuor_attacker", unbound_receipt_id, "person_bob", "participant", 1
    )
    conn.commit()
    bound_before = _membership_row_snapshot(conn, ctx["member_membership_id"])

    set_sql, params = {
        "id": ("id = ?", (ctx["member_membership_id"],)),
        "public_id": ("public_id = ?", (ctx["member_public_id"],)),
        "pair": (
            "receipt_id = ?, participant_id = ?",
            (ctx["receipt_id"], participant_id(conn, "person_alice")),
        ),
    }[identity]
    if not fk_enabled:
        conn.execute("PRAGMA foreign_keys = OFF")
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
    try:
        with pytest.raises(sqlite3.IntegrityError, match="receipt membership"):
            conn.execute(
                f"UPDATE OR REPLACE receipt_participants SET {set_sql} WHERE id = ?",
                (*params, attacker_id),
            )
        conn.rollback()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    assert _membership_row_snapshot(conn, ctx["member_membership_id"]) == bound_before


def test_unbound_membership_stays_mutable(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Positive control: legacy receipts keep mutable membership rows."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    receipt_id = _insert_receipt(conn, "bkmlegacy")
    row_id = _insert_membership(
        conn, "rcpp_bkmlegacy", receipt_id, "person_alice", "participant", 1
    )

    conn.execute("UPDATE receipt_participants SET is_included = 0 WHERE id = ?", (row_id,))
    conn.execute("UPDATE receipt_participants SET role = 'excluded' WHERE id = ?", (row_id,))
    conn.execute("DELETE FROM receipt_participants WHERE id = ?", (row_id,))
    conn.rollback()
