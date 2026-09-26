"""Authenticated parser proposal completion boundary tests.

Covers public API, validation, persistence, lifecycle, idempotency,
concurrency, atomicity, regression, and migration behaviour.
No live database access.  All tests use temporary databases only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.intake.telegram_text_adapter import process_telegram_text_update
from finance_core.parser_proposals.completion import (
    CompletionConflictError,
    InvalidCompletionFieldValueError,
    InvalidCompletionStatusError,
    NoMaterialChangeError,
    ProposalCompletionError,
    StaleProposalContentError,
    UnauthorizedCompletionActorError,
    UnknownCompletionFieldError,
    complete_proposal,
    resolve_effective_proposal_payload,
)
from finance_core.parser_proposals.confirmation import (
    confirm_proposal,
    reject_proposal,
)
from finance_core.parser_proposals.content_hash import compute_proposal_content_hash
from finance_core.parser_proposals.conversion import (
    InvalidProposalStatusError,
    MissingRequiredTransactionFieldError,
    convert_confirmed_proposal_to_transaction,
)
from finance_core.parser_proposals.lifecycle import (
    CONFIRMED,
    EDITED_PENDING_CONFIRMATION,
    PARSED_PENDING_CONFIRMATION,
)
from finance_core.parser_proposals.repository import ParserProposalRepository


@pytest.fixture()
def migrated_temp_db_connection(
    legacy_temp_db_connection: sqlite3.Connection,
) -> sqlite3.Connection:
    """Preserve pre-D3 Telegram intake coverage for proposal lifecycle tests."""
    return legacy_temp_db_connection


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SGD_EXPENSE_TEXT = "Lunch SGD 12.50 at ExampleCafe paid by Owner"


def _telegram_payload(
    text: str = SGD_EXPENSE_TEXT,
    message_id: int = 1,
) -> dict[str, Any]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "date": 1717171200,
            "chat": {"id": -456789123, "type": "group"},
            "text": text,
            "from": {"id": 987654321, "is_bot": False, "first_name": "Owner"},
        },
    }


def _intake_and_proposal(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the result dict from process_telegram_text_update."""
    return process_telegram_text_update(conn, _telegram_payload())


def _current_hash(conn: sqlite3.Connection, proposal: dict[str, Any]) -> str:
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash

    return compute_effective_proposal_content_hash(conn, proposal)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _count_edited_events(conn: sqlite3.Connection, parser_output_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM parser_proposal_events "
            "WHERE parser_output_id = ? AND event_type = 'edited'",
            (parser_output_id,),
        ).fetchone()["c"]
    )


def _count_audit_events(conn: sqlite3.Connection, reference_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM financial_audit_events WHERE aggregate_public_id = ?",
            (reference_id,),
        ).fetchone()["c"]
    )


# ---------------------------------------------------------------------------
# Valid completion
# ---------------------------------------------------------------------------


class TestValidCompletion:
    """Happy-path completion with authenticated human actor."""

    def test_human_completion_with_transaction_date(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        res = complete_proposal(
            conn,
            poid,
            actor="owner-authenticated",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_test_001",
        )
        assert res["idempotent"] is False
        assert res["to_status"] == EDITED_PENDING_CONFIRMATION
        assert res["version_number"] == 1
        assert res["changed_fields"] == ["transaction_date"]
        assert res["completed_content_hash"] != h

        # Verify persistence
        row = conn.execute(
            "SELECT * FROM parser_proposal_completions WHERE completion_public_id = ?",
            ("pco_test_001",),
        ).fetchone()
        assert row is not None
        assert row["version_number"] == 1
        assert row["actor_type"] == "human"
        assert row["authenticated_actor_id"] == "owner-authenticated"

    def test_user_actor_type_normalized(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        res = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_user_test",
            actor_type="user",
        )
        assert res["idempotent"] is False

    def test_multiple_fields_completion(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        res = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={
                "transaction_date": "2024-06-01",
                "merchant": "Example Cafe Food Court",
                "category": "dining",
            },
            completion_public_id="pco_multi",
        )
        assert res["changed_fields"] == ["category", "merchant", "transaction_date"]

    def test_original_payload_preserved(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_payload = result["parser_output"]["parsed_payload"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_preserve",
        )

        row = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()
        assert row["parsed_payload"] == original_payload

    def test_raw_text_preserved(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_raw",
        )

        row = conn.execute("SELECT raw_text FROM parser_outputs WHERE id = ?", (poid,)).fetchone()
        assert row["raw_text"] == SGD_EXPENSE_TEXT

    def test_effective_payload_reflects_completion(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_eff",
        )

        effective, cid, version = resolve_effective_proposal_payload(conn, poid)
        assert effective["transaction_date"] == "2024-05-31"
        assert cid == "pco_eff"
        assert effective["amount"] == "12.50"  # unchanged from original

    def test_no_transaction_created(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_no_txn",
        )
        assert _count(conn, "transactions") == 0
        assert _count(conn, "parser_proposal_conversion_audit") == 0

    def test_no_authorization_created(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_no_auth",
        )
        assert _count(conn, "parser_proposal_authorizations") == 0

    def test_audit_event_persisted(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        proposal_public_id = result["parser_output"]["public_id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_audit",
        )

        events = conn.execute(
            "SELECT event_type FROM financial_audit_events "
            "WHERE aggregate_public_id = ? ORDER BY sequence_number",
            (proposal_public_id,),
        ).fetchall()
        event_types = [e["event_type"] for e in events]
        assert "parser_proposal_completed" in event_types


# ---------------------------------------------------------------------------
# Validation and error cases
# ---------------------------------------------------------------------------


class TestCompletionValidation:
    """Command validation and actor authentication."""

    def test_ai_actor_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(UnauthorizedCompletionActorError, match="ai"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="gpt",
                expected_content_hash="a" * 64,
                field_updates={"transaction_date": "2024-05-31"},
                actor_type="ai",
                completion_public_id="pco_err",
            )

    def test_system_actor_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(UnauthorizedCompletionActorError, match="system"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="scheduler",
                expected_content_hash="a" * 64,
                field_updates={"transaction_date": "2024-05-31"},
                actor_type="system",
                completion_public_id="pco_err",
            )

    def test_test_actor_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(UnauthorizedCompletionActorError, match="test"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="tester",
                expected_content_hash="a" * 64,
                field_updates={"transaction_date": "2024-05-31"},
                actor_type="test",
                completion_public_id="pco_err",
            )

    def test_blank_actor_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(UnauthorizedCompletionActorError, match="must not be empty"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="",
                expected_content_hash="a" * 64,
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )

    def test_blank_channel_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(ProposalCompletionError, match="completion_channel"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash="a" * 64,
                field_updates={"transaction_date": "2024-05-31"},
                completion_channel="",
                completion_public_id="pco_err",
            )

    def test_malformed_completion_id_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        complete_proposal(
            conn,
            result["parser_output"]["id"],
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_err",  # empty generates ID
        )
        # completion_public_id="pco_err" is valid; the test below
        # verifies hash validation, not ID validation. Covered by TestCompletionIdValidation.
        with pytest.raises(ProposalCompletionError, match="64-character"):
            complete_proposal(
                conn,
                result["parser_output"]["id"] + 999,  # non-existent
                actor="owner",
                expected_content_hash="bad",
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )

    def test_malformed_expected_hash_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(ProposalCompletionError, match="64-character"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash="too-short",
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )

    def test_missing_proposal_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        with pytest.raises(ProposalCompletionError, match="not found"):
            complete_proposal(
                conn,
                99999,
                actor="owner",
                expected_content_hash="a" * 64,
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )

    def test_unknown_field_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(UnknownCompletionFieldError, match="unknown_field"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"unknown_field": "value"},
                completion_public_id="pco_err",
            )

    def test_monetary_field_edit_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(UnknownCompletionFieldError, match="cannot be changed"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"amount": 99.99},
                completion_public_id="pco_err",
            )

    def test_invalid_date_rejected(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(InvalidCompletionFieldValueError):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"transaction_date": "not-a-date"},
                completion_public_id="pco_err",
            )

    def test_non_string_date_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(InvalidCompletionFieldValueError, match="string"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"transaction_date": 20240531},
                completion_public_id="pco_err",
            )

    def test_impossible_calendar_date_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        with pytest.raises(InvalidCompletionFieldValueError, match="valid calendar date"):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"transaction_date": "2024-02-30"},
                completion_public_id="pco_err",
            )

    def test_no_material_change_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        # The test proposal has no merchant in the payload.
        # Completing with an empty merchant or one that doesn't change
        # the hash should fail. Let's test by doing a completion that
        # doesn't change anything — provide a field that doesn't exist.
        # The test parser already includes "merchant": "ExampleCafe" in the
        # parsed payload.
        # Let's complete with the same merchant value.
        with pytest.raises(NoMaterialChangeError):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"merchant": "ExampleCafe"},
                completion_public_id="pco_no_change",
            )

    # ---------------------------------------------------------------------------
    # Lifecycle and authorization
    # ---------------------------------------------------------------------------

    # --- actor / channel whitespace padding -----------------------------------

    def test_actor_leading_whitespace_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Actor with leading whitespace must be rejected."""
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        with pytest.raises(UnauthorizedCompletionActorError, match="whitespace"):
            complete_proposal(
                conn,
                poid,
                actor=" owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_actor_ws_1",
            )

    def test_actor_trailing_whitespace_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        with pytest.raises(UnauthorizedCompletionActorError, match="whitespace"):
            complete_proposal(
                conn,
                poid,
                actor="owner ",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_actor_ws_2",
            )

    def test_actor_both_whitespace_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        with pytest.raises(UnauthorizedCompletionActorError, match="whitespace"):
            complete_proposal(
                conn,
                poid,
                actor=" owner ",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_actor_ws_3",
            )

    def test_channel_leading_whitespace_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        with pytest.raises(ProposalCompletionError, match="channel.*whitespace"):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_ch_ws_1",
                completion_channel=" cli",
            )

    def test_channel_trailing_whitespace_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        with pytest.raises(ProposalCompletionError, match="channel.*whitespace"):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_ch_ws_2",
                completion_channel="cli ",
            )


class TestCompletionLifecycle:
    """Completion lifecycle transitions and state validation."""

    def test_parsed_pending_transitions_to_edited_pending(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        assert result["parser_output"]["parse_status"] == PARSED_PENDING_CONFIRMATION

        res = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_lifecycle",
        )
        assert res["to_status"] == EDITED_PENDING_CONFIRMATION

        row = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()
        assert row["parse_status"] == EDITED_PENDING_CONFIRMATION

    def test_confirmed_proposal_cannot_be_completed(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        confirm_proposal(conn, poid, actor="owner")

        with pytest.raises(InvalidCompletionStatusError, match="terminal"):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )

    def test_rejected_proposal_cannot_be_completed(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        reject_proposal(conn, poid, actor="owner")

        with pytest.raises(InvalidCompletionStatusError, match="terminal"):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )

    def test_stale_expected_hash_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        original_hash = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_stale_1",
        )

        # Try completing again with the stale (pre-completion) hash
        with pytest.raises(StaleProposalContentError):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=original_hash,
                field_updates={"transaction_date": "2024-06-01"},
                completion_public_id="pco_stale_2",
            )

    def test_completion_creates_no_authorization(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_noauth",
        )
        assert _count(conn, "parser_proposal_authorizations") == 0

    def test_completion_not_auto_confirming(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_noauto",
        )

        # Proposal is edited, not confirmed
        row = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()
        assert row["parse_status"] == EDITED_PENDING_CONFIRMATION
        assert row["parse_status"] != CONFIRMED

    def test_fresh_confirmation_after_completion(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_fresh_cfm",
        )

        # Now confirm — should work with the new hash
        new_hash = _current_hash(conn, result["parser_output"])
        cfm = confirm_proposal(
            conn,
            poid,
            actor="owner",
            confirmation_public_id="pca_after_comp",
        )
        assert cfm["to_status"] == CONFIRMED
        assert cfm["proposal_content_hash"] == new_hash


# ---------------------------------------------------------------------------
# Persistence and immutability
# ---------------------------------------------------------------------------


class TestCompletionPersistence:
    """Database-level immutability and constraint checks."""

    def test_original_parser_payload_unchanged(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original = result["parser_output"]["parsed_payload"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_payload",
        )

        row = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()
        assert row["parsed_payload"] == original

    def test_raw_intake_unchanged(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        original_raw = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE parser_output_id = ?",
            (poid,),
        ).fetchone()["raw_input"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_raw2",
        )

        after_raw = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE parser_output_id = ?",
            (poid,),
        ).fetchone()["raw_input"]
        assert after_raw == original_raw

    def test_completion_rows_cannot_be_updated(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_no_upd",
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE parser_proposal_completions SET reason = ? WHERE completion_public_id = ?",
                ("hacked", "pco_no_upd"),
            )

    def test_completion_rows_cannot_be_deleted(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_no_del",
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM parser_proposal_completions WHERE completion_public_id = ?",
                ("pco_no_del",),
            )

    def test_version_increments_deterministically(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h1 = _current_hash(conn, result["parser_output"])

        r1 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h1,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_v1",
        )
        assert r1["version_number"] == 1

        h2 = _current_hash(conn, result["parser_output"])
        r2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Example Cafe Food Court"},
            completion_public_id="pco_v2",
        )
        assert r2["version_number"] == 2

    def test_prior_completion_version_unchanged(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h1 = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h1,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_prior",
        )

        prior = conn.execute(
            "SELECT * FROM parser_proposal_completions WHERE completion_public_id = ?",
            ("pco_prior",),
        ).fetchone()
        prior_payload = prior["completed_payload_json"]
        prior_hash = prior["completed_content_hash"]

        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "New Merchant"},
            completion_public_id="pco_prior2",
        )

        still = conn.execute(
            "SELECT * FROM parser_proposal_completions WHERE completion_public_id = ?",
            ("pco_prior",),
        ).fetchone()
        assert still["completed_payload_json"] == prior_payload
        assert still["completed_content_hash"] == prior_hash


# ---------------------------------------------------------------------------
# Idempotency and conflict
# ---------------------------------------------------------------------------


class TestCompletionIdempotency:
    """Same-key replay and conflict semantics."""

    def test_same_key_equivalent_replay_idempotent(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        first = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_replay",
        )
        assert first["idempotent"] is False

        second = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_replay",
        )
        assert second["idempotent"] is True
        assert second["version_number"] == first["version_number"]
        assert second["completed_content_hash"] == first["completed_content_hash"]

    def test_same_key_different_value_conflicts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_conflict_v",
        )

        with pytest.raises(CompletionConflictError):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-01"},
                completion_public_id="pco_conflict_v",
            )

    def test_same_key_different_actor_conflicts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_conflict_a",
        )

        with pytest.raises(CompletionConflictError):
            complete_proposal(
                conn,
                poid,
                actor="other",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_conflict_a",
            )

    def test_same_key_different_channel_conflicts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_conflict_c",
            completion_channel="cli",
        )

        with pytest.raises(CompletionConflictError):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_conflict_c",
                completion_channel="web",
            )

    def test_different_key_no_content_change_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        # The trigger rejects same hash for base and completed
        with pytest.raises((NoMaterialChangeError, sqlite3.OperationalError)):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"merchant": "ExampleCafe"},
                completion_public_id="pco_diff_no_change",
            )

    def test_no_duplicate_events_on_replay(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_no_dup",
        )

        before_events = _count(conn, "parser_proposal_events")
        before_audit = _count(conn, "financial_audit_events")
        before_completions = _count(conn, "parser_proposal_completions")

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_no_dup",
        )

        assert _count(conn, "parser_proposal_events") == before_events
        assert _count(conn, "financial_audit_events") == before_audit
        assert _count(conn, "parser_proposal_completions") == before_completions


# ---------------------------------------------------------------------------
# Conversion after completion
# ---------------------------------------------------------------------------


class TestConversionAfterCompletion:
    """Canonical complete -> confirm -> convert workflow."""

    def test_conversion_without_completion_still_fails(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        confirm_proposal(conn, poid, actor="owner")

        with pytest.raises(MissingRequiredTransactionFieldError, match="transaction_date"):
            convert_confirmed_proposal_to_transaction(conn, poid)

    def test_complete_confirm_convert_workflow(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        # Step 1: Complete with transaction_date
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_workflow",
        )

        # Step 2: Confirm
        new_h = _current_hash(conn, result["parser_output"])
        cfm = confirm_proposal(conn, poid, actor="owner", confirmation_public_id="pca_workflow")
        assert cfm["proposal_content_hash"] == new_h

        # Step 3: Convert
        conv = convert_confirmed_proposal_to_transaction(conn, poid)
        assert conv["final_transaction_created"] is True
        assert conv["transaction_public_id"] is not None

        # Verify the transaction
        txn = conn.execute(
            "SELECT * FROM transactions WHERE public_id = ?",
            (conv["transaction_public_id"],),
        ).fetchone()
        assert txn is not None
        from decimal import Decimal

        assert Decimal(str(txn["amount"])) == Decimal("12.50")
        assert txn["currency"] == "SGD"
        assert txn["transaction_date"] == "2024-05-31"

    def test_completed_confirmation_binds_completed_hash(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_bind",
        )

        cfm = confirm_proposal(conn, poid, actor="owner", confirmation_public_id="pca_bind")
        new_h = _current_hash(conn, result["parser_output"])
        assert cfm["proposal_content_hash"] == new_h
        assert cfm["proposal_content_hash"] != h

    def test_conversion_after_completion_before_confirmation_fails(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_pre_cfm",
        )

        # Conversion without explicit confirmation still fails
        with pytest.raises(InvalidProposalStatusError):
            convert_confirmed_proposal_to_transaction(conn, poid)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestCompletionMigration:
    """Migration 030 integrity and schema verification."""

    def test_migration_030_table_exists(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='parser_proposal_completions'"
        ).fetchone()
        assert row is not None

    def test_migration_030_columns(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        cols = conn.execute("PRAGMA table_info(parser_proposal_completions)").fetchall()
        col_names = {col["name"] for col in cols}
        expected = {
            "id",
            "completion_public_id",
            "parser_output_id",
            "version_number",
            "base_content_hash",
            "completed_content_hash",
            "completed_payload_json",
            "field_updates_json",
            "actor_type",
            "authenticated_actor_id",
            "completion_channel",
            "reason",
            "created_at",
        }
        assert expected.issubset(col_names)

    def test_migration_030_triggers_exist(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%completion%'"
        ).fetchall()
        trigger_names = [t["name"] for t in triggers]
        assert "trg_parser_proposal_completions_no_update" in trigger_names
        assert "trg_parser_proposal_completions_no_delete" in trigger_names

    def test_temp_db_integrity(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        conn = migrated_temp_db_connection
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_rows == [], f"Foreign key violations: {fk_rows}"
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity is not None and integrity[0] == "ok"


# ---------------------------------------------------------------------------
# Regression: proposals without completion
# ---------------------------------------------------------------------------


class TestRegressionWithoutCompletion:
    """Existing behaviour must be preserved when no completion exists."""

    def test_proposal_without_completion_hashes_normally(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        proposal = ParserProposalRepository(conn).get(poid)
        assert proposal is not None
        h = compute_proposal_content_hash(conn, proposal)
        assert len(h) == 64

    def test_proposal_without_completion_confirms_normally(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        cfm = confirm_proposal(conn, poid, actor="owner")
        assert cfm["to_status"] == CONFIRMED
        assert cfm["proposal_content_hash"] is not None

    def test_resolve_effective_no_completion_returns_original(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        effective, cid, version = resolve_effective_proposal_payload(conn, poid)
        assert effective == json.loads(result["parser_output"]["parsed_payload"])
        assert cid is None

    def test_conversion_without_date_still_fails_truthfully(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        confirm_proposal(conn, poid, actor="owner")

        with pytest.raises(MissingRequiredTransactionFieldError, match="transaction_date"):
            convert_confirmed_proposal_to_transaction(conn, poid)


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


class TestCompletionCrossCutting:
    """Tests spanning multiple scenarios."""

    def test_database_not_live(
        self, migrated_temp_db_connection: sqlite3.Connection, temp_db_path: Path
    ) -> None:
        db_file = Path(
            migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
        )
        assert db_file == temp_db_path
        live_db = Path(__file__).resolve().parents[1] / "database" / "finance.db"
        assert db_file != live_db.resolve()

    def test_temp_db_integrity_after_completion(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=_current_hash(conn, result["parser_output"]),
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_integrity",
        )

        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_rows == []
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity is not None and integrity[0] == "ok"

    def test_public_import_exists(
        self,
    ) -> None:
        from finance_core.parser_proposals import complete_proposal as cp_public

        assert cp_public is complete_proposal


# ---------------------------------------------------------------------------
# Cumulative Version Tests
# ---------------------------------------------------------------------------


class TestCumulativeVersions:
    """Cumulative payload versioning: each new completion builds on the
    current effective payload so earlier authenticated field completions
    are never silently discarded."""

    def test_v1_adds_date_v2_adds_merchant_retains_date(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h1 = _current_hash(conn, result["parser_output"])

        # v1: add transaction_date
        _v = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h1,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_cumul_v1",
        )
        effective1, cid1, ver1 = resolve_effective_proposal_payload(conn, poid)
        assert effective1["transaction_date"] == "2024-05-31"
        assert ver1 == 1

        # v2: add merchant — must retain transaction_date from v1
        h2 = _current_hash(conn, result["parser_output"])
        _v2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "New Merchant"},
            completion_public_id="pco_cumul_v2",
        )
        effective2, cid2, ver2 = resolve_effective_proposal_payload(conn, poid)
        assert effective2["transaction_date"] == "2024-05-31"  # retained from v1
        assert effective2["merchant"] == "New Merchant"  # added in v2
        assert ver2 == 2

    def test_v3_adds_category_retains_date_and_merchant(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_cumul_v3a",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Cafe"},
            completion_public_id="pco_cumul_v3b",
        )
        h3 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h3,
            field_updates={"category": "Dining"},
            completion_public_id="pco_cumul_v3c",
        )

        effective, cid, ver = resolve_effective_proposal_payload(conn, poid)
        assert effective["transaction_date"] == "2024-05-31"  # from v1
        assert effective["merchant"] == "Cafe"  # from v2
        assert effective["category"] == "Dining"  # from v3
        assert ver == 3

    def test_original_parser_payload_remains_unchanged_after_cumulative(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_parsed = json.loads(result["parser_output"]["parsed_payload"])
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_cumul_orig1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Some Place"},
            completion_public_id="pco_cumul_orig2",
        )

        row = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()
        current_parsed = json.loads(row["parsed_payload"])
        assert current_parsed == original_parsed

    def test_confirmation_binds_final_cumulative_hash(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_cumul_cfm1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Final Place"},
            completion_public_id="pco_cumul_cfm2",
        )

        final_hash = _current_hash(conn, result["parser_output"])
        cfm = confirm_proposal(conn, poid, actor="owner", confirmation_public_id="pca_cumul_cfm")
        assert cfm["proposal_content_hash"] == final_hash
        assert cfm["proposal_content_hash"] != h

    def test_conversion_uses_cumulative_fields(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_cumul_conv1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Golden Mile"},
            completion_public_id="pco_cumul_conv2",
        )

        confirm_proposal(conn, poid, actor="owner", confirmation_public_id="pca_cumul_conv")
        conv = convert_confirmed_proposal_to_transaction(conn, poid)
        assert conv["final_transaction_created"] is True

        txn = conn.execute(
            "SELECT * FROM transactions WHERE public_id = ?",
            (conv["transaction_public_id"],),
        ).fetchone()
        assert txn["transaction_date"] == "2024-06-15"
        assert txn["merchant"] == "Golden Mile"


# ---------------------------------------------------------------------------
# Mandatory Completion ID Validation
# ---------------------------------------------------------------------------


class TestCompletionIdValidation:
    """The caller must supply a stable, documented public ID."""

    @pytest.mark.parametrize(
        "bad_id,expected",
        [
            (None, "must be a string"),
            ("", "must not be empty"),
            ("   ", "must not be empty"),
            ("pco_\tinside", "must not contain whitespace"),
            ("malformed_id", "must start with 'pco_'"),
            ("x" * 201, "must not exceed 200"),
        ],
    )
    def test_invalid_completion_id_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        bad_id: Any,
        expected: str,
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        from finance_core.parser_proposals.completion import InvalidCompletionIdError

        with pytest.raises(InvalidCompletionIdError, match=expected):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=_current_hash(conn, result["parser_output"]),
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id=bad_id,
            )


# ---------------------------------------------------------------------------
# Hash Format Validation
# ---------------------------------------------------------------------------


class TestHashFormatValidation:
    """expected_content_hash must be exactly 64 lowercase hex chars."""

    @pytest.mark.parametrize(
        "bad_hash,expected_msg",
        [
            ("A" * 64, "lowercase hex"),
            ("g" * 64, "lowercase hex"),
            ("z" * 64, "lowercase hex"),
            ("+" * 64, "lowercase hex"),
            ("!" * 64, "lowercase hex"),
            ("a" * 63 + " ", "lowercase hex"),
            ("a" * 63, "64-character"),
            ("a" * 65, "64-character"),
        ],
    )
    def test_invalid_hash_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        bad_hash: str,
        expected_msg: str,
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)

        with pytest.raises(ProposalCompletionError, match=expected_msg):
            complete_proposal(
                conn,
                result["parser_output"]["id"],
                actor="owner",
                expected_content_hash=bad_hash,
                field_updates={"transaction_date": "2024-05-31"},
                completion_public_id="pco_err",
            )


# ---------------------------------------------------------------------------
# Lifecycle Event Per Version Counts
# ---------------------------------------------------------------------------


class TestLifecycleEventCounts:
    """Every successful new completion version must have corresponding
    lifecycle and audit evidence."""

    def test_two_completions_produce_two_lifecycle_events(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_evt_v1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Eatery"},
            completion_public_id="pco_evt_v2",
        )

        # Count completed records
        completions = _count(conn, "parser_proposal_completions")
        assert completions == 2

        # Count lifecycle events for edited operations
        edited_events = conn.execute(
            "SELECT COUNT(*) AS c FROM parser_proposal_events "
            "WHERE parser_output_id = ? AND event_type = 'edited'",
            (poid,),
        ).fetchone()["c"]
        assert edited_events == 2

        # Count financial audit events for completion
        audit_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM financial_audit_events "
            "WHERE aggregate_public_id = ? AND event_type = 'parser_proposal_completed'",
            (result["parser_output"]["public_id"],),
        ).fetchone()["c"]
        assert audit_rows == 2

    def test_lifecycle_events_bind_version_and_hash(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        _v = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_evtbind_v1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        v2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Binder"},
            completion_public_id="pco_evtbind_v2",
        )

        # Check each event's payload contains the right completion binding
        events = conn.execute(
            "SELECT event_payload FROM parser_proposal_events "
            "WHERE parser_output_id = ? AND event_type = 'edited' "
            "ORDER BY id",
            (poid,),
        ).fetchall()
        assert len(events) == 2

        p1 = json.loads(events[0]["event_payload"])
        assert p1["completion_public_id"] == "pco_evtbind_v1"
        assert p1["version_number"] == 1
        assert p1["previous_content_hash"] == h
        assert p1["new_content_hash"] == _v["completed_content_hash"]

        p2 = json.loads(events[1]["event_payload"])
        assert p2["completion_public_id"] == "pco_evtbind_v2"
        assert p2["version_number"] == 2
        assert p2["previous_content_hash"] == h2
        assert p2["new_content_hash"] == v2["completed_content_hash"]

    def test_complete_complete_confirm_convert_produces_ordered_chain(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_chain_v1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Chain Shop"},
            completion_public_id="pco_chain_v2",
        )

        confirm_proposal(conn, poid, actor="owner", confirmation_public_id="pca_chain")
        convert_confirmed_proposal_to_transaction(conn, poid)

        # Verify audit chain order
        audit_events = conn.execute(
            "SELECT event_type FROM financial_audit_events "
            "WHERE aggregate_public_id = ? "
            "ORDER BY sequence_number",
            (result["parser_output"]["public_id"],),
        ).fetchall()
        event_types = [e["event_type"] for e in audit_events]
        assert event_types == [
            "parser_proposal_completed",
            "parser_proposal_completed",
            "parser_proposal_confirmed",
            "parser_proposal_converted",
        ]

    def test_same_status_edit_still_creates_event(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_same_status_1",
        )
        # Both from and to status are edited_pending_confirmation
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Same Status Shop"},
            completion_public_id="pco_same_status_2",
        )

        edited_events = conn.execute(
            "SELECT COUNT(*) AS c FROM parser_proposal_events "
            "WHERE parser_output_id = ? AND event_type = 'edited'",
            (poid,),
        ).fetchone()["c"]
        assert edited_events == 2

    def test_idempotent_replay_no_duplicate_events(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        r1 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_replay_events",
        )
        assert r1["idempotent"] is False

        edited_before = _count_edited_events(conn, poid)
        audit_before = _count_audit_events(conn, result["parser_output"]["public_id"])

        r2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-05-31"},
            completion_public_id="pco_replay_events",
        )
        assert r2["idempotent"] is True

        edited_after = _count_edited_events(conn, poid)
        audit_after = _count_audit_events(conn, result["parser_output"]["public_id"])
        assert edited_after == edited_before
        assert audit_after == audit_before


# ---------------------------------------------------------------------------
# Historical Replay — v1 replay after later completions must be idempotent
# ---------------------------------------------------------------------------


class TestHistoricalCompletionReplay:
    """Historical same-ID replay must remain durably idempotent even after
    later completion versions exist.  The original completion must never be
    reconstructed against the latest (post-v2) effective payload."""

    def test_v1_replay_immediately_after_v1_is_idempotent(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        v1 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_v1",
        )
        assert v1["idempotent"] is False
        assert v1["version_number"] == 1

        # Immediate replay of v1 command
        replay = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_v1",
        )
        assert replay["idempotent"] is True
        assert replay["version_number"] == 1
        assert replay["completed_content_hash"] == v1["completed_content_hash"]

    def test_v1_replay_after_v2_returns_original_v1_result(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn, result["parser_output"])

        # v1: add transaction_date
        v1 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_v1b",
        )
        assert v1["version_number"] == 1

        # v2: add merchant — now the effective payload includes transaction_date + merchant
        v1_hash = _current_hash(conn, result["parser_output"])
        v2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=v1_hash,
            field_updates={"merchant": "Historic Shop"},
            completion_public_id="pco_hist_v2b",
        )
        assert v2["version_number"] == 2

        # Replay original v1 command — must NOT use v2's effective payload
        replay = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_v1b",
        )
        assert replay["idempotent"] is True
        assert replay["version_number"] == 1
        # The completed hash must match the original v1 completed hash,
        # NOT v2's (which would include merchant)
        assert replay["completed_content_hash"] == v1["completed_content_hash"]
        assert replay["completed_content_hash"] != v2["completed_content_hash"]

        # No new completion rows, events, or audit rows
        assert _count(conn, "parser_proposal_completions") == 2
        assert _count_edited_events(conn, poid) == 2
        # 2 audit events: one for each distinct completion version
        audit_total = conn.execute(
            "SELECT COUNT(*) AS c FROM financial_audit_events "
            "WHERE event_type = 'parser_proposal_completed'",
        ).fetchone()["c"]
        assert audit_total == 2

    def test_v1_replay_after_v3_returns_original_v1_result(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn, result["parser_output"])

        v1 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_v1c",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Triple Shop"},
            completion_public_id="pco_hist_v2c",
        )
        h3 = _current_hash(conn, result["parser_output"])
        v3 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h3,
            field_updates={"category": "Dining"},
            completion_public_id="pco_hist_v3c",
        )

        # Replay v1 after v3 exists
        replay = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_v1c",
        )
        assert replay["idempotent"] is True
        assert replay["version_number"] == 1
        assert replay["completed_content_hash"] == v1["completed_content_hash"]
        assert replay["completed_content_hash"] != v3["completed_content_hash"]

        assert _count(conn, "parser_proposal_completions") == 3

    def test_changed_historical_request_conflicts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_replay",
        )

        # Different date under same completion ID
        with pytest.raises(CompletionConflictError):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-16"},
                completion_public_id="pco_hist_replay",
            )

    def test_changed_historical_actor_conflicts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_actor",
        )

        # Different actor under same completion ID
        with pytest.raises(CompletionConflictError):
            complete_proposal(
                conn,
                poid,
                actor="other",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_hist_actor",
            )

    def test_changed_historical_channel_conflicts(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_hist_chan",
            completion_channel="cli",
        )

        with pytest.raises(CompletionConflictError):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_hist_chan",
                completion_channel="web",
            )

    # ---------------------------------------------------------------------------
    # Separate-Connection Concurrency
    # ---------------------------------------------------------------------------

    def test_v2_replay_after_v3_returns_original_v2_result(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Replay of v2 command after v3 must return persisted v2 result."""
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn, result["parser_output"])

        v1 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_v2r_v1",
        )
        v1_hash = _current_hash(conn, result["parser_output"])

        v2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=v1_hash,
            field_updates={"merchant": "Shop V2"},
            completion_public_id="pco_v2r_v2",
        )
        v2_hash = _current_hash(conn, result["parser_output"])

        # v3 adds category
        v3 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=v2_hash,
            field_updates={"category": "Dining"},
            completion_public_id="pco_v2r_v3",
        )

        # Replay v2 after v3
        replay = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=v1_hash,
            field_updates={"merchant": "Shop V2"},
            completion_public_id="pco_v2r_v2",
        )
        assert replay["idempotent"] is True
        assert replay["version_number"] == v2["version_number"]
        assert replay["completed_content_hash"] == v2["completed_content_hash"]
        # v2's completed hash must include transaction_date from v1
        assert replay["completed_content_hash"] != v1["completed_content_hash"]
        assert replay["completed_content_hash"] != v3["completed_content_hash"]

        # No duplicate rows
        assert _count(conn, "parser_proposal_completions") == 3
        assert _count_edited_events(conn, poid) == 3
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM financial_audit_events "
                "WHERE event_type = 'parser_proposal_completed'",
            ).fetchone()["c"]
            == 3
        )

    def test_v2_replay_after_v4_returns_original_v2_result(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_v4r_v1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        v2 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "V4 Shop"},
            completion_public_id="pco_v4r_v2",
        )
        h3 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h3,
            field_updates={"category": "Dining"},
            completion_public_id="pco_v4r_v3",
        )
        h4 = _current_hash(conn, result["parser_output"])
        v4 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h4,
            field_updates={"description": "Team lunch"},
            completion_public_id="pco_v4r_v4",
        )

        # Replay v2 after v4
        replay = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "V4 Shop"},
            completion_public_id="pco_v4r_v2",
        )
        assert replay["idempotent"] is True
        assert replay["version_number"] == v2["version_number"]
        assert replay["completed_content_hash"] == v2["completed_content_hash"]
        assert replay["completed_content_hash"] != v4["completed_content_hash"]

        assert _count(conn, "parser_proposal_completions") == 4
        assert _count_edited_events(conn, poid) == 4

    def test_v3_replay_after_v4_returns_original_v3_result(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn, result["parser_output"])

        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_v34r_v1",
        )
        h2 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h2,
            field_updates={"merchant": "Shop V3R"},
            completion_public_id="pco_v34r_v2",
        )
        h3 = _current_hash(conn, result["parser_output"])
        v3 = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h3,
            field_updates={"category": "Dining"},
            completion_public_id="pco_v34r_v3",
        )
        h4 = _current_hash(conn, result["parser_output"])
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h4,
            field_updates={"description": "Dinner out"},
            completion_public_id="pco_v34r_v4",
        )

        # Replay v3 after v4
        replay = complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h3,
            field_updates={"category": "Dining"},
            completion_public_id="pco_v34r_v3",
        )
        assert replay["idempotent"] is True
        assert replay["version_number"] == v3["version_number"]
        assert replay["completed_content_hash"] == v3["completed_content_hash"]
        # v3's completed hash includes transaction_date + merchant + category
        assert replay["completed_content_hash"] != _current_hash(conn, result["parser_output"])

        assert _count(conn, "parser_proposal_completions") == 4
        assert _count_edited_events(conn, poid) == 4

    def test_non_v1_restart_replay_is_idempotent(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Replay v2 after closing and reopening the database."""
        db_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()[2]

        conn1 = sqlite3.connect(db_path)
        conn1.row_factory = sqlite3.Row
        conn1.execute("PRAGMA foreign_keys = ON")
        result = _intake_and_proposal(conn1)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn1, result["parser_output"])

        complete_proposal(
            conn1,
            poid,
            actor="owner",
            expected_content_hash=original_hash,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_restart_v2_v1",
        )
        v1_hash = _current_hash(conn1, result["parser_output"])
        r1 = complete_proposal(
            conn1,
            poid,
            actor="owner",
            expected_content_hash=v1_hash,
            field_updates={"merchant": "Reopen Shop"},
            completion_public_id="pco_restart_v2_v2",
        )
        conn1.close()

        # Replay v2 from a new connection
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA foreign_keys = ON")
        r2 = complete_proposal(
            conn2,
            poid,
            actor="owner",
            expected_content_hash=v1_hash,
            field_updates={"merchant": "Reopen Shop"},
            completion_public_id="pco_restart_v2_v2",
        )
        try:
            assert r2["idempotent"] is True
            assert r2["version_number"] == r1["version_number"]
            assert r2["completed_content_hash"] == r1["completed_content_hash"]
        finally:
            conn2.close()


class TestConcurrency:
    """Separate SQLite connections to the same temp database file.

    The proposal is prepared **once** before threads start.  Each worker
    opens its own connection, sets a busy timeout, and calls
    ``complete_proposal`` with the same parser-output id + expected hash.
    """

    @staticmethod
    def _concurrent_write(
        db_path: str,
        parser_output_id: int,
        expected_content_hash: str,
        completion_public_id: str,
        actor: str,
        field_updates: dict[str, Any],
        result_list: list[dict[str, Any]],
        barrier: Any,
    ) -> None:
        """Open a new connection, set busy timeout, then call ``complete_proposal``
        after the barrier is released.  The caller must have already created
        the proposal and computed the expected hash."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            barrier.wait()
            try:
                r = complete_proposal(
                    conn,
                    parser_output_id,
                    actor=actor,
                    expected_content_hash=expected_content_hash,
                    field_updates=field_updates,
                    completion_public_id=completion_public_id,
                )
                result_list.append({"ok": True, "result": r})
            except Exception as exc:
                result_list.append({"ok": False, "error": str(exc), "type": type(exc).__name__})
        finally:
            conn.close()

    # -- Same key + same material --------------------------------------------

    def test_same_key_same_material_one_winner(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Exactly 2 successful calls: 1 non-idempotent + 1 idempotent."""
        import threading

        db_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()[2]

        # Prepare the proposal once
        conn0 = sqlite3.connect(db_path)
        conn0.row_factory = sqlite3.Row
        conn0.execute("PRAGMA foreign_keys = ON")
        result = _intake_and_proposal(conn0)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn0, result["parser_output"])
        conn0.close()

        barrier = threading.Barrier(2, timeout=30)
        results: list[dict[str, Any]] = []

        args = (db_path, poid, h, "pco_conc_same", "owner", {"transaction_date": "2024-06-01"})
        t1 = threading.Thread(target=self._concurrent_write, args=(*args, results, barrier))
        t2 = threading.Thread(target=self._concurrent_write, args=(*args, results, barrier))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive()
        assert not t2.is_alive()

        assert len(results) == 2
        ok_results = [r for r in results if r["ok"]]
        assert len(ok_results) == 2, f"Expected 2 successes, got: {results}"

        non_idem = [r for r in ok_results if r["result"]["idempotent"] is False]
        idem = [r for r in ok_results if r["result"]["idempotent"] is True]
        assert len(non_idem) == 1, f"Expected 1 non-idempotent, got: {results}"
        assert len(idem) == 1, f"Expected 1 idempotent, got: {results}"
        assert non_idem[0]["result"]["version_number"] == 1
        assert (
            non_idem[0]["result"]["completed_content_hash"]
            == idem[0]["result"]["completed_content_hash"]
        )
        assert non_idem[0]["result"]["completed_content_hash"] != h  # material change

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

            assert _count(conn, "parser_proposal_completions") == 1
            events = conn.execute(
                "SELECT COUNT(*) AS c FROM parser_proposal_events WHERE event_type = 'edited'"
            ).fetchone()["c"]
            assert events == 1, f"Expected 1 edited event, got {events}"
            audit = conn.execute(
                "SELECT COUNT(*) AS c FROM financial_audit_events "
                "WHERE event_type = 'parser_proposal_completed'"
            ).fetchone()["c"]
            assert audit == 1, f"Expected 1 audit event, got {audit}"
        finally:
            conn.close()

    # -- Same key + different material ---------------------------------------

    def test_same_key_different_material_one_conflict(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Exactly 1 success + 1 CompletionConflictError."""
        import threading

        db_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()[2]

        conn0 = sqlite3.connect(db_path)
        conn0.row_factory = sqlite3.Row
        conn0.execute("PRAGMA foreign_keys = ON")
        result = _intake_and_proposal(conn0)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn0, result["parser_output"])
        conn0.close()

        barrier = threading.Barrier(2, timeout=30)
        results: list[dict[str, Any]] = []

        t1 = threading.Thread(
            target=self._concurrent_write,
            args=(
                db_path,
                poid,
                h,
                "pco_conc_diff",
                "owner",
                {"transaction_date": "2024-06-01"},
                results,
                barrier,
            ),
        )
        t2 = threading.Thread(
            target=self._concurrent_write,
            args=(
                db_path,
                poid,
                h,
                "pco_conc_diff",
                "owner",
                {"transaction_date": "2024-06-02"},
                results,
                barrier,
            ),
        )
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive()
        assert not t2.is_alive()

        assert len(results) == 2

        ok_count = sum(1 for r in results if r["ok"])
        conflict_count = sum(
            1 for r in results if not r["ok"] and "CompletionConflictError" in r.get("type", "")
        )
        assert ok_count == 1, f"Expected exactly 1 success, got: {results}"
        assert conflict_count == 1, f"Expected exactly 1 CompletionConflictError, got: {results}"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

            assert _count(conn, "parser_proposal_completions") == 1
            events = conn.execute(
                "SELECT COUNT(*) AS c FROM parser_proposal_events WHERE event_type = 'edited'"
            ).fetchone()["c"]
            assert events == 1
            audit = conn.execute(
                "SELECT COUNT(*) AS c FROM financial_audit_events "
                "WHERE event_type = 'parser_proposal_completed'"
            ).fetchone()["c"]
            assert audit == 1
        finally:
            conn.close()

    # -- Different keys + same stale base hash --------------------------------

    def test_different_keys_same_stale_hash(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Exactly 1 success + 1 StaleProposalContentError (not Conflict)."""
        import threading

        db_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()[2]

        conn0 = sqlite3.connect(db_path)
        conn0.row_factory = sqlite3.Row
        conn0.execute("PRAGMA foreign_keys = ON")
        result = _intake_and_proposal(conn0)
        poid = result["parser_output"]["id"]
        original_hash = _current_hash(conn0, result["parser_output"])
        conn0.close()

        barrier = threading.Barrier(2, timeout=30)
        results: list[dict[str, Any]] = []

        t1 = threading.Thread(
            target=self._concurrent_write,
            args=(
                db_path,
                poid,
                original_hash,
                "pco_stale_a",
                "owner",
                {"transaction_date": "2024-07-01"},
                results,
                barrier,
            ),
        )
        t2 = threading.Thread(
            target=self._concurrent_write,
            args=(
                db_path,
                poid,
                original_hash,
                "pco_stale_b",
                "owner",
                {"transaction_date": "2024-07-02"},
                results,
                barrier,
            ),
        )
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive()
        assert not t2.is_alive()

        assert len(results) == 2

        ok_count = sum(1 for r in results if r["ok"])
        stale_count = sum(
            1 for r in results if not r["ok"] and "StaleProposalContentError" in r.get("type", "")
        )
        conflict_count = sum(
            1 for r in results if not r["ok"] and "CompletionConflictError" in r.get("type", "")
        )
        assert ok_count == 1, f"Expected exactly 1 winner, got: {results}"
        assert stale_count == 1, f"Expected exactly 1 StaleProposalContentError, got: {results}"
        assert conflict_count == 0, (
            f"Expected 0 CompletionConflictError (IDs are different), got: {results}"
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

            assert _count(conn, "parser_proposal_completions") == 1
            events = conn.execute(
                "SELECT COUNT(*) AS c FROM parser_proposal_events WHERE event_type = 'edited'"
            ).fetchone()["c"]
            assert events == 1
            audit = conn.execute(
                "SELECT COUNT(*) AS c FROM financial_audit_events "
                "WHERE event_type = 'parser_proposal_completed'"
            ).fetchone()["c"]
            assert audit == 1
        finally:
            conn.close()

    # -- Restart replay ------------------------------------------------------

    def test_restart_replay(self, migrated_temp_db_connection: sqlite3.Connection) -> None:
        """Close and reopen the database, then replay the original command."""
        db_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()[2]

        conn1 = sqlite3.connect(db_path)
        conn1.row_factory = sqlite3.Row
        conn1.execute("PRAGMA foreign_keys = ON")
        result = _intake_and_proposal(conn1)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn1, result["parser_output"])
        r1 = complete_proposal(
            conn1,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_restart",
        )
        conn1.close()

        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA foreign_keys = ON")
        r2 = complete_proposal(
            conn2,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_restart",
        )
        try:
            assert r2["idempotent"] is True
            assert r2["version_number"] == r1["version_number"]
            assert r2["completed_content_hash"] == r1["completed_content_hash"]
            assert _count(conn2, "parser_proposal_completions") == 1
        finally:
            conn2.close()


# ---------------------------------------------------------------------------
# Atomic Rollback — Failure Injection
# ---------------------------------------------------------------------------


class TestAtomicRollback:
    """Prove complete rollback when a write failure is injected at each
    point in the completion transaction."""

    @pytest.fixture
    def _prepare_proposal(self, migrated_temp_db_connection: sqlite3.Connection):
        """Return (conn, parser_output_id, expected_hash) for a fresh proposal."""
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        return conn, poid, h

    def test_rollback_on_completion_insert_failure(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Inject a failure in the completion row insert via a temporary
        trigger that raises on the specific INSERT."""
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        initial_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]

        completed_before = _count(conn, "parser_proposal_completions")
        events_before = _count_edited_events(conn, poid)

        # Install a trigger that fails on completion insert
        conn.execute("""
            CREATE TEMP TRIGGER _test_fail_completion_insert
            BEFORE INSERT ON parser_proposal_completions
            BEGIN
                SELECT RAISE(FAIL, 'injected completion insert failure');
            END
        """)

        with pytest.raises(
            (sqlite3.OperationalError, sqlite3.IntegrityError, ProposalCompletionError)
        ):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_atomic_ins",
            )

        # Clean up the trigger
        conn.execute("DROP TRIGGER _test_fail_completion_insert")

        # Verify full rollback
        assert _count(conn, "parser_proposal_completions") == completed_before
        assert _count_edited_events(conn, poid) == events_before
        # Audit events may have been cleaned by rollback
        current_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]
        assert current_status == initial_status
        # Connection is usable
        conn.execute("SELECT 1")

    def test_rollback_on_lifecycle_event_insert_failure(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        initial_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]

        completed_before = _count(conn, "parser_proposal_completions")
        events_before = _count_edited_events(conn, poid)

        # Fail on lifecycle event insert (parser_proposal_events)
        conn.execute("""
            CREATE TEMP TRIGGER _test_fail_event_insert
            BEFORE INSERT ON parser_proposal_events
            WHEN NEW.event_type = 'edited'
            BEGIN
                SELECT RAISE(FAIL, 'injected lifecycle event insert failure');
            END
        """)

        with pytest.raises(
            (sqlite3.OperationalError, sqlite3.IntegrityError, ProposalCompletionError)
        ):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_atomic_evt",
            )

        conn.execute("DROP TRIGGER _test_fail_event_insert")

        assert _count(conn, "parser_proposal_completions") == completed_before
        assert _count_edited_events(conn, poid) == events_before
        current_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]
        assert current_status == initial_status

    def test_rollback_on_status_update_failure(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        initial_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]

        completed_before = _count(conn, "parser_proposal_completions")

        # Fail on parser_outputs UPDATE (status update)
        conn.execute("""
            CREATE TEMP TRIGGER _test_fail_status_update
            BEFORE UPDATE OF parse_status ON parser_outputs
            BEGIN
                SELECT RAISE(FAIL, 'injected status update failure');
            END
        """)

        with pytest.raises(
            (sqlite3.OperationalError, sqlite3.IntegrityError, ProposalCompletionError)
        ):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_atomic_st",
            )

        conn.execute("DROP TRIGGER _test_fail_status_update")

        assert _count(conn, "parser_proposal_completions") == completed_before
        current_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]
        assert current_status == initial_status

    def test_rollback_on_raw_intake_status_update_failure(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """Inject a failure in the raw-intake status UPDATE via a temporary
        trigger.  After rollback, verify no partial writes and the
        raw-intake status is unchanged."""
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        initial_parser_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]
        initial_raw_intake_status = conn.execute(
            "SELECT ri.status FROM raw_intake_records ri WHERE ri.parser_output_id = ?",
            (poid,),
        ).fetchone()["status"]

        completed_before = _count(conn, "parser_proposal_completions")
        events_before = _count_edited_events(conn, poid)

        conn.execute("""
            CREATE TEMP TRIGGER _test_fail_raw_intake_update
            BEFORE UPDATE OF status ON raw_intake_records
            BEGIN
                SELECT RAISE(FAIL, 'injected raw-intake status update failure');
            END
        """)

        with pytest.raises(
            (sqlite3.OperationalError, sqlite3.IntegrityError, ProposalCompletionError)
        ):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_atomic_ri",
            )

        conn.execute("DROP TRIGGER _test_fail_raw_intake_update")

        assert _count(conn, "parser_proposal_completions") == completed_before
        assert _count_edited_events(conn, poid) == events_before
        current_parser_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]
        assert current_parser_status == initial_parser_status
        current_raw_intake_status = conn.execute(
            "SELECT ri.status FROM raw_intake_records ri WHERE ri.parser_output_id = ?",
            (poid,),
        ).fetchone()["status"]
        assert current_raw_intake_status == initial_raw_intake_status

    def test_rollback_on_audit_append_failure(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])
        initial_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]

        completed_before = _count(conn, "parser_proposal_completions")
        events_before = _count_edited_events(conn, poid)

        # Fail on financial audit event insert
        conn.execute("""
            CREATE TEMP TRIGGER _test_fail_audit_insert
            BEFORE INSERT ON financial_audit_events
            WHEN NEW.event_type = 'parser_proposal_completed'
            BEGIN
                SELECT RAISE(FAIL, 'injected audit event insert failure');
            END
        """)

        with pytest.raises(
            (sqlite3.OperationalError, sqlite3.IntegrityError, ProposalCompletionError)
        ):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_atomic_au",
            )

        conn.execute("DROP TRIGGER _test_fail_audit_insert")

        assert _count(conn, "parser_proposal_completions") == completed_before
        assert _count_edited_events(conn, poid) == events_before
        current_status = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (poid,)
        ).fetchone()["parse_status"]
        assert current_status == initial_status

    def test_connection_usable_after_rollback(
        self, migrated_temp_db_connection: sqlite3.Connection
    ) -> None:
        """After a forced rollback, the connection must still be usable
        for normal operations."""
        conn = migrated_temp_db_connection
        result = _intake_and_proposal(conn)
        poid = result["parser_output"]["id"]
        h = _current_hash(conn, result["parser_output"])

        conn.execute("""
            CREATE TEMP TRIGGER _test_fail_and_recover
            BEFORE INSERT ON parser_proposal_completions
            BEGIN
                SELECT RAISE(FAIL, 'injected for recovery test');
            END
        """)

        with pytest.raises(
            (sqlite3.OperationalError, sqlite3.IntegrityError, ProposalCompletionError)
        ):
            complete_proposal(
                conn,
                poid,
                actor="owner",
                expected_content_hash=h,
                field_updates={"transaction_date": "2024-06-15"},
                completion_public_id="pco_atomic_rec",
            )

        conn.execute("DROP TRIGGER _test_fail_and_recover")

        # Connection must still work
        conn.execute("SELECT 1")
        conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (poid,))

        # A fresh completion should succeed
        complete_proposal(
            conn,
            poid,
            actor="owner",
            expected_content_hash=h,
            field_updates={"transaction_date": "2024-06-15"},
            completion_public_id="pco_atomic_rec2",
        )
        assert _count(conn, "parser_proposal_completions") == 1
