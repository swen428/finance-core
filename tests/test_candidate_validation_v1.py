"""Candidate proof rejects stale identities and incomplete hosted evidence."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY = load("candidate_identity", ".github/scripts/candidate_identity.py")
VERIFIER = load("candidate_verifier", "scripts/verify_candidate_validation.py")
BASE, HEAD, EVENT, TREE = (char * 40 for char in "abce")
REPO = "example/finance-core"
WORKFLOW_BYTES = b"reviewed workflow fixture\n"


def sample(*, bridge: bool = True) -> dict:
    identity = {
        "schema": "finance-candidate-identity-v1",
        "repository": REPO,
        "event": "pull_request",
        "run_id": 42,
        "run_attempt": 1,
        "pr_number": 7,
        "base_sha": BASE,
        "head_sha": HEAD,
        "tested_sha": HEAD,
        "tested_tree": TREE,
        "event_sha": EVENT,
        "workflow_sha": EVENT,
        "workflow_ref": f"{REPO}/{VERIFIER.WORKFLOW}@refs/pull/7/merge",
        "workflow_path": VERIFIER.WORKFLOW,
        "workflow_sha256": hashlib.sha256(WORKFLOW_BYTES).hexdigest(),
    }
    results = {name: "success" for name in ("classify", "quality", "pytest", "pytest-report")}
    results["bridge"] = "success" if bridge else "skipped"
    names = VERIFIER.CORE_JOBS | (VERIFIER.BRIDGE_JOBS if bridge else {"bridge"})
    jobs = [
        {
            "id": index + 100,
            "name": name,
            "run_id": 42,
            "run_attempt": 1,
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "skipped" if name == "bridge" else "success",
        }
        for index, name in enumerate(sorted(names))
    ]
    workflow = {"content": base64.b64encode(WORKFLOW_BYTES).decode()}
    return {
        "pr": {
            "number": 7,
            "state": "open",
            "head": {"sha": HEAD},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPO}},
        },
        "main": {"sha": BASE},
        "run": {
            "id": 42,
            "repository": {"full_name": REPO},
            "event": "pull_request",
            "head_sha": HEAD,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "workflow_id": 123,
            "path": VERIFIER.WORKFLOW,
        },
        "workflow": {"id": 123, "path": VERIFIER.WORKFLOW, "state": "active"},
        "proof": {
            "schema": "finance-candidate-validation-v1",
            "identity": identity,
            "bridge_required": bridge,
            "results": results,
        },
        "head_commit": {"sha": HEAD, "tree": {"sha": TREE}},
        "event_commit": {
            "sha": EVENT,
            "parents": [{"sha": BASE}, {"sha": HEAD}],
            "tree": {"sha": TREE},
        },
        "comparison": {
            "merge_base_commit": {"sha": BASE},
            "files": [
                {"filename": "finance_core/application/review.py" if bridge else "README.md"}
            ],
        },
        "head_workflow": workflow,
        "executed_workflow": copy.deepcopy(workflow),
        "jobs": {"total_count": len(jobs), "jobs": jobs},
        "artifact": {
            "id": 456,
            "name": "candidate-validation-v1-42-1",
            "expired": False,
            "digest": "sha256:" + "f" * 64,
            "workflow_run": {"id": 42, "head_sha": HEAD},
        },
        "artifact_sha256": "f" * 64,
    }


def verify(evidence: dict):
    return VERIFIER.verify(evidence, repository=REPO, pr_number=7, base=BASE, head=HEAD)


@pytest.mark.parametrize("bridge", [False, True])
def test_complete_exact_candidate_evidence_passes(bridge: bool) -> None:
    result = verify(sample(bridge=bridge))
    assert result["status"] == "VERIFIED"
    assert result["base_sha"] == BASE and result["head_sha"] == HEAD
    assert result["tested_tree"] == TREE and result["bridge_required"] is bridge


def test_renaming_a_bridge_input_outside_its_directory_still_requires_bridge() -> None:
    evidence = sample()
    evidence["comparison"]["files"] = [
        {
            "filename": "archive/controller.ts",
            "previous_filename": "plugins/finance-bridge/src/controller.ts",
        }
    ]
    assert verify(evidence)["bridge_required"] is True
    evidence = sample(bridge=False)
    evidence["comparison"]["files"] = [
        {
            "filename": "archive/controller.ts",
            "previous_filename": "plugins/finance-bridge/src/controller.ts",
        }
    ]
    with pytest.raises(ValueError, match="Bridge requirement"):
        verify(evidence)


@pytest.mark.parametrize(
    "path,value",
    [
        (("pr", "head", "sha"), "d" * 40),
        (("pr", "base", "sha"), "d" * 40),
        (("main", "sha"), "d" * 40),
        (("pr", "state"), "closed"),
        (("pr", "base", "repo", "full_name"), "elsewhere/repo"),
        (("run", "event"), "workflow_dispatch"),
        (("run", "head_sha"), EVENT),
        (("run", "run_attempt"), 2),
        (("run", "conclusion"), "cancelled"),
        (("run", "workflow_id"), 999),
        (("run", "repository", "full_name"), "elsewhere/repo"),
        (("proof", "identity", "tested_sha"), EVENT),
        (("proof", "identity", "tested_tree"), "d" * 40),
        (("proof", "identity", "run_id"), 999),
        (("proof", "identity", "base_sha"), "d" * 40),
        (("proof", "identity", "workflow_ref"), "wrong-ref"),
        (("proof", "identity", "workflow_sha"), "d" * 40),
        (("proof", "identity", "workflow_sha256"), "0" * 64),
        (("proof", "results", "pytest-report"), "failure"),
        (("proof", "results", "bridge"), "skipped"),
        (("proof", "bridge_required"), False),
        (("proof", "bridge_required"), 1),
        (("event_commit", "tree", "sha"), "d" * 40),
        (("event_commit", "parents", 0, "sha"), "d" * 40),
        (("comparison", "merge_base_commit", "sha"), "d" * 40),
        (("executed_workflow", "content"), base64.b64encode(b"different").decode()),
        (("artifact", "workflow_run", "id"), 999),
        (("artifact", "workflow_run", "head_sha"), "d" * 40),
        (("artifact", "digest"), "sha256:" + "0" * 64),
        (("artifact", "expired"), True),
        (("artifact", "name"), "candidate-validation-v1-41-1"),
    ],
)
def test_stale_or_mismatched_identity_is_rejected(path: tuple, value: object) -> None:
    evidence = sample()
    target = evidence
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        verify(evidence)


@pytest.mark.parametrize("name", sorted(VERIFIER.CORE_JOBS | VERIFIER.BRIDGE_JOBS))
@pytest.mark.parametrize("failure", ["missing", "failure", "cancelled", "skipped", "foreign-run"])
def test_every_required_job_must_belong_to_successful_exact_run(name: str, failure: str) -> None:
    evidence = sample()
    jobs = evidence["jobs"]["jobs"]
    job = next(job for job in jobs if job["name"] == name)
    if failure == "missing":
        jobs.remove(job)
        evidence["jobs"]["total_count"] -= 1
    elif failure == "foreign-run":
        job["run_id"] = 41
    else:
        job["conclusion"] = failure
    with pytest.raises(ValueError):
        verify(evidence)


def test_duplicate_or_truncated_job_and_comparison_inventory_rejected() -> None:
    for kind in ("duplicate", "truncated", "paths"):
        evidence = sample()
        if kind == "duplicate":
            evidence["jobs"]["jobs"].append(evidence["jobs"]["jobs"][0])
            evidence["jobs"]["total_count"] += 1
        elif kind == "truncated":
            evidence["jobs"]["total_count"] += 1
        else:
            evidence["comparison"]["files"] *= 300
        with pytest.raises(ValueError):
            verify(evidence)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request"])
def test_summary_identity_and_aggregate_contract(event: str) -> None:
    record = sample()["proof"]["identity"]
    record["event"] = event
    needs = {
        name: {"result": "success"}
        for name in ("classify", "quality", "pytest", "bridge", "pytest-report")
    }
    needs["classify"]["outputs"] = {"identity": json.dumps(record), "bridge": "true"}
    assert IDENTITY.summarize(record, needs)["bridge_required"] is True
    bad = copy.deepcopy(needs)
    bad["classify"]["outputs"]["identity"] = json.dumps({**record, "tested_sha": EVENT})
    with pytest.raises(ValueError):
        IDENTITY.summarize(record, bad)
    for name in needs:
        bad = copy.deepcopy(needs)
        bad[name]["result"] = "cancelled"
        with pytest.raises(ValueError):
            IDENTITY.summarize(record, bad)
    needs["classify"]["outputs"]["bridge"] = "false"
    needs["bridge"]["result"] = "skipped"
    if event == "pull_request":
        assert IDENTITY.summarize(record, needs)["bridge_required"] is False
    else:
        with pytest.raises(ValueError):
            IDENTITY.summarize(record, needs)


def test_identity_reads_actual_checkout_and_refuses_wrong_or_dirty_head(
    tmp_path: Path, monkeypatch
) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "--quiet")
    workflow = tmp_path / VERIFIER.WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(WORKFLOW_BYTES)
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    base = git("rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("candidate")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "head",
    )
    head = git("rev-parse", "HEAD")
    env = {
        "CANDIDATE_SHA": head,
        "CANDIDATE_BASE_SHA": base,
        "GITHUB_SHA": EVENT,
        "GITHUB_WORKFLOW_SHA": head,
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": f"{REPO}/{VERIFIER.WORKFLOW}@refs/pull/7/merge",
        "CANDIDATE_PR_NUMBER": "7",
    }
    monkeypatch.chdir(tmp_path)
    actual = IDENTITY.identity(env)
    assert actual["tested_sha"] == head and actual["tested_tree"] == git("rev-parse", "HEAD^{tree}")
    with pytest.raises(ValueError, match="candidate head"):
        IDENTITY.identity({**env, "CANDIDATE_SHA": base})
    workflow.write_text("unreviewed")
    with pytest.raises(ValueError, match="tracked files changed"):
        IDENTITY.identity(env)


@pytest.mark.parametrize(
    "members",
    [
        ["../proof.json"],
        ["candidate-validation-v1.json", "extra"],
        ["candidate-validation-v1.json"],
    ],
)
def test_archive_members_and_size_are_bounded(members: list[str]) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in members:
            archive.writestr(name, json.dumps(sample()["proof"]))
    if members == ["candidate-validation-v1.json"]:
        assert VERIFIER.unpack_proof(out.getvalue())["schema"] == "finance-candidate-validation-v1"
    else:
        with pytest.raises(ValueError):
            VERIFIER.unpack_proof(out.getvalue())
    with pytest.raises(ValueError):
        VERIFIER.unpack_proof(b"x" * (VERIFIER.MAX_ARTIFACT_BYTES + 1))


def test_every_checkout_and_build_reference_is_the_candidate_head() -> None:
    source = (ROOT / VERIFIER.WORKFLOW).read_text()
    assert source.count(
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
    ) == source.count("uses: actions/checkout@")
    assert source.count("Verify candidate checkout identity") == 5
    assert "Bind candidate validation evidence" in source
    assert (
        "REVIEWED_CHECKOUT_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in source
    )
    assert "shard: [0, 1, 2, 3]" in source
    assert "os: [ubuntu-latest, macos-15]" in source
    assert set(VERIFIER.CORE_JOBS | VERIFIER.BRIDGE_JOBS) == {
        "classify",
        "quality",
        "pytest (0)",
        "pytest (1)",
        "pytest (2)",
        "pytest (3)",
        "bridge (ubuntu-latest)",
        "bridge (macos-15)",
        "pytest-report",
        "validate",
    }


def test_collector_binds_api_inventory_archive_and_latest_pr_state(monkeypatch) -> None:
    fixture = sample()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("candidate-validation-v1.json", json.dumps(fixture["proof"]))
    payload = archive.getvalue()
    fixture["artifact"]["size_in_bytes"] = len(payload)
    fixture["artifact"]["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    prefix = f"repos/{REPO}"
    replies = {
        f"{prefix}/actions/runs/42": fixture["run"],
        f"{prefix}/actions/runs/42/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [fixture["artifact"]],
        },
        f"{prefix}/actions/workflows/validate.yml": fixture["workflow"],
        f"{prefix}/actions/runs/42/attempts/1/jobs?per_page=100": fixture["jobs"],
        f"{prefix}/git/commits/{HEAD}": fixture["head_commit"],
        f"{prefix}/git/commits/{EVENT}": fixture["event_commit"],
        f"{prefix}/compare/{BASE}...{HEAD}?per_page=1": fixture["comparison"],
        f"{prefix}/contents/{VERIFIER.WORKFLOW}?ref={HEAD}": fixture["head_workflow"],
        f"{prefix}/contents/{VERIFIER.WORKFLOW}?ref={EVENT}": fixture["executed_workflow"],
        f"{prefix}/pulls/7": fixture["pr"],
        f"{prefix}/commits/main": fixture["main"],
    }
    observed = []

    def api(endpoint):
        observed.append(endpoint)
        return copy.deepcopy(replies[endpoint])

    def download(command):
        assert command == ["gh", "api", f"{prefix}/actions/artifacts/456/zip"]
        return payload

    monkeypatch.setattr(VERIFIER, "api", api)
    monkeypatch.setattr(VERIFIER.subprocess, "check_output", download)
    evidence = VERIFIER.collect(REPO, 7, 42)
    assert verify(evidence)["status"] == "VERIFIED"
    assert observed[-2:] == [f"{prefix}/pulls/7", f"{prefix}/commits/main"]
    replies[f"{prefix}/commits/main"] = {"sha": "d" * 40}
    with pytest.raises(ValueError, match="base/main drifted"):
        verify(VERIFIER.collect(REPO, 7, 42))
