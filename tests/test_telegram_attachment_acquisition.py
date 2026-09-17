"""Focused tests for bounded Telegram attachment acquisition and storage.

Every persistence test uses an authorised temporary database and every
transport is deterministic.  No test contacts Telegram or the live database.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import finance_core.intake.telegram_attachment_acquisition as acquisition_module
import finance_core.intake.telegram_bot_api_transport as transport_module
from finance_core.intake.attachment_evidence import persist_attachment_evidence
from finance_core.intake.telegram_attachment_acquisition import (
    AcquisitionReplayConflictError,
    AcquisitionReplayIntegrityError,
    AttachmentEvidenceHandoffConflictError,
    CallerOwnedTransactionError,
    ContentSignatureMismatchError,
    DurableFileIntegrityConflictError,
    DurablePublicationError,
    InvalidAcquisitionConfigurationError,
    InvalidRemoteFilePathError,
    InvalidTelegramIdentityError,
    MalformedTelegramMetadataError,
    RawIntakeNotFoundError,
    StagingDatabaseRejectedError,
    TelegramAttachmentAcquisitionError,
    TelegramAttachmentAcquisitionLimits,
    TelegramDeclaredFileTooLargeError,
    TelegramDownloadTimeoutError,
    TelegramFileMetadata,
    TelegramHttpResponseError,
    TelegramRedirectError,
    TelegramStreamedFileTooLargeError,
    TelegramTruncatedDownloadError,
    UnexpectedAttachmentPersistenceError,
    UnsafeStorageRootError,
    UnsupportedFilenameExtensionError,
    UnsupportedMimeTypeError,
    acquire_and_persist_telegram_attachment,
)
from finance_core.intake.telegram_bot_api_transport import TelegramBotApiTransport

PDF_BYTES = b"%PDF-1.7\n% bounded attachment\n"
JPEG_BYTES = b"\xff\xd8\xff\xe0receipt-image"
PNG_BYTES = b"\x89PNG\r\n\x1a\nreceipt-image"


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        read_plan: list[int] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.read_plan = list(read_plan or [])
        self.read_error = read_error
        self.position = 0
        self.closed = False
        self.read_sizes: list[int] = []
        self.timeouts: list[float] = []

    def read(self, max_bytes: int, *, timeout_seconds: float) -> bytes:
        self.read_sizes.append(max_bytes)
        self.timeouts.append(timeout_seconds)
        if self.read_error is not None:
            error = self.read_error
            self.read_error = None
            raise error
        if self.position >= len(self.body):
            return b""
        planned = self.read_plan.pop(0) if self.read_plan else max_bytes
        size = min(max_bytes, planned)
        chunk = self.body[self.position : self.position + size]
        self.position += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(
        self,
        body: bytes = PDF_BYTES,
        *,
        metadata: TelegramFileMetadata | None = None,
        response: FakeResponse | None = None,
        metadata_error: Exception | None = None,
        download_error: Exception | None = None,
    ) -> None:
        self.metadata = metadata or TelegramFileMetadata(
            file_path="documents/file.pdf", file_size=len(body)
        )
        self.response = response or FakeResponse(
            body,
            headers={
                "Content-Length": str(len(body)),
                "Content-Type": "application/octet-stream",
            },
        )
        self.metadata_error = metadata_error
        self.download_error = download_error
        self.metadata_calls = 0
        self.download_calls = 0
        self.metadata_timeouts: list[float] = []
        self.download_timeouts: list[float] = []
        self.paths: list[str] = []

    def get_file_metadata(
        self,
        file_id: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_header_bytes: int,
    ) -> TelegramFileMetadata:
        self.metadata_calls += 1
        self.metadata_timeouts.append(timeout_seconds)
        if self.metadata_error is not None:
            raise self.metadata_error
        return self.metadata

    def open_file_download(
        self,
        file_path: str,
        *,
        timeout_seconds: float,
        max_header_bytes: int,
    ) -> FakeResponse:
        self.download_calls += 1
        self.download_timeouts.append(timeout_seconds)
        self.paths.append(file_path)
        if self.download_error is not None:
            raise self.download_error
        return self.response


class MutableClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


class AdvancingMetadataTransport(FakeTransport):
    def __init__(self, clock: MutableClock, seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds

    def get_file_metadata(self, *args: Any, **kwargs: Any) -> TelegramFileMetadata:
        result = super().get_file_metadata(*args, **kwargs)
        self.clock.advance(self.seconds)
        return result


class AdvancingReadResponse(FakeResponse):
    def __init__(self, body: bytes, clock: MutableClock, seconds: float) -> None:
        super().__init__(
            body,
            headers={"Content-Type": "application/pdf"},
            read_plan=[5, 5, 5],
        )
        self.clock = clock
        self.seconds = seconds

    def read(self, max_bytes: int, *, timeout_seconds: float) -> bytes:
        result = super().read(max_bytes, timeout_seconds=timeout_seconds)
        self.clock.advance(self.seconds)
        return result


def _insert_raw_intake(conn: sqlite3.Connection, public_id: str = "raw_attachment") -> int:
    cursor = conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, source_channel, raw_input, received_at
        ) VALUES (?, 'telegram_pdf', 'telegram', ?, ?)
        """,
        (public_id, "original raw input", "2026-07-19T12:00:00+00:00"),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _private_storage(tmp_path: Path, name: str = "attachments") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _command(
    conn: sqlite3.Connection,
    storage_root: Path,
    raw_intake_id: int,
    *,
    transport: FakeTransport,
    public_id: str = "tgae_acquisition_test",
    telegram_file_id: str = "file-id-1",
    telegram_file_unique_id: str = "unique-id-1",
    original_filename: str | None = "receipt.pdf",
    declared_mime_type: str | None = "application/pdf",
    limits: TelegramAttachmentAcquisitionLimits = TelegramAttachmentAcquisitionLimits(),
    clock: Any | None = None,
):
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["_clock"] = clock
    return acquire_and_persist_telegram_attachment(
        conn,
        transport=transport,
        storage_root=storage_root,
        public_id=public_id,
        raw_intake_id=raw_intake_id,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        original_filename=original_filename,
        declared_mime_type=declared_mime_type,
        limits=limits,
        **kwargs,
    )


def _temp_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.name.startswith(".telegram-acquisition-")]


def _filesystem_snapshot(root: Path) -> list[tuple[str, int, int, int, int]]:
    return sorted(
        (
            str(path.relative_to(root)),
            os.lstat(path).st_mode,
            os.lstat(path).st_size,
            os.lstat(path).st_ino,
            os.lstat(path).st_mtime_ns,
        )
        for path in root.rglob("*")
    )


def _persist_pr216_pdf(
    conn: sqlite3.Connection,
    path: Path,
    raw_intake_id: int,
    *,
    public_id: str = "tgae_acquisition_test",
    telegram_file_id: str = "file-id-1",
    telegram_file_unique_id: str = "unique-id-1",
) -> None:
    persist_attachment_evidence(
        conn,
        path,
        public_id=public_id,
        raw_intake_id=raw_intake_id,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        original_filename="receipt.pdf",
        declared_mime_type="application/pdf",
        expected_file_size=len(PDF_BYTES),
        expected_content_hash=hashlib.sha256(PDF_BYTES).hexdigest(),
    )


class TestConfigurationAndOrdering:
    def test_default_limits_are_conservative_and_immutable(self) -> None:
        limits = TelegramAttachmentAcquisitionLimits()
        assert limits.max_file_bytes == 20_000_000
        assert limits.total_timeout_seconds == 30
        assert limits.metadata_response_max_bytes == 65_536
        assert limits.download_chunk_bytes == 65_536
        with pytest.raises(Exception):
            limits.max_file_bytes = 1  # type: ignore[misc]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("max_file_bytes", True),
            ("max_file_bytes", 0),
            ("metadata_response_max_bytes", -1),
            ("download_chunk_bytes", 0),
            ("response_headers_max_bytes", 65_537),
            ("remote_path_max_length", 4_097),
            ("total_timeout_seconds", False),
            ("total_timeout_seconds", 0),
            ("total_timeout_seconds", float("inf")),
            ("total_timeout_seconds", float("nan")),
            ("total_timeout_seconds", 301),
        ],
    )
    def test_invalid_limits_fail_closed(self, field: str, value: object) -> None:
        with pytest.raises(InvalidAcquisitionConfigurationError):
            TelegramAttachmentAcquisitionLimits(**{field: value})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field,value,error",
        [
            ("public_id", "wrong", InvalidAcquisitionConfigurationError),
            ("raw_intake_id", True, InvalidAcquisitionConfigurationError),
            ("telegram_file_id", " bad", InvalidTelegramIdentityError),
            ("telegram_file_unique_id", "", InvalidTelegramIdentityError),
            ("original_filename", "receipt.exe", UnsupportedFilenameExtensionError),
            ("original_filename", "receipt.exe.pdf", UnsupportedFilenameExtensionError),
            ("declared_mime_type", "text/html", UnsupportedMimeTypeError),
        ],
    )
    def test_invalid_inputs_are_rejected_before_transport(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        field: str,
        value: object,
        error: type[Exception],
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        fake = FakeTransport()
        values: dict[str, Any] = {
            "public_id": "tgae_input_validation",
            "raw_intake_id": raw_id,
            "telegram_file_id": "file-id",
            "telegram_file_unique_id": "unique-id",
            "original_filename": "receipt.pdf",
            "declared_mime_type": "application/pdf",
        }
        values[field] = value
        with pytest.raises(error):
            acquire_and_persist_telegram_attachment(
                conn, transport=fake, storage_root=root, **values
            )
        assert fake.metadata_calls == 0
        assert fake.download_calls == 0

    @pytest.mark.parametrize(
        "field,value",
        [
            ("original_filename", "r" * 1_021 + ".pdf"),
            ("declared_mime_type", "application/pdf;" + "x" * 240),
            ("telegram_file_id", "f" * 513),
            ("telegram_file_unique_id", "u" * 513),
        ],
    )
    def test_oversized_source_identity_is_rejected_without_echoing_value(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        field: str,
        value: str,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        fake = FakeTransport()
        values: dict[str, Any] = {
            "public_id": "tgae_oversized_identity",
            "raw_intake_id": raw_id,
            "telegram_file_id": "file-id",
            "telegram_file_unique_id": "unique-id",
            "original_filename": "receipt.pdf",
            "declared_mime_type": "application/pdf",
        }
        values[field] = value
        with pytest.raises(TelegramAttachmentAcquisitionError) as raised:
            acquire_and_persist_telegram_attachment(
                conn,
                transport=fake,
                storage_root=_private_storage(tmp_path),
                **values,
            )
        assert value not in str(raised.value)
        assert value not in repr(raised.value)
        assert fake.metadata_calls == 0
        assert fake.download_calls == 0

    def test_missing_raw_intake_is_rejected_before_transport(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fake = FakeTransport()
        with pytest.raises(RawIntakeNotFoundError):
            _command(
                migrated_temp_db_connection,
                _private_storage(tmp_path),
                999_999,
                transport=fake,
            )
        assert fake.metadata_calls == 0

    def test_active_transaction_is_rejected_before_transport(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        conn.execute("BEGIN")
        fake = FakeTransport()
        with pytest.raises(CallerOwnedTransactionError):
            _command(conn, _private_storage(tmp_path), raw_id, transport=fake)
        assert conn.in_transaction is True
        assert fake.metadata_calls == 0
        conn.rollback()

    def test_untrusted_database_is_rejected_before_transport(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "not-staging.sqlite")
        fake = FakeTransport()
        try:
            with pytest.raises(StagingDatabaseRejectedError):
                _command(conn, _private_storage(tmp_path), 1, transport=fake)
        finally:
            conn.close()
        assert fake.metadata_calls == 0


class TestMetadataAndRemotePath:
    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/absolute/file.pdf",
            "../file.pdf",
            "documents/../file.pdf",
            "documents/./file.pdf",
            "documents\\file.pdf",
            "documents/file.pdf?x=1",
            "documents/file.pdf#fragment",
            "https://evil.example/file.pdf",
            "//evil.example/file.pdf",
            "documents/%2e%2e/file.pdf",
            "file:documents/file.pdf",
            "documents/\x00file.pdf",
            "documents/\nfile.pdf",
            "x" * 1_025,
        ],
    )
    def test_unsafe_remote_path_is_rejected_and_never_downloaded(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        path: str,
    ) -> None:
        raw_id = _insert_raw_intake(migrated_temp_db_connection)
        fake = FakeTransport(metadata=TelegramFileMetadata(file_path=path))
        with pytest.raises(InvalidRemoteFilePathError):
            _command(
                migrated_temp_db_connection,
                _private_storage(tmp_path),
                raw_id,
                transport=fake,
            )
        assert fake.download_calls == 0

    def test_nested_remote_path_is_forwarded_as_remote_identifier(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        fake = FakeTransport(
            metadata=TelegramFileMetadata(
                file_path="documents/nested/file.pdf", file_size=len(PDF_BYTES)
            )
        )
        _command(conn, _private_storage(tmp_path), raw_id, transport=fake)
        assert fake.paths == ["documents/nested/file.pdf"]

    @pytest.mark.parametrize("file_size", [True, -1, "12"])
    def test_invalid_metadata_size_is_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        file_size: Any,
    ) -> None:
        raw_id = _insert_raw_intake(migrated_temp_db_connection)
        fake = FakeTransport(
            metadata=TelegramFileMetadata(file_path="documents/file.pdf", file_size=file_size)
        )
        with pytest.raises(MalformedTelegramMetadataError):
            _command(
                migrated_temp_db_connection,
                _private_storage(tmp_path),
                raw_id,
                transport=fake,
            )

    def test_metadata_above_limit_rejected_before_download(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        raw_id = _insert_raw_intake(migrated_temp_db_connection)
        fake = FakeTransport(
            metadata=TelegramFileMetadata(file_path="documents/file.pdf", file_size=11)
        )
        limits = TelegramAttachmentAcquisitionLimits(max_file_bytes=10)
        with pytest.raises(TelegramDeclaredFileTooLargeError):
            _command(
                migrated_temp_db_connection,
                _private_storage(tmp_path),
                raw_id,
                transport=fake,
                limits=limits,
            )
        assert fake.download_calls == 0

    @pytest.mark.parametrize(
        "metadata",
        [
            TelegramFileMetadata(file_path="documents/file.pdf", file_id="different-file-id"),
            TelegramFileMetadata(
                file_path="documents/file.pdf", file_unique_id="different-unique-id"
            ),
        ],
    )
    def test_returned_telegram_identity_mismatch_is_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        metadata: TelegramFileMetadata,
    ) -> None:
        raw_id = _insert_raw_intake(migrated_temp_db_connection)
        fake = FakeTransport(metadata=metadata)
        with pytest.raises(InvalidTelegramIdentityError):
            _command(
                migrated_temp_db_connection,
                _private_storage(tmp_path),
                raw_id,
                transport=fake,
            )
        assert fake.download_calls == 0

    def test_transport_error_is_chained_without_arbitrary_repr(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        raw_id = _insert_raw_intake(migrated_temp_db_connection)
        fake = FakeTransport(metadata_error=OSError("transport detail"))
        with pytest.raises(TelegramAttachmentAcquisitionError) as raised:
            _command(
                migrated_temp_db_connection,
                _private_storage(tmp_path),
                raw_id,
                transport=fake,
            )
        assert raised.value.__cause__ is not None
        assert "transport detail" not in str(raised.value)


class TestByteHeaderAndTimeoutLimits:
    def test_exact_byte_limit_is_accepted(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        limits = TelegramAttachmentAcquisitionLimits(max_file_bytes=len(PDF_BYTES))
        result = _command(
            conn,
            _private_storage(tmp_path),
            raw_id,
            transport=FakeTransport(),
            limits=limits,
        )
        assert result.observed_file_size == len(PDF_BYTES)

    def test_one_byte_over_limit_is_rejected_and_cleaned(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        body = PDF_BYTES + b"x"
        response = FakeResponse(body, headers={"Content-Type": "application/pdf"})
        fake = FakeTransport(
            body=body,
            metadata=TelegramFileMetadata(file_path="documents/file.pdf"),
            response=response,
        )
        with pytest.raises(TelegramStreamedFileTooLargeError):
            _command(
                conn,
                root,
                raw_id,
                transport=fake,
                limits=TelegramAttachmentAcquisitionLimits(
                    max_file_bytes=len(PDF_BYTES), download_chunk_bytes=7
                ),
            )
        assert response.closed is True
        assert _temp_files(root) == []

    @pytest.mark.parametrize("content_length", ["x", "-1", "1,2", ""])
    def test_malformed_content_length_is_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        content_length: str,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        response = FakeResponse(
            PDF_BYTES,
            headers={"Content-Length": content_length, "Content-Type": "application/pdf"},
        )
        with pytest.raises(TelegramHttpResponseError):
            _command(
                conn,
                _private_storage(tmp_path),
                raw_id,
                transport=FakeTransport(response=response),
            )
        assert response.closed is True

    def test_truncated_response_is_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        response = FakeResponse(
            PDF_BYTES,
            headers={
                "Content-Length": str(len(PDF_BYTES) + 1),
                "Content-Type": "application/pdf",
            },
        )
        with pytest.raises(TelegramTruncatedDownloadError):
            _command(
                conn,
                _private_storage(tmp_path),
                raw_id,
                transport=FakeTransport(
                    metadata=TelegramFileMetadata(file_path="documents/file.pdf"),
                    response=response,
                ),
            )

    def test_actual_bytes_above_content_length_are_rejected(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        response = FakeResponse(
            PDF_BYTES,
            headers={
                "Content-Length": str(len(PDF_BYTES) - 1),
                "Content-Type": "application/pdf",
            },
        )
        with pytest.raises(TelegramHttpResponseError, match="Content-Length"):
            _command(
                conn,
                _private_storage(tmp_path),
                raw_id,
                transport=FakeTransport(
                    metadata=TelegramFileMetadata(file_path="documents/file.pdf"),
                    response=response,
                ),
            )

    def test_content_length_that_contradicts_metadata_is_rejected(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        response = FakeResponse(
            PDF_BYTES,
            headers={
                "Content-Length": str(len(PDF_BYTES)),
                "Content-Type": "application/pdf",
            },
        )
        fake = FakeTransport(
            metadata=TelegramFileMetadata(
                file_path="documents/file.pdf", file_size=len(PDF_BYTES) - 1
            ),
            response=response,
        )
        with pytest.raises(TelegramHttpResponseError, match="contradicts"):
            _command(conn, _private_storage(tmp_path), raw_id, transport=fake)
        assert response.closed is True

    def test_oversized_headers_are_rejected(self, migrated_temp_db_connection, tmp_path) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        response = FakeResponse(PDF_BYTES, headers={"X-Large": "x" * 100})
        with pytest.raises(TelegramHttpResponseError, match="headers"):
            _command(
                conn,
                _private_storage(tmp_path),
                raw_id,
                transport=FakeTransport(response=response),
                limits=TelegramAttachmentAcquisitionLimits(response_headers_max_bytes=16),
            )

    def test_non_success_and_redirect_statuses_close_response(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        for status, error in [(302, TelegramRedirectError), (500, TelegramHttpResponseError)]:
            response = FakeResponse(PDF_BYTES, status_code=status)
            with pytest.raises(error):
                _command(
                    conn,
                    _private_storage(tmp_path, f"root-{status}"),
                    raw_id,
                    transport=FakeTransport(response=response),
                    public_id=f"tgae_status_{status}",
                    telegram_file_unique_id=f"unique-{status}",
                )
            assert response.closed is True

    def test_metadata_deadline_uses_monotonic_clock(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        clock = MutableClock()
        fake = AdvancingMetadataTransport(clock, 31)
        with pytest.raises(TelegramDownloadTimeoutError):
            _command(
                conn,
                _private_storage(tmp_path),
                raw_id,
                transport=fake,
                clock=clock,
            )
        assert fake.download_calls == 0
        assert fake.metadata_timeouts == [30]

    def test_deadline_between_chunks_closes_stream_and_cleans_temp(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        clock = MutableClock()
        response = AdvancingReadResponse(PDF_BYTES, clock, 16)
        fake = FakeTransport(
            metadata=TelegramFileMetadata(file_path="documents/file.pdf"),
            response=response,
        )
        with pytest.raises(TelegramDownloadTimeoutError):
            _command(conn, root, raw_id, transport=fake, clock=clock)
        assert response.closed is True
        assert _temp_files(root) == []

    def test_deadline_immediately_before_publication_leaves_no_durable_file(
        self, migrated_temp_db_connection, tmp_path, monkeypatch
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        clock = MutableClock()
        real_fsync = acquisition_module.os.fsync

        def advancing_fsync(fd: int) -> None:
            real_fsync(fd)
            clock.advance(31)

        monkeypatch.setattr(acquisition_module.os, "fsync", advancing_fsync)
        with pytest.raises(TelegramDownloadTimeoutError):
            _command(conn, root, raw_id, transport=FakeTransport(), clock=clock)
        assert _temp_files(root) == []
        assert list(root.glob("*/*")) == []


class TestMimeExtensionAndSignature:
    @pytest.mark.parametrize(
        "body,filename,declared,http_mime,expected_mime,expected_extension",
        [
            (
                PDF_BYTES,
                "receipt.PDF",
                "application/pdf; version=1.7",
                "application/pdf",
                "application/pdf",
                ".pdf",
            ),
            (
                JPEG_BYTES,
                "receipt.jpeg",
                "image/jpeg",
                "application/octet-stream",
                "image/jpeg",
                ".jpg",
            ),
            (PNG_BYTES, "receipt.PNG", None, None, "image/png", ".png"),
            (PDF_BYTES, None, None, "application/octet-stream", "application/pdf", ".pdf"),
            (JPEG_BYTES, "receipt", None, "image/jpeg", "image/jpeg", ".jpg"),
        ],
    )
    def test_supported_signature_matrix(
        self,
        migrated_temp_db_connection,
        tmp_path,
        body,
        filename,
        declared,
        http_mime,
        expected_mime,
        expected_extension,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        headers = {"Content-Length": str(len(body))}
        if http_mime is not None:
            headers["Content-Type"] = http_mime
        fake = FakeTransport(
            body=body,
            metadata=TelegramFileMetadata(file_path="documents/file", file_size=None),
            response=FakeResponse(body, headers=headers, read_plan=[1, 2, 3, 5, 8]),
        )
        result = _command(
            conn,
            _private_storage(tmp_path),
            raw_id,
            transport=fake,
            original_filename=filename,
            declared_mime_type=declared,
        )
        assert result.detected_mime_type == expected_mime
        assert result.canonical_extension == expected_extension

    @pytest.mark.parametrize(
        "body,filename,declared,http_mime,error",
        [
            (
                b"",
                "receipt.pdf",
                "application/pdf",
                "application/pdf",
                ContentSignatureMismatchError,
            ),
            (
                b"<html>bad</html>",
                "receipt.pdf",
                "application/pdf",
                "application/pdf",
                ContentSignatureMismatchError,
            ),
            (
                b'{"json":true}',
                "receipt.pdf",
                "application/pdf",
                "application/pdf",
                ContentSignatureMismatchError,
            ),
            (
                b"PK\x03\x04zip",
                "receipt.pdf",
                "application/pdf",
                "application/pdf",
                ContentSignatureMismatchError,
            ),
            (
                b"MZ-executable",
                "receipt.pdf",
                "application/pdf",
                "application/pdf",
                ContentSignatureMismatchError,
            ),
            (JPEG_BYTES, "receipt.pdf", "image/jpeg", "image/jpeg", ContentSignatureMismatchError),
            (
                PDF_BYTES,
                "receipt.pdf",
                "image/jpeg",
                "application/pdf",
                ContentSignatureMismatchError,
            ),
            (
                PDF_BYTES,
                "receipt.pdf",
                "application/pdf",
                "image/png",
                ContentSignatureMismatchError,
            ),
            (PDF_BYTES, "receipt.pdf", "application/pdf", "text/html", UnsupportedMimeTypeError),
        ],
    )
    def test_unsupported_or_mismatched_content_is_rejected_and_cleaned(
        self,
        migrated_temp_db_connection,
        tmp_path,
        body,
        filename,
        declared,
        http_mime,
        error,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        fake = FakeTransport(
            body=body,
            metadata=TelegramFileMetadata(file_path="documents/file", file_size=None),
            response=FakeResponse(
                body,
                headers={
                    "Content-Length": str(len(body)),
                    "Content-Type": http_mime,
                },
            ),
        )
        with pytest.raises(error):
            _command(
                conn,
                root,
                raw_id,
                transport=fake,
                original_filename=filename,
                declared_mime_type=declared,
            )
        assert _temp_files(root) == []


class TestStorageAndPersistence:
    @pytest.mark.parametrize("root_kind", ["relative", "file", "symlink", "permissive"])
    def test_unsafe_storage_roots_are_rejected_before_transport(
        self, migrated_temp_db_connection, tmp_path, root_kind
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        if root_kind == "relative":
            root: Path | str = "relative-storage"
        elif root_kind == "file":
            file_root = tmp_path / "storage-file"
            file_root.write_text("not a directory", encoding="utf-8")
            root = file_root
        elif root_kind == "symlink":
            target = _private_storage(tmp_path, "target")
            symlink_root = tmp_path / "storage-link"
            symlink_root.symlink_to(target, target_is_directory=True)
            root = symlink_root
        else:
            permissive_root = tmp_path / "permissive"
            permissive_root.mkdir(mode=0o755)
            permissive_root.chmod(0o755)
            root = permissive_root
        fake = FakeTransport()
        with pytest.raises(UnsafeStorageRootError):
            _command(conn, root, raw_id, transport=fake)  # type: ignore[arg-type]
        assert fake.metadata_calls == 0

    def test_success_publishes_private_content_addressed_file_and_evidence(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        malicious_name = "../../do-not-use-as-local-path.pdf"
        result = _command(
            conn,
            root,
            raw_id,
            transport=FakeTransport(),
            original_filename=malicious_name,
        )
        expected_hash = hashlib.sha256(PDF_BYTES).hexdigest()
        final_path = Path(result.attachment_path)
        assert final_path == root / expected_hash[:2] / f"{expected_hash}.pdf"
        assert final_path.read_bytes() == PDF_BYTES
        assert final_path.is_relative_to(root)
        assert stat_mode(final_path) == 0o400
        assert stat_mode(final_path.parent) == 0o700
        assert malicious_name not in result.attachment_path
        assert result.content_hash == expected_hash
        assert result.network_download_occurred is True
        assert result.durable_file_reused is False
        assert result.persistence_idempotent is False
        assert _temp_files(root) == []

        source = conn.execute(
            "SELECT * FROM telegram_attachment_source WHERE public_id = ?",
            ("tgae_acquisition_test",),
        ).fetchone()
        assert source is not None
        assert source["original_attachment_path"] == str(final_path)
        assert source["observed_file_size"] == len(PDF_BYTES)
        assert source["content_hash"] == expected_hash
        raw = conn.execute(
            "SELECT raw_input, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (raw_id,),
        ).fetchone()
        assert raw["raw_input"] == "original raw input"
        assert raw["attachment_path"] == str(final_path)
        assert raw["attachment_hash"] == expected_hash
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_intake_evidence").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def test_replay_uses_persisted_path_without_transport(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        first = _command(conn, root, raw_id, transport=FakeTransport())
        replay_transport = FakeTransport(metadata_error=AssertionError("must not run"))
        replay = _command(conn, root, raw_id, transport=replay_transport)
        assert replay.attachment_path == first.attachment_path
        assert replay.network_download_occurred is False
        assert replay.durable_file_reused is True
        assert replay.persistence_idempotent is True
        assert replay_transport.metadata_calls == 0
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1

    @pytest.mark.parametrize(
        "case",
        [
            "outside_root",
            "wrong_shard",
            "wrong_hash_name",
            "noncanonical_extension",
            "file_mode",
            "shard_mode",
            "symlink_shard",
            "symlink_file",
            "permissive_root",
        ],
    )
    def test_generic_pr216_evidence_does_not_prove_pr217_durable_replay(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        case: str,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        shard_name = digest[:2]
        final_name = f"{digest}.pdf"

        if case == "outside_root":
            stored = tmp_path / "outside.pdf"
            stored.write_bytes(PDF_BYTES)
            stored.chmod(0o400)
        else:
            shard = root / ("ff" if case == "wrong_shard" else shard_name)
            shard.mkdir(mode=0o700)
            shard.chmod(0o700)
            name = final_name
            if case == "wrong_hash_name":
                name = f"{'0' * 64}.pdf"
            elif case == "noncanonical_extension":
                name = f"{digest}.PDF"
            stored = shard / name
            stored.write_bytes(PDF_BYTES)
            stored.chmod(0o400)

        _persist_pr216_pdf(conn, stored, raw_id)

        if case == "file_mode":
            stored.chmod(0o600)
        elif case == "shard_mode":
            stored.parent.chmod(0o755)
        elif case == "symlink_shard":
            target_shard = tmp_path / "target-shard"
            target_shard.mkdir(mode=0o700)
            target_shard.chmod(0o700)
            target_file = target_shard / final_name
            target_file.write_bytes(PDF_BYTES)
            target_file.chmod(0o400)
            stored.unlink()
            stored.parent.rmdir()
            stored.parent.symlink_to(target_shard, target_is_directory=True)
        elif case == "symlink_file":
            outside = tmp_path / "symlink-target.pdf"
            outside.write_bytes(PDF_BYTES)
            outside.chmod(0o400)
            stored.unlink()
            stored.symlink_to(outside)
        elif case == "permissive_root":
            root.chmod(0o755)

        before_db_changes = conn.total_changes
        before_files = _filesystem_snapshot(tmp_path)
        fake = FakeTransport(metadata_error=AssertionError("transport must not run"))

        def forbidden_pr216_handoff(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("PR #216 handoff must follow durable replay proof")

        monkeypatch.setattr(
            acquisition_module,
            "persist_attachment_evidence",
            forbidden_pr216_handoff,
        )
        with pytest.raises((AcquisitionReplayIntegrityError, UnsafeStorageRootError)):
            _command(conn, root, raw_id, transport=fake)

        assert fake.metadata_calls == 0
        assert fake.download_calls == 0
        assert conn.total_changes == before_db_changes
        assert _filesystem_snapshot(tmp_path) == before_files
        assert conn.in_transaction is False
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    def test_direct_pr216_evidence_replays_only_when_full_pr217_contract_is_valid(
        self, migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        shard = root / digest[:2]
        shard.mkdir(mode=0o700)
        shard.chmod(0o700)
        stored = shard / f"{digest}.pdf"
        stored.write_bytes(PDF_BYTES)
        stored.chmod(0o400)
        _persist_pr216_pdf(conn, stored, raw_id)

        fake = FakeTransport(metadata_error=AssertionError("transport must not run"))
        replay = _command(conn, root, raw_id, transport=fake)

        assert replay.attachment_path == str(stored)
        assert replay.content_hash == digest
        assert replay.persistence_idempotent is True
        assert replay.network_download_occurred is False
        assert fake.metadata_calls == 0
        assert fake.download_calls == 0

    @pytest.mark.parametrize(
        "field,value",
        [
            ("raw_intake_id", "second"),
            ("telegram_file_id", "different-file"),
            ("telegram_file_unique_id", "different-unique"),
            ("original_filename", "different.pdf"),
            ("declared_mime_type", "application/pdf; source=other"),
        ],
    )
    def test_replay_conflicts_before_network(
        self, migrated_temp_db_connection, tmp_path, field, value
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        second_raw = _insert_raw_intake(conn, "raw_attachment_second")
        root = _private_storage(tmp_path)
        _command(conn, root, raw_id, transport=FakeTransport())
        fake = FakeTransport()
        kwargs: dict[str, Any] = {}
        if field == "raw_intake_id":
            raw_id = second_raw
        else:
            kwargs[field] = value
        with pytest.raises(AcquisitionReplayConflictError):
            _command(conn, root, raw_id, transport=fake, **kwargs)
        assert fake.metadata_calls == 0

    def test_missing_persisted_file_fails_closed_without_network(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        first = _command(conn, root, raw_id, transport=FakeTransport())
        Path(first.attachment_path).unlink()
        fake = FakeTransport()
        with pytest.raises(AcquisitionReplayIntegrityError):
            _command(conn, root, raw_id, transport=fake)
        assert fake.metadata_calls == 0

    def test_post_publication_failure_leaves_file_and_replay_reuses_it(
        self, migrated_temp_db_connection, tmp_path, monkeypatch
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)

        def fail(stage: str) -> None:
            if stage == "before_attachment_evidence_persistence":
                raise RuntimeError("controlled persistence boundary failure")

        monkeypatch.setattr(acquisition_module, "_failure_injection_hook", fail)
        with pytest.raises(UnexpectedAttachmentPersistenceError):
            _command(conn, root, raw_id, transport=FakeTransport())
        expected_hash = hashlib.sha256(PDF_BYTES).hexdigest()
        final = root / expected_hash[:2] / f"{expected_hash}.pdf"
        assert final.read_bytes() == PDF_BYTES
        assert _temp_files(root) == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0

        monkeypatch.setattr(acquisition_module, "_failure_injection_hook", None)
        replay = _command(conn, root, raw_id, transport=FakeTransport())
        assert replay.durable_file_reused is True
        assert replay.persistence_idempotent is False

    def test_preexisting_conflicting_target_fails_closed_without_overwrite(
        self, migrated_temp_db_connection, tmp_path
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        shard = root / digest[:2]
        shard.mkdir(mode=0o700)
        shard.chmod(0o700)
        target = shard / f"{digest}.pdf"
        conflicting = b"%PDF-1.7\n% conflicting bytes\n"
        target.write_bytes(conflicting)
        target.chmod(0o400)
        with pytest.raises(DurableFileIntegrityConflictError):
            _command(conn, root, raw_id, transport=FakeTransport())
        assert target.read_bytes() == conflicting
        assert _temp_files(root) == []

    def test_symlink_final_target_fails_closed(self, migrated_temp_db_connection, tmp_path) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        shard = root / digest[:2]
        shard.mkdir(mode=0o700)
        shard.chmod(0o700)
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(PDF_BYTES)
        target = shard / f"{digest}.pdf"
        target.symlink_to(outside)
        with pytest.raises(DurableFileIntegrityConflictError):
            _command(conn, root, raw_id, transport=FakeTransport())
        assert target.is_symlink()
        assert outside.read_bytes() == PDF_BYTES

    def test_failure_after_durable_publication_keeps_final_and_cleans_temp(
        self, migrated_temp_db_connection, tmp_path, monkeypatch
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)

        def fail(stage: str) -> None:
            if stage == "after_durable_publication":
                raise RuntimeError("controlled crash")

        monkeypatch.setattr(acquisition_module, "_failure_injection_hook", fail)
        with pytest.raises(DurablePublicationError):
            _command(conn, root, raw_id, transport=FakeTransport())
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        assert (root / digest[:2] / f"{digest}.pdf").read_bytes() == PDF_BYTES
        assert _temp_files(root) == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "stage",
        [
            "after_temporary_file_creation",
            "before_first_temporary_write",
            "after_temporary_write",
            "before_temporary_flush",
            "before_temporary_fsync",
            "before_durable_publication",
        ],
    )
    def test_prepublication_failure_boundaries_clean_temp_and_publish_nothing(
        self, migrated_temp_db_connection, tmp_path, monkeypatch, stage
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)

        def fail(current: str) -> None:
            if current == stage:
                raise OSError("controlled failure")

        monkeypatch.setattr(acquisition_module, "_failure_injection_hook", fail)
        with pytest.raises(TelegramAttachmentAcquisitionError):
            _command(conn, root, raw_id, transport=FakeTransport())
        assert _temp_files(root) == []
        assert list(root.glob("*/*")) == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0

    def test_directory_sync_failure_keeps_published_file_and_cleans_temp(
        self, migrated_temp_db_connection, tmp_path, monkeypatch
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        real_fsync = acquisition_module.os.fsync
        calls = 0

        def fail_shard_sync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("controlled directory sync failure")
            real_fsync(fd)

        monkeypatch.setattr(acquisition_module.os, "fsync", fail_shard_sync)
        with pytest.raises(DurablePublicationError, match="directory"):
            _command(conn, root, raw_id, transport=FakeTransport())
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        assert (root / digest[:2] / f"{digest}.pdf").read_bytes() == PDF_BYTES
        assert _temp_files(root) == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0

    @pytest.mark.parametrize("failure_phase", ["metadata", "download", "signature"])
    def test_cleanup_unlink_failure_preserves_primary_and_reports_residue(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure_phase: str,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        primary_type: type[Exception]
        if failure_phase == "metadata":
            fake = FakeTransport(metadata_error=RuntimeError("untrusted metadata detail"))
            primary_type = acquisition_module.TelegramMetadataRequestError
        elif failure_phase == "download":
            fake = FakeTransport(download_error=RuntimeError("untrusted download detail"))
            primary_type = TelegramHttpResponseError
        else:
            fake = FakeTransport(
                body=b"unsupported",
                metadata=TelegramFileMetadata(file_path="documents/file", file_size=None),
                response=FakeResponse(b"unsupported"),
            )
            primary_type = ContentSignatureMismatchError

        real_unlink = acquisition_module.os.unlink
        cleanup_attempted: list[str] = []

        def fail_temp_unlink(path: str, *, dir_fd: int | None = None) -> None:
            if path.startswith(".telegram-acquisition-"):
                cleanup_attempted.append(path)
                raise OSError("controlled unlink rejection")
            real_unlink(path, dir_fd=dir_fd)

        monkeypatch.setattr(acquisition_module.os, "unlink", fail_temp_unlink)
        with pytest.raises(ExceptionGroup) as raised:
            _command(conn, root, raw_id, transport=fake)

        assert cleanup_attempted
        assert any(isinstance(error, primary_type) for error in raised.value.exceptions)
        cleanup_errors = [
            error
            for error in raised.value.exceptions
            if isinstance(error, acquisition_module.TemporaryFileCleanupError)
        ]
        assert len(cleanup_errors) == 1
        assert cleanup_errors[0].residue_path is not None
        assert Path(cleanup_errors[0].residue_path).exists()
        assert "untrusted" not in str(raised.value)
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
        assert conn.in_transaction is False
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    def test_cleanup_root_sync_failure_is_visible_after_successful_unlink(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        fake = FakeTransport(metadata_error=RuntimeError("metadata failed"))
        real_unlink = acquisition_module.os.unlink
        real_fsync = acquisition_module.os.fsync
        unlinked = False

        def observe_unlink(path: str, *, dir_fd: int | None = None) -> None:
            nonlocal unlinked
            real_unlink(path, dir_fd=dir_fd)
            if path.startswith(".telegram-acquisition-"):
                unlinked = True

        def fail_cleanup_sync(fd: int) -> None:
            if unlinked:
                raise OSError("controlled cleanup sync rejection")
            real_fsync(fd)

        monkeypatch.setattr(acquisition_module.os, "unlink", observe_unlink)
        monkeypatch.setattr(acquisition_module.os, "fsync", fail_cleanup_sync)
        with pytest.raises(ExceptionGroup) as raised:
            _command(conn, root, raw_id, transport=fake)

        assert any(
            isinstance(error, acquisition_module.TelegramMetadataRequestError)
            for error in raised.value.exceptions
        )
        cleanup = next(
            error
            for error in raised.value.exceptions
            if isinstance(error, acquisition_module.TemporaryFileCleanupError)
        )
        assert cleanup.residue_path is None
        assert _temp_files(root) == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0

    def test_cleanup_failure_during_system_exit_preserves_both_base_exceptions(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)

        def cancel_after_temp(stage: str) -> None:
            if stage == "after_temporary_file_creation":
                raise SystemExit("controlled cancellation")

        def fail_unlink(path: str, *, dir_fd: int | None = None) -> None:
            raise OSError("controlled cancellation cleanup rejection")

        monkeypatch.setattr(acquisition_module, "_failure_injection_hook", cancel_after_temp)
        monkeypatch.setattr(acquisition_module.os, "unlink", fail_unlink)
        with pytest.raises(BaseExceptionGroup) as raised:
            _command(conn, root, raw_id, transport=FakeTransport())

        assert any(isinstance(error, SystemExit) for error in raised.value.exceptions)
        assert any(
            isinstance(error, acquisition_module.TemporaryFileCleanupError)
            for error in raised.value.exceptions
        )
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
        assert conn.in_transaction is False


class TestConcurrency:
    @staticmethod
    def _run_process_pair(
        db_path: Path,
        root: Path,
        raw_id: int,
        commands: list[tuple[str, str, bytes]],
    ) -> list[dict[str, Any]]:
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        outcomes = context.Queue()

        def worker(command: tuple[str, str, bytes]) -> None:
            file_id, unique_id, body = command
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            transport = FakeTransport(
                body=body,
                metadata=TelegramFileMetadata(file_path="documents/file.pdf", file_size=len(body)),
                response=FakeResponse(
                    body,
                    headers={
                        "Content-Length": str(len(body)),
                        "Content-Type": "application/pdf",
                    },
                ),
            )
            try:
                barrier.wait(timeout=5)
                result = _command(
                    conn,
                    root,
                    raw_id,
                    transport=transport,
                    public_id="tgae_cross_process",
                    telegram_file_id=file_id,
                    telegram_file_unique_id=unique_id,
                )
                outcomes.put(
                    {
                        "kind": "result",
                        "idempotent": result.persistence_idempotent,
                        "metadata_calls": transport.metadata_calls,
                        "download_calls": transport.download_calls,
                    }
                )
            except BaseException as exc:
                outcomes.put(
                    {
                        "kind": "error",
                        "type": type(exc).__name__,
                        "metadata_calls": transport.metadata_calls,
                        "download_calls": transport.download_calls,
                    }
                )
            finally:
                conn.close()

        processes = [context.Process(target=worker, args=(command,)) for command in commands]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert not process.is_alive()
            assert process.exitcode == 0
            process.close()
        results = [outcomes.get(timeout=2), outcomes.get(timeout=2)]
        outcomes.close()
        outcomes.join_thread()
        return results

    def test_cross_process_same_command_has_one_download_and_idempotent_replay(
        self, migrated_temp_db_path: Path, tmp_path: Path
    ) -> None:
        root = _private_storage(tmp_path)
        setup = sqlite3.connect(migrated_temp_db_path)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        raw_id = _insert_raw_intake(setup)
        setup.close()

        outcomes = self._run_process_pair(
            migrated_temp_db_path,
            root,
            raw_id,
            [("file-id", "unique-id", PDF_BYTES), ("file-id", "unique-id", PDF_BYTES)],
        )

        assert [outcome["kind"] for outcome in outcomes].count("result") == 2
        assert sorted(outcome["idempotent"] for outcome in outcomes) == [False, True]
        assert sum(outcome["metadata_calls"] for outcome in outcomes) == 1
        assert sum(outcome["download_calls"] for outcome in outcomes) == 1
        assert len(list(root.glob("*/*"))) == 1
        assert _temp_files(root) == []

        verify = sqlite3.connect(migrated_temp_db_path, timeout=1)
        try:
            assert (
                verify.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
            assert verify.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
            verify.execute("BEGIN IMMEDIATE")
            verify.rollback()
        finally:
            verify.close()

    def test_cross_process_conflicting_command_has_one_file_and_explicit_conflict(
        self, migrated_temp_db_path: Path, tmp_path: Path
    ) -> None:
        root = _private_storage(tmp_path)
        setup = sqlite3.connect(migrated_temp_db_path)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        raw_id = _insert_raw_intake(setup)
        setup.close()
        other_pdf = b"%PDF-1.7\n% conflicting acquisition\n"

        outcomes = self._run_process_pair(
            migrated_temp_db_path,
            root,
            raw_id,
            [
                ("file-one", "unique-one", PDF_BYTES),
                ("file-two", "unique-two", other_pdf),
            ],
        )

        successes = [outcome for outcome in outcomes if outcome["kind"] == "result"]
        errors = [outcome for outcome in outcomes if outcome["kind"] == "error"]
        assert len(successes) == 1
        assert successes[0]["idempotent"] is False
        assert len(errors) == 1
        assert errors[0]["type"] == "AcquisitionReplayConflictError"
        assert sum(outcome["metadata_calls"] for outcome in outcomes) == 1
        assert sum(outcome["download_calls"] for outcome in outcomes) == 1
        assert len(list(root.glob("*/*"))) == 1
        assert _temp_files(root) == []

        verify = sqlite3.connect(migrated_temp_db_path, timeout=1)
        try:
            assert (
                verify.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 1
            )
            assert verify.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
            verify.execute("BEGIN IMMEDIATE")
            verify.rollback()
        finally:
            verify.close()

    def test_same_public_id_is_serialized_to_one_download_and_one_file(
        self, migrated_temp_db_path: Path, tmp_path: Path
    ) -> None:
        root = _private_storage(tmp_path)
        setup = sqlite3.connect(migrated_temp_db_path)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        raw_id = _insert_raw_intake(setup)
        setup.close()
        barrier = threading.Barrier(2)
        transports = [FakeTransport(), FakeTransport()]

        def worker(index: int):
            conn = sqlite3.connect(migrated_temp_db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait()
                return _command(conn, root, raw_id, transport=transports[index])
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, [0, 1]))
        assert sorted(result.persistence_idempotent for result in results) == [False, True]
        assert sum(transport.metadata_calls for transport in transports) == 1
        assert len(list(root.glob("*/*"))) == 1
        assert _temp_files(root) == []

    def test_same_public_id_different_command_has_one_success_and_one_conflict(
        self, migrated_temp_db_path: Path, tmp_path: Path
    ) -> None:
        root = _private_storage(tmp_path)
        setup = sqlite3.connect(migrated_temp_db_path)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        raw_id = _insert_raw_intake(setup)
        setup.close()
        barrier = threading.Barrier(2)

        def worker(index: int):
            conn = sqlite3.connect(migrated_temp_db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait()
                return _command(
                    conn,
                    root,
                    raw_id,
                    transport=FakeTransport(),
                    telegram_file_id=f"file-{index}",
                    telegram_file_unique_id=f"unique-{index}",
                )
            except Exception as exc:
                return exc
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, [0, 1]))
        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        assert len(conflicts) == 1
        assert isinstance(conflicts[0], AcquisitionReplayConflictError)
        assert len(list(root.glob("*/*"))) == 1
        assert _temp_files(root) == []

    def test_different_public_ids_same_telegram_content_publish_one_file(
        self, migrated_temp_db_path: Path, tmp_path: Path
    ) -> None:
        root = _private_storage(tmp_path)
        setup = sqlite3.connect(migrated_temp_db_path)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        raw_ids = [
            _insert_raw_intake(setup, "raw_concurrent_one"),
            _insert_raw_intake(setup, "raw_concurrent_two"),
        ]
        setup.close()
        barrier = threading.Barrier(2)

        def worker(index: int):
            conn = sqlite3.connect(migrated_temp_db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait()
                return _command(
                    conn,
                    root,
                    raw_ids[index],
                    transport=FakeTransport(),
                    public_id=f"tgae_concurrent_{index}",
                    telegram_file_unique_id="shared-unique-id",
                )
            except Exception as exc:
                return exc
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, [0, 1]))
        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        assert len(conflicts) == 1
        assert isinstance(conflicts[0], AttachmentEvidenceHandoffConflictError)
        assert len(list(root.glob("*/*"))) == 1
        assert _temp_files(root) == []


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


class FakeHeaders:
    def __init__(self, values: list[tuple[str, str]]) -> None:
        self.values = values

    def keys(self) -> list[str]:
        return list(dict(self.values))

    def get_all(self, name: str) -> list[str]:
        return [value for key, value in self.values if key == name]

    def __getitem__(self, name: str) -> str:
        for key, value in self.values:
            if key == name:
                return value
        raise KeyError(name)


class FakeHttpResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = FakeHeaders(headers or [])
        self.position = 0
        self.read_sizes: list[int] = []
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        result = self.body[self.position : self.position + size]
        self.position += len(result)
        return result

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses: list[Any] | None = None, error=None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def open(self, request, *, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class BlockingOpener:
    def __init__(self, started: Any, release: Any) -> None:
        self.started = started
        self.release = release

    def open(self, request: Any, *, timeout: float) -> Any:
        self.started.set()
        self.release.wait()
        raise AssertionError("blocking opener must be terminated")


class BlockingPhaseHttpResponse:
    def __init__(
        self,
        phase: str,
        started: Any,
        release: Any,
        *,
        body: bytes = PDF_BYTES,
    ) -> None:
        self.phase = phase
        self.started = started
        self.release = release
        self.body = body
        self._headers = FakeHeaders(
            [
                ("Content-Length", str(len(body))),
                ("Content-Type", "application/pdf"),
            ]
        )

    @property
    def headers(self) -> FakeHeaders:
        if self.phase == "headers":
            self.started.set()
            while not self.release.wait(0.005):
                pass
        return self._headers

    def getcode(self) -> int:
        return 200

    def read(self, size: int) -> bytes:
        if self.phase == "body":
            self.started.set()
            while not self.release.wait(0.005):
                pass
        if self.phase == "close":
            return b""
        return self.body[:size]

    def close(self) -> None:
        if self.phase == "close":
            self.started.set()
            while not self.release.wait(0.005):
                pass


class EndlessSmallReadResponse:
    def __init__(self) -> None:
        self.headers = FakeHeaders([("Content-Type", "application/pdf")])
        self.position = 0

    def getcode(self) -> int:
        return 200

    def read(self, size: int) -> bytes:
        prefix = b"%PDF-"
        if self.position < len(prefix):
            result = prefix[self.position : self.position + 1]
        else:
            result = b"x"
        self.position += 1
        return result

    def close(self) -> None:
        return None


class RoutingOpener:
    def __init__(self, download_response: Any) -> None:
        self.download_response = download_response

    def open(self, request: Any, *, timeout: float) -> Any:
        if "/getFile?" in request.full_url:
            payload = json.dumps(
                {
                    "ok": True,
                    "result": {
                        "file_path": "documents/file.pdf",
                        "file_size": None,
                        "file_id": "file-id-1",
                        "file_unique_id": "unique-id-1",
                    },
                }
            ).encode()
            return FakeHttpResponse(payload, headers=[("Content-Type", "application/json")])
        return self.download_response


def _active_telegram_workers() -> list[Any]:
    return [
        process
        for process in multiprocessing.active_children()
        if process.name == transport_module._WORKER_NAME
    ]


class TestConcreteTelegramTransport:
    TOKEN = "123456:SECRET_TOKEN_VALUE"

    def test_get_file_request_is_https_host_bounded_and_strictly_encoded(self) -> None:
        payload = json.dumps(
            {
                "ok": True,
                "result": {
                    "file_path": "documents/file.pdf",
                    "file_size": len(PDF_BYTES),
                    "file_id": "file/id+",
                    "file_unique_id": "unique-id",
                },
            }
        ).encode()
        response = FakeHttpResponse(payload, headers=[("Content-Type", "application/json")])
        opener = FakeOpener([response])
        transport = TelegramBotApiTransport(
            self.TOKEN, _opener=opener, _unsafe_inline_for_tests=True
        )
        result = transport.get_file_metadata(
            "file/id+",
            timeout_seconds=7.5,
            max_response_bytes=65_536,
            max_header_bytes=16_384,
        )
        request_url = opener.requests[0].full_url
        parsed = urlsplit(request_url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "api.telegram.org"
        assert parse_qs(parsed.query) == {"file_id": ["file/id+"]}
        assert "file%2Fid%2B" in request_url
        assert result.file_path == "documents/file.pdf"
        assert response.closed is True
        assert opener.timeouts == [7.5]

    def test_download_url_is_constructed_internally_and_streamed(self) -> None:
        response = FakeHttpResponse(
            PDF_BYTES,
            headers=[
                ("Content-Length", str(len(PDF_BYTES))),
                ("Content-Type", "application/pdf"),
            ],
        )
        opener = FakeOpener([response])
        transport = TelegramBotApiTransport(
            self.TOKEN, _opener=opener, _unsafe_inline_for_tests=True
        )
        download = transport.open_file_download(
            "documents/my file.pdf", timeout_seconds=5, max_header_bytes=16_384
        )
        request_url = opener.requests[0].full_url
        parsed = urlsplit(request_url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "api.telegram.org"
        assert parsed.path.endswith("/documents/my%20file.pdf")
        assert download.read(5, timeout_seconds=4) == PDF_BYTES[:5]
        assert response.read_sizes == [5]
        download.close()
        assert response.closed is True

    def test_token_is_redacted_from_repr_and_wrapped_errors(self) -> None:
        opener = FakeOpener(error=RuntimeError(f"network failed at {self.TOKEN}"))
        transport = TelegramBotApiTransport(
            self.TOKEN, _opener=opener, _unsafe_inline_for_tests=True
        )
        assert self.TOKEN not in repr(transport)
        with pytest.raises(Exception) as raised:
            transport.get_file_metadata(
                "file-id",
                timeout_seconds=5,
                max_response_bytes=1_000,
                max_header_bytes=1_000,
            )
        assert self.TOKEN not in str(raised.value)
        assert self.TOKEN not in repr(raised.value)
        assert raised.value.__cause__ is None

    @pytest.mark.parametrize(
        "body,status,error",
        [
            (b"not-json", 200, MalformedTelegramMetadataError),
            (b'{"ok":false}', 200, MalformedTelegramMetadataError),
            (b'{"ok":true,"result":{}}', 200, MalformedTelegramMetadataError),
            (
                b'{"ok":true,"result":{"file_path":"x","file_size":-1}}',
                200,
                MalformedTelegramMetadataError,
            ),
            (b"server failure", 500, TelegramAttachmentAcquisitionError),
        ],
    )
    def test_metadata_failures_are_sanitized(self, body, status, error) -> None:
        response = FakeHttpResponse(body, status=status)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([response]),
            _unsafe_inline_for_tests=True,
        )
        with pytest.raises(error) as raised:
            transport.get_file_metadata(
                "file-id",
                timeout_seconds=5,
                max_response_bytes=1_000,
                max_header_bytes=1_000,
            )
        assert self.TOKEN not in str(raised.value)
        assert body.decode(errors="ignore") not in str(raised.value)
        assert response.closed is True

    def test_oversized_metadata_is_bounded_and_closed(self) -> None:
        response = FakeHttpResponse(b"x" * 11)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([response]),
            _unsafe_inline_for_tests=True,
        )
        with pytest.raises(MalformedTelegramMetadataError, match="byte limit"):
            transport.get_file_metadata(
                "file-id",
                timeout_seconds=5,
                max_response_bytes=10,
                max_header_bytes=1_000,
            )
        assert response.read_sizes == [11]
        assert response.closed is True

    def test_duplicate_content_length_and_large_headers_fail_closed(self) -> None:
        duplicate = FakeHttpResponse(
            PDF_BYTES,
            headers=[("Content-Length", "1"), ("Content-Length", "2")],
        )
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([duplicate]),
            _unsafe_inline_for_tests=True,
        )
        response = transport.open_file_download(
            "documents/file.pdf", timeout_seconds=5, max_header_bytes=1_000
        )
        assert response.headers["content-length"] == "1,2"
        response.close()

        oversized = FakeHttpResponse(PDF_BYTES, headers=[("X-Large", "x" * 100)])
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([oversized]),
            _unsafe_inline_for_tests=True,
        )
        with pytest.raises(TelegramHttpResponseError, match="headers"):
            transport.open_file_download(
                "documents/file.pdf", timeout_seconds=5, max_header_bytes=10
            )
        assert oversized.closed is True

    def test_metadata_open_block_is_terminated_at_absolute_deadline(self) -> None:
        context = multiprocessing.get_context("fork")
        started = context.Event()
        release = context.Event()
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=BlockingOpener(started, release),
            _process_context=context,
        )
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            transport.get_file_metadata(
                "file-id",
                timeout_seconds=0.08,
                max_response_bytes=1_000,
                max_header_bytes=1_000,
            )
        elapsed = time.monotonic() - began
        assert started.is_set()
        assert elapsed < 1
        assert _active_telegram_workers() == []

    @pytest.mark.parametrize("phase", ["headers", "body", "close"])
    def test_metadata_trickle_or_close_is_terminated_at_absolute_deadline(self, phase: str) -> None:
        context = multiprocessing.get_context("fork")
        started = context.Event()
        release = context.Event()
        response = BlockingPhaseHttpResponse(phase, started, release)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([response]),
            _process_context=context,
        )
        with pytest.raises(TimeoutError):
            transport.get_file_metadata(
                "file-id",
                timeout_seconds=0.08,
                max_response_bytes=1_000,
                max_header_bytes=1_000,
            )
        assert started.is_set()
        assert _active_telegram_workers() == []

    @pytest.mark.parametrize("phase", ["open", "headers"])
    def test_download_open_or_headers_is_terminated_at_absolute_deadline(self, phase: str) -> None:
        context = multiprocessing.get_context("fork")
        started = context.Event()
        release = context.Event()
        opener: Any
        if phase == "open":
            opener = BlockingOpener(started, release)
        else:
            opener = FakeOpener([BlockingPhaseHttpResponse("headers", started, release)])
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=opener,
            _process_context=context,
        )
        with pytest.raises(TimeoutError):
            transport.open_file_download(
                "documents/file.pdf", timeout_seconds=0.08, max_header_bytes=1_000
            )
        assert started.is_set()
        assert _active_telegram_workers() == []

    @pytest.mark.parametrize("phase", ["body", "close"])
    def test_blocked_download_read_or_close_is_terminated_and_redacted(
        self,
        phase: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        context = multiprocessing.get_context("fork")
        started = context.Event()
        release = context.Event()
        audit_messages = context.Queue()

        def audit(message: tuple[Any, ...]) -> None:
            audit_messages.put(repr(message))

        monkeypatch.setattr(transport_module, "_worker_message_audit_hook", audit)
        response = BlockingPhaseHttpResponse(phase, started, release)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([response]),
            _process_context=context,
        )
        download = transport.open_file_download(
            "documents/file.pdf", timeout_seconds=0.12, max_header_bytes=1_000
        )
        with pytest.raises(TimeoutError) as raised:
            download.read(16, timeout_seconds=0.08)
        assert started.is_set()
        assert _active_telegram_workers() == []

        messages: list[str] = []
        while True:
            try:
                messages.append(audit_messages.get(timeout=0.05))
            except queue.Empty:
                break
        audit_messages.close()
        audit_messages.join_thread()
        token_url_fragment = f"/bot{self.TOKEN}"
        inspected = [
            str(raised.value),
            repr(raised.value),
            str(raised.value.__cause__),
            repr(transport),
            caplog.text,
            *messages,
        ]
        assert messages
        assert all(self.TOKEN not in value for value in inspected)
        assert all(token_url_fragment not in value for value in inspected)

    def test_endless_small_chunks_expire_public_deadline_without_side_effects(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=RoutingOpener(EndlessSmallReadResponse()),
        )
        limits = TelegramAttachmentAcquisitionLimits(
            total_timeout_seconds=0.15,
            download_chunk_bytes=1,
        )
        with pytest.raises(TelegramDownloadTimeoutError):
            _command(conn, root, raw_id, transport=transport, limits=limits)  # type: ignore[arg-type]
        assert _active_telegram_workers() == []
        assert _temp_files(root) == []
        assert list(root.glob("*/*")) == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert conn.in_transaction is False

    def test_timeout_plus_cleanup_failure_preserves_sanitized_errors(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = multiprocessing.get_context("fork")
        started = context.Event()
        release = context.Event()
        conn = migrated_temp_db_connection
        raw_id = _insert_raw_intake(conn)
        root = _private_storage(tmp_path)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=BlockingOpener(started, release),
            _process_context=context,
        )

        def fail_unlink(path: str, *, dir_fd: int | None = None) -> None:
            raise OSError("controlled cleanup rejection")

        monkeypatch.setattr(acquisition_module.os, "unlink", fail_unlink)
        with pytest.raises(ExceptionGroup) as raised:
            _command(
                conn,
                root,
                raw_id,
                transport=transport,  # type: ignore[arg-type]
                limits=TelegramAttachmentAcquisitionLimits(total_timeout_seconds=0.08),
            )
        assert started.is_set()
        assert any(
            isinstance(error, TelegramDownloadTimeoutError) for error in raised.value.exceptions
        )
        assert any(
            isinstance(error, acquisition_module.TemporaryFileCleanupError)
            for error in raised.value.exceptions
        )
        inspected = [str(raised.value), repr(raised.value)]
        for error in raised.value.exceptions:
            inspected.extend([str(error), repr(error), str(error.__cause__)])
        assert all(self.TOKEN not in value for value in inspected)
        assert _active_telegram_workers() == []
        assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0
        assert conn.in_transaction is False

    def test_cancellable_process_transport_completes_normal_responses(self) -> None:
        response = FakeHttpResponse(
            PDF_BYTES,
            headers=[
                ("Content-Length", str(len(PDF_BYTES))),
                ("Content-Type", "application/pdf"),
            ],
        )
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=RoutingOpener(response),
        )
        metadata = transport.get_file_metadata(
            "file-id-1",
            timeout_seconds=1,
            max_response_bytes=1_000,
            max_header_bytes=1_000,
        )
        assert metadata.file_path == "documents/file.pdf"
        download = transport.open_file_download(
            metadata.file_path,
            timeout_seconds=1,
            max_header_bytes=1_000,
        )
        chunks = []
        while True:
            chunk = download.read(5, timeout_seconds=0.5)
            if not chunk:
                break
            chunks.append(chunk)
        download.close()
        assert b"".join(chunks) == PDF_BYTES
        assert _active_telegram_workers() == []

    def test_token_and_returned_identity_length_limits_do_not_echo_values(self) -> None:
        oversized_token = "1:" + "S" * 255
        with pytest.raises(InvalidAcquisitionConfigurationError) as token_error:
            TelegramBotApiTransport(oversized_token)
        assert oversized_token not in str(token_error.value)

        oversized_identity = "f" * 513
        payload = json.dumps(
            {
                "ok": True,
                "result": {
                    "file_path": "documents/file.pdf",
                    "file_id": oversized_identity,
                },
            }
        ).encode()
        response = FakeHttpResponse(payload)
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=FakeOpener([response]),
            _unsafe_inline_for_tests=True,
        )
        with pytest.raises(MalformedTelegramMetadataError) as identity_error:
            transport.get_file_metadata(
                "file-id",
                timeout_seconds=1,
                max_response_bytes=2_000,
                max_header_bytes=1_000,
            )
        assert oversized_identity not in str(identity_error.value)

    def test_cancellable_transport_rejects_multithreaded_fork_callers(self) -> None:
        transport = TelegramBotApiTransport(
            self.TOKEN,
            _opener=RoutingOpener(FakeHttpResponse(PDF_BYTES)),
        )

        def call_from_thread() -> Exception:
            try:
                transport.get_file_metadata(
                    "file-id",
                    timeout_seconds=1,
                    max_response_bytes=1_000,
                    max_header_bytes=1_000,
                )
            except Exception as exc:
                return exc
            raise AssertionError("multithreaded fork caller unexpectedly succeeded")

        with ThreadPoolExecutor(max_workers=1) as pool:
            error = pool.submit(call_from_thread).result(timeout=2)
        assert isinstance(error, InvalidAcquisitionConfigurationError)
        assert "single-threaded POSIX" in str(error)
        assert _active_telegram_workers() == []

    def test_default_opener_disables_environment_proxies_and_rejects_redirects(self) -> None:
        opener = transport_module._build_secure_opener()
        handlers = getattr(opener, "handlers")
        proxy_handlers = [
            handler for handler in handlers if isinstance(handler, transport_module.ProxyHandler)
        ]
        redirect_handlers = [
            handler
            for handler in handlers
            if isinstance(handler, transport_module._RejectRedirectHandler)
        ]
        # Passing ProxyHandler({}) suppresses urllib's environment-derived
        # default handler.  With no configured schemes it is intentionally
        # omitted from the final handler chain, leaving only direct HTTPS.
        assert proxy_handlers == []
        assert len(redirect_handlers) == 1


def test_public_error_taxonomy_is_stable() -> None:
    for error in [
        InvalidAcquisitionConfigurationError,
        InvalidTelegramIdentityError,
        StagingDatabaseRejectedError,
        CallerOwnedTransactionError,
        UnsafeStorageRootError,
        TelegramDownloadTimeoutError,
        TelegramDeclaredFileTooLargeError,
        TelegramStreamedFileTooLargeError,
        TelegramTruncatedDownloadError,
        UnsupportedMimeTypeError,
        UnsupportedFilenameExtensionError,
        ContentSignatureMismatchError,
        DurablePublicationError,
        AcquisitionReplayConflictError,
        AcquisitionReplayIntegrityError,
        UnexpectedAttachmentPersistenceError,
    ]:
        assert issubclass(error, TelegramAttachmentAcquisitionError)
