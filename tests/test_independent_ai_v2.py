"""Real Core services, signed synthetic transport and complete v2 interactions."""

from __future__ import annotations

import dataclasses
import json

import pytest
from test_application_amendment_v1 import _persist_fresh_posting_confirmation
from test_application_posting_recovery_v1 import (
    BINDING,
    NOW,
    _DurableSourceVerifier,
    _load,
    _persist,
)
from test_independent_ai_invocation_v1 import (
    KEY,
    ORIGINAL,
    DurableProvider,
    posting,
    subject,
)

from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.ai_invocation_contract import (
    AI_INTERPRETATION_SCHEMA,
    AI_RESULT_SCHEMA,
    AiInvocationError,
    InterpretationPolicy,
    VerifiedInterpretationInputV2,
)
from finance_core.application.posting_contract import posting_review_sha256
from finance_core.parser_proposals.independent_ai_v2 import compute_date
from finance_core.parser_proposals.independent_ai_validation import sha


class InputV2:
    def verify_persisted(self, c, identity, expected):
        proof = VerifiedInterpretationInputV2(
            **_load(c, "synthetic_interpretation_replies", identity, KEY)
        )
        display = _load(c, "synthetic_interpretation_displays", proof.display_id, KEY)
        if (
            display["status"] != "SENT"
            or display["interaction_id"] != expected.interaction_id
            or display["interaction_hash"] != expected.interaction_projection_hash
            or sha(display) != proof.display_evidence_digest
        ):
            raise AiInvocationError("Actual SENT interaction differs")
        projection = display["projection"]
        digest = (
            sha(projection)
            if expected.interaction_kind == "clarification_followup"
            else posting_review_sha256(projection)
        )
        if digest != expected.interaction_projection_hash:
            raise AiInvocationError("Complete delivered interaction differs")
        if (
            proof.source_evidence_digest != expected.source.evidence_digest
            or proof.proposal_content_hash != expected.proposal_content_hash
        ):
            raise AiInvocationError("Exact source/target differs")
        return proof


def service_v2(c, body, clock=lambda: NOW, timezone="UTC"):
    provider = DurableProvider(c, response=body)
    return AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=InputV2(),
        provider=provider,
        clock=clock,
        interpretation_policy=InterpretationPolicy(timezone, "SGD"),
    ), provider


def cite(text, quote=None, ref="source", role="replacement_value"):
    quote = text if quote is None else quote
    start = text.index(quote)
    return {"ref": ref, "start": start, "end": start + len(quote), "quote": quote, "role": role}


def wire(
    text,
    fields=None,
    *,
    ref="source",
    date_expression=None,
    unresolved=None,
    outcome="proposal",
    evidence=None,
):
    fields = ORIGINAL if fields is None else fields
    operations = []
    for i, (field, value) in enumerate(fields.items()):
        op = {
            "operation_id": "op" + str(i),
            "op": "set",
            "field": field,
            "evidence": (evidence or {}).get(field, [cite(text, ref=ref)]),
        }
        if field == "transaction_date":
            y, m, d = map(int, value.split("-"))
            op["date_expression"] = date_expression or {
                "kind": "absolute_date",
                "year": y,
                "month": m,
                "day": d,
            }
            # Only the date expression quote is removed from money partition.
            if evidence is None or field not in evidence:
                op["evidence"] = [cite(text, value, ref)]
        else:
            op["value"] = value
        operations.append(op)
    questions = unresolved or []
    coverage = [
        {
            "start": 0,
            "end": len(text),
            "label": "unresolved_instruction" if questions else "resolved_instruction",
            "operation_ids": [] if questions else [op["operation_id"] for op in operations],
            "question_ids": [q["question_id"] for q in questions],
        }
    ]
    return json.dumps(
        {
            "schema": AI_RESULT_SCHEMA,
            "outcome": outcome,
            "intent": "personal_expense",
            "operations": operations,
            "coverage": coverage,
            "unresolved": questions,
        }
    )


def persist_reply(c, review, text, identity="v2-reply", at=NOW):
    for table in ("synthetic_interpretation_replies", "synthetic_interpretation_displays"):
        c.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY,material TEXT NOT NULL,signature TEXT NOT NULL)"
        )
    clarification = hasattr(review, "request_id")
    interaction = review.request_id if clarification else review.review_id
    digest = review.request_hash if clarification else review.review_hash
    projection = dict(review.projection)
    source = projection["source"]
    display = {
        "status": "SENT",
        "interaction_id": interaction,
        "interaction_hash": digest,
        "projection": projection,
    }
    display_id = "v2-display-" + identity
    _persist(c, "synthetic_interpretation_displays", display_id, display, KEY)
    proof = VerifiedInterpretationInputV2(
        AI_INTERPRETATION_SCHEMA,
        BINDING.instance_id,
        BINDING.human_principal_id,
        identity,
        sha(text),
        source["evidence_id"],
        source["evidence_digest"],
        "clarification_followup" if clarification else "card_edit",
        interaction,
        digest,
        projection["proposal_public_id"],
        projection["proposal_version"],
        projection["effective_content_hash"],
        display_id,
        sha(display),
        display_id,
        text,
        at,
        at + 900,
    )
    _persist(c, "synthetic_interpretation_replies", identity, dataclasses.asdict(proof), KEY)
    c.commit()
    return identity


def test_v2_text_full_card_once_post(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "v2-full")
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    ai, provider = service_v2(c, wire(text))
    prepared = ai.prepare_initial(intake["public_id"], "v2-full-op")
    assert ai.invoke(prepared.attempt_id).state == "VALID"
    published = ai.publish(prepared.attempt_id)
    owner = posting(c, ai)
    review = owner.prepare(published.proposal_public_id)
    assert review.projection["schema"] == "finance-application-posting-review-v2"
    _persist_fresh_posting_confirmation(c, review, published.proposal_public_id, "v2-confirm")
    final = owner.submit_post(review.review_id, "v2-confirm")
    assert final.state == "finalized"
    assert owner.resume_post(final.attempt_id) == final
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert provider.calls == 1


@pytest.mark.parametrize(
    "expression,expected",
    [
        ({"kind": "day_offset", "days": -1}, "2026-10-08"),
        ({"kind": "calendar_week_weekday", "week_offset": 0, "iso_weekday": 1}, "2026-10-05"),
        ({"kind": "calendar_week_weekday", "week_offset": -1, "iso_weekday": 7}, "2026-10-04"),
        (
            {"kind": "previous_or_next_weekday", "direction": "previous", "iso_weekday": 5},
            "2026-10-02",
        ),
        ({"kind": "previous_or_next_weekday", "direction": "next", "iso_weekday": 5}, "2026-10-16"),
    ],
)
def test_v2_python_finite_dates(expression, expected):
    assert (
        compute_date(
            expression,
            {"anchor_timestamp": 1791504000, "timezone": "UTC", "local_date": "2026-10-09"},
        )
        == expected
    )


def test_v2_clarification_followup_new_full_card(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "v2-clarify")
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    question = {
        "question_id": "q_amount",
        "fields": ["amount"],
        "reason": "ambiguous",
        "candidates": ["12.50", "15.00"],
        "evidence": [cite(text)],
    }
    ai, provider = service_v2(c, wire(text, {}, unresolved=[question], outcome="clarification"))
    first = ai.prepare_initial(intake["public_id"], "v2-question-op")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    question_review = ai.prepare_clarification(first.attempt_id)
    assert question_review.projection["confirm_available"] is False
    assert ai.prepare_clarification(first.attempt_id) == question_review
    reply = "Use SGD 15.00"
    proof = persist_reply(c, question_review, reply)
    provider.response = wire(reply, {"amount": "15.00"}, ref="reply")
    followup = ai.prepare_followup(question_review.request_id, proof, "v2-answer-op")
    assert ai.get_clarification(question_review.request_id).state == "CONSUMED"
    assert ai.invoke(followup.attempt_id).state == "VALID"
    published = ai.publish(followup.attempt_id)
    assert ai.recover(published.attempt_id) == published
    assert provider.calls == 2
    assert c.execute("SELECT count(*) FROM independent_ai_followup_links").fetchone()[0] == 1
    with pytest.raises(AiInvocationError):
        ai.prepare_followup(question_review.request_id, proof, "v2-answer-again")
    owner = posting(c, ai)
    fresh = owner.prepare(published.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, fresh, published.proposal_public_id, "clarification-final-confirm"
    )
    final = owner.submit_post(fresh.review_id, "clarification-final-confirm")
    assert owner.resume_post(final.attempt_id) == final
    assert tuple(
        c.execute("SELECT amount,currency,transaction_date,merchant FROM transactions").fetchone()
    ) == (15.0, "SGD", "2026-10-08", "Example Cafe"), "STOP_FINANCIAL_FACT_MISMATCH"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )


@pytest.mark.parametrize(
    "crash_stage",
    [
        None,
        "after_acceptance_commit",
        "after_conversion_commit",
        "after_fact_set_commit",
        "after_snapshot_commit",
        "after_conditional_authorization_commit",
        "after_receipt_finalization_commit",
    ],
)
def test_v2_low_ocr_full_card_real_receipt_once_post(tmp_path, monkeypatch, crash_stage):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    from finance_core.application.posting import PostingService

    c, _, _, _, _, parent, intake, _, _, source_port, decision_port = _prepare_receipt_subject(
        tmp_path, suffix="v2-low-ocr", ocr_confidence_scaled=3000, prepare_review=False
    )
    try:
        text = "EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50"
        fields = {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
        # Money references exact actual TOTAL role span rather than non-total
        # numbers from the rest of the receipt.
        evidence = {
            "amount": [cite(text, "12.50")],
            "currency": [cite(text, "SGD")],
            "transaction_date": [cite(text, "2026-10-08")],
        }
        provider = DurableProvider(c, response=wire(text, fields, evidence=evidence))
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source_port,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        initial = ai.prepare_initial(intake, "v2-low-ocr-op")
        assert ai.invoke(initial.attempt_id).state == "VALID"
        child = ai.publish(initial.attempt_id)
        owner = PostingService(
            connection=c,
            binding=BINDING,
            source_verifier=source_port,
            human_decision_authority=decision_port,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        review = owner.prepare(child.proposal_public_id)
        assert review.projection["expense_assessment"]["hard_blockers"] == []
        assert (
            "historical_low_ocr_or_parser_confidence"
            in review.projection["expense_assessment"]["review_warnings"]
        )
        _persist_fresh_posting_confirmation(c, review, child.proposal_public_id, "v2-low-confirm")
        if crash_stage:
            from finance_core.application import posting as posting_module

            def crash(stage):
                if stage == crash_stage:
                    raise RuntimeError("synthetic durable stage crash")

            monkeypatch.setattr(posting_module, "_failure_injection_hook", crash)
            with pytest.raises(RuntimeError, match="synthetic durable stage crash"):
                owner.submit_post(review.review_id, "v2-low-confirm")
            monkeypatch.setattr(posting_module, "_failure_injection_hook", None)
            attempt_id = c.execute(
                "SELECT attempt_id FROM application_posting_attempts"
            ).fetchone()[0]
            final = owner.resume_post(attempt_id)
        else:
            final = owner.submit_post(review.review_id, "v2-low-confirm")
        assert final.state == "finalized"
        assert owner.resume_post(final.attempt_id) == final
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
        assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
    finally:
        c.close()


def _v2_rule_amendment_service(connection, ai, *, source_verifier=None):
    from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
    from test_application_amendment_v1 import _EDIT_BINDING
    from test_application_posting_recovery_v1 import _DurableSourceVerifier

    from finance_core.application.amendment import AmendmentService

    return AmendmentService(
        connection=connection,
        source_verifier=source_verifier or _DurableSourceVerifier(),
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )


def _v2_rule_assert_local_only_edit_request(
    connection, provider, attempt_id, denied_text, *, marker_in_current_value=False
):
    import hashlib

    material = json.loads(
        connection.execute(
            "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
    )
    request_json = material["request_json"]
    assert hashlib.sha256(request_json.encode("utf-8")).hexdigest() == material["request_hash"]
    request = json.loads(request_json)
    projection = request["input"]
    assert material["projection"]["source_text"] == ""
    assert projection["source_text"] == ""
    assert set(projection) == {
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
    current_fields = {
        "amount",
        "currency",
        "transaction_date",
        "merchant",
        "description",
        "category",
    }
    current_values = projection["current_values"]
    assert set(current_values) == current_fields
    assert current_values == material["projection"]["current_values"]
    if denied_text is not None:
        marker_in_values = any(
            denied_text.casefold() in str(value).casefold()
            for value in current_values.values()
            if value is not None
        )
        assert marker_in_values is marker_in_current_value
        if not marker_in_current_value:
            assert denied_text.casefold() not in request_json.casefold()

    selection = material["projection"]["selection"]
    if selection["kind"] == "local_ocr":
        source_rows = connection.execute(
            "SELECT normalized_text FROM receipt_ocr_blocks WHERE extraction_id=("
            "SELECT id FROM receipt_ocr_extractions WHERE public_id=?) ORDER BY sequence_index",
            (selection["extraction_id"],),
        ).fetchall()
        complete_local_source = "\n".join(str(row[0]) for row in source_rows)
    else:
        source_row = connection.execute(
            "SELECT raw_input FROM raw_intake_records WHERE public_id=?",
            (material["intake_public_id"],),
        ).fetchone()
        assert source_row is not None
        complete_local_source = str(source_row[0])
    assert complete_local_source
    assert complete_local_source not in request_json
    escaped_local_source = json.dumps(complete_local_source, ensure_ascii=False)[1:-1]
    assert escaped_local_source not in request_json
    assert provider.requests[-1]["input"] == projection


def _v2_rule_finish_post_with_fresh_signed_confirmation(connection, ai, published):
    for table in (
        "synthetic_displays",
        "synthetic_decisions",
        "synthetic_replies",
        "synthetic_display_closures",
    ):
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    connection.commit()
    owner = posting(connection, ai)
    card = owner.prepare(published.proposal_public_id)
    decision_id = "synthetic-ai-amendment-fresh-posting"
    _persist_fresh_posting_confirmation(connection, card, published.proposal_public_id, decision_id)
    final = owner.submit_post(card.review_id, decision_id)
    assert final.state == "finalized"
    assert owner.resume_post(final.attempt_id) == final
    assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert connection.execute("SELECT public_id FROM transactions").fetchone()[0] == (
        final.transaction_public_id
    ), "STOP_FINANCIAL_FACT_MISMATCH"
    return card, final


def _v2_rule_assert_no_publication_or_financial_facts(connection):
    for table in (
        "independent_ai_publications",
        "transactions",
        "receipts",
        "receipt_item_allocation_fact_sets",
        "authoritative_calculation_snapshots",
        "receipt_finalization_authorizations",
        "parser_proposal_authorizations",
        "application_posting_attempts",
        "application_posting_decisions",
    ):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


@pytest.mark.parametrize(
    "source_owner,token",
    [("original", "chargeback"), ("human", "refund")],
)
def test_v2_rule_service_legacy_amendment_rechecks_hidden_original_intent(
    migrated_temp_db_connection, source_owner, token
):
    from test_application_amendment_v1 import _apply
    from test_independent_ai_invocation_v1 import (
        DurableProvider as V1InputDurableProvider,
    )
    from test_independent_ai_invocation_v1 import (
        persist_interpretation_reply,
    )
    from test_independent_ai_invocation_v1 import (
        service as service_with_v1_input_proof,
    )

    c = migrated_temp_db_connection
    raw = f"Lunch SGD 12.50 at Example Cafe 2026-10-08 {token}"
    intake, parent = _v2_rule_prepare_text_source(
        c,
        suffix=f"legacy-hidden-{source_owner}-{token.encode('utf-8').hex()}",
        raw_text=raw,
    )
    ai, provider = service_with_v1_input_proof(c, V1InputDurableProvider(c, response="{}"))
    owner = _v2_rule_amendment_service(c, ai)
    proposal_public_id = parent["public_id"]
    if source_owner == "human":
        human_review = owner.prepare(proposal_public_id)
        amended = _apply(
            c,
            owner,
            human_review,
            {"description": "Human note"},
            evidence_id="legacy-hidden-human-" + token.encode("utf-8").hex(),
            amendment_id="legacy-hidden-human-" + token.encode("utf-8").hex(),
        )
        proposal_public_id = amended.proposal_public_id

    review = owner.prepare(proposal_public_id)
    assert review.projection["schema"] == "finance-application-amendment-review-v1"
    reply = "Change category to travel"
    proof = persist_interpretation_reply(
        c, review, reply, identity=f"legacy-hidden-reply-{source_owner}"
    )
    provider.response = wire(reply, {"category": "travel"}, ref="reply")
    prepared = ai.prepare_edit(review.review_id, proof, f"legacy-hidden-op-{source_owner}")
    invoked = ai.invoke(prepared.attempt_id)
    assert (invoked.state, invoked.reason) == ("RESPONSE_REJECTED", "unsupported_intent")
    _v2_rule_assert_local_only_edit_request(c, provider, prepared.attempt_id, token)
    assert ai.get_status(prepared.attempt_id) == invoked
    assert ai.recover(prepared.attempt_id) == invoked
    with pytest.raises(AiInvocationError):
        ai.publish(prepared.attempt_id)
    _v2_rule_assert_no_publication_or_financial_facts(c)
    assert provider.calls == 1
    source_row = c.execute(
        "SELECT raw.public_id FROM parser_outputs AS parser "
        "JOIN raw_intake_records AS raw "
        "ON raw.public_id=parser.source_public_id WHERE parser.public_id=?",
        (parent["public_id"],),
    ).fetchone()
    assert source_row is not None
    assert source_row[0] == intake["public_id"]


@pytest.mark.parametrize("source_owner", ["original", "human", "independent_ai"])
def test_v2_rule_service_legacy_amendment_ordinary_source_owners_edit_and_post_once(
    migrated_temp_db_connection, source_owner
):
    from test_application_amendment_v1 import _apply
    from test_independent_ai_invocation_v1 import (
        persist_interpretation_reply,
    )
    from test_independent_ai_invocation_v1 import (
        service as service_with_v1_input_proof,
    )

    c = migrated_temp_db_connection
    if source_owner == "independent_ai":
        ai, provider, initial = published_text(c, "legacy-amend-independent-control")
        ai, provider = service_with_v1_input_proof(c, provider)
        proposal_public_id = initial.proposal_public_id
        expected_calls = 2
    else:
        _, parent = _v2_rule_prepare_text_source(
            c,
            suffix="legacy-amend-" + source_owner,
            raw_text="Lunch SGD 12.50 at Example Cafe 2026-10-08",
        )
        ai, provider = service_with_v1_input_proof(c, DurableProvider(c, response="{}"))
        proposal_public_id = parent["public_id"]
        expected_calls = 1

    owner = _v2_rule_amendment_service(c, ai)
    if source_owner == "human":
        review_before_human = owner.prepare(proposal_public_id)
        amended = _apply(
            c,
            owner,
            review_before_human,
            {"description": "Human lunch"},
            evidence_id="legacy-amend-human-control",
            amendment_id="legacy-amend-human-control",
        )
        proposal_public_id = amended.proposal_public_id

    review = owner.prepare(proposal_public_id)
    assert review.projection["schema"] == "finance-application-amendment-review-v1"
    reply = "Change merchant to Cafe Two"
    proof = persist_interpretation_reply(
        c, review, reply, identity="legacy-amend-positive-" + source_owner
    )
    provider.response = wire(reply, {"merchant": "Cafe Two"}, ref="reply")
    prepared = ai.prepare_edit(review.review_id, proof, "legacy-amend-positive-op-" + source_owner)
    invoked = ai.invoke(prepared.attempt_id)
    assert invoked.state == "VALID", invoked
    _v2_rule_assert_local_only_edit_request(
        c,
        provider,
        prepared.attempt_id,
        "Lunch SGD 12.50 at Example Cafe 2026-10-08",
    )
    published = ai.publish(prepared.attempt_id)
    assert published.state == "PUBLISHED"
    card, final = _v2_rule_finish_post_with_fresh_signed_confirmation(c, ai, published)
    values = card.projection["financial_projection"]
    row = c.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
        "FROM transactions"
    ).fetchone()
    assert tuple(row) == (
        float(values["amount"]),
        values["currency"],
        values["transaction_date"],
        values["merchant"],
        values["description"],
        values["category"],
        final.transaction_public_id,
    ), "STOP_FINANCIAL_FACT_MISMATCH"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert provider.calls == expected_calls


def test_v2_rule_service_legacy_ai_amendment_ordinary_source_still_posts_once(tmp_path):
    from test_application_amendment_ai_v1 import (
        _create_sealed_ai_root,
        _persist_application_source,
    )
    from test_independent_ai_invocation_v1 import (
        DurableProvider as V1InputDurableProvider,
    )
    from test_independent_ai_invocation_v1 import (
        persist_interpretation_reply,
    )
    from test_independent_ai_invocation_v1 import (
        service as service_with_v1_input_proof,
    )

    from finance_core.application.review import get_proposal_review

    c, intake_public_id, parent = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(c, intake_public_id)
        assert get_proposal_review(c, parent["public_id"])["proposal_origin"] == "ai_fallback"
        ai, provider = service_with_v1_input_proof(c, V1InputDurableProvider(c, response="{}"))
        review = _v2_rule_amendment_service(c, ai).prepare(parent["public_id"])
        assert review.projection["schema"] == "finance-application-amendment-review-v1"
        reply = "Change merchant to Cafe Two"
        proof = persist_interpretation_reply(
            c, review, reply, identity="legacy-ai-amendment-positive"
        )
        provider.response = wire(reply, {"merchant": "Cafe Two"}, ref="reply")
        prepared = ai.prepare_edit(review.review_id, proof, "legacy-ai-amendment-positive-op")
        invoked = ai.invoke(prepared.attempt_id)
        assert invoked.state == "VALID", invoked
        _v2_rule_assert_local_only_edit_request(
            c,
            provider,
            prepared.attempt_id,
            "paid SGD 12.34 at Cafe on 2026-08-13",
        )
        published = ai.publish(prepared.attempt_id)
        card, final = _v2_rule_finish_post_with_fresh_signed_confirmation(c, ai, published)
        values = card.projection["financial_projection"]
        row = c.execute(
            "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
            "FROM transactions"
        ).fetchone()
        assert tuple(row) == (
            float(values["amount"]),
            values["currency"],
            values["transaction_date"],
            values["merchant"],
            values["description"],
            values["category"],
            final.transaction_public_id,
        ), "STOP_FINANCIAL_FACT_MISMATCH"
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
        assert provider.calls == 1
    finally:
        c.close()


@pytest.mark.parametrize("cue", ["in +3 days", "in 3.5 days"])
@pytest.mark.parametrize("date_case", ["omitted", "source_context_default", "wrong_unsigned"])
def test_v2_rule_service_ocr_signed_decimal_offset_keeps_date_question(tmp_path, cue, date_case):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    stable_cue = cue.replace("+", "plus").replace(".", "dot").replace(" ", "-")
    suffix = f"rule-signed-decimal-{stable_cue}-{date_case}"
    c, _, _, _, _, _, intake, _, _, source, _decision = _prepare_receipt_subject(
        tmp_path,
        suffix=suffix,
        date_text=cue,
        prepare_review=False,
    )
    try:
        provider = DurableProvider(c, response="{}")
        base_dispatch = provider.dispatch

        def dispatch_with_unsupported_date(request):
            projection = json.loads(request.request_json)["input"]
            text = projection["source_text"]
            assert cue in text, (cue, text)
            fields = {
                **ORIGINAL,
                "merchant": "EXAMPLE CAFE",
                "description": "Receipt total",
            }
            evidence = None
            expression = None
            if date_case == "source_context_default":
                context = projection["interpretation_context"]
                fields["transaction_date"] = context["local_date"]
                evidence = {
                    "transaction_date": [
                        cite(
                            canonical_text(context),
                            context["local_date"],
                            "source_context",
                        )
                    ]
                }
            elif date_case == "wrong_unsigned":
                context = projection["interpretation_context"]
                anchor = datetime.fromtimestamp(
                    context["anchor_timestamp"], ZoneInfo(context["timezone"])
                ).date()
                fields["transaction_date"] = (anchor + timedelta(days=3)).isoformat()
                expression = {"kind": "day_offset", "days": 3}
                evidence = {"transaction_date": [cite(text, cue)]}
            else:
                assert date_case == "omitted"
                fields.pop("transaction_date", None)
            provider.response = wire(
                text,
                fields,
                date_expression=expression,
                evidence=evidence,
            )
            return base_dispatch(request)

        provider.dispatch = dispatch_with_unsupported_date
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        prepared = ai.prepare_initial(intake, "rule-signed-decimal-op")
        invoked = ai.invoke(prepared.attempt_id)
        assert invoked.state == "NEEDS_CLARIFICATION", invoked
        clarification = ai.prepare_clarification(prepared.attempt_id)
        unresolved = {
            field for item in clarification.projection["unresolved"] for field in item["fields"]
        }
        assert "transaction_date" in unresolved, clarification.projection
        assert clarification.projection["confirm_available"] is False
        _v2_rule_assert_no_publication_or_financial_facts(c)
        assert provider.calls == 1
    finally:
        c.close()


@pytest.mark.parametrize(
    "merchant_text,extra_amounts,marker_in_current_value",
    [
        ("EXAMPLE CAFE chargeback", (), True),
        ("EXAMPLE CAFE", (("NOTE chargeback", "source-only"),), False),
    ],
)
def test_v2_rule_service_ocr_non_total_original_intent_is_hidden_and_rechecked(
    tmp_path, merchant_text, extra_amounts, marker_in_current_value
):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject
    from test_independent_ai_invocation_v1 import (
        DurableInputVerifier,
        persist_interpretation_reply,
    )

    c, _, _, _, _, parent_id, _intake, _, _, source_verifier, _decision = _prepare_receipt_subject(
        tmp_path,
        suffix="rule-ocr-nontotal-intent",
        merchant_text=merchant_text,
        extra_amounts=extra_amounts,
        prepare_review=False,
    )
    try:
        blocks = [
            row[0]
            for row in c.execute(
                "SELECT normalized_text FROM receipt_ocr_blocks ORDER BY sequence_index"
            )
        ]
        if marker_in_current_value:
            assert blocks[0] == "EXAMPLE CAFE chargeback"
        else:
            assert blocks[0] == "EXAMPLE CAFE"
            assert "NOTE chargeback" in blocks
            assert "source-only" in blocks
        assert all(block != "TOTAL" for block in blocks if "chargeback" in block.casefold())
        assert "TOTAL" in blocks
        provider = DurableProvider(c, response="{}")
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source_verifier,
            interpretation_input_verifier=DurableInputVerifier(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        owner = _v2_rule_amendment_service(c, ai, source_verifier=source_verifier)
        review = owner.prepare(parent_id)
        reply = "Change category to travel"
        proof = persist_interpretation_reply(c, review, reply, identity="rule-ocr-nontotal-reply")
        provider.response = wire(reply, {"category": "travel"}, ref="reply")
        prepared = ai.prepare_edit(review.review_id, proof, "rule-ocr-nontotal-op")
        invoked = ai.invoke(prepared.attempt_id)
        assert (invoked.state, invoked.reason) == ("RESPONSE_REJECTED", "unsupported_intent")
        _v2_rule_assert_local_only_edit_request(
            c,
            provider,
            prepared.attempt_id,
            "chargeback",
            marker_in_current_value=marker_in_current_value,
        )
        assert ai.get_status(prepared.attempt_id) == invoked
        assert ai.recover(prepared.attempt_id) == invoked
        with pytest.raises(AiInvocationError):
            ai.publish(prepared.attempt_id)
        _v2_rule_assert_no_publication_or_financial_facts(c)
        assert provider.calls == 1
    finally:
        c.close()


def _v2_rule_schema_ddl(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type,name"
        )
    ]


def _v2_rule_controlled_ocr_source_mutation(connection, corruption, extraction_id):
    import sqlite3

    if corruption == "block_text":
        table = "receipt_ocr_blocks"
        select_sql = (
            "SELECT normalized_text FROM receipt_ocr_blocks WHERE extraction_id=("
            "SELECT id FROM receipt_ocr_extractions WHERE public_id=?) AND sequence_index=0"
        )
        update_sql = (
            "UPDATE receipt_ocr_blocks SET normalized_text=normalized_text||' altered' "
            "WHERE extraction_id=(SELECT id FROM receipt_ocr_extractions "
            "WHERE public_id=?) AND sequence_index=0"
        )
        update_params = (extraction_id,)
        expected_changed_value = None
        expected_direct_error = "receipt_ocr_blocks rows are append-only"
    elif corruption == "normalized_hash":
        table = "receipt_ocr_extractions"
        select_sql = "SELECT normalized_result_hash FROM receipt_ocr_extractions WHERE public_id=?"
        update_sql = "UPDATE receipt_ocr_extractions SET normalized_result_hash=? WHERE public_id=?"
        update_params = ("0" * 64, extraction_id)
        expected_changed_value = "0" * 64
        expected_direct_error = "receipt_ocr_extractions rows are append-only"
    else:
        assert corruption == "attachment_hash"
        table = "receipt_ocr_extractions"
        select_sql = "SELECT source_attachment_hash FROM receipt_ocr_extractions WHERE public_id=?"
        update_sql = "UPDATE receipt_ocr_extractions SET source_attachment_hash=? WHERE public_id=?"
        update_params = ("0" * 64, extraction_id)
        expected_changed_value = "0" * 64
        expected_direct_error = "receipt_ocr_extractions rows are append-only"

    if table == "receipt_ocr_extractions":
        row_sql = "SELECT * FROM receipt_ocr_extractions WHERE public_id=?"
    else:
        row_sql = (
            "SELECT * FROM receipt_ocr_blocks WHERE extraction_id=("
            "SELECT id FROM receipt_ocr_extractions WHERE public_id=?) AND sequence_index=0"
        )

    def read_target():
        row = connection.execute(select_sql, (extraction_id,)).fetchone()
        assert row is not None
        return str(row[0])

    def read_row():
        row = connection.execute(row_sql, (extraction_id,)).fetchone()
        assert row is not None
        return tuple(row)

    original_value = read_target()
    original_row = read_row()
    trigger_rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table,),
        )
    ]
    assert trigger_rows and all(name and ddl for name, ddl in trigger_rows)
    schema_before = _v2_rule_schema_ddl(connection)

    with pytest.raises(sqlite3.IntegrityError, match=expected_direct_error):
        connection.execute(update_sql, update_params)
    assert read_target() == original_value
    assert read_row() == original_row

    savepoint = "v2_rule_controlled_ocr_mutation"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        for name, _ddl in trigger_rows:
            connection.execute(f'DROP TRIGGER "{name}"')
        if corruption == "block_text":
            connection.execute(update_sql, update_params)
            expected_changed_value = original_value + " altered"
        else:
            connection.execute(update_sql, update_params)
        assert read_target() == expected_changed_value
        for _name, ddl in trigger_rows:
            connection.execute(ddl)
        restored_triggers = [
            tuple(row)
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (table,),
            )
        ]
        assert restored_triggers == trigger_rows
        assert _v2_rule_schema_ddl(connection) == schema_before
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    connection.commit()
    assert read_target() != original_value
    return original_value, read_target()


@pytest.mark.parametrize("corruption", ["block_text", "normalized_hash", "attachment_hash"])
def test_v2_rule_service_ocr_card_edit_rechecks_source_hash_before_result(tmp_path, corruption):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject
    from test_independent_ai_invocation_v1 import (
        DurableInputVerifier,
        persist_interpretation_reply,
    )

    c, _, _, _, _, parent_id, _intake, _, _, source_verifier, _decision = _prepare_receipt_subject(
        tmp_path,
        suffix="rule-ocr-integrity-" + corruption,
        prepare_review=False,
    )
    try:
        provider = DurableProvider(c, response="{}")
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source_verifier,
            interpretation_input_verifier=DurableInputVerifier(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        owner = _v2_rule_amendment_service(c, ai, source_verifier=source_verifier)
        review = owner.prepare(parent_id)
        reply = "Change category to travel"
        proof = persist_interpretation_reply(
            c, review, reply, identity="rule-ocr-integrity-reply-" + corruption
        )
        provider.response = wire(reply, {"category": "travel"}, ref="reply")
        prepared = ai.prepare_edit(review.review_id, proof, "rule-ocr-integrity-op-" + corruption)
        projection = json.loads(
            c.execute(
                "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?",
                (prepared.attempt_id,),
            ).fetchone()[0]
        )["projection"]
        extraction_id = projection["selection"]["extraction_id"]
        before_value, after_value = _v2_rule_controlled_ocr_source_mutation(
            c, corruption, extraction_id
        )
        assert before_value != after_value

        from finance_core.receipt_staging_runner.local_intake import LocalLineageError

        expected_error, expected_message = {
            "block_text": (
                AiInvocationError,
                "Original receipt total source integrity changed",
            ),
            "normalized_hash": (
                AiInvocationError,
                "Original source base revision payload or hash changed",
            ),
            "attachment_hash": (
                LocalLineageError,
                "Extraction source_attachment_hash does not match attachment file_hash",
            ),
        }[corruption]
        with pytest.raises(expected_error) as refused:
            ai.invoke(prepared.attempt_id)
        assert str(refused.value) == expected_message
        if corruption == "block_text":
            assert isinstance(refused.value.__cause__, ValueError)
            assert str(refused.value.__cause__) == "OCR layout normalized hash does not match"
        assert provider.calls == 0
        assert provider.requests == []
        assert (
            c.execute(
                "SELECT count(*) FROM independent_ai_claims WHERE attempt_id=?",
                (prepared.attempt_id,),
            ).fetchone()[0]
            == 0
        )
        assert c.execute("SELECT count(*) FROM synthetic_ai_receipts").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM independent_ai_results").fetchone()[0] == 0
        _v2_rule_assert_no_publication_or_financial_facts(c)
        assert c.in_transaction is False
    finally:
        c.close()


def test_v2_rule_contract_month_literal_finite_positive_and_negative_boundaries():
    import calendar

    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    context = canonical_text(_V2_RULE_CONTEXT)
    positive_count = 0
    for month in range(1, 13):
        name = calendar.month_name[month]
        for tail in ("seasonal menu", "weekday special"):
            description = f"{name} {tail}"
            text = f"Lunch SGD 12.50 at Cafe 2026-10-08 {description}"
            result = _v2_rule_validate(
                "initial",
                text,
                {
                    **ORIGINAL,
                    "merchant": "Cafe",
                    "description": description,
                },
                evidence={
                    "amount": [cite(text, "12.50")],
                    "currency": [cite(text, "SGD")],
                    "merchant": [cite(text, "Cafe")],
                    "description": [cite(text, description)],
                    "category": [cite(text)],
                },
            )
            assert result["state"] == "VALID", (description, result)
            positive_count += 1
    assert positive_count == 24

    # Exact historic failure fixture remains a supported normal descriptor.
    original = "Lunch SGD 12.50 at Cafe 2026-10-08 October special"
    original_result = _v2_rule_validate(
        "initial",
        original,
        {**ORIGINAL, "merchant": "Cafe", "description": "October special"},
        evidence={
            "amount": [cite(original, "12.50")],
            "currency": [cite(original, "SGD")],
            "merchant": [cite(original, "Cafe")],
            "description": [cite(original, "October special")],
            "category": [cite(original)],
        },
    )
    assert original_result["state"] == "VALID", original_result

    explicit_ordinal = _v2_rule_validate(
        "card_edit",
        "Change description to October ninth",
        {"description": "October ninth"},
        unresolved_fields=(),
    )
    assert explicit_ordinal["state"] == "VALID", explicit_ordinal

    quoted_merchant = 'Lunch SGD 12.50 at "March Fourth Cafe"'
    quoted = _v2_rule_validate(
        "initial",
        quoted_merchant,
        {**ORIGINAL, "merchant": "March Fourth Cafe", "transaction_date": "2026-10-09"},
        date_expression={"kind": "absolute_date", "year": 2026, "month": 10, "day": 9},
        evidence={
            "amount": [cite(quoted_merchant, "12.50")],
            "currency": [cite(quoted_merchant, "SGD")],
            "merchant": [cite(quoted_merchant, '"March Fourth Cafe"')],
            "description": [cite(quoted_merchant, "Lunch")],
            "transaction_date": [cite(context, "2026-10-09", "source_context")],
            "category": [cite(quoted_merchant)],
        },
    )
    assert quoted["state"] == "VALID", quoted

    missing_year_count = 0
    for month in range(1, 13):
        name = calendar.month_name[month]
        description = f"{name} seasonal menu"
        text = f"Lunch SGD 12.50 at Cafe {description}"
        result = _v2_rule_validate(
            "initial",
            text,
            {
                **ORIGINAL,
                "transaction_date": "2026-10-09",
                "merchant": "Cafe",
                "description": description,
            },
            evidence={
                "amount": [cite(text, "12.50")],
                "currency": [cite(text, "SGD")],
                "merchant": [cite(text, "Cafe")],
                "description": [cite(text, description)],
                "transaction_date": [cite(context, "2026-10-09", "source_context")],
                "category": [cite(text)],
            },
        )
        assert result["state"] == "NEEDS_CLARIFICATION", (description, result)
        assert "transaction_date" in {
            field for question in result.get("unresolved", []) for field in question["fields"]
        }, (description, result)
        missing_year_count += 1
    assert missing_year_count == 12

    requested_label = "Lunch SGD 12.50 at Cafe date October seasonal menu"
    requested = _v2_rule_validate(
        "initial",
        requested_label,
        {
            **ORIGINAL,
            "transaction_date": "2026-10-09",
            "merchant": "Cafe",
            "description": "October seasonal menu",
        },
        evidence={
            "amount": [cite(requested_label, "12.50")],
            "currency": [cite(requested_label, "SGD")],
            "merchant": [cite(requested_label, "Cafe")],
            "description": [cite(requested_label, "October seasonal menu")],
            "transaction_date": [cite(context, "2026-10-09", "source_context")],
            "category": [cite(requested_label)],
        },
    )
    assert requested["state"] == "NEEDS_CLARIFICATION", requested
    assert "transaction_date" in {
        field for question in requested.get("unresolved", []) for field in question["fields"]
    }, requested

    # Keep May Cafe merchant support with both source-date and ordinary-default paths.
    merchant_with_date = "Lunch SGD 12.50 at May Cafe 2026-10-08"
    with_date = _v2_rule_validate(
        "initial",
        merchant_with_date,
        {**ORIGINAL, "merchant": "May Cafe"},
        evidence={
            "amount": [cite(merchant_with_date, "12.50")],
            "currency": [cite(merchant_with_date, "SGD")],
            "merchant": [cite(merchant_with_date, "May Cafe")],
            "description": [cite(merchant_with_date, "Lunch")],
            "category": [cite(merchant_with_date)],
        },
    )
    assert with_date["state"] == "VALID", with_date
    merchant_without_date = "Lunch SGD 12.50 at May Cafe"
    defaulted = _v2_rule_validate(
        "initial",
        merchant_without_date,
        {**ORIGINAL, "merchant": "May Cafe", "transaction_date": "2026-10-09"},
        date_expression={"kind": "absolute_date", "year": 2026, "month": 10, "day": 9},
        evidence={
            "amount": [cite(merchant_without_date, "12.50")],
            "currency": [cite(merchant_without_date, "SGD")],
            "merchant": [cite(merchant_without_date, "May Cafe")],
            "description": [cite(merchant_without_date, "Lunch")],
            "transaction_date": [cite(context, "2026-10-09", "source_context")],
            "category": [cite(merchant_without_date)],
        },
    )
    assert defaulted["state"] == "VALID", defaulted

    partial_date_cases = (
        ("Cafe May 8", "May"),
        ("Cafe May8", "May8"),
        ("Cafe May ninth", "May"),
        ("Cafe October ninth", "October"),
        ("Cafe March fourth", "March"),
        ("Cafe ninth of October", "October"),
        ("Cafe October 8th", "October"),
    )
    partial_date_count = 0
    for suffix, truncated_value in partial_date_cases:
        text = f"Lunch SGD 12.50 at {suffix}"
        result = _v2_rule_validate(
            "initial",
            text,
            {**ORIGINAL, "transaction_date": "2026-10-09", "merchant": truncated_value},
            evidence={
                "amount": [cite(text, "12.50")],
                "currency": [cite(text, "SGD")],
                "merchant": [cite(text, truncated_value)],
                "description": [cite(text, "Lunch")],
                "transaction_date": [cite(context, "2026-10-09", "source_context")],
                "category": [cite(text)],
            },
        )
        assert result["state"] == "NEEDS_CLARIFICATION", (suffix, result)
        assert "transaction_date" in {
            field for question in result.get("unresolved", []) for field in question["fields"]
        }, (suffix, result)
        partial_date_count += 1
    assert partial_date_count == 7

    cardinal = (
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three "
        "twenty-four twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty "
        "thirty-one"
    ).split()
    ordinal = (
        "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
        "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth "
        "twenty-first twenty-second twenty-third twenty-fourth twenty-fifth twenty-sixth "
        "twenty-seventh twenty-eighth twenty-ninth thirtieth thirty-first"
    ).split()
    assert len(cardinal) == len(ordinal) == 31
    date_word_cues = set()
    for words in (cardinal, ordinal):
        for word in words:
            date_word_cues.add(f"October {word}")
            if "-" in word:
                date_word_cues.add(f"October {word.replace('-', ' ')}")
    date_word_case_count = 0
    for cue in sorted(date_word_cues):
        text = f"Lunch SGD 12.50 at Cafe {cue}"
        result = _v2_rule_validate(
            "initial",
            text,
            {
                **ORIGINAL,
                "transaction_date": "2026-10-09",
                "merchant": "Cafe",
                "description": cue,
            },
            evidence={
                "amount": [cite(text, "12.50")],
                "currency": [cite(text, "SGD")],
                "merchant": [cite(text, "Cafe")],
                "description": [cite(text, cue)],
                "transaction_date": [cite(context, "2026-10-09", "source_context")],
                "category": [cite(text)],
            },
        )
        assert result["state"] == "NEEDS_CLARIFICATION", (cue, result)
        assert "transaction_date" in {
            field for question in result.get("unresolved", []) for field in question["fields"]
        }, (cue, result)
        date_word_case_count += 1
    assert len(date_word_cues) == 82
    assert date_word_case_count == 82

    # The month-only weak literal cannot erase a conflict whichever arm is first.
    date_alternative_count = 0
    for text in (
        "Lunch SGD 12.50 at Cafe October ninth or 2026-10-08",
        "Lunch SGD 12.50 at Cafe 2026-10-08 or October ninth",
    ):
        result = _v2_rule_validate(
            "initial",
            text,
            {**ORIGINAL, "merchant": "Cafe", "description": "October ninth"},
            date_expression={"kind": "absolute_date", "year": 2026, "month": 10, "day": 8},
            date_quote="2026-10-08",
            evidence={
                "amount": [cite(text, "12.50")],
                "currency": [cite(text, "SGD")],
                "merchant": [cite(text, "Cafe")],
                "description": [cite(text, "October ninth")],
                "category": [cite(text)],
            },
        )
        assert result["state"] == "NEEDS_CLARIFICATION", result
        assert "transaction_date" in {
            field for question in result.get("unresolved", []) for field in question["fields"]
        }, result
        assert "ambiguous_date_alternative" in result.get("hard_blockers", []), result
        date_alternative_count += 1
    assert date_alternative_count == 2


def published_text(c, suffix="v2-edit"):
    _, _, _, intake, *_ = subject(c, suffix)
    ai, provider = service_v2(c, wire("Lunch SGD 12.50 at Example Cafe 2026-10-08"))
    first = ai.prepare_initial(intake["public_id"], "op-" + suffix)
    assert ai.invoke(first.attempt_id).state == "VALID"
    return ai, provider, ai.publish(first.attempt_id)


@pytest.mark.parametrize(
    "field,value,text",
    [
        ("amount", "15.00", "Change amount to SGD 15.00"),
        ("currency", "USD", "Change currency to USD"),
        ("transaction_date", "2026-10-09", "Change date to 2026-10-09"),
        ("merchant", "New Cafe", "Change merchant to New Cafe"),
        ("description", "Dinner", "Change description to Dinner"),
        ("category", "travel", "Change category to travel"),
    ],
)
def test_v2_all_six_natural_fields_actual_post(migrated_temp_db_connection, field, value, text):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "edit-" + field)
    owner = posting(c, ai)
    old = owner.prepare(initial.proposal_public_id)
    proof = persist_reply(c, old, text)
    provider.response = wire(text, {field: value}, ref="reply")
    edit = ai.prepare_edit(old.review_id, proof, "natural-" + field)
    assert ai.invoke(edit.attempt_id).state == "VALID"
    new = ai.publish(edit.attempt_id)
    fresh = owner.prepare(new.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, new.proposal_public_id, "confirm-" + field)
    final = owner.submit_post(fresh.review_id, "confirm-" + field)
    assert final.state == "finalized"
    row = c.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category FROM transactions"
    ).fetchone()
    expected = {**ORIGINAL, field: value}
    assert tuple(row) == (
        float(expected["amount"]),
        expected["currency"],
        expected["transaction_date"],
        expected["merchant"],
        expected["description"],
        expected["category"],
    )
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert provider.calls == 2


def test_v2_previous_replacement_role_and_same_reply_confirm_is_no_authority(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "role-edit")
    owner = posting(c, ai)
    old = owner.prepare(initial.proposal_public_id)
    text = "Not 12.50, change amount to 15.00 and confirm"
    proof = persist_reply(c, old, text)
    evidence = {
        "amount": [
            cite(text, "12.50", "reply", "previous_value"),
            cite(text, "15.00", "reply", "replacement_value"),
        ]
    }
    provider.response = wire(text, {"amount": "15.00"}, ref="reply", evidence=evidence)
    attempt = ai.prepare_edit(old.review_id, proof, "role-op")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    child = ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    fresh = owner.prepare(child.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, child.proposal_public_id, "role-fresh-confirm")
    final = owner.submit_post(fresh.review_id, "role-fresh-confirm")
    assert final.state == "finalized"
    assert c.execute("SELECT amount FROM transactions").fetchone()[0] == 15.0, (
        "STOP_FINANCIAL_FACT_MISMATCH"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "gap",
        "unknown_key",
        "wrong_offset",
        "boolean_amount",
        "unknown_date_kind",
        "omitted_date",
        "wrong_previous",
        "refund",
    ],
)
def test_v2_malformed_or_unresolved_never_publishes(migrated_temp_db_connection, fault):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "fault-" + fault)
    old = posting(c, ai).prepare(initial.proposal_public_id)
    text = "Change amount to SGD 15.00"
    if fault == "omitted_date":
        text += " and date to yesterday"
    if fault == "refund":
        text += " refund"
    body = json.loads(wire(text, {"amount": "15.00"}, ref="reply"))
    if fault == "gap":
        body["coverage"][0]["end"] -= 4
    if fault == "unknown_key":
        body["approve"] = True
    if fault == "wrong_offset":
        body["operations"][0]["evidence"][0]["start"] += 1
    if fault == "boolean_amount":
        body["operations"][0]["value"] = True
    if fault == "unknown_date_kind":
        body = json.loads(
            wire(
                text,
                {"transaction_date": "2026-10-09"},
                ref="reply",
                date_expression={"kind": "processing_now"},
                evidence={"transaction_date": [cite(text, ref="reply")]},
            )
        )
    if fault == "wrong_previous":
        body["operations"][0]["evidence"][0]["role"] = "previous_value"
    provider.response = json.dumps(body)
    reply = persist_reply(c, old, text)
    attempt = ai.prepare_edit(old.review_id, reply, "fault-op")
    assert ai.invoke(attempt.attempt_id).state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 1


def test_v2_unknown_cannot_prepare_clarification_or_retry(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "v2-unknown")
    ai, provider = service_v2(c, wire("Lunch SGD 12.50 at Example Cafe 2026-10-08"))
    first = ai.prepare_initial(intake["public_id"], "unknown-op")
    ai.claim(first.attempt_id)
    assert ai.recover(first.attempt_id).state == "UNKNOWN"
    with pytest.raises(AiInvocationError):
        ai.prepare_clarification(first.attempt_id)
    with pytest.raises(AiInvocationError):
        ai.prepare_initial(intake["public_id"], "retry-op")
    assert provider.calls == 0


def test_v1_independent_is_historical_only(migrated_temp_db_connection):
    from test_independent_ai_invocation_v1 import response

    from finance_core.application.ai_invocation import _AiInvocationServiceV1

    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "v1-history")
    provider = DurableProvider(c, response=response())
    old = _AiInvocationServiceV1(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=InputV2(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    original = old.prepare_initial(intake["public_id"], "historical-v1-op")
    current = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=InputV2(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    assert current.get_status(original.attempt_id).state == "PREPARED"
    for action in (current.claim, current.publish, current.cancel):
        with pytest.raises(AiInvocationError, match="historical-read-only"):
            action(original.attempt_id)
    assert provider.calls == 0


def test_v2_forged_typed_receipt_port_rejected_before_facts(tmp_path, monkeypatch):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    from finance_core.application import posting as posting_module
    from finance_core.application.posting import PostingService, _AcceptedReceiptMetadataAuthority
    from finance_core.application.receipt_posting_authorization import (
        VerifiedReceiptPostingAuthorization,
    )
    from finance_core.parser_proposals.receipt_facts_conversion import (
        ReceiptFactsConversionCommand,
        convert_confirmed_receipt_proposal_to_facts,
    )

    c, _, _, _, _, _, intake, _, _, source, decision = _prepare_receipt_subject(
        tmp_path, suffix="v2-forged", ocr_confidence_scaled=3000, prepare_review=False
    )
    try:
        text = "EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50"
        fields = {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
        provider = DurableProvider(
            c,
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
            connection=c,
            binding=BINDING,
            source_verifier=source,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        first = ai.prepare_initial(intake, "forged-prep")
        assert ai.invoke(first.attempt_id).state == "VALID"
        published = ai.publish(first.attempt_id)
        owner = PostingService(
            connection=c,
            binding=BINDING,
            source_verifier=source,
            human_decision_authority=decision,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        review = owner.prepare(published.proposal_public_id)
        _persist_fresh_posting_confirmation(
            c, review, published.proposal_public_id, "forged-confirm"
        )

        def stop(stage):
            if stage == "after_acceptance_commit":
                raise RuntimeError("synthetic crash after durable acceptance")

        monkeypatch.setattr(posting_module, "_failure_injection_hook", stop)
        with pytest.raises(RuntimeError):
            owner.submit_post(review.review_id, "forged-confirm")
        attempt = c.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
        payer = review.projection["payer_participant_public_id"]
        command = ReceiptFactsConversionCommand(
            command_public_id="rpfc_application_" + posting_module._sha([attempt])[:24],
            proposal_public_id=published.proposal_public_id,
            expected_content_hash=published.effective_content_hash,
            payer_participant_public_id=payer,
            participants=({"participant_public_id": payer, "is_included": 1},),
            authenticated_actor_id=BINDING.human_principal_id,
            channel="independent_application",
        )

        class Forged:
            def verify_in_transaction(self, connection, expected):
                return VerifiedReceiptPostingAuthorization(
                    expected,
                    "full_card_ai_expense_v1",
                    attempt,
                    review.review_id,
                    review.review_hash,
                    "forged-confirm",
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                )

        for authority in (None, Forged()):
            with pytest.raises(AiInvocationError):
                convert_confirmed_receipt_proposal_to_facts(
                    c,
                    command,
                    metadata_authority=_AcceptedReceiptMetadataAuthority(owner, attempt),
                    ai_lineage_authority=ai,
                    receipt_posting_authorization=authority,
                )
        assert c.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0, (
            "STOP_FINANCIAL_FACT_MISMATCH"
        )
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
    finally:
        c.close()


def finish_post(c, ai, published, identity, *, source=None, decision=None, clock=lambda: NOW):
    from finance_core.application.posting import PostingService

    owner = (
        posting(c, ai)
        if source is None
        else PostingService(
            connection=c,
            binding=BINDING,
            source_verifier=source,
            human_decision_authority=decision,
            clock=clock,
            ai_lineage_authority=ai,
        )
    )
    if source is None:
        owner._clock = clock
    card = owner.prepare(published.proposal_public_id)
    _persist_fresh_posting_confirmation(c, card, published.proposal_public_id, identity)
    final = owner.submit_post(card.review_id, identity)
    assert final.state == "finalized"
    assert owner.resume_post(final.attempt_id) == final
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert (
        c.execute("SELECT public_id FROM transactions").fetchone()[0] == final.transaction_public_id
    ), "STOP_FINANCIAL_FACT_MISMATCH"
    return card, final


@pytest.mark.parametrize("ambiguity", ["two_amounts", "two_dates", "unknown_currency"])
def test_v2_specific_question_authenticated_answer_actual_once_post(
    migrated_temp_db_connection, ambiguity
):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    c = migrated_temp_db_connection
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    if ambiguity == "two_amounts":
        text = "Lunch SGD 12.50 or SGD 15.00 at Example Cafe 2026-10-08"
    if ambiguity == "two_dates":
        text += " or 2026-10-09"
    if ambiguity == "unknown_currency":
        text = "Lunch $12.50 at Example Cafe 2026-10-08"
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="questions-" + ambiguity, raw_text=text)
    ai, provider = service_v2(c, wire(text))
    first = ai.prepare_initial(intake["public_id"], "question-" + ambiguity)
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    question = ai.prepare_clarification(first.attempt_id)
    assert question.projection["unresolved"] and question.projection["confirm_available"] is False
    fields = {field for q in question.projection["unresolved"] for field in q["fields"]}
    assert (
        "transaction_date"
        if ambiguity == "two_dates"
        else "amount"
        if ambiguity == "two_amounts"
        else "currency"
    ) in fields
    if ambiguity == "two_dates":
        reply = "Use date 2026-10-09"
        patch = {"transaction_date": "2026-10-09"}
    else:
        reply = "Use SGD 15.00"
        patch = (
            {"amount": "15.00", "currency": "SGD"}
            if ambiguity == "unknown_currency"
            else {"amount": "15.00"}
        )
    proof = persist_reply(c, question, reply, identity="answer-" + ambiguity)
    provider.response = wire(reply, patch, ref="reply")
    successor = ai.prepare_followup(question.request_id, proof, "answer-op-" + ambiguity)
    assert ai.invoke(successor.attempt_id).state == "VALID"
    published = ai.publish(successor.attempt_id)
    finish_post(c, ai, published, "question-post-" + ambiguity)
    row = c.execute("SELECT amount,currency,transaction_date,merchant FROM transactions").fetchone()
    assert tuple(row) == (
        12.5 if ambiguity == "two_dates" else 15.0,
        "SGD",
        "2026-10-09" if ambiguity == "two_dates" else "2026-10-08",
        "Example Cafe",
    )
    assert (
        c.execute(
            "SELECT raw_input FROM raw_intake_records WHERE public_id=?", (intake["public_id"],)
        ).fetchone()[0]
        == text
    )
    assert provider.calls == 2


def test_v2_original_ocr_missing_currency_date_specific_reply_once_post(tmp_path):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    c, _, _, _, _, _, intake, _, _, source, decision = _prepare_receipt_subject(
        tmp_path,
        suffix="v2-missing-ocr",
        prepare_review=False,
        total_currency=None,
        date_text=None,
        ocr_confidence_scaled=3000,
    )
    try:
        text = "EXAMPLE CAFE\nTOTAL\n12.50"
        fields = {
            "amount": "12.50",
            "merchant": "EXAMPLE CAFE",
            "description": "Receipt total",
            "category": "food",
        }
        provider = DurableProvider(
            c, response=wire(text, fields, evidence={"amount": [cite(text, "12.50")]})
        )
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        first = ai.prepare_initial(intake, "missing-ocr-op")
        assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
        question = ai.prepare_clarification(first.attempt_id)
        required = {field for q in question.projection["unresolved"] for field in q["fields"]}
        assert {"currency", "transaction_date"} <= required
        assert question.projection["draft_values"]["amount"] == "12.50"
        reply = "Use SGD and date 2026-10-09"
        proof = persist_reply(c, question, reply, identity="missing-ocr-answer")
        provider.response = wire(
            reply, {"currency": "SGD", "transaction_date": "2026-10-09"}, ref="reply"
        )
        successor = ai.prepare_followup(question.request_id, proof, "missing-ocr-answer-op")
        assert ai.invoke(successor.attempt_id).state == "VALID"
        published = ai.publish(successor.attempt_id)
        card, _ = finish_post(
            c, ai, published, "missing-ocr-post", source=source, decision=decision
        )
        assert card.projection["expense_assessment"]["hard_blockers"] == []
        assert tuple(
            c.execute(
                "SELECT amount,currency,transaction_date,merchant FROM transactions"
            ).fetchone()
        ) == (12.5, "SGD", "2026-10-09", "EXAMPLE CAFE"), "STOP_FINANCIAL_FACT_MISMATCH"
        assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
    finally:
        c.close()


def test_v2_multiple_edits_card_binds_full_before_after_difference(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    ai, provider, first = published_text(c, "multi-card")
    owner = posting(c, ai)
    old = owner.prepare(first.proposal_public_id)
    reply = "Change amount to SGD 15.00, merchant to New Cafe, category to travel"
    patch = {"amount": "15.00", "merchant": "New Cafe", "category": "travel"}
    proof = persist_reply(c, old, reply)
    provider.response = wire(reply, patch, ref="reply")
    attempt = ai.prepare_edit(old.review_id, proof, "multi-card-edit")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    published = ai.publish(attempt.attempt_id)
    card, _ = finish_post(c, ai, published, "multi-card-confirm")
    summary = card.projection["expense_assessment"]["change_summary"]
    assert summary["before"] == ORIGINAL
    assert summary["after"] == {**ORIGINAL, **patch}
    assert {x["field"]: (x["before"], x["after"]) for x in summary["changes"]} == {
        "amount": ("12.50", "15.00"),
        "merchant": ("Example Cafe", "New Cafe"),
        "category": ("food", "travel"),
    }
    assert card.review_hash != old.review_hash


@pytest.mark.parametrize(
    "fault", ["negative", "conflict_amount", "unknown_currency", "unprocessed_amount"]
)
def test_v2_whole_sentence_date_quote_cannot_hide_financial_conflicts(
    migrated_temp_db_connection, fault
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "date-mask-" + fault)
    card = posting(c, ai).prepare(initial.proposal_public_id)
    fragments = {
        "negative": "Change amount to SGD -15.00",
        "conflict_amount": "Use SGD 15.00 or SGD 20.00",
        "unknown_currency": "Use XYZ 15.00",
        "unprocessed_amount": "Use SGD 15.00",
    }
    reply = fragments[fault] + " and date to 2026-10-09"
    fields = {"transaction_date": "2026-10-09"}
    if fault != "unprocessed_amount":
        fields["amount"] = "15.00"
    provider.response = wire(
        reply, fields, ref="reply", evidence={field: [cite(reply, ref="reply")] for field in fields}
    )
    proof = persist_reply(c, card, reply, identity="date-mask-" + fault)
    attempt = ai.prepare_edit(card.review_id, proof, "date-mask-op-" + fault)
    assert ai.invoke(attempt.attempt_id).state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )


def test_v2_whole_sentence_date_quote_retains_valid_multiple_changes(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "date-mask-valid")
    card = posting(c, ai).prepare(initial.proposal_public_id)
    reply = "Change amount to SGD 15.00 and date to 2026-10-09"
    fields = {"amount": "15.00", "transaction_date": "2026-10-09"}
    provider.response = wire(
        reply, fields, ref="reply", evidence={field: [cite(reply, ref="reply")] for field in fields}
    )
    proof = persist_reply(c, card, reply)
    attempt = ai.prepare_edit(card.review_id, proof, "date-mask-valid-op")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    publication = ai.publish(attempt.attempt_id)
    finish_post(c, ai, publication, "date-mask-valid-confirm")
    assert tuple(c.execute("SELECT amount,transaction_date FROM transactions").fetchone()) == (
        15.0,
        "2026-10-09",
    ), "STOP_FINANCIAL_FACT_MISMATCH"


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Kuala_Lumpur"])
def test_v2_dates_bind_source_then_reply_across_processing_midnight_and_replay(
    migrated_temp_db_connection,
    timezone,
):
    from datetime import datetime
    from datetime import timezone as datetime_timezone

    from test_application_posting_recovery_v1 import (
        SOURCE_KEY,
        _digest,
        _prepare_text_subject,
        _replace,
    )

    def timestamp(value):
        return int(datetime.fromisoformat(value).replace(tzinfo=datetime_timezone.utc).timestamp())

    hour = "23" if timezone == "UTC" else "15"
    next_hour = "00" if timezone == "UTC" else "16"
    c = migrated_temp_db_connection
    text = "Lunch SGD 12.50 at Example Cafe yesterday"
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="date-bound", raw_text=text)
    source = _load(c, "synthetic_sources", "synthetic-source-date-bound", SOURCE_KEY)
    source["source_occurred_at"] = timestamp(f"2026-10-08T{hour}:59:58")
    source["received_at"] = timestamp(f"2026-10-08T{hour}:59:59")
    source["evidence_digest"] = _digest(
        {key: value for key, value in source.items() if key != "evidence_digest"}
    )
    _replace(c, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
    c.commit()
    clock = [timestamp(f"2026-10-{'09' if timezone == 'UTC' else '08'}T{next_hour}:00:01")]
    fields = {**ORIGINAL, "transaction_date": "2026-10-07"}
    body = wire(
        text,
        fields,
        date_expression={"kind": "day_offset", "days": -1},
        evidence={"transaction_date": [cite(text, "yesterday")]},
    )
    ai, provider = service_v2(c, body, clock=lambda: clock[0], timezone=timezone)
    provider.at = clock[0]
    first = ai.prepare_initial(intake["public_id"], "date-bound-source")
    assert ai.invoke(first.attempt_id).state == "VALID"
    publication = ai.publish(first.attempt_id)
    assert (
        provider.requests[0]["input"]["interpretation_context"]["anchor_timestamp"]
        == source["source_occurred_at"]
    )
    clock[0] = timestamp(f"2026-10-09T{hour}:59:57")
    owner = posting(c, ai)
    owner._clock = lambda: clock[0]
    old = owner.prepare(publication.proposal_public_id)
    reply = "Change date to yesterday"
    observed = timestamp(f"2026-10-09T{hour}:59:58")
    proof = persist_reply(c, old, reply, identity="date-bound-reply", at=observed)
    clock[0] = timestamp(f"2026-10-{'10' if timezone == 'UTC' else '09'}T{next_hour}:00:01")
    provider.at = clock[0]
    provider.response = wire(
        reply,
        {"transaction_date": "2026-10-08"},
        ref="reply",
        date_expression={"kind": "day_offset", "days": -1},
        evidence={"transaction_date": [cite(reply, "yesterday", "reply")]},
    )
    edit = ai.prepare_edit(old.review_id, proof, "date-bound-edit")
    assert ai.invoke(edit.attempt_id).state == "VALID"
    new = ai.publish(edit.attempt_id)
    assert provider.requests[-1]["input"]["interpretation_context"]["anchor_timestamp"] == observed
    card, final = finish_post(c, ai, new, "date-bound-confirm", clock=lambda: clock[0])
    assert (
        card.projection["expense_assessment"]["change_summary"]["before"]["transaction_date"]
        == "2026-10-07"
    )
    clock[0] += 86400
    assert ai.recover(new.attempt_id) == new
    assert owner.resume_post(final.attempt_id) == final
    assert c.execute("SELECT transaction_date FROM transactions").fetchone()[0] == "2026-10-08", (
        "STOP_FINANCIAL_FACT_MISMATCH"
    )
    assert provider.calls == 2


def test_v2_expired_question_refresh_rejects_old_generation_and_finishes_once(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "refresh-question")
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    question = {
        "question_id": "amount",
        "fields": ["amount"],
        "reason": "ambiguous",
        "candidates": ["12.50", "15.00"],
        "evidence": [cite(text)],
    }
    clock = [NOW]
    ai, provider = service_v2(
        c,
        wire(text, ORIGINAL, unresolved=[question], outcome="clarification"),
        clock=lambda: clock[0],
    )
    first = ai.prepare_initial(intake["public_id"], "refresh-initial")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    old = ai.prepare_clarification(first.attempt_id)
    old_reply = persist_reply(c, old, "Use SGD 15.00", identity="old-generation", at=NOW)
    clock[0] = old.expires_at
    assert ai.get_clarification(old.request_id).state == "EXPIRED"
    new = ai.prepare_clarification(first.attempt_id)
    assert (
        new.projection["generation"] == 1
        and new.projection["previous_request_id"] == old.request_id
    )
    assert new.request_hash != old.request_hash and provider.calls == 1
    assert ai.get_clarification(old.request_id).state == "STALE"
    with pytest.raises(AiInvocationError):
        ai.prepare_followup(old.request_id, old_reply, "old-generation-op")
    proof = persist_reply(c, new, "Use SGD 15.00", identity="new-generation", at=clock[0])
    provider.response = wire("Use SGD 15.00", {"amount": "15.00"}, ref="reply")
    provider.at = clock[0]
    follow = ai.prepare_followup(new.request_id, proof, "new-generation-op")
    assert ai.invoke(follow.attempt_id).state == "VALID"
    publication = ai.publish(follow.attempt_id)
    finish_post(c, ai, publication, "refresh-confirm", clock=lambda: clock[0])
    assert provider.calls == 2


def test_v2_two_question_rounds_authenticate_complete_predecessor_ancestry(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "two-rounds")
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    questions = [
        {
            "question_id": "q_" + field,
            "fields": [field],
            "reason": "ambiguous",
            "candidates": [],
            "evidence": [cite(text)],
        }
        for field in ("amount", "transaction_date")
    ]
    ai, provider = service_v2(
        c, wire(text, ORIGINAL, unresolved=questions, outcome="clarification")
    )
    first = ai.prepare_initial(intake["public_id"], "two-rounds-initial")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    first_question = ai.prepare_clarification(first.attempt_id)
    reply = "Use SGD 15.00, but date is unclear"
    proof = persist_reply(c, first_question, reply, identity="two-rounds-one")
    remaining = [
        {
            "question_id": "q_date",
            "fields": ["transaction_date"],
            "reason": "ambiguous",
            "candidates": [],
            "evidence": [cite(reply, ref="reply")],
        }
    ]
    provider.response = wire(
        reply, {"amount": "15.00"}, ref="reply", unresolved=remaining, outcome="clarification"
    )
    second = ai.prepare_followup(first_question.request_id, proof, "two-rounds-second")
    assert ai.invoke(second.attempt_id).state == "NEEDS_CLARIFICATION"
    second_question = ai.prepare_clarification(second.attempt_id)
    assert second_question.projection["draft_values"]["amount"] == "15.00"
    reply = "Use date 2026-10-09"
    proof = persist_reply(c, second_question, reply, identity="two-rounds-two")
    provider.response = wire(reply, {"transaction_date": "2026-10-09"}, ref="reply")
    final_attempt = ai.prepare_followup(second_question.request_id, proof, "two-rounds-third")
    assert ai.invoke(final_attempt.attempt_id).state == "VALID"
    publication = ai.publish(final_attempt.attempt_id)
    finish_post(c, ai, publication, "two-rounds-confirm")
    assert tuple(c.execute("SELECT amount,transaction_date FROM transactions").fetchone()) == (
        15.0,
        "2026-10-09",
    ), "STOP_FINANCIAL_FACT_MISMATCH"
    assert (
        c.execute("SELECT count(*) FROM independent_ai_followup_links").fetchone()[0] == 2
        and provider.calls == 3
    )


@pytest.mark.parametrize(
    "fault",
    [
        "unsent",
        "wrong_human",
        "revoked",
        "wrong_reply_target",
        "wrong_proposal",
        "altered_projection",
    ],
)
def test_v2_clarification_input_authority_failure_is_write_free(migrated_temp_db_connection, fault):
    from test_application_posting_recovery_v1 import _replace

    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "question-proof-" + fault)
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    question = {
        "question_id": "q_amount",
        "fields": ["amount"],
        "reason": "ambiguous",
        "candidates": [],
        "evidence": [cite(text)],
    }
    ai, provider = service_v2(
        c, wire(text, ORIGINAL, unresolved=[question], outcome="clarification")
    )
    first = ai.prepare_initial(intake["public_id"], "question-proof-op-" + fault)
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    card = ai.prepare_clarification(first.attempt_id)
    proof_id = persist_reply(c, card, "Use SGD 15.00", identity="proof-" + fault)
    proof = _load(c, "synthetic_interpretation_replies", proof_id, KEY)
    if fault in {"unsent", "altered_projection"}:
        display = _load(c, "synthetic_interpretation_displays", proof["display_id"], KEY)
        if fault == "unsent":
            display["status"] = "PREPARED"
        else:
            display["projection"]["draft_values"]["amount"] = "999.00"
        _replace(c, "synthetic_interpretation_displays", proof["display_id"], display, KEY)
    else:
        key, value = {
            "wrong_human": ("human_principal_id", "other-human"),
            "revoked": ("revoked", True),
            "wrong_reply_target": ("reply_to_display_id", "other-display"),
            "wrong_proposal": ("proposal_public_id", "other-proposal"),
        }[fault]
        proof[key] = value
        _replace(c, "synthetic_interpretation_replies", proof_id, proof, KEY)
    c.commit()
    before = c.total_changes
    with pytest.raises(AiInvocationError):
        ai.prepare_followup(card.request_id, proof_id, "bad-proof-followup")
    assert c.total_changes == before and provider.calls == 1
    assert c.execute("SELECT count(*) FROM independent_ai_followup_links").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )


def test_v2_clarification_successor_and_consumption_roll_back_together(migrated_temp_db_connection):
    import sqlite3

    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "question-atomic")
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    question = {
        "question_id": "q_amount",
        "fields": ["amount"],
        "reason": "ambiguous",
        "candidates": [],
        "evidence": [cite(text)],
    }
    ai, provider = service_v2(
        c, wire(text, ORIGINAL, unresolved=[question], outcome="clarification")
    )
    first = ai.prepare_initial(intake["public_id"], "question-atomic-initial")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    card = ai.prepare_clarification(first.attempt_id)
    proof = persist_reply(c, card, "Use SGD 15.00", identity="question-atomic-proof")
    c.execute(
        "CREATE TRIGGER synthetic_followup_failure BEFORE INSERT ON "
        "independent_ai_followup_links BEGIN SELECT RAISE(ABORT,'synthetic successor "
        "failure'); END"
    )
    c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic successor failure"):
        ai.prepare_followup(card.request_id, proof, "question-atomic-followup")
    assert c.execute("SELECT count(*) FROM independent_ai_attempts").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM independent_ai_followup_links").fetchone()[0] == 0
    assert ai.get_clarification(card.request_id).state == "OPEN" and provider.calls == 1
    c.execute("DROP TRIGGER synthetic_followup_failure")
    c.commit()
    provider.response = wire("Use SGD 15.00", {"amount": "15.00"}, ref="reply")
    follow = ai.prepare_followup(card.request_id, proof, "question-atomic-followup")
    assert ai.prepare_followup(card.request_id, proof, "question-atomic-followup") == follow
    assert ai.invoke(follow.attempt_id).state == "VALID"
    published = ai.publish(follow.attempt_id)
    finish_post(c, ai, published, "question-atomic-confirm")
    assert provider.calls == 2


def test_v1_genuine_published_history_reverified_with_original_resources(
    migrated_temp_db_connection,
):
    from test_independent_ai_invocation_v1 import response

    from finance_core.application.ai_invocation import _AiInvocationServiceV1

    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "v1-published-history")
    provider = DurableProvider(c, response=response())
    old = _AiInvocationServiceV1(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=InputV2(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    prepared = old.prepare_initial(intake["public_id"], "v1-historical-creation")
    assert old.invoke(prepared.attempt_id).state == "VALID"
    publication = old.publish(prepared.attempt_id)
    current = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=InputV2(),
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    before = c.total_changes
    assert current.get_status(publication.attempt_id) == publication
    assert current.recover(publication.attempt_id) == publication
    assert c.total_changes == before and provider.calls == 1
    material = json.loads(
        c.execute("SELECT material_json FROM independent_ai_publications").fetchone()[0]
    )
    assert material["schema"] == "finance-independent-ai-publication-v1"
    card = posting(c, current).prepare(publication.proposal_public_id)
    assert card.projection["schema"] == "finance-application-posting-review-v1"
    assert "expense_assessment" not in card.projection
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "reply,expression,expected",
    [
        (
            "Change date to this week Monday",
            {"kind": "calendar_week_weekday", "week_offset": 0, "iso_weekday": 1},
            "2026-10-05",
        ),
        (
            "Change date to last week Sunday",
            {"kind": "calendar_week_weekday", "week_offset": -1, "iso_weekday": 7},
            "2026-10-04",
        ),
        (
            "Change date to previous Friday",
            {"kind": "previous_or_next_weekday", "direction": "previous", "iso_weekday": 5},
            "2026-10-02",
        ),
        (
            "Change date to next Friday",
            {"kind": "previous_or_next_weekday", "direction": "next", "iso_weekday": 5},
            "2026-10-16",
        ),
        (
            "Change date to October 10, 2026",
            {"kind": "absolute_date", "year": 2026, "month": 10, "day": 10},
            "2026-10-10",
        ),
        (
            "日期改成2026年10月10日",
            {"kind": "absolute_date", "year": 2026, "month": 10, "day": 10},
            "2026-10-10",
        ),
    ],
)
def test_v2_all_finite_calendar_operations_create_true_card_and_once_post(
    migrated_temp_db_connection, reply, expression, expected
):
    c = migrated_temp_db_connection
    ai, provider, first = published_text(c, "calendar")
    current_time = 1791504000
    ai._clock = lambda: current_time
    provider.at = current_time
    owner = posting(c, ai)
    owner._clock = lambda: current_time
    old = owner.prepare(first.proposal_public_id)
    proof = persist_reply(c, old, reply, at=current_time)
    provider.response = wire(
        reply,
        {"transaction_date": expected},
        ref="reply",
        date_expression=expression,
        evidence={"transaction_date": [cite(reply, ref="reply")]},
    )
    attempt = ai.prepare_edit(old.review_id, proof, "calendar-edit")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    new = ai.publish(attempt.attempt_id)
    card, _ = finish_post(c, ai, new, "calendar-confirm", clock=lambda: current_time)
    assert card.projection["expense_assessment"]["canonical_fields"]["transaction_date"] == expected
    assert c.execute("SELECT transaction_date FROM transactions").fetchone()[0] == expected, (
        "STOP_FINANCIAL_FACT_MISMATCH"
    )
    assert provider.calls == 2


@pytest.mark.parametrize("reply", ["不是20，改成15", "金额改15，商家改成Cafe", "改成15并确认"])
def test_v2_chinese_exact_natural_edits_real_new_card_once_post(migrated_temp_db_connection, reply):
    c = migrated_temp_db_connection
    ai, provider, first = published_text(c, "chinese-edit")
    owner = posting(c, ai)
    card = owner.prepare(first.proposal_public_id)
    provider.response = wire("Change amount to SGD 20.00", {"amount": "20.00"}, ref="reply")
    proof = persist_reply(c, card, "Change amount to SGD 20.00", identity="chinese-old20")
    attempt = ai.prepare_edit(card.review_id, proof, "chinese-old20-op")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    old = owner.prepare(ai.publish(attempt.attempt_id).proposal_public_id)
    fields = {"amount": "15.00"}
    evidence = {"amount": [cite(reply, "15", "reply")]}
    if reply.startswith("不是"):
        evidence["amount"].insert(0, cite(reply, "20", "reply", "previous_value"))
    if "商家" in reply:
        fields["merchant"] = "Cafe"
        evidence["merchant"] = [cite(reply, "Cafe", "reply")]
    provider.response = wire(reply, fields, ref="reply", evidence=evidence)
    proof = persist_reply(c, old, reply, identity="chinese-new15")
    edit = ai.prepare_edit(old.review_id, proof, "chinese-new15-op")
    assert ai.invoke(edit.attempt_id).state == "VALID"
    child = ai.publish(edit.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    fresh = owner.prepare(child.proposal_public_id)
    summary = fresh.projection["expense_assessment"]["change_summary"]
    assert summary["before"]["amount"] == "20.00" and summary["after"]["amount"] == "15.00"
    assert {x["field"] for x in summary["changes"]} == set(fields)
    assert fresh.review_hash != old.review_hash
    _, final = finish_post(c, ai, child, "chinese-fresh-confirm")
    assert tuple(
        c.execute(
            "SELECT amount,currency,transaction_date,merchant,public_id FROM transactions"
        ).fetchone()
    ) == (
        15.0,
        "SGD",
        "2026-10-08",
        fields.get("merchant", "Example Cafe"),
        final.transaction_public_id,
    ), "STOP_FINANCIAL_FACT_MISMATCH"
    assert provider.calls == 3


@pytest.mark.parametrize("wrong_roles", [False, True])
def test_v2_chinese_uncertain_amount_model_choice_cannot_publish(
    migrated_temp_db_connection, wrong_roles
):
    c = migrated_temp_db_connection
    ai, provider, first = published_text(c, "chinese-uncertain")
    owner = posting(c, ai)
    old = owner.prepare(first.proposal_public_id)
    text = "20或者15，我不确定"
    evidence = [cite(text, "20", "reply")]
    if wrong_roles:
        # Even deliberately false old/new model roles cannot erase 或者/不确定.
        evidence.append(cite(text, "15", "reply", "previous_value"))
    provider.response = wire(text, {"amount": "20.00"}, ref="reply", evidence={"amount": evidence})
    proof = persist_reply(c, old, text, identity="chinese-uncertain")
    attempt = ai.prepare_edit(old.review_id, proof, "chinese-uncertain-op")
    status = ai.invoke(attempt.attempt_id)
    assert status.state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    if status.state == "NEEDS_CLARIFICATION":
        question = ai.prepare_clarification(attempt.attempt_id)
        assert any("amount" in q["fields"] for q in question.projection["unresolved"])
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    if status.state == "NEEDS_CLARIFICATION":
        answer = "金额改15"
        provider.response = wire(answer, {"amount": "15.00"}, ref="reply")
        proof = persist_reply(c, question, answer, identity="chinese-uncertain-answer")
        follow = ai.prepare_followup(question.request_id, proof, "chinese-uncertain-answer-op")
        assert ai.invoke(follow.attempt_id).state == "VALID"
        child = ai.publish(follow.attempt_id)
        _, final = finish_post(c, ai, child, "chinese-uncertain-fresh-confirm")
        assert tuple(
            c.execute("SELECT amount,currency,public_id FROM transactions").fetchone()
        ) == (15.0, "SGD", final.transaction_public_id), "STOP_FINANCIAL_FACT_MISMATCH"


@pytest.mark.parametrize("model_intent", ["refund", "personal_expense"])
def test_v2_chinese_refund_never_becomes_expense_even_wrong_model(
    migrated_temp_db_connection, model_intent
):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    c = migrated_temp_db_connection
    text = "退了12.50"
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="chinese-refund", raw_text=text)
    body = json.loads(wire(text, {"amount": "12.50"}))
    body["intent"] = model_intent
    body["outcome"] = "unsupported" if model_intent == "refund" else "proposal"
    ai, provider = service_v2(c, json.dumps(body, ensure_ascii=False))
    attempt = ai.prepare_initial(intake["public_id"], "chinese-refund-op")
    assert ai.invoke(attempt.attempt_id).state == "RESPONSE_REJECTED"
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert provider.calls == 1


@pytest.mark.parametrize(
    "cue,expression,expected",
    [
        ("昨天", {"kind": "day_offset", "days": -1}, "1969-12-31"),
        (
            "上周五",
            {"kind": "calendar_week_weekday", "week_offset": -1, "iso_weekday": 5},
            "1969-12-26",
        ),
    ],
)
def test_v2_chinese_natural_dates_authentic_reply_once_post(
    migrated_temp_db_connection, cue, expression, expected
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "chinese-date")
    old = posting(c, ai).prepare(initial.proposal_public_id)
    text = "日期改成" + cue
    proof = persist_reply(c, old, text, identity="chinese-date")
    provider.response = wire(
        text,
        {"transaction_date": expected},
        ref="reply",
        date_expression=expression,
        evidence={"transaction_date": [cite(text, cue, "reply")]},
    )
    attempt = ai.prepare_edit(old.review_id, proof, "chinese-date-op")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    publication = ai.publish(attempt.attempt_id)
    card, final = finish_post(c, ai, publication, "chinese-date-confirm")
    context = provider.requests[-1]["input"]["interpretation_context"]
    assert context["anchor_kind"] == "reply" and context["anchor_timestamp"] == NOW
    assert card.projection["financial_projection"]["transaction_date"] == expected
    assert tuple(c.execute("SELECT transaction_date,public_id FROM transactions").fetchone()) == (
        expected,
        final.transaction_public_id,
    ), "STOP_FINANCIAL_FACT_MISMATCH"


@pytest.mark.parametrize("question_mode", ["empty", "date_only"])
@pytest.mark.parametrize("fake_context", [False, "draft", "actual"])
def test_v2_clarification_core_money_obligations_survive_date_only_followup(
    migrated_temp_db_connection, question_mode, fake_context
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "core-money-question")
    owner = posting(c, ai)
    old = owner.prepare(initial.proposal_public_id)
    text = "Change amount to XYZ 15.00 and date to 2026-10-08 or 2026-10-09"
    questions = (
        [
            {
                "question_id": "date",
                "fields": ["transaction_date"],
                "reason": "ambiguous",
                "candidates": ["2026-10-08", "2026-10-09"],
                "evidence": [cite(text, ref="reply")],
            }
        ]
        if question_mode == "date_only"
        else []
    )
    provider.response = wire(
        text,
        {"amount": "15.00", "transaction_date": "2026-10-09"},
        ref="reply",
        unresolved=questions,
    )
    proof = persist_reply(c, old, text, identity="core-money-question")
    first = ai.prepare_edit(old.review_id, proof, "core-money-question-op")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    question = ai.prepare_clarification(first.attempt_id)
    assert {"amount", "currency", "transaction_date"} <= {
        f for q in question.projection["unresolved"] for f in q["fields"]
    }
    reply = "Use date 2026-10-09"
    fields = {"transaction_date": "2026-10-09"}
    evidence = None
    if fake_context:
        fields["amount"] = "15.00" if fake_context == "draft" else "12.50"
        context = json.dumps(
            provider.requests[-1]["input"]["current_values"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        evidence = {
            "transaction_date": [cite(reply, "2026-10-09", "reply")],
            "amount": [cite(context, "12.50", "context", "retained_value")],
        }
    provider.response = wire(reply, fields, ref="reply", evidence=evidence)
    proof = persist_reply(c, question, reply, identity="core-date-only")
    follow = ai.prepare_followup(question.request_id, proof, "core-date-only-op")
    state = ai.invoke(follow.attempt_id).state
    assert state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    with pytest.raises(AiInvocationError):
        ai.publish(follow.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    if state == "NEEDS_CLARIFICATION":
        next_question = ai.prepare_clarification(follow.attempt_id)
        assert {"amount", "currency"} <= {
            f for q in next_question.projection["unresolved"] for f in q["fields"]
        }
        reply = "Use SGD 15.00 and date 2026-10-09"
        provider.response = wire(
            reply,
            {"amount": "15.00", "currency": "SGD", "transaction_date": "2026-10-09"},
            ref="reply",
        )
        proof = persist_reply(c, next_question, reply, identity="core-complete-money")
        complete = ai.prepare_followup(next_question.request_id, proof, "core-complete-money-op")
        assert ai.invoke(complete.attempt_id).state == "VALID"
        publication = ai.publish(complete.attempt_id)
        _, final = finish_post(c, ai, publication, "core-complete-money-confirm")
        assert tuple(
            c.execute(
                "SELECT amount,currency,transaction_date,public_id FROM transactions"
            ).fetchone()
        ) == (15.0, "SGD", "2026-10-09", final.transaction_public_id), (
            "STOP_FINANCIAL_FACT_MISMATCH"
        )


def test_v2_initial_human_money_conflict_then_authenticated_replacement_once_post(tmp_path):
    from test_independent_ai_history_v1 import _mixed_human, _mixed_receipt

    c, ai, provider, human, owner, parent, intake = _mixed_receipt(
        tmp_path, extra_amounts=(("CASH", "20.00"),)
    )
    try:
        current = _mixed_human(c, human, parent, {"amount": "20.00"}, "initial-human20")
        ai._input_port = InputV2()
        first = ai.prepare_initial(intake, "initial-human-conflict")
        assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
        with pytest.raises(AiInvocationError):
            ai.publish(first.attempt_id)
        request = ai.prepare_clarification(first.attempt_id)
        assert request.projection["proposal_public_id"] == current.proposal_public_id
        reply = "不是20，改成15 SGD"
        provider.response = wire(
            reply,
            {"amount": "15.00", "currency": "SGD"},
            ref="reply",
            evidence={
                "amount": [
                    cite(reply, "20", "reply", "previous_value"),
                    cite(reply, "15", "reply"),
                ],
                "currency": [cite(reply, "SGD", "reply")],
            },
        )
        proof = persist_reply(c, request, reply, identity="initial-human-replacement")
        follow = ai.prepare_followup(request.request_id, proof, "initial-human-replacement-op")
        assert ai.invoke(follow.attempt_id).state == "VALID"
        publication = ai.publish(follow.attempt_id)
        _, final = finish_post(
            c,
            ai,
            publication,
            "initial-human-replacement-confirm",
            source=owner._source_port,
            decision=owner._decision_port,
        )
        assert tuple(
            c.execute("SELECT amount,currency,public_id FROM transactions").fetchone()
        ) == (15.0, "SGD", final.transaction_public_id), "STOP_FINANCIAL_FACT_MISMATCH"
        assert provider.calls == 2
    finally:
        c.close()


@pytest.mark.parametrize(
    "literal", ["air_user_description", "aipo_user_category", "aipub_user_text"]
)
def test_v2_user_literals_are_not_structured_authority_markers(
    migrated_temp_db_connection, literal
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "user-prefix")
    old = posting(c, ai).prepare(initial.proposal_public_id)
    text = "Change description to " + literal
    provider.response = wire(text, {"description": literal}, ref="reply")
    proof = persist_reply(c, old, text, identity="user-prefix")
    edit = ai.prepare_edit(old.review_id, proof, "user-prefix-op")
    assert ai.invoke(edit.attempt_id).state == "VALID"
    child = ai.publish(edit.attempt_id)
    _, final = finish_post(c, ai, child, "user-prefix-confirm")
    assert tuple(c.execute("SELECT description,public_id FROM transactions").fetchone()) == (
        literal,
        final.transaction_public_id,
    ), "STOP_FINANCIAL_FACT_MISMATCH"


@pytest.mark.parametrize(
    "extra", ["amount XYZ 15.00", "amount SGD -15.00", "amount SGD 15.00", "date 2026-10-09"]
)
def test_v2_nonfinancial_literal_quote_never_masks_other_requirements(
    migrated_temp_db_connection, extra
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "literal-unprocessed")
    old = posting(c, ai).prepare(initial.proposal_public_id)
    text = "Change category to new_food, " + extra
    provider.response = wire(
        text,
        {"category": "new_food"},
        ref="reply",
        evidence={"category": [cite(text, ref="reply")]},
    )
    proof = persist_reply(c, old, text, identity="literal-unprocessed")
    attempt = ai.prepare_edit(old.review_id, proof, "literal-unprocessed-op")
    assert ai.invoke(attempt.attempt_id).state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )


def test_v2_unknown_total_abc_is_currency_control_not_merchant_header(tmp_path):
    from test_independent_ai_invocation_v1 import _review_fix_receipt, _source_money_quote

    c, ai, provider, _, intake, *_ = _review_fix_receipt(tmp_path, total_currency="ABC")
    try:
        text = _source_money_quote(c)
        provider.response = wire(
            text, {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
        )
        first = ai.prepare_initial(intake, "unknown-total-abc")
        assert ai.invoke(first.attempt_id).state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
        with pytest.raises(AiInvocationError):
            ai.publish(first.attempt_id)
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
        assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    finally:
        c.close()


@pytest.mark.parametrize(
    "field,text",
    [
        ("amount", "Change amount to 15.00"),
        ("currency", "Change currency to USD"),
        ("transaction_date", "Change date to yesterday"),
        ("merchant", "Change merchant to New Cafe"),
        ("description", "Change description to Dinner"),
        ("category", "Change category to travel"),
    ],
)
def test_v2_explicit_edit_cannot_be_resolved_by_retained_context(
    migrated_temp_db_connection, field, text
):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "retained-new-" + field)
    old = posting(c, ai).prepare(initial.proposal_public_id)
    context = canonical_text(ORIGINAL)
    provider.response = wire(
        text,
        {field: ORIGINAL[field]},
        ref="reply",
        evidence={field: [cite(context, ORIGINAL[field], "context", "retained_value")]},
    )
    reply = persist_reply(c, old, text, identity="retained-new-reply")
    attempt = ai.prepare_edit(old.review_id, reply, "retained-new-op")
    assert ai.invoke(attempt.attempt_id).state == "NEEDS_CLARIFICATION"
    request = ai.prepare_clarification(attempt.attempt_id)
    assert field in {f for q in request.projection["unresolved"] for f in q["fields"]}
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["initial", "card_edit", "clarification_followup"])
@pytest.mark.parametrize(
    "cue", ["yesterday or tomorrow", "last month", "上个月", "next year", "10/08"]
)
def test_v2_known_date_obligation_cannot_default_or_retain(migrated_temp_db_connection, kind, cue):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    c = migrated_temp_db_connection
    if kind == "initial":
        text = "Lunch SGD 12.50 at Example Cafe " + cue
        _, _, _, intake, *_ = _prepare_text_subject(c, suffix="date-known", raw_text=text)
        value = "1970-01-01"
        context = canonical_text({"local_date": value})
        evidence = {"transaction_date": [cite(context, value, "source_context")]}
        fields = {**ORIGINAL, "transaction_date": value}
        ai, provider = service_v2(c, wire(text, fields, evidence=evidence))
        attempt = ai.prepare_initial(intake["public_id"], "known-date-op")
    else:
        ai, provider, initial = published_text(c, "known-date")
        old = posting(c, ai).prepare(initial.proposal_public_id)
        if kind == "clarification_followup":
            question_text = "Change date to an uncertain day"
            question = {
                "question_id": "day",
                "fields": ["transaction_date"],
                "reason": "ambiguous",
                "candidates": [],
                "evidence": [cite(question_text, ref="reply")],
            }
            provider.response = wire(
                question_text, {}, ref="reply", unresolved=[question], outcome="clarification"
            )
            reply = persist_reply(c, old, question_text, identity="known-date-question")
            first = ai.prepare_edit(old.review_id, reply, "known-date-question-op")
            assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
            old = ai.prepare_clarification(first.attempt_id)
        text = "Change date to " + cue
        # Wrong model pretends a source/context day resolves the new request.
        value = ORIGINAL["transaction_date"]
        context = canonical_text(ORIGINAL)
        evidence = {"transaction_date": [cite(context, value, "context", "retained_value")]}
        provider.response = wire(text, {"transaction_date": value}, ref="reply", evidence=evidence)
        reply = persist_reply(c, old, text, identity="known-date-reply")
        attempt = (
            ai.prepare_edit(old.review_id, reply, "known-date-op")
            if kind == "card_edit"
            else ai.prepare_followup(old.request_id, reply, "known-date-op")
        )
    state = ai.invoke(attempt.attempt_id).state
    assert state in {"NEEDS_CLARIFICATION", "RESPONSE_REJECTED"}
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text,expression",
    [
        ("Change date to yesterday or tomorrow", {"kind": "day_offset", "days": -1}),
        ("日期改为昨天或明天", {"kind": "day_offset", "days": -1}),
    ],
)
def test_v2_natural_date_alternative_cannot_select_one(
    migrated_temp_db_connection, text, expression
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "natural-choice")
    old = posting(c, ai).prepare(initial.proposal_public_id)
    provider.response = wire(
        text,
        {"transaction_date": "1970-01-01"},
        ref="reply",
        date_expression=expression,
        evidence={"transaction_date": [cite(text, ref="reply")]},
    )
    reply = persist_reply(c, old, text, identity="natural-choice-reply")
    attempt = ai.prepare_edit(old.review_id, reply, "natural-choice-op")
    assert ai.invoke(attempt.attempt_id).state == "NEEDS_CLARIFICATION"
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("merchant", "May Cafe"),
        ("description", "last month"),
        ("description", "yesterday or tomorrow"),
    ],
)
def test_v2_date_words_in_explicit_literal_fields_survive(
    migrated_temp_db_connection, field, value
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "date-literal")
    old = posting(c, ai).prepare(initial.proposal_public_id)
    text = "Change " + field + " to " + value
    provider.response = wire(text, {field: value}, ref="reply")
    reply = persist_reply(c, old, text, identity="date-literal-reply")
    attempt = ai.prepare_edit(old.review_id, reply, "date-literal-op")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    fresh = ai.publish(attempt.attempt_id)
    _, final = finish_post(c, ai, fresh, "date-literal-confirm")
    row = c.execute("SELECT merchant,description,public_id FROM transactions").fetchone()
    assert row[0 if field == "merchant" else 1] == value
    assert row[2] == final.transaction_public_id


@pytest.mark.parametrize(
    "column",
    [
        "request_id",
        "predecessor_attempt_id",
        "successor_attempt_id",
        "source_event_key",
        "reply_evidence_id",
        "reply_evidence_digest",
        "accepted_at",
    ],
)
@pytest.mark.parametrize("phase", ["before_publication", "accepted_history"])
def test_v2_all_followup_mirrors_reject_authenticated_seal_disagreement(
    migrated_temp_db_connection, column, phase
):
    c = migrated_temp_db_connection
    text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    q = {
        "question_id": "amount",
        "fields": ["amount"],
        "reason": "ambiguous",
        "candidates": [],
        "evidence": [cite(text)],
    }
    _, _, _, intake, *_ = subject(c, "mirror-corrected")
    ai, provider = service_v2(c, wire(text, unresolved=[q], outcome="clarification"))
    first = ai.prepare_initial(intake["public_id"], "mirror-corrected-initial")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    card = ai.prepare_clarification(first.attempt_id)
    reply = persist_reply(c, card, "Use SGD 15.00", identity="mirror-corrected-reply")
    provider.response = wire("Use SGD 15.00", {"amount": "15.00"}, ref="reply")
    follow = ai.prepare_followup(card.request_id, reply, "mirror-corrected-followup")
    assert ai.invoke(follow.attempt_id).state == "VALID"
    final = None
    if phase == "accepted_history":
        published = ai.publish(follow.attempt_id)
        _, final = finish_post(c, ai, published, "mirror-corrected-confirm")
    # Real foreign FK identities, rather than nonexistent-ID shortcuts.
    _, _, _, foreign_intake, *_ = subject(c, "mirror-foreign")
    foreign = ai.prepare_initial(foreign_intake["public_id"], "mirror-foreign-initial")
    provider.response = wire(text, unresolved=[q], outcome="clarification")
    assert ai.invoke(foreign.attempt_id).state == "NEEDS_CLARIFICATION"
    foreign_card = ai.prepare_clarification(foreign.attempt_id)
    replacement = {
        "request_id": foreign_card.request_id,
        "predecessor_attempt_id": foreign.attempt_id,
        "successor_attempt_id": foreign.attempt_id,
        "source_event_key": "e" * 64,
        "reply_evidence_id": "foreign-reply",
        "reply_evidence_digest": "f" * 64,
        "accepted_at": NOW + 1,
    }[column]
    before = c.execute("SELECT count(*) FROM transactions").fetchone()[0]
    trigger = c.execute(
        "SELECT sql FROM sqlite_master WHERE name='independent_ai_followup_links_no_update'"
    ).fetchone()[0]
    c.execute("DROP TRIGGER independent_ai_followup_links_no_update")
    c.execute(
        f"UPDATE independent_ai_followup_links SET {column}=? WHERE successor_attempt_id=?",
        (replacement, follow.attempt_id),
    )
    c.execute(trigger)
    c.commit()
    for read in (
        lambda: ai.get_status(follow.attempt_id),
        lambda: ai.get_clarification(card.request_id),
        lambda: ai.prepare_followup(card.request_id, reply, "mirror-corrected-followup"),
        lambda: ai.publish(follow.attempt_id),
    ):
        with pytest.raises(AiInvocationError):
            read()
    if final is not None:
        with pytest.raises((AiInvocationError, ValueError)):
            posting(c, ai).get_status(final.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == before


def _v2_accepted_text_before_conversion(c, monkeypatch, suffix="text-authority", *, edited=False):
    from finance_core.application import posting as module

    ai, provider, publication = published_text(c, suffix)
    owner = posting(c, ai)
    if edited:
        prior = owner.prepare(publication.proposal_public_id)
        text = "Change amount to SGD 15.00"
        reply = persist_reply(c, prior, text, identity=suffix + "-input-reply")
        provider.response = wire(text, {"amount": "15.00"}, ref="reply")
        changed = ai.prepare_edit(prior.review_id, reply, suffix + "-edit-op")
        assert ai.invoke(changed.attempt_id).state == "VALID"
        publication = ai.publish(changed.attempt_id)
    card = owner.prepare(publication.proposal_public_id)
    _persist_fresh_posting_confirmation(
        c, card, publication.proposal_public_id, suffix + "-decision"
    )

    def stop(stage):
        if stage == "after_acceptance_commit":
            raise RuntimeError("registered stop after genuine acceptance")

    monkeypatch.setattr(module, "_failure_injection_hook", stop)
    with pytest.raises(RuntimeError, match="registered stop"):
        owner.submit_post(card.review_id, suffix + "-decision")
    monkeypatch.setattr(module, "_failure_injection_hook", None)
    attempt = c.execute(
        "SELECT attempt_id FROM application_posting_decisions WHERE decision_id=?",
        (suffix + "-decision",),
    ).fetchone()[0]
    parser_id = c.execute(
        "SELECT id FROM parser_outputs WHERE public_id=?", (publication.proposal_public_id,)
    ).fetchone()[0]
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    return ai, provider, owner, attempt, parser_id


@pytest.mark.parametrize(
    "table", ["synthetic_sources", "synthetic_decisions", "synthetic_displays", "synthetic_replies"]
)
@pytest.mark.parametrize("corruption", ["signature", "missing", "signed_contradiction"])
@pytest.mark.parametrize("phase", ["before_conversion", "converted_history"])
def test_v2_text_direct_authority_rechecks_real_human_evidence(
    migrated_temp_db_connection, monkeypatch, table, corruption, phase
):
    from test_application_posting_recovery_v1 import DECISION_KEY, SOURCE_KEY, _replace

    from finance_core.parser_proposals import decision_owner

    c = migrated_temp_db_connection
    ai, provider, owner, attempt, parser_id = _v2_accepted_text_before_conversion(c, monkeypatch)
    kwargs = owner._text_kwargs(attempt)
    if phase == "converted_history":
        created = decision_owner.convert_confirmed_parser_proposal(c, parser_id, **kwargs)
        assert created["final_transaction_created"]
    before = [tuple(r) for r in c.execute("SELECT * FROM transactions")]
    conversion_before = [
        tuple(r) for r in c.execute("SELECT * FROM parser_proposal_conversion_audit")
    ]
    audit_before = [tuple(r) for r in c.execute("SELECT * FROM financial_audit_events")]
    accepted_decision_id = c.execute(
        "SELECT decision_id FROM application_posting_decisions WHERE attempt_id=?", (attempt,)
    ).fetchone()[0]
    actual_decision = _load(c, "synthetic_decisions", accepted_decision_id, DECISION_KEY)
    identity = {
        "synthetic_sources": actual_decision["source_evidence_id"],
        "synthetic_decisions": accepted_decision_id,
        "synthetic_displays": actual_decision["display_id"],
        "synthetic_replies": actual_decision["reply_id"],
    }[table]
    if corruption == "signature":
        c.execute(f"UPDATE {table} SET signature=? WHERE id=?", ("0" * 64, identity))
    elif corruption == "missing":
        c.execute(f"DELETE FROM {table} WHERE id=?", (identity,))
    else:
        key = SOURCE_KEY if table == "synthetic_sources" else DECISION_KEY
        material = _load(c, table, identity, key)
        changed_key = {
            "synthetic_sources": "source_event_id",
            "synthetic_decisions": "action",
            "synthetic_displays": "human_principal_id",
            "synthetic_replies": "human_principal_id",
        }[table]
        material[changed_key] = "contradictory-owner"
        _replace(c, table, identity, material, key)
    c.commit()
    # Prove this exact accepted chain is invalid before any financial entry.
    from finance_core.application.posting import PostingService

    c.execute("BEGIN DEFERRED")
    try:
        with pytest.raises(ValueError):
            PostingService._accepted(owner, attempt, verify_confirmation=True)
    finally:
        c.rollback()
    for operation in (
        lambda: owner.resume_post(attempt),
        lambda: decision_owner.convert_confirmed_parser_proposal(
            c, parser_id, ai_lineage_authority=ai, persistence_effect=lambda *_: None
        ),
        lambda: decision_owner.convert_confirmed_parser_proposal(c, parser_id, **kwargs),
        lambda: decision_owner.verify_converted_parser_proposal(c, parser_id, **kwargs),
    ):
        with pytest.raises(ValueError):
            operation()
        assert [tuple(r) for r in c.execute("SELECT * FROM transactions")] == before
        assert [
            tuple(r) for r in c.execute("SELECT * FROM parser_proposal_conversion_audit")
        ] == conversion_before
        assert [tuple(r) for r in c.execute("SELECT * FROM financial_audit_events")] == audit_before
    assert c.execute("SELECT count(*) FROM application_posting_decisions").fetchone()[0] == 1
    assert provider.calls == 1


@pytest.mark.parametrize(
    "authority_kind",
    [
        "none",
        "dict",
        "bool",
        "noop",
        "forged",
        "subclass",
        "wrong_attempt",
        "wrong_connection",
        "missing_acceptance",
    ],
)
def test_v2_text_direct_cannot_choose_or_omit_accepted_authority(
    migrated_temp_db_connection, monkeypatch, authority_kind
):
    from finance_core.application.posting import _AcceptedTextPostingAuthorization
    from finance_core.application.text_posting_authorization import VerifiedTextPostingAuthorization
    from finance_core.parser_proposals import decision_owner

    c = migrated_temp_db_connection
    ai, _, owner, attempt, parser_id = _v2_accepted_text_before_conversion(c, monkeypatch)
    called = []

    class Forged:
        def verify_in_transaction(self, connection, expected):
            called.append(True)
            return VerifiedTextPostingAuthorization(
                expected,
                "full_card_ai_expense_v1",
                attempt,
                "forged",
                "f" * 64,
                "forged",
                "f" * 64,
                "f" * 64,
                "f" * 64,
            )

    class Subclass(_AcceptedTextPostingAuthorization):
        pass

    authority = {
        "none": None,
        "dict": {},
        "bool": True,
        "noop": lambda *_: None,
        "forged": Forged(),
        "subclass": Subclass(owner, attempt),
        "wrong_attempt": _AcceptedTextPostingAuthorization(owner, "apa_" + "f" * 64),
    }.get(authority_kind)
    other = None
    if authority_kind == "wrong_connection":
        import sqlite3

        other = sqlite3.connect(":memory:")
        authority = _AcceptedTextPostingAuthorization(posting(other, ai), attempt)
    if authority_kind == "missing_acceptance":
        authority = _AcceptedTextPostingAuthorization(owner, attempt)
        triggers = c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='application_posting_decisions'"
        ).fetchall()
        for name, _ in triggers:
            c.execute(f'DROP TRIGGER "{name}"')
        c.execute("DELETE FROM application_posting_decisions WHERE attempt_id=?", (attempt,))
        for _, ddl in triggers:
            c.execute(ddl)
        c.commit()
    try:
        for operation in (
            decision_owner.convert_confirmed_parser_proposal,
            decision_owner.verify_converted_parser_proposal,
        ):
            with pytest.raises(ValueError):
                operation(
                    c, parser_id, ai_lineage_authority=ai, text_posting_authorization=authority
                )
        assert not called
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM parser_proposal_conversion_audit").fetchone()[0] == 0
    finally:
        if other is not None:
            other.close()


def test_v2_text_fixed_authority_create_replay_readback_same_snapshot(
    migrated_temp_db_connection, monkeypatch
):
    from finance_core.parser_proposals import decision_owner

    c = migrated_temp_db_connection
    ai, provider, owner, attempt, parser_id = _v2_accepted_text_before_conversion(c, monkeypatch)
    checks = []
    for label, verifier, method in (
        ("posting_source", owner._source_port, "verify_persisted"),
        ("human_decision", owner._decision_port, "verify_persisted"),
        ("ai_source", ai._source_port, "verify_persisted"),
        ("provider_observation", provider, "verify_persisted"),
    ):
        original = getattr(verifier, method)

        def observed(connection, *args, original=original, label=label, **kwargs):
            assert connection is c and connection.in_transaction
            checks.append(label)
            return original(connection, *args, **kwargs)

        monkeypatch.setattr(verifier, method, observed)
    kwargs = owner._text_kwargs(attempt)
    first = decision_owner.convert_confirmed_parser_proposal(c, parser_id, **kwargs)
    replay = decision_owner.convert_confirmed_parser_proposal(
        c, parser_id, persistence_effect=lambda *_: None, **kwargs
    )
    assert replay["idempotent"] and not first["idempotent"]
    assert replay["transaction_public_id"] == first["transaction_public_id"]
    assert (
        decision_owner.verify_converted_parser_proposal(c, parser_id, **kwargs)[
            "transaction_public_id"
        ]
        == first["transaction_public_id"]
    )
    assert not c.in_transaction
    c.execute("BEGIN DEFERRED")
    try:
        assert (
            decision_owner.verify_converted_parser_proposal(c, parser_id, **kwargs)[
                "transaction_public_id"
            ]
            == first["transaction_public_id"]
        )
        assert c.in_transaction
    finally:
        c.rollback()
    assert set(checks) == {"posting_source", "human_decision", "ai_source", "provider_observation"}
    assert provider.calls == 1
    assert owner.resume_post(attempt).transaction_public_id == first["transaction_public_id"]
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    import sqlite3

    path = c.execute("PRAGMA database_list").fetchone()[2]
    read_only = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    read_only.row_factory = sqlite3.Row
    read_only.execute("PRAGMA foreign_keys=ON")
    try:
        historical_ai, _ = service_v2(read_only, None)
        historical_owner = posting(read_only, historical_ai)
        assert (
            decision_owner.verify_converted_parser_proposal(
                read_only, parser_id, **historical_owner._text_kwargs(attempt)
            )["transaction_public_id"]
            == first["transaction_public_id"]
        )
        assert not read_only.in_transaction
    finally:
        read_only.close()


@pytest.mark.parametrize("field", ["merchant", "description", "category"])
def test_v2_reply_replacement_label_cannot_resolve_wrong_literal(
    migrated_temp_db_connection, field
):
    c = migrated_temp_db_connection
    ai, provider, publication = published_text(c, "wrong-literal-" + field)
    card = posting(c, ai).prepare(publication.proposal_public_id)
    text = "Change " + field + " to Fresh literal"
    provider.response = wire(text, {field: ORIGINAL[field]}, ref="reply")
    reply = persist_reply(c, card, text, identity="wrong-literal-reply")
    attempt = ai.prepare_edit(card.review_id, reply, "wrong-literal-op")
    assert ai.invoke(attempt.attempt_id).state == "NEEDS_CLARIFICATION"
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "table", ["synthetic_interpretation_replies", "synthetic_interpretation_displays"]
)
@pytest.mark.parametrize("phase", ["before_conversion", "converted_history"])
def test_v2_text_authority_rechecks_actual_edit_input_chain(
    migrated_temp_db_connection, monkeypatch, table, phase
):
    from finance_core.application.posting import PostingService
    from finance_core.parser_proposals import decision_owner

    c = migrated_temp_db_connection
    ai, provider, owner, attempt, parser_id = _v2_accepted_text_before_conversion(
        c, monkeypatch, "text-input-authority", edited=True
    )
    kwargs = owner._text_kwargs(attempt)
    if phase == "converted_history":
        decision_owner.convert_confirmed_parser_proposal(c, parser_id, **kwargs)
    before = [tuple(row) for row in c.execute("SELECT * FROM transactions")]
    proof_id = "text-input-authority-input-reply"
    proof = _load(c, "synthetic_interpretation_replies", proof_id, KEY)
    identity = proof_id if table == "synthetic_interpretation_replies" else proof["display_id"]
    c.execute(f"UPDATE {table} SET signature=? WHERE id=?", ("0" * 64, identity))
    c.commit()
    c.execute("BEGIN DEFERRED")
    try:
        with pytest.raises(ValueError):
            PostingService._accepted(owner, attempt, verify_confirmation=True)
    finally:
        c.rollback()
    for operation in (
        decision_owner.convert_confirmed_parser_proposal,
        decision_owner.verify_converted_parser_proposal,
    ):
        with pytest.raises(ValueError):
            operation(c, parser_id, **kwargs)
        assert [tuple(row) for row in c.execute("SELECT * FROM transactions")] == before
    assert provider.calls == 2


@pytest.mark.parametrize(
    "separator", ["; date yesterday", ", date last month", " and date yesterday"]
)
def test_v2_model_literal_cannot_swallow_separate_date_instruction(separator):
    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

    value = "Cafe" + separator
    text = "Change merchant to " + value
    projection = {
        "interaction_kind": "card_edit",
        "source_text": "",
        "reply_text": text,
        "current_values": ORIGINAL,
        "interpretation_context": {
            "anchor_timestamp": NOW,
            "timezone": "UTC",
            "local_date": "1970-01-01",
            "default_currency": "SGD",
        },
    }
    result = validate_response_v2(
        wire(text, {"merchant": value}, ref="reply"), projection, ORIGINAL
    )
    assert result["state"] == "NEEDS_CLARIFICATION"
    assert "transaction_date" in {f for q in result["unresolved"] for f in q["fields"]}


def test_v2_explicit_quoted_date_words_are_literal_value():
    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

    value = "Cafe; date yesterday"
    text = 'Change merchant to "' + value + '"'
    projection = {
        "interaction_kind": "card_edit",
        "source_text": "",
        "reply_text": text,
        "current_values": ORIGINAL,
        "interpretation_context": {
            "anchor_timestamp": NOW,
            "timezone": "UTC",
            "local_date": "1970-01-01",
            "default_currency": "SGD",
        },
    }
    result = validate_response_v2(
        wire(text, {"merchant": value}, ref="reply"), projection, ORIGINAL
    )
    assert result["state"] == "VALID"
    assert result["payload"]["merchant"] == value


@pytest.mark.parametrize("tail", ["last month", "上个月", "next year", "yesterday", "May 8"])
def test_v2_initial_unquoted_merchant_cannot_erase_date_obligation(tail):
    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    text = "Lunch SGD 12.50 at Example Cafe " + tail
    context = {
        "anchor_timestamp": NOW,
        "timezone": "UTC",
        "local_date": "1970-01-01",
        "default_currency": "SGD",
    }
    fields = {
        **ORIGINAL,
        "transaction_date": context["local_date"],
        "merchant": "Example Cafe " + tail,
    }
    projection = {
        "interaction_kind": "initial",
        "source_text": text,
        "reply_text": "",
        "current_values": ORIGINAL,
        "interpretation_context": context,
        "selection": {"kind": "text"},
    }
    raw = wire(
        text,
        fields,
        evidence={
            "transaction_date": [
                cite(canonical_text(context), context["local_date"], "source_context")
            ]
        },
    )
    result = validate_response_v2(raw, projection, ORIGINAL)
    assert result["state"] == "NEEDS_CLARIFICATION"
    assert "transaction_date" in {f for q in result["unresolved"] for f in q["fields"]}


@pytest.mark.parametrize(
    "merchant,quoted", [("May Cafe", False), ("May Cafe", True), ("Cafe last month", True)]
)
def test_v2_initial_bounded_merchant_date_words_remain_literal(merchant, quoted):
    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    text = "Lunch SGD 12.50 at " + ('"' + merchant + '"' if quoted else merchant)
    context = {
        "anchor_timestamp": NOW,
        "timezone": "UTC",
        "local_date": "1970-01-01",
        "default_currency": "SGD",
    }
    fields = {**ORIGINAL, "transaction_date": context["local_date"], "merchant": merchant}
    projection = {
        "interaction_kind": "initial",
        "source_text": text,
        "reply_text": "",
        "current_values": ORIGINAL,
        "interpretation_context": context,
        "selection": {"kind": "text"},
    }
    raw = wire(
        text,
        fields,
        evidence={
            "transaction_date": [
                cite(canonical_text(context), context["local_date"], "source_context")
            ]
        },
    )
    result = validate_response_v2(raw, projection, ORIGINAL)
    assert result["state"] == "VALID"
    assert result["payload"]["merchant"] == merchant


_V2_RULE_CONTEXT = {
    "anchor_timestamp": 1791504000,
    "timezone": "UTC",
    "local_date": "2026-10-09",
    "default_currency": "SGD",
}
_V2_RULE_SOURCE = "Lunch SGD 12.50 at Example Cafe 2026-10-08"


def _v2_rule_projection(
    kind,
    text,
    *,
    source_text=None,
    unresolved_fields=(),
    current_values=None,
    interpretation_context=None,
    selection=None,
    draft_values=None,
):
    source = text if kind == "initial" else (source_text or _V2_RULE_SOURCE)
    reply = "" if kind == "initial" else text
    projection = {
        "interaction_kind": kind,
        "source_text": source,
        "reply_text": reply,
        "selection": {"kind": "text"} if selection is None else dict(selection),
        "current_values": dict(ORIGINAL if current_values is None else current_values),
        "interpretation_context": dict(
            _V2_RULE_CONTEXT if interpretation_context is None else interpretation_context
        ),
    }
    if unresolved_fields:
        projection["unresolved"] = [{"fields": list(unresolved_fields)}]
    if draft_values is not None:
        projection["draft_values"] = dict(draft_values)
    return projection


def _v2_rule_validate(
    kind,
    text,
    fields,
    *,
    ref=None,
    date_expression=None,
    date_quote=None,
    unresolved_fields=(),
    source_text=None,
    evidence=None,
    intent="personal_expense",
    outcome="proposal",
    coverage_label=None,
    current_values=None,
    interpretation_context=None,
    selection=None,
    receipt_money=None,
    draft_values=None,
    base=None,
):
    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

    ref = ref or ("source" if kind == "initial" else "reply")
    evidence = dict(evidence or {})
    if date_quote is not None:
        evidence["transaction_date"] = [cite(text, date_quote, ref)]
    body = json.loads(
        wire(
            text,
            fields,
            ref=ref,
            date_expression=date_expression,
            unresolved=[
                {
                    "question_id": "pending_" + field,
                    "fields": [field],
                    "reason": "ambiguous",
                    "candidates": [],
                    "evidence": [cite(text, ref=ref)],
                }
                for field in ()
            ],
            evidence=evidence or None,
        )
    )
    body["intent"] = intent
    body["outcome"] = outcome
    if coverage_label is not None:
        body["coverage"][0]["label"] = coverage_label
        if coverage_label in {"non_instruction", "confirmation_request"}:
            body["coverage"][0]["operation_ids"] = []
            body["coverage"][0]["question_ids"] = []
    # A V2 followup answers a question already sealed by the preceding Core
    # interaction; its response itself contains no duplicate question.
    projection = _v2_rule_projection(
        kind,
        text,
        source_text=source_text,
        unresolved_fields=unresolved_fields,
        current_values=current_values,
        interpretation_context=interpretation_context,
        selection=selection,
        draft_values=draft_values,
    )
    return validate_response_v2(
        json.dumps(body, ensure_ascii=False),
        projection,
        ORIGINAL if base is None else base,
        receipt_money=receipt_money,
    )


def test_v2_rule_red_seed_date_omission_and_both_alternative_arms():
    cue = "后天"
    expression = {"kind": "day_offset", "days": 2}
    text = "Lunch SGD 12.50 at Example Cafe " + cue
    control = _v2_rule_validate(
        "initial",
        text,
        ORIGINAL,
        date_expression=expression,
        date_quote=cue,
    )
    assert control["state"] == "VALID", control

    omitted = {field: value for field, value in ORIGINAL.items() if field != "transaction_date"}
    missing = _v2_rule_validate("initial", text, omitted)
    assert missing["state"] == "NEEDS_CLARIFICATION", missing
    assert "transaction_date" in {
        field for question in missing["unresolved"] for field in question["fields"]
    }, missing

    for selected, selected_expression in (
        ("yesterday", {"kind": "day_offset", "days": -1}),
        ("后天", expression),
    ):
        choice = "Lunch SGD 12.50 at Example Cafe yesterday or 后天"
        fields = {**ORIGINAL, "transaction_date": "2026-10-08"}
        result = _v2_rule_validate(
            "initial",
            choice,
            fields,
            date_expression=selected_expression,
            date_quote=selected,
        )
        assert result["state"] == "NEEDS_CLARIFICATION", result
        assert "ambiguous_date_alternative" in result["hard_blockers"], result
        assert "transaction_date" in {
            field for question in result["unresolved"] for field in question["fields"]
        }, result


def test_v2_rule_red_seed_restored_deny_token_overrides_model_intent():
    text = _V2_RULE_SOURCE
    control = _v2_rule_validate("initial", text, ORIGINAL)
    assert control["state"] == "VALID", control

    unsupported = _v2_rule_validate("initial", text + " reimbursement", ORIGINAL)
    assert unsupported["state"] == "RESPONSE_REJECTED", unsupported
    assert unsupported["reason"] == "unsupported_intent", unsupported


@pytest.mark.parametrize("field,value", [("description", "Dinner"), ("category", "travel")])
def test_v2_rule_red_seed_fresh_clear_answers_pending_optional_field(field, value):
    value_reply = f"Set {field} to {value}"
    value_control = _v2_rule_validate(
        "clarification_followup",
        value_reply,
        {field: value},
        unresolved_fields=(field,),
    )
    assert value_control["state"] == "VALID", value_control

    clear_reply = f"Clear {field}"
    clear = _v2_rule_validate(
        "clarification_followup",
        clear_reply,
        {field: None},
        unresolved_fields=(field,),
    )
    assert clear["state"] == "VALID", clear


def _v2_rule_contract_date_vectors():
    """Independently enumerate the fixed finite date contract, not production tables."""
    from datetime import date, timedelta

    anchor = date(2026, 10, 9)
    vectors = []

    def add(cue, expression, expected):
        vectors.append((cue, expression, expected.isoformat()))

    for days in range(-366, 367):
        if days < 0:
            cues = [f"{abs(days)}天前", f"{abs(days)} days ago"]
        elif days > 0:
            cues = [f"{days}天后", f"in {days} days"]
        else:
            cues = ["0天后", "in 0 days"]
        if days in {-366, -13, -3, -1, 0, 1, 3, 13, 366}:
            cues.append(f"{abs(days)} day ago" if days < 0 else f"in {days} day")
        expression = {"kind": "day_offset", "days": days}
        for cue in cues:
            add(cue, expression, anchor + timedelta(days=days))

    named_offsets = {
        -2: ("前天", "the day before yesterday"),
        -1: ("昨天", "昨日", "yesterday"),
        0: ("今天", "今日", "today"),
        1: ("明天", "tomorrow"),
        2: ("后天", "the day after tomorrow"),
    }
    for days, cues in named_offsets.items():
        for cue in cues:
            add(cue, {"kind": "day_offset", "days": days}, anchor + timedelta(days=days))

    import calendar

    def ordinal(day):
        return "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    for month in range(1, 13):
        month_name = calendar.month_name[month]
        lower = month_name.lower()
        expression = {"kind": "absolute_date", "year": 2026, "month": month, "day": 10}
        expected = date(2026, month, 10)
        for cue in (
            f"2026-{month:02d}-10",
            f"2026/{month}/10",
            f"2026.{month}.10",
            f"2026年{month}月10日",
            f"2026年{month}月10号",
            f"2026年{month}月10",
            f"{month_name} 10, 2026",
            f"{lower} 10 2026",
            f"10 {lower} 2026",
            f"{lower} 10th, 2026",
            f"10th {lower}, 2026",
        ):
            add(cue, expression, expected)
    for day in (1, 2, 3, 4, 21, 22, 23, 31):
        expression = {"kind": "absolute_date", "year": 2026, "month": 10, "day": day}
        expected = date(2026, 10, day)
        suffix = ordinal(day)
        for cue in (f"October {day}{suffix}, 2026", f"{day}{suffix} October 2026"):
            add(cue, expression, expected)

    weekdays = {
        1: ("monday", "一"),
        2: ("tuesday", "二"),
        3: ("wednesday", "三"),
        4: ("thursday", "四"),
        5: ("friday", "五"),
        6: ("saturday", "六"),
        7: ("sunday", "日", "天"),
    }
    for weekday, forms in weekdays.items():
        english = forms[0]
        chinese_parts = forms[1:]
        for week_offset, english_prefix, chinese_prefixes in (
            (-1, ("last", "previous"), ("上",)),
            (0, ("this",), ("本", "这")),
            (1, ("next",), ("下",)),
        ):
            expression = {
                "kind": "calendar_week_weekday",
                "week_offset": week_offset,
                "iso_weekday": weekday,
            }
            expected = anchor + timedelta(
                days=1 - anchor.isoweekday() + 7 * week_offset + weekday - 1
            )
            for prefix in english_prefix:
                for tail in (
                    f"week on {english}",
                    f"week {english}",
                    f"week, on {english}",
                    f"week, {english}",
                ):
                    add(f"{prefix} {tail}", expression, expected)
            for prefix in chinese_prefixes:
                for particle in ("周", "星期"):
                    for part in chinese_parts:
                        add(prefix + particle + part, expression, expected)

        for direction, english_prefix, chinese_prefixes in (
            ("previous", ("previous", "last"), ("上一个", "上个", "上一")),
            ("next", ("next",), ("下一个", "下个", "下一")),
        ):
            expression = {
                "kind": "previous_or_next_weekday",
                "direction": direction,
                "iso_weekday": weekday,
            }
            delta = (weekday - anchor.isoweekday()) % 7
            if direction == "previous":
                delta = -((anchor.isoweekday() - weekday) % 7 or 7)
            else:
                delta = delta or 7
            expected = anchor + timedelta(days=delta)
            for prefix in english_prefix:
                add(f"{prefix} {english}", expression, expected)
            for prefix in chinese_prefixes:
                for particle in ("周", "星期"):
                    for part in chinese_parts:
                        add(prefix + particle + part, expression, expected)

    assert len(vectors) == 1928, len(vectors)
    assert len({cue.casefold() for cue, _, _ in vectors}) == 1928
    return vectors


def _v2_rule_legacy_date_vectors():
    """The original 126-cue regression set retained by the approved contract."""
    independently_expected = {
        cue: (expression, expected)
        for cue, expression, expected in _v2_rule_contract_date_vectors()
    }
    # This legacy spelling belongs to the frozen 126-cue diagnostic set, not
    # the separately enumerated 1,928-vector contract.
    independently_expected["10 October 2026"] = (
        {"kind": "absolute_date", "year": 2026, "month": 10, "day": 10},
        "2026-10-10",
    )
    vectors = []

    def add(cue, expression):
        expected_expression, expected = independently_expected[cue]
        assert expected_expression == expression, cue
        vectors.append((cue, expression, expected))

    for days, cues in {
        -2: ("前天", "the day before yesterday"),
        -1: ("昨天", "昨日", "yesterday"),
        0: ("今天", "今日", "today"),
        1: ("明天", "tomorrow"),
        2: ("后天", "the day after tomorrow"),
    }.items():
        for cue in cues:
            add(cue, {"kind": "day_offset", "days": days})
    for cue, days in (("3天前", -3), ("3 days ago", -3), ("3天后", 3), ("in 3 days", 3)):
        add(cue, {"kind": "day_offset", "days": days})
    for cue in (
        "2026-10-10",
        "2026/10/10",
        "2026年10月10日",
        "October 10, 2026",
        "10 October 2026",
    ):
        add(cue, {"kind": "absolute_date", "year": 2026, "month": 10, "day": 10})
    weekday_names = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    chinese_days = ("一", "二", "三", "四", "五", "六", "日")
    for weekday, (english, chinese) in enumerate(zip(weekday_names, chinese_days), 1):
        for offset, english_prefix, chinese_prefix in (
            (-1, "last", "上"),
            (0, "this", "本"),
            (1, "next", "下"),
        ):
            expression = {
                "kind": "calendar_week_weekday",
                "week_offset": offset,
                "iso_weekday": weekday,
            }
            for cue in (
                f"{english_prefix} week on {english}",
                chinese_prefix + "周" + chinese,
                chinese_prefix + "星期" + chinese,
            ):
                add(cue, expression)
        for direction, english_prefix, chinese_prefix in (
            ("previous", "previous", "上一个"),
            ("next", "next", "下一个"),
        ):
            expression = {
                "kind": "previous_or_next_weekday",
                "direction": direction,
                "iso_weekday": weekday,
            }
            for cue in (
                f"{english_prefix} {english}",
                chinese_prefix + "周" + chinese,
                chinese_prefix + "星期" + chinese,
            ):
                add(cue, expression)
    assert len(vectors) == 126, len(vectors)
    return vectors


def test_v2_rule_contract_all_1928_date_vectors_match_python_and_exact_evidence_spans():
    from finance_core.parser_proposals.independent_ai_v2 import (
        date_evidence_spans,
        date_evidence_supported,
    )

    context = dict(_V2_RULE_CONTEXT)
    mismatches = []
    for cue, expression, expected in _v2_rule_contract_date_vectors():
        actual = compute_date(expression, context)
        spans = date_evidence_spans(expression, cue)
        if (
            actual != expected
            or spans != [(0, len(cue))]
            or not date_evidence_supported(expression, cue, expected)
        ):
            mismatches.append((cue, expression, expected, actual, spans))
    assert not mismatches, mismatches[:20]


def test_v2_rule_contract_every_date_alias_is_valid_on_all_three_paths():
    import re

    sample_numeric_offsets = {-366, -13, -3, -2, -1, 0, 1, 2, 3, 13, 366}
    numeric_cue = re.compile(r"^(?:\d+天[前后]|(?:in )?\d+ days?(?: ago)?)$", re.I)
    vectors = [
        item
        for item in _v2_rule_contract_date_vectors()
        if item[1]["kind"] != "day_offset"
        or item[1]["days"] in sample_numeric_offsets
        or not numeric_cue.fullmatch(item[0])
    ]
    valid_controls = 0
    failures = []
    for cue, expression, expected in vectors:
        for kind in ("initial", "card_edit", "clarification_followup"):
            if kind == "initial":
                text = f"Lunch SGD 12.50 at Example Cafe {cue}"
                fields = {**ORIGINAL, "transaction_date": expected}
                source_text = None
                unresolved = ()
            else:
                text = f"Change date to {cue}"
                fields = {"transaction_date": expected}
                source_text = _V2_RULE_SOURCE
                unresolved = ("transaction_date",) if kind == "clarification_followup" else ()
            result = _v2_rule_validate(
                kind,
                text,
                fields,
                date_expression=expression,
                date_quote=cue,
                source_text=source_text,
                unresolved_fields=unresolved,
            )
            valid_controls += 1
            if (
                result["state"] != "VALID"
                or result.get("payload", {}).get("transaction_date") != expected
            ):
                failures.append((kind, cue, expected, result))
    assert not failures, failures[:20]
    assert valid_controls == len(vectors) * 3


def test_v2_rule_contract_retains_all_1008_legacy_date_diagnostics():
    omissions = []
    alternatives_own_arm = []
    alternatives_other_arm = []
    omitted_reply = []
    for cue, expression, expected in _v2_rule_legacy_date_vectors():
        initial = f"Lunch SGD 12.50 at Example Cafe {cue}"
        full = {**ORIGINAL, "transaction_date": expected}
        correct = _v2_rule_validate(
            "initial",
            initial,
            full,
            date_expression=expression,
            date_quote=cue,
        )
        assert correct["state"] == "VALID", (cue, correct)

        without_date = {
            field: value for field, value in ORIGINAL.items() if field != "transaction_date"
        }
        omitted = _v2_rule_validate("initial", initial, without_date)
        omissions.append((cue, omitted))

        alternate = "tomorrow" if expression.get("days") == -1 else "yesterday"
        own_text = f"Lunch SGD 12.50 at Example Cafe {cue} or {alternate}"
        own_expression = expression
        own_expected = expected
        own = _v2_rule_validate(
            "initial",
            own_text,
            {**ORIGINAL, "transaction_date": own_expected},
            date_expression=own_expression,
            date_quote=cue,
        )
        alternatives_own_arm.append((cue, own))

        other_expression = {"kind": "day_offset", "days": 1 if alternate == "tomorrow" else -1}
        other_expected = "2026-10-10" if alternate == "tomorrow" else "2026-10-08"
        other_text = f"{alternate} or {cue}"
        for kind in ("initial", "card_edit", "clarification_followup"):
            text = (
                f"Lunch SGD 12.50 at Example Cafe {other_text}"
                if kind == "initial"
                else f"Change date to {other_text}"
            )
            result = _v2_rule_validate(
                kind,
                text,
                {**ORIGINAL, "transaction_date": other_expected}
                if kind == "initial"
                else {"transaction_date": other_expected},
                date_expression=other_expression,
                date_quote=alternate,
                unresolved_fields=("transaction_date",) if kind == "clarification_followup" else (),
            )
            alternatives_other_arm.append((kind, cue, result))

        for kind in ("card_edit", "clarification_followup"):
            reply = f"Change date to {cue}"
            result = _v2_rule_validate(
                kind,
                reply,
                {},
                coverage_label="non_instruction",
                unresolved_fields=("transaction_date",) if kind == "clarification_followup" else (),
            )
            omitted_reply.append((kind, cue, result))

    assert len(omissions) == 126
    assert len(alternatives_own_arm) == 126
    assert len(alternatives_other_arm) == 378
    assert len(omitted_reply) == 252
    bad_omissions = [
        (cue, result)
        for cue, result in omissions
        if result["state"] != "NEEDS_CLARIFICATION"
        or "transaction_date"
        not in {field for q in result.get("unresolved", []) for field in q["fields"]}
    ]
    bad_own = [
        (cue, result)
        for cue, result in alternatives_own_arm
        if result["state"] != "NEEDS_CLARIFICATION"
        or "ambiguous_date_alternative" not in result.get("hard_blockers", [])
    ]
    bad_other = [
        (kind, cue, result)
        for kind, cue, result in alternatives_other_arm
        if result["state"] != "NEEDS_CLARIFICATION"
        or "ambiguous_date_alternative" not in result.get("hard_blockers", [])
    ]
    bad_reply = [
        (kind, cue, result)
        for kind, cue, result in omitted_reply
        if result["state"] != "NEEDS_CLARIFICATION"
        or "transaction_date"
        not in {field for q in result.get("unresolved", []) for field in q["fields"]}
    ]
    assert not (bad_omissions or bad_own or bad_other or bad_reply), {
        "omissions": bad_omissions[:5],
        "own_arm": bad_own[:5],
        "other_arm": bad_other[:5],
        "reply_omission": bad_reply[:5],
    }


def test_v2_rule_contract_date_token_boundaries_and_money_masks_are_exact():
    from finance_core.parser_proposals.independent_ai_v2 import (
        date_evidence_spans,
        date_evidence_supported,
    )

    boundary_pairs = (
        ({"kind": "day_offset", "days": -3}, "13天前"),
        ({"kind": "day_offset", "days": -3}, "113天前"),
        ({"kind": "day_offset", "days": -3}, "3.5天前"),
        ({"kind": "day_offset", "days": -3}, "-3天前"),
        ({"kind": "day_offset", "days": -3}, "+3天前"),
        ({"kind": "day_offset", "days": 3}, "13天后"),
        ({"kind": "day_offset", "days": 3}, "in +3 days"),
        ({"kind": "day_offset", "days": 3}, "in 3.5 days"),
        ({"kind": "day_offset", "days": -1}, "the day before yesterday"),
        ({"kind": "day_offset", "days": 1}, "the day after tomorrow"),
    )
    for expression, quote in boundary_pairs:
        assert date_evidence_spans(expression, quote) == [], (expression, quote)
        assert not date_evidence_supported(expression, quote, "2026-10-06"), (expression, quote)

    invalid_money_cases = (
        ("Lunch SGD -15.00 at Cafe 3天前", "unsupported_source_money_notation"),
        ("Lunch XYZ 15.00 at Cafe 3天前", "unsupported_currency_notation"),
        ("Lunch SGD 12.50 or SGD 15.00 at Cafe 3天前", "competing_amounts"),
        ("Lunch SGD 12.50 and USD 12.50 at Cafe 3天前", "competing_currencies"),
    )
    expression = {"kind": "day_offset", "days": -3}
    for text, expected_blocker in invalid_money_cases:
        result = _v2_rule_validate(
            "initial",
            text,
            {**ORIGINAL, "transaction_date": "2026-10-06"},
            date_expression=expression,
            date_quote=text,
        )
        assert result["state"] == "NEEDS_CLARIFICATION", (text, result)
        assert expected_blocker in result.get("hard_blockers", []), (text, result)

    valid_text = "Lunch SGD 12.50 at Cafe 3天前"
    valid = _v2_rule_validate(
        "initial",
        valid_text,
        {**ORIGINAL, "transaction_date": "2026-10-06"},
        date_expression=expression,
        date_quote=valid_text,
    )
    assert valid["state"] == "VALID", valid


def test_v2_rule_contract_signed_decimal_date_requests_keep_date_obligation_for_text_and_ocr():
    from finance_core.parser_proposals.independent_ai_v2 import date_evidence_spans
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    expression = {"kind": "day_offset", "days": 3}
    for cue in ("in +3 days", "in 3.5 days"):
        assert date_evidence_spans(expression, cue) == [], cue
    assert date_evidence_spans(expression, "in 3 days") == [(0, len("in 3 days"))]

    default_context = canonical_text(_V2_RULE_CONTEXT)
    cases = []
    for selection in ({"kind": "text"}, {"kind": "local_ocr"}):
        receipt_money = (
            ("12.50", "SGD", "TOTAL SGD 12.50") if selection["kind"] == "local_ocr" else None
        )
        for cue in ("in +3 days", "in 3.5 days"):
            text = (
                f"Example Cafe\nTOTAL SGD 12.50 {cue}"
                if selection["kind"] == "local_ocr"
                else f"Lunch SGD 12.50 at Example Cafe {cue}"
            )
            omitted_fields = {
                field: value for field, value in ORIGINAL.items() if field != "transaction_date"
            }
            cases.append(
                (
                    selection,
                    receipt_money,
                    cue,
                    "omitted",
                    omitted_fields,
                    {},
                )
            )
            cases.append(
                (
                    selection,
                    receipt_money,
                    cue,
                    "source_context_default",
                    {**ORIGINAL, "transaction_date": "2026-10-09"},
                    {"transaction_date": [cite(default_context, "2026-10-09", "source_context")]},
                )
            )
            cases.append(
                (
                    selection,
                    receipt_money,
                    cue,
                    "wrong_unsigned_operation",
                    {**ORIGINAL, "transaction_date": "2026-10-12"},
                    {"transaction_date": [cite(text, cue)]},
                )
            )

    failures = []
    for selection, receipt_money, cue, case, fields, evidence in cases:
        text = (
            f"Example Cafe\nTOTAL SGD 12.50 {cue}"
            if selection["kind"] == "local_ocr"
            else f"Lunch SGD 12.50 at Example Cafe {cue}"
        )
        result = _v2_rule_validate(
            "initial",
            text,
            fields,
            date_expression=expression if case == "wrong_unsigned_operation" else None,
            date_quote=cue if case == "wrong_unsigned_operation" else None,
            evidence=evidence,
            selection=selection,
            receipt_money=receipt_money,
        )
        unresolved = {
            field for question in result.get("unresolved", []) for field in question["fields"]
        }
        if result["state"] != "NEEDS_CLARIFICATION" or "transaction_date" not in unresolved:
            failures.append((selection["kind"], cue, case, result))
    assert len(cases) == 12
    assert not failures, failures

    # A normal unsigned integer remains a supported, independently computed date.
    for selection in ({"kind": "text"}, {"kind": "local_ocr"}):
        receipt_money = (
            ("12.50", "SGD", "TOTAL SGD 12.50") if selection["kind"] == "local_ocr" else None
        )
        text = (
            "Example Cafe\nTOTAL SGD 12.50 in 3 days"
            if selection["kind"] == "local_ocr"
            else "Lunch SGD 12.50 at Example Cafe in 3 days"
        )
        result = _v2_rule_validate(
            "initial",
            text,
            {**ORIGINAL, "transaction_date": "2026-10-12"},
            date_expression=expression,
            date_quote="in 3 days",
            selection=selection,
            receipt_money=receipt_money,
        )
        assert result["state"] == "VALID", (selection, result)


def test_v2_rule_contract_initial_date_request_labels_and_iso_exception():
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    default_date = {"kind": "absolute_date", "year": 2026, "month": 10, "day": 9}
    source_context = canonical_text(_V2_RULE_CONTEXT)
    bad = []
    for label in ("date unknown", "日期不确定"):
        text = f"Lunch SGD 12.50 at Example Cafe {label}"
        omitted = _v2_rule_validate(
            "initial",
            text,
            {field: value for field, value in ORIGINAL.items() if field != "transaction_date"},
        )
        defaulted = _v2_rule_validate(
            "initial",
            text,
            {**ORIGINAL, "transaction_date": "2026-10-09"},
            date_expression=default_date,
            evidence={"transaction_date": [cite(source_context, "2026-10-09", "source_context")]},
        )
        for result in (omitted, defaulted):
            if result["state"] != "NEEDS_CLARIFICATION" or "transaction_date" not in {
                field for question in result.get("unresolved", []) for field in question["fields"]
            }:
                bad.append((label, result))
    assert not bad, bad

    no_date_text = "Lunch SGD 12.50 at Example Cafe"
    ordinary_default = _v2_rule_validate(
        "initial",
        no_date_text,
        ORIGINAL,
        date_expression=default_date,
        evidence={"transaction_date": [cite(source_context, "2026-10-09", "source_context")]},
    )
    assert ordinary_default["state"] == "VALID", ordinary_default

    unchanged_iso = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    unchanged = _v2_rule_validate(
        "initial",
        unchanged_iso,
        {field: value for field, value in ORIGINAL.items() if field != "transaction_date"},
    )
    assert unchanged["state"] == "VALID", unchanged

    literal = _v2_rule_validate(
        "card_edit",
        "Change description to date unknown",
        {"description": "date unknown"},
    )
    assert literal["state"] == "VALID", literal


def _v2_rule_clean_control(kind):
    if kind == "initial":
        return _V2_RULE_SOURCE, dict(ORIGINAL), None, ()
    text = "Change description to Dinner"
    unresolved = ("description",) if kind == "clarification_followup" else ()
    return text, {"description": "Dinner"}, _V2_RULE_SOURCE, unresolved


def _v2_rule_prepare_text_source(connection, *, suffix, raw_text):
    """Build a signed parser source without invoking the legacy posting gate."""
    import hashlib

    from test_application_posting_recovery_v1 import SOURCE_KEY, _digest, _persist

    from finance_core.application import admission
    from finance_core.intake.raw_text_repository import (
        create_raw_intake_record,
        save_parser_proposal,
    )
    from finance_core.parsers.text_expense_parser import parse_text_expense

    connection.execute(
        "CREATE TABLE IF NOT EXISTS synthetic_sources "
        "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
    )
    intake = create_raw_intake_record(
        connection,
        raw_text,
        source_type="manual_entry",
        source_channel="manual",
        public_id="synthetic-rule-intake-" + suffix,
        received_at="2026-10-08T00:00:00Z",
    )
    parsed = parse_text_expense(
        raw_text,
        raw_input_reference=intake["public_id"],
        source_type="manual_entry",
    )
    parsed.update(
        {
            "amount": "12.50",
            "currency": "SGD",
            "transaction_date": "2026-10-08",
            "merchant": "Example Cafe",
            "description": "Lunch",
            "category": "food",
            "intent": "personal_expense_log",
            "transaction_type": "personal_expense",
        }
    )
    proposal = save_parser_proposal(connection, intake["id"], parsed)
    source = {
        "schema": admission.SOURCE_SCHEMA,
        "namespace": BINDING.source_namespace,
        "key_id": BINDING.source_key_id,
        "instance_id": BINDING.instance_id,
        "submission_client_id": BINDING.submission_client_id,
        "evidence_id": "synthetic-rule-source-" + suffix,
        "intake_public_id": intake["public_id"],
        "source_event_id": "synthetic-rule-event-" + suffix,
        "source_content_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "source_occurred_at": NOW - 2,
        "received_at": NOW - 1,
    }
    source["evidence_digest"] = _digest(source)
    _persist(connection, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
    connection.commit()
    return intake, proposal


def _v2_rule_date_from_request(request, day_offset):
    """Independently resolve an expected date from the signed service request anchor."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    projection = json.loads(request.request_json)["input"]
    context = projection["interpretation_context"]
    expected = datetime.fromtimestamp(
        context["anchor_timestamp"], ZoneInfo(context["timezone"])
    ).date()
    assert expected.isoformat() == context["local_date"]
    return (expected + timedelta(days=day_offset)).isoformat()


def test_v2_rule_contract_all_registered_unsupported_tokens_and_patterns_fail_closed():
    import re
    from importlib.resources import files

    from finance_core.parser_proposals.independent_ai_validation import assets

    registry = assets("v1")
    assert "text_readiness_v1.json" in registry
    policy = json.loads(
        files("finance_core.resources")
        .joinpath("independent_ai/text_readiness_v1.json")
        .read_text()
    )
    assert len(policy["deny_intent_tokens"]) == 25
    assert policy["deny_intent_patterns"]
    v2_additions = (
        "退费",
        "退钱",
        "退了",
        "返款",
        "转帐",
        "shared-expense",
        "shared expense",
    )
    policy_examples = ("refund", "refunds", "refunded", "refunding")
    for example in policy_examples:
        assert any(re.search(pattern, example, re.I) for pattern in policy["deny_intent_patterns"])
    stimuli = list(dict.fromkeys((*policy["deny_intent_tokens"], *policy_examples, *v2_additions)))
    assert len(stimuli) == 35

    for kind in ("initial", "card_edit", "clarification_followup"):
        text, fields, source, unresolved = _v2_rule_clean_control(kind)
        control = _v2_rule_validate(
            kind, text, fields, source_text=source, unresolved_fields=unresolved
        )
        assert control["state"] == "VALID", (kind, control)

    failures = []
    for token in stimuli:
        for kind in ("initial", "card_edit", "clarification_followup"):
            control_text, control_fields, source, unresolved = _v2_rule_clean_control(kind)
            if kind == "initial":
                text = control_text + " " + token
                fields = dict(control_fields)
                source_text = None
            else:
                text = f'Change description to "{token}"'
                fields = {"description": token}
                source_text = source
                unresolved = ("description",) if kind == "clarification_followup" else ()
            control = _v2_rule_validate(
                kind,
                control_text,
                control_fields,
                source_text=source,
                unresolved_fields=unresolved,
            )
            assert control["state"] == "VALID", (kind, token, control)
            result = _v2_rule_validate(
                kind,
                text,
                fields,
                source_text=source_text,
                unresolved_fields=unresolved,
                intent="personal_expense",
            )
            if (
                result.get("state") != "RESPONSE_REJECTED"
                or result.get("reason") != "unsupported_intent"
            ):
                failures.append((kind, token, result))

            if kind != "initial":
                source_only = _v2_rule_validate(
                    kind,
                    "Change description to Dinner",
                    {"description": "Dinner"},
                    source_text=_V2_RULE_SOURCE + " " + token,
                    unresolved_fields=("description",) if kind == "clarification_followup" else (),
                    intent="personal_expense",
                )
                if (
                    source_only.get("state") != "RESPONSE_REJECTED"
                    or source_only.get("reason") != "unsupported_intent"
                ):
                    failures.append((kind, token, "source_only", source_only))
    assert not failures, failures[:20]


@pytest.mark.parametrize("kind", ["initial", "card_edit", "clarification_followup"])
@pytest.mark.parametrize("token", ["refund", "shared expense", "退费"])
def test_v2_rule_contract_non_instruction_and_model_label_do_not_hide_denied_source_or_reply(
    kind, token
):
    ordinary, fields, source, unresolved = _v2_rule_clean_control(kind)
    control = _v2_rule_validate(
        kind,
        ordinary,
        fields,
        source_text=source,
        unresolved_fields=unresolved,
    )
    assert control["state"] == "VALID", (kind, control)
    if kind == "initial":
        text = ordinary + " " + token
        source_text = None
    else:
        text = "Keep current values"
        source_text = _V2_RULE_SOURCE + " " + token
    result = _v2_rule_validate(
        kind,
        text,
        {},
        source_text=source_text,
        intent="personal_expense",
        coverage_label="non_instruction",
    )
    assert result["state"] == "RESPONSE_REJECTED", result
    assert result["reason"] == "unsupported_intent", result


def test_v2_rule_contract_ascii_substrings_are_not_standalone_deny_tokens():
    for merchant in ("Payerette", "Splinter Cafe"):
        text = f"Lunch SGD 12.50 at {merchant} 2026-10-08"
        result = _v2_rule_validate(
            "initial",
            text,
            {**ORIGINAL, "merchant": merchant},
            intent="personal_expense",
        )
        assert result["state"] == "VALID", (merchant, result)


@pytest.mark.parametrize("fault", ["tampered", "missing"])
def test_v2_rule_contract_v1_asset_failure_rejects_response(monkeypatch, fault):
    import finance_core.parser_proposals.independent_ai_validation as validation

    class Entry:
        def __init__(self, name):
            self.name = name

        def read_text(self):
            if fault == "missing":
                raise FileNotFoundError(self.name)
            return json.dumps({"text_readiness_v1.json": "0" * 64})

        def read_bytes(self):
            return b"tampered resource"

    class Directory:
        def joinpath(self, name):
            return Entry(name)

    class Package:
        def joinpath(self, name):
            assert name == "independent_ai"
            return Directory()

    monkeypatch.setattr(validation, "files", lambda _package: Package())
    result = _v2_rule_validate("initial", _V2_RULE_SOURCE, ORIGINAL)
    assert result["state"] == "RESPONSE_REJECTED", result
    assert result["reason"] == "invalid_response", result


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize(
    "field",
    ["amount", "currency", "transaction_date", "merchant", "description", "category"],
)
def test_v2_rule_contract_all_six_explicit_null_fields_obey_requiredness(kind, field):
    text = f"Clear {field}"
    if field == "transaction_date":
        from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

        body = json.loads(wire(text, {}, ref="reply"))
        body["coverage"][0].update(label="resolved_instruction", operation_ids=["op-null-date"])
        body["operations"] = [
            {
                "operation_id": "op-null-date",
                "op": "set",
                "field": "transaction_date",
                "date_expression": None,
                "evidence": [cite(text, ref="reply")],
            }
        ]
        projection = _v2_rule_projection(
            kind,
            text,
            unresolved_fields=(field,) if kind == "clarification_followup" else (),
        )
        result = validate_response_v2(json.dumps(body, ensure_ascii=False), projection, ORIGINAL)
    else:
        result = _v2_rule_validate(
            kind,
            text,
            {field: None},
            unresolved_fields=(field,) if kind == "clarification_followup" else (),
        )
    if field in {"description", "category"}:
        assert result["state"] == "VALID", (kind, field, result)
        assert result["payload"][field] is None, (kind, field, result)
    else:
        assert result["state"] != "VALID", (kind, field, result)
        if result["state"] == "NEEDS_CLARIFICATION":
            assert field in {
                target for question in result["unresolved"] for target in question["fields"]
            }, (kind, field, result)


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize("field", ["description", "category"])
def test_v2_rule_contract_each_clear_lexeme_and_field_alias_proves_fresh_optional_null(kind, field):
    clear_tokens = ("remove", "clear", "delete", "删除", "清空", "移除")
    field_cues = {
        "description": ("description", "描述", "备注"),
        "category": ("category", "分类", "类别"),
    }
    failures = []
    for token in clear_tokens:
        for field_cue in field_cues[field]:
            text = f"{token} {field_cue}"
            result = _v2_rule_validate(
                kind,
                text,
                {field: None},
                unresolved_fields=(field,) if kind == "clarification_followup" else (),
            )
            if (
                result["state"] != "VALID"
                or result.get("payload", {}).get(field, "missing") is not None
            ):
                failures.append((token, field_cue, result))
    assert not failures, failures


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize(
    "cleared_field,wrong_field", [("description", "category"), ("category", "description")]
)
def test_v2_rule_contract_clear_must_name_the_same_null_field(kind, cleared_field, wrong_field):
    text = f"Clear {wrong_field}"
    result = _v2_rule_validate(
        kind,
        text,
        {cleared_field: None},
        unresolved_fields=(cleared_field,) if kind == "clarification_followup" else (),
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    assert cleared_field in {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }, result


def test_v2_rule_contract_missing_old_and_wrong_evidence_do_not_discharge_optional_question():
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    absent = _v2_rule_validate(
        "clarification_followup",
        "Okay",
        {},
        coverage_label="non_instruction",
        unresolved_fields=("description",),
    )
    assert absent["state"] == "NEEDS_CLARIFICATION", absent
    assert "description" in {
        field for question in absent.get("unresolved", []) for field in question["fields"]
    }, absent

    context_text = canonical_text(ORIGINAL)
    retained = _v2_rule_validate(
        "clarification_followup",
        "Keep description",
        {"description": ORIGINAL["description"]},
        unresolved_fields=("description",),
        evidence={
            "description": [
                cite(context_text, ORIGINAL["description"], "context", "retained_value")
            ]
        },
    )
    assert retained["state"] == "NEEDS_CLARIFICATION", retained
    assert "description" in {
        field for question in retained.get("unresolved", []) for field in question["fields"]
    }, retained

    source_text = "Clear description"
    source_only = _v2_rule_validate(
        "clarification_followup",
        "Clear description",
        {"description": None},
        source_text=source_text,
        unresolved_fields=("description",),
        evidence={"description": [cite(source_text, ref="source")]},
    )
    assert source_only["state"] == "NEEDS_CLARIFICATION", source_only
    assert "description" in {
        field for question in source_only.get("unresolved", []) for field in question["fields"]
    }, source_only

    previous_text = "Lunch; Clear description"
    previous = _v2_rule_validate(
        "clarification_followup",
        previous_text,
        {"description": ORIGINAL["description"]},
        unresolved_fields=("description",),
        evidence={
            "description": [
                cite(context_text, ORIGINAL["description"], "context", "retained_value"),
                cite(previous_text, ref="reply", role="previous_value"),
            ]
        },
    )
    assert previous["state"] == "NEEDS_CLARIFICATION", previous
    assert "description" in {
        field for question in previous.get("unresolved", []) for field in question["fields"]
    }, previous

    no_clear = _v2_rule_validate(
        "clarification_followup",
        "Set description to null",
        {"description": None},
        unresolved_fields=("description",),
    )
    assert no_clear["state"] == "NEEDS_CLARIFICATION", no_clear
    assert "description" in {
        field for question in no_clear.get("unresolved", []) for field in question["fields"]
    }, no_clear

    malformed = _v2_rule_validate(
        "clarification_followup",
        "Clear description",
        {"description": None},
        unresolved_fields=("description",),
        evidence={"description": [{**cite("Clear description", ref="reply"), "end": 99}]},
    )
    assert malformed["state"] == "RESPONSE_REJECTED", malformed


def test_v2_rule_contract_unasked_optional_values_preserve_and_initial_absence_stays_allowed():
    text = "Change amount to SGD 15.00"
    result = _v2_rule_validate("card_edit", text, {"amount": "15.00"})
    assert result["state"] == "VALID", result
    assert result["payload"]["description"] == ORIGINAL["description"]
    assert result["payload"]["category"] == ORIGINAL["category"]

    initial_text = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
    required = {
        field: ORIGINAL[field] for field in ("amount", "currency", "transaction_date", "merchant")
    }
    initial = _v2_rule_validate(
        "initial",
        initial_text,
        required,
        base=required,
    )
    assert initial["state"] == "VALID", initial


@pytest.mark.parametrize("required_field", ["amount", "currency", "transaction_date"])
def test_v2_rule_contract_optional_clear_keeps_other_required_questions(required_field):
    result = _v2_rule_validate(
        "clarification_followup",
        "Clear description",
        {"description": None},
        unresolved_fields=("description", required_field),
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    remaining = {field for question in result.get("unresolved", []) for field in question["fields"]}
    assert "description" not in remaining, result
    assert required_field in remaining, result


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
def test_v2_rule_contract_two_optional_fields_clear_with_independent_field_evidence(kind):
    text = "Clear category and clear description"
    result = _v2_rule_validate(
        kind,
        text,
        {"category": None, "description": None},
        unresolved_fields=("category", "description") if kind == "clarification_followup" else (),
    )
    assert result["state"] == "VALID", result
    assert result["payload"]["category"] is None
    assert result["payload"]["description"] is None


def test_v2_rule_service_supported_after_tomorrow_shows_and_posts_the_full_card_once(
    migrated_temp_db_connection,
):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    c = migrated_temp_db_connection
    text = "Lunch SGD 12.50 at Example Cafe 后天"
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="rule-supported-date", raw_text=text)
    expression = {"kind": "day_offset", "days": 2}
    ai, provider = service_v2(c, "{}")
    expected = {}
    base_dispatch = provider.dispatch

    def dispatch_with_authenticated_anchor(request):
        expected["date"] = _v2_rule_date_from_request(request, expression["days"])
        fields = {**ORIGINAL, "transaction_date": expected["date"]}
        provider.response = wire(
            text,
            fields,
            date_expression=expression,
            evidence={"transaction_date": [cite(text, "后天")]},
        )
        return base_dispatch(request)

    provider.dispatch = dispatch_with_authenticated_anchor
    prepared = ai.prepare_initial(intake["public_id"], "rule-supported-date-op")
    invoked = ai.invoke(prepared.attempt_id)
    assert invoked.state == "VALID", invoked
    publication = ai.publish(prepared.attempt_id)
    card, final = finish_post(c, ai, publication, "rule-supported-date-confirm")
    assert card.projection["financial_projection"]["transaction_date"] == expected["date"]
    assert tuple(
        c.execute(
            "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
            "FROM transactions"
        ).fetchone()
    ) == (
        12.5,
        "SGD",
        expected["date"],
        "Example Cafe",
        "Lunch",
        "food",
        final.transaction_public_id,
    )
    assert provider.calls == 1


def test_v2_rule_service_ambiguous_date_clarifies_without_a_confirmable_card_or_posting(
    migrated_temp_db_connection,
):
    from test_application_posting_recovery_v1 import _prepare_text_subject

    c = migrated_temp_db_connection
    text = "Lunch SGD 12.50 at Example Cafe yesterday or 后天"
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="rule-ambiguous-date", raw_text=text)
    response = wire(
        text,
        {**ORIGINAL, "transaction_date": "2026-10-08"},
        date_expression={"kind": "day_offset", "days": -1},
        evidence={"transaction_date": [cite(text, "yesterday")]},
    )
    ai, provider = service_v2(c, response)
    prepared = ai.prepare_initial(intake["public_id"], "rule-ambiguous-date-op")
    invoked = ai.invoke(prepared.attempt_id)
    assert invoked.state == "NEEDS_CLARIFICATION", invoked
    question = ai.prepare_clarification(prepared.attempt_id)
    assert question.projection["confirm_available"] is False
    assert "transaction_date" in {
        field for item in question.projection["unresolved"] for field in item["fields"]
    }
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 0
    assert provider.calls == 1


@pytest.mark.parametrize("token", ["reimbursement", "refund", "refunded", "退费", "shared expense"])
def test_v2_rule_service_restored_unsupported_intent_never_posts_as_expense(
    migrated_temp_db_connection, token
):
    c = migrated_temp_db_connection
    text = f"Lunch SGD 12.50 at Example Cafe 2026-10-08 {token}"
    intake, _proposal = _v2_rule_prepare_text_source(
        c,
        suffix="rule-unsupported-" + token.encode("utf-8").hex(),
        raw_text=text,
    )
    response = wire(text, ORIGINAL)
    body = json.loads(response)
    body["intent"] = "personal_expense"
    ai, provider = service_v2(c, json.dumps(body, ensure_ascii=False))
    prepared = ai.prepare_initial(intake["public_id"], "rule-unsupported-op")
    invoked = ai.invoke(prepared.attempt_id)
    assert invoked.state == "RESPONSE_REJECTED", invoked
    assert invoked.reason == "unsupported_intent", invoked
    with pytest.raises(AiInvocationError):
        ai.publish(prepared.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 0
    assert provider.calls == 1


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize("field", ["description", "category"])
def test_v2_rule_service_clear_optional_field_uses_fresh_full_card_and_one_post(
    migrated_temp_db_connection, kind, field
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "rule-clear-" + kind + "-" + field)
    owner = posting(c, ai)
    old_card = owner.prepare(initial.proposal_public_id)

    if kind == "card_edit":
        reply = f"Clear {field}"
        proof = persist_reply(c, old_card, reply, identity="rule-clear-edit-" + field)
        provider.response = wire(reply, {field: None}, ref="reply")
        attempt = ai.prepare_edit(old_card.review_id, proof, "rule-clear-edit-op-" + field)
        expected_provider_calls = 2
    else:
        question_text = f"Change {field}"
        question = {
            "question_id": "clear_" + field,
            "fields": [field],
            "reason": "ambiguous",
            "candidates": [],
            "evidence": [cite(question_text, ref="reply")],
        }
        provider.response = wire(
            question_text,
            {},
            ref="reply",
            unresolved=[question],
            outcome="clarification",
        )
        ask_proof = persist_reply(
            c, old_card, question_text, identity="rule-clear-question-" + field
        )
        ask = ai.prepare_edit(old_card.review_id, ask_proof, "rule-clear-question-op-" + field)
        assert ai.invoke(ask.attempt_id).state == "NEEDS_CLARIFICATION"
        question_card = ai.prepare_clarification(ask.attempt_id)
        assert question_card.projection["confirm_available"] is False
        reply = f"Clear {field}"
        proof = persist_reply(c, question_card, reply, identity="rule-clear-followup-" + field)
        provider.response = wire(reply, {field: None}, ref="reply")
        attempt = ai.prepare_followup(
            question_card.request_id, proof, "rule-clear-followup-op-" + field
        )
        expected_provider_calls = 3

    invoked = ai.invoke(attempt.attempt_id)
    assert invoked.state == "VALID", invoked
    publication = ai.publish(attempt.attempt_id)
    assert publication.proposal_public_id != initial.proposal_public_id
    card, final = finish_post(c, ai, publication, "rule-clear-confirm-" + kind + "-" + field)
    projection = card.projection["financial_projection"]
    assert projection[field] is None
    row = c.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
        "FROM transactions"
    ).fetchone()
    expected = [12.5, "SGD", "2026-10-08", "Example Cafe", "Lunch", "food"]
    expected[4 if field == "description" else 5] = None
    assert tuple(row) == (*expected, final.transaction_public_id), "STOP_FINANCIAL_FACT_MISMATCH"
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert provider.calls == expected_provider_calls


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize("explicit_retained_operation", [False, True])
def test_v2_rule_contract_current_null_without_new_request_is_preserved_as_noop(
    kind, explicit_retained_operation
):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    current = {**ORIGINAL, "description": None, "category": None}
    text = "Change merchant to New Cafe"
    fields = {"merchant": "New Cafe"}
    evidence = {"merchant": [cite(text, "New Cafe", "reply")]}
    if explicit_retained_operation:
        context = canonical_text(current)
        fields.update({"description": None, "category": None})
        evidence.update(
            {
                field: [cite(context, f'"{field}":null', "context", "retained_value")]
                for field in ("description", "category")
            }
        )

    result = _v2_rule_validate(
        kind,
        text,
        fields,
        current_values=current,
        base=current,
        evidence=evidence,
    )
    assert result["state"] == "VALID", result
    assert result["payload"]["merchant"] == "New Cafe"
    assert result["payload"]["description"] is None
    assert result["payload"]["category"] is None


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize("field", ["description", "category"])
@pytest.mark.parametrize("obligation", ["pending", "new_request"])
def test_v2_rule_contract_retained_null_does_not_answer_pending_or_new_field_request(
    kind, field, obligation
):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    current = {**ORIGINAL, field: None}
    context = canonical_text(current)
    if obligation == "pending":
        text = "Change merchant to New Cafe"
        unresolved_fields = (field,)
    else:
        requested_value = "Dinner" if field == "description" else "travel"
        text = f"Change {field} to {requested_value}; change merchant to New Cafe"
        unresolved_fields = ()
    result = _v2_rule_validate(
        kind,
        text,
        {field: None, "merchant": "New Cafe"},
        current_values=current,
        base=current,
        unresolved_fields=unresolved_fields,
        evidence={
            field: [cite(context, f'"{field}":null', "context", "retained_value")],
            "merchant": [cite(text, "New Cafe", "reply")],
        },
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    assert field in {
        target for question in result.get("unresolved", []) for target in question["fields"]
    }, result


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize("field", ["description", "category"])
def test_v2_rule_contract_partial_draft_null_cannot_replace_nonnull_current_value(kind, field):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    text = "Change merchant to New Cafe"
    current = dict(ORIGINAL)
    context = canonical_text(current)
    result = _v2_rule_validate(
        kind,
        text,
        {field: None, "merchant": "New Cafe"},
        current_values=current,
        base=current,
        draft_values={field: None},
        evidence={
            field: [
                cite(
                    context, f'"{field}":{json.dumps(current[field])}', "context", "retained_value"
                )
            ],
            "merchant": [cite(text, "New Cafe", "reply")],
        },
    )
    assert result["state"] == "RESPONSE_REJECTED", result
    assert result["reason"] == "invalid_response", result


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize("field", ["description", "category"])
def test_v2_rule_contract_unrelated_reply_replacement_cannot_clear_current_null(kind, field):
    current = {**ORIGINAL, field: None}
    text = "Change merchant to New Cafe"
    result = _v2_rule_validate(
        kind,
        text,
        {field: None, "merchant": "New Cafe"},
        current_values=current,
        base=current,
        evidence={
            field: [cite(text, "New Cafe", "reply")],
            "merchant": [cite(text, "New Cafe", "reply")],
        },
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    assert field in {
        target for question in result.get("unresolved", []) for target in question["fields"]
    }, result


def test_v2_rule_service_supported_ocr_day_offset_shows_and_posts_once(tmp_path):
    from test_application_posting_recovery_v1 import _prepare_receipt_subject

    c, _, _, _, _, _, intake, _, _, source, decision = _prepare_receipt_subject(
        tmp_path,
        suffix="rule-ocr-supported-date",
        date_text="in 3 days",
        prepare_review=False,
    )
    try:
        provider = DurableProvider(c, response="{}")
        expected = {}

        def dispatch_with_supported_date(request):
            input_projection = json.loads(request.request_json)["input"]
            text = input_projection["source_text"]
            assert "in 3 days" in text, text
            expected["date"] = _v2_rule_date_from_request(request, 3)
            fields = {
                **ORIGINAL,
                "merchant": "EXAMPLE CAFE",
                "description": "Receipt total",
                "transaction_date": expected["date"],
            }
            provider.response = wire(
                text,
                fields,
                date_expression={"kind": "day_offset", "days": 3},
                evidence={"transaction_date": [cite(text, "in 3 days")]},
            )
            return base_dispatch(request)

        base_dispatch = provider.dispatch
        provider.dispatch = dispatch_with_supported_date
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        prepared = ai.prepare_initial(intake, "rule-ocr-supported-date-op")
        invoked = ai.invoke(prepared.attempt_id)
        assert invoked.state == "VALID", invoked
        publication = ai.publish(prepared.attempt_id)
        card, final = finish_post(
            c,
            ai,
            publication,
            "rule-ocr-supported-date-confirm",
            source=source,
            decision=decision,
        )
        projection = card.projection["financial_projection"]
        assert projection["transaction_date"] == expected["date"]
        row = c.execute(
            "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
            "FROM transactions"
        ).fetchone()
        assert tuple(row) == (
            12.5,
            "SGD",
            expected["date"],
            "EXAMPLE CAFE",
            "Receipt total",
            "food",
            final.transaction_public_id,
        ), "STOP_FINANCIAL_FACT_MISMATCH"
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
        assert provider.calls == 1
    finally:
        c.close()


@pytest.mark.parametrize(
    "field,reply,other_field,other_value",
    [
        ("amount", "Use date 2026-10-09", "transaction_date", "2026-10-09"),
        ("currency", "Use date 2026-10-09", "transaction_date", "2026-10-09"),
        ("transaction_date", "Set category to travel", "category", "travel"),
        ("merchant", "Use date 2026-10-09", "transaction_date", "2026-10-09"),
        ("description", "Use date 2026-10-09", "transaction_date", "2026-10-09"),
        ("category", "Use date 2026-10-09", "transaction_date", "2026-10-09"),
    ],
)
def test_v2_pending_support_mixed_context_and_unrelated_reply_stays_pending(
    field, reply, other_field, other_value
):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    context = canonical_text(ORIGINAL)
    fields = {field: ORIGINAL[field], other_field: other_value}
    evidence = {
        field: [
            cite(context, ORIGINAL[field], "context", "retained_value"),
            cite(reply, ref="reply", role="replacement_value"),
        ],
        other_field: [cite(reply, other_value, "reply", "replacement_value")],
    }
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        fields,
        unresolved_fields=(field,),
        evidence=evidence,
    )

    assert result["state"] == "NEEDS_CLARIFICATION", (field, result)
    assert field in {
        unresolved for question in result["unresolved"] for unresolved in question["fields"]
    }, result


@pytest.mark.parametrize(
    "field,wrong_field",
    [
        ("amount", "description"),
        ("currency", "category"),
        ("transaction_date", "merchant"),
        ("merchant", "category"),
        ("description", "category"),
        ("category", "description"),
    ],
)
def test_v2_pending_support_other_field_assignment_cannot_select_same_literal(field, wrong_field):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    selected = ORIGINAL[field]
    context = canonical_text(ORIGINAL)
    reply = f"Set {wrong_field} to {selected}"
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        {field: selected, wrong_field: selected},
        unresolved_fields=(field,),
        evidence={
            field: [
                cite(context, selected, "context", "retained_value"),
                cite(reply, ref="reply", role="replacement_value"),
            ],
            wrong_field: [cite(reply, ref="reply", role="replacement_value")],
        },
    )

    assert result["state"] == "NEEDS_CLARIFICATION", (field, result)
    assert field in {
        unresolved for question in result["unresolved"] for unresolved in question["fields"]
    }, result


@pytest.mark.parametrize(
    "field,reply,quote",
    [
        ("amount", "Set amount to SGD 12.50", "12.50"),
        ("currency", "Set currency to SGD", "SGD"),
        ("transaction_date", "Set date to 2026-10-08", "2026-10-08"),
        ("merchant", "Set merchant to Example Cafe", "Set merchant to Example Cafe"),
        ("description", "Set description to Lunch", "Set description to Lunch"),
        ("category", "Set category to food", "Set category to food"),
    ],
)
def test_v2_pending_support_explicit_fresh_reply_can_select_old_value(field, reply, quote):
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        {field: ORIGINAL[field]},
        unresolved_fields=(field,),
        evidence={field: [cite(reply, quote, "reply", "replacement_value")]},
    )

    assert result["state"] == "VALID", (field, result)
    assert result["payload"][field] == ORIGINAL[field], result


@pytest.mark.parametrize(
    "field,reply",
    [
        ("amount", "Keep amount"),
        ("currency", "retain currency"),
        ("transaction_date", "保持 date"),
        ("merchant", "保留 merchant"),
        ("description", "Keep description"),
        ("category", "retain category"),
    ],
)
def test_v2_pending_support_bounded_same_field_keep_selects_current_value(field, reply):
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        {field: ORIGINAL[field]},
        unresolved_fields=(field,),
        evidence={field: [cite(reply, ref="reply", role="replacement_value")]},
    )

    assert result["state"] == "VALID", (field, result)
    assert result["payload"][field] == ORIGINAL[field], result


@pytest.mark.parametrize(
    "field,reply",
    [
        ("amount", "Keep amount and set amount to SGD 15.00"),
        ("currency", "retain currency and set currency to USD"),
        ("transaction_date", "Keep date and use 2026-10-09"),
        ("merchant", "Keep merchant and set merchant to New Cafe"),
        ("description", "Keep description and set description to Dinner"),
        ("category", "Keep category and set category to travel"),
    ],
)
def test_v2_pending_support_keep_clause_cannot_hide_conflicting_same_field_instruction(
    field, reply
):
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        {field: ORIGINAL[field]},
        unresolved_fields=(field,),
        evidence={field: [cite(reply, ref="reply", role="replacement_value")]},
    )

    assert result["state"] == "NEEDS_CLARIFICATION", (field, result)
    assert field in {
        unresolved for question in result["unresolved"] for unresolved in question["fields"]
    }, result


@pytest.mark.parametrize(
    "field",
    ["merchant", "description", "category"],
)
def test_v2_pending_support_exact_bare_text_selects_only_single_pending_text_field(field):
    reply = ORIGINAL[field]
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        {field: ORIGINAL[field]},
        unresolved_fields=(field,),
        evidence={field: [cite(reply, ref="reply", role="replacement_value")]},
    )

    assert result["state"] == "VALID", (field, result)
    assert result["payload"][field] == ORIGINAL[field], result


def test_v2_pending_support_bare_text_does_not_choose_multiple_pending_text_fields():
    reply = "Example Cafe"
    fields = {"merchant": reply, "description": reply, "category": reply}
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        fields,
        unresolved_fields=("merchant", "description", "category"),
        evidence={field: [cite(reply, ref="reply", role="replacement_value")] for field in fields},
    )

    assert result["state"] == "NEEDS_CLARIFICATION", result
    assert {
        unresolved for question in result["unresolved"] for unresolved in question["fields"]
    } == {"merchant", "description", "category"}, result


def test_v2_pending_support_named_assignments_answer_multiple_text_fields():
    reply = "Set merchant to New Cafe; set description to Dinner; set category to travel"
    fields = {"merchant": "New Cafe", "description": "Dinner", "category": "travel"}
    evidence = {
        "merchant": [cite(reply, "Set merchant to New Cafe", "reply")],
        "description": [cite(reply, "set description to Dinner", "reply")],
        "category": [cite(reply, "set category to travel", "reply")],
    }
    result = _v2_rule_validate(
        "clarification_followup",
        reply,
        fields,
        unresolved_fields=tuple(fields),
        evidence=evidence,
    )

    assert result["state"] == "VALID", result
    assert {field: result["payload"][field] for field in fields} == fields, result


def test_v2_service_date_only_followup_preserves_money_questions_and_posts_after_full_answer(
    migrated_temp_db_connection,
):
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "support-date-only-full-answer")
    owner = posting(c, ai)
    old = owner.prepare(initial.proposal_public_id)
    first_reply = "Change amount to XYZ 15.00 and date to 2026-10-08 or 2026-10-09"
    provider.response = wire(
        first_reply,
        {"amount": "15.00", "transaction_date": "2026-10-09"},
        ref="reply",
    )
    first_proof = persist_reply(c, old, first_reply, identity="support-date-only-first")
    first = ai.prepare_edit(old.review_id, first_proof, "support-date-only-first-op")
    assert ai.invoke(first.attempt_id).state == "NEEDS_CLARIFICATION"
    question = ai.prepare_clarification(first.attempt_id)
    assert {"amount", "currency", "transaction_date"} <= {
        field for item in question.projection["unresolved"] for field in item["fields"]
    }
    assert question.projection["confirm_available"] is False

    date_reply = "Use date 2026-10-09"
    context = canonical_text(ORIGINAL)
    provider.response = wire(
        date_reply,
        {"amount": "12.50", "currency": "SGD", "transaction_date": "2026-10-09"},
        ref="reply",
        evidence={
            "amount": [
                cite(context, "12.50", "context", "retained_value"),
                cite(date_reply, ref="reply", role="replacement_value"),
            ],
            "currency": [
                cite(context, "SGD", "context", "retained_value"),
                cite(date_reply, ref="reply", role="replacement_value"),
            ],
            "transaction_date": [cite(date_reply, "2026-10-09", "reply")],
        },
    )
    date_proof = persist_reply(c, question, date_reply, identity="support-date-only-answer")
    date_only = ai.prepare_followup(
        question.request_id, date_proof, "support-date-only-followup-op"
    )
    date_status = ai.invoke(date_only.attempt_id)
    assert date_status.state == "NEEDS_CLARIFICATION", date_status
    remaining = ai.prepare_clarification(date_only.attempt_id)
    assert {field for item in remaining.projection["unresolved"] for field in item["fields"]} == {
        "amount",
        "currency",
    }, remaining.projection
    assert remaining.projection["confirm_available"] is False
    with pytest.raises(AiInvocationError):
        ai.publish(date_only.attempt_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )

    complete_reply = "Use SGD 15.00 and date 2026-10-09"
    provider.response = wire(
        complete_reply,
        {"amount": "15.00", "currency": "SGD", "transaction_date": "2026-10-09"},
        ref="reply",
    )
    complete_proof = persist_reply(
        c, remaining, complete_reply, identity="support-date-only-complete-answer"
    )
    complete = ai.prepare_followup(
        remaining.request_id, complete_proof, "support-date-only-complete-op"
    )
    complete_status = ai.invoke(complete.attempt_id)
    assert complete_status.state == "VALID", complete_status
    published = ai.publish(complete.attempt_id)
    card, final = finish_post(c, ai, published, "support-date-only-confirm")
    assert card.projection["proposal_review"]["confirm_available"] is True
    assert tuple(
        c.execute(
            "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
            "FROM transactions"
        ).fetchone()
    ) == (
        15.0,
        "SGD",
        "2026-10-09",
        ORIGINAL["merchant"],
        ORIGINAL["description"],
        ORIGINAL["category"],
        final.transaction_public_id,
    )
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    assert provider.calls == 4
    assert len(provider.requests) == 4


def _tamper_v2_publication_creation_metadata(c, published, fault):
    from datetime import datetime, timezone

    from test_independent_ai_history_v1 import _tamper

    from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
    from finance_core.financial_audit.chain import _event_hash, _normalize_timestamp

    row = c.execute(
        "SELECT event_public_id,aggregate_public_id FROM financial_audit_events "
        "WHERE event_type='independent_ai_proposal_published'"
    ).fetchone()
    assert row is not None
    event = FinancialAuditRepository(c).fetch(row[0])
    publication = c.execute(
        "SELECT published_at FROM independent_ai_publications "
        "WHERE child_parser_output_id=(SELECT id FROM parser_outputs WHERE public_id=?)",
        (published.proposal_public_id,),
    ).fetchone()
    assert publication is not None
    published_at = publication[0]
    assert isinstance(published_at, int) and not isinstance(published_at, bool)
    expected_created_at = _normalize_timestamp(
        datetime.fromtimestamp(published_at, timezone.utc).isoformat()
    )
    assert event.event_type == "independent_ai_proposal_published"
    assert event.created_at == expected_created_at
    assert event.calculation_snapshot_public_id is None
    assert event.calculation_snapshot_hash is None
    trigger_before = [
        tuple(item)
        for item in c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='financial_audit_events' ORDER BY name"
        )
    ]
    assert trigger_before
    changes = {
        "event_type": {"event_type": "ordinary_event"},
        "created_at": {"created_at": "2026-10-01T00:00:00.000000Z"},
        "snapshot_pair": {
            "calculation_snapshot_public_id": "snapshot-unrelated",
            "calculation_snapshot_hash": "f" * 64,
        },
    }[fault]
    changed = dataclasses.replace(event, **changes)
    changed = dataclasses.replace(
        changed,
        event_hash=_event_hash(
            changed,
            previous_state_hash=changed.previous_state_hash,
            new_state_hash=changed.new_state_hash,
        ),
    )
    columns = [*changes, "event_hash"]
    _tamper(
        c,
        "financial_audit_events",
        "UPDATE financial_audit_events SET "
        + ",".join(column + "=?" for column in columns)
        + " WHERE event_public_id=?",
        tuple(getattr(changed, column) for column in columns) + (event.event_public_id,),
    )
    mutated = FinancialAuditRepository(c).fetch(event.event_public_id)
    trigger_after = [
        tuple(item)
        for item in c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='financial_audit_events' ORDER BY name"
        )
    ]
    chain = verify_financial_audit_chain(
        c, aggregate_type="parser_proposal", aggregate_public_id=row[1]
    )
    assert trigger_after == trigger_before
    assert chain.valid is True, chain
    assert mutated.event_public_id == event.event_public_id
    assert mutated.event_payload_json == event.event_payload_json
    assert published.proposal_public_id
    return row[1]


@pytest.mark.parametrize("fault", ["event_type", "created_at", "snapshot_pair"])
@pytest.mark.parametrize(
    "surface",
    ["status", "cached_prepare", "cached_submit", "recovery", "final_readback"],
)
def test_v2_publication_creation_metadata_corruption_rejected_at_actual_owner_surface(
    migrated_temp_db_connection, fault, surface
):
    from finance_core.application.posting import PostingError

    c = migrated_temp_db_connection
    ai, provider, published = published_text(c, f"audit-owner-{fault}-{surface}")
    owner = posting(c, ai)
    if surface == "cached_prepare":
        owner.prepare(published.proposal_public_id)
    elif surface == "cached_submit":
        card = owner.prepare(published.proposal_public_id)
        decision_id = f"audit-owner-decision-{fault}-{surface}"
        _persist_fresh_posting_confirmation(c, card, published.proposal_public_id, decision_id)
    elif surface == "final_readback":
        card, final = finish_post(c, ai, published, f"audit-owner-confirm-{fault}-{surface}")
        assert final.state == "finalized" and card.review_id
    transaction_count = c.execute("SELECT count(*) FROM transactions").fetchone()[0]

    _tamper_v2_publication_creation_metadata(c, published, fault)

    audit_refusal = "^Independent AI publication audit changed$"
    if surface == "status":
        with pytest.raises(AiInvocationError, match=audit_refusal):
            ai.get_status(published.attempt_id)
    elif surface == "cached_prepare":
        with pytest.raises(PostingError, match=audit_refusal) as refusal:
            owner.prepare(published.proposal_public_id)
        assert isinstance(refusal.value.__cause__, AiInvocationError)
    elif surface == "cached_submit":
        with pytest.raises(PostingError, match=audit_refusal) as refusal:
            owner.submit_post(card.review_id, decision_id)
        assert isinstance(refusal.value.__cause__, AiInvocationError)
    elif surface == "recovery":
        with pytest.raises(AiInvocationError, match=audit_refusal):
            ai.recover(published.attempt_id)
    else:
        with pytest.raises(PostingError, match=audit_refusal) as refusal:
            owner.get_status(final.attempt_id)
        assert isinstance(refusal.value.__cause__, AiInvocationError)

    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == transaction_count
    assert provider.calls == 1


@pytest.mark.parametrize(
    "target,other_field,text,literal,case_id",
    [
        pytest.param(
            "description",
            "category",
            'Set category to "Clear description"',
            "Clear description",
            "english-quoted-description",
            id="english-quoted-description",
        ),
        pytest.param(
            "description",
            "category",
            "Set category to Clear description",
            "Clear description",
            "english-unquoted-description",
            id="english-unquoted-description",
        ),
        pytest.param(
            "description",
            "category",
            "分类：“清空备注”",
            "清空备注",
            "chinese-quoted-description",
            id="chinese-quoted-description",
        ),
        pytest.param(
            "description",
            "category",
            "分类：清空备注",
            "清空备注",
            "chinese-unquoted-description",
            id="chinese-unquoted-description",
        ),
        pytest.param(
            "category",
            "description",
            'Set description to "Clear category"',
            "Clear category",
            "english-quoted-category",
            id="english-quoted-category",
        ),
        pytest.param(
            "category",
            "description",
            "Set description to Clear category",
            "Clear category",
            "english-unquoted-category",
            id="english-unquoted-category",
        ),
        pytest.param(
            "category",
            "description",
            "备注：“清空分类”",
            "清空分类",
            "chinese-quoted-category",
            id="chinese-quoted-category",
        ),
        pytest.param(
            "category",
            "description",
            "备注：清空分类",
            "清空分类",
            "chinese-unquoted-category",
            id="chinese-unquoted-category",
        ),
    ],
)
@pytest.mark.parametrize("citation_scope", ["full", "value"])
@pytest.mark.parametrize("current_is_null", [False, True], ids=["nonnull", "null"])
@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
def test_v2_rule_contract_other_field_clear_literal_cannot_authorize_optional_null(
    kind, current_is_null, citation_scope, target, other_field, text, literal, case_id
):
    current = {
        **ORIGINAL,
        target: None if current_is_null else ORIGINAL[target],
    }
    evidence = {target: [cite(text, literal if citation_scope == "value" else None, "reply")]}
    result = _v2_rule_validate(
        kind,
        text,
        {target: None, other_field: literal},
        current_values=current,
        base=current,
        unresolved_fields=(target,) if kind == "clarification_followup" else (),
        evidence=evidence,
    )
    assert result["state"] == "NEEDS_CLARIFICATION", (case_id, result)
    unresolved = {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, (case_id, result)
    assert result["draft_values"][target] == current[target], (case_id, result)


@pytest.mark.parametrize(
    "target,other_field,text,literal,model_other_value,case_id",
    [
        pytest.param(
            "description",
            "category",
            'Set category to "Clear description"',
            "Clear description",
            None,
            "description-other-operation-omitted",
            id="description-other-operation-omitted",
        ),
        pytest.param(
            "description",
            "category",
            'Set category to "Clear description"',
            "Clear description",
            "Clear",
            "description-other-operation-truncated",
            id="description-other-operation-truncated",
        ),
        pytest.param(
            "description",
            "category",
            'Set category to "Clear description"',
            "Clear description",
            "Travel",
            "description-other-operation-mismatched",
            id="description-other-operation-mismatched",
        ),
        pytest.param(
            "category",
            "description",
            'Set description to "Clear category"',
            "Clear category",
            None,
            "category-other-operation-omitted",
            id="category-other-operation-omitted",
        ),
        pytest.param(
            "category",
            "description",
            'Set description to "Clear category"',
            "Clear category",
            "Clear",
            "category-other-operation-truncated",
            id="category-other-operation-truncated",
        ),
        pytest.param(
            "category",
            "description",
            'Set description to "Clear category"',
            "Clear category",
            "Dinner",
            "category-other-operation-mismatched",
            id="category-other-operation-mismatched",
        ),
    ],
)
@pytest.mark.parametrize("citation_scope", ["full", "value"])
def test_v2_rule_contract_literal_cannot_authorize_null_when_other_operation_is_wrong(
    target,
    other_field,
    text,
    literal,
    model_other_value,
    case_id,
    citation_scope,
):
    fields = {target: None}
    if model_other_value is not None:
        fields[other_field] = model_other_value
    current = dict(ORIGINAL)
    result = _v2_rule_validate(
        "clarification_followup",
        text,
        fields,
        current_values=current,
        base=current,
        unresolved_fields=(target,),
        evidence={target: [cite(text, literal if citation_scope == "value" else None, "reply")]},
    )
    assert result["state"] == "NEEDS_CLARIFICATION", (case_id, result)
    unresolved = {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, (case_id, result)
    assert result["draft_values"][target] == current[target], (case_id, result)


@pytest.mark.parametrize(
    "target,other_field,literal,text,clear_instruction",
    [
        (
            "description",
            "category",
            "Clear description",
            'Set category to "Clear description"; Remove the description',
            "Remove the description",
        ),
        (
            "category",
            "description",
            "Clear category",
            'Set description to "Clear category"; Remove the category',
            "Remove the category",
        ),
    ],
)
def test_v2_rule_contract_real_clear_outside_other_literal_requires_its_citation(
    target, other_field, literal, text, clear_instruction
):
    current = dict(ORIGINAL)
    fields = {target: None, other_field: literal}
    covered = _v2_rule_validate(
        "clarification_followup",
        text,
        fields,
        current_values=current,
        base=current,
        unresolved_fields=(target,),
        evidence={target: [cite(text, clear_instruction, "reply")]},
    )
    assert covered["state"] == "VALID", covered
    assert covered["payload"][target] is None, covered
    assert covered["payload"][other_field] == literal, covered

    literal_only = _v2_rule_validate(
        "clarification_followup",
        text,
        fields,
        current_values=current,
        base=current,
        unresolved_fields=(target,),
        evidence={target: [cite(text, literal, "reply")]},
    )
    assert literal_only["state"] == "NEEDS_CLARIFICATION", literal_only
    unresolved = {
        field for question in literal_only.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, literal_only
    assert literal_only["draft_values"][target] == current[target], literal_only


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
def test_v2_rule_contract_inner_escaped_quote_literal_does_not_authorize_optional_null(kind):
    target = "description"
    other_field = "category"
    text = r'Set category to "note \"Clear description\" end"'
    literal = 'note "Clear description" end'
    current = dict(ORIGINAL)
    result = _v2_rule_validate(
        kind,
        text,
        {target: None, other_field: literal},
        current_values=current,
        base=current,
        unresolved_fields=(target,) if kind == "clarification_followup" else (),
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    unresolved = {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, result
    assert result["draft_values"][target] == current[target], result


@pytest.mark.parametrize(
    "target,other_field,literal,separator",
    [
        ("description", "category", "Clear description", "; "),
        ("description", "category", "Clear description", ";\n"),
        ("category", "description", "Clear category", "; "),
        ("category", "description", "Clear category", ";\n"),
    ],
)
def test_v2_rule_contract_quoted_separator_inside_other_literal_does_not_authorize_null(
    target, other_field, literal, separator
):
    value = "Other" + separator + literal
    cue = "category" if other_field == "category" else "description"
    text = f'Set {cue} to "{value}"'
    current = dict(ORIGINAL)
    result = _v2_rule_validate(
        "clarification_followup",
        text,
        {target: None, other_field: value},
        current_values=current,
        base=current,
        unresolved_fields=(target,),
    )
    if separator == ";\n":
        # Raw evidence may contain LF; canonical model field values may not.
        assert result == {"state": "RESPONSE_REJECTED", "reason": "invalid_response"}, result
        return
    assert result["state"] == "NEEDS_CLARIFICATION", result
    unresolved = {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, result
    assert result["draft_values"][target] == current[target], result


@pytest.mark.parametrize(
    "target,other_field,literal",
    [
        ("description", "category", "Clear description"),
        ("category", "description", "Clear category"),
    ],
)
def test_v2_rule_contract_raw_newline_literal_without_other_operation_keeps_clear_pending(
    target, other_field, literal
):
    # Do not submit the LF-containing literal as a canonical model field value.
    text = f'Set {other_field} to "Other;\n{literal}"'
    current = dict(ORIGINAL)
    result = _v2_rule_validate(
        "clarification_followup",
        text,
        {target: None},
        current_values=current,
        base=current,
        unresolved_fields=(target,),
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    unresolved = {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, result
    assert result["draft_values"][target] == current[target], result
    assert result["draft_values"][other_field] == current[other_field], result


@pytest.mark.parametrize("kind", ["card_edit", "clarification_followup"])
@pytest.mark.parametrize(
    "target,text",
    [("category", "category: clear"), ("description", "description：清空")],
)
def test_v2_rule_contract_field_first_clear_forms_remain_valid(kind, target, text):
    result = _v2_rule_validate(
        kind,
        text,
        {target: None},
        unresolved_fields=(target,) if kind == "clarification_followup" else (),
    )
    assert result["state"] == "VALID", result
    assert result["payload"][target] is None, result


@pytest.mark.parametrize(
    "target,text",
    [
        ("description", 'Set description to "Clear description"'),
        ("category", 'Set category to "Clear category"'),
    ],
)
def test_v2_rule_contract_self_field_literal_does_not_authorize_optional_null(target, text):
    current = dict(ORIGINAL)
    result = _v2_rule_validate(
        "clarification_followup",
        text,
        {target: None},
        current_values=current,
        base=current,
        unresolved_fields=(target,),
    )
    assert result["state"] == "NEEDS_CLARIFICATION", result
    unresolved = {
        field for question in result.get("unresolved", []) for field in question["fields"]
    }
    assert target in unresolved, result
    assert result["draft_values"][target] == current[target], result


def test_v2_rule_service_false_clear_is_not_confirmable_or_carried_into_followup(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "rule-literal-clear-clarification")
    owner = posting(c, ai)
    old_card = owner.prepare(initial.proposal_public_id)

    misleading_reply = 'Set category to "Clear description"'
    proof = persist_reply(c, old_card, misleading_reply, identity="rule-literal-clear-edit-reply")
    provider.response = wire(
        misleading_reply,
        {"description": None},
        ref="reply",
    )
    edit = ai.prepare_edit(old_card.review_id, proof, "rule-literal-clear-edit-op")
    invoked = ai.invoke(edit.attempt_id)
    assert invoked.state == "NEEDS_CLARIFICATION", invoked
    question = ai.prepare_clarification(edit.attempt_id)
    assert question.projection["confirm_available"] is False
    unresolved = {field for item in question.projection["unresolved"] for field in item["fields"]}
    assert {"description", "category"} <= unresolved, question.projection
    assert question.projection["draft_values"]["description"] == ORIGINAL["description"]
    assert question.projection["draft_values"]["category"] == ORIGINAL["category"]
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 0
    with pytest.raises(AiInvocationError, match="^Only valid timely AI result may publish$"):
        ai.publish(edit.attempt_id)

    actual_reply = 'Set category to "Clear description"; Remove the description'
    followup_proof = persist_reply(
        c, question, actual_reply, identity="rule-literal-clear-clarification-reply"
    )
    provider.response = wire(
        actual_reply,
        {"category": "Clear description", "description": None},
        ref="reply",
    )
    followup = ai.prepare_followup(
        question.request_id,
        followup_proof,
        "rule-literal-clear-clarification-op",
    )
    completed = ai.invoke(followup.attempt_id)
    assert completed.state == "VALID", completed
    publication = ai.publish(followup.attempt_id)
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 2

    card, final = finish_post(c, ai, publication, "rule-literal-clear-confirm")
    assert card.projection["proposal_review"]["confirm_available"] is True
    projection = card.projection["financial_projection"]
    assert projection["amount"] == ORIGINAL["amount"]
    assert projection["currency"] == ORIGINAL["currency"]
    assert projection["transaction_date"] == ORIGINAL["transaction_date"]
    assert projection["merchant"] == ORIGINAL["merchant"]
    assert projection["description"] is None
    assert projection["category"] == "Clear description"
    row = c.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
        "FROM transactions"
    ).fetchone()
    assert tuple(row) == (
        12.5,
        "SGD",
        "2026-10-08",
        "Example Cafe",
        None,
        "Clear description",
        final.transaction_public_id,
    )
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM application_posting_decisions").fetchone()[0] == 1
    assert provider.calls == 3


# This independent contract inventory is transcribed from the finite grammar
# in the approved proposal. It intentionally does not import or inspect the
# production scanner's regular expressions.
_QUOTED_AT_FIELD_CUES = (
    ("merchant", ("商户", "商家", "merchant")),
    ("description", ("描述", "备注", "description")),
    ("category", ("分类", "类别", "category")),
)
_QUOTED_AT_SEPARATORS = (
    ("to", " to "),
    ("as", " as "),
    ("改为", "改为"),
    ("改成", "改成"),
    ("为", "为"),
    ("成", "成"),
    ("是", "是"),
    ("ascii_colon", ": "),
    ("chinese_colon", "："),
)
_QUOTED_AT_MODES = (
    ("unquoted", "", ""),
    ("ascii_double", '"', '"'),
    ("ascii_single", "'", "'"),
    ("curly_double", "“", "”"),
)
_QUOTED_AT_CANONICAL_CUES = {
    "merchant": "merchant",
    "description": "description",
    "category": "category",
}


def _quoted_at_finite_prefix_fixtures():
    fixtures = []
    target_for_literal_field = {
        "merchant": "category",
        "description": "category",
        "category": "description",
    }
    for literal_field, cues in _QUOTED_AT_FIELD_CUES:
        target = target_for_literal_field[literal_field]
        other_value = f"Clear {target}"
        secondary_field = "description" if literal_field == "merchant" else "merchant"
        for cue in cues:
            for separator_name, separator in _QUOTED_AT_SEPARATORS:
                prefix = cue
                if cue in {"merchant", "description", "category"} and separator_name in {
                    "改为",
                    "改成",
                    "为",
                    "成",
                    "是",
                }:
                    # The source grammar uses Unicode word boundaries for
                    # English cues, so separate them from Chinese connectors.
                    prefix += " "
                for mode, opening, closing in _QUOTED_AT_MODES:
                    fixtures.append(
                        {
                            "fixture_id": (f"{literal_field}-{cue}-{separator_name}-{mode}"),
                            "literal_field": literal_field,
                            "other_field": literal_field,
                            "secondary_field": secondary_field,
                            "target": target,
                            "literal": other_value,
                            "instruction": (prefix + separator + opening + other_value + closing),
                        }
                    )
    for mode, opening, closing in (
        ("ascii_double", '"', '"'),
        ("ascii_single", "'", "'"),
        ("curly_double", "“", "”"),
    ):
        fixtures.append(
            {
                "fixture_id": f"merchant-at-{mode}",
                "literal_field": "merchant",
                "other_field": "merchant",
                "secondary_field": "description",
                "target": "category",
                "literal": "Clear category",
                "instruction": f"at {opening}Clear category{closing}",
            }
        )
    return fixtures


def _quoted_at_unresolved_fields(result):
    return {
        field for question in result.get("unresolved", []) for field in question.get("fields", [])
    }


def test_v2_rule_contract_quoted_at_finite_prefix_inventory_1308_checks():
    fixtures = _quoted_at_finite_prefix_fixtures()
    assert len(fixtures) == 327
    assert len({fixture["fixture_id"] for fixture in fixtures}) == 327
    representations = ("exact", "absent", "truncated", "mismatched")
    failures = []
    checks = 0
    current = dict(ORIGINAL)

    for fixture in fixtures:
        target = fixture["target"]
        other_field = fixture["other_field"]
        secondary_field = fixture["secondary_field"]
        text = (
            fixture["instruction"]
            + f'; Set {_QUOTED_AT_CANONICAL_CUES[secondary_field]} to "Spare"'
        )
        for representation in representations:
            fields = {target: None, secondary_field: "Spare"}
            evidence = {
                target: [cite(text, ref="reply")],
                secondary_field: [cite(text, "Spare", "reply")],
            }
            if representation == "exact":
                fields[other_field] = fixture["literal"]
                evidence[other_field] = [cite(text, fixture["literal"], "reply")]
            elif representation == "truncated":
                fields[other_field] = "Clear"
                evidence[other_field] = [cite(text, "Clear", "reply")]
            elif representation == "mismatched":
                # "Spare" is assigned to the secondary field in the actual
                # text; the model attributes the same quoted value to another
                # field. This keeps the operation evidenced but mismatched.
                fields[other_field] = "Spare"
                evidence[other_field] = [cite(text, "Spare", "reply")]

            result = _v2_rule_validate(
                "clarification_followup",
                text,
                fields,
                current_values=current,
                base=current,
                unresolved_fields=(target,),
                evidence=evidence,
            )
            checks += 1
            pending = _quoted_at_unresolved_fields(result)
            if (
                result.get("state") != "NEEDS_CLARIFICATION"
                or target not in pending
                or result.get("draft_values", {}).get(target) != current[target]
            ):
                failures.append(
                    {
                        "fixture": fixture["fixture_id"],
                        "representation": representation,
                        "target": target,
                        "state": result.get("state"),
                        "pending": sorted(pending),
                        "draft_target": result.get("draft_values", {}).get(target),
                    }
                )
    assert checks == 1308
    assert not failures, f"{len(failures)} of {checks} finite checks failed: {failures[:20]}"


_QUOTED_AT_NEGATIVE_ROWS = (
    pytest.param(
        "N01",
        "card_edit",
        "category",
        'at "Clear category"',
        {},
        {},
        False,
        "full",
        id="N01-card-edit-category-ascii-double-full-reply",
    ),
    pytest.param(
        "N02",
        "clarification_followup",
        "category",
        'at "Clear category"',
        {},
        {},
        False,
        "literal",
        id="N02-followup-category-ascii-double-literal-only-citation",
    ),
    pytest.param(
        "N03",
        "card_edit",
        "description",
        'at "Clear description"',
        {},
        {},
        False,
        "full",
        id="N03-card-edit-description-ascii-double",
    ),
    pytest.param(
        "N04",
        "clarification_followup",
        "description",
        'at "Clear description"',
        {},
        {},
        False,
        "full",
        id="N04-followup-description-ascii-double",
    ),
    pytest.param(
        "N05",
        "clarification_followup",
        "category",
        'at "Clear category"; Set description to "Shop"',
        {"merchant": "Shop", "description": "Shop"},
        {"merchant": "Shop", "description": "Shop"},
        True,
        "full",
        id="N05-followup-current-null-mismatched-merchant-operation",
    ),
    pytest.param(
        "N06",
        "clarification_followup",
        "description",
        'at "Clear description"; Set category to "Shop"',
        {"merchant": "Clear", "category": "Shop"},
        {
            "merchant": 'at "Clear description"',
            "category": "Shop",
        },
        False,
        "full",
        id="N06-followup-truncated-merchant-with-full-quoted-citation",
    ),
    pytest.param(
        "N07",
        "card_edit",
        "category",
        "at 'Clear category'",
        {},
        {},
        False,
        "full",
        id="N07-card-edit-category-ascii-single",
    ),
    pytest.param(
        "N08",
        "clarification_followup",
        "description",
        "at “Clear description”",
        {},
        {},
        False,
        "full",
        id="N08-followup-description-curly-double",
    ),
    pytest.param(
        "N09",
        "clarification_followup",
        "category",
        'at "Clear category; Clear description"',
        {},
        {},
        False,
        "full",
        id="N09-followup-category-quoted-semicolon-and-second-clear",
    ),
    pytest.param(
        "N10",
        "clarification_followup",
        "description",
        "at “Clear description\nand Clear category”",
        {},
        {},
        False,
        "full",
        id="N10-followup-description-quoted-newline-conjunction",
    ),
    pytest.param(
        "N11",
        "clarification_followup",
        "category",
        r'at "note \"Clear category\" end"',
        {},
        {},
        False,
        "full",
        id="N11-followup-category-escaped-interior-quotes",
    ),
    pytest.param(
        "N12",
        "clarification_followup",
        "description",
        'at "Clear description',
        {},
        {},
        False,
        "full",
        id="N12-followup-description-unmatched-opening-quote",
    ),
)


@pytest.mark.parametrize(
    "case_id,kind,target,text,model_fields,operation_quotes,current_is_null,citation_scope",
    _QUOTED_AT_NEGATIVE_ROWS,
)
def test_v2_rule_contract_quoted_at_negative_matrix_keeps_optional_clear_pending(
    case_id,
    kind,
    target,
    text,
    model_fields,
    operation_quotes,
    current_is_null,
    citation_scope,
):
    current = {
        **ORIGINAL,
        target: None if current_is_null else ORIGINAL[target],
    }
    fields = {target: None, **model_fields}
    target_quote = "Clear " + target if citation_scope == "literal" else None
    evidence = {target: [cite(text, target_quote, "reply")]}
    for field, quote in operation_quotes.items():
        evidence[field] = [cite(text, quote, "reply")]

    result = _v2_rule_validate(
        kind,
        text,
        fields,
        current_values=current,
        base=current,
        unresolved_fields=(target,) if kind == "clarification_followup" else (),
        evidence=evidence,
    )
    assert result["state"] == "NEEDS_CLARIFICATION", (case_id, result)
    assert target in _quoted_at_unresolved_fields(result), (case_id, result)
    assert result["draft_values"][target] == current[target], (case_id, result)


@pytest.mark.parametrize(
    "case_id",
    ["C01", "C02", "C03", "C04", "C05", "C06"],
    ids=[
        "C01-outside-category-clear-with-fresh-citation",
        "C02-outside-description-clear-with-fresh-citation",
        "C03-two-independent-outside-clears",
        "C04-field-first-category-clear",
        "C05-outside-clear-citation-covers-only-literal",
        "C06-current-null-no-op-with-quoted-at-merchant",
    ],
)
def test_v2_rule_contract_quoted_at_genuine_clear_and_citation_controls(case_id):
    if case_id in {"C01", "C02"}:
        target = "category" if case_id == "C01" else "description"
        literal = "Clear category" if target == "category" else "Clear description"
        clear = f"Remove the {target}"
        text = f'at "{literal}"; {clear}'
        result = _v2_rule_validate(
            "clarification_followup",
            text,
            {"merchant": literal, target: None},
            current_values=ORIGINAL,
            base=ORIGINAL,
            unresolved_fields=(target,),
            evidence={
                "merchant": [cite(text, literal, "reply")],
                target: [cite(text, clear, "reply")],
            },
        )
        assert result["state"] == "VALID", (case_id, result)
        assert result["payload"][target] is None, (case_id, result)
        return

    if case_id == "C03":
        text = 'at "Clear category"; Remove the category and remove the description'
        category_clear = "Remove the category"
        description_clear = "remove the description"
        result = _v2_rule_validate(
            "clarification_followup",
            text,
            {
                "merchant": "Clear category",
                "category": None,
                "description": None,
            },
            current_values=ORIGINAL,
            base=ORIGINAL,
            unresolved_fields=("category", "description"),
            evidence={
                "merchant": [cite(text, "Clear category", "reply")],
                "category": [cite(text, category_clear, "reply")],
                "description": [cite(text, description_clear, "reply")],
            },
        )
        assert result["state"] == "VALID", (case_id, result)
        assert result["payload"]["category"] is None, (case_id, result)
        assert result["payload"]["description"] is None, (case_id, result)
        return

    if case_id == "C04":
        text = 'at "Clear category"; category: clear'
        clear = "category: clear"
        result = _v2_rule_validate(
            "clarification_followup",
            text,
            {"merchant": "Clear category", "category": None},
            current_values=ORIGINAL,
            base=ORIGINAL,
            unresolved_fields=("category",),
            evidence={
                "merchant": [cite(text, "Clear category", "reply")],
                "category": [cite(text, clear, "reply")],
            },
        )
        assert result["state"] == "VALID", (case_id, result)
        assert result["payload"]["category"] is None, (case_id, result)
        return

    if case_id == "C05":
        text = 'at "Clear category"; Remove the category'
        result = _v2_rule_validate(
            "clarification_followup",
            text,
            {"merchant": "Clear category", "category": None},
            current_values=ORIGINAL,
            base=ORIGINAL,
            unresolved_fields=("category",),
            evidence={
                "merchant": [cite(text, "Clear category", "reply")],
                "category": [cite(text, "Clear category", "reply")],
            },
        )
        assert result["state"] == "NEEDS_CLARIFICATION", (case_id, result)
        assert "category" in _quoted_at_unresolved_fields(result), (case_id, result)
        assert result["draft_values"]["category"] == ORIGINAL["category"], (
            case_id,
            result,
        )
        return

    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    current = {**ORIGINAL, "category": None}
    context = canonical_text(current)
    text = 'at "Clear category"'
    result = _v2_rule_validate(
        "clarification_followup",
        text,
        {"merchant": "Clear category", "category": None},
        current_values=current,
        base=current,
        evidence={
            "merchant": [cite(text, "Clear category", "reply")],
            "category": [cite(context, '"category":null', "context", "retained_value")],
        },
    )
    assert result["state"] == "VALID", (case_id, result)
    assert result["payload"]["category"] is None, (case_id, result)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("card_edit", id="T01-card-edit-two-step-carry"),
        pytest.param("clarification_followup", id="T02-followup-two-step-carry"),
    ],
)
def test_v2_rule_contract_quoted_at_money_followup_keeps_real_category_question(kind):
    first_text = 'at "Clear category"'
    initial_pending = ("amount", "currency") if kind == "clarification_followup" else ()
    first = _v2_rule_validate(
        kind,
        first_text,
        {"category": None},
        current_values=ORIGINAL,
        base=ORIGINAL,
        unresolved_fields=initial_pending,
        evidence={"category": [cite(first_text, ref="reply")]},
    )
    assert isinstance(first.get("draft_values"), dict), first
    failures = []
    first_pending = tuple(
        field
        for field in ("amount", "currency", "category")
        if field in _quoted_at_unresolved_fields(first)
    )
    if first.get("state") != "NEEDS_CLARIFICATION":
        failures.append(("quoted-at reply was not clarification", first))
    if "category" not in first_pending:
        failures.append(("category missing from actual first pending set", first))
    if first["draft_values"].get("category") != ORIGINAL["category"]:
        failures.append(("first draft did not preserve category", first))

    money_text = "amount SGD 12.50"
    money = _v2_rule_validate(
        "clarification_followup",
        money_text,
        {"amount": "12.50", "currency": "SGD"},
        current_values=ORIGINAL,
        base=ORIGINAL,
        unresolved_fields=first_pending,
        draft_values=first["draft_values"],
        evidence={
            "amount": [cite(money_text, ref="reply")],
            "currency": [cite(money_text, ref="reply")],
        },
    )
    money_pending = _quoted_at_unresolved_fields(money)
    if money.get("state") != "NEEDS_CLARIFICATION":
        failures.append(("money-only followup was incorrectly confirmable", money))
    if "category" not in money_pending:
        failures.append(("money-only followup lost category obligation", money))
    if money.get("draft_values", {}).get("category") != ORIGINAL["category"]:
        failures.append(("money-only followup carried the wrong category", money))

    if "category" in money_pending:
        clear_text = "Remove the category"
        clear = _v2_rule_validate(
            "clarification_followup",
            clear_text,
            {"category": None},
            current_values=ORIGINAL,
            base=ORIGINAL,
            unresolved_fields=tuple(money_pending),
            draft_values=money["draft_values"],
            evidence={"category": [cite(clear_text, ref="reply")]},
        )
        if clear.get("state") != "VALID":
            failures.append(("fresh exact category clear did not resolve", clear))
        elif (
            clear.get("payload", {}).get("category", "missing") is not None
            or clear["payload"].get("amount") != "12.50"
            or clear["payload"].get("currency") != "SGD"
        ):
            failures.append(("final grounded card changed unrelated values", clear))
    else:
        failures.append(
            ("no actual category question remained; no synthetic carry was fabricated", money)
        )
    assert not failures, failures


def test_v2_rule_service_quoted_at_clear_requires_real_clear_before_single_post(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, initial = published_text(c, "quoted-at-clear-real-service")
    owner = posting(c, ai)
    old_card = owner.prepare(initial.proposal_public_id)

    quoted_reply = 'at "Clear category"'
    quoted_proof = persist_reply(c, old_card, quoted_reply, identity="quoted-at-clear-first-reply")
    provider.response = wire(
        quoted_reply,
        {"category": None},
        ref="reply",
        evidence={"category": [cite(quoted_reply, ref="reply")]},
    )
    edit = ai.prepare_edit(old_card.review_id, quoted_proof, "quoted-at-clear-first-operation")
    assert ai.invoke(edit.attempt_id).state == "NEEDS_CLARIFICATION"
    question = ai.prepare_clarification(edit.attempt_id)
    assert question.projection["confirm_available"] is False
    assert "category" in {
        field for item in question.projection["unresolved"] for field in item["fields"]
    }, question.projection
    assert question.projection["draft_values"]["category"] == ORIGINAL["category"]
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 0

    money_reply = "amount SGD 12.50"
    money_proof = persist_reply(c, question, money_reply, identity="quoted-at-clear-money-reply")
    provider.response = wire(
        money_reply,
        {"amount": "12.50", "currency": "SGD"},
        ref="reply",
    )
    money_attempt = ai.prepare_followup(
        question.request_id,
        money_proof,
        "quoted-at-clear-money-operation",
    )
    assert ai.invoke(money_attempt.attempt_id).state == "NEEDS_CLARIFICATION"
    remaining = ai.prepare_clarification(money_attempt.attempt_id)
    assert remaining.projection["confirm_available"] is False
    assert {field for item in remaining.projection["unresolved"] for field in item["fields"]} == {
        "category"
    }, remaining.projection
    assert remaining.projection["draft_values"]["category"] == ORIGINAL["category"]
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 0

    clear_reply = "Remove the category"
    clear_proof = persist_reply(
        c, remaining, clear_reply, identity="quoted-at-clear-authorized-reply"
    )
    provider.response = wire(
        clear_reply,
        {"category": None},
        ref="reply",
    )
    clear_attempt = ai.prepare_followup(
        remaining.request_id,
        clear_proof,
        "quoted-at-clear-authorized-operation",
    )
    assert ai.invoke(clear_attempt.attempt_id).state == "VALID"
    publication = ai.publish(clear_attempt.attempt_id)
    card, final = finish_post(c, ai, publication, "quoted-at-clear-single-confirmation")
    assert card.projection["proposal_review"]["confirm_available"] is True
    projection = card.projection["financial_projection"]
    assert projection["amount"] == ORIGINAL["amount"]
    assert projection["currency"] == ORIGINAL["currency"]
    assert projection["transaction_date"] == ORIGINAL["transaction_date"]
    assert projection["merchant"] == ORIGINAL["merchant"]
    assert projection["description"] == ORIGINAL["description"]
    assert projection["category"] is None
    row = c.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category,public_id "
        "FROM transactions"
    ).fetchone()
    assert tuple(row) == (
        12.5,
        "SGD",
        "2026-10-08",
        "Example Cafe",
        "Lunch",
        None,
        final.transaction_public_id,
    )
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM application_posting_attempts").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM application_posting_decisions").fetchone()[0] == 1
    assert provider.calls == 4
