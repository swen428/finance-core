#!/usr/bin/env python3
"""Deterministic pytest shard planning from measured timing artifacts.

Two subcommands are provided:

* ``build-baseline`` aggregates three or more rolling timing artifacts into a
  per-file ``pytest-shard-weights-v2`` manifest.  Each test uses the median of
  the valid samples in which it appears, so source inventories may differ.
  File weights are the sum of those medians and retain sample coverage.

* ``plan`` reads the committed manifest plus the *current* tracked test-file
  inventory (discovered from Git, never from history artifacts) and assigns
  every test file to two or four shards using deterministic LPT
  (longest-processing-time-first).  New test files without historical timing
  receive the manifest's P95 fallback weight; baseline files that no longer
  exist are ignored and reported.  Planning fails closed when more than 5% of
  the current files are unknown or the trusted baseline is older than 30 days.

Safety properties
-----------------
* Standard-library only.
* Rejects oversized, corrupt, partial, or inconsistent input.
* Rejects unsafe paths (absolute, ``..``, control characters, non-test paths).
* Deterministic: identical input produces byte-identical output.
* Never modifies test outcomes or gate results.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import hashlib
import html
import json
import math
import re
import statistics
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Schemas and constants
# ---------------------------------------------------------------------------

LEGACY_TIMING_SCHEMA = "pytest-timing-v1"
BASELINE_SCHEMA = "pytest-shard-weights-v2"
PLAN_SCHEMA = "pytest-shard-plan-v1"
AGGREGATION_METHOD = "available_test_median_then_sum_by_file"

DEFAULT_SHARD_COUNT = 4
ALLOWED_SHARD_COUNTS = frozenset({2, 4})
MIN_SOURCE_RUNS = 3
MAX_BASELINE_AGE_DAYS = 30
MAX_UNKNOWN_FILE_PERCENT = 5

# Rolling timing samples come from successful first-attempt public main pushes.
# Retired private bootstrap identities are not eligible refresh evidence.
# GitHub's immutable public repository ID preserves verifiable provenance
# without introducing an owner-name exception to the source privacy checks.
TRUSTED_REPOSITORY = "github-repository-id:1375123906"
TRUSTED_WORKFLOW_PATH = ".github/workflows/validate.yml"
TRUSTED_WORKFLOW_EVENT = "push"
TRUSTED_RUN_STATUS = "completed"
TRUSTED_RUN_CONCLUSION = "success"
TRUSTED_ARTIFACT_KIND = "pytest-timing-v1"

# Resource limits.
MAX_TEST_FILES = 10_000
MAX_TEST_ENTRIES = 50_000
MAX_FIELD_LENGTH = 500
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
SLOWEST_N = 25

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_BASELINE_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "aggregation",
        "baseline_at",
        "source_runs",
        "total_tests",
        "total_test_samples",
        "total_files",
        "ignored_historical_test_count",
        "ignored_historical_test_sample_count",
        "ignored_historical_file_count",
        "ignored_historical_files",
        "unknown_file_weight_seconds",
        "file_weights_seconds",
        "file_sample_coverage",
    }
)
_SOURCE_RUN_KEYS = frozenset(
    {
        "run_id",
        "run_attempt",
        "artifact_id",
        "head_sha",
        "created_at",
        "repository",
        "workflow_path",
        "event",
        "status",
        "conclusion",
        "artifact_name",
        "artifact_sha256",
    }
)
_SOURCE_SPEC_KEYS = _SOURCE_RUN_KEYS | {"path"}
_FILE_COVERAGE_KEYS = frozenset(
    {"test_count", "sample_count", "source_run_count", "sample_coverage"}
)
_TEST_ENTRY_KEYS = frozenset({"id", "outcome", "duration_seconds"})
_OUTCOMES = ("passed", "failed", "skipped", "error")
_TIMING_BASE_KEYS = frozenset(
    {
        "schema",
        "complete",
        "error",
        "pytest_exit_code",
        "total_tests",
        "counts",
        "total_duration_seconds",
        "slowest",
        "tests",
    }
)
_RFC3339_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class ShardPlanningError(ValueError):
    """Shard planning cannot be completed safely."""

    def __init__(self, message: str, *, error_code: str = "shard_planning_failed"):
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------


def _sanitize(value: str) -> str:
    cleaned = "".join(ch for ch in value if ord(ch) >= 32)
    return html.escape(cleaned, quote=True).replace("|", "&#124;")


# ---------------------------------------------------------------------------
# Test-file inventory discovery
# ---------------------------------------------------------------------------


def _read_pytest_discovery(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (testpaths, python_files) from pytest.ini with pytest defaults."""
    testpaths: tuple[str, ...] = ("tests",)
    python_files: tuple[str, ...] = ("test_*.py", "*_test.py")
    ini = root / "pytest.ini"
    if ini.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(ini, encoding="utf-8")
        except (configparser.Error, UnicodeError) as exc:
            raise ShardPlanningError("pytest.ini could not be parsed") from exc
        if parser.has_section("pytest"):
            raw_testpaths = parser.get("pytest", "testpaths", fallback="tests")
            testpaths = tuple(raw_testpaths.split()) or ("tests",)
            raw_python_files = parser.get("pytest", "python_files", fallback="test_*.py *_test.py")
            python_files = tuple(raw_python_files.split()) or ("test_*.py", "*_test.py")
    return testpaths, python_files


def discover_test_files(repository_root: str | Path) -> tuple[str, ...]:
    """Discover the current tracked test-file inventory from Git.

    The inventory comes from ``git ls-files`` filtered by the pytest
    ``python_files`` patterns under the configured ``testpaths``.  It never
    relies on historical timing artifacts to decide which tests exist.
    """
    root = Path(repository_root)
    testpaths, python_files = _read_pytest_discovery(root)
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--", *testpaths],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ShardPlanningError("git ls-files timed out") from exc
    if completed.returncode != 0:
        raise ShardPlanningError("git ls-files could not be evaluated")
    fields = completed.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    files: list[str] = []
    for field in fields:
        try:
            path = field.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ShardPlanningError("tracked path is not valid UTF-8") from exc
        basename = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(basename, pattern) for pattern in python_files):
            files.append(path)
    if len(files) > MAX_TEST_FILES:
        raise ShardPlanningError("too many test files discovered")
    return tuple(sorted(files))


def _module_to_file_map(test_files: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for file in test_files:
        module = file[: -len(".py")].replace("/", ".") if file.endswith(".py") else file
        mapping[module] = file
    return mapping


def _map_test_id_to_file(test_id: str, module_map: dict[str, str]) -> str:
    """Map one JUnit-style test id to a unique tracked test file."""
    classname = test_id.split("::", 1)[0]
    parts = classname.split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in module_map:
            return module_map[candidate]
    raise ShardPlanningError(f"test id could not be mapped to a test file: {test_id}")


def _historical_test_id_to_file(test_id: str) -> str:
    """Recover a canonical former test path from an otherwise valid test id.

    Rolling timing input may retain tests from files that were deleted or
    renamed after the run.  Only a syntactically valid test-module prefix is
    accepted here; arbitrary unmapped ids still fail closed.
    """
    classname = test_id.split("::", 1)[0]
    parts = classname.split(".")
    if not parts or parts[0] != "tests" or any(not part.isidentifier() for part in parts):
        raise ShardPlanningError(f"test id could not be mapped to a test file: {test_id}")
    for index, part in enumerate(parts[1:], start=1):
        filename = f"{part}.py"
        if fnmatch.fnmatch(filename, "test_*.py") or fnmatch.fnmatch(filename, "*_test.py"):
            return _validate_manifest_path("/".join(parts[: index + 1]) + ".py")
    raise ShardPlanningError(f"test id could not be mapped to a test file: {test_id}")


# ---------------------------------------------------------------------------
# Timing artifact loading and validation (build-baseline input)
# ---------------------------------------------------------------------------


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ShardPlanningError(f"provenance {field} must be RFC3339 UTC")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ShardPlanningError(f"provenance {field} is invalid") from exc


def _validate_source_spec(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict) or set(spec) != _SOURCE_SPEC_KEYS:
        raise ShardPlanningError("provenance source spec keys are invalid")
    path = spec["path"]
    if not isinstance(path, str) or not path:
        raise ShardPlanningError("provenance artifact path is invalid")
    for field in ("run_id", "artifact_id"):
        value = spec[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ShardPlanningError(f"provenance {field} must be a positive integer")
    if (
        not isinstance(spec["run_attempt"], int)
        or isinstance(spec["run_attempt"], bool)
        or spec["run_attempt"] != 1
    ):
        raise ShardPlanningError("provenance run_attempt must be exactly 1")
    head_sha = spec["head_sha"]
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None:
        raise ShardPlanningError("provenance head_sha must be a full commit SHA")
    created_at = spec["created_at"]
    _parse_utc_timestamp(created_at, field="created_at")
    expected_values = {
        "repository": TRUSTED_REPOSITORY,
        "workflow_path": TRUSTED_WORKFLOW_PATH,
        "event": TRUSTED_WORKFLOW_EVENT,
        "status": TRUSTED_RUN_STATUS,
        "conclusion": TRUSTED_RUN_CONCLUSION,
        "artifact_name": f"{TRUSTED_ARTIFACT_KIND}-{spec['run_id']}-1",
    }
    for field, expected in expected_values.items():
        if spec[field] != expected:
            raise ShardPlanningError(f"provenance {field} is not trusted")
    artifact_sha256 = spec["artifact_sha256"]
    if (
        not isinstance(artifact_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
    ):
        raise ShardPlanningError("provenance artifact_sha256 must be a lowercase SHA-256")
    return {
        "path": path,
        "run_id": spec["run_id"],
        "run_attempt": spec["run_attempt"],
        "artifact_id": spec["artifact_id"],
        "head_sha": head_sha.lower(),
        "created_at": created_at,
        **expected_values,
        "artifact_sha256": artifact_sha256,
    }


def _load_timing_artifact(
    path: Path, *, allow_timing_v1: bool, expected_sha256: str
) -> dict[str, float]:
    """Load one successful timing artifact and return ``{test_id: duration}``."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ShardPlanningError(f"timing artifact could not be read: {path}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ShardPlanningError(f"timing artifact too large: {path}")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ShardPlanningError(f"timing artifact hash mismatch: {path}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ShardPlanningError(f"timing artifact is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ShardPlanningError(f"timing artifact is not an object: {path}")
    payload_keys = set(payload)
    if payload_keys not in (_TIMING_BASE_KEYS, _TIMING_BASE_KEYS | {"plan_digest"}):
        raise ShardPlanningError(f"timing artifact keys are invalid: {path}")
    if "plan_digest" in payload and (
        not isinstance(payload["plan_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", payload["plan_digest"]) is None
    ):
        raise ShardPlanningError(f"timing artifact plan digest is invalid: {path}")
    if payload.get("schema") != LEGACY_TIMING_SCHEMA:
        raise ShardPlanningError(f"timing artifact schema mismatch: {path}")
    if not allow_timing_v1:
        raise ShardPlanningError("pytest-timing-v1 requires the explicit compatibility option")
    if payload.get("complete") is not True:
        raise ShardPlanningError(f"timing artifact is not complete: {path}")
    if payload.get("error") is not None:
        raise ShardPlanningError(f"timing artifact recorded an error: {path}")
    exit_code = payload.get("pytest_exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
        raise ShardPlanningError(f"timing artifact pytest exit code is not 0: {path}")
    tests = payload.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ShardPlanningError(f"timing artifact has no tests: {path}")
    if len(tests) > MAX_TEST_ENTRIES:
        raise ShardPlanningError(f"timing artifact has too many tests: {path}")
    total_tests = payload.get("total_tests")
    if (
        not isinstance(total_tests, int)
        or isinstance(total_tests, bool)
        or total_tests < 0
        or total_tests != len(tests)
    ):
        raise ShardPlanningError(f"timing artifact total_tests mismatch: {path}")

    reported_counts = payload.get("counts")
    if not isinstance(reported_counts, dict) or set(reported_counts) != set(_OUTCOMES):
        raise ShardPlanningError(f"timing artifact counts malformed: {path}")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in reported_counts.values()
    ):
        raise ShardPlanningError(f"timing artifact counts malformed: {path}")

    reported_duration = payload.get("total_duration_seconds")
    if (
        not isinstance(reported_duration, (int, float))
        or isinstance(reported_duration, bool)
        or not math.isfinite(float(reported_duration))
        or float(reported_duration) < 0
    ):
        raise ShardPlanningError(f"timing artifact total duration invalid: {path}")

    durations: dict[str, float] = {}
    computed_counts = {outcome: 0 for outcome in _OUTCOMES}
    computed_duration = 0.0
    for entry in tests:
        if not isinstance(entry, dict) or set(entry) != _TEST_ENTRY_KEYS:
            raise ShardPlanningError(f"timing artifact entry malformed: {path}")
        test_id = entry["id"]
        outcome = entry["outcome"]
        duration = entry["duration_seconds"]
        if not isinstance(test_id, str) or not test_id:
            raise ShardPlanningError(f"timing artifact test id malformed: {path}")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            raise ShardPlanningError(f"timing artifact duration missing for {test_id}")
        duration = float(duration)
        if not math.isfinite(duration) or duration < 0:
            raise ShardPlanningError(f"timing artifact duration invalid for {test_id}")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ShardPlanningError(f"timing artifact outcome invalid for {test_id}")
        if test_id in durations:
            raise ShardPlanningError(f"timing artifact duplicate test id: {test_id}")
        durations[test_id] = duration
        computed_counts[outcome] += 1
        computed_duration += duration

    slowest = payload.get("slowest")
    if not isinstance(slowest, list) or len(slowest) > SLOWEST_N:
        raise ShardPlanningError(f"timing artifact slowest entries malformed: {path}")
    slowest_ids: set[str] = set()
    for entry in slowest:
        if not isinstance(entry, dict) or set(entry) != _TEST_ENTRY_KEYS:
            raise ShardPlanningError(f"timing artifact slowest entry malformed: {path}")
        test_id = entry["id"]
        outcome = entry["outcome"]
        duration = entry["duration_seconds"]
        if (
            not isinstance(test_id, str)
            or not test_id
            or test_id in slowest_ids
            or not isinstance(outcome, str)
            or outcome not in _OUTCOMES
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or float(duration) < 0
            or entry not in tests
        ):
            raise ShardPlanningError(f"timing artifact slowest entry malformed: {path}")
        slowest_ids.add(test_id)
    expected_slowest = sorted(
        tests,
        key=lambda entry: (-float(entry["duration_seconds"] or 0.0), str(entry["id"])),
    )[:SLOWEST_N]
    if slowest != expected_slowest:
        raise ShardPlanningError(f"timing artifact slowest entries mismatch: {path}")
    if reported_counts != computed_counts or sum(reported_counts.values()) != total_tests:
        raise ShardPlanningError(f"timing artifact counts mismatch: {path}")
    if not math.isclose(float(reported_duration), computed_duration, abs_tol=0.0015):
        raise ShardPlanningError(f"timing artifact total duration mismatch: {path}")
    return durations


def _percentile_95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    return statistics.quantiles(ordered, n=100, method="inclusive")[94]


def build_baseline(
    artifact_specs: Sequence[dict[str, Any]],
    *,
    repository_root: str | Path,
    allow_timing_v1: bool = False,
) -> dict[str, Any]:
    """Aggregate timing artifacts into a deterministic per-file weight manifest."""
    raw_specs = list(artifact_specs)
    if len(raw_specs) < MIN_SOURCE_RUNS:
        raise ShardPlanningError(f"at least {MIN_SOURCE_RUNS} source artifacts are required")

    specs = [_validate_source_spec(spec) for spec in raw_specs]
    run_keys = [(spec["run_id"], spec["run_attempt"]) for spec in specs]
    artifact_ids = [spec["artifact_id"] for spec in specs]
    if len(run_keys) != len(set(run_keys)) or len(artifact_ids) != len(set(artifact_ids)):
        raise ShardPlanningError("provenance source runs and artifact ids must be unique")
    specs.sort(key=lambda item: (item["run_id"], item["run_attempt"], item["artifact_id"]))

    inventory = discover_test_files(repository_root)
    module_map = _module_to_file_map(inventory)
    inventory_set = frozenset(inventory)

    per_artifact: list[dict[str, float]] = []
    for spec in specs:
        durations = _load_timing_artifact(
            Path(spec["path"]),
            allow_timing_v1=allow_timing_v1,
            expected_sha256=spec["artifact_sha256"],
        )
        per_artifact.append(durations)

    # Median duration per test id across the artifacts where it is present,
    # then sum those medians per file.
    test_ids = frozenset(test_id for durations in per_artifact for test_id in durations)
    file_weights: dict[str, float] = {}
    file_tests: dict[str, set[str]] = {}
    file_sample_counts: dict[str, int] = {}
    file_source_runs: dict[str, set[int]] = {}
    ignored_historical_tests: set[str] = set()
    ignored_historical_files: set[str] = set()
    ignored_historical_sample_count = 0
    for test_id in sorted(test_ids):
        samples = [durations[test_id] for durations in per_artifact if test_id in durations]
        median_duration = statistics.median(samples)
        try:
            file = _map_test_id_to_file(test_id, module_map)
        except ShardPlanningError:
            historical_file = _historical_test_id_to_file(test_id)
            if historical_file in inventory_set:
                raise
            ignored_historical_tests.add(test_id)
            ignored_historical_files.add(historical_file)
            ignored_historical_sample_count += len(samples)
            continue
        if file not in inventory_set:
            raise ShardPlanningError(f"mapped file is not a tracked test file: {file}")
        file_weights[file] = file_weights.get(file, 0.0) + median_duration
        file_tests.setdefault(file, set()).add(test_id)
        file_sample_counts[file] = file_sample_counts.get(file, 0) + len(samples)
        source_indexes = file_source_runs.setdefault(file, set())
        source_indexes.update(
            index for index, durations in enumerate(per_artifact) if test_id in durations
        )

    weights = list(file_weights.values())
    unknown_weight = _percentile_95(weights)

    source_runs = [{key: spec[key] for key in _SOURCE_RUN_KEYS} for spec in specs]
    baseline_at = max(
        source_runs,
        key=lambda item: _parse_utc_timestamp(item["created_at"], field="created_at"),
    )["created_at"]
    coverage = {
        file: {
            "test_count": len(file_tests[file]),
            "sample_count": file_sample_counts[file],
            "source_run_count": len(file_source_runs[file]),
            "sample_coverage": round(
                file_sample_counts[file] / (len(file_tests[file]) * len(per_artifact)), 6
            ),
        }
        for file in sorted(file_weights)
    }

    return {
        "schema": BASELINE_SCHEMA,
        "aggregation": AGGREGATION_METHOD,
        "baseline_at": baseline_at,
        "source_runs": source_runs,
        "total_tests": sum(len(tests) for tests in file_tests.values()),
        "total_test_samples": sum(file_sample_counts.values()),
        "total_files": len(file_weights),
        "ignored_historical_test_count": len(ignored_historical_tests),
        "ignored_historical_test_sample_count": ignored_historical_sample_count,
        "ignored_historical_file_count": len(ignored_historical_files),
        "ignored_historical_files": sorted(ignored_historical_files),
        "unknown_file_weight_seconds": round(unknown_weight, 3),
        "file_weights_seconds": {
            file: round(weight, 3) for file, weight in sorted(file_weights.items())
        },
        "file_sample_coverage": coverage,
    }


# ---------------------------------------------------------------------------
# Manifest loading and validation (plan input)
# ---------------------------------------------------------------------------


def _validate_manifest_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ShardPlanningError("manifest path must be a non-empty string")
    if len(path) > MAX_FIELD_LENGTH:
        raise ShardPlanningError("manifest path too long")
    if path.startswith("/") or path.startswith("./") or "\\" in path or "//" in path:
        raise ShardPlanningError(f"unsafe manifest path: {path!r}")
    if _CONTROL_CHARS_RE.search(path):
        raise ShardPlanningError("manifest path contains control characters")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ShardPlanningError(f"unsafe manifest path: {path!r}")
    if not path.endswith(".py"):
        raise ShardPlanningError(f"manifest path is not a Python file: {path!r}")
    basename = path.rsplit("/", 1)[-1]
    if not (fnmatch.fnmatch(basename, "test_*.py") or fnmatch.fnmatch(basename, "*_test.py")):
        raise ShardPlanningError(f"manifest path is not a test file: {path!r}")
    return path


def load_baseline_manifest(path: str | Path) -> dict[str, Any]:
    """Load and strictly validate a pytest-shard-weights-v2 manifest."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ShardPlanningError("baseline manifest could not be read") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ShardPlanningError("baseline manifest too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ShardPlanningError("baseline manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _BASELINE_REQUIRED_KEYS:
        raise ShardPlanningError("baseline manifest keys are incorrect")
    if payload["schema"] != BASELINE_SCHEMA:
        raise ShardPlanningError("baseline manifest schema mismatch")
    if payload["aggregation"] != AGGREGATION_METHOD:
        raise ShardPlanningError("baseline manifest aggregation method mismatch")

    source_runs = payload["source_runs"]
    if not isinstance(source_runs, list) or len(source_runs) < MIN_SOURCE_RUNS:
        raise ShardPlanningError("baseline manifest requires at least three source runs")
    normalized_sources: list[dict[str, Any]] = []
    for run in source_runs:
        if not isinstance(run, dict) or set(run) != _SOURCE_RUN_KEYS:
            raise ShardPlanningError("baseline manifest provenance source run malformed")
        normalized_sources.append(_validate_source_spec({**run, "path": "manifest-source"}))
    source_sort_keys = [
        (run["run_id"], run["run_attempt"], run["artifact_id"]) for run in normalized_sources
    ]
    if source_sort_keys != sorted(source_sort_keys):
        raise ShardPlanningError("baseline manifest provenance is not canonical")
    if len({key[:2] for key in source_sort_keys}) != len(source_sort_keys) or len(
        {key[2] for key in source_sort_keys}
    ) != len(source_sort_keys):
        raise ShardPlanningError("baseline manifest provenance is duplicated")
    newest_source = max(
        _parse_utc_timestamp(run["created_at"], field="created_at") for run in normalized_sources
    )
    baseline_at = _parse_utc_timestamp(payload["baseline_at"], field="baseline_at")
    if baseline_at != newest_source:
        raise ShardPlanningError("baseline manifest baseline_at does not match provenance")

    unknown_weight = payload["unknown_file_weight_seconds"]
    if (
        not isinstance(unknown_weight, (int, float))
        or isinstance(unknown_weight, bool)
        or not math.isfinite(float(unknown_weight))
        or unknown_weight < 0
    ):
        raise ShardPlanningError("baseline manifest unknown_file_weight invalid")

    file_weights = payload["file_weights_seconds"]
    if not isinstance(file_weights, dict) or not file_weights:
        raise ShardPlanningError("baseline manifest file weights missing")
    if len(file_weights) > MAX_TEST_FILES:
        raise ShardPlanningError("baseline manifest has too many files")
    for path_key, weight in file_weights.items():
        _validate_manifest_path(path_key)
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(float(weight))
            or weight < 0
        ):
            raise ShardPlanningError(f"baseline manifest weight invalid: {path_key}")

    for field in ("total_files", "total_tests", "total_test_samples"):
        count = payload[field]
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ShardPlanningError(f"baseline manifest {field} invalid")
    if payload["total_files"] != len(file_weights):
        raise ShardPlanningError("baseline manifest total_files mismatch")
    coverage = payload["file_sample_coverage"]
    if not isinstance(coverage, dict) or set(coverage) != set(file_weights):
        raise ShardPlanningError("baseline manifest file sample coverage mismatch")
    computed_tests = 0
    computed_samples = 0
    source_count = len(source_runs)
    for path_key, value in coverage.items():
        if not isinstance(value, dict) or set(value) != _FILE_COVERAGE_KEYS:
            raise ShardPlanningError(f"baseline manifest coverage malformed: {path_key}")
        test_count = value["test_count"]
        sample_count = value["sample_count"]
        run_count = value["source_run_count"]
        sample_coverage = value["sample_coverage"]
        if any(
            not isinstance(number, int) or isinstance(number, bool) or number <= 0
            for number in (test_count, sample_count, run_count)
        ):
            raise ShardPlanningError(f"baseline manifest coverage counts invalid: {path_key}")
        if run_count > source_count or sample_count > test_count * source_count:
            raise ShardPlanningError(f"baseline manifest coverage counts inconsistent: {path_key}")
        expected_coverage = round(sample_count / (test_count * source_count), 6)
        if (
            not isinstance(sample_coverage, (int, float))
            or isinstance(sample_coverage, bool)
            or not math.isfinite(float(sample_coverage))
            or float(sample_coverage) != expected_coverage
        ):
            raise ShardPlanningError(f"baseline manifest sample coverage invalid: {path_key}")
        computed_tests += test_count
        computed_samples += sample_count
    if payload["total_tests"] != computed_tests:
        raise ShardPlanningError("baseline manifest total_tests mismatch")
    if payload["total_test_samples"] != computed_samples:
        raise ShardPlanningError("baseline manifest total_test_samples mismatch")

    ignored_tests = payload["ignored_historical_test_count"]
    ignored_samples = payload["ignored_historical_test_sample_count"]
    ignored_file_count = payload["ignored_historical_file_count"]
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 0
        for number in (ignored_tests, ignored_samples, ignored_file_count)
    ):
        raise ShardPlanningError("baseline manifest ignored historical counts invalid")
    ignored_files = payload["ignored_historical_files"]
    if not isinstance(ignored_files, list):
        raise ShardPlanningError("baseline manifest ignored historical files invalid")
    if ignored_files != sorted(ignored_files) or len(ignored_files) != len(set(ignored_files)):
        raise ShardPlanningError("baseline manifest ignored historical files not canonical")
    for ignored_file in ignored_files:
        _validate_manifest_path(ignored_file)
    if ignored_file_count != len(ignored_files):
        raise ShardPlanningError("baseline manifest ignored historical file count mismatch")
    if set(ignored_files) & set(file_weights):
        raise ShardPlanningError("baseline manifest historical files overlap current weights")
    if ignored_samples < ignored_tests:
        raise ShardPlanningError("baseline manifest ignored historical counts inconsistent")
    if (ignored_tests == 0) != (ignored_samples == 0):
        raise ShardPlanningError("baseline manifest ignored historical counts inconsistent")
    if (ignored_tests == 0) != (ignored_file_count == 0):
        raise ShardPlanningError("baseline manifest ignored historical counts inconsistent")
    return payload


# ---------------------------------------------------------------------------
# Deterministic LPT shard assignment
# ---------------------------------------------------------------------------


def assign_shards(
    file_weights: dict[str, float],
    *,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> list[list[str]]:
    """Assign files to shards using deterministic LPT.

    Files are ordered by weight descending (ties broken by path ascending) and
    each file is placed on the shard with the smallest current estimated total
    weight (ties broken by the smallest shard index).
    """
    ordered = sorted(file_weights.items(), key=lambda item: (-item[1], item[0]))
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for file, weight in ordered:
        target = min(range(shard_count), key=lambda idx: (totals[idx], idx))
        shards[target].append(file)
        totals[target] += weight
    return shards


def _plan_digest(shard_count: int, shards: list[list[str]]) -> str:
    """Return the canonical identity of the shard-count and assignments."""
    canonical = json.dumps(
        {
            "shard_count": shard_count,
            "indexes": list(range(shard_count)),
            "assignments": [
                {"index": index, "files": sorted(shards[index])} for index in range(shard_count)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_shards(
    manifest: dict[str, Any],
    *,
    repository_root: str | Path,
    shard_count: int = DEFAULT_SHARD_COUNT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic shard plan for the current test inventory."""
    if shard_count not in ALLOWED_SHARD_COUNTS:
        raise ShardPlanningError("shard count must be 2 or 4")

    inventory = discover_test_files(repository_root)
    if not inventory:
        raise ShardPlanningError("test inventory is empty")
    inventory_set = frozenset(inventory)

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ShardPlanningError("current planning time must be timezone-aware")
    current_time = current_time.astimezone(UTC)
    baseline_time = _parse_utc_timestamp(manifest["baseline_at"], field="baseline_at")
    baseline_age = current_time - baseline_time
    if baseline_age < timedelta(0):
        raise ShardPlanningError("trusted timing baseline is dated in the future")
    if baseline_age > timedelta(days=MAX_BASELINE_AGE_DAYS):
        raise ShardPlanningError("trusted timing baseline is older than 30 days")

    baseline_weights = manifest["file_weights_seconds"]
    unknown_weight = float(manifest["unknown_file_weight_seconds"])

    effective_weights: dict[str, float] = {}
    new_files: list[str] = []
    for file in inventory:
        if file in baseline_weights:
            effective_weights[file] = float(baseline_weights[file])
        else:
            effective_weights[file] = unknown_weight
            new_files.append(file)

    deleted_files = sorted(set(baseline_weights) - inventory_set)
    unknown_count = len(new_files)
    known_count = len(inventory) - unknown_count
    if unknown_count * 100 > len(inventory) * MAX_UNKNOWN_FILE_PERCENT:
        raise ShardPlanningError("unknown test files exceed the 5% inventory limit")
    known_coverage = round(known_count / len(inventory), 6)

    shards = assign_shards(effective_weights, shard_count=shard_count)

    # Coverage proof before any output is produced.
    assigned = [file for shard in shards for file in shard]
    if len(shards) != shard_count or any(not shard for shard in shards):
        raise ShardPlanningError("shard plan produced an empty shard")
    if len(assigned) != len(set(assigned)):
        raise ShardPlanningError("shard plan assigned a file more than once")
    if frozenset(assigned) != inventory_set:
        raise ShardPlanningError("shard plan does not cover the test inventory exactly")
    for shard in shards:
        for file in shard:
            _validate_manifest_path(file)

    sorted_shards = [sorted(shard) for shard in shards]
    totals = [round(sum(effective_weights[file] for file in shard), 3) for shard in sorted_shards]

    indexes = list(range(shard_count))

    return {
        "schema": PLAN_SCHEMA,
        "shard_count": shard_count,
        "indexes": indexes,
        "total_files": len(inventory),
        "shards": [
            {
                "index": index,
                "file_count": len(sorted_shards[index]),
                "estimated_weight_seconds": totals[index],
                "files": sorted_shards[index],
            }
            for index in indexes
        ],
        "new_files": sorted(new_files),
        "deleted_files": deleted_files,
        "known_file_count": known_count,
        "unknown_file_count": unknown_count,
        "known_file_coverage": known_coverage,
        "baseline_at": manifest["baseline_at"],
        "unknown_file_weight_seconds": round(unknown_weight, 3),
        "digest": _plan_digest(shard_count, sorted_shards),
    }


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def render_plan_summary(plan: dict[str, Any]) -> str:
    lines = [
        "## Pytest shard plan",
        "",
        f"- Shard count: {plan['shard_count']}",
        f"- Total test files: {plan['total_files']}",
        f"- Known test files: {plan['known_file_count']}",
        f"- Unknown test files: {plan['unknown_file_count']}",
        f"- Known-file coverage: {plan['known_file_coverage']:.2%}",
        f"- Baseline timestamp: <code>{_sanitize(plan['baseline_at'])}</code>",
        f"- Plan digest: <code>{_sanitize(plan['digest'])}</code>",
        "",
        "| Shard | Files | Estimated weight (s) |",
        "| --- | --- | --- |",
    ]
    for shard in plan["shards"]:
        lines.append(
            f"| {shard['index']} | {shard['file_count']} | {shard['estimated_weight_seconds']} |"
        )
    if plan["new_files"]:
        lines.extend(["", "### New test files (P95 fallback weight)", ""])
        lines.extend(f"- <code>{_sanitize(f)}</code>" for f in plan["new_files"])
    if plan["deleted_files"]:
        lines.extend(["", "### Baseline files no longer present (ignored)", ""])
        lines.extend(f"- <code>{_sanitize(f)}</code>" for f in plan["deleted_files"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_source_spec(spec: str) -> dict[str, Any]:
    fields = spec.split(",")
    if len(fields) != 12:
        raise ShardPlanningError(
            "source spec must be run_id,run_attempt,artifact_id,head_sha,created_at,"
            "repository,workflow_path,event,status,conclusion,artifact_name,artifact_sha256"
        )
    (
        run_id,
        run_attempt,
        artifact_id,
        head_sha,
        created_at,
        repository,
        workflow_path,
        event,
        status,
        conclusion,
        artifact_name,
        artifact_sha256,
    ) = fields
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", run_attempt):
        raise ShardPlanningError("source spec run_id/run_attempt must be integers")
    if not re.fullmatch(r"[0-9]+", artifact_id):
        raise ShardPlanningError("source spec artifact_id must be an integer")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise ShardPlanningError("source spec head_sha must be a commit SHA")
    return {
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "artifact_id": int(artifact_id),
        "head_sha": head_sha,
        "created_at": created_at,
        "repository": repository,
        "workflow_path": workflow_path,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "artifact_name": artifact_name,
        "artifact_sha256": artifact_sha256,
    }


def _build_baseline_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="plan_pytest_shards.py build-baseline")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Path to a pytest-timing-v1.json artifact (repeatable)",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "run_id,run_attempt,artifact_id,head_sha,created_at,repository,workflow_path,"
            "event,status,conclusion,artifact_name,artifact_sha256 "
            "(repeatable, pairs with --artifact)"
        ),
    )
    parser.add_argument(
        "--allow-timing-v1",
        action="store_true",
        help="explicitly import retained pytest-timing-v1 compatibility artifacts",
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    try:
        if len(args.artifact) != len(args.source):
            raise ShardPlanningError("--artifact and --source counts must match")
        specs = []
        for artifact_path, source in zip(args.artifact, args.source):
            spec = _parse_source_spec(source)
            spec["path"] = artifact_path
            specs.append(spec)
        manifest = build_baseline(
            specs,
            repository_root=args.repository_root,
            allow_timing_v1=args.allow_timing_v1,
        )
    except ShardPlanningError as exc:
        print(f"build-baseline failed closed: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"baseline written: {manifest['total_files']} files, "
        f"{manifest['total_tests']} tests from {len(manifest['source_runs'])} runs; "
        f"ignored {manifest['ignored_historical_test_count']} historical tests "
        f"from {manifest['ignored_historical_file_count']} files"
    )
    return 0


def _plan_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="plan_pytest_shards.py plan")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument(
        "--now",
        default="",
        help="RFC3339 UTC planning time (tests/reproducibility only; defaults to current UTC)",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        choices=tuple(sorted(ALLOWED_SHARD_COUNTS)),
        default=DEFAULT_SHARD_COUNT,
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_baseline_manifest(args.baseline)
        planning_time = _parse_utc_timestamp(args.now, field="now") if args.now else None
        plan = plan_shards(
            manifest,
            repository_root=args.repository_root,
            shard_count=args.shard_count,
            now=planning_time,
        )
    except ShardPlanningError as exc:
        print(f"plan failed closed: {exc}", file=sys.stderr)
        if args.summary:
            try:
                with open(args.summary, "a", encoding="utf-8") as handle:
                    handle.write(
                        "## Pytest shard plan\n\n"
                        "- Status: **Fail closed**\n"
                        f"- Reason: <code>{_sanitize(str(exc))}</code>\n"
                    )
            except OSError:
                pass
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for shard in plan["shards"]:
        (output_dir / f"shard-{shard['index']}.txt").write_text(
            "\n".join(shard["files"]) + "\n", encoding="utf-8"
        )
    summary_text = render_plan_summary(plan)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary_text)
    print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args[:1] == ["build-baseline"]:
        return _build_baseline_cli(args[1:])
    if args[:1] == ["plan"]:
        return _plan_cli(args[1:])
    print("usage: plan_pytest_shards.py {build-baseline|plan} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
