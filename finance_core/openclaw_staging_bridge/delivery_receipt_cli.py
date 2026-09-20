"""Closed stdin consumer for one host-owned D2 Telegram delivery receipt.

This is deliberately separate from the public bridge v1 command envelope.  The
pinned OpenClaw host invokes it only while consuming its opaque, single-use
receipt.  It accepts no financial values and returns only the durable delivery
observation identity.
"""

from __future__ import annotations

import json
import sys
from typing import BinaryIO, TextIO

from finance_core import posting_authority
from finance_core.openclaw_staging_bridge import workspace_access

MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_024

_FIELDS = frozenset(
    {
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
    }
)


def _emit(stdout: TextIO, payload: dict[str, object]) -> int:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return 8
    stdout.write(encoded + "\n")
    stdout.flush()
    return 0


def execute_stream(stdin: BinaryIO, stdout: TextIO, stderr: TextIO) -> int:
    try:
        raw = stdin.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("oversized")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or frozenset(payload) != _FIELDS:
            raise ValueError("shape")
        workspace = workspace_access.validate_workspace_path(payload["workspace_path"])
        workspace_access.verify_workspace_structure(workspace)
        conn = workspace_access.open_workspace_database(workspace)
        try:
            observation_public_id = posting_authority.record_posting_review_delivery(
                conn,
                attempt_nonce=payload["attempt_nonce"],
                capability=payload["capability"],
                delivery_material_version=payload["delivery_material_version"],
                finance_delivery_material_sha256=payload["delivery_material_sha256"],
                provider_message_id=payload["provider_message_id"],
                receipt_token_sha256=payload["receipt_token_sha256"],
                channel=payload["channel"],
                account_id=payload["account_id"],
                conversation_id=payload["conversation_id"],
                session_key=payload["session_key"],
                source_identity_sha256=payload["source_identity_sha256"],
            )
        finally:
            conn.close()
        return _emit(
            stdout,
            {"observation_public_id": observation_public_id, "status": "ok"},
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        stderr.write("delivery receipt request refused\n")
        return 6
    except posting_authority.PostingAuthorityError:
        stderr.write("delivery receipt authority refused\n")
        return 6
    except Exception:
        stderr.write("delivery receipt consumer failed\n")
        return 8


def main() -> int:
    return execute_stream(sys.stdin.buffer, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
