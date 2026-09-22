"""Platform-independent review with real synthetic persistence and adapter parity."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.application import review
from finance_core.openclaw_staging_bridge import callback_tokens, commands, errors, ocr_boundary

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace(tmp_path: Path, migrated_staging_snapshot_template) -> support.BridgeWorkspace:
    return support.create_snapshot_bridge_workspace(
        tmp_path, template=migrated_staging_snapshot_template
    )


def capture(workspace: support.BridgeWorkspace, *, receipt: bool = False) -> str:
    if receipt:
        support.write_handoff_file(workspace, "receipt.jpg", support.JPEG_BYTES)
        arguments = support.capture_receipt_arguments(workspace, handoff_filename="receipt.jpg")
        message_id = 20
    else:
        arguments = support.capture_text_arguments(
            workspace, support.telegram_text_update("lunch 12.50")
        )
        message_id = 10
    result = support.run_cli(
        support.make_request(
            "capture",
            arguments,
            idempotency_key=support.canonical_capture_key(message_id=message_id),
        )
    )
    assert result.exit_code == errors.EXIT_OK
    if not receipt:
        return result.response["result"]["proposal_public_id"]
    intake = result.response["result"]["intake_public_id"]
    result = support.run_cli(
        support.make_request(
            "propose",
            {"workspace_path": str(workspace.workspace_path), "intake_public_id": intake},
            idempotency_key=support.canonical_propose_key(intake),
        )
    )
    assert result.exit_code == errors.EXIT_OK
    return result.response["result"]["proposal_public_id"]


def patch_payload(workspace: support.BridgeWorkspace, public_id: str, changes: dict) -> None:
    with support.open_database(workspace) as conn:
        row = conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE public_id=?", (public_id,)
        ).fetchone()
        payload = {**json.loads(row[0]), **changes}
        rendered = json.dumps(payload)
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload=?, normalized_payload=? WHERE public_id=?",
            (rendered, rendered, public_id),
        )


def adapter_review(workspace: support.BridgeWorkspace, public_id: str):
    return support.run_cli(
        support.make_request(
            "get_review",
            {"workspace_path": str(workspace.workspace_path), "proposal_public_id": public_id},
        )
    )


@pytest.mark.parametrize("receipt", [False, True])
def test_direct_read_only_review_matches_existing_adapter_and_token_binding(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch, receipt: bool
) -> None:
    monkeypatch.setattr(ocr_boundary, "engine_factory", lambda _workspace: support.FakeOcrEngine())
    public_id = capture(workspace, receipt=receipt)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 23, tzinfo=UTC)

    monkeypatch.setattr(commands, "datetime", FixedClock)
    key = workspace.workspace_path / "runtime/callback_signing.key"
    key.write_bytes(b"S" * 32)
    key.chmod(0o600)
    adapter = adapter_review(workspace, public_id)
    assert adapter.exit_code == errors.EXIT_OK
    expected = adapter.response["result"].copy()
    tokens = expected.pop("callback_tokens")
    key.unlink()
    # SQLite enforces no writes; there is no runtime/workspace/key argument.
    conn = sqlite3.connect(f"file:{workspace.database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.serialize()
        actual = review.get_proposal_review(conn, public_id)
        assert actual == expected
        assert conn.serialize() == before
        assert conn.total_changes == 0 and not conn.in_transaction
    finally:
        conn.close()
    assert not key.exists()
    assert actual["final_transaction_created"] is False
    assert tokens == callback_tokens.issue_callback_tokens(
        b"S" * 32,
        proposal_public_id=public_id,
        version=actual["proposal_version"],
        content_hash=actual["effective_content_hash"],
        expiry=int(FixedClock.now().timestamp()) + commands._DEFAULT_TOKEN_TTL_SECONDS,
    )


@pytest.mark.parametrize(
    "field", ["amount", "currency", "transaction_date", "merchant", "description", "account"]
)
@pytest.mark.parametrize("value", [" ", True, {"value": "12.50"}])
def test_direct_review_refuses_untruthful_hash_bound_fields(
    workspace: support.BridgeWorkspace, field: str, value: object
) -> None:
    public_id = capture(workspace)
    patch_payload(workspace, public_id, {field: value})
    conn = support.open_database(workspace)
    try:
        with pytest.raises(review.ReviewUnavailableError):
            review.get_proposal_review(conn, public_id)
        assert conn.total_changes == 0
    finally:
        conn.close()
    outcome = adapter_review(workspace, public_id)
    assert outcome.exit_code == errors.EXIT_AUTHORITY_REFUSED
    assert outcome.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE


def test_missing_oversized_classification_and_unknown_proposal_contracts(
    workspace: support.BridgeWorkspace,
) -> None:
    public_id = capture(workspace)
    patch_payload(
        workspace,
        public_id,
        {
            "amount": None,
            "currency": None,
            "date": None,
            "transaction_date": None,
            "merchant": "M" * 1025,
            "account": "A" * 1025,
            "transaction_type": "personal_expense",
            "participants": ["another"],
        },
    )
    conn = support.open_database(workspace)
    try:
        result = review.get_proposal_review(conn, public_id)
        assert result["amount"] is None and result["currency"] is None
        assert result["merchant"] == "M" * 1024 and result["account"] == "A" * 1024
        assert result["classification"] == "unknown"
        assert {
            "missing_amount",
            "missing_currency",
            "missing_date",
            "oversized_display_field",
            "unknown_classification",
        } <= set(result["ambiguity_indicators"])
        with pytest.raises(review.ReviewNotFoundError):
            review.get_proposal_review(conn, "proposal_missing")
    finally:
        conn.close()
    patch_payload(workspace, public_id, {"amount": "9" * 1025})
    conn = support.open_database(workspace)
    try:
        with pytest.raises(review.ReviewUnavailableError, match="oversized 'amount'"):
            review.get_proposal_review(conn, public_id)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,expected",
    [("account", errors.PROPOSAL_UNAVAILABLE), ("merchant", errors.CALLBACK_KEY_MISSING)],
)
def test_adapter_preserves_validation_order_around_token_key(
    workspace: support.BridgeWorkspace, field: str, expected: str
) -> None:
    public_id = capture(workspace)
    patch_payload(workspace, public_id, {field: True})
    key = workspace.workspace_path / "runtime/callback_signing.key"
    key.unlink()
    outcome = adapter_review(workspace, public_id)
    assert outcome.response["error"]["code"] == expected
    assert not key.exists()


def test_adapter_uses_the_application_stages(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = capture(workspace)
    calls = []
    prepare, project = review.prepare_proposal_review, review.project_proposal_review

    def observed_prepare(conn, proposal_public_id):
        calls.append("prepare")
        return prepare(conn, proposal_public_id)

    def observed_project(conn, prepared):
        calls.append("project")
        return project(conn, prepared)

    monkeypatch.setattr(review, "prepare_proposal_review", observed_prepare)
    monkeypatch.setattr(review, "project_proposal_review", observed_project)
    assert adapter_review(workspace, public_id).exit_code == errors.EXIT_OK
    assert calls == ["prepare", "project"]


def test_cold_application_import_and_real_read_cannot_load_platform(
    workspace: support.BridgeWorkspace, tmp_path: Path
) -> None:
    public_id = capture(workspace)
    (workspace.workspace_path / "runtime/callback_signing.key").unlink()
    code = """
import importlib.abc, json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
blocked = (
    "finance_core.openclaw_staging_bridge", "finance_core.telegram_source_context",
    "finance_core.intake.telegram_", "finance_core.intake.macos_",
    "finance_core.receipt_staging_runner",
)
class NoPlatform(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(blocked):
            raise AssertionError("platform import: " + fullname)
sys.meta_path.insert(0, NoPlatform())
from finance_core.application.review import get_proposal_review
conn = sqlite3.connect("file:" + sys.argv[2] + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
print(json.dumps(get_proposal_review(conn, sys.argv[3])))
assert conn.total_changes == 0
assert not any(name.startswith(blocked) for name in sys.modules)
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FINANCE_", "OPENCLAW_", "TELEGRAM_"))
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), str(workspace.database_path), public_id],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["proposal_public_id"] == public_id


@pytest.mark.parametrize(
    "package,expected_digest",
    [
        ("finance_core.intake", "9a085e8a0f9654b71a353d5351cb71da6a163835a2bf797735013e7c7862001a"),
        (
            "finance_core.parser_proposals",
            "71c44a139372fa4004620148c7dbed75d233c1f0e2e05fa938636f09c238929e",
        ),
    ],
)
def test_compatibility_exports_retain_baseline_names_and_object_identities(
    package: str, expected_digest: str
) -> None:
    module = importlib.import_module(package)
    material = {"exports": module._LAZY_EXPORTS, "all": module.__all__}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    assert digest == expected_digest
    for name, (target, attribute) in module._LAZY_EXPORTS.items():
        assert getattr(module, name) is getattr(importlib.import_module(target), attribute)
    assert set(module.__all__) <= set(dir(module))
    with pytest.raises(AttributeError):
        getattr(module, "undefined_application_export")


def test_real_ai_lineage_review_is_read_only_and_suppresses_ambiguous_tokens(
    tmp_path: Path,
) -> None:
    import test_ai_fallback_service_v1 as ai_cases

    from finance_core.parser_proposals.ai_fallback import record_ai_fallback_result

    workspace, conn, attempt, claim = ai_cases._prepared_claim(tmp_path)
    try:
        _body, arguments = ai_cases._response_body(claim)
        created = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        assert created["result_status"] == "proposal_created"
        conn.execute("PRAGMA query_only = ON")
        before = conn.serialize()
        actual = review.get_proposal_review(conn, created["proposal_public_id"])
        assert conn.serialize() == before
        assert actual["proposal_origin"] == "ai_fallback"
        assert actual["ai_source_kind"] == "telegram_raw_text"
        assert "missing_date" in actual["ambiguity_indicators"]
        assert actual["confirm_available"] is False
        adapter = adapter_review(workspace, created["proposal_public_id"])
        assert adapter.exit_code == errors.EXIT_OK
        result = adapter.response["result"].copy()
        assert result.pop("callback_tokens") is None
        assert result == actual
    finally:
        conn.close()


def test_source_refusal_precedes_key_and_display_validation(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finance_core.parser_proposals.ai_fallback import AiFallbackServiceError

    public_id = capture(workspace)
    patch_payload(workspace, public_id, {"account": True, "merchant": True})
    (workspace.workspace_path / "runtime/callback_signing.key").unlink()

    def refuse_source(*args, **kwargs):
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "source lineage refused")

    monkeypatch.setattr(review, "verify_ai_fallback_child", refuse_source)
    outcome = adapter_review(workspace, public_id)
    assert outcome.response["error"]["code"] == errors.PROPOSAL_UNAVAILABLE
    assert outcome.response["error"]["message"] == "source lineage refused"
