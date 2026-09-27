"""D4 fixed profile gate tests; every path and process uses temporary synthetic data."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import finance_core.profile_gate as gate
from finance_core.profile_gate import (
    LOCK_FILENAME,
    ProfileGateBusy,
    ProfileGateError,
    ProfileGateHoldExpired,
    exclusive_cut,
    initialize_profile_gate,
    writer_gate,
    writer_gate_from_parent,
)
from finance_core.profile_paths import ProfilePaths, validate_profile_paths


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProfilePaths:
    support = tmp_path / "Application Support"
    support.mkdir(mode=0o700)
    base = support / "Finance-Codex" / "profiles" / "synthetic"
    for directory in (
        base.parent.parent,
        base.parent,
        base,
        base / "runtime",
        base / "runtime/database",
        base / "workspace",
        base / "workspace/database",
        base / "backups",
        base / "work",
        base / "restore",
    ):
        directory.mkdir(mode=0o700)
    marker = base / "profile.json"
    marker.write_text(
        json.dumps(
            {
                "profile_id": "synthetic",
                "runtime_root": str(base / "runtime"),
                "workspace_root": str(base / "workspace"),
            }
        ),
        encoding="utf-8",
    )
    marker.chmod(0o600)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "runtime"))
    with validate_profile_paths(support, "synthetic") as witness:
        yield witness


_CHILD = """
import os
import sys
from finance_core.profile_paths import validate_profile_paths
from finance_core.profile_gate import (
    ProfileGateBusy, ProfileGateError, exclusive_cut, writer_gate, writer_gate_from_parent,
)
support, profile_id, mode = sys.argv[1:4]
if mode == 'from_parent':
    os.dup2(int(sys.argv[4]), 4)
with validate_profile_paths(support, profile_id) as profile:
    try:
        if mode == 'writer':
            lease = writer_gate(profile, timeout_seconds=0.2)
        elif mode == 'cut':
            lease = exclusive_cut(profile, timeout_seconds=0.2)
        else:
            lease = writer_gate_from_parent(profile)
        with lease:
            print('ACQUIRED')
    except ProfileGateBusy:
        print('BUSY')
    except ProfileGateError:
        print('REFUSED')
"""


def _child(profile: ProfilePaths, mode: str, inherited_fd: int | None = None) -> str:
    command = [
        sys.executable,
        "-c",
        _CHILD,
        str(profile.application_support),
        profile.profile_id,
        mode,
    ]
    if inherited_fd is not None:
        command.append(str(inherited_fd))
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
        cwd=Path(__file__).resolve().parents[1],
        pass_fds=() if inherited_fd is None else (inherited_fd,),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_init_is_explicit_exclusive_and_fsyncs_file_and_directory(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = profile.profile / LOCK_FILENAME
    with pytest.raises(ProfileGateError, match="missing"):
        writer_gate(profile, timeout_seconds=0)
    assert not lock_path.exists()

    original_fsync = os.fsync
    synced_types: list[str] = []

    def tracking_fsync(fd: int) -> None:
        synced_types.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(gate.os, "fsync", tracking_fsync)
        assert initialize_profile_gate(profile) == lock_path
    assert synced_types == ["file", "directory"]
    info = lock_path.stat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1 and info.st_size == 0
    with pytest.raises(ProfileGateError):
        initialize_profile_gate(profile)


def test_writer_cannot_enter_new_gate_before_initializer_takes_exclusive_lock(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_try_lock = gate._try_lock
    checked = False

    def pause_before_exclusive(fd: int, operation: int) -> bool:
        nonlocal checked
        assert operation == gate.fcntl.LOCK_EX
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0
        assert _child(profile, "writer") == "REFUSED"
        checked = True
        return original_try_lock(fd, operation)

    with monkeypatch.context() as patch:
        patch.setattr(gate, "_try_lock", pause_before_exclusive)
        initialize_profile_gate(profile)
    assert checked
    assert _child(profile, "writer") == "ACQUIRED"


def test_directory_fsync_failure_leaves_refused_gate(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fsync = os.fsync

    def refuse_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "synthetic directory fsync refusal")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(gate.os, "fsync", refuse_directory_fsync)
        with pytest.raises(ProfileGateError, match="initialization failed"):
            initialize_profile_gate(profile)
    path = profile.profile / LOCK_FILENAME
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0
    with pytest.raises(ProfileGateError):
        writer_gate(profile, timeout_seconds=0)


def test_shared_writers_overlap_and_exclusive_cut_waits_for_other_processes(
    profile: ProfilePaths,
) -> None:
    initialize_profile_gate(profile)
    with writer_gate(profile) as parent:
        parent.assert_valid()
        assert _child(profile, "writer") == "ACQUIRED"
        assert _child(profile, "cut") == "BUSY"
        parent.assert_valid()
    assert _child(profile, "cut") == "ACQUIRED"

    with exclusive_cut(profile) as cut:
        cut.assert_valid()
        assert _child(profile, "writer") == "BUSY"
    assert _child(profile, "writer") == "ACQUIRED"


def test_child_parent_fd_is_only_a_witness_and_contention_is_immediate(
    profile: ProfilePaths,
) -> None:
    initialize_profile_gate(profile)
    with writer_gate(profile) as parent:
        assert _child(profile, "from_parent", parent.fileno()) == "ACQUIRED"
        parent.assert_valid()  # Parent remains held through child reap.
    with exclusive_cut(profile) as cut:
        start = time.monotonic()
        assert _child(profile, "from_parent", cut.fileno()) == "BUSY"
        assert time.monotonic() - start < 2
        cut.assert_valid()


def test_child_uses_exactly_one_nonblocking_shared_lock_attempt(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_profile_gate(profile)
    with writer_gate(profile) as parent:
        calls: list[int] = []

        def contended(_fd: int, operation: int) -> None:
            calls.append(operation)
            raise BlockingIOError(errno.EWOULDBLOCK, "synthetic contention")

        with monkeypatch.context() as patch:
            patch.setattr(gate.fcntl, "flock", contended)
            with pytest.raises(ProfileGateBusy):
                writer_gate_from_parent(profile, parent.fileno())
        assert calls == [gate.fcntl.LOCK_SH | gate.fcntl.LOCK_NB]


def test_child_rejects_wrong_or_closed_inherited_fd(profile: ProfilePaths) -> None:
    initialize_profile_gate(profile)
    other = profile.profile / "other-file"
    other.write_bytes(b"")
    other.chmod(0o600)
    fd = os.open(other, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ProfileGateError):
            writer_gate_from_parent(profile, fd)
    finally:
        os.close(fd)
    with pytest.raises(ProfileGateError):
        writer_gate_from_parent(profile, fd)


def test_inherited_fd_is_identity_only_not_proof_of_parent_lock(profile: ProfilePaths) -> None:
    path = initialize_profile_gate(profile)
    witness_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        # This succeeds because the independent SH lock is available. The fd
        # itself cannot attest that any other process still holds a lease.
        with writer_gate_from_parent(profile, witness_fd) as child:
            child.assert_valid()
    finally:
        os.close(witness_fd)


def test_unlocked_fd_cannot_be_wrapped_as_a_gate_lease(profile: ProfilePaths) -> None:
    path = initialize_profile_gate(profile)
    unlocked_fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        with pytest.raises(ProfileGateError, match="issued by a profile gate acquisition API"):
            gate.GateLease(profile, unlocked_fd)
        with pytest.raises(ProfileGateError, match="issued by a profile gate acquisition API"):
            gate.GateLease(profile, unlocked_fd, _constructor_token=object())
        assert os.fstat(unlocked_fd).st_ino == path.stat().st_ino
        with writer_gate(profile) as issued:
            issued.assert_valid()
    finally:
        os.close(unlocked_fd)


def test_exclusive_hold_deadline_requires_cooperative_check(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_profile_gate(profile)
    with exclusive_cut(profile, max_hold_seconds=30) as cut:
        with monkeypatch.context() as patch:
            patch.setattr(gate.time, "monotonic", lambda: float("inf"))
            with pytest.raises(ProfileGateHoldExpired):
                cut.assert_valid()
    with writer_gate(profile) as writer:
        writer.assert_valid()


def test_exclusive_acquisition_counts_post_lock_validation(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_profile_gate(profile)
    clock = [0.0]
    original_validate = gate._validate_lock_fd
    validations = 0

    def delayed_validation(witness: ProfilePaths, fd: int) -> Path:
        nonlocal validations
        validations += 1
        result = original_validate(witness, fd)
        if validations == 2:  # The second check runs after the exclusive flock succeeds.
            clock[0] = 0.2
        return result

    with monkeypatch.context() as patch:
        patch.setattr(gate.time, "monotonic", lambda: clock[0])
        patch.setattr(gate, "_validate_lock_fd", delayed_validation)
        with pytest.raises(ProfileGateHoldExpired):
            exclusive_cut(profile, max_hold_seconds=0.1)
    with writer_gate(profile) as writer:
        writer.assert_valid()


def test_exclusive_assert_valid_counts_validation_time_and_releases_lock(
    profile: ProfilePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_profile_gate(profile)
    clock = [0.0]
    with monkeypatch.context() as patch:
        patch.setattr(gate.time, "monotonic", lambda: clock[0])
        cut = exclusive_cut(profile, max_hold_seconds=0.1)
        original_validate = gate._validate_lock_fd

        def delayed_validation(witness: ProfilePaths, fd: int) -> Path:
            result = original_validate(witness, fd)
            clock[0] = 0.2
            return result

        patch.setattr(gate, "_validate_lock_fd", delayed_validation)
        with pytest.raises(ProfileGateHoldExpired):
            cut.assert_valid()
        with pytest.raises(ProfileGateError, match="closed"):
            cut.assert_valid()
    with writer_gate(profile) as writer:
        writer.assert_valid()


def test_unsafe_lock_identity_is_refused_without_replacement(profile: ProfilePaths) -> None:
    path = initialize_profile_gate(profile)
    other = profile.profile / "other-link"
    os.link(path, other)
    with pytest.raises(ProfileGateError, match="unsafe"):
        writer_gate(profile, timeout_seconds=0)
    other.unlink()

    path.chmod(0o644)
    with pytest.raises(ProfileGateError, match="unsafe"):
        writer_gate(profile, timeout_seconds=0)
    path.chmod(0o600)

    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(ProfileGateError):
        writer_gate(profile, timeout_seconds=0)
    assert other.exists()


def test_held_lease_detects_replaced_gate_path(profile: ProfilePaths) -> None:
    path = initialize_profile_gate(profile)
    with writer_gate(profile) as lease:
        old = profile.profile / "old-gate"
        path.rename(old)
        path.write_bytes(b"")
        path.chmod(0o600)
        with pytest.raises(ProfileGateError, match="unsafe file identity"):
            lease.assert_valid()


def test_fifo_lock_is_refused_without_blocking(profile: ProfilePaths) -> None:
    path = profile.profile / LOCK_FILENAME
    os.mkfifo(path, 0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from finance_core.profile_gate import writer_gate, ProfileGateError; "
            "from finance_core.profile_paths import validate_profile_paths; "
            "import sys; "
            "\nwith validate_profile_paths(sys.argv[1], 'synthetic') as profile:"
            "\n try: writer_gate(profile, timeout_seconds=0)"
            "\n except ProfileGateError: sys.exit(0)"
            "\nsys.exit(1)",
            str(profile.application_support),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL only")
def test_lock_acl_allow_is_refused(profile: ProfilePaths) -> None:
    path = initialize_profile_gate(profile)
    subprocess.run(["chmod", "+a", "everyone allow read", str(path)], check=True)
    with pytest.raises(ProfileGateError, match="ACL grants"):
        writer_gate(profile, timeout_seconds=0)
