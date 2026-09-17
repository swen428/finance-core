"""B5.1a staging runner foundation tests.

Covers workspace creation/recovery, manifest validation, staging DB
reopen, participant bootstrap, persisted authorization recovery, CLI
envelope stability, and safety boundaries.

Only disposable staging databases are used; ``database/finance.db`` and
seed data are untouched.  Production ``finance_core/**`` never imports ``tests/**``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeRecoveryError,
    authorize_receipt_finalization,
    load_persisted_receipt_finalization_authorization,
    prepare_receipt_calculation,
)
from finance_core.receipt_staging_runner.models import (
    RunnerManifestError,
    RunnerWorkspaceError,
    parse_runner_manifest,
)
from finance_core.receipt_staging_runner.participants import (
    bootstrap_participants,
)
from finance_core.receipt_staging_runner.workspace import (
    create_runner_workspace,
    recover_runner_workspace,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import (
    StagingDatabaseError,
    create_staging_database,
    open_staging_database,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


def _valid_manifest_bytes(**overrides: Any) -> bytes:
    """Produce valid manifest JSON bytes with optional overrides."""
    base: dict[str, Any] = {
        "schema_version": "v1",
        "workspace_identity": "ws_test_001",
        "operator_actor_id": "operator_owner",
        "participants": [
            {
                "public_id": "ptcp_owner",
                "display_name": "Owner",
                "is_self": True,
            },
            {
                "public_id": "ptcp_alice",
                "display_name": "Alice",
                "is_self": False,
            },
        ],
    }
    base.update(overrides)
    return json.dumps(base, separators=(",", ":")).encode("utf-8")


def _seed_participants(conn: sqlite3.Connection) -> None:
    """Seed participants for authorization tests (test helper only)."""
    conn.executemany(
        "INSERT INTO participants (public_id, display_name, is_self) VALUES (?, ?, ?)",
        [
            ("person_owner", "Owner", 1),
            ("person_alice", "Alice", 0),
            ("person_bob", "Bob", 0),
        ],
    )
    conn.commit()


def _create_full_authorization(conn: sqlite3.Connection, tmp_path: Path) -> tuple[Any, str]:
    """Run the full B5 chain to create a durable authorization (test helper).

    Reuses the canonical B5 E2E pipeline through public boundaries.
    Returns (authorization, authorization_id).
    """
    from tests.test_receipt_b5_staging_e2e_v1 import _run_b5_pipeline

    pipeline = _run_b5_pipeline(conn, tmp_path, "b51arecov")

    prepared = prepare_receipt_calculation(conn, pipeline.conversion.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    return authorization, authorization.authorization_id


# ---------------------------------------------------------------------------
# 1. Workspace creation
# ---------------------------------------------------------------------------


class TestWorkspaceCreation:
    def test_clean_external_workspace_created(self, tmp_path: Path) -> None:
        ws = tmp_path / "external_ws"
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        result = create_runner_workspace(str(ws), manifest)

        assert Path(result.workspace_path).is_dir()
        assert Path(result.database_path).parent.is_dir()
        assert Path(result.attachments_path).is_dir()
        assert Path(result.runtime_path).is_dir()
        assert Path(result.evidence_path).is_dir()
        assert result.manifest_hash == manifest.manifest_sha256
        assert result.workspace_identity == "ws_test_001"

    def test_workspace_permissions(self, tmp_path: Path) -> None:
        ws = tmp_path / "perm_ws"
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        result = create_runner_workspace(str(ws), manifest)

        ws_stat = os.stat(result.workspace_path)
        assert stat.S_IMODE(ws_stat.st_mode) == 0o700

        for sub in (result.attachments_path, result.runtime_path, result.evidence_path):
            sub_stat = os.stat(sub)
            assert stat.S_IMODE(sub_stat.st_mode) == 0o700

    def test_runtime_path_rejected(self, tmp_path: Path) -> None:
        runtime_ws = REPO_ROOT / "tmp_test_ws"
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        with pytest.raises(RunnerWorkspaceError, match="inside the runtime root"):
            create_runner_workspace(str(runtime_ws), manifest)

    def test_live_db_path_rejected(self, tmp_path: Path) -> None:
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        with pytest.raises(RunnerWorkspaceError, match="repository root|live database"):
            create_runner_workspace(str(LIVE_DB_PATH), manifest)

    def test_relative_path_rejected(self, tmp_path: Path) -> None:
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        with pytest.raises(RunnerWorkspaceError, match="absolute"):
            create_runner_workspace("relative/path", manifest)

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        # Symlink pointing to a non-existent target: must be rejected as symlink,
        # not followed to create at the target location.
        link = tmp_path / "link_ws"
        link.symlink_to(tmp_path / "nonexistent_target")
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        with pytest.raises(RunnerWorkspaceError, match="symlink"):
            create_runner_workspace(str(link), manifest)

    def test_existing_path_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "existing_ws"
        ws.mkdir()
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        with pytest.raises(RunnerWorkspaceError, match="already exists"):
            create_runner_workspace(str(ws), manifest)


# ---------------------------------------------------------------------------
# 2. Manifest validation
# ---------------------------------------------------------------------------


class TestManifestValidation:
    def test_valid_manifest_parses(self) -> None:
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        assert manifest.schema_version == "v1"
        assert len(manifest.participants) == 2
        assert manifest.self_participant.public_id == "ptcp_owner"

    def test_unknown_fields_rejected(self) -> None:
        raw = _valid_manifest_bytes(extra_field="bad")
        with pytest.raises(RunnerManifestError, match="unknown"):
            parse_runner_manifest(raw)

    def test_oversized_manifest_rejected(self) -> None:
        big = b"x" * 70_000
        with pytest.raises(RunnerManifestError, match="maximum size"):
            parse_runner_manifest(big)

    def test_duplicate_participant_ids_rejected(self) -> None:
        raw = _valid_manifest_bytes(
            participants=[
                {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
                {
                    "public_id": "ptcp_owner",
                    "display_name": "OwnerDuplicate",
                    "is_self": False,
                },
            ]
        )
        with pytest.raises(RunnerManifestError, match="Duplicate"):
            parse_runner_manifest(raw)

    def test_zero_is_self_rejected(self) -> None:
        raw = _valid_manifest_bytes(
            participants=[
                {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": False},
            ]
        )
        with pytest.raises(RunnerManifestError, match="exactly one is_self"):
            parse_runner_manifest(raw)

    def test_multiple_is_self_rejected(self) -> None:
        raw = _valid_manifest_bytes(
            participants=[
                {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
                {"public_id": "ptcp_alice", "display_name": "Alice", "is_self": True},
            ]
        )
        with pytest.raises(RunnerManifestError, match="exactly one is_self"):
            parse_runner_manifest(raw)

    def test_malformed_json_rejected(self) -> None:
        with pytest.raises(RunnerManifestError, match="not valid JSON"):
            parse_runner_manifest(b"{invalid json")

    def test_manifest_hash_is_sha256_of_bytes(self) -> None:
        raw = _valid_manifest_bytes()
        manifest = parse_runner_manifest(raw)
        assert manifest.manifest_sha256 == hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 3. Staging DB reopen
# ---------------------------------------------------------------------------


class TestStagingDatabaseReopen:
    def test_reopen_after_close(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        conn.close()

        reopened = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        assert reopened is not None
        # Verify we can query.
        row = reopened.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
        assert int(row[0]) == len(TEMP_DB_MIGRATION_PATHS)
        reopened.close()

    def test_missing_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(StagingDatabaseError, match="does not exist"):
            open_staging_database(tmp_path / "nonexistent.sqlite")

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "real.sqlite"
        conn = create_staging_database(db_path)
        conn.close()
        link = tmp_path / "link.sqlite"
        link.symlink_to(db_path)
        with pytest.raises(StagingDatabaseError, match="symlink"):
            open_staging_database(link)

    def test_copied_db_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "original.sqlite"
        conn = create_staging_database(db_path)
        conn.close()
        copy_path = tmp_path / "copy.sqlite"
        copy_path.write_bytes(db_path.read_bytes())
        with pytest.raises(StagingDatabaseError, match="not bound"):
            open_staging_database(copy_path)

    def test_live_db_rejected(self) -> None:
        with pytest.raises(StagingDatabaseError, match="live database"):
            open_staging_database(LIVE_DB_PATH)


# ---------------------------------------------------------------------------
# 4. Participant bootstrap
# ---------------------------------------------------------------------------


class TestParticipantBootstrap:
    def test_first_bootstrap_succeeds(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        result = bootstrap_participants(conn, manifest)

        assert result.participant_public_ids == ("ptcp_owner", "ptcp_alice")
        assert result.replayed is False
        assert result.manifest_hash == manifest.manifest_sha256

        # Verify DB content.
        rows = conn.execute("SELECT public_id FROM participants ORDER BY public_id").fetchall()
        assert [str(r[0]) for r in rows] == ["ptcp_alice", "ptcp_owner"]
        conn.close()

    def test_exact_replay_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        first = bootstrap_participants(conn, manifest)
        second = bootstrap_participants(conn, manifest)

        assert second.replayed is True
        assert second.bootstrap_hash == first.bootstrap_hash
        assert second.participant_public_ids == first.participant_public_ids

        # Still only 2 participants.
        count = conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        assert count == 2
        conn.close()

    def test_conflict_rejected_and_rolled_back(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.models import RunnerParticipantError

        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        bootstrap_participants(conn, manifest)

        # Conflicting manifest: same ID, different display_name.
        conflict_raw = _valid_manifest_bytes(
            participants=[
                {"public_id": "ptcp_owner", "display_name": "Different", "is_self": True},
                {"public_id": "ptcp_alice", "display_name": "Alice", "is_self": False},
            ]
        )
        conflict_manifest = parse_runner_manifest(conflict_raw)
        with pytest.raises(RunnerParticipantError, match="typed conflict"):
            bootstrap_participants(conn, conflict_manifest)

        # Verify rollback: still original data.
        row = conn.execute(
            "SELECT display_name FROM participants WHERE public_id = 'ptcp_owner'"
        ).fetchone()
        assert str(row[0]) == "Owner"
        conn.close()


# ---------------------------------------------------------------------------
# 5. Workspace recovery
# ---------------------------------------------------------------------------


class TestWorkspaceRecovery:
    def test_recover_after_init(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        raw = _valid_manifest_bytes()
        manifest = parse_runner_manifest(raw)
        create_runner_workspace(str(ws), manifest)

        recovered = recover_runner_workspace(str(ws), manifest)
        assert recovered.workspace_path == str(ws.resolve())
        assert recovered.manifest_hash == manifest.manifest_sha256

    def test_manifest_hash_mismatch_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        create_runner_workspace(str(ws), manifest)

        # Different manifest bytes -> different hash.
        other_manifest = parse_runner_manifest(_valid_manifest_bytes(workspace_identity="ws_other"))
        with pytest.raises(RunnerWorkspaceError, match="mismatch"):
            recover_runner_workspace(str(ws), other_manifest)

    def test_missing_workspace_rejected(self, tmp_path: Path) -> None:
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        with pytest.raises(RunnerWorkspaceError, match="does not exist"):
            recover_runner_workspace(str(tmp_path / "missing"), manifest)


# ---------------------------------------------------------------------------
# 6. Authorization recovery (integration)
# ---------------------------------------------------------------------------


class TestAuthorizationRecovery:
    def test_recover_authorization_from_durable_truth(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        authorization, auth_id = _create_full_authorization(conn, tmp_path)

        # Close and reopen.
        conn.close()
        reopened = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)

        recovered = load_persisted_receipt_finalization_authorization(reopened, auth_id)
        assert recovered.authorization_id == auth_id
        assert recovered.confirmation_id == authorization.confirmation_id
        assert recovered.content_hash == authorization.content_hash
        assert recovered.actor_type == "human"
        assert recovered.actor_id == "owner"
        assert recovered.prepared.calculation_snapshot_hash == (
            authorization.prepared.calculation_snapshot_hash
        )
        assert recovered.prepared.active_fact_set_binding == (
            authorization.prepared.active_fact_set_binding
        )
        reopened.close()

    def test_missing_authorization_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        with pytest.raises(BridgeRecoveryError, match="not found"):
            load_persisted_receipt_finalization_authorization(conn, "authz_nonexistent")
        conn.close()

    def test_recovery_is_zero_write(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        _, auth_id = _create_full_authorization(conn, tmp_path)

        # Snapshot DB file hash before recovery.
        conn.close()
        hash_before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        reopened = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        load_persisted_receipt_finalization_authorization(reopened, auth_id)
        reopened.close()

        hash_after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert hash_before == hash_after

    def test_authorization_not_consumed_by_recovery(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        _, auth_id = _create_full_authorization(conn, tmp_path)
        conn.close()

        reopened = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        load_persisted_receipt_finalization_authorization(reopened, auth_id)

        # State must still be 'authorized', not 'consumed'.
        row = reopened.execute(
            "SELECT authorization_state FROM receipt_finalization_authorizations "
            "WHERE authorization_id = ?",
            (auth_id,),
        ).fetchone()
        assert str(row[0]) == "authorized"
        reopened.close()


# ---------------------------------------------------------------------------
# 7. CLI envelope
# ---------------------------------------------------------------------------


class TestCLIEnvelope:
    def test_init_cli_json_output(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.cli import main

        ws = tmp_path / "cli_ws"
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(_valid_manifest_bytes())

        exit_code = main(
            [
                "init",
                "--workspace",
                str(ws),
                "--manifest",
                str(manifest_path),
            ]
        )
        assert exit_code == 0

    def test_recover_cli_json_output(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.cli import main

        ws = tmp_path / "cli_ws"
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(_valid_manifest_bytes())

        # Init first.
        assert main(["init", "--workspace", str(ws), "--manifest", str(manifest_path)]) == 0
        # Then recover.
        assert main(["recover", "--workspace", str(ws), "--manifest", str(manifest_path)]) == 0

    def test_cli_error_exit_code(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.cli import main

        exit_code = main(
            [
                "init",
                "--workspace",
                str(tmp_path / "ws"),
                "--manifest",
                str(tmp_path / "nonexistent.json"),
            ]
        )
        assert exit_code == 1


# ---------------------------------------------------------------------------
# 8. Safety boundaries
# ---------------------------------------------------------------------------


class TestSafetyBoundaries:
    def test_live_db_untouched(self, tmp_path: Path) -> None:
        """database/finance.db must not be modified by any runner operation."""
        if not LIVE_DB_PATH.exists():
            pytest.skip("Live DB not present")
        hash_before = hashlib.sha256(LIVE_DB_PATH.read_bytes()).hexdigest()

        ws = tmp_path / "ws"
        manifest = parse_runner_manifest(_valid_manifest_bytes())
        create_runner_workspace(str(ws), manifest)

        hash_after = hashlib.sha256(LIVE_DB_PATH.read_bytes()).hexdigest()
        assert hash_before == hash_after

    def test_production_does_not_import_tests(self) -> None:
        """finance_core/receipt_staging_runner must not import from tests."""
        import finance_core.receipt_staging_runner as pkg

        pkg_path = Path(pkg.__file__).resolve().parent
        for py_file in pkg_path.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            assert "from tests" not in source, f"{py_file.name} imports from tests"
            assert "import tests" not in source, f"{py_file.name} imports tests"
