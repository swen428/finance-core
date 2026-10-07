"""Read-only neutral source and human-decision inspection.

Trusted composition supplies the connection, roots, ports and clock once.
Operational callers supply references only. An inspection never confirms,
consumes a decision or authorizes posting. A future guarded posting service
must reverify and atomically consume durable evidence inside its own guarded write transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from finance_core.application.review import get_proposal_review, review_snapshot

SOURCE_SCHEMA = "finance-application-source-v1"
DECISION_SCHEMA = "finance-application-human-decision-v1"
REVIEW_PROJECTION_SCHEMA = "finance-application-review-projection-v1"


class AdmissionError(ValueError):
    """Durable source or decision evidence could not be verified."""


@dataclass(frozen=True)
class TrustedBinding:
    instance_id: str
    human_principal_id: str
    submission_client_id: str
    source_namespace: str
    source_key_id: str
    decision_namespace: str
    decision_key_id: str


@dataclass(frozen=True)
class VerifiedSource:
    schema: str
    namespace: str
    key_id: str
    instance_id: str
    submission_client_id: str
    evidence_id: str
    intake_public_id: str
    source_event_id: str
    source_content_hash: str
    evidence_digest: str
    source_occurred_at: int
    received_at: int


@dataclass(frozen=True)
class ExpectedHumanDecision:
    binding: TrustedBinding
    source: VerifiedSource
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    review_projection_hash: str
    checked_at: int


@dataclass(frozen=True)
class VerifiedHumanDecision:
    schema: str
    namespace: str
    key_id: str
    instance_id: str
    human_principal_id: str
    decision_id: str
    source_evidence_id: str
    source_evidence_digest: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    review_projection_hash: str
    display_id: str
    display_evidence_digest: str
    action: str
    issued_at: int
    expires_at: int
    consumed: bool
    decision_digest: str


@dataclass(frozen=True)
class SourceInspection:
    """Observation of source evidence, never a capture or posting capability."""

    intake_public_id: str
    evidence_id: str
    evidence_digest: str
    checked_at: int


@dataclass(frozen=True)
class DecisionInspection:
    """Observation only; no confirmation, consumption or posting authority."""

    decision_id: str
    decision_digest: str
    source_evidence_id: str
    source_evidence_digest: str
    display_id: str
    display_evidence_digest: str
    review_projection_hash: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    action: str
    checked_at: int


class SourceVerifier(Protocol):
    def verify_persisted(
        self, connection: sqlite3.Connection, intake_public_id: str
    ) -> VerifiedSource:
        """Read this snapshot and verify durable signed source/content/event binding.

        No cached/claimed DTO, cross-database read, write, transport call or
        legacy evidence relabelling. Missing, UNKNOWN or unverifiable custody
        must raise; the port owns the actual source-content and unique-event
        verification, not merely the signature of a caller assertion.
        """
        ...


class HumanDecisionAuthority(Protocol):
    def verify_persisted(
        self,
        connection: sqlite3.Connection,
        decision_record_id: str,
        expected: ExpectedHumanDecision,
    ) -> VerifiedHumanDecision:
        """Verify durable signed decision AND display in the supplied snapshot.

        Verify exact reply linkage, private human identity, current owner and
        current display, complete displayed material, evidence/signing domains,
        current durable consumption and source/target/version/content binding.
        Fail closed on absent, ambiguous, UNKNOWN or legacy evidence. Never
        manufacture a decision from a proposal, submitter or AI output. No
        writes or decision consumption; trusted roots are composition-owned.
        """
        ...


def _reference(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 512
        and not any(ord(character) < 32 for character in value)
    )


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _proposal_version(value: object) -> bool:
    # Core initial proposals have version zero; completions increment it.
    return type(value) is int and value >= 0


def review_projection_sha256(projection: Mapping[str, object]) -> str:
    """Bind the entire neutral review exactly, in its own evidence domain."""
    material = json.dumps(
        {"schema": REVIEW_PROJECTION_SCHEMA, "projection": dict(projection)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class AdmissionService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        source_verifier: SourceVerifier,
        human_decision_authority: HumanDecisionAuthority,
        binding: TrustedBinding,
        clock: Callable[[], int],
    ) -> None:
        if type(binding) is not TrustedBinding or not all(
            _reference(value) for value in vars(binding).values()
        ):
            raise AdmissionError("Trusted admission binding is incomplete")
        self._connection = connection
        self._source_verifier = source_verifier
        self._human_decision_authority = human_decision_authority
        self._binding = binding
        self._clock = clock

    def _now(self) -> int:
        now = self._clock()
        if not _positive_int(now):
            raise AdmissionError("Admission clock is unavailable")
        return now

    def _source(self, intake_public_id: str, now: int) -> VerifiedSource:
        source = self._source_verifier.verify_persisted(self._connection, intake_public_id)
        binding = self._binding
        if type(source) is not VerifiedSource or (
            source.schema != SOURCE_SCHEMA
            or source.namespace != binding.source_namespace
            or source.key_id != binding.source_key_id
            or source.instance_id != binding.instance_id
            or source.submission_client_id != binding.submission_client_id
            or source.intake_public_id != intake_public_id
            or not _reference(source.evidence_id)
            or not _reference(source.source_event_id)
            or not _digest(source.source_content_hash)
            or not _digest(source.evidence_digest)
            or not _positive_int(source.source_occurred_at)
            or not _positive_int(source.received_at)
            or not source.source_occurred_at <= source.received_at <= now
        ):
            raise AdmissionError("Source evidence does not match trusted admission")
        return source

    def admit_source(self, intake_public_id: str) -> SourceInspection:
        if not _reference(intake_public_id):
            raise AdmissionError("Source reference is invalid")
        try:
            with review_snapshot(self._connection):
                now = self._now()
                source = self._source(intake_public_id, now)
                return SourceInspection(
                    source.intake_public_id, source.evidence_id, source.evidence_digest, now
                )
        except AdmissionError:
            raise
        except Exception as exc:
            raise AdmissionError("Durable source evidence is unavailable") from exc

    def check_human_decision(
        self, proposal_public_id: str, decision_record_id: str
    ) -> DecisionInspection:
        if not _reference(proposal_public_id) or not _reference(decision_record_id):
            raise AdmissionError("Decision reference is invalid")
        try:
            with review_snapshot(self._connection):
                now = self._now()
                projection = get_proposal_review(self._connection, proposal_public_id)
                intake_id = projection["intake_public_id"]
                version = projection["proposal_version"]
                content_hash = projection["effective_content_hash"]
                if (
                    not _reference(intake_id)
                    or not _proposal_version(version)
                    or not _digest(content_hash)
                    or projection["proposal_public_id"] != proposal_public_id
                ):
                    raise AdmissionError("Current proposal evidence is unavailable")
                source = self._source(intake_id, now)
                expected = ExpectedHumanDecision(
                    self._binding,
                    source,
                    proposal_public_id,
                    version,
                    content_hash,
                    review_projection_sha256(projection),
                    now,
                )
                decision = self._human_decision_authority.verify_persisted(
                    self._connection, decision_record_id, expected
                )
                self._check_decision(decision, decision_record_id, expected)
                return DecisionInspection(
                    decision.decision_id,
                    decision.decision_digest,
                    source.evidence_id,
                    source.evidence_digest,
                    decision.display_id,
                    decision.display_evidence_digest,
                    expected.review_projection_hash,
                    expected.proposal_public_id,
                    expected.proposal_version,
                    expected.proposal_content_hash,
                    decision.action,
                    now,
                )
        except AdmissionError:
            raise
        except Exception as exc:
            raise AdmissionError("Durable human decision evidence is unavailable") from exc

    def _check_decision(
        self,
        decision: VerifiedHumanDecision,
        decision_record_id: str,
        expected: ExpectedHumanDecision,
    ) -> None:
        binding, source = expected.binding, expected.source
        if type(decision) is not VerifiedHumanDecision or (
            decision.schema != DECISION_SCHEMA
            or decision.namespace != binding.decision_namespace
            or decision.key_id != binding.decision_key_id
            or decision.instance_id != binding.instance_id
            or decision.human_principal_id != binding.human_principal_id
            or decision.decision_id != decision_record_id
            or decision.source_evidence_id != source.evidence_id
            or decision.source_evidence_digest != source.evidence_digest
            or decision.proposal_public_id != expected.proposal_public_id
            or not _proposal_version(decision.proposal_version)
            or decision.proposal_version != expected.proposal_version
            or decision.proposal_content_hash != expected.proposal_content_hash
            or decision.review_projection_hash != expected.review_projection_hash
            or not _reference(decision.display_id)
            or not _digest(decision.display_evidence_digest)
            or decision.action != "confirm"
            or not _positive_int(decision.issued_at)
            or not _positive_int(decision.expires_at)
            or not decision.issued_at <= expected.checked_at < decision.expires_at
            or type(decision.consumed) is not bool
            or decision.consumed
            or not _digest(decision.decision_digest)
        ):
            raise AdmissionError("Human decision does not match current trusted review")
