"""Recovery callbacks and their SQLite statements share a Core-owned lifetime."""

import sqlite3
import subprocess
import sys
from contextvars import Context
from pathlib import Path
from unittest.mock import patch

import pytest

from finance_core.lifecycle_v2 import LifecycleError, recovery
from finance_core.lifecycle_v2 import historical_evidence_v1 as history
from tests.test_lifecycle_v2 import ready
from tests.test_lifecycle_v2_round3_recovery import _cut, _fence, _manual_historical_target
from tests.test_lifecycle_v2_round3_recovery import original as original


def test_detached_opener_finalizes_retained_ordinary_select_cursors(original, tmp_path):
    _, _, service, _ = original
    ready(service)
    cut, path = _cut(service, tmp_path)
    closed = []

    def observe_close(cursor):
        closed.append(cursor)
        sqlite3.Cursor.close(cursor)

    # Both public SQLite read paths can return an unfinished statement.
    with patch.object(history, "_close_cursor_handle", observe_close):
        with history.open_archive_read_connection_v1(
            path, database_sha256=cut.database_sha256
        ) as conn:
            direct = conn.execute("SELECT seq FROM l2_events ORDER BY seq")
            regular = conn.cursor()
            regular.execute("SELECT seq FROM l2_events ORDER BY seq")
            assert direct.fetchone()[0] == regular.fetchone()[0] == 1
            assert history.is_archive_read_connection_v1(conn)

    assert direct in closed and regular in closed
    for cursor in (direct, regular):
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchone()


def test_detached_owner_guards_connection_settings_in_a_separate_context(original, tmp_path):
    _, _, service, _ = original
    cut, path = _cut(service, tmp_path)
    with history.open_archive_read_connection_v1(path, database_sha256=cut.database_sha256) as conn:
        separate = Context()
        assert separate.run(lambda: conn.execute("SELECT 1").fetchone()[0]) == 1
        with pytest.raises(LifecycleError, match="Core owns the archive read transaction"):
            separate.run(setattr, conn, "row_factory", sqlite3.Row)


def test_detached_archive_guards_sqlite_callback_and_configuration_apis(original, tmp_path):
    _, _, service, _ = original
    cut, path = _cut(service, tmp_path)
    with history.open_archive_read_connection_v1(path, database_sha256=cut.database_sha256) as conn:
        limit = sqlite3.SQLITE_LIMIT_LENGTH
        config = sqlite3.SQLITE_DBCONFIG_DQS_DML
        previous_limit = conn.getlimit(limit)
        previous_config = conn.getconfig(config)
        calls = (
            ("create_function", "unused_archive_callback", 0, lambda: 1),
            ("create_aggregate", "unused_archive_aggregate", 1, None),
            ("create_window_function", "unused_archive_window", 1, None),
            ("create_collation", "unused_archive_collation", None),
            ("set_trace_callback", None),
            ("set_progress_handler", None, 0),
            ("enable_load_extension", False),
            ("setconfig", config, previous_config),
            ("setlimit", limit, previous_limit),
            ("interrupt",),
        )
        for method, *arguments in calls:
            with pytest.raises(LifecycleError, match=method):
                Context().run(getattr(conn, method), *arguments)
        assert conn.getlimit(limit) == previous_limit
        assert conn.getconfig(config) == previous_config
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_detached_archive_permits_pragma_reads_but_refuses_pragma_settings(original, tmp_path):
    _, _, service, _ = original
    cut, path = _cut(service, tmp_path)
    with (
        pytest.raises(LifecycleError, match="violated its read-only scope"),
        history.open_archive_read_connection_v1(path, database_sha256=cut.database_sha256) as conn,
    ):
        assert conn.execute("PRAGMA integrity_check(1)").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA table_xinfo(l2_events)").fetchone() is not None
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(f"PRAGMA busy_timeout={timeout}")


def test_detached_audit_rechecks_the_witness_after_the_final_cut(original, tmp_path):
    _, _, service, port = original
    cut, path = _cut(service, tmp_path)
    verify_cut = recovery._verify_cut_connection
    checks = []

    def observe_cut(conn, cut):
        verify_cut(conn, cut)
        checks.append(cut)
        if len(checks) == 2:
            history._archive_transaction_for(conn).violated = True

    with (
        patch.object(recovery, "_verify_cut_connection", observe_cut),
        pytest.raises(LifecycleError, match="violated its read-only scope"),
        history.open_archive_read_connection_v1(path, database_sha256=cut.database_sha256) as conn,
    ):
        with pytest.raises(LifecycleError, match="violated its read-only scope"):
            history.audit_archive_evidence_connection_v1(
                conn,
                port,
                service.binding,
                cut,
                parent_history_sha256=history.historical_generations_sha256_v1(()),
            )
    assert checks == [cut, cut]


def test_port_only_manual_promotion_and_retry_keep_owned_read_until_close(
    original, tmp_path, monkeypatch
):
    old, old_owner, service, port = original
    ready(service)
    cut, path = _cut(service, tmp_path)
    _fence(old, old_owner)
    config, held, options = _manual_historical_target(
        tmp_path, monkeypatch, old, cut, path, "owned-port-read", 1792800000
    )
    options["port"] = port
    original_source = port.source
    original_publish = recovery._publish
    original_close = recovery._close_sqlite_handle
    reads = []
    closed = []
    closed_connections = []

    def read_source(conn, identity):
        assert conn.in_transaction and history.is_archive_read_connection_v1(conn)
        cursor = conn.execute("SELECT seq FROM l2_events ORDER BY seq")
        assert cursor.fetchone()[0] == 1
        reads.append(cursor)
        return original_source(conn, identity)

    def observe_close(cursor):
        closed.append(cursor)
        sqlite3.Cursor.close(cursor)

    def close_connection(conn):
        assert all(cursor in closed for cursor in reads if cursor.connection is conn)
        original_close(conn)
        closed_connections.append(conn)

    def publish(*args, **kwargs):
        assert reads and all(cursor.connection in closed_connections for cursor in reads)
        return original_publish(*args, **kwargs)

    port.source = read_source
    with (
        patch.object(history, "_close_cursor_handle", observe_close),
        patch.object(recovery, "_close_sqlite_handle", close_connection),
        patch.object(recovery, "_publish", publish),
    ):
        assert recovery.promote_manual_single_host_restore_v1(held, **options)["state"] == "ACTIVE"
        initial_reads = len(reads)
        assert recovery.promote_manual_single_host_restore_v1(held, **options)["state"] == "ACTIVE"

    assert len(reads) > initial_reads
    assert all(cursor in closed for cursor in reads)
    assert all(cursor.connection in closed_connections for cursor in reads)
    config.close()


def test_manual_cursor_close_unknown_keeps_pending_and_all_owned_leases(tmp_path):
    # This operation intentionally retains its acquired leases until process exit.
    script = r"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "tests"))
from instance_admission_support_v1 import synthetic_config
from test_lifecycle_v2 import Port, ready
from tests.test_lifecycle_v2_round3_recovery import _manual_historical_target
from finance_core.instance_admission import (
    PENDING_FILENAME, InstanceAdmissionError, acquire_production_owner,
    admit_production_instance, fence_production_instance, prepare_production_profile,
)
from finance_core.instance_admission import authority
from finance_core.lifecycle_v2 import (
    LedgerService, RuntimeBinding, historical_evidence_v1, recovery,
)

class Environment:
    def setenv(self, key, value):
        os.environ[key] = value

root = Path(sys.argv[1])
environment = Environment()
oldroot = root / "old"
oldroot.mkdir(mode=0o700)
config = synthetic_config(oldroot, environment)
prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
admit_production_instance(config, operation_id="admit", approval_ref="approved")
owner = acquire_production_owner(config)
port = Port()
service = LedgerService(
    owner, port, RuntimeBinding(config.instance_id, "old", "user", "chat"),
    clock=lambda: 1792800000,
)
ready(service)
path = root / "sealed.sqlite"
cut = service.seal_snapshot(path)
fence_production_instance(config, operation_id="fence", approval_ref="approved")
owner.close()
target, held, options = _manual_historical_target(
    root, environment, config, cut, path, "new", 1792800000
)
options["port"] = port
original_source = port.source
escaped = []
attempts = []

def read_source(conn, identity):
    if not escaped:
        cursor = conn.execute("SELECT seq FROM l2_events ORDER BY seq")
        assert cursor.fetchone()[0] == 1
        escaped.append(cursor)
    return original_source(conn, identity)

def close(cursor):
    if cursor is escaped[0]:
        attempts.append(cursor)
        raise OSError("synthetic unfinished SQLite statement close unknown")
    sqlite3.Cursor.close(cursor)

import sqlite3
port.source = read_source
with patch.object(historical_evidence_v1, "_close_cursor_handle", close):
    try:
        recovery.promote_manual_single_host_restore_v1(held, **options)
    except InstanceAdmissionError as exc:
        assert "actual close unknown" in str(exc)
    else:
        raise AssertionError("Expected close failure")

assert attempts == escaped
retained_owner, control, profile, gate, connection = recovery._RECOVERY_CLOSE_UNKNOWN[-1]
assert retained_owner._retain and control._retain and profile._retain
assert retained_owner.fd >= 0 and control.fd >= 0
assert connection is escaped[0].connection
gate.assert_valid()
assert (target.control_root / PENDING_FILENAME).is_file()
state = authority._read(target, authority.STATE_FILENAME)
assert state["instances"][target.binding_sha256]["state"] == "RESTORE_HELD"
print("CLOSE_UNKNOWN_RETAINED")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLOSE_UNKNOWN_RETAINED" in result.stdout


@pytest.mark.parametrize("handle", ["cursor", "connection"])
def test_detached_close_unknown_retains_the_archive_and_never_retries(original, tmp_path, handle):
    _, _, service, _ = original
    ready(service)
    cut, path = _cut(service, tmp_path)
    # Retained SQLite resources are released on process exit only.
    script = r"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
from finance_core.lifecycle_v2 import ArchiveCloseUnknownV1, historical_evidence_v1 as history

path = Path(sys.argv[1])
handle = sys.argv[3]
calls = []

def fail_close(resource):
    calls.append(resource)
    raise OSError("synthetic detached close uncertain")

close_hook = "_close_cursor_handle" if handle == "cursor" else "_close_sqlite_handle"
with patch.object(history, close_hook, fail_close):
    try:
        with history.open_archive_read_connection_v1(path, database_sha256=sys.argv[2]) as conn:
            cursor = conn.execute("SELECT seq FROM l2_events ORDER BY seq")
            assert cursor.fetchone()[0] == 1
            if handle == "cursor":
                try:
                    cursor.close()
                except ArchiveCloseUnknownV1 as exc:
                    assert "actual close unknown" in str(exc)
                else:
                    raise AssertionError("Expected cursor close failure")
    except ArchiveCloseUnknownV1 as exc:
        assert "close is uncertain" in str(exc)
    else:
        raise AssertionError("Expected owner close uncertainty")

assert calls == [cursor if handle == "cursor" else conn]
owned_conn, retained_archive = history._CLOSE_UNKNOWN[-1]
assert owned_conn is conn
if handle == "cursor":
    assert cursor in owned_conn._archive_retained_cursors
assert os.fstat(retained_archive.fileno()).st_ino == path.stat().st_ino
print("DETACHED_CLOSE_UNKNOWN_RETAINED")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), cut.database_sha256, handle],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DETACHED_CLOSE_UNKNOWN_RETAINED" in result.stdout
