"""S1C-B read-only source proofs for synthetic managed receipt lineages."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import human_actions, workspace_access
from finance_core.openclaw_staging_bridge.managed_proposal_source import (
    ManagedSourceUnavailable,
    require_managed_proposal_source,
)
from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.ai_fallback import (
    claim_ai_fallback_invocation,
    prepare_ai_fallback,
    record_ai_fallback_result,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card
from finance_core.parser_proposals.human_revision import publish_human_revision_in_transaction
from finance_core.parser_proposals.receipt_supersession import supersede_receipt_total_proposal
from finance_core.parser_proposals.repository import ParserProposalRepository
from tests import test_receipt_ocr_proposal_ingestion_v1 as receipt_ocr_tests
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests import test_s1c_b_managed_bridge_commands as s1cb
from tests.test_ai_fallback_service_v1 import _replace_response, _response_body
from tests.test_parser_human_revision_v1 import _start_existing_text_proposal
from tests.test_receipt_human_draft_publication_v1 import _card_text

pytest_plugins = ("tests.test_s1c_a_managed_bridge_commands",)


def _require_on_conn(conn: sqlite3.Connection, proposal_public_id: str) -> None:
    proposal = ParserProposalRepository(conn).get_by_public_id(proposal_public_id)
    assert proposal is not None
    require_managed_proposal_source(
        conn,
        context=human_actions.HumanActionContext(
            actor_id=s1ca.ACTOR,
            account_id=s1ca.ACCOUNT,
            conversation_id=s1ca.CONVERSATION,
            binding_id=s1ca.BINDING,
        ),
        proposal=proposal,
        supported_source_types=frozenset({"telegram_text", "telegram_image"}),
    )


def _verify(
    workspace: s1ca.ManagedBridgeWorkspace,
    proposal_public_id: str,
) -> None:
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s1cb-source-verify"
    ) as conn:
        _require_on_conn(conn, proposal_public_id)
        assert not conn.in_transaction


def _disable_trigger_for(conn: sqlite3.Connection, *, table: str, operation: str) -> None:
    """Allow a single synthetic corruption without changing migration files."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    for row in rows:
        if operation.upper() in str(row["sql"]).upper():
            conn.execute(f'DROP TRIGGER "{row["name"]}"')


def _supersede_receipt(
    conn: sqlite3.Connection, parent_public_id: str, correction_public_id: str
) -> dict[str, object]:
    parent = ParserProposalRepository(conn).get_by_public_id(parent_public_id)
    assert parent is not None
    result = supersede_receipt_total_proposal(
        conn,
        int(parent["id"]),
        actor=s1ca.ACTOR,
        expected_content_hash=compute_effective_proposal_content_hash(conn, parent),
        field_updates={"amount": "13.00"},
        correction_public_id=correction_public_id,
        correction_channel="cli",
    )
    child = ParserProposalRepository(conn).get(int(result["replacement_parser_output_id"]))
    assert child is not None
    return child


def test_initial_ocr_receipt_source_is_read_only(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    proposal_id, _version, _content_hash = s1cb._seed_managed_receipt_proposal(managed_workspace)
    _verify(managed_workspace, proposal_id)


def test_public_receipt_supersession_edge_and_old_target_replay_are_source_bound(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    parent_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(managed_workspace)
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-public-revision"
    ) as conn:
        child = _supersede_receipt(conn, parent_public_id, "rcor_s1cb_source_public")
        child_public_id = str(child["public_id"])
    _verify(managed_workspace, child_public_id)
    _verify(managed_workspace, parent_public_id)


def test_sealed_d1_receipt_child_source_is_verified(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(
        managed_workspace, confirm=False
    )
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-d1-receipt"
    ) as conn:
        parent = ParserProposalRepository(conn).get_by_public_id(parent_public_id)
        assert parent is not None
        started = _start_existing_text_proposal(conn, int(parent["id"]), suffix="s1cb-receipt-d1")
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    fields = {**started.field_values, "merchant": "Human Cafe"}
    card_text = _card_text(started.card_generation_public_id, fields)
    captured, _ = s1ca._capture_route(managed_workspace, card_text, message_id=101)
    assert captured.exit_code == 0, captured.response
    route = captured.response["result"]["interaction_route"]
    assert route["route_kind"] == "whole_card"
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-d1-publish"
    ) as conn:
        result = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                101,
                str(route["operation_key"]),
                s1ca.ACTOR,
                s1ca.ACCOUNT,
                s1ca.CONVERSATION,
                s1ca.BINDING,
                card_text,
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        child_public_id = str(result.proposal_public_id)
    _verify(managed_workspace, child_public_id)
    _verify(managed_workspace, parent_public_id)


def test_sealed_ai_receipt_child_source_is_verified_without_provider_call(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_specs = (
        ("PAID", 0, 20),
        ("TOTAL", 3, 140),
        ("S$", 3, 140),
        ("12.34", 3, 140),
        ("2026-08-13", 1, 60),
        ("CAFE", 0, 20),
    )
    monkeypatch.setattr(
        receipt_ocr_tests,
        "_sgd_blocks",
        lambda: tuple(
            replace(support._block(index, text), engine_line_index=line, top=top)
            for index, (text, line, top) in enumerate(block_specs)
        ),
    )
    parent_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(
        managed_workspace, confirm=False
    )
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-ai-receipt"
    ) as conn:
        parent = ParserProposalRepository(conn).get_by_public_id(parent_public_id)
        assert parent is not None
        attempt = prepare_ai_fallback(
            conn, intake_public_id=str(parent["source_public_id"]), now_ms=100_000
        )
        claim = claim_ai_fallback_invocation(
            conn, attempt_public_id=str(attempt["attempt_public_id"]), now_ms=100_001
        )
        body, arguments = _response_body(claim)
        response = json.loads(body)
        _body, receipt_arguments = _replace_response(
            body,
            arguments,
            amount="12.34",
            currency="SGD",
            transaction_date=None,
            merchant="CAFE",
            field_confidence_bps={
                **response["field_confidence_bps"],
                "amount": 9000,
                "currency": 9000,
                "transaction_date": None,
                "merchant": 9000,
            },
            field_evidence_refs={
                **response["field_evidence_refs"],
                "amount": ["e0002", "e0003", "e0004"],
                "currency": ["e0002", "e0003", "e0004"],
                "transaction_date": [],
                "merchant": ["e0006"],
            },
        )
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=str(attempt["attempt_public_id"]),
            transport_outcome="response_received",
            arguments=receipt_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created", result["non_child_reason"]
        child_public_id = str(result["proposal_public_id"])
    _verify(managed_workspace, child_public_id)
    _verify(managed_workspace, parent_public_id)


def test_root_ocr_relational_row_drift_is_refused(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    proposal_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(managed_workspace)
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-ocr-drift"
    ) as conn:
        proposal = ParserProposalRepository(conn).get_by_public_id(proposal_public_id)
        assert proposal is not None
        row = conn.execute(
            "SELECT id, evidence_reference FROM parser_proposal_field_evidence "
            "WHERE parser_output_id = ? AND evidence_source_type = 'ocr' ORDER BY id LIMIT 1",
            (proposal["id"],),
        ).fetchone()
        assert row is not None
        _disable_trigger_for(conn, table="parser_proposal_field_evidence", operation="UPDATE")
        reference = json.loads(str(row["evidence_reference"]))
        reference["normalized_result_hash"] = "0" * 64
        conn.execute(
            "UPDATE parser_proposal_field_evidence SET evidence_reference = ? WHERE id = ?",
            (json.dumps(reference, sort_keys=True, separators=(",", ":")), row["id"]),
        )
        conn.commit()
        with pytest.raises(ManagedSourceUnavailable):
            _require_on_conn(conn, proposal_public_id)


def test_image_attachment_hash_drift_is_refused(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    proposal_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(managed_workspace)
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-attachment-drift"
    ) as conn:
        proposal = ParserProposalRepository(conn).get_by_public_id(proposal_public_id)
        assert proposal is not None
        _disable_trigger_for(conn, table="raw_intake_records", operation="UPDATE")
        conn.execute(
            "UPDATE raw_intake_records SET attachment_hash = ? WHERE public_id = ?",
            ("0" * 64, proposal["source_public_id"]),
        )
        conn.commit()
        with pytest.raises(ManagedSourceUnavailable):
            _require_on_conn(conn, proposal_public_id)


def test_unsealed_receipt_parent_pointer_is_refused(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    parent_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(managed_workspace)
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-unsealed-parent"
    ) as conn:
        child = _supersede_receipt(conn, parent_public_id, "rcor_s1cb_unsealed")
        child_public_id = str(child["public_id"])
        _require_on_conn(conn, child_public_id)
        _disable_trigger_for(conn, table="receipt_proposal_revisions", operation="DELETE")
        conn.execute(
            "DELETE FROM receipt_proposal_revisions WHERE replacement_parser_output_id = ?",
            (child["id"],),
        )
        conn.commit()
        with pytest.raises(ManagedSourceUnavailable):
            _require_on_conn(conn, child_public_id)


def test_inherited_receipt_ocr_payload_drift_is_refused(
    managed_workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    parent_public_id, _version, _hash = s1cb._seed_managed_receipt_proposal(managed_workspace)
    with workspace_access.workspace_database_session(
        managed_workspace.workspace_path, operation_id="test-s1cb-inherited-ocr-drift"
    ) as conn:
        child = _supersede_receipt(conn, parent_public_id, "rcor_s1cb_inherited_drift")
        child_public_id = str(child["public_id"])
        _require_on_conn(conn, child_public_id)
        payload = json.loads(str(child["parsed_payload"]))
        assert isinstance(payload["ocr_evidence"], dict)
        payload["ocr_evidence"]["normalized_result_hash"] = "0" * 64
        _disable_trigger_for(conn, table="parser_outputs", operation="UPDATE")
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), child["id"]),
        )
        conn.commit()
        with pytest.raises(ManagedSourceUnavailable):
            _require_on_conn(conn, child_public_id)
