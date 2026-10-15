"""Actual production-profile business proof using only installed Core bytes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from time import monotonic


def _milestone(stage: str, started: float) -> None:
    print(
        f"installed-instance {stage} elapsed={monotonic() - started:.3f}s",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    started = monotonic()
    _milestone("imports-start", started)
    install_dir, helper_root, outside_root, runtime_root, source_root = map(Path, sys.argv[1:6])
    os.environ["FINANCE_RUNTIME_ROOT"] = str(runtime_root)
    sys.path.insert(0, str(helper_root))
    sys.path.insert(0, str(helper_root / "tests"))
    sys.path.insert(0, str(helper_root / "runtime-dependencies"))
    sys.path.insert(0, str(install_dir))

    import finance_core

    # Existing signed fixtures use both qualified and historical unqualified
    # test imports. Keep one synthetic authority module in this isolated run.
    from tests import test_application_posting_recovery_v1 as posting_fixtures
    from tests import test_independent_corrections_v1 as correction_fixtures

    sys.modules["test_application_posting_recovery_v1"] = posting_fixtures
    sys.modules["test_independent_corrections_v1"] = correction_fixtures
    from tests.instance_admission_business_support_v1 import run_installed_business_probe

    _milestone("imports-end", started)
    _milestone("isolation-start", started)
    installed = install_dir.resolve()
    assert Path(finance_core.__file__).resolve().is_relative_to(installed)
    assert not any(Path(item).resolve() == source_root.resolve() for item in sys.path if item)
    _milestone("isolation-end", started)
    data_root = outside_root / "installed-instance-data" / install_dir.name
    data_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    _milestone("business-start", started)
    business = run_installed_business_probe(data_root)
    _milestone("business-end", started)
    _milestone("final-modules-start", started)
    module_files = {
        name: str(Path(module.__file__).resolve())
        for name, module in sys.modules.items()
        if (name == "finance_core" or name.startswith("finance_core."))
        and getattr(module, "__file__", None) is not None
    }
    assert module_files
    assert all(Path(path).is_relative_to(installed) for path in module_files.values())
    _milestone("final-modules-end", started)
    _milestone("json-output-start", started)
    print(
        json.dumps(
            {"business": business, "module_files": module_files, "sys_path_has_checkout": False},
            sort_keys=True,
        ),
        flush=True,
    )
    _milestone("json-output-end", started)
    _milestone("main-return", started)


if __name__ == "__main__":
    main()
