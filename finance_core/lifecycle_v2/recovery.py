"""Restore to a held identity; promotion requires a real independently fenced writer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from finance_core.instance_admission import (
    TrustedInstanceConfig,
    inspect_instance_admission,
    prepare_production_profile,
)
from finance_core.instance_admission.authority import _change

from .contracts import LifecycleError, SnapshotCut, digest

_ISSUED: dict[int, object] = {}
_VERIFIED: set[int] = set()


@dataclass(frozen=True, init=False)
class RestoredProfile:
    config: TrustedInstanceConfig
    cut: SnapshotCut
    status: str


def prepare_restored_profile(
    config: TrustedInstanceConfig,
    database: Path,
    cut: SnapshotCut,
    *,
    operation_id: str,
    approval_ref: str,
) -> RestoredProfile:
    """Install a verified snapshot into a fresh identity without write authority."""
    if config.instance_id == cut.instance_id:
        raise LifecycleError("Restore must use a genuinely new instance identity")
    database = Path(database).resolve(strict=True)
    if hashlib.sha256(database.read_bytes()).hexdigest() != cut.database_sha256:
        raise LifecycleError("Snapshot bytes differ from exact cut")
    # Validate exact cut before preparing authority; no plain fixture adoption.
    import sqlite3

    from .reader import audit_connection

    source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        audit_connection(source)
        actual = tuple(
            tuple(r)
            for r in source.execute(
                "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
                "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) "
                "ORDER BY record_id"
            )
        )
        seq = source.execute("SELECT coalesce(max(seq),0) FROM l2_events").fetchone()[0]
        completely_empty = not any(
            source.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in (
                "l2_sources",
                "l2_proposals",
                "l2_reviews",
                "l2_deliveries",
                "l2_facts",
                "l2_decisions",
                "l2_events",
                "l2_imports",
                "l2_archives",
            )
        )
        if actual != cut.facts or seq != cut.commit_seq:
            raise LifecycleError("Snapshot record/version/hash watermark mismatch")
    finally:
        source.close()
    profile = prepare_production_profile(
        config,
        operation_id=operation_id,
        approval_ref=approval_ref,
        _restore_database=database,
        _restore_sha256=cut.database_sha256,
    )
    profile.close()
    result = object.__new__(RestoredProfile)
    object.__setattr__(result, "config", config)
    object.__setattr__(result, "cut", cut)
    object.__setattr__(result, "status", "RESTORE_HELD")
    _ISSUED[id(result)] = result
    if completely_empty and not cut.facts:
        _VERIFIED.add(id(result))
    return result


def promote_restored_profile(
    restored: RestoredProfile,
    old_config: TrustedInstanceConfig,
    *,
    operation_id: str,
    approval_ref: str,
):
    """Require actual old control-state fence plus fresh bound promotion approval.

    This API deliberately cannot infer that an offline remote machine is fenced.
    The original trusted configuration must be accessible and verifiably FENCED;
    a boolean or a self-consistent restored database never grants current rights.
    """
    if _ISSUED.get(id(restored)) is not restored:
        raise LifecycleError("Original held restore capability required")
    if old_config.instance_id != restored.cut.instance_id:
        raise LifecycleError("Old writer identity differs from snapshot")
    if id(restored) not in _VERIFIED:
        raise LifecycleError(
            "All held source/provider/action evidence must be verified before promotion"
        )
    old = _verify_fenced_cut(old_config, restored.cut)
    if inspect_instance_admission(restored.config)["state"] != "RESTORE_HELD":
        raise LifecycleError("Restore is not held")
    result = _change(
        restored.config,
        action="restore_admit",
        operation_id=operation_id,
        approval_ref=approval_ref,
        _restore_context={
            "actual_old_binding_sha256": old_config.binding_sha256,
            "fenced_state_sha256": digest(old),
        },
    )
    _VERIFIED.discard(id(restored))
    _ISSUED.pop(id(restored), None)
    return result


@dataclass(frozen=True)
class OldWriterIsolationReceipt:
    material_json: str
    signature: str


@dataclass(frozen=True)
class RecoveryAuthority:
    """Independent trusted recovery key; never recover its trust from old data."""

    namespace: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        if not self.namespace or type(self.key) is not bytes or len(self.key) < 32:
            raise LifecycleError("Independent recovery authority requires a 32-byte key")

    def issue_old_writer_isolation(
        self,
        old_config: TrustedInstanceConfig,
        cut: SnapshotCut,
        *,
        new_instance_id: str | None = None,
        clock=None,
    ):
        import hmac
        import time
        from dataclasses import asdict

        from .contracts import canonical

        state = _verify_fenced_cut(old_config, cut)
        material = canonical(
            {
                "schema": "old-writer-isolation-v2",
                "namespace": self.namespace,
                "old_binding_sha256": old_config.binding_sha256,
                "old_instance_id": old_config.instance_id,
                "cut": asdict(cut),
                "new_instance_id": new_instance_id,
                "issued_at": int((clock or time.time)()),
                "fence_admission": state["admission"],
                "fenced_state_sha256": hashlib.sha256(canonical(state).encode()).hexdigest(),
            }
        )
        signature = hmac.new(self.key, material.encode(), hashlib.sha256).hexdigest()
        return OldWriterIsolationReceipt(material, signature)

    def verify_old_writer_isolation(
        self, receipt: OldWriterIsolationReceipt, restored: RestoredProfile
    ):
        import hmac
        import json
        from dataclasses import asdict

        from .contracts import canonical

        if type(receipt) is not OldWriterIsolationReceipt:
            raise LifecycleError("Signed old writer isolation receipt required")
        expected = hmac.new(self.key, receipt.material_json.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, receipt.signature):
            raise LifecycleError("Old writer isolation signature invalid")
        material = json.loads(receipt.material_json)
        if (
            canonical(material) != receipt.material_json
            or material["schema"] != "old-writer-isolation-v2"
            or material["namespace"] != self.namespace
            or material["cut"] != json.loads(canonical(asdict(restored.cut)))
            or material["old_instance_id"] != restored.cut.instance_id
            or material["new_instance_id"] not in {None, restored.config.instance_id}
        ):
            raise LifecycleError("Old writer isolation receipt context mismatch")
        return material


def promote_with_isolation_receipt(
    restored: RestoredProfile,
    receipt: OldWriterIsolationReceipt,
    recovery_authority: RecoveryAuthority,
    *,
    operation_id: str,
    approval_ref: str,
):
    """Admit a held restore after a previously recorded real old-writer fence.

    The old root may be unavailable. This proves pre-fenced recovery only: it
    cannot assert isolation of a machine lost before its independent receipt.
    """
    if _ISSUED.get(id(restored)) is not restored:
        raise LifecycleError("Original held restore capability required")
    material = recovery_authority.verify_old_writer_isolation(receipt, restored)
    if id(restored) not in _VERIFIED:
        raise LifecycleError(
            "All held source/provider/action evidence must be verified before promotion"
        )
    if inspect_instance_admission(restored.config)["state"] != "RESTORE_HELD":
        raise LifecycleError("Restore is not held")
    result = _change(
        restored.config,
        action="restore_admit",
        operation_id=operation_id,
        approval_ref=approval_ref,
        _restore_context={
            "isolation_receipt_sha256": hashlib.sha256(receipt.material_json.encode()).hexdigest(),
            "isolation": material,
        },
    )
    _VERIFIED.discard(id(restored))
    _ISSUED.pop(id(restored), None)
    return result


def resume_held_restore(
    config: TrustedInstanceConfig,
    cut: SnapshotCut,
    receipt: OldWriterIsolationReceipt,
    recovery_authority: RecoveryAuthority,
):
    """Reissue a held capability after restart from independent signed evidence."""
    import sqlite3

    from finance_core.instance_admission.authority import (
        _control,
        _profile,
        _record,
        _state,
        _verify_record_database,
    )

    from .reader import audit_connection

    if config.instance_id == cut.instance_id:
        raise LifecycleError("Restore identity must differ from original writer")
    result = object.__new__(RestoredProfile)
    object.__setattr__(result, "config", config)
    object.__setattr__(result, "cut", cut)
    object.__setattr__(result, "status", "RESTORE_HELD")
    recovery_authority.verify_old_writer_isolation(receipt, result)
    with _control(config, shared=True) as control:
        record = _record(config, _state(config))
        if record["state"] != "RESTORE_HELD":
            raise LifecycleError("Only durable held restore can be resumed")
        _verify_record_database(config, record, control)
        with _profile(config, expected=record["db_identity"]) as profile:
            conn = sqlite3.connect(profile.live_database.as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                audit_connection(conn)
                facts = tuple(
                    tuple(r)
                    for r in conn.execute(
                        "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
                        "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) "
                        "ORDER BY record_id"
                    )
                )
                seq = conn.execute("SELECT coalesce(max(seq),0) FROM l2_events").fetchone()[0]
                if facts != cut.facts or seq != cut.commit_seq:
                    raise LifecycleError("Held restore no longer matches independently signed cut")
            finally:
                conn.close()
    _ISSUED[id(result)] = result
    return result


def verify_snapshot_evidence(database: Path, port, binding, cut: SnapshotCut, legacy_verifier=None):
    """Read-only complete semantic verification of an independently staged cut."""
    import sqlite3

    from .reader import audit_evidence_connection

    database = Path(database).resolve(strict=True)
    if hashlib.sha256(database.read_bytes()).hexdigest() != cut.database_sha256:
        raise LifecycleError("Snapshot bytes differ from expected cut")
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        result = audit_evidence_connection(conn, port, binding, legacy_verifier)
        _verify_cut_connection(conn, cut)
        return result
    finally:
        conn.close()


def _verify_cut_connection(conn, cut):
    facts = tuple(
        tuple(r)
        for r in conn.execute(
            "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
            "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) ORDER BY record_id"
        )
    )
    seq = conn.execute("SELECT coalesce(max(seq),0) FROM l2_events").fetchone()[0]
    if facts != cut.facts or seq != cut.commit_seq:
        raise LifecycleError("Restored facts differ from exact signed cut")


def verify_held_profile_evidence(restored: RestoredProfile, port, binding, legacy_verifier=None):
    """Verify actual RESTORE_HELD database with staged historical proof ports."""
    import sqlite3

    from finance_core.instance_admission.authority import _control, _profile, _record, _state

    from .reader import audit_evidence_connection

    if _ISSUED.get(id(restored)) is not restored:
        raise LifecycleError("Original held restore capability required")
    config = restored.config
    with _control(config, shared=True):
        record = _record(config, _state(config))
        if record["state"] != "RESTORE_HELD":
            raise LifecycleError("Semantic restore proof requires held state")
        with _profile(config, expected=record["db_identity"]) as profile:
            conn = sqlite3.connect(profile.live_database.as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN")
                result = audit_evidence_connection(conn, port, binding, legacy_verifier)
                _verify_cut_connection(conn, restored.cut)
            finally:
                conn.close()
    _VERIFIED.add(id(restored))
    return result


def _verify_fenced_cut(old_config: TrustedInstanceConfig, cut: SnapshotCut):
    """Shared live-original proof for both generic promotion and signed isolation.

    Hold the process's existing managed SQLite lock while selecting the fixed
    original profile's environment, then restore the caller's current environment.
    The original lifetime owner must already be released; only a new read owner
    may inspect its permanently fenced exact cut.
    """
    import os

    from finance_core.instance_admission import acquire_production_owner
    from finance_core.instance_admission.sessions import lifecycle_read_operation
    from finance_core.profile_paths import _MANAGED_SQLITE_LIFETIME_LOCK
    from finance_core.runtime_paths import RUNTIME_ROOT_ENV

    from .contracts import digest
    from .reader import audit_connection

    if old_config.instance_id != cut.instance_id:
        raise LifecycleError("Old writer identity differs from snapshot")
    with _MANAGED_SQLITE_LIFETIME_LOCK:
        previous = os.environ.get(RUNTIME_ROOT_ENV)
        os.environ[RUNTIME_ROOT_ENV] = str(Path(old_config._binding["profile_root"]) / "runtime")
        try:
            state = inspect_instance_admission(old_config)
            if state["state"] != "FENCED":
                raise LifecycleError("Actual original writer has not been permanently fenced")
            with acquire_production_owner(old_config, timeout_seconds=0) as owner:
                with lifecycle_read_operation(owner) as conn:
                    audit_connection(conn)
                    _verify_cut_connection(conn, cut)
                    if (
                        cut.core_identity != old_config.core_identity
                        or cut.migration_identity != old_config._binding["migration_identity"]
                        or cut.application_schema_digest
                        != digest(old_config._binding["application_schema"])
                    ):
                        raise LifecycleError("Fenced snapshot cut installation identity differs")
            return state
        finally:
            if previous is None:
                os.environ.pop(RUNTIME_ROOT_ENV, None)
            else:
                os.environ[RUNTIME_ROOT_ENV] = previous
