"""Read-only path boundary for an explicitly configured Finance profile.

This module does not create a profile, open SQLite, or grant write authority.
Only a blank profile is admitted until native SQLite file admission exists.
Callers retain the returned object and call ``revalidate`` immediately before
using a path. The pinned descriptors keep the validated inodes alive so a
replacement cannot silently acquire their identity.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
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


@lru_cache(maxsize=1)
def _darwin_acl_library() -> ctypes.CDLL:
    """Load the native extended-ACL API; a missing symbol is a hard failure."""
    library = ctypes.CDLL(None, use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_valid.argtypes = [ctypes.c_void_p]
    library.acl_valid.restype = ctypes.c_int
    library.acl_get_entry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    library.acl_get_tag_type.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    return library


def _reject_acl_grants(fd: int, path: Path) -> None:
    """Reject Darwin extended ACL allow entries, including inherited grants.

    An absent ACL is reported as ENOENT. Deny-only ACLs, including the usual
    ``everyone deny delete`` ACE on macOS home directories, are acceptable.
    Other ACL inspection errors are never interpreted as an empty ACL.
    """
    if sys.platform != "darwin":
        return
    import errno

    try:
        library = _darwin_acl_library()
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:
                return
            raise ProfilePathError(f"Cannot inspect ACL for profile path: {path}")
        try:
            if library.acl_valid(acl) != 0:
                raise ProfilePathError(f"Invalid ACL on profile path: {path}")
            for index in range(129):  # Darwin ACL_MAX_ENTRIES is 128.
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                if library.acl_get_entry(acl, index, ctypes.byref(entry)) != 0:
                    if ctypes.get_errno() == errno.EINVAL:
                        return
                    raise ProfilePathError(f"Cannot enumerate ACL for profile path: {path}")
                if index == 128:
                    raise ProfilePathError(f"ACL has too many entries: {path}")
                tag = ctypes.c_int()
                if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                    raise ProfilePathError(f"Cannot inspect ACL entry for profile path: {path}")
                if tag.value != 2:  # ACL_EXTENDED_DENY; ALLOW is 1.
                    raise ProfilePathError(f"ACL grants access to profile path: {path}")
        finally:
            if library.acl_free(acl) != 0:
                raise ProfilePathError(f"Cannot release ACL for profile path: {path}")
    except (AttributeError, OSError) as exc:
        raise ProfilePathError(f"Cannot inspect ACL for profile path: {path}") from exc


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
        fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if _identity(os.fstat(fd)) != _identity(info):
                raise ProfilePathError(f"Application Support ancestor changed: {current}")
            _reject_acl_grants(fd, current)
        finally:
            os.close(fd)
        if current.parent == current:
            break
        current = current.parent


def _open_checked(path: Path, *, directory: bool) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
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
        _reject_acl_grants(fd, path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _require_blank_databases(paths: Mapping[str, Path]) -> None:
    """Never open an existing SQLite database through the path witness."""
    for name in ("live_database", "staging_database"):
        path = paths[name]
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProfilePathError(f"Cannot inspect reserved database path: {path}") from exc
        raise ProfilePathError(
            f"Existing profile database requires native SQLite admission: {path}"
        )


def _check_pinned_manifest(path: Path, fd: int) -> None:
    """Recheck the manifest without closing another descriptor on its inode."""
    try:
        info = os.fstat(fd)
        named = path.lstat()
    except OSError as exc:
        raise ProfilePathError(f"Profile path is missing or unsafe: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or _identity(info) != _identity(named):
        raise ProfilePathError(f"Profile path identity changed: {path}")
    if stat.S_ISLNK(named.st_mode):
        raise ProfilePathError(f"Profile path is a symbolic link: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ProfilePathError(f"Profile path has the wrong owner: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ProfilePathError(f"Profile path must be private to its owner: {path}")
    if info.st_nlink != 1:
        raise ProfilePathError(f"Profile file has multiple hard links: {path}")
    _reject_acl_grants(fd, path)


class ProfilePaths:
    """Pinned, context-managed path identities; never a database authorization."""

    __slots__ = ("_profile_id", "_paths", "_pins")

    def __init__(self, profile_id: str, paths: dict[str, Path], pins: dict[str, int]) -> None:
        self._profile_id = profile_id
        self._paths: Mapping[str, Path] = MappingProxyType(dict(paths))
        self._pins = pins

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def application_support(self) -> Path:
        return self._paths["application_support"]

    @property
    def profile(self) -> Path:
        return self._paths["profile"]

    @property
    def profile_json(self) -> Path:
        return self._paths["profile_json"]

    @property
    def runtime(self) -> Path:
        return self._paths["runtime"]

    @property
    def live_database(self) -> Path:
        return self._paths["live_database"]

    @property
    def workspace(self) -> Path:
        return self._paths["workspace"]

    @property
    def staging_database(self) -> Path:
        return self._paths["staging_database"]

    @property
    def backups(self) -> Path:
        return self._paths["backups"]

    @property
    def work(self) -> Path:
        return self._paths["work"]

    @property
    def restore(self) -> Path:
        return self._paths["restore"]

    def revalidate(self) -> None:
        """Recheck the blank profile and pinned identities before path-based use."""
        if not self._pins:
            raise ProfilePathError("Closed profile path witness")
        _trusted_ancestor(self.application_support)
        for name, fd in self._pins.items():
            if name == "profile_json":
                continue
            path = self._paths[name]
            fresh = _open_checked(path, directory=True)
            try:
                if _identity(os.fstat(fresh)) != _identity(os.fstat(fd)):
                    raise ProfilePathError(f"Profile path identity changed: {path}")
            finally:
                os.close(fresh)
        _require_blank_databases(self._paths)
        _check_pinned_manifest(self.profile_json, self._pins["profile_json"])
        _validate_manifest(self)
        _require_blank_databases(self._paths)
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
            _require_blank_databases(paths)
            if not os.path.lexists(paths["profile_json"]):
                raise ProfilePathError("profile.json is missing")
            pins["profile_json"] = _open_checked(paths["profile_json"], directory=False)
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
