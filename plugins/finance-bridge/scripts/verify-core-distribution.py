#!/usr/bin/env python3
"""Verify an installed Finance Core distribution against pinned release evidence."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import unquote, urlparse

HEX_40 = re.compile(r"[0-9a-f]{40}")
HEX_64 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CONTRACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MAX_FILES = 50_000
MAX_BYTES = 256 * 1024 * 1024
MANIFEST_NAME = "component-manifest-v1.json"
CHECKSUMS_NAME = "SHA256SUMS"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("release artifact filename must be a string")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or len(path.parts) != 1
        or path.as_posix() != value
        or value in {".", ".."}
    ):
        raise ValueError("release artifact filename is unsafe")
    return value


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} fields are not exact")
    return value


def _safe_regular_file(path: Path, root: Path) -> bytes:
    try:
        metadata = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"required distribution file is unavailable: {path.name}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or canonical.parent != root
        or metadata.st_mode & 0o022
        or (hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()})
    ):
        raise ValueError(f"distribution file is not owner-controlled: {path.name}")
    return path.read_bytes()


def _validate_distribution_inventory(root: Path, version: str, wheel_name: str) -> None:
    expected = {
        CHECKSUMS_NAME: "file",
        MANIFEST_NAME: "file",
        wheel_name: "file",
        "finance_core": "directory",
        f"finance_core-{version}.dist-info": "directory",
    }
    try:
        entries = {entry.name: entry for entry in root.iterdir()}
        if set(entries) != set(expected):
            raise ValueError("Core distribution root inventory is not exact")
        for name, kind in expected.items():
            path = entries[name]
            metadata = path.lstat()
            if (
                path.is_symlink()
                or metadata.st_mode & 0o022
                or (hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()})
                or (kind == "file" and not stat.S_ISREG(metadata.st_mode))
                or (kind == "directory" and not stat.S_ISDIR(metadata.st_mode))
            ):
                raise ValueError(f"Core distribution root entry is unsafe: {name}")
        metadata_root = entries[f"finance_core-{version}.dist-info"]
        for path in metadata_root.rglob("*"):
            metadata = path.lstat()
            if (
                path.is_symlink()
                or metadata.st_mode & 0o022
                or (hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()})
                or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))
            ):
                raise ValueError("installed distribution metadata contains an unsafe entry")
    except OSError as exc:
        raise ValueError("Core distribution root inventory could not be inspected") from exc


def _manifest(payload: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("component manifest is not valid UTF-8 JSON") from exc
    manifest = _exact_object(
        parsed,
        {
            "api_contract_version",
            "artifacts",
            "bridge_version",
            "core_commit",
            "core_version",
            "migration_ledger_digest",
            "schema",
        },
        "component manifest",
    )
    if manifest["schema"] != "finance-core-component-manifest-v1":
        raise ValueError("component manifest schema is invalid")
    core_version = manifest["core_version"]
    if not isinstance(core_version, str) or VERSION.fullmatch(core_version) is None:
        raise ValueError("component manifest core version is invalid")
    if manifest["bridge_version"] != core_version:
        raise ValueError("component manifest versions do not agree")
    if (
        not isinstance(manifest["core_commit"], str)
        or HEX_40.fullmatch(manifest["core_commit"]) is None
    ):
        raise ValueError("component manifest commit is invalid")
    if (
        not isinstance(manifest["migration_ledger_digest"], str)
        or HEX_64.fullmatch(manifest["migration_ledger_digest"]) is None
    ):
        raise ValueError("component manifest migration digest is invalid")
    if (
        not isinstance(manifest["api_contract_version"], str)
        or CONTRACT.fullmatch(manifest["api_contract_version"]) is None
    ):
        raise ValueError("component manifest API contract is invalid")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise ValueError("component manifest must name exactly three release artifacts")
    names: set[str] = set()
    for candidate in artifacts:
        entry = _exact_object(candidate, {"filename", "sha256", "size_bytes"}, "artifact")
        filename = _safe_filename(entry["filename"])
        if filename in names:
            raise ValueError("component manifest contains duplicate artifacts")
        names.add(filename)
        if not isinstance(entry["sha256"], str) or HEX_64.fullmatch(entry["sha256"]) is None:
            raise ValueError("component manifest artifact digest is invalid")
        if not isinstance(entry["size_bytes"], int) or not 0 < entry["size_bytes"] <= MAX_BYTES:
            raise ValueError("component manifest artifact size is invalid")
    wheel_names = [
        name
        for name in names
        if name.startswith(f"finance_core-{core_version}-") and name.endswith(".whl")
    ]
    if (
        len(wheel_names) != 1
        or f"finance_core-{core_version}.tar.gz" not in names
        or f"finance-codex-finance-bridge-{core_version}.tgz" not in names
    ):
        raise ValueError("component manifest artifact inventory is invalid")
    return manifest


def _checksums(payload: bytes, expected_names: set[str]) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("SHA256SUMS is not ASCII") from exc
    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            raise ValueError("SHA256SUMS contains an invalid line")
        filename = _safe_filename(match.group(2))
        if filename in entries:
            raise ValueError("SHA256SUMS contains duplicate filenames")
        entries[filename] = match.group(1)
    if set(entries) != expected_names:
        raise ValueError("SHA256SUMS inventory does not match the component manifest")
    return entries


def _safe_wheel_member(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in name.split("/"))
        or path.as_posix() != name
    ):
        raise ValueError("wheel contains an unsafe member")


def _wheel_payload(path: Path, version: str) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (
                not infos
                or len(infos) > MAX_FILES
                or len({item.filename for item in infos}) != len(infos)
            ):
                raise ValueError("wheel inventory is empty, duplicated, or too large")
            if sum(item.file_size for item in infos) > MAX_BYTES:
                raise ValueError("wheel uncompressed payload is too large")
            for info in infos:
                _safe_wheel_member(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("wheel contains a symbolic link")
            files = {item.filename: archive.read(item) for item in infos if not item.is_dir()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("wheel is not a valid ZIP archive") from exc
    metadata_name = f"finance_core-{version}.dist-info/METADATA"
    if metadata_name not in files:
        raise ValueError("wheel metadata is missing")
    metadata = BytesParser().parsebytes(files[metadata_name])
    if metadata.get("Name") != "finance-core" or metadata.get("Version") != version:
        raise ValueError("wheel metadata identity is invalid")
    unexpected = [
        name
        for name in files
        if not name.startswith("finance_core/")
        and not name.startswith(f"finance_core-{version}.dist-info/")
    ]
    if unexpected:
        raise ValueError("wheel contains an unexpected top-level payload")
    if "finance_core/__init__.py" not in files:
        raise ValueError("wheel Finance Core package is missing")
    return files


def _installed_payload(root: Path, version: str) -> dict[str, bytes]:
    payload_roots = (
        root / "finance_core",
        root / f"finance_core-{version}.dist-info",
    )
    try:
        files: dict[str, bytes] = {}
        total = 0
        for payload_root in payload_roots:
            root_metadata = payload_root.lstat()
            if (
                payload_root.is_symlink()
                or not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_mode & 0o022
                or (hasattr(os, "getuid") and root_metadata.st_uid not in {0, os.getuid()})
            ):
                raise ValueError("installed Finance Core distribution is unsafe")
            for path in sorted(payload_root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                metadata = path.lstat()
                if path.is_symlink() or metadata.st_mode & 0o022:
                    raise ValueError(f"installed Finance Core entry is unsafe: {relative}")
                if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
                    raise ValueError(
                        f"installed Finance Core entry has an unsafe owner: {relative}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"installed Finance Core entry is special: {relative}")
                payload = path.read_bytes()
                total += len(payload)
                if len(files) >= MAX_FILES or total > MAX_BYTES:
                    raise ValueError("installed Finance Core distribution exceeds its limits")
                files[relative] = payload
        return files
    except OSError as exc:
        raise ValueError("installed Finance Core distribution could not be inspected") from exc


def _verify_record(files: dict[str, bytes], record_name: str, label: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"), newline="")))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"{label} RECORD is unreadable") from exc
    if any(len(row) != 3 for row in rows):
        raise ValueError(f"{label} RECORD contains a malformed row")
    names = [row[0] for row in rows]
    if len(names) != len(set(names)) or set(names) != set(files):
        raise ValueError(f"{label} RECORD inventory does not match the distribution")
    for name, encoded_digest, encoded_size in rows:
        if name == record_name:
            if encoded_digest or encoded_size:
                raise ValueError(f"{label} RECORD must not hash itself")
            continue
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode()
        )
        if encoded_digest != f"sha256={digest}" or encoded_size != str(len(files[name])):
            raise ValueError(f"{label} RECORD does not authenticate {name}")


def _verify_installed_distribution(
    root: Path,
    wheel_path: Path,
    wheel_files: dict[str, bytes],
    installed_files: dict[str, bytes],
    version: str,
) -> None:
    metadata_root = f"finance_core-{version}.dist-info"
    record_name = f"{metadata_root}/RECORD"
    _verify_record(wheel_files, record_name, "wheel")
    generated = {
        f"{metadata_root}/INSTALLER",
        f"{metadata_root}/REQUESTED",
        f"{metadata_root}/direct_url.json",
    }
    installed_names = set(installed_files)
    wheel_names = set(wheel_files)
    if installed_names == wheel_names:
        if installed_files != wheel_files:
            raise ValueError("installed Finance Core distribution does not match the pinned wheel")
    elif installed_names == wheel_names | generated:
        for name in wheel_names - {record_name}:
            if installed_files[name] != wheel_files[name]:
                raise ValueError(
                    "installed Finance Core distribution does not match the pinned wheel"
                )
        if installed_files[f"{metadata_root}/INSTALLER"] != b"pip\n":
            raise ValueError("installed Finance Core installer identity is unexpected")
        if installed_files[f"{metadata_root}/REQUESTED"] != b"":
            raise ValueError("installed Finance Core requested marker is unexpected")
        try:
            direct_url = json.loads(installed_files[f"{metadata_root}/direct_url.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("installed Finance Core direct URL metadata is invalid") from exc
        expected_digest = _sha256_file(wheel_path)
        if not isinstance(direct_url, dict) or set(direct_url) != {"archive_info", "url"}:
            raise ValueError("installed Finance Core direct URL metadata is unexpected")
        archive_info = direct_url["archive_info"]
        if archive_info != {
            "hash": f"sha256={expected_digest}",
            "hashes": {"sha256": expected_digest},
        } or not isinstance(direct_url["url"], str):
            raise ValueError("installed Finance Core direct URL metadata is unexpected")
        parsed_url = urlparse(direct_url["url"])
        source_wheel = Path(unquote(parsed_url.path))
        if (
            parsed_url.scheme != "file"
            or parsed_url.netloc
            or parsed_url.params
            or parsed_url.query
            or parsed_url.fragment
            or not source_wheel.is_absolute()
            or source_wheel.name != wheel_path.name
            or source_wheel.is_symlink()
            or not source_wheel.is_file()
            or _sha256_file(source_wheel) != expected_digest
        ):
            raise ValueError("installed Finance Core direct URL metadata is unexpected")
    else:
        raise ValueError("installed Finance Core distribution inventory does not match the wheel")
    _verify_record(installed_files, record_name, "installed distribution")


def _literal_api_contract(package_init: bytes) -> str:
    try:
        module = ast.parse(package_init.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError("installed Finance Core API declaration is invalid") from exc
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "API_CONTRACT_VERSION"
            for target in statement.targets
        ):
            if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                return statement.value.value
    raise ValueError("installed Finance Core API contract is missing")


def _migration_digest(payloads: dict[str, bytes]) -> str:
    prefix = "finance_core/resources/migrations/"
    migrations = {
        PurePosixPath(name).name: body
        for name, body in payloads.items()
        if name.startswith(prefix) and name.endswith(".sql")
    }
    if [int(name[:3]) for name in sorted(migrations)] != list(range(1, 52)):
        raise ValueError("installed Finance Core migration inventory is not 001-051")
    digest = hashlib.sha256()
    for filename in sorted(migrations):
        body = migrations[filename]
        digest.update(filename.encode())
        digest.update(b"\0")
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--api-contract-version", required=True)
    parser.add_argument("--migration-ledger-digest", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    for value, pattern, label in (
        (args.manifest_sha256, HEX_64, "manifest digest"),
        (args.wheel_sha256, HEX_64, "wheel digest"),
        (args.core_commit, HEX_40, "core commit"),
        (args.core_version, VERSION, "core version"),
        (args.api_contract_version, CONTRACT, "API contract"),
        (args.migration_ledger_digest, HEX_64, "migration ledger digest"),
    ):
        if pattern.fullmatch(value) is None:
            raise SystemExit(f"expected {label} is invalid")
    try:
        root = args.root.resolve(strict=True)
        if root != args.root or args.root.is_symlink() or not root.is_dir():
            raise ValueError("Core distribution root must be canonical")
        manifest_bytes = _safe_regular_file(root / MANIFEST_NAME, root)
        checksums_bytes = _safe_regular_file(root / CHECKSUMS_NAME, root)
        manifest_sha256 = _sha256_bytes(manifest_bytes)
        if manifest_sha256 != args.manifest_sha256:
            raise ValueError("component manifest digest does not match the lock")
        manifest = _manifest(manifest_bytes)
        artifacts = cast(list[dict[str, object]], manifest["artifacts"])
        artifact_entries = {str(entry["filename"]): entry for entry in artifacts}
        checksums = _checksums(checksums_bytes, set(artifact_entries) | {MANIFEST_NAME})
        if checksums[MANIFEST_NAME] != manifest_sha256:
            raise ValueError("SHA256SUMS does not bind the component manifest")
        if any(checksums[name] != entry["sha256"] for name, entry in artifact_entries.items()):
            raise ValueError("SHA256SUMS does not bind the manifest artifacts")
        wheel_name = next(name for name in artifact_entries if name.endswith(".whl"))
        _validate_distribution_inventory(root, args.core_version, wheel_name)
        wheel_path = root / wheel_name
        _safe_regular_file(wheel_path, root)
        wheel_sha256 = _sha256_file(wheel_path)
        if (
            wheel_sha256 != args.wheel_sha256
            or artifact_entries[wheel_name]["sha256"] != wheel_sha256
            or artifact_entries[wheel_name]["size_bytes"] != wheel_path.stat().st_size
        ):
            raise ValueError("wheel digest or size does not match the release evidence")
        expected_manifest = {
            "core_version": args.core_version,
            "core_commit": args.core_commit,
            "api_contract_version": args.api_contract_version,
            "migration_ledger_digest": args.migration_ledger_digest,
        }
        if any(manifest[field] != expected for field, expected in expected_manifest.items()):
            raise ValueError("component manifest identity does not match the runtime lock")
        wheel_payload = _wheel_payload(wheel_path, args.core_version)
        installed_payload = _installed_payload(root, args.core_version)
        _verify_installed_distribution(
            root,
            wheel_path,
            wheel_payload,
            installed_payload,
            args.core_version,
        )
        package_payload = {
            name: body
            for name, body in installed_payload.items()
            if name.startswith("finance_core/")
        }
        api_contract = _literal_api_contract(package_payload["finance_core/__init__.py"])
        migration_digest = _migration_digest(package_payload)
        if (
            api_contract != args.api_contract_version
            or migration_digest != args.migration_ledger_digest
        ):
            raise ValueError("installed Finance Core semantic identity does not match the lock")
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "api_contract_version": args.api_contract_version,
                "core_commit": args.core_commit,
                "core_version": args.core_version,
                "manifest_sha256": args.manifest_sha256,
                "migration_ledger_digest": args.migration_ledger_digest,
                "schema": "finance-core-distribution-proof-v1",
                "wheel_sha256": args.wheel_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
