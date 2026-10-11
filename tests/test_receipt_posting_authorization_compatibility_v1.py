"""Native receipt compatibility never substitutes for independent AI authority."""

from __future__ import annotations

import dataclasses

import pytest

from finance_core.application import posting as posting_module
from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.ai_invocation_contract import AiInvocationError, InterpretationPolicy
from finance_core.application.posting import (
    PostingError,
    PostingService,
    _AcceptedReceiptMetadataAuthority,
    _AcceptedReceiptPostingAuthorization,
)
from finance_core.application.receipt_posting_authorization import (
    VerifiedReceiptPostingAuthorization,
    require_conversion_authority,
    require_receipt_posting_authority,
)
from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry
from finance_core.parser_proposals.receipt_facts_conversion import (
    ReceiptFactsConversionCommand,
    _get_registry_row,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_application_amendment_v1 import _persist_fresh_posting_confirmation
from tests.test_application_posting_recovery_v1 import BINDING, NOW, _prepare_receipt_subject
from tests.test_d2_posting_authority_v1 import _file_connection
from tests.test_independent_ai_history_v1 import _tamper
from tests.test_independent_ai_invocation_v1 import ORIGINAL, DurableProvider
from tests.test_independent_ai_v2 import InputV2, cite, wire
from tests.test_receipt_facts_conversion_v1 import (
    command,
    convert,
    seed_confirmed_receipt_proposal,
    seed_people,
)


def _native(tmp_path, schema):
    connection = _file_connection(tmp_path)
    if schema == 59:
        apply_migration_paths(connection, TEMP_DB_MIGRATION_PATHS)
    seed_people(connection)
    parser_id, public_id, content_hash = seed_confirmed_receipt_proposal(
        connection, tmp_path, "receipt_owner_compat"
    )
    return connection, parser_id, command("receipt_owner_compat", public_id, content_hash)


def _snapshot(connection):
    return tuple(connection.iterdump()), connection.total_changes


@pytest.mark.parametrize("schema", [54, 59])
def test_native_receipt_retains_conversion_replay_and_downstream_guard(tmp_path, schema):
    connection, _, request = _native(tmp_path, schema)
    try:
        before = _snapshot(connection)
        assert require_conversion_authority(connection, request, None, None) is None
        assert _snapshot(connection) == before
        result = convert(connection, request)
        before = _snapshot(connection)
        assert require_receipt_posting_authority(connection, result.receipt_public_id, None) is None
        replay = convert(connection, request)
        assert replay.receipt_public_id == result.receipt_public_id
        assert replay.conversion_result_hash == result.conversion_result_hash
        assert _snapshot(connection) == before
        assert connection.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        connection.close()


def _damage_owner(connection, parser_id, fault):
    if fault == "partial-schema":
        # A similarly named fragment must not be treated as an old schema.
        connection.execute("CREATE TABLE independent_ai_orphan_fragment (value TEXT)")
        connection.commit()
    elif fault == "parser":
        _tamper(
            connection,
            "parser_outputs",
            "UPDATE parser_outputs SET parser_name='independent_finance_ai' WHERE id=?",
            (parser_id,),
        )
    elif fault == "link":
        _tamper(
            connection,
            "receipt_ocr_proposal_links",
            "UPDATE receipt_ocr_proposal_links "
            "SET parser_contract_version='finance-independent-ai-orphan' WHERE parser_output_id=?",
            (parser_id,),
        )
    elif fault == "field":
        _tamper(
            connection,
            "parser_proposal_field_evidence",
            "UPDATE parser_proposal_field_evidence SET evidence_reference='independent-ai:orphan' "
            "WHERE parser_output_id=?",
            (parser_id,),
        )
    elif fault == "lifecycle":
        _tamper(
            connection,
            "parser_proposal_events",
            "UPDATE parser_proposal_events SET event_reason='aipub_orphan' "
            "WHERE parser_output_id=?",
            (parser_id,),
        )
    else:
        assert fault == "audit"
        _tamper(
            connection,
            "financial_audit_events",
            "UPDATE financial_audit_events SET causation_public_id='aipub_orphan' "
            "WHERE aggregate_public_id=(SELECT public_id FROM parser_outputs WHERE id=?)",
            (parser_id,),
        )


@pytest.mark.parametrize("schema", [54, 59])
@pytest.mark.parametrize("guard", ["conversion", "receipt"])
@pytest.mark.parametrize(
    "fault", ["partial-schema", "parser", "link", "field", "lifecycle", "audit"]
)
def test_partial_schema_or_unowned_markers_never_select_native_exemption(
    tmp_path, schema, guard, fault
):
    connection, parser_id, request = _native(tmp_path, schema)
    try:
        result = convert(connection, request) if guard == "receipt" else None
        _damage_owner(connection, parser_id, fault)
        before = _snapshot(connection)
        with pytest.raises(AiInvocationError):
            if result is None:
                require_conversion_authority(connection, request, None, None)
            else:
                require_receipt_posting_authority(connection, result.receipt_public_id, None)
        assert _snapshot(connection) == before
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        connection.close()


def _accepted_ai_receipt(tmp_path, monkeypatch):
    connection, _, _, _, _, parent, intake, _, _, source, decision = _prepare_receipt_subject(
        tmp_path, suffix="receipt-owner-compat", ocr_confidence_scaled=3000, prepare_review=False
    )
    text = "EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50"
    fields = {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
    provider = DurableProvider(
        connection,
        response=wire(
            text,
            fields,
            evidence={
                "amount": [cite(text, "12.50")],
                "currency": [cite(text, "SGD")],
                "transaction_date": [cite(text, "2026-10-08")],
            },
        ),
    )
    ai = AiInvocationService(
        connection=connection,
        binding=BINDING,
        source_verifier=source,
        interpretation_input_verifier=InputV2(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    prepared = ai.prepare_initial(intake, "receipt-owner-compat-prepare")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    published = ai.publish(prepared.attempt_id)
    owner = PostingService(
        connection=connection,
        binding=BINDING,
        source_verifier=source,
        human_decision_authority=decision,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    review = owner.prepare(published.proposal_public_id)
    _persist_fresh_posting_confirmation(
        connection, review, published.proposal_public_id, "receipt-owner-compat-confirm"
    )

    def stop(stage):
        if stage == "after_acceptance_commit":
            raise RuntimeError("synthetic accepted receipt")

    monkeypatch.setattr(posting_module, "_failure_injection_hook", stop)
    with pytest.raises(RuntimeError, match="synthetic accepted receipt"):
        owner.submit_post(review.review_id, "receipt-owner-compat-confirm")
    monkeypatch.setattr(posting_module, "_failure_injection_hook", None)
    attempt = connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[
        0
    ]
    payer = review.projection["payer_participant_public_id"]
    request = ReceiptFactsConversionCommand(
        command_public_id="rpfc_application_" + posting_module._sha([attempt])[:24],
        proposal_public_id=published.proposal_public_id,
        expected_content_hash=published.effective_content_hash,
        payer_participant_public_id=payer,
        participants=({"participant_public_id": payer, "is_included": 1},),
        authenticated_actor_id=BINDING.human_principal_id,
        channel="independent_application",
    )
    return connection, parent, owner, attempt, request, provider


class _ForgedReceiptAuthority:
    def verify_in_transaction(self, connection, expected):
        raise AssertionError("Caller-chosen authority must never be invoked")


def test_real_ai_target_and_same_source_sibling_keep_exact_owner_and_fixed_port(
    tmp_path, monkeypatch
):
    connection, _parent, owner, attempt, request, provider = _accepted_ai_receipt(
        tmp_path, monkeypatch
    )
    try:
        # Offline alias attack: another native receipt claims the same source.
        # The alias grants no authority and must not downgrade the real AI target.
        sibling_id, sibling_public_id, _ = seed_confirmed_receipt_proposal(
            connection, tmp_path, "receipt_owner_sibling"
        )
        target = ParserProposalRepository(connection).get_by_public_id(request.proposal_public_id)
        _tamper(
            connection,
            "parser_outputs",
            "UPDATE parser_outputs SET source_public_id=? WHERE id=?",
            (target["source_public_id"], sibling_id),
        )
        native = ParserProposalRepository(connection).get_lineage_row_by_id(sibling_id)
        assert native["source_public_id"] == target["source_public_id"]
        assert native["parent_parser_output_id"] is None
        assert has_independent_ai_ancestry(connection, native) is False
        assert has_independent_ai_ancestry(connection, target) is True
        native_request = dataclasses.replace(request, proposal_public_id=sibling_public_id)

        authority = _AcceptedReceiptPostingAuthorization(owner, attempt)
        connection.execute("BEGIN IMMEDIATE")
        metadata = _AcceptedReceiptMetadataAuthority(owner, attempt).verify_in_transaction(
            connection, command=request, existing=None
        )
        for rejected in (
            None,
            _ForgedReceiptAuthority(),
            _AcceptedReceiptPostingAuthorization(owner, "unaccepted-other-attempt"),
        ):
            before = _snapshot(connection)
            with pytest.raises((AiInvocationError, PostingError)):
                require_conversion_authority(connection, request, None, rejected, metadata)
            assert _snapshot(connection) == before
        proof = require_conversion_authority(connection, request, None, authority, metadata)
        assert type(proof) is VerifiedReceiptPostingAuthorization
        assert proof.expected.proposal_public_id == request.proposal_public_id
        assert proof.accepted_attempt_id == attempt
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0

        final = owner.resume_post(attempt)
        assert final.state == "finalized"
        assert owner.resume_post(attempt) == final
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        receipt = connection.execute("SELECT public_id FROM receipts").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        for rejected in (
            None,
            _ForgedReceiptAuthority(),
            _AcceptedReceiptPostingAuthorization(owner, "unaccepted-other-attempt"),
        ):
            before = _snapshot(connection)
            with pytest.raises((AiInvocationError, PostingError)):
                require_receipt_posting_authority(connection, receipt, rejected)
            assert _snapshot(connection) == before
        proof = require_receipt_posting_authority(connection, receipt, authority)
        assert type(proof) is VerifiedReceiptPostingAuthorization
        assert proof.expected.existing_conversion == _get_registry_row(
            connection, request.command_public_id
        )
        connection.rollback()
        assert provider.calls == 1

        # A genuine AI publication on this source cannot legitimize a forged sibling.
        _damage_owner(connection, sibling_id, "parser")
        before = _snapshot(connection)
        with pytest.raises(AiInvocationError, match="creation owner"):
            require_conversion_authority(connection, native_request, None, authority)
        assert _snapshot(connection) == before
        _tamper(
            connection,
            "receipt_proposal_conversions",
            "UPDATE receipt_proposal_conversions SET parser_output_id=? WHERE command_public_id=?",
            (sibling_id, request.command_public_id),
        )
        before = _snapshot(connection)
        with pytest.raises(AiInvocationError, match="creation owner"):
            require_receipt_posting_authority(connection, receipt, authority)
        assert _snapshot(connection) == before
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    finally:
        connection.close()


def _native_058(tmp_path):
    connection, parser_id, request = _native(tmp_path, 54)
    apply_migration_paths(connection, TEMP_DB_MIGRATION_PATHS[:58])
    return connection, parser_id, request


def test_native_058_receipt_retains_conversion_replay_and_downstream_guard(tmp_path):
    connection, parser_id, request = _native_058(tmp_path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='independent_ai_attempts'"
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'independent_ai_clarification_%' "
            "OR name LIKE 'independent_ai_followup_%'"
        ).fetchall()
        proposal = ParserProposalRepository(connection).get_lineage_row_by_id(parser_id)
        before = _snapshot(connection)
        assert has_independent_ai_ancestry(connection, proposal) is False
        assert require_conversion_authority(connection, request, None, None) is None
        assert _snapshot(connection) == before
        result = convert(connection, request)
        before = _snapshot(connection)
        assert require_receipt_posting_authority(connection, result.receipt_public_id, None) is None
        replay = convert(connection, request)
        assert replay.receipt_public_id == result.receipt_public_id
        assert replay.conversion_result_hash == result.conversion_result_hash
        assert _snapshot(connection) == before
        assert connection.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        connection.close()


def _damage_clarification_schema(connection, fault):
    if fault == "clarification-fragment":
        connection.execute("CREATE TABLE independent_ai_clarification_orphan (value TEXT)")
    elif fault == "followup-fragment":
        connection.execute("CREATE VIEW independent_ai_followup_orphan AS SELECT 1 AS value")
    else:
        assert fault in {"missing-trigger", "changed-trigger"}
        connection.execute("DROP TRIGGER independent_ai_followup_links_no_update")
        if fault == "changed-trigger":
            connection.execute(
                "CREATE TRIGGER independent_ai_followup_links_no_update "
                "BEFORE UPDATE ON independent_ai_followup_links "
                "BEGIN SELECT RAISE(ABORT, 'changed immutable guard'); END"
            )
    connection.commit()


@pytest.mark.parametrize("guard", ["conversion", "receipt"])
@pytest.mark.parametrize(
    ("schema", "fault"),
    [
        (58, "clarification-fragment"),
        (58, "followup-fragment"),
        (59, "missing-trigger"),
        (59, "changed-trigger"),
    ],
)
def test_partial_clarification_schema_never_selects_native_exemption(
    tmp_path, schema, guard, fault
):
    connection, _, request = _native_058(tmp_path) if schema == 58 else _native(tmp_path, schema)
    try:
        result = convert(connection, request) if guard == "receipt" else None
        _damage_clarification_schema(connection, fault)
        before = _snapshot(connection)
        with pytest.raises(AiInvocationError, match="migration 059 absent or changed"):
            if result is None:
                require_conversion_authority(connection, request, None, None)
            else:
                require_receipt_posting_authority(connection, result.receipt_public_id, None)
        assert _snapshot(connection) == before
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("guard", ["conversion", "receipt"])
def test_real_ai_receipt_rejects_damaged_clarification_schema(tmp_path, monkeypatch, guard):
    connection, _, owner, attempt, request, provider = _accepted_ai_receipt(tmp_path, monkeypatch)
    try:
        authority = _AcceptedReceiptPostingAuthorization(owner, attempt)
        receipt = None
        if guard == "receipt":
            assert owner.resume_post(attempt).state == "finalized"
            receipt = connection.execute("SELECT public_id FROM receipts").fetchone()[0]
        _damage_clarification_schema(connection, "missing-trigger")
        connection.execute("BEGIN IMMEDIATE")
        before = _snapshot(connection)
        with pytest.raises(AiInvocationError, match="migration 059 absent or changed"):
            if receipt is None:
                require_conversion_authority(connection, request, None, authority)
            else:
                require_receipt_posting_authority(connection, receipt, authority)
        assert _snapshot(connection) == before
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == (
            0 if receipt is None else 1
        )
        assert provider.calls == 1
        connection.rollback()
    finally:
        connection.close()
