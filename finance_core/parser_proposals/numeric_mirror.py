"""Shared SQLite NUMERIC-affinity mirror contract for proposal boundaries."""

from __future__ import annotations

import math
import sqlite3
from decimal import Decimal
from typing import Any


def decimal_from_numeric_mirror(value: Any) -> Decimal | None:
    """Decode a SQLite NUMERIC mirror without accepting lossy representations.

    SQLite may return an INTEGER or REAL for a NUMERIC-affinity value.  REAL
    values are accepted only when their shortest representation is finite and
    plain decimal text; scientific notation is intentionally refused because
    it is not a stable destination representation for this boundary.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        text = repr(value)
        if "e" in text or "E" in text:
            return None
        return Decimal(text)
    return None


def sqlite_numeric_roundtrip_matches(
    conn: sqlite3.Connection,
    canonical_amount: str,
) -> bool:
    """Return whether SQLite's NUMERIC cast preserves the Decimal value."""
    row = conn.execute("SELECT CAST(? AS NUMERIC)", (canonical_amount,)).fetchone()
    if row is None:
        return False
    mirrored = decimal_from_numeric_mirror(row[0])
    try:
        expected = Decimal(canonical_amount)
    except (ArithmeticError, ValueError):
        return False
    return mirrored is not None and mirrored == expected


__all__ = ["decimal_from_numeric_mirror", "sqlite_numeric_roundtrip_matches"]
