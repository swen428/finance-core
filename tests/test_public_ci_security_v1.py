from __future__ import annotations

import ast
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
    section = source.split("- name: Prepare isolated media acceptance account", 1)[1].split(
        "- name: Set up exact Node runtime", 1
    )[0]
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
    assert "code_read_only" in section
    assert '"CapEff", "CapPrm"' in section
    assert 'item["current"] >= 16' in section
    assert "remaining_uid_processes" in section
    assert "os.O_NOFOLLOW" in section
    assert "visited > 4096" in section
    for label in ("PY_MEDIA_LAUNCH", "PY_MEDIA_PROOF", "PY_MEDIA_EXPORT"):
        step = (
            "Preserve actual Linux receipt media acceptance evidence"
            if label.endswith("EXPORT")
            else "Require actual Linux receipt media acceptance"
        )
        script = _step_script(step)
        ast.parse(script.split("<<'" + label + "'\n", 1)[1].rsplit(label, 1)[0])
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


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


@pytest.mark.parametrize(
    "variant", ["regular", "file_link", "directory_link", "hardlink", "oversize", "survivor"]
)
def test_media_export_quiescent_copy_and_unsafe_refusal(tmp_path: Path, variant: str) -> None:
    """Exercise export bytes with a synthetic UID/proc adapter, never native Linux admission."""
    import hashlib
    import sys

    source = tmp_path / "work"
    source.mkdir(mode=0o700)
    evidence = source / "evidence"
    evidence.mkdir(mode=0o700)
    raw = b"immutable synthetic original\x00"
    member = evidence / "original.bin"
    member.write_bytes(raw)
    member.chmod(0o400)
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "/proc":
            node.value = str(proc)
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id == "parent_info"
            and node.left.attr == "st_uid"
        ):
            node.comparators = [ast.Constant(value=os.getuid())]
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
        ],
        input=adapter + ast.unparse(ast.fix_missing_locations(tree)),
        text=True,
        capture_output=True,
    )
    if variant == "regular":
        assert result.returncode == 0, result.stderr
        copied = destination / "evidence/original.bin"
        assert copied.read_bytes() == member.read_bytes() == raw
        assert hashlib.sha256(copied.read_bytes()).digest() == hashlib.sha256(raw).digest()
        assert copied.stat().st_mode & 0o777 == member.stat().st_mode & 0o777 == 0o400
    else:
        assert result.returncode != 0
        assert not (destination / "evidence/original.bin").exists()
