"""Installed, hash-bound resources owned by :mod:`finance_core`."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

MIGRATION_PREFLIGHT_FILENAME = "migration_029_preflight.py"
MIGRATION_PREFLIGHT_SHA256 = "1fe3324447dbd128a80aadcafd54a70974d9c858cbc3a45a1bdaeae1cf48c295"
MIGRATION_PREFLIGHT_BYTE_COUNT = 55_851
MIGRATION_CONTRACT_FILENAME = "migration-contract-v1.json"
MIGRATION_CONTRACT_SCHEMA = "finance-core-migration-contract-v1"
MAX_MIGRATION_CONTRACT_BYTES = 64 * 1024


class MigrationResourceError(RuntimeError):
    """Raised when installed migration resources are absent or have drifted."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate migration contract field")
        result[key] = value
    return result


def _load_migration_contract() -> tuple[str, tuple[str, ...]]:
    path = Path(__file__).with_name(MIGRATION_CONTRACT_FILENAME)
    if path.is_symlink() or not path.is_file():
        raise MigrationResourceError("Migration contract resource is missing or unsafe")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MigrationResourceError("Migration contract resource cannot be read") from exc
    if not payload or len(payload) > MAX_MIGRATION_CONTRACT_BYTES:
        raise MigrationResourceError("Migration contract resource size is invalid")
    try:
        contract: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise MigrationResourceError("Migration contract resource is invalid JSON") from exc
    if not isinstance(contract, dict) or set(contract) != {
        "migration_filenames",
        "migration_ledger_digest",
        "schema",
    }:
        raise MigrationResourceError("Migration contract schema is invalid")
    digest = contract["migration_ledger_digest"]
    raw_filenames = contract["migration_filenames"]
    if (
        contract["schema"] != MIGRATION_CONTRACT_SCHEMA
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(raw_filenames, list)
        or not raw_filenames
        or any(not isinstance(filename, str) for filename in raw_filenames)
    ):
        raise MigrationResourceError("Migration contract values are invalid")
    filenames = tuple(filename for filename in raw_filenames if isinstance(filename, str))
    if len(filenames) != len(set(filenames)) or any(
        len(filename) < 9
        or not filename[:3].isdigit()
        or filename[3] != "_"
        or not filename.endswith(".sql")
        or "/" in filename
        or "\\" in filename
        for filename in filenames
    ):
        raise MigrationResourceError("Migration contract filename inventory is invalid")
    return digest, filenames


MIGRATION_LEDGER_DIGEST, MIGRATION_FILENAMES = _load_migration_contract()


@lru_cache(maxsize=1)
def migrations_dir() -> Path:
    """Return the filesystem directory containing installed migration resources."""

    resource_root = files("finance_core.resources").joinpath("migrations")
    if not isinstance(resource_root, Path):
        raise MigrationResourceError("Migration resources require a filesystem installation")
    root = resource_root.resolve()
    if not root.is_dir():
        raise MigrationResourceError("Migration resource directory is missing")
    return root


def _ledger_digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise MigrationResourceError(f"Migration resource cannot be read: {path.name}") from exc
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def migration_resource_paths() -> tuple[Path, ...]:
    """Return the exact ordered, checksum-verified migration ledger resources."""

    root = migrations_dir()
    expected_digest, expected_filenames = _load_migration_contract()
    observed = tuple(path.name for path in sorted(root.glob("*.sql")))
    if observed != expected_filenames:
        raise MigrationResourceError("Installed migration resource inventory has drifted")
    paths = tuple(root / name for name in expected_filenames)
    if _ledger_digest(paths) != expected_digest:
        raise MigrationResourceError("Installed migration resource bytes have drifted")
    return paths


@lru_cache(maxsize=1)
def migration_preflight_path() -> Path:
    """Return the exact checksum-bound migration 029 preflight resource."""

    path = migrations_dir().parent / "preflight" / MIGRATION_PREFLIGHT_FILENAME
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MigrationResourceError("Migration 029 preflight resource is missing") from exc
    if (
        len(payload) != MIGRATION_PREFLIGHT_BYTE_COUNT
        or hashlib.sha256(payload).hexdigest() != MIGRATION_PREFLIGHT_SHA256
    ):
        raise MigrationResourceError("Migration 029 preflight resource bytes have drifted")
    return path


__all__ = [
    "MIGRATION_FILENAMES",
    "MIGRATION_LEDGER_DIGEST",
    "MIGRATION_PREFLIGHT_BYTE_COUNT",
    "MIGRATION_PREFLIGHT_FILENAME",
    "MIGRATION_PREFLIGHT_SHA256",
    "MigrationResourceError",
    "migration_preflight_path",
    "migration_resource_paths",
    "migrations_dir",
]
