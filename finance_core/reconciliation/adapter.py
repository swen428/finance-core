"""Reconciliation Candidate Adapter v1 -- read internal transaction records
from the core ``transactions`` table and convert them into matcher-ready
``InternalCandidate`` objects.

This adapter is **read-only**: it never modifies the database.  It wraps an
externally-provided ``sqlite3.Connection``.  The caller is responsible for
connection lifecycle and ensuring the core schema is already migrated.

Design properties:
- Deterministic candidate ordering (``ORDER BY transaction_date, id``).
- ``Decimal`` monetary columns round-tripped through string coercion.
- Optional filtering by intent, intent_type, status, date range, merchant,
  source_channel, account_id, and currency.
- Graceful handling of NULL amounts: rows with NULL ``amount`` are silently
  skipped (they cannot serve as reconciliation candidates).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finance_core.reconciliation.models import InternalCandidate


@dataclass(frozen=True)
class CandidateFilter:
    """Optional filter criteria for fetching internal candidates.

    All fields are optional.  When ``None``, the filter is not applied.
    """

    intent: str | None = None
    intent_type: str | None = None
    source_channel: str | None = None
    status: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    merchant_like: str | None = None
    account_id: int | None = None
    currency: str | None = None


class InternalCandidateAdapter:
    """Read-only adapter that reads internal transaction records from the
    core ``transactions`` table and returns ``InternalCandidate`` objects
    suitable for the reconciliation matcher.

    Usage::

        conn = connect_sqlite("...")
        adapter = InternalCandidateAdapter(conn)
        candidates = adapter.fetch_candidates()
    """

    _DEFAULT_STATUS = "active"

    _BASE_SELECT = """
        SELECT
          id,
          public_id,
          transaction_date,
          merchant,
          amount,
          currency,
          source_channel,
          intent,
          intent_type
        FROM transactions
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def fetch_candidates(
        self,
        filters: CandidateFilter | None = None,
    ) -> list[InternalCandidate]:
        """Return a deterministically ordered list of ``InternalCandidate``
        objects from the ``transactions`` table.

        Rows with a NULL ``amount`` are silently excluded.
        """
        query, params = self._build_query(filters)
        rows = self._conn.execute(query, params).fetchall()
        candidates: list[InternalCandidate] = []
        for row in rows:
            cand = self._row_to_candidate(row)
            if cand is not None:
                candidates.append(cand)
        return candidates

    def candidate_count(self, filters: CandidateFilter | None = None) -> int:
        """Return the number of candidates that would be fetched.

        Delegates to fetch_candidates() so that count and fetch use
        identical eligibility rules.
        """
        return len(self.fetch_candidates(filters))

    def _build_query(
        self,
        filters: CandidateFilter | None,
    ) -> tuple[str, tuple]:
        clauses: list[str] = []
        params: list = []

        if filters is not None:
            if filters.intent is not None:
                clauses.append("AND intent = ?")
                params.append(filters.intent)
            if filters.intent_type is not None:
                clauses.append("AND intent_type = ?")
                params.append(filters.intent_type)
            if filters.source_channel is not None:
                clauses.append("AND source_channel = ?")
                params.append(filters.source_channel)
            status = filters.status if filters.status is not None else self._DEFAULT_STATUS
            clauses.append("AND status = ?")
            params.append(status)
            if filters.date_from is not None:
                clauses.append("AND transaction_date >= ?")
                params.append(filters.date_from)
            if filters.date_to is not None:
                clauses.append("AND transaction_date <= ?")
                params.append(filters.date_to)
            if filters.merchant_like is not None:
                clauses.append("AND merchant LIKE ?")
                params.append(filters.merchant_like)
            if filters.account_id is not None:
                clauses.append("AND account_id = ?")
                params.append(filters.account_id)
            if filters.currency is not None:
                clauses.append("AND currency = ?")
                params.append(filters.currency)
        else:
            clauses.append("AND status = ?")
            params.append(self._DEFAULT_STATUS)

        clauses.append("AND transaction_date IS NOT NULL")

        where = "WHERE amount IS NOT NULL " + " ".join(clauses)
        order = "ORDER BY transaction_date ASC, id ASC"
        query = f"{self._BASE_SELECT} {where} {order}"
        return query, tuple(params)

    def _row_to_candidate(self, row: sqlite3.Row) -> InternalCandidate | None:
        """Convert a ``transactions`` row to an ``InternalCandidate``.

        Returns ``None`` when ``amount`` or ``transaction_date`` is
        NULL or invalid (should already be filtered, but this is a second
        guard).
        """
        amount_raw = row["amount"]
        if amount_raw is None:
            return None

        txn_date = _parse_date(row["transaction_date"])
        if txn_date is None:
            return None

        return InternalCandidate(
            internal_id=str(row["public_id"]),
            transaction_date=txn_date,
            merchant=row["merchant"] or "",
            amount=Decimal(str(amount_raw)),
            currency=row["currency"] or "",
            source_type=row["intent"],
            source_channel=row["source_channel"],
            evidence_reference=str(row["id"]),
        )


def _parse_date(raw: str | None) -> date | None:
    """Parse an ISO date string into a ``date`` object.

    Returns ``None`` when ``raw`` is None or not a valid ISO date
    string.  Adapter v1 must not fabricate source evidence; rows
    with missing or invalid ``transaction_date`` are excluded.
    """
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


__all__ = [
    "InternalCandidateAdapter",
    "CandidateFilter",
]
