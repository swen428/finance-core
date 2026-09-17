"""S4 SELECT-only reader contract tests (Owner's 2026-08-05 reader authorization).

Verifies the two owning-module readers added for finalization-aware
get_status: zero side effects, strictly SELECT-only statements, no
caching, staging-only acceptance with live-database refusal, and bounded
projections with no monetary derivation.  All data is temporary,
synthetic, and staging-authorised.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import openclaw_staging_bridge_support_v1 as support
import pytest

from finance_core.receipt_finalization.stage_read import (
    ReceiptFinalizationStageRead,
    read_receipt_finalization_stage,
)
from finance_core.receipt_staging_runner.participants import ParticipantRead, read_participants
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import StagingDatabaseError, create_staging_database
from tests.test_openclaw_staging_bridge_finalize_status_v1 import (
    authorize_and_finalize,
    bootstrap_self_participant,
    first_snapshot_refusal,
    receipt_identities,
    run_authorize,
    setup_confirmed_receipt_proposal,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LIVE_DB_PATH = _PROJECT_ROOT / "database" / "finance.db"


class SelectOnlyProxy:
    """Connection wrapper that fails closed on any non-SELECT statement.

    Write attempts, DDL, and transaction control all raise immediately, so
    any hidden side effect in a reader turns a test red instead of
    silently mutating durable truth.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql: str, parameters: tuple = ()) -> Any:
        stripped = sql.strip()
        first_word = stripped.split()[0].upper() if stripped else ""
        if first_word not in {"SELECT", "WITH", "PRAGMA"}:
            raise AssertionError(f"non-SELECT statement in SELECT-only reader: {stripped!r}")
        self.statements.append(stripped)
        return self._conn.execute(sql, parameters)

    def commit(self) -> None:
        raise AssertionError("SELECT-only reader must never commit")

    def rollback(self) -> None:
        raise AssertionError("SELECT-only reader must never roll back")

    def close(self) -> None:
        self._conn.close()


def all_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0])
        for table in tables
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> support.BridgeWorkspace:
    return support.create_bridge_workspace(tmp_path)


def reach_authorized_state(
    workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, str]:
    bootstrap_self_participant(workspace)
    review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
    refused = first_snapshot_refusal(workspace, review, monkeypatch)
    expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
    authorize = run_authorize(workspace, review, expected_hash)
    assert authorize.exit_code == 0
    return review, expected_hash


class TestStageReaderSelectOnly:
    def test_projection_through_select_only_proxy(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        review, expected_hash = reach_authorized_state(workspace, monkeypatch)
        _command_id, receipt_public_id = receipt_identities(review)

        conn = support.open_database(workspace)
        try:
            proxy = SelectOnlyProxy(conn)
            stage = read_receipt_finalization_stage(
                proxy,
                receipt_public_id=receipt_public_id,  # type: ignore[arg-type]
            )
        finally:
            conn.close()

        assert isinstance(stage, ReceiptFinalizationStageRead)
        assert stage.authorization_state == "authorized"
        assert stage.calculation_snapshot_hash == expected_hash
        assert stage.authorization_id is not None
        assert stage.finalization_public_id is None
        assert stage.transaction_public_id is None
        assert all(
            statement.strip().upper().startswith(("SELECT", "WITH", "PRAGMA"))
            for statement in proxy.statements
        )
        # Bounded projection: no monetary fields exist on the dataclass.
        assert not {
            name
            for name in stage.__dataclass_fields__
            if name
            in {
                "amount",
                "currency",
                "share_amount",
                "payer_participant_public_id",
                "participant_public_ids",
            }
        }

    def test_no_side_effects_on_any_table(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        review, expected_hash = reach_authorized_state(workspace, monkeypatch)
        _command_id, receipt_public_id = receipt_identities(review)

        conn = support.open_database(workspace)
        try:
            before = all_table_counts(conn)
            read_receipt_finalization_stage(conn, receipt_public_id=receipt_public_id)
            read_receipt_finalization_stage(conn, receipt_public_id="rcpt_unknown")
            read_participants(conn)
            assert all_table_counts(conn) == before
        finally:
            conn.close()

    def test_absent_records_yield_none_fields(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        try:
            stage = read_receipt_finalization_stage(conn, receipt_public_id="rcpt_nonexistent")
            assert stage == ReceiptFinalizationStageRead(
                receipt_public_id="rcpt_nonexistent",
                authorization_id=None,
                authorization_state=None,
                calculation_run_public_id=None,
                calculation_snapshot_id=None,
                calculation_snapshot_hash=None,
                finalization_public_id=None,
                finalization_status=None,
                transaction_public_id=None,
            )
            # Second read returns the identical result (no caching, no drift).
            assert (
                read_receipt_finalization_stage(conn, receipt_public_id="rcpt_nonexistent") == stage
            )
        finally:
            conn.close()

    def test_live_database_is_refused(self) -> None:
        if not _LIVE_DB_PATH.exists():
            pytest.skip("live database fixture not present")
        conn = sqlite3.connect(f"file:{_LIVE_DB_PATH}?mode=ro", uri=True)
        try:
            with pytest.raises(StagingDatabaseError):
                read_receipt_finalization_stage(conn, receipt_public_id="rcpt_anything")
        finally:
            conn.close()

    def test_blank_receipt_identity_is_refused(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        try:
            for invalid in ("", "   "):
                with pytest.raises(ValueError):
                    read_receipt_finalization_stage(conn, receipt_public_id=invalid)
        finally:
            conn.close()


class TestParticipantReaderSelectOnly:
    def test_ordered_bounded_projection(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bootstrap_self_participant(workspace)
        reach_authorized_state(workspace, monkeypatch)
        conn = support.open_database(workspace)
        try:
            proxy = SelectOnlyProxy(conn)
            rows = read_participants(proxy)  # type: ignore[arg-type]
        finally:
            conn.close()
        assert rows == (ParticipantRead(public_id="ptcp_owner", is_self=True, is_active=True),)
        assert all(
            statement.strip().upper().startswith(("SELECT", "WITH", "PRAGMA"))
            for statement in proxy.statements
        )

    def test_empty_table_yields_empty_tuple(self, tmp_path: Path) -> None:
        db_path = tmp_path / "staging.sqlite"
        conn = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        try:
            assert read_participants(conn) == ()
        finally:
            conn.close()

    def test_live_database_is_refused(self) -> None:
        if not _LIVE_DB_PATH.exists():
            pytest.skip("live database fixture not present")
        conn = sqlite3.connect(f"file:{_LIVE_DB_PATH}?mode=ro", uri=True)
        try:
            with pytest.raises(StagingDatabaseError):
                read_participants(conn)
        finally:
            conn.close()


class TestFinalizedStageProjection:
    def test_finalized_stage_after_exact_once_finalize(
        self, workspace: support.BridgeWorkspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bootstrap_self_participant(workspace)
        review = setup_confirmed_receipt_proposal(workspace, monkeypatch)
        refused = first_snapshot_refusal(workspace, review, monkeypatch)
        expected_hash = refused.response["error"]["details"]["calculation_snapshot_hash"]
        finalize = authorize_and_finalize(workspace, review, expected_hash)
        assert finalize.exit_code == 0
        _command_id, receipt_public_id = receipt_identities(review)

        conn = support.open_database(workspace)
        try:
            stage = read_receipt_finalization_stage(conn, receipt_public_id=receipt_public_id)
        finally:
            conn.close()
        assert stage.authorization_state == "consumed"
        assert stage.finalization_status in ("finalized", "already_finalized")
        assert stage.transaction_public_id == finalize.response["result"]["transaction_public_id"]
        assert stage.finalization_public_id == finalize.response["result"]["finalization_public_id"]
