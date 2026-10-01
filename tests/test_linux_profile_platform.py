"""Linux managed-profile admission with disposable synthetic trees only."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import finance_core.managed_cut_protocol as cut_protocol
import finance_core.managed_staging_profile as staging
import finance_core.profile_paths as profile_paths
import finance_core.staging_guard as staging_guard
from finance_core.managed_disk_snapshot import (
    DiskSnapshotLimits,
    stage_disk_snapshot,
    verify_staged_disk_snapshot,
)
from finance_core.openclaw_staging_bridge.workspace_access import managed_profile_for_workspace
from finance_core.profile_gate import exclusive_cut, initialize_profile_gate
from finance_core.profile_layout import (
    has_managed_staging_ancestor,
    is_fixed_staging_path,
    is_linux_managed_namespace,
    is_managed_namespace,
    workspace_layout,
)
from finance_core.reconciliation.migration_safety import (
    MigrationTargetError,
    _is_managed_profile_namespace,
    validate_migration_target,
)
from finance_core.staging_guard import (
    StagingDatabaseError,
    create_staging_database,
    open_staging_database,
)

LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="kernel POSIX ACL proof on Linux")


def _linux_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "finance-codex"
    base = root / "profiles" / "synthetic"
    for directory in (
        root,
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
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
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
    return root, base


def _access_acl() -> bytes:
    # Linux POSIX ACL v2: named user with a zero mask keeps visible mode 0600.
    entries = ((1, 6, 0), (2, 4, 65534), (4, 0, 0), (16, 0, 0), (32, 0, 0))
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def _default_acl() -> bytes:
    entries = ((1, 7, 0), (4, 0, 0), (32, 0, 0))
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def test_fixed_linux_layout_recognizes_unregistered_and_copied_trees(tmp_path: Path) -> None:
    workspace = tmp_path / "copy" / "finance-codex/profiles/synthetic/workspace"
    database = workspace / "database/staging.sqlite"
    assert workspace_layout(workspace) == "linux"
    assert is_fixed_staging_path(database)
    assert is_linux_managed_namespace(database)
    assert is_managed_namespace(database)
    assert _is_managed_profile_namespace(database)
    with pytest.raises(MigrationTargetError, match="managed profile namespace"):
        validate_migration_target(database)
    assert not is_linux_managed_namespace(tmp_path / "ordinary/staging.sqlite")


def test_marker_check_passes_non_directory_ancestors_and_still_checks_higher_markers(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "ordinary-file"
    blocked_parent.write_text("synthetic file", encoding="utf-8")
    target = blocked_parent / "nested/backup.sqlite"
    assert not has_managed_staging_ancestor(target)
    (tmp_path / ".managed-staging.v1.pending").symlink_to(tmp_path / "absent-marker-target")
    assert has_managed_staging_ancestor(target)


@pytest.mark.parametrize(
    "relative",
    (
        "workspace/database/staging.sqlite",
        "workspace/database/other.sqlite",
        "runtime/database/finance.db",
    ),
)
@pytest.mark.parametrize("registration", ("absent", "corrupt", "pending"))
def test_any_linux_managed_namespace_refuses_generic_sqlite_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    registration: str,
) -> None:
    _root, base = _linux_tree(tmp_path, monkeypatch)
    if registration == "corrupt":
        (base / ".managed-staging.v1.json").write_text("{", encoding="utf-8")
    elif registration == "pending":
        (base / ".managed-staging.v1.pending").write_text("pending", encoding="utf-8")
    target = base / relative

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("generic SQLite or file opener was reached")

    with monkeypatch.context() as patch:
        patch.setattr(staging_guard.sqlite3, "connect", forbidden)
        patch.setattr(staging_guard.os, "open", forbidden)
        with pytest.raises(StagingDatabaseError, match="managed profile gate"):
            create_staging_database(target)
        with pytest.raises(StagingDatabaseError, match="managed profile gate"):
            open_staging_database(target)
    assert not target.exists()


def test_linux_managed_symlink_alias_refuses_before_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, base = _linux_tree(tmp_path, monkeypatch)
    target = base / "workspace/database/staging.sqlite"
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(target)
    with pytest.raises(StagingDatabaseError, match="managed profile gate"):
        open_staging_database(alias)


@LINUX_ONLY
def test_linux_blank_enrollment_reopen_gate_and_generic_preopen_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base = _linux_tree(tmp_path, monkeypatch)
    database = base / "workspace/database/staging.sqlite"
    with pytest.raises(StagingDatabaseError, match="managed profile gate"):
        create_staging_database(database)
    with pytest.raises(StagingDatabaseError, match="managed profile gate"):
        open_staging_database(database)
    assert not database.exists()

    blank = profile_paths.validate_linux_profile_paths(root, "synthetic")
    initialize_profile_gate(blank)
    managed = staging.bootstrap_registered_staging(blank)
    try:
        assert managed.linux_data_root == root
        assert managed.layout == "linux"
        with exclusive_cut(managed, timeout_seconds=0):
            managed.revalidate()
            stage = managed.work / f"core-cut-{uuid.uuid4().hex}"
            stage.mkdir(mode=0o700)
            limits = DiskSnapshotLimits(
                max_core_db_bytes=32 * 1024 * 1024,
                max_stage_bytes=64 * 1024 * 1024,
                min_free_bytes=1024 * 1024,
                backup_pages_per_step=256,
            )
            deadline = time.monotonic() + 20.0
            with staging._delegated_cut_source(managed) as source:
                staged = stage_disk_snapshot(
                    source,
                    private_stage=stage,
                    limits=limits,
                    deadline_monotonic=deadline,
                )
            receipt = verify_staged_disk_snapshot(
                staged, private_stage=stage, limits=limits, deadline_monotonic=deadline
            )
            assert receipt.byte_length > 0
        reopened = staging.verify_registered_linux_staging(root, "synthetic")
        reopened.close()
        routed = managed_profile_for_workspace(base / "workspace")
        assert routed is not None and routed.layout == "linux"
        routed.close()
        with pytest.raises(profile_paths.ProfilePathError, match="native SQLite admission"):
            profile_paths.validate_linux_profile_paths(root, "synthetic")
        with pytest.raises(StagingDatabaseError, match="managed profile gate"):
            open_staging_database(database)
    finally:
        managed.close()
        blank.close()


@LINUX_ONLY
def test_linux_root_remains_exact_0700_on_admission_and_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _base = _linux_tree(tmp_path, monkeypatch)
    with profile_paths.validate_linux_profile_paths(root, "synthetic") as blank:
        root.chmod(0o755)
        with pytest.raises(profile_paths.ProfilePathError, match="private"):
            blank.revalidate()
        with pytest.raises(profile_paths.ProfilePathError, match="private"):
            profile_paths.validate_linux_profile_paths(root, "synthetic")
        root.chmod(0o700)
        blank.revalidate()


@LINUX_ONLY
def test_linux_locator_permissions_and_registration_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base = _linux_tree(tmp_path, monkeypatch)
    with pytest.raises(profile_paths.ProfilePathError, match="finance-codex"):
        profile_paths.validate_linux_profile_paths(root.parent, "synthetic")
    blank = profile_paths.validate_linux_profile_paths(root, "synthetic")
    initialize_profile_gate(blank)
    managed = staging.bootstrap_registered_staging(blank)
    try:
        registration = managed.registration
        registration.chmod(0o644)
        with pytest.raises(profile_paths.ProfilePathError, match="private"):
            managed.revalidate()
        registration.chmod(0o600)
        (base / ".managed-staging.v1.pending").write_text("synthetic", encoding="utf-8")
        with pytest.raises(profile_paths.ProfilePathError, match="incomplete"):
            managed.revalidate()
    finally:
        managed.close()
        blank.close()


@LINUX_ONLY
def test_linux_kernel_acl_access_default_and_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base = _linux_tree(tmp_path, monkeypatch)
    marker = base / "profile.json"
    try:
        os.setxattr(marker, "system.posix_acl_access", _access_acl())
    except OSError as exc:
        pytest.fail(f"Linux test runner cannot exercise POSIX ACL xattrs: {exc}")
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with pytest.raises(profile_paths.ProfilePathError, match="ACL grants"):
        profile_paths.validate_linux_profile_paths(root, "synthetic")
    os.removexattr(marker, "system.posix_acl_access")

    blank = profile_paths.validate_linux_profile_paths(root, "synthetic")
    try:
        os.setxattr(base / "workspace", "system.posix_acl_default", _default_acl())
        with pytest.raises(profile_paths.ProfilePathError, match="ACL grants"):
            blank.revalidate()
    finally:
        blank.close()


@LINUX_ONLY
def test_linux_main_acl_revalidation_keeps_existing_posix_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _base = _linux_tree(tmp_path, monkeypatch)
    blank = profile_paths.validate_linux_profile_paths(root, "synthetic")
    initialize_profile_gate(blank)
    managed = staging.bootstrap_registered_staging(blank)
    fd = os.open(managed.staging_database, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
        managed.revalidate()
        os.setxattr(managed.staging_database, "system.posix_acl_access", _access_acl())
        assert stat.S_IMODE(managed.staging_database.stat().st_mode) == 0o600
        with pytest.raises(profile_paths.ProfilePathError, match="ACL grants"):
            managed.revalidate()
        os.removexattr(managed.staging_database, "system.posix_acl_access")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; f=os.open(sys.argv[1], os.O_RDWR); "
                "\ntry: fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)"
                "\nexcept BlockingIOError: sys.exit(2)"
                "\nsys.exit(0)",
                str(managed.staging_database),
            ],
            check=False,
            timeout=5,
            capture_output=True,
            text=True,
        )
        assert child.returncode == 2, child.stderr
    finally:
        os.close(fd)
        managed.close()
        blank.close()


def test_linux_acl_errors_fail_closed_without_opening_sqlite_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "synthetic.sqlite"
    marker.write_bytes(b"synthetic")
    marker.chmod(0o600)
    monkeypatch.setattr(profile_paths.sys, "platform", "linux")

    def unavailable(*_args: object, **_kwargs: object) -> bytes:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(profile_paths.os, "getxattr", unavailable, raising=False)
    with pytest.raises(profile_paths.ProfilePathError, match="Cannot inspect ACL"):
        profile_paths._check_regular_role(marker)

    def absent(*_args: object, **_kwargs: object) -> bytes:
        raise OSError(errno.ENODATA, "absent")

    monkeypatch.setattr(profile_paths.os, "getxattr", absent)
    opened: list[object] = []
    real_open = profile_paths.os.open

    def track_open(path: object, *args: object, **kwargs: object) -> int:
        opened.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(profile_paths.os, "open", track_open)
    profile_paths._check_regular_role(marker)
    assert marker not in opened


def test_cut_child_requires_one_trusted_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_CUT_APPLICATION_SUPPORT", "/tmp/Application Support")
    monkeypatch.setenv("FINANCE_CUT_LINUX_DATA_ROOT", "/tmp/finance-codex")
    monkeypatch.setenv("FINANCE_CUT_PROFILE_ID", "synthetic")
    monkeypatch.setenv("FINANCE_CUT_STAGE_PATH", "/tmp/ignored")
    request = SimpleNamespace(profile_id="synthetic")
    with pytest.raises(cut_protocol.ManagedCutProtocolError, match="locator"):
        cut_protocol.validate_profile(request, empty_stage=True)
