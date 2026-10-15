from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath
from time import monotonic

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
INSTALLED_INSTANCE_TIMEOUT_SECONDS = 600
INSTALLED_INSTANCE_STREAM_LIMIT = 64 * 1024
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
EXPECTED_LEDGER_DIGEST = "0df321b0357d9093181d1446ac0972d071ed167a305b35abb30759252480427c"
EXPECTED_LEDGER_PREFIX_058 = "06b2f093c0d1d718236768f6bfc474b6e96c0eaded1f1db108842004a0365e1b"
EXPECTED_LEDGER_PREFIX_057 = "9b27f55d431a837eaa4fb685d4c70f0d54250f1ff2e67b55fa63ce9307490ab4"
EXPECTED_AI_FEATURE_CONSTANTS = {
    "AI_INPUT_SCHEMA": "finance-independent-ai-input-v1",
    "AI_RESULT_SCHEMA": "finance-independent-ai-result-v1",
    "AI_PUBLICATION_SCHEMA": "finance-independent-ai-publication-v1",
    "AI_INTERPRETATION_SCHEMA": "finance-independent-ai-interpretation-input-v1",
}
EXPECTED_AI_ASSET_HASHES = {
    "prompt_v1.txt": "351cff1db65d8c3aa56a7a73ff2b72753f41b7fce5fb187a3ae35025c47dfe40",
    "schema_v1.json": "84e46f4efcb9eb8100553815b0244a78cb141ce1aaa061fa9b125f90054ef1b0",
    "policy_v1.json": "7ca7503569947ed09fbf2b5ab31b8d333b24b4bfb83d4235baafac94b57e4d8a",
    "text_readiness_v1.json": "3cc43eb20b7f317632da2d59044a0e14918b84ffc986df58663c3d2927c10cd6",
}
EXPECTED_AI_REGISTRY_SHA256 = "e60958d34099c366ac33399fa6e6b92c111e00a8e7885fc929dc8d238364e66f"
EXPECTED_AI_V2_REGISTRY_SHA256 = "7d1f5430c1cd00892b0eb764adfd03ca210ee22ee6fdbd3263da87c88bdbd9f8"
EXPECTED_AI_V2_ASSET_HASHES = {
    "feature_v2.json": "a74c8b112df8c0b4641203b10ad341ec10038eecfbf03ab221d507971adcd036",
    "policy_v2.json": "cf6dcb6d606c7a48b5cf63623272306cbbcad889a738fb255caa77679d894fef",
    "prompt_v2.txt": "72bd6c06e85c83bcf942b731ba9325a372add5766d21eca21f5971ae1741d8a9",
    "schema_v2.json": "18c1cdfe00e0fd670e2c5d5d1048fab1a2a32ecb8b8194797dc53f4e89535f60",
}
EXPECTED_AI_V2_FEATURE = {
    "historical_read_only": ["finance-independent-ai-input-v1"],
    "methods": [
        "prepare_initial",
        "prepare_edit",
        "prepare_clarification",
        "get_clarification",
        "prepare_followup",
        "claim",
        "invoke",
        "record_outcome",
        "publish",
        "cancel",
        "get_status",
        "recover",
    ],
    "minimum_migration": "059",
    "schema": "finance-independent-ai-feature-v2",
    "subschemas": [
        "finance-independent-ai-input-v2",
        "finance-independent-ai-result-v2",
        "finance-independent-ai-publication-v2",
        "finance-independent-ai-interpretation-input-v2",
        "finance-application-posting-review-v2",
        "finance-application-posting-human-decision-v2",
    ],
}


def _load_bridge_distribution_verifier(script: Path):
    spec = importlib.util.spec_from_file_location("bridge_distribution_inventory_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    cache = script.parent / "__pycache__"
    cache_existed = cache.exists()
    # Execute trusted repository source directly: no loader bytecode cache may
    # enter the Bridge build's reviewed source inventory. The spec-created
    # module keeps __main__ false, preserving the verifier's CLI boundary.
    exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
    assert cache_existed or not cache.exists()
    return module


@pytest.fixture(scope="module")
def bridge_distribution_verifier():
    return _load_bridge_distribution_verifier(
        REPOSITORY_ROOT / "plugins/finance-bridge/scripts/verify-core-distribution.py"
    )


def test_bridge_distribution_inventory_import_does_not_create_bytecode_cache(
    tmp_path: Path,
) -> None:
    script = tmp_path / "verify_core_distribution.py"
    script.write_bytes(
        (
            REPOSITORY_ROOT / "plugins/finance-bridge/scripts/verify-core-distribution.py"
        ).read_bytes()
    )
    cache = tmp_path / "__pycache__"
    assert not cache.exists()
    verifier = _load_bridge_distribution_verifier(script)
    assert verifier._migration_digest(_bridge_migration_payloads()) == EXPECTED_LEDGER_DIGEST
    assert not cache.exists()


def _bridge_migration_payloads() -> dict[str, bytes]:
    return {
        f"finance_core/resources/migrations/{path.name}": path.read_bytes()
        for path in migration_resource_paths()
    }


def _synthetic_060_migration_payloads() -> dict[str, bytes]:
    payloads = {
        path: body
        for path, body in _bridge_migration_payloads().items()
        if int(PurePosixPath(path).name[:3]) <= 59
    }
    payloads["finance_core/resources/migrations/060_synthetic_inventory.sql"] = (
        b"-- synthetic inventory member; never written to source\n"
    )
    return payloads


def _payload_ledger_digest(payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, body in sorted(payloads.items(), key=lambda item: PurePosixPath(item[0]).name):
        digest.update(PurePosixPath(path).name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def test_bridge_distribution_inventory_accepts_synthetic_complete_060(
    bridge_distribution_verifier,
) -> None:
    payloads = _synthetic_060_migration_payloads()

    assert len(payloads) == 60
    assert bridge_distribution_verifier._migration_digest(payloads) == _payload_ledger_digest(
        payloads
    )


def test_bridge_distribution_inventory_rejects_missing_synthetic_060(
    bridge_distribution_verifier,
) -> None:
    payloads = _synthetic_060_migration_payloads()
    del payloads["finance_core/resources/migrations/060_synthetic_inventory.sql"]

    with pytest.raises(ValueError, match="migration inventory"):
        bridge_distribution_verifier._migration_digest(payloads)


def test_bridge_distribution_inventory_rejects_extra_synthetic_061(
    bridge_distribution_verifier,
) -> None:
    payloads = _synthetic_060_migration_payloads()
    payloads["finance_core/resources/migrations/061_synthetic_inventory.sql"] = (
        b"-- synthetic inventory member; never written to source\n"
    )

    with pytest.raises(ValueError, match="migration inventory"):
        bridge_distribution_verifier._migration_digest(payloads)


def test_bridge_distribution_inventory_accepts_reviewed_complete_060(
    bridge_distribution_verifier,
) -> None:
    payloads = _bridge_migration_payloads()
    assert len(payloads) == 60
    assert bridge_distribution_verifier._migration_digest(payloads) == EXPECTED_LEDGER_DIGEST


@pytest.mark.parametrize(
    "mutation", ["missing_060", "missing_middle", "extra_061", "duplicate_055"]
)
def test_bridge_distribution_inventory_rejects_incomplete_or_extra_migrations(
    bridge_distribution_verifier, mutation: str
) -> None:
    payloads = _bridge_migration_payloads()
    if mutation in {"missing_060", "missing_middle"}:
        sequence = "060_" if mutation == "missing_060" else "028_"
        missing = next(name for name in payloads if PurePosixPath(name).name.startswith(sequence))
        del payloads[missing]
    else:
        sequence = "061" if mutation == "extra_061" else "055"
        payloads[f"finance_core/resources/migrations/{sequence}_synthetic_duplicate.sql"] = (
            b"-- test"
        )
    with pytest.raises(ValueError, match="migration inventory"):
        bridge_distribution_verifier._migration_digest(payloads)


def test_bridge_distribution_inventory_byte_change_cannot_match_pinned_ledger(
    bridge_distribution_verifier,
) -> None:
    payloads = _bridge_migration_payloads()
    migration = next(name for name in payloads if PurePosixPath(name).name.startswith("060_"))
    payloads[migration] += b"\n-- synthetic byte change, never written to source\n"
    observed = bridge_distribution_verifier._migration_digest(payloads)
    assert observed != EXPECTED_LEDGER_DIGEST


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

    assert len(paths) == 60
    assert [int(path.name[:3]) for path in paths] == list(range(1, 61))
    assert all(path.resolve().is_relative_to(package_root.resolve()) for path in paths)
    assert _ledger_digest(paths) == EXPECTED_LEDGER_DIGEST == MIGRATION_LEDGER_DIGEST
    assert _ledger_digest(paths[:58]) == EXPECTED_LEDGER_PREFIX_058
    assert not (REPOSITORY_ROOT / "database" / "migrations").exists()


def test_previous_migration_bytes_remain_unchanged_through_057() -> None:
    paths = migration_resource_paths()

    assert len(paths) == 60
    assert _ledger_digest(paths[:57]) == EXPECTED_LEDGER_PREFIX_057


def test_migration_runtime_authority_reloads_the_non_executable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_resource_paths.cache_clear()
    monkeypatch.setattr(resource_runtime, "MIGRATION_LEDGER_DIGEST", "0" * 64)
    monkeypatch.setattr(resource_runtime, "MIGRATION_FILENAMES", ("001_wrong.sql",))

    paths = migration_resource_paths()

    assert len(paths) == 60
    assert _ledger_digest(paths) == EXPECTED_LEDGER_DIGEST
    migration_resource_paths.cache_clear()


def test_migration_runtime_rejects_reordered_contract_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest, filenames = resource_runtime._load_migration_contract()
    monkeypatch.setattr(
        resource_runtime,
        "_load_migration_contract",
        lambda: (digest, tuple(reversed(filenames))),
    )
    migration_resource_paths.cache_clear()
    try:
        with pytest.raises(resource_runtime.MigrationResourceError, match="inventory has drifted"):
            migration_resource_paths()
    finally:
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
    runtime_root: Path,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(runtime_python),
            "-I",
            "-S",
            "-c",
            (
                "import hashlib, importlib.util, json, sys; "
                f"sys.path.insert(0, {str(install_dir)!r}); "
                "import finance_core; "
                "import finance_core.instance_admission as instance_admission; "
                "import dataclasses; "
                "from finance_core.application.ai_invocation import AiInvocationService; "
                "import finance_core.application.ai_invocation_contract as ai_contract; "
                "from finance_core.application.ai_invocation_contract import ("
                "ClarificationReview, ClarificationStatus, ExpectedInterpretationInputV2, "
                "VerifiedInterpretationInputV2); "
                "from finance_core.application.ai_invocation_v2 import CLARIFICATION_SCHEMA; "
                "from finance_core.parser_proposals.independent_ai_validation import assets; "
                "from finance_core.resources import ("
                "MIGRATION_LEDGER_DIGEST, MIGRATION_PREFLIGHT_SHA256, "
                "migration_preflight_path, migration_resource_paths); "
                "from decimal import Decimal; "
                "from importlib.metadata import distribution; "
                "from importlib.resources import files; "
                "dist = distribution('finance-core'); "
                "ai_root = files('finance_core.resources').joinpath('independent_ai'); "
                "ai_assets = assets(); "
                "ai_asset_hashes = {name: hashlib.sha256("
                "ai_root.joinpath(name).read_bytes()).hexdigest() for name in ai_assets}; "
                "ai_assets_v2 = assets('v2'); "
                "ai_asset_hashes_v2 = {name: hashlib.sha256("
                "ai_root.joinpath(name).read_bytes()).hexdigest() for name in ai_assets_v2}; "
                "v2_feature = json.loads(ai_root.joinpath('feature_v2.json').read_text()); "
                "v2_types = (ClarificationReview, ClarificationStatus, "
                "ExpectedInterpretationInputV2, VerifiedInterpretationInputV2); "
                "print(json.dumps({"
                "'api': finance_core.API_CONTRACT_VERSION, "
                "'instance_admission_api': {name: "
                "callable(getattr(instance_admission, name, None)) "
                "for name in ("
                "'prepare_production_profile', 'admit_production_instance', "
                "'inspect_instance_admission', 'resume_authority_transition', "
                "'acquire_production_owner', 'production_write_operation', "
                "'production_read_operation', 'enter_maintenance_hold', "
                "'inspect_maintenance_hold', 'abort_maintenance_hold', "
                "'fence_production_instance', 'require_finance_write_connection', "
                "'require_finance_read_connection')}, "
                "'canonical': finance_core.canonical_money_str(Decimal('12.3'), 'SGD'), "
                "'ai_service_module': AiInvocationService.__module__, "
                "'ai_contract_module': ai_contract.__name__, "
                "'ai_feature_constants': {name: getattr(ai_contract, name + '_V1') for name in ("
                "'AI_INPUT_SCHEMA', 'AI_RESULT_SCHEMA', 'AI_PUBLICATION_SCHEMA', "
                "'AI_INTERPRETATION_SCHEMA')}, "
                "'interpretation_policy': vars(ai_contract.InterpretationPolicy('UTC', 'SGD')), "
                "'ai_asset_hashes': ai_asset_hashes, "
                "'ai_asset_paths': [str(ai_root.joinpath(name)) for name in ai_assets], "
                "'ai_registry_path': str(ai_root.joinpath('registry_v1.json')), "
                "'ai_registry_sha256': hashlib.sha256("
                "ai_root.joinpath('registry_v1.json').read_bytes()).hexdigest(), "
                "'ai_v2_feature_constants': {name: getattr(ai_contract, name) for name in ("
                "'AI_INPUT_SCHEMA', 'AI_RESULT_SCHEMA', 'AI_PUBLICATION_SCHEMA', "
                "'AI_INTERPRETATION_SCHEMA')}, "
                "'ai_v2_asset_hashes': ai_asset_hashes_v2, "
                "'ai_v2_asset_paths': [str(ai_root.joinpath(name)) for name in ai_assets_v2], "
                "'ai_v2_registry_path': str(ai_root.joinpath('registry_v2.json')), "
                "'ai_v2_registry_sha256': hashlib.sha256("
                "ai_root.joinpath('registry_v2.json').read_bytes()).hexdigest(), "
                "'ai_v2_feature': v2_feature, "
                "'ai_v2_clarification_schema': CLARIFICATION_SCHEMA, "
                "'ai_v2_methods': {name: callable(getattr(AiInvocationService, name, None)) "
                "for name in v2_feature['methods']}, "
                "'ai_v2_type_modules': {item.__name__: item.__module__ for item in v2_types}, "
                "'ai_v2_type_fields': {item.__name__: [field.name for field in "
                "dataclasses.fields(item)] for item in v2_types}, "
                "'distribution_name': dist.metadata['Name'], "
                "'distribution_version': dist.version, "
                "'money_module': finance_core.Money.__module__, "
                "'module_file': finance_core.__file__, "
                "'migration_count': len(migration_resource_paths()), "
                "'migration_digest': MIGRATION_LEDGER_DIGEST, "
                "'migration_paths': [str(path) for path in migration_resource_paths()], "
                "'migration_filenames': [path.name for path in migration_resource_paths()], "
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
        env={
            "FINANCE_RUNTIME_ROOT": str(runtime_root),
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _probe_installed_correction_flow(
    runtime_python: Path,
    install_dir: Path,
    outside_dir: Path,
    runtime_root: Path,
) -> dict[str, object]:
    """Run real text and receipt correction fixtures against installed bytes only."""
    helper_root = outside_dir / "independent-correction-fixtures"
    helper_tests = helper_root / "tests"
    helper_tests.mkdir(parents=True, exist_ok=True)
    (helper_tests / "__init__.py").write_text("", encoding="utf-8")
    # The copied helpers contain the repository's signed synthetic posting
    # fixtures; the runtime subprocess never adds the checkout to sys.path.
    shutil.copyfile(
        REPOSITORY_ROOT / "tests" / "test_application_posting_recovery_v1.py",
        helper_tests / "test_application_posting_recovery_v1.py",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "tests" / "test_independent_corrections_v1.py",
        helper_tests / "test_independent_corrections_v1.py",
    )
    (helper_tests / "conftest.py").write_text(
        "from pathlib import Path\n"
        "import sqlite3\n"
        "from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS\n"
        "from finance_core.staging_guard import create_staging_database\n"
        "def connect_temp_db(db_path: Path):\n"
        "    path = db_path.resolve()\n"
        "    if path.exists():\n"
        "        connection = sqlite3.connect(str(path))\n"
        "        connection.row_factory = sqlite3.Row\n"
        "        connection.execute('PRAGMA foreign_keys=ON')\n"
        "        return connection\n"
        "    return create_staging_database(path, migration_paths=TEMP_DB_MIGRATION_PATHS)\n",
        encoding="utf-8",
    )
    # Only decorators are needed while importing the copied helper modules.
    # This tiny shim allows the same fixtures to load under the intentionally
    # site-disabled package runtime without installing pytest there.
    (helper_root / "pytest.py").write_text(
        "class _Mark:\n"
        "    @staticmethod\n"
        "    def parametrize(*args, **kwargs):\n"
        "        return lambda function: function\n"
        "mark = _Mark()\n"
        "class MonkeyPatch: pass\n",
        encoding="utf-8",
    )
    dependency_root = helper_root / "runtime-dependencies"
    dependency_root.mkdir(exist_ok=True)
    if not any(dependency_root.iterdir()):
        dependency_site = Path(sysconfig.get_paths()["purelib"])
        for name in (
            "pypdf",
            "pypdf-6.16.1.dist-info",
            "typing_extensions.py",
            "typing_extensions-4.16.0.dist-info",
        ):
            source = dependency_site / name
            if source.is_dir():
                shutil.copytree(source, dependency_root / name)
            elif source.is_file():
                shutil.copyfile(source, dependency_root / name)
            else:
                raise AssertionError(f"required pinned runtime dependency is missing: {source}")
    probe_path = helper_root / "installed_correction_probe.py"
    shutil.copyfile(REPOSITORY_ROOT / "tests" / "installed_correction_probe.py", probe_path)
    completed = subprocess.run(
        [
            str(runtime_python),
            "-I",
            "-S",
            str(probe_path),
            str(install_dir),
            str(helper_root),
            str(outside_dir),
            str(runtime_root),
            str(REPOSITORY_ROOT),
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


def _probe_installed_instance_flow(
    runtime_python: Path,
    install_dir: Path,
    outside_dir: Path,
    runtime_root: Path,
) -> dict[str, object]:
    """Reuse the finite signed fixture/dependency set prepared by the correction probe."""
    helper_root = outside_dir / "independent-correction-fixtures"
    assert (helper_root / "runtime-dependencies").is_dir()
    for name in (
        "instance_admission_business_support_v1.py",
        "application_amendment_support_v1.py",
        "test_application_amendment_v1.py",
        "test_independent_ai_invocation_v1.py",
        "independent_ai_candidate_fixture_v2.py",
    ):
        shutil.copyfile(REPOSITORY_ROOT / "tests" / name, helper_root / "tests" / name)
    probe_path = helper_root / "installed_instance_admission_probe.py"
    shutil.copyfile(REPOSITORY_ROOT / "tests" / probe_path.name, probe_path)
    return _run_installed_instance_child(
        [
            str(runtime_python),
            "-I",
            "-S",
            str(probe_path),
            str(install_dir),
            str(helper_root),
            str(outside_dir),
            str(runtime_root),
            str(REPOSITORY_ROOT),
        ],
        install_dir,
        outside_dir,
    )


def _bounded_instance_output(output: bytes | None) -> str:
    raw = output or b""
    payload = raw.decode("utf-8", errors="replace").encode("utf-8")
    if len(payload) <= INSTALLED_INSTANCE_STREAM_LIMIT:
        return payload.decode("utf-8")
    marker = f"\n...[truncated; total {len(raw)} bytes]...\n"
    remaining = INSTALLED_INSTANCE_STREAM_LIMIT - len(marker.encode())
    head = payload[: remaining // 2].decode("utf-8", errors="ignore")
    tail = payload[-(remaining - remaining // 2) :].decode("utf-8", errors="ignore")
    return head + marker + tail


def _run_installed_instance_child(
    command: list[str],
    install_dir: Path,
    outside_dir: Path,
    *,
    timeout_seconds: float = INSTALLED_INSTANCE_TIMEOUT_SECONDS,
) -> dict[str, object]:
    # Per-statement admission retains the complete real business graph. This
    # finite resource cap does not replace any financial or isolation assertion.
    started = monotonic()
    failure = None
    try:
        completed = subprocess.run(
            command,
            check=False,
            cwd=outside_dir,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.stdout, error.stderr
        failure = f"timeout after {timeout_seconds:g}s"
    else:
        stdout, stderr = completed.stdout, completed.stderr
        if completed.returncode:
            failure = f"nonzero exit {completed.returncode}"
        else:
            try:
                payload = json.loads(stdout)
            except (ValueError, UnicodeDecodeError):
                failure = "invalid JSON output"
    elapsed = monotonic() - started
    identity = f"installed-instance artifact={install_dir.name} elapsed={elapsed:.3f}s"
    if failure is not None:
        # Suppress exception chaining: subprocess exceptions retain unbounded
        # captured streams, which must not reappear in the rendered traceback.
        raise AssertionError(
            f"{identity}: {failure}\n"
            f"stdout:\n{_bounded_instance_output(stdout)}\n"
            f"stderr:\n{_bounded_instance_output(stderr)}"
        ) from None
    print(f"{identity}: success\n{_bounded_instance_output(stderr)}", file=sys.stderr, flush=True)
    return payload


def test_installed_instance_child_success_preserves_json_and_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run_installed_instance_child(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import json, sys; print(json.dumps(dict(ok=True))); "
            'print("synthetic phase", file=sys.stderr, flush=True)',
        ],
        tmp_path / "wheel-installed",
        tmp_path,
    )
    assert payload == {"ok": True}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "artifact=wheel-installed elapsed=" in captured.err
    assert "synthetic phase" in captured.err


@pytest.mark.parametrize("complete_json", [False, True])
def test_installed_instance_child_timeout_retains_partial_output(
    tmp_path: Path, complete_json: bool
) -> None:
    output = '{"ok": true}' if complete_json else "partial stdout"
    program = (
        f"import sys, time; print({output!r}, flush=True); "
        'print("synthetic phase", file=sys.stderr, flush=True); time.sleep(10)'
    )
    with pytest.raises(AssertionError, match="timeout after 1s") as failure:
        _run_installed_instance_child(
            [sys.executable, "-I", "-S", "-c", program],
            tmp_path / "sdist-installed",
            tmp_path,
            timeout_seconds=1,
        )
    message = str(failure.value)
    assert "artifact=sdist-installed elapsed=" in message
    assert output in message
    assert "synthetic phase" in message
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("program", "reason"),
    [
        ("import json, sys; print(json.dumps(dict(ok=True))); sys.exit(7)", "nonzero exit 7"),
        ('print("not JSON")', "invalid JSON output"),
        ('import sys; sys.stdout.buffer.write(b"\\xff")', "invalid JSON output"),
    ],
)
def test_installed_instance_child_rejects_failed_or_invalid_result(
    tmp_path: Path, program: str, reason: str
) -> None:
    with pytest.raises(AssertionError, match=reason) as failure:
        _run_installed_instance_child(
            [sys.executable, "-I", "-S", "-c", program],
            tmp_path / "wheel-installed",
            tmp_path,
        )
    assert "artifact=wheel-installed elapsed=" in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True


@pytest.mark.parametrize("unit", [b"x", b"\xff", "界".encode()])
def test_installed_instance_output_truncation_bounds_rendered_bytes(unit: bytes) -> None:
    rendered = _bounded_instance_output(unit * INSTALLED_INSTANCE_STREAM_LIMIT)
    assert len(rendered.encode("utf-8")) <= INSTALLED_INSTANCE_STREAM_LIMIT
    if unit != b"x":
        assert "[truncated; total " in rendered
    assert _bounded_instance_output(None) == ""


def test_installed_instance_child_bounds_both_failure_streams(tmp_path: Path) -> None:
    program = (
        'import sys; print("out-head" + "x" * 70000 + "out-tail"); '
        'print("err-head" + "y" * 70000 + "err-tail", file=sys.stderr); sys.exit(9)'
    )
    with pytest.raises(AssertionError, match="nonzero exit 9") as failure:
        _run_installed_instance_child(
            [sys.executable, "-I", "-S", "-c", program],
            tmp_path / "wheel-installed",
            tmp_path,
        )
    message = str(failure.value)
    assert len(message.encode()) < 2 * INSTALLED_INSTANCE_STREAM_LIMIT + 200
    assert message.count("[truncated; total ") == 2
    assert all(marker in message for marker in ("out-head", "out-tail", "err-head", "err-tail"))
    assert failure.value.__suppress_context__ is True


@pytest.mark.parametrize("business_fails", [False, True])
def test_installed_instance_probe_entrypoint_diagnostics_and_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], business_fails: bool
) -> None:
    install_dir = tmp_path / "synthetic-installed"
    core = install_dir / "finance_core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    helper_root = tmp_path / "synthetic-helpers"
    helper_tests = helper_root / "tests"
    helper_tests.mkdir(parents=True)
    for name in (
        "__init__.py",
        "test_application_posting_recovery_v1.py",
        "test_independent_corrections_v1.py",
    ):
        (helper_tests / name).write_text("", encoding="utf-8")
    outcome = (
        '    raise RuntimeError("synthetic business failure")\n'
        if business_fails
        else '    return {"synthetic": True}\n'
    )
    (helper_tests / "instance_admission_business_support_v1.py").write_text(
        "import sys\n"
        "def run_installed_business_probe(data_root):\n"
        "    assert data_root.is_dir()\n"
        "    for name in ('test_application_posting_recovery_v1', "
        "'test_independent_corrections_v1'):\n"
        "        assert sys.modules[name] is sys.modules['tests.' + name]\n" + outcome,
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-I",
        "-S",
        str(REPOSITORY_ROOT / "tests/installed_instance_admission_probe.py"),
        str(install_dir),
        str(helper_root),
        str(tmp_path),
        str(tmp_path / "synthetic-runtime"),
        str(REPOSITORY_ROOT),
    ]
    if business_fails:
        with pytest.raises(AssertionError, match="nonzero exit 1") as failure:
            _run_installed_instance_child(command, install_dir, tmp_path, timeout_seconds=5)
        diagnostics = str(failure.value)
        assert "RuntimeError: synthetic business failure" in diagnostics
        assert "business-start" in diagnostics
        assert "business-end" not in diagnostics
        assert "json-output-start" not in diagnostics
        assert "main-return" not in diagnostics
    else:
        payload = _run_installed_instance_child(command, install_dir, tmp_path, timeout_seconds=5)
        assert payload == {
            "business": {"synthetic": True},
            "module_files": {"finance_core": str((core / "__init__.py").resolve())},
            "sys_path_has_checkout": False,
        }
        captured = capsys.readouterr()
        assert captured.out == ""
        stages = [line.split()[1] for line in captured.err.splitlines()[1:] if line]
        assert stages == [
            "imports-start",
            "imports-end",
            "isolation-start",
            "isolation-end",
            "business-start",
            "business-end",
            "final-modules-start",
            "final-modules-end",
            "json-output-start",
            "json-output-end",
            "main-return",
        ]
        assert "synthetic-runtime" not in captured.err


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
from finance_core.application.amendment import AmendmentService
from finance_core.application.amendment_contract import AmendmentBinding, AmendmentError
from finance_core.application.admission import TrustedBinding
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths

class UnusedAuthority:
    calls = 0
    def verify_persisted(self, *_args):
        self.calls += 1
        raise AssertionError("Absent status must not invent source or edit authority")

from pathlib import Path
import finance_core.application.amendment as installed_amendment
assert Path(installed_amendment.__file__).resolve().is_relative_to(Path({str(install_dir)!r}))
probe_database = Path({str(external_workspace)!r}) / (
    "amendment-" + Path({str(install_dir)!r}).name + ".sqlite")
with create_staging_database(probe_database) as connection:
    apply_migration_paths(connection, TEMP_DB_MIGRATION_PATHS)
    binding = TrustedBinding("synthetic-instance", "synthetic-human", "synthetic-client",
        "synthetic-source", "source-key", "synthetic-decision", "decision-key")
    unused_authority = UnusedAuthority()
    amendments = AmendmentService(connection=connection, source_verifier=unused_authority,
        human_amendment_authority=unused_authority,
        binding=AmendmentBinding(binding, "synthetic-edit", "edit-key"), clock=lambda: 1)
    absent_amendment_refused = False
    try:
        amendments.get_status("synthetic-missing-amendment")
    except AmendmentError:
        absent_amendment_refused = True
    assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert unused_authority.calls == 0

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
    "absent_amendment_refused": absent_amendment_refused,
}}))
"""
    completed = subprocess.run(
        [str(runtime_python), "-I", "-c", program],
        check=False,
        cwd=outside_dir,
        env={
            "FINANCE_RUNTIME_ROOT": str(runtime_root),
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
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
        payload = _probe_installed_package(runtime_python, install_dir, outside_dir, runtime_root)
        correction_flow = _probe_installed_correction_flow(
            runtime_python, install_dir, outside_dir, runtime_root
        )
        instance_flow = _probe_installed_instance_flow(
            runtime_python, install_dir, outside_dir, runtime_root
        )
        assert instance_flow["sys_path_has_checkout"] is False
        assert instance_flow["business"]["text_posted"] is True
        assert instance_flow["business"]["receipt_posted"] is True
        assert instance_flow["business"]["current_versions"] == [1, 1]
        assert instance_flow["business"]["economic_events"] == 2
        assert instance_flow["business"]["originals_unchanged"] is True
        assert instance_flow["business"]["readonly_verified"] is True
        assert instance_flow["module_files"]
        assert all(
            Path(path).resolve().is_relative_to(install_dir.resolve())
            for path in instance_flow["module_files"].values()
        )
        assert len(payload["instance_admission_api"]) == 13
        assert all(payload["instance_admission_api"].values())
        assert (
            Path(str(correction_flow["module_file"]))
            .resolve()
            .is_relative_to(install_dir.resolve())
        )
        assert correction_flow["sys_path_has_checkout"] is False
        assert correction_flow["text"] == {
            "route": "text",
            "applied": 0,
            "current": 2,
            "fields": {
                "amount": "18.75",
                "currency": "SGD",
                "transaction_date": "2026-10-09",
                "merchant": "Cafe Two",
                "description": "Team lunch",
                "category": "travel",
            },
        }
        assert correction_flow["receipt"] == {
            "route": "receipt",
            "applied": 0,
            "current": 2,
            "fields": {
                "amount": "18.75",
                "currency": "SGD",
                "transaction_date": "2026-10-09",
                "merchant": "Cafe Two",
                "description": "Second receipt memo",
                "category": "meals",
            },
        }
        assert payload["api"] == "finance-core-api-v1"
        assert payload["canonical"] == "12.30"
        assert payload["ai_service_module"] == "finance_core.application.ai_invocation"
        assert payload["ai_contract_module"] == "finance_core.application.ai_invocation_contract"
        assert payload["ai_feature_constants"] == EXPECTED_AI_FEATURE_CONSTANTS
        assert payload["interpretation_policy"] == {"timezone": "UTC", "default_currency": "SGD"}
        assert payload["ai_asset_hashes"] == EXPECTED_AI_ASSET_HASHES
        assert payload["ai_registry_sha256"] == EXPECTED_AI_REGISTRY_SHA256
        assert payload["ai_v2_feature_constants"] == {
            "AI_INPUT_SCHEMA": "finance-independent-ai-input-v2",
            "AI_RESULT_SCHEMA": "finance-independent-ai-result-v2",
            "AI_PUBLICATION_SCHEMA": "finance-independent-ai-publication-v2",
            "AI_INTERPRETATION_SCHEMA": "finance-independent-ai-interpretation-input-v2",
        }
        assert payload["ai_v2_asset_hashes"] == EXPECTED_AI_V2_ASSET_HASHES
        assert payload["ai_v2_registry_sha256"] == EXPECTED_AI_V2_REGISTRY_SHA256
        assert payload["ai_v2_feature"] == EXPECTED_AI_V2_FEATURE
        assert (
            payload["ai_v2_clarification_schema"]
            == "finance-independent-ai-clarification-review-v2"
        )
        assert payload["ai_v2_methods"] == {
            name: True for name in EXPECTED_AI_V2_FEATURE["methods"]
        }
        assert payload["ai_v2_type_modules"] == {
            "ClarificationReview": "finance_core.application.ai_invocation_contract",
            "ClarificationStatus": "finance_core.application.ai_invocation_contract",
            "ExpectedInterpretationInputV2": "finance_core.application.ai_invocation_contract",
            "VerifiedInterpretationInputV2": "finance_core.application.ai_invocation_contract",
        }
        assert payload["ai_v2_type_fields"] == {
            "ClarificationReview": ["request_id", "request_hash", "projection", "expires_at"],
            "ClarificationStatus": ["request_id", "state", "projection", "expires_at"],
            "ExpectedInterpretationInputV2": [
                "binding",
                "source",
                "interaction_kind",
                "interaction_id",
                "interaction_projection_hash",
                "proposal_public_id",
                "proposal_version",
                "proposal_content_hash",
                "checked_at",
                "historical",
            ],
            "VerifiedInterpretationInputV2": [
                "schema",
                "instance_id",
                "human_principal_id",
                "evidence_id",
                "evidence_digest",
                "source_evidence_id",
                "source_evidence_digest",
                "interaction_kind",
                "interaction_id",
                "interaction_projection_hash",
                "proposal_public_id",
                "proposal_version",
                "proposal_content_hash",
                "display_id",
                "display_evidence_digest",
                "reply_to_display_id",
                "text",
                "observed_at",
                "expires_at",
                "revoked",
            ],
        }
        assert all(
            Path(path).resolve().is_relative_to(install_dir.resolve())
            for path in [
                *payload["ai_asset_paths"],
                payload["ai_registry_path"],
                *payload["ai_v2_asset_paths"],
                payload["ai_v2_registry_path"],
            ]
        )
        assert payload["distribution_name"] == "finance-core"
        assert payload["distribution_version"] == "0.1.6"
        assert payload["money_module"] == "finance_core.money"
        assert payload["migration_count"] == 60
        assert [int(name[:3]) for name in payload["migration_filenames"]] == list(range(1, 61))
        assert payload["migration_filenames"][-1] == "060_independent_corrections.sql"
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
            "absent_amendment_refused": True,
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

    distribution_prefix = "finance_core-0.1.6.dist-info"
    expected_package_payloads = {
        path.relative_to(REPOSITORY_ROOT).as_posix(): path.read_bytes()
        for path in (REPOSITORY_ROOT / "finance_core").rglob("*")
        if path.is_file() and path.suffix in {".json", ".py", ".sql", ".txt"}
    }
    expected_package_members = set(expected_package_payloads)
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
        for name, expected_payload in expected_package_payloads.items():
            assert archive.read(name) == expected_payload
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
        assert {path.parts[0] for path in member_paths} == {"finance_core-0.1.6"}
        files = [member for member in members if member.isfile()]
        observed_files = {PurePosixPath(member.name).parts[1:] for member in files}
        expected_source_files = {PurePosixPath(path).parts for path in expected_package_members}
        assert observed_files == expected_source_files | expected_sdist_metadata
        payloads = []
        observed_source_payloads: dict[str, bytes] = {}
        for member in files:
            extracted = archive.extractfile(member)
            assert extracted is not None
            payload = extracted.read()
            payloads.append(payload)
            relative_path = PurePosixPath(member.name).parts[1:]
            observed_source_payloads[PurePosixPath(*relative_path).as_posix()] = payload
        for name, expected_payload in expected_package_payloads.items():
            assert observed_source_payloads[name] == expected_payload
        _assert_safe_text_payloads(payloads)
