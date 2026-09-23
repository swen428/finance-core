#!/usr/bin/env python3
"""Validate and bind exact Finance Core release artifacts."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import re
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

HEX_40 = re.compile(r"[0-9a-f]{40}")
HEX_64 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
MAX_ARCHIVE_FILES = 50_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
OUTPUT_FILENAMES = {"component-manifest-v1.json", "SHA256SUMS"}
BRIDGE_SOURCE_POLICY = "finance-plugin-build-source-v3"
BRIDGE_SOURCE_SINGLE_FILES = {
    "binding.gyp",
    "npm-shrinkwrap.json",
    "openclaw.plugin.json",
    "package.json",
    "tsconfig.json",
}
BRIDGE_SOURCE_DIRECTORIES = {"native", "scripts", "src", "types"}
BRIDGE_PACKAGE_SINGLE_FILES = BRIDGE_SOURCE_SINGLE_FILES | {
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
}
BRIDGE_PROVENANCE_PATH = "dist/build-provenance-v1.json"
CORE_RELEASE_SOURCE_FILES = {
    "LICENSE",
    "MANIFEST.in",
    "NOTICE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
}
RELEASE_TOOL_SOURCE_FILES = {"scripts/build_release_manifest.py"}
SDIST_GENERATED_ROOT_FILES = {
    "setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
}
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "requires.txt",
    "top_level.txt",
}
PYTHON_METADATA_SINGLE_FIELDS = {
    "Metadata-Version",
    "Name",
    "Version",
    "Summary",
    "License-Expression",
    "Requires-Python",
    "Description-Content-Type",
}
PYTHON_METADATA_MULTI_FIELDS = {"Requires-Dist", "License-File", "Dynamic"}
# This reviewed allowlist is independent of candidate package metadata. Keep
# wheel and sdist checks bound to the same explicitly approved dependencies.
APPROVED_RUNTIME_DEPENDENCIES = ("pypdf==6.16.1", "typing-extensions==4.16.0")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    raw_parts = name.split("/")
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in raw_parts)
        or path.as_posix() != name
    ):
        raise ValueError(f"unsafe archive member: {name!r}")
    return path


def _migration_ledger_digest(payloads: dict[str, bytes]) -> str:
    prefix = "finance_core/resources/migrations/"
    migrations = {
        PurePosixPath(name).name: body
        for name, body in payloads.items()
        if name.startswith(prefix) and name.endswith(".sql")
    }
    observed_numbers = [int(name[:3]) for name in sorted(migrations)]
    if observed_numbers != list(range(1, 52)):
        raise ValueError("wheel must contain the exact migration inventory 001-051")
    digest = hashlib.sha256()
    for filename in sorted(migrations):
        body = migrations[filename]
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate migration contract field")
        result[key] = value
    return result


def _migration_contract(contract_payload: bytes) -> tuple[str, tuple[str, ...]]:
    if not contract_payload or len(contract_payload) > 64 * 1024:
        raise ValueError("wheel migration contract size is invalid")
    try:
        contract: object = json.loads(
            contract_payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("wheel migration contract is invalid JSON") from exc
    if not isinstance(contract, dict) or set(contract) != {
        "migration_filenames",
        "migration_ledger_digest",
        "schema",
    }:
        raise ValueError("wheel migration contract schema is invalid")
    if contract["schema"] != "finance-core-migration-contract-v1":
        raise ValueError("wheel migration contract schema is invalid")
    digest_value = contract["migration_ledger_digest"]
    filenames_value = contract["migration_filenames"]
    if not isinstance(digest_value, str) or HEX_64.fullmatch(digest_value) is None:
        raise ValueError("wheel migration ledger declaration is invalid")
    if (
        not isinstance(filenames_value, list)
        or not filenames_value
        or any(not isinstance(filename, str) for filename in filenames_value)
    ):
        raise ValueError("wheel migration filename declaration is invalid")
    filenames = tuple(filename for filename in filenames_value if isinstance(filename, str))
    if len(filenames) != len(set(filenames)) or any(
        len(filename) < 9
        or not filename[:3].isdigit()
        or filename[3] != "_"
        or not filename.endswith(".sql")
        or "/" in filename
        or "\\" in filename
        for filename in filenames
    ):
        raise ValueError("wheel migration filename declaration is invalid")
    return digest_value, filenames


def _api_contract_version(package_init: bytes) -> str:
    try:
        module = ast.parse(package_init.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError("wheel finance_core/__init__.py is not valid UTF-8 Python") from exc
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        has_target = any(
            isinstance(target, ast.Name) and target.id == "API_CONTRACT_VERSION"
            for target in statement.targets
        )
        if (
            has_target
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return statement.value.value
    raise ValueError("wheel does not declare a literal API_CONTRACT_VERSION")


def _release_source_files(source_root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in sorted(CORE_RELEASE_SOURCE_FILES):
        path = source_root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"release source file is missing or unsafe: {name}")
        files[name] = path.read_bytes()
    return files


def _project_metadata(source_files: dict[str, bytes], version: str) -> dict[str, object]:
    try:
        pyproject = tomllib.loads(source_files["pyproject.toml"].decode("utf-8"))
        project = pyproject["project"]
        build_system = pyproject["build-system"]
        readme = source_files["README.md"].decode("utf-8")
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("pyproject.toml does not contain valid release metadata") from exc
    expected = {
        "Metadata-Version": "2.4",
        "Name": "finance-core",
        "Version": version,
        "Summary": project.get("description"),
        "License-Expression": "Apache-2.0",
        "Requires-Python": ">=3.12",
        "Description-Content-Type": "text/markdown",
        "Requires-Dist": list(APPROVED_RUNTIME_DEPENDENCIES),
        "License-File": ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"],
        "Dynamic": ["license-file"],
        "body": readme,
    }
    if (
        project.get("name") != expected["Name"]
        or project.get("version") != version
        or project.get("license") != "Apache-2.0"
        or project.get("requires-python") != expected["Requires-Python"]
        or project.get("dependencies") != expected["Requires-Dist"]
        or project.get("license-files") != expected["License-File"]
        or project.get("readme") != "README.md"
        or not isinstance(project.get("description"), str)
        or build_system.get("requires") != ["setuptools==84.0.0"]
        or build_system.get("build-backend") != "setuptools.build_meta"
    ):
        raise ValueError("pyproject.toml release metadata is not the approved contract")
    return expected


def _verify_python_metadata(metadata: Message, expected: dict[str, object], label: str) -> None:
    expected_fields = PYTHON_METADATA_SINGLE_FIELDS | PYTHON_METADATA_MULTI_FIELDS
    if set(metadata.keys()) != expected_fields:
        raise ValueError(f"{label} metadata fields are not the approved allowlist")
    for field in PYTHON_METADATA_SINGLE_FIELDS:
        if metadata.get_all(field, []) != [expected[field]]:
            raise ValueError(f"{label} metadata field {field} does not match the release source")
    for field in PYTHON_METADATA_MULTI_FIELDS:
        if metadata.get_all(field, []) != expected[field]:
            raise ValueError(f"{label} metadata field {field} does not match the release source")
    body = metadata.get_payload()
    if not isinstance(body, str) or body != expected["body"]:
        raise ValueError(f"{label} long description does not match README.md")


def _verify_wheel_record(files: dict[str, bytes], record_name: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"), newline="")))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("wheel RECORD is unreadable") from exc
    if any(len(row) != 3 for row in rows):
        raise ValueError("wheel RECORD contains a malformed row")
    names = [row[0] for row in rows]
    if len(names) != len(set(names)) or set(names) != set(files):
        raise ValueError("wheel RECORD inventory does not match the archive")
    for name, encoded_digest, encoded_size in rows:
        if name == record_name:
            if encoded_digest or encoded_size:
                raise ValueError("wheel RECORD must not hash itself")
            continue
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode()
        )
        if encoded_digest != f"sha256={digest}" or encoded_size != str(len(files[name])):
            raise ValueError(f"wheel RECORD does not authenticate {name}")


def _inspect_wheel(
    path: Path,
    version: str,
    source_files: dict[str, bytes],
    expected_metadata: dict[str, object],
) -> tuple[dict[str, bytes], str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_FILES:
                raise ValueError("wheel file inventory is empty or exceeds its limit")
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("wheel contains duplicate members")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError("wheel exceeds its uncompressed byte limit")
            for info in infos:
                _safe_archive_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("wheel contains a symbolic link")
            files = {info.filename: archive.read(info) for info in infos if not info.is_dir()}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("wheel is not a valid ZIP archive") from exc

    metadata_names = [name for name in files if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ValueError("wheel must contain exactly one distribution METADATA file")
    metadata_root = PurePosixPath(metadata_names[0]).parts[0]
    expected_metadata_root = f"finance_core-{version}.dist-info"
    if metadata_root != expected_metadata_root:
        raise ValueError("wheel distribution metadata directory is unexpected")
    unexpected = sorted(
        name
        for name in files
        if not name.startswith("finance_core/")
        and not name.startswith(f"{expected_metadata_root}/")
    )
    if unexpected:
        raise ValueError(f"wheel contains unexpected payloads: {unexpected}")
    expected_dist_files = {
        f"{expected_metadata_root}/METADATA",
        f"{expected_metadata_root}/WHEEL",
        f"{expected_metadata_root}/top_level.txt",
        f"{expected_metadata_root}/RECORD",
        f"{expected_metadata_root}/licenses/LICENSE",
        f"{expected_metadata_root}/licenses/NOTICE",
        f"{expected_metadata_root}/licenses/THIRD_PARTY_NOTICES.md",
    }
    observed_dist_files = {name for name in files if name.startswith(f"{expected_metadata_root}/")}
    if observed_dist_files != expected_dist_files:
        raise ValueError("wheel distribution metadata inventory is unexpected")
    metadata = BytesParser().parsebytes(files[metadata_names[0]])
    _verify_python_metadata(metadata, expected_metadata, "wheel")
    wheel_metadata = BytesParser().parsebytes(files[f"{expected_metadata_root}/WHEEL"])
    if (
        set(wheel_metadata.keys()) != {"Wheel-Version", "Generator", "Root-Is-Purelib", "Tag"}
        or wheel_metadata.get("Wheel-Version") != "1.0"
        or wheel_metadata.get("Generator") != "setuptools (84.0.0)"
        or wheel_metadata.get("Root-Is-Purelib") != "true"
        or wheel_metadata.get_all("Tag", []) != ["py3-none-any"]
    ):
        raise ValueError("wheel compatibility metadata is unexpected")
    if files[f"{expected_metadata_root}/top_level.txt"] != b"finance_core\n":
        raise ValueError("wheel top-level package declaration is unexpected")
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        if files[f"{expected_metadata_root}/licenses/{name}"] != source_files[name]:
            raise ValueError(f"wheel license payload does not match {name}")
    _verify_wheel_record(files, f"{expected_metadata_root}/RECORD")
    package_files = {name: body for name, body in files.items() if name.startswith("finance_core/")}
    package_init = package_files.get("finance_core/__init__.py")
    if package_init is None:
        raise ValueError("wheel is missing finance_core/__init__.py")
    migration_contract = package_files.get("finance_core/resources/migration-contract-v1.json")
    if migration_contract is None:
        raise ValueError("wheel is missing the migration contract resource")
    observed_ledger = _migration_ledger_digest(package_files)
    declared_ledger, declared_filenames = _migration_contract(migration_contract)
    observed_filenames = tuple(
        sorted(
            PurePosixPath(name).name
            for name in package_files
            if name.startswith("finance_core/resources/migrations/") and name.endswith(".sql")
        )
    )
    if declared_ledger != observed_ledger or declared_filenames != observed_filenames:
        raise ValueError("wheel migration contract does not match its migration payload")
    return (
        package_files,
        _api_contract_version(package_init),
        observed_ledger,
    )


def _inspect_sdist(
    path: Path,
    version: str,
    wheel_files: dict[str, bytes],
    source_files: dict[str, bytes],
    expected_metadata: dict[str, object],
) -> None:
    expected_root = f"finance_core-{version}"
    allowed_root_files = CORE_RELEASE_SOURCE_FILES | set(SDIST_GENERATED_ROOT_FILES) | {"PKG-INFO"}
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("source distribution inventory is empty or exceeds its limit")
            if sum(member.size for member in members if member.isfile()) > MAX_ARCHIVE_BYTES:
                raise ValueError("source distribution exceeds its uncompressed byte limit")
            names: set[str] = set()
            regular_file_names: set[str] = set()
            source_package: dict[str, bytes] = {}
            root_files: dict[str, bytes] = {}
            egg_info_files: dict[str, bytes] = {}
            for member in members:
                member_path = _safe_archive_name(member.name)
                if member.name in names:
                    raise ValueError("source distribution contains duplicate members")
                names.add(member.name)
                if member.isfile():
                    regular_file_names.add(member.name)
                if member_path.parts[0] != expected_root:
                    raise ValueError("source distribution has an unexpected root directory")
                if not member.isfile() and not member.isdir():
                    raise ValueError("source distribution contains a link or special file")
                relative_parts = member_path.parts[1:]
                allowed_payload = (
                    not relative_parts
                    or relative_parts[0] == "finance_core"
                    or relative_parts[0] == "finance_core.egg-info"
                    or (len(relative_parts) == 1 and relative_parts[0] in allowed_root_files)
                )
                if not allowed_payload:
                    raise ValueError(
                        f"source distribution contains an unexpected payload: {member.name}"
                    )
                is_package_file = (
                    member.isfile()
                    and len(member_path.parts) > 1
                    and member_path.parts[1] == "finance_core"
                )
                if is_package_file:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("source distribution member could not be read")
                    relative_name = PurePosixPath(*member_path.parts[1:]).as_posix()
                    source_package[relative_name] = stream.read()
                elif member.isfile() and len(relative_parts) == 1:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("source distribution member could not be read")
                    root_files[relative_parts[0]] = stream.read()
                elif member.isfile() and relative_parts[0] == "finance_core.egg-info":
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("source distribution metadata could not be read")
                    egg_info_files[PurePosixPath(*relative_parts[1:]).as_posix()] = stream.read()
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("source distribution is not a valid gzip tar archive") from exc
    if source_package != wheel_files:
        raise ValueError("source distribution and wheel package payloads differ")
    if set(root_files) != allowed_root_files:
        raise ValueError("source distribution root inventory is unexpected")
    for name, payload in source_files.items():
        if root_files[name] != payload:
            raise ValueError(f"source distribution release input does not match {name}")
    for name, payload in SDIST_GENERATED_ROOT_FILES.items():
        if root_files[name] != payload:
            raise ValueError(f"source distribution generated metadata does not match {name}")
    _verify_python_metadata(
        BytesParser().parsebytes(root_files["PKG-INFO"]), expected_metadata, "sdist"
    )
    if set(egg_info_files) != SDIST_EGG_INFO_FILES:
        raise ValueError("source distribution egg-info inventory is unexpected")
    _verify_python_metadata(
        BytesParser().parsebytes(egg_info_files["PKG-INFO"]), expected_metadata, "sdist egg-info"
    )
    if egg_info_files["dependency_links.txt"] != b"\n":
        raise ValueError("source distribution dependency links are unexpected")
    expected_dependencies = "".join(f"{item}\n" for item in APPROVED_RUNTIME_DEPENDENCIES).encode()
    if egg_info_files["requires.txt"] != expected_dependencies:
        raise ValueError("source distribution dependency metadata is unexpected")
    if egg_info_files["top_level.txt"] != b"finance_core\n":
        raise ValueError("source distribution top-level package declaration is unexpected")
    try:
        sources_text = egg_info_files["SOURCES.txt"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source distribution SOURCES.txt is not UTF-8") from exc
    expected_sources = {
        PurePosixPath(name).relative_to(expected_root).as_posix()
        for name in regular_file_names
        if name not in {f"{expected_root}/PKG-INFO", f"{expected_root}/setup.cfg"}
        and name != expected_root
    }
    source_entries = sources_text.splitlines()
    if "\r" in sources_text or any(not entry for entry in source_entries):
        raise ValueError("source distribution SOURCES.txt inventory is unexpected")
    normalized_source_entries = [_safe_archive_name(entry).as_posix() for entry in source_entries]
    if normalized_source_entries != source_entries:
        raise ValueError("source distribution SOURCES.txt inventory is unexpected")
    if len(source_entries) != len(set(source_entries)) or set(source_entries) != expected_sources:
        raise ValueError("source distribution SOURCES.txt inventory is unexpected")


def _regular_tree_files(
    root: Path,
    single_files: set[str],
    directories: set[str],
) -> dict[str, tuple[bytes, int]]:
    files: dict[str, tuple[bytes, int]] = {}
    for relative_name in sorted(single_files):
        path = root / relative_name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Bridge source file is missing or unsafe: {relative_name}")
        files[relative_name] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    for directory_name in sorted(directories):
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"Bridge source directory is missing or unsafe: {directory_name}")
        for path in sorted(directory.rglob("*")):
            relative_name = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError(f"Bridge source contains a symbolic link: {relative_name}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f"Bridge source contains a special file: {relative_name}")
            files[relative_name] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return files


def _git_archive_files(
    source_root: Path, core_commit: str, pathspec: str | list[str]
) -> dict[str, bytes]:
    pathspecs = [pathspec] if isinstance(pathspec, str) else pathspec
    try:
        completed = subprocess.run(
            ["git", "archive", "--format=tar", core_commit, *pathspecs],
            cwd=source_root,
            check=True,
            capture_output=True,
        )
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            files: dict[str, bytes] = {}
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("Git source archive contains a link or special file")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Git source archive member could not be read")
                files[member.name] = stream.read()
            return files
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise ValueError("exact Git source archive could not be inspected") from exc


def _bridge_source_identity(
    files: dict[str, tuple[bytes, int]],
) -> dict[str, object]:
    entries = [
        {
            "path": name,
            "byte_count": len(payload),
            "mode": mode,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, (payload, mode) in sorted(files.items(), key=lambda item: item[0].encode())
    ]
    material = {"policy_version": BRIDGE_SOURCE_POLICY, "entries": entries}
    canonical = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
    return {
        "policy_version": BRIDGE_SOURCE_POLICY,
        "source_identity_sha256": hashlib.sha256(
            f"{BRIDGE_SOURCE_POLICY}\0{canonical}".encode()
        ).hexdigest(),
        "file_count": len(entries),
        "byte_count": sum(len(payload) for payload, _mode in files.values()),
    }


def _inspect_bridge(path: Path, version: str, source_root: Path, core_commit: str) -> None:
    required = {
        "package/package.json",
        "package/npm-shrinkwrap.json",
        "package/tsconfig.json",
        "package/dist/build-provenance-v1.json",
        "package/dist/src/index.js",
    }
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("Bridge package inventory is empty or exceeds its limit")
            if sum(member.size for member in members if member.isfile()) > MAX_ARCHIVE_BYTES:
                raise ValueError("Bridge package exceeds its uncompressed byte limit")
            names: set[str] = set()
            files: dict[str, tuple[bytes, int]] = {}
            for member in members:
                member_path = _safe_archive_name(member.name)
                if member.name in names:
                    raise ValueError("Bridge package contains duplicate members")
                names.add(member.name)
                if member_path.parts[0] != "package":
                    raise ValueError("Bridge package has an unexpected root directory")
                if "platform" in member_path.parts or "test" in member_path.parts:
                    raise ValueError("Bridge package contains private platform or test content")
                if not member.isfile() and not member.isdir():
                    raise ValueError("Bridge package contains a link or special file")
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("Bridge package member could not be read")
                    files[member.name] = (stream.read(), stat.S_IMODE(member.mode))
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("Bridge package is not a valid gzip tar archive") from exc
    missing = required - files.keys()
    if missing:
        raise ValueError(f"Bridge package is missing release evidence: {sorted(missing)}")

    bridge_root = source_root / "plugins" / "finance-bridge"
    source_files = _regular_tree_files(
        bridge_root,
        BRIDGE_PACKAGE_SINGLE_FILES,
        BRIDGE_SOURCE_DIRECTORIES,
    )
    committed_bridge_files = _git_archive_files(
        source_root,
        core_commit,
        "plugins/finance-bridge",
    )
    committed_source_files = {
        name.removeprefix("plugins/finance-bridge/"): payload
        for name, payload in committed_bridge_files.items()
        if name.removeprefix("plugins/finance-bridge/") in source_files
    }
    if {name: payload for name, (payload, _) in source_files.items()} != committed_source_files:
        raise ValueError("Bridge package source does not match the exact source commit")

    dist_files = _regular_tree_files(
        bridge_root,
        {BRIDGE_PROVENANCE_PATH},
        {"dist/src"},
    )
    committed_dist_files = {
        name.removeprefix("plugins/finance-bridge/"): payload
        for name, payload in committed_bridge_files.items()
        if name.removeprefix("plugins/finance-bridge/") in dist_files
    }
    if {name: payload for name, (payload, _) in dist_files.items()} != committed_dist_files:
        raise ValueError("Bridge compiled output does not match the exact source commit")
    expected_files = {**source_files, **dist_files}
    packaged_files = {name.removeprefix("package/"): value for name, value in files.items()}
    if packaged_files != expected_files:
        raise ValueError("Bridge package inventory or payload does not match the exact build")

    manifest = json.loads(files["package/package.json"][0])
    if (
        manifest.get("name") != "@finance-codex/finance-bridge"
        or manifest.get("version") != version
    ):
        raise ValueError("Bridge package identity does not match the requested release")
    build_source_files = {
        name: value
        for name, value in source_files.items()
        if name in BRIDGE_SOURCE_SINGLE_FILES
        or PurePosixPath(name).parts[0] in BRIDGE_SOURCE_DIRECTORIES
    }
    expected_provenance = _bridge_source_identity(build_source_files)
    provenance = json.loads(files["package/dist/build-provenance-v1.json"][0])
    if provenance != expected_provenance:
        raise ValueError("Bridge build provenance is invalid")


def _source_package_files(source_root: Path) -> dict[str, bytes]:
    package_root = source_root / "finance_core"
    return {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in package_root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".py", ".sql", ".txt"}
    }


def _verify_source(
    source_root: Path,
    core_commit: str,
    wheel_files: dict[str, bytes],
    release_source_files: dict[str, bytes],
) -> None:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status_output = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "finance_core",
                "plugins/finance-bridge",
                *sorted(CORE_RELEASE_SOURCE_FILES),
                *sorted(RELEASE_TOOL_SOURCE_FILES),
            ],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("source root must be a readable Git checkout") from exc
    if head != core_commit or status_output:
        raise ValueError("source root must be clean and at the exact core commit")
    committed_package_files = {
        name: payload
        for name, payload in _git_archive_files(source_root, core_commit, "finance_core").items()
        if Path(name).suffix in {".json", ".py", ".sql", ".txt"}
    }
    if _source_package_files(source_root) != committed_package_files:
        raise ValueError("Finance Core package source does not match the exact Git commit")
    if committed_package_files != wheel_files:
        raise ValueError("wheel package payload does not match the exact source commit")
    committed_release_files = _git_archive_files(
        source_root, core_commit, sorted(CORE_RELEASE_SOURCE_FILES)
    )
    if committed_release_files != release_source_files:
        raise ValueError("release source files do not match the exact Git commit")
    release_tool_files = {
        name: (source_root / name).read_bytes() for name in RELEASE_TOOL_SOURCE_FILES
    }
    committed_release_tools = _git_archive_files(
        source_root, core_commit, sorted(RELEASE_TOOL_SOURCE_FILES)
    )
    if committed_release_tools != release_tool_files:
        raise ValueError("release verification tool does not match the exact Git commit")


def _require_single(artifacts_dir: Path, pattern: str, label: str, version: str) -> Path:
    matches = sorted(path for path in artifacts_dir.glob(pattern) if path.is_file())
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {label} for version {version}, found {len(matches)}"
        )
    path = matches[0]
    if path.is_symlink() or path.parent.resolve() != artifacts_dir.resolve():
        raise ValueError(f"unsafe artifact path: {path}")
    return path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--api-contract-version", required=True)
    parser.add_argument("--migration-ledger-digest", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    artifacts_dir = args.artifacts_dir.resolve()
    source_root = args.source_root.resolve()
    if not artifacts_dir.is_dir():
        raise SystemExit(f"artifacts directory does not exist: {artifacts_dir}")
    if not source_root.is_dir():
        raise SystemExit(f"source root does not exist: {source_root}")
    if VERSION.fullmatch(args.core_version) is None:
        raise SystemExit("core version must be a stable semantic version")
    if HEX_40.fullmatch(args.core_commit) is None:
        raise SystemExit("core commit must be a lowercase 40-character SHA-1")
    if HEX_64.fullmatch(args.migration_ledger_digest) is None:
        raise SystemExit("migration ledger digest must be a lowercase SHA-256")
    if not args.api_contract_version or len(args.api_contract_version) > 100:
        raise SystemExit("API contract version is invalid")

    try:
        artifact_paths = (
            _require_single(
                artifacts_dir,
                f"finance_core-{args.core_version}-*.whl",
                "wheel",
                args.core_version,
            ),
            _require_single(
                artifacts_dir,
                f"finance_core-{args.core_version}.tar.gz",
                "source distribution",
                args.core_version,
            ),
            _require_single(
                artifacts_dir,
                f"finance-codex-finance-bridge-{args.core_version}.tgz",
                "Bridge package",
                args.core_version,
            ),
        )
        allowed = {path.name for path in artifact_paths} | OUTPUT_FILENAMES
        extras = sorted(path.name for path in artifacts_dir.iterdir() if path.name not in allowed)
        if extras:
            raise ValueError(f"artifact directory contains unexpected entries: {extras}")
        existing_outputs = sorted(
            filename
            for filename in OUTPUT_FILENAMES
            if (artifacts_dir / filename).exists() or (artifacts_dir / filename).is_symlink()
        )
        if existing_outputs:
            raise ValueError(f"release output files already exist: {existing_outputs}")
        release_source_files = _release_source_files(source_root)
        expected_metadata = _project_metadata(release_source_files, args.core_version)
        wheel_files, observed_api, observed_ledger = _inspect_wheel(
            artifact_paths[0], args.core_version, release_source_files, expected_metadata
        )
        if observed_api != args.api_contract_version:
            raise ValueError("API contract version does not match the wheel")
        if observed_ledger != args.migration_ledger_digest:
            raise ValueError("migration ledger digest does not match the wheel")
        _inspect_sdist(
            artifact_paths[1],
            args.core_version,
            wheel_files,
            release_source_files,
            expected_metadata,
        )
        _verify_source(source_root, args.core_commit, wheel_files, release_source_files)
        _inspect_bridge(
            artifact_paths[2],
            args.core_version,
            source_root,
            args.core_commit,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc

    artifact_entries = [
        {
            "filename": path.name,
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(artifact_paths, key=lambda path: path.name)
    ]
    manifest = {
        "api_contract_version": observed_api,
        "artifacts": artifact_entries,
        "bridge_version": args.core_version,
        "core_commit": args.core_commit,
        "core_version": args.core_version,
        "migration_ledger_digest": observed_ledger,
        "schema": "finance-core-component-manifest-v1",
    }
    manifest_path = artifacts_dir / "component-manifest-v1.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksum_entries = {entry["filename"]: entry["sha256"] for entry in artifact_entries}
    checksum_entries[manifest_path.name] = _sha256(manifest_path)
    checksum_path = artifacts_dir / "SHA256SUMS"
    checksum_path.write_text(
        "".join(f"{digest}  {filename}\n" for filename, digest in sorted(checksum_entries.items())),
        encoding="utf-8",
    )
    print(f"release manifest written for {len(artifact_entries)} artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
