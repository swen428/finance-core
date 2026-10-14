"""Corrections retain actual independent AI and human-amendment source authority."""

from __future__ import annotations

import sqlite3

import pytest

from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.ai_invocation_contract import InterpretationPolicy
from finance_core.application.independent_corrections import IndependentCorrectionService
from finance_core.application.posting import PostingService
from tests.test_application_amendment_v1 import _apply, _persist_fresh_posting_confirmation
from tests.test_application_posting_recovery_v1 import BINDING, NOW, _prepare_receipt_subject
from tests.test_independent_ai_history_v1 import _tamper
from tests.test_independent_ai_invocation_v1 import ORIGINAL, DurableProvider, posting
from tests.test_independent_ai_v2 import (
    InputV2,
    _v2_rule_amendment_service,
    cite,
    published_text,
    wire,
)
from tests.test_independent_corrections_v1 import (
    _persist_correction_approval,
    _SyntheticCorrectionDecisionAuthority,
)


def _correction_owner(owner, ai):
    authority = _SyntheticCorrectionDecisionAuthority()
    coupled = PostingService(
        connection=owner._conn,
        source_verifier=owner._source_port,
        human_decision_authority=owner._decision_port,
        binding=owner._binding,
        clock=lambda: NOW + 5,
        ai_lineage_authority=ai,
        correction_decision_authority=authority,
    )
    return coupled, IndependentCorrectionService(coupled, human_decision_authority=authority)


def _post_and_correct(c, ai, public_id, owner, identity):
    review = owner.prepare(public_id)
    _persist_fresh_posting_confirmation(c, review, public_id, identity + "-post")
    posted = owner.submit_post(review.review_id, identity + "-post")
    corrected_owner, service = _correction_owner(owner, ai)
    original_transaction = dict(
        c.execute(
            "SELECT * FROM transactions WHERE public_id=?",
            (posted.transaction_public_id,),
        ).fetchone()
    )
    plan = service.prepare_correction(
        posted.transaction_public_id,
        {"category": "travel"},
        "Correct whole expense category",
    )
    decision_id = _persist_correction_approval(c, plan, decision_id=identity + "-correction")
    result = service.submit_correction(plan.plan_id, decision_id)
    status = corrected_owner.resume_post(posted.attempt_id)
    assert status.applied.version == 0
    assert status.current == result.current
    assert status.current.version == 1 and status.current.fields.category == "travel"
    assert (
        dict(
            c.execute(
                "SELECT * FROM transactions WHERE public_id=?",
                (posted.transaction_public_id,),
            ).fetchone()
        )
        == original_transaction
    )
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM application_correction_versions").fetchone()[0] == 1
    return corrected_owner, service, posted, result


def test_ai_v2_text_human_amendment_then_correction_preserves_old_resume(
    migrated_temp_db_connection,
):
    c = migrated_temp_db_connection
    ai, provider, published = published_text(c, "correction-ai-human-text")
    amendment = _v2_rule_amendment_service(c, ai)
    changed = _apply(
        c,
        amendment,
        amendment.prepare(published.proposal_public_id),
        {"description": "Human expense note"},
        evidence_id="correction-human-text-evidence",
        amendment_id="correction-human-text-amendment",
    )
    _, service, _, result = _post_and_correct(
        c,
        ai,
        changed.proposal_public_id,
        posting(c, ai),
        "lineage-text",
    )
    assert result.current.fields.description == "Human expense note"
    assert result.current.original_source.fields.description == "Human expense note"
    assert service.read_effective(result.current.target_id) == result.current
    assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 1
    assert provider.calls == 1


def test_ai_v2_receipt_human_amendment_then_correction_has_full_new_fact(tmp_path):
    c, _, _, owner, _, _, intake, _, _, source_port, decision_port = _prepare_receipt_subject(
        tmp_path,
        suffix="correction-ai-human-receipt",
        ocr_confidence_scaled=3000,
        prepare_review=False,
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
            source_verifier=source_port,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        initial = ai.prepare_initial(intake, "correction-ai-receipt-op")
        assert ai.invoke(initial.attempt_id).state == "VALID"
        published = ai.publish(initial.attempt_id)
        amendment = _v2_rule_amendment_service(c, ai, source_verifier=source_port)
        changed = _apply(
            c,
            amendment,
            amendment.prepare(published.proposal_public_id),
            {"description": "Human receipt note"},
            evidence_id="correction-human-receipt-evidence",
            amendment_id="correction-human-receipt-amendment",
        )
        owner = PostingService(
            connection=c,
            binding=BINDING,
            source_verifier=source_port,
            human_decision_authority=decision_port,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        _, _, _, result = _post_and_correct(
            c,
            ai,
            changed.proposal_public_id,
            owner,
            "lineage-receipt",
        )
        assert result.current.fields.description == "Human receipt note"
        assert result.current.fields.amount == result.applied.original_source.fields.amount
        assert result.current.snapshot_id != result.current.original_source.snapshot_id
        assert c.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert (
            c.execute("SELECT count(*) FROM receipt_item_allocation_fact_sets").fetchone()[0] == 1
        )
        assert (
            c.execute("SELECT count(*) FROM application_correction_receipt_facts").fetchone()[0]
            == 1
        )
        assert (
            c.execute("SELECT count(*) FROM authoritative_calculation_snapshots").fetchone()[0] == 2
        )
        assert provider.calls == 1
    finally:
        c.close()


@pytest.mark.parametrize("corruption", ["missing_port", "tampered_provider_history"])
def test_corrected_ai_source_never_falls_back_when_original_lineage_is_unprovable(
    migrated_temp_db_connection: sqlite3.Connection,
    corruption: str,
):
    c = migrated_temp_db_connection
    ai, provider, published = published_text(c, "correction-ai-refuse-" + corruption)
    coupled, service, posted, result = _post_and_correct(
        c,
        ai,
        published.proposal_public_id,
        posting(c, ai),
        "lineage-refusal",
    )
    if corruption == "missing_port":
        coupled, service = _correction_owner(coupled, None)
    else:
        _tamper(c, "synthetic_ai_receipts", "UPDATE synthetic_ai_receipts SET signature='corrupt'")
    counts = tuple(
        c.execute("SELECT count(*) FROM " + table).fetchone()[0]
        for table in (
            "transactions",
            "application_correction_versions",
            "financial_audit_events",
        )
    )
    with pytest.raises(ValueError):
        service.read_effective(posted.transaction_public_id)
    with pytest.raises(ValueError):
        coupled.resume_post(posted.attempt_id)
    assert (
        tuple(
            c.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in (
                "transactions",
                "application_correction_versions",
                "financial_audit_events",
            )
        )
        == counts
    )
    assert result.current.version == 1 and provider.calls == 1


def test_amount_correction_preserves_legal_original_text_exactly(migrated_temp_db_connection):
    from tests.test_application_posting_recovery_v1 import _prepare_text_subject

    c = migrated_temp_db_connection
    description = "午餐" * 300 + "\nCafe\u0301"
    owner, review, _, _, decision, _ = _prepare_text_subject(
        c,
        "correction-retained-text",
        description=description,
        category="  food  ",
    )
    accepted_description = review.projection["financial_projection"]["description"]
    posted = owner.submit_post(review.review_id, decision)
    assert c.execute("SELECT description FROM transactions").fetchone()[0] == accepted_description
    _, service = _correction_owner(owner, None)
    plan = service.prepare_correction(
        posted.transaction_public_id, {"amount": "16.50"}, "Correct total"
    )
    assert plan.projection["before"]["description"] == accepted_description
    assert plan.projection["after"]["description"] == accepted_description
    assert plan.projection["after"]["category"] == "  food  "
    decision_id = _persist_correction_approval(c, plan, decision_id="retained-text-correction")
    result = service.submit_correction(plan.plan_id, decision_id)
    assert result.current.fields.description == accepted_description
    assert result.current.fields.category == "  food  "
    assert result.current.fields.amount == "16.50"


def test_actual_correction_neutral_candidate_reader_uses_owning_snapshot_only(
    migrated_temp_db_connection,
):
    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    from finance_core.reconciliation.adapter import CandidateFilter, InternalCandidateAdapter
    from tests.conftest import connect_temp_db
    from tests.test_application_posting_recovery_v1 import _prepare_text_subject

    c = migrated_temp_db_connection
    owner, review, _, _, decision, _ = _prepare_text_subject(c, "correction-candidate-seam")
    posted = owner.submit_post(review.review_id, decision)
    _, service = _correction_owner(owner, None)
    patch = {
        "amount": "21.25",
        "currency": "USD",
        "transaction_date": "2026-10-11",
        "merchant": "Harbour Market",
    }
    plan = service.prepare_correction(
        posted.transaction_public_id, patch, "Correct expense details"
    )
    approval = _persist_correction_approval(c, plan, decision_id="candidate-seam-correction")
    applied = service.submit_correction(plan.plan_id, approval)
    adapter = InternalCandidateAdapter(c, effective_reader=service.read_effective_in_snapshot)
    filters = CandidateFilter(
        currency="USD",
        date_from="2026-10-11",
        date_to="2026-10-11",
        merchant_like="Harbour%",
    )
    candidates = adapter.fetch_candidates(filters)
    assert len(candidates) == adapter.candidate_count(filters) == 1
    assert candidates[0].internal_id == posted.transaction_public_id
    assert candidates[0].amount == Decimal("21.25")
    assert candidates[0].transaction_date == date(2026, 10, 11)
    assert candidates[0].currency == "USD" and candidates[0].merchant == "Harbour Market"
    assert adapter.candidate_count(CandidateFilter(currency="SGD")) == 0
    assert adapter.candidate_count(CandidateFilter(date_to="2026-10-10")) == 0
    assert adapter.candidate_count(CandidateFilter(merchant_like="Example%")) == 0
    with pytest.raises(ValueError, match="trusted_effective_reader"):
        InternalCandidateAdapter(c).fetch_candidates(filters)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT amount,currency,merchant FROM transactions").fetchone()[1:] == (
        "SGD",
        "Example Cafe",
    )
    assert not c.in_transaction
    with pytest.raises(ValueError, match="owning snapshot"):
        service.read_effective_in_snapshot(c, posted.transaction_public_id)

    c.execute("CREATE TABLE synthetic_candidate_snapshot_guard(value TEXT)")
    c.commit()
    c.execute("BEGIN")
    c.execute("INSERT INTO synthetic_candidate_snapshot_guard VALUES('pending caller work')")
    assert service.read_effective_in_snapshot(c, posted.transaction_public_id) == applied.current
    assert adapter.candidate_count(filters) == 1
    assert c.in_transaction
    assert (
        c.execute("SELECT value FROM synthetic_candidate_snapshot_guard").fetchone()[0]
        == "pending caller work"
    )
    c.rollback()
    assert c.execute("SELECT count(*) FROM synthetic_candidate_snapshot_guard").fetchone()[0] == 0

    foreign = connect_temp_db(Path(c.execute("PRAGMA database_list").fetchone()[2]))
    try:
        with pytest.raises(ValueError, match="owning snapshot"):
            InternalCandidateAdapter(
                foreign, effective_reader=service.read_effective_in_snapshot
            ).fetch_candidates()
    finally:
        foreign.close()
    impostor = sqlite3.connect(":memory:")
    try:
        impostor.execute("CREATE TABLE transactions(public_id TEXT)")
        impostor.execute("INSERT INTO transactions VALUES(?)", (posted.transaction_public_id,))
        assert impostor.in_transaction
        with pytest.raises(ValueError, match="owning snapshot"):
            service.read_effective_in_snapshot(impostor, posted.transaction_public_id)
        assert impostor.in_transaction
    finally:
        impostor.close()
