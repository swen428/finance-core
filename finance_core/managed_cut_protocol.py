"""Fixed descriptor protocol for a synthetic managed Core snapshot child.

This is an internal spawn entry, not a command runner or a gate-acquisition API.
The parent owns the exclusive gate. Descriptors and the one-use control channel
are supplied only by its fixed coordinator; request JSON never selects paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finance_core.profile_paths import (
    MANAGED_STAGING_FILENAME,
    ManagedStagingProfile,
    _reject_acl_grants,
    validate_registered_staging_profile,
)

CONTROL_FD = 3
GATE_FD = 4
PROFILE_FD = 5
STAGE_FD = 6
MAX_FRAME = 8192
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_ROLE = re.compile(r"core-cut-[0-9a-f]{32}\Z")
BUNDLE_VERSION = "delegated-cut-bundle-v1"
BUNDLE_SCOPE = "core_committed_snapshot"
BUNDLE_REGISTRY_VERSION = "core-attachment-reference-registry-v1"
BUNDLE_LIMITS_VERSION = "core-snapshot-bundle-limits-v1"
BUNDLE_LIMITS = {
    "max_core_db_bytes": 67_108_864,
    "max_db_stage_bytes": 134_217_728,
    "max_attachment_bytes": 20_971_520,
    "max_attachment_members": 4096,
    "max_references": 65_536,
    "max_manifest_bytes": 1_048_576,
    "max_stage_bytes": 268_435_456,
    "min_free_bytes": 67_108_864,
    "backup_pages_per_step": 256,
}
BUNDLE_STAGED_KEYS = frozenset(
    {
        "bundle_stage_dev",
        "bundle_stage_ino",
        "db_stage_dev",
        "db_stage_ino",
        "db_output_dev",
        "db_output_ino",
        "db_bytes",
        "db_sha256",
        "db_page_count",
        "manifest_sha256",
        "manifest_bytes",
        "member_count",
        "member_bytes",
        "reference_count",
        "reference_sha256",
        "snapshot_recorded_at",
        "package_completed_at",
        "schema_fingerprint",
        "migration_ledger_sha256",
        "migration_ledger_count",
    }
)
_ATTEMPTED_VERSION = "delegated-cut-worker-v1"


def failure_version() -> str:
    """Select a fixed failure vocabulary after a rejected one-use first frame."""
    return _ATTEMPTED_VERSION


class ManagedCutProtocolError(RuntimeError):
    """The delegated child received an invalid or expired internal session."""


@dataclass(frozen=True)
class CutRequest:
    cut_id: str
    worker_id: str
    profile_id: str
    registration_sha256: str
    artifact_sha256: str
    schema_sha256: str
    limits_sha256: str
    remaining_ms: int
    limits: dict[str, int]
    operation: str
    staged: dict[str, Any] | None
    scope: str | None = None
    registry_version: str | None = None
    limits_version: str | None = None
    core_version: str | None = None
    core_api_contract_version: str | None = None


def _exact_object(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ManagedCutProtocolError("Invalid fixed cut frame")
    return value


def _decode_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManagedCutProtocolError("Duplicate cut frame field")
        result[key] = value
    return result


def read_frame(deadline: float) -> dict[str, Any]:
    chunks = bytearray()
    while len(chunks) <= MAX_FRAME:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ManagedCutProtocolError("Cut control deadline expired")
        ready, _, _ = select.select([CONTROL_FD], [], [], remaining)
        if not ready:
            raise ManagedCutProtocolError("Cut control deadline expired")
        data = os.read(CONTROL_FD, 1)
        if not data:
            raise ManagedCutProtocolError("Cut control closed")
        if data == b"\n":
            try:
                result = json.loads(chunks.decode("utf-8"), object_pairs_hook=_decode_pairs)
                if type(result) is not dict:
                    raise ManagedCutProtocolError("Invalid cut control frame")
                return result
            except (UnicodeError, ValueError, TypeError) as exc:
                raise ManagedCutProtocolError("Invalid cut control frame") from exc
        chunks.extend(data)
    raise ManagedCutProtocolError("Cut control frame exceeds limit")


def write_frame(payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_FRAME:
        raise ManagedCutProtocolError("Cut control frame exceeds limit")
    offset = 0
    while offset < len(encoded):
        count = os.write(CONTROL_FD, encoded[offset:])
        if count <= 0:
            raise ManagedCutProtocolError("Cut control closed")
        offset += count


def _check_private_fd(fd: int, path: Path, *, directory: bool, mode: int) -> None:
    opened = os.fstat(fd)
    named = path.lstat()
    if (
        (not stat.S_ISDIR(opened.st_mode) if directory else not stat.S_ISREG(opened.st_mode))
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or stat.S_IMODE(opened.st_mode) != mode
        or opened.st_uid != os.getuid()
        or (not directory and (opened.st_nlink != 1 or opened.st_size != 0))
    ):
        raise ManagedCutProtocolError("Cut descriptor role is invalid")
    _reject_acl_grants(fd, path)


def validate_descriptors(profile: ManagedStagingProfile, stage: Path, *, empty_stage: bool) -> None:
    if not _ROLE.fullmatch(stage.name) or stage.parent != profile.work:
        raise ManagedCutProtocolError("Cut stage role is invalid")
    _check_private_fd(PROFILE_FD, profile.profile, directory=True, mode=0o700)
    _check_private_fd(STAGE_FD, stage, directory=True, mode=0o700)
    _check_private_fd(
        GATE_FD, profile.profile / ".profile-gate.v1.lock", directory=False, mode=0o600
    )
    if empty_stage and os.listdir(STAGE_FD):
        raise ManagedCutProtocolError("Cut stage is not fresh")


def _digest_file(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        payload = os.read(fd, 65_537)
        if len(payload) > 65_536:
            raise ManagedCutProtocolError("Registration exceeds limit")
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(fd)


def validate_request(frame: dict[str, Any], *, expected_operation: str) -> CutRequest:
    bundle = expected_operation in {"core_bundle_snapshot", "core_bundle_readback"}
    if expected_operation not in {
        "core_snapshot",
        "core_readback",
        "core_bundle_snapshot",
        "core_bundle_readback",
    }:
        raise ManagedCutProtocolError("Cut operation is invalid")
    required = {
        "version",
        "cut_id",
        "worker_id",
        "profile_id",
        "registration_sha256",
        "artifact_sha256",
        "schema_sha256",
        "limits_sha256",
        "remaining_ms",
        "limits",
        "operation",
    }
    if bundle:
        required.update(
            {
                "scope",
                "registry_version",
                "limits_version",
                "core_version",
                "core_api_contract_version",
            }
        )
    if expected_operation in {"core_readback", "core_bundle_readback"}:
        required.add("staged")
    _exact_object(frame, required)
    expected_version = BUNDLE_VERSION if bundle else "delegated-cut-worker-v1"
    if frame["version"] != expected_version or frame["operation"] != expected_operation:
        raise ManagedCutProtocolError("Cut operation is invalid")
    if bundle:
        if (
            frame["scope"] != BUNDLE_SCOPE
            or frame["registry_version"] != BUNDLE_REGISTRY_VERSION
            or frame["limits_version"] != BUNDLE_LIMITS_VERSION
            or type(frame["core_version"]) is not str
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", frame["core_version"])
            or type(frame["core_api_contract_version"]) is not str
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", frame["core_api_contract_version"])
        ):
            raise ManagedCutProtocolError("Bundle identity is invalid")
    for field in ("cut_id", "worker_id"):
        if type(frame[field]) is not str or not _HEX32.fullmatch(frame[field]):
            raise ManagedCutProtocolError("Cut identity is invalid")
    if type(frame["profile_id"]) is not str or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,63}", frame["profile_id"]
    ):
        raise ManagedCutProtocolError("Cut profile identity is invalid")
    for field in ("registration_sha256", "artifact_sha256", "schema_sha256", "limits_sha256"):
        if type(frame[field]) is not str or not _HEX64.fullmatch(frame[field]):
            raise ManagedCutProtocolError("Cut digest is invalid")
    limits = _exact_object(
        frame["limits"],
        set(BUNDLE_LIMITS)
        if bundle
        else {"max_core_db_bytes", "max_stage_bytes", "min_free_bytes", "backup_pages_per_step"},
    )
    if any(type(value) is not int or value <= 0 or value > 2**63 - 1 for value in limits.values()):
        raise ManagedCutProtocolError("Cut limits are invalid")
    encoded = json.dumps(limits, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != frame["limits_sha256"]:
        raise ManagedCutProtocolError("Cut limits digest differs")
    if bundle and limits != BUNDLE_LIMITS:
        raise ManagedCutProtocolError("Bundle limits differ")
    duration = frame["remaining_ms"]
    if type(duration) is not int or not 0 < duration <= 30_000:
        raise ManagedCutProtocolError("Cut duration is invalid")
    staged = frame.get("staged")
    if expected_operation == "core_readback":
        staged = _exact_object(
            staged,
            {
                "byte_length",
                "sha256",
                "page_count",
                "stage_dev",
                "stage_ino",
                "output_dev",
                "output_ino",
            },
        )
        if (
            any(
                type(staged[key]) is not int or staged[key] <= 0
                for key in ("byte_length", "page_count")
            )
            or type(staged["sha256"]) is not str
            or not _HEX64.fullmatch(staged["sha256"])
        ):
            raise ManagedCutProtocolError("Staged evidence is invalid")
        for key in ("stage_dev", "stage_ino", "output_dev", "output_ino"):
            canonical_decimal(staged[key])
    elif expected_operation == "core_bundle_readback":
        staged = _exact_object(staged, set(BUNDLE_STAGED_KEYS))
        for key in (
            "bundle_stage_dev",
            "bundle_stage_ino",
            "db_stage_dev",
            "db_stage_ino",
            "db_output_dev",
            "db_output_ino",
        ):
            canonical_decimal(staged[key])
        for key in ("db_bytes", "db_page_count", "manifest_bytes", "member_count", "member_bytes"):
            if type(staged[key]) is not int or staged[key] <= 0:
                raise ManagedCutProtocolError("Bundle staged count is invalid")
        for key in ("reference_count", "migration_ledger_count"):
            if type(staged[key]) is not int or staged[key] < 0:
                raise ManagedCutProtocolError("Bundle staged count is invalid")
        for key in (
            "db_sha256",
            "manifest_sha256",
            "reference_sha256",
            "schema_fingerprint",
            "migration_ledger_sha256",
        ):
            if type(staged[key]) is not str or not _HEX64.fullmatch(staged[key]):
                raise ManagedCutProtocolError("Bundle staged digest is invalid")
        for key in ("snapshot_recorded_at", "package_completed_at"):
            if type(staged[key]) is not str or not re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", staged[key]
            ):
                raise ManagedCutProtocolError("Bundle staged time is invalid")
    return CutRequest(
        **{key: frame[key] for key in required if key not in {"version", "staged"}}, staged=staged
    )


def validate_profile(
    request: CutRequest, *, empty_stage: bool
) -> tuple[ManagedStagingProfile, Path]:
    support = os.environ.get("FINANCE_CUT_APPLICATION_SUPPORT")
    profile_id = os.environ.get("FINANCE_CUT_PROFILE_ID")
    stage_path = os.environ.get("FINANCE_CUT_STAGE_PATH")
    if not support or request.profile_id != profile_id or not stage_path:
        raise ManagedCutProtocolError("Cut trusted locator is missing")
    profile = validate_registered_staging_profile(support, profile_id)
    stage = Path(stage_path)
    try:
        profile.revalidate()
        validate_descriptors(profile, stage, empty_stage=empty_stage)
        if _digest_file(profile.profile / MANAGED_STAGING_FILENAME) != request.registration_sha256:
            raise ManagedCutProtocolError("Cut registration differs")
        return profile, stage
    except BaseException:
        profile.close()
        raise


def initial_handshake(
    operation: str | tuple[str, ...],
) -> tuple[CutRequest, ManagedStagingProfile, Path, float]:
    # A closed or absent dedicated control channel refuses before any SQLite use.
    first = read_frame(time.monotonic() + 5.0)
    global _ATTEMPTED_VERSION
    _ATTEMPTED_VERSION = (
        BUNDLE_VERSION if first.get("version") == BUNDLE_VERSION else "delegated-cut-worker-v1"
    )
    selected = first.get("operation") if isinstance(operation, tuple) else operation
    if selected not in (operation if isinstance(operation, tuple) else (operation,)):
        raise ManagedCutProtocolError("Cut operation is invalid")
    request = validate_request(first, expected_operation=selected)
    deadline = time.monotonic() + request.remaining_ms / 1000
    profile, stage = validate_profile(
        request, empty_stage=selected in {"core_snapshot", "core_bundle_snapshot"}
    )
    version = BUNDLE_VERSION if selected.startswith("core_bundle_") else "delegated-cut-worker-v1"
    try:
        write_frame(
            {
                "version": version,
                "type": "ready",
                "cut_id": request.cut_id,
                "worker_id": request.worker_id,
            }
        )
        go = read_frame(deadline)
        _exact_object(go, {"version", "type", "cut_id", "worker_id"})
        if go != {
            "version": version,
            "type": "go",
            "cut_id": request.cut_id,
            "worker_id": request.worker_id,
        }:
            raise ManagedCutProtocolError("Cut handshake does not match")
        return request, profile, stage, deadline
    except BaseException:
        profile.close()
        raise


def canonical_decimal(value: Any) -> int:
    if type(value) is not str or not _DECIMAL.fullmatch(value):
        raise ManagedCutProtocolError("Cut identity must be a canonical decimal string")
    parsed = int(value)
    if parsed > 2**64 - 1:
        raise ManagedCutProtocolError("Cut identity exceeds native range")
    return parsed


def check_control_alive(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ManagedCutProtocolError("Cut control deadline expired")
    ready, _, _ = select.select([CONTROL_FD], [], [], 0)
    if ready:
        # After GO there is no further request. EOF is cancellation; any
        # unexpected byte is a protocol violation.
        os.read(CONTROL_FD, 1)
        raise ManagedCutProtocolError("Cut control was cancelled or repeated")
