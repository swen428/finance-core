"""Disposable synthetic FD authority proof, never a SQLite or profile admission API.

Only trusted test composition supplies grants, anchors and disposable keys. This
module deliberately has no path that creates an ordinary managed-open grant.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import sys
import threading
import time
import uuid
import weakref
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Protocol

from finance_core.profile_paths import _reject_acl_grants

from .format import (
    DEFAULT_LIMITS,
    OBJECTS,
    PURPOSE,
    ZERO,
    FormatError,
    decode_frame,
    encode_frame,
    frame_digest,
    state_digest,
)
from .state import (
    StateError,
    apply_record,
    assert_reservations,
    capacity,
    initial_state,
    reservations,
    select_slot,
    transition,
)

Hook = Callable[[str, dict[str, object]], None]
_LIVE_STORES: weakref.WeakSet[SyntheticAuthorityStore] = weakref.WeakSet()
_UNCERTAIN_LEASES: list[tuple[int, ...]] = []
_UNSAFE_ROOTS: set[str] = set()


def _after_fork_child() -> None:
    # A forked child cannot receive an issuer FD or token. Close its copies
    # before user code runs; an uncertain close forces process reap.
    for owner in tuple(_LIVE_STORES):
        owner._poisoned = True
        fds = set(owner._fds.values()) | set(owner._dirs.values())
        fds.update(fd for _path, fd, _dev, _ino in owner._chain)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                os._exit(78)
        owner._closed = True
    for leases in _UNCERTAIN_LEASES:
        for fd in leases:
            try:
                os.close(fd)
            except OSError:
                os._exit(78)


os.register_at_fork(after_in_child=_after_fork_child)


def _public_operation(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(self: SyntheticAuthorityStore, *args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() != self._owner_thread or self._public_busy:
            raise AuthorityError("cross-thread or reentrant owner operation")
        self._public_busy = True
        self._deadline = time.monotonic() + 30.0
        try:
            return function(self, *args, **kwargs)
        finally:
            self._public_busy = False

    return wrapper


class AuthorityError(RuntimeError):
    """The synthetic epoch cannot issue the requested operation."""


class UnsupportedPlatformError(AuthorityError):
    """The required platform ACL/barrier proof is unavailable."""


class QuarantinedError(AuthorityError):
    """Persistent evidence is incomplete or inconsistent; no repair is attempted."""


class PoisonedError(AuthorityError):
    """This owner observed uncertain effects or identity and cannot resume."""


class CapacityError(AuthorityError):
    """Fixed epoch capacity is insufficient before any new allocation effect."""


class LeaseBusyError(AuthorityError):
    """Another synthetic owner still holds an admitted lifetime lease."""


@dataclass(frozen=True)
class FreshTestGrant:
    installation_id: str
    profile_id: str
    instance_id: str
    issuer_epoch: str
    registry_id: str
    key_id: str
    enrollment_id: str
    key: bytes


@dataclass(frozen=True)
class TestAnchor:
    v: int
    installation_id: str
    profile_id: str
    instance_id: str
    issuer_epoch: str
    registry_id: str
    key_id: str
    enrollment_id: str
    enrollment_kind: str
    descriptor_dev: int
    descriptor_ino: int
    descriptor_uid: int
    c_digest: str
    root_chain: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class VerifiedSnapshot:
    seq: int
    head_digest: str
    state: dict[str, Any]
    component_state: str


@dataclass(frozen=True)
class Token:
    _issuer: str
    _nonce: str


@dataclass(frozen=True)
class VerifiedRestoreTestGrant:
    fresh: FreshTestGrant
    source_anchor: TestAnchor
    source_key: bytes
    source_descriptor: bytes
    source_registry: bytes
    source_head: bytes
    source_receipt: bytes
    cut_frame: bytes
    cut_key: bytes
    expected_cut_id: str
    expected_closed_owner_inventory_digest: str
    expected_snapshot_sentinel_digest: str


class SyntheticTestIssuer(Protocol):
    """Test-only external add-only item; methods are never called under a gate."""

    def prepare_fresh(self, root: Path) -> FreshTestGrant: ...
    def prepare_verified_restore(self, root: Path) -> VerifiedRestoreTestGrant: ...
    def add_only(self, anchor: TestAnchor, key: bytes) -> None: ...
    def readback(self, enrollment_id: str) -> tuple[TestAnchor, bytes]: ...


def _platform() -> None:
    if sys.platform != "darwin":
        raise UnsupportedPlatformError("synthetic FD proof supports Darwin ACL checks only")


def _file_witness(
    info: os.stat_result, directory: str, basename: str, operations: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "directory": directory,
        "basename": basename,
        "dev": info.st_dev,
        "ino": info.st_ino,
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "type": "regular",
        "acl_policy": "no-grants-v1",
        "operations": list(operations),
    }


def _dir_witness(info: os.stat_result) -> dict[str, Any]:
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "acl_policy": "no-grants-v1",
    }


def _validate_fd(fd: int, expected: dict[str, Any], path: Path, *, directory: bool) -> None:
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino, info.st_uid) != (
        expected["dev"],
        expected["ino"],
        expected["uid"],
    ):
        raise QuarantinedError(f"FD witness mismatch: {path}")
    if stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise QuarantinedError(f"private mode mismatch: {path}")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise QuarantinedError(f"not directory: {path}")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise QuarantinedError(f"not private single-link regular file: {path}")
    try:
        _reject_acl_grants(fd, path)
    except Exception as exc:
        raise QuarantinedError(f"ACL not admitted: {path}") from exc


def _open_relative(
    parent_fd: int, name: str, *, directory: bool = False, create: bool = False
) -> int:
    flags = (os.O_RDONLY | os.O_DIRECTORY) if directory else os.O_RDWR
    flags |= os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    return os.open(name, flags, 0o600, dir_fd=parent_fd)


def _lock_nonblocking(fd: int, mode: int) -> None:
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise LeaseBusyError("synthetic owner lease is busy") from exc


def _read_exact(fd: int, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = os.pread(fd, size - len(result), len(result))
        if not chunk:
            raise QuarantinedError("premature EOF")
        result.extend(chunk)
    if os.fstat(fd).st_size != size:
        raise QuarantinedError("unexpected file size")
    return bytes(result)


class SyntheticAuthorityStore:
    """One owned, exact-FD synthetic epoch. Never returns a raw descriptor."""

    def __init__(self) -> None:
        self._owner_thread = threading.get_ident()
        self._public_busy = False
        self._deadline = time.monotonic() + 30.0
        self._root: Path
        self._anchor: TestAnchor
        self._key: bytes
        self._limits: dict[str, int]
        self._enrollment_kind: str
        self._descriptor: dict[str, Any]
        self._descriptor_witness: os.stat_result
        self._c_bytes: bytes
        self._c_digest: str
        self._fds: dict[str, int] = {}
        self._dirs: dict[str, int] = {}
        self._chain: list[tuple[str, int, int, int]] = []
        self._closed = False
        self._poisoned = False
        self._tokens: dict[str, tuple[Token, tuple[str, int, int]]] = {}
        self._references: dict[tuple[str, int, int], int] = {}
        self._issuer_nonce = uuid.uuid4().hex
        self._hook: Hook | None = None
        self._receipt_bytes = b""
        self._descriptor_pending = False
        self._descriptor_cursor = b""
        self._registry_bytes = b""
        self._head_bytes = b""
        self._state = initial_state()
        self._seq = 0
        self._used_events: set[str] = set()
        self._used_operations: dict[str, str] = {}
        _LIVE_STORES.add(self)

    @classmethod
    def enroll_fresh_test(
        cls,
        root: Path,
        issuer: SyntheticTestIssuer,
        *,
        limits: dict[str, int] | None = None,
        barrier_profile: str = "process-crash-fsync-v1",
        hook: Hook | None = None,
    ) -> SyntheticAuthorityStore:
        _platform()
        grant = issuer.prepare_fresh(Path(root))  # external, before any lease
        return cls._enroll(Path(root), issuer, grant, "fresh", limits, barrier_profile, hook)

    @classmethod
    def enroll_verified_restore_test(
        cls,
        root: Path,
        issuer: SyntheticTestIssuer,
        *,
        limits: dict[str, int] | None = None,
        barrier_profile: str = "process-crash-fsync-v1",
        hook: Hook | None = None,
    ) -> SyntheticAuthorityStore:
        _platform()
        grant = issuer.prepare_verified_restore(Path(root))  # trusted fake port, outside gate
        if grant.cut_key in (grant.source_key, grant.fresh.key):
            raise QuarantinedError("cut authentication key must be independent")
        try:
            cls._verify_old_cut(grant)
        except (FormatError, StateError) as exc:
            raise QuarantinedError("historical cut authentication or replay failed") from exc
        for field in (
            "profile_id",
            "instance_id",
            "issuer_epoch",
            "registry_id",
            "key_id",
            "enrollment_id",
        ):
            if getattr(grant.fresh, field) == getattr(grant.source_anchor, field):
                raise QuarantinedError(f"restore must issue a new {field}")
        if grant.fresh.key == grant.source_key:
            raise QuarantinedError("historical MAC key cannot issue current authority")
        return cls._enroll(
            Path(root), issuer, grant.fresh, "verified-restore-test", limits, barrier_profile, hook
        )

    @classmethod
    def _verify_old_cut(cls, grant: VerifiedRestoreTestGrant) -> None:
        if len(grant.source_descriptor) != 16384 or len(grant.source_receipt) != 2048:
            raise QuarantinedError("old descriptor/receipt bounds")
        descriptor = decode_frame("descriptor", grant.source_descriptor, grant.source_key)
        c_digest = frame_digest(grant.source_descriptor)
        anchor = grant.source_anchor
        if c_digest != anchor.c_digest:
            raise QuarantinedError("old anchor digest")
        for field in (
            "installation_id",
            "profile_id",
            "instance_id",
            "issuer_epoch",
            "registry_id",
            "key_id",
            "enrollment_id",
            "enrollment_kind",
        ):
            if descriptor[field] != getattr(anchor, field):
                raise QuarantinedError("old anchor identity")
        limits = descriptor["limits"]
        r_bytes, h_bytes = grant.source_registry, grant.source_head
        if (
            not r_bytes
            or not h_bytes
            or len(r_bytes) % 4096
            or len(h_bytes) % 2048
            or len(r_bytes) // 4096 != len(h_bytes) // 2048
            or len(r_bytes) > limits["registry_byte_cap"]
            or len(h_bytes) > limits["head_byte_cap"]
        ):
            raise QuarantinedError("old log bounds")
        # Historical material is replayed as authenticated bytes. Its source
        # device/inode is historical evidence, never a current FD authority.
        old = cls()
        old._descriptor, old._c_digest, old._key, old._limits = (
            descriptor,
            c_digest,
            grant.source_key,
            limits,
        )
        old._registry_bytes, old._head_bytes = r_bytes, h_bytes
        old._replay()
        if any(old._state[kind]["pending"] is not None for kind in ("JOURNAL", "WAL")):
            raise QuarantinedError("old cut has incomplete native allocation")
        receipt = decode_frame("receipt", grant.source_receipt, grant.source_key)
        if receipt != old._receipt_payload():
            raise QuarantinedError("old receipt binding")
        cut = decode_frame("test-cut", grant.cut_frame, grant.cut_key)
        for prefix, field in (
            ("profile_id", "source_profile_id"),
            ("instance_id", "source_instance_id"),
            ("issuer_epoch", "source_issuer_epoch"),
            ("registry_id", "source_registry_id"),
            ("key_id", "source_key_id"),
        ):
            if cut[field] != descriptor[prefix]:
                raise QuarantinedError("old cut identity")
        if (
            cut["cut_id"] != grant.expected_cut_id
            or cut["c_digest"] != c_digest
            or cut["registry_file_digest"] != hashlib.sha256(r_bytes).hexdigest()
            or cut["head_file_digest"] != hashlib.sha256(h_bytes).hexdigest()
            or cut["accepted_seq"] != old._seq
            or cut["accepted_head_digest"] != frame_digest(h_bytes[-2048:])
            or cut["state_digest"] != state_digest(old._state)
            or cut["closed_owner_inventory_digest"] != grant.expected_closed_owner_inventory_digest
            or cut["snapshot_sentinel_digest"] != grant.expected_snapshot_sentinel_digest
        ):
            raise QuarantinedError("old cut certificate mismatch")

    @classmethod
    def _enroll(
        cls,
        root: Path,
        issuer: SyntheticTestIssuer,
        grant: FreshTestGrant,
        enrollment_kind: str,
        limits: dict[str, int] | None,
        barrier_profile: str,
        hook: Hook | None,
    ) -> SyntheticAuthorityStore:
        store = cls()
        store._root = root
        if str(root) in _UNSAFE_ROOTS:
            raise PoisonedError("uncertain owner has not been reaped")
        store._hook = hook
        store._key = grant.key
        store._limits = dict(DEFAULT_LIMITS if limits is None else limits)
        store._enrollment_kind = enrollment_kind
        if len(grant.key) != 32 or any(
            type(getattr(grant, field)) is not str
            or re.fullmatch(r"[0-9a-f]{32}", getattr(grant, field)) is None
            for field in (
                "installation_id",
                "profile_id",
                "instance_id",
                "issuer_epoch",
                "registry_id",
                "key_id",
                "enrollment_id",
            )
        ):
            raise FormatError("invalid test issuer grant")
        if set(store._limits) != set(DEFAULT_LIMITS) or any(
            type(value) is not int or not 1 <= value <= DEFAULT_LIMITS[field]
            for field, value in store._limits.items()
        ):
            raise FormatError("invalid fixed limits")
        if capacity(store._limits) < 1:
            raise CapacityError("no GENESIS pair capacity")
        if barrier_profile not in ("process-crash-fsync-v1", "injected-storage-model-v1"):
            raise UnsupportedPlatformError("barrier profile")
        # Field and hard-cap validation happens through the strict descriptor encoder.
        try:
            store._open_chain()
            store._open_root_gate()
            store._emit("before_first_gate_lock", "gate", "enrollment")
            _lock_nonblocking(store._fds["gate"], fcntl.LOCK_EX)
            store._emit("after_first_gate_lock", "gate", "enrollment")
            store._ensure_fresh_target()
            os.mkdir("authority", 0o700, dir_fd=store._dirs["root"])
            store._dirs["authority"] = _open_relative(
                store._dirs["root"], "authority", directory=True
            )
            os.mkdir("slots", 0o700, dir_fd=store._dirs["root"])
            store._dirs["slots"] = _open_relative(store._dirs["root"], "slots", directory=True)
            for role, (directory, basename, _) in OBJECTS.items():
                if role == "gate":
                    continue
                store._fds[role] = _open_relative(store._dirs[directory], basename, create=True)
            store._fds["descriptor"] = _open_relative(
                store._dirs["authority"], "capabilities.frame", create=True
            )
            lifecycle_info = os.fstat(store._fds["lifecycle"])
            _validate_fd(
                store._fds["lifecycle"],
                _file_witness(lifecycle_info, *OBJECTS["lifecycle"]),
                store._role_path("lifecycle"),
                directory=False,
            )
            _lock_nonblocking(store._fds["lifecycle"], fcntl.LOCK_EX)
            store._descriptor = {
                "v": 1,
                "purpose": PURPOSE,
                **{
                    field: getattr(grant, field)
                    for field in (
                        "installation_id",
                        "profile_id",
                        "instance_id",
                        "issuer_epoch",
                        "registry_id",
                        "key_id",
                        "enrollment_id",
                    )
                },
                "enrollment_kind": enrollment_kind,
                "directories": {
                    role: _dir_witness(os.fstat(fd)) for role, fd in store._dirs.items()
                },
                "objects": {
                    role: _file_witness(os.fstat(store._fds[role]), *OBJECTS[role])
                    for role in OBJECTS
                },
                "limits": store._limits,
                "barrier_profile": barrier_profile,
            }
            store._descriptor_witness = os.fstat(store._fds["descriptor"])
            store._c_bytes = encode_frame("descriptor", store._descriptor, store._key)
            store._descriptor_pending = True
            store._c_digest = frame_digest(store._c_bytes)
            store._anchor = TestAnchor(
                1,
                grant.installation_id,
                grant.profile_id,
                grant.instance_id,
                grant.issuer_epoch,
                grant.registry_id,
                grant.key_id,
                grant.enrollment_id,
                enrollment_kind,
                store._descriptor_witness.st_dev,
                store._descriptor_witness.st_ino,
                store._descriptor_witness.st_uid,
                store._c_digest,
                store._chain_witness(),
            )
            store._guard(allow_unenrolled=True, expected_registry=b"", expected_head=b"")
            store._write_object("descriptor", store._c_bytes)
            store._descriptor_pending = False
            store._barrier("descriptor")
            store._commit("GENESIS", None, 0, None, uuid.uuid4().hex, genesis=True)
            for directory in ("authority", "slots", "root"):
                store._sync_directory(directory, stage="pre_external")
            store._guard(allow_unenrolled=True)
            store._emit("before_first_gate_release", "gate", "enrollment")
            store._close_all()  # first gate phase has no live target FD during external call
            if store._poisoned:
                raise PoisonedError("bootstrap close uncertain before external enrollment")
            store._emit_detached("after_first_gate_release", "gate:released")
            store._emit_detached("before_external_add", "issuer:add_only")
            issuer.add_only(store._anchor, store._key)
            store._emit_detached("after_external_add", "issuer:add_only")
            store._emit_detached("before_external_readback", "issuer:readback")
            read_anchor, read_key = issuer.readback(grant.enrollment_id)
            store._emit_detached("after_external_readback", "issuer:readback")
            if read_anchor != store._anchor or read_key != store._key:
                raise QuarantinedError("add-only external readback mismatch")
            # Reopen exact same object graph and reacquire the gate. No issuer call below.
            reopened = cls._open_existing(
                root, store._anchor, store._key, hook, allow_unenrolled=True, exclusive_gate=True
            )
            try:
                if reopened._seq != 1 or reopened._state != initial_state():
                    raise QuarantinedError("bootstrap genesis changed")
                receipt = reopened._receipt_payload()
                reopened._receipt_bytes = encode_frame("receipt", receipt, reopened._key)
                reopened._guard(allow_unenrolled=True, expected_receipt=b"")
                reopened._write_object("receipt", reopened._receipt_bytes)
                reopened._barrier("receipt")
                reopened._sync_directory("authority", stage="receipt")
                reopened._guard()
                return reopened
            except BaseException:
                reopened._poisoned = True
                reopened._close_all()
                raise
        except BaseException:
            store._poisoned = True
            store._close_all()
            raise

    @classmethod
    def reopen_test(
        cls, root: Path, *, anchor: TestAnchor, key: bytes, hook: Hook | None = None
    ) -> SyntheticAuthorityStore:
        _platform()
        return cls._open_existing(
            Path(root), anchor, key, hook, allow_unenrolled=False, exclusive_gate=False
        )

    @classmethod
    def _open_existing(
        cls,
        root: Path,
        anchor: TestAnchor,
        key: bytes,
        hook: Hook | None,
        *,
        allow_unenrolled: bool,
        exclusive_gate: bool,
    ) -> SyntheticAuthorityStore:
        store = cls()
        store._root, store._anchor, store._key, store._hook = root, anchor, key, hook
        if str(root) in _UNSAFE_ROOTS:
            raise PoisonedError("uncertain owner has not been reaped")
        try:
            store._open_chain()
            if store._chain_witness() != anchor.root_chain:
                raise QuarantinedError("advertised root chain mismatch")
            store._open_root_gate()
            if exclusive_gate and allow_unenrolled:
                store._emit("before_second_gate_lock", "gate", "enrollment")
            _lock_nonblocking(
                store._fds["gate"], fcntl.LOCK_EX if exclusive_gate else fcntl.LOCK_SH
            )
            if exclusive_gate and allow_unenrolled:
                store._emit("after_second_gate_lock", "gate", "enrollment")
            for directory in ("authority", "slots"):
                store._dirs[directory] = _open_relative(
                    store._dirs["root"], directory, directory=True
                )
            for role, (directory, basename, _) in OBJECTS.items():
                if role != "gate":
                    if hook is not None:
                        parent_info = os.fstat(store._dirs[directory])
                        hook(
                            "before_object_open",
                            {
                                "role": role,
                                "dev": parent_info.st_dev,
                                "ino": parent_info.st_ino,
                                "phase": "reopen",
                                "seq": 0,
                            },
                        )
                    store._fds[role] = _open_relative(store._dirs[directory], basename)
                    if hook is not None:
                        opened_info = os.fstat(store._fds[role])
                        hook(
                            "after_object_open",
                            {
                                "role": role,
                                "dev": opened_info.st_dev,
                                "ino": opened_info.st_ino,
                                "phase": "reopen",
                                "seq": 0,
                            },
                        )
            if hook is not None:
                parent_info = os.fstat(store._dirs["authority"])
                hook(
                    "before_object_open",
                    {
                        "role": "descriptor",
                        "dev": parent_info.st_dev,
                        "ino": parent_info.st_ino,
                        "phase": "reopen",
                        "seq": 0,
                    },
                )
            store._fds["descriptor"] = _open_relative(
                store._dirs["authority"], "capabilities.frame"
            )
            if hook is not None:
                opened_info = os.fstat(store._fds["descriptor"])
                hook(
                    "after_object_open",
                    {
                        "role": "descriptor",
                        "dev": opened_info.st_dev,
                        "ino": opened_info.st_ino,
                        "phase": "reopen",
                        "seq": 0,
                    },
                )
            store._descriptor_witness = os.fstat(store._fds["descriptor"])
            if (
                store._descriptor_witness.st_dev,
                store._descriptor_witness.st_ino,
                store._descriptor_witness.st_uid,
            ) != (anchor.descriptor_dev, anchor.descriptor_ino, anchor.descriptor_uid):
                raise QuarantinedError("descriptor FD witness mismatch")
            _validate_fd(
                store._fds["descriptor"],
                {
                    "dev": anchor.descriptor_dev,
                    "ino": anchor.descriptor_ino,
                    "uid": anchor.descriptor_uid,
                },
                store._role_path("descriptor"),
                directory=False,
            )
            store._c_bytes = _read_exact(store._fds["descriptor"], 16384)
            store._c_digest = frame_digest(store._c_bytes)
            store._descriptor = decode_frame("descriptor", store._c_bytes, key)
            store._limits = store._descriptor["limits"]
            store._enrollment_kind = store._descriptor["enrollment_kind"]
            if store._c_digest != anchor.c_digest or anchor.v != 1:
                raise QuarantinedError("external anchor descriptor digest mismatch")
            for field in (
                "installation_id",
                "profile_id",
                "instance_id",
                "issuer_epoch",
                "registry_id",
                "key_id",
                "enrollment_id",
                "enrollment_kind",
            ):
                if store._descriptor[field] != getattr(anchor, field):
                    raise QuarantinedError("external anchor identity mismatch")
            store._guard(
                allow_unenrolled=True,
                expected_registry=None,
                expected_head=None,
                expected_receipt=None,
            )
            _lock_nonblocking(store._fds["lifecycle"], fcntl.LOCK_EX)
            store._guard(
                allow_unenrolled=True,
                expected_registry=None,
                expected_head=None,
                expected_receipt=None,
            )
            r_size, h_size = (
                os.fstat(store._fds["registry"]).st_size,
                os.fstat(store._fds["head"]).st_size,
            )
            if (
                r_size <= 0
                or h_size <= 0
                or r_size > store._limits["registry_byte_cap"]
                or h_size > store._limits["head_byte_cap"]
                or r_size % 4096
                or h_size % 2048
            ):
                raise QuarantinedError("registry/head full-file bounds")
            store._registry_bytes = store._read_existing("registry", r_size)
            store._head_bytes = store._read_existing("head", h_size)
            store._replay()
            receipt_size = os.fstat(store._fds["receipt"]).st_size
            if receipt_size not in (0, 2048):
                raise QuarantinedError("receipt size")
            store._receipt_bytes = (
                store._read_existing("receipt", receipt_size) if receipt_size else b""
            )
            if store._receipt_bytes:
                receipt = decode_frame("receipt", store._receipt_bytes, key)
                if receipt != store._receipt_payload():
                    raise QuarantinedError("receipt binding mismatch")
            elif not allow_unenrolled:
                raise QuarantinedError("UNENROLLED target")
            if any(store._state[k]["pending"] is not None for k in ("JOURNAL", "WAL")):
                raise QuarantinedError("incomplete allocation stays quarantined")
            store._guard(allow_unenrolled=allow_unenrolled)
            return store
        except (OSError, FormatError, StateError, PoisonedError) as exc:
            store._poisoned = True
            store._close_all()
            raise QuarantinedError("existing synthetic authority could not be admitted") from exc
        except BaseException:
            store._poisoned = True
            store._close_all()
            raise

    def _open_chain(self) -> None:
        if not self._root.is_absolute() or ".." in self._root.parts:
            raise QuarantinedError("root must be an absolute unreinterpreted path")
        current = Path("/")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self._chain.append((str(current), fd, os.fstat(fd).st_dev, os.fstat(fd).st_ino))
        for part in self._root.parts[1:]:
            next_fd = _open_relative(fd, part, directory=True)
            current /= part
            info = os.fstat(next_fd)
            self._chain.append((str(current), next_fd, info.st_dev, info.st_ino))
            fd = next_fd
        self._dirs["root"] = fd
        root_info = os.fstat(fd)
        _validate_fd(fd, _dir_witness(root_info), self._root, directory=True)

    def _chain_witness(self) -> tuple[tuple[str, int, int], ...]:
        return tuple((path, dev, ino) for path, _fd, dev, ino in self._chain)

    def _open_root_gate(self) -> None:
        self._fds["gate"] = _open_relative(self._dirs["root"], "profile-gate.lock")
        info = os.fstat(self._fds["gate"])
        _validate_fd(
            self._fds["gate"],
            _file_witness(info, *OBJECTS["gate"]),
            self._root / "profile-gate.lock",
            directory=False,
        )

    def _ensure_fresh_target(self) -> None:
        names = set(os.listdir(self._dirs["root"]))
        if names != {"profile-gate.lock"}:
            raise QuarantinedError("fresh target has existing or unknown objects")

    def _role_path(self, role: str) -> Path:
        if role == "descriptor":
            return self._root / "authority" / "capabilities.frame"
        directory, basename, _operations = OBJECTS[role]
        return self._root / ("" if directory == "root" else directory) / basename

    def _read_existing(self, role: str, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            self._emit(f"before_{role}_read", role, "replay", cursor=len(result))
            self._guard(
                allow_unenrolled=True,
                expected_registry=None,
                expected_head=None,
                expected_receipt=None,
            )
            chunk = os.pread(self._fds[role], min(4096, size - len(result)), len(result))
            if not chunk:
                raise QuarantinedError("premature replay EOF")
            result.extend(chunk)
            self._emit(f"after_{role}_read", role, "replay", cursor=len(result))
            self._guard(
                allow_unenrolled=True,
                expected_registry=None,
                expected_head=None,
                expected_receipt=None,
            )
        if os.fstat(self._fds[role]).st_size != size:
            raise QuarantinedError("replay size changed")
        return bytes(result)

    def _guard(
        self,
        *,
        allow_unenrolled: bool = False,
        expected_registry: bytes | None = b"TRACKED",
        expected_head: bytes | None = b"TRACKED",
        expected_receipt: bytes | None = b"TRACKED",
    ) -> None:
        if self._closed or self._poisoned:
            raise PoisonedError("owner closed or poisoned")
        if time.monotonic() > self._deadline:
            self._poisoned = True
            raise PoisonedError("operation deadline expired")
        try:
            for index, (path, fd, dev, ino) in enumerate(self._chain):
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (dev, ino):
                    raise QuarantinedError("held ancestor changed")
                if index:
                    parent_fd = self._chain[index - 1][1]
                    entry = os.stat(Path(path).name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (
                        dev,
                        ino,
                    ):
                        raise QuarantinedError("advertised ancestor changed")
            if hasattr(self, "_anchor") and self._chain_witness() != self._anchor.root_chain:
                raise QuarantinedError("root chain changed")
            for directory, fd in self._dirs.items():
                witness = self._descriptor["directories"][directory]
                _validate_fd(fd, witness, self._root / directory, directory=True)
                if directory != "root":
                    entry = os.stat(directory, dir_fd=self._dirs["root"], follow_symlinks=False)
                    if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (
                        witness["dev"],
                        witness["ino"],
                    ):
                        raise QuarantinedError("advertised directory changed")
            expected_names = {
                "root": {"authority", "slots", "main.witness", "profile-gate.lock"},
                "authority": {
                    "capabilities.frame",
                    "registry.log",
                    "committed-head.log",
                    "enrollment.receipt",
                    "core-lifecycle.lock",
                },
                "slots": {"journal-0.slot", "journal-1.slot", "wal-0.slot", "wal-1.slot"},
            }
            for directory, names in expected_names.items():
                if set(os.listdir(self._dirs[directory])) != names:
                    raise QuarantinedError(f"unknown or missing {directory} entry")
            for role, witness in self._descriptor["objects"].items():
                fd = self._fds[role]
                _validate_fd(fd, witness, self._role_path(role), directory=False)
                parent_fd = self._dirs[witness["directory"]]
                entry = os.stat(witness["basename"], dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_nlink != 1
                    or (entry.st_dev, entry.st_ino) != (witness["dev"], witness["ino"])
                ):
                    raise QuarantinedError(f"advertised object changed: {role}")
            c = self._fds["descriptor"]
            witness = self._descriptor_witness
            info = os.fstat(c)
            _validate_fd(
                c,
                {"dev": witness.st_dev, "ino": witness.st_ino, "uid": witness.st_uid},
                self._role_path("descriptor"),
                directory=False,
            )
            entry = os.stat(
                "capabilities.frame", dir_fd=self._dirs["authority"], follow_symlinks=False
            )
            if (entry.st_dev, entry.st_ino) != (witness.st_dev, witness.st_ino):
                raise QuarantinedError("advertised descriptor changed")
            if self._descriptor_pending:
                if _read_exact(c, len(self._descriptor_cursor)) != self._descriptor_cursor:
                    raise QuarantinedError("descriptor creation cursor changed")
            elif info.st_size == 16384:
                if (
                    _read_exact(c, 16384) != self._c_bytes
                    or frame_digest(self._c_bytes) != self._anchor.c_digest
                ):
                    raise QuarantinedError("descriptor bytes changed")
            else:
                raise QuarantinedError("missing or partial descriptor")
            for role, expected in (
                ("registry", expected_registry),
                ("head", expected_head),
                ("receipt", expected_receipt),
            ):
                if expected is None:
                    continue
                if expected == b"TRACKED":
                    expected = getattr(self, f"_{role}_bytes")
                if _read_exact(self._fds[role], len(expected)) != expected:
                    raise QuarantinedError(f"{role} bytes changed")
            if not allow_unenrolled and not self._receipt_bytes:
                raise QuarantinedError("UNENROLLED target")
        except (OSError, FormatError, QuarantinedError) as exc:
            self._poisoned = True
            raise PoisonedError("authority identity or bytes changed") from exc

    def _emit(self, event: str, role: str, phase: str, **extra: object) -> None:
        if self._hook is None:
            return
        info = os.fstat(self._fds[role])
        self._hook(
            event,
            {
                "role": role,
                "dev": info.st_dev,
                "ino": info.st_ino,
                "seq": self._seq + 1,
                "phase": phase,
                **extra,
            },
        )

    def _emit_detached(self, event: str, role: str) -> None:
        """Name a test port transition without pretending an external item has an FD."""
        if self._hook is None:
            return
        self._hook(
            event,
            {
                "role": role,
                "dev": None,
                "ino": None,
                "descriptor_dev": self._anchor.descriptor_dev,
                "descriptor_ino": self._anchor.descriptor_ino,
                "seq": 1,
                "phase": "external",
                "live_target_fd": False,
            },
        )

    def _write_object(self, role: str, data: bytes) -> None:
        fd = self._fds[role]
        if os.fstat(fd).st_size != 0:
            raise QuarantinedError("object not empty before exclusive write")
        cursor = 0
        while cursor < len(data):
            self._emit(f"before_{role}_fragment", role, "bootstrap", cursor=cursor)
            self._guard(
                allow_unenrolled=True,
                expected_receipt=data[:cursor] if role == "receipt" else b"TRACKED",
            )
            count = os.pwrite(fd, data[cursor:], cursor)
            if count <= 0:
                raise PoisonedError("bootstrap write made no progress")
            cursor += count
            if role == "descriptor":
                self._descriptor_cursor = data[:cursor]
            self._emit(f"after_{role}_fragment", role, "bootstrap", cursor=cursor)
            self._guard(
                allow_unenrolled=True,
                expected_receipt=data[:cursor] if role == "receipt" else b"TRACKED",
            )
        if _read_exact(fd, len(data)) != data:
            raise PoisonedError("object write mismatch")

    def _sync_directory(self, directory: str, *, stage: str) -> None:
        if self._hook is not None:
            info = os.fstat(self._dirs[directory])
            self._hook(
                "before_directory_barrier",
                {
                    "role": f"directory:{directory}",
                    "dev": info.st_dev,
                    "ino": info.st_ino,
                    "seq": self._seq,
                    "phase": "barrier",
                    "stage": stage,
                },
            )
        self._guard(allow_unenrolled=True)
        os.fsync(self._dirs[directory])
        if self._hook is not None:
            info = os.fstat(self._dirs[directory])
            self._hook(
                "after_directory_barrier",
                {
                    "role": f"directory:{directory}",
                    "dev": info.st_dev,
                    "ino": info.st_ino,
                    "seq": self._seq,
                    "phase": "barrier",
                    "stage": stage,
                },
            )
        self._guard(allow_unenrolled=True)

    def _barrier(self, role: str) -> None:
        self._emit(f"before_{role}_barrier", role, "barrier")
        self._guard(allow_unenrolled=True)
        os.fsync(self._fds[role])
        self._emit(f"after_{role}_barrier", role, "barrier")
        self._guard(allow_unenrolled=True)

    def _receipt_payload(self) -> dict[str, Any]:
        r = self._registry_bytes[:4096]
        h = self._head_bytes[:2048]
        return {
            "v": 1,
            "purpose": PURPOSE,
            "enrollment_id": self._descriptor["enrollment_id"],
            "enrollment_kind": self._descriptor["enrollment_kind"],
            **{
                field: self._descriptor[field]
                for field in (
                    "installation_id",
                    "profile_id",
                    "instance_id",
                    "issuer_epoch",
                    "registry_id",
                    "key_id",
                )
            },
            "c_digest": self._c_digest,
            "genesis_record_digest": frame_digest(r),
            "genesis_head_digest": frame_digest(h),
            "component_state": "ENROLLED_TEST_ONLY",
        }

    def _replay(self) -> None:
        if len(self._registry_bytes) // 4096 != len(self._head_bytes) // 2048:
            raise QuarantinedError("registry/head length mismatch")
        if len(self._registry_bytes) // 4096 > capacity(self._limits):
            raise QuarantinedError("epoch capacity")
        state = initial_state()
        previous_r = previous_h = ZERO
        used_events: set[str] = set()
        used_ops: dict[str, str] = {}
        for index in range(len(self._registry_bytes) // 4096):
            r_frame = self._registry_bytes[index * 4096 : (index + 1) * 4096]
            h_frame = self._head_bytes[index * 2048 : (index + 1) * 2048]
            r = decode_frame("registry", r_frame, self._key)
            h = decode_frame("head", h_frame, self._key)
            seq = index + 1
            if (
                r["seq"] != seq
                or h["seq"] != seq
                or r["prev_record_digest"] != previous_r
                or h["prev_head_digest"] != previous_h
            ):
                raise QuarantinedError("sequence/chain mismatch")
            for field in ("profile_id", "instance_id", "issuer_epoch", "registry_id"):
                if r[field] != self._descriptor[field] or h[field] != self._descriptor[field]:
                    raise QuarantinedError("record identity mismatch")
            if r["c_digest"] != self._c_digest or h["c_digest"] != self._c_digest:
                raise QuarantinedError("record descriptor mismatch")
            if (h["op_id"], h["event_id"], h["record_digest"], h["state_digest"]) != (
                r["op_id"],
                r["event_id"],
                frame_digest(r_frame),
                r["result_state_digest"],
            ):
                raise QuarantinedError("head pair mismatch")
            if r["event_id"] in used_events:
                raise QuarantinedError("duplicate event ID")
            used_events.add(r["event_id"])
            if seq == 1:
                if (
                    r["event"] != "GENESIS"
                    or r["prev_record_digest"] != ZERO
                    or h["prev_head_digest"] != ZERO
                ):
                    raise QuarantinedError("missing genesis")
            elif r["event"] == "GENESIS":
                raise QuarantinedError("duplicate genesis")
            existing = used_ops.get(r["op_id"])
            if r["event"] == "RESET_INTENT":
                if existing is not None:
                    raise QuarantinedError("reused operation ID")
                used_ops[r["op_id"]] = "RESET_INTENT"
            elif r["event"] == "RESET_DONE":
                if existing != "RESET_INTENT":
                    raise QuarantinedError("misordered operation ID")
                used_ops[r["op_id"]] = "RESET_DONE"
            elif r["event"] == "ACTIVE":
                if existing != "RESET_DONE":
                    raise QuarantinedError("misordered operation ID")
                used_ops[r["op_id"]] = "ACTIVE"
            elif existing is not None:
                raise QuarantinedError("reused operation ID")
            else:
                used_ops[r["op_id"]] = r["event"]
            state = apply_record(state, r, self._limits)
            previous_r, previous_h = frame_digest(r_frame), frame_digest(h_frame)
        self._state, self._seq = state, len(self._registry_bytes) // 4096
        self._used_events, self._used_operations = used_events, used_ops

    def _commit(
        self,
        event: str,
        kind: str | None,
        generation: int,
        slot: int | None,
        op_id: str,
        *,
        genesis: bool = False,
    ) -> None:
        self._guard(allow_unenrolled=genesis)
        seq = self._seq + 1
        effect_started = False
        try:
            if seq > capacity(self._limits):
                raise CapacityError("pair capacity")
            future = transition(
                self._state, event, kind, generation, slot, op_id, self._limits["generation_cap"]
            )
            assert_reservations(future, seq, self._limits)
            event_id = uuid.uuid4().hex
            r = {
                "v": 1,
                "profile_id": self._descriptor["profile_id"],
                "instance_id": self._descriptor["instance_id"],
                "issuer_epoch": self._descriptor["issuer_epoch"],
                "registry_id": self._descriptor["registry_id"],
                "c_digest": self._c_digest,
                "seq": seq,
                "op_id": op_id,
                "event_id": event_id,
                "prev_record_digest": frame_digest(self._registry_bytes[-4096:])
                if self._seq
                else ZERO,
                "event": event,
                "kind": kind,
                "generation": generation,
                "slot": slot,
                "prior_state_digest": state_digest(self._state) if self._seq else ZERO,
                "result_state_digest": state_digest(future),
            }
            r_frame = encode_frame("registry", r, self._key)
            h = {
                "v": 1,
                "profile_id": r["profile_id"],
                "instance_id": r["instance_id"],
                "issuer_epoch": r["issuer_epoch"],
                "registry_id": r["registry_id"],
                "c_digest": self._c_digest,
                "seq": seq,
                "op_id": op_id,
                "event_id": event_id,
                "record_digest": frame_digest(r_frame),
                "state_digest": r["result_state_digest"],
                "prev_head_digest": frame_digest(self._head_bytes[-2048:]) if self._seq else ZERO,
            }
            h_frame = encode_frame("head", h, self._key)
            effect_started = True
            self._append_frame("registry", r_frame, event)
            self._barrier("registry")
            self._append_frame("head", h_frame, event)
            self._barrier("head")
            self._emit("before_namespace_postcheck", "head", event)
            self._guard(allow_unenrolled=genesis)
            self._emit("after_namespace_postcheck", "head", event)
            self._emit("before_publish", "head", event)
            self._guard(allow_unenrolled=genesis)
            self._state, self._seq = future, seq
            self._used_events.add(event_id)
            self._used_operations[op_id] = event
            self._emit("after_publish", "head", event)
            self._guard(allow_unenrolled=genesis)
            self._emit("before_ack", "head", event)
            self._guard(allow_unenrolled=genesis)
        except (AuthorityError, StateError, FormatError):
            if effect_started:
                self._poisoned = True
            raise
        except BaseException:
            self._poisoned = True
            raise

    def _append_frame(self, role: str, frame: bytes, event: str) -> None:
        fd = self._fds[role]
        old = getattr(self, f"_{role}_bytes")
        self._emit(f"before_{role}_append", role, event)
        self._guard(allow_unenrolled=(event == "GENESIS"))
        cursor = 0
        while cursor < len(frame):
            self._emit(f"before_{role}_fragment", role, event, cursor=cursor)
            self._guard(allow_unenrolled=(event == "GENESIS"))
            count = os.pwrite(fd, frame[cursor:], len(old) + cursor)
            if count <= 0:
                raise PoisonedError("append made no progress")
            cursor += count
            setattr(self, f"_{role}_bytes", old + frame[:cursor])
            self._emit(f"after_{role}_fragment", role, event, cursor=cursor)
            self._guard(allow_unenrolled=(event == "GENESIS"))
        self._emit(f"after_{role}_append", role, event)
        self._guard(allow_unenrolled=(event == "GENESIS"))

    @_public_operation
    def snapshot(self) -> VerifiedSnapshot:
        self._guard()
        result = VerifiedSnapshot(
            self._seq,
            frame_digest(self._head_bytes[-2048:]),
            deepcopy(self._state),
            "ENROLLED_TEST_ONLY",
        )
        self._guard()
        return result

    def query(self) -> VerifiedSnapshot:
        return self.snapshot()

    def _new_token(self, kind: str, generation: int, slot: int) -> Token:
        if len(self._tokens) >= self._limits["token_cap"]:
            raise CapacityError("token capacity")
        token = Token(self._issuer_nonce, uuid.uuid4().hex)
        ref = (kind, generation, slot)
        self._tokens[token._nonce] = (token, ref)
        self._references[ref] = self._references.get(ref, 0) + 1
        return token

    def _validate_token(self, token: Token, *, require_active: bool = True) -> tuple[str, int, int]:
        if type(token) is not Token or token._issuer != self._issuer_nonce:
            raise AuthorityError("foreign token")
        entry = self._tokens.get(token._nonce)
        if entry is None or entry[0] is not token:
            raise AuthorityError("closed or forged token")
        kind, generation, slot = entry[1]
        if require_active and self._state[kind]["active"] != {
            "generation": generation,
            "slot": slot,
        }:
            raise AuthorityError("stale generation")
        return kind, generation, slot

    @_public_operation
    def open_token(self, kind: str) -> Token:
        self._guard()
        if kind not in ("JOURNAL", "WAL"):
            raise AuthorityError("kind")
        active = self._state[kind]["active"]
        if active is None:
            raise AuthorityError("kind absent")
        self._guard()
        token = self._new_token(kind, active["generation"], active["slot"])
        self._guard()
        return token

    @_public_operation
    def allocate(self, kind: str, *, op_id: str) -> Token:
        self._guard()
        if kind not in ("JOURNAL", "WAL") or type(op_id) is not str or len(op_id) != 32:
            raise AuthorityError("kind or operation ID")
        try:
            int(op_id, 16)
        except ValueError as exc:
            raise AuthorityError("operation ID") from exc
        if op_id.lower() != op_id or op_id in self._used_operations:
            raise AuthorityError("duplicate/noncanonical operation ID")
        if self._state[kind]["active"] is not None or self._state[kind]["pending"] is not None:
            raise AuthorityError("kind occupied")
        if len(self._tokens) >= self._limits["token_cap"]:
            raise CapacityError("token capacity")
        current_reservations = reservations(self._state)
        if capacity(self._limits) - self._seq < sum(current_reservations.values()) + 4:
            raise CapacityError("allocation and retirement room")
        slot = select_slot(self._state, kind, set(self._references))
        generation = self._state[kind]["last_generation"] + 1
        if generation > self._limits["generation_cap"]:
            raise CapacityError("generation cap")
        self._guard()
        self._commit("RESET_INTENT", kind, generation, slot, op_id)
        role = ("journal" if kind == "JOURNAL" else "wal") + str(slot)
        try:
            self._emit("before_slot_admission", role, "RESET_INTENT")
            self._guard()
            _validate_fd(
                self._fds[role],
                self._descriptor["objects"][role],
                self._role_path(role),
                directory=False,
            )
            self._emit("after_slot_admission", role, "RESET_INTENT")
            self._guard()
            self._emit("before_slot_reset", role, "RESET_INTENT")
            self._guard()
            os.ftruncate(self._fds[role], 0)
            self._emit("after_slot_reset", role, "RESET_INTENT")
            self._guard()
            self._emit("before_slot_barrier", role, "RESET_INTENT")
            self._guard()
            os.fsync(self._fds[role])
            self._emit("after_slot_barrier", role, "RESET_INTENT")
            self._guard()
            self._commit("RESET_DONE", kind, generation, slot, op_id)
            self._commit("ACTIVE", kind, generation, slot, op_id)
            token = self._new_token(kind, generation, slot)
            self._guard()
            return token
        except BaseException:
            self._poisoned = True
            raise

    @_public_operation
    def retire(self, token: Token, *, op_id: str) -> None:
        self._guard()
        kind, generation, slot = self._validate_token(token)
        if (
            type(op_id) is not str
            or len(op_id) != 32
            or op_id.lower() != op_id
            or op_id in self._used_operations
        ):
            raise AuthorityError("retirement operation ID")
        try:
            int(op_id, 16)
        except ValueError as exc:
            raise AuthorityError("retirement operation ID") from exc
        self._guard()
        self._commit("RETIRED", kind, generation, slot, op_id)
        self._guard()

    @_public_operation
    def read(self, token: Token, *, offset: int = 0, size: int | None = None) -> bytes:
        self._guard()
        kind, _generation, slot = self._validate_token(token)
        role = ("journal" if kind == "JOURNAL" else "wal") + str(slot)
        length = os.fstat(self._fds[role]).st_size if size is None else size
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or length < 0
            or length > self._limits["operation_byte_cap"]
            or offset + length > self._limits["slot_byte_cap"]
        ):
            raise CapacityError("read bounds")
        try:
            self._emit("before_slot_read", role, "ACTIVE")
            self._guard()
            data = os.pread(self._fds[role], length, offset)
            if len(data) != length:
                raise PoisonedError("short slot read")
            self._emit("after_slot_read", role, "ACTIVE")
            self._guard()
            return data
        except BaseException:
            self._poisoned = True
            raise

    @_public_operation
    def write(self, token: Token, data: bytes, *, offset: int = 0) -> None:
        self._guard()
        kind, _generation, slot = self._validate_token(token)
        if (
            type(data) is not bytes
            or type(offset) is not int
            or offset < 0
            or not 0 < len(data) <= self._limits["operation_byte_cap"]
            or offset + len(data) > self._limits["slot_byte_cap"]
        ):
            raise CapacityError("write bounds")
        role = ("journal" if kind == "JOURNAL" else "wal") + str(slot)
        fd = self._fds[role]
        cursor = 0
        try:
            while cursor < len(data):
                self._emit("before_slot_write", role, "ACTIVE", cursor=cursor)
                self._guard()
                count = os.pwrite(fd, data[cursor:], offset + cursor)
                if count <= 0:
                    raise PoisonedError("slot write made no progress")
                cursor += count
                self._emit("after_slot_write", role, "ACTIVE", cursor=cursor)
                self._guard()
            self._guard()
        except BaseException:
            self._poisoned = True
            raise

    @_public_operation
    def sync(self, token: Token) -> None:
        self._guard()
        kind, _generation, slot = self._validate_token(token)
        role = ("journal" if kind == "JOURNAL" else "wal") + str(slot)
        try:
            self._emit("before_slot_barrier", role, "ACTIVE")
            self._guard()
            os.fsync(self._fds[role])
            self._emit("after_slot_barrier", role, "ACTIVE")
            self._guard()
        except BaseException:
            self._poisoned = True
            raise

    @_public_operation
    def close_token(self, token: Token) -> None:
        self._guard()
        kind, generation, slot = self._validate_token(token, require_active=False)
        self._guard()
        self._tokens.pop(token._nonce)
        ref = kind, generation, slot
        self._references[ref] -= 1
        if self._references[ref] == 0:
            self._references.pop(ref)
        self._guard()

    @_public_operation
    def close(self) -> None:
        if self._closed:
            return
        if not self._poisoned:
            self._guard()
        self._close_all()
        if self._poisoned:
            raise PoisonedError("owner close uncertain or poisoned")

    def _close_all(self) -> None:
        if self._closed:
            return
        leases = [self._fds[role] for role in ("lifecycle", "gate") if role in self._fds]
        unique: list[int] = list(
            dict.fromkeys(
                [
                    *(fd for role, fd in self._fds.items() if role not in ("lifecycle", "gate")),
                    *self._dirs.values(),
                    *(fd for _path, fd, _dev, _ino in self._chain),
                ]
            )
        )
        self._fds.clear()
        self._dirs.clear()
        self._chain.clear()
        uncertain = False
        for fd in reversed(unique):
            try:
                os.close(fd)
            except OSError:
                # A failed close may already have released/reused its numeric fd.
                # Never retry it, and never report ownership as clean.
                self._poisoned = True
                uncertain = True
        if uncertain:
            # Keep the still-owned leases to process exit; another same-process
            # owner also refuses the root even if a numeric FD was recycled.
            _UNCERTAIN_LEASES.append(tuple(leases))
            if hasattr(self, "_root"):
                _UNSAFE_ROOTS.add(str(self._root))
        else:
            for fd in leases:
                try:
                    os.close(fd)
                except OSError:
                    self._poisoned = True
                    if hasattr(self, "_root"):
                        _UNSAFE_ROOTS.add(str(self._root))
        self._closed = True
        _LIVE_STORES.discard(self)
