"""SELECT-only IAF.4 receipt fact-set review boundary.

The reports expose the persisted B4.1 receipt/conversion binding, complete
receipt membership, active IAF fact set, and a bounded page of immutable
fact-set history needed for a human to author a later ``persist`` or
``supersede`` command.  A report is a review snapshot only: it never
authorizes a write and never claims that a later service invocation will
succeed.

Every public function is read-only and staging-guarded.  The IAF.2/IAF.3
services remain the sole write and full-validation authorities.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsError,
    verify_receipt_item_allocation_fact_set_for_review,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database


class ReceiptItemAllocationFactsReviewError(ValueError):
    """Base error for the read-only IAF.4 review boundary."""


class FactSetReviewStagingDatabaseRejectedError(ReceiptItemAllocationFactsReviewError):
    """The staging guard rejected the database identity."""


class InvalidFactSetReviewRequestError(ReceiptItemAllocationFactsReviewError):
    """The review request is malformed."""


class FactSetReviewReceiptNotFoundError(ReceiptItemAllocationFactsReviewError):
    """The requested receipt does not exist."""


class UnsupportedFactSetReviewReceiptError(ReceiptItemAllocationFactsReviewError):
    """The receipt is not bound through the guarded B4.1 conversion."""


class FactSetReviewIntegrityError(ReceiptItemAllocationFactsReviewError):
    """Persisted review material is contradictory or malformed."""


DEFAULT_REVIEW_LIMIT = 20
MAX_REVIEW_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100
SQLITE_MAX_INTEGER = (2**63) - 1
FACT_SET_REVIEW_LABEL = "candidate_for_fact_set_review"


@dataclass(frozen=True)
class FactSetReviewVersion:
    """One immutable registry version and its exact canonical payload."""

    command_public_id: str
    fact_set_public_id: str
    fact_set_version: int
    conversion_command_public_id: str
    expected_conversion_result_hash: str
    supersedes_fact_set_public_id: str | None
    superseded_by_fact_set_public_id: str | None
    command_material_hash: str
    fact_set_input_hash: str
    fact_set_result_hash: str
    canonical_fact_set_payload: str
    actor_type: str
    authenticated_actor_id: str
    channel: str
    reason: str | None
    audit_event_public_id: str
    schema_version: str
    created_at: str
    item_count: int
    allocation_count: int
    adjustment_count: int

    @property
    def is_active(self) -> bool:
        return self.superseded_by_fact_set_public_id is None


@dataclass(frozen=True)
class FactSetReviewReceipt:
    """Bounded list-row summary for one B4.1 conversion-created receipt."""

    receipt_public_id: str
    merchant: str
    receipt_datetime: str | None
    currency: str
    net_paid_amount_canonical_text: str
    receipt_status: str
    receipt_transaction_id_is_set: bool
    receipt_group_binding_count: int
    payer_participant_public_id: str
    conversion_command_public_id: str
    conversion_result_hash: str
    fact_set_count: int
    active_fact_set_public_id: str | None
    active_fact_set_version: int | None
    active_fact_set_result_hash: str | None
    review_label: str = FACT_SET_REVIEW_LABEL

    @property
    def fact_set_state(self) -> str:
        return "none" if self.active_fact_set_public_id is None else "active"


@dataclass(frozen=True)
class FactSetReviewMembership:
    """One explicit B4.1 receipt-membership fact."""

    participant_public_id: str
    display_name: str
    role: str
    is_included: bool


@dataclass(frozen=True)
class FactSetReviewDetail:
    """Complete bounded review report for one conversion-created receipt."""

    receipt: FactSetReviewReceipt
    membership: tuple[FactSetReviewMembership, ...]
    fact_sets: tuple[FactSetReviewVersion, ...]
    active_fact_set: FactSetReviewVersion | None
    history_anchor_version: int | None
    history_limit: int
    history_offset: int
    history_has_more: bool


_RECEIPT_REVIEW_SQL = """
SELECT r.id AS receipt_id,
       r.public_id AS receipt_public_id,
       r.merchant,
       r.receipt_datetime,
       r.currency,
       r.net_paid_amount_canonical_text,
       r.status AS receipt_status,
       r.transaction_id,
       (SELECT COUNT(*) FROM receipt_group_receipts AS rgr
        WHERE rgr.receipt_id = r.id) AS receipt_group_binding_count,
       payer.public_id AS payer_participant_public_id,
       conversion.command_public_id AS conversion_command_public_id,
       conversion.conversion_result_hash
FROM receipts AS r
JOIN receipt_proposal_conversions AS conversion
  ON conversion.receipt_id = r.id
LEFT JOIN participants AS payer
  ON payer.id = r.payer_participant_id
"""


def list_fact_set_review_receipts(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_REVIEW_LIMIT,
) -> list[FactSetReviewReceipt]:
    """List conversion-created receipts for human fact-set review.

    Deterministic ordering is by receipt public ID.  Listing is never a
    persistence or supersession eligibility guarantee; the guarded service
    revalidates the complete command and current database state.
    """
    _require_staging_readable(conn)
    _validate_limit(limit)
    cursor = conn.execute(
        _RECEIPT_REVIEW_SQL + "ORDER BY r.public_id ASC, r.id ASC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    columns = [column[0] for column in cursor.description]
    reviews: list[FactSetReviewReceipt] = []
    for raw_row in rows:
        row = _row_dict(columns, raw_row)
        fact_set_count, active = _load_fact_set_summary(
            conn,
            receipt_id=int(row["receipt_id"]),
            receipt_public_id=str(row["receipt_public_id"]),
            conversion_command_public_id=str(row["conversion_command_public_id"]),
            conversion_result_hash=str(row["conversion_result_hash"]),
        )
        reviews.append(_receipt_summary(row, fact_set_count, active))
    return reviews


def get_fact_set_review_detail(
    conn: sqlite3.Connection,
    receipt_public_id: str,
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    history_offset: int = 0,
    history_anchor_version: int | None = None,
) -> FactSetReviewDetail:
    """Return receipt, membership, active set, and one verified history page."""
    _require_staging_readable(conn)
    _validate_history_window(history_limit=history_limit, history_offset=history_offset)
    if (
        not isinstance(receipt_public_id, str)
        or not receipt_public_id.strip()
        or receipt_public_id != receipt_public_id.strip()
    ):
        raise InvalidFactSetReviewRequestError(
            "receipt_public_id must be a non-empty, whitespace-trimmed string"
        )

    receipt_rows = conn.execute(
        "SELECT id FROM receipts WHERE public_id = ?",
        (receipt_public_id,),
    ).fetchall()
    if not receipt_rows:
        raise FactSetReviewReceiptNotFoundError(f"Receipt not found: {receipt_public_id!r}")
    if len(receipt_rows) != 1:
        raise FactSetReviewIntegrityError(f"Receipt public ID {receipt_public_id!r} is not unique")

    cursor = conn.execute(
        _RECEIPT_REVIEW_SQL + "WHERE r.public_id = ? ORDER BY conversion.command_public_id",
        (receipt_public_id,),
    )
    rows = cursor.fetchall()
    columns = [column[0] for column in cursor.description]
    if not rows:
        raise UnsupportedFactSetReviewReceiptError(
            "Fact-set review supports only receipts created through the guarded B4.1 conversion"
        )
    if len(rows) != 1:
        raise FactSetReviewIntegrityError(
            f"Receipt {receipt_public_id!r} has {len(rows)} conversion bindings"
        )
    row = _row_dict(columns, rows[0])
    if row["payer_participant_public_id"] is None:
        raise FactSetReviewIntegrityError("The receipt payer participant row is missing")

    fact_set_count, active = _load_fact_set_summary(
        conn,
        receipt_id=int(row["receipt_id"]),
        receipt_public_id=receipt_public_id,
        conversion_command_public_id=str(row["conversion_command_public_id"]),
        conversion_result_hash=str(row["conversion_result_hash"]),
    )
    history_anchor_version = _resolve_history_anchor(
        active=active,
        requested_anchor=history_anchor_version,
    )
    versions = _load_fact_set_history_page(
        conn,
        receipt_id=int(row["receipt_id"]),
        receipt_public_id=receipt_public_id,
        history_anchor_version=history_anchor_version,
        history_limit=history_limit,
        history_offset=history_offset,
    )
    membership = _load_membership(
        conn,
        receipt_id=int(row["receipt_id"]),
        payer_participant_public_id=str(row["payer_participant_public_id"]),
    )
    final_fact_set_count, final_active = _load_fact_set_summary(
        conn,
        receipt_id=int(row["receipt_id"]),
        receipt_public_id=receipt_public_id,
        conversion_command_public_id=str(row["conversion_command_public_id"]),
        conversion_result_hash=str(row["conversion_result_hash"]),
    )
    if final_fact_set_count != fact_set_count or final_active != active:
        raise FactSetReviewIntegrityError(
            "Fact-set state changed during SELECT-only review; retry from a fresh snapshot"
        )
    return FactSetReviewDetail(
        receipt=_receipt_summary(row, fact_set_count, active),
        membership=membership,
        fact_sets=versions,
        active_fact_set=active,
        history_anchor_version=(None if history_anchor_version == 0 else history_anchor_version),
        history_limit=history_limit,
        history_offset=history_offset,
        history_has_more=history_offset + len(versions) < history_anchor_version,
    )


def _require_staging_readable(conn: sqlite3.Connection) -> None:
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise FactSetReviewStagingDatabaseRejectedError(str(exc)) from exc


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidFactSetReviewRequestError("limit must be a positive integer")
    if limit > MAX_REVIEW_LIMIT:
        raise InvalidFactSetReviewRequestError(f"limit must not exceed {MAX_REVIEW_LIMIT}")


def _validate_history_window(*, history_limit: int, history_offset: int) -> None:
    if isinstance(history_limit, bool) or not isinstance(history_limit, int) or history_limit < 1:
        raise InvalidFactSetReviewRequestError("history_limit must be a positive integer")
    if history_limit > MAX_HISTORY_LIMIT:
        raise InvalidFactSetReviewRequestError(f"history_limit must not exceed {MAX_HISTORY_LIMIT}")
    if (
        isinstance(history_offset, bool)
        or not isinstance(history_offset, int)
        or history_offset < 0
        or history_offset > SQLITE_MAX_INTEGER
    ):
        raise InvalidFactSetReviewRequestError(
            f"history_offset must be between 0 and {SQLITE_MAX_INTEGER}"
        )


def _resolve_history_anchor(
    *,
    active: FactSetReviewVersion | None,
    requested_anchor: int | None,
) -> int:
    if requested_anchor is not None and (
        isinstance(requested_anchor, bool)
        or not isinstance(requested_anchor, int)
        or requested_anchor < 1
        or requested_anchor > SQLITE_MAX_INTEGER
    ):
        raise InvalidFactSetReviewRequestError(
            "history_anchor_version must be a positive integer when supplied"
        )
    if active is None:
        if requested_anchor is not None:
            raise InvalidFactSetReviewRequestError(
                "history_anchor_version cannot be supplied when no fact set exists"
            )
        return 0
    anchor = active.fact_set_version if requested_anchor is None else requested_anchor
    if anchor > active.fact_set_version:
        raise InvalidFactSetReviewRequestError(
            "history_anchor_version cannot exceed the current active fact-set version"
        )
    return anchor


def _row_dict(columns: list[str], row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(zip(columns, row, strict=True))


def _receipt_summary(
    row: dict[str, Any],
    fact_set_count: int,
    active: FactSetReviewVersion | None,
) -> FactSetReviewReceipt:
    canonical_amount = row["net_paid_amount_canonical_text"]
    payer = row["payer_participant_public_id"]
    if canonical_amount is None or payer is None:
        raise FactSetReviewIntegrityError(
            "The conversion-created receipt is missing canonical amount or payer identity"
        )
    return FactSetReviewReceipt(
        receipt_public_id=str(row["receipt_public_id"]),
        merchant=str(row["merchant"]),
        receipt_datetime=(
            None if row["receipt_datetime"] is None else str(row["receipt_datetime"])
        ),
        currency=str(row["currency"]),
        net_paid_amount_canonical_text=str(canonical_amount),
        receipt_status=str(row["receipt_status"]),
        receipt_transaction_id_is_set=row["transaction_id"] is not None,
        receipt_group_binding_count=int(row["receipt_group_binding_count"]),
        payer_participant_public_id=str(payer),
        conversion_command_public_id=str(row["conversion_command_public_id"]),
        conversion_result_hash=str(row["conversion_result_hash"]),
        fact_set_count=fact_set_count,
        active_fact_set_public_id=(None if active is None else active.fact_set_public_id),
        active_fact_set_version=(None if active is None else active.fact_set_version),
        active_fact_set_result_hash=(None if active is None else active.fact_set_result_hash),
    )


def _load_membership(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    payer_participant_public_id: str,
) -> tuple[FactSetReviewMembership, ...]:
    rows = conn.execute(
        """
        SELECT p.public_id AS participant_public_id, p.display_name,
               rp.role, rp.is_included
        FROM receipt_participants AS rp
        LEFT JOIN participants AS p ON p.id = rp.participant_id
        WHERE rp.receipt_id = ?
        ORDER BY p.public_id ASC, rp.public_id ASC
        """,
        (receipt_id,),
    ).fetchall()
    if not rows:
        raise FactSetReviewIntegrityError("The conversion-created receipt has no membership rows")

    membership: list[FactSetReviewMembership] = []
    payer_rows = 0
    seen: set[str] = set()
    for row in rows:
        if row["participant_public_id"] is None:
            raise FactSetReviewIntegrityError(
                "A receipt membership row references a missing participant"
            )
        public_id = str(row["participant_public_id"])
        if public_id in seen:
            raise FactSetReviewIntegrityError(
                f"Participant {public_id!r} appears more than once in receipt membership"
            )
        seen.add(public_id)
        included = row["is_included"]
        if included not in (0, 1):
            raise FactSetReviewIntegrityError(
                f"Participant {public_id!r} has an invalid is_included value"
            )
        role = str(row["role"])
        if public_id == payer_participant_public_id:
            payer_rows += 1
            if role != "payer":
                raise FactSetReviewIntegrityError(
                    "The payer participant does not carry the required payer role"
                )
        membership.append(
            FactSetReviewMembership(
                participant_public_id=public_id,
                display_name=str(row["display_name"]),
                role=role,
                is_included=bool(included),
            )
        )
    if payer_rows != 1:
        raise FactSetReviewIntegrityError(
            f"Expected exactly one payer membership row, found {payer_rows}"
        )
    return tuple(membership)


def _load_fact_set_summary(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    receipt_public_id: str,
    conversion_command_public_id: str,
    conversion_result_hash: str,
) -> tuple[int, FactSetReviewVersion | None]:
    fact_set_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocation_fact_sets WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()[0]
    )
    active_rows = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? AND superseded_by_fact_set_public_id IS NULL "
        "ORDER BY version ASC, fact_set_public_id ASC",
        (receipt_id,),
    ).fetchall()
    if fact_set_count == 0:
        if active_rows:
            raise FactSetReviewIntegrityError("Fact-set count and active registry state disagree")
        return 0, None
    if len(active_rows) != 1:
        raise FactSetReviewIntegrityError(
            f"Expected exactly one active fact-set version, found {len(active_rows)}"
        )
    active = _version_from_row(
        conn,
        active_rows[0],
        receipt_public_id=receipt_public_id,
    )
    if (
        active.fact_set_version != fact_set_count
        or active.conversion_command_public_id != conversion_command_public_id
        or active.expected_conversion_result_hash != conversion_result_hash
    ):
        raise FactSetReviewIntegrityError(
            "The active fact set contradicts its count or receipt conversion binding"
        )
    final_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocation_fact_sets WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()[0]
    )
    final_active_rows = conn.execute(
        "SELECT fact_set_public_id, version, fact_set_result_hash "
        "FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? AND superseded_by_fact_set_public_id IS NULL",
        (receipt_id,),
    ).fetchall()
    if (
        final_count != fact_set_count
        or len(final_active_rows) != 1
        or str(final_active_rows[0]["fact_set_public_id"]) != active.fact_set_public_id
        or int(final_active_rows[0]["version"]) != active.fact_set_version
        or str(final_active_rows[0]["fact_set_result_hash"]) != active.fact_set_result_hash
    ):
        raise FactSetReviewIntegrityError(
            "Fact-set state changed during SELECT-only review; retry from a fresh snapshot"
        )
    return fact_set_count, active


def _load_fact_set_history_page(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    receipt_public_id: str,
    history_anchor_version: int,
    history_limit: int,
    history_offset: int,
) -> tuple[FactSetReviewVersion, ...]:
    rows = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? AND version <= ? "
        "ORDER BY version DESC, fact_set_public_id DESC "
        "LIMIT ? OFFSET ?",
        (receipt_id, history_anchor_version, history_limit, history_offset),
    ).fetchall()
    return tuple(_version_from_row(conn, row, receipt_public_id=receipt_public_id) for row in rows)


def _version_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    receipt_public_id: str,
) -> FactSetReviewVersion:
    try:
        verify_receipt_item_allocation_fact_set_for_review(
            conn,
            str(row["fact_set_public_id"]),
        )
    except ReceiptItemAllocationFactsError as exc:
        raise FactSetReviewIntegrityError(
            "Persisted fact-set state failed full service-depth review verification"
        ) from exc

    payload_text = str(row["canonical_fact_set_payload"])
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, RecursionError, MemoryError) as exc:
        raise FactSetReviewIntegrityError("Canonical fact-set payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FactSetReviewIntegrityError("Canonical fact-set payload is not a JSON object")
    if payload.get("receipt_public_id") != receipt_public_id:
        raise FactSetReviewIntegrityError(
            "Canonical fact-set payload contradicts the receipt identity"
        )
    if payload.get("schema_version") != "v1":
        raise FactSetReviewIntegrityError("Canonical fact-set payload has an unsupported version")
    try:
        canonical_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    except (RecursionError, MemoryError, UnicodeError) as exc:
        raise FactSetReviewIntegrityError(
            "Canonical fact-set payload cannot be safely verified"
        ) from exc
    if canonical_payload != payload_text:
        raise FactSetReviewIntegrityError("Canonical fact-set payload is not byte-canonical")
    if payload_hash != str(row["fact_set_input_hash"]):
        raise FactSetReviewIntegrityError(
            "Canonical fact-set payload does not match its registered input hash"
        )

    items = payload.get("items")
    allocations = payload.get("allocations")
    adjustments = payload.get("adjustments")
    if (
        not isinstance(items, list)
        or not isinstance(allocations, list)
        or not isinstance(adjustments, list)
    ):
        raise FactSetReviewIntegrityError("Canonical fact-set payload collections are malformed")
    try:
        expected_allocation_count = sum(
            len(allocation["participants"]) for allocation in allocations
        )
    except (KeyError, TypeError) as exc:
        raise FactSetReviewIntegrityError(
            "Canonical fact-set allocation material is malformed"
        ) from exc

    fact_set_public_id = str(row["fact_set_public_id"])
    item_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_items WHERE fact_set_id = ?",
            (fact_set_public_id,),
        ).fetchone()[0]
    )
    allocation_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocation_facts WHERE fact_set_id = ?",
            (fact_set_public_id,),
        ).fetchone()[0]
    )
    adjustment_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_adjustments WHERE fact_set_id = ?",
            (fact_set_public_id,),
        ).fetchone()[0]
    )
    if (
        item_count != len(items)
        or allocation_count != expected_allocation_count
        or adjustment_count != len(adjustments)
    ):
        raise FactSetReviewIntegrityError("Fact-set row counts do not match the canonical payload")

    return FactSetReviewVersion(
        command_public_id=str(row["command_public_id"]),
        fact_set_public_id=fact_set_public_id,
        fact_set_version=int(row["version"]),
        conversion_command_public_id=str(row["conversion_command_public_id"]),
        expected_conversion_result_hash=str(row["expected_conversion_result_hash"]),
        supersedes_fact_set_public_id=(
            None
            if row["supersedes_fact_set_public_id"] is None
            else str(row["supersedes_fact_set_public_id"])
        ),
        superseded_by_fact_set_public_id=(
            None
            if row["superseded_by_fact_set_public_id"] is None
            else str(row["superseded_by_fact_set_public_id"])
        ),
        command_material_hash=str(row["command_material_hash"]),
        fact_set_input_hash=str(row["fact_set_input_hash"]),
        fact_set_result_hash=str(row["fact_set_result_hash"]),
        canonical_fact_set_payload=payload_text,
        actor_type=str(row["actor_type"]),
        authenticated_actor_id=str(row["authenticated_actor_id"]),
        channel=str(row["channel"]),
        reason=None if row["reason"] is None else str(row["reason"]),
        audit_event_public_id=str(row["audit_event_public_id"]),
        schema_version=str(row["schema_version"]),
        created_at=str(row["created_at"]),
        item_count=item_count,
        allocation_count=allocation_count,
        adjustment_count=adjustment_count,
    )


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_REVIEW_LIMIT",
    "FACT_SET_REVIEW_LABEL",
    "MAX_HISTORY_LIMIT",
    "MAX_REVIEW_LIMIT",
    "FactSetReviewDetail",
    "FactSetReviewIntegrityError",
    "FactSetReviewMembership",
    "FactSetReviewReceipt",
    "FactSetReviewReceiptNotFoundError",
    "FactSetReviewStagingDatabaseRejectedError",
    "FactSetReviewVersion",
    "InvalidFactSetReviewRequestError",
    "ReceiptItemAllocationFactsReviewError",
    "UnsupportedFactSetReviewReceiptError",
    "get_fact_set_review_detail",
    "list_fact_set_review_receipts",
]
