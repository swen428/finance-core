from __future__ import annotations

from pathlib import Path

import pytest

from finance_core.openclaw_staging_bridge import errors, workspace_access
from finance_core.receipt_staging_runner import workspace as runner_workspace
from finance_core.receipt_staging_runner.models import RunnerWorkspaceError
from finance_core.runtime_paths import RuntimePathConfigurationError, require_runtime_root
from finance_core.staging_guard import StagingDatabaseError, create_staging_database


def _make_runtime_root(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "private-runtime"
    runtime_root.mkdir(mode=0o700)
    return runtime_root.resolve()


def test_runtime_root_is_explicit_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCE_RUNTIME_ROOT", raising=False)

    with pytest.raises(RuntimePathConfigurationError, match="FINANCE_RUNTIME_ROOT"):
        require_runtime_root()
    with pytest.raises(RunnerWorkspaceError, match="runtime root"):
        runner_workspace._validate_workspace_path("/tmp/external-finance-workspace")
    with pytest.raises(errors.BridgeError, match="runtime root"):
        workspace_access.validate_workspace_path("/tmp/external-finance-workspace")
    with pytest.raises(StagingDatabaseError, match="runtime root"):
        create_staging_database("/tmp/external-finance-staging.sqlite")


def test_installed_core_rejects_runtime_tree_and_live_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    database_dir = runtime_root / "database"
    database_dir.mkdir(mode=0o700)
    external_workspace = tmp_path / "external-workspace"
    external_workspace.mkdir(mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root))

    assert require_runtime_root() == runtime_root
    for candidate in (runtime_root, database_dir, runtime_root / "private-evidence"):
        with pytest.raises(RunnerWorkspaceError, match="inside the runtime root"):
            runner_workspace._validate_workspace_path(str(candidate))
        with pytest.raises(errors.BridgeError, match="runtime root"):
            workspace_access.validate_workspace_path(str(candidate))

    assert runner_workspace._validate_workspace_path(str(external_workspace)) == external_workspace
    assert workspace_access.validate_workspace_path(str(external_workspace)) == external_workspace

    live_database = database_dir / "finance.db"
    with pytest.raises(StagingDatabaseError, match="live database path"):
        create_staging_database(live_database)
    assert not live_database.exists()


def test_runtime_root_rejects_symlink_and_unsafe_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    runtime_alias = tmp_path / "runtime-alias"
    runtime_alias.symlink_to(runtime_root, target_is_directory=True)

    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_alias))
    with pytest.raises(RuntimePathConfigurationError, match="canonical"):
        require_runtime_root()

    runtime_root.chmod(0o770)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root))
    with pytest.raises(RuntimePathConfigurationError, match="owner-controlled"):
        require_runtime_root()


def test_live_database_path_rejects_symlinked_database_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    external_database_directory = tmp_path / "external-database"
    external_database_directory.mkdir(mode=0o700)
    (runtime_root / "database").symlink_to(
        external_database_directory,
        target_is_directory=True,
    )
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root))

    with pytest.raises(StagingDatabaseError, match="database.*symbolic link"):
        create_staging_database(runtime_root / "database" / "finance.db")
    assert not (external_database_directory / "finance.db").exists()


def test_live_database_path_rejects_symlinked_database_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _make_runtime_root(tmp_path)
    database_directory = runtime_root / "database"
    database_directory.mkdir(mode=0o700)
    external_database = tmp_path / "external-finance.db"
    external_database.write_bytes(b"")
    (database_directory / "finance.db").symlink_to(external_database)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root))

    with pytest.raises(StagingDatabaseError, match="canonical regular file"):
        create_staging_database(database_directory / "finance.db")
    assert external_database.read_bytes() == b""
