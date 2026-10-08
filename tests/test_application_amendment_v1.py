"""Independent acceptance of pre-confirmation Application amendments."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from queue import Queue

import pytest
from application_amendment_support_v1 import (
    AMENDMENT_KEY,
    DurableSyntheticAmendmentAuthority,
    persist_signed_amendment,
    read_signed_material,
    replace_signed_material,
    tamper_signed_material,
)
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

from finance_core.application.amendment import AmendmentService
from finance_core.application.amendment_contract import (
    AmendmentBinding,
    AmendmentError,
)

_EDIT_BINDING = AmendmentBinding(
    BINDING,
    "synthetic-amendment-authority",
    "synthetic-amendment-key-v1",
)
_ORIGINAL = {
    "amount": "12.50",
    "currency": "SGD",
    "transaction_date": "2026-10-08",
    "merchant": "Example Cafe",
    "description": "Lunch",
    "category": "food",
}
_NO_POSTING_FACTS = (
    "application_posting_decisions",
    "application_posting_attempts",
    "parser_proposal_authorizations",
    "transactions",
    "receipts",
    "receipt_item_allocation_fact_sets",
    "authoritative_calculation_snapshots",
    "receipt_finalization_authorizations",
)


def _amendment_service(connection, *, source_verifier=None, clock=lambda: NOW):
    return AmendmentService(
        connection=connection,
        source_verifier=source_verifier or _DurableSourceVerifier(),
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=clock,
    )


def _read_effective_values(
    connection: sqlite3.Connection, proposal_public_id: str
) -> dict[str, object]:
    from finance_core.application.review import get_proposal_review

    review = get_proposal_review(connection, proposal_public_id)
    return {field: review[field] for field in _ORIGINAL}


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _assert_no_posting_facts(connection: sqlite3.Connection) -> None:
    for table in _NO_POSTING_FACTS:
        assert _count(connection, table) == 0, table


def _new_evidence(connection, review, patch, *, evidence_id="amendment-evidence-one", **kwargs):
    return persist_signed_amendment(
        connection,
        review,
        patch,
        evidence_id=evidence_id,
        amendment_binding=_EDIT_BINDING,
        **kwargs,
    )


def _apply(
    connection,
    service,
    review,
    patch,
    *,
    evidence_id="amendment-evidence-one",
    amendment_id="amendment-one",
    **kwargs,
):
    _new_evidence(connection, review, patch, evidence_id=evidence_id, **kwargs)
    return service.amend(review.review_id, evidence_id, amendment_id)


def _persist_fresh_posting_confirmation(connection, review, proposal_public_id, decision_id):
    display, decision = _decision_material(review, proposal_public_id, decision_id)
    _persist(connection, "synthetic_displays", str(decision["display_id"]), display, DECISION_KEY)
    _persist(
        connection,
        "synthetic_replies",
        str(decision["reply_id"]),
        _reply_material(decision),
        DECISION_KEY,
    )
    _persist(connection, "synthetic_decisions", decision_id, decision, DECISION_KEY)
    connection.commit()


def _prepare_edited_posting(posting, result, connection, decision_id):
    fresh = posting.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(connection, fresh, result.proposal_public_id, decision_id)
    return fresh


def _make_text_proposal_incomplete(connection: sqlite3.Connection, proposal_public_id: str) -> None:
    """Build a synthetic parser result with unknown total/currency, before review."""
    row = connection.execute(
        "SELECT id,parsed_payload,normalized_payload FROM parser_outputs WHERE public_id=?",
        (proposal_public_id,),
    ).fetchone()
    assert row is not None
    parsed = json.loads(row["parsed_payload"])
    normalized = json.loads(row["normalized_payload"])
    parsed["amount"] = None
    parsed["currency"] = None
    normalized["amount"] = None
    normalized["currency"] = None
    connection.execute(
        "UPDATE parser_outputs SET parsed_payload=?,normalized_payload=? WHERE id=?",
        (
            json.dumps(parsed, sort_keys=True, separators=(",", ":")),
            json.dumps(normalized, sort_keys=True, separators=(",", ":")),
            row["id"],
        ),
    )
    connection.commit()


def test_prepare_binds_full_old_six_values_without_creating_authority_or_facts(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="amendment-prepare"
    )
    service = _amendment_service(connection)

    review = service.prepare(proposal["public_id"])

    assert set(review.projection["editable_values"]) == set(_ORIGINAL)
    assert review.projection["editable_values"] == _ORIGINAL
    assert review.projection["proposal_review"]["proposal_public_id"] == proposal["public_id"]
    assert review.projection["proposal_review"]["proposal_version"] == 0
    assert review.expires_at > NOW
    assert _count(connection, "application_amendment_reviews") == 1
    assert _count(connection, "application_amendment_records") == 0
    _assert_no_posting_facts(connection)
    raw = connection.execute(
        "SELECT raw_input FROM raw_intake_records WHERE public_id=?", (intake["public_id"],)
    ).fetchone()
    assert raw is not None and raw[0] == "Lunch SGD 12.50 at Example Cafe"
    assert posting is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "13.75"),
        ("currency", "USD"),
        ("transaction_date", "2026-10-07"),
        ("merchant", "Harbour Cafe"),
        ("description", "Lunch with Mei"),
        ("category", "transport"),
    ],
)
def test_each_text_editable_field_publishes_exactly_and_remains_unconfirmed(
    migrated_temp_db_connection: sqlite3.Connection,
    field: str,
    value: str,
) -> None:
    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"amendment-field-{field}"
    )
    service = _amendment_service(connection)
    review = service.prepare(proposal["public_id"])
    result = _apply(connection, service, review, {field: value})

    assert _read_effective_values(connection, result.proposal_public_id) == {
        **_ORIGINAL,
        field: value,
    }
    assert result.is_current is True
    assert _count(connection, "application_amendment_records") == 1
    assert _count(connection, "parser_proposal_authorizations") == 0
    assert _count(connection, "transactions") == 0
    assert _count(connection, "application_posting_decisions") == 0
    assert _count(connection, "parser_proposal_conversion_audit") == 0
    raw = connection.execute(
        "SELECT raw_input FROM raw_intake_records WHERE public_id=?", (intake["public_id"],)
    ).fetchone()
    assert raw is not None and raw[0] == "Lunch SGD 12.50 at Example Cafe"
    assert _count(connection, "application_amendment_reviews") == 1


def test_mixed_text_patch_requires_fresh_review_and_confirmation_and_saves_all_six_fields(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    posting, old_review, proposal, intake, old_decision, _old_display = _prepare_text_subject(
        connection, suffix="amendment-mixed-all-fields"
    )
    service = _amendment_service(connection)
    amendment_review = service.prepare(proposal["public_id"])
    patch = {
        "amount": "18.25",
        "currency": "USD",
        "transaction_date": "2026-10-06",
        "merchant": "Harbour Cafe",
        "description": "Dinner after work",
        "category": "dining",
    }
    result = _apply(connection, service, amendment_review, patch)

    assert _read_effective_values(connection, result.proposal_public_id) == patch
    _assert_no_posting_facts(connection)
    assert _count(connection, "application_posting_events") == 0
    with pytest.raises(PostingError):
        posting.submit_post(old_review.review_id, old_decision)
    _assert_no_posting_facts(connection)

    fresh = _prepare_edited_posting(posting, result, connection, "fresh-after-amendment")
    assert fresh.projection["financial_projection"]["amount"] == "18.25"
    posted = posting.submit_post(fresh.review_id, "fresh-after-amendment")
    assert posted.state == "finalized"
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "transactions") == 1
    transaction = connection.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category "
        "FROM transactions WHERE public_id=?",
        (posted.transaction_public_id,),
    ).fetchone()
    assert transaction is not None
    assert tuple(str(transaction[field]) for field in transaction.keys()) == (
        "18.25",
        "USD",
        "2026-10-06",
        "Harbour Cafe",
        "Dinner after work",
        "dining",
    )
    source = _DurableSourceVerifier().verify_persisted(connection, intake["public_id"])
    assert source.source_event_id == "synthetic-event-amendment-mixed-all-fields"
    assert _count(connection, "application_amendment_records") == 1
    with pytest.raises(AmendmentError):
        service.prepare(result.proposal_public_id)
    assert (
        service.amend(amendment_review.review_id, "amendment-evidence-one", "amendment-one")
        == result
    )


def test_text_chained_edits_are_single_leaf_and_historical_replay_never_moves_pointer_back(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="amendment-chain"
    )
    clock_now = [NOW]
    service = _amendment_service(connection, clock=lambda: clock_now[0])
    first_review = service.prepare(proposal["public_id"])
    first = _apply(
        connection,
        service,
        first_review,
        {"description": "Coffee with Jo"},
        evidence_id="chain-edit-one",
        amendment_id="chain-amendment-one",
    )
    assert first.proposal_public_id == proposal["public_id"]
    assert first.proposal_version == 1

    second_review = service.prepare(first.proposal_public_id)
    second = _apply(
        connection,
        service,
        second_review,
        {"amount": "13.50"},
        evidence_id="chain-edit-two",
        amendment_id="chain-amendment-two",
    )
    assert second.proposal_public_id != first.proposal_public_id
    assert _read_effective_values(connection, second.proposal_public_id) == {
        **_ORIGINAL,
        "amount": "13.50",
        "description": "Coffee with Jo",
    }

    third_review = service.prepare(second.proposal_public_id)
    third = _apply(
        connection,
        service,
        third_review,
        {"category": "coffee"},
        evidence_id="chain-edit-three",
        amendment_id="chain-amendment-three",
    )
    assert third.proposal_public_id == second.proposal_public_id
    assert _read_effective_values(connection, third.proposal_public_id) == {
        **_ORIGINAL,
        "amount": "13.50",
        "description": "Coffee with Jo",
        "category": "coffee",
    }
    assert _count(connection, "parser_text_amendment_revisions") == 1
    assert _count(connection, "application_amendment_records") == 3
    assert _count(connection, "parser_proposal_completions") == 2
    current = connection.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (intake["public_id"],),
    ).fetchone()
    assert current is not None
    current_proposal = connection.execute(
        "SELECT public_id FROM parser_outputs WHERE id=?", (current[0],)
    ).fetchone()
    assert current_proposal is not None and current_proposal[0] == third.proposal_public_id

    clock_now[0] = NOW + 50_000
    replay = service.amend(first_review.review_id, "chain-edit-one", "chain-amendment-one")
    assert replay.amendment_id == first.amendment_id
    assert replay.proposal_public_id == first.proposal_public_id
    assert replay.is_current is False
    assert _count(connection, "application_amendment_records") == 3
    current_after = connection.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (intake["public_id"],),
    ).fetchone()
    assert current_after is not None and current_after[0] == current[0]


def test_incomplete_text_can_be_repaired_without_claiming_confirmability_until_money_is_complete(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    posting, _old_review, proposal, _intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="amendment-incomplete"
    )
    _make_text_proposal_incomplete(connection, proposal["public_id"])
    service = _amendment_service(connection)
    first_review = service.prepare(proposal["public_id"])
    assert first_review.projection["editable_values"]["amount"] is None
    assert first_review.projection["editable_values"]["currency"] is None
    first = _apply(
        connection,
        service,
        first_review,
        {"transaction_date": "2026-10-07", "merchant": "Harbour Cafe"},
        evidence_id="incomplete-nonmoney-evidence",
        amendment_id="incomplete-nonmoney-amendment",
    )
    still_incomplete = _read_effective_values(connection, first.proposal_public_id)
    assert still_incomplete == {
        "amount": None,
        "currency": None,
        "transaction_date": "2026-10-07",
        "merchant": "Harbour Cafe",
        "description": "Lunch",
        "category": "food",
    }
    with pytest.raises(PostingError):
        posting.prepare(first.proposal_public_id)
    _assert_no_posting_facts(connection)

    second_review = service.prepare(first.proposal_public_id)
    second = _apply(
        connection,
        service,
        second_review,
        {"amount": "15.00", "currency": "SGD"},
        evidence_id="incomplete-money-evidence",
        amendment_id="incomplete-money-amendment",
    )
    assert _read_effective_values(connection, second.proposal_public_id) == {
        "amount": "15.00",
        "currency": "SGD",
        "transaction_date": "2026-10-07",
        "merchant": "Harbour Cafe",
        "description": "Lunch",
        "category": "food",
    }
    fresh = _prepare_edited_posting(posting, second, connection, "incomplete-fresh-confirm")
    posted = posting.submit_post(fresh.review_id, "incomplete-fresh-confirm")
    assert posted.state == "finalized"
    assert _count(connection, "transactions") == 1
    assert _count(connection, "application_posting_decisions") == 1


def test_owner_failure_before_amendment_seal_rolls_back_completion_and_pointer(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application import amendment as amendment_module

    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="amendment-precommit-fault"
    )
    service = _amendment_service(connection)
    review = service.prepare(proposal["public_id"])
    _new_evidence(
        connection,
        review,
        {"description": "Must roll back"},
        evidence_id="precommit-fault-evidence",
    )
    before = {
        table: _count(connection, table)
        for table in (
            "application_amendment_records",
            "application_amendment_invalidations",
            "parser_proposal_completions",
            "financial_audit_events",
        )
    }

    def stop_before_seal(stage: str) -> None:
        if stage == "before_amendment_seal":
            raise RuntimeError("simulated loss before amendment seal")

    monkeypatch.setattr(amendment_module, "_failure_injection_hook", stop_before_seal)
    with pytest.raises(RuntimeError, match="before amendment seal"):
        service.amend(review.review_id, "precommit-fault-evidence", "precommit-fault-amendment")

    assert not connection.in_transaction
    assert {table: _count(connection, table) for table in before} == before
    row = connection.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (intake["public_id"],),
    ).fetchone()
    assert row is not None and int(row[0]) == int(proposal["id"])
    _assert_no_posting_facts(connection)


def test_two_connections_competing_amendments_have_one_winner(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    from tests.conftest import connect_temp_db

    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="amendment-two-connection-race"
    )
    primary_service = _amendment_service(connection)
    review = primary_service.prepare(proposal["public_id"])
    _new_evidence(
        connection,
        review,
        {"description": "Winner A"},
        evidence_id="race-evidence-a",
    )
    _new_evidence(
        connection,
        review,
        {"description": "Winner B"},
        evidence_id="race-evidence-b",
    )
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    barrier = threading.Barrier(2)
    outcomes: Queue[tuple[str, object]] = Queue()

    def amend(evidence_id: str, amendment_id: str) -> None:
        worker_connection = connect_temp_db(database_path)
        try:
            worker_service = _amendment_service(worker_connection)
            barrier.wait(timeout=10)
            try:
                outcomes.put(
                    ("accepted", worker_service.amend(review.review_id, evidence_id, amendment_id))
                )
            except AmendmentError as exc:
                outcomes.put(("refused", exc))
        finally:
            worker_connection.close()

    workers = [
        threading.Thread(target=amend, args=("race-evidence-a", "race-amendment-a"), daemon=True),
        threading.Thread(target=amend, args=("race-evidence-b", "race-amendment-b"), daemon=True),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    assert all(not worker.is_alive() for worker in workers)
    observed = [outcomes.get_nowait() for _ in workers]
    winners = [value for state, value in observed if state == "accepted"]
    losers = [value for state, value in observed if state == "refused"]
    assert len(winners) == len(losers) == 1
    assert isinstance(losers[0], AmendmentError)
    assert _count(connection, "application_amendment_records") == 1
    assert _count(connection, "parser_proposal_completions") == 1
    assert _read_effective_values(connection, winners[0].proposal_public_id)["description"] in {
        "Winner A",
        "Winner B",
    }
    source = _DurableSourceVerifier().verify_persisted(connection, intake["public_id"])
    assert source.source_event_id == "synthetic-event-amendment-two-connection-race"
    _assert_no_posting_facts(connection)


@pytest.mark.parametrize("corruption", ("changed_edge", "missing_edge"))
def test_independent_text_lineage_tampering_and_legacy_facade_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    corruption: str,
) -> None:
    from finance_core.application.posting import PostingError
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
    from finance_core.parser_proposals.repository import ParserProposalRepository
    from finance_core.parser_proposals.service import (
        ParserConfirmationError,
        confirm_parser_proposal,
    )

    connection = migrated_temp_db_connection
    posting, _old_review, proposal, _intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"lineage-{corruption}"
    )
    service = _amendment_service(connection)
    review = service.prepare(proposal["public_id"])
    result = _apply(
        connection,
        service,
        review,
        {"amount": "13.75"},
        evidence_id=f"lineage-evidence-{corruption}",
        amendment_id=f"lineage-amendment-{corruption}",
    )
    child = ParserProposalRepository(connection).get_by_public_id(result.proposal_public_id)
    assert child is not None

    with pytest.raises(ParserConfirmationError):
        confirm_parser_proposal(
            connection,
            child["id"],
            authenticated_actor_id=BINDING.human_principal_id,
            confirmation_channel="independent_application",
            confirmation_public_id=f"legacy-confirm-{corruption}",
            expected_content_hash=compute_effective_proposal_content_hash(connection, child),
            expected_version=result.proposal_version,
        )
    assert _count(connection, "transactions") == 0
    assert _count(connection, "parser_proposal_authorizations") == 0

    trigger_operation = "no_update" if corruption == "changed_edge" else "no_delete"
    trigger = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='parser_text_amendment_revisions' AND name=?",
        (f"parser_text_amendment_revisions_{trigger_operation}",),
    ).fetchone()
    assert trigger is not None
    connection.execute(f'DROP TRIGGER "{trigger[0]}"')
    if corruption == "changed_edge":
        connection.execute(
            "UPDATE parser_text_amendment_revisions SET child_payload_json='{}' "
            "WHERE amendment_id=?",
            (result.amendment_id,),
        )
    else:
        connection.execute(
            "DELETE FROM parser_text_amendment_revisions WHERE amendment_id=?",
            (result.amendment_id,),
        )
    connection.commit()

    with pytest.raises((AmendmentError, PostingError)):
        service.get_status(result.amendment_id)
    with pytest.raises(PostingError):
        posting.prepare(result.proposal_public_id)
    assert _count(connection, "transactions") == 0
    assert _count(connection, "parser_proposal_authorizations") == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"amount": "12.345"},
        {"amount": True},
        {"currency": "US"},
        {"transaction_date": "2026-02-30"},
        {"merchant": "   "},
        {"description": "x" * 1_025},
        {"intent": "shared_expense"},
        {"merchant": "Example Cafe"},
        {"merchant": "Valid Cafe", "amount": "12.345"},
    ],
    ids=(
        "excess-precision",
        "bool-amount",
        "invalid-currency",
        "invalid-date",
        "blank-merchant",
        "oversize-text",
        "prohibited-field",
        "no-material-change",
        "whole-patch-rollback",
    ),
)
def test_invalid_or_nonmaterial_patch_refuses_atomically(
    migrated_temp_db_connection: sqlite3.Connection,
    patch: Mapping[str, object],
) -> None:
    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"invalid-{len(patch)}-{abs(hash(tuple(patch)))}"
    )
    service = _amendment_service(connection)
    review = service.prepare(proposal["public_id"])
    _new_evidence(connection, review, patch)
    before = {
        table: _count(connection, table)
        for table in (
            "application_amendment_records",
            "parser_proposal_completions",
            "parser_text_amendment_revisions",
            "transactions",
            "parser_proposal_authorizations",
        )
    }

    with pytest.raises(AmendmentError):
        service.amend(review.review_id, "amendment-evidence-one", "invalid-amendment")

    assert {table: _count(connection, table) for table in before} == before
    row = connection.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (intake["public_id"],),
    ).fetchone()
    assert row is not None and int(row[0]) == int(proposal["id"])
    _assert_no_posting_facts(connection)


@pytest.mark.parametrize(
    "case",
    (
        "wrong_schema",
        "wrong_namespace",
        "wrong_key_id",
        "wrong_principal",
        "wrong_source",
        "wrong_version",
        "wrong_hash",
        "wrong_display",
        "wrong_reply",
        "wrong_conversation",
        "changed_display_body",
        "expired",
        "consumed",
        "revoked",
        "unknown_display",
        "tampered_signature",
        "missing_reply",
        "wrong_hmac_key",
    ),
)
def test_durable_authority_mismatch_refuses_without_any_publication(
    migrated_temp_db_connection: sqlite3.Connection,
    case: str,
) -> None:
    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"authority-{case}"
    )
    port = DurableSyntheticAmendmentAuthority()
    service = AmendmentService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_amendment_authority=port,
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
    )
    review = service.prepare(proposal["public_id"])
    display_overrides: dict[str, object] = {}
    reply_overrides: dict[str, object] = {}
    proof_overrides: dict[str, object] = {}
    signing_key = AMENDMENT_KEY
    if case == "wrong_schema":
        proof_overrides["schema"] = "other-schema"
    elif case == "wrong_namespace":
        proof_overrides["namespace"] = "other-namespace"
    elif case == "wrong_key_id":
        proof_overrides["key_id"] = "other-key"
    elif case == "wrong_principal":
        proof_overrides["human_principal_id"] = "other-human"
    elif case == "wrong_source":
        proof_overrides["source_evidence_digest"] = "0" * 64
    elif case == "wrong_version":
        proof_overrides["proposal_version"] = 9
    elif case == "wrong_hash":
        proof_overrides["proposal_content_hash"] = "0" * 64
    elif case == "wrong_display":
        proof_overrides["display_id"] = "missing-display"
    elif case == "wrong_reply":
        proof_overrides["reply_evidence_digest"] = "0" * 64
    elif case == "wrong_conversation":
        reply_overrides["conversation_id"] = "different-private-conversation"
    elif case == "changed_display_body":
        display_overrides["projection"] = {"different": "display body"}
    elif case == "expired":
        proof_overrides.update({"issued_at": NOW - 600, "expires_at": NOW - 1})
        reply_overrides.update({"issued_at": NOW - 600, "expires_at": NOW - 1})
    elif case == "consumed":
        proof_overrides["consumed"] = True
    elif case == "revoked":
        proof_overrides["revoked"] = True
    elif case == "unknown_display":
        display_overrides["state"] = "unknown"
    elif case == "wrong_hmac_key":
        signing_key = b"different synthetic key"
    ids = _new_evidence(
        connection,
        review,
        {"description": "Changed description"},
        display_overrides=display_overrides,
        reply_overrides=reply_overrides,
        proof_overrides=proof_overrides,
        sign_with=signing_key,
    )
    if case == "tampered_signature":
        tamper_signed_material(connection, "evidence", ids["evidence_id"])
    elif case == "missing_reply":
        connection.execute(
            "DELETE FROM synthetic_amendment_replies_v1 WHERE id=?", (ids["reply_id"],)
        )
        connection.commit()
    before = (
        _count(connection, "application_amendment_records"),
        _count(connection, "parser_proposal_completions"),
        _count(connection, "parser_text_amendment_revisions"),
        connection.total_changes,
    )

    with pytest.raises(AmendmentError):
        service.amend(review.review_id, ids["evidence_id"], "rejected-amendment")

    assert port.last_connection is connection
    assert (
        _count(connection, "application_amendment_records"),
        _count(connection, "parser_proposal_completions"),
        _count(connection, "parser_text_amendment_revisions"),
    ) == before[:3]
    assert connection.total_changes == before[3]
    row = connection.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (intake["public_id"],),
    ).fetchone()
    assert row is not None and int(row[0]) == int(proposal["id"])
    _assert_no_posting_facts(connection)


def test_external_evidence_is_read_from_the_supplied_database_and_not_a_copied_card(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, _intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="authority-same-db"
    )
    port = DurableSyntheticAmendmentAuthority()
    service = AmendmentService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_amendment_authority=port,
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
    )
    review = service.prepare(proposal["public_id"])
    ids = _new_evidence(connection, review, {"description": "Changed description"})
    copied_display = read_signed_material(connection, "display", ids["display_id"])
    copied_display["human_principal_id"] = "different-human"
    replace_signed_material(connection, "display", ids["display_id"], copied_display)

    with pytest.raises(AmendmentError):
        service.amend(review.review_id, ids["evidence_id"], "copied-card-amendment")

    assert port.last_connection is connection
    assert _count(connection, "application_amendment_records") == 0
    assert _count(connection, "parser_proposal_completions") == 0


def test_amendment_replay_survives_expiry_and_consumed_marker_without_duplicate_publication(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    _posting, _old_review, proposal, _intake, _decision_id, _display_id = _prepare_text_subject(
        connection, suffix="amendment-historical-replay"
    )
    now = [NOW]
    service = _amendment_service(connection, clock=lambda: now[0])
    review = service.prepare(proposal["public_id"])
    ids = _new_evidence(
        connection,
        review,
        {"description": "Changed description"},
        evidence_id="replay-evidence",
    )
    accepted = service.amend(review.review_id, ids["evidence_id"], "replay-amendment")
    before = (
        _count(connection, "application_amendment_records"),
        _count(connection, "parser_proposal_completions"),
        connection.total_changes,
    )
    proof = read_signed_material(connection, "evidence", ids["evidence_id"])
    proof["consumed"] = True
    replace_signed_material(connection, "evidence", ids["evidence_id"], proof)
    now[0] = NOW + 100_000

    replay = service.amend(review.review_id, ids["evidence_id"], "replay-amendment")
    status = service.get_status("replay-amendment")

    assert replay == status == dataclasses.replace(accepted, is_current=True)
    assert _count(connection, "application_amendment_records") == before[0]
    assert _count(connection, "parser_proposal_completions") == before[1]
    assert connection.total_changes == before[2] + 1


def test_confirmed_but_unconverted_posting_refuses_amendment(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application import posting as posting_module

    connection = migrated_temp_db_connection
    posting, review, proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="accepted-not-converted"
    )

    def fail_after_acceptance(stage: str) -> None:
        if stage == "after_acceptance_commit":
            raise RuntimeError("stop after accepted human decision")

    monkeypatch.setattr(posting_module, "_failure_injection_hook", fail_after_acceptance)
    with pytest.raises(RuntimeError, match="after accepted human decision"):
        posting.submit_post(review.review_id, decision_id)
    monkeypatch.setattr(posting_module, "_failure_injection_hook", None)
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "transactions") == 0

    service = _amendment_service(connection)
    with pytest.raises(AmendmentError):
        service.prepare(proposal["public_id"])

    assert _count(connection, "application_amendment_records") == 0
    assert _count(connection, "transactions") == 0


def test_receipt_chain_preserves_ocr_source_snapshot_and_final_metadata(
    tmp_path: Path,
) -> None:
    from finance_core.bookkeeping_metadata import (
        build_application_receipt_projection,
        metadata_from_payload,
    )
    from finance_core.calculation.authoritative_snapshot import (
        AuthoritativeSnapshotRepository,
        canonical_json_value,
    )
    from finance_core.calculators.receipt_calculator_input_projection import (
        project_receipt_calculator_input,
    )
    from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
    from finance_core.money import canonical_decimal_str

    (
        connection,
        _workspace,
        _manifest,
        posting,
        _old_review,
        proposal,
        intake_public_id,
        _old_decision,
        _old_display,
        source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, "amendment-receipt-chain")
    try:
        service = _amendment_service(connection, source_verifier=source_verifier)
        first_review = service.prepare(proposal)
        first = _apply(
            connection,
            service,
            first_review,
            {"description": "Dinner with Jo", "category": "dining"},
            evidence_id="receipt-edit-one",
            amendment_id="receipt-amendment-one",
        )
        second_review = service.prepare(first.proposal_public_id)
        second = _apply(
            connection,
            service,
            second_review,
            {
                "amount": "14.25",
                "transaction_date": "2026-10-07",
                "merchant": "Harbour Cafe",
            },
            evidence_id="receipt-edit-two",
            amendment_id="receipt-amendment-two",
        )
        assert second.proposal_public_id != first.proposal_public_id
        assert _read_effective_values(connection, second.proposal_public_id) == {
            "amount": "14.25",
            "currency": "SGD",
            "transaction_date": "2026-10-07",
            "merchant": "Harbour Cafe",
            "description": "Dinner with Jo",
            "category": "dining",
        }
        _assert_no_posting_facts(connection)

        raw = connection.execute(
            "SELECT raw_input,source_content_hash,attachment_hash FROM raw_intake_records "
            "WHERE public_id=?",
            (intake_public_id,),
        ).fetchone()
        assert raw is not None
        original_image = tmp_path / "external-amendment-receipt-chain" / "receipt.jpg"
        assert raw["raw_input"] == "local receipt image: receipt.jpg"
        assert raw["source_content_hash"] == (
            "sha256:" + hashlib.sha256(raw["raw_input"].encode("utf-8")).hexdigest()
        )
        assert raw["attachment_hash"] == _digest_bytes(original_image.read_bytes())
        ocr_links = connection.execute(
            "SELECT extraction_id,link_role,parser_output_id FROM receipt_ocr_proposal_links "
            "WHERE parser_output_id IN "
            "(SELECT id FROM parser_outputs WHERE public_id IN (?,?)) "
            "ORDER BY parser_output_id",
            (proposal, second.proposal_public_id),
        ).fetchall()
        assert len(ocr_links) == 2
        assert {str(row["link_role"]) for row in ocr_links} == {
            "initial",
            "superseding_correction",
        }
        assert len({int(row["extraction_id"]) for row in ocr_links}) == 1
        assert _count(connection, "receipt_ocr_blocks") == 5
        revision = connection.execute(
            "SELECT parent.public_id AS parent_public_id,child.public_id AS child_public_id "
            "FROM receipt_proposal_revisions AS revisions "
            "JOIN parser_outputs AS parent ON parent.id=revisions.superseded_parser_output_id "
            "JOIN parser_outputs AS child ON child.id=revisions.replacement_parser_output_id "
            "WHERE child.public_id=?",
            (second.proposal_public_id,),
        ).fetchone()
        assert revision is not None
        assert revision["parent_public_id"] == proposal
        assert revision["child_public_id"] == second.proposal_public_id

        fresh = _prepare_edited_posting(posting, second, connection, "receipt-fresh-confirm")
        approved = dict(fresh.projection["financial_projection"])
        assert approved["amount"] == "14.25"
        assert approved["transaction_date"] == "2026-10-07"
        assert approved["merchant"] == "Harbour Cafe"
        assert approved["description"] == "Dinner with Jo"
        assert approved["category"] == "dining"
        posted = posting.submit_post(fresh.review_id, "receipt-fresh-confirm")
        assert posted.state == "finalized"

        transaction = connection.execute(
            "SELECT amount,currency,transaction_date,merchant,description,category "
            "FROM transactions WHERE public_id=?",
            (posted.transaction_public_id,),
        ).fetchone()
        assert transaction is not None
        assert tuple(str(transaction[field]) for field in transaction.keys()) == (
            "14.25",
            "SGD",
            "2026-10-07",
            "Harbour Cafe",
            "Dinner with Jo",
            "dining",
        )
        receipt = connection.execute("SELECT description,category FROM receipts").fetchone()
        assert receipt is not None and tuple(receipt) == ("Dinner with Jo", "dining")
        assert _count(connection, "application_posting_decisions") == 1
        assert _count(connection, "transactions") == 1
        assert _count(connection, "authoritative_calculation_snapshots") == 1
        assert _count(connection, "receipt_proposal_revisions") == 1

        auth = connection.execute(
            "SELECT calculation_snapshot_id FROM application_conditional_authorization_proofs"
        ).fetchone()
        assert auth is not None
        snapshot = AuthoritativeSnapshotRepository(connection).fetch(str(auth[0]))
        assert snapshot is not None
        snapshot_input = canonical_json_value(snapshot.input_payload_json, label="input")
        snapshot_identity = snapshot_input["confirmed_receipt_identity"]
        assert snapshot_identity["bookkeeping_metadata"] == {
            "version": "application_bookkeeping_metadata_v1",
            "description": "Dinner with Jo",
            "category": "dining",
        }
        actual_input = project_receipt_calculator_input(
            connection, str(snapshot_identity["receipt_public_id"])
        )
        actual_calculation = calculate_receipt_split(actual_input.calculator_input)
        snapshot_output = canonical_json_value(snapshot.output_payload_json, label="output")
        serialized_calculation = json.loads(
            json.dumps(actual_calculation, sort_keys=True, default=canonical_decimal_str)
        )
        assert snapshot_output == serialized_calculation
        reconstructed_financial_projection = build_application_receipt_projection(
            merchant=str(snapshot_identity["merchant"]),
            receipt_date=str(snapshot_identity["receipt_date"]),
            currency=str(snapshot_identity["currency"]),
            payer_participant_public_id=str(actual_input.calculator_input["payer"]),
            calculation=actual_calculation,
            bookkeeping_metadata=metadata_from_payload(snapshot_identity["bookkeeping_metadata"]),
        )
        assert reconstructed_financial_projection == approved
        assert approved["description"] == "Dinner with Jo"
        assert approved["category"] == "dining"
        assert _count(connection, "application_amendment_records") == 2
        source = _DurableSourceVerifier().verify_persisted(connection, intake_public_id)
        assert source.source_event_id == "synthetic-event-amendment-receipt-chain"
    finally:
        connection.close()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
