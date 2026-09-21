from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable

from finance_core.intake.raw_text_repository import (
    TELEGRAM_TEXT,
    create_raw_intake_record,
    get_parser_proposal,
    get_raw_intake_record,
    save_parser_proposal,
)
from finance_core.parsers.text_expense_parser import parse_text_expense


def process_raw_text_input(
    conn: sqlite3.Connection,
    raw_input: str,
    *,
    source_type: str = TELEGRAM_TEXT,
    source_channel: str | None = None,
    source_metadata: Mapping[str, Any] | None = None,
    received_at: datetime | str | None = None,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Persist raw text intake and parser proposal without finalizing records."""
    with conn:
        intake_record = create_raw_intake_record(
            conn,
            raw_input,
            source_type=source_type,
            source_channel=source_channel,
            source_metadata=source_metadata,
            received_at=received_at,
        )
        parser_output = get_parser_proposal(conn, intake_record_id=intake_record["id"])
        if parser_output is None:
            proposal = parse_text_expense(
                raw_input,
                raw_input_reference=intake_record["public_id"],
                source_type=source_type,
            )
            parser_output = save_parser_proposal(
                conn,
                intake_record["id"],
                proposal,
            )
        updated_intake_record = get_raw_intake_record(conn, intake_record["id"])
        assert updated_intake_record is not None
        if persistence_effect is not None:
            persistence_effect(conn, updated_intake_record)

    return {
        "intake": updated_intake_record,
        "parser_output": parser_output,
        "proposal": parser_output["proposal"] if parser_output else None,
    }
