"""Focused migration, engine, attachment, persistence, and replay tests for PR #218."""

from __future__ import annotations

import hashlib
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

import finance_core.intake.receipt_ocr_evidence as ocr_module
from finance_core.intake.attachment_evidence import persist_attachment_evidence
from finance_core.intake.receipt_ocr_evidence import (
    ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES,
    InvalidOcrConfigurationError,
    MalformedOcrOutputError,
    OcrAttachmentIntegrityConflictError,
    OcrAttachmentNotFoundError,
    OcrCallerOwnedTransactionError,
    OcrDeadlineExceededError,
    OcrEngineLaunchError,
    OcrIdempotencyConflictError,
    OcrPersistenceConflictError,
    OcrResourceLimitExceededError,
    OcrStagingDatabaseRejectedError,
    OcrUnexpectedPersistenceError,
    OcrUnsupportedPlatformError,
    ReceiptOcrBlock,
    ReceiptOcrEngineIdentity,
    ReceiptOcrEngineResult,
    ReceiptOcrError,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    ReceiptOcrSource,
    TesseractTsvOcrEngine,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    migration_file_checksum,
    migration_ledger_rows,
    verify_migration_history,
)
from finance_core.staging_guard import create_staging_database

JPEG = b"\xff\xd8\xff" + b"receipt-jpeg-evidence"
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-png-evidence"
PDF = b"%PDF-1.7\nreceipt-pdf-evidence"


def _hash(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _insert_raw_intake(conn: sqlite3.Connection, suffix: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, source_channel, raw_input, received_at
        ) VALUES (?, 'telegram_text', 'telegram', 'receipt image', ?)
        """,
        (f"raw_ocr_{suffix}", "2026-07-19T15:00:00+00:00"),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _persist_attachment(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    suffix: str,
    content: bytes = JPEG,
    mime_type: str = "image/jpeg",
) -> tuple[int, Path]:
    path = (tmp_path / f"{suffix}.bin").resolve()
    path.write_bytes(content)
    path.chmod(0o400)
    raw_intake_id = _insert_raw_intake(conn, suffix)
    result = persist_attachment_evidence(
        conn,
        path,
        public_id=f"tgae_ocr_{suffix}",
        raw_intake_id=raw_intake_id,
        telegram_file_id=f"file_{suffix}",
        telegram_file_unique_id=f"unique_{suffix}",
        original_filename=f"{suffix}.jpg",
        declared_mime_type=mime_type,
        expected_file_size=len(content),
        expected_content_hash=_hash(content),
    )
    return int(result["attachment_id"]), path


def _block(sequence: int = 0, text: str = "TOTAL") -> ReceiptOcrBlock:
    return ReceiptOcrBlock(
        sequence_index=sequence,
        page_index=0,
        engine_block_index=0,
        engine_paragraph_index=0,
        engine_line_index=0,
        engine_word_index=sequence,
        text=text,
        left=10 + sequence * 40,
        top=20,
        width=30,
        height=10,
        page_width=800,
        page_height=1200,
        confidence_scaled=9750,
    )


class FakeEngine:
    def __init__(
        self,
        *,
        name: str = "fake_ocr",
        version: str = "1.0",
        binary: str = "fake-binary-v1",
        configuration: str = "fake-config-v1",
        result: ReceiptOcrEngineResult | None = None,
        callback: Callable[[ReceiptOcrSource], None] | None = None,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self._identity = ReceiptOcrEngineIdentity(
            name=name,
            version=version,
            binary_sha256=_hash(binary),
            configuration_hash=_hash(configuration),
        )
        self.result = result or ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks=(_block(0, "TOTAL"), _block(1, "12.34")),
            outcome_code="ok",
        )
        self.callback = callback
        self.barrier = barrier
        self.calls = 0

    @property
    def identity(self) -> ReceiptOcrEngineIdentity:
        return self._identity

    def extract(
        self,
        source: ReceiptOcrSource,
        *,
        limits: ReceiptOcrLimits,
        deadline: float,
    ) -> ReceiptOcrEngineResult:
        assert limits.max_attachment_bytes > 0
        assert deadline > time.monotonic()
        assert os.path.isabs(source.attachment_path)
        self.calls += 1
        if self.callback is not None:
            self.callback(source)
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        return self.result


def _call(
    conn: sqlite3.Connection,
    attachment_id: int,
    engine: FakeEngine | TesseractTsvOcrEngine,
    *,
    public_id: str = "rocr_test_001",
    limits: ReceiptOcrLimits = ReceiptOcrLimits(),
):
    return extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id=public_id,
        attachment_id=attachment_id,
        engine=engine,
        limits=limits,
    )


def _write_fake_tesseract(tmp_path: Path, body: str, *, name: str = "tesseract-fake") -> Path:
    path = (tmp_path / name).resolve()
    script = f"#!{sys.executable}\n" + body
    path.write_text(script, encoding="utf-8")
    path.chmod(0o500)
    return path


def _tsv(*rows: str) -> str:
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
        "width\theight\tconf\ttext"
    )
    return "\n".join((header, *rows)) + "\n"


def _require_concrete_platform() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("The v1 concrete OCR adapter requires Linux RLIMIT_AS controls.")


def _fake_script_for_output(output_literal: str, *, stderr_literal: str = "b''") -> str:
    return f"""
import os
import sys

if sys.argv[1:] == ['--version']:
    print('tesseract 5.3.4')
    raise SystemExit(0)
if os.environ.get('OCR_PARENT_SECRET') is not None:
    raise SystemExit(9)
if sys.argv[2:] != ['stdout', '-l', 'eng', '--dpi', '300', 'tsv']:
    raise SystemExit(8)
with open(sys.argv[1], 'rb') as source:
    if not source.read(3):
        raise SystemExit(7)
os.write(1, {output_literal})
os.write(2, {stderr_literal})
"""


def _insert_attachment_metadata(
    conn: sqlite3.Connection,
    *,
    suffix: str,
    path: Path,
    persisted_size: int,
    content_hash: str,
    mime_type: str,
) -> int:
    raw_id = _insert_raw_intake(conn, suffix)
    cursor = conn.execute(
        """
        INSERT INTO attachments (
            public_id, attachment_type, file_path, mime_type, file_hash, source_channel
        ) VALUES (?, 'telegram_attachment', ?, ?, ?, 'telegram')
        """,
        (f"at_ocr_{suffix}", str(path), mime_type, content_hash),
    )
    assert cursor.lastrowid is not None
    attachment_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO telegram_attachment_source (
            public_id, attachment_id, raw_intake_record_id,
            original_attachment_path, observed_file_size, content_hash,
            source_evidence_payload
        ) VALUES (?, ?, ?, ?, ?, ?, '{}')
        """,
        (
            f"tgae_ocr_{suffix}",
            attachment_id,
            raw_id,
            str(path),
            persisted_size,
            content_hash,
        ),
    )
    conn.commit()
    return attachment_id


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def _inject_persisted_outcome(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    attachment_id: int,
    engine: FakeEngine,
    limits: ReceiptOcrLimits,
    status: ReceiptOcrExtractionStatus,
    outcome_code: str,
    blocks: tuple[ReceiptOcrBlock, ...] = (),
) -> None:
    attachment = ocr_module._load_attachment_record(conn, attachment_id)
    identity = ocr_module._validate_engine_identity(engine.identity, limits=limits)
    fingerprint = ocr_module._extraction_fingerprint(attachment, identity, limits)
    normalized = ocr_module._normalized_outcome(
        status,
        blocks,
        outcome_code,
        limits=limits,
    )
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        cursor = conn.execute(
            """
            INSERT INTO receipt_ocr_extractions (
                public_id, attachment_id, source_attachment_hash,
                source_attachment_size, source_mime_type, engine_name,
                engine_version, engine_binary_sha256, engine_configuration_hash,
                extraction_fingerprint, extraction_status, block_count,
                total_normalized_text_length, normalized_result_hash,
                sanitized_outcome_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                attachment.attachment_id,
                attachment.content_hash,
                attachment.size_bytes,
                attachment.mime_type,
                identity.name,
                identity.version,
                identity.binary_sha256,
                identity.configuration_hash,
                fingerprint,
                normalized.status.value,
                len(normalized.blocks),
                normalized.total_text_length,
                normalized.result_hash,
                normalized.outcome_code,
                "2026-07-20T00:00:00+00:00",
            ),
        )
        assert cursor.lastrowid is not None
        for block in normalized.blocks:
            conn.execute(
                """
                INSERT INTO receipt_ocr_blocks (
                    extraction_id, sequence_index, page_index,
                    engine_block_index, engine_paragraph_index,
                    engine_line_index, engine_word_index, normalized_text,
                    coordinate_left, coordinate_top, coordinate_width,
                    coordinate_height, page_width, page_height, confidence_scaled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    block.sequence_index,
                    block.page_index,
                    block.engine_block_index,
                    block.engine_paragraph_index,
                    block.engine_line_index,
                    block.engine_word_index,
                    block.text,
                    block.left,
                    block.top,
                    block.width,
                    block.height,
                    block.page_width,
                    block.page_height,
                    block.confidence_scaled,
                ),
            )
        conn.commit()
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")


def test_migration_032_is_registered_sql_only_and_replays_cleanly() -> None:
    migration = next(
        path for path in TEMP_DB_MIGRATION_PATHS if path.name == "032_receipt_ocr_evidence.sql"
    )
    assert migration_file_checksum(migration) == _hash(migration.read_bytes())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        before = migration_ledger_rows(conn)
        assert len(before) == len(TEMP_DB_MIGRATION_PATHS)
        assert "032" in {row["migration_id"] for row in before}
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert migration_ledger_rows(conn) == before
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_upgrade_from_031_applies_only_032() -> None:
    through_031 = TEMP_DB_MIGRATION_PATHS[:31]
    through_032 = TEMP_DB_MIGRATION_PATHS[:32]
    assert through_031[-1].name == "031_telegram_attachment_evidence.sql"
    assert through_032[-1].name == "032_receipt_ocr_evidence.sql"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migration_paths(conn, through_031)
        old_rows = migration_ledger_rows(conn)
        apply_migration_paths(conn, through_032)
        rows = migration_ledger_rows(conn)
        assert rows[:-1] == old_rows
        assert rows[-1]["migration_id"] == "032"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_attachment_bytes", True),
        ("max_attachment_bytes", 0),
        ("max_attachment_bytes", 100_000_001),
        ("total_timeout_seconds", float("inf")),
        ("termination_grace_seconds", -1),
        ("max_block_count", 100_001),
        ("max_public_id_length", 201),
    ],
)
def test_limits_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(InvalidOcrConfigurationError):
        ReceiptOcrLimits(**{field: value})  # type: ignore[arg-type]


def test_public_boundary_rejects_bad_ids_engine_and_attachment_id(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    engine = FakeEngine()
    with pytest.raises(InvalidOcrConfigurationError):
        extract_and_persist_receipt_ocr_evidence(
            migrated_temp_db_connection,
            public_id="bad",
            attachment_id=1,
            engine=engine,
        )
    with pytest.raises(InvalidOcrConfigurationError):
        extract_and_persist_receipt_ocr_evidence(
            migrated_temp_db_connection,
            public_id="rocr_valid",
            attachment_id=True,
            engine=engine,
        )
    with pytest.raises(InvalidOcrConfigurationError):
        extract_and_persist_receipt_ocr_evidence(
            migrated_temp_db_connection,
            public_id="rocr_valid",
            attachment_id=1,
            engine=object(),  # type: ignore[arg-type]
        )


def test_staging_guard_and_caller_transaction_are_public_errors(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    arbitrary = sqlite3.connect(tmp_path / "unauthorised.sqlite")
    try:
        with pytest.raises(OcrStagingDatabaseRejectedError):
            extract_and_persist_receipt_ocr_evidence(
                arbitrary,
                public_id="rocr_staging",
                attachment_id=1,
                engine=FakeEngine(),
            )
    finally:
        arbitrary.close()
    migrated_temp_db_connection.execute("BEGIN")
    try:
        with pytest.raises(OcrCallerOwnedTransactionError):
            extract_and_persist_receipt_ocr_evidence(
                migrated_temp_db_connection,
                public_id="rocr_tx",
                attachment_id=1,
                engine=FakeEngine(),
            )
    finally:
        migrated_temp_db_connection.rollback()


def test_missing_attachment_and_missing_file_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    with pytest.raises(OcrAttachmentNotFoundError):
        _call(migrated_temp_db_connection, 999_999, FakeEngine())
    attachment_id, path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="missing_file"
    )
    path.chmod(0o600)
    path.unlink()
    with pytest.raises(OcrAttachmentIntegrityConflictError):
        _call(migrated_temp_db_connection, attachment_id, FakeEngine())


@pytest.mark.parametrize(
    ("mutation", "suffix"),
    [
        (lambda path: path.chmod(0o600), "unsafe_mode"),
        (lambda path: path.write_bytes(JPEG + b"changed"), "wrong_size_hash"),
        (lambda path: path.write_bytes(PNG[: len(JPEG)]), "wrong_signature"),
    ],
)
def test_attachment_mode_size_hash_and_signature_conflicts(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    mutation: Callable[[Path], object],
    suffix: str,
) -> None:
    attachment_id, path = _persist_attachment(migrated_temp_db_connection, tmp_path, suffix=suffix)
    path.chmod(0o600)
    mutation(path)
    path.chmod(0o400 if suffix != "unsafe_mode" else 0o600)
    with pytest.raises(OcrAttachmentIntegrityConflictError):
        _call(migrated_temp_db_connection, attachment_id, FakeEngine())


def test_symlink_attachment_path_is_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    target = (tmp_path / "target.jpg").resolve()
    target.write_bytes(JPEG)
    target.chmod(0o400)
    link = (tmp_path / "receipt-link.jpg").resolve()
    link.symlink_to(target)
    raw_id = _insert_raw_intake(migrated_temp_db_connection, "symlink")
    attachment = migrated_temp_db_connection.execute(
        """
        INSERT INTO attachments (
            public_id, attachment_type, file_path, mime_type, file_hash, source_channel
        ) VALUES ('at_symlink_ocr', 'telegram_attachment', ?, 'image/jpeg', ?, 'telegram')
        """,
        (str(link), _hash(JPEG)),
    )
    assert attachment.lastrowid is not None
    migrated_temp_db_connection.execute(
        """
        INSERT INTO telegram_attachment_source (
            public_id, attachment_id, raw_intake_record_id,
            original_attachment_path, observed_file_size, content_hash,
            source_evidence_payload
        ) VALUES ('tgae_ocr_symlink', ?, ?, ?, ?, ?, '{}')
        """,
        (attachment.lastrowid, raw_id, str(link), len(JPEG), _hash(JPEG)),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(OcrAttachmentIntegrityConflictError):
        _call(migrated_temp_db_connection, int(attachment.lastrowid), FakeEngine())


def test_success_persists_normalized_blocks_and_replays_without_engine(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="success"
    )
    before = path.read_bytes()
    engine = FakeEngine()
    first = _call(migrated_temp_db_connection, attachment_id, engine)
    second = _call(migrated_temp_db_connection, attachment_id, engine)
    assert first.status == ReceiptOcrExtractionStatus.SUCCEEDED
    assert first.block_count == 2
    assert first.total_normalized_text_length == len("TOTAL12.34")
    assert first.persistence_idempotent is False
    assert second == replace(first, persistence_idempotent=True)
    assert engine.calls == 1
    assert path.read_bytes() == before
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 1
    )
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0]
        == 2
    )


def test_pdf_is_persisted_as_explicit_unsupported_without_engine_call(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix="pdf_unsupported",
        content=PDF,
        mime_type="application/pdf",
    )
    engine = FakeEngine()
    first = _call(migrated_temp_db_connection, attachment_id, engine)
    second = _call(migrated_temp_db_connection, attachment_id, engine)
    assert first.status == ReceiptOcrExtractionStatus.UNSUPPORTED_INPUT
    assert first.outcome_code == "pdf_unsupported"
    assert first.block_count == 0
    assert second == replace(first, persistence_idempotent=True)
    assert engine.calls == 0


def test_oversized_attachment_is_persisted_as_resource_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="oversize"
    )
    engine = FakeEngine()
    first = _call(
        migrated_temp_db_connection,
        attachment_id,
        engine,
        limits=ReceiptOcrLimits(max_attachment_bytes=1),
    )
    second = _call(
        migrated_temp_db_connection,
        attachment_id,
        engine,
        limits=ReceiptOcrLimits(max_attachment_bytes=1),
    )
    assert first.status == ReceiptOcrExtractionStatus.RESOURCE_REJECTED
    assert first.outcome_code == "attachment_size_limit"
    assert second == replace(first, persistence_idempotent=True)
    assert engine.calls == 0


def test_persisted_size_above_absolute_ceiling_fails_before_open_or_read(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (tmp_path / "persisted-over-absolute.jpg").resolve()
    path.write_bytes(JPEG)
    path.chmod(0o400)
    attachment_id = _insert_attachment_metadata(
        migrated_temp_db_connection,
        suffix="persisted_over_absolute",
        path=path,
        persisted_size=ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES + 1,
        content_hash=_hash(JPEG),
        mime_type="image/jpeg",
    )
    reads = 0
    opens = 0
    real_read = ocr_module.os.read
    real_open = ocr_module.os.open

    def count_read(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        return real_read(fd, size)

    def count_open(*args: object, **kwargs: object) -> int:
        nonlocal opens
        opens += 1
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ocr_module.os, "read", count_read)
    monkeypatch.setattr(ocr_module.os, "open", count_open)
    engine = FakeEngine()
    with pytest.raises(OcrResourceLimitExceededError):
        _call(migrated_temp_db_connection, attachment_id, engine)
    assert opens == 0
    assert reads == 0
    assert engine.calls == 0
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


def test_actual_sparse_size_above_absolute_ceiling_fails_before_read(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (tmp_path / "actual-over-absolute.jpg").resolve()
    path.write_bytes(JPEG)
    with path.open("r+b") as destination:
        destination.truncate(ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES + 1)
    path.chmod(0o400)
    attachment_id = _insert_attachment_metadata(
        migrated_temp_db_connection,
        suffix="actual_over_absolute",
        path=path,
        persisted_size=len(JPEG),
        content_hash=_hash(JPEG),
        mime_type="image/jpeg",
    )
    reads = 0
    real_read = ocr_module.os.read

    def count_read(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        return real_read(fd, size)

    monkeypatch.setattr(ocr_module.os, "read", count_read)
    engine = FakeEngine()
    with pytest.raises(OcrResourceLimitExceededError):
        _call(migrated_temp_db_connection, attachment_id, engine)
    assert reads == 0
    assert engine.calls == 0
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0]
        == 0
    )


def test_attachment_at_exact_configured_limit_remains_eligible(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix="exact_configured_limit",
    )
    engine = FakeEngine()
    result = _call(
        migrated_temp_db_connection,
        attachment_id,
        engine,
        limits=ReceiptOcrLimits(max_attachment_bytes=len(JPEG)),
    )
    assert result.status == ReceiptOcrExtractionStatus.SUCCEEDED
    assert engine.calls == 1


def test_attachment_at_exact_absolute_ceiling_is_fully_bounded_and_eligible(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    path = (tmp_path / "exact-absolute.jpg").resolve()
    with path.open("wb") as destination:
        destination.write(JPEG)
        destination.truncate(ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES)
    content_hash = _hash_path(path)
    path.chmod(0o400)
    attachment_id = _insert_attachment_metadata(
        migrated_temp_db_connection,
        suffix="exact_absolute",
        path=path,
        persisted_size=ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES,
        content_hash=content_hash,
        mime_type="image/jpeg",
    )
    engine = FakeEngine()
    result = _call(
        migrated_temp_db_connection,
        attachment_id,
        engine,
        limits=ReceiptOcrLimits(max_attachment_bytes=ABSOLUTE_ATTACHMENT_VERIFICATION_BYTES),
    )
    assert result.status == ReceiptOcrExtractionStatus.SUCCEEDED
    assert engine.calls == 1


def test_engine_failed_and_no_text_are_handled_append_only_outcomes(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    failed_attachment, _ = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="engine_failed"
    )
    no_text_attachment, _ = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="no_text", content=PNG, mime_type="image/png"
    )
    failed = FakeEngine(
        result=ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.ENGINE_FAILED,
            blocks=(),
            outcome_code="engine_exit_nonzero",
        )
    )
    no_text = FakeEngine(
        configuration="no-text-config",
        result=ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.NO_TEXT,
            blocks=(),
            outcome_code="no_text",
        ),
    )
    assert (
        _call(migrated_temp_db_connection, failed_attachment, failed).status.value
        == "engine_failed"
    )
    assert (
        _call(
            migrated_temp_db_connection,
            no_text_attachment,
            no_text,
            public_id="rocr_test_no_text",
        ).status.value
        == "no_text"
    )


@pytest.mark.parametrize(
    (
        "content",
        "mime_type",
        "limits",
        "status",
        "outcome_code",
        "blocks",
    ),
    [
        (
            PDF,
            "application/pdf",
            ReceiptOcrLimits(),
            ReceiptOcrExtractionStatus.SUCCEEDED,
            "ok",
            (_block(),),
        ),
        (
            PDF,
            "application/pdf",
            ReceiptOcrLimits(),
            ReceiptOcrExtractionStatus.NO_TEXT,
            "no_text",
            (),
        ),
        (
            PDF,
            "application/pdf",
            ReceiptOcrLimits(),
            ReceiptOcrExtractionStatus.RESOURCE_REJECTED,
            "attachment_size_limit",
            (),
        ),
        (
            JPEG,
            "image/jpeg",
            ReceiptOcrLimits(max_attachment_bytes=1),
            ReceiptOcrExtractionStatus.SUCCEEDED,
            "ok",
            (_block(),),
        ),
        (
            JPEG,
            "image/jpeg",
            ReceiptOcrLimits(max_attachment_bytes=1),
            ReceiptOcrExtractionStatus.UNSUPPORTED_INPUT,
            "pdf_unsupported",
            (),
        ),
        (
            JPEG,
            "image/jpeg",
            ReceiptOcrLimits(),
            ReceiptOcrExtractionStatus.UNSUPPORTED_INPUT,
            "pdf_unsupported",
            (),
        ),
        (
            PNG,
            "image/png",
            ReceiptOcrLimits(),
            ReceiptOcrExtractionStatus.RESOURCE_REJECTED,
            "attachment_size_limit",
            (),
        ),
    ],
    ids=(
        "pdf-succeeded",
        "pdf-no-text",
        "pdf-resource-rejected",
        "oversized-succeeded",
        "oversized-unsupported",
        "jpeg-unsupported",
        "png-resource-rejected",
    ),
)
def test_semantically_impossible_persisted_replay_fails_without_engine_or_repair(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    content: bytes,
    mime_type: str,
    limits: ReceiptOcrLimits,
    status: ReceiptOcrExtractionStatus,
    outcome_code: str,
    blocks: tuple[ReceiptOcrBlock, ...],
) -> None:
    suffix = _hash(repr((mime_type, limits, status, outcome_code)))[:12]
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"corrupt_replay_{suffix}",
        content=content,
        mime_type=mime_type,
    )
    public_id = f"rocr_corrupt_{suffix}"
    engine = FakeEngine()
    _inject_persisted_outcome(
        migrated_temp_db_connection,
        public_id=public_id,
        attachment_id=attachment_id,
        engine=engine,
        limits=limits,
        status=status,
        outcome_code=outcome_code,
        blocks=blocks,
    )
    before = (
        tuple(
            tuple(row)
            for row in migrated_temp_db_connection.execute(
                "SELECT * FROM receipt_ocr_extractions ORDER BY id"
            ).fetchall()
        ),
        tuple(
            tuple(row)
            for row in migrated_temp_db_connection.execute(
                "SELECT * FROM receipt_ocr_blocks ORDER BY id"
            ).fetchall()
        ),
    )
    with pytest.raises(OcrPersistenceConflictError):
        _call(
            migrated_temp_db_connection,
            attachment_id,
            engine,
            public_id=public_id,
            limits=limits,
        )
    after = (
        tuple(
            tuple(row)
            for row in migrated_temp_db_connection.execute(
                "SELECT * FROM receipt_ocr_extractions ORDER BY id"
            ).fetchall()
        ),
        tuple(
            tuple(row)
            for row in migrated_temp_db_connection.execute(
                "SELECT * FROM receipt_ocr_blocks ORDER BY id"
            ).fetchall()
        ),
    )
    assert after == before
    assert engine.calls == 0
    assert migrated_temp_db_connection.in_transaction is False


def test_public_id_and_duplicate_fingerprint_conflicts_happen_before_ocr(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    first_attachment, _ = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="conflict_one"
    )
    second_attachment, _ = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix="conflict_two",
        content=PNG,
        mime_type="image/png",
    )
    engine = FakeEngine()
    _call(migrated_temp_db_connection, first_attachment, engine)
    conflicting = FakeEngine(configuration="different")
    with pytest.raises(OcrIdempotencyConflictError):
        _call(migrated_temp_db_connection, first_attachment, conflicting)
    assert conflicting.calls == 0
    with pytest.raises(OcrIdempotencyConflictError):
        _call(
            migrated_temp_db_connection,
            first_attachment,
            engine,
            public_id="rocr_different_public",
        )
    assert engine.calls == 1
    with pytest.raises(OcrIdempotencyConflictError):
        _call(migrated_temp_db_connection, second_attachment, engine)


def test_attachment_mutation_during_ocr_fails_without_persistence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="mutated_during_ocr"
    )

    def mutate(_source: ReceiptOcrSource) -> None:
        path.chmod(0o600)
        path.write_bytes(JPEG + b"mutated")
        path.chmod(0o400)

    with pytest.raises(OcrAttachmentIntegrityConflictError):
        _call(migrated_temp_db_connection, attachment_id, FakeEngine(callback=mutate))
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )
    assert migrated_temp_db_connection.in_transaction is False


def test_attachment_path_inode_replacement_during_ocr_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="inode_replaced_during_ocr"
    )
    displaced = path.with_name(f"{path.name}.displaced")

    def replace_path(_source: ReceiptOcrSource) -> None:
        path.rename(displaced)
        path.write_bytes(JPEG)
        path.chmod(0o400)

    with pytest.raises(OcrAttachmentIntegrityConflictError):
        _call(migrated_temp_db_connection, attachment_id, FakeEngine(callback=replace_path))
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "stage",
    [
        "before_extraction_insert",
        "after_extraction_insert",
        "during_first_block_insert",
        "during_later_block_insert",
        "after_all_block_inserts",
        "before_persisted_verification",
        "before_commit",
    ],
)
def test_failure_injection_rolls_back_every_write_boundary(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    attachment_id, path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix=f"rollback_{stage}"
    )
    before = path.read_bytes()

    def inject(observed: str) -> None:
        if observed == stage:
            raise sqlite3.OperationalError("injected OCR write failure")

    monkeypatch.setattr(ocr_module, "_failure_injection_hook", inject)
    with pytest.raises(OcrUnexpectedPersistenceError):
        _call(migrated_temp_db_connection, attachment_id, FakeEngine())
    assert migrated_temp_db_connection.in_transaction is False
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0]
        == 0
    )
    assert path.read_bytes() == before
    assert migrated_temp_db_connection.execute("SELECT 1").fetchone()[0] == 1


def test_interrupted_persistence_restarts_without_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = (tmp_path / "ocr-crash-restart.sqlite").resolve()
    first = create_staging_database(db_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    attachment_id, path = _persist_attachment(first, tmp_path, suffix="crash_restart")

    def interrupt(stage: str) -> None:
        if stage == "before_commit":
            raise SystemExit("simulated process interruption")

    monkeypatch.setattr(ocr_module, "_failure_injection_hook", interrupt)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        _call(first, attachment_id, FakeEngine())
    assert first.in_transaction is False
    assert first.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 0
    first.close()

    monkeypatch.setattr(ocr_module, "_failure_injection_hook", None)
    restarted = sqlite3.connect(str(db_path), timeout=5)
    restarted.row_factory = sqlite3.Row
    restarted.execute("PRAGMA foreign_keys = ON")
    try:
        result = _call(restarted, attachment_id, FakeEngine())
        assert result.persistence_idempotent is False
        assert restarted.execute("SELECT COUNT(*) FROM receipt_ocr_extractions").fetchone()[0] == 1
        assert restarted.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0] == 2
        assert restarted.in_transaction is False
        assert path.read_bytes() == JPEG
    finally:
        restarted.close()


def test_schema_is_append_only_and_protects_ocr_referenced_attachment(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="append_only"
    )
    _call(migrated_temp_db_connection, attachment_id, FakeEngine())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated_temp_db_connection.execute(
            "UPDATE receipt_ocr_extractions SET sanitized_outcome_code = 'changed'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated_temp_db_connection.execute("DELETE FROM receipt_ocr_blocks")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        migrated_temp_db_connection.execute(
            "UPDATE attachments SET file_hash = ? WHERE id = ?", (_hash("changed"), attachment_id)
        )
    migrated_temp_db_connection.rollback()


def test_migration_unique_and_check_constraints_reject_invalid_rows(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="schema_constraints"
    )
    result = _call(migrated_temp_db_connection, attachment_id, FakeEngine())
    row = migrated_temp_db_connection.execute(
        "SELECT * FROM receipt_ocr_extractions WHERE public_id = ?", (result.public_id,)
    ).fetchone()
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        migrated_temp_db_connection.execute(
            """
            INSERT INTO receipt_ocr_blocks (
                extraction_id, sequence_index, page_index, normalized_text,
                coordinate_left, coordinate_top, coordinate_width,
                coordinate_height, page_width, page_height, confidence_scaled
            ) VALUES (?, 0, 0, 'duplicate', 0, 0, 1, 1, 10, 10, 5000)
            """,
            (row["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_temp_db_connection.execute(
            """
            INSERT INTO receipt_ocr_blocks (
                extraction_id, sequence_index, page_index, normalized_text,
                coordinate_left, coordinate_top, coordinate_width,
                coordinate_height, page_width, page_height, confidence_scaled
            ) VALUES (?, 99, 0, 'bad confidence', 0, 0, 1, 1, 10, 10, 10001)
            """,
            (row["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_temp_db_connection.execute(
            """
            INSERT INTO receipt_ocr_extractions (
                public_id, attachment_id, source_attachment_hash,
                source_attachment_size, source_mime_type, engine_name,
                engine_version, engine_binary_sha256, engine_configuration_hash,
                extraction_fingerprint, extraction_status, block_count,
                total_normalized_text_length, normalized_result_hash,
                sanitized_outcome_code
            ) VALUES (
                'rocr_bad_hash', ?, 'NOT_A_HASH', 1, 'image/jpeg', 'fake', '1',
                ?, ?, ?, 'no_text', 0, 0, ?, 'no_text'
            )
            """,
            (
                attachment_id,
                _hash("binary"),
                _hash("config"),
                _hash("fingerprint"),
                _hash("result"),
            ),
        )
    migrated_temp_db_connection.rollback()


@pytest.mark.parametrize(
    ("mime_type", "status", "outcome_code"),
    [
        ("image/jpeg", "unsupported_input", "pdf_unsupported"),
        ("application/pdf", "unsupported_input", "wrong_code"),
        ("image/png", "resource_rejected", "wrong_code"),
        ("image/png", "no_text", "attachment_size_limit"),
    ],
)
def test_migration_032_rejects_invalid_service_status_outcome_relationships(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    mime_type: str,
    status: str,
    outcome_code: str,
) -> None:
    content = PDF if mime_type == "application/pdf" else PNG if mime_type == "image/png" else JPEG
    suffix = _hash(repr((mime_type, status, outcome_code)))[:10]
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"schema_status_{suffix}",
        content=content,
        mime_type=mime_type,
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_temp_db_connection.execute(
            """
            INSERT INTO receipt_ocr_extractions (
                public_id, attachment_id, source_attachment_hash,
                source_attachment_size, source_mime_type, engine_name,
                engine_version, engine_binary_sha256, engine_configuration_hash,
                extraction_fingerprint, extraction_status, block_count,
                total_normalized_text_length, normalized_result_hash,
                sanitized_outcome_code
            ) VALUES (?, ?, ?, ?, ?, 'fake', '1', ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                f"rocr_schema_{suffix}",
                attachment_id,
                _hash(content),
                len(content),
                mime_type,
                _hash("binary"),
                _hash("config"),
                _hash(f"fingerprint-{suffix}"),
                status,
                _hash(f"result-{suffix}"),
                outcome_code,
            ),
        )
    migrated_temp_db_connection.rollback()


@pytest.mark.parametrize(
    "bad_block",
    [
        _block(1),
        replace(_block(), left=-1),
        replace(_block(), left=799, width=2),
        replace(_block(), confidence_scaled=10_001),
        replace(_block(), text="bad\x00text"),
        replace(_block(), page_width=100_001),
    ],
)
def test_malformed_blocks_fail_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    bad_block: ReceiptOcrBlock,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"bad_block_{abs(hash(repr(bad_block)))}",
    )
    engine = FakeEngine(
        result=ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks=(bad_block,),
            outcome_code="ok",
        )
    )
    with pytest.raises((MalformedOcrOutputError, OcrResourceLimitExceededError)):
        _call(migrated_temp_db_connection, attachment_id, engine)
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("blocks", "limits"),
    [
        ((_block(0), _block(1)), ReceiptOcrLimits(max_block_count=1)),
        ((_block(0, "TOO_LONG"),), ReceiptOcrLimits(max_text_characters_per_block=3)),
        (
            (_block(0, "ABCD"), _block(1, "EFGH")),
            ReceiptOcrLimits(max_total_normalized_text_characters=5),
        ),
    ],
)
def test_block_count_and_text_resource_limits_fail_before_persistence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    blocks: tuple[ReceiptOcrBlock, ...],
    limits: ReceiptOcrLimits,
) -> None:
    suffix = _hash(repr((blocks, limits)))[:10]
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix=f"resource_{suffix}"
    )
    engine = FakeEngine(
        result=ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks=blocks,
            outcome_code="ok",
        )
    )
    with pytest.raises(OcrResourceLimitExceededError):
        _call(migrated_temp_db_connection, attachment_id, engine, limits=limits)
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("rows", "limits", "error"),
    [
        (
            (
                "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
                "1\t2\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
            ),
            ReceiptOcrLimits(max_page_count=1),
            OcrResourceLimitExceededError,
        ),
        (
            ("1\t1\t0\t0\t0\t0\t0\t0\t101\t100\t-1\t",),
            ReceiptOcrLimits(max_image_width=100),
            OcrResourceLimitExceededError,
        ),
        (
            ("1\t1\t0\t0\t0\t0\t0\t0\t100\t101\t-1\t",),
            ReceiptOcrLimits(max_image_height=100),
            OcrResourceLimitExceededError,
        ),
        (
            (
                "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
                "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
            ),
            ReceiptOcrLimits(),
            MalformedOcrOutputError,
        ),
    ],
    ids=("too-many-empty-pages", "empty-page-width", "empty-page-height", "duplicate-page"),
)
def test_tsv_page_limits_apply_without_word_rows(
    rows: tuple[str, ...],
    limits: ReceiptOcrLimits,
    error: type[ReceiptOcrError],
) -> None:
    with pytest.raises(error):
        ocr_module._parse_tesseract_tsv(_tsv(*rows).encode("utf-8"), limits=limits)


def test_tsv_one_bounded_empty_page_is_a_valid_no_text_result() -> None:
    blocks = ocr_module._parse_tesseract_tsv(
        _tsv("1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t").encode("utf-8"),
        limits=ReceiptOcrLimits(),
    )
    assert blocks == ()


@pytest.mark.parametrize("content,mime", [(JPEG, "image/jpeg"), (PNG, "image/png")])
def test_concrete_tesseract_adapter_success_is_bounded_and_deterministic(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    mime: str,
) -> None:
    _require_concrete_platform()
    tsv = _tsv(
        "1\t1\t0\t0\t0\t0\t0\t0\t800\t1200\t-1\t",
        "5\t1\t1\t1\t1\t1\t10\t20\t50\t10\t96.25\tTOTAL",
    )
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(repr(tsv.encode("utf-8"))),
        name=f"tesseract-{mime.rsplit('/', 1)[1]}",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    same = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    assert engine.identity == same.identity
    launches: list[tuple[list[str], tuple[int, ...]]] = []
    real_popen = ocr_module.subprocess.Popen

    def capture_popen(arguments: list[str], **kwargs: object):
        launches.append((arguments, kwargs["pass_fds"]))  # type: ignore[arg-type]
        return real_popen(arguments, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(ocr_module.subprocess, "Popen", capture_popen)
    monkeypatch.setenv("OCR_PARENT_SECRET", "must-not-leak")
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"concrete_{mime.rsplit('/', 1)[1]}",
        content=content,
        mime_type=mime,
    )
    result = _call(migrated_temp_db_connection, attachment_id, engine)
    assert result.status == ReceiptOcrExtractionStatus.SUCCEEDED
    assert result.block_count == 1
    block = migrated_temp_db_connection.execute("SELECT * FROM receipt_ocr_blocks").fetchone()
    assert block["normalized_text"] == "TOTAL"
    assert block["confidence_scaled"] == 9625
    assert len(launches) == 2
    assert launches[0][0][0] == launches[1][0][0]
    assert launches[0][0][0].startswith("/proc/self/fd/")
    assert launches[0][0][0] != str(executable)
    executable_fd = int(launches[0][0][0].rsplit("/", 1)[1])
    assert executable_fd in launches[0][1]
    assert executable_fd in launches[1][1]


def test_concrete_adapter_rejects_symlink_and_writable_binary(tmp_path: Path) -> None:
    _require_concrete_platform()
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(repr(b"")),
        name="unsafe-tesseract",
    )
    executable.chmod(0o522)
    with pytest.raises(InvalidOcrConfigurationError):
        TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    executable.chmod(0o500)
    link = (tmp_path / "tesseract-link").resolve()
    link.symlink_to(executable)
    with pytest.raises(InvalidOcrConfigurationError):
        TesseractTsvOcrEngine(link, expected_version="5.3.4")
    with pytest.raises(InvalidOcrConfigurationError):
        TesseractTsvOcrEngine(tmp_path, expected_version="5.3.4")
    missing = (tmp_path / "missing-tesseract").resolve()
    with pytest.raises(InvalidOcrConfigurationError):
        TesseractTsvOcrEngine(missing, expected_version="5.3.4")


def test_concrete_adapter_rejects_path_replacement_before_version_execution(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _require_concrete_platform()
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(repr(b"")),
        name="replace-before-version",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    replacement_marker = (tmp_path / "replacement-before-version-ran").resolve()
    replacement = _write_fake_tesseract(
        tmp_path,
        f"""
import pathlib
pathlib.Path({str(replacement_marker)!r}).write_text('ran', encoding='ascii')
raise SystemExit(0)
""",
        name="replacement-before-version-new",
    )
    os.replace(replacement, executable)
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix="replace_before_version",
    )
    with pytest.raises(InvalidOcrConfigurationError):
        _call(migrated_temp_db_connection, attachment_id, engine)
    assert replacement_marker.exists() is False
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


def test_concrete_adapter_pins_original_bytes_across_version_to_ocr_race(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_concrete_platform()
    tsv = _tsv(
        "1\t1\t0\t0\t0\t0\t0\t0\t800\t1200\t-1\t",
        "5\t1\t1\t1\t1\t1\t10\t20\t50\t10\t96.25\tTOTAL",
    )
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(repr(tsv.encode("utf-8"))),
        name="replace-after-version",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    replacement_marker = (tmp_path / "replacement-after-version-ran").resolve()
    replacement = _write_fake_tesseract(
        tmp_path,
        f"""
import pathlib
pathlib.Path({str(replacement_marker)!r}).write_text('ran', encoding='ascii')
raise SystemExit(0)
""",
        name="replacement-after-version-new",
    )
    real_run = ocr_module._run_bounded_process
    calls = 0

    def replace_after_version(*args: object, **kwargs: object):
        nonlocal calls
        result = real_run(*args, **kwargs)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            os.replace(replacement, executable)
        return result

    monkeypatch.setattr(ocr_module, "_run_bounded_process", replace_after_version)
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix="replace_after_version",
    )
    with pytest.raises(InvalidOcrConfigurationError):
        _call(migrated_temp_db_connection, attachment_id, engine)
    assert calls == 2
    assert replacement_marker.exists() is False
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "mode",
    ("success", "nonzero", "malformed", "timeout", "overflow", "launch", "unexpected"),
)
def test_concrete_adapter_closes_pinned_executable_fd_on_every_exit(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    _require_concrete_platform()
    valid_tsv = _tsv(
        "1\t1\t0\t0\t0\t0\t0\t0\t800\t1200\t-1\t",
        "5\t1\t1\t1\t1\t1\t10\t20\t50\t10\t96.25\tTOTAL",
    )
    if mode == "nonzero":
        body = """
import sys
if sys.argv[1:] == ['--version']:
    print('tesseract 5.3.4')
    raise SystemExit(0)
raise SystemExit(7)
"""
    elif mode == "timeout":
        body = """
import signal
import sys
import time
if sys.argv[1:] == ['--version']:
    print('tesseract 5.3.4')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""
    elif mode == "overflow":
        body = _fake_script_for_output("b'x' * 5000")
    elif mode == "malformed":
        body = _fake_script_for_output("b'wrong\\theader\\n'")
    else:
        body = _fake_script_for_output(repr(valid_tsv.encode("utf-8")))
    executable = _write_fake_tesseract(
        tmp_path,
        body,
        name=f"fd-cleanup-{mode}",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    captured_fds: list[int] = []
    real_open = ocr_module._open_verified_executable

    def capture_open(*args: object, **kwargs: object):
        pinned = real_open(*args, **kwargs)  # type: ignore[arg-type]
        captured_fds.append(pinned.fd)
        return pinned

    monkeypatch.setattr(ocr_module, "_open_verified_executable", capture_open)
    if mode == "launch":
        monkeypatch.setattr(
            ocr_module.subprocess,
            "Popen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("launch failure")),
        )
    if mode == "unexpected":
        monkeypatch.setattr(
            ocr_module,
            "_parse_tesseract_tsv",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
        )
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"fd_cleanup_{mode}",
    )
    limits = ReceiptOcrLimits(
        total_timeout_seconds=0.4 if mode == "timeout" else 30.0,
        termination_grace_seconds=0.05 if mode == "timeout" else 0.25,
        max_stdout_bytes=100 if mode == "overflow" else 8_000_000,
    )
    if mode == "success":
        assert (
            _call(
                migrated_temp_db_connection,
                attachment_id,
                engine,
                limits=limits,
            ).status
            == ReceiptOcrExtractionStatus.SUCCEEDED
        )
    elif mode == "nonzero":
        assert (
            _call(
                migrated_temp_db_connection,
                attachment_id,
                engine,
                limits=limits,
            ).status
            == ReceiptOcrExtractionStatus.ENGINE_FAILED
        )
    else:
        errors = {
            "malformed": MalformedOcrOutputError,
            "timeout": OcrDeadlineExceededError,
            "overflow": OcrResourceLimitExceededError,
            "launch": OcrEngineLaunchError,
            "unexpected": OcrEngineLaunchError,
        }
        with pytest.raises(errors[mode]):
            _call(
                migrated_temp_db_connection,
                attachment_id,
                engine,
                limits=limits,
            )
    assert len(captured_fds) == 1
    with pytest.raises(OSError):
        os.fstat(captured_fds[0])


def test_concrete_adapter_fails_configuration_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ocr_module.sys, "platform", "darwin")
    with pytest.raises(OcrUnsupportedPlatformError):
        TesseractTsvOcrEngine("/absolute/not-used", expected_version="5.3.4")


def test_concrete_adapter_translates_launch_failure(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_concrete_platform()
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(repr(b"")),
        name="launch-failure",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="launch_failure"
    )

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("unsafe raw subprocess detail")

    monkeypatch.setattr(ocr_module.subprocess, "Popen", fail_popen)
    with pytest.raises(OcrEngineLaunchError) as caught:
        _call(migrated_temp_db_connection, attachment_id, engine)
    assert "unsafe raw" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_concrete_adapter_times_out_and_kills_term_ignoring_process(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _require_concrete_platform()
    executable = _write_fake_tesseract(
        tmp_path,
        """
import signal
import sys
import time

if sys.argv[1:] == ['--version']:
    print('tesseract 5.3.4')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
""",
        name="timeout-tesseract",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="timeout"
    )
    started = time.monotonic()
    with pytest.raises(OcrDeadlineExceededError):
        _call(
            migrated_temp_db_connection,
            attachment_id,
            engine,
            limits=ReceiptOcrLimits(
                total_timeout_seconds=0.4,
                termination_grace_seconds=0.05,
                cpu_time_seconds=2,
            ),
        )
    assert time.monotonic() - started < 2
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 0
    )


def test_process_group_cleanup_checks_for_disappearance_after_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    killed = False
    post_kill_probes = 0
    signals: list[int] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 0.01)

    def killpg(_process_group: int, sent_signal: int) -> None:
        nonlocal killed, post_kill_probes
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            killed = True
        elif sent_signal == 0 and killed:
            post_kill_probes += 1
            if post_kill_probes >= 2:
                raise ProcessLookupError

    monkeypatch.setattr(ocr_module.time, "monotonic", monotonic)
    monkeypatch.setattr(ocr_module.time, "sleep", sleep)
    monkeypatch.setattr(ocr_module.os, "killpg", killpg)
    ocr_module._terminate_remaining_process_group(12345, 0.03)
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals
    assert post_kill_probes == 2


def test_concrete_adapter_timeout_leaves_no_process_group_child(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _require_concrete_platform()
    child_pid_path = (tmp_path / "ocr-child.pid").resolve()
    executable = _write_fake_tesseract(
        tmp_path,
        f"""
import os
import pathlib
import signal
import sys
import time

if sys.argv[1:] == ['--version']:
    print('tesseract 5.3.4')
    raise SystemExit(0)
child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
pathlib.Path({str(child_pid_path)!r}).write_text(str(child_pid), encoding='ascii')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
""",
        name="process-group-tesseract",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="process_group_timeout"
    )
    with pytest.raises(OcrDeadlineExceededError):
        _call(
            migrated_temp_db_connection,
            attachment_id,
            engine,
            limits=ReceiptOcrLimits(
                total_timeout_seconds=0.5,
                termination_grace_seconds=0.05,
                cpu_time_seconds=2,
                # RLIMIT_NPROC is an absolute per-UID ceiling. Leave enough room
                # for the shared CI runner while exercising one process-group child.
                process_count=128,
            ),
        )
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    for _ in range(100):
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text(encoding="ascii").split()[2]
        except (FileNotFoundError, ProcessLookupError):
            break
        if state == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("OCR process-group child remained running after timeout cleanup")


@pytest.mark.parametrize(
    ("stdout_literal", "stderr_literal"),
    [
        ("b'x' * 5000", "b''"),
        ("b''", "b'x' * 5000"),
    ],
)
def test_concrete_adapter_bounds_stdout_and_stderr(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    stdout_literal: str,
    stderr_literal: str,
) -> None:
    _require_concrete_platform()
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(stdout_literal, stderr_literal=stderr_literal),
        name=f"bounded-{_hash(stdout_literal + stderr_literal)[:8]}",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"bounded_{_hash(stdout_literal + stderr_literal)[:8]}",
    )
    with pytest.raises(OcrResourceLimitExceededError):
        _call(
            migrated_temp_db_connection,
            attachment_id,
            engine,
            limits=ReceiptOcrLimits(max_stdout_bytes=100, max_stderr_bytes=100),
        )


@pytest.mark.parametrize(
    "output_literal",
    [
        "b'\\xff\\xfe'",
        "b'wrong\\theader\\n'",
        repr(
            _tsv(
                "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
                "5\t1\t1\t1\t1\t1\t99\t0\t2\t1\t90\tbad",
            ).encode("utf-8")
        ),
        repr(
            _tsv(
                "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
                "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t101\tbad",
            ).encode("utf-8")
        ),
    ],
)
def test_concrete_adapter_rejects_malformed_utf8_tsv_coordinates_and_confidence(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    output_literal: str,
) -> None:
    _require_concrete_platform()
    suffix = _hash(output_literal)[:8]
    executable = _write_fake_tesseract(
        tmp_path,
        _fake_script_for_output(output_literal),
        name=f"malformed-{suffix}",
    )
    engine = TesseractTsvOcrEngine(executable, expected_version="5.3.4")
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix=f"malformed_{suffix}"
    )
    with pytest.raises(MalformedOcrOutputError):
        _call(migrated_temp_db_connection, attachment_id, engine)


def _thread_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_separate_connection_same_command_concurrency_has_one_canonical_result(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="concurrent_same"
    )
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker() -> None:
        conn = _thread_connection(migrated_temp_db_path)
        try:
            outcomes.append(_call(conn, attachment_id, FakeEngine(barrier=barrier)))
        except BaseException as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert not [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert sorted(bool(getattr(outcome, "persistence_idempotent")) for outcome in outcomes) == [
        False,
        True,
    ]
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 1
    )
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM receipt_ocr_blocks").fetchone()[0]
        == 2
    )


@pytest.mark.parametrize("different_public_ids", [False, True])
def test_separate_connection_conflicting_concurrency_is_deterministic(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
    different_public_ids: bool,
) -> None:
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection,
        tmp_path,
        suffix=f"concurrent_conflict_{different_public_ids}",
    )
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker(index: int) -> None:
        conn = _thread_connection(migrated_temp_db_path)
        try:
            engine = FakeEngine(
                configuration=("same" if different_public_ids else f"config-{index}"),
                barrier=barrier,
            )
            public_id = f"rocr_concurrent_{index}" if different_public_ids else "rocr_concurrent"
            outcomes.append(_call(conn, attachment_id, engine, public_id=public_id))
        except BaseException as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], OcrIdempotencyConflictError)
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM receipt_ocr_extractions"
        ).fetchone()[0]
        == 1
    )


def test_no_parser_or_financial_rows_are_created(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    excluded_tables = (
        "parser_proposals",
        "parser_proposal_confirmations",
        "receipt_groups",
        "receipt_items",
        "calculation_runs",
        "calculation_snapshots",
        "transactions",
        "settlement_obligations",
        "reconciliation_match_results",
    )
    existing_tables = {
        row[0]
        for row in migrated_temp_db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    before = {
        table: migrated_temp_db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in excluded_tables
        if table in existing_tables
    }
    attachment_id, _path = _persist_attachment(
        migrated_temp_db_connection, tmp_path, suffix="architecture_exclusions"
    )
    _call(migrated_temp_db_connection, attachment_id, FakeEngine())
    after = {
        table: migrated_temp_db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before
