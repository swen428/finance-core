from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "build_release_manifest.py"


def _manifest_command(
    artifacts: Path,
    *,
    source_root: Path = REPOSITORY_ROOT,
    core_commit: str = "a" * 40,
    migration_digest: str = "b" * 64,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--artifacts-dir",
        str(artifacts),
        "--source-root",
        str(source_root),
        "--core-version",
        "0.1.0",
        "--core-commit",
        core_commit,
        "--api-contract-version",
        "finance-core-api-v1",
        "--migration-ledger-digest",
        migration_digest,
    ]


def _write_fake_packages(artifacts: Path, *, bridge_version: str = "0.1.0") -> None:
    payloads = {
        "finance_core-0.1.0-py3-none-any.whl": b"not-a-wheel",
        "finance_core-0.1.0.tar.gz": b"not-an-sdist",
        f"finance-codex-finance-bridge-{bridge_version}.tgz": b"not-a-bridge",
    }
    for filename, payload in payloads.items():
        (artifacts / filename).write_bytes(payload)


def _add_tar_file(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(payload))


def _add_tar_directory(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    archive.addfile(info)


def _bridge_provenance(source_files: dict[str, bytes]) -> bytes:
    entries = [
        {
            "path": name,
            "byte_count": len(payload),
            "mode": 0o644,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(source_files.items(), key=lambda item: item[0].encode())
    ]
    material = {
        "policy_version": "finance-plugin-build-source-v3",
        "entries": entries,
    }
    canonical = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
    provenance = {
        "policy_version": "finance-plugin-build-source-v3",
        "source_identity_sha256": hashlib.sha256(
            f"finance-plugin-build-source-v3\0{canonical}".encode()
        ).hexdigest(),
        "file_count": len(entries),
        "byte_count": sum(len(payload) for payload in source_files.values()),
    }
    return (json.dumps(provenance, separators=(",", ":")) + "\n").encode()


def _write_bridge_source(source_root: Path) -> dict[str, bytes]:
    bridge_root = source_root / "plugins" / "finance-bridge"
    package_manifest = json.dumps(
        {"name": "@finance-codex/finance-bridge", "version": "0.1.0"},
        separators=(",", ":"),
    ).encode()
    source_files = {
        "binding.gyp": b"{}\n",
        "native/addon.cc": b"// native\n",
        "npm-shrinkwrap.json": b"{}\n",
        "openclaw.plugin.json": b"{}\n",
        "package.json": package_manifest,
        "scripts/build.mjs": b"export {};\n",
        "src/index.ts": b"export {};\n",
        "tsconfig.json": b"{}\n",
        "types/runtime.d.ts": b"export {};\n",
    }
    package_files = {
        "LICENSE": b"synthetic license fixture\n",
        "THIRD_PARTY_NOTICES.md": b"synthetic notices fixture\n",
        **source_files,
        "dist/build-provenance-v1.json": _bridge_provenance(source_files),
        "dist/src/index.js": b"export {};\n",
    }
    for name, payload in package_files.items():
        path = bridge_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o644)
    return package_files


def _write_valid_release_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    source_root = tmp_path / "source"
    shutil.copytree(REPOSITORY_ROOT / "finance_core", source_root / "finance_core")
    (source_root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, source_root / "scripts" / "build_release_manifest.py")
    release_source_names = {
        "LICENSE",
        "MANIFEST.in",
        "NOTICE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
    }
    for name in release_source_names:
        shutil.copy2(REPOSITORY_ROOT / name, source_root / name)
    bridge_files = _write_bridge_source(source_root)
    subprocess.run(["git", "init", "--quiet"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "add", "finance_core", "plugins", "scripts", *sorted(release_source_names)],
        cwd=source_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Finance Release Test",
            "-c",
            "user.email=finance-release@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=source_root,
        check=True,
    )
    core_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    package_payloads = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in (source_root / "finance_core").rglob("*")
        if path.is_file() and path.suffix in {".json", ".py", ".sql", ".txt"}
    }

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = artifacts / "finance_core-0.1.0-py3-none-any.whl"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: finance-core\n"
        "Version: 0.1.0\n"
        "Summary: Deterministic, auditable personal-finance core\n"
        "License-Expression: Apache-2.0\n"
        "Requires-Python: >=3.12\n"
        "Description-Content-Type: text/markdown\n"
        "License-File: LICENSE\n"
        "License-File: NOTICE\n"
        "License-File: THIRD_PARTY_NOTICES.md\n"
        "Requires-Dist: pypdf==6.14.2\n"
        "Requires-Dist: typing-extensions==4.16.0\n"
        "Dynamic: license-file\n\n"
    ).encode() + (source_root / "README.md").read_bytes()
    wheel_metadata = (
        b"Wheel-Version: 1.0\nGenerator: setuptools (84.0.0)\n"
        b"Root-Is-Purelib: true\nTag: py3-none-any\n\n"
    )
    dist_root = "finance_core-0.1.0.dist-info"
    wheel_payloads = {
        **package_payloads,
        f"{dist_root}/licenses/LICENSE": (source_root / "LICENSE").read_bytes(),
        f"{dist_root}/licenses/NOTICE": (source_root / "NOTICE").read_bytes(),
        f"{dist_root}/licenses/THIRD_PARTY_NOTICES.md": (
            source_root / "THIRD_PARTY_NOTICES.md"
        ).read_bytes(),
        f"{dist_root}/METADATA": metadata,
        f"{dist_root}/WHEEL": wheel_metadata,
        f"{dist_root}/top_level.txt": b"finance_core\n",
    }
    record_name = f"{dist_root}/RECORD"
    record_stream = io.StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for name, payload in sorted(wheel_payloads.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", str(len(payload))))
    writer.writerow((record_name, "", ""))
    wheel_payloads[record_name] = record_stream.getvalue().encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in wheel_payloads.items():
            archive.writestr(name, payload)

    sdist_payloads: dict[str, bytes] = {}
    for name, payload in package_payloads.items():
        sdist_payloads[name] = payload
    for name in sorted(release_source_names):
        sdist_payloads[name] = (source_root / name).read_bytes()
    sdist_payloads.update(
        {
            "setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
            "PKG-INFO": metadata,
            "finance_core.egg-info/PKG-INFO": metadata,
            "finance_core.egg-info/dependency_links.txt": b"\n",
            "finance_core.egg-info/requires.txt": (b"pypdf==6.14.2\ntyping-extensions==4.16.0\n"),
            "finance_core.egg-info/top_level.txt": b"finance_core\n",
        }
    )
    sources_names = sorted(
        set(sdist_payloads) - {"PKG-INFO", "setup.cfg"} | {"finance_core.egg-info/SOURCES.txt"},
        key=lambda name: (name.startswith("finance_core"), name),
    )
    sdist_payloads["finance_core.egg-info/SOURCES.txt"] = "".join(
        f"{name}\n" for name in sources_names
    ).encode()
    with tarfile.open(artifacts / "finance_core-0.1.0.tar.gz", "w:gz") as archive:
        _add_tar_directory(archive, "finance_core-0.1.0/finance_core/")
        for name, payload in sorted(sdist_payloads.items()):
            _add_tar_file(archive, f"finance_core-0.1.0/{name}", payload)

    with tarfile.open(artifacts / "finance-codex-finance-bridge-0.1.0.tgz", "w:gz") as archive:
        for name, payload in bridge_files.items():
            _add_tar_file(archive, f"package/{name}", payload)
    return artifacts, source_root, core_commit


def test_release_manifest_accepts_and_binds_inspected_exact_artifacts(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    migration_digest = "61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=migration_digest,
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "release manifest written for 3 artifacts\n"
    manifest = json.loads((artifacts / "component-manifest-v1.json").read_text())
    assert manifest["core_commit"] == core_commit
    assert manifest["api_contract_version"] == "finance-core-api-v1"
    assert manifest["migration_ledger_digest"] == migration_digest
    assert len(manifest["artifacts"]) == 3
    checksum_lines = (artifacts / "SHA256SUMS").read_text().splitlines()
    assert len(checksum_lines) == 4


def test_release_manifest_rejects_untracked_core_payload(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    (source_root / "finance_core" / "private_runtime_secret.py").write_text(
        "SECRET = 'must-not-publish'\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source root must be clean and at the exact core commit" in completed.stderr


def test_release_manifest_rejects_modified_build_metadata(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    sdist = artifacts / "finance_core-0.1.0.tar.gz"
    with tarfile.open(sdist, "r:gz") as archive:
        payloads = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }
    pyproject_name = "finance_core-0.1.0/pyproject.toml"
    payloads[pyproject_name] = payloads[pyproject_name].replace(
        b'build-backend = "setuptools.build_meta"',
        b'build-backend = "untrusted.backend"',
    )
    with tarfile.open(sdist, "w:gz") as archive:
        for name, payload in payloads.items():
            _add_tar_file(archive, name, payload)

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source distribution release input does not match pyproject.toml" in completed.stderr


def test_release_manifest_rejects_dirty_release_source(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    (source_root / "pyproject.toml").write_text(
        (source_root / "pyproject.toml")
        .read_text()
        .replace(
            'build-backend = "setuptools.build_meta"',
            'build-backend = "untrusted.backend"',
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "pyproject.toml release metadata is not the approved contract" in completed.stderr


def test_release_manifest_rejects_dirty_release_verification_tool(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    tool = source_root / "scripts" / "build_release_manifest.py"
    tool.write_bytes(tool.read_bytes() + b"\n# unreviewed verifier change\n")

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source root must be clean and at the exact core commit" in completed.stderr


def test_release_manifest_rejects_unexpected_wheel_entry_point(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    wheel = artifacts / "finance_core-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(
            "finance_core-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\nfinance-unsafe = finance_core:unsafe\n",
        )

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "metadata inventory is unexpected" in completed.stderr


def test_release_manifest_rejects_unapproved_python_metadata_header(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    wheel = artifacts / "finance_core-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    metadata_name = "finance_core-0.1.0.dist-info/METADATA"
    record_name = "finance_core-0.1.0.dist-info/RECORD"
    payloads[metadata_name] = payloads[metadata_name].replace(
        b"\n\n", b"\nAuthor-Email: private.person@example.invalid\n\n", 1
    )
    record_stream = io.StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for name, payload in sorted(payloads.items()):
        if name == record_name:
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", str(len(payload))))
    writer.writerow((record_name, "", ""))
    payloads[record_name] = record_stream.getvalue().encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "metadata fields are not the approved allowlist" in completed.stderr


def test_release_manifest_rejects_sdist_sources_inventory_drift(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    sdist = artifacts / "finance_core-0.1.0.tar.gz"
    with tarfile.open(sdist, "r:gz") as archive:
        payloads = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }
    sources_name = "finance_core-0.1.0/finance_core.egg-info/SOURCES.txt"
    payloads[sources_name] += b"private-runtime-secret.txt\n"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, payload in payloads.items():
            _add_tar_file(archive, name, payload)

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "SOURCES.txt inventory is unexpected" in completed.stderr


def test_release_manifest_rejects_bridge_payload_not_in_exact_source(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    bridge_path = artifacts / "finance-codex-finance-bridge-0.1.0.tgz"
    with tarfile.open(bridge_path, "r:gz") as archive:
        files = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }
    files["package/src/private-runtime-secret.txt"] = b"must-not-publish\n"
    with tarfile.open(bridge_path, "w:gz") as archive:
        for name, payload in files.items():
            _add_tar_file(archive, name, payload)

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "inventory or payload does not match the exact build" in completed.stderr


def test_release_manifest_rejects_dirty_compiled_bridge_output(tmp_path: Path) -> None:
    artifacts, source_root, core_commit = _write_valid_release_fixture(tmp_path)
    compiled = source_root / "plugins/finance-bridge/dist/src/index.js"
    compiled.write_text("export const unreviewed = true;\n", encoding="utf-8")
    bridge_path = artifacts / "finance-codex-finance-bridge-0.1.0.tgz"
    with tarfile.open(bridge_path, "r:gz") as archive:
        files = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }
    files["package/dist/src/index.js"] = compiled.read_bytes()
    with tarfile.open(bridge_path, "w:gz") as archive:
        for name, payload in files.items():
            _add_tar_file(archive, name, payload)

    completed = subprocess.run(
        _manifest_command(
            artifacts,
            source_root=source_root,
            core_commit=core_commit,
            migration_digest=("61e7dfaa6b1d8e4ffaccb04c52fb9335d709bf82a9c8c48965138fe859b6e6f3"),
        ),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source root must be clean and at the exact core commit" in completed.stderr


def test_release_manifest_rejects_unexpected_artifact_directory_content(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _write_fake_packages(artifacts)
    (artifacts / "private-runtime-secret.txt").write_text("must-not-publish", encoding="utf-8")

    completed = subprocess.run(
        _manifest_command(artifacts),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "artifact directory contains unexpected entries" in completed.stderr
    assert "private-runtime-secret.txt" in completed.stderr
    assert not (artifacts / "component-manifest-v1.json").exists()
    assert not (artifacts / "SHA256SUMS").exists()


def test_release_manifest_rejects_uninspected_package_payloads(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _write_fake_packages(artifacts)

    completed = subprocess.run(
        _manifest_command(artifacts),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "wheel is not a valid ZIP archive" in completed.stderr
    assert not (artifacts / "component-manifest-v1.json").exists()
    assert not (artifacts / "SHA256SUMS").exists()


def test_release_manifest_rejects_version_or_artifact_ambiguity(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _write_fake_packages(artifacts, bridge_version="0.1.1")

    completed = subprocess.run(
        _manifest_command(artifacts),
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "expected exactly one Bridge package for version 0.1.0" in completed.stderr
