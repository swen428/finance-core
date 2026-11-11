"""Durable exact preparation for offline synthetic native expense finalization.

A prepared reference and its plaintext closure confer no online write authority,
remote durability or encryption. Only guarded local application creates facts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from finance_core.instance_admission import ProductionOwner
from finance_core.instance_admission.sessions import (
    _prepared_finalization_operation,
    lifecycle_read_operation,
)
from finance_core.profile_paths import _migration_contract_digest
from finance_core.resources import MIGRATION_LEDGER_DIGEST

from ._finalization_journal_v1 import (
    _fail,
    _verify_journal,
)
from ._finalization_plan_v1 import (
    _append_finalization,
    _compile_finalization,
    _confirmed_receipt,
)
from ._recovery_rows_v1 import COLUMNS, ClosureLimitsV1, limits_v1, sha, text
from .contracts import CommitReceipt, RuntimeBinding, canonical, digest
from .reader import audit_evidence_connection
from .recovery_material_v1 import (
    RecoveryEvidenceArchiveSourceV1,
    RecoveryEvidenceAuthenticatorV1,
)
from .recovery_material_v2 import (
    ClosureFrontierV2,
    _build_prepared_native_closure_v2,
    _prepared_before_v2,
    check_native_closure_v2,
    decode_native_closure_v2,
)
from .service import LedgerService


@dataclass(frozen=True)
class PreparedFinalizationV1:
    operation_id: str
    intent_sha256: str
    review_id: str
    action_evidence_id: str
    record_id: str
    economic_event_id: str
    version: int
    fact_sha256: str
    commit_seq: int
    format: str = "prepared-native-finalization-ref-v1"


@dataclass(frozen=True)
class FinalizationStateV1:
    operation_id: str
    state: str
    prepared: PreparedFinalizationV1 | None
    receipt: CommitReceipt | None
    format: str = "prepared-native-finalization-state-v1"


def _reference(entry):
    body, fact = entry.body, entry.body["append"]["fact"]
    return PreparedFinalizationV1(
        body["operation_id"],
        entry.intent_sha256,
        body["review_id"],
        body["action_evidence_id"],
        fact["record_id"],
        fact["economic_event_id"],
        fact["version"],
        fact["fact_sha256"],
        fact["commit_seq"],
    )


def _state(operation_id, entry):
    if entry is None:
        return FinalizationStateV1(operation_id, "ABSENT_LOCAL", None, None)
    return FinalizationStateV1(
        operation_id, entry.outcome or "PREPARED_OFFLINE", _reference(entry), entry.receipt
    )


def _commit_local(conn, owner):
    """A failed COMMIT may already be durable; never release an unknown result."""
    try:
        conn.commit()
    except BaseException:
        try:
            still_open = conn.in_transaction
        except BaseException:
            still_open = False
        if not still_open:
            owner._uncertain = True
        raise


class PreparedFinalizationServiceV1:
    def __init__(
        self,
        service: LedgerService,
        *,
        archive_source: RecoveryEvidenceArchiveSourceV1,
        authenticator: RecoveryEvidenceAuthenticatorV1,
        limits: ClosureLimitsV1 | None = None,
    ):
        if (
            not isinstance(service, LedgerService)
            or not isinstance(service.owner, ProductionOwner)
            or type(service.binding) is not RuntimeBinding
            or service.owner.config.instance_id != service.binding.instance_id
        ):
            _fail("invalid_preparation", "Actual matching LedgerService and owner required")
        service.owner.assert_valid()
        if not callable(getattr(archive_source, "export", None)):
            _fail("missing_evidence", "Pre-stored archive source required")
        if not callable(getattr(authenticator, "verify", None)):
            _fail("evidence_authentication_failed", "Independent authenticator required")
        self.service, self.archive_source = service, archive_source
        self.authenticator, self.limits = authenticator, limits_v1(limits)

    def _identity(self, value):
        text(value, self.limits, identity=True)
        return value

    def _binding(self, entry):
        body = entry.body
        if (
            body["binding"] != asdict(self.service.binding)
            or body["core_identity"] != self.service.owner.config.core_identity
            or body["migration_identity"] != _migration_contract_digest()
            or body["migration_ledger_digest"] != MIGRATION_LEDGER_DIGEST
        ):
            _fail("binding_mismatch", "Intent belongs to another runtime or installed identity")

    def _checked(self, journal, operation_id, expected_hash=None):
        entry = journal.get(operation_id)
        if entry is None:
            _fail("absent_local", "No local preparation exists for this exact operation")
        if expected_hash is not None:
            sha(expected_hash)
            if entry.intent_sha256 != expected_hash:
                _fail("intent_mismatch", "Expected intent differs from durable local preparation")
        tail = decode_native_closure_v2(entry.packages[-1], limits=self.limits)
        checked = check_native_closure_v2(
            entry.packages,
            authenticator=self.authenticator,
            expected_identity=tail.identity,
            expected_frontier=ClosureFrontierV2(**entry.body["projected_frontier"]),
            limits=self.limits,
        )
        if canonical(asdict(checked.inventory)) != canonical(entry.body["projected_inventory"]):
            _fail("journal_mismatch", "Independent reconstruction differs from prepared inventory")
        return entry

    def inspect(self, operation_id: str) -> FinalizationStateV1:
        operation_id = self._identity(operation_id)
        with lifecycle_read_operation(self.service.owner) as conn:
            conn.execute("BEGIN")
            try:
                journal = _verify_journal(conn, limits=self.limits)
                entry = journal.get(operation_id)
                if entry is not None:
                    self._checked(journal, operation_id)
                result = _state(operation_id, entry)
            finally:
                conn.rollback()
        return result

    def material(self, operation_id: str, *, expected_intent_sha256: str) -> tuple[bytes, ...]:
        operation_id = self._identity(operation_id)
        sha(expected_intent_sha256)
        with lifecycle_read_operation(self.service.owner) as conn:
            conn.execute("BEGIN")
            try:
                entry = self._checked(
                    _verify_journal(conn, limits=self.limits), operation_id, expected_intent_sha256
                )
                result = entry.packages
            finally:
                conn.rollback()
        return result

    def prepare(
        self,
        review_id: str,
        action_evidence_id: str,
        operation_id: str,
        *,
        previous_packages: tuple[bytes, ...] = (),
    ) -> PreparedFinalizationV1:
        review_id, action_evidence_id, operation_id = (
            self._identity(value) for value in (review_id, action_evidence_id, operation_id)
        )
        with _prepared_finalization_operation(self.service.owner, operation_id, "prepare") as conn:
            conn.execute("BEGIN IMMEDIATE")
            journal = _verify_journal(conn, limits=self.limits)
            if operation_id in journal:
                entry = self._checked(journal, operation_id)
                self._binding(entry)
                if (
                    entry.body["review_id"] != review_id
                    or entry.body["action_evidence_id"] != action_evidence_id
                ):
                    _fail("intent_mismatch", "Same operation cannot change review or action")
                result = _reference(entry)
            else:
                if _confirmed_receipt(
                    self.service, conn, review_id, action_evidence_id, operation_id
                ):
                    _fail("already_finalized", "An existing finalized operation cannot be prepared")
                if len(journal) >= 1024:
                    _fail("journal_limit_exceeded", "Offline intent capacity reached")
                plan = _compile_finalization(
                    self.service, conn, review_id, action_evidence_id, operation_id
                )
                packages = _build_prepared_native_closure_v2(
                    self.service,
                    conn,
                    fact=plan.fact,
                    decision=plan.decision,
                    event=plan.event,
                    archive_source=self.archive_source,
                    authenticator=self.authenticator,
                    previous_packages=previous_packages,
                    limits=self.limits,
                )
                tail = decode_native_closure_v2(packages[-1], limits=self.limits)
                body = {
                    "format": "prepared-native-finalization-intent-v1",
                    "scope": "offline-synthetic-only-v1",
                    "binding": asdict(self.service.binding),
                    "core_identity": self.service.owner.config.core_identity,
                    "migration_identity": _migration_contract_digest(),
                    "migration_ledger_digest": MIGRATION_LEDGER_DIGEST,
                    "operation_id": operation_id,
                    "review_id": review_id,
                    "action_evidence_id": action_evidence_id,
                    "prepared_at": json.loads(plan.event[4])["accepted_at"],
                    "expires_at": json.loads(
                        conn.execute(
                            "SELECT material_json FROM l2_reviews WHERE review_id=?", (review_id,)
                        ).fetchone()[0]
                    )["expires_at"],
                    "before": _prepared_before_v2(conn, packages, limits=self.limits),
                    "append": {
                        key: dict(zip(COLUMNS[table], values, strict=True))
                        for key, table, values in (
                            ("event", "events", plan.event),
                            ("fact", "facts", plan.fact),
                            ("decision", "decisions", plan.decision),
                        )
                    },
                    "packages": [hashlib.sha256(package).hexdigest() for package in packages],
                    "projected_frontier": asdict(
                        ClosureFrontierV2(
                            hashlib.sha256(packages[-1]).hexdigest(),
                            tail.last_event_seq,
                            tail.last_event_sha256,
                        )
                    ),
                    "projected_inventory": asdict(tail.inventory),
                }
                stored = conn.execute(
                    "SELECT coalesce(sum(length(payload)),0) FROM l2_finalization_materials"
                ).fetchone()[0]
                for package_hash, package in zip(body["packages"], packages, strict=True):
                    existing = conn.execute(
                        "SELECT payload FROM l2_finalization_materials WHERE package_sha256=?",
                        (package_hash,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != package:
                            _fail(
                                "journal_mismatch", "Existing package identity has different bytes"
                            )
                        continue
                    stored += len(package)
                    if stored > 64 * 1024 * 1024:
                        _fail("journal_limit_exceeded", "Offline material capacity exceeded")
                    conn.execute(
                        "INSERT INTO l2_finalization_materials VALUES(?,?)", (package_hash, package)
                    )
                intent_hash = digest(body)
                conn.execute(
                    "INSERT INTO l2_finalization_intents VALUES(?,?,?,?,?,?)",
                    (
                        operation_id,
                        review_id,
                        action_evidence_id,
                        intent_hash,
                        canonical(body),
                        body["prepared_at"],
                    ),
                )
                entry = self._checked(_verify_journal(conn, limits=self.limits), operation_id)
                result = _reference(entry)
                _commit_local(conn, self.service.owner)
        return result

    def apply_offline(self, operation_id: str, *, expected_intent_sha256: str) -> CommitReceipt:
        operation_id = self._identity(operation_id)
        sha(expected_intent_sha256)
        with _prepared_finalization_operation(self.service.owner, operation_id, "apply") as conn:
            conn.execute("BEGIN IMMEDIATE")
            entry = self._checked(
                _verify_journal(conn, limits=self.limits), operation_id, expected_intent_sha256
            )
            self._binding(entry)
            body = entry.body
            if entry.outcome == "CANCELLED_OFFLINE":
                _fail("cancelled", "Cancelled preparation cannot be applied")
            if entry.outcome == "APPLIED_OFFLINE":
                result = _confirmed_receipt(
                    self.service, conn, body["review_id"], body["action_evidence_id"], operation_id
                )
                if result != entry.receipt:
                    _fail("journal_mismatch", "Applied original receipt is absent or changed")
            else:
                plan = _compile_finalization(
                    self.service,
                    conn,
                    body["review_id"],
                    body["action_evidence_id"],
                    operation_id,
                    record_id=body["append"]["fact"]["record_id"],
                    accepted_at=body["prepared_at"],
                    reservation=operation_id,
                )
                actual = {
                    key: dict(zip(COLUMNS[table], values, strict=True))
                    for key, table, values in (
                        ("event", "events", plan.event),
                        ("fact", "facts", plan.fact),
                        ("decision", "decisions", plan.decision),
                    )
                }
                if actual != body["append"]:
                    _fail("intent_mismatch", "Current exact finalization differs from durable plan")
                result = _append_finalization(conn, plan)
                conn.execute(
                    "INSERT INTO l2_finalization_outcomes VALUES(?,?,?,?,?)",
                    (
                        operation_id,
                        entry.intent_sha256,
                        "APPLIED_OFFLINE",
                        self.service._now(),
                        canonical(asdict(result)),
                    ),
                )
                audit_evidence_connection(
                    conn, self.service.port, self.service.binding, self.service.legacy_verifier
                )
                _verify_journal(conn, limits=self.limits)
                _commit_local(conn, self.service.owner)
        return result

    def cancel_offline(
        self, operation_id: str, *, expected_intent_sha256: str
    ) -> FinalizationStateV1:
        operation_id = self._identity(operation_id)
        sha(expected_intent_sha256)
        with _prepared_finalization_operation(self.service.owner, operation_id, "cancel") as conn:
            conn.execute("BEGIN IMMEDIATE")
            journal = _verify_journal(conn, limits=self.limits)
            entry = self._checked(journal, operation_id, expected_intent_sha256)
            self._binding(entry)
            if entry.outcome == "APPLIED_OFFLINE":
                _fail("already_finalized", "Applied preparation cannot be cancelled")
            if entry.outcome is None:
                conn.execute(
                    "INSERT INTO l2_finalization_outcomes VALUES(?,?,?,?,?)",
                    (
                        operation_id,
                        entry.intent_sha256,
                        "CANCELLED_OFFLINE",
                        self.service._now(),
                        None,
                    ),
                )
                journal = _verify_journal(conn, limits=self.limits)
                _commit_local(conn, self.service.owner)
            result = _state(operation_id, journal[operation_id])
        return result
