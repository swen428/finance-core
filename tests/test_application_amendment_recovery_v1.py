"""Independent restart and opposing-writer acceptance for Application amendments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from queue import Queue
from typing import Any, Callable

import pytest
from application_amendment_support_v1 import read_signed_material
from test_application_amendment_v1 import (
    _amendment_service,
    _assert_no_posting_facts,
    _count,
    _new_evidence,
    _prepare_edited_posting,
    _read_effective_values,
)
from test_application_posting_recovery_v1 import (
    BINDING,
    DECISION_KEY,
    NOW,
    _DurablePostingDecisionAuthority,
    _DurableSourceVerifier,
    _load,
    _prepare_receipt_subject,
    _prepare_text_subject,
)

from finance_core.application.amendment_contract import AmendmentError
from finance_core.application.posting import PostingError, PostingService
from finance_core.bookkeeping_metadata import (
    BOOKKEEPING_METADATA_VERSION,
    read_receipt_bookkeeping_metadata,
)
from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeSnapshotRepository,
    SnapshotVerificationError,
    canonical_json_value,
)
from finance_core.receipt_staging_runner.models import parse_runner_manifest
from finance_core.receipt_staging_runner.workspace import recover_runner_workspace
from tests.conftest import connect_temp_db


class _SimulatedProcessInterruption(BaseException):
    """Represent abrupt process loss at a real owner boundary."""


@dataclass(frozen=True)
class _PreparedSubject:
    connection: sqlite3.Connection
    posting: PostingService
    review: Any
    proposal_public_id: str
    intake_public_id: str
    decision_id: str
    display_id: str
    source_verifier: Any
    decision_authority: Any
    workspace: Any | None = None
    manifest: Any | None = None


def _subject(
    connection: sqlite3.Connection,
    tmp_path: Path,
    route: str,
    suffix: str,
) -> _PreparedSubject:
    if route == "receipt_supersession":
        receipt_suffix = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:16]
        (
            receipt_connection,
            workspace,
            manifest,
            posting,
            review,
            proposal,
            intake_public_id,
            decision_id,
            display_id,
            source_verifier,
            decision_authority,
        ) = _prepare_receipt_subject(tmp_path, f"receipt-{receipt_suffix}")
        return _PreparedSubject(
            receipt_connection,
            posting,
            review,
            proposal,
            intake_public_id,
            decision_id,
            display_id,
            source_verifier,
            decision_authority,
            workspace,
            manifest,
        )

    posting, review, proposal, intake, decision_id, display_id = _prepare_text_subject(
        connection, suffix=suffix
    )
    return _PreparedSubject(
        connection,
        posting,
        review,
        str(proposal["public_id"]),
        str(intake["public_id"]),
        decision_id,
        display_id,
        _DurableSourceVerifier(),
        _DurablePostingDecisionAuthority(),
    )


def _source_verifier_after_reopen(subject: _PreparedSubject) -> tuple[Any, Any, Any]:
    if subject.workspace is None:
        return None, None, _DurableSourceVerifier()
    manifest_path = Path(subject.workspace.workspace_path) / "manifest.json"
    manifest = parse_runner_manifest(manifest_path.read_bytes())
    workspace = recover_runner_workspace(subject.workspace.workspace_path, manifest)
    return workspace, manifest, _DurableSourceVerifier(workspace=workspace, manifest=manifest)


def _posting_service(
    connection: sqlite3.Connection,
    subject: _PreparedSubject,
    *,
    source_verifier: Any | None = None,
) -> PostingService:
    return PostingService(
        connection=connection,
        source_verifier=source_verifier or subject.source_verifier,
        human_decision_authority=_DurablePostingDecisionAuthority(),
        binding=BINDING,
        clock=lambda: NOW,
    )


def _database_path(connection: sqlite3.Connection) -> Path:
    return Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))


def _start_gated_writer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: Any,
    stage: str,
    database_path: Path,
    operation: Callable[[sqlite3.Connection], Any],
) -> tuple[threading.Thread, threading.Event, threading.Event, Queue[tuple[str, Any]]]:
    reached = threading.Event()
    release = threading.Event()
    outcomes: Queue[tuple[str, Any]] = Queue()

    def pause_at_winner_commit(current: str) -> None:
        if current == stage:
            reached.set()
            if not release.wait(timeout=20):
                raise RuntimeError(f"timed out while paused at {stage}")

    monkeypatch.setattr(module, "_failure_injection_hook", pause_at_winner_commit)

    def run() -> None:
        worker_connection = connect_temp_db(database_path)
        try:
            outcomes.put(("accepted", operation(worker_connection)))
        except BaseException as exc:
            outcomes.put(("raised", exc))
        finally:
            worker_connection.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, reached, release, outcomes


def _assert_one_amendment_leaf(
    connection: sqlite3.Connection,
    *,
    intake_public_id: str,
    amendment_id: str,
    resulting_public_id: str,
    resulting_hash: str,
) -> None:
    rows = connection.execute(
        "SELECT amendment_id,publication_public_id,resulting_parser_output_id,"
        "resulting_content_hash FROM application_amendment_records"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["amendment_id"] == amendment_id
    assert rows[0]["resulting_content_hash"] == resulting_hash
    leaf = connection.execute(
        "SELECT parser_outputs.public_id,raw_intake_records.parser_output_id "
        "FROM raw_intake_records JOIN parser_outputs "
        "ON parser_outputs.id=raw_intake_records.parser_output_id "
        "WHERE raw_intake_records.public_id=?",
        (intake_public_id,),
    ).fetchone()
    assert leaf is not None
    assert leaf["public_id"] == resulting_public_id
    assert leaf["parser_output_id"] == rows[0]["resulting_parser_output_id"]
    assert _count(connection, "application_amendment_invalidations") == 1


@pytest.mark.parametrize(
    ("route", "expected_publication", "patch"),
    [
        ("text_completion", "completion", {"description": "Text edit wins"}),
        ("text_supersession", "text_supersession", {"amount": "13.75"}),
        ("receipt_supersession", "receipt_supersession", {"amount": "13.75"}),
    ],
)
@pytest.mark.parametrize("winner", ["confirmation", "amendment"])
def test_old_confirmation_and_edit_have_one_controlled_winner(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_publication: str,
    patch: dict[str, str],
    winner: str,
) -> None:
    from finance_core.application import amendment as amendment_module
    from finance_core.application import posting as posting_module

    subject = _subject(
        migrated_temp_db_connection,
        tmp_path,
        "receipt_supersession" if route == "receipt_supersession" else "text",
        f"opposing-writers-{route}-{winner}",
    )
    connection = subject.connection
    amendment = _amendment_service(connection, source_verifier=subject.source_verifier)
    edit_review = amendment.prepare(subject.proposal_public_id)
    evidence_id = f"evidence-{route}-{winner}"
    amendment_id = f"amendment-{route}-{winner}"
    _new_evidence(connection, edit_review, patch, evidence_id=evidence_id)
    assert not connection.in_transaction

    database_path = _database_path(connection)
    connection.close()
    edit_source_verifier = subject.source_verifier

    if winner == "confirmation":

        def confirm(worker_connection: sqlite3.Connection) -> Any:
            return _posting_service(worker_connection, subject).submit_post(
                subject.review.review_id, subject.decision_id
            )

        worker, reached, release, outcomes = _start_gated_writer(
            monkeypatch,
            module=posting_module,
            stage="after_acceptance_commit",
            database_path=database_path,
            operation=confirm,
        )
        observer = connect_temp_db(database_path)
        try:
            assert reached.wait(timeout=20), "confirmation did not reach its committed pause"
            assert _count(observer, "application_posting_decisions") == 1
            assert _count(observer, "application_posting_attempts") == 1
            assert _count(observer, "transactions") == 0
            loser_service = _amendment_service(observer, source_verifier=edit_source_verifier)
            with pytest.raises(AmendmentError):
                loser_service.amend(edit_review.review_id, evidence_id, amendment_id)
            evidence = read_signed_material(observer, "evidence", evidence_id)
            assert evidence["consumed"] is False
            assert _count(observer, "application_amendment_records") == 0
        finally:
            release.set()
            worker.join(timeout=30)
            observer.close()

        assert not worker.is_alive()
        outcome, posted = outcomes.get_nowait()
        assert outcome == "accepted", posted
        assert posted.state == "finalized"
        final_connection = connect_temp_db(database_path)
        try:
            assert _count(final_connection, "application_amendment_records") == 0
            assert _count(final_connection, "application_posting_decisions") == 1
            assert _count(final_connection, "transactions") == 1
            assert (
                _load(final_connection, "synthetic_decisions", subject.decision_id, DECISION_KEY)[
                    "consumed"
                ]
                is False
            )
            assert (
                read_signed_material(final_connection, "evidence", evidence_id)["consumed"] is False
            )
            current = final_connection.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
                (subject.intake_public_id,),
            ).fetchone()
            assert current is not None
            assert (
                final_connection.execute(
                    "SELECT public_id FROM parser_outputs WHERE id=?", (current[0],)
                ).fetchone()[0]
                == subject.proposal_public_id
            )
        finally:
            final_connection.close()
        return

    def amend(worker_connection: sqlite3.Connection) -> Any:
        service = _amendment_service(worker_connection, source_verifier=edit_source_verifier)
        return service.amend(edit_review.review_id, evidence_id, amendment_id)

    worker, reached, release, outcomes = _start_gated_writer(
        monkeypatch,
        module=amendment_module,
        stage="after_commit",
        database_path=database_path,
        operation=amend,
    )
    observer = connect_temp_db(database_path)
    try:
        assert reached.wait(timeout=20), "amendment did not reach its committed pause"
        old_posting = _posting_service(observer, subject)
        with pytest.raises(PostingError):
            old_posting.submit_post(subject.review.review_id, subject.decision_id)
        assert _count(observer, "application_posting_decisions") == 0
        assert _count(observer, "transactions") == 0
        assert (
            _load(observer, "synthetic_decisions", subject.decision_id, DECISION_KEY)["consumed"]
            is False
        )
    finally:
        release.set()
        worker.join(timeout=30)
        observer.close()

    assert not worker.is_alive()
    outcome, accepted = outcomes.get_nowait()
    assert outcome == "accepted", accepted
    assert accepted.publication_kind == expected_publication
    final_connection = connect_temp_db(database_path)
    try:
        _assert_one_amendment_leaf(
            final_connection,
            intake_public_id=subject.intake_public_id,
            amendment_id=amendment_id,
            resulting_public_id=accepted.proposal_public_id,
            resulting_hash=accepted.effective_content_hash,
        )
        expected_values = _read_effective_values(final_connection, accepted.proposal_public_id)
        assert all(expected_values[field] == value for field, value in patch.items())
        assert _count(final_connection, "application_posting_decisions") == 0
        assert _count(final_connection, "transactions") == 0
        accepted_record = final_connection.execute(
            "SELECT evidence_id FROM application_amendment_records WHERE amendment_id=?",
            (amendment_id,),
        ).fetchone()
        assert accepted_record is not None and accepted_record["evidence_id"] == evidence_id
        assert (
            _load(final_connection, "synthetic_decisions", subject.decision_id, DECISION_KEY)[
                "consumed"
            ]
            is False
        )
    finally:
        final_connection.close()


def _publication_patch(route: str) -> tuple[dict[str, str], str]:
    if route == "text_completion":
        return {"description": "First recovered text edit"}, "completion"
    return {"amount": "13.75"}, route


@pytest.mark.parametrize(
    ("route", "expected_publication"),
    [
        ("text_completion", "completion"),
        ("text_supersession", "text_supersession"),
        ("receipt_supersession", "receipt_supersession"),
    ],
)
@pytest.mark.parametrize("fault_stage", ["before_amendment_seal", "after_commit"])
def test_publication_interruptions_rollback_or_recover_without_pointer_regression(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_publication: str,
    fault_stage: str,
) -> None:
    from finance_core.application import amendment as amendment_module

    subject = _subject(
        migrated_temp_db_connection,
        tmp_path,
        "receipt_supersession" if route == "receipt_supersession" else "text",
        f"publication-recovery-{route}-{fault_stage}",
    )
    connection = subject.connection
    service = _amendment_service(connection, source_verifier=subject.source_verifier)
    review = service.prepare(subject.proposal_public_id)
    first_patch, route_publication = _publication_patch(route)
    assert route_publication == expected_publication
    first_evidence = f"first-evidence-{route}-{fault_stage}"
    first_amendment_id = f"first-amendment-{route}-{fault_stage}"
    _new_evidence(connection, review, first_patch, evidence_id=first_evidence)
    counts_before = {
        table: _count(connection, table)
        for table in (
            "application_amendment_records",
            "application_amendment_invalidations",
            "parser_proposal_completions",
            "parser_text_amendment_revisions",
            "receipt_proposal_revisions",
            "financial_audit_events",
        )
    }
    base_pointer = connection.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (subject.intake_public_id,),
    ).fetchone()[0]

    def interrupt(stage: str) -> None:
        if stage == fault_stage:
            raise _SimulatedProcessInterruption(f"interrupted at {stage}")

    monkeypatch.setattr(amendment_module, "_failure_injection_hook", interrupt)
    with pytest.raises(_SimulatedProcessInterruption, match=fault_stage):
        service.amend(review.review_id, first_evidence, first_amendment_id)
    monkeypatch.setattr(amendment_module, "_failure_injection_hook", None)

    assert not connection.in_transaction
    if fault_stage == "before_amendment_seal":
        assert {table: _count(connection, table) for table in counts_before} == counts_before
        current = connection.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (subject.intake_public_id,),
        ).fetchone()
        assert current is not None and current[0] == base_pointer
        assert read_signed_material(connection, "evidence", first_evidence)["consumed"] is False
        _assert_no_posting_facts(connection)
        return

    assert (
        _count(connection, "application_amendment_records")
        == counts_before["application_amendment_records"] + 1
    )
    database_path = _database_path(connection)
    connection.close()
    workspace, manifest, source_verifier = _source_verifier_after_reopen(subject)
    reopened = connect_temp_db(database_path)
    try:
        recovered_service = _amendment_service(reopened, source_verifier=source_verifier)
        first = recovered_service.get_status(first_amendment_id)
        assert first.publication_kind == expected_publication
        assert first.is_current is True
        assert _read_effective_values(reopened, first.proposal_public_id)[
            next(iter(first_patch))
        ] == next(iter(first_patch.values()))

        second_review = recovered_service.prepare(first.proposal_public_id)
        second_evidence = f"second-evidence-{route}-{fault_stage}"
        second_amendment_id = f"second-amendment-{route}-{fault_stage}"
        _new_evidence(
            reopened,
            second_review,
            {"amount": "15.50"},
            evidence_id=second_evidence,
        )
        second = recovered_service.amend(
            second_review.review_id, second_evidence, second_amendment_id
        )
        assert second.proposal_public_id != first.proposal_public_id
        assert second.is_current is True

        replay = recovered_service.amend(review.review_id, first_evidence, first_amendment_id)
        assert replay.amendment_id == first.amendment_id
        assert replay.effective_content_hash == first.effective_content_hash
        assert replay.is_current is False
        first_status = recovered_service.get_status(first_amendment_id)
        assert first_status.is_current is False
        current = reopened.execute(
            "SELECT parser_outputs.public_id,raw_intake_records.parser_output_id "
            "FROM raw_intake_records JOIN parser_outputs "
            "ON parser_outputs.id=raw_intake_records.parser_output_id "
            "WHERE raw_intake_records.public_id=?",
            (subject.intake_public_id,),
        ).fetchone()
        assert current is not None
        assert current["public_id"] == second.proposal_public_id
        assert current["parser_output_id"] != base_pointer
        records = reopened.execute(
            "SELECT amendment_id,resulting_parser_output_id,resulting_content_hash "
            "FROM application_amendment_records ORDER BY accepted_at,amendment_id"
        ).fetchall()
        assert len(records) == 2
        assert len({row["amendment_id"] for row in records}) == 2
        assert len({row["resulting_parser_output_id"] for row in records}) == 2
        assert len({row["resulting_content_hash"] for row in records}) == 2
        assert _count(reopened, "application_amendment_invalidations") == 2
        _assert_no_posting_facts(reopened)
        if subject.workspace is not None:
            assert workspace.workspace_identity == manifest.workspace_identity
            assert source_verifier.verify_persisted(
                reopened, subject.intake_public_id
            ).source_event_id.startswith("synthetic-event-")
    finally:
        reopened.close()


def _assert_final_receipt_metadata_readback(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    status: Any,
    description: str,
    category: str,
) -> None:
    from finance_core.bookkeeping_metadata import metadata_hash

    assert status.state == "finalized"
    assert status.transaction_public_id
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "application_posting_receipt_evidence") == 4
    assert _count(connection, "parser_proposal_authorizations") == 1
    assert _count(connection, "receipt_proposal_conversions") == 1
    assert _count(connection, "receipts") == 1
    assert _count(connection, "receipt_item_allocation_fact_sets") == 1
    assert _count(connection, "authoritative_calculation_snapshots") == 1
    assert _count(connection, "receipt_finalization_authorizations") == 1
    assert _count(connection, "application_conditional_authorization_proofs") == 1
    assert _count(connection, "transactions") == 1
    assert _count(connection, "application_amendment_records") == 1

    receipt_row = connection.execute(
        "SELECT id,public_id,merchant,receipt_datetime,net_paid_amount,currency,"
        "description,category,bookkeeping_metadata_version FROM receipts"
    ).fetchone()
    assert receipt_row is not None
    assert tuple(
        receipt_row[field]
        for field in (
            "merchant",
            "receipt_datetime",
            "currency",
            "description",
            "category",
            "bookkeeping_metadata_version",
        )
    ) == (
        "EXAMPLE CAFE",
        "2026-10-08",
        "SGD",
        description,
        category,
        BOOKKEEPING_METADATA_VERSION,
    )
    assert Decimal(str(receipt_row["net_paid_amount"])) == Decimal("12.50")
    metadata = read_receipt_bookkeeping_metadata(connection, str(receipt_row["public_id"]))
    assert metadata is not None
    assert metadata.as_payload() == {
        "version": BOOKKEEPING_METADATA_VERSION,
        "description": description,
        "category": category,
    }
    seal = connection.execute(
        "SELECT metadata_version,description,category,material_hash "
        "FROM application_amendment_receipt_metadata WHERE receipt_id=?",
        (receipt_row["id"],),
    ).fetchone()
    assert seal is not None
    assert tuple(seal) == (
        BOOKKEEPING_METADATA_VERSION,
        description,
        category,
        metadata_hash(metadata),
    )

    item_totals = connection.execute(
        "SELECT COUNT(*) AS item_count,SUM(line_amount) AS total FROM receipt_items "
        "WHERE receipt_id=?",
        (receipt_row["id"],),
    ).fetchone()
    assert item_totals is not None
    assert item_totals["item_count"] == 1
    assert Decimal(str(item_totals["total"])) == Decimal("12.50")
    transaction = connection.execute(
        "SELECT amount,currency,transaction_date,merchant,description,category "
        "FROM transactions WHERE public_id=?",
        (status.transaction_public_id,),
    ).fetchone()
    assert transaction is not None
    assert Decimal(str(transaction["amount"])) == Decimal("12.50")
    assert tuple(
        transaction[field]
        for field in (
            "currency",
            "transaction_date",
            "merchant",
            "description",
            "category",
        )
    ) == (
        "SGD",
        "2026-10-08",
        "EXAMPLE CAFE",
        description,
        category,
    )

    proof = connection.execute(
        "SELECT calculation_snapshot_id FROM application_conditional_authorization_proofs "
        "WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    assert proof is not None
    snapshot = AuthoritativeSnapshotRepository(connection).fetch(str(proof[0]))
    assert snapshot is not None
    snapshot.verify()
    snapshot_input = canonical_json_value(snapshot.input_payload_json, label="input")
    identity = snapshot_input["confirmed_receipt_identity"]
    assert identity["receipt_public_id"] == receipt_row["public_id"]
    assert identity["bookkeeping_metadata"] == metadata.as_payload()
    snapshot_evidence = connection.execute(
        "SELECT evidence_public_id,evidence_hash FROM application_posting_receipt_evidence "
        "WHERE attempt_id=? AND evidence_type='snapshot'",
        (attempt_id,),
    ).fetchone()
    assert snapshot_evidence is not None
    assert snapshot_evidence["evidence_public_id"] == snapshot.snapshot_public_id
    assert snapshot_evidence["evidence_hash"] == snapshot.combined_snapshot_hash
    assert _count(connection, "receipt_item_allocation_fact_sets") == 1


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_conversion_commit",
        "after_snapshot_commit",
        "after_receipt_finalization_commit",
    ],
)
def test_edited_receipt_metadata_recovers_after_each_owner_commit_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    from finance_core.application import posting as posting_module

    (
        connection,
        workspace,
        _manifest,
        posting,
        _old_review,
        proposal,
        _intake_public_id,
        _old_decision,
        _old_display,
        source_verifier,
        decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"edited-metadata-{fault_stage}")
    amendment = _amendment_service(connection, source_verifier=source_verifier)
    review = amendment.prepare(proposal)
    description = "Dinner after recovery"
    category = "dining"
    result = amendment.amend(
        review.review_id,
        _new_evidence(
            connection,
            review,
            {"description": description, "category": category},
            evidence_id=f"metadata-evidence-{fault_stage}",
        )["evidence_id"],
        f"metadata-amendment-{fault_stage}",
    )
    fresh = _prepare_edited_posting(posting, result, connection, f"metadata-confirm-{fault_stage}")
    assert fresh.projection["financial_projection"]["description"] == description
    assert fresh.projection["financial_projection"]["category"] == category

    def lose_return(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"simulated loss after {stage}")

    monkeypatch.setattr(posting_module, "_failure_injection_hook", lose_return)
    with pytest.raises(RuntimeError, match=fault_stage):
        posting.submit_post(fresh.review_id, f"metadata-confirm-{fault_stage}")
    monkeypatch.setattr(posting_module, "_failure_injection_hook", None)
    assert not connection.in_transaction
    attempt_id = str(
        connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
    )
    database_path = _database_path(connection)
    connection.close()

    reopened_workspace, reopened_manifest, reopened_source = _source_verifier_after_reopen(
        _PreparedSubject(
            connection,
            posting,
            fresh,
            proposal,
            "",
            "",
            "",
            source_verifier,
            decision_authority,
            workspace,
            _manifest,
        )
    )
    reopened = connect_temp_db(database_path)
    try:
        recovered_service = PostingService(
            connection=reopened,
            source_verifier=reopened_source,
            human_decision_authority=type(decision_authority)(),
            binding=BINDING,
            clock=lambda: NOW + 10_000,
        )
        recovered = recovered_service.resume_post(attempt_id)
        repeated = recovered_service.resume_post(attempt_id)
        status = recovered_service.get_status(attempt_id)
        assert recovered.transaction_public_id == repeated.transaction_public_id
        assert repeated.transaction_public_id == status.transaction_public_id
        _assert_final_receipt_metadata_readback(
            reopened,
            attempt_id=attempt_id,
            status=status,
            description=description,
            category=category,
        )
        assert (
            _load(
                reopened,
                "synthetic_decisions",
                f"metadata-confirm-{fault_stage}",
                DECISION_KEY,
            )["consumed"]
            is False
        )
        if reopened_workspace is not None:
            assert reopened_workspace.workspace_identity == reopened_manifest.workspace_identity
    finally:
        reopened.close()


def _temporarily_disable_trigger_for_out_of_band_corruption(
    connection: sqlite3.Connection,
    trigger_name: str,
    mutate: Callable[[], None],
) -> None:
    """Simulate offline storage corruption, then restore the original guard."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
    ).fetchone()
    assert row is not None and row["sql"]
    definition = str(row["sql"])
    connection.execute(f'DROP TRIGGER "{trigger_name}"')
    try:
        mutate()
        connection.commit()
    finally:
        connection.execute(definition)
        connection.commit()
    restored = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
    ).fetchone()
    assert restored is not None and restored["sql"] == definition


@pytest.mark.parametrize(
    ("fault_stage", "trigger_name", "corruption"),
    [
        (
            "after_conversion_commit",
            "application_amendment_receipt_metadata_no_delete",
            "missing_receipt_metadata_seal",
        ),
        (
            "after_snapshot_commit",
            "trg_authoritative_snapshots_no_update",
            "snapshot_without_bookkeeping_metadata",
        ),
    ],
)
def test_recovery_refuses_missing_receipt_metadata_proof_or_snapshot_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
    trigger_name: str,
    corruption: str,
) -> None:
    """Corrupt only the disposable DB, restore guards, then require fail-closed recovery."""
    from finance_core.application import posting as posting_module

    (
        connection,
        workspace,
        manifest,
        posting,
        _old_review,
        proposal,
        _intake_public_id,
        _old_decision,
        _old_display,
        source_verifier,
        decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"metadata-corruption-{fault_stage}")
    amendment = _amendment_service(connection, source_verifier=source_verifier)
    review = amendment.prepare(proposal)
    description = "Metadata guarded receipt"
    category = "dining"
    result = amendment.amend(
        review.review_id,
        _new_evidence(
            connection,
            review,
            {"description": description, "category": category},
            evidence_id=f"corrupt-metadata-evidence-{fault_stage}",
        )["evidence_id"],
        f"corrupt-metadata-amendment-{fault_stage}",
    )
    fresh = _prepare_edited_posting(
        posting, result, connection, f"corrupt-metadata-confirm-{fault_stage}"
    )

    def lose_return(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"simulated loss after {stage}")

    monkeypatch.setattr(posting_module, "_failure_injection_hook", lose_return)
    with pytest.raises(RuntimeError, match=fault_stage):
        posting.submit_post(fresh.review_id, f"corrupt-metadata-confirm-{fault_stage}")
    monkeypatch.setattr(posting_module, "_failure_injection_hook", None)
    attempt_id = str(
        connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
    )
    database_path = _database_path(connection)

    if corruption == "missing_receipt_metadata_seal":
        receipt_id = int(connection.execute("SELECT id FROM receipts").fetchone()[0])

        def remove_seal() -> None:
            connection.execute(
                "DELETE FROM application_amendment_receipt_metadata WHERE receipt_id=?",
                (receipt_id,),
            )

        _temporarily_disable_trigger_for_out_of_band_corruption(
            connection, trigger_name, remove_seal
        )
    else:
        snapshot_id = str(
            connection.execute(
                "SELECT evidence_public_id FROM application_posting_receipt_evidence "
                "WHERE attempt_id=? AND evidence_type='snapshot'",
                (attempt_id,),
            ).fetchone()[0]
        )

        def remove_snapshot_metadata() -> None:
            row = connection.execute(
                "SELECT input_payload_json FROM authoritative_calculation_snapshots "
                "WHERE snapshot_public_id=?",
                (snapshot_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(str(row[0]))
            canonical_value = payload.get("value", payload)
            identity = canonical_value["confirmed_receipt_identity"]
            identity.pop("bookkeeping_metadata", None)
            connection.execute(
                "UPDATE authoritative_calculation_snapshots SET input_payload_json=? "
                "WHERE snapshot_public_id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), snapshot_id),
            )

        _temporarily_disable_trigger_for_out_of_band_corruption(
            connection, trigger_name, remove_snapshot_metadata
        )

    assert not connection.in_transaction
    connection.close()
    recovered_workspace, recovered_manifest, recovered_source = _source_verifier_after_reopen(
        _PreparedSubject(
            connection,
            posting,
            fresh,
            proposal,
            "",
            "",
            "",
            source_verifier,
            decision_authority,
            workspace,
            manifest,
        )
    )
    reopened = connect_temp_db(database_path)
    try:
        recovered_service = PostingService(
            connection=reopened,
            source_verifier=recovered_source,
            human_decision_authority=type(decision_authority)(),
            binding=BINDING,
            clock=lambda: NOW + 10_000,
        )
        if corruption == "missing_receipt_metadata_seal":
            with pytest.raises(
                ValueError,
                match="Receipt bookkeeping metadata seal does not verify",
            ):
                read_receipt_bookkeeping_metadata(
                    reopened,
                    str(reopened.execute("SELECT public_id FROM receipts").fetchone()[0]),
                )
            status = recovered_service.get_status(attempt_id)
            assert status.state != "finalized"
            with pytest.raises(
                ValueError,
                match="Receipt bookkeeping metadata seal does not verify",
            ):
                recovered_service.resume_post(attempt_id)
        else:
            with pytest.raises(
                SnapshotVerificationError,
                match="Authoritative calculation snapshot hash mismatch",
            ):
                recovered_service.get_status(attempt_id)
            with pytest.raises(
                SnapshotVerificationError,
                match="Authoritative calculation snapshot hash mismatch",
            ):
                recovered_service.resume_post(attempt_id)
        assert _count(reopened, "application_posting_decisions") == 1
        assert _count(reopened, "receipt_proposal_conversions") == 1
        assert _count(reopened, "receipts") == 1
        assert _count(reopened, "transactions") == 0
        assert _count(reopened, "receipt_finalization_authorizations") == 0
        if fault_stage == "after_snapshot_commit":
            assert _count(reopened, "authoritative_calculation_snapshots") == 1
        if recovered_workspace is not None:
            assert recovered_workspace.workspace_identity == recovered_manifest.workspace_identity
    finally:
        reopened.close()
