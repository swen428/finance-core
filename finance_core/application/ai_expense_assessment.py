"""One complete v2 card assessment used by prepare, accept and recovery."""

from __future__ import annotations

import json
from typing import Any

from finance_core.application.ai_invocation_contract import AI_INPUT_SCHEMA, AiInvocationError
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.independent_ai_lineage import verify_independent_ai_lineage
from finance_core.parser_proposals.independent_ai_v2 import _canonical_fields
from finance_core.parser_proposals.independent_ai_validation import sha

BASIS = "full_card_ai_expense_v1"


def assess_independent_ai_expense_v2(conn, proposal, source) -> dict[str, Any] | None:
    payload, _, version = resolve_effective_payload(conn, proposal)
    digest = compute_effective_proposal_content_hash(conn, proposal)
    lineage = verify_independent_ai_lineage(
        conn, proposal, content_hash=digest, proposal_version=version, require_resolved=False
    )
    if lineage is None:
        return None
    attempts = []
    for identity in lineage["attempt_ids"]:
        row = conn.execute(
            "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?", (identity,)
        ).fetchone()
        if row is None:
            raise AiInvocationError("Expense assessment invocation absent")
        attempts.append(json.loads(row[0]))
    if not any(m["schema"] == AI_INPUT_SCHEMA for m in attempts):
        return None
    fields = _canonical_fields(payload, complete=True)
    blockers = []
    if (
        payload.get("intent") != "personal_expense_log"
        or payload.get("transaction_type") != "personal_expense"
        or any(
            payload.get(k) is not None
            for k in ("account", "account_id", "payer", "paid_by", "participants", "ledger")
        )
    ):
        blockers.append("unsupported_scope")
    latest = attempts[0]
    before = _canonical_fields(latest["projection"]["current_values"], complete=False)
    changes = [
        {"field": field, "before": before.get(field), "after": fields[field]}
        for field in fields
        if before.get(field) != fields[field]
    ]
    change_summary = {
        "before": before,
        "after": fields,
        "changes": changes,
        "interaction_kind": latest["projection"].get("interaction_kind", "initial"),
    }
    # Provider/input authentication is separately required by the fixed
    # composition at every owning operation; static SQL never replaces it.
    warnings = sorted(set(payload.get("ambiguity_flags", [])))
    if proposal["confidence_score"] is None or float(proposal["confidence_score"]) < 0.8:
        warnings.append("historical_low_ocr_or_parser_confidence")
    return {
        "basis": BASIS,
        "proposal_public_id": proposal["public_id"],
        "proposal_version": version,
        "effective_content_hash": digest,
        "canonical_fields": fields,
        "hard_blockers": blockers,
        "review_warnings": sorted(set(warnings)),
        "source_and_interpretation_digest": sha(
            {"source": source, "attempts": attempts, "fields": fields}
        ),
        "assessment_policy_version": "finance-independent-ai-expense-assessment-v2",
        "change_summary": change_summary,
    }
