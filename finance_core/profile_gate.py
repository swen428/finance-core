"""Fixed, profile-bound advisory gate for a future consistent backup cut.

This module does not open a database or start a backup. Every participating
writer must hold a shared lease for the whole durable write, and a cut must
hold an exclusive lease for the whole freeze. A descriptor's metadata cannot
prove that another process still holds a lock; the owner of a parent lease
must keep it open until its child has been reaped. Only leases returned by
writer_gate, writer_gate_from_parent, or exclusive_cut represent acquisition;
assert_valid checks identity and deadline, not whether a lock is still held.
"""

from __future__ import annotations

import errno
import fcntl
import math
import os
import stat
import time
from pathlib import Path
from typing import Self

from finance_core.profile_paths import ProfilePathError, ProfilePaths, _reject_acl_grants

LOCK_FILENAME = ".profile-gate.v1.lock"
DEFAULT_ACQUIRE_SECONDS = 5.0
MAX_ACQUIRE_SECONDS = 30.0
MAX_HOLD_SECONDS = 30.0
_POLL_SECONDS = 0.02
_LEASE_CONSTRUCTOR_TOKEN = object()


class ProfileGateError(RuntimeError):
    """The profile or fixed gate identity is unsafe or unavailable."""


class ProfileGateBusy(ProfileGateError):
    """A shared or exclusive gate could not be acquired within its bound."""


class ProfileGateHoldExpired(ProfileGateError):
    """An exclusive cut exceeded its checked hold deadline."""


def _require_profile(profile: ProfilePaths) -> Path:
    if not isinstance(profile, ProfilePaths):
        raise ProfileGateError("A validated ProfilePaths witness is required")
    try:
        profile.revalidate()
    except (OSError, ProfilePathError) as exc:
        raise ProfileGateError("Profile path witness is unsafe") from exc
    return profile.profile / LOCK_FILENAME


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_lock_fd(profile: ProfilePaths, fd: int) -> Path:
    path = _require_profile(profile)
    try:
        opened = os.fstat(fd)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or not _same_file(opened, named)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_size != 0
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
        ):
            raise ProfileGateError("Fixed profile gate has unsafe file identity or permissions")
        _reject_acl_grants(fd, path)
        profile.revalidate()
        if not _same_file(opened, path.lstat()):
            raise ProfileGateError("Fixed profile gate path changed")
    except (OSError, ProfilePathError) as exc:
        raise ProfileGateError(f"Fixed profile gate cannot be validated: {exc}") from exc
    return path


def initialize_profile_gate(profile: ProfilePaths) -> Path:
    """Explicitly create one empty 0600 gate, then fsync its file and directory.

    An existing name, including a symlink, is never opened or replaced. A
    failed initialization leaves its file as evidence rather than unlinking a
    potentially replaced path. Normal gate opens never create the file.
    """
    path = _require_profile(profile)
    directory_fd = -1
    fd = -1
    try:
        directory_fd = os.open(
            profile.profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        if not _same_file(os.fstat(directory_fd), profile.profile.stat()):
            raise ProfileGateError("Profile directory changed before gate initialization")
        fd = os.open(
            LOCK_FILENAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_fd,
        )
        if not _try_lock(fd, fcntl.LOCK_EX):
            raise ProfileGateError("New profile gate could not be locked during initialization")
        os.fchmod(fd, 0o600)
        _validate_lock_fd(profile, fd)
        os.fsync(fd)
        os.fsync(directory_fd)
        _validate_lock_fd(profile, fd)
        return path
    except BaseException as exc:
        if fd >= 0:
            # A failed fsync must not leave an apparently usable 0600 gate.
            # Keep the failed file as evidence, but make normal opens refuse it.
            try:
                os.fchmod(fd, 0o000)
                os.fsync(fd)
            except OSError:
                pass
        if isinstance(exc, (OSError, ProfilePathError)):
            raise ProfileGateError("Fixed profile gate initialization failed") from exc
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _open_existing_gate(profile: ProfilePaths) -> int:
    path = _require_profile(profile)
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ProfileGateError("Fixed profile gate is missing or cannot be opened") from exc
    try:
        _validate_lock_fd(profile, fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _bounded_seconds(value: float, *, maximum: float, name: str, allow_zero: bool) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value > maximum
        or (value < 0 if allow_zero else value <= 0)
    ):
        raise ValueError(f"{name} must be a finite value within the allowed bound")
    return float(value)


def _try_lock(fd: int, operation: int) -> bool:
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
            return False
        raise ProfileGateError("Fixed profile gate lock operation failed") from exc


class GateLease:
    """A module-issued lock lease on one independent open; release is close only."""

    __slots__ = ("_profile", "_fd", "_hold_deadline")

    def __init__(
        self,
        profile: ProfilePaths,
        fd: int,
        *,
        hold_deadline: float | None = None,
        _constructor_token: object | None = None,
    ) -> None:
        if _constructor_token is not _LEASE_CONSTRUCTOR_TOKEN:
            raise ProfileGateError("GateLease must be issued by a profile gate acquisition API")
        self._profile = profile
        self._fd = fd
        self._hold_deadline = hold_deadline

    def fileno(self) -> int:
        if self._fd < 0:
            raise ProfileGateError("Profile gate lease is closed")
        return self._fd

    def assert_valid(self) -> None:
        """Check the held file identity and, for a cut, its monotonic hold bound.

        This cannot prove the lock is still held. It is a cooperative deadline
        check, not asynchronous preemption. Call it at every exporter transition
        and abort the cut on expiry.
        """
        fd = self.fileno()
        if self._hold_deadline is not None and time.monotonic() >= self._hold_deadline:
            raise ProfileGateHoldExpired("Exclusive profile cut exceeded its hold deadline")
        _validate_lock_fd(self._profile, fd)

    def close(self) -> None:
        if self._fd >= 0:
            fd = self._fd
            self._fd = -1
            os.close(fd)

    def __enter__(self) -> Self:
        try:
            self.assert_valid()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_args: object) -> None:
        self.close()


def _acquire(
    profile: ProfilePaths,
    operation: int,
    *,
    timeout_seconds: float,
    max_hold_seconds: float | None = None,
) -> GateLease:
    timeout = _bounded_seconds(
        timeout_seconds, maximum=MAX_ACQUIRE_SECONDS, name="timeout_seconds", allow_zero=True
    )
    hold = (
        None
        if max_hold_seconds is None
        else _bounded_seconds(
            max_hold_seconds,
            maximum=MAX_HOLD_SECONDS,
            name="max_hold_seconds",
            allow_zero=False,
        )
    )
    fd = _open_existing_gate(profile)
    try:
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(fd, operation):
                _validate_lock_fd(profile, fd)
                hold_deadline = None if hold is None else time.monotonic() + hold
                return GateLease(
                    profile,
                    fd,
                    hold_deadline=hold_deadline,
                    _constructor_token=_LEASE_CONSTRUCTOR_TOKEN,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProfileGateBusy("Fixed profile gate is contended")
            time.sleep(min(_POLL_SECONDS, remaining))
    except BaseException:
        os.close(fd)
        raise


def writer_gate(
    profile: ProfilePaths, *, timeout_seconds: float = DEFAULT_ACQUIRE_SECONDS
) -> GateLease:
    """Acquire a bounded shared writer lease on the existing fixed gate."""
    return _acquire(profile, fcntl.LOCK_SH, timeout_seconds=timeout_seconds)


def exclusive_cut(
    profile: ProfilePaths,
    *,
    timeout_seconds: float = DEFAULT_ACQUIRE_SECONDS,
    max_hold_seconds: float = MAX_HOLD_SECONDS,
) -> GateLease:
    """Acquire a bounded exclusive cut lease; no writer-lock upgrade exists."""
    return _acquire(
        profile,
        fcntl.LOCK_EX,
        timeout_seconds=timeout_seconds,
        max_hold_seconds=max_hold_seconds,
    )


def writer_gate_from_parent(profile: ProfilePaths, inherited_fd: int = 4) -> GateLease:
    """Validate fd4 as a witness, then take one independent SH|NB attempt.

    This does not inspect or mutate the inherited lock state. An inherited
    descriptor can identify the file but cannot prove the parent still holds a
    lease. The parent must retain its own lease until child reap.
    """
    _validate_lock_fd(profile, inherited_fd)
    fd = _open_existing_gate(profile)
    try:
        if not _try_lock(fd, fcntl.LOCK_SH):
            raise ProfileGateBusy("Fixed profile gate is contended for child writer")
        _validate_lock_fd(profile, fd)
        return GateLease(profile, fd, _constructor_token=_LEASE_CONSTRUCTOR_TOKEN)
    except BaseException:
        os.close(fd)
        raise


__all__ = [
    "DEFAULT_ACQUIRE_SECONDS",
    "GateLease",
    "LOCK_FILENAME",
    "MAX_ACQUIRE_SECONDS",
    "MAX_HOLD_SECONDS",
    "ProfileGateBusy",
    "ProfileGateError",
    "ProfileGateHoldExpired",
    "exclusive_cut",
    "initialize_profile_gate",
    "writer_gate",
    "writer_gate_from_parent",
]
