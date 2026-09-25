"""D3 result/reply recovery on a synthetic temporary ledger."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from finance_core.application.corrections import CorrectionService
from finance_core.correction_adapters import local_authority
from finance_core.correction_adapters.d2_source import D2OriginalSourceVerifier
from finance_core.correction_adapters.local_authority import LocalApprovalAuthority
from finance_core.correction_adapters.policy import open_local_authority_connection, provision
from finance_core.intake.capture_jobs import (
    DurableCaptureConnectionError,
    capture_job_public_id,
)
from finance_core.openclaw_staging_bridge.capture_reply_outbox import (
    ReplyOutboxConflict,
    begin_reply_attempt,
    ensure_result_reply,
    get_reply,
    reconcile_capture_result_replies,
    record_reply_sent,
)
from finance_core.openclaw_staging_bridge.capture_results import (
    CaptureResultUnavailable,
    recover_capture_result,
)
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.posting_authority import (
    begin_posting_review_delivery,
    confirm_and_post,
    prepare_posting_review,
)
from tests.test_correction_adapters_d2_source import _Terminal
from tests.test_d2_initial_card_delivery_authority_v1 import (
    _file_connection as _initial_file_connection,
)
from tests.test_d2_initial_card_delivery_authority_v1 import (
    _record_delivery,
    _seed_initial_text,
)
from tests.test_d2_posting_authority_v1 import _issue_and_activate, _published_text_card


def _committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn, published = _published_text_card(tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    intake_id = conn.execute(
        "SELECT drafts.source_raw_intake_id FROM parser_human_draft_cards AS cards "
        "JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id "
        "WHERE cards.card_generation_public_id = ?",
        (published.card_generation_public_id,),
    ).fetchone()[0]
    assert intake_id is not None
    # This D2 fixture predates Telegram capture jobs. Bind its synthetic raw
    # source directly so this test exercises result recovery without changing
    # D2's historical setup or production capture validation.
    intake = conn.execute(
        "SELECT public_id FROM raw_intake_records WHERE id = ?",
        (intake_id,),
    ).fetchone()
    job_id = capture_job_public_id(str(intake["public_id"]))
    conn.execute(
        "INSERT INTO finance_capture_jobs "
        "(public_id, raw_intake_record_id, capture_kind, intake_fingerprint, status) "
        "VALUES (?, ?, 'text', ?, 'awaiting_user')",
        (job_id, intake_id, "0" * 64),
    )
    conn.commit()
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d3-result-review",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"synthetic-d3-result-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=211,
    )
    posted = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d3-result-confirm",
        callback_message_id=211,
        clock=lambda: 1004,
    )
    assert posted.state == "finalized"
    return conn, job_id, review.review_public_id, context, posted


def _committed_initial(tmp_path: Path):
    conn = _initial_file_connection(tmp_path)
    proposal_id = _seed_initial_text(conn)
    intake = conn.execute(
        "SELECT id, public_id FROM raw_intake_records WHERE public_id = 'intake_d2_initial_text'"
    ).fetchone()
    job_id = capture_job_public_id(str(intake["public_id"]))
    conn.execute(
        "INSERT INTO finance_capture_jobs "
        "(public_id, raw_intake_record_id, capture_kind, intake_fingerprint, status) "
        "VALUES (?, ?, 'text', ?, 'awaiting_user')",
        (job_id, intake["id"], "0" * 64),
    )
    conn.commit()
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d3-corrected-initial-review",
        proposal_public_id=proposal_id,
        admitted_source_message_id="77",
        context=context,
        clock=lambda: 1000,
    )
    review_id = review.review_public_id
    key = b"synthetic-d3-corrected-key"
    manifest = begin_posting_review_delivery(
        conn,
        review_public_id=review_id,
        key=key,
        context=context,
        clock=lambda: 1001,
    )
    _record_delivery(conn, manifest=manifest, context=context, provider_message_id=901, now=1002)
    control = next(item for item in manifest.controls if item.action == "confirm")
    posted = confirm_and_post(
        conn,
        key=key,
        reference=control.callback_value.removeprefix("post:"),
        context=context,
        callback_id="d3-corrected-confirm",
        callback_message_id=901,
        clock=lambda: 1003,
    )
    return conn, job_id, review_id, context, posted


def test_committed_result_reply_loss_and_duplicate_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, job_id, review_id, context, posted = _committed_initial(tmp_path)
    try:
        before = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
        # The canonical D2 commit exists but the process died before enqueue.
        recovered_rows = reconcile_capture_result_replies(conn, job_public_id=job_id)
        assert len(recovered_rows) == 1
        first, result = ensure_result_reply(
            conn, job_public_id=job_id, review_public_id=review_id, context=context
        )
        assert result["current"]["transaction_public_id"] == posted.transaction_public_id
        assert result["current"]["amount"] == "12.50"
        replay, same = ensure_result_reply(
            conn, job_public_id=job_id, review_public_id=review_id, context=context
        )
        assert replay == first and same == result
        assert reconcile_capture_result_replies(conn, job_public_id=job_id) == [first]
        assert conn.execute("SELECT count(*) FROM finance_capture_reply_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == before
        assert get_reply(conn, public_id=first["public_id"])["status"] == "pending"
    finally:
        conn.close()


def test_send_attempt_upgrades_wal_normal_to_full_before_nonce_is_exposed(
    tmp_path: Path,
) -> None:
    conn, job_id, review_id, context, _ = _committed_initial(tmp_path)
    try:
        row, _ = ensure_result_reply(
            conn, job_public_id=job_id, review_public_id=review_id, context=context
        )
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("PRAGMA synchronous=NORMAL")
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        attempted, _ = begin_reply_attempt(
            conn, public_id=row["public_id"], review_public_id=review_id, context=context
        )
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert attempted["status"] == "outcome_unknown"
        assert attempted["send_attempt_nonce"]
    finally:
        conn.close()


def test_send_attempt_refuses_unsafe_journal_before_issuing_nonce(tmp_path: Path) -> None:
    conn, job_id, review_id, context, _ = _committed_initial(tmp_path)
    try:
        row, _ = ensure_result_reply(
            conn, job_public_id=job_id, review_public_id=review_id, context=context
        )
        assert conn.execute("PRAGMA journal_mode=OFF").fetchone()[0] == "off"
        with pytest.raises(DurableCaptureConnectionError):
            begin_reply_attempt(
                conn, public_id=row["public_id"], review_public_id=review_id, context=context
            )
        untouched = get_reply(conn, public_id=row["public_id"])
        assert untouched["status"] == "pending"
        assert untouched["send_attempt_nonce"] is None
    finally:
        conn.close()


def test_unknown_send_outcome_requires_explicit_retry_and_fences_old_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, job_id, review_id, context, _ = _committed(tmp_path, monkeypatch)
    try:
        row, _ = ensure_result_reply(
            conn, job_public_id=job_id, review_public_id=review_id, context=context
        )
        first, view = begin_reply_attempt(
            conn, public_id=row["public_id"], review_public_id=review_id, context=context
        )
        assert view["current"]["amount"] == "12.50"
        assert first["status"] == "outcome_unknown"
        # Simulate a crash after the send boundary but before its receipt.
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        conn.close()
        from finance_core.staging_guard import open_staging_database

        conn = open_staging_database(path)
        with pytest.raises(ReplyOutboxConflict):
            begin_reply_attempt(
                conn,
                public_id=row["public_id"],
                review_public_id=review_id,
                context=context,
            )
        second, replay = begin_reply_attempt(
            conn,
            public_id=row["public_id"],
            review_public_id=review_id,
            context=context,
            retry_unknown=True,
        )
        assert replay["result_public_id"] == view["result_public_id"]
        assert second["send_attempt_nonce"] != first["send_attempt_nonce"]
        with pytest.raises(ReplyOutboxConflict):
            record_reply_sent(
                conn,
                public_id=row["public_id"],
                send_attempt_nonce=first["send_attempt_nonce"],
            )
        sent = record_reply_sent(
            conn,
            public_id=row["public_id"],
            send_attempt_nonce=second["send_attempt_nonce"],
        )
        assert sent["status"] == "sent"
        assert (
            record_reply_sent(
                conn,
                public_id=row["public_id"],
                send_attempt_nonce=second["send_attempt_nonce"],
            )
            == sent
        )
    finally:
        conn.close()


def test_result_recovery_refuses_unrelated_job_or_missing_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, job_id, review_id, context, _ = _committed(tmp_path, monkeypatch)
    try:
        with pytest.raises(CaptureResultUnavailable):
            recover_capture_result(
                conn,
                job_public_id="fcj_missing",
                review_public_id=review_id,
                context=context,
            )
        with pytest.raises(CaptureResultUnavailable):
            recover_capture_result(
                conn,
                job_public_id=job_id,
                review_public_id="d2rev_missing",
                context=context,
            )
    finally:
        conn.close()


def test_canonical_result_does_not_override_an_active_processing_lease(
    tmp_path: Path,
) -> None:
    conn, job_id, review_id, context, _ = _committed_initial(tmp_path)
    try:
        conn.execute(
            "UPDATE finance_capture_jobs SET status = 'processing', lease_owner = 'worker', "
            "lease_expires_at = 9999999999999 WHERE public_id = ?",
            (job_id,),
        )
        conn.commit()
        with pytest.raises(ReplyOutboxConflict, match="active processing"):
            ensure_result_reply(
                conn,
                job_public_id=job_id,
                review_public_id=review_id,
                context=context,
            )
        assert conn.execute("SELECT count(*) FROM finance_capture_reply_outbox").fetchone()[0] == 0
        conn.execute(
            "UPDATE finance_capture_jobs SET lease_owner = NULL, lease_expires_at = NULL "
            "WHERE public_id = ?",
            (job_id,),
        )
        conn.commit()
        row, _ = ensure_result_reply(
            conn, job_public_id=job_id, review_public_id=review_id, context=context
        )
        assert row["status"] == "pending"
        assert (
            conn.execute(
                "SELECT status FROM finance_capture_jobs WHERE public_id = ?", (job_id,)
            ).fetchone()[0]
            == "result_ready"
        )
    finally:
        conn.close()


def test_older_correction_reply_replays_current_verified_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "owner_runtime"
    (runtime_root / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root.resolve()))
    conn, job_id, review_id, context, posted = _committed_initial(tmp_path)
    database = Path(
        next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    )
    database.chmod(0o600)
    provision(database, "111")
    conn.close()
    tick = [int(time.time())]
    authority = LocalApprovalAuthority(clock=lambda: tick[0])
    nonces = iter(("2" * 64, "3" * 64, "4" * 64, "5" * 64, "6" * 64))
    monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: next(nonces))
    with open_local_authority_connection() as trusted:
        service = CorrectionService(trusted, D2OriginalSourceVerifier(), authority)
        target = str(posted.transaction_public_id)
        first = service.preview(target, {"amount": "13.50"}, "first correction")
        tick[0] = first.created_at_epoch + 1
        signed_first = authority.sign_with_terminal(
            first,
            input_stream=_Terminal(f"CONFIRM {first.plan_id} {'2' * 64}\n"),
            output_stream=_Terminal(),
        )
        service.apply(first.plan_id, signed_first)
        second = service.preview(target, {"amount": "14.00"}, "second correction")
        tick[0] = second.created_at_epoch + 1
        signed_second = authority.sign_with_terminal(
            second,
            input_stream=_Terminal(f"CONFIRM {second.plan_id} {'4' * 64}\n"),
            output_stream=_Terminal(),
        )
        service.apply(second.plan_id, signed_second)

        # Startup scan must reconstruct the original plus both correction
        # replies after a crash before any reply row was written.
        with pytest.raises(CaptureResultUnavailable, match="verifier"):
            reconcile_capture_result_replies(trusted, job_public_id=job_id)
        assert (
            trusted.execute("SELECT count(*) FROM finance_capture_reply_outbox").fetchone()[0] == 0
        )
        recovered_rows = reconcile_capture_result_replies(
            trusted,
            job_public_id=job_id,
            correction_service=service,
        )
        assert len(recovered_rows) == 3
        assert {row["result_public_id"] for row in recovered_rows} == {
            target,
            first.correction_id,
            second.correction_id,
        }

        row, result = ensure_result_reply(
            trusted,
            job_public_id=job_id,
            review_public_id=review_id,
            context=context,
            correction_service=service,
            correction_plan_id=first.plan_id,
        )
        assert result["historical_correction_public_id"] == first.correction_id
        assert result["historical_version"] == 1
        assert result["current"]["correction_public_id"] == second.correction_id
        assert result["current"]["correction_version"] == 2
        assert result["current"]["amount"] == "14.00"
        attempt, refreshed = begin_reply_attempt(
            trusted,
            public_id=row["public_id"],
            review_public_id=review_id,
            context=context,
            correction_service=service,
        )
        assert refreshed["current"]["amount"] == "14.00"
        assert attempt["status"] == "outcome_unknown"
        assert (
            trusted.execute(
                "SELECT reply_status FROM finance_capture_jobs WHERE public_id = ?", (job_id,)
            ).fetchone()[0]
            == "pending"
        )
        posting = recover_capture_result(
            trusted,
            job_public_id=job_id,
            review_public_id=review_id,
            context=context,
            correction_service=service,
        )
        assert posting["current"]["correction_public_id"] == second.correction_id
        assert posting["current"]["amount"] == "14.00"
