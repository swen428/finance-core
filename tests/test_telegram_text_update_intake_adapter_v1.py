"""Tests for the Telegram text-message update intake adapter.

Covers validation, mapping, persistence, idempotency, content
conflicts, and negative cases.  Uses temporary databases only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from finance_core.intake.raw_text_repository import (
    RawIntakeIdempotencyConflictError,
)
from finance_core.intake.telegram_text_adapter import (
    TelegramTextUpdate,
    TelegramTextUpdateValidationError,
    process_telegram_text_update,
    validate_telegram_text_update,
)

# ---------------------------------------------------------------------------
#  Shared helpers
# ---------------------------------------------------------------------------


def _standard_payload(
    *,
    update_id: int = 123456,
    message_id: int = 789,
    chat_id: int = -456789123,
    date: int = 1717171200,  # 2024-05-31T16:00:00 UTC
    text: str = "Lunch SGD 12.50 at ExampleCafe paid by Owner",
    sender_id: int | None = 987654321,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": date,
        "chat": {"id": chat_id, "type": "group"},
        "text": text,
    }
    if sender_id is not None:
        message["from"] = {"id": sender_id, "is_bot": False, "first_name": "Owner"}
    return {"update_id": update_id, "message": message}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


# ---------------------------------------------------------------------------
#  Unit: validate_telegram_text_update
# ---------------------------------------------------------------------------


class TestValidUpdateMapping:
    def test_standard_text_message(self):
        update = validate_telegram_text_update(_standard_payload())
        assert update.update_id == 123456
        assert update.message_id == 789
        assert update.chat_id == -456789123
        assert update.date == 1717171200
        assert update.text == "Lunch SGD 12.50 at ExampleCafe paid by Owner"
        assert update.sender_id == 987654321

    def test_negative_group_chat_id(self):
        update = validate_telegram_text_update(_standard_payload(chat_id=-1001234567890))
        assert update.chat_id == -1001234567890

    def test_sender_id_present(self):
        update = validate_telegram_text_update(_standard_payload(sender_id=42))
        assert update.sender_id == 42

    def test_sender_id_absent(self):
        payload = _standard_payload()
        del payload["message"]["from"]
        update = validate_telegram_text_update(payload)
        assert update.sender_id is None

    def test_unicode_preservation(self):
        text = "Cafe \u65e5\u672c\u8a9e \u2615 1,500\u00a5 \u8f6c\u8d26\u8bb0\u5f55"
        update = validate_telegram_text_update(_standard_payload(text=text))
        assert update.text == text

    def test_leading_trailing_whitespace_preservation(self):
        text = "   Lunch SGD 12.50   "
        update = validate_telegram_text_update(_standard_payload(text=text))
        assert update.text == text

    def test_multiline_preservation(self):
        text = "Lunch\nSGD 12.50\nat Example Cafe"
        update = validate_telegram_text_update(_standard_payload(text=text))
        assert update.text == text

    def test_command_text_preservation(self):
        text = "/add Lunch SGD 12.50 at Example Cafe"
        update = validate_telegram_text_update(_standard_payload(text=text))
        assert update.text == text

    def test_positive_chat_id(self):
        update = validate_telegram_text_update(_standard_payload(chat_id=12345678))
        assert update.chat_id == 12345678

    def test_update_id_zero(self):
        update = validate_telegram_text_update(_standard_payload(update_id=0))
        assert update.update_id == 0


class TestDateConversion:
    def test_utc_conversion(self):
        update = validate_telegram_text_update(_standard_payload(date=1717171200))
        expected = datetime.fromtimestamp(1717171200, tz=UTC).isoformat()
        assert update.source_received_at == expected


class TestImmutability:
    def test_caller_payload_unchanged(self):
        payload = _standard_payload()
        original = json.dumps(payload, sort_keys=True)
        validate_telegram_text_update(payload)
        assert json.dumps(payload, sort_keys=True) == original

    def test_validated_dto_is_immutable(self):
        update = validate_telegram_text_update(_standard_payload())
        with pytest.raises(Exception):
            update.update_id = 999  # type: ignore[misc]

    def test_source_metadata_is_defensive_copy(self):
        update = validate_telegram_text_update(_standard_payload())
        metadata = update.source_metadata
        assert isinstance(metadata, dict)
        metadata["extra"] = "should-not-persist"
        assert "extra" not in update.source_metadata


# ---------------------------------------------------------------------------
#  Unit: Invalid inputs
# ---------------------------------------------------------------------------


class TestInvalidInputs:
    @pytest.mark.parametrize(
        "payload,expected_reason",
        [
            pytest.param([], "PAYLOAD_NOT_MAPPING", id="list_payload"),
            pytest.param("text", "PAYLOAD_NOT_MAPPING", id="string_payload"),
            pytest.param(42, "PAYLOAD_NOT_MAPPING", id="int_payload"),
        ],
    )
    def test_payload_type_rejection(self, payload, expected_reason):
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == expected_reason

    @pytest.mark.parametrize(
        "update_key",
        [
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "callback_query",
            "inline_query",
            "poll",
        ],
    )
    def test_unsupported_update_types(self, update_key):
        payload = {update_key: {}}
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "UNSUPPORTED_UPDATE_TYPE"

    def test_missing_message(self):
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update({"update_id": 1})
        assert exc.value.reason_code == "MISSING_MESSAGE"

    def test_message_not_mapping(self):
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update({"update_id": 1, "message": "nope"})
        assert exc.value.reason_code == "MESSAGE_NOT_MAPPING"

    def test_missing_update_id(self):
        payload = {
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "hi",
            }
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_UPDATE_ID"

    def test_boolean_update_id(self):
        payload = {
            "update_id": True,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "UPDATE_ID_BOOL"

    def test_missing_message_id(self):
        payload = {
            "update_id": 1,
            "message": {"date": 1717171200, "chat": {"id": 1}, "text": "hi"},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_MESSAGE_ID"

    def test_boolean_message_id(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": False,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MESSAGE_ID_BOOL"

    def test_non_positive_message_id(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 0,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MESSAGE_ID_NOT_POSITIVE"

    def test_missing_chat(self):
        payload = {
            "update_id": 1,
            "message": {"message_id": 1, "date": 1717171200, "text": "hi"},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_CHAT"

    def test_chat_not_mapping(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": "bad",
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "CHAT_NOT_MAPPING"

    def test_missing_chat_id(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_ID"

    def test_boolean_chat_id(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": True},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "ID_BOOL"

    def test_missing_message_date(self):
        payload = {
            "update_id": 1,
            "message": {"message_id": 1, "chat": {"id": 1}, "text": "hi"},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_MESSAGE_DATE"

    def test_boolean_message_date(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": False,
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MESSAGE_DATE_BOOL"

    def test_invalid_date_type(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": "1717171200",
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MESSAGE_DATE_TYPE"

    def test_negative_date(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": -1,
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MESSAGE_DATE_NEGATIVE"

    def test_out_of_range_date(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 1},
                "text": "hi",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MESSAGE_DATE_OUT_OF_RANGE"

    def test_missing_text(self):
        payload = {
            "update_id": 1,
            "message": {"message_id": 1, "date": 1717171200, "chat": {"id": 1}},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_TEXT"

    def test_non_string_text(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": 123,
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "TEXT_NOT_STRING"

    def test_empty_text(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "TEXT_EMPTY_OR_WHITESPACE"

    def test_whitespace_only_text(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "   \n\t   ",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "TEXT_EMPTY_OR_WHITESPACE"

    def test_photo_only_message(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "photo": [{"file_id": "abc"}],
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MEDIA_WITHOUT_TEXT"

    def test_document_only_message(self):
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "document": {"file_id": "abc"},
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MEDIA_WITHOUT_TEXT"


class TestValidationCreatesNoDbRows:
    def test_invalid_update_creates_no_rows(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        before = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            validate_telegram_text_update({"update_id": True, "message": {}})
        after = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        assert after == before

    def test_unsupported_update_type_creates_no_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        before = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            validate_telegram_text_update({"callback_query": {"id": "1"}})
        after = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        assert after == before


# ---------------------------------------------------------------------------
#  Integration: process_telegram_text_update (one-step public entry)
# ---------------------------------------------------------------------------


class TestPersistenceIntegration:
    def test_valid_update_creates_intake_record(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        assert result["intake"]["source_type"] == "telegram_text"
        assert result["intake"]["source_channel"] == "telegram"
        assert result["intake"]["raw_input"] == _standard_payload()["message"]["text"]
        assert _count(conn, "raw_intake_records") == 1

    def test_creates_one_evidence_row(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        assert _count(conn, "raw_intake_evidence") == 1
        evidence = conn.execute(
            "SELECT * FROM raw_intake_evidence WHERE raw_intake_record_id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert evidence is not None
        assert evidence["evidence_type"] == "raw_input"

    def test_creates_one_parser_output(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        assert result["parser_output"] is not None
        assert result["proposal"] is not None
        assert _count(conn, "parser_outputs") == 1

    def test_source_type_is_telegram_text(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        row = conn.execute(
            "SELECT source_type FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["source_type"] == "telegram_text"

    def test_source_channel_is_telegram(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        row = conn.execute(
            "SELECT source_channel FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["source_channel"] == "telegram"

    def test_raw_input_exact_match(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        text = "   leading space preserved   "
        result = process_telegram_text_update(conn, _standard_payload(text=text))

        row = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["raw_input"] == text

    def test_source_payload_contains_evidence(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        evidence = conn.execute(
            "SELECT source_payload FROM raw_intake_evidence WHERE raw_intake_record_id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        payload = json.loads(evidence["source_payload"])
        assert payload["telegram_update_id"] == "123456"
        assert payload["chat_id"] == "-456789123"
        assert payload["message_id"] == "789"
        assert payload["sender_id"] == "987654321"
        assert "source_received_at" in payload
        assert "telegram_message_date" in payload

    def test_external_source_id_follows_convention(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        row = conn.execute(
            "SELECT external_source_id FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["external_source_id"] == "telegram:-456789123:789"

    def test_idempotency_key_follows_convention(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        row = conn.execute(
            "SELECT idempotency_key FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["idempotency_key"] == "raw-intake:telegram:-456789123:789"

    def test_content_fingerprint_is_populated(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        row = conn.execute(
            "SELECT content_fingerprint, fingerprint_version FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["content_fingerprint"] is not None
        assert len(row["content_fingerprint"]) == 64  # SHA-256 hex
        assert row["fingerprint_version"] == "raw-intake-v1"

    def test_source_message_id_preserved(self, migrated_temp_db_connection: sqlite3.Connection):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())

        row = conn.execute(
            "SELECT source_message_id FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        assert row["source_message_id"] == "789"

    def test_creates_no_final_financial_facts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        process_telegram_text_update(conn, _standard_payload())

        assert _count(conn, "transactions") == 0
        assert _count(conn, "shared_expense_obligations") == 0
        assert _count(conn, "settlement_obligations") == 0
        assert _count(conn, "reconciliation_records") == 0
        assert _count(conn, "calculation_runs") == 0

    def test_source_received_at_uses_telegram_timestamp(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        date = 1717171200
        result = process_telegram_text_update(conn, _standard_payload(date=date))

        row = conn.execute(
            "SELECT source_received_at, received_at FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        expected_source = datetime.fromtimestamp(date, tz=UTC).isoformat()
        assert row["source_received_at"] == expected_source
        assert row["received_at"] is not None


# ---------------------------------------------------------------------------
#  Integration: Replay and idempotency
# ---------------------------------------------------------------------------


class TestReplayIdempotency:
    def test_same_chat_message_text_returns_existing(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload()

        first = process_telegram_text_update(conn, payload)
        second = process_telegram_text_update(conn, dict(payload))

        assert second["intake"]["id"] == first["intake"]["id"]
        assert second["parser_output"]["id"] == first["parser_output"]["id"]
        assert _count(conn, "raw_intake_records") == 1
        assert _count(conn, "raw_intake_evidence") == 1
        assert _count(conn, "parser_outputs") == 1

    def test_replay_does_not_duplicate_evidence(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload()

        process_telegram_text_update(conn, payload)
        process_telegram_text_update(conn, dict(payload))

        assert _count(conn, "raw_intake_evidence") == 1

    def test_replay_does_not_duplicate_parser_output(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload()

        process_telegram_text_update(conn, payload)
        process_telegram_text_update(conn, dict(payload))

        assert _count(conn, "parser_outputs") == 1

    def test_same_key_different_text_raises_conflict(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload(text="Coffee SGD 6.40 at Starbucks")

        process_telegram_text_update(conn, payload)

        conflict_payload = _standard_payload(text="Different text SGD 9.90")
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(conn, conflict_payload)

    def test_conflicting_replay_leaves_row_counts_unchanged(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload(text="Original text SGD 5.50")

        process_telegram_text_update(conn, payload)

        before = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }

        conflict_payload = _standard_payload(text="Conflicting text SGD 9.90")
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(conn, conflict_payload)

        after = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        assert after == before

    def test_different_message_ids_same_text_creates_separate_records(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        text = "Coffee SGD 6.40 at Starbucks"

        first = process_telegram_text_update(
            conn,
            _standard_payload(message_id=1, text=text),
        )
        second = process_telegram_text_update(
            conn,
            _standard_payload(message_id=2, text=text),
        )

        assert second["intake"]["id"] != first["intake"]["id"]
        assert _count(conn, "raw_intake_records") == 2

    def test_different_chat_ids_same_message_id_creates_separate_records(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection

        first = process_telegram_text_update(
            conn,
            _standard_payload(chat_id=111),
        )
        second = process_telegram_text_update(
            conn,
            _standard_payload(chat_id=222),
        )

        assert second["intake"]["id"] != first["intake"]["id"]
        assert _count(conn, "raw_intake_records") == 2

    def test_replayed_timestamp_does_not_create_duplicate(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload(date=1717171200)

        first = process_telegram_text_update(conn, payload)
        replay_payload = dict(payload)
        replay_payload["message"]["date"] = 1717171300
        second = process_telegram_text_update(conn, replay_payload)

        assert second["intake"]["id"] == first["intake"]["id"]
        assert _count(conn, "raw_intake_records") == 1

    def test_update_id_does_not_bypass_content_conflict(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload(update_id=1, text="Original SGD 5.50")

        process_telegram_text_update(conn, payload)

        conflict_payload = _standard_payload(
            update_id=2,
            text="Different SGD 9.90",
        )
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(conn, conflict_payload)

    def test_original_source_payload_not_overwritten_on_replay(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload(update_id=1)

        process_telegram_text_update(conn, payload)

        replay_payload = _standard_payload(update_id=999)
        result = process_telegram_text_update(conn, replay_payload)

        evidence = conn.execute(
            "SELECT source_payload FROM raw_intake_evidence WHERE raw_intake_record_id = ?",
            (result["intake"]["id"],),
        ).fetchone()
        stored = json.loads(evidence["source_payload"])
        assert stored["telegram_update_id"] == "1"


# ---------------------------------------------------------------------------
#  Regression: Finding 1 — Validation cannot be bypassed
# ---------------------------------------------------------------------------


class TestValidationBypassPrevented:
    """The public persistence entry must validate internally on every call."""

    def test_public_entry_accepts_raw_payload_and_persists(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _standard_payload())
        assert result["intake"]["source_type"] == "telegram_text"
        assert _count(conn, "raw_intake_records") == 1

    def test_direct_dto_passed_to_public_entry_is_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        dto = TelegramTextUpdate(
            update_id=1,
            message_id=2,
            chat_id=3,
            date=1717171200,
            text="valid text",
            sender_id=42,
            _source_received_at="2024-05-31T16:00:00+00:00",
        )
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, dto)
        assert exc.value.reason_code == "PAYLOAD_NOT_MAPPING"

    def test_invalid_raw_payload_passed_directly_raises_validation_error(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(
                conn,
                {
                    "update_id": True,
                    "message": {"message_id": "bad", "date": 1717171200, "chat": {"id": 1}},
                },
            )
        assert exc.value.reason_code == "UPDATE_ID_BOOL"

    def test_invalid_raw_payload_creates_no_db_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        before = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            process_telegram_text_update(conn, {"update_id": True, "message": {}})
        after = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "raw_intake_evidence": _count(conn, "raw_intake_evidence"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        assert after == before

    def test_whitespace_only_text_passed_to_public_entry_creates_no_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        before = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            process_telegram_text_update(conn, _standard_payload(text="   \n\t   "))
        after = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        assert after == before

    def test_invalid_message_id_passed_to_public_entry_creates_no_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        before = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            process_telegram_text_update(conn, _standard_payload(message_id=0))
        after = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        assert after == before

    def test_invalid_date_passed_to_public_entry_creates_no_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        before = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            process_telegram_text_update(conn, _standard_payload(date=-1))
        after = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        assert after == before


# ---------------------------------------------------------------------------
#  Regression: Finding 2 — Timestamp conversion boundaries
# ---------------------------------------------------------------------------


class TestTimestampConversionBoundaries:
    """Unconvertible or extremely large timestamps must be caught during validation."""

    def test_valid_timestamp_passes(self):
        update = validate_telegram_text_update(_standard_payload(date=1717171200))
        expected = datetime.fromtimestamp(1717171200, tz=UTC).isoformat()
        assert update.source_received_at == expected
        assert update.date == 1717171200

    def test_pre_epoch_timestamp_rejected(self):
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(_standard_payload(date=0))
        assert exc.value.reason_code == "MESSAGE_DATE_OUT_OF_RANGE"

    def test_extremely_large_timestamp_rejected(self):
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(_standard_payload(date=10**100))
        assert exc.value.reason_code == "MESSAGE_DATE_OUT_OF_RANGE"

    def test_timestamp_beyond_datetime_range_rejected(self):
        # Timestamp beyond the upper bound of datetime.fromtimestamp on most platforms.
        beyond = 253402300801  # Year 9999-12-31 + 1 second
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(_standard_payload(date=beyond))
        assert exc.value.reason_code == "MESSAGE_DATE_OUT_OF_RANGE"

    def test_no_raw_overflow_error_escapes(self):
        """Validation must convert OverflowError into TelegramTextUpdateValidationError."""
        try:
            validate_telegram_text_update(_standard_payload(date=10**100))
        except TelegramTextUpdateValidationError:
            pass
        except (OverflowError, OSError, ValueError) as raw:
            pytest.fail(f"Raw platform error escaped validation: {type(raw).__name__}: {raw}")

    def test_invalid_range_creates_no_db_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        before = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            validate_telegram_text_update(_standard_payload(date=10**100))
        after = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        assert after == before


# ---------------------------------------------------------------------------
#  Regression: Finding 3 — Malformed sender rejection
# ---------------------------------------------------------------------------


class TestMalformedSenderRejection:
    """Malformed ``message.from`` values must be rejected with sender-specific reason codes."""

    def test_from_absent_accepted(self):
        payload = _standard_payload()
        del payload["message"]["from"]
        update = validate_telegram_text_update(payload)
        assert update.sender_id is None

    def test_valid_sender_id_accepted(self):
        update = validate_telegram_text_update(_standard_payload(sender_id=42))
        assert update.sender_id == 42

    def test_from_is_string_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = "not-a-mapping"
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_NOT_MAPPING"

    def test_from_is_list_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = [1, 2, 3]
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_NOT_MAPPING"

    def test_from_is_int_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = 12345
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_NOT_MAPPING"

    def test_from_mapping_missing_id_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = {"first_name": "Owner"}
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "MISSING_SENDER_ID"

    def test_sender_id_bool_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = {"id": True, "first_name": "Owner"}
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_ID_BOOL"

    def test_sender_id_string_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = {"id": "abc", "first_name": "Owner"}
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_ID_TYPE"

    def test_sender_id_float_rejected(self):
        payload = _standard_payload()
        payload["message"]["from"] = {"id": 1.5, "first_name": "Owner"}
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_ID_TYPE"

    def test_invalid_sender_creates_no_db_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload()
        payload["message"]["from"] = "not-a-mapping"
        before = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            validate_telegram_text_update(payload)
        after = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        assert after == before

    def test_from_explicit_none_rejected(self):
        """message.from = None is malformed, not absent."""
        payload = _standard_payload()
        payload["message"]["from"] = None
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            validate_telegram_text_update(payload)
        assert exc.value.reason_code == "SENDER_NOT_MAPPING"

    def test_from_explicit_none_via_public_entry_creates_no_rows(
        self, migrated_temp_db_connection: sqlite3.Connection
    ):
        conn = migrated_temp_db_connection
        payload = _standard_payload()
        payload["message"]["from"] = None
        before = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        with pytest.raises(TelegramTextUpdateValidationError):
            process_telegram_text_update(conn, payload)
        after = {
            t: _count(conn, t)
            for t in ("raw_intake_records", "raw_intake_evidence", "parser_outputs")
        }
        assert after == before
