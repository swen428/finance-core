"""Typed durable, transport-neutral human amendment evidence contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from finance_core.application.admission import TrustedBinding, VerifiedSource
from finance_core.calculation.authoritative_snapshot import canonical_json_text

AMENDMENT_SCHEMA = "finance-application-human-amendment-v1"
AMENDMENT_REVIEW_SCHEMA = "finance-application-amendment-review-v1"


class AmendmentError(ValueError):
    """An independent amendment cannot be truthfully authorized or verified."""


@dataclass(frozen=True)
class AmendmentBinding:
    binding: TrustedBinding
    amendment_namespace: str
    amendment_key_id: str


@dataclass(frozen=True)
class ExpectedHumanAmendment:
    binding: AmendmentBinding
    source: VerifiedSource
    review_id: str
    review_projection: Mapping[str, object]
    review_projection_hash: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    checked_at: int
    accepted_amendment_id: str | None = None


@dataclass(frozen=True)
class VerifiedHumanAmendment:
    schema: str
    namespace: str
    key_id: str
    instance_id: str
    human_principal_id: str
    evidence_id: str
    decision_id: str
    source_evidence_id: str
    source_evidence_digest: str
    intake_public_id: str
    source_event_id: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    review_id: str
    review_projection_hash: str
    display_id: str
    display_evidence_digest: str
    reply_evidence_digest: str
    reply_to_display_id: str
    action: str
    patch: Mapping[str, object]
    patch_digest: str
    observed_at: int
    issued_at: int
    expires_at: int
    consumed: bool
    revoked: bool
    evidence_digest: str


@dataclass(frozen=True)
class AmendmentReview:
    review_id: str
    review_hash: str
    projection: Mapping[str, object]
    expires_at: int


@dataclass(frozen=True)
class AmendmentResult:
    amendment_id: str
    proposal_public_id: str
    proposal_version: int
    effective_content_hash: str
    publication_kind: str
    publication_public_id: str
    is_current: bool


class HumanAmendmentAuthority(Protocol):
    def verify_persisted(
        self,
        connection: sqlite3.Connection,
        amendment_evidence_id: str,
        expected: ExpectedHumanAmendment,
    ) -> VerifiedHumanAmendment:
        """Reverify durable acknowledged full display, direct reply and signature.

        Reads only the supplied snapshot. Historical context verifies retained
        accepted-time proof, including revocation, without fresh-card checks.
        Never writes, consumes, uses a request patch, or calls a transport.
        """
        ...


def amendment_patch_sha256(patch: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_json_text({"schema": AMENDMENT_SCHEMA, "patch": dict(patch)}).encode()
    ).hexdigest()


def amendment_review_sha256(projection: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_json_text(
            {"schema": AMENDMENT_REVIEW_SCHEMA, "projection": dict(projection)}
        ).encode()
    ).hexdigest()


_AMENDMENT_SCHEMA_HASH = "a63f28298b86bacbca3b42454d759276ec397a267602dd50f87877ffb0961c02"


def require_amendment_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name LIKE "
        "'application_amendment_%' OR name LIKE 'parser_text_amendment_%' "
        "OR name='trg_ai_fallback_raw_intake_no_lineage_escape' ORDER BY type,name"
    ).fetchall()
    digest = hashlib.sha256(
        json.dumps([tuple(row) for row in rows], separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if digest != _AMENDMENT_SCHEMA_HASH:
        raise AmendmentError("Independent amendment migration 057 is absent or changed")
