"""Transaction-neutral SQL repositories for parser confirmation/conversion."""

from __future__ import annotations

import sqlite3
from typing import Any


def row_to_dict(row: sqlite3.Row | tuple[Any, ...], description: tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((column[0] for column in description), row, strict=True))


class ParserProposalRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, parser_output_id: int) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            """
            SELECT id, public_id, source_type, source_public_id, statement_batch_id,
                   attachment_id, parser_name, parser_version, raw_text, parsed_payload,
                   normalized_payload, confidence_score, parse_status
            FROM parser_outputs WHERE id = ?
            """,
            (parser_output_id,),
        )
        row = cursor.fetchone()
        return None if row is None else row_to_dict(row, cursor.description)

    def get_by_public_id(self, public_id: str) -> dict[str, Any] | None:
        """Read-only lookup of one proposal row by its public identity."""
        cursor = self._conn.execute(
            """
            SELECT id, public_id, source_type, source_public_id, statement_batch_id,
                   attachment_id, parser_name, parser_version, raw_text, parsed_payload,
                   normalized_payload, confidence_score, parse_status
            FROM parser_outputs WHERE public_id = ?
            """,
            (public_id,),
        )
        row = cursor.fetchone()
        return None if row is None else row_to_dict(row, cursor.description)

    def update_status(self, parser_output_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE parser_outputs SET parse_status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, parser_output_id),
        )

    def update_raw_intake_status(self, parser_output_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE raw_intake_records SET status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE parser_output_id = ?",
            (status, parser_output_id),
        )

    def insert_event(
        self,
        parser_output_id: int,
        from_status: str,
        to_status: str,
        reason: str | None,
        actor_id: str,
        payload: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO parser_proposal_events (
              parser_output_id, from_status, to_status, event_type, event_reason,
              actor_type, actor_identifier, event_payload
            ) VALUES (?, ?, ?, ?, ?, 'user', ?, ?)
            """,
            (parser_output_id, from_status, to_status, to_status, reason, actor_id, payload),
        )
        return _last_insert_id(cursor)

    def insert_legacy_confirmation(
        self, parser_output_id: int, decision: str, actor_id: str, reason: str | None, payload: str
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO parser_proposal_confirmations (
              parser_output_id, decision, decided_by, decision_reason, decision_payload
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (parser_output_id, decision, actor_id, reason, payload),
        )
        return _last_insert_id(cursor)


class ParserAuthorizationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_for_proposal(self, parser_output_id: int) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?",
            (parser_output_id,),
        )
        row = cursor.fetchone()
        return None if row is None else row_to_dict(row, cursor.description)

    def insert(
        self,
        *,
        confirmation_public_id: str,
        parser_output_id: int,
        content_hash: str,
        actor_id: str,
        state: str,
        channel: str,
        decided_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO parser_proposal_authorizations (
              confirmation_public_id, parser_output_id, proposal_content_hash, actor_type,
              authenticated_actor_id, confirmation_state, confirmation_channel, decided_at
            ) VALUES (?, ?, ?, 'human', ?, ?, ?, ?)
            """,
            (
                confirmation_public_id,
                parser_output_id,
                content_hash,
                actor_id,
                state,
                channel,
                decided_at,
            ),
        )


class CanonicalTransactionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, values: dict[str, Any]) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO transactions (
              public_id, intent, intent_type, source_channel, transaction_date, status,
              review_status, amount, total_amount, currency, merchant, category, notes,
              raw_input, statement_batch_id, parser_output_id, confidence_score
            ) VALUES (?, ?, 'Generated', ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["public_id"],
                values["intent"],
                values["source_channel"],
                values["transaction_date"],
                values["review_status"],
                values["amount"],
                values["amount"],
                values["currency"],
                values["merchant"],
                values["category"],
                values["notes"],
                values["raw_input"],
                values["statement_batch_id"],
                values["parser_output_id"],
                values["confidence_score"],
            ),
        )
        return _last_insert_id(cursor)


class ParserConversionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_for_proposal(self, parser_output_id: int) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            """
            SELECT audit.*, transactions.public_id AS transaction_public_id
            FROM parser_proposal_conversion_audit AS audit
            JOIN transactions ON transactions.id = audit.transaction_id
            WHERE audit.parser_output_id = ?
            """,
            (parser_output_id,),
        )
        row = cursor.fetchone()
        return None if row is None else row_to_dict(row, cursor.description)

    def insert(
        self,
        *,
        parser_output_id: int,
        transaction_id: int,
        confirmation_public_id: str,
        content_hash: str,
        actor_id: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO parser_proposal_conversion_audit (
              parser_output_id, transaction_id, confirmation_public_id, proposal_content_hash,
              authenticated_actor_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (parser_output_id, transaction_id, confirmation_public_id, content_hash, actor_id),
        )


def _last_insert_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return int(cursor.lastrowid)
