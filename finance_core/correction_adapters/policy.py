"""Owner-only local key and database-instance policy for controlled correction."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.runtime_paths import require_runtime_root
from finance_core.staging_guard import open_staging_database, require_staging_database

from .wire import CorrectionWireError, key_id

_POLICY_DIR = "correction_authority"
_POLICY_FILE = "policy.json"
_POLICY_KEYS = frozenset(
    {
        "schema",
        "key_hex",
        "key_id",
        "realm",
        "instance_id",
        "uid",
        "actor",
        "database_path",
        "database_dev",
        "database_ino",
        "database_uid",
        "database_mode",
    }
)
_CORRECTION_TABLES = (
    "correction_targets",
    "correction_plans",
    "correction_versions",
    "correction_authorities",
    "correction_receipt_facts",
)


class LocalPolicyError(RuntimeError):
    """A local owner, key, path or database witness cannot be trusted."""


@dataclass(frozen=True)
class LocalPolicy:
    key: bytes
    key_id: str
    realm: str
    instance_id: str
    uid: int
    actor: str
    database_path: Path
    database_dev: int
    database_ino: int
    database_uid: int
    database_mode: int


@dataclass(frozen=True)
class _RegisteredConnection:
    policy: LocalPolicy
    anchor_fd: int


_REGISTRY: dict[sqlite3.Connection, _RegisteredConnection] = {}
_REGISTRY_LOCK = threading.Lock()


def _trusted_directory(path: Path) -> None:
    current = path
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise LocalPolicyError("trusted directory is unavailable") from exc
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_mode & 0o022 and not sticky_root)
            or info.st_uid not in {0, os.getuid()}
            or current.resolve(strict=True) != current
        ):
            raise LocalPolicyError("policy directory chain is not owner controlled")
        if current == current.parent:
            return
        current = current.parent


def _policy_path() -> Path:
    return require_runtime_root() / _POLICY_DIR / _POLICY_FILE


def _database_witness(path: Path) -> tuple[int, int, int, int]:
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise LocalPolicyError("database path is not canonical")
        _trusted_directory(path.parent)
        info = path.lstat()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
        finally:
            os.close(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or info.st_nlink != 1
            or opened.st_nlink != 1
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise LocalPolicyError("database file witness is unsafe")
        return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)
    except OSError as exc:
        raise LocalPolicyError("database file witness is unavailable") from exc


def _connection_path(conn: sqlite3.Connection) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if row[1] == "main" and row[2]:
            return Path(str(row[2]))
    raise LocalPolicyError("local correction requires a file-backed database")


def _policy_witness(policy: LocalPolicy) -> tuple[int, int, int, int]:
    return (
        policy.database_dev,
        policy.database_ino,
        policy.database_uid,
        policy.database_mode,
    )


def _anchor_witness(fd: int) -> tuple[int, int, int, int]:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise LocalPolicyError("retained database descriptor is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LocalPolicyError("retained database descriptor is unsafe")
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


@contextmanager
def _open_anchored_staging(
    path: Path, witness: tuple[int, int, int, int]
) -> Iterator[tuple[sqlite3.Connection, int]]:
    """Hold a no-follow file witness across the staging connection open.

    Python's sqlite3 API does not expose its internal DB descriptor. This
    factory verifies the canonical path and retained descriptor before and
    after open, then keeps the descriptor for later checks. Same-UID malicious
    replacement and restore between those observations is outside the local
    host assurance model.
    """
    if _database_witness(path) != witness:
        raise LocalPolicyError("database instance changed before connection open")
    try:
        anchor_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise LocalPolicyError("database descriptor cannot be opened safely") from exc
    conn: sqlite3.Connection | None = None
    try:
        if _anchor_witness(anchor_fd) != witness:
            raise LocalPolicyError("database instance changed before connection open")
        conn = open_staging_database(path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        if (
            _connection_path(conn) != path
            or _anchor_witness(anchor_fd) != witness
            or _database_witness(path) != witness
        ):
            raise LocalPolicyError("database instance changed during connection open")
        yield conn, anchor_fd
    finally:
        if conn is not None:
            conn.close()
        os.close(anchor_fd)


@contextmanager
def open_local_authority_connection() -> Iterator[sqlite3.Connection]:
    """Sole trusted local Application composition connection factory.

    A connection is registered only during this context and is always closed
    and unregistered on exit. Caller-created SQLite connections cannot claim
    the local policy through ``load_policy_for_connection``.
    """
    policy = _read_policy()
    with _open_anchored_staging(policy.database_path, _policy_witness(policy)) as (
        conn,
        anchor_fd,
    ):
        with _REGISTRY_LOCK:
            if conn in _REGISTRY:
                raise LocalPolicyError("database connection is already registered")
            _REGISTRY[conn] = _RegisteredConnection(policy, anchor_fd)
        try:
            load_policy_for_connection(conn)
            yield conn
        finally:
            with _REGISTRY_LOCK:
                _REGISTRY.pop(conn, None)


def _empty_correction_ledger(conn: sqlite3.Connection) -> None:
    for table in _CORRECTION_TABLES:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        ):
            raise LocalPolicyError("correction schema 051 is incomplete")
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise LocalPolicyError("correction ledger already contains authority")
    from finance_core.application.correction_schema import verify_correction_schema

    if not verify_correction_schema(conn):
        raise LocalPolicyError("correction schema 051 is not installed")


def _strict_policy(raw: bytes) -> dict[str, object]:
    if len(raw) > 8192 or not raw:
        raise LocalPolicyError("policy has invalid size")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for name, value in items:
            if name in out:
                raise LocalPolicyError("policy contains duplicate keys")
            out[name] = value
        return out

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        if type(value) is not dict or set(value) != _POLICY_KEYS:
            raise LocalPolicyError("policy has invalid fields")
        if (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            != raw
        ):
            raise LocalPolicyError("policy encoding is not canonical")
        return value
    except (UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, LocalPolicyError):
            raise
        raise LocalPolicyError("policy JSON is invalid") from exc


def _read_policy() -> LocalPolicy:
    path = _policy_path()
    _trusted_directory(path.parent)
    try:
        info = path.lstat()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
                or info.st_nlink != 1
                or opened.st_nlink != 1
                or info.st_uid != os.getuid()
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise LocalPolicyError("policy file witness is unsafe")
            raw = os.read(fd, 8193)
        finally:
            os.close(fd)
    except OSError as exc:
        raise LocalPolicyError("local correction policy is missing or unreadable") from exc
    data = _strict_policy(raw)
    try:
        if data["schema"] != "correction-local-policy-v1":
            raise ValueError
        key_hex = data["key_hex"]
        if type(key_hex) is not str or len(key_hex) != 64:
            raise ValueError
        key = bytes.fromhex(key_hex)
        if key_hex != key.hex() or data["key_id"] != key_id(key):
            raise ValueError
        if type(data["uid"]) is not int or data["uid"] != os.getuid():
            raise ValueError
        if any(
            type(data[name]) is not str or not data[name]
            for name in ("realm", "instance_id", "actor", "database_path")
        ):
            raise ValueError
        numerics: dict[str, int] = {}
        for name in ("database_dev", "database_ino", "database_uid", "database_mode"):
            number = data[name]
            if type(number) is not int:
                raise ValueError
            parsed = cast(int, number)
            if parsed < 0:
                raise ValueError
            numerics[name] = parsed
        return LocalPolicy(
            key=key,
            key_id=str(data["key_id"]),
            realm=str(data["realm"]),
            instance_id=str(data["instance_id"]),
            uid=int(data["uid"]),
            actor=str(data["actor"]),
            database_path=Path(str(data["database_path"])),
            database_dev=numerics["database_dev"],
            database_ino=numerics["database_ino"],
            database_uid=numerics["database_uid"],
            database_mode=numerics["database_mode"],
        )
    except (ValueError, CorrectionWireError) as exc:
        raise LocalPolicyError("local correction policy has invalid authority fields") from exc


def load_policy_for_connection(conn: sqlite3.Connection) -> LocalPolicy:
    """Recheck a factory-registered connection, policy and retained file anchor."""
    with _REGISTRY_LOCK:
        registration = _REGISTRY.get(conn)
    if registration is None:
        raise LocalPolicyError("local authority requires a factory-registered connection")
    require_staging_database(conn)
    policy = _read_policy()
    if policy != registration.policy:
        raise LocalPolicyError("local approval policy changed during connection lifetime")
    opened_path = _connection_path(conn)
    if opened_path != policy.database_path:
        raise LocalPolicyError("opened database does not match policy path")
    if _anchor_witness(registration.anchor_fd) != _policy_witness(policy) or _database_witness(
        opened_path
    ) != _policy_witness(policy):
        raise LocalPolicyError("database instance does not match policy witness")
    return policy


def provision(database: Path, actor: str) -> LocalPolicy:
    """Exclusively create the first local policy for an empty schema-051 ledger."""
    if type(actor) is not str or not actor or len(actor.encode("utf-8")) > 1024:
        raise LocalPolicyError("original actor is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in actor):
        raise LocalPolicyError("original actor contains controls")
    root = require_runtime_root()
    if not database.is_absolute() or database.resolve(strict=True) != database:
        raise LocalPolicyError("database path must be canonical")
    witness = _database_witness(database)
    policy_path = root / _POLICY_DIR / _POLICY_FILE
    if policy_path.exists() or policy_path.is_symlink():
        raise LocalPolicyError("local correction policy already exists")
    with _open_anchored_staging(database, witness) as (conn, _anchor_fd):
        conn.execute("BEGIN IMMEDIATE")
        _empty_correction_ledger(conn)
        ledger = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id = '051' LIMIT 1"
        ).fetchone()
        if ledger is None:
            raise LocalPolicyError("migration 051 is not applied")
        conn.rollback()
    directory = policy_path.parent
    try:
        directory.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        _trusted_directory(directory)
        if (
            stat.S_IMODE(directory.stat().st_mode) != 0o700
            or directory.stat().st_uid != os.getuid()
        ):
            raise LocalPolicyError("policy directory must be owner-only") from None
    key = secrets.token_bytes(32)
    if _database_witness(database) != witness:
        raise LocalPolicyError("database instance changed during first policy setup")
    payload: dict[str, object] = {
        "schema": "correction-local-policy-v1",
        "key_hex": key.hex(),
        "key_id": key_id(key),
        "realm": secrets.token_hex(32),
        "instance_id": secrets.token_hex(32),
        "uid": os.getuid(),
        "actor": actor,
        "database_path": str(database),
        "database_dev": witness[0],
        "database_ino": witness[1],
        "database_uid": witness[2],
        "database_mode": witness[3],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    fd = os.open(
        policy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        written = 0
        while written < len(raw):
            count = os.write(fd, raw[written:])
            if count <= 0:
                raise LocalPolicyError("policy write did not complete")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return _read_policy()


def quarantine_incomplete_policy(database: Path) -> Path:
    """Manual first-policy recovery; retains malformed bytes as evidence.

    There is no CLI command for this. An owner must separately invoke it after
    reviewing the malformed file and its retained destination.
    """
    policy_path = _policy_path()
    _trusted_directory(policy_path.parent)
    try:
        _read_policy()
    except LocalPolicyError:
        pass
    else:
        raise LocalPolicyError("valid correction policy cannot be quarantined")
    if not database.is_absolute() or database.resolve(strict=True) != database:
        raise LocalPolicyError("database path is not canonical")
    witness = _database_witness(database)
    with _open_anchored_staging(database, witness) as (conn, _anchor_fd):
        conn.execute("BEGIN IMMEDIATE")
        _empty_correction_ledger(conn)
        destination = policy_path.with_name(f"policy.invalid.{secrets.token_hex(16)}.json")
        if not policy_path.exists() or policy_path.is_symlink():
            raise LocalPolicyError("malformed policy is not an owner-only regular file")
        info = policy_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise LocalPolicyError("malformed policy is not an owner-only regular file")
        os.link(policy_path, destination, follow_symlinks=False)
        policy_path.unlink()
        dir_fd = os.open(policy_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        conn.rollback()
        return destination
