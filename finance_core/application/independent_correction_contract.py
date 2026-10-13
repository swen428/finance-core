"""Independent correction evidence domains and immutable migration contract."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Protocol

from finance_core.application.admission import TrustedBinding, VerifiedSource
from finance_core.calculation.authoritative_snapshot import canonical_json_text

if TYPE_CHECKING:
    from finance_core.application.independent_correction_source import (
        IndependentCorrectionFields,
        IndependentOriginalSource,
    )

CORRECTION_REVIEW_SCHEMA = "finance-application-correction-review-v1"
CORRECTION_DECISION_SCHEMA = "finance-application-correction-human-decision-v1"
MIGRATION = "060_independent_corrections.sql"


class IndependentCorrectionError(ValueError):
    """The independent correction cannot be proved or safely committed."""


class IndependentCorrectionConflict(IndependentCorrectionError):
    """A prepared predecessor is no longer current."""


class IndependentCorrectionIntegrityError(IndependentCorrectionError):
    """Retained original or correction evidence is incomplete or contradictory."""


def correction_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


@dataclass(frozen=True)
class ExpectedCorrectionDecision:
    binding: TrustedBinding
    source: VerifiedSource
    plan_id: str
    plan_hash: str
    review_projection: Mapping[str, object]
    checked_at: int
    accepted_correction_id: str | None = None


@dataclass(frozen=True)
class VerifiedCorrectionDecision:
    schema: str
    namespace: str
    key_id: str
    instance_id: str
    human_principal_id: str
    decision_id: str
    plan_id: str
    plan_hash: str
    source_evidence_id: str
    source_evidence_digest: str
    display_id: str
    display_evidence_digest: str
    reply_id: str
    reply_evidence_digest: str
    action: str
    issued_at: int
    expires_at: int
    consumed: bool
    decision_digest: str


class CorrectionHumanDecisionAuthority(Protocol):
    def verify_persisted(
        self,
        connection: sqlite3.Connection,
        decision_record_id: str,
        expected: ExpectedCorrectionDecision,
    ) -> VerifiedCorrectionDecision:
        """Verify signed exact full display and private human reply in this snapshot.

        Fresh verification requires the current delivered display and unconsumed
        approval. Historical verification uses checked_at and retained delivery,
        human ownership, reply and signing evidence at the immutable acceptance
        time. It must not require current display freshness or read another DB.
        consumed=False records external state at acceptance; Core owns consumption.
        Legacy SignedDecision or posting evidence cannot be relabelled here.
        """
        ...


def require_independent_correction_schema(conn: sqlite3.Connection) -> None:
    sql = files("finance_core.resources.migrations").joinpath(MIGRATION).read_text("utf-8")
    expected: dict[tuple[str, str], str] = {}
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            match = re.search(
                r"\bCREATE\s+(TABLE|TRIGGER)\s+([a-z][a-z0-9_]*)\b", statement, flags=re.IGNORECASE
            )
            if match:
                expected[(match[1].lower(), match[2])] = statement
            statement = ""
    observed = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'application_correction_%'"
        )
    }

    def normalize(value: str) -> str:
        return re.sub(r"(?m)^\s*--[^\n]*", "", value).strip().rstrip(";").strip()

    if set(expected) != set(observed) or any(
        normalize(value) != normalize(observed[key]) for key, value in expected.items()
    ):
        raise IndependentCorrectionIntegrityError(
            "Independent correction schema changed or missing"
        )
    ledger = conn.execute(
        "SELECT migration_id,migration_filename,migration_sequence,checksum_sha256 "
        "FROM schema_migrations WHERE migration_id='060' OR migration_sequence=60 "
        "OR migration_filename=?",
        (MIGRATION,),
    ).fetchall()
    if len(ledger) != 1 or tuple(ledger[0]) != (
        "060",
        MIGRATION,
        60,
        hashlib.sha256(sql.encode()).hexdigest(),
    ):
        raise IndependentCorrectionIntegrityError(
            "Independent correction migration identity changed"
        )
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise IndependentCorrectionIntegrityError("Foreign key enforcement is disabled")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise IndependentCorrectionIntegrityError("Correction database foreign key violation")


HumanCorrectionDecisionAuthority = CorrectionHumanDecisionAuthority


def has_independent_correction(conn: sqlite3.Connection, target_id: str) -> bool:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    audit = (
        "financial_audit_events" in tables
        and conn.execute(
            "SELECT 1 FROM financial_audit_events WHERE aggregate_type='transaction' "
            "AND aggregate_public_id=? AND event_type='independent_transaction_correction_applied' "
            "LIMIT 1",
            (target_id,),
        ).fetchone()
        is not None
    )
    recorded = (
        "schema_migrations" in tables
        and conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id='060' OR migration_sequence=60 "
            "OR migration_filename=?",
            (MIGRATION,),
        ).fetchone()
        is not None
    )
    objects = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name LIKE 'application_correction_%' LIMIT 1"
    ).fetchone()
    if objects is None and not recorded:
        if audit:
            raise IndependentCorrectionIntegrityError("Correction audit exists without schema")
        return False
    require_independent_correction_schema(conn)
    if audit:
        return True
    for table in ("versions", "decisions", "receipt_facts"):
        if (
            conn.execute(
                f"SELECT 1 FROM application_correction_{table} WHERE target_id=? LIMIT 1",
                (target_id,),
            ).fetchone()
            is not None
        ):
            return True
    return (
        conn.execute(
            "SELECT 1 FROM authoritative_calculation_snapshots s "
            "JOIN application_correction_plans p ON s.authorization_reference=p.correction_id "
            "WHERE p.target_id=? LIMIT 1",
            (target_id,),
        ).fetchone()
        is not None
    )


@dataclass(frozen=True)
class IndependentCorrectionPlan:
    plan_id: str
    plan_hash: str
    projection: Mapping[str, object]
    expires_at: int


@dataclass(frozen=True)
class IndependentEffectiveTransaction:
    target_id: str
    version: int
    fields: IndependentCorrectionFields
    predecessor_id: str | None
    predecessor_hash: str
    snapshot_id: str | None
    snapshot_hash: str | None
    history: tuple[Mapping[str, object], ...]
    original_source: IndependentOriginalSource

    @property
    def transaction_id(self) -> str:
        return self.target_id


@dataclass(frozen=True)
class IndependentCorrectionResult:
    applied: IndependentEffectiveTransaction
    current: IndependentEffectiveTransaction
    recovered: bool
