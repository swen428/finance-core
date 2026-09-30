"""Internal, component-only SQLite disk snapshot for a trusted frozen Core owner.

The caller owns an already recovered source connection, the managed exclusion,
and a newly created private stage. It must keep exclusion until the source has
really closed and the complete cut is finished. This module grants no managed
authority, publishes no backup, and never reports a verified full cut.
Failures leave the stage and any partial SQLite files for explicit disposition.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finance_core.profile_paths import ProfilePathError, _reject_acl_grants

_OUTPUT = "core.sqlite"
_SIDECARS = ("-wal", "-shm", "-journal")
_READBACK = r"""
import json
import sqlite3
import sys
from pathlib import Path

uri = Path(sys.argv[1]).as_uri() + "?mode=ro"
connection = sqlite3.connect(uri, uri=True, timeout=0)
try:
    connection.execute("PRAGMA query_only=ON")
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchmany(2)
    fk_problem = connection.execute("PRAGMA foreign_key_check").fetchone()
    schema_count = connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]
    page_count = connection.execute("PRAGMA page_count").fetchone()[0]
    print(json.dumps({"mode": mode, "integrity": integrity, "fk_problem": fk_problem,
                      "schema_count": schema_count, "page_count": page_count}))
finally:
    connection.close()
"""


class DiskSnapshotError(RuntimeError):
    """The component snapshot did not produce a verified standalone file."""


@dataclass(frozen=True)
class DiskSnapshotLimits:
    """Trusted, finite P2 subset; full-cut limits are owned by the coordinator."""

    max_core_db_bytes: int
    max_stage_bytes: int
    min_free_bytes: int
    backup_pages_per_step: int

    def validate(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0 or value > 2**63 - 1:
                raise DiskSnapshotError(f"Invalid disk snapshot limit: {name}")
        if self.max_stage_bytes < self.max_core_db_bytes:
            raise DiskSnapshotError("Stage limit is smaller than Core database limit")
        if self.backup_pages_per_step > 1024:
            raise DiskSnapshotError("Backup step exceeds bounded page count")


@dataclass(frozen=True)
class DiskSnapshotReceipt:
    """Closed component file evidence; not a cut, publication, or restore grant."""

    output: Path
    byte_length: int
    sha256: str
    page_count: int
    schema_object_count: int
    journal_mode: str


@dataclass(frozen=True)
class StagedDiskSnapshot:
    """Unverified, JSON-serializable stage evidence; never a success receipt.

    A parent must supply its own trusted ``private_stage`` to verification.
    In particular, the output path in a worker's descriptor grants no authority
    to choose which file the parent opens.
    """

    output: str
    byte_length: int
    sha256: str
    page_count: int
    stage_dev: int
    stage_ino: int
    output_dev: int
    output_ino: int


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DiskSnapshotError("Disk snapshot deadline expired")


def _checkpoint(deadline: float, control_check: Callable[[], None] | None) -> None:
    _check_deadline(deadline)
    if control_check is not None:
        control_check()


def _sidecars_absent(output: Path) -> None:
    if any(os.path.lexists(f"{output}{suffix}") for suffix in _SIDECARS):
        raise DiskSnapshotError("Disk snapshot has a SQLite sidecar")


def _check_private_acl(fd: int, path: Path) -> None:
    try:
        _reject_acl_grants(fd, path)
    except ProfilePathError as exc:
        raise DiskSnapshotError(f"Disk snapshot path has an unsafe ACL: {path}") from exc


def _check_output_role(fd: int, output: Path, expected: os.stat_result) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise DiskSnapshotError("Disk snapshot is not a private regular file")
    if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        raise DiskSnapshotError("Disk snapshot identity changed")
    _check_private_acl(fd, output)
    named = os.stat(output, follow_symlinks=False)
    if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
        raise DiskSnapshotError("Disk snapshot path changed during role inspection")
    return info


def _hash_closed_file(
    output: Path,
    expected: os.stat_result,
    deadline: float,
    control_check: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = _check_output_role(fd, output, expected)
        if info.st_size != expected.st_size:
            raise DiskSnapshotError("Disk snapshot identity changed")
        while chunk := os.read(fd, 1024 * 1024):
            _checkpoint(deadline, control_check)
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _closed_output_info(output: Path, expected: os.stat_result, limit: int) -> os.stat_result:
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = _check_output_role(fd, output, expected)
    finally:
        os.close(fd)
    if info.st_size <= 0 or info.st_size > limit:
        raise DiskSnapshotError("Disk snapshot exceeds size limit")
    return info


def _source_pragma(source: sqlite3.Connection, name: str) -> int:
    cursor = source.execute(f"PRAGMA main.{name}")
    try:
        row = cursor.fetchone()
        if row is None or type(row[0]) is not int:
            raise DiskSnapshotError("Source SQLite metadata is invalid")
        return row[0]
    finally:
        cursor.close()


def _check_stage_bytes(stage_fd: int, maximum: int) -> None:
    total = 0
    for name in os.listdir(stage_fd):
        if name not in {_OUTPUT, *(_OUTPUT + suffix for suffix in _SIDECARS)}:
            raise DiskSnapshotError("Unexpected file in private snapshot stage")
        info = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise DiskSnapshotError("Non-regular file in private snapshot stage")
        total += info.st_size
        if total > maximum:
            raise DiskSnapshotError("Snapshot stage exceeded byte limit")


def _direct_readback(
    output: Path,
    deadline: float,
    control_check: Callable[[], None] | None,
) -> dict[str, Any]:
    """Fixed reader process checks SQLite directly, without descendants."""
    connection = sqlite3.connect(output.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:

        def progress() -> int:
            try:
                _checkpoint(deadline, control_check)
            except BaseException:
                return 1
            return 0

        connection.set_progress_handler(progress, 1024)
        _checkpoint(deadline, control_check)
        connection.execute("PRAGMA query_only=ON")
        _checkpoint(deadline, control_check)
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        _checkpoint(deadline, control_check)
        integrity = [list(row) for row in connection.execute("PRAGMA integrity_check").fetchmany(2)]
        _checkpoint(deadline, control_check)
        fk_problem = connection.execute("PRAGMA foreign_key_check").fetchone()
        _checkpoint(deadline, control_check)
        schema_count = connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]
        _checkpoint(deadline, control_check)
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        return {
            "mode": mode,
            "integrity": integrity,
            "fk_problem": fk_problem,
            "schema_count": schema_count,
            "page_count": page_count,
        }
    finally:
        connection.close()


def stage_disk_snapshot(
    source: sqlite3.Connection,
    *,
    private_stage: Path,
    limits: DiskSnapshotLimits,
    deadline_monotonic: float,
    _control_check: Callable[[], None] | None = None,
) -> StagedDiskSnapshot:
    """Backup one frozen source into fixed ``core.sqlite`` without readback.

    The trusted caller supplies a live source connection and a fresh 0700 stage
    under validated custody, holds the exclusive managed cut throughout this
    call, and closes the source itself before releasing that cut. A connection or
    directory path alone cannot establish those preconditions. No source path is
    accepted, and the source is never normalized or modified here. This returns
    only unverified stage evidence; the caller must close/reap the source worker
    and separately verify before treating the output as a component snapshot.
    """
    if not isinstance(source, sqlite3.Connection):
        raise DiskSnapshotError("A SQLite source connection is required")
    if not isinstance(private_stage, Path) or not private_stage.is_absolute():
        raise DiskSnapshotError("A private stage path is required")
    if not isinstance(limits, DiskSnapshotLimits):
        raise DiskSnapshotError("Disk snapshot limits are required")
    limits.validate()
    if type(deadline_monotonic) is not float or not (
        time.monotonic() < deadline_monotonic < float("inf")
    ):
        raise DiskSnapshotError("A finite future deadline is required")
    if source.in_transaction:
        raise DiskSnapshotError("Source has an active transaction")

    stage_fd = os.open(private_stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        stage_info = os.fstat(stage_fd)
        if stage_info.st_uid != os.getuid() or stat.S_IMODE(stage_info.st_mode) != 0o700:
            raise DiskSnapshotError("Stage must be owner-only 0700")
        _check_private_acl(stage_fd, private_stage)
        path_info = os.stat(private_stage, follow_symlinks=False)
        if (path_info.st_dev, path_info.st_ino) != (stage_info.st_dev, stage_info.st_ino):
            raise DiskSnapshotError("Stage path changed")
        entries = os.listdir(stage_fd)
        if entries:
            raise DiskSnapshotError("Stage is not fresh and empty")
        _checkpoint(deadline_monotonic, _control_check)
        volume = os.statvfs(private_stage)
        free_bytes = volume.f_bavail * volume.f_frsize
        if free_bytes < limits.max_stage_bytes + limits.min_free_bytes:
            raise DiskSnapshotError("Insufficient free space for declared stage limit")
        page_size = _source_pragma(source, "page_size")
        page_count = _source_pragma(source, "page_count")
        if page_size <= 0 or page_count <= 0 or page_count * page_size > limits.max_core_db_bytes:
            raise DiskSnapshotError("Source exceeds Core database size limit")

        file_fd = os.open(
            _OUTPUT,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=stage_fd,
        )
        try:
            created_info = os.fstat(file_fd)
            _check_output_role(file_fd, private_stage / _OUTPUT, created_info)
        finally:
            os.close(file_fd)
        output = private_stage / _OUTPUT

        destination: sqlite3.Connection | None = None
        try:
            _checkpoint(deadline_monotonic, _control_check)
            destination = sqlite3.connect(output, isolation_level=None, timeout=0)
            destination.execute("PRAGMA busy_timeout=0")

            def progress(_status: int, remaining: int, total: int) -> None:
                _checkpoint(deadline_monotonic, _control_check)
                if total < 0 or remaining < 0 or total * page_size > limits.max_core_db_bytes:
                    raise DiskSnapshotError("Backup exceeded Core database size limit")
                _check_stage_bytes(stage_fd, limits.max_stage_bytes)

            source.backup(
                destination, pages=limits.backup_pages_per_step, progress=progress, sleep=0.0
            )
        finally:
            if destination is not None:
                destination.close()

        _checkpoint(deadline_monotonic, _control_check)
        finalizer = sqlite3.connect(output, isolation_level=None, timeout=0)
        try:
            finalizer.execute("PRAGMA busy_timeout=0")
            mode = finalizer.execute("PRAGMA journal_mode").fetchone()[0].lower()
            if mode == "wal":
                checkpoint = finalizer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint != (0, 0, 0):
                    raise DiskSnapshotError("Destination WAL checkpoint did not complete")
            changed = finalizer.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower()
            if changed != "delete":
                raise DiskSnapshotError("Destination is not in DELETE journal mode")
        finally:
            finalizer.close()

        _checkpoint(deadline_monotonic, _control_check)
        _check_stage_bytes(stage_fd, limits.max_stage_bytes)
        _sidecars_absent(output)
        closed_info = _closed_output_info(output, created_info, limits.max_core_db_bytes)
        if closed_info.st_size > limits.max_stage_bytes:
            raise DiskSnapshotError("Snapshot exceeds stage size limit")
        before_hash = _hash_closed_file(output, closed_info, deadline_monotonic, _control_check)
        _checkpoint(deadline_monotonic, _control_check)
        final_stage = os.fstat(stage_fd)
        if final_stage.st_uid != os.getuid() or stat.S_IMODE(final_stage.st_mode) != 0o700:
            raise DiskSnapshotError("Stage must be owner-only 0700")
        _check_private_acl(stage_fd, private_stage)
        final_path = os.stat(private_stage, follow_symlinks=False)
        if (final_stage.st_dev, final_stage.st_ino) != (stage_info.st_dev, stage_info.st_ino):
            raise DiskSnapshotError("Stage path changed during snapshot")
        if (final_path.st_dev, final_path.st_ino) != (final_stage.st_dev, final_stage.st_ino):
            raise DiskSnapshotError("Stage path changed during snapshot")
        return StagedDiskSnapshot(
            output=str(output),
            byte_length=closed_info.st_size,
            sha256=before_hash,
            page_count=page_count,
            stage_dev=stage_info.st_dev,
            stage_ino=stage_info.st_ino,
            output_dev=created_info.st_dev,
            output_ino=created_info.st_ino,
        )
    finally:
        os.close(stage_fd)


def verify_staged_disk_snapshot(
    staged: StagedDiskSnapshot,
    *,
    private_stage: Path,
    limits: DiskSnapshotLimits,
    deadline_monotonic: float,
    _direct_reader: bool = False,
    _control_check: Callable[[], None] | None = None,
) -> DiskSnapshotReceipt:
    """Independently read back a staged file after its worker has been reaped.

    The descriptor is untrusted. The parent supplies its own expected private
    stage; descriptor paths are compared before opening, never followed as
    authority. A verified receipt is returned only after readback and sync.
    """
    if not isinstance(staged, StagedDiskSnapshot):
        raise DiskSnapshotError("Staged disk snapshot evidence is required")
    if not isinstance(private_stage, Path) or not private_stage.is_absolute():
        raise DiskSnapshotError("A private stage path is required")
    output = private_stage / _OUTPUT
    if staged.output != str(output):
        raise DiskSnapshotError("Staged disk snapshot output does not match expected stage")
    if not isinstance(limits, DiskSnapshotLimits):
        raise DiskSnapshotError("Disk snapshot limits are required")
    limits.validate()
    if type(deadline_monotonic) is not float or not (
        time.monotonic() < deadline_monotonic < float("inf")
    ):
        raise DiskSnapshotError("A finite future deadline is required")
    if (
        type(staged.byte_length) is not int
        or staged.byte_length <= 0
        or staged.byte_length > limits.max_core_db_bytes
        or staged.byte_length > limits.max_stage_bytes
        or type(staged.page_count) is not int
        or staged.page_count <= 0
        or type(staged.sha256) is not str
        or len(staged.sha256) != 64
        or any(char not in "0123456789abcdef" for char in staged.sha256)
        or any(
            type(value) is not int or value < 0
            for value in (
                staged.stage_dev,
                staged.stage_ino,
                staged.output_dev,
                staged.output_ino,
            )
        )
    ):
        raise DiskSnapshotError("Staged disk snapshot evidence is invalid")

    try:
        stage_fd = os.open(private_stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise DiskSnapshotError("Expected private snapshot stage is unavailable") from exc
    try:
        stage_info = os.fstat(stage_fd)
        if stage_info.st_uid != os.getuid() or stat.S_IMODE(stage_info.st_mode) != 0o700:
            raise DiskSnapshotError("Stage must be owner-only 0700")
        if (stage_info.st_dev, stage_info.st_ino) != (staged.stage_dev, staged.stage_ino):
            raise DiskSnapshotError("Staged disk snapshot stage identity changed")
        _check_private_acl(stage_fd, private_stage)
        path_info = os.stat(private_stage, follow_symlinks=False)
        if (path_info.st_dev, path_info.st_ino) != (stage_info.st_dev, stage_info.st_ino):
            raise DiskSnapshotError("Stage path changed")
        _checkpoint(deadline_monotonic, _control_check)
        _check_stage_bytes(stage_fd, limits.max_stage_bytes)
        _sidecars_absent(output)
        try:
            expected = os.stat(output, follow_symlinks=False)
        except OSError as exc:
            raise DiskSnapshotError("Staged disk snapshot output is unavailable") from exc
        if (expected.st_dev, expected.st_ino) != (staged.output_dev, staged.output_ino):
            raise DiskSnapshotError("Staged disk snapshot output identity changed")
        closed_info = _closed_output_info(output, expected, limits.max_core_db_bytes)
        if closed_info.st_size != staged.byte_length:
            raise DiskSnapshotError("Staged disk snapshot size changed")
        before_hash = _hash_closed_file(output, closed_info, deadline_monotonic, _control_check)
        if before_hash != staged.sha256:
            raise DiskSnapshotError("Staged disk snapshot content changed")
        _checkpoint(deadline_monotonic, _control_check)
        if _direct_reader:
            try:
                readback = _direct_readback(output, deadline_monotonic, _control_check)
            except (OSError, sqlite3.Error) as exc:
                raise DiskSnapshotError("Fresh-process snapshot readback failed") from exc
        else:
            try:
                run = subprocess.run(
                    [sys.executable, "-I", "-c", _READBACK, str(output)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=max(0.001, deadline_monotonic - time.monotonic()),
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise DiskSnapshotError("Fresh-process snapshot readback failed") from exc
            if len(run.stdout) > 4096 or run.stderr:
                raise DiskSnapshotError("Fresh-process readback was malformed")
            try:
                readback = json.loads(run.stdout)
            except (ValueError, TypeError) as exc:
                raise DiskSnapshotError("Fresh-process readback was malformed") from exc
        if not isinstance(readback, dict) or (
            readback.get("mode") != "delete"
            or readback.get("integrity") != [["ok"]]
            or readback.get("fk_problem") is not None
            or type(readback.get("schema_count")) is not int
            or type(readback.get("page_count")) is not int
            or readback["page_count"] != staged.page_count
        ):
            raise DiskSnapshotError("Fresh-process snapshot readback failed verification")
        _checkpoint(deadline_monotonic, _control_check)
        _check_stage_bytes(stage_fd, limits.max_stage_bytes)
        _sidecars_absent(output)
        after_info = _closed_output_info(output, expected, limits.max_core_db_bytes)
        after_hash = _hash_closed_file(output, after_info, deadline_monotonic, _control_check)
        if staged.sha256 != after_hash or staged.byte_length != after_info.st_size:
            raise DiskSnapshotError("Snapshot changed during independent readback")
        sync_fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            sync_info = _check_output_role(sync_fd, output, expected)
            if sync_info.st_size != after_info.st_size:
                raise DiskSnapshotError("Disk snapshot identity changed before sync")
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        os.fsync(stage_fd)
        final_stage = os.fstat(stage_fd)
        if final_stage.st_uid != os.getuid() or stat.S_IMODE(final_stage.st_mode) != 0o700:
            raise DiskSnapshotError("Stage must be owner-only 0700")
        _check_private_acl(stage_fd, private_stage)
        final_path = os.stat(private_stage, follow_symlinks=False)
        if (final_stage.st_dev, final_stage.st_ino) != (staged.stage_dev, staged.stage_ino):
            raise DiskSnapshotError("Stage path changed during snapshot")
        if (final_path.st_dev, final_path.st_ino) != (final_stage.st_dev, final_stage.st_ino):
            raise DiskSnapshotError("Stage path changed during snapshot")
        _closed_output_info(output, expected, limits.max_core_db_bytes)
        _checkpoint(deadline_monotonic, _control_check)
        return DiskSnapshotReceipt(
            output=output,
            byte_length=after_info.st_size,
            sha256=after_hash,
            page_count=readback["page_count"],
            schema_object_count=readback["schema_count"],
            journal_mode="delete",
        )
    finally:
        os.close(stage_fd)


def create_disk_snapshot(
    source: sqlite3.Connection,
    *,
    private_stage: Path,
    limits: DiskSnapshotLimits,
    deadline_monotonic: float,
) -> DiskSnapshotReceipt:
    """Preserve the existing one-call verified component snapshot API."""
    staged = stage_disk_snapshot(
        source,
        private_stage=private_stage,
        limits=limits,
        deadline_monotonic=deadline_monotonic,
    )
    return verify_staged_disk_snapshot(
        staged,
        private_stage=private_stage,
        limits=limits,
        deadline_monotonic=deadline_monotonic,
    )
