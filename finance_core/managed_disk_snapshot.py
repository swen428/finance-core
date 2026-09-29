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
from dataclasses import dataclass
from pathlib import Path

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
    print(json.dumps({"mode": mode, "integrity": integrity, "fk_problem": fk_problem,
                      "schema_count": schema_count}))
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


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DiskSnapshotError("Disk snapshot deadline expired")


def _sidecars_absent(output: Path) -> None:
    if any(os.path.lexists(f"{output}{suffix}") for suffix in _SIDECARS):
        raise DiskSnapshotError("Disk snapshot has a SQLite sidecar")


def _hash_closed_file(output: Path, expected: os.stat_result, deadline: float) -> str:
    digest = hashlib.sha256()
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
        ):
            raise DiskSnapshotError("Disk snapshot identity changed")
        while chunk := os.read(fd, 1024 * 1024):
            _check_deadline(deadline)
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _closed_output_info(output: Path, expected: os.stat_result, limit: int) -> os.stat_result:
    info = os.stat(output, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600:
        raise DiskSnapshotError("Disk snapshot is not a private regular file")
    if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        raise DiskSnapshotError("Disk snapshot identity changed")
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


def create_disk_snapshot(
    source: sqlite3.Connection,
    *,
    private_stage: Path,
    limits: DiskSnapshotLimits,
    deadline_monotonic: float,
) -> DiskSnapshotReceipt:
    """Backup one frozen source into fixed ``core.sqlite`` and verify the file.

    The trusted caller supplies a live source connection and a fresh 0700 stage
    under validated custody, holds the exclusive managed cut throughout this
    call, and closes the source itself before releasing that cut. A connection or
    directory path alone cannot establish those preconditions. No source path is
    accepted, and the source is never normalized or modified here.
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
        if stage_info.st_uid != os.getuid() or stage_info.st_mode & 0o777 != 0o700:
            raise DiskSnapshotError("Stage must be owner-only 0700")
        path_info = os.stat(private_stage, follow_symlinks=False)
        if (path_info.st_dev, path_info.st_ino) != (stage_info.st_dev, stage_info.st_ino):
            raise DiskSnapshotError("Stage path changed")
        entries = os.listdir(stage_fd)
        if entries:
            raise DiskSnapshotError("Stage is not fresh and empty")
        _check_deadline(deadline_monotonic)
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
        finally:
            os.close(file_fd)
        output = private_stage / _OUTPUT

        destination: sqlite3.Connection | None = None
        try:
            _check_deadline(deadline_monotonic)
            destination = sqlite3.connect(output, isolation_level=None, timeout=0)
            destination.execute("PRAGMA busy_timeout=0")

            def progress(_status: int, remaining: int, total: int) -> None:
                _check_deadline(deadline_monotonic)
                if total < 0 or remaining < 0 or total * page_size > limits.max_core_db_bytes:
                    raise DiskSnapshotError("Backup exceeded Core database size limit")
                _check_stage_bytes(stage_fd, limits.max_stage_bytes)

            source.backup(
                destination, pages=limits.backup_pages_per_step, progress=progress, sleep=0.0
            )
        finally:
            if destination is not None:
                destination.close()

        _check_deadline(deadline_monotonic)
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

        _check_deadline(deadline_monotonic)
        _check_stage_bytes(stage_fd, limits.max_stage_bytes)
        _sidecars_absent(output)
        closed_info = _closed_output_info(output, created_info, limits.max_core_db_bytes)
        if closed_info.st_size > limits.max_stage_bytes:
            raise DiskSnapshotError("Snapshot exceeds stage size limit")
        before_hash = _hash_closed_file(output, closed_info, deadline_monotonic)
        _check_deadline(deadline_monotonic)
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
        if (
            readback.get("mode") != "delete"
            or readback.get("integrity") != [["ok"]]
            or readback.get("fk_problem") is not None
            or type(readback.get("schema_count")) is not int
        ):
            raise DiskSnapshotError("Fresh-process snapshot readback failed verification")
        _check_deadline(deadline_monotonic)
        _check_stage_bytes(stage_fd, limits.max_stage_bytes)
        _sidecars_absent(output)
        after_info = _closed_output_info(output, created_info, limits.max_core_db_bytes)
        after_hash = _hash_closed_file(output, after_info, deadline_monotonic)
        if before_hash != after_hash or closed_info.st_size != after_info.st_size:
            raise DiskSnapshotError("Snapshot changed during independent readback")
        sync_fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            sync_info = os.fstat(sync_fd)
            if (sync_info.st_dev, sync_info.st_ino, sync_info.st_size) != (
                created_info.st_dev,
                created_info.st_ino,
                after_info.st_size,
            ):
                raise DiskSnapshotError("Disk snapshot identity changed before sync")
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        os.fsync(stage_fd)
        final_stage = os.stat(private_stage, follow_symlinks=False)
        if (final_stage.st_dev, final_stage.st_ino) != (stage_info.st_dev, stage_info.st_ino):
            raise DiskSnapshotError("Stage path changed during snapshot")
        _check_deadline(deadline_monotonic)
        return DiskSnapshotReceipt(
            output=output,
            byte_length=after_info.st_size,
            sha256=after_hash,
            page_count=page_count,
            schema_object_count=readback["schema_count"],
            journal_mode="delete",
        )
    finally:
        os.close(stage_fd)
