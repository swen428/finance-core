"""Synthetic helpers for managed correction and receipt CLI tests.

These helpers prepare only disposable synthetic profiles.  They reuse the
existing S1C-A/B fixtures and real public Bridge / delivery CLI flows; no
correction authority or source-validation path is bypassed.
"""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest

from finance_core.openclaw_staging_bridge import (
    commands,
    delivery_receipt_cli,
    envelope,
    errors,
    workspace_access,
)
from finance_core.openclaw_staging_bridge.delivery_receipt_proof import (
    PROOF_VERSION,
    receipt_proof_sha256,
)
from finance_core.receipt_staging_runner.workspace import (
    generate_delivery_receipt_signing_key,
    load_delivery_receipt_signing_key,
)
from tests import test_s1c_a_managed_bridge_commands as s1ca
from tests import test_s1c_b_managed_bridge_commands as s1cb


@dataclass(frozen=True)
class PendingManagedDelivery:
    manifest: dict[str, object]
    signed_payload: dict[str, object]
    source_kind: str


@dataclass(frozen=True)
class PostedManagedTransaction:
    transaction_public_id: str
    delivery: PendingManagedDelivery
    observation_public_id: str


_DELIVERY_PROOF_FIELDS = (
    "workspace_path",
    "attempt_nonce",
    "capability",
    "delivery_material_version",
    "delivery_material_sha256",
    "provider_message_id",
    "receipt_token_sha256",
    "channel",
    "account_id",
    "conversation_id",
    "session_key",
    "source_identity_sha256",
)

_CORRECTION_LEDGER_TABLES = (
    "correction_targets",
    "correction_plans",
    "correction_versions",
    "correction_authorities",
    "correction_receipt_facts",
)

_SNAPSHOT_TABLES = {
    *_CORRECTION_LEDGER_TABLES,
    "d2_posting_review_delivery_observations",
    "d2_posting_review_delivery_activations",
    "financial_audit_events",
    "transactions",
}


def prepare_managed_delivery(
    workspace: s1ca.ManagedBridgeWorkspace,
    source_kind: str,
) -> PendingManagedDelivery:
    """Create and issue one pending text or personal-receipt posting attempt."""
    generate_delivery_receipt_signing_key(str(workspace.workspace_path / "runtime"))
    if source_kind == "text":
        proposal_id = s1ca._seed_proposal(workspace)
        admitted_message_id = s1ca.SEEDED_MESSAGE_ID
    elif source_kind == "receipt":
        proposal_id, _version, _content_hash = s1cb._seed_managed_receipt_proposal(
            workspace,
            confirm=False,
        )
        admitted_message_id = "141"
    else:
        raise AssertionError(f"unsupported synthetic posting source: {source_kind}")

    prepared = s1ca._run(
        workspace,
        s1cb._prepare_initial_request(
            workspace,
            proposal_id,
            admitted_message_id=admitted_message_id,
        ),
    )
    assert prepared.exit_code == errors.EXIT_OK, prepared.response
    review_id = str(prepared.response["result"]["review_public_id"])
    issue_request = s1ca._request(
        workspace,
        envelope.COMMAND_ISSUE_POSTING_REVIEW_ACTIONS,
        {
            **s1ca._context(workspace),
            "posting_review_public_id": review_id,
        },
        idempotency_key=commands.canonical_issue_posting_review_actions_key(review_id),
    )
    issued = s1ca._run(workspace, issue_request)
    assert issued.exit_code == errors.EXIT_OK, issued.response
    manifest = issued.response["result"]
    payload = s1cb._signed_delivery_receipt(
        workspace,
        manifest,
        provider_message_id="930",
    )
    return PendingManagedDelivery(manifest, payload, source_kind)


def execute_delivery_receipt(
    workspace: s1ca.ManagedBridgeWorkspace,
    payload: dict[str, object],
) -> tuple[int, dict[str, Any] | None, str]:
    """Invoke the real closed-stdin consumer and check each managed close."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    before_sessions = len(workspace.sessions)
    exit_code = delivery_receipt_cli.execute_stream(
        io.BytesIO(json.dumps(payload).encode("utf-8")),
        stdout,
        stderr,
    )
    opened = workspace.sessions[before_sessions:]
    for observation in opened:
        assert observation.workspace_path == workspace.workspace_path
        with pytest.raises(sqlite3.ProgrammingError):
            observation.connection.execute("SELECT 1")
    response = json.loads(stdout.getvalue()) if stdout.getvalue() else None
    assert response is None or isinstance(response, dict)
    return exit_code, response, stderr.getvalue()


def resign_delivery_receipt(
    workspace: s1ca.ManagedBridgeWorkspace,
    payload: dict[str, object],
    **changes: object,
) -> dict[str, object]:
    """Build a correctly HMAC-signed synthetic variant for source denial tests."""
    proof_fields: dict[str, object] = {name: payload[name] for name in _DELIVERY_PROOF_FIELDS}
    proof_fields.update(changes)
    signing_key = load_delivery_receipt_signing_key(str(workspace.workspace_path / "runtime"))
    return {
        **proof_fields,
        "receipt_proof_version": PROOF_VERSION,
        "receipt_proof_sha256": receipt_proof_sha256(
            signing_key=signing_key,
            **proof_fields,
        ),
    }


def confirm_managed_delivery(
    workspace: s1ca.ManagedBridgeWorkspace,
    delivery: PendingManagedDelivery,
) -> PostedManagedTransaction:
    """Consume one signed receipt, then finalize it through the public Bridge."""
    exit_code, receipt_result, stderr = execute_delivery_receipt(
        workspace,
        delivery.signed_payload,
    )
    assert exit_code == errors.EXIT_OK, stderr
    assert receipt_result is not None and receipt_result["status"] == "ok"
    observation_id = str(receipt_result["observation_public_id"])
    manifest = delivery.manifest
    control = next(
        item
        for item in manifest["controls"]
        if isinstance(item, dict) and item.get("action") == "confirm"
    )
    reference = str(control["callback_value"]).removeprefix("post:")
    callback_kind = (
        "initial_text" if delivery.source_kind == "text" else "pending_initial_ocr_receipt"
    )
    callback_id = f"s1c-b-{callback_kind}-confirm"
    confirm_request = s1ca._request(
        workspace,
        envelope.COMMAND_CONFIRM_AND_POST,
        {
            **s1ca._context(workspace),
            "short_reference": reference,
            "callback_id": callback_id,
            "callback_message_id": 930,
        },
        idempotency_key=commands.canonical_confirm_and_post_key(callback_id),
    )
    confirmed = s1ca._run(workspace, confirm_request)
    assert confirmed.exit_code == errors.EXIT_OK, confirmed.response
    result = confirmed.response["result"]
    assert result["state"] == "finalized"
    assert result["final_transaction_created"] is True
    assert result["transaction_public_id"]
    return PostedManagedTransaction(
        str(result["transaction_public_id"]),
        delivery,
        observation_id,
    )


def create_posted_managed_transaction(
    workspace: s1ca.ManagedBridgeWorkspace,
    source_kind: str,
) -> PostedManagedTransaction:
    return confirm_managed_delivery(workspace, prepare_managed_delivery(workspace, source_kind))


def table_counts(
    workspace: s1ca.ManagedBridgeWorkspace,
    names: tuple[str, ...] = tuple(sorted(_SNAPSHOT_TABLES)),
) -> dict[str, int]:
    assert set(names) <= _SNAPSHOT_TABLES
    result: dict[str, int] = {}
    for name in names:
        row = s1ca._read_one(workspace, f'SELECT COUNT(*) FROM "{name}"')
        assert row is not None
        result[name] = int(row[0])
    return result


def correction_ledger_counts(workspace: s1ca.ManagedBridgeWorkspace) -> dict[str, int]:
    return table_counts(workspace, _CORRECTION_LEDGER_TABLES)


def assert_correction_ledger_empty(workspace: s1ca.ManagedBridgeWorkspace) -> None:
    assert correction_ledger_counts(workspace) == dict.fromkeys(_CORRECTION_LEDGER_TABLES, 0)


def caller_connection_must_not_be_registered(
    workspace: s1ca.ManagedBridgeWorkspace,
) -> None:
    from finance_core.correction_adapters.policy import LocalPolicyError, load_policy_for_connection

    with workspace_access.workspace_database_session(
        workspace.workspace_path,
        operation_id="test-s1d1-unregistered-caller-connection",
    ) as conn:
        with pytest.raises(LocalPolicyError, match="factory-registered"):
            load_policy_for_connection(conn)


def acquire_exclusive_profile_gate(workspace: s1ca.ManagedBridgeWorkspace) -> None:
    from finance_core.profile_gate import exclusive_cut

    with exclusive_cut(workspace.witness, timeout_seconds=0):
        pass


__all__ = [
    "PendingManagedDelivery",
    "PostedManagedTransaction",
    "acquire_exclusive_profile_gate",
    "assert_correction_ledger_empty",
    "caller_connection_must_not_be_registered",
    "create_posted_managed_transaction",
    "correction_ledger_counts",
    "execute_delivery_receipt",
    "prepare_managed_delivery",
    "resign_delivery_receipt",
    "table_counts",
]
