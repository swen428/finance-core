"""Architecture guard mutations, including package and lazy-import bypass paths."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "application_dependency_guard", ROOT / "scripts/check_application_dependencies.py"
)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def write(root: Path, name: str, content: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scaffold(root: Path) -> dict:
    write(root, "finance_core/__init__.py")
    write(root, "finance_core/application/__init__.py")
    write(root, "finance_core/application/review.py", "from finance_core import helper\n")
    write(root, "finance_core/helper.py")
    write(root, "finance_core/telegram_source_context.py")
    return {"schema_version": 1, "direct": [], "indirect": []}


def test_existing_exceptions_are_exact_and_application_has_no_platform_path() -> None:
    registry = json.loads(
        (ROOT / "docs/development/platform_dependency_exceptions_v1.json").read_text()
    )
    assert GUARD.check_boundaries(ROOT, registry) == []
    assert registry["direct"] and registry["indirect"]
    assert not any("*" in json.dumps(edge) for edge in registry["direct"])


def test_correction_adapter_package_is_a_platform_boundary(tmp_path: Path) -> None:
    registry = scaffold(tmp_path)
    write(tmp_path, "finance_core/correction_adapters/__init__.py")
    write(tmp_path, "finance_core/correction_adapters/local_authority.py")
    write(
        tmp_path,
        "finance_core/application/review.py",
        "from finance_core.correction_adapters.local_authority import LocalApprovalAuthority\n",
    )
    assert any(
        "application reaches platform" in error
        for error in GUARD.check_boundaries(tmp_path, registry)
    )


def test_cold_correction_application_import_does_not_load_platform() -> None:
    script = (
        "import sys\n"
        "import finance_core.application.corrections\n"
        "import finance_core.application.correction_receipts\n"
        "for name in sys.modules:\n"
        "    assert not name.startswith('finance_core.correction_adapters')\n"
        "    assert not name.startswith('finance_core.openclaw_staging_bridge')\n"
        "    assert name != 'finance_core.telegram_source_context'\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "path,source",
    [
        ("finance_core/application/review.py", "import finance_core.telegram_source_context\n"),
        ("finance_core/helper.py", "from finance_core.telegram_source_context import Context\n"),
        ("finance_core/__init__.py", "from . import telegram_source_context\n"),
        (
            "finance_core/helper.py",
            "from importlib import import_module as load\n"
            "load('finance_core.telegram_source_context')\n",
        ),
        (
            "finance_core/application/review.py",
            "from importlib import import_module\n"
            "import_module('.telegram_source_context', 'finance_core')\n",
        ),
        (
            "finance_core/application/review.py",
            "import importlib\n"
            "importlib.import_module(name='..telegram_source_context', package=__package__)\n",
        ),
        (
            "finance_core/application/review.py",
            "from builtins import __import__ as load\n"
            "load('finance_core.telegram_source_context')\n",
        ),
    ],
)
def test_direct_indirect_initializer_and_literal_dynamic_imports_are_rejected(
    tmp_path: Path, path: str, source: str
) -> None:
    registry = scaffold(tmp_path)
    write(tmp_path, path, source)
    errors = GUARD.check_boundaries(tmp_path, registry)
    assert any("application reaches platform" in error for error in errors)
    # Even copying the newly observed dependency set cannot waive Application.
    inventory = GUARD.dependency_inventory(tmp_path)
    registry.update({key: inventory[key] for key in ("direct", "indirect")})
    assert any(
        "application reaches platform" in error
        for error in GUARD.check_boundaries(tmp_path, registry)
    )


def test_new_generic_caller_cannot_inherit_an_old_platform_exception(tmp_path: Path) -> None:
    scaffold(tmp_path)
    write(tmp_path, "finance_core/legacy.py", "import finance_core.telegram_source_context\n")
    old = GUARD.dependency_inventory(tmp_path)
    registry = {"schema_version": 1, "direct": old["direct"], "indirect": old["indirect"]}
    assert GUARD.check_boundaries(tmp_path, registry) == []
    write(tmp_path, "finance_core/new_business.py", "from finance_core import legacy\n")
    assert any(
        "historical indirect" in error for error in GUARD.check_boundaries(tmp_path, registry)
    )


def test_historical_symbols_cannot_expand_and_removed_exceptions_are_stale(tmp_path: Path) -> None:
    scaffold(tmp_path)
    write(
        tmp_path,
        "finance_core/legacy.py",
        "from finance_core.telegram_source_context import Context\n",
    )
    old = GUARD.dependency_inventory(tmp_path)
    registry = {"schema_version": 1, "direct": old["direct"], "indirect": old["indirect"]}
    write(
        tmp_path,
        "finance_core/legacy.py",
        "from finance_core.telegram_source_context import Context, new_write\n",
    )
    assert any("historical direct" in error for error in GUARD.check_boundaries(tmp_path, registry))
    write(tmp_path, "finance_core/legacy.py")
    assert any("historical direct" in error for error in GUARD.check_boundaries(tmp_path, registry))


@pytest.mark.parametrize(
    "source",
    [
        "from finance_core.intake import Client\n",
        "import finance_core.intake as intake\nclient = intake.Client\n",
        "import finance_core.intake\nclient = finance_core.intake.Client\n",
        "from builtins import __import__ as load\n"
        "load('finance_core.intake', fromlist=['Client'])\n",
        "from importlib import import_module\nimport_module('finance_core.intake').Client\n",
        "from finance_core import intake\nother = intake\nother.Client\n",
    ],
)
def test_explicit_lazy_platform_export_is_followed(tmp_path: Path, source: str) -> None:
    scaffold(tmp_path)
    write(tmp_path, "finance_core/intake/telegram_client.py")
    write(tmp_path, "finance_core/intake/neutral.py")
    write(
        tmp_path,
        "finance_core/intake/__init__.py",
        "_LAZY_EXPORTS = {'Client': ('finance_core.intake.telegram_client', 'Client')}\n",
    )
    old = GUARD.dependency_inventory(tmp_path)
    registry = {"schema_version": 1, "direct": old["direct"], "indirect": old["indirect"]}
    write(tmp_path, "finance_core/helper.py", "from finance_core.intake import neutral\n")
    assert GUARD.check_boundaries(tmp_path, registry) == []
    write(tmp_path, "finance_core/helper.py", source)
    assert any(
        "application reaches platform" in error
        for error in GUARD.check_boundaries(tmp_path, registry)
    )


@pytest.mark.parametrize(
    "call",
    [
        "import_module(name)",
        "import_module('.telegram_source_context', package=name)",
        "import_module('.telegram_source_context', **options)",
        "__import__('telegram_source_context', globals(), level=1)",
        "__import__('finance_core.intake', fromlist=names)",
    ],
)
def test_computed_imports_fail_closed(tmp_path: Path, call: str) -> None:
    registry = scaffold(tmp_path)
    write(
        tmp_path,
        "finance_core/helper.py",
        "from importlib import import_module\nname = 'anything'\n" + call + "\n",
    )
    assert any("unresolved" in error for error in GUARD.check_boundaries(tmp_path, registry))


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nload = importlib.import_module\n"
        "load('finance_core.telegram_source_context')\n",
        "load = __import__\nload('finance_core.telegram_source_context')\n",
        "from importlib import import_module\nconsume(import_module)\n",
        "import builtins\nconsume(builtins.__import__)\n",
        "from importlib import import_module\ndef factory():\n    return import_module\n",
    ],
)
def test_importer_values_cannot_escape_analyzed_calls(tmp_path: Path, source: str) -> None:
    registry = scaffold(tmp_path)
    write(tmp_path, "finance_core/application/review.py", source)
    assert any("escaped importer" in error for error in GUARD.check_boundaries(tmp_path, registry))


@pytest.mark.parametrize("package", ["intake", "parser_proposals"])
@pytest.mark.parametrize("change", ["extra_helper", "changed_loader"])
def test_computed_loader_exception_is_bound_to_exact_function(
    tmp_path: Path, package: str, change: str
) -> None:
    scaffold(tmp_path)
    write(tmp_path, "finance_core/intake/telegram_client.py")
    loader = (ROOT / "finance_core/intake/__init__.py").read_text().split("def __getattr__", 1)[1]
    loader = "def __getattr__" + loader.split("\n\ndef __dir__", 1)[0]
    source = (
        "from importlib import import_module\n"
        "_LAZY_EXPORTS = {'Client': ('finance_core.intake.telegram_client', 'Client')}\n" + loader
    )
    path = f"finance_core/{package}/__init__.py"
    write(tmp_path, path, source)
    old = GUARD.dependency_inventory(tmp_path)
    registry = {"schema_version": 1, "direct": old["direct"], "indirect": old["indirect"]}
    assert GUARD.check_boundaries(tmp_path, registry) == []
    if change == "extra_helper":
        source += "\ndef load_any(module_name):\n    return import_module(module_name)\n"
        write(
            tmp_path,
            "finance_core/application/review.py",
            f"from finance_core.{package} import load_any\n"
            "load_any('finance_core.telegram_source_context')\n",
        )
    else:
        source = source.replace(
            "module_name, attribute = target",
            "module_name, attribute = target\n    module_name = 'arbitrary'",
        )
    write(tmp_path, path, source)
    assert any(
        "unresolved dynamic import" in error for error in GUARD.check_boundaries(tmp_path, registry)
    )


@pytest.mark.parametrize("module", ["importlib", "builtins"])
@pytest.mark.parametrize(
    "escape", ["other = {module}", "consume({module})", "def value():\n    return {module}"]
)
def test_importer_module_values_cannot_escape(tmp_path: Path, module: str, escape: str) -> None:
    registry = scaffold(tmp_path)
    write(
        tmp_path,
        "finance_core/application/review.py",
        f"import {module}\n" + escape.format(module=module),
    )
    assert any(
        "escaped importer module" in error for error in GUARD.check_boundaries(tmp_path, registry)
    )


@pytest.mark.parametrize("package", ["intake", "parser_proposals"])
@pytest.mark.parametrize(
    "mutation",
    [
        "_LAZY_EXPORTS['Client'] = ('finance_core.intake.telegram_client', 'Client')",
        "_LAZY_EXPORTS.update({'Client': ('finance_core.intake.telegram_client', 'Client')})",
        "other = _LAZY_EXPORTS",
        "consume(_LAZY_EXPORTS)",
        "def value():\n    return _LAZY_EXPORTS",
        "_LAZY_EXPORTS = {}",
    ],
)
def test_literal_lazy_table_cannot_be_mutated_or_escape(
    tmp_path: Path, package: str, mutation: str
) -> None:
    registry = scaffold(tmp_path)
    write(tmp_path, "finance_core/intake/neutral.py")
    write(tmp_path, "finance_core/intake/telegram_client.py")
    real = (ROOT / f"finance_core/{package}/__init__.py").read_text()
    loader = "def __getattr__" + real.split("def __getattr__", 1)[1]
    source = (
        "from importlib import import_module\nfrom typing import Any\n"
        "_LAZY_EXPORTS = {'Neutral': ('finance_core.intake.neutral', 'Neutral')}\n" + loader
    )
    path = f"finance_core/{package}/__init__.py"
    write(tmp_path, path, source)
    assert GUARD.check_boundaries(tmp_path, registry) == []
    write(tmp_path, path, source + "\n" + mutation + "\n")
    write(
        tmp_path,
        "finance_core/application/review.py",
        f"from finance_core.{package} import Client\n",
    )
    assert GUARD.check_boundaries(tmp_path, registry)


@pytest.mark.parametrize(
    "source",
    [
        "from finance_core.intake import _LAZY_EXPORTS as table\n",
        "import finance_core.intake as package\ntable = package._LAZY_EXPORTS\n",
        "import finance_core.intake as package\nother = package\ntable = other._LAZY_EXPORTS\n",
    ],
)
def test_external_lazy_table_access_is_rejected(tmp_path: Path, source: str) -> None:
    registry = scaffold(tmp_path)
    write(tmp_path, "finance_core/intake/__init__.py", "_LAZY_EXPORTS = {}\n")
    write(tmp_path, "finance_core/application/review.py", source)
    assert any(
        "external lazy table access" in error
        for error in GUARD.check_boundaries(tmp_path, registry)
    )


@pytest.mark.parametrize(
    "importer,call",
    [
        (
            "from importlib import import_module as load",
            "load('finance_core.telegram_source_context')",
        ),
        ("from builtins import __import__ as load", "load('finance_core.telegram_source_context')"),
        ("import importlib as load", "load.import_module('finance_core.telegram_source_context')"),
        ("import builtins as load", "load.__import__('finance_core.telegram_source_context')"),
    ],
)
@pytest.mark.parametrize("global_importer", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_importer_bindings_cannot_hide_cross_scope_calls(
    tmp_path: Path, importer: str, call: str, global_importer: bool, reverse: bool
) -> None:
    registry = scaffold(tmp_path)
    controlled = importer + "\n" + call + "\n"
    if not global_importer:
        controlled = "def controlled():\n" + "".join(
            "    " + line + "\n" for line in controlled.splitlines()
        )
    unrelated = "def unrelated():\n    from finance_core import helper as load\n    return load\n"
    source = unrelated + controlled if reverse else controlled + unrelated
    write(tmp_path, "finance_core/application/review.py", source)
    assert any(
        "conflicting importer binding" in error
        for error in GUARD.check_boundaries(tmp_path, registry)
    )


def test_local_import_cannot_hide_implicit_builtin_importer(tmp_path: Path) -> None:
    registry = scaffold(tmp_path)
    write(
        tmp_path,
        "finance_core/application/review.py",
        "__import__('finance_core.telegram_source_context')\n"
        "def unrelated():\n    from finance_core import helper as __import__\n"
        "    return __import__\n",
    )
    assert any(
        "conflicting importer binding" in error
        for error in GUARD.check_boundaries(tmp_path, registry)
    )
