"""Actual OCR/amendment/posting owners resolve only complete human receipt values."""

from __future__ import annotations

import copy
import dataclasses
import json
from decimal import Decimal

import pytest
from test_application_amendment_review_fixes_v1 import _ReceiptReady
from test_application_amendment_v1 import (
    _amendment_service,
    _apply,
    _assert_no_posting_facts,
    _count,
    _prepare_edited_posting,
)
from test_application_posting_recovery_v1 import (
    NOW,
    _prepare_receipt_subject,
    _temporarily_drop_triggers,
)

from finance_core.application.posting import PostingError, PostingService
from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.intake.receipt_ocr_evidence import ReceiptOcrExtractionStatus, ReceiptOcrLimits
from finance_core.parser_proposals.content_hash import (
    compute_effective_proposal_content_hash,
    compute_proposal_content_hash,
)
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_revision import HumanRevisionLineageError
from finance_core.parser_proposals.receipt_facts_conversion import (
    ConversionEvidenceLineageError,
    _durable_resolution_provenance,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.receipt_staging_runner import local_intake

PATCH = {
    "amount": "14.25",
    "currency": "SGD",
    "transaction_date": "2026-10-07",
    "merchant": "Harbour Cafe",
    "description": "Dinner",
    "category": "dining",
}


def _subject(tmp_path, monkeypatch, *, parsed=False, status="succeeded"):
    """Use genuinely successful OCR with no deterministic financial interpretation."""
    real_intake = local_intake.run_local_receipt_intake

    def intake(*args, engine, **kwargs):
        if status == "resource_rejected":
            kwargs["ocr_limits"] = ReceiptOcrLimits(max_attachment_bytes=1)
        extract = engine.extract

        def unparseable(*extract_args, **extract_kwargs):
            result = extract(*extract_args, **extract_kwargs)
            return dataclasses.replace(
                result,
                status=ReceiptOcrExtractionStatus(status),
                blocks=tuple(
                    dataclasses.replace(
                        block, text=block.text if parsed else "12345", confidence_scaled=None
                    )
                    for block in result.blocks
                )
                if status == "succeeded"
                else (),
            )

        engine.extract = unparseable
        return real_intake(*args, engine=engine, **kwargs)

    def stop(service, proposal):
        raise _ReceiptReady(service, proposal)

    with monkeypatch.context() as context:
        context.setattr(local_intake, "run_local_receipt_intake", intake)
        context.setattr(PostingService, "prepare", stop)
        with pytest.raises(_ReceiptReady) as ready:
            _prepare_receipt_subject(tmp_path, "human-resolution")
    posting = ready.value.service
    return posting._conn, posting, ready.value.proposal


def test_complete_human_receipt_needs_new_confirmation_then_posts_exactly_once(
    tmp_path, monkeypatch
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        original = dict(
            connection.execute(
                "SELECT * FROM parser_outputs WHERE public_id=?", (proposal,)
            ).fetchone()
        )
        payload = json.loads(original["parsed_payload"])
        raw_source = tuple(
            connection.execute(
                "SELECT raw_input,source_content_hash,attachment_path,attachment_hash "
                "FROM raw_intake_records"
            ).fetchone()
        )
        assert all(payload.get(field) is None for field in PATCH)
        assert original["confidence_score"] is None
        assert set(payload["ambiguity_flags"]) == {
            "merchant_not_determined",
            "total_not_found",
            "transaction_date_not_found",
        }
        with pytest.raises(PostingError):
            posting.prepare(proposal)
        service = _amendment_service(connection, source_verifier=posting._source_port)
        result = _apply(connection, service, service.prepare(proposal), PATCH)
        _assert_no_posting_facts(connection)
        review = _prepare_edited_posting(posting, result, connection, "new-confirm")
        assert {k: review.projection["financial_projection"][k] for k in PATCH} == PATCH
        assert {"low_confidence", "merchant_not_determined"} <= set(
            review.projection["proposal_review"]["ambiguity_indicators"]
        )
        _assert_no_posting_facts(connection)
        current = connection.execute(
            "SELECT confidence_score,parsed_payload FROM parser_outputs WHERE public_id=?",
            (result.proposal_public_id,),
        ).fetchone()
        assert current[0] is None
        assert json.loads(current[1])["ambiguity_flags"] == payload["ambiguity_flags"]
        posted = posting.submit_post(review.review_id, "new-confirm")
        assert posted.state == "finalized"
        transaction = connection.execute(
            "SELECT amount,currency,transaction_date,merchant,description,category "
            "FROM transactions"
        ).fetchone()
        assert Decimal(str(transaction[0])) == Decimal("14.25")
        assert tuple(transaction)[1:] == ("SGD", "2026-10-07", "Harbour Cafe", "Dinner", "dining")
        assert _count(connection, "transactions") == 1
        assert _count(connection, "receipts") == 1
        assert _count(connection, "receipt_item_allocation_fact_sets") == 1
        assert _count(connection, "authoritative_calculation_snapshots") == 1
        assert posting.resume_post(posted.attempt_id) == posted
        with pytest.raises(PostingError):
            posting.submit_post(review.review_id, "new-confirm")
        posting._clock = lambda: NOW + 10_000
        assert posting.resume_post(posted.attempt_id) == posted
        assert posting.get_status(posted.attempt_id) == posted
        snapshot = canonical_json_value(
            connection.execute(
                "SELECT input_payload_json FROM authoritative_calculation_snapshots"
            ).fetchone()[0],
            label="human receipt snapshot",
        )["confirmed_receipt_identity"]
        assert (snapshot["merchant"], snapshot["receipt_date"], snapshot["currency"]) == (
            "Harbour Cafe",
            "2026-10-07",
            "SGD",
        )
        assert snapshot["bookkeeping_metadata"] == {
            "version": "application_bookkeeping_metadata_v1",
            "description": "Dinner",
            "category": "dining",
        }
        calculation = canonical_json_value(
            connection.execute(
                "SELECT output_payload_json FROM authoritative_calculation_snapshots"
            ).fetchone()[0],
            label="human receipt calculation",
        )
        assert calculation["total_paid"] == calculation["payer_own_share"] == "14.25"
        assert calculation["participant_shares"] == {"ptcp_posting_self": "14.25"}
        assert calculation["total_to_collect"] == "0"
        assert calculation["settlement_obligations"] == []
        assert calculation["receipts"][0]["items"] == [
            {
                "allocation_method": "manual",
                "amount": "14.25",
                "description": "Receipt total",
                "participant_allocations": {"ptcp_posting_self": "14.25"},
            }
        ]
        assert (
            tuple(
                connection.execute(
                    "SELECT raw_input,source_content_hash,attachment_path,attachment_hash "
                    "FROM raw_intake_records"
                ).fetchone()
            )
            == raw_source
        )
        retained = dict(
            connection.execute(
                "SELECT * FROM parser_outputs WHERE public_id=?", (proposal,)
            ).fetchone()
        )
        assert retained["parsed_payload"] == original["parsed_payload"]
        assert retained["raw_text"] == original["raw_text"]
        assert retained["confidence_score"] is None
    finally:
        connection.close()


@pytest.mark.parametrize("field", sorted(PATCH))
def test_missing_any_human_field_cannot_gain_low_confidence_permission(
    tmp_path, monkeypatch, field
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        patch = {key: value for key, value in PATCH.items() if key != field}
        with pytest.raises(ValueError):
            result = _apply(connection, service, service.prepare(proposal), patch)
            posting.prepare(result.proposal_public_id)
        _assert_no_posting_facts(connection)
        assert not connection.in_transaction
    finally:
        connection.close()


@pytest.mark.parametrize(
    "patch", [{"transaction_date": "2026-10-07"}, {"merchant": "Harbour Cafe"}]
)
def test_partial_human_receipt_still_refuses(tmp_path, monkeypatch, patch):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        result = _apply(connection, service, service.prepare(proposal), patch)
        with pytest.raises(PostingError):
            posting.prepare(result.proposal_public_id)
        _assert_no_posting_facts(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("field", ["amount", "currency", "transaction_date", "merchant"])
def test_echo_of_machine_field_is_not_material_human_resolution(tmp_path, monkeypatch, field):
    connection, posting, proposal = _subject(tmp_path, monkeypatch, parsed=True)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        review = service.prepare(proposal)
        patch = {**PATCH, "currency": "USD"}
        patch[field] = review.projection["editable_values"][field]
        if field == "amount":
            patch[field] = "12.50"  # canonical numeric echo, not a new correction
        result = _apply(connection, service, review, patch)
        with pytest.raises(PostingError):
            posting.prepare(result.proposal_public_id)
        _assert_no_posting_facts(connection)
    finally:
        connection.close()


def test_complete_receipt_inherits_exact_human_provenance_after_partial_edit(tmp_path, monkeypatch):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        first = _apply(connection, service, service.prepare(proposal), PATCH)
        old = _prepare_edited_posting(posting, first, connection, "old-confirm")
        second = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            {"amount": "15.00", "description": "Dinner with Jo"},
            evidence_id="second",
            amendment_id="second",
        )
        with pytest.raises(PostingError):
            posting.submit_post(old.review_id, "old-confirm")
        _assert_no_posting_facts(connection)
        fresh = _prepare_edited_posting(posting, second, connection, "fresh-confirm")
        assert fresh.projection["financial_projection"]["description"] == "Dinner with Jo"
        assert posting.submit_post(fresh.review_id, "fresh-confirm").state == "finalized"
        assert _count(connection, "transactions") == 1
    finally:
        connection.close()


@pytest.mark.parametrize("status", ["no_text", "engine_failed", "resource_rejected"])
def test_full_human_patch_cannot_bypass_failed_original_extraction(tmp_path, monkeypatch, status):
    connection, posting, proposal = _subject(tmp_path, monkeypatch, status=status)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        result = _apply(connection, service, service.prepare(proposal), PATCH)
        with pytest.raises(PostingError):
            posting.prepare(result.proposal_public_id)
        _assert_no_posting_facts(connection)
    finally:
        connection.close()


def _damage_payload(connection, proposal_id, change):
    row = connection.execute(
        "SELECT parsed_payload FROM parser_outputs WHERE public_id=?", (proposal_id,)
    ).fetchone()
    payload = json.loads(row[0])
    change(payload)
    connection.execute(
        "UPDATE parser_outputs SET parsed_payload=?,normalized_payload=? WHERE public_id=?",
        (
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            proposal_id,
        ),
    )
    connection.commit()


@pytest.mark.parametrize(
    "damage", [*sorted(PATCH), "stripped_evidence", "forged_evidence", "unknown_flags"]
)
@pytest.mark.parametrize("phase", ["prepare", "submit", "accepted"])
def test_six_field_tampering_refuses_before_write_and_during_result_recovery(
    tmp_path, monkeypatch, damage, phase
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        result = _apply(connection, service, service.prepare(proposal), PATCH)
        review = _prepare_edited_posting(posting, result, connection, "confirm")
        posted = posting.submit_post(review.review_id, "confirm") if phase == "accepted" else None

        def change(payload):
            if damage in PATCH:
                payload[damage] = {
                    "amount": "15.00",
                    "currency": "USD",
                    "transaction_date": "2026-10-06",
                }.get(damage, "Forged")
            elif damage == "stripped_evidence":
                payload["field_evidence"] = []
            elif damage == "forged_evidence":
                payload["field_evidence"][0]["correction_public_id"] = "forged"
            else:
                payload["ambiguity_flags"] += ["future_unknown"]

        _damage_payload(connection, result.proposal_public_id, change)
        changes = connection.total_changes
        with pytest.raises((ValueError, HumanRevisionLineageError)):
            if phase == "prepare":
                posting.prepare(result.proposal_public_id)
            elif phase == "submit":
                posting.submit_post(review.review_id, "confirm")
            else:
                posting.resume_post(posted.attempt_id)
        assert connection.total_changes == changes
        if posted:
            with pytest.raises((ValueError, HumanRevisionLineageError)):
                posting.get_status(posted.attempt_id)
            assert _count(connection, "transactions") == 1
        else:
            _assert_no_posting_facts(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "Dinner with Jo"),
        ("merchant", "Second Cafe"),
        ("transaction_date", "2026-10-06"),
        ("category", "transport"),
    ],
)
def test_full_human_receipt_remains_eligible_after_each_nonmonetary_only_completion(
    tmp_path, monkeypatch, field, value
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        first = _apply(connection, service, service.prepare(proposal), PATCH)
        second = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            {field: value},
            evidence_id="second",
            amendment_id="second",
        )
        fresh = _prepare_edited_posting(posting, second, connection, "completion-confirm")
        expected = {**PATCH, field: value}
        assert {key: fresh.projection["financial_projection"][key] for key in PATCH} == expected
        assert (
            connection.execute(
                "SELECT publication_kind FROM application_amendment_records "
                "WHERE amendment_id='second'"
            ).fetchone()[0]
            == "completion"
        )
        _assert_no_posting_facts(connection)
        posted = posting.submit_post(fresh.review_id, "completion-confirm")
        assert posted.state == "finalized"
        transaction = connection.execute(
            "SELECT currency,transaction_date,merchant,description,category FROM transactions"
        ).fetchone()
        assert tuple(transaction) == tuple(
            expected[key]
            for key in ("currency", "transaction_date", "merchant", "description", "category")
        )
        snapshot = canonical_json_value(
            connection.execute(
                "SELECT input_payload_json FROM authoritative_calculation_snapshots"
            ).fetchone()[0],
            label="completed receipt snapshot",
        )["confirmed_receipt_identity"]
        assert snapshot["merchant"] == expected["merchant"]
        assert snapshot["receipt_date"] == expected["transaction_date"]
        assert snapshot["bookkeeping_metadata"]["description"] == expected["description"]
        assert snapshot["bookkeeping_metadata"]["category"] == expected["category"]
        assert tuple(
            connection.execute(
                "SELECT merchant,receipt_datetime,description,category FROM receipts"
            ).fetchone()
        ) == (
            expected["merchant"],
            expected["transaction_date"],
            expected["description"],
            expected["category"],
        )
        assert _count(connection, "transactions") == 1
        assert _count(connection, "authoritative_calculation_snapshots") == 1
        posting._clock = lambda: NOW + 10_000
        assert posting.resume_post(posted.attempt_id) == posted
    finally:
        connection.close()


@pytest.mark.parametrize(
    "damage", ["amendment_seal", "actual_revision", "ocr_link", "source", "sibling"]
)
@pytest.mark.parametrize("phase", ["submit", "accepted"])
def test_durable_human_receipt_proof_damage_blocks_confirmation_or_readback(
    tmp_path, monkeypatch, damage, phase
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        result = _apply(connection, service, service.prepare(proposal), PATCH)
        review = _prepare_edited_posting(posting, result, connection, "confirm")
        posted = posting.submit_post(review.review_id, "confirm") if phase == "accepted" else None
        if damage == "source":
            connection.execute("UPDATE synthetic_sources SET signature='forged'")
            connection.commit()
        elif damage == "sibling":
            from test_application_amendment_review_fixes_v1 import _insert_unsealed_child

            _insert_unsealed_child(
                connection,
                connection.execute(
                    "SELECT id FROM parser_outputs WHERE public_id=?", (proposal,)
                ).fetchone()[0],
            )
        else:
            table, update = {
                "amendment_seal": (
                    "application_amendment_records",
                    "record_hash='" + "0" * 64 + "'",
                ),
                "actual_revision": (
                    "receipt_proposal_revisions",
                    "applied_field_updates_json='{}'",
                ),
                "ocr_link": (
                    "receipt_ocr_proposal_links",
                    "proposal_result_hash='" + "0" * 64 + "'",
                ),
            }[damage]
            triggers = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
                    "AND sql LIKE '%BEFORE UPDATE%'",
                    (table,),
                )
            )
            _temporarily_drop_triggers(
                connection, triggers, lambda: connection.execute(f"UPDATE {table} SET {update}")
            )
        changes = connection.total_changes
        with pytest.raises((ValueError, HumanRevisionLineageError)):
            if posted:
                posting.resume_post(posted.attempt_id)
            else:
                posting.submit_post(review.review_id, "confirm")
        assert connection.total_changes == changes
        if posted:
            with pytest.raises((ValueError, HumanRevisionLineageError)):
                posting.get_status(posted.attempt_id)
            assert _count(connection, "transactions") == 1
        else:
            _assert_no_posting_facts(connection)
    finally:
        connection.close()


def test_unedited_machine_receipt_with_complete_financial_fields_still_refuses_low_confidence(
    tmp_path, monkeypatch
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch, parsed=True)
    try:
        with pytest.raises(PostingError):
            posting.prepare(proposal)
        _assert_no_posting_facts(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("inherited", [False, True])
def test_latest_field_completion_and_inherited_completion_after_supersession_post_once(
    tmp_path, monkeypatch, inherited
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        result = _apply(connection, service, service.prepare(proposal), PATCH)
        original_material = tuple(
            connection.execute(
                "SELECT material_json,record_hash FROM application_amendment_records"
            ).fetchone()
        )
        original_payload = connection.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id=?",
            (result.proposal_public_id,),
        ).fetchone()[0]
        patches = [
            {"description": "Dinner with Jo"},
            {"description": "Dinner with Li"},
            {"category": "social"},
        ]
        if inherited:
            patches += [{"amount": "16.50"}, {"merchant": "Second Cafe"}]
        expected = dict(PATCH)
        for index, patch in enumerate(patches):
            old = _prepare_edited_posting(posting, result, connection, f"old-{index}")
            result = _apply(
                connection,
                service,
                service.prepare(result.proposal_public_id),
                patch,
                evidence_id=f"edit-{index}",
                amendment_id=f"edit-{index}",
            )
            expected.update(patch)
            with pytest.raises((PostingError, HumanRevisionLineageError)):
                posting.submit_post(old.review_id, f"old-{index}")
            _assert_no_posting_facts(connection)
        fresh = _prepare_edited_posting(posting, result, connection, "chain-confirm")
        assert {
            field: fresh.projection["financial_projection"][field] for field in PATCH
        } == expected
        posted = posting.submit_post(fresh.review_id, "chain-confirm")
        assert posted.state == "finalized"
        assert (
            _count(connection, "transactions")
            == _count(connection, "authoritative_calculation_snapshots")
            == 1
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT material_json,record_hash FROM application_amendment_records "
                    "WHERE amendment_id='amendment-one'"
                ).fetchone()
            )
            == original_material
        )
        assert (
            connection.execute("SELECT parsed_payload FROM parser_outputs WHERE id=2").fetchone()[0]
            == original_payload
        )
        assert posting.resume_post(posted.attempt_id) == posted
    finally:
        connection.close()


@pytest.mark.parametrize(
    "damage",
    [
        "bare_completion",
        "field_updates",
        "foreign_proposal",
        "gap",
        "invalidation",
        "audit",
        "same_hash_full_payload",
    ],
)
@pytest.mark.parametrize("phase", ["submit", "accepted"])
def test_current_completion_owner_damage_is_not_historical_resolution_permission(
    tmp_path, monkeypatch, damage, phase
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        first = _apply(connection, service, service.prepare(proposal), PATCH)
        second = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            {"description": "Dinner with Jo"},
            evidence_id="second",
            amendment_id="second",
        )
        review = _prepare_edited_posting(posting, second, connection, "confirm")
        posted = posting.submit_post(review.review_id, "confirm") if phase == "accepted" else None
        live = ParserProposalRepository(connection).get_by_public_id(second.proposal_public_id)
        old_hash = compute_effective_proposal_content_hash(connection, live)
        if damage == "same_hash_full_payload":
            payload, _, _ = resolve_effective_payload(connection, live)
            payload["field_evidence"][0]["untrusted_claim"] = True
            table, update, params = (
                "parser_proposal_completions",
                "completed_payload_json=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
            )
        else:
            table, update, params = {
                "bare_completion": (
                    "application_amendment_records",
                    "publication_public_id=?",
                    ("orphan",),
                ),
                "field_updates": ("parser_proposal_completions", "field_updates_json=?", ("{}",)),
                "foreign_proposal": ("parser_proposal_completions", "parser_output_id=?", (1,)),
                "gap": ("parser_proposal_completions", "version_number=?", (2,)),
                "invalidation": (
                    "application_amendment_invalidations",
                    "base_content_hash=?",
                    ("0" * 64,),
                ),
                "audit": ("financial_audit_events", "event_payload_json=?", ("{}",)),
            }[damage]
        where = {
            "application_amendment_records": "amendment_id='second'",
            "application_amendment_invalidations": "amendment_id='second'",
            "parser_proposal_completions": "version_number=1",
            "financial_audit_events": "event_type='parser_proposal_completed'",
        }[table]
        triggers = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
                "AND sql LIKE '%BEFORE UPDATE%'",
                (table,),
            )
        )
        _temporarily_drop_triggers(
            connection,
            triggers,
            lambda: connection.execute(f"UPDATE {table} SET {update} WHERE {where}", params),
        )
        if damage == "same_hash_full_payload":
            assert compute_effective_proposal_content_hash(connection, live) == old_hash
        changes = connection.total_changes
        with pytest.raises((ValueError, HumanRevisionLineageError)):
            if posted:
                posting.resume_post(posted.attempt_id)
            else:
                posting.submit_post(review.review_id, "confirm")
        assert changes == connection.total_changes
        if posted:
            with pytest.raises((ValueError, HumanRevisionLineageError)):
                posting.get_status(posted.attempt_id)
            assert _count(connection, "transactions") == 1
        else:
            _assert_no_posting_facts(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "damage", ["old_item", "full_payload", "typed_field_evidence", "amount", "currency"]
)
def test_completion_override_never_excuses_forged_old_proof_or_passed_payload(
    tmp_path, monkeypatch, damage
):
    connection, posting, proposal = _subject(tmp_path, monkeypatch)
    try:
        service = _amendment_service(connection, source_verifier=posting._source_port)
        first = _apply(connection, service, service.prepare(proposal), PATCH)
        result = _apply(
            connection,
            service,
            service.prepare(first.proposal_public_id),
            {"description": "Dinner with Jo"},
            evidence_id="second",
            amendment_id="second",
        )
        if damage == "typed_field_evidence":
            result = _apply(
                connection,
                service,
                service.prepare(result.proposal_public_id),
                {"amount": "15.00"},
                evidence_id="third",
                amendment_id="third",
            )
            result = _apply(
                connection,
                service,
                service.prepare(result.proposal_public_id),
                {"description": "Later Dinner"},
                evidence_id="fourth",
                amendment_id="fourth",
            )
        live = ParserProposalRepository(connection).get_by_public_id(result.proposal_public_id)
        original, _, _ = resolve_effective_payload(connection, live)
        effective = copy.deepcopy(original)
        if damage in {"full_payload", "typed_field_evidence"}:
            if damage == "typed_field_evidence":
                for item in effective["field_evidence"]:
                    if item["field_name"] == "description":
                        assert item["completion_version"] == 1
                        item["completion_version"] = True
                assert effective == original  # Python equality alone misses this typed corruption.
            else:
                effective["field_evidence"][0]["untrusted_claim"] = True
            fake = {**live, "parsed_payload": json.dumps(effective)}
            assert compute_proposal_content_hash(
                connection, fake
            ) == compute_effective_proposal_content_hash(connection, live)
        elif damage == "old_item":
            for item in effective["field_evidence"]:
                if item["field_name"] == "description":
                    item["correction_public_id"] = "forged"
        else:
            effective[damage] = "15.00" if damage == "amount" else "USD"
        changes = connection.total_changes
        with pytest.raises(ConversionEvidenceLineageError):
            _durable_resolution_provenance(connection, live, effective)
        assert connection.total_changes == changes
        _assert_no_posting_facts(connection)
    finally:
        connection.close()


def test_legacy_receipt_completion_cannot_acquire_independent_history_override(tmp_path):
    from finance_core.parser_proposals.completion import complete_proposal
    from finance_core.parser_proposals.receipt_supersession import supersede_receipt_total_proposal

    connection, _, _, _, _, proposal, *_ = _prepare_receipt_subject(tmp_path, "legacy-completion")
    try:
        original = ParserProposalRepository(connection).get_by_public_id(proposal)
        result = supersede_receipt_total_proposal(
            connection,
            original["id"],
            actor="synthetic-human",
            expected_content_hash=compute_effective_proposal_content_hash(connection, original),
            field_updates={"amount": "14.25", "description": "Dinner"},
            correction_public_id="rcor_legacy_material_edit",
        )
        live = ParserProposalRepository(connection).get_by_public_id(
            result["replacement_proposal_public_id"]
        )
        complete_proposal(
            connection,
            live["id"],
            actor="synthetic-human",
            expected_content_hash=compute_effective_proposal_content_hash(connection, live),
            field_updates={"description": "Dinner with Jo"},
            completion_public_id="pco_legacy_completion",
        )
        assert _count(connection, "application_amendment_records") == 0
        effective, _, _ = resolve_effective_payload(connection, live)
        changes = connection.total_changes
        with pytest.raises(ConversionEvidenceLineageError):
            _durable_resolution_provenance(connection, live, effective)
        assert connection.total_changes == changes
        _assert_no_posting_facts(connection)
    finally:
        connection.close()
