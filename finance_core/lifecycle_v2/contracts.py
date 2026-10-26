"""Versioned offline lifecycle evidence contracts; no public boolean authorizations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
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
    current_fact_sha256: str | None = None
    current_economic_event_id: str | None = None


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
    logical_database_sha256: str = field(kw_only=True)
    source_binding_sha256: str = field(kw_only=True)
    format: str = field(default="lifecycle-snapshot-cut-v3", kw_only=True)
    logical_digest_format: str = field(default="core-logical-database-v1", kw_only=True)

    def __post_init__(self) -> None:
        if (
            self.format != "lifecycle-snapshot-cut-v3"
            or self.logical_digest_format != "core-logical-database-v1"
        ):
            raise LifecycleError("Unsupported snapshot cut format")
        for value in (
            self.snapshot_id,
            self.instance_id,
            self.generation,
            self.core_identity,
            self.migration_identity,
        ):
            if not isinstance(value, str) or not value:
                raise LifecycleError("Snapshot cut requires exact identity strings")
        if type(self.commit_seq) is not int or self.commit_seq < 0:
            raise LifecycleError("Snapshot cut sequence must be a nonnegative integer")
        for value in (
            self.database_sha256,
            self.application_schema_digest,
            self.logical_database_sha256,
            self.source_binding_sha256,
        ):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise LifecycleError("Snapshot cut requires complete lowercase SHA-256 values")
        if type(self.facts) is not tuple:
            raise LifecycleError("Snapshot cut facts must be an ordered tuple")
        previous = None
        for fact in self.facts:
            if (
                type(fact) is not tuple
                or len(fact) != 3
                or not isinstance(fact[0], str)
                or not fact[0]
                or type(fact[1]) is not int
                or fact[1] < 1
                or not isinstance(fact[2], str)
                or re.fullmatch(r"[0-9a-f]{64}", fact[2]) is None
                or (previous is not None and fact[0] <= previous)
            ):
                raise LifecycleError("Snapshot cut facts must be unique, ordered exact records")
            previous = fact[0]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SnapshotCut":
        """Strict JSON decode; historical watermarks cannot become v3 cuts."""
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise LifecycleError("Snapshot cut field set differs from v3")
        data = dict(value)
        facts = data.get("facts")
        if type(facts) is not list or any(type(item) is not list for item in facts):
            raise LifecycleError("Snapshot cut JSON facts must be arrays")
        data["facts"] = tuple(tuple(item) for item in facts)
        return cls(**data)


def cut_ref(cut: SnapshotCut) -> dict[str, str]:
    if type(cut) is not SnapshotCut:
        raise LifecycleError("Exact v3 snapshot cut required")
    return {
        "format": "lifecycle-cut-ref-v1",
        "cut_sha256": digest(asdict(cut)),
        "snapshot_id": cut.snapshot_id,
        "source_binding_sha256": cut.source_binding_sha256,
        "logical_digest_format": cut.logical_digest_format,
        "logical_database_sha256": cut.logical_database_sha256,
        "database_sha256": cut.database_sha256,
    }
