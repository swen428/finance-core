"""Historical native 001-061 closure format; no recovery or writer authority."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol

from ._recovery_rows_v1 import ClosureLimitsV1, NativeRowsV1, RecoveryMaterialError
from .contracts import RuntimeBinding
from .service import LedgerService


class RecoveryEvidenceArchiveSourceV1(Protocol):
    def export(self, kind: str, evidence_id: str) -> bytes: ...


class RecoveryEvidenceAuthenticatorV1(Protocol):
    def verify(
        self, *, owner_id: str, key_id: str, payload: bytes, authentication: bytes
    ) -> None: ...


@dataclass(frozen=True)
class ClosureIdentityV1:
    source_binding: RuntimeBinding
    core_identity: str
    migration_identity: str
    migration_ledger_digest: str
    format: str = "native-closure-identity-v1"
    scope: str = "offline-native-personal-expense-v1"


@dataclass(frozen=True)
class ClosureFrontierV1:
    package_sha256: str
    event_seq: int
    event_sha256: str | None
    format: str = "native-closure-frontier-v1"


@dataclass(frozen=True)
class ClosureInventoryV1:
    event_count: int
    event_sha256: str | None
    fact_version_count: int
    current_facts: tuple[tuple[str, int, str], ...]
    rows_sha256: str
    evidence_sha256: str


@dataclass(frozen=True)
class _ArtifactV1:
    role: str
    sha256: str
    size: int
    bytes_b64: str
    locator: str | None


@dataclass(frozen=True)
class _EvidenceEntryV1:
    kind: str
    evidence_id: str
    owner_id: str
    key_id: str
    context_json: str
    value_json: str
    artifacts: tuple[_ArtifactV1, ...]
    authentication_b64: str
    format: str = "native-evidence-archive-v1"

    def wire(self):
        return {
            "format": self.format,
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "owner_id": self.owner_id,
            "key_id": self.key_id,
            "context": json.loads(self.context_json),
            "value": json.loads(self.value_json),
            "artifacts": [asdict(item) for item in self.artifacts],
            "authentication_b64": self.authentication_b64,
        }


@dataclass(frozen=True)
class NativeClosureV1:
    identity: ClosureIdentityV1
    previous: ClosureFrontierV1 | None
    first_event_seq: int
    last_event_seq: int
    last_event_sha256: str | None
    rows: NativeRowsV1
    evidence: tuple[_EvidenceEntryV1, ...]
    inventory: ClosureInventoryV1
    format: str = "native-closure-segment-v1"

    def _wire(self):
        return {
            "format": self.format,
            "identity": asdict(self.identity),
            "previous": asdict(self.previous) if self.previous is not None else None,
            "first_event_seq": self.first_event_seq,
            "last_event_seq": self.last_event_seq,
            "last_event_sha256": self.last_event_sha256,
            "rows": self.rows.wire(),
            "evidence": [entry.wire() for entry in self.evidence],
            "inventory": asdict(self.inventory),
        }


@dataclass(frozen=True)
class ClosureCheckV1:
    identity: ClosureIdentityV1
    frontier: ClosureFrontierV1
    inventory: ClosureInventoryV1
    current_values: tuple[tuple[str, str, int, str, int, str, str], ...]
    status: str = "CONTENT_VERIFIED_OFFLINE"


def collect_native_closure_v1(
    service: LedgerService,
    *,
    archive_source: RecoveryEvidenceArchiveSourceV1,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    previous_packages: tuple[bytes, ...] = (),
    limits: ClosureLimitsV1 | None = None,
) -> bytes:
    """Collect from an exact 061 source; a current 062 source must use v2."""
    from . import _recovery_material_engine as _engine

    return _engine._collect(
        service,
        archive_source=archive_source,
        authenticator=authenticator,
        previous_packages=previous_packages,
        limits=limits,
    )


def decode_native_closure_v1(
    payload: bytes, *, limits: ClosureLimitsV1 | None = None
) -> NativeClosureV1:
    """Decode historical v1 structure; no content or authenticity conclusion."""
    from . import _recovery_material_engine as _engine

    return _engine._decode(payload, limits=limits)


def check_native_closure_v1(
    packages: tuple[bytes, ...],
    *,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    expected_identity: ClosureIdentityV1,
    expected_frontier: ClosureFrontierV1,
    limits: ClosureLimitsV1 | None = None,
) -> ClosureCheckV1:
    """Rebuild genuine historical packages using exact trusted 001-061 schema."""
    from . import _recovery_material_engine as _engine

    return _engine._check(
        packages,
        authenticator=authenticator,
        expected_identity=expected_identity,
        expected_frontier=expected_frontier,
        limits=limits,
    )


__all__ = [
    "RecoveryMaterialError",
    "RecoveryEvidenceArchiveSourceV1",
    "RecoveryEvidenceAuthenticatorV1",
    "ClosureLimitsV1",
    "ClosureIdentityV1",
    "ClosureFrontierV1",
    "ClosureInventoryV1",
    "NativeClosureV1",
    "ClosureCheckV1",
    "collect_native_closure_v1",
    "decode_native_closure_v1",
    "check_native_closure_v1",
]
