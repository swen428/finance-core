"""Finite known intent/date conflicts, not a proof of unrestricted prose."""

from __future__ import annotations

import json

import pytest
from test_application_amendment_v1 import _persist_fresh_posting_confirmation
from test_application_posting_recovery_v1 import _prepare_text_subject
from test_independent_ai_invocation_v1 import (
    ORIGINAL,
    _review_fix_receipt,
    _source_money_quote,
    amendment,
    persist_interpretation_reply,
    posting,
    response,
    service,
    subject,
)

from finance_core.application.ai_invocation_contract import AI_RESULT_SCHEMA_V1 as AI_RESULT_SCHEMA
from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.parser_proposals.independent_ai_validation import _known_date_alternative

_RELATION_CASES = [
    (text, True)
    for cue in ("yesterday", "October eighth", "October ninth", "March fourth")
    for text in ("2026-10-08 or " + cue, cue + " or 2026-10-08")
]
_RELATION_CASES += [
    ("May Cafe 2026-10-08", False),
    ("2026-10-08 October special", False),
    ("Change description to October special", False),
    ("Change date to 2026-10-08", False),
    ("2026-10-08 lunch or October ninth", False),
    ("October special; 2026-10-08 or October ninth", True),
    ("March fourth or 2026-10-08; October special", True),
    ("October special or lunch or 2026-10-08", False),
    ("2026-10-08 or lunch or October special", False),
    ("October ninth or 2026-10-08 or lunch", True),
    ("lunch or 2026-10-08 or March fourth", True),
    ("October special 5.80 or 2026-10-08", True),
    ("2026-10-08 or coffee 5.80 October special", True),
    ("2026.10.08 or 2026-10-08", True),
    ("2026-10-08 or 2026.10.08", True),
]
_RELATION_CASES += [
    (text, False)
    for boundary in (";", "；", "\n", "\r", "!", "！", "?", "？", ".", "。")
    for text in (
        "October special" + boundary + "2026-10-08 or lunch",
        "lunch or 2026-10-08" + boundary + "October special",
    )
]
_RELATION_CASES += [
    (text, True)
    for connector in ("或者", "或", "还是")
    for text in (
        "2026-10-08 " + connector + " October ninth",
        "March fourth " + connector + " 2026-10-08",
    )
]


@pytest.mark.parametrize("text,expected", _RELATION_CASES)
def test_proposal_date_relation_fixed_properties(text, expected):
    assert _known_date_alternative(text) is expected


def _all_rows(c):
    return {
        name: [tuple(r) for r in c.execute('SELECT * FROM "' + name + '"')]
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }


def _refusal(c, ai, provider, attempt, *, state=None, reason=None):
    result = ai.invoke(attempt.attempt_id)
    assert result.state in ("NEEDS_CLARIFICATION", "RESPONSE_REJECTED")
    if state is not None:
        assert result.state == state and result.reason == reason
    before = _all_rows(c)
    assert ai.get_status(attempt.attempt_id) == ai.recover(attempt.attempt_id) == result
    with pytest.raises(AiInvocationError):
        ai.publish(attempt.attempt_id)
    assert _all_rows(c) == before and provider.calls == 1
    assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("word", ["Refund", "Refunded", "Refunds", "Refunding"])
@pytest.mark.parametrize("role", ["initial", "ocr", "edit"])
def test_proposal_refund_family_never_becomes_expense(
    migrated_temp_db_connection, tmp_path, word, role
):
    if role == "ocr":
        c, ai, provider, _, intake, *_ = _review_fix_receipt(
            tmp_path, merchant=word + " EXAMPLE CAFE"
        )
        provider.response = response(
            fields={**ORIGINAL, "merchant": "EXAMPLE CAFE"}, quote=_source_money_quote(c)
        )
        attempt = ai.prepare_initial(intake, "proposal-refund")
    else:
        c = migrated_temp_db_connection
        tail = "SGD 12.50 at Example Cafe 2026-10-08"
        raw = word + " " + tail
        if role == "initial":
            _, _, _, intake, *_ = _prepare_text_subject(c, suffix="proposal-refund", raw_text=raw)
            ai, provider = service(c)
            provider.response = response(quote=tail)
            attempt = ai.prepare_initial(intake["public_id"], "proposal-refund")
        else:
            _, _, parent, *_ = subject(c, "proposal-refund")
            ai, provider = service(c)
            review = amendment(c, ai).prepare(parent["public_id"])
            text = word + " change amount to SGD 5.80"
            reply = persist_interpretation_reply(c, review, text)
            provider.response = response("patch", {"amount": "5.80"}, "SGD 5.80")
            attempt = ai.prepare_edit(review.review_id, reply, "proposal-refund")
    try:
        _refusal(c, ai, provider, attempt)
        validation = json.loads(
            c.execute(
                "SELECT validation_json FROM independent_ai_results WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()[0]
        )
        assert "unsupported_intent" in validation.get("text_readiness", {}).get("restrictions", [])
    finally:
        if role == "ocr":
            c.close()


def _context_response(raw, *, explicit_currency=False):
    parsed = json.loads(
        response(fields={**ORIGINAL, "amount": "5.80", "transaction_date": "1970-01-01"}, quote=raw)
    )
    parsed["evidence"]["transaction_date"] = {"ref": "source_context", "quote": "1970-01-01"}
    if not explicit_currency:
        parsed["evidence"]["currency"] = {"ref": "source_context", "quote": "SGD"}
    return json.dumps(parsed)


@pytest.mark.parametrize(
    "role,cue",
    [
        ("initial", "yesterday"),
        ("initial", "October eighth"),
        ("initial-reverse", "yesterday"),
        ("initial-reverse", "October eighth"),
        ("initial-reverse", "October ninth"),
        ("default", "yesterday"),
        ("default", "October eighth"),
        ("edit", "yesterday"),
        ("edit", "October eighth"),
        ("omit-date", "October eighth"),
        ("omit-date", "March fourth"),
        ("omit-date-reverse", "March fourth"),
    ],
)
def test_proposal_date_alternative_cannot_be_selected_away(migrated_temp_db_connection, role, cue):
    c = migrated_temp_db_connection
    if role in ("initial", "initial-reverse", "default"):
        supported = "Lunch SGD 12.50 at Example Cafe 2026-10-08"
        if role == "initial-reverse":
            raw = "Lunch SGD 12.50 at Example Cafe " + cue + " or 2026-10-08"
        else:
            raw = (
                supported + " or " + cue
                if role == "initial"
                else "Coffee SGD 5.80 yesterday or " + cue
            )
        _, _, _, intake, *_ = _prepare_text_subject(c, suffix="proposal-date", raw_text=raw)
        ai, provider = service(c)
        provider.response = (
            response(quote=supported if role == "initial" else raw)
            if role != "default"
            else _context_response(raw, explicit_currency=True)
        )
        attempt = ai.prepare_initial(intake["public_id"], "proposal-date")
    else:
        _, _, parent, *_ = subject(c, "proposal-date")
        ai, provider = service(c)
        review = amendment(c, ai).prepare(parent["public_id"])
        text = (
            "Change date to " + cue + " or 2026-10-09"
            if role == "omit-date-reverse"
            else "Change date to 2026-10-09 or " + cue
        )
        if role.startswith("omit-date"):
            text += "; change description to New lunch"
        reply = persist_interpretation_reply(c, review, text)
        patch = (
            {"description": "New lunch"}
            if role.startswith("omit-date")
            else {"transaction_date": "2026-10-09"}
        )
        provider.response = response("patch", patch, text)
        attempt = ai.prepare_edit(review.review_id, reply, "proposal-date")
    _refusal(c, ai, provider, attempt)
    if role != "default":
        validation = json.loads(
            c.execute(
                "SELECT validation_json FROM independent_ai_results WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()[0]
        )
        assert "ambiguous_date_alternative" in validation["text_readiness"]["restrictions"]
        assert any("transaction_date" in q["fields"] for q in validation["unresolved"])


@pytest.mark.parametrize(
    "raw,fields,context",
    [
        ("Lunch SGD 12.50 at Example Cafe 2026-10-08", ORIGINAL, None),
        (
            "我在咖啡店花了 SGD 5.80 2026-10-08",
            {**ORIGINAL, "amount": "5.80", "merchant": "咖啡店", "description": "咖啡"},
            None,
        ),
        ("Lunch SGD 12.50 at May Cafe 2026-10-08", {**ORIGINAL, "merchant": "May Cafe"}, None),
        (
            "Lunch SGD 12.50 at Cafe 2026-10-08 October special",
            {**ORIGINAL, "merchant": "Cafe", "description": "October special"},
            None,
        ),
        ("Coffee SGD 5.80 at Cafe", None, "date"),
        ("Coffee 5.80 at Cafe", None, "both"),
        ("咖啡 5.80", None, "both"),
    ],
)
def test_proposal_ordinary_text_and_true_context_absence_remain_eligible(
    migrated_temp_db_connection, raw, fields, context
):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = _prepare_text_subject(c, suffix="proposal-positive", raw_text=raw)
    ai, provider = service(c)
    provider.response = (
        response(fields=fields, quote=raw)
        if context is None
        else _context_response(raw, explicit_currency=context == "date")
    )
    attempt = ai.prepare_initial(intake["public_id"], "proposal-positive")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    published = ai.publish(attempt.attempt_id)
    assert published.state == "PUBLISHED" and provider.calls == 1
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    if raw == "Lunch SGD 12.50 at Example Cafe 2026-10-08":
        owner = posting(c, ai)
        review = owner.prepare(published.proposal_public_id)
        _persist_fresh_posting_confirmation(
            c, review, published.proposal_public_id, "proposal-positive-confirm"
        )
        final = owner.submit_post(review.review_id, "proposal-positive-confirm")
        assert final.state == "finalized"
        assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (
            12.5,
            "SGD",
        )
    assert ai.get_status(attempt.attempt_id).state == "PUBLISHED"


@pytest.mark.parametrize(
    "kind,state", [("unsupported", "RESPONSE_REJECTED"), ("clarification", "NEEDS_CLARIFICATION")]
)
def test_proposal_model_nonexpense_result_is_not_authority(
    migrated_temp_db_connection, kind, state
):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "proposal-model-control")
    ai, provider = service(c)
    reason = (
        "Refund posting is unsupported"
        if kind == "unsupported"
        else "Which expense date did you mean?"
    )
    raw = json.dumps({"schema": AI_RESULT_SCHEMA, "kind": kind, "reason": reason})
    provider.response = raw
    attempt = ai.prepare_initial(intake["public_id"], "proposal-model-control")
    _refusal(
        c,
        ai,
        provider,
        attempt,
        state=state,
        reason="unsupported_intent" if kind == "unsupported" else "source_ambiguity",
    )
    from independent_ai_candidate_fixture_v2 import candidate_fixture_v2

    assert c.execute("SELECT raw_response FROM independent_ai_results").fetchone()[
        0
    ] == candidate_fixture_v2(raw, provider.requests[0])


def test_proposal_real_total_with_non_total_numbers_remains_eligible(tmp_path):
    c, ai, provider, _, intake, *_ = _review_fix_receipt(
        tmp_path,
        extra_amounts=(
            ("SUBTOTAL", "15.00"),
            ("TAX", "0.00"),
            ("CASH", "20.00"),
            ("DISCOUNT", "-2.50"),
        ),
    )
    try:
        provider.response = response(
            fields={**ORIGINAL, "merchant": "EXAMPLE CAFE"}, quote=_source_money_quote(c)
        )
        attempt = ai.prepare_initial(intake, "proposal-total-positive")
        assert ai.invoke(attempt.attempt_id).state == "VALID"
        assert ai.publish(attempt.attempt_id).state == "PUBLISHED"
        assert (
            provider.calls == 1
            and c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        )
    finally:
        c.close()


@pytest.mark.parametrize(
    "text,patch",
    [
        ("Change date to 2026-10-09", {"transaction_date": "2026-10-09"}),
        ("Change description to October special", {"description": "October special"}),
        ("Change amount to SGD 5.80", {"amount": "5.80"}),
    ],
)
def test_proposal_ordinary_material_or_description_edit_remains_eligible(
    migrated_temp_db_connection, text, patch
):
    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "proposal-edit-positive")
    ai, provider = service(c)
    review = amendment(c, ai).prepare(parent["public_id"])
    reply = persist_interpretation_reply(c, review, text)
    provider.response = response("patch", patch, text)
    attempt = ai.prepare_edit(review.review_id, reply, "proposal-edit-positive")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    assert ai.publish(attempt.attempt_id).state == "PUBLISHED"
    assert provider.calls == 1 and c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 0
