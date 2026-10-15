"""Durable current-instance authority and trusted, fixed filesystem bindings."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finance_core.profile_gate import GateLease, initialize_profile_gate, writer_gate
from finance_core.profile_layout import ProfileLayout, product_root, profile_root
from finance_core.profile_paths import (
    _DIRECTORIES,
    ProfilePathError,
    ProfilePaths,
    _check_regular_role,
    _identity,
    _migration_contract_digest,
    _open_checked,
    _reject_path_acl_grants,
    _trusted_ancestor,
)
from finance_core.sqlite_connection import configure_sqlite_connection
from finance_core.staging_guard import StagingDatabaseError

from .schema_contract import (
    ApplicationSchemaContract,
    SchemaContractError,
    expected_schema,
    initialize_application_schema,
    ledger_sha256,
    validate_application_database,
)

STATE_FILENAME = "authority.v1.json"
PENDING_FILENAME = "authority.v1.pending"
CONTROL_LOCK_FILENAME = ".authority.v1.lock"
OWNER_LOCK_FILENAME = ".owner.v1.lock"
MAX_DOCUMENT_BYTES = 65536
_TOKEN = object()
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")


class InstanceAdmissionError(StagingDatabaseError):
    """Current instance authority, identity or operation cannot be proved."""


class InstanceAdmissionBusy(InstanceAdmissionError):
    """A bounded control/owner acquisition did not obtain its fixed lease."""


def _canonical(value: object) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise InstanceAdmissionError("Authority document exceeds 64 KiB")
    return payload


def _decode(payload: bytes) -> dict[str, Any]:
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InstanceAdmissionError("Duplicate authority field")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique)
        if not isinstance(value, dict) or _canonical(value) != payload:
            raise InstanceAdmissionError("Authority document is not canonical")
        return value
    except (ValueError, UnicodeError, TypeError) as exc:
        raise InstanceAdmissionError("Invalid authority document") from exc


def _reference(value: str) -> None:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise InstanceAdmissionError("An exact bounded operation/approval/evidence ref is required")


@dataclass(frozen=True)
class TrustedInstanceConfig:
    """Trusted composition only; request payloads supply references, never paths.

    The verifier attests exact ``(reference, action, binding_sha256)``. It must
    verify safe-abort evidence as zero protected effects, no replacement and
    original admission still valid; a reference alone is not that evidence.
    Same-UID hostile Python code is outside this cooperative capability scope.
    """

    control_root: Path
    locator_root: Path
    profile_id: str
    layout: ProfileLayout
    instance_id: str
    config_identity: str
    core_identity: str
    approval_verifier: Callable[[str, str, str], bool] = field(repr=False, compare=False)
    application_schema: ApplicationSchemaContract | None = field(default=None, kw_only=True)
    _pid: int = field(init=False, repr=False)
    _control_fd: int = field(init=False, repr=False, compare=False)
    _binding: dict[str, Any] = field(init=False, repr=False, compare=False)
    _binding_hash: str = field(init=False, repr=False)
    _closed: bool = field(init=False, default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        root, locator = Path(self.control_root), Path(self.locator_root)
        if (
            self.layout not in {"mac", "linux"}
            or not root.is_absolute()
            or not locator.is_absolute()
            or root.resolve(strict=True) != root
            or locator.resolve(strict=True) != locator
            or root.is_relative_to(locator)
            or locator.is_relative_to(root)
        ):
            raise InstanceAdmissionError("Control root must be canonical and outside profile data")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.profile_id):
            raise InstanceAdmissionError("Invalid profile ID")
        import sys

        if (self.layout == "linux" and sys.platform != "linux") or (
            self.layout == "mac" and sys.platform != "darwin"
        ):
            raise InstanceAdmissionError("Profile layout requires its actual supported host")
        if locator.name != ("Application Support" if self.layout == "mac" else "finance-codex"):
            raise InstanceAdmissionError("Fixed profile locator required")
        if not callable(self.approval_verifier):
            raise InstanceAdmissionError("Trusted approval verifier required")
        for identity in (self.instance_id, self.config_identity, self.core_identity):
            _reference(identity)
        migration_identity = _migration_contract_digest()
        try:
            schema_binding = expected_schema(migration_identity, self.application_schema).binding
        except SchemaContractError as exc:
            raise InstanceAdmissionError(str(exc)) from exc
        _trusted_ancestor(root)
        fd = _open_checked(root, directory=True)
        binding = {
            "instance_id": self.instance_id,
            "profile_id": self.profile_id,
            "layout": self.layout,
            "profile_root": str(profile_root(locator, self.profile_id, self.layout)),
            "control_root": str(root),
            "control_identity": list(_identity(os.fstat(fd))),
            "config_identity": self.config_identity,
            "core_identity": self.core_identity,
            "migration_identity": migration_identity,
            "application_schema": _decode(_canonical(schema_binding)),
        }
        object.__setattr__(self, "control_root", root)
        object.__setattr__(self, "locator_root", locator)
        object.__setattr__(self, "_pid", os.getpid())
        object.__setattr__(self, "_control_fd", fd)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_binding_hash", hashlib.sha256(_canonical(binding)).hexdigest())

    @property
    def binding_sha256(self) -> str:
        return self._binding_hash

    def assert_valid(self) -> None:
        if self._pid != os.getpid() or self._closed:
            raise InstanceAdmissionError("Trusted composition cannot cross process/fork boundary")
        _trusted_ancestor(self.control_root)
        fd = _open_checked(self.control_root, directory=True)
        try:
            if _identity(os.fstat(fd)) != _identity(os.fstat(self._control_fd)):
                raise InstanceAdmissionError("Control root identity changed")
        finally:
            os.close(fd)
        if self._binding["migration_identity"] != _migration_contract_digest():
            raise InstanceAdmissionError("Installed migration identity changed")

    def close(self) -> None:
        """Release trusted root pin only after all owners/sessions are closed."""
        if self._pid != os.getpid():
            raise InstanceAdmissionError("Cannot close fork-inherited trusted composition")
        if self._closed:
            return
        # Every issued owner registers its lifetime here, including unknown close.
        if _CONFIG_OWNERS.get(id(self), 0):
            raise InstanceAdmissionError("Trusted config still has a lifetime owner")
        os.close(self._control_fd)
        _CONFIG_OWNERS.pop(id(self), None)
        object.__setattr__(self, "_closed", True)

    def __enter__(self):
        self.assert_valid()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


_CONFIG_OWNERS: dict[int, int] = {}
_BOOTSTRAP_UNCERTAIN: list[tuple[object, ...]] = []


class ProductionBootstrapCloseUncertain(InstanceAdmissionError):
    """Fresh bootstrap failed with unknown close; all resources remain pinned."""

    def __init__(self, conn: sqlite3.Connection):
        super().__init__("Bootstrap SQLite close uncertain; owner/control/profile leases retained")
        self.connection = conn


def _close_sqlite_handle(conn: sqlite3.Connection) -> None:
    """One observable close boundary for lifecycle fault injection."""
    sqlite3.Connection.close(conn)


def _read(config: TrustedInstanceConfig, name: str) -> dict[str, Any]:
    config.assert_valid()
    fd = _open_checked(config.control_root / name, directory=False)
    try:
        payload = os.pread(fd, MAX_DOCUMENT_BYTES + 1, 0)
        if _identity(os.fstat(fd)) != _identity((config.control_root / name).lstat()):
            raise InstanceAdmissionError("Authority document replaced during read")
        return _decode(payload)
    finally:
        os.close(fd)


def _exists(config: TrustedInstanceConfig, name: str) -> bool:
    return os.path.lexists(config.control_root / name)


def _new_file(config: TrustedInstanceConfig, name: str, payload: bytes) -> None:
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=config._control_fd
    )
    try:
        view = memoryview(payload)
        while view:
            size = os.write(fd, view)
            if size <= 0:
                raise InstanceAdmissionError("Incomplete authority write")
            view = view[size:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(config._control_fd)


class _Lock:
    def __init__(self, config: TrustedInstanceConfig, name: str, *, initialize: bool = False):
        config.assert_valid()
        self.config, self.name, self.pid = config, name, os.getpid()
        self._retain = False
        if initialize and not _exists(config, name):
            try:
                _new_file(config, name, b"")
            except FileExistsError:
                pass
        self.fd = os.open(
            name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=config._control_fd
        )
        try:
            self.assert_valid()
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise

    def assert_valid(self) -> None:
        self.config.assert_valid()
        self._assert_file_valid()

    def _assert_file_valid(self) -> None:
        """Lease proof; a shared config must be proved in the same admission."""
        if self.pid != os.getpid() or self.fd < 0:
            raise InstanceAdmissionError("Closed or forked authority lease")
        opened, named = os.fstat(self.fd), (self.config.control_root / self.name).lstat()
        if (
            _identity(opened) != _identity(named)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_IMODE(named.st_mode) != 0o600
            or named.st_nlink != 1
            or named.st_size != 0
            or named.st_uid != os.getuid()
        ):
            raise InstanceAdmissionError("Fixed authority lock identity is unsafe")
        _reject_path_acl_grants(self.config.control_root / self.name)

    def acquire(self, operation: int, timeout: float) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not 0 <= timeout <= 30
        ):
            raise ValueError("timeout_seconds must be finite and between zero and 30")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fd, operation | fcntl.LOCK_NB)
                self.assert_valid()
                return
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InstanceAdmissionBusy("Authority/owner lease is contended") from None
                time.sleep(min(0.02, remaining))

    def close(self) -> None:
        if self._retain:
            return
        if self.pid != os.getpid():
            # Never explicitly unlock a fork-inherited open-file description.
            raise InstanceAdmissionError("Cannot release another process owner")
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


@contextmanager
def _control(
    config: TrustedInstanceConfig,
    *,
    shared: bool = False,
    timeout_seconds: float = 5.0,
    initialize: bool = False,
) -> Iterator[_Lock]:
    lease = _Lock(config, CONTROL_LOCK_FILENAME, initialize=initialize)
    try:
        lease.acquire(fcntl.LOCK_SH if shared else fcntl.LOCK_EX, timeout_seconds)
        yield lease
    finally:
        lease.close()


def _body(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != "history"}


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _initial_state() -> dict[str, Any]:
    return {
        "format": "instance-authority-v1",
        "version": 0,
        "current": None,
        "instances": {},
        "history": [],
    }


def _validate_state(state: dict[str, Any], *, prefix_version: int | None = None) -> dict[str, Any]:
    """Replay compact immutable control history and bind every current byte.

    Frozen profile/database identities come from the final record; each prepare
    entry binds their digest. Replaying subsequent admission/fence/hold entries
    reconstructs every prior body, so a history suffix cannot revive old state.
    """
    if (
        set(state) != {"format", "version", "current", "instances", "history"}
        or state["format"] != "instance-authority-v1"
        or type(state["version"]) is not int
        or state["version"] < 0
        or not isinstance(state["instances"], dict)
        or not isinstance(state["history"], list)
        or len(state["history"]) != state["version"]
    ):
        raise InstanceAdmissionError("Invalid authority state structure")
    replay = _initial_state()
    prefix = _initial_state() if prefix_version == 0 else None
    previous = None
    operations: set[str] = set()
    fields = {
        "version",
        "action",
        "operation_id",
        "binding",
        "approval_ref",
        "approval_digest",
        "context_digest",
        "record_identity",
        "hold_baseline",
        "previous_sha256",
        "before_sha256",
        "after_sha256",
        "entry_sha256",
    }
    for index, entry in enumerate(state["history"], 1):
        if (
            not isinstance(entry, dict)
            or set(entry) != fields
            or type(entry["version"]) is not int
            or entry["version"] != index
            or entry["previous_sha256"] != previous
            or entry["before_sha256"] != _digest(_body(replay))
            or entry["entry_sha256"]
            != _digest({key: value for key, value in entry.items() if key != "entry_sha256"})
        ):
            raise InstanceAdmissionError("Authority history chain is invalid")
        _reference(entry["operation_id"])
        _reference(entry["approval_ref"])
        if entry["operation_id"] in operations:
            raise InstanceAdmissionError("Authority operation reference was reused")
        operations.add(entry["operation_id"])
        key = entry["binding"]
        if not isinstance(key, str):
            raise InstanceAdmissionError("Authority history binding must be a digest")
        final = state["instances"].get(key)
        if (
            not isinstance(key, str)
            or not isinstance(final, dict)
            or set(final)
            != {"binding", "db_identity", "ledger_sha256", "state", "admission", "maintenance"}
            or not isinstance(final["binding"], dict)
            or set(final["binding"])
            != {
                "instance_id",
                "profile_id",
                "layout",
                "profile_root",
                "control_root",
                "control_identity",
                "config_identity",
                "core_identity",
                "migration_identity",
                "application_schema",
            }
            or any(
                not isinstance(value, str)
                for name, value in final["binding"].items()
                if name not in {"control_identity", "application_schema"}
            )
            or not isinstance(final["binding"]["application_schema"], dict)
            or not isinstance(final["ledger_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", final["ledger_sha256"])
            or not isinstance(final["binding"]["control_identity"], list)
            or len(final["binding"]["control_identity"]) != 2
            or any(
                type(value) is not int or value < 0
                for value in final["binding"]["control_identity"]
            )
            or _digest(final["binding"]) != key
            or not isinstance(final["db_identity"], list)
            or len(final["db_identity"]) != 2
            or any(type(value) is not int or value < 0 for value in final["db_identity"])
            or entry["record_identity"]
            != _digest(
                {
                    "binding": final["binding"],
                    "db_identity": final["db_identity"],
                    "ledger_sha256": final["ledger_sha256"],
                }
            )
        ):
            raise InstanceAdmissionError("Authority history instance identity is invalid")
        action = entry["action"]
        if action != "maintenance" and entry["hold_baseline"] is not None:
            raise InstanceAdmissionError("Authority history has unrelated hold baseline")
        target = replay["instances"].get(key)
        if action == "prepare":
            if (
                target is not None
                or any(
                    record["binding"]["instance_id"] == final["binding"].get("instance_id")
                    for record in replay["instances"].values()
                )
                or (
                    replay["current"] is not None
                    and replay["instances"][replay["current"]]["state"] != "FENCED"
                )
            ):
                raise InstanceAdmissionError("Authority history reused an instance")
            replay["instances"][key] = {
                "binding": final["binding"],
                "db_identity": final["db_identity"],
                "ledger_sha256": final["ledger_sha256"],
                "state": "PREPARED",
                "admission": None,
                "maintenance": None,
            }
        elif target is None:
            raise InstanceAdmissionError("Authority history operation precedes prepare")
        elif action == "admit":
            if (
                target["state"] != "PREPARED"
                or target["maintenance"] is not None
                or (
                    replay["current"] is not None
                    and replay["instances"][replay["current"]]["state"] != "FENCED"
                )
            ):
                raise InstanceAdmissionError("Authority history has invalid admission")
            target["state"], target["admission"] = "ACTIVE", entry["operation_id"]
            replay["current"] = key
        elif action == "fence":
            if target["state"] != "ACTIVE":
                raise InstanceAdmissionError("Authority history attempts fence revival")
            target["state"] = "FENCED"
        elif action == "maintenance":
            if target["state"] != "ACTIVE" or target["maintenance"] is not None:
                raise InstanceAdmissionError("Authority history has invalid maintenance")
            baseline = entry["hold_baseline"]
            expected_fields = {
                "format",
                "scope",
                "instance_id",
                "admission",
                "admission_version",
                "authority_version",
                "database_sha256",
                "profile_sha256",
            }
            if (
                not isinstance(baseline, dict)
                or set(baseline) != expected_fields
                or baseline["format"] != "maintenance-baseline-v1"
                or baseline["scope"] != "core-logical-db-and-fixed-profile-v1"
                or baseline["instance_id"] != target["binding"]["instance_id"]
                or baseline["admission"] != target["admission"]
                or type(baseline["authority_version"]) is not int
                or baseline["authority_version"] != index - 1
                or type(baseline["admission_version"]) is not int
                or baseline["admission_version"]
                != next(
                    prior["version"]
                    for prior in replay["history"]
                    if prior["operation_id"] == target["admission"]
                )
                or any(
                    not isinstance(baseline[field], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", baseline[field])
                    for field in ("database_sha256", "profile_sha256")
                )
            ):
                raise InstanceAdmissionError("Authority history maintenance baseline is invalid")
            target["maintenance"] = {
                "operation_id": entry["operation_id"],
                "admission": target["admission"],
                "baseline": baseline,
            }
        elif action == "safe_abort":
            if (
                target["state"] != "ACTIVE"
                or target["maintenance"] is None
                or replay["current"] != key
                or target["maintenance"]["admission"] != target["admission"]
            ):
                raise InstanceAdmissionError("Authority history cannot safely abort hold")
            target["maintenance"] = None
        else:
            raise InstanceAdmissionError("Unknown authority history action")
        replay["version"] = index
        if entry["after_sha256"] != _digest(_body(replay)):
            raise InstanceAdmissionError("Authority history body binding is invalid")
        replay["history"].append(entry)
        if index == prefix_version:
            prefix = _decode(_canonical(replay))
        previous = entry["entry_sha256"]
    if _body(replay) != _body(state):
        raise InstanceAdmissionError("Authority current state differs from complete history")
    if prefix_version is not None:
        if prefix is None:
            raise InstanceAdmissionError("Authority predecessor version is invalid")
        return prefix
    return replay


def _state(config: TrustedInstanceConfig, *, initial: bool = False) -> dict[str, Any]:
    if _exists(config, PENDING_FILENAME):
        raise InstanceAdmissionError("Authority transition pending; exact resume required")
    if initial and not _exists(config, STATE_FILENAME):
        _new_file(config, STATE_FILENAME, _canonical(_initial_state()))
    try:
        state = _read(config, STATE_FILENAME)
        _validate_state(state)
        return state
    except (OSError, ProfilePathError) as exc:
        raise InstanceAdmissionError("Authority state missing or unsafe") from exc


def _record(config: TrustedInstanceConfig, state: dict[str, Any]) -> dict[str, Any]:
    record = state["instances"].get(config.binding_sha256)
    if record is None or record["binding"] != config._binding:
        raise InstanceAdmissionError("Current authority does not bind this trusted composition")
    return record


def _approve(
    config: TrustedInstanceConfig, reference: str, action: str, extra: object = None
) -> str:
    _reference(reference)
    digest = hashlib.sha256(
        _canonical({"binding": config._binding, "action": action, "context": extra})
    ).hexdigest()
    if config.approval_verifier(reference, action, digest) is not True:
        raise InstanceAdmissionError("Exact trusted approval was not verified")
    return digest


def _publish(config: TrustedInstanceConfig, journal: dict[str, Any]) -> dict[str, Any]:
    """Complete the exact two-document journal; intermediate outcomes deny use."""
    after = journal["after"]
    _validate_state(after)
    candidate = "authority.v1.next"
    if _exists(config, candidate):
        if _read(config, candidate) != after:
            raise InstanceAdmissionError("Pending publication candidate differs")
    else:
        _new_file(config, candidate, _canonical(after))
    os.replace(
        candidate, STATE_FILENAME, src_dir_fd=config._control_fd, dst_dir_fd=config._control_fd
    )
    os.fsync(config._control_fd)
    if _read(config, STATE_FILENAME) != after:
        raise InstanceAdmissionError("Authority publication verification failed")
    os.unlink(PENDING_FILENAME, dir_fd=config._control_fd)
    os.fsync(config._control_fd)
    return after


def _transition(
    config: TrustedInstanceConfig,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    action: str,
    operation_id: str,
    approval_ref: str,
    context: object = None,
):
    _reference(operation_id)
    if any(entry["operation_id"] == operation_id for entry in before["history"]):
        raise InstanceAdmissionError("Authority operation reference was reused")
    approval_digest = _approve(
        config,
        approval_ref,
        action,
        {"operation_id": operation_id, "before_sha256": _digest(before), "context": context},
    )
    after["version"] = before["version"] + 1
    record = _record(config, after)
    entry = {
        "version": after["version"],
        "action": action,
        "operation_id": operation_id,
        "binding": config.binding_sha256,
        "approval_ref": approval_ref,
        "approval_digest": approval_digest,
        "context_digest": _digest(context),
        "record_identity": _digest(
            {
                "binding": record["binding"],
                "db_identity": record["db_identity"],
                "ledger_sha256": record["ledger_sha256"],
            }
        ),
        "hold_baseline": record["maintenance"]["baseline"] if action == "maintenance" else None,
        "previous_sha256": before["history"][-1]["entry_sha256"] if before["history"] else None,
        "before_sha256": _digest(_body(before)),
        "after_sha256": _digest(_body(after)),
    }
    entry["entry_sha256"] = _digest(entry)
    after["history"].append(entry)
    _validate_state(after)
    journal = {
        "format": "instance-transition-v1",
        "binding": config.binding_sha256,
        "action": action,
        "operation_id": operation_id,
        "approval_ref": approval_ref,
        "approval_digest": approval_digest,
        "before_sha256": _digest(before),
        "after": after,
        "context": context,
    }
    _new_file(config, PENDING_FILENAME, _canonical(journal))
    return _publish(config, journal)


def resume_authority_transition(
    config: TrustedInstanceConfig, *, operation_id: str, approval_ref: str
) -> dict[str, Any]:
    """Resume pending publication or observe an exact completed lost return.

    Completed recovery re-verifies the originally recorded approval digest and
    returns the instance's current record. It never replays an older action,
    publishes another transition or revives an admission since fenced.
    """
    _reference(operation_id)
    _reference(approval_ref)
    with _control(config) as control:
        if not _exists(config, PENDING_FILENAME):
            state = _state(config)
            matches = [
                entry
                for entry in state["history"]
                if entry["operation_id"] == operation_id
                and entry["binding"] == config.binding_sha256
            ]
            if len(matches) != 1 or matches[0]["approval_ref"] != approval_ref:
                raise InstanceAdmissionError(
                    "Completed recovery does not match exact original operation"
                )
            entry = matches[0]
            if (
                config.approval_verifier(approval_ref, entry["action"], entry["approval_digest"])
                is not True
            ):
                raise InstanceAdmissionError(
                    "Exact original completed approval binding was not verified"
                )
            record = _record(config, state)
            _verify_record_database(config, record, control)
            return _decode(_canonical(record))
        journal = _read(config, PENDING_FILENAME)
        if (
            set(journal)
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
            or journal["operation_id"] != operation_id
            or journal["approval_ref"] != approval_ref
        ):
            raise InstanceAdmissionError("Resume does not match exact pending operation")
        after = journal["after"]
        if (
            not isinstance(after, dict)
            or type(after.get("version")) is not int
            or after["version"] < 1
        ):
            raise InstanceAdmissionError("Pending authority successor is invalid")
        before = _validate_state(after, prefix_version=after["version"] - 1)
        if _digest(before) != journal["before_sha256"]:
            raise InstanceAdmissionError("Pending authority predecessor binding changed")
        digest = _approve(
            config,
            approval_ref,
            journal["action"],
            {
                "operation_id": operation_id,
                "before_sha256": _digest(before),
                "context": journal["context"],
            },
        )
        if digest != journal["approval_digest"]:
            raise InstanceAdmissionError("Pending approval binding changed")
        suffix = after["history"][-1]
        if (
            suffix["operation_id"] != operation_id
            or suffix["approval_ref"] != approval_ref
            or suffix["approval_digest"] != digest
            or suffix["action"] != journal["action"]
            or suffix["binding"] != config.binding_sha256
            or suffix["context_digest"] != _digest(journal["context"])
        ):
            raise InstanceAdmissionError("Pending journal does not bind exact history suffix")
        if not _exists(config, STATE_FILENAME):
            raise InstanceAdmissionError("Pending transition predecessor missing")
        current = _read(config, STATE_FILENAME)
        _validate_state(current)
        if current not in (before, after):
            raise InstanceAdmissionError("Pending transition predecessor changed")
        expected_baseline = None
        if journal["action"] in {"maintenance", "safe_abort"}:
            hold = (
                _record(config, after)["maintenance"]
                if journal["action"] == "maintenance"
                else _record(config, before)["maintenance"]
            )
            if (
                not isinstance(journal["context"], dict)
                or journal["context"].get("hold") != hold
                or hold is None
            ):
                raise InstanceAdmissionError(
                    "Pending recovery does not bind its exact maintenance hold"
                )
            expected_baseline = hold["baseline"]
        baseline_context = (
            None
            if expected_baseline is None
            else {
                key: expected_baseline[key]
                for key in ("instance_id", "admission", "admission_version", "authority_version")
            }
        )
        actual_baseline = _verify_record_database(
            config, _record(config, after), control, baseline_context=baseline_context
        )
        if expected_baseline is not None and actual_baseline != expected_baseline:
            raise InstanceAdmissionError("Pending maintenance baseline changed; publication denied")
        with _profile(config, expected=_record(config, after)["db_identity"]):
            return _record(config, _publish(config, journal)).copy()


class ProductionProfile(ProfilePaths):
    """Issued pinned production paths, without staging authorization semantics."""

    def __init__(self, *args, db_identity: list[int] | None, _token=None):
        if _token is not _TOKEN:
            raise InstanceAdmissionError("Production profile must be issued by trusted composition")
        super().__init__(*args)
        self._production_db_identity = db_identity
        self._pid = os.getpid()
        self._retain = False

    def close(self) -> None:
        if not self._retain:
            super().close()

    def revalidate(self) -> None:
        if self._pid != os.getpid():
            raise InstanceAdmissionError("Production profile cannot cross fork boundary")
        self._revalidate_common()
        if (
            os.path.lexists(self.staging_database)
            or os.path.lexists(self.profile / ".managed-staging.v1.json")
            or os.path.lexists(self.profile / ".managed-staging.v1.pending")
        ):
            raise InstanceAdmissionError("Production profile cannot carry staging enrollment")
        if self._production_db_identity is None:
            if os.path.lexists(self.live_database):
                raise InstanceAdmissionError("Production bootstrap requires blank database")
            return
        info = _check_regular_role(self.live_database, check_acl=False)
        if list(_identity(info)) != self._production_db_identity:
            raise InstanceAdmissionError("Production database identity changed")
        # Darwin path ACL inspection opens a main descriptor internally. Do not
        # disturb SQLite POSIX locks while any managed handle is active.
        import sys

        from finance_core.profile_paths import _MANAGED_SQLITE_LIFETIME_LOCK

        if sys.platform != "darwin" or not _MANAGED_SQLITE_LIFETIME_LOCK.sqlite_active:
            _reject_path_acl_grants(self.live_database)
        allowed = {
            self.live_database.name,
            *(self.live_database.name + suffix for suffix in ("-wal", "-shm", "-journal")),
        }
        for path in self.live_database.parent.iterdir():
            if path.name not in allowed:
                raise InstanceAdmissionError("Unknown production database role")
            _check_regular_role(path, check_acl=path != self.live_database)


def _profile(config: TrustedInstanceConfig, *, expected: list[int] | None) -> ProductionProfile:
    config.assert_valid()
    root = config.locator_root
    _trusted_ancestor(root)
    if any(os.path.lexists(parent / ".git") for parent in (root, *root.parents)):
        raise InstanceAdmissionError("Repository cannot be a production profile")
    product, base = (
        product_root(root, config.layout),
        profile_root(root, config.profile_id, config.layout),
    )
    paths = {
        "layout_root": root,
        "product_root": product,
        "profiles": product / "profiles",
        "profile": base,
        "runtime": base / "runtime",
        "runtime/database": base / "runtime/database",
        "workspace": base / "workspace",
        "workspace/database": base / "workspace/database",
        "backups": base / "backups",
        "work": base / "work",
        "restore": base / "restore",
        "profile_json": base / "profile.json",
        "live_database": base / "runtime/database/finance.db",
        "staging_database": base / "workspace/database/staging.sqlite",
    }
    if config.layout == "mac":
        paths["application_support"] = root
    pins = {}
    try:
        for name in _DIRECTORIES:
            if name == "layout_root" and config.layout == "mac":
                continue
            pins[name] = _open_checked(paths[name], directory=True)
        pins["profile_json"] = _open_checked(paths["profile_json"], directory=False)
        profile = ProductionProfile(
            config.profile_id, paths, pins, config.layout, db_identity=expected, _token=_TOKEN
        )
        profile.revalidate()
        return profile
    except BaseException:
        for fd in pins.values():
            os.close(fd)
        raise


def prepare_production_profile(
    config: TrustedInstanceConfig, *, operation_id: str, approval_ref: str
) -> ProductionProfile:
    from finance_core.reconciliation.migrations import (
        TEMP_DB_MIGRATION_PATHS,
        apply_migration_paths,
        verify_migration_history,
    )

    _reference(operation_id)
    virgin_control = not any(config.control_root.iterdir())
    owner = _Lock(config, OWNER_LOCK_FILENAME, initialize=True)
    try:
        owner.acquire(fcntl.LOCK_EX, 5.0)
        with _control(config, initialize=True) as control:
            state = _state(config, initial=virgin_control)
            if config.binding_sha256 in state["instances"]:
                raise InstanceAdmissionError("Existing database/instance can never be adopted")
            if any(
                record["binding"]["instance_id"] == config.instance_id
                for record in state["instances"].values()
            ):
                raise InstanceAdmissionError(
                    "Previously registered instance identity cannot be reused"
                )
            if (
                state["current"] is not None
                and state["instances"][state["current"]]["state"] != "FENCED"
            ):
                raise InstanceAdmissionError("Original instance must be permanently fenced first")
            _approve(
                config,
                approval_ref,
                "prepare",
                {"operation_id": operation_id, "before_sha256": _digest(state), "context": None},
            )
            with _profile(config, expected=None) as blank:
                expected_profile_names = {
                    "profile.json",
                    "runtime",
                    "workspace",
                    "backups",
                    "work",
                    "restore",
                }
                if {entry.name for entry in blank.profile.iterdir()} != expected_profile_names:
                    raise InstanceAdmissionError(
                        "Production preparation requires fresh profile roles"
                    )
                for folder in (
                    blank.runtime,
                    blank.workspace,
                    blank.backups,
                    blank.work,
                    blank.restore,
                ):
                    expected_names = (
                        {"database"} if folder in (blank.runtime, blank.workspace) else set()
                    )
                    if {entry.name for entry in folder.iterdir()} != expected_names:
                        raise InstanceAdmissionError(
                            "Production preparation requires fresh empty profile"
                        )
                if any(blank.live_database.parent.iterdir()) or any(
                    blank.staging_database.parent.iterdir()
                ):
                    raise InstanceAdmissionError(
                        "Production preparation requires empty database directories"
                    )
                initialize_profile_gate(blank)
                gate = writer_gate(blank)
                fd = -1
                conn = None
                uncertain = False
                lifetime = False
                sqlite_began = False
                try:
                    from finance_core.profile_paths import _MANAGED_SQLITE_LIFETIME_LOCK

                    _MANAGED_SQLITE_LIFETIME_LOCK.__enter__()
                    lifetime = True
                    _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
                    sqlite_began = True
                    fd = os.open(
                        blank.live_database,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    identity = list(_identity(os.fstat(fd)))
                    conn = sqlite3.connect(str(blank.live_database))
                    if list(_identity(blank.live_database.lstat())) != identity:
                        raise InstanceAdmissionError("Bootstrap database replaced")
                    configure_sqlite_connection(conn)
                    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
                    verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
                    sealed_ledger = ledger_sha256(conn)
                    initialize_application_schema(
                        conn, config._binding["migration_identity"], config.application_schema
                    )
                    validate_application_database(
                        conn,
                        config._binding["migration_identity"],
                        config.application_schema,
                        sealed_ledger,
                    )
                finally:
                    if conn is not None:
                        try:
                            if conn.in_transaction:
                                sqlite3.Connection.rollback(conn)
                            _close_sqlite_handle(conn)
                        except BaseException as exc:
                            uncertain = True
                            owner._retain = control._retain = blank._retain = True
                            _CONFIG_OWNERS[id(config)] = _CONFIG_OWNERS.get(id(config), 0) + 1
                            _BOOTSTRAP_UNCERTAIN.append(
                                (config, owner, control, blank, gate, conn, fd)
                            )
                            _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
                            raise ProductionBootstrapCloseUncertain(conn) from exc
                    if not uncertain:
                        try:
                            if fd >= 0:
                                os.fsync(fd)
                                directory = os.open(
                                    blank.live_database.parent, os.O_RDONLY | os.O_DIRECTORY
                                )
                                try:
                                    os.fsync(directory)
                                finally:
                                    os.close(directory)
                        finally:
                            if fd >= 0:
                                os.close(fd)
                            gate.close()
                            if sqlite_began:
                                _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
                    if lifetime:
                        _MANAGED_SQLITE_LIFETIME_LOCK.__exit__()
                after = _decode(_canonical(state))
                after["instances"][config.binding_sha256] = {
                    "binding": config._binding,
                    "db_identity": identity,
                    "ledger_sha256": sealed_ledger,
                    "state": "PREPARED",
                    "admission": None,
                    "maintenance": None,
                }
                _transition(
                    config,
                    state,
                    after,
                    action="prepare",
                    operation_id=operation_id,
                    approval_ref=approval_ref,
                )
        return _profile(config, expected=identity)
    finally:
        owner.close()


def _profile_baseline(profile: ProductionProfile) -> str:
    """Fixed-role metadata and manifest bytes, excluding transient file clocks."""
    metadata = []
    for role, path in sorted(profile._paths.items()):
        if role == "staging_database":
            continue  # Its required absence is revalidated by the issued profile.
        info = path.lstat()
        metadata.append(
            [
                role,
                str(path),
                info.st_dev,
                info.st_ino,
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_nlink,
            ]
        )
    manifest = os.pread(profile._pins["profile_json"], MAX_DOCUMENT_BYTES + 1, 0)
    if len(manifest) > MAX_DOCUMENT_BYTES:
        raise InstanceAdmissionError("Protected manifest exceeds baseline limit")
    return _digest({"roles": metadata, "manifest_sha256": hashlib.sha256(manifest).hexdigest()})


def _logical_database_digest(conn: sqlite3.Connection) -> str:
    """Stream every main table/row/storage type from one SQLite read snapshot.

    Raw WAL, pages, counters and checkpoint placement are not logical facts.
    sqlite_sequence and other existing SQLite tables are included. Explicit
    BINARY ordering plus hidden row identity removes declared NOCASE ties.
    """
    digest = hashlib.sha256()

    def frame(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name COLLATE BINARY"
        )
    ]
    without_rowid = {row[1]: bool(row[4]) for row in conn.execute("PRAGMA main.table_list")}
    previous_factory = conn.text_factory
    try:
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_xinfo({quoted(table)})")]
            expressions = [quoted(column) for column in columns]
            if not without_rowid[table]:
                hidden = next(
                    (
                        alias
                        for alias in ("rowid", "_rowid_", "oid")
                        if alias not in {column.casefold() for column in columns}
                    ),
                    None,
                )
                if hidden is None:
                    raise InstanceAdmissionError("Cannot attest shadowed protected row identity")
                expressions.append(hidden)
            frame(table.encode("utf-8"))
            for column in columns:
                frame(column.encode("utf-8"))
            frame(b"end-columns")
            selection = ",".join(f"typeof({column}),{column}" for column in expressions)
            ordering = ",".join(
                f"typeof({column}) COLLATE BINARY,{column} COLLATE BINARY" for column in expressions
            )
            conn.text_factory = bytes
            for row in conn.execute(f"SELECT {selection} FROM {quoted(table)} ORDER BY {ordering}"):
                frame(b"row")
                for index in range(0, len(row), 2):
                    kind, value = row[index], row[index + 1]
                    frame(kind)
                    if kind == b"null":
                        encoded = b""
                    elif kind == b"integer":
                        encoded = str(value).encode("ascii")
                    elif kind == b"real":
                        encoded = struct.pack("!d", value)
                    elif kind in {b"text", b"blob"} and isinstance(value, bytes):
                        encoded = value
                    else:
                        raise InstanceAdmissionError("Unsupported protected SQLite storage type")
                    frame(encoded)
            conn.text_factory = previous_factory
            frame(b"end-table")
    finally:
        conn.text_factory = previous_factory
    return digest.hexdigest()


def _verify_record_database(
    config: TrustedInstanceConfig,
    record: dict[str, Any],
    control: _Lock,
    *,
    baseline_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read-only native schema/ledger proof before authority publication."""
    from finance_core.profile_paths import _MANAGED_SQLITE_LIFETIME_LOCK

    profile = _profile(config, expected=record["db_identity"])
    gate: GateLease | None = None
    conn = None
    uncertain = False
    lifetime = False
    sqlite_began = False
    try:
        gate = writer_gate(profile)
        _MANAGED_SQLITE_LIFETIME_LOCK.__enter__()
        lifetime = True
        _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
        sqlite_began = True
        conn = sqlite3.connect(profile.live_database.as_uri() + "?mode=ro", uri=True)
        configure_sqlite_connection(conn)
        conn.execute("BEGIN")
        validate_application_database(
            conn,
            config._binding["migration_identity"],
            config.application_schema,
            record["ledger_sha256"],
        )
        if baseline_context is not None:
            before_profile = _profile_baseline(profile)
            content_sha256 = _logical_database_digest(conn)
            profile.revalidate()
            if _profile_baseline(profile) != before_profile:
                raise InstanceAdmissionError("Protected profile baseline changed during snapshot")
            return {
                "format": "maintenance-baseline-v1",
                "scope": "core-logical-db-and-fixed-profile-v1",
                **baseline_context,
                "database_sha256": content_sha256,
                "profile_sha256": before_profile,
            }
        return None
    except SchemaContractError as exc:
        raise InstanceAdmissionError(str(exc)) from exc
    finally:
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.rollback()
                _close_sqlite_handle(conn)
            except BaseException as exc:
                uncertain = True
                profile._retain = control._retain = True
                _CONFIG_OWNERS[id(config)] = _CONFIG_OWNERS.get(id(config), 0) + 1
                _BOOTSTRAP_UNCERTAIN.append((config, control, profile, gate, conn))
                _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
                raise ProductionBootstrapCloseUncertain(conn) from exc
        if not uncertain:
            try:
                if sqlite_began:
                    _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
                if lifetime:
                    _MANAGED_SQLITE_LIFETIME_LOCK.__exit__()
            finally:
                try:
                    if gate is not None:
                        gate.close()
                finally:
                    profile.close()


def inspect_instance_admission(config: TrustedInstanceConfig) -> dict[str, Any]:
    with _control(config, shared=True):
        return _decode(_canonical(_record(config, _state(config))))


def _change(
    config: TrustedInstanceConfig,
    *,
    action: str,
    operation_id: str,
    approval_ref: str,
    evidence_ref: str | None = None,
) -> dict[str, Any]:
    with _control(config) as control:
        state = _state(config)
        record = _record(config, state)
        baseline_context = None
        if (
            action == "maintenance"
            and record["state"] == "ACTIVE"
            and record["maintenance"] is None
        ):
            admission_version = next(
                entry["version"]
                for entry in state["history"]
                if entry["operation_id"] == record["admission"]
            )
            baseline_context = {
                "instance_id": config.instance_id,
                "admission": record["admission"],
                "admission_version": admission_version,
                "authority_version": state["version"],
            }
        elif action == "safe_abort" and record["maintenance"] is not None:
            recorded_baseline = record["maintenance"]["baseline"]
            baseline_context = {
                key: recorded_baseline[key]
                for key in ("instance_id", "admission", "admission_version", "authority_version")
            }
        actual_baseline = _verify_record_database(
            config, record, control, baseline_context=baseline_context
        )
        with _profile(config, expected=record["db_identity"]):
            after = _decode(_canonical(state))
            target = _record(config, after)
            context: object = None
            if action == "admit":
                if target["state"] != "PREPARED" or target["maintenance"] is not None:
                    raise InstanceAdmissionError("Only fresh PREPARED instance can be admitted")
                prior = after["current"]
                if prior is not None and after["instances"][prior]["state"] != "FENCED":
                    raise InstanceAdmissionError("Current instance is still active")
                target["state"], target["admission"] = "ACTIVE", operation_id
                after["current"] = config.binding_sha256
            elif action == "fence":
                if target["state"] != "ACTIVE":
                    raise InstanceAdmissionError("Only ACTIVE instance can be permanently fenced")
                target["state"] = "FENCED"
            elif action == "maintenance":
                if target["state"] != "ACTIVE" or target["maintenance"] is not None:
                    raise InstanceAdmissionError(
                        "Maintenance requires ACTIVE instance without hold"
                    )
                target["maintenance"] = {
                    "operation_id": operation_id,
                    "admission": target["admission"],
                    "baseline": actual_baseline,
                }
                context = {"hold": target["maintenance"]}
            elif action == "safe_abort":
                hold = target["maintenance"]
                if (
                    target["state"] != "ACTIVE"
                    or hold is None
                    or after["current"] != config.binding_sha256
                    or hold["admission"] != target["admission"]
                    or evidence_ref is None
                ):
                    raise InstanceAdmissionError(
                        "Maintenance cannot safely return to original admission"
                    )
                if actual_baseline != hold["baseline"]:
                    raise InstanceAdmissionError(
                        "Protected maintenance baseline changed; hold retained"
                    )
                _reference(evidence_ref)
                context = {
                    "hold": hold,
                    "evidence_ref": evidence_ref,
                    "zero_protected_effects": True,
                    "no_replacement": True,
                    "original_admission_valid": True,
                }
                target["maintenance"] = None
            else:
                raise InstanceAdmissionError("Unknown authority action")
            result = _transition(
                config,
                state,
                after,
                action=action,
                operation_id=operation_id,
                approval_ref=approval_ref,
                context=context,
            )
            return _decode(_canonical(_record(config, result)))


def admit_production_instance(
    config: TrustedInstanceConfig, *, operation_id: str, approval_ref: str
) -> dict[str, Any]:
    return _change(config, action="admit", operation_id=operation_id, approval_ref=approval_ref)


def fence_production_instance(
    config: TrustedInstanceConfig, *, operation_id: str, approval_ref: str
) -> dict[str, Any]:
    return _change(config, action="fence", operation_id=operation_id, approval_ref=approval_ref)


def enter_maintenance_hold(
    config: TrustedInstanceConfig, *, operation_id: str, approval_ref: str
) -> dict[str, Any]:
    return _change(
        config, action="maintenance", operation_id=operation_id, approval_ref=approval_ref
    )


def inspect_maintenance_hold(config: TrustedInstanceConfig) -> dict[str, Any] | None:
    return inspect_instance_admission(config)["maintenance"]


def abort_maintenance_hold(
    config: TrustedInstanceConfig, *, operation_id: str, approval_ref: str, evidence_ref: str
) -> dict[str, Any]:
    return _change(
        config,
        action="safe_abort",
        operation_id=operation_id,
        approval_ref=approval_ref,
        evidence_ref=evidence_ref,
    )
