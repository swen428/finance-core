"""Staging workspace access boundary for the bridge CLI.

Validates the external workspace path against the same safety model as the
B5.1a runner (absolute, external to the repository, non-symlink, never the
live database), opens the staging database through the existing
``open_staging_database`` contract with the full migration ledger, and owns
the bounded receipt-image handoff directory (design §7.3, §9).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from finance_core.openclaw_staging_bridge import errors
from finance_core.runtime_paths import RuntimePathConfigurationError, live_database_path
from finance_core.staging_guard import StagingDatabaseError, open_staging_database

HANDOFF_DIRNAME = "handoff"
COMMANDS_DIRNAME = "commands"
_REQUIRED_SUBDIRS = ("database", "attachments", "runtime", "evidence")
_DATABASE_FILENAME = "staging.sqlite"

MAX_WORKSPACE_PATH_LENGTH = 4_096
MAX_HANDOFF_FILENAME_LENGTH = 255
MAX_COMMAND_FILENAME_LENGTH = 255
MAX_COMMAND_FILE_BYTES = 262_144  # 256 KiB


def validate_workspace_path(raw_path: object) -> Path:
    """Validate the envelope workspace path and return the resolved directory."""
    if not isinstance(raw_path, str) or not raw_path or not raw_path.strip():
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "workspace_path must be a non-empty string.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if len(raw_path) > MAX_WORKSPACE_PATH_LENGTH:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "workspace_path exceeds the bounded length limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    path = Path(raw_path)
    if not path.is_absolute():
        raise errors.bridge_error(
            errors.WORKSPACE_REFUSED,
            "workspace_path must be absolute.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if os.path.islink(raw_path):
        raise errors.bridge_error(
            errors.WORKSPACE_REFUSED,
            "workspace_path must not be a symlink.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    resolved = path.resolve()
    try:
        live_db_path = live_database_path()
        runtime_root = live_db_path.parents[1]
    except RuntimePathConfigurationError as exc:
        raise errors.bridge_error(
            errors.WORKSPACE_REFUSED,
            f"Private runtime root is not safely configured: {exc}",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    try:
        resolved.relative_to(runtime_root)
        raise errors.bridge_error(
            errors.WORKSPACE_REFUSED,
            "workspace_path must be external to the runtime root.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    except ValueError:
        pass
    if resolved == live_db_path or resolved.is_relative_to(live_db_path.parent):
        raise errors.bridge_error(
            errors.WORKSPACE_REFUSED,
            "workspace_path must not target the live database location.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if not resolved.exists():
        raise errors.bridge_error(
            errors.WORKSPACE_MISSING,
            "Staging workspace does not exist.",
            errors.EXIT_USAGE,
        )
    if not resolved.is_dir():
        raise errors.bridge_error(
            errors.WORKSPACE_REFUSED,
            "workspace_path is not a directory.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return resolved


def database_path_for(workspace: Path) -> Path:
    return workspace / "database" / _DATABASE_FILENAME


def verify_workspace_structure(workspace: Path, *, require_handoff: bool = False) -> None:
    """Fail closed unless the expected private workspace directories exist."""
    required = list(_REQUIRED_SUBDIRS)
    if require_handoff:
        required.append(HANDOFF_DIRNAME)
    for name in required:
        sub = workspace / name
        if sub.is_symlink() or not sub.is_dir():
            raise errors.bridge_error(
                errors.WORKSPACE_REFUSED,
                f"Staging workspace subdirectory is missing or unsafe: {name}",
                errors.EXIT_VALIDATION_REFUSED,
            )


def open_workspace_database(workspace: Path) -> sqlite3.Connection:
    """Reopen the staging database through the existing guard and ledger check."""
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

    db_path = database_path_for(workspace)
    try:
        return open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    except StagingDatabaseError as exc:
        raise errors.bridge_error(
            errors.STAGING_REFUSED,
            f"Staging database refused: {exc}",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc
    except FileNotFoundError as exc:
        raise errors.bridge_error(
            errors.WORKSPACE_MISSING,
            "Staging database file does not exist.",
            errors.EXIT_USAGE,
        ) from exc


def ensure_handoff_directory(workspace: Path) -> Path:
    """Return the bounded handoff directory, creating it privately when needed."""
    handoff = workspace / HANDOFF_DIRNAME
    if handoff.is_symlink():
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "Handoff directory must not be a symlink.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if not handoff.exists():
        try:
            handoff.mkdir(mode=0o700, parents=False)
        except FileExistsError:
            raise errors.bridge_error(
                errors.HANDOFF_REFUSED,
                "Handoff directory appeared during creation (race).",
                errors.EXIT_VALIDATION_REFUSED,
            ) from None
        except OSError as exc:
            raise errors.bridge_error(
                errors.HANDOFF_REFUSED,
                f"Cannot create handoff directory: {exc}",
                errors.EXIT_VALIDATION_REFUSED,
            ) from None
    os.chmod(handoff, 0o700)
    if not handoff.is_dir():
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "Handoff path is not a directory.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return handoff


def validate_handoff_filename(value: object) -> str:
    """Validate a bounded handoff basename (no separators, no traversal)."""
    if not isinstance(value, str) or not value:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "handoff_filename must be a non-empty string.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if len(value) > MAX_HANDOFF_FILENAME_LENGTH:
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "handoff_filename exceeds the bounded length limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "handoff_filename must be a safe basename.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if value.startswith("."):
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "handoff_filename must not be hidden.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "handoff_filename contains control characters.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise errors.bridge_error(
            errors.HANDOFF_REFUSED,
            "handoff_filename must contain valid Unicode scalar values.",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    return value


def validate_command_filename(value: object) -> str:
    """Validate a bounded human-authored command basename (S4/D1b).

    Same safety shape as the handoff basename contract: no separators, no
    traversal, no hidden files, no control characters.
    """
    if not isinstance(value, str) or not value:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "command_filename must be a non-empty string.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if len(value) > MAX_COMMAND_FILENAME_LENGTH:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "command_filename exceeds the bounded length limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "command_filename must be a safe basename.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if value.startswith("."):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "command_filename must not be hidden.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "command_filename contains control characters.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return value


def read_command_file(workspace: Path, filename: str) -> bytes:
    """Read one human-authored command file from the workspace commands dir.

    S4/D1b delivery seam: the envelope carries only the bounded basename;
    the human-authored IAF command material lives out-of-envelope inside the
    external workspace.  The commands directory is a human surface — the
    bridge never creates, repairs, or mutates it or its files.  Fails closed
    for a missing or unsafe directory/file, a symlink anywhere on the path,
    or an oversized payload.
    """
    commands_dir = workspace / COMMANDS_DIRNAME
    if commands_dir.is_symlink() or not commands_dir.is_dir():
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Staging workspace has no safe commands directory.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    path = commands_dir / filename
    try:
        path.relative_to(commands_dir)
    except ValueError:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "command_filename escapes the commands directory.",
            errors.EXIT_VALIDATION_REFUSED,
        ) from None
    if path.is_symlink() or not path.is_file():
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Command file is missing or not a safe regular file.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"Command file cannot be inspected: {exc}",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    if size > MAX_COMMAND_FILE_BYTES:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Command file exceeds the bounded 256 KiB size limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    try:
        with open(path, "rb") as handle:
            content = handle.read(MAX_COMMAND_FILE_BYTES + 1)
    except OSError as exc:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"Command file cannot be read: {exc}",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    if len(content) > MAX_COMMAND_FILE_BYTES:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Command file exceeds the bounded 256 KiB size limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return content


__all__ = [
    "COMMANDS_DIRNAME",
    "HANDOFF_DIRNAME",
    "MAX_COMMAND_FILE_BYTES",
    "MAX_COMMAND_FILENAME_LENGTH",
    "MAX_HANDOFF_FILENAME_LENGTH",
    "MAX_WORKSPACE_PATH_LENGTH",
    "database_path_for",
    "ensure_handoff_directory",
    "open_workspace_database",
    "read_command_file",
    "validate_command_filename",
    "validate_handoff_filename",
    "validate_workspace_path",
    "verify_workspace_structure",
]
