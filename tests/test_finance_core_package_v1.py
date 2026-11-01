from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import textwrap
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
EXPECTED_LEDGER_DIGEST = "d333fff715b95938005bfb7a5bfc31ee94c29bd8c081db62b52932610e7d3850"
HISTORICAL_LEDGER_DIGEST_060 = "0df321b0357d9093181d1446ac0972d071ed167a305b35abb30759252480427c"
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


def test_bridge_distribution_inventory_rejects_extra_synthetic_062(
    bridge_distribution_verifier,
) -> None:
    payloads = _bridge_migration_payloads()
    payloads["finance_core/resources/migrations/062_synthetic_inventory.sql"] = (
        b"-- synthetic inventory member; never written to source\n"
    )

    with pytest.raises(ValueError, match="migration inventory"):
        bridge_distribution_verifier._migration_digest(payloads)


def test_bridge_distribution_inventory_accepts_reviewed_complete_061(
    bridge_distribution_verifier,
) -> None:
    payloads = _bridge_migration_payloads()
    assert len(payloads) == 61
    assert bridge_distribution_verifier._migration_digest(payloads) == EXPECTED_LEDGER_DIGEST


def test_bridge_distribution_inventory_accepts_actual_historical_060_prefix(
    bridge_distribution_verifier,
) -> None:
    payloads = {
        name: body
        for name, body in _bridge_migration_payloads().items()
        if int(PurePosixPath(name).name[:3]) <= 60
    }
    assert len(payloads) == 60
    assert bridge_distribution_verifier._migration_digest(payloads) == HISTORICAL_LEDGER_DIGEST_060


@pytest.mark.parametrize(
    "mutation",
    ["missing_060", "missing_middle", "extra_062", "replace_061_with_062", "duplicate_055"],
)
def test_bridge_distribution_inventory_rejects_incomplete_or_extra_migrations(
    bridge_distribution_verifier, mutation: str
) -> None:
    payloads = _bridge_migration_payloads()
    if mutation in {"missing_060", "missing_middle"}:
        sequence = "060_" if mutation == "missing_060" else "028_"
        missing = next(name for name in payloads if PurePosixPath(name).name.startswith(sequence))
        del payloads[missing]
    elif mutation == "replace_061_with_062":
        removed = next(name for name in payloads if PurePosixPath(name).name.startswith("061_"))
        payloads["finance_core/resources/migrations/062_replacement.sql"] = payloads.pop(removed)
    else:
        sequence = "062" if mutation == "extra_062" else "055"
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

    assert len(paths) == 61
    assert [int(path.name[:3]) for path in paths] == list(range(1, 62))
    assert all(path.resolve().is_relative_to(package_root.resolve()) for path in paths)
    assert _ledger_digest(paths) == EXPECTED_LEDGER_DIGEST == MIGRATION_LEDGER_DIGEST
    assert _ledger_digest(paths[:60]) == HISTORICAL_LEDGER_DIGEST_060
    assert _ledger_digest(paths[:58]) == EXPECTED_LEDGER_PREFIX_058
    assert not (REPOSITORY_ROOT / "database" / "migrations").exists()


def test_previous_migration_bytes_remain_unchanged_through_057() -> None:
    paths = migration_resource_paths()

    assert len(paths) == 61
    assert _ledger_digest(paths[:57]) == EXPECTED_LEDGER_PREFIX_057


def test_migration_runtime_authority_reloads_the_non_executable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_resource_paths.cache_clear()
    monkeypatch.setattr(resource_runtime, "MIGRATION_LEDGER_DIGEST", "0" * 64)
    monkeypatch.setattr(resource_runtime, "MIGRATION_FILENAMES", ("001_wrong.sql",))

    paths = migration_resource_paths()

    assert len(paths) == 61
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
                "'runtime_dependencies': dist.metadata.get_all('Requires-Dist'), "
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


def _installed_runtime_dependencies(outside_dir: Path) -> Path:
    """Provide the same pinned third-party bytes to every site-disabled package probe."""

    dependency_root = outside_dir / "independent-correction-fixtures" / "runtime-dependencies"
    dependency_root.mkdir(parents=True, exist_ok=True)
    dependency_site = Path(sysconfig.get_paths()["purelib"])
    for name in (
        "pypdf",
        "pypdf-6.21.2.dist-info",
        "typing_extensions.py",
        "typing_extensions-4.16.0.dist-info",
    ):
        source = dependency_site / name
        target = dependency_root / name
        if target.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copyfile(source, target)
        else:
            raise AssertionError(f"required pinned runtime dependency is missing: {source}")
    return dependency_root


def _installed_recovery_restart_program() -> str:
    """Fresh installed-only consumers reconstruct synthetic retained recovery material."""
    return textwrap.dedent(
        """
        import hashlib
        import json
        import os
        import sqlite3
        import sys
        from dataclasses import replace
        from pathlib import Path

        installed, dependencies, checkout, material_path = map(Path, sys.argv[1:5])
        mode = sys.argv[5]
        sys.path[:0] = [str(installed), str(dependencies)]
        import finance_core.lifecycle_v2 as lifecycle
        import finance_core.lifecycle_v2.recovery as recovery
        import finance_core.instance_admission as admission
        m = json.loads(material_path.read_text())
        b = m['config']
        os.environ['FINANCE_RUNTIME_ROOT'] = str(Path(b['profile_root']) / 'runtime')
        config = admission.TrustedInstanceConfig(
            Path(b['control_root']), Path(m['support_root']), b['profile_id'],
            layout=b['layout'], instance_id=b['instance_id'],
            config_identity=b['config_identity'], core_identity=b['core_identity'],
            approval_verifier=lambda ref, action, owner_binding: ref == 'approved',
        )
        cut = lifecycle.SnapshotCut.from_dict(m['cut'])
        receipt = recovery.OldWriterIsolationReceipt(**m['receipt'])
        authority = recovery.RecoveryAuthority('installed-synthetic-recovery', b'r' * 32)
        binding = lifecycle.RuntimeBinding(**m['binding'])
        class Port:
            def source(self, conn, evidence_id):
                return lifecycle.VerifiedSource(
                    evidence_id, 'actor', 'chat',
                    'message-1' if evidence_id == 'source1' else evidence_id,
                    m['raw'], m['original_sha'], 1792800000,
                )
            def provider(self, conn, evidence_id, expected):
                return lifecycle.VerifiedObservation(**m['observations'][evidence_id])
            def delivery(self, conn, evidence_id, expected):
                return lifecycle.VerifiedDelivery(
                    evidence_id, expected.review_id, expected.review_sha256, 'actor', 'chat',
                )
            def action(self, conn, evidence_id, expected):
                return lifecycle.VerifiedAction(
                    evidence_id, expected.review_id, expected.review_sha256,
                    expected.delivery_evidence_id, 'actor', 'chat',
                )
        port = Port()
        pending = config.control_root / admission.PENDING_FILENAME
        state_path = config.control_root / admission.STATE_FILENAME
        def reconstruct(selected_cut=cut, selected_receipt=receipt):
            return recovery.resume_held_restore(config, selected_cut, selected_receipt, authority)
        def resume(held, operation_id='restore-admit', selected_receipt=receipt):
            return recovery.resume_restore_promotion(
                held, operation_id=operation_id, approval_ref='approved',
                port=port, binding=binding, receipt=selected_receipt,
                recovery_authority=authority,
            )
        refused = []
        owner = None
        try:
            held = reconstruct()
            assert held.new_binding == binding
            observed_status = held.status
            if mode == 'pending':
                assert observed_status == 'RESTORE_ADMIT_PENDING'
                pending_bytes = pending.read_bytes()
                def refuse(name, action):
                    try:
                        action()
                    except (lifecycle.LifecycleError, admission.InstanceAdmissionError):
                        refused.append(name)
                    else:
                        raise AssertionError(name + ' unexpectedly accepted')
                    assert pending.read_bytes() == pending_bytes
                refuse('owner_while_pending', lambda: admission.acquire_production_owner(config))
                refuse('generic_resume', lambda: admission.resume_authority_transition(
                    config, operation_id='restore-admit', approval_ref='approved'))
                refuse('wrong_operation', lambda: resume(held, operation_id='wrong-operation'))
                wrong_cut = replace(cut, logical_database_sha256='0' * 64)
                refuse('wrong_cut', lambda: resume(reconstruct(wrong_cut)))
                wrong_receipt = replace(receipt, signature='0' * 64)
                refuse('wrong_receipt', lambda: resume(
                    reconstruct(selected_receipt=wrong_receipt), selected_receipt=wrong_receipt))
                database = Path(b['profile_root']) / 'runtime/database/finance.db'
                original_database = database.read_bytes()
                with sqlite3.connect(database) as offline:
                    offline.execute(
                        "INSERT INTO participants(public_id,display_name) VALUES "
                        "('installed-tamper','Installed tamper')")
                offline.close()
                try:
                    refuse('changed_content', lambda: resume(reconstruct()))
                finally:
                    with database.open('r+b') as restored:
                        restored.write(original_database)
                        restored.truncate()
                assert pending.read_bytes() == pending_bytes
                assert resume(reconstruct())['state'] == 'ACTIVE'
                assert not pending.exists()
            else:
                assert mode == 'completed' and observed_status == 'ACTIVE'
                authority_before = state_path.read_bytes()
                assert resume(held)['state'] == 'ACTIVE'
                assert state_path.read_bytes() == authority_before
                assert hashlib.sha256(authority_before).hexdigest() == m['completed_expectation'][
                    'authority_sha256']
            owner = admission.acquire_production_owner(config)
            service = lifecycle.LedgerService(owner, port, binding, clock=lambda: 1792800001)
            restored_fact = service.reader().get(m['posted']['record_id'])
            assert restored_fact['fact_sha256'] == m['posted']['fact_sha256']
            assert restored_fact['version'] == m['posted']['version']
            if mode == 'pending':
                active_source = service.capture('source-after-recovery', 'capture-after-recovery')
                active_source_id = active_source.source_id
            else:
                active_source_id = m['completed_expectation']['post_promotion_source_id']
                assert service.reader().source(active_source_id) is not None
            audit = service.reader().full_audit()
            assert audit['fact_version_count'] == 1 and audit['event_count'] == 6
            state_bytes = state_path.read_bytes()
            state = json.loads(state_bytes)
            assert sum(entry['action'] == 'restore_admit' for entry in state['history']) == 1
            if mode == 'completed':
                assert audit == m['completed_expectation']['audit']
                assert state['version'] == m['completed_expectation']['authority_version']
            module_paths = [
                Path(module.__file__).resolve()
                for name, module in sys.modules.items()
                if (name == 'finance_core' or name.startswith('finance_core.'))
                and getattr(module, '__file__', None)
            ]
            installed_only = all(path.is_relative_to(installed) for path in module_paths)
            assert installed_only and not any(
                Path(entry or '.').resolve().is_relative_to(checkout) for entry in sys.path)
            print(json.dumps({
                'mode': mode, 'pid': os.getpid(), 'installed_only': installed_only,
                'observed_status': observed_status, 'refused': refused,
                'pending_retained_on_refusal': mode == 'pending',
                'authority_sha256': hashlib.sha256(state_bytes).hexdigest(),
                'authority_version': state['version'], 'audit': audit,
                'post_promotion_source_id': active_source_id,
                'record_id': restored_fact['record_id'],
                'record_version': restored_fact['version'],
                'fact_sha256': restored_fact['fact_sha256'],
                'prepare_entry_sha256': held.restore_prepare_entry_sha256,
            }, sort_keys=True))
        finally:
            if owner is not None:
                owner.close()
            config.close()
        """
    )


def _probe_installed_lifecycle(
    runtime_python: Path, install_dir: Path, outside_dir: Path
) -> dict[str, object]:
    """Exercise the opt-in API and appended schema using only installed bytes."""

    program = textwrap.dedent(
        """
        import hashlib
        import json
        import os
        import sqlite3
        import subprocess
        import sys
        import tempfile
        from dataclasses import asdict
        from pathlib import Path
        from unittest.mock import patch

        installed = Path(sys.argv[1]).resolve()
        checkout = Path(sys.argv[2]).resolve()
        outside = Path(sys.argv[3]).resolve()
        dependency_root = Path(sys.argv[4]).resolve()
        restart_program = sys.argv[5]
        sys.path.insert(0, str(dependency_root))
        sys.path.insert(0, str(installed))
        import finance_core
        import finance_core.lifecycle_v2 as lifecycle
        import finance_core.instance_admission as admission
        import finance_core.lifecycle_v2.recovery as recovery
        from finance_core.lifecycle_v2.contracts import digest
        from finance_core.profile_layout import product_root, profile_root
        from finance_core.resources import MIGRATION_LEDGER_DIGEST, migration_resource_paths

        binding = lifecycle.RuntimeBinding('synthetic-instance', 'generation-1', 'actor', 'chat')
        refused_owner = False
        try:
            lifecycle.LedgerService(None, None, binding)
        except lifecycle.LifecycleError:
            refused_owner = True
        refused_runtime = False
        try:
            lifecycle.RuntimeBinding('synthetic-instance', 'generation-1', 'actor', 'chat',
                                     environment='production')
        except lifecycle.LifecycleError:
            refused_runtime = True

        paths = migration_resource_paths()
        migration = paths[-1]
        connection = sqlite3.connect(':memory:')
        connection.executescript(migration.read_text())
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'l2_%'")}
        connection.close()
        with tempfile.TemporaryDirectory(prefix='installed-lifecycle-', dir=outside) as scratch:
            scratch = Path(scratch)
            layout = 'mac' if sys.platform == 'darwin' else 'linux'
            support = scratch / ('Application Support' if layout == 'mac' else 'finance-codex')
            support.mkdir(mode=0o700)
            product = product_root(support, layout)
            profile = profile_root(support, 'synthetic', layout)
            if product != support:
                product.mkdir(mode=0o700)
            for directory in (
                profile.parent, profile, profile / 'runtime', profile / 'runtime/database',
                profile / 'workspace', profile / 'workspace/database', profile / 'backups',
                profile / 'work', profile / 'restore', scratch / 'control',
            ):
                directory.mkdir(mode=0o700)
            manifest = profile / 'profile.json'
            manifest.write_text(json.dumps({
                'profile_id': 'synthetic', 'runtime_root': str(profile / 'runtime'),
                'workspace_root': str(profile / 'workspace'),
            }))
            manifest.chmod(0o600)
            os.environ['FINANCE_RUNTIME_ROOT'] = str(profile / 'runtime')
            cfg = admission.TrustedInstanceConfig(
                scratch / 'control', support, 'synthetic', layout=layout,
                instance_id='synthetic-instance',
                config_identity='synthetic-config-v1', core_identity='synthetic-core-v1',
                approval_verifier=lambda ref, action, owner_binding: ref == 'approved',
            )
            owner = None
            new_config = None
            new_owner = None
            try:
                admission.prepare_production_profile(
                    cfg, operation_id='prepare', approval_ref='approved').close()
                admission.admit_production_instance(
                    cfg, operation_id='admit', approval_ref='approved')
                owner = admission.acquire_production_owner(cfg)
                raw = '2026-10-24 Cafe SGD 12.00'
                original_sha = hashlib.sha256(raw.encode()).hexdigest()
                operations = []
                for index, (field, value) in enumerate((
                    ('amount', '12.00'), ('currency', 'SGD'),
                    ('merchant', 'Cafe'), ('transaction_date', '2026-10-24'),
                )):
                    operation = {
                        'operation_id': f'o{index}', 'op': 'set', 'field': field,
                        'evidence': [{
                            'ref': 'source', 'start': raw.index(value),
                            'end': raw.index(value) + len(value), 'quote': value,
                            'role': 'replacement_value',
                        }],
                    }
                    if field == 'transaction_date':
                        operation['date_expression'] = {
                            'kind': 'absolute_date', 'year': 2026, 'month': 10, 'day': 24,
                        }
                    else:
                        operation['value'] = value
                    operations.append(operation)
                response = json.dumps({
                    'schema': 'finance-independent-ai-result-v2',
                    'outcome': 'proposal', 'intent': 'personal_expense',
                    'operations': operations, 'unresolved': [],
                    'coverage': [{
                        'start': 0, 'end': len(raw), 'label': 'resolved_instruction',
                        'operation_ids': [item['operation_id'] for item in operations],
                        'question_ids': [],
                    }],
                })
                class Port:
                    bad_actor = False
                    def __init__(self):
                        self.observations = {}
                    def source(self, conn, evidence_id):
                        return lifecycle.VerifiedSource(
                            evidence_id, 'other' if self.bad_actor else 'actor', 'chat',
                            'message-1', raw, original_sha, 1792800000,
                        )
                    def provider(self, conn, evidence_id, expected):
                        if evidence_id not in self.observations:
                            self.observations[evidence_id] = lifecycle.VerifiedObservation(
                                evidence_id, expected.source_id, 'actor', response,
                                digest(asdict(expected)), expected.projection['reply_text'],
                            )
                        return self.observations[evidence_id]
                    def delivery(self, conn, evidence_id, expected):
                        return lifecycle.VerifiedDelivery(
                            evidence_id, expected.review_id, expected.review_sha256,
                            'actor', 'chat',
                        )
                    def action(self, conn, evidence_id, expected):
                        return lifecycle.VerifiedAction(
                            evidence_id, expected.review_id, expected.review_sha256,
                            expected.delivery_evidence_id, 'actor', 'chat',
                        )
                port = Port()
                service = lifecycle.LedgerService(owner, port, binding, clock=lambda: 1792800000)
                port.bad_actor = True
                actor_refused = False
                try:
                    service.capture('source1', 'capture-bad-actor')
                except lifecycle.LifecycleError:
                    actor_refused = True
                port.bad_actor = False
                source = service.capture('source1', 'capture1')
                retry_same = service.capture('source1', 'capture1') == source
                proposal = service.propose(source.source_id, 'observation1', 0, 'propose1')
                review = service.prepare_review(proposal.proposal_id, proposal.version)
                service.record_delivery(review.review_id, 'delivery1')
                posted = service.confirm(review.review_id, 'action1', 'confirm1')
                audit = service.reader().full_audit()
                guarded_capture = {
                    'actor_refused': actor_refused,
                    'retry_same': retry_same,
                    'source_sha256': source.source_sha256,
                    'event_count': audit['event_count'],
                    'fact_version_count': audit['fact_version_count'],
                }
                snapshot = scratch / 'sealed.sqlite'
                cut = service.seal_snapshot(snapshot)
                new_root = scratch / 'successor'
                new_root.mkdir(mode=0o700)
                new_support = new_root / (
                    'Application Support' if layout == 'mac' else 'finance-codex'
                )
                new_support.mkdir(mode=0o700)
                new_product = product_root(new_support, layout)
                new_profile = profile_root(new_support, 'synthetic', layout)
                if new_product != new_support:
                    new_product.mkdir(mode=0o700)
                for directory in (
                    new_profile.parent, new_profile, new_profile / 'runtime',
                    new_profile / 'runtime/database', new_profile / 'workspace',
                    new_profile / 'workspace/database', new_profile / 'backups',
                    new_profile / 'work', new_profile / 'restore', new_root / 'control',
                ):
                    directory.mkdir(mode=0o700)
                new_manifest = new_profile / 'profile.json'
                new_manifest.write_text(json.dumps({
                    'profile_id': 'synthetic', 'runtime_root': str(new_profile / 'runtime'),
                    'workspace_root': str(new_profile / 'workspace'),
                }))
                new_manifest.chmod(0o600)
                new_runtime = new_profile / 'runtime'
                os.environ['FINANCE_RUNTIME_ROOT'] = str(new_runtime)
                new_config = admission.TrustedInstanceConfig(
                    new_root / 'control', new_support, 'synthetic', layout=layout,
                    instance_id='successor-instance',
                    config_identity='synthetic-config-v1', core_identity='synthetic-core-v1',
                    approval_verifier=lambda ref, action, owner_binding: ref == 'approved',
                )
                new_binding = lifecycle.RuntimeBinding(
                    new_config.instance_id, 'generation-2', 'actor', 'chat',
                )
                held = recovery.prepare_restored_profile(
                    new_config, snapshot, cut, new_binding=new_binding,
                    operation_id='restore-prepare', approval_ref='approved',
                )
                os.environ['FINANCE_RUNTIME_ROOT'] = str(profile / 'runtime')
                admission.fence_production_instance(
                    cfg, operation_id='old-fence', approval_ref='approved',
                )
                owner.close()
                owner = None
                authority = recovery.RecoveryAuthority(
                    'installed-synthetic-recovery', b'r' * 32,
                )
                isolation = authority.issue_old_writer_isolation(
                    cfg, held, operation_id='designate', approval_ref='approved',
                )
                cfg.close()
                support.rename(scratch / 'unavailable-old-support')
                old_control = scratch / 'control'
                old_control.rename(scratch / 'unavailable-old-control')
                old_root_unavailable = not support.exists() and not old_control.exists()
                os.environ['FINANCE_RUNTIME_ROOT'] = str(new_runtime)
                with patch.object(recovery, '_publish', side_effect=OSError(
                    'synthetic installed process loss before authority publication'
                )):
                    try:
                        recovery.promote_with_isolation_receipt(
                            held, isolation, authority, operation_id='restore-admit',
                            approval_ref='approved', port=port, binding=new_binding,
                        )
                    except OSError as error:
                        assert 'synthetic installed process loss' in str(error)
                    else:
                        raise AssertionError('Synthetic promotion interruption did not run')
                assert (new_config.control_root / admission.PENDING_FILENAME).is_file()
                retained = {
                    'config': new_config._binding, 'support_root': str(new_support),
                    'cut': asdict(cut), 'receipt': asdict(isolation),
                    'binding': asdict(new_binding), 'raw': raw, 'original_sha': original_sha,
                    'observations': {key: asdict(value)
                                     for key, value in port.observations.items()},
                    'posted': asdict(posted),
                }
                material_path = scratch / 'retained-recovery.json'
                material_path.write_text(json.dumps(retained))
                material_path.chmod(0o600)
                new_config.close()
                new_config = None
                fresh_proofs = []
                for mode in ('pending', 'completed'):
                    child = subprocess.run(
                        [sys.executable, '-I', '-S', '-c', restart_program,
                         str(installed), str(dependency_root), str(checkout),
                         str(material_path), mode],
                        cwd=outside,
                        env={'PATH': os.environ.get('PATH', ''),
                             'FINANCE_RUNTIME_ROOT': str(new_runtime)},
                        capture_output=True, text=True, timeout=180,
                    )
                    assert child.returncode == 0, child.stdout[-32768:] + child.stderr[-32768:]
                    fresh = json.loads(child.stdout)
                    assert fresh['pid'] != os.getpid() and fresh['installed_only']
                    fresh_proofs.append(fresh)
                    if mode == 'pending':
                        retained['completed_expectation'] = fresh
                        material_path.write_text(json.dumps(retained))
                assert fresh_proofs[0]['pid'] != fresh_proofs[1]['pid']
                new_config = admission.TrustedInstanceConfig(
                    new_root / 'control', new_support, 'synthetic', layout=layout,
                    instance_id='successor-instance',
                    config_identity='synthetic-config-v1', core_identity='synthetic-core-v1',
                    approval_verifier=lambda ref, action, owner_binding: ref == 'approved',
                )
                promoted = admission.inspect_instance_admission(new_config)
                new_owner = admission.acquire_production_owner(new_config)
                restored_service = lifecycle.LedgerService(new_owner, port, new_binding)
                restored_fact = restored_service.reader().get(posted.record_id)
                recovery_proof = {
                    'cut_format': cut.format,
                    'logical_digest_format': cut.logical_digest_format,
                    'logical_database_sha256': cut.logical_database_sha256,
                    'source_binding_sha256': cut.source_binding_sha256,
                    'snapshot_sha256': cut.database_sha256,
                    'source_binding_matches': cut.source_binding_sha256 == cfg.binding_sha256,
                    'snapshot_file_matches': (
                        cut.database_sha256 == hashlib.sha256(snapshot.read_bytes()).hexdigest()
                    ),
                    'cut_contains_posted_fact': cut.facts == ((
                        posted.record_id, posted.version, posted.fact_sha256,
                    ),),
                    'receipt_schema': json.loads(isolation.material_json)['schema'],
                    'held_status': held.status,
                    'new_instance_id': new_binding.instance_id,
                    'new_generation': new_binding.generation,
                    'old_root_unavailable': old_root_unavailable,
                    'promoted_state': promoted['state'],
                    'record_id': restored_fact['record_id'],
                    'record_version': restored_fact['version'],
                    'record_amount': restored_fact['values']['amount'],
                    'fact_sha256_matches': restored_fact['fact_sha256'] == posted.fact_sha256,
                    'fresh_process_retries': fresh_proofs,
                }
            finally:
                if new_owner is not None:
                    new_owner.close()
                if owner is not None:
                    owner.close()
                if new_config is not None:
                    new_config.close()
                cfg.close()
        module_paths = sorted({
            Path(module.__file__).resolve()
            for name, module in sys.modules.items()
            if (name == 'finance_core' or name.startswith('finance_core.'))
            and getattr(module, '__file__', None)
        } | {migration.resolve()})
        print(json.dumps({
            'root_api': finance_core.API_CONTRACT_VERSION,
            'lifecycle_exports': sorted(lifecycle.__all__),
            'refused_owner': refused_owner,
            'refused_runtime': refused_runtime,
            'migration_count': len(paths),
            'migration_digest': MIGRATION_LEDGER_DIGEST,
            'migration_name': migration.name,
            'migration_sha256': hashlib.sha256(migration.read_bytes()).hexdigest(),
            'lifecycle_tables': sorted(tables),
            'guarded_capture': guarded_capture,
            'recovery': recovery_proof,
            'module_paths': [str(path) for path in module_paths],
            'installed_only': all(path.is_relative_to(installed) for path in module_paths)
                and not any(Path(path or '.').resolve().is_relative_to(checkout)
                            for path in sys.path),
        }, sort_keys=True))
        """
    )
    with tempfile.TemporaryDirectory(prefix="installed-lifecycle-import-", dir=outside_dir) as root:
        dependency_root = _installed_runtime_dependencies(outside_dir)
        import_runtime = Path(root) / "runtime"
        import_runtime.mkdir(mode=0o700)
        (import_runtime / "database").mkdir(mode=0o700)
        command = [
            str(runtime_python),
            "-I",
            "-S",
            "-c",
            program,
            str(install_dir),
            str(REPOSITORY_ROOT),
            str(outside_dir),
            str(dependency_root),
            _installed_recovery_restart_program(),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                cwd=outside_dir,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "FINANCE_RUNTIME_ROOT": str(import_runtime.resolve(strict=True)),
                },
                capture_output=True,
                text=True,
                timeout=INSTALLED_INSTANCE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                f"installed lifecycle probe timeout after {INSTALLED_INSTANCE_TIMEOUT_SECONDS}s; "
                f"stdout={_bounded_instance_output(error.stdout)!r}; "
                f"stderr={_bounded_instance_output(error.stderr)!r}"
            ) from None
    if completed.returncode != 0:
        raise AssertionError(
            f"installed lifecycle probe exit {completed.returncode}; "
            f"stdout={completed.stdout[-INSTALLED_INSTANCE_STREAM_LIMIT:]!r}; "
            f"stderr={completed.stderr[-INSTALLED_INSTANCE_STREAM_LIMIT:]!r}"
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
    _installed_runtime_dependencies(outside_dir)
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


def _probe_installed_pdf(
    runtime_python: Path, install_dir: Path, outside_dir: Path, runtime_root: Path
) -> dict[str, object]:
    dependency_root = outside_dir / "independent-correction-fixtures" / "runtime-dependencies"
    # Copy the exact installed optional media distributions, including native closure.
    # This is a codec/import proof; Linux sandbox qualification stays in its required lane.
    import importlib.metadata

    dependency_site = Path(sysconfig.get_paths()["purelib"])
    for name in ("Pillow", "pillow-heif"):
        distribution = importlib.metadata.distribution(name)
        for member in distribution.files or ():
            if str(member).endswith((".pyc", ".pyo")):
                continue
            source_member = Path(distribution.locate_file(member)).resolve()
            relative = source_member.relative_to(dependency_site.resolve())
            destination = dependency_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_member, destination)
    source = outside_dir / (install_dir.name + "-symbol.pdf")
    script = textwrap.dedent(
        """
        import hashlib, json, sys
        from dataclasses import replace
        from importlib.metadata import version
        from pathlib import Path
        sys.path[:0] = [sys.argv[1], sys.argv[2]]
        import finance_core, pypdf
        import os, PIL, pillow_heif
        from PIL import Image
        from finance_core.intake import _receipt_media_worker as media_worker
        assert media_worker.identity()['packages'] == {'Pillow': '13.0.0', 'pillow-heif': '1.8.0'}
        original = Path(sys.argv[3] + '.png')
        output = Path(sys.argv[3] + '-normalized.png')
        Image.new('RGBA', (2, 1), (10, 20, 30, 0)).save(original)
        source_fd = os.open(original, os.O_RDONLY)
        output_fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            metadata = media_worker.normalize(source_fd, output_fd, {})
        finally:
            os.close(source_fd)
            os.close(output_fd)
        with Image.open(output) as decoded:
            assert decoded.mode == 'RGB' and decoded.getpixel((0, 0)) == (255, 255, 255)
        assert metadata['alpha_policy'] == 'white_matte'
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        from finance_core.reconciliation.pdf_statement_template_cli import parse_pdf_with_template
        from finance_core.reconciliation.pdf_statement_extractor import (
            DEFAULT_PDF_RESOURCE_LIMITS, extract_text_from_pdf)
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        fonts = DictionaryObject()
        for name, base in (('/F1', '/Courier'), ('/F2', '/Symbol')):
            fonts[NameObject(name)] = DictionaryObject({
                NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'),
                NameObject('/BaseFont'): NameObject(base)})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): fonts})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 10 Tf 72 740 Td (01/07/2026 ) Tj '
                        b'/F2 10 Tf (D) Tj /F1 10 Tf ( 12.50 D) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        source = Path(sys.argv[3])
        writer.write(source)
        result = parse_pdf_with_template(source, 'sample_bank_v1')
        assert result.extraction_success and result.total_rows == 1
        limited = extract_text_from_pdf(source, limits=replace(
            DEFAULT_PDF_RESOURCE_LIMITS, max_chars_per_page=5))
        print(json.dumps({
            'core_module': finance_core.__file__, 'pypdf_module': pypdf.__file__,
            'pillow_version': version('Pillow'), 'pillow_heif_version': version('pillow-heif'),
            'media_module': media_worker.__file__, 'pillow_module': PIL.__file__,
            'actual_small_codec_helper': True, 'linux_worker_qualification': False,
            'pypdf_version': version('pypdf'),
            'extraction_version': result.extraction.extraction_version,
            'row_extraction_version': result.rows[0].extraction_version,
            'raw_line': result.rows[0].raw_line, 'amount': str(result.rows[0].amount),
            'source_hash_matches': result.rows[0].source_content_hash ==
                hashlib.sha256(source.read_bytes()).hexdigest(),
            'limit_error': limited.error_code.value,
            'sys_path_has_checkout': any(Path(p or '.').resolve().is_relative_to(
                Path(sys.argv[4]).resolve()) for p in sys.path)
        }))
        """
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["FINANCE_RUNTIME_ROOT"] = str(runtime_root)
    completed = subprocess.run(
        [
            str(runtime_python),
            "-I",
            "-S",
            "-c",
            script,
            str(install_dir),
            str(dependency_root),
            str(source),
            str(REPOSITORY_ROOT),
        ],
        check=True,
        cwd=outside_dir,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    payload = json.loads(completed.stdout)
    assert payload["pillow_version"] == "13.0.0"
    assert payload["pillow_heif_version"] == "1.8.0"
    assert payload["actual_small_codec_helper"] and not payload["linux_worker_qualification"]
    assert Path(payload["media_module"]).resolve().is_relative_to(install_dir.resolve())
    assert Path(payload["pillow_module"]).resolve().is_relative_to(dependency_root.resolve())
    assert payload["pypdf_version"] == "6.21.2"
    assert payload["extraction_version"] == "pypdf-6.21.2-text-extraction-v4"
    assert payload["row_extraction_version"] == payload["extraction_version"]
    assert payload["raw_line"] == "01/07/2026 \u2206 12.50 D"
    assert payload["amount"] == "12.50" and payload["source_hash_matches"]
    assert payload["limit_error"] == "pdf_page_text_limit_exceeded"
    assert not payload["sys_path_has_checkout"]
    assert Path(payload["core_module"]).resolve().is_relative_to(install_dir.resolve())
    assert Path(payload["pypdf_module"]).resolve().is_relative_to(dependency_root.resolve())
    print("INSTALLED_PDF_PROOF " + json.dumps(payload, sort_keys=True))
    return payload


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
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_identity = archive.read("finance_core-0.1.7.dist-info/WHEEL")
        assert b"Generator: setuptools (84.1.0)\n" in wheel_identity

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
        lifecycle = _probe_installed_lifecycle(runtime_python, install_dir, outside_dir)
        assert lifecycle["root_api"] == "finance-core-api-v1"
        assert {"LedgerService", "LedgerReader", "RuntimeBinding", "VerifiedSource"}.issubset(
            lifecycle["lifecycle_exports"]
        )
        assert lifecycle["refused_owner"] and lifecycle["refused_runtime"]
        assert lifecycle["migration_count"] == 61
        assert lifecycle["migration_digest"] == EXPECTED_LEDGER_DIGEST
        assert lifecycle["migration_name"] == "061_lifecycle_v2.sql"
        assert lifecycle["guarded_capture"]["actor_refused"] is True
        assert lifecycle["guarded_capture"]["retry_same"] is True
        assert lifecycle["guarded_capture"]["event_count"] == 5
        assert lifecycle["guarded_capture"]["fact_version_count"] == 1
        recovery = lifecycle["recovery"]
        assert recovery["cut_format"] == "lifecycle-snapshot-cut-v3"
        assert recovery["logical_digest_format"] == "core-logical-database-v1"
        assert len(recovery["logical_database_sha256"]) == 64
        assert len(recovery["source_binding_sha256"]) == 64
        assert len(recovery["snapshot_sha256"]) == 64
        assert recovery["source_binding_matches"] is True
        assert recovery["snapshot_file_matches"] is True
        assert recovery["cut_contains_posted_fact"] is True
        assert recovery["receipt_schema"] == "old-writer-isolation-v3"
        assert recovery["held_status"] == "RESTORE_HELD"
        assert recovery["new_instance_id"] == "successor-instance"
        assert recovery["new_generation"] == "generation-2"
        assert recovery["old_root_unavailable"] is True
        assert recovery["promoted_state"] == "ACTIVE"
        assert recovery["record_id"]
        assert recovery["record_version"] == 1
        assert recovery["record_amount"] == "12.00"
        assert recovery["fact_sha256_matches"] is True
        pending_retry, completed_retry = recovery["fresh_process_retries"]
        assert pending_retry["observed_status"] == "RESTORE_ADMIT_PENDING"
        assert completed_retry["observed_status"] == "ACTIVE"
        assert pending_retry["mode"] == "pending" and completed_retry["mode"] == "completed"
        assert pending_retry["pid"] != completed_retry["pid"]
        assert pending_retry["installed_only"] and completed_retry["installed_only"]
        assert pending_retry["pending_retained_on_refusal"] is True
        assert set(pending_retry["refused"]) == {
            "owner_while_pending",
            "generic_resume",
            "wrong_operation",
            "wrong_cut",
            "wrong_receipt",
            "changed_content",
        }
        for key in (
            "authority_sha256",
            "authority_version",
            "audit",
            "post_promotion_source_id",
            "record_id",
            "record_version",
            "fact_sha256",
            "prepare_entry_sha256",
        ):
            assert pending_retry[key] == completed_retry[key]
        assert completed_retry["audit"]["event_count"] == 6
        assert completed_retry["audit"]["fact_version_count"] == 1
        assert {"l2_sources", "l2_facts", "l2_events", "l2_imports"}.issubset(
            lifecycle["lifecycle_tables"]
        )
        assert lifecycle["installed_only"] is True
        print(
            "INSTALLED_LIFECYCLE_PROOF "
            + json.dumps(
                {
                    "artifact": install_dir.name,
                    "artifact_sha256": hashlib.sha256(
                        (
                            wheels[0]
                            if install_dir == wheel_install_dir
                            else source_distributions[0]
                        ).read_bytes()
                    ).hexdigest(),
                    **lifecycle,
                },
                sort_keys=True,
            )
        )
        assert "pypdf==6.21.2" in payload["runtime_dependencies"]
        assert 'Pillow==13.0.0; extra == "media"' in payload["runtime_dependencies"]
        assert 'pillow-heif==1.8.0; extra == "media"' in payload["runtime_dependencies"]
        correction_flow = _probe_installed_correction_flow(
            runtime_python, install_dir, outside_dir, runtime_root
        )
        _probe_installed_pdf(runtime_python, install_dir, outside_dir, runtime_root)
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
        assert payload["distribution_version"] == "0.1.7"
        assert payload["money_module"] == "finance_core.money"
        assert payload["migration_count"] == 61
        assert [int(name[:3]) for name in payload["migration_filenames"]] == list(range(1, 62))
        assert payload["migration_filenames"][-1] == "061_lifecycle_v2.sql"
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

    distribution_prefix = "finance_core-0.1.7.dist-info"
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
        assert {path.parts[0] for path in member_paths} == {"finance_core-0.1.7"}
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


def _candidate_package_producer():
    path = REPOSITORY_ROOT / ".github/scripts/build_candidate_package.py"
    spec = importlib.util.spec_from_file_location("candidate_package_producer_test", path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _candidate_package_repository(tmp_path, *, complete_core=False):
    producer = _candidate_package_producer()
    root = tmp_path / "tracked-source"
    root.mkdir()
    paths = producer.PROJECT_INPUTS | {
        "requirements-dev.txt",
        ".github/scripts/build_candidate_package.py",
    }
    if complete_core:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z", "--", "finance_core"], cwd=REPOSITORY_ROOT
        )
        paths |= {name.decode() for name in tracked.split(b"\0") if name}
    for name in paths:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / name, target)
        target.chmod((REPOSITORY_ROOT / name).stat().st_mode & 0o777)
    if not complete_core:
        for name, body in {
            "finance_core/__init__.py": b'"""Synthetic package only."""\n',
            "finance_core/resources/__init__.py": b"",
            "finance_core/resources/example.json": b"{}\n",
            "finance_core/resources/migrations/001_fixture.sql": b"-- synthetic\n",
        }.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Candidate Package Test",
            "-c",
            "user.email=package@example.invalid",
            "commit",
            "-qm",
            "synthetic tracked fixture",
        ],
        cwd=root,
        check=True,
    )
    identity = {
        "head_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "tested_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "tested_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
        ).strip(),
    }
    return producer, root, identity


@pytest.mark.parametrize("change", ["dirty", "untracked", "wrong-head", "symlink", "export-ignore"])
def test_candidate_package_snapshot_refuses_source_drift(tmp_path, change):
    producer, root, identity = _candidate_package_repository(tmp_path)
    if change == "dirty":
        (root / "finance_core/__init__.py").write_bytes(b"unreviewed\n")
    elif change == "untracked":
        (root / "finance_core/untracked.py").write_bytes(b"untracked\n")
    elif change == "wrong-head":
        identity["head_sha"] = "a" * 40
    elif change == "symlink":
        (root / "finance_core/link.py").symlink_to("__init__.py")
    else:
        (root / ".gitattributes").write_text("finance_core/__init__.py export-ignore\n")
    if change in {"symlink", "export-ignore"}:
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Package Test",
                "-c",
                "user.email=package@example.invalid",
                "commit",
                "-qm",
                "unsafe synthetic fixture",
            ],
            cwd=root,
            check=True,
        )
        identity["head_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        identity["tested_tree"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
        ).strip()
    with pytest.raises(ValueError):
        producer.tracked_snapshot(root, identity)


def _candidate_package_archives(tmp_path, payloads, *, kind=None, mutation=None):
    producer = _candidate_package_producer()
    core = producer.package_payloads(payloads)
    wheel = tmp_path / "finance_core-0.1.7-py3-none-any.whl"
    sdist = tmp_path / "finance_core-0.1.7.tar.gz"
    wheel_members = {
        **core,
        "finance_core-0.1.7.dist-info/METADATA": producer.sdist_metadata(payloads, "0.1.7")[
            "PKG-INFO"
        ],
        "finance_core-0.1.7.dist-info/RECORD": b"",
        "finance_core-0.1.7.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    }
    sdist_members = {
        **core,
        **{name: payloads[name] for name in producer.PROJECT_INPUTS},
        **producer.sdist_metadata(payloads, "0.1.7"),
    }
    source_paths = (
        set(core)
        | producer.PROJECT_INPUTS
        | {name for name in sdist_members if name.startswith("finance_core.egg-info/")}
        | {"finance_core.egg-info/SOURCES.txt"}
    )
    sdist_members["finance_core.egg-info/SOURCES.txt"] = (
        "\n".join(sorted(source_paths)) + "\n"
    ).encode()
    selected = wheel_members if kind == "wheel" else sdist_members
    first = "finance_core/__init__.py"
    if mutation == "missing":
        del selected[first]
    elif mutation == "changed":
        selected[first] = b"changed\n"
    elif mutation == "extra":
        selected["finance_core/extra.py"] = b"extra\n"
    elif mutation == "unsafe":
        selected["../escape.py"] = b"unsafe\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, body in wheel_members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (
                0o120777
                if kind == "wheel" and mutation == "symlink" and name == first
                else 0o100644
            ) << 16
            archive.writestr(info, body)
        if kind == "wheel" and mutation == "duplicate":
            archive.writestr(first, core[first])
    with tarfile.open(sdist, "w:gz") as archive:
        for name, body in sdist_members.items():
            member = tarfile.TarInfo("finance_core-0.1.7/" + name)
            member.size = len(body)
            if kind == "sdist" and mutation == "symlink" and name == first:
                member.type = tarfile.SYMTYPE
                member.linkname = "/outside"
            archive.addfile(member, io.BytesIO(body))
        if kind == "sdist" and mutation == "duplicate":
            member = tarfile.TarInfo("finance_core-0.1.7/" + first)
            member.size = len(core[first])
            archive.addfile(member, io.BytesIO(core[first]))
    return wheel, sdist


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "mutation", ["missing", "changed", "extra", "unsafe", "symlink", "duplicate"]
)
def test_candidate_package_archives_reject_incomplete_or_unsafe_bytes(tmp_path, kind, mutation):
    producer, root, identity = _candidate_package_repository(tmp_path)
    payloads, _ = producer.tracked_snapshot(root, identity)
    wheel, sdist = _candidate_package_archives(tmp_path, payloads, kind=kind, mutation=mutation)
    with pytest.raises(ValueError):
        producer.verify_distributions(wheel, sdist, payloads, "0.1.7")


def test_candidate_package_real_locked_build_and_outside_import(tmp_path):
    producer, root, identity = _candidate_package_repository(tmp_path, complete_core=True)
    output = tmp_path / "candidate-artifact"
    before = subprocess.check_output(["git", "status", "--porcelain"], cwd=root)
    record = producer.build(root, output, identity)
    assert record["status"] == "BUILT_NOT_VALIDATED"
    assert record["identity"] == identity
    assert record["recipe"]["tools"] == {"build": "1.6.1", "setuptools": "84.1.0"}
    inventory_bytes = (output / "source-inventory.json").read_bytes()
    assert hashlib.sha256(inventory_bytes).hexdigest() == record["source_inventory_sha256"]
    files = json.loads(inventory_bytes)["files"]
    assert (
        len(files)
        == len(subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")) - 1
    )
    assert set(record["artifacts"]) == {
        "finance_core-0.1.7-py3-none-any.whl",
        "finance_core-0.1.7.tar.gz",
    }
    for name, artifact in record["artifacts"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == artifact["sha256"]
        assert (output / name).stat().st_size == artifact["size_bytes"]
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root) == before == b""
    installed = tmp_path / "outside-installed"
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(installed),
            str(output / "finance_core-0.1.7-py3-none-any.whl"),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    script = (
        "import sys,pathlib;sys.path.insert(0,sys.argv[1]);"
        "import finance_core.lifecycle_v2 as api;"
        "assert api.LedgerService;"
        "assert pathlib.Path(api.__file__).resolve().is_relative_to("
        "pathlib.Path(sys.argv[1]).resolve())"
    )
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(installed)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_candidate_package_complete_archive_pair_passes(tmp_path):
    producer, root, identity = _candidate_package_repository(tmp_path)
    payloads, _ = producer.tracked_snapshot(root, identity)
    wheel, sdist = _candidate_package_archives(tmp_path, payloads)
    producer.verify_distributions(wheel, sdist, payloads, "0.1.7")


@pytest.mark.parametrize(
    "mutation",
    [
        "setup.py",
        "arbitrary.txt",
        "bin/launcher",
        "finance_core.egg-info/entry_points.txt",
        "changed-config",
        "changed-requires",
        "changed-pkg-info",
        "changed-top-level",
        "changed-links",
        "sources-extra",
        "sources-missing",
        "sources-duplicate",
        "sources-traversal",
        "executable-config",
        "missing-metadata",
        "unknown-directory",
        "duplicate-directory",
    ],
)
def test_candidate_package_sdist_rejects_undeclared_or_changed_metadata(tmp_path, mutation):
    producer, root, identity = _candidate_package_repository(tmp_path)
    payloads, _ = producer.tracked_snapshot(root, identity)
    wheel, sdist = _candidate_package_archives(tmp_path, payloads)
    members = []
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive:
            members.append((member, archive.extractfile(member).read()))
    prefix = "finance_core-0.1.7/"
    sources = "finance_core.egg-info/SOURCES.txt"
    target = {
        "changed-config": "setup.cfg",
        "executable-config": "setup.cfg",
        "changed-requires": "finance_core.egg-info/requires.txt",
        "changed-pkg-info": "PKG-INFO",
        "changed-top-level": "finance_core.egg-info/top_level.txt",
        "changed-links": "finance_core.egg-info/dependency_links.txt",
        "missing-metadata": "finance_core.egg-info/top_level.txt",
        **{
            name: sources
            for name in (
                "sources-extra",
                "sources-missing",
                "sources-duplicate",
                "sources-traversal",
            )
        },
    }.get(mutation)
    revised = []
    for member, body in members:
        if member.name == prefix + str(target):
            if mutation == "missing-metadata":
                continue
            if mutation == "executable-config":
                member.mode = 0o755
            elif mutation == "sources-extra":
                body += b"setup.py\n"
            elif mutation == "sources-missing":
                body = body.split(b"\n", 1)[1]
            elif mutation == "sources-duplicate":
                body += body.split(b"\n", 1)[0] + b"\n"
            elif mutation == "sources-traversal":
                body += b"../outside.py\n"
            else:
                body = b"undeclared generated metadata\n"
            member.size = len(body)
        revised.append((member, body))
    if target is None:
        if mutation in {"unknown-directory", "duplicate-directory"}:
            member = tarfile.TarInfo(
                prefix + ("unexpected" if mutation == "unknown-directory" else "finance_core")
            )
            member.type = tarfile.DIRTYPE
            revised.append((member, b""))
            if mutation == "duplicate-directory":
                revised.append((member, b""))
        else:
            member = tarfile.TarInfo(prefix + mutation)
            body = b"raise RuntimeError('undeclared root hook')\n"
            member.size = len(body)
            member.mode = 0o755 if mutation in {"setup.py", "bin/launcher"} else 0o644
            revised.append((member, body))
    altered = tmp_path / "altered-sdist.tar.gz"
    with tarfile.open(altered, "w:gz") as archive:
        for member, body in revised:
            archive.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError):
        producer.verify_distributions(wheel, altered, payloads, "0.1.7")


def test_candidate_package_metadata_cannot_collectively_override_source_dependencies(tmp_path):
    producer, root, identity = _candidate_package_repository(tmp_path)
    payloads, _ = producer.tracked_snapshot(root, identity)
    wheel, sdist = _candidate_package_archives(tmp_path, payloads)
    altered_wheel = tmp_path / "altered-wheel.whl"
    with zipfile.ZipFile(wheel) as original, zipfile.ZipFile(altered_wheel, "w") as altered:
        for member in original.infolist():
            body = original.read(member)
            if member.filename.endswith("/METADATA"):
                body = body.replace(b"pypdf==6.21.2", b"pypdf==0.0.1")
            altered.writestr(member, body)
    altered_sdist = tmp_path / "altered-sdist.tar.gz"
    with tarfile.open(sdist, "r:gz") as original, tarfile.open(altered_sdist, "w:gz") as altered:
        for member in original:
            body = original.extractfile(member).read()
            if member.name.endswith("/PKG-INFO"):
                body = body.replace(b"pypdf==6.21.2", b"pypdf==0.0.1")
            member.size = len(body)
            altered.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="metadata differs from source recipe"):
        producer.verify_distributions(altered_wheel, altered_sdist, payloads, "0.1.7")
