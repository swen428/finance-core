"""Explicit migration of unpublished v1 fixtures to actual v2 model wire.

This is synthetic test generation, not a Core compatibility adapter. Original
source/reply strings and financial ambiguity remain in the actual v2 request.
Direct new product acceptance tests construct v2 without this conversion.
"""

from __future__ import annotations

import json

from finance_core.application.ai_invocation_contract import AI_RESULT_SCHEMA
from finance_core.parser_proposals.independent_ai_validation import canonical_text


def candidate_fixture_v2(raw, request):
    try:
        old = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(old, dict) or old.get("kind") not in {
        "initial",
        "patch",
        "clarification",
        "unsupported",
    }:
        return raw
    p = request["input"]
    if p.get("schema") != "finance-independent-ai-input-v2":
        return raw
    texts = {
        "source": p["source_text"],
        "reply": p["reply_text"],
        "context": canonical_text(p["current_values"]),
        "source_context": canonical_text(p["interpretation_context"]),
    }
    ref = "source" if p["interaction_kind"] == "initial" else "reply"
    text = texts[ref]
    if old["kind"] in {"clarification", "unsupported"}:
        if set(old) != {"schema", "kind", "reason"}:
            return raw
        evidence = {
            "ref": ref,
            "start": 0,
            "end": len(text),
            "quote": text,
            "role": "replacement_value",
        }
        question = {
            "question_id": "fixture_question",
            "fields": [
                "amount",
                "currency",
                "transaction_date",
                "merchant",
                "description",
                "category",
            ],
            "reason": "ambiguous",
            "candidates": [],
            "evidence": [evidence],
        }
        return json.dumps(
            {
                "schema": AI_RESULT_SCHEMA,
                "outcome": "clarification" if old["kind"] == "clarification" else "unsupported",
                "intent": "personal_expense" if old["kind"] == "clarification" else "unknown",
                "operations": [],
                "coverage": [
                    {
                        "start": 0,
                        "end": len(text),
                        "label": "unresolved_instruction"
                        if old["kind"] == "clarification"
                        else "non_instruction",
                        "operation_ids": [],
                        "question_ids": ["fixture_question"]
                        if old["kind"] == "clarification"
                        else [],
                    }
                ],
                "unresolved": [question] if old["kind"] == "clarification" else [],
            }
        )
    if (
        set(old) != {"schema", "kind", "fields", "evidence"}
        or not isinstance(old["fields"], dict)
        or not isinstance(old["evidence"], dict)
        or set(old["fields"]) != set(old["evidence"])
    ):
        return raw
    ops = []
    for i, (field, value) in enumerate(old["fields"].items()):
        evidence = old["evidence"][field]
        if not isinstance(evidence, dict) or set(evidence) != {"ref", "quote"}:
            return raw
        cite_ref = evidence["ref"]
        quote = evidence["quote"]
        if cite_ref not in texts or not isinstance(quote, str):
            return raw
        actual = texts[cite_ref]
        if cite_ref == "source_context":
            # Default-context authority binds the actual trusted context, not
            # the old fixture's cached default string.
            expected = p["interpretation_context"][
                "local_date" if field == "transaction_date" else "default_currency"
            ]
            quote = value if value == expected else evidence["quote"]
        if field == "transaction_date" and isinstance(value, str) and value in actual:
            quote = value
        start = actual.find(quote)
        if start < 0:
            return raw
        cite = {
            "ref": cite_ref,
            "start": start,
            "end": start + len(quote),
            "quote": quote,
            "role": "retained_value" if cite_ref == "context" else "replacement_value",
        }
        op = {
            "operation_id": "fixture_op_" + str(i),
            "op": "set",
            "field": field,
            "evidence": [cite],
        }
        if field == "transaction_date":
            try:
                y, m, d = map(int, value.split("-"))
            except (ValueError, TypeError, AttributeError):
                return raw
            op["date_expression"] = {"kind": "absolute_date", "year": y, "month": m, "day": d}
        else:
            op["value"] = value
        ops.append(op)
    return json.dumps(
        {
            "schema": AI_RESULT_SCHEMA,
            "outcome": "proposal",
            "intent": "personal_expense",
            "operations": ops,
            "coverage": [
                {
                    "start": 0,
                    "end": len(text),
                    "label": "resolved_instruction",
                    "operation_ids": [op["operation_id"] for op in ops],
                    "question_ids": [],
                }
            ],
            "unresolved": [],
        }
    )
