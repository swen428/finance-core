"Genuine neutral owner probes: capture, durable transport, child, human post."

from __future__ import annotations

import dataclasses
import json

import pytest
from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
from test_application_amendment_v1 import _EDIT_BINDING, _apply, _persist_fresh_posting_confirmation
from test_application_posting_recovery_v1 import (
    BINDING,
    NOW,
    _DurablePostingDecisionAuthority,
    _DurableSourceVerifier,
    _load,
    _persist,
    _prepare_text_subject,
)

from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.ai_invocation_contract import (
    AI_INTERPRETATION_SCHEMA,
    AiInvocationError,
    AiProviderObservation,
    InterpretationPolicy,
    ProviderBinding,
)
from finance_core.application.ai_invocation_contract import (
    AI_RESULT_SCHEMA_V1 as AI_RESULT_SCHEMA,
)
from finance_core.application.ai_invocation_contract import (
    VerifiedInterpretationInputV2 as VerifiedInterpretationInput,
)
from finance_core.application.amendment import AmendmentService
from finance_core.application.posting import PostingError, PostingService
from finance_core.application.review import get_proposal_review
from finance_core.parser_proposals.independent_ai_validation import sha

PROVIDER = ProviderBinding(
    "synthetic-provider",
    "synthetic-model",
    "synthetic-adapter-v1",
    "synthetic-endpoint",
    "nonsecret-key-id",
    60,
)
KEY = b"synthetic-test-provider-only-key"
ORIGINAL = {
    "amount": "12.50",
    "currency": "SGD",
    "transaction_date": "2026-10-08",
    "merchant": "Example Cafe",
    "description": "Lunch",
    "category": "food",
}


class DurableProvider:
    binding = PROVIDER

    def __init__(
        self,
        connection,
        *,
        response=None,
        outcome="RESPONSE",
        observed_provider="synthetic-provider",
        observed_model="synthetic-model",
        at=NOW,
    ):
        self.connection = connection
        self.response, self.outcome = response, outcome
        self.observed_provider, self.observed_model, self.at = observed_provider, observed_model, at
        self.calls = 0
        self.requests = []
        connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS synthetic_ai_receipts (id TEXT PRIMARY"
                " KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
            )
        )
        connection.commit()

    def dispatch(self, request):
        assert not self.connection.in_transaction
        self.calls += 1
        self.requests.append(json.loads(request.request_json))
        from independent_ai_candidate_fixture_v2 import candidate_fixture_v2

        retained_response = candidate_fixture_v2(self.response, json.loads(request.request_json))
        observation = AiProviderObservation(
            request.claim_id,
            request.request_hash,
            self.outcome,
            self.observed_provider,
            self.observed_model,
            self.at,
            "a" * 64,
            retained_response,
        )
        identity = "synthetic-observation-" + request.claim_id
        _persist(
            self.connection,
            "synthetic_ai_receipts",
            identity,
            {
                "request": dataclasses.asdict(request),
                "observation": dataclasses.asdict(observation),
            },
            KEY,
        )
        self.connection.commit()
        return identity

    def verify_persisted(self, connection, observation_record_id, expected):
        assert connection is self.connection
        material = _load(connection, "synthetic_ai_receipts", observation_record_id, KEY)
        if material["request"] != dataclasses.asdict(expected):
            raise AiInvocationError("Actual retained transport request differs")
        return AiProviderObservation(**material["observation"])


class DurableInputVerifier:
    def verify_persisted(self, connection, reply_evidence_id, expected):
        retained = _load(connection, "synthetic_interpretation_replies", reply_evidence_id, KEY)
        proof = VerifiedInterpretationInput(**retained)
        display = _load(connection, "synthetic_interpretation_displays", proof.display_id, KEY)
        if (
            display["status"] != "SENT"
            or display["review_id"] != expected.interaction_id
            or display["review_hash"] != expected.interaction_projection_hash
            or sha(display) != proof.display_evidence_digest
        ):
            raise AiInvocationError("Interpretation display delivery is not genuine")
        from finance_core.application.amendment_contract import amendment_review_sha256
        from finance_core.application.posting_contract import posting_review_sha256

        projection = display["projection"]
        digest = (
            posting_review_sha256(projection)
            if projection["schema"].startswith("finance-application-posting-review-")
            else amendment_review_sha256(projection)
        )
        if digest != expected.interaction_projection_hash:
            raise AiInvocationError("Full displayed material differs")
        if (
            proof.interaction_id != expected.interaction_id
            or proof.interaction_projection_hash != expected.interaction_projection_hash
            or proof.source_evidence_digest != expected.source.evidence_digest
            or proof.proposal_content_hash != expected.proposal_content_hash
        ):
            raise AiInvocationError("Retained exact display/reply differs")
        if not expected.historical:
            current = connection.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
                (expected.source.intake_public_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT public_id FROM parser_outputs WHERE id=?", (current[0],)
            ).fetchone()
            if target[0] != expected.proposal_public_id:
                raise AiInvocationError("Reply target stale")
        return proof


def response(kind="initial", fields=None, quote="Lunch SGD 12.50 at Example Cafe 2026-10-08"):
    fields = ORIGINAL if fields is None else fields
    return json.dumps(
        {
            "schema": AI_RESULT_SCHEMA,
            "kind": kind,
            "fields": fields,
            "evidence": {
                k: {"ref": "source" if kind == "initial" else "reply", "quote": quote}
                for k in fields
            },
        }
    )


def service(connection, provider=None, *, clock=lambda: NOW):
    provider = provider or DurableProvider(connection, response=response())
    return AiInvocationService(
        connection=connection,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=clock,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    ), provider


def subject(connection, suffix="ai"):
    return _prepare_text_subject(
        connection, suffix=suffix, raw_text="Lunch SGD 12.50 at Example Cafe 2026-10-08"
    )


def posting(connection, ai):
    return PostingService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_decision_authority=_DurablePostingDecisionAuthority(),
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )


def amendment(connection, ai):
    return AmendmentService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )


def publish_initial(connection, suffix="ai"):
    oldposting, old_review, parent, intake, _, _ = subject(connection, suffix)
    ai, provider = service(connection)
    prepared = ai.prepare_initial(intake["public_id"], "operation-" + suffix)
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    result = ai.publish(prepared.attempt_id)
    assert result.state == "PUBLISHED"
    assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    return ai, provider, result, oldposting, old_review, parent, intake


def test_real_text_initial_to_new_review_fresh_human_final_transaction(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    ai, provider, result, oldposting, old_review, parent, intake = publish_initial(c)
    assert provider.calls == 1
    assert get_proposal_review(c, result.proposal_public_id)["proposal_origin"] == "independent_ai"
    assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
    with pytest.raises(PostingError):
        oldposting.prepare(result.proposal_public_id)
    p = posting(c, ai)
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, result.proposal_public_id, "ai-fresh-confirm")
    final = p.submit_post(fresh.review_id, "ai-fresh-confirm")
    assert final.state == "finalized"
    transaction = c.execute(
        ("SELECT amount,currency,transaction_date,merchant,description,category FROM transactions")
    ).fetchone()
    assert tuple(transaction) == (12.5, "SGD", "2026-10-08", "Example Cafe", "Lunch", "food")
    assert ai.recover(result.attempt_id) == result
    assert p.get_status(final.attempt_id) == final


def test_one_claim_restart_unknown_no_second_dispatch(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = subject(c)
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "one-operation")
    first = ai.claim(attempt.attempt_id)
    assert first.claim_id
    assert ai.claim(attempt.attempt_id).state == "UNKNOWN"
    assert ai.invoke(attempt.attempt_id).state == "UNKNOWN"
    assert ai.recover(attempt.attempt_id).state == "UNKNOWN"
    assert provider.calls == 0
    with pytest.raises(AiInvocationError, match="cannot be dispatched again|no retry"):
        ai.prepare_initial(intake["public_id"], "retry-operation")


@pytest.mark.parametrize("after_claim", [False, True])
def test_cancel_before_after_claim_no_publication(migrated_temp_db_connection, after_claim):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = subject(c)
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "cancel-operation")
    if after_claim:
        request = ai.claim(attempt.attempt_id)
        receipt = provider.dispatch(request)
    cancelled = ai.cancel(attempt.attempt_id)
    assert cancelled.state == ("UNKNOWN" if after_claim else "CANCELLED")
    if after_claim:
        assert ai.record_outcome(attempt.attempt_id, receipt).state == "UNKNOWN"
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kwargs,state",
    [
        ({"observed_model": "silent-fallback"}, "RESPONSE_REJECTED"),
        ({"outcome": "UNKNOWN", "response": None}, "UNKNOWN"),
        ({"outcome": "NOT_SENT", "response": None}, "NOT_SENT"),
        ({"response": '{"schema":"wrong"}'}, "RESPONSE_REJECTED"),
    ],
)
def test_failed_observation_is_retained_without_retry(migrated_temp_db_connection, kwargs, state):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = subject(c)
    config = {"response": response(), **kwargs}
    ai, provider = service(c, DurableProvider(c, **config))
    attempt = ai.prepare_initial(intake["public_id"], "failed-operation")
    assert ai.invoke(attempt.attempt_id).state == state
    assert ai.invoke(attempt.attempt_id).state == state
    assert provider.calls == 1
    assert c.execute("SELECT count(*) FROM independent_ai_results").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_exact_input_is_text_only_privacy_refuses_before_claim(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = _prepare_text_subject(
        c, suffix="privacy", raw_text="Lunch SGD 12.50 api_key credential /private/tmp/image.png"
    )
    ai, provider = service(c)
    with pytest.raises(AiInvocationError, match="sensitive_text"):
        ai.prepare_initial(intake["public_id"], "private-operation")
    assert provider.calls == 0
    assert c.execute("SELECT count(*) FROM independent_ai_claims").fetchone()[0] == 0


def persist_interpretation_reply(c, review, text, *, identity="natural-reply"):
    for table in ("synthetic_interpretation_replies", "synthetic_interpretation_displays"):
        c.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    source = review.projection["source"]
    view = review.projection["proposal_review"]
    display = {
        "review_id": review.review_id,
        "review_hash": review.review_hash,
        "projection": review.projection,
        "status": "SENT",
        "display_id": "natural-display-" + identity,
    }
    _persist(c, "synthetic_interpretation_displays", display["display_id"], display, KEY)
    proof = VerifiedInterpretationInput(
        AI_INTERPRETATION_SCHEMA,
        BINDING.instance_id,
        BINDING.human_principal_id,
        identity,
        sha(text),
        source["evidence_id"],
        source["evidence_digest"],
        "card_edit",
        review.review_id,
        review.review_hash,
        view["proposal_public_id"],
        view["proposal_version"],
        view["effective_content_hash"],
        display["display_id"],
        sha(display),
        display["display_id"],
        text,
        NOW,
        NOW + 30,
    )
    _persist(c, "synthetic_interpretation_replies", identity, dataclasses.asdict(proof), KEY)
    c.commit()
    return identity


def test_natural_edit_mixed_confirm_requires_fresh_confirmation_and_no_human_ai_witness(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, initial, *_ = publish_initial(c, "natural")
    edit = amendment(c, ai).prepare(initial.proposal_public_id)
    text = "Actually SGD 5.80 at Cafe Two; change and confirm"
    reply = persist_interpretation_reply(c, edit, text)
    provider.response = response("patch", {"amount": "5.80", "merchant": "Cafe Two"}, text)
    attempt = ai.prepare_edit(edit.review_id, reply, "natural-operation")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    sent = provider.requests[-1]["input"]
    assert set(sent) == {
        "schema",
        "kind",
        "interaction_kind",
        "interaction",
        "interpretation_context",
        "draft_values",
        "unresolved",
        "source_text",
        "selection",
        "image_to_model",
        "reply_text",
        "current_values",
    }
    assert sent["source_text"] == ""
    assert sent["reply_text"] == text
    assert sent["current_values"] == ORIGINAL
    assert "Lunch SGD 12.50 at Example Cafe 2026-10-08" not in json.dumps(provider.requests[-1])
    assert "source_context" not in sent
    assert "attachment_path" not in json.dumps(sent)
    assert "image_url" not in json.dumps(sent)
    result = ai.publish(attempt.attempt_id)
    assert get_proposal_review(c, result.proposal_public_id)["amount"] == "5.80"
    assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM parser_proposal_authorizations").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    with pytest.raises(PostingError):
        posting(c, ai).prepare(initial.proposal_public_id)
    p = posting(c, ai)
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, fresh, result.proposal_public_id, "natural-fresh-confirm"
    )
    final = p.submit_post(fresh.review_id, "natural-fresh-confirm")
    assert final.state == "finalized"
    assert tuple(c.execute("SELECT amount,merchant FROM transactions").fetchone()) == (
        5.8,
        "Cafe Two",
    )
    assert ai.get_status(initial.attempt_id).is_current is False


def test_ai_human_ai_human_mixed_lineage_publishes_and_posts_once(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    ai, provider, initial, *_ = publish_initial(c, "mixed")
    human = amendment(c, ai)
    old = human.prepare(initial.proposal_public_id)
    first = _apply(
        c,
        human,
        old,
        {"amount": "7.25"},
        evidence_id="mixed-human-one",
        amendment_id="mixed-amendment-one",
    )
    edit = human.prepare(first.proposal_public_id)
    text = "Change merchant to Cafe Three"
    reply = persist_interpretation_reply(c, edit, text, identity="mixed-reply")
    provider.response = response("patch", {"merchant": "Cafe Three"}, text)
    attempt = ai.prepare_edit(edit.review_id, reply, "mixed-ai-two")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    second = ai.publish(attempt.attempt_id)
    old2 = human.prepare(second.proposal_public_id)
    final_proposal = _apply(
        c,
        human,
        old2,
        {"amount": "8.25"},
        evidence_id="mixed-human-two",
        amendment_id="mixed-amendment-two",
    )
    p = posting(c, ai)
    fresh = p.prepare(final_proposal.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, fresh, final_proposal.proposal_public_id, "mixed-final-confirm"
    )
    final = p.submit_post(fresh.review_id, "mixed-final-confirm")
    assert final.state == "finalized"
    assert tuple(c.execute("SELECT amount,merchant FROM transactions").fetchone()) == (
        8.25,
        "Cafe Three",
    )
    assert ai.get_status(initial.attempt_id).is_current is False
    assert human.get_status(first.amendment_id).is_current is False


def test_genuine_local_ocr_receipt_child_source_guard_and_actual_facts_snapshot_final(tmp_path):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    from finance_core.receipt_staging_runner.local_intake import (
        require_local_runner_receipt_proposal,
    )

    c, workspace, manifest, _, _, parent, intake, _, _, source_port, decision_port = (
        _prepare_receipt_subject(tmp_path, suffix="ai-receipt")
    )
    fields = {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
    quote = "EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50"
    provider = DurableProvider(c, response=response(fields=fields, quote=quote))
    ai = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=source_port,
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    prepared = ai.prepare_initial(intake, "receipt-ai-operation")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    result = ai.publish(prepared.attempt_id)
    require_local_runner_receipt_proposal(
        c, result.proposal_public_id, workspace=workspace, manifest=manifest
    )
    assert (
        c.execute(
            (
                "SELECT link_role FROM receipt_ocr_proposal_links WHERE parser_out"
                "put_id=(SELECT id FROM parser_outputs WHERE public_id=?)"
            ),
            (result.proposal_public_id,),
        ).fetchone()[0]
        == "independent_ai"
    )
    assert c.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    p = PostingService(
        connection=c,
        source_verifier=source_port,
        human_decision_authority=decision_port,
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, fresh, result.proposal_public_id, "receipt-ai-fresh-confirm"
    )
    final = p.submit_post(fresh.review_id, "receipt-ai-fresh-confirm")
    assert final.state == "finalized"
    assert p.get_status(final.attempt_id) == final
    assert c.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM receipt_item_allocation_fact_sets").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM authoritative_calculation_snapshots").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
    assert tuple(
        c.execute(
            "SELECT amount,currency,merchant,description,category FROM transactions"
        ).fetchone()
    ) == (12.5, "SGD", "EXAMPLE CAFE", "Receipt total", "food")
    c.close()


def test_actual_low_ocr_ai_full_card_has_new_authority_without_fabricated_human(tmp_path):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    c, _, _, _, _, _, intake, _, _, source_port, decision_port = _prepare_receipt_subject(
        tmp_path, suffix="ai-low-ocr", ocr_confidence_scaled=6000, prepare_review=False
    )
    fields = {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
    quote = "EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50"
    provider = DurableProvider(c, response=response(fields=fields, quote=quote))
    ai = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=source_port,
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    prepared = ai.prepare_initial(intake, "low-ocr-operation")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    result = ai.publish(prepared.attempt_id)
    p = PostingService(
        connection=c,
        source_verifier=source_port,
        human_decision_authority=decision_port,
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    assert get_proposal_review(c, result.proposal_public_id)["confirm_available"] is False
    assessed = p.prepare(result.proposal_public_id)
    assert assessed.projection["expense_assessment"]["basis"] == "full_card_ai_expense_v1"
    assert (
        "historical_low_ocr_or_parser_confidence"
        in assessed.projection["expense_assessment"]["review_warnings"]
    )
    assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert (
        c.execute(
            "SELECT confidence_score FROM parser_outputs WHERE public_id=?",
            (result.proposal_public_id,),
        ).fetchone()[0]
        < 0.8
    )
    human = AmendmentService(
        connection=c,
        source_verifier=source_port,
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    old = human.prepare(result.proposal_public_id)
    patch = {
        "amount": "13.75",
        "currency": "USD",
        "transaction_date": "2026-10-09",
        "merchant": "Human Cafe",
        "description": "Human lunch",
        "category": "human_food",
    }
    material = _apply(
        c,
        human,
        old,
        patch,
        evidence_id="low-ocr-human-material",
        amendment_id="low-ocr-human-amendment",
    )
    fresh = p.prepare(material.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, fresh, material.proposal_public_id, "low-ocr-human-confirm"
    )
    final = p.submit_post(fresh.review_id, "low-ocr-human-confirm")
    assert final.state == "finalized"
    assert tuple(c.execute("SELECT amount,currency,merchant FROM transactions").fetchone()) == (
        13.75,
        "USD",
        "Human Cafe",
    )
    c.close()


@pytest.mark.parametrize(
    "raw,fields",
    [
        ("SGD 12.50 or SGD 15.50 at Cafe 2026-10-08", ORIGINAL),
        ("SGD 12.50 or USD 15.50 at Cafe 2026-10-08", ORIGINAL),
        ("SGD 12.50 at Cafe 2026-10-08 or 2026-10-09", ORIGINAL),
        ("refund SGD 12.50 at Cafe 2026-10-08", ORIGINAL),
    ],
)
def test_python_text_assessment_refuses_model_selected_conflicts(
    migrated_temp_db_connection, raw, fields
):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = _prepare_text_subject(c, raw_text=raw, suffix="conflict")
    ai, provider = service(c, DurableProvider(c, response=response(fields=fields, quote=raw)))
    prepared = ai.prepare_initial(intake["public_id"], "conflict-operation")
    assert ai.invoke(prepared.attempt_id).state == (
        "RESPONSE_REJECTED" if raw.startswith("refund") else "NEEDS_CLARIFICATION"
    )
    with pytest.raises(AiInvocationError):
        ai.publish(prepared.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0


@pytest.mark.parametrize(
    "raw",
    [
        "Cafe SGD 5.80 2026/08/02",
        "Cafe SGD 5.80 2026年8月2日",
        "昨天 Cafe SGD 5.80",
        "Cafe usd 5.80",
        "Cafe ￥5.80",
        "Cafe $5.80",
    ],
)
def test_default_context_does_not_erase_date_or_currency_notation(raw):
    from finance_core.parser_proposals.independent_ai_validation import validate_response

    projection = {
        "kind": "initial",
        "source_text": raw,
        "selection": {"kind": "text"},
        "source_context": {"currency": "SGD", "transaction_date": "2026-10-09"},
    }
    fields = {
        "amount": "5.80",
        "currency": "SGD",
        "transaction_date": "2026-10-09",
        "merchant": "Cafe",
    }
    evidence = {k: {"ref": "source", "quote": raw} for k in fields}
    evidence["currency"] = {"ref": "source_context", "quote": "SGD"}
    evidence["transaction_date"] = {"ref": "source_context", "quote": "2026-10-09"}
    body = json.dumps(
        {"schema": AI_RESULT_SCHEMA, "kind": "initial", "fields": fields, "evidence": evidence}
    )
    assert validate_response(body, projection, {})["state"] != "VALID"


@pytest.mark.parametrize("returned", [True, {}, {"attempt_ids": []}])
def test_noncontract_ai_authority_cannot_prepare_posting(migrated_temp_db_connection, returned):
    c = migrated_temp_db_connection
    ai, _, result, *_ = publish_initial(c)

    class InvalidAuthority:
        def verify_in_snapshot(self, *args):
            return returned

    p = PostingService(
        connection=c,
        source_verifier=_DurableSourceVerifier(),
        human_decision_authority=_DurablePostingDecisionAuthority(),
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=InvalidAuthority(),
    )
    with pytest.raises(PostingError):
        p.prepare(result.proposal_public_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_provider_signature_tamper_refuses_history_and_post_readback(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    ai, provider, result, *_ = publish_initial(c)
    p = posting(c, ai)
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, result.proposal_public_id, "tamper-confirm")
    final = p.submit_post(fresh.review_id, "tamper-confirm")
    c.execute("UPDATE synthetic_ai_receipts SET signature=?", ("0" * 64,))
    c.commit()
    for operation in (
        lambda: ai.get_status(result.attempt_id),
        lambda: ai.recover(result.attempt_id),
        lambda: p.get_status(final.attempt_id),
        lambda: p.resume_post(final.attempt_id),
    ):
        with pytest.raises(ValueError):
            operation()
    assert provider.calls == 1
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


@pytest.mark.parametrize("domain", ["v1", "v2"])
def test_direct_conversion_and_readback_require_actual_ai_port(migrated_temp_db_connection, domain):
    from finance_core.parser_proposals import decision_owner

    c = migrated_temp_db_connection
    if domain == "v2":
        ai, _, result, *_ = publish_initial(c)
    else:
        from finance_core.application.ai_invocation import _AiInvocationServiceV1

        _, _, _, intake, *_ = subject(c, "direct-historical-v1")
        provider = DurableProvider(c, response=response())
        ai = _AiInvocationServiceV1(
            connection=c,
            binding=BINDING,
            source_verifier=_DurableSourceVerifier(),
            interpretation_input_verifier=DurableInputVerifier(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        old = ai.prepare_initial(intake["public_id"], "direct-historical-v1-op")
        assert ai.invoke(old.attempt_id).state == "VALID"
        result = ai.publish(old.attempt_id)
    p = posting(c, ai)
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, result.proposal_public_id, "raw-confirm")
    final = p.submit_post(fresh.review_id, "raw-confirm")
    parser_id = c.execute(
        "SELECT id FROM parser_outputs WHERE public_id=?", (result.proposal_public_id,)
    ).fetchone()[0]
    for operation in (
        lambda: decision_owner.convert_confirmed_parser_proposal(
            c, parser_id, persistence_effect=lambda *_: None
        ),
        lambda: decision_owner.verify_converted_parser_proposal(c, parser_id),
    ):
        with pytest.raises(AiInvocationError):
            operation()
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    kwargs = {"ai_lineage_authority": ai}
    if domain == "v2":
        with pytest.raises(AiInvocationError):
            decision_owner.verify_converted_parser_proposal(c, parser_id, **kwargs)
        kwargs = p._text_kwargs(final.attempt_id)
    assert (
        decision_owner.verify_converted_parser_proposal(c, parser_id, **kwargs)[
            "transaction_public_id"
        ]
        == final.transaction_public_id
    )


def test_genuine_unicode_source_and_raw_response_bytes_preserved(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    raw = "Lunch SGD 12.50 at Cafe\u0301 2026-10-08"
    _, _, _, intake, _, _ = _prepare_text_subject(
        c, suffix="unicode", raw_text=raw, merchant="Cafe\u0301"
    )
    fields = {**ORIGINAL, "merchant": "Cafe\u0301"}
    body = response(fields=fields, quote=raw)
    ai, provider = service(c, DurableProvider(c, response=body))
    prepared = ai.prepare_initial(intake["public_id"], "unicode-operation")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    result = ai.publish(prepared.attempt_id)
    p = posting(c, ai)
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, result.proposal_public_id, "unicode-confirm")
    final = p.submit_post(fresh.review_id, "unicode-confirm")
    assert final.state == "finalized"
    assert (
        c.execute(
            "SELECT raw_input FROM raw_intake_records WHERE public_id=?", (intake["public_id"],)
        ).fetchone()[0]
        == raw
    ), "STOP_ORIGINAL_EVIDENCE_CHANGED"
    from independent_ai_candidate_fixture_v2 import candidate_fixture_v2

    actual_wire = candidate_fixture_v2(body, provider.requests[0])
    assert (
        c.execute("SELECT raw_response FROM independent_ai_results").fetchone()[0] == actual_wire
    ), "STOP_ORIGINAL_EVIDENCE_CHANGED"
    assert any(op.get("value") == "Cafe\u0301" for op in json.loads(actual_wire)["operations"])
    assert c.execute("SELECT merchant FROM transactions").fetchone()[0] == "Café", (
        "STOP_FINANCIAL_FACT_MISMATCH"
    )


def test_new_ai_receipt_raw_converter_and_finalizer_noop_cannot_authenticate(tmp_path, monkeypatch):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    import finance_core.application.posting as posting_module
    from finance_core.parser_proposals.receipt_facts_conversion import (
        ReceiptFactsConversionCommand,
        convert_confirmed_receipt_proposal_to_facts,
    )
    from finance_core.receipt_finalization import fact_set_bridge

    c, _, _, _, _, _, intake, _, _, source_port, decision_port = _prepare_receipt_subject(
        tmp_path, suffix="ai-raw-receipt"
    )
    fields = {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
    provider = DurableProvider(
        c, response=response(fields=fields, quote="EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50")
    )
    ai = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=source_port,
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    attempt = ai.prepare_initial(intake, "raw-receipt-operation")
    ai.invoke(attempt.attempt_id)
    result = ai.publish(attempt.attempt_id)
    p = PostingService(
        connection=c,
        source_verifier=source_port,
        human_decision_authority=decision_port,
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, result.proposal_public_id, "raw-receipt-confirm")

    def interrupt(cut):
        if cut == "after_acceptance_commit":
            raise RuntimeError("synthetic interrupted after acceptance")

    monkeypatch.setattr(posting_module, "_inject", interrupt)
    with pytest.raises(RuntimeError):
        p.submit_post(fresh.review_id, "raw-receipt-confirm")
    command = ReceiptFactsConversionCommand(
        command_public_id="rpfc_raw_ai_receipt",
        proposal_public_id=result.proposal_public_id,
        expected_content_hash=result.effective_content_hash,
        payer_participant_public_id="ptcp_posting_self",
        participants=({"participant_public_id": "ptcp_posting_self", "is_included": 1},),
        authenticated_actor_id=BINDING.human_principal_id,
        channel="independent_application",
    )

    class NoopMetadata:
        def verify_in_transaction(self, *args, **kwargs):
            return None

        def persist_in_transaction(self, *args, **kwargs):
            return None

    with pytest.raises(AiInvocationError):
        convert_confirmed_receipt_proposal_to_facts(
            c, command, metadata_authority=NoopMetadata(), persistence_effect=lambda *_: None
        )
    assert c.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    monkeypatch.setattr(posting_module, "_inject", lambda *_: None)
    posting_attempt = c.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
    final = p.resume_post(posting_attempt)
    assert final.state == "finalized"
    authorization_id = c.execute(
        "SELECT evidence_public_id FROM application_posting_receipt_evidence "
        "WHERE evidence_type='authorization'"
    ).fetchone()[0]
    auth = fact_set_bridge.load_persisted_receipt_finalization_authorization(c, authorization_id)
    for call in (
        lambda: fact_set_bridge.finalize_prepared_receipt(
            c, auth, persistence_effect=lambda *_: None
        ),
        lambda: fact_set_bridge.verify_finalized_prepared_receipt(c, authorization_id),
    ):
        with pytest.raises(AiInvocationError):
            call()
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    c.close()


@pytest.mark.parametrize("tamper", ["relabel", "missing_publication", "partial_schema"])
def test_cross_origin_markers_cannot_downgrade_to_deterministic(
    migrated_temp_db_connection, tamper
):
    from test_application_posting_recovery_v1 import _temporarily_drop_triggers

    c = migrated_temp_db_connection
    ai, _, result, *_ = publish_initial(c)
    if tamper == "relabel":
        _temporarily_drop_triggers(
            c,
            ("independent_ai_parser_no_hash_update",),
            lambda: c.execute(
                "UPDATE parser_outputs SET parser_name='deterministic' WHERE public_id=?",
                (result.proposal_public_id,),
            ),
        )
    elif tamper == "missing_publication":
        _temporarily_drop_triggers(
            c,
            ("independent_ai_publications_no_delete",),
            lambda: c.execute("DELETE FROM independent_ai_publications"),
        )
    else:
        c.execute("DROP TRIGGER independent_ai_claims_no_update")
        c.commit()
    with pytest.raises(ValueError):
        get_proposal_review(c, result.proposal_public_id)
    with pytest.raises(ValueError):
        ai.get_status(result.attempt_id)
    with pytest.raises(ValueError):
        posting(c, ai).prepare(result.proposal_public_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_published_history_remains_verified_after_deadline_without_new_dispatch(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, result, *_ = publish_initial(c)
    later = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=lambda: NOW + 3600,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    assert later.recover(result.attempt_id) == result
    assert later.invoke(result.attempt_id) == result
    assert provider.calls == 1


def test_claim_race_two_real_connections_dispatches_once(migrated_temp_db_connection):
    import sqlite3
    import threading
    from queue import Queue

    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = subject(c, "claim-race")
    ai, _ = service(c)
    prepared = ai.prepare_initial(intake["public_id"], "race-operation")
    path = c.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2)
    out = Queue()

    def worker():
        connection = sqlite3.connect(path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        local, provider = service(connection)
        barrier.wait()
        try:
            out.put((local.invoke(prepared.attempt_id).state, provider.calls))
        except BaseException as exc:
            out.put(exc)
        finally:
            connection.close()

    workers = [threading.Thread(target=worker) for _ in range(2)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in workers)
    results = [out.get_nowait() for _ in workers]
    assert all(isinstance(item, tuple) for item in results), results
    assert sum(item[1] for item in results) == 1
    assert c.execute("SELECT count(*) FROM independent_ai_claims").fetchone()[0] == 1
    assert ai.publish(prepared.attempt_id).state == "PUBLISHED"


def test_publication_fault_rolls_back_all_edges_and_recovers_same_child(
    migrated_temp_db_connection, monkeypatch
):
    import finance_core.parser_proposals.independent_ai_publication as publication

    c = migrated_temp_db_connection
    _, _, parent, intake, _, _ = subject(c, "publication-crash")
    ai, provider = service(c)
    prepared = ai.prepare_initial(intake["public_id"], "crash-operation")
    ai.invoke(prepared.attempt_id)
    original = publication.append_financial_audit_event

    def fault(*args, **kwargs):
        raise RuntimeError("synthetic before publication commit")

    monkeypatch.setattr(publication, "append_financial_audit_event", fault)
    with pytest.raises(RuntimeError):
        ai.publish(prepared.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert (
        c.execute(
            "SELECT count(*) FROM parser_outputs WHERE parent_parser_output_id=?", (parent["id"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        c.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (intake["public_id"],),
        ).fetchone()[0]
        == parent["id"]
    )
    monkeypatch.setattr(publication, "append_financial_audit_event", original)
    result = ai.publish(prepared.attempt_id)
    assert ai.publish(prepared.attempt_id) == result
    assert provider.calls == 1


def test_late_response_retained_never_published(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = subject(c, "late")
    provider = DurableProvider(c, response=response(), at=NOW + 61)
    ai, _ = service(c, provider, clock=lambda: NOW)
    prepared = ai.prepare_initial(intake["public_id"], "late-operation")
    request = ai.claim(prepared.attempt_id)
    receipt = provider.dispatch(request)
    later = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=lambda: NOW + 61,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    assert later.record_outcome(prepared.attempt_id, receipt).state == "LATE"
    with pytest.raises(AiInvocationError):
        later.publish(prepared.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_results").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("raw", ["Coffee SGD5.80 2026-10-08", "咖啡SGD5.80 2026-10-08"])
def test_explicit_currency_without_spaces_has_unique_money_support(
    migrated_temp_db_connection, raw
):
    c = migrated_temp_db_connection
    _, _, _, intake, _, _ = _prepare_text_subject(c, suffix="compact-money", raw_text=raw)
    fields = {**ORIGINAL, "amount": "5.80", "merchant": "Coffee"}
    ai, provider = service(c, DurableProvider(c, response=response(fields=fields, quote=raw)))
    prepared = ai.prepare_initial(intake["public_id"], "compact-money-operation")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    result = ai.publish(prepared.attempt_id)
    assert posting(c, ai).prepare(result.proposal_public_id)


def test_nonempty_actual_receipt_057_to_058_preserves_old_bytes_and_posts(tmp_path, monkeypatch):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    import finance_core.reconciliation.migrations as migrations

    full = migrations.TEMP_DB_MIGRATION_PATHS
    monkeypatch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", full[:57])
    c, workspace, manifest, _, _, proposal, intake, _, _, _, _ = _prepare_receipt_subject(
        tmp_path, suffix="upgrade-old-receipt", retain_synthetic_authority=False
    )
    tables = (
        "receipt_ocr_proposal_links",
        "receipt_ocr_extractions",
        "receipt_ocr_blocks",
        "raw_intake_records",
        "attachments",
        "local_attachment_source",
        "parser_outputs",
        "parser_proposal_field_evidence",
        "financial_audit_events",
    )
    before = {
        table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in tables
    }
    ledger = [
        tuple(row)
        for row in c.execute("SELECT * FROM schema_migrations ORDER BY migration_sequence")
    ]
    assert len(ledger) == 57
    assert len(before["receipt_ocr_proposal_links"]) == 1
    assert before["receipt_ocr_proposal_links"][0][7] == "initial"
    migrations.apply_migration_paths(c, full)
    assert {
        table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in tables
    } == before
    assert [
        tuple(row)
        for row in c.execute(
            "SELECT * FROM schema_migrations WHERE migration_sequence<=57 "
            "ORDER BY migration_sequence"
        )
    ] == ledger
    assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(full)
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert get_proposal_review(c, proposal)["proposal_origin"] == "deterministic"
    from test_application_posting_recovery_v1 import SOURCE_KEY, _digest

    for table in (
        "synthetic_sources",
        "synthetic_displays",
        "synthetic_decisions",
        "synthetic_replies",
        "synthetic_display_closures",
    ):
        c.execute(
            f"CREATE TABLE {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    raw = c.execute(
        "SELECT raw_input FROM raw_intake_records WHERE public_id=?", (intake,)
    ).fetchone()[0]
    import hashlib

    source = {
        "schema": "finance-application-source-v1",
        "namespace": BINDING.source_namespace,
        "key_id": BINDING.source_key_id,
        "instance_id": BINDING.instance_id,
        "submission_client_id": BINDING.submission_client_id,
        "evidence_id": "upgrade-old-source",
        "intake_public_id": intake,
        "source_event_id": "upgrade-old-event",
        "source_content_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "source_occurred_at": NOW - 2,
        "received_at": NOW - 1,
    }
    source["evidence_digest"] = _digest(source)
    _persist(c, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
    c.commit()
    p = PostingService(
        connection=c,
        source_verifier=_DurableSourceVerifier(workspace=workspace, manifest=manifest),
        human_decision_authority=_DurablePostingDecisionAuthority(),
        binding=BINDING,
        clock=lambda: NOW,
    )
    review = p.prepare(proposal)
    _persist_fresh_posting_confirmation(c, review, proposal, "upgrade-fresh-confirm")
    final = p.submit_post(review.review_id, "upgrade-fresh-confirm")
    assert final.state == "finalized"
    assert p.get_status(final.attempt_id) == final
    assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (12.5, "SGD")
    c.close()


def test_actual_sealed_legacy_ai_057_to_058_keeps_old_domain_without_new_port(
    tmp_path, monkeypatch
):
    import openclaw_staging_bridge_support_v1 as support
    from test_application_amendment_ai_v1 import _create_sealed_ai_root

    import finance_core.reconciliation.migrations as migrations
    from finance_core.parser_proposals.ai_fallback import verify_ai_fallback_child
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash

    full = migrations.TEMP_DB_MIGRATION_PATHS
    monkeypatch.setattr(support, "TEMP_DB_MIGRATION_PATHS", full[:57])
    # The real workspace opener independently verifies the same explicit
    # historical manifest; no custody/claim/result verifier is replaced.
    monkeypatch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", full[:57])
    c, _, legacy_proposal = _create_sealed_ai_root(tmp_path)
    proposal_id = str(legacy_proposal["public_id"])
    monkeypatch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", full)
    tables = (
        "ai_fallback_attempts",
        "ai_fallback_invocation_claims",
        "ai_fallback_results",
        "ai_fallback_proposal_links",
        "parser_outputs",
        "parser_proposal_field_evidence",
        "raw_intake_records",
    )
    before = {
        table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in tables
    }
    ledger = [
        tuple(row)
        for row in c.execute("SELECT * FROM schema_migrations ORDER BY migration_sequence")
    ]
    assert len(ledger) == 57
    migrations.apply_migration_paths(c, full)
    assert {
        table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in tables
    } == before
    assert [
        tuple(row)
        for row in c.execute(
            "SELECT * FROM schema_migrations WHERE migration_sequence<=57 "
            "ORDER BY migration_sequence"
        )
    ] == ledger
    proposal = dict(
        c.execute("SELECT * FROM parser_outputs WHERE public_id=?", (proposal_id,)).fetchone()
    )
    verified = verify_ai_fallback_child(
        c,
        proposal,
        content_hash=compute_effective_proposal_content_hash(c, proposal),
        proposal_version=0,
        require_resolved=False,
    )
    assert verified["proposal_origin"] == "ai_fallback"
    assert get_proposal_review(c, proposal_id)["proposal_origin"] == "ai_fallback"
    assert c.execute("SELECT count(*) FROM independent_ai_attempts").fetchone()[0] == 0
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def default_context_response(raw):
    fields = {
        **ORIGINAL,
        "amount": "5.80",
        "currency": "SGD",
        "transaction_date": "1970-01-01",
        "merchant": "Cafe",
    }
    evidence = {k: {"ref": "source", "quote": raw} for k in fields}
    for field in ("currency", "transaction_date"):
        evidence[field] = {"ref": "source_context", "quote": fields[field]}
    return json.dumps(
        {"schema": AI_RESULT_SCHEMA, "kind": "initial", "fields": fields, "evidence": evidence}
    )


def test_genuine_no_date_no_currency_initial_uses_fixed_source_context_then_posts(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    raw = "Coffee 5.80 at Cafe"
    _, _, _, intake, _, _ = _prepare_text_subject(c, suffix="default-context", raw_text=raw)
    ai, provider = service(c, DurableProvider(c, response=default_context_response(raw)))
    prepared = ai.prepare_initial(intake["public_id"], "default-context-operation")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    result = ai.publish(prepared.attempt_id)
    context = provider.requests[0]["input"]["interpretation_context"]
    assert context["default_currency"] == "SGD"
    assert context["local_date"] == "1970-01-01"
    assert context["anchor_kind"] == "source" and context["timezone"] == "UTC"
    p = posting(c, ai)
    review = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, review, result.proposal_public_id, "default-context-confirm"
    )
    assert p.submit_post(review.review_id, "default-context-confirm").state == "finalized"
    assert tuple(
        c.execute("SELECT amount,currency,transaction_date FROM transactions").fetchone()
    ) == (5.8, "SGD", "1970-01-01")


@pytest.mark.parametrize(
    "cue", ["last Tuesday", "上个月", "去年", "前日", "2026/08/02", "2026年8月2日"]
)
def test_relative_absolute_date_cue_cannot_be_erased_by_source_context(cue):
    from finance_core.parser_proposals.independent_ai_validation import validate_response

    raw = f"Coffee 5.80 at Cafe {cue}"
    projection = {
        "kind": "initial",
        "source_text": raw,
        "selection": {"kind": "text"},
        "source_context": {"currency": "SGD", "transaction_date": "1970-01-01"},
    }
    assert validate_response(default_context_response(raw), projection, {})["state"] != "VALID"


# Consolidated candidate-A review-fix behavioral regressions.
def _review_fix_receipt(tmp_path, *, merchant="EXAMPLE CAFE", extra_amounts=(), **receipt_options):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    c, _, _, _, _, parent, intake, _, _, source_port, decision_port = _prepare_receipt_subject(
        tmp_path,
        suffix="review-fix",
        merchant_text=merchant,
        prepare_review=False,
        extra_amounts=extra_amounts,
        **receipt_options,
    )
    fields = {**ORIGINAL, "merchant": merchant, "description": "Receipt total"}
    quote = merchant + "\n2026-10-08\nTOTAL\nSGD\n12.50"
    provider = DurableProvider(c, response=response(fields=fields, quote=quote))
    ai = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=source_port,
        interpretation_input_verifier=DurableInputVerifier(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    return c, ai, provider, parent, intake, source_port, decision_port


def _review_fix_refuses_publication(c, ai, attempt, *, state="NEEDS_CLARIFICATION"):
    assert ai.invoke(attempt.attempt_id).state == state
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("intent", ["REFUND", "INCOME", "TRANSFER"])
def test_review_fix_initial_ocr_unsupported_intent(tmp_path, intent):
    c, ai, _, _, intake, *_ = _review_fix_receipt(tmp_path, merchant=intent + " FROM EXAMPLE CAFE")
    attempt = ai.prepare_initial(intake, "review-fix-initial")
    _review_fix_refuses_publication(c, ai, attempt, state="RESPONSE_REJECTED")
    c.close()


@pytest.mark.parametrize(
    "reply,patch",
    [
        ("Change amount to SGD 5.80 or SGD 9.80", {"amount": "5.80"}),
        ("Change amount to SGD 5.80 or USD 5.80", {"amount": "5.80"}),
        ("Change amount to SGD 5.80 or MYR 5.80", {"amount": "5.80"}),
        ("Change date to 2026-10-08 or 2026-10-09", {"transaction_date": "2026-10-09"}),
        ("Change amount to SGD 5.80 and refund this expense", {"amount": "5.80"}),
        ("Change amount to SGD 5.80 as income", {"amount": "5.80"}),
        ("Change amount to SGD 5.80 as transfer", {"amount": "5.80"}),
    ],
)
def test_review_fix_receipt_reply_financial_readiness(tmp_path, reply, patch):
    c, ai, provider, parent, _, source_port, _ = _review_fix_receipt(tmp_path)
    human = AmendmentService(
        connection=c,
        source_verifier=source_port,
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    review = human.prepare(parent)
    identity = persist_interpretation_reply(c, review, reply)
    provider.response = response("patch", patch, reply)
    attempt = ai.prepare_edit(review.review_id, identity, "review-fix-edit")
    _review_fix_refuses_publication(
        c,
        ai,
        attempt,
        state="RESPONSE_REJECTED"
        if any(word in reply for word in ("refund", "income", "transfer"))
        else "NEEDS_CLARIFICATION",
    )
    c.close()


@pytest.mark.parametrize("currency", ["MYR", "RM", "INR", "XYZ"])
def test_review_fix_unknown_competing_currency_has_no_publication(
    migrated_temp_db_connection, currency
):
    c = migrated_temp_db_connection
    raw = f"Lunch SGD 12.50 or {currency} 12.50 at Example Cafe 2026-10-08"
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="unknown-currency", raw_text=raw)
    ai, _ = service(c, DurableProvider(c, response=response(quote=raw)))
    _review_fix_refuses_publication(c, ai, ai.prepare_initial(intake["public_id"], "unknown"))


@pytest.mark.parametrize(
    "stage", ["prepare", "claim", "invoke", "result", "publish", "cancel", "recover"]
)
@pytest.mark.parametrize("caller_transaction", [False, True])
def test_review_fix_fk_off_refuses_without_rows_or_dispatch(
    migrated_temp_db_connection, stage, caller_transaction
):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "fk-review")
    ai, provider = service(c)
    attempt = None
    receipt = None
    if stage != "prepare":
        attempt = ai.prepare_initial(intake["public_id"], "fk-review")
    if stage == "result":
        request = ai.claim(attempt.attempt_id)
        receipt = provider.dispatch(request)
    if stage == "publish":
        assert ai.invoke(attempt.attempt_id).state == "VALID"
    if stage == "recover":
        ai.claim(attempt.attempt_id)
    before = _review_fix_owner_rows(c)
    calls = provider.calls
    c.execute("PRAGMA foreign_keys=OFF")
    if caller_transaction:
        c.execute("CREATE TEMP TABLE caller_pending(value TEXT)")
        c.execute("BEGIN")
        c.execute("INSERT INTO caller_pending VALUES ('retained')")
    with pytest.raises(AiInvocationError, match="foreign"):
        if stage == "prepare":
            ai.prepare_initial(intake["public_id"], "fk-review")
        elif stage == "result":
            ai.record_outcome(attempt.attempt_id, receipt)
        else:
            getattr(ai, stage)(attempt.attempt_id)
    assert provider.calls == calls
    assert _review_fix_owner_rows(c) == before
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert c.in_transaction is caller_transaction
    if caller_transaction:
        assert c.execute("SELECT value FROM caller_pending").fetchone()[0] == "retained"
        c.rollback()


def _review_fix_owner_rows(c):
    return {
        table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in (
            "independent_ai_attempts",
            "independent_ai_claims",
            "independent_ai_events",
            "independent_ai_results",
            "independent_ai_publications",
            "parser_outputs",
            "raw_intake_records",
            "financial_audit_events",
            "transactions",
        )
    }


@pytest.mark.parametrize("separator", ["\t", "\n", "\r"])
@pytest.mark.parametrize("path", ["source", "reply"])
def test_review_fix_privacy_scans_actual_strings_before_escape(
    migrated_temp_db_connection, separator, path
):
    c = migrated_temp_db_connection
    sensitive = "Bearer" + separator + "abcdefghijklmnopqrstuvwxyz012345"
    raw = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    _, _, parent, intake, *_ = _prepare_text_subject(
        c,
        suffix="privacy-review",
        raw_text=raw + (" " + sensitive if path == "source" else ""),
    )
    ai, provider = service(c)
    before = tuple(
        c.execute(
            "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
            (intake["public_id"],),
        ).fetchone()
    )
    if path == "source":

        def operation():
            return ai.prepare_initial(intake["public_id"], "privacy-review")
    else:
        review = amendment(c, ai).prepare(parent["public_id"])
        reply = persist_interpretation_reply(
            c, review, "Change amount to SGD 5.80 " + (sensitive if path == "reply" else "")
        )

        def operation():
            return ai.prepare_edit(review.review_id, reply, "privacy-review")

    with pytest.raises(AiInvocationError, match="sensitive_text"):
        operation()
    assert provider.calls == 0
    for table in (
        "independent_ai_attempts",
        "independent_ai_claims",
        "independent_ai_publications",
    ):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert (
        tuple(
            c.execute(
                "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
                (intake["public_id"],),
            ).fetchone()
        )
        == before
    )


def test_review_fix_parser_copy_cannot_substitute_authenticated_raw_source(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    _, _, parent, intake, *_ = subject(c, "source-copy-review")
    before = tuple(
        c.execute(
            "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
            (intake["public_id"],),
        ).fetchone()
    )
    c.execute(
        "UPDATE parser_outputs SET raw_text=? WHERE id=?",
        ("Lunch SGD 99.00 at Example Cafe 2026-10-08", parent["id"]),
    )
    c.commit()
    ai, provider = service(c)
    with pytest.raises(AiInvocationError, match="source"):
        ai.prepare_initial(intake["public_id"], "source-copy-review")
    assert provider.calls == 0
    assert c.execute("SELECT count(*) FROM independent_ai_attempts").fetchone()[0] == 0
    assert (
        tuple(
            c.execute(
                "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
                (intake["public_id"],),
            ).fetchone()
        )
        == before
    )


def test_review_fix_normal_receipt_total_retains_multiple_noncompeting_amounts(tmp_path):
    c, ai, _, _, intake, *_ = _review_fix_receipt(
        tmp_path,
        merchant="EXAMPLE CAFE",
        extra_amounts=(("SUBTOTAL", "10.00"), ("TAX", "2.50"), ("CASH", "20.00")),
    )
    attempt = ai.prepare_initial(intake, "review-fix-normal-receipt")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    assert ai.publish(attempt.attempt_id).state == "PUBLISHED"
    c.close()


def test_review_fix_merchant_abc_real_total_posts_once(tmp_path):
    from test_independent_ai_v2 import finish_post

    c, ai, provider, _, intake, source, decision = _review_fix_receipt(
        tmp_path,
        merchant="ABC",
        extra_amounts=(("SUBTOTAL", "10.00"), ("TAX", "2.50"), ("CASH", "20.00")),
    )
    try:
        attempt = ai.prepare_initial(intake, "review-fix-merchant-abc")
        assert ai.invoke(attempt.attempt_id).state == "VALID"
        publication = ai.publish(attempt.attempt_id)
        finish_post(c, ai, publication, "merchant-abc-confirm", source=source, decision=decision)
        assert tuple(c.execute("SELECT amount,currency,merchant FROM transactions").fetchone()) == (
            12.5,
            "SGD",
            "ABC",
        ), "STOP_FINANCIAL_FACT_MISMATCH"
        assert provider.calls == 1
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
    finally:
        c.close()


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_review_fix_fk_off_edit_prepare_preserves_caller_work(
    migrated_temp_db_connection, caller_transaction
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "fk-edit")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    reply = persist_interpretation_reply(c, review, "Change amount to SGD 5.80")
    before = _review_fix_owner_rows(c)
    c.execute("PRAGMA foreign_keys=OFF")
    if caller_transaction:
        c.execute("BEGIN")
    with pytest.raises(AiInvocationError, match="foreign"):
        ai.prepare_edit(review.review_id, reply, "fk-edit")
    assert provider.calls == 0
    assert _review_fix_owner_rows(c) == before
    assert c.in_transaction is caller_transaction
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    if caller_transaction:
        c.rollback()


@pytest.mark.parametrize("published", [False, True])
def test_review_fix_original_source_is_rechecked_for_claim_recovery_and_history(
    migrated_temp_db_connection, published
):
    c = migrated_temp_db_connection
    _, _, parent, intake, *_ = subject(c, "source-history-review")
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "source-history-review")
    if published:
        assert ai.invoke(attempt.attempt_id).state == "VALID"
        result = ai.publish(attempt.attempt_id)
    raw_before = tuple(
        c.execute(
            "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
            (intake["public_id"],),
        ).fetchone()
    )
    # A forged-cache fixture restores the original DDL after deliberate drift;
    # no origin, source signature or invocation/result evidence is fabricated.
    trigger = c.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", ("independent_ai_parser_no_hash_update",)
    ).fetchone()[0]
    c.execute("DROP TRIGGER independent_ai_parser_no_hash_update")
    c.execute(
        "UPDATE parser_outputs SET raw_text=? WHERE id=?",
        ("Lunch SGD 99.00 at Example Cafe 2026-10-08", parent["id"]),
    )
    c.execute(trigger)
    c.commit()
    before = _review_fix_owner_rows(c)
    calls = provider.calls
    operations = [lambda: ai.get_status(attempt.attempt_id), lambda: ai.recover(attempt.attempt_id)]
    if published:
        operations.append(lambda: ai.verify_lineage(result.proposal_public_id))
    else:
        operations.append(lambda: ai.invoke(attempt.attempt_id))
    for operation in operations:
        with pytest.raises(AiInvocationError, match="(?i)source"):
            operation()
    assert provider.calls == calls
    assert _review_fix_owner_rows(c) == before
    assert (
        tuple(
            c.execute(
                "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
                (intake["public_id"],),
            ).fetchone()
        )
        == raw_before
    )


@pytest.mark.parametrize("credential_kind", ["Bearer", "Basic"])
def test_review_fix_actual_ocr_sensitive_input_never_claims(tmp_path, credential_kind):
    # Two individually valid OCR blocks become newline-separated selected text;
    # this uses the real OCR owner, without injecting forbidden control chars.
    c, ai, provider, _, intake, *_ = _review_fix_receipt(
        tmp_path, extra_amounts=((credential_kind, "abcdefghijklmnopqrstuvwxyz012345"),)
    )
    before = [tuple(row) for row in c.execute("SELECT * FROM receipt_ocr_blocks ORDER BY id")]
    with pytest.raises(AiInvocationError, match="sensitive_text"):
        ai.prepare_initial(intake, "ocr-privacy-review")
    assert provider.calls == 0
    assert c.execute("SELECT count(*) FROM independent_ai_claims").fetchone()[0] == 0
    assert [
        tuple(row) for row in c.execute("SELECT * FROM receipt_ocr_blocks ORDER BY id")
    ] == before
    c.close()


def test_review_fix_sensitive_current_field_refuses_before_claim(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, parent, _, *_ = _prepare_text_subject(
        c,
        suffix="sensitive-current",
        raw_text="Lunch SGD 12.50 at Example Cafe 2026-10-08",
        merchant="Bearer abcdefghijklmnopqrstuvwxyz012345",
    )
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    reply = persist_interpretation_reply(c, review, "Change amount to SGD 5.80")
    with pytest.raises(AiInvocationError, match="sensitive_text"):
        ai.prepare_edit(review.review_id, reply, "sensitive-current")
    assert provider.calls == 0
    assert c.execute("SELECT count(*) FROM independent_ai_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("unknown_cue", ["yesterday", "October eighth"])
def test_review_fix_natural_date_patch_does_not_drop_unknown_date_candidate(
    migrated_temp_db_connection, unknown_cue
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "date-cue-review")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    text = "Change date to 2026-10-09 or " + unknown_cue
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", {"transaction_date": "2026-10-09"}, text)
    attempt = ai.prepare_edit(review.review_id, reply, "date-cue-review")
    _review_fix_refuses_publication(c, ai, attempt)


@pytest.mark.parametrize(
    "text,patch",
    [
        ("Change date to 2026-10-09", {"transaction_date": "2026-10-09"}),
        ("Change description to October special", {"description": "October special"}),
    ],
)
def test_review_fix_pure_iso_date_and_description_words_remain_supported(
    migrated_temp_db_connection, text, patch
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "date-positive-review")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", patch, text)
    attempt = ai.prepare_edit(review.review_id, reply, "date-positive-review")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    result = ai.publish(attempt.attempt_id)
    assert result.state == "PUBLISHED"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


# Candidate C: actual source-money qualification, not model-selected numbers.
def _source_money_quote(c):
    return "\n".join(
        row[0]
        for row in c.execute(
            "SELECT normalized_text FROM receipt_ocr_blocks ORDER BY sequence_index"
        )
    )


def _source_money_no_child(c, ai, attempt, parent_id):
    status = ai.invoke(attempt.attempt_id)
    assert status.state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    assert ai.get_status(attempt.attempt_id).state == status.state
    assert ai.recover(attempt.attempt_id).state == status.state
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT parser_output_id FROM raw_intake_records").fetchone()[0] == parent_id


@pytest.mark.parametrize("amount", ["10.00", "2.50", "20.00"])
def test_source_money_initial_receipt_cannot_select_subtotal_tax_cash(tmp_path, amount):
    c, ai, provider, parent, intake, *_ = _review_fix_receipt(
        tmp_path, extra_amounts=(("SUBTOTAL", "10.00"), ("TAX", "2.50"), ("CASH", "20.00"))
    )
    provider.response = response(
        fields={**ORIGINAL, "amount": amount}, quote=_source_money_quote(c)
    )
    attempt = ai.prepare_initial(intake, "source-money-role")
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    _source_money_no_child(c, ai, attempt, parent_id)
    c.close()


@pytest.mark.parametrize(
    "notation",
    ["-5.80", "−5.80", "- 5.80", "-SGD 5.80", "SGD -5.80", "(SGD 5.80)", "SGD 5.80-", "+5.80"],
)
@pytest.mark.parametrize("operation", ["initial", "edit"])
def test_source_money_signed_notation_is_not_positive_support(
    migrated_temp_db_connection, notation, operation
):
    c = migrated_temp_db_connection
    raw = "Lunch SGD " + notation + " at Example Cafe 2026-10-08"
    if operation == "initial":
        _, _, parent, intake, *_ = _prepare_text_subject(c, raw_text=raw, suffix="source-sign")
        ai, provider = service(c)
        provider.response = response(fields={**ORIGINAL, "amount": "5.80"}, quote=raw)
        attempt = ai.prepare_initial(intake["public_id"], "source-sign")
    else:
        _, _, parent, *_ = subject(c, "source-sign")
        ai, provider = service(c)
        review = amendment(c, ai).prepare(parent["public_id"])
        reply = persist_interpretation_reply(c, review, raw)
        provider.response = response("patch", {"amount": "5.80"}, raw)
        attempt = ai.prepare_edit(review.review_id, reply, "source-sign")
    _source_money_no_child(c, ai, attempt, parent["id"])


@pytest.mark.parametrize("operation", ["initial", "edit"])
def test_source_money_truncated_quote_cannot_drop_negative_sign(
    migrated_temp_db_connection, operation
):
    c = migrated_temp_db_connection
    raw = "Lunch SGD -5.80 at Example Cafe 2026-10-08"
    if operation == "initial":
        _, _, parent, intake, *_ = _prepare_text_subject(c, raw_text=raw, suffix="truncated-sign")
        ai, provider = service(c)
        body = json.loads(response(fields={**ORIGINAL, "amount": "5.80"}, quote=raw))
        body["evidence"]["amount"]["quote"] = "5.80"
        provider.response = json.dumps(body)
        attempt = ai.prepare_initial(intake["public_id"], "truncated-sign")
    else:
        _, _, parent, *_ = subject(c, "truncated-sign")
        ai, provider = service(c)
        review = amendment(c, ai).prepare(parent["public_id"])
        reply = persist_interpretation_reply(c, review, raw)
        provider.response = response("patch", {"amount": "5.80"}, "5.80")
        attempt = ai.prepare_edit(review.review_id, reply, "truncated-sign")
    _source_money_no_child(c, ai, attempt, parent["id"])


@pytest.mark.parametrize(
    "receipt_options",
    [
        {"total_label": "SUBTOTAL"},
        {"extra_amounts": (("TOTAL", "20.00"),)},
        {"total_currency": None, "extra_amounts": (("CASH SGD", "20.00"),)},
        {"total_currency": "$", "extra_amounts": (("CASH SGD", "20.00"),)},
        {"total_currency": "MYR", "extra_amounts": (("CASH SGD", "20.00"),)},
    ],
)
def test_source_money_missing_conflicting_unknown_total_stays_pending(tmp_path, receipt_options):
    c, ai, provider, parent, intake, *_ = _review_fix_receipt(tmp_path, **receipt_options)
    quote = _source_money_quote(c)
    provider.response = response(fields=ORIGINAL, quote=quote)
    attempt = ai.prepare_initial(intake, "source-total-missing")
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    _source_money_no_child(c, ai, attempt, parent_id)
    c.close()


@pytest.mark.parametrize("total_notation", ["-5.80", "−5.80", "- 5.80", "5.80-", "(5.80)"])
def test_source_money_receipt_total_signed_notation_is_not_positive(tmp_path, total_notation):
    c, ai, provider, parent, intake, *_ = _review_fix_receipt(tmp_path, total_amount=total_notation)
    provider.response = response(
        fields={**ORIGINAL, "amount": "5.80"}, quote=_source_money_quote(c)
    )
    attempt = ai.prepare_initial(intake, "source-total-sign")
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    _source_money_no_child(c, ai, attempt, parent_id)
    c.close()


@pytest.mark.parametrize("model_amount", ["12.50", "20.00"])
def test_source_money_initial_does_not_overwrite_current_human_amount(tmp_path, model_amount):
    c, ai, provider, parent, intake, source_port, _ = _review_fix_receipt(
        tmp_path, extra_amounts=(("CASH", "20.00"),)
    )
    human = AmendmentService(
        connection=c,
        source_verifier=source_port,
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    review = human.prepare(parent)
    changed = _apply(
        c,
        human,
        review,
        {"amount": "20.00"},
        evidence_id="source-human-money",
        amendment_id="source-human-amendment",
    )
    provider.response = response(
        fields={**ORIGINAL, "amount": model_amount}, quote=_source_money_quote(c)
    )
    attempt = ai.prepare_initial(intake, "source-after-human")
    parent_id = c.execute(
        "SELECT id FROM parser_outputs WHERE public_id=?", (changed.proposal_public_id,)
    ).fetchone()[0]
    _source_money_no_child(c, ai, attempt, parent_id)
    assert get_proposal_review(c, changed.proposal_public_id)["amount"] == "20.00"
    c.close()


@pytest.mark.parametrize("edit", [False, True])
def test_source_money_correct_total_and_positive_exact_edit_actually_post_once(tmp_path, edit):
    c, ai, provider, _, intake, source_port, decision_port = _review_fix_receipt(
        tmp_path,
        extra_amounts=(
            ("SUBTOTAL", "10.00"),
            ("TAX", "2.50"),
            ("CASH", "20.00"),
            ("DISCOUNT", "-1.00"),
        ),
    )
    provider.response = response(
        fields={**ORIGINAL, "merchant": "EXAMPLE CAFE"}, quote=_source_money_quote(c)
    )
    attempt = ai.prepare_initial(intake, "source-correct-total")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    result = ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    if edit:
        human = AmendmentService(
            connection=c,
            source_verifier=source_port,
            human_amendment_authority=DurableSyntheticAmendmentAuthority(),
            binding=_EDIT_BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        review = human.prepare(result.proposal_public_id)
        text = "Change amount to 20.00"
        reply = persist_interpretation_reply(c, review, text)
        provider.response = response("patch", {"amount": "20.00"}, text)
        attempt = ai.prepare_edit(review.review_id, reply, "source-positive-edit")
        assert ai.invoke(attempt.attempt_id).state == "VALID"
        result = ai.publish(attempt.attempt_id)
        assert provider.requests[-1]["input"]["source_text"] == ""
        assert provider.requests[-1]["input"]["current_values"]["currency"] == "SGD"
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    p = PostingService(
        connection=c,
        source_verifier=source_port,
        human_decision_authority=decision_port,
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, result.proposal_public_id, "source-money-fresh")
    final = p.submit_post(fresh.review_id, "source-money-fresh")
    assert final.state == "finalized"
    assert p.get_status(final.attempt_id) == final
    assert p.resume_post(final.attempt_id) == final
    assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (
        20 if edit else 12.5,
        "SGD",
    )
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM authoritative_calculation_snapshots").fetchone()[0] == 1
    c.close()


@pytest.mark.parametrize("notation", ["-$5.80", "-S$5.80"])
def test_source_money_currency_symbol_does_not_hide_reply_sign(
    migrated_temp_db_connection, notation
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "symbol-sign")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    text = "Change amount to SGD " + notation
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", {"amount": "5.80"}, "5.80")
    attempt = ai.prepare_edit(review.review_id, reply, "symbol-sign")
    _source_money_no_child(c, ai, attempt, parent["id"])


def test_source_money_positive_dollar_edit_uses_exact_known_card_currency(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, initial, *_ = publish_initial(c, "positive-dollar")
    review = amendment(c, ai).prepare(initial.proposal_public_id)
    text = "Change amount to $5.80"
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", {"amount": "5.80"}, text)
    attempt = ai.prepare_edit(review.review_id, reply, "positive-dollar")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    result = ai.publish(attempt.attempt_id)
    assert provider.requests[-1]["input"]["current_values"]["currency"] == "SGD"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    p = posting(c, ai)
    fresh = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, fresh, result.proposal_public_id, "positive-dollar-confirm"
    )
    final = p.submit_post(fresh.review_id, "positive-dollar-confirm")
    assert final.state == "finalized"
    assert p.get_status(final.attempt_id) == final
    assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (5.8, "SGD")


@pytest.mark.parametrize("notation", ["US$5.80", "-US$5.80", "$SGD5.80"])
def test_source_money_decorated_dollar_is_not_bare_card_currency(
    migrated_temp_db_connection, notation
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "decorated-dollar")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    text = "Change amount to " + notation
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", {"amount": "5.80"}, "5.80")
    attempt = ai.prepare_edit(review.review_id, reply, "decorated-dollar")
    _source_money_no_child(c, ai, attempt, parent["id"])


def _source_money_forged_drift(c, table, mutation, parameters=()):
    triggers = c.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for name, _ in triggers:
        c.execute('DROP TRIGGER "' + name + '"')
    c.execute(mutation, parameters)
    for _, definition in triggers:
        c.execute(definition)
    c.commit()


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_amount_evidence",
        "wrong_amount_evidence",
        "normalized_hash",
        "geometry",
        "initial_role",
        "parser_contract",
    ],
)
def test_source_money_rebuild_refuses_original_ocr_proof_drift(tmp_path, tamper):
    from finance_core.parser_proposals.independent_ai_lineage import verify_publication

    c, ai, provider, parent, intake, *_ = _review_fix_receipt(tmp_path)
    provider.response = response(fields=ORIGINAL, quote=_source_money_quote(c))
    attempt = ai.prepare_initial(intake, "source-proof-drift")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    ai.publish(attempt.attempt_id)
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    if tamper == "missing_amount_evidence":
        _source_money_forged_drift(
            c,
            "parser_proposal_field_evidence",
            "DELETE FROM parser_proposal_field_evidence WHERE parser_output_id=? "
            "AND field_name='amount'",
            (parent_id,),
        )
    elif tamper == "wrong_amount_evidence":
        _source_money_forged_drift(
            c,
            "parser_proposal_field_evidence",
            "UPDATE parser_proposal_field_evidence SET proposed_value='20.00' "
            "WHERE parser_output_id=? AND field_name='amount'",
            (parent_id,),
        )
    elif tamper == "normalized_hash":
        _source_money_forged_drift(
            c,
            "receipt_ocr_extractions",
            "UPDATE receipt_ocr_extractions SET normalized_result_hash=?",
            ("f" * 64,),
        )
    elif tamper == "geometry":
        _source_money_forged_drift(
            c,
            "receipt_ocr_blocks",
            "UPDATE receipt_ocr_blocks SET coordinate_left=coordinate_left+1 "
            "WHERE sequence_index=4",
        )
    elif tamper == "initial_role":
        _source_money_forged_drift(
            c,
            "receipt_ocr_proposal_links",
            "UPDATE receipt_ocr_proposal_links SET link_role='superseding_correction' "
            "WHERE parser_output_id=?",
            (parent_id,),
        )
    else:
        _source_money_forged_drift(
            c,
            "receipt_ocr_proposal_links",
            "UPDATE receipt_ocr_proposal_links SET parser_contract_version='unknown-contract' "
            "WHERE parser_output_id=?",
            (parent_id,),
        )
    # Mandatory schema DDL is restored exactly; the new proof must detect the
    # actual evidence change, not just a removed-trigger fingerprint.
    before = _review_fix_owner_rows(c)
    calls = provider.calls
    edge = c.execute(
        "SELECT * FROM independent_ai_publications WHERE attempt_id=?", (attempt.attempt_id,)
    ).fetchone()
    with pytest.raises(AiInvocationError):
        verify_publication(c, edge)
    for operation in (
        lambda: ai.get_status(attempt.attempt_id),
        lambda: ai.recover(attempt.attempt_id),
        lambda: ai.publish(attempt.attempt_id),
    ):
        with pytest.raises((AiInvocationError, ValueError, RuntimeError)):
            operation()
    assert _review_fix_owner_rows(c) == before
    assert provider.calls == calls
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    c.close()


@pytest.mark.parametrize("currency", ["SGD", "USD"])
def test_source_money_explicit_currency_edit_remains_supported(
    migrated_temp_db_connection, currency
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "explicit-code")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    text = "Change amount and currency to " + currency + " 5.80"
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", {"amount": "5.80", "currency": currency}, text)
    attempt = ai.prepare_edit(review.review_id, reply, "explicit-code")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    assert ai.publish(attempt.attempt_id).state == "PUBLISHED"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_source_money_cached_base_cannot_erase_authentic_human_override(tmp_path):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    c, ai, _, parent, intake, source_port, _ = _review_fix_receipt(tmp_path)
    human = AmendmentService(
        connection=c,
        source_verifier=source_port,
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    review = human.prepare(parent)
    changed = _apply(
        c,
        human,
        review,
        {"amount": "20.00"},
        evidence_id="cache-human",
        amendment_id="cache-amendment",
    )
    attempt = ai.prepare_initial(intake, "cache-override")
    material = json.loads(
        c.execute(
            "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()[0]
    )
    material["base_payload"]["amount"] = "12.50"
    _source_money_forged_drift(
        c,
        "independent_ai_attempts",
        "UPDATE independent_ai_attempts SET material_json=?,material_hash=? WHERE attempt_id=?",
        (canonical_text(material), sha(material), attempt.attempt_id),
    )
    with pytest.raises(AiInvocationError, match="source base"):
        ai.invoke(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_claims").fetchone()[0] == 0
    assert get_proposal_review(c, changed.proposal_public_id)["amount"] == "20.00"
    c.close()


# Candidate D: closed raw source grammar; these first six cases expose old C.
@pytest.mark.parametrize("sign", ["\uff0d", "\ufe63"])
@pytest.mark.parametrize("operation", ["initial", "receipt_edit"])
def test_source_grammar_unicode_negative_never_qualifies_positive(
    tmp_path, migrated_temp_db_connection, sign, operation
):
    if operation == "receipt_edit":
        c, ai, provider, parent, _, _, _ = _review_fix_receipt(tmp_path)
        review = amendment(c, ai).prepare(parent)
        parent = {
            "id": c.execute(
                "SELECT id FROM parser_outputs WHERE public_id=?", (parent,)
            ).fetchone()[0]
        }
        raw = "Change amount to SGD " + sign + "5.80"
        reply = persist_interpretation_reply(c, review, raw)
        provider.response = response("patch", {"amount": "5.80"}, "5.80")
        attempt = ai.prepare_edit(review.review_id, reply, "grammar-negative")
    else:
        c = migrated_temp_db_connection
        raw = "Coffee SGD " + sign + "5.80 2026-10-08"
        _, _, parent, intake, *_ = _prepare_text_subject(c, raw_text=raw, suffix="grammar-negative")
        ai, provider = service(c)
        body = json.loads(response(fields={**ORIGINAL, "amount": "5.80"}, quote=raw))
        body["evidence"]["amount"]["quote"] = "5.80"
        provider.response = json.dumps(body)
        attempt = ai.prepare_initial(intake["public_id"], "grammar-negative")
    _source_money_no_child(c, ai, attempt, parent["id"])
    assert provider.calls == 1
    c.close()


@pytest.mark.parametrize("notation", ["\uff35\uff33\uff24 5.80", "\uff045.80"])
def test_source_grammar_unicode_currency_cannot_be_absent_default(
    migrated_temp_db_connection, notation
):
    c = migrated_temp_db_connection
    raw = "Cafe " + notation
    _, _, parent, intake, *_ = _prepare_text_subject(c, raw_text=raw, suffix="grammar-currency")
    ai, provider = service(c, DurableProvider(c, response=default_context_response(raw)))
    attempt = ai.prepare_initial(intake["public_id"], "grammar-currency")
    _source_money_no_child(c, ai, attempt, parent["id"])
    assert provider.calls == 1
    c.close()


@pytest.mark.parametrize(
    "raw", ["Coffee SGD5.80 2026-10-08", "咖啡SGD5.80 2026-10-08", "Coffee 5.80 at Cafe"]
)
def test_source_grammar_supported_raw_text_actually_posts_once(migrated_temp_db_connection, raw):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = _prepare_text_subject(c, raw_text=raw, suffix="grammar-positive")
    body = (
        default_context_response(raw)
        if raw == "Coffee 5.80 at Cafe"
        else response(fields={**ORIGINAL, "amount": "5.80"}, quote=raw)
    )
    ai, provider = service(c, DurableProvider(c, response=body))
    attempt = ai.prepare_initial(intake["public_id"], "grammar-positive")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    result = ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert ai.verify_lineage(result.proposal_public_id)
    assert ai.get_status(attempt.attempt_id) == result
    assert ai.recover(attempt.attempt_id) == result
    assert provider.requests[0]["input"]["source_text"] == raw
    assert c.execute("SELECT raw_input FROM raw_intake_records").fetchone()[0] == raw, (
        "STOP_ORIGINAL_EVIDENCE_CHANGED"
    )
    retained = json.loads(c.execute("SELECT material FROM synthetic_ai_receipts").fetchone()[0])
    from independent_ai_candidate_fixture_v2 import candidate_fixture_v2

    actual_wire = candidate_fixture_v2(body, provider.requests[0])
    assert retained["observation"]["raw_response"] == actual_wire
    assert (
        c.execute("SELECT raw_response FROM independent_ai_results").fetchone()[0] == actual_wire
    ), "STOP_ORIGINAL_EVIDENCE_CHANGED"
    p = posting(c, ai)
    review = p.prepare(result.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, review, result.proposal_public_id, "grammar-positive-fresh"
    )
    final = p.submit_post(review.review_id, "grammar-positive-fresh")
    assert final.state == "finalized"
    assert p.get_status(final.attempt_id) == final
    assert p.resume_post(final.attempt_id) == final
    assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (
        5.8,
        "SGD",
    ), "STOP_FINANCIAL_FACT_MISMATCH"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert provider.calls == 1


@pytest.mark.parametrize("sign", ["\uff0d", "\ufe63"])
def test_source_grammar_selected_receipt_total_unicode_sign_stays_pending(tmp_path, sign):
    c, ai, provider, parent, intake, *_ = _review_fix_receipt(tmp_path, total_amount=sign + "5.80")
    body = json.loads(response(fields={**ORIGINAL, "amount": "5.80"}, quote=_source_money_quote(c)))
    body["evidence"]["amount"]["quote"] = "5.80"
    provider.response = json.dumps(body)
    attempt = ai.prepare_initial(intake, "grammar-receipt-negative")
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    _source_money_no_child(c, ai, attempt, parent_id)
    assert provider.calls == 1
    c.close()


def test_source_grammar_unknown_card_dollar_edit_never_defaults(tmp_path):
    c, ai, provider, parent, _, *_ = _review_fix_receipt(tmp_path, total_currency=None)
    review = amendment(c, ai).prepare(parent)
    text = "Change amount to $5.80"
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", {"amount": "5.80", "currency": "SGD"}, text)
    attempt = ai.prepare_edit(review.review_id, reply, "grammar-unknown-dollar")
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    _source_money_no_child(c, ai, attempt, parent_id)
    assert provider.calls == 1
    c.close()


@pytest.mark.parametrize("modifier", ["minus SGD 5.80", "负5.80 SGD"])
def test_source_grammar_literal_modifier_is_not_unsigned_positive(
    migrated_temp_db_connection, modifier
):
    c = migrated_temp_db_connection
    raw = "Coffee " + modifier + " 2026-10-08"
    _, _, parent, intake, *_ = _prepare_text_subject(c, raw_text=raw, suffix="grammar-modifier")
    ai, provider = service(c)
    body = json.loads(response(fields={**ORIGINAL, "amount": "5.80"}, quote=raw))
    body["evidence"]["amount"]["quote"] = "5.80"
    provider.response = json.dumps(body)
    attempt = ai.prepare_initial(intake["public_id"], "grammar-modifier")
    _source_money_no_child(c, ai, attempt, parent["id"])
    assert provider.calls == 1


@pytest.mark.parametrize("sign", ["\uff0d", "\ufe63"])
def test_source_grammar_good_total_cannot_hide_unknown_duplicate_total(tmp_path, sign):
    c, ai, provider, parent, intake, *_ = _review_fix_receipt(
        tmp_path, extra_amounts=(("TOTAL " + sign + " SGD", "12.50"), ("DISCOUNT", "-1.00"))
    )
    provider.response = response(fields=ORIGINAL, quote=_source_money_quote(c))
    # The real legacy owner deduplicates both canonical positive pairs. New
    # source qualification must inspect both original TOTAL role spans.
    original = json.loads(
        c.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id=?", (parent,)
        ).fetchone()[0]
    )
    assert original["amount"] == "12.50"
    assert original["currency"] == "SGD"
    attempt = ai.prepare_initial(intake, "grammar-duplicate-total")
    parent_id = c.execute("SELECT id FROM parser_outputs WHERE public_id=?", (parent,)).fetchone()[
        0
    ]
    _source_money_no_child(c, ai, attempt, parent_id)
    assert provider.calls == 1
    c.close()
