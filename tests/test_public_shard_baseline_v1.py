"""Public timing provenance and exact current test-inventory coverage."""

from __future__ import annotations

import copy
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "public_shard_planner", ROOT / ".github/scripts/plan_pytest_shards.py"
)
assert SPEC and SPEC.loader
PLANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLANNER)


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
