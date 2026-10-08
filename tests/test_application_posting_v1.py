"""Neutral posting acceptance; only synthetic authority is used."""

from __future__ import annotations

import importlib
import importlib.util

import pytest


def test_completed_text_missing_owner_proof_refuses_status_and_resume(
    migrated_temp_db_connection,
):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    from finance_core.application.posting import PostingError

    conn = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(conn)
    posted = service.submit_post(review.review_id, decision_id)
    conn.execute("DELETE FROM parser_proposal_conversion_audit")
    conn.commit()
    before = conn.total_changes
    for operation in (service.get_status, service.resume_post):
        with pytest.raises(PostingError, match="verified canonical result"):
            operation(posted.attempt_id)
        assert conn.total_changes == before
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


@pytest.mark.parametrize("stage", ["accepted", "finalized"])
def test_text_coordination_result_contradiction_refuses_even_before_finalized(
    migrated_temp_db_connection,
    stage,
):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    from finance_core.application.posting import PostingError

    conn = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(conn)
    posted = service.submit_post(review.review_id, decision_id)
    other_service, other_review, *_rest, other_decision, _display = _prepare_text_subject(
        conn, "other"
    )
    other = other_service.submit_post(other_review.review_id, other_decision)
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='application_posting_attempts_guard_update'"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER application_posting_attempts_guard_update")
    conn.execute(
        "UPDATE application_posting_attempts SET stage=?,transaction_public_id=? "
        "WHERE attempt_id=?",
        (stage, other.transaction_public_id, posted.attempt_id),
    )
    conn.execute(trigger)
    conn.commit()
    before = conn.total_changes
    for operation in (service.get_status, service.resume_post):
        with pytest.raises(PostingError, match="verified canonical result"):
            operation(posted.attempt_id)
        assert conn.total_changes == before
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 2


def test_neutral_owner_preserves_legacy_exception_identity():
    assert importlib.util.find_spec("finance_core.parser_proposals.decision_owner") is not None, (
        "Neutral parser decision owner has not been extracted"
    )
    owner = importlib.import_module("finance_core.parser_proposals.decision_owner")
    legacy = importlib.import_module("finance_core.parser_proposals.service")
    assert owner.ParserConfirmationError is legacy.ParserConfirmationError
    assert owner.ProposalConversionError is legacy.ProposalConversionError


def test_neutral_confirmation_and_conversion_use_one_verified_human_decision(
    migrated_temp_db_connection,
):
    from finance_core.intake.raw_text_repository import (
        create_raw_intake_record,
        save_parser_proposal,
    )
    from finance_core.parsers.text_expense_parser import parse_text_expense

    assert importlib.util.find_spec("finance_core.parser_proposals.decision_owner") is not None, (
        "Neutral parser decision owner has not been extracted"
    )
    owner = importlib.import_module("finance_core.parser_proposals.decision_owner")
    conn = migrated_temp_db_connection
    raw = create_raw_intake_record(
        conn, "lunch SGD 12.50", source_type="manual_entry", source_channel="manual"
    )
    parsed = parse_text_expense(
        raw["raw_input"], raw_input_reference=raw["public_id"], source_type="manual_entry"
    )
    parsed["transaction_date"] = "2026-01-01"
    proposal = save_parser_proposal(conn, raw["id"], parsed)
    conn.execute("CREATE TABLE synthetic_owner_effect (confirmation_id TEXT PRIMARY KEY)")
    conn.commit()

    class SyntheticAuthority:
        def verify_in_transaction(
            self,
            connection,
            *,
            proposal,
            content_hash,
            proposal_version,
            authenticated_actor_id,
            decision,
            decision_epoch,
        ):
            assert connection.in_transaction
            assert authenticated_actor_id == "synthetic-human" and decision == "confirmed"
            assert proposal_version == 0 and len(content_hash) == 64

        def persist_effect_in_transaction(self, connection, *, confirmation_public_id):
            connection.execute(
                "INSERT INTO synthetic_owner_effect VALUES (?)", (confirmation_public_id,)
            )

    confirmed = owner.confirm_parser_proposal(
        conn,
        proposal["id"],
        authenticated_actor_id="synthetic-human",
        decision_authority=SyntheticAuthority(),
        confirmation_public_id="synthetic-confirmation",
    )
    assert confirmed["final_transaction_created"] is False
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert (
        conn.execute("SELECT confirmation_id FROM synthetic_owner_effect").fetchone()[0]
        == "synthetic-confirmation"
    )
    converted = owner.convert_confirmed_parser_proposal(conn, proposal["id"])
    assert converted["confirmation_id"] == "synthetic-confirmation"
    assert owner.convert_confirmed_parser_proposal(conn, proposal["id"])["idempotent"] is True
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_independent_posting_api_exists():
    assert importlib.util.find_spec("finance_core.application.posting") is not None, (
        "Independent posting composition is missing"
    )


def test_legacy_nfd_confirmation_conversion_and_replay_preserve_exact_text(
    migrated_temp_db_connection,
):
    from finance_core.intake.raw_text_repository import (
        create_raw_intake_record,
        save_parser_proposal,
    )
    from finance_core.parser_proposals import service
    from finance_core.parsers.text_expense_parser import parse_text_expense

    conn = migrated_temp_db_connection
    raw_text = "Lunch SGD 12.50 Cafe\u0301"
    raw = create_raw_intake_record(
        conn, raw_text, source_type="manual_entry", source_channel="manual"
    )
    parsed = parse_text_expense(
        raw_text, raw_input_reference=raw["public_id"], source_type="manual_entry"
    )
    parsed.update(
        {
            "transaction_date": "2026-01-01",
            "merchant": "Cafe\u0301",
            "description": "Cre\u0300me",
            "category": "Di\u0301ning",
        }
    )
    proposal = save_parser_proposal(conn, raw["id"], parsed)
    conn.commit()
    service.confirm_parser_proposal(conn, proposal["id"], authenticated_actor_id="legacy-human")
    converted = service.convert_confirmed_parser_proposal(conn, proposal["id"])
    row = conn.execute(
        "SELECT merchant,category,raw_input FROM transactions WHERE id=?",
        (converted["transaction_id"],),
    ).fetchone()
    assert tuple(row) == ("Cafe\u0301", "Di\u0301ning", raw_text)
    assert (
        service.resolve_simple_expense_conversion_fields(conn, proposal)["description"]
        == "Cre\u0300me"
    )
    assert service.convert_confirmed_parser_proposal(conn, proposal["id"])["idempotent"] is True
    assert (
        service.verify_converted_parser_proposal(conn, proposal["id"])["transaction_public_id"]
        == converted["transaction_public_id"]
    )
