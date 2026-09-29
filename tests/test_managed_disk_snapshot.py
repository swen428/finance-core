"""Synthetic acceptance for the component-only managed disk snapshot."""

from __future__ import annotations

import hashlib
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from finance_core.managed_disk_snapshot import (
    DiskSnapshotError,
    DiskSnapshotLimits,
    create_disk_snapshot,
)

_LIMITS = DiskSnapshotLimits(
    max_core_db_bytes=1_048_576,
    max_stage_bytes=2_097_152,
    min_free_bytes=1,
    backup_pages_per_step=8,
)
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@pytest.fixture()
def wal_source(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    database = tmp_path / "synthetic-source.sqlite"
    source = sqlite3.connect(database)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("PRAGMA wal_autocheckpoint=0")
    source.execute("CREATE TABLE snapshot_rows (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
    source.executemany(
        "INSERT INTO snapshot_rows (id, label) VALUES (?, ?)",
        ((5, "deleted-row"), (37, "committed-in-wal")),
    )
    source.execute("DELETE FROM snapshot_rows WHERE id = 5")
    source.commit()
    try:
        yield source, database
    finally:
        source.close()


def _private_stage(root: Path, name: str = "stage") -> Path:
    stage = root / name
    stage.mkdir(mode=0o700)
    stage.chmod(0o700)
    return stage


def test_wal_snapshot_preserves_rowid_and_returns_closed_delete_file(
    wal_source: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    source, database = wal_source
    assert source.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    wal_path = Path(f"{database}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 0

    stage = _private_stage(tmp_path)
    receipt = create_disk_snapshot(
        source,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )

    assert receipt.output == stage / "core.sqlite"
    assert receipt.byte_length == receipt.output.stat().st_size
    assert receipt.sha256 == hashlib.sha256(receipt.output.read_bytes()).hexdigest()
    assert receipt.page_count > 0
    assert receipt.schema_object_count > 0
    assert receipt.journal_mode == "delete"
    assert stat.S_IMODE(receipt.output.stat().st_mode) == 0o600
    sidecars_before_readback = tuple(
        Path(f"{receipt.output}{suffix}").exists() for suffix in _SIDECAR_SUFFIXES
    )
    assert sidecars_before_readback == (False, False, False)

    uri = receipt.output.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as copied:
        assert copied.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copied.execute("SELECT rowid, id, label FROM snapshot_rows").fetchall() == [
            (37, 37, "committed-in-wal")
        ]
    assert hashlib.sha256(receipt.output.read_bytes()).hexdigest() == receipt.sha256
    assert (
        tuple(Path(f"{receipt.output}{suffix}").exists() for suffix in _SIDECAR_SUFFIXES)
        == sidecars_before_readback
    )

    # The snapshotter leaves source ownership and its WAL state with the caller.
    assert source.execute("SELECT rowid, label FROM snapshot_rows").fetchall() == [
        (37, "committed-in-wal")
    ]


def test_existing_stage_output_collision_is_refused_and_preserved(
    wal_source: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)
    output = stage / "core.sqlite"
    output.write_bytes(b"synthetic pre-existing evidence")

    with pytest.raises(DiskSnapshotError, match="Stage is not fresh and empty"):
        create_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert output.read_bytes() == b"synthetic pre-existing evidence"


def test_nonprivate_stage_permissions_are_refused_without_creating_output(
    wal_source: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)
    stage.chmod(0o755)

    with pytest.raises(DiskSnapshotError, match="owner-only 0700"):
        create_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert list(stage.iterdir()) == []


def test_expired_and_unbounded_deadlines_are_refused_before_stage_write(
    wal_source: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)

    for deadline in (0.0, float("inf"), float("nan")):
        with pytest.raises(DiskSnapshotError, match="finite future deadline"):
            create_disk_snapshot(
                source,
                private_stage=stage,
                limits=_LIMITS,
                deadline_monotonic=deadline,
            )
        assert list(stage.iterdir()) == []
