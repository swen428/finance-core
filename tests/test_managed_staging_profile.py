"""Managed staging lifecycle proof with disposable synthetic profiles only."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import finance_core.managed_staging_profile as managed_staging
import finance_core.profile_gate as profile_gate
import finance_core.profile_paths as profile_paths
import finance_core.staging_guard as staging_guard
from finance_core.profile_gate import (
    ProfileGateBusy,
    exclusive_cut,
    initialize_profile_gate,
)
from finance_core.profile_paths import (
    MANAGED_STAGING_FILENAME,
    ProfilePathError,
    validate_profile_paths,
)
from finance_core.staging_guard import (
    StagingDatabaseError,
    create_staging_database,
    open_staging_database,
)

_PROFILE_ID = "synthetic"
_RAW_INTAKE_INSERT = (
    "INSERT INTO raw_intake_records "
    "(public_id, source_type, source_channel, raw_input, received_at) "
    "VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z')"
)


def _blank_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_id: str = _PROFILE_ID
) -> tuple[Path, Path, object]:
    support = tmp_path / "Application Support"
    support.mkdir(mode=0o700, exist_ok=True)
    base = support / "Finance-Codex" / "profiles" / profile_id
    directories = (
        base.parent.parent,
        base.parent,
        base,
        base / "runtime",
        base / "runtime" / "database",
        base / "workspace",
        base / "workspace" / "database",
        base / "backups",
        base / "work",
        base / "restore",
    )
    for directory in directories:
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    marker = base / "profile.json"
    marker.write_text(
        json.dumps(
            {
                "profile_id": profile_id,
                "runtime_root": str(base / "runtime"),
                "workspace_root": str(base / "workspace"),
            }
        ),
        encoding="utf-8",
    )
    marker.chmod(0o600)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "runtime"))
    blank = validate_profile_paths(support, profile_id)
    initialize_profile_gate(blank)
    return support, base, blank


@pytest.fixture
def registered_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, object, object]:
    support, base, blank = _blank_profile(tmp_path, monkeypatch)
    managed = managed_staging.bootstrap_registered_staging(blank)
    try:
        yield support, base, blank, managed
    finally:
        managed.close()
        blank.close()


def _insert_raw_intake(conn: object, public_id: str, raw_input: str) -> None:
    conn.execute(_RAW_INTAKE_INSERT, (public_id, raw_input))


def test_bootstrap_commit_reopen_and_legacy_blank_guard(
    registered_profile: tuple[Path, Path, object, object],
) -> None:
    support, _base, _blank, managed = registered_profile

    with managed_staging._managed_staging_connection(managed, purpose="reopen") as conn:
        _insert_raw_intake(conn, "d4-synthetic-committed", "synthetic committed row")
        conn.commit()

    reopened = managed_staging.verify_registered_staging(support, _PROFILE_ID)
    try:
        with managed_staging._managed_staging_connection(reopened, purpose="reopen") as conn:
            row = conn.execute(
                "SELECT raw_input FROM raw_intake_records WHERE public_id=?",
                ("d4-synthetic-committed",),
            ).fetchone()
            assert row is not None and row[0] == "synthetic committed row"
    finally:
        reopened.close()

    # The original path witness keeps its old blank-only contract after P1.
    with pytest.raises(ProfilePathError, match="native SQLite admission"):
        validate_profile_paths(support, _PROFILE_ID)


def test_bootstrap_releases_its_shared_gate_before_final_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, _base, blank = _blank_profile(tmp_path, monkeypatch)
    real_verify = managed_staging.verify_registered_staging

    def verify_after_release(root: str | Path, profile_id: str) -> object:
        witness = profile_paths.validate_registered_staging_profile(root, profile_id)
        try:
            with exclusive_cut(witness, timeout_seconds=0):
                pass
        finally:
            witness.close()
        return real_verify(root, profile_id)

    monkeypatch.setattr(managed_staging, "verify_registered_staging", verify_after_release)
    managed = managed_staging.bootstrap_registered_staging(blank)
    try:
        assert managed.registration.exists()
    finally:
        managed.close()
        blank.close()


def test_legacy_reopen_factory_cannot_bypass_managed_profile_gate(
    registered_profile: tuple[Path, Path, object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    _support, _base, _blank, managed = registered_profile
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

    real_connect = staging_guard.sqlite3.connect
    opens: list[str] = []

    def tracked_connect(database: object, *args: object, **kwargs: object) -> object:
        opens.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(staging_guard.sqlite3, "connect", tracked_connect)
    main_before = managed.staging_database.read_bytes()
    with pytest.raises(StagingDatabaseError):
        open_staging_database(managed.staging_database, migration_paths=TEMP_DB_MIGRATION_PATHS)
    assert opens == []
    assert managed.staging_database.read_bytes() == main_before

    with managed_staging._managed_staging_connection(managed, purpose="reopen") as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("version", [True, 1.0], ids=["boolean-one", "float-one"])
def test_registration_version_requires_exact_integer_and_preserves_bytes(
    registered_profile: tuple[Path, Path, object, object],
    monkeypatch: pytest.MonkeyPatch,
    version: bool | float,
) -> None:
    support, _base, _blank, managed = registered_profile
    registration = managed.registration
    payload = json.loads(registration.read_text(encoding="utf-8"))
    payload["version"] = version
    invalid_bytes = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    registration.write_bytes(invalid_bytes)
    registration.chmod(0o600)

    connect_attempts: list[str] = []

    def tracked_connect(database: object, *args: object, **kwargs: object) -> object:
        connect_attempts.append(str(database))
        raise AssertionError("malformed registration reached SQLite")

    monkeypatch.setattr(staging_guard.sqlite3, "connect", tracked_connect)
    with pytest.raises(ProfilePathError):
        managed_staging.verify_registered_staging(support, _PROFILE_ID)

    assert connect_attempts == []
    assert registration.read_bytes() == invalid_bytes


@pytest.mark.parametrize(
    ("role", "fault"),
    [
        ("main", "mode"),
        ("main", "symlink"),
        ("main", "hardlink"),
        ("sidecar", "mode"),
        ("sidecar", "fifo"),
        ("sidecar", "hardlink"),
    ],
    ids=[
        "main-mode",
        "main-symlink",
        "main-hardlink",
        "sidecar-mode",
        "sidecar-fifo",
        "sidecar-hardlink",
    ],
)
def test_untrusted_managed_main_and_sidecar_roles_fail_before_sqlite_open(
    registered_profile: tuple[Path, Path, object, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    fault: str,
) -> None:
    support, _base, _blank, managed = registered_profile
    main = managed.staging_database
    main_bytes = main.read_bytes()
    role_path = main if role == "main" else Path(f"{main}-wal")
    if role == "sidecar":
        role_path.write_bytes(b"synthetic enrolled sidecar")
        role_path.chmod(0o600)
    role_bytes = role_path.read_bytes() if role_path.is_file() else b""
    preserved_path: Path | None = None
    linked_path: Path | None = None

    if fault == "mode":
        role_path.chmod(0o644)
    elif fault == "symlink":
        preserved_path = tmp_path / "preserved-managed-main.sqlite"
        role_path.rename(preserved_path)
        role_path.symlink_to(preserved_path)
    elif fault == "fifo":
        role_path.unlink()
        os.mkfifo(role_path, 0o600)
    elif fault == "hardlink":
        linked_path = tmp_path / f"preserved-{role}-hardlink"
        os.link(role_path, linked_path)
    else:
        raise AssertionError(f"unknown role fault: {fault}")

    connect_attempts: list[str] = []

    def tracked_connect(database: object, *args: object, **kwargs: object) -> object:
        connect_attempts.append(str(database))
        raise AssertionError("unsafe managed role reached SQLite")

    monkeypatch.setattr(staging_guard.sqlite3, "connect", tracked_connect)
    with pytest.raises(ProfilePathError):
        managed_staging.verify_registered_staging(support, _PROFILE_ID)

    assert connect_attempts == []
    assert main_bytes == (preserved_path.read_bytes() if preserved_path else main.read_bytes())
    if fault == "fifo":
        assert stat.S_ISFIFO(role_path.lstat().st_mode)
    elif role_path.is_symlink():
        assert preserved_path is not None and preserved_path.read_bytes() == role_bytes
    else:
        assert role_path.read_bytes() == role_bytes
    if linked_path is not None:
        assert linked_path.read_bytes() == role_bytes


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL only")
@pytest.mark.parametrize("role", ["main", "sidecar"])
def test_managed_main_and_sidecar_acl_allow_fail_before_sqlite_open(
    registered_profile: tuple[Path, Path, object, object],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    support, _base, _blank, managed = registered_profile
    main = managed.staging_database
    target = main if role == "main" else Path(f"{main}-wal")
    if role == "sidecar":
        target.write_bytes(b"synthetic ACL sidecar")
        target.chmod(0o600)
    before = target.read_bytes()
    subprocess.run(["chmod", "+a", "everyone allow read", str(target)], check=True)

    connect_attempts: list[str] = []

    def tracked_connect(database: object, *args: object, **kwargs: object) -> object:
        connect_attempts.append(str(database))
        raise AssertionError("ACL-protected managed role reached SQLite")

    monkeypatch.setattr(staging_guard.sqlite3, "connect", tracked_connect)
    with pytest.raises(ProfilePathError):
        managed_staging.verify_registered_staging(support, _PROFILE_ID)

    assert connect_attempts == []
    assert target.read_bytes() == before


def test_existing_unregistered_database_is_not_adopted_or_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base, blank = _blank_profile(tmp_path, monkeypatch)
    try:
        # Simulate an interrupted bootstrap after SQLite created/authorized the
        # fixed main file but before the managed registration was published.
        conn = create_staging_database(blank.staging_database)
        conn.close()
        main = blank.staging_database
        before = main.read_bytes()

        with pytest.raises(ProfilePathError):
            managed_staging.verify_registered_staging(support, _PROFILE_ID)
        with pytest.raises(ProfilePathError):
            managed_staging.bootstrap_registered_staging(blank)

        assert main.read_bytes() == before
        assert not (base / MANAGED_STAGING_FILENAME).exists()
    finally:
        blank.close()


def test_copied_database_and_registration_are_refused_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, source_base, source_blank = _blank_profile(tmp_path, monkeypatch)
    source = managed_staging.bootstrap_registered_staging(source_blank)
    target_support, _target_base, target_blank = _blank_profile(tmp_path, monkeypatch, "copied")
    try:
        assert target_support == support
        source_main = source.staging_database.read_bytes()
        source_registration = source.registration.read_bytes()

        target_main = target_blank.staging_database
        target_registration = target_blank.profile / MANAGED_STAGING_FILENAME
        target_main.write_bytes(source_main)
        target_main.chmod(0o600)
        target_registration.write_bytes(source_registration)
        target_registration.chmod(0o600)
        main_before = target_main.read_bytes()
        registration_before = target_registration.read_bytes()

        with pytest.raises(ProfilePathError):
            managed_staging.verify_registered_staging(support, "copied")

        assert target_main.read_bytes() == main_before
        assert target_registration.read_bytes() == registration_before
        assert source_main == source.staging_database.read_bytes()
    finally:
        source.close()
        source_blank.close()
        target_blank.close()


def test_incomplete_registration_publication_is_preserved_and_refused(
    registered_profile: tuple[Path, Path, object, object],
) -> None:
    support, base, _blank, managed = registered_profile
    pending = base / ".managed-staging.v1.pending"
    pending.write_bytes(b"synthetic interrupted publication\n")
    pending.chmod(0o600)
    main_before = managed.staging_database.read_bytes()
    registration_before = managed.registration.read_bytes()

    with pytest.raises(ProfilePathError, match="incomplete"):
        managed_staging.verify_registered_staging(support, _PROFILE_ID)

    assert pending.read_bytes() == b"synthetic interrupted publication\n"
    assert managed.staging_database.read_bytes() == main_before
    assert managed.registration.read_bytes() == registration_before


def test_wrong_runtime_root_is_refused_without_opening_registered_database(
    registered_profile: tuple[Path, Path, object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base, _blank, managed = registered_profile
    main_before = managed.staging_database.read_bytes()
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "workspace"))

    with pytest.raises(ProfilePathError, match="FINANCE_RUNTIME_ROOT"):
        managed_staging.verify_registered_staging(support, _PROFILE_ID)

    assert managed.staging_database.read_bytes() == main_before


def test_unknown_bootstrap_sidecar_is_preserved_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _support, _base, blank = _blank_profile(tmp_path, monkeypatch)
    try:
        sidecar = Path(f"{blank.staging_database}-wal")
        sidecar.write_bytes(b"unknown synthetic sidecar")
        sidecar.chmod(0o600)

        with pytest.raises(ProfilePathError, match="reserved name|sidecar|role"):
            managed_staging.bootstrap_registered_staging(blank)

        assert sidecar.read_bytes() == b"unknown synthetic sidecar"
        assert not blank.staging_database.exists()
    finally:
        blank.close()


def test_shared_gate_covers_connection_until_close(
    registered_profile: tuple[Path, Path, object, object],
) -> None:
    support, _base, _blank, managed = registered_profile
    probe = r"""
import sys
from finance_core.profile_gate import ProfileGateBusy, exclusive_cut
from finance_core.profile_paths import validate_registered_staging_profile
support, profile_id = sys.argv[1:3]
profile = validate_registered_staging_profile(support, profile_id)
try:
    try:
        with exclusive_cut(profile, timeout_seconds=0):
            raise SystemExit(2)
    except ProfileGateBusy:
        raise SystemExit(0)
    raise SystemExit(3)
finally:
    profile.close()
"""

    with managed_staging._managed_staging_connection(managed, purpose="reopen"):
        result = subprocess.run(
            [sys.executable, "-c", probe, str(support), _PROFILE_ID],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    with exclusive_cut(managed, timeout_seconds=0) as cut:
        cut.assert_valid()


def test_other_thread_revalidation_waits_for_managed_connection_close(
    registered_profile: tuple[Path, Path, object, object],
) -> None:
    _support, _base, _blank, managed = registered_profile
    lifetime_lock = profile_paths._MANAGED_SQLITE_LIFETIME_LOCK
    observed_contention = threading.Event()
    completed = threading.Event()
    errors: list[BaseException] = []

    class ObservedLifetimeLock:
        def __enter__(self) -> None:
            if not lifetime_lock._lock.acquire(blocking=False):
                observed_contention.set()
            else:
                lifetime_lock._lock.release()
            return lifetime_lock.__enter__()

        def __exit__(self, *args: object) -> None:
            lifetime_lock.__exit__(*args)

        def mark_uncertain(self) -> None:
            lifetime_lock.mark_uncertain()

    def revalidate_in_other_thread() -> None:
        try:
            managed.revalidate()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    with managed_staging._managed_staging_connection(managed, purpose="reopen"):
        original_lock = profile_paths._MANAGED_SQLITE_LIFETIME_LOCK
        profile_paths._MANAGED_SQLITE_LIFETIME_LOCK = ObservedLifetimeLock()  # type: ignore[assignment]
        worker = threading.Thread(target=revalidate_in_other_thread)
        try:
            worker.start()
            assert observed_contention.wait(timeout=5)
            assert not completed.is_set()
        finally:
            profile_paths._MANAGED_SQLITE_LIFETIME_LOCK = original_lock

    assert completed.wait(timeout=5)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []


@pytest.mark.parametrize("operation", ["reopen", "bootstrap"])
def test_exclusive_cut_can_validate_while_managed_writer_waits_for_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _support, _base, blank = _blank_profile(tmp_path, monkeypatch)
    managed = managed_staging.bootstrap_registered_staging(blank) if operation == "reopen" else None
    profile = managed if managed is not None else blank
    shared_waiting = threading.Event()
    completed = threading.Event()
    errors: list[BaseException] = []
    sqlite_opens: list[str] = []
    real_try_lock = profile_gate._try_lock
    real_connect = staging_guard.sqlite3.connect

    def observe_try_lock(fd: int, mode: int) -> bool:
        acquired = real_try_lock(fd, mode)
        if mode == fcntl.LOCK_SH and not acquired:
            shared_waiting.set()
        return acquired

    def observe_connect(database: object, *args: object, **kwargs: object) -> object:
        sqlite_opens.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(profile_gate, "_try_lock", observe_try_lock)
    monkeypatch.setattr(staging_guard.sqlite3, "connect", observe_connect)

    def shared_writer() -> None:
        try:
            if operation == "reopen":
                assert managed is not None
                with managed_staging._managed_staging_connection(managed, purpose="reopen"):
                    pass
            else:
                enrolled = managed_staging.bootstrap_registered_staging(blank)
                enrolled.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=shared_writer)
    try:
        with exclusive_cut(profile, max_hold_seconds=4) as cut:
            worker.start()
            assert shared_waiting.wait(timeout=2), "shared writer did not reach the held gate"
            assert sqlite_opens == []
            with profile_paths._MANAGED_SQLITE_LIFETIME_LOCK:
                cut.assert_valid()
            assert sqlite_opens == []
            assert not completed.is_set()
        assert completed.wait(timeout=5)
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert errors == []
        assert sqlite_opens != []
    finally:
        if worker.is_alive():
            worker.join(timeout=5)
        if managed is not None:
            managed.close()
        blank.close()


def test_configuration_failure_with_successful_close_releases_gate(
    registered_profile: tuple[Path, Path, object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _support, _base, _blank, managed = registered_profile

    def fail_configuration(_conn: object) -> None:
        raise RuntimeError("synthetic SQLite configuration failure")

    monkeypatch.setattr(staging_guard, "configure_sqlite_connection", fail_configuration)
    with pytest.raises(RuntimeError, match="synthetic SQLite configuration failure"):
        with managed_staging._managed_staging_connection(managed, purpose="reopen"):
            pytest.fail("open must not yield after configuration fails")

    with exclusive_cut(managed, timeout_seconds=0) as cut:
        cut.assert_valid()


def test_keyboard_interrupt_during_configuration_releases_gate_after_close(
    registered_profile: tuple[Path, Path, object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _support, _base, _blank, managed = registered_profile

    def interrupt_configuration(_conn: object) -> None:
        raise KeyboardInterrupt("synthetic configuration interrupt")

    monkeypatch.setattr(staging_guard, "configure_sqlite_connection", interrupt_configuration)
    with pytest.raises(KeyboardInterrupt, match="synthetic configuration interrupt"):
        with managed_staging._managed_staging_connection(managed, purpose="reopen"):
            pytest.fail("open must not yield after KeyboardInterrupt")

    with exclusive_cut(managed, timeout_seconds=0) as cut:
        cut.assert_valid()


_CRASH_WORKER = r"""
import os
import sys
import time
from pathlib import Path

import finance_core.managed_staging_profile as managed
from finance_core.profile_paths import validate_registered_staging_profile

support, profile_id, mode, database, ready = sys.argv[1:6]
profile = validate_registered_staging_profile(support, profile_id)
with managed._managed_staging_connection(profile, purpose="reopen") as conn:
    if mode == "hot-journal":
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA cache_size=1")
        conn.execute("BEGIN IMMEDIATE")
        for index in range(80):
            conn.execute(
                "INSERT INTO raw_intake_records "
                "(public_id, source_type, source_channel, raw_input, received_at) "
                "VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z')",
                (f"d4-uncommitted-{index}", "synthetic " + ("x" * 3000)),
            )
        sidecar = Path(database + "-journal")
        deadline = time.monotonic() + 5
        while (
            not sidecar.exists() or sidecar.stat().st_size < 512
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sidecar.exists() and sidecar.stat().st_size >= 512
    elif mode == "wal":
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at) "
            "VALUES ('d4-wal-committed', 'manual_entry', 'manual', "
            "'synthetic committed WAL row', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        sidecar = Path(database + "-wal")
        assert sidecar.exists() and sidecar.stat().st_size > 32
    else:
        raise AssertionError("unknown crash fixture")
    Path(ready).write_text("ready", encoding="ascii")
    while True:
        time.sleep(1)
"""


def _kill_after_marker(support: Path, managed: object, tmp_path: Path, mode: str) -> None:
    ready = tmp_path / f"{mode}.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CRASH_WORKER,
            str(support),
            _PROFILE_ID,
            mode,
            str(managed.staging_database),
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 8
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"crash worker did not reach its fixture point: {stdout}\n{stderr}")
    process.kill()
    process.wait(timeout=5)
    assert process.returncode is not None


def test_real_hot_journal_crash_reopens_committed_rows_and_rolls_back_new_rows(
    registered_profile: tuple[Path, Path, object, object], tmp_path: Path
) -> None:
    support, _base, _blank, managed = registered_profile
    with managed_staging._managed_staging_connection(managed, purpose="reopen") as conn:
        _insert_raw_intake(conn, "d4-hot-committed", "synthetic committed row")
        conn.commit()

    _kill_after_marker(support, managed, tmp_path, "hot-journal")
    journal = Path(f"{managed.staging_database}-journal")
    assert journal.exists() and journal.stat().st_size >= 512

    with managed_staging._managed_staging_connection(managed, purpose="reopen") as conn:
        committed = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE public_id='d4-hot-committed'"
        ).fetchone()
        uncommitted_count = conn.execute(
            "SELECT COUNT(*) FROM raw_intake_records WHERE public_id LIKE 'd4-uncommitted-%'"
        ).fetchone()[0]
        assert committed is not None and committed[0] == "synthetic committed row"
        assert uncommitted_count == 0
    assert not journal.exists()


def test_committed_wal_survives_process_crash_and_managed_reopen(
    registered_profile: tuple[Path, Path, object, object], tmp_path: Path
) -> None:
    support, _base, _blank, managed = registered_profile

    _kill_after_marker(support, managed, tmp_path, "wal")
    wal = Path(f"{managed.staging_database}-wal")
    assert wal.exists() and wal.stat().st_size > 32

    with managed_staging._managed_staging_connection(managed, purpose="reopen") as conn:
        row = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE public_id='d4-wal-committed'"
        ).fetchone()
        assert row is not None and row[0] == "synthetic committed WAL row"


_UNCERTAIN_CLOSE_WORKER = r"""
import sys
import time
from pathlib import Path

import finance_core.managed_staging_profile as managed
import finance_core.staging_guard as guard
from finance_core.profile_paths import validate_registered_staging_profile

support, profile_id, ready = sys.argv[1:4]
profile = validate_registered_staging_profile(support, profile_id)
real_connect = guard.sqlite3.connect

class CloseFailingConnection:
    def __init__(self, inner):
        self.inner = inner
    def __getattr__(self, name):
        return getattr(self.inner, name)
    def close(self):
        raise CloseAbort("synthetic close BaseException")

class CloseAbort(BaseException):
    pass

guard.sqlite3.connect = lambda *args, **kwargs: CloseFailingConnection(
    real_connect(*args, **kwargs)
)
def fail_configuration(_conn):
    raise KeyboardInterrupt("synthetic configuration interrupt")
guard.configure_sqlite_connection = fail_configuration

try:
    with managed._managed_staging_connection(profile, purpose="reopen"):
        raise AssertionError("open must fail before yielding")
except guard._StagingCloseUncertain as exc:
    assert isinstance(exc.operation_error, KeyboardInterrupt)
    assert str(exc.operation_error) == "synthetic configuration interrupt"
    assert isinstance(exc.__cause__, CloseAbort)
    Path(ready).write_text("close-uncertain", encoding="ascii")
else:
    raise AssertionError("uncertain close must not be reported as a normal failure")
while True:
    time.sleep(1)
"""


def test_uncertain_sqlite_close_keeps_gate_until_child_exit_and_reap(
    registered_profile: tuple[Path, Path, object, object], tmp_path: Path
) -> None:
    support, _base, _blank, managed = registered_profile
    ready = tmp_path / "close-uncertain.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _UNCERTAIN_CLOSE_WORKER,
            str(support),
            _PROFILE_ID,
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"close-failure worker did not reach its hold point: {stdout}\n{stderr}")

        with pytest.raises(ProfileGateBusy):
            with exclusive_cut(managed, timeout_seconds=0):
                pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    # The child kept both uncertain SQLite ownership and its shared lease alive;
    # the exclusive cut becomes available only after process exit/reap.
    with exclusive_cut(managed, timeout_seconds=0) as cut:
        cut.assert_valid()
