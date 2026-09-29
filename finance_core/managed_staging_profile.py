"""Fixed, cooperative synthetic staging enrollment and short SQLite lifecycle.

Only trusted profile owners should call these APIs. This component does not
offer an arbitrary SQL, path, connection, or live-database entry point.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from finance_core.profile_gate import (
    GateLease,
    initialize_profile_gate,
    writer_gate,
)
from finance_core.profile_paths import (
    _MANAGED_SQLITE_LIFETIME_LOCK,
    MANAGED_STAGING_FILENAME,
    ManagedStagingProfile,
    ProfilePathError,
    ProfilePaths,
    _check_regular_role,
    _migration_contract_digest,
    _reject_unexpected_staging_roles,
    validate_registered_staging_profile,
)
from finance_core.staging_guard import (
    _open_managed_staging_database,
    _StagingCloseUncertain,
    create_staging_database,
)

_PENDING_FILENAME = ".managed-staging.v1.pending"
_SIDECARS = ("-wal", "-shm", "-journal")
# A failed close does not establish that SQLite released all handles. Keep the
# connection and gate alive until process exit instead of reporting a safe cut.
_UNCERTAIN_CLOSES: list[tuple[sqlite3.Connection, GateLease]] = []


def _close_sqlite_then_gate(
    conn: sqlite3.Connection | None,
    lease: GateLease,
    *,
    release: bool = True,
) -> None:
    if conn is None:
        if release:
            lease.close()
        return
    rollback_error: BaseException | None = None
    try:
        if conn.in_transaction:
            conn.rollback()
    except BaseException as exc:
        rollback_error = exc
    try:
        conn.close()
    except BaseException:
        _UNCERTAIN_CLOSES.append((conn, lease))
        _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
        raise
    if release:
        lease.close()
    if rollback_error is not None:
        raise rollback_error


def _require_fresh_names(profile: ProfilePaths) -> None:
    """No pre-existing enrollment, SQLite main, or fixed associated sidecars."""
    profile.revalidate()
    names = (
        profile.profile / MANAGED_STAGING_FILENAME,
        profile.profile / _PENDING_FILENAME,
        profile.staging_database,
        *(Path(f"{profile.staging_database}{suffix}") for suffix in _SIDECARS),
    )
    for path in names:
        if os.path.lexists(path):
            raise ProfilePathError(f"Fresh staging profile has an existing reserved name: {path}")
    _reject_unexpected_staging_roles(profile.staging_database)


def _publish_registration(profile: ProfilePaths) -> None:
    main = _check_regular_role(profile.staging_database)
    _reject_unexpected_staging_roles(profile.staging_database)
    for suffix in _SIDECARS:
        sidecar = Path(f"{profile.staging_database}{suffix}")
        if os.path.lexists(sidecar):
            _check_regular_role(sidecar)
    payload = {
        "version": 1,
        "profile_id": profile.profile_id,
        "runtime_root": str(profile.runtime),
        "workspace_root": str(profile.workspace),
        "staging_database": str(profile.staging_database),
        "main_device": main.st_dev,
        "main_inode": main.st_ino,
        "migration_contract_sha256": _migration_contract_digest(),
        "instance_id": uuid.uuid4().hex,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    directory_fd = os.open(profile.profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pending_fd = os.open(
            _PENDING_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(pending_fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
            os.fsync(pending_fd)
        finally:
            os.close(pending_fd)
        # Hard-link publication cannot replace an existing registration. A
        # crash before pending removal remains visibly incomplete on reopen.
        os.link(
            _PENDING_FILENAME,
            MANAGED_STAGING_FILENAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
        os.unlink(_PENDING_FILENAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _sync_created_main_name(profile: ProfilePaths) -> None:
    """Make the SQLite main directory entry durable before enrollment."""
    directory = profile.staging_database.parent
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pinned = os.fstat(profile._pins["workspace/database"])
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ProfilePathError("Staging directory changed before registration")
        os.fsync(fd)
    finally:
        os.close(fd)


def bootstrap_registered_staging(blank_profile: ProfilePaths) -> ManagedStagingProfile:
    """Enroll one existing blank synthetic profile at its fixed staging path.

    Failure preserves the database and registration bytes for explicit
    disposition. It never adopts an unregistered existing SQLite database.
    """
    with _MANAGED_SQLITE_LIFETIME_LOCK:
        return _bootstrap_registered_staging_locked(blank_profile)


def _bootstrap_registered_staging_locked(blank_profile: ProfilePaths) -> ManagedStagingProfile:
    if type(blank_profile) is not ProfilePaths:
        raise ProfilePathError("Bootstrap requires the original blank profile witness")
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

    _require_fresh_names(blank_profile)
    gate_path = blank_profile.profile / ".profile-gate.v1.lock"
    if not os.path.lexists(gate_path):
        initialize_profile_gate(blank_profile)
    lease = writer_gate(blank_profile)
    conn: sqlite3.Connection | None = None
    sqlite_started = False
    try:
        _require_fresh_names(blank_profile)
        _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
        sqlite_started = True
        conn = create_staging_database(
            blank_profile.staging_database, migration_paths=TEMP_DB_MIGRATION_PATHS
        )
        _close_sqlite_then_gate(conn, lease, release=False)
        conn = None
        _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
        sqlite_started = False
        _sync_created_main_name(blank_profile)
        _publish_registration(blank_profile)
        return verify_registered_staging(
            blank_profile.application_support, blank_profile.profile_id
        )
    except _StagingCloseUncertain as exc:
        _UNCERTAIN_CLOSES.append((exc.connection, lease))
        _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
        raise
    except BaseException:
        # If the close failed, _close_sqlite_then_gate retained the lease.
        if conn is not None and not any(retained is lease for _, retained in _UNCERTAIN_CLOSES):
            _close_sqlite_then_gate(conn, lease, release=False)
        raise
    finally:
        if not any(retained is lease for _, retained in _UNCERTAIN_CLOSES):
            lease.close()
            if sqlite_started:
                _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()


@contextmanager
def _managed_staging_connection(
    profile: ManagedStagingProfile,
    *,
    purpose: Literal["reopen"] = "reopen",
) -> Iterator[sqlite3.Connection]:
    """Hold a shared gate from before SQLite recovery until close completes."""
    if type(profile) is not ManagedStagingProfile or purpose != "reopen":
        raise ProfilePathError("A registered fixed staging profile is required")
    with _MANAGED_SQLITE_LIFETIME_LOCK:
        with _managed_staging_connection_locked(profile) as conn:
            yield conn


@contextmanager
def _managed_staging_connection_locked(
    profile: ManagedStagingProfile,
) -> Iterator[sqlite3.Connection]:
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

    lease = writer_gate(profile)
    conn: sqlite3.Connection | None = None
    sqlite_started = False
    try:
        profile.revalidate()
        _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
        sqlite_started = True
        conn = _open_managed_staging_database(
            profile.staging_database, migration_paths=TEMP_DB_MIGRATION_PATHS
        )
        # The process lock excludes another thread's ACL path check while this
        # handle lives. Same-thread revalidation skips main/sidecar ACL APIs;
        # a future cut must do its full ACL preflight after SQLite closes.
        yield conn
    except _StagingCloseUncertain as exc:
        _UNCERTAIN_CLOSES.append((exc.connection, lease))
        _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
        raise
    finally:
        if not any(retained is lease for _, retained in _UNCERTAIN_CLOSES):
            try:
                _close_sqlite_then_gate(conn, lease)
            finally:
                if sqlite_started and not any(
                    retained is lease for _, retained in _UNCERTAIN_CLOSES
                ):
                    _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()


def verify_registered_staging(
    application_support_root: str | Path,
    profile_id: str,
) -> ManagedStagingProfile:
    """Reopen and check the fixed enrolled DB; return a managed path witness."""
    profile = validate_registered_staging_profile(application_support_root, profile_id)
    try:
        with _managed_staging_connection(profile) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise ProfilePathError("Managed staging SQLite quick check failed")
        profile.revalidate()
        return profile
    except BaseException:
        profile.close()
        raise


__all__ = ["bootstrap_registered_staging", "verify_registered_staging"]
