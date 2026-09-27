"""D4 profile path boundary, using only disposable synthetic paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from finance_core.profile_paths import ProfilePathError, validate_profile_paths
from finance_core.staging_guard import StagingDatabaseError, create_staging_database


def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    support = tmp_path / "Application Support"
    support.mkdir(mode=0o700)
    base = support / "Finance-Codex" / "profiles" / "synthetic"
    for directory in (
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
    ):
        directory.mkdir(mode=0o700)
    (base / "profile.json").write_text(
        json.dumps(
            {
                "profile_id": "synthetic",
                "runtime_root": str(base / "runtime"),
                "workspace_root": str(base / "workspace"),
            }
        ),
        encoding="utf-8",
    )
    (base / "profile.json").chmod(0o600)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "runtime"))
    return support, base


def test_profile_paths_accept_explicit_synthetic_layout_and_preserve_staging_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    with validate_profile_paths(support, "synthetic") as profile:
        assert profile.runtime == base / "runtime"
        assert profile.workspace == base / "workspace"
        assert profile.staging_database == base / "workspace/database/staging.sqlite"
        assert profile.live_database == base / "runtime/database/finance.db"
        assert profile.backups == base / "backups"
        profile.revalidate()
        with pytest.raises(StagingDatabaseError, match="live database path"):
            create_staging_database(profile.live_database)
        assert not profile.live_database.exists()


@pytest.mark.parametrize("bad_id", ["../escape", ".hidden", "a/b", "A", "", "a" * 65])
def test_profile_id_cannot_escape_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    support, _ = _profile(tmp_path, monkeypatch)
    with pytest.raises(ProfilePathError, match="profile ID"):
        validate_profile_paths(support, bad_id)


def test_profile_rejects_mismatched_runtime_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "workspace"))
    with pytest.raises(ProfilePathError, match="FINANCE_RUNTIME_ROOT"):
        validate_profile_paths(support, "synthetic")
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(base / "runtime"))
    marker = base / "profile.json"
    marker.write_text('{"profile_id":"other"}', encoding="utf-8")
    with pytest.raises(ProfilePathError, match="profile.json"):
        validate_profile_paths(support, "synthetic")


def test_profile_rejects_symlink_hardlink_and_public_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    workspace = base / "workspace"
    workspace.rename(base / "actual-workspace")
    workspace.symlink_to(base / "actual-workspace", target_is_directory=True)
    with pytest.raises(ProfilePathError):
        validate_profile_paths(support, "synthetic")
    workspace.unlink()
    (base / "actual-workspace").rename(workspace)

    marker = base / "profile.json"
    os.link(marker, base / "extra-profile-link")
    with pytest.raises(ProfilePathError, match="hard links"):
        validate_profile_paths(support, "synthetic")
    (base / "extra-profile-link").unlink()

    (base / "backups").chmod(0o755)
    with pytest.raises(ProfilePathError, match="private"):
        validate_profile_paths(support, "synthetic")


def test_profile_rejects_database_aliases_and_replaced_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"synthetic")
    outside.chmod(0o600)
    staging = base / "workspace/database/staging.sqlite"
    staging.symlink_to(outside)
    with pytest.raises(ProfilePathError):
        validate_profile_paths(support, "synthetic")
    staging.unlink()
    os.link(outside, staging)
    with pytest.raises(ProfilePathError, match="hard links"):
        validate_profile_paths(support, "synthetic")
    staging.unlink()

    with validate_profile_paths(support, "synthetic") as profile:
        original = base / "backups"
        original.rename(base / "old-backups")
        original.mkdir(mode=0o700)
        with pytest.raises(ProfilePathError, match="identity changed"):
            profile.revalidate()


def test_profile_rejects_repository_disguise_and_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, _ = _profile(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.symlink_to(support, target_is_directory=True)
    with pytest.raises(ProfilePathError, match="Application Support"):
        validate_profile_paths(alias, "synthetic")
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / ".git").mkdir(mode=0o700)
    nested_support = repository / "Application Support"
    nested_support.mkdir(mode=0o700)
    with pytest.raises(ProfilePathError, match="repository"):
        validate_profile_paths(nested_support, "synthetic")
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", "")
    with pytest.raises(ProfilePathError, match="FINANCE_RUNTIME_ROOT"):
        validate_profile_paths(support, "synthetic")


def test_profile_witness_refuses_new_unpinned_database_and_duplicate_manifest_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    with validate_profile_paths(support, "synthetic") as profile:
        profile.staging_database.write_bytes(b"synthetic")
        profile.staging_database.chmod(0o600)
        with pytest.raises(ProfilePathError, match="appeared"):
            profile.revalidate()
    (base / "workspace/database/staging.sqlite").unlink()
    (base / "profile.json").write_text(
        '{"profile_id":"synthetic","profile_id":"synthetic"}', encoding="utf-8"
    )
    with pytest.raises(ProfilePathError, match="duplicate"):
        validate_profile_paths(support, "synthetic")
