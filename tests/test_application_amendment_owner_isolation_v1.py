"""Owning decisions and compatibility facades preserve amendment authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_application_amendment_smoke_v1 import SignedAmendmentPort, persist_edit
from test_application_posting_recovery_v1 import (
    BINDING,
    DECISION_KEY,
    NOW,
    _decision_material,
    _DurableSourceVerifier,
    _persist,
    _prepare_receipt_subject,
    _prepare_text_subject,
    _reply_material,
)
from test_parser_proposal_authorization_uow import _proposal

from finance_core.application.amendment import AmendmentService
from finance_core.application.amendment_contract import AmendmentBinding, AmendmentError
from finance_core.application.posting import PostingError
from finance_core.parser_proposals import decision_owner, service
from finance_core.parser_proposals.completion import complete_proposal
from finance_core.parser_proposals.confirmation import confirm_proposal
from finance_core.parser_proposals.conversion import convert_confirmed_proposal_to_transaction
from finance_core.parser_proposals.receipt_supersession import supersede_receipt_total_proposal
from finance_core.parser_proposals.repository import ParserProposalRepository


def _edited_subject(connection: sqlite3.Connection):
    posting, _review, proposal, *_rest = _prepare_text_subject(connection)
    edits = AmendmentService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_amendment_authority=SignedAmendmentPort(),
        binding=AmendmentBinding(BINDING, "synthetic-amendment", "synthetic-amendment-key"),
        clock=lambda: NOW,
    )
    review = edits.prepare(proposal["public_id"])
    persist_edit(connection, review, {"amount": "14.00"})
    result = edits.amend(review.review_id, "edit-one", "amend-one")
    proposal = ParserProposalRepository(connection).get_by_public_id(result.proposal_public_id)
    assert proposal is not None
    return posting, proposal


def test_legacy_service_conversion_retains_its_existing_signature_and_result(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    proposal_id = _proposal(connection)
    confirm_proposal(connection, proposal_id, actor="owner-authenticated")

    result = service.convert_confirmed_parser_proposal(connection, proposal_id)

    assert result["final_transaction_created"] is True
    assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def _unsealed_source_editor(connection, source_verifier):
    return AmendmentService(
        connection=connection,
        source_verifier=source_verifier,
        human_amendment_authority=SignedAmendmentPort(),
        binding=AmendmentBinding(BINDING, "synthetic-amendment", "synthetic-amendment-key"),
        clock=lambda: NOW,
    )


def test_legacy_completion_history_cannot_be_relabelled_as_independent_authority(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, review, proposal, *_rest = _prepare_text_subject(connection)
    complete_proposal(
        connection,
        proposal["id"],
        actor="synthetic-local-human",
        expected_content_hash=review.projection["effective_content_hash"],
        field_updates={"merchant": "Legacy Cafe"},
        completion_public_id="pco_legacy_unsealed_source",
    )
    assert connection.execute("SELECT COUNT(*) FROM parser_proposal_completions").fetchone()[0] == 1

    with pytest.raises(AmendmentError):
        _unsealed_source_editor(connection, _DurableSourceVerifier()).prepare(proposal["public_id"])
    with pytest.raises(PostingError):
        posting.prepare(proposal["public_id"])

    assert (
        connection.execute("SELECT COUNT(*) FROM application_amendment_reviews").fetchone()[0] == 0
    )
    assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_legacy_receipt_child_at_version_zero_cannot_gain_independent_edit_authority(
    tmp_path: Path,
) -> None:
    (
        connection,
        _workspace,
        _manifest,
        posting,
        review,
        proposal,
        _intake,
        _decision,
        _display,
        source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, suffix="legacy-receipt-child")
    try:
        parent = ParserProposalRepository(connection).get_by_public_id(proposal)
        assert parent is not None
        correction = supersede_receipt_total_proposal(
            connection,
            parent["id"],
            actor="synthetic-local-human",
            expected_content_hash=review.projection["effective_content_hash"],
            field_updates={"amount": "15.00"},
            correction_public_id="rcor_legacy_unsealed_source",
        )
        child_id = correction["replacement_proposal_public_id"]
        assert (
            connection.execute("SELECT COUNT(*) FROM receipt_proposal_revisions").fetchone()[0] == 1
        )

        with pytest.raises(AmendmentError):
            _unsealed_source_editor(connection, source_verifier).prepare(child_id)
        with pytest.raises(PostingError):
            posting.prepare(child_id)

        assert (
            connection.execute("SELECT COUNT(*) FROM application_amendment_reviews").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        connection.close()


class _AuthorityWithoutAcceptedPosting:
    """An actor/channel claim is not an accepted independent posting record."""

    def verify_in_transaction(self, connection, **_arguments):
        assert connection.in_transaction

    def persist_effect_in_transaction(self, connection, **_arguments):
        assert connection.in_transaction


def test_neutral_decision_owner_refuses_amended_target_without_accepted_posting(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    _posting, proposal = _edited_subject(connection)

    with pytest.raises(decision_owner.ParserConfirmationError):
        decision_owner.confirm_parser_proposal(
            connection,
            proposal["id"],
            authenticated_actor_id=BINDING.human_principal_id,
            confirmation_channel="independent_application",
            confirmation_public_id="pca_unbacked_independent_claim",
            decision_authority=_AuthorityWithoutAcceptedPosting(),
        )

    assert (
        connection.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0] == 0
    )
    assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert ParserProposalRepository(connection).get(proposal["id"])["parse_status"] == (
        "parsed_pending_confirmation"
    )


@pytest.mark.parametrize(
    "converter",
    [service.convert_confirmed_parser_proposal, convert_confirmed_proposal_to_transaction],
)
def test_legacy_converter_facades_refuse_independently_amended_posting_replay(
    migrated_temp_db_connection: sqlite3.Connection,
    converter,
) -> None:
    connection = migrated_temp_db_connection
    posting, proposal = _edited_subject(connection)
    review = posting.prepare(proposal["public_id"])
    display, decision = _decision_material(review, proposal["public_id"], "fresh-owner-proof")
    _persist(connection, "synthetic_displays", decision["display_id"], display, DECISION_KEY)
    _persist(
        connection,
        "synthetic_replies",
        decision["reply_id"],
        _reply_material(decision),
        DECISION_KEY,
    )
    _persist(connection, "synthetic_decisions", "fresh-owner-proof", decision, DECISION_KEY)
    connection.commit()
    posted = posting.submit_post(review.review_id, "fresh-owner-proof")
    assert posted.state == "finalized"

    with pytest.raises(decision_owner.ParserConfirmationError):
        converter(connection, proposal["id"])

    assert (
        posting.get_status(posted.attempt_id).transaction_public_id == posted.transaction_public_id
    )
    assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
