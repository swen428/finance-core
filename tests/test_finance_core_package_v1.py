from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath

import pytest

import finance_core.resources as resource_runtime
from finance_core.reconciliation import migrations as migration_runtime
from finance_core.resources import (
    MIGRATION_LEDGER_DIGEST,
    MIGRATION_PREFLIGHT_FILENAME,
    MIGRATION_PREFLIGHT_SHA256,
    migration_preflight_path,
    migration_resource_paths,
    migrations_dir,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MONEY_EXPORTS = {
    "API_CONTRACT_VERSION",
    "SUPPORTED_CURRENCIES",
    "CurrencyMismatchError",
    "Money",
    "MoneyValidationError",
    "SignPolicy",
    "canonical_decimal_str",
    "canonical_money_str",
    "minor_units",
    "money_decimal",
    "normalize_currency",
    "quantize_for_currency",
    "quantum_for_currency",
    "require_same_currency",
    "validate_amount_for_currency",
}
PRIVATE_ARTIFACT_MARKERS = (
    b"example-private-owner",
    b"finance-" + b"automation",
)
EXPECTED_LEDGER_DIGEST = "9bf4d410cdb6f72d9517ac1a9c012892a10b9bdddf4d4f90e91bb712d972ab29"


def test_pdf_dependency_inventory_matches_packaged_notice_and_lock_summary() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    pin = next(item for item in project["project"]["dependencies"] if item.startswith("pypdf=="))
    dependency_version = pin.removeprefix("pypdf==")
    notice = (REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    lock = (REPOSITORY_ROOT / "requirements-dev.txt").read_text()
    assert f"- `pypdf` {dependency_version} — BSD-3-Clause." in notice
    assert pin in "\n".join(line for line in lock.splitlines() if line.startswith("##"))
    assert any(line.startswith(f"{pin} ") for line in lock.splitlines())


def _ledger_digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        payload = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def test_migration_resources_have_one_package_owned_source() -> None:
    package_root = REPOSITORY_ROOT / "finance_core"
    paths = migration_resource_paths()

    assert len(paths) == 55
    assert [int(path.name[:3]) for path in paths] == list(range(1, 56))
    assert all(path.resolve().is_relative_to(package_root.resolve()) for path in paths)
    assert _ledger_digest(paths) == EXPECTED_LEDGER_DIGEST == MIGRATION_LEDGER_DIGEST
    assert not (REPOSITORY_ROOT / "database" / "migrations").exists()


def test_migration_runtime_authority_reloads_the_non_executable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_resource_paths.cache_clear()
    monkeypatch.setattr(resource_runtime, "MIGRATION_LEDGER_DIGEST", "0" * 64)
    monkeypatch.setattr(resource_runtime, "MIGRATION_FILENAMES", ("001_wrong.sql",))

    paths = migration_resource_paths()

    assert len(paths) == 55
    assert _ledger_digest(paths) == EXPECTED_LEDGER_DIGEST
    migration_resource_paths.cache_clear()


def test_migration_runtime_consumes_the_package_resources() -> None:
    resource_paths = migration_resource_paths()

    assert migration_runtime.MIGRATIONS_DIR == migrations_dir()
    assert tuple(migration_runtime.TEMP_DB_MIGRATION_PATHS) == resource_paths
    assert migration_runtime.MIGRATION_029_PREFLIGHT_ARTIFACT == migration_preflight_path()
    assert migration_preflight_path().name == MIGRATION_PREFLIGHT_FILENAME
    assert migration_preflight_path().parent.name == "preflight"
    assert migration_preflight_path().resolve().is_relative_to(migrations_dir().parent.resolve())


def test_finance_core_exposes_the_authoritative_money_contract() -> None:
    """Removing or replacing the public facade must break consumer-visible behavior."""

    assert importlib.util.find_spec("finance_core") is not None, (
        "the installable finance_core public package is missing"
    )
    finance_core = importlib.import_module("finance_core")
    internal_money = importlib.import_module("finance_core.money")

    assert finance_core.API_CONTRACT_VERSION == "finance-core-api-v1"
    assert set(finance_core.__all__) == PUBLIC_MONEY_EXPORTS
    for name in PUBLIC_MONEY_EXPORTS - {"API_CONTRACT_VERSION"}:
        assert getattr(finance_core, name) is getattr(internal_money, name)
    assert finance_core.Money.__module__ == "finance_core.money"
    assert finance_core.Money.from_string("12.30", "sgd") == finance_core.Money(
        Decimal("12.30"), "SGD"
    )
    assert finance_core.canonical_money_str(Decimal("12.3"), "SGD") == "12.30"
    assert finance_core.quantum_for_currency("JPY") == Decimal("1")

    with pytest.raises(finance_core.CurrencyMismatchError):
        finance_core.Money(Decimal("1.00"), "SGD") + finance_core.Money(Decimal("1.00"), "USD")


def _probe_installed_package(
    runtime_python: Path,
    install_dir: Path,
    outside_dir: Path,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(runtime_python),
            "-I",
            "-S",
            "-c",
            (
                "import importlib.util, json, sys; "
                f"sys.path.insert(0, {str(install_dir)!r}); "
                "import finance_core; "
                "from finance_core.resources import ("
                "MIGRATION_LEDGER_DIGEST, MIGRATION_PREFLIGHT_SHA256, "
                "migration_preflight_path, migration_resource_paths); "
                "from decimal import Decimal; "
                "from importlib.metadata import distribution; "
                "dist = distribution('finance-core'); "
                "print(json.dumps({"
                "'api': finance_core.API_CONTRACT_VERSION, "
                "'canonical': finance_core.canonical_money_str(Decimal('12.3'), 'SGD'), "
                "'distribution_name': dist.metadata['Name'], "
                "'distribution_version': dist.version, "
                "'money_module': finance_core.Money.__module__, "
                "'module_file': finance_core.__file__, "
                "'migration_count': len(migration_resource_paths()), "
                "'migration_digest': MIGRATION_LEDGER_DIGEST, "
                "'migration_paths': [str(path) for path in migration_resource_paths()], "
                "'preflight_path': str(migration_preflight_path()), "
                "'preflight_sha256': MIGRATION_PREFLIGHT_SHA256, "
                "'broad_modules': all(importlib.util.find_spec(name) is not None for name in ("
                "'finance_core.intake', 'finance_core.parser_proposals', "
                "'finance_core.receipt_finalization', 'finance_core.reconciliation', "
                "'finance_core.settlement')), "
                "'src_visible': importlib.util.find_spec('src') is not None, "
                "'sys_path': sys.path}))"
            ),
        ],
        check=True,
        cwd=outside_dir,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _assert_safe_text_payloads(payloads: list[bytes]) -> None:
    combined = b"\n".join(payloads).lower()
    for marker in PRIVATE_ARTIFACT_MARKERS:
        assert marker not in combined


def _probe_installed_runtime_boundary(
    runtime_python: Path,
    install_dir: Path,
    outside_dir: Path,
    runtime_root: Path,
    external_workspace: Path,
) -> dict[str, object]:
    program = f"""
import json
import sys
sys.path.insert(0, {str(install_dir)!r})
from finance_core.receipt_staging_runner.models import RunnerWorkspaceError
from finance_core.receipt_staging_runner.workspace import _validate_workspace_path
from finance_core.staging_guard import StagingDatabaseError, create_staging_database

runtime_refused = False
try:
    _validate_workspace_path({str(runtime_root)!r})
except RunnerWorkspaceError:
    runtime_refused = True

live_refused = False
live_database = {str(runtime_root / "database" / "finance.db")!r}
try:
    create_staging_database(live_database)
except StagingDatabaseError:
    live_refused = True

print(json.dumps({{
    "runtime_refused": runtime_refused,
    "live_refused": live_refused,
    "live_created": __import__("pathlib").Path(live_database).exists(),
    "external_workspace": str(_validate_workspace_path({str(external_workspace)!r})),
}}))
"""
    completed = subprocess.run(
        [str(runtime_python), "-I", "-c", program],
        check=True,
        cwd=outside_dir,
        env={
            "FINANCE_RUNTIME_ROOT": str(runtime_root),
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_built_wheel_installs_and_runs_without_the_source_checkout(tmp_path: Path) -> None:
    """A missing package/module in either artifact must fail outside the repository."""

    dist_dir = tmp_path / "dist"
    wheel_install_dir = tmp_path / "wheel-installed"
    sdist_install_dir = tmp_path / "sdist-installed"
    runtime_venv = tmp_path / "runtime-venv"
    runtime_root = tmp_path / "private-runtime"
    external_workspace = tmp_path / "external-workspace"
    outside_dir = tmp_path / "outside"
    dist_dir.mkdir()
    wheel_install_dir.mkdir()
    sdist_install_dir.mkdir()
    outside_dir.mkdir()
    runtime_root.mkdir(mode=0o700)
    (runtime_root / "database").mkdir(mode=0o700)
    external_workspace.mkdir(mode=0o700)

    build_environment = os.environ.copy()
    build_environment.pop("PYTHONPATH", None)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
            str(REPOSITORY_ROOT),
        ],
        check=True,
        cwd=outside_dir,
        env=build_environment,
        capture_output=True,
        text=True,
    )

    wheels = list(dist_dir.glob("*.whl"))
    source_distributions = list(dist_dir.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(source_distributions) == 1

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(wheel_install_dir),
            str(wheels[0]),
        ],
        check=True,
        cwd=outside_dir,
        env=build_environment,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(sdist_install_dir),
            str(source_distributions[0]),
        ],
        check=True,
        cwd=outside_dir,
        env=build_environment,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime_venv)],
        check=True,
        cwd=outside_dir,
        env=build_environment,
        capture_output=True,
        text=True,
    )
    runtime_python = runtime_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    for install_dir in (wheel_install_dir, sdist_install_dir):
        payload = _probe_installed_package(runtime_python, install_dir, outside_dir)
        assert payload["api"] == "finance-core-api-v1"
        assert payload["canonical"] == "12.30"
        assert payload["distribution_name"] == "finance-core"
        assert payload["distribution_version"] == "0.1.5"
        assert payload["money_module"] == "finance_core.money"
        assert payload["migration_count"] == 55
        assert payload["migration_digest"] == MIGRATION_LEDGER_DIGEST
        assert payload["preflight_sha256"] == MIGRATION_PREFLIGHT_SHA256
        assert payload["broad_modules"] is True
        assert all(
            Path(path).resolve().is_relative_to(install_dir.resolve())
            for path in payload["migration_paths"]
        )
        assert Path(str(payload["preflight_path"])).resolve().is_relative_to(install_dir.resolve())
        assert payload["src_visible"] is False
        assert Path(str(payload["module_file"])).resolve().is_relative_to(install_dir.resolve())
        assert all(
            not Path(entry).resolve().is_relative_to(REPOSITORY_ROOT)
            for entry in payload["sys_path"]
            if entry
        )
        runtime_boundary = _probe_installed_runtime_boundary(
            Path(sys.executable),
            install_dir,
            outside_dir,
            runtime_root,
            external_workspace,
        )
        assert runtime_boundary == {
            "runtime_refused": True,
            "live_refused": True,
            "live_created": False,
            "external_workspace": str(external_workspace),
        }

        missing_runtime = subprocess.run(
            [
                str(runtime_python),
                "-I",
                "-S",
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(install_dir)!r}); "
                    "import finance_core.reconciliation"
                ),
            ],
            cwd=outside_dir,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
        )
        assert missing_runtime.returncode != 0
        assert "FINANCE_RUNTIME_ROOT is required" in missing_runtime.stderr

    distribution_prefix = "finance_core-0.1.5.dist-info"
    expected_package_members = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / "finance_core").rglob("*")
        if path.is_file() and path.suffix in {".json", ".py", ".sql", ".txt"}
    }
    expected_metadata_members = {
        f"{distribution_prefix}/METADATA",
        f"{distribution_prefix}/RECORD",
        f"{distribution_prefix}/WHEEL",
        f"{distribution_prefix}/top_level.txt",
        f"{distribution_prefix}/licenses/LICENSE",
        f"{distribution_prefix}/licenses/NOTICE",
        f"{distribution_prefix}/licenses/THIRD_PARTY_NOTICES.md",
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        assert set(archive.namelist()) == expected_package_members | expected_metadata_members
        assert archive.read(f"{distribution_prefix}/top_level.txt") == b"finance_core\n"
        _assert_safe_text_payloads([archive.read(name) for name in archive.namelist()])

    expected_sdist_metadata = {
        ("LICENSE",),
        ("MANIFEST.in",),
        ("NOTICE",),
        ("PKG-INFO",),
        ("README.md",),
        ("THIRD_PARTY_NOTICES.md",),
        ("finance_core.egg-info", "PKG-INFO"),
        ("finance_core.egg-info", "SOURCES.txt"),
        ("finance_core.egg-info", "dependency_links.txt"),
        ("finance_core.egg-info", "requires.txt"),
        ("finance_core.egg-info", "top_level.txt"),
        ("pyproject.toml",),
        ("setup.cfg",),
    }
    with tarfile.open(source_distributions[0], "r:gz") as archive:
        members = archive.getmembers()
        assert members
        assert all(member.isfile() or member.isdir() for member in members)
        member_paths = [PurePosixPath(member.name) for member in members]
        assert all(not path.is_absolute() and ".." not in path.parts for path in member_paths)
        assert {path.parts[0] for path in member_paths} == {"finance_core-0.1.5"}
        files = [member for member in members if member.isfile()]
        observed_files = {PurePosixPath(member.name).parts[1:] for member in files}
        expected_source_files = {PurePosixPath(path).parts for path in expected_package_members}
        assert observed_files == expected_source_files | expected_sdist_metadata
        payloads = []
        for member in files:
            extracted = archive.extractfile(member)
            assert extracted is not None
            payloads.append(extracted.read())
        _assert_safe_text_payloads(payloads)
