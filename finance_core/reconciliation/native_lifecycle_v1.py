"""Opt-in verified native candidates; no legacy-table or reconciliation writes.

Keep the collection and each fact reference with matcher results. A candidate
is a read observation, not authority to post, adjust, or persist a match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import localcontext

from finance_core.lifecycle_v2.effective_v1 import (
    EffectiveFilterV1,
    EffectiveLedgerReaderV1,
    ReadIdentityV1,
    ReadLimitsV1,
    SourceIdentityV1,
)
from finance_core.money import Money
from finance_core.reconciliation.models import InternalCandidate, ReconciliationTransactionType


@dataclass(frozen=True)
class FactRefV1:
    record_id: str
    economic_event_id: str
    version: int
    fact_sha256: str
    commit_seq: int


@dataclass(frozen=True)
class NativeReconciliationCandidateV1:
    fact_ref: FactRefV1
    source: SourceIdentityV1
    candidate: InternalCandidate
    format: str = field(default="finance-native-reconciliation-candidate-v1", init=False)
    account_status: str = field(default="unknown", init=False)
    account_id: None = field(default=None, init=False)
    status: str = field(default="LOCAL_POSTED", init=False)


@dataclass(frozen=True)
class NativeCandidateCollectionV1:
    filters: EffectiveFilterV1
    read_identity: ReadIdentityV1
    candidates: tuple[NativeReconciliationCandidateV1, ...]
    format: str = field(default="finance-native-candidate-collection-v1", init=False)
    scope: str = field(default="native-lifecycle-v2", init=False)
    complete: bool = field(default=True, init=False)


class NativeReconciliationAdapterV1:
    """Read one complete verified collection through the actual Core reader.

    This deliberately does not accept a SQLite connection, cached DTOs, or
    caller-supplied facts. There is no matching-decision persistence API.
    """

    def __init__(self, reader: EffectiveLedgerReaderV1):
        if not isinstance(reader, EffectiveLedgerReaderV1):
            raise TypeError("Native candidates require an EffectiveLedgerReaderV1")
        self._reader = reader

    def fetch_candidates(
        self,
        filters: EffectiveFilterV1 | None = None,
        limits: ReadLimitsV1 | None = None,
    ) -> NativeCandidateCollectionV1:
        collection = self._reader.collect(filters=filters, limits=limits)
        candidates = []
        for fact in collection.facts:
            ref = FactRefV1(
                fact.record_id,
                fact.economic_event_id,
                fact.version,
                fact.fact_sha256,
                fact.commit_seq,
            )
            # Match the reader's bounded canonical-money validation context.
            with localcontext() as context:
                context.prec = 1024
                amount = Money.from_string(fact.amount, fact.currency).amount
            candidate = InternalCandidate(
                internal_id=f"lifecycle-v2:{fact.record_id}",
                transaction_date=date.fromisoformat(fact.transaction_date),
                merchant=fact.merchant,
                amount=amount,
                currency=fact.currency,
                transaction_type=ReconciliationTransactionType.EXPENSE,
                source_type="native-lifecycle-v2",
                source_channel=None,
                attachment_path=None,
                evidence_reference=(
                    f"lifecycle-v2:{fact.record_id}:{fact.version}:{fact.fact_sha256}"
                ),
                posted_date=None,
            )
            candidates.append(NativeReconciliationCandidateV1(ref, fact.source, candidate))
        return NativeCandidateCollectionV1(
            filters=collection.filters,
            read_identity=collection.read_identity,
            candidates=tuple(candidates),
        )


__all__ = [
    "FactRefV1",
    "NativeCandidateCollectionV1",
    "NativeReconciliationAdapterV1",
    "NativeReconciliationCandidateV1",
]
