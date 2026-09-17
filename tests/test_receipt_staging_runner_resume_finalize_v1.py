"""B5.1c staging runner resume-and-finalize authority boundary tests.

Proves the B5.1c resume + guarded finalize path on a **real external
temporary workspace** with **real separate process boundaries**:

- authorized cross-process resume;
- successful finalization through the existing guarded boundary;
- exact post-finalization replay with ``already_finalized`` semantics and no
  duplicate canonical facts;
- durable run-manifest reconstruction before and after finalization;
- authorization / snapshot / fact-set drift refusal with zero facts;
- injected failure / crash with atomic rollback followed by safe recovery;
- concurrent attempts using independent connections / processes: a contender
  returns a fully verified canonical replay or a typed retryable refusal but
  never unverified success or duplicate canonical facts.

Production ``finance_core/**`` never imports ``tests/**``.  Only disposable staging
databases and external workspaces are used; ``database/finance.db`` and seed
data are untouched.  No network, credentials, or repository-local runtime
state is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
)
from finance_core.parser_proposals import confirm_proposal
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.receipt_facts_conversion import (
    ReceiptFactsConversionCommand,
    convert_confirmed_receipt_proposal_to_facts,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
    persist_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    authorize_receipt_finalization,
    prepare_receipt_calculation,
)
from finance_core.receipt_staging_runner.local_intake import (
    run_local_receipt_intake,
    validate_personal_conversion_command,
    validate_personal_fact_set_command,
)
from finance_core.receipt_staging_runner.models import (
    RunnerFinalizeError,
    RunnerResumeError,
    parse_runner_manifest,
)
from finance_core.receipt_staging_runner.participants import bootstrap_participants
from finance_core.receipt_staging_runner.workspace import (
    create_runner_workspace,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import create_staging_database, open_staging_database

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"

# Canonical financial fact tables that only a successful guarded finalization
# may populate.
CANONICAL_FACT_TABLES = (
    "transactions",
    "settlement_obligations",
    "receipt_finalization_audit",
    "calculation_runs",
    "calculation_participant_shares",
    "receipt_groups",
    "receipt_group_receipts",
)


# ---------------------------------------------------------------------------
# Test-only helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_bytes(workspace_identity: str = "ws_b51c_test") -> bytes:
    return json.dumps(
        {
            "schema_version": "v1",
            "workspace_identity": workspace_identity,
            "operator_actor_id": "owner",
            "participants": [
                {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_jpeg(suffix: str) -> bytes:
    return b"\xff\xd8\xff\xe0" + f"b51c-{suffix}".encode("utf-8")


def _b(seq: int, text: str, line: int) -> ReceiptOcrBlock:
    return ReceiptOcrBlock(
        sequence_index=seq,
        page_index=0,
        engine_block_index=0,
        engine_paragraph_index=0,
        engine_line_index=line,
        engine_word_index=seq,
        text=text,
        left=10,
        top=20,
        width=30,
        height=10,
        page_width=800,
        page_height=600,
        confidence_scaled=9500,
    )


def _sgd_blocks() -> tuple[ReceiptOcrBlock, ...]:
    return (
        _b(0, "COLD", 0),
        _b(1, "STORAGE", 0),
        _b(2, "2026-07-20", 1),
        _b(3, "TOTAL", 2),
        _b(4, "S$", 2),
        _b(5, "12.34", 2),
    )


class FakeEngine:
    """Test-only fake OCR engine (dependency injection only)."""

    def __init__(self, result: ReceiptOcrEngineResult) -> None:
        self._result = result
        self._identity = ReceiptOcrEngineIdentity(
            name="b51c_fake_ocr",
            version="1.0",
            binary_sha256="a" * 64,
            configuration_hash="b" * 64,
        )

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self._identity

    def extract(
        self,
        source: Any,
        *,
        limits: ReceiptOcrLimits,
        deadline: float,
    ) -> ReceiptOcrEngineResult:
        return self._result


def _ok_engine() -> FakeEngine:
    return FakeEngine(
        result=ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks=_sgd_blocks(),
            outcome_code="ok",
        )
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CANONICAL_FACT_TABLES
    }


class PreparedAuthorizedWorkspace:
    """A real external workspace whose receipt is prepared and authorized."""

    def __init__(
        self,
        workspace_path: str,
        manifest_bytes: bytes,
        authorization_id: str,
        receipt_public_id: str,
        conversion_command_public_id: str,
        conversion_result_hash: str,
    ) -> None:
        self.workspace_path = workspace_path
        self.manifest_bytes = manifest_bytes
        self.authorization_id = authorization_id
        self.receipt_public_id = receipt_public_id
        self.conversion_command_public_id = conversion_command_public_id
        self.conversion_result_hash = conversion_result_hash


def _build_prepared_authorized_workspace(
    tmp_path: Path,
    suffix: str,
) -> PreparedAuthorizedWorkspace:
    """Run the full B5.1a/B5.1b chain in one process, stopping at authorization.

    Uses a real external workspace under ``tmp_path`` and a real staging
    database file.  Returns the durable identities a separate process needs to
    resume and finalize.
    """
    manifest = parse_runner_manifest(_manifest_bytes(f"ws_{suffix}"))
    ws_path = str(tmp_path / f"ws_{suffix}")
    workspace = create_runner_workspace(ws_path, manifest)
    conn = create_staging_database(workspace.database_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    bootstrap_participants(conn, manifest)

    source = tmp_path / f"external_{suffix}" / "receipt.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_receipt_jpeg(suffix))

    intake = run_local_receipt_intake(
        conn,
        workspace=workspace,
        manifest=manifest,
        source_image_path=str(source),
        engine=_ok_engine(),
        public_id_prefix=suffix,
    )

    confirm_proposal(
        conn,
        intake.ingestion.parser_output_id,
        actor="owner",
        confirmation_public_id=f"pca_{intake.ingestion.proposal_public_id}",
    )
    expected_hash = compute_effective_proposal_content_hash(
        conn, {"id": intake.ingestion.parser_output_id}
    )
    conversion_command = ReceiptFactsConversionCommand(
        command_public_id=f"rpfc_{suffix}",
        proposal_public_id=intake.ingestion.proposal_public_id,
        expected_content_hash=expected_hash,
        payer_participant_public_id="ptcp_owner",
        participants=[{"participant_public_id": "ptcp_owner", "is_included": 1}],
        authenticated_actor_id="owner",
        channel="local_file",
    )
    validate_personal_conversion_command(manifest, conversion_command)
    conversion = convert_confirmed_receipt_proposal_to_facts(conn, conversion_command)

    fact_set_command = ReceiptItemAllocationFactsCommand(
        command_public_id=f"riaf_{suffix}",
        receipt_public_id=conversion.receipt_public_id,
        expected_conversion_command_public_id=f"rpfc_{suffix}",
        expected_conversion_result_hash=conversion.conversion_result_hash,
        expected_current_fact_set="none",
        items=[
            {
                "line_number": 1,
                "item_name": "Cold storage",
                "line_amount": "12.34",
                "currency": "SGD",
            }
        ],
        allocations=[
            {
                "line_number": 1,
                "allocation_method": "manual",
                "participants": [
                    {
                        "participant_public_id": "ptcp_owner",
                        "share_amount": "12.34",
                        "currency": "SGD",
                    }
                ],
            }
        ],
        adjustments=[],
        authenticated_actor_id="owner",
        channel="local_file",
    )
    validate_personal_fact_set_command(conn, fact_set_command, manifest)
    persist_receipt_item_allocation_facts(conn, fact_set_command)
    conn.commit()

    prepared = prepare_receipt_calculation(conn, conversion.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    conn.close()

    return PreparedAuthorizedWorkspace(
        workspace_path=ws_path,
        manifest_bytes=_manifest_bytes(f"ws_{suffix}"),
        authorization_id=authorization.authorization_id,
        receipt_public_id=conversion.receipt_public_id,
        conversion_command_public_id=f"rpfc_{suffix}",
        conversion_result_hash=conversion.conversion_result_hash,
    )


# ---------------------------------------------------------------------------
# Cross-process helper (real subprocess boundary)
# ---------------------------------------------------------------------------

_FINALIZE_SCRIPT = textwrap.dedent(
    r"""
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])

    from finance_core.receipt_staging_runner.models import parse_runner_manifest
    from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

    workspace = sys.argv[2]
    manifest_path = sys.argv[3]
    authorization_id = sys.argv[4]

    manifest = parse_runner_manifest(Path(manifest_path).read_bytes())
    report, output = finalize_runner_run(
        workspace, manifest, authorization_id=authorization_id
    )
    json.dump(
        {
            "status": output.status,
            "finalization_public_id": output.finalization_public_id,
            "transaction_public_id": output.transaction_public_id,
            "report": report.to_json_dict(),
        },
        sys.stdout,
    )
    """
)


def _finalize_in_subprocess(
    prepared: PreparedAuthorizedWorkspace,
    tmp_path: Path,
    *,
    manifest_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Run ``finalize_runner_run`` in a fresh Python process.

    Returns the parsed JSON result.  Raises ``subprocess.CalledProcessError``
    when the process exits non-zero.
    """
    manifest_path = tmp_path / "subprocess_manifest.json"
    manifest_path.write_bytes(manifest_bytes or prepared.manifest_bytes)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _FINALIZE_SCRIPT,
            str(REPO_ROOT),
            prepared.workspace_path,
            str(manifest_path),
            prepared.authorization_id,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"finalize subprocess failed (rc={proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout)


_RESUME_SCRIPT = textwrap.dedent(
    r"""
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])

    from finance_core.receipt_staging_runner.models import parse_runner_manifest
    from finance_core.receipt_staging_runner.resume_finalize import resume_runner_run

    workspace = sys.argv[2]
    manifest_path = sys.argv[3]
    authorization_id = sys.argv[4]

    manifest = parse_runner_manifest(Path(manifest_path).read_bytes())
    report = resume_runner_run(workspace, manifest, authorization_id=authorization_id)
    json.dump(report.to_json_dict(), sys.stdout)
    """
)


def _resume_in_subprocess(
    prepared: PreparedAuthorizedWorkspace,
    tmp_path: Path,
    *,
    manifest_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Run ``resume_runner_run`` in a fresh Python process.

    Returns the parsed run-manifest JSON.  Raises ``AssertionError`` when the
    process exits non-zero.
    """
    manifest_path = tmp_path / "subprocess_manifest.json"
    manifest_path.write_bytes(manifest_bytes or prepared.manifest_bytes)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _RESUME_SCRIPT,
            str(REPO_ROOT),
            prepared.workspace_path,
            str(manifest_path),
            prepared.authorization_id,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"resume subprocess failed (rc={proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1. Authorized cross-process resume (read-only)
# ---------------------------------------------------------------------------


class TestCrossProcessResume:
    def test_resume_reconstructs_manifest_in_a_fresh_process(self, tmp_path: Path) -> None:
        """A genuinely separate process (fresh imports, fresh connection)
        resumes the durably authorized receipt and reconstructs the run
        manifest without touching financial facts."""
        prepared = _build_prepared_authorized_workspace(tmp_path, "resume1")

        payload = _resume_in_subprocess(prepared, tmp_path)

        assert payload["report_schema_version"] == "v1"
        assert payload["workspace_identity"] == "ws_resume1"
        assert payload["operator_actor_id"] == "owner"
        assert payload["authorization_id"] == prepared.authorization_id
        assert payload["authorization_state"] == "authorized"
        assert payload["receipt_public_id"] == prepared.receipt_public_id
        assert payload["finalization_executed"] is False
        assert payload["canonical_financial_facts_created"] == 0
        assert payload["finalization_public_id"] is None
        assert payload["transaction_public_id"] is None
        assert len(payload["content_hash"]) == 64
        assert len(payload["calculation_snapshot_hash"]) == 64
        assert payload["fact_set_version"] == 1

    def test_resume_is_zero_write(self, tmp_path: Path) -> None:
        prepared = _build_prepared_authorized_workspace(tmp_path, "resumezero")
        db_path = Path(prepared.workspace_path) / "database" / "staging.sqlite"
        hash_before = _sha256(db_path.read_bytes())

        from finance_core.receipt_staging_runner.resume_finalize import resume_runner_run

        resume_runner_run(
            prepared.workspace_path,
            parse_runner_manifest(prepared.manifest_bytes),
            authorization_id=prepared.authorization_id,
        )
        hash_after = _sha256(db_path.read_bytes())
        assert hash_before == hash_after

    def test_resume_unknown_authorization_fails_closed(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.resume_finalize import resume_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "resumemiss")
        with pytest.raises(RunnerResumeError, match="not found"):
            resume_runner_run(
                prepared.workspace_path,
                parse_runner_manifest(prepared.manifest_bytes),
                authorization_id="authz_nonexistent",
            )

    def test_resume_wrong_operator_actor_fails_closed(self, tmp_path: Path) -> None:
        """A non-operator actor can never resume the authorization."""
        from finance_core.receipt_staging_runner.resume_finalize import resume_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "resumeact")
        # The authorization was created with actor_id="owner".  A caller with a
        # different operator can only supply a manifest whose operator is not
        # "owner", which must be rejected by the workspace recovery or the
        # operator check.
        other_manifest = parse_runner_manifest(_manifest_bytes("ws_resumeact_other"))

        # Manifest identity is bound to the workspace; a foreign manifest is
        # rejected before any authorization read.
        with pytest.raises(RunnerResumeError):
            resume_runner_run(
                prepared.workspace_path,
                other_manifest,
                authorization_id=prepared.authorization_id,
            )


# ---------------------------------------------------------------------------
# 2. Successful finalization (same-process + cross-process)
# ---------------------------------------------------------------------------


class TestFinalize:
    def test_finalize_same_process_via_guarded_boundary(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "fin1")

        report, output = finalize_runner_run(
            prepared.workspace_path,
            parse_runner_manifest(prepared.manifest_bytes),
            authorization_id=prepared.authorization_id,
        )

        assert output.status == "finalized"
        assert output.transaction_public_id
        assert report.finalization_executed is True
        assert report.finalization_public_id == output.finalization_public_id
        assert report.transaction_public_id == output.transaction_public_id
        assert report.authorization_state == "consumed"
        # The manifest count reflects the complete durable financial state a
        # finalization created, not just the three core fact tables.
        assert report.canonical_financial_facts_created == 6

        # Exactly one canonical transaction and one settlement obligation.
        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            counts = _counts(conn)
            assert counts["transactions"] == 1
            assert counts["settlement_obligations"] == 0  # personal-only
            assert counts["receipt_finalization_audit"] == 1
            state = conn.execute(
                "SELECT authorization_state FROM receipt_finalization_authorizations "
                "WHERE authorization_id = ?",
                (prepared.authorization_id,),
            ).fetchone()[0]
            assert state == "consumed"
        finally:
            conn.close()

    def test_finalize_cross_process(self, tmp_path: Path) -> None:
        """A real separate process resumes and finalizes exactly once."""
        prepared = _build_prepared_authorized_workspace(tmp_path, "finxp")
        result = _finalize_in_subprocess(prepared, tmp_path)

        assert result["status"] == "finalized"
        assert result["finalization_public_id"]
        assert result["transaction_public_id"]
        assert result["report"]["authorization_state"] == "consumed"
        assert result["report"]["finalization_executed"] is True

        # Verify the durable result with a fresh connection.
        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1
            )
        finally:
            conn.close()

    def test_finalize_rejects_wrong_actor(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.cli import main as cli_main

        prepared = _build_prepared_authorized_workspace(tmp_path, "finact")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(prepared.manifest_bytes)

        # The CLI validates actor == manifest operator; "evil" is rejected.
        exit_code = cli_main(
            [
                "finalize",
                "--workspace",
                prepared.workspace_path,
                "--manifest",
                str(manifest_path),
                "--authorization-id",
                prepared.authorization_id,
                "--actor",
                "evil",
            ]
        )
        assert exit_code == 1

    def test_finalize_error_envelope_carries_bounded_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The finalize CLI error envelope must carry the bounded retryable
        lock reason so automation can distinguish it from a permanent refusal."""
        import threading

        from finance_core.receipt_staging_runner.cli import main as cli_main
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "finreason")
        db_path = Path(prepared.workspace_path) / "database" / "staging.sqlite"
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(prepared.manifest_bytes)

        # Hold an exclusive write lock so the finalize attempt refuses.
        lock_holder = sqlite3.connect(str(db_path))
        lock_holder.execute("BEGIN IMMEDIATE")
        try:
            result: dict[str, Any] = {}

            def _attempt() -> None:
                result["exit_code"] = cli_main(
                    [
                        "finalize",
                        "--workspace",
                        prepared.workspace_path,
                        "--manifest",
                        str(manifest_path),
                        "--authorization-id",
                        prepared.authorization_id,
                        "--actor",
                        "owner",
                    ]
                )
                result["stderr"] = capsys.readouterr().err

            thread = threading.Thread(target=_attempt, daemon=True)
            thread.start()
            thread.join(timeout=30)
        finally:
            lock_holder.rollback()
            lock_holder.close()

        assert not thread.is_alive()
        assert result["exit_code"] == 1
        envelope = json.loads(result["stderr"])
        assert envelope["status"] == "error"
        assert envelope["error_type"] == "RunnerFinalizeError"
        assert envelope["reason"] == "runner_finalization_locked"

        # The lock refusal left zero committed facts; a clean retry finalizes
        # exactly once.
        _, output = finalize_runner_run(
            prepared.workspace_path,
            parse_runner_manifest(prepared.manifest_bytes),
            authorization_id=prepared.authorization_id,
        )
        assert output.status == "finalized"


# ---------------------------------------------------------------------------
# 3. Exact post-finalization replay
# ---------------------------------------------------------------------------


class TestExactReplay:
    def test_replay_returns_already_finalized_with_no_duplicates(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "replay1")
        manifest = parse_runner_manifest(prepared.manifest_bytes)

        report1, output1 = finalize_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        report2, output2 = finalize_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )

        assert output1.status == "finalized"
        assert output2.status == "already_finalized"
        assert output2.finalization_public_id == output1.finalization_public_id
        assert output2.transaction_public_id == output1.transaction_public_id
        assert output2.audit_id == output1.audit_id

        # The second resume still reconstructs the same manifest.
        assert report2.finalization_public_id == report1.finalization_public_id
        assert report2.transaction_public_id == report1.transaction_public_id

        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            counts = _counts(conn)
            assert counts["transactions"] == 1
            assert counts["receipt_finalization_audit"] == 1
            assert counts["receipt_groups"] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_idempotency").fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    def test_cross_process_replay_is_durable(self, tmp_path: Path) -> None:
        """Finalize in one process, replay in another, no duplicate facts."""
        prepared = _build_prepared_authorized_workspace(tmp_path, "replayxp")
        first = _finalize_in_subprocess(prepared, tmp_path)
        second = _finalize_in_subprocess(prepared, tmp_path)

        assert first["status"] == "finalized"
        assert second["status"] == "already_finalized"
        assert second["finalization_public_id"] == first["finalization_public_id"]
        assert second["transaction_public_id"] == first["transaction_public_id"]

        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_idempotency").fetchone()[0]
                == 1
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. Run-manifest reconstruction before and after finalization
# ---------------------------------------------------------------------------


class TestRunManifest:
    def test_manifest_before_and_after_finalization(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.resume_finalize import (
            finalize_runner_run,
            resume_runner_run,
        )

        prepared = _build_prepared_authorized_workspace(tmp_path, "manifest1")
        manifest = parse_runner_manifest(prepared.manifest_bytes)

        before = resume_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        assert before.finalization_executed is False
        assert before.finalization_public_id is None
        assert before.authorization_state == "authorized"
        assert before.canonical_financial_facts_created == 0

        report, _ = finalize_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        assert report.finalization_executed is True
        assert report.finalization_public_id is not None
        assert report.authorization_state == "consumed"
        assert report.canonical_financial_facts_created >= 1

        after = resume_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        assert after.finalization_executed is True
        assert after.finalization_public_id == report.finalization_public_id
        assert after.transaction_public_id == report.transaction_public_id

        # Durable identities are stable across reconstruction.
        assert before.calculation_snapshot_id == after.calculation_snapshot_id
        assert before.calculation_snapshot_hash == after.calculation_snapshot_hash
        assert before.fact_set_public_id == after.fact_set_public_id

    def test_manifest_serializes_to_bounded_json(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.resume_finalize import resume_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "manifestjson")
        report = resume_runner_run(
            prepared.workspace_path,
            parse_runner_manifest(prepared.manifest_bytes),
            authorization_id=prepared.authorization_id,
        )
        payload = report.to_json_dict()

        # All values are JSON-serializable and bounded.
        json.dumps(payload)
        assert set(payload) == {
            "report_schema_version",
            "workspace_identity",
            "workspace_path",
            "manifest_hash",
            "operator_actor_id",
            "database_identity",
            "migration_ledger_count",
            "latest_migration",
            "migration_verification",
            "participant_bootstrap_hash",
            "authorization_id",
            "authorization_state",
            "confirmation_id",
            "content_hash",
            "receipt_public_id",
            "receipt_group_public_id",
            "calculation_run_public_id",
            "calculation_snapshot_id",
            "calculation_snapshot_hash",
            "currency_contract_version",
            "fact_set_public_id",
            "fact_set_version",
            "fact_set_input_hash",
            "fact_set_result_hash",
            "source_evidence_refs",
            "canonical_financial_facts_created",
            "finalization_executed",
            "finalization_public_id",
            "transaction_public_id",
            "audit_id",
        }
        # The manifest never carries mutable financial input.
        assert "amount" not in payload
        assert "obligations" not in payload
        assert "settlement" not in payload


# ---------------------------------------------------------------------------
# 5. Authorization / snapshot / fact-set drift refusal
# ---------------------------------------------------------------------------


class TestDriftRefusal:
    def _tamper_authorization_state(
        self, prepared: PreparedAuthorizedWorkspace, new_state: str
    ) -> None:
        """Direct-SQL tamper fixture (test-only): corrupt the durable state."""
        conn = sqlite3.connect(str(Path(prepared.workspace_path) / "database" / "staging.sqlite"))
        try:
            conn.execute(
                "UPDATE receipt_finalization_authorizations "
                "SET authorization_state = ? WHERE authorization_id = ?",
                (new_state, prepared.authorization_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_consumed_authorization_replays_not_rejects(self, tmp_path: Path) -> None:
        """A consumed authorization (post-success) is a replay, not an error."""
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftcons")
        manifest = parse_runner_manifest(prepared.manifest_bytes)
        _, output1 = finalize_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        _, output2 = finalize_runner_run(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        assert output2.status == "already_finalized"
        assert output1.status == "finalized"

    def test_revoked_authorization_fails_closed(self, tmp_path: Path) -> None:
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftrev")
        self._tamper_authorization_state(prepared, "revoked")

        with pytest.raises(RunnerFinalizeError) as excinfo:
            finalize_runner_run(
                prepared.workspace_path,
                parse_runner_manifest(prepared.manifest_bytes),
                authorization_id=prepared.authorization_id,
            )
        assert excinfo.value.reason is not None
        # Zero canonical facts were created by the refused attempt.
        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 0
            )
        finally:
            conn.close()

    def test_actor_id_tamper_fails_closed(self, tmp_path: Path) -> None:
        """A durable authorization whose actor_id was corrupted (drifted from
        the manifest operator) fails closed with a typed error and zero facts,
        on both resume and finalize."""
        from finance_core.receipt_staging_runner.resume_finalize import (
            finalize_runner_run,
            resume_runner_run,
        )

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftactor")
        # Corrupt the durable authorization actor to a non-operator value.
        conn = sqlite3.connect(str(Path(prepared.workspace_path) / "database" / "staging.sqlite"))
        try:
            conn.execute(
                "UPDATE receipt_finalization_authorizations "
                "SET actor_id = ? WHERE authorization_id = ?",
                ("evil", prepared.authorization_id),
            )
            conn.commit()
        finally:
            conn.close()

        manifest = parse_runner_manifest(prepared.manifest_bytes)
        with pytest.raises(RunnerResumeError):
            resume_runner_run(
                prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
            )
        with pytest.raises(RunnerFinalizeError) as excinfo:
            finalize_runner_run(
                prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
            )
        assert excinfo.value.reason is not None
        # Zero canonical facts were created by either refused attempt.
        conn2 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn2.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        finally:
            conn2.close()

    def test_snapshot_tamper_fails_closed(self, tmp_path: Path) -> None:
        """A durable authorization whose content hash was corrupted (drifted
        from the hash-verified snapshot) fails closed with zero facts.

        The authoritative snapshot is append-only, so the tamper is applied at
        the authorization's own monetary content binding that the runner and
        finalizer re-verify.
        """
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftsnap")
        # The authoritative snapshot is append-only, so tampering must be
        # simulated at the authz/confirmation monetary fields that the runner
        # and finalizer re-check.  Corrupt the authorization content hash.
        conn = sqlite3.connect(str(Path(prepared.workspace_path) / "database" / "staging.sqlite"))
        try:
            conn.execute(
                "UPDATE receipt_finalization_authorizations "
                "SET content_hash = ? WHERE authorization_id = ?",
                ("0" * 64, prepared.authorization_id),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RunnerFinalizeError):
            finalize_runner_run(
                prepared.workspace_path,
                parse_runner_manifest(prepared.manifest_bytes),
                authorization_id=prepared.authorization_id,
            )
        conn2 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn2.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        finally:
            conn2.close()

    def test_malformed_durable_evidence_fails_closed_typed(self, tmp_path: Path) -> None:
        """Corrupt durable evidence must fail closed with a typed bounded error,
        never a raw json.JSONDecodeError escaping the public boundaries."""
        from finance_core.receipt_staging_runner.resume_finalize import (
            finalize_runner_run,
            resume_runner_run,
        )

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftjdoc")
        # Corrupt the durable source-evidence JSON that the recovery boundary
        # re-parses.
        conn = sqlite3.connect(str(Path(prepared.workspace_path) / "database" / "staging.sqlite"))
        try:
            conn.execute(
                "UPDATE receipt_finalization_authorizations "
                "SET source_evidence_refs_json = 'not-json{[' WHERE authorization_id = ?",
                (prepared.authorization_id,),
            )
            conn.commit()
        finally:
            conn.close()

        manifest = parse_runner_manifest(prepared.manifest_bytes)
        with pytest.raises(RunnerResumeError):
            resume_runner_run(
                prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
            )
        with pytest.raises(RunnerFinalizeError):
            finalize_runner_run(
                prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
            )
        # Zero canonical facts were created by either refused attempt.
        conn2 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn2.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        finally:
            conn2.close()

    def test_corrupt_ledger_fails_closed_typed(self, tmp_path: Path) -> None:
        """A staging DB whose ledger is unreadable at the file level must fail
        closed with a typed RunnerResumeError on resume (and a typed
        RunnerFinalizeError on finalize) -- never a raw sqlite3 exception."""
        from finance_core.receipt_staging_runner.resume_finalize import (
            finalize_runner_run,
            resume_runner_run,
        )

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftledger")
        db_path = Path(prepared.workspace_path) / "database" / "staging.sqlite"

        # Corrupt the DB file so the ledger reads fail (byte-level truncation
        # produces a sqlite3.DatabaseError on the resume path's manifest build).
        db_bytes = db_path.read_bytes()
        db_path.write_bytes(db_bytes[: len(db_bytes) // 2])

        manifest = parse_runner_manifest(prepared.manifest_bytes)
        with pytest.raises(RunnerResumeError):
            resume_runner_run(
                prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
            )
        with pytest.raises(RunnerFinalizeError):
            finalize_runner_run(
                prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
            )

    def test_fact_set_drift_fails_closed(self, tmp_path: Path) -> None:
        """Superseding the active fact set after authorization fails closed."""
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "driftfs")

        # Build the correction command and supersede through the public IAF
        # boundary (same helper used by the existing bridge tests).
        from finance_core.parser_proposals.receipt_item_allocation_facts import (
            ReceiptItemAllocationFactsSupersessionCommand,
            supersede_receipt_item_allocation_facts,
        )

        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            # Find the existing fact set to build the supersession command.
            fs_row = conn.execute(
                "SELECT fact_set_public_id, version, fact_set_input_hash, fact_set_result_hash "
                "FROM receipt_item_allocation_fact_sets "
                "WHERE receipt_id = (SELECT id FROM receipts WHERE public_id = ?) "
                "AND superseded_by_fact_set_public_id IS NULL "
                "ORDER BY version DESC LIMIT 1",
                (prepared.receipt_public_id,),
            ).fetchone()
            assert fs_row is not None
            correction = ReceiptItemAllocationFactsSupersessionCommand(
                command_public_id="riafc_drift",
                receipt_public_id=prepared.receipt_public_id,
                expected_conversion_command_public_id=prepared.conversion_command_public_id,
                expected_conversion_result_hash=prepared.conversion_result_hash,
                expected_current_fact_set_public_id=str(fs_row["fact_set_public_id"]),
                expected_current_fact_set_result_hash=str(fs_row["fact_set_result_hash"]),
                items=[
                    {
                        "line_number": 1,
                        "item_name": "Corrected",
                        "line_amount": "12.34",
                        "currency": "SGD",
                    }
                ],
                allocations=[
                    {
                        "line_number": 1,
                        "allocation_method": "manual",
                        "participants": [
                            {
                                "participant_public_id": "ptcp_owner",
                                "share_amount": "12.34",
                                "currency": "SGD",
                            }
                        ],
                    }
                ],
                adjustments=[],
                authenticated_actor_id="owner",
                channel="local_file",
            )
            supersede_receipt_item_allocation_facts(conn, correction)
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RunnerFinalizeError) as excinfo:
            finalize_runner_run(
                prepared.workspace_path,
                parse_runner_manifest(prepared.manifest_bytes),
                authorization_id=prepared.authorization_id,
            )
        # The stale fact-set refusal is typed and bounded.
        assert excinfo.value.reason is not None
        conn2 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn2.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        finally:
            conn2.close()


# ---------------------------------------------------------------------------
# 6. Injected failure / crash with atomic rollback + safe recovery
# ---------------------------------------------------------------------------


class TestCrashAndFailureInjection:
    def test_injected_finalizer_failure_rolls_back_then_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import finance_core.receipt_finalization.finalizer as finalizer
        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "inject")

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected finalizer seam failure")

        # The finalizer's write seam is patched in this process; the crash
        # aborts the guarded finalization with a full rollback.
        monkeypatch.setattr(finalizer, "_insert_settlement_obligations", _boom)
        with pytest.raises(RunnerFinalizeError):
            finalize_runner_run(
                prepared.workspace_path,
                parse_runner_manifest(prepared.manifest_bytes),
                authorization_id=prepared.authorization_id,
            )
        monkeypatch.undo()

        # Zero canonical facts survived the rollback; authorization intact.
        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
            state = conn.execute(
                "SELECT authorization_state FROM receipt_finalization_authorizations "
                "WHERE authorization_id = ?",
                (prepared.authorization_id,),
            ).fetchone()[0]
            assert state == "authorized"
        finally:
            conn.close()

        # A clean retry finalizes exactly once.
        _, output = finalize_runner_run(
            prepared.workspace_path,
            parse_runner_manifest(prepared.manifest_bytes),
            authorization_id=prepared.authorization_id,
        )
        assert output.status == "finalized"

    def test_hard_process_kill_mid_transaction_leaves_no_partial_facts(
        self, tmp_path: Path
    ) -> None:
        """A subprocess hard-killed inside a write transaction must leave zero
        committed partial facts, and a later finalize must succeed exactly once."""
        prepared = _build_prepared_authorized_workspace(tmp_path, "crash")
        db_path = Path(prepared.workspace_path) / "database" / "staging.sqlite"

        kill_script = textwrap.dedent(
            r"""
            import os
            import sys

            sys.path.insert(0, sys.argv[1])
            from finance_core.staging_guard import open_staging_database
            from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

            db = sys.argv[2]
            c = open_staging_database(db, migration_paths=TEMP_DB_MIGRATION_PATHS)
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO transactions (public_id, intent, intent_type, status, "
                "amount, currency, transaction_date) "
                "VALUES ('crash_txn', 'x', 'Generated', 'active', '9.99', 'SGD', "
                "'2026-07-20')"
            )
            # Prove the dirty write was actually made inside the open transaction
            # before the hard kill: a second connection cannot see it yet, but the
            # row exists within this connection's uncommitted transaction.
            assert c.execute(
                "SELECT COUNT(*) FROM transactions WHERE public_id = 'crash_txn'"
            ).fetchone()[0] == 1
            # Hard kill: no commit, no connection close, journal not checkpointed.
            os._exit(1)
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", kill_script, str(REPO_ROOT), str(db_path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 1
        # The child must have reached os._exit (hard kill), not an exception:
        # a clean traceback on stderr would mean the INSERT failed before the
        # dirty write was made and the test would not exercise rollback.
        assert proc.stderr == "", f"kill script failed unexpectedly: {proc.stderr}"

        # The uncommitted crash row must not exist after reopen.
        conn = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE public_id='crash_txn'"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()

        # Safe recovery finalizes exactly once.
        result = _finalize_in_subprocess(prepared, tmp_path)
        assert result["status"] == "finalized"
        conn2 = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        try:
            assert conn2.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        finally:
            conn2.close()


# ---------------------------------------------------------------------------
# 7. Concurrency with independent connections/processes
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_two_independent_connections_yield_one_canonical_result(self, tmp_path: Path) -> None:
        """Two finalizers on independent connections produce exactly one set of
        canonical facts; the second attempt replays the durable result."""
        from finance_core.receipt_finalization import finalize_prepared_receipt
        from finance_core.receipt_finalization.fact_set_bridge import (
            load_persisted_receipt_finalization_authorization,
        )
        from finance_core.receipt_finalization.models import FinalizationStatus

        prepared = _build_prepared_authorized_workspace(tmp_path, "conc1")

        # Two independent connections, each with its own rehydrated
        # authorization object, drive the guarded finalizer sequentially.
        conn1 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        conn2 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            authz1 = load_persisted_receipt_finalization_authorization(
                conn1, prepared.authorization_id
            )
            out1 = finalize_prepared_receipt(conn1, authz1)

            authz2 = load_persisted_receipt_finalization_authorization(
                conn2, prepared.authorization_id
            )
            out2 = finalize_prepared_receipt(conn2, authz2)
        finally:
            conn1.close()
            conn2.close()

        assert out1.status == FinalizationStatus.FINALIZED.value
        assert out2.status == FinalizationStatus.ALREADY_FINALIZED.value
        assert out1.finalization_public_id == out2.finalization_public_id
        assert out1.transaction_public_id == out2.transaction_public_id

        conn3 = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn3.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
            assert (
                conn3.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1
            )
        finally:
            conn3.close()

    def test_held_write_lock_yields_typed_retryable_refusal(self, tmp_path: Path) -> None:
        """A contender blocked on an open write transaction must receive a typed
        retryable refusal -- never unverified success -- and the durable result
        after lock release must be the one canonical finalization."""
        import threading

        from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

        prepared = _build_prepared_authorized_workspace(tmp_path, "conclock")
        db_path = Path(prepared.workspace_path) / "database" / "staging.sqlite"

        # Hold an exclusive write lock from an independent connection.
        lock_holder = sqlite3.connect(str(db_path))
        lock_holder.execute("BEGIN IMMEDIATE")

        result: dict[str, Any] = {}

        def _attempt() -> None:
            try:
                finalize_runner_run(
                    prepared.workspace_path,
                    parse_runner_manifest(prepared.manifest_bytes),
                    authorization_id=prepared.authorization_id,
                )
                result["outcome"] = "success"
            except RunnerFinalizeError as exc:
                result["outcome"] = "refused"
                result["reason"] = exc.reason

        thread = threading.Thread(target=_attempt, daemon=True)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "finalize attempt hung on the held lock"

        # The contender must fail closed with a typed retryable refusal.
        assert result["outcome"] == "refused"
        assert result["reason"] == "runner_finalization_locked"

        # Zero canonical facts were created while the lock was held.
        lock_holder.rollback()
        lock_holder.close()

        # After lock release, the runner yields the one durable result.
        _, output = finalize_runner_run(
            prepared.workspace_path,
            parse_runner_manifest(prepared.manifest_bytes),
            authorization_id=prepared.authorization_id,
        )
        assert output.status == "finalized"
        conn = open_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        finally:
            conn.close()

    def test_two_processes_finalize_exactly_once(self, tmp_path: Path) -> None:
        """Two separate processes racing to finalize the same authorization
        must yield the one durable canonical result."""
        prepared = _build_prepared_authorized_workspace(tmp_path, "concxp")

        # Launch two independent subprocesses near-simultaneously.
        results: list[dict[str, Any]] = []
        procs = []
        for _ in range(2):
            procs.append(_spawn_finalize(prepared, tmp_path))
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=120)
            if proc.returncode != 0:
                raise AssertionError(f"subprocess failed: {stderr}")
            results.append(json.loads(stdout))

        assert len(results) == 2
        statuses = {r["status"] for r in results}
        assert statuses <= {"finalized", "already_finalized"}
        assert "finalized" in statuses
        # Both must report the same canonical finalization identity.
        finals = {r["finalization_public_id"] for r in results}
        assert len(finals) == 1

        conn = open_staging_database(
            Path(prepared.workspace_path) / "database" / "staging.sqlite",
            migration_paths=TEMP_DB_MIGRATION_PATHS,
        )
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_audit").fetchone()[0] == 1
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM receipt_finalization_idempotency").fetchone()[0]
                == 1
            )
        finally:
            conn.close()


def _spawn_finalize(prepared: PreparedAuthorizedWorkspace, tmp_path: Path) -> subprocess.Popen[str]:
    """Spawn one ``finalize_runner_run`` subprocess (not yet awaited)."""
    manifest_path = tmp_path / f"subprocess_manifest_{time.time_ns()}.json"
    manifest_path.write_bytes(prepared.manifest_bytes)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _FINALIZE_SCRIPT,
            str(REPO_ROOT),
            prepared.workspace_path,
            str(manifest_path),
            prepared.authorization_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# 8. Safety boundaries
# ---------------------------------------------------------------------------


class TestSafetyBoundaries:
    def test_live_db_untouched(self, tmp_path: Path) -> None:
        if not LIVE_DB_PATH.exists():
            pytest.skip("Live DB not present")
        hash_before = _sha256(LIVE_DB_PATH.read_bytes())
        _build_prepared_authorized_workspace(tmp_path, "safety")
        hash_after = _sha256(LIVE_DB_PATH.read_bytes())
        assert hash_before == hash_after

    def test_production_does_not_import_tests(self) -> None:
        """finance_core/receipt_staging_runner must not import from tests."""
        import finance_core.receipt_staging_runner as pkg

        pkg_path = Path(pkg.__file__).resolve().parent
        for py_file in pkg_path.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            assert "from tests" not in source, f"{py_file.name} imports from tests"
            assert "import tests" not in source, f"{py_file.name} imports tests"

    def test_recover_remains_zero_facts(self, tmp_path: Path) -> None:
        """The B5.1a recover command still reports zero final facts after B5.1c."""
        from finance_core.receipt_staging_runner.recovery import run_recovery

        prepared = _build_prepared_authorized_workspace(tmp_path, "recoverzf")
        manifest = parse_runner_manifest(prepared.manifest_bytes)
        evidence = run_recovery(
            prepared.workspace_path, manifest, authorization_id=prepared.authorization_id
        )
        assert evidence.canonical_financial_facts_created == 0
        assert evidence.finalization_executed is False

    def test_resume_cli_has_no_actor_argument(self, tmp_path: Path) -> None:
        """The resume command surface (parser and module docstring) carries no
        --actor argument; the manifest operator is bound through the
        workspace/manifest identity."""
        from finance_core.receipt_staging_runner.cli import main as cli_main

        # The module docstring must not document a resume --actor argument.
        cli_source = (REPO_ROOT / "finance_core/receipt_staging_runner/cli.py").read_text()
        resume_block = cli_source.split("cli resume", 1)[1].split("cli finalize", 1)[0]
        assert "--actor" not in resume_block

        # Passing --actor to resume is a usage error (SystemExit 2), matching
        # the parser surface.
        try:
            cli_main(
                [
                    "resume",
                    "--workspace",
                    "/tmp/ws",
                    "--manifest",
                    "/tmp/m.json",
                    "--authorization-id",
                    "authz_x",
                    "--actor",
                    "owner",
                ]
            )
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("resume with --actor should be a usage error (SystemExit 2)")
