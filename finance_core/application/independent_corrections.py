"""Append-only corrections for independently posted personal expenses.

Every read verifies retained original and complete correction history in one
SQLite snapshot. Every write repeats the proof inside one BEGIN IMMEDIATE.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from finance_core.application.admission import _digest, _positive_int, _reference
from finance_core.application.correction_relationships import assert_correction_eligible
from finance_core.application.correction_schema import verify_correction_schema
from finance_core.application.independent_correction_contract import (
    CORRECTION_DECISION_SCHEMA,
    CORRECTION_REVIEW_SCHEMA,
    ExpectedCorrectionDecision,
    HumanCorrectionDecisionAuthority,
    IndependentCorrectionConflict,
    IndependentCorrectionError,
    IndependentCorrectionIntegrityError,
    IndependentCorrectionPlan,
    IndependentCorrectionResult,
    IndependentEffectiveTransaction,
    VerifiedCorrectionDecision,
    correction_hash,
    has_independent_correction,
    independent_correction_snapshot_inventory,
    require_independent_correction_schema,
)
from finance_core.application.independent_correction_receipts import (
    ReceiptMaterial,
    prepare_receipt_material,
    verify_historical_receipt_material,
)
from finance_core.application.independent_correction_source import (
    IndependentCorrectionFields,
    IndependentOriginalSource,
    verify_original,
)
from finance_core.application.posting import PostingService
from finance_core.application.review import review_snapshot
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
    persist_authoritative_snapshot_in_transaction,
)
from finance_core.financial_audit import (
    AuditEventCommand,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.financial_audit.chain import _normalize_timestamp
from finance_core.money import (
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

_failure_injection_hook: Callable[[str], None] | None = None
_FIELDS = frozenset(vars(IndependentCorrectionFields("", "", "", None, None, None)))
_EVENT = "independent_transaction_correction_applied"


def _inject(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _object(raw: str) -> dict[str, Any]:
    value = canonical_json_value(raw, label="independent correction evidence")
    if type(value) is not dict or canonical_json_text(value) != raw:
        raise IndependentCorrectionIntegrityError("Correction evidence is not canonical")
    return value


def _text(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str:
        raise IndependentCorrectionError(f"{label} must be text")
    value = unicodedata.normalize("NFC", value.strip())
    if (
        not value
        or len(value) > 1024
        or any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029" for c in value)
    ):
        raise IndependentCorrectionError(f"{label} is empty, oversized or unsafe")
    return value


def _fields(values: Mapping[str, object]) -> IndependentCorrectionFields:
    if set(values) != _FIELDS:
        raise IndependentCorrectionError("Correction requires exactly six fields")
    raw_currency = values["currency"]
    if type(raw_currency) is not str:
        raise IndependentCorrectionError("Currency must be text")
    currency = normalize_currency(raw_currency)
    raw = values["amount"]
    if type(raw) is not str or len(raw) > 1024 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw) is None:
        raise IndependentCorrectionError("Amount must have bounded positive decimal syntax")
    amount = validate_amount_for_currency(money_decimal(raw, label="correction amount"), currency)
    if amount <= 0:
        raise IndependentCorrectionError("Correction amount must be positive")
    raw_date = values["transaction_date"]
    if type(raw_date) is not str or date.fromisoformat(raw_date).isoformat() != raw_date:
        raise IndependentCorrectionError("Transaction date must be a full calendar date")
    retained_text: dict[str, str | None] = {}
    for name in ("merchant", "description", "category"):
        value = values[name]
        if value is not None and (type(value) is not str or len(value) > 1024):
            raise IndependentCorrectionError(f"{name} is not bounded exact text")
        retained_text[name] = value
    return IndependentCorrectionFields(
        canonical_money_str(amount, currency),
        currency,
        raw_date,
        retained_text["merchant"],
        retained_text["description"],
        retained_text["category"],
    )


class IndependentCorrectionService:
    def __init__(
        self,
        posting_service: PostingService,
        *,
        human_decision_authority: HumanCorrectionDecisionAuthority | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(posting_service, PostingService):
            raise IndependentCorrectionError("Correction requires a fixed PostingService owner")
        self._posting = posting_service
        self._conn = posting_service._conn
        self._binding = posting_service._binding
        self._authority = human_decision_authority
        self._clock = clock or posting_service._clock

    def _now(self) -> int:
        value = self._clock()
        if not _positive_int(value) or value > 253402300799:
            raise IndependentCorrectionError("Correction clock is invalid")
        return value

    def _begin(self) -> None:
        require_staging_database(self._conn)
        require_foreign_keys_enabled(self._conn)
        if self._conn.in_transaction:
            raise IndependentCorrectionError("Correction cannot own caller's existing transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            require_independent_correction_schema(self._conn)
        except BaseException:
            self._conn.rollback()
            raise

    def _original(self, source: IndependentOriginalSource) -> IndependentEffectiveTransaction:
        return IndependentEffectiveTransaction(
            source.target_id,
            0,
            source.fields,
            None,
            source.original_hash,
            source.snapshot_id,
            source.snapshot_hash,
            (),
            source,
        )

    def _read_original(self, transaction_id: str) -> IndependentEffectiveTransaction:
        require_staging_database(self._conn)
        with review_snapshot(self._conn):
            return self._original(verify_original(self._posting, transaction_id))

    def _plan(self, plan_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM application_correction_plans WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise IndependentCorrectionError("Correction plan does not exist")
        p = _object(row["material_json"])
        if (
            correction_hash(p) != row["plan_hash"]
            or any(
                p[key] != row[key]
                for key in (
                    "plan_id",
                    "target_id",
                    "correction_id",
                    "expected_version",
                    "predecessor_id",
                    "predecessor_hash",
                    "created_at",
                    "expires_at",
                )
            )
            or p["schema"] != CORRECTION_REVIEW_SCHEMA
            or p["binding"] != vars(self._binding)
        ):
            raise IndependentCorrectionIntegrityError("Correction plan binding changed")
        before, after = _fields(p["before"]), _fields(p["after"])
        if before.as_dict() != p["before"] or after.as_dict() != p["after"] or before == after:
            raise IndependentCorrectionIntegrityError("Correction plan fields changed")
        if _text(p["reason"], "reason") != p["reason"]:
            raise IndependentCorrectionIntegrityError("Correction reason changed")
        return row, p

    def _decision(
        self,
        p: dict[str, Any],
        plan_hash: str,
        decision_id: str,
        checked_at: int,
        source: IndependentOriginalSource,
        accepted: str | None = None,
    ) -> VerifiedCorrectionDecision:
        if self._authority is None:
            raise IndependentCorrectionError("Correction human decision authority is unavailable")
        expected = ExpectedCorrectionDecision(
            self._binding,
            source.source,
            p["plan_id"],
            plan_hash,
            p,
            checked_at,
            accepted,
        )
        decision = self._authority.verify_persisted(self._conn, decision_id, expected)
        if type(decision) is not VerifiedCorrectionDecision or any(
            (
                decision.schema != CORRECTION_DECISION_SCHEMA,
                decision.namespace != self._binding.decision_namespace,
                decision.key_id != self._binding.decision_key_id,
                decision.instance_id != self._binding.instance_id,
                decision.human_principal_id != self._binding.human_principal_id,
                decision.decision_id != decision_id,
                decision.plan_id != p["plan_id"],
                decision.plan_hash != plan_hash,
                decision.source_evidence_id != source.source.evidence_id,
                decision.source_evidence_digest != source.source.evidence_digest,
                not _reference(decision.display_id),
                not _digest(decision.display_evidence_digest),
                not _reference(decision.reply_id),
                not _digest(decision.reply_evidence_digest),
                decision.action != "confirm",
                not _positive_int(decision.issued_at),
                not _positive_int(decision.expires_at),
                type(decision.consumed) is not bool,
                decision.consumed,
                not _digest(decision.decision_digest),
            )
        ):
            raise IndependentCorrectionIntegrityError(
                "Human correction evidence does not match plan"
            )
        if (
            not p["created_at"]
            <= decision.issued_at
            <= checked_at
            < min(decision.expires_at, p["expires_at"])
        ):
            raise IndependentCorrectionError("Correction approval is expired or predates plan")
        return decision

    def _receipt(
        self,
        source: IndependentOriginalSource,
        p: dict[str, Any],
        current: IndependentEffectiveTransaction,
        created: str,
        historical: dict[str, Any] | None = None,
    ) -> ReceiptMaterial | None:
        if source.route == "text":
            if p["receipt"] is not None:
                raise IndependentCorrectionIntegrityError("Text plan has receipt evidence")
            return None
        r = p["receipt"]
        if current.snapshot_id is None or current.snapshot_hash is None:
            raise IndependentCorrectionIntegrityError("Receipt predecessor snapshot is missing")
        arguments = (
            self._conn,
            source,
            _fields(p["after"]),
            r["fact_id"],
            r["snapshot_id"],
            p["correction_id"],
            p["plan_id"],
            current.snapshot_id,
            current.snapshot_hash,
        )
        if historical is not None:
            material = verify_historical_receipt_material(
                *arguments,
                historical["fact_json"],
                historical["fact_hash"],
                historical["snapshot_hash"],
                created,
                historical["frozen_self_json"],
            )
        else:
            material = prepare_receipt_material(*arguments, created)
        if (
            material.fact_hash != r["fact_hash"]
            or (material.snapshot.combined_snapshot_hash != r["snapshot_hash"])
            or material.fact_json != r["fact_json"]
            or material.frozen_self_json != r["frozen_self_json"]
            or material.calculator_output_json != r["calculator_output_json"]
            or r["previous_snapshot_id"] != current.snapshot_id
            or r["previous_snapshot_hash"] != current.snapshot_hash
        ):
            raise IndependentCorrectionIntegrityError(
                "Receipt differs from exact approved projection"
            )
        return material

    def _audit_command(
        self,
        source: IndependentOriginalSource,
        p: dict[str, Any],
        result: dict[str, Any],
        digest: str,
    ) -> AuditEventCommand:
        return AuditEventCommand(
            event_public_id=derive_audit_event_public_id(
                aggregate_type="transaction",
                aggregate_public_id=source.target_id,
                event_type=_EVENT,
                causation_public_id=p["correction_id"],
            ),
            aggregate_type="transaction",
            aggregate_public_id=source.target_id,
            event_type=_EVENT,
            event_payload={
                "schema": "independent-correction-audit-v1",
                "result": result,
                "result_hash": digest,
            },
            new_state={
                "schema": "independent-correction-state-v1",
                "target_id": source.target_id,
                "version": result["version"],
                "fields": p["after"],
                "result_hash": digest,
            },
            actor_type="human",
            actor_public_id=source.actor,
            correlation_public_id=p["plan_id"],
            causation_public_id=p["correction_id"],
            created_at=_utc(result["accepted_at"]),
            authorization_public_id=result["decision_id"],
            calculation_snapshot_public_id=result["snapshot_id"],
            calculation_snapshot_hash=result["snapshot_hash"],
            source_evidence_references=source.evidence_refs,
        )

    def _verify_audit(self, command: AuditEventCommand) -> None:
        event = FinancialAuditRepository(self._conn).fetch(command.event_public_id)
        if event is None or any(
            (
                event.aggregate_type != command.aggregate_type,
                event.aggregate_public_id != command.aggregate_public_id,
                event.event_type != command.event_type,
                event.event_payload_json != canonical_json_text(command.event_payload),
                event.new_state_json != canonical_json_text(command.new_state),
                event.actor_type != command.actor_type,
                event.actor_public_id != command.actor_public_id,
                event.correlation_public_id != command.correlation_public_id,
                event.causation_public_id != command.causation_public_id,
                event.authorization_public_id != command.authorization_public_id,
                event.calculation_snapshot_public_id != command.calculation_snapshot_public_id,
                event.calculation_snapshot_hash != command.calculation_snapshot_hash,
                event.source_evidence_references
                != tuple(sorted(command.source_evidence_references)),
                event.created_at != _normalize_timestamp(command.created_at),
            )
        ):
            raise IndependentCorrectionIntegrityError("Correction financial audit binding changed")

    def _lookup(
        self, transaction_id: str, *, _verified_source: IndependentOriginalSource | None = None
    ) -> tuple[IndependentEffectiveTransaction, dict[str, IndependentEffectiveTransaction]]:
        source = _verified_source or verify_original(self._posting, transaction_id)
        current = self._original(source)
        legacy_schema = verify_correction_schema(self._conn)
        legacy_versions = (
            legacy_schema
            and self._conn.execute(
                "SELECT 1 FROM correction_versions WHERE target_id=? LIMIT 1",
                (transaction_id,),
            ).fetchone()
            is not None
        )
        legacy_audit = (
            self._conn.execute(
                "SELECT 1 FROM financial_audit_events WHERE aggregate_type='transaction' "
                "AND aggregate_public_id=? AND event_type='transaction_correction_applied' LIMIT 1",
                (transaction_id,),
            ).fetchone()
            is not None
        )
        if legacy_versions or legacy_audit:
            raise IndependentCorrectionIntegrityError(
                "Independent target has legacy correction evidence"
            )
        has_independent_correction(self._conn, transaction_id)
        if (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='application_correction_targets'"
            ).fetchone()
            is None
        ):
            return current, {}
        require_independent_correction_schema(self._conn)
        anchor = self._conn.execute(
            "SELECT * FROM application_correction_targets WHERE target_id=?",
            (transaction_id,),
        ).fetchone()
        if anchor is not None and any(
            (
                anchor["attempt_id"] != source.attempt_id,
                anchor["source_json"] != source.source_json,
                anchor["source_hash"] != source.source_hash,
                anchor["original_hash"] != source.original_hash,
            )
        ):
            raise IndependentCorrectionIntegrityError("Original correction anchor changed")
        versions = self._conn.execute(
            "SELECT * FROM application_correction_versions WHERE target_id=? ORDER BY version",
            (transaction_id,),
        ).fetchall()
        applied: dict[str, IndependentEffectiveTransaction] = {}
        events: set[str] = set()
        facts: set[str] = set()
        snapshots: set[str] = set()
        decisions: set[str] = set()
        for v in versions:
            if anchor is None:
                raise IndependentCorrectionIntegrityError(
                    "Correction version lacks original anchor"
                )
            row, p = self._plan(v["plan_id"])
            if any(
                (
                    p["target_id"] != transaction_id,
                    p["attempt_id"] != source.attempt_id,
                    p["route"] != source.route,
                    p["source_hash"] != source.source_hash,
                    p["original_hash"] != source.original_hash,
                    p["source"] != vars(source.source),
                    p["before"] != current.fields.as_dict(),
                    p["expected_version"] != current.version,
                    p["predecessor_id"] != current.predecessor_id,
                    p["predecessor_hash"] != current.predecessor_hash,
                    v["version"] != current.version + 1,
                    v["correction_id"] != p["correction_id"],
                    v["predecessor_id"] != current.predecessor_id,
                    v["predecessor_hash"] != current.predecessor_hash,
                )
            ):
                raise IndependentCorrectionIntegrityError("Correction history is not contiguous")
            d = self._conn.execute(
                "SELECT * FROM application_correction_decisions WHERE correction_id=?",
                (v["correction_id"],),
            ).fetchone()
            if d is None or d["plan_id"] != p["plan_id"] or d["target_id"] != transaction_id:
                raise IndependentCorrectionIntegrityError(
                    "Correction decision consumption is missing"
                )
            checked = d["accepted_at"]
            decision = self._decision(
                p, row["plan_hash"], d["decision_id"], checked, source, v["correction_id"]
            )
            if vars(decision) != _object(d["material_json"]) or (
                d["decision_digest"] != decision.decision_digest
                or d["decision_namespace"] != decision.namespace
            ):
                raise IndependentCorrectionIntegrityError("Consumed correction decision changed")
            receipt_record = None
            if source.route == "receipt":
                fact = self._conn.execute(
                    "SELECT * FROM application_correction_receipt_facts WHERE correction_id=?",
                    (v["correction_id"],),
                ).fetchone()
                if (
                    fact is None
                    or fact["target_id"] != transaction_id
                    or (
                        fact["fact_id"] != p["receipt"]["fact_id"]
                        or fact["snapshot_id"] != p["receipt"]["snapshot_id"]
                    )
                ):
                    raise IndependentCorrectionIntegrityError(
                        "Correction receipt fact identity changed"
                    )
                receipt_record = _object(fact["material_json"])
                if receipt_record != p["receipt"]:
                    raise IndependentCorrectionIntegrityError("Receipt full material changed")
                facts.add(fact["fact_id"])
                snapshots.add(fact["snapshot_id"])
            material = self._receipt(source, p, current, _utc(checked), receipt_record)
            result = self._result(p, row["plan_hash"], decision, checked, material)
            digest = correction_hash(result)
            if _object(v["result_json"]) != result or digest != v["result_hash"]:
                raise IndependentCorrectionIntegrityError("Committed correction result changed")
            command = self._audit_command(source, p, result, digest)
            self._verify_audit(command)
            events.add(command.event_public_id)
            decisions.add(v["correction_id"])
            if material is not None:
                self._verify_snapshot_audit(material)
            current = IndependentEffectiveTransaction(
                transaction_id,
                v["version"],
                _fields(p["after"]),
                v["correction_id"],
                digest,
                result["snapshot_id"],
                result["snapshot_hash"],
                (*current.history, result),
                source,
            )
            applied[p["plan_id"]] = current
        inventories = (
            (
                events,
                "SELECT event_public_id FROM financial_audit_events "
                "WHERE aggregate_type='transaction' AND aggregate_public_id=? AND event_type='"
                + _EVENT
                + "'",
            ),
            (
                decisions,
                "SELECT correction_id FROM application_correction_decisions WHERE target_id=?",
            ),
            (facts, "SELECT fact_id FROM application_correction_receipt_facts WHERE target_id=?"),
        )
        for expected, query in inventories:
            if {r[0] for r in self._conn.execute(query, (transaction_id,))} != expected:
                raise IndependentCorrectionIntegrityError(
                    "Correction inventory contains missing or orphan evidence"
                )
        observed_snapshots, snapshot_audit_inventory = independent_correction_snapshot_inventory(
            self._conn, transaction_id
        )
        if observed_snapshots != snapshots or snapshot_audit_inventory != snapshots:
            raise IndependentCorrectionIntegrityError("Correction snapshot inventory changed")
        chain = verify_financial_audit_chain(
            self._conn, aggregate_type="transaction", aggregate_public_id=transaction_id
        )
        if versions and (not chain.valid or chain.legacy_without_chain):
            raise IndependentCorrectionIntegrityError("Correction audit chain is invalid")
        return current, applied

    def _verify_snapshot_audit(self, material: ReceiptMaterial) -> None:
        snapshot = material.snapshot
        chain = verify_financial_audit_chain(
            self._conn,
            aggregate_type="calculation_snapshot",
            aggregate_public_id=snapshot.snapshot_public_id,
        )
        if not chain.valid or chain.event_count != 1:
            raise IndependentCorrectionIntegrityError(
                "Correction snapshot audit is missing or corrupt"
            )
        event = FinancialAuditRepository(self._conn).fetch(
            derive_audit_event_public_id(
                aggregate_type="calculation_snapshot",
                aggregate_public_id=snapshot.snapshot_public_id,
                event_type="calculation_snapshot_finalized",
                causation_public_id=snapshot.snapshot_public_id,
            )
        )
        if event is None or any(
            (
                event.event_type != "calculation_snapshot_finalized",
                event.causation_public_id != snapshot.snapshot_public_id,
                event.actor_type != snapshot.actor_type,
                event.event_payload_json
                != canonical_json_text(
                    {
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
                ),
                event.previous_state_json != canonical_json_text({"finalization_status": "draft"}),
                event.new_state_json
                != canonical_json_text(
                    {
                        "finalization_status": "finalized",
                        "combined_snapshot_hash": snapshot.combined_snapshot_hash,
                    }
                ),
                event.authorization_public_id != snapshot.authorization_reference,
                event.calculation_snapshot_hash != snapshot.combined_snapshot_hash,
                event.calculation_snapshot_public_id != snapshot.snapshot_public_id,
                event.actor_public_id != snapshot.actor_public_id,
                event.source_evidence_references != snapshot.source_references,
                event.correlation_public_id != snapshot.aggregate_public_id,
                event.created_at != _normalize_timestamp(snapshot.created_at),
            )
        ):
            raise IndependentCorrectionIntegrityError("Correction snapshot audit binding changed")

    def _result(self, p, plan_hash, decision, checked, receipt):
        return {
            "schema": "independent-correction-result-v1",
            "target_id": p["target_id"],
            "correction_id": p["correction_id"],
            "plan_id": p["plan_id"],
            "plan_hash": plan_hash,
            "version": p["expected_version"] + 1,
            "predecessor_id": p["predecessor_id"],
            "predecessor_hash": p["predecessor_hash"],
            "source_hash": p["source_hash"],
            "before": p["before"],
            "after": p["after"],
            "reason": p["reason"],
            "binding": p["binding"],
            "decision_id": decision.decision_id,
            "decision_digest": decision.decision_digest,
            "accepted_at": checked,
            "fact_id": None if receipt is None else p["receipt"]["fact_id"],
            "fact_hash": None if receipt is None else receipt.fact_hash,
            "snapshot_id": None if receipt is None else receipt.snapshot.snapshot_public_id,
            "snapshot_hash": None if receipt is None else receipt.snapshot.combined_snapshot_hash,
        }

    def _read_views_in_snapshot(
        self, transaction_id: str
    ) -> tuple[IndependentEffectiveTransaction, IndependentEffectiveTransaction]:
        if not self._conn.in_transaction:
            raise IndependentCorrectionError("Effective views require the owning snapshot")
        source = verify_original(self._posting, transaction_id)
        return self._original(source), self._lookup(transaction_id, _verified_source=source)[0]

    def read_effective(self, transaction_id: str) -> IndependentEffectiveTransaction:
        if not _reference(transaction_id):
            raise IndependentCorrectionError("Transaction reference is invalid")
        require_staging_database(self._conn)
        with review_snapshot(self._conn):
            return self._lookup(transaction_id)[0]

    def read_effective_in_snapshot(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
    ) -> IndependentEffectiveTransaction:
        """Read through a neutral consumer's existing, identity-bound snapshot.

        The caller retains transaction ownership. A foreign connection is
        refused even when it points to the same file or repeats the public ID.
        """
        if connection is not self._conn or not connection.in_transaction:
            raise IndependentCorrectionError("Effective reader requires its owning snapshot")
        if not _reference(transaction_id):
            raise IndependentCorrectionError("Transaction reference is invalid")
        require_staging_database(connection)
        return self._lookup(transaction_id)[0]

    def prepare_correction(
        self,
        transaction_id: str,
        patch: Mapping[str, object],
        reason: str,
    ) -> IndependentCorrectionPlan:
        if self._authority is None:
            raise IndependentCorrectionError(
                "Correction human authority is required for preparation"
            )
        if (
            not _reference(transaction_id)
            or not isinstance(patch, Mapping)
            or not patch
            or set(patch) - _FIELDS
        ):
            raise IndependentCorrectionError(
                "Correction patch is empty or contains unsupported fields"
            )
        canonical_reason = _text(reason, "reason")
        self._begin()
        try:
            current, _ = self._lookup(transaction_id)
            source = current.original_source
            assert_correction_eligible(self._conn, source)
            if "currency" in patch and type(patch["currency"]) is not str:
                raise IndependentCorrectionError("Currency must be text")
            if (
                "currency" in patch
                and normalize_currency(str(patch["currency"])) != current.fields.currency
                and "amount" not in patch
            ):
                raise IndependentCorrectionError("Currency change requires explicit amount")
            normalized_patch = dict(patch)
            for field in ("merchant", "description", "category"):
                if field in normalized_patch:
                    normalized_patch[field] = _text(normalized_patch[field], field, optional=True)
            after = _fields({**current.fields.as_dict(), **normalized_patch})
            if after == current.fields:
                raise IndependentCorrectionError("Correction patch has no effective change")
            if source.route == "receipt" and not after.merchant:
                raise IndependentCorrectionError("Personal receipt requires a merchant")
            if source.route == "text" and not after.merchant and not after.description:
                raise IndependentCorrectionError(
                    "Personal expense requires merchant or description"
                )
            now = self._now()
            identity = uuid.uuid4().hex
            p: dict[str, Any] = {
                "schema": CORRECTION_REVIEW_SCHEMA,
                "plan_id": "icplan_" + identity,
                "correction_id": "ic_" + identity,
                "target_id": transaction_id,
                "attempt_id": source.attempt_id,
                "route": source.route,
                "expected_version": current.version,
                "predecessor_id": current.predecessor_id,
                "predecessor_hash": current.predecessor_hash,
                "before": current.fields.as_dict(),
                "after": after.as_dict(),
                "reason": canonical_reason,
                "binding": vars(self._binding),
                "source": vars(source.source),
                "source_hash": source.source_hash,
                "original_hash": source.original_hash,
                "created_at": now,
                "expires_at": now + 600,
                "receipt": None,
            }
            if source.route == "receipt":
                if current.snapshot_id is None or current.snapshot_hash is None:
                    raise IndependentCorrectionIntegrityError(
                        "Receipt predecessor snapshot missing"
                    )
                material = prepare_receipt_material(
                    self._conn,
                    source,
                    after,
                    "icfact_" + identity,
                    "icsnap_" + identity,
                    p["correction_id"],
                    p["plan_id"],
                    current.snapshot_id,
                    current.snapshot_hash,
                    _utc(now),
                )
                p["receipt"] = {
                    "fact_id": "icfact_" + identity,
                    "snapshot_id": "icsnap_" + identity,
                    "fact_json": material.fact_json,
                    "fact_hash": material.fact_hash,
                    "snapshot_hash": material.snapshot.combined_snapshot_hash,
                    "frozen_self_json": material.frozen_self_json,
                    "calculator_output_json": material.calculator_output_json,
                    "previous_snapshot_id": current.snapshot_id,
                    "previous_snapshot_hash": current.snapshot_hash,
                }
            if (
                self._conn.execute(
                    "SELECT 1 FROM application_correction_targets WHERE target_id=?",
                    (transaction_id,),
                ).fetchone()
                is None
            ):
                self._conn.execute(
                    "INSERT INTO application_correction_targets VALUES(?,?,?,?,?)",
                    (
                        transaction_id,
                        source.attempt_id,
                        source.source_json,
                        source.source_hash,
                        source.original_hash,
                    ),
                )
            digest = correction_hash(p)
            self._conn.execute(
                "INSERT INTO application_correction_plans VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    p["plan_id"],
                    transaction_id,
                    p["correction_id"],
                    current.version,
                    current.predecessor_id,
                    current.predecessor_hash,
                    canonical_json_text(p),
                    digest,
                    now,
                    now + 600,
                ),
            )
            self._conn.commit()
            return IndependentCorrectionPlan(p["plan_id"], digest, p, now + 600)
        except BaseException:
            self._conn.rollback()
            raise

    def submit_correction(
        self, plan_id: str, decision_record_id: str
    ) -> IndependentCorrectionResult:
        if not _reference(plan_id) or not _reference(decision_record_id):
            raise IndependentCorrectionError("Plan or decision reference is invalid")
        self._begin()
        try:
            row, p = self._plan(plan_id)
            current, applied = self._lookup(p["target_id"])
            if plan_id in applied:
                recorded = self._conn.execute(
                    "SELECT decision_id FROM application_correction_decisions WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()
                if recorded[0] != decision_record_id:
                    raise IndependentCorrectionConflict("Plan consumed a different decision")
                self._conn.rollback()
                return IndependentCorrectionResult(applied[plan_id], current, True)
            if (
                current.version != p["expected_version"]
                or current.predecessor_hash != p["predecessor_hash"]
                or current.fields.as_dict() != p["before"]
            ):
                raise IndependentCorrectionConflict("Correction predecessor is no longer current")
            source = current.original_source
            if p["source_hash"] != source.source_hash or p["original_hash"] != source.original_hash:
                raise IndependentCorrectionIntegrityError("Correction original source changed")
            assert_correction_eligible(self._conn, source)
            checked = self._now()
            decision = self._decision(p, row["plan_hash"], decision_record_id, checked, source)
            material = self._receipt(source, p, current, _utc(checked))
            result = self._result(p, row["plan_hash"], decision, checked, material)
            digest = correction_hash(result)
            self._conn.execute(
                "INSERT INTO application_correction_decisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    p["correction_id"],
                    plan_id,
                    p["target_id"],
                    decision.namespace,
                    decision_record_id,
                    decision.decision_digest,
                    canonical_json_text(vars(decision)),
                    checked,
                ),
            )
            _inject("after_decision")
            if material is not None:
                persist_authoritative_snapshot_in_transaction(self._conn, material.snapshot)
            _inject("after_snapshot")
            self._conn.execute(
                "INSERT INTO application_correction_versions VALUES(?,?,?,?,?,?,?,?)",
                (
                    p["correction_id"],
                    p["target_id"],
                    current.version + 1,
                    p["predecessor_id"],
                    p["predecessor_hash"],
                    plan_id,
                    canonical_json_text(result),
                    digest,
                ),
            )
            _inject("after_version")
            if material is not None:
                self._conn.execute(
                    "INSERT INTO application_correction_receipt_facts VALUES(?,?,?,?,?)",
                    (
                        p["receipt"]["fact_id"],
                        p["correction_id"],
                        p["target_id"],
                        material.snapshot.snapshot_public_id,
                        canonical_json_text(p["receipt"]),
                    ),
                )
            _inject("after_receipt_fact")
            append_financial_audit_event(self._conn, self._audit_command(source, p, result, digest))
            _inject("after_audit")
            # Verify inventory and historical decision semantics before the only commit.
            final, committed = self._lookup(p["target_id"])
            _inject("before_commit")
            self._conn.commit()
            _inject("after_commit")
            return IndependentCorrectionResult(committed[plan_id], final, False)
        except BaseException:
            self._conn.rollback()
            raise

    def recover_correction(self, plan_id: str) -> IndependentCorrectionResult | None:
        if not _reference(plan_id):
            raise IndependentCorrectionError("Plan reference is invalid")
        require_staging_database(self._conn)
        with review_snapshot(self._conn):
            require_independent_correction_schema(self._conn)
            _, p = self._plan(plan_id)
            current, applied = self._lookup(p["target_id"])
            if plan_id not in applied:
                return None
            return IndependentCorrectionResult(applied[plan_id], current, True)

    submit = submit_correction
    recover = recover_correction
