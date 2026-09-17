"""Compatibility entry points for the parser conversion unit of work."""

from __future__ import annotations

import sqlite3
from typing import Any

from finance_core.parser_proposals.service import (
    CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX,
    AlreadyConvertedProposalError,
    InvalidProposalStatusError,
    MissingConfirmationRecordError,
    MissingRequiredTransactionFieldError,
    ProposalConversionError,
    StaleProposalConfirmationError,
    UnsupportedProposalTypeError,
    convert_confirmed_parser_proposal,
)


def convert_confirmed_proposal_to_transaction(
    conn: sqlite3.Connection, parser_output_id: int
) -> dict[str, Any]:
    """Convert through the service-owned canonical transaction boundary."""
    return convert_confirmed_parser_proposal(conn, parser_output_id)


def validate_confirmed_proposal_for_transaction(
    conn: sqlite3.Connection, parser_output_id: int
) -> dict[str, Any]:
    """Retained only as a compatibility guard; validation is authoritative in conversion."""
    # This function must not enable a caller to authorize conversion.  The
    # public converter always re-loads state inside its own transaction.
    from finance_core.parser_proposals.content_hash import compute_proposal_content_hash
    from finance_core.parser_proposals.repository import ParserProposalRepository
    from finance_core.parser_proposals.service import (
        _require_active_authorization,
        _transaction_fields,
    )

    proposal = ParserProposalRepository(conn).get(parser_output_id)
    if proposal is None:
        raise ProposalConversionError(f"parser output not found: {parser_output_id}")
    if proposal["parse_status"] != "confirmed":
        raise InvalidProposalStatusError(
            f"Only confirmed parser proposals can be converted: {proposal['parse_status']}"
        )
    from finance_core.parser_proposals.repository import ParserAuthorizationRepository

    _require_active_authorization(
        ParserAuthorizationRepository(conn).get_for_proposal(parser_output_id),
        parser_output_id,
        compute_proposal_content_hash(conn, proposal),
    )
    return {"parser_output": proposal, "transaction_fields": _transaction_fields(conn, proposal)}


__all__ = [
    "CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX",
    "AlreadyConvertedProposalError",
    "InvalidProposalStatusError",
    "MissingConfirmationRecordError",
    "MissingRequiredTransactionFieldError",
    "ProposalConversionError",
    "StaleProposalConfirmationError",
    "UnsupportedProposalTypeError",
    "convert_confirmed_proposal_to_transaction",
    "validate_confirmed_proposal_for_transaction",
]
