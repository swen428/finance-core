"""Bridge recovery commands against only synthetic staging workspaces."""

from __future__ import annotations

from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest
from test_receipt_ocr_evidence import FakeEngine

from finance_core.application.corrections import CorrectionService
from finance_core.correction_adapters import local_authority
from finance_core.correction_adapters.d2_source import D2OriginalSourceVerifier
from finance_core.correction_adapters.local_authority import LocalApprovalAuthority
from finance_core.correction_adapters.policy import open_local_authority_connection, provision
from finance_core.openclaw_staging_bridge import (
    capture_review,
    commands,
    errors,
    guided_edit,
    identity,
)
from finance_core.posting_authority import confirm_and_post
from tests.test_correction_adapters_d2_source import _Terminal
from tests.test_d3_capture_review_v1 import _activate_review, _capture_processed_text
from tests.test_openclaw_staging_bridge_guided_edit_v1 import (
    _apply,
    _begin,
    _capture_routed_message,
    _draft_from_redemption,
    _whole_card_text,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _context(workspace: support.BridgeWorkspace, job_id: str) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "job_public_id": job_id,
        "operator_actor_id": "111",
        "telegram_account_id": "synthetic-account",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "synthetic-binding",
    }


def _capture(workspace: support.BridgeWorkspace, text: str) -> str:
    update = support.telegram_text_update(text)
    arguments = support.authenticated_text_capture_arguments(
        workspace, update, account_id="synthetic-account", binding_id="synthetic-binding"
    )
    outcome = support.run_cli(
        support.make_request(
            "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=10)
        )
    )
    assert outcome.exit_code == 0, outcome.response
    return str(outcome.response["result"]["capture_job"]["public_id"])


def _resume(workspace: support.BridgeWorkspace, job_id: str) -> support.CliOutcome:
    viewed = support.run_cli(
        support.make_request("get_capture_recovery", _context(workspace, job_id))
    )
    assert viewed.exit_code == 0, viewed.response
    token = str(viewed.response["result"]["recovery_step_token"])
    return support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**_context(workspace, job_id), "recovery_step_token": token},
            idempotency_key=commands._capture_recovery_key(job_id, token),
        )
    )


def test_known_initial_capture_can_resume_local_processing_without_new_fact(
    workspace: support.BridgeWorkspace,
) -> None:
    job_id = _capture(workspace, "lunch 12.50")
    before = support.run_cli(
        support.make_request("get_capture_recovery", _context(workspace, job_id))
    )
    assert before.exit_code == 0, before.response
    assert before.response["result"]["next_action"] == "capture_processing_required"
    resumed = _resume(workspace, job_id)
    assert resumed.exit_code == 0, resumed.response
    assert resumed.response["result"]["performed_action"] == "process_capture_job"
    assert resumed.response["result"]["source_job_public_id"] == job_id
    refreshed = support.run_cli(
        support.make_request("get_capture_recovery", _context(workspace, job_id))
    )
    assert (
        resumed.response["result"]["recovery_step_token"]
        == refreshed.response["result"]["recovery_step_token"]
    )
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_receipt_resume_uses_local_ocr_and_preserves_source(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.write_handoff_file(workspace, "d3-recover.jpg", support.JPEG_BYTES)
    capture_arguments = support.capture_receipt_arguments(
        workspace, handoff_filename="d3-recover.jpg"
    )
    capture_arguments.update(
        authenticated_actor_id="111",
        telegram_account_id="finance-account",
        telegram_conversation_id="111",
        conversation_binding_id="binding-1",
    )
    captured = support.run_cli(
        support.make_request(
            "capture",
            capture_arguments,
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    assert captured.exit_code == 0, captured.response
    job_id = str(captured.response["result"]["capture_job"]["public_id"])
    engine = FakeEngine()
    monkeypatch.setattr(commands, "build_ocr_engine", lambda _workspace: engine)
    arguments = {
        **_context(workspace, job_id),
        "telegram_account_id": "finance-account",
        "conversation_binding_id": "binding-1",
    }
    viewed = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert viewed.exit_code == 0, viewed.response
    assert viewed.response["result"]["next_action"] == "capture_processing_required"
    token = str(viewed.response["result"]["recovery_step_token"])
    resumed = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**arguments, "recovery_step_token": token},
            idempotency_key=commands._capture_recovery_key(job_id, token),
        )
    )
    assert resumed.exit_code == 0, resumed.response
    assert resumed.response["result"]["performed_action"] == "process_capture_job"
    assert engine.calls == 1
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_recovery_rejects_wrong_authenticated_binding(
    workspace: support.BridgeWorkspace,
) -> None:
    job_id = _capture(workspace, "lunch 12.50")
    arguments = _context(workspace, job_id)
    arguments["conversation_binding_id"] = "other-binding"
    denied = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert denied.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert denied.response["error"]["code"] == errors.ACTOR_MISMATCH
    denied_resume = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**arguments, "recovery_step_token": "d3step_" + "0" * 64},
            idempotency_key=commands._capture_recovery_key(job_id, "d3step_" + "0" * 64),
        )
    )
    assert denied_resume.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert denied_resume.response["error"]["code"] == errors.ACTOR_MISMATCH
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_resume_key_must_bind_one_job(workspace: support.BridgeWorkspace) -> None:
    job_id = _capture(workspace, "lunch 12.50")
    viewed = support.run_cli(
        support.make_request("get_capture_recovery", _context(workspace, job_id))
    )
    assert viewed.exit_code == 0
    token = str(viewed.response["result"]["recovery_step_token"])
    denied = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**_context(workspace, job_id), "recovery_step_token": token},
            idempotency_key="bridge-d3-recover:wrong",
        )
    )
    assert denied.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert denied.response["error"]["code"] == errors.IDEMPOTENCY_CONFLICT
    assert commands._capture_recovery_key(job_id, token).startswith("bridge-d3-recover:")
    assert identity.canonical_digest("finance-capture-recovery-v1", job_id)


def test_resume_requires_a_well_formed_step_token(workspace: support.BridgeWorkspace) -> None:
    job_id = _capture(workspace, "lunch 12.50")
    missing = support.run_cli(
        support.make_request(
            "resume_capture_recovery", _context(workspace, job_id), idempotency_key="bad"
        )
    )
    assert missing.exit_code == errors.EXIT_VALIDATION_REFUSED
    assert missing.response["error"]["code"] == errors.ARGUMENTS_REFUSED
    malformed = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**_context(workspace, job_id), "recovery_step_token": "d3step_bad"},
            idempotency_key="bad",
        )
    )
    assert malformed.exit_code == errors.EXIT_VALIDATION_REFUSED
    assert malformed.response["error"]["code"] == errors.ARGUMENTS_REFUSED


def test_refused_control_remains_refused_after_recovery(
    workspace: support.BridgeWorkspace,
) -> None:
    job_id = _capture(workspace, "完成")
    viewed = support.run_cli(
        support.make_request("get_capture_recovery", _context(workspace, job_id))
    )
    assert viewed.exit_code == 0, viewed.response
    state = viewed.response["result"]
    assert state["route_kind"] == "control_refused"
    assert state["interaction_outcome"] == "refused"
    assert state["result"] is None
    resumed = _resume(workspace, job_id)
    assert resumed.exit_code == 0, resumed.response
    assert resumed.response["result"]["performed_action"] == "none"
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_corrected_result_reopens_exact_policy_database_and_recovers_one_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "owner_runtime"
    (runtime_root / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root.resolve()))
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _capture_processed_text(workspace, eligible_proposal=True)
    with support.open_database(workspace) as conn:
        job, review, _ = capture_review.ensure_capture_review(conn, job_public_id=job_id)
        assert review is not None
        context, key, reference = _activate_review(conn, job=job, review=review)
        posted = confirm_and_post(
            conn,
            key=key,
            reference=reference,
            context=context,
            callback_id="d3-recovery-corrected-confirm",
            callback_message_id=54321,
        )
        assert posted.state == "finalized"
    workspace.database_path.chmod(0o600)
    provision(workspace.database_path, "111")
    authority = LocalApprovalAuthority()
    original_token_hex = local_authority.secrets.token_hex
    nonces = iter(("2" * 64, "3" * 64))
    monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: next(nonces))
    with open_local_authority_connection() as trusted:
        service = CorrectionService(trusted, D2OriginalSourceVerifier(), authority)
        plan = service.preview(str(posted.transaction_public_id), {"amount": "13.50"}, "fix")
        signed = authority.sign_with_terminal(
            plan,
            input_stream=_Terminal(f"CONFIRM {plan.plan_id} {'2' * 64}\n"),
            output_stream=_Terminal(),
        )
        applied = service.apply(plan.plan_id, signed)
    monkeypatch.setattr(local_authority.secrets, "token_hex", original_token_hex)
    arguments = _context(workspace, job_id)
    viewed = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert viewed.exit_code == 0, viewed.response
    result = viewed.response["result"]
    assert result["result"]["current"]["amount"] == "13.50"
    assert result["financial_state"] == "corrected"
    first_view = support.run_cli(support.make_request("get_capture_recovery", arguments))
    first_token = str(first_view.response["result"]["recovery_step_token"])
    first_request = support.make_request(
        "resume_capture_recovery",
        {**arguments, "recovery_step_token": first_token},
        idempotency_key=commands._capture_recovery_key(job_id, first_token),
    )
    first = support.run_cli(first_request)
    replay = support.run_cli(first_request)
    assert first.exit_code == replay.exit_code == 0
    assert replay.response["result"]["stale_recovery_step"] is True
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM finance_capture_reply_outbox").fetchone()[0] == 1
    second = _resume(workspace, job_id)
    assert second.exit_code == 0
    assert second.response["result"]["result"]["current"]["amount"] == "13.50"
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        rows = conn.execute(
            "SELECT result_public_id FROM finance_capture_reply_outbox ORDER BY result_kind"
        ).fetchall()
        assert {row[0] for row in rows} == {
            str(posted.transaction_public_id),
            applied.applied.correction_id,
        }
    wrong_actor = {
        **arguments,
        "operator_actor_id": "222",
        "telegram_conversation_id": "222",
    }
    actor_refused = support.run_cli(support.make_request("get_capture_recovery", wrong_actor))
    assert actor_refused.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert actor_refused.response["error"]["code"] == errors.LIFECYCLE_CONFLICT
    copied = support.create_bridge_workspace(tmp_path, name="copied-ledger")
    with support.open_database(workspace) as source, support.open_database(copied) as target:
        source.backup(target)
    copied_arguments = {**arguments, "workspace_path": str(copied.workspace_path)}
    refused = support.run_cli(support.make_request("get_capture_recovery", copied_arguments))
    assert refused.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == errors.STAGING_REFUSED

    other_runtime = tmp_path / "other_owner_runtime"
    (other_runtime / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(other_runtime.resolve()))
    other_workspace = support.create_bridge_workspace(tmp_path, name="other-policy-ledger")
    other_workspace.database_path.chmod(0o600)
    provision(other_workspace.database_path, "111")
    wrong_policy = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert wrong_policy.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert wrong_policy.response["error"]["code"] == errors.LIFECYCLE_CONFLICT
    missing_runtime = tmp_path / "missing_policy_runtime"
    (missing_runtime / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(missing_runtime.resolve()))
    missing_policy = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert missing_policy.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert missing_policy.response["error"]["code"] == errors.LIFECYCLE_CONFLICT


def test_guided_business_commit_before_settlement_replays_same_operation(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proposal, session, _redemption = _begin(workspace)
    _capture_routed_message(workspace, "merchant=Cafe Two", 21)
    original_record = guided_edit.record_update_applied

    def fail_after_business_commit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic crash before guided settlement")

    monkeypatch.setattr(guided_edit, "record_update_applied", fail_after_business_commit)
    crashed = _apply(workspace, session, 21, "merchant", "Cafe Two")
    assert crashed.exit_code == errors.EXIT_INTERNAL
    with support.open_database(workspace) as conn:
        job_id = str(
            conn.execute(
                "SELECT job_public_id FROM finance_capture_interaction_routes "
                "WHERE guided_session_public_id = ? AND telegram_message_id = 21",
                (session,),
            ).fetchone()[0]
        )
        assert (
            conn.execute(
                "SELECT pending_message_id FROM openclaw_guided_edit_sessions "
                "WHERE session_public_id = ?",
                (session,),
            ).fetchone()[0]
            == 21
        )
        before_count = conn.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations"
        ).fetchone()[0]
    monkeypatch.setattr(guided_edit, "record_update_applied", original_record)
    arguments = {
        "workspace_path": str(workspace.workspace_path),
        "job_public_id": job_id,
        "operator_actor_id": "111",
        "telegram_account_id": "finance-account",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "binding-1",
    }
    pending = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert pending.exit_code == 0, pending.response
    assert pending.response["result"]["next_action"] == "guided_command_required"
    token = str(pending.response["result"]["recovery_step_token"])
    resumed = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**arguments, "recovery_step_token": token},
            idempotency_key=commands._capture_recovery_key(job_id, token),
        )
    )
    assert resumed.exit_code == 0, resumed.response
    assert resumed.response["result"]["interaction_outcome"] == "update_applied"
    with support.open_database(workspace) as conn:
        assert (
            conn.execute(
                "SELECT pending_message_id FROM openclaw_guided_edit_sessions "
                "WHERE session_public_id = ?",
                (session,),
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
            == before_count
        )


def test_frozen_whole_card_recovery_applies_once_and_returns_original_card(
    workspace: support.BridgeWorkspace,
) -> None:
    _proposal, _session, redemption = _begin(workspace)
    initial = _draft_from_redemption(workspace, redemption)
    card_id = str(initial["card_generation_public_id"])
    route = _capture_routed_message(workspace, _whole_card_text(card_id), 30)
    job_id = str(route["job_public_id"])
    arguments = {
        "workspace_path": str(workspace.workspace_path),
        "job_public_id": job_id,
        "operator_actor_id": "111",
        "telegram_account_id": "finance-account",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "binding-1",
    }
    before = support.run_cli(support.make_request("get_capture_recovery", arguments))
    assert before.exit_code == 0, before.response
    assert before.response["result"]["next_action"] == "d1_command_required"
    token = str(before.response["result"]["recovery_step_token"])
    request = support.make_request(
        "resume_capture_recovery",
        {**arguments, "recovery_step_token": token},
        idempotency_key=commands._capture_recovery_key(job_id, token),
    )
    first = support.run_cli(request)
    assert first.exit_code == 0, first.response
    assert first.response["result"]["interaction_outcome"] == "accepted"
    assert first.response["result"]["interaction_operation_public_id"] == route["operation_key"]
    assert first.response["result"]["route_card_public_id"] == card_id
    assert first.response["result"]["interaction_card_public_id"].startswith("d1card_")
    with support.open_database(workspace) as conn:
        count = conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
    replay = support.run_cli(request)
    assert replay.exit_code == 0, replay.response
    assert replay.response["result"]["stale_recovery_step"] is True
    with support.open_database(workspace) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
            == count
        )
    assert first.response["result"]["next_action"] == "prepare_child_review"
    second_token = str(first.response["result"]["recovery_step_token"])
    prepared = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**arguments, "recovery_step_token": second_token},
            idempotency_key=commands._capture_recovery_key(job_id, second_token),
        )
    )
    assert prepared.exit_code == 0, prepared.response
    assert prepared.response["result"]["performed_action"] == "prepare_child_review"
    assert prepared.response["result"]["review_public_id"]
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM d2_posting_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
