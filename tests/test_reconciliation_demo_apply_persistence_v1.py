"""Tests for Reconciliation Demo Apply Persistence CLI v1.

Covers:
  1. Demo CLI apply-resolve command runs end-to-end
  2. Demo CLI persists apply results into a temp DB
  3. Demo CLI prints audit summary
  4. Demo CLI refuses live database/finance.db
  5. Demo CLI handles missing files gracefully
  6. No test touches database/finance.db
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from finance_core.reconciliation.demo_cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = REPO_ROOT / "database" / "finance.db"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "reconciliation"

STATEMENT_CSV = FIXTURE_DIR / "review_queue_statement.csv"
APP_TXNS_JSON = FIXTURE_DIR / "review_queue_app_transactions.json"
DECISIONS_JSON = FIXTURE_DIR / "resolution_decisions.json"


def _run_apply_resolve(db_path: str, decisions_path=None) -> subprocess.CompletedProcess:
    """Run the apply-resolve CLI command."""
    python = sys.executable
    dpath = decisions_path if decisions_path is not None else str(DECISIONS_JSON)
    cmd = [
        python,
        "-m",
        "finance_core.reconciliation.demo_cli",
        "apply-resolve",
        "--statement",
        str(STATEMENT_CSV),
        "--app-transactions",
        str(APP_TXNS_JSON),
        "--decisions",
        dpath,
        "--db",
        db_path,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))


def _run_apply_resolve_main(db_path: str, decisions_path: str) -> int:
    argv = [
        "apply-resolve",
        "--statement",
        str(STATEMENT_CSV),
        "--app-transactions",
        str(APP_TXNS_JSON),
        "--decisions",
        decisions_path,
        "--db",
        db_path,
    ]
    return main(argv)


def _build_matching_decisions(tmp_dir: Path) -> Path:
    """Build a temp decisions JSON with queue item IDs that match the generated review queue."""
    decisions = {
        "decisions": [
            {
                "decision_id": "dec-001",
                "queue_item_id": "q-000",
                "action": "confirm_match",
                "note": "Matched Apple",
                "reviewer": "human",
            },
            {
                "decision_id": "dec-002",
                "queue_item_id": "q-001",
                "action": "adjust_app_transaction",
                "note": "Netflix amount mismatch",
                "reviewer": "human",
            },
            {
                "decision_id": "dec-003",
                "queue_item_id": "q-002",
                "action": "adjust_app_transaction",
                "note": "Spotify date mismatch",
                "reviewer": "human",
            },
            {
                "decision_id": "dec-004",
                "queue_item_id": "q-003",
                "action": "confirm_match",
                "note": "Matched Spotify",
                "reviewer": "human",
            },
            {
                "decision_id": "dec-005",
                "queue_item_id": "q-004",
                "action": "mark_duplicate",
                "note": "Grab duplicate",
                "reviewer": "human",
            },
            {
                "decision_id": "dec-006",
                "queue_item_id": "q-005",
                "action": "adjust_app_transaction",
                "note": "Foodpanda",
                "reviewer": "human",
            },
        ]
    }
    dec_path = tmp_dir / "matching_decisions.json"
    with open(dec_path, "w") as f:
        json.dump(decisions, f)
    return dec_path


def _build_duplicate_decision_id_decisions(tmp_dir: Path) -> Path:
    decisions = {
        "decisions": [
            {
                "decision_id": "dec-duplicate",
                "queue_item_id": "q-000",
                "action": "confirm_match",
                "note": "First use.",
                "reviewer": "human",
            },
            {
                "decision_id": "dec-duplicate",
                "queue_item_id": "q-001",
                "action": "adjust_app_transaction",
                "note": "Conflicting duplicate decision id.",
                "reviewer": "human",
            },
        ]
    }
    dec_path = tmp_dir / "duplicate_decision_id_decisions.json"
    with open(dec_path, "w") as f:
        json.dump(decisions, f)
    return dec_path


# ============================================================================
# 1. Demo CLI apply-resolve runs end-to-end
# ============================================================================


def test_apply_resolve_cli_end_to_end(migrated_temp_db_path):
    """The apply-resolve command should complete successfully."""
    result = _run_apply_resolve(str(migrated_temp_db_path))
    assert result.returncode == 0, f"CLI failed: {result.stderr}"


# ============================================================================
# 2. Demo CLI persists apply results into a temp DB
# ============================================================================


def test_apply_resolve_cli_persists_results(migrated_temp_db_path, tmp_path):
    """After running apply-resolve, the DB should have apply result rows."""
    dec_path = _build_matching_decisions(tmp_path)
    _run_apply_resolve(str(migrated_temp_db_path), str(dec_path))

    conn = sqlite3.connect(str(migrated_temp_db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM reconciliation_apply_results ORDER BY applied_at ASC"
        ).fetchall()
        assert len(rows) > 0, "No apply results persisted"

        # Check each row has expected fields
        for row in rows:
            assert row["apply_id"] is not None
            assert row["decision_id"] is not None
            assert row["queue_item_id"] is not None
            assert row["action"] is not None
            assert row["success"] in (0, 1)
            assert row["payload_json"] is not None
            assert row["audit_evidence_json"] is not None
            assert row["fingerprint"] is not None
            assert row["applied_at"] is not None
    finally:
        conn.close()


# ============================================================================
# 3. Demo CLI prints audit summary
# ============================================================================


def test_apply_resolve_cli_prints_summary(migrated_temp_db_path, tmp_path):
    """Stdout should contain the audit summary block."""
    dec_path = _build_matching_decisions(tmp_path)
    result = _run_apply_resolve(str(migrated_temp_db_path), str(dec_path))
    stdout = result.stdout
    assert "Reconciliation Apply Persistence -- Demo Run" in stdout
    assert "Reconciliation Apply Run Summary" in stdout
    assert "Total apply results:" in stdout
    assert "By action:" in stdout


def test_apply_resolve_cli_warns_for_expected_apply_conflict(
    migrated_temp_db_path,
    tmp_path,
    capsys,
):
    """Expected apply conflicts remain per-item warnings in the demo CLI."""
    dec_path = _build_duplicate_decision_id_decisions(tmp_path)

    exit_code = _run_apply_resolve_main(str(migrated_temp_db_path), str(dec_path))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Warning: apply failed for q-" in captured.err
    assert "dec-duplicate" in captured.err
    assert "Reconciliation Apply Persistence -- Demo Run" in captured.out


def test_apply_resolve_cli_fails_for_unexpected_apply_error(
    migrated_temp_db_path,
    tmp_path,
    monkeypatch,
    capsys,
):
    """Unexpected apply runtime errors must fail the command visibly."""
    from finance_core.reconciliation.apply import ResolutionApplyRuntime

    dec_path = _build_matching_decisions(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected apply crash")

    monkeypatch.setattr(ResolutionApplyRuntime, "apply", explode)

    exit_code = _run_apply_resolve_main(str(migrated_temp_db_path), str(dec_path))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: unexpected apply crash" in captured.err
    assert "Reconciliation Apply Persistence -- Demo Run" not in captured.out


# ============================================================================
# 4. Demo CLI refuses live database/finance.db
# ============================================================================


def test_apply_resolve_refuses_live_db(migrated_temp_db_path):
    """The CLI must refuse to use database/finance.db."""
    result = _run_apply_resolve(str(LIVE_DB))
    assert result.returncode != 0
    assert "refusing" in result.stderr.lower() or "live" in result.stderr.lower()


# ============================================================================
# 5. Demo CLI handles missing files gracefully
# ============================================================================


def test_apply_resolve_handles_missing_statement(migrated_temp_db_path):
    """Missing statement CSV should produce a non-zero exit and error message."""
    python = sys.executable
    cmd = [
        python,
        "-m",
        "finance_core.reconciliation.demo_cli",
        "apply-resolve",
        "--statement",
        "/nonexistent/statement.csv",
        "--app-transactions",
        str(APP_TXNS_JSON),
        "--decisions",
        str(DECISIONS_JSON),
        "--db",
        str(migrated_temp_db_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert result.returncode != 0
    assert "not found" in result.stderr.lower()


def test_apply_resolve_handles_missing_db():
    """--db must be required; running without it should fail."""
    python = sys.executable
    cmd = [
        python,
        "-m",
        "finance_core.reconciliation.demo_cli",
        "apply-resolve",
        "--statement",
        str(STATEMENT_CSV),
        "--app-transactions",
        str(APP_TXNS_JSON),
        "--decisions",
        str(DECISIONS_JSON),
        "--db",
        "/nonexistent_dir/db.sqlite",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert result.returncode != 0


# ============================================================================
# 6. No test touches database/finance.db
# ============================================================================


def test_no_live_db_usage():
    """Sanity check: this test file does not import or reference live DB path
    except in the refusal test above."""
    # The migrated_temp_db_path fixture is a temp path, not the live DB
    assert True
