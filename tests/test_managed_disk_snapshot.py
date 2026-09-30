"""Synthetic acceptance for the component-only managed disk snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

import pytest

import finance_core.managed_disk_snapshot as disk_snapshot
from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.managed_disk_snapshot import (
    DiskSnapshotError,
    DiskSnapshotLimits,
    DiskSnapshotReceipt,
    StagedDiskSnapshot,
    create_disk_snapshot,
    stage_disk_snapshot,
    verify_staged_disk_snapshot,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    supersede_receipt_item_allocation_facts,
)
from finance_core.profile_paths import ProfilePathError
from finance_core.receipt_finalization import (
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
)
from finance_core.reconciliation.migrations import migration_ledger_rows
from tests.test_receipt_b5_staging_e2e_v1 import _run_b5_pipeline
from tests.test_receipt_item_allocation_facts_supersession_v1 import (
    correction_command,
    replacement_items,
)

_LIMITS = DiskSnapshotLimits(
    max_core_db_bytes=1_048_576,
    max_stage_bytes=2_097_152,
    min_free_bytes=1,
    backup_pages_per_step=8,
)
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class _ProgressObservedConnection(sqlite3.Connection):
    active_backup_remaining: int | None = None

    def backup(
        self,
        target: sqlite3.Connection,
        *,
        pages: int = -1,
        progress: Callable[[int, int, int], object] | None = None,
        name: str = "main",
        sleep: float = 0.250,
    ) -> None:
        def observe(status: int, remaining: int, total: int) -> None:
            self.active_backup_remaining = remaining
            try:
                if progress is not None:
                    progress(status, remaining, total)
            finally:
                self.active_backup_remaining = None

        super().backup(target, pages=pages, progress=observe, name=name, sleep=sleep)


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


def _table_contents(conn: sqlite3.Connection) -> dict[str, tuple[tuple[object, ...], ...]]:
    table_names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    contents: dict[str, tuple[tuple[object, ...], ...]] = {}
    for table_name in table_names:
        quoted_name = '"' + table_name.replace('"', '""') + '"'
        rows = [tuple(row) for row in conn.execute(f"SELECT * FROM {quoted_name}").fetchall()]
        contents[table_name] = tuple(sorted(rows, key=repr))
    return contents


def _schema_objects(conn: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
    )


def _add_multistep_backup_payload(
    source: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    source.execute("CREATE TABLE backup_payload (id INTEGER PRIMARY KEY, body BLOB NOT NULL)")
    source.executemany(
        "INSERT INTO backup_payload (id, body) VALUES (?, ?)",
        ((row_id, bytes([row_id % 251]) * 4096) for row_id in range(1, 49)),
    )
    source.commit()
    assert source.execute("PRAGMA page_count").fetchone()[0] > 1
    return _table_contents(source)


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


def test_stage_defers_child_readback_and_verify_can_run_after_source_close(
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = wal_source
    wal_path = Path(f"{database}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 0
    source_before = _table_contents(source)
    source_identity = (database.stat().st_dev, database.stat().st_ino)
    wal_bytes = wal_path.read_bytes()
    stage = _private_stage(tmp_path)

    def reject_child_readback(*_args: object, **_kwargs: object) -> None:
        pytest.fail("stage_disk_snapshot must not start a readback child")

    with monkeypatch.context() as stage_only:
        stage_only.setattr(disk_snapshot.subprocess, "run", reject_child_readback)
        staged = stage_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert isinstance(staged, StagedDiskSnapshot)
    assert not isinstance(staged, DiskSnapshotReceipt)
    assert not hasattr(staged, "schema_object_count")
    assert staged.output == str(stage / "core.sqlite")
    output = Path(staged.output)
    output_info = output.stat()
    stage_info = stage.stat()
    assert (staged.output_dev, staged.output_ino) == (output_info.st_dev, output_info.st_ino)
    assert (staged.stage_dev, staged.stage_ino) == (stage_info.st_dev, stage_info.st_ino)
    assert staged.byte_length == output_info.st_size
    assert staged.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()

    # Staging copies committed WAL facts without checkpointing or otherwise
    # changing the caller-owned source database and WAL.
    assert _table_contents(source) == source_before
    assert (database.stat().st_dev, database.stat().st_ino) == source_identity
    assert wal_path.read_bytes() == wal_bytes

    # Verification is deliberately independent of the source connection.
    transported = StagedDiskSnapshot(**json.loads(json.dumps(asdict(staged))))
    assert transported == staged
    source.close()
    receipt = verify_staged_disk_snapshot(
        transported,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )

    assert isinstance(receipt, DiskSnapshotReceipt)
    assert receipt.output == output
    assert receipt.byte_length == staged.byte_length
    assert receipt.sha256 == staged.sha256
    assert receipt.journal_mode == "delete"
    assert (output.stat().st_dev, output.stat().st_ino) == (
        staged.output_dev,
        staged.output_ino,
    )
    assert not any(Path(f"{output}{suffix}").exists() for suffix in _SIDECAR_SUFFIXES)
    with closing(sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copied.execute("SELECT rowid, id, label FROM snapshot_rows").fetchall() == [
            (37, 37, "committed-in-wal")
        ]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replaced"])
def test_verify_rejects_missing_corrupt_or_replaced_staged_output(
    damage: Literal["missing", "corrupt", "replaced"],
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)
    staged = stage_disk_snapshot(
        source,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    output = Path(staged.output)
    original_bytes = output.read_bytes()
    original_identity = (output.stat().st_dev, output.stat().st_ino)

    if damage == "missing":
        output.unlink()
    elif damage == "corrupt":
        output.write_bytes(b"\x00" + original_bytes[1:])
    else:
        replacement = tmp_path / "replacement.sqlite"
        replacement.write_bytes(original_bytes)
        replacement.chmod(0o600)
        os.replace(replacement, output)
        assert (output.stat().st_dev, output.stat().st_ino) != original_identity

    with pytest.raises(DiskSnapshotError):
        verify_staged_disk_snapshot(
            staged,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    # Failed verification leaves the candidate exactly as the caller changed it.
    if damage == "missing":
        assert not output.exists()
        assert list(stage.iterdir()) == []
    elif damage == "corrupt":
        assert output.read_bytes() == b"\x00" + original_bytes[1:]
        assert (output.stat().st_dev, output.stat().st_ino) == original_identity
    else:
        assert output.read_bytes() == original_bytes
        assert (output.stat().st_dev, output.stat().st_ino) != original_identity
    assert _table_contents(source)["snapshot_rows"] == ((37, "committed-in-wal"),)


def test_verify_refuses_and_preserves_sidecar_added_after_staging(
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)
    staged = stage_disk_snapshot(
        source,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    output = Path(staged.output)
    output_bytes = output.read_bytes()
    sidecar = Path(f"{output}-wal")
    sidecar_bytes = b"synthetic unexpected WAL sidecar"
    sidecar.write_bytes(sidecar_bytes)

    with pytest.raises(DiskSnapshotError, match="sidecar"):
        verify_staged_disk_snapshot(
            staged,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert output.read_bytes() == output_bytes
    assert sidecar.read_bytes() == sidecar_bytes


def test_verify_rejects_replaced_stage_directory_and_preserves_both_candidates(
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path, name="stage-to-replace")
    staged = stage_disk_snapshot(
        source,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    output_bytes = Path(staged.output).read_bytes()
    displaced_stage = tmp_path / "displaced-stage"
    stage.rename(displaced_stage)
    replacement_stage = _private_stage(tmp_path, name="stage-to-replace")
    replacement_output = replacement_stage / "core.sqlite"
    replacement_output.write_bytes(output_bytes)
    replacement_output.chmod(0o600)
    assert (replacement_stage.stat().st_dev, replacement_stage.stat().st_ino) != (
        staged.stage_dev,
        staged.stage_ino,
    )

    with pytest.raises(DiskSnapshotError, match="stage identity changed"):
        verify_staged_disk_snapshot(
            staged,
            private_stage=replacement_stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert (displaced_stage / "core.sqlite").read_bytes() == output_bytes
    assert replacement_output.read_bytes() == output_bytes


def test_verify_binds_output_to_caller_supplied_private_stage_before_opening_it(
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path, name="owned-stage")
    staged = stage_disk_snapshot(
        source,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    output = Path(staged.output)
    output_bytes = output.read_bytes()
    wrong_stage = _private_stage(tmp_path, name="untrusted-stage")
    output_opens: list[object] = []
    actual_open = os.open

    def observe_output_open(path: object, *args: object, **kwargs: object) -> int:
        if os.fspath(path) == staged.output:
            output_opens.append(path)
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(disk_snapshot.os, "open", observe_output_open)
    with pytest.raises(DiskSnapshotError):
        verify_staged_disk_snapshot(
            staged,
            private_stage=wrong_stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert output_opens == []
    assert output.read_bytes() == output_bytes


def test_verify_rejects_untrusted_page_count_in_staged_descriptor(
    wal_source: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)
    staged = stage_disk_snapshot(
        source,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    forged = replace(staged, page_count=staged.page_count + 1)

    with pytest.raises(DiskSnapshotError, match="readback failed verification"):
        verify_staged_disk_snapshot(
            forged,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    receipt = verify_staged_disk_snapshot(
        staged,
        private_stage=stage,
        limits=_LIMITS,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    assert receipt.page_count == staged.page_count


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


def test_deadline_during_incomplete_backup_preserves_stage_placeholder_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_source = sqlite3.connect(tmp_path / "deadline-fixture.sqlite")
    source = sqlite3.connect(
        tmp_path / "deadline-source.sqlite", factory=_ProgressObservedConnection
    )
    try:
        fixture_source.execute(
            "CREATE TABLE snapshot_rows (id INTEGER PRIMARY KEY, label TEXT NOT NULL)"
        )
        fixture_source.execute("INSERT INTO snapshot_rows (id, label) VALUES (37, 'synthetic')")
        fixture_source.commit()
        fixture_source.backup(source)
        source_before = _add_multistep_backup_payload(source)
        stage = _private_stage(tmp_path, name="deadline-partial")
        output = stage / "core.sqlite"
        original_check_deadline = disk_snapshot._check_deadline
        observed_remaining: int | None = None

        def expire_from_progress(deadline: float) -> None:
            nonlocal observed_remaining
            remaining = source.active_backup_remaining
            if remaining is not None and remaining > 0:
                observed_remaining = remaining
                raise DiskSnapshotError("Disk snapshot deadline expired")
            original_check_deadline(deadline)

        monkeypatch.setattr(disk_snapshot, "_check_deadline", expire_from_progress)
        limits = DiskSnapshotLimits(
            max_core_db_bytes=1_048_576,
            max_stage_bytes=2_097_152,
            min_free_bytes=1,
            backup_pages_per_step=1,
        )
        with pytest.raises(DiskSnapshotError, match="deadline expired"):
            create_disk_snapshot(
                source,
                private_stage=stage,
                limits=limits,
                deadline_monotonic=time.monotonic() + 30.0,
            )

        assert observed_remaining is not None and observed_remaining > 0
        failed_stage_bytes = output.read_bytes()
        assert output.is_file()
        # SQLite rolls the incomplete backup back when the destination closes;
        # preserve the empty placeholder rather than silently resetting stage.
        assert failed_stage_bytes == b""
        assert list(stage.iterdir()) == [output]
        assert _table_contents(source) == source_before
        with pytest.raises(DiskSnapshotError, match="Stage is not fresh and empty"):
            create_disk_snapshot(
                source,
                private_stage=stage,
                limits=limits,
                deadline_monotonic=time.monotonic() + 30.0,
            )
        assert output.read_bytes() == failed_stage_bytes
    finally:
        source.close()
        fixture_source.close()


def test_output_fsync_failure_preserves_completed_stage_and_source(
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _database = wal_source
    source_before = _add_multistep_backup_payload(source)
    stage = _private_stage(tmp_path, name="sync-failure")
    output = stage / "core.sqlite"
    actual_run = subprocess.run
    readback_completed = False

    def record_readback(
        command: list[str],
        *,
        capture_output: Literal[True],
        text: Literal[True],
        check: Literal[True],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal readback_completed
        completed = actual_run(
            command,
            capture_output=capture_output,
            text=text,
            check=check,
            timeout=timeout,
        )
        readback_completed = True
        return completed

    actual_fsync = os.fsync

    def fail_output_sync(fd: int) -> None:
        assert readback_completed
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("synthetic output fsync failure")
        actual_fsync(fd)

    monkeypatch.setattr(disk_snapshot.subprocess, "run", record_readback)
    monkeypatch.setattr(disk_snapshot.os, "fsync", fail_output_sync)

    with pytest.raises(OSError, match="synthetic output fsync failure"):
        create_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert readback_completed
    completed_bytes = output.read_bytes()
    assert completed_bytes
    assert _table_contents(source) == source_before
    with closing(sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _table_contents(copied) == source_before
    with pytest.raises(DiskSnapshotError, match="Stage is not fresh and empty"):
        create_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )
    assert output.read_bytes() == completed_bytes


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


def test_synthetic_darwin_allow_acl_on_stage_is_refused_before_output(
    wal_source: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)

    def synthetic_allow_acl(_fd: int, path: Path) -> None:
        raise ProfilePathError(f"ACL grants access to profile path: {path}")

    monkeypatch.setattr(disk_snapshot, "_reject_acl_grants", synthetic_allow_acl)
    with pytest.raises(DiskSnapshotError, match="unsafe ACL"):
        create_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert list(stage.iterdir()) == []
    assert not (stage / "core.sqlite").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL only")
def test_darwin_stage_allow_acl_is_refused_before_output_even_with_mode_0700(
    wal_source: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    source, _database = wal_source
    stage = _private_stage(tmp_path)
    initial_mode = stat.S_IMODE(stage.stat().st_mode)

    subprocess.run(["chmod", "+a", "everyone allow read", str(stage)], check=True)

    assert initial_mode == 0o700
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    with pytest.raises(DiskSnapshotError, match="unsafe ACL"):
        create_disk_snapshot(
            source,
            private_stage=stage,
            limits=_LIMITS,
            deadline_monotonic=time.monotonic() + 30.0,
        )

    assert list(stage.iterdir()) == []
    assert not (stage / "core.sqlite").exists()


def test_synthetic_allow_acl_on_output_role_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "synthetic-output.sqlite"
    output.write_bytes(b"synthetic output bytes")
    output.chmod(0o600)
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
    expected = os.fstat(fd)

    def synthetic_allow_acl(_fd: int, path: Path) -> None:
        raise ProfilePathError(f"ACL grants access to profile path: {path}")

    monkeypatch.setattr(disk_snapshot, "_reject_acl_grants", synthetic_allow_acl)
    try:
        with pytest.raises(DiskSnapshotError, match="unsafe ACL"):
            disk_snapshot._check_output_role(fd, output, expected)
    finally:
        os.close(fd)

    assert output.read_bytes() == b"synthetic output bytes"


def test_complete_output_role_check_rejects_hardlink_alias(tmp_path: Path) -> None:
    output = tmp_path / "core.sqlite"
    alias = tmp_path / "core-alias.sqlite"
    output.write_bytes(b"synthetic standalone database")
    output.chmod(0o600)
    os.link(output, alias)
    fd = os.open(output, os.O_RDONLY | os.O_NOFOLLOW)
    expected = os.fstat(fd)
    try:
        assert expected.st_nlink == 2
        with pytest.raises(DiskSnapshotError, match="private regular file"):
            disk_snapshot._check_output_role(fd, output, expected)
    finally:
        os.close(fd)


def test_migrated_core_snapshot_preserves_financial_source_corrections_and_audit(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    source = migrated_temp_db_connection

    # Leave a committed rowid gap in the actual Core source table while keeping
    # its surviving synthetic source/evidence rows available for comparison.
    scratch_ids: list[int] = []
    with source:
        for suffix in ("before", "hole", "after"):
            raw = create_raw_intake_record(
                source,
                f"C06 synthetic rowid fixture {suffix}",
                source_type="manual_entry",
                source_channel="manual",
                received_at="2026-09-01T00:00:00+00:00",
                public_id=f"raw_c06_rowid_{suffix}",
            )
            scratch_ids.append(int(raw["id"]))
        source.execute(
            "DELETE FROM raw_intake_evidence WHERE raw_intake_record_id = ?",
            (scratch_ids[1],),
        )
        source.execute("DELETE FROM raw_intake_records WHERE id = ?", (scratch_ids[1],))

    pipeline = _run_b5_pipeline(source, tmp_path, "c06_snapshot")
    v2 = supersede_receipt_item_allocation_facts(
        source,
        correction_command(pipeline.suffix, pipeline.ctx, pipeline.iaf_result),
    )
    v3 = supersede_receipt_item_allocation_facts(
        source,
        correction_command(
            f"{pipeline.suffix}_v3",
            pipeline.ctx,
            v2,
            items=replacement_items("C06 reviewed correction"),
        ),
    )
    source.commit()

    fact_sets = source.execute(
        "SELECT version, fact_set_public_id, supersedes_fact_set_public_id, "
        "superseded_by_fact_set_public_id FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? ORDER BY version",
        (pipeline.ctx.receipt_id,),
    ).fetchall()
    assert [row["version"] for row in fact_sets] == [1, 2, 3]
    assert fact_sets[0]["superseded_by_fact_set_public_id"] == v2.fact_set_public_id
    assert fact_sets[1]["supersedes_fact_set_public_id"] == pipeline.iaf_result.fact_set_public_id
    assert fact_sets[1]["superseded_by_fact_set_public_id"] == v3.fact_set_public_id
    assert fact_sets[2]["supersedes_fact_set_public_id"] == v2.fact_set_public_id
    assert fact_sets[2]["superseded_by_fact_set_public_id"] is None

    prepared = prepare_receipt_calculation(source, pipeline.conversion.receipt_public_id)
    authorization = authorize_receipt_finalization(source, prepared, actor_id="owner")
    finalized = finalize_prepared_receipt(source, authorization)
    assert finalized.status == "finalized"

    authorization_row = source.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()
    assert authorization_row is not None
    assert authorization_row["authorization_state"] == "consumed"
    assert source.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert source.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0] > 0
    assert source.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
    assert source.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0] > 0
    assert (
        source.execute("SELECT COUNT(*) FROM authoritative_calculation_snapshots").fetchone()[0] > 0
    )
    assert source.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] > 0
    assert source.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1

    source_rowids = [
        int(row[0])
        for row in source.execute("SELECT id FROM raw_intake_records ORDER BY id").fetchall()
    ]
    assert scratch_ids[0] in source_rowids
    assert scratch_ids[1] not in source_rowids
    assert scratch_ids[2] in source_rowids
    assert any(right - left > 1 for left, right in zip(source_rowids, source_rowids[1:]))

    aggregate_keys = source.execute(
        "SELECT DISTINCT aggregate_type, aggregate_public_id FROM financial_audit_events"
    ).fetchall()
    assert aggregate_keys
    for aggregate in aggregate_keys:
        verification = verify_financial_audit_chain(
            source,
            aggregate_type=str(aggregate["aggregate_type"]),
            aggregate_public_id=str(aggregate["aggregate_public_id"]),
        )
        assert verification.valid, verification.reason

    source_schema = _schema_objects(source)
    source_ledger = migration_ledger_rows(source)
    source_contents = _table_contents(source)
    stage = _private_stage(tmp_path, name="c06-financial-snapshot")
    receipt = create_disk_snapshot(
        source,
        private_stage=stage,
        limits=DiskSnapshotLimits(
            max_core_db_bytes=16 * 1024 * 1024,
            max_stage_bytes=32 * 1024 * 1024,
            min_free_bytes=1,
            backup_pages_per_step=32,
        ),
        deadline_monotonic=time.monotonic() + 60.0,
    )

    with closing(sqlite3.connect(receipt.output.as_uri() + "?mode=ro", uri=True)) as copied:
        copied.row_factory = sqlite3.Row
        assert copied.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migration_ledger_rows(copied) == source_ledger
        assert _schema_objects(copied) == source_schema
        assert _table_contents(copied) == source_contents
        copied_rowids = [
            int(row[0])
            for row in copied.execute("SELECT id FROM raw_intake_records ORDER BY id").fetchall()
        ]
        assert copied_rowids == source_rowids

        for aggregate in aggregate_keys:
            verification = verify_financial_audit_chain(
                copied,
                aggregate_type=str(aggregate["aggregate_type"]),
                aggregate_public_id=str(aggregate["aggregate_public_id"]),
            )
            assert verification.valid, verification.reason
