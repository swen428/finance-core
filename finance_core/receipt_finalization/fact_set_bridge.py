"""IAF.7 fact-set -> calculation -> finalization bridge (staging-only).

Connects, through existing public guarded boundaries only:

    positive readiness (B4.2)
    -> SELECT-only calculator-input projection (IAF.6)
    -> deterministic ``calculate_receipt_split``
    -> immutable authoritative calculation snapshot (bound to the active
       fact-set four-tuple)
    -> human finalization confirmation + authorization (bound to the same
       snapshot and the same four-tuple)
    -> existing guarded ``finalize_receipt_split``

The slice's core invariant is that the active IAF fact-set four-tuple
actually consumed (``fact_set_public_id``, ``fact_set_version``,
``fact_set_input_hash``, ``fact_set_result_hash``) is durably bound into the
calculation snapshot, the human authorization content hash, and the
finalization audit, and that the finalizer re-reads the live active fact set
inside its own ``BEGIN IMMEDIATE`` and fails closed on any drift.

No new business rule, migration, calculator, snapshot, finalizer,
transaction, or settlement authority is introduced.  See
``docs/design/receipt_fact_set_calculation_finalization_bridge_v1.md``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeCalculationSnapshot,
    AuthoritativeSnapshotRepository,
    build_authoritative_snapshot,
    canonical_json_text,
    canonical_json_value,
    persist_authoritative_snapshot_in_transaction,
)
from finance_core.calculation.run_persistence import (
    CalculationRunRecord,
    CalculationRunRepository,
    make_run_record,
)
from finance_core.calculators.receipt_calculator_input_projection import (
    ReceiptCalculatorInputProjection,
    project_receipt_calculator_input,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.receipt_finalization.d2_conditional import (
    D2ConditionalAuthorityError,
    build_d2_receipt_projection,
    require_d2_conditional_authority,
)

# ``_require_active_fact_set_binding`` is the single source of truth for the
# active-fact-set re-read query; the bridge and the finalizer share it so the
# stale check is identical on both the group-materialization transaction and
# the finalizer's own write transaction.  Both live in this package.
from finance_core.receipt_finalization.finalizer import (
    _require_active_fact_set_binding,
    finalize_receipt_split,
)
from finance_core.receipt_finalization.models import (
    ActiveFactSetBinding,
    ConfirmedReceiptIdentity,
    FinalizationInput,
    FinalizationOutput,
    ReceiptGroupMaterialization,
    build_finalization_content_fingerprint,
    to_settlement_obligations,
)
from finance_core.receipt_finalization.persistence import (
    FactSetBindingEvidenceError,
    append_fact_set_binding_evidence,
    read_fact_set_binding_evidence,
)
from finance_core.receipt_finalization.snapshot_authority import (
    IAF_CALCULATION_RUN_SOURCE_TYPE,
    IAF_CALCULATION_RUN_STATUS,
    IAF_CALCULATION_RUN_TYPE,
    SnapshotAuthorityError,
    read_snapshot_bound_authority,
)
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

CALCULATION_TYPE = "receipt_split"
ALGORITHM_VERSION = "receipt-split-v1"
MONEY_CONTRACT_VERSION = "money-v1"
RULES_PAYLOAD: dict[str, Any] = {"algorithm": "receipt_split"}
SNAPSHOT_FINALIZATION_STATUS = "finalized"
HUMAN_ACTOR_TYPE = "human"
RECEIPT_CALCULATION_RUN_TYPE = IAF_CALCULATION_RUN_TYPE
RECEIPT_GROUP_TYPE = "manual"
RECEIPT_GROUP_STATUS = "calculated"
RECEIPT_GROUP_SOURCE = "iaf_fact_set_bridge"
CALCULATION_RUN_SOURCE_TYPE = IAF_CALCULATION_RUN_SOURCE_TYPE
CALCULATION_RUN_STATUS = IAF_CALCULATION_RUN_STATUS


class ReceiptFactSetBridgeError(RuntimeError):
    """Base error for the IAF.7 calculation/finalization bridge."""


class BridgePreparationError(ReceiptFactSetBridgeError):
    """The prepare stage could not produce a coherent calculation snapshot."""


class BridgeAuthorizationActorError(ReceiptFactSetBridgeError):
    """A non-human actor attempted to create a finalization authorization."""


class BridgeAuthorizationConflictError(ReceiptFactSetBridgeError):
    """A durable authorization for this content exists with a different identity."""


class BridgeBindingAuthorityError(ReceiptFactSetBridgeError):
    """Caller-supplied material contradicts the durable snapshot binding.

    The active fact-set binding, the calculation material, and the confirmed
    receipt identity are one inseparable authority derived from the persisted,
    hash-verified authoritative snapshot.  A prepared DTO that disagrees with
    that durable authority is refused before any authorization is created.
    """


class BridgeCalculationRunConflictError(ReceiptFactSetBridgeError):
    """A durable calculation run exists with different material content."""


@dataclass(frozen=True)
class PreparedReceiptCalculation:
    """Immutable result of the calculation-prepare stage.

    Carries every deterministic identity the authorize and finalize stages
    need so the four-tuple binding stays constant across all three stages.
    """

    receipt_public_id: str
    receipt_group_public_id: str
    receipt_group_receipt_public_id: str
    calculation_run_public_id: str
    calculation_snapshot_id: str
    calculation_snapshot_hash: str
    authorization_id: str
    confirmation_id: str
    idempotency_key: str
    currency: str
    currency_contract_version: str
    payer_participant_public_id: str
    active_fact_set_binding: ActiveFactSetBinding
    source_evidence_refs: tuple[str, ...]
    calculation_result: dict[str, Any]
    confirmed_receipt_identity: ConfirmedReceiptIdentity
    # Per-call replay metadata is not part of the deterministic authority
    # identity. Equal prepared objects still represent the same durable
    # snapshot and fact-set binding across a re-prepare.
    idempotent_replay: bool = field(compare=False, repr=False)


@dataclass(frozen=True)
class ReceiptFinalizationAuthorization:
    """Immutable result of the human authorization stage."""

    authorization_id: str
    confirmation_id: str
    content_hash: str
    actor_type: str
    actor_id: str
    prepared: PreparedReceiptCalculation


# ---------------------------------------------------------------------------
# Deterministic identity derivation
# ---------------------------------------------------------------------------


def _now(clock: Callable[[], str] | None) -> str:
    return clock() if clock is not None else datetime.now(timezone.utc).isoformat()


def _binding_token(binding: ActiveFactSetBinding) -> str:
    canonical = "|".join(
        (
            binding.receipt_public_id,
            binding.fact_set_public_id,
            str(binding.fact_set_version),
            binding.fact_set_input_hash,
            binding.fact_set_result_hash,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _currency_contract_version(currency: str) -> str:
    return f"currency-{currency}-v1"


def _derive_ids(receipt_public_id: str, binding: ActiveFactSetBinding) -> dict[str, str]:
    """Deterministic public IDs bound to the receipt and the fact-set four-tuple.

    Re-preparing the same active fact set reuses the same IDs (idempotent);
    a superseded fact set (different result hash) yields a distinct set.
    """
    token = _binding_token(binding)
    return {
        "receipt_group_public_id": f"rgrp_{receipt_public_id}",
        "receipt_group_receipt_public_id": f"rgrpr_{receipt_public_id}_{token}",
        "calculation_run_public_id": f"calc_{receipt_public_id}_{token}",
        "calculation_snapshot_id": f"snap_{receipt_public_id}_{token}",
        "confirmation_id": f"conf_{receipt_public_id}_{token}",
        "authorization_id": f"authz_{receipt_public_id}_{token}",
        "idempotency_key": f"idemfin_{receipt_public_id}_{token}",
    }


def _source_evidence_refs(
    projection: ReceiptCalculatorInputProjection,
    binding: ActiveFactSetBinding,
) -> tuple[str, ...]:
    """Canonical, hash-bound evidence references for the downstream authority.

    The four-tuple binding strings are the durable machine-verifiable link to
    the active fact set; the conversion/proposal/attachment refs preserve the
    upstream evidence chain, and the receipt date/source channel refs preserve
    the confirmed receipt's own source identity.  The merchant is hash-bound in
    the snapshot input payload rather than encoded here, because free-text
    merchant names are not a safe canonical reference token.
    """
    evidence = projection.source_evidence
    refs = (
        f"iaf.receipt_public_id={binding.receipt_public_id}",
        f"iaf.fact_set_public_id={binding.fact_set_public_id}",
        f"iaf.fact_set_version={binding.fact_set_version}",
        f"iaf.fact_set_input_hash={binding.fact_set_input_hash}",
        f"iaf.fact_set_result_hash={binding.fact_set_result_hash}",
        f"iaf.conversion_command_public_id={evidence.conversion_command_public_id}",
        f"iaf.conversion_result_hash={evidence.conversion_result_hash}",
        f"iaf.confirmation_public_id={evidence.confirmation_public_id}",
        f"iaf.proposal_content_hash={evidence.proposal_content_hash}",
        f"iaf.attachment_content_hash={evidence.attachment_content_hash}",
        f"iaf.receipt_date={projection.receipt_date}",
        f"iaf.receipt_source_channel={projection.receipt_source_channel}",
    )
    return tuple(sorted(set(refs)))


# ---------------------------------------------------------------------------
# Stage 1: calculation prepare
# ---------------------------------------------------------------------------


def prepare_receipt_calculation(
    conn: sqlite3.Connection,
    receipt_public_id: str,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    clock: Callable[[], str] | None = None,
) -> PreparedReceiptCalculation:
    """Project, calculate, and persist an authoritative snapshot for a receipt.

    SELECT-only readiness + projection are obtained on one coherent read
    snapshot, the deterministic calculator runs, and an append-only
    authoritative snapshot bound to the active fact-set four-tuple is
    persisted alongside a receipt-scoped historical calculation run.  No
    authorization and no final financial facts are produced.  Idempotent.

    The snapshot, its finalization audit event, the ``calc_audit_runs`` row, and
    both binding evidence rows are written in **one** ``BEGIN IMMEDIATE`` Unit of
    Work, so a failure can never leave a committed snapshot without its run or
    its machine-verifiable four-tuple evidence.
    """
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    created_at = _now(clock)

    projection = project_receipt_calculator_input(conn, receipt_public_id)
    binding = ActiveFactSetBinding(
        receipt_public_id=receipt_public_id,
        fact_set_public_id=projection.fact_set_public_id,
        fact_set_version=projection.fact_set_version,
        fact_set_input_hash=projection.fact_set_input_hash,
        fact_set_result_hash=projection.fact_set_result_hash,
    )
    ids = _derive_ids(receipt_public_id, binding)
    currency = projection.currency
    cc_version = _currency_contract_version(currency)
    receipt_identity = ConfirmedReceiptIdentity(
        receipt_public_id=receipt_public_id,
        merchant=projection.receipt_merchant,
        receipt_date=projection.receipt_date,
        source_channel=projection.receipt_source_channel,
        currency=currency,
    )

    calc_result = calculate_receipt_split(projection.calculator_input)
    _require_calc_matches_projection(calc_result, projection)
    payer = str(calc_result["payer"])
    source_refs = _source_evidence_refs(projection, binding)

    # A provisional finalization input yields the exact canonical snapshot body
    # the finalizer re-verifies at finalize time (string-amount canonical JSON).
    provisional = FinalizationInput(
        calculation_run_public_id=ids["calculation_run_public_id"],
        receipt_group_public_id=ids["receipt_group_public_id"],
        currency=currency,
        payer_participant_public_id=payer,
        settlement_obligations=to_settlement_obligations(
            calc_result["settlement_obligations"], currency
        ),
        calculation_snapshot=calc_result,
        source_evidence_refs=source_refs,
    )
    output_payload = provisional.calculation_snapshot

    input_payload = _snapshot_input_payload(
        calculation_run_public_id=ids["calculation_run_public_id"],
        binding=binding,
        receipt_identity=receipt_identity,
    )

    snapshot_hash, idempotent_replay = _persist_prepare_authority(
        conn,
        ids=ids,
        binding=binding,
        input_payload=input_payload,
        output_payload=output_payload,
        currency_contract_version=cc_version,
        source_references=source_refs,
        actor_type=actor_type,
        actor_id=actor_id,
        created_at=created_at,
    )

    return PreparedReceiptCalculation(
        receipt_public_id=receipt_public_id,
        receipt_group_public_id=ids["receipt_group_public_id"],
        receipt_group_receipt_public_id=ids["receipt_group_receipt_public_id"],
        calculation_run_public_id=ids["calculation_run_public_id"],
        calculation_snapshot_id=ids["calculation_snapshot_id"],
        calculation_snapshot_hash=snapshot_hash,
        authorization_id=ids["authorization_id"],
        confirmation_id=ids["confirmation_id"],
        idempotency_key=ids["idempotency_key"],
        currency=currency,
        currency_contract_version=cc_version,
        payer_participant_public_id=payer,
        active_fact_set_binding=binding,
        source_evidence_refs=source_refs,
        calculation_result=calc_result,
        confirmed_receipt_identity=receipt_identity,
        idempotent_replay=idempotent_replay,
    )


def _snapshot_input_payload(
    *,
    calculation_run_public_id: str,
    binding: ActiveFactSetBinding,
    receipt_identity: ConfirmedReceiptIdentity,
) -> dict[str, Any]:
    """The hash-bound snapshot input payload: the single binding truth source.

    The finalizer re-derives the active fact-set four-tuple and the confirmed
    receipt identity from this payload after verifying the snapshot's hashes, so
    no caller DTO can substitute either.
    """
    return {
        "calculation_run_public_id": calculation_run_public_id,
        "receipt_public_id": binding.receipt_public_id,
        "currency": receipt_identity.currency,
        "active_fact_set_binding": binding.as_fingerprint_payload(),
        "confirmed_receipt_identity": receipt_identity.as_fingerprint_payload(),
    }


def _require_calc_matches_projection(
    calc_result: dict[str, Any],
    projection: ReceiptCalculatorInputProjection,
) -> None:
    calc_currency = str(calc_result.get("currency"))
    if calc_currency != projection.currency:
        raise BridgePreparationError(
            f"Calculator currency {calc_currency!r} does not match projection "
            f"currency {projection.currency!r}"
        )
    payer = calc_result.get("payer")
    projection_payer = projection.calculator_input.get("payer")
    if payer is None:
        raise BridgePreparationError("Calculator produced no primary payer")
    if payer != projection_payer:
        raise BridgePreparationError(
            f"Calculator payer {payer!r} does not match projection payer {projection_payer!r}"
        )


def _require_reusable_snapshot(
    existing: AuthoritativeCalculationSnapshot,
    *,
    ids: dict[str, str],
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    currency_contract_version: str,
    source_references: tuple[str, ...],
    actor_type: str,
    actor_id: str | None,
) -> None:
    """Fail closed unless an existing snapshot is byte-equivalent for reuse.

    An idempotent re-prepare must observe the same hash-consistent, correctly
    bound snapshot the first prepare persisted.  The complete hash-bound
    material is compared — input (which carries the active fact-set four-tuple
    and the confirmed receipt identity), output, rules, algorithm/Money/currency
    contract versions, source references, actor, authorization reference, and
    finalization status — by rebuilding the expected snapshot at the durable
    row's own ``created_at``.  Any drift (row corruption, a changed
    deterministic calculation, or a re-bound fact set) is refused.
    """
    existing.verify()
    expected = build_authoritative_snapshot(
        snapshot_public_id=ids["calculation_snapshot_id"],
        calculation_type=CALCULATION_TYPE,
        aggregate_public_id=ids["receipt_group_public_id"],
        input_payload=input_payload,
        output_payload=output_payload,
        rules_payload=RULES_PAYLOAD,
        money_contract_version=MONEY_CONTRACT_VERSION,
        currency_contract_version=currency_contract_version,
        algorithm_version=ALGORITHM_VERSION,
        source_references=source_references,
        actor_type=actor_type,
        actor_public_id=actor_id,
        authorization_reference=ids["authorization_id"],
        finalization_status=SNAPSHOT_FINALIZATION_STATUS,
        created_at=existing.created_at,
    )
    if existing != expected:
        raise BridgePreparationError(
            f"Existing snapshot {ids['calculation_snapshot_id']!r} does not match "
            "this deterministic preparation; refusing to reuse it"
        )


def _persist_prepare_binding_evidence(
    conn: sqlite3.Connection,
    *,
    ids: dict[str, str],
    binding: ActiveFactSetBinding,
    created_at: str,
) -> None:
    """Deprecated shim kept only so no caller silently loses atomicity.

    The prepare stage now owns one Unit of Work in
    :func:`_persist_prepare_authority`; a separate evidence-only transaction is
    exactly the split-authority defect that was fixed.
    """
    raise BridgePreparationError(
        "Prepare-stage binding evidence must be written inside the prepare Unit "
        "of Work; a separate evidence transaction is no longer supported"
    )


def _persist_prepare_authority(
    conn: sqlite3.Connection,
    *,
    ids: dict[str, str],
    binding: ActiveFactSetBinding,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    currency_contract_version: str,
    source_references: tuple[str, ...],
    actor_type: str,
    actor_id: str | None,
    created_at: str,
) -> tuple[str, bool]:
    """Persist the complete prepare-stage authority in one Unit of Work.

    One bridge-owned ``BEGIN IMMEDIATE`` writes the authoritative snapshot (with
    its finalization audit event), the receipt-scoped ``calc_audit_runs`` row
    (IA-D11), and the append-only four-tuple binding evidence for both the
    snapshot and that run.  Nothing is committed unless all of it is, so a
    snapshot can never exist without its run and its machine-verifiable binding.

    Idempotent re-prepare reuses the durable snapshot's hash rather than
    rebuilding with a new wall-clock ``created_at``; replay compares the
    complete durable record, and a conflicting durable row is a typed conflict,
    never a silently accepted idempotent result.
    """
    require_foreign_keys_enabled(conn)
    expected_run = make_run_record(
        run_id=ids["calculation_run_public_id"],
        run_type=RECEIPT_CALCULATION_RUN_TYPE,
        entity_type="receipt",
        entity_id=binding.receipt_public_id,
        rule_version=ALGORITHM_VERSION,
        status=CALCULATION_RUN_STATUS,
        source_type=CALCULATION_RUN_SOURCE_TYPE,
        source_reference=binding.fact_set_result_hash,
        created_at=created_at,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        snapshot_hash, idempotent_replay = _write_prepare_authority(
            conn,
            ids=ids,
            binding=binding,
            input_payload=input_payload,
            output_payload=output_payload,
            currency_contract_version=currency_contract_version,
            source_references=source_references,
            actor_type=actor_type,
            actor_id=actor_id,
            created_at=created_at,
            expected_run=expected_run,
        )
        conn.commit()
        return snapshot_hash, idempotent_replay
    except sqlite3.IntegrityError:
        # A concurrent identical prepare committed the same durable authority
        # first: roll back, re-read, and re-verify the complete durable truth.
        # R3-05: reconstruct expected_run using the durable snapshot's created_at
        # so the timestamp comparison is consistent with the winner's write.
        if conn.in_transaction:
            conn.rollback()
        durable_snapshot = AuthoritativeSnapshotRepository(conn).fetch(
            ids["calculation_snapshot_id"]
        )
        if durable_snapshot is not None:
            expected_run = make_run_record(
                run_id=ids["calculation_run_public_id"],
                run_type=RECEIPT_CALCULATION_RUN_TYPE,
                entity_type="receipt",
                entity_id=binding.receipt_public_id,
                rule_version=ALGORITHM_VERSION,
                status=CALCULATION_RUN_STATUS,
                source_type=CALCULATION_RUN_SOURCE_TYPE,
                source_reference=binding.fact_set_result_hash,
                created_at=durable_snapshot.created_at,
            )
        return _require_durable_prepare_authority(
            conn,
            ids=ids,
            binding=binding,
            input_payload=input_payload,
            output_payload=output_payload,
            currency_contract_version=currency_contract_version,
            source_references=source_references,
            actor_type=actor_type,
            actor_id=actor_id,
            expected_run=expected_run,
        ), True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _write_prepare_authority(
    conn: sqlite3.Connection,
    *,
    ids: dict[str, str],
    binding: ActiveFactSetBinding,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    currency_contract_version: str,
    source_references: tuple[str, ...],
    actor_type: str,
    actor_id: str | None,
    created_at: str,
    expected_run: CalculationRunRecord,
) -> tuple[str, bool]:
    """Write (or fully verify) every prepare-stage authority record."""
    existing = AuthoritativeSnapshotRepository(conn).fetch(ids["calculation_snapshot_id"])
    if existing is not None:
        _require_reusable_snapshot(
            existing,
            ids=ids,
            input_payload=input_payload,
            output_payload=output_payload,
            currency_contract_version=currency_contract_version,
            source_references=source_references,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        snapshot_hash = existing.combined_snapshot_hash
        idempotent_replay = True
        # R3-05: on existing-snapshot path, reconstruct expected_run with the
        # durable snapshot's created_at so timestamps are consistent.
        if expected_run.created_at != existing.created_at:
            expected_run = make_run_record(
                run_id=expected_run.run_id,
                run_type=expected_run.run_type,
                entity_type=expected_run.entity_type,
                entity_id=expected_run.entity_id,
                rule_version=expected_run.rule_version,
                status=expected_run.status,
                source_type=expected_run.source_type,
                source_reference=expected_run.source_reference,
                created_at=existing.created_at,
            )
    else:
        snapshot = build_authoritative_snapshot(
            snapshot_public_id=ids["calculation_snapshot_id"],
            calculation_type=CALCULATION_TYPE,
            aggregate_public_id=ids["receipt_group_public_id"],
            input_payload=input_payload,
            output_payload=output_payload,
            rules_payload=RULES_PAYLOAD,
            money_contract_version=MONEY_CONTRACT_VERSION,
            currency_contract_version=currency_contract_version,
            algorithm_version=ALGORITHM_VERSION,
            source_references=source_references,
            actor_type=actor_type,
            actor_public_id=actor_id,
            authorization_reference=ids["authorization_id"],
            finalization_status=SNAPSHOT_FINALIZATION_STATUS,
            created_at=created_at,
        )
        persist_authoritative_snapshot_in_transaction(conn, snapshot)
        snapshot_hash = snapshot.combined_snapshot_hash
        idempotent_replay = False

    repository = CalculationRunRepository(conn)
    if repository.fetch_by_run_id(expected_run.run_id) is None:
        repository.create(expected_run)
    else:
        _require_durable_calculation_run(conn, expected_run)
    try:
        append_fact_set_binding_evidence(
            conn,
            binding=binding,
            bound_record_type="calculation_snapshot",
            bound_record_public_id=ids["calculation_snapshot_id"],
            created_at=expected_run.created_at,
        )
        append_fact_set_binding_evidence(
            conn,
            binding=binding,
            bound_record_type="calculation_run",
            bound_record_public_id=expected_run.run_id,
            created_at=expected_run.created_at,
        )
    except FactSetBindingEvidenceError as exc:
        raise BridgeCalculationRunConflictError(str(exc)) from exc
    return snapshot_hash, idempotent_replay


def _require_durable_prepare_authority(
    conn: sqlite3.Connection,
    *,
    ids: dict[str, str],
    binding: ActiveFactSetBinding,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    currency_contract_version: str,
    source_references: tuple[str, ...],
    actor_type: str,
    actor_id: str | None,
    expected_run: CalculationRunRecord,
) -> str:
    """Fail closed unless the whole durable prepare authority matches.

    Used after a losing concurrent prepare: the winner's snapshot, run, and both
    binding evidence rows must be exactly what this preparation would have
    written, or there is no idempotent result to return.
    """
    durable = AuthoritativeSnapshotRepository(conn).fetch(ids["calculation_snapshot_id"])
    if durable is None:
        raise BridgePreparationError(
            f"Authoritative snapshot {ids['calculation_snapshot_id']!r} is missing after "
            "a concurrent preparation"
        )
    _require_reusable_snapshot(
        durable,
        ids=ids,
        input_payload=input_payload,
        output_payload=output_payload,
        currency_contract_version=currency_contract_version,
        source_references=source_references,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    _require_durable_calculation_run(conn, expected_run)
    _require_durable_prepare_binding_evidence(conn, ids=ids, binding=binding)
    return durable.combined_snapshot_hash


def _require_durable_calculation_run(
    conn: sqlite3.Connection,
    expected: CalculationRunRecord,
) -> None:
    """Fail closed unless the durable run record equals the expected record.

    R3-05: created_at MUST NOT be masked; the durable timestamp must match
    the expected value (derived from the snapshot's durable created_at on the
    existing-snapshot path, or the fresh prepare time on the new path).
    """
    durable = CalculationRunRepository(conn).fetch_by_run_id(expected.run_id)
    if durable is None:
        raise BridgeCalculationRunConflictError(
            f"Calculation run {expected.run_id!r} is missing after persistence"
        )
    if durable != expected:
        raise BridgeCalculationRunConflictError(
            f"A durable calculation run {expected.run_id!r} exists with different "
            "material content; refusing to reuse it"
        )


def _require_durable_prepare_binding_evidence(
    conn: sqlite3.Connection,
    *,
    ids: dict[str, str],
    binding: ActiveFactSetBinding,
) -> None:
    for bound_record_type, bound_record_public_id in (
        ("calculation_snapshot", ids["calculation_snapshot_id"]),
        ("calculation_run", ids["calculation_run_public_id"]),
    ):
        durable = read_fact_set_binding_evidence(
            conn,
            bound_record_type=bound_record_type,
            bound_record_public_id=bound_record_public_id,
        )
        if durable != binding:
            raise BridgeCalculationRunConflictError(
                f"Durable fact-set binding evidence for {bound_record_type} "
                f"{bound_record_public_id!r} does not match the active binding"
            )


# ---------------------------------------------------------------------------
# Stage 2: human authorization
# ---------------------------------------------------------------------------


def authorize_receipt_finalization(
    conn: sqlite3.Connection,
    prepared: PreparedReceiptCalculation,
    *,
    actor_id: str,
    actor_type: str = HUMAN_ACTOR_TYPE,
    clock: Callable[[], str] | None = None,
) -> ReceiptFinalizationAuthorization:
    """Bind a prepared snapshot into a human confirmation + authorization pair.

    ``actor_type`` must be ``'human'``; AI/system/automation may not create a
    finalization authorization.  The authorization ``content_hash`` is the
    finalization fingerprint, which transitively covers the snapshot hash and
    the active fact-set four-tuple.  Idempotent by deterministic identity.
    """
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    if actor_type != HUMAN_ACTOR_TYPE:
        raise BridgeAuthorizationActorError(
            f"Receipt finalization authorization requires a human actor; got {actor_type!r}"
        )
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise BridgeAuthorizationActorError(
            "Receipt finalization authorization requires a non-empty human actor id"
        )

    created_at = _now(clock)
    fin_input = _build_finalization_input(prepared, actor_type=actor_type, actor_id=actor_id)
    # The durable snapshot -- not the prepared DTO -- is the binding authority.
    binding = _require_snapshot_bound_authority(
        conn, prepared, output_payload=fin_input.calculation_snapshot
    )
    fingerprint = build_finalization_content_fingerprint(fin_input)

    final_total = str(fin_input.calculation_snapshot.get("total_paid", ""))
    if not final_total:
        raise BridgePreparationError("Calculation snapshot has no total_paid")
    obligations_json = _obligations_json(fin_input)
    participants_json = json.dumps(sorted(fin_input.participant_public_ids))
    evidence_json = json.dumps(sorted(prepared.source_evidence_refs))

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO receipt_finalization_confirmations (
                confirmation_id, receipt_group_public_id, calculation_run_public_id,
                calculation_snapshot_id, content_hash, currency, final_total,
                payer_participant_public_id, participant_public_ids_json,
                settlement_obligations_json, actor_type, actor_id,
                confirmation_state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)
            """,
            (
                prepared.confirmation_id,
                prepared.receipt_group_public_id,
                prepared.calculation_run_public_id,
                prepared.calculation_snapshot_id,
                fingerprint,
                prepared.currency,
                final_total,
                prepared.payer_participant_public_id,
                participants_json,
                obligations_json,
                actor_type,
                actor_id,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO receipt_finalization_authorizations (
                authorization_id, receipt_group_public_id, calculation_run_public_id,
                calculation_snapshot_id, confirmation_id, content_hash,
                currency, final_total, payer_participant_public_id,
                participant_public_ids_json, settlement_obligations_json,
                source_evidence_refs_json, actor_type, actor_id,
                authorization_state, authorization_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'authorized', 'v1', ?)
            """,
            (
                prepared.authorization_id,
                prepared.receipt_group_public_id,
                prepared.calculation_run_public_id,
                prepared.calculation_snapshot_id,
                prepared.confirmation_id,
                fingerprint,
                prepared.currency,
                final_total,
                prepared.payer_participant_public_id,
                participants_json,
                obligations_json,
                evidence_json,
                actor_type,
                actor_id,
                created_at,
            ),
        )
        try:
            append_fact_set_binding_evidence(
                conn,
                binding=binding,
                bound_record_type="finalization_authorization",
                bound_record_public_id=prepared.authorization_id,
                created_at=created_at,
            )
        except FactSetBindingEvidenceError as exc:
            raise BridgeAuthorizationConflictError(str(exc)) from exc
        _require_persisted_authorization_truth(
            conn,
            prepared=prepared,
            fingerprint=fingerprint,
            final_total=final_total,
            participants_json=participants_json,
            obligations_json=obligations_json,
            evidence_json=evidence_json,
            actor_type=actor_type,
            actor_id=actor_id,
            expected_authorization_version="v1",
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    return ReceiptFinalizationAuthorization(
        authorization_id=prepared.authorization_id,
        confirmation_id=prepared.confirmation_id,
        content_hash=fingerprint,
        actor_type=actor_type,
        actor_id=actor_id,
        prepared=prepared,
    )


def authorize_d2_conditional_receipt_finalization(
    conn: sqlite3.Connection,
    prepared: PreparedReceiptCalculation,
    *,
    actor_id: str,
    decision_public_id: str,
    review_public_id: str,
    clock: Callable[[], str] | None = None,
) -> ReceiptFinalizationAuthorization:
    """Create D2B authority only when the accepted review equals snapshot truth.

    This is intentionally separate from :func:`authorize_receipt_finalization`.
    It cannot create or adopt the manual ``v1`` authorization shape, and the
    finalizer accepts its version only while the immutable D2 proof remains
    fully bound to the accepted decision, review, active fact set, and
    authoritative calculation snapshot.
    """
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    if not actor_id.strip():
        raise BridgeAuthorizationActorError("D2 receipt authorization requires a human actor")

    created_at = _now(clock)
    fin_input = _build_finalization_input(prepared, actor_type=HUMAN_ACTOR_TYPE, actor_id=actor_id)
    fingerprint = build_finalization_content_fingerprint(fin_input)
    final_total = str(fin_input.calculation_snapshot.get("total_paid", ""))
    if not final_total:
        raise BridgePreparationError("Calculation snapshot has no total_paid")
    obligations_json = _obligations_json(fin_input)
    participants_json = json.dumps(sorted(fin_input.participant_public_ids))
    evidence_json = json.dumps(sorted(prepared.source_evidence_refs))

    conn.execute("BEGIN IMMEDIATE")
    try:
        binding = _require_snapshot_bound_authority(
            conn, prepared, output_payload=fin_input.calculation_snapshot
        )
        d2_authority = conn.execute(
            """
            SELECT reviews.visible_projection_hash, reviews.authenticated_actor_id,
                   reviews.receipt_fact_candidate_json, reviews.posting_path,
                   attempts.stage, confirmations.authenticated_actor_id AS confirmation_actor_id,
                   evidence.fact_set_public_id
            FROM d2_posting_decisions AS decisions
            JOIN d2_posting_reviews AS reviews
              ON reviews.review_public_id = decisions.review_public_id
            JOIN d2_posting_attempts AS attempts
              ON attempts.attempt_public_id = decisions.attempt_public_id
             AND attempts.review_public_id = reviews.review_public_id
            JOIN parser_proposal_authorizations AS confirmations
              ON confirmations.confirmation_public_id = decisions.confirmation_public_id
            JOIN d2_posting_receipt_evidence AS evidence
              ON evidence.decision_public_id = decisions.decision_public_id
             AND evidence.evidence_type = 'fact_set'
            WHERE decisions.decision_public_id = ? AND reviews.review_public_id = ?
            """,
            (decision_public_id, review_public_id),
        ).fetchone()
        if d2_authority is None:
            raise BridgeAuthorizationConflictError("D2 decision authority is incomplete")
        candidate = canonical_json_value(
            str(d2_authority["receipt_fact_candidate_json"]),
            label="D2 receipt fact candidate",
        )
        if (
            d2_authority["posting_path"] != "personal_receipt"
            or d2_authority["stage"]
            not in {"snapshot_persisted", "conditional_authorization_persisted", "finalized"}
            or d2_authority["authenticated_actor_id"] != actor_id
            or d2_authority["confirmation_actor_id"] != actor_id
            or d2_authority["fact_set_public_id"] != binding.fact_set_public_id
            or not isinstance(candidate, dict)
            or candidate.get("payer_participant_public_id") != prepared.payer_participant_public_id
        ):
            raise BridgeAuthorizationConflictError("D2 decision authority is contradictory")
        projection = build_d2_receipt_projection(
            merchant=prepared.confirmed_receipt_identity.merchant,
            receipt_date=prepared.confirmed_receipt_identity.receipt_date,
            currency=prepared.currency,
            payer_participant_public_id=prepared.payer_participant_public_id,
            calculation=prepared.calculation_result,
        )
        snapshot_projection_hash = hashlib.sha256(
            canonical_json_text(projection).encode("utf-8")
        ).hexdigest()
        reviewed_projection_hash = str(d2_authority["visible_projection_hash"])
        if reviewed_projection_hash != snapshot_projection_hash:
            raise BridgeAuthorizationConflictError(
                "Reviewed receipt projection does not equal authoritative snapshot projection"
            )
        proof_hash = hashlib.sha256(
            canonical_json_text(
                {
                    "authorization_id": prepared.authorization_id,
                    "decision_public_id": decision_public_id,
                    "review_public_id": review_public_id,
                    "fact_set_public_id": binding.fact_set_public_id,
                    "calculation_snapshot_id": prepared.calculation_snapshot_id,
                    "projection_hash": reviewed_projection_hash,
                    "proof_version": "d2_conditional_v1",
                }
            ).encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            INSERT OR IGNORE INTO receipt_finalization_confirmations (
                confirmation_id, receipt_group_public_id, calculation_run_public_id,
                calculation_snapshot_id, content_hash, currency, final_total,
                payer_participant_public_id, participant_public_ids_json,
                settlement_obligations_json, actor_type, actor_id,
                confirmation_state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'human', ?, 'confirmed', ?)
            """,
            (
                prepared.confirmation_id,
                prepared.receipt_group_public_id,
                prepared.calculation_run_public_id,
                prepared.calculation_snapshot_id,
                fingerprint,
                prepared.currency,
                final_total,
                prepared.payer_participant_public_id,
                participants_json,
                obligations_json,
                actor_id,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO receipt_finalization_authorizations (
                authorization_id, receipt_group_public_id, calculation_run_public_id,
                calculation_snapshot_id, confirmation_id, content_hash,
                currency, final_total, payer_participant_public_id,
                participant_public_ids_json, settlement_obligations_json,
                source_evidence_refs_json, actor_type, actor_id,
                authorization_state, authorization_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'human', ?,
                      'authorized', 'd2_conditional_v1', ?)
            """,
            (
                prepared.authorization_id,
                prepared.receipt_group_public_id,
                prepared.calculation_run_public_id,
                prepared.calculation_snapshot_id,
                prepared.confirmation_id,
                fingerprint,
                prepared.currency,
                final_total,
                prepared.payer_participant_public_id,
                participants_json,
                obligations_json,
                evidence_json,
                actor_id,
                created_at,
            ),
        )
        append_fact_set_binding_evidence(
            conn,
            binding=binding,
            bound_record_type="finalization_authorization",
            bound_record_public_id=prepared.authorization_id,
            created_at=created_at,
        )
        proof = conn.execute(
            "SELECT * FROM d2_conditional_authorization_proofs "
            "WHERE authorization_id = ? OR decision_public_id = ? OR review_public_id = ? "
            "OR fact_set_public_id = ? OR calculation_snapshot_id = ? "
            "OR equality_proof_hash = ?",
            (
                prepared.authorization_id,
                decision_public_id,
                review_public_id,
                binding.fact_set_public_id,
                prepared.calculation_snapshot_id,
                proof_hash,
            ),
        ).fetchone()
        if proof is None:
            conn.execute(
                """
                INSERT INTO d2_conditional_authorization_proofs (
                    authorization_id, decision_public_id, review_public_id,
                    fact_set_public_id, calculation_snapshot_id,
                    reviewed_projection_hash, snapshot_projection_hash,
                    equality_proof_hash, proof_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'd2_conditional_v1', ?)
                """,
                (
                    prepared.authorization_id,
                    decision_public_id,
                    review_public_id,
                    binding.fact_set_public_id,
                    prepared.calculation_snapshot_id,
                    reviewed_projection_hash,
                    snapshot_projection_hash,
                    proof_hash,
                    created_at,
                ),
            )
            proof = conn.execute(
                "SELECT * FROM d2_conditional_authorization_proofs WHERE authorization_id = ?",
                (prepared.authorization_id,),
            ).fetchone()
        if proof is None or any(
            str(proof[column]) != expected
            for column, expected in (
                ("decision_public_id", decision_public_id),
                ("review_public_id", review_public_id),
                ("fact_set_public_id", binding.fact_set_public_id),
                ("calculation_snapshot_id", prepared.calculation_snapshot_id),
                ("reviewed_projection_hash", reviewed_projection_hash),
                ("snapshot_projection_hash", snapshot_projection_hash),
                ("equality_proof_hash", proof_hash),
                ("proof_version", "d2_conditional_v1"),
            )
        ):
            raise BridgeAuthorizationConflictError("D2 conditional proof conflict")
        _require_persisted_authorization_truth(
            conn,
            prepared=prepared,
            fingerprint=fingerprint,
            final_total=final_total,
            participants_json=participants_json,
            obligations_json=obligations_json,
            evidence_json=evidence_json,
            actor_type=HUMAN_ACTOR_TYPE,
            actor_id=actor_id,
            expected_authorization_version="d2_conditional_v1",
        )
        require_d2_conditional_authority(
            conn,
            {
                "authorization_id": prepared.authorization_id,
                "calculation_snapshot_id": prepared.calculation_snapshot_id,
                "actor_id": actor_id,
            },
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    return ReceiptFinalizationAuthorization(
        authorization_id=prepared.authorization_id,
        confirmation_id=prepared.confirmation_id,
        content_hash=fingerprint,
        actor_type=HUMAN_ACTOR_TYPE,
        actor_id=actor_id,
        prepared=prepared,
    )


def _require_snapshot_bound_authority(
    conn: sqlite3.Connection,
    prepared: PreparedReceiptCalculation,
    *,
    output_payload: dict[str, Any],
) -> ActiveFactSetBinding:
    """Return the fact-set binding derived from the durable snapshot.

    The prepared DTO is a request.  The authority is the persisted,
    hash-verified authoritative snapshot: its hash-bound input payload carries
    the four-tuple, the confirmed receipt identity, the run, the group, and the
    currency, and its output payload carries the calculator result.  Anything the
    DTO claims that the snapshot does not is refused before a human
    authorization can be created, so an authorization can never bind material
    the snapshot never covered.
    """
    try:
        authority = read_snapshot_bound_authority(
            conn,
            snapshot_public_id=prepared.calculation_snapshot_id,
            expected_combined_hash=prepared.calculation_snapshot_hash,
        )
    except SnapshotAuthorityError as exc:
        raise BridgeBindingAuthorityError(str(exc)) from exc
    if authority is None:
        raise BridgeBindingAuthorityError(
            f"Snapshot {prepared.calculation_snapshot_id!r} carries no IAF fact-set binding; "
            "an IAF finalization authorization cannot be created from it"
        )
    mismatches: list[str] = []
    if authority.active_fact_set_binding != prepared.active_fact_set_binding:
        mismatches.append("active_fact_set_binding")
    if authority.confirmed_receipt_identity != prepared.confirmed_receipt_identity:
        mismatches.append("confirmed_receipt_identity")
    if authority.calculation_run_public_id != prepared.calculation_run_public_id:
        mismatches.append("calculation_run_public_id")
    if authority.receipt_group_public_id != prepared.receipt_group_public_id:
        mismatches.append("receipt_group_public_id")
    if authority.currency != prepared.currency:
        mismatches.append("currency")
    if authority.currency_contract_version != prepared.currency_contract_version:
        mismatches.append("currency_contract_version")
    if authority.authorization_reference != prepared.authorization_id:
        mismatches.append("authorization_reference")
    if authority.output_payload != json.loads(canonical_json_text(output_payload))["value"]:
        mismatches.append("calculation_result")
    # Source evidence is snapshot-derived truth: the human authorization may bind
    # only the exact hash-anchored bundle the snapshot carries.  A DTO that
    # dropped, reordered, duplicated, or substituted any reference is refused
    # before an authorization can record a divergent evidence set.
    if tuple(authority.source_references) != tuple(sorted(prepared.source_evidence_refs)):
        mismatches.append("source_evidence_refs")
    if mismatches:
        raise BridgeBindingAuthorityError(
            f"Prepared calculation contradicts durable snapshot "
            f"{prepared.calculation_snapshot_id!r} on: {', '.join(sorted(mismatches))}"
        )
    _require_durable_prepare_binding_evidence(
        conn,
        ids={
            "calculation_snapshot_id": prepared.calculation_snapshot_id,
            "calculation_run_public_id": prepared.calculation_run_public_id,
        },
        binding=authority.active_fact_set_binding,
    )
    _require_active_fact_set_binding(conn, authority.active_fact_set_binding)
    return authority.active_fact_set_binding


def _require_persisted_authorization_truth(
    conn: sqlite3.Connection,
    *,
    prepared: PreparedReceiptCalculation,
    fingerprint: str,
    final_total: str,
    participants_json: str,
    obligations_json: str,
    evidence_json: str,
    actor_type: str,
    actor_id: str,
    expected_authorization_version: str,
) -> None:
    """Fail closed unless the durable rows match this exact request in full.

    ``INSERT OR IGNORE`` keeps the first durable confirmation/authorization for a
    deterministic identity, so replay must compare the complete durable material
    -- every group/run/snapshot reference, the content hash, the currency, the
    final total, the payer, the participant set, the obligations, the evidence
    bundle, the human actor, the state, and the authorization version -- not just
    the hash and actor.  A durable row that differs anywhere yields no
    authorization result.  ``consumed`` is accepted so a full pipeline replay
    still reaches the finalizer's durable idempotent result.
    """
    conf = conn.execute(
        "SELECT receipt_group_public_id, calculation_run_public_id, calculation_snapshot_id, "
        "content_hash, currency, final_total, payer_participant_public_id, "
        "participant_public_ids_json, settlement_obligations_json, actor_type, actor_id, "
        "confirmation_state "
        "FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
        (prepared.confirmation_id,),
    ).fetchone()
    auth = conn.execute(
        "SELECT receipt_group_public_id, calculation_run_public_id, calculation_snapshot_id, "
        "confirmation_id, content_hash, currency, final_total, payer_participant_public_id, "
        "participant_public_ids_json, settlement_obligations_json, source_evidence_refs_json, "
        "actor_type, actor_id, authorization_state, authorization_version "
        "FROM receipt_finalization_authorizations WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()
    if conf is None or auth is None:
        raise BridgeAuthorizationConflictError(
            "Confirmation/authorization rows are missing after authorization creation"
        )

    shared: tuple[Any, ...] = (
        prepared.receipt_group_public_id,
        prepared.calculation_run_public_id,
        prepared.calculation_snapshot_id,
    )
    material: tuple[Any, ...] = (
        fingerprint,
        prepared.currency,
        final_total,
        prepared.payer_participant_public_id,
        participants_json,
        obligations_json,
    )
    durable_conf = tuple(conf)[:11]
    if durable_conf != (*shared, *material, actor_type, actor_id):
        raise BridgeAuthorizationConflictError(
            f"A durable confirmation {prepared.confirmation_id!r} exists with different "
            "material content; refusing to reuse it"
        )
    if str(conf["confirmation_state"]) != "confirmed":
        raise BridgeAuthorizationConflictError(
            f"A durable confirmation {prepared.confirmation_id!r} is in state "
            f"{str(conf['confirmation_state'])!r}; refusing to reuse it"
        )

    durable_auth = tuple(auth)[:13]
    expected_auth = (
        *shared,
        prepared.confirmation_id,
        *material,
        evidence_json,
        actor_type,
        actor_id,
    )
    if durable_auth != expected_auth:
        raise BridgeAuthorizationConflictError(
            f"A durable authorization {prepared.authorization_id!r} exists with different "
            "material content; refusing to reuse it"
        )
    if str(auth["authorization_state"]) not in ("authorized", "consumed"):
        raise BridgeAuthorizationConflictError(
            f"A durable authorization {prepared.authorization_id!r} is in state "
            f"{str(auth['authorization_state'])!r}; refusing to reuse it"
        )
    if str(auth["authorization_version"]) != expected_authorization_version:
        raise BridgeAuthorizationConflictError(
            f"A durable authorization {prepared.authorization_id!r} uses version "
            f"{str(auth['authorization_version'])!r}; refusing to reuse it"
        )


def _obligations_json(fin_input: FinalizationInput) -> str:
    return json.dumps(
        sorted(
            (
                {
                    "debtor": o.debtor_participant_public_id,
                    "creditor": o.creditor_participant_public_id,
                    "amount": str(o.amount),
                    "currency": o.currency,
                }
                for o in fin_input.settlement_obligations
            ),
            key=lambda x: (x["debtor"], x["creditor"], x["amount"]),
        )
    )


# ---------------------------------------------------------------------------
# Stage 3: finalization (thin orchestration over the existing finalizer)
# ---------------------------------------------------------------------------


def finalize_prepared_receipt(
    conn: sqlite3.Connection,
    authorization: ReceiptFinalizationAuthorization,
    *,
    clock: Callable[[], str] | None = None,
) -> FinalizationOutput:
    """Run the guarded finalizer for an authorized prepared receipt.

    The deterministic single-receipt group is no longer materialized in a
    separate committed transaction.  It is described by the authorized
    :class:`ReceiptGroupMaterialization` and created inside the finalizer's own
    ``BEGIN IMMEDIATE`` Unit of Work, so a later finalization failure rolls the
    group and its membership back with every other write and a subsequent
    fact-set supersession stays possible.
    """
    require_staging_database(conn)
    prepared = authorization.prepared
    fin_input = _build_finalization_input(
        prepared,
        actor_type=authorization.actor_type,
        actor_id=authorization.actor_id,
    )
    return finalize_receipt_split(conn, fin_input, clock=clock)


# ---------------------------------------------------------------------------
# Shared finalization-input construction
# ---------------------------------------------------------------------------


def _build_finalization_input(
    prepared: PreparedReceiptCalculation,
    *,
    actor_type: str,
    actor_id: str,
) -> FinalizationInput:
    """Rebuild the exact finalization input for both authorize and finalize.

    Deterministic in ``prepared`` so the content fingerprint computed at
    authorization time equals the one the finalizer recomputes.  The confirmed
    receipt identity and the single-receipt group materialization are part of
    that fingerprint, so the human authorization covers the canonical
    transaction metadata and the group the finalizer will create.
    """
    calc_result = prepared.calculation_result
    obligations = to_settlement_obligations(
        calc_result["settlement_obligations"], prepared.currency
    )
    return FinalizationInput(
        calculation_run_public_id=prepared.calculation_run_public_id,
        receipt_group_public_id=prepared.receipt_group_public_id,
        currency=prepared.currency,
        payer_participant_public_id=prepared.payer_participant_public_id,
        settlement_obligations=obligations,
        calculation_snapshot=calc_result,
        authorization_id=prepared.authorization_id,
        confirmation_id=prepared.confirmation_id,
        idempotency_key=prepared.idempotency_key,
        calculation_snapshot_id=prepared.calculation_snapshot_id,
        calculation_snapshot_hash=prepared.calculation_snapshot_hash,
        currency_contract_version=prepared.currency_contract_version,
        actor_type=actor_type,
        actor_id=actor_id,
        source_evidence_refs=prepared.source_evidence_refs,
        active_fact_set_binding=prepared.active_fact_set_binding,
        confirmed_receipt_identity=prepared.confirmed_receipt_identity,
        receipt_group_materialization=ReceiptGroupMaterialization(
            receipt_public_id=prepared.receipt_public_id,
            receipt_group_receipt_public_id=prepared.receipt_group_receipt_public_id,
            group_type=RECEIPT_GROUP_TYPE,
            status=RECEIPT_GROUP_STATUS,
            source=RECEIPT_GROUP_SOURCE,
        ),
    )


# ---------------------------------------------------------------------------
# Stage 4: persisted authorization recovery (B5.1a, read-only)
# ---------------------------------------------------------------------------


class BridgeRecoveryError(ReceiptFactSetBridgeError):
    """Persisted authorization recovery failed — durable truth is missing or drifted."""


def load_persisted_receipt_finalization_authorization(
    conn: sqlite3.Connection,
    authorization_id: str,
) -> ReceiptFinalizationAuthorization:
    """Recover a persisted authorization from durable truth (read-only).

    Reconstructs the complete :class:`ReceiptFinalizationAuthorization` from
    the durable authorization, confirmation, authoritative snapshot,
    calculation run, and fact-set binding evidence records.  Every field is
    cross-verified; any missing, malformed, stale, or contradictory durable
    truth fails closed with a typed error.

    This function is strictly zero-write: it does not commit, roll back,
    consume the authorization, or call the finalizer.  It exists to let the
    B5.1a staging runner verify that a previously authorized state can be
    safely rehydrated across process boundaries.

    Parameters
    ----------
    conn:
        Open staging database connection (verified by ``require_staging_database``).
    authorization_id:
        The deterministic authorization identity to recover.

    Returns
    -------
    ReceiptFinalizationAuthorization
        The recovered immutable authorization object.

    Raises
    ------
    BridgeRecoveryError
        If any durable record is missing, malformed, or contradictory.
    """
    require_staging_database(conn)
    if not authorization_id or not authorization_id.strip():
        raise BridgeRecoveryError("authorization_id must be a non-empty string")

    # -- 1. Load authorization row ----------------------------------------
    auth = conn.execute(
        "SELECT receipt_group_public_id, calculation_run_public_id, "
        "calculation_snapshot_id, confirmation_id, content_hash, currency, "
        "final_total, payer_participant_public_id, participant_public_ids_json, "
        "settlement_obligations_json, source_evidence_refs_json, actor_type, "
        "actor_id, authorization_state, authorization_version "
        "FROM receipt_finalization_authorizations WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    if auth is None:
        raise BridgeRecoveryError(
            f"Authorization {authorization_id!r} not found in durable storage"
        )

    auth_state = str(auth["authorization_state"])
    if auth_state not in ("authorized", "consumed"):
        raise BridgeRecoveryError(
            f"Authorization {authorization_id!r} is in state {auth_state!r}; "
            "only 'authorized' or 'consumed' can be recovered"
        )
    auth_version = str(auth["authorization_version"])
    if auth_version not in {"v1", "d2_conditional_v1"}:
        raise BridgeRecoveryError(
            f"Authorization {authorization_id!r} uses version {auth_version!r}; "
            "only 'v1' and 'd2_conditional_v1' are supported"
        )
    if auth_version == "d2_conditional_v1":
        try:
            require_d2_conditional_authority(
                conn, {"authorization_id": authorization_id, **dict(auth)}
            )
        except D2ConditionalAuthorityError as exc:
            raise BridgeRecoveryError(str(exc)) from exc

    receipt_group_public_id = str(auth["receipt_group_public_id"])
    calculation_run_public_id = str(auth["calculation_run_public_id"])
    calculation_snapshot_id = str(auth["calculation_snapshot_id"])
    confirmation_id = str(auth["confirmation_id"])
    content_hash = str(auth["content_hash"])
    currency = str(auth["currency"])
    final_total = str(auth["final_total"])
    payer = str(auth["payer_participant_public_id"])
    participants_json = str(auth["participant_public_ids_json"])
    obligations_json = str(auth["settlement_obligations_json"])
    evidence_json = str(auth["source_evidence_refs_json"])
    actor_type = str(auth["actor_type"])
    actor_id = str(auth["actor_id"])

    if actor_type != HUMAN_ACTOR_TYPE:
        raise BridgeRecoveryError(
            f"Authorization {authorization_id!r} actor_type is {actor_type!r}; "
            "only 'human' is valid"
        )
    if not actor_id.strip():
        raise BridgeRecoveryError(f"Authorization {authorization_id!r} has an empty actor_id")

    # -- 2. Load and verify confirmation row ------------------------------
    conf = conn.execute(
        "SELECT receipt_group_public_id, calculation_run_public_id, "
        "calculation_snapshot_id, content_hash, currency, final_total, "
        "payer_participant_public_id, participant_public_ids_json, "
        "settlement_obligations_json, actor_type, actor_id, confirmation_state "
        "FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
        (confirmation_id,),
    ).fetchone()
    if conf is None:
        raise BridgeRecoveryError(
            f"Confirmation {confirmation_id!r} not found for authorization {authorization_id!r}"
        )
    if str(conf["confirmation_state"]) != "confirmed":
        raise BridgeRecoveryError(
            f"Confirmation {confirmation_id!r} is in state "
            f"{str(conf['confirmation_state'])!r}; expected 'confirmed'"
        )
    # Cross-verify shared material.
    _recovery_require_match(
        str(conf["receipt_group_public_id"]),
        receipt_group_public_id,
        "receipt_group_public_id",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["calculation_run_public_id"]),
        calculation_run_public_id,
        "calculation_run_public_id",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["calculation_snapshot_id"]),
        calculation_snapshot_id,
        "calculation_snapshot_id",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["content_hash"]),
        content_hash,
        "content_hash",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["currency"]),
        currency,
        "currency",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["final_total"]),
        final_total,
        "final_total",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["payer_participant_public_id"]),
        payer,
        "payer_participant_public_id",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["participant_public_ids_json"]),
        participants_json,
        "participant_public_ids_json",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["settlement_obligations_json"]),
        obligations_json,
        "settlement_obligations_json",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["actor_type"]),
        actor_type,
        "actor_type",
        authorization_id,
    )
    _recovery_require_match(
        str(conf["actor_id"]),
        actor_id,
        "actor_id",
        authorization_id,
    )

    # -- 3. Load and verify authoritative snapshot ------------------------
    try:
        authority = read_snapshot_bound_authority(
            conn,
            snapshot_public_id=calculation_snapshot_id,
            expected_combined_hash="",  # Accept any hash; we verify binding below.
        )
    except SnapshotAuthorityError as exc:
        raise BridgeRecoveryError(
            f"Snapshot authority verification failed for authorization {authorization_id!r}: {exc}"
        ) from exc
    if authority is None:
        raise BridgeRecoveryError(
            f"Snapshot {calculation_snapshot_id!r} carries no IAF fact-set binding"
        )

    # Verify snapshot references match authorization.
    _recovery_require_match(
        authority.receipt_group_public_id,
        receipt_group_public_id,
        "snapshot receipt_group_public_id",
        authorization_id,
    )
    _recovery_require_match(
        authority.calculation_run_public_id,
        calculation_run_public_id,
        "snapshot calculation_run_public_id",
        authorization_id,
    )
    _recovery_require_match(
        authority.currency,
        currency,
        "snapshot currency",
        authorization_id,
    )
    if authority.authorization_reference != authorization_id:
        raise BridgeRecoveryError(
            f"Snapshot authorization_reference {authority.authorization_reference!r} "
            f"does not match authorization {authorization_id!r}"
        )

    # -- 4. Verify fact-set binding evidence ------------------------------
    binding = authority.active_fact_set_binding
    try:
        auth_binding = read_fact_set_binding_evidence(
            conn,
            bound_record_type="finalization_authorization",
            bound_record_public_id=authorization_id,
        )
    except FactSetBindingEvidenceError as exc:
        raise BridgeRecoveryError(
            f"Binding evidence read failed for authorization {authorization_id!r}: {exc}"
        ) from exc
    if auth_binding is None:
        raise BridgeRecoveryError(
            f"No fact-set binding evidence for authorization {authorization_id!r}"
        )
    if auth_binding != binding:
        raise BridgeRecoveryError(
            f"Binding evidence for authorization {authorization_id!r} does not "
            "match the snapshot-derived active fact-set binding"
        )

    snap_binding = read_fact_set_binding_evidence(
        conn,
        bound_record_type="calculation_snapshot",
        bound_record_public_id=calculation_snapshot_id,
    )
    if snap_binding != binding:
        raise BridgeRecoveryError(
            f"Snapshot binding evidence does not match the active fact-set binding "
            f"for authorization {authorization_id!r}"
        )

    run_binding = read_fact_set_binding_evidence(
        conn,
        bound_record_type="calculation_run",
        bound_record_public_id=calculation_run_public_id,
    )
    if run_binding != binding:
        raise BridgeRecoveryError(
            f"Calculation run binding evidence does not match the active fact-set "
            f"binding for authorization {authorization_id!r}"
        )

    # -- 5. Verify calculation run exists ---------------------------------
    run_repo = CalculationRunRepository(conn)
    run_record = run_repo.fetch_by_run_id(calculation_run_public_id)
    if run_record is None:
        raise BridgeRecoveryError(
            f"Calculation run {calculation_run_public_id!r} not found for "
            f"authorization {authorization_id!r}"
        )

    # -- 6. Verify source evidence refs -----------------------------------
    source_evidence_refs = tuple(sorted(json.loads(evidence_json)))
    if tuple(authority.source_references) != source_evidence_refs:
        raise BridgeRecoveryError(
            f"Source evidence refs in authorization {authorization_id!r} do not "
            "match the snapshot-derived source references"
        )

    # -- 7. Reconstruct PreparedReceiptCalculation ------------------------
    receipt_public_id = binding.receipt_public_id
    cc_version = authority.currency_contract_version

    # Derive deterministic IDs from the binding to verify consistency.
    derived_ids = _derive_ids(receipt_public_id, binding)
    _recovery_require_match(
        derived_ids["receipt_group_public_id"],
        receipt_group_public_id,
        "derived receipt_group_public_id",
        authorization_id,
    )
    _recovery_require_match(
        derived_ids["calculation_run_public_id"],
        calculation_run_public_id,
        "derived calculation_run_public_id",
        authorization_id,
    )
    _recovery_require_match(
        derived_ids["calculation_snapshot_id"],
        calculation_snapshot_id,
        "derived calculation_snapshot_id",
        authorization_id,
    )
    _recovery_require_match(
        derived_ids["confirmation_id"],
        confirmation_id,
        "derived confirmation_id",
        authorization_id,
    )
    _recovery_require_match(
        derived_ids["authorization_id"],
        authorization_id,
        "derived authorization_id",
        authorization_id,
    )

    # The calculation result is the snapshot output payload.
    calculation_result = authority.output_payload
    if not isinstance(calculation_result, dict):
        raise BridgeRecoveryError(
            f"Snapshot output payload for authorization {authorization_id!r} is not a JSON object"
        )

    # -- 7b. Cross-verify content fingerprint and monetary fields ----------
    # Construct the PreparedReceiptCalculation first, then use the same
    # _build_finalization_input as the authorize stage to recompute the
    # fingerprint and verify it matches the durable content_hash.
    prepared = PreparedReceiptCalculation(
        receipt_public_id=receipt_public_id,
        receipt_group_public_id=receipt_group_public_id,
        receipt_group_receipt_public_id=derived_ids["receipt_group_receipt_public_id"],
        calculation_run_public_id=calculation_run_public_id,
        calculation_snapshot_id=calculation_snapshot_id,
        calculation_snapshot_hash=authority.combined_snapshot_hash,
        authorization_id=authorization_id,
        confirmation_id=confirmation_id,
        idempotency_key=derived_ids["idempotency_key"],
        currency=currency,
        currency_contract_version=cc_version,
        payer_participant_public_id=payer,
        active_fact_set_binding=binding,
        source_evidence_refs=source_evidence_refs,
        calculation_result=calculation_result,
        confirmed_receipt_identity=authority.confirmed_receipt_identity,
        idempotent_replay=True,
    )

    try:
        recovery_fin_input = _build_finalization_input(
            prepared, actor_type=actor_type, actor_id=actor_id
        )
        recomputed_fingerprint = build_finalization_content_fingerprint(recovery_fin_input)
    except Exception as exc:
        raise BridgeRecoveryError(
            f"Cannot rebuild finalization fingerprint for authorization {authorization_id!r}: {exc}"
        ) from exc
    if recomputed_fingerprint != content_hash:
        raise BridgeRecoveryError(
            f"Content fingerprint mismatch for authorization {authorization_id!r}: "
            f"durable={content_hash!r}, recomputed={recomputed_fingerprint!r}"
        )
    # Verify final_total against snapshot output.
    snapshot_total = str(calculation_result.get("total_paid", ""))
    if snapshot_total != final_total:
        raise BridgeRecoveryError(
            f"final_total mismatch for authorization {authorization_id!r}: "
            f"durable={final_total!r}, snapshot={snapshot_total!r}"
        )

    return ReceiptFinalizationAuthorization(
        authorization_id=authorization_id,
        confirmation_id=confirmation_id,
        content_hash=content_hash,
        actor_type=actor_type,
        actor_id=actor_id,
        prepared=prepared,
    )


def _recovery_require_match(
    actual: str, expected: str, field_name: str, authorization_id: str
) -> None:
    """Fail closed if a durable field does not match the expected value."""
    if actual != expected:
        raise BridgeRecoveryError(
            f"Recovery of authorization {authorization_id!r} failed: "
            f"{field_name} mismatch (durable={actual!r}, expected={expected!r})"
        )


__all__ = [
    "BridgeAuthorizationActorError",
    "BridgeAuthorizationConflictError",
    "BridgeBindingAuthorityError",
    "BridgeCalculationRunConflictError",
    "BridgePreparationError",
    "BridgeRecoveryError",
    "PreparedReceiptCalculation",
    "ReceiptFactSetBridgeError",
    "ReceiptFinalizationAuthorization",
    "authorize_d2_conditional_receipt_finalization",
    "authorize_receipt_finalization",
    "finalize_prepared_receipt",
    "load_persisted_receipt_finalization_authorization",
    "prepare_receipt_calculation",
]
