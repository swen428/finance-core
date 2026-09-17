"""Statement import contracts shared by adapters and import runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, TypeAlias, TypedDict

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.statement_identity import require_sha256

StatementImportRow: TypeAlias = dict[str, Any]


class StatementSourceIdentity(TypedDict):
    """Source identity fields used for deterministic statement row IDs."""

    source_type: str | None
    source_file_hash: str | None
    source_file_path: str | None
    source_filename: str | None
    batch_public_id: str | None
    import_contract_version: str
    account_id: str | None
    account_name: str | None
    statement_period_start: str | None
    statement_period_end: str | None


@dataclass(frozen=True)
class StructuredStatementRow:
    """A single structured row input for the statement import runtime.

    All monetary fields use ``Decimal``. Optional fields default to ``None``.
    This is the canonical contract between adapters (CSV, future PDF/OCR)
    and the import runtime.
    """

    merchant_raw: str
    amount: Decimal
    currency: str
    transaction_date: date | None = None
    posted_date: date | None = None
    merchant_normalized: str | None = None
    account_name: str | None = None
    account_id: str | None = None
    statement_row_reference: str | None = None
    raw_row_payload: dict[str, Any] | None = None
    row_fingerprint: str | None = None
    row_fingerprint_version: str | None = None
    external_row_fingerprint: str | None = None
    external_row_fingerprint_version: str | None = None
    fingerprint_source_content_hash: str | None = None
    amount_direction: StatementAmountDirection | None = None
    raw_amount_type: str | None = None
    raw_amount: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Statement amount must be Decimal")
        if not self.amount.is_finite():
            raise ValueError("Statement amount must be finite")
        if self.amount < 0:
            raise ValueError(f"Amount must be non-negative, got {self.amount}")
        if self.row_fingerprint is not None:
            require_sha256(self.row_fingerprint, field="row_fingerprint")
        if self.row_fingerprint_version is not None and self.row_fingerprint is None:
            raise ValueError("row_fingerprint_version requires row_fingerprint")
        if self.row_fingerprint_version is not None and not self.row_fingerprint_version.strip():
            raise ValueError("row_fingerprint_version must not be empty")
        if self.external_row_fingerprint is not None:
            require_sha256(
                self.external_row_fingerprint,
                field="external_row_fingerprint",
            )
        if (self.external_row_fingerprint is None) != (
            self.external_row_fingerprint_version is None
        ):
            raise ValueError(
                "external_row_fingerprint and external_row_fingerprint_version "
                "must be supplied together"
            )
        if (
            self.external_row_fingerprint_version is not None
            and not self.external_row_fingerprint_version.strip()
        ):
            raise ValueError("external_row_fingerprint_version must not be empty")
        if self.fingerprint_source_content_hash is not None:
            require_sha256(
                self.fingerprint_source_content_hash,
                field="fingerprint_source_content_hash",
            )
        if self.amount_direction is not None and not isinstance(
            self.amount_direction,
            StatementAmountDirection,
        ):
            raise ValueError("amount_direction must be a StatementAmountDirection")


@dataclass(frozen=True)
class StatementImportBatch:
    """Immutable summary of a completed statement import batch."""

    batch_id: int
    public_id: str
    source_type: str
    row_count: int
    inserted_ids: list[int] = field(default_factory=list)
    owned_row_ids: list[int] = field(default_factory=list)
    skipped_duplicates: int = 0
    idempotent_count: int = 0
    source_content_hash: str | None = None
    row_set_fingerprint: str | None = None
    import_command_hash: str | None = None


__all__ = [
    "StatementImportBatch",
    "StatementImportRow",
    "StatementSourceIdentity",
    "StructuredStatementRow",
]
