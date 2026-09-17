#!/usr/bin/env python3
"""Aggregate per-shard pytest timing reports into the stable timing artifact.

After the sharded ``pytest`` matrix runs, each shard uploads its own
``pytest-timing-v1`` report.  This script merges the plan-declared shard reports into a
single stable ``pytest-timing-v1-{run_id}-{run_attempt}`` artifact with the
same top-level schema and semantics as the monolithic report produced by
``report_pytest_timing.py``.

Validation performed (fail closed):
* all expected shard reports are present with indexes exactly ``0..N-1``;
* every shard report has a valid ``pytest-timing-v1`` schema;
* no test id appears in more than one shard;
* the aggregate test ids map exactly onto the shard plan's test-file coverage;
* durations are finite and non-negative;
* per-shard counts and total duration sum correctly;
* the aggregate is complete only when every shard is complete;
* the aggregate exit code is ``0`` only when every shard exit code is ``0``.
* the aggregate carries the digest of the exact validated shard plan.

The script never emits tracebacks, captured output, environment data, secrets,
or absolute filesystem paths.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TIMING_SCHEMA = "pytest-timing-v1"
PLAN_SCHEMA = "pytest-shard-plan-v1"
ALLOWED_SHARD_COUNTS = frozenset({2, 4})
SLOWEST_N = 25
MAX_FIELD_LENGTH = 500
MAX_BASELINE_AGE_DAYS = 30
MAX_UNKNOWN_FILE_PERCENT = 5

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RFC3339_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
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
_PLAN_KEYS = frozenset(
    {
        "schema",
        "shard_count",
        "indexes",
        "total_files",
        "shards",
        "new_files",
        "deleted_files",
        "known_file_count",
        "unknown_file_count",
        "known_file_coverage",
        "baseline_at",
        "unknown_file_weight_seconds",
        "digest",
    }
)
_PLAN_SHARD_KEYS = frozenset({"index", "file_count", "estimated_weight_seconds", "files"})


class AggregationError(ValueError):
    """Timing aggregation cannot be completed safely."""

    def __init__(self, message: str, *, error_code: str = "timing_aggregation_failed"):
        super().__init__(message)
        self.error_code = error_code


def _sanitize(value: str) -> str:
    cleaned = "".join(ch for ch in value if ord(ch) >= 32)
    return html.escape(cleaned, quote=True).replace("|", "\uff5c")


def _load_shard_report(path: Path, index: int) -> dict[str, Any]:
    if not path.exists():
        raise AggregationError(
            f"shard {index} timing report is missing", error_code="shard_report_missing"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggregationError(
            f"shard {index} timing report is corrupt", error_code="shard_report_corrupt"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _TIMING_BASE_KEYS
        or payload.get("schema") != TIMING_SCHEMA
    ):
        raise AggregationError(
            f"shard {index} timing report schema mismatch", error_code="shard_schema_mismatch"
        )
    complete = payload.get("complete")
    error = payload.get("error")
    if not isinstance(complete, bool):
        raise AggregationError(
            f"shard {index} timing report complete flag is invalid",
            error_code="shard_report_corrupt",
        )
    if error is not None and (not isinstance(error, str) or not error):
        raise AggregationError(
            f"shard {index} timing report error field is invalid",
            error_code="shard_report_corrupt",
        )
    if complete and error is not None:
        raise AggregationError(
            f"shard {index} timing report complete/error state is inconsistent",
            error_code="shard_report_corrupt",
        )
    tests = payload.get("tests")
    if not isinstance(tests, list):
        raise AggregationError(
            f"shard {index} timing report has no tests", error_code="shard_report_corrupt"
        )
    total_tests = payload.get("total_tests")
    if (
        not isinstance(total_tests, int)
        or isinstance(total_tests, bool)
        or total_tests < 0
        or total_tests != len(tests)
    ):
        raise AggregationError(
            f"shard {index} timing total_tests is invalid",
            error_code="shard_report_corrupt",
        )
    reported_counts = payload.get("counts")
    if not isinstance(reported_counts, dict) or set(reported_counts) != set(_OUTCOMES):
        raise AggregationError(
            f"shard {index} timing counts are malformed",
            error_code="shard_report_corrupt",
        )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in reported_counts.values()
    ):
        raise AggregationError(
            f"shard {index} timing counts are malformed",
            error_code="shard_report_corrupt",
        )
    computed_counts = {outcome: 0 for outcome in _OUTCOMES}
    computed_duration = 0.0
    seen_ids: set[str] = set()
    for entry in tests:
        if not isinstance(entry, dict) or set(entry) != _TEST_ENTRY_KEYS:
            raise AggregationError(
                f"shard {index} timing report entry malformed", error_code="shard_report_corrupt"
            )
        test_id = entry["id"]
        outcome = entry["outcome"]
        if not isinstance(test_id, str) or not test_id or test_id in seen_ids:
            raise AggregationError(
                f"shard {index} timing report has duplicate/invalid test ids",
                error_code="shard_report_corrupt",
            )
        if outcome not in _OUTCOMES:
            raise AggregationError(
                f"shard {index} timing report outcome is invalid",
                error_code="shard_report_corrupt",
            )
        seen_ids.add(test_id)
        computed_counts[str(outcome)] += 1
        duration = entry["duration_seconds"]
        if duration is not None:
            if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                raise AggregationError(
                    f"shard {index} duration malformed", error_code="shard_duration_invalid"
                )
            duration = float(duration)
            if not math.isfinite(duration) or duration < 0:
                raise AggregationError(
                    f"shard {index} duration invalid", error_code="shard_duration_invalid"
                )
            computed_duration += duration
    slowest = payload.get("slowest")
    if not isinstance(slowest, list) or len(slowest) > SLOWEST_N:
        raise AggregationError(
            f"shard {index} timing slowest entries are malformed",
            error_code="shard_report_corrupt",
        )
    slowest_ids: set[str] = set()
    for entry in slowest:
        if not isinstance(entry, dict) or set(entry) != _TEST_ENTRY_KEYS:
            raise AggregationError(
                f"shard {index} timing slowest entry is malformed",
                error_code="shard_report_corrupt",
            )
        test_id = entry["id"]
        outcome = entry["outcome"]
        duration = entry["duration_seconds"]
        duration_is_invalid = duration is not None and (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or float(duration) < 0
        )
        if (
            not isinstance(test_id, str)
            or not test_id
            or test_id in slowest_ids
            or not isinstance(outcome, str)
            or outcome not in _OUTCOMES
            or duration_is_invalid
            or entry not in tests
        ):
            raise AggregationError(
                f"shard {index} timing slowest entry is malformed",
                error_code="shard_report_corrupt",
            )
        slowest_ids.add(test_id)
    expected_slowest = sorted(
        tests,
        key=lambda entry: (-float(entry["duration_seconds"] or 0.0), str(entry["id"])),
    )[:SLOWEST_N]
    if slowest != expected_slowest:
        raise AggregationError(
            f"shard {index} timing slowest entries do not match its tests",
            error_code="shard_report_corrupt",
        )
    if reported_counts != computed_counts or sum(reported_counts.values()) != total_tests:
        raise AggregationError(
            f"shard {index} timing counts do not match its tests",
            error_code="shard_report_corrupt",
        )
    reported_duration = payload.get("total_duration_seconds")
    if (
        not isinstance(reported_duration, (int, float))
        or isinstance(reported_duration, bool)
        or not math.isfinite(float(reported_duration))
        or not math.isclose(float(reported_duration), computed_duration, abs_tol=0.0015)
    ):
        raise AggregationError(
            f"shard {index} timing total duration is inconsistent",
            error_code="shard_report_corrupt",
        )
    exit_code = payload.get("pytest_exit_code")
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code < 0
        or exit_code > 5
    ):
        raise AggregationError(
            f"shard {index} pytest exit code is invalid",
            error_code="shard_report_corrupt",
        )
    return payload


def _plan_digest(shard_count: int, shards: list[list[str]]) -> str:
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


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise AggregationError(
            f"shard plan {field} must be RFC3339 UTC", error_code="plan_contract_mismatch"
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AggregationError(
            f"shard plan {field} is invalid", error_code="plan_contract_mismatch"
        ) from exc


def _validate_plan_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("tests/")
        or not value.endswith(".py")
        or value.startswith("/")
        or value.startswith("./")
        or "\\" in value
        or "//" in value
        or ".." in Path(value).parts
        or _CONTROL_CHARS_RE.search(value)
    ):
        raise AggregationError(
            "shard plan contains an unsafe path", error_code="plan_contract_mismatch"
        )
    return value


def _validate_canonical_path_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise AggregationError(
            f"shard plan {field} is invalid", error_code="plan_contract_mismatch"
        )
    paths = [_validate_plan_path(item) for item in value]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise AggregationError(
            f"shard plan {field} is not canonical", error_code="plan_contract_mismatch"
        )
    return paths


def _load_plan(
    path: Path,
    expected_shard_count: int | None = None,
    *,
    now: datetime | None = None,
) -> list[list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggregationError(
            "shard plan could not be read", error_code="plan_unreadable"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != PLAN_SCHEMA:
        raise AggregationError("shard plan schema mismatch", error_code="plan_schema_mismatch")
    if set(payload) != _PLAN_KEYS:
        raise AggregationError("shard plan keys mismatch", error_code="plan_contract_mismatch")

    shard_count = payload.get("shard_count")
    if (
        not isinstance(shard_count, int)
        or isinstance(shard_count, bool)
        or shard_count not in ALLOWED_SHARD_COUNTS
    ):
        raise AggregationError("shard plan count is invalid", error_code="plan_count_mismatch")
    if expected_shard_count is not None and shard_count != expected_shard_count:
        raise AggregationError(
            "workflow shard count does not match the plan", error_code="plan_count_mismatch"
        )

    indexes = payload.get("indexes")
    expected_indexes = list(range(shard_count))
    if (
        not isinstance(indexes, list)
        or any(not isinstance(index, int) or isinstance(index, bool) for index in indexes)
        or indexes != expected_indexes
    ):
        raise AggregationError(
            "shard plan indexes are not contiguous", error_code="plan_index_mismatch"
        )

    raw_shards = payload.get("shards")
    if not isinstance(raw_shards, list) or len(raw_shards) != shard_count:
        raise AggregationError("shard plan count mismatch", error_code="plan_count_mismatch")

    shards: list[list[str]] = []
    all_files: list[str] = []
    for expected_index, shard in enumerate(raw_shards):
        if not isinstance(shard, dict) or set(shard) != _PLAN_SHARD_KEYS:
            raise AggregationError(
                "shard plan assignment is malformed", error_code="plan_contract_mismatch"
            )
        shard_index = shard.get("index")
        if (
            not isinstance(shard_index, int)
            or isinstance(shard_index, bool)
            or shard_index != expected_index
        ):
            raise AggregationError(
                "shard plan assignment index mismatch", error_code="plan_index_mismatch"
            )
        files = shard.get("files")
        if not isinstance(files, list) or not files:
            raise AggregationError("shard plan has an empty shard", error_code="plan_empty")
        file_count = shard.get("file_count")
        if (
            not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or file_count != len(files)
        ):
            raise AggregationError(
                "shard plan file count mismatch", error_code="plan_contract_mismatch"
            )
        estimated_weight = shard.get("estimated_weight_seconds")
        if (
            not isinstance(estimated_weight, (int, float))
            or isinstance(estimated_weight, bool)
            or not math.isfinite(float(estimated_weight))
            or float(estimated_weight) < 0
        ):
            raise AggregationError(
                "shard plan estimated weight is invalid", error_code="plan_contract_mismatch"
            )
        if files != sorted(files):
            raise AggregationError(
                "shard plan files are not canonical", error_code="plan_contract_mismatch"
            )
        for file in files:
            _validate_plan_path(file)
        shards.append(files)
        all_files.extend(files)

    if not all_files:
        raise AggregationError("shard plan has no files", error_code="plan_empty")
    if len(all_files) != len(set(all_files)):
        raise AggregationError(
            "shard plan assigns a file more than once", error_code="plan_duplicate_file"
        )
    total_files = payload.get("total_files")
    if (
        not isinstance(total_files, int)
        or isinstance(total_files, bool)
        or total_files <= 0
        or total_files != len(all_files)
    ):
        raise AggregationError(
            "shard plan total file count mismatch", error_code="plan_contract_mismatch"
        )

    new_files = _validate_canonical_path_list(payload.get("new_files"), field="new_files")
    deleted_files = _validate_canonical_path_list(
        payload.get("deleted_files"), field="deleted_files"
    )
    current_files = set(all_files)
    if not set(new_files).issubset(current_files):
        raise AggregationError(
            "shard plan new files are outside the current inventory",
            error_code="plan_contract_mismatch",
        )
    if set(deleted_files) & current_files:
        raise AggregationError(
            "shard plan deleted files overlap the current inventory",
            error_code="plan_contract_mismatch",
        )

    known_count = payload.get("known_file_count")
    unknown_count = payload.get("unknown_file_count")
    if (
        not isinstance(known_count, int)
        or isinstance(known_count, bool)
        or known_count < 0
        or not isinstance(unknown_count, int)
        or isinstance(unknown_count, bool)
        or unknown_count < 0
    ):
        raise AggregationError(
            "shard plan inventory counts are invalid", error_code="plan_contract_mismatch"
        )
    if known_count + unknown_count != total_files or unknown_count != len(new_files):
        raise AggregationError(
            "shard plan inventory counts are inconsistent", error_code="plan_contract_mismatch"
        )
    if unknown_count * 100 > total_files * MAX_UNKNOWN_FILE_PERCENT:
        raise AggregationError(
            "shard plan unknown files exceed the 5% inventory limit",
            error_code="plan_contract_mismatch",
        )

    known_coverage = payload.get("known_file_coverage")
    expected_coverage = round(known_count / total_files, 6)
    if (
        not isinstance(known_coverage, (int, float))
        or isinstance(known_coverage, bool)
        or not math.isfinite(float(known_coverage))
        or not 0 <= float(known_coverage) <= 1
        or float(known_coverage) != expected_coverage
    ):
        raise AggregationError(
            "shard plan known-file coverage is invalid", error_code="plan_contract_mismatch"
        )

    unknown_weight = payload.get("unknown_file_weight_seconds")
    if (
        not isinstance(unknown_weight, (int, float))
        or isinstance(unknown_weight, bool)
        or not math.isfinite(float(unknown_weight))
        or float(unknown_weight) < 0
    ):
        raise AggregationError(
            "shard plan fallback weight is invalid", error_code="plan_contract_mismatch"
        )

    baseline_time = _parse_utc_timestamp(payload.get("baseline_at"), field="baseline_at")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise AggregationError(
            "current aggregation time must be timezone-aware",
            error_code="plan_contract_mismatch",
        )
    baseline_age = current_time.astimezone(UTC) - baseline_time
    if baseline_age < timedelta(0) or baseline_age > timedelta(days=MAX_BASELINE_AGE_DAYS):
        raise AggregationError(
            "shard plan baseline timestamp is outside the trusted age window",
            error_code="plan_contract_mismatch",
        )
    if payload.get("digest") != _plan_digest(shard_count, shards):
        raise AggregationError("shard plan digest mismatch", error_code="plan_digest_mismatch")
    return shards


def _map_test_id_to_file(test_id: str, module_map: dict[str, str]) -> str:
    classname = test_id.split("::", 1)[0]
    parts = classname.split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in module_map:
            return module_map[candidate]
    raise AggregationError(
        f"aggregate test id not covered by the shard plan: {test_id}",
        error_code="test_id_not_covered",
    )


def aggregate_shard_reports(
    shard_reports: list[dict[str, Any]],
    plan_shards: list[list[str]],
) -> dict[str, Any]:
    """Merge validated shard reports into the stable aggregate report."""
    plan_files = [file for shard in plan_shards for file in shard]
    module_map = {
        (f[: -len(".py")].replace("/", ".") if f.endswith(".py") else f): f for f in plan_files
    }
    plan_file_set = frozenset(plan_files)
    plan_shard_sets = [frozenset(files) for files in plan_shards]

    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    counts = {outcome: 0 for outcome in _OUTCOMES}
    total_duration = 0.0
    all_complete = True
    exit_codes: list[int] = []
    covered_files: set[str] = set()

    if len(shard_reports) != len(plan_shards):
        raise AggregationError(
            "report count does not match the shard plan", error_code="shard_report_count_mismatch"
        )

    for index, report in enumerate(shard_reports):
        if not isinstance(report, dict) or report.get("schema") != TIMING_SCHEMA:
            raise AggregationError(
                f"shard {index} timing report schema mismatch",
                error_code="shard_schema_mismatch",
            )
        all_complete = all_complete and report.get("complete") is True
        exit_codes.append(int(report.get("pytest_exit_code", 1)))
        shard_counts = report.get("counts", {})
        for outcome in _OUTCOMES:
            counts[outcome] += int(shard_counts.get(outcome, 0))
        for entry in report["tests"]:
            test_id = str(entry["id"])
            if test_id in seen_ids:
                raise AggregationError(
                    f"duplicate test id across shards: {test_id}",
                    error_code="duplicate_test_id",
                )
            seen_ids.add(test_id)
            file = _map_test_id_to_file(test_id, module_map)
            if file not in plan_file_set:
                raise AggregationError(
                    f"aggregate test id maps outside the shard plan: {test_id}",
                    error_code="test_id_not_covered",
                )
            if file not in plan_shard_sets[index]:
                raise AggregationError(
                    f"shard {index} reported a test assigned to another shard: {test_id}",
                    error_code="shard_assignment_mismatch",
                )
            covered_files.add(file)
            duration = entry["duration_seconds"]
            if duration is not None:
                duration = float(duration)
                if not math.isfinite(duration) or duration < 0:
                    raise AggregationError(
                        f"shard {index} duration invalid for {test_id}",
                        error_code="shard_duration_invalid",
                    )
                total_duration += duration
            merged.append(
                {
                    "id": _sanitize(test_id)[:MAX_FIELD_LENGTH],
                    "outcome": entry["outcome"],
                    "duration_seconds": (
                        round(float(duration), 6) if duration is not None else None
                    ),
                }
            )

    if covered_files != plan_file_set:
        missing = sorted(plan_file_set - covered_files)
        raise AggregationError(
            f"aggregate does not cover every planned test file; missing: {missing[:10]}",
            error_code="coverage_mismatch",
        )

    sorted_entries = sorted(
        merged,
        key=lambda e: (-float(e["duration_seconds"] or 0.0), str(e["id"])),
    )
    aggregate_exit_code = max(exit_codes) if exit_codes else 1
    complete = all_complete and aggregate_exit_code == 0
    if aggregate_exit_code != 0:
        aggregate_error: str | None = "pytest_execution_failed"
    elif not all_complete:
        aggregate_error = "shard_report_incomplete"
    else:
        aggregate_error = None

    return {
        "schema": TIMING_SCHEMA,
        "plan_digest": _plan_digest(len(plan_shards), plan_shards),
        "complete": complete,
        "error": aggregate_error,
        "pytest_exit_code": aggregate_exit_code,
        "total_tests": len(merged),
        "counts": counts,
        "total_duration_seconds": round(total_duration, 3),
        "slowest": sorted_entries[:SLOWEST_N],
        "tests": sorted_entries,
    }


def build_step_summary(report: dict[str, Any], shard_count: int) -> str:
    exit_code = int(report.get("pytest_exit_code", 1))
    status = "\u2705" if exit_code == 0 else "\u274c"
    counts = report.get("counts", {})
    lines = [
        "## Pytest aggregate timing report",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Shards aggregated | {shard_count} |",
        f"| Aggregate result | {status} exit code {exit_code} |",
        f"| Complete | {report.get('complete')} |",
        f"| Total tests | {report.get('total_tests', 0)} |",
        f"| Passed | {counts.get('passed', 0)} |",
        f"| Failed | {counts.get('failed', 0)} |",
        f"| Skipped | {counts.get('skipped', 0)} |",
        f"| Errors | {counts.get('error', 0)} |",
        f"| Total duration | {report.get('total_duration_seconds', 0):.1f}s |",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate per-shard pytest timing reports.")
    parser.add_argument("--shard-dir", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--expected-shard-count", type=int, choices=(2, 4), default=None)
    parser.add_argument(
        "--now",
        default="",
        help="RFC3339 UTC validation time (tests/reproducibility only; defaults to current UTC)",
    )
    args = parser.parse_args(argv)

    shard_dir = Path(args.shard_dir)
    output_path = Path(args.output)

    try:
        validation_time = _parse_utc_timestamp(args.now, field="now") if args.now else None
        plan_shards = _load_plan(Path(args.plan), args.expected_shard_count, now=validation_time)
        plan_digest = _plan_digest(len(plan_shards), plan_shards)
        shard_count = len(plan_shards)
        discovered_reports = {
            path.name for path in shard_dir.glob("shard-*.json") if path.is_file()
        }
        expected_reports = {f"shard-{index}.json" for index in range(shard_count)}
        if expected_reports - discovered_reports:
            raise AggregationError(
                "one or more shard reports are missing",
                error_code="shard_report_missing",
            )
        if discovered_reports - expected_reports:
            raise AggregationError(
                "shard report indexes do not match the plan",
                error_code="shard_report_index_mismatch",
            )
        shard_reports = [
            _load_shard_report(shard_dir / f"shard-{index}.json", index)
            for index in range(shard_count)
        ]
        report = aggregate_shard_reports(shard_reports, plan_shards)
        exit_code = int(report["pytest_exit_code"])
    except AggregationError as exc:
        report = {
            "schema": TIMING_SCHEMA,
            "plan_digest": plan_digest if "plan_digest" in locals() else None,
            "complete": False,
            "error": exc.error_code,
            "pytest_exit_code": 1,
            "total_tests": 0,
            "counts": {outcome: 0 for outcome in _OUTCOMES},
            "total_duration_seconds": 0.0,
            "slowest": [],
            "tests": [],
        }
        exit_code = 1
        print(f"timing aggregation failed closed: {exc}", file=sys.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary_text = build_step_summary(
        report,
        len(plan_shards) if "plan_shards" in locals() else (args.expected_shard_count or 0),
    )
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary_text + "\n")
    else:
        print(summary_text)  # noqa: T201
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
