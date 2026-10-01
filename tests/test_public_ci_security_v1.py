from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

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


def test_linux_ocr_diagnostic_is_manual_quality_only_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    normal_guard = (
        "github.event_name != 'workflow_dispatch' || inputs.d4_linux_ocr_diagnostic != true"
    )
    diagnostic_guard = (
        "github.event_name == 'workflow_dispatch' && inputs.d4_linux_ocr_diagnostic == true"
    )
    jobs = source.split("\njobs:\n", 1)[1]
    job_names = re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", jobs, re.MULTILINE)
    assert job_names == ["classify", "quality", "pytest", "bridge", "pytest-report", "validate"]
    assert "shard: [0, 1, 2, 3]" in jobs
    assert "os: [ubuntu-latest, macos-15]" in jobs
    assert "runner: ubuntu-24.04" in jobs

    assert (
        "d4_linux_ocr_diagnostic:\n"
        '        description: "Run the bounded synthetic Ubuntu Linux OCR diagnostic only"\n'
        "        required: false\n"
        "        type: boolean\n"
        "        default: false"
    ) in source
    assert (
        "group: finance-core-${{ github.workflow }}-${{ github.ref }}"
        "${{ github.event_name == 'workflow_dispatch' && inputs.d4_linux_ocr_diagnostic == true "
        "&& '-linux-ocr-diagnostic' || '' }}"
    ) in source
    cancellation_guard = (
        "cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' || "
        "inputs.d4_linux_ocr_diagnostic != true }}"
    )
    assert cancellation_guard in source
    for event_name, diagnostic_input, sqlite_f0_probe, expected_cancellation in [
        ("push", False, False, True),
        ("pull_request", True, False, True),
        ("workflow_dispatch", False, False, True),  # default/manual stays cancellable
        ("workflow_dispatch", False, True, True),  # F0-only stays cancellable
        ("workflow_dispatch", True, False, False),  # isolated OCR diagnostic is not cancelled
    ]:
        assert isinstance(sqlite_f0_probe, bool)
        actual_cancellation = event_name != "workflow_dispatch" or diagnostic_input is not True
        assert actual_cancellation is expected_cancellation

    classify = jobs.split("  classify:\n", 1)[1].split("\n  quality:\n", 1)[0]
    pytest_job = jobs.split("  pytest:\n", 1)[1].split("\n  bridge:\n", 1)[0]
    bridge = jobs.split("  bridge:\n", 1)[1].split("\n  pytest-report:\n", 1)[0]
    report = jobs.split("  pytest-report:\n", 1)[1].split("\n  validate:\n", 1)[0]
    validate = jobs.split("  validate:\n", 1)[1]
    assert normal_guard in classify
    assert normal_guard in pytest_job
    assert ("needs.classify.outputs.bridge == 'true' && (" + normal_guard + ")") in bridge
    assert "always() && (" + normal_guard + ")" in report
    assert "always() && (" + normal_guard + ")" in validate

    quality = jobs.split("  quality:\n", 1)[1].split("\n  pytest:\n", 1)[0]
    assert "timeout-minutes: 15" in quality
    assert (
        "runs-on: ${{ github.event_name == 'workflow_dispatch' && "
        "inputs.d4_linux_ocr_diagnostic == true && 'ubuntu-24.04' || 'ubuntu-latest' }}"
    ) in quality
    assert "name: Verify candidate checkout identity" in quality
    assert "name: Install locked development dependencies" in quality
    assert "name: Initialize bounded Linux OCR diagnostic evidence" in quality
    assert 'mkdir -m 700 -p "$RUNNER_TEMP/d4-linux-ocr-diagnostic"' in quality
    assert 'chmod 600 "$RUNNER_TEMP/d4-linux-ocr-diagnostic/candidate-identity.json"' in quality
    quality_checks = quality.split("- name: Run Python quality checks\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    assert normal_guard in quality_checks
    assert diagnostic_guard in quality
    assert 'FINANCE_LINUX_OCR_REQUIRED: "1"' in quality
    assert "FINANCE_LINUX_OCR_CONFIG: ${{ runner.temp }}/linux-ocr/ocr_engine.json" in quality
    assert "FINANCE_LINUX_OCR_DIAGNOSTIC_DIR: ${{ runner.temp }}/d4-linux-ocr-diagnostic" in quality
    assert "scripts/prepare_linux_ocr.py" in quality
    assert '--junitxml="$RUNNER_TEMP/linux-ocr-diagnostic.xml"' in quality
    assert 'totals["tests"] != 13' in quality

    artifact = quality.split("- name: Preserve bounded Linux OCR diagnostic evidence\n", 1)[
        1
    ].split("\n      - name:", 1)[0]
    assert "always() && " + diagnostic_guard in artifact
    assert "retention-days: 30" in artifact
    assert "candidate-validation" not in artifact
    assert "linux-ocr-diagnostic.xml" in artifact
    assert "preparation-receipt.json" in artifact
    assert "linux-ocr-preparation-failure.json" in artifact
    sqlite_upload = quality.split("- name: Upload synthetic diagnostic evidence\n", 1)[1]
    assert "inputs.d4_linux_ocr_diagnostic != true" in sqlite_upload

    conflict_script = _step_script("Reject simultaneous diagnostic modes")
    for linux, sqlite, accepted in [
        ("false", "false", True),
        ("true", "false", True),
        ("false", "true", True),
        ("true", "true", False),
    ]:
        completed = subprocess.run(
            ["bash", "-c", conflict_script],
            env={
                "EVENT_NAME": "workflow_dispatch",
                "LINUX_OCR_DIAGNOSTIC": linux,
                "SQLITE_F0_PROBE": sqlite,
            },
            capture_output=True,
            text=True,
        )
        assert (completed.returncode == 0) is accepted

    from tests import test_linux_receipt_ocr_acceptance_v1 as linux_ocr

    payload = {
        "schema": "finance-linux-ocr-synthetic-diagnostic-v1",
        "fixture": {"name": "synthetic_mixed_receipt.jpg"},
        "source": {"sha256": "a" * 64},
        "run": {"event_name": None},
        "engine": {"name": "tesseract_tsv"},
        "extraction": {"status": "succeeded"},
        "stored_ocr_hierarchy": [],
        "loader_view": [],
        "parser_grouped_lines": [],
        "proposal": {"amount": None, "currency": None, "ambiguity_flags": []},
        "counts": {"capture": {}, "extractions": 0, "proposals": 0, "final_facts": {}},
    }
    output_dir = tmp_path / "diagnostic"
    monkeypatch.setenv("FINANCE_LINUX_OCR_DIAGNOSTIC_DIR", str(output_dir))
    linux_ocr._write_diagnostic_artifact("synthetic_mixed_receipt.jpg", payload)
    artifact_path = output_dir / "mixed-jpeg.json"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == payload
    assert artifact_path.stat().st_mode & 0o077 == 0

    oversized_dir = tmp_path / "oversized"
    monkeypatch.setenv("FINANCE_LINUX_OCR_DIAGNOSTIC_DIR", str(oversized_dir))
    payload["stored_ocr_hierarchy"] = [{"text": "x" * 1024}] * 600
    with pytest.raises(AssertionError, match="file-size bound"):
        linux_ocr._write_diagnostic_artifact("synthetic_mixed_receipt.jpg", payload)
    assert not oversized_dir.exists()

    monkeypatch.setenv(
        "FINANCE_LINUX_OCR_DIAGNOSTIC_DIR",
        str(linux_ocr.REPOSITORY_ROOT / "diagnostic"),
    )
    payload["stored_ocr_hierarchy"] = []
    with pytest.raises(AssertionError, match="outside the checkout"):
        linux_ocr._write_diagnostic_artifact("synthetic_mixed_receipt.jpg", payload)
    payload["source"]["database_path"] = "/synthetic/path"
    with pytest.raises(AssertionError, match="prohibited field"):
        linux_ocr._write_diagnostic_artifact("synthetic_mixed_receipt.jpg", payload)


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


def test_actual_scope_script_keeps_renamed_bridge_input_in_scope(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    old = tmp_path / "plugins/finance-bridge/src/controller.ts"
    old.parent.mkdir(parents=True)
    old.write_text("export const marker = 1;\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    old.rename(tmp_path / "archived-controller.ts")
    git("add", "-A")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "rename")
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
    assert output.read_text(encoding="utf-8") == "bridge=true\n"


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
