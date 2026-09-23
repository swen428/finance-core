"""Tests for Reconciliation Candidate Adapter v1.

Covers:
  1. fetch_candidates maps rows to InternalCandidate
  2. fetch_candidates excludes NULL-amount rows
  3. Decimal amounts round-trip correctly
  4. candidate_count returns correct count
  5. intent filter works
  6. intent_type filter works
  7. date range filters work
  8. source_channel filter works
  9. status filter works (default active)
 10. merchant_like filter works
 11. currency filter works
 12. deterministic ordering (by transaction_date, id)
 13. adapter is read-only (no INSERT/UPDATE/DELETE)
 14. adapter does not touch database/finance.db
 15. empty results when no rows match
 16. adapter maps all fields correctly
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from finance_core.reconciliation.adapter import CandidateFilter, InternalCandidateAdapter
from finance_core.reconciliation.models import InternalCandidate

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_transaction(
    conn: sqlite3.Connection,
    public_id: str,
    merchant: str = "Apple",
    amount: Decimal | None = Decimal("29.90"),
    currency: str = "SGD",
    transaction_date: str = "2024-12-01",
    intent: str = "expense",
    intent_type: str = "Manual",
    source_channel: str = "telegram",
    status: str = "active",
    account_id: int | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO transactions (
          public_id, merchant, amount, currency, transaction_date,
          intent, intent_type, source_channel, status, account_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            merchant,
            str(amount) if amount is not None else None,
            currency,
            transaction_date,
            intent,
            intent_type,
            source_channel,
            status,
            account_id,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM transactions WHERE public_id = ?", (public_id,)).fetchone()
    return row["id"]


def _make_adapter(conn: sqlite3.Connection) -> InternalCandidateAdapter:
    return InternalCandidateAdapter(conn)


def test_effective_fields_are_verified_before_filter_order_and_count(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    _seed_transaction(conn, "txn-corrected", merchant="Old", amount=Decimal("5.00"),
                      transaction_date="2024-01-01")
    _seed_transaction(conn, "txn-original", merchant="Other", amount=Decimal("7.00"),
                      transaction_date="2024-06-01")
    monkeypatch.setattr(
        "finance_core.reconciliation.adapter.has_committed_correction",
        lambda _conn, target: target == "txn-corrected",
    )
    effective = SimpleNamespace(
        target_id="txn-corrected",
        fields=SimpleNamespace(
            amount="12.34", currency="USD", transaction_date="2024-12-01", merchant="New Shop"
        ),
    )
    adapter = InternalCandidateAdapter(conn, effective_reader=lambda _conn, _id: effective)
    filters = CandidateFilter(
        merchant_like="new%", date_from="2024-11-01", currency="USD"
    )
    candidates = adapter.fetch_candidates(filters)
    assert [candidate.internal_id for candidate in candidates] == ["txn-corrected"]
    assert candidates[0].amount == Decimal("12.34")
    assert adapter.candidate_count(filters) == 1
    assert [candidate.internal_id for candidate in adapter.fetch_candidates()] == [
        "txn-original", "txn-corrected"
    ]
    with pytest.raises(ValueError, match="trusted_effective_reader"):
        InternalCandidateAdapter(conn).fetch_candidates(CandidateFilter(merchant_like="no-match"))


# ---------------------------------------------------------------------------
# 1. fetch_candidates maps rows to InternalCandidate
# ---------------------------------------------------------------------------


def test_fetch_candidates_maps_to_internal_candidate(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-apple")

    candidates = adapter.fetch_candidates()

    assert len(candidates) == 1
    assert isinstance(candidates[0], InternalCandidate)
    assert candidates[0].internal_id == "txn-apple"
    assert candidates[0].merchant == "Apple"
    assert candidates[0].amount == Decimal("29.90")
    assert candidates[0].currency == "SGD"


# ---------------------------------------------------------------------------
# 2. fetch_candidates excludes NULL-amount rows
# ---------------------------------------------------------------------------


def test_fetch_candidates_excludes_null_amount(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-null-amt", amount=None)
    _seed_transaction(migrated_temp_db_connection, "txn-valid")

    candidates = adapter.fetch_candidates()

    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-valid"


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2b. fetch_candidates excludes NULL transaction_date rows
# ---------------------------------------------------------------------------


def test_fetch_candidates_excludes_invalid_transaction_date_sql(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-null-date", transaction_date="")
    _seed_transaction(migrated_temp_db_connection, "txn-valid")

    candidates = adapter.fetch_candidates()

    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-valid"


# ---------------------------------------------------------------------------
# 2c. fetch_candidates excludes invalid transaction_date rows
# ---------------------------------------------------------------------------


def test_fetch_candidates_excludes_invalid_transaction_date(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-bad-date", transaction_date="not-a-date")
    _seed_transaction(migrated_temp_db_connection, "txn-valid")

    candidates = adapter.fetch_candidates()

    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-valid"


# ---------------------------------------------------------------------------
# 2d. candidate_count excludes rows excluded by fetch_candidates
# ---------------------------------------------------------------------------


def test_candidate_count_matches_fetch_candidates_for_invalid_dates(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-null-date", transaction_date="")
    _seed_transaction(migrated_temp_db_connection, "txn-valid")

    assert adapter.candidate_count() == 1


# ---------------------------------------------------------------------------
# 2e. no date.today() fallback is used (invalid date excluded, not fabricated)
# ---------------------------------------------------------------------------


def test_no_date_today_fallback(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Invalid or NULL dates must be excluded, never silently fabricated."""
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-bad-date", transaction_date="garbage")

    candidates = adapter.fetch_candidates()
    # The bad-date row should be excluded entirely, not appear with a fabricated date
    assert len(candidates) == 0


# 3. Decimal amounts round-trip correctly
# ---------------------------------------------------------------------------


def test_decimal_amount_round_trip(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(
        migrated_temp_db_connection,
        "txn-decimal",
        amount=Decimal("123.45"),
    )

    candidates = adapter.fetch_candidates()
    assert candidates[0].amount == Decimal("123.45")
    assert isinstance(candidates[0].amount, Decimal)


# ---------------------------------------------------------------------------
# 4. candidate_count returns correct count
# ---------------------------------------------------------------------------


def test_candidate_count(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-c1")
    _seed_transaction(migrated_temp_db_connection, "txn-c2")
    _seed_transaction(migrated_temp_db_connection, "txn-c3", amount=None)

    assert adapter.candidate_count() == 2


# ---------------------------------------------------------------------------
# 5. intent filter works
# ---------------------------------------------------------------------------


def test_intent_filter(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-exp", intent="expense")
    _seed_transaction(migrated_temp_db_connection, "txn-inc", intent="income")

    candidates = adapter.fetch_candidates(CandidateFilter(intent="expense"))
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-exp"


# ---------------------------------------------------------------------------
# 6. intent_type filter works
# ---------------------------------------------------------------------------


def test_intent_type_filter(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-manual", intent_type="Manual")
    _seed_transaction(migrated_temp_db_connection, "txn-gen", intent_type="Generated")

    candidates = adapter.fetch_candidates(CandidateFilter(intent_type="Manual"))
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-manual"


# ---------------------------------------------------------------------------
# 7. date range filters work
# ---------------------------------------------------------------------------


def test_date_range_filter(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-nov", transaction_date="2024-11-01")
    _seed_transaction(migrated_temp_db_connection, "txn-dec", transaction_date="2024-12-15")
    _seed_transaction(migrated_temp_db_connection, "txn-jan", transaction_date="2025-01-10")

    candidates = adapter.fetch_candidates(
        CandidateFilter(date_from="2024-12-01", date_to="2024-12-31")
    )
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-dec"


# ---------------------------------------------------------------------------
# 8. source_channel filter works
# ---------------------------------------------------------------------------


def test_source_channel_filter(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-tg", source_channel="telegram")
    _seed_transaction(migrated_temp_db_connection, "txn-si", source_channel="statement_import")

    candidates = adapter.fetch_candidates(CandidateFilter(source_channel="telegram"))
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-tg"


# ---------------------------------------------------------------------------
# 9. status filter works (default active)
# ---------------------------------------------------------------------------


def test_status_filter_default_active(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-active", status="active")
    _seed_transaction(migrated_temp_db_connection, "txn-pending", status="pending_review")

    # Default: only active
    candidates = adapter.fetch_candidates()
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-active"

    # Explicit override
    candidates2 = adapter.fetch_candidates(CandidateFilter(status="pending_review"))
    assert len(candidates2) == 1
    assert candidates2[0].internal_id == "txn-pending"


# ---------------------------------------------------------------------------
# 10. merchant_like filter works
# ---------------------------------------------------------------------------


def test_merchant_like_filter(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-apple", merchant="Apple Store")
    _seed_transaction(migrated_temp_db_connection, "txn-netflix", merchant="Netflix")

    candidates = adapter.fetch_candidates(CandidateFilter(merchant_like="apple%"))
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-apple"


# ---------------------------------------------------------------------------
# 11. currency filter works
# ---------------------------------------------------------------------------


def test_currency_filter(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-sgd", currency="SGD")
    _seed_transaction(migrated_temp_db_connection, "txn-usd", currency="USD")

    candidates = adapter.fetch_candidates(CandidateFilter(currency="SGD"))
    assert len(candidates) == 1
    assert candidates[0].internal_id == "txn-sgd"


# ---------------------------------------------------------------------------
# 12. deterministic ordering (by transaction_date, id)
# ---------------------------------------------------------------------------


def test_deterministic_ordering(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-c", transaction_date="2024-12-02")
    _seed_transaction(migrated_temp_db_connection, "txn-a", transaction_date="2024-12-01")
    _seed_transaction(migrated_temp_db_connection, "txn-b", transaction_date="2024-12-01")

    candidates = adapter.fetch_candidates()

    # Same date: ordered by id ASC. txn-a seeded before txn-b.
    ids = [c.internal_id for c in candidates]
    assert ids == ["txn-a", "txn-b", "txn-c"]


# ---------------------------------------------------------------------------
# 13. adapter is read-only
# ---------------------------------------------------------------------------


def test_adapter_is_read_only(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-ro")

    before_count = adapter.candidate_count()
    candidates = adapter.fetch_candidates()
    after_count = adapter.candidate_count()

    assert before_count == after_count
    assert len(candidates) == before_count


# ---------------------------------------------------------------------------
# 14. adapter does not touch database/finance.db
# ---------------------------------------------------------------------------


def test_adapter_does_not_touch_live_db(
    migrated_temp_db_connection: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH

    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-live-check")
    candidates = adapter.fetch_candidates()
    assert len(candidates) == 1


# ---------------------------------------------------------------------------
# 15. empty results when no rows match
# ---------------------------------------------------------------------------


def test_empty_results_when_no_rows_match(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(migrated_temp_db_connection, "txn-sgd", currency="SGD")

    candidates = adapter.fetch_candidates(CandidateFilter(currency="EUR"))
    assert candidates == []


# ---------------------------------------------------------------------------
# 16. adapter maps all fields correctly
# ---------------------------------------------------------------------------


def test_adapter_maps_all_fields(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    adapter = _make_adapter(migrated_temp_db_connection)
    _seed_transaction(
        migrated_temp_db_connection,
        "txn-full",
        merchant="Spotify",
        amount=Decimal("9.99"),
        currency="SGD",
        transaction_date="2024-12-01",
        intent="expense",
        intent_type="Manual",
        source_channel="telegram",
    )

    candidates = adapter.fetch_candidates()
    c = candidates[0]

    assert c.internal_id == "txn-full"
    assert c.merchant == "Spotify"
    assert c.amount == Decimal("9.99")
    assert c.currency == "SGD"
    assert c.transaction_date == date(2024, 12, 1)
    assert c.source_type == "expense"
    assert c.source_channel == "telegram"
    assert c.evidence_reference is not None


def test_adapter_accepts_plain_sqlite_connection(
    migrated_temp_db_path: Path,
) -> None:
    conn = sqlite3.connect(migrated_temp_db_path)
    try:
        adapter = _make_adapter(conn)
        _seed_transaction(conn, "txn-plain-conn")

        candidates = adapter.fetch_candidates()

        assert [c.internal_id for c in candidates] == ["txn-plain-conn"]
    finally:
        conn.close()
