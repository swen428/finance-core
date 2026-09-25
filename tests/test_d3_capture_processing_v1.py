"""D3 local processing, lease fencing, and crash recovery on synthetic data."""

from __future__ import annotations

from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest
from test_ai_fallback_service_v1 import _prepared_claim
from test_receipt_ocr_evidence import FakeEngine

from finance_core.application import capture_processing
from finance_core.intake.capture_jobs import (
    CaptureJobNotRunnableError,
    CaptureLeaseLostError,
    assert_capture_lease,
    claim_capture_job,
    get_capture_job,
    renew_capture_job_lease,
)
from finance_core.intake.receipt_ocr_evidence import (
    OcrAttachmentIntegrityConflictError,
    OcrDeadlineExceededError,
)
from finance_core.openclaw_staging_bridge import commands, identity


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _capture_text(workspace: support.BridgeWorkspace) -> str:
    request = support.make_request(
        "capture",
        support.capture_text_arguments(workspace, support.telegram_text_update("lunch 12.50")),
        idempotency_key=support.canonical_capture_key(message_id=10),
    )
    result = support.run_cli(request)
    assert result.exit_code == 0
    return str(result.response["result"]["capture_job"]["public_id"])


def _capture_receipt(workspace: support.BridgeWorkspace) -> str:
    support.write_handoff_file(workspace, "d3-process.jpg", support.JPEG_BYTES)
    request = support.make_request(
        "capture",
        support.capture_receipt_arguments(workspace, handoff_filename="d3-process.jpg"),
        idempotency_key=support.canonical_capture_key(message_id=20),
    )
    result = support.run_cli(request)
    assert result.exit_code == 0
    return str(result.response["result"]["capture_job"]["public_id"])


def test_claim_renew_and_expired_reclaim_fence_old_worker(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_text(workspace)
    with support.open_database(workspace) as conn:
        first = claim_capture_job(
            conn, public_id=public_id, owner="worker-a", duration_ms=1_000, now_ms=100_000
        )
        with pytest.raises(CaptureJobNotRunnableError):
            claim_capture_job(
                conn,
                public_id=public_id,
                owner="worker-b",
                duration_ms=1_000,
                now_ms=100_500,
            )
        renewed = renew_capture_job_lease(conn, first, duration_ms=2_000, now_ms=100_500)
        assert renewed.epoch == first.epoch
        second = claim_capture_job(
            conn,
            public_id=public_id,
            owner="worker-b",
            duration_ms=1_000,
            now_ms=renewed.expires_at_ms,
        )
        assert second.epoch == first.epoch + 1
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(CaptureLeaseLostError):
                assert_capture_lease(conn, first, now_ms=renewed.expires_at_ms)
            assert_capture_lease(conn, second, now_ms=renewed.expires_at_ms)
        finally:
            conn.rollback()


def test_text_processing_replays_existing_proposal_without_finalization(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_text(workspace)
    with support.open_database(workspace) as conn:
        lease = claim_capture_job(conn, public_id=public_id, owner="worker-text")
        job = capture_processing.process_claimed_capture_job(conn, lease=lease)
        assert job["status"] == "processing"
        assert job["proposal_public_id"]
        assert job["lease_owner"] is None
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_receipt_processing_binds_stable_stages_and_no_duplicate_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_receipt(workspace)
    engine = FakeEngine()
    with support.open_database(workspace) as conn:
        lease = claim_capture_job(conn, public_id=public_id, owner="worker-receipt")
        job = capture_processing.process_claimed_capture_job(conn, lease=lease, engine=engine)
        assert job["status"] == "processing"
        assert job["ocr_extraction_public_id"].startswith("rocr_bridge_")
        assert job["proposal_public_id"].startswith("prop_bridge_")
        assert job["proposal_link_public_id"].startswith("ropl_bridge_")
        assert job["ai_status"] == "not_started"
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_worker_stolen_during_ocr_cannot_save_evidence(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_receipt(workspace)
    with support.open_database(workspace) as first_conn:
        first = claim_capture_job(first_conn, public_id=public_id, owner="worker-old")

        def steal(_source: object) -> None:
            with support.open_database(workspace) as second_conn:
                second = claim_capture_job(
                    second_conn,
                    public_id=public_id,
                    owner="worker-new",
                    now_ms=first.expires_at_ms,
                )
                assert second.epoch == first.epoch + 1

        with pytest.raises(CaptureLeaseLostError):
            capture_processing.process_claimed_capture_job(
                first_conn, lease=first, engine=FakeEngine(callback=steal)
            )
        assert first_conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
        assert (
            first_conn.execute("SELECT count(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 0
        )
        job = get_capture_job(first_conn, public_id=public_id)
        assert job is not None and job["lease_epoch"] == first.epoch + 1


def test_crash_after_ocr_replays_stage_identity(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = _capture_receipt(workspace)
    engine = FakeEngine()
    with support.open_database(workspace) as conn:
        first = claim_capture_job(conn, public_id=public_id, owner="worker-first")
        original = capture_processing.ingest_receipt_ocr_evidence_as_total_expense_proposal

        def crash(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic crash after OCR commit")

        monkeypatch.setattr(
            capture_processing, "ingest_receipt_ocr_evidence_as_total_expense_proposal", crash
        )
        with pytest.raises(RuntimeError, match="synthetic crash"):
            capture_processing.process_claimed_capture_job(conn, lease=first, engine=engine)
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        monkeypatch.setattr(
            capture_processing, "ingest_receipt_ocr_evidence_as_total_expense_proposal", original
        )
        second = claim_capture_job(
            conn,
            public_id=public_id,
            owner="worker-recovery",
            now_ms=first.expires_at_ms,
        )
        result = capture_processing.process_claimed_capture_job(conn, lease=second, engine=engine)
        assert result["status"] == "processing"
        assert engine.calls == 1
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 1


def test_stolen_after_ocr_cannot_save_proposal(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = _capture_receipt(workspace)
    with support.open_database(workspace) as first_conn:
        first = claim_capture_job(first_conn, public_id=public_id, owner="worker-old")
        original = capture_processing.ingest_receipt_ocr_evidence_as_total_expense_proposal

        def steal_then_ingest(*args: object, **kwargs: object) -> object:
            with support.open_database(workspace) as second_conn:
                claim_capture_job(
                    second_conn,
                    public_id=public_id,
                    owner="worker-new",
                    now_ms=first.expires_at_ms,
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(
            capture_processing,
            "ingest_receipt_ocr_evidence_as_total_expense_proposal",
            steal_then_ingest,
        )
        with pytest.raises(CaptureLeaseLostError):
            capture_processing.process_claimed_capture_job(
                first_conn, lease=first, engine=FakeEngine()
            )
        assert first_conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert (
            first_conn.execute("SELECT count(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 0
        )
        assert first_conn.execute("SELECT count(*) FROM parser_outputs").fetchone()[0] == 0


def test_existing_ai_claim_without_result_is_unknown_and_never_reclaimed(
    tmp_path: Path,
) -> None:
    workspace, conn, attempt, _claim = _prepared_claim(tmp_path)
    try:
        row = conn.execute("SELECT public_id FROM finance_capture_jobs").fetchone()
        assert row is not None
        public_id = str(row[0])
        lease = claim_capture_job(conn, public_id=public_id, owner="worker-ai")
        job = capture_processing.process_claimed_capture_job(conn, lease=lease)
        assert job["ai_attempt_public_id"] == attempt["attempt_public_id"]
        assert job["ai_status"] == "outcome_unknown"
        assert conn.execute("SELECT count(*) FROM ai_fallback_invocation_claims").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM ai_fallback_results").fetchone()[0] == 0
    finally:
        conn.close()


def test_bridge_process_command_replays_finished_local_job(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_text(workspace)
    request = support.make_request(
        "process_capture_job",
        {"workspace_path": str(workspace.workspace_path), "job_public_id": public_id},
        idempotency_key="fcp_"
        + identity.canonical_digest("finance-process-capture-job-v1", public_id),
    )
    first = support.run_cli(request)
    replay = support.run_cli(request)
    assert first.exit_code == replay.exit_code == 0
    assert first.response["result"]["capture_job"]["status"] == "processing"
    assert replay.response["idempotent_replay"] is True
    assert replay.response["result"]["final_transaction_created"] is False


def test_bridge_receipt_command_uses_local_engine_only(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = _capture_receipt(workspace)
    engine = FakeEngine()
    monkeypatch.setattr(commands, "build_ocr_engine", lambda _workspace: engine)
    request = support.make_request(
        "process_capture_job",
        {"workspace_path": str(workspace.workspace_path), "job_public_id": public_id},
        idempotency_key="fcp_"
        + identity.canonical_digest("finance-process-capture-job-v1", public_id),
    )
    outcome = support.run_cli(request)
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.response["result"]["capture_job"]["status"] == "processing"
    assert engine.calls == 1
    replay = support.run_cli(request)
    assert replay.response["idempotent_replay"] is True
    assert engine.calls == 1


def test_worker_adopts_prior_bridge_propose_without_duplicate_evidence(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = _capture_receipt(workspace)
    engine = FakeEngine()
    monkeypatch.setattr(commands, "build_ocr_engine", lambda _workspace: engine)
    with support.open_database(workspace) as conn:
        job = get_capture_job(conn, public_id=public_id)
        assert job is not None
        intake_public_id = str(job["intake_public_id"])
    proposed = support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
            },
            idempotency_key=support.canonical_propose_key(intake_public_id),
        )
    )
    assert proposed.exit_code == 0, proposed.stderr
    with support.open_database(workspace) as conn:
        lease = claim_capture_job(conn, public_id=public_id, owner="worker-adopt")
        result = capture_processing.process_claimed_capture_job(conn, lease=lease, engine=engine)
        assert result["status"] == "processing"
        assert result["proposal_public_id"] == proposed.response["result"]["proposal_public_id"]
        assert engine.calls == 1
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 1


def test_ocr_timeout_defers_then_reuses_same_job_and_stage_ids(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_receipt(workspace)

    def timeout(_source: object) -> None:
        raise OcrDeadlineExceededError("synthetic local OCR timeout")

    with support.open_database(workspace) as conn:
        first = claim_capture_job(conn, public_id=public_id, owner="worker-timeout")
        deferred = capture_processing.process_claimed_capture_job(
            conn, lease=first, engine=FakeEngine(callback=timeout)
        )
        assert deferred["status"] == "processing"
        assert deferred["last_error"] == "ocr_timeout_retry_pending"
        assert deferred["ocr_retry_count"] == 1
        assert deferred["ocr_retry_not_before_ms"] > 0
        assert deferred["lease_owner"] is None
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
        with pytest.raises(CaptureJobNotRunnableError, match="deferred"):
            claim_capture_job(
                conn,
                public_id=public_id,
                owner="worker-too-soon",
                now_ms=int(deferred["ocr_retry_not_before_ms"]) - 1,
            )
        second = claim_capture_job(
            conn,
            public_id=public_id,
            owner="worker-after-delay",
            now_ms=int(deferred["ocr_retry_not_before_ms"]),
        )
        assert second.epoch == first.epoch + 1
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(CaptureLeaseLostError):
                assert_capture_lease(conn, first)
        finally:
            conn.rollback()
        result = capture_processing.process_claimed_capture_job(
            conn, lease=second, engine=FakeEngine()
        )
        assert result["status"] == "processing"
        assert result["last_error"] is None
        assert result["ocr_retry_count"] == 1
        assert result["ocr_retry_not_before_ms"] == 0
        assert result["ocr_extraction_public_id"] == deferred["ocr_extraction_public_id"]
        assert result["proposal_public_id"] == deferred["proposal_public_id"]
        assert result["proposal_link_public_id"] == deferred["proposal_link_public_id"]
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM receipt_ocr_proposal_links").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_three_ocr_timeouts_stop_without_spin_or_new_economic_event(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_receipt(workspace)

    def timeout(_source: object) -> None:
        raise OcrDeadlineExceededError("synthetic local OCR timeout")

    with support.open_database(workspace) as conn:
        next_claim_ms = None
        for attempt in range(1, 4):
            lease = claim_capture_job(
                conn,
                public_id=public_id,
                owner=f"worker-timeout-{attempt}",
                now_ms=next_claim_ms,
            )
            job = capture_processing.process_claimed_capture_job(
                conn, lease=lease, engine=FakeEngine(callback=timeout)
            )
            assert job["ocr_retry_count"] == attempt
            if attempt < 3:
                assert job["status"] == "processing"
                assert job["last_error"] == "ocr_timeout_retry_pending"
                next_claim_ms = int(job["ocr_retry_not_before_ms"])
            else:
                assert job["status"] == "needs_attention"
                assert job["last_error"] == "ocr_timeout_exhausted"
                assert job["ocr_retry_not_before_ms"] == 0
        with pytest.raises(CaptureJobNotRunnableError):
            claim_capture_job(conn, public_id=public_id, owner="worker-fourth")
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_stale_ocr_timeout_cannot_release_successor_claim(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_receipt(workspace)
    with support.open_database(workspace) as conn:
        first = claim_capture_job(conn, public_id=public_id, owner="worker-old")

        def late_timeout(_source: object) -> None:
            with support.open_database(workspace) as other:
                second = claim_capture_job(
                    other,
                    public_id=public_id,
                    owner="worker-new",
                    now_ms=first.expires_at_ms,
                )
                assert second.epoch == first.epoch + 1
            raise OcrDeadlineExceededError("old OCR finished after lease loss")

        with pytest.raises(CaptureLeaseLostError):
            capture_processing.process_claimed_capture_job(
                conn, lease=first, engine=FakeEngine(callback=late_timeout)
            )
        job = get_capture_job(conn, public_id=public_id)
        assert job is not None
        assert job["lease_owner"] == "worker-new"
        assert job["ocr_retry_count"] == 0
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 0


def test_permanent_ocr_integrity_error_remains_attention(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = _capture_receipt(workspace)

    def invalid_original(_source: object) -> None:
        raise OcrAttachmentIntegrityConflictError("synthetic original mismatch")

    with support.open_database(workspace) as conn:
        lease = claim_capture_job(conn, public_id=public_id, owner="worker-integrity")
        job = capture_processing.process_claimed_capture_job(
            conn, lease=lease, engine=FakeEngine(callback=invalid_original)
        )
        assert job["status"] == "needs_attention"
        assert job["last_error"] == "ocr_failed"
        assert job["ocr_retry_count"] == 0
        with pytest.raises(CaptureJobNotRunnableError):
            claim_capture_job(conn, public_id=public_id, owner="worker-unsafe")


def test_process_command_waits_for_retry_time_then_runs_same_receipt_job(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = _capture_receipt(workspace)
    request = support.make_request(
        "process_capture_job",
        {"workspace_path": str(workspace.workspace_path), "job_public_id": public_id},
        idempotency_key="fcp_"
        + identity.canonical_digest("finance-process-capture-job-v1", public_id),
    )

    def timeout(_source: object) -> None:
        raise OcrDeadlineExceededError("synthetic local OCR timeout")

    monkeypatch.setattr(
        commands, "build_ocr_engine", lambda _workspace: FakeEngine(callback=timeout)
    )
    first = support.run_cli(request)
    assert first.exit_code == 0, first.stderr
    deferred = first.response["result"]["capture_job"]
    assert deferred["last_error"] == "ocr_timeout_retry_pending"
    monkeypatch.setattr(commands, "build_ocr_engine", lambda _workspace: FakeEngine())
    immediate = support.run_cli(request)
    assert immediate.exit_code == 0, immediate.stderr
    assert immediate.response["idempotent_replay"] is True
    assert immediate.response["result"]["capture_job"]["ocr_retry_count"] == 1
    monkeypatch.setattr(
        commands.time,
        "time_ns",
        lambda: (int(deferred["ocr_retry_not_before_ms"]) + 1) * 1_000_000,
    )
    resumed = support.run_cli(request)
    assert resumed.exit_code == 0, resumed.stderr
    result = resumed.response["result"]["capture_job"]
    assert result["last_error"] is None
    assert result["ocr_retry_count"] == 1
    assert result["proposal_public_id"] == deferred["proposal_public_id"]
    with support.open_database(workspace) as conn:
        assert conn.execute("SELECT count(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("failure", "retryable"),
    [
        (CaptureLeaseLostError("synthetic lease loss"), True),
        (capture_processing.CaptureProcessingConflictError("synthetic source conflict"), False),
    ],
)
def test_process_command_distinguishes_lease_loss_from_source_conflict(
    workspace: support.BridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    retryable: bool,
) -> None:
    public_id = _capture_receipt(workspace)
    monkeypatch.setattr(commands, "build_ocr_engine", lambda _workspace: FakeEngine())

    def fail_processor(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(commands, "process_claimed_capture_job", fail_processor)
    request = support.make_request(
        "process_capture_job",
        {"workspace_path": str(workspace.workspace_path), "job_public_id": public_id},
        idempotency_key="fcp_"
        + identity.canonical_digest("finance-process-capture-job-v1", public_id),
    )
    refused = support.run_cli(request)
    assert refused.exit_code != 0
    assert refused.response["error"]["retryable"] is retryable
