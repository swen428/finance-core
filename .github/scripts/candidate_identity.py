"""Bind every validation lane to its actual immutable checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

WORKFLOW = ".github/workflows/validate.yml"
SHA = re.compile(r"[0-9a-f]{40}")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def identity(env: dict[str, str]) -> dict[str, object]:
    head = env["CANDIDATE_SHA"]
    base = env["CANDIDATE_BASE_SHA"]
    event_sha = env["GITHUB_SHA"]
    workflow_sha = env["GITHUB_WORKFLOW_SHA"]
    if any(SHA.fullmatch(value) is None for value in (head, base, event_sha, workflow_sha)):
        raise ValueError("candidate identity requires exact commit SHAs")
    if git("rev-parse", "HEAD") != head:
        raise ValueError("actual checkout is not the candidate head")
    if env["GITHUB_EVENT_NAME"] == "pull_request":
        if git("merge-base", base, head) != base:
            raise ValueError("candidate must include the frozen base before validation")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("candidate tracked files changed before validation")
    return {
        "schema": "finance-candidate-identity-v1",
        "repository": env["GITHUB_REPOSITORY"],
        "event": env["GITHUB_EVENT_NAME"],
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "pr_number": int(env.get("CANDIDATE_PR_NUMBER") or "0"),
        "base_sha": base,
        "head_sha": head,
        "tested_sha": git("rev-parse", "HEAD"),
        "tested_tree": git("rev-parse", "HEAD^{tree}"),
        "event_sha": event_sha,
        "workflow_sha": workflow_sha,
        "workflow_ref": env["GITHUB_WORKFLOW_REF"],
        "workflow_path": WORKFLOW,
        "workflow_sha256": hashlib.sha256(Path(WORKFLOW).read_bytes()).hexdigest(),
    }


def summarize(record: dict[str, object], needs: dict[str, object]) -> dict[str, object]:
    if set(needs) != {"classify", "quality", "pytest", "bridge", "pytest-report"}:
        raise ValueError("required validation needs are missing or unexpected")
    classify = needs["classify"]
    if not isinstance(classify, dict):
        raise ValueError("classification result is missing")
    outputs = classify.get("outputs")
    if not isinstance(outputs, dict) or json.loads(str(outputs.get("identity", "null"))) != record:
        raise ValueError("classification and final checkout identities disagree")
    bridge = outputs.get("bridge")
    if bridge not in {"true", "false"}:
        raise ValueError("Bridge scope is missing")
    if record["event"] != "pull_request" and bridge != "true":
        raise ValueError("push and manual validation require both Bridge systems")
    results = {}
    for name, job in needs.items():
        expected = "skipped" if name == "bridge" and bridge == "false" else "success"
        if not isinstance(job, dict) or job.get("result") != expected:
            raise ValueError(f"missing or unsuccessful required validation: {name}")
        results[name] = expected
    return {
        "schema": "finance-candidate-validation-v1",
        "identity": record,
        "bridge_required": bridge == "true",
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    record = identity(dict(os.environ))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(
                "identity=" + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )
    result = (
        summarize(record, json.loads(os.environ["VALIDATION_NEEDS"])) if args.summarize else record
    )
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
