"""S5e-B service boundary tests using temporary staging databases only."""

from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest
from migrated_staging_snapshot_v1 import MigratedStagingTemplate

import finance_core.parser_proposals.ai_fallback as ai_fallback_module
import finance_core.parser_proposals.service as parser_service
from finance_core.intake.receipt_ocr_evidence import ReceiptOcrLimits
from finance_core.openclaw_staging_bridge import commands as bridge_commands
from finance_core.openclaw_staging_bridge import errors as bridge_errors
from finance_core.openclaw_staging_bridge import ocr_boundary
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    _intent_result,
    _parse_ocr_evidence_reference,
    _selected_money_pair,
    claim_ai_fallback_invocation,
    prepare_ai_fallback,
    record_ai_fallback_result,
    record_ai_fallback_result_with_disposition,
    requires_deterministic_intent_policy,
    verify_ai_fallback_child,
    verify_deterministic_intent_policy,
)
from finance_core.parser_proposals.ai_fallback_provenance import canonical_response_sha256
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.numeric_mirror import (
    decimal_from_numeric_mirror,
    sqlite_numeric_roundtrip_matches,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.parser_proposals.service import (
    ProposalConversionError,
    confirm_parser_proposal,
    convert_confirmed_parser_proposal,
)


@pytest.fixture(autouse=True)
def _use_snapshot_bridge_workspace(
    monkeypatch: pytest.MonkeyPatch,
    migrated_staging_snapshot_template: MigratedStagingTemplate,
) -> None:
    monkeypatch.setattr(
        support,
        "create_bridge_workspace",
        partial(
            support.create_snapshot_bridge_workspace,
            template=migrated_staging_snapshot_template,
        ),
    )


def _captured_text_proposal(
    tmp_path: Path,
    text: str = "paid SGD 12.34 at Cafe",
) -> tuple[object, sqlite3.Connection, str, int]:
    workspace = support.create_bridge_workspace(tmp_path)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace,
                support.telegram_text_update(text),
            ),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    intake_public_id = capture.response["result"]["intake_public_id"]
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
    assert proposed.exit_code == bridge_errors.EXIT_OK
    conn = support.open_database(workspace)
    parent = conn.execute("SELECT id FROM parser_outputs ORDER BY id LIMIT 1").fetchone()
    assert parent is not None
    return workspace, conn, intake_public_id, int(parent["id"])


def _set_parent_payload_fields(
    conn: sqlite3.Connection,
    parent_id: int,
    updates: dict[str, object],
) -> None:
    parent = conn.execute(
        "SELECT parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert parent is not None
    for column in ("parsed_payload", "normalized_payload"):
        payload = json.loads(parent[column])
        payload.update(updates)
        conn.execute(
            f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), parent_id),
        )
    conn.commit()


def _confirmed_text_proposal(
    tmp_path: Path,
    *,
    amount: str = "12.34",
) -> tuple[object, sqlite3.Connection, int]:
    workspace, conn, _intake_public_id, parent_id = _captured_text_proposal(
        tmp_path,
        "paid SGD 12.34 at Cafe on 2026-08-13",
    )
    _set_parent_payload_fields(
        conn,
        parent_id,
        {
            "description": None,
            "account": None,
            "category": None,
            "amount": amount,
            "transaction_date": "2026-08-13",
        },
    )
    content_hash = compute_effective_proposal_content_hash(
        conn,
        {"id": parent_id},
    )
    proposal = ParserProposalRepository(conn).get(parent_id)
    assert proposal is not None
    _payload, _completion_id, version = resolve_effective_payload(conn, proposal)
    confirmed = confirm_parser_proposal(
        conn,
        parent_id,
        authenticated_actor_id="person_owner",
        expected_content_hash=content_hash,
        expected_version=version,
        clock=lambda: "2026-08-20T00:00:00+00:00",
    )
    assert confirmed["to_status"] == "confirmed"
    return workspace, conn, parent_id


def _fallback_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "ai_fallback_attempts",
        "ai_fallback_invocation_claims",
        "ai_fallback_results",
        "ai_fallback_proposal_links",
    )
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables
    }


def _prepared_attempt(
    tmp_path: Path,
    text: str = "paid SGD 12.34 at Cafe",
) -> tuple[object, object, object]:
    workspace, conn, _intake_public_id, parent_id = _captured_text_proposal(tmp_path, text)
    # The deterministic text parser may surface a lossy description fragment
    # (for example, a numeric date as ``on --``).  S5e intentionally refuses
    # such parent destination fields; this helper models the otherwise-valid
    # pending parent used by the fallback service tests.  Dedicated tests
    # below exercise the refusal against each non-empty field explicitly.
    parent = conn.execute(
        "SELECT id, parsed_payload, normalized_payload FROM parser_outputs WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert parent is not None
    for column in ("parsed_payload", "normalized_payload"):
        payload = json.loads(parent[column])
        payload["description"] = None
        payload["account"] = None
        payload["category"] = None
        conn.execute(
            f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), parent["id"]),
        )
    conn.commit()
    attempt = prepare_ai_fallback(conn, intake_public_id=_intake_public_id, now_ms=100_000)
    return workspace, conn, attempt


def _prepared_claim(
    tmp_path: Path,
    text: str = "paid SGD 12.34 at Cafe",
) -> tuple[object, object, object, str]:
    workspace, conn, attempt = _prepared_attempt(tmp_path, text)
    claim = claim_ai_fallback_invocation(
        conn,
        attempt_public_id=attempt["attempt_public_id"],
        now_ms=100_001,
    )
    return workspace, conn, attempt, claim


def _captured_ocr_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    date_text: str = "2026-08-13",
    page_width: int = 800,
    extra_block_texts: tuple[str, ...] = (),
    extra_block_specs: tuple[tuple[str, int, int], ...] = (),
) -> tuple[object, sqlite3.Connection, str, int]:
    workspace = support.create_bridge_workspace(tmp_path)
    support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg"),
            idempotency_key=support.canonical_capture_key(message_id=20),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    block_specs = (
        (
            ("PAID", 0, 20),
            ("TOTAL", 3, 140),
            ("S$", 3, 140),
            ("12.34", 3, 140),
            (date_text, 1, 60),
            ("CAFE", 0, 20),
        )
        + tuple((text, 0, 20) for text in extra_block_texts)
        + extra_block_specs
    )
    blocks = tuple(
        replace(
            support._block(index, text),
            engine_line_index=line,
            top=top,
            page_width=page_width,
        )
        for index, (text, line, top) in enumerate(block_specs)
    )
    if page_width != 800:
        monkeypatch.setattr(
            bridge_commands,
            "extract_and_persist_receipt_ocr_evidence",
            partial(
                bridge_commands.extract_and_persist_receipt_ocr_evidence,
                limits=ReceiptOcrLimits(max_image_width=page_width),
            ),
        )
    monkeypatch.setattr(
        ocr_boundary,
        "engine_factory",
        lambda _workspace: support.FakeOcrEngine(blocks=blocks),
    )
    intake_public_id = capture.response["result"]["intake_public_id"]
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
    assert proposed.exit_code == bridge_errors.EXIT_OK
    conn = support.open_database(workspace)
    parent = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
        (intake_public_id,),
    ).fetchone()
    assert parent is not None
    return workspace, conn, intake_public_id, int(parent["parser_output_id"])


def _prepared_ocr_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, object, object, dict[str, object]]:
    workspace, conn, intake_public_id, _parent_id = _captured_ocr_proposal(
        tmp_path,
        monkeypatch,
    )
    attempt = prepare_ai_fallback(
        conn,
        intake_public_id=intake_public_id,
        now_ms=100_000,
    )
    claim = claim_ai_fallback_invocation(
        conn,
        attempt_public_id=attempt["attempt_public_id"],
        now_ms=100_001,
    )
    return workspace, conn, attempt, claim


def _response_body(claim: dict[str, object]) -> tuple[bytes, dict[str, object]]:
    projection = json.loads(
        claim["model_call"]["messages"][0]["content"]  # type: ignore[index]
    )
    ref = projection["evidence_catalog"][0]["ref"]
    response = {
        "schema_version": "finance-ai-facts-v2",
        "intent_type": "personal_expense",
        "amount": "12.34",
        "currency": "SGD",
        "transaction_date": None,
        "merchant": "Cafe",
        "description": None,
        "account": None,
        "category": None,
        "field_confidence_bps": {
            "amount": 9000,
            "currency": 9000,
            "transaction_date": None,
            "merchant": 9000,
            "description": None,
            "account": None,
            "category": None,
        },
        "field_conflicts": {
            field: [] for field in ("amount", "currency", "transaction_date", "merchant")
        },
        "field_evidence_refs": {
            "amount": [ref],
            "currency": [ref],
            "transaction_date": [],
            "merchant": [ref],
            "description": [],
            "account": [],
            "category": [],
        },
    }
    body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return body, {
        "returned_provider": "openai",
        "returned_model": "gpt-5.6-luna",
        "returned_agent_id": "finance-bridge-staging",
        "audit_caller_kind": "plugin",
        "audit_caller_id": "finance-bridge",
        "audit_caller_name": None,
        "audit_purpose": "finance-bridge.ai-proposal-v1",
        "audit_session_key_sha256": None,
        "usage_input_tokens": 1,
        "usage_output_tokens": 1,
        "response_utf8_b64": base64.b64encode(body).decode("ascii"),
        "response_byte_count": len(body),
        "response_sha256": canonical_response_sha256(body),
    }


def _replace_response(
    body: bytes,
    arguments: dict[str, object],
    **updates: object,
) -> tuple[bytes, dict[str, object]]:
    response = json.loads(body)
    response.update(updates)
    replacement = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return replacement, {
        **arguments,
        "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
        "response_byte_count": len(replacement),
        "response_sha256": canonical_response_sha256(replacement),
    }


def _ocr_response_with_inherited_date(
    body: bytes,
    arguments: dict[str, object],
    *,
    ambiguity_flags: list[str],
) -> tuple[bytes, dict[str, object]]:
    response = json.loads(body)
    all_refs = [f"e{index:04d}" for index in range(2, 5)]
    return _replace_response(
        body,
        arguments,
        amount="12.34",
        currency="SGD",
        transaction_date=None,
        merchant="CAFE",
        **({"ambiguity_flags": ambiguity_flags} if ambiguity_flags else {}),
        field_confidence_bps={
            **response["field_confidence_bps"],
            "amount": 9000,
            "currency": 9000,
            "transaction_date": None,
            "merchant": 9000,
        },
        field_evidence_refs={
            **response["field_evidence_refs"],
            "amount": all_refs,
            "currency": all_refs,
            "transaction_date": [],
            "merchant": ["e0006"],
        },
    )


def test_successful_child_repoints_intake_and_replays_exactly(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        assert result["proposal_version"] == 0

        proposals = conn.execute(
            "SELECT id, public_id, parse_status, parent_parser_output_id "
            "FROM parser_outputs ORDER BY id"
        ).fetchall()
        assert proposals[0]["parse_status"] == "superseded"
        assert proposals[1]["public_id"] == result["proposal_public_id"]
        child_payload = json.loads(
            conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE id = ?",
                (proposals[1]["id"],),
            ).fetchone()[0]
        )
        assert child_payload["ambiguity_flags"] == ["missing_date"]
        assert (
            conn.execute("SELECT parser_output_id FROM raw_intake_records").fetchone()[0]
            == proposals[1]["id"]
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parser_proposal_events WHERE actor_type = 'ai'"
            ).fetchone()[0]
            == 2
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result["proposal_public_id"],)
        ).fetchone()
        assert child is not None
        _payload, _completion_id, version = resolve_effective_payload(conn, child)
        lineage = verify_ai_fallback_child(
            conn,
            child,
            content_hash=compute_effective_proposal_content_hash(conn, child),
            proposal_version=version,
            require_resolved=False,
        )
        assert lineage is not None
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM financial_audit_events
                WHERE aggregate_type = 'parser_proposal'
                  AND aggregate_public_id = ?
                  AND event_type = 'parser_proposal_ai_fallback_child_created'
                """,
                (result["proposal_public_id"],),
            ).fetchone()[0]
            == 1
        )
        audit_event = conn.execute(
            """
            SELECT source_evidence_refs_json
            FROM financial_audit_events
            WHERE aggregate_type = 'parser_proposal'
              AND aggregate_public_id = ?
              AND event_type = 'parser_proposal_ai_fallback_child_created'
            """,
            (result["proposal_public_id"],),
        ).fetchone()
        assert audit_event is not None
        references = json.loads(audit_event["source_evidence_refs_json"])
        assert f"attempt:{attempt['attempt_public_id']}" in references
        assert any(reference.startswith("raw-intake:") for reference in references)
        assert f"result:{result['result_public_id']}" in references
        assert f"proposal:{proposals[0]['public_id']}" in references
        assert f"proposal:{result['proposal_public_id']}" in references
        assert any(reference.startswith("proposal-link:") for reference in references)
        assert any(reference.startswith("field-evidence:") for reference in references)
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_003,
            )
            == result
        )
        assert body
    finally:
        conn.close()


def test_ai_filled_field_has_no_false_parent_inheritance_evidence(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        "paid SGD 12.34 at Cafe on 2026-08-13",
    )
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        ref = response["field_evidence_refs"]["amount"][0]
        response["transaction_date"] = "2026-08-13"
        response["field_confidence_bps"]["transaction_date"] = 9000
        response["field_evidence_refs"]["transaction_date"] = [ref]
        replacement = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode()
        changed_arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
            "response_byte_count": len(replacement),
            "response_sha256": canonical_response_sha256(replacement),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        rows = conn.execute(
            """
            SELECT evidence_source_type
            FROM parser_proposal_field_evidence
            WHERE parser_output_id = (
                SELECT id FROM parser_outputs WHERE public_id = ?
            ) AND field_name = 'transaction_date'
            ORDER BY evidence_source_type
            """,
            (result["proposal_public_id"],),
        ).fetchall()
        assert [row["evidence_source_type"] for row in rows] == ["ai_model"]
    finally:
        conn.close()


def test_ai_child_human_decision_conversion_and_result_replay(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        "paid SGD 12.34 at Cafe on 2026-08-13",
    )
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        date_ref = response["field_evidence_refs"]["amount"][0]
        response["transaction_date"] = "2026-08-13"
        response["field_confidence_bps"]["transaction_date"] = 9000
        response["field_evidence_refs"]["transaction_date"] = [date_ref]
        body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(body).decode("ascii"),
            "response_byte_count": len(body),
            "response_sha256": canonical_response_sha256(body),
        }
        first = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (first["proposal_public_id"],),
        ).fetchone()
        assert child is not None
        projection = ParserProposalRepository(conn).get(int(child["id"]))
        assert projection is not None
        content_hash = compute_effective_proposal_content_hash(conn, child)
        _payload, _completion_id, version = resolve_effective_payload(conn, child)
        assert verify_ai_fallback_child(
            conn,
            projection,
            content_hash=content_hash,
            proposal_version=version,
            require_resolved=True,
        ) == {
            "proposal_origin": "ai_fallback",
            "ai_source_kind": "telegram_raw_text",
            "ambiguity_flags": (),
            "requires_resolution": False,
        }
        confirmed = confirm_parser_proposal(
            conn,
            int(child["id"]),
            authenticated_actor_id="telegram-user-111",
            expected_content_hash=content_hash,
            expected_version=version,
            clock=lambda: "2026-08-20T00:00:00+00:00",
        )
        assert confirmed["to_status"] == "confirmed"
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_003,
            )
            == first
        )
        converted = convert_confirmed_parser_proposal(conn, int(child["id"]))
        assert converted["final_transaction_created"] is True
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_004,
            )
            == first
        )
        audit_types = conn.execute(
            """
            SELECT event_type FROM financial_audit_events
            WHERE aggregate_type = 'parser_proposal' AND aggregate_public_id = ?
            ORDER BY sequence_number
            """,
            (child["public_id"],),
        ).fetchall()
        assert [row["event_type"] for row in audit_types] == [
            "parser_proposal_ai_fallback_child_created",
            "parser_proposal_confirmed",
            "parser_proposal_converted",
        ]
        audit_states = conn.execute(
            """
            SELECT event_type, previous_state_json, new_state_json
            FROM financial_audit_events
            WHERE aggregate_type = 'parser_proposal' AND aggregate_public_id = ?
            ORDER BY sequence_number
            """,
            (child["public_id"],),
        ).fetchall()
        assert json.loads(audit_states[0]["new_state_json"])["value"] == {
            "parse_status": "parsed_pending_confirmation",
            "raw_intake_status": "parsed_pending_confirmation",
            "proposal_content_hash": content_hash,
            "conversion_status": "not_converted",
        }
        assert (
            json.loads(audit_states[1]["previous_state_json"])["value"]
            == json.loads(audit_states[0]["new_state_json"])["value"]
        )
        assert (
            json.loads(audit_states[2]["previous_state_json"])["value"]["conversion_status"]
            == "not_converted"
        )
    finally:
        conn.close()


def test_ai_child_reject_uses_repository_projection_without_conversion(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result["proposal_public_id"],),
        ).fetchone()
        assert child is not None
        projection = ParserProposalRepository(conn).get(int(child["id"]))
        assert projection is not None
        content_hash = compute_effective_proposal_content_hash(conn, projection)
        version = resolve_effective_payload(conn, projection)[2]
        assert verify_ai_fallback_child(
            conn,
            projection,
            content_hash=content_hash,
            proposal_version=version,
            require_resolved=False,
        ) == {
            "proposal_origin": "ai_fallback",
            "ai_source_kind": "telegram_raw_text",
            "ambiguity_flags": ("missing_date",),
            "requires_resolution": True,
        }
        rejected = confirm_parser_proposal(
            conn,
            int(child["id"]),
            authenticated_actor_id="telegram-user-111",
            decision="rejected",
            expected_content_hash=content_hash,
            expected_version=version,
            clock=lambda: "2026-08-20T00:00:00+00:00",
        )
        assert rejected["to_status"] == "rejected"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE parser_output_id = ?",
                (child["id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                """
            SELECT event_type FROM financial_audit_events
            WHERE aggregate_type = 'parser_proposal' AND aggregate_public_id = ?
            ORDER BY sequence_number
            """,
                (child["public_id"],),
            ).fetchall()[-1]["event_type"]
            == "parser_proposal_rejected"
        )
    finally:
        conn.close()


def test_claim_rechecks_current_parent_state_before_model_invocation(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace,
                support.telegram_text_update("paid SGD 12.34 at Cafe"),
            ),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    intake_public_id = capture.response["result"]["intake_public_id"]
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
    assert proposed.exit_code == bridge_errors.EXIT_OK
    conn = support.open_database(workspace)
    try:
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        conn.execute(
            "UPDATE parser_outputs SET parse_status = 'confirmed' WHERE parse_status = ?",
            ("parsed_pending_confirmation",),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="parent lifecycle"):
            claim_ai_fallback_invocation(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                now_ms=100_001,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_invocation_claims").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("prepare fault"), sqlite3.OperationalError("prepare sqlite fault")],
    ids=["ordinary", "sqlite"],
)
def test_prepare_rolls_back_after_fault_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace,
                support.telegram_text_update("paid SGD 12.34 at Cafe"),
            ),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    assert capture.exit_code == bridge_errors.EXIT_OK
    intake_public_id = capture.response["result"]["intake_public_id"]
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
    assert proposed.exit_code == bridge_errors.EXIT_OK
    conn = support.open_database(workspace)
    try:

        def fail_source_material(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(
            ai_fallback_module,
            "_verify_attempt_source_material",
            fail_source_material,
        )
        with pytest.raises((RuntimeError, AiFallbackServiceError)):
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("claim fault"), sqlite3.OperationalError("claim sqlite fault")],
    ids=["ordinary", "sqlite"],
)
def test_claim_rolls_back_after_fault_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    _workspace, conn, attempt = _prepared_attempt(tmp_path)
    try:

        def fail_parent_state(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(
            ai_fallback_module,
            "_verify_attempt_current_parent_state",
            fail_parent_state,
        )
        with pytest.raises((RuntimeError, AiFallbackServiceError)):
            claim_ai_fallback_invocation(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                now_ms=100_001,
            )
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_invocation_claims").fetchone()[0] == 0
    finally:
        conn.close()


def test_result_refuses_claim_material_tamper_before_persisting_result(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)
        durable_attempt = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()
        assert durable_attempt is not None
        durable_claim = conn.execute(
            "SELECT * FROM ai_fallback_invocation_claims WHERE attempt_id = ?",
            (durable_attempt["id"],),
        ).fetchone()
        assert durable_claim is not None
        conn.execute("DROP TRIGGER trg_ai_fallback_claims_no_update")
        conn.execute(
            "UPDATE ai_fallback_invocation_claims SET call_start_not_after_ms = ? WHERE id = ?",
            (durable_claim["call_start_not_after_ms"] + 1, durable_claim["id"]),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="claim hash"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_002,
            )
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 0
    finally:
        conn.close()


def test_result_refuses_model_override_of_deterministic_money(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        _replacement, changed_arguments = _replace_response(
            body,
            arguments,
            amount="99.99",
        )
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_result_refuses_value_not_supported_by_field_evidence(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response["transaction_date"] = "2026-01-01"
        response["field_confidence_bps"]["transaction_date"] = 9000
        response["field_evidence_refs"]["transaction_date"] = response["field_evidence_refs"][
            "amount"
        ]
        replacement = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        changed_arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
            "response_byte_count": len(replacement),
            "response_sha256": canonical_response_sha256(replacement),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
    finally:
        conn.close()


def test_result_refuses_missing_date_flag_when_date_is_present(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        "paid SGD 12.34 at Cafe on 2026-08-13",
    )
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        date_ref = response["field_evidence_refs"]["amount"][0]
        response["transaction_date"] = "2026-08-13"
        response["field_confidence_bps"]["transaction_date"] = 9000
        response["field_evidence_refs"]["transaction_date"] = [date_ref]
        response["ambiguity_flags"] = ["missing_date"]
        replacement = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode()
        changed_arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
            "response_byte_count": len(replacement),
            "response_sha256": canonical_response_sha256(replacement),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_result_refuses_missing_date_omission_without_source_date(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        replacement, changed_arguments = _replace_response(
            body,
            arguments,
            ambiguity_flags=[],
        )
        assert replacement
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_result_refuses_unsorted_ambiguity_flags(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        replacement, changed_arguments = _replace_response(
            body,
            arguments,
            ambiguity_flags=["missing_date", "ambiguous_amount"],
        )
        assert replacement
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_result_accepts_omitted_model_date_when_parent_date_is_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, conn, attempt, claim = _prepared_ocr_claim(tmp_path, monkeypatch)
    try:
        body, arguments = _response_body(claim)
        replacement, changed_arguments = _ocr_response_with_inherited_date(
            body,
            arguments,
            ambiguity_flags=[],
        )
        assert replacement
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child_payload = json.loads(
            conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
                (result["proposal_public_id"],),
            ).fetchone()[0]
        )
        assert child_payload["transaction_date"] == "2026-08-13"
        assert "missing_date" not in child_payload["ambiguity_flags"]
    finally:
        conn.close()


def test_result_refuses_missing_date_when_parent_date_is_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, conn, attempt, claim = _prepared_ocr_claim(tmp_path, monkeypatch)
    try:
        body, arguments = _response_body(claim)
        replacement, changed_arguments = _ocr_response_with_inherited_date(
            body,
            arguments,
            ambiguity_flags=["missing_date"],
        )
        assert replacement
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_result_refuses_cross_paired_money_candidates(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        "paid USD 10.00 and SGD 20.00 at Cafe",
    )
    try:
        body, arguments = _response_body(claim)
        replacement, _ = _replace_response(
            body,
            arguments,
            amount="10.00",
            currency="SGD",
        )
        changed_arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
            "response_byte_count": len(replacement),
            "response_sha256": canonical_response_sha256(replacement),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
    finally:
        conn.close()


def test_multiple_text_money_candidates_remain_unresolved(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        "paid SGD 12.34 and SGD 10 at Cafe",
    )
    try:
        reasons = json.loads(
            conn.execute(
                "SELECT eligibility_reasons_json FROM ai_fallback_attempts WHERE id = ?",
                (
                    conn.execute(
                        "SELECT id FROM ai_fallback_attempts WHERE attempt_public_id = ?",
                        (attempt["attempt_public_id"],),
                    ).fetchone()[0],
                ),
            ).fetchone()[0]
        )
        assert "conflicting_text_candidates" in reasons

        body, arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result["proposal_public_id"],),
        ).fetchone()
        assert child is not None
        payload = json.loads(child["parsed_payload"])
        assert payload["ambiguity_flags"] == [
            "ambiguous_amount",
            "missing_date",
            "source_conflict",
        ]

        projection = ParserProposalRepository(conn).get(int(child["id"]))
        assert projection is not None
        content_hash = compute_effective_proposal_content_hash(conn, projection)
        version = resolve_effective_payload(conn, projection)[2]
        with pytest.raises(AiFallbackServiceError, match="ambiguity remains unresolved"):
            verify_ai_fallback_child(
                conn,
                projection,
                content_hash=content_hash,
                proposal_version=version,
                require_resolved=True,
            )
    finally:
        conn.close()


def test_result_accepts_deterministic_currency_alias_pair(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path, "paid $12.34 at Cafe")
    try:
        body, arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        assert result["proposal_public_id"] is not None
    finally:
        conn.close()


def test_result_refuses_currency_alias_outside_money_contract(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path, "paid RM 12.34 at Cafe")
    try:
        body, arguments = _response_body(claim)
        replacement, _ = _replace_response(body, arguments, currency="MYR")
        changed_arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
            "response_byte_count": len(replacement),
            "response_sha256": canonical_response_sha256(replacement),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
    finally:
        conn.close()


def test_result_rechecks_global_raw_intake_pointer_cardinality(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        conn.execute("DROP TRIGGER trg_ai_fallback_raw_intake_no_insert_pointer_collision")
        parent_id = int(
            conn.execute(
                """
                SELECT parent_parser_output_id
                FROM ai_fallback_attempts
                WHERE attempt_public_id = ?
                """,
                (attempt["attempt_public_id"],),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                status, parser_output_id, idempotency_key
            ) VALUES (
                'raw_duplicate_pointer_s5e', 'telegram_text', 'telegram',
                'paid SGD 12.34 at Cafe', '2026-01-01T00:00:00Z',
                'parsed_pending_confirmation', ?, 'duplicate-pointer-s5e'
            )
            """,
            (parent_id,),
        )
        conn.commit()
        body, arguments = _response_body(claim)
        with pytest.raises(AiFallbackServiceError, match="exactly one raw-intake binding"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_002,
            )
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM raw_intake_records WHERE parser_output_id = ?",
                (parent_id,),
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_claim_rechecks_global_raw_intake_pointer_cardinality(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace,
                support.telegram_text_update("paid SGD 12.34 at Cafe"),
            ),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    intake_public_id = capture.response["result"]["intake_public_id"]
    support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
            },
            idempotency_key=support.canonical_propose_key(intake_public_id),
        )
    )
    conn = support.open_database(workspace)
    try:
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        conn.execute("DROP TRIGGER trg_ai_fallback_raw_intake_no_insert_pointer_collision")
        parent_id = conn.execute(
            "SELECT parent_parser_output_id FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                status, parser_output_id, idempotency_key
            ) VALUES (
                'raw_duplicate_claim_s5e', 'telegram_text', 'telegram',
                'paid SGD 12.34 at Cafe', '2026-01-01T00:00:00Z',
                'parsed_pending_confirmation', ?, 'duplicate-claim-s5e'
            )
            """,
            (parent_id,),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="exactly one raw-intake binding"):
            claim_ai_fallback_invocation(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                now_ms=100_001,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_invocation_claims").fetchone()[0] == 0
    finally:
        conn.close()


def test_ocr_money_pair_reuses_receipt_source_specific_currency_alias() -> None:
    parent_payload = {
        "amount": "12.34",
        "currency": "SGD",
        "ocr_evidence": {
            "extraction_public_id": "rocr_test",
            "normalized_result_hash": "a" * 64,
        },
        "field_evidence": [
            {
                "field_name": "amount",
                "proposed_value": "12.34",
                "evidence_source_type": "ocr",
                "evidence_reference": json.dumps(
                    {
                        "extraction_public_id": "rocr_test",
                        "normalized_result_hash": "a" * 64,
                        "block_sequence_indexes": [0],
                    },
                    sort_keys=True,
                ),
            },
            {
                "field_name": "currency",
                "proposed_value": "SGD",
                "evidence_source_type": "ocr",
                "evidence_reference": json.dumps(
                    {
                        "extraction_public_id": "rocr_test",
                        "normalized_result_hash": "a" * 64,
                        "block_sequence_indexes": [0],
                    },
                    sort_keys=True,
                ),
            },
        ],
    }
    pair = _selected_money_pair(
        "12.34",
        "SGD",
        {"e0001": "TOTAL", "e0002": "S$ 12.34"},
        source_kind="receipt_local_ocr_text",
        parent_payload=parent_payload,
    )
    assert pair is not None
    assert pair["refs"] == ["e0001"]
    assert pair["pair_identity"]

    mismatched_value = json.loads(json.dumps(parent_payload))
    mismatched_value["field_evidence"][0]["proposed_value"] = "99.99"
    assert (
        _selected_money_pair(
            "12.34",
            "SGD",
            {"e0001": "TOTAL", "e0002": "S$ 12.34"},
            source_kind="receipt_local_ocr_text",
            parent_payload=mismatched_value,
        )
        is None
    )

    mismatched_extraction = json.loads(json.dumps(parent_payload))
    reference = json.loads(mismatched_extraction["field_evidence"][1]["evidence_reference"])
    reference["normalized_result_hash"] = "b" * 64
    mismatched_extraction["field_evidence"][1]["evidence_reference"] = json.dumps(
        reference,
        sort_keys=True,
    )
    assert (
        _selected_money_pair(
            "12.34",
            "SGD",
            {"e0001": "TOTAL", "e0002": "S$ 12.34"},
            source_kind="receipt_local_ocr_text",
            parent_payload=mismatched_extraction,
        )
        is None
    )


def test_ocr_money_pair_accepts_real_receipt_producer_evidence_shape() -> None:
    parent_payload = {
        "amount": "12.34",
        "currency": "SGD",
        "ocr_evidence": {
            "extraction_public_id": "rocr_real_shape",
            "normalized_result_hash": "a" * 64,
            "extraction_status": "succeeded",
        },
        "field_evidence": [
            {
                "field_name": "amount",
                "proposed_value": "12.34",
                "evidence_source_type": "ocr",
                "extraction_public_id": "rocr_real_shape",
                "normalized_result_hash": "a" * 64,
                "block_sequence_indexes": [0, 1],
                "excerpt": "TOTAL S$ 12.34",
            },
            {
                "field_name": "currency",
                "proposed_value": "SGD",
                "evidence_source_type": "ocr",
                "extraction_public_id": "rocr_real_shape",
                "normalized_result_hash": "a" * 64,
                "block_sequence_indexes": [0, 1],
                "excerpt": "TOTAL S$ 12.34",
            },
        ],
    }
    pair = _selected_money_pair(
        "12.34",
        "SGD",
        {"e0001": "TOTAL", "e0002": "S$ 12.34"},
        source_kind="receipt_local_ocr_text",
        parent_payload=parent_payload,
    )
    assert pair is not None
    assert pair["refs"] == ["e0001", "e0002"]
    assert pair["source_span"] == [0, 2]


@pytest.mark.parametrize(
    "indexes",
    ([0, -1], [0, "bad"], [0, 1.5], [0, 0], []),
)
def test_ocr_evidence_reference_rejects_malformed_block_indexes(
    indexes: list[object],
) -> None:
    reference = {
        "extraction_public_id": "rocr_test",
        "normalized_result_hash": "a" * 64,
        "block_sequence_indexes": indexes,
    }
    assert _parse_ocr_evidence_reference(json.dumps(reference)) is None


def test_committed_child_replay_and_reader_refuse_duplicate_child_pointer(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, _claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(_claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        child_id = conn.execute(
            "SELECT parser_output_id FROM ai_fallback_proposal_links WHERE result_id = "
            "(SELECT id FROM ai_fallback_results WHERE result_public_id = ?)",
            (result["result_public_id"],),
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_ai_fallback_raw_intake_no_insert_pointer_collision")
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                status, parser_output_id, idempotency_key
            ) VALUES (
                'raw_duplicate_child_s5e', 'telegram_text', 'telegram',
                'paid SGD 12.34 at Cafe', '2026-01-01T00:00:00Z',
                'parsed_pending_confirmation', ?, 'duplicate-child-s5e'
            )
            """,
            (child_id,),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="exactly one raw-intake binding"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_003,
            )
        child = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (child_id,)).fetchone()
        _payload, _completion_id, version = resolve_effective_payload(conn, child)
        with pytest.raises(AiFallbackServiceError, match="exactly one raw-intake binding"):
            verify_ai_fallback_child(
                conn,
                child,
                content_hash=compute_effective_proposal_content_hash(conn, child),
                proposal_version=version,
                require_resolved=False,
            )
    finally:
        conn.close()


def test_reader_refuses_trigger_bypassed_generic_reparse_escape(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result["proposal_public_id"],),
        ).fetchone()
        assert child is not None
        durable_attempt = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()
        assert durable_attempt is not None
        for trigger in (
            "trg_ai_fallback_raw_intake_no_lineage_escape",
            "trg_ai_fallback_raw_intake_no_update_pointer_collision",
            "trg_raw_intake_records_pointer_lineage_control",
        ):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
            (durable_attempt["parent_parser_output_id"], durable_attempt["raw_intake_record_id"]),
        )
        conn.commit()
        parent = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (durable_attempt["parent_parser_output_id"],),
        ).fetchone()
        assert parent is not None
        with pytest.raises(AiFallbackServiceError, match="escaped"):
            verify_ai_fallback_child(
                conn,
                ParserProposalRepository(conn).get(int(parent["id"])) or {},
                content_hash=compute_effective_proposal_content_hash(conn, parent),
                proposal_version=resolve_effective_payload(conn, parent)[2],
                require_resolved=False,
            )
    finally:
        conn.close()


def test_ocr_child_verifier_refuses_trigger_bypassed_extraction_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, conn, attempt, claim = _prepared_ocr_claim(tmp_path, monkeypatch)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        all_refs = [f"e{index:04d}" for index in range(2, 5)]
        response["amount"] = "12.34"
        response["currency"] = "SGD"
        response["transaction_date"] = "2026-08-13"
        response["merchant"] = "CAFE"
        response["field_confidence_bps"].update(
            {
                "amount": 9000,
                "currency": 9000,
                "transaction_date": 9000,
                "merchant": 9000,
            }
        )
        response["field_evidence_refs"].update(
            {
                "amount": all_refs,
                "currency": all_refs,
                "transaction_date": ["e0005"],
                "merchant": ["e0006"],
            }
        )
        body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(body).decode("ascii"),
            "response_byte_count": len(body),
            "response_sha256": canonical_response_sha256(body),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result["proposal_public_id"],),
        ).fetchone()
        assert child is not None
        child_hash = compute_effective_proposal_content_hash(conn, child)
        child_version = resolve_effective_payload(conn, child)[2]
        durable_attempt = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()
        assert durable_attempt is not None
        audit_event = conn.execute(
            """
            SELECT source_evidence_refs_json
            FROM financial_audit_events
            WHERE aggregate_type = 'parser_proposal'
              AND aggregate_public_id = ?
              AND event_type = 'parser_proposal_ai_fallback_child_created'
            """,
            (result["proposal_public_id"],),
        ).fetchone()
        assert audit_event is not None
        references = json.loads(audit_event["source_evidence_refs_json"])
        assert any(reference.startswith("attachment:") for reference in references)
        assert any(reference.startswith("ocr-link:") for reference in references)
        assert any(reference.startswith("ocr-extraction:") for reference in references)
        extraction_id = conn.execute(
            "SELECT extraction_id FROM receipt_ocr_proposal_links WHERE parser_output_id = ? "
            "AND link_role = 'initial'",
            (durable_attempt["parent_parser_output_id"],),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO receipt_ocr_extractions (
                public_id, attachment_id, source_attachment_hash, source_attachment_size,
                source_mime_type, engine_name, engine_version, engine_binary_sha256,
                engine_configuration_hash, extraction_fingerprint, extraction_status,
                block_count, total_normalized_text_length, normalized_result_hash,
                sanitized_outcome_code
            )
            SELECT 'rocr_tampered_s5e', attachment_id, source_attachment_hash,
                   source_attachment_size, source_mime_type, engine_name, engine_version,
                   engine_binary_sha256, engine_configuration_hash, ? AS extraction_fingerprint,
                   extraction_status, block_count, total_normalized_text_length,
                   normalized_result_hash, sanitized_outcome_code
            FROM receipt_ocr_extractions WHERE id = ?
            """,
            ("f" * 64, extraction_id),
        )
        tampered_extraction_id = conn.execute(
            "SELECT id FROM receipt_ocr_extractions WHERE public_id = 'rocr_tampered_s5e'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_receipt_ocr_proposal_links_no_update")
        conn.execute(
            "UPDATE receipt_ocr_proposal_links SET extraction_id = ? "
            "WHERE parser_output_id = ? AND link_role = 'ai_fallback'",
            (tampered_extraction_id, child["id"]),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="OCR|audit chain"):
            verify_ai_fallback_child(
                conn,
                ParserProposalRepository(conn).get(int(child["id"])) or {},
                content_hash=child_hash,
                proposal_version=child_version,
                require_resolved=False,
            )
    finally:
        conn.close()


def test_committed_child_replay_refuses_deleted_audit_event(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)
        first = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        audit_id = conn.execute(
            """
            SELECT event_public_id
            FROM financial_audit_events
            WHERE aggregate_public_id = ?
              AND event_type = 'parser_proposal_ai_fallback_child_created'
            """,
            (first["proposal_public_id"],),
        ).fetchone()["event_public_id"]
        conn.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
        conn.execute(
            "DELETE FROM financial_audit_events WHERE event_public_id = ?",
            (audit_id,),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError, match="audit chain"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_003,
            )
    finally:
        conn.close()


def test_result_rolls_back_on_untyped_audit_helper_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)

        def fail_audit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected audit helper failure")

        monkeypatch.setattr(ai_fallback_module, "_append_ai_child_audit_event", fail_audit)
        with pytest.raises(RuntimeError, match="injected audit helper failure"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_002,
            )
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM parser_outputs WHERE parser_name = 'finance_ai_proposal'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                """
            SELECT parser_output_id
            FROM raw_intake_records
            WHERE id = (
                SELECT raw_intake_record_id
                FROM ai_fallback_attempts
                WHERE attempt_public_id = ?
            )
            """,
                (attempt["attempt_public_id"],),
            ).fetchone()[0]
            == conn.execute(
                """
            SELECT parent_parser_output_id
            FROM ai_fallback_attempts
            WHERE attempt_public_id = ?
            """,
                (attempt["attempt_public_id"],),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_result_replay_survives_connection_loss_without_duplicate_child(tmp_path: Path) -> None:
    workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    body, arguments = _response_body(claim)
    first = record_ai_fallback_result(
        conn,
        attempt_public_id=attempt["attempt_public_id"],
        transport_outcome="response_received",
        arguments=arguments,
        now_ms=100_002,
    )
    conn.close()

    restarted = support.open_database(workspace)
    try:
        replay = record_ai_fallback_result(
            restarted,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_003,
        )
        assert replay == first
        assert restarted.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 1
        assert (
            restarted.execute(
                "SELECT COUNT(*) FROM parser_outputs WHERE parser_name = 'finance_ai_proposal'"
            ).fetchone()[0]
            == 1
        )
        assert body
    finally:
        restarted.close()


def test_result_replay_disposition_is_derived_from_persisted_result(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    body, arguments = _response_body(claim)
    try:
        first, first_replay = record_ai_fallback_result_with_disposition(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        replay, replay_replay = record_ai_fallback_result_with_disposition(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_003,
        )
        assert body
        assert first_replay is False
        assert replay_replay is True
        assert replay == first
    finally:
        conn.close()


def test_deterministic_deny_intent_is_rejected_by_lifecycle_guard(tmp_path: Path) -> None:
    _workspace, conn, _intake_public_id, parent_id = _captured_text_proposal(
        tmp_path,
        "Lunch SGD 12.34 paid by Alice at Cafe",
    )
    try:
        proposal = ParserProposalRepository(conn).get(parent_id)
        assert proposal is not None
        with pytest.raises(AiFallbackServiceError, match="deny policy"):
            verify_deterministic_intent_policy(proposal)
    finally:
        conn.close()


def test_shared_deterministic_proposal_keeps_its_existing_lifecycle(tmp_path: Path) -> None:
    _workspace, conn, _intake_public_id, parent_id = _captured_text_proposal(
        tmp_path,
        "Dinner MYR 85.00 shared equally with Owner, John, Mary at Nando's",
    )
    try:
        proposal = ParserProposalRepository(conn).get(parent_id)
        assert proposal is not None
        assert json.loads(proposal["normalized_payload"])["transaction_type"] == "shared_expense"
        assert requires_deterministic_intent_policy(proposal) is False
        with pytest.raises(AiFallbackServiceError, match="deny policy"):
            verify_deterministic_intent_policy(proposal)
    finally:
        conn.close()


def test_oversize_result_replays_without_retained_blob(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, received_arguments = _response_body(claim)
        arguments = {
            key: value
            for key, value in received_arguments.items()
            if key not in {"response_utf8_b64", "response_byte_count", "response_sha256"}
        }
        arguments.update(
            {
                "response_code_unit_count": 65_537,
                "response_byte_count": 65_537,
                "response_sha256": "a" * 64,
            }
        )
        first = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_oversize",
            arguments=arguments,
            now_ms=100_002,
        )
        replay = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_oversize",
            arguments=arguments,
            now_ms=100_003,
        )
        assert replay == first
        persisted = conn.execute(
            "SELECT response_blob, response_sha256, response_byte_count "
            "FROM ai_fallback_results WHERE result_public_id = ?",
            (first["result_public_id"],),
        ).fetchone()
        assert persisted["response_blob"] is None
        assert persisted["response_sha256"] == "a" * 64
        assert persisted["response_byte_count"] == 65_537
    finally:
        conn.close()


def test_concurrent_prepare_has_one_attempt_winner(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace,
                support.telegram_text_update("paid SGD 12.34 at Cafe"),
            ),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    intake_public_id = capture.response["result"]["intake_public_id"]
    support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
            },
            idempotency_key=support.canonical_propose_key(intake_public_id),
        )
    )

    def prepare_once() -> dict[str, object]:
        conn = support.open_database(workspace)
        try:
            return prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: prepare_once(), range(2)))
    assert sorted(result["claim_disposition"] for result in results) == [
        "claim_once",
        "do_not_claim",
    ]
    assert len({result["attempt_public_id"] for result in results}) == 1
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_claim_has_one_invocation_winner(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    capture = support.run_cli(
        support.make_request(
            "capture",
            support.capture_text_arguments(
                workspace,
                support.telegram_text_update("paid SGD 12.34 at Cafe"),
            ),
            idempotency_key=support.canonical_capture_key(message_id=10),
        )
    )
    intake_public_id = capture.response["result"]["intake_public_id"]
    support.run_cli(
        support.make_request(
            "propose",
            {
                "workspace_path": str(workspace.workspace_path),
                "intake_public_id": intake_public_id,
            },
            idempotency_key=support.canonical_propose_key(intake_public_id),
        )
    )
    conn = support.open_database(workspace)
    attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
    conn.close()

    def claim_once() -> dict[str, object]:
        candidate = support.open_database(workspace)
        try:
            return claim_ai_fallback_invocation(
                candidate,
                attempt_public_id=attempt["attempt_public_id"],
                now_ms=100_001,
            )
        finally:
            candidate.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim_once(), range(2)))
    assert sorted(result["invocation_disposition"] for result in results) == [
        "do_not_invoke",
        "invoke_once",
    ]
    conn = support.open_database(workspace)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_invocation_claims").fetchone()[0] == 1
    finally:
        conn.close()


def test_claim_crash_window_replays_outcome_unknown_without_retry(tmp_path: Path) -> None:
    workspace, conn, attempt, _claim = _prepared_claim(tmp_path)
    conn.close()

    restarted = support.open_database(workspace)
    try:
        intake_public_id = restarted.execute(
            """
            SELECT raw.public_id
            FROM raw_intake_records AS raw
            JOIN ai_fallback_attempts AS attempt
              ON attempt.raw_intake_record_id = raw.id
            WHERE attempt.attempt_public_id = ?
            """,
            (attempt["attempt_public_id"],),
        ).fetchone()[0]
        replay = prepare_ai_fallback(
            restarted,
            intake_public_id=intake_public_id,
            now_ms=140_001,
        )
        assert replay["claim_disposition"] == "do_not_claim"
        assert replay["replay_view"]["view_kind"] == "attempt"
        assert replay["replay_view"]["attempt_status"] == "outcome_unknown"
        assert replay["replay_view"]["recovery_disposition"] == (
            "resend_new_intake_after_unknown_outcome"
        )
        assert restarted.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 0
        assert (
            restarted.execute("SELECT COUNT(*) FROM ai_fallback_invocation_claims").fetchone()[0]
            == 1
        )
    finally:
        restarted.close()


@pytest.mark.parametrize("confidence", [7900, "7900"])
def test_low_confidence_ai_child_is_not_confirmation_resolved(
    tmp_path: Path, confidence: int | str
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response["field_confidence_bps"]["amount"] = confidence
        replacement = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        changed_arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(replacement).decode("ascii"),
            "response_byte_count": len(replacement),
            "response_sha256": canonical_response_sha256(replacement),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result["proposal_public_id"],),
        ).fetchone()
        assert child is not None
        with pytest.raises(AiFallbackServiceError, match="ambiguity remains unresolved"):
            verify_ai_fallback_child(
                conn,
                child,
                content_hash=compute_effective_proposal_content_hash(conn, child),
                proposal_version=resolve_effective_payload(conn, child)[2],
                require_resolved=True,
            )
    finally:
        conn.close()


def test_result_union_rejects_extra_fields_and_hash_drift(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)
        with pytest.raises(AiFallbackServiceError, match="exact transport union"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments={**arguments, "unexpected": True},
                now_ms=100_002,
            )
        with pytest.raises(AiFallbackServiceError, match="response body hash"):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments={**arguments, "response_sha256": "0" * 64},
                now_ms=100_002,
            )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 0
    finally:
        conn.close()


def test_result_deadline_equality_is_late_and_creates_no_child(tmp_path: Path) -> None:
    _workspace, conn, attempt, _claim = _prepared_claim(tmp_path)
    try:
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="provider_error",
            arguments={"failure_code": "host_llm_failed"},
            now_ms=attempt["result_not_after_ms"],
        )
        assert result["result_status"] == "late_result"
        assert result["recovery_disposition"] == "resend_new_intake_after_late_result"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("paid SGD 12.34 at Cafe", "positive"),
        ("paid a chargeback fee", "deny"),
        ("reimbursement for lunch", "deny"),
        ("third-party paid SGD 12.34", "deny"),
        ("付款 12.34 但要报销", "deny"),
        ("unpaid invoice", "unknown"),
    ],
)
def test_intent_policy_uses_deny_precedence_and_token_boundaries(text: str, expected: str) -> None:
    policy = {
        "positive_personal_intent_tokens": ["paid", "付款"],
        "deny_intent_tokens": [
            "chargeback",
            "reimbursement",
            "third-party",
            "报销",
        ],
    }
    assert _intent_result(text, policy)[0] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("paid by Alice SGD 12.34 at Cafe", "deny"),
        ("payer: Alice SGD 12.34 at Cafe", "deny"),
        ("paid by Owner SGD 12.34 at Cafe", "unknown"),
        ("personal expense paid by Owner SGD 12.34 at Cafe", "positive"),
        (
            "personal expense paid by Owner SGD 12.34 paid by Alice at Cafe",
            "deny",
        ),
        (
            "personal expense payer: Owner SGD 12.34 payer: Alice at Cafe",
            "deny",
        ),
        (
            "personal expense paid by Alice SGD 12.34 paid by Owner at Cafe",
            "deny",
        ),
        ("paid by Owner SGD 12.34 paid by me at Cafe", "unknown"),
        ("payer: Owner SGD 12.34 payer: self at Cafe", "unknown"),
        (
            "personal expense paid by Owner SGD 12.34 paid by me at Cafe",
            "positive",
        ),
        (
            "personal expense payer: Owner SGD 12.34 payer: self at Cafe",
            "positive",
        ),
        ("personal expense paid by Owner and Alice SGD 12.34 at Cafe", "deny"),
        ("personal expense paid by Owner/Alice SGD 12.34 at Cafe", "deny"),
        ("personal expense payer: Owner,Alice SGD 12.34 at Cafe", "deny"),
        ("paid by Owner and paid by me SGD 12.34 at Cafe", "unknown"),
        (
            "personal expense paid by Owner and payer: me SGD 12.34 at Cafe",
            "positive",
        ),
        ("personal expense paid by Owner SGD 12.34 paid by", "deny"),
        ("personal expense payer: Owner SGD 12.34 payer:", "deny"),
    ],
)
def test_controlled_payer_grammar_requires_independent_self_expense_evidence(
    text: str,
    expected: str,
) -> None:
    policy = ai_fallback_module._policy_assets()["intent"]
    assert _intent_result(text, policy)[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "paid by Alice SGD 12.34 at Cafe",
        "personal expense paid by Owner SGD 12.34 paid by Alice at Cafe",
        "personal expense payer: Owner SGD 12.34 payer: Alice at Cafe",
        "personal expense paid by Owner and Alice SGD 12.34 at Cafe",
        "personal expense paid by Owner/Alice SGD 12.34 at Cafe",
        "personal expense payer: Owner,Alice SGD 12.34 at Cafe",
        "personal expense paid by Owner SGD 12.34 paid by",
        "personal expense payer: Owner SGD 12.34 payer:",
    ],
)
def test_third_party_or_malformed_payer_is_refused_before_any_fallback_write(
    tmp_path: Path,
    text: str,
) -> None:
    _workspace, conn, intake_public_id, _parent_id = _captured_text_proposal(
        tmp_path,
        text,
    )
    try:
        with pytest.raises(AiFallbackServiceError) as error:
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert error.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert error.value.details == {
            "eligibility_disposition": "manual_recovery",
            "refusal_reason": "explicit_deny",
        }
        assert _fallback_counts(conn) == {
            "ai_fallback_attempts": 0,
            "ai_fallback_invocation_claims": 0,
            "ai_fallback_results": 0,
            "ai_fallback_proposal_links": 0,
        }
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "text",
    [
        "paid by Owner SGD 12.34 at Cafe",
        "paid by Owner SGD 12.34 paid by me at Cafe",
        "payer: Owner SGD 12.34 payer: self at Cafe",
        "paid by Owner and paid by me SGD 12.34 at Cafe",
    ],
)
def test_self_payer_attributions_are_classification_only_without_child(
    tmp_path: Path,
    text: str,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        text,
    )
    try:
        _body, arguments = _response_body(claim)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "classification_only"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["description", "account", "category"])
@pytest.mark.parametrize(
    "value",
    [
        "manual destination",
        "",
        " ",
        "\t\r\n",
        "\u00a0",
        "\u2003",
        0,
        False,
        [],
        {},
    ],
)
def test_prepare_forbidden_parent_field_is_stable_and_side_effect_free(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(tmp_path)
    try:
        _set_parent_payload_fields(conn, parent_id, {field: value})
        parent_before = dict(
            conn.execute(
                "SELECT parsed_payload, normalized_payload, parse_status FROM parser_outputs "
                "WHERE id = ?",
                (parent_id,),
            ).fetchone()
        )
        pointer_before = conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
            (intake_public_id,),
        ).fetchone()[0]
        counts_before = _fallback_counts(conn)
        with pytest.raises(AiFallbackServiceError) as error:
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert error.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert error.value.details == {
            "eligibility_disposition": "manual_recovery",
            "refusal_reason": "forbidden_field",
        }
        assert _fallback_counts(conn) == counts_before
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
        assert (
            dict(
                conn.execute(
                    "SELECT parsed_payload, normalized_payload, parse_status FROM parser_outputs "
                    "WHERE id = ?",
                    (parent_id,),
                ).fetchone()
            )
            == parent_before
        )
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id = ?",
                (intake_public_id,),
            ).fetchone()[0]
            == pointer_before
        )
    finally:
        conn.close()


TELEGRAM_TOKEN_VECTORS = (
    "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
    "1234567:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
    "https://api.telegram.org/bot1234567:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef/getMe",
)

SENSITIVE_ZERO_CALL_VECTORS = (
    "paid SGD 12.34 at Cafe 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
    *(f"paid SGD 12.34 at Cafe {token}" for token in TELEGRAM_TOKEN_VECTORS),
    "paid SGD 12.34 at Cafe sk-proj-abcdefghijklmnopqrstuvwxyz",
    "paid SGD 12.34 at Cafe Authorization: Bearer abcdefghijklmnop",
    "paid SGD 12.34 at Cafe Bearer abcdefghijklmnop1234",
    "paid SGD 12.34 at Cafe Basic dXNlcjpwYXNzd29yZA==",
    "paid SGD 12.34 at Cafe -----BEGIN PRIVATE KEY-----",
    "paid SGD 12.34 at Cafe https://alice:secret@example.com",
    "paid SGD 12.34 at Cafe /Users/example-user/Documents/Finance-Codex/database/finance.db",
    "paid SGD 12.34 at Cafe ../database/finance.db",
    "paid SGD 12.34 at Cafe OPENAI_API_KEY",
)


@pytest.mark.parametrize("text", SENSITIVE_ZERO_CALL_VECTORS)
def test_sensitive_text_policy_rejects_complete_scalar_before_claim_or_model(
    tmp_path: Path,
    text: str,
) -> None:
    _workspace, conn, intake_public_id, _parent_id = _captured_text_proposal(tmp_path, text)
    try:
        with pytest.raises(AiFallbackServiceError) as error:
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert error.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert error.value.details is not None
        assert error.value.details["refusal_reason"] == "sensitive_text_refused"
        assert _fallback_counts(conn) == {
            "ai_fallback_attempts": 0,
            "ai_fallback_invocation_claims": 0,
            "ai_fallback_results": 0,
            "ai_fallback_proposal_links": 0,
        }
    finally:
        conn.close()


@pytest.mark.parametrize("ordinary_text", ["basic groceries", "bearer securities"])
def test_sensitive_policy_keeps_normal_language_money_date_merchant_eligible(
    tmp_path: Path,
    ordinary_text: str,
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_text_proposal(
        tmp_path,
        f"paid SGD 12.34 at Cafe on 2026-08-13 for {ordinary_text}",
    )
    try:
        _set_parent_payload_fields(
            conn,
            parent_id,
            {"description": None, "account": None, "category": None},
        )
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert attempt["attempt_public_id"].startswith("aifa_")
        assert _fallback_counts(conn)["ai_fallback_attempts"] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("token", TELEGRAM_TOKEN_VECTORS)
def test_sensitive_telegram_token_in_ocr_scalar_is_zero_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    _workspace, conn, intake_public_id, _parent_id = _captured_ocr_proposal(
        tmp_path,
        monkeypatch,
        extra_block_texts=(token,),
    )
    try:
        with pytest.raises(AiFallbackServiceError) as error:
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert error.value.code == "AI_FALLBACK_NOT_ELIGIBLE"
        assert error.value.details is not None
        assert error.value.details["refusal_reason"] == "sensitive_text_refused"
        assert _fallback_counts(conn) == {
            "ai_fallback_attempts": 0,
            "ai_fallback_invocation_claims": 0,
            "ai_fallback_results": 0,
            "ai_fallback_proposal_links": 0,
        }
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("ordinary_text", ["basic groceries", "bearer securities"])
def test_sensitive_policy_keeps_normal_language_ocr_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ordinary_text: str,
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_ocr_proposal(
        tmp_path,
        monkeypatch,
        extra_block_texts=(ordinary_text,),
    )
    try:
        _set_parent_payload_fields(
            conn,
            parent_id,
            {"description": None, "account": None, "category": None},
        )
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        assert attempt["attempt_public_id"].startswith("aifa_")
        assert _fallback_counts(conn)["ai_fallback_attempts"] == 1
    finally:
        conn.close()


def test_ai_child_numeric_preflight_rejects_lossy_large_amount_without_child(
    tmp_path: Path,
) -> None:
    huge_amount = "100000000000000000000.00"
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path,
        f"paid SGD {huge_amount} at Cafe",
    )
    try:
        body, arguments = _response_body(claim)
        replacement, changed_arguments = _replace_response(
            body,
            arguments,
            amount=huge_amount,
        )
        assert replacement
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed_arguments,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["non_child_reason"] == "validation_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_numeric_mirror_contract_covers_scientific_storage_and_trailing_zero(
    tmp_path: Path,
) -> None:
    _workspace, conn, _attempt = _prepared_attempt(tmp_path)
    try:
        assert sqlite_numeric_roundtrip_matches(conn, "12.00") is True
        scientific_mirror = conn.execute(
            "SELECT CAST(? AS NUMERIC)", ("100000000000000000000.00",)
        ).fetchone()[0]
        assert decimal_from_numeric_mirror(scientific_mirror) is None
        assert sqlite_numeric_roundtrip_matches(conn, "100000000000000000000.00") is False
    finally:
        conn.close()


def test_text_conversion_accepts_trailing_zero_after_same_transaction_readback(
    tmp_path: Path,
) -> None:
    _workspace, conn, parent_id = _confirmed_text_proposal(tmp_path, amount="12.00")
    try:
        converted = convert_confirmed_parser_proposal(conn, parent_id)
        assert converted["final_transaction_created"] is True
        row = conn.execute(
            "SELECT amount, total_amount, currency FROM transactions WHERE parser_output_id = ?",
            (parent_id,),
        ).fetchone()
        assert row is not None
        assert decimal_from_numeric_mirror(row["amount"]) == Decimal("12.00")
        assert decimal_from_numeric_mirror(row["total_amount"]) == Decimal("12.00")
        assert row["currency"] == "SGD"
    finally:
        conn.close()


def test_text_conversion_rejects_lossy_large_amount_before_transaction_write(
    tmp_path: Path,
) -> None:
    _workspace, conn, parent_id = _confirmed_text_proposal(
        tmp_path,
        amount="100000000000000000000.00",
    )
    try:
        with pytest.raises(ProposalConversionError, match="NUMERIC"):
            convert_confirmed_parser_proposal(conn, parent_id)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_conversion_audit").fetchone()[0] == 0
        )
        assert not conn.in_transaction
    finally:
        conn.close()


def test_text_conversion_rolls_back_when_persisted_money_readback_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, conn, parent_id = _confirmed_text_proposal(tmp_path)
    original_insert = parser_service.CanonicalTransactionRepository.insert

    def tampered_insert(repository: Any, values: dict[str, object]) -> int:
        transaction_id = original_insert(repository, values)
        repository._conn.execute(  # type: ignore[attr-defined]
            "UPDATE transactions SET amount = ?, total_amount = ? WHERE id = ?",
            ("12.35", "12.35", transaction_id),
        )
        return transaction_id

    monkeypatch.setattr(parser_service.CanonicalTransactionRepository, "insert", tampered_insert)
    try:
        with pytest.raises(ProposalConversionError, match="Persisted parser transaction"):
            convert_confirmed_parser_proposal(conn, parent_id)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_conversion_audit").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_result_rechecks_source_evidence_inside_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    conn2 = support.open_database(workspace)
    fired = False
    original_verify = ai_fallback_module._verify_attempt_source_material

    def race_verify(*args: Any, **kwargs: Any) -> None:
        nonlocal fired
        original_verify(*args, **kwargs)
        if not fired:
            fired = True
            intake = kwargs["intake"]
            updated = conn2.execute(
                "UPDATE raw_intake_evidence SET evidence_reference = ? "
                "WHERE raw_intake_record_id = ? AND evidence_type = 'raw_input'",
                ("tampered-evidence-reference", intake["id"]),
            )
            assert updated.rowcount == 1
            conn2.commit()

    monkeypatch.setattr(ai_fallback_module, "_verify_attempt_source_material", race_verify)
    try:
        _body, arguments = _response_body(claim)
        with pytest.raises(AiFallbackServiceError):
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_002,
            )
        assert fired is True
        assert not conn.in_transaction
        assert _fallback_counts(conn) == {
            "ai_fallback_attempts": 1,
            "ai_fallback_invocation_claims": 1,
            "ai_fallback_results": 0,
            "ai_fallback_proposal_links": 0,
        }
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn2.close()
        conn.close()


@pytest.mark.parametrize("tamper", ["audit", "link", "result"])
def test_prepare_result_replay_refuses_audit_link_or_result_tamper(
    tmp_path: Path,
    tamper: str,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        _body, arguments = _response_body(claim)
        first = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        durable_attempt = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()
        assert durable_attempt is not None
        if tamper == "audit":
            audit_id = conn.execute(
                "SELECT event_public_id FROM financial_audit_events "
                "WHERE aggregate_public_id = ? "
                "AND event_type = 'parser_proposal_ai_fallback_child_created'",
                (first["proposal_public_id"],),
            ).fetchone()[0]
            conn.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
            conn.execute(
                "DELETE FROM financial_audit_events WHERE event_public_id = ?", (audit_id,)
            )
        elif tamper == "link":
            conn.execute("DROP TRIGGER trg_ai_fallback_links_no_update")
            conn.execute(
                "UPDATE ai_fallback_proposal_links SET effective_content_hash = ? "
                "WHERE result_id = (SELECT id FROM ai_fallback_results WHERE attempt_id = ?)",
                ("0" * 64, durable_attempt["id"]),
            )
        else:
            conn.execute("DROP TRIGGER trg_ai_fallback_results_no_update")
            conn.execute(
                "UPDATE ai_fallback_results SET response_sha256 = ? WHERE attempt_id = ?",
                ("0" * 64, durable_attempt["id"]),
            )
        conn.commit()
        intake_public_id = conn.execute(
            "SELECT public_id FROM raw_intake_records WHERE id = ?",
            (durable_attempt["raw_intake_record_id"],),
        ).fetchone()[0]
        with pytest.raises(AiFallbackServiceError):
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_004)
        assert _fallback_counts(conn)["ai_fallback_results"] == 1
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 2
    finally:
        conn.close()


def test_non_child_provider_error_replay_verifies_preparation_and_attempt_identity(
    tmp_path: Path,
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        first = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="provider_error",
            arguments={"failure_code": "host_llm_failed"},
            now_ms=100_002,
        )
        durable_attempt = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()
        assert durable_attempt is not None
        intake_public_id = conn.execute(
            "SELECT public_id FROM raw_intake_records WHERE id = ?",
            (durable_attempt["raw_intake_record_id"],),
        ).fetchone()[0]
        replay = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_003)
        assert replay["claim_disposition"] == "do_not_claim"
        assert replay["replay_view"]["result"]["result_status"] == "provider_error"
        assert first["result_status"] == "provider_error"
        conn.execute("DROP TRIGGER trg_ai_fallback_attempts_no_update")
        conn.execute(
            "UPDATE ai_fallback_attempts SET expected_model = ? WHERE id = ?",
            ("tampered-model", durable_attempt["id"]),
        )
        conn.commit()
        with pytest.raises(AiFallbackServiceError):
            prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_004)
    finally:
        conn.close()


@pytest.mark.parametrize("replay_surface", ["prepare", "result"])
def test_non_child_replay_refuses_trigger_bypassed_link_and_pointer(
    tmp_path: Path,
    replay_surface: str,
) -> None:
    _workspace, conn, attempt, _claim = _prepared_claim(tmp_path)
    arguments = {"failure_code": "host_llm_failed"}
    try:
        first = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="provider_error",
            arguments=arguments,
            now_ms=100_002,
        )
        durable_attempt = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?",
            (attempt["attempt_public_id"],),
        ).fetchone()
        parent = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (durable_attempt["parent_parser_output_id"],),
        ).fetchone()
        intake = conn.execute(
            "SELECT * FROM raw_intake_records WHERE id = ?",
            (durable_attempt["raw_intake_record_id"],),
        ).fetchone()
        assert parent is not None and intake is not None
        child_id = int(
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, attachment_id,
                    parser_name, parser_version, ai_provider, ai_model, prompt_version,
                    raw_text, parsed_payload, normalized_payload, confidence_score,
                    parse_status, parent_parser_output_id
                ) VALUES (?, ?, ?, ?, 'finance_ai_proposal', 'finance-ai-proposal-v1',
                          ?, ?, ?, ?, '{}', '{}', 0.5,
                          'parsed_pending_confirmation', ?)
                """,
                (
                    "prop_forged_non_child",
                    parent["source_type"],
                    intake["public_id"],
                    parent["attachment_id"],
                    durable_attempt["expected_provider"],
                    durable_attempt["expected_model"],
                    durable_attempt["prompt_version"],
                    parent["raw_text"],
                    parent["id"],
                ),
            ).lastrowid
        )
        result_id = int(
            conn.execute(
                "SELECT id FROM ai_fallback_results WHERE result_public_id = ?",
                (first["result_public_id"],),
            ).fetchone()[0]
        )
        conn.execute("DROP TRIGGER trg_ai_fallback_links_require_created_child_lineage")
        conn.execute(
            """
            INSERT INTO ai_fallback_proposal_links (
                link_public_id, link_material_hash, result_id, parser_output_id,
                proposal_version, effective_content_hash
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            ("aipl_" + "7" * 64, "8" * 64, result_id, child_id, "9" * 64),
        )
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
            (child_id, intake["id"]),
        )
        conn.commit()

        with pytest.raises(AiFallbackServiceError) as error:
            if replay_surface == "prepare":
                prepare_ai_fallback(
                    conn,
                    intake_public_id=intake["public_id"],
                    now_ms=100_003,
                )
            else:
                record_ai_fallback_result(
                    conn,
                    attempt_public_id=attempt["attempt_public_id"],
                    transport_outcome="provider_error",
                    arguments=arguments,
                    now_ms=100_003,
                )
        assert error.value.code == "AI_FALLBACK_CONFLICT"
        assert _fallback_counts(conn)["ai_fallback_proposal_links"] == 1
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE id = ?",
                (intake["id"],),
            ).fetchone()[0]
            == child_id
        )
    finally:
        conn.close()


@pytest.mark.parametrize("replay_surface", ["prepare", "result"])
@pytest.mark.parametrize("tamper", ["different_hash", "legacy_null"])
def test_result_arguments_hash_tamper_or_legacy_null_fails_closed(
    tmp_path: Path,
    replay_surface: str,
    tamper: str,
) -> None:
    _workspace, conn, attempt, _claim = _prepared_claim(tmp_path)
    original_arguments = {"failure_code": "host_llm_failed"}
    try:
        record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="provider_error",
            arguments=original_arguments,
            now_ms=100_002,
        )
        alternative_arguments = {"failure_code": "deadline_exceeded"}
        alternative_hash = ai_fallback_module._hash_material(
            "finance-ai-result-arguments-v1",
            {"transport_outcome": "timeout", "arguments": alternative_arguments},
        )
        conn.execute("DROP TRIGGER trg_ai_fallback_results_no_update")
        conn.execute(
            "UPDATE ai_fallback_results SET result_arguments_hash = ? "
            "WHERE attempt_id = (SELECT id FROM ai_fallback_attempts "
            "WHERE attempt_public_id = ?)",
            (
                None if tamper == "legacy_null" else alternative_hash,
                attempt["attempt_public_id"],
            ),
        )
        conn.commit()
        intake_public_id = conn.execute(
            """
            SELECT intake.public_id
            FROM raw_intake_records AS intake
            JOIN ai_fallback_attempts AS attempt ON attempt.raw_intake_record_id = intake.id
            WHERE attempt.attempt_public_id = ?
            """,
            (attempt["attempt_public_id"],),
        ).fetchone()[0]

        with pytest.raises(AiFallbackServiceError) as error:
            if replay_surface == "prepare":
                prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_003)
            elif tamper == "legacy_null":
                record_ai_fallback_result(
                    conn,
                    attempt_public_id=attempt["attempt_public_id"],
                    transport_outcome="provider_error",
                    arguments=original_arguments,
                    now_ms=100_003,
                )
            else:
                record_ai_fallback_result(
                    conn,
                    attempt_public_id=attempt["attempt_public_id"],
                    transport_outcome="timeout",
                    arguments=alternative_arguments,
                    now_ms=100_003,
                )
        assert error.value.code == "AI_FALLBACK_CONFLICT"
    finally:
        conn.close()


def test_fact_conflict_survives_write_and_audit_replay(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        ref = response["field_evidence_refs"]["merchant"][0]
        response["merchant"] = None
        response["field_evidence_refs"]["merchant"] = []
        response["field_confidence_bps"]["merchant"] = None
        response["field_conflicts"]["merchant"] = [ref]
        raw, changed = _replace_response(body, arguments, **response)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result["proposal_public_id"],),
        ).fetchone()
        payload = json.loads(child["parsed_payload"])
        assert "ambiguous_merchant" in payload["ambiguity_flags"]
        assert "source_conflict" in payload["ambiguity_flags"]
        retained = conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0]
        assert retained == raw
        assert "ambiguity_flags" not in json.loads(retained)
        assert json.loads(retained)["field_conflicts"]["merchant"] == [ref]
        kwargs = dict(
            content_hash=compute_effective_proposal_content_hash(conn, child),
            proposal_version=resolve_effective_payload(conn, child)[2],
        )
        verify_ai_fallback_child(conn, child, **kwargs, require_resolved=False)
        with pytest.raises(AiFallbackServiceError, match="ambiguity remains unresolved"):
            verify_ai_fallback_child(conn, child, **kwargs, require_resolved=True)
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_003,
            )
            == result
        )
    finally:
        conn.close()


def test_current_prompt_refuses_legacy_wire_response(tmp_path: Path) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response.pop("field_conflicts")
        response.update(schema_version="finance-ai-proposal-v1", ambiguity_flags=["missing_date"])
        raw = json.dumps(response).encode()
        changed = {
            **arguments,
            "response_utf8_b64": base64.b64encode(raw).decode(),
            "response_byte_count": len(raw),
            "response_sha256": canonical_response_sha256(raw),
        }
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    finally:
        conn.close()


def test_ocr_item_tax_cash_do_not_conflict_with_persisted_total_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, conn, intake_public_id, _parent_id = _captured_ocr_proposal(
        tmp_path,
        monkeypatch,
        extra_block_texts=("ITEM SGD 5.00", "TAX SGD 1.00", "CASH SGD 20.00"),
    )
    try:
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        claim = claim_ai_fallback_invocation(
            conn, attempt_public_id=attempt["attempt_public_id"], now_ms=100_001
        )
        body, arguments = _response_body(claim)
        raw, changed = _ocr_response_with_inherited_date(body, arguments, ambiguity_flags=[])
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result["proposal_public_id"],)
        ).fetchone()
        payload = json.loads(child["parsed_payload"])
        assert payload["amount"] == "12.34"
        assert payload["currency"] == "SGD"
        # The existing unresolved merchant restriction is retained; unrelated
        # monetary lines must not introduce any amount or currency conflict.
        assert payload["ambiguity_flags"] == ["merchant_not_determined"]
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        verify_ai_fallback_child(
            conn,
            child,
            content_hash=compute_effective_proposal_content_hash(conn, child),
            proposal_version=resolve_effective_payload(conn, child)[2],
            require_resolved=False,
        )
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_003,
            )
            == result
        )
    finally:
        conn.close()


@pytest.mark.parametrize("page_width", [800, 60_000])
@pytest.mark.parametrize("quoted_confidence", [False, True])
@pytest.mark.parametrize("repeat_total_in_conflicts", [False, True])
def test_ocr_proven_item_conflict_preserves_response_and_child_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_width: int,
    quoted_confidence: bool,
    repeat_total_in_conflicts: bool,
) -> None:
    _workspace, conn, intake_public_id, _parent_id = _captured_ocr_proposal(
        tmp_path,
        monkeypatch,
        date_text="CAFE",
        page_width=page_width,
        extra_block_specs=(("ITEM SGD 5.00", 7, 240),),
    )
    try:
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        claim = claim_ai_fallback_invocation(
            conn, attempt_public_id=attempt["attempt_public_id"], now_ms=100_001
        )
        body, arguments = _response_body(claim)
        raw, changed = _ocr_response_with_inherited_date(body, arguments, ambiguity_flags=[])
        raw, changed = _replace_response(
            raw,
            changed,
            field_evidence_refs={**json.loads(raw)["field_evidence_refs"], "merchant": ["e0005"]},
            field_conflicts={
                "amount": ["e0007"]
                + (["e0002", "e0003", "e0004"] if repeat_total_in_conflicts else []),
                "currency": [],
                "transaction_date": [],
                "merchant": [],
            },
        )
        if quoted_confidence:
            raw, changed = _replace_response(
                raw,
                changed,
                field_confidence_bps={
                    field: str(value) if value is not None else None
                    for field, value in json.loads(raw)["field_confidence_bps"].items()
                },
            )
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result["proposal_public_id"],)
        ).fetchone()
        payload = json.loads(child["parsed_payload"])
        assert payload["amount"] == "12.34"
        assert payload["currency"] == "SGD"
        # The missing date restriction survives the proven ITEM/TOTAL comparison.
        assert "transaction_date_not_found" in payload["ambiguity_flags"]
        assert "conflicting_total_candidates" not in payload["ambiguity_flags"]
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        verify_ai_fallback_child(
            conn,
            child,
            content_hash=compute_effective_proposal_content_hash(conn, child),
            proposal_version=resolve_effective_payload(conn, child)[2],
            require_resolved=False,
        )
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_003,
            )
            == result
        )
    finally:
        conn.close()


def test_ocr_two_totals_cannot_be_cleared_by_empty_model_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace, conn, intake_public_id, parent_id = _captured_ocr_proposal(
        tmp_path,
        monkeypatch,
        extra_block_specs=(("TOTAL SGD 20.00", 5, 220),),
    )
    try:
        parent_payload = json.loads(
            conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE id = ?", (parent_id,)
            ).fetchone()[0]
        )
        assert "conflicting_total_candidates" in parent_payload["ambiguity_flags"]
        assert parent_payload["amount"] is None
        assert parent_payload["currency"] is None
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_public_id, now_ms=100_000)
        claim = claim_ai_fallback_invocation(
            conn, attempt_public_id=attempt["attempt_public_id"], now_ms=100_001
        )
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response["amount"] = None
        response["currency"] = None
        response["merchant"] = "CAFE"
        response["field_evidence_refs"]["amount"] = []
        response["field_evidence_refs"]["currency"] = []
        response["field_evidence_refs"]["merchant"] = ["e0006"]
        response["field_confidence_bps"]["amount"] = None
        response["field_confidence_bps"]["currency"] = None
        assert all(refs == [] for refs in response["field_conflicts"].values())
        raw, changed = _replace_response(body, arguments, **response)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_002,
        )
        assert result["result_status"] == "response_refused"
        assert result["proposal_public_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_003,
            )
            == result
        )
    finally:
        conn.close()


@pytest.mark.parametrize("tamper", ["money_extra", "date_wrong", "date_extra", "valid_date"])
def test_ocr_field_scoped_evidence_controls_child_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    _workspace, conn, intake_id, _parent_id = _captured_ocr_proposal(
        tmp_path, monkeypatch, date_text="13/08/2026"
    )
    try:
        attempt = prepare_ai_fallback(conn, intake_public_id=intake_id, now_ms=100_000)
        claim = claim_ai_fallback_invocation(
            conn, attempt_public_id=attempt["attempt_public_id"], now_ms=100_001
        )
        body, arguments = _response_body(claim)
        body, arguments = _ocr_response_with_inherited_date(body, arguments, ambiguity_flags=[])
        response = json.loads(body)
        response["transaction_date"] = "2026-08-13"
        response["field_confidence_bps"]["transaction_date"] = 9000
        response["field_evidence_refs"]["transaction_date"] = ["e0005"]
        if tamper == "money_extra":
            for field in ("amount", "currency"):
                response["field_evidence_refs"][field].append("e0006")
        elif tamper == "date_wrong":
            response["field_evidence_refs"]["transaction_date"] = ["e0006"]
        elif tamper == "date_extra":
            response["field_evidence_refs"]["transaction_date"].append("e0006")
        raw, arguments = _replace_response(body, arguments, **response)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        valid = tamper == "valid_date"
        assert result["result_status"] == ("proposal_created" if valid else "response_refused")
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == (
            2 if valid else 1
        )
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        if not valid:
            assert result["proposal_public_id"] is None
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM parser_proposal_field_evidence "
                    "WHERE evidence_source_type = 'ai_model'"
                ).fetchone()[0]
                == 0
            )
        else:
            child = conn.execute(
                "SELECT * FROM parser_outputs WHERE public_id = ?", (result["proposal_public_id"],)
            ).fetchone()
            verify_ai_fallback_child(
                conn,
                child,
                content_hash=compute_effective_proposal_content_hash(conn, child),
                proposal_version=resolve_effective_payload(conn, child)[2],
                require_resolved=False,
            )
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=arguments,
                now_ms=100_003,
            )
            == result
        )
    finally:
        conn.close()


def test_confidence_decoder_change_never_upgrades_a_historical_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finance_core.parser_proposals import ai_fact_observations

    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response["field_confidence_bps"]["amount"] = "9500"
        raw, changed = _replace_response(body, arguments, **response)
        # Emulate the former strict decoder when this terminal result was stored.
        with monkeypatch.context() as former:
            former.setattr(ai_fact_observations, "_decode_confidence_bps", lambda value: value)
            historical = record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_002,
            )
        assert historical["result_status"] == "response_refused"
        replay = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_003,
        )
        assert replay == historical
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 1
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_proposal_links").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("sparse", [False, True])
def test_null_merchant_conflicts_preserve_parent_provenance_and_replay(
    tmp_path: Path, sparse: bool
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response["merchant"] = None
        response["field_confidence_bps"]["merchant"] = None
        response["field_evidence_refs"]["merchant"] = []
        if sparse:
            response["field_conflicts"].pop("merchant")
            response["field_conflicts"].pop("transaction_date")
        raw, changed = _replace_response(body, arguments, **response)
        result = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=changed,
            now_ms=100_002,
        )
        assert result["result_status"] == "proposal_created"
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result["proposal_public_id"],)
        ).fetchone()
        payload = json.loads(child["parsed_payload"])
        assert payload["merchant"] == "Cafe"
        assert payload["amount"] == "12.34" and payload["currency"] == "SGD"
        evidence = conn.execute(
            "SELECT * FROM parser_proposal_field_evidence "
            "WHERE parser_output_id = ? AND field_name = 'merchant'",
            (child["id"],),
        ).fetchone()
        assert evidence["evidence_source_type"] == "system"
        assert "inherited_from_parent" in json.loads(evidence["evidence_reference"])
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        verify_ai_fallback_child(
            conn,
            child,
            content_hash=compute_effective_proposal_content_hash(conn, child),
            proposal_version=resolve_effective_payload(conn, child)[2],
            require_resolved=False,
        )
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_003,
            )
            == result
        )
    finally:
        conn.close()


def test_sparse_decoder_does_not_upgrade_historical_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finance_core.parser_proposals import ai_response_validation

    original = ai_response_validation.normalize_fact_observations

    def former_decoder(response: Any, **kwargs: Any) -> Any:
        if set(response["field_conflicts"]) != {
            "amount",
            "currency",
            "transaction_date",
            "merchant",
        }:
            raise ValueError("former exact conflict keys")
        return original(response, **kwargs)

    _workspace, conn, attempt, claim = _prepared_claim(tmp_path)
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        response["field_conflicts"].pop("transaction_date")
        raw, changed = _replace_response(body, arguments, **response)
        with monkeypatch.context() as former:
            former.setattr(ai_response_validation, "normalize_fact_observations", former_decoder)
            historical = record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_002,
            )
        assert historical["result_status"] == "response_refused"
        assert (
            record_ai_fallback_result(
                conn,
                attempt_public_id=attempt["attempt_public_id"],
                transport_outcome="response_received",
                arguments=changed,
                now_ms=100_003,
            )
            == historical
        )
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_results").fetchone()[0] == 1
        assert conn.execute("SELECT response_blob FROM ai_fallback_results").fetchone()[0] == raw
        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_proposal_links").fetchone()[0] == 0
    finally:
        conn.close()
