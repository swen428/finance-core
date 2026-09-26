"""One-step capture recovery over synthetic, temporary SQLite ledgers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core import posting_authority as posting_authority_module
from finance_core.intake.capture_jobs import get_capture_job
from finance_core.openclaw_staging_bridge import capture_review, guided_edit, identity
from finance_core.openclaw_staging_bridge.capture_recovery import (
    CaptureRecoveryUnavailable,
    get_capture_recovery,
    resume_capture_recovery,
)
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.posting_authority import confirm_and_post
from tests.test_d3_capture_result_outbox_v1 import _committed_initial
from tests.test_d3_capture_review_v1 import _activate_review
from tests.test_d3_interaction_routes_v1 import _request
from tests.test_migration_042_s5e_ai_fallback_provenance_foundation_v1 import (
    HASH,
    insert_attempt,
    insert_claim,
    insert_result,
)
from tests.test_openclaw_staging_bridge_guided_edit_v1 import (
    _apply,
    _apply_whole_card,
    _begin,
    _capture_routed_message,
    _draft_from_redemption,
    _whole_card_text,
)

CONTEXT = HumanActionContext("111", "finance", "111", "bind-1")


def _captured_route(workspace: support.BridgeWorkspace, text: str = "Lunch SGD 12.50") -> str:
    captured = support.run_cli(_request(workspace, text))
    assert captured.exit_code == 0, captured.response
    return str(captured.response["result"]["capture_job"]["public_id"])


def test_query_authenticates_full_identity_and_frozen_route(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _captured_route(workspace)
    with support.open_database(workspace) as conn:
        before = conn.total_changes
        view = get_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert view["route_kind"] == "initial_intake"
        assert view["interaction_outcome"] == "initial_intake"
        assert view["financial_state"] == "unposted"
        assert view["next_action"] == "capture_processing_required"
        assert conn.total_changes == before
        with pytest.raises(CaptureRecoveryUnavailable, match="identity"):
            get_capture_recovery(
                conn,
                job_public_id=job_id,
                context=HumanActionContext("111", "finance", "111", "different-binding"),
            )


def test_unknown_ai_never_reinvokes_or_prepares_card(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _captured_route(workspace)
    with support.open_database(workspace) as conn:
        conn.execute(
            "UPDATE finance_capture_jobs SET ai_status = 'outcome_unknown' WHERE public_id = ?",
            (job_id,),
        )
        conn.commit()
        view = resume_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert view["next_action"] == "ai_outcome_unknown"
        assert view["performed_action"] == "none"
        assert conn.execute("SELECT COUNT(*) FROM d2_posting_reviews").fetchone()[0] == 0


def test_committed_posting_restarts_and_enqueues_once(tmp_path: Path) -> None:
    conn, job_id, review_id, context, posted = _committed_initial(tmp_path)
    try:
        source = conn.execute(
            "SELECT r.raw_input, r.source_message_id FROM finance_capture_jobs j "
            "JOIN raw_intake_records r ON r.id = j.raw_intake_record_id "
            "WHERE j.public_id = ?",
            (job_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO finance_capture_interaction_routes "
            "(job_public_id, route_kind, raw_text_sha256, authenticated_actor_id, "
            "telegram_account_id, telegram_conversation_id, conversation_binding_id, "
            "telegram_message_id) VALUES (?, 'initial_intake', ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                hashlib.sha256(str(source["raw_input"]).encode()).hexdigest(),
                context.actor_id,
                context.account_id,
                context.conversation_id,
                context.binding_id,
                int(source["source_message_id"]),
            ),
        )
        conn.commit()
        before = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert before["review_public_id"] == review_id
        assert before["financial_state"] == "finalized"
        assert before["result"]["result_public_id"] == posted.transaction_public_id
        assert before["next_action"] == "enqueue_existing_result"
        first = resume_capture_recovery(conn, job_public_id=job_id, context=context)
        assert first["performed_action"] == "enqueue_existing_result"
        assert first["next_action"] == "none"
        assert len(first["reply_outbox"]) == 1
        stale = resume_capture_recovery(
            conn, job_public_id=job_id, context=context, expected_view=before
        )
        assert stale["performed_action"] == "none"
        assert stale["stale_recovery_step"] is True
        assert stale["next_action"] == "none"
        assert (
            resume_capture_recovery(conn, job_public_id=job_id, context=context)["performed_action"]
            == "none"
        )
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_reply_outbox").fetchone()[0] == 1
    finally:
        conn.close()


def test_initial_review_is_restricted_to_original_bound_proposal(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _captured_route(workspace, "Lunch SGD 12.50 at ExampleCafe paid by Owner")
    processed = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert processed.exit_code == 0, processed.response
    with support.open_database(workspace) as conn:
        job = get_capture_job(conn, public_id=job_id)
        assert job is not None
        proposal = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
            (job["proposal_public_id"],),
        ).fetchone()
        payload = json.loads(str(proposal[0]))
        payload["transaction_date"] = "2026-09-26"
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload = ? WHERE public_id = ?",
            (json.dumps(payload, sort_keys=True), job["proposal_public_id"]),
        )
        conn.commit()
        view = get_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert view["next_action"] == "prepare_initial_review"
        resumed = resume_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert resumed["performed_action"] == "prepare_initial_review"
        assert conn.execute("SELECT COUNT(*) FROM d2_initial_proposal_cards").fetchone()[0] == 1


def test_guided_control_outcome_is_separate_from_source_finance(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    _proposal, session, _redemption = _begin(workspace)
    _capture_routed_message(workspace, "merchant=Cafe Two", 21)
    context = HumanActionContext("111", "finance-account", "111", "binding-1")
    with support.open_database(workspace) as conn:
        job_id = str(
            conn.execute(
                "SELECT job_public_id FROM finance_capture_interaction_routes "
                "WHERE guided_session_public_id = ? AND telegram_message_id = 21",
                (session,),
            ).fetchone()[0]
        )
        pending = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert pending["route_kind"] == "guided_update"
        assert pending["interaction_outcome"] == "pending"
        assert pending["financial_state"] == "unposted"
        assert pending["source_job_public_id"] != job_id
        assert pending["next_action"] == "guided_command_required"
    applied = _apply(workspace, session, 21, "merchant", "Cafe Two")
    assert applied.exit_code == 0, applied.response
    with support.open_database(workspace) as conn:
        after = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert after["interaction_outcome"] == "update_applied"
        assert after["financial_state"] == "unposted"
        assert after["next_action"] == "none"


def test_guided_committed_edit_with_lost_ack_still_requests_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    _proposal, session, _redemption = _begin(workspace)
    _capture_routed_message(workspace, "category=Meals", 30)
    original = guided_edit.record_update_applied

    def lose_ack(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic crash after authoritative edit commit")

    monkeypatch.setattr(guided_edit, "record_update_applied", lose_ack)
    unknown = _apply(workspace, session, 30, "category", "Meals")
    assert unknown.exit_code != 0
    monkeypatch.setattr(guided_edit, "record_update_applied", original)
    context = HumanActionContext("111", "finance-account", "111", "binding-1")
    with support.open_database(workspace) as conn:
        job_id = str(
            conn.execute(
                "SELECT job_public_id FROM finance_capture_interaction_routes "
                "WHERE guided_session_public_id = ? AND telegram_message_id = 30",
                (session,),
            ).fetchone()[0]
        )
        view = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert view["interaction_outcome"] == "update_requested"
        assert view["next_action"] == "guided_command_required"
        assert (
            conn.execute(
                "SELECT pending_message_id FROM openclaw_guided_edit_sessions "
                "WHERE session_public_id = ?",
                (session,),
            ).fetchone()[0]
            == 30
        )


def test_d1_control_job_keeps_source_job_and_child_separate(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    card_id = str(initial["card_generation_public_id"])
    _capture_routed_message(workspace, _whole_card_text(card_id), 30)
    context = HumanActionContext("111", "finance-account", "111", "binding-1")
    with support.open_database(workspace) as conn:
        job_id = str(
            conn.execute(
                "SELECT job_public_id FROM finance_capture_interaction_routes "
                "WHERE card_generation_public_id = ? AND telegram_message_id = 30",
                (card_id,),
            ).fetchone()[0]
        )
        pending = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert pending["interaction_outcome"] == "pending"
        assert pending["next_action"] == "d1_command_required"
        assert pending["source_job_public_id"] != job_id
        assert get_capture_job(conn, public_id=job_id)["proposal_public_id"] is None
    applied = _apply_whole_card(workspace, card_id)
    assert applied.exit_code == 0, applied.response
    from_bridge = support.run_cli(
        support.make_request(
            "get_capture_recovery",
            {
                "workspace_path": str(workspace.workspace_path),
                "job_public_id": job_id,
                "operator_actor_id": "111",
                "telegram_account_id": "finance-account",
                "telegram_conversation_id": "111",
                "conversation_binding_id": "binding-1",
            },
        )
    )
    assert from_bridge.exit_code == 0, from_bridge.response
    assert from_bridge.response["result"]["interaction_result"]["card_generation_public_id"]
    with support.open_database(workspace) as conn:
        after = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert after["interaction_outcome"] == "accepted"
        assert after["financial_state"] == "unposted"
        assert after["next_action"] == "prepare_child_review"
        assert (
            after["interaction_result"]["card_generation_public_id"]
            == applied.response["result"]["card_generation_public_id"]
        )
        prepared = resume_capture_recovery(conn, job_public_id=job_id, context=context)
        assert prepared["performed_action"] == "prepare_child_review"
        assert prepared["review_public_id"] is not None
        assert get_capture_job(conn, public_id=job_id)["proposal_public_id"] is None
    second = _apply_whole_card(
        workspace,
        str(applied.response["result"]["card_generation_public_id"]),
        message_id=31,
        merchant="New Cafe",
    )
    assert second.exit_code == 0, second.response
    with support.open_database(workspace) as conn:
        source_id = str(prepared["source_job_public_id"])
        current = get_capture_recovery(conn, job_public_id=source_id, context=context)
        assert current["next_action"] == "prepare_child_review"
        assert (
            current["child_review_target"] == second.response["result"]["card_generation_public_id"]
        )


def test_accepted_d2_attempt_resumes_without_new_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    captured = support.run_cli(
        _request(
            workspace,
            "Lunch SGD 12.50 at ExampleCafe paid by Owner",
            account="synthetic-account",
            binding="synthetic-binding",
        )
    )
    assert captured.exit_code == 0
    processed = support.process_captured_text(workspace, captured)
    assert processed.exit_code == 0
    job_id = str(processed.response["result"]["capture_job"]["public_id"])
    with support.open_database(workspace) as conn:
        job = get_capture_job(conn, public_id=job_id)
        assert job is not None
        proposal = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
            (job["proposal_public_id"],),
        ).fetchone()
        payload = json.loads(str(proposal[0]))
        payload["transaction_date"] = "2026-09-26"
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload = ? WHERE public_id = ?",
            (json.dumps(payload, sort_keys=True), job["proposal_public_id"]),
        )
        conn.commit()
        job, review, _ = capture_review.ensure_capture_review(conn, job_public_id=job_id)
        assert review is not None
        context, key, reference = _activate_review(conn, job=job, review=review)

        def crash_after_accept(stage: str) -> None:
            if stage == "after_confirmation_commit":
                raise RuntimeError("synthetic crash after accepted decision")

        monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", crash_after_accept)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            confirm_and_post(
                conn,
                key=key,
                reference=reference,
                context=context,
                callback_id="d3-recovery-accepted-confirm",
                callback_message_id=54321,
            )
        monkeypatch.setattr(posting_authority_module, "_failure_injection_hook", None)
        before = get_capture_recovery(conn, job_public_id=job_id, context=context)
        assert before["next_action"] == "resume_accepted_posting"
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        after = resume_capture_recovery(conn, job_public_id=job_id, context=context)
        assert after["performed_action"] == "resume_accepted_posting"
        assert after["next_action"] == "enqueue_existing_result"
        assert conn.execute("SELECT COUNT(*) FROM d2_posting_decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_saved_ai_child_prepares_review_without_rebinding_capture_job(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _captured_route(workspace, "Lunch SGD 12.50 at ExampleCafe paid by Owner")
    processed = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert processed.exit_code == 0, processed.response
    with support.open_database(workspace) as conn:
        job = get_capture_job(conn, public_id=job_id)
        assert job is not None
        parent = conn.execute(
            "SELECT id, parsed_payload FROM parser_outputs WHERE public_id = ?",
            (job["proposal_public_id"],),
        ).fetchone()
        attempt_id = insert_attempt(conn, int(parent["id"]), int(job["raw_intake_record_id"]), "c")
        conn.commit()
        prepared_attempt = get_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert prepared_attempt["next_action"] == "ai_prepared_attention"
        claim_id = insert_claim(conn, attempt_id, "d")
        conn.commit()
        unknown = get_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert unknown["next_action"] == "ai_outcome_unknown"
        result_id = insert_result(
            conn,
            attempt_id,
            claim_id,
            "e",
            transport_outcome="response_received",
            result_status="proposal_created",
            recovery_disposition=None,
        )
        conn.commit()
        unlinked = get_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert unlinked["next_action"] == "ai_child_lineage_attention"
        payload = json.loads(str(parent["parsed_payload"]))
        payload["transaction_date"] = "2026-09-26"
        child_id = conn.execute(
            "INSERT INTO parser_outputs "
            "(public_id, source_type, source_public_id, parser_name, "
            "parser_version, raw_text, parsed_payload, normalized_payload, "
            "parse_status, parent_parser_output_id) "
            "VALUES ('synthetic_ai_child_d3', 'telegram_text', 'synthetic_ai_source_d3', "
            "'ai-fallback', 'v1', 'child', ?, ?, 'parsed_pending_confirmation', ?)",
            (json.dumps(payload), json.dumps(payload), int(parent["id"])),
        ).lastrowid
        conn.execute(
            "INSERT INTO ai_fallback_proposal_links "
            "(link_public_id, link_material_hash, result_id, parser_output_id, "
            "proposal_version, effective_content_hash) VALUES (?, ?, ?, ?, 0, ?)",
            ("aipl_" + "f" * 64, "a" * 64, result_id, child_id, HASH),
        )
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
            (child_id, job["raw_intake_record_id"]),
        )
        conn.commit()
        before = get_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert before["next_action"] == "prepare_child_review"
        assert before["child_review_target"] == "synthetic_ai_child_d3"
        prepared = resume_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert prepared["performed_action"] == "prepare_child_review"
        assert prepared["review_public_id"] is not None
        assert (
            get_capture_job(conn, public_id=job_id)["proposal_public_id"]
            == job["proposal_public_id"]
        )


def test_non_child_ai_result_never_becomes_a_review(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _captured_route(workspace, "Lunch SGD 12.50 at ExampleCafe paid by Owner")
    processed = support.run_cli(
        support.make_request(
            "process_capture_job",
            {"workspace_path": str(workspace.workspace_path), "job_public_id": job_id},
            idempotency_key="fcp_"
            + identity.canonical_digest("finance-process-capture-job-v1", job_id),
        )
    )
    assert processed.exit_code == 0, processed.response
    with support.open_database(workspace) as conn:
        job = get_capture_job(conn, public_id=job_id)
        assert job is not None
        parent_id = conn.execute(
            "SELECT id FROM parser_outputs WHERE public_id = ?",
            (job["proposal_public_id"],),
        ).fetchone()[0]
        attempt_id = insert_attempt(conn, parent_id, job["raw_intake_record_id"], "c")
        claim_id = insert_claim(conn, attempt_id, "d")
        insert_result(
            conn,
            attempt_id,
            claim_id,
            "e",
            transport_outcome="response_received",
            result_status="classification_only",
            recovery_disposition=None,
            non_child_reason="intent_unproven",
        )
        conn.commit()
        view = resume_capture_recovery(conn, job_public_id=job_id, context=CONTEXT)
        assert view["next_action"] == "ai_non_child_result"
        assert view["ai_result_status"] == "classification_only"
        assert view["performed_action"] == "none"
        assert conn.execute("SELECT COUNT(*) FROM d2_posting_reviews").fetchone()[0] == 0
