"""Real process interruption and concurrent snapshot proof for corrections."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from finance_core.application.posting import PostingService
from tests import test_application_posting_recovery_v1 as original
from tests import test_independent_corrections_v1 as signed
from tests.conftest import connect_temp_db


@pytest.mark.parametrize(
    ("route", "stage", "committed"),
    [
        ("text", "after_decision", False),
        ("receipt", "after_receipt_fact", False),
        ("text", "after_commit", True),
        ("receipt", "after_commit", True),
    ],
)
def test_abrupt_process_exit_recovers_only_committed_correction(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    route: str,
    stage: str,
    committed: bool,
) -> None:
    connection = migrated_temp_db_connection
    workspace_path = ""
    if route == "text":
        posting, posted, authority = signed._posted_text(connection, "abrupt-" + stage)
    else:
        (
            connection,
            workspace,
            _manifest,
            service,
            card,
            _proposal,
            _intake,
            decision,
            _display,
            _source,
            _authority,
        ) = original._prepare_receipt_subject(tmp_path, "abrupt-receipt-" + stage)
        workspace_path = workspace.workspace_path
        posted = service.submit_post(card.review_id, decision)
        authority = signed._SyntheticCorrectionDecisionAuthority()
        posting = PostingService(
            connection=connection,
            source_verifier=service._source_port,
            human_decision_authority=service._decision_port,
            binding=service._binding,
            clock=lambda: original.NOW + 5,
            correction_decision_authority=authority,
        )
    corrections = signed._correction_service(posting, authority=authority)
    plan = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "Process interruption"}, "correct label"
    )
    decision_id = signed._persist_correction_approval(
        connection, plan, decision_id="abrupt-decision-" + stage
    )
    snapshot_count = connection.execute(
        "SELECT count(*) FROM authoritative_calculation_snapshots"
    ).fetchone()[0]
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    connection.close()
    # The child exits without a Python exception, rollback, close or finally.
    # SQLite must recover an uncommitted receipt snapshot/version/decision.
    program = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from tests.conftest import connect_temp_db
from tests import test_application_posting_recovery_v1 as original
from tests import test_independent_corrections_v1 as signed
from finance_core.application.posting import PostingService
import finance_core.application.independent_corrections as owner
connection = connect_temp_db(Path(sys.argv[1]))
authority = signed._SyntheticCorrectionDecisionAuthority()
source = original._DurableSourceVerifier()
if sys.argv[5]:
    from finance_core.receipt_staging_runner.models import parse_runner_manifest
    from finance_core.receipt_staging_runner.workspace import recover_runner_workspace
    manifest = parse_runner_manifest((Path(sys.argv[5]) / 'manifest.json').read_bytes())
    workspace = recover_runner_workspace(sys.argv[5], manifest)
    source = original._DurableSourceVerifier(workspace=workspace, manifest=manifest)
posting = PostingService(
    connection=connection, source_verifier=source,
    human_decision_authority=original._DurablePostingDecisionAuthority(),
    binding=original.BINDING, clock=lambda: original.NOW + 5,
    correction_decision_authority=authority,
)
def interrupt(stage):
    if stage == sys.argv[4]:
        os._exit(73)
owner._failure_injection_hook = interrupt
signed._correction_service(posting, authority=authority).submit(sys.argv[2], sys.argv[3])
raise AssertionError('interruption point was not reached')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            program,
            str(path),
            plan.plan_id,
            decision_id,
            stage,
            workspace_path,
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    reopened = connect_temp_db(path)
    try:
        recovered_posting = PostingService(
            connection=reopened,
            source_verifier=posting._source_port,
            human_decision_authority=posting._decision_port,
            binding=posting._binding,
            clock=lambda: original.NOW + 5,
            correction_decision_authority=authority,
        )
        reader = signed._correction_service(recovered_posting, authority=authority)
        recovered = reader.recover(plan.plan_id)
        assert (recovered is not None) is committed
        assert reader.read_effective(posted.transaction_public_id).version == int(committed)
        assert reopened.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        for table in ("application_correction_decisions", "application_correction_versions"):
            assert reopened.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == int(committed)
        assert reopened.execute(
            "SELECT count(*) FROM application_correction_receipt_facts"
        ).fetchone()[0] == int(committed and route == "receipt")
        assert reopened.execute(
            "SELECT count(*) FROM authoritative_calculation_snapshots"
        ).fetchone()[0] == snapshot_count + int(committed and route == "receipt")
        if committed:
            assert reader.submit(plan.plan_id, decision_id).recovered
    finally:
        reopened.close()


def test_posting_views_remain_one_snapshot_while_correction_commits(
    migrated_temp_db_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = migrated_temp_db_connection
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    posting, posted, authority = signed._posted_text(connection, "concurrent-reader")
    corrections = signed._correction_service(posting, authority=authority)
    first = corrections.prepare_correction(
        posted.transaction_public_id, {"amount": "16.00"}, "first"
    )
    first_decision = signed._persist_correction_approval(
        connection, first, decision_id="snapshot-first"
    )
    corrections.submit(first.plan_id, first_decision)
    second = corrections.prepare_correction(
        posted.transaction_public_id, {"amount": "17.00"}, "second"
    )
    second_decision = signed._persist_correction_approval(
        connection, second, decision_id="snapshot-second"
    )
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    start = threading.Event()
    finished = threading.Event()
    outcomes: list[object] = []

    def write() -> None:
        writer = connect_temp_db(path)
        try:
            assert start.wait(15)
            separate = PostingService(
                connection=writer,
                source_verifier=posting._source_port,
                human_decision_authority=posting._decision_port,
                binding=posting._binding,
                clock=lambda: original.NOW + 5,
                correction_decision_authority=authority,
            )
            outcomes.append(
                signed._correction_service(separate, authority=authority).submit(
                    second.plan_id, second_decision
                )
            )
        except BaseException as error:
            outcomes.append(error)
        finally:
            writer.close()
            finished.set()

    verify = posting._get_original_status
    paused = False

    def pause_once(attempt_id: str):
        nonlocal paused
        verified = verify(attempt_id)
        if not paused:
            paused = True
            start.set()
            assert finished.wait(15)
        return verified

    thread = threading.Thread(target=write)
    thread.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(posting, "_get_original_status", pause_once)
            status = posting.get_status(posted.attempt_id)
        assert finished.wait(15)
        assert len(outcomes) == 1 and not isinstance(outcomes[0], BaseException), outcomes
        assert status.applied is not None and status.applied.version == 0
        assert status.current is not None and status.current.version == 1
        assert status.current.fields.amount == "16.00"
        latest = posting.get_status(posted.attempt_id)
        assert latest.current is not None and latest.current.version == 2
        assert latest.current.fields.amount == "17.00"
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    finally:
        start.set()
        thread.join(timeout=20)
        assert not thread.is_alive()


@pytest.mark.parametrize("route", ["text", "receipt"])
def test_original_commit_without_coordination_can_be_corrected_then_resumed(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    import finance_core.application.posting as original_owner

    connection = migrated_temp_db_connection
    if route == "text":
        service, card, _proposal, _intake, decision, _display = original._prepare_text_subject(
            connection, "coordination-text"
        )
        fault = "after_text_conversion_commit"
    else:
        (
            connection,
            _workspace,
            _manifest,
            service,
            card,
            _proposal,
            _intake,
            decision,
            _display,
            _source,
            _decision,
        ) = original._prepare_receipt_subject(tmp_path, "coordination-receipt")
        fault = "after_receipt_finalization_commit"

    def lose_original_reply(stage: str) -> None:
        if stage == fault:
            raise RuntimeError("original financial commit outlived coordination")

    with monkeypatch.context() as patch:
        patch.setattr(original_owner, "_failure_injection_hook", lose_original_reply)
        with pytest.raises(RuntimeError, match="outlived coordination"):
            service.submit_post(card.review_id, decision)
    attempt = connection.execute(
        "SELECT attempt_id,stage FROM application_posting_attempts"
    ).fetchone()
    assert attempt["stage"] != "finalized"
    original_status = service.get_status(attempt["attempt_id"])
    assert original_status.state == "finalized"
    authority = signed._SyntheticCorrectionDecisionAuthority()
    posting = PostingService(
        connection=connection,
        source_verifier=service._source_port,
        human_decision_authority=service._decision_port,
        binding=service._binding,
        clock=lambda: original.NOW + 5,
        correction_decision_authority=authority,
    )
    corrections = signed._correction_service(posting, authority=authority)
    plan = corrections.prepare_correction(
        original_status.transaction_public_id, {"amount": "13.50"}, "correct total"
    )
    approved = signed._persist_correction_approval(
        connection, plan, decision_id="coordination-correction-" + route
    )
    applied = corrections.submit(plan.plan_id, approved)
    resumed = posting.resume_post(attempt["attempt_id"])
    assert resumed.applied is not None and resumed.applied.version == 0
    assert resumed.current is not None and resumed.current.version == 1
    assert resumed.current.fields.amount == "13.50"
    assert resumed.current == applied.current
    assert resumed.transaction_public_id == original_status.transaction_public_id
    assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert (
        connection.execute("SELECT stage FROM application_posting_attempts").fetchone()[0]
        == "finalized"
    )
    assert posting.submit_post(card.review_id, decision).current == applied.current
    if route == "receipt":
        connection.close()
