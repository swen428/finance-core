"""Versioned offline lifecycle evidence contracts; no public boolean authorizations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol


class LifecycleError(ValueError):
    """An operation lacks current, exact evidence or violates lifecycle state."""


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class RuntimeBinding:
    instance_id: str
    generation: str
    principal_id: str
    client_id: str
    environment: str = "offline-synthetic"

    def __post_init__(self):
        if self.environment != "offline-synthetic" or any(
            not isinstance(v, str) or not v or len(v) > 256
            for v in (self.instance_id, self.generation, self.principal_id, self.client_id)
        ):
            raise LifecycleError("Only exact offline-synthetic bindings are admitted")


@dataclass(frozen=True)
class VerifiedSource:
    evidence_id: str
    principal_id: str
    client_id: str
    source_key: str
    raw_input: str
    original_sha256: str
    source_timestamp: int = 0
    timezone: str = "Asia/Singapore"


@dataclass(frozen=True)
class ExpectedInvocation:
    binding: RuntimeBinding
    source_id: str
    source_sha256: str
    expected_version: int
    projection: dict[str, Any]
    record_id: str | None = None
    reply_evidence_id: str | None = None
    current_proposal_id: str | None = None
    current_review_id: str | None = None
    correction_reason: str | None = None


@dataclass(frozen=True)
class VerifiedObservation:
    evidence_id: str
    source_id: str
    principal_id: str
    raw_response: str
    request_sha256: str
    reply_text: str = ""


@dataclass(frozen=True)
class ExpectedDisplay:
    binding: RuntimeBinding
    review_id: str
    review_sha256: str


@dataclass(frozen=True)
class VerifiedDelivery:
    evidence_id: str
    review_id: str
    display_sha256: str
    principal_id: str
    client_id: str


@dataclass(frozen=True)
class ExpectedAction:
    binding: RuntimeBinding
    review_id: str
    review_sha256: str
    delivery_evidence_id: str


@dataclass(frozen=True)
class VerifiedAction:
    evidence_id: str
    review_id: str
    review_sha256: str
    delivery_evidence_id: str
    principal_id: str
    client_id: str
    action: str = "confirm"


class EvidencePort(Protocol):
    def source(self, conn, evidence_id: str) -> VerifiedSource: ...
    def provider(
        self, conn, evidence_id: str, expected: ExpectedInvocation
    ) -> VerifiedObservation: ...
    def delivery(self, conn, evidence_id: str, expected: ExpectedDisplay) -> VerifiedDelivery: ...
    def action(self, conn, evidence_id: str, expected: ExpectedAction) -> VerifiedAction: ...


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    source_sha256: str


@dataclass(frozen=True)
class ProposalRef:
    proposal_id: str
    source_id: str
    version: int
    proposal_sha256: str


@dataclass(frozen=True)
class Review:
    review_id: str
    proposal_id: str
    expected_version: int
    record_id: str | None
    values: dict[str, Any]
    review_sha256: str
    delivery_evidence_id: str | None = None
    before_values: dict[str, Any] | None = None
    correction_reason: str | None = None


@dataclass(frozen=True)
class CommitReceipt:
    record_id: str
    version: int
    fact_sha256: str
    commit_seq: int
    status: str = "LOCAL_POSTED"


@dataclass(frozen=True)
class SnapshotCut:
    snapshot_id: str
    instance_id: str
    generation: str
    commit_seq: int
    facts: tuple[tuple[str, int, str], ...]
    database_sha256: str
    core_identity: str = ""
    migration_identity: str = ""
    application_schema_digest: str = ""
