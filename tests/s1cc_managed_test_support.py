"""Synthetic fixture helpers for S1C-C managed Bridge tests.

These helpers intentionally reuse the existing A/B managed-profile fixtures
and the service-level AI response builders.  They do not invoke a provider or
alter production entry points.
"""

from __future__ import annotations

import json
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.openclaw_staging_bridge import workspace_access
from finance_core.profile_gate import exclusive_cut
from tests import test_ai_fallback_service_v1 as ai_service_tests
from tests import test_receipt_ocr_proposal_ingestion_v1 as ocr_tests
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests import test_s1c_b_managed_bridge_commands as s1cb


def _ai_eligible_ocr_blocks() -> tuple[Any, ...]:
    """Use the same six-block shape as existing AI OCR service tests."""
    return (
        ocr_tests._ocr_block(0, "PAID", line=0, left=10, top=20),
        ocr_tests._ocr_block(1, "TOTAL", line=3, left=10, top=140),
        ocr_tests._ocr_block(2, "S$", line=3, left=80, top=140),
        ocr_tests._ocr_block(3, "12.34", line=3, left=140, top=140),
        ocr_tests._ocr_block(4, "2026-08-13", line=1, left=10, top=60),
        ocr_tests._ocr_block(5, "CAFE", line=0, left=70, top=20),
    )


def _intake_for_proposal(workspace: s1ca.ManagedBridgeWorkspace, proposal_id: str) -> str:
    row = s1ca._read_one(
        workspace,
        """
        SELECT intake.public_id
        FROM raw_intake_records AS intake
        JOIN parser_outputs AS proposal ON proposal.id = intake.parser_output_id
        WHERE proposal.public_id = ?
        """,
        (proposal_id,),
    )
    assert row is not None
    return str(row[0])


def _clear_parent_destination_fields(
    workspace: s1ca.ManagedBridgeWorkspace, proposal_id: str
) -> None:
    """Match the pending-parent fixture shape used by the existing AI suites."""
    with workspace_access.workspace_database_session(
        workspace.workspace_path, operation_id="test-s1cc-clear-ai-destinations"
    ) as conn:
        row = conn.execute(
            "SELECT id, parsed_payload, normalized_payload FROM parser_outputs WHERE public_id = ?",
            (proposal_id,),
        ).fetchone()
        assert row is not None
        for column in ("parsed_payload", "normalized_payload"):
            raw = row[column]
            if raw is None:
                continue
            payload = json.loads(raw)
            payload.update({"description": None, "account": None, "category": None})
            conn.execute(
                f"UPDATE parser_outputs SET {column} = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), row["id"]),
            )
        conn.commit()


def seed_ai_source(
    workspace: s1ca.ManagedBridgeWorkspace,
    source_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    """Create one lawful synthetic pending text or OCR source in the profile."""
    if source_kind == "text":
        proposal_id = s1ca._seed_proposal(
            workspace,
            source_text="paid SGD 12.34 at Cafe",
            amount="12.34",
            transaction_date=None,
        )
    elif source_kind == "receipt_ocr":
        monkeypatch.setattr(s1cb.receipt_ocr_tests, "_sgd_blocks", _ai_eligible_ocr_blocks)
        proposal_id, _version, _content_hash = s1cb._seed_managed_receipt_proposal(
            workspace,
            confirm=False,
        )
    else:
        raise AssertionError(f"unsupported source kind: {source_kind}")

    intake_id = _intake_for_proposal(workspace, proposal_id)
    _clear_parent_destination_fields(workspace, proposal_id)
    return proposal_id, intake_id


def request(
    workspace: s1ca.ManagedBridgeWorkspace,
    command: str,
    arguments: dict[str, object],
    *,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    return s1ca._request(
        workspace,
        command,
        arguments,
        idempotency_key=idempotency_key,
    )


def run(workspace: s1ca.ManagedBridgeWorkspace, payload: dict[str, object]) -> support.CliOutcome:
    """Reuse A's actual Bridge invocation and per-command close witness."""
    return s1ca._run(workspace, payload)


def read_count(workspace: s1ca.ManagedBridgeWorkspace, table_name: str) -> int:
    allowed = {
        "ai_fallback_attempts",
        "ai_fallback_invocation_claims",
        "ai_fallback_results",
        "ai_fallback_proposal_links",
        "ai_model_admission_decisions",
        "ai_model_compatibility_receipts",
        "ai_fallback_attempt_compatibility_receipts",
        "parser_outputs",
        "parser_proposal_confirmations",
        "transactions",
    }
    assert table_name in allowed
    row = s1ca._read_one(workspace, f'SELECT COUNT(*) FROM "{table_name}"')
    assert row is not None
    return int(row[0])


def fallback_response_arguments(
    claim: dict[str, Any],
    *,
    version: str,
    source_kind: str,
) -> dict[str, object]:
    body, arguments = ai_service_tests._response_body(claim)
    if source_kind == "receipt_ocr":
        _body, arguments = ai_service_tests._ocr_response_with_inherited_date(
            body,
            arguments,
            ambiguity_flags=[],
        )
    if version == "v2":
        model_call = claim["model_call"]
        provider, returned_model = str(model_call["model"]).split("/", maxsplit=1)
        arguments.update(
            {
                "returned_provider": provider,
                "returned_model": returned_model,
                "returned_agent_id": model_call["agentId"],
                "audit_purpose": model_call["purpose"],
            }
        )
    return arguments


def projection_from_claim(claim: dict[str, Any]) -> dict[str, Any]:
    model_call = claim["model_call"]
    messages = model_call["messages"]
    assert isinstance(messages, list) and messages
    content = messages[0]["content"]
    assert isinstance(content, str)
    projection = json.loads(content)
    assert isinstance(projection, dict)
    return projection


def assert_exclusive_gate_released(workspace: s1ca.ManagedBridgeWorkspace) -> None:
    """Model the provider wait between closed Core claim and result calls."""
    profile = workspace_access.managed_profile_for_workspace(workspace.workspace_path)
    assert profile is not None
    try:
        with exclusive_cut(profile, timeout_seconds=0):
            pass
    finally:
        profile.close()


__all__ = [
    "assert_exclusive_gate_released",
    "fallback_response_arguments",
    "projection_from_claim",
    "read_count",
    "request",
    "run",
    "seed_ai_source",
]
