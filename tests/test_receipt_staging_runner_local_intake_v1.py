"""B5.1b real local intake authority boundary tests.

Proves the B5.1b local intake pipeline enforces truthful local source
evidence, separate human authority stages, safe file import, personal-only
fact sets, and mandatory macOS Vision OCR.

Uses manifest-bootstrapped participant identity (no seed_people, no test-only
production prerequisite).  FakeEngine only via dependency injection in tests.

Only disposable staging databases are used; ``database/finance.db`` and seed
data are untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    ReceiptOcrSource,
)
from finance_core.parser_proposals.receipt_facts_conversion import ReceiptFactsConversionCommand
from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeCopyError,
    LocalIntakeFileError,
    LocalIntakePersonalOnlyError,
    LocalLineageError,
    import_local_receipt_file,
    require_local_runner_receipt_proposal,
    run_local_receipt_intake,
    validate_personal_conversion_command,
    validate_personal_fact_set_command,
)
from finance_core.receipt_staging_runner.models import parse_runner_manifest
from finance_core.receipt_staging_runner.participants import bootstrap_participants
from finance_core.receipt_staging_runner.workspace import create_runner_workspace
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
from finance_core.staging_guard import create_staging_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest_bytes() -> bytes:
    return json.dumps(
        {
            "schema_version": "v1",
            "workspace_identity": "ws_b51b_test",
            "operator_actor_id": "owner",
            "participants": [
                {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_jpeg(suffix: str) -> bytes:
    return b"\xff\xd8\xff\xe0" + f"b51b-{suffix}".encode("utf-8")


def _receipt_png(suffix: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + f"b51b-png-{suffix}".encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sgd_blocks() -> tuple[ReceiptOcrBlock, ...]:
    def _b(seq: int, text: str, *, line: int, left: int = 10, top: int = 20) -> ReceiptOcrBlock:
        return ReceiptOcrBlock(
            sequence_index=seq,
            page_index=0,
            engine_block_index=0,
            engine_paragraph_index=0,
            engine_line_index=line,
            engine_word_index=seq,
            text=text,
            left=left,
            top=top,
            width=30,
            height=10,
            page_width=800,
            page_height=600,
            confidence_scaled=9500,
        )

    return (
        _b(0, "COLD", line=0, left=10, top=20),
        _b(1, "STORAGE", line=0, left=60, top=20),
        _b(2, "2026-07-20", line=1, left=10, top=60),
        _b(3, "TOTAL", line=2, left=10, top=100),
        _b(4, "S$", line=2, left=80, top=100),
        _b(5, "12.34", line=2, left=140, top=100),
    )


class FakeEngine:
    """Test-only fake OCR engine (dependency injection)."""

    def __init__(
        self, result: ReceiptOcrEngineResult, *, identity: ReceiptOcrEngineIdentity | None = None
    ) -> None:
        self._result = result
        self._identity = identity or ReceiptOcrEngineIdentity(
            name="fake_ocr",
            version="1.0",
            binary_sha256="a" * 64,
            configuration_hash="b" * 64,
        )

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self._identity

    def extract(
        self, source: ReceiptOcrSource, *, limits: ReceiptOcrLimits, deadline: float
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


@pytest.fixture()
def b51b_env(tmp_path: Path) -> tuple[Any, Any, sqlite3.Connection, Path]:
    """Create workspace + staging DB + bootstrapped manifest participant.

    Returns (workspace, manifest, conn, external_dir).
    """
    manifest = parse_runner_manifest(_manifest_bytes())
    ws_path = str(tmp_path / "workspace")
    workspace = create_runner_workspace(ws_path, manifest)
    conn = create_staging_database(workspace.database_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    bootstrap_participants(conn, manifest)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    yield workspace, manifest, conn, external_dir
    conn.close()


# ---------------------------------------------------------------------------
# Local evidence: safe import
# ---------------------------------------------------------------------------


class TestSafeFileImport:
    def test_jpeg_safe_import(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("jpeg1")
        source = ext_dir / "receipt.jpg"
        source.write_bytes(content)
        original_stat = source.stat()

        copy_path, data, mime, size, ext = import_local_receipt_file(
            str(source), workspace, "test_jpeg"
        )
        assert data == content
        assert mime == "image/jpeg"
        assert size == len(content)
        assert ext == ".jpg"
        assert copy_path.exists()
        assert copy_path.read_bytes() == content
        # Original file unchanged.
        assert source.stat().st_ino == original_stat.st_ino
        assert source.read_bytes() == content

    def test_png_safe_import(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_png("png1")
        source = ext_dir / "receipt.png"
        source.write_bytes(content)

        _, _, mime, _, ext = import_local_receipt_file(str(source), workspace, "test_png")
        assert mime == "image/png"
        assert ext == ".png"

    def test_original_file_permissions_unchanged(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("perm")
        source = ext_dir / "receipt_perm.jpg"
        source.write_bytes(content)
        os.chmod(str(source), 0o644)
        original_mode = source.stat().st_mode

        import_local_receipt_file(str(source), workspace, "test_perm")
        assert source.stat().st_mode == original_mode

    def test_pdf_rejected(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "doc.pdf"
        source.write_bytes(b"%PDF-1.7\nnot-an-image")
        with pytest.raises(LocalIntakeFileError, match="not a supported format"):
            import_local_receipt_file(str(source), workspace, "test_pdf")

    def test_empty_file_rejected(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "empty.jpg"
        source.write_bytes(b"")
        with pytest.raises(LocalIntakeFileError, match="empty"):
            import_local_receipt_file(str(source), workspace, "test_empty")

    def test_missing_file_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        with pytest.raises(LocalIntakeFileError, match="Cannot open source image"):
            import_local_receipt_file(str(ext_dir / "nope.jpg"), workspace, "test_missing")

    def test_symlink_rejected(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        real = ext_dir / "real.jpg"
        real.write_bytes(_receipt_jpeg("sym"))
        link = ext_dir / "link.jpg"
        link.symlink_to(real)
        with pytest.raises(LocalIntakeFileError, match="Cannot open source image"):
            import_local_receipt_file(str(link), workspace, "test_sym")

    def test_directory_rejected(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        with pytest.raises(LocalIntakeFileError, match="not a regular file"):
            import_local_receipt_file(str(ext_dir), workspace, "test_dir")

    def test_idempotent_replay(self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("replay")
        source = ext_dir / "replay.jpg"
        source.write_bytes(content)

        path1, _, _, _, _ = import_local_receipt_file(str(source), workspace, "test_replay")
        path2, _, _, _, _ = import_local_receipt_file(str(source), workspace, "test_replay")
        assert path1 == path2

    def test_altered_content_fails_closed(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "altered.jpg"
        source.write_bytes(_receipt_jpeg("v1"))
        import_local_receipt_file(str(source), workspace, "test_altered")

        # Overwrite source with different content.
        source.write_bytes(_receipt_jpeg("v2"))
        with pytest.raises(LocalIntakeCopyError, match="different content hash"):
            import_local_receipt_file(str(source), workspace, "test_altered")

    def test_no_os_replace_dependency(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path], monkeypatch: Any
    ) -> None:
        """Import succeeds even if os.replace is broken (proves no dependency)."""
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("noreplace")
        source = ext_dir / "noreplace.jpg"
        source.write_bytes(content)

        def _broken_replace(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("os.replace must not be called")

        monkeypatch.setattr(os, "replace", _broken_replace)
        copy_path, data, _, _, _ = import_local_receipt_file(
            str(source), workspace, "test_noreplace"
        )
        assert data == content
        assert copy_path.read_bytes() == content

    def test_destination_exists_different_hash_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        # Pre-create destination with different content.
        att_dir = Path(workspace.attachments_path)
        dest = att_dir / "lae_test_race.jpg"
        dest.write_bytes(b"\xff\xd8\xff\xe0OLD_CONTENT")
        os.chmod(str(dest), 0o600)

        source = ext_dir / "race.jpg"
        source.write_bytes(_receipt_jpeg("race"))
        with pytest.raises(LocalIntakeCopyError, match="different content hash"):
            import_local_receipt_file(str(source), workspace, "test_race")
        # Original destination not modified.
        assert dest.read_bytes() == b"\xff\xd8\xff\xe0OLD_CONTENT"

    def test_destination_symlink_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        att_dir = Path(workspace.attachments_path)
        real_file = att_dir / "real_target.jpg"
        real_file.write_bytes(_receipt_jpeg("symdest"))
        dest = att_dir / "lae_test_symdest.jpg"
        dest.symlink_to(real_file)

        source = ext_dir / "symdest.jpg"
        source.write_bytes(_receipt_jpeg("symdest"))
        with pytest.raises(LocalIntakeCopyError, match="Cannot open|not a regular file"):
            import_local_receipt_file(str(source), workspace, "test_symdest")

    def test_source_inode_permissions_unchanged(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("inode")
        source = ext_dir / "inode.jpg"
        source.write_bytes(content)
        os.chmod(str(source), 0o755)
        orig_stat = source.stat()

        import_local_receipt_file(str(source), workspace, "test_inode")
        new_stat = source.stat()
        assert new_stat.st_ino == orig_stat.st_ino
        assert new_stat.st_mode == orig_stat.st_mode
        assert source.read_bytes() == content

    def test_short_read_fails_closed(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path], monkeypatch: Any
    ) -> None:
        """If os.read returns empty before full size, fail closed."""
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("shortread")
        source = ext_dir / "shortread.jpg"
        source.write_bytes(content)
        # First import succeeds normally.
        import_local_receipt_file(str(source), workspace, "test_shortread")

        # Now simulate short read on replay by patching os.read.
        real_read = os.read
        call_count = [0]

        def _short_read(fd: int, n: int) -> bytes:
            call_count[0] += 1
            if call_count[0] > 1:  # Let source read succeed, fail on replay.
                return b""
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", _short_read)
        with pytest.raises(LocalIntakeCopyError, match="Short read"):
            import_local_receipt_file(str(source), workspace, "test_shortread")


# ---------------------------------------------------------------------------
# Local evidence: no Telegram in lineage
# ---------------------------------------------------------------------------


class TestLocalLineage:
    def test_no_telegram_type_channel_or_table(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        content = _receipt_jpeg("lineage")
        source = ext_dir / "lineage.jpg"
        source.write_bytes(content)

        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_lineage",
        )

        # Raw intake record uses local_image / local_file.
        raw = dict(
            conn.execute(
                "SELECT source_type, source_channel FROM raw_intake_records WHERE id = ?",
                (result.raw_intake_id,),
            ).fetchone()
        )
        assert raw["source_type"] == "local_image"
        assert raw["source_channel"] == "local_file"

        # No telegram_attachment_source row.
        tg_count = conn.execute(
            "SELECT COUNT(*) FROM telegram_attachment_source WHERE attachment_id = ?",
            (result.attachment_id,),
        ).fetchone()[0]
        assert tg_count == 0

        # local_attachment_source row exists.
        local_row = conn.execute(
            "SELECT public_id, content_hash FROM local_attachment_source WHERE attachment_id = ?",
            (result.attachment_id,),
        ).fetchone()
        assert local_row is not None
        assert local_row["public_id"].startswith("lae_")
        assert local_row["content_hash"] == _sha256(content)

        # No tgae_ public ID anywhere.
        tgae_count = conn.execute(
            "SELECT COUNT(*) FROM telegram_attachment_source WHERE public_id GLOB 'tgae_*'"
        ).fetchone()[0]
        assert tgae_count == 0


# ---------------------------------------------------------------------------
# Lifecycle / authority
# ---------------------------------------------------------------------------


class TestLifecycleAuthority:
    def test_intake_stops_at_pending_confirmation(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "lifecycle.jpg"
        source.write_bytes(_receipt_jpeg("lifecycle"))

        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_lifecycle",
        )
        assert result.ingestion.parse_status == "parsed_pending_confirmation"

        # No receipt, no transaction, no settlement.
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0] == 0

    def test_no_confirm_means_no_conversion(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "noconfirm.jpg"
        source.write_bytes(_receipt_jpeg("noconfirm"))

        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_noconfirm",
        )

        from finance_core.parser_proposals.content_hash import (
            compute_effective_proposal_content_hash,
        )
        from finance_core.parser_proposals.receipt_facts_conversion import (
            convert_confirmed_receipt_proposal_to_facts,
        )

        content_hash = compute_effective_proposal_content_hash(
            conn, {"id": result.ingestion.parser_output_id}
        )
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_noconfirm",
            proposal_public_id=result.ingestion.proposal_public_id,
            expected_content_hash=content_hash,
            payer_participant_public_id="ptcp_owner",
            participants=[{"participant_public_id": "ptcp_owner", "is_included": 1}],
            authenticated_actor_id="owner",
            channel="cli",
        )
        with pytest.raises(Exception):
            convert_confirmed_receipt_proposal_to_facts(conn, cmd)
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Personal-only enforcement: conversion stage
# ---------------------------------------------------------------------------


class TestPersonalOnlyConversion:
    def test_self_only_succeeds(self) -> None:
        manifest = parse_runner_manifest(_manifest_bytes())
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_ok",
            proposal_public_id="prop_x",
            expected_content_hash="h" * 64,
            payer_participant_public_id="ptcp_owner",
            participants=[{"participant_public_id": "ptcp_owner", "is_included": 1}],
            authenticated_actor_id="owner",
            channel="local_file",
        )
        validate_personal_conversion_command(manifest, cmd)  # Should not raise.

    def test_second_participant_rejected(self) -> None:
        manifest_bytes = json.dumps(
            {
                "schema_version": "v1",
                "workspace_identity": "ws",
                "operator_actor_id": "owner",
                "participants": [
                    {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
                    {"public_id": "ptcp_alice", "display_name": "Alice", "is_self": False},
                ],
            },
            separators=(",", ":"),
        ).encode()
        manifest = parse_runner_manifest(manifest_bytes)
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_multi",
            proposal_public_id="prop_x",
            expected_content_hash="h" * 64,
            payer_participant_public_id="ptcp_owner",
            participants=[
                {"participant_public_id": "ptcp_owner", "is_included": 1},
                {"participant_public_id": "ptcp_alice", "is_included": 1},
            ],
            authenticated_actor_id="owner",
            channel="local_file",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="exactly one participant"):
            validate_personal_conversion_command(manifest, cmd)

    def test_non_self_payer_rejected(self) -> None:
        manifest_bytes = json.dumps(
            {
                "schema_version": "v1",
                "workspace_identity": "ws",
                "operator_actor_id": "owner",
                "participants": [
                    {"public_id": "ptcp_owner", "display_name": "Owner", "is_self": True},
                    {"public_id": "ptcp_alice", "display_name": "Alice", "is_self": False},
                ],
            },
            separators=(",", ":"),
        ).encode()
        manifest = parse_runner_manifest(manifest_bytes)
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_nonself",
            proposal_public_id="prop_x",
            expected_content_hash="h" * 64,
            payer_participant_public_id="ptcp_alice",
            participants=[{"participant_public_id": "ptcp_alice", "is_included": 1}],
            authenticated_actor_id="owner",
            channel="local_file",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="self participant"):
            validate_personal_conversion_command(manifest, cmd)


# ---------------------------------------------------------------------------
# OCR / CLI
# ---------------------------------------------------------------------------


class TestOcrCli:
    def test_cli_intake_requires_ocr_helper(self) -> None:
        """CLI intake without --ocr-helper is a usage error (argparse rejects)."""
        from finance_core.receipt_staging_runner.cli import main as cli_main

        with pytest.raises(SystemExit) as exc_info:
            cli_main(["intake", "--workspace", "/tmp/ws", "--manifest", "/tmp/m.json"])
        assert exc_info.value.code == 2

    def test_cli_has_no_fake_ocr(self) -> None:
        """Production CLI module contains no fake OCR implementation."""
        cli_source = Path("finance_core/receipt_staging_runner/cli.py").read_text()
        assert "FakeEngine" not in cli_source
        assert "_CliFakeEngine" not in cli_source
        assert "fake_ocr" not in cli_source
        assert "cli_fake" not in cli_source


# ---------------------------------------------------------------------------
# Failure / replay
# ---------------------------------------------------------------------------


class TestFailureReplay:
    def test_intake_replay_idempotent(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "idem.jpg"
        source.write_bytes(_receipt_jpeg("idem"))

        r1 = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_idem",
        )
        r2 = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_idem",
        )
        assert r1.raw_intake_id == r2.raw_intake_id
        assert r1.attachment_id == r2.attachment_id
        assert r2.idempotent is True
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1

    def test_ocr_failure_preserves_evidence_blocks_conversion(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "ocrfail.jpg"
        source.write_bytes(_receipt_jpeg("ocrfail"))

        fail_engine = FakeEngine(
            result=ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.ENGINE_FAILED,
                blocks=(),
                outcome_code="engine_failed",
            )
        )
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=fail_engine,
            public_id_prefix="b51b_ocrfail",
        )
        # OCR evidence is persisted.
        ext_row = conn.execute(
            "SELECT extraction_status FROM receipt_ocr_extractions WHERE public_id = ?",
            (result.extraction_public_id,),
        ).fetchone()
        assert ext_row["extraction_status"] == "engine_failed"
        # Proposal has ambiguity flags.
        assert "ocr_engine_failed" in result.ingestion.ambiguity_flags


# ---------------------------------------------------------------------------
# Migration 040 trigger enforcement
# ---------------------------------------------------------------------------


class TestMigration040Triggers:
    def test_local_attachment_source_immutable(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "immut.jpg"
        source.write_bytes(_receipt_jpeg("immut"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_immut",
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE local_attachment_source SET content_hash = ? WHERE public_id = ?",
                ("f" * 64, result.local_evidence_public_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM local_attachment_source WHERE public_id = ?",
                (result.local_evidence_public_id,),
            )

    def test_cross_table_exclusion(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "excl.jpg"
        source.write_bytes(_receipt_jpeg("excl"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_excl",
        )
        # Attempting to insert a telegram source for the same attachment fails.
        with pytest.raises(sqlite3.IntegrityError, match="local source evidence"):
            conn.execute(
                "INSERT INTO telegram_attachment_source "
                "(public_id, attachment_id, raw_intake_record_id, "
                "original_attachment_path, observed_file_size, content_hash, "
                "source_evidence_payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}', '2026-01-01')",
                (
                    "tgae_excl",
                    result.attachment_id,
                    result.raw_intake_id,
                    "/tmp/x.jpg",
                    100,
                    _sha256(_receipt_jpeg("excl")),
                ),
            )


# ---------------------------------------------------------------------------
# P1-1: raw_intake_evidence trigger + attachment immutability
# ---------------------------------------------------------------------------


class TestEvidenceAndAttachmentImmutability:
    def test_local_source_insert_creates_raw_intake_evidence(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "ev.jpg"
        source.write_bytes(_receipt_jpeg("ev"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_ev",
        )
        rows = conn.execute(
            "SELECT * FROM raw_intake_evidence WHERE evidence_reference = ?",
            (result.local_evidence_public_id,),
        ).fetchall()
        assert len(rows) == 1
        ev = rows[0]
        assert ev["evidence_type"] == "attachment"
        assert ev["raw_intake_record_id"] == result.raw_intake_id
        assert ev["attachment_id"] == result.attachment_id
        assert ev["source_file_hash"] == _sha256(_receipt_jpeg("ev"))
        assert ev["extraction_method"] == "local_file_import"

    def test_attachment_key_fields_frozen_when_local_referenced(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "freeze.jpg"
        source.write_bytes(_receipt_jpeg("freeze"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_freeze",
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET file_path = '/tmp/evil' WHERE id = ?",
                (result.attachment_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET file_hash = ? WHERE id = ?",
                ("e" * 64, result.attachment_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET original_filename = 'evil.jpg' WHERE id = ?",
                (result.attachment_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET mime_type = 'image/gif' WHERE id = ?",
                (result.attachment_id,),
            )

    def test_attachment_delete_blocked_when_local_referenced(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "del.jpg"
        source.write_bytes(_receipt_jpeg("del"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_del",
        )
        with pytest.raises(sqlite3.IntegrityError, match="Cannot delete attachment"):
            conn.execute("DELETE FROM attachments WHERE id = ?", (result.attachment_id,))

    def test_telegram_immutability_not_regressed(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        """Telegram source triggers still work independently."""
        workspace, manifest, conn, ext_dir = b51b_env
        # Insert a telegram-sourced attachment manually.
        conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at, "
            "status, created_at, updated_at) "
            "VALUES ('ri_tg1', 'telegram_image', 'telegram', 'test', '2026-01-01', "
            "'pending_parse', '2026-01-01', '2026-01-01')"
        )
        ri_id = conn.execute(
            "SELECT id FROM raw_intake_records WHERE public_id = 'ri_tg1'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO attachments "
            "(public_id, attachment_type, file_path, original_filename, "
            "mime_type, file_hash, source_channel, created_at, updated_at) "
            "VALUES ('at_tg1', 'receipt_image', '/tmp/tg.jpg', 'tg.jpg', "
            "'image/jpeg', ?, 'telegram', '2026-01-01', '2026-01-01')",
            ("a" * 64,),
        )
        att_id = conn.execute("SELECT id FROM attachments WHERE public_id = 'at_tg1'").fetchone()[0]
        conn.execute(
            "INSERT INTO telegram_attachment_source "
            "(public_id, attachment_id, raw_intake_record_id, "
            "original_attachment_path, observed_file_size, content_hash, "
            "source_evidence_payload, created_at) "
            "VALUES ('tgae_tg1', ?, ?, '/tmp/tg.jpg', 100, ?, '{}', '2026-01-01')",
            (att_id, ri_id, "a" * 64),
        )
        conn.commit()
        # Telegram trigger blocks update.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET file_path = '/tmp/evil' WHERE id = ?",
                (att_id,),
            )


# ---------------------------------------------------------------------------
# P1-2: Local-file lineage guard
# ---------------------------------------------------------------------------


class TestLocalLineageGuard:
    def test_valid_local_proposal_passes(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "guard.jpg"
        source.write_bytes(_receipt_jpeg("guard"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_guard",
        )
        lineage = require_local_runner_receipt_proposal(
            conn, result.ingestion.proposal_public_id, workspace=workspace, manifest=manifest
        )
        assert lineage["source_type"] == "local_image"
        assert lineage["source_channel"] == "local_file"

    def test_nonexistent_proposal_fails(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        with pytest.raises(LocalLineageError, match="not found"):
            require_local_runner_receipt_proposal(
                conn, "prop_nonexistent", workspace=workspace, manifest=manifest
            )

    def test_wrong_workspace_identity_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "wrongws.jpg"
        source.write_bytes(_receipt_jpeg("wrongws"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_wrongws",
        )
        # Bypass immutability trigger to simulate tampered durable state.
        conn.execute("DROP TRIGGER IF EXISTS trg_local_attachment_source_no_update")
        conn.execute("UPDATE local_attachment_source SET workspace_identity = 'evil_ws'")
        conn.commit()
        with pytest.raises(LocalLineageError, match="workspace_identity"):
            require_local_runner_receipt_proposal(
                conn,
                result.ingestion.proposal_public_id,
                workspace=workspace,
                manifest=manifest,
            )

    def test_wrong_operator_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "wrongop.jpg"
        source.write_bytes(_receipt_jpeg("wrongop"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_wrongop",
        )
        # Bypass immutability trigger to simulate tampered durable state.
        conn.execute("DROP TRIGGER IF EXISTS trg_local_attachment_source_no_update")
        conn.execute("UPDATE local_attachment_source SET operator_actor_id = 'evil'")
        conn.commit()
        with pytest.raises(LocalLineageError, match="operator_actor_id"):
            require_local_runner_receipt_proposal(
                conn,
                result.ingestion.proposal_public_id,
                workspace=workspace,
                manifest=manifest,
            )

    def test_tampered_source_public_id_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "tamper_spid.jpg"
        source.write_bytes(_receipt_jpeg("tamper_spid"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_tspid",
        )
        conn.execute(
            "UPDATE parser_outputs SET source_public_id = 'ri_evil' WHERE public_id = ?",
            (result.ingestion.proposal_public_id,),
        )
        conn.commit()
        with pytest.raises(LocalLineageError, match="source_public_id"):
            require_local_runner_receipt_proposal(
                conn,
                result.ingestion.proposal_public_id,
                workspace=workspace,
                manifest=manifest,
            )

    def test_tampered_raw_intake_source_type_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        """Tampered raw_intake_records.source_type is caught by guard."""
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "tamper_ristype.jpg"
        source.write_bytes(_receipt_jpeg("tamper_ristype"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_ristype",
        )
        # Bypass raw_intake freeze triggers to simulate tampered state.
        conn.execute("DROP TRIGGER IF EXISTS trg_raw_intake_no_detach_attachment_when_local_source")
        conn.execute(
            "UPDATE raw_intake_records SET source_type = 'telegram_image' WHERE id = ?",
            (result.raw_intake_id,),
        )
        conn.commit()
        with pytest.raises(LocalLineageError, match="source_type"):
            require_local_runner_receipt_proposal(
                conn,
                result.ingestion.proposal_public_id,
                workspace=workspace,
                manifest=manifest,
            )

    def test_tampered_raw_intake_attachment_id_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        source = ext_dir / "tamper_riatt.jpg"
        source.write_bytes(_receipt_jpeg("tamper_riatt"))
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=str(source),
            engine=_ok_engine(),
            public_id_prefix="b51b_riatt",
        )
        # Tamper raw_intake_records.attachment_id to mismatch (set to NULL).
        conn.execute("DROP TRIGGER IF EXISTS trg_raw_intake_no_detach_attachment_when_local_source")
        conn.execute(
            "UPDATE raw_intake_records SET attachment_id = NULL WHERE id = ?",
            (result.raw_intake_id,),
        )
        conn.commit()
        with pytest.raises(LocalLineageError, match="attachment_id"):
            require_local_runner_receipt_proposal(
                conn,
                result.ingestion.proposal_public_id,
                workspace=workspace,
                manifest=manifest,
            )

    def test_proposal_without_ocr_link_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        """A parser_output with no OCR proposal link is not a local lineage."""
        workspace, manifest, conn, ext_dir = b51b_env
        # Insert a bare parser_output with no OCR link.
        conn.execute(
            "INSERT INTO parser_outputs "
            "(public_id, source_type, parse_status, created_at, updated_at) "
            "VALUES ('prop_no_ocr', 'local_image', 'parsed_pending_confirmation', "
            "'2026-01-01', '2026-01-01')"
        )
        conn.commit()
        with pytest.raises(LocalLineageError, match="not a valid B5.1b local-file"):
            require_local_runner_receipt_proposal(
                conn, "prop_no_ocr", workspace=workspace, manifest=manifest
            )


# ---------------------------------------------------------------------------
# P1-3: 100% self allocation validation
# ---------------------------------------------------------------------------


class TestPersonalFactSetValidation:
    def _setup_receipt(self, conn: sqlite3.Connection) -> str:
        """Create a minimal receipt + self participant for fact-set tests.

        Assumes ptcp_owner already exists (bootstrapped by b51b_env fixture).
        """
        p_id = conn.execute(
            "SELECT id FROM participants WHERE public_id = 'ptcp_owner'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO receipts (public_id, merchant, currency, "
            "gross_amount, net_paid_amount, payer_participant_id, "
            "status, created_at, updated_at) "
            "VALUES ('rcpt_test', 'Shop', 'SGD', '10.00', '10.00', "
            "?, 'confirmed', '2026-01-01', '2026-01-01')",
            (p_id,),
        )
        rcpt_id = conn.execute("SELECT id FROM receipts WHERE public_id = 'rcpt_test'").fetchone()[
            0
        ]
        conn.execute(
            "INSERT INTO receipt_participants "
            "(public_id, receipt_id, participant_id, role, is_included, created_at, updated_at) "
            "VALUES ('rp_test', ?, ?, 'payer', 1, '2026-01-01', '2026-01-01')",
            (rcpt_id, p_id),
        )
        conn.commit()
        return "rcpt_test"

    def _make_command(
        self,
        *,
        items: list,
        allocations: list,
        adjustments: list | None = None,
        actor: str = "owner",
        actor_type: str = "human",
        channel: str = "local_file",
    ) -> Any:
        from finance_core.parser_proposals.receipt_item_allocation_facts import (
            ReceiptItemAllocationFactsCommand,
        )

        return ReceiptItemAllocationFactsCommand(
            command_public_id="cmd_test",
            receipt_public_id="rcpt_test",
            expected_conversion_command_public_id="rpfc_x",
            expected_conversion_result_hash="h" * 64,
            expected_current_fact_set="none",
            items=items,
            allocations=allocations,
            adjustments=adjustments or [],
            authenticated_actor_id=actor,
            channel=channel,
            actor_type=actor_type,
        )

    def test_correct_full_self_allocation(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[
                {"line_number": 1, "item_name": "Item", "line_amount": "10.00", "currency": "SGD"}
            ],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
        )
        validate_personal_fact_set_command(conn, cmd, manifest)  # Should not raise.

    def test_share_less_than_line_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[
                {"line_number": 1, "item_name": "Item", "line_amount": "10.00", "currency": "SGD"}
            ],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "9.99",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="exact equality"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_share_greater_than_line_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[
                {"line_number": 1, "item_name": "Item", "line_amount": "10.00", "currency": "SGD"}
            ],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.01",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="exact equality"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_currency_mismatch_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[
                {"line_number": 1, "item_name": "Item", "line_amount": "10.00", "currency": "SGD"}
            ],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "USD",
                        }
                    ],
                }
            ],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="currency"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_missing_allocation_line_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[
                {"line_number": 1, "item_name": "A", "line_amount": "5.00", "currency": "SGD"},
                {"line_number": 2, "item_name": "B", "line_amount": "5.00", "currency": "SGD"},
            ],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "5.00",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="count"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_duplicate_allocation_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[{"line_number": 1, "item_name": "A", "line_amount": "10.00", "currency": "SGD"}],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                },
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                },
            ],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="count"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_adjustments_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[{"line_number": 1, "item_name": "A", "line_amount": "10.00", "currency": "SGD"}],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
            adjustments=[{"adjustment_index": 1}],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="adjustments"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_non_self_actor_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[{"line_number": 1, "item_name": "A", "line_amount": "10.00", "currency": "SGD"}],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
            actor="evil",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="manifest operator"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_non_human_actor_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[{"line_number": 1, "item_name": "A", "line_amount": "10.00", "currency": "SGD"}],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
            actor_type="agent",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="human"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_invalid_channel_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[{"line_number": 1, "item_name": "A", "line_amount": "10.00", "currency": "SGD"}],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "manual",
                    "participants": [
                        {
                            "participant_public_id": "ptcp_owner",
                            "share_amount": "10.00",
                            "currency": "SGD",
                        }
                    ],
                }
            ],
            channel="telegram",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="channel"):
            validate_personal_fact_set_command(conn, cmd, manifest)

    def test_malformed_allocation_rejected(
        self, b51b_env: tuple[Any, Any, sqlite3.Connection, Path]
    ) -> None:
        workspace, manifest, conn, ext_dir = b51b_env
        self._setup_receipt(conn)
        cmd = self._make_command(
            items=[{"line_number": 1, "item_name": "A", "line_amount": "10.00", "currency": "SGD"}],
            allocations=["not_a_mapping"],
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="mapping"):
            validate_personal_fact_set_command(conn, cmd, manifest)


# ---------------------------------------------------------------------------
# P1-3: Conversion actor validation
# ---------------------------------------------------------------------------


class TestConversionActorValidation:
    def test_wrong_actor_rejected(self) -> None:
        manifest = parse_runner_manifest(_manifest_bytes())
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_bad_actor",
            proposal_public_id="prop_x",
            expected_content_hash="h" * 64,
            payer_participant_public_id="ptcp_owner",
            participants=[{"participant_public_id": "ptcp_owner", "is_included": 1}],
            authenticated_actor_id="evil",
            channel="local_file",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="manifest operator"):
            validate_personal_conversion_command(manifest, cmd)

    def test_non_human_actor_type_rejected(self) -> None:
        manifest = parse_runner_manifest(_manifest_bytes())
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_agent",
            proposal_public_id="prop_x",
            expected_content_hash="h" * 64,
            payer_participant_public_id="ptcp_owner",
            participants=[{"participant_public_id": "ptcp_owner", "is_included": 1}],
            authenticated_actor_id="owner",
            channel="local_file",
            actor_type="agent",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="human"):
            validate_personal_conversion_command(manifest, cmd)

    def test_invalid_channel_rejected(self) -> None:
        manifest = parse_runner_manifest(_manifest_bytes())
        cmd = ReceiptFactsConversionCommand(
            command_public_id="rpfc_tg",
            proposal_public_id="prop_x",
            expected_content_hash="h" * 64,
            payer_participant_public_id="ptcp_owner",
            participants=[{"participant_public_id": "ptcp_owner", "is_included": 1}],
            authenticated_actor_id="owner",
            channel="telegram",
        )
        with pytest.raises(LocalIntakePersonalOnlyError, match="channel"):
            validate_personal_conversion_command(manifest, cmd)


# ---------------------------------------------------------------------------
# P2-2: CLI does not leak full source path
# ---------------------------------------------------------------------------


class TestCliPathLeakage:
    def test_intake_output_has_no_full_source_path(self) -> None:
        """The CLI intake payload must not contain the full external source path."""
        cli_source = Path("finance_core/receipt_staging_runner/cli.py").read_text()
        # The old key must be gone.
        assert '"source_image_path"' not in cli_source
        # The new key must be present.
        assert '"source_basename"' in cli_source
