"""Shared deterministic support for OpenClaw staging bridge slice tests.

All fixtures here are synthetic and temporary: staging workspaces live under
pytest tmp_path, databases are staging-authorised temp files, and Telegram
identities are redacted stand-ins.  Nothing in this module touches the live
database, credentials, or the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from migrated_staging_snapshot_v1 import (
    MigratedStagingTemplate,
    clone_migrated_staging_template,
)

from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    ReceiptOcrSource,
)
from finance_core.openclaw_staging_bridge import cli as bridge_cli
from finance_core.openclaw_staging_bridge import envelope as bridge_envelope
from finance_core.receipt_staging_runner.models import parse_runner_manifest
from finance_core.receipt_staging_runner.workspace import create_runner_workspace
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import create_staging_database

JPEG_BYTES = b"\xff\xd8\xff" + b"bridge-receipt-jpeg-evidence"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"bridge-receipt-png-evidence"

# Private-DM structural identity: in the Bot API model a private chat's id
# equals the user's id, so the synthetic chat and sender identities match.
SYNTHETIC_CHAT_ID = 111
SYNTHETIC_SENDER_ID = 111


def canonical_capture_key(*, chat_id: int = SYNTHETIC_CHAT_ID, message_id: int) -> str:
    """Canonical capture idempotency key: the durable Telegram message identity."""
    return f"raw-intake:telegram:{chat_id}:{message_id}"


def canonical_propose_key(intake_public_id: str) -> str:
    """Canonical propose idempotency key: the intake this propose authorizes."""
    return f"bridge-propose:{intake_public_id}"


def canonical_decision_key(*, action: str, proposal_public_id: str) -> str:
    """Canonical decision idempotency key: the proposal plus the action."""
    return f"bridge-{action}:{proposal_public_id}"


def canonical_edit_key(*, proposal_public_id: str, version: int, content_hash: str) -> str:
    """Canonical edit idempotency key: proposal plus pre-edit version/hash."""
    return f"bridge-edit:{proposal_public_id}:v{version}:{content_hash}"


def canonical_human_action_issuance_key(batch_id: str) -> str:
    return f"bridge-human-action-issue:{batch_id}"


def canonical_human_action_redemption_key(callback_id: str) -> str:
    digest = hashlib.sha256(callback_id.encode("utf-8")).hexdigest()[:32]
    return f"bridge-human-action-redeem:{digest}"


def canonical_prepare_posting_review_key(card_generation_public_id: str) -> str:
    return f"bridge-d2-prepare:{card_generation_public_id}"


def canonical_issue_posting_review_actions_key(review_public_id: str) -> str:
    return f"bridge-d2-issue:{review_public_id}"


def canonical_confirm_and_post_key(callback_id: str) -> str:
    digest = hashlib.sha256(callback_id.encode("utf-8")).hexdigest()[:32]
    return f"bridge-d2-confirm:{digest}"


def canonical_resume_posting_key(attempt_public_id: str) -> str:
    return f"bridge-d2-resume:{attempt_public_id}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_manifest_bytes(
    *,
    workspace_identity: str = "ws_bridge_test",
    operator_actor_id: str = "person_owner",
) -> bytes:
    return json.dumps(
        {
            "schema_version": "v1",
            "workspace_identity": workspace_identity,
            "operator_actor_id": operator_actor_id,
            "participants": [
                {
                    "public_id": "ptcp_owner",
                    "display_name": "Owner",
                    "is_self": True,
                }
            ],
        },
        sort_keys=True,
    ).encode("utf-8")


def parse_runner_manifest_helper() -> "object":
    """Validated manifest matching create_bridge_workspace defaults."""
    return parse_runner_manifest(make_manifest_bytes())


@dataclass(frozen=True)
class BridgeWorkspace:
    workspace_path: Path
    database_path: Path


def create_bridge_workspace(tmp_path: Path, *, name: str | None = None) -> BridgeWorkspace:
    """Create a runner workspace plus a migrated staging database inside it."""
    suffix = name or uuid.uuid4().hex[:8]
    workspace_path = str((tmp_path / f"workspace_{suffix}").resolve())
    manifest = parse_runner_manifest(make_manifest_bytes())
    workspace = create_runner_workspace(workspace_path, manifest)
    conn = create_staging_database(workspace.database_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    conn.close()
    return BridgeWorkspace(
        workspace_path=Path(workspace.workspace_path),
        database_path=Path(workspace.database_path),
    )


def create_snapshot_bridge_workspace(
    tmp_path: Path,
    *,
    template: MigratedStagingTemplate,
    name: str | None = None,
) -> BridgeWorkspace:
    """Create a runner workspace backed by a fresh migrated snapshot clone."""
    suffix = name or uuid.uuid4().hex[:8]
    workspace_path = str((tmp_path / f"workspace_{suffix}").resolve())
    manifest = parse_runner_manifest(make_manifest_bytes())
    workspace = create_runner_workspace(workspace_path, manifest)
    conn = clone_migrated_staging_template(template, workspace.database_path)
    conn.close()
    return BridgeWorkspace(
        workspace_path=Path(workspace.workspace_path),
        database_path=Path(workspace.database_path),
    )


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def make_request(
    command: str,
    arguments: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    envelope_version: str = "v1",
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "envelope_version": envelope_version,
        "command": command,
        "request_id": request_id or new_request_id(),
        "arguments": arguments,
    }
    if idempotency_key is not None:
        request["idempotency_key"] = idempotency_key
    if extra_fields:
        request.update(extra_fields)
    return request


@dataclass(frozen=True)
class CliOutcome:
    exit_code: int
    response: dict[str, Any]
    stderr: str


def run_cli(
    request: dict[str, Any] | bytes,
    *,
    deadline_seconds: float = 30.0,
    raw_override: bytes | None = None,
) -> CliOutcome:
    """Run one in-process CLI exchange through the real execute_stream path."""
    if raw_override is not None:
        raw = raw_override
    elif isinstance(request, bytes):
        raw = request
    else:
        raw = json.dumps(request).encode("utf-8")
    stdin = io.BytesIO(raw)
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = bridge_cli.execute_stream(stdin, stdout, stderr, deadline_seconds=deadline_seconds)
    rendered = stdout.getvalue()
    lines = [line for line in rendered.splitlines() if line.strip()]
    assert len(lines) <= 1, f"CLI emitted more than one stdout line: {rendered!r}"
    response = json.loads(lines[0]) if lines else {}
    return CliOutcome(exit_code=exit_code, response=response, stderr=stderr.getvalue())


def telegram_text_update(
    text: str,
    *,
    update_id: int = 1,
    message_id: int = 10,
    chat_id: int = SYNTHETIC_CHAT_ID,
    sender_id: int | None = SYNTHETIC_SENDER_ID,
    date: int = 1_750_000_000,
    chat_type: str | None = "private",
) -> dict[str, Any]:
    chat: dict[str, Any] = {"id": chat_id}
    if chat_type is not None:
        chat["type"] = chat_type
    message: dict[str, Any] = {
        "message_id": message_id,
        "chat": chat,
        "date": date,
        "text": text,
    }
    if sender_id is not None:
        message["from"] = {"id": sender_id}
    return {"update_id": update_id, "message": message}


def capture_text_arguments(workspace: BridgeWorkspace, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "kind": "text",
        "telegram_update": update,
    }


def capture_receipt_arguments(
    workspace: BridgeWorkspace,
    *,
    handoff_filename: str,
    update_id: int = 1,
    message_id: int = 20,
    chat_id: int = SYNTHETIC_CHAT_ID,
    caption: str | None = None,
    declared_mime_type: str | None = None,
    original_filename: str | None = None,
    sender_id: int | None = SYNTHETIC_SENDER_ID,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "workspace_path": str(workspace.workspace_path),
        "kind": "receipt_image",
        "handoff_filename": handoff_filename,
        "telegram_update_id": update_id,
        "telegram_message_id": message_id,
        "telegram_chat_id": chat_id,
    }
    if sender_id is not None:
        arguments["sender_id"] = sender_id
    if caption is not None:
        arguments["caption"] = caption
    if declared_mime_type is not None:
        arguments["declared_mime_type"] = declared_mime_type
    if original_filename is not None:
        arguments["original_filename"] = original_filename
    return arguments


def write_handoff_file(workspace: BridgeWorkspace, filename: str, content: bytes) -> Path:
    handoff_dir = workspace.workspace_path / "handoff"
    handoff_dir.mkdir(mode=0o700, exist_ok=True)
    path = handoff_dir / filename
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def open_database(workspace: BridgeWorkspace) -> sqlite3.Connection:
    conn = sqlite3.connect(str(workspace.database_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def count_final_facts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count canonical final-fact tables the bridge must never write."""
    counts: dict[str, int] = {}
    for table in (
        "transactions",
        "receipt_fact_sets",
        "receipt_item_allocation_fact_sets",
        "calculation_snapshots",
        "parser_proposal_conversion_audit",
        "receipt_proposal_conversions",
    ):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None:
            counts[table] = 0
            continue
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


class FakeOcrEngine:
    """Deterministic ReceiptOcrEngine stand-in for CI-safe receipt tests."""

    def __init__(
        self,
        *,
        blocks: tuple[ReceiptOcrBlock, ...] | None = None,
        status: ReceiptOcrExtractionStatus = ReceiptOcrExtractionStatus.SUCCEEDED,
        outcome_code: str = "ok",
        raise_on_extract: Exception | None = None,
    ) -> None:
        self._identity = ReceiptOcrEngineIdentity(
            name="fake_bridge_ocr",
            version="1.0",
            binary_sha256=sha256_hex(b"fake-bridge-binary"),
            configuration_hash=sha256_hex(b"fake-bridge-config"),
        )
        self._result = ReceiptOcrEngineResult(
            status=status,
            blocks=blocks if blocks is not None else (_block(0, "TOTAL"), _block(1, "12.34")),
            outcome_code=outcome_code,
        )
        self.raise_on_extract = raise_on_extract
        self.calls = 0

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self._identity

    def extract(
        self,
        source: ReceiptOcrSource,
        *,
        limits: ReceiptOcrLimits,
        deadline: float,
    ) -> ReceiptOcrEngineResult:
        self.calls += 1
        if self.raise_on_extract is not None:
            raise self.raise_on_extract
        assert deadline > time.monotonic() - 5
        return self._result


def _block(sequence: int, text: str) -> ReceiptOcrBlock:
    return ReceiptOcrBlock(
        sequence_index=sequence,
        page_index=0,
        engine_block_index=0,
        engine_paragraph_index=0,
        engine_line_index=0,
        engine_word_index=sequence,
        text=text,
        left=10 + sequence * 40,
        top=20,
        width=30,
        height=10,
        page_width=800,
        page_height=1200,
        confidence_scaled=9750,
    )


def assert_no_sensitive_material(rendered: str, workspace: BridgeWorkspace) -> None:
    """Bounded review payloads never leak absolute paths, key material, or DB bytes."""
    assert str(workspace.workspace_path) not in rendered
    key_path = workspace.workspace_path / "runtime" / "callback_signing.key"
    if key_path.exists():
        key_hex = key_path.read_bytes().hex()
        assert key_hex not in rendered


def health_arguments(workspace: BridgeWorkspace) -> dict[str, Any]:
    return {"workspace_path": str(workspace.workspace_path)}


def max_request_envelope(command: str) -> bytes:
    """Build an envelope just above the stdin byte limit."""
    padding = "x" * (bridge_envelope.MAX_REQUEST_BYTES)
    return json.dumps(
        make_request(command, {"workspace_path": padding}, idempotency_key="key")
    ).encode("utf-8")
