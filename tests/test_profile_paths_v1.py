"""D4 profile path boundary, using only disposable synthetic paths."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
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


@pytest.mark.parametrize(
    "relative_path",
    ["profile.json", "runtime/database/finance.db", "workspace/database/staging.sqlite"],
)
def test_profile_fifo_is_refused_without_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    fifo = base / relative_path
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo, 0o600)
    code = (
        "import sys; from finance_core.profile_paths import ProfilePathError, "
        "validate_profile_paths; "
        "\ntry: validate_profile_paths(sys.argv[1], 'synthetic')"
        "\nexcept ProfilePathError: sys.exit(0)"
        "\nsys.exit(1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(support)],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr


def test_profile_paths_are_read_only_and_match_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, base = _profile(tmp_path, monkeypatch)
    with validate_profile_paths(support, "synthetic") as profile:
        with pytest.raises(AttributeError):
            profile.runtime = tmp_path  # type: ignore[misc]
        with pytest.raises(AttributeError):
            profile.profile_id = "other"  # type: ignore[misc]
        assert profile.runtime == base / "runtime"
        profile.revalidate()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL only")
@pytest.mark.parametrize(
    "relative_path",
    [
        ".",
        "Finance-Codex/profiles/synthetic/runtime",
        "Finance-Codex/profiles/synthetic/profile.json",
    ],
)
def test_profile_rejects_acl_allow_even_when_mode_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    support, _ = _profile(tmp_path, monkeypatch)
    target = support / relative_path
    mode = stat.S_IMODE(target.stat().st_mode)
    subprocess.run(["chmod", "+a", "everyone allow read", str(target)], check=True)
    assert stat.S_IMODE(target.stat().st_mode) == mode
    with pytest.raises(ProfilePathError, match="ACL grants"):
        validate_profile_paths(support, "synthetic")


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL only")
def test_profile_allows_deny_only_acl_and_rejects_acl_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, _ = _profile(tmp_path, monkeypatch)
    subprocess.run(["chmod", "+a", "everyone deny delete", str(support)], check=True)
    with validate_profile_paths(support, "synthetic") as profile:
        profile.revalidate()

    import finance_core.profile_paths as module

    def unavailable() -> object:
        raise OSError("synthetic ACL inspection failure")

    monkeypatch.setattr(module, "_darwin_acl_library", unavailable)
    with pytest.raises(ProfilePathError, match="Cannot inspect ACL"):
        validate_profile_paths(support, "synthetic")


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL only")
def test_profile_rejects_allow_entry_after_deny_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, _ = _profile(tmp_path, monkeypatch)
    subprocess.run(["chmod", "+a", "everyone deny delete", str(support)], check=True)
    subprocess.run(["chmod", "+a", "everyone allow read", str(support)], check=True)
    with pytest.raises(ProfilePathError, match="ACL grants"):
        validate_profile_paths(support, "synthetic")
