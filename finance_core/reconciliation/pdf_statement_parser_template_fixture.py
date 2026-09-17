"""PDF Statement Parser Template Fixture v1.

Deterministic synthetic parser template fixture that models how future
bank-specific PDF statement parsers will produce structured payloads
before they are adapted into ``ParsedPdfStatementRow`` objects.

This module does NOT implement real PDF parsing. It only provides
synthetic in-memory template fixtures that exercise the adapter
contract and full review chain.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No pdfplumber, pytesseract, pypdf, camelot, tabula, or other PDF/OCR deps.
- No database connection or SQL execution.
- No file I/O (does not open ``attachment_path`` or any other file).
- No mutation of final financial records.
- No statement import persistence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from finance_core.reconciliation.pdf_statement_bridge import (
    normalize_pdf_statement_rows_batch,
)
from finance_core.reconciliation.pdf_statement_import_review_fixture import (
    build_pdf_statement_import_review_fixture,
    export_pdf_statement_import_review_audit_payload,
    export_pdf_statement_import_review_dashboard_payload,
)
from finance_core.reconciliation.pdf_statement_parser_adapter_contract import (
    PdfParserRowPayload,
    PdfParserSmokeTestResult,
    PdfParserStatementPayload,
    adapt_pdf_parser_payload_to_statement_rows,
    run_smoke_test,
)

# ---------------------------------------------------------------------------
# Template name constants
# ---------------------------------------------------------------------------


class PdfParserTemplateName:
    """Stable template name constants for parser template fixtures.

    Each constant is a deterministic identifier used to look up a
    specific synthetic template payload via
    ``build_pdf_parser_template_payload()``.
    """

    ACCEPTED_ONLY: str = "accepted_only"
    BLOCKED_MIXED: str = "blocked_mixed"
    MULTI_PAGE: str = "multi_page"
    DEBIT_CREDIT_DIRECTION: str = "debit_credit_direction"
    MALFORMED_RAW_DATA: str = "malformed_raw_data"


# ---------------------------------------------------------------------------
# Template fixture result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfParserTemplateFixtureResult:
    """Result of running a parser template fixture through the full chain.

    Carries the results from every stage of the PDF statement pipeline:
    adapter -> batch normalizer -> import review fixture -> dashboard
    and audit payloads, plus the template name for traceability.

    Fields
    ------
    template_name:
        The template name used to produce this result.
    source_statement_id:
        The stable source statement identifier from the template.
    smoke_result:
        The full-chain smoke test result.
    dashboard_payload:
        Dashboard-safe export payload.
    audit_payload:
        Audit export payload with source evidence.
    """

    template_name: str
    source_statement_id: str
    smoke_result: PdfParserSmokeTestResult
    dashboard_payload: dict[str, Any]
    audit_payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Template: accepted_only
# ---------------------------------------------------------------------------


def _build_accepted_only_template() -> PdfParserStatementPayload:
    """Build a synthetic template where every row has valid, parseable fields.

    All rows should pass adapter adaptation and bridge validation
    without blocking.
    """
    return PdfParserStatementPayload(
        source_statement_id="tmpl-accepted-001",
        attachment_path="/templates/accepted_only_stmt.pdf",
        rows=(
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r1",
                transaction_date_raw="2026-06-28",
                posted_date_raw="2026-06-29",
                description="Coffee Shop",
                merchant_raw="",
                amount_raw="5.50",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="28/06 Coffee Shop  5.50 D",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r2",
                transaction_date_raw="2026-06-28",
                posted_date_raw="2026-06-29",
                description="Grocery Mart",
                merchant_raw="Tesco Extra",
                amount_raw="45.80",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="28/06 Tesco Extra  45.80",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r3",
                transaction_date_raw="2026-06-27",
                posted_date_raw="2026-06-28",
                description="Salary Deposit",
                merchant_raw="ACME Corp Payroll",
                amount_raw="8000.00",
                currency_raw="MYR",
                debit_credit_raw="CR",
                raw_row_text="27/06 ACME Corp Payroll  8000.00 CR",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r4",
                transaction_date_raw="2026-06-26",
                posted_date_raw="2026-06-27",
                description="Online Subscription",
                merchant_raw="Netflix",
                amount_raw="55.90",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="26/06 Netflix  55.90",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r5",
                transaction_date_raw="2026-06-25",
                posted_date_raw="2026-06-26",
                description="Transfer to Savings",
                merchant_raw="",
                amount_raw="500.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="25/06 Transfer to Savings  500.00",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Template: blocked_mixed
# ---------------------------------------------------------------------------


def _build_blocked_mixed_template() -> PdfParserStatementPayload:
    """Build a synthetic template with a mix of valid and blocked rows.

    Some rows have unparseable amounts, missing currencies, or unknown
    directions that will be blocked at either the adapter or bridge level.
    """
    return PdfParserStatementPayload(
        source_statement_id="tmpl-blocked-mixed-001",
        attachment_path="/templates/blocked_mixed_stmt.pdf",
        rows=(
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r1",
                transaction_date_raw="2026-06-28",
                description="Valid Coffee",
                amount_raw="5.50",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="28/06 Valid Coffee  5.50",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r2",
                transaction_date_raw="2026-06-28",
                description="Unparseable Amount Row",
                amount_raw="???",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="28/06 Unparseable Amount  ???",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r3",
                transaction_date_raw="2026-06-27",
                description="No Currency Row",
                amount_raw="20.00",
                currency_raw="",
                debit_credit_raw="D",
                raw_row_text="27/06 No Currency  20.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r4",
                transaction_date_raw="2026-06-26",
                description="Unknown Direction Row",
                amount_raw="100.00",
                currency_raw="MYR",
                debit_credit_raw="XYZ",
                raw_row_text="26/06 Unknown Direction  100.00 XYZ",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r5",
                transaction_date_raw="2026-06-25",
                description="Another Valid Row",
                amount_raw="75.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="25/06 Another Valid  75.00",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Template: multi_page
# ---------------------------------------------------------------------------


def _build_multi_page_template() -> PdfParserStatementPayload:
    """Build a synthetic template simulating a multi-page bank statement.

    Rows span pages 1, 2, and 3 with stable page/row references.
    """
    return PdfParserStatementPayload(
        source_statement_id="tmpl-multi-page-001",
        attachment_path="/templates/multi_page_stmt.pdf",
        rows=(
            # Page 1
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r1",
                transaction_date_raw="2026-06-01",
                description="Opening Balance",
                amount_raw="0.00",
                currency_raw="MYR",
                debit_credit_raw="CR",
                raw_row_text="01/06 Opening Balance  0.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r2",
                transaction_date_raw="2026-06-02",
                description="Lunch",
                amount_raw="12.50",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="02/06 Lunch  12.50",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r3",
                transaction_date_raw="2026-06-03",
                description="Petrol",
                amount_raw="80.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="03/06 Petrol  80.00",
            ),
            # Page 2
            PdfParserRowPayload(
                source_page_number=2,
                source_row_ref="p2r1",
                transaction_date_raw="2026-06-10",
                description="Dinner",
                merchant_raw="Sushi King",
                amount_raw="150.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="10/06 Sushi King  150.00",
            ),
            PdfParserRowPayload(
                source_page_number=2,
                source_row_ref="p2r2",
                transaction_date_raw="2026-06-12",
                description="Salary",
                merchant_raw="ACME Corp",
                amount_raw="8000.00",
                currency_raw="MYR",
                debit_credit_raw="CR",
                raw_row_text="12/06 ACME Corp  8000.00 CR",
            ),
            PdfParserRowPayload(
                source_page_number=2,
                source_row_ref="p2r3",
                transaction_date_raw="2026-06-15",
                description="Electricity Bill",
                amount_raw="250.50",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="15/06 Electricity Bill  250.50",
            ),
            # Page 3
            PdfParserRowPayload(
                source_page_number=3,
                source_row_ref="p3r1",
                transaction_date_raw="2026-06-20",
                description="Internet Bill",
                amount_raw="120.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="20/06 Internet Bill  120.00",
            ),
            PdfParserRowPayload(
                source_page_number=3,
                source_row_ref="p3r2",
                transaction_date_raw="2026-06-28",
                description="Refund - Return",
                amount_raw="55.00",
                currency_raw="MYR",
                debit_credit_raw="CR",
                raw_row_text="28/06 Refund - Return  55.00 CR",
            ),
            PdfParserRowPayload(
                source_page_number=3,
                source_row_ref="p3r3",
                transaction_date_raw="2026-06-30",
                description="Closing Balance",
                amount_raw="7437.00",
                currency_raw="MYR",
                debit_credit_raw="CR",
                raw_row_text="30/06 Closing Balance  7437.00",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Template: debit_credit_direction
# ---------------------------------------------------------------------------


def _build_debit_credit_direction_template() -> PdfParserStatementPayload:
    """Build a synthetic template exercising various debit/credit direction
    indicators.

    Covers explicit indicators (D, DR, C, CR, DEBIT, CREDIT, PURCHASE,
    PAYMENT, DEPOSIT, REFUND) and sign-inferred directions (negative
    amounts, parenthesised amounts).
    """
    return PdfParserStatementPayload(
        source_statement_id="tmpl-direction-001",
        attachment_path="/templates/debit_credit_direction_stmt.pdf",
        rows=(
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r1",
                transaction_date_raw="2026-06-01",
                description="D Indicator",
                amount_raw="10.00",
                currency_raw="SGD",
                debit_credit_raw="D",
                raw_row_text="01/06 D Indicator  10.00 D",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r2",
                transaction_date_raw="2026-06-02",
                description="DR Indicator",
                amount_raw="20.00",
                currency_raw="SGD",
                debit_credit_raw="DR",
                raw_row_text="02/06 DR Indicator  20.00 DR",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r3",
                transaction_date_raw="2026-06-03",
                description="C Indicator",
                amount_raw="500.00",
                currency_raw="SGD",
                debit_credit_raw="C",
                raw_row_text="03/06 C Indicator  500.00 C",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r4",
                transaction_date_raw="2026-06-04",
                description="CR Indicator",
                amount_raw="300.00",
                currency_raw="SGD",
                debit_credit_raw="CR",
                raw_row_text="04/06 CR Indicator  300.00 CR",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r5",
                transaction_date_raw="2026-06-05",
                description="DEBIT Keyword",
                amount_raw="40.00",
                currency_raw="SGD",
                debit_credit_raw="DEBIT",
                raw_row_text="05/06 DEBIT Keyword  40.00 DEBIT",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r6",
                transaction_date_raw="2026-06-06",
                description="CREDIT Keyword",
                amount_raw="600.00",
                currency_raw="SGD",
                debit_credit_raw="CREDIT",
                raw_row_text="06/06 CREDIT Keyword  600.00 CREDIT",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r7",
                transaction_date_raw="2026-06-07",
                description="PURCHASE Keyword",
                amount_raw="15.00",
                currency_raw="SGD",
                debit_credit_raw="PURCHASE",
                raw_row_text="07/06 PURCHASE Keyword  15.00 PURCHASE",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r8",
                transaction_date_raw="2026-06-08",
                description="DEPOSIT Keyword",
                amount_raw="1000.00",
                currency_raw="SGD",
                debit_credit_raw="DEPOSIT",
                raw_row_text="08/06 DEPOSIT Keyword  1000.00 DEPOSIT",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r9",
                transaction_date_raw="2026-06-09",
                description="REFUND Keyword",
                amount_raw="25.00",
                currency_raw="SGD",
                debit_credit_raw="REFUND",
                raw_row_text="09/06 REFUND Keyword  25.00 REFUND",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r10",
                transaction_date_raw="2026-06-10",
                description="Negative Amount (Sign-Inferred)",
                amount_raw="-50.00",
                currency_raw="SGD",
                debit_credit_raw=None,
                raw_row_text="10/06 Negative Amount  -50.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r11",
                transaction_date_raw="2026-06-11",
                description="Parentheses Amount",
                amount_raw="(200.00)",
                currency_raw="SGD",
                debit_credit_raw=None,
                raw_row_text="11/06 Parentheses Amount  (200.00)",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r12",
                transaction_date_raw="2026-06-12",
                description="PAYMENT Keyword",
                amount_raw="350.00",
                currency_raw="SGD",
                debit_credit_raw="PAYMENT",
                raw_row_text="12/06 PAYMENT Keyword  350.00 PAYMENT",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Template: malformed_raw_data
# ---------------------------------------------------------------------------


def _build_malformed_raw_data_template() -> PdfParserStatementPayload:
    """Build a synthetic template with garbled or malformed raw data.

    Some rows have partial fields, others have garbled raw_text but
    parseable structured fields. Tests the resilience boundary:
    can the adapter identify what is parseable vs what must be blocked?
    """
    return PdfParserStatementPayload(
        source_statement_id="tmpl-malformed-001",
        attachment_path="/templates/malformed_raw_data_stmt.pdf",
        rows=(
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r1",
                transaction_date_raw="2026-06-01",
                description="Normal Row",
                amount_raw="100.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="01/06 Normal Row  100.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r2",
                transaction_date_raw="2026-06-02",
                description="Garbled Amount",
                amount_raw="RMabc-50.00xyz",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="02/06 Garbled Amount  RMabc-50.00xyz",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r3",
                transaction_date_raw="2026-06-03",
                description="",
                amount_raw="50.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="03/06    50.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r4",
                transaction_date_raw="not-a-date",
                description="Unparseable Date Row",
                amount_raw="30.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="xXx Unparseable Date Row  30.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r5",
                transaction_date_raw=None,
                posted_date_raw=None,
                description="No Dates Row",
                amount_raw="75.00",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="No Dates Row  75.00",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r6",
                transaction_date_raw="2026-06-06",
                description="Raw Text Only",
                amount_raw=None,
                currency_raw="",
                debit_credit_raw=None,
                raw_row_text="06/06 Raw Text Only - No structured fields parsed",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r7",
                transaction_date_raw="2026-06-07",
                description="Comma Amount",
                amount_raw="1,234.56",
                currency_raw="MYR",
                debit_credit_raw="D",
                raw_row_text="07/06 Comma Amount  1,234.56",
            ),
            PdfParserRowPayload(
                source_page_number=1,
                source_row_ref="p1r8",
                transaction_date_raw="2026-06-08",
                description="Currency Symbol Amount",
                amount_raw="$99.00",
                currency_raw="SGD",
                debit_credit_raw="D",
                raw_row_text="08/06 Currency Symbol  $99.00",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------


_TEMPLATE_BUILDERS: dict[str, Any] = {
    PdfParserTemplateName.ACCEPTED_ONLY: _build_accepted_only_template,
    PdfParserTemplateName.BLOCKED_MIXED: _build_blocked_mixed_template,
    PdfParserTemplateName.MULTI_PAGE: _build_multi_page_template,
    PdfParserTemplateName.DEBIT_CREDIT_DIRECTION: _build_debit_credit_direction_template,
    PdfParserTemplateName.MALFORMED_RAW_DATA: _build_malformed_raw_data_template,
}

_ALL_TEMPLATE_NAMES: tuple[str, ...] = tuple(sorted(_TEMPLATE_BUILDERS.keys()))


# ---------------------------------------------------------------------------
# Public helpers -- build template payloads
# ---------------------------------------------------------------------------


def build_pdf_parser_template_payloads() -> "dict[str, PdfParserStatementPayload]":
    """Build all parser template payloads in a deterministic dict.

    Returns a dict mapping stable template name strings to
    ``PdfParserStatementPayload`` instances. The dict keys are
    sorted alphabetically for determinism.

    Calling this function twice returns payloads with the same
    structure, row count, and field values. The payloads are
    never mutated.
    """
    return {name: build_pdf_parser_template_payload(name) for name in _ALL_TEMPLATE_NAMES}


def build_pdf_parser_template_payload(
    name: str,
) -> PdfParserStatementPayload:
    """Build a single parser template payload by name.

    Parameters
    ----------
    name:
        One of the ``PdfParserTemplateName`` constants:
        ``"accepted_only"``, ``"blocked_mixed"``, ``"multi_page"``,
        ``"debit_credit_direction"``, or ``"malformed_raw_data"``.

    Returns
    -------
    PdfParserStatementPayload
        The template payload with stable rows in document order.

    Raises
    ------
    ValueError
        When ``name`` is not a recognised template name.
    """
    builder = _TEMPLATE_BUILDERS.get(name)
    if builder is None:
        available = ", ".join(_ALL_TEMPLATE_NAMES)
        raise ValueError(f"Unknown template name: {name!r}. Available: {available}")
    payload = builder()
    if payload.source_content_hash is not None:
        return payload
    fixture_hash = hashlib.sha256(f"synthetic-pdf-template:{name}".encode()).hexdigest()
    return replace(
        payload,
        source_content_hash=fixture_hash,
        source_filename=f"{name}.synthetic.pdf",
        template_name=name,
    )


# ---------------------------------------------------------------------------
# Public helpers -- run fixture on a template
# ---------------------------------------------------------------------------


def run_pdf_parser_template_fixture(
    name: str,
) -> PdfParserTemplateFixtureResult:
    """Run the full-chain fixture on a parser template.

    Pipeline:
    1. Build the template payload.
    2. Run the full-chain smoke test (adapter -> batch normalizer ->
       review fixture -> dashboard + audit payloads).
    3. Export dashboard-safe and audit payloads.

    Parameters
    ----------
    name:
        One of the ``PdfParserTemplateName`` constants.

    Returns
    -------
    PdfParserTemplateFixtureResult
        A frozen result carrying the template name, source statement ID,
        smoke test result, dashboard payload, and audit payload.

    Raises
    ------
    ValueError
        When ``name`` is not a recognised template name.
    """
    payload = build_pdf_parser_template_payload(name)
    smoke = run_smoke_test(payload)
    dashboard = export_pdf_parser_template_dashboard_payload(name)
    audit = export_pdf_parser_template_audit_payload(name)
    return PdfParserTemplateFixtureResult(
        template_name=name,
        source_statement_id=payload.source_statement_id,
        smoke_result=smoke,
        dashboard_payload=dashboard,
        audit_payload=audit,
    )


# ---------------------------------------------------------------------------
# Public helpers -- export payloads
# ---------------------------------------------------------------------------


def export_pdf_parser_template_dashboard_payload(
    name: str,
) -> dict[str, Any]:
    """Export a dashboard-safe payload for a parser template.

    Runs the full adapter -> batch normalizer -> review fixture chain
    and returns only the dashboard-safe payload (no ``attachment_path``,
    ``raw_row_text``, or ``raw_row_payload``).

    Parameters
    ----------
    name:
        One of the ``PdfParserTemplateName`` constants.

    Returns
    -------
    dict[str, Any]
        Dashboard-safe payload suitable for JSON serialization.

    Raises
    ------
    ValueError
        When ``name`` is not a recognised template name.
    """
    payload = build_pdf_parser_template_payload(name)
    adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
    batch_result = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
    review_fixture = build_pdf_statement_import_review_fixture(batch_result)
    return export_pdf_statement_import_review_dashboard_payload(review_fixture)


def export_pdf_parser_template_audit_payload(
    name: str,
) -> dict[str, Any]:
    """Export an audit payload for a parser template with source evidence.

    Runs the full adapter -> batch normalizer chain and returns the
    audit payload that includes ``attachment_path``, ``raw_row_text``,
    and ``raw_row_payload`` for traceability.

    Parameters
    ----------
    name:
        One of the ``PdfParserTemplateName`` constants.

    Returns
    -------
    dict[str, Any]
        Audit payload with source evidence, suitable for JSON serialization.

    Raises
    ------
    ValueError
        When ``name`` is not a recognised template name.
    """
    payload = build_pdf_parser_template_payload(name)
    adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
    batch_result = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
    return export_pdf_statement_import_review_audit_payload(batch_result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfParserTemplateName",
    "PdfParserTemplateFixtureResult",
    "build_pdf_parser_template_payloads",
    "build_pdf_parser_template_payload",
    "run_pdf_parser_template_fixture",
    "export_pdf_parser_template_dashboard_payload",
    "export_pdf_parser_template_audit_payload",
]
