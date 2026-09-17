"""Receipt split finalization runtime -- atomic, authorized, durably idempotent.

Converts a calculated shared expense result into persisted settlement
obligations, canonical transaction records, and immutable audit evidence
inside one explicit database transaction.

Key invariants:
- Loads authorization from persisted records -- never trusts caller DTOs.
- BEGIN IMMEDIATE ... COMMIT / ROLLBACK -- no partial state.
- Durable idempotency via database-backed idempotency table.
- Creates canonical transactions in the ``transactions`` table (migration 001).
- Persists complete audit record with all authoritative IDs.
- No runtime DDL -- migrations 020, 024, and 025 must be applied.
- Staging databases only -- refuses ``database/finance.db``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from finance_core.calculation.authoritative_snapshot import (
    SnapshotVerificationError,
    verify_snapshot_binding,
)
from finance_core.calculation.run_persistence import CalculationRunRepository
from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.money import (
    MoneyValidationError,
    canonical_money_str,
    money_decimal,
    quantize_for_currency,
)
from finance_core.receipt_finalization.models import (
    ActiveFactSetBinding,
    ConfirmedReceiptIdentity,
    FinalizationAuthorizationError,
    FinalizationBlockReason,
    FinalizationIdempotencyError,
    FinalizationInput,
    FinalizationOutput,
    FinalizationPersistenceError,
    FinalizationStatus,
    FinalizationValidationError,
    IneligibleForFinalizationError,
    ReceiptGroupMaterialization,
    ReceiptGroupMaterializationError,
    build_finalization_content_fingerprint,
)
from finance_core.receipt_finalization.persistence import (
    FactSetBindingEvidenceError,
    append_fact_set_binding_evidence,
    read_fact_set_binding_evidence,
)
from finance_core.receipt_finalization.snapshot_authority import (
    IAF_CALCULATION_RUN_ENTITY_TYPE,
    IAF_CALCULATION_RUN_RULE_VERSION,
    IAF_CALCULATION_RUN_SOURCE_TYPE,
    IAF_CALCULATION_RUN_STATUS,
    IAF_CALCULATION_RUN_TYPE,
    SnapshotAuthorityError,
    SnapshotBoundAuthority,
    read_snapshot_bound_authority,
)
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

CALCULATION_VERSION = "receipt_finalization_v2"
ELIGIBLE_RECEIPT_GROUP_STATUSES = {"active", "calculated"}

# Distinguishes "the snapshot carries no collection total" from "the snapshot
# explicitly carries null": the first is derivable, the second is corrupt.
_MISSING: Any = object()

# Schema version check -- raised when migrations 020, 024, 025, or 039 are missing.
_REQUIRED_TABLES = frozenset(
    {
        "authoritative_calculation_snapshots",
        "financial_audit_events",
        "receipt_finalization_confirmations",
        "receipt_finalization_authorizations",
        "receipt_finalization_audit",
        "receipt_finalization_idempotency",
        "receipt_finalization_membership_evidence",
    }
)

# ---------------------------------------------------------------------------
# Strict string-list validation (shared helper)
# ---------------------------------------------------------------------------


def _parse_unique_nonempty_string_list(
    raw_json: str,
    *,
    label: str,
    malformed_reason: str,
    mismatch_reason: str,
) -> list[str]:
    """Parse, validate, and return a de-duplicated sorted list of non-empty strings.

    Raises ``FinalizationAuthorizationError`` with *malformed_reason* for
    structural failures (non-JSON, non-list, non-string values, empty
    strings, duplicate values).  Callers then compare the returned ordered
    list against an expected value and raise with *mismatch_reason* on
    content mismatch.

    No values are silently coerced.  No duplicates are silently removed.
    """
    try:
        values = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise FinalizationAuthorizationError(
            f"{label} is malformed JSON",
            reason=malformed_reason,
        ) from exc

    if not isinstance(values, list):
        raise FinalizationAuthorizationError(
            f"{label} must be a JSON list, got {type(values).__name__}",
            reason=malformed_reason,
        )

    seen: set[str] = set()
    result: list[str] = []
    for i, v in enumerate(values):
        if not isinstance(v, str):
            raise FinalizationAuthorizationError(
                f"{label}[{i}] must be a string, got {type(v).__name__}: {v!r}",
                reason=malformed_reason,
            )
        if v == "":
            raise FinalizationAuthorizationError(
                f"{label}[{i}] is an empty string",
                reason=malformed_reason,
            )
        if v in seen:
            raise FinalizationAuthorizationError(
                f"{label} contains duplicate value {v!r}",
                reason=mismatch_reason,
            )
        seen.add(v)
        result.append(v)

    return sorted(result)


# Deleted the duplicate FinalizationBlockReason class -- now imported from models.


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def finalize_receipt_split(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    *,
    clock: Callable[[], str] | None = None,
) -> FinalizationOutput:
    """Finalize a calculated receipt split -- atomic, authorized, durably idempotent.

    All writes occur inside one explicit ``BEGIN IMMEDIATE ... COMMIT``.
    On any error, everything is rolled back.
    """
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    created_at = clock() if clock is not None else datetime.now(timezone.utc).isoformat()

    # --- schema check (fail fast, no writes) ---
    _require_schema(conn)

    # --- idempotency key required for all paths ---
    if not fin_input.idempotency_key:
        raise FinalizationValidationError("idempotency_key is required for finalization")

    # --- authorization required ---
    if not fin_input.authorization_id:
        raise FinalizationAuthorizationError(
            "authorization_id is required; receipt finalization requires persisted authorization",
            reason=FinalizationBlockReason.AUTHORIZATION_MISSING,
        )

    # --- compute content fingerprint (deterministic, no DB access) ---
    fingerprint = build_finalization_content_fingerprint(fin_input)

    if not fin_input.calculation_snapshot_hash:
        raise FinalizationValidationError(
            "Authoritative calculation snapshot hash is required for finalization"
        )

    # --- check durable idempotency (read-only, before any transaction) ---
    idem = _check_idempotency(conn, fin_input.idempotency_key, fingerprint)
    if idem is not None:
        replay = _verify_replay_in_coherent_snapshot(
            conn,
            fin_input=fin_input,
            fingerprint=fingerprint,
            recheck=lambda: _check_idempotency(conn, fin_input.idempotency_key, fingerprint),
        )
        if replay is not None:
            return replay
        # If we reach here, the idempotency row vanished between the two
        # checks (concurrent rollback); continue to the normal path.

    # --- already-finalized / different-key replay (read-only) ---
    # The group may already be durably finalized with this exact content under
    # another idempotency key.  That replay path must run BEFORE authorization
    # validation: the consumed authorization is the expected durable state of
    # a completed finalization, never a reason to refuse its replay.  Both
    # replay entry paths call the same complete graph verifier.
    existing = _check_existing_finalization(conn, fin_input.receipt_group_public_id, fingerprint)
    if existing is not None:
        replay = _verify_replay_in_coherent_snapshot(
            conn,
            fin_input=fin_input,
            fingerprint=fingerprint,
            recheck=lambda: _check_existing_finalization(
                conn, fin_input.receipt_group_public_id, fingerprint
            ),
        )
        if replay is not None:
            return replay

    # Preserve stable authorization failures before resolving its bound
    # snapshot. Authorization is loaded again inside the write transaction.
    _load_and_validate_authorization(conn, fin_input.authorization_id, fin_input, fingerprint)
    _require_authoritative_snapshot_binding(conn, fin_input)
    _require_snapshot_bound_authority(conn, fin_input)

    # --- BEGIN IMMEDIATE: acquire write lock, start transaction ---
    conn.execute("BEGIN IMMEDIATE")

    try:
        # A competing finalizer can commit after the optimistic pre-check but
        # before this connection acquires the write lock. Recheck inside the
        # serialized transaction so an identical concurrent command replays
        # the durable result instead of observing a consumed authorization.
        idem = _check_idempotency(conn, fin_input.idempotency_key, fingerprint)
        if idem is not None:
            _require_complete_replay_truth(
                conn, audit_id=idem.audit_id or "", fin_input=fin_input, fingerprint=fingerprint
            )
            # Replay never writes: release the write lock with a rollback so an
            # accidental future write inside the verifier can never persist.
            conn.rollback()
            return idem

        # 2. Check receipt group not already finalized (before any group write).
        # This runs before authorization state validation: a same-content
        # replay of an already-finalized group observes its authorization as
        # 'consumed', which the complete verifier requires, never refuses.
        existing = _check_existing_finalization(
            conn, fin_input.receipt_group_public_id, fingerprint
        )
        if existing is not None:
            _require_complete_replay_truth(
                conn,
                audit_id=existing.audit_id or "",
                fin_input=fin_input,
                fingerprint=fingerprint,
            )
            conn.rollback()
            return existing

        # 1. Load and validate persisted authorization (includes confirmation).
        _ = _load_and_validate_authorization(
            conn, fin_input.authorization_id, fin_input, fingerprint
        )

        # 1b. Derive the single binding authority from the persisted,
        # hash-verified authoritative snapshot.  The caller's DTO binding and
        # receipt identity are requests: they must agree with the snapshot or
        # this finalization is refused before any write.
        authority = _require_snapshot_bound_authority(conn, fin_input)

        # 1c. IAF.7: re-read the receipt's live active fact set inside this
        # write transaction using the snapshot-derived four-tuple and fail
        # closed if it drifted.  No-op for non-IAF (group-only) finalizations.
        active_binding = (
            authority.active_fact_set_binding
            if authority is not None
            else fin_input.active_fact_set_binding
        )
        _require_active_fact_set_binding(conn, active_binding)

        # 3. Materialize the deterministic single-receipt group inside this same
        # Unit of Work, so a later failure rolls it back with every other write
        # and a fact-set supersession stays possible.  An existing group is
        # fully compared, never silently adopted.
        if fin_input.receipt_group_materialization is not None:
            _materialize_receipt_group(
                conn,
                receipt_group_public_id=fin_input.receipt_group_public_id,
                currency=fin_input.currency,
                spec=fin_input.receipt_group_materialization,
                binding=active_binding,
            )

        # 4. Look up receipt group.
        group = _lookup_receipt_group(conn, fin_input.receipt_group_public_id)

        # 5. Validate receipt group eligibility.
        _require_currency_match(group, fin_input)
        _require_eligible_for_finalization(group, fin_input.receipt_group_public_id)

        # 6. Look up participants using the exact authoritative membership
        # scope: the snapshot-bound receipt for IAF finalizations,
        # uncontradicted group-scoped membership for legacy group-only
        # finalizations.
        participants_by_public_id = _lookup_participants_by_public_id(
            conn,
            fin_input,
            group["id"],
            bound_receipt_public_id=(
                active_binding.receipt_public_id if active_binding is not None else None
            ),
        )
        payer_id = _resolve_participant(
            participants_by_public_id, fin_input.payer_participant_public_id
        )

        # 7. Validate snapshot identity against DB participants.
        _validate_snapshot_identity(fin_input, participants_by_public_id)

        # 8. Pre-write revalidation of obligations.
        _revalidate_obligations_match_snapshot(fin_input)

        # 9. Compute monetary totals.  ``total_to_collect`` is the payer's own
        # collection total: it must equal the calculator's value and the sum of
        # the obligations owed to the payer, or the audit total would be a
        # fabricated number.
        actual_total_paid = _snapshot_amount(
            fin_input.calculation_snapshot, "total_paid", fin_input.currency
        )
        total_to_collect = _resolve_total_to_collect(fin_input, total_paid=actual_total_paid)

        # 10. Insert canonical transaction using the confirmed receipt's own
        # merchant and date when the snapshot binds a receipt identity.
        receipt_identity = authority.confirmed_receipt_identity if authority is not None else None
        if receipt_identity is not None:
            _require_live_receipt_identity(conn, receipt_identity)
        txn_public_id = _make_transaction_public_id(fin_input)
        txn_date = _derive_transaction_date(
            fin_input.calculation_snapshot, created_at, receipt_identity=receipt_identity
        )
        _insert_canonical_transaction(
            conn,
            public_id=txn_public_id,
            payer_id=payer_id,
            amount=actual_total_paid,
            currency=fin_input.currency,
            fin_input=fin_input,
            txn_date=txn_date,
            receipt_identity=receipt_identity,
        )

        # 11. Insert calculation run + participant shares.
        calc_run_id = _insert_calculation_run(
            conn, fin_input, group["id"], group["currency"], participants_by_public_id
        )
        _insert_participant_shares(conn, calc_run_id, fin_input, participants_by_public_id)

        # 12. Insert settlement obligations.
        settlement_pub_ids = _insert_settlement_obligations(
            conn, calc_run_id, fin_input, participants_by_public_id, payer_id
        )

        # 13. Build finalization ID.
        finalization_pub_id = f"fin_{fin_input.calculation_run_public_id}"

        # 14. Insert audit record (before idempotency for FK ordering).
        total_paid_str = canonical_money_str(actual_total_paid, fin_input.currency)
        total_collect_str = canonical_money_str(total_to_collect, fin_input.currency)
        _insert_audit(
            conn,
            finalization_id=finalization_pub_id,
            fin_input=fin_input,
            fingerprint=fingerprint,
            txn_public_id=txn_public_id,
            settlement_public_ids=settlement_pub_ids,
            total_paid_str=total_paid_str,
            total_to_collect_str=total_collect_str,
            status=FinalizationStatus.FINALIZED.value,
            created_at=created_at,
        )

        # 14b. Bind the finalization audit to the consumed fact-set four-tuple.
        if active_binding is not None:
            _append_audit_fact_set_binding_evidence(
                conn,
                binding=active_binding,
                finalization_id=finalization_pub_id,
                created_at=created_at,
            )

        # 14c. Record the exact receipt-scoped membership this finalization
        # verified and consumed (R3-07-1, migration 039).  One append-only row
        # per referenced participant, carrying the receipt dimension.
        _insert_membership_evidence(
            conn,
            finalization_id=finalization_pub_id,
            fin_input=fin_input,
            participants_by_public_id=participants_by_public_id,
            bound_receipt_public_id=(
                active_binding.receipt_public_id if active_binding is not None else None
            ),
            created_at=created_at,
        )

        # 15. Insert idempotency record (FK → audit).
        _insert_idempotency(
            conn,
            idempotency_key=fin_input.idempotency_key,
            fingerprint=fingerprint,
            status=FinalizationStatus.FINALIZED.value,
            audit_id=finalization_pub_id,
            created_at=created_at,
        )

        # 16. Consume authorization.
        _consume_authorization(conn, fin_input.authorization_id)

        # 17. Update receipt group status.
        _update_receipt_group_status(conn, fin_input.receipt_group_public_id, "settled")

        # 18. Append settlement + finalization events in the same transaction.
        _append_receipt_finalization_audits(
            conn,
            fin_input=fin_input,
            previous_group_status=str(group["status"]),
            finalization_public_id=finalization_pub_id,
            transaction_public_id=txn_public_id,
            settlement_public_ids=settlement_pub_ids,
            created_at=created_at,
        )

        # --- COMMIT ---
        conn.commit()

    except (
        FinalizationAuthorizationError,
        IneligibleForFinalizationError,
        ReceiptGroupMaterializationError,
    ):
        if conn.in_transaction:
            conn.rollback()
        raise
    except FinalizationValidationError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except FinalizationIdempotencyError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        _reason = FinalizationBlockReason.DATABASE_INTEGRITY_ERROR
        raise FinalizationIdempotencyError(
            f"Database integrity error during finalization: {exc}",
            reason=_reason,
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    return FinalizationOutput(
        finalization_public_id=finalization_pub_id,
        calculation_run_public_id=fin_input.calculation_run_public_id,
        obligations_created=len(fin_input.settlement_obligations),
        settlement_public_ids=settlement_pub_ids,
        status=FinalizationStatus.FINALIZED.value,
        transaction_public_id=txn_public_id,
        audit_id=finalization_pub_id,
        idempotency_key=fin_input.idempotency_key,
    )


def _verify_replay_in_coherent_snapshot(
    conn: sqlite3.Connection,
    *,
    fin_input: FinalizationInput,
    fingerprint: str,
    recheck: Callable[[], FinalizationOutput | None],
) -> FinalizationOutput | None:
    """Verify a durable replay under one coherent read snapshot (R3-04).

    All replay truth verification must observe a single SQLite snapshot to
    prevent mixed-timepoint data; ``BEGIN DEFERRED`` acquires the shared lock
    on first read.  The durable pointer is re-read inside the snapshot via
    ``recheck`` so it is consistent with the subsequent graph selects.
    Returns ``None`` when the pointer vanished (concurrent rollback), leaving
    the caller to continue on the normal finalization path.  Strictly
    zero-write: the snapshot is always released with a rollback.
    """
    require_foreign_keys_enabled(conn)
    replay_in_caller_txn = conn.in_transaction
    if not replay_in_caller_txn:
        conn.execute("BEGIN DEFERRED")
    try:
        inner = recheck()
        if inner is None:
            if not replay_in_caller_txn:
                conn.rollback()
            return None
        _require_complete_replay_truth(
            conn,
            audit_id=inner.audit_id or "",
            fin_input=fin_input,
            fingerprint=fingerprint,
        )
        if not replay_in_caller_txn:
            conn.rollback()
        return inner
    except BaseException:
        if not replay_in_caller_txn and conn.in_transaction:
            conn.rollback()
        raise


def _require_active_fact_set_binding(
    conn: sqlite3.Connection,
    binding: ActiveFactSetBinding | None,
) -> None:
    """Fail closed if the receipt's live active fact set drifted (IAF.7).

    Re-read inside the finalizer's ``BEGIN IMMEDIATE`` transaction: the sole
    active (non-superseded) fact set for the bound receipt must still match
    the authorized four-tuple exactly.  A supersession committed before this
    lock, a version bump, or any hash drift rejects finalization fail-closed
    with zero writes.  ``None`` (non-IAF finalizations) is a no-op.
    """
    if binding is None:
        return
    rows = conn.execute(
        "SELECT ras.fact_set_public_id, ras.version, ras.fact_set_input_hash, "
        "ras.fact_set_result_hash "
        "FROM receipt_item_allocation_fact_sets ras "
        "JOIN receipts r ON r.id = ras.receipt_id "
        "WHERE r.public_id = ? AND ras.superseded_by_fact_set_public_id IS NULL",
        (binding.receipt_public_id,),
    ).fetchall()
    if len(rows) != 1:
        raise FinalizationAuthorizationError(
            f"Receipt {binding.receipt_public_id!r} has {len(rows)} active fact set(s) at "
            "finalization; the authorized fact set is no longer the unique active set",
            reason=FinalizationBlockReason.STALE_ACTIVE_FACT_SET,
        )
    active = rows[0]
    if (
        str(active["fact_set_public_id"]) != binding.fact_set_public_id
        or int(active["version"]) != binding.fact_set_version
        or str(active["fact_set_input_hash"]) != binding.fact_set_input_hash
        or str(active["fact_set_result_hash"]) != binding.fact_set_result_hash
    ):
        raise FinalizationAuthorizationError(
            "The receipt's active fact set was superseded or drifted after "
            "authorization; finalization with a stale fact-set authorization is refused",
            reason=FinalizationBlockReason.STALE_ACTIVE_FACT_SET,
        )


def _require_authoritative_snapshot_binding(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
) -> None:
    try:
        verify_snapshot_binding(
            conn,
            snapshot_public_id=fin_input.calculation_snapshot_id,
            expected_combined_hash=fin_input.calculation_snapshot_hash,
            expected_calculation_type="receipt_split",
            expected_aggregate_public_id=fin_input.receipt_group_public_id,
            expected_currency_contract_version=fin_input.currency_contract_version,
            expected_authorization_reference=fin_input.authorization_id,
            expected_output_payload=fin_input.calculation_snapshot,
        )
    except SnapshotVerificationError as exc:
        raise FinalizationValidationError(
            "Authoritative calculation snapshot binding failed"
        ) from exc


def _require_snapshot_bound_authority(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
) -> SnapshotBoundAuthority | None:
    """Derive the single binding authority and refuse contradicting caller material.

    The active fact-set four-tuple and the confirmed receipt identity are read
    from the persisted, hash-verified authoritative snapshot -- never from the
    caller's DTO.  The DTO's copies are requests: if they disagree with the
    snapshot, or if the request drops a binding the snapshot carries, the
    finalization is refused before any write.  This is what stops an old
    snapshot from being combined with a newer active fact-set binding.

    Returns ``None`` for snapshots that carry no IAF binding material at all
    (legacy and group-only finalizations).
    """
    try:
        authority = read_snapshot_bound_authority(
            conn,
            snapshot_public_id=fin_input.calculation_snapshot_id,
            expected_combined_hash=fin_input.calculation_snapshot_hash,
        )
    except SnapshotAuthorityError as exc:
        raise FinalizationAuthorizationError(
            str(exc),
            reason=exc.reason or FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
        ) from exc

    if authority is None:
        if fin_input.active_fact_set_binding is not None:
            raise FinalizationAuthorizationError(
                f"Snapshot {fin_input.calculation_snapshot_id!r} binds no IAF fact set, but "
                "the finalization request claims an active fact-set binding",
                reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
            )
        if fin_input.confirmed_receipt_identity is not None:
            raise FinalizationAuthorizationError(
                f"Snapshot {fin_input.calculation_snapshot_id!r} binds no confirmed receipt "
                "identity, but the finalization request supplies one",
                reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
            )
        return None

    if fin_input.active_fact_set_binding is None:
        raise FinalizationAuthorizationError(
            f"Snapshot {authority.snapshot_public_id!r} binds IAF fact set "
            f"{authority.active_fact_set_binding.fact_set_public_id!r}, but the finalization "
            "request carries no active fact-set binding",
            reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
        )
    if fin_input.active_fact_set_binding != authority.active_fact_set_binding:
        raise FinalizationAuthorizationError(
            f"The finalization request's active fact-set binding does not match the binding "
            f"hash-bound into authorized snapshot {authority.snapshot_public_id!r}; combining a "
            "snapshot with a different fact set is refused",
            reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
        )
    if (
        fin_input.confirmed_receipt_identity is not None
        and fin_input.confirmed_receipt_identity != authority.confirmed_receipt_identity
    ):
        raise FinalizationAuthorizationError(
            "The finalization request's confirmed receipt identity does not match the identity "
            f"hash-bound into authorized snapshot {authority.snapshot_public_id!r}",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )
    if authority.calculation_run_public_id != fin_input.calculation_run_public_id:
        raise FinalizationAuthorizationError(
            f"Snapshot {authority.snapshot_public_id!r} binds calculation run "
            f"{authority.calculation_run_public_id!r}, not "
            f"{fin_input.calculation_run_public_id!r}",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    if authority.receipt_group_public_id != fin_input.receipt_group_public_id:
        raise FinalizationAuthorizationError(
            f"Snapshot {authority.snapshot_public_id!r} binds receipt group "
            f"{authority.receipt_group_public_id!r}, not "
            f"{fin_input.receipt_group_public_id!r}",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    if authority.currency != fin_input.currency:
        raise FinalizationAuthorizationError(
            f"Snapshot {authority.snapshot_public_id!r} binds currency "
            f"{authority.currency!r}, not {fin_input.currency!r}",
            reason=FinalizationBlockReason.CURRENCY_MISMATCH.value,
        )
    if authority.authorization_reference != fin_input.authorization_id:
        raise FinalizationAuthorizationError(
            f"Snapshot {authority.snapshot_public_id!r} binds authorization "
            f"{authority.authorization_reference!r}, not {fin_input.authorization_id!r}",
            reason=FinalizationBlockReason.AUTHORIZATION_CONTENT_MISMATCH.value,
        )
    materialization = fin_input.receipt_group_materialization
    if (
        materialization is not None
        and materialization.receipt_public_id != authority.active_fact_set_binding.receipt_public_id
    ):
        raise FinalizationAuthorizationError(
            f"The group materialization targets receipt "
            f"{materialization.receipt_public_id!r}, but the authorized snapshot binds receipt "
            f"{authority.active_fact_set_binding.receipt_public_id!r}",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )
    _require_durable_binding_evidence(conn, authority, fin_input)
    return authority


def _require_durable_binding_evidence(
    conn: sqlite3.Connection,
    authority: SnapshotBoundAuthority,
    fin_input: FinalizationInput,
) -> None:
    """Fail closed unless the durable four-tuple evidence matches the authority.

    Migration 037 records one immutable binding evidence row per authority
    record.  The snapshot's, the authorization's, **and the calculation run's**
    rows must all describe the exact four-tuple derived from the snapshot, and
    the durable ``calc_audit_runs`` row must carry the complete run material the
    prepare stage wrote, so a replay cannot accept a conflicting or re-pointed
    calculation run.
    """
    _require_binding_evidence_schema(conn)
    for bound_record_type, bound_record_public_id in (
        ("calculation_snapshot", authority.snapshot_public_id),
        ("finalization_authorization", fin_input.authorization_id),
        ("calculation_run", authority.calculation_run_public_id),
    ):
        durable = read_fact_set_binding_evidence(
            conn,
            bound_record_type=bound_record_type,
            bound_record_public_id=bound_record_public_id,
        )
        if durable is None:
            raise FinalizationAuthorizationError(
                f"No durable fact-set binding evidence for {bound_record_type} "
                f"{bound_record_public_id!r}; the four-tuple binding is unverifiable",
                reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
            )
        if durable != authority.active_fact_set_binding:
            raise FinalizationAuthorizationError(
                f"Durable fact-set binding evidence for {bound_record_type} "
                f"{bound_record_public_id!r} contradicts the authorized four-tuple",
                reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
            )
    _require_durable_calculation_run_truth(conn, authority)


def _require_durable_calculation_run_truth(
    conn: sqlite3.Connection,
    authority: SnapshotBoundAuthority,
) -> None:
    """Re-verify the historical calculation run's complete durable material.

    The finalizer trusts only the persisted, hash-verified snapshot authority.
    The ``calc_audit_runs`` row it references must still carry the exact IAF
    run contract (type, entity type/id, rule version, status, source type) and
    the fact-set result hash, so a run whose entity, source reference, or any
    material field was altered -- or that was deleted -- fails closed with zero
    canonical writes.
    """
    binding = authority.active_fact_set_binding
    durable = CalculationRunRepository(conn).fetch_by_run_id(authority.calculation_run_public_id)
    if durable is None:
        raise FinalizationAuthorizationError(
            f"Calculation run {authority.calculation_run_public_id!r} bound by the authorized "
            "snapshot no longer exists",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )
    expected = (
        IAF_CALCULATION_RUN_TYPE,
        IAF_CALCULATION_RUN_ENTITY_TYPE,
        binding.receipt_public_id,
        IAF_CALCULATION_RUN_RULE_VERSION,
        IAF_CALCULATION_RUN_STATUS,
        IAF_CALCULATION_RUN_SOURCE_TYPE,
        binding.fact_set_result_hash,
        authority.snapshot_created_at,
    )
    actual = (
        durable.run_type,
        durable.entity_type,
        durable.entity_id,
        durable.rule_version,
        durable.status,
        durable.source_type,
        durable.source_reference,
        durable.created_at,
    )
    if actual != expected:
        raise FinalizationAuthorizationError(
            f"Calculation run {authority.calculation_run_public_id!r} durable material "
            f"{actual!r} contradicts the authorized run contract {expected!r}",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH.value,
        )


def _require_binding_evidence_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("receipt_fact_set_binding_evidence",),
    ).fetchone()
    if row is None:
        raise FinalizationPersistenceError(
            "Required schema table receipt_fact_set_binding_evidence is missing. "
            "Migration 037 must be applied before an IAF fact-set finalization runs."
        )


def _append_audit_fact_set_binding_evidence(
    conn: sqlite3.Connection,
    *,
    binding: ActiveFactSetBinding,
    finalization_id: str,
    created_at: str,
) -> None:
    """Bind the finalization audit row to the fact-set four-tuple it consumed."""
    _require_binding_evidence_schema(conn)
    try:
        append_fact_set_binding_evidence(
            conn,
            binding=binding,
            bound_record_type="finalization_audit",
            bound_record_public_id=finalization_id,
            created_at=created_at,
        )
    except FactSetBindingEvidenceError as exc:
        raise FinalizationAuthorizationError(
            str(exc),
            reason=FinalizationBlockReason.SNAPSHOT_BINDING_MISMATCH.value,
        ) from exc


def _require_live_receipt_identity(
    conn: sqlite3.Connection,
    identity: ConfirmedReceiptIdentity,
) -> None:
    """Cross-check the snapshot-bound receipt identity against the live row.

    The canonical transaction's merchant and date come from the confirmed
    receipt, so the hash-bound identity must still equal the receipt's own
    durable facts inside this write transaction.
    """
    row = conn.execute(
        "SELECT merchant, receipt_datetime, source_channel, currency "
        "FROM receipts WHERE public_id = ?",
        (identity.receipt_public_id,),
    ).fetchone()
    if row is None:
        raise FinalizationAuthorizationError(
            f"Receipt {identity.receipt_public_id!r} bound by the authorized snapshot no "
            "longer exists",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )
    live_datetime = row["receipt_datetime"]
    live = (
        None if row["merchant"] is None else str(row["merchant"]),
        None if live_datetime is None else str(live_datetime)[:10],
        None if row["source_channel"] is None else str(row["source_channel"]),
        None if row["currency"] is None else str(row["currency"]),
    )
    expected = (
        identity.merchant,
        identity.receipt_date,
        identity.source_channel,
        identity.currency,
    )
    if live != expected:
        raise FinalizationAuthorizationError(
            f"Receipt {identity.receipt_public_id!r} facts {live!r} no longer match the "
            f"identity hash-bound into the authorized snapshot {expected!r}",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )


def _materialize_receipt_group(
    conn: sqlite3.Connection,
    *,
    receipt_group_public_id: str,
    currency: str,
    spec: ReceiptGroupMaterialization,
    binding: ActiveFactSetBinding | None,
) -> None:
    """Create or fully verify the deterministic single-receipt group.

    Runs inside the finalizer's own ``BEGIN IMMEDIATE`` Unit of Work, so the
    group and its single membership row commit or roll back with every other
    finalization write.  No ``INSERT OR IGNORE``: an already existing group or
    membership row is compared field by field and is never silently adopted, so
    a foreign, multi-receipt, wrong-currency, wrong-status, or wrong-source
    group fails closed with a typed reason instead of being reused.
    """
    if binding is not None and spec.receipt_public_id != binding.receipt_public_id:
        raise ReceiptGroupMaterializationError(
            f"Group materialization targets receipt {spec.receipt_public_id!r} but the "
            f"authorized fact set binds {binding.receipt_public_id!r}",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )
    receipt_row = conn.execute(
        "SELECT id, currency FROM receipts WHERE public_id = ?",
        (spec.receipt_public_id,),
    ).fetchone()
    if receipt_row is None:
        raise ReceiptGroupMaterializationError(
            f"Receipt {spec.receipt_public_id!r} not found for finalization grouping",
            reason=FinalizationBlockReason.RECEIPT_IDENTITY_MISMATCH.value,
        )
    receipt_id = int(receipt_row["id"])

    group_row = conn.execute(
        "SELECT id, group_type, currency, status, source FROM receipt_groups WHERE public_id = ?",
        (receipt_group_public_id,),
    ).fetchone()
    if group_row is None:
        conn.execute(
            """
            INSERT INTO receipt_groups (
                public_id, group_type, currency, status, source
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (receipt_group_public_id, spec.group_type, currency, spec.status, spec.source),
        )
        created = conn.execute(
            "SELECT id FROM receipt_groups WHERE public_id = ?",
            (receipt_group_public_id,),
        ).fetchone()
        if created is None:
            raise ReceiptGroupMaterializationError(
                f"Receipt group {receipt_group_public_id!r} is missing immediately after "
                "materialization",
                reason=FinalizationBlockReason.RECEIPT_GROUP_NOT_FOUND.value,
            )
        group_id = int(created["id"])
    else:
        durable = (
            str(group_row["group_type"]),
            str(group_row["currency"]),
            str(group_row["status"]),
            None if group_row["source"] is None else str(group_row["source"]),
        )
        expected = (spec.group_type, currency, spec.status, spec.source)
        if durable != expected:
            raise ReceiptGroupMaterializationError(
                f"Receipt group {receipt_group_public_id!r} already exists as {durable!r}, "
                f"which contradicts this finalization's group identity {expected!r}; "
                "an existing group is never silently adopted",
                reason=FinalizationBlockReason.RECEIPT_GROUP_IDENTITY_CONFLICT.value,
            )
        group_id = int(group_row["id"])

    _materialize_group_membership(
        conn,
        receipt_group_public_id=receipt_group_public_id,
        group_id=group_id,
        receipt_id=receipt_id,
        spec=spec,
    )


def _materialize_group_membership(
    conn: sqlite3.Connection,
    *,
    receipt_group_public_id: str,
    group_id: int,
    receipt_id: int,
    spec: ReceiptGroupMaterialization,
) -> None:
    members = conn.execute(
        "SELECT public_id, receipt_id, sequence_number FROM receipt_group_receipts "
        "WHERE receipt_group_id = ? ORDER BY id",
        (group_id,),
    ).fetchall()
    if len(members) > 1:
        raise ReceiptGroupMaterializationError(
            f"Receipt group {receipt_group_public_id!r} already holds {len(members)} receipts; "
            "a single-receipt finalization may not adopt a multi-receipt group",
            reason=FinalizationBlockReason.RECEIPT_GROUP_MEMBERSHIP_CONFLICT.value,
        )
    if members:
        member = members[0]
        if int(member["receipt_id"]) != receipt_id:
            raise ReceiptGroupMaterializationError(
                f"Receipt group {receipt_group_public_id!r} already holds a different receipt; "
                "adopting a foreign receipt's group is refused",
                reason=FinalizationBlockReason.RECEIPT_GROUP_FOREIGN_RECEIPT.value,
            )
        durable_member = (
            str(member["public_id"]),
            None if member["sequence_number"] is None else int(member["sequence_number"]),
        )
        if durable_member != (spec.receipt_group_receipt_public_id, 1):
            raise ReceiptGroupMaterializationError(
                f"Receipt group {receipt_group_public_id!r} membership {durable_member!r} "
                f"contradicts the expected membership "
                f"{(spec.receipt_group_receipt_public_id, 1)!r}",
                reason=FinalizationBlockReason.RECEIPT_GROUP_MEMBERSHIP_CONFLICT.value,
            )
    else:
        collision = conn.execute(
            "SELECT receipt_group_id FROM receipt_group_receipts WHERE public_id = ?",
            (spec.receipt_group_receipt_public_id,),
        ).fetchone()
        if collision is not None:
            raise ReceiptGroupMaterializationError(
                f"Membership public ID {spec.receipt_group_receipt_public_id!r} is already used "
                "by an unrelated receipt group",
                reason=FinalizationBlockReason.RECEIPT_GROUP_MEMBERSHIP_CONFLICT.value,
            )
        conn.execute(
            """
            INSERT INTO receipt_group_receipts (
                public_id, receipt_group_id, receipt_id, sequence_number
            ) VALUES (?, ?, ?, 1)
            """,
            (spec.receipt_group_receipt_public_id, group_id, receipt_id),
        )

    other_groups = conn.execute(
        "SELECT COUNT(*) AS n FROM receipt_group_receipts "
        "WHERE receipt_id = ? AND receipt_group_id <> ?",
        (receipt_id, group_id),
    ).fetchone()
    if other_groups is not None and int(other_groups["n"]) > 0:
        raise ReceiptGroupMaterializationError(
            f"Receipt {spec.receipt_public_id!r} is already bound to another receipt group; "
            "a second group-scoped finalization path is refused",
            reason=FinalizationBlockReason.RECEIPT_GROUP_MEMBERSHIP_CONFLICT.value,
        )


def _resolve_total_to_collect(
    fin_input: FinalizationInput,
    *,
    total_paid: Decimal,
) -> Decimal:
    """Resolve the payer's collection total and prove it against the obligations.

    The calculator publishes ``total_to_collect`` (the sum of the obligations
    owed to the primary payer).  A missing key is derived from total paid minus
    the payer's own share; an explicitly null value is corrupt material and is
    refused rather than silently treated as zero.  The resolved value is then
    proved equal to the sum of the settlement obligations this finalization is
    about to persist, so the audit total can never diverge from the calculator
    or from the obligations.
    """
    snapshot = fin_input.calculation_snapshot
    currency = fin_input.currency
    raw = snapshot.get("total_to_collect", _MISSING)
    if raw is None:
        raise FinalizationValidationError(
            "calculation_snapshot.total_to_collect is explicitly null; a collection total "
            "cannot be inferred from corrupt snapshot material"
        )
    if raw is _MISSING:
        payer_own = _snapshot_amount(snapshot, "payer_own_share", currency)
        resolved = quantize_for_currency(total_paid - payer_own, currency)
    else:
        resolved = money_decimal(raw, label="calculation_snapshot.total_to_collect")

    payer = fin_input.payer_participant_public_id
    obligations_total = quantize_for_currency(
        sum(
            (
                obligation.amount
                for obligation in fin_input.settlement_obligations
                if obligation.creditor_participant_public_id == payer
            ),
            Decimal("0"),
        ),
        currency,
    )
    if resolved != obligations_total:
        raise FinalizationValidationError(
            f"Collection total {canonical_money_str(resolved, currency)} does not equal the "
            f"{canonical_money_str(obligations_total, currency)} owed to payer {payer!r} by "
            "the settlement obligations being persisted"
        )
    return resolved


# ===================================================================
# Schema check
# ===================================================================


def _require_schema(conn: sqlite3.Connection) -> None:
    """Fail closed if required finalization/audit tables are missing."""
    missing: list[str] = []
    for table in sorted(_REQUIRED_TABLES):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None:
            missing.append(table)
    if missing:
        raise FinalizationPersistenceError(
            f"Required schema tables are missing: {', '.join(missing)}. "
            f"Migrations 020, 024, 025, and 039 must be applied "
            f"before the receipt finalization workflow runs."
        )


def _append_receipt_finalization_audits(
    conn: sqlite3.Connection,
    *,
    fin_input: FinalizationInput,
    previous_group_status: str,
    finalization_public_id: str,
    transaction_public_id: str,
    settlement_public_ids: list[str],
    created_at: str,
) -> None:
    actor_public_id = fin_input.actor_id or f"receipt-finalization:{fin_input.actor_type}"
    base = {
        "transaction_public_id": transaction_public_id,
        "settlement_public_ids": tuple(sorted(settlement_public_ids)),
        "calculation_run_public_id": fin_input.calculation_run_public_id,
    }
    before = {
        "receipt_group_status": previous_group_status,
        "transaction_public_id": None,
        "settlement_public_ids": (),
        "calculation_run_public_id": fin_input.calculation_run_public_id,
    }
    obligations_state = {"receipt_group_status": previous_group_status, **base}
    finalized_state = {"receipt_group_status": "settled", **base}
    common: dict[str, Any] = {
        "aggregate_type": "receipt_group",
        "aggregate_public_id": fin_input.receipt_group_public_id,
        "actor_type": fin_input.actor_type,
        "actor_public_id": actor_public_id,
        "authorization_public_id": fin_input.authorization_id,
        "calculation_snapshot_public_id": fin_input.calculation_snapshot_id,
        "calculation_snapshot_hash": fin_input.calculation_snapshot_hash,
        "source_evidence_references": fin_input.source_evidence_refs,
        "correlation_public_id": finalization_public_id,
        "causation_public_id": finalization_public_id,
        "created_at": created_at,
    }
    obligations_type = "settlement_obligations_created"
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=derive_audit_event_public_id(
                aggregate_type="receipt_group",
                aggregate_public_id=fin_input.receipt_group_public_id,
                event_type=obligations_type,
                causation_public_id=finalization_public_id,
            ),
            event_type=obligations_type,
            event_payload={
                **base,
                "obligations_created": len(settlement_public_ids),
            },
            previous_state=before,
            new_state=obligations_state,
            **common,
        ),
    )
    finalization_type = "receipt_finalized"
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=derive_audit_event_public_id(
                aggregate_type="receipt_group",
                aggregate_public_id=fin_input.receipt_group_public_id,
                event_type=finalization_type,
                causation_public_id=finalization_public_id,
            ),
            event_type=finalization_type,
            event_payload={
                **base,
                "authorization_id": fin_input.authorization_id,
                "confirmation_id": fin_input.confirmation_id,
            },
            previous_state=obligations_state,
            new_state=finalized_state,
            **common,
        ),
    )


# ===================================================================
# Authorization loading -- full-field validation
# ===================================================================


def _load_and_validate_authorization(
    conn: sqlite3.Connection,
    authorization_id: str,
    fin_input: FinalizationInput,
    fingerprint: str,
) -> dict[str, Any]:
    """Load and fully validate the persisted authorization + confirmation chain.

    Every material persisted field is cross-checked against the request.
    The confirmation is loaded independently and validated against BOTH
    the authorization and the finalization request.
    """
    # --- Load authorization ---
    row = conn.execute(
        """
        SELECT authorization_id, receipt_group_public_id, calculation_run_public_id,
               calculation_snapshot_id, confirmation_id, content_hash,
               currency, final_total, payer_participant_public_id,
               participant_public_ids_json, settlement_obligations_json,
               source_evidence_refs_json, actor_type, actor_id,
               authorization_state, authorization_version
        FROM receipt_finalization_authorizations
        WHERE authorization_id = ?
        """,
        (authorization_id,),
    ).fetchone()

    if row is None:
        raise FinalizationAuthorizationError(
            f"Authorization {authorization_id!r} not found",
            reason=FinalizationBlockReason.AUTHORIZATION_MISSING,
        )

    auth = dict(row)

    # --- State must be 'authorized' ---
    if auth["authorization_state"] != "authorized":
        raise FinalizationAuthorizationError(
            f"Authorization {authorization_id!r} state is "
            f"{auth['authorization_state']!r}, not 'authorized'",
            reason=FinalizationBlockReason.AUTHORIZATION_STATE_DENIED,
        )

    # --- Content hash must match ---
    if auth["content_hash"] != fingerprint:
        raise FinalizationAuthorizationError(
            "Authorization content hash mismatch",
            reason=FinalizationBlockReason.AUTHORIZATION_CONTENT_MISMATCH,
        )

    # --- Version ---
    if not auth.get("authorization_version"):
        raise FinalizationAuthorizationError(
            f"Authorization {authorization_id!r} has missing or empty version",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        )

    # --- content_hash format ---
    content_hash_val = auth.get("content_hash", "")
    if len(content_hash_val) != 64:
        raise FinalizationAuthorizationError(
            f"Authorization {authorization_id!r} has malformed content_hash",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        )

    # --- Receipt group ---
    if auth["receipt_group_public_id"] != fin_input.receipt_group_public_id:
        raise FinalizationAuthorizationError(
            f"Authorization receipt group {auth['receipt_group_public_id']!r} "
            f"does not match request {fin_input.receipt_group_public_id!r}",
            reason=FinalizationBlockReason.AUTHORIZATION_CONTENT_MISMATCH,
        )

    # --- Calculation run ---
    if auth["calculation_run_public_id"] != fin_input.calculation_run_public_id:
        raise FinalizationAuthorizationError(
            f"Authorization calculation run {auth['calculation_run_public_id']!r} "
            f"does not match request {fin_input.calculation_run_public_id!r}",
            reason=FinalizationBlockReason.AUTHORIZATION_CONTENT_MISMATCH,
        )

    # --- Snapshot ID ---
    if auth["calculation_snapshot_id"] != fin_input.calculation_snapshot_id:
        raise FinalizationAuthorizationError(
            f"Authorization snapshot {auth['calculation_snapshot_id']!r} "
            f"does not match request {fin_input.calculation_snapshot_id!r}",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH,
        )

    # --- Confirmation ID ---
    if auth["confirmation_id"] != fin_input.confirmation_id:
        raise FinalizationAuthorizationError(
            f"Authorization confirmation {auth['confirmation_id']!r} "
            f"does not match request {fin_input.confirmation_id!r}",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )

    # --- Payer ---
    if auth["payer_participant_public_id"] != fin_input.payer_participant_public_id:
        raise FinalizationAuthorizationError(
            f"Authorization payer {auth['payer_participant_public_id']!r} "
            f"does not match request {fin_input.payer_participant_public_id!r}",
            reason=FinalizationBlockReason.PARTICIPANT_MISMATCH,
        )

    # --- Currency ---
    if auth["currency"] != fin_input.currency:
        raise FinalizationAuthorizationError(
            f"Authorization currency {auth['currency']!r} "
            f"does not match request {fin_input.currency!r}",
            reason=FinalizationBlockReason.CURRENCY_MISMATCH,
        )

    # --- Final total (canonical comparison) ---
    _validate_authorization_final_total(auth, fin_input)

    # --- Actor binding ---
    if auth["actor_type"] != fin_input.actor_type:
        raise FinalizationAuthorizationError(
            f"Authorization actor_type {auth['actor_type']!r} "
            f"does not match request {fin_input.actor_type!r}",
            reason=FinalizationBlockReason.ACTOR_MISMATCH,
        )
    if auth.get("actor_id") != fin_input.actor_id:
        raise FinalizationAuthorizationError(
            "Authorization actor_id does not match request",
            reason=FinalizationBlockReason.ACTOR_MISMATCH,
        )

    # --- Participant set ---
    _validate_authorization_participants(auth, fin_input)

    # --- Settlement obligations JSON ---
    _validate_authorization_obligations(auth, fin_input)

    # --- Source evidence refs ---
    _validate_authorization_evidence(auth, fin_input)

    # --- Load and fully validate confirmation ---
    _load_and_validate_confirmation(conn, auth, fin_input, fingerprint)

    return auth


def _validate_authorization_final_total(
    auth: dict[str, Any],
    fin_input: FinalizationInput,
) -> None:
    """Canonicalize and compare the authorization final_total against the request."""
    try:
        auth_total = money_decimal(auth["final_total"], label="authorization final_total")
    except MoneyValidationError as exc:
        raise FinalizationAuthorizationError(
            f"Authorization final_total is invalid: {exc}",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        ) from exc

    snapshot_total = fin_input.calculation_snapshot.get("total_paid")
    if snapshot_total is None:
        raise FinalizationAuthorizationError(
            "Cannot validate final_total: snapshot has no total_paid",
            reason=FinalizationBlockReason.AMOUNT_MISMATCH,
        )

    try:
        snapshot_dec = money_decimal(snapshot_total, label="snapshot total_paid")
    except MoneyValidationError as exc:
        raise FinalizationAuthorizationError(
            f"Snapshot total_paid is invalid: {exc}",
            reason=FinalizationBlockReason.AMOUNT_MISMATCH,
        ) from exc

    auth_canon = quantize_for_currency(auth_total, fin_input.currency)
    snap_canon = quantize_for_currency(snapshot_dec, fin_input.currency)

    if auth_canon != snap_canon:
        raise FinalizationAuthorizationError(
            f"Authorization final_total {auth_canon} does not match "
            f"snapshot total_paid {snap_canon}",
            reason=FinalizationBlockReason.AMOUNT_MISMATCH,
        )


def _validate_authorization_participants(
    auth: dict[str, Any],
    fin_input: FinalizationInput,
) -> None:
    """Validate participant set in authorization against the request.

    Uses the shared strict validator -- no permissive filtering, no silent
    coercion, no duplicate deduplication.
    """
    auth_participants = _parse_unique_nonempty_string_list(
        auth.get("participant_public_ids_json", "[]"),
        label="Authorization participant_public_ids_json",
        malformed_reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED.value,
        mismatch_reason=FinalizationBlockReason.PARTICIPANT_MISMATCH.value,
    )

    request_set = sorted(fin_input.participant_public_ids)
    if auth_participants != request_set:
        raise FinalizationAuthorizationError(
            f"Authorization participant set does not match request: "
            f"auth={auth_participants}, request={request_set}",
            reason=FinalizationBlockReason.PARTICIPANT_MISMATCH.value,
        )


def _validate_authorization_obligations(
    auth: dict[str, Any],
    fin_input: FinalizationInput,
) -> None:
    """Validate settlement obligations JSON in authorization against the request."""
    try:
        auth_obls = json.loads(auth.get("settlement_obligations_json", "[]"))
    except json.JSONDecodeError:
        raise FinalizationAuthorizationError(
            "Authorization settlement_obligations_json is malformed JSON",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        )

    if not isinstance(auth_obls, list):
        raise FinalizationAuthorizationError(
            "Authorization settlement_obligations_json is not a list",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        )

    # Canonicalize: (debtor, creditor, canonical amount str, currency)
    def _canon(obl: Any) -> tuple[str, str, str, str]:
        if not isinstance(obl, dict):
            raise FinalizationAuthorizationError(
                f"Authorization obligation is not a mapping: {obl!r}",
                reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
            )
        debtor = obl.get("debtor", "")
        creditor = obl.get("creditor", "")
        amount_raw = obl.get("amount", "0")
        currency = obl.get("currency", fin_input.currency)
        if not isinstance(debtor, str) or not debtor:
            raise FinalizationAuthorizationError(
                "Authorization obligation has invalid debtor",
                reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
            )
        if not isinstance(creditor, str) or not creditor:
            raise FinalizationAuthorizationError(
                "Authorization obligation has invalid creditor",
                reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
            )
        if not isinstance(currency, str) or not currency:
            raise FinalizationAuthorizationError(
                "Authorization obligation has invalid currency",
                reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
            )
        try:
            amount_dec = money_decimal(amount_raw, label="authorization obligation amount")
        except MoneyValidationError as exc:
            raise FinalizationAuthorizationError(
                f"Authorization obligation amount is invalid: {exc}",
                reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
            ) from exc
        can_amount = canonical_money_str(amount_dec, currency)
        return (debtor, creditor, can_amount, currency)

    try:
        auth_canon = sorted(_canon(o) for o in auth_obls)
    except FinalizationAuthorizationError:
        raise

    request_canon = sorted(
        (
            o.debtor_participant_public_id,
            o.creditor_participant_public_id,
            canonical_money_str(o.amount, o.currency),
            o.currency,
        )
        for o in fin_input.settlement_obligations
    )

    if auth_canon != request_canon:
        raise FinalizationAuthorizationError(
            "Authorization settlement obligations do not match request",
            reason=FinalizationBlockReason.OBLIGATION_MISMATCH,
        )


def _validate_authorization_evidence(
    auth: dict[str, Any],
    fin_input: FinalizationInput,
) -> None:
    """Validate source evidence refs in authorization against the request.

    Uses the shared strict validator -- no permissive coercion, no silent
    deduplication through set(), no str() casting.
    """
    auth_evidence = _parse_unique_nonempty_string_list(
        auth.get("source_evidence_refs_json", "[]"),
        label="Authorization source_evidence_refs_json",
        malformed_reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED.value,
        mismatch_reason=FinalizationBlockReason.EVIDENCE_MISMATCH.value,
    )

    request_evidence = sorted(fin_input.source_evidence_refs)
    if auth_evidence != request_evidence:
        raise FinalizationAuthorizationError(
            "Authorization evidence refs do not match request",
            reason=FinalizationBlockReason.EVIDENCE_MISMATCH.value,
        )


def _load_and_validate_confirmation(
    conn: sqlite3.Connection,
    auth: dict[str, Any],
    fin_input: FinalizationInput,
    fingerprint: str,
) -> None:
    """Load the complete confirmation record and validate every material field.

    The confirmation must match BOTH the persisted authorization AND the
    finalization request.  Confirmation state alone is not sufficient.
    """
    confirmation_id = auth.get("confirmation_id")
    if not confirmation_id:
        raise FinalizationAuthorizationError(
            "Authorization references no confirmation",
            reason=FinalizationBlockReason.CONFIRMATION_MISSING,
        )

    row = conn.execute(
        """
        SELECT confirmation_id, receipt_group_public_id, calculation_run_public_id,
               calculation_snapshot_id, content_hash, currency, final_total,
               payer_participant_public_id, participant_public_ids_json,
               settlement_obligations_json, actor_type, actor_id,
               confirmation_state
        FROM receipt_finalization_confirmations
        WHERE confirmation_id = ?
        """,
        (confirmation_id,),
    ).fetchone()

    if row is None:
        raise FinalizationAuthorizationError(
            f"Confirmation {confirmation_id!r} not found",
            reason=FinalizationBlockReason.CONFIRMATION_MISSING,
        )

    conf = dict(row)

    # --- Confirmation state ---
    if conf["confirmation_state"] != "confirmed":
        raise FinalizationAuthorizationError(
            f"Confirmation {confirmation_id!r} state is "
            f"{conf['confirmation_state']!r}, not 'confirmed'",
            reason=FinalizationBlockReason.CONFIRMATION_STATE_DENIED,
        )

    # --- Content hash must match authorization AND request fingerprint ---
    if conf["content_hash"] != fingerprint:
        raise FinalizationAuthorizationError(
            "Confirmation content hash does not match request fingerprint",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )
    if conf["content_hash"] != auth["content_hash"]:
        raise FinalizationAuthorizationError(
            "Confirmation content hash does not match authorization content hash",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )

    # --- Receipt group must match authorization AND request ---
    if conf["receipt_group_public_id"] != auth["receipt_group_public_id"]:
        raise FinalizationAuthorizationError(
            "Confirmation receipt group does not match authorization",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )
    if conf["receipt_group_public_id"] != fin_input.receipt_group_public_id:
        raise FinalizationAuthorizationError(
            "Confirmation receipt group does not match request",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )

    # --- Calculation run ---
    if conf["calculation_run_public_id"] != auth["calculation_run_public_id"]:
        raise FinalizationAuthorizationError(
            "Confirmation calculation run does not match authorization",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )

    # --- Snapshot ID ---
    if conf["calculation_snapshot_id"] != auth["calculation_snapshot_id"]:
        raise FinalizationAuthorizationError(
            "Confirmation snapshot ID does not match authorization",
            reason=FinalizationBlockReason.CALCULATION_SNAPSHOT_MISMATCH,
        )

    # --- Currency ---
    if conf["currency"] != auth["currency"]:
        raise FinalizationAuthorizationError(
            "Confirmation currency does not match authorization",
            reason=FinalizationBlockReason.CONFIRMATION_CONTENT_MISMATCH,
        )

    # --- Final total ---
    try:
        conf_total = money_decimal(conf["final_total"], label="confirmation final_total")
        auth_total = money_decimal(auth["final_total"], label="authorization final_total")
    except MoneyValidationError as exc:
        raise FinalizationAuthorizationError(
            f"Invalid total in confirmation/authorization: {exc}",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        ) from exc
    if quantize_for_currency(conf_total, fin_input.currency) != quantize_for_currency(
        auth_total, fin_input.currency
    ):
        raise FinalizationAuthorizationError(
            "Confirmation final_total does not match authorization",
            reason=FinalizationBlockReason.AMOUNT_MISMATCH,
        )

    # --- Payer ---
    if conf["payer_participant_public_id"] != auth["payer_participant_public_id"]:
        raise FinalizationAuthorizationError(
            "Confirmation payer does not match authorization",
            reason=FinalizationBlockReason.PARTICIPANT_MISMATCH,
        )

    # --- Participant set (strict, shared validator) ---
    conf_parts = _parse_unique_nonempty_string_list(
        conf.get("participant_public_ids_json", "[]"),
        label="Confirmation participant_public_ids_json",
        malformed_reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED.value,
        mismatch_reason=FinalizationBlockReason.PARTICIPANT_MISMATCH.value,
    )
    auth_parts = _parse_unique_nonempty_string_list(
        auth.get("participant_public_ids_json", "[]"),
        label="Authorization participant_public_ids_json (re-parsed)",
        malformed_reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED.value,
        mismatch_reason=FinalizationBlockReason.PARTICIPANT_MISMATCH.value,
    )
    if conf_parts != auth_parts:
        raise FinalizationAuthorizationError(
            "Confirmation participant set does not match authorization",
            reason=FinalizationBlockReason.PARTICIPANT_MISMATCH.value,
        )
    # Also enforce that payer is in the confirmation participant set.
    payer_pid = fin_input.payer_participant_public_id
    if payer_pid not in conf_parts:
        raise FinalizationAuthorizationError(
            f"Confirmation participant set does not include payer {payer_pid!r}",
            reason=FinalizationBlockReason.PARTICIPANT_MISMATCH.value,
        )

    # --- Settlement obligations ---
    try:
        conf_obls = json.loads(conf.get("settlement_obligations_json", "[]"))
        auth_obls = json.loads(auth.get("settlement_obligations_json", "[]"))
    except json.JSONDecodeError:
        raise FinalizationAuthorizationError(
            "Settlement obligations JSON is malformed",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        )
    if not isinstance(conf_obls, list) or not isinstance(auth_obls, list):
        raise FinalizationAuthorizationError(
            "Settlement obligations JSON is not a list",
            reason=FinalizationBlockReason.AUTHORIZATION_MALFORMED,
        )
    conf_obls_canon = sorted(json.dumps(o, sort_keys=True) for o in conf_obls)
    auth_obls_canon = sorted(json.dumps(o, sort_keys=True) for o in auth_obls)
    if conf_obls_canon != auth_obls_canon:
        raise FinalizationAuthorizationError(
            "Confirmation settlement obligations do not match authorization",
            reason=FinalizationBlockReason.OBLIGATION_MISMATCH,
        )

    # --- Actor binding: confirmation actor must match authorization actor ---
    if conf["actor_type"] != auth["actor_type"]:
        raise FinalizationAuthorizationError(
            "Confirmation actor_type does not match authorization actor_type",
            reason=FinalizationBlockReason.CONFIRMATION_ACTOR_MISMATCH,
        )
    if conf.get("actor_id") != auth.get("actor_id"):
        raise FinalizationAuthorizationError(
            "Confirmation actor_id does not match authorization actor_id",
            reason=FinalizationBlockReason.CONFIRMATION_ACTOR_MISMATCH,
        )


# ===================================================================
# Idempotency
# ===================================================================


def _check_idempotency(
    conn: sqlite3.Connection,
    idempotency_key: str,
    fingerprint: str,
) -> FinalizationOutput | None:
    """Check the idempotency table for a previous attempt.

    Returns a ``FinalizationOutput`` for idempotent replays, raises
    ``FinalizationIdempotencyError`` for conflicts, or returns ``None``
    if this is a first attempt.
    """
    row = conn.execute(
        """
        SELECT idempotency_key, content_fingerprint, status, finalization_audit_id
        FROM receipt_finalization_idempotency
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()

    if row is None:
        return None  # First attempt.

    # Use indexed access on sqlite3.Row (not .get)
    stored_fingerprint = row["content_fingerprint"]
    stored_status = row["status"]
    stored_audit_id = row["finalization_audit_id"]

    if stored_fingerprint == fingerprint:
        # Same key, same content -- idempotent replay.
        if stored_status in ("finalized", "already_finalized"):
            if stored_audit_id is None:
                raise FinalizationIdempotencyError(
                    f"Idempotency record for key {idempotency_key!r} has no audit link "
                    f"-- database integrity violation",
                    reason=FinalizationBlockReason.DANGLING_AUDIT_REFERENCE,
                )
            return _build_replay_output(conn, stored_audit_id, idempotency_key)
        # If the prior attempt was blocked/failed, allow retry.
        return None

    # Same key, different content -- conflict.
    raise FinalizationIdempotencyError(
        f"Idempotency conflict: key {idempotency_key!r} already used with different content",
        reason=FinalizationBlockReason.CONTENT_CONFLICT,
    )


def _check_existing_finalization(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
    fingerprint: str,
) -> FinalizationOutput | None:
    """Check if the receipt group is already finalized (different idempotency key)."""
    row = conn.execute(
        """
        SELECT finalization_id, content_fingerprint, status
        FROM receipt_finalization_audit
        WHERE receipt_group_public_id = ? AND status = 'finalized'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (receipt_group_public_id,),
    ).fetchone()

    if row is None:
        return None  # Not yet finalized.

    existing_finalization_id = row["finalization_id"]
    existing_fingerprint = row["content_fingerprint"]

    if existing_fingerprint == fingerprint:
        return _build_replay_output(conn, existing_finalization_id, None)

    raise FinalizationIdempotencyError(
        f"Receipt group {receipt_group_public_id!r} is already finalized with different content",
        reason=FinalizationBlockReason.CONTENT_CONFLICT,
    )


def _build_replay_output(
    conn: sqlite3.Connection,
    audit_id: str | None,
    idempotency_key: str | None,
) -> FinalizationOutput:
    """Reconstruct a ``FinalizationOutput`` from the audit record.

    Uses dict() to safely convert sqlite3.Row for .get() access.
    """
    if audit_id is None:
        # Missing audit_id on an expected successful replay: fail closed.
        raise FinalizationIdempotencyError(
            "Cannot replay finalization: audit_id is None on a finalized idempotency record",
            reason=FinalizationBlockReason.DANGLING_AUDIT_REFERENCE,
        )

    row = conn.execute(
        """
        SELECT finalization_id, calculation_run_public_id,
               transaction_public_id, settlement_public_ids_json,
               idempotency_key
        FROM receipt_finalization_audit
        WHERE finalization_id = ?
        """,
        (audit_id,),
    ).fetchone()

    if row is None:
        # Idempotency record points to a nonexistent audit row.
        raise FinalizationIdempotencyError(
            f"Idempotency record references nonexistent audit {audit_id!r}",
            reason=FinalizationBlockReason.DANGLING_AUDIT_REFERENCE,
        )

    # Convert sqlite3.Row to dict for safe .get() access.
    audit = dict(row)

    sp_ids = _parse_unique_nonempty_string_list(
        audit.get("settlement_public_ids_json") or "[]",
        label="Finalization audit settlement_public_ids_json",
        malformed_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        mismatch_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
    )

    return FinalizationOutput(
        finalization_public_id=audit["finalization_id"],
        calculation_run_public_id=audit["calculation_run_public_id"],
        obligations_created=len(sp_ids),
        settlement_public_ids=sp_ids,
        status=FinalizationStatus.ALREADY_FINALIZED.value,
        transaction_public_id=audit.get("transaction_public_id"),
        audit_id=audit["finalization_id"],
        idempotency_key=idempotency_key or audit.get("idempotency_key", ""),
    )


def _require_complete_replay_truth(
    conn: sqlite3.Connection,
    *,
    audit_id: str,
    fin_input: FinalizationInput,
    fingerprint: str,
) -> None:
    """Fail closed unless the complete durable financial graph is intact for replay.

    An idempotent replay must prove the complete durable truth still matches
    this exact request -- not merely that an audit row with the right key
    exists.  Every replay entry path (same idempotency key, and an
    already-finalized group reached under another valid key) calls this one
    verifier.  It walks the durable graph -- idempotency record, finalization
    audit, consumed authorization, confirmation, historical calculation run,
    authoritative snapshot, fact-set binding evidence, confirmed receipt
    identity, receipt group and its exact receipt membership, receipt-scoped
    participant membership, canonical transaction, finalizer calculation run,
    participant shares, settlement obligations, and the financial audit-chain
    events -- verifying existence, exact identity and foreign-key targets,
    cardinality, receipt scope, content hashes, canonical amounts, payer and
    inclusion semantics, and lifecycle state.  A valid hash chain is never
    sufficient when semantic fields or graph edges have been forged: any
    parse failure, missing row, extra row, forged ID, or drift raises a typed
    integrity error rather than reporting success.  Strictly read-only.
    """
    # Graph roots: the authoritative snapshot and its snapshot-bound authority
    # (active fact-set four-tuple, confirmed receipt identity, durable binding
    # evidence, and the historical calculation run including created_at).
    _require_authoritative_snapshot_binding(conn, fin_input)
    authority = _require_snapshot_bound_authority(conn, fin_input)

    audit_row = conn.execute(
        "SELECT finalization_id, idempotency_key, content_fingerprint, authorization_id, "
        "confirmation_id, receipt_group_public_id, calculation_run_public_id, "
        "calculation_snapshot_id, transaction_public_id, settlement_public_ids_json, "
        "currency, total_paid, total_to_collect, payer_participant_public_id, "
        "actor_type, actor_id, status "
        "FROM receipt_finalization_audit WHERE finalization_id = ?",
        (audit_id,),
    ).fetchone()
    if audit_row is None:
        raise FinalizationIdempotencyError(
            f"Replay references nonexistent finalization audit {audit_id!r}",
            reason=FinalizationBlockReason.DANGLING_AUDIT_REFERENCE,
        )
    audit = dict(audit_row)

    if str(audit["content_fingerprint"]) != fingerprint:
        raise FinalizationIdempotencyError(
            "Replay content fingerprint does not match the durable finalization audit",
            reason=FinalizationBlockReason.CONTENT_CONFLICT,
        )
    # R3-02: the audit's idempotency key is a graph edge, not a request field.
    # The durable idempotency record carrying that key must exist, reference
    # exactly this audit row, and carry this fingerprint.  When the request
    # arrived under the same key this also binds the request; when the replay
    # was reached through the already-finalized path, the original key's
    # record is still the durable truth being verified.
    idem_row = conn.execute(
        "SELECT idempotency_key, content_fingerprint, status, finalization_audit_id "
        "FROM receipt_finalization_idempotency WHERE idempotency_key = ?",
        (str(audit["idempotency_key"]),),
    ).fetchone()
    if idem_row is None:
        raise FinalizationIdempotencyError(
            "Replay finalization audit has no durable idempotency record for its key",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if (
        str(idem_row["finalization_audit_id"] or "") != audit_id
        or str(idem_row["content_fingerprint"]) != fingerprint
        or str(idem_row["status"]) not in ("finalized", "already_finalized")
    ):
        raise FinalizationIdempotencyError(
            "Replay idempotency record does not match the durable finalization audit",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    audit_material = (
        str(audit["authorization_id"]),
        None if audit["confirmation_id"] is None else str(audit["confirmation_id"]),
        str(audit["receipt_group_public_id"]),
        str(audit["calculation_run_public_id"]),
        None if audit["calculation_snapshot_id"] is None else str(audit["calculation_snapshot_id"]),
        str(audit["currency"]),
        str(audit["payer_participant_public_id"]),
        str(audit["actor_type"]),
        None if audit["actor_id"] is None else str(audit["actor_id"]),
        str(audit["status"]),
    )
    expected_material = (
        fin_input.authorization_id,
        fin_input.confirmation_id or None,
        fin_input.receipt_group_public_id,
        fin_input.calculation_run_public_id,
        fin_input.calculation_snapshot_id or None,
        fin_input.currency,
        fin_input.payer_participant_public_id,
        fin_input.actor_type,
        fin_input.actor_id,
        FinalizationStatus.FINALIZED.value,
    )
    if audit_material != expected_material:
        raise FinalizationIdempotencyError(
            "Replay finalization audit material does not match the request",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )

    settlement_ids = _parse_unique_nonempty_string_list(
        audit.get("settlement_public_ids_json") or "[]",
        label="Replay finalization audit settlement_public_ids_json",
        malformed_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        mismatch_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
    )
    # R3-02: independently verify total_paid from snapshot and total_to_collect
    # from settlement obligations, not just trusting the audit's own claim.
    snapshot_total = fin_input.calculation_snapshot.get("total_paid")
    if snapshot_total is not None:
        expected_paid = quantize_for_currency(
            money_decimal(str(snapshot_total), label="replay snapshot total_paid"),
            fin_input.currency,
        )
        audit_paid = quantize_for_currency(
            money_decimal(str(audit["total_paid"]), label="replay audit total_paid"),
            fin_input.currency,
        )
        if audit_paid != expected_paid:
            raise FinalizationIdempotencyError(
                "Replay audit total_paid does not match the snapshot-derived value",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
    # total_to_collect: sum of all settlement obligation amounts.
    expected_collect = sum(
        quantize_for_currency(o.amount, o.currency) for o in fin_input.settlement_obligations
    )
    audit_collect = quantize_for_currency(
        money_decimal(str(audit["total_to_collect"]), label="replay audit total_to_collect"),
        fin_input.currency,
    )
    if audit_collect != expected_collect:
        raise FinalizationIdempotencyError(
            "Replay audit total_to_collect does not match the settlement obligations sum",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # FIX-01: re-verify the complete authorization/confirmation durable truth on
    # replay.  The authorization may have drifted after the first finalization.
    _require_replay_authorization_truth(conn, fin_input, fingerprint)
    # FIX-02: finalization-audit binding evidence must still match the four-tuple.
    _require_replay_audit_binding_evidence(conn, audit_id, fin_input)
    # Receipt group node: existence, identity, currency, settled state, and
    # the exact receipt membership of the group.
    group_id = _require_replay_receipt_group_truth(conn, fin_input, authority)
    # Receipt-scoped participant membership, including durable membership
    # evidence (migration 039) when present.
    _require_replay_membership_truth(
        conn,
        audit_id=audit_id,
        fin_input=fin_input,
        authority=authority,
        group_id=group_id,
    )
    _require_replay_canonical_transaction(conn, audit, fin_input)
    _require_replay_calculation_run_row(conn, fin_input, group_id)
    _require_replay_settlement_rows(conn, settlement_ids, fin_input)
    _require_replay_participant_shares(conn, fin_input)
    # Confirmed receipt identity: the live receipt row must still equal the
    # identity hash-bound into the authorized snapshot.
    receipt_identity = (
        authority.confirmed_receipt_identity
        if authority is not None
        else fin_input.confirmed_receipt_identity
    )
    if receipt_identity is not None:
        _require_live_receipt_identity(conn, receipt_identity)
    # Financial audit-chain events appended by the original finalization.
    _require_replay_financial_audit_events(conn, audit_id, fin_input)


def _require_replay_receipt_group_truth(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    authority: SnapshotBoundAuthority | None,
) -> int:
    """Verify the settled receipt group and its exact receipt membership.

    A finalized group must still exist with the finalized currency and the
    consumed 'settled' state.  For IAF finalizations the group must hold
    exactly the snapshot-bound receipt: a missing link, a retargeted link, or
    an extra forged receipt in the group is corruption, never noise.
    """
    group_row = conn.execute(
        "SELECT id, currency, status FROM receipt_groups WHERE public_id = ?",
        (fin_input.receipt_group_public_id,),
    ).fetchone()
    if group_row is None:
        raise FinalizationIdempotencyError(
            f"Replay receipt group {fin_input.receipt_group_public_id!r} is missing",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if str(group_row["currency"]) != fin_input.currency:
        raise FinalizationIdempotencyError(
            "Replay receipt group currency drifted from the recorded finalization",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if str(group_row["status"]) != "settled":
        raise FinalizationIdempotencyError(
            f"Replay receipt group status is {str(group_row['status'])!r}, not 'settled'",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    group_id = int(group_row["id"])
    if authority is not None:
        member_rows = conn.execute(
            "SELECT r.public_id FROM receipt_group_receipts rgr "
            "JOIN receipts r ON r.id = rgr.receipt_id "
            "WHERE rgr.receipt_group_id = ? ORDER BY r.public_id",
            (group_id,),
        ).fetchall()
        members = [str(row["public_id"]) for row in member_rows]
        expected_receipt = authority.active_fact_set_binding.receipt_public_id
        if members != [expected_receipt]:
            raise FinalizationIdempotencyError(
                f"Replay receipt group membership {members!r} does not equal the "
                f"single snapshot-bound receipt {[expected_receipt]!r}",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
    return group_id


def _require_replay_membership_truth(
    conn: sqlite3.Connection,
    *,
    audit_id: str,
    fin_input: FinalizationInput,
    authority: SnapshotBoundAuthority | None,
    group_id: int,
) -> None:
    """Verify receipt-scoped participant membership on replay (R3-07-1).

    Live membership is re-resolved with the exact receipt scope the
    finalization consumed: the snapshot-bound receipt for IAF, uncontradicted
    group-scoped membership for legacy finalizations.  When migration 039
    membership evidence exists for this finalization it must agree row for
    row with both the request (participant set and receipt scope) and the live
    rows (role and inclusion).  Absence of evidence means the finalization
    predates migration 039, or that the append-only evidence was removed by
    out-of-band DDL tampering; live receipt-scoped verification is the
    compensating control in both cases, and making absence falsifiable is a
    recorded follow-up.
    """
    bound_receipt = (
        authority.active_fact_set_binding.receipt_public_id if authority is not None else None
    )
    try:
        live = _lookup_participants_by_public_id(
            conn, fin_input, group_id, bound_receipt_public_id=bound_receipt
        )
    except IneligibleForFinalizationError as exc:
        raise FinalizationIdempotencyError(
            f"Replay receipt-scoped membership verification failed: {exc}",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        ) from exc

    evidence_rows = conn.execute(
        "SELECT membership_scope, receipt_public_id, receipt_group_public_id, "
        "participant_public_id, role, is_included "
        "FROM receipt_finalization_membership_evidence WHERE finalization_id = ?",
        (audit_id,),
    ).fetchall()
    if not evidence_rows:
        # Pre-039 finalization: no durable membership evidence exists.
        return

    expected_scope = "receipt" if bound_receipt is not None else "receipt_group"
    seen: set[str] = set()
    for row in evidence_rows:
        participant = str(row["participant_public_id"])
        if participant in seen:
            raise FinalizationIdempotencyError(
                f"Replay membership evidence duplicates participant {participant!r}",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        seen.add(participant)
        if str(row["receipt_group_public_id"]) != fin_input.receipt_group_public_id:
            raise FinalizationIdempotencyError(
                f"Replay membership evidence for {participant!r} targets a foreign group",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        if str(row["membership_scope"]) != expected_scope:
            raise FinalizationIdempotencyError(
                f"Replay membership evidence scope for {participant!r} is "
                f"{str(row['membership_scope'])!r}, expected {expected_scope!r}",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        evidence_receipt = row["receipt_public_id"]
        if bound_receipt is not None and str(evidence_receipt or "") != bound_receipt:
            raise FinalizationIdempotencyError(
                f"Replay membership evidence for {participant!r} is bound to receipt "
                f"{str(evidence_receipt)!r}, not the snapshot-bound receipt "
                f"{bound_receipt!r}",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        live_info = live.get(participant)
        if live_info is None:
            raise FinalizationIdempotencyError(
                f"Replay membership evidence references {participant!r}, which is no "
                "longer a resolvable member",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        if str(row["role"]) != str(live_info["role"]) or int(row["is_included"]) != int(
            live_info["is_included"]
        ):
            raise FinalizationIdempotencyError(
                f"Replay membership for {participant!r} drifted from the durable "
                "membership evidence",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
    if seen != set(live.keys()):
        raise FinalizationIdempotencyError(
            "Replay membership evidence participant set does not match the referenced participants",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


def _require_replay_calculation_run_row(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    group_id: int,
) -> None:
    """Verify the finalizer-written calculation run row on replay."""
    row = conn.execute(
        "SELECT receipt_group_id, currency, calculation_status FROM calculation_runs "
        "WHERE public_id = ?",
        (fin_input.calculation_run_public_id,),
    ).fetchone()
    if row is None:
        raise FinalizationIdempotencyError(
            f"Replay calculation run {fin_input.calculation_run_public_id!r} is missing",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if (
        (row["receipt_group_id"] is None or int(row["receipt_group_id"]) != group_id)
        or str(row["currency"]) != fin_input.currency
        or str(row["calculation_status"]) != "completed"
    ):
        raise FinalizationIdempotencyError(
            "Replay calculation run drifted from the recorded finalization",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


def _require_replay_financial_audit_events(
    conn: sqlite3.Connection,
    audit_id: str,
    fin_input: FinalizationInput,
) -> None:
    """Verify the append-only audit-chain events of the original finalization.

    Existence of the two event types is not sufficient: each event's public ID
    is deterministically derived from its aggregate, type, and causation, so a
    retargeted or fabricated event is detectable.  The finalizer is the only
    writer of events under this correlation for this receipt group, so the set
    scoped to that aggregate must be exactly the two events it appended -- an
    extra event there is forged material, never noise.  Events recorded under
    other aggregates are outside this node and are covered by the audit
    chain's own append-only hash chain.
    """
    rows = conn.execute(
        "SELECT event_type, event_public_id, aggregate_type FROM financial_audit_events "
        "WHERE correlation_public_id = ? AND aggregate_public_id = ?",
        (audit_id, fin_input.receipt_group_public_id),
    ).fetchall()
    durable = {
        (str(row["aggregate_type"]), str(row["event_type"]), str(row["event_public_id"]))
        for row in rows
    }
    expected = {
        (
            "receipt_group",
            event_type,
            derive_audit_event_public_id(
                aggregate_type="receipt_group",
                aggregate_public_id=fin_input.receipt_group_public_id,
                event_type=event_type,
                causation_public_id=audit_id,
            ),
        )
        for event_type in ("settlement_obligations_created", "receipt_finalized")
    }
    if len(durable) != len(rows) or durable != expected:
        raise FinalizationIdempotencyError(
            "Replay financial audit-chain events do not match the two derived "
            "finalization event identities of the recorded finalization",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


def _require_replay_canonical_transaction(
    conn: sqlite3.Connection,
    audit: dict[str, Any],
    fin_input: FinalizationInput,
) -> None:
    txn_public_id = audit["transaction_public_id"]
    if not txn_public_id:
        raise FinalizationIdempotencyError(
            "Replay finalization audit has no canonical transaction reference",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    row = conn.execute(
        "SELECT t.amount, t.currency, t.merchant, t.source_channel, t.transaction_date, "
        "t.status, t.intent, t.intent_type, p.public_id AS payer_public_id "
        "FROM transactions t "
        "JOIN participants p ON p.id = t.paid_by_participant_id "
        "WHERE t.public_id = ?",
        (txn_public_id,),
    ).fetchone()
    if row is None:
        raise FinalizationIdempotencyError(
            f"Replay canonical transaction {str(txn_public_id)!r} is missing",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    expected_total = quantize_for_currency(
        money_decimal(audit["total_paid"], label="replay audit total_paid"), fin_input.currency
    )
    durable_total = quantize_for_currency(
        money_decimal(str(row["amount"]), label="replay transaction amount"), fin_input.currency
    )
    if (
        durable_total != expected_total
        or str(row["currency"]) != fin_input.currency
        or str(row["payer_public_id"]) != fin_input.payer_participant_public_id
    ):
        raise FinalizationIdempotencyError(
            "Replay canonical transaction drifted from the recorded finalization",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # FIX-02: verify merchant, source_channel, date, status, intent.
    identity = fin_input.confirmed_receipt_identity
    if identity is not None:
        if str(row["merchant"] or "") != identity.merchant:
            raise FinalizationIdempotencyError(
                "Replay canonical transaction merchant drifted",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        if str(row["source_channel"] or "") != identity.source_channel:
            raise FinalizationIdempotencyError(
                "Replay canonical transaction source_channel drifted",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        durable_date = str(row["transaction_date"] or "")[:10]
        if durable_date != identity.receipt_date:
            raise FinalizationIdempotencyError(
                "Replay canonical transaction date drifted",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
    if str(row["status"] or "") != "active":
        raise FinalizationIdempotencyError(
            "Replay canonical transaction status is not 'active'",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # R3-03: verify intent and intent_type match the expected canonical values.
    expected_intent = "receipt_finalization"
    expected_intent_type = "Generated"
    if str(row["intent"] or "") != expected_intent:
        raise FinalizationIdempotencyError(
            f"Replay canonical transaction intent is {row['intent']!r}, "
            f"expected {expected_intent!r}",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if str(row["intent_type"] or "") != expected_intent_type:
        raise FinalizationIdempotencyError(
            f"Replay canonical transaction intent_type is {row['intent_type']!r}, "
            f"expected {expected_intent_type!r}",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


def _require_replay_settlement_rows(
    conn: sqlite3.Connection,
    settlement_ids: list[str],
    fin_input: FinalizationInput,
) -> None:
    rows = conn.execute(
        "SELECT so.public_id, d.public_id AS debtor, c.public_id AS creditor, "
        "so.amount, so.currency "
        "FROM settlement_obligations so "
        "JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id "
        "JOIN participants d ON d.id = so.debtor_id "
        "JOIN participants c ON c.id = so.creditor_id "
        "WHERE cr.public_id = ?",
        (fin_input.calculation_run_public_id,),
    ).fetchall()
    durable_ids = sorted(str(row["public_id"]) for row in rows)
    if durable_ids != sorted(settlement_ids):
        raise FinalizationIdempotencyError(
            "Replay settlement obligation rows do not match the recorded finalization",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    durable_obls = sorted(
        (
            str(row["debtor"]),
            str(row["creditor"]),
            canonical_money_str(
                money_decimal(str(row["amount"]), label="replay settlement amount"),
                str(row["currency"]),
            ),
            str(row["currency"]),
        )
        for row in rows
    )
    expected_obls = sorted(
        (
            o.debtor_participant_public_id,
            o.creditor_participant_public_id,
            canonical_money_str(o.amount, o.currency),
            o.currency,
        )
        for o in fin_input.settlement_obligations
    )
    if durable_obls != expected_obls:
        raise FinalizationIdempotencyError(
            "Replay settlement obligation amounts do not match the request",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


def _require_replay_participant_shares(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
) -> None:
    shares = fin_input.calculation_snapshot.get("participant_shares", {})
    if not isinstance(shares, dict):
        raise FinalizationIdempotencyError(
            "Replay snapshot participant_shares is not a mapping",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    rows = conn.execute(
        "SELECT p.public_id, cps.final_share_amount, cps.currency "
        "FROM calculation_participant_shares cps "
        "JOIN calculation_runs cr ON cr.id = cps.calculation_run_id "
        "JOIN participants p ON p.id = cps.participant_id "
        "WHERE cr.public_id = ?",
        (fin_input.calculation_run_public_id,),
    ).fetchall()
    for row in rows:
        if str(row["currency"]) != fin_input.currency:
            raise FinalizationIdempotencyError(
                f"Replay participant share for {str(row['public_id'])!r} carries currency "
                f"{str(row['currency'])!r}, not the finalized currency "
                f"{fin_input.currency!r}",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
    durable_shares = {
        str(row["public_id"]): canonical_money_str(
            money_decimal(str(row["final_share_amount"]), label="replay participant share"),
            str(row["currency"]),
        )
        for row in rows
    }
    if len(durable_shares) != len(rows):
        raise FinalizationIdempotencyError(
            "Replay participant shares carry duplicate participant identities",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    expected_shares = {
        str(pub_id): canonical_money_str(
            money_decimal(amount, label="replay expected participant share"), fin_input.currency
        )
        for pub_id, amount in shares.items()
    }
    if durable_shares != expected_shares:
        raise FinalizationIdempotencyError(
            "Replay participant shares do not match the recorded finalization",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


def _require_replay_authorization_truth(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    fingerprint: str,
) -> None:
    """Re-verify the durable authorization on replay, requiring 'consumed' state.

    A successful finalization transitions its authorization to 'consumed'
    inside the same Unit of Work that writes the idempotency record, so on
    any true replay the durable state can only be 'consumed'.  An
    authorization observed as 'authorized' (or any other state) alongside a
    durable finalization is forged or inconsistent state and fails closed.
    Every material field of both the authorization and the confirmation rows
    is compared against this request, including the authorization version.
    """
    row = conn.execute(
        "SELECT authorization_id, receipt_group_public_id, calculation_run_public_id, "
        "calculation_snapshot_id, confirmation_id, content_hash, currency, final_total, "
        "payer_participant_public_id, participant_public_ids_json, "
        "settlement_obligations_json, source_evidence_refs_json, actor_type, actor_id, "
        "authorization_state, authorization_version "
        "FROM receipt_finalization_authorizations WHERE authorization_id = ?",
        (fin_input.authorization_id,),
    ).fetchone()
    if row is None:
        raise FinalizationIdempotencyError(
            f"Replay authorization {fin_input.authorization_id!r} is missing",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    auth = dict(row)
    if auth["authorization_state"] != "consumed":
        raise FinalizationIdempotencyError(
            f"Replay authorization state is {auth['authorization_state']!r}; "
            "a durably finalized authorization can only be 'consumed'",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if str(auth.get("authorization_version") or "") != "v1":
        raise FinalizationIdempotencyError(
            f"Replay authorization version is {str(auth.get('authorization_version'))!r}, not 'v1'",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if auth["content_hash"] != fingerprint:
        raise FinalizationIdempotencyError(
            "Replay authorization content hash does not match the request fingerprint",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    material = (
        auth["receipt_group_public_id"],
        auth["calculation_run_public_id"],
        auth["calculation_snapshot_id"],
        auth["confirmation_id"],
        auth["currency"],
        auth["payer_participant_public_id"],
        auth["actor_type"],
        auth.get("actor_id"),
    )
    expected = (
        fin_input.receipt_group_public_id,
        fin_input.calculation_run_public_id,
        fin_input.calculation_snapshot_id,
        fin_input.confirmation_id,
        fin_input.currency,
        fin_input.payer_participant_public_id,
        fin_input.actor_type,
        fin_input.actor_id,
    )
    if material != expected:
        raise FinalizationIdempotencyError(
            "Replay authorization material fields do not match the request",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # R3-01: compare final_total, participant IDs, settlement obligations, version.
    # These are compared against the request's snapshot-derived values.
    shares = fin_input.calculation_snapshot.get("participant_shares", {})
    auth_total = quantize_for_currency(
        money_decimal(str(auth["final_total"]), label="replay authorization final_total"),
        fin_input.currency,
    )
    # The expected total_paid comes from the snapshot's total_paid field.
    snapshot_total_paid = fin_input.calculation_snapshot.get("total_paid")
    if snapshot_total_paid is not None:
        expected_total = quantize_for_currency(
            money_decimal(str(snapshot_total_paid), label="replay snapshot total_paid"),
            fin_input.currency,
        )
        if auth_total != expected_total:
            raise FinalizationIdempotencyError(
                "Replay authorization final_total does not match the snapshot total_paid",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
    # Participant set: authorization must match the snapshot's participant shares keys.
    auth_participants = _parse_unique_nonempty_string_list(
        auth.get("participant_public_ids_json") or "[]",
        label="Replay authorization participant_public_ids_json",
        malformed_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        mismatch_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
    )
    expected_participants = sorted(shares.keys())
    if auth_participants != expected_participants:
        raise FinalizationIdempotencyError(
            "Replay authorization participant set does not match the snapshot shares",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # Settlement obligations JSON must match the request.
    import json as _json

    auth_obligations_raw = auth.get("settlement_obligations_json") or "[]"
    try:
        auth_obligations = _json.loads(auth_obligations_raw)
    except (ValueError, TypeError):
        raise FinalizationIdempotencyError(
            "Replay authorization settlement_obligations_json is malformed",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    request_obligations = sorted(
        [
            {
                "debtor": o.debtor_participant_public_id,
                "creditor": o.creditor_participant_public_id,
                "amount": canonical_money_str(o.amount, o.currency),
                "currency": o.currency,
            }
            for o in fin_input.settlement_obligations
        ],
        key=lambda x: (x["debtor"], x["creditor"]),
    )
    durable_obligations = sorted(
        [
            {
                "debtor": str(o.get("debtor", "")),
                "creditor": str(o.get("creditor", "")),
                "amount": canonical_money_str(
                    money_decimal(str(o.get("amount", "0")), label="replay auth obligation"),
                    str(o.get("currency", fin_input.currency)),
                ),
                "currency": str(o.get("currency", "")),
            }
            for o in auth_obligations
        ],
        key=lambda x: (x["debtor"], x["creditor"]),
    )
    if durable_obligations != request_obligations:
        raise FinalizationIdempotencyError(
            "Replay authorization settlement obligations do not match the request",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # Verify source evidence refs are intact.
    auth_evidence = _parse_unique_nonempty_string_list(
        auth.get("source_evidence_refs_json") or "[]",
        label="Replay authorization source_evidence_refs_json",
        malformed_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        mismatch_reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
    )
    if auth_evidence != sorted(fin_input.source_evidence_refs):
        raise FinalizationIdempotencyError(
            "Replay authorization evidence refs do not match the request",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    # R3-01: re-read and verify the confirmation row on replay.
    confirmation_id = auth.get("confirmation_id")
    if confirmation_id:
        conf_row = conn.execute(
            "SELECT confirmation_id, content_hash, receipt_group_public_id, "
            "calculation_run_public_id, calculation_snapshot_id, currency, "
            "payer_participant_public_id, confirmation_state "
            "FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
            (confirmation_id,),
        ).fetchone()
        if conf_row is None:
            raise FinalizationIdempotencyError(
                f"Replay confirmation {confirmation_id!r} is missing",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        conf = dict(conf_row)
        if conf["confirmation_state"] != "confirmed":
            raise FinalizationIdempotencyError(
                f"Replay confirmation state is {conf['confirmation_state']!r}, not 'confirmed'",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        if conf["content_hash"] != fingerprint:
            raise FinalizationIdempotencyError(
                "Replay confirmation content_hash does not match the request fingerprint",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )
        conf_material = (
            conf["receipt_group_public_id"],
            conf["calculation_run_public_id"],
            conf["calculation_snapshot_id"],
            conf["currency"],
            conf["payer_participant_public_id"],
        )
        expected_conf = (
            fin_input.receipt_group_public_id,
            fin_input.calculation_run_public_id,
            fin_input.calculation_snapshot_id,
            fin_input.currency,
            fin_input.payer_participant_public_id,
        )
        if conf_material != expected_conf:
            raise FinalizationIdempotencyError(
                "Replay confirmation material fields do not match the request",
                reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
            )


def _require_replay_audit_binding_evidence(
    conn: sqlite3.Connection,
    audit_id: str,
    fin_input: FinalizationInput,
) -> None:
    """Verify the finalization-audit binding evidence still matches the four-tuple."""
    if fin_input.active_fact_set_binding is None:
        return
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("receipt_fact_set_binding_evidence",),
    ).fetchone()
    if row is None:
        return
    durable = read_fact_set_binding_evidence(
        conn,
        bound_record_type="finalization_audit",
        bound_record_public_id=audit_id,
    )
    if durable is None:
        raise FinalizationIdempotencyError(
            f"Replay finalization-audit binding evidence for {audit_id!r} is missing",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )
    if durable != fin_input.active_fact_set_binding:
        raise FinalizationIdempotencyError(
            f"Replay finalization-audit binding evidence for {audit_id!r} contradicts "
            "the authorized four-tuple",
            reason=FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value,
        )


# ===================================================================
# Canonical transaction write
# ===================================================================


def _make_transaction_public_id(fin_input: FinalizationInput) -> str:
    """Build a deterministic transaction public_id."""
    raw = f"fin-txn|{fin_input.receipt_group_public_id}|{fin_input.calculation_run_public_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"fin-txn-{digest}"


def _insert_canonical_transaction(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    payer_id: int,
    amount: Decimal,
    currency: str,
    fin_input: FinalizationInput,
    txn_date: str,
    receipt_identity: ConfirmedReceiptIdentity | None = None,
) -> None:
    """Insert a canonical transaction row into the ``transactions`` table.

    The merchant is the confirmed receipt's own merchant whenever the authorized
    snapshot hash-binds a receipt identity.  Falling back to the receipt public
    ID would record an identifier as a merchant name, which is not a financial
    fact of the receipt.
    """
    snapshot = fin_input.calculation_snapshot
    if receipt_identity is not None:
        merchant = receipt_identity.merchant
        source_channel = receipt_identity.source_channel
    else:
        merchant = snapshot.get("merchant") or snapshot.get("case_id", "receipt")
        source_channel = "receipt_finalization"

    conn.execute(
        """
        INSERT INTO transactions (
            public_id, intent, intent_type,
            source_channel, transaction_date,
            status, paid_by_participant_id,
            amount, currency,
            merchant, raw_input, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            "receipt_finalization",
            "Generated",
            source_channel,
            txn_date,
            "active",
            payer_id,
            canonical_money_str(amount, currency),
            currency,
            merchant,
            json.dumps(fin_input.calculation_snapshot, sort_keys=True, default=str),
            f"Receipt finalization for {fin_input.receipt_group_public_id}",
        ),
    )


def _derive_transaction_date(
    snapshot: dict[str, Any],
    created_at: str,
    *,
    receipt_identity: ConfirmedReceiptIdentity | None = None,
) -> str:
    """Derive a transaction date from the authorized receipt facts.

    Prefers the confirmed receipt identity hash-bound into the authoritative
    snapshot, then ``receipt_datetime`` from the first snapshot receipt, and
    only falls back to the injected ``created_at`` timestamp when the snapshot
    carries no receipt date at all (never wall-clock time).  The finalization
    clock is not a receipt date and is never preferred over one.
    """
    if receipt_identity is not None:
        return receipt_identity.receipt_date
    receipts = snapshot.get("receipts", [])
    if isinstance(receipts, list) and receipts:
        first = receipts[0]
        if isinstance(first, dict):
            dt = first.get("receipt_datetime")
            if isinstance(dt, str) and dt:
                return dt[:10]  # date part only
    # Fall back to the injected clock's date, never datetime.now().
    return created_at[:10]


# ===================================================================
# Receipt group
# ===================================================================


def _lookup_receipt_group(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, public_id, currency, status
        FROM receipt_groups
        WHERE public_id = ?
        """,
        (receipt_group_public_id,),
    ).fetchone()
    if row is None:
        raise IneligibleForFinalizationError(
            f"Receipt group not found: {receipt_group_public_id}",
        )
    return dict(row)


def _require_eligible_for_finalization(
    group: dict[str, Any],
    receipt_group_public_id: str,
) -> None:
    status = group["status"]
    if status not in ELIGIBLE_RECEIPT_GROUP_STATUSES:
        raise IneligibleForFinalizationError(
            f"Receipt group {receipt_group_public_id} has status {status!r}; "
            f"eligible statuses are {sorted(ELIGIBLE_RECEIPT_GROUP_STATUSES)}"
        )


def _require_currency_match(
    group: dict[str, Any],
    fin_input: FinalizationInput,
) -> None:
    group_currency = group["currency"]
    input_currency = fin_input.currency
    if group_currency != input_currency:
        raise IneligibleForFinalizationError(
            f"Receipt group {group['public_id']} has currency {group_currency!r}; "
            f"finalization input expected {input_currency!r}"
        )


def _update_receipt_group_status(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
    new_status: str,
) -> None:
    conn.execute(
        "UPDATE receipt_groups SET status = ?, updated_at = ? WHERE public_id = ?",
        (new_status, datetime.now(timezone.utc).isoformat(), receipt_group_public_id),
    )


# ===================================================================
# Participants
# ===================================================================


def _lookup_participants_by_public_id(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    receipt_group_internal_id: int,
    *,
    bound_receipt_public_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load participants using the exact authoritative membership scope.

    R3-07-1: membership is receipt-scoped.  Participant public ID alone is an
    insufficient identity inside a multi-receipt group, so no group-wide
    collapse ("most included wins", OR-style inclusion, or participant-only
    aggregation) is ever applied:

    - IAF path (``bound_receipt_public_id`` set): the snapshot-bound receipt
      is the single membership authority.  Every referenced participant must
      hold its membership row on exactly that receipt; membership on any
      other receipt of the group never leaks in.
    - Legacy group-scoped path (no binding): membership must be
      uncontradicted among the receipts of the group that carry the
      participant.
      A participant whose ``is_included`` or ``role`` differs across receipts
      of the group is contradictory, and no approved contract defines which
      receipt owns the decision, so the finalization fails closed.

    The payer is loaded regardless of its ``is_included`` status: a payer may
    be excluded as a consumer (D5, Section 7) but must still exist as a
    settlement creditor and canonical-transaction payer.  All other
    referenced participants (consumers and debtors) must be included members.
    """
    referenced_public_ids: set[str] = {fin_input.payer_participant_public_id}
    for obligation in fin_input.settlement_obligations:
        referenced_public_ids.add(obligation.debtor_participant_public_id)
        referenced_public_ids.add(obligation.creditor_participant_public_id)
    shares = fin_input.calculation_snapshot.get("participant_shares", {})
    referenced_public_ids.update(shares.keys())

    if not referenced_public_ids:
        raise IneligibleForFinalizationError("No participants referenced in finalization input")

    placeholders = ",".join("?" for _ in referenced_public_ids)
    if bound_receipt_public_id is not None:
        bound_row = conn.execute(
            "SELECT r.id FROM receipts r "
            "JOIN receipt_group_receipts rgr ON rgr.receipt_id = r.id "
            "WHERE r.public_id = ? AND rgr.receipt_group_id = ?",
            (bound_receipt_public_id, receipt_group_internal_id),
        ).fetchone()
        if bound_row is None:
            raise IneligibleForFinalizationError(
                f"Snapshot-bound receipt {bound_receipt_public_id!r} is not a member of "
                "the target receipt group; receipt-scoped membership is unresolvable"
            )
        rows = conn.execute(
            f"""
            SELECT p.id, p.public_id, p.display_name, rp.is_included, rp.role,
                   rp.receipt_id
            FROM participants p
            JOIN receipt_participants rp ON rp.participant_id = p.id
            WHERE p.public_id IN ({placeholders})
              AND rp.receipt_id = ?
            """,
            tuple(referenced_public_ids) + (int(bound_row["id"]),),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            pub_id = row["public_id"]
            if pub_id in result:
                raise IneligibleForFinalizationError(
                    f"Participant {pub_id!r} holds duplicate membership rows on receipt "
                    f"{bound_receipt_public_id!r}; membership identity is corrupt"
                )
            result[pub_id] = dict(row)
    else:
        rows = conn.execute(
            f"""
            SELECT p.id, p.public_id, p.display_name, rp.is_included, rp.role,
                   rp.receipt_id
            FROM participants p
            JOIN receipt_participants rp ON rp.participant_id = p.id
            JOIN receipt_group_receipts rgr ON rgr.receipt_id = rp.receipt_id
            WHERE p.public_id IN ({placeholders})
              AND rgr.receipt_group_id = ?
            """,
            tuple(referenced_public_ids) + (receipt_group_internal_id,),
        ).fetchall()
        result = {}
        for row in rows:
            pub_id = row["public_id"]
            if pub_id not in result:
                result[pub_id] = dict(row)
                continue
            existing = result[pub_id]
            if int(row["is_included"]) != int(existing["is_included"]) or str(row["role"]) != str(
                existing["role"]
            ):
                raise IneligibleForFinalizationError(
                    f"Participant {pub_id!r} has contradictory membership across "
                    "receipts in this group; finalization requires "
                    "uncontradicted membership identity"
                )

    found_ids = set(result.keys())
    missing = referenced_public_ids - found_ids
    if missing:
        raise IneligibleForFinalizationError(
            f"Participants not found or not in receipt group: {sorted(missing)}"
        )

    # The payer must exist, but may be excluded. Every other referenced
    # participant (consumer / debtor) must have is_included=1.
    # FIX-06A: the payer's durable role must still be 'payer' -- role drift to
    # 'excluded' or 'participant' means the membership authority was corrupted.
    # This check only applies when a confirmed_receipt_identity is bound (IAF
    # path); legacy group-only finalizations seed all members as 'participant'.
    payer_public_id = fin_input.payer_participant_public_id
    payer_info = result.get(payer_public_id)
    if payer_info is None:
        raise IneligibleForFinalizationError(
            f"Payer {payer_public_id!r} not found in receipt group membership"
        )
    if fin_input.confirmed_receipt_identity is not None:
        if str(payer_info.get("role", "")) != "payer":
            raise IneligibleForFinalizationError(
                f"Payer {payer_public_id!r} membership role is "
                f"{str(payer_info.get('role'))!r}, not 'payer'; "
                "the payer identity has drifted and finalization is refused"
            )
    # R3-06: excluded payer (is_included=0) must have zero consumer share.
    # An excluded payer only exists as the payment creditor; if the snapshot
    # assigns a nonzero share to an excluded payer, finalization is refused.
    if int(payer_info.get("is_included", 1)) == 0:
        shares = fin_input.calculation_snapshot.get("participant_shares", {})
        payer_share_raw = shares.get(payer_public_id, "0")
        payer_share = money_decimal(str(payer_share_raw), label="excluded payer share")
        if payer_share != Decimal("0"):
            raise IneligibleForFinalizationError(
                f"Excluded payer {payer_public_id!r} has nonzero consumer share "
                f"{payer_share_raw!r}; excluded payers must not consume"
            )
    for public_id, info in result.items():
        if public_id == payer_public_id:
            continue  # The payer's is_included does not gate finalization.
        if int(info["is_included"]) != 1:
            raise IneligibleForFinalizationError(
                f"Participant {public_id!r} is referenced in obligations or shares "
                "but is not an included member of the receipt group"
            )
    return result


def _resolve_participant(
    participants_by_public_id: dict[str, dict[str, Any]],
    public_id: str,
) -> int:
    participant = participants_by_public_id.get(public_id)
    if participant is None:
        raise IneligibleForFinalizationError(
            f"Participant {public_id!r} not found in receipt group"
        )
    return participant["id"]


# ===================================================================
# Snapshot identity validation
# ===================================================================


def _validate_snapshot_identity(
    fin_input: FinalizationInput,
    participants_by_public_id: dict[str, dict[str, Any]],
) -> None:
    """Validate snapshot identity consistency before any database writes."""
    snapshot = fin_input.calculation_snapshot
    snapshot_participants = snapshot.get("participants", [])
    if not isinstance(snapshot_participants, list) or not snapshot_participants:
        raise IneligibleForFinalizationError(
            "calculation_snapshot.participants must be a non-empty list"
        )

    seen: set[str] = set()
    duplicates: set[str] = set()
    for pub_id in snapshot_participants:
        if not isinstance(pub_id, str) or not pub_id:
            raise IneligibleForFinalizationError(
                f"calculation_snapshot.participants contains non-string value: {pub_id!r}"
            )
        if pub_id in seen:
            duplicates.add(pub_id)
        seen.add(pub_id)
    if duplicates:
        raise IneligibleForFinalizationError(
            f"calculation_snapshot.participants contains duplicate public IDs: {sorted(duplicates)}"
        )

    participant_set = seen

    snapshot_payer = snapshot.get("payer")
    if not snapshot_payer or not isinstance(snapshot_payer, str):
        raise IneligibleForFinalizationError(
            "calculation_snapshot.payer is required and must be a non-empty string"
        )
    if snapshot_payer != fin_input.payer_participant_public_id:
        raise IneligibleForFinalizationError(
            f"calculation_snapshot.payer {snapshot_payer!r} does not match "
            f"FinalizationInput.payer_participant_public_id "
            f"{fin_input.payer_participant_public_id!r}"
        )
    if snapshot_payer not in participants_by_public_id:
        raise IneligibleForFinalizationError(
            f"calculation_snapshot.payer {snapshot_payer!r} not found in receipt group"
        )
    if snapshot_payer not in participant_set:
        raise IneligibleForFinalizationError(
            f"calculation_snapshot.payer {snapshot_payer!r} is not in "
            f"calculation_snapshot.participants"
        )

    shares = snapshot.get("participant_shares", {})
    if not isinstance(shares, dict):
        raise IneligibleForFinalizationError(
            "calculation_snapshot.participant_shares must be a mapping"
        )
    for pub_id in shares:
        if not isinstance(pub_id, str) or not pub_id:
            raise IneligibleForFinalizationError(
                f"calculation_snapshot.participant_shares key {pub_id!r} "
                f"is not a valid participant public ID"
            )
    share_keys = set(shares.keys())
    if share_keys != participant_set:
        missing_from_shares = participant_set - share_keys
        extra_in_shares = share_keys - participant_set
        msg_parts = []
        if missing_from_shares:
            msg_parts.append(f"missing from shares: {sorted(missing_from_shares)}")
        if extra_in_shares:
            msg_parts.append(f"extra in shares: {sorted(extra_in_shares)}")
        raise IneligibleForFinalizationError(
            "calculation_snapshot.participant_shares keys do not match "
            f"participant set: {'; '.join(msg_parts)}"
        )

    _validate_nested_snapshot_identity(snapshot, participant_set)

    expected_obligations = snapshot.get("settlement_obligations", [])
    if isinstance(expected_obligations, list):
        for i, obl in enumerate(expected_obligations):
            if not isinstance(obl, dict):
                continue
            debtor = obl.get("debtor") if isinstance(obl, dict) else None
            creditor = obl.get("creditor") if isinstance(obl, dict) else None
            label = f"calculation_snapshot.settlement_obligations[{i}]"
            if debtor and (not isinstance(debtor, str) or debtor not in participants_by_public_id):
                raise IneligibleForFinalizationError(
                    f"{label}.debtor {debtor!r} not found in receipt group"
                )
            if creditor and (
                not isinstance(creditor, str) or creditor not in participants_by_public_id
            ):
                raise IneligibleForFinalizationError(
                    f"{label}.creditor {creditor!r} not found in receipt group"
                )
            if debtor and (not isinstance(debtor, str) or debtor not in participant_set):
                raise IneligibleForFinalizationError(
                    f"{label}.debtor {debtor!r} is not in snapshot participants"
                )
            if creditor and (not isinstance(creditor, str) or creditor not in participant_set):
                raise IneligibleForFinalizationError(
                    f"{label}.creditor {creditor!r} is not in snapshot participants"
                )


def _validate_nested_snapshot_identity(
    snapshot: dict[str, Any],
    participant_set: set[str],
) -> None:
    """Validate that all nested identity fields reference valid snapshot participants."""
    ppa = snapshot.get("payer_paid_amounts", {})
    if isinstance(ppa, dict):
        for key in ppa:
            if isinstance(key, str) and key and key not in participant_set:
                raise IneligibleForFinalizationError(
                    f"calculation_snapshot.payer_paid_amounts key {key!r} "
                    f"is not in snapshot participants"
                )

    pos = snapshot.get("payer_own_shares", {})
    if isinstance(pos, dict):
        for key in pos:
            if isinstance(key, str) and key and key not in participant_set:
                raise IneligibleForFinalizationError(
                    f"calculation_snapshot.payer_own_shares key {key!r} "
                    f"is not in snapshot participants"
                )

    tps = snapshot.get("total_participant_shares", {})
    if isinstance(tps, dict):
        for key in tps:
            if isinstance(key, str) and key and key not in participant_set:
                raise IneligibleForFinalizationError(
                    f"calculation_snapshot.total_participant_shares key {key!r} "
                    f"is not in snapshot participants"
                )

    receipts = snapshot.get("receipts", [])
    if isinstance(receipts, list):
        for ri, receipt in enumerate(receipts):
            if not isinstance(receipt, dict):
                continue
            rlabel = f"calculation_snapshot.receipts[{ri}]"

            paid_by = receipt.get("paid_by")
            if isinstance(paid_by, str) and paid_by and paid_by not in participant_set:
                raise IneligibleForFinalizationError(
                    f"{rlabel}.paid_by {paid_by!r} is not in snapshot participants"
                )

            items = receipt.get("items", [])
            if isinstance(items, list):
                for ii, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    ilabel = f"{rlabel}.items[{ii}]"
                    allocs = item.get("participant_allocations", {})
                    if isinstance(allocs, dict):
                        for key in allocs:
                            if isinstance(key, str) and key and key not in participant_set:
                                raise IneligibleForFinalizationError(
                                    f"{ilabel}.participant_allocations key {key!r} "
                                    f"is not in snapshot participants"
                                )

            adjustments = receipt.get("adjustments", [])
            if isinstance(adjustments, list):
                for ai, adj in enumerate(adjustments):
                    if not isinstance(adj, dict):
                        continue
                    alabel = f"{rlabel}.adjustments[{ai}]"
                    allocs = adj.get("participant_allocations", {})
                    if isinstance(allocs, dict):
                        for key in allocs:
                            if isinstance(key, str) and key and key not in participant_set:
                                raise IneligibleForFinalizationError(
                                    f"{alabel}.participant_allocations key {key!r} "
                                    f"is not in snapshot participants"
                                )

            rounding_adjs = receipt.get("rounding_adjustments", [])
            if isinstance(rounding_adjs, list):
                for rai, ra in enumerate(rounding_adjs):
                    if not isinstance(ra, dict):
                        continue
                    ra_participant = ra.get("participant")
                    if (
                        isinstance(ra_participant, str)
                        and ra_participant
                        and ra_participant not in participant_set
                    ):
                        raise IneligibleForFinalizationError(
                            f"{rlabel}.rounding_adjustments[{rai}].participant "
                            f"{ra_participant!r} is not in snapshot participants"
                        )

            rap = receipt.get("rounding_adjustment_participant")
            if isinstance(rap, str) and rap and rap not in participant_set:
                raise IneligibleForFinalizationError(
                    f"{rlabel}.rounding_adjustment_participant {rap!r} "
                    f"is not in snapshot participants"
                )


# ===================================================================
# Pre-write revalidation
# ===================================================================


def _revalidate_obligations_match_snapshot(
    fin_input: FinalizationInput,
) -> None:
    """Revalidate obligations against snapshot immediately before writes."""
    from finance_core.receipt_finalization.models import (
        _canonical_from_mapping,
        _canonical_from_obligation,
        _money_decimal,
    )

    snapshot = fin_input.calculation_snapshot
    expected_obligations = snapshot.get("settlement_obligations", [])

    if len(fin_input.settlement_obligations) != len(expected_obligations):
        raise IneligibleForFinalizationError(
            f"Pre-write revalidation: settlement obligations count "
            f"{len(fin_input.settlement_obligations)} does not match "
            f"snapshot count {len(expected_obligations)}"
        )

    actual = sorted(_canonical_from_obligation(o) for o in fin_input.settlement_obligations)
    expected = sorted(
        _canonical_from_mapping(o, f"calculation_snapshot.settlement_obligations[{i}]")
        for i, o in enumerate(expected_obligations)
    )
    if actual != expected:
        raise IneligibleForFinalizationError(
            "Pre-write revalidation: settlement obligations do not match "
            "calculation snapshot settlement_obligations"
        )

    expected_total = snapshot.get("total_paid")
    payer_own_val = snapshot.get("payer_own_share")
    if expected_total is not None and payer_own_val is not None:
        expected_collect = _money_decimal(expected_total, "calculation_snapshot.total_paid")
        expected_collect -= _money_decimal(payer_own_val, "calculation_snapshot.payer_own_share")
        actual_total = sum((o.amount for o in fin_input.settlement_obligations), Decimal("0.00"))
        if actual_total != expected_collect:
            raise IneligibleForFinalizationError(
                f"Pre-write revalidation: settlement obligations total "
                f"{actual_total} does not match expected total to collect "
                f"{expected_collect} (total_paid {expected_total} - "
                f"payer_own_share {payer_own_val})"
            )


# ===================================================================
# Persistence helpers (no independent commits)
# ===================================================================


def _insert_calculation_run(
    conn: sqlite3.Connection,
    fin_input: FinalizationInput,
    receipt_group_id: int,
    currency: str,
    participants_by_public_id: dict[str, dict[str, Any]],
) -> int:
    safe_snapshot = dict(fin_input.calculation_snapshot)
    safe_snapshot.pop("participant_display_names", None)
    snapshot_participants = safe_snapshot.get("participants", [])
    db_display_names: dict[str, str] = {}
    if isinstance(snapshot_participants, list):
        for pub_id in snapshot_participants:
            if isinstance(pub_id, str) and pub_id in participants_by_public_id:
                db_display_names[pub_id] = participants_by_public_id[pub_id]["display_name"]
    if db_display_names:
        safe_snapshot["participant_display_names"] = db_display_names

    snapshot_json = (
        json.dumps(_serialize_snapshot(safe_snapshot), sort_keys=True) if safe_snapshot else None
    )

    input_hash = hashlib.sha256(snapshot_json.encode("utf-8") if snapshot_json else b"").hexdigest()

    cursor = conn.execute(
        """
        INSERT INTO calculation_runs (
            public_id, calculation_version, scope_type,
            receipt_group_id, currency, input_hash,
            calculation_status, source, raw_input
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fin_input.calculation_run_public_id,
            CALCULATION_VERSION,
            "receipt_group",
            receipt_group_id,
            currency,
            input_hash,
            "completed",
            "receipt_finalization_runtime_v1",
            snapshot_json,
        ),
    )
    last_row_id = cursor.lastrowid
    assert last_row_id is not None
    return last_row_id


def _insert_participant_shares(
    conn: sqlite3.Connection,
    calc_run_id: int,
    fin_input: FinalizationInput,
    participants_by_public_id: dict[str, dict[str, Any]],
) -> None:
    shares = fin_input.calculation_snapshot.get("participant_shares", {})
    if not shares:
        return

    for pub_id, amount in shares.items():
        participant = participants_by_public_id.get(pub_id)
        if participant is None:
            raise IneligibleForFinalizationError(
                f"Participant {pub_id!r} not found in database; "
                f"cannot persist participant share for calculation"
            )
        share_pub_id = f"cps_{fin_input.calculation_run_public_id}_{pub_id}"

        amount_dec = Decimal(str(amount))
        conn.execute(
            """
            INSERT INTO calculation_participant_shares (
                public_id, calculation_run_id,
                participant_id, final_share_amount,
                currency, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                share_pub_id,
                calc_run_id,
                participant["id"],
                str(amount_dec),
                fin_input.currency,
                "receipt_finalization_runtime_v1",
            ),
        )


def _insert_settlement_obligations(
    conn: sqlite3.Connection,
    calc_run_id: int,
    fin_input: FinalizationInput,
    participants_by_public_id: dict[str, dict[str, Any]],
    payer_id: int,
) -> list[str]:
    """Insert settlement obligation rows. Returns the list of public_ids."""
    pub_ids: list[str] = []
    for i, obligation in enumerate(fin_input.settlement_obligations):
        debtor = participants_by_public_id[obligation.debtor_participant_public_id]
        creditor = participants_by_public_id[obligation.creditor_participant_public_id]
        settlement_pub_id = f"soblg_{fin_input.calculation_run_public_id}_{i}"

        if debtor["id"] == creditor["id"]:
            raise IneligibleForFinalizationError(
                f"Self-obligation rejected: "
                f"{obligation.debtor_participant_public_id} "
                f"cannot owe {obligation.creditor_participant_public_id}"
            )

        if creditor["id"] != payer_id:
            raise IneligibleForFinalizationError(
                f"Creditor {obligation.creditor_participant_public_id!r} "
                f"(internal id {creditor['id']}) does not match payer "
                f"{fin_input.payer_participant_public_id!r} "
                f"(internal id {payer_id}); all obligations must credit the payer"
            )

        conn.execute(
            """
            INSERT INTO settlement_obligations (
                public_id, debtor_id, creditor_id,
                amount, currency,
                source_calculation_run_id,
                settlement_status, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settlement_pub_id,
                debtor["id"],
                creditor["id"],
                str(obligation.amount),
                obligation.currency,
                calc_run_id,
                "open",
                "receipt_finalization_runtime_v1",
            ),
        )
        pub_ids.append(settlement_pub_id)
    return pub_ids


# ===================================================================
# Audit
# ===================================================================


def _insert_audit(
    conn: sqlite3.Connection,
    *,
    finalization_id: str,
    fin_input: FinalizationInput,
    fingerprint: str,
    txn_public_id: str,
    settlement_public_ids: list[str],
    total_paid_str: str,
    total_to_collect_str: str,
    status: str,
    created_at: str,
) -> None:
    """Insert an immutable finalization audit record."""
    conn.execute(
        """
        INSERT INTO receipt_finalization_audit (
            finalization_id, idempotency_key, content_fingerprint,
            authorization_id, confirmation_id,
            receipt_group_public_id, calculation_run_public_id,
            calculation_snapshot_id, transaction_public_id,
            participant_public_ids_json, settlement_public_ids_json,
            currency, total_paid, total_to_collect,
            payer_participant_public_id,
            actor_type, actor_id,
            status, failure_reasons_json,
            evidence_refs_json, source_attachment_refs_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            finalization_id,
            fin_input.idempotency_key,
            fingerprint,
            fin_input.authorization_id,
            fin_input.confirmation_id or None,
            fin_input.receipt_group_public_id,
            fin_input.calculation_run_public_id,
            fin_input.calculation_snapshot_id or None,
            txn_public_id,
            json.dumps(sorted(fin_input.participant_public_ids), sort_keys=True),
            json.dumps(sorted(settlement_public_ids), sort_keys=True),
            fin_input.currency,
            total_paid_str,
            total_to_collect_str,
            fin_input.payer_participant_public_id,
            fin_input.actor_type,
            fin_input.actor_id,
            status,
            "[]",
            json.dumps(sorted(fin_input.source_evidence_refs), sort_keys=True),
            "[]",
            created_at,
        ),
    )


# ===================================================================
# Idempotency persistence
# ===================================================================


def _derive_membership_evidence_public_id(finalization_id: str, participant_public_id: str) -> str:
    """Build a deterministic membership evidence public ID."""
    raw = f"rfme|{finalization_id}|{participant_public_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"rfme_{digest}"


def _insert_membership_evidence(
    conn: sqlite3.Connection,
    *,
    finalization_id: str,
    fin_input: FinalizationInput,
    participants_by_public_id: dict[str, dict[str, Any]],
    bound_receipt_public_id: str | None,
    created_at: str,
) -> None:
    """Persist the exact receipt-scoped membership this finalization consumed.

    One append-only row per referenced participant (migration 039).  IAF
    finalizations record scope 'receipt' with the snapshot-bound receipt;
    legacy group-scoped finalizations record scope 'receipt_group' with no
    single owning receipt, because no receipt in the group contradicted the
    membership identity that was consumed.
    """
    scope = "receipt" if bound_receipt_public_id is not None else "receipt_group"
    for participant_public_id in sorted(participants_by_public_id):
        info = participants_by_public_id[participant_public_id]
        conn.execute(
            """
            INSERT INTO receipt_finalization_membership_evidence (
                membership_evidence_public_id, finalization_id,
                receipt_group_public_id, membership_scope, receipt_public_id,
                participant_public_id, role, is_included, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _derive_membership_evidence_public_id(finalization_id, participant_public_id),
                finalization_id,
                fin_input.receipt_group_public_id,
                scope,
                bound_receipt_public_id,
                participant_public_id,
                str(info["role"]),
                int(info["is_included"]),
                created_at,
            ),
        )


def _insert_idempotency(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    fingerprint: str,
    status: str,
    audit_id: str,
    created_at: str,
) -> None:
    """Insert a durable idempotency guard record (FK → audit)."""
    conn.execute(
        """
        INSERT INTO receipt_finalization_idempotency (
            idempotency_key, content_fingerprint, status,
            finalization_audit_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (idempotency_key, fingerprint, status, audit_id, created_at),
    )


# ===================================================================
# Authorization consumption
# ===================================================================


def _consume_authorization(
    conn: sqlite3.Connection,
    authorization_id: str,
) -> None:
    """Mark the authorization as consumed inside the transaction."""
    conn.execute(
        """
        UPDATE receipt_finalization_authorizations
        SET authorization_state = 'consumed'
        WHERE authorization_id = ?
        """,
        (authorization_id,),
    )


# ===================================================================
# Helpers
# ===================================================================


def _snapshot_amount(
    snapshot: dict[str, Any],
    key: str,
    currency: str,
    default: Decimal | None = None,
) -> Decimal:
    """Extract and validate an amount from the snapshot."""
    raw = snapshot.get(key)
    if raw is None and default is not None:
        return default
    if raw is None:
        return Decimal("0.00")
    return money_decimal(raw, label=f"calculation_snapshot.{key}")


def _decimal_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError("Object of type {{obj.__class__.__name__}} is not JSON serializable")


def _serialize_snapshot(snapshot: dict) -> dict:
    """Deep-convert Decimal values in a snapshot dict to JSON-safe strings."""
    return json.loads(json.dumps(snapshot, default=_decimal_default, sort_keys=True))
