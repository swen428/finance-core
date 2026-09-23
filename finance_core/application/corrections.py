"""Neutral, append-only controlled correction Application boundary.

Ports are supplied by trusted composition. This module never selects a source,
policy, key, terminal or database from an operational request.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol

from finance_core.application.correction_schema import verify_correction_schema
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_bytes,
    canonical_json_text,
    canonical_json_value,
)
from finance_core.money import (
    MoneyValidationError,
    canonical_money_str,
    normalize_currency,
    validate_amount_for_currency,
)

if TYPE_CHECKING:
    from finance_core.application.correction_receipts import ReceiptMaterial
    from finance_core.financial_audit import AuditEventCommand


class CorrectionError(ValueError):
    """A requested correction or persisted correction cannot be trusted."""


class CorrectionConflict(CorrectionError):
    """The plan is stale, already consumed differently, or otherwise conflicts."""


class CorrectionIntegrityError(CorrectionError):
    """Persisted authority or history failed verification."""


@dataclass(frozen=True)
class CorrectionFields:
    amount: str
    currency: str
    transaction_date: str
    merchant: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "transaction_date": self.transaction_date,
            "merchant": self.merchant,
        }


@dataclass(frozen=True)
class VerifiedOriginalSource:
    target_id: str
    route: str
    actor: str
    fields: CorrectionFields
    source_hash: str
    original_hash: str
    original_projection_hash: str
    source_json: str
    evidence_refs: tuple[str, ...]
    receipt_id: str | None = None
    fact_set_id: str | None = None
    fact_set_version: int | None = None
    fact_input_hash: str | None = None
    fact_result_hash: str | None = None
    snapshot_id: str | None = None
    snapshot_hash: str | None = None
    payer_id: str | None = None
    aggregate_id: str | None = None


@dataclass(frozen=True)
class TrustedApprovalBinding:
    actor: str
    key_id: str
    realm: str
    instance_id: str


@dataclass(frozen=True)
class CorrectionPlan:
    plan_id: str
    correction_id: str
    authority_id: str
    target_id: str
    route: str
    expected_version: int
    predecessor_id: str | None
    predecessor_hash: str
    before: CorrectionFields
    before_hash: str
    after: CorrectionFields
    after_hash: str
    source_hash: str
    reason: str
    actor: str
    realm: str
    key_id: str
    instance_id: str
    fact_id: str | None
    fact_hash: str | None
    snapshot_id: str | None
    snapshot_hash: str | None
    receipt_json: str | None
    created_at_epoch: int
    expires_at_epoch: int
    plan_hash: str


@dataclass(frozen=True)
class SignedDecision:
    envelope_json: str
    signature: str


@dataclass(frozen=True)
class ExpectedDecision:
    plan: CorrectionPlan
    plan_expires_at_epoch: int


@dataclass(frozen=True)
class VerifiedDecision:
    envelope: dict[str, object]
    decision_digest: str
    checked_at_epoch: int
    correction_id: str


@dataclass(frozen=True)
class ConsumptionSeal:
    material_json: str
    seal: str


@dataclass(frozen=True)
class ExpectedHistory:
    plan: CorrectionPlan
    result_core_hash: str
    checked_at_epoch: int


@dataclass(frozen=True)
class VerifiedApprovalHistory:
    decision_digest: str
    checked_at_epoch: int


@dataclass(frozen=True)
class CorrectionHistoryItem:
    correction_id: str
    version: int
    plan_id: str
    authority_id: str
    before: CorrectionFields
    after: CorrectionFields
    reason: str
    actor: str
    applied_at_epoch: int
    result_hash: str
    snapshot_id: str | None
    snapshot_hash: str | None


@dataclass(frozen=True)
class EffectiveTransaction:
    target_id: str
    route: str
    version: int
    fields: CorrectionFields
    history: tuple[CorrectionHistoryItem, ...]
    original_source: VerifiedOriginalSource


@dataclass(frozen=True)
class CorrectionApplyResult:
    applied: CorrectionHistoryItem
    current: EffectiveTransaction
    recovered: bool


class SourceVerifier(Protocol):
    def verify_original(
        self, connection: sqlite3.Connection, target_id: str
    ) -> VerifiedOriginalSource: ...


class ApprovalAuthority(Protocol):
    def current_binding(
        self, connection: sqlite3.Connection, expected_actor: str
    ) -> TrustedApprovalBinding: ...

    def verify_fresh(
        self, signed_decision: SignedDecision, expected_decision: ExpectedDecision
    ) -> VerifiedDecision: ...

    def seal_consumption(
        self, verified_decision: VerifiedDecision, result_core_hash: str
    ) -> ConsumptionSeal: ...

    def verify_history(
        self,
        decision: SignedDecision,
        consumption: ConsumptionSeal,
        expected_history: ExpectedHistory,
    ) -> VerifiedApprovalHistory: ...


_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_EDIT_KEYS = frozenset({"amount", "currency", "date", "merchant"})


def _hash(tag: str, value: object) -> str:
    payload = canonical_json_bytes(value)
    framed = tag.encode("ascii") + b"\0" + len(payload).to_bytes(8, "big") + payload
    return hashlib.sha256(framed).hexdigest()


def _fields_hash(fields: CorrectionFields) -> str:
    return _hash("finance-correction-fields-v1", fields.as_dict())


def _positive_epoch(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= 253402300799:
        raise CorrectionError(f"{label} must be a positive integer UTC second")
    return value


def _required_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorrectionIntegrityError(f"Required {label} is missing")
    return value


def _nonnegative_version(value: object) -> int:
    if type(value) is not int or value < 0:
        raise CorrectionIntegrityError("Correction version is malformed")
    return value


def _canonical_amount(raw: str, currency: str) -> str:
    value = raw.strip(" \t")
    if _AMOUNT.fullmatch(value) is None:
        raise CorrectionError("Invalid amount syntax")
    integral, dot, fraction = value.partition(".")
    if len(integral) > 18 or (dot and len(fraction) > 6) or len(value.encode()) > 25:
        raise CorrectionError("Amount exceeds lexical limit")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise CorrectionError("Invalid amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise CorrectionError("Amount must be positive and finite")
    try:
        return canonical_money_str(
            validate_amount_for_currency(amount, currency, label="correction amount"), currency
        )
    except MoneyValidationError as exc:
        raise CorrectionError("Amount does not match currency precision") from exc


def _canonical_date(raw: str) -> str:
    value = raw.strip(" \t")
    if len(value) != 10 or value[4:5] != "-" or value[7:8] != "-":
        raise CorrectionError("Invalid calendar date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise CorrectionError("Invalid calendar date") from exc
    return value


def _canonical_text(raw: str, *, label: str, limit: int) -> str:
    value = unicodedata.normalize("NFC", raw.strip())
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CorrectionError(f"Invalid {label} text") from exc
    if not value or len(encoded) > limit:
        raise CorrectionError(f"Invalid {label} length")
    if any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F or ch in "\u2028\u2029" for ch in value):
        raise CorrectionError(f"Invalid {label} control character")
    return value


def _canonical_changes(changes: Mapping[str, str], reason: str) -> tuple[dict[str, str], str]:
    if not isinstance(changes, Mapping) or not changes or set(changes) - _EDIT_KEYS:
        raise CorrectionError("Correction changes have unsupported shape or fields")
    if any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in changes.items()
    ):
        raise CorrectionError("Correction edits must be strings")
    if not isinstance(reason, str):
        raise CorrectionError("Correction reason must be text")
    canonical = dict(changes)
    if "currency" in canonical:
        if "amount" not in canonical:
            raise CorrectionError("Currency change requires an explicit amount")
        try:
            canonical["currency"] = normalize_currency(canonical["currency"])
        except MoneyValidationError as exc:
            raise CorrectionError("Invalid currency") from exc
    if "amount" in canonical:
        # Final precision is checked against the effective currency after lookup.
        value = canonical["amount"].strip(" \t")
        if _AMOUNT.fullmatch(value) is None:
            raise CorrectionError("Invalid amount syntax")
        integral, dot, fraction = value.partition(".")
        if len(integral) > 18 or (dot and len(fraction) > 6) or len(value.encode()) > 25:
            raise CorrectionError("Amount exceeds lexical limit")
        if "currency" in canonical:
            # The explicit currency is already trusted enough for a pure Money
            # check, so precision precedes date and free-text validation.
            canonical["amount"] = _canonical_amount(value, canonical["currency"])
    if "date" in canonical:
        canonical["date"] = _canonical_date(canonical["date"])
    if "merchant" in canonical:
        canonical["merchant"] = _canonical_text(canonical["merchant"], label="merchant", limit=1024)
    return canonical, _canonical_text(reason, label="reason", limit=2048)


def _after_fields(before: CorrectionFields, changes: Mapping[str, str]) -> CorrectionFields:
    currency = changes.get("currency", before.currency)
    amount = (
        _canonical_amount(changes["amount"], currency) if "amount" in changes else before.amount
    )
    return CorrectionFields(
        amount=amount,
        currency=currency,
        transaction_date=changes.get("date", before.transaction_date),
        merchant=changes.get("merchant", before.merchant),
    )


def _field_from_json(value: str) -> CorrectionFields:
    parsed = canonical_json_value(value, label="correction fields")
    if not isinstance(parsed, dict) or set(parsed) != {
        "amount",
        "currency",
        "transaction_date",
        "merchant",
    }:
        raise CorrectionIntegrityError("Correction field projection is malformed")
    if (
        not isinstance(parsed["amount"], str)
        or not isinstance(parsed["currency"], str)
        or not isinstance(parsed["transaction_date"], str)
        or (parsed["merchant"] is not None and not isinstance(parsed["merchant"], str))
    ):
        raise CorrectionIntegrityError("Correction field types are malformed")
    fields = CorrectionFields(**parsed)
    if _fields_hash(fields) != _hash("finance-correction-fields-v1", parsed):
        raise CorrectionIntegrityError("Correction field hash mismatch")
    return fields


def _row(cursor: sqlite3.Cursor) -> dict[str, object] | None:
    data = cursor.fetchone()
    return (
        None
        if data is None
        else dict(zip((col[0] for col in cursor.description), data, strict=True))
    )


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, object]]:
    names = tuple(col[0] for col in cursor.description)
    return [dict(zip(names, data, strict=True)) for data in cursor.fetchall()]


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _plan_payload(plan: CorrectionPlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "correction_id": plan.correction_id,
        "authority_id": plan.authority_id,
        "target_id": plan.target_id,
        "route": plan.route,
        "expected_version": plan.expected_version,
        "predecessor_id": plan.predecessor_id,
        "predecessor_hash": plan.predecessor_hash,
        "before": plan.before.as_dict(),
        "before_hash": plan.before_hash,
        "after": plan.after.as_dict(),
        "after_hash": plan.after_hash,
        "source_hash": plan.source_hash,
        "reason": plan.reason,
        "actor": plan.actor,
        "realm": plan.realm,
        "key_id": plan.key_id,
        "instance_id": plan.instance_id,
        "fact_id": plan.fact_id,
        "fact_hash": plan.fact_hash,
        "snapshot_id": plan.snapshot_id,
        "snapshot_hash": plan.snapshot_hash,
        "receipt_json": plan.receipt_json,
        "created_at_epoch": plan.created_at_epoch,
        "expires_at_epoch": plan.expires_at_epoch,
    }


def _plan_from_row(row: Mapping[str, object]) -> CorrectionPlan:
    before = _field_from_json(str(row["before_json"]))
    after = _field_from_json(str(row["after_json"]))
    plan = CorrectionPlan(
        plan_id=str(row["plan_id"]),
        correction_id=str(row["correction_id"]),
        authority_id=str(row["authority_id"]),
        target_id=str(row["target_id"]),
        route=str(row["route"]),
        expected_version=_nonnegative_version(row["expected_version"]),
        predecessor_id=None if row["predecessor_id"] is None else str(row["predecessor_id"]),
        predecessor_hash=str(row["predecessor_hash"]),
        before=before,
        before_hash=str(row["before_hash"]),
        after=after,
        after_hash=str(row["after_hash"]),
        source_hash=str(row["source_hash"]),
        reason=str(row["reason"]),
        actor=str(row["actor"]),
        realm=str(row["realm"]),
        key_id=str(row["key_id"]),
        instance_id=str(row["instance_id"]),
        fact_id=None if row["fact_id"] is None else str(row["fact_id"]),
        fact_hash=None if row["fact_hash"] is None else str(row["fact_hash"]),
        snapshot_id=None if row["snapshot_id"] is None else str(row["snapshot_id"]),
        snapshot_hash=None if row["snapshot_hash"] is None else str(row["snapshot_hash"]),
        receipt_json=None if row["receipt_json"] is None else str(row["receipt_json"]),
        created_at_epoch=_positive_epoch(row["created_at_epoch"], "plan creation"),
        expires_at_epoch=_positive_epoch(row["expires_at_epoch"], "plan expiry"),
        plan_hash=str(row["plan_hash"]),
    )
    if (
        plan.before_hash != _fields_hash(before)
        or plan.after_hash != _fields_hash(after)
        or plan.plan_hash != _hash("finance-correction-plan-v1", _plan_payload(plan))
        or plan.expires_at_epoch != plan.created_at_epoch + 600
    ):
        raise CorrectionIntegrityError("Correction plan material has changed")
    return plan


def _verify_source(source: VerifiedOriginalSource, target_id: str) -> None:
    if not isinstance(source, VerifiedOriginalSource) or source.target_id != target_id:
        raise CorrectionIntegrityError("Source verifier returned a different target")
    if source.route not in {"text", "receipt"} or not source.actor:
        raise CorrectionIntegrityError("Source route or actor is unsupported")
    if any(
        _HASH.fullmatch(value) is None
        for value in (source.source_hash, source.original_hash, source.original_projection_hash)
    ):
        raise CorrectionIntegrityError("Source hash is malformed")
    if hashlib.sha256(source.source_json.encode("utf-8")).hexdigest() != source.source_hash:
        raise CorrectionIntegrityError("Source projection hash mismatch")
    canonical_json_value(source.source_json, label="verified original source")
    if (
        hashlib.sha256(canonical_json_bytes(source.fields.as_dict())).hexdigest()
        != source.original_projection_hash
    ):
        raise CorrectionIntegrityError("Original four-field projection mismatch")
    if source.route == "receipt" and any(
        value is None
        for value in (
            source.receipt_id,
            source.fact_set_id,
            source.fact_set_version,
            source.fact_input_hash,
            source.fact_result_hash,
            source.snapshot_id,
            source.snapshot_hash,
            source.payer_id,
            source.aggregate_id,
        )
    ):
        raise CorrectionIntegrityError("Receipt original source is incomplete")


def _verify_anchor(row: Mapping[str, object] | None, source: VerifiedOriginalSource) -> None:
    if row is None:
        return
    expected = {
        "target_id": source.target_id,
        "route": source.route,
        "actor": source.actor,
        "source_json": source.source_json,
        "source_hash": source.source_hash,
        "original_hash": source.original_hash,
        "projection_json": canonical_json_text(source.fields.as_dict()),
        "projection_hash": source.original_projection_hash,
        "receipt_id": source.receipt_id,
        "fact_set_id": source.fact_set_id,
        "original_snapshot_id": source.snapshot_id,
        "original_snapshot_hash": source.snapshot_hash,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise CorrectionIntegrityError("Original correction anchor no longer matches source")


def _assert_binding(binding: TrustedApprovalBinding, plan: CorrectionPlan) -> None:
    if not isinstance(binding, TrustedApprovalBinding) or (
        binding.actor,
        binding.key_id,
        binding.realm,
        binding.instance_id,
    ) != (plan.actor, plan.key_id, plan.realm, plan.instance_id):
        raise CorrectionConflict("Current approval binding differs from the plan")


def _result_core(
    plan: CorrectionPlan,
    *,
    version: int,
    decision_digest: str,
    checked_at_epoch: int,
    snapshot_created_at: str | None,
) -> dict[str, object]:
    return {
        "schema": "correction-result-core-v1",
        "correction_id": plan.correction_id,
        "target_id": plan.target_id,
        "version": version,
        "predecessor_id": plan.predecessor_id,
        "predecessor_hash": plan.predecessor_hash,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "authority_id": plan.authority_id,
        "decision_digest": decision_digest,
        "source_hash": plan.source_hash,
        "before_hash": plan.before_hash,
        "after_hash": plan.after_hash,
        "reason": plan.reason,
        "actor": plan.actor,
        "apply_epoch": checked_at_epoch,
        "apply_utc": _utc(checked_at_epoch),
        "instance_id": plan.instance_id,
        "fact_id": plan.fact_id,
        "fact_hash": plan.fact_hash,
        "snapshot_id": plan.snapshot_id,
        "snapshot_hash": plan.snapshot_hash,
        "snapshot_created_at": snapshot_created_at,
    }


def _correction_audit_command(
    source: VerifiedOriginalSource,
    plan: CorrectionPlan,
    result_hash: str,
    version: int,
    epoch: int,
) -> AuditEventCommand:
    from finance_core.financial_audit import AuditEventCommand, derive_audit_event_public_id

    event_type = "transaction_correction_applied"
    event_id = derive_audit_event_public_id(
        aggregate_type="transaction",
        aggregate_public_id=plan.target_id,
        event_type=event_type,
        causation_public_id=plan.correction_id,
    )
    return AuditEventCommand(
        event_public_id=event_id,
        aggregate_type="transaction",
        aggregate_public_id=plan.target_id,
        event_type=event_type,
        event_payload={
            "schema": "transaction-correction-applied-v1",
            "correction_id": plan.correction_id,
            "result_core_hash": result_hash,
            "source_hash": plan.source_hash,
            "before_hash": plan.before_hash,
            "after_hash": plan.after_hash,
            "snapshot_id": plan.snapshot_id,
            "snapshot_hash": plan.snapshot_hash,
        },
        new_state={
            "schema": "transaction-correction-state-v1",
            "target_id": plan.target_id,
            "version": version,
            "fields": plan.after.as_dict(),
            "result_core_hash": result_hash,
        },
        actor_type="human",
        actor_public_id=plan.actor,
        correlation_public_id=plan.plan_id,
        causation_public_id=plan.correction_id,
        created_at=_utc(epoch),
        authorization_public_id=plan.authority_id,
        calculation_snapshot_public_id=plan.snapshot_id,
        calculation_snapshot_hash=plan.snapshot_hash,
        source_evidence_references=source.evidence_refs,
    )


class CorrectionService:
    """One connection, one verified source port and one trusted approval port."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        source_verifier: SourceVerifier,
        approval_authority: ApprovalAuthority,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("A SQLite connection is required")
        if source_verifier is None or approval_authority is None:
            raise TypeError("Both trusted correction ports are required")
        for method in ("verify_original",):
            if not callable(getattr(source_verifier, method, None)):
                raise TypeError(f"Source port lacks {method}")
        for method in ("current_binding", "verify_fresh", "seal_consumption", "verify_history"):
            if not callable(getattr(approval_authority, method, None)):
                raise TypeError(f"Approval port lacks {method}")
        self.connection = connection
        self.source_verifier = source_verifier
        self.approval_authority = approval_authority

    def _source(self, conn: sqlite3.Connection, target_id: str) -> VerifiedOriginalSource:
        source = self.source_verifier.verify_original(conn, target_id)
        _verify_source(source, target_id)
        return source

    def lookup(self, target_id: str) -> EffectiveTransaction:
        conn = self.connection
        owns_snapshot = not conn.in_transaction
        if owns_snapshot:
            conn.execute("BEGIN")
        try:
            result = self.lookup_in_snapshot(conn, target_id)
            if owns_snapshot:
                conn.commit()
            return result
        except Exception:
            if owns_snapshot and conn.in_transaction:
                conn.rollback()
            raise

    def lookup_in_snapshot(self, conn: sqlite3.Connection, target_id: str) -> EffectiveTransaction:
        """Join a caller snapshot or own a deferred read snapshot."""
        if conn is not self.connection:
            raise CorrectionError("Lookup must use the service's verified connection")
        owns_snapshot = not conn.in_transaction
        if owns_snapshot:
            conn.execute("BEGIN")
        try:
            result = self._lookup_in_existing_snapshot(conn, target_id)
            if owns_snapshot:
                conn.commit()
            return result
        except Exception:
            if owns_snapshot and conn.in_transaction:
                conn.rollback()
            raise

    def _lookup_in_existing_snapshot(
        self, conn: sqlite3.Connection, target_id: str
    ) -> EffectiveTransaction:
        if conn is not self.connection:
            raise CorrectionError("Lookup must use the service's verified connection")
        if not isinstance(target_id, str) or not target_id:
            raise CorrectionError("Transaction ID is required")
        if not verify_correction_schema(conn):
            raise CorrectionIntegrityError("Correction migration 051 is not installed")
        source = self._source(conn, target_id)
        binding = self.approval_authority.current_binding(conn, source.actor)
        if not isinstance(binding, TrustedApprovalBinding) or binding.actor != source.actor:
            raise CorrectionIntegrityError("Current approval actor binding is invalid")
        anchor = _row(
            conn.execute("SELECT * FROM correction_targets WHERE target_id = ?", (target_id,))
        )
        _verify_anchor(anchor, source)
        if anchor is not None and (anchor["realm"], anchor["key_id"]) != (
            binding.realm,
            binding.key_id,
        ):
            raise CorrectionIntegrityError("Current approval realm or key differs from anchor")
        rows = _rows(
            conn.execute(
                "SELECT * FROM correction_versions WHERE target_id = ? ORDER BY version",
                (target_id,),
            )
        )
        if rows:
            instances = _rows(
                conn.execute(
                    "SELECT DISTINCT plans.instance_id FROM correction_versions AS versions "
                    "JOIN correction_plans AS plans ON plans.plan_id = versions.plan_id "
                    "WHERE versions.target_id = ?",
                    (target_id,),
                )
            )
            if len(instances) != 1 or instances[0]["instance_id"] != binding.instance_id:
                raise CorrectionIntegrityError("Current approval instance differs from history")
        history: list[CorrectionHistoryItem] = []
        before = source.fields
        predecessor_id: str | None = None
        predecessor_hash = source.original_projection_hash
        for index, row in enumerate(rows, start=1):
            item = self._verify_version(
                conn, source, row, index, before, predecessor_id, predecessor_hash
            )
            history.append(item)
            before = item.after
            predecessor_id = item.correction_id
            predecessor_hash = item.result_hash
        return EffectiveTransaction(
            target_id=target_id,
            route=source.route,
            version=len(history),
            fields=before,
            history=tuple(history),
            original_source=source,
        )

    def read_plan(self, plan_id: str) -> CorrectionPlan:
        conn = self.connection
        owns_snapshot = not conn.in_transaction
        if owns_snapshot:
            conn.execute("BEGIN")
        try:
            if not verify_correction_schema(conn):
                raise CorrectionIntegrityError("Correction migration 051 is not installed")
            row = _row(conn.execute("SELECT * FROM correction_plans WHERE plan_id = ?", (plan_id,)))
            if row is None:
                raise CorrectionError("Correction plan not found")
            plan = _plan_from_row(row)
            effective = self.lookup_in_snapshot(conn, plan.target_id)
            _assert_binding(self.approval_authority.current_binding(conn, plan.actor), plan)
            if plan.source_hash != effective.original_source.source_hash:
                raise CorrectionIntegrityError("Plan original source binding changed")
            if owns_snapshot:
                conn.commit()
            return plan
        except Exception:
            if owns_snapshot and conn.in_transaction:
                conn.rollback()
            raise

    def preview(self, target_id: str, changes: Mapping[str, str], reason: str) -> CorrectionPlan:
        canonical, canonical_reason = _canonical_changes(changes, reason)
        conn = self.connection
        if conn.in_transaction:
            raise CorrectionConflict("Preview requires an idle connection")
        if not verify_correction_schema(conn):
            raise CorrectionIntegrityError("Correction migration 051 is not installed")
        conn.execute("BEGIN IMMEDIATE")
        try:
            effective = self.lookup_in_snapshot(conn, target_id)
            source = effective.original_source
            after = _after_fields(effective.fields, canonical)
            if after == effective.fields:
                raise CorrectionError("Correction makes no effective change")
            from finance_core.application.correction_relationships import assert_correction_eligible

            assert_correction_eligible(conn, source)
            binding = self.approval_authority.current_binding(conn, source.actor)
            if not isinstance(binding, TrustedApprovalBinding) or binding.actor != source.actor:
                raise CorrectionIntegrityError("Trusted actor binding is invalid")
            created = _positive_epoch(int(time.time()), "plan creation")
            plan_id = "corrplan_" + uuid.uuid4().hex
            correction_id = "corr_" + uuid.uuid4().hex
            authority_id = "corrauth_" + uuid.uuid4().hex
            fact_id = "corrfact_" + uuid.uuid4().hex if source.route == "receipt" else None
            snapshot_id = "corrsnap_" + uuid.uuid4().hex if source.route == "receipt" else None
            fact_hash: str | None = None
            snapshot_hash: str | None = None
            receipt_json: str | None = None
            if source.route == "receipt":
                from finance_core.application.correction_receipts import prepare_receipt_material

                material = prepare_receipt_material(
                    conn,
                    source,
                    after,
                    _required_str(fact_id, "reserved receipt fact ID"),
                    _required_str(snapshot_id, "reserved snapshot ID"),
                    authority_id,
                    plan_id,
                    _required_str(
                        effective.history[-1].snapshot_id
                        if effective.history
                        else source.snapshot_id,
                        "previous snapshot ID",
                    ),
                    _required_str(
                        effective.history[-1].snapshot_hash
                        if effective.history
                        else source.snapshot_hash,
                        "previous snapshot hash",
                    ),
                    _utc(created),
                )
                fact_hash = material.fact_hash
                snapshot_hash = material.snapshot.combined_snapshot_hash
                receipt_json = material.fact_json
            predecessor_id = effective.history[-1].correction_id if effective.history else None
            predecessor_hash = (
                effective.history[-1].result_hash
                if effective.history
                else source.original_projection_hash
            )
            provisional = CorrectionPlan(
                plan_id=plan_id,
                correction_id=correction_id,
                authority_id=authority_id,
                target_id=target_id,
                route=source.route,
                expected_version=effective.version,
                predecessor_id=predecessor_id,
                predecessor_hash=predecessor_hash,
                before=effective.fields,
                before_hash=_fields_hash(effective.fields),
                after=after,
                after_hash=_fields_hash(after),
                source_hash=source.source_hash,
                reason=canonical_reason,
                actor=source.actor,
                realm=binding.realm,
                key_id=binding.key_id,
                instance_id=binding.instance_id,
                fact_id=fact_id,
                fact_hash=fact_hash,
                snapshot_id=snapshot_id,
                snapshot_hash=snapshot_hash,
                receipt_json=receipt_json,
                created_at_epoch=created,
                expires_at_epoch=created + 600,
                plan_hash="",
            )
            plan = replace(
                provisional,
                plan_hash=_hash("finance-correction-plan-v1", _plan_payload(provisional)),
            )
            self._insert_anchor(conn, source, binding, created)
            self._insert_plan(conn, plan)
            _assert_binding(self.approval_authority.current_binding(conn, source.actor), plan)
            conn.commit()
            return plan
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    def _insert_anchor(
        self,
        conn: sqlite3.Connection,
        source: VerifiedOriginalSource,
        binding: TrustedApprovalBinding,
        created: int,
    ) -> None:
        row = _row(
            conn.execute(
                "SELECT * FROM correction_targets WHERE target_id = ?", (source.target_id,)
            )
        )
        if row is not None:
            _verify_anchor(row, source)
            if row["realm"] != binding.realm or row["key_id"] != binding.key_id:
                raise CorrectionConflict("Original target belongs to another authority realm")
            return
        conn.execute(
            """INSERT INTO correction_targets (
                target_id,route,actor,realm,key_id,source_json,source_hash,original_hash,
                projection_json,projection_hash,receipt_id,fact_set_id,original_snapshot_id,
                original_snapshot_hash,created_at_epoch
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source.target_id,
                source.route,
                source.actor,
                binding.realm,
                binding.key_id,
                source.source_json,
                source.source_hash,
                source.original_hash,
                canonical_json_text(source.fields.as_dict()),
                source.original_projection_hash,
                source.receipt_id,
                source.fact_set_id,
                source.snapshot_id,
                source.snapshot_hash,
                created,
            ),
        )

    def _insert_plan(self, conn: sqlite3.Connection, p: CorrectionPlan) -> None:
        conn.execute(
            """INSERT INTO correction_plans (
                plan_id,target_id,correction_id,authority_id,route,expected_version,
                predecessor_id,predecessor_hash,before_json,before_hash,after_json,after_hash,
                source_hash,reason,actor,realm,key_id,instance_id,fact_id,fact_hash,snapshot_id,
                snapshot_hash,receipt_json,created_at_epoch,expires_at_epoch,plan_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.plan_id,
                p.target_id,
                p.correction_id,
                p.authority_id,
                p.route,
                p.expected_version,
                p.predecessor_id,
                p.predecessor_hash,
                canonical_json_text(p.before.as_dict()),
                p.before_hash,
                canonical_json_text(p.after.as_dict()),
                p.after_hash,
                p.source_hash,
                p.reason,
                p.actor,
                p.realm,
                p.key_id,
                p.instance_id,
                p.fact_id,
                p.fact_hash,
                p.snapshot_id,
                p.snapshot_hash,
                p.receipt_json,
                p.created_at_epoch,
                p.expires_at_epoch,
                p.plan_hash,
            ),
        )

    def apply(self, plan_id: str, signed_decision: SignedDecision) -> CorrectionApplyResult:
        if not isinstance(signed_decision, SignedDecision):
            raise CorrectionError("A typed signed decision is required")
        conn = self.connection
        if conn.in_transaction:
            raise CorrectionConflict("Apply requires an idle connection")
        if not verify_correction_schema(conn):
            raise CorrectionIntegrityError("Correction migration 051 is not installed")
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan_row = _row(
                conn.execute("SELECT * FROM correction_plans WHERE plan_id = ?", (plan_id,))
            )
            if plan_row is None:
                raise CorrectionError("Correction plan not found")
            plan = _plan_from_row(plan_row)
            committed = _row(
                conn.execute("SELECT * FROM correction_authorities WHERE plan_id = ?", (plan_id,))
            )
            if committed is not None:
                if (
                    committed["decision_json"] != signed_decision.envelope_json
                    or committed["decision_signature"] != signed_decision.signature
                ):
                    raise CorrectionConflict("Plan already consumed by another decision")
                current = self.lookup_in_snapshot(conn, plan.target_id)
                original = next(
                    (item for item in current.history if item.correction_id == plan.correction_id),
                    None,
                )
                if original is None:
                    raise CorrectionIntegrityError("Consumed plan has no verified correction")
                conn.commit()
                return CorrectionApplyResult(original, current, recovered=True)

            effective = self.lookup_in_snapshot(conn, plan.target_id)
            source = effective.original_source
            if (
                effective.version != plan.expected_version
                or plan.predecessor_id
                != (effective.history[-1].correction_id if effective.history else None)
                or plan.predecessor_hash
                != (
                    effective.history[-1].result_hash
                    if effective.history
                    else source.original_projection_hash
                )
                or plan.before != effective.fields
                or plan.source_hash != source.source_hash
            ):
                raise CorrectionConflict("Correction plan has a stale predecessor or source")
            from finance_core.application.correction_relationships import assert_correction_eligible

            assert_correction_eligible(conn, source)
            _assert_binding(self.approval_authority.current_binding(conn, plan.actor), plan)
            preview_receipt: ReceiptMaterial | None = None
            previous_snapshot_id = (
                effective.history[-1].snapshot_id if effective.history else source.snapshot_id
            )
            previous_snapshot_hash = (
                effective.history[-1].snapshot_hash if effective.history else source.snapshot_hash
            )
            if plan.route == "receipt":
                from finance_core.application.correction_receipts import prepare_receipt_material

                preview_receipt = prepare_receipt_material(
                    conn,
                    source,
                    plan.after,
                    _required_str(plan.fact_id, "reserved receipt fact ID"),
                    _required_str(plan.snapshot_id, "reserved snapshot ID"),
                    plan.authority_id,
                    plan.plan_id,
                    _required_str(previous_snapshot_id, "previous snapshot ID"),
                    _required_str(previous_snapshot_hash, "previous snapshot hash"),
                    _utc(plan.created_at_epoch),
                )
                if (
                    preview_receipt.fact_json != plan.receipt_json
                    or preview_receipt.fact_hash != plan.fact_hash
                    or preview_receipt.snapshot.combined_snapshot_hash != plan.snapshot_hash
                ):
                    raise CorrectionConflict("Receipt calculation or source changed after preview")
            verified = self.approval_authority.verify_fresh(
                signed_decision, ExpectedDecision(plan, plan.expires_at_epoch)
            )
            if (
                not isinstance(verified, VerifiedDecision)
                or verified.correction_id != plan.correction_id
                or not isinstance(verified.envelope, dict)
                or _HASH.fullmatch(verified.decision_digest) is None
            ):
                raise CorrectionIntegrityError(
                    "Approval port did not return typed verified evidence"
                )
            epoch = _positive_epoch(verified.checked_at_epoch, "verified decision")
            envelope = verified.envelope
            issued_at = envelope.get("issued_at_epoch")
            expires_at = envelope.get("expires_at_epoch")
            if (
                envelope.get("plan_id") != plan.plan_id
                or envelope.get("plan_hash") != plan.plan_hash
                or envelope.get("authority_id") != plan.authority_id
                or envelope.get("target_id") != plan.target_id
                or envelope.get("source_hash") != plan.source_hash
                or envelope.get("actor") != plan.actor
                or envelope.get("instance_id") != plan.instance_id
                or type(issued_at) is not int
                or type(expires_at) is not int
                or not isinstance(issued_at, int)
                or not isinstance(expires_at, int)
                or not issued_at <= epoch <= expires_at
                or epoch > plan.expires_at_epoch
            ):
                raise CorrectionIntegrityError(
                    "Decision evidence does not bind the plan or apply time"
                )
            receipt: ReceiptMaterial | None = None
            snapshot_created_at = None
            if plan.route == "receipt":
                receipt = prepare_receipt_material(
                    conn,
                    source,
                    plan.after,
                    _required_str(plan.fact_id, "reserved receipt fact ID"),
                    _required_str(plan.snapshot_id, "reserved snapshot ID"),
                    plan.authority_id,
                    plan.plan_id,
                    _required_str(previous_snapshot_id, "previous snapshot ID"),
                    _required_str(previous_snapshot_hash, "previous snapshot hash"),
                    _utc(epoch),
                )
                if (
                    receipt.fact_json != plan.receipt_json
                    or receipt.fact_hash != plan.fact_hash
                    or receipt.snapshot.combined_snapshot_hash != plan.snapshot_hash
                    or preview_receipt is None
                    or receipt.calculator_output_json != preview_receipt.calculator_output_json
                ):
                    raise CorrectionConflict("Receipt material differs from the signed plan")
                snapshot_created_at = _utc(epoch)
            version = plan.expected_version + 1
            core = _result_core(
                plan,
                version=version,
                decision_digest=verified.decision_digest,
                checked_at_epoch=epoch,
                snapshot_created_at=snapshot_created_at,
            )
            result_hash = _hash("finance-correction-result-core-v1", core)
            consumption = self.approval_authority.seal_consumption(verified, result_hash)
            if not isinstance(consumption, ConsumptionSeal):
                raise CorrectionIntegrityError(
                    "Approval port did not return a typed consumption seal"
                )

            if receipt is not None:
                from finance_core.calculation.authoritative_snapshot import (
                    persist_authoritative_snapshot_in_transaction,
                )

                _, already = persist_authoritative_snapshot_in_transaction(conn, receipt.snapshot)
                if already:
                    raise CorrectionConflict("Receipt correction snapshot already exists")
            self._insert_version(
                conn,
                plan,
                version,
                verified.decision_digest,
                epoch,
                snapshot_created_at,
                core,
                result_hash,
            )
            self._insert_authority(conn, plan, signed_decision, verified, consumption, result_hash)
            if receipt is not None:
                self._insert_receipt_fact(
                    conn,
                    source,
                    plan,
                    receipt,
                    _required_str(previous_snapshot_id, "previous snapshot ID"),
                    _required_str(previous_snapshot_hash, "previous snapshot hash"),
                    _required_str(snapshot_created_at, "snapshot creation time"),
                )
            from finance_core.financial_audit import append_financial_audit_event

            audit, idempotent = append_financial_audit_event(
                conn, _correction_audit_command(source, plan, result_hash, version, epoch)
            )
            if idempotent or audit is None:
                raise CorrectionConflict("Correction audit was already present")
            _assert_binding(self.approval_authority.current_binding(conn, plan.actor), plan)
            conn.commit()
            current = self.lookup(plan.target_id)
            applied = next(
                item for item in current.history if item.correction_id == plan.correction_id
            )
            return CorrectionApplyResult(applied, current, recovered=False)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    def recover(self, plan_id: str) -> CorrectionApplyResult | None:
        """Read-only reply-loss recovery; absence remains an explicit absence."""
        conn = self.connection
        owns_snapshot = not conn.in_transaction
        if owns_snapshot:
            conn.execute("BEGIN")
        try:
            if not verify_correction_schema(conn):
                raise CorrectionIntegrityError("Correction migration 051 is not installed")
            row = _row(conn.execute("SELECT * FROM correction_plans WHERE plan_id = ?", (plan_id,)))
            if row is None:
                raise CorrectionError("Correction plan not found")
            plan = _plan_from_row(row)
            current = self.lookup_in_snapshot(conn, plan.target_id)
            _assert_binding(self.approval_authority.current_binding(conn, plan.actor), plan)
            applied = next((item for item in current.history if item.plan_id == plan_id), None)
            if owns_snapshot:
                conn.commit()
            return (
                None if applied is None else CorrectionApplyResult(applied, current, recovered=True)
            )
        except Exception:
            if owns_snapshot and conn.in_transaction:
                conn.rollback()
            raise

    def _insert_version(
        self,
        conn: sqlite3.Connection,
        p: CorrectionPlan,
        version: int,
        decision_digest: str,
        epoch: int,
        snapshot_created_at: str | None,
        core: dict[str, object],
        result_hash: str,
    ) -> None:
        conn.execute(
            """INSERT INTO correction_versions (
                correction_id,target_id,version,predecessor_version,predecessor_id,
                predecessor_hash,plan_id,authority_id,route,source_hash,before_json,before_hash,
                after_json,after_hash,result_core_json,result_hash,reason,actor,apply_epoch,
                apply_utc,instance_id,fact_id,fact_hash,snapshot_id,snapshot_hash,
                snapshot_created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.correction_id,
                p.target_id,
                version,
                version - 1 or None,
                p.predecessor_id,
                p.predecessor_hash,
                p.plan_id,
                p.authority_id,
                p.route,
                p.source_hash,
                canonical_json_text(p.before.as_dict()),
                p.before_hash,
                canonical_json_text(p.after.as_dict()),
                p.after_hash,
                canonical_json_text(core),
                result_hash,
                p.reason,
                p.actor,
                epoch,
                _utc(epoch),
                p.instance_id,
                p.fact_id,
                p.fact_hash,
                p.snapshot_id,
                p.snapshot_hash,
                snapshot_created_at,
            ),
        )

    def _insert_authority(
        self,
        conn: sqlite3.Connection,
        p: CorrectionPlan,
        signed: SignedDecision,
        verified: VerifiedDecision,
        consumption: ConsumptionSeal,
        result_hash: str,
    ) -> None:
        envelope = verified.envelope
        conn.execute(
            """INSERT INTO correction_authorities (
                authority_id,plan_id,correction_id,target_id,result_hash,nonce,decision_json,
                decision_signature,decision_digest,issued_at_epoch,expires_at_epoch,
                checked_at_epoch,instance_id,consumption_json,consumption_seal
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.authority_id,
                p.plan_id,
                p.correction_id,
                p.target_id,
                result_hash,
                envelope["nonce"],
                signed.envelope_json,
                signed.signature,
                verified.decision_digest,
                envelope["issued_at_epoch"],
                envelope["expires_at_epoch"],
                verified.checked_at_epoch,
                p.instance_id,
                consumption.material_json,
                consumption.seal,
            ),
        )

    def _insert_receipt_fact(
        self,
        conn: sqlite3.Connection,
        source: VerifiedOriginalSource,
        p: CorrectionPlan,
        receipt: ReceiptMaterial,
        previous_id: str,
        previous_hash: str,
        snapshot_created_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO correction_receipt_facts (
                fact_id,correction_id,target_id,route,snapshot_id,fact_hash,fact_json,
                frozen_self_json,original_receipt_id,original_fact_set_id,original_snapshot_id,
                previous_snapshot_id,previous_snapshot_hash,snapshot_hash,snapshot_created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.fact_id,
                p.correction_id,
                p.target_id,
                "receipt",
                p.snapshot_id,
                p.fact_hash,
                receipt.fact_json,
                receipt.frozen_self_json,
                source.receipt_id,
                source.fact_set_id,
                source.snapshot_id,
                previous_id,
                previous_hash,
                p.snapshot_hash,
                snapshot_created_at,
            ),
        )

    def _verify_version(
        self,
        conn: sqlite3.Connection,
        source: VerifiedOriginalSource,
        row: Mapping[str, object],
        version: int,
        before: CorrectionFields,
        predecessor_id: str | None,
        predecessor_hash: str,
    ) -> CorrectionHistoryItem:
        plan_row = _row(
            conn.execute("SELECT * FROM correction_plans WHERE plan_id = ?", (row["plan_id"],))
        )
        if plan_row is None:
            raise CorrectionIntegrityError("Correction version has no plan")
        plan = _plan_from_row(plan_row)
        expected_version = version - 1
        if (
            row["target_id"] != source.target_id
            or row["version"] != version
            or row["predecessor_version"] != (expected_version or None)
            or row["predecessor_id"] != predecessor_id
            or row["predecessor_hash"] != predecessor_hash
            or plan.expected_version != expected_version
            or plan.predecessor_id != predecessor_id
            or plan.predecessor_hash != predecessor_hash
            or plan.target_id != source.target_id
            or plan.route != source.route
            or plan.actor != source.actor
            or plan.source_hash != source.source_hash
            or plan.before != before
        ):
            raise CorrectionIntegrityError("Correction predecessor or source chain has changed")
        if any(
            row[key] != value
            for key, value in {
                "correction_id": plan.correction_id,
                "authority_id": plan.authority_id,
                "route": plan.route,
                "source_hash": plan.source_hash,
                "before_json": canonical_json_text(plan.before.as_dict()),
                "before_hash": plan.before_hash,
                "after_json": canonical_json_text(plan.after.as_dict()),
                "after_hash": plan.after_hash,
                "reason": plan.reason,
                "actor": plan.actor,
                "instance_id": plan.instance_id,
                "fact_id": plan.fact_id,
                "fact_hash": plan.fact_hash,
                "snapshot_id": plan.snapshot_id,
                "snapshot_hash": plan.snapshot_hash,
            }.items()
        ):
            raise CorrectionIntegrityError("Correction version differs from its immutable plan")
        auth = _row(
            conn.execute(
                "SELECT * FROM correction_authorities WHERE authority_id = ?", (plan.authority_id,)
            )
        )
        if auth is None or any(
            auth[key] != value
            for key, value in {
                "plan_id": plan.plan_id,
                "correction_id": plan.correction_id,
                "target_id": plan.target_id,
                "result_hash": row["result_hash"],
                "instance_id": plan.instance_id,
                "checked_at_epoch": row["apply_epoch"],
            }.items()
        ):
            raise CorrectionIntegrityError(
                "Correction consumption authority is absent or mismatched"
            )
        epoch = _positive_epoch(row["apply_epoch"], "correction apply")
        if row["apply_utc"] != _utc(epoch):
            raise CorrectionIntegrityError("Correction UTC timestamp changed")
        signed = SignedDecision(str(auth["decision_json"]), str(auth["decision_signature"]))
        consumption = ConsumptionSeal(str(auth["consumption_json"]), str(auth["consumption_seal"]))
        proof = self.approval_authority.verify_history(
            signed, consumption, ExpectedHistory(plan, str(row["result_hash"]), epoch)
        )
        if (
            not isinstance(proof, VerifiedApprovalHistory)
            or proof.decision_digest != auth["decision_digest"]
            or proof.checked_at_epoch != epoch
        ):
            raise CorrectionIntegrityError("Historical approval proof did not match")
        core = _result_core(
            plan,
            version=version,
            decision_digest=proof.decision_digest,
            checked_at_epoch=epoch,
            snapshot_created_at=None
            if row["snapshot_created_at"] is None
            else str(row["snapshot_created_at"]),
        )
        if (
            row["result_core_json"] != canonical_json_text(core)
            or row["result_hash"] != _hash("finance-correction-result-core-v1", core)
            or auth["result_hash"] != row["result_hash"]
        ):
            raise CorrectionIntegrityError("Correction result-core hash mismatch")
        if source.route == "receipt":
            self._verify_receipt(conn, source, plan, row)
        self._verify_correction_audit(conn, source, plan, str(row["result_hash"]), version, epoch)
        return CorrectionHistoryItem(
            correction_id=plan.correction_id,
            version=version,
            plan_id=plan.plan_id,
            authority_id=plan.authority_id,
            before=plan.before,
            after=plan.after,
            reason=plan.reason,
            actor=plan.actor,
            applied_at_epoch=epoch,
            result_hash=str(row["result_hash"]),
            snapshot_id=plan.snapshot_id,
            snapshot_hash=plan.snapshot_hash,
        )

    def _verify_correction_audit(
        self,
        conn: sqlite3.Connection,
        source: VerifiedOriginalSource,
        plan: CorrectionPlan,
        result_hash: str,
        version: int,
        epoch: int,
    ) -> None:
        from finance_core.financial_audit import (
            FinancialAuditRepository,
            verify_financial_audit_chain,
        )

        command = _correction_audit_command(source, plan, result_hash, version, epoch)
        chain = verify_financial_audit_chain(
            conn, aggregate_type="transaction", aggregate_public_id=plan.target_id
        )
        if not chain.valid:
            raise CorrectionIntegrityError("Transaction financial audit chain is invalid")
        event = FinancialAuditRepository(conn).fetch(command.event_public_id)
        if event is None or any(
            (
                event.event_type != command.event_type,
                event.event_payload_json != canonical_json_text(command.event_payload),
                event.new_state_json != canonical_json_text(command.new_state),
                event.actor_public_id != plan.actor,
                event.authorization_public_id != plan.authority_id,
                event.correlation_public_id != plan.plan_id,
                event.causation_public_id != plan.correction_id,
                event.created_at != _utc(epoch),
                event.calculation_snapshot_public_id != plan.snapshot_id,
                event.calculation_snapshot_hash != plan.snapshot_hash,
                event.source_evidence_references != tuple(sorted(set(source.evidence_refs))),
            )
        ):
            raise CorrectionIntegrityError("Correction financial audit event does not bind result")

    def _verify_receipt(
        self,
        conn: sqlite3.Connection,
        source: VerifiedOriginalSource,
        plan: CorrectionPlan,
        row: Mapping[str, object],
    ) -> None:
        from finance_core.application.correction_receipts import verify_historical_receipt_material
        from finance_core.calculation.authoritative_snapshot import verify_snapshot_binding
        from finance_core.financial_audit import (
            FinancialAuditRepository,
            derive_audit_event_public_id,
            verify_financial_audit_chain,
        )

        fact = _row(
            conn.execute(
                "SELECT * FROM correction_receipt_facts WHERE correction_id = ?",
                (plan.correction_id,),
            )
        )
        if fact is None or any(
            fact[key] != value
            for key, value in {
                "fact_id": plan.fact_id,
                "target_id": plan.target_id,
                "snapshot_id": plan.snapshot_id,
                "fact_hash": plan.fact_hash,
                "fact_json": plan.receipt_json,
                "original_receipt_id": source.receipt_id,
                "original_fact_set_id": source.fact_set_id,
                "original_snapshot_id": source.snapshot_id,
                "snapshot_hash": plan.snapshot_hash,
                "snapshot_created_at": row["snapshot_created_at"],
            }.items()
        ):
            raise CorrectionIntegrityError("Receipt correction fact is absent or changed")
        fact_json = _required_str(plan.receipt_json, "receipt fact payload")
        fact_hash = _required_str(plan.fact_hash, "receipt fact hash")
        snapshot_id = _required_str(plan.snapshot_id, "receipt snapshot ID")
        snapshot_hash = _required_str(plan.snapshot_hash, "receipt snapshot hash")
        if hashlib.sha256(fact_json.encode("utf-8")).hexdigest() != fact_hash:
            # The fact contract hashes the full canonical wrapper, not a summary.
            raise CorrectionIntegrityError("Receipt correction fact hash changed")
        if row["snapshot_created_at"] != _utc(_positive_epoch(row["apply_epoch"], "apply epoch")):
            raise CorrectionIntegrityError("Receipt snapshot timestamp differs from decision time")
        if row["version"] == 1:
            expected_previous_id = _required_str(source.snapshot_id, "original snapshot ID")
            expected_previous_hash = _required_str(source.snapshot_hash, "original snapshot hash")
        else:
            previous = _row(
                conn.execute(
                    "SELECT snapshot_id, snapshot_hash FROM correction_versions "
                    "WHERE correction_id = ?",
                    (row["predecessor_id"],),
                )
            )
            if previous is None:
                raise CorrectionIntegrityError("Previous receipt correction is absent")
            expected_previous_id = _required_str(previous["snapshot_id"], "previous snapshot ID")
            expected_previous_hash = _required_str(
                previous["snapshot_hash"], "previous snapshot hash"
            )
        if (
            fact["previous_snapshot_id"] != expected_previous_id
            or fact["previous_snapshot_hash"] != expected_previous_hash
        ):
            raise CorrectionIntegrityError(
                "Receipt snapshot is not linked to immediate predecessor"
            )
        verify_historical_receipt_material(
            conn,
            source,
            plan.after,
            _required_str(plan.fact_id, "receipt fact ID"),
            snapshot_id,
            plan.authority_id,
            plan.plan_id,
            str(fact["previous_snapshot_id"]),
            str(fact["previous_snapshot_hash"]),
            str(fact["fact_json"]),
            str(fact["fact_hash"]),
            str(fact["snapshot_hash"]),
            str(fact["snapshot_created_at"]),
            str(fact["frozen_self_json"]),
        )
        snapshot = verify_snapshot_binding(
            conn,
            snapshot_public_id=snapshot_id,
            expected_combined_hash=snapshot_hash,
            expected_calculation_type="receipt_split",
            expected_aggregate_public_id=_required_str(source.aggregate_id, "receipt aggregate ID"),
            expected_currency_contract_version=f"currency-{plan.after.currency}-v1",
            expected_authorization_reference=plan.authority_id,
        )
        if snapshot.created_at != row["snapshot_created_at"]:
            raise CorrectionIntegrityError("Receipt snapshot creation time changed")
        audit = verify_financial_audit_chain(
            conn, aggregate_type="calculation_snapshot", aggregate_public_id=snapshot_id
        )
        if not audit.valid or audit.event_count != 1:
            raise CorrectionIntegrityError("Receipt snapshot audit chain is missing or invalid")
        snapshot_event_id = derive_audit_event_public_id(
            aggregate_type="calculation_snapshot",
            aggregate_public_id=snapshot_id,
            event_type="calculation_snapshot_finalized",
            causation_public_id=snapshot_id,
        )
        event = FinancialAuditRepository(conn).fetch(snapshot_event_id)
        expected_payload = {
            "calculation_type": snapshot.calculation_type,
            "calculation_aggregate_public_id": snapshot.aggregate_public_id,
            "input_hash": snapshot.input_hash,
            "output_hash": snapshot.output_hash,
            "rules_hash": snapshot.rules_hash,
            "combined_snapshot_hash": snapshot.combined_snapshot_hash,
            "algorithm_version": snapshot.algorithm_version,
            "money_contract_version": snapshot.money_contract_version,
            "currency_contract_version": snapshot.currency_contract_version,
        }
        if event is None or any(
            (
                event.event_type != "calculation_snapshot_finalized",
                event.event_payload_json != canonical_json_text(expected_payload),
                event.new_state_json
                != canonical_json_text(
                    {
                        "finalization_status": "finalized",
                        "combined_snapshot_hash": snapshot.combined_snapshot_hash,
                    }
                ),
                event.authorization_public_id != plan.authority_id,
                event.calculation_snapshot_public_id != snapshot_id,
                event.calculation_snapshot_hash != snapshot_hash,
                event.source_evidence_references != snapshot.source_references,
                event.created_at != snapshot.created_at,
            )
        ):
            raise CorrectionIntegrityError(
                "Receipt snapshot finalization audit does not bind proof"
            )
