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
