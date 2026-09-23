"""Deterministic personal-receipt material for a controlled correction.

This module prepares hash-bound material only. The correction service owns the
write transaction, trusted approval, snapshot persistence, and audit replay.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeCalculationSnapshot,
    AuthoritativeSnapshotRepository,
    build_authoritative_snapshot,
    canonical_json_bytes,
    canonical_json_text,
    canonical_json_value,
)
from finance_core.calculation.receipt_output_serialization import (
    serialize_receipt_calculation_output,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.money import canonical_money_str, money_decimal, normalize_currency


class CorrectionReceiptError(ValueError):
    """A personal receipt cannot support the requested correction material."""


class ReceiptFields(Protocol):
    @property
    def amount(self) -> str: ...

    @property
    def currency(self) -> str: ...

    @property
    def transaction_date(self) -> str: ...

    @property
    def merchant(self) -> str | None: ...


class ReceiptSource(Protocol):
    @property
    def target_id(self) -> str: ...

    @property
    def route(self) -> str: ...

    @property
    def actor(self) -> str: ...

    @property
    def source_hash(self) -> str: ...

    @property
    def original_hash(self) -> str: ...

    @property
    def evidence_refs(self) -> tuple[str, ...]: ...

    @property
    def receipt_id(self) -> str | None: ...

    @property
    def fact_set_id(self) -> str | None: ...

    @property
    def fact_set_version(self) -> int | None: ...

    @property
    def fact_input_hash(self) -> str | None: ...

    @property
    def fact_result_hash(self) -> str | None: ...

    @property
    def snapshot_id(self) -> str | None: ...

    @property
    def snapshot_hash(self) -> str | None: ...

    @property
    def payer_id(self) -> str | None: ...

    @property
    def aggregate_id(self) -> str | None: ...


@dataclass(frozen=True)
class ReceiptMaterial:
    fact_json: str
    fact_hash: str
    snapshot: AuthoritativeCalculationSnapshot
    calculator_output_json: str
    frozen_self_json: str


def _required(value: str | None, name: str) -> str:
    if type(value) is not str or not value:
        raise CorrectionReceiptError(f"receipt {name} is missing")
    return value


def _active_self_witness(conn: sqlite3.Connection, payer_id: str) -> dict[str, object]:
    rows = conn.execute(
        "SELECT public_id, is_self, is_active FROM participants "
        "WHERE is_self = 1 AND is_active = 1 ORDER BY public_id"
    ).fetchall()
    if len(rows) != 1 or str(rows[0][0]) != payer_id:
        raise CorrectionReceiptError("receipt payer is not the unique active self participant")
    return {
        "schema": "correction-active-self-witness-v1",
        "payer_participant_public_id": payer_id,
        "active_self_count": 1,
        "payer_was_active_self": 1,
    }


def _historical_self_witness(raw: str, payer_id: str) -> dict[str, object]:
    try:
        value = canonical_json_value(raw, label="frozen correction self witness")
    except (TypeError, ValueError) as exc:
        raise CorrectionReceiptError("frozen receipt self witness is not canonical") from exc
    if (
        type(value) is not dict
        or set(value)
        != {"schema", "payer_participant_public_id", "active_self_count", "payer_was_active_self"}
        or value["schema"] != "correction-active-self-witness-v1"
        or value["payer_participant_public_id"] != payer_id
        or type(value["active_self_count"]) is not int
        or value["active_self_count"] != 1
        or type(value["payer_was_active_self"]) is not int
        or value["payer_was_active_self"] != 1
        or canonical_json_text(value) != raw
    ):
        raise CorrectionReceiptError("frozen receipt self witness does not match payer")
    return value


def _checked_previous(
    conn: sqlite3.Connection, previous_id: str, previous_hash: str
) -> AuthoritativeCalculationSnapshot:
    previous = AuthoritativeSnapshotRepository(conn).fetch(previous_id)
    if previous is None:
        raise CorrectionReceiptError("previous receipt snapshot is missing")
    previous.verify()
    if previous.combined_snapshot_hash != previous_hash:
        raise CorrectionReceiptError("previous receipt snapshot hash changed")
    return previous


def _build_receipt_material(
    conn: sqlite3.Connection,
    source: ReceiptSource,
    fields: ReceiptFields,
    fact_id: str,
    snapshot_id: str,
    authority_id: str,
    plan_id: str,
    previous_snapshot_id: str,
    previous_snapshot_hash: str,
    created_at: str,
    frozen_self_json: str | None,
) -> ReceiptMaterial:
    """Rebuild one personal receipt fact and complete authoritative snapshot.

    ``created_at`` is supplied by the caller. It is not part of the snapshot's
    combined hash; apply must replace preview's placeholder with the single
    verified decision epoch and compare the recomputed hash-bound material.
    """
    if source.route != "receipt":
        raise CorrectionReceiptError("receipt material requires a receipt source")
    receipt_id = _required(source.receipt_id, "identity")
    fact_set_id = _required(source.fact_set_id, "original fact set")
    source_snapshot_id = _required(source.snapshot_id, "original snapshot")
    source_snapshot_hash = _required(source.snapshot_hash, "original snapshot hash")
    payer_id = _required(source.payer_id, "payer")
    aggregate_id = _required(source.aggregate_id, "aggregate")
    if type(source.fact_set_version) is not int or source.fact_set_version <= 0:
        raise CorrectionReceiptError("original fact-set version is invalid")
    fact_input_hash = _required(source.fact_input_hash, "original fact input hash")
    fact_result_hash = _required(source.fact_result_hash, "original fact result hash")
    if not fields.merchant or not fields.merchant.strip():
        raise CorrectionReceiptError("receipt merchant is missing")
    if previous_snapshot_id == snapshot_id or source_snapshot_id == snapshot_id:
        raise CorrectionReceiptError("receipt snapshot cannot be its own predecessor")
    previous = _checked_previous(conn, previous_snapshot_id, previous_snapshot_hash)
    if previous.aggregate_public_id != aggregate_id:
        raise CorrectionReceiptError("previous receipt snapshot belongs to another aggregate")
    if (
        previous_snapshot_id == source_snapshot_id
        and previous_snapshot_hash != source_snapshot_hash
    ):
        raise CorrectionReceiptError("original receipt snapshot hash changed")
    witness = (
        _active_self_witness(conn, payer_id)
        if frozen_self_json is None
        else _historical_self_witness(frozen_self_json, payer_id)
    )
    currency = normalize_currency(fields.currency)
    amount = canonical_money_str(
        money_decimal(fields.amount, label="corrected receipt amount"), currency
    )
    if amount != fields.amount or currency != fields.currency:
        raise CorrectionReceiptError("receipt money is not canonical")

    fact_payload: dict[str, object] = {
        "schema": "correction-personal-receipt-facts-v1",
        "fact_id": fact_id,
        "target_id": source.target_id,
        "original_receipt_id": receipt_id,
        "original_fact_set": {
            "id": fact_set_id,
            "version": source.fact_set_version,
            "input_hash": fact_input_hash,
            "result_hash": fact_result_hash,
        },
        "original_snapshot": {"id": source_snapshot_id, "hash": source_snapshot_hash},
        "previous_snapshot": {"id": previous_snapshot_id, "hash": previous_snapshot_hash},
        "effective_fields": {
            "amount": amount,
            "currency": currency,
            "transaction_date": fields.transaction_date,
            "merchant": fields.merchant,
        },
        "lines": [
            {
                "line_number": 1,
                "item_name": "Receipt total",
                "amount": amount,
                "currency": currency,
                "quantity": None,
                "unit_price": None,
                "allocation_method": "manual",
                "allocations": {payer_id: amount},
            }
        ],
        "adjustments": [],
        "frozen_self_witness": witness,
    }
    fact_json = canonical_json_text(fact_payload)
    fact_hash = hashlib.sha256(canonical_json_bytes(fact_payload)).hexdigest()
    calculator_input = {
        "case_id": receipt_id,
        "currency": currency,
        "participants": [payer_id],
        "payer": payer_id,
        "receipts": [
            {
                "receipt_id": receipt_id,
                "merchant": fields.merchant,
                "paid_by": payer_id,
                "currency": currency,
                "net_paid": amount,
                "rounding_policy": "payer",
                "items": [
                    {
                        "description": "Receipt total",
                        "amount": amount,
                        "quantity": None,
                        "unit_price": None,
                        "allocation_method": "manual",
                        "allocations": {payer_id: amount},
                    }
                ],
                "adjustments": [],
            }
        ],
    }
    calculation = serialize_receipt_calculation_output(calculate_receipt_split(calculator_input))
    zero = canonical_money_str(money_decimal("0", label="zero collection"), currency)
    if (
        calculation.get("currency") != currency
        or calculation.get("payer") != payer_id
        or calculation.get("total_paid") != amount
        or calculation.get("payer_own_share") != amount
        or calculation.get("total_to_collect") != zero
        or calculation.get("settlement_obligations") != []
    ):
        raise CorrectionReceiptError("corrected receipt calculation is not personal total")
    calculator_output_json = canonical_json_text(calculation)
    references = tuple(
        sorted(
            set(source.evidence_refs)
            | {
                f"transaction:{source.target_id}",
                f"receipt:{receipt_id}",
                f"fact-set:{fact_set_id}",
                f"original-snapshot:{source_snapshot_id}:{source_snapshot_hash}",
                f"previous-snapshot:{previous_snapshot_id}:{previous_snapshot_hash}",
                f"correction-plan:{plan_id}",
                f"correction-fact:{fact_id}:{fact_hash}",
            }
        )
    )
    snapshot = build_authoritative_snapshot(
        snapshot_public_id=snapshot_id,
        calculation_type="receipt_split",
        aggregate_public_id=aggregate_id,
        input_payload={
            "schema": "correction-personal-receipt-input-v1",
            "original_receipt_id": receipt_id,
            "target_id": source.target_id,
            "source_hash": source.source_hash,
            "original_hash": source.original_hash,
            "original_fact_set": fact_payload["original_fact_set"],
            "original_snapshot": fact_payload["original_snapshot"],
            "previous_snapshot": fact_payload["previous_snapshot"],
            "effective_fields": fact_payload["effective_fields"],
            "correction_fact_id": fact_id,
            "correction_fact_hash": fact_hash,
            "plan_id": plan_id,
            "calculator_input": calculator_input,
        },
        output_payload=calculation,
        rules_payload={
            "schema": "correction-personal-receipt-rules-v1",
            "one_line": True,
            "payer_rounding": True,
            "no_external_obligations": True,
            "frozen_self_witness": witness,
        },
        money_contract_version="money-v1",
        currency_contract_version=f"currency-{currency}-v1",
        algorithm_version="receipt-correction-personal-total-v1",
        source_references=references,
        previous_snapshot_public_id=previous_snapshot_id,
        actor_type="human",
        actor_public_id=source.actor,
        authorization_reference=authority_id,
        finalization_status="finalized",
        created_at=created_at,
    )
    return ReceiptMaterial(
        fact_json=fact_json,
        fact_hash=fact_hash,
        snapshot=snapshot,
        calculator_output_json=calculator_output_json,
        frozen_self_json=canonical_json_text(witness),
    )


def prepare_receipt_material(
    conn: sqlite3.Connection,
    source: ReceiptSource,
    fields: ReceiptFields,
    fact_id: str,
    snapshot_id: str,
    authority_id: str,
    plan_id: str,
    previous_snapshot_id: str,
    previous_snapshot_hash: str,
    created_at: str,
) -> ReceiptMaterial:
    """Prepare a new fact with a fresh active-self witness."""
    return _build_receipt_material(
        conn,
        source,
        fields,
        fact_id,
        snapshot_id,
        authority_id,
        plan_id,
        previous_snapshot_id,
        previous_snapshot_hash,
        created_at,
        None,
    )


def verify_historical_receipt_material(
    conn: sqlite3.Connection,
    source: ReceiptSource,
    fields: ReceiptFields,
    fact_id: str,
    snapshot_id: str,
    authority_id: str,
    plan_id: str,
    previous_snapshot_id: str,
    previous_snapshot_hash: str,
    fact_json: str,
    fact_hash: str,
    snapshot_hash: str,
    snapshot_created_at: str,
    frozen_self_json: str,
) -> ReceiptMaterial:
    """Verify persisted material using its frozen witness, even after retirement.

    The correction service separately verifies the snapshot audit and sealed
    result's timestamp; this check proves all calculation and fact bytes.
    """
    expected = _build_receipt_material(
        conn,
        source,
        fields,
        fact_id,
        snapshot_id,
        authority_id,
        plan_id,
        previous_snapshot_id,
        previous_snapshot_hash,
        snapshot_created_at,
        frozen_self_json,
    )
    if (
        expected.fact_json != fact_json
        or expected.fact_hash != fact_hash
        or expected.snapshot.combined_snapshot_hash != snapshot_hash
        or expected.frozen_self_json != frozen_self_json
    ):
        raise CorrectionReceiptError("historical receipt fact or calculation changed")
    stored = AuthoritativeSnapshotRepository(conn).fetch(snapshot_id)
    if stored is None:
        raise CorrectionReceiptError("historical correction snapshot is missing")
    stored.verify()
    if stored != expected.snapshot:
        raise CorrectionReceiptError("historical correction snapshot material changed")
    return expected


__all__ = [
    "CorrectionReceiptError",
    "ReceiptMaterial",
    "prepare_receipt_material",
    "verify_historical_receipt_material",
]
