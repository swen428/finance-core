"""Focused tests for ingesting receipt OCR evidence into total-level proposals.

These tests exercise migration 033 constraints, the deterministic total-level
ingestion service, the OCR-bound content-hash contract, and the idempotency,
conflict, concurrency, and crash-replay guarantees.  All fixtures are
synthetic and privacy-safe; no live database, seed data, or cloud model is
touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import finance_core.intake.receipt_ocr_proposal as proposal_module
from finance_core.intake.attachment_evidence import persist_attachment_evidence
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.intake.receipt_ocr_proposal import (
    InvalidProposalCommandError,
    OcrExtractionNotFoundError,
    ProposalCallerOwnedTransactionError,
    ProposalDuplicateInitialError,
    ProposalIdempotencyConflictError,
    ProposalSourceBindingConflictError,
    ProposalStagingDatabaseRejectedError,
    ProposalUnexpectedPersistenceError,
    ReceiptTotalProposalIngestionResult,
    UnusableOcrEvidenceError,
    ingest_receipt_ocr_evidence_as_total_expense_proposal,
)
from finance_core.parser_proposals.content_hash import (
    _attachment_evidence,
    _canonical_amount,
    _canonical_currency,
    compute_proposal_content_hash,
)
from finance_core.parser_proposals.receipt_total_parser import (
    FLAG_AMBIGUOUS_CURRENCY_SYMBOL,
    FLAG_AMBIGUOUS_DATE,
    FLAG_CONFLICTING_TOTALS,
    FLAG_OCR_ENGINE_FAILED,
    FLAG_OCR_NO_TEXT,
    FLAG_OCR_RESOURCE_REJECTED,
    FLAG_OCR_UNSUPPORTED_INPUT,
    FLAG_TOTAL_AMOUNT_INVALID,
    FLAG_TOTAL_NOT_FOUND,
    FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT,
    PARSER_CONTRACT_VERSION_DEFAULT,
)
from tests.conftest import connect_temp_db

JPEG = b"\xff\xd8\xff" + b"receipt-jpeg-evidence"
PDF = b"%PDF-1.7\nreceipt-pdf-evidence"

_PENDING = "parsed_pending_confirmation"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _hash(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _insert_raw_intake(conn: sqlite3.Connection, suffix: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, source_channel, raw_input, received_at
        ) VALUES (?, 'telegram_text', 'telegram', 'receipt image', ?)
        """,
        (f"raw_ocr_{suffix}", "2026-07-19T15:00:00+00:00"),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _persist_attachment(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    suffix: str,
    content: bytes = JPEG,
    mime_type: str = "image/jpeg",
) -> int:
    path = (tmp_path / f"{suffix}.bin").resolve()
    path.write_bytes(content)
    path.chmod(0o400)
    raw_intake_id = _insert_raw_intake(conn, suffix)
    result = persist_attachment_evidence(
        conn,
        path,
        public_id=f"tgae_ocr_{suffix}",
        raw_intake_id=raw_intake_id,
        telegram_file_id=f"file_{suffix}",
        telegram_file_unique_id=f"unique_{suffix}",
        original_filename=f"{suffix}.jpg",
        declared_mime_type=mime_type,
        expected_file_size=len(content),
        expected_content_hash=_hash(content),
    )
    return int(result["attachment_id"])


def _ocr_block(
    sequence: int,
    text: str,
    *,
    line: int,
    left: int = 10,
    top: int = 20,
) -> ReceiptOcrBlock:
    return ReceiptOcrBlock(
        sequence_index=sequence,
        page_index=0,
        engine_block_index=0,
        engine_paragraph_index=0,
        engine_line_index=line,
        engine_word_index=sequence,
        text=text,
        left=left,
        top=top,
        width=30,
        height=10,
        page_width=800,
        page_height=1200,
        confidence_scaled=9750,
    )


def _sgd_blocks(
    *, total_marker: str = "S$", total_value: str = "12.34"
) -> tuple[ReceiptOcrBlock, ...]:
    return (
        _ocr_block(0, "COLD", line=0, left=10, top=20),
        _ocr_block(1, "STORAGE", line=0, left=60, top=20),
        _ocr_block(2, "2026-07-20", line=1, left=10, top=60),
        _ocr_block(3, "SUBTOTAL", line=2, left=10, top=100),
        _ocr_block(4, "10.00", line=2, left=120, top=100),
        _ocr_block(5, "TOTAL", line=3, left=10, top=140),
        _ocr_block(6, total_marker, line=3, left=80, top=140),
        _ocr_block(7, total_value, line=3, left=140, top=140),
    )


class FakeEngine:
    def __init__(self, *, result: ReceiptOcrEngineResult) -> None:
        self._identity = ReceiptOcrEngineIdentity(
            name="fake_ocr",
            version="1.0",
            binary_sha256=_hash("fake-binary-v1"),
            configuration_hash=_hash("fake-config-v1"),
        )
        self.result = result

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self._identity

    def extract(
        self,
        source: Any,
        *,
        limits: ReceiptOcrLimits,
        deadline: float,
    ) -> ReceiptOcrEngineResult:
        return self.result


def _prepare_extraction(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    suffix: str,
    blocks: tuple[ReceiptOcrBlock, ...] | None = None,
    status: ReceiptOcrExtractionStatus = ReceiptOcrExtractionStatus.SUCCEEDED,
    outcome_code: str = "ok",
    content: bytes = JPEG,
    mime_type: str = "image/jpeg",
    limits: ReceiptOcrLimits = ReceiptOcrLimits(),
) -> str:
    """Persist a real attachment and one canonical OCR extraction, return its public id."""
    attachment_id = _persist_attachment(
        conn, tmp_path, suffix=suffix, content=content, mime_type=mime_type
    )
    engine = FakeEngine(
        result=ReceiptOcrEngineResult(
            status=status,
            blocks=blocks or (),
            outcome_code=outcome_code,
        )
    )
    public_id = f"rocr_{suffix}"
    extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id=public_id,
        attachment_id=attachment_id,
        engine=engine,
        limits=limits,
    )
    return public_id


def _ingest(
    conn: sqlite3.Connection,
    extraction_public_id: str,
    *,
    proposal: str = "prop_1",
    link: str = "ropl_link_1",
    contract_version: str = PARSER_CONTRACT_VERSION_DEFAULT,
) -> ReceiptTotalProposalIngestionResult:
    return ingest_receipt_ocr_evidence_as_total_expense_proposal(
        conn,
        extraction_public_id=extraction_public_id,
        proposal_public_id=proposal,
        link_public_id=link,
        parser_contract_version=contract_version,
    )


def _payload(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT parsed_payload FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    ).fetchone()
    return json.loads(row["parsed_payload"])


# ---------------------------------------------------------------------------
# Migration 033 schema, constraints, and append-only enforcement
# ---------------------------------------------------------------------------


def test_links_table_and_indexes_exist(migrated_temp_db_connection: sqlite3.Connection) -> None:
    conn = migrated_temp_db_connection
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='receipt_ocr_proposal_links'"
    ).fetchone()
    assert table is not None
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='receipt_ocr_proposal_links'"
        ).fetchall()
    }
    assert "idx_receipt_ocr_proposal_links_initial_unique" in indexes
    assert "idx_receipt_ocr_proposal_links_extraction_id" in indexes
    assert "idx_receipt_ocr_proposal_links_parser_output_id" in indexes


def _seed_link_prereqs(conn: sqlite3.Connection, tmp_path: Path, suffix: str) -> tuple[int, int]:
    """Return (extraction_id, parser_output_id) for direct link-table tests."""
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix=suffix, blocks=_sgd_blocks())
    extraction_id = conn.execute(
        "SELECT id FROM receipt_ocr_extractions WHERE public_id = ?",
        (extraction_public_id,),
    ).fetchone()["id"]
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (public_id, source_type, parser_name, parser_version,
                                    parsed_payload, parse_status)
        VALUES (?, 'telegram_image', 'x', 'v1', '{}', 'parsed_pending_confirmation')
        """,
        (f"po_direct_{suffix}",),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(extraction_id), int(cursor.lastrowid)


def test_links_reject_bad_public_id_prefix(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_id, parser_output_id = _seed_link_prereqs(conn, tmp_path, "prefix")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id, proposal_input_hash,
                proposal_result_hash, parser_contract_version, link_role
            ) VALUES (?, ?, ?, ?, ?, ?, 'initial')
            """,
            ("bad_prefix_1", extraction_id, parser_output_id, "a" * 64, "b" * 64, "v1"),
        )


def test_links_reject_unknown_link_role(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_id, parser_output_id = _seed_link_prereqs(conn, tmp_path, "role")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id, proposal_input_hash,
                proposal_result_hash, parser_contract_version, link_role
            ) VALUES (?, ?, ?, ?, ?, ?, 'not_a_role')
            """,
            ("ropl_role_1", extraction_id, parser_output_id, "a" * 64, "b" * 64, "v1"),
        )


def test_links_reject_bad_foreign_key(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_id, _ = _seed_link_prereqs(conn, tmp_path, "fk")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id, proposal_input_hash,
                proposal_result_hash, parser_contract_version, link_role
            ) VALUES (?, ?, 999999, ?, ?, 'v1', 'initial')
            """,
            ("ropl_fk_1", extraction_id, "a" * 64, "b" * 64),
        )


def test_links_are_append_only(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    extraction_public_id = _prepare_extraction(
        migrated_temp_db_connection, tmp_path, suffix="append", blocks=_sgd_blocks()
    )
    result = _ingest(migrated_temp_db_connection, extraction_public_id)
    conn = migrated_temp_db_connection
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE receipt_ocr_proposal_links SET link_role = 'initial' WHERE public_id = ?",
            (result.link_public_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM receipt_ocr_proposal_links WHERE public_id = ?",
            (result.link_public_id,),
        )


# ---------------------------------------------------------------------------
# Successful ingestion, linkage, payload, and evidence
# ---------------------------------------------------------------------------


def test_successful_sgd_ingestion(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="sgd", blocks=_sgd_blocks())
    result = _ingest(conn, extraction_public_id)

    assert result.idempotent is False
    assert result.parse_status == _PENDING
    assert result.parser_contract_version == PARSER_CONTRACT_VERSION_DEFAULT
    assert len(result.proposal_input_hash) == 64
    assert len(result.proposal_result_hash) == 64

    payload = _payload(conn, result.parser_output_id)
    assert payload["amount"] == "12.34"
    assert payload["currency"] == "SGD"
    assert payload["transaction_date"] == "2026-07-20"
    assert payload["merchant"] == "COLD STORAGE"
    assert payload["is_final"] is False
    assert payload["confirmation_required"] is True
    assert payload["status"] == _PENDING
    assert payload["ocr_evidence"]["extraction_public_id"] == extraction_public_id
    assert payload["ocr_evidence"]["extraction_status"] == "succeeded"


def test_source_linkage_and_raw_intake_pointer(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="link", blocks=_sgd_blocks())
    result = _ingest(conn, extraction_public_id)

    proposal = conn.execute(
        "SELECT source_type, source_public_id, attachment_id FROM parser_outputs WHERE id = ?",
        (result.parser_output_id,),
    ).fetchone()
    assert proposal["source_type"] == "telegram_image"
    assert proposal["source_public_id"] == "raw_ocr_link"

    pointer = conn.execute(
        "SELECT parser_output_id, status FROM raw_intake_records WHERE public_id = 'raw_ocr_link'"
    ).fetchone()
    assert pointer["parser_output_id"] == result.parser_output_id
    assert pointer["status"] == _PENDING


def test_field_evidence_rows_are_ocr_sourced(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="evid", blocks=_sgd_blocks())
    result = _ingest(conn, extraction_public_id)

    rows = conn.execute(
        """
        SELECT field_name, evidence_source_type
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ?
        ORDER BY field_name
        """,
        (result.parser_output_id,),
    ).fetchall()
    fields = {row["field_name"] for row in rows}
    assert fields == {"merchant", "amount", "currency", "transaction_date"}
    assert all(row["evidence_source_type"] == "ocr" for row in rows)


# ---------------------------------------------------------------------------
# Content-hash contract
# ---------------------------------------------------------------------------


def _reference_pre_ocr_hash(conn: sqlite3.Connection, parser_output: dict[str, Any]) -> str:
    """Reproduce the pre-OCR content-hash material exactly (no ocr_evidence)."""
    payload = json.loads(parser_output["parsed_payload"])
    material = {
        "proposal_public_id": parser_output["public_id"],
        "source": {
            "source_type": parser_output["source_type"],
            "source_public_id": parser_output["source_public_id"],
            "statement_batch_id": parser_output["statement_batch_id"],
            "raw_text": parser_output["raw_text"],
        },
        "parser": {
            "name": parser_output["parser_name"],
            "version": parser_output["parser_version"],
        },
        "transaction": {
            "intent": payload.get("intent"),
            "transaction_type": payload.get("transaction_type"),
            "amount": _canonical_amount(payload),
            "currency": _canonical_currency(payload),
            "transaction_date": payload.get("transaction_date", payload.get("date")),
            "merchant": payload.get("merchant"),
            "description": payload.get("description"),
            "payer": payload.get("payer", payload.get("paid_by")),
            "account": payload.get("account", payload.get("account_id")),
            "category": payload.get("category"),
        },
        "attachments": _attachment_evidence(conn, parser_output),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _full_parser_output(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (parser_output_id,)).fetchone()
    return dict(row)


def test_content_hash_includes_ocr_evidence(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="hash", blocks=_sgd_blocks())
    result = _ingest(conn, extraction_public_id)

    parser_output = _full_parser_output(conn, result.parser_output_id)
    hash_with_ocr = compute_proposal_content_hash(conn, parser_output)
    hash_without_ocr = _reference_pre_ocr_hash(conn, parser_output)
    # The bound OCR link must materially change the authoritative hash.
    assert hash_with_ocr != hash_without_ocr


def test_non_ocr_proposal_hash_is_byte_for_byte_unchanged(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    payload = {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": "5.00",
        "currency": "SGD",
        "transaction_date": "2026-07-20",
        "merchant": "Plain Merchant",
    }
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (public_id, source_type, source_public_id, parser_name,
                                    parser_version, raw_text, parsed_payload, parse_status)
        VALUES (?, 'telegram_text', 'raw_plain', 'manual', 'v1', 'total 5', ?, 'parsed')
        """,
        ("po_plain_1", json.dumps(payload)),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    parser_output = _full_parser_output(conn, int(cursor.lastrowid))

    assert compute_proposal_content_hash(conn, parser_output) == _reference_pre_ocr_hash(
        conn, parser_output
    )


# ---------------------------------------------------------------------------
# Deterministic parse behavior through the ingestion boundary
# ---------------------------------------------------------------------------


def test_myr_is_conservatively_unresolved(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = _sgd_blocks(total_marker="RM")
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="myr", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    assert payload["currency"] == "MYR"
    assert payload["amount"] is None
    assert FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT in result.ambiguity_flags


def test_bare_dollar_symbol_is_ambiguous(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = _sgd_blocks(total_marker="$")
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="dollar", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    assert payload["amount"] is None
    assert payload["currency"] is None
    assert FLAG_AMBIGUOUS_CURRENCY_SYMBOL in result.ambiguity_flags


def test_subtotal_and_tax_are_excluded(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="sub", blocks=_sgd_blocks())
    result = _ingest(conn, extraction_public_id)
    payload = _payload(conn, result.parser_output_id)
    # The 10.00 subtotal must never be chosen as the total.
    assert payload["amount"] == "12.34"


def test_total_not_found(migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path) -> None:
    conn = migrated_temp_db_connection
    blocks = (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "2026-07-20", line=1, left=10, top=60),
        _ocr_block(3, "THANK", line=2, left=10, top=100),
        _ocr_block(4, "YOU", line=2, left=80, top=100),
    )
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="nototal", blocks=blocks)
    result = _ingest(conn, extraction_public_id)
    payload = _payload(conn, result.parser_output_id)
    assert payload["amount"] is None
    assert FLAG_TOTAL_NOT_FOUND in result.ambiguity_flags


def test_conflicting_totals(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "TOTAL", line=1, left=10, top=60),
        _ocr_block(3, "S$", line=1, left=80, top=60),
        _ocr_block(4, "12.34", line=1, left=140, top=60),
        _ocr_block(5, "TOTAL", line=2, left=10, top=100),
        _ocr_block(6, "S$", line=2, left=80, top=100),
        _ocr_block(7, "99.99", line=2, left=140, top=100),
    )
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="conflict", blocks=blocks)
    result = _ingest(conn, extraction_public_id)
    payload = _payload(conn, result.parser_output_id)
    assert payload["amount"] is None
    assert FLAG_CONFLICTING_TOTALS in result.ambiguity_flags


def test_ambiguous_numeric_date(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "05/06/2026", line=1, left=10, top=60),
        _ocr_block(3, "TOTAL", line=2, left=10, top=100),
        _ocr_block(4, "S$", line=2, left=80, top=100),
        _ocr_block(5, "12.34", line=2, left=140, top=100),
    )
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="ambdate", blocks=blocks)
    result = _ingest(conn, extraction_public_id)
    payload = _payload(conn, result.parser_output_id)
    assert payload["transaction_date"] is None
    assert payload["amount"] == "12.34"
    assert FLAG_AMBIGUOUS_DATE in result.ambiguity_flags


def test_us_dollar_prefix_resolves_usd(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    # "US$" must resolve to USD and never be misread as the "S$"/SGD alias.
    blocks = _sgd_blocks(total_marker="US$")
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="usd", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    assert payload["currency"] == "USD"
    assert payload["amount"] == "12.34"


def test_bare_iso_currency_code_resolves(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = _sgd_blocks(total_marker="USD")
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="isocur", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    assert payload["currency"] == "USD"
    assert payload["amount"] == "12.34"


def test_negative_total_is_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    # A negative (e.g. refund) total must fail the Money Contract sign policy
    # rather than silently emitting a positive expense amount.
    blocks = (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "TOTAL", line=1, left=10, top=60),
        _ocr_block(3, "-5.00", line=1, left=80, top=60),
        _ocr_block(4, "SGD", line=1, left=140, top=60),
    )
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="neg", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    assert payload["amount"] is None
    assert payload["currency"] == "SGD"
    assert FLAG_TOTAL_AMOUNT_INVALID in result.ambiguity_flags


def test_tax_and_change_lines_are_excluded(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "GST", line=1, left=10, top=60),
        _ocr_block(3, "S$", line=1, left=80, top=60),
        _ocr_block(4, "0.70", line=1, left=140, top=60),
        _ocr_block(5, "TOTAL", line=2, left=10, top=100),
        _ocr_block(6, "S$", line=2, left=80, top=100),
        _ocr_block(7, "12.34", line=2, left=140, top=100),
        _ocr_block(8, "CHANGE", line=3, left=10, top=140),
        _ocr_block(9, "S$", line=3, left=80, top=140),
        _ocr_block(10, "7.66", line=3, left=140, top=140),
    )
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="taxchg", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    # Neither the 0.70 tax nor the 7.66 change may be chosen as the total.
    assert payload["amount"] == "12.34"


def test_invalid_calendar_date_unresolved(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    blocks = (
        _ocr_block(0, "COLD", line=0, left=10),
        _ocr_block(1, "STORAGE", line=0, left=60),
        _ocr_block(2, "2026-13-45", line=1, left=10, top=60),
        _ocr_block(3, "TOTAL", line=2, left=10, top=100),
        _ocr_block(4, "S$", line=2, left=80, top=100),
        _ocr_block(5, "12.34", line=2, left=140, top=100),
    )
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="baddate", blocks=blocks)
    result = _ingest(conn, extraction_public_id)

    payload = _payload(conn, result.parser_output_id)
    assert payload["transaction_date"] is None
    assert payload["amount"] == "12.34"


def test_non_staging_database_is_rejected(tmp_path: Path) -> None:
    # A plain on-disk database is not an authorised staging database.
    conn = sqlite3.connect(tmp_path / "plain.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ProposalStagingDatabaseRejectedError):
            _ingest(conn, "rocr_absent")
    finally:
        conn.close()


def _ocr_evidence_snapshot(conn: sqlite3.Connection) -> tuple[Any, Any]:
    extractions = conn.execute("SELECT * FROM receipt_ocr_extractions ORDER BY id").fetchall()
    blocks = conn.execute("SELECT * FROM receipt_ocr_blocks ORDER BY id").fetchall()
    return (
        tuple(tuple(row) for row in extractions),
        tuple(tuple(row) for row in blocks),
    )


def test_ingestion_does_not_mutate_ocr_evidence_or_attachment(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="nomut", blocks=_sgd_blocks())
    attachment_path = (tmp_path / "nomut.bin").resolve()
    file_hash_before = _hash(attachment_path.read_bytes())
    evidence_before = _ocr_evidence_snapshot(conn)

    _ingest(conn, extraction_public_id)

    assert _hash(attachment_path.read_bytes()) == file_hash_before
    assert _ocr_evidence_snapshot(conn) == evidence_before


# ---------------------------------------------------------------------------
# OCR failure statuses produce incomplete pending proposals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "status", "outcome_code", "content", "mime_type", "limits", "flag"),
    [
        (
            "notext",
            ReceiptOcrExtractionStatus.NO_TEXT,
            "no_text",
            JPEG,
            "image/jpeg",
            ReceiptOcrLimits(),
            FLAG_OCR_NO_TEXT,
        ),
        (
            "enginefail",
            ReceiptOcrExtractionStatus.ENGINE_FAILED,
            "engine_failed",
            JPEG,
            "image/jpeg",
            ReceiptOcrLimits(),
            FLAG_OCR_ENGINE_FAILED,
        ),
        (
            "unsupported",
            ReceiptOcrExtractionStatus.SUCCEEDED,  # ignored: PDF forces deterministic outcome
            "ok",
            PDF,
            "application/pdf",
            ReceiptOcrLimits(),
            FLAG_OCR_UNSUPPORTED_INPUT,
        ),
        (
            "resource",
            ReceiptOcrExtractionStatus.SUCCEEDED,  # ignored: tiny limit forces rejection
            "ok",
            JPEG,
            "image/jpeg",
            ReceiptOcrLimits(max_attachment_bytes=1),
            FLAG_OCR_RESOURCE_REJECTED,
        ),
    ],
)
def test_ocr_failure_statuses_yield_incomplete_pending(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    suffix: str,
    status: ReceiptOcrExtractionStatus,
    outcome_code: str,
    content: bytes,
    mime_type: str,
    limits: ReceiptOcrLimits,
    flag: str,
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn,
        tmp_path,
        suffix=suffix,
        blocks=(),
        status=status,
        outcome_code=outcome_code,
        content=content,
        mime_type=mime_type,
        limits=limits,
    )
    result = _ingest(conn, extraction_public_id)
    assert result.parse_status == _PENDING
    assert flag in result.ambiguity_flags
    payload = _payload(conn, result.parser_output_id)
    assert payload["amount"] is None
    assert payload["currency"] is None
    assert payload["is_final"] is False


# ---------------------------------------------------------------------------
# Fail-closed error and binding paths
# ---------------------------------------------------------------------------


def test_missing_extraction_raises(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(OcrExtractionNotFoundError):
        _ingest(migrated_temp_db_connection, "rocr_does_not_exist")


def test_block_count_mismatch_is_unusable(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn, tmp_path, suffix="mismatch", blocks=_sgd_blocks()
    )
    extraction_id = conn.execute(
        "SELECT id FROM receipt_ocr_extractions WHERE public_id = ?",
        (extraction_public_id,),
    ).fetchone()["id"]
    # Append an extra block (allowed: block rows are append-only, not insert-only)
    # so the persisted block count no longer matches the recorded extraction.
    conn.execute(
        """
        INSERT INTO receipt_ocr_blocks (
            extraction_id, sequence_index, page_index, normalized_text,
            coordinate_left, coordinate_top, coordinate_width, coordinate_height,
            page_width, page_height, confidence_scaled
        ) VALUES (?, 99, 0, 'EXTRA', 10, 200, 30, 10, 800, 1200, 9000)
        """,
        (extraction_id,),
    )
    conn.commit()
    with pytest.raises(UnusableOcrEvidenceError):
        _ingest(conn, extraction_public_id)


def test_ambiguous_source_binding_raises(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn, tmp_path, suffix="ambsrc", blocks=_sgd_blocks()
    )
    attachment_id = conn.execute(
        """
        SELECT attachment_id FROM receipt_ocr_extractions WHERE public_id = ?
        """,
        (extraction_public_id,),
    ).fetchone()["attachment_id"]
    # Bind the same attachment to a second raw-intake record -> ambiguous source.
    second_raw = _insert_raw_intake(conn, "ambsrc_second")
    conn.execute(
        """
        INSERT INTO telegram_attachment_source (
            public_id, attachment_id, raw_intake_record_id,
            original_attachment_path, observed_file_size, content_hash,
            source_evidence_payload
        ) VALUES (?, ?, ?, '/tmp/x', ?, ?, '{}')
        """,
        (
            "tgae_ambsrc_second",
            attachment_id,
            second_raw,
            len(JPEG),
            _hash(JPEG),
        ),
    )
    conn.commit()
    with pytest.raises(ProposalSourceBindingConflictError):
        _ingest(conn, extraction_public_id)


@pytest.mark.parametrize(
    ("proposal", "link", "contract_version"),
    [
        ("bad id with spaces", "ropl_ok", PARSER_CONTRACT_VERSION_DEFAULT),
        ("prop_ok", "no_prefix_link", PARSER_CONTRACT_VERSION_DEFAULT),
        ("prop_ok", "ropl_ok", "bad version!"),
    ],
)
def test_invalid_command_arguments_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    proposal: str,
    link: str,
    contract_version: str,
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn, tmp_path, suffix="badargs", blocks=_sgd_blocks()
    )
    with pytest.raises(InvalidProposalCommandError):
        _ingest(
            conn,
            extraction_public_id,
            proposal=proposal,
            link=link,
            contract_version=contract_version,
        )


def test_caller_owned_transaction_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="txn", blocks=_sgd_blocks())
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO raw_intake_records (public_id, source_type, source_channel, raw_input,"
        " received_at) VALUES ('raw_pending', 'telegram_text', 'telegram', 'x',"
        " '2026-07-19T15:00:00+00:00')"
    )
    try:
        with pytest.raises(ProposalCallerOwnedTransactionError):
            _ingest(conn, extraction_public_id)
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# Idempotency and conflict detection
# ---------------------------------------------------------------------------


def test_identical_command_is_idempotent_replay(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="idem", blocks=_sgd_blocks())
    first = _ingest(conn, extraction_public_id)
    second = _ingest(conn, extraction_public_id)

    assert first.idempotent is False
    assert second.idempotent is True
    assert second.parser_output_id == first.parser_output_id
    assert second.proposal_result_hash == first.proposal_result_hash
    # Exactly one proposal and one link were created.
    assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 1


def test_same_link_id_different_material_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn, tmp_path, suffix="material", blocks=_sgd_blocks()
    )
    _ingest(conn, extraction_public_id, proposal="prop_a", link="ropl_shared")
    with pytest.raises(ProposalIdempotencyConflictError):
        _ingest(conn, extraction_public_id, proposal="prop_b", link="ropl_shared")


def test_duplicate_initial_for_extraction_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn, tmp_path, suffix="dupinit", blocks=_sgd_blocks()
    )
    _ingest(conn, extraction_public_id, proposal="prop_x", link="ropl_x")
    with pytest.raises(ProposalDuplicateInitialError):
        _ingest(conn, extraction_public_id, proposal="prop_y", link="ropl_y")


def test_duplicate_proposal_public_id_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    first_extraction = _prepare_extraction(conn, tmp_path, suffix="dupprop1", blocks=_sgd_blocks())
    # Distinct attachment bytes -> distinct extraction fingerprint (which is UNIQUE).
    second_extraction = _prepare_extraction(
        conn, tmp_path, suffix="dupprop2", blocks=_sgd_blocks(), content=JPEG + b"-second"
    )
    _ingest(conn, first_extraction, proposal="prop_shared", link="ropl_first")
    with pytest.raises(ProposalIdempotencyConflictError):
        _ingest(conn, second_extraction, proposal="prop_shared", link="ropl_second")


# ---------------------------------------------------------------------------
# Concurrency: one success, one idempotent replay, no leaked transactions
# ---------------------------------------------------------------------------


def test_concurrent_identical_ingestion(
    migrated_temp_db_path: Path,
) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as work_dir:
        setup = connect_temp_db(migrated_temp_db_path)
        try:
            extraction_public_id = _prepare_extraction(
                setup, Path(work_dir), suffix="concurrent", blocks=_sgd_blocks()
            )
        finally:
            setup.close()

        results: list[ReceiptTotalProposalIngestionResult] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            conn = connect_temp_db(migrated_temp_db_path)
            conn.execute("PRAGMA busy_timeout = 5000")
            try:
                barrier.wait(timeout=5)
                results.append(_ingest(conn, extraction_public_id))
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)
            finally:
                assert conn.in_transaction is False
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert errors == []
        assert sorted(result.idempotent for result in results) == [False, True]

        check = connect_temp_db(migrated_temp_db_path)
        try:
            link_count = check.execute(
                "SELECT COUNT(*) FROM receipt_ocr_proposal_links"
            ).fetchone()[0]
        finally:
            check.close()
        assert link_count == 1


# ---------------------------------------------------------------------------
# Crash replay: failure at each persistence boundary fully rolls back
# ---------------------------------------------------------------------------


@pytest.fixture()
def _reset_failure_hook():
    yield
    proposal_module._failure_injection_hook = None


@pytest.mark.parametrize(
    "stage",
    [
        "before_extraction_revalidation",
        "before_proposal_insert",
        "before_field_evidence_insert",
        "before_link_insert",
        "before_raw_intake_update",
        "before_persisted_verification",
        "before_commit",
    ],
)
def test_failure_at_each_boundary_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    _reset_failure_hook: None,
    stage: str,
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(
        conn, tmp_path, suffix=f"crash_{stage}", blocks=_sgd_blocks()
    )

    def hook(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"injected failure at {stage}")

    proposal_module._failure_injection_hook = hook
    with pytest.raises(ProposalUnexpectedPersistenceError):
        _ingest(conn, extraction_public_id)

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_outputs WHERE public_id = 'prop_1'").fetchone()[0]
        == 0
    )
    pointer = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
        (f"raw_ocr_crash_{stage}",),
    ).fetchone()
    assert pointer["parser_output_id"] is None

    # After clearing the fault the same command succeeds cleanly.
    proposal_module._failure_injection_hook = None
    result = _ingest(conn, extraction_public_id)
    assert result.idempotent is False


# ---------------------------------------------------------------------------
# Final-state safety: proposals stay pending, no downstream rows created
# ---------------------------------------------------------------------------


def test_ingestion_creates_no_final_financial_state(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix="final", blocks=_sgd_blocks())
    result = _ingest(conn, extraction_public_id)

    assert result.parse_status == _PENDING
    for table in (
        "transactions",
        "parser_proposal_confirmations",
        "parser_proposal_conversion_audit",
    ):
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
