"""Read-only verification of one frozen PR's hosted validation evidence.

Use the reviewed copy of this tool, with explicit reviewed base/head. A receipt
is a point-in-time observation, not merge approval or a release attestation.
Run again immediately before a protected exact-head merge if delivery waits.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKFLOW = ".github/workflows/validate.yml"
SHA = re.compile(r"[0-9a-f]{40}")
CORE_JOBS = {"classify", "quality", "pytest-report", "validate"} | {
    f"pytest ({index})" for index in range(4)
}
BRIDGE_JOBS = {"bridge (ubuntu-latest)", "bridge (macos-15)"}
MAX_ARTIFACT_BYTES = 1024 * 1024


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify(
    evidence: dict[str, Any], *, repository: str, pr_number: int, base: str, head: str
) -> dict[str, Any]:
    """Verify independently fetched API records and exact run artifact."""
    require(SHA.fullmatch(base) and SHA.fullmatch(head), "expected identities must be exact SHAs")
    pr, run, workflow = evidence["pr"], evidence["run"], evidence["workflow"]
    require(pr["number"] == pr_number and pr["state"] == "open", "PR is not the open candidate")
    require(pr["base"]["repo"]["full_name"] == repository, "wrong base repository")
    require(pr["base"]["ref"] == "main", "PR must target main")
    require(pr["base"]["sha"] == base and evidence["main"]["sha"] == base, "base/main drifted")
    require(pr["head"]["sha"] == head, "candidate head drifted")
    require(run["repository"]["full_name"] == repository, "run belongs to another repository")
    require(run["event"] == "pull_request", "normal candidate proof requires a PR run")
    require(run["head_sha"] == head, "run head mismatch")
    require(run["run_attempt"] == 1, "normal candidate proof cannot reuse a rerun")
    require(run["status"] == "completed" and run["conclusion"] == "success", "run did not succeed")
    require(workflow["path"] == WORKFLOW and workflow["state"] == "active", "wrong workflow")
    require(
        run["path"] == WORKFLOW and run["workflow_id"] == workflow["id"], "run workflow mismatch"
    )

    proof = evidence["proof"]
    require(proof["schema"] == "finance-candidate-validation-v1", "unknown proof schema")
    identity = proof["identity"]
    expected = {
        "schema": "finance-candidate-identity-v1",
        "repository": repository,
        "event": "pull_request",
        "run_id": run["id"],
        "run_attempt": 1,
        "pr_number": pr_number,
        "base_sha": base,
        "head_sha": head,
        "tested_sha": head,
        "tested_tree": evidence["head_commit"]["tree"]["sha"],
        "workflow_path": WORKFLOW,
        "workflow_ref": f"{repository}/{WORKFLOW}@refs/pull/{pr_number}/merge",
    }
    require(
        all(identity.get(key) == value for key, value in expected.items()),
        "proof identity mismatch",
    )
    require(evidence["head_commit"]["sha"] == head, "head commit record mismatch")
    require(
        evidence["comparison"]["merge_base_commit"]["sha"] == base, "candidate excludes frozen base"
    )
    event_commit = evidence["event_commit"]
    require(event_commit["sha"] == identity["event_sha"], "event commit record mismatch")
    require(
        [parent["sha"] for parent in event_commit["parents"]] == [base, head]
        and event_commit["tree"]["sha"] == identity["tested_tree"],
        "PR merge identity does not bind the exact frozen base/head tree",
    )
    require(
        identity["workflow_sha"] in {head, identity["event_sha"]},
        "workflow commit is not this candidate",
    )
    reviewed_bytes = base64.b64decode(evidence["head_workflow"]["content"], validate=False)
    executed_bytes = base64.b64decode(evidence["executed_workflow"]["content"], validate=False)
    require(reviewed_bytes == executed_bytes, "executed workflow differs from reviewed workflow")
    require(
        hashlib.sha256(reviewed_bytes).hexdigest() == identity["workflow_sha256"],
        "workflow content digest mismatch",
    )
    require(type(proof["bridge_required"]) is bool, "invalid Bridge scope")
    bridge_required = proof["bridge_required"]
    # Recompute scope from the complete API comparison, never trust only the artifact.
    files = evidence["comparison"]["files"]
    require(len(files) < 300, "comparison may be truncated; cannot prove complete changed paths")
    pattern = re.compile(
        r"^(\.github/|scripts/|pyproject\.toml$|requirements-dev\.txt$|MANIFEST\.in$|"
        r"plugins/finance-bridge/|native/|finance_core/(openclaw_staging_bridge|"
        r"application/|intake/(__init__\.py$|macos_vision_receipt_ocr)|"
        r"parser_proposals/(__init__\.py$|ai_)))"
    )
    actual_scope = any(
        pattern.search(path)
        for item in files
        for path in (item["filename"], item.get("previous_filename", ""))
    )
    require(bridge_required is actual_scope, "Bridge requirement disagrees with candidate paths")
    expected_results = {
        name: "success" for name in ("classify", "quality", "pytest", "pytest-report")
    }
    expected_results["bridge"] = "success" if bridge_required else "skipped"
    require(proof["results"] == expected_results, "proof lacks successful required results")

    jobs = evidence["jobs"]["jobs"]
    require(evidence["jobs"]["total_count"] == len(jobs), "job inventory is truncated")
    names = [job["name"] for job in jobs]
    require(len(set(names)) == len(names), "duplicate job identity")
    require(len({job["id"] for job in jobs}) == len(jobs), "duplicate job ID")
    if bridge_required:
        require(set(names) == CORE_JOBS | BRIDGE_JOBS, "required job inventory mismatch")
    else:
        require(
            set(names) in (CORE_JOBS | {"bridge"}, CORE_JOBS | BRIDGE_JOBS),
            "optional job inventory mismatch",
        )
    for job in jobs:
        conclusion = "success" if job["name"] in CORE_JOBS or bridge_required else "skipped"
        require(
            job["run_id"] == run["id"]
            and job["run_attempt"] == 1
            and job["head_sha"] == head
            and job["status"] == "completed"
            and job["conclusion"] == conclusion,
            f"wrong or unsuccessful job evidence: {job['name']}",
        )
    artifact = evidence["artifact"]
    require(not artifact["expired"], "candidate proof artifact expired")
    require(artifact["name"] == f"candidate-validation-v1-{run['id']}-1", "artifact name mismatch")
    require(
        artifact.get("digest") == "sha256:" + evidence["artifact_sha256"],
        "downloaded artifact digest mismatch",
    )
    require(
        artifact["workflow_run"]["id"] == run["id"]
        and artifact["workflow_run"]["head_sha"] == head,
        "artifact run identity mismatch",
    )
    return {
        "schema": "finance-verified-candidate-v1",
        "repository": repository,
        "pr_number": pr_number,
        "base_sha": base,
        "head_sha": head,
        "tested_tree": identity["tested_tree"],
        "workflow_id": workflow["id"],
        "workflow_sha": identity["workflow_sha"],
        "workflow_sha256": identity["workflow_sha256"],
        "run_id": run["id"],
        "run_attempt": 1,
        "artifact_id": artifact["id"],
        "artifact_digest": artifact.get("digest"),
        "bridge_required": bridge_required,
        "jobs": [{key: job[key] for key in ("id", "name", "conclusion")} for job in jobs],
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "VERIFIED",
    }


def api(endpoint: str) -> Any:
    result = subprocess.check_output(["gh", "api", endpoint])
    return json.loads(result)


def unpack_proof(payload: bytes) -> dict[str, Any]:
    require(len(payload) <= MAX_ARTIFACT_BYTES, "candidate archive is too large")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        require(
            archive.namelist() == ["candidate-validation-v1.json"],
            "unexpected proof archive members",
        )
        info = archive.infolist()[0]
        require(info.file_size <= MAX_ARTIFACT_BYTES, "candidate proof is too large")
        return json.loads(archive.read(info))


def collect(repository: str, pr_number: int, run_id: int) -> dict[str, Any]:
    prefix = f"repos/{repository}"
    run = api(f"{prefix}/actions/runs/{run_id}")
    artifacts = api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
    require(artifacts["total_count"] == len(artifacts["artifacts"]), "artifact inventory truncated")
    found = [
        item
        for item in artifacts["artifacts"]
        if item["name"] == f"candidate-validation-v1-{run_id}-1"
    ]
    require(len(found) == 1, "unique candidate proof artifact is missing")
    artifact = found[0]
    require(0 < artifact["size_in_bytes"] <= MAX_ARTIFACT_BYTES, "artifact size is invalid")
    payload = subprocess.check_output(
        ["gh", "api", f"{prefix}/actions/artifacts/{int(artifact['id'])}/zip"]
    )
    proof = unpack_proof(payload)
    identity = proof["identity"]
    for key in ("base_sha", "head_sha", "event_sha", "workflow_sha"):
        require(
            isinstance(identity[key], str) and SHA.fullmatch(identity[key]), "invalid proof SHA"
        )
    head, base = identity["head_sha"], identity["base_sha"]
    evidence = {
        "run": run,
        "artifact": artifact,
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "proof": proof,
        "workflow": api(f"{prefix}/actions/workflows/validate.yml"),
        "jobs": api(f"{prefix}/actions/runs/{run_id}/attempts/1/jobs?per_page=100"),
        "head_commit": api(f"{prefix}/git/commits/{head}"),
        "event_commit": api(f"{prefix}/git/commits/{identity['event_sha']}"),
        "comparison": api(f"{prefix}/compare/{base}...{head}?per_page=1"),
        "head_workflow": api(f"{prefix}/contents/{WORKFLOW}?ref={head}"),
        "executed_workflow": api(f"{prefix}/contents/{WORKFLOW}?ref={identity['workflow_sha']}"),
    }
    # Last reads bind the proof to the still-current candidate, not an old PR snapshot.
    evidence["pr"] = api(f"{prefix}/pulls/{pr_number}")
    evidence["main"] = api(f"{prefix}/commits/main")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository), "invalid repository")
    require(args.pr > 0 and args.run_id > 0, "invalid PR/run identity")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    evidence = collect(args.repository, args.pr, args.run_id)
    (args.output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )
    verified = verify(
        evidence, repository=args.repository, pr_number=args.pr, base=args.base, head=args.head
    )
    (args.output_dir / "verified.json").write_text(
        json.dumps(verified, indent=2) + "\n", encoding="utf-8"
    )
    print(f"VERIFIED PR #{args.pr}: {args.head} from run {args.run_id}")


if __name__ == "__main__":
    main()
