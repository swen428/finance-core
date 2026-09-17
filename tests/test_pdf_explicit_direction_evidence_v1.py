"""High-risk PDF explicit-direction and page/row evidence contract tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfBlockedReason,
    build_pdf_statement_row_fingerprint,
    normalize_pdf_statement_row_checked,
    normalize_pdf_statement_rows_batch,
    validate_pdf_statement_row,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PDF_EVIDENCE_CONTRACT_VERSION,
    PDF_ROW_FINGERPRINT_VERSION,
    PdfAmountSignConvention,
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
    validate_canonical_normalized_amount_text,
)
from finance_core.reconciliation.pdf_statement_extractor import (
    PdfExtractedLine,
    PdfExtractedPage,
    PdfExtractionResult,
)
from finance_core.reconciliation.pdf_statement_parser_adapter_contract import (
    PdfParserRowPayload,
    PdfParserStatementPayload,
    adapt_pdf_parser_payload_to_statement_rows,
)
from finance_core.reconciliation.pdf_statement_template import PdfStatementTemplate
from finance_core.reconciliation.pdf_statement_template_cli import (
    _parse_amount,
    _parse_row_heuristic,
    parse_pdf_with_template,
)
from finance_core.reconciliation.statement_import import StatementImporter

pytestmark = pytest.mark.migrated_staging_snapshot

SOURCE_HASH = hashlib.sha256(b"synthetic-pdf-direction-evidence-v1").hexdigest()


def _row(**overrides: object) -> ParsedPdfStatementRow:
    values: dict[str, object] = {
        "source_statement_id": "pdf-source-v2",
        "attachment_path": "/synthetic/statement.pdf",
        "description": "Synthetic Merchant",
        "amount": Decimal("12.34"),
        "currency": "SGD",
        "source_page_number": 1,
        "source_row_number": 1,
        "source_row_ref": "page-1:line-1",
        "transaction_date": date(2026, 7, 13),
        "posted_date": date(2026, 7, 14),
        "raw_row_text": "13/07/2026 14/07/2026 Synthetic Merchant 12.34 D",
        "amount_direction": StatementAmountDirection.DEBIT,
        "source_content_hash": SOURCE_HASH,
        "source_filename": "statement.pdf",
        "source_text_excerpt": "13/07/2026 Synthetic Merchant 12.34 D",
        "table_section_id": "transactions",
        "parser_name": "finance-pdf-template-parser",
        "parser_version": "pdf-template-parser-v2",
        "template_name": "synthetic-bank",
        "template_version": "synthetic-bank-v2",
        "extraction_version": "pypdf-text-extraction-v2",
        "evidence_contract_version": PDF_EVIDENCE_CONTRACT_VERSION,
        "direction_source": PdfDirectionSource.EXPLICIT_TOKEN,
        "direction_confidence": PdfDirectionConfidence.HIGH,
        "review_status": PdfRowReviewStatus.AUTHORITATIVE,
        "review_reason": None,
        "original_amount_token": "12.34",
        "original_amount_sign": PdfOriginalAmountSign.POSITIVE,
        "amount_sign_convention": PdfAmountSignConvention.UNSIGNED_EXPLICIT,
        "currency_token": "SGD",
        "currency_source": "parser_column",
        "transaction_date_token": "13/07/2026",
        "posted_date_token": "14/07/2026",
    }
    values.update(overrides)
    return ParsedPdfStatementRow(**values)  # type: ignore[arg-type]


def _template() -> PdfStatementTemplate:
    return PdfStatementTemplate(
        template_id="synthetic-bank",
        institution_name="Synthetic Bank",
        currency="SGD",
        date_format="%d/%m/%Y",
        amount_sign_rules="explicit_only",
        template_version="synthetic-bank-v2",
        direction_field_offset_from_end=1,
    )


@pytest.mark.parametrize(
    "direction",
    [
        StatementAmountDirection.DEBIT,
        StatementAmountDirection.CREDIT,
        StatementAmountDirection.REFUND,
    ],
)
def test_explicit_supported_directions_are_authoritative(
    direction: StatementAmountDirection,
) -> None:
    row = _row(amount_direction=direction)
    assert validate_pdf_statement_row(row).is_valid
    assert normalize_pdf_statement_row_checked(row).amount_direction is direction


def test_default_direction_is_unknown_and_fails_closed() -> None:
    row = ParsedPdfStatementRow(
        source_statement_id="unverified",
        attachment_path="/synthetic/missing.pdf",
        description="Missing Direction",
        amount=Decimal("1.00"),
        currency="SGD",
    )
    assert row.amount_direction is StatementAmountDirection.UNKNOWN
    result = validate_pdf_statement_row(row)
    assert PdfBlockedReason.UNKNOWN_DIRECTION in result.blocked_reasons
    assert PdfBlockedReason.NON_EXPLICIT_DIRECTION in result.blocked_reasons


def test_invalid_direction_is_rejected() -> None:
    row = _row(amount_direction="sideways", direction_source=PdfDirectionSource.INVALID)
    result = validate_pdf_statement_row(row)
    assert PdfBlockedReason.INVALID_DIRECTION in result.blocked_reasons


def test_review_required_row_never_becomes_authoritative() -> None:
    row = _row(
        review_status=PdfRowReviewStatus.REVIEW_REQUIRED,
        review_reason="ambiguous_direction",
    )
    result = normalize_pdf_statement_rows_batch([row])
    assert result.accepted_count == 0
    assert result.blocked_count == 1
    assert PdfBlockedReason.REVIEW_REQUIRED in result.blocked_rows[0].blocked_reasons


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 12.34])
def test_float_nan_and_infinity_are_not_authoritative(value: float) -> None:
    result = validate_pdf_statement_row(_row(amount=value))
    assert PdfBlockedReason.INVALID_AMOUNT in result.blocked_reasons


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_amount_parser_rejects_non_finite_tokens(token: str) -> None:
    assert _parse_amount(token) is None


def test_negative_normalized_amount_is_not_silently_abs_converted() -> None:
    result = validate_pdf_statement_row(
        _row(amount=Decimal("-12.34"), original_amount_sign=PdfOriginalAmountSign.NEGATIVE)
    )
    assert PdfBlockedReason.INVALID_AMOUNT in result.blocked_reasons


@pytest.mark.parametrize(
    ("sign", "direction", "convention"),
    [
        (
            PdfOriginalAmountSign.NEGATIVE,
            StatementAmountDirection.DEBIT,
            PdfAmountSignConvention.UNSIGNED_EXPLICIT,
        ),
        (
            PdfOriginalAmountSign.POSITIVE,
            StatementAmountDirection.CREDIT,
            PdfAmountSignConvention.OUTFLOW_POSITIVE,
        ),
    ],
)
def test_contradictory_sign_and_direction_require_review(
    sign: PdfOriginalAmountSign,
    direction: StatementAmountDirection,
    convention: PdfAmountSignConvention,
) -> None:
    token = "-12.34" if sign is PdfOriginalAmountSign.NEGATIVE else "12.34"
    result = validate_pdf_statement_row(
        _row(
            amount_direction=direction,
            original_amount_token=token,
            original_amount_sign=sign,
            amount_sign_convention=convention,
        )
    )
    assert PdfBlockedReason.SIGN_DIRECTION_CONTRADICTION in result.blocked_reasons


def test_fingerprint_binds_page_row_sign_dates_versions_and_direction() -> None:
    base = _row()
    base_hash = build_pdf_statement_row_fingerprint(base)
    variants = (
        replace(base, source_page_number=2),
        replace(base, source_row_ref="page-1:line-2", source_row_number=2),
        replace(base, original_amount_token="+12.34"),
        replace(base, amount_sign_convention=PdfAmountSignConvention.OUTFLOW_POSITIVE),
        replace(base, direction_source=PdfDirectionSource.EXPLICIT_COLUMN),
        replace(base, direction_confidence=PdfDirectionConfidence.REVIEW),
        replace(base, posted_date=date(2026, 7, 15), posted_date_token="15/07/2026"),
        replace(base, parser_version="pdf-template-parser-v3"),
        replace(base, template_version="synthetic-bank-v3"),
        replace(base, amount_direction=StatementAmountDirection.CREDIT),
    )
    assert all(build_pdf_statement_row_fingerprint(row) != base_hash for row in variants)
    assert (
        build_pdf_statement_row_fingerprint(
            replace(base, attachment_path="/moved/renamed.pdf", source_filename="renamed.pdf")
        )
        == base_hash
    )


def test_complete_evidence_contract_reaches_structured_row() -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    payload = normalized.raw_row_payload
    assert payload is not None
    assert normalized.row_fingerprint_version == PDF_ROW_FINGERPRINT_VERSION
    assert normalized.fingerprint_source_content_hash == SOURCE_HASH
    assert payload["row_fingerprint"] == normalized.row_fingerprint
    assert payload["row_fingerprint_version"] == PDF_ROW_FINGERPRINT_VERSION
    assert payload["source_content_hash"] == SOURCE_HASH
    assert payload["source_page_number"] == 1
    assert payload["source_row_number"] == 1
    assert payload["stable_row_locator"] == "page-1:line-1"
    assert payload["direction_source"] == "explicit_token"
    assert payload["original_amount_token"] == "12.34"
    assert payload["transaction_date_token"] == "13/07/2026"
    assert payload["posted_date_token"] == "14/07/2026"


@pytest.mark.parametrize(
    ("amount_token", "row_currency", "currency_token"),
    [
        ("USD12.34", "SGD", "SGD"),
        ("S$12.34", "USD", "USD"),
        ("RM12.34", "SGD", "SGD"),
        ("USD12.34", "USD", "SGD"),
    ],
)
def test_explicit_amount_currency_conflicts_fail_closed(
    amount_token: str,
    row_currency: str,
    currency_token: str,
) -> None:
    result = validate_pdf_statement_row(
        _row(
            original_amount_token=amount_token,
            currency=row_currency,
            currency_token=currency_token,
        )
    )
    assert PdfBlockedReason.CURRENCY_CONFLICT in result.blocked_reasons


@pytest.mark.parametrize(
    ("amount_token", "row_currency", "currency_token"),
    [
        ("USD12.34", "USD", "USD"),
        ("SGD12.34", "SGD", "SGD"),
        ("S$12.34", "SGD", "SGD"),
        ("MYR12.34", "MYR", "MYR"),
        ("RM12.34", "MYR", "MYR"),
        ("$12.34", "SGD", "SGD"),
    ],
)
def test_matching_amount_currency_evidence_is_authoritative(
    amount_token: str,
    row_currency: str,
    currency_token: str,
) -> None:
    result = validate_pdf_statement_row(
        _row(
            original_amount_token=amount_token,
            currency=row_currency,
            currency_token=currency_token,
        )
    )
    assert PdfBlockedReason.CURRENCY_CONFLICT not in result.blocked_reasons
    assert result.is_valid


def test_ambiguous_dollar_prefix_requires_authoritative_currency_context() -> None:
    result = validate_pdf_statement_row(
        _row(original_amount_token="$12.34", currency="", currency_token="$")
    )
    assert PdfBlockedReason.MISSING_CURRENCY in result.blocked_reasons
    assert result.is_blocked


@pytest.mark.parametrize(
    "value",
    [
        "12.34garbage",
        "12.34 USD",
        "1e2",
        " 12.34",
        "12.34 ",
        "12.345",
        "--12.34",
        "+-12.34",
        "-0",
        "-0.00",
        "NaN",
        "Infinity",
    ],
)
def test_normalized_amount_text_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="canonical normalized amount"):
        validate_canonical_normalized_amount_text(
            value,
            relational_amount=Decimal("12.34") if "12.34" in value else None,
        )


@pytest.mark.parametrize("value", ["0", "12", "12.3", "12.34"])
def test_normalized_amount_text_accepts_canonical_values(value: str) -> None:
    amount = Decimal(value)
    assert validate_canonical_normalized_amount_text(value, relational_amount=amount) == amount


def test_parser_does_not_select_year_account_or_reference_as_amount() -> None:
    parsed = _parse_row_heuristic(
        raw_line="13/07/2026 ACCOUNT 12345678 REF 998877 YEAR 2026 Merchant 45.67 D",
        source_path="/synthetic/statement.pdf",
        template=_template(),
        row_index=0,
        source_content_hash=SOURCE_HASH,
        source_filename="statement.pdf",
        source_page_number=2,
        source_row_number=7,
    )
    assert parsed.amount == Decimal("45.67")
    assert parsed.original_amount_token == "45.67"
    assert parsed.source_row_ref == "page-2:line-7"
    assert parsed.review_status is PdfRowReviewStatus.AUTHORITATIVE


def test_multiple_amount_candidates_require_review() -> None:
    parsed = _parse_row_heuristic(
        raw_line="13/07/2026 Ambiguous Merchant 12.34 56.78 D",
        source_path="/synthetic/statement.pdf",
        template=_template(),
        row_index=0,
        source_content_hash=SOURCE_HASH,
        source_page_number=1,
        source_row_number=1,
    )
    assert parsed.amount is None
    assert parsed.review_status is PdfRowReviewStatus.REVIEW_REQUIRED
    assert "Ambiguous amount candidates" in parsed.warnings


def test_missing_and_invalid_direction_are_distinct_parser_states() -> None:
    missing = _parse_row_heuristic(
        "13/07/2026 Merchant 12.34",
        "/synthetic/statement.pdf",
        _template(),
        0,
    )
    invalid = _parse_row_heuristic(
        "13/07/2026 Merchant 12.34 direction=sideways",
        "/synthetic/statement.pdf",
        _template(),
        0,
    )
    assert missing.amount_direction == "UNKNOWN"
    assert missing.review_status in {
        PdfRowReviewStatus.REVIEW_REQUIRED,
        PdfRowReviewStatus.UNSUPPORTED_LAYOUT,
    }
    assert invalid.review_status is PdfRowReviewStatus.REJECTED
    assert invalid.direction_source is PdfDirectionSource.INVALID


@pytest.mark.parametrize(
    "merchant",
    [
        "PAYMENT SERVICES",
        "FEE FIGHTERS",
        "PURCHASE PLUS",
        "DEPOSIT INSURANCE",
        "MERCHANT REFUND DESK",
    ],
)
def test_direction_like_merchant_text_never_becomes_explicit_direction(
    merchant: str,
) -> None:
    parsed = _parse_row_heuristic(
        f"13/07/2026 {merchant} 12.34",
        "/synthetic/statement.pdf",
        replace(_template(), direction_field_offset_from_end=None),
        0,
    )
    assert parsed.description == merchant
    assert parsed.amount_direction == "UNKNOWN"
    assert parsed.direction_source is PdfDirectionSource.MISSING
    assert parsed.direction_confidence is PdfDirectionConfidence.NONE
    assert parsed.review_status is PdfRowReviewStatus.REVIEW_REQUIRED


def test_positional_direction_field_remains_explicit_with_direction_like_merchant() -> None:
    parsed = _parse_row_heuristic(
        "13/07/2026 PAYMENT SERVICES REFUND DESK 12.34 D",
        "/synthetic/statement.pdf",
        _template(),
        0,
    )
    assert parsed.description == "PAYMENT SERVICES REFUND DESK"
    assert parsed.amount_direction == "DEBIT"
    assert parsed.direction_source is PdfDirectionSource.EXPLICIT_COLUMN
    assert parsed.direction_confidence is PdfDirectionConfidence.HIGH
    assert parsed.review_status is PdfRowReviewStatus.AUTHORITATIVE


@pytest.mark.parametrize("merchant_token", ["PAYMENT", "FEE", "REFUND"])
def test_merchant_after_amount_never_becomes_direction_without_declared_field(
    merchant_token: str,
) -> None:
    parsed = _parse_row_heuristic(
        f"13/07/2026 CAFE 12.34 {merchant_token}",
        "/synthetic/statement.pdf",
        replace(_template(), direction_field_offset_from_end=None),
        0,
    )
    assert merchant_token in parsed.description
    assert parsed.amount_direction == "UNKNOWN"
    assert parsed.direction_confidence is PdfDirectionConfidence.NONE
    assert parsed.review_status is PdfRowReviewStatus.REVIEW_REQUIRED


def test_exact_last_field_direction_contract_is_authoritative() -> None:
    parsed = _parse_row_heuristic(
        "13/07/2026 CAFE 12.34 D",
        "/synthetic/statement.pdf",
        replace(_template(), direction_field_offset_from_end=1),
        0,
    )
    assert parsed.amount_direction == "DEBIT"
    assert parsed.direction_confidence is PdfDirectionConfidence.HIGH
    assert parsed.review_status is PdfRowReviewStatus.AUTHORITATIVE


def test_exact_offset_from_end_ignores_following_memo_aliases() -> None:
    template = replace(
        _template(),
        direction_field_offset_from_end=2,
    )
    parsed = _parse_row_heuristic(
        "13/07/2026 CAFE 12.34 D REFUND",
        "/synthetic/statement.pdf",
        template,
        0,
    )
    assert parsed.amount_direction == "DEBIT"
    assert parsed.direction_confidence is PdfDirectionConfidence.HIGH
    assert "REFUND" in parsed.description


def test_exact_delimited_index_ignores_direction_like_memo() -> None:
    template = replace(
        _template(),
        column_delimiter="|",
        direction_field_offset_from_end=None,
        direction_field_index=3,
    )
    parsed = _parse_row_heuristic(
        "13/07/2026|CAFE|12.34|D|PAYMENT",
        "/synthetic/statement.pdf",
        template,
        0,
    )
    assert parsed.amount_direction == "DEBIT"
    assert parsed.direction_confidence is PdfDirectionConfidence.HIGH
    assert "PAYMENT" in parsed.description


def test_named_delimited_direction_column_is_exact() -> None:
    template = replace(
        _template(),
        column_delimiter="|",
        column_names=("date", "merchant", "amount", "direction", "reference"),
        direction_field_offset_from_end=None,
        direction_field_name="direction",
    )
    parsed = _parse_row_heuristic(
        "13/07/2026|CAFE|12.34|CR|FEE-REFERENCE",
        "/synthetic/statement.pdf",
        template,
        0,
    )
    assert parsed.amount_direction == "CREDIT"
    assert parsed.direction_confidence is PdfDirectionConfidence.HIGH
    assert "FEE-REFERENCE" in parsed.description


def test_missing_or_malformed_declared_direction_field_fails_closed() -> None:
    template = replace(
        _template(),
        column_delimiter="|",
        direction_field_offset_from_end=None,
        direction_field_index=3,
    )
    missing = _parse_row_heuristic(
        "13/07/2026|CAFE|12.34",
        "/synthetic/statement.pdf",
        template,
        0,
    )
    malformed = _parse_row_heuristic(
        "13/07/2026|CAFE|12.34|D memo",
        "/synthetic/statement.pdf",
        template,
        0,
    )
    assert missing.direction_confidence is PdfDirectionConfidence.NONE
    assert missing.review_status is PdfRowReviewStatus.REVIEW_REQUIRED
    assert malformed.direction_confidence is PdfDirectionConfidence.NONE
    assert malformed.review_status is PdfRowReviewStatus.UNSUPPORTED_LAYOUT


def test_transaction_and_posted_date_tokens_remain_separate() -> None:
    parsed = _parse_row_heuristic(
        "13/07/2026 14/07/2026 Merchant 12.34 D",
        "/synthetic/statement.pdf",
        _template(),
        0,
    )
    assert parsed.transaction_date == date(2026, 7, 13)
    assert parsed.posted_date == date(2026, 7, 14)
    assert parsed.transaction_date_token == "13/07/2026"
    assert parsed.posted_date_token == "14/07/2026"


def test_ambiguous_third_date_requires_review() -> None:
    parsed = _parse_row_heuristic(
        "13/07/2026 14/07/2026 15/07/2026 Merchant 12.34 D",
        "/synthetic/statement.pdf",
        _template(),
        0,
    )
    assert parsed.review_status is PdfRowReviewStatus.REVIEW_REQUIRED
    assert "Ambiguous date candidates" in parsed.warnings


def test_multiline_merchant_description_is_preserved_deterministically() -> None:
    parsed = _parse_row_heuristic(
        "13/07/2026 Multi Line\nMerchant Description 12.34 D",
        "/synthetic/statement.pdf",
        _template(),
        0,
        source_content_hash=SOURCE_HASH,
        source_page_number=1,
        source_row_number=1,
    )
    assert parsed.description == "Multi Line Merchant Description"
    assert parsed.source_text_excerpt == parsed.raw_line
    assert parsed.review_status is PdfRowReviewStatus.AUTHORITATIVE


def test_unsupported_layout_fails_closed() -> None:
    parsed = _parse_row_heuristic(
        "UNSUPPORTED",
        "/synthetic/statement.pdf",
        _template(),
        0,
        source_content_hash=SOURCE_HASH,
        source_page_number=1,
        source_row_number=1,
    )
    assert parsed.review_status is PdfRowReviewStatus.UNSUPPORTED_LAYOUT
    assert parsed.review_reason == "unsupported_layout"
    adapted = _row(
        review_status=parsed.review_status,
        review_reason=parsed.review_reason,
    )
    result = normalize_pdf_statement_rows_batch([adapted])
    assert result.accepted_count == 0
    assert PdfBlockedReason.REJECTED in result.blocked_rows[0].blocked_reasons


def test_parse_result_preserves_multipage_duplicate_text(monkeypatch: pytest.MonkeyPatch) -> None:
    line = "13/07/2026 Duplicate Merchant 12.34 D"
    extraction = PdfExtractionResult(
        pdf_path="/synthetic/statement.pdf",
        pages=(
            PdfExtractedPage(
                1,
                (PdfExtractedLine(line, 1), PdfExtractedLine(line, 2)),
                f"{line}\n{line}",
            ),
            PdfExtractedPage(2, (PdfExtractedLine(line, 1),), line),
        ),
        total_pages=2,
        total_lines=3,
        source_content_hash=SOURCE_HASH,
        source_filename="statement.pdf",
    )
    monkeypatch.setattr(
        "finance_core.reconciliation.pdf_statement_template_cli.extract_text_from_pdf",
        lambda _path, **_kwargs: extraction,
    )
    result = parse_pdf_with_template("/synthetic/statement.pdf", "sample_bank_v1")
    assert [row.source_page_number for row in result.rows] == [1, 1, 2]
    assert [row.source_row_ref for row in result.rows] == [
        "page-1:line-1",
        "page-1:line-2",
        "page-2:line-1",
    ]
    adapted = normalize_pdf_statement_rows_batch(
        [
            _row(
                source_page_number=row.source_page_number,
                source_row_number=row.source_row_number,
                source_row_ref=row.source_row_ref,
            )
            for row in result.rows
        ]
    )
    assert adapted.accepted_count == 3
    assert len({row.row_fingerprint for row in adapted.accepted_rows}) == 3


def test_persistence_reload_and_audit_preserve_pdf_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "statement.pdf"
    content = b"%PDF-1.7\nsynthetic direction evidence\n%%EOF\n"
    pdf_path.write_bytes(content)
    source_hash = hashlib.sha256(content).hexdigest()
    normalized = normalize_pdf_statement_row_checked(
        _row(
            attachment_path=str(pdf_path),
            source_content_hash=source_hash,
            source_filename=pdf_path.name,
        )
    )
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalized],
        source_type="bank_statement",
        public_id="pdf-evidence-reload",
        source_file_path=str(pdf_path),
        actor_public_id="synthetic-pdf-parser",
    )
    stored = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    payload = json.loads(stored["raw_row_payload_json"])
    assert payload["source_content_hash"] == source_hash
    assert payload["stable_row_locator"] == "page-1:line-1"
    assert payload["parser_version"] == "pdf-template-parser-v2"
    audit = migrated_temp_db_connection.execute(
        """SELECT event_payload_json FROM financial_audit_events
        WHERE aggregate_public_id = ? AND event_type = 'statement_import_accepted'""",
        (batch.public_id,),
    ).fetchone()
    audit_payload = json.loads(audit["event_payload_json"])["value"]
    assert "pdf_row_evidence" in audit_payload, audit_payload
    assert audit_payload["pdf_row_evidence"][0]["evidence"]["source_page_number"] == 1
    assert migrated_temp_db_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrated_temp_db_connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pdf_evidence_constraint_failure_rolls_back_everything(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "invalid-evidence.pdf"
    content = b"%PDF-1.7\ninvalid evidence\n%%EOF\n"
    pdf_path.write_bytes(content)
    source_hash = hashlib.sha256(content).hexdigest()
    normalized = normalize_pdf_statement_row_checked(
        _row(attachment_path=str(pdf_path), source_content_hash=source_hash)
    )
    assert normalized.raw_row_payload is not None
    bad_payload = dict(normalized.raw_row_payload)
    bad_payload.pop("source_page_number")
    invalid = replace(normalized, raw_row_payload=bad_payload)
    with pytest.raises(Exception, match="invalid authoritative PDF statement evidence"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [invalid],
            source_type="bank_statement",
            public_id="pdf-evidence-rollback",
            source_file_path=str(pdf_path),
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_import_batches "
            "WHERE public_id = 'pdf-evidence-rollback'"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_transactions"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events"
        ).fetchone()[0]
        == 0
    )


def test_audit_failure_rolls_back_pdf_row_and_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "audit-failure.pdf"
    content = b"%PDF-1.7\naudit failure\n%%EOF\n"
    pdf_path.write_bytes(content)
    source_hash = hashlib.sha256(content).hexdigest()
    normalized = normalize_pdf_statement_row_checked(
        _row(attachment_path=str(pdf_path), source_content_hash=source_hash)
    )

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(
        "finance_core.reconciliation.statement_import.append_financial_audit_event",
        fail_audit,
    )
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [normalized],
            source_type="bank_statement",
            public_id="pdf-audit-rollback",
            source_file_path=str(pdf_path),
        )
    for table in (
        "statement_import_batches",
        "statement_import_source_evidence",
        "statement_transactions",
        "financial_audit_events",
    ):
        assert (
            migrated_temp_db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("12.34", Decimal("12.34")),
        ("+12.34", Decimal("12.34")),
        ("-12.34", Decimal("-12.34")),
        ("(12.34)", Decimal("-12.34")),
        ("12.34-", Decimal("-12.34")),
        ("S$12.34", Decimal("12.34")),
        ("S$ 12.34", Decimal("12.34")),
        ("SGD12.34", Decimal("12.34")),
        ("SGD 12.34", Decimal("12.34")),
        ("$12.34", Decimal("12.34")),
        ("$ 12.34", Decimal("12.34")),
        ("RM12.34", Decimal("12.34")),
        ("RM 12.34", Decimal("12.34")),
        ("-S$12.34", Decimal("-12.34")),
        ("S$-12.34", Decimal("-12.34")),
    ],
)
def test_shared_amount_parser_supports_signs_and_currency_prefixes(
    token: str,
    expected: Decimal,
) -> None:
    assert _parse_amount(token) == expected
    adapted = adapt_pdf_parser_payload_to_statement_rows(
        PdfParserStatementPayload(
            source_statement_id="amount-parser",
            attachment_path="/synthetic/statement.pdf",
            rows=(PdfParserRowPayload(amount_raw=token, debit_credit_raw="D"),),
        )
    )
    assert adapted.adapted_count == 1
    assert adapted.adapted_rows[0].amount == abs(expected)


@pytest.mark.parametrize(
    "token",
    [
        "--12.34",
        "+-12.34",
        "12-34",
        "(12.34)-",
        "S$RM12.34",
        "SGD$12.34",
        "12.34SGD",
        "$S$12.34",
    ],
)
def test_shared_amount_parser_rejects_duplicate_or_ambiguous_markers(token: str) -> None:
    assert _parse_amount(token) is None


@pytest.mark.parametrize(
    ("token", "currency", "currency_token"),
    [
        ("-S$12.34", "SGD", "SGD"),
        ("S$-12.34", "SGD", "SGD"),
        ("S$ -12.34", "SGD", "SGD"),
        ("(S$12.34)", "SGD", "SGD"),
        ("RM -12.34", "MYR", "MYR"),
    ],
)
def test_accepted_amount_token_spacing_matches_parser_bridge_payload_and_sqlite(
    migrated_temp_db_connection: sqlite3.Connection,
    token: str,
    currency: str,
    currency_token: str,
) -> None:
    assert _parse_amount(token) == Decimal("-12.34")
    normalized = normalize_pdf_statement_row_checked(
        _row(
            amount_direction=StatementAmountDirection.CREDIT,
            original_amount_token=token,
            original_amount_sign=PdfOriginalAmountSign.NEGATIVE,
            currency=currency,
            currency_token=currency_token,
        )
    )
    assert normalized.raw_amount == token
    assert normalized.raw_row_payload is not None
    assert normalized.raw_row_payload["original_amount_token"] == token
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalized],
        source_type="bank_statement",
        public_id=f"accepted-token-{hashlib.sha256(token.encode()).hexdigest()[:12]}",
        source_file_hash=SOURCE_HASH,
    )
    assert batch.row_count == 1


@pytest.mark.parametrize(
    "token",
    ["- S$12.34", "( S$12.34 )", "+ SGD 12.34"],
)
def test_rejected_amount_token_spacing_matches_parser_bridge_and_sqlite(
    migrated_temp_db_connection: sqlite3.Connection,
    token: str,
) -> None:
    assert _parse_amount(token) is None
    assert (
        PdfBlockedReason.INVALID_AMOUNT
        in validate_pdf_statement_row(
            _row(
                amount_direction=StatementAmountDirection.CREDIT,
                original_amount_token=token,
                original_amount_sign=PdfOriginalAmountSign.NEGATIVE,
            )
        ).blocked_reasons
    )

    normalized = normalize_pdf_statement_row_checked(_row())
    assert normalized.raw_row_payload is not None
    payload = {
        **normalized.raw_row_payload,
        "original_amount_token": token,
        "currency_resolution": {
            "resolved_currency": "SGD",
            "currency_token": "SGD",
            "currency_token_currency": "SGD",
        },
    }
    invalid = replace(normalized, raw_amount=token, raw_row_payload=payload)
    with pytest.raises(Exception, match="invalid authoritative PDF statement evidence"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [invalid],
            source_type="bank_statement",
            public_id=f"rejected-token-{hashlib.sha256(token.encode()).hexdigest()[:12]}",
            source_file_hash=SOURCE_HASH,
        )


def test_bridge_derives_sign_from_source_token_before_declared_sign() -> None:
    result = validate_pdf_statement_row(
        _row(
            amount_direction=StatementAmountDirection.CREDIT,
            original_amount_token="-12.34",
            original_amount_sign=PdfOriginalAmountSign.POSITIVE,
            amount_sign_convention=PdfAmountSignConvention.UNSIGNED_EXPLICIT,
        )
    )
    assert PdfBlockedReason.SIGN_DIRECTION_CONTRADICTION in result.blocked_reasons


@pytest.mark.parametrize("token", ["0", "+0", "-0", "(0)", "0-"])
def test_zero_and_signed_zero_never_become_authoritative(token: str) -> None:
    result = validate_pdf_statement_row(
        _row(
            amount=Decimal("0"),
            original_amount_token=token,
            original_amount_sign=PdfOriginalAmountSign.ZERO,
        )
    )
    assert PdfBlockedReason.ZERO_AMOUNT_REQUIRES_REVIEW in result.blocked_reasons


@pytest.mark.parametrize(
    ("line", "expected_direction", "ambiguous"),
    [
        ("13/07/2026|Merchant|12.34|D/D|memo", "DEBIT", False),
        ("13/07/2026|Merchant|12.34|D/DR|memo", "DEBIT", False),
        ("13/07/2026|Merchant|12.34|D/C|memo", "UNKNOWN", True),
        ("13/07/2026|Merchant|12.34|C/D|memo", "UNKNOWN", True),
        ("13/07/2026|Merchant|12.34|DR/CR|memo", "UNKNOWN", True),
        ("13/07/2026|Merchant|12.34|CR/DR|memo", "UNKNOWN", True),
        ("13/07/2026|UNKNOWN_TOKEN Merchant|12.34|D|memo", "DEBIT", False),
    ],
)
def test_direction_parser_collects_all_semantic_indicators_before_selection(
    line: str,
    expected_direction: str,
    ambiguous: bool,
) -> None:
    template = replace(
        _template(),
        column_delimiter="|",
        column_names=("date", "merchant", "amount", "direction", "memo"),
        direction_field_offset_from_end=None,
        direction_field_name="direction",
    )
    parsed = _parse_row_heuristic(line, "/synthetic/statement.pdf", template, 0)
    assert parsed.amount_direction == expected_direction
    if ambiguous:
        assert parsed.review_reason == "ambiguous_direction"
        assert parsed.review_status is PdfRowReviewStatus.REVIEW_REQUIRED
        adapted = _row(
            amount_direction=StatementAmountDirection.UNKNOWN,
            direction_source=parsed.direction_source,
            direction_confidence=parsed.direction_confidence,
            review_status=parsed.review_status,
            review_reason=parsed.review_reason,
        )
        assert (
            PdfBlockedReason.AMBIGUOUS_DIRECTION
            in validate_pdf_statement_row(adapted).blocked_reasons
        )
    else:
        assert parsed.review_status is PdfRowReviewStatus.AUTHORITATIVE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_page_number", 0),
        ("source_page_number", -1),
        ("source_page_number", True),
        ("source_row_number", 0),
        ("source_row_number", -1),
        ("source_row_number", True),
        ("source_row_ref", None),
        ("source_row_ref", ""),
        ("source_row_ref", "   "),
        ("source_row_ref", " page-1:line-1 "),
    ],
)
def test_page_and_row_coordinates_fail_closed(field: str, value: object) -> None:
    result = validate_pdf_statement_row(_row(**{field: value}))
    assert result.is_blocked


_REQUIRED_PDF_EVIDENCE_KEYS = (
    "evidence_contract_version",
    "row_fingerprint",
    "row_fingerprint_version",
    "review_status",
    "source_content_hash",
    "source_page_number",
    "stable_row_locator",
    "attachment_path",
    "source_text_excerpt",
    "original_line_text",
    "parser_name",
    "parser_version",
    "template_name",
    "template_version",
    "extraction_version",
    "direction_source",
    "direction_confidence",
    "direction",
    "original_amount_token",
    "original_amount_sign",
    "amount_sign_convention",
    "normalized_amount",
    "currency_token",
    "currency_source",
    "currency_resolution",
    "transaction_date_token",
    "posted_date_token",
)


@pytest.mark.parametrize("key", _REQUIRED_PDF_EVIDENCE_KEYS)
@pytest.mark.parametrize("case", ["omitted", "json_null", "wrong_type"])
def test_required_pdf_json_keys_cannot_bypass_sqlite_validation(
    migrated_temp_db_connection: sqlite3.Connection,
    key: str,
    case: str,
) -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    assert normalized.raw_row_payload is not None
    payload = dict(normalized.raw_row_payload)
    if case == "omitted":
        payload.pop(key)
    elif case == "json_null":
        payload[key] = None
    else:
        payload[key] = "1" if key == "source_page_number" else 1
    invalid = replace(normalized, raw_row_payload=payload)

    with pytest.raises(Exception, match="invalid authoritative PDF statement evidence"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [invalid],
            source_type="bank_statement",
            public_id=f"pdf-json-{key}-{case}",
            source_file_hash=SOURCE_HASH,
        )


_INVALID_PDF_EVIDENCE_VALUES = (
    ("evidence_contract_version", "wrong-version"),
    ("row_fingerprint", "not-a-hash"),
    ("row_fingerprint_version", "wrong-version"),
    ("review_status", "review_required"),
    ("source_content_hash", "not-a-hash"),
    ("source_page_number", 0),
    ("stable_row_locator", ""),
    ("attachment_path", ""),
    ("source_text_excerpt", ""),
    ("original_line_text", ""),
    ("parser_name", ""),
    ("parser_version", ""),
    ("template_name", ""),
    ("template_version", ""),
    ("extraction_version", ""),
    ("direction_source", "template_assumption"),
    ("direction_confidence", "review"),
    ("direction", "credit"),
    ("original_amount_token", ""),
    ("original_amount_sign", "invalid"),
    ("amount_sign_convention", "unknown"),
    ("normalized_amount", ""),
    ("normalized_amount", "12.34garbage"),
    ("normalized_amount", "12.34 USD"),
    ("normalized_amount", "1e2"),
    ("normalized_amount", " 12.34"),
    ("normalized_amount", "12.34 "),
    ("normalized_amount", "12.345"),
    ("normalized_amount", "--12.34"),
    ("normalized_amount", "-0.00"),
    ("normalized_amount", "NaN"),
    ("normalized_amount", "Infinity"),
    ("currency_token", ""),
    ("currency_source", ""),
    ("transaction_date_token", ""),
    ("posted_date_token", ""),
)


@pytest.mark.parametrize(("key", "value"), _INVALID_PDF_EVIDENCE_VALUES)
def test_required_pdf_json_keys_reject_invalid_or_empty_values(
    migrated_temp_db_connection: sqlite3.Connection,
    key: str,
    value: object,
) -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    assert normalized.raw_row_payload is not None
    payload = {**normalized.raw_row_payload, key: value}
    invalid = replace(normalized, raw_row_payload=payload)
    with pytest.raises(Exception, match="invalid authoritative PDF statement evidence"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [invalid],
            source_type="bank_statement",
            public_id=f"pdf-json-invalid-{key}",
            source_file_hash=SOURCE_HASH,
        )


def test_sqlite_rejects_explicit_amount_currency_disagreement(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    assert normalized.raw_row_payload is not None
    payload = {
        **normalized.raw_row_payload,
        "original_amount_token": "USD12.34",
    }
    invalid = replace(
        normalized,
        raw_amount="USD12.34",
        raw_row_payload=payload,
    )
    with pytest.raises(Exception, match="invalid authoritative PDF statement evidence"):
        StatementImporter(migrated_temp_db_connection).import_rows(
            [invalid],
            source_type="bank_statement",
            public_id="pdf-json-currency-conflict",
            source_file_hash=SOURCE_HASH,
        )


def test_malformed_pdf_json_fails_with_stable_constraint_error(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalized],
        source_type="bank_statement",
        public_id="pdf-malformed-json",
        source_file_hash=SOURCE_HASH,
    )
    migrated_temp_db_connection.execute("DROP TRIGGER trg_authoritative_statement_row_no_update")
    with pytest.raises(
        sqlite3.IntegrityError, match="invalid authoritative PDF statement evidence"
    ):
        migrated_temp_db_connection.execute(
            "UPDATE statement_transactions SET raw_row_payload_json = '{' WHERE id = ?",
            (batch.owned_row_ids[0],),
        )


@pytest.mark.parametrize("key", _REQUIRED_PDF_EVIDENCE_KEYS)
@pytest.mark.parametrize("case", ["omitted", "json_null", "wrong_type"])
def test_required_pdf_json_key_updates_are_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    key: str,
    case: str,
) -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalized],
        source_type="bank_statement",
        public_id=f"pdf-json-update-{key}-{case}",
        source_file_hash=SOURCE_HASH,
    )
    assert normalized.raw_row_payload is not None
    payload = dict(normalized.raw_row_payload)
    if case == "omitted":
        payload.pop(key)
    elif case == "json_null":
        payload[key] = None
    else:
        payload[key] = "1" if key == "source_page_number" else 1
    migrated_temp_db_connection.execute("DROP TRIGGER trg_authoritative_statement_row_no_update")
    with pytest.raises(
        sqlite3.IntegrityError, match="invalid authoritative PDF statement evidence"
    ):
        migrated_temp_db_connection.execute(
            "UPDATE statement_transactions SET raw_row_payload_json = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), batch.owned_row_ids[0]),
        )


@pytest.mark.parametrize(("key", "value"), _INVALID_PDF_EVIDENCE_VALUES)
def test_invalid_pdf_json_key_updates_are_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    key: str,
    value: object,
) -> None:
    normalized = normalize_pdf_statement_row_checked(_row())
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalized],
        source_type="bank_statement",
        public_id=f"pdf-json-invalid-update-{key}",
        source_file_hash=SOURCE_HASH,
    )
    assert normalized.raw_row_payload is not None
    payload = {**normalized.raw_row_payload, key: value}
    migrated_temp_db_connection.execute("DROP TRIGGER trg_authoritative_statement_row_no_update")
    with pytest.raises(
        sqlite3.IntegrityError, match="invalid authoritative PDF statement evidence"
    ):
        migrated_temp_db_connection.execute(
            "UPDATE statement_transactions SET raw_row_payload_json = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), batch.owned_row_ids[0]),
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("public_id", "tampered-pdf-row"),
        ("batch_id", 999_999),
        ("transaction_date", "2099-01-01"),
        ("posted_date", "2099-01-02"),
        ("merchant_raw", "Tampered PDF Merchant"),
        ("merchant_normalized", "tampered pdf merchant"),
        ("amount", "99.99"),
        ("currency", "USD"),
        ("account_id", 999_999),
        ("account_name", "Tampered PDF Account"),
        ("statement_row_reference", "tampered-pdf-locator"),
        ("raw_row_payload_json", '{"tampered":true}'),
        ("amount_direction", "credit"),
        ("raw_amount", "-99.99"),
        ("raw_amount_type", "credit"),
        ("row_fingerprint", "b" * 64),
        ("row_fingerprint_version", "tampered-pdf-version"),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("updated_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_pdf_fingerprint_rows_are_append_only_across_relational_material(
    migrated_temp_db_connection: sqlite3.Connection,
    column: str,
    replacement: object,
) -> None:
    batch = StatementImporter(migrated_temp_db_connection).import_rows(
        [normalize_pdf_statement_row_checked(_row())],
        source_type="bank_statement",
        public_id=f"immutable-pdf-row-{column}",
        source_file_hash=SOURCE_HASH,
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="authoritative statement row|invalid authoritative PDF statement evidence",
    ):
        migrated_temp_db_connection.execute(
            f"UPDATE statement_transactions SET {column} = ? WHERE id = ?",
            (replacement, batch.owned_row_ids[0]),
        )
