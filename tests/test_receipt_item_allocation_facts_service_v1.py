"""IAF.2 guarded receipt item & allocation facts persistence service tests.

Covers the applicable design Section 18 matrix of
``docs/design/receipt_item_allocation_facts_boundary_v1.md`` for the
create-only IAF.2 slice: happy paths (18.1), fail-closed inputs (18.2),
persistence and verification (18.3), replay and concurrency (18.4),
failure injection (18.5), and boundary non-effects (18.6). Correction and
supersession are covered separately by the IAF.3 suite; readiness remains
a later slice.

Fixtures are built through public boundaries (B1 ingestion → confirmation
→ B4.1 conversion → IAF command); direct SQL appears only in clearly
marked schema-backstop and forged-drift fixtures per the established
Section 8 preamble pattern.  Only disposable staging databases under
``tmp_path`` are used; ``database/finance.db`` and seed data are never
touched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import finance_core.parser_proposals.receipt_item_allocation_facts as iaf_module
from finance_core.financial_audit import (
    AuditChainTransactionError,
    AuditVerificationError,
    verify_financial_audit_chain,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE,
    RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_PAYLOAD_FIELDS,
    AmbiguousItemAllocationError,
    IncompleteItemFactsError,
    InvalidItemFactsCommandError,
    InvalidItemFactsMoneyError,
    ItemFactsCallerOwnedTransactionError,
    ItemFactSetAlreadyExistsError,
    ItemFactsEvidenceLineageError,
    ItemFactsForeignKeysDisabledError,
    ItemFactsIdempotencyConflictError,
    ItemFactsPersistenceError,
    ItemFactsReceiptNotFoundError,
    ItemFactsReconciliationError,
    ItemFactsStagingDatabaseRejectedError,
    ReceiptItemAllocationFactsCommand,
    StaleItemFactsReceiptBindingError,
    UnauthorizedItemFactsActorError,
    UnsupportedAllocationRuleError,
    UnsupportedItemFactsReceiptProvenanceError,
    derive_adjustment_public_id,
    derive_allocation_public_id,
    derive_fact_set_public_id,
    derive_item_public_id,
    persist_receipt_item_allocation_facts,
)
from finance_core.sqlite_connection import ForeignKeysDisabledError
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_receipt_facts_conversion_v1 import (
    attachment_content,
    convert,
    count_diff,
    entries,
    evidence_rows,
    participant_id,
    seed_confirmed_receipt_proposal,
    seed_people,
    table_counts,
)
from tests.test_receipt_facts_conversion_v1 import (
    command as b41_command,
)
from tests.test_receipt_ocr_proposal_ingestion_v1 import _hash

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclasses.dataclass(frozen=True)
class ConvertedReceipt:
    """One receipt created entirely through the guarded B4.1 boundary."""

    receipt_public_id: str
    receipt_id: int
    conversion_command_public_id: str
    conversion_result_hash: str
    proposal_content_hash: str
    attachment_content_hash: str


def setup_receipt(
    conn: sqlite3.Connection,
    tmp_path: Path,
    suffix: str = "iaf",
    *,
    seed: bool = True,
    membership: list[dict[str, Any]] | None = None,
) -> ConvertedReceipt:
    """B1 → confirmation → B4.1 conversion, all through public boundaries.

    The seeded receipt is COLD STORAGE, 2026-07-20, SGD 12.34 net paid.
    """
    if seed:
        seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, suffix)
    if membership is None:
        membership = entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 0))
    result = convert(conn, b41_command(suffix, public_id, expected, participants=membership))
    return ConvertedReceipt(
        receipt_public_id=result.receipt_public_id,
        receipt_id=result.receipt_id,
        conversion_command_public_id=f"rpfc_{suffix}",
        conversion_result_hash=result.conversion_result_hash,
        proposal_content_hash=result.proposal_content_hash,
        attachment_content_hash=_hash(attachment_content(suffix)),
    )


def default_items() -> list[dict[str, Any]]:
    return [
        {
            "line_number": 1,
            "item_name": "Chicken Rice",
            "line_amount": "5.00",
            "currency": "SGD",
        },
        {
            "line_number": 2,
            "item_name": "Kopi",
            "quantity": "2",
            "unit_price": "3.11",
            "line_amount": "6.22",
            "currency": "SGD",
        },
    ]


def default_allocations() -> list[dict[str, Any]]:
    return [
        {
            "line_number": 1,
            "allocation_method": "manual",
            "participants": [
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "2.50",
                    "currency": "SGD",
                },
                {
                    "participant_public_id": "person_alice",
                    "share_amount": "2.50",
                    "currency": "SGD",
                },
            ],
        },
        {
            "line_number": 2,
            "allocation_method": "equal_amount",
            "participants": [
                {"participant_public_id": "person_owner"},
                {"participant_public_id": "person_alice"},
            ],
        },
    ]


def default_adjustments() -> list[dict[str, Any]]:
    return [
        {
            "adjustment_index": 1,
            "adjustment_type": "service_charge",
            "amount": "1.12",
            "currency": "SGD",
            "direction": "add",
            "allocation_method": "proportional_by_item_amount",
            "description": "10% service charge",
        }
    ]


def iaf_command(
    suffix: str, ctx: ConvertedReceipt, **overrides: Any
) -> ReceiptItemAllocationFactsCommand:
    fields: dict[str, Any] = {
        "command_public_id": f"riaf_{suffix}",
        "receipt_public_id": ctx.receipt_public_id,
        "expected_conversion_command_public_id": ctx.conversion_command_public_id,
        "expected_conversion_result_hash": ctx.conversion_result_hash,
        "expected_current_fact_set": "none",
        "items": default_items(),
        "allocations": default_allocations(),
        "adjustments": default_adjustments(),
        "authenticated_actor_id": "owner",
        "channel": "cli",
        "actor_type": "human",
        "reason": None,
    }
    fields.update(overrides)
    return ReceiptItemAllocationFactsCommand(**fields)


def persist(conn: sqlite3.Connection, cmd: ReceiptItemAllocationFactsCommand) -> Any:
    return persist_receipt_item_allocation_facts(conn, cmd)


def expected_fact_set_diff(items: int, allocations: int, adjustments: int) -> dict[str, int]:
    """The only writes a successful persistence may produce (Section 18.6)."""
    diff = {
        "receipt_item_allocation_fact_sets": 1,
        "receipt_items": items,
        "receipt_item_allocation_facts": allocations,
        "financial_audit_events": 1,
    }
    if adjustments:
        diff["receipt_adjustments"] = adjustments
    return diff


_RECEIPT_STATE_TABLES = (
    "receipts",
    "receipt_participants",
    "receipt_proposal_conversions",
)


def receipt_state(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Byte-identity snapshot of the B4.1 receipt fact and registry tables."""
    return {
        table: [
            dict(row)
            for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in _RECEIPT_STATE_TABLES
    }


def assert_rejected(
    conn: sqlite3.Connection,
    cmd: ReceiptItemAllocationFactsCommand,
    error_cls: type[Exception],
) -> Exception:
    """Fail-closed assertion: typed error, zero rows, evidence untouched."""
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)
    with pytest.raises(error_cls) as excinfo:
        persist_receipt_item_allocation_facts(conn, cmd)
    assert not conn.in_transaction
    assert count_diff(before_counts, table_counts(conn)) == {}
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence
    return excinfo.value


def result_fields(result: Any) -> dict[str, Any]:
    return dataclasses.asdict(result)


def event_payload_value(event_payload_json: str) -> dict[str, Any]:
    envelope = json.loads(event_payload_json)
    assert set(envelope) == {"contract_version", "value"}
    return envelope["value"]


# ---------------------------------------------------------------------------
# 18.1 Happy paths
# ---------------------------------------------------------------------------


def test_happy_path_persists_first_fact_set(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "happy")
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)
    cmd = iaf_command("happy", ctx, reason="itemised split")

    result = persist(conn, cmd)

    assert result.idempotent is False
    assert not conn.in_transaction
    assert result.command_public_id == "riaf_happy"
    assert result.receipt_public_id == ctx.receipt_public_id
    assert result.receipt_id == ctx.receipt_id
    assert result.fact_set_public_id == derive_fact_set_public_id("riaf_happy")
    assert result.fact_set_version == 1
    assert result.conversion_command_public_id == ctx.conversion_command_public_id
    assert result.item_count == 2
    assert result.allocation_count == 4
    assert result.adjustment_count == 1

    # Independent Section 12.2 material hash recomputation: reason is
    # excluded; allocations sort participants by public ID; item and
    # adjustment order is semantic.
    material = {
        "schema_version": "v1",
        "command_public_id": "riaf_happy",
        "receipt_public_id": ctx.receipt_public_id,
        "expected_conversion_command_public_id": ctx.conversion_command_public_id,
        "expected_conversion_result_hash": ctx.conversion_result_hash,
        "expected_current_fact_set": "none",
        "items": [
            {
                "line_number": 1,
                "item_name": "Chicken Rice",
                "quantity": None,
                "unit_price": None,
                "line_amount": "5.00",
                "currency": "SGD",
            },
            {
                "line_number": 2,
                "item_name": "Kopi",
                "quantity": "2",
                "unit_price": "3.11",
                "line_amount": "6.22",
                "currency": "SGD",
            },
        ],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "manual",
                "participants": [
                    {
                        "participant_public_id": "person_alice",
                        "share_amount": "2.50",
                        "currency": "SGD",
                    },
                    {
                        "participant_public_id": "person_owner",
                        "share_amount": "2.50",
                        "currency": "SGD",
                    },
                ],
            },
            {
                "line_number": 2,
                "allocation_method": "equal_amount",
                "participants": [
                    {
                        "participant_public_id": "person_alice",
                        "share_amount": None,
                        "currency": None,
                    },
                    {
                        "participant_public_id": "person_owner",
                        "share_amount": None,
                        "currency": None,
                    },
                ],
            },
        ],
        "adjustments": [
            {
                "adjustment_index": 1,
                "adjustment_type": "service_charge",
                "amount": "1.12",
                "currency": "SGD",
                "direction": "add",
                "allocation_method": "proportional_by_item_amount",
                "description": "10% service charge",
                "participants": None,
            }
        ],
        "authenticated_actor_id": "owner",
        "actor_type": "human",
        "channel": "cli",
    }
    assert result.command_material_hash == _sha(_canonical_json(material))

    # Independent input-hash recomputation over the canonical payload.
    input_material = {
        "schema_version": "v1",
        "receipt_public_id": ctx.receipt_public_id,
        "currency": "SGD",
        "net_paid_amount": "12.34",
        "items": [
            {
                "line_number": 1,
                "item_name": "Chicken Rice",
                "quantity": None,
                "unit_price": None,
                "line_amount": "5.00",
            },
            {
                "line_number": 2,
                "item_name": "Kopi",
                "quantity": "2",
                "unit_price": "3.11",
                "line_amount": "6.22",
            },
        ],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "manual",
                "participants": [
                    {"participant_public_id": "person_alice", "share_amount": "2.50"},
                    {"participant_public_id": "person_owner", "share_amount": "2.50"},
                ],
            },
            {
                "line_number": 2,
                "allocation_method": "equal_amount",
                "participants": [
                    {"participant_public_id": "person_alice", "share_amount": None},
                    {"participant_public_id": "person_owner", "share_amount": None},
                ],
            },
        ],
        "adjustments": [
            {
                "adjustment_index": 1,
                "adjustment_type": "service_charge",
                "direction": "add",
                "amount": "1.12",
                "allocation_method": "proportional_by_item_amount",
                "description": "10% service charge",
                "participants": None,
            }
        ],
    }
    payload_text = _canonical_json(input_material)
    assert result.fact_set_input_hash == _sha(payload_text)

    fact_set_id = result.fact_set_public_id
    item_1 = derive_item_public_id(fact_set_id, 1)
    item_2 = derive_item_public_id(fact_set_id, 2)
    result_material = dict(input_material)
    result_material.update(
        {
            "fact_set_public_id": fact_set_id,
            "version": 1,
            "conversion_command_public_id": ctx.conversion_command_public_id,
            "command_material_hash": result.command_material_hash,
            "item_public_ids": [item_1, item_2],
            "allocation_public_ids": [
                derive_allocation_public_id(item_1, "person_alice"),
                derive_allocation_public_id(item_1, "person_owner"),
                derive_allocation_public_id(item_2, "person_alice"),
                derive_allocation_public_id(item_2, "person_owner"),
            ],
            "adjustment_public_ids": [derive_adjustment_public_id(fact_set_id, 1)],
        }
    )
    assert result.fact_set_result_hash == _sha(_canonical_json(result_material))

    # Registry row.
    registry = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE command_public_id = 'riaf_happy'"
    ).fetchone()
    assert registry["fact_set_public_id"] == fact_set_id
    assert registry["receipt_id"] == ctx.receipt_id
    assert registry["version"] == 1
    assert registry["conversion_command_public_id"] == ctx.conversion_command_public_id
    assert registry["expected_conversion_result_hash"] == ctx.conversion_result_hash
    assert registry["supersedes_fact_set_public_id"] is None
    assert registry["superseded_by_fact_set_public_id"] is None
    assert registry["command_material_hash"] == result.command_material_hash
    assert registry["fact_set_input_hash"] == result.fact_set_input_hash
    assert registry["fact_set_result_hash"] == result.fact_set_result_hash
    assert registry["canonical_fact_set_payload"] == payload_text
    assert registry["actor_type"] == "human"
    assert registry["authenticated_actor_id"] == "owner"
    assert registry["channel"] == "cli"
    assert registry["reason"] == "itemised split"
    assert registry["schema_version"] == "v1"
    assert registry["audit_event_public_id"] == result.audit_event_public_id

    # Item rows: canonical texts byte-exact, mirrors Decimal-equal.
    items = conn.execute(
        "SELECT * FROM receipt_items WHERE fact_set_id = ? ORDER BY line_number",
        (fact_set_id,),
    ).fetchall()
    assert len(items) == 2
    assert items[0]["public_id"] == item_1
    assert items[0]["item_name"] == "Chicken Rice"
    assert items[0]["line_amount_canonical_text"] == "5.00"
    assert items[0]["quantity_canonical_text"] is None
    assert items[0]["unit_price_canonical_text"] is None
    assert str(items[0]["line_amount"]) == "5.0" or items[0]["line_amount"] == 5.0
    assert items[1]["public_id"] == item_2
    assert items[1]["quantity_canonical_text"] == "2"
    assert items[1]["unit_price_canonical_text"] == "3.11"
    assert items[1]["line_amount_canonical_text"] == "6.22"
    assert items[1]["currency"] == "SGD"

    # Allocation fact rows: manual carries canonical shares; equal_amount
    # persists no per-participant amount (Section 7).
    allocations = conn.execute(
        "SELECT af.*, p.public_id AS pid FROM receipt_item_allocation_facts af "
        "JOIN participants p ON p.id = af.participant_id "
        "WHERE af.fact_set_id = ? ORDER BY af.receipt_item_id, p.public_id",
        (fact_set_id,),
    ).fetchall()
    assert len(allocations) == 4
    manual_rows = [row for row in allocations if row["allocation_method"] == "manual"]
    equal_rows = [row for row in allocations if row["allocation_method"] == "equal_amount"]
    assert {row["pid"] for row in manual_rows} == {"person_owner", "person_alice"}
    assert all(row["share_amount_canonical_text"] == "2.50" for row in manual_rows)
    assert {row["pid"] for row in equal_rows} == {"person_owner", "person_alice"}
    assert all(row["share_amount_canonical_text"] is None for row in equal_rows)
    assert all(row["share_amount"] is None for row in equal_rows)
    for row in allocations:
        expected_item = item_1 if row["allocation_method"] == "manual" else item_2
        assert row["allocation_public_id"] == derive_allocation_public_id(
            expected_item, str(row["pid"])
        )

    # Adjustment row.
    adjustment = conn.execute(
        "SELECT * FROM receipt_adjustments WHERE fact_set_id = ?", (fact_set_id,)
    ).fetchone()
    assert adjustment["public_id"] == derive_adjustment_public_id(fact_set_id, 1)
    assert adjustment["adjustment_type"] == "service_charge"
    assert adjustment["direction"] == "add"
    assert adjustment["allocation_method"] == "proportional_by_item_amount"
    assert adjustment["adjustment_index"] == 1
    assert adjustment["amount_canonical_text"] == "1.12"
    assert adjustment["description"] == "10% service charge"
    assert adjustment["currency"] == "SGD"

    # Exactly one audit event with the frozen Section 14.1 payload.
    events = conn.execute(
        "SELECT * FROM financial_audit_events WHERE causation_public_id = 'riaf_happy'"
    ).fetchall()
    assert len(events) == 1
    event = events[0]
    assert event["event_public_id"] == result.audit_event_public_id
    assert event["aggregate_type"] == "receipt"
    assert event["aggregate_public_id"] == ctx.receipt_public_id
    assert event["event_type"] == RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE
    assert event["actor_type"] == "human"
    assert event["actor_public_id"] == "owner"
    assert event["authorization_public_id"] == "riaf_happy"
    assert event["correlation_public_id"] == ctx.receipt_public_id
    assert event["sequence_number"] == 2  # conversion genesis + this event
    assert event["created_at"] == registry["created_at"]
    payload = event_payload_value(event["event_payload_json"])
    assert tuple(sorted(payload)) == RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_PAYLOAD_FIELDS
    assert payload == {
        "actor_type": "human",
        "adjustment_count": 1,
        "allocation_count": 4,
        "authenticated_actor_id": "owner",
        "channel": "cli",
        "command_material_hash": result.command_material_hash,
        "command_public_id": "riaf_happy",
        "conversion_command_public_id": ctx.conversion_command_public_id,
        "conversion_result_hash": ctx.conversion_result_hash,
        "fact_set_input_hash": result.fact_set_input_hash,
        "fact_set_public_id": fact_set_id,
        "fact_set_result_hash": result.fact_set_result_hash,
        "fact_set_version": 1,
        "item_count": 2,
        "receipt_public_id": ctx.receipt_public_id,
        "supersedes_fact_set_public_id": None,
    }
    references = json.loads(event["source_evidence_refs_json"])
    assert references == sorted(
        [
            f"receipt:{ctx.receipt_public_id}",
            f"conversion:{ctx.conversion_command_public_id}",
            f"conversion-result-hash:{ctx.conversion_result_hash}",
            f"attachment-content-hash:{ctx.attachment_content_hash}",
            f"proposal-content-hash:{ctx.proposal_content_hash}",
            f"fact-set:{fact_set_id}",
        ]
    )
    new_state = event_payload_value(event["new_state_json"])
    assert new_state == {
        "fact_set_status": "active",
        "receipt_public_id": ctx.receipt_public_id,
        "fact_set_public_id": fact_set_id,
        "fact_set_version": 1,
        "fact_set_input_hash": result.fact_set_input_hash,
        "fact_set_result_hash": result.fact_set_result_hash,
    }
    chain = verify_financial_audit_chain(
        conn, aggregate_type="receipt", aggregate_public_id=ctx.receipt_public_id
    )
    assert chain.valid and chain.event_count == 2

    # Section 18.6: only the fact-set writes happened; the receipt,
    # membership, conversion registry, and all evidence stay byte-identical.
    assert count_diff(before_counts, table_counts(conn)) == expected_fact_set_diff(2, 4, 1)
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence


def test_happy_path_single_owner_equal_and_manual_variants(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "single")
    items = [
        {"line_number": 1, "item_name": "Set Lunch", "line_amount": "10.00", "currency": "SGD"},
        {"line_number": 2, "item_name": "Dessert", "line_amount": "2.34", "currency": "SGD"},
    ]
    allocations = [
        {
            "line_number": 1,
            "allocation_method": "equal_amount",
            "participants": [{"participant_public_id": "person_owner"}],
        },
        {
            "line_number": 2,
            "allocation_method": "manual",
            "participants": [
                {"participant_public_id": "person_alice", "share_amount": "2.34", "currency": "SGD"}
            ],
        },
    ]
    result = persist(
        conn,
        iaf_command("single", ctx, items=items, allocations=allocations, adjustments=[]),
    )
    assert result.item_count == 2
    assert result.allocation_count == 2
    assert result.adjustment_count == 0
    rows = conn.execute(
        "SELECT allocation_method, share_amount_canonical_text "
        "FROM receipt_item_allocation_facts WHERE fact_set_id = ? "
        "ORDER BY allocation_method",
        (result.fact_set_public_id,),
    ).fetchall()
    # Both single-owner forms persist exactly as authored.
    assert [tuple(row) for row in rows] == [("equal_amount", None), ("manual", "2.34")]


def test_happy_path_payer_excluded_as_consumer(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Payer membership is_included=0 and payer in no allocation → succeeds."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(
        conn,
        tmp_path,
        "payerx",
        membership=entries(("person_owner", 0), ("person_alice", 1)),
    )
    items = [
        {"line_number": 1, "item_name": "Solo Meal", "line_amount": "12.34", "currency": "SGD"}
    ]
    allocations = [
        {
            "line_number": 1,
            "allocation_method": "equal_amount",
            "participants": [{"participant_public_id": "person_alice"}],
        }
    ]
    result = persist(
        conn,
        iaf_command("payerx", ctx, items=items, allocations=allocations, adjustments=[]),
    )
    assert result.allocation_count == 1
    # This receipt now has a fact set, so any further create command
    # fails on guard 12 first; the dedicated excluded-payer rejection is
    # test_excluded_payer_named_as_consumer_rejected.
    assert_rejected(
        conn,
        iaf_command(
            "payerx2",
            ctx,
            items=items,
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "equal_amount",
                    "participants": [{"participant_public_id": "person_owner"}],
                }
            ],
            adjustments=[],
        ),
        ItemFactSetAlreadyExistsError,
    )


def test_adjustment_description_500_boundary_accepted(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "desc500")
    description = "d" * 500
    adjustments = [dict(default_adjustments()[0], description=description)]
    result = persist(conn, iaf_command("desc500", ctx, adjustments=adjustments))
    row = conn.execute(
        "SELECT description FROM receipt_adjustments WHERE fact_set_id = ?",
        (result.fact_set_public_id,),
    ).fetchone()
    assert row["description"] == description
    payload = json.loads(
        conn.execute(
            "SELECT canonical_fact_set_payload FROM receipt_item_allocation_fact_sets "
            "WHERE command_public_id = 'riaf_desc500'"
        ).fetchone()[0]
    )
    # The description participates in the canonical payload and hashes.
    assert payload["adjustments"][0]["description"] == description


# ---------------------------------------------------------------------------
# 18.2 Fail-closed inputs: guard 1 command structure
# ---------------------------------------------------------------------------


def test_unknown_fields_rejected_at_every_level(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "unknown")
    base = iaf_command("unknown", ctx)
    # Command level, via the mapping constructor.
    mapping = dict(dataclasses.asdict(base))
    mapping["surprise_field"] = "x"
    with pytest.raises(InvalidItemFactsCommandError, match="Unknown fact-set command fields"):
        ReceiptItemAllocationFactsCommand.from_mapping(mapping)
    # Item level.
    items = default_items()
    items[0]["note"] = "free text"
    assert_rejected(conn, iaf_command("unknown", ctx, items=items), InvalidItemFactsCommandError)
    # Allocation level.
    allocations = default_allocations()
    allocations[0]["weight"] = 2
    assert_rejected(
        conn, iaf_command("unknown", ctx, allocations=allocations), InvalidItemFactsCommandError
    )
    # Allocation participant level.
    allocations = default_allocations()
    allocations[0]["participants"][0]["percentage"] = "50"
    assert_rejected(
        conn, iaf_command("unknown", ctx, allocations=allocations), InvalidItemFactsCommandError
    )
    # Adjustment level.
    adjustments = default_adjustments()
    adjustments[0]["percent"] = "10"
    assert_rejected(
        conn, iaf_command("unknown", ctx, adjustments=adjustments), InvalidItemFactsCommandError
    )
    # Manual adjustment participant level.
    adjustments = [
        {
            "adjustment_index": 1,
            "adjustment_type": "service_charge",
            "amount": "1.12",
            "currency": "SGD",
            "direction": "add",
            "allocation_method": "manual",
            "participants": [
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "1.12",
                    "currency": "SGD",
                    "why": "extra",
                }
            ],
        }
    ]
    assert_rejected(
        conn, iaf_command("unknown", ctx, adjustments=adjustments), InvalidItemFactsCommandError
    )


def test_missing_required_command_fields_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "missing")
    complete = dict(dataclasses.asdict(iaf_command("missing", ctx)))
    for field in (
        "command_public_id",
        "receipt_public_id",
        "expected_conversion_command_public_id",
        "expected_conversion_result_hash",
        "expected_current_fact_set",
        "items",
        "allocations",
        "adjustments",
        "authenticated_actor_id",
        "actor_type",
        "channel",
        "schema_version",
    ):
        broken = dict(complete)
        del broken[field]
        with pytest.raises(InvalidItemFactsCommandError, match="missing required fields"):
            ReceiptItemAllocationFactsCommand.from_mapping(broken)


def test_malformed_command_identity_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "badid")
    cases: list[dict[str, Any]] = [
        {"command_public_id": "iaf_no_prefix"},
        {"command_public_id": "riaf_"},
        {"command_public_id": "riaf_white space"},
        {"command_public_id": 42},
        {"receipt_public_id": ""},
        {"receipt_public_id": " rcpt_padded "},
        {"expected_conversion_command_public_id": "conv_wrong_prefix"},
        {"expected_conversion_result_hash": "ABC123"},
        {"expected_conversion_result_hash": "g" * 64},
        {"expected_current_fact_set": "any"},
        {"expected_current_fact_set": None},
        {"schema_version": "v2"},
    ]
    for overrides in cases:
        assert_rejected(conn, iaf_command("badid", ctx, **overrides), InvalidItemFactsCommandError)


def test_non_human_actor_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "actor")
    for overrides in (
        {"actor_type": "agent"},
        {"actor_type": "ai"},
        {"actor_type": "system"},
        {"authenticated_actor_id": ""},
        {"authenticated_actor_id": "  "},
        {"authenticated_actor_id": " owner "},
        {"authenticated_actor_id": None},
    ):
        assert_rejected(
            conn, iaf_command("actor", ctx, **overrides), UnauthorizedItemFactsActorError
        )


def test_channel_and_reason_bounds_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "chan")
    for overrides in (
        {"channel": ""},
        {"channel": " cli "},
        {"channel": None},
        {"reason": "   "},
        {"reason": "r" * 501},
        {"reason": 5},
    ):
        assert_rejected(conn, iaf_command("chan", ctx, **overrides), InvalidItemFactsCommandError)


def test_item_structure_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "itemstruct")
    # Empty items: no implicit "whole receipt" default.
    assert_rejected(
        conn,
        iaf_command("itemstruct", ctx, items=[], allocations=[]),
        IncompleteItemFactsError,
    )
    # Non-contiguous, duplicate, and non-positive line numbers.
    for lines in ((1, 3), (1, 1), (0, 1), (2, 3)):
        items = default_items()
        items[0]["line_number"], items[1]["line_number"] = lines
        assert_rejected(
            conn, iaf_command("itemstruct", ctx, items=items), InvalidItemFactsCommandError
        )
    # Missing required item fields fail as incomplete facts.
    items = default_items()
    del items[0]["line_amount"]
    assert_rejected(conn, iaf_command("itemstruct", ctx, items=items), IncompleteItemFactsError)
    items = default_items()
    items[0]["item_name"] = "   "
    assert_rejected(conn, iaf_command("itemstruct", ctx, items=items), IncompleteItemFactsError)
    # Oversize item name is malformed, not silently truncated.
    items = default_items()
    items[0]["item_name"] = "n" * 201
    assert_rejected(conn, iaf_command("itemstruct", ctx, items=items), InvalidItemFactsCommandError)
    # Fractional quantity is deferred by IA-D12.
    items = default_items()
    items[1]["quantity"] = "2.5"
    assert_rejected(conn, iaf_command("itemstruct", ctx, items=items), InvalidItemFactsCommandError)
    items = default_items()
    items[1]["quantity"] = 0
    assert_rejected(conn, iaf_command("itemstruct", ctx, items=items), InvalidItemFactsCommandError)


def test_allocation_structure_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "allocstruct")
    # An item without any allocation entry is incomplete (IA-D3).
    assert_rejected(
        conn,
        iaf_command("allocstruct", ctx, allocations=default_allocations()[:1]),
        IncompleteItemFactsError,
    )
    # An allocation referencing an unknown item line is malformed.
    allocations = default_allocations()
    allocations[1]["line_number"] = 9
    assert_rejected(
        conn, iaf_command("allocstruct", ctx, allocations=allocations), InvalidItemFactsCommandError
    )
    # Duplicate entries for one line — same or mixed methods — are rejected.
    allocations = default_allocations() + [default_allocations()[0]]
    assert_rejected(
        conn, iaf_command("allocstruct", ctx, allocations=allocations), InvalidItemFactsCommandError
    )
    mixed = default_allocations() + [
        {
            "line_number": 1,
            "allocation_method": "equal_amount",
            "participants": [{"participant_public_id": "person_owner"}],
        }
    ]
    assert_rejected(
        conn, iaf_command("allocstruct", ctx, allocations=mixed), InvalidItemFactsCommandError
    )
    # An empty participants list is incomplete, never "everyone".
    allocations = default_allocations()
    allocations[1]["participants"] = []
    assert_rejected(
        conn, iaf_command("allocstruct", ctx, allocations=allocations), IncompleteItemFactsError
    )


def test_adjustment_structure_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "adjstruct")
    # adjustments=None (absence) is never defaulted to "no adjustments".
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=None), InvalidItemFactsCommandError
    )
    # Non-contiguous or duplicate adjustment indexes.
    two = [
        dict(default_adjustments()[0], adjustment_index=1, amount="0.56"),
        dict(default_adjustments()[0], adjustment_index=3, amount="0.56"),
    ]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=two), InvalidItemFactsCommandError
    )
    dup = [
        dict(default_adjustments()[0], adjustment_index=1, amount="0.56"),
        dict(default_adjustments()[0], adjustment_index=1, amount="0.56"),
    ]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=dup), InvalidItemFactsCommandError
    )
    # Missing required adjustment fields.
    broken = [dict(default_adjustments()[0])]
    del broken[0]["direction"]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=broken), IncompleteItemFactsError
    )
    # Whitespace-only description is malformed, not silently dropped.
    blank = [dict(default_adjustments()[0], description="   ")]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=blank), InvalidItemFactsCommandError
    )
    # Description over the approved 500 bound.
    long = [dict(default_adjustments()[0], description="d" * 501)]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=long), InvalidItemFactsCommandError
    )
    # Participants on a non-manual adjustment method are contradictory.
    nonmanual = [
        dict(
            default_adjustments()[0],
            participants=[
                {"participant_public_id": "person_owner", "share_amount": "1.12", "currency": "SGD"}
            ],
        )
    ]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=nonmanual), InvalidItemFactsCommandError
    )
    # A manual adjustment without explicit shares is incomplete.
    manual = [dict(default_adjustments()[0], allocation_method="manual")]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=manual), IncompleteItemFactsError
    )
    # A manual participant entry without share_amount is incomplete.
    manual_missing = [
        dict(
            default_adjustments()[0],
            allocation_method="manual",
            participants=[{"participant_public_id": "person_owner"}],
        )
    ]
    assert_rejected(
        conn, iaf_command("adjstruct", ctx, adjustments=manual_missing), IncompleteItemFactsError
    )


# ---------------------------------------------------------------------------
# 18.2 Money Contract, mirrors, and reconciliation
# ---------------------------------------------------------------------------


def test_non_string_monetary_values_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Floats, ints, bools, and None can never enter canonical hashing."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "floaty")
    for bad in (5.0, 5, True, None, ""):
        items = default_items()
        items[0]["line_amount"] = bad
        error_cls: type[Exception] = InvalidItemFactsMoneyError
        if bad is None:
            error_cls = IncompleteItemFactsError  # missing required field
        assert_rejected(conn, iaf_command("floaty", ctx, items=items), error_cls)
    allocations = default_allocations()
    allocations[0]["participants"][0]["share_amount"] = 2.50
    assert_rejected(
        conn, iaf_command("floaty", ctx, allocations=allocations), InvalidItemFactsMoneyError
    )
    adjustments = [dict(default_adjustments()[0], amount=1.12)]
    assert_rejected(
        conn, iaf_command("floaty", ctx, adjustments=adjustments), InvalidItemFactsMoneyError
    )


def test_money_scale_currency_and_sign_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "scale")
    # Wrong minor-unit scale for the declared currency.
    items = default_items()
    items[0]["line_amount"] = "5.001"
    assert_rejected(conn, iaf_command("scale", ctx, items=items), InvalidItemFactsMoneyError)
    # JPY forbids decimals: the declared currency is validated first, so
    # the Money error fires even though the receipt is SGD.
    items = default_items()
    items[0]["line_amount"] = "100.5"
    items[0]["currency"] = "JPY"
    assert_rejected(conn, iaf_command("scale", ctx, items=items), InvalidItemFactsMoneyError)
    # Unsupported currency code.
    items = default_items()
    items[0]["currency"] = "XXX"
    assert_rejected(conn, iaf_command("scale", ctx, items=items), InvalidItemFactsMoneyError)
    # Malformed string forms the shared Money Contract rejects.
    for bad in ("5,00", "1e2", "NaN", "abc", "$5.00"):
        items = default_items()
        items[0]["line_amount"] = bad
        assert_rejected(conn, iaf_command("scale", ctx, items=items), InvalidItemFactsMoneyError)
    # Zero and negative amounts are never facts (IA-D12).
    for bad in ("0.00", "-5.00"):
        items = default_items()
        items[0]["line_amount"] = bad
        assert_rejected(conn, iaf_command("scale", ctx, items=items), InvalidItemFactsMoneyError)
    # Negative and malformed adjustment amounts.
    for bad in ("-1.12", "0.00", "1.123"):
        adjustments = [dict(default_adjustments()[0], amount=bad)]
        assert_rejected(
            conn, iaf_command("scale", ctx, adjustments=adjustments), InvalidItemFactsMoneyError
        )


def test_lenient_money_forms_persist_canonical_text(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Money-Contract-accepted forms (e.g. "5.0") persist as the byte-exact
    canonical minor-unit text, exactly like the shared B4.1 contract."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "lenient")
    items = default_items()
    items[0]["line_amount"] = "5.0"
    result = persist(conn, iaf_command("lenient", ctx, items=items))
    row = conn.execute(
        "SELECT line_amount_canonical_text FROM receipt_items "
        "WHERE fact_set_id = ? AND line_number = 1",
        (result.fact_set_public_id,),
    ).fetchone()
    assert row["line_amount_canonical_text"] == "5.00"
    # The canonical payload carries the canonical text, never the raw form.
    payload = json.loads(
        conn.execute(
            "SELECT canonical_fact_set_payload FROM receipt_item_allocation_fact_sets "
            "WHERE command_public_id = 'riaf_lenient'"
        ).fetchone()[0]
    )
    assert payload["items"][0]["line_amount"] == "5.00"


def test_cross_currency_facts_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A valid foreign-currency amount is incomplete, not a Money error."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "xcur")
    items = default_items()
    items[0]["currency"] = "USD"  # "5.00" is a valid USD amount
    assert_rejected(conn, iaf_command("xcur", ctx, items=items), IncompleteItemFactsError)
    allocations = default_allocations()
    allocations[0]["participants"][0]["currency"] = "USD"
    assert_rejected(
        conn, iaf_command("xcur", ctx, allocations=allocations), IncompleteItemFactsError
    )
    adjustments = [dict(default_adjustments()[0], currency="USD")]
    assert_rejected(
        conn, iaf_command("xcur", ctx, adjustments=adjustments), IncompleteItemFactsError
    )


def test_zero_manual_share_is_contradiction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "zeroshare")
    allocations = default_allocations()
    allocations[0]["participants"][0]["share_amount"] = "0.00"
    allocations[0]["participants"][1]["share_amount"] = "5.00"
    assert_rejected(
        conn, iaf_command("zeroshare", ctx, allocations=allocations), AmbiguousItemAllocationError
    )


def test_manual_shares_must_sum_exactly(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "sharesum")
    allocations = default_allocations()
    allocations[0]["participants"][0]["share_amount"] = "2.49"  # 2.49 + 2.50 != 5.00
    assert_rejected(
        conn, iaf_command("sharesum", ctx, allocations=allocations), ItemFactsReconciliationError
    )
    # Over-allocation fails identically: zero tolerance in both directions.
    allocations = default_allocations()
    allocations[0]["participants"][0]["share_amount"] = "2.51"
    assert_rejected(
        conn, iaf_command("sharesum", ctx, allocations=allocations), ItemFactsReconciliationError
    )


def test_quantity_unit_price_line_reconciliation(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "qtyline")
    items = default_items()
    items[1]["line_amount"] = "6.23"  # 2 x 3.11 = 6.22
    assert_rejected(conn, iaf_command("qtyline", ctx, items=items), ItemFactsReconciliationError)


def test_fact_set_total_reconciliation_zero_tolerance(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "total")
    # One cent under and one cent over both fail closed (IA-D6).
    for amount in ("1.11", "1.13"):
        adjustments = [dict(default_adjustments()[0], amount=amount)]
        assert_rejected(
            conn, iaf_command("total", ctx, adjustments=adjustments), ItemFactsReconciliationError
        )
    # Missing adjustments entirely: items alone do not reach net paid.
    assert_rejected(conn, iaf_command("total", ctx, adjustments=[]), ItemFactsReconciliationError)
    # Subtract direction moves the wrong way and fails.
    adjustments = [dict(default_adjustments()[0], direction="subtract")]
    assert_rejected(
        conn, iaf_command("total", ctx, adjustments=adjustments), ItemFactsReconciliationError
    )


def test_manual_adjustment_shares_must_sum_exactly(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "adjsum")
    adjustments = [
        dict(
            default_adjustments()[0],
            allocation_method="manual",
            participants=[
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "0.56",
                    "currency": "SGD",
                },
                {
                    "participant_public_id": "person_alice",
                    "share_amount": "0.57",  # 0.56 + 0.57 != 1.12
                    "currency": "SGD",
                },
            ],
        )
    ]
    assert_rejected(
        conn, iaf_command("adjsum", ctx, adjustments=adjustments), ItemFactsReconciliationError
    )


def test_unsupported_vocabulary_rejected_never_mapped(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "vocab")
    # Item allocation methods outside IA-D7.
    for method in ("percentage", "equal_quantity", "payer_only", "proportional"):
        allocations = default_allocations()
        allocations[1]["allocation_method"] = method
        allocations[1]["participants"] = [{"participant_public_id": "person_owner"}]
        assert_rejected(
            conn, iaf_command("vocab", ctx, allocations=allocations), UnsupportedAllocationRuleError
        )
    # Adjustment types outside the approved migration 002 vocabulary.
    adjustments = [dict(default_adjustments()[0], adjustment_type="tip")]
    assert_rejected(
        conn, iaf_command("vocab", ctx, adjustments=adjustments), UnsupportedAllocationRuleError
    )
    # 'informational' direction cannot participate in reconciliation.
    for direction in ("informational", "neutral"):
        adjustments = [dict(default_adjustments()[0], direction=direction)]
        assert_rejected(
            conn, iaf_command("vocab", ctx, adjustments=adjustments), UnsupportedAllocationRuleError
        )
    # Adjustment allocation methods outside IA-D7b.
    for method in ("proportional_by_net_amount", "equal_amount", "percentage"):
        adjustments = [dict(default_adjustments()[0], allocation_method=method)]
        assert_rejected(
            conn, iaf_command("vocab", ctx, adjustments=adjustments), UnsupportedAllocationRuleError
        )


def test_equal_amount_with_prerounded_share_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A pre-rounded equal share would fabricate a monetary fact."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "preround")
    allocations = default_allocations()
    allocations[1]["participants"][0]["share_amount"] = "3.11"
    allocations[1]["participants"][0]["currency"] = "SGD"
    assert_rejected(
        conn, iaf_command("preround", ctx, allocations=allocations), InvalidItemFactsCommandError
    )


# ---------------------------------------------------------------------------
# 18.2 Participants: unknown, excluded, duplicate, contradictory
# ---------------------------------------------------------------------------


def test_unknown_participant_rejected_membership_never_edited(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "nopart")
    allocations = default_allocations()
    allocations[1]["participants"] = [{"participant_public_id": "person_stranger"}]
    assert_rejected(
        conn, iaf_command("nopart", ctx, allocations=allocations), AmbiguousItemAllocationError
    )
    # Even a real person without a membership row on this receipt fails:
    # person_carol exists globally but has no receipt membership.
    conn.execute(
        "INSERT INTO participants (public_id, display_name, is_self) VALUES "
        "('person_carol', 'Carol', 0)"
    )
    conn.commit()
    allocations = default_allocations()
    allocations[1]["participants"] = [{"participant_public_id": "person_carol"}]
    assert_rejected(
        conn, iaf_command("nopart", ctx, allocations=allocations), AmbiguousItemAllocationError
    )


def test_excluded_participant_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "excl")  # person_bob is excluded
    allocations = default_allocations()
    allocations[1]["participants"] = [{"participant_public_id": "person_bob"}]
    assert_rejected(
        conn, iaf_command("excl", ctx, allocations=allocations), AmbiguousItemAllocationError
    )
    # Excluded participants may not appear in manual adjustment shares either.
    adjustments = [
        dict(
            default_adjustments()[0],
            allocation_method="manual",
            participants=[
                {"participant_public_id": "person_bob", "share_amount": "1.12", "currency": "SGD"}
            ],
        )
    ]
    assert_rejected(
        conn, iaf_command("excl", ctx, adjustments=adjustments), AmbiguousItemAllocationError
    )


def test_excluded_payer_named_as_consumer_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(
        conn, tmp_path, "exclpayer", membership=entries(("person_owner", 0), ("person_alice", 1))
    )
    items = [
        {"line_number": 1, "item_name": "Solo Meal", "line_amount": "12.34", "currency": "SGD"}
    ]
    allocations = [
        {
            "line_number": 1,
            "allocation_method": "equal_amount",
            "participants": [{"participant_public_id": "person_owner"}],
        }
    ]
    assert_rejected(
        conn,
        iaf_command("exclpayer", ctx, items=items, allocations=allocations, adjustments=[]),
        AmbiguousItemAllocationError,
    )


def test_duplicate_and_contradictory_participants_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "dupp")
    # Identical duplicates are ambiguous, never merged.
    allocations = default_allocations()
    allocations[1]["participants"] = [
        {"participant_public_id": "person_owner"},
        {"participant_public_id": "person_owner"},
    ]
    assert_rejected(
        conn, iaf_command("dupp", ctx, allocations=allocations), AmbiguousItemAllocationError
    )
    # Contradictory duplicates (different amounts) are equally ambiguous.
    allocations = default_allocations()
    allocations[0]["participants"] = [
        {"participant_public_id": "person_owner", "share_amount": "2.00", "currency": "SGD"},
        {"participant_public_id": "person_owner", "share_amount": "3.00", "currency": "SGD"},
    ]
    assert_rejected(
        conn, iaf_command("dupp", ctx, allocations=allocations), AmbiguousItemAllocationError
    )
    # Duplicate manual adjustment shares.
    adjustments = [
        dict(
            default_adjustments()[0],
            allocation_method="manual",
            participants=[
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "0.56",
                    "currency": "SGD",
                },
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "0.56",
                    "currency": "SGD",
                },
            ],
        )
    ]
    assert_rejected(
        conn, iaf_command("dupp", ctx, adjustments=adjustments), AmbiguousItemAllocationError
    )


# ---------------------------------------------------------------------------
# 18.2 Guards 6-13: receipt, provenance, staleness, authority, ad-hoc rows
# ---------------------------------------------------------------------------


def test_receipt_not_found(migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "nofind")
    assert_rejected(
        conn,
        iaf_command("nofind", ctx, receipt_public_id="rcpt_does_not_exist"),
        ItemFactsReceiptNotFoundError,
    )


def test_non_registry_receipt_provenance_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A receipt without a conversion registry row is unsupported, even
    with populated canonical amount columns (schema-backstop fixture)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "prov")
    conn.execute(
        """
        INSERT INTO receipts (
            public_id, merchant, net_paid_amount, net_paid_amount_canonical_text,
            currency, payer_participant_id, status
        ) VALUES ('rcpt_adhoc_prov', 'Ad-hoc Cafe', '12.34', '12.34', 'SGD', ?, 'confirmed')
        """,
        (participant_id(conn, "person_owner"),),
    )
    conn.commit()
    assert_rejected(
        conn,
        iaf_command("prov", ctx, receipt_public_id="rcpt_adhoc_prov"),
        UnsupportedItemFactsReceiptProvenanceError,
    )


def test_stale_conversion_binding_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "stale")
    # Wrong asserted conversion command (well-formed but not the binding).
    assert_rejected(
        conn,
        iaf_command("stale", ctx, expected_conversion_command_public_id="rpfc_other"),
        StaleItemFactsReceiptBindingError,
    )
    # Wrong asserted conversion result hash.
    assert_rejected(
        conn,
        iaf_command("stale", ctx, expected_conversion_result_hash="a" * 64),
        StaleItemFactsReceiptBindingError,
    )


def test_receipt_status_drift_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Forged-drift fixture: the freeze trigger is dropped so the status
    can drift; the service must still fail closed (Decision D4)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "voided")
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute("UPDATE receipts SET status = 'voided' WHERE id = ?", (ctx.receipt_id,))
    conn.commit()
    assert_rejected(conn, iaf_command("voided", ctx), StaleItemFactsReceiptBindingError)


def test_finalized_receipt_transaction_binding_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A set transaction_id means finalization authority exists (IA-D11)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "final")
    cursor = conn.execute(
        "INSERT INTO transactions (public_id, intent, intent_type, transaction_date) "
        "VALUES ('txn_iaf_final', 'Expense', 'Generated', '2026-07-20')"
    )
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipts SET transaction_id = ? WHERE id = ?",
        (cursor.lastrowid, ctx.receipt_id),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("final", ctx), StaleItemFactsReceiptBindingError)


def test_existing_calculation_authority_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "calcrun")
    conn.execute(
        "INSERT INTO calculation_runs (public_id, calculation_version, scope_type, "
        "receipt_id, currency) VALUES ('calc_iaf_run', 'v1', 'receipt', ?, 'SGD')",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("calcrun", ctx), StaleItemFactsReceiptBindingError)


def test_existing_calculation_snapshot_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "calcsnap")
    conn.execute(
        """
        INSERT INTO authoritative_calculation_snapshots (
            snapshot_public_id, snapshot_schema_version, calculation_type,
            aggregate_public_id, input_payload_json, output_payload_json,
            rules_payload_json, input_hash, output_hash, rules_hash,
            combined_snapshot_hash, money_contract_version,
            currency_contract_version, algorithm_version, actor_type,
            finalization_status, created_at
        ) VALUES ('snap_iaf', 'v1', 'receipt_split', ?, '{}', '{}', '{}',
                  ?, ?, ?, ?, 'v1', 'v1', 'v1', 'system', 'draft',
                  '2026-07-20T00:00:00Z')
        """,
        (ctx.receipt_public_id, "1" * 64, "2" * 64, "3" * 64, "4" * 64),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("calcsnap", ctx), StaleItemFactsReceiptBindingError)


def test_existing_fact_set_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "onefs")
    persist(conn, iaf_command("onefs", ctx))
    # A different create command against the same receipt fails: the
    # correction path is the separate IAF.3 supersession command.
    assert_rejected(conn, iaf_command("onefs2", ctx), ItemFactSetAlreadyExistsError)


def test_untrusted_adhoc_rows_rejected_never_adopted(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "adhoc")
    conn.execute(
        "INSERT INTO receipt_items (public_id, receipt_id, line_number, item_name, "
        "line_amount, currency) VALUES ('ritem_adhoc_x', ?, 1, 'Mystery', '5.00', 'SGD')",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("adhoc", ctx), ItemFactsEvidenceLineageError)
    # Legacy v1 allocation rows are equally untrusted.
    item_row = conn.execute(
        "SELECT id FROM receipt_items WHERE public_id = 'ritem_adhoc_x'"
    ).fetchone()
    conn.execute(
        "INSERT INTO receipt_item_allocations (public_id, receipt_item_id, participant_id, "
        "share_amount_before_service_charge, allocation_method) "
        "VALUES ('ralloc_adhoc_x', ?, ?, '5.00', 'equal_amount')",
        (item_row["id"], participant_id(conn, "person_owner")),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("adhoc", ctx), ItemFactsEvidenceLineageError)


def test_adhoc_adjustment_rows_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "adhocadj")
    conn.execute(
        "INSERT INTO receipt_adjustments (public_id, receipt_id, adjustment_type, amount, "
        "currency, direction, allocation_method) VALUES "
        "('radj_adhoc_x', ?, 'service_charge', '1.12', 'SGD', 'add', 'equal_per_participant')",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("adhocadj", ctx), ItemFactsEvidenceLineageError)


# ---------------------------------------------------------------------------
# 18.2 Guard 8/15: corrupted B4.1 lineage and audit chain
# ---------------------------------------------------------------------------


def test_never_written_column_drift_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "nevercol")
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute("UPDATE receipts SET notes = 'forged' WHERE id = ?", (ctx.receipt_id,))
    conn.commit()
    assert_rejected(conn, iaf_command("nevercol", ctx), ItemFactsEvidenceLineageError)


def test_canonical_text_drift_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A drifted authoritative canonical text breaks the mirror contract."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "canondrift")
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipts SET net_paid_amount_canonical_text = '12.35' WHERE id = ?",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("canondrift", ctx), ItemFactsEvidenceLineageError)


def test_membership_role_corruption_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "memdrift")
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET role = 'participant' "
        "WHERE receipt_id = ? AND role = 'payer'",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("memdrift", ctx), ItemFactsEvidenceLineageError)


def test_broken_evidence_lineage_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Deleting the OCR proposal link breaks the guard 15 evidence chain
    (forged-drift fixture: the append-only trigger is dropped first)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "lineage")
    conversion = conn.execute(
        "SELECT parser_output_id FROM receipt_proposal_conversions WHERE receipt_id = ?",
        (ctx.receipt_id,),
    ).fetchone()
    conn.execute("DROP TRIGGER trg_receipt_ocr_proposal_links_no_delete")
    conn.execute(
        "DELETE FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
        (conversion["parser_output_id"],),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("lineage", ctx), ItemFactsEvidenceLineageError)


def test_tampered_audit_chain_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "chainbad")
    conn.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    conn.execute(
        "UPDATE financial_audit_events SET event_hash = ? "
        "WHERE aggregate_type = 'receipt' AND aggregate_public_id = ?",
        ("f" * 64, ctx.receipt_public_id),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("chainbad", ctx), ItemFactsEvidenceLineageError)


# ---------------------------------------------------------------------------
# 18.2 Connection preconditions and guard precedence
# ---------------------------------------------------------------------------


def test_foreign_keys_off_rejected_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "fkoff")
    cmd = iaf_command("fkoff", ctx)
    before = table_counts(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with pytest.raises(ItemFactsForeignKeysDisabledError) as excinfo:
            persist(conn, cmd)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    assert isinstance(excinfo.value.__cause__, ForeignKeysDisabledError)
    assert not conn.in_transaction
    assert table_counts(conn) == before
    # With enforcement restored, the identical command persists normally.
    result = persist(conn, cmd)
    assert result.idempotent is False


def test_caller_owned_transaction_rejected_untouched(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "callertx")
    cmd = iaf_command("callertx", ctx)
    before = table_counts(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ItemFactsCallerOwnedTransactionError):
            persist(conn, cmd)
        # The caller's transaction is left pending, not rolled back.
        assert conn.in_transaction
    finally:
        conn.rollback()
    assert table_counts(conn) == before
    result = persist(conn, cmd)
    assert result.idempotent is False


def test_guard_precedence_is_deterministic(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """One fixture violating several guards fails on the earliest guard."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "preced")
    # Structural validation (guard 1) precedes receipt existence (guard 6):
    # a malformed command against a nonexistent receipt is Invalid, not
    # NotFound.
    assert_rejected(
        conn,
        iaf_command(
            "preced",
            ctx,
            receipt_public_id="rcpt_missing",
            expected_current_fact_set="whatever",
        ),
        InvalidItemFactsCommandError,
    )
    # Actor authorization precedes receipt existence too.
    assert_rejected(
        conn,
        iaf_command("preced", ctx, receipt_public_id="rcpt_missing", actor_type="agent"),
        UnauthorizedItemFactsActorError,
    )
    # Receipt existence (guard 6) precedes binding staleness.
    assert_rejected(
        conn,
        iaf_command(
            "preced",
            ctx,
            receipt_public_id="rcpt_missing",
            expected_conversion_result_hash="a" * 64,
        ),
        ItemFactsReceiptNotFoundError,
    )
    # Binding staleness precedes content validation (bad reconciliation).
    items = default_items()
    items[0]["line_amount"] = "4.99"
    assert_rejected(
        conn,
        iaf_command("preced", ctx, items=items, expected_conversion_result_hash="a" * 64),
        StaleItemFactsReceiptBindingError,
    )


def test_structural_errors_precede_staging_guard(tmp_path: Path) -> None:
    """Guard 1 fires before the staging guard: a malformed command fails
    typed even on a plain untrusted database, with zero writes."""
    plain_path = tmp_path / "plain_untrusted.sqlite"
    conn = sqlite3.connect(str(plain_path))
    conn.row_factory = sqlite3.Row
    try:
        cmd = ReceiptItemAllocationFactsCommand(
            command_public_id="bad id",
            receipt_public_id="rcpt_x",
            expected_conversion_command_public_id="rpfc_x",
            expected_conversion_result_hash="0" * 64,
            expected_current_fact_set="none",
            items=default_items(),
            allocations=default_allocations(),
            adjustments=default_adjustments(),
            authenticated_actor_id="owner",
            channel="cli",
        )
        with pytest.raises(InvalidItemFactsCommandError):
            persist_receipt_item_allocation_facts(conn, cmd)
        # A float amount is likewise structural (guard 1) and precedes staging.
        items = default_items()
        items[0]["line_amount"] = 5.0
        cmd2 = ReceiptItemAllocationFactsCommand(
            command_public_id="riaf_plainmoney",
            receipt_public_id="rcpt_x",
            expected_conversion_command_public_id="rpfc_x",
            expected_conversion_result_hash="0" * 64,
            expected_current_fact_set="none",
            items=items,
            allocations=default_allocations(),
            adjustments=default_adjustments(),
            authenticated_actor_id="owner",
            channel="cli",
        )
        with pytest.raises(InvalidItemFactsMoneyError):
            persist_receipt_item_allocation_facts(conn, cmd2)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 18.3 / 18.4 Idempotent replay and material conflicts
# ---------------------------------------------------------------------------


def test_exact_replay_zero_writes_one_audit_event(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "replay")
    first = persist(conn, iaf_command("replay", ctx, reason="first"))
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)

    second = persist(conn, iaf_command("replay", ctx, reason="first"))

    assert second.idempotent is True
    assert not conn.in_transaction
    # The replay returns the recorded result exactly, not a recomputation.
    assert result_fields(second) == {**result_fields(first), "idempotent": True}
    assert count_diff(before_counts, table_counts(conn)) == {}
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence
    events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events WHERE causation_public_id = 'riaf_replay'"
    ).fetchone()[0]
    assert events == 1
    chain = verify_financial_audit_chain(
        conn, aggregate_type="receipt", aggregate_public_id=ctx.receipt_public_id
    )
    assert chain.valid and chain.event_count == 2


def test_reason_only_replay(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """``reason`` is excluded from the command material (Section 12.2)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "reason")
    persist(conn, iaf_command("reason", ctx, reason="original reason"))
    before = table_counts(conn)
    for new_reason in (None, "a different reason"):
        result = persist(conn, iaf_command("reason", ctx, reason=new_reason))
        assert result.idempotent is True
    assert count_diff(before, table_counts(conn)) == {}
    # The registry keeps the originally recorded reason, never a rewrite.
    row = conn.execute(
        "SELECT reason FROM receipt_item_allocation_fact_sets "
        "WHERE command_public_id = 'riaf_reason'"
    ).fetchone()
    assert row["reason"] == "original reason"


def test_reconnect_exact_replay(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """A crash/reconnect replay on a fresh connection is byte-identical."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "reconn")
    first = persist(conn, iaf_command("reconn", ctx))
    before = table_counts(conn)

    reconnected = connect_temp_db(migrated_temp_db_path)
    try:
        second = persist(reconnected, iaf_command("reconn", ctx))
    finally:
        reconnected.close()
    assert second.idempotent is True
    assert result_fields(second) == {**result_fields(first), "idempotent": True}
    assert count_diff(before, table_counts(conn)) == {}


def test_material_conflicts_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Same command public ID with different canonical material conflicts."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "conflict")
    persist(conn, iaf_command("conflict", ctx))

    # Changed item amount.
    items = default_items()
    items[0]["line_amount"] = "4.99"
    assert_rejected(
        conn, iaf_command("conflict", ctx, items=items), ItemFactsIdempotencyConflictError
    )
    # Changed allocation participants.
    allocations = default_allocations()
    allocations[1]["participants"] = [{"participant_public_id": "person_owner"}]
    assert_rejected(
        conn,
        iaf_command("conflict", ctx, allocations=allocations),
        ItemFactsIdempotencyConflictError,
    )
    # Changed manual share split (2.00/3.00 instead of 2.50/2.50).
    allocations = default_allocations()
    allocations[0]["participants"][0]["share_amount"] = "3.00"
    allocations[0]["participants"][1]["share_amount"] = "2.00"
    assert_rejected(
        conn,
        iaf_command("conflict", ctx, allocations=allocations),
        ItemFactsIdempotencyConflictError,
    )
    # Removed adjustments.
    assert_rejected(
        conn, iaf_command("conflict", ctx, adjustments=[]), ItemFactsIdempotencyConflictError
    )
    # Different actor or channel is different authorization material.
    assert_rejected(
        conn,
        iaf_command("conflict", ctx, authenticated_actor_id="alice"),
        ItemFactsIdempotencyConflictError,
    )
    assert_rejected(
        conn, iaf_command("conflict", ctx, channel="telegram"), ItemFactsIdempotencyConflictError
    )


def test_entry_order_is_nonsemantic(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Reordering lists without changing facts replays idempotently:
    items sort by line number, allocations by line, participants by
    public ID, adjustments by index (Section 12.1)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "order")
    first = persist(conn, iaf_command("order", ctx))

    items = list(reversed(default_items()))
    allocations = list(reversed(default_allocations()))
    for entry in allocations:
        entry["participants"] = list(reversed(entry["participants"]))
    result = persist(conn, iaf_command("order", ctx, items=items, allocations=allocations))
    assert result.idempotent is True
    assert result.command_material_hash == first.command_material_hash


def test_line_number_swap_is_semantic(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Swapping which facts sit on which line changes the material."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "semorder")
    persist(conn, iaf_command("semorder", ctx))
    items = [
        {"line_number": 1, "item_name": "Kopi", "line_amount": "6.22", "currency": "SGD"},
        {"line_number": 2, "item_name": "Chicken Rice", "line_amount": "5.00", "currency": "SGD"},
    ]
    allocations = [
        {
            "line_number": 1,
            "allocation_method": "equal_amount",
            "participants": [
                {"participant_public_id": "person_owner"},
                {"participant_public_id": "person_alice"},
            ],
        },
        {
            "line_number": 2,
            "allocation_method": "manual",
            "participants": [
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "2.50",
                    "currency": "SGD",
                },
                {
                    "participant_public_id": "person_alice",
                    "share_amount": "2.50",
                    "currency": "SGD",
                },
            ],
        },
    ]
    assert_rejected(
        conn,
        iaf_command("semorder", ctx, items=items, allocations=allocations),
        ItemFactsIdempotencyConflictError,
    )


def test_replay_time_drift_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Replay never trusts the registry row alone: tampered canonical
    texts and lossy mirrors are detected (forged-drift fixtures)."""
    conn = migrated_temp_db_connection
    ctx_a = setup_receipt(conn, tmp_path, "rdrifta")
    result_a = persist(conn, iaf_command("rdrifta", ctx_a))
    ctx_b = setup_receipt(conn, tmp_path, "rdriftb", seed=False)
    result_b = persist(conn, iaf_command("rdriftb", ctx_b))
    conn.execute("DROP TRIGGER trg_receipt_items_fact_set_bound_freeze")

    # Canonical-text drift on receipt A.
    conn.execute(
        "UPDATE receipt_items SET line_amount_canonical_text = '5.01' "
        "WHERE fact_set_id = ? AND line_number = 1",
        (result_a.fact_set_public_id,),
    )
    conn.commit()
    error = assert_rejected(conn, iaf_command("rdrifta", ctx_a), ItemFactsPersistenceError)
    assert "replay refused" in str(error)

    # Mirror-only drift on receipt B: canonical text intact, NUMERIC lossy.
    conn.execute(
        "UPDATE receipt_items SET line_amount = '9.99' WHERE fact_set_id = ? AND line_number = 1",
        (result_b.fact_set_public_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("rdriftb", ctx_b), ItemFactsPersistenceError)


def test_forged_supersession_pointer_rejected_on_replay(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A forged cross-receipt superseded_by pointer fails lineage verification."""
    conn = migrated_temp_db_connection
    ctx_a = setup_receipt(conn, tmp_path, "forgea")
    persist(conn, iaf_command("forgea", ctx_a))
    ctx_b = setup_receipt(conn, tmp_path, "forgeb", seed=False)
    result_b = persist(conn, iaf_command("forgeb", ctx_b))
    conn.execute("DROP TRIGGER trg_receipt_item_allocation_fact_sets_single_transition")
    conn.execute(
        "UPDATE receipt_item_allocation_fact_sets SET superseded_by_fact_set_public_id = ? "
        "WHERE command_public_id = 'riaf_forgea'",
        (result_b.fact_set_public_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("forgea", ctx_a), ItemFactsPersistenceError)


# ---------------------------------------------------------------------------
# 18.4 Concurrency and busy/locked mapping
# ---------------------------------------------------------------------------


def test_busy_locked_mapped_to_typed_persistence_error(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "busy")
    conn.execute("PRAGMA busy_timeout = 100")
    holder = connect_temp_db(migrated_temp_db_path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ItemFactsPersistenceError) as excinfo:
            persist(conn, iaf_command("busy", ctx))
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
        assert not conn.in_transaction
    finally:
        holder.rollback()
        holder.close()
    # After the lock is released the identical command persists cleanly.
    result = persist(conn, iaf_command("busy", ctx))
    assert result.idempotent is False


def run_persist_worker(
    db_path: Path,
    cmd: ReceiptItemAllocationFactsCommand,
    results: dict[str, object],
    key: str,
    write_lock_queued: threading.Event | None = None,
) -> None:
    """Worker body: own connection, bounded busy wait, exceptions captured."""
    conn = connect_temp_db(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        if write_lock_queued is not None:

            def trace(statement: str) -> None:
                if "BEGIN IMMEDIATE" in statement:
                    write_lock_queued.set()

            conn.set_trace_callback(trace)
        results[key] = persist_receipt_item_allocation_facts(conn, cmd)
    except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
        results[key] = exc
    finally:
        conn.close()


def test_true_concurrency_identical_command_exactly_once(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """A live second writer against a paused first writer stays exactly-once."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "thrsame")
    cmd = iaf_command("thrsame", ctx)

    first_at_commit = threading.Event()
    release_first = threading.Event()
    second_queued_on_lock = threading.Event()

    def hook(stage: str) -> None:
        if stage != "before_commit":
            return
        if threading.current_thread().name != "iaf-first-writer":
            return
        first_at_commit.set()
        if not release_first.wait(timeout=10.0):
            raise RuntimeError("first writer was never released")

    results: dict[str, object] = {}
    first = threading.Thread(
        target=run_persist_worker,
        args=(migrated_temp_db_path, cmd, results, "first"),
        name="iaf-first-writer",
    )
    second = threading.Thread(
        target=run_persist_worker,
        args=(migrated_temp_db_path, cmd, results, "second", second_queued_on_lock),
        name="iaf-second-writer",
    )
    iaf_module._failure_injection_hook = hook
    try:
        first.start()
        assert first_at_commit.wait(timeout=10.0), "first writer never reached before_commit"
        second.start()
        # Deterministic overlap proof: the second writer's own BEGIN
        # IMMEDIATE fires the trace hook while the first still holds the
        # write transaction (no wall-clock sleeps).
        assert second_queued_on_lock.wait(timeout=10.0), (
            "second writer never issued BEGIN IMMEDIATE"
        )
        assert first_at_commit.is_set() and not release_first.is_set()
        release_first.set()
        first.join(timeout=15.0)
        second.join(timeout=15.0)
    finally:
        iaf_module._failure_injection_hook = None
        release_first.set()
    assert not first.is_alive() and not second.is_alive()

    first_result = results["first"]
    if isinstance(first_result, BaseException):
        raise AssertionError(f"first writer failed: {first_result!r}") from first_result
    assert first_result.idempotent is False  # type: ignore[attr-defined]

    second_result = results["second"]
    if isinstance(second_result, BaseException):
        # Acceptable outcome: bounded busy wait exhausted, failed closed.
        assert isinstance(second_result, ItemFactsPersistenceError)
        assert isinstance(second_result.__cause__, sqlite3.OperationalError)
    else:
        assert second_result.idempotent is True  # type: ignore[attr-defined]
        assert result_fields(second_result) == {
            **result_fields(first_result),
            "idempotent": True,
        }

    assert conn.execute("SELECT COUNT(*) FROM receipt_item_allocation_fact_sets").fetchone()[0] == 1
    events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events WHERE causation_public_id = 'riaf_thrsame'"
    ).fetchone()[0]
    assert events == 1


def test_true_concurrency_competing_commands_single_winner(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """Two distinct create commands racing on one receipt: exactly one
    fact set wins; the loser fails typed (AlreadyExists or busy)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "thrrace")
    barrier = threading.Barrier(2, timeout=10.0)
    results: dict[str, object] = {}

    def racer(key: str, cmd: ReceiptItemAllocationFactsCommand) -> None:
        conn_local = connect_temp_db(migrated_temp_db_path)
        try:
            conn_local.execute("PRAGMA busy_timeout = 10000")
            barrier.wait()
            results[key] = persist_receipt_item_allocation_facts(conn_local, cmd)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
            results[key] = exc
        finally:
            conn_local.close()

    threads = [
        threading.Thread(target=racer, args=("a", iaf_command("thrrace_a", ctx))),
        threading.Thread(target=racer, args=("b", iaf_command("thrrace_b", ctx))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
    assert not any(thread.is_alive() for thread in threads)

    winners = [key for key, value in results.items() if not isinstance(value, BaseException)]
    losers = [key for key, value in results.items() if isinstance(value, BaseException)]
    assert len(winners) == 1 and len(losers) == 1
    loser_error = results[losers[0]]
    assert isinstance(loser_error, (ItemFactSetAlreadyExistsError, ItemFactsPersistenceError))
    if isinstance(loser_error, ItemFactsPersistenceError):
        assert isinstance(loser_error.__cause__, sqlite3.OperationalError)
    registry = conn.execute(
        "SELECT command_public_id FROM receipt_item_allocation_fact_sets"
    ).fetchall()
    assert len(registry) == 1
    assert registry[0]["command_public_id"] == f"riaf_thrrace_{winners[0]}"
    events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events WHERE event_type = ?",
        (RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE,),
    ).fetchone()[0]
    assert events == 1


# ---------------------------------------------------------------------------
# 18.5 Failure injection and atomicity
# ---------------------------------------------------------------------------


class _InjectedFailure(RuntimeError):
    """Marker raised by the failure-injection hooks in these tests."""


def test_every_failure_injection_stage_rolls_back_completely(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "inject")
    cmd = iaf_command("inject", ctx)
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)
    assert iaf_module.FAILURE_INJECTION_STAGES == (
        "before_fact_set_registry_insert",
        "before_items_insert",
        "before_allocations_insert",
        "before_adjustments_insert",
        "before_audit_append",
        "before_persisted_verification",
        "before_commit",
    )
    for stage in iaf_module.FAILURE_INJECTION_STAGES:

        def hook(current: str, stage: str = stage) -> None:
            if current == stage:
                raise _InjectedFailure(stage)

        iaf_module._failure_injection_hook = hook
        try:
            with pytest.raises(_InjectedFailure):
                persist(conn, cmd)
        finally:
            iaf_module._failure_injection_hook = None
        assert not conn.in_transaction, stage
        assert count_diff(before_counts, table_counts(conn)) == {}, stage
        assert receipt_state(conn) == before_receipt, stage
        assert evidence_rows(conn) == before_evidence, stage
    # After all injected failures, the identical command persists cleanly.
    result = persist(conn, cmd)
    assert result.idempotent is False
    assert count_diff(before_counts, table_counts(conn)) == expected_fact_set_diff(2, 4, 1)


def test_sqlite_failure_injection_mapped_with_cause(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "sqlerr")
    cmd = iaf_command("sqlerr", ctx)

    def hook(stage: str) -> None:
        if stage == "before_audit_append":
            raise sqlite3.OperationalError("disk I/O error (injected)")

    iaf_module._failure_injection_hook = hook
    try:
        error = assert_rejected(conn, cmd, ItemFactsPersistenceError)
    finally:
        iaf_module._failure_injection_hook = None
    assert isinstance(error.__cause__, sqlite3.OperationalError)
    result = persist(conn, cmd)
    assert result.idempotent is False


def test_mutation_at_verification_seam_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """State mutated inside the transaction at the pre-commit seam fails
    the persisted verification and rolls back completely (mutation too)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "mutate")
    cmd = iaf_command("mutate", ctx)
    fact_set_id = derive_fact_set_public_id("riaf_mutate")
    before = table_counts(conn)

    def hook(stage: str) -> None:
        if stage != "before_persisted_verification":
            return
        conn.execute("DROP TRIGGER trg_receipt_items_fact_set_bound_freeze")
        conn.execute(
            "UPDATE receipt_items SET line_amount_canonical_text = '9.99' "
            "WHERE fact_set_id = ? AND line_number = 1",
            (fact_set_id,),
        )

    iaf_module._failure_injection_hook = hook
    try:
        with pytest.raises(ItemFactsPersistenceError):
            persist(conn, cmd)
    finally:
        iaf_module._failure_injection_hook = None
    assert not conn.in_transaction
    assert count_diff(before, table_counts(conn)) == {}
    # The rollback also restored the dropped trigger (DDL inside the txn).
    trigger = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'trg_receipt_items_fact_set_bound_freeze'"
    ).fetchone()[0]
    assert trigger == 1
    result = persist(conn, cmd)
    assert result.idempotent is False


def test_audit_event_adoption_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh persistence refuses to adopt an already-recorded audit event."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "adopt")
    real_append = iaf_module.append_financial_audit_event

    def fake_append(conn_arg: sqlite3.Connection, command_arg: Any) -> Any:
        event, _idempotent = real_append(conn_arg, command_arg)
        return event, True

    monkeypatch.setattr(iaf_module, "append_financial_audit_event", fake_append)
    error = assert_rejected(conn, iaf_command("adopt", ctx), ItemFactsPersistenceError)
    assert "refusing to adopt" in str(error)


def test_audit_chain_conflict_mapped_with_cause(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "auditconf")

    def raising_append(conn_arg: sqlite3.Connection, command_arg: Any) -> Any:
        raise AuditVerificationError("injected chain-head conflict")

    monkeypatch.setattr(iaf_module, "append_financial_audit_event", raising_append)
    error = assert_rejected(conn, iaf_command("auditconf", ctx), ItemFactsPersistenceError)
    assert isinstance(error.__cause__, AuditVerificationError)


# ---------------------------------------------------------------------------
# 18.6 Boundary non-effects and database safety
# ---------------------------------------------------------------------------


def test_zero_boundary_effects_outside_iaf_tables(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The whole-database diff is exactly the IAF whitelist; no calculation,
    snapshot, transaction, settlement, or reconciliation authority appears."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "noeffect")
    before = table_counts(conn)
    persist(conn, iaf_command("noeffect", ctx))
    assert count_diff(before, table_counts(conn)) == expected_fact_set_diff(2, 4, 1)
    for table in (
        "calculation_runs",
        "authoritative_calculation_snapshots",
        "transactions",
        "settlement_obligations",
        "receipt_item_allocations",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    # The service never imports the calculator, finalizer, settlement, or
    # reconciliation boundaries (static import audit).
    source = Path(iaf_module.__file__).read_text(encoding="utf-8")
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    for forbidden in (
        "finance_core.calculators",
        "receipt_finalization",
        "settlement",
        "reconciliation",
    ):
        assert not any(forbidden in line for line in import_lines), forbidden


def test_plain_database_rejected_without_writes(tmp_path: Path) -> None:
    plain_path = tmp_path / "plain_untrusted.sqlite"
    conn = sqlite3.connect(str(plain_path))
    conn.row_factory = sqlite3.Row
    try:
        cmd = ReceiptItemAllocationFactsCommand(
            command_public_id="riaf_plainzz",
            receipt_public_id="rcpt_none",
            expected_conversion_command_public_id="rpfc_none",
            expected_conversion_result_hash="0" * 64,
            expected_current_fact_set="none",
            items=default_items(),
            allocations=default_allocations(),
            adjustments=default_adjustments(),
            authenticated_actor_id="owner",
            channel="cli",
        )
        with pytest.raises(ItemFactsStagingDatabaseRejectedError):
            persist_receipt_item_allocation_facts(conn, cmd)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


def test_copied_staging_database_rejected(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    copied_path = tmp_path / "copied_staging.sqlite"
    shutil.copy2(migrated_temp_db_path, copied_path)
    conn = sqlite3.connect(str(copied_path))
    conn.row_factory = sqlite3.Row
    try:
        cmd = ReceiptItemAllocationFactsCommand(
            command_public_id="riaf_copyzz",
            receipt_public_id="rcpt_none",
            expected_conversion_command_public_id="rpfc_none",
            expected_conversion_result_hash="0" * 64,
            expected_current_fact_set="none",
            items=default_items(),
            allocations=default_allocations(),
            adjustments=default_adjustments(),
            authenticated_actor_id="owner",
            channel="cli",
        )
        with pytest.raises(ItemFactsStagingDatabaseRejectedError):
            persist_receipt_item_allocation_facts(conn, cmd)
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_live_database_rejected_via_readonly_connection() -> None:
    conn = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cmd = ReceiptItemAllocationFactsCommand(
            command_public_id="riaf_livezz",
            receipt_public_id="rcpt_none",
            expected_conversion_command_public_id="rpfc_none",
            expected_conversion_result_hash="0" * 64,
            expected_current_fact_set="none",
            items=default_items(),
            allocations=default_allocations(),
            adjustments=default_adjustments(),
            authenticated_actor_id="owner",
            channel="cli",
        )
        with pytest.raises(ItemFactsStagingDatabaseRejectedError):
            persist_receipt_item_allocation_facts(conn, cmd)
        assert not conn.in_transaction
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Three-reviewer regression coverage (verified findings R1/R2/R3)
# ---------------------------------------------------------------------------


def test_fresh_write_on_tuple_row_connection(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """R2-F1: the service is row_factory-neutral for fresh writes too.

    The audit head read AND the chain append both need sqlite3.Row; a
    default tuple-row connection must persist (and replay) normally.
    """
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "tuplerow")
    tuple_conn = sqlite3.connect(str(migrated_temp_db_path))
    tuple_conn.execute("PRAGMA foreign_keys = ON")
    try:
        assert tuple_conn.row_factory is None
        first = persist(tuple_conn, iaf_command("tuplerow", ctx))
        assert first.idempotent is False
        # The caller's row factory is restored, not left as sqlite3.Row.
        assert tuple_conn.row_factory is None
        second = persist(tuple_conn, iaf_command("tuplerow", ctx))
        assert second.idempotent is True
        assert result_fields(second) == {**result_fields(first), "idempotent": True}
    finally:
        tuple_conn.close()
    events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events WHERE causation_public_id = 'riaf_tuplerow'"
    ).fetchone()[0]
    assert events == 1


def test_group_scoped_finalization_authority_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R1-F1 (guard 10 second clause): the existing finalization path is
    group-scoped and never sets receipts.transaction_id, so any
    receipt_group_receipts membership means unverifiable downstream
    authority and fails closed."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "grouped")
    cursor = conn.execute(
        "INSERT INTO receipt_groups (public_id, group_name, currency) "
        "VALUES ('rgrp_iaf_x', 'IAF Group', 'SGD')"
    )
    conn.execute(
        "INSERT INTO receipt_group_receipts (public_id, receipt_group_id, receipt_id) "
        "VALUES ('rgr_iaf_x', ?, ?)",
        (cursor.lastrowid, ctx.receipt_id),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("grouped", ctx), StaleItemFactsReceiptBindingError)


def test_missing_payer_membership_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R3-F1a (guard 8 / D5): a deleted payer membership row is integrity
    drift (forged-drift fixture: no-delete trigger dropped first)."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "nopayer")
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_no_delete")
    conn.execute(
        "DELETE FROM receipt_participants WHERE receipt_id = ? AND role = 'payer'",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("nopayer", ctx), ItemFactsEvidenceLineageError)


def test_membership_role_inclusion_contradiction_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R3-F1b (guard 8 / D5): role and is_included must map consistently.

    The schema-level CHECK makes a non-0/1 is_included unrepresentable,
    so the contradiction leg is proven via role drift against an intact
    is_included flag."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "rolecontra")
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET role = 'excluded' "
        "WHERE receipt_id = ? AND role = 'participant' AND is_included = 1",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("rolecontra", ctx), ItemFactsEvidenceLineageError)


def test_negative_and_zero_unit_price_and_share_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R3-F1c: strictly-positive applies to unit_price and manual shares."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "negunit")
    for bad_unit in ("-3.11", "0.00"):
        items = default_items()
        items[1]["unit_price"] = bad_unit
        assert_rejected(conn, iaf_command("negunit", ctx, items=items), InvalidItemFactsMoneyError)
    allocations = default_allocations()
    allocations[0]["participants"][0]["share_amount"] = "-2.50"
    assert_rejected(
        conn, iaf_command("negunit", ctx, allocations=allocations), InvalidItemFactsMoneyError
    )


def test_receipt_numeric_mirror_drift_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R3-F1d (guard 8): a lossy receipt NUMERIC mirror alone — canonical
    text intact — fails the mirror-decoding contract."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "mirrordrift")
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipts SET net_paid_amount = '12.35' WHERE id = ?",
        (ctx.receipt_id,),
    )
    conn.commit()
    assert_rejected(conn, iaf_command("mirrordrift", ctx), ItemFactsEvidenceLineageError)


def test_mirror_lossy_amount_rejected_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R3-F3 (18.3): a Money-Contract-valid amount whose NUMERIC mirror
    cannot round-trip losslessly is rejected pre-write, zero rows."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "lossy")
    items = default_items()
    items[0]["line_amount"] = "90071992547409.93"  # 2-dp SGD, > 2**53 minor units
    error = assert_rejected(
        conn, iaf_command("lossy", ctx, items=items), InvalidItemFactsMoneyError
    )
    assert "losslessly" in str(error)


def test_oversize_quantity_rejected_as_malformed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R2-F2: quantities beyond the 15-digit mirror-safe bound are a
    guard 1 command error, never a late persistence/drift error."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "bigqty")
    for bad in ("9999999999999999", 10**16):
        items = default_items()
        items[1]["quantity"] = bad
        assert_rejected(conn, iaf_command("bigqty", ctx, items=items), InvalidItemFactsCommandError)


MUTATION_VARIANTS = [
    (
        "item_canonical_text",
        "trg_receipt_items_fact_set_bound_freeze",
        "UPDATE receipt_items SET line_amount_canonical_text = '9.99' "
        "WHERE fact_set_id = ? AND line_number = 1",
    ),
    (
        "item_mirror",
        "trg_receipt_items_fact_set_bound_freeze",
        "UPDATE receipt_items SET line_amount = '9.99' WHERE fact_set_id = ? AND line_number = 1",
    ),
    (
        "allocation_share_canonical_text",
        "trg_receipt_item_allocation_facts_no_update",
        "UPDATE receipt_item_allocation_facts SET share_amount_canonical_text = '9.99' "
        "WHERE fact_set_id = ? AND allocation_method = 'manual'",
    ),
    (
        "adjustment_amount_canonical_text",
        "trg_receipt_adjustments_fact_set_bound_freeze",
        "UPDATE receipt_adjustments SET amount_canonical_text = '9.99' WHERE fact_set_id = ?",
    ),
    (
        "registry_canonical_payload",
        "trg_receipt_item_allocation_fact_sets_single_transition",
        "UPDATE receipt_item_allocation_fact_sets SET canonical_fact_set_payload = '{}' "
        "WHERE fact_set_public_id = ?",
    ),
]


@pytest.mark.parametrize(
    ("variant", "trigger", "mutation_sql"),
    MUTATION_VARIANTS,
    ids=[variant for variant, _, _ in MUTATION_VARIANTS],
)
def test_mutation_of_each_written_field_at_seam_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    variant: str,
    trigger: str,
    mutation_sql: str,
) -> None:
    """R3-F2 (18.3): the pre-commit verification catches tampering of each
    written field class at the before_persisted_verification seam and
    rolls the whole persistence — including the mutation — back."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, f"mut_{variant[:8]}")
    cmd = iaf_command(f"mut_{variant[:8]}", ctx)
    fact_set_id = derive_fact_set_public_id(cmd.command_public_id)
    before = table_counts(conn)

    def hook(stage: str) -> None:
        if stage != "before_persisted_verification":
            return
        conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(mutation_sql, (fact_set_id,))

    iaf_module._failure_injection_hook = hook
    try:
        with pytest.raises(ItemFactsPersistenceError):
            persist(conn, cmd)
    finally:
        iaf_module._failure_injection_hook = None
    assert not conn.in_transaction
    assert count_diff(before, table_counts(conn)) == {}
    # The rollback restored the dropped trigger (DDL inside the txn).
    restored = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger,),
    ).fetchone()[0]
    assert restored == 1
    result = persist(conn, cmd)
    assert result.idempotent is False


def test_audit_chain_transaction_error_mapped_with_cause(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3-F4 (18.5): the AuditChainTransactionError leg maps typed too."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "audittxn")

    def raising_append(conn_arg: sqlite3.Connection, command_arg: Any) -> Any:
        raise AuditChainTransactionError("injected transaction-context failure")

    monkeypatch.setattr(iaf_module, "append_financial_audit_event", raising_append)
    error = assert_rejected(conn, iaf_command("audittxn", ctx), ItemFactsPersistenceError)
    assert isinstance(error.__cause__, AuditChainTransactionError)


def test_calculator_never_invoked_runtime_spy(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3-F6 (18.6): runtime spy proof that persistence never runs the
    deterministic calculator (B4.3 precedent)."""
    import finance_core.calculators.receipt_split_calculator as calculator_module

    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "calcspy")
    calls: list[object] = []

    def spy(case_data: Any) -> Any:
        calls.append(case_data)
        raise AssertionError("persist_receipt_item_allocation_facts ran the calculator")

    monkeypatch.setattr(calculator_module, "calculate_receipt_split", spy)
    result = persist(conn, iaf_command("calcspy", ctx))
    assert result.idempotent is False
    assert calls == []


def test_clock_validation_and_canonical_timestamp(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R3-F10: the optional clock seam is validated fail-closed and the
    persisted timestamp is the canonical UTC microsecond form."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "clock")
    for bad_clock in (lambda: 123, lambda: "not-a-timestamp", lambda: "2026-07-20T00:00:00"):
        before = table_counts(conn)
        with pytest.raises(ItemFactsPersistenceError):
            persist_receipt_item_allocation_facts(
                conn,
                iaf_command("clock", ctx),
                clock=bad_clock,  # type: ignore[arg-type]
            )
        assert not conn.in_transaction
        assert count_diff(before, table_counts(conn)) == {}
    result = persist_receipt_item_allocation_facts(
        conn,
        iaf_command("clock", ctx),
        clock=lambda: "2026-07-20T08:30:00+08:00",
    )
    row = conn.execute(
        "SELECT created_at FROM receipt_item_allocation_fact_sets "
        "WHERE command_public_id = 'riaf_clock'"
    ).fetchone()
    assert row["created_at"] == "2026-07-20T00:30:00.000000Z"
    event = conn.execute(
        "SELECT created_at FROM financial_audit_events WHERE event_public_id = ?",
        (result.audit_event_public_id,),
    ).fetchone()
    assert event["created_at"] == "2026-07-20T00:30:00.000000Z"
