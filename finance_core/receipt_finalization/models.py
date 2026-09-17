"""Receipt finalization models — authorization, idempotency, audit, and status.

Defines the type contract for the hardened receipt finalization workflow.
All caller-supplied DTOs are treated as requests; persisted records are
the authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from finance_core.money import (
    SUPPORTED_CURRENCIES,
    ZERO,
    MoneyValidationError,
    canonical_decimal_str,
    money_decimal,
    quantize_for_currency,
    validate_amount_for_currency,
)

# ---------------------------------------------------------------------------
# Status and reason codes
# ---------------------------------------------------------------------------


class FinalizationStatus(str, Enum):
    """Stable status codes for a receipt finalization attempt."""

    AUTHORIZED = "authorized"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    FAILED = "failed"


class FinalizationBlockReason(str, Enum):
    """Single canonical reason-code authority for receipt finalization.

    Every authorization, confirmation, idempotency, and validation failure
    uses one of these stable values.  No duplicate enum or free-text literal
    is authoritative.
    """

    AUTHORIZATION_MISSING = "authorization_missing"
    AUTHORIZATION_STATE_DENIED = "authorization_state_denied"
    AUTHORIZATION_CONTENT_MISMATCH = "authorization_content_mismatch"
    AUTHORIZATION_MALFORMED = "authorization_malformed"
    AUTHORIZATION_CONSUMED = "authorization_consumed"
    CONFIRMATION_MISSING = "confirmation_missing"
    CONFIRMATION_STATE_DENIED = "confirmation_state_denied"
    CONFIRMATION_CONTENT_MISMATCH = "confirmation_content_mismatch"
    CONFIRMATION_ACTOR_MISMATCH = "confirmation_actor_mismatch"
    ACTOR_MISMATCH = "actor_mismatch"
    CALCULATION_SNAPSHOT_MISMATCH = "calculation_snapshot_mismatch"
    PARTICIPANT_MISMATCH = "participant_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    OBLIGATION_MISMATCH = "obligation_mismatch"
    RECEIPT_GROUP_NOT_FOUND = "receipt_group_not_found"
    RECEIPT_GROUP_WRONG_STATUS = "receipt_group_wrong_status"
    CURRENCY_MISMATCH = "currency_mismatch"
    CONTENT_CONFLICT = "content_conflict"
    ALREADY_FINALIZED = "already_finalized"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    DATABASE_INTEGRITY_ERROR = "database_integrity_error"
    MISSING_SCHEMA = "missing_schema"
    EMPTY_IDEMPOTENCY_KEY = "empty_idempotency_key"
    DANGLING_AUDIT_REFERENCE = "dangling_audit_reference"
    STALE_ACTIVE_FACT_SET = "stale_active_fact_set"
    SNAPSHOT_BINDING_MISMATCH = "snapshot_binding_mismatch"
    RECEIPT_IDENTITY_MISMATCH = "receipt_identity_mismatch"
    RECEIPT_GROUP_IDENTITY_CONFLICT = "receipt_group_identity_conflict"
    RECEIPT_GROUP_MEMBERSHIP_CONFLICT = "receipt_group_membership_conflict"
    RECEIPT_GROUP_FOREIGN_RECEIPT = "receipt_group_foreign_receipt"
    AUDIT_TOTAL_MISMATCH = "audit_total_mismatch"
    REPLAY_TRUTH_MISMATCH = "replay_truth_mismatch"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FinalizationValidationError(ValueError):
    """Input failed pre-finalization validation and no database writes occurred."""


class DuplicateFinalizationError(Exception):
    """The calculation has already been finalized."""


class IneligibleForFinalizationError(ValueError):
    """The receipt group or calculation is not in an eligible state for finalization."""


class FinalizationAuthorizationError(ValueError):
    """Persisted authorization is missing, malformed, denied, or mismatched."""

    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class FinalizationIdempotencyError(ValueError):
    """Idempotency conflict — same key, different content."""

    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class FinalizationPersistenceError(RuntimeError):
    """Required schema is missing — migration 020 must be applied."""


class ReceiptGroupMaterializationError(ValueError):
    """The single-receipt group could not be materialized or validated.

    Raised when a receipt group with the deterministic public ID already
    exists with a conflicting identity, holds a foreign or additional
    receipt, or when a membership public ID collides with unrelated durable
    state.  Such a group is never silently adopted.
    """

    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Core domain types (preserved from previous version)
# ---------------------------------------------------------------------------

CanonicalObligation = tuple[str, str, Decimal, str]


@dataclass(frozen=True)
class SettlementObligation:
    debtor_participant_public_id: str
    creditor_participant_public_id: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.debtor_participant_public_id == self.creditor_participant_public_id:
            raise FinalizationValidationError(
                f"Self-obligation is not allowed: "
                f"{self.debtor_participant_public_id} owes "
                f"{self.creditor_participant_public_id}"
            )
        if not isinstance(self.amount, Decimal):
            raise FinalizationValidationError(
                f"Amount for {self.debtor_participant_public_id}"
                f"->{self.creditor_participant_public_id} must be Decimal, "
                f"got {type(self.amount).__name__}"
            )
        if self.amount <= ZERO:
            raise FinalizationValidationError(
                f"Obligation amount for {self.debtor_participant_public_id}"
                f"->{self.creditor_participant_public_id} "
                f"must be positive, got {self.amount}"
            )
        validate_amount_for_currency(
            self.amount,
            self.currency,
            label=f"obligation {self.debtor_participant_public_id}"
            f"->{self.creditor_participant_public_id}",
        )
        if self.currency not in SUPPORTED_CURRENCIES:
            raise FinalizationValidationError(
                f"Unsupported currency {self.currency!r} "
                f"for obligation {self.debtor_participant_public_id}"
                f"->{self.creditor_participant_public_id}"
            )


# ---------------------------------------------------------------------------
# Authorization records (loaded from DB, not caller-trusted)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersistedFinalizationAuthorization:
    """A validated persisted authorization record loaded from the database.

    This is the authority for whether finalization is permitted.  Caller-
    supplied DTO fields are NEVER trusted in place of this record.
    """

    authorization_id: str
    receipt_group_public_id: str
    calculation_run_public_id: str
    calculation_snapshot_id: str
    confirmation_id: str
    content_hash: str
    currency: str
    final_total: str
    payer_participant_public_id: str
    participant_public_ids: tuple[str, ...]
    settlement_obligations_json: str
    source_evidence_refs: tuple[str, ...]
    actor_type: str
    actor_id: str | None
    authorization_state: str
    authorization_version: str


# ---------------------------------------------------------------------------
# Idempotency record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizationIdempotencyRecord:
    """A previously persisted idempotency guard record."""

    idempotency_key: str
    content_fingerprint: str
    status: str
    finalization_audit_id: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizationAuditRecord:
    """Immutable audit record for a finalization attempt."""

    finalization_id: str
    idempotency_key: str
    content_fingerprint: str
    authorization_id: str
    confirmation_id: str | None
    receipt_group_public_id: str
    calculation_run_public_id: str
    calculation_snapshot_id: str | None
    transaction_public_id: str | None
    participant_public_ids: tuple[str, ...]
    settlement_public_ids: tuple[str, ...]
    currency: str
    total_paid: str
    total_to_collect: str
    payer_participant_public_id: str
    actor_type: str
    actor_id: str | None
    status: str
    failure_reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    source_attachment_refs: tuple[str, ...]
    created_at: str


# ---------------------------------------------------------------------------
# Active IAF fact-set binding (IAF.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveFactSetBinding:
    """The active IAF fact-set four-tuple a finalization was authorized against.

    Optional on :class:`FinalizationInput`.  When present, the finalizer
    re-reads the receipt's live active fact set inside its write transaction
    and fails closed on any drift (supersession, version, or hash mismatch).
    """

    receipt_public_id: str
    fact_set_public_id: str
    fact_set_version: int
    fact_set_input_hash: str
    fact_set_result_hash: str

    def __post_init__(self) -> None:
        for name, value in (
            ("receipt_public_id", self.receipt_public_id),
            ("fact_set_public_id", self.fact_set_public_id),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise FinalizationValidationError(
                    f"active fact-set binding {name} must be a trimmed non-empty string"
                )
        if not isinstance(self.fact_set_version, int) or self.fact_set_version < 1:
            raise FinalizationValidationError(
                "active fact-set binding fact_set_version must be a positive integer"
            )
        for name, value in (
            ("fact_set_input_hash", self.fact_set_input_hash),
            ("fact_set_result_hash", self.fact_set_result_hash),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise FinalizationValidationError(
                    f"active fact-set binding {name} must be a lowercase 64-char SHA-256 hash"
                )

    def as_fingerprint_payload(self) -> dict[str, Any]:
        return {
            "receipt_public_id": self.receipt_public_id,
            "fact_set_public_id": self.fact_set_public_id,
            "fact_set_version": self.fact_set_version,
            "fact_set_input_hash": self.fact_set_input_hash,
            "fact_set_result_hash": self.fact_set_result_hash,
        }


# ---------------------------------------------------------------------------
# Confirmed receipt identity (canonical transaction metadata authority)
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ConfirmedReceiptIdentity:
    """The confirmed receipt facts a canonical transaction must preserve.

    Hash-bound into the authoritative calculation snapshot input payload and
    re-verified against the live ``receipts`` row inside the finalizer's write
    transaction, so the canonical transaction's merchant and transaction date
    are the receipt's own facts — never the receipt public ID and never the
    finalization clock.
    """

    receipt_public_id: str
    merchant: str
    receipt_date: str
    source_channel: str
    currency: str

    def __post_init__(self) -> None:
        for name, value in (
            ("receipt_public_id", self.receipt_public_id),
            ("merchant", self.merchant),
            ("source_channel", self.source_channel),
            ("currency", self.currency),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise FinalizationValidationError(
                    f"confirmed receipt identity {name} must be a trimmed non-empty string"
                )
        if not isinstance(self.receipt_date, str) or not _ISO_DATE_RE.match(self.receipt_date):
            raise FinalizationValidationError(
                "confirmed receipt identity receipt_date must be an ISO YYYY-MM-DD date"
            )

    def as_fingerprint_payload(self) -> dict[str, Any]:
        return {
            "receipt_public_id": self.receipt_public_id,
            "merchant": self.merchant,
            "receipt_date": self.receipt_date,
            "source_channel": self.source_channel,
            "currency": self.currency,
        }


# ---------------------------------------------------------------------------
# Single-receipt group materialization request (IAF finalization)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptGroupMaterialization:
    """Deterministic single-receipt group the finalizer materializes itself.

    The group and its one membership row are written inside the finalizer's own
    ``BEGIN IMMEDIATE`` Unit of Work, so any later failure rolls them back with
    every other finalization write and a later fact-set supersession stays
    possible.
    """

    receipt_public_id: str
    receipt_group_receipt_public_id: str
    group_type: str
    status: str
    source: str

    def __post_init__(self) -> None:
        for name, value in (
            ("receipt_public_id", self.receipt_public_id),
            ("receipt_group_receipt_public_id", self.receipt_group_receipt_public_id),
            ("group_type", self.group_type),
            ("status", self.status),
            ("source", self.source),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise FinalizationValidationError(
                    f"receipt group materialization {name} must be a trimmed non-empty string"
                )

    def as_fingerprint_payload(self) -> dict[str, Any]:
        return {
            "receipt_public_id": self.receipt_public_id,
            "receipt_group_receipt_public_id": self.receipt_group_receipt_public_id,
            "group_type": self.group_type,
            "status": self.status,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Finalization input (extended with authorization + idempotency fields)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizationInput:
    calculation_run_public_id: str
    receipt_group_public_id: str
    currency: str
    payer_participant_public_id: str
    settlement_obligations: tuple[SettlementObligation, ...] = ()
    # Private canonical JSON bytes — assigned only during construction.
    _snapshot_json_bytes: bytes = field(default=b"", init=False)
    # Authorization / idempotency fields
    authorization_id: str = ""
    confirmation_id: str = ""
    idempotency_key: str = ""
    calculation_snapshot_id: str = ""
    calculation_snapshot_hash: str = ""
    currency_contract_version: str = ""
    actor_type: str = "system"
    actor_id: str | None = None
    source_evidence_refs: tuple[str, ...] = ()
    active_fact_set_binding: ActiveFactSetBinding | None = None
    confirmed_receipt_identity: ConfirmedReceiptIdentity | None = None
    receipt_group_materialization: ReceiptGroupMaterialization | None = None

    @property
    def calculation_snapshot(self) -> dict[str, Any]:
        """Return a fresh decoded dict copy; mutations cannot affect canonical state."""
        return json.loads(self._snapshot_json_bytes.decode("utf-8"))

    def __init__(
        self,
        calculation_run_public_id: str = "",
        receipt_group_public_id: str = "",
        currency: str = "",
        payer_participant_public_id: str = "",
        settlement_obligations: tuple[SettlementObligation, ...] | list[SettlementObligation] = (),
        calculation_snapshot: dict[str, Any] | None = None,
        *,
        authorization_id: str = "",
        confirmation_id: str = "",
        idempotency_key: str = "",
        calculation_snapshot_id: str = "",
        calculation_snapshot_hash: str = "",
        currency_contract_version: str = "",
        actor_type: str = "system",
        actor_id: str | None = None,
        source_evidence_refs: tuple[str, ...] = (),
        active_fact_set_binding: ActiveFactSetBinding | None = None,
        confirmed_receipt_identity: ConfirmedReceiptIdentity | None = None,
        receipt_group_materialization: ReceiptGroupMaterialization | None = None,
    ) -> None:
        raw = calculation_snapshot if calculation_snapshot is not None else {}

        if isinstance(settlement_obligations, list):
            settlement_obligations = tuple(settlement_obligations)
        if isinstance(source_evidence_refs, list):
            source_evidence_refs = tuple(source_evidence_refs)

        # Validate evidence refs BEFORE canonicalization.
        _validate_evidence_refs(source_evidence_refs)

        # --- raw-input validation BEFORE JSON canonicalization ---
        _validate_raw_snapshot_input(raw)

        # --- serialize into immutable bytes ---
        snapshot_bytes = json.dumps(raw, default=_snapshot_decimal_default, sort_keys=True).encode(
            "utf-8"
        )

        # --- standard frozen-dataclass init ---
        object.__setattr__(self, "calculation_run_public_id", calculation_run_public_id)
        object.__setattr__(self, "receipt_group_public_id", receipt_group_public_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "payer_participant_public_id", payer_participant_public_id)
        object.__setattr__(self, "settlement_obligations", settlement_obligations)
        object.__setattr__(self, "_snapshot_json_bytes", snapshot_bytes)
        object.__setattr__(self, "authorization_id", authorization_id)
        object.__setattr__(self, "confirmation_id", confirmation_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "calculation_snapshot_id", calculation_snapshot_id)
        object.__setattr__(self, "calculation_snapshot_hash", calculation_snapshot_hash)
        object.__setattr__(self, "currency_contract_version", currency_contract_version)
        object.__setattr__(self, "actor_type", actor_type)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "source_evidence_refs", source_evidence_refs)
        object.__setattr__(self, "active_fact_set_binding", active_fact_set_binding)
        object.__setattr__(self, "confirmed_receipt_identity", confirmed_receipt_identity)
        object.__setattr__(self, "receipt_group_materialization", receipt_group_materialization)

        # --- field-level validation on canonical data ---
        if not self.calculation_run_public_id:
            raise FinalizationValidationError("calculation_run_public_id is required")
        if not self.receipt_group_public_id:
            raise FinalizationValidationError("receipt_group_public_id is required")
        if not self.payer_participant_public_id:
            raise FinalizationValidationError("payer_participant_public_id is required")
        if self.currency not in SUPPORTED_CURRENCIES:
            raise FinalizationValidationError(
                f"Unsupported finalization currency {self.currency!r}"
            )
        if self.calculation_snapshot_hash:
            if len(self.calculation_snapshot_hash) != 64 or any(
                char not in "0123456789abcdef" for char in self.calculation_snapshot_hash
            ):
                raise FinalizationValidationError(
                    "calculation_snapshot_hash must be a lowercase 64-character SHA-256 hash"
                )
            if not self.calculation_snapshot_id:
                raise FinalizationValidationError(
                    "calculation_snapshot_id is required with calculation_snapshot_hash"
                )
            if not self.currency_contract_version:
                raise FinalizationValidationError(
                    "currency_contract_version is required with calculation_snapshot_hash"
                )
        if self.authorization_id and not isinstance(self.authorization_id, str):
            raise FinalizationValidationError("authorization_id must be a string")
        if self.actor_type not in ("human", "cli", "system"):
            raise FinalizationValidationError(
                f"Invalid actor_type {self.actor_type!r}; must be one of: human, cli, system"
            )

        for obligation in self.settlement_obligations:
            if obligation.currency != self.currency:
                raise FinalizationValidationError(
                    f"Obligation currency {obligation.currency!r} "
                    f"does not match finalization currency {self.currency!r}"
                )
            if obligation.creditor_participant_public_id != self.payer_participant_public_id:
                raise FinalizationValidationError(
                    f"Obligation creditor {obligation.creditor_participant_public_id!r} "
                    f"must match payer {self.payer_participant_public_id!r}"
                )

        self._validate_calculation_snapshot()

    def _validate_calculation_snapshot(self) -> None:
        snapshot = self.calculation_snapshot
        if not snapshot:
            raise FinalizationValidationError(
                "calculation_snapshot with settlement_obligations is required for finalization"
            )

        expected_obligations = snapshot.get("settlement_obligations")
        if expected_obligations is None:
            raise FinalizationValidationError(
                "calculation_snapshot['settlement_obligations'] is required for finalization"
            )
        if not isinstance(expected_obligations, list):
            raise FinalizationValidationError(
                "calculation_snapshot['settlement_obligations'] must be a list"
            )

        self._validate_obligations_match_snapshot(expected_obligations)

        # Validate snapshot payer.
        snapshot_payer = snapshot.get("payer")
        if not snapshot_payer or not isinstance(snapshot_payer, str):
            raise FinalizationValidationError(
                "calculation_snapshot.payer is required and must be a non-empty string"
            )
        if snapshot_payer != self.payer_participant_public_id:
            raise FinalizationValidationError(
                f"calculation_snapshot.payer {snapshot_payer!r} does not match "
                f"FinalizationInput.payer_participant_public_id "
                f"{self.payer_participant_public_id!r}"
            )

        # Validate snapshot participants.
        snapshot_participants = snapshot.get("participants")
        if not isinstance(snapshot_participants, list) or not snapshot_participants:
            raise FinalizationValidationError(
                "calculation_snapshot.participants must be a non-empty list"
            )
        if len(set(snapshot_participants)) != len(snapshot_participants):
            raise FinalizationValidationError(
                "calculation_snapshot.participants contains duplicate public IDs"
            )
        for pub_id in snapshot_participants:
            if not isinstance(pub_id, str) or not pub_id:
                raise FinalizationValidationError(
                    f"calculation_snapshot.participants contains non-string value: {pub_id!r}"
                )

        participant_set = set(snapshot_participants)

        if snapshot_payer not in participant_set:
            raise FinalizationValidationError(
                f"calculation_snapshot.payer {snapshot_payer!r} is not in "
                f"calculation_snapshot.participants"
            )

        # Validate participant_shares.
        snapshot_shares = snapshot.get("participant_shares")
        if not isinstance(snapshot_shares, dict):
            raise FinalizationValidationError(
                "calculation_snapshot.participant_shares must be a mapping"
            )
        share_keys = set(snapshot_shares.keys())
        if share_keys != participant_set:
            missing = participant_set - share_keys
            extra = share_keys - participant_set
            msg_parts = []
            if missing:
                msg_parts.append(f"missing from shares: {sorted(missing)}")
            if extra:
                msg_parts.append(f"extra in shares: {sorted(extra)}")
            raise FinalizationValidationError(
                "calculation_snapshot.participant_shares keys do not match "
                f"participant set: {'; '.join(msg_parts)}"
            )

        # ---- participant-share and total reconciliation ----
        _validate_share_total_reconciliation(
            snapshot, snapshot_participants, participant_set, snapshot_payer, self.currency
        )

        # ---- obligation/balance consistency for single-payer contract ----
        _validate_balance_obligation_consistency(
            snapshot, participant_set, snapshot_payer, self.currency
        )

        # Validate obligation identities belong to participant set.
        for i, obl in enumerate(expected_obligations):
            if not isinstance(obl, dict):
                continue
            debtor = obl.get("debtor") if isinstance(obl, dict) else None
            creditor = obl.get("creditor") if isinstance(obl, dict) else None
            label = f"calculation_snapshot.settlement_obligations[{i}]"
            if debtor and (not isinstance(debtor, str) or debtor not in participant_set):
                raise FinalizationValidationError(
                    f"{label}.debtor {debtor!r} is not in snapshot participants"
                )
            if creditor and (not isinstance(creditor, str) or creditor not in participant_set):
                raise FinalizationValidationError(
                    f"{label}.creditor {creditor!r} is not in snapshot participants"
                )

        # Amount reconciliation.
        expected_total = snapshot.get("total_paid")
        payer_own = snapshot.get("payer_own_share")
        if expected_total is not None and payer_own is not None:
            expected_collect = _validated_amount(
                expected_total, self.currency, "calculation_snapshot.total_paid"
            )
            expected_collect -= _validated_amount(
                payer_own, self.currency, "calculation_snapshot.payer_own_share"
            )
            actual_total = sum((o.amount for o in self.settlement_obligations), ZERO)
            if actual_total != expected_collect:
                raise FinalizationValidationError(
                    f"Settlement obligations total {actual_total} does not match "
                    f"expected total to collect {expected_collect} "
                    f"(total_paid {expected_total} - payer_own_share {payer_own})"
                )

    def _validate_obligations_match_snapshot(self, expected_obligations: list[Any]) -> None:
        if len(self.settlement_obligations) != len(expected_obligations):
            raise FinalizationValidationError(
                f"Settlement obligations count {len(self.settlement_obligations)} "
                f"does not match calculation snapshot count {len(expected_obligations)}"
            )

        actual = sorted(_canonical_from_obligation(o) for o in self.settlement_obligations)
        expected = sorted(
            _canonical_from_mapping(
                o,
                f"calculation_snapshot.settlement_obligations[{i}]",
            )
            for i, o in enumerate(expected_obligations)
        )
        if actual != expected:
            raise FinalizationValidationError(
                "Settlement obligations do not match calculation snapshot settlement_obligations"
            )

    @property
    def participant_public_ids(self) -> tuple[str, ...]:
        """Extract the sorted participant public IDs from the snapshot."""
        snapshot = self.calculation_snapshot
        participants = snapshot.get("participants", [])
        if isinstance(participants, list):
            return tuple(sorted(p for p in participants if isinstance(p, str)))
        return ()


# ---------------------------------------------------------------------------
# Finalization output (extended with status and audit linkage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizationOutput:
    finalization_public_id: str
    calculation_run_public_id: str
    obligations_created: int
    settlement_public_ids: list[str]
    status: str = "finalized"
    transaction_public_id: str | None = None
    audit_id: str | None = None
    idempotency_key: str = ""


# ---------------------------------------------------------------------------
# Content fingerprint
# ---------------------------------------------------------------------------


def build_finalization_content_fingerprint(
    fin_input: FinalizationInput,
) -> str:
    """Build a deterministic SHA-256 content fingerprint of the finalization.

    The fingerprint covers all material financial content: receipt group,
    calculation run, currency, payer, every settlement obligation, every
    participant, the snapshot totals, and source evidence refs.

    Amounts use ``canonical_decimal_str()`` for canonical serialization.
    Participant lists and evidence refs are sorted for determinism.
    """
    snapshot = fin_input.calculation_snapshot
    total_paid = snapshot.get("total_paid")
    payer_own = snapshot.get("payer_own_share")

    payload: dict[str, Any] = {
        "receipt_group_public_id": fin_input.receipt_group_public_id,
        "calculation_run_public_id": fin_input.calculation_run_public_id,
        "calculation_snapshot_id": fin_input.calculation_snapshot_id,
        "calculation_snapshot_hash": fin_input.calculation_snapshot_hash or None,
        "currency_contract_version": fin_input.currency_contract_version or None,
        "currency": fin_input.currency,
        "payer_participant_public_id": fin_input.payer_participant_public_id,
        "settlement_obligations": sorted(
            [
                {
                    "debtor": o.debtor_participant_public_id,
                    "creditor": o.creditor_participant_public_id,
                    "amount": canonical_decimal_str(o.amount),
                    "currency": o.currency,
                }
                for o in fin_input.settlement_obligations
            ],
            key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
        ),
        "participant_public_ids": sorted(fin_input.participant_public_ids),
        "total_paid": canonical_decimal_str(money_decimal(total_paid, label="total_paid"))
        if total_paid is not None
        else None,
        "payer_own_share": canonical_decimal_str(money_decimal(payer_own, label="payer_own_share"))
        if payer_own is not None
        else None,
        "source_evidence_refs": sorted(fin_input.source_evidence_refs),
    }
    if fin_input.active_fact_set_binding is not None:
        payload["active_fact_set_binding"] = (
            fin_input.active_fact_set_binding.as_fingerprint_payload()
        )
    if fin_input.confirmed_receipt_identity is not None:
        payload["confirmed_receipt_identity"] = (
            fin_input.confirmed_receipt_identity.as_fingerprint_payload()
        )
    if fin_input.receipt_group_materialization is not None:
        payload["receipt_group_materialization"] = (
            fin_input.receipt_group_materialization.as_fingerprint_payload()
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def to_settlement_obligations(
    raw_obligations: list[dict[str, Any]],
    currency: str,
) -> list[SettlementObligation]:
    return [
        SettlementObligation(
            debtor_participant_public_id=o["debtor"],
            creditor_participant_public_id=o["creditor"],
            amount=_money_decimal(o["amount"], "settlement obligation amount"),
            currency=_obligation_currency(o, currency),
        )
        for o in raw_obligations
    ]


# ---------------------------------------------------------------------------
# Participant-share and total reconciliation
# ---------------------------------------------------------------------------


def _validate_share_total_reconciliation(
    snapshot: dict[str, Any],
    participants: list[str],
    participant_set: set[str],
    payer: str,
    currency: str,
) -> None:
    """Validate participant shares, totals, payer-paid amounts, and
    single-payer contract invariants.  All comparisons use the authoritative
    currency's scale."""
    shares = snapshot.get("participant_shares", {})

    # ---- sum(shares) == total_paid ----
    total_paid = snapshot.get("total_paid")
    if total_paid is not None:
        total_paid_dec = _validated_amount(total_paid, currency, "calculation_snapshot.total_paid")
        shares_sum = ZERO
        for pub_id, amount in shares.items():
            share_dec = _validated_amount(
                amount,
                currency,
                f"calculation_snapshot.participant_shares[{pub_id!r}]",
            )
            shares_sum += share_dec
        shares_sum = _round_for_currency(shares_sum, currency)
        if shares_sum != _round_for_currency(total_paid_dec, currency):
            raise FinalizationValidationError(
                f"Participant shares sum {shares_sum} does not equal total_paid {total_paid_dec}"
            )

    # ---- payer_own_share == shares[payer] ----
    payer_own = snapshot.get("payer_own_share")
    if payer_own is not None and payer in shares:
        payer_own_dec = _validated_amount(
            payer_own, currency, "calculation_snapshot.payer_own_share"
        )
        payer_share_dec = _validated_amount(
            shares[payer],
            currency,
            f"calculation_snapshot.participant_shares[{payer!r}]",
        )
        if _round_for_currency(payer_own_dec, currency) != _round_for_currency(
            payer_share_dec, currency
        ):
            raise FinalizationValidationError(
                f"payer_own_share {payer_own_dec} does not equal "
                f"participant_shares[{payer!r}] = {payer_share_dec}"
            )

    # ---- total_participant_shares == participant_shares ----
    tps = snapshot.get("total_participant_shares")
    if isinstance(tps, dict) and tps:
        for pub_id in participant_set:
            expected = _validated_amount(
                tps.get(pub_id, ZERO),
                currency,
                f"calculation_snapshot.total_participant_shares[{pub_id!r}]",
            )
            actual = _validated_amount(
                shares.get(pub_id, ZERO),
                currency,
                f"calculation_snapshot.participant_shares[{pub_id!r}]",
            )
            if _round_for_currency(expected, currency) != _round_for_currency(actual, currency):
                raise FinalizationValidationError(
                    f"total_participant_shares[{pub_id!r}] = {expected} "
                    f"does not match participant_shares[{pub_id!r}] = {actual}"
                )

    # ---- payer_paid_amounts: keys are participants, sums to total_paid,
    #      single-payer contract ----
    ppa = snapshot.get("payer_paid_amounts")
    if isinstance(ppa, dict):
        ppa_sum = ZERO
        for key, val in ppa.items():
            if isinstance(key, str) and key not in participant_set:
                raise FinalizationValidationError(
                    f"calculation_snapshot.payer_paid_amounts key {key!r} "
                    f"is not in snapshot participants"
                )
            ppa_sum += _validated_amount(
                val,
                currency,
                f"calculation_snapshot.payer_paid_amounts[{key!r}]",
            )
        ppa_sum = _round_for_currency(ppa_sum, currency)
        if total_paid is not None and ppa_sum != _round_for_currency(total_paid_dec, currency):
            raise FinalizationValidationError(
                f"payer_paid_amounts sum {ppa_sum} does not equal total_paid {total_paid_dec}"
            )

        # single-payer contract: payer paid total_paid, all others paid 0
        for pub_id in participant_set:
            ppa_val = _validated_amount(
                ppa.get(pub_id, ZERO),
                currency,
                f"calculation_snapshot.payer_paid_amounts[{pub_id!r}]",
            )
            if pub_id == payer:
                if total_paid is not None and _round_for_currency(
                    ppa_val, currency
                ) != _round_for_currency(total_paid_dec, currency):
                    raise FinalizationValidationError(
                        f"payer_paid_amounts[{pub_id!r}] = {ppa_val} "
                        f"does not equal total_paid {total_paid_dec}"
                    )
            elif _round_for_currency(ppa_val, currency) != ZERO:
                raise FinalizationValidationError(
                    f"Non-payer {pub_id!r} has "
                    f"payer_paid_amounts[{pub_id!r}] = {ppa_val}; "
                    f"only payer {payer!r} can pay in the single-payer "
                    f"finalization contract"
                )


def _validate_balance_obligation_consistency(
    snapshot: dict[str, Any],
    participant_set: set[str],
    payer: str,
    currency: str,
) -> None:
    """Validate that paid-minus-share balances produce the exact submitted
    settlement obligations (single-payer contract).  All amounts are validated
    at the authoritative currency's scale."""
    shares = snapshot.get("participant_shares", {})
    ppa = snapshot.get("payer_paid_amounts", {})

    # Compute balances: paid - share
    balances: dict[str, Decimal] = {}
    for pub_id in participant_set:
        paid = _validated_amount(
            ppa.get(pub_id, ZERO),
            currency,
            f"payer_paid_amounts[{pub_id!r}]",
        )
        share = _validated_amount(
            shares.get(pub_id, ZERO),
            currency,
            f"participant_shares[{pub_id!r}]",
        )
        balances[pub_id] = _round_for_currency(paid - share, currency)

    # Balance zero-sum check
    balance_sum = _round_for_currency(sum(balances.values(), ZERO), currency)
    if balance_sum != ZERO:
        raise FinalizationValidationError(
            f"Balances (paid - share) do not sum to zero: {balance_sum}"
        )

    # Single-payer contract: all debtors owe the payer; payer is the sole creditor.
    debtors = {p: -b for p, b in balances.items() if b < ZERO}
    creditors = {p: b for p, b in balances.items() if b > ZERO}

    if not debtors and not creditors:
        # Every participant consumed exactly what they paid (single-owner /
        # payer-only receipt).  Zero settlement obligations is the correct
        # authoritative outcome; it must not block finalization.  Any snapshot
        # obligation under a zero-balance state is a contradiction.
        snapshot_obls = snapshot.get("settlement_obligations", [])
        if isinstance(snapshot_obls, list) and snapshot_obls:
            raise FinalizationValidationError(
                "Snapshot carries settlement obligations while every participant "
                "balance (paid - share) is zero"
            )
        return

    if len(creditors) != 1 or payer not in creditors:
        raise FinalizationValidationError(
            f"Single-payer contract requires exactly one creditor "
            f"(the payer {payer!r}); got creditors: {sorted(creditors.keys())}"
        )

    # Build expected obligations from balances
    expected_debtor_amounts: dict[str, Decimal] = {}
    for debtor, amount in sorted(debtors.items()):
        expected_debtor_amounts[debtor] = _round_for_currency(amount, currency)

    actual_debtor_amounts: dict[str, Decimal] = {}
    snapshot_obls = snapshot.get("settlement_obligations", [])
    if isinstance(snapshot_obls, list):
        for obl in snapshot_obls:
            if not isinstance(obl, dict):
                continue
            d = obl.get("debtor")
            c = obl.get("creditor")
            a = obl.get("amount")
            if isinstance(d, str) and d and isinstance(c, str) and c and a is not None:
                amt = _validated_amount(a, currency, f"obligation {d}->{c}")
                actual_debtor_amounts[d] = _round_for_currency(
                    actual_debtor_amounts.get(d, ZERO) + amt, currency
                )

    if expected_debtor_amounts != actual_debtor_amounts:
        raise FinalizationValidationError(
            f"Balance-derived debtor amounts "
            f"{_sorted_decimal_map(expected_debtor_amounts)} "
            f"do not match snapshot obligation amounts "
            f"{_sorted_decimal_map(actual_debtor_amounts)}"
        )


# ---------------------------------------------------------------------------
# Nested identity validation — fail closed
# ---------------------------------------------------------------------------

_NESTED_IDENTITY_FIELDS: list[tuple[str, str | None]] = [
    ("payer_paid_amounts", "dict"),
    ("payer_own_shares", "dict"),
    ("total_participant_shares", "dict"),
]


def _validate_nested_identity_closed(raw: dict[str, Any], participant_set: set[str]) -> None:
    """Validate every nested identity field: correct type, non-empty string,
    participant membership.  Fail closed on wrong types — never skip."""
    for field_name, container in _NESTED_IDENTITY_FIELDS:
        d = raw.get(field_name)
        if d is None:
            continue
        if container == "dict":
            if not isinstance(d, dict):
                raise FinalizationValidationError(
                    f"calculation_snapshot.{field_name} must be a mapping, got {type(d).__name__}"
                )
            for key in d:
                _require_non_empty_string(key, f"calculation_snapshot.{field_name} key")
                if key not in participant_set:
                    raise FinalizationValidationError(
                        f"calculation_snapshot.{field_name} key {key!r} "
                        f"is not in snapshot participants"
                    )

    # --- top-level obligations ---
    raw_obls = raw.get("settlement_obligations")
    if not isinstance(raw_obls, list):
        raise FinalizationValidationError(
            "calculation_snapshot.settlement_obligations must be a list"
        )
    for i, obl in enumerate(raw_obls):
        if not isinstance(obl, dict):
            raise FinalizationValidationError(
                f"calculation_snapshot.settlement_obligations[{i}] must be a mapping"
            )
        label = f"calculation_snapshot.settlement_obligations[{i}]"
        debtor = obl.get("debtor")
        creditor = obl.get("creditor")
        if debtor is not None:
            _require_non_empty_string(debtor, f"{label}.debtor")
            if debtor not in participant_set:
                raise FinalizationValidationError(
                    f"{label}.debtor {debtor!r} is not in snapshot participants"
                )
        if creditor is not None:
            _require_non_empty_string(creditor, f"{label}.creditor")
            if creditor not in participant_set:
                raise FinalizationValidationError(
                    f"{label}.creditor {creditor!r} is not in snapshot participants"
                )

    # --- receipts ---
    receipts = raw.get("receipts")
    if not isinstance(receipts, list):
        raise FinalizationValidationError("calculation_snapshot.receipts must be a list")
    for ri, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            raise FinalizationValidationError(
                f"calculation_snapshot.receipts[{ri}] must be a mapping"
            )
        rlabel = f"calculation_snapshot.receipts[{ri}]"

        paid_by = receipt.get("paid_by")
        if paid_by is not None:
            _require_non_empty_string(paid_by, f"{rlabel}.paid_by")
            if paid_by not in participant_set:
                raise FinalizationValidationError(
                    f"{rlabel}.paid_by {paid_by!r} is not in snapshot participants"
                )

        _validate_receipt_map_keys(receipt, rlabel, "item_shares", participant_set)
        _validate_receipt_map_keys(receipt, rlabel, "participant_shares", participant_set)

        items = receipt.get("items")
        if items is not None and not isinstance(items, list):
            raise FinalizationValidationError(f"{rlabel}.items must be a list")
        if isinstance(items, list):
            for ii, item in enumerate(items):
                if not isinstance(item, dict):
                    raise FinalizationValidationError(f"{rlabel}.items[{ii}] must be a mapping")
                ilabel = f"{rlabel}.items[{ii}]"
                _validate_receipt_map_keys(item, ilabel, "participant_allocations", participant_set)

        adjustments = receipt.get("adjustments")
        if adjustments is not None and not isinstance(adjustments, list):
            raise FinalizationValidationError(f"{rlabel}.adjustments must be a list")
        if isinstance(adjustments, list):
            for ai, adj in enumerate(adjustments):
                if not isinstance(adj, dict):
                    raise FinalizationValidationError(
                        f"{rlabel}.adjustments[{ai}] must be a mapping"
                    )
                alabel = f"{rlabel}.adjustments[{ai}]"
                _validate_receipt_map_keys(adj, alabel, "participant_allocations", participant_set)

        radjs = receipt.get("rounding_adjustments")
        if radjs is not None and not isinstance(radjs, list):
            raise FinalizationValidationError(f"{rlabel}.rounding_adjustments must be a list")
        if isinstance(radjs, list):
            for rai, ra in enumerate(radjs):
                if not isinstance(ra, dict):
                    raise FinalizationValidationError(
                        f"{rlabel}.rounding_adjustments[{rai}] must be a mapping"
                    )
                ra_participant = ra.get("participant")
                if ra_participant is not None:
                    _require_non_empty_string(
                        ra_participant,
                        f"{rlabel}.rounding_adjustments[{rai}].participant",
                    )
                    if ra_participant not in participant_set:
                        raise FinalizationValidationError(
                            f"{rlabel}.rounding_adjustments[{rai}].participant "
                            f"{ra_participant!r} is not in snapshot participants"
                        )

        rap = receipt.get("rounding_adjustment_participant")
        if rap is not None:
            _require_non_empty_string(rap, f"{rlabel}.rounding_adjustment_participant")
            if rap not in participant_set:
                raise FinalizationValidationError(
                    f"{rlabel}.rounding_adjustment_participant {rap!r} "
                    f"is not in snapshot participants"
                )


def _validate_receipt_map_keys(
    container: dict[str, Any],
    label: str,
    field: str,
    participant_set: set[str],
) -> None:
    d = container.get(field)
    if d is None:
        return
    if not isinstance(d, dict):
        raise FinalizationValidationError(f"{label}.{field} must be a mapping")
    for key in d:
        _require_non_empty_string(key, f"{label}.{field} key")
        if key not in participant_set:
            raise FinalizationValidationError(
                f"{label}.{field} key {key!r} is not in snapshot participants"
            )


def _require_non_empty_string(value: Any, label: str) -> None:
    if isinstance(value, bool):
        raise FinalizationValidationError(
            f"{label} must be a non-empty string, got boolean {value!r}"
        )
    if not isinstance(value, str):
        raise FinalizationValidationError(
            f"{label} must be a non-empty string, got {type(value).__name__}"
        )
    if value == "":
        raise FinalizationValidationError(f"{label} must be a non-empty string")


# ---------------------------------------------------------------------------
# Raw-input validation — runs BEFORE JSON canonicalization
# ---------------------------------------------------------------------------


def _validate_raw_snapshot_input(raw: dict[str, Any]) -> None:
    """Validate raw snapshot structure and identity values before JSON round-trip."""
    participants = raw.get("participants")
    if not isinstance(participants, list) or not participants:
        raise FinalizationValidationError(
            "calculation_snapshot.participants must be a non-empty list"
        )
    participant_set: set[str] = set()
    for i, pub_id in enumerate(participants):
        _require_non_empty_string(pub_id, f"calculation_snapshot.participants[{i}]")
        if pub_id in participant_set:
            raise FinalizationValidationError(
                f"calculation_snapshot.participants contains duplicate public ID: {pub_id!r}"
            )
        participant_set.add(pub_id)

    payer = raw.get("payer")
    _require_non_empty_string(payer, "calculation_snapshot.payer")

    shares = raw.get("participant_shares")
    if not isinstance(shares, dict):
        raise FinalizationValidationError(
            "calculation_snapshot.participant_shares must be a mapping"
        )
    for key in shares:
        _require_non_empty_string(key, "calculation_snapshot.participant_shares key")

    raw_obls = raw.get("settlement_obligations")
    if not isinstance(raw_obls, list):
        raise FinalizationValidationError(
            "calculation_snapshot.settlement_obligations must be a list"
        )

    _validate_nested_identity_closed(raw, participant_set)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obligation_currency(raw_obligation: dict[str, Any], fallback_currency: str) -> str:
    raw_currency = raw_obligation.get("currency", fallback_currency)
    if raw_currency != fallback_currency:
        raise FinalizationValidationError(
            f"Obligation currency {raw_currency!r} does not match "
            f"finalization currency {fallback_currency!r}"
        )
    return fallback_currency


def _canonical_from_obligation(obligation: SettlementObligation) -> CanonicalObligation:
    return (
        obligation.debtor_participant_public_id,
        obligation.creditor_participant_public_id,
        obligation.amount,
        obligation.currency,
    )


def _canonical_from_mapping(value: Any, label: str) -> CanonicalObligation:
    if not isinstance(value, dict):
        raise FinalizationValidationError(f"{label} must be a mapping")
    try:
        debtor = value["debtor"]
        creditor = value["creditor"]
        amount = value["amount"]
        currency = value["currency"]
    except KeyError as exc:
        raise FinalizationValidationError(
            f"{label} missing required field {exc.args[0]!r}"
        ) from exc
    if not isinstance(debtor, str) or not debtor:
        raise FinalizationValidationError(f"{label}.debtor is required")
    if not isinstance(creditor, str) or not creditor:
        raise FinalizationValidationError(f"{label}.creditor is required")
    if not isinstance(currency, str) or not currency:
        raise FinalizationValidationError(f"{label}.currency is required")
    return (debtor, creditor, _money_decimal(amount, f"{label}.amount"), currency)


def _money_decimal(value: Any, label: str) -> Decimal:
    """Convert a value to Decimal via the shared Money Contract.

    Re-raises ``MoneyValidationError`` as ``FinalizationValidationError``
    to preserve the existing error contract.
    """
    try:
        return money_decimal(value, label=label)
    except MoneyValidationError as exc:
        raise FinalizationValidationError(str(exc)) from exc


def _validated_amount(value: Any, currency: str, label: str) -> Decimal:
    """Validate an authoritative monetary input against the currency scale.

    Rejects: float, bool, None, NaN, Infinity, malformed strings,
    scientific notation, and sub-minor-unit precision for the currency.
    """
    try:
        amount = money_decimal(value, label=label)
        validate_amount_for_currency(amount, currency, label=label)
        return amount
    except MoneyValidationError as exc:
        raise FinalizationValidationError(str(exc)) from exc


def _round_for_currency(value: Decimal, currency: str) -> Decimal:
    """Deliberately quantize a computed intermediate to the currency's scale."""
    return quantize_for_currency(value, currency)


def _sorted_decimal_map(d: dict[str, Decimal]) -> dict[str, str]:
    return {k: str(v) for k, v in sorted(d.items())}


def _snapshot_decimal_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return canonical_decimal_str(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def _validate_evidence_refs(refs: tuple[str, ...]) -> None:
    """Validate evidence references at model construction.

    Every item must be a non-empty string.  Duplicates are rejected.
    No value is silently coerced or deduplicated.
    """
    seen: set[str] = set()
    for i, ref in enumerate(refs):
        if not isinstance(ref, str):
            raise FinalizationValidationError(
                f"source_evidence_refs[{i}] must be a string, got {type(ref).__name__}: {ref!r}"
            )
        if ref == "":
            raise FinalizationValidationError(f"source_evidence_refs[{i}] is an empty string")
        if ref in seen:
            raise FinalizationValidationError(
                f"source_evidence_refs contains duplicate value {ref!r}"
            )
        seen.add(ref)
