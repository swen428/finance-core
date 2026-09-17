"""Explicit private-runtime path authority for installed Finance Core."""

from __future__ import annotations

import os
import stat
from pathlib import Path

RUNTIME_ROOT_ENV = "FINANCE_RUNTIME_ROOT"


class RuntimePathConfigurationError(RuntimeError):
    """Raised when the private runtime root is absent or unsafe."""


def _trusted_owner(path: Path) -> bool:
    if not hasattr(os, "getuid"):
        return True
    owner = path.stat().st_uid
    return owner in {0, os.getuid()}


def _validate_directory_chain(path: Path) -> None:
    current = path
    while True:
        metadata = current.stat()
        mode = stat.S_IMODE(metadata.st_mode)
        writable = bool(mode & 0o022)
        trusted_sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if not current.is_dir() or not _trusted_owner(current) or (writable and not trusted_sticky):
            raise RuntimePathConfigurationError(
                f"{RUNTIME_ROOT_ENV} must be owner-controlled with a trusted directory chain"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def require_runtime_root() -> Path:
    """Return the explicitly configured private runtime root or fail closed."""

    value = os.environ.get(RUNTIME_ROOT_ENV)
    if value is None or not value.strip():
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV} is required; the installed package cannot infer a runtime root"
        )
    configured = Path(value)
    if not configured.is_absolute():
        raise RuntimePathConfigurationError(f"{RUNTIME_ROOT_ENV} must be an absolute path")
    try:
        resolved = configured.resolve(strict=True)
        metadata = configured.lstat()
    except OSError as exc:
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV} must name an existing runtime root"
        ) from exc
    if configured != resolved or stat.S_ISLNK(metadata.st_mode):
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV} must be canonical and contain no symbolic link"
        )
    _validate_directory_chain(resolved)
    return resolved


def live_database_path() -> Path:
    """Return the private live database path bound to the configured runtime root."""

    runtime_root = require_runtime_root()
    database_directory = runtime_root / "database"
    try:
        database_metadata = database_directory.lstat()
        resolved_database_directory = database_directory.resolve(strict=True)
    except OSError as exc:
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV}/database must be an existing owner-controlled directory"
        ) from exc
    if (
        stat.S_ISLNK(database_metadata.st_mode)
        or not database_directory.is_dir()
        or resolved_database_directory != database_directory
    ):
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV}/database must be canonical and contain no symbolic link"
        )
    _validate_directory_chain(database_directory)

    live_database = database_directory / "finance.db"
    try:
        live_metadata = live_database.lstat()
    except FileNotFoundError:
        return live_database
    except OSError as exc:
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV}/database/finance.db cannot be inspected safely"
        ) from exc
    if (
        stat.S_ISLNK(live_metadata.st_mode)
        or not live_database.is_file()
        or live_database.resolve(strict=True) != live_database
    ):
        raise RuntimePathConfigurationError(
            f"{RUNTIME_ROOT_ENV}/database/finance.db must be a canonical regular file"
        )
    return live_database


__all__ = [
    "RUNTIME_ROOT_ENV",
    "RuntimePathConfigurationError",
    "live_database_path",
    "require_runtime_root",
]
