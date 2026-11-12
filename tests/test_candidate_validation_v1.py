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
from typing import Any

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
AGE_RELEASE = "v1.3.2"
AGE_SOURCE_COMMIT = "b74dce4cdbe35b5e5f66c06d9612b72f89028758"


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    )


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def acceptance_fixture(identity):
    payloads = {
        "finance_core-0.1.7-py3-none-any.whl": b"synthetic wheel bytes",
        "finance_core-0.1.7.tar.gz": b"synthetic sdist bytes",
    }
    distribution_records = {
        name: {"size_bytes": len(body), "sha256": digest(body)} for name, body in payloads.items()
    }
    inventory = {
        "schema": "finance-candidate-source-inventory-v1",
        "identity": identity,
        "files": [],
    }
    inventory_bytes = canonical(inventory)
    recipe = {"command": ["fixture"]}
    receipt = {
        "schema": "finance-candidate-package-v1",
        "status": "BUILT_NOT_VALIDATED",
        "identity": identity,
        "source_inventory_sha256": digest(inventory_bytes),
        "recipe": recipe,
        "recipe_sha256": digest(canonical(recipe)),
        "artifacts": distribution_records,
    }
    receipt_bytes = canonical(receipt)
    package_archive = io.BytesIO()
    with zipfile.ZipFile(package_archive, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package-receipt.json", receipt_bytes)
        archive.writestr("source-inventory.json", inventory_bytes)
        for name, body in payloads.items():
            archive.writestr(name, body)
    package_zip = package_archive.getvalue()
    bundle_artifacts = {
        "wheel": {
            "filename": "finance_core-0.1.7-py3-none-any.whl",
            **distribution_records["finance_core-0.1.7-py3-none-any.whl"],
        },
        "sdist": {
            "filename": "finance_core-0.1.7.tar.gz",
            **distribution_records["finance_core-0.1.7.tar.gz"],
        },
    }
    native_checks = {name: True for name in VERIFIER.NATIVE_CHECKS}
    installed_checks = {name: True for name in VERIFIER.INSTALLED_CHECKS}
    qualification = {
        "release": AGE_RELEASE,
        "source_commit": AGE_SOURCE_COMMIT,
        "platform": "linux-x86_64",
        "source_authentication": {
            "status": "PREQUALIFIED_SIGSUM_PROOF_REUSED",
            "source_commit": AGE_SOURCE_COMMIT,
            "archive_sha256": VERIFIER.AGE_SOURCE_ARCHIVE_SHA256,
            "proof_sha256": VERIFIER.AGE_SOURCE_PROOF_SHA256,
            "policy": "sigsum-generic-2025-1",
            "verifier_version": "sigsum-verify (sigsum-go module) v0.13.1",
        },
        "archive": {
            "filename": VERIFIER.AGE_SOURCE_ARCHIVE,
            "size_bytes": 2048,
            "sha256": VERIFIER.AGE_SOURCE_ARCHIVE_SHA256,
        },
        "proof": {
            "filename": VERIFIER.AGE_SOURCE_ARCHIVE + ".proof",
            "size_bytes": 128,
            "sha256": VERIFIER.AGE_SOURCE_PROOF_SHA256,
        },
        "tools": {
            name: {
                "path": f"/tmp/age/{'age-keygen' if name == 'age_keygen' else 'age'}",
                "sha256": pin[0],
                "size_bytes": pin[1],
                "version": AGE_RELEASE,
            }
            for name, pin in VERIFIER.AGE_TOOL_PINS.items()
        },
    }
    acceptance = {
        "schema": "finance-prepared-envelope-acceptance-v1",
        "status": "ACCEPTED",
        "identity": identity,
        "run_id": identity["run_id"],
        "run_attempt": identity["run_attempt"],
        "platform": "linux-x86_64",
        "qualification": qualification,
        "package": {
            "receipt_sha256": digest(receipt_bytes),
            "source_inventory_sha256": digest(inventory_bytes),
            "artifacts": bundle_artifacts,
        },
        "runtime_dependencies": {
            "pypdf": {"version": "6.21.4", "files_sha256": "1" * 64},
            "typing-extensions": {"version": "4.16.0", "files_sha256": "2" * 64},
        },
        "sdist_build_backend": {"version": "84.1.0", "files_sha256": "3" * 64},
        "native": {
            "checks": native_checks,
            "required": 7,
            "passed": 7,
            "skipped": 0,
            "failed": 0,
            "errors": 0,
        },
        "artifacts": {
            kind: {
                "checks": installed_checks,
                "required": 7,
                "passed": 7,
                "skipped": 0,
                "failed": 0,
                "errors": 0,
                "status": "PASS",
                "content_status": "CONTENT_VERIFIED_OFFLINE",
                "content_kind": "PREPARED_PROJECTION",
                "source_finalization": "NOT_ESTABLISHED",
                "source_checkout_visible": False,
                "native_checks": native_checks,
                "module_file": f"/tmp/{kind}/installed/finance_core/__init__.py",
                "frame_size_bytes": 9 * 1024 * 1024,
                "envelope_size_bytes": 2500000,
                "envelope_sha256": "4" * 64,
                "ciphertext_sha256": "5" * 64,
                "intent_sha256": "6" * 64,
                "inventory_rows_sha256": "7" * 64,
                "inventory_evidence_sha256": "8" * 64,
            }
            for kind in ("wheel", "sdist")
        },
        "totals": {"required": 21, "passed": 21, "skipped": 0, "failed": 0, "errors": 0},
    }
    acceptance_bytes = canonical(acceptance)
    acceptance_archive = io.BytesIO()
    with zipfile.ZipFile(acceptance_archive, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("prepared-envelope-acceptance-v1.json", acceptance_bytes)
    return {
        "package_zip": package_zip,
        "package_bundle": {
            "archive_sha256": digest(package_zip),
            "members": sorted({"package-receipt.json", "source-inventory.json", *payloads}),
            "receipt": receipt,
            "receipt_sha256": digest(receipt_bytes),
            "inventory_sha256": digest(inventory_bytes),
            "inventory": inventory,
            "artifacts": bundle_artifacts,
        },
        "acceptance": acceptance,
        "acceptance_file_sha256": digest(acceptance_bytes),
        "acceptance_zip": acceptance_archive.getvalue(),
    }


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
    results = {
        name: "success"
        for name in (
            "classify",
            "quality",
            "pytest",
            "pytest-report",
            "candidate-package",
        )
    }
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
            "steps": (
                [
                    {
                        "name": "Prepare pinned Linux age tools",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "Require installed wheel and sdist prepared-envelope acceptance",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "Upload prepared-envelope acceptance proof",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "Upload candidate build identity, not release approval",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ]
                if name == "candidate-package"
                else [
                    {
                        "name": "Prepare pinned Linux age tools for this shard",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
                if name.startswith("pytest (")
                else []
            ),
        }
        for index, name in enumerate(sorted(names))
    ]
    workflow = {"content": base64.b64encode(WORKFLOW_BYTES).decode()}
    bundle = acceptance_fixture(identity)
    proof = {
        "schema": "finance-candidate-validation-v2",
        "identity": identity,
        "bridge_required": bridge,
        "results": results,
        "package": {
            "artifact_id": 789,
            "artifact_name": "candidate-package-v1-42-1",
            "artifact_digest": "sha256:" + digest(bundle["package_zip"]),
            "receipt_sha256": bundle["package_bundle"]["receipt_sha256"],
        },
    }
    proof_archive = io.BytesIO()
    with zipfile.ZipFile(proof_archive, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("candidate-validation-v2.json", canonical(proof))
    proof_zip = proof_archive.getvalue()
    acceptance_zip = bundle["acceptance_zip"]
    proof_artifact = {
        "id": 456,
        "name": "candidate-validation-v2-42-1",
        "expired": False,
        "digest": "sha256:" + digest(proof_zip),
        "size_in_bytes": len(proof_zip),
        "workflow_run": {"id": 42, "run_attempt": 1, "head_sha": HEAD},
    }
    package_artifact = {
        "id": 789,
        "name": "candidate-package-v1-42-1",
        "digest": "sha256:" + digest(bundle["package_zip"]),
        "expired": False,
        "size_in_bytes": len(bundle["package_zip"]),
        "workflow_run": {"id": 42, "run_attempt": 1, "head_sha": HEAD},
    }
    acceptance_artifact = {
        "id": 790,
        "name": "prepared-envelope-acceptance-v1-42-1",
        "digest": "sha256:" + digest(acceptance_zip),
        "expired": False,
        "size_in_bytes": len(acceptance_zip),
        "workflow_run": {"id": 42, "run_attempt": 1, "head_sha": HEAD},
    }
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
        "proof": proof,
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
        "artifact": proof_artifact,
        "artifact_sha256": digest(proof_zip),
        "artifact_inventory": [
            copy.deepcopy(proof_artifact),
            copy.deepcopy(package_artifact),
            copy.deepcopy(acceptance_artifact),
        ],
        "artifact_total_count": 3,
        "package_artifact": package_artifact,
        "package_bundle": bundle["package_bundle"],
        "package_zip_payload": bundle["package_zip"],
        "acceptance_artifact": acceptance_artifact,
        "acceptance_artifact_sha256": digest(acceptance_zip),
        "acceptance_zip_payload": acceptance_zip,
        "acceptance": bundle["acceptance"],
        "acceptance_file_sha256": bundle["acceptance_file_sha256"],
    }


def verify(evidence: dict):
    return VERIFIER.verify(evidence, repository=REPO, pr_number=7, base=BASE, head=HEAD)


@pytest.mark.parametrize("bridge", [False, True])
def test_complete_exact_candidate_evidence_passes(bridge: bool) -> None:
    result = verify(sample(bridge=bridge))
    assert result["status"] == "VERIFIED"
    assert result["base_sha"] == BASE and result["head_sha"] == HEAD
    assert result["tested_tree"] == TREE and result["bridge_required"] is bridge


def test_optional_bridge_accepts_actual_skipped_matrix_job_name() -> None:
    evidence = sample(bridge=False)
    skipped = next(job for job in evidence["jobs"]["jobs"] if job["name"] == "bridge")
    skipped["name"] = "bridge (${{ matrix.os }})"
    assert verify(evidence)["bridge_required"] is False


def test_optional_bridge_preserves_existing_expanded_skipped_inventory() -> None:
    evidence = sample(bridge=False)
    skipped = next(job for job in evidence["jobs"]["jobs"] if job["name"] == "bridge")
    evidence["jobs"]["jobs"].remove(skipped)
    evidence["jobs"]["jobs"].extend(
        {**skipped, "id": 1000 + index, "name": name}
        for index, name in enumerate(sorted(VERIFIER.BRIDGE_JOBS))
    )
    evidence["jobs"]["total_count"] += 1
    assert verify(evidence)["bridge_required"] is False


@pytest.mark.parametrize("name", ["bridge (ubuntu-latest)", "bridge (unknown)"])
def test_optional_bridge_rejects_partial_or_unknown_job_name(name: str) -> None:
    evidence = sample(bridge=False)
    skipped = next(job for job in evidence["jobs"]["jobs"] if job["name"] == "bridge")
    skipped["name"] = name
    with pytest.raises(ValueError, match="optional job inventory mismatch"):
        verify(evidence)


def test_optional_bridge_matrix_job_must_be_skipped() -> None:
    evidence = sample(bridge=False)
    skipped = next(job for job in evidence["jobs"]["jobs"] if job["name"] == "bridge")
    skipped["name"] = "bridge (${{ matrix.os }})"
    skipped["conclusion"] = "success"
    with pytest.raises(ValueError, match="wrong or unsuccessful job evidence"):
        verify(evidence)


def test_required_bridge_rejects_unexpanded_matrix_job_name() -> None:
    evidence = sample(bridge=True)
    bridge = next(job for job in evidence["jobs"]["jobs"] if job["name"] in VERIFIER.BRIDGE_JOBS)
    bridge["name"] = "bridge (${{ matrix.os }})"
    with pytest.raises(ValueError, match="required job inventory mismatch"):
        verify(evidence)


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
        (("artifact", "name"), "candidate-validation-v2-41-1"),
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
    needs: dict[str, Any] = {
        name: {"result": "success"}
        for name in (
            "classify",
            "quality",
            "pytest",
            "bridge",
            "pytest-report",
            "candidate-package",
        )
    }
    needs["classify"]["outputs"] = {"identity": json.dumps(record), "bridge": "true"}
    needs["candidate-package"]["outputs"] = {
        "identity": json.dumps(record),
        "artifact_id": "789",
        "artifact_name": "candidate-package-v1-42-1",
        "artifact_digest": "d" * 64,
        "receipt_sha256": "a" * 64,
    }
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
        ["candidate-validation-v2.json", "extra"],
        ["candidate-validation-v2.json"],
    ],
)
def test_archive_members_and_size_are_bounded(members: list[str]) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in members:
            archive.writestr(name, json.dumps(sample()["proof"]))
    if members == ["candidate-validation-v2.json"]:
        assert VERIFIER.unpack_proof(out.getvalue())["schema"] == "finance-candidate-validation-v2"
    else:
        with pytest.raises(ValueError):
            VERIFIER.unpack_proof(out.getvalue())
    with pytest.raises(ValueError):
        VERIFIER.unpack_proof(b"x" * (VERIFIER.MAX_ARTIFACT_BYTES + 1))


def test_downloaded_package_bundle_has_exact_four_safe_members_and_bound_bytes() -> None:
    evidence = sample()
    package = VERIFIER.unpack_package_bundle(evidence["package_zip_payload"])
    assert package["archive_sha256"] == evidence["package_bundle"]["archive_sha256"]
    assert package["receipt_sha256"] == evidence["package_bundle"]["receipt_sha256"]
    assert package["artifacts"] == evidence["package_bundle"]["artifacts"]

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("package-receipt.json", b"{}")
        archive.writestr("source-inventory.json", b"{}")
        archive.writestr("finance_core-0.1.7-py3-none-any.whl", b"wheel")
        archive.writestr("finance_core-0.1.7.tar.gz", b"sdist")
        archive.writestr("unexpected", b"extra")
    with pytest.raises(ValueError, match="one wheel and one sdist|exactly four"):
        VERIFIER.unpack_package_bundle(out.getvalue())
    with pytest.raises(ValueError):
        VERIFIER.unpack_package_bundle(b"x" * (VERIFIER.MAX_PACKAGE_ARCHIVE_BYTES + 1))


def test_acceptance_archive_has_one_bounded_report() -> None:
    evidence = sample()
    acceptance, content_sha = VERIFIER.unpack_acceptance(evidence["acceptance_zip_payload"])
    assert acceptance == evidence["acceptance"]
    assert content_sha == evidence["acceptance_file_sha256"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("prepared-envelope-acceptance-v1.json", b"{}")
        archive.writestr("extra.json", b"{}")
    with pytest.raises(ValueError, match="unexpected acceptance archive members"):
        VERIFIER.unpack_acceptance(out.getvalue())


def test_every_checkout_and_build_reference_is_the_candidate_head() -> None:
    source = (ROOT / VERIFIER.WORKFLOW).read_text()
    assert source.count(
        "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
    ) == source.count("uses: actions/checkout@")
    assert source.count("Verify candidate checkout identity") == 6
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
        "candidate-package",
        "validate",
    }


def test_collector_binds_api_inventory_archive_and_latest_pr_state(monkeypatch) -> None:
    fixture = sample()
    proof_archive = io.BytesIO()
    with zipfile.ZipFile(proof_archive, "w") as stream:
        stream.writestr("candidate-validation-v2.json", json.dumps(fixture["proof"]))
    proof_payload = proof_archive.getvalue()
    fixture["artifact"]["size_in_bytes"] = len(proof_payload)
    fixture["artifact"]["digest"] = "sha256:" + hashlib.sha256(proof_payload).hexdigest()
    prefix = f"repos/{REPO}"
    replies = {
        f"{prefix}/actions/runs/42": fixture["run"],
        f"{prefix}/actions/runs/42/artifacts?per_page=100": {
            "total_count": 3,
            "artifacts": [
                fixture["artifact"],
                fixture["package_artifact"],
                fixture["acceptance_artifact"],
            ],
        },
        f"{prefix}/actions/workflows/validate.yml": fixture["workflow"],
        f"{prefix}/actions/artifacts/789": fixture["package_artifact"],
        f"{prefix}/actions/artifacts/790": fixture["acceptance_artifact"],
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
        assert command[:2] == ["gh", "api"]
        return {
            f"{prefix}/actions/artifacts/456/zip": proof_payload,
            f"{prefix}/actions/artifacts/789/zip": fixture["package_zip_payload"],
            f"{prefix}/actions/artifacts/790/zip": fixture["acceptance_zip_payload"],
        }[command[2]]

    monkeypatch.setattr(VERIFIER, "api", api)
    monkeypatch.setattr(VERIFIER.subprocess, "check_output", download)
    evidence = VERIFIER.collect(REPO, 7, 42)
    assert verify(evidence)["status"] == "VERIFIED"
    assert observed[-2:] == [f"{prefix}/pulls/7", f"{prefix}/commits/main"]
    replies[f"{prefix}/commits/main"] = {"sha": "d" * 40}
    with pytest.raises(ValueError, match="base/main drifted"):
        verify(VERIFIER.collect(REPO, 7, 42))


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
        "finance_core/_native_age_v1.py",
        "finance_core/resources/crypto/age_toolchain_v1.json",
        "finance_core/resources/crypto/nested/future-tool.json",
        "finance_core/lifecycle_v2/prepared_envelope_v1.py",
        "finance_core/lifecycle_v2/prepared_future_v1.py",
        "finance_core/lifecycle_v2/_prepared_content_v1.py",
        "finance_core/lifecycle_v2/_prepared_future_v1.py",
        "tests/test_native_age_v1.py",
        "tests/test_prepared_content_export_v1.py",
        "tests/test_prepared_envelope_v1.py",
        "tests/test_prepared_envelope_faults_v1.py",
        "tests/test_age_toolchain_qualification_v1.py",
        "tests/test_finance_core_package_v1.py",
        "tests/installed_prepared_envelope_probe.py",
        "tests/prepared_envelope_test_support.py",
        "tests/lifecycle_recovery_material_support_v1.py",
        "tests/instance_admission_support_v1.py",
        "finance_core/openclaw_staging_bridge/commands.py",
        "finance_core/application/example.py",
        "finance_core/intake/__init__.py",
        "finance_core/intake/macos_vision_receipt_ocr.py",
        "finance_core/parser_proposals/__init__.py",
        "finance_core/parser_proposals/ai_example.py",
        "finance_core/parser_proposals/independent_ai_lineage.py",
        "finance_core/parser_proposals/independent_ai_validation.py",
        "finance_core/resources/independent_ai/policy_v1.json",
        "finance_core/resources/independent_ai/prompt_v1.txt",
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
    ],
)
def test_each_high_risk_bridge_input_requires_bridge_for_modify_delete_and_rename(
    path: str,
) -> None:
    for item in (
        {"filename": path, "status": "modified"},
        {"filename": path, "status": "removed"},
        {
            "filename": "archived/example.txt",
            "previous_filename": path,
            "status": "renamed",
        },
        {
            "filename": path,
            "previous_filename": "archived/example.txt",
            "status": "renamed",
        },
    ):
        evidence = sample(bridge=True)
        evidence["comparison"]["files"] = [item]
        assert verify(evidence)["bridge_required"] is True
        refused = sample(bridge=False)
        refused["comparison"]["files"] = [item]
        with pytest.raises(ValueError):
            verify(refused)

        missing = sample(bridge=True)
        missing["comparison"]["files"] = [item]
        del missing["proof"]["results"]["bridge"]
        with pytest.raises(ValueError, match="proof lacks successful required results"):
            verify(missing)
        missing = sample(bridge=True)
        missing["comparison"]["files"] = [item]
        missing["jobs"]["jobs"] = [
            job for job in missing["jobs"]["jobs"] if job["name"] not in VERIFIER.BRIDGE_JOBS
        ]
        missing["jobs"]["total_count"] = len(missing["jobs"]["jobs"])
        with pytest.raises(ValueError, match="required job inventory mismatch"):
            verify(missing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_id", 0),
        ("artifact_name", "release-latest"),
        ("artifact_digest", "sha256:" + "e" * 64),
        ("receipt_sha256", "incomplete"),
    ],
)
def test_candidate_package_proof_rejects_forged_identity(field, value):
    evidence = sample()
    evidence["proof"]["package"][field] = value
    with pytest.raises(ValueError):
        verify(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expired", True),
        ("id", 790),
        ("size_in_bytes", 0),
        ("workflow_run", {"id": 43, "head_sha": HEAD}),
    ],
)
def test_candidate_package_requires_actual_same_run_artifact(field, value):
    evidence = sample()
    evidence["package_artifact"][field] = value
    with pytest.raises(ValueError, match="package artifact"):
        verify(evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.__setitem__("status", "REFUSED"),
        lambda item: item.__setitem__("platform", "darwin-arm64"),
        lambda item: item["qualification"]["source_authentication"].__setitem__(
            "status", "VERIFIED"
        ),
        lambda item: item["qualification"]["tools"]["age"].__setitem__("sha256", "0" * 64),
        lambda item: item["native"]["checks"].pop("wrong_key_refused"),
        lambda item: item["native"].__setitem__("skipped", 1),
        lambda item: item["native"].__setitem__("skipped", False),
        lambda item: item["artifacts"]["wheel"].__setitem__("status", "FAIL"),
        lambda item: item["artifacts"]["sdist"]["checks"].__setitem__(
            "detached_source_free", False
        ),
        lambda item: item["package"].__setitem__("receipt_sha256", "0" * 64),
        lambda item: item["totals"].__setitem__("skipped", 1),
        lambda item: item["totals"].__setitem__("skipped", False),
    ],
)
def test_prepared_envelope_acceptance_rejects_incomplete_or_forged_results(mutation):
    evidence = sample()
    mutation(evidence["acceptance"])
    evidence["acceptance_file_sha256"] = digest(canonical(evidence["acceptance"]))
    with pytest.raises(ValueError):
        verify(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expired", True),
        ("id", 791),
        ("digest", "sha256:" + "0" * 64),
        ("size_in_bytes", 0),
        ("workflow_run", {"id": 43, "head_sha": HEAD}),
    ],
)
def test_prepared_envelope_artifact_requires_exact_same_run_bytes(field, value):
    evidence = sample()
    evidence["acceptance_artifact"][field] = value
    with pytest.raises(ValueError, match="acceptance artifact"):
        verify(evidence)


def test_prepared_envelope_package_receipt_and_archive_are_cross_checked():
    for mutation in (
        lambda item: item.__setitem__("archive_sha256", "0" * 64),
        lambda item: item["receipt"].__setitem__("status", "VALIDATED"),
        lambda item: item["artifacts"]["wheel"].__setitem__("sha256", "0" * 64),
    ):
        evidence = sample()
        mutation(evidence["package_bundle"])
        with pytest.raises(ValueError):
            verify(evidence)


def test_candidate_package_missing_or_skipped_job_cannot_validate():
    evidence = sample()
    evidence["jobs"]["jobs"] = [
        job for job in evidence["jobs"]["jobs"] if job["name"] != "candidate-package"
    ]
    evidence["jobs"]["total_count"] -= 1
    with pytest.raises(ValueError, match="job inventory"):
        verify(evidence)
    evidence = sample()
    evidence["proof"]["results"]["candidate-package"] = "skipped"
    with pytest.raises(ValueError, match="required results"):
        verify(evidence)


@pytest.mark.parametrize(
    "job_name,step_name",
    [
        ("candidate-package", "Prepare pinned Linux age tools"),
        (
            "candidate-package",
            "Require installed wheel and sdist prepared-envelope acceptance",
        ),
        ("candidate-package", "Upload prepared-envelope acceptance proof"),
        ("pytest (2)", "Prepare pinned Linux age tools for this shard"),
    ],
)
def test_native_qualification_and_installed_acceptance_steps_must_execute(
    job_name: str, step_name: str
) -> None:
    for outcome in ("missing", "skipped", "failure"):
        evidence = sample()
        job = next(item for item in evidence["jobs"]["jobs"] if item["name"] == job_name)
        if outcome == "missing":
            job["steps"] = [step for step in job["steps"] if step["name"] != step_name]
        else:
            step = next(step for step in job["steps"] if step["name"] == step_name)
            step["conclusion"] = "skipped" if outcome == "skipped" else "failure"
        with pytest.raises(ValueError, match="required job step"):
            verify(evidence)


def test_new_candidate_contract_does_not_reinterpret_historical_v1_proof():
    evidence = sample()
    evidence["proof"]["schema"] = "finance-candidate-validation-v1"
    with pytest.raises(ValueError, match="unknown proof schema"):
        verify(evidence)
