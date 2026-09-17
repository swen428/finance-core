"""Canonical Telegram text finance workflow integration coverage.

Proves the complete temporary-database workflow from raw Telegram text
update through raw intake, parser proposal, and authenticated human
confirmation.  Conversion to canonical transaction is currently blocked:
the deterministic text parser does not yet produce ``transaction_date``,
and no public proposal-completion boundary supplies it.

No production database writes, network calls, or production source changes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.intake.raw_text_repository import RawIntakeIdempotencyConflictError
from finance_core.intake.telegram_text_adapter import (
    TelegramTextUpdateValidationError,
    process_telegram_text_update,
)
from finance_core.parser_proposals.confirmation import (
    confirm_proposal,
    reject_proposal,
)
from finance_core.parser_proposals.content_hash import compute_proposal_content_hash
from finance_core.parser_proposals.conversion import (
    InvalidProposalStatusError,
    MissingRequiredTransactionFieldError,
    UnsupportedProposalTypeError,
    convert_confirmed_proposal_to_transaction,
)
from finance_core.parser_proposals.repository import ParserProposalRepository

# ---------------------------------------------------------------------------
#  Shared helpers
# ---------------------------------------------------------------------------

SGD_EXPENSE_TEXT = "Lunch SGD 12.50 at ExampleCafe paid by Owner"
FIXED_UPDATE_ID = 123456
FIXED_MESSAGE_ID = 789
FIXED_CHAT_ID = -456789123
FIXED_DATE = 1717171200  # 2024-05-31T16:00:00 UTC
FIXED_SENDER_ID = 987654321

# Every table that the canonical migration should create, used to prove
# that malformed or blocked workflows leave no state behind.
_CANONICAL_TABLES: tuple[str, ...] = (
    "raw_intake_records",
    "raw_intake_evidence",
    "parser_outputs",
    "parser_proposal_field_evidence",
    "parser_proposal_authorizations",
    "parser_proposal_conversion_audit",
    "parser_proposal_events",
    "transactions",
    "shared_expense_obligations",
    "settlement_obligations",
    "reconciliation_records",
    "calculation_runs",
    "receipt_finalization_audit",
    "financial_audit_events",
)


def _telegram_payload(
    *,
    update_id: int = FIXED_UPDATE_ID,
    message_id: int = FIXED_MESSAGE_ID,
    chat_id: int = FIXED_CHAT_ID,
    date: int = FIXED_DATE,
    text: str = SGD_EXPENSE_TEXT,
    sender_id: int | None = FIXED_SENDER_ID,
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


# ---------------------------------------------------------------------------
#  Read-only database verification helpers
# ---------------------------------------------------------------------------


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _fetch_one(conn: sqlite3.Connection, query: str, params: tuple = ()) -> sqlite3.Row:
    row = conn.execute(query, params).fetchone()
    assert row is not None, f"Expected one row for: {query}"
    return row


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }


def _assert_table_exists(conn: sqlite3.Connection, table: str) -> None:
    existing = _table_names(conn)
    assert table in existing, f"Expected table '{table}' not found in schema"


def _assert_table_zero(conn: sqlite3.Connection, table: str) -> None:
    """Assert *table* exists and has zero rows."""
    _assert_table_exists(conn, table)
    assert _count(conn, table) == 0, f"Expected 0 rows in {table}"


def _assert_all_canonical_financial_tables_zero(conn: sqlite3.Connection) -> None:
    """Every financial/audit table exists and is empty."""
    for table in _CANONICAL_TABLES:
        _assert_table_zero(conn, table)


def _assert_temp_db_integrity(conn: sqlite3.Connection) -> None:
    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert fk_rows == [], f"Foreign key violations: {fk_rows}"
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    assert integrity is not None and integrity[0] == "ok", f"Integrity: {integrity}"


# ---------------------------------------------------------------------------
#  Scenario 1: Canonical workflow boundary (conversion blocked)
# ---------------------------------------------------------------------------


class TestCanonicalWorkflowBoundary:
    """Intake -> parser proposal -> human confirmation.

    Conversion is *not* covered: the deterministic text parser does not
    yet produce ``transaction_date``, and there is no public
    proposal-completion boundary.  The test proves the truthful workflow
    boundary and the exact error raised when conversion is attempted.
    """

    # --- intake-level assertions ---------------------------------------

    def test_intake_creates_no_final_transaction(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        process_telegram_text_update(conn, _telegram_payload())
        assert _count(conn, "transactions") == 0
        _assert_table_zero(conn, "parser_proposal_conversion_audit")

    def test_raw_input_preserved_exactly(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        row = _fetch_one(
            conn,
            "SELECT raw_input FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        )
        assert row["raw_input"] == SGD_EXPENSE_TEXT

    def test_source_evidence_preserved(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        intake = _fetch_one(
            conn,
            "SELECT * FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        )

        # --- authoritative intake columns ---
        assert intake["source_type"] == "telegram_text"
        assert intake["source_channel"] == "telegram"
        assert intake["source_message_id"] == str(FIXED_MESSAGE_ID)
        expected_external = f"telegram:{FIXED_CHAT_ID}:{FIXED_MESSAGE_ID}"
        assert intake["external_source_id"] == expected_external
        expected_ts = datetime.fromtimestamp(FIXED_DATE, tz=UTC).isoformat()
        assert intake["source_received_at"] == expected_ts
        assert intake["idempotency_key"] is not None
        expected_idem = f"raw-intake:telegram:{FIXED_CHAT_ID}:{FIXED_MESSAGE_ID}"
        assert intake["idempotency_key"] == expected_idem
        assert intake["content_fingerprint"] is not None
        assert len(intake["content_fingerprint"]) == 64
        assert intake["fingerprint_version"] == "raw-intake-v1"

        # --- persisted evidence payload ---
        evidence = conn.execute(
            "SELECT source_payload FROM raw_intake_evidence WHERE raw_intake_record_id = ?",
            (intake["id"],),
        ).fetchone()
        assert evidence is not None
        payload = json.loads(evidence["source_payload"])
        assert payload["telegram_update_id"] == str(FIXED_UPDATE_ID)
        assert payload["chat_id"] == str(FIXED_CHAT_ID)
        assert payload["message_id"] == str(FIXED_MESSAGE_ID)
        assert payload["source_message_id"] == str(FIXED_MESSAGE_ID)
        assert payload["sender_id"] == str(FIXED_SENDER_ID)
        assert payload["telegram_message_date"] == str(FIXED_DATE)
        assert payload["source_received_at"] == expected_ts

    def test_single_intake_and_proposal_created(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        process_telegram_text_update(conn, _telegram_payload())
        assert _count(conn, "raw_intake_records") == 1
        assert _count(conn, "raw_intake_evidence") == 1
        assert _count(conn, "parser_outputs") == 1

    def test_proposal_linked_to_intake(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        intake_id = result["intake"]["id"]
        parser_output_id = result["parser_output"]["id"]
        intake = _fetch_one(
            conn,
            "SELECT parser_output_id FROM raw_intake_records WHERE id = ?",
            (intake_id,),
        )
        assert intake["parser_output_id"] == parser_output_id
        parser = _fetch_one(
            conn,
            "SELECT source_public_id FROM parser_outputs WHERE id = ?",
            (parser_output_id,),
        )
        assert parser["source_public_id"] == result["intake"]["public_id"]

    # --- confirmation-level assertions --------------------------------

    def test_confirmation_succeeds_without_conversion(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        assert result["intake"]["status"] == "parsed_pending_confirmation"

        confirmation = confirm_proposal(
            conn,
            parser_output_id,
            actor="owner-authenticated",
            confirmation_public_id="pca_canonical_test",
        )
        assert confirmation["to_status"] == "confirmed"
        assert confirmation["final_transaction_created"] is False
        assert confirmation["proposal_content_hash"] is not None
        assert len(confirmation["proposal_content_hash"]) == 64
        content_hash = confirmation["proposal_content_hash"]

        # Authorization record persisted and content-bound
        auth = _fetch_one(
            conn,
            "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?",
            (parser_output_id,),
        )
        assert auth["confirmation_public_id"] == "pca_canonical_test"
        assert auth["actor_type"] == "human"
        assert auth["authenticated_actor_id"] == "owner-authenticated"
        assert auth["proposal_content_hash"] == content_hash
        assert auth["confirmation_state"] == "confirmed"
        assert auth["revoked_at"] is None

        # Content hash is verifiable through the public helper
        proposal = ParserProposalRepository(conn).get(parser_output_id)
        assert proposal is not None
        computed = compute_proposal_content_hash(conn, proposal)
        assert computed == content_hash

        # Confirmation alone does not create a transaction
        assert _count(conn, "transactions") == 0
        _assert_table_zero(conn, "parser_proposal_conversion_audit")

    def test_conversion_blocked_by_missing_transaction_date(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner-authenticated",
            confirmation_public_id="pca_block_test",
        )
        with pytest.raises(
            MissingRequiredTransactionFieldError,
            match="Missing required field: transaction_date",
        ):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)

        assert _count(conn, "transactions") == 0
        _assert_table_zero(conn, "parser_proposal_conversion_audit")
        # No successful conversion financial-audit event
        audited = conn.execute(
            "SELECT event_type FROM financial_audit_events "
            "WHERE aggregate_public_id = ? ORDER BY event_public_id",
            (result["parser_output"]["public_id"],),
        ).fetchall()
        event_types = [r["event_type"] for r in audited]
        assert "parser_proposal_converted" not in event_types

    def test_financial_audit_chain_has_confirmation_event_and_verifies(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner-authenticated",
            confirmation_public_id="pca_audit_test",
        )
        chain = verify_financial_audit_chain(
            conn,
            aggregate_type="parser_proposal",
            aggregate_public_id=proposal_public_id,
        )
        assert chain.valid is True
        assert chain.event_count == 1

        events = FinancialAuditRepository(conn).list_chain(
            "parser_proposal",
            proposal_public_id,
        )
        assert [e.event_type for e in events] == ["parser_proposal_confirmed"]
        assert events[0].previous_event_hash == "0" * 64

    def test_no_final_facts_after_confirmation(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        confirm_proposal(
            conn,
            result["parser_output"]["id"],
            actor="owner",
            confirmation_public_id="pca_nf_test",
        )
        assert _count(conn, "transactions") == 0
        _assert_table_zero(conn, "parser_proposal_conversion_audit")
        _assert_table_zero(conn, "shared_expense_obligations")
        _assert_table_zero(conn, "settlement_obligations")
        _assert_table_zero(conn, "reconciliation_records")
        _assert_table_zero(conn, "calculation_runs")
        _assert_table_zero(conn, "receipt_finalization_audit")

    def test_temp_db_integrity_after_workflow(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        confirm_proposal(
            conn,
            result["parser_output"]["id"],
            actor="owner",
            confirmation_public_id="pca_int_test",
        )
        _assert_temp_db_integrity(conn)

    def test_audit_events_link_through_authorization(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]
        cid = "pca_audit_link_test"
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id=cid,
        )
        events = FinancialAuditRepository(conn).list_chain(
            "parser_proposal",
            proposal_public_id,
        )
        assert events[0].authorization_public_id == cid
        assert events[0].causation_public_id == cid

    def test_raw_input_never_overwritten_after_confirmation(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="pca_ro_test",
        )
        intake = _fetch_one(
            conn,
            "SELECT raw_input FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        )
        assert intake["raw_input"] == SGD_EXPENSE_TEXT
        parser = _fetch_one(
            conn,
            "SELECT raw_text FROM parser_outputs WHERE id = ?",
            (parser_output_id,),
        )
        assert parser["raw_text"] == SGD_EXPENSE_TEXT


# ---------------------------------------------------------------------------
#  Scenario 2: Equivalent Telegram retry (idempotent replay)
# ---------------------------------------------------------------------------


class TestEquivalentTelegramRetry:
    """Same Telegram source identity with same text is idempotent."""

    def test_same_payload_returns_same_intake_and_proposal(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload = _telegram_payload()
        first = process_telegram_text_update(conn, payload)
        second = process_telegram_text_update(conn, dict(payload))
        assert second["intake"]["id"] == first["intake"]["id"]
        assert second["parser_output"]["id"] == first["parser_output"]["id"]
        assert _count(conn, "raw_intake_records") == 1
        assert _count(conn, "raw_intake_evidence") == 1
        assert _count(conn, "parser_outputs") == 1

    def test_retry_after_confirmation_is_idempotent(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload = _telegram_payload()
        result = process_telegram_text_update(conn, payload)
        parser_output_id = result["parser_output"]["id"]
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="pca_retry_cfm",
        )
        before = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "parser_outputs": _count(conn, "parser_outputs"),
            "transactions": _count(conn, "transactions"),
            "financial_audit_events": _count(conn, "financial_audit_events"),
            "parser_proposal_authorizations": _count(
                conn,
                "parser_proposal_authorizations",
            ),
        }
        retry = process_telegram_text_update(conn, dict(payload))
        assert retry["intake"]["id"] == result["intake"]["id"]
        assert retry["parser_output"]["id"] == parser_output_id

        after = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "parser_outputs": _count(conn, "parser_outputs"),
            "transactions": _count(conn, "transactions"),
            "financial_audit_events": _count(conn, "financial_audit_events"),
            "parser_proposal_authorizations": _count(
                conn,
                "parser_proposal_authorizations",
            ),
        }
        assert after == before

    def test_retry_does_not_create_duplicate_authorization(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        confirm_proposal(conn, result["parser_output"]["id"], actor="owner")
        assert _count(conn, "parser_proposal_authorizations") == 1
        process_telegram_text_update(conn, _telegram_payload())
        assert _count(conn, "parser_proposal_authorizations") == 1

    def test_stable_public_ids_remain_unchanged(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        first = process_telegram_text_update(conn, _telegram_payload())
        second = process_telegram_text_update(conn, _telegram_payload())
        assert second["intake"]["public_id"] == first["intake"]["public_id"]
        assert second["parser_output"]["public_id"] == first["parser_output"]["public_id"]

    def test_retry_after_confirmation_idempotent_audit_event(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="pca_iac",
        )
        before_count = _count(conn, "financial_audit_events")
        process_telegram_text_update(conn, _telegram_payload())
        after_count = _count(conn, "financial_audit_events")
        assert after_count == before_count
        events = FinancialAuditRepository(conn).list_chain(
            "parser_proposal",
            proposal_public_id,
        )
        assert len(events) == 1
        assert events[0].event_type == "parser_proposal_confirmed"


# ---------------------------------------------------------------------------
#  Scenario 3: Conflicting Telegram retry
# ---------------------------------------------------------------------------


class TestConflictingTelegramRetry:
    """Same Telegram source identity with different text is a conflict."""

    def test_same_source_different_text_raises_conflict(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        process_telegram_text_update(
            conn,
            _telegram_payload(text="Coffee SGD 6.40 at Starbucks"),
        )
        conflict = _telegram_payload(text="Coffee SGD 9.90 at Starbucks")
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(conn, conflict)

    def test_conflict_does_not_mutate_original_intake(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        original = "Coffee SGD 6.40 at Starbucks"
        process_telegram_text_update(
            conn,
            _telegram_payload(text=original),
        )
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(
                conn,
                _telegram_payload(text="Coffee SGD 9.90 at Starbucks"),
            )
        rows = conn.execute(
            "SELECT raw_input FROM raw_intake_records",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["raw_input"] == original

    def test_conflict_does_not_create_second_parser_output(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        process_telegram_text_update(conn, _telegram_payload())
        before = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        conflict = _telegram_payload(text="Different text SGD 9.90")
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(conn, conflict)
        after = {
            "raw_intake_records": _count(conn, "raw_intake_records"),
            "parser_outputs": _count(conn, "parser_outputs"),
        }
        assert after == before

    def test_conflict_after_confirmation_no_extra_audit(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        confirm_proposal(
            conn,
            result["parser_output"]["id"],
            actor="owner",
            confirmation_public_id="pca_cfl",
        )
        before = _count(conn, "financial_audit_events")
        with pytest.raises(RawIntakeIdempotencyConflictError):
            process_telegram_text_update(
                conn,
                _telegram_payload(text="Completely different SGD 99.99"),
            )
        assert _count(conn, "financial_audit_events") == before


# ---------------------------------------------------------------------------
#  Scenario 4: Missing or malformed required Telegram fields
# ---------------------------------------------------------------------------


class TestMissingOrMalformedFields:
    """Canonical workflow fails closed at its entry boundary.

    Each test invokes the adapter exactly once and proves that *no*
    financial or audit table receives a row.
    """

    def test_missing_message(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, {"update_id": 1})
        assert exc.value.reason_code == "MISSING_MESSAGE"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_missing_update_id(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "hi",
            }
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "MISSING_UPDATE_ID"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_missing_message_id(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "update_id": 1,
            "message": {"date": 1717171200, "chat": {"id": 1}, "text": "hi"},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "MISSING_MESSAGE_ID"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_missing_chat(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "update_id": 1,
            "message": {"message_id": 1, "date": 1717171200, "text": "hi"},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "MISSING_CHAT"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_missing_message_date(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "update_id": 1,
            "message": {"message_id": 1, "chat": {"id": 1}, "text": "hi"},
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "MISSING_MESSAGE_DATE"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_missing_text(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "MISSING_TEXT"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_empty_text(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "text": "",
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "TEXT_EMPTY_OR_WHITESPACE"
        _assert_all_canonical_financial_tables_zero(conn)

    def test_media_without_text(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        payload: dict[str, Any] = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1717171200,
                "chat": {"id": 1},
                "photo": [{"file_id": "abc"}],
            },
        }
        with pytest.raises(TelegramTextUpdateValidationError) as exc:
            process_telegram_text_update(conn, payload)
        assert exc.value.reason_code == "MEDIA_WITHOUT_TEXT"
        _assert_all_canonical_financial_tables_zero(conn)


# ---------------------------------------------------------------------------
#  Scenario 5: Rejected proposal
# ---------------------------------------------------------------------------


class TestRejectedProposal:
    """Human rejection blocks conversion and preserves evidence."""

    def test_rejection_is_bound_to_proposal_content(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        rejection = reject_proposal(
            conn,
            parser_output_id,
            actor="owner-authenticated",
            reason="wrong merchant",
        )
        assert rejection["to_status"] == "rejected"
        assert rejection["proposal_content_hash"] is not None
        auth = _fetch_one(
            conn,
            "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?",
            (parser_output_id,),
        )
        assert auth["confirmation_state"] == "rejected"
        assert auth["proposal_content_hash"] == rejection["proposal_content_hash"]

    def test_rejected_proposal_cannot_be_converted(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        reject_proposal(
            conn,
            parser_output_id,
            actor="owner",
            reason="wrong parse",
        )
        with pytest.raises(InvalidProposalStatusError, match="rejected"):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)
        assert _count(conn, "transactions") == 0

    def test_rejection_preserves_original_evidence(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        intake_before = _row_dict(
            _fetch_one(
                conn,
                "SELECT raw_input FROM raw_intake_records WHERE id = ?",
                (result["intake"]["id"],),
            )
        )
        parser_before = _row_dict(
            _fetch_one(
                conn,
                "SELECT raw_text, parsed_payload FROM parser_outputs WHERE id = ?",
                (parser_output_id,),
            )
        )
        reject_proposal(
            conn,
            parser_output_id,
            actor="owner",
            reason="wrong",
        )
        intake_after = _row_dict(
            _fetch_one(
                conn,
                "SELECT raw_input FROM raw_intake_records WHERE id = ?",
                (result["intake"]["id"],),
            )
        )
        parser_after = _row_dict(
            _fetch_one(
                conn,
                "SELECT raw_text, parsed_payload FROM parser_outputs WHERE id = ?",
                (parser_output_id,),
            )
        )
        assert intake_after["raw_input"] == intake_before["raw_input"]
        assert parser_after["raw_text"] == parser_before["raw_text"]
        assert parser_after["parsed_payload"] == parser_before["parsed_payload"]

    def test_rejection_creates_no_transaction_or_audit_success(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        reject_proposal(
            conn,
            parser_output_id,
            actor="owner",
            reason="wrong",
        )
        assert _count(conn, "transactions") == 0
        _assert_table_zero(conn, "parser_proposal_conversion_audit")
        chain = FinancialAuditRepository(conn).list_chain(
            "parser_proposal",
            result["parser_output"]["public_id"],
        )
        assert len(chain) == 1
        assert chain[0].event_type == "parser_proposal_rejected"

    def test_rejection_event_is_audited(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]
        reject_proposal(conn, parser_output_id, actor="owner")
        chain = verify_financial_audit_chain(
            conn,
            aggregate_type="parser_proposal",
            aggregate_public_id=proposal_public_id,
        )
        assert chain.valid is True
        assert chain.event_count == 1


# ---------------------------------------------------------------------------
#  Scenario 6: Unsupported shared-expense conversion
# ---------------------------------------------------------------------------


class TestUnsupportedSharedExpenseConversion:
    """Shared/split expense proposals cannot be converted by simple-expense."""

    SHARED_TEXT = "Dinner MYR 85.00 shared equally with Owner, John, Mary at Nando's"

    def test_shared_expense_proposal_not_convertible(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(
            conn,
            _telegram_payload(message_id=999, text=self.SHARED_TEXT),
        )
        parser_output_id = result["parser_output"]["id"]
        assert result["proposal"]["transaction_type"] == "shared_expense"
        confirm_proposal(conn, parser_output_id, actor="owner")
        with pytest.raises(
            UnsupportedProposalTypeError,
            match="Only simple expense",
        ):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)

    def test_unsupported_conversion_creates_no_transaction(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(
            conn,
            _telegram_payload(message_id=998, text=self.SHARED_TEXT),
        )
        parser_output_id = result["parser_output"]["id"]
        confirm_proposal(conn, parser_output_id, actor="owner")
        with pytest.raises(UnsupportedProposalTypeError):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)
        assert _count(conn, "transactions") == 0
        _assert_table_zero(conn, "shared_expense_obligations")
        _assert_table_zero(conn, "settlement_obligations")
        _assert_table_zero(conn, "reconciliation_records")
        _assert_table_zero(conn, "receipt_finalization_audit")

    def test_shared_expense_evidence_preserved(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(
            conn,
            _telegram_payload(message_id=997, text=self.SHARED_TEXT),
        )
        parser_output_id = result["parser_output"]["id"]
        confirm_proposal(conn, parser_output_id, actor="owner")
        with pytest.raises(UnsupportedProposalTypeError):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)
        intake = _fetch_one(
            conn,
            "SELECT raw_input FROM raw_intake_records WHERE id = ?",
            (result["intake"]["id"],),
        )
        assert intake["raw_input"] == self.SHARED_TEXT
        parser = _fetch_one(
            conn,
            "SELECT raw_text, parse_status FROM parser_outputs WHERE id = ?",
            (parser_output_id,),
        )
        assert parser["raw_text"] == self.SHARED_TEXT
        assert parser["parse_status"] == "confirmed"

    def test_shared_expense_audit_confirmation_only(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(
            conn,
            _telegram_payload(message_id=996, text=self.SHARED_TEXT),
        )
        parser_output_id = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]
        confirm_proposal(conn, parser_output_id, actor="owner")
        with pytest.raises(UnsupportedProposalTypeError):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)
        chain = verify_financial_audit_chain(
            conn,
            aggregate_type="parser_proposal",
            aggregate_public_id=proposal_public_id,
        )
        assert chain.valid is True
        assert chain.event_count == 1
        events = FinancialAuditRepository(conn).list_chain(
            "parser_proposal",
            proposal_public_id,
        )
        assert events[0].event_type == "parser_proposal_confirmed"


# ---------------------------------------------------------------------------
#  Cross-cutting invariants
# ---------------------------------------------------------------------------


class TestCrossCuttingInvariants:
    """Tests spanning multiple scenarios or verifying system-level properties."""

    def test_database_not_live(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        temp_db_path: Path,
    ) -> None:
        db_file = Path(
            migrated_temp_db_connection.execute(
                "PRAGMA database_list",
            ).fetchone()["file"]
        )
        assert db_file == temp_db_path
        live_db = Path(__file__).resolve().parents[1] / "database" / "finance.db"
        assert db_file != live_db.resolve()

    def test_audit_chain_append_only(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]
        confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            confirmation_public_id="pca_ao_test",
        )

        chain = FinancialAuditRepository(conn).list_chain(
            "parser_proposal",
            proposal_public_id,
        )
        assert len(chain) == 1

        # Verify no update/delete is possible on a single audit event
        with pytest.raises((sqlite3.OperationalError, sqlite3.Error)):
            conn.execute(
                "DELETE FROM financial_audit_events WHERE event_public_id = ?",
                (chain[0].event_public_id,),
            )

    def test_confirmation_needed_before_conversion(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        parser_output_id = result["parser_output"]["id"]
        with pytest.raises(InvalidProposalStatusError):
            convert_confirmed_proposal_to_transaction(conn, parser_output_id)
        assert _count(conn, "transactions") == 0

    def test_intake_alone_never_creates_transaction(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        process_telegram_text_update(conn, _telegram_payload())
        process_telegram_text_update(conn, _telegram_payload())
        assert _count(conn, "transactions") == 0

    def test_confirming_does_not_create_transaction(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        result = process_telegram_text_update(conn, _telegram_payload())
        confirm_proposal(
            conn,
            result["parser_output"]["id"],
            actor="owner",
            confirmation_public_id="pca_ncf_test",
        )
        assert _count(conn, "transactions") == 0

    def test_fingerprint_is_deterministic(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        first = process_telegram_text_update(conn, _telegram_payload())
        second = process_telegram_text_update(conn, _telegram_payload())
        assert first["intake"]["content_fingerprint"] == second["intake"]["content_fingerprint"]

    def test_different_text_gives_different_fingerprint(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        a = process_telegram_text_update(
            conn,
            _telegram_payload(
                message_id=100,
                text="Coffee SGD 6.40 at Starbucks",
            ),
        )
        b = process_telegram_text_update(
            conn,
            _telegram_payload(
                message_id=101,
                text="Dinner SGD 25.00 at Pizza Hut",
            ),
        )
        assert a["intake"]["content_fingerprint"] != b["intake"]["content_fingerprint"]

    def test_temp_db_integrity_final(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        _assert_temp_db_integrity(migrated_temp_db_connection)

    def test_all_tests_use_temporary_database_only(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        temp_db_path: Path,
    ) -> None:
        db_file = Path(
            migrated_temp_db_connection.execute(
                "PRAGMA database_list",
            ).fetchone()["file"]
        )
        assert db_file == temp_db_path
        assert "finance.db" not in str(db_file)
