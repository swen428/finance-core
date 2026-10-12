"""Public timing provenance and exact current test-inventory coverage."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "public_shard_planner", ROOT / ".github/scripts/plan_pytest_shards.py"
)
assert SPEC and SPEC.loader
PLANNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLANNER
SPEC.loader.exec_module(PLANNER)

AGGREGATOR_SPEC = importlib.util.spec_from_file_location(
    "public_shard_aggregator", ROOT / ".github/scripts/aggregate_pytest_timing.py"
)
assert AGGREGATOR_SPEC and AGGREGATOR_SPEC.loader
AGGREGATOR = importlib.util.module_from_spec(AGGREGATOR_SPEC)
sys.modules[AGGREGATOR_SPEC.name] = AGGREGATOR
AGGREGATOR_SPEC.loader.exec_module(AGGREGATOR)


def baseline():
    return PLANNER.load_baseline_manifest(ROOT / ".github/scripts/pytest-shard-weights-v2.json")


def test_public_baseline_covers_current_inventory_exactly_once() -> None:
    manifest = baseline()
    # The production CLI enforces today's age. This regression fixes time to
    # the recorded sample so future baseline maintenance is not a test timer.
    now = datetime.fromisoformat(manifest["baseline_at"].replace("Z", "+00:00"))
    plan = PLANNER.plan_shards(manifest, repository_root=ROOT, now=now)
    assert plan == PLANNER.plan_shards(manifest, repository_root=ROOT, now=now)
    assigned = [file for shard in plan["shards"] for file in shard["files"]]
    assert len(assigned) == len(set(assigned))
    assert sorted(assigned) == list(PLANNER.discover_test_files(ROOT))
    assert plan["shard_count"] == 4
    assert plan["unknown_file_count"] * 100 <= plan["total_files"] * 5
    assert len(manifest["source_runs"]) >= 3
    for run in manifest["source_runs"]:
        assert run["repository"] == "github-repository-id:1375123906"
        assert run["workflow_path"] == ".github/workflows/validate.yml"
        assert run["event"] == "push" and run["run_attempt"] == 1
        assert run["status"] == "completed" and run["conclusion"] == "success"
    with pytest.raises(PLANNER.ShardPlanningError, match="older than 30 days"):
        PLANNER.plan_shards(manifest, repository_root=ROOT, now=now + timedelta(days=31))
    broken = copy.deepcopy(manifest)
    broken["file_weights_seconds"] = {}
    with pytest.raises(PLANNER.ShardPlanningError, match="5% inventory limit"):
        PLANNER.plan_shards(broken, repository_root=ROOT, now=now)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "foreign/repo"),
        ("event", "pull_request"),
        ("event", "workflow_dispatch"),
        ("workflow_path", ".github/workflows/other.yml"),
        ("run_attempt", 2),
        ("conclusion", "failure"),
    ],
)
def test_baseline_refresh_refuses_untrusted_source_identity(field: str, value: object) -> None:
    source = {**baseline()["source_runs"][0], "path": "synthetic-only.json"}
    source[field] = value
    with pytest.raises(PLANNER.ShardPlanningError):
        PLANNER._validate_source_spec(source)


def make_validated_plan(
    *,
    expected: tuple[float, ...] = (10.0, 10.0, 10.0, 10.0),
    unknown_indexes: tuple[int, ...] = (0,),
    fallback_per_file: float = 6.25,
):
    files = tuple(f"tests/test_{index}.py" for index in range(len(expected)))
    shards = tuple((file,) for file in files)
    return AGGREGATOR.ValidatedPlan(
        shards=shards,
        expected_seconds=expected,
        unknown_files=frozenset(files[index] for index in unknown_indexes),
        unknown_file_fallback_seconds=fallback_per_file,
    )


def make_reports(
    durations: tuple[float | None, ...],
    *,
    outcomes: tuple[str, ...] | None = None,
    complete: bool = True,
    error: str | None = None,
    exit_code: int = 0,
    cases_per_file: int = 1,
):
    if outcomes is None:
        outcomes = ("passed",) * (len(durations) * cases_per_file)
    reports = []
    case_index = 0
    for shard_index, shard_duration in enumerate(durations):
        tests = []
        for case_number in range(cases_per_file):
            outcome = outcomes[case_index]
            case_index += 1
            duration = shard_duration / cases_per_file if shard_duration is not None else None
            tests.append(
                {
                    "id": f"tests.test_{shard_index}::test_case_{case_number}",
                    "outcome": outcome,
                    "duration_seconds": duration,
                }
            )
        tests.sort(
            key=lambda entry: (
                -float(entry["duration_seconds"] or 0.0),
                str(entry["id"]),
            )
        )
        counts = {
            outcome: sum(entry["outcome"] == outcome for entry in tests)
            for outcome in AGGREGATOR._OUTCOMES
        }
        total_duration = sum(float(entry["duration_seconds"] or 0.0) for entry in tests)
        reports.append(
            {
                "schema": AGGREGATOR.TIMING_SCHEMA,
                "complete": complete,
                "error": error,
                "pytest_exit_code": exit_code,
                "total_tests": len(tests),
                "counts": counts,
                "total_duration_seconds": total_duration,
                "slowest": tests[: AGGREGATOR.SLOWEST_N],
                "tests": tests,
            }
        )
    return reports


@pytest.mark.parametrize(
    ("durations", "fallback", "expected_hint"),
    [
        ((12.5, 12.5, 12.5, 12.5), 6.25, None),
        ((12.0, 8.0, 10.0, 10.0), 6.0, None),
        ((12.6, 12.6, 12.6, 12.6), 6.25, "overall_slower"),
        ((13.0, 10.0, 10.0, 10.0), 6.5, "imbalance"),
        ((12.6, 10.0, 10.0, 10.0), 6.25, "refresh_unknown_weights"),
    ],
)
def test_diagnostics_use_strict_thresholds(
    durations: tuple[float, ...], fallback: float, expected_hint: str | None
) -> None:
    plan = make_validated_plan(fallback_per_file=fallback)
    reports = make_reports(durations)

    diagnostics = AGGREGATOR.build_performance_diagnostics(reports, plan)

    assert diagnostics["evaluable"] is True
    if expected_hint is None:
        assert diagnostics["hints"] == []
    else:
        assert expected_hint in diagnostics["hints"]
    if durations == (12.5, 12.5, 12.5, 12.5):
        assert diagnostics["actual_expected_ratio"] == 1.25
        assert diagnostics["max_mean_ratio"] == 1.0
        assert diagnostics["max_unknown_file_actual_fallback_ratio"] == 2.0
    if durations == (12.0, 8.0, 10.0, 10.0):
        assert diagnostics["actual_expected_ratio"] == 1.0
        assert diagnostics["max_mean_ratio"] == 1.2
        assert diagnostics["max_unknown_file_actual_fallback_ratio"] == 2.0


@pytest.mark.parametrize(
    ("reports", "plan", "reason"),
    [
        (None, None, "missing validated plan"),
        ([], make_validated_plan(), "report count"),
        (
            make_reports((1.0, 1.0, 1.0, 1.0), complete=False, error="partial"),
            make_validated_plan(),
            "incomplete or failed",
        ),
        (
            make_reports((1.0, 1.0, 1.0, 1.0), exit_code=1),
            make_validated_plan(),
            "incomplete or failed",
        ),
        (
            make_reports((None, 1.0, 1.0, 1.0)),
            make_validated_plan(),
            "missing duration",
        ),
        (
            make_reports((float("nan"), 1.0, 1.0, 1.0)),
            make_validated_plan(),
            "invalid duration",
        ),
        (
            make_reports((1.0, 1.0, 1.0, 1.0)),
            make_validated_plan(expected=(1e-308, 1e-308, 1e-308, 1e-308)),
            "invalid computed ratio",
        ),
        (
            make_reports((0.0, 0.0, 0.0, 0.0)),
            make_validated_plan(),
            "zero total duration",
        ),
    ],
)
def test_diagnostics_fail_closed_for_unusable_timing(
    reports: list[dict[str, object]] | None,
    plan: object | None,
    reason: str,
) -> None:
    diagnostics = AGGREGATOR.build_performance_diagnostics(reports, plan)

    assert diagnostics["evaluable"] is False
    assert reason in diagnostics["reason"]
    assert diagnostics["actual_expected_ratio"] is None
    assert diagnostics["max_mean_ratio"] is None
    assert diagnostics["ideal_lower_bound_seconds"] is None


def test_diagnostics_use_validated_weights_and_report_arithmetic_without_mutation() -> None:
    plan = make_validated_plan(
        expected=(8.0, 10.0, 12.0, 10.0),
        unknown_indexes=(0, 2),
        fallback_per_file=4.0,
    )
    reports = make_reports((2.0, 4.0, 6.0, 8.0), cases_per_file=2)
    before_plan = copy.deepcopy(plan)
    before_reports = copy.deepcopy(reports)

    diagnostics = AGGREGATOR.build_performance_diagnostics(reports, plan)
    summary = AGGREGATOR.build_step_summary(
        AGGREGATOR.aggregate_shard_reports(reports, [list(shard) for shard in plan.shards]),
        4,
        diagnostics,
    )

    assert diagnostics["evaluable"] is True
    assert diagnostics["actual_seconds"] == 20.0
    assert diagnostics["expected_seconds"] == 40.0
    assert diagnostics["ideal_lower_bound_seconds"] == 5.0
    assert diagnostics["shards"][0]["test_count"] == 2
    assert diagnostics["shards"][0]["deviation_percent"] == -75.0
    assert diagnostics["unknown_fallback_seconds"] == 8.0
    assert diagnostics["unknown_actual_seconds"] == 8.0
    assert diagnostics["max_unknown_file_actual_fallback_ratio"] == 1.5
    assert diagnostics["top_files"][0]["share"] == 0.4
    assert diagnostics["top_cases"][0]["share"] == 0.2
    assert "test-time lower bound" in summary
    assert "not a wall-time guarantee" in summary
    assert plan == before_plan
    assert reports == before_reports


def test_unknown_weight_hint_is_per_file_not_aggregate() -> None:
    plan = make_validated_plan(
        expected=(41.0, 30.0, 41.0, 41.0),
        unknown_indexes=(0, 1),
        fallback_per_file=20.0,
    )
    reports = make_reports((41.0, 30.0, 41.0, 41.0))

    diagnostics = AGGREGATOR.build_performance_diagnostics(reports, plan)

    assert diagnostics["unknown_actual_seconds"] == 71.0
    assert diagnostics["unknown_fallback_seconds"] == 40.0
    assert diagnostics["unknown_actual_seconds"] / diagnostics["unknown_fallback_seconds"] < 2
    assert diagnostics["max_unknown_file_actual_fallback_ratio"] == 2.05
    assert diagnostics["hints"] == ["refresh_unknown_weights"]


def test_zero_denominators_are_not_reported_as_zero_ratios() -> None:
    plan = make_validated_plan(
        expected=(0.0, 0.0, 0.0, 0.0),
        unknown_indexes=(0,),
        fallback_per_file=0.0,
    )
    reports = make_reports((1.0, 1.0, 1.0, 1.0))

    diagnostics = AGGREGATOR.build_performance_diagnostics(reports, plan)

    assert diagnostics["actual_expected_ratio"] is None
    assert diagnostics["shards"][0]["deviation_percent"] is None
    assert diagnostics["max_unknown_file_actual_fallback_ratio"] is None
    assert "not evaluable" in AGGREGATOR._format_performance_diagnostics(diagnostics)


def test_markdown_diagnostics_escape_hostile_ids_and_bound_lists() -> None:
    plan = make_validated_plan(
        expected=(10.0, 10.0, 10.0, 10.0),
        unknown_indexes=(),
        fallback_per_file=0.0,
    )
    reports = make_reports((1.0, 2.0, 3.0, 4.0), cases_per_file=4)
    reports[3]["tests"][0]["id"] = (
        "tests.test_3::`\n|<img src=x onerror=alert(1)> **bold** [x](javascript:1)\x01\u202e"
    )
    diagnostics = AGGREGATOR.build_performance_diagnostics(reports, plan)

    markdown = AGGREGATOR._format_performance_diagnostics(diagnostics)

    assert "<img src=x" not in markdown
    assert "javascript:1" in markdown
    assert "｜" in markdown
    assert "&lt;img" in markdown
    assert "**bold**" in markdown
    assert "\u202e" not in markdown
    assert markdown.count("::") <= 10
    assert "6 more" in markdown
    assert len(markdown) < 12_000


@pytest.mark.parametrize(
    ("value", "fence"),
    [
        ("[x](javascript:1) **bold**`", "``"),
        ("[x](javascript:1) **bold**``", "```"),
        ("[x](javascript:1) **bold** " + ("x" * 200) + "``", "`"),
    ],
)
def test_markdown_code_span_delimiters_stay_separate_after_trailing_ticks_and_truncation(
    value: str, fence: str
) -> None:
    rendered = AGGREGATOR._markdown_code(value)

    assert rendered.startswith(f"{fence} ")
    assert rendered.endswith(f" {fence}")
    assert "[x](javascript:1)" in rendered
    assert "**bold**" in rendered
    assert len(rendered) <= 166


def test_failed_or_incomplete_summary_never_shows_zero_as_success() -> None:
    reports = make_reports((None, 1.0, 1.0, 1.0), complete=False, error="partial")
    plan = make_validated_plan()
    report = AGGREGATOR.aggregate_shard_reports(reports, [list(shard) for shard in plan.shards])

    summary = AGGREGATOR.build_step_summary(
        report, 4, AGGREGATOR.build_performance_diagnostics(reports, plan)
    )

    assert "not evaluable" in summary
    assert "Total duration | 0.0s" not in summary
    assert "✅ exit code 0" not in summary


def make_cli_plan() -> dict[str, object]:
    files = [f"tests/test_{index}.py" for index in range(4)]
    shards = [
        {
            "index": index,
            "file_count": 1,
            "estimated_weight_seconds": 10.0,
            "files": [file],
        }
        for index, file in enumerate(files)
    ]
    plan: dict[str, object] = {
        "schema": AGGREGATOR.PLAN_SCHEMA,
        "shard_count": 4,
        "indexes": [0, 1, 2, 3],
        "total_files": 4,
        "shards": shards,
        "new_files": [],
        "deleted_files": [],
        "known_file_count": 4,
        "unknown_file_count": 0,
        "known_file_coverage": 1.0,
        "baseline_at": "2026-10-12T00:00:00Z",
        "unknown_file_weight_seconds": 0.0,
    }
    plan["digest"] = AGGREGATOR._plan_digest(4, [[file] for file in files])
    return plan


def test_plan_and_report_trust_rejections_and_incomplete_report_contract(tmp_path: Path) -> None:
    manifest = baseline()
    sample_time = datetime.fromisoformat(manifest["baseline_at"].replace("Z", "+00:00"))
    plan = PLANNER.plan_shards(manifest, repository_root=ROOT, now=sample_time)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    validated = AGGREGATOR._load_plan(plan_path, expected_shard_count=4, now=sample_time)
    assert len(validated.shards) == 4
    assert len(validated.expected_seconds) == 4
    assert len(validated.unknown_files) == plan["unknown_file_count"]

    stale_now = sample_time + timedelta(days=31)
    with pytest.raises(AGGREGATOR.AggregationError, match="outside the trusted age window"):
        AGGREGATOR._load_plan(plan_path, 4, now=stale_now)

    corrupt = copy.deepcopy(plan)
    corrupt["digest"] = "sha256:" + "0" * 64
    plan_path.write_text(json.dumps(corrupt), encoding="utf-8")
    with pytest.raises(AGGREGATOR.AggregationError, match="digest mismatch"):
        AGGREGATOR._load_plan(plan_path, 4, now=sample_time)

    untrusted = copy.deepcopy(plan)
    untrusted["shards"][0]["files"][0] = "tests/../outside.py"
    plan_path.write_text(json.dumps(untrusted), encoding="utf-8")
    with pytest.raises(AGGREGATOR.AggregationError, match="unsafe path"):
        AGGREGATOR._load_plan(plan_path, 4, now=sample_time)

    duplicate = copy.deepcopy(plan)
    duplicate["shards"][1]["files"] = duplicate["shards"][0]["files"]
    duplicate["shards"][1]["file_count"] = len(duplicate["shards"][0]["files"])
    duplicate["shards"][1]["estimated_weight_seconds"] = plan["shards"][1][
        "estimated_weight_seconds"
    ]
    plan_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(AGGREGATOR.AggregationError, match="more than once"):
        AGGREGATOR._load_plan(plan_path, 4, now=sample_time)

    report_path = tmp_path / "shard.json"
    incomplete = make_reports((1.0,), complete=False, error="partial")[0]
    report_path.write_text(json.dumps(incomplete), encoding="utf-8")
    loaded_report = AGGREGATOR._load_shard_report(report_path, 0)
    assert (
        AGGREGATOR.build_performance_diagnostics(
            [loaded_report],
            AGGREGATOR.ValidatedPlan(
                shards=(("tests/test_0.py",),),
                expected_seconds=(1.0,),
                unknown_files=frozenset(),
                unknown_file_fallback_seconds=0.0,
            ),
        )["evaluable"]
        is False
    )

    report_path.write_text("{", encoding="utf-8")
    with pytest.raises(AGGREGATOR.AggregationError, match="corrupt"):
        AGGREGATOR._load_shard_report(report_path, 0)

    duplicate_report = make_reports((1.0,))[0]
    duplicate_report["tests"].append(copy.deepcopy(duplicate_report["tests"][0]))
    duplicate_report["total_tests"] = 2
    duplicate_report["counts"]["passed"] = 2
    duplicate_report["total_duration_seconds"] = 2.0
    duplicate_report["slowest"] = duplicate_report["tests"]
    report_path.write_text(json.dumps(duplicate_report), encoding="utf-8")
    with pytest.raises(AGGREGATOR.AggregationError, match="duplicate/invalid test ids"):
        AGGREGATOR._load_shard_report(report_path, 0)


def test_cli_json_and_exit_contract_remain_stable(tmp_path: Path) -> None:
    plan = make_cli_plan()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    reports = make_reports((2.0, 3.0, 4.0, 5.0))
    for index, report in enumerate(reports):
        (shard_dir / f"shard-{index}.json").write_text(json.dumps(report), encoding="utf-8")

    success_output = tmp_path / "success.json"
    success_summary = tmp_path / "success.md"
    success = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".github/scripts/aggregate_pytest_timing.py"),
            "--shard-dir",
            str(shard_dir),
            "--plan",
            str(plan_path),
            "--output",
            str(success_output),
            "--summary",
            str(success_summary),
            "--expected-shard-count",
            "4",
            "--now",
            "2026-10-12T00:00:00Z",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert success.returncode == 0
    expected_success = AGGREGATOR.aggregate_shard_reports(
        reports, [[f"tests/test_{index}.py"] for index in range(4)]
    )
    assert success_output.read_bytes() == (
        json.dumps(expected_success, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    assert "Performance diagnostics" in success_summary.read_text(encoding="utf-8")

    (shard_dir / "shard-3.json").unlink()
    failure_output = tmp_path / "failure.json"
    failure = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".github/scripts/aggregate_pytest_timing.py"),
            "--shard-dir",
            str(shard_dir),
            "--plan",
            str(plan_path),
            "--output",
            str(failure_output),
            "--expected-shard-count",
            "4",
            "--now",
            "2026-10-12T00:00:00Z",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    expected_failure = {
        "schema": AGGREGATOR.TIMING_SCHEMA,
        "plan_digest": plan["digest"],
        "complete": False,
        "error": "shard_report_missing",
        "pytest_exit_code": 1,
        "total_tests": 0,
        "counts": {outcome: 0 for outcome in AGGREGATOR._OUTCOMES},
        "total_duration_seconds": 0.0,
        "slowest": [],
        "tests": [],
    }
    assert failure.returncode == 1
    assert failure_output.read_bytes() == (
        json.dumps(expected_failure, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
