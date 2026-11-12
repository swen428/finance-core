"""Explicit 001-062 native closure content; excludes preparation journal state.

This format carries the seven native tables and archived evidence only. Checked
content provides neither a writable owner nor complete application recovery.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from decimal import localcontext

from finance_core.instance_admission.sessions import ProductionOwner
from finance_core.reconciliation.migrations import apply_migration_paths
from finance_core.sqlite_connection import configure_sqlite_connection

from . import _recovery_material_engine as _engine
from ._recovery_rows_v1 import COLUMNS, decode_rows, fail, limits_v1, read_rows, rows_hash
from .contracts import LifecycleError, RuntimeBinding, digest
from .reader import audit_evidence_connection
from .recovery_material_v1 import (
    ClosureCheckV1,
    ClosureFrontierV1,
    ClosureIdentityV1,
    ClosureInventoryV1,
    ClosureLimitsV1,
    NativeClosureV1,
    RecoveryEvidenceArchiveSourceV1,
    RecoveryEvidenceAuthenticatorV1,
    RecoveryMaterialError,
)
from .service import LedgerService


@dataclass(frozen=True)
class ClosureIdentityV2(ClosureIdentityV1):
    format: str = "native-closure-identity-v2"
    scope: str = "offline-native-personal-expense-v2"


@dataclass(frozen=True)
class ClosureFrontierV2(ClosureFrontierV1):
    format: str = "native-closure-frontier-v2"


@dataclass(frozen=True)
class ClosureInventoryV2(ClosureInventoryV1):
    pass


@dataclass(frozen=True)
class NativeClosureV2(NativeClosureV1):
    identity: ClosureIdentityV2
    previous: ClosureFrontierV2 | None
    inventory: ClosureInventoryV2
    format: str = "native-closure-segment-v2"


@dataclass(frozen=True)
class ClosureCheckV2(ClosureCheckV1):
    identity: ClosureIdentityV2
    frontier: ClosureFrontierV2
    inventory: ClosureInventoryV2


_V2_POLICY = _engine._MaterialPolicy(
    2, ClosureIdentityV2, ClosureFrontierV2, ClosureInventoryV2, NativeClosureV2, ClosureCheckV2
)


def collect_native_closure_v2(
    service: LedgerService,
    *,
    archive_source: RecoveryEvidenceArchiveSourceV1,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    previous_packages: tuple[bytes, ...] = (),
    limits: ClosureLimitsV1 | None = None,
) -> bytes:
    """Collect native content from an exact current 001-062 source."""
    return _engine._collect(
        service,
        archive_source=archive_source,
        authenticator=authenticator,
        previous_packages=previous_packages,
        limits=limits,
        policy=_V2_POLICY,
    )


def decode_native_closure_v2(
    payload: bytes, *, limits: ClosureLimitsV1 | None = None
) -> NativeClosureV2:
    """Decode v2 structure only, without authenticating its content."""
    return _engine._decode(payload, limits=limits, policy=_V2_POLICY)


def check_native_closure_v2(
    packages: tuple[bytes, ...],
    *,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    expected_identity: ClosureIdentityV2,
    expected_frontier: ClosureFrontierV2,
    limits: ClosureLimitsV1 | None = None,
) -> ClosureCheckV2:
    """Independently rebuild and audit native content against fixed v2 anchors."""
    return _engine._check(
        packages,
        authenticator=authenticator,
        expected_identity=expected_identity,
        expected_frontier=expected_frontier,
        limits=limits,
        policy=_V2_POLICY,
    )


def _prepared_service_identity(service, conn, limits):
    if (
        not isinstance(service, LedgerService)
        or not isinstance(service.owner, ProductionOwner)
        or type(service.binding) is not RuntimeBinding
        or service.owner.config.instance_id != service.binding.instance_id
    ):
        fail("invalid_material", "Actual matching LedgerService and owner required")
    service.owner.assert_valid()
    witness = service.owner._active
    if witness is None or witness.connection is not conn or not conn.in_transaction:
        fail("invalid_material", "Actual active protected source transaction required")
    witness.preflight(instance_id=service.binding.instance_id)
    migration, ledger = _engine._installed_identity(_V2_POLICY)
    if service.owner.config._binding["migration_identity"] != migration:
        fail("identity_mismatch", "Source owner differs from installed 001-062 identity")
    return _engine._identity(
        asdict(
            ClosureIdentityV2(
                service.binding, service.owner.config.core_identity, migration, ledger
            )
        ),
        limits,
        _V2_POLICY,
    )


def _build_prepared_native_closure_v2(
    service: LedgerService,
    conn,
    *,
    fact: tuple,
    decision: tuple,
    event: tuple,
    archive_source: RecoveryEvidenceArchiveSourceV1,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    previous_packages: tuple[bytes, ...] = (),
    limits: ClosureLimitsV1 | None = None,
) -> tuple[bytes, ...]:
    """Audit compiler-owned planned rows only in an isolated trusted 062 DB.

    Called solely by preparation while the actual source transaction remains
    protected. The caller must retain results until source commit/close succeeds.
    """
    limits = limits_v1(limits)
    identity = _prepared_service_identity(service, conn, limits)
    _engine._packages(previous_packages, limits)
    if archive_source is None or not callable(getattr(archive_source, "export", None)):
        fail("missing_evidence", "Explicit pre-stored evidence archive source required")
    if authenticator is None or not callable(getattr(authenticator, "verify", None)):
        fail("evidence_authentication_failed", "Independent trusted authenticator required")
    source_rows = read_rows(conn, limits)
    _engine.owned_rows(source_rows, 1, len(source_rows.events))
    _engine._scope(conn, source_rows)
    _engine._schema(
        conn,
        identity,
        service.owner.config.application_schema,
        isolated=False,
        policy=_V2_POLICY,
    )
    with localcontext() as context:
        context.prec = 1024
        for row in source_rows.wire()["facts"]:
            _engine._business_fields(row["values_json"])
        audit_evidence_connection(conn, service.port, service.binding, service.legacy_verifier)
    projected = source_rows.wire()
    for name, row in (("facts", fact), ("decisions", decision), ("events", event)):
        if type(row) is not tuple or len(row) != len(COLUMNS[name]):
            fail("invalid_material", "Compiler-owned exact native row tuple required")
        projected[name].append(dict(zip(COLUMNS[name], row, strict=True)))
    rows = decode_rows(projected, limits)
    _engine.owned_rows(rows, 1, len(rows.events))
    # Export includes the as-yet-unposted action, never generated from expected.
    entries = _engine._collect_entries(rows, archive_source, authenticator, limits)
    scratch = sqlite3.connect(":memory:")
    try:
        configure_sqlite_connection(scratch)
        apply_migration_paths(scratch, _engine._trusted_paths(_V2_POLICY))
        scratch.execute("BEGIN")
        last = projected["events"][-1]
        _engine._replay_rows(scratch, rows, 1, last["seq"])
        rows, inventory = _engine._audit(scratch, identity, entries, limits, policy=_V2_POLICY)
        # Previous material must be actual source history, not a cancelled
        # preparation's virtual final event, even if the projected rows match it.
        if previous_packages:
            old, old_rows, _ = _engine._rebuild(
                previous_packages,
                authenticator=authenticator,
                expected_identity=identity,
                expected_frontier=None,
                limits=limits,
                policy=_V2_POLICY,
            )
            if old.frontier.event_seq > len(source_rows.events) or old_rows != _engine._slice_rows(
                source_rows, 1, old.frontier.event_seq, limits
            ):
                fail("chain_mismatch", "Previous chain is not actual committed source history")
        payload = _engine._segment_payload(
            rows, inventory, entries, identity, previous_packages, authenticator, limits, _V2_POLICY
        )
    except RecoveryMaterialError:
        raise
    except LifecycleError as exc:
        raise RecoveryMaterialError("invalid_material", "Projected native audit failed") from exc
    except sqlite3.Error as exc:
        raise RecoveryMaterialError("invalid_material", "Projected native replay failed") from exc
    finally:
        try:
            if scratch.in_transaction:
                scratch.rollback()
        finally:
            scratch.close()
    return previous_packages + (payload,)


def _prepared_before_v2(conn, packages, *, limits=None):
    """Derive the source inventory from this transaction's already verified chain.

    This private computation is not a checker. Preparation calls it only on the
    exact bytes returned by the preceding builder in the same source transaction.
    """
    limits = limits_v1(limits)
    rows = read_rows(conn, limits)
    required = _engine._required_evidence(rows)
    entries = tuple(
        sorted(
            (
                entry
                for payload in packages
                for entry in decode_native_closure_v2(payload, limits=limits).evidence
                if (entry.kind, entry.evidence_id) in required
            ),
            key=lambda entry: (entry.kind, entry.evidence_id),
        )
    )
    if {(entry.kind, entry.evidence_id) for entry in entries} != required or len(entries) != len(
        required
    ):
        fail("missing_evidence", "Prepared chain differs from required source archive")
    tail = rows.wire()["events"][-1] if rows.events else None
    return {
        "event_seq": tail["seq"] if tail else 0,
        "event_sha256": tail["event_sha256"] if tail else None,
        "rows_sha256": rows_hash(rows),
        "evidence_sha256": digest([entry.wire() for entry in entries]),
    }


__all__ = [
    "RecoveryMaterialError",
    "RecoveryEvidenceArchiveSourceV1",
    "RecoveryEvidenceAuthenticatorV1",
    "ClosureLimitsV1",
    "ClosureIdentityV2",
    "ClosureFrontierV2",
    "ClosureInventoryV2",
    "NativeClosureV2",
    "ClosureCheckV2",
    "collect_native_closure_v2",
    "decode_native_closure_v2",
    "check_native_closure_v2",
]
