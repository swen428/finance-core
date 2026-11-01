"""Private, pinned destination for one lifecycle SQLite snapshot cut."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from finance_core.profile_paths import ProfilePathError, _reject_acl_grants, _trusted_ancestor

from .contracts import LifecycleError

_SIDECARS = ("-wal", "-shm", "-journal")


class _SnapshotDestination:
    def __init__(self, path: Path, parent_fd: int, file_fd: int):
        self.path = path
        self.parent_fd = parent_fd
        self.file_fd = file_fd
        parent = os.fstat(parent_fd)
        output = os.fstat(file_fd)
        self.parent_identity = parent.st_dev, parent.st_ino
        self.file_identity = output.st_dev, output.st_ino

    def check(self) -> None:
        try:
            parent = os.fstat(self.parent_fd)
            named_parent = os.stat(self.path.parent, follow_symlinks=False)
            output = os.fstat(self.file_fd)
            named_output = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
            if (
                (parent.st_dev, parent.st_ino) != self.parent_identity
                or (named_parent.st_dev, named_parent.st_ino) != self.parent_identity
                or not stat.S_ISDIR(parent.st_mode)
                or not stat.S_ISDIR(named_parent.st_mode)
                or parent.st_uid != os.getuid()
                or named_parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
                or stat.S_IMODE(named_parent.st_mode) != 0o700
                or (output.st_dev, output.st_ino) != self.file_identity
                or (named_output.st_dev, named_output.st_ino) != self.file_identity
                or not stat.S_ISREG(output.st_mode)
                or not stat.S_ISREG(named_output.st_mode)
                or output.st_uid != os.getuid()
                or output.st_nlink != 1
                or stat.S_IMODE(output.st_mode) != 0o600
            ):
                raise LifecycleError("Snapshot destination identity or private role changed")
            _reject_acl_grants(self.parent_fd, self.path.parent)
            _reject_acl_grants(self.file_fd, self.path)
            for suffix in _SIDECARS:
                try:
                    os.stat(
                        self.path.name + suffix,
                        dir_fd=self.parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                raise LifecycleError("Snapshot destination has a SQLite sidecar")
        except (OSError, ProfilePathError) as exc:
            raise LifecycleError("Snapshot destination cannot be verified") from exc

    def backup(self, source: sqlite3.Connection) -> None:
        self.check()
        target = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0)
        try:
            self.check()
            sqlite3.Connection.backup(source, target)
        finally:
            target.close()
        self.check()

    def closed_sha256(self) -> str:
        """Hash the closed SQLite file through the pinned inode, then sync both names."""
        self.check()
        sha = hashlib.sha256()
        os.lseek(self.file_fd, 0, os.SEEK_SET)
        while chunk := os.read(self.file_fd, 1024 * 1024):
            sha.update(chunk)
        self.check()
        os.fsync(self.file_fd)
        os.fsync(self.parent_fd)
        self.check()
        return sha.hexdigest()


@contextmanager
def _secure_snapshot_destination(destination: Path):
    """Reserve a fresh private file without following or replacing its final name.

    Caller supplies an existing trusted 0700 parent. A failed cut leaves its
    partial file at 0600 for explicit disposition and never returns a cut.
    """
    path = Path(destination)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise LifecycleError("Snapshot destination must be an absolute canonical path")
    parent = path.parent
    try:
        if parent.resolve(strict=True) != parent:
            raise LifecycleError("Snapshot destination parent is not canonical")
        _trusted_ancestor(parent)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise LifecycleError("Snapshot parent must be owned private directory")
            _reject_acl_grants(parent_fd, parent)
            file_fd = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                stage = _SnapshotDestination(path, parent_fd, file_fd)
                stage.check()
                yield stage
            finally:
                os.close(file_fd)
        finally:
            os.close(parent_fd)
    except (OSError, ProfilePathError) as exc:
        raise LifecycleError("Snapshot destination is not a fresh trusted private file") from exc
