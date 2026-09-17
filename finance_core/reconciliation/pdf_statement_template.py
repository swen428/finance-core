"""PDF Statement Template Model v1.

Dataclass-based template model that defines how to parse PDF bank/credit-card
statements.  A template describes the expected layout, column mapping, date
format, amount sign conventions, and currency assumptions for a specific
financial institution's statement format.

These templates are read-only configuration objects -- they carry no parser
runtime logic, no file I/O, and no database access.  The actual parsing is
handled by the CLI layer which reads a template and applies its rules to
extracted text.

Template Registry
-----------------
The module maintains a lazy-loaded registry of built-in templates.  Callers
look up a template by ``template_id`` via ``get_template()``.  Unknown
template IDs raise ``ValueError``.

Non-goals:
- No raw PDF parsing (handled by the extraction layer).
- No OCR.
- No database connection or SQL execution.
- No file I/O.
- No mutation of final financial records.
- No settlement obligation generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from finance_core.reconciliation.pdf_statement_evidence import (
    PDF_EVIDENCE_CONTRACT_VERSION,
    PDF_TEMPLATE_PARSER_NAME,
    PDF_TEMPLATE_PARSER_VERSION,
    PDF_TEXT_EXTRACTION_VERSION,
    PdfAmountSignConvention,
)

# ---------------------------------------------------------------------------
# Template model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementTemplate:
    """Configuration template for parsing a bank/credit-card PDF statement.

    Describes the expected layout, column mapping, date format, amount sign
    conventions, and currency assumptions for a specific institution.

    Fields
    ------
    template_id:
        Stable unique identifier for this template (e.g. "sample_bank_v1").
    institution_name:
        Human-readable name of the financial institution.
    statement_type:
        Type of statement: ``"bank"``, ``"credit_card"``, ``"investment"``, etc.
    account_label:
        Account or card label this template applies to.
    currency:
        Default currency for amounts when not explicitly specified per row
        (e.g. ``"MYR"``, ``"SGD"``).  Upper-case ISO-3 code.
    date_format:
        Expected date format string such as ``"%d/%m/%Y"``.
    date_field_names:
        Column/field names that map to transaction dates (in priority order).
    posted_date_field_names:
        Column/field names that map to posted dates (in priority order).
    description_field_names:
        Column/field names that map to transaction descriptions.
    amount_field_names:
        Column/field names that map to amounts.
    debit_credit_field_names:
        Column/field names that map to debit/credit indicators.
        Rows without an explicit supported value remain ``UNKNOWN``.
    amount_sign_rules:
        How raw amount signs should be interpreted.  Options:

        - ``"standard"``: negative sign means credit/outflow, positive means debit.
        - ``"inverted"``: negative means debit/cash received (credit-card view).
        - ``"explicit_only"``: rely only on an explicit debit/credit column.
    column_delimiter:
        Expected column delimiter in extracted text (e.g. ``None`` for
        whitespace-based splitting, ``","`` for CSV-like, ``"|"`` for
        pipe-delimited).
    column_names:
        Ordered, unique names for delimiter-separated fields. Required when
        ``direction_field_name`` is used.
    direction_field_index:
        Exact zero-based delimiter field containing direction evidence.
    direction_field_offset_from_amount:
        Non-zero delimiter-field offset from the selected amount token.
    direction_field_offset_from_end:
        Positive 1-based offset from the end of the complete row. This is also
        available for whitespace-split templates.
    direction_field_name:
        Exact named delimiter field containing direction evidence.

        At most one direction-field contract may be set. The parser never
        scans arbitrary fields after the amount.
    skip_header_lines:
        Number of leading lines to skip before parsing rows.
    skip_footer_lines:
        Number of trailing lines to skip after parsing rows.
    notes:
        Free-form notes about template limitations, tested statement formats,
        or known edge cases.
    """

    template_id: str
    institution_name: str
    statement_type: str = "bank"
    account_label: str = ""
    currency: str = "MYR"
    date_format: str = "%d/%m/%Y"
    date_field_names: tuple[str, ...] = ("transaction_date", "date", "txn_date")
    posted_date_field_names: tuple[str, ...] = ("posted_date", "posting_date")
    description_field_names: tuple[str, ...] = ("description", "merchant", "details")
    amount_field_names: tuple[str, ...] = ("amount", "txn_amount", "value")
    debit_credit_field_names: tuple[str, ...] = ("debit_credit", "dr_cr", "type")
    amount_sign_rules: str = "explicit_only"
    column_delimiter: str | None = None
    column_names: tuple[str, ...] = ()
    skip_header_lines: int = 0
    skip_footer_lines: int = 0
    notes: str = ""
    template_version: str = "pdf-template-v2"
    parser_name: str = PDF_TEMPLATE_PARSER_NAME
    parser_version: str = PDF_TEMPLATE_PARSER_VERSION
    extraction_version: str = PDF_TEXT_EXTRACTION_VERSION
    evidence_contract_version: str = PDF_EVIDENCE_CONTRACT_VERSION
    amount_sign_convention: PdfAmountSignConvention = PdfAmountSignConvention.UNSIGNED_EXPLICIT
    direction_field_index: int | None = None
    direction_field_offset_from_amount: int | None = None
    direction_field_offset_from_end: int | None = None
    direction_field_name: str | None = None

    # -- Computed defaults --
    date_field_names_lower: tuple[str, ...] = field(init=False, repr=False)
    posted_date_field_names_lower: tuple[str, ...] = field(init=False, repr=False)
    description_field_names_lower: tuple[str, ...] = field(init=False, repr=False)
    amount_field_names_lower: tuple[str, ...] = field(init=False, repr=False)
    debit_credit_field_names_lower: tuple[str, ...] = field(init=False, repr=False)
    column_names_lower: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Lower-case field name tuples for case-insensitive matching."""

        if self.amount_sign_rules != "explicit_only":
            raise ValueError("PDF templates must use explicit_only direction rules")
        configured_direction_fields = sum(
            value is not None
            for value in (
                self.direction_field_index,
                self.direction_field_offset_from_amount,
                self.direction_field_offset_from_end,
                self.direction_field_name,
            )
        )
        if configured_direction_fields > 1:
            raise ValueError("PDF templates may declare only one positional direction field")
        if self.direction_field_index is not None and (
            isinstance(self.direction_field_index, bool)
            or not isinstance(self.direction_field_index, int)
            or self.direction_field_index < 0
        ):
            raise ValueError("direction_field_index must be a zero-based non-negative integer")
        if self.direction_field_offset_from_amount is not None and (
            isinstance(self.direction_field_offset_from_amount, bool)
            or not isinstance(self.direction_field_offset_from_amount, int)
            or self.direction_field_offset_from_amount == 0
        ):
            raise ValueError("direction_field_offset_from_amount must be a non-zero integer")
        if self.direction_field_offset_from_end is not None and (
            isinstance(self.direction_field_offset_from_end, bool)
            or not isinstance(self.direction_field_offset_from_end, int)
            or self.direction_field_offset_from_end < 1
        ):
            raise ValueError("direction_field_offset_from_end must be a positive integer")
        if self.direction_field_name is not None and not self.direction_field_name.strip():
            raise ValueError("direction_field_name must not be empty")
        delimiter_required = any(
            value is not None
            for value in (
                self.direction_field_index,
                self.direction_field_offset_from_amount,
                self.direction_field_name,
            )
        )
        if delimiter_required and not self.column_delimiter:
            raise ValueError("indexed PDF direction fields require a declared column_delimiter")
        if self.column_names and not self.column_delimiter:
            raise ValueError("column_names require a declared column_delimiter")
        normalized_column_names = tuple(name.strip().lower() for name in self.column_names)
        if any(not name for name in normalized_column_names):
            raise ValueError("column_names must not contain empty values")
        if len(set(normalized_column_names)) != len(normalized_column_names):
            raise ValueError("column_names must be unique")
        if (
            self.direction_field_name is not None
            and self.direction_field_name.strip().lower() not in normalized_column_names
        ):
            raise ValueError("direction_field_name must name a declared column")
        for field_name in (
            "template_id",
            "template_version",
            "parser_name",
            "parser_version",
            "extraction_version",
            "evidence_contract_version",
            "date_format",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must not be empty")

        def _lower(t: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(s.lower() for s in t)

        object.__setattr__(self, "date_field_names_lower", _lower(self.date_field_names))
        object.__setattr__(
            self, "posted_date_field_names_lower", _lower(self.posted_date_field_names)
        )
        object.__setattr__(
            self, "description_field_names_lower", _lower(self.description_field_names)
        )
        object.__setattr__(self, "amount_field_names_lower", _lower(self.amount_field_names))
        object.__setattr__(
            self, "debit_credit_field_names_lower", _lower(self.debit_credit_field_names)
        )
        object.__setattr__(self, "column_names_lower", normalized_column_names)


# ---------------------------------------------------------------------------
# Template registry (lazy-loaded, read-only)
# ---------------------------------------------------------------------------

_TEMPLATE_REGISTRY: dict[str, PdfStatementTemplate] = {}


def _build_builtin_templates() -> dict[str, PdfStatementTemplate]:
    """Build the set of built-in templates."""
    return {
        "sample_bank_v1": PdfStatementTemplate(
            template_id="sample_bank_v1",
            institution_name="Sample Bank",
            statement_type="bank",
            account_label="Current Account",
            currency="MYR",
            date_format="%d/%m/%Y",
            date_field_names=("date", "transaction_date"),
            posted_date_field_names=("posted_date", "posting_date"),
            description_field_names=("description", "merchant"),
            amount_field_names=("amount",),
            debit_credit_field_names=("debit_credit", "dr_cr"),
            amount_sign_rules="explicit_only",
            column_delimiter=None,
            direction_field_offset_from_end=1,
            skip_header_lines=0,
            skip_footer_lines=0,
            notes="Generic bank statement template.  Assumes whitespace-delimited columns "
            "with date, description, and amount fields in that order.  "
            "Debit rows positive, credit rows negative or marked with explicit C/CR indicator.",
        ),
        "sample_credit_card_v1": PdfStatementTemplate(
            template_id="sample_credit_card_v1",
            institution_name="Sample Credit Card",
            statement_type="credit_card",
            account_label="Visa Platinum",
            currency="MYR",
            date_format="%d/%m/%Y",
            date_field_names=("transaction_date", "date"),
            posted_date_field_names=("posted_date", "posting_date"),
            description_field_names=("description", "merchant"),
            amount_field_names=("amount",),
            debit_credit_field_names=("debit_credit",),
            amount_sign_rules="explicit_only",
            column_delimiter=None,
            direction_field_offset_from_end=1,
            skip_header_lines=0,
            skip_footer_lines=0,
            notes="Generic credit card statement template.  Charges are typically debits; "
            "refunds and payments are marked with explicit direction or negative signs.",
        ),
    }


def _ensure_registry() -> None:
    """Lazy-init the template registry on first access."""
    global _TEMPLATE_REGISTRY
    if not _TEMPLATE_REGISTRY:
        _TEMPLATE_REGISTRY = _build_builtin_templates()


def get_template(template_id: str) -> PdfStatementTemplate:
    """Return a built-in template by ID.  Raises ``ValueError`` if unknown.

    Args:
        template_id: The template identifier, e.g. ``"sample_bank_v1"``.

    Returns:
        The matching ``PdfStatementTemplate``.

    Raises:
        ValueError: If ``template_id`` is not a known built-in template.
    """
    _ensure_registry()
    template = _TEMPLATE_REGISTRY.get(template_id)
    if template is None:
        known = ", ".join(sorted(_TEMPLATE_REGISTRY.keys()))
        raise ValueError(f"Unknown template '{template_id}'. Known templates: {known}")
    return template


def list_templates() -> list[str]:
    """Return a sorted list of all known built-in template IDs."""
    _ensure_registry()
    return sorted(_TEMPLATE_REGISTRY.keys())


def register_template(template: PdfStatementTemplate) -> None:
    """Register a custom template (programmatic use only, for testing).

    Args:
        template: A ``PdfStatementTemplate`` to register.
    """
    _ensure_registry()
    _TEMPLATE_REGISTRY[template.template_id] = template


def _reset_registry() -> None:
    """Reset the registry to defaults (used by tests only)."""
    global _TEMPLATE_REGISTRY
    _TEMPLATE_REGISTRY = {}
