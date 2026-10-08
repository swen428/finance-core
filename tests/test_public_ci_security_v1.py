from __future__ import annotations

import ast
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "validate.yml"
FULL_SHA_ACTION = re.compile(r"^\s*uses:\s*[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$", re.MULTILINE)


def test_public_workflow_has_read_only_top_level_permissions() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request_target" not in source
    assert re.search(r"^permissions:\n  contents: read$", source, re.MULTILINE)
    assert "contents: write" not in source
    assert "pull-requests: write" not in source
    assert "id-token: write" not in source


def test_all_actions_are_pinned_and_checkout_does_not_persist_credentials() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    uses_lines = [line for line in source.splitlines() if line.lstrip().startswith("uses:")]

    assert uses_lines
    assert all(FULL_SHA_ACTION.fullmatch(line) for line in uses_lines)
    assert source.count("persist-credentials: false") == source.count("uses: actions/checkout@")


def test_bridge_lane_is_conditional_and_uses_exact_runtime() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "needs.classify.outputs.bridge == 'true'" in source
    assert 'node-version: "24.15.0"' in source
    assert "matrix:\n        os: [ubuntu-latest, macos-15]" in source
    assert "python-version: \"${{ runner.os == 'Linux' && '3.12.14' || '3.12.10' }}\"" in source
    assert "PYTHON_EXECUTABLE: ${{ github.workspace }}/.venv/bin/python" in source
    assert "Require exact reviewed Bridge build output" in source
    assert (
        "REVIEWED_CHECKOUT_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in source
    )
    assert 'git diff --exit-code "$REVIEWED_CHECKOUT_SHA" --' in source
    assert "git ls-files --others --exclude-standard --" in source
    assert "plugins/finance-bridge/dist/src" in source
    assert "plugins/finance-bridge/dist/build-provenance-v1.json" in source


def test_candidate_sha_diff_detects_staged_bridge_drift(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    compiled = repository / "plugins/finance-bridge/dist/src/index.js"
    compiled.parent.mkdir(parents=True)
    compiled.write_text("export const reviewed = true;\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Finance Test",
            "-c",
            "user.email=finance-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )
    reviewed_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    compiled.write_text("export const staged = true;\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(compiled.relative_to(repository))],
        cwd=repository,
        check=True,
    )

    completed = subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            reviewed_sha,
            "--",
            "plugins/finance-bridge/dist/src",
        ],
        cwd=repository,
        capture_output=True,
    )

    assert completed.returncode == 1


def test_validate_aggregates_required_and_optional_jobs() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "needs: [classify, quality, pytest, bridge, pytest-report]" in source
    assert 'test "$PYTEST_RESULT" = success' in source
    assert 'test "$QUALITY_RESULT" = success' in source
    assert 'test "$REPORT_RESULT" = success' in source
    assert "REPORT_RESULT: ${{ needs.pytest-report.result }}" in source
    assert "BRIDGE_SCOPE: ${{ needs.classify.outputs.bridge }}" in source
    assert "EVENT_NAME: ${{ github.event_name }}" in source.split("\n  validate:", 1)[1]
    report = source.split("\n  pytest-report:\n", 1)[1].split("\n  validate:", 1)[0]
    assert "    continue-on-error: true" not in report.split("    steps:", 1)[0]


def _step_script(name: str) -> str:
    section = WORKFLOW.read_text(encoding="utf-8").split(f"- name: {name}\n", 1)[1]
    match = re.search(r"^        run: \|\n((?:          .*\n|[ \t]*\n)+)", section, re.MULTILINE)
    assert match is not None
    return textwrap.dedent(match.group(1))


def _inline_python(step: str, label: str) -> str:
    script = _step_script(step)
    opening = f"<<'{label}'\n"
    assert opening in script
    body = script.split(opening, 1)[1]
    return body.split(f"\n{label}\n", 1)[0]


def _inline_namespace(step: str, label: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": f"workflow_{label.lower()}_test"}
    script = _inline_python(step, label)
    exec(compile(script, f"<{label}>", "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    ("event", "path", "expected"),
    [
        ("push", "finance_core/money.py", "true"),
        ("push", "README.md", "true"),
        ("push", "finance_core/resources/migrations/049_example.sql", "true"),
        ("workflow_dispatch", "README.md", "true"),
        ("pull_request", "README.md", "false"),
        ("pull_request", "finance_core/money.py", "false"),
        ("pull_request", "finance_core/application/review.py", "true"),
        ("pull_request", "finance_core/intake/__init__.py", "true"),
        ("pull_request", "finance_core/intake/receipt_ocr_evidence.py", "true"),
        ("pull_request", "finance_core/intake/tesseract_resources.py", "true"),
        ("pull_request", "finance_core/intake/receipt_media.py", "true"),
        ("pull_request", "finance_core/intake/_receipt_media_worker.py", "true"),
        ("pull_request", "tests/test_receipt_media_v1.py", "true"),
        ("pull_request", "tests/test_receipt_media_vectors_v1.py", "true"),
        ("pull_request", "tests/fixtures/receipt_media_v1/vectors.zip", "true"),
        ("pull_request", "tests/fixtures/receipt_media_v1/nested/future-vector.json", "true"),
        ("pull_request", "finance_core/openclaw_staging_bridge/ocr_boundary.py", "true"),
        ("pull_request", "tests/test_tesseract_pinned_resources_v1.py", "true"),
        ("pull_request", "tests/test_linux_receipt_ocr_acceptance_v1.py", "true"),
        ("pull_request", "tests/test_receipt_ocr_evidence.py", "true"),
        (
            "pull_request",
            "tests/test_openclaw_staging_bridge_ocr_production_boundary_v1.py",
            "true",
        ),
        ("pull_request", "tests/fixtures/linux_receipt_ocr/synthetic_mixed_receipt.png", "true"),
        ("pull_request", "tests/fixtures/linux_receipt_ocr/synthetic_mixed_receipt.jpg", "true"),
        (
            "pull_request",
            "tests/fixtures/linux_receipt_ocr/synthetic_incomplete_receipt.png",
            "true",
        ),
        ("pull_request", "tests/fixtures/linux_receipt_ocr/provenance.json", "true"),
        ("pull_request", "scripts/linux_ocr_assets_v1.json", "true"),
        ("pull_request", "scripts/prepare_linux_ocr.py", "true"),
        ("pull_request", "finance_core/parser_proposals/__init__.py", "true"),
        ("pull_request", "plugins/finance-bridge/src/index.ts", "true"),
        ("pull_request", "requirements-dev.txt", "true"),
        ("pull_request", "pyproject.toml", "true"),
        ("pull_request", ".github/workflows/validate.yml", "true"),
    ],
)
def test_actual_scope_script_produces_release_compatible_evidence(
    tmp_path: Path, event: str, path: str, expected: str
) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nprintf "%s\\n" "$CHANGED_PATH"\n', encoding="utf-8")
    fake_git.chmod(0o755)
    output = tmp_path / "output"
    completed = subprocess.run(
        ["bash", "-c", _step_script("Classify Bridge scope")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "BASE_SHA": "a" * 40,
            "HEAD_SHA": "b" * 40,
            "EVENT_NAME": event,
            "CHANGED_PATH": path,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == f"bridge={expected}\n"


@pytest.mark.parametrize(
    ("event", "scope", "bridge", "report", "accepted"),
    [
        ("push", "true", "success", "success", True),
        ("push", "false", "skipped", "success", False),
        ("workflow_dispatch", "false", "skipped", "success", False),
        ("pull_request", "false", "skipped", "success", True),
        ("pull_request", "true", "skipped", "success", False),
        ("pull_request", "true", "failure", "success", False),
        ("push", "true", "success", "failure", False),
        ("push", "true", "success", "cancelled", False),
        ("pull_request", "false", "skipped", "failure", False),
        ("pull_request", "unknown", "skipped", "success", False),
    ],
)
def test_actual_validation_gate_rejects_missing_release_evidence(
    event: str, scope: str, bridge: str, report: str, accepted: bool
) -> None:
    completed = subprocess.run(
        ["bash", "-c", _step_script("Require all applicable validation")],
        env={
            **os.environ,
            "EVENT_NAME": event,
            "BRIDGE_SCOPE": scope,
            "BRIDGE_RESULT": bridge,
            "REPORT_RESULT": report,
            "CLASSIFY_RESULT": "success",
            "PYTEST_RESULT": "success",
            "QUALITY_RESULT": "success",
        },
        capture_output=True,
        text=True,
    )
    assert (completed.returncode == 0) is accepted, completed.stderr


@pytest.mark.parametrize("operation", ["modify", "delete", "rename-in", "rename-out"])
@pytest.mark.parametrize(
    "path",
    [
        "finance_core/intake/receipt_ocr_proposal.py",
        "finance_core/parser_proposals/receipt_total_parser.py",
        "tests/test_receipt_total_parser_hierarchy_v2.py",
        "tests/fixtures/receipt_total_parser_v2/actual_synthetic_vectors.json",
        "tests/fixtures/receipt_total_parser_v2/legacy_persisted_v1.json",
        "tests/fixtures/receipt_total_parser_v2/nested/future-vector.json",
        ".github/workflows/example.yml",
        "scripts/example.py",
        "pyproject.toml",
        "requirements-dev.txt",
        "MANIFEST.in",
        "plugins/finance-bridge/src/controller.ts",
        "native/example.swift",
        "finance_core/openclaw_staging_bridge/commands.py",
        "finance_core/application/example.py",
        "finance_core/intake/__init__.py",
        "finance_core/intake/macos_vision_receipt_ocr.py",
        "finance_core/parser_proposals/__init__.py",
        "finance_core/parser_proposals/ai_example.py",
        "finance_core/intake/receipt_ocr_evidence.py",
        "finance_core/intake/tesseract_resources.py",
        "finance_core/intake/receipt_media.py",
        "finance_core/intake/_receipt_media_worker.py",
        "tests/test_receipt_media_v1.py",
        "tests/test_receipt_media_vectors_v1.py",
        "tests/fixtures/receipt_media_v1/vectors.zip",
        "tests/fixtures/receipt_media_v1/nested/future-vector.json",
        "finance_core/openclaw_staging_bridge/ocr_boundary.py",
        "tests/test_tesseract_pinned_resources_v1.py",
        "tests/test_linux_receipt_ocr_acceptance_v1.py",
        "tests/test_receipt_ocr_evidence.py",
        "tests/test_openclaw_staging_bridge_ocr_production_boundary_v1.py",
        "tests/fixtures/linux_receipt_ocr/synthetic_mixed_receipt.png",
        "tests/fixtures/linux_receipt_ocr/synthetic_mixed_receipt.jpg",
        "tests/fixtures/linux_receipt_ocr/synthetic_incomplete_receipt.png",
        "tests/fixtures/linux_receipt_ocr/provenance.json",
        "scripts/linux_ocr_assets_v1.json",
        "scripts/prepare_linux_ocr.py",
        "README.md",
    ],
)
def test_actual_scope_script_keeps_renamed_bridge_input_in_scope(
    tmp_path: Path, path: str, operation: str
) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    scoped = tmp_path / path
    archived = tmp_path / "archived-example.txt"
    original = archived if operation == "rename-in" else scoped
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("synthetic classifier witness\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    if operation == "modify":
        scoped.write_text("changed synthetic classifier witness\n", encoding="utf-8")
    elif operation == "delete":
        scoped.unlink()
    elif operation == "rename-in":
        scoped.parent.mkdir(parents=True, exist_ok=True)
        archived.rename(scoped)
    else:
        scoped.rename(archived)
    git("add", "-A")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", operation)
    output = tmp_path / "output"
    completed = subprocess.run(
        ["bash", "-c", _step_script("Classify Bridge scope")],
        cwd=tmp_path,
        env={
            **os.environ,
            "BASE_SHA": base,
            "HEAD_SHA": git("rev-parse", "HEAD"),
            "EVENT_NAME": "pull_request",
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    expected = "false" if path == "README.md" else "true"
    assert output.read_text(encoding="utf-8") == f"bridge={expected}\n"


def test_linux_ocr_is_mandatory_in_existing_bridge_lane() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "name: bridge (${{ matrix.os }})" in source
    assert "runner: ubuntu-24.04" in source
    assert "runs-on: ${{ matrix.runner }}" in source
    assert 'FINANCE_LINUX_OCR_REQUIRED: "1"' in source
    assert "tests/test_linux_receipt_ocr_acceptance_v1.py" in source
    assert "scripts/prepare_linux_ocr.py" in source
    assert "sudo apt-get install --no-install-recommends -y tesseract-ocr" in source
    assert (
        "continue-on-error"
        not in source.split("- name: Prepare pinned Linux OCR acceptance assets", 1)[1].split(
            "- name: Set up exact Node runtime", 1
        )[0]
    )


def test_ocr_scope_regex_is_identical_in_workflow_and_independent_verifier() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    workflow_pattern = re.search(r"if grep -Eq '([^']+)' ", source)
    assert workflow_pattern
    tree = ast.parse((REPOSITORY_ROOT / "scripts" / "verify_candidate_validation.py").read_text())
    patterns = [
        ast.literal_eval(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compile"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and str(node.args[0].value).startswith(r"^(\.github/")
    ]
    assert patterns == [workflow_pattern.group(1)]
    assert 'totals["tests"] < 13' in source
    assert '("skipped", "failures", "errors")' in source


def test_linux_media_is_mandatory_and_preserves_failure_evidence() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert 'media_root="/opt/finance-media-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in source
    assert "sudo apt-get install --no-install-recommends -y tesseract-ocr acl" in source
    section = source.split("- name: Prepare isolated media candidate snapshot and runtime", 1)[
        1
    ].split("- name: Set up exact Node runtime", 1)[0]
    assert "if: runner.os == 'Linux'" in section
    assert "sudo -u financemedia -- env -i" in section
    assert "FINANCE_LINUX_MEDIA_REQUIRED=1" in section
    assert 'FINANCE_LINUX_OCR_CONFIG="$media_root/work/ocr/ocr_engine.json"' in section
    assert "tests/test_receipt_media_vectors_v1.py" in section
    assert 'totals["tests"] != 50' in section
    assert "continue-on-error" not in section
    assert "always() && runner.os == 'Linux'" in section
    assert "${{ runner.temp }}/linux-media/" in section
    assert "resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))" in section
    assert "code_runtime_read_only" in section
    assert "Path(sys.prefix) == expected_prefix" in section
    assert "Path(sys.base_prefix) == expected_base" in section
    assert "binary_hash == expected_hash" in section
    assert "sys.version_info[:3] == (3, 12, 14)" in section
    assert 'expected_base == Path("/opt/hostedtoolcache/Python/3.12.14/x64")' in section
    assert "mapped_libpython" in section
    assert "libpython_hash == expected_libpython_hash" in section
    assert 'config.get("include-system-site-packages") == "false"' in section
    assert 'result.get("status") == "identity"' in section
    assert '"--identity"' in section
    assert '"CapEff", "CapPrm"' in section
    assert 'item["current"] >= 16' in section
    assert "remaining_uid_processes" in section
    assert "os.O_NOFOLLOW" in section
    assert "visited > MAX_SDK_ENTRIES" in section
    assert "git ls-tree -r -z --full-tree" in section
    assert "git cat-file commit" in section
    assert "committed_tree_hash(expected) != tree_sha" in section
    assert 'member.mode != (0o755 if mode == "100755" else 0o644)' in section
    assert "def read_bounded(path, maximum)" in section
    assert "os.O_NOFOLLOW | os.O_NONBLOCK" in section
    assert "before.st_nlink != 1" in section
    assert "identity(path.lstat()) != identity(before)" in section
    assert "len(name) > 4096" in section
    assert "MAX_ARCHIVE = 134_217_728" in section
    assert "MAX_MEMBERS = 8192" in section
    assert "MAX_FILE = 20_000_000" in section
    assert "PY_MEDIA_SDK_ACL" in section
    assert "MAX_SDK_ENTRIES = 32768" in section
    assert "MAX_SDK_FILE = 67_108_864" in section
    assert "info.st_uid not in {0, source_owner}" in section
    assert 'SDK_ROOT = Path("/opt/hostedtoolcache/Python/3.12.14/x64")' in section
    assert "SDK_VERSION = (3, 12, 14)" in section
    assert "bef88f140b625959f8af25c7b75cce2cd5d4b29cc2f2b079befd7f68eda4dba0" in section
    assert "1fa3c52ba5aa8f6b2852836a4bf6cbb23161f7cf379f6f19248d87124b28b38a" in section
    assert '"system.posix_acl_access"' in section
    assert '"system.posix_acl_default"' in section
    assert '"--no-mask"' in section
    assert ":r-X" not in section
    assert "for permissions in (4, 5)" in section
    assert "record[-1] == permissions" in section
    assert "for start in range(0, len(group), 128)" in section
    assert 'f"u:{fin_uid}:{permissions}"' in section
    assert "class SdkAclRefusal(ValueError)" in section
    assert "error.detail" in section
    assert 'result["failure_detail"] = error.detail' in section
    assert '"path_truncated"' in section
    assert "before_access_sha256=acl_digest(access)" in section
    assert "after_default_sha256=acl_digest(actual_default)" in section
    assert '"other_subjects_raw_effective_and_mask_unchanged"' in section
    assert "Existing ACL mask cannot admit read/execute without expansion" in section
    assert '"other_subjects_effective_permissions_and_default_acl": "UNCHANGED"' in section
    assert '"mask_recalculation": False' in section
    assert "only ephemeral fin named UID" in section
    assert '"--require-hashes"' in section
    assert '"PIP_CONFIG_FILE": os.devnull' in section
    assert '"/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C", "TZ=UTC"' in section
    assert '"TMPDIR=" + str(root / "work")' in section
    assert "LD_LIBRARY_PATH" not in section
    assert "PYTHONPATH" not in section
    assert "PYTHONHOME" not in section
    assert '"bootstrap-" + name' in section
    assert '("snapshot.json", 65_536)' in section
    assert '("sdk-acl.json", 65_536)' in section
    assert '("runtime.json", 65_536)' in section
    assert '("startup.json", 393_216)' in section
    assert "target.chmod(0o400)" in section
    assert '"finance-media-ci-snapshot-v1"' in section
    assert '"finance-media-ci-sdk-acl-v1"' in section
    assert '"finance-media-ci-runtime-v1"' in section
    assert '"finance-media-ci-startup-v1"' in section
    assert 'core_root="/tmp/finance-media-core-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in section
    assert 'FINANCE_RUNTIME_ROOT="$core_root"' in section
    assert "def prepare_core_runtime(path, uid, gid)" in section
    assert "os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)" in section
    assert (
        "core_runtime = prepare_core_runtime(core_path, account.pw_uid, account.pw_gid)" in section
    )

    startup = _inline_python(
        "Verify isolated media startup and prepare OCR assets", "PY_MEDIA_STARTUP"
    )
    startup_tree = ast.parse(startup)
    startup_code = next(
        ast.literal_eval(node.value)
        for node in startup_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "STARTUP_CODE" for target in node.targets
        )
    )
    assert (
        "from finance_core.runtime_paths import require_runtime_root, live_database_path"
        in startup_code
    )
    assert startup_code.index("core = require_runtime_root()") < startup_code.index(
        "from finance_core.intake.receipt_media import _worker_run"
    )
    assert startup_code.index("core = require_runtime_root()") < startup_code.index(
        "with os.scandir(core)"
    )
    assert startup_code.index("live_database_path()") < startup_code.index(
        "from finance_core.intake.receipt_media import _worker_run"
    )
    assert startup_code.index("live_database_path()") < startup_code.index("with os.scandir(core)")
    assert '"FINANCE_RUNTIME_ROOT=" + str(core_path)' in startup

    launch = _inline_python("Require actual Linux receipt media acceptance", "PY_MEDIA_LAUNCH")
    assert (
        "from finance_core.runtime_paths import require_runtime_root, live_database_path" in launch
    )
    assert launch.index("core = require_runtime_root()") < launch.index("os.execv(")
    assert launch.index("core = require_runtime_root()") < launch.index("with os.scandir(core)")
    assert launch.index("live_database_path()") < launch.index("os.execv(")
    assert launch.index("live_database_path()") < launch.index("with os.scandir(core)")
    assert 'FINANCE_RUNTIME_ROOT="$core_root"' in section

    export = _inline_python(
        "Preserve actual Linux receipt media acceptance evidence", "PY_MEDIA_EXPORT"
    )
    assert "def remove_empty_core_runtime(path, expected)" in export
    assert export.count('os.rmdir("database", dir_fd=root_fd)') == 1
    assert export.count("os.rmdir(path.name, dir_fd=parent_fd)") == 1
    assert "shutil.rmtree" not in export
    assert "core_runtime_postcondition" in export
    assert export.index("if survivors:") < export.index(
        'export_state["core_runtime_postcondition"] = remove_empty_core_runtime'
    )
    assert export.index("copy_member(selected)") < export.index("if core_postcondition_failed:")
    assert (
        "Synthetic Core postcondition refused; residue and bounded media evidence retained"
        in export
    )

    for label in (
        "PY_MEDIA_SNAPSHOT",
        "PY_MEDIA_SDK_ACL",
        "PY_MEDIA_RUNTIME",
        "PY_MEDIA_STARTUP",
        "PY_MEDIA_LAUNCH",
        "PY_MEDIA_PROOF",
        "PY_MEDIA_EXPORT",
    ):
        step = (
            "Preserve actual Linux receipt media acceptance evidence"
            if label.endswith("EXPORT")
            else (
                "Prepare isolated media candidate snapshot and runtime"
                if label in {"PY_MEDIA_SNAPSHOT", "PY_MEDIA_SDK_ACL", "PY_MEDIA_RUNTIME"}
                else (
                    "Verify isolated media startup and prepare OCR assets"
                    if label == "PY_MEDIA_STARTUP"
                    else "Require actual Linux receipt media acceptance"
                )
            )
        )
        script = _step_script(step)
        ast.parse(_inline_python(step, label))
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


@pytest.mark.parametrize("entrypoint", ["startup", "launch"])
@pytest.mark.parametrize("runtime", ["canonical", "symlink", "groupwrite"])
def test_actual_inline_core_guard_runs_before_directory_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    runtime: str,
) -> None:
    """Execute the inline Core guard with the real runtime-path functions and count scans."""
    import stat

    if entrypoint == "startup":
        startup = ast.parse(
            _inline_python(
                "Verify isolated media startup and prepare OCR assets", "PY_MEDIA_STARTUP"
            )
        )
        code = next(
            ast.literal_eval(node.value)
            for node in startup.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "STARTUP_CODE"
                for target in node.targets
            )
        )
    else:
        code = _inline_python("Require actual Linux receipt media acceptance", "PY_MEDIA_LAUNCH")
    tree = ast.parse(code)
    guard = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.ImportFrom) and child.module == "finance_core.runtime_paths"
            for child in ast.walk(node)
        )
    )

    root = tmp_path / "private-runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    database = root / "database"
    database.mkdir(mode=0o700)
    database.chmod(0o700)
    selected = root
    if runtime == "symlink":
        selected = tmp_path / "runtime-alias"
        selected.symlink_to(root, target_is_directory=True)
    elif runtime == "groupwrite":
        root.chmod(0o770)

    def directory_identity(path: Path) -> list[int]:
        info = path.lstat()
        return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]

    expected = {
        "path": str(root),
        "root_identity": directory_identity(root),
        "database_identity": directory_identity(database),
    }
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(selected))
    calls: list[str] = []
    original_scandir = os.scandir

    def count_scandir(path: str | os.PathLike[str] | int) -> Any:
        calls.append(str(path))
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", count_scandir)
    namespace: dict[str, object] = {
        "Path": Path,
        "os": os,
        "stat": stat,
        "uid": os.getuid(),
        "expected_core": expected,
        "receipt": {},
        "admitted": True,
    }
    module = ast.Module(body=[guard], type_ignores=[])
    exec(
        compile(ast.fix_missing_locations(module), f"<{entrypoint}-core-guard>", "exec"), namespace
    )

    if runtime == "canonical":
        assert namespace["admitted"] is True
        assert len(calls) == 2
        assert namespace["receipt"]["core_runtime"]["only_empty_database"] is True  # type: ignore[index]
        assert namespace["receipt"]["core_runtime"]["database_path"] == str(database / "finance.db")  # type: ignore[index]
        assert not (database / "finance.db").exists()
    else:
        assert namespace["admitted"] is False
        assert calls == []
        core_result = namespace["receipt"]["core_runtime"]  # type: ignore[index]
        assert core_result["failure_type"] == "RuntimePathConfigurationError"  # type: ignore[index]
        assert not (database / "finance.db").exists()


def test_actual_snapshot_helper_binds_commit_tree_blobs_modes_and_paths(tmp_path: Path) -> None:
    """Exercise the workflow snapshot helper against a tiny synthetic Git commit."""
    import io
    import tarfile

    repository = tmp_path / "repository"
    (repository / "bin").mkdir(parents=True)
    (repository / "README.md").write_bytes(b"synthetic snapshot fixture\n")
    executable = repository / "bin" / "tool"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Finance Test",
            "-c",
            "user.email=finance-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "synthetic snapshot fixture",
        ],
        cwd=repository,
        check=True,
    )

    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", *arguments], cwd=repository, check=True, capture_output=True
        ).stdout

    candidate = git("rev-parse", "HEAD").decode().strip()
    tree_sha = git("rev-parse", "HEAD^{tree}").decode().strip()
    archive = tmp_path / "candidate.tar"
    subprocess.run(
        [
            "git",
            "-c",
            "tar.umask=0022",
            "archive",
            "--format=tar",
            "--output",
            str(archive),
            candidate,
        ],
        cwd=repository,
        check=True,
    )
    inventory = tmp_path / "inventory.bin"
    inventory.write_bytes(git("ls-tree", "-r", "-z", "--full-tree", candidate))
    commit_object = tmp_path / "commit.bin"
    commit_object.write_bytes(git("cat-file", "commit", candidate))

    namespace: dict[str, object] = {"__name__": "workflow_snapshot_test"}
    script = _inline_python(
        "Prepare isolated media candidate snapshot and runtime", "PY_MEDIA_SNAPSHOT"
    )
    exec(compile(script, "<PY_MEDIA_SNAPSHOT>", "exec"), namespace)
    extract = namespace["extract_candidate"]
    safe_name = namespace["safe_name"]
    assert callable(extract) and callable(safe_name)
    destination = tmp_path / "snapshot"
    receipt = extract(archive, inventory, destination, candidate, tree_sha, commit_object)
    assert receipt["candidate_sha"] == candidate
    assert receipt["candidate_tree_sha"] == tree_sha
    assert receipt["files"] == 2
    assert receipt["bytes"] == len(b"synthetic snapshot fixture\n") + len(b"#!/bin/sh\nexit 0\n")
    assert (destination / "README.md").read_bytes() == b"synthetic snapshot fixture\n"
    assert (destination / "README.md").stat().st_mode & 0o777 == 0o444
    assert (destination / "bin/tool").read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert (destination / "bin/tool").stat().st_mode & 0o777 == 0o555
    assert safe_name("receipts/page.2.heic").as_posix() == "receipts/page.2.heic"
    unsafe_names = (
        "",
        "../outside",
        "/absolute",
        "a//b",
        "a/./b",
        "a\\b",
        ".git/config",
        ".venv/bin/python",
    )
    for unsafe in unsafe_names:
        with pytest.raises(ValueError):
            safe_name(unsafe)

    expected_content = {
        "README.md": b"synthetic snapshot fixture\n",
        "bin/tool": b"#!/bin/sh\nexit 0\n",
    }

    def make_archive(
        path: Path, entries: list[tuple[str, bytes, int, str]], comment: str = candidate
    ) -> None:
        with tarfile.open(
            path, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment}
        ) as package:
            for name, content, mode, kind in entries:
                member = tarfile.TarInfo(name)
                member.mode = mode
                if kind == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "README.md"
                    package.addfile(member)
                else:
                    member.size = len(content)
                    package.addfile(member, io.BytesIO(content))

    regular_entries = [
        ("README.md", expected_content["README.md"], 0o644, "file"),
        ("bin/tool", expected_content["bin/tool"], 0o755, "file"),
    ]
    invalid_archives = {
        "duplicate": regular_entries + [regular_entries[0]],
        "symlink": [("README.md", b"", 0o777, "symlink"), regular_entries[1]],
        "traversal": [("../outside", b"bad", 0o644, "file")],
        "wrong_mode": [
            regular_entries[0],
            ("bin/tool", expected_content["bin/tool"], 0o644, "file"),
        ],
    }
    for variant, entries in invalid_archives.items():
        bad_archive = tmp_path / f"{variant}.tar"
        make_archive(bad_archive, entries)
        rejected_destination = tmp_path / f"rejected-{variant}"
        with pytest.raises(ValueError):
            extract(
                bad_archive, inventory, rejected_destination, candidate, tree_sha, commit_object
            )
        assert not rejected_destination.exists()

    wrong_comment = tmp_path / "wrong-comment.tar"
    make_archive(wrong_comment, regular_entries, "0" * 40)
    with pytest.raises(ValueError, match="does not bind"):
        extract(
            wrong_comment,
            inventory,
            tmp_path / "rejected-comment",
            candidate,
            tree_sha,
            commit_object,
        )

    with pytest.raises(ValueError, match="root tree"):
        extract(archive, inventory, tmp_path / "rejected-tree", candidate, "0" * 40, commit_object)
    changed_inventory = tmp_path / "changed-inventory.bin"
    changed_inventory.write_bytes(
        b"\0".join(
            record.replace(b"100755 blob ", b"100644 blob ", 1)
            if b"\tbin/tool" in record
            else record
            for record in inventory.read_bytes().split(b"\0")
        )
    )
    with pytest.raises(ValueError, match="root tree"):
        extract(
            archive,
            changed_inventory,
            tmp_path / "rejected-inventory",
            candidate,
            tree_sha,
            commit_object,
        )
    changed_commit = tmp_path / "changed-commit.bin"
    changed_commit.write_bytes(commit_object.read_bytes()[:-1] + b"x")
    with pytest.raises(ValueError, match="candidate object"):
        extract(
            archive,
            inventory,
            tmp_path / "rejected-commit",
            candidate,
            tree_sha,
            changed_commit,
        )

    linked_archive = tmp_path / "linked-archive.tar"
    linked_archive.symlink_to(archive)
    with pytest.raises(OSError):
        extract(
            linked_archive,
            inventory,
            tmp_path / "rejected-archive-link",
            candidate,
            tree_sha,
            commit_object,
        )
    hardlinked_inventory = tmp_path / "hardlinked-inventory.bin"
    hardlinked_inventory.hardlink_to(inventory)
    with pytest.raises(ValueError, match="bounded regular file"):
        extract(
            archive,
            hardlinked_inventory,
            tmp_path / "rejected-inventory-link",
            candidate,
            tree_sha,
            commit_object,
        )

    oversized_inventory = tmp_path / "oversized-inventory.bin"
    oversized_inventory.write_bytes(b"x" * 2_097_153)
    with pytest.raises(ValueError, match="bounded regular file"):
        extract(
            archive,
            oversized_inventory,
            tmp_path / "rejected-inventory-size",
            candidate,
            tree_sha,
            commit_object,
        )
    oversized_archive = tmp_path / "oversized-archive.tar"
    oversized_archive.write_bytes(b"x")
    with oversized_archive.open("r+b") as stream:
        stream.truncate(134_217_729)
    with pytest.raises(ValueError, match="bounded regular file"):
        extract(
            oversized_archive,
            inventory,
            tmp_path / "rejected-archive-size",
            candidate,
            tree_sha,
            commit_object,
        )


def _encode_synthetic_acl(entries: dict[tuple[int, int | None], int]) -> bytes:
    import struct

    raw = bytearray(struct.pack("<I", 2))
    for (tag, qualifier), permissions in sorted(
        entries.items(), key=lambda item: (item[0][0], -1 if item[0][1] is None else item[0][1])
    ):
        raw.extend(
            struct.pack("<HHI", tag, permissions, 0xFFFFFFFF if qualifier is None else qualifier)
        )
    return bytes(raw)


def _synthetic_acl_for_mode(mode: int) -> bytes:
    group_permissions = (mode >> 3) & 7
    entries = {
        (1, None): (mode >> 6) & 7,
        (2, 12345): 7,
        (4, None): group_permissions,
        (8, 23456): 7,
        (16, None): group_permissions,
        (32, None): mode & 7,
    }
    return _encode_synthetic_acl(entries)


def _synthetic_masked_raw_execute_acl() -> bytes:
    return _encode_synthetic_acl(
        {
            (1, None): 6,
            (2, 12345): 4,
            (4, None): 7,
            (8, 23456): 4,
            (16, None): 6,
            (32, None): 6,
        }
    )


def test_selected_sdk_validator_binds_fixed_path_version_and_hashes(tmp_path: Path) -> None:
    """Validate selected SDK checks against tiny synthetic files, not the hosted SDK."""
    import hashlib
    import os
    from types import SimpleNamespace

    step = "Prepare isolated media candidate snapshot and runtime"
    namespace = _inline_namespace(step, "PY_MEDIA_RUNTIME")
    expected_root = Path("/opt/hostedtoolcache/Python/3.12.14/x64")
    expected_hashes = {
        "bin/python3.12": "bef88f140b625959f8af25c7b75cce2cd5d4b29cc2f2b079befd7f68eda4dba0",
        "lib/libpython3.12.so.1.0": (
            "1fa3c52ba5aa8f6b2852836a4bf6cbb23161f7cf379f6f19248d87124b28b38a"
        ),
    }
    assert namespace["SDK_ROOT"] == expected_root
    assert namespace["SDK_VERSION"] == (3, 12, 14)
    assert namespace["SDK_HASHES"] == expected_hashes

    sdk_root = tmp_path / "sdk"
    binary = sdk_root / "bin/python3.12"
    libpython = sdk_root / "lib/libpython3.12.so.1.0"
    binary.parent.mkdir(parents=True)
    libpython.parent.mkdir(parents=True)
    binary_bytes = b"synthetic selected Python binary"
    libpython_bytes = b"synthetic selected libpython"
    binary.write_bytes(binary_bytes)
    binary.chmod(0o755)
    libpython.write_bytes(libpython_bytes)
    libpython.chmod(0o644)

    namespace["SDK_ROOT"] = sdk_root
    namespace["SDK_VERSION"] = (3, 12, 14)
    namespace["SDK_HASHES"] = {
        "bin/python3.12": hashlib.sha256(binary_bytes).hexdigest(),
        "lib/libpython3.12.so.1.0": hashlib.sha256(libpython_bytes).hexdigest(),
    }
    namespace["sys"] = SimpleNamespace(version_info=(3, 12, 14))
    validate = namespace["validate_selected_sdk"]
    assert callable(validate)

    identity = validate(sdk_root, binary, os.getuid())
    assert identity["selected_base_prefix"] == str(sdk_root)
    assert identity["selected_binary"] == str(binary)
    assert identity["selected_python_version"] == [3, 12, 14]
    assert identity["selected_binary_sha256"] == hashlib.sha256(binary_bytes).hexdigest()
    assert identity["selected_libpython_sha256"] == hashlib.sha256(libpython_bytes).hexdigest()

    with pytest.raises(ValueError, match="exact canonical SDK"):
        validate(tmp_path / "other-sdk", binary, os.getuid())
    with pytest.raises(ValueError, match="exact canonical SDK"):
        validate(sdk_root, libpython, os.getuid())
    namespace["sys"].version_info = (3, 12, 13)
    with pytest.raises(ValueError, match="exact canonical SDK"):
        validate(sdk_root, binary, os.getuid())
    namespace["sys"].version_info = (3, 12, 14)
    namespace["SDK_HASHES"]["bin/python3.12"] = "0" * 64
    with pytest.raises(ValueError, match="bytes or custody differ"):
        validate(sdk_root, binary, os.getuid())


@pytest.mark.parametrize("mutation", ["none", "other_raw_only", "fin_write"])
def test_sdk_acl_adapter_models_gnu_x_and_preserves_other_subjects(
    tmp_path: Path, mutation: str
) -> None:
    """Model GNU raw-entry X with fake ACL adapters; native Linux ACL execution is NOT_RUN."""
    import os
    import stat
    from types import SimpleNamespace

    namespace = _inline_namespace(
        "Prepare isolated media candidate snapshot and runtime", "PY_MEDIA_SDK_ACL"
    )
    sdk_root = tmp_path / "opt/hostedtoolcache/Python/3.12.14/x64"
    sdk_root.mkdir(parents=True)
    workflow_ancestors = tuple(namespace["ANCESTORS"])
    workflow_sdk_root = namespace["SDK_ROOT"]
    assert tuple(reversed(workflow_ancestors)) == tuple(
        path for path in workflow_sdk_root.parents if path != Path("/")
    )
    fake_opt = sdk_root.parents[3]
    ancestors = tuple(fake_opt / path.relative_to(Path("/opt")) for path in workflow_ancestors)
    for path in ancestors:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777 if path == fake_opt else 0o755)
    sdk_root.chmod(0o755)
    binary = sdk_root / "bin/python3.12"
    library = sdk_root / "lib/libpython3.12.so.1.0"
    binary.parent.mkdir()
    library.parent.mkdir()
    binary.write_bytes(b"synthetic SDK binary")
    binary.chmod(0o666)
    library.write_bytes(b"synthetic SDK library")
    library.chmod(0o644)
    (binary.parent / "python").symlink_to(binary.name)

    namespace["SDK_ROOT"] = sdk_root
    namespace["ANCESTORS"] = ancestors
    fin_uid = os.getuid() + 100_000
    source_owner = os.getuid()
    nodes = list(namespace["sdk_acl_nodes"](sdk_root, fin_uid, source_owner))
    paths = [*ancestors, *nodes]
    access_acls = {
        path: (
            _synthetic_masked_raw_execute_acl()
            if path == binary
            else _synthetic_acl_for_mode(stat.S_IMODE(path.lstat().st_mode))
        )
        for path in paths
    }
    default_acls = {
        path: _synthetic_acl_for_mode(stat.S_IMODE(path.lstat().st_mode))
        if stat.S_ISDIR(path.lstat().st_mode)
        else None
        for path in paths
    }
    initial_access = dict(access_acls)
    initial_defaults = dict(default_acls)
    opt_entries = namespace["decode_acl"](initial_access[fake_opt])
    assert fake_opt.lstat().st_mode & 0o777 == 0o777
    assert opt_entries[(16, None)] == 7
    binary_entries = namespace["decode_acl"](initial_access[binary])
    binary_mask = binary_entries[(16, None)]
    mode_needed = 4 | (1 if binary.lstat().st_mode & 0o111 else 0)
    gnu_x_permissions = 4 | int(any(permissions & 1 for permissions in binary_entries.values()))
    assert binary.lstat().st_mode & 0o777 == 0o666
    assert binary_entries[(4, None)] == 7 and binary_mask == 6
    assert mode_needed == 4
    # Frozen D's r-X selects from raw ACL bits, even when the mask hides execute.
    assert gnu_x_permissions == 5
    assert gnu_x_permissions & binary_mask == mode_needed
    assert gnu_x_permissions != mode_needed

    def read_synthetic_acl(path: Path, name: str) -> bytes | None:
        if name == "system.posix_acl_access":
            return access_acls[Path(path)]
        assert name == "system.posix_acl_default"
        return default_acls[Path(path)]

    calls: list[list[str]] = []

    def apply_synthetic_acl(arguments: list[str], *, check: bool, timeout: int) -> None:
        assert check is True and timeout == 15
        assert arguments[:3] == ["/usr/bin/setfacl", "--no-mask", "-m"]
        selector = arguments[3].rsplit(":", maxsplit=1)[1]
        assert arguments[3] == f"u:{fin_uid}:{selector}"
        if selector == "r-X":
            # GNU X scans raw ACL entries, including execute masked off in st_mode.
            permissions = 4 | int(
                any(value & 1 for value in namespace["decode_acl"](access_acls[binary]).values())
            )
            assert permissions == gnu_x_permissions
        else:
            permissions = int(selector)
            assert selector == str(permissions)
        assert permissions in {4, 5}
        calls.append(arguments)
        batch_paths = arguments[arguments.index("--") + 1 :]
        assert len(batch_paths) <= 128
        for raw_path in batch_paths:
            path = Path(raw_path)
            entries = namespace["decode_acl"](access_acls[path])
            needed = 4 | (
                1 if stat.S_ISDIR(path.lstat().st_mode) or path.lstat().st_mode & 0o111 else 0
            )
            if selector == "r-X":
                assert path == binary
                assert permissions & entries[(16, None)] == needed
            else:
                assert permissions == needed
            applied = (
                permissions | 2
                if mutation == "fin_write" and path == binary and selector != "r-X"
                else permissions
            )
            entries[(2, fin_uid)] = applied
            if mutation == "other_raw_only" and path == binary and selector != "r-X":
                entries[(4, None)] = 6
            access_acls[path] = _encode_synthetic_acl(entries)

    namespace["read_acl"] = read_synthetic_acl
    namespace["subprocess"] = SimpleNamespace(run=apply_synthetic_acl)

    # Exercise D's former r-X command against the same masked raw-execute ACL.
    # GNU resolves it to raw fin=5 (effective 4); D's exact raw equality expected 4.
    binary_before_legacy = access_acls[binary]
    apply_synthetic_acl(
        ["/usr/bin/setfacl", "--no-mask", "-m", f"u:{fin_uid}:r-X", "--", str(binary)],
        check=True,
        timeout=15,
    )
    legacy_entries = namespace["decode_acl"](access_acls[binary])
    assert legacy_entries[(2, fin_uid)] == 5
    assert legacy_entries[(2, fin_uid)] & binary_mask == mode_needed == 4
    assert legacy_entries[(2, fin_uid)] != mode_needed
    access_acls[binary] = binary_before_legacy
    calls.clear()

    if mutation == "none":
        result = namespace["restrict_fin_sdk"](sdk_root, fin_uid, source_owner)
        assert calls
        assert result["sdk_acl_entries"] == len(paths)
        assert result["fin_uid"] == fin_uid
        assert result["before_acl_sha256"] != result["after_acl_sha256"]
        assert result["other_subjects_effective_permissions_and_default_acl"] == "UNCHANGED"
        assert result["mask_recalculation"] is False
        for path in paths:
            before_info = path.lstat()
            before = namespace["acl_subjects"](initial_access[path], before_info, fin_uid)
            after_entries = namespace["decode_acl"](access_acls[path])
            after = namespace["acl_subjects"](access_acls[path], path.lstat(), fin_uid)
            required = 4 | (
                1 if stat.S_ISDIR(path.lstat().st_mode) or path.lstat().st_mode & 0o111 else 0
            )
            assert after_entries[(2, fin_uid)] == required
            assert after_entries[(2, fin_uid)] & 2 == 0
            assert after == before
            assert default_acls[path] == initial_defaults[path]
        assert namespace["decode_acl"](access_acls[binary])[(2, fin_uid)] == 4
        return

    with pytest.raises(ValueError) as refusal:
        namespace["restrict_fin_sdk"](sdk_root, fin_uid, source_owner)
    assert calls
    detail = cast(Any, refusal.value).detail
    assert detail["stage"] == "post_acl"
    assert detail["path"] == str(binary)
    assert len(detail["path"]) <= 4096
    assert detail["path_truncated"] is False
    assert len(detail["before_access_sha256"]) == 64
    assert len(detail["after_access_sha256"]) == 64
    if mutation == "other_raw_only":
        changed = namespace["decode_acl"](access_acls[binary])
        assert changed[(4, None)] == 6
        assert changed[(4, None)] != binary_entries[(4, None)]
        assert changed[(4, None)] & binary_mask == binary_entries[(4, None)] & binary_mask == 6
        assert detail["comparisons"]["other_subjects_raw_effective_and_mask_unchanged"] is False
    else:
        actual_fin = namespace["decode_acl"](access_acls[binary])[(2, fin_uid)]
        assert actual_fin & 2
        assert detail["actual_fin_permissions"] == actual_fin
        assert detail["comparisons"]["fin_raw_permissions_match"] is False


@pytest.mark.parametrize("unsafe", ["fin_owned", "external_link"])
def test_sdk_acl_scope_refuses_fin_owned_nodes_and_external_links(
    tmp_path: Path, unsafe: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check path ownership/link guards with local metadata adapters, not native ACLs."""
    import os

    namespace = _inline_namespace(
        "Prepare isolated media candidate snapshot and runtime", "PY_MEDIA_SDK_ACL"
    )
    sdk_root = tmp_path / "sdk"
    (sdk_root / "bin").mkdir(parents=True)
    binary = sdk_root / "bin/python3.12"
    binary.write_bytes(b"synthetic interpreter")
    namespace["SDK_ROOT"] = sdk_root
    fin_uid = os.getuid() + 100_000
    source_owner = os.getuid()

    if unsafe == "external_link":
        outside = tmp_path / "outside"
        outside.write_text("outside SDK", encoding="utf-8")
        (sdk_root / "escape").symlink_to(outside)
        with pytest.raises(ValueError, match="external symlink"):
            list(namespace["sdk_acl_nodes"](sdk_root, fin_uid, source_owner))
    else:
        original_lstat = Path.lstat

        class FinOwnedStat:
            def __init__(self, original: os.stat_result) -> None:
                self._original = original

            def __getattr__(self, name: str) -> object:
                if name == "st_uid":
                    return fin_uid
                return getattr(self._original, name)

        def lstat_with_fin_owner(path: Path) -> os.stat_result:
            info = original_lstat(path)
            if path == binary:
                return FinOwnedStat(info)  # type: ignore[return-value]
            return info

        monkeypatch.setattr(Path, "lstat", lstat_with_fin_owner)
        with pytest.raises(ValueError, match="trusted non-fin ownership"):
            list(namespace["sdk_acl_nodes"](sdk_root, fin_uid, source_owner))


def test_sdk_acl_refusal_writes_bounded_failure_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake ACL adapter proves refusal receipt behavior; native Linux ACL execution is NOT_RUN."""
    import json
    import os
    import pwd
    import sys
    from types import SimpleNamespace

    script = _inline_python(
        "Prepare isolated media candidate snapshot and runtime", "PY_MEDIA_SDK_ACL"
    )
    tree = ast.parse(script)
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    sdk_root.chmod(0o700)
    access_acl = _encode_synthetic_acl({(1, None): 7, (4, None): 0, (16, None): 0, (32, None): 0})
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if "SDK_ROOT" in names:
            node.value = ast.parse(f"Path({str(sdk_root)!r})", mode="eval").body
        elif "ANCESTORS" in names:
            node.value = ast.parse("()", mode="eval").body
    adapted_script = ast.unparse(ast.fix_missing_locations(tree))
    fin_uid = os.getuid() + 100_000

    def get_synthetic_xattr(
        path: str | Path, name: str, *, follow_symlinks: bool = False
    ) -> bytes | None:
        assert follow_symlinks is False
        if Path(path) == sdk_root and name == "system.posix_acl_access":
            return access_acl
        return None

    def no_acl_command(*args: object, **kwargs: object) -> None:
        raise AssertionError("The insufficient synthetic mask must fail before setfacl")

    monkeypatch.setattr(os, "getxattr", get_synthetic_xattr, raising=False)
    monkeypatch.setattr(subprocess, "run", no_acl_command)
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=fin_uid))
    monkeypatch.setattr(sys, "platform", "linux")
    receipt_path = tmp_path / "sdk-acl.json"
    monkeypatch.setattr(sys, "argv", ["PY_MEDIA_SDK_ACL", str(receipt_path), str(os.getuid())])

    with pytest.raises(SystemExit, match="Fin-specific SDK ACL restriction refused"):
        exec(compile(adapted_script, "<PY_MEDIA_SDK_ACL>", "exec"), {"__name__": "__main__"})

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "finance-media-ci-sdk-acl-v1"
    assert receipt["status"] == "REFUSED"
    assert receipt["failure_type"] == "SdkAclRefusal"
    assert "without expansion" in receipt["failure_reason"]
    assert len(receipt["failure_reason"]) <= 1024
    detail = receipt["failure_detail"]
    assert set(detail) == {"path", "path_truncated", "stage", "needed", "existing_mask"}
    assert detail["path"] == str(sdk_root)
    assert len(detail["path"]) <= 4096
    assert detail["path_truncated"] is False
    assert detail["stage"] == "preflight_mask"
    assert detail["needed"] == 5
    assert detail["existing_mask"] == 0
    assert receipt_path.stat().st_mode & 0o777 == 0o400
    assert receipt_path.stat().st_size <= 65_536


@pytest.mark.parametrize("failure", ["permission", "exit126", "timeout", "invalid_identity"])
def test_actual_startup_helper_records_bounded_refusal_for_bootstrap_failures(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise bounded receipts with a child-process adapter, never claim Linux admission."""
    import json
    import os
    import pwd
    import stat
    import sys
    import time
    from types import SimpleNamespace

    script = _inline_python(
        "Verify isolated media startup and prepare OCR assets", "PY_MEDIA_STARTUP"
    )
    tree = ast.parse(script)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "/tmp":
            node.value = "/private/tmp"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_startup_probe"
            and len(node.args) == 1
            and not node.keywords
        ):
            node.keywords.append(ast.keyword(arg="timeout", value=ast.Constant(value=0.05)))
    script = ast.unparse(ast.fix_missing_locations(tree))

    root = tmp_path / "media-root"
    root.mkdir()
    (root / "runtime.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "base_prefix": "/opt/hostedtoolcache/Python/3.12.14/x64",
                "selected_binary_sha256": "a" * 64,
                "selected_libpython_sha256": "c" * 64,
                "trusted_sdk_owners": [0, os.getuid()],
            }
        ),
        encoding="utf-8",
    )
    (root / "snapshot.json").write_text(json.dumps({"candidate_sha": "b" * 40}), encoding="utf-8")
    receipt_path = root / "startup.json"
    core_path = Path("/private/tmp") / f"finance-media-core-{os.getpid()}-{time.time_ns()}"
    original_popen = subprocess.Popen
    observed_arguments: list[str] = []

    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
    )

    def adapted_popen(arguments: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        observed_arguments.extend(arguments)
        assert "/usr/bin/env" in arguments
        assert "-i" in arguments
        assert f"FINANCE_RUNTIME_ROOT={core_path}" in arguments
        if failure == "permission":
            raise PermissionError("synthetic interpreter execute denial")
        command = (
            "import json; print(json.dumps({'admission':'REFUSED'}))"
            if failure == "invalid_identity"
            else ("import time; time.sleep(2)" if failure == "timeout" else "raise SystemExit(126)")
        )
        return original_popen([sys.executable, "-I", "-c", command], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", adapted_popen)
    monkeypatch.setattr(
        sys, "argv", ["PY_MEDIA_STARTUP", str(root), str(receipt_path), str(core_path)]
    )
    namespace: dict[str, object] = {"__name__": "__main__"}
    try:
        with pytest.raises(SystemExit):
            exec(compile(script, "<PY_MEDIA_STARTUP>", "exec"), namespace)

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["schema"] == "finance-media-ci-startup-v1"
        assert receipt["candidate_sha"] == "b" * 40
        assert receipt["status"] == "REFUSED"
        assert receipt["core_runtime"]["path"] == str(core_path)
        assert receipt["core_runtime"]["database_empty"] is True
        assert receipt_path.stat().st_mode & 0o777 == 0o400
        assert receipt_path.stat().st_size <= 393_216
        assert len(receipt.get("stdout", "")) <= 8192
        assert f"FINANCE_RUNTIME_ROOT={core_path}" in observed_arguments
        if failure == "permission":
            assert receipt["failure_type"] == "PermissionError"
        elif failure == "timeout":
            assert receipt["failure_type"] == "TimeoutError"
        elif failure == "exit126":
            assert receipt["returncode"] == 126
        else:
            assert "admitted identity" in receipt["failure_reason"]
    finally:
        if core_path.exists():
            database_path = core_path / "database"
            assert core_path.stat().st_uid == os.getuid()
            assert stat.S_IMODE(core_path.stat().st_mode) == 0o700
            assert database_path.stat().st_uid == os.getuid()
            assert stat.S_IMODE(database_path.stat().st_mode) == 0o700
            assert list(core_path.iterdir()) == [database_path]
            assert list(database_path.iterdir()) == []
            database_path.rmdir()
            core_path.rmdir()


def _media_proof_script() -> str:
    script = _step_script("Require actual Linux receipt media acceptance")
    return script.split("<<'PY_MEDIA_PROOF'\n", 1)[1].rsplit("PY_MEDIA_PROOF", 1)[0]


def _media_report(path: Path, *, totals: tuple[int, int, int, int], variation: str = "") -> None:
    import xml.etree.ElementTree as ET

    tree = ast.parse(_media_proof_script())
    ids = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "required_ids" for target in node.targets
        )
    )
    names = ["test_vector_archive_is_bounded_synthetic_and_hash_verified"] + [
        f"test_actual_linux_receipt_media_vector[{case}]" for case in ids
    ]
    assert len(names) == len(set(names)) == 50
    if variation == "duplicate":
        names[-1] = names[-2]
    elif variation == "missing":
        names.pop()
    elif variation == "unknown":
        names[-1] = "unrelated_padding_test"
    report = ET.Element("testsuites")
    suite = ET.SubElement(
        report,
        "testsuite",
        dict(zip(("tests", "skipped", "failures", "errors"), map(str, totals), strict=True)),
    )
    for name in names:
        case = ET.SubElement(suite, "testcase", name=name)
        if variation == "hidden_skip" and name == names[-1]:
            ET.SubElement(case, "skipped")
    ET.ElementTree(report).write(path, encoding="utf-8")


@pytest.mark.parametrize(
    ("totals", "variation", "expected_exit"),
    [
        ((49, 0, 0, 0), "", 1),
        ((50, 1, 0, 0), "", 1),
        ((50, 0, 1, 0), "", 1),
        ((50, 0, 0, 1), "", 1),
        ((50, 0, 0, 0), "duplicate", 1),
        ((50, 0, 0, 0), "missing", 1),
        ((50, 0, 0, 0), "unknown", 1),
        ((50, 0, 0, 0), "hidden_skip", 1),
        ((50, 0, 0, 0), "", 0),
    ],
)
def test_actual_media_proof_rejects_incomplete_or_skipped_inventory(
    tmp_path: Path, totals: tuple[int, int, int, int], variation: str, expected_exit: int
) -> None:
    import sys

    report = tmp_path / "proof.xml"
    _media_report(report, totals=totals, variation=variation)
    result = subprocess.run(
        [sys.executable, "-I", "-", str(report), str(os.getuid())],
        input=_media_proof_script(),
        text=True,
        capture_output=True,
    )
    assert result.returncode == expected_exit, result.stderr


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "oversize", "entity", "owner"])
def test_actual_media_report_refuses_unsafe_files(tmp_path: Path, unsafe: str) -> None:
    import sys

    report = tmp_path / "proof.xml"
    _media_report(report, totals=(50, 0, 0, 0))
    owner = os.getuid()
    if unsafe == "symlink":
        original = tmp_path / "original.xml"
        report.rename(original)
        report.symlink_to(original)
    elif unsafe == "hardlink":
        os.link(report, tmp_path / "alias.xml")
    elif unsafe == "oversize":
        report.write_bytes(b"x" * 2_097_153)
    elif unsafe == "entity":
        report.write_bytes(
            b'<!DOCTYPE testsuites [<!ENTITY injected "unsafe">]>' + report.read_bytes()
        )
    else:
        owner += 1
    result = subprocess.run(
        [sys.executable, "-I", "-", str(report), str(owner)],
        input=_media_proof_script(),
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0


def _prepare_export_core_runtime(core_path: Path) -> dict[str, Any]:
    """Run the workflow's real exclusive Core-root creator with a local UID adapter."""
    import re
    import stat

    startup = ast.parse(
        _inline_python("Verify isolated media startup and prepare OCR assets", "PY_MEDIA_STARTUP")
    )
    definitions: list[ast.stmt] = []
    for node in startup.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "directory_identity",
            "prepare_core_runtime",
        }:
            definitions.append(node)
    for ast_node in ast.walk(ast.Module(body=definitions, type_ignores=[])):
        if isinstance(ast_node, ast.Constant) and ast_node.value == "/tmp":
            ast_node.value = "/private/tmp"
    namespace: dict[str, object] = {"Path": Path, "os": os, "re": re, "stat": stat}
    module = ast.Module(body=definitions, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<PY_MEDIA_CORE_SETUP>", "exec"), namespace)
    prepare = cast(Any, namespace["prepare_core_runtime"])
    return cast(dict[str, Any], prepare(core_path, os.getuid(), os.getgid()))


@pytest.mark.parametrize(
    "variant",
    [
        "regular",
        "file_link",
        "directory_link",
        "hardlink",
        "oversize",
        "survivor",
        "controls",
        "control_symlink",
        "control_mode",
        "control_oversize",
        "core_content",
        "core_custody",
    ],
)
def test_media_export_quiescent_copy_and_unsafe_refusal(tmp_path: Path, variant: str) -> None:
    """Exercise bounded export with local UID/proc adapters, never Linux admission."""
    import hashlib
    import json
    import stat
    import sys
    import time

    source = tmp_path / "work"
    source.mkdir(mode=0o700)
    evidence = source / "evidence"
    evidence.mkdir(mode=0o700)
    raw = b"immutable synthetic original\x00"
    member = evidence / "original.bin"
    member.write_bytes(raw)
    member.chmod(0o400)
    core_path = Path("/private/tmp") / f"finance-media-core-{os.getpid()}-{time.time_ns()}"
    core_runtime = _prepare_export_core_runtime(core_path)
    control_payloads = {
        "snapshot.json": b'{"schema":"finance-media-ci-snapshot-v1","status":"REFUSED"}\n',
        "sdk-acl.json": b'{"schema":"finance-media-ci-sdk-acl-v1","status":"REFUSED"}\n',
        "runtime.json": b'{"schema":"finance-media-ci-runtime-v1","status":"REFUSED"}\n',
        "startup.json": (
            json.dumps(
                {
                    "schema": "finance-media-ci-startup-v1",
                    "status": "REFUSED",
                    "core_runtime": core_runtime,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    }
    if variant in {"survivor", "controls", "control_symlink", "control_mode", "control_oversize"}:
        for name, payload in control_payloads.items():
            control = source.parent / name
            control.write_bytes(payload)
            control.chmod(0o400)
        if variant == "control_symlink":
            target = source.parent / "snapshot-target.json"
            target.write_bytes(control_payloads["snapshot.json"])
            target.chmod(0o400)
            (source.parent / "snapshot.json").unlink()
            (source.parent / "snapshot.json").symlink_to(target)
        elif variant == "control_mode":
            (source.parent / "snapshot.json").chmod(0o600)
        elif variant == "control_oversize":
            (source.parent / "snapshot.json").chmod(0o600)
            (source.parent / "snapshot.json").write_bytes(b"x" * 65_537)
            (source.parent / "snapshot.json").chmod(0o400)
    else:
        startup_control = source.parent / "startup.json"
        startup_control.write_bytes(control_payloads["startup.json"])
        startup_control.chmod(0o400)
    if variant == "core_content":
        unexpected = core_path / "database" / "synthetic-unexpected"
        unexpected.write_bytes(b"synthetic residue")
        unexpected.chmod(0o400)
    elif variant == "core_custody":
        (core_path / "database").chmod(0o750)
    proc = tmp_path / "synthetic-proc"
    proc.mkdir()
    if variant == "survivor":
        process = proc / "123"
        process.mkdir()
        (process / "status").write_text(
            f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n"
        )
    elif variant == "file_link":
        member.unlink()
        member.symlink_to(tmp_path / "unrelated")
    elif variant == "directory_link":
        member.unlink()
        evidence.rmdir()
        evidence.symlink_to(proc, target_is_directory=True)
    elif variant == "hardlink":
        os.link(member, evidence / "alias.bin")
    elif variant == "oversize":
        member.chmod(0o600)
        with member.open("wb") as output:
            output.truncate(20_000_001)
        member.chmod(0o400)

    shell = _step_script("Preserve actual Linux receipt media acceptance evidence")
    export = shell.split("<<'PY_MEDIA_EXPORT'\n", 1)[1].rsplit("PY_MEDIA_EXPORT", 1)[0]
    tree = ast.parse(export)
    # Adapt only OS admission observations to an unprivileged synthetic test host.
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def enclosing_function(node: ast.AST) -> str | None:
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent.name
            parent = parents.get(parent)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "/proc":
            node.value = str(proc)
        if isinstance(node, ast.Constant) and node.value == "/tmp":
            node.value = "/private/tmp"
        if not (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and isinstance(node.left.value, ast.Name)
            and node.left.attr == "st_uid"
        ):
            continue
        if node.left.value.id == "parent_info" and enclosing_function(node) is None:
            node.comparators = [ast.Constant(value=os.getuid())]
        elif node.left.value.id == "before" and enclosing_function(node) == "copy_control_receipt":
            node.comparators = [ast.Constant(value=os.getuid())]
    export_state_writer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "write_export_state"
    )
    write_call_index = next(
        index
        for index, node in enumerate(export_state_writer.body)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "cleanup"
        and node.value.func.attr == "write_text"
    )
    # The hosted exporter is root and can refresh its fin-owned 0400 state file;
    # this adapter opens it before each rewrite, then the real helper seals it again.
    export_state_writer.body.insert(
        write_call_index,
        ast.If(
            test=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="cleanup", ctx=ast.Load()), attr="exists", ctx=ast.Load()
                ),
                args=[],
                keywords=[],
            ),
            body=[
                ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="cleanup", ctx=ast.Load()),
                            attr="chmod",
                            ctx=ast.Load(),
                        ),
                        args=[ast.Constant(value=0o600)],
                        keywords=[],
                    )
                )
            ],
            orelse=[],
        ),
    )
    adapter = (
        "import pwd, types, os\n"
        "pwd.getpwnam = lambda _: types.SimpleNamespace(pw_uid=os.getuid())\n"
    )
    destination = tmp_path / "export"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-",
            str(source),
            str(destination),
            str(os.getuid()),
            str(os.getgid()),
            str(core_path),
        ],
        input=adapter + ast.unparse(ast.fix_missing_locations(tree)),
        text=True,
        capture_output=True,
    )
    try:
        if variant in {"regular", "controls"}:
            assert result.returncode == 0, result.stderr
            copied = destination / "evidence/original.bin"
            assert copied.read_bytes() == member.read_bytes() == raw
            assert hashlib.sha256(copied.read_bytes()).digest() == hashlib.sha256(raw).digest()
            assert copied.stat().st_mode & 0o777 == member.stat().st_mode & 0o777 == 0o400
            export_state = json.loads((destination / "export-state.json").read_text())
            assert (destination / "export-state.json").stat().st_mode & 0o777 == 0o400
            assert export_state["core_runtime_postcondition"]["status"] == "PASS_REMOVED"
            assert export_state["core_runtime_postcondition"]["only_empty_database"] is True
            assert not core_path.exists()
            if variant == "controls":
                for name, payload in control_payloads.items():
                    exported = destination / ("bootstrap-" + name)
                    assert exported.read_bytes() == payload
                    assert exported.stat().st_mode & 0o777 == 0o400
        elif variant == "survivor":
            assert result.returncode != 0
            for name, payload in control_payloads.items():
                exported = destination / ("bootstrap-" + name)
                assert exported.read_bytes() == payload
                assert exported.stat().st_mode & 0o777 == 0o400
            assert not (destination / "evidence/original.bin").exists()
            assert core_path.exists()
            export_state = json.loads((destination / "export-state.json").read_text())
            assert export_state["core_runtime_postcondition"]["status"] == "NOT_CHECKED"
        elif variant in {"core_content", "core_custody"}:
            assert result.returncode != 0, result.stderr
            copied = destination / "evidence/original.bin"
            assert copied.read_bytes() == raw
            export_state = json.loads((destination / "export-state.json").read_text())
            assert (destination / "export-state.json").stat().st_mode & 0o777 == 0o400
            assert export_state["core_runtime_postcondition"]["status"] == "REFUSED"
            assert len(export_state["core_runtime_postcondition"]["failure_reason"]) <= 1024
            assert core_path.exists()
        else:
            assert result.returncode != 0
            if variant.startswith("control_"):
                assert not (destination / "bootstrap-snapshot.json").exists()
            assert not (destination / "evidence/original.bin").exists()
    finally:
        if core_path.exists():
            database_path = core_path / "database"
            if variant == "core_content":
                unexpected = database_path / "synthetic-unexpected"
                assert unexpected.is_file() and unexpected.stat().st_uid == os.getuid()
                unexpected.unlink()
            if variant == "core_custody":
                database_path.chmod(0o700)
            assert core_path.stat().st_uid == os.getuid()
            assert stat.S_IMODE(core_path.stat().st_mode) == 0o700
            assert database_path.stat().st_uid == os.getuid()
            assert stat.S_IMODE(database_path.stat().st_mode) == 0o700
            assert list(core_path.iterdir()) == [database_path]
            assert list(database_path.iterdir()) == []
            database_path.rmdir()
            core_path.rmdir()
