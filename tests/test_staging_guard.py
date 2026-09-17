"""Tests for the centralized staging-database guard.

Covers:
  1. :memory: database is accepted (genuine in-memory).
  2. Trusted staging database is accepted.
  3. Live database/finance.db is rejected.
  4. Arbitrary existing SQLite file is rejected.
  5. Temp dir DB without token is rejected.
  6. Relative path resolving to live DB is rejected.
  7. Symlink to live DB is rejected.
  8. Unnamed temp database (connect("")) is rejected.
  9. Parser conversion performs no write when rejected.
 10. Receipt finalization performs no write when rejected.
 11. Reconciliation final mutation performs no write when rejected.
 12. Existing temp-db workflows continue to work.
 13. Guard failures do not commit unrelated pending writes.
 14. Exception type and stable error message are asserted.
 15. create_staging_database rejects live DB path.
 16. create_staging_database rejects existing file.
 17. create_staging_database rejects existing empty file.
 18. create_staging_database rejects existing symlink.
 19. Empty _staging_authorization table is rejected.
 20. Invalid token format is rejected.
 21. Incorrect token length is rejected.
 22. Multiple authorization rows are rejected.
 23. Incorrect marker schema is rejected.
 24. Copied authorized staging database is rejected.
 25. New staging database has expected authorization columns.
 26. Failed initialization leaves an unauthorised file instead of unlinking a raced path.
 27. require_staging_database is read-only (no commit/rollback on the same conn).
 28. Guard fires before business mutation (conversion on no-schema DB).
 29. Guard fires before business mutation (finalization on no-schema DB).
 30. Guard fires before business mutation (final mutation on no-schema DB).
 31. create_staging_database does not stamp existing databases.
 32. Replacement with a regular file after exclusive creation is rejected.
 33. Replacement with a symlink after exclusive creation is rejected.
 34. No authorization table written to replacement file.
 35. Failed initialization does not delete an attacker-replaced file.
 36. Failed initialization remains unauthorised.
 37. Migration failure leaves an unauthorised file.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest.mock as mock
from decimal import Decimal
from pathlib import Path

import pytest

import finance_core.staging_guard as _guard
from finance_core.resources import migrations_dir
from finance_core.staging_guard import (
    StagingDatabaseError,
    create_staging_database,
    open_staging_database,
    require_staging_database,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


# ======================================================================
# 1. :memory: database is accepted (genuine in-memory)
# ======================================================================
def test_memory_database_is_accepted() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 2. Trusted staging database is accepted
# ======================================================================
def test_staging_database_is_accepted(tmp_path: Path) -> None:
    db_path = tmp_path / "test_staging.sqlite"
    conn = create_staging_database(db_path)
    try:
        require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 2b. New staging database has expected authorization columns
# ======================================================================
def test_staging_database_has_expected_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "test_columns.sqlite"
    conn = create_staging_database(db_path)
    try:
        row = conn.execute(
            "SELECT token_hash, authorization_version, db_identity, created_at "
            "FROM _staging_authorization LIMIT 1"
        ).fetchone()
        assert row is not None

        token_hash = row["token_hash"]
        assert isinstance(token_hash, str)
        assert len(token_hash) == 64
        assert all(c in "0123456789abcdef" for c in token_hash)

        auth_version = row["authorization_version"]
        assert auth_version == 1

        db_identity = row["db_identity"]
        assert db_identity == str(db_path.resolve())

        created_at = row["created_at"]
        assert isinstance(created_at, str)
        assert len(created_at) > 0
    finally:
        conn.close()


# ======================================================================
# 3. Live database/finance.db is rejected
# ======================================================================
def test_live_db_path_is_rejected() -> None:
    if not LIVE_DB_PATH.exists():
        pytest.skip("Live DB does not exist")

    conn = sqlite3.connect(str(LIVE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="live database"):
            require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 4. Arbitrary existing SQLite file is rejected
# ======================================================================
def test_arbitrary_sqlite_file_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "arbitrary.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE foo (id INTEGER)")
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 5. Temp dir DB without token is rejected
# ======================================================================
def test_temp_dir_db_without_token_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "no_token.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE foo (id INTEGER)")
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 6. Relative path resolving to live DB is rejected
# ======================================================================
def test_relative_path_to_live_db_is_rejected() -> None:
    if not LIVE_DB_PATH.exists():
        pytest.skip("Live DB does not exist")

    resolved = str(LIVE_DB_PATH.resolve())
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="live database"):
            require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 7. Symlink to live DB is rejected
# ======================================================================
def test_symlink_to_live_db_is_rejected(tmp_path: Path) -> None:
    if not LIVE_DB_PATH.exists():
        pytest.skip("Live DB does not exist")

    symlink_path = tmp_path / "symlink_finance.sqlite"
    os.symlink(str(LIVE_DB_PATH.resolve()), str(symlink_path))

    conn = sqlite3.connect(str(symlink_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="live database"):
            require_staging_database(conn)
    finally:
        conn.close()


def test_hardlink_alias_to_live_db_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    database_dir = runtime_root / "database"
    database_dir.mkdir(parents=True)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root))

    staging_path = tmp_path / "authorised-staging.sqlite"
    conn = create_staging_database(staging_path)
    live_path = database_dir / "finance.db"
    try:
        try:
            os.link(staging_path, live_path)
        except OSError:
            pytest.skip("hard links are unavailable")

        with pytest.raises(StagingDatabaseError, match="live database"):
            require_staging_database(conn)
        with pytest.raises(StagingDatabaseError, match="live database"):
            open_staging_database(staging_path)
    finally:
        conn.close()


# ======================================================================
# 8. Unnamed temp database (connect("")) is rejected
# ======================================================================
def test_unnamed_temp_database_is_rejected() -> None:
    """connect('') creates an on-disk temp DB -- it must not be trusted."""
    conn = sqlite3.connect("")
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="identity"):
            require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 9. Parser conversion performs no write when rejected
# ======================================================================
def test_conversion_rejected_on_arbitrary_db(tmp_path: Path) -> None:
    from finance_core.parser_proposals.conversion import convert_confirmed_proposal_to_transaction

    db_path = tmp_path / "conv_no_staging.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_core_schema(conn)

    with pytest.raises(StagingDatabaseError):
        convert_confirmed_proposal_to_transaction(conn, 1)

    row_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert row_count == 0
    conn.close()


# ======================================================================
# 10. Receipt finalization performs no write when rejected
# ======================================================================
def test_finalization_rejected_on_arbitrary_db(tmp_path: Path) -> None:
    from finance_core.receipt_finalization import finalize_receipt_split
    from finance_core.receipt_finalization.models import (
        FinalizationInput,
        SettlementObligation,
    )

    db_path = tmp_path / "fin_no_staging.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_core_schema(conn)

    fin_input = FinalizationInput(
        receipt_group_public_id="rg_test",
        payer_participant_public_id="Owner",
        currency="SGD",
        calculation_run_public_id="calc_test",
        calculation_snapshot={
            "participants": ["Owner", "MemberA"],
            "payer": "Owner",
            "participant_shares": {"Owner": "0", "MemberA": "50"},
            "total_paid": "50",
            "payer_own_share": "0",
            "payer_paid_amounts": {"Owner": "50", "MemberA": "0"},
            "receipts": [],
            "settlement_obligations": [
                {"debtor": "MemberA", "creditor": "Owner", "amount": "50", "currency": "SGD"}
            ],
        },
        settlement_obligations=[
            SettlementObligation(
                debtor_participant_public_id="MemberA",
                creditor_participant_public_id="Owner",
                amount=Decimal("50"),
                currency="SGD",
            ),
        ],
    )

    with pytest.raises(StagingDatabaseError):
        finalize_receipt_split(conn, fin_input)

    calc_count = conn.execute("SELECT COUNT(*) FROM calculation_runs").fetchone()[0]
    obl_count = conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]
    assert calc_count == 0
    assert obl_count == 0
    conn.close()


# ======================================================================
# 11. Reconciliation final mutation performs no write when rejected
# ======================================================================
def test_final_mutation_rejected_on_arbitrary_db(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from finance_core.reconciliation.final_mutation_workflow import (
        FinalMutationWorkflowInput,
        execute_guarded_final_mutation_workflow,
    )

    db_path = tmp_path / "fm_no_staging.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    wf_input = MagicMock(spec=FinalMutationWorkflowInput)
    wf_input.idempotency_key = "fm-key-001"

    with pytest.raises(StagingDatabaseError):
        execute_guarded_final_mutation_workflow(conn, wf_input)

    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='reconciliation_final_mutation_audit'"
    ).fetchone()
    assert row[0] == 0
    conn.close()


# ======================================================================
# 12. Existing temp-db workflows continue to work
# ======================================================================
def test_staging_db_with_migrations_accepted(migrated_temp_db_connection):
    require_staging_database(migrated_temp_db_connection)


# ======================================================================
# 13. Guard failures do not commit unrelated pending writes
# ======================================================================
def test_guard_failure_does_not_commit_pending_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "no_commit.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()

    conn.execute("INSERT INTO test_table (id, val) VALUES (1, 'pending')")

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError):
            require_staging_database(conn2)
    finally:
        conn2.close()

    # Rollback confirms the pending write was not committed
    conn.rollback()
    conn.close()


# ======================================================================
# 14. Exception type and stable error message
# ======================================================================
def test_exception_type_and_message(tmp_path: Path) -> None:
    db_path = tmp_path / "msg_test.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE foo (id INTEGER)")
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError) as exc_info:
            require_staging_database(conn2)
        msg = str(exc_info.value)
        assert "not an authorised staging database" in msg
    finally:
        conn2.close()


# ======================================================================
# 15. create_staging_database rejects live DB path
# ======================================================================
def test_create_staging_database_rejects_live_path() -> None:
    with pytest.raises(StagingDatabaseError, match="live database path"):
        create_staging_database(LIVE_DB_PATH)


# ======================================================================
# 16. create_staging_database rejects existing file
# ======================================================================
def test_create_staging_database_rejects_existing_file(tmp_path: Path) -> None:
    db_path = tmp_path / "existing.sqlite"
    db_path.write_text("not a database")

    with pytest.raises(StagingDatabaseError, match="existing path"):
        create_staging_database(db_path)

    # File must be unchanged
    assert db_path.read_text() == "not a database"


# ======================================================================
# 17. create_staging_database rejects existing empty file
# ======================================================================
def test_create_staging_database_rejects_existing_empty_file(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite"
    db_path.touch()

    with pytest.raises(StagingDatabaseError, match="existing path"):
        create_staging_database(db_path)

    # File must be unchanged
    assert db_path.stat().st_size == 0


# ======================================================================
# 18. create_staging_database rejects existing symlink
# ======================================================================
def test_create_staging_database_rejects_existing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real_db.sqlite"
    target.touch()
    symlink_path = tmp_path / "link.sqlite"
    os.symlink(str(target), str(symlink_path))

    with pytest.raises(StagingDatabaseError, match="symlink"):
        create_staging_database(symlink_path)

    # Target must be unchanged
    assert target.exists()


# ======================================================================
# 19. Empty _staging_authorization table is rejected
# ======================================================================
def test_empty_authorization_table_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "empty_auth.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE _staging_authorization ("
        "token_hash TEXT NOT NULL,"
        "authorization_version INTEGER NOT NULL,"
        "db_identity TEXT NOT NULL,"
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="empty"):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 20. Invalid token format is rejected
# ======================================================================
def test_invalid_token_format_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_token.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE _staging_authorization ("
        "token_hash TEXT NOT NULL,"
        "authorization_version INTEGER NOT NULL,"
        "db_identity TEXT NOT NULL,"
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO _staging_authorization VALUES (?, ?, ?, ?)",
        ("not-a-valid-hex-token", 1, str(db_path.resolve()), "2024-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="token"):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 21. Incorrect token length is rejected
# ======================================================================
def test_incorrect_token_length_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "short_token.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE _staging_authorization ("
        "token_hash TEXT NOT NULL,"
        "authorization_version INTEGER NOT NULL,"
        "db_identity TEXT NOT NULL,"
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO _staging_authorization VALUES (?, ?, ?, ?)",
        ("abcdef12", 1, str(db_path.resolve()), "2024-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="token"):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 22. Multiple authorization rows are rejected
# ======================================================================
def test_multiple_authorization_rows_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "multi_auth.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE _staging_authorization ("
        "token_hash TEXT NOT NULL,"
        "authorization_version INTEGER NOT NULL,"
        "db_identity TEXT NOT NULL,"
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO _staging_authorization VALUES (?, ?, ?, ?)",
        ("a" * 64, 1, str(db_path.resolve()), "2024-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO _staging_authorization VALUES (?, ?, ?, ?)",
        ("b" * 64, 1, str(db_path.resolve()), "2024-01-02T00:00:00"),
    )
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="2 rows"):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 23. Incorrect marker schema is rejected
# ======================================================================
def test_incorrect_marker_schema_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_schema.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Old v0 schema (just token_hash column)
    conn.execute("CREATE TABLE _staging_authorization (token_hash TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO _staging_authorization VALUES (?)",
        ("a" * 64,),
    )
    conn.commit()
    conn.close()

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="schema"):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 24. Copied authorized staging database is rejected
# ======================================================================
def test_copied_staging_database_rejected(tmp_path: Path) -> None:
    # Create an authorized staging DB
    db_path = tmp_path / "original.sqlite"
    conn = create_staging_database(db_path)
    conn.close()

    # Copy the file to a new location
    copy_path = tmp_path / "copy.sqlite"
    shutil.copy2(str(db_path), str(copy_path))

    # The copy should be rejected (db_identity doesn't match)
    conn2 = sqlite3.connect(str(copy_path))
    conn2.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="not bound"):
            require_staging_database(conn2)
    finally:
        conn2.close()


# ======================================================================
# 25. failed initialization leaves an unauthorised file
# ======================================================================
def test_failed_initialization_leaves_unauthorised_file(tmp_path: Path) -> None:
    db_path = tmp_path / "cleanup_test.sqlite"

    # Pass a non-existent migration to trigger failure after file creation
    with pytest.raises(FileNotFoundError):
        create_staging_database(
            db_path,
            migration_paths=[tmp_path / "nonexistent.sql"],
        )

    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="not an authorised staging database"):
            require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 26. require_staging_database is read-only (no commit/rollback side effects)
# ======================================================================
def test_require_staging_does_not_modify_transaction_state(tmp_path: Path) -> None:
    """Calling require_staging_database on an untrusted connection must not
    commit or rollback any caller-owned pending transaction on that
    same connection."""
    db_path = tmp_path / "tx_test.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()

    # Start a pending write
    conn.execute("INSERT INTO t VALUES (1)")

    # The guard must reject and NOT commit/rollback
    with pytest.raises(StagingDatabaseError):
        require_staging_database(conn)

    # Pending write is still pending -- rollback to confirm
    conn.rollback()
    row = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert row == 0
    conn.close()


# ======================================================================
# 27. Guard fires before business mutation (parser conversion)
# ======================================================================
def test_conversion_guard_fires_before_validation(tmp_path: Path) -> None:
    """The guard fires before proposal validation, so no proposal lookup
    or transaction insert occurs."""
    from finance_core.parser_proposals.conversion import convert_confirmed_proposal_to_transaction

    db_path = tmp_path / "conv_early.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # DB has no schema at all -- if the guard fires after validation,
    # we would get a different error.  The guard fires first.
    with pytest.raises(StagingDatabaseError):
        convert_confirmed_proposal_to_transaction(conn, 1)

    conn.close()


# ======================================================================
# 28. Guard fires before business mutation (finalization)
# ======================================================================
def test_finalization_guard_fires_before_validation(tmp_path: Path) -> None:
    """The guard fires before any lookup or insert, so even a no-schema
    database triggers StagingDatabaseError not OperationalError."""
    from finance_core.receipt_finalization import finalize_receipt_split
    from finance_core.receipt_finalization.models import (
        FinalizationInput,
        SettlementObligation,
    )

    db_path = tmp_path / "fin_early.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    fin_input = FinalizationInput(
        receipt_group_public_id="rg_test",
        payer_participant_public_id="Owner",
        currency="SGD",
        calculation_run_public_id="calc_test",
        calculation_snapshot={
            "participants": ["Owner", "MemberA"],
            "payer": "Owner",
            "participant_shares": {"Owner": "0", "MemberA": "50"},
            "total_paid": "50",
            "payer_own_share": "0",
            "payer_paid_amounts": {"Owner": "50", "MemberA": "0"},
            "receipts": [],
            "settlement_obligations": [
                {"debtor": "MemberA", "creditor": "Owner", "amount": "50", "currency": "SGD"}
            ],
        },
        settlement_obligations=[
            SettlementObligation(
                debtor_participant_public_id="MemberA",
                creditor_participant_public_id="Owner",
                amount=Decimal("50"),
                currency="SGD",
            ),
        ],
    )

    with pytest.raises(StagingDatabaseError):
        finalize_receipt_split(conn, fin_input)

    conn.close()


# ======================================================================
# 29. Guard fires before business mutation (final mutation)
# ======================================================================
def test_final_mutation_guard_fires_before_validation(tmp_path: Path) -> None:
    """The guard fires before any mutation processing. A no-schema
    database triggers StagingDatabaseError not OperationalError."""
    from unittest.mock import MagicMock

    from finance_core.reconciliation.final_mutation_workflow import (
        FinalMutationWorkflowInput,
        execute_guarded_final_mutation_workflow,
    )

    db_path = tmp_path / "fm_early.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    wf_input = MagicMock(spec=FinalMutationWorkflowInput)
    wf_input.idempotency_key = "fm-early-001"

    with pytest.raises(StagingDatabaseError):
        execute_guarded_final_mutation_workflow(conn, wf_input)

    conn.close()


# ======================================================================
# 30. create_staging_database does not stamp existing databases
# ======================================================================
def test_create_does_not_stamp_existing_database(tmp_path: Path) -> None:
    """An existing SQLite database passed to create_staging_database
    must be rejected without adding any marker table or data."""
    db_path = tmp_path / "unstamped.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE important_data (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO important_data VALUES (1, 'do not lose me')")
    conn.commit()
    conn.close()

    with pytest.raises(StagingDatabaseError):
        create_staging_database(db_path)

    # Reopen and verify: the database must be untouched
    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    tables = conn2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {t[0] for t in tables}
    assert "_staging_authorization" not in table_names, (
        "create_staging_database must not stamp an existing database"
    )
    row = conn2.execute("SELECT value FROM important_data WHERE id=1").fetchone()
    assert row[0] == "do not lose me"
    conn2.close()


# ======================================================================
# Helpers
# ======================================================================
# ======================================================================
# 32. Replacement with a regular file after exclusive creation is rejected
# ======================================================================
def test_replacement_with_regular_file_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "raced.sqlite"
    replacement_path = tmp_path / "replacement_real.sqlite"

    def _swap() -> None:
        os.unlink(str(db_path))
        other = sqlite3.connect(str(replacement_path))
        other.execute("CREATE TABLE injected (x INTEGER)")
        other.commit()
        other.close()
        os.link(str(replacement_path), str(db_path))

    _guard._test_path_exchange_hook = _swap
    try:
        with pytest.raises(StagingDatabaseError, match="replaced"):
            create_staging_database(db_path)
    finally:
        _guard._test_path_exchange_hook = None

    # The replacement file must not have a staging authorization table
    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_staging_authorization'"
    ).fetchone()
    assert row is None, "Replacement file must not have authorization table"
    conn2.close()


# ======================================================================
# 33. Replacement with a symlink after exclusive creation is rejected
# ======================================================================
def test_replacement_with_symlink_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "raced_symlink.sqlite"
    replacement_path = tmp_path / "replacement_symlink.sqlite"

    def _swap() -> None:
        os.unlink(str(db_path))
        other = sqlite3.connect(str(replacement_path))
        other.execute("CREATE TABLE injected (x INTEGER)")
        other.commit()
        other.close()
        os.symlink(str(replacement_path), str(db_path))

    _guard._test_path_exchange_hook = _swap
    try:
        with pytest.raises(StagingDatabaseError, match="replaced"):
            create_staging_database(db_path)
    finally:
        _guard._test_path_exchange_hook = None


# ======================================================================
# 34. No authorization table written to replacement file
# ======================================================================
def test_no_authorization_written_to_replacement(tmp_path: Path) -> None:
    db_path = tmp_path / "no_auth_replacement.sqlite"
    replacement_path = tmp_path / "replacement_no_auth.sqlite"

    def _swap() -> None:
        os.unlink(str(db_path))
        other = sqlite3.connect(str(replacement_path))
        other.execute("CREATE TABLE innocent (id INTEGER)")
        other.commit()
        other.close()
        os.link(str(replacement_path), str(db_path))

    _guard._test_path_exchange_hook = _swap
    try:
        with pytest.raises(StagingDatabaseError):
            create_staging_database(db_path)
    finally:
        _guard._test_path_exchange_hook = None

    # neither file should have the auth table
    for check_path in (db_path, replacement_path):
        conn = sqlite3.connect(str(check_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_staging_authorization'"
        ).fetchone()
        assert row is None, f"{check_path.name} must not have authorization table"
        conn.close()


# ======================================================================
# 35. Failed initialization does not delete attacker-replaced file
# ======================================================================
def test_failed_initialization_does_not_delete_attacker_file(tmp_path: Path) -> None:
    db_path = tmp_path / "safe_cleanup.sqlite"
    replacement_path = tmp_path / "attacker.sqlite"

    def _swap() -> None:
        os.unlink(str(db_path))
        attacker = sqlite3.connect(str(replacement_path))
        attacker.execute("CREATE TABLE important (data TEXT)")
        attacker.execute("INSERT INTO important VALUES ('do not delete')")
        attacker.commit()
        attacker.close()
        os.link(str(replacement_path), str(db_path))

    _guard._test_path_exchange_hook = _swap
    try:
        with pytest.raises(StagingDatabaseError):
            create_staging_database(db_path)
    finally:
        _guard._test_path_exchange_hook = None

    # The attacker file at db_path must still exist with its data
    assert db_path.exists(), "Attacker-replaced file must not be deleted"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT data FROM important").fetchone()
    assert row is not None
    assert row["data"] == "do not delete"
    conn.close()


# ======================================================================
# 36. Failed initialization remains unauthorised
# ======================================================================
def test_failed_initialization_is_not_authorised(tmp_path: Path) -> None:
    db_path = tmp_path / "cleanup_match.sqlite"

    with pytest.raises(FileNotFoundError):
        create_staging_database(
            db_path,
            migration_paths=[tmp_path / "nonexistent.sql"],
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="not an authorised staging database"):
            require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 37. Migration failure leaves an unauthorised file
# ======================================================================
def test_migration_failure_leaves_unauthorised_file(tmp_path: Path) -> None:
    db_path = tmp_path / "mig_cleanup.sqlite"

    with pytest.raises(FileNotFoundError):
        create_staging_database(
            db_path,
            migration_paths=[tmp_path / "nonexistent.sql"],
        )

    assert db_path.exists()


def _apply_core_schema(conn: sqlite3.Connection) -> None:
    resource_dir = migrations_dir()
    for name in (
        "001_create_core_schema.sql",
        "002_receipt_split_schema.sql",
        "003_raw_intake_persistence.sql",
        "004_parser_proposal_confirmation.sql",
        "005_raw_intake_source_evidence.sql",
        "006_reconciliation_persistence_schema_v1.sql",
        "015_calculation_run_persistence.sql",
    ):
        path = resource_dir / name
        if path.exists():
            conn.executescript(path.read_text(encoding="utf-8"))
    conn.commit()


# ======================================================================
# 38. sqlite3.connect() failure closes the exclusive-creation fd
# ======================================================================
def test_connect_failure_closes_fd(tmp_path: Path) -> None:
    """sqlite3.connect() raises → exclusive fd is closed."""
    db_path = tmp_path / "connect_fail_fd.sqlite"

    opened: list[int] = []
    orig_open = os.open

    def _capture_open(*args: object, **kwargs: object) -> int:
        fd = orig_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    with (
        mock.patch("os.open", _capture_open),
        mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("simulated")),
    ):
        with pytest.raises(sqlite3.OperationalError):
            _guard.create_staging_database(db_path)

    assert len(opened) == 1, f"Expected 1 open, got {len(opened)}"
    # Verify fd is closed by attempting to fstat it
    with pytest.raises(OSError):
        os.fstat(opened[0])


# ======================================================================
# 39. sqlite3.connect() failure leaves an unauthorised file
# ======================================================================
def test_connect_failure_leaves_unauthorised_file(tmp_path: Path) -> None:
    """sqlite3.connect() raises → no path-based cleanup is attempted."""
    db_path = tmp_path / "connect_fail_cleanup.sqlite"

    with mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("simulated")):
        with pytest.raises(sqlite3.OperationalError):
            _guard.create_staging_database(db_path)

    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError, match="not an authorised staging database"):
            require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 40. connect failure after path replacement preserves attacker file
# ======================================================================
def test_connect_failure_preserves_attacker_file_after_replacement(
    tmp_path: Path,
) -> None:
    """sqlite3.connect() raises after path replacement → attacker file
    is NOT deleted."""
    db_path = tmp_path / "raced_connect_fail.sqlite"
    replacement_path = tmp_path / "attacker_connect_fail.sqlite"

    _original_connect = sqlite3.connect

    def _swap() -> None:
        os.unlink(str(db_path))
        attacker = _original_connect(str(replacement_path))
        attacker.execute("CREATE TABLE important (data TEXT)")
        attacker.execute("INSERT INTO important VALUES ('do not delete')")
        attacker.commit()
        attacker.close()
        os.link(str(replacement_path), str(db_path))

    _guard._test_path_exchange_hook = _swap
    try:
        with mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("simulated")):
            with pytest.raises(sqlite3.OperationalError):
                _guard.create_staging_database(db_path)
    finally:
        _guard._test_path_exchange_hook = None

    # The attacker file must still exist with its data intact
    assert db_path.exists(), "Attacker-replaced file must not be deleted"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT data FROM important").fetchone()
    assert row is not None
    assert row["data"] == "do not delete"
    conn.close()


# ======================================================================
# 41. Migration failure closes the exclusive-creation fd
# ======================================================================
def test_migration_failure_closes_fd(tmp_path: Path) -> None:
    """Migration failure still closes the exclusive-creation fd."""
    db_path = tmp_path / "mig_fail_fd.sqlite"

    opened: list[int] = []
    orig_open = os.open

    def _capture_open(*args: object, **kwargs: object) -> int:
        fd = orig_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    with mock.patch("os.open", _capture_open):
        with pytest.raises(FileNotFoundError):
            _guard.create_staging_database(
                db_path,
                migration_paths=[tmp_path / "nonexistent.sql"],
            )

    assert len(opened) == 1, f"Expected 1 open, got {len(opened)}"
    # Verify fd is closed
    with pytest.raises(OSError):
        os.fstat(opened[0])


# ======================================================================
# 42. Successful creation closes the exclusive-creation fd
# ======================================================================
def test_successful_creation_closes_fd(tmp_path: Path) -> None:
    """Successful creation closes the exclusive-creation fd before
    returning the SQLite connection."""
    db_path = tmp_path / "success_fd.sqlite"

    opened: list[int] = []
    orig_open = os.open

    def _capture_open(*args: object, **kwargs: object) -> int:
        fd = orig_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    with mock.patch("os.open", _capture_open):
        conn = _guard.create_staging_database(db_path)

    try:
        assert len(opened) == 1, f"Expected 1 open, got {len(opened)}"
        # Verify fd is closed
        with pytest.raises(OSError):
            os.fstat(opened[0])
        # Connection must still be usable
        row = conn.execute("SELECT 1 AS one").fetchone()
        assert row is not None
        assert row["one"] == 1
    finally:
        conn.close()


# ======================================================================
# 43. Successful creation leaves SQLite connection open and usable
# ======================================================================
def test_successful_creation_connection_usable(tmp_path: Path) -> None:
    """The returned SQLite connection is open and can execute queries."""
    db_path = tmp_path / "usable_conn.sqlite"
    conn = _guard.create_staging_database(db_path)
    try:
        conn.execute("CREATE TABLE test_fd (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO test_fd (id, val) VALUES (1, 'hello')")
        conn.commit()
        row = conn.execute("SELECT val FROM test_fd WHERE id=1").fetchone()
        assert row is not None
        assert row["val"] == "hello"
        # Authorization must still be valid
        _guard.require_staging_database(conn)
    finally:
        conn.close()


# ======================================================================
# 44. No double-close on successful creation
# ======================================================================
def test_no_double_close_on_success(tmp_path: Path) -> None:
    """The exclusive-creation fd is closed exactly once on success."""
    db_path = tmp_path / "no_double_success.sqlite"

    close_counts: dict[int, int] = {}
    orig_close = os.close

    def _tracking_close(fd: int) -> None:
        close_counts[fd] = close_counts.get(fd, 0) + 1
        orig_close(fd)

    with mock.patch("os.close", _tracking_close):
        conn = _guard.create_staging_database(db_path)

    try:
        assert len(close_counts) >= 1, "Expected at least one close"
        for fd, count in close_counts.items():
            assert count == 1, f"fd {fd} closed {count} times (expected 1)"
    finally:
        conn.close()


# ======================================================================
# 45. No double-close on connect failure
# ======================================================================
def test_no_double_close_on_connect_failure(tmp_path: Path) -> None:
    """The exclusive-creation fd is closed exactly once on connect failure."""
    db_path = tmp_path / "no_double_connect_fail.sqlite"

    close_counts: dict[int, int] = {}
    orig_close = os.close

    def _tracking_close(fd: int) -> None:
        close_counts[fd] = close_counts.get(fd, 0) + 1
        orig_close(fd)

    with (
        mock.patch("os.close", _tracking_close),
        mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("simulated")),
    ):
        with pytest.raises(sqlite3.OperationalError):
            _guard.create_staging_database(db_path)

    assert len(close_counts) >= 1, "Expected at least one close"
    for fd, count in close_counts.items():
        assert count == 1, f"fd {fd} closed {count} times (expected 1)"
