"""Read-only receipt calculator-input projection boundary (IAF.6).

Deterministically maps one **active, calculator-ready IAF fact set** into the
input shape accepted by
[`calculate_receipt_split`](receipt_split_calculator.py), per Section 16 of
``docs/design/receipt_item_allocation_facts_boundary_v1.md``.  It reads and
maps only; it never runs the calculator, persists a result, or mutates any
lifecycle state.

Contract (design Section 16):

* Projection runs only when the receipt's calculator-readiness report is
  positive at the **same read snapshot**.  When this boundary establishes
  its own read transaction it manages only that transaction; a caller-owned
  transaction is preserved untouched, so readiness and projection observe
  one coherent SQLite snapshot.
* The active fact set is located through the registry and every fact query
  is bound by ``fact_set_id`` / the active registry row; superseded,
  ad-hoc, and legacy rows are never consumed.
* SQL selects facts only; all mapping and validation happen in Python.
* Canonical monetary strings are carried verbatim (never the NUMERIC
  mirrors, never floats).
* Only approved v1 allocation/adjustment methods are mapped.  Any fact that
  cannot map one-to-one onto the calculator fails closed with a typed
  error; nothing is inferred, defaulted, or silently dropped.
* The projected receipt fixes the approved payer-following rounding policy
  (``docs/business_rules/rounding_rules.md``); no unapproved business rule
  is invented here.

The return value additionally carries the immutable active-fact-set binding
four-tuple (public ID, version, ``fact_set_input_hash``,
``fact_set_result_hash``), the receipt/conversion identity, the currency and
authoritative net-paid canonical text, and source-evidence references
sufficient for the future IAF.7 bridge to build authoritative snapshot
source references.  This boundary does not build the snapshot itself.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.calculators.receipt_calculator_input_mapping import (
    CalculatorInputMappingError,
    build_calculator_receipt,
    build_participant_list,
)
from finance_core.calculators.receipt_calculator_readiness import (
    ReceiptCalculatorReadinessError,
    report_receipt_calculator_readiness,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ReceiptCalculatorInputProjectionError(ValueError):
    """Base error for the calculator-input projection boundary."""


class ProjectionStagingDatabaseRejectedError(ReceiptCalculatorInputProjectionError):
    """The staging guard rejected the database (live database, copies)."""


class ReceiptNotCalculatorReadyError(ReceiptCalculatorInputProjectionError):
    """The receipt is not calculator-ready at this read snapshot.

    Carries the stable not-ready reason codes so callers can distinguish an
    ordinary "no fact set yet" receipt from an integrity failure (which is
    raised as :class:`ProjectionIntegrityError` / a readiness error).
    """

    def __init__(self, message: str, not_ready_reasons: tuple[str, ...]) -> None:
        super().__init__(message)
        self.not_ready_reasons = not_ready_reasons


class ProjectionIntegrityError(ReceiptCalculatorInputProjectionError):
    """Persisted fact-set state drifted, or an unsupported rule was found.

    This includes a mid-read supersession/binding change, an
    unsupported/ambiguous allocation or adjustment method, a broken
    evidence chain, or any canonical payload state that cannot be mapped
    one-to-one onto the calculator.  These states fail closed.
    """


# ---------------------------------------------------------------------------
# Public projection result
# ---------------------------------------------------------------------------

PROJECTION_SCHEMA_VERSION = "v1"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ReceiptCalculatorInputSourceEvidence:
    """Durable, hash-anchored evidence references for the downstream bridge."""

    receipt_public_id: str
    conversion_command_public_id: str
    conversion_result_hash: str
    confirmation_public_id: str
    proposal_content_hash: str
    attachment_content_hash: str


@dataclass(frozen=True)
class ReceiptCalculatorInputProjection:
    """Immutable deterministic calculator-input projection.

    Exact replay of the same read against the same persisted state — on the
    same connection or a fresh one — produces an equal projection.
    """

    calculator_input: dict[str, Any]
    fact_set_public_id: str
    fact_set_version: int
    fact_set_input_hash: str
    fact_set_result_hash: str
    receipt_public_id: str
    conversion_command_public_id: str
    conversion_result_hash: str
    currency: str
    net_paid_amount_canonical_text: str
    source_evidence: ReceiptCalculatorInputSourceEvidence
    receipt_merchant: str = ""
    receipt_date: str = ""
    receipt_source_channel: str = ""
    schema_version: str = PROJECTION_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def project_receipt_calculator_input(
    conn: sqlite3.Connection,
    receipt_public_id: str,
) -> ReceiptCalculatorInputProjection:
    """Project one calculator-ready receipt fact set into calculator input.

    SELECT-only: performs no writes, runs no calculator, and mutates no
    lifecycle state.  When the caller is not already inside a transaction,
    this boundary opens and owns a read transaction so readiness and
    projection share one snapshot, then rolls that transaction back; a
    caller-owned transaction is left untouched.

    Raises the typed errors of this module (staging rejection, not-ready,
    integrity/unsupported-mapping) and re-raises the readiness boundary's
    integrity errors unchanged.
    """
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise ProjectionStagingDatabaseRejectedError(str(exc)) from exc

    if not isinstance(receipt_public_id, str) or not receipt_public_id.strip():
        raise ProjectionIntegrityError(
            f"Receipt public ID must be a non-empty string, got: {receipt_public_id!r}"
        )

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        # Deferred read transaction: the first SELECT establishes a coherent
        # snapshot that both readiness and projection observe.
        conn.execute("BEGIN")
    try:
        return _project_within_snapshot(conn, receipt_public_id)
    finally:
        if owns_transaction:
            conn.rollback()


def _project_within_snapshot(
    conn: sqlite3.Connection,
    receipt_public_id: str,
) -> ReceiptCalculatorInputProjection:
    try:
        readiness = report_receipt_calculator_readiness(conn, receipt_public_id)
    except ReceiptCalculatorReadinessError:
        # Staging/receipt/integrity errors from the readiness boundary are
        # already typed and fail closed; propagate unchanged.
        raise

    if not readiness.is_calculator_ready:
        raise ReceiptNotCalculatorReadyError(
            f"Receipt {receipt_public_id!r} is not calculator-ready; projection "
            "requires a positive readiness report at the same read snapshot",
            not_ready_reasons=readiness.not_ready_reasons,
        )

    active = _fetch_active_registry_row(conn, receipt_public_id, readiness)
    conversion = _fetch_conversion_binding(conn, active)
    payload = _parse_canonical_payload(active, readiness)
    included, payer_public_id = _fetch_included_membership_and_payer(conn, active["receipt_id"])

    currency = str(readiness.currency)
    net_paid_text = str(readiness.net_paid_amount_canonical_text)
    merchant, receipt_date, source_channel = _require_confirmed_receipt_identity(active)

    # One shared mapping implementation, also used by the readiness preflight.
    try:
        participants = build_participant_list(included, payer_public_id)
        calculator_receipt = build_calculator_receipt(
            receipt_public_id=receipt_public_id,
            merchant=merchant,
            currency=currency,
            payer_public_id=payer_public_id,
            net_paid_text=net_paid_text,
            payload=payload,
            included=included,
        )
    except CalculatorInputMappingError as exc:
        raise ProjectionIntegrityError(str(exc)) from exc

    calculator_input: dict[str, Any] = {
        "case_id": receipt_public_id,
        "currency": currency,
        "participants": participants,
        "payer": payer_public_id,
        "receipts": [calculator_receipt],
    }

    source_evidence = _load_source_evidence(conn, receipt_public_id, conversion)

    return ReceiptCalculatorInputProjection(
        calculator_input=calculator_input,
        fact_set_public_id=str(active["fact_set_public_id"]),
        fact_set_version=int(active["version"]),
        fact_set_input_hash=str(active["fact_set_input_hash"]),
        fact_set_result_hash=str(active["fact_set_result_hash"]),
        receipt_public_id=receipt_public_id,
        conversion_command_public_id=str(conversion["command_public_id"]),
        conversion_result_hash=str(conversion["conversion_result_hash"]),
        currency=currency,
        net_paid_amount_canonical_text=net_paid_text,
        source_evidence=source_evidence,
        receipt_merchant=merchant,
        receipt_date=receipt_date,
        receipt_source_channel=source_channel,
    )


def _require_confirmed_receipt_identity(active: dict[str, Any]) -> tuple[str, str, str]:
    """Return the confirmed receipt's (merchant, receipt date, source channel).

    B4.1 always persists all three on a conversion-created receipt, so absence
    or an unexpected date shape is persisted-state drift and fails closed: the
    downstream canonical transaction must never fall back to the receipt public
    ID or the finalization clock.
    """
    merchant = active["receipt_merchant"]
    receipt_date = active["receipt_datetime"]
    source_channel = active["receipt_source_channel"]
    if not isinstance(merchant, str) or not merchant.strip():
        raise ProjectionIntegrityError(
            "The conversion-created receipt carries no merchant fact; the "
            "canonical transaction metadata cannot be projected"
        )
    if not isinstance(receipt_date, str) or not _ISO_DATE_RE.match(receipt_date):
        raise ProjectionIntegrityError(
            "The conversion-created receipt carries no ISO YYYY-MM-DD receipt "
            "date fact; the canonical transaction date cannot be projected"
        )
    if not isinstance(source_channel, str) or not source_channel.strip():
        raise ProjectionIntegrityError(
            "The conversion-created receipt carries no source channel fact; "
            "the source identity cannot be projected"
        )
    return merchant, receipt_date, source_channel


# ---------------------------------------------------------------------------
# Active fact-set + binding resolution (all fact_set / receipt scoped)
# ---------------------------------------------------------------------------


def _fetch_active_registry_row(
    conn: sqlite3.Connection,
    receipt_public_id: str,
    readiness: Any,
) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        "SELECT ras.*, r.merchant AS receipt_merchant, "
        "r.receipt_datetime AS receipt_datetime, "
        "r.source_channel AS receipt_source_channel "
        "FROM receipt_item_allocation_fact_sets ras "
        "JOIN receipts r ON r.id = ras.receipt_id "
        "WHERE r.public_id = ? AND ras.superseded_by_fact_set_public_id IS NULL",
        (receipt_public_id,),
    )
    if len(rows) != 1:
        raise ProjectionIntegrityError(
            f"Receipt {receipt_public_id!r} has {len(rows)} active fact-set rows "
            "at projection time; exactly one is required and the active binding "
            "must not change between readiness and projection"
        )
    active = rows[0]
    if (
        str(active["fact_set_public_id"]) != str(readiness.active_fact_set_public_id)
        or int(active["version"]) != int(readiness.active_fact_set_version)
        or str(active["fact_set_result_hash"]) != str(readiness.fact_set_result_hash)
    ):
        raise ProjectionIntegrityError(
            "The active fact set changed between the readiness read and the "
            "projection read; retry from a fresh snapshot"
        )
    return active


def _fetch_conversion_binding(conn: sqlite3.Connection, active: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        "SELECT * FROM receipt_proposal_conversions WHERE command_public_id = ?",
        (active["conversion_command_public_id"],),
    )
    if len(rows) != 1:
        raise ProjectionIntegrityError(
            "The active fact set does not resolve to exactly one B4.1 conversion "
            "registry row; the receipt/conversion binding is broken"
        )
    conversion = rows[0]
    if int(conversion["receipt_id"]) != int(active["receipt_id"]) or str(
        conversion["conversion_result_hash"]
    ) != str(active["expected_conversion_result_hash"]):
        raise ProjectionIntegrityError(
            "The active fact set is not bound to its receipt's B4.1 conversion "
            "(receipt or conversion result hash mismatch)"
        )
    return conversion


def _parse_canonical_payload(active: dict[str, Any], readiness: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(active["canonical_fact_set_payload"]))
    except (json.JSONDecodeError, RecursionError, MemoryError) as exc:
        raise ProjectionIntegrityError(
            "The active fact set carries a canonical payload that is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectionIntegrityError("The canonical fact-set payload is not a JSON object")
    if (
        payload.get("receipt_public_id") != readiness.receipt_public_id
        or payload.get("currency") != readiness.currency
        or payload.get("net_paid_amount") != readiness.net_paid_amount_canonical_text
    ):
        raise ProjectionIntegrityError(
            "The canonical fact-set payload contradicts the verified readiness identity"
        )
    for key in ("items", "allocations", "adjustments"):
        if not isinstance(payload.get(key), list):
            raise ProjectionIntegrityError(f"The canonical fact-set payload {key!r} is malformed")
    return payload


def _fetch_included_membership_and_payer(
    conn: sqlite3.Connection, receipt_id: Any
) -> tuple[set[str], str]:
    rows = _fetch_all(
        conn,
        "SELECT p.public_id AS participant_public_id, rp.role AS role, "
        "rp.is_included AS is_included "
        "FROM receipt_participants rp "
        "JOIN participants p ON p.id = rp.participant_id "
        "WHERE rp.receipt_id = ?",
        (receipt_id,),
    )
    if not rows:
        raise ProjectionIntegrityError("The conversion-created receipt has no membership rows")
    included: set[str] = set()
    payer_public_id: str | None = None
    for row in rows:
        public_id = str(row["participant_public_id"])
        if row["role"] == "payer":
            if payer_public_id is not None:
                raise ProjectionIntegrityError("The receipt has more than one payer membership row")
            payer_public_id = public_id
        if int(row["is_included"]) == 1:
            included.add(public_id)
    if payer_public_id is None:
        raise ProjectionIntegrityError("The receipt has no payer membership row")
    return included, payer_public_id


# ---------------------------------------------------------------------------
# Source evidence (durable references for the IAF.7 snapshot bridge)
# ---------------------------------------------------------------------------


def _load_source_evidence(
    conn: sqlite3.Connection,
    receipt_public_id: str,
    conversion: dict[str, Any],
) -> ReceiptCalculatorInputSourceEvidence:
    link_rows = _fetch_all(
        conn,
        "SELECT extraction_id FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
        (conversion["parser_output_id"],),
    )
    if len(link_rows) != 1:
        raise ProjectionIntegrityError(
            "The conversion's OCR evidence link does not resolve to exactly one extraction"
        )
    extraction_rows = _fetch_all(
        conn,
        "SELECT source_attachment_hash FROM receipt_ocr_extractions WHERE id = ?",
        (link_rows[0]["extraction_id"],),
    )
    if len(extraction_rows) != 1:
        raise ProjectionIntegrityError(
            "The conversion's OCR extraction row is missing from the evidence chain"
        )
    return ReceiptCalculatorInputSourceEvidence(
        receipt_public_id=receipt_public_id,
        conversion_command_public_id=str(conversion["command_public_id"]),
        conversion_result_hash=str(conversion["conversion_result_hash"]),
        confirmation_public_id=str(conversion["confirmation_public_id"]),
        proposal_content_hash=str(conversion["proposal_content_hash"]),
        attachment_content_hash=str(extraction_rows[0]["source_attachment_hash"]),
    )


# ---------------------------------------------------------------------------
# Row helpers (support connections with or without sqlite3.Row)
# ---------------------------------------------------------------------------


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    if cursor.description is None:
        raise ProjectionIntegrityError(
            "SQLite cursor did not expose column metadata for a projection query"
        )
    columns = [column[0] for column in cursor.description]
    return [_row_to_dict(row, columns) for row in cursor.fetchall()]


def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, dict):
        return {column: row[column] for column in columns}
    return dict(zip(columns, row, strict=True))


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "ProjectionIntegrityError",
    "ProjectionStagingDatabaseRejectedError",
    "ReceiptCalculatorInputProjection",
    "ReceiptCalculatorInputProjectionError",
    "ReceiptCalculatorInputSourceEvidence",
    "ReceiptNotCalculatorReadyError",
    "project_receipt_calculator_input",
]
