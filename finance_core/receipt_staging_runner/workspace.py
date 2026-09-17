"""B5.1a external workspace creation and validation.

Creates a disposable staging workspace outside the Git repository with
explicit absolute paths, bounded permissions, and no silent overwrite.
The workspace is never placed inside the repository root, never at the
live database path, and never through a symlink.

Workspace creation also generates the OpenClaw staging-bridge callback
signing key (0600, outside Git, never overwritten) under ``runtime/``;
recovery verifies that key without creating, replacing, or reading it into
any report.  The key bytes never appear in envelopes, logs, tests, reports,
or audit evidence; loss of the key invalidates outstanding callback tokens
fail-closed.

No recursive delete or cleanup command is provided; cleanup is an explicit
operator action outside this boundary.

See ``docs/design/b5_1a_receipt_staging_runner_foundation_v1.md`` and
``docs/design/openclaw_telegram_staging_bridge_v1.md`` §7.4/§9.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from finance_core.receipt_staging_runner.models import (
    CallbackKeyMissingError,
    RunnerInputManifest,
    RunnerWorkspace,
    RunnerWorkspaceError,
)
from finance_core.runtime_paths import RuntimePathConfigurationError, live_database_path

# ---------------------------------------------------------------------------
# Workspace layout constants
# ---------------------------------------------------------------------------

_DATABASE_DIR = "database"
_ATTACHMENTS_DIR = "attachments"
_RUNTIME_DIR = "runtime"
_EVIDENCE_DIR = "evidence"
_MANIFEST_FILE = "manifest.json"
_MANIFEST_HASH_FILE = "manifest.sha256"

_WORKSPACE_DIRS = (_DATABASE_DIR, _ATTACHMENTS_DIR, _RUNTIME_DIR, _EVIDENCE_DIR)

_DATABASE_FILENAME = "staging.sqlite"

_CALLBACK_KEY_FILENAME = "callback_signing.key"
CALLBACK_SIGNING_KEY_BYTES = 32

# Bounded key status values reported by health checks; never key material.
CALLBACK_KEY_PRESENT = "present"
CALLBACK_KEY_MISSING = "missing"
CALLBACK_KEY_UNSAFE = "unsafe"

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _validate_workspace_path(raw_path: str) -> Path:
    """Resolve and validate the workspace path against safety constraints.

    Rejects:
    - empty or relative paths
    - symlinks at the final path component (checked before resolve)
    - paths inside the private runtime root
    - the live database path
    - existing non-directory filesystem entries
    """
    if not raw_path or not raw_path.strip():
        raise RunnerWorkspaceError("Workspace path must not be empty")

    path = Path(raw_path)
    if not path.is_absolute():
        raise RunnerWorkspaceError(f"Workspace path must be absolute: {raw_path!r}")

    # Reject symlinks at the final component BEFORE resolve.
    # We check the raw path itself (not system ancestors like /tmp on macOS).
    if os.path.islink(raw_path):
        raise RunnerWorkspaceError(f"Workspace path is a symlink: {raw_path!r}")

    resolved = path.resolve()

    try:
        runtime_root = live_database_path().parents[1]
        live_db_path = live_database_path()
    except RuntimePathConfigurationError as exc:
        raise RunnerWorkspaceError(f"Private runtime root is not safely configured: {exc}") from exc

    # Preserve the most precise refusal for the authoritative live database.
    if resolved == live_db_path:
        raise RunnerWorkspaceError(f"Workspace path must not be the live database: {live_db_path}")

    # Reject the private runtime root and any descendant.
    try:
        resolved.relative_to(runtime_root)
        raise RunnerWorkspaceError(
            f"Workspace path {resolved} is inside the runtime root "
            f"{runtime_root}. The staging workspace must be external."
        )
    except ValueError:
        pass  # Not inside the runtime root — expected.

    return resolved


def _reject_symlink_at(path: Path) -> None:
    """Reject if the exact path is a symlink (lstat-based)."""
    if path.is_symlink():
        raise RunnerWorkspaceError(f"Path is a symlink: {path}")


# ---------------------------------------------------------------------------
# Workspace initialization
# ---------------------------------------------------------------------------


def create_runner_workspace(
    workspace_path: str,
    manifest: RunnerInputManifest,
) -> RunnerWorkspace:
    """Create a new disposable staging workspace.

    The workspace directory and all private subdirectories are created with
    mode 0700.  The manifest is persisted with its exact-byte SHA-256 hash.
    No file is silently overwritten; any pre-existing entry causes failure.

    Parameters
    ----------
    workspace_path:
        Absolute external path for the new workspace.
    manifest:
        Validated runner input manifest.

    Returns
    -------
    RunnerWorkspace
        Immutable result with resolved paths and manifest hash.

    Raises
    ------
    RunnerWorkspaceError
        If the path is unsafe or any filesystem entry already exists.
    """
    resolved = _validate_workspace_path(workspace_path)

    if resolved.exists():
        raise RunnerWorkspaceError(
            f"Workspace path already exists: {resolved}. "
            "Refusing to overwrite. Use a fresh path or clean up explicitly."
        )

    # Create workspace root with 0700.
    try:
        resolved.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        raise RunnerWorkspaceError(
            f"Workspace path appeared during creation (race): {resolved}"
        ) from None
    except OSError as exc:
        raise RunnerWorkspaceError(f"Cannot create workspace at {resolved}: {exc}") from None

    # Enforce 0700 regardless of umask.
    os.chmod(resolved, 0o700)

    # Create private subdirectories.
    for dir_name in _WORKSPACE_DIRS:
        sub = resolved / dir_name
        try:
            sub.mkdir(mode=0o700, parents=False)
            os.chmod(sub, 0o700)
        except FileExistsError:
            raise RunnerWorkspaceError(f"Workspace subdirectory already exists: {sub}") from None

    # Persist manifest bytes (exact, no re-serialization).
    manifest_path = resolved / _MANIFEST_FILE
    _write_exclusive(manifest_path, manifest.raw_bytes, mode=0o600)

    # Persist manifest hash.
    hash_content = f"{manifest.manifest_sha256}  {_MANIFEST_FILE}\n".encode("utf-8")
    hash_path = resolved / _MANIFEST_HASH_FILE
    _write_exclusive(hash_path, hash_content, mode=0o600)

    # Generate the callback signing key exclusively under runtime/ (0600).
    generate_callback_signing_key(str(resolved / _RUNTIME_DIR))

    db_path = resolved / _DATABASE_DIR / _DATABASE_FILENAME
    return RunnerWorkspace(
        workspace_path=str(resolved),
        database_path=str(db_path),
        attachments_path=str(resolved / _ATTACHMENTS_DIR),
        runtime_path=str(resolved / _RUNTIME_DIR),
        evidence_path=str(resolved / _EVIDENCE_DIR),
        manifest_hash=manifest.manifest_sha256,
        workspace_identity=manifest.workspace_identity,
        callback_key_path=str(resolved / _RUNTIME_DIR / _CALLBACK_KEY_FILENAME),
    )


def _write_exclusive(path: Path, data: bytes, *, mode: int) -> None:
    """Write bytes to a file exclusively; fail if it already exists."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, mode)
    except FileExistsError:
        raise RunnerWorkspaceError(f"Refusing to overwrite existing file: {path}") from None
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    # Enforce permissions regardless of umask.
    os.chmod(path, mode)


# ---------------------------------------------------------------------------
# Workspace recovery (reopen existing)
# ---------------------------------------------------------------------------


def recover_runner_workspace(
    workspace_path: str,
    manifest: RunnerInputManifest,
) -> RunnerWorkspace:
    """Recover an existing workspace and verify manifest identity.

    Verifies:
    - workspace exists and is a directory (not a symlink)
    - persisted manifest hash matches the supplied manifest
    - all expected subdirectories exist

    Does not create, modify, or delete any file.

    Raises
    ------
    RunnerWorkspaceError
        If the workspace is missing, is a symlink, or the manifest hash
        does not match the persisted identity.
    """
    resolved = _validate_workspace_path(workspace_path)

    if not resolved.exists():
        raise RunnerWorkspaceError(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise RunnerWorkspaceError(f"Workspace path is not a directory: {resolved}")
    _reject_symlink_at(resolved)

    # Verify persisted manifest hash.
    hash_path = resolved / _MANIFEST_HASH_FILE
    if not hash_path.exists():
        raise RunnerWorkspaceError(f"Workspace manifest hash file is missing: {hash_path}")
    persisted_hash_content = hash_path.read_text(encoding="utf-8").strip()
    persisted_hash = persisted_hash_content.split()[0] if persisted_hash_content else ""
    if persisted_hash != manifest.manifest_sha256:
        raise RunnerWorkspaceError(
            f"Manifest hash mismatch: workspace has {persisted_hash!r} "
            f"but supplied manifest has {manifest.manifest_sha256!r}"
        )

    # Verify persisted manifest bytes hash.
    manifest_path = resolved / _MANIFEST_FILE
    if not manifest_path.exists():
        raise RunnerWorkspaceError(f"Workspace manifest file is missing: {manifest_path}")
    persisted_raw = manifest_path.read_bytes()
    persisted_raw_hash = hashlib.sha256(persisted_raw).hexdigest()
    if persisted_raw_hash != manifest.manifest_sha256:
        raise RunnerWorkspaceError(
            "Persisted manifest bytes do not match the supplied manifest hash"
        )

    # Verify subdirectories.
    for dir_name in _WORKSPACE_DIRS:
        sub = resolved / dir_name
        if not sub.is_dir():
            raise RunnerWorkspaceError(f"Workspace subdirectory missing: {sub}")

    # Verify the callback signing key without exposing its bytes.
    verify_callback_signing_key(str(resolved / _RUNTIME_DIR))

    db_path = resolved / _DATABASE_DIR / _DATABASE_FILENAME
    return RunnerWorkspace(
        workspace_path=str(resolved),
        database_path=str(db_path),
        attachments_path=str(resolved / _ATTACHMENTS_DIR),
        runtime_path=str(resolved / _RUNTIME_DIR),
        evidence_path=str(resolved / _EVIDENCE_DIR),
        manifest_hash=manifest.manifest_sha256,
        workspace_identity=manifest.workspace_identity,
        callback_key_path=str(resolved / _RUNTIME_DIR / _CALLBACK_KEY_FILENAME),
    )


# ---------------------------------------------------------------------------
# Callback signing key (OpenClaw staging bridge S3)
# ---------------------------------------------------------------------------


def _validate_runtime_dir(runtime_path: str) -> Path:
    """Resolve the workspace runtime directory and enforce its private contract."""
    path = Path(runtime_path)
    if not path.is_absolute():
        raise RunnerWorkspaceError(f"Runtime path must be absolute: {runtime_path!r}")
    if path.is_symlink():
        raise RunnerWorkspaceError(f"Runtime path is a symlink: {path}")
    if not path.is_dir():
        raise RunnerWorkspaceError(f"Runtime directory does not exist: {path}")
    st = os.lstat(path)
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise RunnerWorkspaceError(f"Runtime directory permissions are unsafe: {path}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise RunnerWorkspaceError(f"Runtime directory is not owned by the current user: {path}")
    return path


def generate_callback_signing_key(runtime_path: str) -> Path:
    """Generate the callback signing key exclusively under ``runtime/``.

    The key is ``CALLBACK_SIGNING_KEY_BYTES`` random bytes written with
    ``O_CREAT | O_EXCL`` at mode 0600.  An existing key is never overwritten.
    """
    runtime = _validate_runtime_dir(runtime_path)
    key_path = runtime / _CALLBACK_KEY_FILENAME
    if key_path.is_symlink():
        raise RunnerWorkspaceError(f"Callback key path is a symlink: {key_path}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(key_path), flags, 0o600)
    except FileExistsError:
        raise RunnerWorkspaceError(
            f"Refusing to overwrite existing callback signing key: {key_path}"
        ) from None
    except OSError as exc:
        raise RunnerWorkspaceError(f"Cannot create callback signing key: {exc}") from None
    try:
        os.write(fd, secrets.token_bytes(CALLBACK_SIGNING_KEY_BYTES))
    except OSError as exc:
        raise RunnerWorkspaceError(f"Cannot write callback signing key: {exc}") from None
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return key_path


def verify_callback_signing_key(runtime_path: str) -> Path:
    """Verify the persisted callback signing key without returning its bytes.

    Requires a direct regular non-symlink file at mode 0600, owned by the
    current user, holding exactly ``CALLBACK_SIGNING_KEY_BYTES`` bytes.
    """
    runtime = _validate_runtime_dir(runtime_path)
    key_path = runtime / _CALLBACK_KEY_FILENAME
    if key_path.is_symlink():
        raise RunnerWorkspaceError(f"Callback key path is a symlink: {key_path}")
    if not key_path.is_file():
        raise CallbackKeyMissingError(f"Callback signing key is missing: {key_path}")
    st = os.lstat(key_path)
    if not stat.S_ISREG(st.st_mode):
        raise RunnerWorkspaceError(f"Callback signing key is not a regular file: {key_path}")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise RunnerWorkspaceError(f"Callback signing key permissions are unsafe: {key_path}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise RunnerWorkspaceError(
            f"Callback signing key is not owned by the current user: {key_path}"
        )
    if st.st_size != CALLBACK_SIGNING_KEY_BYTES:
        raise RunnerWorkspaceError(f"Callback signing key length is unsafe: {key_path}")
    return key_path


def load_callback_signing_key(runtime_path: str) -> bytes:
    """Verify and read the callback signing key for signature operations.

    The returned bytes are used only in-memory for HMAC computation and must
    never be serialized into envelopes, logs, reports, or audit evidence.
    """
    key_path = verify_callback_signing_key(runtime_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(key_path), flags)
    except OSError as exc:
        raise RunnerWorkspaceError(f"Cannot open callback signing key: {exc}") from None
    try:
        key_bytes = os.read(fd, CALLBACK_SIGNING_KEY_BYTES + 1)
    except OSError as exc:
        raise RunnerWorkspaceError(f"Cannot read callback signing key: {exc}") from None
    finally:
        os.close(fd)
    if len(key_bytes) != CALLBACK_SIGNING_KEY_BYTES:
        raise RunnerWorkspaceError("Callback signing key length changed during read.")
    return key_bytes


def callback_key_status(runtime_path: str) -> str:
    """Return a bounded key status for health reporting: present/missing/unsafe.

    Never reads or reports key material.
    """
    try:
        verify_callback_signing_key(runtime_path)
    except CallbackKeyMissingError:
        return CALLBACK_KEY_MISSING
    except RunnerWorkspaceError:
        return CALLBACK_KEY_UNSAFE
    return CALLBACK_KEY_PRESENT


__all__ = [
    "CALLBACK_KEY_MISSING",
    "CALLBACK_KEY_PRESENT",
    "CALLBACK_KEY_UNSAFE",
    "CALLBACK_SIGNING_KEY_BYTES",
    "callback_key_status",
    "create_runner_workspace",
    "generate_callback_signing_key",
    "load_callback_signing_key",
    "recover_runner_workspace",
    "verify_callback_signing_key",
]
