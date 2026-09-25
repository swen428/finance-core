"""D3 review-card recovery over synthetic, temporary staging databases."""

from __future__ import annotations

import json
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest
from test_receipt_ocr_evidence import FakeEngine

from finance_core.application import capture_processing, capture_review
from finance_core.intake.capture_jobs import claim_capture_job, get_capture_job


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def _capture_processed_text(
    workspace: support.BridgeWorkspace,
    *,
    with_context: bool = True,
    eligible_proposal: bool = False,
) -> str:
    arguments = support.capture_text_arguments(
        workspace,
        support.telegram_text_update(
            "Lunch SGD 12.50 at ExampleCafe paid by Owner" if eligible_proposal else "lunch 12.50"
        ),
    )
    if with_context:
        arguments.update(
            {
                "authenticated_actor_id": "111",
                "telegram_account_id": "synthetic-account",
                "telegram_conversation_id": "111",
                "conversation_binding_id": "synthetic-binding",
            }
        )
    captured = support.run_cli(
        support.make_request(
            "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=10)
        )
    )
    assert captured.exit_code == 0, captured.stderr
    job_public_id = str(captured.response["result"]["capture_job"]["public_id"])
    with support.open_database(workspace) as conn:
        lease = claim_capture_job(conn, public_id=job_public_id, owner="synthetic-worker")
        processed = capture_processing.process_claimed_capture_job(conn, lease=lease)
        assert processed["status"] == "processing"
        assert processed["proposal_public_id"]
        if eligible_proposal:
            # Synthetic completed parser proposal: the local parser does not
            # infer a transaction date. This test supplies the proposed date
            # before D2 review, which still requires a later human decision.
            proposal = conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
                (processed["proposal_public_id"],),
            ).fetchone()
            assert proposal is not None
            payload = json.loads(str(proposal["parsed_payload"]))
            payload["transaction_date"] = "2026-09-25"
            conn.execute(
                "UPDATE parser_outputs SET parsed_payload = ? WHERE public_id = ?",
                (json.dumps(payload, sort_keys=True), processed["proposal_public_id"]),
            )
            conn.commit()
    return job_public_id


def test_review_card_is_durable_before_awaiting_user_and_replays_same_card(
    workspace: support.BridgeWorkspace,
) -> None:
    job_public_id = _capture_processed_text(workspace, eligible_proposal=True)
    with support.open_database(workspace) as conn:
        first_job, first_review, replay = capture_review.ensure_capture_review(
            conn, job_public_id=job_public_id
        )
        assert replay is False
        assert first_job["status"] == "awaiting_user"
        assert first_review is not None
        assert first_review.initial_card_public_id is not None
        assert first_review.presentation_text
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM d2_initial_proposal_cards").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0

    with support.open_database(workspace) as conn:
        second_job, second_review, replay = capture_review.ensure_capture_review(
            conn, job_public_id=job_public_id
        )
        assert replay is True
        assert second_job["status"] == "awaiting_user"
        assert second_review is not None
        assert second_review.review_public_id == first_review.review_public_id
        assert second_review.initial_card_public_id == first_review.initial_card_public_id
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM d2_initial_proposal_cards").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_crash_after_d2_card_commit_recovers_same_review(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_public_id = _capture_processed_text(workspace, eligible_proposal=True)
    promote = capture_review._promote_review_status

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic crash after card commit")

    monkeypatch.setattr(capture_review, "_promote_review_status", crash)
    with support.open_database(workspace) as conn:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            capture_review.ensure_capture_review(conn, job_public_id=job_public_id)
        job = get_capture_job(conn, public_id=job_public_id)
        assert job is not None and job["status"] == "processing"
        original = conn.execute(
            "SELECT review_public_id, initial_card_public_id FROM d2_posting_reviews"
        ).fetchone()
        assert original is not None

    monkeypatch.setattr(capture_review, "_promote_review_status", promote)
    with support.open_database(workspace) as conn:
        job, review, replay = capture_review.ensure_capture_review(
            conn, job_public_id=job_public_id
        )
        assert replay is True
        assert job["status"] == "awaiting_user"
        assert review is not None
        assert review.review_public_id == original["review_public_id"]
        assert review.initial_card_public_id == original["initial_card_public_id"]
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_missing_capture_context_refuses_review_without_state_change(
    workspace: support.BridgeWorkspace,
) -> None:
    job_public_id = _capture_processed_text(workspace, with_context=False)
    with support.open_database(workspace) as conn:
        with pytest.raises(capture_review.CaptureReviewConflictError, match="context"):
            capture_review.ensure_capture_review(conn, job_public_id=job_public_id)
        job = get_capture_job(conn, public_id=job_public_id)
        assert job is not None and job["status"] == "processing"
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_changed_proposal_binding_refuses_review_without_card(
    workspace: support.BridgeWorkspace,
) -> None:
    job_public_id = _capture_processed_text(workspace)
    with support.open_database(workspace) as conn:
        conn.execute(
            "UPDATE finance_capture_jobs SET proposal_public_id = ? WHERE public_id = ?",
            ("prop_wrong_binding", job_public_id),
        )
        conn.commit()
        with pytest.raises(capture_review.CaptureReviewConflictError, match="binding"):
            capture_review.ensure_capture_review(conn, job_public_id=job_public_id)
        job = get_capture_job(conn, public_id=job_public_id)
        assert job is not None and job["status"] == "processing"
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 0


def test_incomplete_text_stops_for_edit_without_claiming_review(
    workspace: support.BridgeWorkspace,
) -> None:
    job_public_id = _capture_processed_text(workspace)
    with support.open_database(workspace) as conn:
        job, review, replay = capture_review.ensure_capture_review(
            conn, job_public_id=job_public_id
        )
        assert replay is False
        assert review is None
        assert job["status"] == "needs_attention"
        assert job["last_error"] == "review_requires_edit"
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM d2_initial_proposal_cards").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_ambiguous_receipt_stops_for_edit_without_claiming_review(
    workspace: support.BridgeWorkspace,
) -> None:
    support.write_handoff_file(workspace, "ambiguous.jpg", support.JPEG_BYTES)
    arguments = support.capture_receipt_arguments(workspace, handoff_filename="ambiguous.jpg")
    arguments.update(
        {
            "authenticated_actor_id": "111",
            "telegram_account_id": "synthetic-account",
            "telegram_conversation_id": "111",
            "conversation_binding_id": "synthetic-binding",
        }
    )
    captured = support.run_cli(
        support.make_request(
            "capture", arguments, idempotency_key=support.canonical_capture_key(message_id=20)
        )
    )
    assert captured.exit_code == 0, captured.stderr
    job_public_id = str(captured.response["result"]["capture_job"]["public_id"])
    with support.open_database(workspace) as conn:
        lease = claim_capture_job(conn, public_id=job_public_id, owner="synthetic-receipt-worker")
        processed = capture_processing.process_claimed_capture_job(
            conn, lease=lease, engine=FakeEngine()
        )
        assert processed["status"] == "processing"
        job, review, _replay = capture_review.ensure_capture_review(
            conn, job_public_id=job_public_id
        )
        assert review is None
        assert job["status"] == "needs_attention"
        assert job["last_error"] == "review_requires_edit"
        assert conn.execute("SELECT count(*) FROM d2_posting_reviews").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
