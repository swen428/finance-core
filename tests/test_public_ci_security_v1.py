from __future__ import annotations

import re
import subprocess
from pathlib import Path

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
    assert "REVIEWED_CHECKOUT_SHA: ${{ github.sha }}" in source
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

    assert "needs: [classify, quality, pytest, bridge]" in source
    assert 'test "$PYTEST_RESULT" = success' in source
    assert 'test "$QUALITY_RESULT" = success' in source
    assert 'test "$BRIDGE_RESULT" = success || test "$BRIDGE_RESULT" = skipped' in source
