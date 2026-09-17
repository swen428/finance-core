"""Internal read-only conversion-state inspection helpers.

Shared by the legacy converter, completion, and receipt supersession
boundaries so mutual-exclusion guards use one fixed SQL definition per
conversion evidence source instead of divergent copies.

All helpers are transaction-neutral: they never commit, roll back, or
mutate any row, and they run no DDL.  The future B4 receipt conversion
registry (``receipt_proposal_conversions``) is recognised read-only and
forward-compatibly: an absent table means "no registry state", while a
present-but-malformed table fails closed by letting the underlying
``sqlite3`` error propagate to the caller's transaction handling.

This module is internal to ``finance_core.parser_proposals`` and is intentionally
not re-exported from the package ``__init__``.
"""

from __future__ import annotations

import sqlite3

RECEIPT_CONVERSION_REGISTRY_TABLE = "receipt_proposal_conversions"


def has_receipt_ocr_proposal_link(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    """Return whether the proposal carries a persisted receipt OCR link.

    ``receipt_ocr_proposal_links`` enforces ``UNIQUE(parser_output_id)``, so
    any row (``initial`` or ``superseding_correction``) marks the proposal as
    a receipt proposal owned by the guarded receipt conversion boundary.
    """
    row = conn.execute(
        "SELECT 1 FROM receipt_ocr_proposal_links WHERE parser_output_id = ? LIMIT 1",
        (parser_output_id,),
    ).fetchone()
    return row is not None


def has_legacy_transaction_conversion(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    """Return whether a legacy conversion audit row exists for the proposal."""
    row = conn.execute(
        "SELECT 1 FROM parser_proposal_conversion_audit WHERE parser_output_id = ? LIMIT 1",
        (parser_output_id,),
    ).fetchone()
    return row is not None


def receipt_conversion_registry_exists(conn: sqlite3.Connection) -> bool:
    """Return whether the future B4 receipt conversion registry table exists.

    Detection is by exact table name in ``sqlite_master`` only; this module
    never creates the registry and never runs DDL of any kind.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RECEIPT_CONVERSION_REGISTRY_TABLE,),
    ).fetchone()
    return row is not None


def has_receipt_registry_conversion(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    """Return whether the future registry already records this proposal.

    An absent registry table deterministically means no registry conversion
    state.  A present registry is queried with one fixed statement; a
    present-but-malformed registry (for example missing the
    ``parser_output_id`` column) raises the underlying ``sqlite3`` error so
    callers fail closed instead of silently treating the proposal as
    unconverted.
    """
    if not receipt_conversion_registry_exists(conn):
        return False
    row = conn.execute(
        "SELECT 1 FROM receipt_proposal_conversions WHERE parser_output_id = ? LIMIT 1",
        (parser_output_id,),
    ).fetchone()
    return row is not None
