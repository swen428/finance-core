"""Guarded receipt item & allocation facts persistence boundary (IAF.2/IAF.3).

Persists one complete, human-authored item/allocation/adjustment fact set
for exactly one eligible B4.1 conversion-created receipt: one append-only
fact-set registry row (migration 036), fact-set-bound ``receipt_items``
rows, ``receipt_item_allocation_facts`` rows, fact-set-bound
``receipt_adjustments`` rows, and one append-only financial audit event —
atomically, idempotently, inside a single service-owned ``BEGIN
IMMEDIATE`` transaction.

IAF.3 adds a separate, human-authored complete-replacement correction
command.  It never patches or edits an existing fact set: the predecessor
is transitioned once to point at a newly inserted successor, and every
predecessor row remains immutable history.

The binding design contract lives in
``docs/design/receipt_item_allocation_facts_boundary_v1.md`` (Sections
4–14, 17–18; approved decisions IA-D1 through IA-D12).  Positive
readiness, the calculator projection, and the calculation bridge remain
later slices.  The service never runs the calculator, never creates
calculation, snapshot, transaction, settlement, or reconciliation rows,
and never mutates the B4.1 receipt, membership, conversion registry,
proposal, confirmation, OCR, attachment, or raw-intake evidence.

Every monetary value is a human-authored canonical decimal string that
must pass the Money Contract, the exact Section 8 reconciliation against
the receipt's authoritative net-paid canonical text, and the NUMERIC
compatibility-mirror losslessness check before any write.  Nothing is
inferred, defaulted, or fabricated.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from finance_core.calculation.authoritative_snapshot import canonical_json_text
from finance_core.financial_audit import (
    AuditChainTransactionError,
    AuditEventCommand,
    AuditVerificationError,
    FinancialAuditEvent,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.money import (
    MoneyValidationError,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.receipt_facts_conversion import (
    NEVER_WRITTEN_RECEIPT_COLUMNS,
    decimal_from_numeric_mirror,
    derive_receipt_public_id,
)
from finance_core.sqlite_connection import ForeignKeysDisabledError, require_foreign_keys_enabled
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

# ---------------------------------------------------------------------------
# Public errors (Section 17 taxonomy)
# ---------------------------------------------------------------------------


class ReceiptItemAllocationFactsError(ValueError):
    """Base error for receipt item & allocation facts persistence failures."""


class InvalidItemFactsCommandError(ReceiptItemAllocationFactsError):
    """The command is malformed: bad ID pattern, unknown fields, bad lists."""


class UnauthorizedItemFactsActorError(ReceiptItemAllocationFactsError):
    """The command did not identify an authenticated human actor."""


class ItemFactsReceiptNotFoundError(ReceiptItemAllocationFactsError):
    """The referenced receipt does not exist (or is not unique)."""


class UnsupportedItemFactsReceiptProvenanceError(ReceiptItemAllocationFactsError):
    """The receipt is not registry-bound through the guarded B4.1 conversion."""


class StaleItemFactsReceiptBindingError(ReceiptItemAllocationFactsError):
    """The asserted receipt/conversion state no longer matches, or downstream
    finalization/calculation authority already binds the receipt."""


class ItemFactSetAlreadyExistsError(ReceiptItemAllocationFactsError):
    """A fact set already exists for the receipt (create command)."""


class StaleItemFactSetVersionError(ReceiptItemAllocationFactsError):
    """The correction command's expected active fact-set state is stale."""


class IncompleteItemFactsError(ReceiptItemAllocationFactsError):
    """A required item/allocation/adjustment fact input is missing."""


class InvalidItemFactsMoneyError(ReceiptItemAllocationFactsError):
    """A monetary value failed the Money Contract or mirror losslessness."""


class AmbiguousItemAllocationError(ReceiptItemAllocationFactsError):
    """An allocation is ambiguous or contradictory (participants, shares)."""


class UnsupportedAllocationRuleError(ReceiptItemAllocationFactsError):
    """An allocation or adjustment rule/method/direction/type is unsupported."""


class ItemFactsReconciliationError(ReceiptItemAllocationFactsError):
    """Exact-equality reconciliation failed (shares, line, or fact-set total)."""


class ItemFactsEvidenceLineageError(ReceiptItemAllocationFactsError):
    """B4.1 integrity, evidence lineage, audit chain, or ad-hoc-row failure."""


class ItemFactsIdempotencyConflictError(ReceiptItemAllocationFactsError):
    """The same command public ID exists with different canonical material."""


class ItemFactsPersistenceError(ReceiptItemAllocationFactsError):
    """Constraint violation, busy/locked, verification mismatch, replay drift."""


class ItemFactsCallerOwnedTransactionError(ReceiptItemAllocationFactsError):
    """The connection already carries a caller-owned pending transaction."""


class ItemFactsStagingDatabaseRejectedError(ReceiptItemAllocationFactsError):
    """The staging guard rejected the database (live database, copies)."""


class ItemFactsForeignKeysDisabledError(ReceiptItemAllocationFactsError):
    """The connection does not enforce SQLite foreign keys (fail closed)."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ITEM_ALLOCATION_FACTS_SCHEMA_VERSION = "v1"
RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE = "receipt_item_allocation_facts_persisted"
RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE = "receipt_item_allocation_facts_superseded"

# Frozen audit-event payload field list for the persisted event (design
# Section 14.1).  The payload carries exactly these fields, no more, no
# fewer; the contract is test-asserted field-by-field.
RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_PAYLOAD_FIELDS = (
    "actor_type",
    "adjustment_count",
    "allocation_count",
    "authenticated_actor_id",
    "channel",
    "command_material_hash",
    "command_public_id",
    "conversion_command_public_id",
    "conversion_result_hash",
    "fact_set_input_hash",
    "fact_set_public_id",
    "fact_set_result_hash",
    "fact_set_version",
    "item_count",
    "receipt_public_id",
    "supersedes_fact_set_public_id",
)

# Frozen audit-event payload field list for the IAF.3 supersession event.
# It is the create payload plus the predecessor's result hash.  Together,
# ``supersedes_fact_set_public_id`` + ``superseded_fact_set_result_hash``
# bind the old version while the ordinary fact-set fields bind the complete
# successor.
RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_PAYLOAD_FIELDS = (
    "actor_type",
    "adjustment_count",
    "allocation_count",
    "authenticated_actor_id",
    "channel",
    "command_material_hash",
    "command_public_id",
    "conversion_command_public_id",
    "conversion_result_hash",
    "fact_set_input_hash",
    "fact_set_public_id",
    "fact_set_result_hash",
    "fact_set_version",
    "item_count",
    "receipt_public_id",
    "superseded_fact_set_result_hash",
    "supersedes_fact_set_public_id",
)

_COMMAND_ID_RE = re.compile(r"^riaf_[A-Za-z0-9_-]{1,195}$")
_CORRECTION_COMMAND_ID_RE = re.compile(r"^riafc_[A-Za-z0-9_-]{1,194}$")
_CONVERSION_COMMAND_ID_RE = re.compile(r"^rpfc_[A-Za-z0-9_-]{1,195}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_QUANTITY_RE = re.compile(r"^[1-9][0-9]{0,14}$")
# The 15-digit quantity bound keeps every quantity exactly representable
# by the SQLite NUMERIC compatibility mirror (below 2**53); larger values
# are rejected as malformed commands, never written and then refused.

_EXPECTED_CURRENT_FACT_SET_NONE = "none"

# Approved IA-D7 / IA-D7b vocabularies.  Values outside these sets are
# rejected typed (UnsupportedAllocationRuleError), never silently mapped.
# Public: the SELECT-only readiness/projection boundaries re-verify
# persisted fact sets against the same frozen vocabularies (Section 15).
ITEM_ALLOCATION_METHODS = frozenset({"equal_amount", "manual"})
ADJUSTMENT_TYPES = frozenset(
    {"service_charge", "gst", "discount", "voucher", "cashback", "promo", "manual_adjustment"}
)
ADJUSTMENT_DIRECTIONS = frozenset({"add", "subtract"})
ADJUSTMENT_ALLOCATION_METHODS = frozenset(
    {"equal_per_participant", "proportional_by_item_amount", "manual", "payer_only"}
)

# Approved Section 6 bounds; oversize fails closed.
_MAX_ITEMS = 200
_MAX_ITEM_NAME_LENGTH = 200
_MAX_REASON_LENGTH = 500
_MAX_DESCRIPTION_LENGTH = 500
_MAX_ADJUSTMENTS = 200
_MAX_PARTICIPANTS_PER_ENTRY = 200

_COMMAND_FIELDS = frozenset(
    {
        "command_public_id",
        "receipt_public_id",
        "expected_conversion_command_public_id",
        "expected_conversion_result_hash",
        "expected_current_fact_set",
        "items",
        "allocations",
        "adjustments",
        "authenticated_actor_id",
        "actor_type",
        "channel",
        "reason",
        "schema_version",
    }
)
_REQUIRED_COMMAND_FIELDS = frozenset(
    {
        "command_public_id",
        "receipt_public_id",
        "expected_conversion_command_public_id",
        "expected_conversion_result_hash",
        "expected_current_fact_set",
        "items",
        "allocations",
        "adjustments",
        "authenticated_actor_id",
        "actor_type",
        "channel",
        "schema_version",
    }
)

_ITEM_FIELDS = frozenset(
    {"line_number", "item_name", "quantity", "unit_price", "line_amount", "currency"}
)
_ITEM_REQUIRED_FIELDS = frozenset({"line_number", "item_name", "line_amount", "currency"})
_ALLOCATION_FIELDS = frozenset({"line_number", "allocation_method", "participants"})
_ALLOCATION_PARTICIPANT_FIELDS = frozenset({"participant_public_id", "share_amount", "currency"})
_ADJUSTMENT_FIELDS = frozenset(
    {
        "adjustment_index",
        "adjustment_type",
        "amount",
        "currency",
        "direction",
        "allocation_method",
        "description",
        "participants",
    }
)
_ADJUSTMENT_REQUIRED_FIELDS = frozenset(
    {"adjustment_index", "adjustment_type", "amount", "currency", "direction", "allocation_method"}
)
_MANUAL_PARTICIPANT_REQUIRED_FIELDS = frozenset(
    {"participant_public_id", "share_amount", "currency"}
)

# Receipt columns a B4.1 facts-only conversion never writes, minus
# transaction_id: a set transaction_id means finalization authority exists
# and maps to StaleItemFactsReceiptBindingError at guard 10, not to the
# guard 8 integrity error.
_B41_NEVER_WRITTEN_COLUMNS = tuple(
    name for name in NEVER_WRITTEN_RECEIPT_COLUMNS if name != "transaction_id"
)

_ROLE_PAYER = "payer"
_ROLE_PARTICIPANT = "participant"
_ROLE_EXCLUDED = "excluded"

_REPLAY_DRIFT_MESSAGE = (
    "Guard 5 replay refused: the persisted fact-set state no longer matches its registered binding"
)

# ---------------------------------------------------------------------------
# Test-only failure seam
# ---------------------------------------------------------------------------

_failure_injection_hook: Callable[[str], None] | None = None
"""Private test-only failure seam at real fact-set write boundaries."""

FAILURE_INJECTION_STAGES = (
    "before_fact_set_registry_insert",
    "before_items_insert",
    "before_allocations_insert",
    "before_adjustments_insert",
    "before_audit_append",
    "before_persisted_verification",
    "before_commit",
)
SUPERSESSION_FAILURE_INJECTION_STAGES = (
    "before_supersession_transition",
    *FAILURE_INJECTION_STAGES,
)


def _inject_failure(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


# ---------------------------------------------------------------------------
# Command and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptItemAllocationFactsCommand:
    """Immutable authenticated human fact-set create command (Section 5.1).

    Every monetary value is a human-authored canonical decimal string; the
    service never generates command identity, expectations, or hashes on
    the caller's behalf.  ``expected_current_fact_set`` must be the literal
    string ``"none"`` in the create command.
    """

    command_public_id: str
    receipt_public_id: str
    expected_conversion_command_public_id: str
    expected_conversion_result_hash: str
    expected_current_fact_set: str
    items: Sequence[Mapping[str, object]]
    allocations: Sequence[Mapping[str, object]]
    adjustments: Sequence[Mapping[str, object]]
    authenticated_actor_id: str
    channel: str
    actor_type: str = "human"
    reason: str | None = None
    schema_version: str = ITEM_ALLOCATION_FACTS_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "ReceiptItemAllocationFactsCommand":
        """Build a command from a mapping, rejecting unknown fields fail-closed."""
        if not isinstance(data, Mapping):
            raise InvalidItemFactsCommandError("Fact-set command must be a mapping")
        unknown = sorted(set(data) - _COMMAND_FIELDS)
        if unknown:
            raise InvalidItemFactsCommandError(
                f"Unknown fact-set command fields are rejected: {unknown}"
            )
        missing = sorted(_REQUIRED_COMMAND_FIELDS - set(data))
        if missing:
            raise InvalidItemFactsCommandError(
                f"Fact-set command is missing required fields: {missing}"
            )

        def _tuple(value: object) -> object:
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return tuple(value)
            return value

        return cls(
            command_public_id=data["command_public_id"],  # type: ignore[arg-type]
            receipt_public_id=data["receipt_public_id"],  # type: ignore[arg-type]
            expected_conversion_command_public_id=data[  # type: ignore[arg-type]
                "expected_conversion_command_public_id"
            ],
            expected_conversion_result_hash=data[  # type: ignore[arg-type]
                "expected_conversion_result_hash"
            ],
            expected_current_fact_set=data["expected_current_fact_set"],  # type: ignore[arg-type]
            items=_tuple(data["items"]),  # type: ignore[arg-type]
            allocations=_tuple(data["allocations"]),  # type: ignore[arg-type]
            adjustments=_tuple(data["adjustments"]),  # type: ignore[arg-type]
            authenticated_actor_id=data["authenticated_actor_id"],  # type: ignore[arg-type]
            channel=data["channel"],  # type: ignore[arg-type]
            actor_type=data.get("actor_type", "human"),  # type: ignore[arg-type]
            reason=data.get("reason"),  # type: ignore[arg-type]
            schema_version=data.get(  # type: ignore[arg-type]
                "schema_version", ITEM_ALLOCATION_FACTS_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class ReceiptItemAllocationFactsSupersessionCommand:
    """Immutable complete-replacement correction command (IAF.3).

    The caller supplies the exact active predecessor public ID and result
    hash they reviewed.  The service never substitutes a newer version and
    never retries a stale command.
    """

    command_public_id: str
    receipt_public_id: str
    expected_conversion_command_public_id: str
    expected_conversion_result_hash: str
    expected_current_fact_set_public_id: str
    expected_current_fact_set_result_hash: str
    items: Sequence[Mapping[str, object]]
    allocations: Sequence[Mapping[str, object]]
    adjustments: Sequence[Mapping[str, object]]
    authenticated_actor_id: str
    channel: str
    actor_type: str = "human"
    reason: str | None = None
    schema_version: str = ITEM_ALLOCATION_FACTS_SCHEMA_VERSION

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object]
    ) -> "ReceiptItemAllocationFactsSupersessionCommand":
        """Build a correction command, rejecting unknown fields fail-closed."""
        if not isinstance(data, Mapping):
            raise InvalidItemFactsCommandError("Fact-set correction command must be a mapping")
        fields = _COMMAND_FIELDS - {"expected_current_fact_set"} | {
            "expected_current_fact_set_public_id",
            "expected_current_fact_set_result_hash",
        }
        required = _REQUIRED_COMMAND_FIELDS - {"expected_current_fact_set"} | {
            "expected_current_fact_set_public_id",
            "expected_current_fact_set_result_hash",
        }
        unknown = sorted(set(data) - fields)
        if unknown:
            raise InvalidItemFactsCommandError(
                f"Unknown fact-set correction command fields are rejected: {unknown}"
            )
        missing = sorted(required - set(data))
        if missing:
            raise InvalidItemFactsCommandError(
                f"Fact-set correction command is missing required fields: {missing}"
            )

        def _tuple(value: object) -> object:
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return tuple(value)
            return value

        return cls(
            command_public_id=data["command_public_id"],  # type: ignore[arg-type]
            receipt_public_id=data["receipt_public_id"],  # type: ignore[arg-type]
            expected_conversion_command_public_id=data[  # type: ignore[arg-type]
                "expected_conversion_command_public_id"
            ],
            expected_conversion_result_hash=data[  # type: ignore[arg-type]
                "expected_conversion_result_hash"
            ],
            expected_current_fact_set_public_id=data[  # type: ignore[arg-type]
                "expected_current_fact_set_public_id"
            ],
            expected_current_fact_set_result_hash=data[  # type: ignore[arg-type]
                "expected_current_fact_set_result_hash"
            ],
            items=_tuple(data["items"]),  # type: ignore[arg-type]
            allocations=_tuple(data["allocations"]),  # type: ignore[arg-type]
            adjustments=_tuple(data["adjustments"]),  # type: ignore[arg-type]
            authenticated_actor_id=data["authenticated_actor_id"],  # type: ignore[arg-type]
            channel=data["channel"],  # type: ignore[arg-type]
            actor_type=data.get("actor_type", "human"),  # type: ignore[arg-type]
            reason=data.get("reason"),  # type: ignore[arg-type]
            schema_version=data.get(  # type: ignore[arg-type]
                "schema_version", ITEM_ALLOCATION_FACTS_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class ReceiptItemAllocationFactsResult:
    """Immutable deterministic persistence result (identical on replay)."""

    command_public_id: str
    receipt_public_id: str
    receipt_id: int
    fact_set_public_id: str
    fact_set_version: int
    conversion_command_public_id: str
    command_material_hash: str
    fact_set_input_hash: str
    fact_set_result_hash: str
    item_count: int
    allocation_count: int
    adjustment_count: int
    audit_event_public_id: str
    idempotent: bool
    supersedes_fact_set_public_id: str | None = None
    superseded_fact_set_result_hash: str | None = None


# ---------------------------------------------------------------------------
# Deterministic identity derivation (Section 12.4, frozen)
# ---------------------------------------------------------------------------


def derive_fact_set_public_id(command_public_id: str) -> str:
    """Frozen deterministic derivation: ``rfs_`` + truncated SHA-256."""
    digest = _sha256_hex(f"receipt-item-allocation-fact-set:{command_public_id}")
    return f"rfs_{digest[:32]}"


def derive_item_public_id(fact_set_public_id: str, line_number: int) -> str:
    digest = _sha256_hex(f"receipt-fact-set-item:{fact_set_public_id}:{line_number}")
    return f"rfsi_{digest[:32]}"


def derive_allocation_public_id(item_public_id: str, participant_public_id: str) -> str:
    digest = _sha256_hex(f"receipt-fact-set-allocation:{item_public_id}:{participant_public_id}")
    return f"rfsa_{digest[:32]}"


def derive_adjustment_public_id(fact_set_public_id: str, adjustment_index: int) -> str:
    digest = _sha256_hex(f"receipt-fact-set-adjustment:{fact_set_public_id}:{adjustment_index}")
    return f"rfsj_{digest[:32]}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def persist_receipt_item_allocation_facts(
    conn: sqlite3.Connection,
    command: ReceiptItemAllocationFactsCommand,
    *,
    clock: Callable[[], str] | None = None,
) -> ReceiptItemAllocationFactsResult:
    """Atomically persist one authorized item/allocation fact set (create).

    Runs the Section 4.1 guards 1–15 in normative order, then writes the
    fact-set registry row, the fact-set-bound ``receipt_items`` rows, the
    ``receipt_item_allocation_facts`` rows, the fact-set-bound
    ``receipt_adjustments`` rows, and one financial audit event — in that
    fixed order — inside a single service-owned ``BEGIN IMMEDIATE``
    transaction.  Same command replays idempotently after replay-time
    integrity verification; any failure rolls back completely.
    """
    spec = _validate_command(command)  # guard 1
    _require_staging(conn)  # guards 2-3
    _acquire_write_transaction(conn)  # guard 4
    try:
        material_hash = _command_material_hash(command, spec)

        existing = _get_registry_row(conn, command.command_public_id)
        if existing is not None:  # guard 5
            if str(existing["command_material_hash"]) != material_hash:
                raise ItemFactsIdempotencyConflictError(
                    f"Fact-set command {command.command_public_id} already "
                    "exists with different canonical material"
                )
            result = _verify_replay(conn, existing)
            conn.commit()
            return result

        receipt = _require_receipt(conn, command.receipt_public_id)  # guard 6
        conversion = _require_conversion_binding(conn, receipt)  # guard 7
        membership = _require_b41_integrity(conn, receipt, conversion)  # guard 8
        _require_asserted_binding(command, conversion)  # binding staleness
        _require_confirmed_unfinalized(receipt)  # guards 9-10
        _require_no_calculation_authority(conn, receipt)  # guard 11
        _require_no_existing_fact_set(conn, receipt)  # guard 12
        _require_no_adhoc_rows(conn, receipt)  # guard 13
        facts = _require_complete_facts(conn, spec, receipt, membership)  # guard 14
        lineage = _require_evidence_lineage(conn, conversion, receipt)  # guard 15

        fact_set_public_id = derive_fact_set_public_id(command.command_public_id)
        payload_text = _canonical_json(_input_material(str(receipt["public_id"]), facts))
        input_hash = _sha256_hex(payload_text)
        identities = _derive_row_identities(fact_set_public_id, facts)
        result_hash = _fact_set_result_hash(
            receipt_public_id=str(receipt["public_id"]),
            facts=facts,
            fact_set_public_id=fact_set_public_id,
            conversion_command_public_id=str(conversion["command_public_id"]),
            command_material_hash=material_hash,
            identities=identities,
        )
        now = _now(clock)
        audit_event_public_id = derive_audit_event_public_id(
            aggregate_type="receipt",
            aggregate_public_id=str(receipt["public_id"]),
            event_type=RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE,
            causation_public_id=command.command_public_id,
        )

        _inject_failure("before_fact_set_registry_insert")
        _insert_registry_row(
            conn,
            command=command,
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            conversion_command_public_id=str(conversion["command_public_id"]),
            expected_conversion_result_hash=str(conversion["conversion_result_hash"]),
            command_material_hash=material_hash,
            fact_set_input_hash=input_hash,
            fact_set_result_hash=result_hash,
            canonical_fact_set_payload=payload_text,
            audit_event_public_id=audit_event_public_id,
            created_at=now,
        )

        _inject_failure("before_items_insert")
        item_rowids = _insert_items(
            conn,
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            facts=facts,
            identities=identities,
        )

        _inject_failure("before_allocations_insert")
        _insert_allocations(
            conn,
            fact_set_public_id=fact_set_public_id,
            facts=facts,
            identities=identities,
            item_rowids=item_rowids,
        )

        _inject_failure("before_adjustments_insert")
        _insert_adjustments(
            conn,
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            facts=facts,
            identities=identities,
        )

        counts = _fact_counts(facts)

        _inject_failure("before_audit_append")
        audit_event = _append_fact_set_audit(
            conn,
            command=command,
            receipt_public_id=str(receipt["public_id"]),
            conversion=conversion,
            lineage=lineage,
            fact_set_public_id=fact_set_public_id,
            material_hash=material_hash,
            input_hash=input_hash,
            result_hash=result_hash,
            counts=counts,
            created_at=now,
        )

        _inject_failure("before_persisted_verification")
        _verify_persisted(
            conn,
            command=command,
            receipt=receipt,
            conversion=conversion,
            membership=membership,
            facts=facts,
            fact_set_public_id=fact_set_public_id,
            material_hash=material_hash,
            input_hash=input_hash,
            result_hash=result_hash,
            payload_text=payload_text,
            audit_event=audit_event,
            now=now,
        )

        _inject_failure("before_commit")
        conn.commit()
        return ReceiptItemAllocationFactsResult(
            command_public_id=command.command_public_id,
            receipt_public_id=str(receipt["public_id"]),
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            fact_set_version=1,
            conversion_command_public_id=str(conversion["command_public_id"]),
            command_material_hash=material_hash,
            fact_set_input_hash=input_hash,
            fact_set_result_hash=result_hash,
            item_count=counts["item_count"],
            allocation_count=counts["allocation_count"],
            adjustment_count=counts["adjustment_count"],
            audit_event_public_id=audit_event.event_public_id,
            idempotent=False,
        )
    except ReceiptItemAllocationFactsError:
        _rollback_if_needed(conn)
        raise
    except (AuditVerificationError, AuditChainTransactionError) as exc:
        # Audit-chain conflicts and audit transaction-context failures are
        # persistence failures of this fact set: translate them into the
        # Section 17 taxonomy after a full rollback, preserving the cause.
        _rollback_if_needed(conn)
        raise ItemFactsPersistenceError(
            "Fact-set audit event could not be appended atomically"
        ) from exc
    except sqlite3.Error as exc:
        _rollback_if_needed(conn)
        raise ItemFactsPersistenceError(
            "Receipt item/allocation fact set could not be persisted atomically"
        ) from exc
    except BaseException:
        _rollback_if_needed(conn)
        raise


def supersede_receipt_item_allocation_facts(
    conn: sqlite3.Connection,
    command: ReceiptItemAllocationFactsSupersessionCommand,
    *,
    clock: Callable[[], str] | None = None,
) -> ReceiptItemAllocationFactsResult:
    """Atomically supersede the active fact set with a complete replacement.

    The frozen IAF.3 sequence is enforced exactly: acquire ``BEGIN
    IMMEDIATE``; revalidate the receipt, conversion, evidence, predecessor,
    and replacement payload; transition the predecessor first with a
    conditional NULL-guarded update; insert the complete successor and its
    rows; append one supersession audit event; verify the full lineage; and
    commit.  No predecessor content row is updated or deleted.
    """
    spec = _validate_supersession_command(command)  # guard 1
    _require_staging(conn)  # guards 2-3
    _acquire_write_transaction(conn)  # guard 4
    try:
        material_hash = _supersession_command_material_hash(command, spec)

        existing = _get_registry_row(conn, command.command_public_id)
        if existing is not None:  # guard 5
            if str(existing["command_material_hash"]) != material_hash:
                raise ItemFactsIdempotencyConflictError(
                    f"Fact-set correction command {command.command_public_id} "
                    "already exists with different canonical material"
                )
            result = _verify_replay(conn, existing)
            conn.commit()
            return result

        receipt = _require_receipt(conn, command.receipt_public_id)  # guard 6
        conversion = _require_conversion_binding(conn, receipt)  # guard 7
        membership = _require_b41_integrity(conn, receipt, conversion)  # guard 8
        _require_asserted_binding(command, conversion)
        _require_confirmed_unfinalized(receipt)  # guards 9-10
        _require_no_finalization_authority(conn, receipt)
        predecessor = _require_expected_active_fact_set(
            conn, receipt, command
        )  # correction guard 12
        predecessor_drift = (
            "The expected predecessor fact set failed persisted-state "
            "verification; correction refused"
        )
        try:
            _verify_fact_set_state(
                conn,
                predecessor,
                require_chain_head=False,
                drift_message=predecessor_drift,
            )
        except ReceiptItemAllocationFactsError:
            raise
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ItemFactsPersistenceError(predecessor_drift) from exc
        _require_no_untrusted_rows(conn, receipt)  # correction guard 13
        facts = _require_complete_facts(conn, spec, receipt, membership)  # guard 14
        lineage = _require_evidence_lineage(conn, conversion, receipt)  # guard 15

        fact_set_public_id = derive_fact_set_public_id(command.command_public_id)
        version = int(predecessor["version"]) + 1
        payload_text = _canonical_json(_input_material(str(receipt["public_id"]), facts))
        input_hash = _sha256_hex(payload_text)
        identities = _derive_row_identities(fact_set_public_id, facts)
        result_hash = _fact_set_result_hash(
            receipt_public_id=str(receipt["public_id"]),
            facts=facts,
            fact_set_public_id=fact_set_public_id,
            version=version,
            conversion_command_public_id=str(conversion["command_public_id"]),
            command_material_hash=material_hash,
            identities=identities,
        )
        now = _now(clock)
        audit_event_public_id = derive_audit_event_public_id(
            aggregate_type="receipt",
            aggregate_public_id=str(receipt["public_id"]),
            event_type=RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE,
            causation_public_id=command.command_public_id,
        )

        # Frozen Section 11.1 order: transition first, successor insert
        # second.  The successor public ID is deterministic and known before
        # this transition; its FK is deferred until COMMIT.
        _inject_failure("before_supersession_transition")
        transition = conn.execute(
            "UPDATE receipt_item_allocation_fact_sets "
            "SET superseded_by_fact_set_public_id = ? "
            "WHERE fact_set_public_id = ? "
            "AND receipt_id = ? "
            "AND version = ? "
            "AND fact_set_result_hash = ? "
            "AND superseded_by_fact_set_public_id IS NULL",
            (
                fact_set_public_id,
                predecessor["fact_set_public_id"],
                receipt["id"],
                predecessor["version"],
                command.expected_current_fact_set_result_hash,
            ),
        )
        if transition.rowcount != 1:
            raise StaleItemFactSetVersionError(
                "The expected predecessor was superseded or changed before "
                "the correction transition; re-review and issue a new command"
            )

        _inject_failure("before_fact_set_registry_insert")
        _insert_registry_row(
            conn,
            command=command,
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            version=version,
            conversion_command_public_id=str(conversion["command_public_id"]),
            expected_conversion_result_hash=str(conversion["conversion_result_hash"]),
            supersedes_fact_set_public_id=str(predecessor["fact_set_public_id"]),
            command_material_hash=material_hash,
            fact_set_input_hash=input_hash,
            fact_set_result_hash=result_hash,
            canonical_fact_set_payload=payload_text,
            audit_event_public_id=audit_event_public_id,
            created_at=now,
        )

        _inject_failure("before_items_insert")
        item_rowids = _insert_items(
            conn,
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            facts=facts,
            identities=identities,
        )
        _inject_failure("before_allocations_insert")
        _insert_allocations(
            conn,
            fact_set_public_id=fact_set_public_id,
            facts=facts,
            identities=identities,
            item_rowids=item_rowids,
        )
        _inject_failure("before_adjustments_insert")
        _insert_adjustments(
            conn,
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            facts=facts,
            identities=identities,
        )

        counts = _fact_counts(facts)
        _inject_failure("before_audit_append")
        audit_event = _append_supersession_audit(
            conn,
            command=command,
            receipt_public_id=str(receipt["public_id"]),
            conversion=conversion,
            lineage=lineage,
            predecessor=predecessor,
            fact_set_public_id=fact_set_public_id,
            version=version,
            material_hash=material_hash,
            input_hash=input_hash,
            result_hash=result_hash,
            counts=counts,
            created_at=now,
        )

        _inject_failure("before_persisted_verification")
        _verify_supersession_persisted(
            conn,
            command=command,
            receipt=receipt,
            conversion=conversion,
            membership=membership,
            facts=facts,
            predecessor=predecessor,
            fact_set_public_id=fact_set_public_id,
            version=version,
            material_hash=material_hash,
            input_hash=input_hash,
            result_hash=result_hash,
            payload_text=payload_text,
            audit_event=audit_event,
            now=now,
        )

        _inject_failure("before_commit")
        conn.commit()
        return ReceiptItemAllocationFactsResult(
            command_public_id=command.command_public_id,
            receipt_public_id=str(receipt["public_id"]),
            receipt_id=int(receipt["id"]),
            fact_set_public_id=fact_set_public_id,
            fact_set_version=version,
            conversion_command_public_id=str(conversion["command_public_id"]),
            command_material_hash=material_hash,
            fact_set_input_hash=input_hash,
            fact_set_result_hash=result_hash,
            item_count=counts["item_count"],
            allocation_count=counts["allocation_count"],
            adjustment_count=counts["adjustment_count"],
            audit_event_public_id=audit_event.event_public_id,
            idempotent=False,
            supersedes_fact_set_public_id=str(predecessor["fact_set_public_id"]),
            superseded_fact_set_result_hash=str(predecessor["fact_set_result_hash"]),
        )
    except ReceiptItemAllocationFactsError:
        _rollback_if_needed(conn)
        raise
    except (AuditVerificationError, AuditChainTransactionError) as exc:
        _rollback_if_needed(conn)
        raise ItemFactsPersistenceError(
            "Fact-set supersession audit event could not be appended atomically"
        ) from exc
    except sqlite3.Error as exc:
        _rollback_if_needed(conn)
        raise ItemFactsPersistenceError(
            "Receipt item/allocation fact-set supersession could not be persisted atomically"
        ) from exc
    except BaseException:
        _rollback_if_needed(conn)
        raise


def verify_receipt_item_allocation_fact_set_for_review(
    conn: sqlite3.Connection,
    fact_set_public_id: str,
) -> None:
    """SELECT-only full persisted-state verification for IAF.4 review.

    This exposes the same deep verifier used by fresh-write and replay:
    registry lineage, B4.1 provenance/evidence, canonical payload, every
    persisted row and derived identity, all hashes, monetary mirrors, and
    audit binding.  It never begins, commits, or rolls back a transaction.
    """
    if (
        not isinstance(fact_set_public_id, str)
        or not fact_set_public_id.startswith("rfs_")
        or not fact_set_public_id.strip()
        or fact_set_public_id != fact_set_public_id.strip()
    ):
        raise InvalidItemFactsCommandError(
            "fact_set_public_id must be a non-empty, whitespace-trimmed rfs_ identifier"
        )
    _require_staging(conn)
    cursor = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE fact_set_public_id = ?",
        (fact_set_public_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ItemFactsPersistenceError(
            f"Expected exactly one fact-set registry row for review, found {len(rows)}"
        )
    registry = _row_dict(cursor, rows[0])
    drift = "IAF.4 review refused: persisted fact-set state failed full verification"
    try:
        _verify_fact_set_state(
            conn,
            registry,
            require_chain_head=False,
            drift_message=drift,
        )
    except ReceiptItemAllocationFactsError:
        raise
    except (
        KeyError,
        TypeError,
        IndexError,
        ValueError,
        RecursionError,
        MemoryError,
        UnicodeError,
        OverflowError,
    ) as exc:
        raise ItemFactsPersistenceError(drift) from exc


# ---------------------------------------------------------------------------
# Guard 1: command structural validation (always precedes hashing)
# ---------------------------------------------------------------------------


def _validate_command(command: ReceiptItemAllocationFactsCommand) -> dict[str, Any]:
    """Structurally validate the complete command; return the canonical spec.

    Deterministic guard 1 error mapping: malformed IDs / unknown fields /
    malformed lists / non-contiguous ordering / oversize →
    ``InvalidItemFactsCommandError``; actor problems →
    ``UnauthorizedItemFactsActorError``; missing required fact fields →
    ``IncompleteItemFactsError``; non-string monetary values (floats,
    ints, None) → ``InvalidItemFactsMoneyError``; duplicate participants →
    ``AmbiguousItemAllocationError``.  Money Contract content validation
    (scale, currency, sign, mirrors) runs later inside the transaction.
    """
    if not isinstance(command, ReceiptItemAllocationFactsCommand):
        raise InvalidItemFactsCommandError(
            "Persistence requires a ReceiptItemAllocationFactsCommand instance"
        )
    if not isinstance(command.command_public_id, str) or not _COMMAND_ID_RE.match(
        command.command_public_id
    ):
        raise InvalidItemFactsCommandError(
            "command_public_id must match 'riaf_' plus 1-195 characters of [A-Za-z0-9_-]"
        )
    if (
        not isinstance(command.receipt_public_id, str)
        or not command.receipt_public_id
        or command.receipt_public_id != command.receipt_public_id.strip()
    ):
        raise InvalidItemFactsCommandError(
            "receipt_public_id must be a non-empty string without surrounding whitespace"
        )
    if not isinstance(
        command.expected_conversion_command_public_id, str
    ) or not _CONVERSION_COMMAND_ID_RE.match(command.expected_conversion_command_public_id):
        raise InvalidItemFactsCommandError(
            "expected_conversion_command_public_id must be a canonical 'rpfc_' command ID"
        )
    if not isinstance(command.expected_conversion_result_hash, str) or not _HASH_RE.match(
        command.expected_conversion_result_hash
    ):
        raise InvalidItemFactsCommandError(
            "expected_conversion_result_hash must be a 64-character lowercase hex string"
        )
    if command.expected_current_fact_set != _EXPECTED_CURRENT_FACT_SET_NONE:
        raise InvalidItemFactsCommandError(
            "The create command must assert expected_current_fact_set == 'none'; "
            "corrections are a separate guarded command (IAF.3)"
        )
    if command.schema_version != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION:
        raise InvalidItemFactsCommandError(
            f"Unsupported fact-set command schema_version: {command.schema_version!r}"
        )
    if command.actor_type != "human":
        raise UnauthorizedItemFactsActorError(
            "Only authenticated human actors may persist item/allocation "
            f"facts, got: {command.actor_type!r}"
        )
    if (
        not isinstance(command.authenticated_actor_id, str)
        or not command.authenticated_actor_id
        or command.authenticated_actor_id != command.authenticated_actor_id.strip()
    ):
        raise UnauthorizedItemFactsActorError(
            "authenticated_actor_id must be a non-empty string without surrounding whitespace"
        )
    if (
        not isinstance(command.channel, str)
        or not command.channel
        or command.channel != command.channel.strip()
    ):
        raise InvalidItemFactsCommandError(
            "channel must be a non-empty string without surrounding whitespace"
        )
    if command.reason is not None:
        if (
            not isinstance(command.reason, str)
            or not command.reason.strip()
            or len(command.reason) > _MAX_REASON_LENGTH
        ):
            raise InvalidItemFactsCommandError(
                f"reason must be None or a non-blank string of at most "
                f"{_MAX_REASON_LENGTH} characters"
            )

    items = _validate_items(command.items)
    allocations = _validate_allocations(command.allocations, items)
    adjustments = _validate_adjustments(command.adjustments)
    return {"items": items, "allocations": allocations, "adjustments": adjustments}


def _validate_supersession_command(
    command: ReceiptItemAllocationFactsSupersessionCommand,
) -> dict[str, Any]:
    """Structurally validate the complete IAF.3 correction command."""
    if not isinstance(command, ReceiptItemAllocationFactsSupersessionCommand):
        raise InvalidItemFactsCommandError(
            "Supersession requires a ReceiptItemAllocationFactsSupersessionCommand instance"
        )
    if not isinstance(command.command_public_id, str) or not _CORRECTION_COMMAND_ID_RE.match(
        command.command_public_id
    ):
        raise InvalidItemFactsCommandError(
            "command_public_id must match 'riafc_' plus 1-194 characters of [A-Za-z0-9_-]"
        )
    if (
        not isinstance(command.receipt_public_id, str)
        or not command.receipt_public_id
        or command.receipt_public_id != command.receipt_public_id.strip()
    ):
        raise InvalidItemFactsCommandError(
            "receipt_public_id must be a non-empty string without surrounding whitespace"
        )
    if not isinstance(
        command.expected_conversion_command_public_id, str
    ) or not _CONVERSION_COMMAND_ID_RE.match(command.expected_conversion_command_public_id):
        raise InvalidItemFactsCommandError(
            "expected_conversion_command_public_id must be a canonical 'rpfc_' command ID"
        )
    if not isinstance(command.expected_conversion_result_hash, str) or not _HASH_RE.match(
        command.expected_conversion_result_hash
    ):
        raise InvalidItemFactsCommandError(
            "expected_conversion_result_hash must be a 64-character lowercase hex string"
        )
    if (
        not isinstance(command.expected_current_fact_set_public_id, str)
        or not command.expected_current_fact_set_public_id.startswith("rfs_")
        or len(command.expected_current_fact_set_public_id) > 200
        or not re.fullmatch(
            r"rfs_[A-Za-z0-9_-]{1,196}",
            command.expected_current_fact_set_public_id,
        )
    ):
        raise InvalidItemFactsCommandError(
            "expected_current_fact_set_public_id must be a canonical rfs_ public ID"
        )
    if not isinstance(command.expected_current_fact_set_result_hash, str) or not _HASH_RE.match(
        command.expected_current_fact_set_result_hash
    ):
        raise InvalidItemFactsCommandError(
            "expected_current_fact_set_result_hash must be a 64-character lowercase hex string"
        )
    if command.schema_version != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION:
        raise InvalidItemFactsCommandError(
            f"Unsupported fact-set correction schema_version: {command.schema_version!r}"
        )
    if command.actor_type != "human":
        raise UnauthorizedItemFactsActorError(
            "Only authenticated human actors may supersede item/allocation "
            f"facts, got: {command.actor_type!r}"
        )
    if (
        not isinstance(command.authenticated_actor_id, str)
        or not command.authenticated_actor_id
        or command.authenticated_actor_id != command.authenticated_actor_id.strip()
    ):
        raise UnauthorizedItemFactsActorError(
            "authenticated_actor_id must be a non-empty string without surrounding whitespace"
        )
    if (
        not isinstance(command.channel, str)
        or not command.channel
        or command.channel != command.channel.strip()
    ):
        raise InvalidItemFactsCommandError(
            "channel must be a non-empty string without surrounding whitespace"
        )
    if command.reason is not None and (
        not isinstance(command.reason, str)
        or not command.reason.strip()
        or len(command.reason) > _MAX_REASON_LENGTH
    ):
        raise InvalidItemFactsCommandError(
            f"reason must be None or a non-blank string of at most {_MAX_REASON_LENGTH} characters"
        )

    items = _validate_items(command.items)
    allocations = _validate_allocations(command.allocations, items)
    adjustments = _validate_adjustments(command.adjustments)
    return {"items": items, "allocations": allocations, "adjustments": adjustments}


def _validate_items(items: object) -> list[dict[str, Any]]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise InvalidItemFactsCommandError("items must be a sequence of item entry mappings")
    if len(items) == 0:
        raise IncompleteItemFactsError("items must contain at least one explicit item entry")
    if len(items) > _MAX_ITEMS:
        raise InvalidItemFactsCommandError(f"items exceeds the approved bound of {_MAX_ITEMS}")
    validated: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            raise InvalidItemFactsCommandError("Each item entry must be a mapping")
        unknown = sorted(set(entry) - _ITEM_FIELDS)
        if unknown:
            raise InvalidItemFactsCommandError(f"Unknown item entry fields are rejected: {unknown}")
        missing = sorted(
            name for name in _ITEM_REQUIRED_FIELDS if name not in entry or entry[name] is None
        )
        if missing:
            raise IncompleteItemFactsError(f"Item entry is missing required fields: {missing}")
        line_number = entry["line_number"]
        if isinstance(line_number, bool) or not isinstance(line_number, int) or line_number < 1:
            raise InvalidItemFactsCommandError(
                f"Item line_number must be a positive integer, got: {line_number!r}"
            )
        name = entry["item_name"]
        if not isinstance(name, str):
            raise InvalidItemFactsCommandError("item_name must be a string")
        stripped_name = name.strip()
        if not stripped_name:
            raise IncompleteItemFactsError("item_name must not be empty or whitespace-only")
        if len(stripped_name) > _MAX_ITEM_NAME_LENGTH:
            raise InvalidItemFactsCommandError(
                f"item_name exceeds the approved bound of {_MAX_ITEM_NAME_LENGTH} characters"
            )
        quantity_text = _validate_quantity(entry.get("quantity"))
        unit_price_raw = entry.get("unit_price")
        if unit_price_raw is not None:
            unit_price_raw = _require_money_string(unit_price_raw, "item unit_price")
        line_amount_raw = _require_money_string(entry["line_amount"], "item line_amount")
        currency_raw = entry["currency"]
        if not isinstance(currency_raw, str) or not currency_raw.strip():
            raise InvalidItemFactsCommandError("item currency must be a non-empty string")
        validated.append(
            {
                "line_number": line_number,
                "item_name": stripped_name,
                "quantity_text": quantity_text,
                "unit_price_raw": unit_price_raw,
                "line_amount_raw": line_amount_raw,
                "currency_raw": currency_raw,
            }
        )
    line_numbers = sorted(item["line_number"] for item in validated)
    if line_numbers != list(range(1, len(validated) + 1)):
        raise InvalidItemFactsCommandError(
            f"Item line numbers must be unique and contiguous from 1..N; got: {line_numbers}"
        )
    validated.sort(key=lambda item: int(item["line_number"]))
    return validated


def _validate_quantity(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidItemFactsCommandError("Item quantity must be a positive integer (IA-D12)")
    if isinstance(value, int):
        if value < 1 or not _QUANTITY_RE.match(str(value)):
            raise InvalidItemFactsCommandError(
                "Item quantity must be a positive integer within the "
                "15-digit mirror-safe bound (IA-D12)"
            )
        return str(value)
    if isinstance(value, str) and _QUANTITY_RE.match(value):
        return value
    raise InvalidItemFactsCommandError(
        "Item quantity must be a positive integer in canonical form "
        f"(IA-D12; fractional quantities are deferred), got: {value!r}"
    )


def _require_money_string(value: object, label: str) -> str:
    """Monetary command values must be canonical decimal strings (Section 9).

    Floats, bools, ints, None, and empty strings are rejected here so that
    no non-string monetary value can ever reach canonical hashing; full
    Money Contract validation runs later against the declared currency.
    """
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        raise InvalidItemFactsMoneyError(
            f"{label} must be a canonical decimal string; got {type(value).__name__}: {value!r}"
        )
    if not isinstance(value, str) or not value.strip():
        raise InvalidItemFactsMoneyError(f"{label} must be a non-empty canonical decimal string")
    return value


def _validate_allocations(allocations: object, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(allocations, (str, bytes)) or not isinstance(allocations, Sequence):
        raise InvalidItemFactsCommandError(
            "allocations must be a sequence of per-item allocation entry mappings"
        )
    item_lines = {int(item["line_number"]) for item in items}
    by_line: dict[int, dict[str, Any]] = {}
    for entry in allocations:
        if not isinstance(entry, Mapping):
            raise InvalidItemFactsCommandError("Each allocation entry must be a mapping")
        unknown = sorted(set(entry) - _ALLOCATION_FIELDS)
        if unknown:
            raise InvalidItemFactsCommandError(
                f"Unknown allocation entry fields are rejected: {unknown}"
            )
        missing = sorted(
            name for name in _ALLOCATION_FIELDS if name not in entry or entry[name] is None
        )
        if missing:
            raise IncompleteItemFactsError(
                f"Allocation entry is missing required fields: {missing}"
            )
        line_number = entry["line_number"]
        if isinstance(line_number, bool) or not isinstance(line_number, int):
            raise InvalidItemFactsCommandError("Allocation line_number must be an integer")
        if line_number not in item_lines:
            raise InvalidItemFactsCommandError(
                f"Allocation entry references unknown item line_number: {line_number}"
            )
        method = entry["allocation_method"]
        if not isinstance(method, str) or not method.strip():
            raise InvalidItemFactsCommandError("allocation_method must be a non-empty string")
        if line_number in by_line:
            if str(by_line[line_number]["allocation_method"]) != method:
                raise InvalidItemFactsCommandError(
                    f"Mixed allocation methods for item line {line_number} are rejected"
                )
            raise InvalidItemFactsCommandError(
                f"Duplicate allocation entries for item line {line_number} are rejected"
            )
        participants = _validate_allocation_participants(
            entry["participants"], f"item line {line_number}"
        )
        by_line[line_number] = {
            "line_number": line_number,
            "allocation_method": method,
            "participants": participants,
        }
    uncovered = sorted(item_lines - set(by_line))
    if uncovered:
        raise IncompleteItemFactsError(
            f"Every item must have at least one allocation; uncovered item lines: {uncovered}"
        )
    return [by_line[line] for line in sorted(by_line)]


def _validate_allocation_participants(participants: object, label: str) -> list[dict[str, Any]]:
    if isinstance(participants, (str, bytes)) or not isinstance(participants, Sequence):
        raise InvalidItemFactsCommandError(
            f"Allocation participants for {label} must be a sequence of mappings"
        )
    if len(participants) == 0:
        raise IncompleteItemFactsError(
            f"Allocation participants for {label} must list at least one explicit consumer"
        )
    if len(participants) > _MAX_PARTICIPANTS_PER_ENTRY:
        raise InvalidItemFactsCommandError(
            f"Allocation participants for {label} exceed the approved bound "
            f"of {_MAX_PARTICIPANTS_PER_ENTRY}"
        )
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in participants:
        if not isinstance(entry, Mapping):
            raise InvalidItemFactsCommandError(
                f"Each allocation participant entry for {label} must be a mapping"
            )
        unknown = sorted(set(entry) - _ALLOCATION_PARTICIPANT_FIELDS)
        if unknown:
            raise InvalidItemFactsCommandError(
                f"Unknown allocation participant fields are rejected: {unknown}"
            )
        pid = entry.get("participant_public_id")
        if not isinstance(pid, str) or not pid or pid != pid.strip():
            raise InvalidItemFactsCommandError(
                f"Allocation participant_public_id for {label} must be a non-empty string"
            )
        if pid in seen:
            # Rejected before sorting (Section 12.1): duplicates — with the
            # same or contradictory amounts — are ambiguous, never merged.
            raise AmbiguousItemAllocationError(
                f"Duplicate allocation entries for participant {pid!r} on {label}"
            )
        seen.add(pid)
        share_raw = entry.get("share_amount")
        if share_raw is not None:
            share_raw = _require_money_string(share_raw, f"allocation share_amount ({label})")
        currency_raw = entry.get("currency")
        if currency_raw is not None and (
            not isinstance(currency_raw, str) or not currency_raw.strip()
        ):
            raise InvalidItemFactsCommandError(
                f"Allocation participant currency for {label} must be a non-empty string"
            )
        validated.append(
            {
                "participant_public_id": pid,
                "share_amount_raw": share_raw,
                "currency_raw": currency_raw,
            }
        )
    validated.sort(key=lambda item: str(item["participant_public_id"]))
    return validated


def _validate_adjustments(adjustments: object) -> list[dict[str, Any]]:
    if isinstance(adjustments, (str, bytes)) or not isinstance(adjustments, Sequence):
        raise InvalidItemFactsCommandError(
            "adjustments must be an explicit sequence (an empty list states "
            "'no adjustments'; absence is never defaulted)"
        )
    if len(adjustments) > _MAX_ADJUSTMENTS:
        raise InvalidItemFactsCommandError(
            f"adjustments exceeds the approved bound of {_MAX_ADJUSTMENTS}"
        )
    validated: list[dict[str, Any]] = []
    for entry in adjustments:
        if not isinstance(entry, Mapping):
            raise InvalidItemFactsCommandError("Each adjustment entry must be a mapping")
        unknown = sorted(set(entry) - _ADJUSTMENT_FIELDS)
        if unknown:
            raise InvalidItemFactsCommandError(
                f"Unknown adjustment entry fields are rejected: {unknown}"
            )
        missing = sorted(
            name for name in _ADJUSTMENT_REQUIRED_FIELDS if name not in entry or entry[name] is None
        )
        if missing:
            raise IncompleteItemFactsError(
                f"Adjustment entry is missing required fields: {missing}"
            )
        index = entry["adjustment_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise InvalidItemFactsCommandError(
                f"adjustment_index must be a positive integer, got: {index!r}"
            )
        adjustment_type = entry["adjustment_type"]
        direction = entry["direction"]
        method = entry["allocation_method"]
        for label, value in (
            ("adjustment_type", adjustment_type),
            ("direction", direction),
            ("allocation_method", method),
        ):
            if not isinstance(value, str) or not value.strip():
                raise InvalidItemFactsCommandError(f"Adjustment {label} must be a non-empty string")
        amount_raw = _require_money_string(entry["amount"], "adjustment amount")
        currency_raw = entry["currency"]
        if not isinstance(currency_raw, str) or not currency_raw.strip():
            raise InvalidItemFactsCommandError("Adjustment currency must be a non-empty string")
        description = _validate_description(entry.get("description"))
        participants_raw = entry.get("participants")
        participants: list[dict[str, Any]] | None = None
        if participants_raw is not None:
            participants = _validate_manual_adjustment_participants(participants_raw, index)
        validated.append(
            {
                "adjustment_index": index,
                "adjustment_type": adjustment_type,
                "amount_raw": amount_raw,
                "currency_raw": currency_raw,
                "direction": direction,
                "allocation_method": method,
                "description": description,
                "participants": participants,
            }
        )
    indexes = sorted(int(entry["adjustment_index"]) for entry in validated)
    if indexes != list(range(1, len(validated) + 1)):
        raise InvalidItemFactsCommandError(
            f"Adjustment indexes must be unique and contiguous from 1..N; got: {indexes}"
        )
    validated.sort(key=lambda entry: int(entry["adjustment_index"]))
    return validated


def _validate_description(value: object) -> str | None:
    """Approved Section 8.2 bound: ≤500 code points post-strip, never blank."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidItemFactsCommandError("Adjustment description must be a string or None")
    stripped = value.strip()
    if not stripped:
        raise InvalidItemFactsCommandError(
            "A whitespace-only adjustment description is rejected as malformed"
        )
    if len(stripped) > _MAX_DESCRIPTION_LENGTH:
        raise InvalidItemFactsCommandError(
            f"Adjustment description exceeds the approved bound of "
            f"{_MAX_DESCRIPTION_LENGTH} characters after stripping"
        )
    return stripped


def _validate_manual_adjustment_participants(
    participants: object, index: int
) -> list[dict[str, Any]]:
    if isinstance(participants, (str, bytes)) or not isinstance(participants, Sequence):
        raise InvalidItemFactsCommandError(
            f"Adjustment {index} participants must be a sequence of mappings"
        )
    if len(participants) == 0:
        raise InvalidItemFactsCommandError(
            f"Adjustment {index} participants must not be an empty list; omit "
            "the field for non-manual methods"
        )
    if len(participants) > _MAX_PARTICIPANTS_PER_ENTRY:
        raise InvalidItemFactsCommandError(
            f"Adjustment {index} participants exceed the approved bound of "
            f"{_MAX_PARTICIPANTS_PER_ENTRY}"
        )
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in participants:
        if not isinstance(entry, Mapping):
            raise InvalidItemFactsCommandError(
                f"Each adjustment {index} participant entry must be a mapping"
            )
        unknown = sorted(set(entry) - _ALLOCATION_PARTICIPANT_FIELDS)
        if unknown:
            raise InvalidItemFactsCommandError(
                f"Unknown adjustment participant fields are rejected: {unknown}"
            )
        missing = sorted(
            name
            for name in _MANUAL_PARTICIPANT_REQUIRED_FIELDS
            if name not in entry or entry[name] is None
        )
        if missing:
            raise IncompleteItemFactsError(
                f"Adjustment {index} manual participant entry is missing required fields: {missing}"
            )
        pid = entry["participant_public_id"]
        if not isinstance(pid, str) or not pid or pid != pid.strip():
            raise InvalidItemFactsCommandError(
                f"Adjustment {index} participant_public_id must be a non-empty string"
            )
        if pid in seen:
            raise AmbiguousItemAllocationError(
                f"Duplicate manual share entries for participant {pid!r} on adjustment {index}"
            )
        seen.add(pid)
        share_raw = _require_money_string(entry["share_amount"], f"adjustment {index} manual share")
        currency_raw = entry["currency"]
        if not isinstance(currency_raw, str) or not currency_raw.strip():
            raise InvalidItemFactsCommandError(
                f"Adjustment {index} participant currency must be a non-empty string"
            )
        validated.append(
            {
                "participant_public_id": pid,
                "share_amount_raw": share_raw,
                "currency_raw": currency_raw,
            }
        )
    validated.sort(key=lambda item: str(item["participant_public_id"]))
    return validated


# ---------------------------------------------------------------------------
# Guards 2-4: staging database, foreign keys, transaction acquisition
# ---------------------------------------------------------------------------


def _require_staging(conn: sqlite3.Connection) -> None:
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise ItemFactsStagingDatabaseRejectedError(str(exc)) from exc
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise ItemFactsForeignKeysDisabledError(str(exc)) from exc


def _acquire_write_transaction(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        # Caller-owned transaction: reject without touching its state.
        raise ItemFactsCallerOwnedTransactionError(
            "Fact-set persistence requires a connection without pending work"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        _rollback_if_needed(conn)
        raise ItemFactsPersistenceError("Could not acquire the fact-set write transaction") from exc


# ---------------------------------------------------------------------------
# Guard 5: idempotency lookup
# ---------------------------------------------------------------------------


def _get_registry_row(conn: sqlite3.Connection, command_public_id: str) -> dict[str, Any] | None:
    """Found/not-found is decided by the fact-set registry table alone."""
    cursor = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE command_public_id = ?",
        (command_public_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_dict(cursor, row)


# ---------------------------------------------------------------------------
# Guards 6-8: receipt, provenance, and B4.1 persisted integrity
# ---------------------------------------------------------------------------


def _require_receipt(conn: sqlite3.Connection, receipt_public_id: str) -> dict[str, Any]:
    cursor = conn.execute("SELECT * FROM receipts WHERE public_id = ?", (receipt_public_id,))
    rows = cursor.fetchall()
    if not rows:
        raise ItemFactsReceiptNotFoundError(f"Receipt not found: {receipt_public_id!r}")
    if len(rows) != 1:
        raise ItemFactsReceiptNotFoundError(
            f"Receipt public ID {receipt_public_id!r} matches {len(rows)} "
            "rows; receipt identity is not unique and cannot be trusted"
        )
    return _row_dict(cursor, rows[0])


def _require_conversion_binding(
    conn: sqlite3.Connection, receipt: dict[str, Any]
) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT * FROM receipt_proposal_conversions WHERE receipt_id = ?",
        (receipt["id"],),
    )
    rows = cursor.fetchall()
    if not rows:
        raise UnsupportedItemFactsReceiptProvenanceError(
            f"Receipt {receipt['public_id']!r} is not bound by a "
            "receipt_proposal_conversions registry row; only B4.1 "
            "conversion-created receipts are supported, and a populated "
            "canonical amount column alone does not establish provenance"
        )
    if len(rows) != 1:
        raise ItemFactsEvidenceLineageError(
            f"Receipt {receipt['public_id']!r} is bound by {len(rows)} "
            "conversion registry rows; the one-conversion-per-receipt "
            "invariant is violated"
        )
    return _row_dict(cursor, rows[0])


def _require_b41_integrity(
    conn: sqlite3.Connection,
    receipt: dict[str, Any],
    conversion: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Guard 8: SELECT-only revalidation of the B4.1 trust surface.

    Returns the Decision D5 membership map keyed by participant public ID.
    Any drift fails closed as ``ItemFactsEvidenceLineageError`` — never a
    downgraded "not eligible" result.
    """
    command_public_id = conversion["command_public_id"]
    if not isinstance(command_public_id, str) or not _CONVERSION_COMMAND_ID_RE.match(
        command_public_id
    ):
        raise ItemFactsEvidenceLineageError(
            "Conversion registry command_public_id does not match the frozen "
            f"rpfc_ identity pattern: {command_public_id!r}"
        )
    if str(receipt["public_id"]) != derive_receipt_public_id(command_public_id):
        raise ItemFactsEvidenceLineageError(
            "Receipt public ID does not match the deterministic derivation "
            "from the conversion registry command; registry-to-receipt "
            "identity has drifted"
        )
    if conversion["receipt_id"] != receipt["id"]:
        raise ItemFactsEvidenceLineageError(
            "Conversion registry receipt_id does not match the receipt row"
        )
    if receipt["parser_output_id"] is None or (
        conversion["parser_output_id"] != receipt["parser_output_id"]
    ):
        raise ItemFactsEvidenceLineageError(
            "Conversion registry parser_output_id does not match the "
            "receipt's persisted parser output lineage"
        )
    if str(conversion["actor_type"]) != "human":
        raise ItemFactsEvidenceLineageError(
            "Conversion registry actor_type must be 'human'; the persisted "
            "conversion provenance is untrusted"
        )
    for field in ("authenticated_actor_id", "conversion_channel", "confirmation_public_id"):
        value = conversion[field]
        if not isinstance(value, str) or not value.strip():
            raise ItemFactsEvidenceLineageError(f"Conversion registry {field} is missing or empty")
    for field in ("proposal_content_hash", "command_material_hash", "conversion_result_hash"):
        value = conversion[field]
        if not isinstance(value, str) or not _HASH_RE.match(value):
            raise ItemFactsEvidenceLineageError(
                f"Conversion registry {field} is not a lowercase 64-hex digest"
            )
    proposal_rows = conn.execute(
        "SELECT id FROM parser_outputs WHERE id = ?", (conversion["parser_output_id"],)
    ).fetchall()
    if len(proposal_rows) != 1:
        raise ItemFactsEvidenceLineageError(
            "Conversion registry parser_output_id does not resolve to a "
            "parser_outputs row; the proposal lineage is broken"
        )
    confirmation_rows = conn.execute(
        "SELECT parser_output_id, actor_type, proposal_content_hash "
        "FROM parser_proposal_authorizations WHERE confirmation_public_id = ?",
        (conversion["confirmation_public_id"],),
    ).fetchall()
    if len(confirmation_rows) != 1:
        raise ItemFactsEvidenceLineageError(
            "Conversion registry confirmation_public_id does not resolve to "
            "a confirmation authorization row; the confirmation lineage is broken"
        )
    confirmation = confirmation_rows[0]
    if (
        confirmation[0] != conversion["parser_output_id"]
        or str(confirmation[1]) != "human"
        or str(confirmation[2]) != str(conversion["proposal_content_hash"])
    ):
        raise ItemFactsEvidenceLineageError(
            "Conversion registry confirmation binding does not match the "
            "persisted confirmation authorization record"
        )
    drifted = sorted(name for name in _B41_NEVER_WRITTEN_COLUMNS if receipt[name] is not None)
    if drifted:
        raise ItemFactsEvidenceLineageError(
            f"Conversion-created receipt carries non-NULL values in {drifted}, "
            "which a B4.1 facts-only conversion never writes; the persisted "
            "state is untrusted"
        )
    _require_receipt_monetary_integrity(receipt)
    return _load_membership(conn, receipt)


def _require_receipt_monetary_integrity(receipt: dict[str, Any]) -> None:
    canonical_text = receipt["net_paid_amount_canonical_text"]
    if not isinstance(canonical_text, str):
        raise ItemFactsEvidenceLineageError(
            "Registry-bound receipt is missing the authoritative canonical "
            f"monetary text, got: {canonical_text!r}"
        )
    currency = receipt["currency"]
    try:
        normalized_currency = normalize_currency(currency)
    except MoneyValidationError as exc:
        raise ItemFactsEvidenceLineageError(
            f"Persisted receipt currency failed the Money Contract: {exc}"
        ) from exc
    if normalized_currency != currency:
        raise ItemFactsEvidenceLineageError(
            f"Persisted receipt currency {currency!r} is not in canonical form"
        )
    try:
        amount = money_decimal(canonical_text, label="canonical amount text")
        validate_amount_for_currency(amount, currency, label="canonical amount text")
    except MoneyValidationError as exc:
        raise ItemFactsEvidenceLineageError(
            f"Persisted canonical amount text failed the Money Contract: {exc}"
        ) from exc
    if amount <= Decimal(0):
        raise ItemFactsEvidenceLineageError(
            f"Persisted canonical amount must be strictly positive, got: {canonical_text!r}"
        )
    if canonical_money_str(amount, currency) != canonical_text:
        raise ItemFactsEvidenceLineageError(
            f"Persisted canonical amount text {canonical_text!r} is not the "
            f"byte-exact canonical minor-unit form for {currency}"
        )
    mirrored = decimal_from_numeric_mirror(receipt["net_paid_amount"])
    if mirrored is None or mirrored != amount:
        raise ItemFactsEvidenceLineageError(
            "Legacy NUMERIC net_paid_amount mirror does not decode "
            "Decimal-equal to the authoritative canonical text under the "
            "B4.1 mirror-decoding contract; the persisted monetary state is lossy"
        )


def _load_membership(
    conn: sqlite3.Connection, receipt: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Decision D5 membership facts, revalidated fail-closed (guard 8)."""
    payer_participant_id = receipt["payer_participant_id"]
    if payer_participant_id is None:
        raise ItemFactsEvidenceLineageError("Conversion-created receipt has no payer participant")
    raw_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_participants WHERE receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    member_rows = conn.execute(
        "SELECT rp.participant_id, rp.role, rp.is_included, p.public_id "
        "FROM receipt_participants rp "
        "JOIN participants p ON p.id = rp.participant_id "
        "WHERE rp.receipt_id = ?",
        (receipt["id"],),
    ).fetchall()
    if raw_count != len(member_rows):
        raise ItemFactsEvidenceLineageError(
            "Receipt membership rows reference participants that do not exist"
        )
    if not member_rows:
        raise ItemFactsEvidenceLineageError(
            "Conversion-created receipt has no membership rows; B4.1 always "
            "persists at least the payer entry (Decision D5)"
        )
    membership: dict[str, dict[str, Any]] = {}
    payer_rows = 0
    for row in sorted(member_rows, key=lambda member: str(member[3])):
        public_id = str(row[3])
        if public_id in membership:
            raise ItemFactsEvidenceLineageError(
                "Receipt membership carries duplicate participant identities"
            )
        if row[2] not in (0, 1):
            raise ItemFactsEvidenceLineageError(
                f"Receipt membership is_included must be an explicit 0 or 1, got: {row[2]!r}"
            )
        included = int(row[2])
        role = str(row[1])
        if int(row[0]) == int(payer_participant_id):
            payer_rows += 1
            if role != _ROLE_PAYER:
                raise ItemFactsEvidenceLineageError(
                    "Receipt membership payer row does not carry role='payer'"
                )
        else:
            expected_role = _ROLE_PARTICIPANT if included == 1 else _ROLE_EXCLUDED
            if role != expected_role:
                raise ItemFactsEvidenceLineageError(
                    "Receipt membership role/inclusion facts are contradictory "
                    f"under the Decision D5 mapping: role {role!r} with "
                    f"is_included {included!r}"
                )
        membership[public_id] = {
            "participant_id": int(row[0]),
            "is_included": included,
            "role": role,
        }
    if payer_rows != 1:
        raise ItemFactsEvidenceLineageError(
            f"Receipt membership must contain exactly one payer row, found {payer_rows}"
        )
    return membership


# ---------------------------------------------------------------------------
# Binding staleness and guards 9-13
# ---------------------------------------------------------------------------


def _require_asserted_binding(
    command: ReceiptItemAllocationFactsCommand | ReceiptItemAllocationFactsSupersessionCommand,
    conversion: dict[str, Any],
) -> None:
    """The human asserts exactly which conversion state they reviewed."""
    if command.expected_conversion_command_public_id != str(conversion["command_public_id"]):
        raise StaleItemFactsReceiptBindingError(
            "The asserted conversion command public ID does not match the "
            "receipt's actual conversion registry binding"
        )
    if command.expected_conversion_result_hash != str(conversion["conversion_result_hash"]):
        raise StaleItemFactsReceiptBindingError(
            "The asserted conversion result hash does not match the recorded "
            "conversion_result_hash; re-review the receipt and issue a new command"
        )


def _require_confirmed_unfinalized(receipt: dict[str, Any]) -> None:
    if str(receipt["status"]) != "confirmed":
        raise StaleItemFactsReceiptBindingError(
            f"Receipt status must remain 'confirmed' (Decision D4); got {receipt['status']!r}"
        )
    if receipt["transaction_id"] is not None:
        raise StaleItemFactsReceiptBindingError(
            "A canonical transaction already binds the receipt through the "
            "finalization path; fact-set creation fails closed (IA-D11)"
        )


def _require_no_calculation_authority(conn: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    # Guard 10 second clause: the finalization path that exists today is
    # group-scoped (finance_core/receipt_finalization/finalizer.py writes
    # group-scoped calculation_runs, transactions, and settlement rows
    # without ever setting receipts.transaction_id), and it is only
    # reachable through receipt_group_receipts membership.  A B4.1
    # conversion-created receipt is never legitimately grouped, so any
    # membership means unverifiable downstream authority: fail closed.
    group_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_group_receipts WHERE receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if group_count:
        raise StaleItemFactsReceiptBindingError(
            f"{group_count} receipt_group_receipts row(s) bind the receipt "
            "to the group-scoped calculation/finalization path; a first "
            "fact set may not be added under that authority (guard 10)"
        )
    run_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM calculation_runs WHERE receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if run_count:
        raise StaleItemFactsReceiptBindingError(
            f"{run_count} calculation_runs row(s) already reference the "
            "receipt; a first fact set may not be added under existing "
            "calculation authority (guard 11)"
        )
    snapshot_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM authoritative_calculation_snapshots "
            "WHERE aggregate_public_id = ?",
            (receipt["public_id"],),
        ).fetchone()[0]
    )
    if snapshot_count:
        raise StaleItemFactsReceiptBindingError(
            f"{snapshot_count} authoritative calculation snapshot(s) already "
            "bind the receipt aggregate (guard 11)"
        )
    # Defensive backstop: settlement_obligations only reach a receipt
    # through a receipt-scoped calculation run, which run_count above
    # already rejects; this leg guards against future schema drift.
    settlement_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM settlement_obligations so "
            "JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id "
            "WHERE cr.receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if settlement_count:
        raise StaleItemFactsReceiptBindingError(
            f"{settlement_count} settlement obligation(s) already derive from "
            "the receipt (guard 11)"
        )


def _require_no_finalization_authority(conn: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    """IAF.3 allows historical calculations but never finalized authority.

    Today's finalizer is group-scoped and a B4.1 conversion-created receipt
    has no legitimate group membership.  Such membership therefore remains
    a fail-closed finalization-authority signal.  Receipt-scoped calculation
    runs and snapshots are intentionally *not* rejected here: the approved
    IA-D11 contract preserves them as immutable historical evidence while
    the active fact set advances.
    """
    group_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_group_receipts WHERE receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if group_count:
        raise StaleItemFactsReceiptBindingError(
            f"{group_count} receipt_group_receipts row(s) bind the receipt "
            "to the group-scoped finalization path; fact-set correction "
            "fails closed"
        )


def _require_expected_active_fact_set(
    conn: sqlite3.Connection,
    receipt: dict[str, Any],
    command: ReceiptItemAllocationFactsSupersessionCommand,
) -> dict[str, Any]:
    """Require the caller-reviewed predecessor to be the unique active set."""
    cursor = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE receipt_id = ? ORDER BY version",
        (receipt["id"],),
    )
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    active = [row for row in rows if row["superseded_by_fact_set_public_id"] is None]
    if len(active) != 1:
        raise StaleItemFactSetVersionError(
            f"Expected exactly one active fact set for correction, found {len(active)}"
        )
    predecessor = active[0]
    if (
        str(predecessor["fact_set_public_id"]) != command.expected_current_fact_set_public_id
        or str(predecessor["fact_set_result_hash"]) != command.expected_current_fact_set_result_hash
    ):
        raise StaleItemFactSetVersionError(
            "The active fact set does not match the caller-reviewed public ID "
            "and result hash; re-review and issue a new command"
        )
    return predecessor


def _require_no_existing_fact_set(conn: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocation_fact_sets WHERE receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if count:
        raise ItemFactSetAlreadyExistsError(
            f"{count} fact-set registry row(s) already exist for receipt "
            f"{receipt['public_id']!r} while the create command asserts "
            "'none'; corrections require the separate IAF.3 supersession command"
        )


def _require_no_adhoc_rows(conn: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    item_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?", (receipt["id"],)
        ).fetchone()[0]
    )
    allocation_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocations ria "
            "JOIN receipt_items ri ON ri.id = ria.receipt_item_id "
            "WHERE ri.receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    adjustment_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_adjustments WHERE receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if item_count or allocation_count or adjustment_count:
        raise ItemFactsEvidenceLineageError(
            f"Receipt {receipt['public_id']!r} carries untrusted ad-hoc rows "
            f"({item_count} item, {allocation_count} allocation, "
            f"{adjustment_count} adjustment) that no guarded boundary "
            "created; the command never adopts, repairs, or deletes them"
        )


def _require_no_untrusted_rows(conn: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    """Correction accepts prior guarded versions, never ad-hoc legacy rows."""
    item_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ? AND fact_set_id IS NULL",
            (receipt["id"],),
        ).fetchone()[0]
    )
    allocation_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocations ria "
            "JOIN receipt_items ri ON ri.id = ria.receipt_item_id "
            "WHERE ri.receipt_id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    )
    adjustment_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_adjustments WHERE receipt_id = ? AND fact_set_id IS NULL",
            (receipt["id"],),
        ).fetchone()[0]
    )
    if item_count or allocation_count or adjustment_count:
        raise ItemFactsEvidenceLineageError(
            f"Receipt {receipt['public_id']!r} carries untrusted ad-hoc rows "
            f"({item_count} item, {allocation_count} allocation, "
            f"{adjustment_count} adjustment); correction never adopts, "
            "repairs, or deletes them"
        )


# ---------------------------------------------------------------------------
# Guard 14: monetary / allocation / adjustment content validation
# ---------------------------------------------------------------------------


def _require_supported_currency(raw: object, label: str) -> str:
    try:
        return normalize_currency(raw if isinstance(raw, str) else "")
    except MoneyValidationError as exc:
        raise InvalidItemFactsMoneyError(f"{label} failed the Money Contract: {exc}") from exc


def _canonical_amount(
    conn: sqlite3.Connection,
    raw: str,
    currency: str,
    label: str,
) -> tuple[Decimal, str]:
    """Validate one authored monetary string; return (Decimal, canonical text).

    Money Contract: canonical decimal string, minor-unit scale for the
    declared currency, strictly positive, and NUMERIC-mirror lossless
    (the SQLite ``CAST(? AS NUMERIC)`` prediction must decode Decimal-equal
    under the shared mirror-decoding rule) — all before any write.
    """
    try:
        amount = money_decimal(raw, label=label)
        validate_amount_for_currency(amount, currency, label=label)
    except MoneyValidationError as exc:
        raise InvalidItemFactsMoneyError(f"{label} failed the Money Contract: {exc}") from exc
    if amount <= Decimal(0):
        raise InvalidItemFactsMoneyError(
            f"{label} must be strictly positive (IA-D12), got: {raw!r}"
        )
    canonical_text = canonical_money_str(amount, currency)
    mirrored = conn.execute("SELECT CAST(? AS NUMERIC)", (canonical_text,)).fetchone()[0]
    mirrored_decimal = decimal_from_numeric_mirror(mirrored)
    exact = money_decimal(canonical_text)
    if mirrored_decimal is None or mirrored_decimal != exact:
        raise InvalidItemFactsMoneyError(
            f"{label} cannot be mirrored losslessly by the legacy NUMERIC "
            "compatibility column; refusing a lossy write"
        )
    return exact, canonical_text


def _require_receipt_currency(declared: str, receipt_currency: str, label: str) -> None:
    if declared != receipt_currency:
        raise IncompleteItemFactsError(
            f"{label} currency {declared!r} does not equal the receipt "
            f"currency {receipt_currency!r}; cross-currency facts are rejected"
        )


def _resolve_included_participant(
    membership: dict[str, dict[str, Any]], pid: str, label: str
) -> int:
    member = membership.get(pid)
    if member is None:
        raise AmbiguousItemAllocationError(
            f"{label} references participant {pid!r} without a membership "
            "row on this receipt; membership is never edited here"
        )
    if int(member["is_included"]) != 1:
        raise AmbiguousItemAllocationError(
            f"{label} references excluded participant {pid!r} "
            "(is_included=0); excluded participants — including the payer — "
            "may not consume"
        )
    return int(member["participant_id"])


def _require_complete_facts(
    conn: sqlite3.Connection,
    spec: dict[str, Any],
    receipt: dict[str, Any],
    membership: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Guard 14 plus Sections 6-9: produce the canonical fact structures."""
    currency = str(receipt["currency"])
    _require_receipt_monetary_integrity(receipt)
    net_paid_text = str(receipt["net_paid_amount_canonical_text"])
    net_paid = money_decimal(net_paid_text)

    items: list[dict[str, Any]] = []
    line_amounts: dict[int, Decimal] = {}
    for entry in spec["items"]:
        line = int(entry["line_number"])
        label = f"item line {line}"
        declared_currency = _require_supported_currency(entry["currency_raw"], f"{label} currency")
        line_amount, line_amount_text = _canonical_amount(
            conn, str(entry["line_amount_raw"]), declared_currency, f"{label} line_amount"
        )
        _require_receipt_currency(declared_currency, currency, label)
        unit_price_text: str | None = None
        unit_price: Decimal | None = None
        if entry["unit_price_raw"] is not None:
            unit_price, unit_price_text = _canonical_amount(
                conn, str(entry["unit_price_raw"]), declared_currency, f"{label} unit_price"
            )
        quantity_text = entry["quantity_text"]
        if quantity_text is not None and unit_price is not None:
            expected = Decimal(quantity_text) * unit_price
            if expected != line_amount:
                raise ItemFactsReconciliationError(
                    f"{label}: quantity {quantity_text} × unit price "
                    f"{unit_price_text} = {expected} does not equal the "
                    f"line amount {line_amount_text} exactly; in-line "
                    "discounts must be explicit adjustments or a true line "
                    "amount with the unit price omitted"
                )
        line_amounts[line] = line_amount
        items.append(
            {
                "line_number": line,
                "item_name": str(entry["item_name"]),
                "quantity_text": quantity_text,
                "unit_price_text": unit_price_text,
                "line_amount_text": line_amount_text,
            }
        )

    allocations: list[dict[str, Any]] = []
    allocation_count = 0
    for entry in spec["allocations"]:
        line = int(entry["line_number"])
        label = f"allocation for item line {line}"
        method = str(entry["allocation_method"])
        if method not in ITEM_ALLOCATION_METHODS:
            raise UnsupportedAllocationRuleError(
                f"Unsupported item allocation method {method!r}; approved v1 "
                "methods are 'equal_amount' and 'manual' (IA-D7), and "
                "unsupported methods are never silently mapped"
            )
        participants: list[dict[str, Any]] = []
        manual_total = Decimal(0)
        for participant in entry["participants"]:
            pid = str(participant["participant_public_id"])
            participant_id = _resolve_included_participant(membership, pid, label)
            share_text: str | None = None
            if method == "equal_amount":
                if (
                    participant["share_amount_raw"] is not None
                    or participant["currency_raw"] is not None
                ):
                    raise InvalidItemFactsCommandError(
                        f"{label}: equal_amount allocations persist no "
                        "per-participant amount; a pre-rounded equal share "
                        "would fabricate a monetary fact"
                    )
            else:
                if participant["share_amount_raw"] is None:
                    raise IncompleteItemFactsError(
                        f"{label}: manual allocation for {pid!r} is missing "
                        "its explicit share_amount"
                    )
                if participant["currency_raw"] is None:
                    raise IncompleteItemFactsError(
                        f"{label}: manual allocation for {pid!r} is missing its explicit currency"
                    )
                share_currency = _require_supported_currency(
                    participant["currency_raw"], f"{label} share currency"
                )
                share_raw = str(participant["share_amount_raw"])
                try:
                    share_decimal = money_decimal(share_raw, label=f"{label} share")
                    validate_amount_for_currency(
                        share_decimal, share_currency, label=f"{label} share"
                    )
                except MoneyValidationError as exc:
                    raise InvalidItemFactsMoneyError(
                        f"{label} share for {pid!r} failed the Money Contract: {exc}"
                    ) from exc
                if share_decimal == Decimal(0):
                    raise AmbiguousItemAllocationError(
                        f"{label}: a zero manual share for {pid!r} is a "
                        "contradiction — remove the participant instead"
                    )
                if share_decimal < Decimal(0):
                    raise InvalidItemFactsMoneyError(
                        f"{label} share for {pid!r} must be strictly positive"
                    )
                _require_receipt_currency(share_currency, str(receipt["currency"]), label)
                share, share_text = _canonical_amount(
                    conn, share_raw, share_currency, f"{label} share"
                )
                manual_total += share
            participants.append(
                {
                    "participant_public_id": pid,
                    "participant_id": participant_id,
                    "share_text": share_text,
                }
            )
            allocation_count += 1
        if method == "manual" and manual_total != line_amounts[line]:
            raise ItemFactsReconciliationError(
                f"{label}: manual shares sum to {manual_total} but the item "
                f"line amount is {line_amounts[line]}; exact equality is "
                "required with zero tolerance (IA-D6)"
            )
        allocations.append(
            {
                "line_number": line,
                "allocation_method": method,
                "participants": participants,
            }
        )

    adjustments: list[dict[str, Any]] = []
    add_total = Decimal(0)
    subtract_total = Decimal(0)
    for entry in spec["adjustments"]:
        index = int(entry["adjustment_index"])
        label = f"adjustment {index}"
        adjustment_type = str(entry["adjustment_type"])
        if adjustment_type not in ADJUSTMENT_TYPES:
            raise UnsupportedAllocationRuleError(
                f"Unsupported adjustment type {adjustment_type!r}; the "
                "approved vocabulary is the migration 002 CHECK set (IA-D7b)"
            )
        direction = str(entry["direction"])
        if direction not in ADJUSTMENT_DIRECTIONS:
            raise UnsupportedAllocationRuleError(
                f"Unsupported adjustment direction {direction!r}; only "
                "'add' and 'subtract' are approved in v1 (IA-D7b) — "
                "'informational' cannot participate in reconciliation"
            )
        method = str(entry["allocation_method"])
        if method not in ADJUSTMENT_ALLOCATION_METHODS:
            raise UnsupportedAllocationRuleError(
                f"Unsupported adjustment allocation method {method!r}; "
                "approved v1 methods are equal_per_participant, "
                "proportional_by_item_amount, manual, and payer_only (IA-D7b)"
            )
        declared_currency = _require_supported_currency(entry["currency_raw"], f"{label} currency")
        amount, amount_text = _canonical_amount(
            conn, str(entry["amount_raw"]), declared_currency, f"{label} amount"
        )
        _require_receipt_currency(declared_currency, currency, label)
        participants_spec = entry["participants"]
        adjustment_participants: list[dict[str, Any]] | None = None
        if method == "manual":
            if participants_spec is None:
                raise IncompleteItemFactsError(
                    f"{label}: manual adjustments require explicit per-participant share amounts"
                )
            adjustment_participants = []
            manual_total = Decimal(0)
            for participant in participants_spec:
                pid = str(participant["participant_public_id"])
                participant_id = _resolve_included_participant(membership, pid, label)
                share_currency = _require_supported_currency(
                    participant["currency_raw"], f"{label} share currency"
                )
                share_raw = str(participant["share_amount_raw"])
                try:
                    share_decimal = money_decimal(share_raw, label=f"{label} share")
                    validate_amount_for_currency(
                        share_decimal, share_currency, label=f"{label} share"
                    )
                except MoneyValidationError as exc:
                    raise InvalidItemFactsMoneyError(
                        f"{label} share for {pid!r} failed the Money Contract: {exc}"
                    ) from exc
                if share_decimal == Decimal(0):
                    raise AmbiguousItemAllocationError(
                        f"{label}: a zero manual share for {pid!r} is a "
                        "contradiction — remove the participant instead"
                    )
                if share_decimal < Decimal(0):
                    raise InvalidItemFactsMoneyError(
                        f"{label} share for {pid!r} must be strictly positive"
                    )
                _require_receipt_currency(share_currency, currency, label)
                share, share_text = _canonical_amount(
                    conn, share_raw, share_currency, f"{label} share"
                )
                manual_total += share
                adjustment_participants.append(
                    {
                        "participant_public_id": pid,
                        "participant_id": participant_id,
                        "share_text": share_text,
                    }
                )
            if manual_total != amount:
                raise ItemFactsReconciliationError(
                    f"{label}: manual shares sum to {manual_total} but the "
                    f"adjustment amount is {amount_text}; exact equality is "
                    "required with zero tolerance (IA-D6)"
                )
        elif participants_spec is not None:
            raise InvalidItemFactsCommandError(
                f"{label}: per-participant shares are only meaningful for "
                "the manual method; remove the participants field"
            )
        if direction == "add":
            add_total += amount
        else:
            subtract_total += amount
        adjustments.append(
            {
                "adjustment_index": index,
                "adjustment_type": adjustment_type,
                "direction": direction,
                "amount_text": amount_text,
                "allocation_method": method,
                "description": entry["description"],
                "participants": adjustment_participants,
            }
        )

    items_total = sum(line_amounts.values(), Decimal(0))
    fact_set_total = items_total + add_total - subtract_total
    if fact_set_total != net_paid:
        raise ItemFactsReconciliationError(
            f"Fact-set reconciliation failed (IA-D6): items {items_total} "
            f"+ add {add_total} - subtract {subtract_total} = "
            f"{fact_set_total} does not equal the authoritative net paid "
            f"{net_paid_text}; difference {fact_set_total - net_paid}"
        )

    return {
        "currency": currency,
        "net_paid_text": net_paid_text,
        "items": items,
        "allocations": allocations,
        "adjustments": adjustments,
    }


# ---------------------------------------------------------------------------
# Guard 15: source evidence and conversion registry lineage
# ---------------------------------------------------------------------------


def _require_evidence_lineage(
    conn: sqlite3.Connection,
    conversion: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, str]:
    """Resolve the registry's evidence chain; return the lineage hashes."""
    link_rows = conn.execute(
        "SELECT extraction_id FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
        (conversion["parser_output_id"],),
    ).fetchall()
    if len(link_rows) != 1:
        raise ItemFactsEvidenceLineageError(
            f"Expected exactly one OCR proposal link for the conversion's "
            f"parser output, found {len(link_rows)}"
        )
    extraction_row = conn.execute(
        "SELECT attachment_id, source_attachment_hash FROM receipt_ocr_extractions WHERE id = ?",
        (link_rows[0][0],),
    ).fetchone()
    if extraction_row is None:
        raise ItemFactsEvidenceLineageError(
            "The conversion's OCR link references a missing extraction row"
        )
    attachment_row = conn.execute(
        "SELECT id FROM attachments WHERE id = ?", (extraction_row[0],)
    ).fetchone()
    if attachment_row is None:
        raise ItemFactsEvidenceLineageError(
            "The conversion's attachment row is missing from the evidence chain"
        )
    intake_rows = conn.execute(
        "SELECT id FROM raw_intake_records WHERE parser_output_id = ?",
        (conversion["parser_output_id"],),
    ).fetchall()
    if len(intake_rows) != 1:
        raise ItemFactsEvidenceLineageError(
            f"Expected exactly one raw intake record for the conversion's "
            f"parser output, found {len(intake_rows)}"
        )
    chain_check = _verify_receipt_chain(conn, str(receipt["public_id"]))
    if not chain_check.valid or chain_check.legacy_without_chain:
        raise ItemFactsEvidenceLineageError(
            "The receipt aggregate's financial audit chain does not verify; "
            "broken lineage fails closed"
        )
    return {
        "attachment_content_hash": str(extraction_row[1]),
        "proposal_content_hash": str(conversion["proposal_content_hash"]),
    }


def _verify_receipt_chain(conn: sqlite3.Connection, receipt_public_id: str) -> Any:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return verify_financial_audit_chain(
            conn, aggregate_type="receipt", aggregate_public_id=receipt_public_id
        )
    finally:
        conn.row_factory = previous_factory


# ---------------------------------------------------------------------------
# Canonical material and hashing (Section 12, frozen)
# ---------------------------------------------------------------------------


def _command_material_hash(command: ReceiptItemAllocationFactsCommand, spec: dict[str, Any]) -> str:
    """Canonical command material (Section 12.2): ``reason`` is excluded."""
    material = {
        "schema_version": ITEM_ALLOCATION_FACTS_SCHEMA_VERSION,
        "command_public_id": command.command_public_id,
        "receipt_public_id": command.receipt_public_id,
        "expected_conversion_command_public_id": (command.expected_conversion_command_public_id),
        "expected_conversion_result_hash": command.expected_conversion_result_hash,
        "expected_current_fact_set": command.expected_current_fact_set,
        "items": [
            {
                "line_number": item["line_number"],
                "item_name": item["item_name"],
                "quantity": item["quantity_text"],
                "unit_price": item["unit_price_raw"],
                "line_amount": item["line_amount_raw"],
                "currency": item["currency_raw"],
            }
            for item in spec["items"]
        ],
        "allocations": [
            {
                "line_number": entry["line_number"],
                "allocation_method": entry["allocation_method"],
                "participants": [
                    {
                        "participant_public_id": participant["participant_public_id"],
                        "share_amount": participant["share_amount_raw"],
                        "currency": participant["currency_raw"],
                    }
                    for participant in entry["participants"]
                ],
            }
            for entry in spec["allocations"]
        ],
        "adjustments": [
            {
                "adjustment_index": entry["adjustment_index"],
                "adjustment_type": entry["adjustment_type"],
                "amount": entry["amount_raw"],
                "currency": entry["currency_raw"],
                "direction": entry["direction"],
                "allocation_method": entry["allocation_method"],
                "description": entry["description"],
                "participants": (
                    None
                    if entry["participants"] is None
                    else [
                        {
                            "participant_public_id": participant["participant_public_id"],
                            "share_amount": participant["share_amount_raw"],
                            "currency": participant["currency_raw"],
                        }
                        for participant in entry["participants"]
                    ]
                ),
            }
            for entry in spec["adjustments"]
        ],
        "authenticated_actor_id": command.authenticated_actor_id,
        "actor_type": command.actor_type,
        "channel": command.channel,
    }
    return _sha256_hex(_canonical_json(material))


def _supersession_command_material_hash(
    command: ReceiptItemAllocationFactsSupersessionCommand,
    spec: dict[str, Any],
) -> str:
    """Canonical IAF.3 command material; ``reason`` remains excluded."""
    material = {
        "schema_version": ITEM_ALLOCATION_FACTS_SCHEMA_VERSION,
        "command_public_id": command.command_public_id,
        "receipt_public_id": command.receipt_public_id,
        "expected_conversion_command_public_id": (command.expected_conversion_command_public_id),
        "expected_conversion_result_hash": command.expected_conversion_result_hash,
        "expected_current_fact_set": {
            "fact_set_public_id": command.expected_current_fact_set_public_id,
            "fact_set_result_hash": command.expected_current_fact_set_result_hash,
        },
        "items": [
            {
                "line_number": item["line_number"],
                "item_name": item["item_name"],
                "quantity": item["quantity_text"],
                "unit_price": item["unit_price_raw"],
                "line_amount": item["line_amount_raw"],
                "currency": item["currency_raw"],
            }
            for item in spec["items"]
        ],
        "allocations": [
            {
                "line_number": entry["line_number"],
                "allocation_method": entry["allocation_method"],
                "participants": [
                    {
                        "participant_public_id": participant["participant_public_id"],
                        "share_amount": participant["share_amount_raw"],
                        "currency": participant["currency_raw"],
                    }
                    for participant in entry["participants"]
                ],
            }
            for entry in spec["allocations"]
        ],
        "adjustments": [
            {
                "adjustment_index": entry["adjustment_index"],
                "adjustment_type": entry["adjustment_type"],
                "amount": entry["amount_raw"],
                "currency": entry["currency_raw"],
                "direction": entry["direction"],
                "allocation_method": entry["allocation_method"],
                "description": entry["description"],
                "participants": (
                    None
                    if entry["participants"] is None
                    else [
                        {
                            "participant_public_id": participant["participant_public_id"],
                            "share_amount": participant["share_amount_raw"],
                            "currency": participant["currency_raw"],
                        }
                        for participant in entry["participants"]
                    ]
                ),
            }
            for entry in spec["adjustments"]
        ],
        "authenticated_actor_id": command.authenticated_actor_id,
        "actor_type": command.actor_type,
        "channel": command.channel,
    }
    return _sha256_hex(_canonical_json(material))


def _input_material(receipt_public_id: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Canonical fact-set payload material (Section 12.2, content identity).

    Excludes actor/channel/command identity: two humans authoring
    identical facts produce identical input hashes.  The canonical JSON of
    this material is persisted byte-exact as the registry's
    ``canonical_fact_set_payload`` and its SHA-256 is the
    ``fact_set_input_hash``.
    """
    return {
        "schema_version": ITEM_ALLOCATION_FACTS_SCHEMA_VERSION,
        "receipt_public_id": receipt_public_id,
        "currency": facts["currency"],
        "net_paid_amount": facts["net_paid_text"],
        "items": [
            {
                "line_number": item["line_number"],
                "item_name": item["item_name"],
                "quantity": item["quantity_text"],
                "unit_price": item["unit_price_text"],
                "line_amount": item["line_amount_text"],
            }
            for item in facts["items"]
        ],
        "allocations": [
            {
                "line_number": entry["line_number"],
                "allocation_method": entry["allocation_method"],
                "participants": [
                    {
                        "participant_public_id": participant["participant_public_id"],
                        "share_amount": participant["share_text"],
                    }
                    for participant in entry["participants"]
                ],
            }
            for entry in facts["allocations"]
        ],
        "adjustments": [
            {
                "adjustment_index": entry["adjustment_index"],
                "adjustment_type": entry["adjustment_type"],
                "direction": entry["direction"],
                "amount": entry["amount_text"],
                "allocation_method": entry["allocation_method"],
                "description": entry["description"],
                "participants": (
                    None
                    if entry["participants"] is None
                    else [
                        {
                            "participant_public_id": participant["participant_public_id"],
                            "share_amount": participant["share_text"],
                        }
                        for participant in entry["participants"]
                    ]
                ),
            }
            for entry in facts["adjustments"]
        ],
    }


def _derive_row_identities(fact_set_public_id: str, facts: dict[str, Any]) -> dict[str, Any]:
    item_ids = {
        int(item["line_number"]): derive_item_public_id(
            fact_set_public_id, int(item["line_number"])
        )
        for item in facts["items"]
    }
    allocation_ids: dict[tuple[int, str], str] = {}
    for entry in facts["allocations"]:
        line = int(entry["line_number"])
        for participant in entry["participants"]:
            pid = str(participant["participant_public_id"])
            allocation_ids[(line, pid)] = derive_allocation_public_id(item_ids[line], pid)
    adjustment_ids = {
        int(entry["adjustment_index"]): derive_adjustment_public_id(
            fact_set_public_id, int(entry["adjustment_index"])
        )
        for entry in facts["adjustments"]
    }
    return {"items": item_ids, "allocations": allocation_ids, "adjustments": adjustment_ids}


def _fact_set_result_hash(
    *,
    receipt_public_id: str,
    facts: dict[str, Any],
    fact_set_public_id: str,
    version: int = 1,
    conversion_command_public_id: str,
    command_material_hash: str,
    identities: dict[str, Any],
) -> str:
    material = _input_material(receipt_public_id, facts)
    material.update(
        {
            "fact_set_public_id": fact_set_public_id,
            "version": version,
            "conversion_command_public_id": conversion_command_public_id,
            "command_material_hash": command_material_hash,
            "item_public_ids": [identities["items"][line] for line in sorted(identities["items"])],
            "allocation_public_ids": [
                identities["allocations"][key] for key in sorted(identities["allocations"])
            ],
            "adjustment_public_ids": [
                identities["adjustments"][index] for index in sorted(identities["adjustments"])
            ],
        }
    )
    return _sha256_hex(_canonical_json(material))


def _fact_counts(facts: dict[str, Any]) -> dict[str, int]:
    return {
        "item_count": len(facts["items"]),
        "allocation_count": sum(len(entry["participants"]) for entry in facts["allocations"]),
        "adjustment_count": len(facts["adjustments"]),
    }


# ---------------------------------------------------------------------------
# Writes (fixed Section 13 order: registry → items → allocations →
# adjustments → audit event)
# ---------------------------------------------------------------------------


def _insert_registry_row(
    conn: sqlite3.Connection,
    *,
    command: ReceiptItemAllocationFactsCommand | ReceiptItemAllocationFactsSupersessionCommand,
    receipt_id: int,
    fact_set_public_id: str,
    version: int = 1,
    conversion_command_public_id: str,
    expected_conversion_result_hash: str,
    supersedes_fact_set_public_id: str | None = None,
    command_material_hash: str,
    fact_set_input_hash: str,
    fact_set_result_hash: str,
    canonical_fact_set_payload: str,
    audit_event_public_id: str,
    created_at: str,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO receipt_item_allocation_fact_sets (
                command_public_id, fact_set_public_id, receipt_id, version,
                conversion_command_public_id, expected_conversion_result_hash,
                supersedes_fact_set_public_id, superseded_by_fact_set_public_id,
                command_material_hash, fact_set_input_hash, fact_set_result_hash,
                canonical_fact_set_payload, actor_type, authenticated_actor_id,
                channel, reason, audit_event_public_id, schema_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'human', ?, ?, ?, ?, ?, ?)
            """,
            (
                command.command_public_id,
                fact_set_public_id,
                receipt_id,
                version,
                conversion_command_public_id,
                expected_conversion_result_hash,
                supersedes_fact_set_public_id,
                command_material_hash,
                fact_set_input_hash,
                fact_set_result_hash,
                canonical_fact_set_payload,
                command.authenticated_actor_id,
                command.channel,
                command.reason,
                audit_event_public_id,
                ITEM_ALLOCATION_FACTS_SCHEMA_VERSION,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ItemFactsPersistenceError(
            "The fact-set registry row violated a schema constraint "
            "(identity collision or uniqueness backstop); no silent re-derivation"
        ) from exc


def _insert_items(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    fact_set_public_id: str,
    facts: dict[str, Any],
    identities: dict[str, Any],
) -> dict[int, int]:
    rowids: dict[int, int] = {}
    for item in facts["items"]:
        line = int(item["line_number"])
        try:
            cursor = conn.execute(
                """
                INSERT INTO receipt_items (
                    public_id, receipt_id, line_number, item_name, quantity,
                    unit_price, line_amount, currency, quantity_canonical_text,
                    unit_price_canonical_text, line_amount_canonical_text,
                    fact_set_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identities["items"][line],
                    receipt_id,
                    line,
                    item["item_name"],
                    item["quantity_text"],
                    item["unit_price_text"],
                    item["line_amount_text"],
                    facts["currency"],
                    item["quantity_text"],
                    item["unit_price_text"],
                    item["line_amount_text"],
                    fact_set_public_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ItemFactsPersistenceError(
                f"The fact-set item row for line {line} violated a schema constraint"
            ) from exc
        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise ItemFactsPersistenceError("Fact-set item insert returned no identity")
        rowids[line] = int(lastrowid)
    return rowids


def _insert_allocations(
    conn: sqlite3.Connection,
    *,
    fact_set_public_id: str,
    facts: dict[str, Any],
    identities: dict[str, Any],
    item_rowids: dict[int, int],
) -> None:
    for entry in facts["allocations"]:
        line = int(entry["line_number"])
        for participant in entry["participants"]:
            pid = str(participant["participant_public_id"])
            try:
                conn.execute(
                    """
                    INSERT INTO receipt_item_allocation_facts (
                        allocation_public_id, fact_set_id, receipt_item_id,
                        participant_id, allocation_method,
                        share_amount_canonical_text, share_amount,
                        schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identities["allocations"][(line, pid)],
                        fact_set_public_id,
                        item_rowids[line],
                        participant["participant_id"],
                        entry["allocation_method"],
                        participant["share_text"],
                        participant["share_text"],
                        ITEM_ALLOCATION_FACTS_SCHEMA_VERSION,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ItemFactsPersistenceError(
                    f"The allocation fact row for item line {line} and "
                    f"participant {pid!r} violated a schema constraint"
                ) from exc


def _insert_adjustments(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    fact_set_public_id: str,
    facts: dict[str, Any],
    identities: dict[str, Any],
) -> None:
    for entry in facts["adjustments"]:
        index = int(entry["adjustment_index"])
        try:
            conn.execute(
                """
                INSERT INTO receipt_adjustments (
                    public_id, receipt_id, adjustment_type, description,
                    amount, currency, direction, allocation_method,
                    adjustment_index, amount_canonical_text, fact_set_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identities["adjustments"][index],
                    receipt_id,
                    entry["adjustment_type"],
                    entry["description"],
                    entry["amount_text"],
                    facts["currency"],
                    entry["direction"],
                    entry["allocation_method"],
                    index,
                    entry["amount_text"],
                    fact_set_public_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ItemFactsPersistenceError(
                f"The fact-set adjustment row {index} violated a schema constraint"
            ) from exc


def _audit_payload(
    *,
    command_public_id: str,
    receipt_public_id: str,
    conversion_command_public_id: str,
    conversion_result_hash: str,
    fact_set_public_id: str,
    material_hash: str,
    input_hash: str,
    result_hash: str,
    counts: dict[str, int],
    authenticated_actor_id: str,
    channel: str,
) -> dict[str, Any]:
    """The frozen Section 14.1 payload; keys must equal the frozen field list."""
    return {
        "actor_type": "human",
        "adjustment_count": counts["adjustment_count"],
        "allocation_count": counts["allocation_count"],
        "authenticated_actor_id": authenticated_actor_id,
        "channel": channel,
        "command_material_hash": material_hash,
        "command_public_id": command_public_id,
        "conversion_command_public_id": conversion_command_public_id,
        "conversion_result_hash": conversion_result_hash,
        "fact_set_input_hash": input_hash,
        "fact_set_public_id": fact_set_public_id,
        "fact_set_result_hash": result_hash,
        "fact_set_version": 1,
        "item_count": counts["item_count"],
        "receipt_public_id": receipt_public_id,
        "supersedes_fact_set_public_id": None,
    }


def _audit_new_state(
    *,
    receipt_public_id: str,
    fact_set_public_id: str,
    input_hash: str,
    result_hash: str,
    version: int = 1,
) -> dict[str, Any]:
    return {
        "fact_set_status": "active",
        "receipt_public_id": receipt_public_id,
        "fact_set_public_id": fact_set_public_id,
        "fact_set_version": version,
        "fact_set_input_hash": input_hash,
        "fact_set_result_hash": result_hash,
    }


def _audit_references(
    *,
    receipt_public_id: str,
    conversion_command_public_id: str,
    conversion_result_hash: str,
    lineage: dict[str, str],
    fact_set_public_id: str,
) -> tuple[str, ...]:
    return (
        f"receipt:{receipt_public_id}",
        f"conversion:{conversion_command_public_id}",
        f"conversion-result-hash:{conversion_result_hash}",
        f"attachment-content-hash:{lineage['attachment_content_hash']}",
        f"proposal-content-hash:{lineage['proposal_content_hash']}",
        f"fact-set:{fact_set_public_id}",
    )


def _append_fact_set_audit(
    conn: sqlite3.Connection,
    *,
    command: ReceiptItemAllocationFactsCommand,
    receipt_public_id: str,
    conversion: dict[str, Any],
    lineage: dict[str, str],
    fact_set_public_id: str,
    material_hash: str,
    input_hash: str,
    result_hash: str,
    counts: dict[str, int],
    created_at: str,
) -> FinancialAuditEvent:
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE,
        causation_public_id=command.command_public_id,
    )
    # The chain head is read inside this transaction and pinned; it is
    # never caller-supplied (the Section 5 command carries no audit-hash
    # field).  Guard 15 already required a valid non-legacy receipt chain,
    # so a missing head here is persisted drift.  The whole head-read plus
    # append runs under sqlite3.Row because the audit chain module
    # requires Row mappings for chain verification.
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        head = FinancialAuditRepository(conn).head("receipt", receipt_public_id)
        if head is None:
            raise ItemFactsPersistenceError(
                "The receipt aggregate has no audit chain head; the "
                "conversion genesis event is missing"
            )
        event, idempotent = append_financial_audit_event(
            conn,
            AuditEventCommand(
                event_public_id=event_id,
                aggregate_type="receipt",
                aggregate_public_id=receipt_public_id,
                event_type=RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE,
                event_payload=_audit_payload(
                    command_public_id=command.command_public_id,
                    receipt_public_id=receipt_public_id,
                    conversion_command_public_id=str(conversion["command_public_id"]),
                    conversion_result_hash=str(conversion["conversion_result_hash"]),
                    fact_set_public_id=fact_set_public_id,
                    material_hash=material_hash,
                    input_hash=input_hash,
                    result_hash=result_hash,
                    counts=counts,
                    authenticated_actor_id=command.authenticated_actor_id,
                    channel=command.channel,
                ),
                # previous_state=None inherits the chain head's new state, so
                # audit state continuity is preserved without restating it.
                previous_state=None,
                new_state=_audit_new_state(
                    receipt_public_id=receipt_public_id,
                    fact_set_public_id=fact_set_public_id,
                    input_hash=input_hash,
                    result_hash=result_hash,
                ),
                actor_type="human",
                actor_public_id=command.authenticated_actor_id,
                # IA-D9: the authenticated command is the authorization.
                authorization_public_id=command.command_public_id,
                source_evidence_references=_audit_references(
                    receipt_public_id=receipt_public_id,
                    conversion_command_public_id=str(conversion["command_public_id"]),
                    conversion_result_hash=str(conversion["conversion_result_hash"]),
                    lineage=lineage,
                    fact_set_public_id=fact_set_public_id,
                ),
                correlation_public_id=receipt_public_id,
                causation_public_id=command.command_public_id,
                created_at=created_at,
                expected_previous_event_hash=head.event_hash,
            ),
        )
    finally:
        conn.row_factory = previous_factory
    if idempotent:
        # A fresh persistence appends a brand-new audit event; finding it
        # already recorded means another writer claimed this identity, so
        # the whole transaction is rolled back rather than adopted.
        raise ItemFactsPersistenceError(
            "A fresh fact-set persistence found its audit event already "
            "recorded; refusing to adopt an audit trail this transaction "
            "did not write"
        )
    return event


def _supersession_audit_payload(
    *,
    command_public_id: str,
    receipt_public_id: str,
    conversion_command_public_id: str,
    conversion_result_hash: str,
    fact_set_public_id: str,
    fact_set_version: int,
    material_hash: str,
    input_hash: str,
    result_hash: str,
    counts: dict[str, int],
    authenticated_actor_id: str,
    channel: str,
    supersedes_fact_set_public_id: str,
    superseded_fact_set_result_hash: str,
) -> dict[str, Any]:
    """Frozen IAF.3 supersession payload (design Section 14.2)."""
    return {
        "actor_type": "human",
        "adjustment_count": counts["adjustment_count"],
        "allocation_count": counts["allocation_count"],
        "authenticated_actor_id": authenticated_actor_id,
        "channel": channel,
        "command_material_hash": material_hash,
        "command_public_id": command_public_id,
        "conversion_command_public_id": conversion_command_public_id,
        "conversion_result_hash": conversion_result_hash,
        "fact_set_input_hash": input_hash,
        "fact_set_public_id": fact_set_public_id,
        "fact_set_result_hash": result_hash,
        "fact_set_version": fact_set_version,
        "item_count": counts["item_count"],
        "receipt_public_id": receipt_public_id,
        "superseded_fact_set_result_hash": superseded_fact_set_result_hash,
        "supersedes_fact_set_public_id": supersedes_fact_set_public_id,
    }


def _supersession_audit_references(
    *,
    receipt_public_id: str,
    conversion_command_public_id: str,
    conversion_result_hash: str,
    lineage: dict[str, str],
    fact_set_public_id: str,
    supersedes_fact_set_public_id: str,
) -> tuple[str, ...]:
    return (
        *_audit_references(
            receipt_public_id=receipt_public_id,
            conversion_command_public_id=conversion_command_public_id,
            conversion_result_hash=conversion_result_hash,
            lineage=lineage,
            fact_set_public_id=fact_set_public_id,
        ),
        f"superseded-fact-set:{supersedes_fact_set_public_id}",
    )


def _append_supersession_audit(
    conn: sqlite3.Connection,
    *,
    command: ReceiptItemAllocationFactsSupersessionCommand,
    receipt_public_id: str,
    conversion: dict[str, Any],
    lineage: dict[str, str],
    predecessor: dict[str, Any],
    fact_set_public_id: str,
    version: int,
    material_hash: str,
    input_hash: str,
    result_hash: str,
    counts: dict[str, int],
    created_at: str,
) -> FinancialAuditEvent:
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE,
        causation_public_id=command.command_public_id,
    )
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        head = FinancialAuditRepository(conn).head("receipt", receipt_public_id)
        if head is None:
            raise ItemFactsPersistenceError(
                "The receipt aggregate has no audit chain head; correction "
                "cannot bind its supersession event"
            )
        event, idempotent = append_financial_audit_event(
            conn,
            AuditEventCommand(
                event_public_id=event_id,
                aggregate_type="receipt",
                aggregate_public_id=receipt_public_id,
                event_type=RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE,
                event_payload=_supersession_audit_payload(
                    command_public_id=command.command_public_id,
                    receipt_public_id=receipt_public_id,
                    conversion_command_public_id=str(conversion["command_public_id"]),
                    conversion_result_hash=str(conversion["conversion_result_hash"]),
                    fact_set_public_id=fact_set_public_id,
                    fact_set_version=version,
                    material_hash=material_hash,
                    input_hash=input_hash,
                    result_hash=result_hash,
                    counts=counts,
                    authenticated_actor_id=command.authenticated_actor_id,
                    channel=command.channel,
                    supersedes_fact_set_public_id=str(predecessor["fact_set_public_id"]),
                    superseded_fact_set_result_hash=str(predecessor["fact_set_result_hash"]),
                ),
                previous_state=None,
                new_state=_audit_new_state(
                    receipt_public_id=receipt_public_id,
                    fact_set_public_id=fact_set_public_id,
                    input_hash=input_hash,
                    result_hash=result_hash,
                    version=version,
                ),
                actor_type="human",
                actor_public_id=command.authenticated_actor_id,
                authorization_public_id=command.command_public_id,
                source_evidence_references=_supersession_audit_references(
                    receipt_public_id=receipt_public_id,
                    conversion_command_public_id=str(conversion["command_public_id"]),
                    conversion_result_hash=str(conversion["conversion_result_hash"]),
                    lineage=lineage,
                    fact_set_public_id=fact_set_public_id,
                    supersedes_fact_set_public_id=str(predecessor["fact_set_public_id"]),
                ),
                correlation_public_id=receipt_public_id,
                causation_public_id=command.command_public_id,
                created_at=created_at,
                expected_previous_event_hash=head.event_hash,
            ),
        )
    finally:
        conn.row_factory = previous_factory
    if idempotent:
        raise ItemFactsPersistenceError(
            "A fresh fact-set correction found its supersession audit event "
            "already recorded; refusing to adopt another writer's trail"
        )
    return event


# ---------------------------------------------------------------------------
# Persisted-state verification (fresh pre-commit and replay, Section 13)
# ---------------------------------------------------------------------------


def _verify_fact_set_state(
    conn: sqlite3.Connection,
    registry: dict[str, Any],
    *,
    require_chain_head: bool,
    drift_message: str,
) -> dict[str, Any]:
    """Rebuild and verify every persisted fact-set field from durable state.

    Shared by the fresh-write pre-commit verification and by guard 5
    replay-time integrity verification (B4.1 F14/F15/F16 depth).  Nothing
    is trusted from the registry row alone: the receipt, its B4.1 trust
    surface, the canonical payload, every item/allocation/adjustment row,
    every canonical text, every NUMERIC mirror, every derived identity,
    all three hashes, the membership preconditions, and the audit binding
    are re-verified.  Any drift → ``ItemFactsPersistenceError``.
    """
    command_public_id = str(registry["command_public_id"])
    version = int(registry["version"])
    if version == 1:
        if (
            not _COMMAND_ID_RE.match(command_public_id)
            or registry["supersedes_fact_set_public_id"] is not None
        ):
            raise ItemFactsPersistenceError(drift_message)
    elif (
        version < 2
        or not _CORRECTION_COMMAND_ID_RE.match(command_public_id)
        or not isinstance(registry["supersedes_fact_set_public_id"], str)
    ):
        raise ItemFactsPersistenceError(drift_message)
    if str(registry["actor_type"]) != "human":
        raise ItemFactsPersistenceError(drift_message)
    for field in ("authenticated_actor_id", "channel"):
        value = registry[field]
        if not isinstance(value, str) or not value.strip():
            raise ItemFactsPersistenceError(drift_message)
    for field in ("command_material_hash", "fact_set_input_hash", "fact_set_result_hash"):
        value = registry[field]
        if not isinstance(value, str) or not _HASH_RE.match(value):
            raise ItemFactsPersistenceError(drift_message)
    if str(registry["schema_version"]) != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION:
        raise ItemFactsPersistenceError(drift_message)
    fact_set_public_id = str(registry["fact_set_public_id"])
    if fact_set_public_id != derive_fact_set_public_id(command_public_id):
        raise ItemFactsPersistenceError(drift_message)

    receipt_cursor = conn.execute("SELECT * FROM receipts WHERE id = ?", (registry["receipt_id"],))
    receipt_row = receipt_cursor.fetchone()
    if receipt_row is None:
        raise ItemFactsPersistenceError(drift_message)
    receipt = _row_dict(receipt_cursor, receipt_row)
    _verify_registry_lineage(conn, receipt, drift_message)

    try:
        conversion = _require_conversion_binding(conn, receipt)
        _require_b41_integrity(conn, receipt, conversion)
        lineage = _require_evidence_lineage(conn, conversion, receipt)
    except ItemFactsPersistenceError:
        raise
    except ReceiptItemAllocationFactsError as exc:
        raise ItemFactsPersistenceError(drift_message) from exc
    if str(conversion["command_public_id"]) != str(registry["conversion_command_public_id"]):
        raise ItemFactsPersistenceError(drift_message)
    if str(conversion["conversion_result_hash"]) != str(
        registry["expected_conversion_result_hash"]
    ):
        raise ItemFactsPersistenceError(drift_message)

    payload_text = str(registry["canonical_fact_set_payload"])
    if _sha256_hex(payload_text) != str(registry["fact_set_input_hash"]):
        raise ItemFactsPersistenceError(drift_message)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ItemFactsPersistenceError(drift_message) from exc
    if not isinstance(payload, dict) or _canonical_json(payload) != payload_text:
        raise ItemFactsPersistenceError(drift_message)
    if (
        payload.get("schema_version") != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION
        or payload.get("receipt_public_id") != receipt["public_id"]
        or payload.get("currency") != receipt["currency"]
        or payload.get("net_paid_amount") != receipt["net_paid_amount_canonical_text"]
    ):
        raise ItemFactsPersistenceError(drift_message)
    if (
        not isinstance(payload.get("items"), list)
        or not isinstance(payload.get("allocations"), list)
        or not isinstance(payload.get("adjustments"), list)
        or not all(isinstance(entry, dict) for entry in payload["items"])
        or not all(isinstance(entry, dict) for entry in payload["allocations"])
        or not all(isinstance(entry, dict) for entry in payload["adjustments"])
    ):
        raise ItemFactsPersistenceError(drift_message)

    membership = _load_membership_for_verification(conn, receipt, drift_message)
    item_rowids = _verify_persisted_items(
        conn, registry, payload, fact_set_public_id, drift_message
    )
    allocation_count = _verify_persisted_allocations(
        conn, payload, fact_set_public_id, item_rowids, membership, drift_message
    )
    _verify_persisted_adjustments(conn, registry, payload, fact_set_public_id, drift_message)
    _verify_no_foreign_rows(conn, registry, drift_message)

    counts = {
        "item_count": len(payload["items"]),
        "allocation_count": allocation_count,
        "adjustment_count": len(payload["adjustments"]),
    }
    identities = _payload_identities(payload, fact_set_public_id)
    result_material = dict(payload)
    result_material.update(
        {
            "fact_set_public_id": fact_set_public_id,
            "version": version,
            "conversion_command_public_id": str(registry["conversion_command_public_id"]),
            "command_material_hash": str(registry["command_material_hash"]),
            "item_public_ids": identities["item_public_ids"],
            "allocation_public_ids": identities["allocation_public_ids"],
            "adjustment_public_ids": identities["adjustment_public_ids"],
        }
    )
    if _sha256_hex(_canonical_json(result_material)) != str(registry["fact_set_result_hash"]):
        raise ItemFactsPersistenceError(drift_message)

    _verify_audit_binding(
        conn,
        registry=registry,
        receipt_public_id=str(receipt["public_id"]),
        conversion=conversion,
        lineage=lineage,
        counts=counts,
        require_chain_head=require_chain_head,
        drift_message=drift_message,
    )
    return {"receipt": receipt, "conversion": conversion, "counts": counts}


def _load_membership_for_verification(
    conn: sqlite3.Connection, receipt: dict[str, Any], drift_message: str
) -> dict[str, dict[str, Any]]:
    try:
        return _load_membership(conn, receipt)
    except ReceiptItemAllocationFactsError as exc:
        raise ItemFactsPersistenceError(drift_message) from exc


def _payload_identities(payload: dict[str, Any], fact_set_public_id: str) -> dict[str, Any]:
    item_ids = {
        int(item["line_number"]): derive_item_public_id(
            fact_set_public_id, int(item["line_number"])
        )
        for item in payload["items"]
    }
    allocation_ids: dict[tuple[int, str], str] = {}
    for entry in payload["allocations"]:
        line = int(entry["line_number"])
        for participant in entry["participants"]:
            pid = str(participant["participant_public_id"])
            allocation_ids[(line, pid)] = derive_allocation_public_id(item_ids[line], pid)
    adjustment_ids = {
        int(entry["adjustment_index"]): derive_adjustment_public_id(
            fact_set_public_id, int(entry["adjustment_index"])
        )
        for entry in payload["adjustments"]
    }
    return {
        "item_public_ids": [item_ids[line] for line in sorted(item_ids)],
        "allocation_public_ids": [allocation_ids[key] for key in sorted(allocation_ids)],
        "adjustment_public_ids": [adjustment_ids[index] for index in sorted(adjustment_ids)],
        "items": item_ids,
        "allocations": allocation_ids,
        "adjustments": adjustment_ids,
    }


def _mirror_matches(mirror_value: Any, canonical_text: str | None) -> bool:
    if canonical_text is None:
        return mirror_value is None
    mirrored = decimal_from_numeric_mirror(mirror_value)
    if mirrored is None:
        return False
    try:
        return mirrored == money_decimal(canonical_text)
    except MoneyValidationError:
        return False


def _verify_persisted_items(
    conn: sqlite3.Connection,
    registry: dict[str, Any],
    payload: dict[str, Any],
    fact_set_public_id: str,
    drift_message: str,
) -> dict[int, int]:
    cursor = conn.execute(
        "SELECT * FROM receipt_items WHERE fact_set_id = ? ORDER BY line_number",
        (fact_set_public_id,),
    )
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    expected = {int(item["line_number"]): item for item in payload["items"]}
    if len(rows) != len(expected):
        raise ItemFactsPersistenceError(drift_message)
    rowids: dict[int, int] = {}
    for row in rows:
        line = int(row["line_number"])
        item = expected.get(line)
        if item is None:
            raise ItemFactsPersistenceError(drift_message)
        if (
            row["public_id"] != derive_item_public_id(fact_set_public_id, line)
            or int(row["receipt_id"]) != int(registry["receipt_id"])
            or row["item_name"] != item["item_name"]
            or row["currency"] != payload["currency"]
            or row["quantity_canonical_text"] != item["quantity"]
            or row["unit_price_canonical_text"] != item["unit_price"]
            or row["line_amount_canonical_text"] != item["line_amount"]
            or not _mirror_matches(row["quantity"], item["quantity"])
            or not _mirror_matches(row["unit_price"], item["unit_price"])
            or not _mirror_matches(row["line_amount"], item["line_amount"])
        ):
            raise ItemFactsPersistenceError(drift_message)
        rowids[line] = int(row["id"])
    return rowids


def _verify_persisted_allocations(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    fact_set_public_id: str,
    item_rowids: dict[int, int],
    membership: dict[str, dict[str, Any]],
    drift_message: str,
) -> int:
    cursor = conn.execute(
        "SELECT af.*, p.public_id AS participant_public_id "
        "FROM receipt_item_allocation_facts af "
        "LEFT JOIN participants p ON p.id = af.participant_id "
        "WHERE af.fact_set_id = ?",
        (fact_set_public_id,),
    )
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    persisted = {
        (int(row["receipt_item_id"]), str(row["participant_public_id"])): row for row in rows
    }
    if len(persisted) != len(rows):
        raise ItemFactsPersistenceError(drift_message)
    expected_count = 0
    for entry in payload["allocations"]:
        line = int(entry["line_number"])
        item_public_id = derive_item_public_id(fact_set_public_id, line)
        for participant in entry["participants"]:
            pid = str(participant["participant_public_id"])
            expected_count += 1
            row = persisted.pop((item_rowids[line], pid), None)
            if row is None:
                raise ItemFactsPersistenceError(drift_message)
            member = membership.get(pid)
            if member is None or int(member["is_included"]) != 1:
                raise ItemFactsPersistenceError(drift_message)
            if (
                row["allocation_public_id"] != derive_allocation_public_id(item_public_id, pid)
                or int(row["participant_id"]) != int(member["participant_id"])
                or row["allocation_method"] != entry["allocation_method"]
                or row["share_amount_canonical_text"] != participant["share_amount"]
                or not _mirror_matches(row["share_amount"], participant["share_amount"])
                or str(row["schema_version"]) != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION
            ):
                raise ItemFactsPersistenceError(drift_message)
    if persisted or len(rows) != expected_count:
        raise ItemFactsPersistenceError(drift_message)
    return expected_count


def _verify_persisted_adjustments(
    conn: sqlite3.Connection,
    registry: dict[str, Any],
    payload: dict[str, Any],
    fact_set_public_id: str,
    drift_message: str,
) -> None:
    cursor = conn.execute(
        "SELECT * FROM receipt_adjustments WHERE fact_set_id = ? ORDER BY adjustment_index",
        (fact_set_public_id,),
    )
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    expected = {int(entry["adjustment_index"]): entry for entry in payload["adjustments"]}
    if len(rows) != len(expected):
        raise ItemFactsPersistenceError(drift_message)
    for row in rows:
        index = int(row["adjustment_index"])
        entry = expected.get(index)
        if entry is None:
            raise ItemFactsPersistenceError(drift_message)
        if (
            row["public_id"] != derive_adjustment_public_id(fact_set_public_id, index)
            or int(row["receipt_id"]) != int(registry["receipt_id"])
            or row["adjustment_type"] != entry["adjustment_type"]
            or row["direction"] != entry["direction"]
            or row["allocation_method"] != entry["allocation_method"]
            or row["description"] != entry["description"]
            or row["currency"] != payload["currency"]
            or row["amount_canonical_text"] != entry["amount"]
            or not _mirror_matches(row["amount"], entry["amount"])
        ):
            raise ItemFactsPersistenceError(drift_message)


def _verify_no_foreign_rows(
    conn: sqlite3.Connection, registry: dict[str, Any], drift_message: str
) -> None:
    """No untrusted ad-hoc or legacy allocation rows for the receipt."""
    adhoc_items = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ? AND fact_set_id IS NULL",
            (registry["receipt_id"],),
        ).fetchone()[0]
    )
    adhoc_adjustments = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_adjustments WHERE receipt_id = ? AND fact_set_id IS NULL",
            (registry["receipt_id"],),
        ).fetchone()[0]
    )
    legacy_allocations = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocations ria "
            "JOIN receipt_items ri ON ri.id = ria.receipt_item_id "
            "WHERE ri.receipt_id = ?",
            (registry["receipt_id"],),
        ).fetchone()[0]
    )
    if adhoc_items or adhoc_adjustments or legacy_allocations:
        raise ItemFactsPersistenceError(drift_message)


def _verify_registry_lineage(
    conn: sqlite3.Connection,
    receipt: dict[str, Any],
    drift_message: str,
) -> list[dict[str, Any]]:
    """Verify one gapless, bidirectionally linked, single-active chain."""
    cursor = conn.execute(
        "SELECT command_public_id, fact_set_public_id, receipt_id, version, "
        "conversion_command_public_id, expected_conversion_result_hash, "
        "supersedes_fact_set_public_id, superseded_by_fact_set_public_id "
        "FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? ORDER BY version",
        (receipt["id"],),
    )
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    if not rows:
        raise ItemFactsPersistenceError(drift_message)
    for index, row in enumerate(rows):
        version = index + 1
        command_public_id = str(row["command_public_id"])
        fact_set_public_id = str(row["fact_set_public_id"])
        previous = rows[index - 1] if index else None
        successor = rows[index + 1] if index + 1 < len(rows) else None
        if (
            int(row["version"]) != version
            or int(row["receipt_id"]) != int(receipt["id"])
            or fact_set_public_id != derive_fact_set_public_id(command_public_id)
        ):
            raise ItemFactsPersistenceError(drift_message)
        if version == 1:
            if (
                not _COMMAND_ID_RE.match(command_public_id)
                or row["supersedes_fact_set_public_id"] is not None
            ):
                raise ItemFactsPersistenceError(drift_message)
        elif (
            not _CORRECTION_COMMAND_ID_RE.match(command_public_id)
            or previous is None
            or row["supersedes_fact_set_public_id"] != previous["fact_set_public_id"]
        ):
            raise ItemFactsPersistenceError(drift_message)
        expected_successor = None if successor is None else successor["fact_set_public_id"]
        if row["superseded_by_fact_set_public_id"] != expected_successor:
            raise ItemFactsPersistenceError(drift_message)
        if (
            row["conversion_command_public_id"] != rows[0]["conversion_command_public_id"]
            or row["expected_conversion_result_hash"] != rows[0]["expected_conversion_result_hash"]
        ):
            raise ItemFactsPersistenceError(drift_message)
    return rows


def _verify_audit_binding(
    conn: sqlite3.Connection,
    *,
    registry: dict[str, Any],
    receipt_public_id: str,
    conversion: dict[str, Any],
    lineage: dict[str, str],
    counts: dict[str, int],
    require_chain_head: bool,
    drift_message: str,
) -> None:
    """Verify the persisted audit event to full semantic depth (F16 rule).

    Chain-hash self-consistency alone is never accepted: every semantic
    field is rebuilt from the registry row and the evidence lineage, and
    the whole receipt aggregate chain must verify.
    """
    version = int(registry["version"])
    event_type = (
        RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE
        if version == 1
        else RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE
    )
    event_id = derive_audit_event_public_id(
        aggregate_type="receipt",
        aggregate_public_id=receipt_public_id,
        event_type=event_type,
        causation_public_id=str(registry["command_public_id"]),
    )
    if str(registry["audit_event_public_id"]) != event_id:
        raise ItemFactsPersistenceError(drift_message)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        repository = FinancialAuditRepository(conn)
        fetched = repository.fetch(event_id)
        chain_check = verify_financial_audit_chain(
            conn, aggregate_type="receipt", aggregate_public_id=receipt_public_id
        )
        head = repository.head("receipt", receipt_public_id)
    finally:
        conn.row_factory = previous_factory
    if fetched is None:
        raise ItemFactsPersistenceError(drift_message)
    if version == 1:
        payload_value = _audit_payload(
            command_public_id=str(registry["command_public_id"]),
            receipt_public_id=receipt_public_id,
            conversion_command_public_id=str(conversion["command_public_id"]),
            conversion_result_hash=str(conversion["conversion_result_hash"]),
            fact_set_public_id=str(registry["fact_set_public_id"]),
            material_hash=str(registry["command_material_hash"]),
            input_hash=str(registry["fact_set_input_hash"]),
            result_hash=str(registry["fact_set_result_hash"]),
            counts=counts,
            authenticated_actor_id=str(registry["authenticated_actor_id"]),
            channel=str(registry["channel"]),
        )
        references_value = _audit_references(
            receipt_public_id=receipt_public_id,
            conversion_command_public_id=str(conversion["command_public_id"]),
            conversion_result_hash=str(conversion["conversion_result_hash"]),
            lineage=lineage,
            fact_set_public_id=str(registry["fact_set_public_id"]),
        )
    else:
        predecessor_cursor = conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets WHERE fact_set_public_id = ?",
            (registry["supersedes_fact_set_public_id"],),
        )
        predecessor_row = predecessor_cursor.fetchone()
        if predecessor_row is None:
            raise ItemFactsPersistenceError(drift_message)
        predecessor = _row_dict(predecessor_cursor, predecessor_row)
        payload_value = _supersession_audit_payload(
            command_public_id=str(registry["command_public_id"]),
            receipt_public_id=receipt_public_id,
            conversion_command_public_id=str(conversion["command_public_id"]),
            conversion_result_hash=str(conversion["conversion_result_hash"]),
            fact_set_public_id=str(registry["fact_set_public_id"]),
            fact_set_version=version,
            material_hash=str(registry["command_material_hash"]),
            input_hash=str(registry["fact_set_input_hash"]),
            result_hash=str(registry["fact_set_result_hash"]),
            counts=counts,
            authenticated_actor_id=str(registry["authenticated_actor_id"]),
            channel=str(registry["channel"]),
            supersedes_fact_set_public_id=str(registry["supersedes_fact_set_public_id"]),
            superseded_fact_set_result_hash=str(predecessor["fact_set_result_hash"]),
        )
        references_value = _supersession_audit_references(
            receipt_public_id=receipt_public_id,
            conversion_command_public_id=str(conversion["command_public_id"]),
            conversion_result_hash=str(conversion["conversion_result_hash"]),
            lineage=lineage,
            fact_set_public_id=str(registry["fact_set_public_id"]),
            supersedes_fact_set_public_id=str(registry["supersedes_fact_set_public_id"]),
        )
    expected_payload = canonical_json_text(payload_value)
    expected_new_state = canonical_json_text(
        _audit_new_state(
            receipt_public_id=receipt_public_id,
            fact_set_public_id=str(registry["fact_set_public_id"]),
            input_hash=str(registry["fact_set_input_hash"]),
            result_hash=str(registry["fact_set_result_hash"]),
            version=version,
        )
    )
    expected_references = tuple(sorted(references_value))
    if (
        fetched.event_public_id != event_id
        or fetched.aggregate_type != "receipt"
        or fetched.aggregate_public_id != receipt_public_id
        or fetched.event_type != event_type
        or fetched.actor_type != "human"
        or fetched.actor_public_id != str(registry["authenticated_actor_id"])
        or fetched.authorization_public_id != str(registry["command_public_id"])
        or fetched.causation_public_id != str(registry["command_public_id"])
        or fetched.correlation_public_id != receipt_public_id
        or fetched.created_at != str(registry["created_at"])
        or fetched.calculation_snapshot_public_id is not None
        or fetched.calculation_snapshot_hash is not None
        or fetched.source_evidence_references != expected_references
        or fetched.event_payload_json != expected_payload
        or fetched.new_state_json != expected_new_state
        or fetched.sequence_number < 2
    ):
        raise ItemFactsPersistenceError(drift_message)
    if not chain_check.valid or chain_check.legacy_without_chain:
        raise ItemFactsPersistenceError(drift_message)
    if require_chain_head and (
        head is None
        or head.event_public_id != event_id
        or chain_check.event_count != fetched.sequence_number
    ):
        raise ItemFactsPersistenceError(drift_message)


def _verify_persisted(
    conn: sqlite3.Connection,
    *,
    command: ReceiptItemAllocationFactsCommand,
    receipt: dict[str, Any],
    conversion: dict[str, Any],
    membership: dict[str, dict[str, Any]],
    facts: dict[str, Any],
    fact_set_public_id: str,
    material_hash: str,
    input_hash: str,
    result_hash: str,
    payload_text: str,
    audit_event: FinancialAuditEvent,
    now: str,
) -> None:
    """Complete pre-commit revalidation (Section 13 rules 3 and 5).

    Guards 6-15 are re-run against the state visible inside this
    transaction and compared with the cached snapshots; the persisted
    rows, hashes, mirrors, derived identities, and the audit event are
    verified field-by-field.  Every failure surfaces as
    ``ItemFactsPersistenceError`` and rolls the whole persistence back.
    """
    drift = "Persisted fact-set state does not match this command's writes"
    try:
        fresh_receipt = _require_receipt(conn, command.receipt_public_id)
        fresh_conversion = _require_conversion_binding(conn, fresh_receipt)
        fresh_membership = _require_b41_integrity(conn, fresh_receipt, fresh_conversion)
        _require_asserted_binding(command, fresh_conversion)
        _require_confirmed_unfinalized(fresh_receipt)
        _require_no_calculation_authority(conn, fresh_receipt)
    except ItemFactsPersistenceError:
        raise
    except ReceiptItemAllocationFactsError as exc:
        raise ItemFactsPersistenceError(
            "Pre-commit revalidation failed inside the fact-set transaction"
        ) from exc
    if fresh_receipt != receipt or fresh_conversion != conversion:
        raise ItemFactsPersistenceError(
            "The receipt or conversion registry row changed inside the fact-set transaction"
        )
    if fresh_membership != membership:
        raise ItemFactsPersistenceError(
            "The membership facts changed inside the fact-set transaction"
        )

    registry = _get_registry_row(conn, command.command_public_id)
    if registry is None:
        raise ItemFactsPersistenceError("Persisted fact-set registry row is missing")
    if (
        str(registry["fact_set_public_id"]) != fact_set_public_id
        or int(registry["receipt_id"]) != int(receipt["id"])
        or str(registry["conversion_command_public_id"]) != str(conversion["command_public_id"])
        or str(registry["expected_conversion_result_hash"])
        != str(conversion["conversion_result_hash"])
        or str(registry["command_material_hash"]) != material_hash
        or str(registry["fact_set_input_hash"]) != input_hash
        or str(registry["fact_set_result_hash"]) != result_hash
        or str(registry["canonical_fact_set_payload"]) != payload_text
        or str(registry["actor_type"]) != "human"
        or str(registry["authenticated_actor_id"]) != command.authenticated_actor_id
        or str(registry["channel"]) != command.channel
        or registry["reason"] != command.reason
        or str(registry["schema_version"]) != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION
        or str(registry["created_at"]) != now
        or str(registry["audit_event_public_id"]) != audit_event.event_public_id
    ):
        raise ItemFactsPersistenceError(
            "Persisted fact-set registry row does not match the command"
        )

    try:
        state = _verify_fact_set_state(conn, registry, require_chain_head=True, drift_message=drift)
    except ReceiptItemAllocationFactsError:
        raise
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ItemFactsPersistenceError(drift) from exc
    if state["counts"] != _fact_counts(facts):
        raise ItemFactsPersistenceError(drift)

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        fetched = FinancialAuditRepository(conn).fetch(audit_event.event_public_id)
    finally:
        conn.row_factory = previous_factory
    if fetched is None or fetched != audit_event:
        raise ItemFactsPersistenceError(
            "The persisted financial audit event does not match the event this persistence appended"
        )
    if fetched.created_at != now or audit_event.created_at != now:
        raise ItemFactsPersistenceError(
            "The persisted audit event timestamp does not match the fact-set timestamp"
        )


def _verify_supersession_persisted(
    conn: sqlite3.Connection,
    *,
    command: ReceiptItemAllocationFactsSupersessionCommand,
    receipt: dict[str, Any],
    conversion: dict[str, Any],
    membership: dict[str, dict[str, Any]],
    facts: dict[str, Any],
    predecessor: dict[str, Any],
    fact_set_public_id: str,
    version: int,
    material_hash: str,
    input_hash: str,
    result_hash: str,
    payload_text: str,
    audit_event: FinancialAuditEvent,
    now: str,
) -> None:
    """Complete IAF.3 pre-commit verification, including both link directions."""
    drift = "Persisted fact-set supersession does not match this correction command"
    try:
        fresh_receipt = _require_receipt(conn, command.receipt_public_id)
        fresh_conversion = _require_conversion_binding(conn, fresh_receipt)
        fresh_membership = _require_b41_integrity(conn, fresh_receipt, fresh_conversion)
        _require_asserted_binding(command, fresh_conversion)
        _require_confirmed_unfinalized(fresh_receipt)
        _require_no_finalization_authority(conn, fresh_receipt)
        _require_no_untrusted_rows(conn, fresh_receipt)
    except ItemFactsPersistenceError:
        raise
    except ReceiptItemAllocationFactsError as exc:
        raise ItemFactsPersistenceError(
            "Pre-commit revalidation failed inside the fact-set correction transaction"
        ) from exc
    if fresh_receipt != receipt or fresh_conversion != conversion:
        raise ItemFactsPersistenceError(
            "The receipt or conversion registry row changed inside the correction transaction"
        )
    if fresh_membership != membership:
        raise ItemFactsPersistenceError(
            "The membership facts changed inside the correction transaction"
        )

    predecessor_cursor = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE fact_set_public_id = ?",
        (predecessor["fact_set_public_id"],),
    )
    predecessor_row = predecessor_cursor.fetchone()
    if predecessor_row is None:
        raise ItemFactsPersistenceError("The superseded predecessor registry row is missing")
    fresh_predecessor = _row_dict(predecessor_cursor, predecessor_row)
    if (
        fresh_predecessor["superseded_by_fact_set_public_id"] != fact_set_public_id
        or str(fresh_predecessor["fact_set_result_hash"])
        != command.expected_current_fact_set_result_hash
        or int(fresh_predecessor["version"]) + 1 != version
    ):
        raise ItemFactsPersistenceError(
            "The predecessor transition does not match the correction command"
        )

    registry = _get_registry_row(conn, command.command_public_id)
    if registry is None:
        raise ItemFactsPersistenceError("Persisted successor registry row is missing")
    if (
        str(registry["fact_set_public_id"]) != fact_set_public_id
        or int(registry["receipt_id"]) != int(receipt["id"])
        or int(registry["version"]) != version
        or str(registry["supersedes_fact_set_public_id"]) != str(predecessor["fact_set_public_id"])
        or registry["superseded_by_fact_set_public_id"] is not None
        or str(registry["conversion_command_public_id"]) != str(conversion["command_public_id"])
        or str(registry["expected_conversion_result_hash"])
        != str(conversion["conversion_result_hash"])
        or str(registry["command_material_hash"]) != material_hash
        or str(registry["fact_set_input_hash"]) != input_hash
        or str(registry["fact_set_result_hash"]) != result_hash
        or str(registry["canonical_fact_set_payload"]) != payload_text
        or str(registry["actor_type"]) != "human"
        or str(registry["authenticated_actor_id"]) != command.authenticated_actor_id
        or str(registry["channel"]) != command.channel
        or registry["reason"] != command.reason
        or str(registry["schema_version"]) != ITEM_ALLOCATION_FACTS_SCHEMA_VERSION
        or str(registry["created_at"]) != now
        or str(registry["audit_event_public_id"]) != audit_event.event_public_id
    ):
        raise ItemFactsPersistenceError(
            "Persisted successor registry row does not match the correction command"
        )

    try:
        state = _verify_fact_set_state(conn, registry, require_chain_head=True, drift_message=drift)
        if state["counts"] != _fact_counts(facts):
            raise ItemFactsPersistenceError(drift)
        # The predecessor remains fully verifiable after its one permitted
        # pointer transition; all content and its original audit event stay
        # immutable.
        _verify_fact_set_state(
            conn,
            fresh_predecessor,
            require_chain_head=False,
            drift_message=drift,
        )
    except ReceiptItemAllocationFactsError:
        raise
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ItemFactsPersistenceError(drift) from exc

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        fetched = FinancialAuditRepository(conn).fetch(audit_event.event_public_id)
    finally:
        conn.row_factory = previous_factory
    if fetched is None or fetched != audit_event:
        raise ItemFactsPersistenceError(
            "The persisted supersession audit event does not match this correction"
        )
    if fetched.created_at != now or audit_event.created_at != now:
        raise ItemFactsPersistenceError(
            "The supersession audit timestamp does not match the successor timestamp"
        )


def _verify_replay(
    conn: sqlite3.Connection, existing: dict[str, Any]
) -> ReceiptItemAllocationFactsResult:
    """Guard 5 replay: verified exact replay of the recorded result, zero writes."""
    try:
        state = _verify_fact_set_state(
            conn, existing, require_chain_head=False, drift_message=_REPLAY_DRIFT_MESSAGE
        )
    except ReceiptItemAllocationFactsError:
        raise
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ItemFactsPersistenceError(_REPLAY_DRIFT_MESSAGE) from exc
    receipt = state["receipt"]
    counts = state["counts"]
    predecessor_result_hash: str | None = None
    if existing["supersedes_fact_set_public_id"] is not None:
        row = conn.execute(
            "SELECT fact_set_result_hash "
            "FROM receipt_item_allocation_fact_sets WHERE fact_set_public_id = ?",
            (existing["supersedes_fact_set_public_id"],),
        ).fetchone()
        if row is None:
            raise ItemFactsPersistenceError(_REPLAY_DRIFT_MESSAGE)
        predecessor_result_hash = str(row[0])
    return ReceiptItemAllocationFactsResult(
        command_public_id=str(existing["command_public_id"]),
        receipt_public_id=str(receipt["public_id"]),
        receipt_id=int(receipt["id"]),
        fact_set_public_id=str(existing["fact_set_public_id"]),
        fact_set_version=int(existing["version"]),
        conversion_command_public_id=str(existing["conversion_command_public_id"]),
        command_material_hash=str(existing["command_material_hash"]),
        fact_set_input_hash=str(existing["fact_set_input_hash"]),
        fact_set_result_hash=str(existing["fact_set_result_hash"]),
        item_count=counts["item_count"],
        allocation_count=counts["allocation_count"],
        adjustment_count=counts["adjustment_count"],
        audit_event_public_id=str(existing["audit_event_public_id"]),
        idempotent=True,
        supersedes_fact_set_public_id=(
            None
            if existing["supersedes_fact_set_public_id"] is None
            else str(existing["supersedes_fact_set_public_id"])
        ),
        superseded_fact_set_result_hash=predecessor_result_hash,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((col[0] for col in cursor.description), row, strict=True))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rollback_if_needed(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def _now(clock: Callable[[], str] | None) -> str:
    """Return the persistence's canonical UTC timestamp before any write.

    The registry row and the audit event created by one persistence share
    this single timestamp, and both the fresh-write verification and the
    replay audit binding compare them byte-exactly (the B4.1 rule).
    """
    if clock is None:
        parsed = datetime.now(timezone.utc)
    else:
        value = clock()
        if not isinstance(value, str):
            raise ItemFactsPersistenceError(
                "Fact-set clock must return an ISO 8601 timestamp string"
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ItemFactsPersistenceError(
                "Fact-set clock returned a non-ISO-8601 timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ItemFactsPersistenceError("Fact-set clock returned a timezone-naive timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "ADJUSTMENT_ALLOCATION_METHODS",
    "ADJUSTMENT_DIRECTIONS",
    "ADJUSTMENT_TYPES",
    "AmbiguousItemAllocationError",
    "FAILURE_INJECTION_STAGES",
    "ITEM_ALLOCATION_FACTS_SCHEMA_VERSION",
    "ITEM_ALLOCATION_METHODS",
    "IncompleteItemFactsError",
    "InvalidItemFactsCommandError",
    "InvalidItemFactsMoneyError",
    "ItemFactSetAlreadyExistsError",
    "ItemFactsCallerOwnedTransactionError",
    "ItemFactsEvidenceLineageError",
    "ItemFactsForeignKeysDisabledError",
    "ItemFactsIdempotencyConflictError",
    "ItemFactsPersistenceError",
    "ItemFactsReceiptNotFoundError",
    "ItemFactsReconciliationError",
    "ItemFactsStagingDatabaseRejectedError",
    "RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_EVENT_TYPE",
    "RECEIPT_ITEM_ALLOCATION_FACTS_PERSISTED_PAYLOAD_FIELDS",
    "RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE",
    "RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_PAYLOAD_FIELDS",
    "ReceiptItemAllocationFactsCommand",
    "ReceiptItemAllocationFactsError",
    "ReceiptItemAllocationFactsResult",
    "ReceiptItemAllocationFactsSupersessionCommand",
    "StaleItemFactSetVersionError",
    "StaleItemFactsReceiptBindingError",
    "UnauthorizedItemFactsActorError",
    "UnsupportedAllocationRuleError",
    "UnsupportedItemFactsReceiptProvenanceError",
    "SUPERSESSION_FAILURE_INJECTION_STAGES",
    "derive_adjustment_public_id",
    "derive_allocation_public_id",
    "derive_fact_set_public_id",
    "derive_item_public_id",
    "persist_receipt_item_allocation_facts",
    "supersede_receipt_item_allocation_facts",
    "verify_receipt_item_allocation_fact_set_for_review",
]
