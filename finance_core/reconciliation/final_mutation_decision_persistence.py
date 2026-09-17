"""SQLite-backed persistence for reconciliation final mutation guard decisions.

This adapter persists ``FinalMutationGuardDecision`` records and their
dry-run previews into SQLite for auditability and idempotency across
process restarts. It never writes final financial records.

Key invariants:
- No final financial record mutation.
- Identical idempotency replay is safe; changed content conflicts.
- JSON fields use deterministic ordering via ``sort_keys=True``.
- Monetary amounts are stored as strings (not floats).
- The repository executes SQL without commit/rollback; the persistence
  service owns the decision + chain-event transaction.
- The module accepts an explicit ``sqlite3.Connection``; it never opens
  ``database/finance.db`` itself.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from finance_core.financial_audit import (
    AuditEventCommand,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationGuardDecision,
    FinalMutationPreview,
    FinalMutationProposal,
)
from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FinalMutationGuardDecisionAlreadyExists(Exception):
    """Raised when an idempotency key collision is detected on insert."""


class FinalMutationGuardDecisionPersistenceError(Exception):
    """Raised when a guard decision persistence operation fails."""


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalMutationGuardDecisionRecord:
    """A flat record representation of a persisted guard decision, suitable
    for reading back from the database without loading the full domain model."""

    id: int
    proposal_id: str
    idempotency_key: str
    action: str
    approved: bool
    blocked_reasons: tuple[str, ...]
    preview_json: str | None
    evidence_refs: tuple[str, ...]
    source_statement_ref: str | None
    source_app_transaction_ref: str | None
    target_transaction_id: str | None
    guard_version: str
    actor_type: str
    actor_id: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Idempotency key builder
# ---------------------------------------------------------------------------


def build_final_mutation_guard_idempotency_key(
    decision: FinalMutationGuardDecision,
    proposal: FinalMutationProposal | None = None,
) -> str:
    """Build a deterministic idempotency key from a guard decision and
    optional proposal context.

    The key includes guard version, proposal_id, action, source refs,
    target transaction id, evidence refs, blocked reasons (or preview
    content hash for approved), so that materially different decisions
    produce different keys even for the same proposal_id.

    No randomness or timestamps are used.
    """
    parts: list[str] = [
        "final-mutation-guard",
        decision.guard_version,
        decision.proposal_id,
        decision.action.value,
    ]

    if proposal is not None:
        if proposal.source_statement_ref:
            parts.append(f"ssr:{proposal.source_statement_ref}")
        if proposal.source_app_transaction_ref:
            parts.append(f"satr:{proposal.source_app_transaction_ref}")
        if proposal.target_transaction_id:
            parts.append(f"tti:{proposal.target_transaction_id}")

    # Evidence refs in deterministic order
    if proposal is not None and proposal.evidence_refs:
        parts.append("ev:" + ",".join(sorted(proposal.evidence_refs)))

    if decision.approved and decision.preview is not None:
        # Hash the preview content for approved decisions
        preview_payload = _serialize_preview_for_hashing(decision.preview)
        preview_hash = _sha256(preview_payload)
        parts.append(f"pv:{preview_hash}")
    else:
        # Use blocked reasons for blocked decisions
        parts.append("br:" + ",".join(sorted(decision.blocked_reasons)))

    joined = "|".join(parts)
    return _sha256(joined)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class FinalMutationGuardDecisionRepository:
    """Transaction-neutral SQL access for guard-decision records.

    Usage::

        conn = connect_sqlite(":memory:")
        repo = FinalMutationGuardDecisionRepository(conn)
        persist_final_mutation_guard_decision(
            conn, decision, proposal, idempotency_key="..."
        )
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    # ------------------------------------------------------------------
    # Public persistence
    # ------------------------------------------------------------------

    def insert(
        self,
        decision: FinalMutationGuardDecision,
        proposal: FinalMutationProposal | None = None,
        *,
        idempotency_key: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> tuple[str, bool]:
        """Insert a guard decision inside a caller-owned transaction.

        If ``idempotency_key`` is not provided, one is built from the
        decision and proposal context.

        Returns ``(key, idempotent)``. Identical replay is safe; changed
        content under the same key raises a stable conflict.
        """
        if not self._conn.in_transaction:
            raise FinalMutationGuardDecisionPersistenceError(
                "Guard decision repository insert requires a caller-owned transaction"
            )
        key = idempotency_key or build_final_mutation_guard_idempotency_key(decision, proposal)

        # --- Guard decision consistency checks ---
        if decision.approved and decision.preview is None:
            raise FinalMutationGuardDecisionPersistenceError(
                "Approved guard decision must have a preview"
            )
        if not decision.approved and decision.preview is not None:
            raise FinalMutationGuardDecisionPersistenceError(
                "Blocked guard decision must not have a preview"
            )

        preview_json: str | None = None
        if decision.approved and decision.preview is not None:
            preview_json = _serialize_preview(decision.preview)

        blocked_reasons_json = _serialize_json(tuple(sorted(decision.blocked_reasons)))
        evidence_refs_json = _serialize_json(
            tuple(sorted(proposal.evidence_refs if proposal is not None else ()))
        )

        expected = {
            "proposal_id": decision.proposal_id,
            "action": decision.action.value,
            "approved": 1 if decision.approved else 0,
            "blocked_reasons_json": blocked_reasons_json,
            "preview_json": preview_json,
            "evidence_refs_json": evidence_refs_json,
            "source_statement_ref": (
                proposal.source_statement_ref if proposal is not None else None
            ),
            "source_app_transaction_ref": (
                proposal.source_app_transaction_ref if proposal is not None else None
            ),
            "target_transaction_id": (
                proposal.target_transaction_id if proposal is not None else None
            ),
            "guard_version": decision.guard_version,
            "actor_type": actor_type,
            "actor_id": actor_id,
        }
        existing = self._conn.execute(
            """SELECT proposal_id, action, approved, blocked_reasons_json,
            preview_json, evidence_refs_json, source_statement_ref,
            source_app_transaction_ref, target_transaction_id, guard_version,
            actor_type, actor_id
            FROM reconciliation_final_mutation_guard_decisions
            WHERE idempotency_key = ?""",
            (key,),
        ).fetchone()
        if existing is not None:
            if all(existing[field] == value for field, value in expected.items()):
                return key, True
            raise FinalMutationGuardDecisionAlreadyExists(
                f"Conflicting guard decision for idempotency_key: {key}"
            )
        self._conn.execute(
            """
            INSERT INTO reconciliation_final_mutation_guard_decisions (
                proposal_id, idempotency_key, action, approved,
                blocked_reasons_json, preview_json, evidence_refs_json,
                source_statement_ref, source_app_transaction_ref,
                target_transaction_id, guard_version, actor_type, actor_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.proposal_id,
                key,
                decision.action.value,
                1 if decision.approved else 0,
                blocked_reasons_json,
                preview_json,
                evidence_refs_json,
                proposal.source_statement_ref if proposal is not None else None,
                proposal.source_app_transaction_ref if proposal is not None else None,
                proposal.target_transaction_id if proposal is not None else None,
                decision.guard_version,
                actor_type,
                actor_id,
            ),
        )
        return key, False

    def save(
        self,
        decision: FinalMutationGuardDecision,
        proposal: FinalMutationProposal | None = None,
        *,
        idempotency_key: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> str:
        """Compatibility facade delegating transaction ownership to the service."""
        return persist_final_mutation_guard_decision(
            self._conn,
            decision,
            proposal,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
        )

    # ------------------------------------------------------------------
    # Public retrieval
    # ------------------------------------------------------------------

    def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> FinalMutationGuardDecisionRecord | None:
        """Load a single guard decision by its idempotency key."""
        row = self._conn.execute(
            """
            SELECT * FROM reconciliation_final_mutation_guard_decisions
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def list_by_proposal_id(self, proposal_id: str) -> list[FinalMutationGuardDecisionRecord]:
        """List all guard decisions for a given proposal_id, ordered by
        insertion."""
        rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_final_mutation_guard_decisions
            WHERE proposal_id = ?
            ORDER BY id ASC
            """,
            (proposal_id,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_by_source_statement_ref(
        self, source_statement_ref: str
    ) -> list[FinalMutationGuardDecisionRecord]:
        """List guard decisions by source statement reference."""
        rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_final_mutation_guard_decisions
            WHERE source_statement_ref = ?
            ORDER BY id ASC
            """,
            (source_statement_ref,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_by_source_app_transaction_ref(
        self, source_app_transaction_ref: str
    ) -> list[FinalMutationGuardDecisionRecord]:
        """List guard decisions by source app transaction reference."""
        rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_final_mutation_guard_decisions
            WHERE source_app_transaction_ref = ?
            ORDER BY id ASC
            """,
            (source_app_transaction_ref,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]


def persist_final_mutation_guard_decision(
    conn: sqlite3.Connection,
    decision: FinalMutationGuardDecision,
    proposal: FinalMutationProposal | None = None,
    *,
    idempotency_key: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> str:
    """Atomically persist a reconciliation guard decision and its audit event."""
    require_staging_database(conn)
    if conn.in_transaction:
        raise FinalMutationGuardDecisionPersistenceError(
            "Guard decision persistence requires no pending transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        key, idempotent = FinalMutationGuardDecisionRepository(conn).insert(
            decision,
            proposal,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        preview_json = (
            _serialize_preview(decision.preview)
            if decision.approved and decision.preview is not None
            else None
        )
        if idempotent:
            event_id = _guard_decision_audit_event_id(decision.proposal_id, key)
            if FinancialAuditRepository(conn).fetch(event_id) is not None:
                _append_guard_decision_audit(
                    conn,
                    decision=decision,
                    proposal=proposal,
                    idempotency_key=key,
                    preview_json=preview_json,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
        else:
            _append_guard_decision_audit(
                conn,
                decision=decision,
                proposal=proposal,
                idempotency_key=key,
                preview_json=preview_json,
                actor_type=actor_type,
                actor_id=actor_id,
            )
        conn.commit()
        return key
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _append_guard_decision_audit(
    conn: sqlite3.Connection,
    *,
    decision: FinalMutationGuardDecision,
    proposal: FinalMutationProposal | None,
    idempotency_key: str,
    preview_json: str | None,
    actor_type: str,
    actor_id: str | None,
) -> None:
    event_type = "reconciliation_guard_decision_recorded"
    event_id = _guard_decision_audit_event_id(decision.proposal_id, idempotency_key)
    evidence = set(proposal.evidence_refs if proposal is not None else ())
    if proposal is not None and proposal.source_statement_ref:
        evidence.add(f"statement:{proposal.source_statement_ref}")
    if proposal is not None and proposal.source_app_transaction_ref:
        evidence.add(f"app-transaction:{proposal.source_app_transaction_ref}")
    decision_state = {
        "idempotency_key": idempotency_key,
        "action": decision.action.value,
        "approved": decision.approved,
        "blocked_reasons": tuple(sorted(decision.blocked_reasons)),
        "guard_version": decision.guard_version,
        "preview": json.loads(preview_json) if preview_json is not None else None,
    }
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="reconciliation_proposal",
            aggregate_public_id=decision.proposal_id,
            event_type=event_type,
            event_payload=decision_state,
            previous_state=None,
            new_state=decision_state,
            actor_type=actor_type,
            actor_public_id=actor_id or f"reconciliation-guard:{actor_type}",
            source_evidence_references=tuple(evidence),
            correlation_public_id=decision.proposal_id,
            causation_public_id=idempotency_key,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def _guard_decision_audit_event_id(proposal_id: str, idempotency_key: str) -> str:
    return derive_audit_event_public_id(
        aggregate_type="reconciliation_proposal",
        aggregate_public_id=proposal_id,
        event_type="reconciliation_guard_decision_recorded",
        causation_public_id=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialize_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _serialize_preview(preview: FinalMutationPreview) -> str:
    """Serialize a preview to deterministic JSON."""
    return _serialize_json(
        {
            "proposal_id": preview.proposal_id,
            "action": preview.action.value,
            "amount": preview.amount,
            "currency": preview.currency,
            "merchant": preview.merchant,
            "transaction_date": preview.transaction_date,
            "target_transaction_id": preview.target_transaction_id,
            "suggested_fields": _sorted_dict(preview.suggested_fields),
            "source_statement_ref": preview.source_statement_ref,
            "source_app_transaction_ref": preview.source_app_transaction_ref,
            "evidence_refs": tuple(sorted(preview.evidence_refs)),
            "is_dry_run": preview.is_dry_run,
            "preview_note": preview.preview_note,
        }
    )


def _serialize_preview_for_hashing(preview: FinalMutationPreview) -> str:
    """Serialize preview content for idempotency key hashing."""
    return json.dumps(
        {
            "proposal_id": preview.proposal_id,
            "action": preview.action.value,
            "amount": str(preview.amount) if preview.amount is not None else None,
            "currency": preview.currency,
            "merchant": preview.merchant,
            "transaction_date": preview.transaction_date,
            "target_transaction_id": preview.target_transaction_id,
            "suggested_fields": _sorted_dict(preview.suggested_fields),
            "source_statement_ref": preview.source_statement_ref,
            "source_app_transaction_ref": preview.source_app_transaction_ref,
            "evidence_refs": tuple(sorted(preview.evidence_refs)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sorted_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a dict with keys sorted for deterministic serialization."""
    return {k: d[k] for k in sorted(d)}


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _row_to_record(row: sqlite3.Row) -> FinalMutationGuardDecisionRecord:
    blocked_reasons: tuple[str, ...] = ()
    if row["blocked_reasons_json"]:
        blocked_reasons = tuple(json.loads(row["blocked_reasons_json"]))

    evidence_refs: tuple[str, ...] = ()
    if row["evidence_refs_json"]:
        evidence_refs = tuple(json.loads(row["evidence_refs_json"]))

    return FinalMutationGuardDecisionRecord(
        id=row["id"],
        proposal_id=row["proposal_id"],
        idempotency_key=row["idempotency_key"],
        action=row["action"],
        approved=bool(row["approved"]),
        blocked_reasons=blocked_reasons,
        preview_json=row["preview_json"],
        evidence_refs=evidence_refs,
        source_statement_ref=row["source_statement_ref"],
        source_app_transaction_ref=row["source_app_transaction_ref"],
        target_transaction_id=row["target_transaction_id"],
        guard_version=row["guard_version"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        created_at=row["created_at"],
    )


__all__ = [
    "FinalMutationGuardDecisionAlreadyExists",
    "FinalMutationGuardDecisionPersistenceError",
    "FinalMutationGuardDecisionRecord",
    "FinalMutationGuardDecisionRepository",
    "build_final_mutation_guard_idempotency_key",
]
