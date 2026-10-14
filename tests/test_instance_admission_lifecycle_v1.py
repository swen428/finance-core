"""Independent system regressions for production-instance admission lifetimes.

All records and profiles are synthetic and live under pytest's temporary root.
The multiprocess cases use pipes and process completion, never timing sleeps.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sqlite3
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from finance_core.instance_admission import (
    PENDING_FILENAME,
    STATE_FILENAME,
    InstanceAdmissionBusy,
    InstanceAdmissionError,
    ProductionCloseUncertain,
    TrustedInstanceConfig,
    abort_maintenance_hold,
    acquire_production_owner,
    admit_production_instance,
    enter_maintenance_hold,
    fence_production_instance,
    inspect_instance_admission,
    inspect_maintenance_hold,
    prepare_production_profile,
    production_read_operation,
    production_write_operation,
    require_finance_write_connection,
    resume_authority_transition,
)
from finance_core.profile_layout import profile_root
from finance_core.profile_paths import ProfilePathError
from finance_core.staging_guard import StagingDatabaseError
from tests.instance_admission_support_v1 import synthetic_config

_CONFIG_IDENTITY = "synthetic-config-v1"
_CORE_IDENTITY = "synthetic-core-v1"
_INSTANCE_ID = "synthetic-instance-v1"
_PROFILE_ID = "synthetic"

# CPython 3.12's native handles have more callable surface than this session
# intends to expose. This inventory deliberately classifies every public
# member, plus the protocol methods used for connection/cursor lifetime and
# iteration. A newly added native API changes this assertion until its policy
# and regression coverage are reviewed.
_SQLITE_312_SURFACE = {
    sqlite3.Connection: {
        "error_types": frozenset(
            {
                "DataError",
                "DatabaseError",
                "Error",
                "IntegrityError",
                "InterfaceError",
                "InternalError",
                "NotSupportedError",
                "OperationalError",
                "ProgrammingError",
                "Warning",
            }
        ),
        "guarded_operations": frozenset(
            {
                "__enter__",
                "__exit__",
                "close",
                "commit",
                "cursor",
                "execute",
                "executemany",
                "rollback",
            }
        ),
        "guarded_configuration": frozenset(
            {"autocommit", "isolation_level", "row_factory", "text_factory"}
        ),
        "guarded_diagnostics": frozenset({"getconfig", "getlimit", "interrupt"}),
        "observational_attributes": frozenset({"in_transaction", "total_changes"}),
        "refused_operations": frozenset(
            {
                "backup",
                "blobopen",
                "create_aggregate",
                "create_collation",
                "create_function",
                "create_window_function",
                "deserialize",
                "enable_load_extension",
                "executescript",
                "iterdump",
                "load_extension",
                "serialize",
                "set_authorizer",
                "set_progress_handler",
                "set_trace_callback",
                "setconfig",
                "setlimit",
            }
        ),
        "native_internal_compiler": frozenset({"__call__"}),
    },
    sqlite3.Cursor: {
        "guarded_operations": frozenset(
            {
                "__iter__",
                "__next__",
                "close",
                "execute",
                "executemany",
                "fetchall",
                "fetchmany",
                "fetchone",
            }
        ),
        "guarded_configuration": frozenset({"row_factory"}),
        "metadata": frozenset({"arraysize", "connection", "description", "lastrowid", "rowcount"}),
        "guarded_no_op_hints": frozenset({"setinputsizes", "setoutputsize"}),
        "refused_operations": frozenset({"executescript"}),
    },
    sqlite3.Blob: {
        "unreachable_handle_surface": frozenset(
            {"close", "read", "seek", "tell", "write", "__enter__", "__exit__"}
        ),
    },
}

_PYTHON_PROTOCOL_MEMBERS = frozenset({"__call__", "__enter__", "__exit__", "__iter__", "__next__"})


def _child_config(control_root: Path, locator_root: Path) -> TrustedInstanceConfig:
    """Rebuild trusted composition in the child; parent witnesses are PID-bound."""
    layout = "linux" if Path(locator_root).name == "finance-codex" else "mac"
    return TrustedInstanceConfig(
        control_root=Path(control_root),
        locator_root=Path(locator_root),
        profile_id=_PROFILE_ID,
        instance_id=_INSTANCE_ID,
        layout=layout,
        config_identity=_CONFIG_IDENTITY,
        core_identity=_CORE_IDENTITY,
        approval_verifier=lambda ref, _action, _binding: ref == "approved",
    )


def _owner_attempt(control_root: Path, locator_root: Path, sender) -> None:
    config = _child_config(control_root, locator_root)
    try:
        with acquire_production_owner(config, timeout_seconds=0):
            sender.send(("acquired", None))
    except InstanceAdmissionBusy as exc:
        sender.send(("busy", type(exc).__name__))
    except BaseException as exc:  # report child failures instead of hiding them in a pipe timeout
        sender.send(("error", type(exc).__name__))


def _fence_after_start(control_root: Path, locator_root: Path, sender) -> None:
    config = _child_config(control_root, locator_root)
    sender.send(("started", None))
    try:
        state = fence_production_instance(
            config, operation_id="fence-during-write", approval_ref="approved"
        )
        sender.send(("fenced", state["state"]))
    except BaseException as exc:
        sender.send(("error", type(exc).__name__))


def _crash_transition_at_filesystem_boundary(
    control_root: Path,
    locator_root: Path,
    stage: str,
    transition: str,
    operation_id: str,
    evidence_ref: str,
    sender,
) -> None:
    """Exit without acknowledgement at one real authority publication boundary."""
    config = _child_config(control_root, locator_root)
    import finance_core.instance_admission.authority as authority

    root_info = Path(control_root).stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    real_open = os.open
    real_write = os.write
    real_fsync = os.fsync
    real_replace = os.replace
    real_unlink = os.unlink
    fd_roles: dict[int, str] = {}
    pending_synced = False
    candidate_synced = False
    candidate_replaced = False
    exits = {
        "journal-create": 81,
        "journal-write": 82,
        "journal-file-fsync": 83,
        "journal-dir-fsync": 84,
        "state-write": 85,
        "state-file-fsync": 86,
        "state-dir-fsync-before-replace": 87,
        "state-replace": 88,
        "state-dir-fsync-after-replace": 89,
        "cleanup": 90,
        "before-return": 91,
    }
    if stage not in exits:  # pragma: no cover - the parent controls this list
        raise AssertionError(stage)

    def in_control_dir(fd: int | None) -> bool:
        if fd is None:
            return False
        opened = os.fstat(fd)
        return (opened.st_dev, opened.st_ino) == root_identity

    def open_boundary(path, flags, mode=0o777, *args, **kwargs):
        fd = real_open(path, flags, mode, *args, **kwargs)
        if in_control_dir(kwargs.get("dir_fd")) and path in {
            PENDING_FILENAME,
            "authority.v1.next",
        }:
            fd_roles[fd] = str(path)
            if path == PENDING_FILENAME and stage == "journal-create":
                os._exit(exits[stage])
        return fd

    def write_boundary(fd: int, payload) -> int:
        size = real_write(fd, payload)
        role = fd_roles.get(fd)
        if role == PENDING_FILENAME and stage == "journal-write":
            os._exit(exits[stage])
        if role == "authority.v1.next" and stage == "state-write":
            os._exit(exits[stage])
        return size

    def fsync_boundary(fd: int) -> None:
        nonlocal pending_synced, candidate_synced
        real_fsync(fd)
        role = fd_roles.get(fd)
        if role == PENDING_FILENAME:
            pending_synced = True
            if stage == "journal-file-fsync":
                os._exit(exits[stage])
        elif role == "authority.v1.next":
            candidate_synced = True
            if stage == "state-file-fsync":
                os._exit(exits[stage])
        elif in_control_dir(fd):
            if pending_synced and stage == "journal-dir-fsync":
                os._exit(exits[stage])
            if (
                candidate_synced
                and not candidate_replaced
                and stage == "state-dir-fsync-before-replace"
            ):
                os._exit(exits[stage])
            if candidate_replaced and stage == "state-dir-fsync-after-replace":
                os._exit(exits[stage])

    def replace_boundary(src, dst, *args, **kwargs):
        nonlocal candidate_replaced
        result = real_replace(src, dst, *args, **kwargs)
        if src == "authority.v1.next" and dst == STATE_FILENAME:
            candidate_replaced = True
            if stage == "state-replace":
                os._exit(exits[stage])
        return result

    def unlink_boundary(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == PENDING_FILENAME and in_control_dir(kwargs.get("dir_fd")) and stage == "cleanup":
            os._exit(exits[stage])
        return result

    os.open = open_boundary
    os.write = write_boundary
    os.fsync = fsync_boundary
    os.replace = replace_boundary
    os.unlink = unlink_boundary
    if stage == "before-return":
        real_publish = authority._publish

        def publish_then_die(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            os._exit(exits[stage])
            return result

        authority._publish = publish_then_die

    try:
        if transition == "abort":
            abort_maintenance_hold(
                config,
                operation_id=operation_id,
                approval_ref="approved",
                evidence_ref=evidence_ref,
            )
        elif transition == "fence":
            fence_production_instance(config, operation_id=operation_id, approval_ref="approved")
        else:  # pragma: no cover - the parent controls this value
            raise AssertionError(transition)
    except BaseException as exc:
        sender.send(("unexpected-error", type(exc).__name__))
        raise
    sender.send(("returned", None))


def _crash_child_result(process, receiver, expected_exit: int) -> None:
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("crash injector did not reach its selected filesystem boundary")
    assert process.exitcode == expected_exit
    if receiver.poll(0):
        try:
            unexpected = receiver.recv()
        except EOFError:
            receiver.close()
            return
        receiver.close()
        pytest.fail(f"crash path acknowledged or raised before the intended boundary: {unexpected}")
    receiver.close()
    pytest.fail("crash process exited without closing its result pipe")


def _crash_uncommitted_write(control_root: Path, locator_root: Path, sender) -> None:
    config = _child_config(control_root, locator_root)
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE lifecycle_crash_probe (value TEXT NOT NULL)")
            conn.execute("INSERT INTO lifecycle_crash_probe VALUES ('uncommitted')")
            sender.send(("transaction-open", None))
            os._exit(73)


class _ProcessEnvironment:
    def setenv(self, name: str, value: str) -> None:
        os.environ[name] = value


def _uncertain_close_worker(tmp_root: Path, sender) -> None:
    import finance_core.instance_admission.sessions as session_module
    from tests.instance_admission_support_v1 import synthetic_config

    config = synthetic_config(Path(tmp_root), _ProcessEnvironment())
    profile = prepare_production_profile(
        config, operation_id="prepare-uncertain", approval_ref="approved"
    )
    profile.close()
    admit_production_instance(config, operation_id="admit-uncertain", approval_ref="approved")
    owner = acquire_production_owner(config)
    operation = production_write_operation(owner)
    conn = operation.__enter__()
    real_close = session_module._close_sqlite_handle

    def uncertain_close(_connection) -> None:
        raise sqlite3.OperationalError("synthetic close uncertainty")

    # The API's cleanup must retain the actual handle and leases when close
    # cannot be proved. Patch only its explicit private fault boundary in this
    # isolated process.
    session_module._close_sqlite_handle = uncertain_close
    try:
        try:
            operation.__exit__(None, None, None)
        except ProductionCloseUncertain as exc:
            uncertain = exc
        else:
            sender.send(("error", "close uncertainty was not reported"))
            os._exit(2)
    finally:
        session_module._close_sqlite_handle = real_close

    retained_query = sqlite3.Connection.execute(conn, "SELECT 1").fetchone()[0]
    owner_close_refused = False
    try:
        owner.close()
    except InstanceAdmissionError:
        owner_close_refused = True

    process, receiver = _start_spawn_process(
        _owner_attempt, (config.control_root, config.locator_root)
    )
    owner_result = _receive(receiver, process)
    sender.send(
        (
            "retained",
            uncertain.connection is conn,
            uncertain.owner is owner,
            retained_query == 1,
            owner_close_refused,
            owner_result[0] == "busy",
        )
    )
    os._exit(0)


def _forked_connection_attempt(conn: sqlite3.Connection, owner, sender) -> None:
    try:
        owner.close()
    except BaseException as exc:
        owner_close = ("refused", type(exc).__name__)
    else:
        owner_close = ("accepted", None)
    try:
        require_finance_write_connection(conn)
    except BaseException as exc:
        sender.send((owner_close, ("refused", type(exc).__name__)))
        os._exit(0)
    conn.execute("INSERT INTO lifecycle_fork_probe VALUES ('child')")
    conn.commit()
    sender.send((owner_close, ("accepted", None)))
    os._exit(1)


def _start_fork_process(target, args: tuple[object, ...]):
    context = mp.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(*args, sender))
    process.start()
    sender.close()
    return process, receiver


def _start_spawn_process(target, args: tuple[object, ...]):
    """Start an independent process without inheriting active lock descriptors."""
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(*args, sender))
    process.start()
    sender.close()
    return process, receiver


def _receive(receiver, process, *, timeout: float = 10.0):
    assert receiver.poll(timeout), f"child produced no result (exit={process.exitcode})"
    result = receiver.recv()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(timeout)
        pytest.fail("child did not finish after sending its result")
    receiver.close()
    return result


def _prepare_and_admit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    database = profile.live_database
    profile.close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    return config, database


def _classified_native_members(native_type: type) -> set[str]:
    categories = _SQLITE_312_SURFACE[native_type]
    flattened = [name for names in categories.values() for name in names]
    assert len(flattened) == len(set(flattened)), f"overlapping {native_type.__name__} categories"
    return set(flattened)


def test_python_312_sqlite_native_surfaces_are_fully_classified() -> None:
    assert sys.version_info[:2] == (3, 12), "the instance API inventory targets Python 3.12"

    for native_type in (sqlite3.Connection, sqlite3.Cursor, sqlite3.Blob):
        observed = {
            name
            for name in dir(native_type)
            if not name.startswith("_") or name in _PYTHON_PROTOCOL_MEMBERS
        }
        assert observed == _classified_native_members(native_type)


def _logical_database_snapshot(database_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True) as raw:
        schema = tuple(
            tuple(row)
            for row in raw.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        )
        tables = [
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        rows = []
        for table in tables:
            quoted_table = '"' + table.replace('"', '""') + '"'
            table_rows = tuple(tuple(row) for row in raw.execute(f"SELECT * FROM {quoted_table}"))
            rows.append((table, table_rows))
    return schema, tuple(rows)


def _serialized_synthetic_database() -> bytes:
    source = sqlite3.connect(":memory:")
    try:
        source.execute("CREATE TABLE synthetic_restore_probe (value TEXT NOT NULL)")
        return source.serialize()
    finally:
        source.close()


def _exercise_supported_native_subset(conn: sqlite3.Connection, *, read_only: bool) -> None:
    assert conn.row_factory is sqlite3.Row
    assert conn.text_factory is str
    assert conn.isolation_level == ""
    assert conn.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL

    row = conn.execute("SELECT 'supported' AS value").fetchone()
    assert type(row) is sqlite3.Row and type(row["value"]) is str
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 AS value")
        assert cursor.fetchone()["value"] == 1
    finally:
        cursor.close()

    if read_only:
        conn.commit()
        conn.rollback()
        return

    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO participants(public_id, display_name) VALUES (?, ?)",
        [("native-surface-probe", "Synthetic")],
    )
    assert conn.in_transaction
    conn.rollback()
    assert not conn.in_transaction
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM participants WHERE public_id=?", ("native-surface-probe",)
        ).fetchone()[0]
        == 0
    )

    conn.execute("BEGIN IMMEDIATE")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        pass
    assert not conn.in_transaction


def _native_refusal_calls(
    conn: sqlite3.Connection,
    *,
    backup_target: sqlite3.Connection,
    serialized_source: bytes,
    callback_calls: list[str],
    missing_extension: Path,
) -> dict[str, Callable[[], object]]:
    def authorizer(*_args) -> int:
        callback_calls.append("authorizer")
        return sqlite3.SQLITE_OK

    def scalar() -> int:
        callback_calls.append("function")
        return 1

    def collation(_left: str, _right: str) -> int:
        callback_calls.append("collation")
        return 0

    class RecordingAggregate:
        def step(self, _value: int) -> None:
            callback_calls.append("aggregate.step")

        def finalize(self) -> int:
            callback_calls.append("aggregate.finalize")
            return 1

    class RecordingWindow:
        def step(self, _value: int) -> None:
            callback_calls.append("window.step")

        def inverse(self, _value: int) -> None:
            callback_calls.append("window.inverse")

        def value(self) -> int:
            callback_calls.append("window.value")
            return 1

        def finalize(self) -> int:
            callback_calls.append("window.finalize")
            return 1

    return {
        "backup": lambda: conn.backup(backup_target),
        "create_aggregate": lambda: conn.create_aggregate(
            "pr11_surface_aggregate", 1, RecordingAggregate
        ),
        "create_collation": lambda: conn.create_collation("pr11_surface_collation", collation),
        "create_function": lambda: conn.create_function("pr11_surface_function", 0, scalar),
        "create_window_function": lambda: conn.create_window_function(
            "pr11_surface_window", 1, RecordingWindow
        ),
        "deserialize": lambda: conn.deserialize(serialized_source),
        "enable_load_extension": lambda: conn.enable_load_extension(False),
        "executescript": lambda: conn.executescript("SELECT 1;"),
        "iterdump": lambda: next(conn.iterdump()),
        "load_extension": lambda: conn.load_extension(str(missing_extension)),
        "serialize": lambda: conn.serialize(),
        "set_authorizer": lambda: conn.set_authorizer(authorizer),
        "set_progress_handler": lambda: conn.set_progress_handler(
            lambda: callback_calls.append("progress") or 0, 1
        ),
        "set_trace_callback": lambda: conn.set_trace_callback(
            lambda _statement: callback_calls.append("trace")
        ),
        "setconfig": lambda: conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_FKEY, True),
        "setlimit": lambda: conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024),
    }


def _assert_native_refusals(
    conn: sqlite3.Connection,
    *,
    backup_target: sqlite3.Connection,
    serialized_source: bytes,
    callback_calls: list[str],
    missing_extension: Path,
    exercise_callback_probes: bool,
) -> None:
    refused = _native_refusal_calls(
        conn,
        backup_target=backup_target,
        serialized_source=serialized_source,
        callback_calls=callback_calls,
        missing_extension=missing_extension,
    )
    expected = _SQLITE_312_SURFACE[sqlite3.Connection]["refused_operations"] - {"blobopen"}
    assert set(refused) == expected
    previous_calls = list(callback_calls)
    for name, operation in refused.items():
        with pytest.raises(InstanceAdmissionError):
            operation()
        assert callback_calls == previous_calls, f"{name} invoked a Python callback"

    if exercise_callback_probes:
        # If a callback had been installed before a refusal, the probes below
        # would run it. Missing callback-backed SQL objects are expected errors.
        for statement in (
            "SELECT 1",
            "SELECT pr11_surface_function()",
            "SELECT pr11_surface_aggregate(value) FROM (SELECT 1 AS value)",
            "SELECT pr11_surface_window(value) OVER () FROM (SELECT 1 AS value)",
            "SELECT 'left' = 'right' COLLATE pr11_surface_collation",
        ):
            try:
                conn.execute(statement).fetchall()
            except sqlite3.Error:
                pass
        assert callback_calls == previous_calls

    assert (
        backup_target.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    )


def test_issued_sessions_refuse_unsupported_native_apis_without_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _prepare_and_admit(tmp_path, monkeypatch)
    before = _logical_database_snapshot(database)
    serialized_source = _serialized_synthetic_database()
    callback_calls: list[str] = []
    backup_target = sqlite3.connect(":memory:")
    missing_extension = tmp_path / "missing-synthetic-extension.so"
    stale_connections: list[sqlite3.Connection] = []

    try:
        with acquire_production_owner(config) as owner:
            for read_only, operation_factory in (
                (False, production_write_operation),
                (True, production_read_operation),
            ):
                with operation_factory(owner) as conn:
                    _exercise_supported_native_subset(conn, read_only=read_only)
                    transaction_before = conn.in_transaction
                    changes_before = conn.total_changes
                    _assert_native_refusals(
                        conn,
                        backup_target=backup_target,
                        serialized_source=serialized_source,
                        callback_calls=callback_calls,
                        missing_extension=missing_extension,
                        exercise_callback_probes=True,
                    )
                    assert conn.in_transaction is transaction_before is False
                    assert conn.total_changes == changes_before
                    stale_connections.append(conn)

            for conn in stale_connections:
                before_calls = list(callback_calls)
                _assert_native_refusals(
                    conn,
                    backup_target=backup_target,
                    serialized_source=serialized_source,
                    callback_calls=callback_calls,
                    missing_extension=missing_extension,
                    exercise_callback_probes=False,
                )
                assert callback_calls == before_calls
    finally:
        backup_target.close()

    assert _logical_database_snapshot(database) == before


@pytest.mark.skipif(
    sys.platform != "linux", reason="the fixed Linux production profile exists only on Linux"
)
def test_linux_profile_uses_fixed_linux_production_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = synthetic_config(tmp_path, monkeypatch)
    assert config.layout == "linux"
    assert config.locator_root.name == "finance-codex"
    expected_profile = config.locator_root / "profiles" / _PROFILE_ID

    profile = prepare_production_profile(
        config, operation_id="prepare-linux", approval_ref="approved"
    )
    assert profile.layout == "linux"
    assert profile.profile == expected_profile
    assert profile.live_database == expected_profile / "runtime/database/finance.db"
    profile.close()
    admitted = admit_production_instance(
        config, operation_id="admit-linux", approval_ref="approved"
    )
    assert admitted["state"] == "ACTIVE"
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 60


@pytest.mark.skipif("fork" not in mp.get_all_start_methods(), reason="requires POSIX fork")
def test_process_owner_stays_exclusive_until_owner_context_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _database = _prepare_and_admit(tmp_path, monkeypatch)
    paths = (config.control_root, config.locator_root)
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 60
            with pytest.raises(InstanceAdmissionError):
                owner.close()
            process, receiver = _start_spawn_process(_owner_attempt, paths)
            assert _receive(receiver, process)[0] == "busy"

        # Closing SQLite must not silently drop the process owner lease.
        process, receiver = _start_spawn_process(_owner_attempt, paths)
        assert _receive(receiver, process)[0] == "busy"

    process, receiver = _start_spawn_process(_owner_attempt, paths)
    assert _receive(receiver, process)[0] == "acquired"
    assert process.exitcode == 0


@pytest.mark.skipif("spawn" not in mp.get_all_start_methods(), reason="requires spawn")
def test_owner_close_cannot_race_a_live_connection_during_operation_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opening reservation keeps close from releasing the lease mid-connect."""
    import finance_core.instance_admission.sessions as session_module

    config, _database = _prepare_and_admit(tmp_path, monkeypatch)
    owner = acquire_production_owner(config)
    native_connection_ready = threading.Event()
    allow_connect_return = threading.Event()
    operation_open = threading.Event()
    release_operation = threading.Event()
    operation_finished = threading.Event()
    opened_connections: list[sqlite3.Connection] = []
    operation_errors: list[BaseException] = []
    closed_after_operation: list[bool] = []
    real_connect = session_module.sqlite3.connect

    def paused_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        assert sqlite3.Connection.execute(connection, "SELECT 1").fetchone()[0] == 1
        opened_connections.append(connection)
        if len(opened_connections) == 1:
            native_connection_ready.set()
            if allow_connect_return.wait(10):
                return connection
            sqlite3.Connection.close(connection)
            raise TimeoutError("test did not release paused SQLite connect")
        return connection

    monkeypatch.setattr(session_module.sqlite3, "connect", paused_connect)

    def open_operation() -> None:
        try:
            with production_write_operation(owner) as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
                operation_open.set()
                if not release_operation.wait(20):
                    raise TimeoutError("test did not release live production operation")
        except BaseException as exc:
            operation_errors.append(exc)
        finally:
            if opened_connections:
                try:
                    sqlite3.Connection.execute(opened_connections[0], "SELECT 1")
                except sqlite3.ProgrammingError:
                    closed_after_operation.append(True)
                else:
                    closed_after_operation.append(False)
            operation_finished.set()

    thread = threading.Thread(target=open_operation)
    thread.start()
    close_refused = False
    operation_was_live = False
    competing_owner_result: tuple[str, str | None] | None = None
    try:
        assert native_connection_ready.wait(10), "real SQLite handle did not reach open boundary"
        with pytest.raises(InstanceAdmissionError):
            with production_read_operation(owner, timeout_seconds=0):
                pytest.fail("an opening owner issued a nested production operation")
        try:
            owner.close()
        except InstanceAdmissionError:
            close_refused = True
        finally:
            # Release only after close has attempted to cross the opening
            # boundary. No sleep or scheduler-order assumption controls this.
            allow_connect_return.set()

        # Schema validation rebuilds the expected 60-migration inventory
        # before yielding the connection; allow that deterministic work to
        # finish rather than treating it as a scheduler race.
        operation_was_live = operation_open.wait(45)
        if operation_was_live:
            process, receiver = _start_spawn_process(
                _owner_attempt, (config.control_root, config.locator_root)
            )
            competing_owner_result = _receive(receiver, process)
        release_operation.set()
        assert operation_finished.wait(15), "operation thread did not finish cleanup"
        thread.join(1)
    finally:
        allow_connect_return.set()
        release_operation.set()
        if thread.is_alive():
            thread.join(10)
        if thread.is_alive():
            pytest.fail("operation thread remained live after both release events")
        try:
            owner.close()
        except InstanceAdmissionError:
            pass  # The implementation may already have safely closed after an operation.

    assert close_refused, "owner close released a lease while SQLite connect was in progress"
    assert operation_was_live, f"operation did not publish a live session: {operation_errors!r}"
    assert competing_owner_result is not None and competing_owner_result[0] == "busy"
    assert not operation_errors
    assert closed_after_operation == [True]
    process, receiver = _start_spawn_process(
        _owner_attempt, (config.control_root, config.locator_root)
    )
    assert _receive(receiver, process)[0] == "acquired"


@pytest.mark.skipif("fork" not in mp.get_all_start_methods(), reason="requires POSIX fork")
def test_fence_waits_for_the_real_write_connection_to_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _database = _prepare_and_admit(tmp_path, monkeypatch)
    paths = (config.control_root, config.locator_root)
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("SELECT COUNT(*) FROM schema_migrations")

            thread_started = threading.Event()
            thread_finished = threading.Event()
            thread_result: list[tuple[str, str]] = []

            def attempt_thread_fence() -> None:
                thread_started.set()
                try:
                    result = fence_production_instance(
                        config, operation_id="thread-fence-during-write", approval_ref="approved"
                    )
                except InstanceAdmissionBusy as exc:
                    thread_result.append(("busy", type(exc).__name__))
                except BaseException as exc:  # surface child-thread failures as test data
                    thread_result.append(("error", type(exc).__name__))
                else:
                    thread_result.append(("fenced", result["state"]))
                finally:
                    thread_finished.set()

            fence_thread = threading.Thread(target=attempt_thread_fence)
            fence_thread.start()
            assert thread_started.wait(10)
            assert thread_finished.wait(7), "thread fence returned no bounded result"
            fence_thread.join(1)
            assert thread_result == [("busy", "InstanceAdmissionBusy")]
            assert inspect_instance_admission(config)["state"] == "ACTIVE"

            process, receiver = _start_spawn_process(_fence_after_start, paths)
            assert receiver.poll(10)
            assert receiver.recv() == ("started", None)
            # The externally visible authority remains ACTIVE while the real
            # SQLite connection and operation leases are still open.
            assert inspect_instance_admission(config)["state"] == "ACTIVE"
            assert not receiver.poll(0)

        result = _receive(receiver, process)
        assert result == ("fenced", "FENCED")
        assert process.exitcode == 0
        assert inspect_instance_admission(config)["state"] == "FENCED"
        with production_read_operation(owner) as read_conn:
            assert read_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 60
            with pytest.raises(sqlite3.OperationalError):
                read_conn.execute(
                    "UPDATE schema_migrations SET migration_id=migration_id "
                    "WHERE migration_id='001'"
                )
        with pytest.raises(InstanceAdmissionError):
            with production_write_operation(owner):
                pytest.fail("a fenced instance issued a writable SQLite connection")


@pytest.mark.skipif("fork" not in mp.get_all_start_methods(), reason="requires POSIX fork")
def test_authority_publication_crashes_resume_once_at_each_durable_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    profile.close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")

    state_path = config.control_root / "authority.v1.json"
    pending_path = config.control_root / PENDING_FILENAME
    resumable_stages = (
        "journal-write",
        "journal-file-fsync",
        "journal-dir-fsync",
        "state-write",
        "state-file-fsync",
        "state-dir-fsync-before-replace",
        "state-replace",
        "state-dir-fsync-after-replace",
        "cleanup",
    )
    expected_codes = {
        "journal-write": 82,
        "journal-file-fsync": 83,
        "journal-dir-fsync": 84,
        "state-write": 85,
        "state-file-fsync": 86,
        "state-dir-fsync-before-replace": 87,
        "state-replace": 88,
        "state-dir-fsync-after-replace": 89,
        "cleanup": 90,
    }

    for index, stage in enumerate(resumable_stages, 1):
        enter_maintenance_hold(config, operation_id=f"hold-{index}", approval_ref="approved")
        before_bytes = state_path.read_bytes()
        before_version = json.loads(before_bytes)["version"]
        operation_id = f"abort-crash-{index}"
        process, receiver = _start_spawn_process(
            _crash_transition_at_filesystem_boundary,
            (
                config.control_root,
                config.locator_root,
                stage,
                "abort",
                operation_id,
                f"synthetic-no-effects-{index}",
            ),
        )
        _crash_child_result(process, receiver, expected_codes[stage])

        after_crash_bytes = state_path.read_bytes()
        pending_expected = stage != "cleanup"
        assert pending_path.exists() is pending_expected
        state_replaced = stage in {
            "state-replace",
            "state-dir-fsync-after-replace",
            "cleanup",
        }
        assert (after_crash_bytes != before_bytes) is state_replaced

        if pending_expected:
            with pytest.raises(InstanceAdmissionError):
                inspect_instance_admission(config)
            with pytest.raises(InstanceAdmissionError):
                with acquire_production_owner(config):
                    pytest.fail("pending authority issued a process owner")

        for wrong_operation, wrong_approval in (
            (f"wrong-{operation_id}", "approved"),
            (operation_id, "wrong-approval"),
        ):
            with pytest.raises(InstanceAdmissionError):
                resume_authority_transition(
                    config, operation_id=wrong_operation, approval_ref=wrong_approval
                )
            assert state_path.read_bytes() == after_crash_bytes
            assert pending_path.exists() is pending_expected

        resumed = resume_authority_transition(
            config, operation_id=operation_id, approval_ref="approved"
        )
        assert resumed["state"] == "ACTIVE"
        assert resumed["maintenance"] is None
        durable_bytes = state_path.read_bytes()
        durable = json.loads(durable_bytes)
        assert durable["version"] == before_version + 1
        assert sum(entry["operation_id"] == operation_id for entry in durable["history"]) == 1
        assert len({entry["operation_id"] for entry in durable["history"]}) == durable["version"]
        assert not pending_path.exists()

        # Recovery after a lost return is an observation of the exact durable
        # operation, never a second publication.
        repeated = resume_authority_transition(
            config, operation_id=operation_id, approval_ref="approved"
        )
        assert repeated == resumed
        assert state_path.read_bytes() == durable_bytes

    before_fence = state_path.read_bytes()
    before_version = json.loads(before_fence)["version"]
    process, receiver = _start_spawn_process(
        _crash_transition_at_filesystem_boundary,
        (
            config.control_root,
            config.locator_root,
            "before-return",
            "fence",
            "fence-before-return",
            "synthetic-no-effects-final",
        ),
    )
    _crash_child_result(process, receiver, 91)
    assert not pending_path.exists()
    completed_bytes = state_path.read_bytes()
    assert completed_bytes != before_fence
    for wrong_operation, wrong_approval in (
        ("wrong-fence-before-return", "approved"),
        ("fence-before-return", "wrong-approval"),
    ):
        with pytest.raises(InstanceAdmissionError):
            resume_authority_transition(
                config, operation_id=wrong_operation, approval_ref=wrong_approval
            )
        assert state_path.read_bytes() == completed_bytes
        assert not pending_path.exists()
    fenced = resume_authority_transition(
        config, operation_id="fence-before-return", approval_ref="approved"
    )
    assert fenced["state"] == "FENCED"
    assert state_path.read_bytes() == completed_bytes
    assert (
        resume_authority_transition(
            config, operation_id="fence-before-return", approval_ref="approved"
        )
        == fenced
    )
    assert state_path.read_bytes() == completed_bytes
    fenced_state = json.loads(completed_bytes)
    assert fenced_state["version"] == before_version + 1
    assert (
        sum(entry["operation_id"] == "fence-before-return" for entry in fenced_state["history"])
        == 1
    )
    with pytest.raises(InstanceAdmissionError):
        with acquire_production_owner(config) as owner:
            with production_write_operation(owner):
                pytest.fail("a lost-return recovery revived fenced write authority")


@pytest.mark.skipif("fork" not in mp.get_all_start_methods(), reason="requires POSIX fork")
def test_crash_during_journal_file_creation_fails_closed_without_inferred_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = synthetic_config(tmp_path, monkeypatch)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    profile.close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")
    enter_maintenance_hold(config, operation_id="hold-incomplete-journal", approval_ref="approved")

    state_path = config.control_root / STATE_FILENAME
    pending_path = config.control_root / PENDING_FILENAME
    before = state_path.read_bytes()
    process, receiver = _start_spawn_process(
        _crash_transition_at_filesystem_boundary,
        (
            config.control_root,
            config.locator_root,
            "journal-create",
            "abort",
            "abort-incomplete-journal",
            "synthetic-no-effects-incomplete",
        ),
    )
    _crash_child_result(process, receiver, 81)
    assert state_path.read_bytes() == before
    assert pending_path.is_file()
    assert pending_path.read_bytes() == b""
    with pytest.raises(InstanceAdmissionError):
        inspect_instance_admission(config)
    with pytest.raises(InstanceAdmissionError):
        resume_authority_transition(
            config, operation_id="abort-incomplete-journal", approval_ref="approved"
        )
    assert state_path.read_bytes() == before
    assert pending_path.read_bytes() == b""


def test_copying_database_or_prior_instance_authority_cannot_adopt_as_new_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "source").mkdir(mode=0o700)
    source_config, source_database = _prepare_and_admit(tmp_path / "source", monkeypatch)
    fence_production_instance(source_config, operation_id="fence-source", approval_ref="approved")
    source_profile = source_database.parent.parent.parent
    source_database_bytes = source_database.read_bytes()
    source_state_bytes = (source_config.control_root / STATE_FILENAME).read_bytes()
    source_control = source_config.control_root
    source_layout = source_config.layout
    source_config.close()

    database_only_root = tmp_path / "database-only-copy"
    database_only_root.mkdir(mode=0o700)
    database_only = synthetic_config(
        database_only_root,
        monkeypatch,
        instance_id="synthetic-instance-b",
    )
    database_only_path = (
        profile_root(database_only.locator_root, _PROFILE_ID, database_only.layout)
        / "runtime/database/finance.db"
    )
    database_only_path.write_bytes(source_database_bytes)
    copied_db_bytes = database_only_path.read_bytes()
    with pytest.raises((StagingDatabaseError, ProfilePathError)):
        prepare_production_profile(
            database_only,
            operation_id="prepare-database-copy",
            approval_ref="approved",
        )
    with pytest.raises(InstanceAdmissionError):
        inspect_instance_admission(database_only)
    assert database_only_path.read_bytes() == copied_db_bytes
    first_state_path = database_only.control_root / STATE_FILENAME
    assert json.loads(first_state_path.read_bytes())["instances"] == {}
    assert not (database_only.control_root / PENDING_FILENAME).exists()
    database_only.close()

    profile_copy_root = tmp_path / "whole-profile-copy"
    profile_copy_root.mkdir(mode=0o700)
    profile_copy = synthetic_config(
        profile_copy_root,
        monkeypatch,
        instance_id="synthetic-instance-copy-b",
    )
    copied_profile = profile_root(profile_copy.locator_root, _PROFILE_ID, profile_copy.layout)
    shutil.rmtree(copied_profile)
    shutil.copytree(source_profile, copied_profile)
    shutil.copytree(source_control, profile_copy.control_root, dirs_exist_ok=True)
    copied_db = copied_profile / "runtime/database/finance.db"
    copied_control_state = (profile_copy.control_root / STATE_FILENAME).read_bytes()
    copied_db_before = copied_db.read_bytes()

    with pytest.raises(InstanceAdmissionError):
        inspect_instance_admission(profile_copy)
    with pytest.raises((StagingDatabaseError, ProfilePathError)):
        prepare_production_profile(
            profile_copy,
            operation_id="prepare-whole-profile-copy",
            approval_ref="approved",
        )
    assert copied_db.read_bytes() == copied_db_before
    assert (profile_copy.control_root / STATE_FILENAME).read_bytes() == copied_control_state
    assert not (profile_copy.control_root / PENDING_FILENAME).exists()
    profile_copy.close()

    # The source remains its own permanently fenced decision and files; copied
    # bytes never make the fresh B binding an alias for A.
    assert source_database.read_bytes() == source_database_bytes
    assert (source_control / STATE_FILENAME).read_bytes() == source_state_bytes
    assert source_layout == profile_copy.layout


def test_moved_symlinked_or_hardlinked_profile_database_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _prepare_and_admit(tmp_path, monkeypatch)
    profile = database.parent.parent.parent
    original_bytes = database.read_bytes()

    moved_profile = profile.with_name("synthetic-moved-away")
    profile.rename(moved_profile)
    try:
        with pytest.raises((InstanceAdmissionError, ProfilePathError, OSError)):
            with acquire_production_owner(config):
                pytest.fail("a moved production profile issued an owner")
    finally:
        moved_profile.rename(profile)
    assert database.read_bytes() == original_bytes

    saved_database = database.with_name("finance.db.saved")
    database.rename(saved_database)
    database.symlink_to(saved_database)
    try:
        with pytest.raises((StagingDatabaseError, ProfilePathError, OSError)):
            with acquire_production_owner(config):
                pytest.fail("a symlinked production database issued an owner")
    finally:
        database.unlink()
        saved_database.rename(database)
    assert database.read_bytes() == original_bytes

    alias = database.with_name("finance.db.hardlink-alias")
    os.link(database, alias)
    try:
        with pytest.raises((StagingDatabaseError, ProfilePathError, OSError)):
            with acquire_production_owner(config):
                pytest.fail("a multiply linked production database issued an owner")
    finally:
        alias.unlink()
    assert database.read_bytes() == original_bytes
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 60


@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX ACL xattrs are host-specific")
def test_linux_posix_acl_drift_fails_closed_even_when_mode_bits_stay_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _prepare_and_admit(tmp_path, monkeypatch)
    named_user = os.getuid() + 100_000
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, subject)
        for tag, permissions, subject in (
            (0x01, 0x06, 0xFFFFFFFF),  # ACL_USER_OBJ
            (0x02, 0x07, named_user),  # ACL_USER
            (0x04, 0x00, 0xFFFFFFFF),  # ACL_GROUP_OBJ
            (0x10, 0x00, 0xFFFFFFFF),  # ACL_MASK
            (0x20, 0x00, 0xFFFFFFFF),  # ACL_OTHER
        )
    )
    try:
        os.setxattr(database, "system.posix_acl_access", acl)
    except OSError as exc:
        raise AssertionError(
            "supported Linux validation host must permit synthetic POSIX ACL creation"
        ) from exc
    try:
        assert os.getxattr(database, "system.posix_acl_access") == acl
        assert database.stat().st_mode & 0o777 == 0o600
        with pytest.raises((StagingDatabaseError, ProfilePathError)):
            with acquire_production_owner(config):
                pytest.fail("a database with a POSIX ACL issued a production owner")
    finally:
        os.removexattr(database, "system.posix_acl_access")
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_process_exit_rolls_back_open_sqlite_transaction_and_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _prepare_and_admit(tmp_path, monkeypatch)
    process, receiver = _start_spawn_process(
        _crash_uncommitted_write, (config.control_root, config.locator_root)
    )
    assert receiver.poll(10)
    assert receiver.recv() == ("transaction-open", None)
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("crash writer did not exit")
    receiver.close()
    assert process.exitcode == 73

    # The process death, not a context-manager cleanup path, releases the lease.
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("lifecycle_crash_probe",),
            ).fetchone()
            assert row is None
    assert database.is_file()


@pytest.mark.skipif("spawn" not in mp.get_all_start_methods(), reason="requires spawn")
def test_uncertain_close_retains_connection_and_owner_lease_until_process_exit(
    tmp_path: Path,
) -> None:
    process, receiver = _start_spawn_process(_uncertain_close_worker, (tmp_path,))
    result = _receive(receiver, process)
    assert result == ("retained", True, True, True, True, True)
    assert process.exitcode == 0


def test_maintenance_hold_denies_operations_until_bound_safe_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_safe_aborts: set[tuple[str, str, str]] = set()
    verifier_calls: list[tuple[str, str, str]] = []

    def verifier(reference: str, action: str, binding: str) -> bool:
        call = (reference, action, binding)
        verifier_calls.append(call)
        if action == "safe_abort":
            return call in allowed_safe_aborts
        return reference == "approved"

    config = synthetic_config(tmp_path, monkeypatch, verifier=verifier)
    profile = prepare_production_profile(config, operation_id="prepare-1", approval_ref="approved")
    database = profile.live_database
    profile.close()
    admit_production_instance(config, operation_id="admit-1", approval_ref="approved")

    with acquire_production_owner(config) as owner:
        hold = enter_maintenance_hold(config, operation_id="hold-1", approval_ref="approved")
        original_hold = inspect_maintenance_hold(config)
        assert original_hold is not None
        assert original_hold["operation_id"] == "hold-1"
        assert hold["maintenance"] == original_hold

        with pytest.raises(InstanceAdmissionError):
            with production_write_operation(owner):
                pytest.fail("maintenance hold issued a writable connection")
        with pytest.raises(InstanceAdmissionError):
            with production_read_operation(owner):
                pytest.fail("maintenance hold issued a readable connection")

        with pytest.raises(InstanceAdmissionError):
            abort_maintenance_hold(
                config,
                operation_id="abort-1",
                approval_ref="safe-abort",
                evidence_ref="evidence-with-effects",
            )
        rejected_binding = verifier_calls[-1][2]
        assert inspect_maintenance_hold(config) == original_hold

        with pytest.raises(InstanceAdmissionError):
            abort_maintenance_hold(
                config,
                operation_id="abort-1",
                approval_ref="safe-abort",
                evidence_ref="evidence-no-effects-v1",
            )
        safe_binding = verifier_calls[-1][2]
        assert len(safe_binding) == 64
        assert safe_binding != rejected_binding
        assert inspect_maintenance_hold(config) == original_hold

        # The trusted verifier authorizes the exact digest it observed for the
        # original hold and no-effects evidence. A changed evidence ref has a
        # different binding and cannot reuse that approval.
        allowed_safe_aborts.add(("safe-abort", "safe_abort", safe_binding))
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO transactions(public_id, intent, intent_type, transaction_date) "
                "VALUES (?, ?, ?, ?)",
                ("synthetic-baseline-drift", "synthetic baseline probe", "Manual", "2026-01-01"),
            )
        with pytest.raises(InstanceAdmissionError, match="baseline changed"):
            abort_maintenance_hold(
                config,
                operation_id="abort-1",
                approval_ref="safe-abort",
                evidence_ref="evidence-no-effects-v1",
            )
        assert inspect_maintenance_hold(config) == original_hold
        assert not any(
            entry["operation_id"] == "abort-1"
            for entry in json.loads((config.control_root / STATE_FILENAME).read_bytes())["history"]
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                "DELETE FROM transactions WHERE public_id=?", ("synthetic-baseline-drift",)
            )
        resumed = abort_maintenance_hold(
            config,
            operation_id="abort-1",
            approval_ref="safe-abort",
            evidence_ref="evidence-no-effects-v1",
        )
        assert resumed["maintenance"] is None
        assert inspect_maintenance_hold(config) is None
        with production_write_operation(owner) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.skipif("fork" not in mp.get_all_start_methods(), reason="requires POSIX fork")
def test_forked_or_caller_created_connection_is_rejected_before_sql_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _prepare_and_admit(tmp_path, monkeypatch)
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("CREATE TABLE lifecycle_fork_probe (value TEXT NOT NULL)")
            conn.commit()
            raw = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                with pytest.raises(StagingDatabaseError):
                    require_finance_write_connection(raw)
            finally:
                raw.close()

            process, receiver = _start_fork_process(_forked_connection_attempt, (conn, owner))
            result = _receive(receiver, process)
            assert result[0][0] == "refused"
            assert result[1][0] == "refused"
            assert process.exitcode == 0
            process, receiver = _start_spawn_process(
                _owner_attempt, (config.control_root, config.locator_root)
            )
            assert _receive(receiver, process)[0] == "busy"
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 60
            assert conn.execute("SELECT COUNT(*) FROM lifecycle_fork_probe").fetchone()[0] == 0

        with pytest.raises(StagingDatabaseError):
            require_finance_write_connection(conn)
