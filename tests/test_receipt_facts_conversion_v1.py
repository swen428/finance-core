"""B4.1 guarded receipt proposal-to-facts conversion tests (guards and lineage).

Covers design Sections 8.1 (happy path), 8.2 (fail-closed rejections),
8.3 (staleness and supersession), and 8.3a (mutual exclusion with the legacy
converter and neighbour boundaries) of
``docs/design/receipt_proposal_to_facts_conversion_v1.md``.

Idempotency/concurrency, failure injection, boundary non-effects, and
database safety live in ``test_receipt_facts_conversion_concurrency_v1.py``,
which reuses the fixture helpers defined here.  All fixtures are synthetic
and privacy-safe; no live database or seed data is touched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from finance_core.calculation.authoritative_snapshot import canonical_json_text
from finance_core.financial_audit import (
    AuditChainConflictError,
    AuditEventCommand,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.parser_proposals import (
    UnsupportedReceiptFactsMetadataError,
    complete_proposal,
    confirm_proposal,
    convert_confirmed_receipt_proposal_to_facts,
    reject_proposal,
    supersede_receipt_total_proposal,
)
from finance_core.parser_proposals import (
    receipt_facts_conversion as receipt_facts_conversion_module,
)
from finance_core.parser_proposals.completion import InvalidCompletionStatusError
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.receipt_facts_conversion import (
    CONVERSION_SCHEMA_VERSION,
    RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
    AmbiguousReceiptInputError,
    ConversionEvidenceLineageError,
    ConversionIdempotencyConflictError,
    ConversionPersistenceError,
    ConversionProposalNotFoundError,
    IncompleteReceiptInputsError,
    InvalidConversionCommandError,
    ProposalNotConfirmedError,
    ReceiptFactsAlreadyConvertedError,
    ReceiptFactsConversionCommand,
    ReceiptFactsConversionError,
    StaleConfirmationHashError,
    StaleConversionTargetError,
    UnauthorizedConversionActorError,
    UnsupportedConversionProposalTypeError,
    derive_receipt_public_id,
)
from finance_core.parser_proposals.receipt_supersession import InvalidSupersessionStatusError
from finance_core.parser_proposals.receipt_total_parser import RECEIPT_AMBIGUITY_FLAGS
from finance_core.parser_proposals.service import (
    UnsupportedProposalTypeError,
    convert_confirmed_parser_proposal,
)
from tests.test_parser_proposal_conversion import create_confirmed_simple_proposal
from tests.test_receipt_ocr_proposal_ingestion_v1 import (
    JPEG,
    _hash,
    _ingest,
    _ocr_block,
    _prepare_extraction,
    _sgd_blocks,
)

pytestmark = pytest.mark.migrated_staging_snapshot

# ---------------------------------------------------------------------------
# Shared fixture helpers (also imported by the concurrency test module)
# ---------------------------------------------------------------------------

PEOPLE = ("person_owner", "person_alice", "person_bob")


def test_receipt_ambiguity_flag_classification_is_exhaustive_and_disjoint() -> None:
    groups = (
        receipt_facts_conversion_module._AMOUNT_FLAGS,
        receipt_facts_conversion_module._CURRENCY_FLAGS,
        receipt_facts_conversion_module._DATE_FLAGS,
        receipt_facts_conversion_module._INFORMATIONAL_FLAGS,
    )
    for index, group in enumerate(groups):
        assert all(group.isdisjoint(other) for other in groups[index + 1 :])
    classified = frozenset().union(*groups)
    assert classified == RECEIPT_AMBIGUITY_FLAGS
    assert receipt_facts_conversion_module._KNOWN_AMBIGUITY_FLAGS == classified


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def attachment_content(suffix: str) -> bytes:
    """Suffix-unique valid-magic JPEG bytes for fingerprint-distinct fixtures."""
    return JPEG + f"-{suffix}".encode("utf-8")


def seed_people(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO participants (public_id, display_name, is_self) VALUES (?, ?, ?)",
        [
            ("person_owner", "Owner", 1),
            ("person_alice", "Alice", 0),
            ("person_bob", "Bob", 0),
        ],
    )
    conn.commit()


def participant_id(conn: sqlite3.Connection, public_id: str) -> int:
    row = conn.execute("SELECT id FROM participants WHERE public_id = ?", (public_id,)).fetchone()
    assert row is not None
    return int(row["id"])


def hash_of(conn: sqlite3.Connection, parser_output_id: int) -> str:
    return compute_effective_proposal_content_hash(conn, {"id": parser_output_id})


def seed_receipt_proposal(
    conn: sqlite3.Connection,
    tmp_path: Path,
    suffix: str,
    *,
    blocks: tuple[Any, ...] | None = None,
) -> tuple[int, str]:
    """Ingest one B1 receipt total proposal; return (parser_output_id, public_id)."""
    extraction_public_id = _prepare_extraction(
        conn,
        tmp_path,
        suffix=suffix,
        blocks=blocks if blocks is not None else _sgd_blocks(),
        # Suffix-unique attachment bytes keep extraction fingerprints distinct
        # when one staging database seeds multiple proposals.
        content=attachment_content(suffix),
    )
    result = _ingest(
        conn,
        extraction_public_id,
        proposal=f"prop_{suffix}",
        link=f"ropl_{suffix}",
    )
    return result.parser_output_id, f"prop_{suffix}"


def seed_confirmed_receipt_proposal(
    conn: sqlite3.Connection,
    tmp_path: Path,
    suffix: str,
    *,
    blocks: tuple[Any, ...] | None = None,
) -> tuple[int, str, str]:
    """Return (parser_output_id, proposal_public_id, effective_content_hash)."""
    pid, public_id = seed_receipt_proposal(conn, tmp_path, suffix, blocks=blocks)
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id=f"pca_{suffix}")
    return pid, public_id, hash_of(conn, pid)


def entries(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"participant_public_id": p, "is_included": i} for p, i in pairs]


def command(
    suffix: str,
    proposal: str,
    expected: str,
    **overrides: Any,
) -> ReceiptFactsConversionCommand:
    fields: dict[str, Any] = {
        "command_public_id": f"rpfc_{suffix}",
        "proposal_public_id": proposal,
        "expected_content_hash": expected,
        "payer_participant_public_id": "person_owner",
        "participants": entries(("person_owner", 1), ("person_alice", 1)),
        "authenticated_actor_id": "owner",
        "channel": "cli",
        "actor_type": "human",
        "reason": None,
    }
    fields.update(overrides)
    return ReceiptFactsConversionCommand(**fields)


def convert(conn: sqlite3.Connection, cmd: ReceiptFactsConversionCommand) -> Any:
    return convert_confirmed_receipt_proposal_to_facts(conn, cmd)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for every user table: the full-boundary zero-effect snapshot."""
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables
    }


def count_diff(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        table: after[table] - before.get(table, 0)
        for table in after
        if after[table] != before.get(table, 0)
    }


def expected_conversion_diff(membership_rows: int) -> dict[str, int]:
    """The only writes a successful conversion may produce (Section 8.6)."""
    return {
        "receipts": 1,
        "receipt_participants": membership_rows,
        "receipt_proposal_conversions": 1,
        "financial_audit_events": 1,
    }


_EVIDENCE_TABLES = (
    "parser_outputs",
    "parser_proposal_completions",
    "receipt_proposal_revisions",
    "parser_proposal_confirmations",
    "parser_proposal_authorizations",
    "parser_proposal_field_evidence",
    "receipt_ocr_extractions",
    "receipt_ocr_blocks",
    "receipt_ocr_proposal_links",
    "attachments",
    "raw_intake_records",
    "raw_intake_evidence",
    "telegram_attachment_source",
)


def evidence_rows(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Byte-identity snapshot of every proposal/evidence table (Section 8.6)."""
    return {
        table: [
            dict(row)
            for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in _EVIDENCE_TABLES
    }


def conflicting_totals_blocks() -> tuple[Any, ...]:
    """Two TOTAL candidates and no date line → amount None + monetary flag."""
    return (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "TOTAL", line=1, left=10, top=60),
        _ocr_block(3, "S$", line=1, left=80, top=60),
        _ocr_block(4, "12.34", line=1, left=140, top=60),
        _ocr_block(5, "TOTAL", line=2, left=10, top=100),
        _ocr_block(6, "S$", line=2, left=80, top=100),
        _ocr_block(7, "99.99", line=2, left=140, top=100),
    )


def ambiguous_date_blocks() -> tuple[Any, ...]:
    """An ambiguous numeric date → transaction_date None + date flag."""
    return (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "05/06/2026", line=1, left=10, top=60),
        _ocr_block(3, "TOTAL", line=2, left=10, top=100),
        _ocr_block(4, "S$", line=2, left=80, top=100),
        _ocr_block(5, "12.34", line=2, left=140, top=100),
    )


def strip_payload_field(conn: sqlite3.Connection, parser_output_id: int, field: str) -> None:
    """Null one payload field before confirmation (fixture surgery only)."""
    row = conn.execute(
        "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (parser_output_id,)
    ).fetchone()
    payload = json.loads(row["parsed_payload"])
    payload[field] = None
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), parser_output_id),
    )
    conn.commit()


def forge_legacy_conversion(conn: sqlite3.Connection, parser_output_id: int, suffix: str) -> None:
    """Directly persist a schema-legal legacy conversion audit row.

    The legacy converter itself refuses OCR-linked proposals, so the
    B4-after-legacy ordering can only be produced by direct fixture SQL.
    """
    authorization = conn.execute(
        "SELECT confirmation_public_id, proposal_content_hash "
        "FROM parser_proposal_authorizations WHERE parser_output_id = ?",
        (parser_output_id,),
    ).fetchone()
    assert authorization is not None
    cursor = conn.execute(
        "INSERT INTO transactions (public_id, intent, intent_type, transaction_date) "
        "VALUES (?, 'Expense', 'Generated', '2026-07-20')",
        (f"txn_forged_{suffix}",),
    )
    conn.execute(
        "INSERT INTO parser_proposal_conversion_audit ("
        "  parser_output_id, transaction_id, confirmation_public_id,"
        "  proposal_content_hash, authenticated_actor_id"
        ") VALUES (?, ?, ?, ?, 'owner')",
        (
            parser_output_id,
            cursor.lastrowid,
            authorization["confirmation_public_id"],
            authorization["proposal_content_hash"],
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 8.1 Happy path
# ---------------------------------------------------------------------------


def test_happy_path_converts_confirmed_proposal_to_receipt_facts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "happy")
    membership = entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 0))
    cmd = command("happy", public_id, expected, participants=membership, reason="split dinner")
    before_counts = table_counts(conn)
    before_evidence = evidence_rows(conn)

    result = convert(conn, cmd)

    assert result.idempotent is False
    assert result.command_public_id == "rpfc_happy"
    assert result.proposal_public_id == public_id
    assert result.parser_output_id == pid
    assert result.confirmation_public_id == "pca_happy"
    assert result.proposal_content_hash == expected
    assert result.receipt_public_id == derive_receipt_public_id("rpfc_happy")
    assert not conn.in_transaction

    # Independent Section 6 hash recomputation: reason is excluded from the
    # command material; entries are canonically sorted by participant ID.
    sorted_entries = sorted(
        (
            {"participant_public_id": p, "is_included": i}
            for p, i in (("person_owner", 1), ("person_alice", 1), ("person_bob", 0))
        ),
        key=lambda item: str(item["participant_public_id"]),
    )
    material = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "command_public_id": "rpfc_happy",
        "proposal_public_id": public_id,
        "expected_content_hash": expected,
        "payer_participant_public_id": "person_owner",
        "participants": sorted_entries,
        "authenticated_actor_id": "owner",
        "actor_type": "human",
        "channel": "cli",
    }
    assert result.command_material_hash == _sha(_canonical_json(material))
    result_material = {
        "receipt_public_id": result.receipt_public_id,
        "merchant": "COLD STORAGE",
        "receipt_date": "2026-07-20",
        "net_paid_amount": "12.34",
        "currency": "SGD",
        "payer_participant_public_id": "person_owner",
        "participants": sorted_entries,
        "attachment_content_hash": _hash(attachment_content("happy")),
        "proposal_content_hash": expected,
        "command_material_hash": result.command_material_hash,
    }
    assert result.conversion_result_hash == _sha(_canonical_json(result_material))

    # Receipt fact row: facts-only v1 with full evidence lineage.
    receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (result.receipt_id,)).fetchone()
    proposal = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (pid,)).fetchone()
    attachment = conn.execute(
        "SELECT file_path FROM attachments WHERE id = ?", (proposal["attachment_id"],)
    ).fetchone()
    assert receipt["public_id"] == result.receipt_public_id
    assert receipt["transaction_id"] is None
    assert receipt["merchant"] == "COLD STORAGE"
    assert receipt["receipt_datetime"] == "2026-07-20"
    assert Decimal(str(receipt["net_paid_amount"])) == Decimal("12.34")
    for component in (
        "gross_amount",
        "subtotal_amount",
        "service_charge_amount",
        "tax_amount",
        "discount_amount",
    ):
        assert receipt[component] is None
    assert receipt["currency"] == "SGD"
    assert receipt["payer_participant_id"] == participant_id(conn, "person_owner")
    assert receipt["status"] == "confirmed"
    assert receipt["source_channel"] == "telegram"
    assert receipt["raw_input"] == "receipt image"
    assert receipt["attachment_id"] == proposal["attachment_id"]
    assert receipt["attachment_path"] == attachment["file_path"]
    assert receipt["parser_output_id"] == pid
    assert receipt["ocr_confidence"] == proposal["confidence_score"]

    # Membership rows: Decision D5 role mapping, deterministic rcpp_ IDs.
    members = {
        row["participant_id"]: row
        for row in conn.execute(
            "SELECT * FROM receipt_participants WHERE receipt_id = ?", (result.receipt_id,)
        ).fetchall()
    }
    assert len(members) == 3
    for person, role, included in (
        ("person_owner", "payer", 1),
        ("person_alice", "participant", 1),
        ("person_bob", "excluded", 0),
    ):
        row = members[participant_id(conn, person)]
        assert row["role"] == role
        assert row["is_included"] == included
        derived = "rcpp_" + _sha(f"receipt-participant:rpfc_happy:{person}")[:32]
        assert row["public_id"] == derived

    # Registry row.
    registry = conn.execute(
        "SELECT * FROM receipt_proposal_conversions WHERE command_public_id = 'rpfc_happy'"
    ).fetchone()
    assert registry["parser_output_id"] == pid
    assert registry["supersession_root_parser_output_id"] == pid
    assert registry["receipt_id"] == result.receipt_id
    assert registry["confirmation_public_id"] == "pca_happy"
    assert registry["proposal_content_hash"] == expected
    assert registry["command_material_hash"] == result.command_material_hash
    assert registry["conversion_result_hash"] == result.conversion_result_hash
    assert registry["actor_type"] == "human"
    assert registry["authenticated_actor_id"] == "owner"
    assert registry["conversion_channel"] == "cli"
    assert registry["reason"] == "split dinner"
    assert registry["schema_version"] == "v1"

    # Exactly one audit event, bound to command, confirmation, and evidence.
    events = conn.execute(
        "SELECT * FROM financial_audit_events WHERE causation_public_id = 'rpfc_happy'"
    ).fetchall()
    assert len(events) == 1
    event = events[0]
    assert event["aggregate_type"] == "receipt"
    assert event["aggregate_public_id"] == result.receipt_public_id
    assert event["event_type"] == "receipt_proposal_converted_to_facts"
    assert event["actor_type"] == "human"
    assert event["actor_public_id"] == "owner"
    assert event["authorization_public_id"] == "pca_happy"
    assert event["correlation_public_id"] == result.receipt_public_id
    references = json.loads(event["source_evidence_refs_json"])
    assert f"parser-output:{public_id}" in references
    assert "extraction:rocr_happy" in references
    assert "confirmation:pca_happy" in references

    # Section 8.6 on the happy path: only the four conversion writes happened
    # and every proposal/evidence row is byte-identical.
    assert count_diff(before_counts, table_counts(conn)) == expected_conversion_diff(3)
    assert evidence_rows(conn) == before_evidence


def test_happy_path_payer_not_consumer_membership_variant(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "payer0")
    membership = entries(("person_owner", 0), ("person_alice", 1))
    result = convert(conn, command("payer0", public_id, expected, participants=membership))

    rows = {
        row["participant_id"]: row
        for row in conn.execute(
            "SELECT * FROM receipt_participants WHERE receipt_id = ?", (result.receipt_id,)
        ).fetchall()
    }
    payer_row = rows[participant_id(conn, "person_owner")]
    # The payer keeps role='payer' even when explicitly not a consumer.
    assert payer_row["role"] == "payer"
    assert payer_row["is_included"] == 0
    assert rows[participant_id(conn, "person_alice")]["role"] == "participant"
    receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (result.receipt_id,)).fetchone()
    assert receipt["payer_participant_id"] == participant_id(conn, "person_owner")
    assert receipt["parser_output_id"] == pid


def test_happy_path_completion_supplies_date_then_fresh_confirmation(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Guard 13 positive path: a date flag cleared by human completion."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "comp", blocks=ambiguous_date_blocks())
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"transaction_date": "2026-06-05", "merchant": "Cold Storage Somerset"},
        completion_public_id="pco_comp_1",
    )
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_comp")
    expected = hash_of(conn, pid)

    result = convert(conn, command("comp", public_id, expected))

    receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (result.receipt_id,)).fetchone()
    assert receipt["receipt_datetime"] == "2026-06-05"
    assert receipt["merchant"] == "Cold Storage Somerset"
    assert Decimal(str(receipt["net_paid_amount"])) == Decimal("12.34")
    assert result.confirmation_public_id == "pca_comp"


def test_derive_receipt_public_id_is_deterministic_and_frozen() -> None:
    derived = derive_receipt_public_id("rpfc_fixed")
    assert derived == derive_receipt_public_id("rpfc_fixed")
    assert derived == "rcpt_" + _sha("receipt-fact:rpfc_fixed")[:32]
    assert derived != derive_receipt_public_id("rpfc_other")


# ---------------------------------------------------------------------------
# 8.2 Fail-closed rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        # Malformed command IDs and structural problems → invalid command.
        ({"command_public_id": "rcv_wrong_prefix"}, InvalidConversionCommandError),
        ({"command_public_id": "rpfc_"}, InvalidConversionCommandError),
        ({"command_public_id": "rpfc_bad space"}, InvalidConversionCommandError),
        ({"command_public_id": "rpfc_" + "x" * 196}, InvalidConversionCommandError),
        ({"expected_content_hash": "not-a-hash"}, InvalidConversionCommandError),
        ({"expected_content_hash": "A" * 64}, InvalidConversionCommandError),
        ({"proposal_public_id": "   "}, InvalidConversionCommandError),
        ({"channel": ""}, InvalidConversionCommandError),
        ({"channel": " cli"}, InvalidConversionCommandError),
        ({"participants": "person_owner"}, InvalidConversionCommandError),
        (
            {
                "participants": [
                    {"participant_public_id": "person_owner", "is_included": 1, "role": "payer"}
                ]
            },
            InvalidConversionCommandError,
        ),
        # Actor problems → unauthorized (guard 1 deterministic mapping).
        ({"actor_type": "agent"}, UnauthorizedConversionActorError),
        ({"actor_type": "system"}, UnauthorizedConversionActorError),
        ({"actor_type": ""}, UnauthorizedConversionActorError),
        ({"actor_type": "   "}, UnauthorizedConversionActorError),
        ({"authenticated_actor_id": ""}, UnauthorizedConversionActorError),
        ({"authenticated_actor_id": " owner "}, UnauthorizedConversionActorError),
        # Missing explicit membership inputs → incomplete inputs.
        ({"payer_participant_public_id": ""}, IncompleteReceiptInputsError),
        ({"participants": []}, IncompleteReceiptInputsError),
        (
            {"participants": [{"participant_public_id": "person_owner"}]},
            IncompleteReceiptInputsError,
        ),
        (
            {"participants": [{"participant_public_id": "person_owner", "is_included": 2}]},
            IncompleteReceiptInputsError,
        ),
        (
            {"participants": [{"participant_public_id": "person_owner", "is_included": "1"}]},
            IncompleteReceiptInputsError,
        ),
        # Payer must appear in the membership structure.
        (
            {"participants": [{"participant_public_id": "person_alice", "is_included": 1}]},
            IncompleteReceiptInputsError,
        ),
        # Duplicate / contradictory membership entries → ambiguous.
        (
            {
                "participants": [
                    {"participant_public_id": "person_owner", "is_included": 1},
                    {"participant_public_id": "person_owner", "is_included": 1},
                ]
            },
            AmbiguousReceiptInputError,
        ),
        (
            {
                "participants": [
                    {"participant_public_id": "person_owner", "is_included": 1},
                    {"participant_public_id": "person_owner", "is_included": 0},
                ]
            },
            AmbiguousReceiptInputError,
        ),
    ],
)
def test_guard1_rejections_leave_zero_rows(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    overrides: dict[str, Any],
    error: type[Exception],
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "g1")
    before = table_counts(conn)

    with pytest.raises(error):
        convert(conn, command("g1", public_id, expected, **overrides))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_from_mapping_rejects_unknown_item_group_and_missing_fields() -> None:
    base: dict[str, Any] = {
        "command_public_id": "rpfc_map",
        "proposal_public_id": "prop_map",
        "expected_content_hash": "a" * 64,
        "payer_participant_public_id": "person_owner",
        "participants": entries(("person_owner", 1)),
        "authenticated_actor_id": "owner",
        "channel": "cli",
    }
    # Item/allocation/adjustment/group inputs are unknown by construction
    # (approved Decisions D2 and D6) and are rejected fail-closed.
    for extra in ("items", "allocations", "adjustments", "receipt_group_public_id"):
        with pytest.raises(InvalidConversionCommandError, match="Unknown conversion command"):
            ReceiptFactsConversionCommand.from_mapping({**base, extra: []})
    with pytest.raises(InvalidConversionCommandError, match="missing required"):
        ReceiptFactsConversionCommand.from_mapping(
            {k: v for k, v in base.items() if k != "payer_participant_public_id"}
        )
    with pytest.raises(InvalidConversionCommandError, match="must be a mapping"):
        ReceiptFactsConversionCommand.from_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]
    built = ReceiptFactsConversionCommand.from_mapping(base)
    assert built.actor_type == "human"
    assert built.reason is None


def test_unknown_participant_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "ghost")
    membership = entries(("person_owner", 1), ("person_ghost", 1))
    before = table_counts(conn)

    with pytest.raises(AmbiguousReceiptInputError, match="unknown participant"):
        convert(conn, command("ghost", public_id, expected, participants=membership))

    assert table_counts(conn) == before


@pytest.mark.parametrize("field", ["merchant", "amount", "currency", "transaction_date"])
def test_missing_effective_field_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, field: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, f"miss_{field}")
    strip_payload_field(conn, pid, field)
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id=f"pca_miss_{field}")
    expected = hash_of(conn, pid)
    before = table_counts(conn)

    with pytest.raises(IncompleteReceiptInputsError):
        convert(conn, command(f"miss_{field}", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_unresolved_ambiguity_flags_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    # Confirmed but still carrying an unresolved date ambiguity flag.
    pid, public_id = seed_receipt_proposal(
        conn, tmp_path, "flagdate", blocks=ambiguous_date_blocks()
    )
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagdate")
    before = table_counts(conn)

    with pytest.raises(AmbiguousReceiptInputError, match="ambiguity flags"):
        convert(conn, command("flagdate", public_id, hash_of(conn, pid)))

    assert table_counts(conn) == before


def test_unresolved_monetary_flag_rejected_before_incomplete_amount(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(
        conn, tmp_path, "flagamt", blocks=conflicting_totals_blocks()
    )
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagamt")

    # Guard 13 (flags) precedes guard 14 (completeness): the conflicting-total
    # flag is reported even though the amount is also missing.
    with pytest.raises(AmbiguousReceiptInputError, match="ambiguity flags"):
        convert(conn, command("flagamt", public_id, hash_of(conn, pid)))


def test_unsupported_simple_text_proposal_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid = create_confirmed_simple_proposal(conn)
    public_id = conn.execute(
        "SELECT public_id FROM parser_outputs WHERE id = ?", (pid,)
    ).fetchone()["public_id"]
    before = table_counts(conn)

    with pytest.raises(UnsupportedConversionProposalTypeError):
        convert(conn, command("simple", public_id, hash_of(conn, pid)))

    assert table_counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_missing_proposal_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    with pytest.raises(ConversionProposalNotFoundError):
        convert(conn, command("none", "prop_does_not_exist", "b" * 64))
    assert not conn.in_transaction


def test_pending_confirmation_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "pend")
    with pytest.raises(ProposalNotConfirmedError):
        convert(conn, command("pend", public_id, hash_of(conn, pid)))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_rejected_proposal_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "rej")
    expected = hash_of(conn, pid)
    reject_proposal(conn, pid, actor="owner", confirmation_public_id="pca_rej")
    with pytest.raises(ProposalNotConfirmedError):
        convert(conn, command("rej", public_id, expected))


def test_revoked_authorization_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "revoked")
    conn.execute(
        "UPDATE parser_proposal_authorizations "
        "SET confirmation_state = 'revoked', revoked_at = '2026-07-25T00:00:00+00:00' "
        "WHERE parser_output_id = ?",
        (pid,),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(StaleConfirmationHashError, match="revoked"):
        convert(conn, command("revoked", public_id, expected))

    assert table_counts(conn) == before


def test_guard_order_superseded_wins_over_unconfirmed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A fixture violating guards 7 and 9 fails with guard 7's error."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "order")
    supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"amount": "20.00"},
        correction_public_id="rcor_order_1",
    )
    # The parent is now superseded AND unconfirmed; guard 7 must fire first.
    with pytest.raises(StaleConversionTargetError, match="superseded"):
        convert(conn, command("order", public_id, hash_of(conn, pid)))


@pytest.mark.parametrize(
    ("field_name", "conflicting_value"),
    [("amount", "99.99"), ("currency", "USD")],
)
def test_monetary_evidence_conflict_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    field_name: str,
    conflicting_value: str,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(
        conn, tmp_path, f"evconf_{field_name}"
    )
    conn.execute(
        "INSERT INTO parser_proposal_field_evidence "
        "(parser_output_id, field_name, proposed_value, evidence_source_type) "
        "VALUES (?, ?, ?, 'system')",
        (pid, field_name, conflicting_value),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(ConversionEvidenceLineageError, match="conflicts"):
        convert(conn, command(f"evconf_{field_name}", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# 8.3 Staleness and supersession
# ---------------------------------------------------------------------------


def test_wrong_expected_hash_command_leg_isolated(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Command leg of the Section 6 triple equality, in isolation."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "cmdleg")
    wrong = "0" * 64
    assert wrong != expected
    # Authorization hash and recomputed hash still agree; only the command
    # supplied a stale expectation.
    with pytest.raises(StaleConfirmationHashError, match="command's expected content hash"):
        convert(conn, command("cmdleg", public_id, wrong))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_payload_drift_authorization_leg_isolated(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Authorization leg: payload drifted after confirmation (fixture SQL)."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, _old = seed_confirmed_receipt_proposal(conn, tmp_path, "authleg")
    row = conn.execute("SELECT parsed_payload FROM parser_outputs WHERE id = ?", (pid,)).fetchone()
    payload = json.loads(row["parsed_payload"])
    payload["amount"] = "55.55"
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), pid),
    )
    conn.commit()
    drifted = hash_of(conn, pid)

    # The command tracks the drifted content, so the command leg passes and
    # the confirmation-bound leg must fire.
    with pytest.raises(StaleConfirmationHashError, match="confirmation-bound"):
        convert(conn, command("authleg", public_id, drifted))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_superseded_parent_rejected_after_confirmation(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "supold")
    supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"amount": "21.00"},
        correction_public_id="rcor_supold_1",
    )
    with pytest.raises(StaleConversionTargetError):
        convert(conn, command("supold", public_id, expected))


def test_child_without_fresh_confirmation_rejected_then_succeeds_after(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The superseded parent's authorization never authorizes the child."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "child")
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"amount": "20.00"},
        correction_public_id="rcor_child_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = conn.execute(
        "SELECT public_id FROM parser_outputs WHERE id = ?", (child_id,)
    ).fetchone()["public_id"]

    with pytest.raises(ProposalNotConfirmedError):
        convert(conn, command("child_a", child_public_id, hash_of(conn, child_id)))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_child_fresh")
    result = convert(conn, command("child_b", child_public_id, hash_of(conn, child_id)))

    assert result.idempotent is False
    assert result.parser_output_id == child_id
    registry = conn.execute(
        "SELECT * FROM receipt_proposal_conversions WHERE command_public_id = 'rpfc_child_b'"
    ).fetchone()
    # The chain root is the original superseded parent proposal.
    assert registry["parser_output_id"] == child_id
    assert registry["supersession_root_parser_output_id"] == pid
    receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (result.receipt_id,)).fetchone()
    assert Decimal(str(receipt["net_paid_amount"])) == Decimal("20.00")


def test_monetary_flag_cleared_by_supersession_then_converts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Guard 13 positive path: monetary flag cleared by a B3 correction."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "flagfix", blocks=conflicting_totals_blocks()
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        # The fixture resolves neither total nor currency nor date, so the
        # correction bundles the human-supplied date alongside the material
        # monetary pair.
        field_updates={
            "amount": "20.00",
            "currency": "SGD",
            "transaction_date": "2026-07-21",
        },
        correction_public_id="rcor_flagfix_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = conn.execute(
        "SELECT public_id FROM parser_outputs WHERE id = ?", (child_id,)
    ).fetchone()["public_id"]
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_flagfix")

    result = convert(conn, command("flagfix", child_public_id, hash_of(conn, child_id)))

    receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (result.receipt_id,)).fetchone()
    assert Decimal(str(receipt["net_paid_amount"])) == Decimal("20.00")
    assert receipt["receipt_datetime"] == "2026-07-21"


def test_raw_intake_pointer_zero_and_repointed_and_double(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "ptr")
    # Distinct OCR material: identical blocks would collide on the extraction
    # fingerprint inside the same staging database.
    other_blocks = (
        _ocr_block(0, "GIANT", line=0, left=10),
        _ocr_block(1, "2026-07-21", line=1, left=10, top=60),
        _ocr_block(2, "TOTAL", line=2, left=10, top=100),
        _ocr_block(3, "S$", line=2, left=80, top=100),
        _ocr_block(4, "45.67", line=2, left=140, top=100),
    )
    other_pid, _other_public = seed_receipt_proposal(
        conn, tmp_path, "ptr_other", blocks=other_blocks
    )

    # TEST-ONLY: migration 035's pointer-lineage trigger now blocks detach
    # and retarget at the schema level (round 3).  Drop it inside this
    # staging database only, to forge the legacy/corrupted pointer states
    # that the service-level lineage guards must still fail closed on.
    conn.execute("DROP TRIGGER trg_raw_intake_records_pointer_lineage_control")

    # Zero-pointer: the raw intake record no longer points at the proposal.
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = NULL WHERE parser_output_id = ?",
        (pid,),
    )
    conn.commit()
    with pytest.raises(StaleConversionTargetError):
        convert(conn, command("ptr_zero", public_id, expected))

    # Repointed-elsewhere behaves identically to zero-pointer for this
    # proposal (its own pointer set is empty).
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ? WHERE public_id = 'raw_ocr_ptr'",
        (other_pid,),
    )
    conn.commit()
    with pytest.raises(StaleConversionTargetError):
        convert(conn, command("ptr_moved", public_id, expected))

    # Two-pointer: ambiguous source binding fails closed as lineage error.
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ? WHERE public_id = 'raw_ocr_ptr'",
        (pid,),
    )
    conn.execute(
        "INSERT INTO raw_intake_records "
        "(public_id, source_type, source_channel, raw_input, received_at, parser_output_id) "
        "VALUES ('raw_ocr_ptr_dup', 'telegram_text', 'telegram', 'dup', "
        "'2026-07-19T15:00:00+00:00', ?)",
        (pid,),
    )
    conn.commit()
    with pytest.raises(ConversionEvidenceLineageError, match="raw intake"):
        convert(conn, command("ptr_two", public_id, expected))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_chain_uniqueness_child_after_parent_converted(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Guard 11: one receipt fact set per supersession chain."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "chain")
    convert(conn, command("chain_parent", public_id, expected))

    # Forge a confirmed child on the already-converted chain by direct SQL
    # (the real B3 boundary would refuse a registry-recorded parent).
    parent = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (pid,)).fetchone()
    cursor = conn.execute(
        "INSERT INTO parser_outputs (public_id, source_type, source_public_id, attachment_id, "
        "parser_name, parser_version, parsed_payload, confidence_score, parse_status, "
        "parent_parser_output_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
        (
            "po_forged_chain",
            parent["source_type"],
            parent["source_public_id"],
            parent["attachment_id"],
            parent["parser_name"],
            parent["parser_version"],
            parent["parsed_payload"],
            parent["confidence_score"],
            pid,
        ),
    )
    child_id = int(cursor.lastrowid or 0)
    extraction_id = conn.execute(
        "SELECT extraction_id FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
        (pid,),
    ).fetchone()["extraction_id"]
    conn.execute(
        "INSERT INTO receipt_ocr_proposal_links (public_id, extraction_id, parser_output_id, "
        "proposal_input_hash, proposal_result_hash, parser_contract_version, link_role) "
        "VALUES ('ropl_forged_chain', ?, ?, ?, ?, 'v1', 'superseding_correction')",
        (extraction_id, child_id, "c" * 64, "d" * 64),
    )
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ? WHERE parser_output_id = ?",
        (child_id, pid),
    )
    child_hash = hash_of(conn, child_id)
    conn.execute(
        "INSERT INTO parser_proposal_authorizations (confirmation_public_id, parser_output_id, "
        "proposal_content_hash, actor_type, authenticated_actor_id, confirmation_state, "
        "confirmation_channel, decided_at) "
        "VALUES ('pca_forged_chain', ?, ?, 'human', 'owner', 'confirmed', 'cli', "
        "'2026-07-25T00:00:00+00:00')",
        (child_id, child_hash),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(ReceiptFactsAlreadyConvertedError, match="supersession chain"):
        convert(conn, command("chain_child", "po_forged_chain", child_hash))

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 8.3a Mutual exclusion with the legacy converter and neighbours
# ---------------------------------------------------------------------------


def test_legacy_converter_rejects_b4_converted_proposal(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mx_legacy")
    convert(conn, command("mx_legacy", public_id, expected))

    # The OCR-link guard fires first, so the typed rejection is the
    # unsupported-type error; either way, zero transactions are created.
    with pytest.raises(UnsupportedProposalTypeError):
        convert_confirmed_parser_proposal(conn, pid)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_b4_conversion_rejected_after_legacy_conversion(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mx_b4")
    forge_legacy_conversion(conn, pid, "mx_b4")
    before = table_counts(conn)

    with pytest.raises(ReceiptFactsAlreadyConvertedError, match="legacy"):
        convert(conn, command("mx_b4", public_id, expected))

    assert table_counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_supersession_rejected_after_b4_conversion(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mx_sup")
    convert(conn, command("mx_sup", public_id, expected))
    before = table_counts(conn)

    with pytest.raises(InvalidSupersessionStatusError, match="receipt conversion registry"):
        supersede_receipt_total_proposal(
            conn,
            pid,
            actor="owner",
            expected_content_hash=expected,
            field_updates={"amount": "44.00"},
            correction_public_id="rcor_mx_sup_1",
        )
    assert table_counts(conn) == before


def test_completion_rejected_after_b4_conversion(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mx_comp")
    convert(conn, command("mx_comp", public_id, expected))
    before = table_counts(conn)

    # The confirmed status is terminal for completion, so the typed status
    # rejection fires before the registry check ever runs.
    with pytest.raises(InvalidCompletionStatusError):
        complete_proposal(
            conn,
            pid,
            actor="owner",
            expected_content_hash=expected,
            field_updates={"merchant": "Sheng Siong"},
            completion_public_id="pco_mx_comp_1",
        )
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Review-fix helpers (payload forgery fixtures for provenance/metadata tests)
# ---------------------------------------------------------------------------


def set_payload_fields(
    conn: sqlite3.Connection, parser_output_id: int, updates: dict[str, Any]
) -> None:
    """Apply raw payload surgery before confirmation (fixture forgery only)."""
    row = conn.execute(
        "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (parser_output_id,)
    ).fetchone()
    payload = json.loads(row["parsed_payload"])
    payload.update(updates)
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), parser_output_id),
    )
    conn.commit()


def insert_amount_table_evidence(
    conn: sqlite3.Connection, parser_output_id: int, value: str
) -> None:
    """Persist a matching amount evidence row so guard 12 reaches guard 13."""
    conn.execute(
        "INSERT INTO parser_proposal_field_evidence ("
        "  parser_output_id, field_name, proposed_value, evidence_source_type"
        ") VALUES (?, 'amount', ?, 'user_message')",
        (parser_output_id, value),
    )
    conn.commit()


def append_receipt_audit_event(
    conn: sqlite3.Connection,
    *,
    event_public_id: str,
    receipt_public_id: str,
    event_type: str,
    payload: dict[str, Any],
    new_state: dict[str, Any],
    causation_public_id: str,
) -> None:
    """Append one committed audit event on a receipt aggregate (fixture only)."""
    conn.execute("BEGIN IMMEDIATE")
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_public_id,
            aggregate_type="receipt",
            aggregate_public_id=receipt_public_id,
            event_type=event_type,
            event_payload=payload,
            new_state=new_state,
            actor_type="human",
            actor_public_id="owner",
            correlation_public_id=receipt_public_id,
            causation_public_id=causation_public_id,
            created_at="2026-07-25T00:00:00.000000Z",
        ),
    )
    conn.commit()


def forge_confirmed_child_without_revision(
    conn: sqlite3.Connection, pid: int, suffix: str
) -> tuple[int, str, str]:
    """Forge a confirmed superseding child with no durable revision row.

    The real B3 boundary always writes ``receipt_proposal_revisions``; this
    fixture reproduces a tampered/legacy edge by direct SQL so guard 13's
    durable-provenance walk can be exercised in isolation.
    """
    parent = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (pid,)).fetchone()
    cursor = conn.execute(
        "INSERT INTO parser_outputs (public_id, source_type, source_public_id, attachment_id, "
        "parser_name, parser_version, parsed_payload, confidence_score, parse_status, "
        "parent_parser_output_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
        (
            f"po_forged_{suffix}",
            parent["source_type"],
            parent["source_public_id"],
            parent["attachment_id"],
            parent["parser_name"],
            parent["parser_version"],
            parent["parsed_payload"],
            parent["confidence_score"],
            pid,
        ),
    )
    child_id = int(cursor.lastrowid or 0)
    extraction_id = conn.execute(
        "SELECT extraction_id FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
        (pid,),
    ).fetchone()["extraction_id"]
    conn.execute(
        "INSERT INTO receipt_ocr_proposal_links (public_id, extraction_id, parser_output_id, "
        "proposal_input_hash, proposal_result_hash, parser_contract_version, link_role) "
        "VALUES (?, ?, ?, ?, ?, 'v1', 'superseding_correction')",
        (f"ropl_forged_{suffix}", extraction_id, child_id, "c" * 64, "d" * 64),
    )
    # Repoint the raw intake and align its lifecycle status with the forged
    # confirmed child so guard 12 reaches the guard 13 provenance walk.
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ?, status = 'confirmed' "
        "WHERE parser_output_id = ?",
        (child_id, pid),
    )
    child_hash = hash_of(conn, child_id)
    conn.execute(
        "INSERT INTO parser_proposal_authorizations (confirmation_public_id, parser_output_id, "
        "proposal_content_hash, actor_type, authenticated_actor_id, confirmation_state, "
        "confirmation_channel, decided_at) "
        "VALUES (?, ?, ?, 'human', 'owner', 'confirmed', 'cli', '2026-07-25T00:00:00+00:00')",
        (f"pca_forged_{suffix}", child_id, child_hash),
    )
    conn.commit()
    return child_id, f"po_forged_{suffix}", child_hash


# ---------------------------------------------------------------------------
# Review fix A (B41-AUDIT-ERROR-MAPPING): audit-chain conflicts are typed
# persistence failures with full rollback
# ---------------------------------------------------------------------------


def test_audit_event_identity_collision_maps_to_persistence_error(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A deterministic event-identity collision rolls back with zero writes."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "audid")
    receipt_public_id = derive_receipt_public_id("rpfc_audid")
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        causation_public_id="rpfc_audid",
    )
    # Occupy the conversion's deterministic event identity with different
    # content before the conversion runs.
    append_receipt_audit_event(
        conn,
        event_public_id=event_id,
        receipt_public_id=receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        payload={"forged": True},
        new_state={"conversion_status": "forged"},
        causation_public_id="rpfc_audid",
    )
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="audit event could not be appended"
    ) as excinfo:
        convert(conn, command("audid", public_id, expected))

    assert isinstance(excinfo.value.__cause__, AuditChainConflictError)
    assert not conn.in_transaction
    assert table_counts(conn) == before

    # The proposal itself was never marked converted: a fresh command
    # identity (fresh receipt aggregate) converts cleanly afterwards.
    result = convert(conn, command("audid_retry", public_id, expected))
    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_audit_chain_head_conflict_maps_to_persistence_error(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A tampered chain head fails closed as ConversionPersistenceError."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "audhead")
    receipt_public_id = derive_receipt_public_id("rpfc_audhead")
    seed_event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type="receipt_forged_seed",
        causation_public_id="rpfc_audhead_seed",
    )
    # Seed a head whose new_state cannot equal the conversion's
    # previous_state, so the chain-head continuity check must fire.
    append_receipt_audit_event(
        conn,
        event_public_id=seed_event_id,
        receipt_public_id=receipt_public_id,
        event_type="receipt_forged_seed",
        payload={"seed": True},
        new_state={"conversion_status": "tampered_head"},
        causation_public_id="rpfc_audhead_seed",
    )
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="audit event could not be appended"
    ) as excinfo:
        convert(conn, command("audhead", public_id, expected))

    assert isinstance(excinfo.value.__cause__, AuditChainConflictError)
    assert not conn.in_transaction
    assert table_counts(conn) == before

    result = convert(conn, command("audhead_retry", public_id, expected))
    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_compatible_but_unrelated_preexisting_head_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Round 3 fix F6: a fresh conversion must be the aggregate's genesis.

    A pre-seeded head whose new_state happens to equal the conversion's
    previous_state would otherwise let the conversion event append as
    sequence 2 on an audit trail this transaction never wrote.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "audgenesis")
    receipt_public_id = derive_receipt_public_id("rpfc_audgenesis")
    seed_event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type="receipt_forged_seed",
        causation_public_id="rpfc_audgenesis_seed",
    )
    # The forged head is state-compatible: its new_state equals the exact
    # previous_state the conversion will claim, so the head-continuity
    # check alone cannot reject it.
    append_receipt_audit_event(
        conn,
        event_public_id=seed_event_id,
        receipt_public_id=receipt_public_id,
        event_type="receipt_forged_seed",
        payload={"seed": True},
        new_state={
            "conversion_status": "not_converted",
            "parse_status": "confirmed",
            "proposal_content_hash": expected,
        },
        causation_public_id="rpfc_audgenesis_seed",
    )
    seed_rows = conn.execute(
        "SELECT * FROM financial_audit_events ORDER BY event_public_id"
    ).fetchall()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="audit event could not be appended"
    ) as excinfo:
        convert(conn, command("audgenesis", public_id, expected))

    assert isinstance(excinfo.value.__cause__, AuditChainConflictError)
    assert not conn.in_transaction
    assert table_counts(conn) == before
    # The pre-seeded chain is untouched: full rollback, no adopted trail.
    assert (
        conn.execute("SELECT * FROM financial_audit_events ORDER BY event_public_id").fetchall()
        == seed_rows
    )


# ---------------------------------------------------------------------------
# Review fix C (B41-DURABLE-FLAG-PROVENANCE): guard 13 verifies durable
# revision/completion evidence, not payload claims
# ---------------------------------------------------------------------------


def test_forged_payload_correction_claim_without_durable_revision_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A payload-only correction claim never clears a monetary flag."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(
        conn, tmp_path, "provclaim", blocks=conflicting_totals_blocks()
    )
    # Forge the amount and a correction claim directly in the payload with
    # no receipt_proposal_revisions row behind it.
    set_payload_fields(
        conn,
        pid,
        {
            "amount": "20.00",
            "correction": {
                "correction_public_id": "rcor_provclaim_forged",
                "corrected_fields": ["amount"],
                "actor_type": "human",
                "superseded_proposal_public_id": "prop_ghost",
            },
        },
    )
    insert_amount_table_evidence(conn, pid, "20.00")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_provclaim")
    before = table_counts(conn)

    with pytest.raises(ConversionEvidenceLineageError, match="claims a correction but no durable"):
        convert(conn, command("provclaim", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_forged_human_field_evidence_without_provenance_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Payload human evidence without durable provenance fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(
        conn, tmp_path, "provhuman", blocks=conflicting_totals_blocks()
    )
    row = conn.execute("SELECT parsed_payload FROM parser_outputs WHERE id = ?", (pid,)).fetchone()
    payload = json.loads(row["parsed_payload"])
    payload["amount"] = "20.00"
    evidence = payload.get("field_evidence")
    assert isinstance(evidence, list)
    # A forged human amount item with neither correction nor completion ids.
    evidence.append(
        {
            "field_name": "amount",
            "proposed_value": "20.00",
            "evidence_source_type": "human",
        }
    )
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), pid),
    )
    conn.commit()
    insert_amount_table_evidence(conn, pid, "20.00")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_provhuman")
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="no verifiable correction or completion provenance",
    ):
        convert(conn, command("provhuman", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_tampered_child_correction_metadata_contradicting_revision_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Child payload correction metadata must equal the durable revision."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "provtamper", blocks=conflicting_totals_blocks()
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        # The conflicting-totals fixture resolves neither total nor currency
        # nor date, so the correction must supply the full monetary triple.
        field_updates={
            "amount": "20.00",
            "currency": "SGD",
            "transaction_date": "2026-07-21",
        },
        correction_public_id="rcor_provtamper_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    # Tamper the durable-claim mirror: the payload now claims fewer corrected
    # fields than the revision row durably recorded.
    set_payload_fields(
        conn,
        child_id,
        {
            "correction": {
                "correction_public_id": "rcor_provtamper_1",
                "corrected_fields": ["amount"],
                "actor_type": "human",
                "superseded_proposal_public_id": "prop_provtamper",
            }
        },
    )
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_provtamper")
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="correction metadata contradicts the durable",
    ):
        convert(conn, command("provtamper", child_public_id, hash_of(conn, child_id)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_forged_supersession_edge_without_revision_row_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A parent-child edge with no durable revision row fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "provedge", blocks=conflicting_totals_blocks()
    )
    child_id, child_public_id, child_hash = forge_confirmed_child_without_revision(
        conn, pid, "provedge"
    )
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="edge has no durable receipt proposal revision evidence",
    ):
        convert(conn, command("provedge", child_public_id, child_hash))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_flags_resolved_by_parent_completion_then_supersession_converts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Positive path: parent completion (date) + supersession (amount).

    The date is resolved on the parent by a durable completion, the amount
    by a durable supersession correction; the confirmed child converts and
    the receipt facts carry both durably resolved values.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "provok", blocks=conflicting_totals_blocks()
    )
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"transaction_date": "2026-07-21"},
        completion_public_id="pco_provok_1",
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        # The supersession boundary requires a valid monetary pair, so the
        # correction carries amount plus currency; the date stays resolved
        # solely by the parent's durable completion above.
        field_updates={"amount": "20.00", "currency": "SGD"},
        correction_public_id="rcor_provok_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_provok")
    before = table_counts(conn)

    result = convert(conn, command("provok", child_public_id, hash_of(conn, child_id)))

    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)
    receipt = conn.execute("SELECT * FROM receipts WHERE id = ?", (result.receipt_id,)).fetchone()
    assert Decimal(str(receipt["net_paid_amount"])) == Decimal("20.00")
    assert receipt["receipt_datetime"] == "2026-07-21"


# ---------------------------------------------------------------------------
# Review fix D (B41-OPTIONAL-METADATA): non-null description/category fail
# closed before any write; absent/null values convert
# ---------------------------------------------------------------------------


def test_null_description_and_category_still_convert(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Explicit null metadata is not a loss: conversion proceeds."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "metanull")
    set_payload_fields(conn, pid, {"description": None, "category": None})
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_metanull")
    before = table_counts(conn)

    result = convert(conn, command("metanull", public_id, hash_of(conn, pid)))

    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_completed_description_fails_closed_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A human-completed description must never be silently dropped."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "metadesc")
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"description": "Team dinner"},
        completion_public_id="pco_metadesc_1",
    )
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_metadesc")
    before = table_counts(conn)

    # The error type is part of the package export surface and the Section 7
    # taxonomy: reviewers and callers catch it as a conversion error.
    assert issubclass(UnsupportedReceiptFactsMetadataError, ReceiptFactsConversionError)
    with pytest.raises(UnsupportedReceiptFactsMetadataError, match="refusing to silently drop"):
        convert(conn, command("metadesc", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_completed_category_fails_closed_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "metacat")
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"category": "Food"},
        completion_public_id="pco_metacat_1",
    )
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_metacat")
    before = table_counts(conn)

    with pytest.raises(UnsupportedReceiptFactsMetadataError, match=r"\['category'\]"):
        convert(conn, command("metacat", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_both_unsupported_metadata_fields_reported_sorted(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "metaboth")
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"description": "Team dinner", "category": "Food"},
        completion_public_id="pco_metaboth_1",
    )
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_metaboth")
    before = table_counts(conn)

    with pytest.raises(
        UnsupportedReceiptFactsMetadataError, match=r"\['category', 'description'\]"
    ):
        convert(conn, command("metaboth", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_exact_replay_takes_priority_over_metadata_rejection(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Guard 4 replay of a registered command precedes the metadata guard."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "metareplay")
    cmd = command("metareplay", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # Post-conversion payload drift adds a non-null description; the exact
    # replay of the registered command must still return idempotently.
    set_payload_fields(conn, pid, {"description": "Added after conversion"})
    before = table_counts(conn)

    replay = convert(conn, cmd)

    assert replay.idempotent is True
    assert replay.receipt_public_id == first.receipt_public_id
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Round 3 fix F3 (replay hardening): guard 4 must not trust the registry row
# alone; exact replay re-verifies the persisted receipt, participants, and
# audit state against the registered result-hash binding and fails closed on
# post-conversion drift (missing/bypassed triggers or historical corruption).
# ---------------------------------------------------------------------------


def test_replay_fails_closed_on_receipt_facts_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Drifted authoritative receipt facts must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpdrift")
    cmd = command("rpdrift", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-3 conversion-bound freeze trigger to forge
    # the historical/bypassed-trigger drift the replay guard must catch.
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipts SET net_paid_amount = '88.88', "
        "net_paid_amount_canonical_text = '99.99' WHERE id = ?",
        (first.receipt_id,),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    # Fail closed only: the replay guard must not silently repair the drift.
    drifted = conn.execute(
        "SELECT net_paid_amount_canonical_text FROM receipts WHERE id = ?",
        (first.receipt_id,),
    ).fetchone()
    assert drifted[0] == "99.99"


def test_replay_fails_closed_on_numeric_mirror_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A drifted NUMERIC compatibility mirror alone must reject the replay.

    The canonical text stays intact; only the legacy ``net_paid_amount``
    mirror loses Decimal-equality.  Replay-time integrity verification must
    treat the broken mirror contract as drift, not return a stale success.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpmirror")
    cmd = command("rpmirror", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-3 conversion-bound freeze trigger to forge
    # historical drift confined to the compatibility mirror column.
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipts SET net_paid_amount = '88.88' WHERE id = ?",
        (first.receipt_id,),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    # Fail closed only: the drifted mirror is never silently repaired.
    drifted = conn.execute(
        "SELECT net_paid_amount, net_paid_amount_canonical_text FROM receipts WHERE id = ?",
        (first.receipt_id,),
    ).fetchone()
    assert drifted[0] == 88.88
    assert drifted[1] == "12.34"


def test_replay_fails_closed_on_participant_membership_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Flipped or deleted membership rows must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpmember")
    cmd = command("rpmember", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-4 bound-membership freeze trigger to forge
    # the historical/bypassed-trigger drift the replay guard must catch.
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET is_included = 0 "
        "WHERE receipt_id = ? AND is_included = 1 "
        "AND participant_id != (SELECT payer_participant_id FROM receipts WHERE id = ?)",
        (first.receipt_id, first.receipt_id),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_deleted_membership_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A deleted membership row is drift, not a stale idempotent success."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpmemdel")
    cmd = command("rpmemdel", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-4 bound-membership delete guard to forge
    # a historically corrupted membership set.
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_no_delete")
    conn.execute(
        "DELETE FROM receipt_participants WHERE receipt_id = ? "
        "AND participant_id != (SELECT payer_participant_id FROM receipts WHERE id = ?)",
        (first.receipt_id, first.receipt_id),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_audit_event_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A missing conversion audit event must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudit")
    cmd = command("rpaudit", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the migration 025 append-only triggers to forge a
    # historically corrupted database whose audit trail was destroyed.
    conn.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
    conn.execute(
        "DELETE FROM financial_audit_events WHERE aggregate_public_id = ?",
        (first.receipt_public_id,),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError,
        match="audit state no longer matches its registered binding",
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_tampered_audit_binding(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An audit event whose payload lost the hash binding rejects replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpauditbind")
    cmd = command("rpauditbind", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the 025 freeze trigger, then break the payload binding
    # without touching the row's hash-chain columns.
    conn.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    conn.execute(
        "UPDATE financial_audit_events "
        "SET event_payload_json = replace(event_payload_json, ?, ?) "
        "WHERE aggregate_public_id = ?",
        (first.conversion_result_hash, "f" * 64, first.receipt_public_id),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(ConversionPersistenceError):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_clean_replay_still_idempotent_after_integrity_checks(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Positive control: an undrifted conversion still replays idempotently."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpclean")
    cmd = command("rpclean", public_id, expected)
    first = convert(conn, cmd)
    before = table_counts(conn)

    replay = convert(conn, cmd)

    assert replay.idempotent is True
    assert replay == dataclasses.replace(first, idempotent=True)
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Round 4 fix R4-F3 (membership Facts): replay must verify the complete
# deterministic membership set - derived membership public IDs, participant
# linkage, role semantics (payer/participant/excluded), inclusion, and row
# count - not only the (participant, is_included) pairs the result hash
# happens to cover.
# ---------------------------------------------------------------------------


def test_replay_fails_closed_on_role_only_membership_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A role-only drift (result hash unchanged) must reject the replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rprole")
    cmd = command("rprole", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-4 freeze trigger; the drift leaves the
    # (participant, is_included) hash material untouched.
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET role = 'observer' "
        "WHERE receipt_id = ? "
        "AND participant_id != (SELECT payer_participant_id FROM receipts WHERE id = ?)",
        (first.receipt_id, first.receipt_id),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    # Fail closed only: the drifted role is never silently repaired.
    drifted = conn.execute(
        "SELECT COUNT(*) FROM receipt_participants WHERE receipt_id = ? AND role = 'observer'",
        (first.receipt_id,),
    ).fetchone()
    assert int(drifted[0]) == 1


def test_replay_fails_closed_on_payer_role_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The payer membership row losing its payer role must reject the replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rppayer")
    cmd = command("rppayer", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET role = 'participant' "
        "WHERE receipt_id = ? "
        "AND participant_id = (SELECT payer_participant_id FROM receipts WHERE id = ?)",
        (first.receipt_id, first.receipt_id),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_membership_public_id_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A forged membership public_id (hash material unchanged) rejects replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpmpid")
    cmd = command("rpmpid", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET public_id = ? "
        "WHERE receipt_id = ? "
        "AND participant_id != (SELECT payer_participant_id FROM receipts WHERE id = ?)",
        (f"rcpp_{'f' * 32}", first.receipt_id, first.receipt_id),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_extra_membership_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An extra membership row injected after conversion rejects the replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpextra")
    cmd = command("rpextra", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-4 bound-membership insert guard.
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_no_insert")
    conn.execute(
        "INSERT INTO receipt_participants "
        "(public_id, receipt_id, participant_id, role, is_included) "
        "VALUES (?, ?, ?, 'participant', 1)",
        (f"rcpp_{'e' * 32}", first.receipt_id, participant_id(conn, "person_bob")),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_membership_replace_rewrite(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A REPLACE rewrite preserving the hash material still rejects replay.

    INSERT OR REPLACE swaps the bound membership row for one with the same
    (receipt_id, participant_id, is_included) but a forged public_id and
    role, so the registered result hash still matches; the full membership
    verification must catch the rewrite.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpmrepl")
    cmd = command("rpmrepl", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    # TEST-ONLY: drop the round-4 insert guards; recursive_triggers stays
    # off so REPLACE's implicit DELETE bypasses the delete guard too.
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_no_insert")
    conn.execute("DROP TRIGGER trg_receipt_participants_no_insert_collision")
    conn.execute(
        "INSERT OR REPLACE INTO receipt_participants "
        "(public_id, receipt_id, participant_id, role, is_included) "
        "VALUES (?, ?, ?, 'observer', 1)",
        (f"rcpp_{'d' * 32}", first.receipt_id, participant_id(conn, "person_alice")),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Round 4 fix R4-F4 (full field replay): replay must rebuild and verify every
# conversion-written receipt/source field from the proposal, raw intake,
# attachment evidence, and the deterministic conversion contract - not only
# the seven fields the registered result hash happens to cover.
# ---------------------------------------------------------------------------


def _frozen_receipt_drift(
    conn: sqlite3.Connection, receipt_id: int, set_sql: str, params: tuple[Any, ...]
) -> None:
    """TEST-ONLY: drop the freeze trigger and drift one frozen column."""
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(f"UPDATE receipts SET {set_sql} WHERE id = ?", (*params, receipt_id))
    conn.commit()


@pytest.mark.parametrize(
    ("set_sql", "params"),
    [
        ("raw_input = ?", ("forged raw input body",)),
        ("source_channel = ?", ("email",)),
        ("attachment_path = ?", ("/forged/receipt.jpg",)),
        ("attachment_id = NULL", ()),
        ("ocr_confidence = ?", (0.01,)),
        ("parser_output_id = NULL", ()),
    ],
)
def test_replay_fails_closed_on_frozen_source_field_drift(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    set_sql: str,
    params: tuple[Any, ...],
) -> None:
    """Result-hash-invisible drift of any frozen source field rejects replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpfield")
    cmd = command("rpfield", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    _frozen_receipt_drift(conn, first.receipt_id, set_sql, params)
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_status_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A conversion-written receipt losing 'confirmed' status rejects replay.

    ``status`` is a lifecycle column (not schema-frozen), but the B4.1
    exclusion contract keeps conversion-bound receipts out of finalization,
    so a drifted status is historical corruption, not a legal transition.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpstatus")
    cmd = command("rpstatus", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    conn.execute("UPDATE receipts SET status = 'draft' WHERE id = ?", (first.receipt_id,))
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_never_written_column_binding(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An extra binding in a facts-only never-written column rejects replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpnever")
    cmd = command("rpnever", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    conn.execute(
        "UPDATE receipts SET notes = 'unexpected binding' WHERE id = ?", (first.receipt_id,)
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Round 4 fix R4-F5 (audit semantic binding): replay must verify the full
# audit event semantics - actor, authorization, references, states,
# correlation, payload - not only chain hash self-consistency.  Every
# tampered event below carries recomputed, valid hashes so the stored chain
# still verifies; only the semantic binding check can catch the drift.
# ---------------------------------------------------------------------------


def _tamper_chain_valid_audit_event(conn: sqlite3.Connection, first: Any, **updates: Any) -> None:
    """TEST-ONLY: rewrite the conversion audit event with drifted semantic
    fields and recomputed valid state/event hashes, so the persisted chain
    stays self-consistent while the semantics no longer match the
    conversion.  Asserts the forged chain still verifies as valid.
    """
    from finance_core.financial_audit import chain as audit_chain

    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=first.receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        causation_public_id=first.command_public_id,
    )
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        event = FinancialAuditRepository(conn).fetch(event_id)
    finally:
        conn.row_factory = previous_factory
    assert event is not None
    tampered = dataclasses.replace(event, **updates)
    previous_state_hash = audit_chain._domain_hash(
        "finance-audit-state-v1", tampered.previous_state_json.encode("utf-8")
    )
    new_state_hash = audit_chain._domain_hash(
        "finance-audit-state-v1", tampered.new_state_json.encode("utf-8")
    )
    tampered = dataclasses.replace(
        tampered,
        previous_state_hash=previous_state_hash,
        new_state_hash=new_state_hash,
    )
    tampered = dataclasses.replace(
        tampered,
        event_hash=audit_chain._event_hash(
            tampered,
            previous_state_hash=previous_state_hash,
            new_state_hash=new_state_hash,
        ),
    )
    tampered.verify()  # the forged row is hash-valid by construction
    conn.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    conn.execute(
        "UPDATE financial_audit_events SET event_payload_json = ?, "
        "previous_state_json = ?, new_state_json = ?, previous_state_hash = ?, "
        "new_state_hash = ?, event_hash = ?, actor_type = ?, "
        "actor_public_id = ?, authorization_public_id = ?, "
        "source_evidence_refs_json = ?, correlation_public_id = ?, "
        "created_at = ? "
        "WHERE event_public_id = ?",
        (
            tampered.event_payload_json,
            tampered.previous_state_json,
            tampered.new_state_json,
            tampered.previous_state_hash,
            tampered.new_state_hash,
            tampered.event_hash,
            tampered.actor_type,
            tampered.actor_public_id,
            tampered.authorization_public_id,
            json.dumps(tampered.source_evidence_references, separators=(",", ":")),
            tampered.correlation_public_id,
            tampered.created_at,
            event_id,
        ),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    try:
        forged_chain = verify_financial_audit_chain(
            conn,
            aggregate_type="receipt",
            aggregate_public_id=first.receipt_public_id,
        )
    finally:
        conn.row_factory = previous_factory
    assert forged_chain.valid and forged_chain.event_count == 1


def _assert_replay_rejects_audit_drift(
    conn: sqlite3.Connection, cmd: ReceiptFactsConversionCommand
) -> None:
    before = table_counts(conn)
    with pytest.raises(
        ConversionPersistenceError,
        match="audit state no longer matches its registered binding",
    ):
        convert(conn, cmd)
    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_replay_fails_closed_on_chain_valid_actor_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid forged actor_public_id must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudactor")
    cmd = command("rpaudactor", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    _tamper_chain_valid_audit_event(conn, first, actor_public_id="mallory")

    _assert_replay_rejects_audit_drift(conn, cmd)
    # The drift is reported, never silently repaired.
    row = conn.execute(
        "SELECT actor_public_id FROM financial_audit_events WHERE aggregate_public_id = ?",
        (first.receipt_public_id,),
    ).fetchone()
    assert row[0] == "mallory"


def test_replay_fails_closed_on_chain_valid_authorization_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid forged authorization binding must reject the replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudauth")
    cmd = command("rpaudauth", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    _tamper_chain_valid_audit_event(conn, first, authorization_public_id="rppa_forged_confirmation")

    _assert_replay_rejects_audit_drift(conn, cmd)


def test_replay_fails_closed_on_chain_valid_source_reference_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid drifted source evidence reference set rejects replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudrefs")
    cmd = command("rpaudrefs", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        event = FinancialAuditRepository(conn).fetch(
            derive_audit_event_public_id(
                aggregate_type="receipt",
                aggregate_public_id=first.receipt_public_id,
                event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
                causation_public_id=first.command_public_id,
            )
        )
    finally:
        conn.row_factory = prev_factory
    assert event is not None
    forged_references = tuple(
        reference
        for reference in event.source_evidence_references
        if not reference.startswith("confirmation:")
    )
    assert len(forged_references) == len(event.source_evidence_references) - 1
    _tamper_chain_valid_audit_event(conn, first, source_evidence_references=forged_references)

    _assert_replay_rejects_audit_drift(conn, cmd)


def test_replay_fails_closed_on_chain_valid_state_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid drifted new_state must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudstate")
    cmd = command("rpaudstate", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        event = FinancialAuditRepository(conn).fetch(
            derive_audit_event_public_id(
                aggregate_type="receipt",
                aggregate_public_id=first.receipt_public_id,
                event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
                causation_public_id=first.command_public_id,
            )
        )
    finally:
        conn.row_factory = prev_factory
    assert event is not None
    forged_state = json.loads(event.new_state_json)["value"]
    forged_state["conversion_status"] = "converted_then_reopened"
    _tamper_chain_valid_audit_event(conn, first, new_state_json=canonical_json_text(forged_state))

    _assert_replay_rejects_audit_drift(conn, cmd)


def test_replay_fails_closed_on_chain_valid_previous_state_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid drifted previous_state must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudprev")
    cmd = command("rpaudprev", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        event = FinancialAuditRepository(conn).fetch(
            derive_audit_event_public_id(
                aggregate_type="receipt",
                aggregate_public_id=first.receipt_public_id,
                event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
                causation_public_id=first.command_public_id,
            )
        )
    finally:
        conn.row_factory = prev_factory
    assert event is not None
    forged_state = json.loads(event.previous_state_json)["value"]
    forged_state["parse_status"] = "draft"
    _tamper_chain_valid_audit_event(
        conn, first, previous_state_json=canonical_json_text(forged_state)
    )

    _assert_replay_rejects_audit_drift(conn, cmd)


def test_replay_fails_closed_on_chain_valid_correlation_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid forged correlation binding must reject the replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudcorr")
    cmd = command("rpaudcorr", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    _tamper_chain_valid_audit_event(conn, first, correlation_public_id="rcpt_" + "f" * 32)

    _assert_replay_rejects_audit_drift(conn, cmd)


def test_replay_fails_closed_on_chain_valid_payload_proposal_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid forged payload proposal binding must reject the replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudpayl")
    cmd = command("rpaudpayl", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        event = FinancialAuditRepository(conn).fetch(
            derive_audit_event_public_id(
                aggregate_type="receipt",
                aggregate_public_id=first.receipt_public_id,
                event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
                causation_public_id=first.command_public_id,
            )
        )
    finally:
        conn.row_factory = prev_factory
    assert event is not None
    forged_payload = json.loads(event.event_payload_json)["value"]
    forged_payload["proposal_public_id"] = "po_forged_proposal"
    _tamper_chain_valid_audit_event(
        conn, first, event_payload_json=canonical_json_text(forged_payload)
    )

    _assert_replay_rejects_audit_drift(conn, cmd)


# ---------------------------------------------------------------------------
# Review fix G (B41-SOURCE-LINEAGE): telegram source binding revalidation
# and lineage drift fail-closed
# ---------------------------------------------------------------------------


def test_second_telegram_source_binding_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two telegram source bindings for one attachment are ambiguous."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "srcdup")
    intake = conn.execute(
        "SELECT id FROM raw_intake_records WHERE parser_output_id = ?", (pid,)
    ).fetchone()
    binding = conn.execute(
        "SELECT * FROM telegram_attachment_source WHERE raw_intake_record_id = ?",
        (int(intake["id"]),),
    ).fetchone()
    assert binding is not None
    # A second binding row requires a second raw intake record because of the
    # UNIQUE (attachment_id, raw_intake_record_id) constraint.  The extra
    # intake record carries no parser pointer so guard 12's single-pointer
    # check stays satisfied and the binding-cardinality check must fire.
    cursor = conn.execute(
        "INSERT INTO raw_intake_records "
        "(public_id, source_type, source_channel, raw_input, received_at) "
        "VALUES ('raw_srcdup_dup', 'telegram_text', 'telegram', 'dup', "
        "'2026-07-19T15:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO telegram_attachment_source (public_id, attachment_id, "
        "raw_intake_record_id, original_attachment_path, observed_file_size, "
        "content_hash, source_evidence_payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "tas_srcdup_dup",
            binding["attachment_id"],
            cursor.lastrowid,
            binding["original_attachment_path"],
            binding["observed_file_size"],
            binding["content_hash"],
            binding["source_evidence_payload"],
        ),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="Expected exactly one telegram attachment source binding",
    ):
        convert(conn, command("srcdup", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_frozen_raw_intake_hash_update_rejected_by_schema_then_converts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Migration 031 freezes the raw intake hash while evidence exists."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "srcfreeze")

    with pytest.raises(
        sqlite3.IntegrityError, match="immutable once Telegram source evidence exists"
    ):
        conn.execute(
            "UPDATE raw_intake_records SET attachment_hash = ? WHERE parser_output_id = ?",
            ("e" * 64, pid),
        )
    conn.rollback()

    # The frozen lineage remains intact, so the conversion still succeeds.
    result = convert(conn, command("srcfreeze", public_id, expected))
    assert result.idempotent is False


def test_historical_raw_intake_hash_drift_fails_closed_without_trigger(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The service revalidates the hash chain; it does not lean on triggers."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "srchash")
    # Simulate pre-031 historical drift: rows written before the freeze
    # trigger existed may disagree with the binding, so drop the trigger to
    # prove the service-level check fails closed on its own.
    conn.execute("DROP TRIGGER trg_raw_intake_no_change_hash_when_telegram_source")
    conn.execute(
        "UPDATE raw_intake_records SET attachment_hash = ? WHERE parser_output_id = ?",
        ("e" * 64, pid),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="raw intake attachment hash does not match",
    ):
        convert(conn, command("srchash", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_historical_raw_intake_path_drift_fails_closed_without_trigger(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "srcpath")
    conn.execute("DROP TRIGGER trg_raw_intake_no_change_path_when_telegram_source")
    conn.execute(
        "UPDATE raw_intake_records SET attachment_path = ? WHERE parser_output_id = ?",
        ("/forged/srcpath.jpg", pid),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="raw intake attachment path does not match",
    ):
        convert(conn, command("srcpath", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_non_telegram_source_channel_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Channel drift on the raw intake record breaks the telegram lineage."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "srcchan")
    conn.execute(
        "UPDATE raw_intake_records SET source_channel = 'manual' WHERE parser_output_id = ?",
        (pid,),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(ConversionEvidenceLineageError, match="source channel to be"):
        convert(conn, command("srcchan", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_missing_source_identity_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A proposal without a source identity has no verifiable lineage."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "srcident")
    # The content hash binds source_public_id, so the surgery must happen
    # before confirmation for the triple hash equality to stay green.
    conn.execute("UPDATE parser_outputs SET source_public_id = NULL WHERE id = ?", (pid,))
    conn.commit()
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_srcident")
    before = table_counts(conn)

    with pytest.raises(ConversionEvidenceLineageError, match="source identity is missing"):
        convert(conn, command("srcident", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Second-round review fixes: strict flag validation (F3), durable value
# binding (F4), evidence cardinality (F5), canonical monetary text (F8),
# and fresh-audit adoption refusal (F2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_flags",
    [{"date": True}, "conflicting_totals", 7],
    ids=["dict", "string", "int"],
)
def test_non_list_ambiguity_flags_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, bad_flags: Any
) -> None:
    """Guard 13 requires a list shape; any other payload shape fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "flagshape")
    set_payload_fields(conn, pid, {"ambiguity_flags": bad_flags})
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagshape")
    before = table_counts(conn)

    with pytest.raises(AmbiguousReceiptInputError, match="must be a list"):
        convert(conn, command("flagshape", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


@pytest.mark.parametrize(
    "bad_flags",
    [["flag_from_the_future"], [42], [None]],
    ids=["unknown_token", "non_string", "none_entry"],
)
def test_unrecognized_ambiguity_flag_tokens_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, bad_flags: list[Any]
) -> None:
    """Unknown or non-string flag tokens fail closed instead of passing."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "flagtoken")
    set_payload_fields(conn, pid, {"ambiguity_flags": bad_flags})
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagtoken")
    before = table_counts(conn)

    with pytest.raises(AmbiguousReceiptInputError, match="malformed or unrecognized"):
        convert(conn, command("flagtoken", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Round 3 fix F4: ambiguity_flags must be explicitly present as a JSON list;
# top-level null and a missing key fail closed (design Section 12.4).
# ---------------------------------------------------------------------------


def test_null_ambiguity_flags_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A top-level null is not the explicit empty list; it fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "flagnull")
    set_payload_fields(conn, pid, {"ambiguity_flags": None})
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagnull")
    before = table_counts(conn)

    with pytest.raises(AmbiguousReceiptInputError, match="must be a list"):
        convert(conn, command("flagnull", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_missing_ambiguity_flags_key_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A payload without the ambiguity_flags key fails closed pre-write."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "flagmissing")
    row = conn.execute("SELECT parsed_payload FROM parser_outputs WHERE id = ?", (pid,)).fetchone()
    payload = json.loads(row["parsed_payload"])
    del payload["ambiguity_flags"]
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), pid),
    )
    conn.commit()
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagmissing")
    before = table_counts(conn)

    with pytest.raises(AmbiguousReceiptInputError, match="explicit ambiguity_flags"):
        convert(conn, command("flagmissing", public_id, hash_of(conn, pid)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_explicit_empty_ambiguity_flags_positive_control(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The explicit empty list remains the valid no-flags representation."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id = seed_receipt_proposal(conn, tmp_path, "flagempty")
    payload = json.loads(
        conn.execute("SELECT parsed_payload FROM parser_outputs WHERE id = ?", (pid,)).fetchone()[
            "parsed_payload"
        ]
    )
    assert payload["ambiguity_flags"] == []
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_flagempty")

    result = convert(conn, command("flagempty", public_id, hash_of(conn, pid)))

    assert result.idempotent is False


def test_inherited_completion_evidence_value_binding_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A child date drifted away from inherited completion evidence fails closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "bindval", blocks=ambiguous_date_blocks()
    )
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"transaction_date": "2026-06-05"},
        completion_public_id="pco_bindval_1",
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"amount": "20.00", "currency": "SGD"},
        correction_public_id="rcor_bindval_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    # Drift only the child payload date: the inherited human evidence item
    # still claims the durably completed 2026-06-05, so the effective-value
    # binding on the evidence item must fail closed.
    set_payload_fields(conn, child_id, {"transaction_date": "2026-06-06"})
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_bindval")
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="does not match the effective proposal value",
    ):
        convert(conn, command("bindval", child_public_id, hash_of(conn, child_id)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_tampered_inherited_completion_evidence_value_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An inherited evidence value contradicting the durable completion fails."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "binddur", blocks=ambiguous_date_blocks()
    )
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"transaction_date": "2026-06-05"},
        completion_public_id="pco_binddur_1",
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={"amount": "20.00", "currency": "SGD"},
        correction_public_id="rcor_binddur_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    # Tamper the inherited human evidence item and the payload date in
    # lockstep: the claim is internally consistent but contradicts the
    # durable parser_proposal_completions row it references.
    row = conn.execute(
        "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (child_id,)
    ).fetchone()
    payload = json.loads(row["parsed_payload"])
    payload["transaction_date"] = "2026-06-06"
    for item in payload.get("field_evidence", []):
        if (
            item.get("field_name") == "transaction_date"
            and item.get("completion_public_id") is not None
        ):
            item["proposed_value"] = "2026-06-06"
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), child_id),
    )
    conn.commit()
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_binddur")
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match="contradicts .* durable completion record",
    ):
        convert(conn, command("binddur", child_public_id, hash_of(conn, child_id)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


@pytest.mark.parametrize("field", ["amount", "currency"])
def test_missing_monetary_evidence_row_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, field: str
) -> None:
    """Zero persisted evidence rows for an effective monetary field fail closed."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, f"noev{field}")
    conn.execute(
        "DELETE FROM parser_proposal_field_evidence WHERE parser_output_id = ? AND field_name = ?",
        (pid, field),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(
        ConversionEvidenceLineageError,
        match=f"effective {field} has no persisted field evidence row",
    ):
        convert(conn, command(f"noev{field}", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_canonical_amount_text_persisted_byte_exact(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The authoritative canonical text column round-trips byte-identically."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "canon")
    result = convert(conn, command("canon", public_id, expected))

    row = conn.execute(
        "SELECT net_paid_amount, net_paid_amount_canonical_text FROM receipts WHERE id = ?",
        (result.receipt_id,),
    ).fetchone()
    assert row["net_paid_amount_canonical_text"] == "12.34"
    assert Decimal(str(row["net_paid_amount"])) == Decimal("12.34")

    # An independent fresh connection sees the same exact TEXT bytes: the
    # column is authoritative for downstream readers (B4.2).
    db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    fresh = sqlite3.connect(db_path)
    try:
        stored = fresh.execute(
            "SELECT net_paid_amount_canonical_text FROM receipts WHERE id = ?",
            (result.receipt_id,),
        ).fetchone()[0]
    finally:
        fresh.close()
    assert isinstance(stored, str)
    assert stored == "12.34"


def test_amount_beyond_exact_persistence_boundary_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """16 significant digits lose Decimal equality through NUMERIC affinity."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "digits16", blocks=conflicting_totals_blocks()
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={
            "amount": "99999999999999.99",
            "currency": "SGD",
            "transaction_date": "2026-07-21",
        },
        correction_public_id="rcor_digits16_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_digits16")
    before = table_counts(conn)

    with pytest.raises(IncompleteReceiptInputsError, match="cannot be mirrored losslessly"):
        convert(conn, command("digits16", child_public_id, hash_of(conn, child_id)))

    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_amount_at_exact_persistence_boundary_converts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """15 significant digits are exactly representable and convert cleanly."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "digits15", blocks=conflicting_totals_blocks()
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={
            "amount": "9999999999999.99",
            "currency": "SGD",
            "transaction_date": "2026-07-21",
        },
        correction_public_id="rcor_digits15_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_digits15")

    result = convert(conn, command("digits15", child_public_id, hash_of(conn, child_id)))

    row = conn.execute(
        "SELECT net_paid_amount, net_paid_amount_canonical_text FROM receipts WHERE id = ?",
        (result.receipt_id,),
    ).fetchone()
    assert row["net_paid_amount_canonical_text"] == "9999999999999.99"
    assert Decimal(str(row["net_paid_amount"])) == Decimal("9999999999999.99")

    # Round 3 fix F7: a reconnected reader sees a NUMERIC mirror that is
    # still Decimal-equal to the authoritative canonical text.
    db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    fresh = sqlite3.connect(db_path)
    try:
        mirrored, canonical = fresh.execute(
            "SELECT net_paid_amount, net_paid_amount_canonical_text FROM receipts WHERE id = ?",
            (result.receipt_id,),
        ).fetchone()
    finally:
        fresh.close()
    assert canonical == "9999999999999.99"
    assert Decimal(repr(mirrored)) == Decimal("9999999999999.99")


# ---------------------------------------------------------------------------
# Round 3 fix F7: the NUMERIC compatibility boundary is decided by SQLite's
# own NUMERIC affinity (CAST prediction + one explicit mirror-decoding rule),
# not by an arbitrary significant-digit constant.  Unsupported amounts fail
# closed before any write.
# ---------------------------------------------------------------------------


def test_high_exponent_trailing_zero_amount_rejected_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A trailing-zero amount that SQLite mirrors as scientific notation.

    ``100000000000000000000000.00`` normalizes to one significant digit, so
    a digit-count guard passes it; SQLite NUMERIC affinity stores ``1e+23``
    and the canonical/legacy pair silently diverges.  The affinity-based
    guard must reject it before any write instead of relying on the
    post-write ``_verify_persisted`` rollback.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id = seed_receipt_proposal(
        conn, tmp_path, "hiexp", blocks=conflicting_totals_blocks()
    )
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=hash_of(conn, pid),
        field_updates={
            "amount": "100000000000000000000000.00",
            "currency": "SGD",
            "transaction_date": "2026-07-21",
        },
        correction_public_id="rcor_hiexp_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_hiexp")
    before = table_counts(conn)

    with pytest.raises(IncompleteReceiptInputsError, match="cannot be mirrored losslessly"):
        convert(conn, command("hiexp", child_public_id, hash_of(conn, child_id)))

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_numeric_mirror_decoding_rule_direct(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The mirror-decoding rule is explicit: int exact, plain-repr float only."""
    from finance_core.parser_proposals.receipt_facts_conversion import decimal_from_numeric_mirror

    # INTEGER storage decodes exactly.
    assert decimal_from_numeric_mirror(12) == Decimal("12")
    assert decimal_from_numeric_mirror(100000000000000000) == Decimal("100000000000000000")
    # REAL storage decodes only through a plain (non-scientific) repr.
    assert decimal_from_numeric_mirror(12.34) == Decimal("12.34")
    assert decimal_from_numeric_mirror(9999999999999.99) == Decimal("9999999999999.99")
    # Scientific-notation reprs cannot be trusted as canonical amounts.
    assert decimal_from_numeric_mirror(1e23) is None
    # Non-finite mirrors fail closed (overflow/Infinity boundary).
    assert decimal_from_numeric_mirror(float("inf")) is None
    assert decimal_from_numeric_mirror(float("-inf")) is None
    assert decimal_from_numeric_mirror(float("nan")) is None
    # Any other storage class is not a valid NUMERIC mirror of our write.
    assert decimal_from_numeric_mirror(True) is None
    assert decimal_from_numeric_mirror("12.34") is None
    assert decimal_from_numeric_mirror(None) is None


def test_exact_representation_guard_uses_sqlite_affinity_prediction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The pre-write guard consults SQLite itself, including overflow to Inf."""
    from finance_core.parser_proposals.receipt_facts_conversion import (
        _require_exact_monetary_representation,
    )

    conn = migrated_temp_db_connection
    # Positive controls: exact INTEGER and exact REAL mirrors.
    _require_exact_monetary_representation(conn, "12.34")
    _require_exact_monetary_representation(conn, "12.00")
    _require_exact_monetary_representation(conn, "9999999999999.99")
    # 16 significant digits lose Decimal equality through binary64.
    with pytest.raises(IncompleteReceiptInputsError, match="cannot be mirrored losslessly"):
        _require_exact_monetary_representation(conn, "99999999999999.99")
    # High-exponent trailing-zero amounts mirror as scientific notation.
    with pytest.raises(IncompleteReceiptInputsError, match="cannot be mirrored losslessly"):
        _require_exact_monetary_representation(conn, "100000000000000000000000.00")
    # Overflow past binary64 mirrors as Infinity and must fail closed.
    with pytest.raises(IncompleteReceiptInputsError, match="cannot be mirrored losslessly"):
        _require_exact_monetary_representation(conn, "9" + "0" * 999 + ".00")


def test_fresh_conversion_refuses_preexisting_identical_audit_event(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Even a byte-identical pre-seeded audit event is refused, not adopted."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "preaud")
    receipt_public_id = derive_receipt_public_id("rpfc_preaud")
    sorted_entries = sorted(
        (
            {"participant_public_id": p, "is_included": i}
            for p, i in (("person_owner", 1), ("person_alice", 1))
        ),
        key=lambda item: str(item["participant_public_id"]),
    )
    material = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "command_public_id": "rpfc_preaud",
        "proposal_public_id": public_id,
        "expected_content_hash": expected,
        "payer_participant_public_id": "person_owner",
        "participants": sorted_entries,
        "authenticated_actor_id": "owner",
        "actor_type": "human",
        "channel": "cli",
    }
    material_hash = _sha(_canonical_json(material))
    result_material = {
        "receipt_public_id": receipt_public_id,
        "merchant": "COLD STORAGE",
        "receipt_date": "2026-07-20",
        "net_paid_amount": "12.34",
        "currency": "SGD",
        "payer_participant_public_id": "person_owner",
        "participants": sorted_entries,
        "attachment_content_hash": _hash(attachment_content("preaud")),
        "proposal_content_hash": expected,
        "command_material_hash": material_hash,
    }
    result_hash = _sha(_canonical_json(result_material))
    proposal = conn.execute(
        "SELECT source_public_id, attachment_id FROM parser_outputs WHERE id = ?",
        (pid,),
    ).fetchone()
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
        causation_public_id="rpfc_preaud",
    )
    # Pre-seed the exact event the conversion itself would append.  A fresh
    # conversion must refuse to adopt it (idempotent replay is guard 4's
    # job, and guard 4 sees no registry row here).
    conn.execute("BEGIN IMMEDIATE")
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="receipt",
            aggregate_public_id=receipt_public_id,
            event_type=RECEIPT_FACTS_CONVERSION_EVENT_TYPE,
            event_payload={
                "command_public_id": "rpfc_preaud",
                "proposal_public_id": public_id,
                "confirmation_public_id": "pca_preaud",
                "proposal_content_hash": expected,
                "command_material_hash": material_hash,
                "conversion_result_hash": result_hash,
            },
            previous_state={
                "conversion_status": "not_converted",
                "parse_status": "confirmed",
                "proposal_content_hash": expected,
            },
            new_state={
                "conversion_status": "converted_to_receipt_facts",
                "receipt_public_id": receipt_public_id,
                "proposal_content_hash": expected,
                "conversion_result_hash": result_hash,
            },
            actor_type="human",
            actor_public_id="owner",
            authorization_public_id="pca_preaud",
            source_evidence_references=(
                f"parser-output:{public_id}",
                f"source:{proposal['source_public_id']}",
                f"attachment-id:{proposal['attachment_id']}",
                "extraction:rocr_preaud",
                "confirmation:pca_preaud",
            ),
            correlation_public_id=receipt_public_id,
            causation_public_id="rpfc_preaud",
            created_at="2026-07-25T00:00:00.000000Z",
        ),
    )
    conn.commit()
    before = table_counts(conn)

    with pytest.raises(ConversionPersistenceError, match="found its audit event already recorded"):
        convert(conn, command("preaud", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# Round 5 Fix 1 (Guard 4 registry-first lookup): the durable registry alone
# decides found/not-found for a command_public_id.  When dependent facts or
# evidence rows have been destroyed out-of-band, changed material must still
# surface the idempotency conflict first, and same-material replays must
# fail closed as replay drift - never fall through to guards 5+ and degrade
# into ReceiptFactsAlreadyConvertedError or a proposal-not-found error.
# ---------------------------------------------------------------------------


def _registry_row_snapshot(conn: sqlite3.Connection, command_public_id: str) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT * FROM receipt_proposal_conversions WHERE command_public_id = ?",
        (command_public_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _corrupt_delete_receipt(conn: sqlite3.Connection, receipt_id: int) -> None:
    """TEST-ONLY corruption: destroy the conversion-written receipt row.

    Drops the migration 035 delete backstops and disables FK enforcement
    for the deletion only, simulating historical out-of-band damage.
    """
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_no_delete")
    conn.execute("DROP TRIGGER trg_receipt_participants_conversion_bound_no_delete")
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("DELETE FROM receipt_participants WHERE receipt_id = ?", (receipt_id,))
        conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def _corrupt_delete_proposal(conn: sqlite3.Connection, parser_output_id: int) -> None:
    """TEST-ONLY corruption: destroy the registered parser output row."""
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("DELETE FROM parser_outputs WHERE id = ?", (parser_output_id,))
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_registry_survives_missing_receipt_same_material_fails_replay_integrity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Registry present + receipt destroyed + same material = replay drift."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "regmissr")
    cmd = command("regmissr", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    _corrupt_delete_receipt(conn, first.receipt_id)
    registry_before = _registry_row_snapshot(conn, "rpfc_regmissr")
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert _registry_row_snapshot(conn, "rpfc_regmissr") == registry_before


def test_registry_survives_missing_receipt_changed_material_conflicts_first(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Registry present + receipt destroyed + changed material = conflict."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "regmisrc")
    first = convert(conn, command("regmisrc", public_id, expected))
    assert first.idempotent is False

    _corrupt_delete_receipt(conn, first.receipt_id)
    registry_before = _registry_row_snapshot(conn, "rpfc_regmisrc")
    before = table_counts(conn)

    with pytest.raises(ConversionIdempotencyConflictError, match="different canonical material"):
        convert(
            conn,
            command(
                "regmisrc",
                public_id,
                expected,
                participants=entries(("person_owner", 1), ("person_alice", 0)),
            ),
        )

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert _registry_row_snapshot(conn, "rpfc_regmisrc") == registry_before


def test_registry_survives_missing_proposal_same_material_fails_replay_integrity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Registry present + proposal destroyed + same material = replay drift."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "regmissp")
    cmd = command("regmissp", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    _corrupt_delete_proposal(conn, pid)
    registry_before = _registry_row_snapshot(conn, "rpfc_regmissp")
    before = table_counts(conn)

    with pytest.raises(
        ConversionPersistenceError, match="no longer matches its registered result binding"
    ):
        convert(conn, cmd)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert _registry_row_snapshot(conn, "rpfc_regmissp") == registry_before


def test_registry_survives_missing_proposal_changed_material_conflicts_first(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Registry present + proposal destroyed + changed material = conflict."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "regmispc")
    first = convert(conn, command("regmispc", public_id, expected))
    assert first.idempotent is False

    _corrupt_delete_proposal(conn, pid)
    registry_before = _registry_row_snapshot(conn, "rpfc_regmispc")
    before = table_counts(conn)

    with pytest.raises(ConversionIdempotencyConflictError, match="different canonical material"):
        convert(
            conn,
            command(
                "regmispc",
                public_id,
                expected,
                payer_participant_public_id="person_alice",
            ),
        )

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert _registry_row_snapshot(conn, "rpfc_regmispc") == registry_before


# ---------------------------------------------------------------------------
# Round 5 Fix 2 (audit created_at semantic binding): the fresh conversion
# writes the registry row and the audit event with one shared timestamp, so
# replay must require the persisted audit created_at to still equal the
# registry created_at byte-exactly.  A recomputed event hash keeps the chain
# valid, so only the semantic binding can catch timestamp drift.
# ---------------------------------------------------------------------------


def test_replay_fails_closed_on_chain_valid_audit_created_at_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A chain-valid forged audit created_at must reject the exact replay."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rpaudtime")
    cmd = command("rpaudtime", public_id, expected)
    first = convert(conn, cmd)
    assert first.idempotent is False

    registry_created_at = conn.execute(
        "SELECT created_at FROM receipt_proposal_conversions WHERE command_public_id = ?",
        ("rpfc_rpaudtime",),
    ).fetchone()[0]
    drifted = "2020-01-01T00:00:00.000000Z"
    assert drifted != registry_created_at
    # The tamper helper recomputes valid state/event hashes and asserts the
    # forged chain still verifies, so only the semantic binding remains.
    _tamper_chain_valid_audit_event(conn, first, created_at=drifted)

    _assert_replay_rejects_audit_drift(conn, cmd)
    # The drift is reported, never silently repaired.
    row = conn.execute(
        "SELECT created_at FROM financial_audit_events WHERE aggregate_public_id = ?",
        (first.receipt_public_id,),
    ).fetchone()
    assert row[0] == drifted


# ---------------------------------------------------------------------------
# Round 6 fix (irreplayable conversion timestamps): the registry row and the
# audit event share one canonical UTC timestamp and the replay audit binding
# compares them byte-exactly, so `_now(clock)` must normalize any injected
# clock output to the audit chain's canonical form (microseconds, 'Z')
# before anything is written, and fail closed on invalid values.  A fresh
# conversion that commits must always be exactly replayable.
# ---------------------------------------------------------------------------

_CANONICAL_CLOCK_TEXT = "2026-01-02T03:04:05.000000Z"


def _conversion_created_at_pair(
    conn: sqlite3.Connection, command_public_id: str, receipt_public_id: str
) -> tuple[str, str]:
    registry = conn.execute(
        "SELECT created_at FROM receipt_proposal_conversions WHERE command_public_id = ?",
        (command_public_id,),
    ).fetchone()
    audit = conn.execute(
        "SELECT created_at FROM financial_audit_events WHERE aggregate_public_id = ?",
        (receipt_public_id,),
    ).fetchone()
    assert registry is not None and audit is not None
    return str(registry[0]), str(audit[0])


def test_offset_utc_clock_normalized_and_replayable(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A '+00:00' clock is canonicalized; registry and audit stay byte-exact."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "clkoffz")
    cmd = command("clkoffz", public_id, expected)

    first = convert_confirmed_receipt_proposal_to_facts(
        conn, cmd, clock=lambda: "2026-01-02T03:04:05+00:00"
    )
    assert first.idempotent is False

    registry_ts, audit_ts = _conversion_created_at_pair(
        conn, "rpfc_clkoffz", first.receipt_public_id
    )
    assert registry_ts == _CANONICAL_CLOCK_TEXT
    assert audit_ts == _CANONICAL_CLOCK_TEXT

    # A committed fresh conversion must always be exactly replayable.
    replayed = convert(conn, cmd)
    assert replayed.idempotent is True
    assert replayed.conversion_result_hash == first.conversion_result_hash


def test_non_utc_offset_clock_converted_to_canonical_utc(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A legal non-UTC offset is converted to canonical UTC text."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "clksgt")
    cmd = command("clksgt", public_id, expected)

    first = convert_confirmed_receipt_proposal_to_facts(
        conn, cmd, clock=lambda: "2026-01-02T11:04:05+08:00"
    )
    assert first.idempotent is False

    registry_ts, audit_ts = _conversion_created_at_pair(
        conn, "rpfc_clksgt", first.receipt_public_id
    )
    assert registry_ts == _CANONICAL_CLOCK_TEXT
    assert audit_ts == _CANONICAL_CLOCK_TEXT

    replayed = convert(conn, cmd)
    assert replayed.idempotent is True


@pytest.mark.parametrize(
    "bad_value",
    ["not-a-timestamp", "2026-01-02T03:04:05", 123],
    ids=["invalid-string", "timezone-naive", "non-string"],
)
def test_invalid_clock_values_rejected_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, bad_value: Any
) -> None:
    """Invalid, naive, or non-string clock output fails closed, zero writes."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "clkbad")
    cmd = command("clkbad", public_id, expected)
    before = table_counts(conn)

    with pytest.raises(ConversionPersistenceError, match="[Cc]onversion clock") as excinfo:
        convert_confirmed_receipt_proposal_to_facts(conn, cmd, clock=lambda: bad_value)

    if isinstance(bad_value, str) and bad_value == "not-a-timestamp":
        assert isinstance(excinfo.value.__cause__, ValueError)
    assert not conn.in_transaction
    assert table_counts(conn) == before


def test_raising_clock_rolls_back_completely(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A clock callable that raises leaves the transaction fully rolled back."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "clkboom")
    cmd = command("clkboom", public_id, expected)
    before = table_counts(conn)

    def _boom() -> str:
        raise RuntimeError("clock backend unavailable")

    with pytest.raises(RuntimeError, match="clock backend unavailable"):
        convert_confirmed_receipt_proposal_to_facts(conn, cmd, clock=_boom)

    assert not conn.in_transaction
    assert table_counts(conn) == before
