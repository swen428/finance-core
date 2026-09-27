"""Read-only path boundary for an explicitly configured Finance profile.

This module does not create a profile, open SQLite, or grant write authority.
Callers retain the returned object and call ``revalidate`` immediately before
using a path. The pinned descriptors keep the validated inodes alive so a
replacement cannot silently acquire their identity.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Self

from finance_core.runtime_paths import RUNTIME_ROOT_ENV

_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DIRECTORIES = (
    "Finance-Codex",
    "profiles",
    "profile",
    "runtime",
    "runtime/database",
    "workspace",
    "workspace/database",
    "backups",
    "work",
    "restore",
)


class ProfilePathError(RuntimeError):
    """A profile path, identity, or private permission is unsafe."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _trusted_ancestor(path: Path) -> None:
    """Validate the existing parent chain without following symbolic links."""
    current = path
    while True:
        info = current.lstat()
        mode = stat.S_IMODE(info.st_mode)
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid not in {0, os.getuid()})
            or ((mode & 0o022) and not sticky_root)
        ):
            raise ProfilePathError(f"Unsafe Application Support ancestor: {current}")
        if current.parent == current:
            break
        current = current.parent


def _open_checked(path: Path, *, directory: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        named = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode) or _identity(info) != _identity(named):
            raise ProfilePathError(f"Profile path changed or has the wrong type: {path}")
        if stat.S_ISLNK(named.st_mode):
            raise ProfilePathError(f"Profile path is a symbolic link: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProfilePathError(f"Profile path has the wrong owner: {path}")
        expected_mode = 0o700 if directory else 0o600
        if mode != expected_mode:
            raise ProfilePathError(f"Profile path must be private to its owner: {path}")
        if not directory and info.st_nlink != 1:
            raise ProfilePathError(f"Profile file has multiple hard links: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


class ProfilePaths:
    """Pinned, context-managed path identities; never a database authorization."""

    def __init__(self, profile_id: str, paths: dict[str, Path], pins: dict[str, int]) -> None:
        self.profile_id = profile_id
        self.application_support = paths["application_support"]
        self.profile = paths["profile"]
        self.profile_json = paths["profile_json"]
        self.runtime = paths["runtime"]
        self.live_database = paths["live_database"]
        self.workspace = paths["workspace"]
        self.staging_database = paths["staging_database"]
        self.backups = paths["backups"]
        self.work = paths["work"]
        self.restore = paths["restore"]
        self._paths = paths
        self._pins = pins

    def revalidate(self) -> None:
        """Recheck every pinned inode and permission before path-based use."""
        if not self._pins:
            raise ProfilePathError("Closed profile path witness")
        _trusted_ancestor(self.application_support)
        for name, fd in self._pins.items():
            path = self._paths[name]
            fresh = _open_checked(
                path, directory=name not in {"profile_json", "live_database", "staging_database"}
            )
            try:
                if _identity(os.fstat(fresh)) != _identity(os.fstat(fd)):
                    raise ProfilePathError(f"Profile path identity changed: {path}")
            finally:
                os.close(fresh)
        for name in ("live_database", "staging_database"):
            if name not in self._pins and os.path.lexists(self._paths[name]):
                raise ProfilePathError(
                    f"Profile file appeared after validation: {self._paths[name]}"
                )
        _validate_manifest(self)
        if os.environ.get(RUNTIME_ROOT_ENV) != str(self.runtime):
            raise ProfilePathError(f"{RUNTIME_ROOT_ENV} does not match the profile runtime")

    def close(self) -> None:
        for fd in self._pins.values():
            os.close(fd)
        self._pins.clear()

    def __enter__(self) -> Self:
        try:
            self.revalidate()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_args: object) -> None:
        self.close()


def _validate_manifest(profile: ProfilePaths) -> None:
    def unique_fields(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ProfilePathError("profile.json contains duplicate fields")
            result[key] = value
        return result

    try:
        payload = os.pread(profile._pins["profile_json"], 65_537, 0)
        if len(payload) > 65_536:
            raise ProfilePathError("profile.json exceeds the size limit")
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_fields)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProfilePathError("profile.json is invalid") from exc
    if not isinstance(data, dict) or (
        data.get("profile_id") != profile.profile_id
        or data.get("runtime_root") != str(profile.runtime)
        or data.get("workspace_root") != str(profile.workspace)
    ):
        raise ProfilePathError("profile.json does not match the validated profile paths")


def validate_profile_paths(application_support_root: str | Path, profile_id: str) -> ProfilePaths:
    """Validate one existing explicit profile without creating or opening data.

    ``application_support_root`` is an owner-provided absolute path ending in
    ``Application Support``. This accepts temporary synthetic trees in tests;
    the macOS owner must separately choose the real user's Library directory.
    """
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise ProfilePathError("Invalid profile ID")
    root = Path(application_support_root)
    if not root.is_absolute() or root.name != "Application Support":
        raise ProfilePathError("An absolute Application Support root is required")
    try:
        _trusted_ancestor(root)
        if root.resolve(strict=True) != root:
            raise ProfilePathError("Application Support path must be canonical")
        repository = Path(__file__).resolve().parents[1]
        if root == repository or root.is_relative_to(repository):
            raise ProfilePathError("A repository path cannot be a profile")
        if any(os.path.lexists(parent / ".git") for parent in (root, *root.parents)):
            raise ProfilePathError("A repository path cannot be a profile")
        base = root / "Finance-Codex" / "profiles" / profile_id
        paths = {
            "application_support": root,
            "Finance-Codex": root / "Finance-Codex",
            "profiles": root / "Finance-Codex" / "profiles",
            "profile": base,
            "runtime": base / "runtime",
            "runtime/database": base / "runtime" / "database",
            "workspace": base / "workspace",
            "workspace/database": base / "workspace" / "database",
            "backups": base / "backups",
            "work": base / "work",
            "restore": base / "restore",
            "profile_json": base / "profile.json",
            "live_database": base / "runtime" / "database" / "finance.db",
            "staging_database": base / "workspace" / "database" / "staging.sqlite",
        }
        pins: dict[str, int] = {}
        try:
            for name in _DIRECTORIES:
                pins[name] = _open_checked(paths[name], directory=True)
            for name in ("profile_json", "live_database", "staging_database"):
                if os.path.lexists(paths[name]):
                    pins[name] = _open_checked(paths[name], directory=False)
            if "profile_json" not in pins:
                raise ProfilePathError("profile.json is missing")
            result = ProfilePaths(profile_id, paths, pins)
            result.revalidate()
            return result
        except BaseException:
            for fd in pins.values():
                os.close(fd)
            raise
    except OSError as exc:
        raise ProfilePathError("Profile path is missing or unsafe") from exc


__all__ = ["ProfilePathError", "ProfilePaths", "validate_profile_paths"]
