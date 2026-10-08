"""Application amendment acceptance against actual sealed AI proposal lineage."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from functools import partial
from pathlib import Path

import openclaw_staging_bridge_support_v1 as bridge_support
import pytest
from application_amendment_support_v1 import (
    DurableSyntheticAmendmentAuthority,
    persist_signed_amendment,
)
from migrated_staging_snapshot_v1 import MigratedStagingTemplate
from test_ai_fallback_service_v1 import (
    _captured_text_proposal,
    _response_body,
    _set_parent_payload_fields,
    canonical_response_sha256,
)
from test_ai_model_compatibility_receipts_v2 import _projection, _register
from test_application_posting_recovery_v1 import (
    BINDING,
    DECISION_KEY,
    NOW,
    SOURCE_KEY,
    _decision_material,
    _digest,
    _DurablePostingDecisionAuthority,
    _DurableSourceVerifier,
    _persist,
    _reply_material,
)

from finance_core.application import admission
from finance_core.application.amendment import AmendmentService
from finance_core.application.amendment_contract import AmendmentBinding, AmendmentError
from finance_core.application.posting import PostingService
from finance_core.application.review import ReviewUnavailableError, get_proposal_review
from finance_core.parser_proposals.ai_fallback import (
    claim_ai_fallback_invocation_v2,
    prepare_ai_fallback_v2,
    record_ai_fallback_result_v2,
    verify_ai_fallback_child,
)
from finance_core.parser_proposals.ai_model_compatibility import (
    compatibility_receipt_for_attempt,
    verify_persisted_compatibility_receipt,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.parser_proposals.service import (
    ParserConfirmationError,
    ProposalConversionError,
    confirm_parser_proposal,
    convert_confirmed_parser_proposal,
)

_EDIT_BINDING = AmendmentBinding(
    BINDING,
    "synthetic-amendment-authority",
    "synthetic-amendment-key-v1",
)
_FINAL_FACT_TABLES = (
    "application_posting_decisions",
    "application_posting_attempts",
    "parser_proposal_authorizations",
    "parser_proposal_conversion_audit",
    "transactions",
    "receipts",
    "receipt_item_allocation_fact_sets",
    "authoritative_calculation_snapshots",
    "receipt_finalization_authorizations",
)


@pytest.fixture(autouse=True)
def _use_snapshot_bridge_workspace(
    monkeypatch: pytest.MonkeyPatch,
    migrated_staging_snapshot_template: MigratedStagingTemplate,
) -> None:
    monkeypatch.setattr(
        bridge_support,
        "create_bridge_workspace",
        partial(
            bridge_support.create_snapshot_bridge_workspace,
            template=migrated_staging_snapshot_template,
        ),
    )


def _create_sealed_ai_root(tmp_path: Path) -> tuple[sqlite3.Connection, str, dict[str, object]]:
    raw_text = "paid SGD 12.34 at Cafe on 2026-08-13"
    _workspace, connection, intake_public_id, parent_id = _captured_text_proposal(
        tmp_path, raw_text
    )
    _set_parent_payload_fields(
        connection,
        parent_id,
        {
            "transaction_date": None,
            "description": None,
            "account": None,
            "category": None,
        },
    )

    receipt = _register(connection)
    prepared = prepare_ai_fallback_v2(
        connection,
        intake_public_id=intake_public_id,
        config_projection=_projection(),
        now_ms=100_000,
    )
    assert prepared["receipt_public_id"] == receipt["receipt_public_id"]
    claim = claim_ai_fallback_invocation_v2(
        connection,
        attempt_public_id=prepared["attempt_public_id"],
        now_ms=100_001,
    )
    assert claim["invocation_disposition"] == "invoke_once"

    body, arguments = _response_body(claim)
    response = json.loads(body)
    date_ref = response["field_evidence_refs"]["amount"][0]
    response["transaction_date"] = "2026-08-13"
    response["field_confidence_bps"]["transaction_date"] = 9000
    response["field_evidence_refs"]["transaction_date"] = [date_ref]
    body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    arguments.update(
        {
            "returned_model": "example-model",
            "returned_agent_id": "finance",
            "audit_purpose": "finance-bridge.ai-proposal-v2",
            "response_utf8_b64": base64.b64encode(body).decode("ascii"),
            "response_byte_count": len(body),
            "response_sha256": canonical_response_sha256(body),
        }
    )
    result, replay = record_ai_fallback_result_v2(
        connection,
        attempt_public_id=prepared["attempt_public_id"],
        transport_outcome="response_received",
        arguments=arguments,
        now_ms=100_002,
    )
    assert replay is False
    assert result["result_status"] == "proposal_created"
    proposal = ParserProposalRepository(connection).get_by_public_id(
        str(result["proposal_public_id"])
    )
    assert proposal is not None
    assert proposal["parser_name"] == "finance_ai_proposal"
    assert proposal["parser_version"] == "finance-ai-proposal-v1"
    ai_payload, _, _ = resolve_effective_payload(connection, proposal)
    assert ai_payload["intent"] == "personal_expense"
    assert ai_payload["transaction_type"] == "personal_expense"
    assert ai_payload["account"] is None
    assert (
        get_proposal_review(connection, str(proposal["public_id"]))["proposal_origin"]
        == "ai_fallback"
    )

    attempt = connection.execute(
        "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id=?",
        (prepared["attempt_public_id"],),
    ).fetchone()
    assert attempt is not None
    assert attempt["intent_policy_version"]
    assert len(attempt["intent_policy_hash"]) == 64
    assert attempt["intent_policy_result"] == "positive"
    receipt_binding = compatibility_receipt_for_attempt(connection, attempt_id=int(attempt["id"]))
    assert receipt_binding is not None
    assert receipt_binding["receipt_public_id"] == prepared["receipt_public_id"]
    compatibility_projection = verify_persisted_compatibility_receipt(receipt_binding)
    assert compatibility_projection["canonical_model"] == "example-model"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM ai_fallback_invocation_claims WHERE attempt_id=?",
            (attempt["id"],),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM ai_fallback_results WHERE attempt_id=?", (attempt["id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM ai_fallback_proposal_links WHERE parser_output_id=?",
            (proposal["id"],),
        ).fetchone()[0]
        == 1
    )
    _assert_ai_root(connection, proposal)
    return connection, intake_public_id, dict(proposal)


def _assert_ai_root(connection: sqlite3.Connection, proposal: dict[str, object]) -> None:
    _payload, _completion_id, version = resolve_effective_payload(connection, proposal)
    proof = verify_ai_fallback_child(
        connection,
        proposal,
        content_hash=compute_effective_proposal_content_hash(connection, proposal),
        proposal_version=version,
        require_resolved=True,
    )
    assert proof is not None
    assert proof["proposal_origin"] == "ai_fallback"
    assert proof["requires_resolution"] is False
    assert proof["ambiguity_flags"] == ()


def _persist_application_source(connection: sqlite3.Connection, intake_public_id: str) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS synthetic_sources "
        "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
    )
    raw = connection.execute(
        "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
        (intake_public_id,),
    ).fetchone()
    assert raw is not None
    raw_hash = hashlib.sha256(str(raw["raw_input"]).encode("utf-8")).hexdigest()
    assert raw["source_content_hash"] == f"sha256:{raw_hash}"
    source: dict[str, object] = {
        "schema": admission.SOURCE_SCHEMA,
        "namespace": BINDING.source_namespace,
        "key_id": BINDING.source_key_id,
        "instance_id": BINDING.instance_id,
        "submission_client_id": BINDING.submission_client_id,
        "evidence_id": "synthetic-ai-amendment-source",
        "intake_public_id": intake_public_id,
        "source_event_id": "synthetic-ai-amendment-source-event",
        "source_content_hash": raw_hash,
        "source_occurred_at": NOW - 2,
        "received_at": NOW - 1,
    }
    source["evidence_digest"] = _digest(source)
    _persist(connection, "synthetic_sources", str(source["evidence_id"]), source, SOURCE_KEY)
    connection.commit()


def _amendment_service(connection: sqlite3.Connection) -> AmendmentService:
    return AmendmentService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: NOW,
    )


def _amend(
    connection: sqlite3.Connection,
    service: AmendmentService,
    proposal_public_id: str,
    patch: dict[str, object],
    *,
    evidence_id: str,
    amendment_id: str,
):
    review = service.prepare(proposal_public_id)
    persist_signed_amendment(
        connection,
        review,
        patch,
        evidence_id=evidence_id,
        amendment_binding=_EDIT_BINDING,
    )
    result = service.amend(review.review_id, evidence_id, amendment_id)
    return review, result


def _verify_current_ai_root(connection: sqlite3.Connection, proposal_public_id: str) -> None:
    proposal = ParserProposalRepository(connection).get_by_public_id(proposal_public_id)
    assert proposal is not None
    _assert_ai_root(connection, dict(proposal))


def _persist_fresh_posting_reply(connection: sqlite3.Connection, review, proposal_id: str) -> str:
    for table in (
        "synthetic_displays",
        "synthetic_decisions",
        "synthetic_replies",
        "synthetic_display_closures",
    ):
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    decision_id = "synthetic-ai-amendment-fresh-posting"
    display, decision = _decision_material(review, proposal_id, decision_id)
    _persist(
        connection,
        "synthetic_displays",
        str(decision["display_id"]),
        display,
        DECISION_KEY,
    )
    _persist(
        connection,
        "synthetic_replies",
        str(decision["reply_id"]),
        _reply_material(decision),
        DECISION_KEY,
    )
    _persist(connection, "synthetic_decisions", decision_id, decision, DECISION_KEY)
    connection.commit()
    return decision_id


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _assert_no_final_facts(connection: sqlite3.Connection) -> None:
    for table in _FINAL_FACT_TABLES:
        assert _count(connection, table) == 0, table


def test_sealed_ai_root_nonmoney_completion_then_fresh_posting_preserves_exact_fields(
    tmp_path: Path,
) -> None:
    connection, intake_public_id, proposal = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(connection, intake_public_id)
        raw_before = connection.execute(
            "SELECT raw_input,source_content_hash,attachment_id FROM raw_intake_records "
            "WHERE public_id=?",
            (intake_public_id,),
        ).fetchone()
        assert raw_before is not None

        service = _amendment_service(connection)
        _review, amendment = _amend(
            connection,
            service,
            str(proposal["public_id"]),
            {"description": "Lunch", "category": "food"},
            evidence_id="ai-completion-evidence",
            amendment_id="ai-completion-amendment",
        )
        assert amendment.publication_kind == "completion"
        assert amendment.proposal_public_id == proposal["public_id"]
        assert amendment.proposal_version == 1
        _verify_current_ai_root(connection, amendment.proposal_public_id)

        raw_after = connection.execute(
            "SELECT raw_input,source_content_hash,attachment_id FROM raw_intake_records "
            "WHERE public_id=?",
            (intake_public_id,),
        ).fetchone()
        assert raw_after is not None
        assert tuple(raw_after) == tuple(raw_before)

        posting = PostingService(
            connection=connection,
            source_verifier=_DurableSourceVerifier(),
            human_decision_authority=_DurablePostingDecisionAuthority(),
            binding=BINDING,
            clock=lambda: NOW,
        )
        fresh = posting.prepare(amendment.proposal_public_id)
        decision_id = _persist_fresh_posting_reply(connection, fresh, amendment.proposal_public_id)
        posted = posting.submit_post(fresh.review_id, decision_id)

        assert posted.state == "finalized"
        assert _count(connection, "transactions") == 1
        assert _count(connection, "application_posting_attempts") == 1
        assert _count(connection, "application_posting_decisions") == 1
        transaction = connection.execute(
            "SELECT public_id,amount,currency,transaction_date,merchant,description,category "
            "FROM transactions WHERE public_id=?",
            (posted.transaction_public_id,),
        ).fetchone()
        assert transaction is not None
        assert tuple(str(transaction[field]) for field in transaction.keys()) == (
            str(posted.transaction_public_id),
            "12.34",
            "SGD",
            "2026-08-13",
            "Cafe",
            "Lunch",
            "food",
        )
    finally:
        connection.close()


def test_sealed_ai_text_child_is_verified_and_legacy_confirmation_and_conversion_refuse(
    tmp_path: Path,
) -> None:
    connection, intake_public_id, proposal = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(connection, intake_public_id)
        service = _amendment_service(connection)
        _review, amendment = _amend(
            connection,
            service,
            str(proposal["public_id"]),
            {"amount": "13.75"},
            evidence_id="ai-child-evidence",
            amendment_id="ai-child-amendment",
        )
        assert amendment.publication_kind == "text_supersession"
        assert amendment.proposal_public_id != proposal["public_id"]
        child_ref = ParserProposalRepository(connection).get_by_public_id(
            amendment.proposal_public_id
        )
        assert child_ref is not None
        child = ParserProposalRepository(connection).get_lineage_row_by_id(int(child_ref["id"]))
        assert child is not None
        assert child["parser_name"] == "application_human_amendment"
        assert child["parser_version"] == "v1"
        assert child["parent_parser_output_id"] == proposal["id"]
        assert child["source_public_id"] == proposal["source_public_id"] == intake_public_id
        assert child["raw_text"] == proposal["raw_text"]
        _assert_ai_root(connection, dict(child))

        before = connection.total_changes
        with pytest.raises(ParserConfirmationError, match="exact accepted posting decision"):
            confirm_parser_proposal(
                connection,
                int(child["id"]),
                authenticated_actor_id=BINDING.human_principal_id,
                clock=lambda: "2026-10-08T00:00:00+00:00",
            )
        with pytest.raises(ProposalConversionError, match="independent decision owner"):
            convert_confirmed_parser_proposal(connection, int(child["id"]))
        assert connection.total_changes == before
        assert _count(connection, "parser_proposal_authorizations") == 0
        assert _count(connection, "parser_proposal_conversion_audit") == 0
        assert _count(connection, "transactions") == 0
        assert _count(connection, "application_posting_attempts") == 0
        assert _count(connection, "application_posting_decisions") == 0
    finally:
        connection.close()


@pytest.mark.parametrize("corruption", ["tampered_result", "missing_link"])
def test_tampered_or_missing_sealed_ai_root_refuses_amendment_without_facts(
    tmp_path: Path,
    corruption: str,
) -> None:
    connection, intake_public_id, proposal = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(connection, intake_public_id)
        if corruption == "tampered_result":
            connection.execute("DROP TRIGGER trg_ai_fallback_results_no_update")
            connection.execute(
                "UPDATE ai_fallback_results SET response_sha256=? WHERE attempt_id="
                "(SELECT id FROM ai_fallback_attempts WHERE raw_intake_record_id="
                "(SELECT id FROM raw_intake_records WHERE public_id=?))",
                ("0" * 64, intake_public_id),
            )
        else:
            connection.execute("DROP TRIGGER trg_ai_fallback_links_no_delete")
            connection.execute(
                "DELETE FROM ai_fallback_proposal_links WHERE parser_output_id=?",
                (proposal["id"],),
            )
        connection.commit()

        with pytest.raises(AmendmentError) as refusal:
            _amendment_service(connection).prepare(str(proposal["public_id"]))
        assert isinstance(refusal.value.__cause__, ReviewUnavailableError)

        _assert_no_final_facts(connection)
        assert _count(connection, "application_amendment_reviews") == 0
        assert _count(connection, "application_amendment_records") == 0
        assert _count(connection, "parser_proposal_completions") == 0
        assert _count(connection, "parser_text_amendment_revisions") == 0
    finally:
        connection.close()


@pytest.mark.parametrize("corruption", ["tampered_result", "missing_link"])
def test_accepted_amendment_status_reverifies_its_original_ai_root(
    tmp_path: Path,
    corruption: str,
) -> None:
    connection, intake_public_id, proposal = _create_sealed_ai_root(tmp_path)
    try:
        _persist_application_source(connection, intake_public_id)
        edits = _amendment_service(connection)
        review = edits.prepare(str(proposal["public_id"]))
        persist_signed_amendment(
            connection,
            review,
            {"description": "Afternoon meal"},
            evidence_id="accepted-ai-root-edit",
            amendment_binding=_EDIT_BINDING,
        )
        applied = edits.amend(review.review_id, "accepted-ai-root-edit", "accepted-ai-edit")
        assert edits.get_status(applied.amendment_id) == applied
        if corruption == "tampered_result":
            connection.execute("DROP TRIGGER trg_ai_fallback_results_no_update")
            connection.execute(
                "UPDATE ai_fallback_results SET response_sha256=? WHERE id="
                "(SELECT result_id FROM ai_fallback_proposal_links WHERE parser_output_id=?)",
                ("0" * 64, proposal["id"]),
            )
        else:
            connection.execute("DROP TRIGGER trg_ai_fallback_links_no_delete")
            connection.execute(
                "DELETE FROM ai_fallback_proposal_links WHERE parser_output_id=?",
                (proposal["id"],),
            )
        connection.commit()

        with pytest.raises(AmendmentError):
            edits.get_status(applied.amendment_id)

        assert _count(connection, "application_amendment_records") == 1
        _assert_no_final_facts(connection)
    finally:
        connection.close()
