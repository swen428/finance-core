"""Exact prepared content only; no source, finalization or recovery authority."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass

from finance_core.reconciliation.migrations import apply_migration_paths
from finance_core.sqlite_connection import configure_sqlite_connection

from . import _recovery_material_engine as _engine
from ._finalization_journal_v1 import _BODY, _verify_journal
from ._recovery_rows_v1 import (
    COLUMNS,
    ClosureLimitsV1,
    decode_rows,
    exact,
    fail,
    integer,
    limit,
    limits_v1,
    sha,
    strict_json,
    text,
)
from .contracts import LifecycleError
from .recovery_material_v1 import RecoveryEvidenceAuthenticatorV1
from .recovery_material_v2 import (
    _V2_POLICY,
    ClosureCheckV2,
    ClosureFrontierV2,
    ClosureIdentityV2,
    check_native_closure_v2,
    decode_native_closure_v2,
)


@dataclass(frozen=True)
class PreparedPackageIdentityV1:
    package_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class PreparedContentAnchorV1:
    source_identity: ClosureIdentityV2
    operation_id: str
    intent_sha256: str
    projected_frontier: ClosureFrontierV2
    packages: tuple[PreparedPackageIdentityV1, ...]
    format: str = "prepared-content-anchor-v1"


@dataclass(frozen=True)
class PreparedContentV1:
    intent_body: bytes
    packages: tuple[bytes, ...]
    anchor: PreparedContentAnchorV1
    observed_local_state: str
    content_kind: str = "PREPARED_PROJECTION"
    format: str = "prepared-content-export-v1"


def _validate_prepared_content_anchor_v1(expected, limits=None) -> PreparedContentAnchorV1:
    """Validate a separately fixed anchor, never deriving one from the content."""
    limits = limits_v1(limits)
    if (
        type(expected) is not PreparedContentAnchorV1
        or expected.format != "prepared-content-anchor-v1"
        or type(expected.source_identity) is not ClosureIdentityV2
        or type(expected.projected_frontier) is not ClosureFrontierV2
        or type(expected.packages) is not tuple
        or not expected.packages
    ):
        fail("invalid_material", "Exact independently fixed prepared content anchor required")
    text(expected.operation_id, limits, identity=True)
    sha(expected.intent_sha256)
    _engine._identity(asdict(expected.source_identity), limits, _V2_POLICY)
    _engine._frontier(asdict(expected.projected_frontier), limits, _V2_POLICY)
    limit(len(expected.packages), limits.max_packages)
    total = 0
    seen = set()
    for item in expected.packages:
        if type(item) is not PreparedPackageIdentityV1:
            fail("invalid_material", "Exact ordered package identities required")
        sha(item.package_sha256)
        integer(item.size_bytes, minimum=1)
        limit(item.size_bytes, limits.max_package_bytes)
        if item.package_sha256 in seen:
            fail("chain_mismatch", "Prepared chain cannot repeat a package")
        seen.add(item.package_sha256)
        total += item.size_bytes
    limit(total, limits.max_chain_bytes)
    if expected.packages[-1].package_sha256 != expected.projected_frontier.package_sha256:
        fail("frontier_mismatch", "Prepared anchor tail differs from projected frontier")
    return expected


def _check_prepared_content_v1(
    intent_body: bytes,
    packages: tuple[bytes, ...],
    *,
    expected: PreparedContentAnchorV1,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    limits: ClosureLimitsV1 | None = None,
) -> ClosureCheckV2:
    """Authenticate native content and reuse B1's exact intent linkage rules.

    The only reconstruction is private, temporary and in memory. It retains the
    before-prefix and one pending preparation solely for the original journal
    verifier; no owner, writable profile, outcome, ticket or connection escapes.
    """
    limits = limits_v1(limits)
    expected = _validate_prepared_content_anchor_v1(expected, limits)
    _engine._packages(packages, limits)
    body = exact(strict_json(intent_body, limits), _BODY)
    if (
        hashlib.sha256(intent_body).hexdigest() != expected.intent_sha256
        or body["operation_id"] != expected.operation_id
        or len(packages) != len(expected.packages)
        or any(
            len(payload) != anchor.size_bytes
            or hashlib.sha256(payload).hexdigest() != anchor.package_sha256
            for payload, anchor in zip(packages, expected.packages, strict=True)
        )
    ):
        fail("intent_mismatch", "Prepared bytes differ from independently fixed content anchor")
    checked = check_native_closure_v2(
        packages,
        authenticator=authenticator,
        expected_identity=expected.source_identity,
        expected_frontier=expected.projected_frontier,
        limits=limits,
    )
    scratch = sqlite3.connect(":memory:")
    try:
        configure_sqlite_connection(scratch)
        apply_migration_paths(scratch, _engine._trusted_paths(_V2_POLICY))
        rows_wire: dict[str, list[dict]] = {name: [] for name in COLUMNS}
        for package in packages:
            segment = decode_native_closure_v2(package, limits=limits)
            for name, rows in segment.rows.wire().items():
                rows_wire[name].extend(rows)
        rows = decode_rows(rows_wire, limits)
        before = exact(
            body["before"], ("event_seq", "event_sha256", "rows_sha256", "evidence_sha256")
        )
        before_seq = integer(before["event_seq"])
        limit(before_seq, limits.max_events)
        prefix = _engine._slice_rows(rows, 1, before_seq, limits)
        scratch.execute("BEGIN")
        _engine._replay_rows(scratch, prefix, 1, before_seq)
        for item, payload in zip(expected.packages, packages, strict=True):
            scratch.execute(
                "INSERT INTO l2_finalization_materials VALUES(?,?)", (item.package_sha256, payload)
            )
        scratch.execute(
            "INSERT INTO l2_finalization_intents VALUES(?,?,?,?,?,?)",
            (
                expected.operation_id,
                body["review_id"],
                body["action_evidence_id"],
                expected.intent_sha256,
                intent_body.decode("utf-8"),
                body["prepared_at"],
            ),
        )
        journal = _verify_journal(scratch, limits=limits)
        entry = journal.get(expected.operation_id)
        if len(journal) != 1 or entry is None or entry.outcome is not None:
            fail("journal_mismatch", "Detached content requires its exact single pending intent")
    except LifecycleError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        from ._recovery_rows_v1 import RecoveryMaterialError

        raise RecoveryMaterialError(
            "invalid_material", "Prepared content association failed"
        ) from exc
    finally:
        try:
            if scratch.in_transaction:
                scratch.rollback()
        finally:
            scratch.close()
    return checked
