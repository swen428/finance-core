from __future__ import annotations

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
