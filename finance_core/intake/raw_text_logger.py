from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from finance_core.parsers.text_expense_parser import parse_text_expense

PENDING_PARSE = "pending_parse"
PARSED_PENDING_CONFIRMATION = "parsed_pending_confirmation"
TELEGRAM_TEXT = "telegram_text"


def prepare_raw_text_intake(
    raw_input: str,
    *,
    received_at: datetime | None = None,
    source_type: str = TELEGRAM_TEXT,
    intake_id: str | None = None,
) -> dict[str, Any]:
    """Prepare a raw text intake record without mutating the original text."""
    timestamp = received_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    return {
        "intake_id": intake_id or str(uuid4()),
        "source_type": source_type,
        "received_at": timestamp.isoformat(),
        "status": PENDING_PARSE,
        "raw_input": raw_input,
    }


def log_raw_text_input(
    raw_input: str,
    *,
    received_at: datetime | None = None,
    source_type: str = TELEGRAM_TEXT,
    intake_id: str | None = None,
) -> dict[str, Any]:
    """Prepare raw intake data and a parser proposal for local validation."""
    intake_record = prepare_raw_text_intake(
        raw_input,
        received_at=received_at,
        source_type=source_type,
        intake_id=intake_id,
    )
    proposal = parse_text_expense(
        raw_input,
        raw_input_reference=intake_record["intake_id"],
        source_type=source_type,
    )
    intake_record["status"] = PARSED_PENDING_CONFIRMATION

    return {
        "intake": intake_record,
        "proposal": proposal,
    }
