"""Real SIGKILL/reap and cold replay proof; storage models are tested separately."""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

import finance_core.synthetic_native_authority.store as store_module
from finance_core.synthetic_native_authority.store import (
    PoisonedError,
    QuarantinedError,
    SyntheticAuthorityStore,
)

HELPERS = Path(__file__).parent / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))
from synthetic_native_authority_worker import (  # noqa: E402
    InMemoryIssuer,
    LeaseCheckingIssuer,
    prepare_root,
)

WORKER = Path(__file__).parent / "helpers" / "synthetic_native_authority_worker.py"


def _line(fd: int, timeout: float = 5.0) -> dict[str, object]:
    end = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], max(0, end - time.monotonic()))
        if not ready:
            break
        char = os.read(fd, 1)
        if not char:
            break
        if char == b"\n":
            return json.loads(data.decode("ascii"))
        data.extend(char)
        if len(data) > 32768:
            raise AssertionError("worker event exceeded private channel bound")
    raise AssertionError("worker event missing or timed out")


def _digest(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def _observe(root: Path) -> dict[str, tuple[int, str] | None]:
    paths = {
        "gate": root / "profile-gate.lock",
        "descriptor": root / "authority" / "capabilities.frame",
        "lifecycle": root / "authority" / "core-lifecycle.lock",
        "registry": root / "authority" / "registry.log",
        "head": root / "authority" / "committed-head.log",
        "receipt": root / "authority" / "enrollment.receipt",
        "main": root / "main.witness",
        "journal0": root / "slots" / "journal-0.slot",
        "journal1": root / "slots" / "journal-1.slot",
        "wal0": root / "slots" / "wal-0.slot",
        "wal1": root / "slots" / "wal-1.slot",
    }
    return {role: _digest(path) if path.exists() else None for role, path in paths.items()}


@pytest.fixture
def modeled_store():
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-modeled-") as tmp:
        parent = Path(tmp)
        parent.chmod(0o700)
        root = prepare_root(parent / "profile")
        issuer = InMemoryIssuer()
        store = SyntheticAuthorityStore.enroll_fresh_test(root, issuer)
        try:
            yield root, issuer, store
        finally:
            try:
                store.close()
            except PoisonedError:
                pass


def _run_kill(
    *,
    mode: str,
    event: str,
    seq: int | None = None,
    phase: str | None = None,
    cursor: int | None = None,
    stage: str | None = None,
    role: str | None = None,
    short_chunk: int | None = None,
    kind: str = "JOURNAL",
    pre_retire_count: int = 0,
) -> dict[str, object]:
    parent = Path(tempfile.mkdtemp(dir="/private/tmp", prefix="fna-grid-"))
    completed = False
    first: dict[str, object] | None = None
    hit: dict[str, object] | None = None
    issued: dict[str, object] | None = None
    stderr = b""
    try:
        parent.chmod(0o700)
        root = parent / "profile"
        root.mkdir(mode=0o700)
        gate = os.open(root / "profile-gate.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(gate)
        event_r, event_w = os.pipe()
        continue_r, continue_w = os.pipe()
        command = [
            sys.executable,
            str(WORKER),
            mode,
            "--root",
            str(root),
            "--event-fd",
            str(event_w),
            "--continue-fd",
            str(continue_r),
            "--target-event",
            event,
            "--kind",
            kind,
            "--pre-retire-count",
            str(pre_retire_count),
        ]
        if seq is not None:
            command += ["--target-seq", str(seq)]
        if phase is not None:
            command += ["--target-phase", phase]
        if cursor is not None:
            command += ["--target-cursor", str(cursor)]
        if stage is not None:
            command += ["--target-stage", stage]
        if role is not None:
            command += ["--target-role", role]
        if short_chunk is not None:
            command += ["--short-chunk", str(short_chunk)]
        process = subprocess.Popen(
            command, pass_fds=(event_w, continue_r), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        os.close(event_w)
        os.close(continue_r)
        try:
            first = _line(event_r)
            assert first["kind"] == ("prepared" if mode == "crash-enrollment" else "ready")
            before = _observe(root) if mode == "crash-allocation" else None
            if mode == "crash-allocation":
                os.write(continue_w, b"x")
            while True:
                message = _line(event_r)
                if message.get("kind") == "issued":
                    assert issued is None and mode == "crash-enrollment"
                    issued = message
                    continue
                hit = message
                break
            assert hit["kind"] == "hit" and hit["event"] == event
            witness = hit["evidence"]
            assert isinstance(witness, dict)
            role_path = {
                "gate": root / "profile-gate.lock",
                "descriptor": root / "authority" / "capabilities.frame",
                "receipt": root / "authority" / "enrollment.receipt",
                "registry": root / "authority" / "registry.log",
                "head": root / "authority" / "committed-head.log",
                "directory:root": root,
                "directory:authority": root / "authority",
                "directory:slots": root / "slots",
                "journal0": root / "slots" / "journal-0.slot",
                "journal1": root / "slots" / "journal-1.slot",
                "wal0": root / "slots" / "wal-0.slot",
                "wal1": root / "slots" / "wal-1.slot",
            }.get(str(witness["role"]))
            if role_path is None:
                assert str(witness["role"]) in {
                    "gate:released",
                    "issuer:add_only",
                    "issuer:readback",
                }
                assert witness["dev"] is None and witness["ino"] is None
                assert witness["live_target_fd"] is False
                descriptor = (root / "authority" / "capabilities.frame").stat()
                assert (witness["descriptor_dev"], witness["descriptor_ino"]) == (
                    descriptor.st_dev,
                    descriptor.st_ino,
                )
            else:
                actual = role_path.stat()
                assert (witness["dev"], witness["ino"]) == (actual.st_dev, actual.st_ino)
            if seq is not None:
                assert witness["seq"] == seq
            if phase is not None:
                assert witness["phase"] == phase
            if cursor is not None:
                assert witness["cursor"] == cursor
            if stage is not None:
                assert witness["stage"] == stage
            if role is not None:
                assert witness["role"] == role
            os.kill(process.pid, signal.SIGKILL)
            _stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == -signal.SIGKILL, stderr.decode("utf-8", "replace")
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
            after = _observe(root)
            lifecycle_created = not (
                mode == "crash-enrollment"
                and event in ("before_first_gate_lock", "after_first_gate_lock")
            )
            assert (after["lifecycle"] is not None) is lifecycle_created
            if event in ("before_first_gate_lock", "after_first_gate_lock"):
                assert after["main"] is None
            else:
                assert after["main"] == (0, hashlib.sha256(b"").hexdigest())
            if mode == "crash-allocation":
                request = json.dumps({"anchor": first["anchor"], "key": first["key"]}).encode(
                    "ascii"
                )
                cold = subprocess.run(
                    [sys.executable, str(WORKER), "reopen", "--root", str(root)],
                    input=request,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                cold_status = (
                    cold.returncode,
                    cold.stdout.decode("utf-8", "replace"),
                    cold.stderr.decode("utf-8", "replace"),
                )
                assert _observe(root) == after, "cold replay mutated preserved evidence"
            else:
                assert "anchor" not in first, "GENESIS kill cannot publish an enrollment anchor"
                if event not in ("before_first_gate_lock", "after_first_gate_lock"):
                    with pytest.raises(QuarantinedError):
                        SyntheticAuthorityStore.enroll_fresh_test(root, InMemoryIssuer())
                    assert _observe(root) == after, "fresh retry altered uncommitted enrollment"
                if issued is None:
                    cold_status = None
                else:
                    request = json.dumps({"anchor": issued["anchor"], "key": first["key"]}).encode(
                        "ascii"
                    )
                    cold = subprocess.run(
                        [sys.executable, str(WORKER), "reopen", "--root", str(root)],
                        input=request,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    cold_status = (
                        cold.returncode,
                        cold.stdout.decode("utf-8", "replace"),
                        cold.stderr.decode("utf-8", "replace"),
                    )
                    assert _observe(root) == after, "cold admission rewrote enrollment evidence"
            completed = True
            return {
                "before": before,
                "after": after,
                "hit": hit,
                "issued": issued is not None,
                "cold": cold_status,
                "pid_reaped": True,
            }
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(event_r)
            os.close(continue_w)
    except BaseException as exc:
        try:
            (parent / "failure.json").write_text(
                json.dumps(
                    {
                        "error": repr(exc),
                        "first_kind": first.get("kind") if first else None,
                        "hit": hit,
                        "worker_stderr": stderr.decode("utf-8", "replace"),
                        "objects": _observe(parent / "profile"),
                    },
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        exc.add_note(f"synthetic crash evidence retained at {parent}")
        raise
    finally:
        if completed:
            shutil.rmtree(parent)


def _assert_cold_pair_outcome(result: dict[str, object]) -> None:
    after = result["after"]
    r_len = after["registry"][0]
    h_len = after["head"][0]
    complete = r_len % 4096 == 0 and h_len % 2048 == 0 and r_len // 4096 == h_len // 2048
    accepted_seq = r_len // 4096
    status, stdout, stderr = result["cold"]
    if complete and accepted_seq in (1, 4, 5, 8, 9, 12):
        assert status == 0, stderr
        assert json.loads(stdout)["seq"] == accepted_seq
    else:
        assert status == 2 and "QuarantinedError" in stderr
    assert result["pid_reaped"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p2_genesis_registry_barrier_kill_stays_unenrolled() -> None:
    result = _run_kill(mode="crash-enrollment", event="after_registry_barrier", seq=1)
    after = result["after"]
    assert after["registry"][0] == 4096
    assert after["head"][0] == 0
    assert after["receipt"][0] == 0
    assert result["cold"] is None and result["pid_reaped"] is True


_COMMIT_BOUNDARIES = (
    "before_registry_append",
    "after_registry_append",
    "before_registry_barrier",
    "after_registry_barrier",
    "before_head_append",
    "after_head_append",
    "before_head_barrier",
    "after_head_barrier",
    "before_namespace_postcheck",
    "after_namespace_postcheck",
    "before_publish",
    "after_publish",
    "before_ack",
)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("event", _COMMIT_BOUNDARIES)
def test_p2_genesis_all_commit_boundaries_are_unenrolled(event: str) -> None:
    result = _run_kill(
        mode="crash-enrollment",
        event=event,
        seq=1 if event not in ("after_publish", "before_ack") else None,
        phase="GENESIS" if event in ("after_publish", "before_ack") else None,
    )
    after = result["after"]
    assert after["receipt"] == (0, hashlib.sha256(b"").hexdigest())
    assert after["registry"] is not None and after["head"] is not None
    assert result["cold"] is None and result["pid_reaped"] is True


def _assert_p6_enrollment_cold_outcome(result: dict[str, object]) -> None:
    after = result["after"]
    assert result["pid_reaped"] is True
    if not result["issued"]:
        assert result["cold"] is None, "no external item may supply a cold anchor"
        return
    status, stdout, stderr = result["cold"]
    receipt = after["receipt"]
    if receipt is not None and receipt[0] == 2048:
        # A full, authenticated receipt is visible after process kill even if
        # its fsync or directory barrier was not reached. This is not a
        # power-loss durability assertion.
        assert status == 0, stderr
        assert json.loads(stdout)["seq"] == 1
    else:
        assert status == 2 and "QuarantinedError" in stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/lease proof is Darwin only")
@pytest.mark.parametrize(
    "stage",
    (
        pytest.param("add_only", id="P1-PORT-001-add-only"),
        pytest.param("readback", id="P1-PORT-002-readback"),
    ),
)
def test_p1_fake_issuer_refuses_add_and_readback_while_core_lifecycle_is_held(
    stage: str,
) -> None:
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fna-issuer-lock-") as location:
        parent = Path(location)
        parent.chmod(0o700)
        root = prepare_root(parent / "profile")
        seed_issuer = InMemoryIssuer()
        store = SyntheticAuthorityStore.enroll_fresh_test(root, seed_issuer)
        assert seed_issuer.grant is not None
        anchor, key = seed_issuer.readback(seed_issuer.grant.enrollment_id)
        store.close()

        issuer = LeaseCheckingIssuer(root)
        if stage == "readback":
            InMemoryIssuer.add_only(issuer, anchor, key)

        gate_path = root / "profile-gate.lock"
        lifecycle_path = root / "authority" / "core-lifecycle.lock"
        gate_fd = os.open(gate_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        lifecycle_fd = os.open(lifecycle_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            fcntl.flock(lifecycle_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Prove the gate is free while the lifecycle lease remains held.
            fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(gate_fd, fcntl.LOCK_UN)

            with pytest.raises(BlockingIOError):
                if stage == "add_only":
                    issuer.add_only(anchor, key)
                else:
                    issuer.readback(anchor.enrollment_id)

            if stage == "add_only":
                assert issuer._item is None
            else:
                assert issuer._item == (anchor, key)
            # The failed external-port probe must not leave the gate held.
            fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(gate_fd, fcntl.LOCK_UN)
        finally:
            fcntl.flock(lifecycle_fd, fcntl.LOCK_UN)
            os.close(lifecycle_fd)
            os.close(gate_fd)


_GATE_AND_ISSUER_BOUNDARIES = (
    "before_first_gate_lock",
    "after_first_gate_lock",
    "before_first_gate_release",
    "after_first_gate_release",
    "before_external_add",
    "after_external_add",
    "before_external_readback",
    "after_external_readback",
    "before_second_gate_lock",
    "after_second_gate_lock",
)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("event", _GATE_AND_ISSUER_BOUNDARIES)
def test_p6_gate_and_fake_external_port_kill_has_exact_cold_outcome(event: str) -> None:
    result = _run_kill(mode="crash-enrollment", event=event)
    after = result["after"]
    assert after["receipt"] in (None, (0, hashlib.sha256(b"").hexdigest()))
    if event in ("before_first_gate_lock", "after_first_gate_lock"):
        assert all(
            after[name] is None
            for name in ("descriptor", "lifecycle", "registry", "head", "receipt", "main")
        )
    _assert_p6_enrollment_cold_outcome(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize(
    "stage,role",
    (
        ("pre_external", "directory:authority"),
        ("pre_external", "directory:slots"),
        ("pre_external", "directory:root"),
        ("receipt", "directory:authority"),
    ),
)
@pytest.mark.parametrize("event", ("before_directory_barrier", "after_directory_barrier"))
def test_p6_directory_barrier_kill_preserves_exact_stage_and_outcome(
    event: str, stage: str, role: str
) -> None:
    result = _run_kill(mode="crash-enrollment", event=event, stage=stage, role=role)
    _assert_p6_enrollment_cold_outcome(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize(
    "event,cursor",
    (
        *(("before_descriptor_fragment", cursor) for cursor in (0, 4096, 8192, 12288)),
        *(("after_descriptor_fragment", cursor) for cursor in (4096, 8192, 12288, 16384)),
        *(("before_receipt_fragment", cursor) for cursor in (0, 1024)),
        *(("after_receipt_fragment", cursor) for cursor in (1024, 2048)),
    ),
)
def test_p6_descriptor_and_receipt_fragment_kill_has_exact_cold_outcome(
    event: str, cursor: int
) -> None:
    result = _run_kill(
        mode="crash-enrollment",
        event=event,
        cursor=cursor,
        short_chunk=4096 if "descriptor" in event else 1024,
    )
    _assert_p6_enrollment_cold_outcome(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize(
    "event",
    (
        "before_descriptor_barrier",
        "after_descriptor_barrier",
        "before_receipt_barrier",
        "after_receipt_barrier",
    ),
)
def test_p6_descriptor_and_receipt_file_barrier_kill_has_exact_cold_outcome(event: str) -> None:
    result = _run_kill(mode="crash-enrollment", event=event)
    _assert_p6_enrollment_cold_outcome(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("phase,seq", (("RESET_INTENT", 2), ("RESET_DONE", 3), ("ACTIVE", 4)))
@pytest.mark.parametrize("event", _COMMIT_BOUNDARIES)
def test_p2_allocation_commit_kill_replays_exact_complete_pair(
    event: str, phase: str, seq: int
) -> None:
    result = _run_kill(
        mode="crash-allocation",
        event=event,
        seq=seq if event not in ("after_publish", "before_ack") else None,
        phase=phase if event in ("before_publish", "after_publish", "before_ack") else None,
    )
    _assert_cold_pair_outcome(result)


_FRAGMENT_BOUNDARIES = tuple(
    (event, cursor)
    for role, size in (("registry", 4096), ("head", 2048))
    for event, positions in (
        (f"before_{role}_fragment", range(0, size, 1024)),
        (f"after_{role}_fragment", range(1024, size + 1, 1024)),
    )
    for cursor in positions
)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("event,cursor", _FRAGMENT_BOUNDARIES)
def test_p2_genesis_each_short_write_fragment_retains_unenrolled_evidence(
    event: str, cursor: int
) -> None:
    result = _run_kill(mode="crash-enrollment", event=event, seq=1, cursor=cursor, short_chunk=1024)
    after = result["after"]
    assert after["receipt"] == (0, hashlib.sha256(b"").hexdigest())
    assert result["pid_reaped"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("phase,seq", (("RESET_INTENT", 2), ("RESET_DONE", 3), ("ACTIVE", 4)))
@pytest.mark.parametrize("event,cursor", _FRAGMENT_BOUNDARIES)
def test_p2_allocation_each_short_write_fragment_has_exact_cold_outcome(
    event: str, cursor: int, phase: str, seq: int
) -> None:
    result = _run_kill(
        mode="crash-allocation", event=event, seq=seq, cursor=cursor, short_chunk=1024
    )
    _assert_cold_pair_outcome(result)


_SLOT_BOUNDARIES = (
    "before_slot_admission",
    "after_slot_admission",
    "before_slot_reset",
    "after_slot_reset",
    "before_slot_barrier",
    "after_slot_barrier",
)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize(
    "kind,pre_retire_count",
    (
        ("JOURNAL", 0),
        ("JOURNAL", 1),
        ("JOURNAL", 2),
        ("WAL", 0),
        ("WAL", 2),
    ),
)
@pytest.mark.parametrize("event", _SLOT_BOUNDARIES)
def test_p3_slot_admission_reset_and_barrier_kill_quarantines_pending(
    event: str, kind: str, pre_retire_count: int
) -> None:
    result = _run_kill(
        mode="crash-allocation",
        event=event,
        phase="RESET_INTENT",
        kind=kind,
        pre_retire_count=pre_retire_count,
    )
    before, after = result["before"], result["after"]
    role = result["hit"]["evidence"]["role"]
    assert role == f"{'journal' if kind == 'JOURNAL' else 'wal'}{pre_retire_count % 2}"
    assert after["registry"][0] == (2 + 4 * pre_retire_count) * 4096
    assert after["head"][0] == (2 + 4 * pre_retire_count) * 2048
    if event in ("before_slot_admission", "after_slot_admission", "before_slot_reset"):
        assert after[role] == before[role]
    else:
        assert after[role][0] == 0
        if pre_retire_count == 2:
            assert before[role][0] > 0, "actual retired-slot bytes must be reset"
    for other in ("journal0", "journal1", "wal0", "wal1"):
        if other != role:
            assert after[other] == before[other]
    _assert_cold_pair_outcome(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize(
    "kind,pre_retire_count",
    (
        ("JOURNAL", 0),
        ("JOURNAL", 1),
        ("JOURNAL", 2),
        ("WAL", 0),
        ("WAL", 1),
        ("WAL", 2),
    ),
)
def test_p3_active_ack_lost_reopens_same_generation_without_reset(
    kind: str, pre_retire_count: int
) -> None:
    result = _run_kill(
        mode="crash-allocation",
        event="before_ack",
        phase="ACTIVE",
        kind=kind,
        pre_retire_count=pre_retire_count,
    )
    before, after = result["before"], result["after"]
    selected = f"{'journal' if kind == 'JOURNAL' else 'wal'}{pre_retire_count % 2}"
    assert after[selected][0] == 0
    if pre_retire_count == 2:
        assert before[selected][0] > 0
    _assert_cold_pair_outcome(result)


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p2_modeled_zero_write_poison_before_registry_effect(modeled_store) -> None:
    root, issuer, store = modeled_store
    before = _observe(root)
    registry_ino = (root / "authority" / "registry.log").stat().st_ino
    original = os.pwrite

    def zero_registry(fd: int, data: bytes, offset: int) -> int:
        if os.fstat(fd).st_ino == registry_ino:
            return 0
        return original(fd, data, offset)

    with patch.object(store_module.os, "pwrite", side_effect=zero_registry):
        with pytest.raises(PoisonedError):
            store.allocate("JOURNAL", op_id=f"{71:032x}")
    assert _observe(root) == before
    with pytest.raises(PoisonedError):
        store.close()
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    reopened = SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
    assert reopened.snapshot().seq == 1
    reopened.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p2_modeled_registry_fsync_error_never_attempts_head(modeled_store) -> None:
    root, issuer, store = modeled_store
    before = _observe(root)
    registry_ino = (root / "authority" / "registry.log").stat().st_ino
    original = os.fsync
    observed: list[str] = []
    store._hook = lambda event, evidence: observed.append(event)

    def fail_registry(fd: int) -> None:
        if os.fstat(fd).st_ino == registry_ino:
            raise OSError(errno.ENOSPC, "modeled registry barrier failure")
        original(fd)

    with patch.object(store_module.os, "fsync", side_effect=fail_registry):
        with pytest.raises(OSError, match="modeled registry barrier failure"):
            store.allocate("JOURNAL", op_id=f"{72:032x}")
    after = _observe(root)
    assert after["registry"][0] == before["registry"][0] + 4096
    assert after["head"] == before["head"]
    assert after["journal0"] == before["journal0"]
    assert "before_registry_barrier" in observed
    assert "before_head_append" not in observed
    with pytest.raises(PoisonedError):
        store.close()
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    with pytest.raises(QuarantinedError):
        SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
    assert _observe(root) == after


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p2_modeled_positive_short_writes_finish_within_live_call(modeled_store) -> None:
    root, issuer, store = modeled_store
    original = os.pwrite

    def short_write(fd: int, data: bytes, offset: int) -> int:
        return original(fd, data[:128], offset)

    with patch.object(store_module.os, "pwrite", side_effect=short_write):
        token = store.allocate("JOURNAL", op_id=f"{73:032x}")
        store.write(token, b"SHORT_WRITE_SENTINEL")
    assert store.read(token, size=len(b"SHORT_WRITE_SENTINEL")) == b"SHORT_WRITE_SENTINEL"
    assert store.snapshot().seq == 4
    store.close_token(token)
    store.close()
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    reopened = SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
    assert reopened.snapshot().seq == 4
    reopened.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p2_modeled_head_read_error_quarantines_without_rewrite(modeled_store) -> None:
    root, issuer, store = modeled_store
    store.close()
    before = _observe(root)
    head_ino = (root / "authority" / "committed-head.log").stat().st_ino
    original = os.pread

    def fail_head(fd: int, size: int, offset: int) -> bytes:
        if os.fstat(fd).st_ino == head_ino:
            raise OSError(errno.EIO, "modeled head read failure")
        return original(fd, size, offset)

    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    with patch.object(store_module.os, "pread", side_effect=fail_head):
        with pytest.raises(QuarantinedError):
            SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
    assert _observe(root) == before


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
@pytest.mark.parametrize("fault", ("tear_registry", "drop_head", "reorder_head"))
def test_p2_modeled_storage_faults_are_not_process_kill_proof(modeled_store, fault: str) -> None:
    root, issuer, store = modeled_store
    token = store.allocate("JOURNAL", op_id=f"{74:032x}")
    store.close_token(token)
    store.close()
    registry = root / "authority" / "registry.log"
    head = root / "authority" / "committed-head.log"
    if fault == "tear_registry":
        with registry.open("r+b") as stream:
            stream.truncate(registry.stat().st_size - 1)
    elif fault == "drop_head":
        with head.open("r+b") as stream:
            stream.truncate(head.stat().st_size - 2048)
    else:
        data = head.read_bytes()
        assert len(data) == 4 * 2048
        swapped = data[:2048] + data[4096:6144] + data[2048:4096] + data[6144:]
        with head.open("r+b") as stream:
            stream.write(swapped)
            stream.flush()
            os.fsync(stream.fileno())
    observed = _observe(root)
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    with pytest.raises(QuarantinedError):
        SyntheticAuthorityStore.reopen_test(root, anchor=anchor, key=key)
    assert _observe(root) == observed, "modeled fault evidence must not be repaired"


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_live_owner_lease_blocks_cold_process_until_clean_close(modeled_store) -> None:
    root, issuer, store = modeled_store
    anchor, key = issuer.readback(issuer.grant.enrollment_id)
    anchor_wire = asdict(anchor)
    anchor_wire["root_chain"] = [list(x) for x in anchor.root_chain]
    request = json.dumps(
        {"anchor": anchor_wire, "key": base64.b64encode(key).decode("ascii")},
    ).encode("ascii")
    before = _observe(root)
    blocked = subprocess.run(
        [sys.executable, str(WORKER), "reopen", "--root", str(root)],
        input=request,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert blocked.returncode == 2
    assert b"LeaseBusyError" in blocked.stderr
    assert _observe(root) == before
    store.close()
    admitted = subprocess.run(
        [sys.executable, str(WORKER), "reopen", "--root", str(root)],
        input=request,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert admitted.returncode == 0, admitted.stderr.decode("utf-8", "replace")
    assert json.loads(admitted.stdout)["seq"] == 1
    assert _observe(root) == before


@pytest.mark.skipif(sys.platform != "darwin", reason="positive ACL/FD proof is Darwin only")
def test_p5_fork_child_cannot_use_parent_owner_but_parent_remains_active(modeled_store) -> None:
    root, _issuer, store = modeled_store
    before = _observe(root)
    result_r, result_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(result_r)
        try:
            store.snapshot()
        except PoisonedError:
            os.write(result_w, b"PoisonedError")
            os._exit(0)
        except BaseException as exc:
            os.write(result_w, type(exc).__name__.encode("ascii"))
            os._exit(2)
        os.write(result_w, b"unexpected-success")
        os._exit(3)
    os.close(result_w)
    try:
        ready, _, _ = select.select([result_r], [], [], 5)
        assert ready, "fork child did not report a bounded outcome"
        outcome = os.read(result_r, 128)
        reaped_pid, status = os.waitpid(pid, 0)
        assert reaped_pid == pid and os.waitstatus_to_exitcode(status) == 0
        assert outcome == b"PoisonedError"
        assert store.snapshot().seq == 1
        assert _observe(root) == before
    finally:
        os.close(result_r)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
