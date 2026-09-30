"""Managed S1D2 recovery and status proofs over disposable synthetic profiles."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

import pytest
from s1d1_managed_test_support import create_posted_managed_transaction

from finance_core import posting_authority
from finance_core.application.capture_processing import process_claimed_capture_job
from finance_core.intake.capture_jobs import claim_capture_job
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.openclaw_staging_bridge import (
    capture_review,
    commands,
    envelope,
    errors,
    human_actions,
    workspace_access,
)
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    claim_ai_fallback_invocation_v2,
    prepare_ai_fallback_v2,
    record_ai_fallback_result_v2,
)
from finance_core.receipt_staging_runner.workspace import generate_delivery_receipt_signing_key
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    record_telegram_source_context,
    require_telegram_source_context,
)
from tests import s1cc_managed_test_support as s1cc
from tests import test_ai_model_compatibility_receipts_v2 as ai_v2_tests
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests import test_s1d1_managed_correction_delivery as s1d1
from tests.test_d2_initial_card_delivery_authority_v1 import _record_delivery

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)


def _managed_context(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    actor_id: str = s1ca.ACTOR,
    account_id: str = s1ca.ACCOUNT,
    conversation_id: str = s1ca.CONVERSATION,
    binding_id: str = s1ca.BINDING,
) -> dict[str, object]:
    return s1ca._context(
        workspace,
        actor_id=actor_id,
        account_id=account_id,
        conversation_id=conversation_id,
        binding_id=binding_id,
    )


def _status_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    intake_id: str,
    *,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    return s1ca._request(
        workspace,
        envelope.COMMAND_GET_AI_PROCESSING_STATUS_V2,
        {**(context or _managed_context(workspace)), "intake_public_id": intake_id},
    )


def _recovery_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    command: str,
    job_id: str,
    *,
    token: str | None = None,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    arguments = {**(context or _managed_context(workspace)), "job_public_id": job_id}
    key = None
    if token is not None:
        arguments["recovery_step_token"] = token
        key = commands._capture_recovery_key(job_id, token)
    return s1ca._request(workspace, command, arguments, idempotency_key=key)


def _get_recovery(workspace: s1ca.ManagedBridgeWorkspace, job_id: str) -> dict[str, object]:
    outcome = s1ca._run(
        workspace,
        _recovery_request(workspace, envelope.COMMAND_GET_CAPTURE_RECOVERY, job_id),
        expected_sessions=1,
    )
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    return outcome.response["result"]


def _resume_request(
    workspace: s1ca.ManagedBridgeWorkspace,
    job_id: str,
    view: dict[str, object],
) -> dict[str, object]:
    token = str(view["recovery_step_token"])
    return _recovery_request(
        workspace,
        envelope.COMMAND_RESUME_CAPTURE_RECOVERY,
        job_id,
        token=token,
    )


def _seed_early_intake(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    message_id: str = "77",
    bad_digest: bool = False,
) -> str:
    """Persist only a raw synthetic Telegram text intake and frozen context."""
    source = TelegramSourceContext(
        authenticated_actor_id=s1ca.ACTOR,
        account_id=s1ca.ACCOUNT,
        conversation_id=s1ca.CONVERSATION,
        binding_id=s1ca.BINDING,
        message_id=message_id,
    )
    received_at = "2026-10-01T00:00:00Z"
    metadata = {
        "chat_id": s1ca.CONVERSATION,
        "message_id": message_id,
        "source_received_at": received_at,
    }
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s1d2-seed-early-intake"
    ) as conn:
        intake = create_raw_intake_record(
            conn,
            "synthetic early status intake",
            source_channel="telegram",
            source_metadata=metadata,
            received_at=received_at,
        )
        if bad_digest:
            # First insertion is intentionally inconsistent but schema-valid.
            # The source row remains immutable; no trigger is disabled or row
            # repaired after insertion.
            conn.execute(
                """
                INSERT INTO d2_telegram_source_contexts (
                    raw_intake_record_id, authenticated_actor_id,
                    telegram_account_id, telegram_conversation_id,
                    conversation_binding_id, source_message_id,
                    source_identity_sha256, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(intake["id"]),
                    source.authenticated_actor_id,
                    source.account_id,
                    source.conversation_id,
                    source.binding_id,
                    source.message_id,
                    "f" * 64,
                    received_at,
                ),
            )
        else:
            record_telegram_source_context(
                conn,
                raw_intake_record_id=int(intake["id"]),
                context=source,
                captured_at=received_at,
            )
        conn.commit()
    return str(intake["public_id"])


def _job_for_proposal(workspace: s1ca.ManagedBridgeWorkspace, proposal_id: str) -> str:
    row = s1ca._read_one(
        workspace,
        """
        SELECT job.public_id
        FROM finance_capture_jobs AS job
        JOIN raw_intake_records AS intake ON intake.id = job.raw_intake_record_id
        JOIN parser_outputs AS proposal ON proposal.id = intake.parser_output_id
        WHERE proposal.public_id = ?
        """,
        (proposal_id,),
    )
    assert row is not None
    return str(row[0])


def _bind_seeded_proposal_to_job(
    workspace: s1ca.ManagedBridgeWorkspace,
    *,
    job_id: str,
    proposal_id: str,
) -> None:
    """Run the ordinary text job service over an already seeded proposal."""
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s1d2-bind-text-proposal"
    ) as conn:
        lease = claim_capture_job(
            conn,
            public_id=job_id,
            owner="test-s1d2-proposal-binding",
        )
        job = process_claimed_capture_job(conn, lease=lease, engine=None)
        assert job["status"] == "processing"
        assert job["proposal_public_id"] == proposal_id
        assert job["lease_owner"] is None


def test_managed_ai_status_reports_source_bound_early_no_model(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    intake_id = _seed_early_intake(managed_workspace)
    before = s1ca._read_one(
        managed_workspace,
        "SELECT COUNT(*) FROM parser_outputs WHERE source_public_id = ?",
        (intake_id,),
    )
    assert before is not None and int(before[0]) == 0

    status = s1ca._run(
        managed_workspace,
        _status_request(managed_workspace, intake_id),
        expected_sessions=1,
    )
    assert status.exit_code == errors.EXIT_OK, status.response
    assert status.response["result"]["intake_public_id"] == intake_id
    assert status.response["result"]["processing_path"] == "no_model"
    assert status.response["result"]["attempt_public_id"] is None
    assert status.response["result"]["receipt_public_id"] is None
    assert managed_workspace.sessions[-1].total_changes == 0

    missing_target = s1ca._run(
        managed_workspace,
        _status_request(managed_workspace, "synthetic-missing-intake"),
        expected_sessions=1,
    )
    assert missing_target.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert missing_target.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    for mismatched_context in (
        _managed_context(managed_workspace, account_id=s1ca.OTHER_ACCOUNT),
        _managed_context(managed_workspace, binding_id=s1ca.OTHER_BINDING),
    ):
        unavailable = s1ca._run(
            managed_workspace,
            _status_request(managed_workspace, intake_id, context=mismatched_context),
            expected_sessions=1,
        )
        assert unavailable.exit_code == errors.EXIT_AUTHORITY_REFUSED
        assert unavailable.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
        assert unavailable.response["error"] == missing_target.response["error"]
        assert "result" not in unavailable.response
        assert intake_id not in json.dumps(unavailable.response, sort_keys=True)
        assert managed_workspace.sessions[-1].total_changes == 0

    for invalid_context in (
        _managed_context(managed_workspace, actor_id=s1ca.OTHER_ACTOR),
        _managed_context(managed_workspace, conversation_id="222"),
    ):
        before_sessions = len(managed_workspace.sessions)
        invalid = s1ca._run(
            managed_workspace,
            _status_request(managed_workspace, intake_id, context=invalid_context),
            expected_sessions=0,
        )
        assert invalid.exit_code == errors.EXIT_AUTHORITY_REFUSED
        assert invalid.response["error"]["code"] == errors.ACTOR_MISMATCH
        assert "result" not in invalid.response
        assert intake_id not in json.dumps(invalid.response, sort_keys=True)
        assert len(managed_workspace.sessions) == before_sessions

    malformed_context = _managed_context(managed_workspace)
    malformed_context.pop("conversation_binding_id")
    before_sessions = len(managed_workspace.sessions)
    malformed = s1ca._run(
        managed_workspace,
        _status_request(managed_workspace, intake_id, context=malformed_context),
        expected_sessions=0,
    )
    assert malformed.exit_code == errors.EXIT_VALIDATION_REFUSED
    assert len(managed_workspace.sessions) == before_sessions


@pytest.mark.parametrize(
    ("message_id", "bad_digest"),
    (("0077", False), ("77", True)),
)
def test_early_status_refuses_noncanonical_message_or_frozen_digest_drift(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    message_id: str,
    bad_digest: bool,
) -> None:
    intake_id = _seed_early_intake(
        managed_workspace,
        message_id=message_id,
        bad_digest=bad_digest,
    )
    stored_source = s1ca._read_one(
        managed_workspace,
        """
        SELECT intake.source_message_id, intake.external_source_id,
               source.source_identity_sha256
        FROM raw_intake_records AS intake
        JOIN d2_telegram_source_contexts AS source
          ON source.raw_intake_record_id = intake.id
        WHERE intake.public_id = ?
        """,
        (intake_id,),
    )
    assert stored_source is not None
    assert stored_source[0] == message_id
    assert stored_source[1] == f"telegram:{s1ca.CONVERSATION}:{message_id}"
    if bad_digest:
        assert stored_source[2] == "f" * 64
    else:
        assert stored_source[2] != "f" * 64
    unavailable = s1ca._run(
        managed_workspace,
        _status_request(managed_workspace, intake_id),
        expected_sessions=1,
    )
    assert unavailable.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert unavailable.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    assert "result" not in unavailable.response
    assert intake_id not in json.dumps(unavailable.response, sort_keys=True)
    assert managed_workspace.sessions[-1].total_changes == 0


@pytest.mark.parametrize(
    ("state", "expected_path", "expected_counts"),
    (
        ("denied", "model_denied", (1, 0, 0, 0)),
        # A prepared attempt is pending before claim; the truthful projection
        # keeps the closed `no_model` path while returning its attempt/receipt.
        ("pending", "no_model", (0, 1, 0, 0)),
        ("unknown", "outcome_unknown", (0, 1, 1, 0)),
        ("finished", "cloud_projection", (0, 1, 1, 1)),
    ),
)
def test_managed_ai_status_projects_denied_pending_unknown_and_finished_evidence(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected_path: str,
    expected_counts: tuple[int, int, int, int],
) -> None:
    _proposal_id, intake_id = s1cc.seed_ai_source(managed_workspace, "text", monkeypatch)
    projection = ai_v2_tests._projection()
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path,
        operation_id=f"test-s1d2-seed-ai-status-{state}",
    ) as conn:
        if state == "denied":
            with pytest.raises(AiFallbackServiceError) as refusal:
                prepare_ai_fallback_v2(
                    conn,
                    intake_public_id=intake_id,
                    config_projection=projection,
                    now_ms=100_000,
                )
            assert refusal.value.code == "AI_MODEL_CONFIG_NOT_ACCEPTED"
        else:
            ai_v2_tests._register(conn)
            prepared = prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_id,
                config_projection=projection,
                now_ms=100_000,
            )
            assert prepared["receipt_public_id"]
            if state in {"unknown", "finished"}:
                claim = claim_ai_fallback_invocation_v2(
                    conn,
                    attempt_public_id=str(prepared["attempt_public_id"]),
                    now_ms=100_001,
                )
                if state == "finished":
                    arguments = s1cc.fallback_response_arguments(
                        claim,
                        version="v2",
                        source_kind="text",
                    )
                    result, replay = record_ai_fallback_result_v2(
                        conn,
                        attempt_public_id=str(prepared["attempt_public_id"]),
                        transport_outcome="response_received",
                        arguments=arguments,
                        now_ms=100_002,
                    )
                    assert replay is False
                    assert result["result_status"] == "proposal_created"
        if conn.in_transaction:
            conn.commit()

    outcome = s1ca._run(
        managed_workspace,
        _status_request(managed_workspace, intake_id),
        expected_sessions=1,
    )
    assert outcome.exit_code == errors.EXIT_OK, outcome.response
    status = outcome.response["result"]
    assert status["processing_path"] == expected_path
    assert managed_workspace.sessions[-1].total_changes == 0
    if state == "denied":
        assert status["attempt_public_id"] is None
        assert status["receipt_public_id"] is None
        assert status["admission_decision_public_id"]
        assert status["safe_reason_code"] == "configuration_not_accepted"
    else:
        assert status["admission_decision_public_id"] is None
        assert status["attempt_public_id"]
        assert status["receipt_public_id"]
        if state == "unknown":
            assert status["safe_reason_code"] == "outcome_unknown"
        elif state == "finished":
            assert status["safe_reason_code"] is None
            assert status["attribution_match"] is True

    durable = s1ca._read_one(
        managed_workspace,
        """
        SELECT
          (SELECT COUNT(*) FROM ai_model_admission_decisions AS decision
           WHERE decision.raw_intake_record_id = intake.id),
          (SELECT COUNT(*) FROM ai_fallback_attempts AS attempt
           WHERE attempt.raw_intake_record_id = intake.id),
          (SELECT COUNT(*) FROM ai_fallback_invocation_claims AS claim
           JOIN ai_fallback_attempts AS attempt ON attempt.id = claim.attempt_id
           WHERE attempt.raw_intake_record_id = intake.id),
          (SELECT COUNT(*) FROM ai_fallback_results AS result
           JOIN ai_fallback_attempts AS attempt ON attempt.id = result.attempt_id
           WHERE attempt.raw_intake_record_id = intake.id)
        FROM raw_intake_records AS intake
        WHERE intake.public_id = ?
        """,
        (intake_id,),
    )
    assert durable is not None and tuple(int(value) for value in durable) == expected_counts
    assert managed_workspace.sessions[-1].total_changes == 0


def test_managed_recovery_prepares_review_once_and_stale_replay_is_noop(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    proposal_id = s1ca._seed_proposal(managed_workspace)
    job_id = _job_for_proposal(managed_workspace, proposal_id)
    _bind_seeded_proposal_to_job(managed_workspace, job_id=job_id, proposal_id=proposal_id)
    before = _get_recovery(managed_workspace, job_id)
    assert before["next_action"] == "prepare_initial_review"

    request = _resume_request(managed_workspace, job_id, before)
    first = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert first.exit_code == errors.EXIT_OK, first.response
    assert first.response["result"]["performed_action"] == "prepare_initial_review"
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM d2_posting_reviews")[0] == 1
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0

    stale = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert stale.exit_code == errors.EXIT_OK, stale.response
    assert stale.response["result"]["stale_recovery_step"] is True
    assert stale.response["result"]["performed_action"] == "none"
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM d2_posting_reviews")[0] == 1


def test_managed_recovery_source_refusal_preserves_code_and_releases_gate(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    routed, _ = s1ca._capture_route(
        managed_workspace, "synthetic source-bound recovery", message_id=88
    )
    assert routed.exit_code == errors.EXIT_OK, routed.response
    job_id = str(routed.response["result"]["capture_job"]["public_id"])
    denied = s1ca._run(
        managed_workspace,
        _recovery_request(
            managed_workspace,
            envelope.COMMAND_GET_CAPTURE_RECOVERY,
            job_id,
            context=_managed_context(managed_workspace, binding_id="wrong-binding"),
        ),
        expected_sessions=1,
    )
    assert denied.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert denied.response["error"]["code"] == errors.ACTOR_MISMATCH
    s1cc.assert_exclusive_gate_released(managed_workspace)


def test_managed_resume_refuses_capture_processing_before_s3_processor(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed, _ = s1ca._capture_route(managed_workspace, "synthetic pending capture", message_id=89)
    assert routed.exit_code == errors.EXIT_OK, routed.response
    job_id = str(routed.response["result"]["capture_job"]["public_id"])
    before = _get_recovery(managed_workspace, job_id)
    assert before["next_action"] == "capture_processing_required"

    calls: list[str] = []

    def unexpected_processor(*_args: object, **_kwargs: object) -> object:
        calls.append("processor")
        raise AssertionError("managed recovery entered the S3 processor")

    def unexpected_engine(*_args: object, **_kwargs: object) -> object:
        calls.append("engine")
        raise AssertionError("managed recovery constructed an OCR engine")

    monkeypatch.setattr(commands, "handle_process_capture_job", unexpected_processor)
    monkeypatch.setattr(commands, "build_ocr_engine", unexpected_engine)
    refused = s1ca._run(
        managed_workspace,
        _resume_request(managed_workspace, job_id, before),
        expected_sessions=1,
    )
    assert refused.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert refused.response["error"]["code"] == errors.LIFECYCLE_CONFLICT
    assert calls == []
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM parser_outputs")[0] == 0
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM ai_fallback_attempts")[0] == 0
    assert (
        s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM ai_fallback_invocation_claims")[0]
        == 0
    )
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM receipt_ocr_extractions")[0] == 0
    assert _get_recovery(managed_workspace, job_id)["next_action"] == "capture_processing_required"


def _seed_accepted_attempt_after_commit_crash(
    workspace: s1ca.ManagedBridgeWorkspace,
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generate_delivery_receipt_signing_key(str(workspace.workspace_path / "runtime"))
    context = human_actions.HumanActionContext(
        s1ca.ACTOR,
        s1ca.ACCOUNT,
        s1ca.CONVERSATION,
        s1ca.BINDING,
    )
    source_context = TelegramSourceContext(
        authenticated_actor_id=s1ca.ACTOR,
        account_id=s1ca.ACCOUNT,
        conversation_id=s1ca.CONVERSATION,
        binding_id=s1ca.BINDING,
        message_id=s1ca.SEEDED_MESSAGE_ID,
    )
    key = b"synthetic-s1d2-posting-key"
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s1d2-accepted-posting-seed"
    ) as conn:
        job, review, _ = capture_review.ensure_capture_review(conn, job_public_id=job_id)
        assert review is not None
        source_digest = require_telegram_source_context(
            conn,
            raw_intake_record_id=int(job["raw_intake_record_id"]),
            context=source_context,
        )
        manifest = posting_authority.begin_posting_review_delivery(
            conn,
            review_public_id=review.review_public_id,
            key=key,
            context=context,
        )
        _record_delivery(
            conn,
            manifest=manifest,
            context=context,
            provider_message_id=930,
            source_identity_sha256=source_digest,
            now=int(time.time()),
        )
        confirm = next(control for control in manifest.controls if control.action == "confirm")

        def fail_after_accept(stage: str) -> None:
            if stage == "after_confirmation_commit":
                raise RuntimeError("synthetic accepted-posting crash")

        monkeypatch.setattr(posting_authority, "_failure_injection_hook", fail_after_accept)
        with pytest.raises(RuntimeError, match="synthetic accepted-posting crash"):
            posting_authority.confirm_and_post(
                conn,
                key=key,
                reference=confirm.callback_value.removeprefix("post:"),
                context=context,
                callback_id="s1d2-accepted-posting-confirm",
                callback_message_id=930,
            )
        monkeypatch.setattr(posting_authority, "_failure_injection_hook", None)


def test_managed_resume_accepted_posting_without_new_decision(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id = s1ca._seed_proposal(managed_workspace)
    job_id = _job_for_proposal(managed_workspace, proposal_id)
    _bind_seeded_proposal_to_job(managed_workspace, job_id=job_id, proposal_id=proposal_id)
    _seed_accepted_attempt_after_commit_crash(managed_workspace, job_id, monkeypatch)

    pending = _get_recovery(managed_workspace, job_id)
    assert pending["next_action"] == "resume_accepted_posting"
    resumed = s1ca._run(
        managed_workspace,
        _resume_request(managed_workspace, job_id, pending),
        expected_sessions=1,
    )
    assert resumed.exit_code == errors.EXIT_OK, resumed.response
    assert resumed.response["result"]["performed_action"] == "resume_accepted_posting"
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM d2_posting_decisions")[0] == 1
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 1
    assert _get_recovery(managed_workspace, job_id)["next_action"] == "enqueue_existing_result"


def _job_and_proposal_for_transaction(
    workspace: s1ca.ManagedBridgeWorkspace,
    transaction_id: str,
) -> tuple[str, str]:
    row = s1ca._read_one(
        workspace,
        """
        SELECT job.public_id, proposal.public_id
        FROM transactions AS tx
        JOIN parser_outputs AS proposal ON proposal.id = tx.parser_output_id
        JOIN raw_intake_records AS intake ON intake.parser_output_id = proposal.id
        JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = intake.id
        WHERE tx.public_id = ?
        """,
        (transaction_id,),
    )
    assert row is not None
    return str(row[0]), str(row[1])


def test_managed_corrected_result_uses_s1d1_owner_and_replays_outbox_once(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_posted_managed_transaction(managed_workspace, "text")
    transaction_id = source.transaction_public_id
    s1d1._assert_provisioned_over_nonempty_finance_ledger(
        managed_workspace,
        transaction_id,
        capsys,
    )
    correction_connections = s1d1._connection_observer(monkeypatch)
    plan = s1d1._preview_managed_correction(capsys, transaction_id, "13.50")
    s1d1._prepare_real_terminal_signer(
        managed_workspace,
        monkeypatch,
        correction_connections,
    )
    exit_code, stdout, stderr = s1d1._invoke_correction_cli(
        capsys,
        "confirm",
        str(plan["plan_id"]),
    )
    assert exit_code == 0, (stdout, stderr)
    corrected = json.loads(stdout)
    correction_id = str(corrected["applied"]["correction_id"])
    job_id, proposal_id = _job_and_proposal_for_transaction(managed_workspace, transaction_id)
    _bind_seeded_proposal_to_job(managed_workspace, job_id=job_id, proposal_id=proposal_id)

    # Observe only the Bridge's correction-owned factory use below; S1D1's
    # correction CLI has its own already-tested factory wiring.
    from finance_core.correction_adapters import policy as correction_policy

    opened: list[sqlite3.Connection] = []
    real_factory = correction_policy.open_local_authority_connection

    @contextmanager
    def observed_factory() -> Iterator[sqlite3.Connection]:
        with real_factory() as connection:
            opened.append(connection)
            yield connection

    monkeypatch.setattr(correction_policy, "open_local_authority_connection", observed_factory)

    view = _get_recovery(managed_workspace, job_id)
    assert view["financial_state"] == "corrected"
    assert view["result"]["current"]["amount"] == "13.50"

    for _ in range(2):
        current = _get_recovery(managed_workspace, job_id)
        if current["next_action"] != "enqueue_existing_result":
            break
        request = _resume_request(managed_workspace, job_id, current)
        first = s1ca._run(managed_workspace, request, expected_sessions=1)
        replay = s1ca._run(managed_workspace, request, expected_sessions=1)
        assert first.exit_code == replay.exit_code == errors.EXIT_OK
        assert first.response["result"]["performed_action"] == "enqueue_existing_result"
        assert replay.response["result"]["stale_recovery_step"] is True

    outbox = s1ca._read_one(
        managed_workspace,
        """
        SELECT
          COALESCE(
            SUM(CASE WHEN result_kind = 'posting' AND result_public_id = ? THEN 1 ELSE 0 END), 0
          ),
          COALESCE(
            SUM(CASE WHEN result_kind = 'correction' AND result_public_id = ? THEN 1 ELSE 0 END), 0
          ),
          COUNT(*)
        FROM finance_capture_reply_outbox
        WHERE job_public_id = ?
        """,
        (transaction_id, correction_id, job_id),
    )
    assert outbox is not None and tuple(int(value) for value in outbox) == (1, 1, 2)
    assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 1
    assert _get_recovery(managed_workspace, job_id)["next_action"] == "none"
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("frozen_kind", ("whole_card", "guided_update"))
def test_managed_frozen_d1_and_guided_replay_use_one_session(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    frozen_kind: str,
) -> None:
    _proposal_id, redeemed, _redemption_request = s1ca._start_guided_edit(managed_workspace)
    if frozen_kind == "whole_card":
        card_id = str(redeemed.response["result"]["human_draft_card"]["card_generation_public_id"])
        text = s1ca._whole_card_text(card_id)
        message_id = 90
        expected_route = "whole_card"
        expected_action = "apply_human_draft_card"
    else:
        text = "merchant=Synthetic Cafe"
        message_id = 91
        expected_route = "guided_update"
        expected_action = "apply_guided_edit_update"

    routed, _ = s1ca._capture_route(managed_workspace, text, message_id=message_id)
    assert routed.exit_code == errors.EXIT_OK, routed.response
    assert routed.response["result"]["interaction_route"]["route_kind"] == expected_route
    job_id = str(routed.response["result"]["capture_job"]["public_id"])
    before = _get_recovery(managed_workspace, job_id)
    assert before["next_action"] in {"d1_command_required", "guided_command_required"}
    request = _resume_request(managed_workspace, job_id, before)

    resumed = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert resumed.exit_code == errors.EXIT_OK, resumed.response
    assert resumed.response["result"]["performed_action"] == expected_action
    if frozen_kind == "whole_card":
        operation_sql = "SELECT COUNT(*) FROM parser_human_draft_operations"
    else:
        operation_sql = (
            "SELECT COUNT(*) FROM openclaw_guided_edit_events WHERE event_type = 'update_applied'"
        )
    operations_after_first = s1ca._read_one(managed_workspace, operation_sql)[0]
    assert int(operations_after_first) > 0
    stale = s1ca._run(managed_workspace, request, expected_sessions=1)
    assert stale.exit_code == errors.EXIT_OK, stale.response
    assert stale.response["result"]["stale_recovery_step"] is True
    assert stale.response["result"]["performed_action"] == "none"
    operations_after_replay = s1ca._read_one(managed_workspace, operation_sql)[0]
    assert operations_after_replay == operations_after_first

    if frozen_kind == "whole_card":
        child_review_view = _get_recovery(managed_workspace, job_id)
        assert child_review_view["next_action"] == "prepare_child_review"
        child_review_request = _resume_request(managed_workspace, job_id, child_review_view)
        child_review = s1ca._run(
            managed_workspace,
            child_review_request,
            expected_sessions=1,
        )
        assert child_review.exit_code == errors.EXIT_OK, child_review.response
        assert child_review.response["result"]["performed_action"] == "prepare_child_review"
        assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM d2_posting_reviews")[0] == 1
        assert (
            s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM d2_posting_decisions")[0] == 0
        )
        assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM transactions")[0] == 0

        child_review_stale = s1ca._run(
            managed_workspace,
            child_review_request,
            expected_sessions=1,
        )
        assert child_review_stale.exit_code == errors.EXIT_OK, child_review_stale.response
        assert child_review_stale.response["result"]["stale_recovery_step"] is True
        assert child_review_stale.response["result"]["performed_action"] == "none"
        assert s1ca._read_one(managed_workspace, "SELECT COUNT(*) FROM d2_posting_reviews")[0] == 1
