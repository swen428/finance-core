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
import stat
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKFLOW = ".github/workflows/validate.yml"
SHA = re.compile(r"[0-9a-f]{40}")
CORE_JOBS = {
    "classify",
    "quality",
    "pytest-report",
    "validate",
    "candidate-package",
} | {f"pytest ({index})" for index in range(4)}
BRIDGE_JOBS = {"bridge (ubuntu-latest)", "bridge (macos-15)"}
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_PACKAGE_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_METADATA_BYTES = 16 * 1024 * 1024
AGE_RELEASE = "v1.3.2"
AGE_SOURCE_COMMIT = "b74dce4cdbe35b5e5f66c06d9612b72f89028758"
AGE_SOURCE_ARCHIVE = "age-v1.3.2-linux-amd64.tar.gz"
AGE_SOURCE_ARCHIVE_SHA256 = "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"
AGE_SOURCE_PROOF_SHA256 = "98402fa59b4e433426156b4a7e74a32e44902e0434c61158f7de18ffc84f1f97"
AGE_TOOL_PINS = {
    "age": ("eb7dd1b518f0a307c99cd97782623c5321da049154b04acd2d98d21aa7bc9b2c", 6977014),
    "age_keygen": ("0a0009db842259d6717f7eeb30acb6b90d2a2eb924c6acd0a0db0ca1f1537899", 4051278),
}
NATIVE_CHECKS = {
    "age_version",
    "age_keygen_version",
    "two_recipient_encrypt",
    "recovery_decrypt",
    "temporary_decrypt",
    "wrong_key_refused",
    "corrupt_tail_refused",
}
INSTALLED_CHECKS = {
    "installed_origin",
    "checkout_invisible",
    "protected_prepare_export",
    "large_frame_over_8mib",
    "seal",
    "restart_same_bytes",
    "detached_source_free",
}
BRIDGE_SCOPE_PATTERN = (
    r"^(\.github/|scripts/|pyproject\.toml$|requirements-dev\.txt$|MANIFEST\.in$|"
    r"plugins/finance-bridge/|native/|finance_core/_native_age_v1\.py$|"
    r"finance_core/resources/crypto/|finance_core/lifecycle_v2/(prepared_[^/]+\.py|"
    r"_prepared_[^/]+\.py)$|finance_core/(openclaw_staging_bridge|"
    r"application/|intake/(__init__\.py$|macos_vision_receipt_ocr|receipt_media\.py$|"
    r"_receipt_media_worker\.py$|receipt_ocr_evidence\.py$|receipt_ocr_proposal\.py$|"
    r"tesseract_resources\.py$)|parser_proposals/(__init__\.py$|receipt_total_parser\.py$|"
    r"ai_|independent_ai_)|resources/independent_ai/)|tests/(test_(native_age|"
    r"prepared_content_export|prepared_envelope(_faults)?|age_toolchain_qualification|"
    r"finance_core_package|candidate_validation|public_ci_security)_v1\.py$|"
    r"prepared_envelope_test_support\.py$|installed_prepared_envelope_probe\.py$|"
    r"lifecycle_recovery_material_support_v1\.py$|instance_admission_support_v1\.py$|"
    r"test_receipt_media(_vectors)?_v1\.py$|fixtures/receipt_media_v1/|"
    r"test_receipt_total_parser_hierarchy_v2\.py$|fixtures/receipt_total_parser_v2/|"
    r"test_tesseract_pinned_resources_v1\.py$|test_linux_receipt_ocr_acceptance_v1\.py$|"
    r"test_receipt_ocr_evidence\.py$|"
    r"test_openclaw_staging_bridge_ocr_production_boundary_v1\.py$|"
    r"fixtures/linux_receipt_ocr/))"
)


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _same_workflow_run(metadata: dict[str, Any], run_id: int, head: str) -> bool:
    workflow_run = metadata.get("workflow_run")
    if not isinstance(workflow_run, dict):
        return False
    attempt = workflow_run.get("run_attempt")
    return (
        workflow_run.get("id") == run_id
        and workflow_run.get("head_sha") == head
        and (attempt is None or (type(attempt) is int and attempt == 1))
    )


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    )


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _simple_archive_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and not any(ord(character) < 32 or ord(character) == 127 for character in name)
    )


def _validated_zip(payload: bytes, *, maximum_archive: int, maximum_total: int):
    require(0 < len(payload) <= maximum_archive, "archive size is invalid")
    archive = zipfile.ZipFile(io.BytesIO(payload))
    infos = archive.infolist()
    names = [info.filename for info in infos]
    require(len(names) == len(set(names)), "duplicate archive member")
    total = 0
    for info in infos:
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        require(
            _simple_archive_name(info.filename)
            and not info.is_dir()
            and (file_type in {0, stat.S_IFREG})
            and not (info.flag_bits & 0x1),
            "unsafe archive member",
        )
        require(0 <= info.file_size <= MAX_PACKAGE_MEMBER_BYTES, "archive member too large")
        total += info.file_size
        require(total <= maximum_total, "archive expanded size exceeds bound")
    return archive, infos


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, maximum: int) -> bytes:
    require(info.file_size <= maximum, "archive metadata member exceeds bound")
    with archive.open(info, "r") as stream:
        body = stream.read(maximum + 1)
        require(
            len(body) == info.file_size and stream.read(1) == b"",
            "archive member size changed",
        )
    return body


def unpack_acceptance(payload: bytes) -> tuple[dict[str, Any], str]:
    expected_name = "prepared-envelope-acceptance-v1.json"
    with _validated_zip(
        payload, maximum_archive=MAX_ARTIFACT_BYTES, maximum_total=MAX_ARTIFACT_BYTES
    )[0] as archive:
        infos = archive.infolist()
        require(
            [info.filename for info in infos] == [expected_name],
            "unexpected acceptance archive members",
        )
        body = _read_bounded(archive, infos[0], MAX_ARTIFACT_BYTES)
    acceptance = json.loads(body)
    require(isinstance(acceptance, dict), "acceptance report is not an object")
    return acceptance, sha256(body)


def _distribution_names(names: set[str]) -> dict[str, str]:
    wheel_pattern = r"finance_core-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any\.whl"
    sdist_pattern = r"finance_core-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz"
    wheels = [name for name in names if re.fullmatch(wheel_pattern, name)]
    sdists = [name for name in names if re.fullmatch(sdist_pattern, name)]
    require(len(wheels) == len(sdists) == 1, "package archive must have one wheel and one sdist")
    wheel_version = wheels[0].removeprefix("finance_core-").removesuffix("-py3-none-any.whl")
    sdist_version = sdists[0].removeprefix("finance_core-").removesuffix(".tar.gz")
    require(wheel_version == sdist_version, "wheel and sdist versions differ")
    return {"wheel": wheels[0], "sdist": sdists[0]}


def _stream_member_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            require(size <= MAX_PACKAGE_MEMBER_BYTES, "distribution exceeds package size bound")
            digest.update(chunk)
    require(size == info.file_size, "distribution size differs from ZIP metadata")
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def unpack_package_bundle(payload: bytes) -> dict[str, Any]:
    with _validated_zip(
        payload,
        maximum_archive=MAX_PACKAGE_ARCHIVE_BYTES,
        maximum_total=MAX_PACKAGE_ARCHIVE_BYTES,
    )[0] as archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        require(
            "package-receipt.json" in names and "source-inventory.json" in names,
            "package evidence missing",
        )
        distributions = _distribution_names(
            names - {"package-receipt.json", "source-inventory.json"}
        )
        require(len(names) == 4, "candidate package must contain exactly four members")
        by_name = {info.filename: info for info in infos}
        receipt_bytes = _read_bounded(archive, by_name["package-receipt.json"], 1024 * 1024)
        inventory_bytes = _read_bounded(
            archive, by_name["source-inventory.json"], MAX_PACKAGE_METADATA_BYTES
        )
        receipt = json.loads(receipt_bytes)
        inventory = json.loads(inventory_bytes)
        require(
            isinstance(receipt, dict) and isinstance(inventory, dict),
            "package evidence is not an object",
        )
        package_artifacts = {
            kind: {"filename": name, **_stream_member_digest(archive, by_name[name])}
            for kind, name in distributions.items()
        }
    return {
        "archive_sha256": sha256(payload),
        "members": sorted(names),
        "receipt": receipt,
        "receipt_sha256": sha256(receipt_bytes),
        "inventory_sha256": sha256(inventory_bytes),
        "inventory": inventory,
        "artifacts": package_artifacts,
    }


def _check_counts(record: dict[str, Any], checks: set[str], expected: int) -> None:
    observed = record.get("checks")
    require(
        isinstance(observed, dict)
        and set(observed) == checks
        and all(value is True for value in observed.values()),
        "required native or installed checks are missing or false",
    )
    require(
        all(
            type(record.get(key)) is int
            for key in ("required", "passed", "skipped", "failed", "errors")
        )
        and record.get("required") == expected
        and record.get("passed") == expected
        and record.get("skipped") == 0
        and record.get("failed") == 0
        and record.get("errors") == 0,
        "acceptance counts include skips or failures",
    )


def _object(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return value


def _list(value: Any, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(message)
    return value


def _require_job_step(job: dict[str, Any], name: str) -> None:
    steps = _list(job.get("steps"), f"job step evidence is missing: {job.get('name')}")
    if any(not isinstance(step, dict) for step in steps):
        raise ValueError(f"job step evidence is malformed: {job.get('name')}")
    matches = [step for step in steps if step.get("name") == name]
    require(
        len(matches) == 1
        and matches[0].get("status") == "completed"
        and matches[0].get("conclusion") == "success",
        f"required job step did not succeed: {job.get('name')} / {name}",
    )


def _check_acceptance(
    acceptance: dict[str, Any],
    *,
    identity: dict[str, Any],
    run_id: int,
    package_bundle: dict[str, Any],
) -> None:
    require(
        acceptance.get("schema") == "finance-prepared-envelope-acceptance-v1"
        and acceptance.get("status") == "ACCEPTED"
        and acceptance.get("identity") == identity
        and acceptance.get("run_id") == run_id
        and acceptance.get("run_attempt") == 1
        and acceptance.get("platform") == "linux-x86_64",
        "prepared-envelope acceptance identity or platform mismatch",
    )
    qualification = _object(
        acceptance.get("qualification"), "native tool qualification is missing or malformed"
    )
    require(
        isinstance(qualification, dict)
        and qualification.get("release") == AGE_RELEASE
        and qualification.get("source_commit") == AGE_SOURCE_COMMIT
        and qualification.get("platform") == "linux-x86_64",
        "native tool qualification identity mismatch",
    )
    source_auth = _object(
        qualification.get("source_authentication"),
        "source authentication provenance is missing or malformed",
    )
    require(
        isinstance(source_auth, dict)
        and source_auth
        == {
            "status": "PREQUALIFIED_SIGSUM_PROOF_REUSED",
            "source_commit": AGE_SOURCE_COMMIT,
            "archive_sha256": AGE_SOURCE_ARCHIVE_SHA256,
            "proof_sha256": AGE_SOURCE_PROOF_SHA256,
            "policy": "sigsum-generic-2025-1",
            "verifier_version": "sigsum-verify (sigsum-go module) v0.13.1",
        },
        "source authentication provenance was changed or overstated",
    )
    archive = _object(qualification.get("archive"), "pinned source archive record is malformed")
    proof = _object(qualification.get("proof"), "pinned source proof record is malformed")
    require(
        archive.get("filename") == AGE_SOURCE_ARCHIVE
        and archive.get("sha256") == AGE_SOURCE_ARCHIVE_SHA256
        and type(archive.get("size_bytes")) is int
        and 0 < archive["size_bytes"] <= 64 * 1024 * 1024
        and proof.get("filename") == AGE_SOURCE_ARCHIVE + ".proof"
        and proof.get("sha256") == AGE_SOURCE_PROOF_SHA256
        and type(proof.get("size_bytes")) is int
        and 0 < proof["size_bytes"] <= 16 * 1024 * 1024,
        "pinned source archive or proof differs",
    )
    tools = _object(qualification.get("tools"), "tool inventory is missing or malformed")
    require(set(tools) == set(AGE_TOOL_PINS), "tool inventory differs")
    for name, (expected_hash, expected_size) in AGE_TOOL_PINS.items():
        tool = _object(tools[name], f"pinned native executable evidence is malformed: {name}")
        require(
            tool.get("version") == AGE_RELEASE
            and tool.get("sha256") == expected_hash
            and type(tool.get("size_bytes")) is int
            and tool.get("size_bytes") == expected_size
            and isinstance(tool.get("path"), str)
            and Path(tool["path"]).is_absolute(),
            f"pinned native executable evidence differs: {name}",
        )

    native = _object(acceptance.get("native"), "native result missing or malformed")
    _check_counts(native, NATIVE_CHECKS, 7)

    package = _object(
        acceptance.get("package"), "acceptance package binding is missing or malformed"
    )
    require(
        set(package) == {"receipt_sha256", "source_inventory_sha256", "artifacts"}
        and package.get("receipt_sha256") == package_bundle["receipt_sha256"]
        and package.get("source_inventory_sha256") == package_bundle["inventory_sha256"]
        and package.get("artifacts") == package_bundle["artifacts"],
        "acceptance proof does not bind exact receipt and distributions",
    )
    receipt = _object(package_bundle["receipt"], "candidate package receipt is malformed")
    inventory = _object(package_bundle["inventory"], "candidate source inventory is malformed")
    recipe = _object(receipt.get("recipe"), "candidate package recipe is missing or malformed")
    receipt_artifacts = _object(
        receipt.get("artifacts"), "candidate package artifact inventory is missing or malformed"
    )
    require(
        receipt.get("schema") == "finance-candidate-package-v1"
        and receipt.get("status") == "BUILT_NOT_VALIDATED"
        and receipt.get("identity") == identity
        and receipt.get("source_inventory_sha256") == package_bundle["inventory_sha256"]
        and receipt.get("recipe_sha256") == sha256(encoded(recipe))
        and set(receipt_artifacts)
        == {item["filename"] for item in package_bundle["artifacts"].values()},
        "candidate package receipt identity or status mismatch",
    )
    inventory_files = _list(
        inventory.get("files"), "candidate source inventory files are malformed"
    )
    require(
        inventory.get("schema") == "finance-candidate-source-inventory-v1"
        and inventory.get("identity") == identity
        and len(inventory_files) <= 20000,
        "candidate source inventory identity mismatch",
    )
    for item in package_bundle["artifacts"].values():
        require(
            receipt_artifacts[item["filename"]]
            == {"size_bytes": item["size_bytes"], "sha256": item["sha256"]},
            "candidate distribution differs from package receipt",
        )
    runtime_dependencies = _object(
        acceptance.get("runtime_dependencies"),
        "installed-probe runtime dependencies are missing or malformed",
    )
    pypdf = _object(runtime_dependencies.get("pypdf"), "pypdf dependency record is malformed")
    typing_extensions = _object(
        runtime_dependencies.get("typing-extensions"),
        "typing-extensions dependency record is malformed",
    )
    sdist_backend = _object(
        acceptance.get("sdist_build_backend"), "sdist build backend record is malformed"
    )
    require(
        pypdf.get("version") == "6.21.4"
        and typing_extensions.get("version") == "4.16.0"
        and sdist_backend.get("version") == "84.1.0",
        "installed-probe dependency inputs differ from the lock",
    )

    artifacts = _object(acceptance.get("artifacts"), "wheel/sdist outcomes missing or malformed")
    require(set(artifacts) == {"wheel", "sdist"}, "wheel/sdist outcomes missing")
    for kind, result in artifacts.items():
        result = _object(result, f"{kind} outcome is malformed")
        _check_counts(result, INSTALLED_CHECKS, 7)
        expected_distribution = package_bundle["artifacts"][kind]
        require(
            result.get("status") == "PASS"
            and result.get("content_status") == "CONTENT_VERIFIED_OFFLINE"
            and result.get("content_kind") == "PREPARED_PROJECTION"
            and result.get("source_finalization") == "NOT_ESTABLISHED"
            and result.get("source_checkout_visible") is False
            and result.get("native_checks") == native["checks"]
            and result.get("module_file", "").endswith("/finance_core/__init__.py")
            and type(result.get("frame_size_bytes")) is int
            and result.get("frame_size_bytes", 0) > 8 * 1024 * 1024
            and type(result.get("envelope_size_bytes")) is int
            and result.get("envelope_size_bytes", 0) > 0
            and result.get("envelope_size_bytes", MAX_PACKAGE_MEMBER_BYTES + 1)
            <= MAX_PACKAGE_MEMBER_BYTES
            and all(
                isinstance(result.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", result[field])
                for field in (
                    "envelope_sha256",
                    "ciphertext_sha256",
                    "intent_sha256",
                    "inventory_rows_sha256",
                    "inventory_evidence_sha256",
                )
            )
            and expected_distribution["filename"] in package_bundle["receipt"]["artifacts"],
            f"installed {kind} process evidence is incomplete",
        )
    totals = acceptance.get("totals")
    require(
        isinstance(totals, dict)
        and set(totals) == {"required", "passed", "skipped", "failed", "errors"}
        and all(type(value) is int for value in totals.values())
        and totals == {"required": 21, "passed": 21, "skipped": 0, "failed": 0, "errors": 0},
        "acceptance totals are not complete and skip-free",
    )


def _check_artifact_inventory(evidence: dict[str, Any], run_id: int) -> None:
    inventory = [
        _object(item, "artifact inventory entry is malformed")
        for item in _list(evidence.get("artifact_inventory"), "artifact inventory is missing")
    ]
    total = evidence.get("artifact_total_count")
    require(
        type(total) is int
        and total == len(inventory)
        and total < 100
        and len({item.get("id") for item in inventory}) == len(inventory),
        "artifact inventory is truncated or has duplicate IDs",
    )
    pairs = (
        ("artifact", f"candidate-validation-v2-{run_id}-1"),
        ("package_artifact", f"candidate-package-v1-{run_id}-1"),
        ("acceptance_artifact", f"prepared-envelope-acceptance-v1-{run_id}-1"),
    )
    for field, name in pairs:
        detail = _object(evidence.get(field), f"{field} artifact API record is malformed")
        matches = [item for item in inventory if item.get("name") == name]
        label = {
            "artifact": "candidate proof",
            "package_artifact": "candidate package",
            "acceptance_artifact": "prepared-envelope acceptance",
        }[field]
        require(
            len(matches) == 1
            and all(
                detail.get(key) == matches[0].get(key)
                for key in ("id", "name", "digest", "size_in_bytes", "expired", "workflow_run")
            ),
            f"{label} artifact API record is not the unique run artifact: {name}",
        )


def verify(
    evidence: dict[str, Any], *, repository: str, pr_number: int, base: str, head: str
) -> dict[str, Any]:
    """Verify independently fetched API records and exact run artifact."""
    require(
        SHA.fullmatch(base) and SHA.fullmatch(head),
        "expected identities must be exact SHAs",
    )
    pr, run, workflow = evidence["pr"], evidence["run"], evidence["workflow"]
    require(
        pr["number"] == pr_number and pr["state"] == "open",
        "PR is not the open candidate",
    )
    require(pr["base"]["repo"]["full_name"] == repository, "wrong base repository")
    require(pr["base"]["ref"] == "main", "PR must target main")
    require(
        pr["base"]["sha"] == base and evidence["main"]["sha"] == base,
        "base/main drifted",
    )
    require(pr["head"]["sha"] == head, "candidate head drifted")
    require(
        run["repository"]["full_name"] == repository,
        "run belongs to another repository",
    )
    require(run["event"] == "pull_request", "normal candidate proof requires a PR run")
    require(run["head_sha"] == head, "run head mismatch")
    require(run["run_attempt"] == 1, "normal candidate proof cannot reuse a rerun")
    require(
        run["status"] == "completed" and run["conclusion"] == "success",
        "run did not succeed",
    )
    _check_artifact_inventory(evidence, run["id"])
    require(workflow["path"] == WORKFLOW and workflow["state"] == "active", "wrong workflow")
    require(
        run["path"] == WORKFLOW and run["workflow_id"] == workflow["id"],
        "run workflow mismatch",
    )

    proof = evidence["proof"]
    require(proof["schema"] == "finance-candidate-validation-v2", "unknown proof schema")
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
        evidence["comparison"]["merge_base_commit"]["sha"] == base,
        "candidate excludes frozen base",
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
    require(
        reviewed_bytes == executed_bytes,
        "executed workflow differs from reviewed workflow",
    )
    require(
        hashlib.sha256(reviewed_bytes).hexdigest() == identity["workflow_sha256"],
        "workflow content digest mismatch",
    )
    require(type(proof["bridge_required"]) is bool, "invalid Bridge scope")
    bridge_required = proof["bridge_required"]
    # Recompute scope from the complete API comparison, never trust only the artifact.
    files = evidence["comparison"]["files"]
    require(
        len(files) < 300,
        "comparison may be truncated; cannot prove complete changed paths",
    )
    pattern = re.compile(BRIDGE_SCOPE_PATTERN)
    actual_scope = any(
        pattern.search(path)
        for item in files
        for path in (item["filename"], item.get("previous_filename", ""))
    )
    require(
        bridge_required is actual_scope,
        "Bridge requirement disagrees with candidate paths",
    )
    expected_results = {
        name: "success"
        for name in (
            "classify",
            "quality",
            "pytest",
            "pytest-report",
            "candidate-package",
        )
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
            set(names)
            in (
                CORE_JOBS | {"bridge"},
                CORE_JOBS | {"bridge (${{ matrix.os }})"},
                CORE_JOBS | BRIDGE_JOBS,
            ),
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
    by_name = {job["name"]: job for job in jobs}
    package_job = by_name["candidate-package"]
    for step_name in (
        "Prepare pinned Linux age tools",
        "Require installed wheel and sdist prepared-envelope acceptance",
        "Upload prepared-envelope acceptance proof",
        "Upload candidate build identity, not release approval",
    ):
        _require_job_step(package_job, step_name)
    for index in range(4):
        _require_job_step(
            by_name[f"pytest ({index})"], "Prepare pinned Linux age tools for this shard"
        )
    package = proof.get("package")
    require(
        isinstance(package, dict)
        and set(package) == {"artifact_id", "artifact_name", "artifact_digest", "receipt_sha256"},
        "candidate package proof is missing or malformed",
    )
    require(
        type(package["artifact_id"]) is int
        and package["artifact_id"] > 0
        and isinstance(package["receipt_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", package["receipt_sha256"]),
        "invalid package receipt identity",
    )
    package_artifact = evidence["package_artifact"]
    require(
        type(package_artifact.get("id")) is int
        and package_artifact["id"] == package["artifact_id"]
        and package_artifact["name"]
        == package["artifact_name"]
        == f"candidate-package-v1-{run['id']}-1"
        and package_artifact["digest"] == package["artifact_digest"]
        and isinstance(package_artifact["digest"], str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", package_artifact["digest"])
        and package_artifact["expired"] is False
        and type(package_artifact.get("size_in_bytes")) is int
        and 0 < package_artifact["size_in_bytes"] <= 128 * 1024 * 1024
        and _same_workflow_run(package_artifact, run["id"], head),
        "candidate package artifact identity mismatch",
    )
    package_bundle = evidence["package_bundle"]
    require(
        package_bundle["archive_sha256"] == package_artifact["digest"].removeprefix("sha256:")
        and package_bundle["receipt_sha256"] == package["receipt_sha256"],
        "downloaded package archive or receipt digest mismatch",
    )
    require(
        package_bundle["receipt_sha256"] == sha256(encoded(package_bundle["receipt"]))
        and package_bundle["inventory_sha256"] == sha256(encoded(package_bundle["inventory"])),
        "package JSON summary differs from its downloaded bytes",
    )
    acceptance_artifact = evidence["acceptance_artifact"]
    require(
        acceptance_artifact["name"] == f"prepared-envelope-acceptance-v1-{run['id']}-1"
        and type(acceptance_artifact.get("id")) is int
        and acceptance_artifact["id"] > 0
        and acceptance_artifact["id"] not in {package_artifact["id"], evidence["artifact"]["id"]}
        and acceptance_artifact["expired"] is False
        and isinstance(acceptance_artifact.get("digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", acceptance_artifact["digest"])
        and type(acceptance_artifact.get("size_in_bytes")) is int
        and 0 < acceptance_artifact["size_in_bytes"] <= MAX_ARTIFACT_BYTES
        and _same_workflow_run(acceptance_artifact, run["id"], head)
        and acceptance_artifact["digest"] == "sha256:" + evidence["acceptance_artifact_sha256"],
        "prepared-envelope acceptance artifact identity or archive digest mismatch",
    )
    acceptance = evidence["acceptance"]
    require(
        evidence.get("acceptance_file_sha256")
        and re.fullmatch(r"[0-9a-f]{64}", evidence["acceptance_file_sha256"]),
        "acceptance document digest is missing",
    )
    require(
        evidence["acceptance_file_sha256"] == sha256(encoded(acceptance)),
        "acceptance report summary differs from its downloaded bytes",
    )
    _check_acceptance(
        acceptance,
        identity=identity,
        run_id=run["id"],
        package_bundle=package_bundle,
    )
    artifact = evidence["artifact"]
    require(not artifact["expired"], "candidate proof artifact expired")
    require(
        artifact["name"] == f"candidate-validation-v2-{run['id']}-1",
        "artifact name mismatch",
    )
    require(
        artifact.get("digest") == "sha256:" + evidence["artifact_sha256"],
        "downloaded artifact digest mismatch",
    )
    require(
        type(artifact.get("id")) is int
        and artifact["id"] > 0
        and _same_workflow_run(artifact, run["id"], head)
        and artifact["expired"] is False
        and type(artifact.get("size_in_bytes")) is int
        and 0 < artifact["size_in_bytes"] <= MAX_ARTIFACT_BYTES,
        "artifact run identity mismatch",
    )
    return {
        "schema": "finance-verified-candidate-v2",
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
        "package": package,
        "prepared_envelope_acceptance": {
            "artifact_id": acceptance_artifact["id"],
            "artifact_name": acceptance_artifact["name"],
            "artifact_digest": acceptance_artifact["digest"],
            "document_sha256": evidence["acceptance_file_sha256"],
            "platform": acceptance["platform"],
            "release": acceptance["qualification"]["release"],
            "native_checks": acceptance["native"]["checks"],
            "installed_checks": {
                kind: result["checks"] for kind, result in acceptance["artifacts"].items()
            },
            "package_artifacts": acceptance["package"]["artifacts"],
            "totals": acceptance["totals"],
            "status": acceptance["status"],
        },
        "bridge_required": bridge_required,
        "jobs": [{key: job[key] for key in ("id", "name", "conclusion")} for job in jobs],
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "VERIFIED",
    }


def api(endpoint: str) -> Any:
    result = subprocess.check_output(["gh", "api", endpoint])
    return json.loads(result)


def unpack_proof(payload: bytes) -> dict[str, Any]:
    with _validated_zip(
        payload, maximum_archive=MAX_ARTIFACT_BYTES, maximum_total=MAX_ARTIFACT_BYTES
    )[0] as archive:
        require(
            archive.namelist() == ["candidate-validation-v2.json"],
            "unexpected proof archive members",
        )
        info = archive.infolist()[0]
        proof = json.loads(_read_bounded(archive, info, MAX_ARTIFACT_BYTES))
    require(isinstance(proof, dict), "candidate proof is not an object")
    return proof


def _named_artifact(records: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [item for item in records if item.get("name") == name]
    require(len(found) == 1, f"unique artifact is missing: {name}")
    return found[0]


def collect(repository: str, pr_number: int, run_id: int) -> dict[str, Any]:
    prefix = f"repos/{repository}"
    run = api(f"{prefix}/actions/runs/{run_id}")
    artifacts = api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
    require(
        type(artifacts.get("total_count")) is int
        and artifacts["total_count"] == len(artifacts["artifacts"])
        and artifacts["total_count"] < 100
        and len({item.get("id") for item in artifacts["artifacts"]}) == len(artifacts["artifacts"]),
        "artifact inventory truncated",
    )
    artifact = _named_artifact(artifacts["artifacts"], f"candidate-validation-v2-{run_id}-1")
    require(0 < artifact["size_in_bytes"] <= MAX_ARTIFACT_BYTES, "artifact size is invalid")
    proof_payload = subprocess.check_output(
        ["gh", "api", f"{prefix}/actions/artifacts/{int(artifact['id'])}/zip"]
    )
    proof = unpack_proof(proof_payload)
    package_ref = _object(proof.get("package"), "package reference is missing or malformed")
    package_name = f"candidate-package-v1-{run_id}-1"
    require(package_ref.get("artifact_name") == package_name, "package reference name mismatch")
    listed_package = _named_artifact(artifacts["artifacts"], package_name)
    require(listed_package["id"] == package_ref.get("artifact_id"), "package reference ID mismatch")
    package_artifact = api(f"{prefix}/actions/artifacts/{int(package_ref['artifact_id'])}")
    require(
        package_artifact.get("id") == listed_package.get("id")
        and package_artifact.get("name") == listed_package.get("name")
        and package_artifact.get("digest") == listed_package.get("digest"),
        "package artifact API records disagree",
    )
    require(
        0 < package_artifact.get("size_in_bytes", 0) <= MAX_PACKAGE_ARCHIVE_BYTES,
        "package archive size is invalid",
    )
    package_payload = subprocess.check_output(
        ["gh", "api", f"{prefix}/actions/artifacts/{int(package_artifact['id'])}/zip"]
    )
    package_bundle = unpack_package_bundle(package_payload)

    acceptance_name = f"prepared-envelope-acceptance-v1-{run_id}-1"
    listed_acceptance = _named_artifact(artifacts["artifacts"], acceptance_name)
    acceptance_artifact = api(f"{prefix}/actions/artifacts/{int(listed_acceptance['id'])}")
    require(
        acceptance_artifact.get("id") == listed_acceptance.get("id")
        and acceptance_artifact.get("name") == acceptance_name
        and acceptance_artifact.get("digest") == listed_acceptance.get("digest"),
        "prepared-envelope artifact API records disagree",
    )
    require(
        0 < acceptance_artifact.get("size_in_bytes", 0) <= MAX_ARTIFACT_BYTES,
        "prepared-envelope artifact size is invalid",
    )
    acceptance_payload = subprocess.check_output(
        ["gh", "api", f"{prefix}/actions/artifacts/{int(acceptance_artifact['id'])}/zip"]
    )
    acceptance, acceptance_file_sha256 = unpack_acceptance(acceptance_payload)
    identity = proof["identity"]
    for key in ("base_sha", "head_sha", "event_sha", "workflow_sha"):
        require(
            isinstance(identity[key], str) and SHA.fullmatch(identity[key]),
            "invalid proof SHA",
        )
    head, base = identity["head_sha"], identity["base_sha"]
    evidence = {
        "run": run,
        "artifact_total_count": artifacts["total_count"],
        "artifact_inventory": artifacts["artifacts"],
        "package_artifact": package_artifact,
        "package_bundle": package_bundle,
        "acceptance_artifact": acceptance_artifact,
        "acceptance_artifact_sha256": sha256(acceptance_payload),
        "acceptance_file_sha256": acceptance_file_sha256,
        "acceptance": acceptance,
        "artifact": artifact,
        "artifact_sha256": sha256(proof_payload),
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
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository),
        "invalid repository",
    )
    require(args.pr > 0 and args.run_id > 0, "invalid PR/run identity")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    evidence = collect(args.repository, args.pr, args.run_id)
    (args.output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )
    verified = verify(
        evidence,
        repository=args.repository,
        pr_number=args.pr,
        base=args.base,
        head=args.head,
    )
    (args.output_dir / "verified.json").write_text(
        json.dumps(verified, indent=2) + "\n", encoding="utf-8"
    )
    print(f"VERIFIED PR #{args.pr}: {args.head} from run {args.run_id}")


if __name__ == "__main__":
    main()
