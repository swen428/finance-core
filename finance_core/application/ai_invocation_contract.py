"""Fixed-composition text-only AI interpretation ports; no human authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from finance_core.application.admission import TrustedBinding, VerifiedSource

AI_INPUT_SCHEMA_V1 = "finance-independent-ai-input-v1"
AI_RESULT_SCHEMA_V1 = "finance-independent-ai-result-v1"
AI_PUBLICATION_SCHEMA_V1 = "finance-independent-ai-publication-v1"
AI_INTERPRETATION_SCHEMA_V1 = "finance-independent-ai-interpretation-input-v1"
AI_INPUT_SCHEMA = "finance-independent-ai-input-v2"
AI_RESULT_SCHEMA = "finance-independent-ai-result-v2"
AI_PUBLICATION_SCHEMA = "finance-independent-ai-publication-v2"
AI_INTERPRETATION_SCHEMA = "finance-independent-ai-interpretation-input-v2"


class AiInvocationError(ValueError):
    """The invocation/evidence cannot be truthfully admitted."""


@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    model_id: str
    adapter_id: str
    endpoint_id: str
    key_id: str
    deadline_seconds: int = 60


@dataclass(frozen=True)
class InterpretationPolicy:
    timezone: str
    default_currency: str


@dataclass(frozen=True)
class ExpectedInterpretationInput:
    binding: TrustedBinding
    source: VerifiedSource
    review_id: str
    review_hash: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    checked_at: int
    historical: bool = False


@dataclass(frozen=True)
class VerifiedInterpretationInput:
    schema: str
    instance_id: str
    human_principal_id: str
    evidence_id: str
    evidence_digest: str
    source_evidence_id: str
    source_evidence_digest: str
    review_id: str
    review_hash: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    display_id: str
    display_evidence_digest: str
    reply_to_display_id: str
    text: str
    observed_at: int
    expires_at: int
    revoked: bool = False


@dataclass(frozen=True)
class AiDispatchRequest:
    attempt_id: str
    claim_id: str
    request_json: str
    request_hash: str
    binding: ProviderBinding
    claimed_at: int
    deadline_at: int


@dataclass(frozen=True)
class AiProviderObservation:
    claim_id: str
    request_hash: str
    outcome: str
    observed_provider_id: str | None
    observed_model_id: str | None
    observed_at: int
    evidence_digest: str
    raw_response: str | None = None


@dataclass(frozen=True)
class AiInvocationStatus:
    attempt_id: str
    state: str
    reason: str | None = None
    proposal_public_id: str | None = None
    proposal_version: int | None = None
    effective_content_hash: str | None = None
    is_current: bool = False


class InterpretationInputVerifier(Protocol):
    def verify_persisted(
        self,
        connection: sqlite3.Connection,
        reply_evidence_id: str,
        expected: ExpectedInterpretationInput,
    ) -> VerifiedInterpretationInput:
        """Reverify retained exact reply to a genuinely SENT full current display.

        Historical verification preserves accepted-time proof. This port never
        interprets the text as a human amendment or decision and never writes.
        """
        ...


class TrustedAiProvider(Protocol):
    binding: ProviderBinding

    def dispatch(self, request: AiDispatchRequest) -> str:
        """Perform at most one text-only call, retry=0/tools=none; retain receipt.

        Return an opaque durable observation reference. Never expose headers,
        credentials, SDK exceptions or fallback provider/model substitutions.
        This is called without a SQLite writer transaction.
        """
        ...

    def verify_persisted(
        self,
        connection: sqlite3.Connection,
        observation_record_id: str,
        expected: AiDispatchRequest,
    ) -> AiProviderObservation:
        """Reverify actual retained adapter/transport receipt and request correlation.

        A caller DTO or self-declared attribution is insufficient. Verify the
        fixed adapter/endpoint/provider/model, actual request serialization and
        authentic response/transport observation in this snapshot. Read-only.
        """
        ...


@dataclass(frozen=True)
class ExpectedAiLineage:
    binding: TrustedBinding
    source: VerifiedSource
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str


class AiLineageAuthority(Protocol):
    def verify_in_snapshot(
        self, connection: sqlite3.Connection, expected: ExpectedAiLineage
    ) -> Mapping[str, object] | None:
        "Reverify all new AI ancestors with composition-owned source/input/provider ports."
        ...


@dataclass(frozen=True)
class ExpectedInterpretationInputV2:
    binding: TrustedBinding
    source: VerifiedSource
    interaction_kind: str
    interaction_id: str
    interaction_projection_hash: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    checked_at: int
    historical: bool = False


@dataclass(frozen=True)
class VerifiedInterpretationInputV2:
    schema: str
    instance_id: str
    human_principal_id: str
    evidence_id: str
    evidence_digest: str
    source_evidence_id: str
    source_evidence_digest: str
    interaction_kind: str
    interaction_id: str
    interaction_projection_hash: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    display_id: str
    display_evidence_digest: str
    reply_to_display_id: str
    text: str
    observed_at: int
    expires_at: int
    revoked: bool = False


@dataclass(frozen=True)
class ClarificationReview:
    request_id: str
    request_hash: str
    projection: dict[str, object]
    expires_at: int


@dataclass(frozen=True)
class ClarificationStatus:
    request_id: str
    state: str
    projection: dict[str, object]
    expires_at: int
