"""Real-process checks for the fixed delegated-cut worker boundary."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from finance_core.managed_cut_protocol import (
    ManagedCutProtocolError,
    canonical_decimal,
    validate_request,
)
from finance_core.managed_staging_profile import (
    _managed_staging_connection,
    bootstrap_registered_staging,
)
from finance_core.profile_paths import MANAGED_STAGING_FILENAME, ManagedStagingProfile
from tests.test_managed_staging_profile import _blank_profile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_LIMITS = {
    "max_core_db_bytes": 128 * 1024 * 1024,
    "max_stage_bytes": 192 * 1024 * 1024,
    "min_free_bytes": 1,
    "backup_pages_per_step": 1,
}


@pytest.fixture()
def synthetic_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path, ManagedStagingProfile]]:
    support, profile_root, blank = _blank_profile(
        tmp_path, monkeypatch, profile_id="cut-worker"
    )
    managed = bootstrap_registered_staging(blank)
    try:
        yield support, profile_root, managed
    finally:
        managed.close()
        blank.close()


def _request(
    profile_root: Path,
    cut_id: str,
    *,
    operation: str = "core_snapshot",
) -> dict[str, object]:
    registration = (profile_root / MANAGED_STAGING_FILENAME).read_bytes()
    registration_value = json.loads(registration)
    limits_bytes = json.dumps(_LIMITS, sort_keys=True, separators=(",", ":")).encode()
    return {
        "version": "delegated-cut-worker-v1",
        "cut_id": cut_id,
        "worker_id": uuid.uuid4().hex,
        "profile_id": "cut-worker",
        "registration_sha256": hashlib.sha256(registration).hexdigest(),
        "artifact_sha256": "a" * 64,
        "schema_sha256": registration_value["migration_contract_sha256"],
        "limits_sha256": hashlib.sha256(limits_bytes).hexdigest(),
        "remaining_ms": 20_000,
        "limits": _LIMITS,
        "operation": operation,
    }


class _WorkerHarness(NamedTuple):
    process: subprocess.Popen[bytes]
    control: socket.socket
    stage: Path
    gate_fd: int
    profile_fd: int
    stage_fd: int
    child_fds: tuple[int, ...]


_CHILD_BOOTSTRAP = r"""
import os,runpy,sys,time
sources=[int(value) for value in sys.argv[1:5]]
for source,target in zip(sources,(3,4,5,6)):
    os.dup2(source,target,inheritable=True)
for source in sources:
    if source not in (3,4,5,6): os.close(source)
sys.path.insert(0,os.environ['S2B_SOURCE_ROOT'])
code=1
try:
    runpy.run_module('finance_core.managed_cut_worker',run_name='__main__',alter_sys=True)
except SystemExit as exc:
    code=exc.code if type(exc.code) is int else 1
if code == 0:
    delay=int(os.environ.get('S2B_HOLD_AFTER_TERMINAL_MS','0'))
    if delay: time.sleep(delay/1000)
raise SystemExit(code)
"""

_PARENT_DEATH_HELPER = r"""
import json
import os
import signal
import sys
import time
from finance_core.profile_paths import validate_registered_staging_profile
from tests.test_managed_cut_worker import _read_frame, _request_line, _start_worker

support = sys.argv[1]
managed = validate_registered_staging_profile(support, 'cut-worker')
try:
    harness, request = _start_worker(support, managed.profile, managed)
    harness.control.sendall(_request_line(request))
    if _read_frame(harness.control)['type'] != 'ready':
        raise RuntimeError('worker did not become ready')
    go = {
        'version': 'delegated-cut-worker-v1',
        'type': 'go',
        'cut_id': request['cut_id'],
        'worker_id': request['worker_id'],
    }
    harness.control.sendall(_request_line(go))
    output = harness.stage / 'core.sqlite'
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if output.exists() and output.stat().st_size >= 4096:
            if harness.process.poll() is not None:
                raise RuntimeError('worker closed before pause')
            os.kill(harness.process.pid, signal.SIGSTOP)
            print(json.dumps({'worker_pid': harness.process.pid}), flush=True)
            while True:
                time.sleep(1)
        if harness.process.poll() is not None:
            raise RuntimeError('worker exited before output')
        time.sleep(0.001)
    raise RuntimeError('worker did not enter output backup')
finally:
    managed.close()
"""


def _start_worker(
    support: Path,
    profile_root: Path,
    managed: ManagedStagingProfile,
    *,
    cut_id: str | None = None,
    hold_after_terminal_ms: int = 0,
) -> tuple[_WorkerHarness, dict[str, object]]:
    selected_cut = cut_id or uuid.uuid4().hex
    stage = managed.work / f"core-cut-{selected_cut}"
    stage.mkdir(mode=0o700)
    gate_path = profile_root / ".profile-gate.v1.lock"
    gate_fd = os.open(gate_path, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    profile_fd = os.open(profile_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parent_control, child_control = socket.socketpair()
    sources = (child_control.fileno(), gate_fd, profile_fd, stage_fd)
    child_fds = tuple(fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in sources)
    environment = {
        **os.environ,
        "FINANCE_CUT_APPLICATION_SUPPORT": str(support),
        "FINANCE_CUT_PROFILE_ID": "cut-worker",
        "FINANCE_CUT_STAGE_PATH": str(stage),
        "FINANCE_CUT_ARTIFACT_SHA256": "a" * 64,
        "S2B_SOURCE_ROOT": str(_REPOSITORY_ROOT),
        "S2B_HOLD_AFTER_TERMINAL_MS": str(hold_after_terminal_ms),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_BOOTSTRAP, *(str(fd) for fd in child_fds)],
        cwd=_REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=child_fds,
    )
    child_control.close()
    for fd in child_fds:
        os.close(fd)
    return _WorkerHarness(process, parent_control, stage, gate_fd, profile_fd, stage_fd,
                          child_fds), _request(profile_root, selected_cut)


def _read_frame(control: socket.socket, timeout: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    payload = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([control], [], [], deadline - time.monotonic())
        if not ready:
            break
        chunk = control.recv(1)
        if not chunk:
            raise AssertionError("worker closed the control channel before a terminal frame")
        if chunk == b"\n":
            return json.loads(payload.decode("utf-8"))
        payload.extend(chunk)
    raise AssertionError("worker did not produce a bounded control frame")


def _close_harness(harness: _WorkerHarness) -> None:
    if harness.process.poll() is None:
        harness.process.kill()
        harness.process.wait(timeout=5)
    harness.control.close()
    for fd in (harness.gate_fd, harness.profile_fd, harness.stage_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def _request_line(frame: dict[str, object]) -> bytes:
    return json.dumps(frame, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _assert_shared_lock_contends(profile_root: Path) -> bool:
    fd = os.open(profile_root / ".profile-gate.v1.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def _seed_large_synthetic_rows(managed: ManagedStagingProfile, *, count: int = 768) -> None:
    with _managed_staging_connection(managed, purpose="reopen") as connection:
        connection.executemany(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at) "
            "VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z')",
            ((f"synthetic-cut-payload-{index}", "synthetic-cut-payload" * 2048)
             for index in range(count)),
        )
        connection.commit()


def test_pathless_request_and_native_identity_precision() -> None:
    request: dict[str, object] = {
        "version": "delegated-cut-worker-v1",
        "cut_id": "1" * 32,
        "worker_id": "2" * 32,
        "profile_id": "cut-worker",
        "registration_sha256": "3" * 64,
        "artifact_sha256": "4" * 64,
        "schema_sha256": "5" * 64,
        "limits_sha256": "6" * 64,
        "remaining_ms": 10_000,
        "limits": _LIMITS,
        "operation": "core_snapshot",
    }
    encoded_limits = json.dumps(_LIMITS, sort_keys=True, separators=(",", ":")).encode()
    request["limits_sha256"] = hashlib.sha256(encoded_limits).hexdigest()
    validated = validate_request(request, expected_operation="core_snapshot")
    assert validated.operation == "core_snapshot"

    for forbidden in ("path", "executable", "provider", "descriptor"):
        with pytest.raises(ManagedCutProtocolError, match="Invalid fixed cut frame"):
            validate_request(
                {**request, forbidden: "synthetic-value"},
                expected_operation="core_snapshot",
            )

    identity = 2**53 + 1
    assert canonical_decimal(str(identity)) == identity
    with pytest.raises(ManagedCutProtocolError, match="canonical decimal"):
        canonical_decimal(identity)
    with pytest.raises(ManagedCutProtocolError, match="canonical decimal"):
        canonical_decimal(f"0{identity}")
    with pytest.raises(ManagedCutProtocolError, match="native range"):
        canonical_decimal(str(2**64))


def test_real_worker_rejects_missing_wrong_and_replayed_delegation(
    synthetic_profile: tuple[Path, Path, ManagedStagingProfile],
) -> None:
    support, profile_root, managed = synthetic_profile

    # A closed FD3 channel is a real worker refusal before any SQLite file is staged.
    missing, _ = _start_worker(support, profile_root, managed)
    try:
        missing.control.shutdown(socket.SHUT_WR)
        assert _read_frame(missing.control)["type"] == "failed"
        assert missing.process.wait(timeout=5) == 2
        assert not (missing.stage / "core.sqlite").exists()
    finally:
        _close_harness(missing)

    # A regular shared-writer version cannot consume the delegated-cut entry.
    wrong, wrong_request = _start_worker(support, profile_root, managed)
    try:
        wrong_request["version"] = "shared-writer-v1"
        wrong.control.sendall(_request_line(wrong_request))
        assert _read_frame(wrong.control)["type"] == "failed"
        assert wrong.process.wait(timeout=5) == 2
        assert not (wrong.stage / "core.sqlite").exists()
    finally:
        _close_harness(wrong)

    # A second initial request is not a GO frame and cannot replay the one-use session.
    replayed, replay_request = _start_worker(support, profile_root, managed)
    try:
        replayed.control.sendall(_request_line(replay_request) + _request_line(replay_request))
        assert _read_frame(replayed.control)["type"] == "ready"
        assert _read_frame(replayed.control)["type"] == "failed"
        assert replayed.process.wait(timeout=5) == 2
        assert not (replayed.stage / "core.sqlite").exists()
    finally:
        _close_harness(replayed)


def test_fd3_eof_during_backup_aborts_real_worker_and_child_holds_ex_until_close(
    synthetic_profile: tuple[Path, Path, ManagedStagingProfile],
) -> None:
    support, profile_root, managed = synthetic_profile
    _seed_large_synthetic_rows(managed)

    harness, request = _start_worker(support, profile_root, managed)
    try:
        harness.control.sendall(_request_line(request))
        assert _read_frame(harness.control)["type"] == "ready"
        harness.control.sendall(_request_line({
            "version": "delegated-cut-worker-v1",
            "type": "go",
            "cut_id": request["cut_id"],
            "worker_id": request["worker_id"],
        }))

        output = harness.stage / "core.sqlite"
        deadline = time.monotonic() + 15.0
        while not output.exists() and time.monotonic() < deadline:
            if harness.process.poll() is not None:
                terminal = _read_frame(harness.control)
                stderr = harness.process.stderr.read().decode("utf-8", errors="replace")
                pytest.fail(
                    "worker exited before backup output appeared: "
                    f"terminal={terminal!r}, stderr={stderr!r}"
                )
            time.sleep(0.002)
        assert output.exists(), "worker never entered disk snapshot creation"
        assert harness.process.poll() is None, (
            "worker completed the large synthetic backup too early"
        )

        # Freeze the actual worker in the backup loop so the observer can prove
        # that FD3 EOF plus parent descriptor closure do not release the child lock.
        os.kill(harness.process.pid, signal.SIGSTOP)
        harness.control.shutdown(socket.SHUT_WR)
        os.close(harness.gate_fd)
        assert _assert_shared_lock_contends(profile_root)

        os.kill(harness.process.pid, signal.SIGCONT)
        assert _read_frame(harness.control)["type"] == "failed"
        assert harness.process.wait(timeout=10) == 2
        assert not _assert_shared_lock_contends(profile_root)
        assert output.exists(), "failed stage evidence should remain for explicit disposition"
    finally:
        _close_harness(harness)


def test_success_frame_precedes_late_child_close_and_keeps_inherited_exclusion(
    synthetic_profile: tuple[Path, Path, ManagedStagingProfile],
) -> None:
    support, profile_root, managed = synthetic_profile
    harness, request = _start_worker(
        support, profile_root, managed, hold_after_terminal_ms=700
    )
    try:
        harness.control.sendall(_request_line(request))
        assert _read_frame(harness.control)["type"] == "ready"
        harness.control.sendall(_request_line({
            "version": "delegated-cut-worker-v1",
            "type": "go",
            "cut_id": request["cut_id"],
            "worker_id": request["worker_id"],
        }))
        terminal = _read_frame(harness.control)
        assert terminal["type"] == "staged"
        assert harness.process.poll() is None, (
            "test wrapper should keep child alive after success frame"
        )

        # The coordinator-side descriptor is gone; the live worker's inherited
        # FD4 must retain exclusion until its actual process close.
        os.close(harness.gate_fd)
        assert _assert_shared_lock_contends(profile_root)
        assert harness.process.wait(timeout=5) == 0
        assert not _assert_shared_lock_contends(profile_root)
    finally:
        _close_harness(harness)


def test_parent_death_keeps_inherited_ex_until_real_worker_close(
    synthetic_profile: tuple[Path, Path, ManagedStagingProfile],
) -> None:
    support, profile_root, managed = synthetic_profile
    _seed_large_synthetic_rows(managed)
    helper = subprocess.Popen(
        [sys.executable, "-c", _PARENT_DEATH_HELPER, str(support)],
        cwd=_REPOSITORY_ROOT,
        env={**os.environ, "S2B_SOURCE_ROOT": str(_REPOSITORY_ROOT)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    worker_pid: int | None = None
    try:
        assert helper.stdout is not None
        ready, _, _ = select.select([helper.stdout], [], [], 20.0)
        if not ready:
            helper.kill()
            helper.wait(timeout=5)
            stderr = helper.stderr.read() if helper.stderr is not None else ""
            pytest.fail(f"parent-death helper did not reach paused worker: {stderr}")
        line = helper.stdout.readline()
        if not line:
            stderr = helper.stderr.read() if helper.stderr is not None else ""
            pytest.fail(f"parent-death helper exited before worker pause: {stderr}")
        worker_pid = int(json.loads(line)["worker_pid"])
        assert helper.poll() is None
        assert _assert_shared_lock_contends(profile_root)

        # Abruptly kill the EX-owning coordinator parent while its real worker
        # is paused in backup. FD4 inherited by that worker must retain EX.
        helper.kill()
        assert helper.wait(timeout=5) == -signal.SIGKILL
        assert _assert_shared_lock_contends(profile_root)

        os.kill(worker_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _assert_shared_lock_contends(profile_root):
            time.sleep(0.01)
        assert not _assert_shared_lock_contends(profile_root), (
            "the kernel lock should become available only after the inherited worker "
            "descriptor closes"
        )
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=5)
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
