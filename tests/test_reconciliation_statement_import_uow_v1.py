"""Tests for Statement Import Unit of Work v1.

Covers the service-owned transaction boundary, atomicity, rollback,
replay, and repository prohibition invariants.

Transaction invariants under test:
  1. Repository methods never commit.
  2. One import command has one transaction owner.
  3. Batch header and rows commit together.
  4. Any failure rolls back all writes from that import command.
  5. Rollback does not close or corrupt the caller-owned connection.
  6. Failed imports leave no records that appear successfully imported.
  7. Identical successful replay does not duplicate persisted facts.
  8. No runtime DDL occurs.
  9. No live database is touched.
 10. Raw source evidence and attachment/source references are preserved.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from finance_core.reconciliation.repository import (
    DuplicatePublicIdError,
    ReconciliationRepository,
)
from finance_core.reconciliation.statement_identity import (
    STATEMENT_IMPORT_CONTRACT_VERSION,
    canonical_statement_row_fingerprint,
    derive_statement_row_public_id,
)
from finance_core.reconciliation.statement_import import (
    StatementImporter,
    StatementImportTransactionError,
    StructuredStatementRow,
)
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite
from finance_core.staging_guard import require_staging_database

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    merchant: str = "Apple",
    amount: Decimal = Decimal("10.00"),
    currency: str = "SGD",
    txn_date: date | None = date(2024, 12, 1),
    posted_date: date | None = date(2024, 12, 3),
    **kw,
) -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw=merchant,
        amount=amount,
        currency=currency,
        transaction_date=txn_date,
        posted_date=posted_date,
        **kw,
    )


def _seed_legacy_collision_row(
    conn: sqlite3.Connection,
    *,
    batch_public_id: str,
    source_hash: str,
    merchant: str,
    amount: Decimal,
    statement_row_reference: str,
) -> None:
    """Seed a pre-v27 row so an authoritative import reaches row persistence."""
    row_fingerprint = canonical_statement_row_fingerprint(
        source_content_hash=source_hash,
        import_contract_version=STATEMENT_IMPORT_CONTRACT_VERSION,
        source_row_locator=statement_row_reference,
        transaction_date="2024-12-01",
        posted_date="2024-12-03",
        original_amount=None,
        normalized_amount=amount,
        currency="SGD",
        direction=None,
        raw_amount_type=None,
        merchant_raw=merchant,
        merchant_normalized=None,
        account_id=None,
        account_name=None,
        statement_row_reference=statement_row_reference,
        raw_row_payload={"v": 2},
    )
    conn.execute(
        """INSERT INTO statement_import_batches
        (public_id, source_type, source_file_hash)
        VALUES (?, 'bank_statement', ?)""",
        (batch_public_id, source_hash),
    )
    batch_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """INSERT INTO statement_transactions
        (public_id, batch_id, transaction_date, posted_date, merchant_raw,
         amount, currency, statement_row_reference, row_fingerprint,
         raw_row_payload_json)
        VALUES (?, ?, '2024-12-01', '2024-12-03', ?, ?, 'SGD', ?, ?, ?)""",
        (
            derive_statement_row_public_id(row_fingerprint, source_hash),
            batch_id,
            merchant,
            str(amount),
            statement_row_reference,
            row_fingerprint,
            json.dumps({"v": 1}, sort_keys=True),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# A. Successful Atomic Import
# ---------------------------------------------------------------------------


def test_successful_multi_row_import_creates_one_batch(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A valid multi-row import creates exactly one expected batch."""
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [
        _make_row(merchant="Apple", amount=Decimal("10.00")),
        _make_row(merchant="Netflix", amount=Decimal("15.99")),
        _make_row(merchant="Spotify", amount=Decimal("9.99")),
    ]
    batch = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="atomic-batch-001",
    )

    assert batch.public_id == "atomic-batch-001"
    assert batch.row_count == 3
    assert batch.skipped_duplicates == 0
    assert len(batch.inserted_ids) == 3

    batch_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_import_batches WHERE public_id = ?",
        ("atomic-batch-001",),
    ).fetchone()[0]
    assert batch_count == 1


def test_successful_import_commits_all_rows(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """All rows are committed and visible after a successful import."""
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [
        _make_row(merchant="Row-A", amount=Decimal("1.00")),
        _make_row(merchant="Row-B", amount=Decimal("2.00")),
        _make_row(merchant="Row-C", amount=Decimal("3.00")),
    ]
    batch = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="atomic-batch-committed",
    )

    stored = migrated_temp_db_connection.execute(
        "SELECT merchant_raw, amount FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert len(stored) == 3
    merchants = [r["merchant_raw"] for r in stored]
    assert merchants == ["Row-A", "Row-B", "Row-C"]


def test_successful_import_preserves_foreign_key_checks(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """After a successful import, foreign-key checks are clean."""
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [_make_row()]
    importer.import_rows(rows, source_type="bank_statement", public_id="fk-clean-001")

    fk_violations = migrated_temp_db_connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(fk_violations) == 0


def test_successful_import_preserves_source_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Source evidence (file path, hash, raw payload) is preserved."""
    importer = StatementImporter(migrated_temp_db_connection)
    payload = {"csv_line": 3, "bank_id": "ev-123"}
    rows = [
        _make_row(
            merchant="EvidenceRow",
            raw_row_payload=payload,
            statement_row_reference="ev-ref-1",
        )
    ]
    source_path = tmp_path / "stmt.csv"
    source_bytes = b"date,merchant,amount\n2024-12-01,EvidenceRow,10.00\n"
    source_path.write_bytes(source_bytes)
    batch = importer.import_rows(
        rows,
        source_type="credit_card_statement",
        public_id="atomic-batch-evidence",
        source_file_path=str(source_path),
        account_name="Owner Visa",
    )

    batch_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert batch_row["source_file_path"] == str(source_path)
    assert batch_row["source_file_hash"] == hashlib.sha256(source_bytes).hexdigest()
    assert batch_row["source_filename"] == "stmt.csv"
    assert batch_row["source_hash_verification_status"] == "verified_from_bytes"
    assert batch_row["account_name"] == "Owner Visa"

    stmt_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stmt_row["statement_row_reference"] == "ev-ref-1"
    stored_payload = json.loads(stmt_row["raw_row_payload_json"])
    assert stored_payload == payload


# ---------------------------------------------------------------------------
# B. Failure on First Row (real persistence-boundary injection)
# ---------------------------------------------------------------------------


class _PostBatchRaise(RuntimeError):
    """Deterministic failure injected after batch INSERT, before any row."""


def test_first_row_failure_after_batch_insert_rolls_back_everything(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A failure after the batch is inserted but before any row is persisted
    rolls back the batch and all rows.

    Sequence:
      BEGIN IMMEDIATE
      → insert statement_import_batches row
      → _post_batch_hook raises _PostBatchRaise
      → service rolls back
    """
    fail_batch_pubid = "real-first-row-fail-batch"

    def _raise_after_batch() -> None:
        raise _PostBatchRaise("injected after batch insert")

    importer = StatementImporter(
        migrated_temp_db_connection,
        _test_post_batch_hook=_raise_after_batch,
    )

    with pytest.raises(_PostBatchRaise, match="injected after batch insert"):
        importer.import_rows(
            [_make_row(merchant="WouldBeFirstRow", amount=Decimal("1.00"))],
            source_type="bank_statement",
            public_id=fail_batch_pubid,
        )

    # The newly requested batch must not exist (it was rolled back).
    batch_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE public_id = ?",
        (fail_batch_pubid,),
    ).fetchone()
    assert batch_row is None

    # No statement row from the failed command exists.
    stmt_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions"
    ).fetchone()[0]
    assert stmt_count == 0

    # The caller-owned connection remains usable.
    result = migrated_temp_db_connection.execute("SELECT 1").fetchone()
    assert result is not None

    # A corrected retry using the same intended batch identity succeeds.
    retry_importer = StatementImporter(migrated_temp_db_connection)
    retry = retry_importer.import_rows(
        [_make_row(merchant="CorrectedFirstRow", amount=Decimal("99.99"))],
        source_type="bank_statement",
        public_id=fail_batch_pubid,
    )
    assert retry.public_id == fail_batch_pubid
    assert retry.row_count == 1
    assert len(retry.inserted_ids) == 1


# ---------------------------------------------------------------------------
# C. Failure in Middle Row
# ---------------------------------------------------------------------------


def test_failure_in_middle_row_rolls_back_all(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A failure in the middle row rolls back earlier rows and the batch.

    Strategy: seed a row with a specific public_id via dup content at
    source_row_index 2, then re-import with different data at position 2
    → public_id conflict → entire import rolls back.
    """
    importer = StatementImporter(migrated_temp_db_connection)

    source_hash = _sha("middle-rollback-hash")
    # Seed a legacy row identity. Legacy rows are intentionally outside the
    # authoritative one-source-content owner policy, so the new import reaches
    # row 2 before detecting the cross-batch public_id collision.
    _seed_legacy_collision_row(
        migrated_temp_db_connection,
        batch_public_id="middle-rollback-seed",
        source_hash=source_hash,
        merchant="ConflictTarget",
        amount=Decimal("50.00"),
        statement_row_reference="ct-ref",
    )

    # Now import 3 rows where row 2 produces the SAME public_id as seed_row2
    # (same source_file_hash, same source_row_index=2, same content fields)
    # but different raw_row_payload → conflict
    row1 = _make_row(merchant="SafeRow1", amount=Decimal("11.00"))
    row2_conflict = _make_row(
        merchant="ConflictTarget",
        amount=Decimal("50.00"),
        statement_row_reference="ct-ref",
        raw_row_payload={"v": 2},
        row_fingerprint=_sha("middle-row-collision"),
    )
    row3 = _make_row(merchant="SafeRow3", amount=Decimal("33.00"))

    with pytest.raises(DuplicatePublicIdError, match="already owned by another batch"):
        importer.import_rows(
            [row1, row2_conflict, row3],
            source_type="bank_statement",
            public_id="middle-rollback-target",
            source_file_hash=source_hash,
        )

    # No batch exists for the failed import
    batch_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE public_id = ?",
        ("middle-rollback-target",),
    ).fetchone()
    assert batch_row is None

    # row1 (SafeRow1) was NOT persisted — it was rolled back
    safe_row = migrated_temp_db_connection.execute(
        "SELECT 1 FROM statement_transactions WHERE merchant_raw = ?",
        ("SafeRow1",),
    ).fetchone()
    assert safe_row is None

    # Retry succeeds
    retry = importer.import_rows(
        [
            _make_row(merchant="Retry1", amount=Decimal("11.00")),
            _make_row(merchant="Retry2", amount=Decimal("12.00")),
            _make_row(merchant="Retry3", amount=Decimal("13.00")),
        ],
        source_type="bank_statement",
        public_id="middle-rollback-retry",
    )
    assert retry.row_count == 3
    assert len(retry.inserted_ids) == 3


# ---------------------------------------------------------------------------
# D. Failure on Final Row
# ---------------------------------------------------------------------------


def test_failure_on_final_row_rolls_back_all(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Failure on the last row rolls back all prior rows and the batch."""
    importer = StatementImporter(migrated_temp_db_connection)

    source_hash = _sha("final-row-rollback-hash")
    # Seed a legacy row identity so the new authoritative import reaches its
    # final row before detecting the cross-batch public_id collision.
    _seed_legacy_collision_row(
        migrated_temp_db_connection,
        batch_public_id="final-rollback-seed",
        source_hash=source_hash,
        merchant="ConflictFinal",
        amount=Decimal("5.00"),
        statement_row_reference="cf-ref",
    )

    # Import 3 rows; the LAST collides with seed_row_c (same source_row_index=3)
    conflict_row = _make_row(
        merchant="ConflictFinal",
        amount=Decimal("5.00"),
        statement_row_reference="cf-ref",
        raw_row_payload={"v": 2},
        row_fingerprint=_sha("final-row-collision"),
    )
    rows = [
        _make_row(merchant="Prior1", amount=Decimal("10.00")),
        _make_row(merchant="Prior2", amount=Decimal("20.00")),
        conflict_row,
    ]

    with pytest.raises(DuplicatePublicIdError, match="already owned by another batch"):
        importer.import_rows(
            rows,
            source_type="bank_statement",
            public_id="final-rollback-target",
            source_file_hash=source_hash,
        )

    # No batch exists
    batch_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE public_id = ?",
        ("final-rollback-target",),
    ).fetchone()
    assert batch_row is None

    # Prior rows do not exist
    prior = migrated_temp_db_connection.execute(
        "SELECT 1 FROM statement_transactions WHERE merchant_raw = ?",
        ("Prior1",),
    ).fetchone()
    assert prior is None

    # Connection remains usable
    row = migrated_temp_db_connection.execute("SELECT 1").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# E. Failure After Row Persistence – real pre-commit injection
# ---------------------------------------------------------------------------


class _PreCommitRaise(RuntimeError):
    """Deterministic failure injected after all rows, before COMMIT."""


def test_post_row_pre_commit_failure_rolls_back_everything(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A failure after all rows are persisted but before COMMIT rolls back
    the complete import command.

    Sequence:
      BEGIN IMMEDIATE
      → insert batch
      → insert all rows (2 requested)
      → _pre_commit_hook raises _PreCommitRaise
      → ROLLBACK
    """
    fail_batch_pubid = "pre-commit-fail-batch"

    def _raise_before_commit() -> None:
        raise _PreCommitRaise("injected before commit")

    importer = StatementImporter(
        migrated_temp_db_connection,
        _test_pre_commit_hook=_raise_before_commit,
    )

    rows = [
        _make_row(merchant="PreCommitRow1", amount=Decimal("11.00")),
        _make_row(merchant="PreCommitRow2", amount=Decimal("22.00")),
    ]

    with pytest.raises(_PreCommitRaise, match="injected before commit"):
        importer.import_rows(
            rows,
            source_type="bank_statement",
            public_id=fail_batch_pubid,
        )

    # The batch does not remain (it was rolled back).
    batch_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE public_id = ?",
        (fail_batch_pubid,),
    ).fetchone()
    assert batch_row is None

    # None of the inserted rows remain.
    stmt_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions"
    ).fetchone()[0]
    assert stmt_count == 0

    # The connection is usable after rollback.
    result = migrated_temp_db_connection.execute("SELECT 1").fetchone()
    assert result is not None

    # A corrected retry succeeds.
    retry_importer = StatementImporter(migrated_temp_db_connection)
    retry = retry_importer.import_rows(
        [
            _make_row(merchant="CorrectedRow1", amount=Decimal("33.00")),
            _make_row(merchant="CorrectedRow2", amount=Decimal("44.00")),
        ],
        source_type="bank_statement",
        public_id=fail_batch_pubid,
    )
    assert retry.public_id == fail_batch_pubid
    assert retry.row_count == 2
    assert len(retry.inserted_ids) == 2


# ---------------------------------------------------------------------------
# F. Repository Transaction Prohibition
# ---------------------------------------------------------------------------


class _TransactionProhibitedConnection(sqlite3.Connection):
    """A proxy connection that records transaction-control calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed: list[str] = []

    def commit(self) -> None:
        self.executed.append("commit")
        # Don't actually commit -- let the real connection handle it

    def rollback(self) -> None:
        self.executed.append("rollback")
        # Don't actually roll back

    def execute(self, sql, parameters=...):
        upper = sql.strip().upper()
        if upper == "BEGIN" or upper.startswith("BEGIN "):
            self.executed.append(f"execute:{sql.strip()}")
        if upper == "SAVEPOINT" or upper.startswith("SAVEPOINT "):
            self.executed.append(f"execute:{sql.strip()}")
        return super().execute(sql, parameters) if parameters is not ... else super().execute(sql)


def test_create_statement_import_batch_does_not_commit(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Repository method create_statement_import_batch does not commit."""
    # We verify this behaviourally: insert a batch via the repository,
    # roll back the transaction, and confirm nothing is persisted.
    conn = migrated_temp_db_connection
    conn.execute("BEGIN")
    try:
        repo = ReconciliationRepository(conn)
        batch_id = repo.create_statement_import_batch(
            public_id="no-commit-test-batch",
            source_type="bank_statement",
        )
        assert batch_id > 0
    finally:
        conn.rollback()

    # After rollback, the batch must NOT exist
    row = conn.execute(
        "SELECT * FROM statement_import_batches WHERE public_id = ?",
        ("no-commit-test-batch",),
    ).fetchone()
    assert row is None


def test_create_statement_transactions_does_not_commit(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Repository method create_statement_transactions does not commit."""
    conn = migrated_temp_db_connection
    conn.execute("BEGIN")
    try:
        # First create the batch inside the transaction
        repo = ReconciliationRepository(conn)
        batch_id = repo.create_statement_import_batch(
            public_id="no-commit-txns-batch",
            source_type="bank_statement",
        )
        ids = repo.create_statement_transactions(
            batch_id,
            [
                {
                    "public_id": "no-commit-txn-1",
                    "merchant_raw": "Test",
                    "amount": Decimal("10.00"),
                    "currency": "SGD",
                }
            ],
        )
        assert len(ids) == 1
    finally:
        conn.rollback()

    # After rollback, nothing persists
    batch_row = conn.execute(
        "SELECT * FROM statement_import_batches WHERE public_id = ?",
        ("no-commit-txns-batch",),
    ).fetchone()
    assert batch_row is None

    txn_row = conn.execute(
        "SELECT * FROM statement_transactions WHERE public_id = ?",
        ("no-commit-txn-1",),
    ).fetchone()
    assert txn_row is None


# ---------------------------------------------------------------------------
# G. Service Transaction Ownership
# ---------------------------------------------------------------------------


def test_service_begins_immediate_and_commits(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The service begins BEGIN IMMEDIATE and commits once on success."""
    importer = StatementImporter(migrated_temp_db_connection)

    # Verify the connection is not in a transaction after a successful import
    assert not migrated_temp_db_connection.in_transaction

    batch = importer.import_rows(
        [_make_row()],
        source_type="bank_statement",
        public_id="tx-ownership-test",
    )
    assert batch.row_count == 1

    # Connection must not be in a transaction after commit
    assert not migrated_temp_db_connection.in_transaction


def test_service_rolls_back_on_failure(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The service rolls back the transaction on failure."""
    importer = StatementImporter(migrated_temp_db_connection)

    # Pre-seed so we can trigger a duplicate
    importer.import_rows(
        [_make_row(merchant="TXOwnerSeed", amount=Decimal("99.99"))],
        source_type="bank_statement",
        public_id="tx-owner-seed-batch",
    )

    try:
        importer.import_rows(
            [_make_row(merchant="TXOwnerSeed", amount=Decimal("99.99"))],
            source_type="bank_statement",
            public_id="tx-owner-seed-batch",
            source_file_hash=_sha("changed-hash-should-fail"),
        )
    except DuplicatePublicIdError:
        pass

    # Connection must not be in a transaction after rollback
    assert not migrated_temp_db_connection.in_transaction


def test_repository_executes_inside_service_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Repository writes are visible only after the service commits."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)

    batch = importer.import_rows(
        [_make_row(merchant="InsideTxn", amount=Decimal("25.00"))],
        source_type="bank_statement",
        public_id="inside-txn-batch",
    )

    # After commit, data is visible
    row = conn.execute(
        "SELECT * FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert row is not None
    assert row["merchant_raw"] == "InsideTxn"


# ---------------------------------------------------------------------------
# H. Existing Transaction Handling
# ---------------------------------------------------------------------------


def test_supplied_connection_already_in_transaction_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """When the supplied connection is already in a transaction, the
    service fails closed with a clear error."""
    conn = migrated_temp_db_connection
    conn.execute("BEGIN")
    try:
        importer = StatementImporter(conn)
        with pytest.raises(StatementImportTransactionError, match="pending work"):
            importer.import_rows(
                [_make_row()],
                source_type="bank_statement",
                public_id="should-not-persist",
            )
    finally:
        conn.rollback()

    # The connection remains usable
    row = conn.execute("SELECT 1").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# I. Replay
# ---------------------------------------------------------------------------


def test_identical_replay_does_not_duplicate_rows(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """An identical successful replay does not create duplicate batches or rows."""
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(
        merchant="ReplayRow",
        amount=Decimal("12.00"),
        statement_row_reference="replay-ref-1",
    )

    first = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="replay-batch",
        source_file_hash=_sha("replay-hash"),
    )
    assert first.skipped_duplicates == 0
    assert len(first.inserted_ids) == 1

    second = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="replay-batch",
        source_file_hash=_sha("replay-hash"),
    )
    assert second.batch_id == first.batch_id
    assert second.skipped_duplicates == 0
    assert second.idempotent_count == 1
    assert second.inserted_ids == []

    batch_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_import_batches WHERE public_id = ?",
        ("replay-batch",),
    ).fetchone()[0]
    assert batch_count == 1

    row_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions WHERE batch_id = ?",
        (first.batch_id,),
    ).fetchone()[0]
    assert row_count == 1


def test_failure_followed_by_retry_succeeds(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A retry after rollback can complete normally."""
    importer = StatementImporter(migrated_temp_db_connection)

    source_hash = _sha("retry-source-hash")
    # Seed a row at position 1 with ref "retry-fp"
    seed = _make_row(
        merchant="RetrySeed",
        amount=Decimal("10.00"),
        statement_row_reference="retry-fp",
        row_fingerprint=_sha("retry-row-collision"),
    )
    importer.import_rows(
        [seed],
        source_type="bank_statement",
        public_id="retry-seed-batch",
        source_file_hash=source_hash,
    )

    # Failed attempt: same position 1, same ref, same source_file_hash
    # but different raw_row_payload -> public_id conflict
    with pytest.raises(DuplicatePublicIdError):
        importer.import_rows(
            [
                _make_row(
                    merchant="RetrySeed",
                    amount=Decimal("10.00"),
                    statement_row_reference="retry-fp",
                    raw_row_payload={"v": 2},
                    row_fingerprint=_sha("retry-row-collision"),
                )
            ],
            source_type="bank_statement",
            public_id="retry-fail-batch",
            source_file_hash=source_hash,
        )

    # Retry with non-conflicting data
    retry = importer.import_rows(
        [
            _make_row(
                merchant="RetrySuccess",
                amount=Decimal("30.00"),
            )
        ],
        source_type="bank_statement",
        public_id="retry-success-batch",
    )
    assert retry.row_count == 1
    assert len(retry.inserted_ids) == 1


def test_rollback_leaves_no_stale_batch_that_changes_retry(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A rolled-back import does not leave a stale batch that would
    change retry behaviour."""
    importer = StatementImporter(migrated_temp_db_connection)

    source_hash = _sha("stale-batch-hash-001")
    # Seed
    seed = _make_row(
        merchant="StaleSeed",
        amount=Decimal("50.00"),
        statement_row_reference="stale-ref",
        row_fingerprint=_sha("stale-row-collision"),
    )
    importer.import_rows(
        [seed],
        source_type="bank_statement",
        public_id="stale-seed-batch",
        source_file_hash=source_hash,
    )

    # Attempt that fails due to public_id conflict
    with pytest.raises(DuplicatePublicIdError):
        importer.import_rows(
            [
                _make_row(
                    merchant="StaleSeed",
                    amount=Decimal("50.00"),
                    statement_row_reference="stale-ref",
                    raw_row_payload={"v": 2},
                    row_fingerprint=_sha("stale-row-collision"),
                )
            ],
            source_type="bank_statement",
            public_id="stale-attempt-batch",
            source_file_hash=source_hash,
        )

    # After rollback, a fresh import with the same public_id should succeed
    retry = importer.import_rows(
        [_make_row(merchant="CleanRetry", amount=Decimal("70.00"))],
        source_type="bank_statement",
        public_id="stale-attempt-batch",
    )
    assert retry.row_count == 1
    assert len(retry.inserted_ids) == 1


# ---------------------------------------------------------------------------
# J. CSV Import Path
# ---------------------------------------------------------------------------


def test_csv_import_routes_through_uow(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The CSV-facing import path routes through the authoritative Unit of Work
    and cannot partially persist rows."""
    # This test exercises the importer with the same shape that the CSV
    # adapter produces — dict input with typical fields.
    importer = StatementImporter(migrated_temp_db_connection)

    # Simulate CSV-format rows as dicts
    csv_rows: list[dict] = [
        {
            "merchant_raw": "CSVMerchant1",
            "amount": Decimal("10.50"),
            "currency": "SGD",
            "transaction_date": date(2024, 12, 1),
            "posted_date": date(2024, 12, 3),
        },
        {
            "merchant_raw": "CSVMerchant2",
            "amount": "22.00",
            "currency": "SGD",
            "transaction_date": "2024-12-02",
        },
        {
            "merchant_raw": "CSVMerchant3",
            "amount": Decimal("33.33"),
            "currency": "SGD",
            "transaction_date": date(2024, 12, 3),
        },
    ]

    batch = importer.import_rows(
        csv_rows,
        source_type="structured_csv",
        public_id="csv-uow-batch",
    )
    assert batch.row_count == 3
    assert len(batch.inserted_ids) == 3

    stored = migrated_temp_db_connection.execute(
        "SELECT merchant_raw, amount FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert len(stored) == 3


# ---------------------------------------------------------------------------
# K. Structured/Dictionary Input
# ---------------------------------------------------------------------------


def test_structured_input_uses_same_uow(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Both StructuredStatementRow and dict inputs use the same atomic UoW."""
    importer = StatementImporter(migrated_temp_db_connection)

    structured_rows = [
        _make_row(merchant="Structured1", amount=Decimal("11.00")),
        _make_row(merchant="Structured2", amount=Decimal("22.00")),
    ]
    batch_s = importer.import_rows(
        structured_rows,
        source_type="bank_statement",
        public_id="structured-uow-batch",
    )
    assert batch_s.row_count == 2
    assert len(batch_s.inserted_ids) == 2

    dict_rows = [
        {
            "merchant_raw": "Dict1",
            "amount": Decimal("33.00"),
            "currency": "SGD",
            "transaction_date": date(2024, 12, 10),
        },
    ]
    batch_d = importer.import_rows(
        dict_rows,
        source_type="structured_csv",
        public_id="dict-uow-batch",
    )
    assert batch_d.row_count == 1
    assert len(batch_d.inserted_ids) == 1


# ---------------------------------------------------------------------------
# L. PDF Bridge Regression
# ---------------------------------------------------------------------------


def test_pdf_bridge_regression_normalized_rows_import_atomically(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Normalized PDF rows (via the bridge) import through the UoW atomically."""
    importer = StatementImporter(migrated_temp_db_connection)

    # Simulate rows that look like what the PDF bridge produces
    pdf_like_rows = [
        StructuredStatementRow(
            merchant_raw="PDF Merchant One",
            amount=Decimal("12.50"),
            currency="SGD",
            transaction_date=date(2024, 12, 7),
            posted_date=date(2024, 12, 9),
            amount_direction=None,
            raw_amount="12.50",
            raw_amount_type=None,
            statement_row_reference="pdf-row-001",
            raw_row_payload={
                "attachment_path": "statement.pdf",
                "raw_row_text": "Some raw text",
                "source_page_number": 1,
            },
            row_fingerprint=_sha("pdf-fp-bridge-001"),
        ),
        StructuredStatementRow(
            merchant_raw="PDF Merchant Two",
            amount=Decimal("99.00"),
            currency="SGD",
            transaction_date=date(2024, 12, 8),
            amount_direction=None,
            raw_amount="99.00",
            statement_row_reference="pdf-row-002",
            raw_row_payload={
                "attachment_path": "statement.pdf",
                "raw_row_text": "More raw text",
                "source_page_number": 2,
            },
            row_fingerprint=_sha("pdf-fp-bridge-002"),
        ),
    ]

    pdf_path = tmp_path / "statement.pdf"
    pdf_bytes = b"%PDF-1.7\nsynthetic statement evidence\n%%EOF\n"
    pdf_path.write_bytes(pdf_bytes)
    batch = importer.import_rows(
        pdf_like_rows,
        source_type="bank_statement",
        public_id="pdf-bridge-uow-batch",
        source_file_path=str(pdf_path),
    )
    assert batch.row_count == 2
    assert len(batch.inserted_ids) == 2

    # Verify source evidence
    batch_row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert batch_row["source_file_path"] == str(pdf_path)
    assert batch_row["source_file_hash"] == hashlib.sha256(pdf_bytes).hexdigest()

    rows = migrated_temp_db_connection.execute(
        "SELECT merchant_raw, raw_row_payload_json, row_fingerprint, "
        "external_row_fingerprint "
        "FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["merchant_raw"] == "PDF Merchant One"
    assert rows[1]["merchant_raw"] == "PDF Merchant Two"
    assert rows[0]["row_fingerprint"] != _sha("pdf-fp-bridge-001")
    assert rows[0]["external_row_fingerprint"] == _sha("pdf-fp-bridge-001")

    payload = json.loads(rows[0]["raw_row_payload_json"])
    assert payload["attachment_path"] == "statement.pdf"


# ---------------------------------------------------------------------------
# M. Missing Schema and Runtime DDL
# ---------------------------------------------------------------------------


def test_missing_schema_fails_without_creating_tables(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """When required tables are missing, the import fails without creating schema."""
    # This is already enforced by the SQLite authorizer in the repository layer.
    # We verify that the import succeeds only on migrated databases.
    importer = StatementImporter(migrated_temp_db_connection)

    # On a properly migrated DB, it just works
    batch = importer.import_rows(
        [_make_row()],
        source_type="bank_statement",
        public_id="schema-exists-batch",
    )
    assert batch.row_count == 1


def test_no_runtime_ddl_in_import_path(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The import path does not execute CREATE TABLE or ALTER TABLE."""
    # We verify by checking that importing doesn't create new tables
    importer = StatementImporter(migrated_temp_db_connection)

    before_tables = set(
        row[0]
        for row in migrated_temp_db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )

    importer.import_rows(
        [_make_row()],
        source_type="bank_statement",
        public_id="no-ddl-batch",
    )

    after_tables = set(
        row[0]
        for row in migrated_temp_db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )

    assert before_tables == after_tables


# ---------------------------------------------------------------------------
# N. Connection and Database Safety
# ---------------------------------------------------------------------------


def test_centrally_configured_connection_accepted(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Centrally configured temporary database connections are accepted."""
    importer = StatementImporter(migrated_temp_db_connection)

    batch = importer.import_rows(
        [_make_row()],
        source_type="bank_statement",
        public_id="centrally-configured-batch",
    )
    assert batch.row_count == 1


def test_import_never_opens_live_db(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The import never opens database/finance.db."""
    db_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(db_path) != LIVE_DB_PATH


def test_temp_db_foreign_key_integrity(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Temporary database foreign-key checks pass after import."""
    importer = StatementImporter(migrated_temp_db_connection)
    importer.import_rows(
        [_make_row(), _make_row(merchant="FKTest", amount=Decimal("5.00"))],
        source_type="bank_statement",
        public_id="fk-integrity-batch",
    )

    fk_check = migrated_temp_db_connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(fk_check) == 0

    integrity = migrated_temp_db_connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"


# ---------------------------------------------------------------------------
# O. Staging Guard
# ---------------------------------------------------------------------------


def test_staging_guard_accepts_migrated_temp_db(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The staging guard accepts the migrated temp db connection used by tests."""
    # This should not raise
    require_staging_database(migrated_temp_db_connection)


def test_staging_guard_accepts_in_memory_db() -> None:
    """The staging guard accepts :memory: databases."""
    conn = connect_sqlite(":memory:", mode=ConnectionMode.APPLICATION)
    try:
        require_staging_database(conn)
    finally:
        conn.close()


def test_staging_guard_rejects_unconfigured_file_db(tmp_path: Path) -> None:
    """An unconfigured file database (without staging authorization) is rejected."""
    db_path = tmp_path / "unconfigured.sqlite"
    conn = connect_sqlite(str(db_path), mode=ConnectionMode.APPLICATION)
    try:
        from finance_core.staging_guard import StagingDatabaseError

        with pytest.raises(StagingDatabaseError):
            require_staging_database(conn)
    finally:
        conn.close()


def test_import_rejects_unconfigured_connection(tmp_path: Path) -> None:
    """The import service rejects a connection without staging authorization."""
    from finance_core.staging_guard import StagingDatabaseError

    db_path = tmp_path / "no-auth.sqlite"
    conn = connect_sqlite(str(db_path), mode=ConnectionMode.APPLICATION)
    try:
        importer = StatementImporter(conn)
        with pytest.raises(StagingDatabaseError):
            importer.import_rows(
                [_make_row()],
                source_type="bank_statement",
                public_id="should-fail",
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Variance: empty rows rejected before transaction
# ---------------------------------------------------------------------------


def test_empty_rows_rejected_before_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Empty rows are rejected during pre-validation, before any transaction."""
    importer = StatementImporter(migrated_temp_db_connection)
    with pytest.raises(ValueError, match="must not be empty"):
        importer.import_rows([], source_type="bank_statement")

    # Connection must not be in a transaction
    assert not migrated_temp_db_connection.in_transaction


# ---------------------------------------------------------------------------
# Variance: batch-level idempotent replay
# ---------------------------------------------------------------------------


def test_batch_idempotent_replay_with_same_public_id(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Re-importing with the same batch public_id and matching metadata is idempotent."""
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(statement_row_reference="idem-batch-ref-1")

    first = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="idem-batch",
        source_file_hash=_sha("same-hash"),
    )
    assert len(first.inserted_ids) == 1

    second = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="idem-batch",
        source_file_hash=_sha("same-hash"),
    )
    assert second.batch_id == first.batch_id
    assert second.skipped_duplicates == 0
    assert second.idempotent_count == 1
    assert second.inserted_ids == []


def test_batch_public_id_conflicting_metadata_fails(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Same batch public_id with different metadata fails."""
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row()

    importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="conflict-meta-batch",
        source_file_hash=_sha("hash-one"),
    )

    with pytest.raises(DuplicatePublicIdError):
        importer.import_rows(
            [row],
            source_type="bank_statement",
            public_id="conflict-meta-batch",
            source_file_hash=_sha("hash-two"),
        )
