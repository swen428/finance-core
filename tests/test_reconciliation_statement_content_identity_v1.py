"""Statement source-content identity and canonical row fingerprint contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Callable

import pytest

from finance_core.financial_audit import FinancialAuditRepository
from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    normalize_pdf_statement_row_checked,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.repository import DuplicatePublicIdError
from finance_core.reconciliation.statement_csv import StatementCsvAdapter
from finance_core.reconciliation.statement_identity import (
    CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION,
    ROW_FINGERPRINT_VERSION,
    STATEMENT_IMPORT_CONTRACT_VERSION,
    SourceFileIdentityError,
)
from finance_core.reconciliation.statement_import import StatementImporter, StatementImportError
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow


def _sha(content: bytes | str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _row(
    merchant: str = "Content Merchant",
    *,
    reference: str | None = "line-1",
    payload: dict[str, object] | None = None,
    fingerprint: str | None = None,
    fingerprint_version: str | None = None,
    external_fingerprint: str | None = None,
    external_fingerprint_version: str | None = None,
    fingerprint_source_content_hash: str | None = None,
    direction: StatementAmountDirection | None = StatementAmountDirection.DEBIT,
) -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw=merchant,
        amount=Decimal("12.34"),
        currency="SGD",
        transaction_date=date(2026, 7, 13),
        posted_date=date(2026, 7, 14),
        statement_row_reference=reference,
        raw_row_payload=payload,
        row_fingerprint=fingerprint,
        row_fingerprint_version=fingerprint_version,
        external_row_fingerprint=external_fingerprint,
        external_row_fingerprint_version=external_fingerprint_version,
        fingerprint_source_content_hash=fingerprint_source_content_hash,
        amount_direction=direction,
        raw_amount="12.34",
        raw_amount_type=direction.value if direction is not None else None,
    )


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_same_bytes_at_different_paths_replay_one_canonical_batch(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source_bytes = b"date,merchant,amount\n2026-07-13,Content Merchant,12.34\n"
    first_path = tmp_path / "original.csv"
    moved_path = tmp_path / "renamed.csv"
    first_path.write_bytes(source_bytes)
    moved_path.write_bytes(source_bytes)
    importer = StatementImporter(migrated_temp_db_connection)

    first = importer.import_rows(
        [_row(payload={"attachment_path": str(first_path), "source_page_number": 1})],
        source_type="bank_statement",
        public_id="content-moved",
        source_file_path=str(first_path),
    )
    replay = importer.import_rows(
        [_row(payload={"attachment_path": str(moved_path), "source_page_number": 1})],
        source_type="bank_statement",
        public_id="content-moved",
        source_file_path=str(moved_path),
    )

    assert first.source_content_hash == _sha(source_bytes)
    assert replay.batch_id == first.batch_id
    assert replay.public_id == first.public_id
    assert replay.idempotent_count == 1
    batch = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE id = ?", (first.batch_id,)
    ).fetchone()
    assert batch["source_file_path"] == str(first_path)
    assert batch["source_filename"] == "original.csv"
    assert batch["source_file_hash"] == _sha(source_bytes)
    assert batch["source_hash_verification_status"] == "verified_from_bytes"
    evidence_rows = migrated_temp_db_connection.execute(
        "SELECT evidence_path, original_filename, source_content_hash "
        "FROM statement_import_source_evidence WHERE batch_id = ? ORDER BY evidence_path",
        (first.batch_id,),
    ).fetchall()
    assert [(row["evidence_path"], row["original_filename"]) for row in evidence_rows] == [
        (str(first_path), "original.csv"),
        (str(moved_path), "renamed.csv"),
    ]
    assert {row["source_content_hash"] for row in evidence_rows} == {_sha(source_bytes)}
    audit_events = FinancialAuditRepository(migrated_temp_db_connection).list_chain(
        "statement_import_batch", "content-moved"
    )
    observed_paths = {
        reference
        for event in audit_events
        if event.event_type == "statement_import_source_evidence_observed"
        for reference in event.source_evidence_references
        if reference.startswith("source-file-path:")
    }
    assert observed_paths == {
        f"source-file-path:{first_path}",
        f"source-file-path:{moved_path}",
    }
    persisted = migrated_temp_db_connection.execute(
        "SELECT raw_row_payload_json FROM statement_transactions WHERE batch_id = ?",
        (first.batch_id,),
    ).fetchone()
    assert json.loads(persisted["raw_row_payload_json"])["attachment_path"] == str(first_path)


def test_replaced_file_at_same_path_changes_identity_and_conflicts(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "statement.csv"
    source_path.write_bytes(b"version-one")
    importer = StatementImporter(migrated_temp_db_connection)
    first = importer.import_rows(
        [_row()],
        source_type="bank_statement",
        public_id="content-replaced",
        source_file_path=str(source_path),
    )
    source_path.write_bytes(b"version-two")

    with pytest.raises(DuplicatePublicIdError, match="statement import batch"):
        importer.import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="content-replaced",
            source_file_path=str(source_path),
        )

    assert first.source_content_hash == _sha(b"version-one")
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_import_batches"
        ).fetchone()[0]
        == 1
    )


def test_different_bytes_at_same_path_create_different_auto_identities(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "statement.csv"
    source_path.write_bytes(b"version-one")
    importer = StatementImporter(migrated_temp_db_connection)
    first = importer.import_rows(
        [_row()], source_type="bank_statement", source_file_path=str(source_path)
    )
    source_path.write_bytes(b"version-two")
    second = importer.import_rows(
        [_row()], source_type="bank_statement", source_file_path=str(source_path)
    )

    assert first.source_content_hash != second.source_content_hash
    assert first.public_id != second.public_id
    assert first.batch_id != second.batch_id
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_import_source_evidence"
        ).fetchone()[0]
        == 2
    )


def test_expected_content_hash_must_match_exact_bytes(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "statement.csv"
    source_path.write_bytes(b"authoritative bytes")
    with pytest.raises(SourceFileIdentityError, match="does not match"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [_row()],
            source_type="bank_statement",
            source_file_path=str(source_path),
            source_file_hash=_sha("different bytes"),
        )
    assert not migrated_temp_db_connection.in_transaction


def test_unreadable_and_symlink_sources_fail_before_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    with pytest.raises(SourceFileIdentityError, match="Unable to read"):
        importer.import_rows(
            [_row()],
            source_type="bank_statement",
            source_file_path=str(tmp_path / "missing.csv"),
        )
    target = tmp_path / "target.csv"
    link = tmp_path / "link.csv"
    target.write_bytes(b"not followed")
    link.symlink_to(target)
    with pytest.raises(SourceFileIdentityError, match="symlink"):
        importer.import_rows([_row()], source_type="bank_statement", source_file_path=str(link))
    assert not migrated_temp_db_connection.in_transaction


def test_import_contract_version_is_bound_to_explicit_batch_identity(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    importer.import_rows(
        [_row()],
        source_type="bank_statement",
        public_id="version-bound",
        source_file_hash=_sha("source"),
    )
    with pytest.raises(DuplicatePublicIdError, match="source content already belongs"):
        importer.import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="version-bound",
            source_file_hash=_sha("source"),
            import_contract_version="statement-import-v4-test",
        )


def test_valid_supplied_fingerprint_is_preserved_as_external_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    supplied = _sha("caller fingerprint")
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row(fingerprint=supplied)],
        source_type="bank_statement",
        public_id="supplied-fingerprint",
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT row_fingerprint, row_fingerprint_version, "
        "external_row_fingerprint, external_row_fingerprint_version "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["row_fingerprint"] != supplied
    assert stored["row_fingerprint_version"] == ROW_FINGERPRINT_VERSION
    assert stored["external_row_fingerprint"] == supplied
    assert stored["external_row_fingerprint_version"] == CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION
    event = next(
        event
        for event in FinancialAuditRepository(migrated_temp_db_connection).list_chain(
            "statement_import_batch", "supplied-fingerprint"
        )
        if event.event_type == "statement_import_accepted"
    )
    payload = json.loads(event.event_payload_json)["value"]
    assert payload["external_row_evidence"] == [
        {
            "row_public_id": migrated_temp_db_connection.execute(
                "SELECT public_id FROM statement_transactions WHERE batch_id = ?",
                (batch.batch_id,),
            ).fetchone()[0],
            "external_row_fingerprint": supplied,
            "external_row_fingerprint_version": CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION,
        }
    ]


def test_explicit_external_fingerprint_is_evidence_not_authority(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    supplied = _sha("external adapter fingerprint")
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [
            _row(
                external_fingerprint=supplied,
                external_fingerprint_version="dictionary-parser-v2",
            )
        ],
        source_type="structured_csv",
        public_id="external-adapter-fingerprint",
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT row_fingerprint, row_fingerprint_version, "
        "external_row_fingerprint, external_row_fingerprint_version "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["row_fingerprint_version"] == ROW_FINGERPRINT_VERSION
    assert stored["row_fingerprint"] != supplied
    assert stored["external_row_fingerprint"] == supplied
    assert stored["external_row_fingerprint_version"] == "dictionary-parser-v2"


def test_missing_fingerprint_is_calculated_and_versioned(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row(fingerprint=None)],
        source_type="bank_statement",
        public_id="calculated-fingerprint",
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT row_fingerprint, row_fingerprint_version "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert len(stored["row_fingerprint"]) == 64
    int(stored["row_fingerprint"], 16)
    assert stored["row_fingerprint_version"] == ROW_FINGERPRINT_VERSION


@pytest.mark.parametrize("fingerprint", ["short", "A" * 64, "g" * 64])
def test_invalid_supplied_fingerprint_is_rejected(fingerprint: str) -> None:
    with pytest.raises(ValueError, match="full lowercase SHA-256"):
        _row(fingerprint=fingerprint)


def test_dictionary_input_cannot_claim_arbitrary_authoritative_fingerprint(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    supplied = _sha("dict fingerprint")
    with pytest.raises(StatementImportError, match="Unsupported authoritative"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [
                {
                    "merchant_raw": "Dictionary Merchant",
                    "amount": "9.90",
                    "currency": "SGD",
                    "statement_row_reference": "dict-1",
                    "raw_row_payload": {"raw": "evidence"},
                    "row_fingerprint": supplied,
                    "row_fingerprint_version": "dictionary-parser-v2",
                    "amount_direction": "refund",
                    "raw_amount": "-9.90",
                }
            ],
            source_type="structured_csv",
            public_id="dictionary-fingerprint",
        )
    assert not migrated_temp_db_connection.in_transaction


@pytest.mark.parametrize(
    "version",
    ["statement-row-fingerprint-vl", "pdf-row-fingerprint-v3", "arbitrary-name"],
)
def test_unknown_and_typo_fingerprint_versions_are_rejected_as_authority(
    migrated_temp_db_connection: sqlite3.Connection,
    version: str,
) -> None:
    with pytest.raises(StatementImportError, match="Unsupported authoritative"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [_row(fingerprint=_sha(version), fingerprint_version=version)],
            source_type="bank_statement",
        )


def test_sqlite_rejects_unregistered_authoritative_fingerprint_version(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    migrated_temp_db_connection.execute(
        "INSERT INTO statement_import_batches (public_id, source_type) VALUES (?, ?)",
        ("unregistered-version-batch", "bank_statement"),
    )
    batch_id = migrated_temp_db_connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="unsupported authoritative"):
        migrated_temp_db_connection.execute(
            """INSERT INTO statement_transactions (
            public_id, batch_id, merchant_raw, amount, currency,
            row_fingerprint, row_fingerprint_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "unregistered-version-row",
                batch_id,
                "Unregistered",
                "1.00",
                "SGD",
                _sha("unregistered-version-row"),
                "dictionary-parser-v2",
            ),
        )
    migrated_temp_db_connection.rollback()


def test_registered_generic_fingerprint_must_match_canonical_material(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(StatementImportError, match="does not match row material"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [
                _row(
                    fingerprint=_sha("wrong generic material"),
                    fingerprint_version=ROW_FINGERPRINT_VERSION,
                )
            ],
            source_type="bank_statement",
        )


def test_invalid_direction_rejected_and_missing_direction_remains_null(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    with pytest.raises(StatementImportError, match="Invalid statement amount direction"):
        importer.import_rows(
            [
                {
                    "merchant_raw": "Invalid Direction",
                    "amount": "1.00",
                    "currency": "SGD",
                    "amount_direction": "not-a-direction",
                }
            ],
            source_type="structured_csv",
        )
    batch = importer.import_rows(
        [
            {
                "merchant_raw": "Missing Direction",
                "amount": "1.00",
                "currency": "SGD",
            }
        ],
        source_type="structured_csv",
        public_id="missing-direction",
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT amount_direction FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["amount_direction"] is None


def test_csv_file_hash_and_fingerprint_survive_import(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "statement.csv"
    csv_bytes = (
        b"transaction_date,posted_date,merchant_raw,amount,currency,reference\n"
        b"2026-07-13,2026-07-14,CSV Merchant,12.34,SGD,csv-1\n"
    )
    csv_path.write_bytes(csv_bytes)
    parsed = StatementCsvAdapter().parse_file_hardened(csv_path)
    assert parsed.success
    assert parsed.source_content_hash == _sha(csv_bytes)
    assert parsed.rows[0].fingerprint_source_content_hash == _sha(csv_bytes)
    assert parsed.rows[0].row_fingerprint is None

    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        parsed.rows,
        source_type="structured_csv",
        public_id="csv-content",
        source_file_path=str(csv_path),
        source_file_hash=parsed.source_content_hash,
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT row_fingerprint, row_fingerprint_version "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert len(stored["row_fingerprint"]) == 64
    assert stored["row_fingerprint_version"] == ROW_FINGERPRINT_VERSION
    assert batch.source_content_hash == _sha(csv_bytes)


def test_csv_changed_after_parse_is_rejected_before_import(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "statement.csv"
    csv_path.write_text(
        "transaction_date,merchant_raw,amount,currency\n2026-07-13,CSV Merchant,12.34,SGD\n",
        encoding="utf-8",
    )
    parsed = StatementCsvAdapter().parse_file_hardened(csv_path)
    csv_path.write_text(
        "transaction_date,merchant_raw,amount,currency\n2026-07-13,Changed Merchant,99.99,SGD\n",
        encoding="utf-8",
    )
    with pytest.raises(StatementImportError, match="does not match statement source bytes"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            parsed.rows,
            source_type="structured_csv",
            source_file_path=str(csv_path),
        )
    assert not migrated_temp_db_connection.in_transaction


def test_pdf_bridge_preserves_attachment_hash_fingerprint_and_page_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_bytes = b"%PDF-1.7\nsynthetic content\n%%EOF\n"
    pdf_path.write_bytes(pdf_bytes)
    source_hash = _sha(pdf_bytes)
    normalized = normalize_pdf_statement_row_checked(
        ParsedPdfStatementRow(
            source_statement_id=source_hash,
            attachment_path=str(pdf_path),
            description="PDF Merchant",
            amount=Decimal("12.34"),
            currency="SGD",
            source_page_number=3,
            source_row_ref="pdf-3-2",
            transaction_date=date(2026, 7, 13),
            posted_date=date(2026, 7, 14),
            raw_row_text="13 JUL PDF Merchant 12.34",
            amount_direction=StatementAmountDirection.DEBIT,
            source_content_hash=source_hash,
            source_filename="statement.pdf",
            source_row_number=2,
            source_text_excerpt="13 JUL PDF Merchant 12.34",
            direction_source=PdfDirectionSource.EXPLICIT_TOKEN,
            direction_confidence=PdfDirectionConfidence.HIGH,
            review_status=PdfRowReviewStatus.AUTHORITATIVE,
            original_amount_token="12.34",
            original_amount_sign=PdfOriginalAmountSign.POSITIVE,
            currency_token="SGD",
            transaction_date_token="2026-07-13",
            posted_date_token="2026-07-14",
        )
    )
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalized],
        source_type="credit_card_statement",
        public_id="pdf-content",
        source_file_path=str(pdf_path),
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_transactions WHERE batch_id = ?", (batch.batch_id,)
    ).fetchone()
    payload = json.loads(stored["raw_row_payload_json"])
    assert payload["attachment_path"] == str(pdf_path)
    assert payload["source_page_number"] == 3
    assert stored["row_fingerprint"] == normalized.row_fingerprint
    assert batch.source_content_hash == source_hash


def test_duplicate_rows_and_location_materiality(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    distinct = importer.import_rows(
        [_row(reference="line-1"), _row(reference="line-2")],
        source_type="bank_statement",
        public_id="distinct-locations",
    )
    assert len(distinct.inserted_ids) == 2
    with pytest.raises(StatementImportError, match="duplicate statement row identity"):
        importer.import_rows(
            [_row(reference="same-line"), _row(reference="same-line")],
            source_type="bank_statement",
            public_id="duplicate-location",
        )


def test_row_order_is_nonmaterial_with_explicit_locations(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [_row("A", reference="line-a"), _row("B", reference="line-b")]
    first = importer.import_rows(rows, source_type="bank_statement", public_id="order-nonmaterial")
    replay = importer.import_rows(
        list(reversed(rows)),
        source_type="bank_statement",
        public_id="order-nonmaterial",
    )
    assert replay.batch_id == first.batch_id
    assert replay.idempotent_count == 2


def test_row_order_is_material_without_explicit_locations(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [_row("A", reference=None), _row("B", reference=None)]
    importer.import_rows(rows, source_type="bank_statement", public_id="order-material")
    with pytest.raises(DuplicatePublicIdError, match="statement import batch"):
        importer.import_rows(
            list(reversed(rows)),
            source_type="bank_statement",
            public_id="order-material",
        )


def test_stable_page_evidence_is_material_but_attachment_path_is_not(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    first = importer.import_rows(
        [_row(payload={"attachment_path": "/tmp/a.pdf", "source_page_number": 1})],
        source_type="bank_statement",
        public_id="evidence-materiality",
        source_file_hash=_sha("same-source"),
    )
    replay = importer.import_rows(
        [_row(payload={"attachment_path": "/tmp/b.pdf", "source_page_number": 1})],
        source_type="bank_statement",
        public_id="evidence-materiality",
        source_file_hash=_sha("same-source"),
    )
    assert replay.batch_id == first.batch_id
    assert replay.idempotent_count == 1
    with pytest.raises(DuplicatePublicIdError, match="source content already belongs"):
        importer.import_rows(
            [_row(payload={"attachment_path": "/tmp/c.pdf", "source_page_number": 2})],
            source_type="bank_statement",
            public_id="evidence-materiality",
            source_file_hash=_sha("same-source"),
        )


def test_audit_binds_content_hash_row_set_actor_causation_and_path(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "audit.csv"
    source_path.write_bytes(b"audited bytes")
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row()],
        source_type="bank_statement",
        public_id="audited-content",
        source_file_path=str(source_path),
        actor_type="human",
        actor_public_id="person-owner",
        authorization_public_id="auth-statement-1",
    )
    events = FinancialAuditRepository(migrated_temp_db_connection).list_chain(
        "statement_import_batch", "audited-content"
    )
    assert len(events) == 2
    event = next(item for item in events if item.event_type == "statement_import_accepted")
    payload = json.loads(event.event_payload_json)["value"]
    assert event.actor_type == "human"
    assert event.actor_public_id == "person-owner"
    assert event.authorization_public_id == "auth-statement-1"
    assert event.causation_public_id == batch.import_command_hash
    assert payload["source_file_hash"] == _sha(b"audited bytes")
    assert payload["row_set_fingerprint"] == batch.row_set_fingerprint
    assert f"source-file-path:{source_path}" in event.source_evidence_references
    assert f"source-file-sha256:{_sha(b'audited bytes')}" in event.source_evidence_references


def test_source_evidence_observation_rolls_back_with_batch(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    class InjectedFailure(RuntimeError):
        pass

    source_path = tmp_path / "rollback.csv"
    source_path.write_bytes(b"rollback evidence")

    def fail_before_commit() -> None:
        raise InjectedFailure("before commit")

    importer = StatementImporter(
        migrated_temp_db_connection,
        _test_pre_commit_hook=fail_before_commit,
    )
    with pytest.raises(InjectedFailure, match="before commit"):
        importer.import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="evidence-rollback",
            source_file_path=str(source_path),
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_import_source_evidence"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_import_batches"
        ).fetchone()[0]
        == 0
    )
    assert not migrated_temp_db_connection.in_transaction


def test_restart_replay_returns_canonical_batch_and_fingerprint(
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "restart.csv"
    source_path.write_bytes(b"restart source")
    first_conn = _connect(migrated_temp_db_path)
    try:
        first = StatementImporter(first_conn).import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="restart-content",
            source_file_path=str(source_path),
        )
        fingerprint = first_conn.execute(
            "SELECT row_fingerprint FROM statement_transactions WHERE batch_id = ?",
            (first.batch_id,),
        ).fetchone()["row_fingerprint"]
    finally:
        first_conn.close()

    replay_conn = _connect(migrated_temp_db_path)
    try:
        replay = StatementImporter(replay_conn).import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="restart-content",
            source_file_path=str(source_path),
        )
        reloaded = replay_conn.execute(
            "SELECT row_fingerprint FROM statement_transactions WHERE batch_id = ?",
            (replay.batch_id,),
        ).fetchone()["row_fingerprint"]
    finally:
        replay_conn.close()
    assert replay.batch_id == first.batch_id
    assert replay.idempotent_count == 1
    assert reloaded == fingerprint


def _race_imports(
    database: Path,
    commands: tuple[Callable[[sqlite3.Connection], object], Callable[[sqlite3.Connection], object]],
) -> tuple[object, object]:
    barrier = Barrier(2)

    def run(command: Callable[[sqlite3.Connection], object]) -> object:
        conn = _connect(database)
        try:
            barrier.wait(timeout=10)
            return command(conn)
        except BaseException as exc:
            if conn.in_transaction:
                conn.rollback()
            return exc
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, command) for command in commands]
        return futures[0].result(timeout=15), futures[1].result(timeout=15)


def test_concurrent_identical_imports_converge_on_one_canonical_result(
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "race.csv"
    source_path.write_bytes(b"identical race")

    def command(conn: sqlite3.Connection) -> object:
        return StatementImporter(conn).import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="content-race-identical",
            source_file_path=str(source_path),
        )

    outcomes = _race_imports(migrated_temp_db_path, (command, command))
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
    batches = outcomes
    assert len({batch.batch_id for batch in batches}) == 1  # type: ignore[attr-defined]
    assert sorted(batch.idempotent_count for batch in batches) == [0, 1]  # type: ignore[attr-defined]
    inspection = _connect(migrated_temp_db_path)
    try:
        batch_count = inspection.execute(
            "SELECT COUNT(*) FROM statement_import_batches"
        ).fetchone()[0]
        assert batch_count == 1
        assert inspection.execute("SELECT COUNT(*) FROM statement_transactions").fetchone()[0] == 1
        assert (
            inspection.execute("SELECT COUNT(*) FROM statement_import_source_evidence").fetchone()[
                0
            ]
            == 1
        )
    finally:
        inspection.close()


def test_concurrent_conflicting_imports_have_one_winner_and_one_conflict(
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.csv"
    second_path = tmp_path / "second.csv"
    first_path.write_bytes(b"first content")
    second_path.write_bytes(b"second content")

    def command_for(path: Path) -> Callable[[sqlite3.Connection], object]:
        return lambda conn: StatementImporter(conn).import_rows(
            [_row()],
            source_type="bank_statement",
            public_id="content-race-conflict",
            source_file_path=str(path),
        )

    outcomes = _race_imports(
        migrated_temp_db_path,
        (command_for(first_path), command_for(second_path)),
    )
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, DuplicatePublicIdError) for outcome in outcomes) == 1
    inspection = _connect(migrated_temp_db_path)
    try:
        batch_count = inspection.execute(
            "SELECT COUNT(*) FROM statement_import_batches"
        ).fetchone()[0]
        assert batch_count == 1
        assert inspection.execute("SELECT COUNT(*) FROM statement_transactions").fetchone()[0] == 1
    finally:
        inspection.close()


def test_legacy_batch_without_source_bytes_remains_explicitly_unverified(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    migrated_temp_db_connection.execute(
        "INSERT INTO statement_import_batches (public_id, source_type) VALUES (?, ?)",
        ("legacy-no-bytes", "bank_statement"),
    )
    migrated_temp_db_connection.commit()
    legacy = migrated_temp_db_connection.execute(
        "SELECT source_file_hash, source_hash_verification_status, import_contract_version "
        "FROM statement_import_batches WHERE public_id = ?",
        ("legacy-no-bytes",),
    ).fetchone()
    assert legacy["source_file_hash"] is None
    assert legacy["source_hash_verification_status"] == "legacy_unverified"
    assert legacy["import_contract_version"] is None


def test_non_file_import_is_marked_unverified_without_fabricated_hash(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row()], source_type="manual_test_fixture", public_id="no-source-bytes"
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT source_file_hash, source_hash_verification_status, import_contract_version "
        "FROM statement_import_batches WHERE id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["source_file_hash"] is None
    assert stored["source_hash_verification_status"] == "unverified_no_source_bytes"
    assert stored["import_contract_version"] == STATEMENT_IMPORT_CONTRACT_VERSION


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("public_id", "tampered-statement-row"),
        ("batch_id", 999_999),
        ("transaction_date", "2099-01-01"),
        ("posted_date", "2099-01-02"),
        ("merchant_raw", "Tampered Merchant"),
        ("merchant_normalized", "tampered merchant"),
        ("amount", "99.99"),
        ("currency", "USD"),
        ("account_id", 999_999),
        ("account_name", "Tampered Account"),
        ("statement_row_reference", "tampered-row-ref"),
        ("raw_row_payload_json", '{"tampered":true}'),
        ("amount_direction", "credit"),
        ("raw_amount", "-99.99"),
        ("raw_amount_type", "credit"),
        ("row_fingerprint", "b" * 64),
        ("row_fingerprint_version", "tampered-version"),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("updated_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_fingerprint_bearing_statement_rows_are_append_only(
    migrated_temp_db_connection: sqlite3.Connection,
    column: str,
    replacement: object,
) -> None:
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row()],
        source_type="bank_statement",
        public_id=f"immutable-row-{column}",
        source_file_hash=_sha(f"immutable-row-{column}"),
    )
    row_id = batch.owned_row_ids[0]

    with pytest.raises(sqlite3.IntegrityError, match="authoritative statement row"):
        migrated_temp_db_connection.execute(
            f"UPDATE statement_transactions SET {column} = ? WHERE id = ?",
            (replacement, row_id),
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("public_id", "tampered-batch"),
        ("source_type", "structured_csv"),
        ("account_id", 999_999),
        ("account_name", "Tampered Account"),
        ("statement_period_start", "2099-01-01"),
        ("statement_period_end", "2099-12-31"),
        ("currency", "USD"),
        ("source_file_path", "/tampered/path.csv"),
        ("source_file_hash", "b" * 64),
        ("source_filename", "tampered.csv"),
        ("source_hash_verification_status", "unverified_no_source_bytes"),
        ("import_contract_version", "statement-import-tampered"),
        ("import_command_hash", "c" * 64),
        ("row_set_fingerprint", "d" * 64),
        ("imported_at", "2099-01-01T00:00:00Z"),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("updated_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_authoritative_statement_batches_are_append_only(
    migrated_temp_db_connection: sqlite3.Connection,
    column: str,
    replacement: object,
) -> None:
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row()],
        source_type="bank_statement",
        public_id=f"immutable-batch-{column}",
        source_file_hash=_sha(f"immutable-batch-{column}"),
    )

    with pytest.raises(sqlite3.IntegrityError, match="authoritative statement batch"):
        migrated_temp_db_connection.execute(
            f"UPDATE statement_import_batches SET {column} = ? WHERE id = ?",
            (replacement, batch.batch_id),
        )


def test_legacy_statement_batch_and_row_cannot_be_promoted_in_place(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    migrated_temp_db_connection.execute(
        """INSERT INTO statement_import_batches
        (public_id, source_type) VALUES ('legacy-no-promotion', 'bank_statement')"""
    )
    batch_id = int(migrated_temp_db_connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    migrated_temp_db_connection.execute(
        """INSERT INTO statement_transactions
        (public_id, batch_id, merchant_raw, amount, currency, row_fingerprint)
        VALUES ('legacy-row-no-promotion', ?, 'Legacy', '1.00', 'SGD', ?)""",
        (batch_id, _sha("legacy-row-no-promotion")),
    )
    row_id = int(migrated_temp_db_connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    with pytest.raises(sqlite3.IntegrityError, match="authoritative statement batch"):
        migrated_temp_db_connection.execute(
            """UPDATE statement_import_batches
            SET import_contract_version = ?, import_command_hash = ?, row_set_fingerprint = ?
            WHERE id = ?""",
            (STATEMENT_IMPORT_CONTRACT_VERSION, "a" * 64, "b" * 64, batch_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="authoritative statement row"):
        migrated_temp_db_connection.execute(
            "UPDATE statement_transactions SET row_fingerprint_version = ? WHERE id = ?",
            (ROW_FINGERPRINT_VERSION, row_id),
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"account_id": 2},
        {"account_name": "Different Account"},
        {"currency": "USD"},
        {"statement_period_start": "2026-06-01"},
        {"statement_period_end": "2026-06-30"},
        {"source_type": "credit_card_statement"},
        {"import_contract_version": "statement-import-v4-test"},
    ],
)
def test_same_source_content_with_changed_command_metadata_conflicts(
    migrated_temp_db_connection: sqlite3.Connection,
    changed: dict[str, object],
) -> None:
    source_hash = _sha("same-source-command-conflict")
    importer = StatementImporter(migrated_temp_db_connection)
    first = importer.import_rows(
        [_row()],
        source_type="bank_statement",
        source_file_hash=source_hash,
        account_name="Canonical Account",
        currency="SGD",
        statement_period_start="2026-07-01",
        statement_period_end="2026-07-31",
    )
    command: dict[str, object] = {
        "source_type": "bank_statement",
        "source_file_hash": source_hash,
        "account_name": "Canonical Account",
        "currency": "SGD",
        "statement_period_start": "2026-07-01",
        "statement_period_end": "2026-07-31",
    }
    command.update(changed)

    with pytest.raises(DuplicatePublicIdError, match="source content already belongs"):
        importer.import_rows([_row()], **command)  # type: ignore[arg-type]

    owned = migrated_temp_db_connection.execute(
        "SELECT id, batch_id FROM statement_transactions"
    ).fetchall()
    assert [(row["id"], row["batch_id"]) for row in owned] == [
        (first.owned_row_ids[0], first.batch_id)
    ]


def test_successful_import_result_audit_and_rows_have_one_batch_owner(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row("Owner A", reference="owner-a"), _row("Owner B", reference="owner-b")],
        source_type="bank_statement",
        public_id="owned-batch",
        source_file_hash=_sha("owned-batch"),
    )
    rows = migrated_temp_db_connection.execute(
        "SELECT id, public_id, batch_id FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    event = next(
        event
        for event in FinancialAuditRepository(migrated_temp_db_connection).list_chain(
            "statement_import_batch", batch.public_id
        )
        if event.event_type == "statement_import_accepted"
    )
    audit_payload = json.loads(event.event_payload_json)["value"]

    assert batch.row_count == len(rows) == len(batch.owned_row_ids) == 2
    assert batch.owned_row_ids == [row["id"] for row in rows]
    assert all(row["batch_id"] == batch.batch_id for row in rows)
    assert sorted(audit_payload["row_public_ids"]) == sorted(row["public_id"] for row in rows)


def test_authoritative_statement_batch_rows_and_source_observations_cannot_be_deleted(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source = tmp_path / "append-only.csv"
    source.write_bytes(b"append-only source evidence")
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [_row()],
        source_type="bank_statement",
        source_file_path=str(source),
    )
    with pytest.raises(sqlite3.IntegrityError, match="authoritative statement row"):
        migrated_temp_db_connection.execute(
            "DELETE FROM statement_transactions WHERE batch_id = ?",
            (batch.batch_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="statement source evidence"):
        migrated_temp_db_connection.execute(
            "DELETE FROM statement_import_source_evidence WHERE batch_id = ?",
            (batch.batch_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="authoritative statement batch"):
        migrated_temp_db_connection.execute(
            "DELETE FROM statement_import_batches WHERE id = ?",
            (batch.batch_id,),
        )
