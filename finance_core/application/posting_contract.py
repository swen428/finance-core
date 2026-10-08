"""Versioned independent posting commitments shared by owning services."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from finance_core.calculation.authoritative_snapshot import canonical_json_text

POSTING_REVIEW_SCHEMA = "finance-application-posting-review-v1"
POSTING_DECISION_SCHEMA = "finance-application-posting-human-decision-v1"


def posting_review_sha256(material: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_json_text({"schema": POSTING_REVIEW_SCHEMA, "review": dict(material)}).encode()
    ).hexdigest()


class PostingContractError(ValueError):
    """The additive independent posting schema is unavailable or changed."""


_SCHEMA_HASH = "93ca381fed6d05d94d197bf714a2cf3633e2803c85a79b1d2efe1eba592ee614"


def require_posting_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'application_posting_%' "
        "OR name LIKE 'application_conditional_%' ORDER BY type,name"
    ).fetchall()
    digest = hashlib.sha256(
        json.dumps([tuple(row) for row in rows], separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if digest != _SCHEMA_HASH:
        raise PostingContractError("Independent posting migration 056 is absent or changed")
