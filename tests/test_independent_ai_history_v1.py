"""Actual immutable revision history, distinct from live AI write authority."""

from __future__ import annotations

import json

import pytest
from test_application_amendment_v1 import _apply
from test_independent_ai_invocation_v1 import (
    ORIGINAL,
    _review_fix_receipt,
    amendment,
    persist_interpretation_reply,
    posting,
    publish_initial,
    response,
    service,
    subject,
)

from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.parser_proposals.amendment_lineage import verify_amendment_record


def _owner_rows(c):
    tables = (
        "independent_ai_attempts",
        "independent_ai_claims",
        "independent_ai_results",
        "independent_ai_events",
        "independent_ai_publications",
        "application_amendment_records",
        "application_posting_decisions",
        "transactions",
        "receipts",
        "receipt_item_allocation_fact_sets",
        "authoritative_calculation_snapshots",
        "parser_proposal_completions",
        "application_amendment_reviews",
        "application_amendment_invalidations",
        "parser_proposal_events",
        "financial_audit_events",
        "parser_outputs",
        "raw_intake_records",
        "synthetic_ai_receipts",
    )
    return {table: [tuple(row) for row in c.execute("SELECT * FROM " + table)] for table in tables}


def _human(c, ai, public_id, number, patch=None):
    owner = amendment(c, ai)
    return _apply(
        c,
        owner,
        owner.prepare(public_id),
        patch or {"description": "Human revision " + str(number)},
        evidence_id="history-human-" + str(number),
        amendment_id="history-human-" + str(number),
    )


def _strict_live_refused(c, ai, provider, identity):
    receipt = c.execute(
        "SELECT observation_record_id FROM independent_ai_results WHERE attempt_id=?", (identity,)
    ).fetchone()[0]
    before = _owner_rows(c)
    calls = provider.calls
    for operation in (
        lambda: ai.claim(identity),
        lambda: ai.invoke(identity),
        lambda: ai.record_outcome(identity, receipt),
        lambda: ai.publish(identity),
    ):
        with pytest.raises(AiInvocationError):
            operation()
        assert _owner_rows(c) == before
        assert provider.calls == calls


def test_history_initial_actual_v0_to_v1_stale_and_live_refusal(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, parent, intake, *_ = subject(c, "history-initial")
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "history-initial")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    accepted = _human(c, ai, parent["public_id"], 1)
    row = c.execute("SELECT * FROM application_amendment_records").fetchone()
    actual = verify_amendment_record(c, row)
    original = json.loads(c.execute("SELECT parsed_payload FROM parser_outputs").fetchone()[0])
    assert actual["base_version"] == 0 and actual["resulting_version"] == 1
    assert json.loads(actual["base_payload_json"]) == original
    assert accepted.proposal_public_id == parent["public_id"]
    before = _owner_rows(c)
    assert ai.get_status(attempt.attempt_id).state == "RESULT_STALE"
    assert ai.recover(attempt.attempt_id).state == "RESULT_STALE"
    assert _owner_rows(c) == before and provider.calls == 1
    _strict_live_refused(c, ai, provider, attempt.attempt_id)


def _cancel_all_rows(c):
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return {
        name: [tuple(row) for row in c.execute('SELECT * FROM "' + name + '"')]
        for (name,) in tables
    }


def _cancel_subject(c, phase):
    ai, provider, published, *_ = publish_initial(c, "cancel-atomic")
    review = amendment(c, ai).prepare(published.proposal_public_id)
    reply = "Change merchant to Cafe Two"
    evidence = persist_interpretation_reply(c, review, reply, identity="cancel-atomic-reply")
    provider.response = response("patch", {"merchant": "Cafe Two"}, reply)
    attempt = ai.prepare_edit(review.review_id, evidence, "cancel-atomic-edit")
    if phase == "claimed":
        ai.claim(attempt.attempt_id)
    elif phase == "result":
        assert ai.invoke(attempt.attempt_id).state == "VALID"
    return ai, provider, attempt, published, review, evidence


@pytest.mark.parametrize(
    "phase,corruption",
    [
        ("prepared", "ancestor"),
        ("claimed", "ancestor"),
        ("result", "own-provider"),
        ("result", "own-response"),
        ("claimed", "own-claim"),
    ],
)
def test_cancel_corrupt_owner_evidence_is_atomic(migrated_temp_db_connection, phase, corruption):
    c = migrated_temp_db_connection
    ai, provider, attempt, ancestor, *_ = _cancel_subject(c, phase)
    if corruption in ("ancestor", "own-provider"):
        target = ancestor.attempt_id if corruption == "ancestor" else attempt.attempt_id
        receipt = c.execute(
            "SELECT observation_record_id FROM independent_ai_results WHERE attempt_id=?", (target,)
        ).fetchone()[0]
        c.execute("UPDATE synthetic_ai_receipts SET signature='invalid' WHERE id=?", (receipt,))
        c.commit()
    elif corruption == "own-response":
        _tamper(
            c,
            "independent_ai_results",
            "UPDATE independent_ai_results SET raw_response=raw_response||' ' WHERE attempt_id=?",
            (attempt.attempt_id,),
        )
    else:
        _tamper(
            c,
            "independent_ai_claims",
            "UPDATE independent_ai_claims SET material_hash=? WHERE attempt_id=?",
            ("0" * 64, attempt.attempt_id),
        )
    before = _cancel_all_rows(c)
    calls = provider.calls
    with pytest.raises(ValueError):
        ai.get_status(attempt.attempt_id)
    assert _cancel_all_rows(c) == before
    with pytest.raises(ValueError):
        ai.cancel(attempt.attempt_id)
    assert _cancel_all_rows(c) == before
    assert provider.calls == calls
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("phase", ["prepared", "claimed"])
def test_cancel_healthy_replay_preserves_committed_claim_and_no_retry(
    migrated_temp_db_connection, phase
):
    c = migrated_temp_db_connection
    ai, provider, attempt, _, review, evidence = _cancel_subject(c, phase)
    claims = [tuple(r) for r in c.execute("SELECT * FROM independent_ai_claims")]
    calls = provider.calls
    cancelled = ai.cancel(attempt.attempt_id)
    assert cancelled.state == ("CANCELLED" if phase == "prepared" else "UNKNOWN")
    if phase == "claimed":
        assert cancelled.reason == "cancel_requested"
    before = _cancel_all_rows(c)
    assert ai.cancel(attempt.attempt_id) == ai.invoke(attempt.attempt_id) == cancelled
    with pytest.raises(AiInvocationError):
        ai.prepare_edit(review.review_id, evidence, "cancel-atomic-retry")
    assert _cancel_all_rows(c) == before and provider.calls == calls
    assert [tuple(r) for r in c.execute("SELECT * FROM independent_ai_claims")] == claims
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("phase", ["prepared", "claimed"])
def test_cancel_verification_failure_after_local_event_rolls_back(
    migrated_temp_db_connection, monkeypatch, phase
):
    c = migrated_temp_db_connection
    ai, provider, attempt, ancestor, *_ = _cancel_subject(c, phase)
    receipt = c.execute(
        "SELECT observation_record_id FROM independent_ai_results WHERE attempt_id=?",
        (ancestor.attempt_id,),
    ).fetchone()[0]
    before = _cancel_all_rows(c)
    calls = provider.calls
    original_event = ai._event
    observed = []

    def event_then_retained_proof_fault(identity, kind, now):
        original_event(identity, kind, now)
        observed.append(
            (
                c.in_transaction,
                c.execute(
                    "SELECT count(*) FROM independent_ai_events WHERE attempt_id=?", (identity,)
                ).fetchone()[0],
            )
        )
        c.execute(
            "UPDATE synthetic_ai_receipts SET signature='late-precommit-fault' WHERE id=?",
            (receipt,),
        )

    monkeypatch.setattr(ai, "_event", event_then_retained_proof_fault)
    with pytest.raises(ValueError, match="Invalid durable synthetic evidence"):
        ai.cancel(attempt.attempt_id)
    assert observed and all(in_transaction and count > 0 for in_transaction, count in observed)
    assert _cancel_all_rows(c) == before and provider.calls == calls
    assert ai.get_status(attempt.attempt_id).state == (
        "PREPARED" if phase == "prepared" else "UNKNOWN"
    )


def test_history_new_physical_human_supersession(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, parent, intake, *_ = subject(c, "history-physical")
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "history-physical")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    replacement = _human(c, ai, parent["public_id"], 1, {"amount": "8.25"})
    assert replacement.proposal_public_id != parent["public_id"]
    before = _owner_rows(c)
    assert ai.get_status(attempt.attempt_id).state == "RESULT_STALE"
    assert ai.recover(attempt.attempt_id).state == "RESULT_STALE"
    assert _owner_rows(c) == before
    _strict_live_refused(c, ai, provider, attempt.attempt_id)


@pytest.mark.parametrize("mode", ["prepared", "unknown", "cancel-requested", "cancelled"])
def test_history_four_unresolved_states_no_stale_recovery_writes(migrated_temp_db_connection, mode):
    c = migrated_temp_db_connection
    _, _, parent, intake, *_ = subject(c, "history-" + mode)
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "history-" + mode)
    if mode in ("unknown", "cancel-requested"):
        ai.claim(attempt.attempt_id)
    if mode in ("cancel-requested", "cancelled"):
        ai.cancel(attempt.attempt_id)
    _human(c, ai, parent["public_id"], 1)
    expected = {
        "prepared": "RESULT_STALE",
        "unknown": "UNKNOWN",
        "cancel-requested": "UNKNOWN",
        "cancelled": "CANCELLED",
    }[mode]
    before = _owner_rows(c)
    assert ai.get_status(attempt.attempt_id).state == expected
    assert ai.recover(attempt.attempt_id).state == expected
    assert _owner_rows(c) == before and provider.calls == 0
    for operation in (ai.claim, ai.invoke, ai.publish):
        with pytest.raises(AiInvocationError):
            operation(attempt.attempt_id)
        assert _owner_rows(c) == before and provider.calls == 0


def test_history_published_child_completed_replay_and_actual_post(migrated_temp_db_connection):
    from test_application_amendment_v1 import _persist_fresh_posting_confirmation

    c = migrated_temp_db_connection
    ai, provider, published, *_ = publish_initial(c, "history-published")
    accepted = _human(c, ai, published.proposal_public_id, 1)
    assert accepted.proposal_public_id == published.proposal_public_id
    before = _owner_rows(c)
    historical = ai.get_status(published.attempt_id)
    assert historical.state == "PUBLISHED" and not historical.is_current
    assert historical.proposal_version == 0
    assert historical.effective_content_hash == published.effective_content_hash
    assert ai.publish(published.attempt_id) == ai.recover(published.attempt_id) == historical
    assert ai.claim(published.attempt_id) == ai.invoke(published.attempt_id) == historical
    assert _owner_rows(c) == before and provider.calls == 1
    owner = posting(c, ai)
    fresh = owner.prepare(accepted.proposal_public_id)
    _persist_fresh_posting_confirmation(c, fresh, accepted.proposal_public_id, "history-fresh-post")
    finalized = owner.submit_post(fresh.review_id, "history-fresh-post")
    assert finalized.state == "finalized"
    assert tuple(c.execute("SELECT amount,description FROM transactions").fetchone()) == (
        12.5,
        "Human revision 1",
    )
    assert ai.get_status(published.attempt_id) == historical
    assert provider.calls == 1


def test_history_pending_real_ocr_human_completion_without_ai_child(tmp_path):
    import sqlite3

    from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
    from test_application_amendment_v1 import _EDIT_BINDING, _persist_fresh_posting_confirmation
    from test_application_posting_recovery_v1 import BINDING, NOW
    from test_independent_ai_invocation_v1 import DurableInputVerifier, DurableProvider

    from finance_core.application.ai_invocation import AiInvocationService
    from finance_core.application.ai_invocation_contract import InterpretationPolicy
    from finance_core.application.amendment import AmendmentService
    from finance_core.application.posting import PostingError, PostingService
    from finance_core.staging_guard import open_staging_database

    c, ai, provider, parent, intake, source, decision = _review_fix_receipt(
        tmp_path,
        merchant="ABC",
        extra_amounts=(("SUBTOTAL", "10.00"), ("TAX", "2.50"), ("CASH", "20.00")),
    )
    try:
        database = c.execute("PRAGMA database_list").fetchone()[2]
        raw_before = [
            tuple(r)
            for r in c.execute(
                "SELECT public_id,raw_input,source_content_hash FROM raw_intake_records"
            )
        ]
        ocr_before = [tuple(r) for r in c.execute("SELECT * FROM receipt_ocr_blocks")]
        # Authentic model question keeps this history scenario pending; ABC is
        # a merchant, not an unsupported monetary code.
        provider.response = json.dumps(
            {
                "schema": "finance-independent-ai-result-v1",
                "kind": "clarification",
                "reason": "Which description and category?",
            }
        )
        attempt = ai.prepare_initial(intake, "history-pending")
        pending = ai.invoke(attempt.attempt_id)
        assert pending.state == "NEEDS_CLARIFICATION" and pending.reason == "source_ambiguity"
        owner = AmendmentService(
            connection=c,
            source_verifier=source,
            human_amendment_authority=DurableSyntheticAmendmentAuthority(),
            binding=_EDIT_BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        posting_owner = PostingService(
            connection=c,
            source_verifier=source,
            human_decision_authority=decision,
            binding=BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        old_review = posting_owner.prepare(parent)
        canonical = {**ORIGINAL, "merchant": "ABC", "description": "ABC lunch"}
        accepted = _apply(
            c,
            owner,
            owner.prepare(parent),
            canonical,
            evidence_id="history-pending",
            amendment_id="history-pending",
        )
        material = verify_amendment_record(
            c, c.execute("SELECT * FROM application_amendment_records").fetchone()
        )
        assert material["canonical_patch"] == canonical
        assert set(material["material_patch"]) == {"description", "category"}
        before = _owner_rows(c)
        assert (
            ai.get_status(attempt.attempt_id).state
            == ai.recover(attempt.attempt_id).state
            == "NEEDS_CLARIFICATION"
        )
        assert _owner_rows(c) == before and provider.calls == 1
        _persist_fresh_posting_confirmation(c, old_review, parent, "history-old-post")
        before = _owner_rows(c)
        with pytest.raises(PostingError):
            posting_owner.submit_post(old_review.review_id, "history-old-post")
        assert _owner_rows(c) == before
        fresh = posting_owner.prepare(accepted.proposal_public_id)
        _persist_fresh_posting_confirmation(
            c, fresh, accepted.proposal_public_id, "history-new-post"
        )
        final = posting_owner.submit_post(fresh.review_id, "history-new-post")
        assert final.state == "finalized"
        assert tuple(
            c.execute(
                "SELECT amount,currency,transaction_date,merchant,description,category "
                "FROM transactions"
            ).fetchone()
        ) == (12.5, "SGD", "2026-10-08", "ABC", "ABC lunch", "food")
        for table in (
            "transactions",
            "receipts",
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
            "application_posting_decisions",
        ):
            assert c.execute("SELECT count(*) FROM " + table).fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 0
        before = _owner_rows(c)
        c.commit()
        c.close()
        c = open_staging_database(database)
        c.row_factory = sqlite3.Row
        provider = DurableProvider(c)
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source,
            interpretation_input_verifier=DurableInputVerifier(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        posting_owner = PostingService(
            connection=c,
            source_verifier=source,
            human_decision_authority=decision,
            binding=BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        assert (
            posting_owner.get_status(final.attempt_id)
            == posting_owner.resume_post(final.attempt_id)
            == final
        )
        assert ai.get_status(attempt.attempt_id) == ai.recover(attempt.attempt_id) == pending
        with pytest.raises(PostingError):
            posting_owner.submit_post(old_review.review_id, "history-old-post")
        with pytest.raises(PostingError):
            posting_owner.submit_post(fresh.review_id, "history-new-post")
        assert _owner_rows(c) == before
        assert (
            provider.calls == 0
            and c.execute("SELECT count(*) FROM synthetic_ai_receipts").fetchone()[0] == 1
        )
        assert [
            tuple(r)
            for r in c.execute(
                "SELECT public_id,raw_input,source_content_hash FROM raw_intake_records"
            )
        ] == raw_before
        assert [tuple(r) for r in c.execute("SELECT * FROM receipt_ocr_blocks")] == ocr_before
    finally:
        c.close()


def _tamper(c, table, statement, parameters=()):
    """Offline corruption, restoring exact production schema before reads."""
    triggers = c.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for name, _ in triggers:
        c.execute('DROP TRIGGER "' + name + '"')
    c.execute(statement, parameters)
    for _, sql in triggers:
        c.execute(sql)
    c.commit()


@pytest.mark.parametrize(
    "corruption",
    [
        "raw",
        "provider",
        "response",
        "validation",
        "cached-base",
        "completion-gap",
        "completion-seal",
        "human-review",
        "invalidation",
        "audit",
        "edit-review",
        "reply",
    ],
)
def test_history_corruption_is_refusal_never_stale(migrated_temp_db_connection, corruption):
    from finance_core.calculation.authoritative_snapshot import canonical_json_text
    from finance_core.parser_proposals.independent_ai_validation import canonical_text, sha

    c = migrated_temp_db_connection
    _, _, parent, intake, *_ = subject(c, "history-tamper")
    ai, provider = service(c)
    if corruption in ("edit-review", "reply"):
        first = _human(c, ai, parent["public_id"], 1)
        review = amendment(c, ai).prepare(first.proposal_public_id)
        reply = "Change merchant to Cafe Two"
        evidence = persist_interpretation_reply(c, review, reply)
        provider.response = response("patch", {"merchant": "Cafe Two"}, reply)
        attempt = ai.prepare_edit(review.review_id, evidence, "history-tamper")
        assert ai.invoke(attempt.attempt_id).state == "VALID"
        _human(c, ai, parent["public_id"], 2)
    else:
        attempt = ai.prepare_initial(intake["public_id"], "history-tamper")
        assert ai.invoke(attempt.attempt_id).state == "VALID"
        _human(c, ai, parent["public_id"], 1)
    if corruption == "raw":
        _tamper(c, "raw_intake_records", "UPDATE raw_intake_records SET raw_input=raw_input||' '")
    elif corruption == "provider":
        _tamper(c, "synthetic_ai_receipts", "UPDATE synthetic_ai_receipts SET signature='invalid'")
    elif corruption == "response":
        _tamper(
            c,
            "independent_ai_results",
            "UPDATE independent_ai_results SET raw_response=raw_response||' '",
        )
    elif corruption == "validation":
        _tamper(
            c,
            "independent_ai_results",
            "UPDATE independent_ai_results SET validation_json=?",
            (canonical_text({"state": "VALID", "reason": "forged"}),),
        )
    elif corruption == "cached-base":
        material = json.loads(
            c.execute("SELECT material_json FROM independent_ai_attempts").fetchone()[0]
        )
        material["base_payload"]["description"] = "Fake cached description"
        _tamper(
            c,
            "independent_ai_attempts",
            "UPDATE independent_ai_attempts SET material_json=?,material_hash=?",
            (canonical_text(material), sha(material)),
        )
    elif corruption == "completion-gap":
        _tamper(
            c,
            "parser_proposal_completions",
            "UPDATE parser_proposal_completions SET version_number=3",
        )
    elif corruption == "completion-seal":
        _tamper(
            c,
            "application_amendment_records",
            "UPDATE application_amendment_records SET record_hash=?",
            ("0" * 64,),
        )
    elif corruption in ("human-review", "edit-review"):
        identity = (
            c.execute("SELECT review_id FROM application_amendment_records LIMIT 1").fetchone()[0]
            if corruption == "human-review"
            else review.review_id
        )
        retained = canonical_json_value(
            c.execute(
                "SELECT material_json FROM application_amendment_reviews WHERE review_id=?",
                (identity,),
            ).fetchone()[0]
        )
        retained["proposal_review"]["merchant"] = "Forged unchanged money review"
        _tamper(
            c,
            "application_amendment_reviews",
            "UPDATE application_amendment_reviews SET material_json=? WHERE review_id=?",
            (canonical_json_text(retained), identity),
        )
    elif corruption == "invalidation":
        _tamper(
            c,
            "application_amendment_invalidations",
            "DELETE FROM application_amendment_invalidations",
        )
    elif corruption == "audit":
        _tamper(
            c,
            "financial_audit_events",
            "UPDATE financial_audit_events SET event_payload_json=? "
            "WHERE event_type='parser_proposal_completed'",
            (canonical_json_text({"forged": True}),),
        )
    else:
        _tamper(
            c,
            "synthetic_interpretation_replies",
            "UPDATE synthetic_interpretation_replies SET signature='invalid'",
        )
    before = _owner_rows(c)
    for operation in (ai.get_status, ai.recover):
        with pytest.raises(ValueError):
            operation(attempt.attempt_id)
        assert _owner_rows(c) == before and provider.calls == 1


def test_history_original_ocr_corruption_refused_after_completion(tmp_path):
    from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
    from test_application_amendment_v1 import _EDIT_BINDING
    from test_application_posting_recovery_v1 import NOW

    from finance_core.application.amendment import AmendmentService

    c, ai, provider, parent, intake, source, _ = _review_fix_receipt(tmp_path, merchant="ABC")
    try:
        # Authentic model question keeps this history scenario pending; ABC is
        # a merchant, not an unsupported monetary code.
        provider.response = json.dumps(
            {
                "schema": "finance-independent-ai-result-v1",
                "kind": "clarification",
                "reason": "Which description and category?",
            }
        )
        attempt = ai.prepare_initial(intake, "history-ocr-corrupt")
        assert ai.invoke(attempt.attempt_id).state == "NEEDS_CLARIFICATION"
        owner = AmendmentService(
            connection=c,
            source_verifier=source,
            human_amendment_authority=DurableSyntheticAmendmentAuthority(),
            binding=_EDIT_BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        _apply(
            c,
            owner,
            owner.prepare(parent),
            {"description": "ABC lunch", "category": "food"},
            evidence_id="history-ocr-corrupt",
            amendment_id="history-ocr-corrupt",
        )
        _tamper(
            c,
            "receipt_ocr_blocks",
            "UPDATE receipt_ocr_blocks SET normalized_text='12.50 altered' WHERE sequence_index=4",
        )
        before = _owner_rows(c)
        for operation in (ai.get_status, ai.recover):
            with pytest.raises(ValueError):
                operation(attempt.attempt_id)
            assert _owner_rows(c) == before and provider.calls == 1
    finally:
        c.close()


def test_history_edit_actual_v1_to_v2_uses_old_card(migrated_temp_db_connection):
    from test_application_posting_recovery_v1 import BINDING, NOW, _DurableSourceVerifier
    from test_independent_ai_invocation_v1 import DurableInputVerifier, DurableProvider

    from finance_core.application.ai_invocation import AiInvocationService
    from finance_core.application.ai_invocation_contract import InterpretationPolicy

    class ObservedDurableInput(DurableInputVerifier):
        def __init__(self):
            self.modes = []

        def verify_persisted(self, connection, reply_evidence_id, expected):
            self.modes.append(expected.historical)
            return super().verify_persisted(connection, reply_evidence_id, expected)

    c = migrated_temp_db_connection
    _, _, parent, *_ = subject(c, "history-edit")
    provider = DurableProvider(c)
    verifier = ObservedDurableInput()
    ai = AiInvocationService(
        connection=c,
        binding=BINDING,
        source_verifier=_DurableSourceVerifier(),
        interpretation_input_verifier=verifier,
        provider=provider,
        clock=lambda: NOW,
        interpretation_policy=InterpretationPolicy("UTC", "SGD"),
    )
    first = _human(c, ai, parent["public_id"], 1)
    owner = amendment(c, ai)
    review = owner.prepare(first.proposal_public_id)
    old_payload = json.loads(review.projection["effective_payload_json"])
    reply = "Change merchant to Cafe Two"
    evidence = persist_interpretation_reply(c, review, reply)
    provider.response = response("patch", {"merchant": "Cafe Two"}, reply)
    attempt = ai.prepare_edit(review.review_id, evidence, "history-edit")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    assert False in verifier.modes
    _human(c, ai, first.proposal_public_id, 2)
    latest = c.execute(
        "SELECT material_json FROM application_amendment_records ORDER BY resulting_version DESC"
    ).fetchone()[0]
    actual = canonical_json_value(latest)
    assert actual["base_version"] == 1 and actual["resulting_version"] == 2
    assert json.loads(actual["base_payload_json"]) == old_payload
    assert provider.requests[0]["input"]["current_values"]["description"] == "Human revision 1"
    before = _owner_rows(c)
    verifier.modes.clear()
    assert ai.get_status(attempt.attempt_id).state == "RESULT_STALE"
    assert ai.recover(attempt.attempt_id).state == "RESULT_STALE"
    assert verifier.modes and all(verifier.modes)
    assert _owner_rows(c) == before and provider.calls == 1
    _strict_live_refused(c, ai, provider, attempt.attempt_id)


# One bounded mixed-owner transition correction: actual owners, not cached permissions.
def _mixed_edit(c, ai, provider, human, public_id, patch, text, identity):
    review = human.prepare(public_id)
    reply = persist_interpretation_reply(c, review, text, identity=identity)
    provider.response = response("patch", patch, text)
    attempt = ai.prepare_edit(review.review_id, reply, identity)
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    published = ai.publish(attempt.attempt_id)
    assert published.state == "PUBLISHED" and published.is_current
    return published


def _mixed_receipt(tmp_path, **options):
    from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
    from test_application_amendment_v1 import _EDIT_BINDING
    from test_application_posting_recovery_v1 import BINDING, NOW

    from finance_core.application.amendment import AmendmentService
    from finance_core.application.posting import PostingService

    c, ai, provider, parent, intake, source, decision = _review_fix_receipt(tmp_path, **options)
    human = AmendmentService(
        connection=c,
        source_verifier=source,
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    post = PostingService(
        connection=c,
        source_verifier=source,
        human_decision_authority=decision,
        binding=BINDING,
        clock=lambda: NOW,
        ai_lineage_authority=ai,
    )
    return c, ai, provider, human, post, parent, intake


def _mixed_human(c, owner, public_id, patch, identity):
    return _apply(
        c, owner, owner.prepare(public_id), patch, evidence_id=identity, amendment_id=identity
    )


def _mixed_post(c, owner, public_id, identity, *, receipt=True):
    from test_application_amendment_v1 import _persist_fresh_posting_confirmation

    review = owner.prepare(public_id)
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    _persist_fresh_posting_confirmation(c, review, public_id, identity)
    final = owner.submit_post(review.review_id, identity)
    assert final.state == "finalized"
    assert owner.get_status(final.attempt_id) == owner.resume_post(final.attempt_id) == final
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
    if receipt:
        for table in (
            "receipts",
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
        ):
            assert c.execute("SELECT count(*) FROM " + table).fetchone()[0] == 1, (
                "STOP_ECONOMIC_EVENT_COUNT"
            )
    return review


def test_mixed_owner_T1_same_id_completion_then_ai_and_fresh_post(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    ai, provider, published, *_ = publish_initial(c, "mixed-T1")
    human = amendment(c, ai)
    completed = _mixed_human(
        c, human, published.proposal_public_id, {"description": "Human lunch"}, "mixed-T1-human"
    )
    assert completed.proposal_public_id == published.proposal_public_id
    post = posting(c, ai)
    old = post.prepare(completed.proposal_public_id)
    result = _mixed_edit(
        c,
        ai,
        provider,
        human,
        completed.proposal_public_id,
        {"merchant": "Cafe Two"},
        "Change merchant to Cafe Two",
        "mixed-T1-ai",
    )
    from test_application_amendment_v1 import _persist_fresh_posting_confirmation

    from finance_core.application.posting import PostingError

    _persist_fresh_posting_confirmation(c, old, completed.proposal_public_id, "mixed-T1-old")
    before = _cancel_all_rows(c)
    with pytest.raises(PostingError):
        post.submit_post(old.review_id, "mixed-T1-old")
    assert _cancel_all_rows(c) == before
    _mixed_post(c, post, result.proposal_public_id, "mixed-T1-fresh", receipt=False)
    assert tuple(c.execute("SELECT amount,merchant,description FROM transactions").fetchone()) == (
        12.5,
        "Cafe Two",
        "Human lunch",
    )
    assert provider.calls == 2


def test_mixed_owner_R1_human_amount_then_ai_current_value_once(tmp_path):
    c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path)
    try:
        first = _mixed_human(c, human, parent, {"amount": "15.00"}, "mixed-R1-human")
        assert first.proposal_public_id != parent
        old = post.prepare(first.proposal_public_id)
        assert old.projection["financial_projection"]["amount"] == "15.00"
        result = _mixed_edit(
            c,
            ai,
            provider,
            human,
            first.proposal_public_id,
            {"amount": "20.00"},
            "Change amount to SGD 20.00",
            "mixed-R1-ai",
        )
        from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry
        from finance_core.parser_proposals.receipt_facts_conversion import _supersession_path
        from finance_core.parser_proposals.repository import ParserProposalRepository

        prefix = ParserProposalRepository(c).get_by_public_id(first.proposal_public_id)
        assert has_independent_ai_ancestry(c, prefix)
        path = _supersession_path(c, prefix["id"])
        assert all(
            c.execute(
                "SELECT count(*) FROM independent_ai_publications WHERE child_parser_output_id=?",
                (node_id,),
            ).fetchone()[0]
            == 0
            for node_id in path
        )
        ai.verify_lineage(result.proposal_public_id)
        human_history = verify_amendment_record(
            c, c.execute("SELECT * FROM application_amendment_records").fetchone()
        )
        assert human_history["material_patch"]["amount"] == "15.00"
        history_rows = [
            tuple(row) for row in c.execute("SELECT * FROM application_amendment_records")
        ]
        review = _mixed_post(c, post, result.proposal_public_id, "mixed-R1-fresh")
        assert [
            tuple(row) for row in c.execute("SELECT * FROM application_amendment_records")
        ] == history_rows
        assert (
            c.execute(
                "SELECT a.review_id FROM application_posting_decisions d "
                "JOIN application_posting_attempts a ON a.attempt_id=d.attempt_id"
            ).fetchone()[0]
            == review.review_id
        )
        assert review.projection["financial_projection"]["amount"] == "20.00"
        assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (
            20,
            "SGD",
        )
        assert c.execute("SELECT count(*) FROM application_amendment_records").fetchone()[0] == 1
        assert provider.calls == 1
    finally:
        c.close()


def test_mixed_owner_F1_result_time_refuses_before_any_publication(migrated_temp_db_connection):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "mixed-F1-time")
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "mixed-F1-time")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    _tamper(
        c, "independent_ai_results", "UPDATE independent_ai_results SET observed_at=observed_at+1"
    )
    before = _cancel_all_rows(c)
    for operation in (ai.get_status, ai.recover, ai.publish):
        with pytest.raises(AiInvocationError):
            operation(attempt.attempt_id)
        assert _cancel_all_rows(c) == before and provider.calls == 1


def test_mixed_owner_T2_legacy_ai_keeps_its_real_owner(tmp_path):
    from test_application_amendment_ai_v1 import _create_sealed_ai_root, _persist_application_source

    c, intake, parent = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(c, intake)
        old = {
            table: [tuple(r) for r in c.execute("SELECT * FROM " + table)]
            for table in (
                "ai_fallback_attempts",
                "ai_fallback_invocation_claims",
                "ai_fallback_results",
                "ai_fallback_proposal_links",
            )
        }
        ai, provider = service(c)
        result = _mixed_edit(
            c,
            ai,
            provider,
            amendment(c, ai),
            parent["public_id"],
            {"merchant": "Cafe Two"},
            "Change merchant to Cafe Two",
            "mixed-T2",
        )
        before = _cancel_all_rows(c)
        assert ai.get_status(result.attempt_id) == ai.recover(result.attempt_id) == result
        assert ai.publish(result.attempt_id) == ai.invoke(result.attempt_id) == result
        assert _cancel_all_rows(c) == before and provider.calls == 1
        assert old == {
            table: [tuple(r) for r in c.execute("SELECT * FROM " + table)] for table in old
        }
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        c.close()


@pytest.mark.parametrize("case", ["currency", "date", "date-preserved"])
def test_mixed_owner_R2_R3_R4_current_pair_and_ordered_date(tmp_path, case):
    c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path)
    try:
        first_patch = (
            {"currency": "USD"} if case == "currency" else {"transaction_date": "2026-10-09"}
        )
        first = _mixed_human(c, human, parent, first_patch, "mixed-sibling-human")
        post.prepare(first.proposal_public_id)
        if case == "currency":
            patch, text = {"currency": "SGD"}, "Change currency to SGD"
        elif case == "date":
            patch, text = {"transaction_date": "2026-10-10"}, "Change date to 2026-10-10"
        else:
            patch, text = {"amount": "20.00"}, "Change amount to SGD 20.00"
        result = _mixed_edit(
            c, ai, provider, human, first.proposal_public_id, patch, text, "mixed-sibling-ai"
        )
        public_id = result.proposal_public_id
        if case == "date-preserved":
            assert (
                post.prepare(public_id).projection["financial_projection"]["transaction_date"]
                == "2026-10-09"
            )
            public_id = _mixed_human(
                c, human, public_id, {"transaction_date": "2026-10-10"}, "mixed-later-date-human"
            ).proposal_public_id
        _mixed_post(c, post, public_id, "mixed-sibling-fresh")
        expected = (
            (12.5, "SGD", "2026-10-08")
            if case == "currency"
            else (20 if case == "date-preserved" else 12.5, "SGD", "2026-10-10")
        )
        assert (
            tuple(c.execute("SELECT amount,currency,transaction_date FROM transactions").fetchone())
            == expected
        )
        assert provider.calls == 1
    finally:
        c.close()


def test_mixed_owner_R5_R7_ai_overwrite_return_echo_never_revives_human(tmp_path):
    from test_application_amendment_v1 import _new_evidence

    from finance_core.application.amendment_contract import AmendmentError
    from finance_core.parser_proposals.effective_payload import resolve_effective_payload
    from finance_core.parser_proposals.receipt_facts_conversion import (
        _durable_resolution_provenance,
    )
    from finance_core.parser_proposals.repository import ParserProposalRepository

    c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path, ocr_confidence_scaled=6000)
    try:
        original_ocr = [tuple(r) for r in c.execute("SELECT * FROM receipt_ocr_blocks")]
        fields = {
            "amount": "13.75",
            "currency": "USD",
            "transaction_date": "2026-10-09",
            "merchant": "Human Cafe",
            "description": "Human lunch",
            "category": "human_food",
        }
        first = _mixed_human(c, human, parent, fields, "mixed-six-human")
        post.prepare(first.proposal_public_id)
        changed = _mixed_edit(
            c,
            ai,
            provider,
            human,
            first.proposal_public_id,
            {"amount": "20.00"},
            "Change amount to USD 20.00",
            "mixed-six-ai1",
        )
        assert (
            post.prepare(changed.proposal_public_id).projection["expense_assessment"]["basis"]
            == "full_card_ai_expense_v1"
        )
        returned = _mixed_edit(
            c,
            ai,
            provider,
            human,
            changed.proposal_public_id,
            {"amount": "13.75"},
            "Change amount to USD 13.75",
            "mixed-six-ai2",
        )
        proposal = ParserProposalRepository(c).get_lineage_row_by_id(
            c.execute(
                "SELECT id FROM parser_outputs WHERE public_id=?", (returned.proposal_public_id,)
            ).fetchone()[0]
        )
        payload = resolve_effective_payload(c, proposal)[0]
        corrected, completed = _durable_resolution_provenance(c, proposal, payload)
        assert "amount" not in corrected | completed
        assert {
            "currency",
            "transaction_date",
            "merchant",
            "description",
            "category",
        } <= corrected | completed
        returned_card = post.prepare(returned.proposal_public_id)
        assert returned_card.projection["expense_assessment"]["hard_blockers"] == []
        assert (
            "historical_low_ocr_or_parser_confidence"
            in returned_card.projection["expense_assessment"]["review_warnings"]
        )
        echo = human.prepare(returned.proposal_public_id)
        _new_evidence(c, echo, fields, evidence_id="mixed-six-echo")
        before = _cancel_all_rows(c)
        with pytest.raises(AmendmentError, match="material"):
            human.amend(echo.review_id, "mixed-six-echo", "mixed-six-echo")
        assert _cancel_all_rows(c) == before
        renewed = _mixed_human(
            c, human, returned.proposal_public_id, {"amount": "14.00"}, "mixed-six-renewed"
        )
        _mixed_post(c, post, renewed.proposal_public_id, "mixed-six-final")
        assert tuple(c.execute("SELECT amount,currency FROM transactions").fetchone()) == (
            14,
            "USD",
        ), "STOP_FINANCIAL_FACT_MISMATCH"
        assert (
            provider.calls == 2
            and [tuple(r) for r in c.execute("SELECT * FROM receipt_ocr_blocks")] == original_ocr
        ), "STOP_ORIGINAL_EVIDENCE_CHANGED"
    finally:
        c.close()


@pytest.mark.parametrize("flag_field", ["currency", "transaction_date"])
@pytest.mark.parametrize("change_required", [True, False])
def test_mixed_owner_R6_original_flag_current_human_binding(
    tmp_path, monkeypatch, flag_field, change_required
):
    from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
    from test_application_amendment_review_fixes_v1 import _prepare_missing_date_receipt
    from test_application_amendment_v1 import _EDIT_BINDING
    from test_application_posting_recovery_v1 import BINDING, NOW
    from test_independent_ai_invocation_v1 import DurableInputVerifier, DurableProvider

    from finance_core.application.ai_invocation import AiInvocationService
    from finance_core.application.ai_invocation_contract import InterpretationPolicy
    from finance_core.application.amendment import AmendmentService
    from finance_core.application.posting import PostingService
    from finance_core.parser_proposals.effective_payload import resolve_effective_payload
    from finance_core.parser_proposals.receipt_facts_conversion import (
        _durable_resolution_provenance,
        _require_flags_resolved,
    )
    from finance_core.parser_proposals.repository import ParserProposalRepository

    if flag_field == "currency":
        c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path, total_currency=None)
    else:
        c, old, parent = _prepare_missing_date_receipt(tmp_path, monkeypatch)
        provider = DurableProvider(c)
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=old._source_port,
            interpretation_input_verifier=DurableInputVerifier(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        human = AmendmentService(
            connection=c,
            source_verifier=old._source_port,
            human_amendment_authority=DurableSyntheticAmendmentAuthority(),
            binding=_EDIT_BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        post = PostingService(
            connection=c,
            source_verifier=old._source_port,
            human_decision_authority=old._decision_port,
            binding=BINDING,
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
    try:
        root = ParserProposalRepository(c).get_by_public_id(parent)
        flags = resolve_effective_payload(c, root)[0]["ambiguity_flags"]
        assert flags
        fields = {
            "amount": "13.75",
            "currency": "USD",
            "transaction_date": "2026-10-09",
            "merchant": "Human Cafe",
            "description": "Human lunch",
            "category": "human_food",
        }
        first = _mixed_human(c, human, parent, fields, "mixed-R6-human")
        post.prepare(first.proposal_public_id)
        if not change_required:
            patch, text = {"category": "new_food"}, "Change category to new_food"
        elif flag_field == "currency":
            patch, text = {"currency": "SGD"}, "Change currency to SGD"
        else:
            patch, text = {"transaction_date": "2026-10-10"}, "Change date to 2026-10-10"
        repository = ParserProposalRepository(c)
        proposal = repository.get_by_public_id(first.proposal_public_id)
        payload, completion_id, version = resolve_effective_payload(c, proposal)
        baseline = (dict(proposal), payload, completion_id, version)
        provenance = _durable_resolution_provenance(c, proposal, payload)
        assert set(fields) <= provenance[0] | provenance[1]
        _require_flags_resolved(c, proposal, payload)
        material = verify_amendment_record(
            c, c.execute("SELECT * FROM application_amendment_records").fetchone()
        )
        assert set(material["material_patch"]) == set(fields)
        retained = _cancel_all_rows(c)
        preserved_tables = (
            "parser_outputs",
            "raw_intake_records",
            "receipt_ocr_blocks",
            "receipt_ocr_extractions",
            "receipt_ocr_proposal_links",
            "application_amendment_records",
            "parser_proposal_completions",
            "parser_proposal_events",
            "financial_audit_events",
            "independent_ai_publications",
            "application_posting_decisions",
            "transactions",
            "receipts",
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
        )
        review = human.prepare(first.proposal_public_id)
        reply = persist_interpretation_reply(c, review, text, identity="mixed-R6-ai")
        provider.response = response("patch", patch, text)
        attempt = ai.prepare_edit(review.review_id, reply, "mixed-R6-ai")
        result = ai.invoke(attempt.attempt_id)
        assert result.state == "VALID"
        assert provider.calls == 1
        after_result = _cancel_all_rows(c)
        assert all(after_result[table] == retained[table] for table in preserved_tables)
        unchanged = repository.get_by_public_id(first.proposal_public_id)
        assert (dict(unchanged), *resolve_effective_payload(c, unchanged)) == baseline
        assert _durable_resolution_provenance(c, unchanged, payload) == provenance
        _require_flags_resolved(c, unchanged, payload)
        child = ai.publish(attempt.attempt_id)
        assert ai.recover(attempt.attempt_id) == child
        card = post.prepare(child.proposal_public_id)
        assert card.projection["expense_assessment"]["hard_blockers"] == []
        assert card.projection["expense_assessment"]["basis"] == "full_card_ai_expense_v1"
        current = repository.get_by_public_id(child.proposal_public_id)
        current_payload = resolve_effective_payload(c, current)[0]
        current_provenance = _durable_resolution_provenance(c, current, current_payload)
        if change_required:
            assert flag_field not in current_provenance[0] | current_provenance[1]
        else:
            assert flag_field in current_provenance[0] | current_provenance[1]
        # The actual full-card decision supplies the new basis; it does not
        # invent a HUMAN field witness for a model-produced replacement.
        _mixed_post(c, post, child.proposal_public_id, "mixed-R6-confirm")
        expected = {**fields, **patch}
        assert tuple(
            c.execute(
                "SELECT amount,currency,transaction_date,merchant FROM transactions"
            ).fetchone()
        ) == (
            float(expected["amount"]),
            expected["currency"],
            expected["transaction_date"],
            expected["merchant"],
        )
        assert [tuple(r) for r in c.execute("SELECT * FROM receipt_ocr_blocks")] == retained[
            "receipt_ocr_blocks"
        ]
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1

    finally:
        c.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "result-id",
        "result-attempt",
        "reference",
        "claim-hash",
        "claim-id",
        "raw",
        "raw-hash",
        "seal",
        "validation",
    ],
)
def test_mixed_owner_F1_scalar_and_seal_groups(migrated_temp_db_connection, corruption):
    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "mixed-correlations")
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "mixed-correlations")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    if corruption == "result-attempt":
        ai.publish(attempt.attempt_id)
        _, _, _, other, *_ = subject(c, "mixed-other-attempt")
        second = ai.prepare_initial(other["public_id"], "mixed-other-attempt")
        _tamper(
            c,
            "independent_ai_results",
            "UPDATE independent_ai_results SET attempt_id=?",
            (second.attempt_id,),
        )
    else:
        table, update = {
            "result-id": ("independent_ai_results", "result_id='air_broken'"),
            "reference": ("independent_ai_results", "observation_record_id='missing-observation'"),
            "claim-hash": ("independent_ai_claims", "request_hash='" + "0" * 64 + "'"),
            "claim-id": ("independent_ai_claims", "claim_id='aic_broken'"),
            "raw": ("independent_ai_results", "raw_response=raw_response||' '"),
            "raw-hash": ("independent_ai_results", "raw_response_hash='" + "0" * 64 + "'"),
            "seal": ("independent_ai_results", "material_hash='" + "0" * 64 + "'"),
            "validation": ("independent_ai_results", "validation_json='{}'"),
        }[corruption]
        _tamper(c, table, "UPDATE " + table + " SET " + update)
    before = _cancel_all_rows(c)
    for operation in (ai.get_status, ai.recover, ai.publish):
        with pytest.raises(ValueError):
            operation(attempt.attempt_id)
        assert _cancel_all_rows(c) == before and provider.calls == 1


@pytest.mark.parametrize("fault", ["child-field", "late-provider"])
def test_mixed_owner_F2_full_real_graph_verifies_before_commit(
    migrated_temp_db_connection, monkeypatch, fault
):
    import finance_core.application.ai_invocation as invocation

    c = migrated_temp_db_connection
    _, _, _, intake, *_ = subject(c, "mixed-precommit")
    ai, provider = service(c)
    attempt = ai.prepare_initial(intake["public_id"], "mixed-precommit")
    assert ai.invoke(attempt.attempt_id).state == "VALID"
    before = _cancel_all_rows(c)
    actual_write = invocation.publish_in_transaction

    def write_then_damage(*args, **kwargs):
        identity = actual_write(*args, **kwargs)
        assert (
            c.in_transaction
            and c.execute("SELECT count(*) FROM independent_ai_publications").fetchone()[0] == 1
        )
        if fault == "late-provider":
            c.execute("UPDATE synthetic_ai_receipts SET signature='invalid'")
        else:
            rows = c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='parser_proposal_field_evidence'"
            ).fetchall()
            for name, _ in rows:
                c.execute('DROP TRIGGER "' + name + '"')
            c.execute(
                "UPDATE parser_proposal_field_evidence SET proposed_value='changed' "
                "WHERE evidence_source_type='ai_model' AND field_name='amount'"
            )
            for _, sql in rows:
                c.execute(sql)
        return identity

    monkeypatch.setattr(invocation, "publish_in_transaction", write_then_damage)
    with pytest.raises(ValueError):
        ai.publish(attempt.attempt_id)
    assert _cancel_all_rows(c) == before and provider.calls == 1
    assert ai.get_status(attempt.attempt_id).state == "VALID"


def test_mixed_owner_F3_replay_preserves_completed_and_physical_leaf_on_reopen(
    migrated_temp_db_connection,
):
    import sqlite3

    from finance_core.staging_guard import open_staging_database

    c = migrated_temp_db_connection
    ai, provider, published, *_ = publish_initial(c, "mixed-replay")
    assert ai.publish(published.attempt_id) == published
    human = amendment(c, ai)
    completed = _mixed_human(
        c,
        human,
        published.proposal_public_id,
        {"description": "Later human lunch"},
        "mixed-replay-H1",
    )
    status = ai.get_status(published.attempt_id)
    assert not status.is_current
    before = _cancel_all_rows(c)
    assert ai.publish(published.attempt_id) == status
    assert _cancel_all_rows(c) == before
    leaf = _mixed_human(
        c, human, completed.proposal_public_id, {"amount": "14.00"}, "mixed-replay-H2"
    )
    before = _cancel_all_rows(c)
    assert ai.publish(published.attempt_id) == status
    assert _cancel_all_rows(c) == before and provider.calls == 1
    database = c.execute("PRAGMA database_list").fetchone()[2]
    c.close()
    reopened = open_staging_database(database)
    reopened.row_factory = sqlite3.Row
    try:
        recovered, new_provider = service(reopened)
        assert (
            recovered.get_status(published.attempt_id)
            == recovered.recover(published.attempt_id)
            == recovered.publish(published.attempt_id)
            == status
        )
        assert _cancel_all_rows(reopened) == before and new_provider.calls == 0
        assert (
            reopened.execute(
                "SELECT p.public_id FROM raw_intake_records r "
                "JOIN parser_outputs p ON p.id=r.parser_output_id"
            ).fetchone()[0]
            == leaf.proposal_public_id
        )
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "parser-id",
        "receipt-role",
        "field-marker",
        "orphan-audit",
        "first-base",
        "human-seal",
    ],
)
def test_prefix_owner_unexplained_indicator_refuses_financial_entry(tmp_path, corruption):
    from finance_core.application.posting import PostingError
    from finance_core.financial_audit import AuditEventCommand, append_financial_audit_event
    from finance_core.parser_proposals.independent_ai_validation import canonical_text, sha

    c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path)
    try:
        first = _mixed_human(c, human, parent, {"amount": "15.00"}, "prefix-boundary-human")
        result = _mixed_edit(
            c,
            ai,
            provider,
            human,
            first.proposal_public_id,
            {"amount": "20.00"},
            "Change amount to SGD 20.00",
            "prefix-boundary-ai",
        )
        assert (
            post.prepare(result.proposal_public_id).projection["financial_projection"]["amount"]
            == "20.00"
        )
        prefix_id = c.execute(
            "SELECT id FROM parser_outputs WHERE public_id=?", (first.proposal_public_id,)
        ).fetchone()[0]
        if corruption == "parser-id":
            _tamper(
                c,
                "parser_outputs",
                "UPDATE parser_outputs SET parser_name='independent_finance_ai', "
                "public_id='aipo_hidden_prefix' WHERE id=?",
                (prefix_id,),
            )
        elif corruption == "receipt-role":
            _tamper(
                c,
                "receipt_ocr_proposal_links",
                "UPDATE receipt_ocr_proposal_links SET link_role='independent_ai' "
                "WHERE parser_output_id=?",
                (prefix_id,),
            )
        elif corruption == "field-marker":
            _tamper(
                c,
                "parser_proposal_field_evidence",
                "UPDATE parser_proposal_field_evidence "
                "SET evidence_reference='independent-ai:orphan' "
                "WHERE parser_output_id=?",
                (prefix_id,),
            )
        elif corruption == "orphan-audit":
            head = c.execute(
                "SELECT new_state_json,created_at FROM financial_audit_events "
                "WHERE aggregate_public_id=? ORDER BY sequence_number DESC LIMIT 1",
                (first.proposal_public_id,),
            ).fetchone()
            c.execute("BEGIN IMMEDIATE")
            append_financial_audit_event(
                c,
                AuditEventCommand(
                    event_public_id="prefix_orphan_ai_audit",
                    aggregate_type="parser_proposal",
                    aggregate_public_id=first.proposal_public_id,
                    event_type="independent_ai_proposal_published",
                    event_payload={"publication_id": "aipub_orphan"},
                    new_state=canonical_json_value(head["new_state_json"]),
                    actor_type="ai",
                    actor_public_id="synthetic-adapter",
                    correlation_public_id=first.proposal_public_id,
                    causation_public_id="aipub_orphan",
                    created_at=head["created_at"],
                ),
            )
            c.commit()
        elif corruption == "first-base":
            material = json.loads(
                c.execute("SELECT material_json FROM independent_ai_attempts").fetchone()[0]
            )
            material["parent_version"] += 1
            _tamper(
                c,
                "independent_ai_attempts",
                "UPDATE independent_ai_attempts SET parent_version=?, "
                "material_json=?,material_hash=?",
                (material["parent_version"], canonical_text(material), sha(material)),
            )
        else:
            _tamper(
                c,
                "application_amendment_records",
                "UPDATE application_amendment_records SET record_hash=?",
                ("0" * 64,),
            )
        before = _cancel_all_rows(c)
        if corruption == "field-marker":
            from finance_core.application.ai_invocation_contract import AiInvocationError
            from finance_core.parser_proposals.human_revision import HumanRevisionLineageError

            with pytest.raises(
                PostingError, match="^receipt revision relational evidence is invalid$"
            ) as rejected:
                post.prepare(result.proposal_public_id)
            invocation_error = rejected.value.__cause__
            assert type(invocation_error) is AiInvocationError
            native_error = invocation_error.__cause__
            assert type(native_error) is HumanRevisionLineageError
            assert str(native_error) == "receipt revision relational evidence is invalid"
        else:
            with pytest.raises(PostingError):
                post.prepare(result.proposal_public_id)
        assert _cancel_all_rows(c) == before and provider.calls == 1
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        c.close()


def test_systemic_old_creation_completion_before_new_and_after_replay(tmp_path):
    from test_application_amendment_ai_v1 import _create_sealed_ai_root, _persist_application_source

    from finance_core.parser_proposals.independent_ai_revision import _revisions
    from finance_core.parser_proposals.repository import ParserProposalRepository

    c, intake, parent = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(c, intake)
        old = {
            table: [tuple(row) for row in c.execute("SELECT * FROM " + table)]
            for table in (
                "ai_fallback_attempts",
                "ai_fallback_invocation_claims",
                "ai_fallback_results",
                "ai_fallback_proposal_links",
            )
        }
        ai, provider = service(c)
        human = amendment(c, ai)
        first = _mixed_human(
            c,
            human,
            parent["public_id"],
            {"description": "Lunch", "category": "food"},
            "systemic-old-completion",
        )
        assert first.proposal_public_id == parent["public_id"] and first.proposal_version == 1
        revision = ParserProposalRepository(c).get_lineage_row_by_id(parent["id"])
        actual = _revisions(c, revision)
        assert (
            len(actual) == 2
            and actual[0]["description"] is None
            and actual[1]["description"] == "Lunch"
        )
        result = _mixed_edit(
            c,
            ai,
            provider,
            human,
            first.proposal_public_id,
            {"merchant": "Cafe Two"},
            "Change merchant to Cafe Two",
            "systemic-new-after-completion",
        )
        retained = json.loads(
            c.execute("SELECT material_json FROM independent_ai_attempts").fetchone()[0]
        )
        assert retained["parent_version"] == 1 and retained["base_payload"] == actual[1]
        later = _mixed_human(
            c,
            human,
            result.proposal_public_id,
            {"description": "Later meal"},
            "systemic-later-completion",
        )
        assert later.proposal_public_id == result.proposal_public_id and later.proposal_version == 1
        before = _cancel_all_rows(c)
        history = ai.get_status(result.attempt_id)
        assert history.state == "PUBLISHED" and not history.is_current
        assert (
            ai.recover(result.attempt_id)
            == ai.publish(result.attempt_id)
            == ai.invoke(result.attempt_id)
            == history
        )
        assert _cancel_all_rows(c) == before and provider.calls == 1
        assert all(
            [tuple(row) for row in c.execute("SELECT * FROM " + table)] == rows
            for table, rows in old.items()
        )
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
    finally:
        c.close()


def test_systemic_old_creation_completed_physical_human_then_new_oncepost(tmp_path):
    from test_application_amendment_ai_v1 import (
        _create_sealed_ai_root,
        _persist_application_source,
        _persist_fresh_posting_reply,
    )

    c, intake, parent = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(c, intake)
        ai, provider = service(c)
        human = amendment(c, ai)
        complete = _mixed_human(
            c,
            human,
            parent["public_id"],
            {"description": "Lunch", "category": "food"},
            "systemic-physical-complete",
        )
        assert complete.proposal_public_id == parent["public_id"] and complete.proposal_version == 1
        corrected = _mixed_human(
            c, human, complete.proposal_public_id, {"amount": "15.00"}, "systemic-physical-human"
        )
        assert corrected.proposal_public_id != parent["public_id"]
        old = _cancel_all_rows(c)
        result = _mixed_edit(
            c,
            ai,
            provider,
            human,
            corrected.proposal_public_id,
            {"merchant": "Cafe Two"},
            "Change merchant to Cafe Two",
            "systemic-physical-new",
        )
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
            "STOP_ECONOMIC_EVENT_COUNT"
        )
        posting_owner = posting(c, ai)
        review = posting_owner.prepare(result.proposal_public_id)
        decision_id = _persist_fresh_posting_reply(c, review, result.proposal_public_id)
        final = posting_owner.submit_post(review.review_id, decision_id)
        assert final.state == "finalized"
        final_rows = _cancel_all_rows(c)
        assert (
            posting_owner.get_status(final.attempt_id)
            == posting_owner.resume_post(final.attempt_id)
            == final
        )
        assert _cancel_all_rows(c) == final_rows
        for table in (
            "transactions",
            "application_posting_attempts",
            "application_posting_decisions",
        ):
            assert c.execute("SELECT count(*) FROM " + table).fetchone()[0] == 1, (
                "STOP_ECONOMIC_EVENT_COUNT"
            )
        assert tuple(
            c.execute(
                "SELECT amount,currency,merchant,description,category FROM transactions"
            ).fetchone()
        ) == (
            15,
            "SGD",
            "Cafe Two",
            "Lunch",
            "food",
        ), "STOP_FINANCIAL_FACT_MISMATCH"
        assert provider.calls == 1
        for table in (
            "ai_fallback_attempts",
            "ai_fallback_invocation_claims",
            "ai_fallback_results",
            "ai_fallback_proposal_links",
            "application_amendment_records",
            "parser_proposal_completions",
        ):
            assert [tuple(row) for row in c.execute("SELECT * FROM " + table)] == old[table]
    finally:
        c.close()


@pytest.mark.parametrize("corruption", ["old-response", "human-seal", "new-provider"])
def test_systemic_old_creation_completion_and_new_producer_fault_refuses(tmp_path, corruption):
    from test_application_amendment_ai_v1 import _create_sealed_ai_root, _persist_application_source

    from finance_core.parser_proposals.ai_fallback import AiFallbackServiceError
    from finance_core.parser_proposals.amendment_lineage import AmendmentLineageError

    c, intake, parent = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(c, intake)
        ai, provider = service(c)
        human = amendment(c, ai)
        first = _mixed_human(
            c,
            human,
            parent["public_id"],
            {"description": "Lunch", "category": "food"},
            "systemic-fault-complete",
        )
        result = _mixed_edit(
            c,
            ai,
            provider,
            human,
            first.proposal_public_id,
            {"merchant": "Cafe Two"},
            "Change merchant to Cafe Two",
            "systemic-fault-new",
        )
        if corruption == "old-response":
            _tamper(
                c,
                "ai_fallback_results",
                "UPDATE ai_fallback_results SET response_sha256=?",
                ("0" * 64,),
            )
            refusal = AiFallbackServiceError
        elif corruption == "human-seal":
            _tamper(
                c,
                "application_amendment_records",
                "UPDATE application_amendment_records SET record_hash=?",
                ("0" * 64,),
            )
            refusal = AiInvocationError
        else:
            _tamper(
                c, "synthetic_ai_receipts", "UPDATE synthetic_ai_receipts SET signature='invalid'"
            )
            refusal = ValueError
        before = _cancel_all_rows(c)
        for operation in (ai.get_status, ai.recover, ai.publish):
            with pytest.raises(refusal) as caught:
                operation(result.attempt_id)
            if corruption == "human-seal":
                assert type(caught.value) is AiInvocationError
                assert str(caught.value) == "Original source revision history does not verify"
                assert type(caught.value.__cause__) is AmendmentLineageError
                assert str(caught.value.__cause__) == "Amendment seal hash does not verify"
            assert _cancel_all_rows(c) == before and provider.calls == 1
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        c.close()


@pytest.mark.parametrize("prefix_kind", ["root", "human", "independent", "legacy"])
@pytest.mark.parametrize(
    "marker",
    [
        "orphan_audit",
        "unknown_audit",
        "duplicate_audit",
        "unknown_lifecycle",
        "wrong_node_lifecycle",
        "unknown_parser",
        "unknown_field",
        "payload_only_audit",
        "references_only_audit",
        "wrong_node_audit",
        "wrong_node_field",
    ],
)
@pytest.mark.parametrize("phase", ["before_facts", "accepted_replay"])
def test_closeout_all_creation_prefix_markers_refuse_cached_card_and_replay(
    tmp_path, prefix_kind, marker, phase
):
    from test_application_amendment_ai_v1 import _create_sealed_ai_root, _persist_application_source

    from finance_core.application.posting import PostingError
    from finance_core.financial_audit import AuditEventCommand, append_financial_audit_event
    from finance_core.parser_proposals.ai_fallback import AiFallbackServiceError
    from finance_core.parser_proposals.amendment_lineage import AmendmentLineageError
    from finance_core.parser_proposals.human_revision import HumanRevisionLineageError

    owner_errors = (
        PostingError,
        AiInvocationError,
        AiFallbackServiceError,
        AmendmentLineageError,
        HumanRevisionLineageError,
    )
    if prefix_kind == "legacy":
        c, intake, parent = _create_sealed_ai_root(tmp_path)
        _persist_application_source(c, intake)
        ai, provider = service(c)
        human, post = amendment(c, ai), posting(c, ai)
        for table in (
            "synthetic_displays",
            "synthetic_decisions",
            "synthetic_replies",
            "synthetic_display_closures",
        ):
            c.execute(
                "CREATE TABLE IF NOT EXISTS "
                + table
                + " (id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
            )
        c.commit()
        prefix = parent["public_id"]
        _mixed_human(
            c,
            human,
            prefix,
            {"transaction_date": "2026-08-13", "description": "Lunch", "category": "food"},
            "prefix-legacy-complete",
        )
    else:
        c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path)
        prefix = parent
        if prefix_kind == "human":
            prefix = _mixed_human(
                c, human, prefix, {"amount": "15.00"}, "prefix-human"
            ).proposal_public_id
        if prefix_kind == "independent":
            prefix = _mixed_edit(
                c,
                ai,
                provider,
                human,
                prefix,
                {"amount": "15.00"},
                "Change amount to SGD 15.00",
                "prefix-first-ai",
            ).proposal_public_id
    try:
        publication = _mixed_edit(
            c,
            ai,
            provider,
            human,
            prefix,
            {"merchant": "Cafe Two"},
            "Change merchant to Cafe Two",
            "prefix-successor-ai",
        )
        card = post.prepare(publication.proposal_public_id)
        # Genuine outgoing AI audit on every kind of parent is admitted first.
        assert card.projection["financial_projection"]["merchant"] == "Cafe Two"
        from test_application_amendment_v1 import _persist_fresh_posting_confirmation

        accepted = None
        if phase == "accepted_replay":
            _persist_fresh_posting_confirmation(
                c, card, publication.proposal_public_id, "marker-existing-confirm"
            )
            accepted = post.submit_post(card.review_id, "marker-existing-confirm")
            assert accepted.state == "finalized"
            assert post.resume_post(accepted.attempt_id) == accepted
        else:
            _persist_fresh_posting_confirmation(
                c, card, publication.proposal_public_id, "marker-existing-confirm"
            )
        prefix_id = c.execute(
            "SELECT id FROM parser_outputs WHERE public_id=?", (prefix,)
        ).fetchone()[0]
        if marker.endswith("audit"):
            head = c.execute(
                "SELECT new_state_json,created_at FROM "
                "financial_audit_events WHERE aggregate_public_id=? ORDER "
                "BY sequence_number DESC LIMIT 1",
                (prefix,),
            ).fetchone()
            owned = c.execute(
                "SELECT * FROM financial_audit_events WHERE "
                "aggregate_public_id=? AND "
                "event_type='independent_ai_proposal_published'",
                (prefix,),
            ).fetchone()
            aggregate = publication.proposal_public_id if marker == "wrong_node_audit" else prefix
            if marker == "wrong_node_audit":
                head = (
                    c.execute(
                        (
                            "SELECT new_state_json,created_at FROM financial_audit_events"
                            " WHERE aggregate_public_id=? ORDER BY sequence_number DESC L"
                            "IMIT 1"
                        ),
                        (aggregate,),
                    ).fetchone()
                    or head
                )
            c.execute("BEGIN IMMEDIATE")
            append_financial_audit_event(
                c,
                AuditEventCommand(
                    event_public_id="closeout-extra-audit",
                    aggregate_type="parser_proposal",
                    aggregate_public_id=aggregate,
                    event_type="ordinary_event"
                    if marker in {"payload_only_audit", "references_only_audit"}
                    else "independent_ai_unknown"
                    if marker == "unknown_audit"
                    else "independent_ai_proposal_published",
                    event_payload={"publication_id": owned["causation_public_id"]}
                    if marker == "wrong_node_audit"
                    else {}
                    if marker == "references_only_audit"
                    else canonical_json_value(owned["event_payload_json"])
                    if marker == "duplicate_audit"
                    else {"publication_id": "aipub_orphan"},
                    new_state=canonical_json_value(head["new_state_json"]),
                    actor_type="ai",
                    actor_public_id=owned["actor_public_id"],
                    correlation_public_id=prefix,
                    causation_public_id="ordinary_cause"
                    if marker in {"payload_only_audit", "references_only_audit"}
                    else "aipub_orphan",
                    created_at=head["created_at"],
                    source_evidence_references=("independent-ai-result:air_orphan",)
                    if marker == "references_only_audit"
                    else (),
                ),
            )
            c.commit()
        elif marker == "unknown_lifecycle":
            c.execute(
                "INSERT INTO parser_proposal_events "
                "(parser_output_id,event_type,actor_type,actor_identifier,eve"
                "nt_reason,event_payload,to_status) VALUES (?, 'superseded'"
                ", 'ai', 'synthetic-adapter', 'independent-ai:unknown', '{}', 'superseded')",
                (prefix_id,),
            )
            c.commit()
            assert (
                c.execute(
                    "SELECT count(*) FROM parser_proposal_events WHERE parser_output_id=? "
                    "AND event_reason='independent-ai:unknown'",
                    (prefix_id,),
                ).fetchone()[0]
                == 1
            )
            assert (
                c.execute(
                    "SELECT 1 FROM independent_ai_publications "
                    "WHERE publication_id='independent-ai:unknown'"
                ).fetchone()
                is None
            )
        elif marker == "wrong_node_lifecycle":
            _tamper(
                c,
                "parser_proposal_events",
                "UPDATE parser_proposal_events SET parser_output_id=? WHERE "
                "parser_output_id=? AND event_type='superseded' AND "
                "event_reason LIKE 'aipub_%'",
                (
                    c.execute(
                        "SELECT id FROM parser_outputs WHERE public_id=?",
                        (publication.proposal_public_id,),
                    ).fetchone()[0],
                    prefix_id,
                ),
            )
        elif marker == "wrong_node_field":
            child_id = c.execute(
                "SELECT id FROM parser_outputs WHERE public_id=?", (publication.proposal_public_id,)
            ).fetchone()[0]
            _tamper(
                c,
                "parser_proposal_field_evidence",
                (
                    "INSERT INTO parser_proposal_field_evidence (parser_output_id"
                    ",field_name,proposed_value,confidence_score,evidence_source_"
                    "type,evidence_reference,notes) SELECT ?,field_name,proposed_"
                    "value,confidence_score,evidence_source_type,evidence_referen"
                    "ce,notes FROM parser_proposal_field_evidence WHERE parser_ou"
                    "tput_id=? AND field_name='currency'"
                ),
                (prefix_id, child_id),
            )

        elif marker == "unknown_parser":
            _tamper(
                c,
                "parser_outputs",
                "UPDATE parser_outputs SET parser_name='independent_finance_ai_unknown' WHERE id=?",
                (prefix_id,),
            )
        else:
            _tamper(
                c,
                "parser_proposal_field_evidence",
                "UPDATE parser_proposal_field_evidence SET "
                "evidence_reference='independent-ai:aipub_unknown:context' "
                "WHERE parser_output_id=?",
                (prefix_id,),
            )
        before = _cancel_all_rows(c)
        calls = provider.calls
        # All are read-only re-admission; do not proceed to confirmation/facts.
        with pytest.raises(owner_errors):
            post.prepare(publication.proposal_public_id)
        for operation in (ai.get_status, ai.recover, ai.publish, ai.invoke, ai.claim):
            with pytest.raises(owner_errors):
                operation(publication.attempt_id)
        with pytest.raises(owner_errors):
            post.submit_post(card.review_id, "marker-existing-confirm")
        if accepted is not None:
            for operation in (post.get_status, post.resume_post):
                with pytest.raises(owner_errors):
                    operation(accepted.attempt_id)
        assert _cancel_all_rows(c) == before and provider.calls == calls, (
            "STOP_EVIDENCE_OR_PROVIDER_MUTATION"
        )
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == (
            1 if accepted else 0
        ), "STOP_ECONOMIC_EVENT_COUNT"
    finally:
        c.close()


def test_targeted_independent_then_two_human_receipt_children_preserve_current_evidence(tmp_path):
    """Continuous HUMAN owners retain their current fields before a real outgoing AI edge."""
    c, ai, provider, human, post, parent, _ = _mixed_receipt(tmp_path)
    try:
        independent = _mixed_edit(
            c,
            ai,
            provider,
            human,
            parent,
            {"amount": "15.00"},
            "Change amount to SGD 15.00",
            "targeted-first-ai",
        )
        first = _mixed_human(
            c,
            human,
            independent.proposal_public_id,
            {"amount": "16.00", "merchant": "Human Cafe"},
            "targeted-first-human",
        )
        second = _mixed_human(
            c,
            human,
            first.proposal_public_id,
            {"amount": "17.00"},
            "targeted-second-human",
        )
        human_rows = [tuple(r) for r in c.execute("SELECT * FROM application_amendment_records")]
        outgoing = _mixed_edit(
            c,
            ai,
            provider,
            human,
            second.proposal_public_id,
            {"category": "human_food"},
            "Change category to human_food",
            "targeted-outgoing-ai",
        )
        card = _mixed_post(c, post, outgoing.proposal_public_id, "targeted-human-chain-post")
        assert card.projection["financial_projection"]["merchant"] == "Human Cafe"
        assert tuple(c.execute("SELECT amount,currency,merchant FROM transactions").fetchone()) == (
            17,
            "SGD",
            "Human Cafe",
        ), "STOP_FINANCIAL_FACT_MISMATCH"
        assert human_rows == [
            tuple(r) for r in c.execute("SELECT * FROM application_amendment_records")
        ]
        assert provider.calls == 2
    finally:
        c.close()


@pytest.mark.parametrize("mutation", ["borrowed", "unknown", "wrong_aggregate", "independent_type"])
def test_targeted_legacy_shared_air_extra_audit_never_acquires_native_owner(tmp_path, mutation):
    from test_application_amendment_ai_v1 import _create_sealed_ai_root

    from finance_core.financial_audit import AuditEventCommand, append_financial_audit_event
    from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry

    c, _, parent = _create_sealed_ai_root(tmp_path)
    try:
        assert has_independent_ai_ancestry(c, parent) is False
        old = c.execute(
            "SELECT * FROM financial_audit_events WHERE aggregate_public_id=?",
            (parent["public_id"],),
        ).fetchone()
        aggregate = (
            c.execute(
                "SELECT public_id FROM parser_outputs WHERE id=?",
                (
                    c.execute(
                        "SELECT parent_parser_output_id FROM parser_outputs WHERE id=?",
                        (parent["id"],),
                    ).fetchone()[0],
                ),
            ).fetchone()[0]
            if mutation == "wrong_aggregate"
            else parent["public_id"]
        )
        head = (
            c.execute(
                "SELECT new_state_json,created_at FROM financial_audit_events "
                "WHERE aggregate_public_id=? ORDER BY sequence_number DESC LIMIT 1",
                (aggregate,),
            ).fetchone()
            or old
        )
        c.execute("BEGIN IMMEDIATE")
        append_financial_audit_event(
            c,
            AuditEventCommand(
                event_public_id="targeted-extra-shared-air",
                aggregate_type="parser_proposal",
                aggregate_public_id=aggregate,
                event_type="independent_ai_proposal_published"
                if mutation == "independent_type"
                else "ordinary_event",
                event_payload={},
                new_state=canonical_json_value(head["new_state_json"]),
                actor_type="ai",
                actor_public_id=old["actor_public_id"],
                correlation_public_id=aggregate,
                causation_public_id="air_unknown"
                if mutation == "unknown"
                else old["causation_public_id"],
                created_at=head["created_at"],
            ),
        )
        c.commit()
        before = _cancel_all_rows(c)
        with pytest.raises(AiInvocationError):
            has_independent_ai_ancestry(c, parent)
        assert _cancel_all_rows(c) == before, "STOP_EVIDENCE_OR_PROVIDER_MUTATION"
        assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    finally:
        c.close()


def test_targeted_marker_inventory_tuple_and_row_are_identical(migrated_temp_db_connection):
    import sqlite3

    from finance_core.parser_proposals.independent_ai_lineage import independent_marker_inventory
    from finance_core.parser_proposals.repository import ParserProposalRepository

    c = migrated_temp_db_connection
    ai, _, published, *_ = publish_initial(c, "targeted-tuple-markers")
    node = ParserProposalRepository(c).get_by_public_id(published.proposal_public_id)
    full = ParserProposalRepository(c).get_lineage_row_by_id(node["id"])
    expected = independent_marker_inventory(c, full)
    from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry

    assert has_independent_ai_ancestry(c, full) is True
    c.row_factory = None
    try:
        assert independent_marker_inventory(c, full) == expected
        assert has_independent_ai_ancestry(c, full) is True
        assert c.row_factory is None and not c.in_transaction
    finally:
        c.row_factory = sqlite3.Row
    assert ai.get_status(published.attempt_id) == published


def test_targeted_independent_marker_tuple_negative_refuses_without_factory_side_effect(
    migrated_temp_db_connection,
):
    import sqlite3

    from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry
    from finance_core.parser_proposals.repository import ParserProposalRepository

    c = migrated_temp_db_connection
    _, _, published, *_ = publish_initial(c, "targeted-tuple-negative")
    node = ParserProposalRepository(c).get_by_public_id(published.proposal_public_id)
    full = ParserProposalRepository(c).get_lineage_row_by_id(node["id"])
    _tamper(
        c,
        "parser_outputs",
        "UPDATE parser_outputs SET parser_version='unknown' WHERE id=?",
        (full["id"],),
    )
    before = _cancel_all_rows(c)
    with pytest.raises(AiInvocationError, match="topology"):
        has_independent_ai_ancestry(c, full)
    c.row_factory = None
    try:
        with pytest.raises(AiInvocationError, match="topology"):
            has_independent_ai_ancestry(c, full)
        assert c.row_factory is None and not c.in_transaction
        assert _cancel_all_rows(c) == before
    finally:
        c.row_factory = sqlite3.Row


def test_targeted_sealed_independent_field_append_trigger_is_separate_boundary(
    migrated_temp_db_connection,
):
    import sqlite3

    c = migrated_temp_db_connection
    _, _, published, *_ = publish_initial(c, "targeted-field-trigger")
    child = c.execute(
        "SELECT id FROM parser_outputs WHERE public_id=?", (published.proposal_public_id,)
    ).fetchone()[0]
    before = _cancel_all_rows(c)
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        c.execute(
            "INSERT INTO parser_proposal_field_evidence "
            "(parser_output_id,field_name,proposed_value,confidence_score,evidence_source_type,"
            "evidence_reference,notes) SELECT parser_output_id,field_name,proposed_value,"
            "confidence_score,evidence_source_type,evidence_reference,notes "
            "FROM parser_proposal_field_evidence WHERE parser_output_id=? LIMIT 1",
            (child,),
        )
    c.rollback()
    assert _cancel_all_rows(c) == before


@pytest.mark.parametrize(
    "table,column",
    [("parser_proposal_events", "event_payload"), ("financial_audit_events", "event_payload_json")],
)
def test_targeted_malformed_reserved_protocol_json_refuses_before_routing(
    migrated_temp_db_connection, table, column
):
    from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry
    from finance_core.parser_proposals.repository import ParserProposalRepository

    c = migrated_temp_db_connection
    _, provider, published, *_ = publish_initial(c, "targeted-malformed-" + table)
    row = ParserProposalRepository(c).get_by_public_id(published.proposal_public_id)
    full = ParserProposalRepository(c).get_lineage_row_by_id(row["id"])
    c.execute("PRAGMA ignore_check_constraints=ON")
    try:
        _tamper(c, table, "UPDATE " + table + " SET " + column + "=?", ('{"publication_id":',))
    finally:
        c.execute("PRAGMA ignore_check_constraints=OFF")
    assert c.execute("PRAGMA ignore_check_constraints").fetchone()[0] == 0
    before, calls = _cancel_all_rows(c), provider.calls
    from finance_core.financial_audit import AuditVerificationError

    expected = AuditVerificationError if table == "financial_audit_events" else AiInvocationError
    with pytest.raises(expected, match="invalid JSON|protocol marker JSON malformed"):
        has_independent_ai_ancestry(c, full)
    assert _cancel_all_rows(c) == before and provider.calls == calls
    assert (
        not c.in_transaction and c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    )


def test_targeted_native_legacy_lifecycle_clock_rollover_keeps_actual_owner(tmp_path):
    from test_application_amendment_ai_v1 import _create_sealed_ai_root

    from finance_core.application.review import get_proposal_review
    from finance_core.parser_proposals.independent_ai_lineage import has_independent_ai_ancestry

    c, _, parent = _create_sealed_ai_root(tmp_path)
    try:
        # SQL CURRENT_TIMESTAMP is evaluated on each INSERT step. These two
        # genuine writer events may straddle a second without changing owner.
        _tamper(
            c,
            "parser_proposal_events",
            "UPDATE parser_proposal_events SET created_at=CASE event_type "
            "WHEN 'superseded' THEN '2026-10-10 05:00:00' "
            "WHEN 'created' THEN '2026-10-10 05:00:01' ELSE created_at END",
        )
        before = _cancel_all_rows(c)
        assert has_independent_ai_ancestry(c, parent) is False
        assert get_proposal_review(c, parent["public_id"])["proposal_origin"] == "ai_fallback"
        assert _cancel_all_rows(c) == before
        assert c.execute("SELECT count(*) FROM independent_ai_attempts").fetchone()[0] == 0
    finally:
        c.close()


def test_targeted_review_diagnostics_before_missing_ai_port_never_publish_amendment(
    migrated_temp_db_connection,
):
    from finance_core.application.amendment_contract import AmendmentError

    c = migrated_temp_db_connection
    _, provider, published, *_ = publish_initial(c, "targeted-missing-ai-port")
    human = amendment(c, None)
    before, calls = _cancel_all_rows(c), provider.calls
    with pytest.raises(AmendmentError, match="fixed authenticated lineage authority"):
        human.prepare(published.proposal_public_id)
    assert _cancel_all_rows(c) == before and provider.calls == calls, (
        "STOP_EVIDENCE_OR_PROVIDER_MUTATION"
    )
    assert not c.in_transaction
    assert c.execute("SELECT count(*) FROM application_amendment_reviews").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0, (
        "STOP_ECONOMIC_EVENT_COUNT"
    )
