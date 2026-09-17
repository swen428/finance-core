"""The single snapshot-derived finalization authority.

The active IAF fact-set four-tuple and the confirmed receipt identity a
finalization is allowed to use are read from exactly one place: the hash-bound
``input_payload`` of the persisted authoritative calculation snapshot, after
that snapshot has been loaded and ``verify()``-ed.

Before this boundary existed the bridge carried its own
``PreparedReceiptCalculation.active_fact_set_binding`` and the finalizer trusted
``FinalizationInput.active_fact_set_binding``, so an old snapshot could be
combined with a newer binding and still finalize.  Both stages now derive the
binding here and refuse any caller-supplied material that disagrees, so the
snapshot, the human authorization, and the finalizer share one inseparable
authority.

This module introduces no new business rule, no new hash contract, and no
second authority: it only reads what the snapshot already hash-binds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeCalculationSnapshot,
    AuthoritativeSnapshotRepository,
    SnapshotVerificationError,
    canonical_json_value,
)
from finance_core.receipt_finalization.models import (
    ActiveFactSetBinding,
    ConfirmedReceiptIdentity,
    FinalizationBlockReason,
    FinalizationValidationError,
)

BINDING_INPUT_KEY = "active_fact_set_binding"
RECEIPT_IDENTITY_INPUT_KEY = "confirmed_receipt_identity"

# The IAF calculation-run contract (IA-D11): the single description of the
# receipt-scoped historical run the bridge writes at prepare time and the
# finalizer re-verifies at finalize time.  Kept here -- the one module both the
# bridge and the finalizer already import without a cycle -- so neither side
# owns a divergent copy of the run's durable identity.
IAF_CALCULATION_RUN_TYPE = "receipt_split"
IAF_CALCULATION_RUN_RULE_VERSION = "receipt-split-v1"
IAF_CALCULATION_RUN_STATUS = "calculated"
IAF_CALCULATION_RUN_SOURCE_TYPE = "iaf_active_fact_set"
IAF_CALCULATION_RUN_ENTITY_TYPE = "receipt"

_BINDING_KEYS = frozenset(
    {
        "receipt_public_id",
        "fact_set_public_id",
        "fact_set_version",
        "fact_set_input_hash",
        "fact_set_result_hash",
    }
)
_RECEIPT_IDENTITY_KEYS = frozenset(
    {
        "receipt_public_id",
        "merchant",
        "receipt_date",
        "source_channel",
        "currency",
    }
)


class SnapshotAuthorityError(ValueError):
    """The persisted snapshot cannot serve as the finalization binding authority."""

    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SnapshotBoundAuthority:
    """Everything a finalization may treat as authoritative, snapshot-derived.

    Constructed only from a persisted snapshot whose hashes verified, so every
    field here is covered by ``combined_snapshot_hash`` and therefore by the
    human authorization content hash that binds that snapshot.
    """

    snapshot_public_id: str
    combined_snapshot_hash: str
    calculation_run_public_id: str
    receipt_group_public_id: str
    currency: str
    currency_contract_version: str
    authorization_reference: str | None
    active_fact_set_binding: ActiveFactSetBinding
    confirmed_receipt_identity: ConfirmedReceiptIdentity
    source_references: tuple[str, ...]
    snapshot_created_at: str
    output_payload: Any


def read_snapshot_bound_authority(
    conn: sqlite3.Connection,
    *,
    snapshot_public_id: str,
    expected_combined_hash: str,
) -> SnapshotBoundAuthority | None:
    """Load, verify, and derive the snapshot-bound authority for a finalization.

    Returns ``None`` for a snapshot that carries no IAF fact-set binding at
    all: legacy and group-only finalizations are unaffected, and absence of the
    binding keys means "no IAF fact set was consumed", never "the binding is
    unknown".  Every other failure — missing snapshot, hash drift, malformed or
    half-present binding material — fails closed.
    """
    if not snapshot_public_id:
        raise SnapshotAuthorityError(
            "A calculation snapshot public ID is required to derive the finalization authority",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    snapshot = AuthoritativeSnapshotRepository(conn).fetch(snapshot_public_id)
    if snapshot is None:
        raise SnapshotAuthorityError(
            f"Authoritative calculation snapshot {snapshot_public_id!r} is missing",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    try:
        snapshot.verify()
    except SnapshotVerificationError as exc:
        raise SnapshotAuthorityError(
            f"Authoritative calculation snapshot {snapshot_public_id!r} failed verification",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        ) from exc
    if expected_combined_hash and snapshot.combined_snapshot_hash != expected_combined_hash:
        raise SnapshotAuthorityError(
            f"Authoritative calculation snapshot {snapshot_public_id!r} hash does not match "
            "the hash this finalization was authorized against",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    return derive_snapshot_bound_authority(snapshot)


def derive_snapshot_bound_authority(
    snapshot: AuthoritativeCalculationSnapshot,
) -> SnapshotBoundAuthority | None:
    """Derive the bound authority from an already verified snapshot."""
    input_value = canonical_json_value(snapshot.input_payload_json, label="input")
    if not isinstance(input_value, dict):
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot.snapshot_public_id!r} input payload is not a JSON object",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    has_binding = BINDING_INPUT_KEY in input_value
    has_identity = RECEIPT_IDENTITY_INPUT_KEY in input_value
    if not has_binding and not has_identity:
        return None
    if not has_binding or not has_identity:
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot.snapshot_public_id!r} carries only part of the IAF binding "
            "material; a partial binding is never trusted",
            reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
        )

    binding = _binding_from_payload(
        input_value[BINDING_INPUT_KEY], snapshot_public_id=snapshot.snapshot_public_id
    )
    identity = _receipt_identity_from_payload(
        input_value[RECEIPT_IDENTITY_INPUT_KEY], snapshot_public_id=snapshot.snapshot_public_id
    )
    if identity.receipt_public_id != binding.receipt_public_id:
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot.snapshot_public_id!r} binds fact-set receipt "
            f"{binding.receipt_public_id!r} but receipt identity "
            f"{identity.receipt_public_id!r}",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )
    currency = input_value.get("currency")
    if currency != identity.currency:
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot.snapshot_public_id!r} input currency {currency!r} does not "
            f"match the bound receipt identity currency {identity.currency!r}",
            reason=FinalizationBlockReason.CURRENCY_MISMATCH.value,
        )
    run_public_id = input_value.get("calculation_run_public_id")
    if not isinstance(run_public_id, str) or not run_public_id:
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot.snapshot_public_id!r} input payload has no "
            "calculation_run_public_id",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )

    return SnapshotBoundAuthority(
        snapshot_public_id=snapshot.snapshot_public_id,
        combined_snapshot_hash=snapshot.combined_snapshot_hash,
        calculation_run_public_id=run_public_id,
        receipt_group_public_id=snapshot.aggregate_public_id,
        currency=identity.currency,
        currency_contract_version=snapshot.currency_contract_version,
        authorization_reference=snapshot.authorization_reference,
        active_fact_set_binding=binding,
        confirmed_receipt_identity=identity,
        source_references=snapshot.source_references,
        snapshot_created_at=snapshot.created_at,
        output_payload=canonical_json_value(snapshot.output_payload_json, label="output"),
    )


def _binding_from_payload(payload: object, *, snapshot_public_id: str) -> ActiveFactSetBinding:
    values = _require_exact_object(
        payload,
        expected_keys=_BINDING_KEYS,
        label=f"snapshot {snapshot_public_id!r} {BINDING_INPUT_KEY}",
        reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
    )
    try:
        return ActiveFactSetBinding(
            receipt_public_id=_require_str(values["receipt_public_id"]),
            fact_set_public_id=_require_str(values["fact_set_public_id"]),
            fact_set_version=_require_int(values["fact_set_version"]),
            fact_set_input_hash=_require_str(values["fact_set_input_hash"]),
            fact_set_result_hash=_require_str(values["fact_set_result_hash"]),
        )
    except (FinalizationValidationError, TypeError) as exc:
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot_public_id!r} {BINDING_INPUT_KEY} is malformed: {exc}",
            reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
        ) from exc


def _receipt_identity_from_payload(
    payload: object, *, snapshot_public_id: str
) -> ConfirmedReceiptIdentity:
    values = _require_exact_object(
        payload,
        expected_keys=_RECEIPT_IDENTITY_KEYS,
        label=f"snapshot {snapshot_public_id!r} {RECEIPT_IDENTITY_INPUT_KEY}",
        reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
    )
    try:
        return ConfirmedReceiptIdentity(
            receipt_public_id=_require_str(values["receipt_public_id"]),
            merchant=_require_str(values["merchant"]),
            receipt_date=_require_str(values["receipt_date"]),
            source_channel=_require_str(values["source_channel"]),
            currency=_require_str(values["currency"]),
        )
    except (FinalizationValidationError, TypeError) as exc:
        raise SnapshotAuthorityError(
            f"Snapshot {snapshot_public_id!r} {RECEIPT_IDENTITY_INPUT_KEY} is malformed: {exc}",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        ) from exc


def _require_exact_object(
    payload: object,
    *,
    expected_keys: frozenset[str],
    label: str,
    reason: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SnapshotAuthorityError(f"{label} must be a JSON object", reason=reason)
    if frozenset(payload) != expected_keys:
        raise SnapshotAuthorityError(
            f"{label} keys {sorted(payload)} do not match the bound contract "
            f"{sorted(expected_keys)}",
            reason=reason,
        )
    return payload


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return value


__all__ = [
    "BINDING_INPUT_KEY",
    "IAF_CALCULATION_RUN_ENTITY_TYPE",
    "IAF_CALCULATION_RUN_RULE_VERSION",
    "IAF_CALCULATION_RUN_SOURCE_TYPE",
    "IAF_CALCULATION_RUN_STATUS",
    "IAF_CALCULATION_RUN_TYPE",
    "RECEIPT_IDENTITY_INPUT_KEY",
    "SnapshotAuthorityError",
    "SnapshotBoundAuthority",
    "derive_snapshot_bound_authority",
    "read_snapshot_bound_authority",
]
