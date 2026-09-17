import sqlite3
from pathlib import Path

import pytest

RAW_INTAKE_SOURCE_TYPES = [
    "telegram_text",
    "telegram_image",
    "telegram_pdf",
    "uploaded_pdf",
    "bank_statement_pdf",
    "credit_card_statement_pdf",
    "statement_row",
    "manual_entry",
    "system_generated",
]

RAW_INTAKE_SOURCE_CHANNELS = [
    "telegram",
    "manual",
    "upload",
    "system",
    "imported_statement",
]


@pytest.fixture()
def source_evidence_db(
    migrated_temp_db_connection: sqlite3.Connection,
) -> sqlite3.Connection:
    return migrated_temp_db_connection


@pytest.mark.parametrize("source_type", RAW_INTAKE_SOURCE_TYPES)
def test_raw_intake_records_accept_supported_source_types(
    source_evidence_db: sqlite3.Connection,
    source_type: str,
) -> None:
    source_evidence_db.execute(
        """
        INSERT INTO raw_intake_records (
          public_id,
          source_type,
          source_channel,
          raw_input,
          received_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            f"raw_intake_{source_type}",
            source_type,
            "telegram" if source_type.startswith("telegram_") else "manual",
            "Original source text is preserved.",
            "2026-06-04T10:00:00+00:00",
        ),
    )

    row = source_evidence_db.execute(
        """
        SELECT source_type, source_channel, raw_input
        FROM raw_intake_records
        WHERE public_id = ?
        """,
        (f"raw_intake_{source_type}",),
    ).fetchone()
    assert row["source_type"] == source_type
    assert row["source_channel"] in RAW_INTAKE_SOURCE_CHANNELS
    assert row["raw_input"] == "Original source text is preserved."


def test_raw_intake_records_reject_unknown_source_type(
    source_evidence_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        source_evidence_db.execute(
            """
            INSERT INTO raw_intake_records (
              public_id,
              source_type,
              source_channel,
              raw_input,
              received_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "raw_intake_unknown_source_type",
                "email_forward",
                "manual",
                "Original source text is preserved.",
                "2026-06-04T10:00:00+00:00",
            ),
        )


def test_source_channel_is_separate_from_source_type(
    source_evidence_db: sqlite3.Connection,
) -> None:
    source_evidence_db.execute(
        """
        INSERT INTO raw_intake_records (
          public_id,
          source_type,
          source_channel,
          external_source_id,
          idempotency_key,
          raw_input,
          received_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "raw_intake_uploaded_statement_pdf",
            "bank_statement_pdf",
            "upload",
            "upload:statement:dbs:2026-05",
            "raw-intake:upload:statement:dbs:2026-05",
            "DBS statement PDF uploaded for May 2026.",
            "2026-06-04T10:00:00+00:00",
        ),
    )

    row = source_evidence_db.execute(
        """
        SELECT source_type, source_channel, external_source_id, idempotency_key
        FROM raw_intake_records
        WHERE public_id = 'raw_intake_uploaded_statement_pdf'
        """
    ).fetchone()
    assert row["source_type"] == "bank_statement_pdf"
    assert row["source_channel"] == "upload"
    assert row["external_source_id"] == "upload:statement:dbs:2026-05"
    assert row["idempotency_key"] == "raw-intake:upload:statement:dbs:2026-05"


def test_evidence_rows_can_represent_attachment_ocr_pdf_and_statement_row(
    source_evidence_db: sqlite3.Connection,
) -> None:
    source_evidence_db.execute(
        """
        INSERT INTO attachments (
          public_id,
          attachment_type,
          file_path,
          original_filename,
          mime_type,
          file_hash,
          source_channel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "att_statement_pdf_001",
            "pdf",
            "attachments/statements/dbs_may_2026.pdf",
            "dbs_may_2026.pdf",
            "application/pdf",
            "sha256:statement-file",
            "upload",
        ),
    )
    attachment_id = source_evidence_db.execute(
        "SELECT id FROM attachments WHERE public_id = 'att_statement_pdf_001'"
    ).fetchone()["id"]
    source_evidence_db.execute(
        """
        INSERT INTO raw_intake_records (
          public_id,
          source_type,
          source_channel,
          raw_input,
          received_at,
          attachment_path,
          attachment_id,
          source_content_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "raw_intake_statement_pdf_001",
            "bank_statement_pdf",
            "upload",
            "DBS May 2026 statement source.",
            "2026-06-04T10:00:00+00:00",
            "attachments/statements/dbs_may_2026.pdf",
            attachment_id,
            "sha256:statement-file",
        ),
    )
    raw_intake_id = source_evidence_db.execute(
        "SELECT id FROM raw_intake_records WHERE public_id = ?",
        ("raw_intake_statement_pdf_001",),
    ).fetchone()["id"]

    source_evidence_db.execute(
        """
        INSERT INTO raw_intake_evidence (
          public_id,
          raw_intake_record_id,
          attachment_id,
          evidence_type,
          attachment_path,
          source_file_hash,
          ocr_text,
          pdf_page_number,
          ocr_bounding_box,
          statement_row_index,
          statement_transaction_date,
          statement_posted_date,
          parser_name,
          parser_version,
          confidence_score,
          extraction_method,
          evidence_reference
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "evidence_statement_pdf_row_001",
            raw_intake_id,
            attachment_id,
            "statement_row",
            "attachments/statements/dbs_may_2026.pdf",
            "sha256:statement-file",
            "PAYNOW TRANSFER SGD 12.50",
            2,
            '{"x":42,"y":120,"width":220,"height":18}',
            17,
            "2026-05-30",
            "2026-05-31",
            "pdf_statement_parser",
            "v1",
            0.88,
            "pdf_table_extraction",
            "att_statement_pdf_001#page=2&row=17",
        ),
    )

    row = source_evidence_db.execute(
        """
        SELECT evidence_type, attachment_path, ocr_text, pdf_page_number,
               ocr_bounding_box, statement_row_index,
               statement_transaction_date, statement_posted_date,
               parser_name, parser_version, confidence_score,
               extraction_method, evidence_reference
        FROM raw_intake_evidence
        WHERE public_id = 'evidence_statement_pdf_row_001'
        """
    ).fetchone()
    assert row["evidence_type"] == "statement_row"
    assert row["attachment_path"] == "attachments/statements/dbs_may_2026.pdf"
    assert row["ocr_text"] == "PAYNOW TRANSFER SGD 12.50"
    assert row["pdf_page_number"] == 2
    assert row["ocr_bounding_box"] == '{"x":42,"y":120,"width":220,"height":18}'
    assert row["statement_row_index"] == 17
    assert row["statement_transaction_date"] == "2026-05-30"
    assert row["statement_posted_date"] == "2026-05-31"
    assert row["parser_name"] == "pdf_statement_parser"
    assert row["parser_version"] == "v1"
    assert row["confidence_score"] == 0.88
    assert row["extraction_method"] == "pdf_table_extraction"
    assert row["evidence_reference"] == "att_statement_pdf_001#page=2&row=17"


def test_idempotency_key_prevents_duplicate_raw_intake_records(
    source_evidence_db: sqlite3.Connection,
) -> None:
    values = (
        "telegram_text",
        "telegram",
        "telegram:chat-123:message-456",
        "raw-intake:telegram:chat-123:message-456",
        "sha256:telegram-message-body",
        "Lunch SGD 12.50",
        "2026-06-04T10:00:00+00:00",
    )
    source_evidence_db.execute(
        """
        INSERT INTO raw_intake_records (
          source_type,
          source_channel,
          external_source_id,
          idempotency_key,
          source_content_hash,
          raw_input,
          received_at,
          public_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'raw_intake_duplicate_001')
        """,
        values,
    )

    with pytest.raises(sqlite3.IntegrityError):
        source_evidence_db.execute(
            """
            INSERT INTO raw_intake_records (
              source_type,
              source_channel,
              external_source_id,
              idempotency_key,
              source_content_hash,
              raw_input,
              received_at,
              public_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'raw_intake_duplicate_002')
            """,
            values,
        )


def test_source_evidence_schema_uses_temporary_database_only(
    source_evidence_db: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = source_evidence_db.execute("PRAGMA database_list").fetchone()["file"]

    assert Path(database_path) == temp_db_path
