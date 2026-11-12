"""Private local custody; only authenticated public envelope bytes leave this store.

This is neither remote durability nor financial write authority. A capsule retains
its temporary decrypt identity. Ambiguous I/O withholds success and is never an
instruction to regenerate or replace a persisted operation.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import stat
import struct
from pathlib import Path

from finance_core._native_age_v1 import NativeAgeToolchainV1, derive_recipient_v1
from finance_core.profile_paths import _reject_acl_grants

from ._recovery_rows_v1 import strict_json
from .contracts import canonical
from .prepared_envelope_v1 import (
    PreparedEnvelopeErrorV1,
    PreparedEnvelopeKeyContextV1,
    PreparedEnvelopeLimitsV1,
    PreparedEnvelopeRefV1,
    _check_local_envelope_v1,
    _create_envelope_v1,
    _key_context,
    _reference_for_content_v1,
)
from .prepared_finalization_v1 import PreparedFinalizationServiceV1
from .recovery_material_v1 import RecoveryEvidenceAuthenticatorV1

_MAGIC = b"FCLOC001"
_MAC_DOMAIN = b"finance-prepared-local-capsule-auth-v1\0"
_SLOT_DOMAIN = b"finance-prepared-local-slot-v1\0"
_NAMESPACE_DOMAIN = b"finance-prepared-local-namespace-v1\0"
_HEX = re.compile(r"[0-9a-f]{64}")
_HEADER = {
    "format",
    "slot",
    "context_id",
    "envelope_sha256",
    "envelope_size_bytes",
    "temporary_recipient",
    "identity_sha256",
    "mac",
}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK


def _fail(code: str, message: str):
    raise PreparedEnvelopeErrorV1(code, message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoint(name: str) -> None:
    """Private process-fault seam; no runtime/caller fault options."""


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _Custody:
    """Close data and directories before releasing either lock.

    Remove a close target before calling close: an error may occur after the OS
    released it. Never close that integer a second time. Keep all other known
    descriptors/locks on the poisoned store until the process exits.
    """

    def __init__(self, store: PreparedEnvelopeStoreV1):
        self.store = store
        self.handles: list[tuple[int, str]] = []
        self.directories: list[tuple[int, Path, int | None, str | None]] = []
        self.locks: list[tuple[int, int, str, Path]] = []

    def keep(self, fd: int, role: str = "data") -> int:
        self.handles.append((fd, role))
        return fd

    def close(self) -> None:
        ordered = [pair for pair in reversed(self.handles) if pair[1] != "lock"]
        ordered += [pair for pair in reversed(self.handles) if pair[1] == "lock"]
        for fd, role in ordered:
            self.handles.remove((fd, role))
            try:
                os.close(fd)
            except BaseException as exc:
                self.store._uncertain = True
                self.store._held_handles = self.handles[:]
                self.store._ambiguous_close = fd
                raise PreparedEnvelopeErrorV1(
                    "ARTIFACT_UNKNOWN", "Actual descriptor close is uncertain"
                ) from exc

    def check(self) -> None:
        for fd, path, parent, name in self.directories:
            if parent is not None and name is None:
                _fail("ARTIFACT_UNKNOWN", "Incomplete directory custody")
            named = (
                path.lstat()
                if parent is None
                else os.stat(str(name), dir_fd=parent, follow_symlinks=False)
            )
            info = os.fstat(fd)
            private = path == self.store.root or self.store.root in path.parents
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or _inode(info) != _inode(named)
                or _inode(path.lstat()) != _inode(named)
                or info.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
                or (
                    stat.S_IMODE(info.st_mode) != 0o700
                    if private
                    else bool(info.st_mode & 0o022) and not sticky_root
                )
            ):
                _fail("ARTIFACT_UNKNOWN", "Store directory identity changed")
            _reject_acl_grants(fd, path)
        for fd, parent, name, path in self.locks:
            self.store._file_identity(parent, name, fd, path)


class PreparedEnvelopeStoreV1:
    """One immutable capsule per namespace/operation, with external references.

    Configure an existing canonical 0700 root. The public envelope returned by
    read() contains no retained temporary identity. Never upload the capsule or
    traverse the private store to assemble a remote backup.
    """

    def __init__(
        self,
        root: Path,
        *,
        namespace: str,
        recovery_key_id: str,
        recovery_recipient: str,
        key_epoch: int,
        authentication_key_id: str,
        authentication_key: bytes,
        authenticator: RecoveryEvidenceAuthenticatorV1,
        toolchain: NativeAgeToolchainV1,
        limits: PreparedEnvelopeLimitsV1 | None = None,
    ):
        if not isinstance(root, Path) or not root.is_absolute() or root.resolve() != root:
            _fail("invalid_store", "An existing canonical absolute private root is required")
        if type(authentication_key) is not bytes or len(authentication_key) != 32:
            _fail("invalid_store", "An independent exact 32-byte authentication key is required")
        if limits is not None and type(limits) is not PreparedEnvelopeLimitsV1:
            _fail("invalid_store", "Exact envelope limits required")
        self.root = root
        self.key_context = PreparedEnvelopeKeyContextV1(
            namespace, recovery_key_id, recovery_recipient, key_epoch, authentication_key_id
        )
        _key_context(self.key_context)
        self._authentication_key = authentication_key
        self.authenticator, self.toolchain = authenticator, toolchain
        self.limits = limits or PreparedEnvelopeLimitsV1()
        self.limits.__post_init__()
        self._uncertain = False
        self._held_handles: list[tuple[int, str]] = []
        self._ambiguous_close: int | None = None
        self._namespace = _sha(_NAMESPACE_DOMAIN + canonical(namespace).encode())
        custody = _Custody(self)
        try:
            self._root(custody)
            custody.check()
        finally:
            custody.close()

    def _usable(self) -> None:
        if self._uncertain:
            _fail("ARTIFACT_UNKNOWN", "Uncertain custody requires independent process recovery")

    def _root(self, custody: _Custody) -> int:
        parent = None
        fd = -1
        for path in reversed((self.root, *self.root.parents)):
            fd = custody.keep(os.open(path, _DIR_FLAGS), "directory")
            info, named = os.fstat(fd), path.lstat()
            private = path == self.root
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or _inode(info) != _inode(named)
                or info.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
                or (
                    stat.S_IMODE(info.st_mode) != 0o700
                    if private
                    else bool(info.st_mode & 0o022) and not sticky_root
                )
            ):
                _fail("invalid_store", "Private root or trusted ancestor is unsafe")
            _reject_acl_grants(fd, path)
            custody.directories.append((fd, path, parent, None if parent is None else path.name))
            parent = fd
        return fd

    def _directory(self, parent: int, name: str, path: Path, custody: _Custody) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
        fd = custody.keep(os.open(name, _DIR_FLAGS, dir_fd=parent), "directory")
        info = os.fstat(fd)
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or _inode(info) != _inode(os.stat(name, dir_fd=parent, follow_symlinks=False))
        ):
            _fail("ARTIFACT_UNKNOWN", "Managed directory is not private or stable")
        _reject_acl_grants(fd, path)
        custody.directories.append((fd, path, parent, name))
        os.fsync(fd)
        return fd

    def _file(
        self,
        parent: int,
        name: str,
        path: Path,
        custody: _Custody,
        *,
        create: bool = False,
        exclusive: bool = False,
        links: int = 1,
        lock: bool = False,
    ) -> int:
        flags = _FILE_FLAGS | (os.O_RDWR if create else os.O_RDONLY)
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        fd = custody.keep(os.open(name, flags, 0o600, dir_fd=parent), "lock" if lock else "data")
        self._file_identity(parent, name, fd, path, links=links)
        if lock:
            if os.fstat(fd).st_size != 0:
                _fail("ARTIFACT_UNKNOWN", "Unexpected lock contents")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fsync(fd)
            os.fsync(parent)
            custody.locks.append((fd, parent, name, path))
        return fd

    def _file_identity(self, parent, name, fd, path, *, links=1):
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != links
            or _inode(info) != _inode(named)
        ):
            _fail("ARTIFACT_UNKNOWN", "Capsule or lock file identity is unsafe")
        _reject_acl_grants(fd, path)
        return info

    def _inventory(self, namespace: int, path: Path, custody: _Custody) -> tuple[int, int]:
        slots, total = 0, 0
        for name in os.listdir(namespace):
            if name == "namespace.lock":
                continue
            if _HEX.fullmatch(name) is None:
                _fail("ARTIFACT_UNKNOWN", "Unknown namespace entry")
            slot = self._directory(namespace, name, path / name, custody)
            slots += 1
            entries = set(os.listdir(slot))
            if not entries <= {"operation.lock", "candidate", "final"}:
                _fail("ARTIFACT_UNKNOWN", "Unknown slot entry")
            if not entries:
                self._file(
                    slot,
                    "operation.lock",
                    path / name / "operation.lock",
                    custody,
                    create=True,
                )
                os.fsync(slot)
                entries.add("operation.lock")
            if "operation.lock" not in entries:
                _fail("ARTIFACT_UNKNOWN", "Incomplete slot custody")
            lock_fd = self._file(slot, "operation.lock", path / name / "operation.lock", custody)
            if os.fstat(lock_fd).st_size:
                _fail("ARTIFACT_UNKNOWN", "Unexpected operation lock contents")
            inode_seen = set()
            pair = "candidate" in entries and "final" in entries
            for filename in entries - {"operation.lock"}:
                fd = self._file(
                    slot, filename, path / name / filename, custody, links=2 if pair else 1
                )
                info = os.fstat(fd)
                if pair:
                    other = "final" if filename == "candidate" else "candidate"
                    if _inode(info) != _inode(os.stat(other, dir_fd=slot, follow_symlinks=False)):
                        _fail("ARTIFACT_UNKNOWN", "Uncontrolled double-file publication")
                if info.st_size > self.limits.max_capsule_bytes:
                    _fail("ARTIFACT_UNKNOWN", "Capsule exceeds configured size")
                if _inode(info) not in inode_seen:
                    total += info.st_size
                    inode_seen.add(_inode(info))
        if slots > self.limits.max_slots or total > self.limits.max_store_bytes:
            _fail("store_limit_exceeded", "Namespace exceeds its fixed quota")
        return slots, total

    def _open(self, operation: str, custody: _Custody, *, create: bool):
        if type(operation) is not str or not 1 <= len(operation.encode()) <= 256:
            _fail("invalid_store", "A bounded exact operation identity is required")
        slot_name = _sha(_SLOT_DOMAIN + canonical([self.key_context.namespace, operation]).encode())
        root = self._root(custody)
        namespace_path = self.root / self._namespace
        namespace = self._directory(root, self._namespace, namespace_path, custody)
        self._file(
            namespace,
            "namespace.lock",
            namespace_path / "namespace.lock",
            custody,
            create=True,
            lock=True,
        )
        slots, total = self._inventory(namespace, namespace_path, custody)
        exists = slot_name in os.listdir(namespace)
        if not exists and (not create or slots >= self.limits.max_slots):
            _fail("absent_local" if not create else "store_limit_exceeded", "No admissible slot")
        slot_path = namespace_path / slot_name
        slot = self._directory(namespace, slot_name, slot_path, custody)
        self._file(
            slot, "operation.lock", slot_path / "operation.lock", custody, create=True, lock=True
        )
        custody.check()
        return slot, slot_path, slot_name, total

    def _capsule(self, identity: bytes, envelope: bytes, ref, slot: str) -> bytes:
        header = {
            "format": "prepared-local-capsule-v1",
            "slot": slot,
            "context_id": ref.context_id,
            "envelope_sha256": _sha(envelope),
            "envelope_size_bytes": len(envelope),
            "temporary_recipient": ref.temporary_recipient,
            "identity_sha256": _sha(identity),
        }
        header["mac"] = hmac.digest(
            self._authentication_key, _MAC_DOMAIN + canonical(header).encode(), "sha256"
        ).hex()
        encoded = canonical(header).encode()
        if (
            not 1 <= len(encoded) <= self.limits.max_private_header_bytes
            or not 1 <= len(identity) <= self.limits.max_identity_bytes
            or not 1 <= len(envelope) <= self.limits.max_envelope_bytes
        ):
            _fail("envelope_limit_exceeded", "Private capsule exceeds configured limits")
        return (
            _MAGIC
            + struct.pack(">QQQ", len(encoded), len(identity), len(envelope))
            + encoded
            + identity
            + envelope
        )

    def _decode(self, capsule: bytes, slot: str):
        if not 32 <= len(capsule) <= self.limits.max_capsule_bytes or capsule[:8] != _MAGIC:
            _fail("ARTIFACT_UNKNOWN", "Invalid or incomplete private capsule")
        header_len, identity_len, envelope_len = struct.unpack(">QQQ", capsule[8:32])
        if (
            not 1 <= header_len <= self.limits.max_private_header_bytes
            or not 1 <= identity_len <= self.limits.max_identity_bytes
            or not 1 <= envelope_len <= self.limits.max_envelope_bytes
            or 32 + header_len + identity_len + envelope_len != len(capsule)
        ):
            _fail("ARTIFACT_UNKNOWN", "Private capsule lengths are inconsistent")
        raw = capsule[32 : 32 + header_len]
        try:
            header = strict_json(
                raw, self.limits.closure_limits, maximum=self.limits.max_private_header_bytes
            )
        except ValueError as exc:
            raise PreparedEnvelopeErrorV1("ARTIFACT_UNKNOWN", "Invalid capsule header") from exc
        if type(header) is not dict or set(header) != _HEADER or canonical(header).encode() != raw:
            _fail("ARTIFACT_UNKNOWN", "Private capsule header is not exact canonical JSON")
        identity = capsule[32 + header_len : 32 + header_len + identity_len]
        envelope = capsule[32 + header_len + identity_len :]
        mac = header["mac"]
        unsigned = {key: value for key, value in header.items() if key != "mac"}
        expected_mac = hmac.digest(
            self._authentication_key, _MAC_DOMAIN + canonical(unsigned).encode(), "sha256"
        ).hex()
        if (
            type(mac) is not str
            or not hmac.compare_digest(mac, expected_mac)
            or header["format"] != "prepared-local-capsule-v1"
            or header["slot"] != slot
            or type(header["envelope_size_bytes"]) is not int
            or header["envelope_size_bytes"] != len(envelope)
            or header["envelope_sha256"] != _sha(envelope)
            or header["identity_sha256"] != _sha(identity)
            or type(header["context_id"]) is not str
            or _HEX.fullmatch(header["context_id"]) is None
        ):
            _fail("ARTIFACT_UNKNOWN", "Private capsule authentication or binding failed")
        recipient = derive_recipient_v1(
            identity, self.toolchain, deadline_seconds=self.limits.child_deadline_seconds
        )
        if header["temporary_recipient"] != recipient:
            _fail("ARTIFACT_UNKNOWN", "Private capsule identity recipient mismatch")
        return identity, envelope, header

    def _read_fd(self, fd: int) -> bytes:
        size = os.fstat(fd).st_size
        if not 32 <= size <= self.limits.max_capsule_bytes:
            _fail("ARTIFACT_UNKNOWN", "Private capsule has incomplete or excessive size")
        chunks = []
        position = 0
        while position <= size:
            chunk = os.pread(fd, min(1024 * 1024, size + 1 - position), position)
            if not chunk:
                break
            chunks.append(chunk)
            position += len(chunk)
        if position != size or os.fstat(fd).st_size != size:
            _fail("ARTIFACT_UNKNOWN", "Private capsule changed during readback")
        return b"".join(chunks)

    def _existing(self, slot, path, slot_name, custody):
        entries = set(os.listdir(slot)) - {"operation.lock"}
        if not entries:
            return None
        if not entries <= {"candidate", "final"}:
            _fail("ARTIFACT_UNKNOWN", "Unknown slot publication state")
        name = "final" if "final" in entries else "candidate"
        fd = self._file(slot, name, path / name, custody, links=2 if len(entries) == 2 else 1)
        info = os.fstat(fd)
        if len(entries) == 2 and _inode(info) != _inode(
            os.stat("candidate", dir_fd=slot, follow_symlinks=False)
        ):
            _fail("ARTIFACT_UNKNOWN", "Candidate and final are not one publication")
        capsule = self._read_fd(fd)
        identity, envelope, header = self._decode(capsule, slot_name)
        return fd, identity, envelope, header, entries

    def _validate(self, identity, envelope, header, ref):
        if (
            ref.key_context != self.key_context
            or header["context_id"] != ref.context_id
            or header["temporary_recipient"] != ref.temporary_recipient
        ):
            _fail("context_mismatch", "Stored content belongs to another immutable context")
        return _check_local_envelope_v1(
            envelope,
            expected=ref,
            temporary_identity=identity,
            authentication_key=self._authentication_key,
            authenticator=self.authenticator,
            toolchain=self.toolchain,
            limits=self.limits,
        )

    def _publish(self, slot, path, fd, entries, custody):
        os.fsync(fd)
        custody.check()
        if "final" not in entries:
            self._file_identity(slot, "candidate", fd, path / "candidate")
            os.link("candidate", "final", src_dir_fd=slot, dst_dir_fd=slot, follow_symlinks=False)
            _checkpoint("link")
            entries = {"candidate", "final"}
        if "candidate" in entries:
            self._file_identity(slot, "final", fd, path / "final", links=2)
            if _inode(os.stat("candidate", dir_fd=slot, follow_symlinks=False)) != _inode(
                os.fstat(fd)
            ):
                _fail("ARTIFACT_UNKNOWN", "Candidate identity changed before unlink")
            os.unlink("candidate", dir_fd=slot)
            _checkpoint("unlink")
        os.fsync(fd)
        os.fsync(slot)
        _checkpoint("directory_sync")
        custody.check()
        self._file_identity(slot, "final", fd, path / "final")

    def seal(
        self,
        preparation: PreparedFinalizationServiceV1,
        operation_id: str,
        *,
        expected_intent_sha256: str,
    ) -> PreparedEnvelopeRefV1:
        self._usable()
        if type(preparation) is not PreparedFinalizationServiceV1:
            _fail("invalid_preparation", "Actual protected preparation service required")
        custody = _Custody(self)
        try:
            slot, path, slot_name, total = self._open(operation_id, custody, create=True)
            content = PreparedFinalizationServiceV1.export_content_v1(
                preparation, operation_id, expected_intent_sha256=expected_intent_sha256
            )
            existing = self._existing(slot, path, slot_name, custody)
            if existing:
                fd, identity, envelope, header, entries = existing
                ref = _reference_for_content_v1(
                    envelope,
                    content_anchor=content.anchor,
                    key_context=self.key_context,
                    temporary_recipient=header["temporary_recipient"],
                    authentication_key=self._authentication_key,
                    limits=self.limits,
                )
                self._validate(identity, envelope, header, ref)
            else:
                if content.observed_local_state != "PREPARED_OFFLINE":
                    _fail(
                        "invalid_preparation", "First seal requires a current pending preparation"
                    )
                identity, envelope, ref = _create_envelope_v1(
                    content,
                    key_context=self.key_context,
                    authentication_key=self._authentication_key,
                    authenticator=self.authenticator,
                    toolchain=self.toolchain,
                    limits=self.limits,
                )
                capsule = self._capsule(identity, envelope, ref, slot_name)
                if total + len(capsule) > self.limits.max_store_bytes:
                    _fail("store_limit_exceeded", "New capsule would exceed namespace byte quota")
                custody.check()
                fd = self._file(
                    slot, "candidate", path / "candidate", custody, create=True, exclusive=True
                )
                _checkpoint("create")
                view = memoryview(capsule)
                while view:
                    written = os.write(fd, view[: 1024 * 1024])
                    if not 0 < written <= len(view[: 1024 * 1024]):
                        _fail("ARTIFACT_UNKNOWN", "Private capsule write made no progress")
                    view = view[written:]
                    _checkpoint("write")
                _checkpoint("full_write")
                os.fsync(fd)
                _checkpoint("file_sync")
                readback = self._read_fd(fd)
                if readback != capsule:
                    _fail("ARTIFACT_UNKNOWN", "Same-descriptor capsule readback differs")
                actual_identity, actual_envelope, header = self._decode(readback, slot_name)
                self._validate(actual_identity, actual_envelope, header, ref)
                _checkpoint("readback")
                entries = {"candidate"}
            self._publish(slot, path, fd, entries, custody)
        except OSError as exc:
            raise PreparedEnvelopeErrorV1(
                "ARTIFACT_UNKNOWN", "Filesystem custody or durability could not be established"
            ) from exc
        finally:
            custody.close()
        _checkpoint("close_before_reply")
        return ref

    def read(self, ref: PreparedEnvelopeRefV1) -> bytes:
        self._usable()
        if type(ref) is not PreparedEnvelopeRefV1 or ref.key_context != self.key_context:
            _fail("context_mismatch", "An independently retained exact reference is required")
        custody = _Custody(self)
        try:
            slot, path, slot_name, _ = self._open(
                ref.content_anchor.operation_id, custody, create=False
            )
            existing = self._existing(slot, path, slot_name, custody)
            if existing is None or "final" not in existing[4]:
                _fail("ARTIFACT_UNKNOWN", "No fully published local capsule is established")
            fd, identity, envelope, header, entries = existing
            self._validate(identity, envelope, header, ref)
            self._publish(slot, path, fd, entries, custody)
        except OSError as exc:
            raise PreparedEnvelopeErrorV1(
                "ARTIFACT_UNKNOWN", "Cannot establish capsule read custody"
            ) from exc
        finally:
            custody.close()
        _checkpoint("close_before_reply")
        return envelope
