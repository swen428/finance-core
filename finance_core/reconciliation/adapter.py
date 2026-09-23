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
from typing import Callable, Protocol, cast

from finance_core.application.correction_schema import has_committed_correction
from finance_core.reconciliation.models import InternalCandidate


class _EffectiveFields(Protocol):
    amount: str
    currency: str
    transaction_date: str
    merchant: str | None


class _EffectiveTransaction(Protocol):
    target_id: str
    fields: _EffectiveFields


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
          intent_type,
          status,
          account_id
        FROM transactions
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        effective_reader: Callable[[sqlite3.Connection, str], _EffectiveTransaction] | None = None,
    ) -> None:
        conn.row_factory = sqlite3.Row
        if not conn.in_transaction and conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0:
            conn.execute("PRAGMA foreign_keys = ON")
        self._conn = conn
        self._effective_reader = effective_reader

    def fetch_candidates(
        self,
        filters: CandidateFilter | None = None,
    ) -> list[InternalCandidate]:
        """Return a deterministically ordered list of ``InternalCandidate``
        objects from the ``transactions`` table.

        Rows with a NULL ``amount`` are silently excluded.
        """
        owns_snapshot = not self._conn.in_transaction
        if owns_snapshot:
            self._conn.execute("BEGIN")
        try:
            # Fetch every target before filtering: a corrected row must not be
            # hidden by a predicate over its now historical original columns.
            rows = self._conn.execute(self._BASE_SELECT).fetchall()
            candidates: list[tuple[str, int, InternalCandidate]] = []
            for row in rows:
                values = dict(row)
                target_id = str(values["public_id"])
                if has_committed_correction(self._conn, target_id):
                    if self._effective_reader is None:
                        raise ValueError("corrected_transaction_requires_trusted_effective_reader")
                    effective = self._effective_reader(self._conn, target_id)
                    if getattr(effective, "target_id", None) != target_id:
                        raise ValueError("effective reader returned another transaction")
                    fields = effective.fields
                    values.update(
                        amount=fields.amount,
                        currency=fields.currency,
                        transaction_date=fields.transaction_date,
                        merchant=fields.merchant,
                    )
                if not self._matches_filters(values, filters):
                    continue
                candidate = self._row_to_candidate(values)
                if candidate is not None:
                    candidates.append(
                        (str(values["transaction_date"]), cast(int, values["id"]), candidate)
                    )
            candidates.sort(key=lambda pair: (pair[0], pair[1]))
            return [candidate for _, _, candidate in candidates]
        finally:
            if owns_snapshot:
                self._conn.rollback()

    def _matches_filters(self, row: dict[str, object], filters: CandidateFilter | None) -> bool:
        f = filters or CandidateFilter()
        status = f.status if f.status is not None else self._DEFAULT_STATUS
        if row["status"] != status:
            return False
        for field in ("intent", "intent_type", "source_channel", "account_id", "currency"):
            expected = getattr(f, field)
            if expected is not None and row[field] != expected:
                return False
        transaction_date = row["transaction_date"]
        if transaction_date is None:
            return False
        if f.date_from is not None and str(transaction_date) < f.date_from:
            return False
        if f.date_to is not None and str(transaction_date) > f.date_to:
            return False
        if f.merchant_like is not None:
            matched = self._conn.execute(
                "SELECT ? LIKE ?", (row["merchant"], f.merchant_like)
            ).fetchone()[0]
            if matched != 1:
                return False
        return True

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

    def _row_to_candidate(self, row: sqlite3.Row | dict[str, object]) -> InternalCandidate | None:
        """Convert a ``transactions`` row to an ``InternalCandidate``.

        Returns ``None`` when ``amount`` or ``transaction_date`` is
        NULL or invalid (should already be filtered, but this is a second
        guard).
        """
        amount_raw = row["amount"]
        if amount_raw is None:
            return None

        date_raw = row["transaction_date"]
        txn_date = _parse_date(None if date_raw is None else str(date_raw))
        if txn_date is None:
            return None

        return InternalCandidate(
            internal_id=str(row["public_id"]),
            transaction_date=txn_date,
            merchant=str(row["merchant"] or ""),
            amount=Decimal(str(amount_raw)),
            currency=str(row["currency"] or ""),
            source_type=None if row["intent"] is None else str(row["intent"]),
            source_channel=(None if row["source_channel"] is None else str(row["source_channel"])),
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
