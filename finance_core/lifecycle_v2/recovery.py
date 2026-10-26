"""Bounded offline restore: one durable successor, held audit, then admission."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from finance_core.instance_admission import TrustedInstanceConfig, prepare_production_profile
from finance_core.instance_admission.authority import (
    OWNER_LOCK_FILENAME,
    PENDING_FILENAME,
    STATE_FILENAME,
    InstanceAdmissionError,
    _approve,
    _canonical,
    _close_sqlite_handle,
    _control,
    _decode,
    _digest,
    _exists,
    _Lock,
    _logical_database_digest,
    _profile,
    _publish,
    _read,
    _record,
    _recovery_designation,
    _state,
    _transition,
    _validate_state,
)
from finance_core.instance_admission.schema_contract import validate_application_database
from finance_core.profile_gate import _acquire as _acquire_profile_gate
from finance_core.profile_paths import _MANAGED_SQLITE_LIFETIME_LOCK
from finance_core.runtime_paths import RUNTIME_ROOT_ENV
from finance_core.sqlite_connection import configure_sqlite_connection

from .contracts import (
    LifecycleError,
    RuntimeBinding,
    SnapshotCut,
    canonical,
    cut_ref,
    digest,
)

# Keep uncertain SQLite and every cooperative lease live until process exit.
_RECOVERY_CLOSE_UNKNOWN: list[tuple[object, ...]] = []


@dataclass(frozen=True)
class RestoredProfile:
    config: TrustedInstanceConfig
    cut: SnapshotCut
    new_binding: RuntimeBinding
    restore_prepare_operation_id: str
    restore_prepare_entry_sha256: str
    status: str = "RESTORE_HELD"


@dataclass(frozen=True)
class RestoreDesignation:
    material: dict[str, Any]
    operation_id: str
    approval_ref: str
    entry_sha256: str


@dataclass(frozen=True)
class OldWriterIsolationReceipt:
    material_json: str
    signature: str


def _entry(state: dict[str, Any], operation_id: str, action: str) -> dict[str, Any]:
    matches = [
        item
        for item in state["history"]
        if item["operation_id"] == operation_id and item["action"] == action
    ]
    if len(matches) != 1:
        raise LifecycleError("Exact recovery history entry is unavailable")
    return matches[0]


def _validate_recovery_pending(
    config: TrustedInstanceConfig,
    journal: dict[str, Any],
    *,
    action: str,
    operation_id: str,
    approval_ref: str,
    context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Bind an existing pending document to its full history and approval."""
    try:
        if (
            type(journal) is not dict
            or set(journal)
            != {
                "format",
                "binding",
                "action",
                "operation_id",
                "approval_ref",
                "approval_digest",
                "before_sha256",
                "after",
                "context",
            }
            or journal["format"] != "instance-transition-v1"
            or journal["binding"] != config.binding_sha256
            or journal["action"] != action
            or journal["operation_id"] != operation_id
            or journal["approval_ref"] != approval_ref
            or journal["context"] != context
        ):
            raise LifecycleError("Pending recovery request differs")
        after = journal["after"]
        if type(after) is not dict or type(after.get("version")) is not int or after["version"] < 1:
            raise LifecycleError("Pending recovery successor is malformed")
        before = _validate_state(after, prefix_version=after["version"] - 1)
        suffix = after["history"][-1]
        if (
            _digest(before) != journal["before_sha256"]
            or _read(config, STATE_FILENAME) not in (before, after)
            or suffix["action"] != action
            or suffix["operation_id"] != operation_id
            or suffix["approval_ref"] != approval_ref
            or suffix["binding"] != config.binding_sha256
            or suffix.get("recovery_context") != context
            or suffix["context_digest"] != _digest(context)
        ):
            raise LifecycleError("Pending recovery predecessor/history differs")
        expected = _approve(
            config,
            approval_ref,
            action,
            {
                "operation_id": operation_id,
                "before_sha256": _digest(before),
                "context": context,
            },
        )
        if expected != suffix["approval_digest"] or expected != journal["approval_digest"]:
            raise LifecycleError("Pending recovery approval differs")
        return before, after, suffix
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise LifecycleError("Pending recovery journal is invalid") from exc


def _verify_cut_connection(conn: sqlite3.Connection, cut: SnapshotCut) -> None:
    if type(cut) is not SnapshotCut:
        raise LifecycleError("Exact v3 snapshot cut required")
    actual = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
            "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) ORDER BY record_id"
        )
    )
    seq = conn.execute("SELECT coalesce(max(seq),0) FROM l2_events").fetchone()[0]
    if actual != cut.facts or seq != cut.commit_seq:
        raise LifecycleError("Recovery fact watermark differs from complete cut")
    if _logical_database_digest(conn) != cut.logical_database_sha256:
        raise LifecycleError("Recovery database content differs from complete cut")


def _target_from_state(
    restored: RestoredProfile,
    state: dict[str, Any],
    *,
    allow_completed: bool = False,
):
    config = restored.config
    if (
        type(restored.cut) is not SnapshotCut
        or restored.new_binding.instance_id != config.instance_id
    ):
        raise LifecycleError("Restore target identity is invalid")
    record = _record(config, state)
    if (
        record["state"] != "RESTORE_HELD"
        and not (
            allow_completed
            and record["state"] in {"ACTIVE", "FENCED"}
            and any(
                entry["action"] == "restore_admit"
                and entry["binding"] == config.binding_sha256
                and "recovery_context" in entry
                for entry in state["history"]
            )
        )
    ) or (record["state"] == "RESTORE_HELD" and record["maintenance"] is not None):
        raise LifecycleError("Target is not an exact held or completed restore")
    prepare = _entry(state, restored.restore_prepare_operation_id, "restore_prepare")
    context = prepare.get("recovery_context")
    if (
        context is None
        or prepare["entry_sha256"] != restored.restore_prepare_entry_sha256
        or context["cut_ref"] != cut_ref(restored.cut)
        or context["runtime_binding"] != asdict(restored.new_binding)
        or context["target_binding_sha256"] != config.binding_sha256
        or prepare["record_identity"]
        != _digest(
            {
                "binding": record["binding"],
                "db_identity": record["db_identity"],
                "ledger_sha256": record["ledger_sha256"],
            }
        )
    ):
        raise LifecycleError("Target lacks exact durable v3 preparation")
    target = {
        "binding": _decode(_canonical(config._binding)),
        "binding_sha256": config.binding_sha256,
        "runtime_binding": asdict(restored.new_binding),
        "db_identity": list(record["db_identity"]),
        "restore_prepare_operation_id": prepare["operation_id"],
        "restore_prepare_entry_sha256": prepare["entry_sha256"],
        "record_identity": prepare["record_identity"],
    }
    return target, record


def _held_description(
    restored: RestoredProfile,
    *,
    allow_pending: bool = False,
    allow_completed: bool = False,
):
    config = restored.config
    with _control(config, shared=True):
        if allow_pending and _exists(config, PENDING_FILENAME):
            journal = _read(config, PENDING_FILENAME)
            if journal.get("action") != "restore_admit":
                raise LifecycleError("Only exact pending restore admission can resume")
            state = _validate_state(
                journal["after"], prefix_version=journal["after"]["version"] - 1
            )
        else:
            state = _state(config)
        return _target_from_state(restored, state, allow_completed=allow_completed)


def prepare_restored_profile(
    config: TrustedInstanceConfig,
    database: Path,
    cut: SnapshotCut,
    *,
    new_binding: RuntimeBinding,
    operation_id: str,
    approval_ref: str,
) -> RestoredProfile:
    if (
        type(cut) is not SnapshotCut
        or type(new_binding) is not RuntimeBinding
        or config.instance_id == cut.instance_id
        or new_binding.instance_id != config.instance_id
        or new_binding.generation == cut.generation
    ):
        raise LifecycleError("Restore needs a new exact offline instance and generation")
    database = Path(database).resolve(strict=True)
    if hashlib.sha256(database.read_bytes()).hexdigest() != cut.database_sha256:
        raise LifecycleError("Snapshot bytes differ from exact cut")
    from .reader import audit_connection

    source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute("BEGIN")
        audit_connection(source)
        _verify_cut_connection(source, cut)
    finally:
        source.close()
    context = {
        "format": "restore-prepare-context-v1",
        "cut_ref": cut_ref(cut),
        "runtime_binding": asdict(new_binding),
        "target_binding_sha256": config.binding_sha256,
    }
    profile = prepare_production_profile(
        config,
        operation_id=operation_id,
        approval_ref=approval_ref,
        _restore_database=database,
        _restore_sha256=cut.database_sha256,
        _restore_context=context,
    )
    profile.close()
    with _control(config, shared=True):
        state = _state(config)
        prepare = _entry(state, operation_id, "restore_prepare")
    result = RestoredProfile(config, cut, new_binding, operation_id, prepare["entry_sha256"])
    _held_description(result)
    return result


def _protected_database_transition(
    config: TrustedInstanceConfig,
    owner: _Lock,
    control: _Lock,
    record: dict[str, Any],
    *,
    write: bool,
    build: Callable[[sqlite3.Connection], dict[str, Any]],
) -> dict[str, Any]:
    """Hold owner/control/profile/gate through actual SQLite close and publish."""
    profile = None
    gate = None
    conn = None
    lifetime = False
    sqlite_began = False
    unknown = False
    previous_runtime = None
    runtime_switched = False
    try:
        _MANAGED_SQLITE_LIFETIME_LOCK.__enter__()
        lifetime = True
        previous_runtime = os.environ.get(RUNTIME_ROOT_ENV)
        os.environ[RUNTIME_ROOT_ENV] = str(Path(config._binding["profile_root"]) / "runtime")
        runtime_switched = True
        profile = _profile(config, expected=record["db_identity"])
        # The retained exclusive gate also excludes cooperative maintenance
        # writers in the close-to-publication gap. Full audits may exceed the
        # short public export-cut hold limit, so this internal lease has none.
        gate = _acquire_profile_gate(
            profile, fcntl.LOCK_EX, timeout_seconds=5, max_hold_seconds=None
        )
        _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
        sqlite_began = True
        conn = sqlite3.connect(
            profile.live_database.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True
        )
        configure_sqlite_connection(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        validate_application_database(
            conn,
            config._binding["migration_identity"],
            config.application_schema,
            record["ledger_sha256"],
        )
        journal = build(conn)
        try:
            conn.rollback()
            _close_sqlite_handle(conn)
            conn = None
        except BaseException as exc:
            unknown = True
            owner._retain = control._retain = profile._retain = True
            _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
            _RECOVERY_CLOSE_UNKNOWN.append((owner, control, profile, gate, conn))
            raise InstanceAdmissionError(
                "Recovery SQLite actual close unknown; pending retained"
            ) from exc
        _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
        sqlite_began = False
        profile.revalidate()
        gate.assert_valid()
        control._assert_file_valid()
        owner._assert_file_valid()
        return _publish(config, journal)
    finally:
        if not unknown:
            if conn is not None:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                    _close_sqlite_handle(conn)
                except BaseException as exc:
                    owner._retain = control._retain = True
                    if profile is not None:
                        profile._retain = True
                    _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
                    _RECOVERY_CLOSE_UNKNOWN.append((owner, control, profile, gate, conn))
                    raise InstanceAdmissionError("Recovery SQLite actual close unknown") from exc
            try:
                if sqlite_began:
                    _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
                if gate is not None:
                    gate.close()
                if profile is not None:
                    profile.close()
            finally:
                if runtime_switched:
                    if previous_runtime is None:
                        os.environ.pop(RUNTIME_ROOT_ENV, None)
                    else:
                        os.environ[RUNTIME_ROOT_ENV] = previous_runtime
                if lifetime:
                    _MANAGED_SQLITE_LIFETIME_LOCK.__exit__()


def _designation_from_entry(entry: dict[str, Any]) -> RestoreDesignation:
    return RestoreDesignation(
        entry["recovery_context"]["designation"],
        entry["operation_id"],
        entry["approval_ref"],
        entry["entry_sha256"],
    )


def resume_restore_prepare(
    config: TrustedInstanceConfig,
    cut: SnapshotCut,
    new_binding: RuntimeBinding,
    *,
    operation_id: str,
    approval_ref: str,
) -> RestoredProfile:
    """Complete only the exact v3 held preparation after a lost return/publish."""
    if (
        type(cut) is not SnapshotCut
        or type(new_binding) is not RuntimeBinding
        or config.instance_id == cut.instance_id
        or new_binding.instance_id != config.instance_id
        or new_binding.generation == cut.generation
    ):
        raise LifecycleError("Restore prepare resume has invalid exact identities")
    context = {
        "format": "restore-prepare-context-v1",
        "cut_ref": cut_ref(cut),
        "runtime_binding": asdict(new_binding),
        "target_binding_sha256": config.binding_sha256,
    }
    owner = _Lock(config, OWNER_LOCK_FILENAME)
    try:
        owner.acquire(fcntl.LOCK_EX, 0)
        with _control(config) as control:
            if not _exists(config, PENDING_FILENAME):
                state = _state(config)
                entry = _entry(state, operation_id, "restore_prepare")
                if (
                    entry["binding"] != config.binding_sha256
                    or entry["approval_ref"] != approval_ref
                    or entry.get("recovery_context") != context
                    or config.approval_verifier(
                        approval_ref, "restore_prepare", entry["approval_digest"]
                    )
                    is not True
                ):
                    raise LifecycleError("Completed held preparation differs from original request")
                result = RestoredProfile(
                    config, cut, new_binding, operation_id, entry["entry_sha256"]
                )
            else:
                journal = _read(config, PENDING_FILENAME)
                if (
                    journal.get("action") != "restore_prepare"
                    or journal.get("operation_id") != operation_id
                    or journal.get("approval_ref") != approval_ref
                    or journal.get("binding") != config.binding_sha256
                    or journal.get("context") != context
                ):
                    raise LifecycleError("Pending held preparation is not the exact request")
                _, after, _ = _validate_recovery_pending(
                    config,
                    journal,
                    action="restore_prepare",
                    operation_id=operation_id,
                    approval_ref=approval_ref,
                    context=context,
                )
                record = _record(config, after)
                if record["state"] != "RESTORE_HELD":
                    raise LifecycleError("Pending preparation is not held")
                from .reader import audit_connection

                def build(conn):
                    audit_connection(conn)
                    _verify_cut_connection(conn, cut)
                    return journal

                published = _protected_database_transition(
                    config, owner, control, record, write=False, build=build
                )
                entry = _entry(published, operation_id, "restore_prepare")
                result = RestoredProfile(
                    config, cut, new_binding, operation_id, entry["entry_sha256"]
                )
    finally:
        owner.close()
    verify_held_profile_evidence_content(result)
    return result


def designate_restore_successor(
    old_config: TrustedInstanceConfig,
    restored: RestoredProfile,
    *,
    operation_id: str,
    approval_ref: str,
) -> RestoreDesignation:
    target = None
    if old_config.control_root == restored.config.control_root:
        # A designation pending in this shared root also blocks ordinary target
        # reads. Only this exact coordinator may use its authenticated predecessor.
        with _control(old_config, shared=True):
            if _exists(old_config, PENDING_FILENAME):
                restored.config.assert_valid()
                journal = _read(old_config, PENDING_FILENAME)
                predecessor, _, _ = _validate_recovery_pending(
                    old_config,
                    journal,
                    action="recovery_designate",
                    operation_id=operation_id,
                    approval_ref=approval_ref,
                    context=journal.get("context", {}),
                )
                target, _ = _target_from_state(restored, predecessor)
    if target is None:
        target, _ = _held_description(restored, allow_completed=True)
    cut = restored.cut
    if (
        old_config.instance_id != cut.instance_id
        or old_config.binding_sha256 != cut.source_binding_sha256
        or old_config.core_identity != cut.core_identity
        or old_config._binding["migration_identity"] != cut.migration_identity
        or digest(old_config._binding["application_schema"]) != cut.application_schema_digest
    ):
        raise LifecycleError("Old writer does not match complete source cut")
    owner = _Lock(old_config, OWNER_LOCK_FILENAME)
    try:
        owner.acquire(fcntl.LOCK_EX, 0)
        with _control(old_config) as control:
            pending = (
                _read(old_config, PENDING_FILENAME)
                if _exists(old_config, PENDING_FILENAME)
                else None
            )
            if pending is not None:
                if (
                    pending.get("action") != "recovery_designate"
                    or pending.get("operation_id") != operation_id
                    or pending.get("approval_ref") != approval_ref
                ):
                    raise LifecycleError("An exact recovery designation remains pending")
                after = pending["after"]
                before = _validate_state(after, prefix_version=after["version"] - 1)
                if _read(old_config, STATE_FILENAME) not in (before, after):
                    raise LifecycleError("Pending designation predecessor differs")
                state = before
            else:
                state = _state(old_config)
                previous = [
                    entry
                    for entry in state["history"]
                    if entry["action"] == "recovery_designate"
                    and entry["binding"] == old_config.binding_sha256
                ]
                if previous:
                    existing = previous[0]
                    designation = _designation_from_entry(existing)
                    if (
                        existing["operation_id"] == operation_id
                        and existing["approval_ref"] == approval_ref
                        and designation.material["cut_ref"] == cut_ref(cut)
                        and designation.material["target"] == target
                        and old_config.approval_verifier(
                            approval_ref, "recovery_designate", existing["approval_digest"]
                        )
                        is True
                    ):
                        return designation
                    raise LifecycleError("Old binding already designated one successor")
            record = _record(old_config, state)
            if record["state"] != "FENCED" or state["current"] != old_config.binding_sha256:
                raise LifecycleError("Old writer is not the exact fenced current authority")
            fences = [
                entry
                for entry in state["history"]
                if entry["action"] == "fence" and entry["binding"] == old_config.binding_sha256
            ]
            if len(fences) != 1:
                raise LifecycleError("Exact old fence history is unavailable")
            material = {
                "format": "restore-designation-v1",
                "old_binding_sha256": old_config.binding_sha256,
                "old_instance_id": old_config.instance_id,
                "fence_admission": record["admission"],
                "fence_entry_sha256": fences[0]["entry_sha256"],
                "cut_ref": cut_ref(cut),
                "target": target,
            }
            context = {"format": "restore-designation-context-v1", "designation": material}
            from .reader import audit_connection

            def build(conn):
                audit_connection(conn)
                _verify_cut_connection(conn, cut)
                if pending is not None:
                    verified_before, _, _ = _validate_recovery_pending(
                        old_config,
                        pending,
                        action="recovery_designate",
                        operation_id=operation_id,
                        approval_ref=approval_ref,
                        context=context,
                    )
                    if verified_before != state:
                        raise LifecycleError("Pending designation predecessor changed")
                    return pending
                after = _decode(_canonical(state))
                return _transition(
                    old_config,
                    state,
                    after,
                    action="recovery_designate",
                    operation_id=operation_id,
                    approval_ref=approval_ref,
                    context=context,
                    defer_publish=True,
                )

            published = _protected_database_transition(
                old_config, owner, control, record, write=False, build=build
            )
            return _designation_from_entry(_entry(published, operation_id, "recovery_designate"))
    finally:
        owner.close()


def resume_restore_designation(
    old_config: TrustedInstanceConfig,
    restored: RestoredProfile,
    *,
    operation_id: str,
    approval_ref: str,
) -> RestoreDesignation:
    """Recheck and publish one exact designation pending under its original approval."""
    return designate_restore_successor(
        old_config, restored, operation_id=operation_id, approval_ref=approval_ref
    )


@dataclass(frozen=True)
class RecoveryAuthority:
    namespace: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        if (
            type(self.namespace) is not str
            or not 0 < len(self.namespace) <= 256
            or type(self.key) is not bytes
            or len(self.key) < 32
        ):
            raise LifecycleError("Independent recovery authority requires a 32-byte key")

    def issue_old_writer_isolation(
        self,
        old_config: TrustedInstanceConfig,
        restored: RestoredProfile,
        *,
        operation_id: str,
        approval_ref: str,
    ) -> OldWriterIsolationReceipt:
        designation = designate_restore_successor(
            old_config, restored, operation_id=operation_id, approval_ref=approval_ref
        )
        material = canonical(
            {
                "schema": "old-writer-isolation-v3",
                "namespace": self.namespace,
                "designation": designation.material,
                "designation_entry_sha256": designation.entry_sha256,
            }
        )
        return OldWriterIsolationReceipt(
            material, hmac.new(self.key, material.encode(), hashlib.sha256).hexdigest()
        )

    def verify_old_writer_isolation(
        self,
        receipt: OldWriterIsolationReceipt,
        restored: RestoredProfile,
    ) -> RestoreDesignation:
        if type(receipt) is not OldWriterIsolationReceipt:
            raise LifecycleError("Signed v3 old writer isolation receipt required")
        if (
            type(receipt.material_json) is not str
            or type(receipt.signature) is not str
            or len(receipt.signature) != 64
            or len(receipt.material_json) > 65536
        ):
            raise LifecycleError("Signed isolation receipt shape is invalid")
        expected = hmac.new(self.key, receipt.material_json.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, receipt.signature):
            raise LifecycleError("Old writer isolation signature invalid")
        import json

        try:
            material = json.loads(receipt.material_json)
        except (ValueError, TypeError) as exc:
            raise LifecycleError("Signed isolation receipt JSON is invalid") from exc
        target, _ = _held_description(restored, allow_pending=True, allow_completed=True)
        if (
            type(material) is not dict
            or set(material) != {"schema", "namespace", "designation", "designation_entry_sha256"}
            or material["schema"] != "old-writer-isolation-v3"
            or material["namespace"] != self.namespace
            or not _recovery_designation(material["designation"])
            or type(material["designation_entry_sha256"]) is not str
            or len(material["designation_entry_sha256"]) != 64
            or canonical(material) != receipt.material_json
            or material["designation"]["cut_ref"] != cut_ref(restored.cut)
            or material["designation"]["target"] != target
            or material["designation"]["old_binding_sha256"] != restored.cut.source_binding_sha256
            or material["designation"]["old_instance_id"] != restored.cut.instance_id
        ):
            raise LifecycleError("Signed isolation does not bind exact held target and cut")
        return RestoreDesignation(
            material["designation"], "", "", material["designation_entry_sha256"]
        )


def _verify_held_evidence(conn, restored, port, binding, legacy_verifier):
    from .reader import audit_evidence_connection

    if binding != restored.new_binding:
        raise LifecycleError("Evidence binding differs from designated target")
    result = audit_evidence_connection(conn, port, binding, legacy_verifier)
    _verify_cut_connection(conn, restored.cut)
    return result


@contextmanager
def _held_read_connection(restored: RestoredProfile):
    """One diagnostic HELD read snapshot with fixed profile and actual close."""
    config = restored.config
    with _control(config, shared=True) as control:
        state = _state(config)
        _, record = _target_from_state(restored, state)
        profile = None
        gate = None
        conn = None
        lifetime = False
        sqlite_began = False
        unknown = False
        previous_runtime = None
        runtime_switched = False
        try:
            _MANAGED_SQLITE_LIFETIME_LOCK.__enter__()
            lifetime = True
            previous_runtime = os.environ.get(RUNTIME_ROOT_ENV)
            os.environ[RUNTIME_ROOT_ENV] = str(Path(config._binding["profile_root"]) / "runtime")
            runtime_switched = True
            profile = _profile(config, expected=record["db_identity"])
            gate = _acquire_profile_gate(
                profile, fcntl.LOCK_EX, timeout_seconds=5, max_hold_seconds=None
            )
            _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
            sqlite_began = True
            conn = sqlite3.connect(profile.live_database.as_uri() + "?mode=ro", uri=True)
            configure_sqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            validate_application_database(
                conn,
                config._binding["migration_identity"],
                config.application_schema,
                record["ledger_sha256"],
            )
            yield conn
        finally:
            if conn is not None:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                    _close_sqlite_handle(conn)
                except BaseException as exc:
                    unknown = True
                    control._retain = True
                    if profile is not None:
                        profile._retain = True
                    _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
                    _RECOVERY_CLOSE_UNKNOWN.append((control, profile, gate, conn))
                    raise InstanceAdmissionError("Held diagnostic SQLite close unknown") from exc
            if not unknown:
                try:
                    if sqlite_began:
                        _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
                    if gate is not None:
                        gate.close()
                    if profile is not None:
                        profile.close()
                finally:
                    if runtime_switched:
                        if previous_runtime is None:
                            os.environ.pop(RUNTIME_ROOT_ENV, None)
                        else:
                            os.environ[RUNTIME_ROOT_ENV] = previous_runtime
                    if lifetime:
                        _MANAGED_SQLITE_LIFETIME_LOCK.__exit__()


def verify_held_profile_evidence(restored: RestoredProfile, port, binding, legacy_verifier=None):
    """Read-only diagnostic; no result grants later admission authority."""
    with _held_read_connection(restored) as conn:
        return _verify_held_evidence(conn, restored, port, binding, legacy_verifier)


def verify_snapshot_evidence(database: Path, port, binding, cut: SnapshotCut, legacy_verifier=None):
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


def _live_designation(old_config, restored, *, operation_id, approval_ref):
    designation = designate_restore_successor(
        old_config, restored, operation_id=operation_id, approval_ref=approval_ref
    )
    return designation, None


def _promotion_context(restored, target, designation, receipt_sha256):
    return {
        "format": "restore-admit-context-v1",
        "cut_ref": cut_ref(restored.cut),
        "target_sha256": digest(target),
        "designation_entry_sha256": designation.entry_sha256,
        "designation_sha256": digest(designation.material),
        "receipt_sha256": receipt_sha256,
    }


def _promote(
    restored,
    designation,
    *,
    operation_id,
    approval_ref,
    port,
    binding,
    legacy_verifier,
    receipt_sha256=None,
):
    target, _ = _held_description(restored, allow_pending=True, allow_completed=True)
    if (
        designation.material["target"] != target
        or designation.material["cut_ref"] != cut_ref(restored.cut)
        or designation.material["old_binding_sha256"] != restored.cut.source_binding_sha256
    ):
        raise LifecycleError("Designation does not bind the exact target")
    context = _promotion_context(restored, target, designation, receipt_sha256)
    config = restored.config
    owner = _Lock(config, OWNER_LOCK_FILENAME)
    try:
        owner.acquire(fcntl.LOCK_EX, 0)
        with _control(config) as control:
            pending = _read(config, PENDING_FILENAME) if _exists(config, PENDING_FILENAME) else None
            if pending is None:
                state = _state(config)
                completed = [
                    entry
                    for entry in state["history"]
                    if entry["action"] == "restore_admit"
                    and entry["operation_id"] == operation_id
                    and entry["binding"] == config.binding_sha256
                ]
                if completed:
                    entry = completed[0]
                    if (
                        entry["approval_ref"] != approval_ref
                        or entry.get("recovery_context") != context
                        or binding != restored.new_binding
                        or config.approval_verifier(
                            approval_ref, "restore_admit", entry["approval_digest"]
                        )
                        is not True
                    ):
                        raise LifecycleError("Completed restore admission context differs")
                    return _decode(_canonical(_record(config, state)))
                before = state
            else:
                before, _, _ = _validate_recovery_pending(
                    config,
                    pending,
                    action="restore_admit",
                    operation_id=operation_id,
                    approval_ref=approval_ref,
                    context=context,
                )
            record = _record(config, before)
            locked_target, _ = _target_from_state(restored, before, allow_completed=True)
            if locked_target != target:
                raise LifecycleError("Target changed before protected recovery admission")
            if record["state"] != "RESTORE_HELD" or record["maintenance"] is not None:
                raise LifecycleError("Only exact held target can become active")
            from .reader import audit_connection

            def build(conn):
                audit_connection(conn)
                _verify_held_evidence(conn, restored, port, binding, legacy_verifier)
                if pending is not None:
                    return pending
                after = _decode(_canonical(before))
                after_record = _record(config, after)
                if (
                    after["current"] is not None
                    and after["instances"][after["current"]]["state"] != "FENCED"
                ):
                    raise LifecycleError("Another current instance remains active")
                after_record["state"], after_record["admission"] = "ACTIVE", operation_id
                after["current"] = config.binding_sha256
                return _transition(
                    config,
                    before,
                    after,
                    action="restore_admit",
                    operation_id=operation_id,
                    approval_ref=approval_ref,
                    context=context,
                    defer_publish=True,
                )

            published = _protected_database_transition(
                config, owner, control, record, write=True, build=build
            )
            return _decode(_canonical(_record(config, published)))
    finally:
        owner.close()


def promote_restored_profile(
    restored: RestoredProfile,
    old_config: TrustedInstanceConfig,
    *,
    designation_operation_id: str,
    designation_approval_ref: str,
    operation_id: str,
    approval_ref: str,
    port,
    binding: RuntimeBinding,
    legacy_verifier=None,
):
    designation, _ = _live_designation(
        old_config,
        restored,
        operation_id=designation_operation_id,
        approval_ref=designation_approval_ref,
    )
    return _promote(
        restored,
        designation,
        operation_id=operation_id,
        approval_ref=approval_ref,
        port=port,
        binding=binding,
        legacy_verifier=legacy_verifier,
    )


def promote_with_isolation_receipt(
    restored: RestoredProfile,
    receipt: OldWriterIsolationReceipt,
    recovery_authority: RecoveryAuthority,
    *,
    operation_id: str,
    approval_ref: str,
    port,
    binding: RuntimeBinding,
    legacy_verifier=None,
):
    designation = recovery_authority.verify_old_writer_isolation(receipt, restored)
    return _promote(
        restored,
        designation,
        operation_id=operation_id,
        approval_ref=approval_ref,
        port=port,
        binding=binding,
        legacy_verifier=legacy_verifier,
        receipt_sha256=digest(asdict(receipt)),
    )


def _uses_receipt_route(receipt, recovery_authority, old_config):
    if old_config is not None:
        if receipt is not None or recovery_authority is not None:
            raise LifecycleError("Recovery requires exactly one complete route")
        return False
    if receipt is None or recovery_authority is None:
        raise LifecycleError("Recovery requires exactly one complete route")
    return True


def _verify_recovery_entry_approval(config, state, entry):
    predecessor = _validate_state(state, prefix_version=entry["version"] - 1)
    expected = _approve(
        config,
        entry["approval_ref"],
        entry["action"],
        {
            "operation_id": entry["operation_id"],
            "before_sha256": _digest(predecessor),
            "context": entry["recovery_context"],
        },
    )
    if expected != entry["approval_digest"]:
        raise LifecycleError("Recovery history approval differs from exact predecessor")


def resume_held_restore(
    config: TrustedInstanceConfig,
    cut: SnapshotCut,
    receipt: OldWriterIsolationReceipt | None = None,
    recovery_authority: RecoveryAuthority | None = None,
    *,
    old_config: TrustedInstanceConfig | None = None,
) -> RestoredProfile:
    """Reconstruct one prepared target; its observed status grants no authority."""
    receipt_route = _uses_receipt_route(receipt, recovery_authority, old_config)
    if type(cut) is not SnapshotCut:
        raise LifecycleError("Exact v3 snapshot cut required")
    with _control(config, shared=True):
        pending = _read(config, PENDING_FILENAME) if _exists(config, PENDING_FILENAME) else None
        if pending is not None:
            if not {"operation_id", "approval_ref", "context"}.issubset(pending):
                raise LifecycleError("Pending reconstruction locator is malformed")
            state, _, promotion = _validate_recovery_pending(
                config,
                pending,
                action="restore_admit",
                operation_id=pending["operation_id"],
                approval_ref=pending["approval_ref"],
                context=pending["context"],
            )
            observed = _read(config, STATE_FILENAME)
            status = "RESTORE_ADMIT_PENDING"
        else:
            state = observed = _state(config)
            promotion = None
            status = _record(config, state)["state"]
            if status not in {"RESTORE_HELD", "ACTIVE", "FENCED"}:
                raise LifecycleError("Only an originally held restore can be reconstructed")
        entries = [
            entry
            for entry in state["history"]
            if entry["action"] == "restore_prepare"
            and entry["binding"] == config.binding_sha256
            and "recovery_context" in entry
        ]
        if len(entries) != 1 or entries[0]["recovery_context"]["cut_ref"] != cut_ref(cut):
            raise LifecycleError("Held preparation lacks exact v3 cut")
        prepare = entries[0]
        _verify_recovery_entry_approval(config, state, prepare)
        new_binding = RuntimeBinding(**prepare["recovery_context"]["runtime_binding"])
        if config.instance_id == cut.instance_id or new_binding.generation == cut.generation:
            raise LifecycleError("Reconstruction requires the exact new instance and generation")
        restored = RestoredProfile(
            config, cut, new_binding, prepare["operation_id"], prepare["entry_sha256"], status
        )
        target, record = _target_from_state(restored, state, allow_completed=True)
        if pending is None and status != "RESTORE_HELD":
            promotion = _entry(state, record["admission"], "restore_admit")
            if promotion["binding"] != config.binding_sha256:
                raise LifecycleError("Completed restore admission belongs to another target")
            _verify_recovery_entry_approval(config, state, promotion)

    if receipt_route:
        assert recovery_authority is not None and receipt is not None
        designation = recovery_authority.verify_old_writer_isolation(receipt, restored)
        receipt_sha256 = digest(asdict(receipt))
    else:
        designation = _resume_live_designation(
            restored,
            old_config,
            operation_id=promotion["operation_id"] if promotion is not None else None,
            approval_ref=promotion["approval_ref"] if promotion is not None else None,
        )
        receipt_sha256 = None
    if promotion is not None and promotion.get("recovery_context") != _promotion_context(
        restored, target, designation, receipt_sha256
    ):
        raise LifecycleError("Reconstructed promotion context differs from exact route")

    # Separate-root designation lookup cannot lend a stale target observation.
    # Pin the actual profile/inode here without opening or auditing SQLite.
    with _control(config, shared=True):
        current_pending = (
            _read(config, PENDING_FILENAME) if _exists(config, PENDING_FILENAME) else None
        )
        if current_pending != pending or _read(config, STATE_FILENAME) != observed:
            raise LifecycleError("Restore authority changed during reconstruction")
        with _MANAGED_SQLITE_LIFETIME_LOCK:
            previous_runtime = os.environ.get(RUNTIME_ROOT_ENV)
            os.environ[RUNTIME_ROOT_ENV] = str(Path(config._binding["profile_root"]) / "runtime")
            try:
                profile = _profile(config, expected=record["db_identity"])
                profile.close()
            finally:
                if previous_runtime is None:
                    os.environ.pop(RUNTIME_ROOT_ENV, None)
                else:
                    os.environ[RUNTIME_ROOT_ENV] = previous_runtime
    if status == "RESTORE_HELD":
        verify_held_profile_evidence_content(restored)
    return restored


def verify_held_profile_evidence_content(restored: RestoredProfile) -> None:
    from .reader import audit_connection

    with _held_read_connection(restored) as conn:
        audit_connection(conn)
        _verify_cut_connection(conn, restored.cut)


def _resume_live_designation(restored, old_config, *, operation_id, approval_ref):
    """Recover an existing designation without treating pending state as ordinary."""
    config = restored.config
    with _control(config, shared=True):
        pending = _read(config, PENDING_FILENAME) if _exists(config, PENDING_FILENAME) else None
        if pending is None:
            state = _state(config)
        else:
            state, _, _ = _validate_recovery_pending(
                config,
                pending,
                action="restore_admit",
                operation_id=operation_id,
                approval_ref=approval_ref,
                context=pending.get("context"),
            )
        target, _ = _target_from_state(restored, state, allow_completed=True)
    cut = restored.cut
    if (
        old_config.instance_id != cut.instance_id
        or old_config.binding_sha256 != cut.source_binding_sha256
        or old_config.core_identity != cut.core_identity
        or old_config._binding["migration_identity"] != cut.migration_identity
        or digest(old_config._binding["application_schema"]) != cut.application_schema_digest
    ):
        raise LifecycleError("Old writer does not match complete source cut")
    # Never hold two control roots simultaneously. Shared-root pending belongs
    # to the target, so only its authenticated predecessor can supply history.
    with _control(old_config, shared=True):
        if pending is not None and old_config.control_root == config.control_root:
            if _read(old_config, PENDING_FILENAME) != pending:
                raise LifecycleError("Pending restore admission changed during resume")
            state, _, _ = _validate_recovery_pending(
                config,
                pending,
                action="restore_admit",
                operation_id=operation_id,
                approval_ref=approval_ref,
                context=pending["context"],
            )
        else:
            state = _state(old_config)
        record = _record(old_config, state)
        matches = [
            entry
            for entry in state["history"]
            if entry["action"] == "recovery_designate"
            and entry["binding"] == old_config.binding_sha256
        ]
        if len(matches) != 1 or record["state"] != "FENCED":
            raise LifecycleError("Original fenced designation is unavailable")
        entry = matches[0]
        designation = _designation_from_entry(entry)
        _verify_recovery_entry_approval(old_config, state, entry)
        if (
            designation.material["target"] != target
            or designation.material["cut_ref"] != cut_ref(cut)
            or designation.material["old_binding_sha256"] != old_config.binding_sha256
            or designation.material["old_instance_id"] != old_config.instance_id
        ):
            raise LifecycleError("Original designation differs from exact recovery request")
        return designation


def resume_restore_promotion(
    restored: RestoredProfile,
    *,
    operation_id: str,
    approval_ref: str,
    port,
    binding: RuntimeBinding,
    legacy_verifier=None,
    old_config: TrustedInstanceConfig | None = None,
    receipt: OldWriterIsolationReceipt | None = None,
    recovery_authority: RecoveryAuthority | None = None,
):
    if _uses_receipt_route(receipt, recovery_authority, old_config):
        assert receipt is not None and recovery_authority is not None
        designation = recovery_authority.verify_old_writer_isolation(receipt, restored)
        receipt_sha256 = digest(asdict(receipt))
    elif old_config is not None:
        designation = _resume_live_designation(
            restored, old_config, operation_id=operation_id, approval_ref=approval_ref
        )
        receipt_sha256 = None
    else:
        raise LifecycleError("Dedicated resume needs live designation or signed receipt")
    return _promote(
        restored,
        designation,
        operation_id=operation_id,
        approval_ref=approval_ref,
        port=port,
        binding=binding,
        legacy_verifier=legacy_verifier,
        receipt_sha256=receipt_sha256,
    )
