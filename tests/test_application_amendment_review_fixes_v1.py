"""Actual-owner regressions for the consolidated PR06-P Candidate A findings."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import connect_temp_db
from test_application_amendment_v1 import (
    _amendment_service,
    _apply,
    _count,
    _new_evidence,
    _prepare_edited_posting,
)
from test_application_posting_recovery_v1 import (
    _prepare_receipt_subject,
    _prepare_text_subject,
    _temporarily_drop_triggers,
)

from finance_core.application import amendment
from finance_core.application import posting as posting_module
from finance_core.application.amendment_contract import AmendmentError
from finance_core.application.posting import PostingError, PostingService
from finance_core.application.review import ReviewUnavailableError
from finance_core.parser_proposals.amendment_lineage import AmendmentLineageError
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.receipt_staging_runner import local_intake


@pytest.mark.parametrize(("origin", "target"), [("JPY", "SGD"), ("SGD", "JPY")])
def test_currency_scale_echo_is_not_fresh_amount_provenance(tmp_path, origin, target):
    connection, _, _, posting, _, proposal, _, _, _, source, _ = _prepare_receipt_subject(
        tmp_path, f"scale-echo-{origin}"
    )
    try:
        service = _amendment_service(connection, source_verifier=source)
        first = _apply(
            connection,
            service,
            service.prepare(proposal),
            {"amount": "12" if origin == "JPY" else "12.00", "currency": origin},
            evidence_id="first",
            amendment_id="first",
        )
        patch = {"amount": "12.00" if target == "SGD" else "12", "currency": target}
        second = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            patch,
            evidence_id="second",
            amendment_id="second",
        )
        revision = connection.execute(
            "SELECT field_updates_json,applied_field_updates_json,replacement_payload_json "
            "FROM receipt_proposal_revisions WHERE correction_public_id=?",
            (second.publication_public_id,),
        ).fetchone()
        assert json.loads(revision[0]) == patch
        assert json.loads(revision[1]) == {"currency": target}
        amount_evidence = [
            item
            for item in json.loads(revision[2])["field_evidence"]
            if item["field_name"] == "amount"
        ]
        assert amount_evidence
        assert all(
            item.get("correction_public_id") != second.publication_public_id
            for item in amount_evidence
        )
        fresh = _prepare_edited_posting(posting, second, connection, "scale-fresh-confirmation")
        assert fresh.projection["financial_projection"]["amount"] == patch["amount"]
        posted = posting.submit_post(fresh.review_id, "scale-fresh-confirmation")
        assert posted.state == "finalized"
        assert connection.execute("SELECT currency FROM transactions").fetchone()[0] == target
        assert _count(connection, "transactions") == 1
    finally:
        connection.close()


def test_currency_echo_cannot_round_incompatible_destination(tmp_path):
    connection, _, _, _, _, proposal, _, _, _, source, _ = _prepare_receipt_subject(
        tmp_path, "scale-invalid-jpy"
    )
    try:
        service = _amendment_service(connection, source_verifier=source)
        review = service.prepare(proposal)
        _new_evidence(connection, review, {"amount": "12.50", "currency": "JPY"})
        with pytest.raises(AmendmentError):
            service.amend(review.review_id, "amendment-evidence-one", "invalid-destination")
        assert _count(connection, "application_amendment_records") == 0
        assert _count(connection, "receipt_proposal_revisions") == 0
        assert _count(connection, "transactions") == 0
        assert not connection.in_transaction
    finally:
        connection.close()


class _ReceiptReady(Exception):
    def __init__(self, service, proposal):
        self.service, self.proposal = service, proposal


def _prepare_missing_date_receipt(tmp_path, monkeypatch, unknown_field=None):
    """Change the synthetic engine output before genuine OCR ingestion/publication."""
    real_intake = local_intake.run_local_receipt_intake

    def missing_date_intake(*args, engine, **kwargs):
        real_extract = engine.extract

        def extract(*extract_args, **extract_kwargs):
            result = real_extract(*extract_args, **extract_kwargs)
            replacements = {"2026-10-08": "DATE UNKNOWN"}
            if unknown_field == "amount":
                replacements["TOTAL"] = "UNKNOWN"
            elif unknown_field == "currency":
                replacements["SGD"] = "UNKNOWN"
            return dataclasses.replace(
                result,
                blocks=tuple(
                    dataclasses.replace(block, text=replacements.get(block.text, block.text))
                    for block in result.blocks
                ),
            )

        engine.extract = extract
        return real_intake(*args, engine=engine, **kwargs)

    def stop_before_initial_posting(service, proposal):
        raise _ReceiptReady(service, proposal)

    with monkeypatch.context() as context:
        context.setattr(local_intake, "run_local_receipt_intake", missing_date_intake)
        context.setattr(PostingService, "prepare", stop_before_initial_posting)
        with pytest.raises(_ReceiptReady) as ready:
            _prepare_receipt_subject(tmp_path, "missing-date-owner")
    return ready.value.service._conn, ready.value.service, ready.value.proposal


@pytest.mark.parametrize("damage", [None, "completion", "low_confidence", "amount", "currency"])
def test_missing_ocr_date_requires_genuine_completion_then_fresh_exact_posting(
    tmp_path,
    monkeypatch,
    damage,
):
    connection, posting, proposal = _prepare_missing_date_receipt(
        tmp_path, monkeypatch, damage if damage in {"amount", "currency"} else None
    )
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        old = service.prepare(proposal)
        assert old.projection["editable_values"]["transaction_date"] is None
        assert (
            "transaction_date_not_found"
            in old.projection["proposal_review"]["ambiguity_indicators"]
        )
        with pytest.raises(PostingError):
            posting.prepare(proposal)
        result = _apply(connection, service, old, {"transaction_date": "2026-10-07"})
        if damage == "completion":
            triggers = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='parser_proposal_completions' AND sql LIKE '%BEFORE UPDATE%'"
                )
            )
            _temporarily_drop_triggers(
                connection,
                triggers,
                lambda: connection.execute(
                    "UPDATE parser_proposal_completions SET field_updates_json='{}'"
                ),
            )
        elif damage == "low_confidence":
            connection.execute(
                "UPDATE parser_outputs SET confidence_score=0.1 WHERE public_id=?",
                (result.proposal_public_id,),
            )
            connection.commit()
        if damage is not None:
            with pytest.raises(PostingError):
                posting.prepare(result.proposal_public_id)
            assert _count(connection, "transactions") == 0
            assert _count(connection, "application_posting_decisions") == 0
            return
        fresh = _prepare_edited_posting(posting, result, connection, "exact-date-confirmation")
        assert (
            "transaction_date_not_found"
            in fresh.projection["proposal_review"]["ambiguity_indicators"]
        )
        assert fresh.projection["financial_projection"]["transaction_date"] == "2026-10-07"
        posted = posting.submit_post(fresh.review_id, "exact-date-confirmation")
        assert posted.state == "finalized"
        assert (
            connection.execute("SELECT transaction_date FROM transactions").fetchone()[0]
            == "2026-10-07"
        )
        assert _count(connection, "transactions") == 1
        assert _count(connection, "authoritative_calculation_snapshots") == 1
        assert posting.resume_post(posted.attempt_id) == posted
    finally:
        connection.close()


def _subject(tmp_path, kind):
    if kind == "receipt_supersession":
        connection, _, _, posting, _, proposal, _, _, _, source, _ = _prepare_receipt_subject(
            tmp_path, "actual-receipt-owner"
        )
    else:
        connection = connect_temp_db(tmp_path / "synthetic.sqlite")
        from conftest import apply_migrations

        apply_migrations(connection)
        connection.commit()
        posting, _, row, _, _, _ = _prepare_text_subject(connection, suffix="actual-text-owner")
        proposal, source = row["public_id"], posting._source_port
    patch = {"description": "Lunch with Jo"} if kind == "completion" else {"amount": "14.00"}
    return (
        connection,
        posting,
        proposal,
        _amendment_service(connection, source_verifier=source),
        patch,
    )


def _owner_state(connection):
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in (
            "parser_outputs",
            "raw_intake_records",
            "parser_proposal_completions",
            "receipt_proposal_revisions",
            "parser_text_amendment_revisions",
            "application_amendment_records",
            "application_amendment_invalidations",
            "parser_proposal_events",
            "financial_audit_events",
        )
    }


@pytest.mark.parametrize("kind", ["completion", "text_supersession", "receipt_supersession"])
@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["before_amendment_seal", "before_commit", "after_commit"])
def test_abrupt_owner_exit_releases_writer_and_reconnects_atomically(
    tmp_path,
    monkeypatch,
    kind,
    failure,
    stage,
):
    connection, _, proposal, service, patch = _subject(tmp_path, kind)
    try:
        review = service.prepare(proposal)
        _new_evidence(connection, review, patch)
        before = _owner_state(connection)
        path = Path(connection.execute("PRAGMA database_list").fetchone()[2])

        def interrupt(actual):
            if actual == stage:
                raise failure("synthetic abrupt exit")

        monkeypatch.setattr(amendment, "_failure_injection_hook", interrupt)
        with pytest.raises(failure):
            service.amend(review.review_id, "amendment-evidence-one", "abrupt-owner")
        monkeypatch.setattr(amendment, "_failure_injection_hook", None)
        assert not connection.in_transaction
        connection.commit()  # An unrelated caller commit must not persist half an amendment.
        observed = _owner_state(connection)
        if stage == "after_commit":
            assert _count(connection, "application_amendment_records") == 1
            assert service.get_status("abrupt-owner").is_current
        else:
            assert observed == before
        reopened = connect_temp_db(path)
        try:
            reopened.execute("BEGIN IMMEDIATE")  # No leaked writer/transaction survives.
            reopened.rollback()
            assert _owner_state(reopened) == observed
            assert _count(reopened, "transactions") == 0
        finally:
            reopened.close()
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["completion", "text_supersession", "receipt_supersession"])
def test_caller_owned_work_remains_open_and_unmodified(tmp_path, kind):
    connection, _, proposal, service, patch = _subject(tmp_path, kind)
    try:
        review = service.prepare(proposal)
        _new_evidence(connection, review, patch)
        connection.execute("CREATE TABLE caller_work (value TEXT)")
        connection.commit()
        connection.execute("INSERT INTO caller_work VALUES ('keep')")
        with pytest.raises(AmendmentError, match="caller work"):
            service.amend(review.review_id, "amendment-evidence-one", "caller-work")
        assert connection.in_transaction
        assert connection.execute("SELECT value FROM caller_work").fetchone()[0] == "keep"
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0] == 0
    finally:
        connection.close()


def _insert_unsealed_child(connection, parent_id):
    connection.execute(
        "INSERT INTO parser_outputs (public_id,source_type,source_public_id,statement_batch_id,"
        "attachment_id,parser_name,parser_version,raw_text,parsed_payload,normalized_payload,"
        "confidence_score,parse_status,parent_parser_output_id) SELECT ?,source_type,"
        "source_public_id,statement_batch_id,attachment_id,parser_name,parser_version,raw_text,"
        "parsed_payload,normalized_payload,confidence_score,parse_status,id FROM parser_outputs "
        "WHERE id=?",
        ("unexpected-unsealed-child", parent_id),
    )
    connection.commit()


@pytest.mark.parametrize("kind", ["completion", "text_supersession", "receipt_supersession"])
@pytest.mark.parametrize("phase", ["pending", "accepted", "finalized"])
def test_unseen_child_refuses_status_prepare_submit_and_resume(tmp_path, monkeypatch, kind, phase):
    connection, posting, proposal, service, patch = _subject(tmp_path, kind)
    try:
        result = _apply(connection, service, service.prepare(proposal), patch)
        fresh = _prepare_edited_posting(posting, result, connection, "fork-confirmation")
        attempt_id = None
        if phase == "accepted":

            def stop(actual):
                if actual == "after_acceptance_commit":
                    raise RuntimeError("accepted before conversion")

            monkeypatch.setattr(posting_module, "_failure_injection_hook", stop)
            with pytest.raises(RuntimeError):
                posting.submit_post(fresh.review_id, "fork-confirmation")
            monkeypatch.setattr(posting_module, "_failure_injection_hook", None)
            attempt_id = connection.execute(
                "SELECT attempt_id FROM application_posting_attempts"
            ).fetchone()[0]
        elif phase == "finalized":
            attempt_id = posting.submit_post(fresh.review_id, "fork-confirmation").attempt_id
        leaf = ParserProposalRepository(connection).get_by_public_id(result.proposal_public_id)
        _insert_unsealed_child(connection, leaf["id"])
        with pytest.raises(AmendmentError):
            service.get_status(result.amendment_id)
        with pytest.raises(PostingError):
            posting.prepare(result.proposal_public_id)
        with pytest.raises(ReviewUnavailableError if phase == "pending" else PostingError):
            posting.submit_post(fresh.review_id, "fork-confirmation")
        if attempt_id is not None:
            with pytest.raises(AmendmentLineageError):
                posting.get_status(attempt_id)
            with pytest.raises(AmendmentLineageError):
                posting.resume_post(attempt_id)
        assert _count(connection, "transactions") == (1 if phase == "finalized" else 0)
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["text_supersession", "receipt_supersession"])
def test_historical_completion_replay_checks_successor_and_ancestry_siblings(tmp_path, kind):
    connection, _, proposal, service, patch = _subject(tmp_path, kind)
    try:
        old = service.prepare(proposal)
        first = _apply(connection, service, old, {"description": "First edit"})
        second = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            patch,
            evidence_id="successor-evidence",
            amendment_id="successor",
        )
        assert not service.amend(
            old.review_id, "amendment-evidence-one", first.amendment_id
        ).is_current
        assert service.get_status(second.amendment_id).is_current
        first_proposal = ParserProposalRepository(connection).get_by_public_id(
            first.proposal_public_id
        )
        _insert_unsealed_child(connection, first_proposal["id"])
        with pytest.raises(AmendmentError):
            service.amend(old.review_id, "amendment-evidence-one", first.amendment_id)
        with pytest.raises(AmendmentError):
            service.get_status(second.amendment_id)
        current = connection.execute("SELECT parser_output_id FROM raw_intake_records").fetchone()[
            0
        ]
        assert (
            current
            == ParserProposalRepository(connection).get_by_public_id(second.proposal_public_id)[
                "id"
            ]
        )
    finally:
        connection.close()


@pytest.mark.parametrize("damage", [None, "tampered_result", "missing_link"])
def test_genuine_ai_root_topology_and_historical_successor_replay(tmp_path, damage):
    from test_application_amendment_ai_v1 import (
        _create_sealed_ai_root,
        _persist_application_source,
    )

    connection, intake, proposal = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(connection, intake)
        service = _amendment_service(connection)
        old = service.prepare(proposal["public_id"])
        first = _apply(connection, service, old, {"description": "AI root completed"})
        second = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            {"amount": "13.75"},
            evidence_id="ai-successor-evidence",
            amendment_id="ai-successor",
        )
        assert service.get_status(second.amendment_id).is_current
        assert not service.amend(
            old.review_id, "amendment-evidence-one", first.amendment_id
        ).is_current
        if damage == "tampered_result":
            _temporarily_drop_triggers(
                connection,
                ("trg_ai_fallback_results_no_update",),
                lambda: connection.execute(
                    "UPDATE ai_fallback_results SET response_sha256=? WHERE id="
                    "(SELECT result_id FROM ai_fallback_proposal_links WHERE parser_output_id=?)",
                    ("0" * 64, proposal["id"]),
                ),
            )
        elif damage == "missing_link":
            _temporarily_drop_triggers(
                connection,
                ("trg_ai_fallback_links_no_delete",),
                lambda: connection.execute(
                    "DELETE FROM ai_fallback_proposal_links WHERE parser_output_id=?",
                    (proposal["id"],),
                ),
            )
        if damage is not None:
            with pytest.raises(AmendmentError):
                service.get_status(second.amendment_id)
            with pytest.raises(AmendmentError):
                service.amend(old.review_id, "amendment-evidence-one", first.amendment_id)
        assert _count(connection, "application_amendment_records") == 2
        assert _count(connection, "transactions") == 0
    finally:
        connection.close()
