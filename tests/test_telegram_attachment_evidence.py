"""Telegram attachment evidence persistence boundary tests.

Covers public API, file validation, stable content identity, idempotency,
conflict detection, immutability, atomic rollback, concurrency,
expected-identity checks, and migration integrity.

No live database access.  All tests use temporary databases only.
"""

from __future__ import annotations

import hashlib
import os as _os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from finance_core.intake.attachment_evidence import (
    AttachmentEvidenceConflictError,
    AttachmentEvidenceError,
    AttachmentEvidencePersistenceError,
    AttachmentExpectedHashMismatchError,
    AttachmentExpectedSizeMismatchError,
    AttachmentFileChangedDuringHashError,
    AttachmentFileNotFoundError,
    AttachmentFileUnreadableError,
    AttachmentRawIntakeConflictError,
    InvalidAttachmentIdentityError,
    get_attachment_evidence,
    persist_attachment_evidence,
)
from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    verify_migration_history,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _insert_raw_intake(conn: sqlite3.Connection, public_id: str | None = None) -> int:
    from uuid import uuid4

    pid = public_id or f"raw_intake_{uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, source_channel, raw_input, received_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            pid,
            "telegram_text",
            "telegram",
            "Test raw input",
            "2026-07-19T10:00:00+00:00",
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _make_test_file(tmp_path: Path, name: str, content: bytes) -> Path:
    file_path = tmp_path / name
    file_path.write_bytes(content)
    return file_path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _unique_public_id() -> str:
    from uuid import uuid4

    return f"tgae_{uuid4().hex[:16]}"


def _count_source_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM telegram_attachment_source").fetchone()["c"])


def _count_evidence_rows(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM raw_intake_evidence WHERE evidence_type = 'attachment'"
        ).fetchone()["c"]
    )


def _count_attachment_rows(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM attachments WHERE source_channel = 'telegram'"
        ).fetchone()["c"]
    )


def _foreign_key_ok(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return len(rows) == 0


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] == "ok"


# ---------------------------------------------------------------------------
# Public API and import
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_persist_attachment_evidence_is_importable(self) -> None:
        assert callable(persist_attachment_evidence)

    def test_get_attachment_evidence_is_importable(self) -> None:
        assert callable(get_attachment_evidence)

    def test_error_classes_are_importable(self) -> None:
        for cls in [
            AttachmentEvidenceError,
            AttachmentFileNotFoundError,
            AttachmentFileUnreadableError,
            AttachmentFileChangedDuringHashError,
            AttachmentEvidenceConflictError,
            AttachmentEvidencePersistenceError,
            AttachmentRawIntakeConflictError,
            InvalidAttachmentIdentityError,
            AttachmentExpectedSizeMismatchError,
            AttachmentExpectedHashMismatchError,
        ]:
            assert issubclass(cls, AttachmentEvidenceError)


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------


class TestFileValidation:
    def test_nonexistent_path_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        nonexistent = tmp_path / "does_not_exist.txt"
        with pytest.raises(AttachmentFileNotFoundError, match="does not exist"):
            persist_attachment_evidence(
                conn, nonexistent, raw_intake_id=1, public_id=_unique_public_id()
            )

    def test_directory_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        with pytest.raises(AttachmentFileNotFoundError, match="not a regular file"):
            persist_attachment_evidence(
                conn, subdir, raw_intake_id=1, public_id=_unique_public_id()
            )

    def test_unreadable_file_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        f = _make_test_file(tmp_path, "unreadable.bin", b"secret")
        f.chmod(0o000)
        try:
            with pytest.raises(AttachmentFileUnreadableError, match="not readable"):
                persist_attachment_evidence(
                    conn,
                    f,
                    raw_intake_id=1,
                    public_id=_unique_public_id(),
                )
        finally:
            f.chmod(0o644)

    def test_relative_path_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        with pytest.raises(AttachmentFileNotFoundError, match="must be absolute"):
            persist_attachment_evidence(
                conn,
                "relative/path.txt",
                raw_intake_id=1,
                public_id=_unique_public_id(),
            )


# ---------------------------------------------------------------------------
# Original path preservation
# ---------------------------------------------------------------------------


class TestOriginalPathPreservation:
    def test_original_path_preserved(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"path preservation test"
        f = _make_test_file(tmp_path, "original_path.txt", content)

        pid = _unique_public_id()
        persist_attachment_evidence(conn, str(f), raw_intake_id=rid, public_id=pid)

        row = get_attachment_evidence(conn, public_id=pid)
        assert row is not None
        assert row["original_attachment_path"] == str(f)
        # attachments.file_path should also store the original path
        att = conn.execute(
            "SELECT file_path FROM attachments WHERE id = ?",
            (row["attachment_id"],),
        ).fetchone()
        assert att is not None
        assert att["file_path"] == str(f)


# ---------------------------------------------------------------------------
# Public ID validation
# ---------------------------------------------------------------------------


class TestPublicIdValidation:
    def test_missing_public_id_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "missing_id.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="must not be empty"):
            persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id="")

    def test_whitespace_public_id_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "ws_id.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="must not be empty"):
            persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id="   ")

    def test_leading_whitespace_in_public_id_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "lead_ws.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError):
            persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=" tgae_test")

    def test_wrong_prefix_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "wrong_pre.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="must start with 'tgae_'"):
            persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id="pco_wrong")


# ---------------------------------------------------------------------------
# Successful persistence
# ---------------------------------------------------------------------------


class TestSuccessfulPersistence:
    def test_basic_text_file(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"Hello from Telegram attachment\n"
        f = _make_test_file(tmp_path, "receipt.txt", content)
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        assert result["idempotent"] is False
        assert result["public_id"] == pid
        assert result["observed_file_size"] == len(content)
        assert result["content_hash"] == _sha256(content)
        assert _count_source_rows(conn) == 1
        assert _count_evidence_rows(conn) == 1
        assert _count_attachment_rows(conn) == 1
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_with_all_optional_fields(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"Receipt image content"
        f = _make_test_file(tmp_path, "receipt.png", content)
        pid = _unique_public_id()

        result = persist_attachment_evidence(
            conn,
            f,
            raw_intake_id=rid,
            public_id=pid,
            telegram_file_id="tg_file_abc123",
            telegram_file_unique_id="unq_def456",
            original_filename="receipt.png",
            declared_mime_type="image/png",
        )

        assert result["idempotent"] is False
        assert result["public_id"] == pid
        assert result["telegram_file_id"] == "tg_file_abc123"
        assert result["telegram_file_unique_id"] == "unq_def456"
        assert result["original_filename"] == "receipt.png"
        assert result["declared_mime_type"] == "image/png"

    def test_empty_file(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "empty.txt", b"")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["observed_file_size"] == 0
        assert result["content_hash"] == _sha256(b"")

    def test_large_file_hash_correctness(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        # larger than one read buffer (64KB)
        content = b"A" * 131072
        f = _make_test_file(tmp_path, "large.bin", content)
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["content_hash"] == _sha256(content)
        assert result["observed_file_size"] == 131072

    def test_raw_intake_linked_to_attachment(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "link.txt", b"link test")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        row = conn.execute(
            "SELECT attachment_id FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row is not None
        assert row["attachment_id"] == result["attachment_id"]

    def test_source_evidence_payload_is_canonical_json(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "json.txt", b"json test")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        row = conn.execute(
            "SELECT source_evidence_payload FROM telegram_attachment_source WHERE public_id = ?",
            (pid,),
        ).fetchone()
        assert row is not None
        import json

        payload = json.loads(row["source_evidence_payload"])
        assert "content_hash" in payload
        assert "original_attachment_path" in payload


# ---------------------------------------------------------------------------
# Stable file identity
# ---------------------------------------------------------------------------


class TestStableFileIdentity:
    def test_different_content_produces_different_hash(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f1 = _make_test_file(tmp_path, "a.txt", b"content A")
        r1 = persist_attachment_evidence(conn, f1, raw_intake_id=rid, public_id=_unique_public_id())

        rid2 = _insert_raw_intake(conn)
        f2 = _make_test_file(tmp_path, "b.txt", b"content B")
        r2 = persist_attachment_evidence(
            conn, f2, raw_intake_id=rid2, public_id=_unique_public_id()
        )

        assert r1["content_hash"] != r2["content_hash"]

    def test_file_changed_during_hash_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Verify _compute_stable_identity detects file changes via fstat."""
        f = _make_test_file(tmp_path, "changing.txt", b"content" * 1000 + b"end")

        # Patch os.fstat to simulate an inode change mid-read
        from unittest.mock import patch

        _real_fstat = _os.fstat
        _call_count = [0]

        def _changing_fstat(fd):
            _call_count[0] += 1
            stat_result = _real_fstat(fd)
            if _call_count[0] == 2:
                # On the second call (post-hash), return a different inode
                return _os.stat_result(
                    (
                        stat_result.st_mode,
                        99999999,  # different inode
                        stat_result.st_dev,
                        stat_result.st_nlink,
                        stat_result.st_uid,
                        stat_result.st_gid,
                        stat_result.st_size,
                        stat_result.st_atime,
                        stat_result.st_mtime,
                        stat_result.st_ctime,
                    )
                )
            return stat_result

        from finance_core.intake import attachment_evidence

        with patch.object(_os, "fstat", side_effect=_changing_fstat):
            with pytest.raises(
                AttachmentFileChangedDuringHashError,
                match="inode changed",
            ):
                attachment_evidence._compute_stable_identity(f)

    def test_hash_is_stable_on_repeated_reads(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        _insert_raw_intake(conn)
        content = b"stable content"
        _make_test_file(tmp_path, "stable.txt", content)

        h1 = _sha256(content)
        h2 = _sha256(content)
        assert h1 == h2


# ---------------------------------------------------------------------------
# Expected identity checks
# ---------------------------------------------------------------------------


class TestExpectedIdentity:
    def test_expected_size_mismatch_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "size_mismatch.txt", b"hello")
        pid = _unique_public_id()

        with pytest.raises(AttachmentExpectedSizeMismatchError, match="Expected"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=pid,
                expected_file_size=999,
            )

    def test_expected_hash_mismatch_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "hash_mismatch.txt", b"hello")
        pid = _unique_public_id()

        with pytest.raises(AttachmentExpectedHashMismatchError, match="Expected"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=pid,
                expected_content_hash="0" * 64,
            )

    def test_expected_size_and_hash_match_succeeds(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"expected match"
        f = _make_test_file(tmp_path, "match.txt", content)
        pid = _unique_public_id()

        result = persist_attachment_evidence(
            conn,
            f,
            raw_intake_id=rid,
            public_id=pid,
            expected_file_size=len(content),
            expected_content_hash=_sha256(content),
        )
        assert result["idempotent"] is False

    def test_malformed_expected_hash_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "badhash.txt", b"data")
        pid = _unique_public_id()

        with pytest.raises(InvalidAttachmentIdentityError, match="64 lowercase hex"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=pid,
                expected_content_hash="ABCD" + "0" * 60,
            )

    def test_negative_expected_size_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "negsize.txt", b"data")
        pid = _unique_public_id()

        with pytest.raises(InvalidAttachmentIdentityError, match="must not be negative"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=pid,
                expected_file_size=-1,
            )


# ---------------------------------------------------------------------------
# Idempotent replay
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    def test_same_public_id_same_material_idempotent(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"idempotent test content"
        f = _make_test_file(tmp_path, "idem.txt", content)
        pid = _unique_public_id()

        r1 = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert r1["idempotent"] is False

        r2 = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert r2["idempotent"] is True
        assert r2["content_hash"] == r1["content_hash"]
        assert r2["public_id"] == r1["public_id"]
        assert r2["id"] == r1["id"]

        assert _count_source_rows(conn) == 1
        assert _count_evidence_rows(conn) == 1

    def test_replay_after_reconnect(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"replay after reconnect"
        f = _make_test_file(tmp_path, "replay.txt", content)
        pid = _unique_public_id()

        r1 = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert r1["idempotent"] is False

        db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA foreign_keys = ON")
        try:
            r2 = persist_attachment_evidence(conn2, f, raw_intake_id=rid, public_id=pid)
            assert r2["idempotent"] is True
            assert r2["content_hash"] == r1["content_hash"]
        finally:
            conn2.close()


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_same_public_id_different_content_conflicts(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f1 = _make_test_file(tmp_path, "first.txt", b"first content")
        f2 = _make_test_file(tmp_path, "second.txt", b"different!!")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f1, raw_intake_id=rid, public_id=pid)

        with pytest.raises(AttachmentEvidenceConflictError, match="content_hash"):
            persist_attachment_evidence(conn, f2, raw_intake_id=rid, public_id=pid)

    def test_same_public_id_different_path_conflicts(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        content = b"same content"
        f1 = _make_test_file(tmp_path, "path_a.txt", content)
        f2 = _make_test_file(tmp_path, "path_b.txt", content)
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f1, raw_intake_id=rid, public_id=pid)

        with pytest.raises(AttachmentEvidenceConflictError, match="original_attachment_path"):
            persist_attachment_evidence(conn, f2, raw_intake_id=rid, public_id=pid)

    def test_same_public_id_different_raw_intake_conflicts(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid1 = _insert_raw_intake(conn)
        rid2 = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "shared.txt", b"same data")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid1, public_id=pid)

        with pytest.raises(AttachmentEvidenceConflictError, match="raw_intake_record_id"):
            persist_attachment_evidence(conn, f, raw_intake_id=rid2, public_id=pid)

    def test_telegram_file_unique_id_different_content_conflicts(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid1 = _insert_raw_intake(conn)
        rid2 = _insert_raw_intake(conn)
        f1 = _make_test_file(tmp_path, "tg1.txt", b"tg content A")
        f2 = _make_test_file(tmp_path, "tg2.txt", b"tg content B")

        persist_attachment_evidence(
            conn,
            f1,
            raw_intake_id=rid1,
            public_id=_unique_public_id(),
            telegram_file_unique_id="unq_dup_test",
        )

        with pytest.raises(AttachmentEvidenceConflictError, match="telegram_file_unique_id"):
            persist_attachment_evidence(
                conn,
                f2,
                raw_intake_id=rid2,
                public_id=_unique_public_id(),
                telegram_file_unique_id="unq_dup_test",
            )

    def test_same_content_from_different_sources(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Same bytes from different Telegram source messages — both source
        links preserved, attachments deduplicated."""
        conn = migrated_temp_db_connection
        content = b"shared content for two sources"
        f = _make_test_file(tmp_path, "shared_content.txt", content)

        rid1 = _insert_raw_intake(conn)
        pid1 = _unique_public_id()
        r1 = persist_attachment_evidence(
            conn,
            f,
            raw_intake_id=rid1,
            public_id=pid1,
            telegram_file_unique_id="unq_src_1",
            telegram_file_id="file_src_1",
        )

        rid2 = _insert_raw_intake(conn)
        pid2 = _unique_public_id()
        r2 = persist_attachment_evidence(
            conn,
            f,
            raw_intake_id=rid2,
            public_id=pid2,
            telegram_file_unique_id="unq_src_2",
            telegram_file_id="file_src_2",
        )

        # Both source rows exist
        assert _count_source_rows(conn) == 2
        # Content hash is the same
        assert r1["content_hash"] == r2["content_hash"]
        # They may or may not share an attachment row
        # (depends on whether file_path matches — in this case it does)
        assert r1["attachment_id"] == r2["attachment_id"]
        assert _count_attachment_rows(conn) == 1


# ---------------------------------------------------------------------------
# Missing / invalid raw_intake_id
# ---------------------------------------------------------------------------


class TestMissingRawIntakeId:
    def test_nonexistent_raw_intake_id_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        f = _make_test_file(tmp_path, "orphan.txt", b"orphan")

        with pytest.raises(AttachmentEvidenceError, match="not found"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=99999,
                public_id=_unique_public_id(),
            )


# ---------------------------------------------------------------------------
# Telegram identity validation
# ---------------------------------------------------------------------------


class TestTelegramIdentityValidation:
    def test_empty_telegram_file_id_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "tg_empty.txt", b"data")

        with pytest.raises(InvalidAttachmentIdentityError, match="must not be empty"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                telegram_file_id="   ",
            )

    def test_none_telegram_identity_is_ok(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "tg_none.txt", b"data")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["idempotent"] is False
        assert result["telegram_file_id"] is None
        assert result["telegram_file_unique_id"] is None


# ---------------------------------------------------------------------------
# Caller-owned transaction rejection
# ---------------------------------------------------------------------------


class TestTransactionOwnership:
    def test_rejects_caller_owned_transaction(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "tx.txt", b"data")

        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(AttachmentEvidenceError, match="pending work"):
                persist_attachment_evidence(
                    conn,
                    f,
                    raw_intake_id=rid,
                    public_id=_unique_public_id(),
                )
        finally:
            conn.rollback()


# ---------------------------------------------------------------------------
# Staging guard
# ---------------------------------------------------------------------------


class TestStagingGuard:
    def test_non_staging_database_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "non_staging.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        f = _make_test_file(tmp_path, "staging.txt", b"data")

        try:
            with pytest.raises(RuntimeError, match="staging database"):
                persist_attachment_evidence(
                    conn,
                    f,
                    raw_intake_id=1,
                    public_id=_unique_public_id(),
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# get_attachment_evidence lookup
# ---------------------------------------------------------------------------


class TestGetAttachmentEvidence:
    def test_lookup_by_evidence_id(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "lookup1.txt", b"lookup data")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        row = get_attachment_evidence(conn, evidence_id=result["id"])
        assert row is not None
        assert row["public_id"] == pid
        assert row["raw_intake_record_id"] == rid

    def test_lookup_by_public_id(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "lookup2.txt", b"lookup by pid")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        row = get_attachment_evidence(conn, public_id=pid)
        assert row is not None
        assert row["id"] == result["id"]

    def test_lookup_nonexistent_returns_none(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        assert get_attachment_evidence(conn, evidence_id=99999) is None
        assert get_attachment_evidence(conn, public_id="tgae_nonexistent") is None

    def test_lookup_without_args_raises(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        with pytest.raises(ValueError, match="evidence_id or public_id"):
            get_attachment_evidence(conn)


# ---------------------------------------------------------------------------
# Immutability (append-only enforcement)
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_source_rows_cannot_be_updated(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "immutable.txt", b"immutable data")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE telegram_attachment_source SET content_hash = ? WHERE public_id = ?",
                ("0" * 64, pid),
            )

    def test_source_rows_cannot_be_deleted(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "nodelete.txt", b"persistent data")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM telegram_attachment_source WHERE public_id = ?",
                (pid,),
            )

    def test_update_and_delete_change_no_rows(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "keep.txt", b"keep me")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        before_count = _count_source_rows(conn)

        # Both should raise, leaving row count unchanged
        try:
            conn.execute(
                "UPDATE telegram_attachment_source SET content_hash = ? WHERE public_id = ?",
                ("0" * 64, pid),
            )
        except sqlite3.IntegrityError:
            pass

        try:
            conn.execute(
                "DELETE FROM telegram_attachment_source WHERE public_id = ?",
                (pid,),
            )
        except sqlite3.IntegrityError:
            pass

        assert _count_source_rows(conn) == before_count


# ---------------------------------------------------------------------------
# Atomic rollback with failure injection
# ---------------------------------------------------------------------------


class TestAtomicRollback:
    def test_rollback_on_source_insert_failure(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "atomic1.txt", b"atomic test")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        _before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)

        # Simulate failure at the narrow _insert_telegram_source_row seam.
        with patch(
            "finance_core.intake.attachment_evidence._insert_telegram_source_row",
            side_effect=sqlite3.OperationalError("simulated insert failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated insert failure"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == _before_att
        assert _count_evidence_rows(conn) == before_ev
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_rollback_on_attachment_insert_failure(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Use a mock to simulate insert failure on attachments table."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "atomic_att.txt", b"rollback test")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        _before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)

        with patch(
            "finance_core.intake.attachment_evidence._resolve_attachment_row",
            side_effect=sqlite3.OperationalError("simulated failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == _before_att
        assert _count_evidence_rows(conn) == before_ev
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_connection_usable_after_rollback(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "recover.txt", b"recovery test")
        pid = _unique_public_id()

        with patch(
            "finance_core.intake.attachment_evidence._resolve_attachment_row",
            side_effect=sqlite3.OperationalError("simulated failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        # Connection should still work
        pid2 = _unique_public_id()
        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid2)
        assert result["idempotent"] is False
        assert _count_source_rows(conn) == 1


class TestConcurrency:
    def _tmp_db_path(self, migrated_temp_db_connection: sqlite3.Connection) -> Path:
        return Path(migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"])

    def test_same_public_id_same_material_one_winner(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        db_path = self._tmp_db_path(conn)
        rid = _insert_raw_intake(conn)
        content = b"concurrency same material"
        f = _make_test_file(tmp_path, "conc_same.txt", content)
        pid = _unique_public_id()

        results: list[dict] = []
        errors: list[Exception] = []

        def worker() -> None:
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            try:
                r = persist_attachment_evidence(c, f, raw_intake_id=rid, public_id=pid)
                results.append(r)
            except Exception as exc:
                errors.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(results) == 2
        non_idem = [r for r in results if not r.get("idempotent")]
        idem = [r for r in results if r.get("idempotent")]
        assert len(non_idem) == 1
        assert len(idem) == 1
        assert non_idem[0]["content_hash"] == idem[0]["content_hash"]

        # Reconnect to verify final state
        c2 = sqlite3.connect(str(db_path))
        c2.row_factory = sqlite3.Row
        c2.execute("PRAGMA foreign_keys = ON")
        try:
            assert (
                int(c2.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0])
                == 1
            )
            assert (
                int(
                    c2.execute(
                        "SELECT COUNT(*) FROM raw_intake_evidence "
                        "WHERE evidence_type = 'attachment'"
                    ).fetchone()[0]
                )
                == 1
            )
            assert _foreign_key_ok(c2)
            assert _integrity_ok(c2)
        finally:
            c2.close()

    def test_same_public_id_different_material_one_conflict(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        db_path = self._tmp_db_path(conn)
        rid = _insert_raw_intake(conn)
        f1 = _make_test_file(tmp_path, "conc_diff_a.txt", b"material A")
        f2 = _make_test_file(tmp_path, "conc_diff_b.txt", b"material B different")
        pid = _unique_public_id()

        results: list[dict] = []
        errors: list[Exception] = []

        def worker_a() -> None:
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            try:
                r = persist_attachment_evidence(c, f1, raw_intake_id=rid, public_id=pid)
                results.append(r)
            except Exception as exc:
                errors.append(exc)
            finally:
                c.close()

        def worker_b() -> None:
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            try:
                r = persist_attachment_evidence(c, f2, raw_intake_id=rid, public_id=pid)
                results.append(r)
            except Exception as exc:
                errors.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=worker_a)
        t2 = threading.Thread(target=worker_b)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            (AttachmentEvidenceConflictError,),
        )

    def test_different_sources_same_content(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        db_path = self._tmp_db_path(conn)
        content = b"same bytes two sources"
        f = _make_test_file(tmp_path, "shared_source.txt", content)

        rid1 = _insert_raw_intake(conn)
        rid2 = _insert_raw_intake(conn)
        pid1 = _unique_public_id()
        pid2 = _unique_public_id()

        results: list[dict] = []
        errors: list[Exception] = []

        def worker1() -> None:
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            try:
                r = persist_attachment_evidence(
                    c,
                    f,
                    raw_intake_id=rid1,
                    public_id=pid1,
                    telegram_file_unique_id="concur_src_1",
                )
                results.append(r)
            except Exception as exc:
                errors.append(exc)
            finally:
                c.close()

        def worker2() -> None:
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            try:
                r = persist_attachment_evidence(
                    c,
                    f,
                    raw_intake_id=rid2,
                    public_id=pid2,
                    telegram_file_unique_id="concur_src_2",
                )
                results.append(r)
            except Exception as exc:
                errors.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(results) == 2
        assert results[0]["content_hash"] == results[1]["content_hash"]

        # Two source rows, one attachment
        c2 = sqlite3.connect(str(db_path))
        c2.row_factory = sqlite3.Row
        c2.execute("PRAGMA foreign_keys = ON")
        try:
            assert (
                int(c2.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0])
                == 2
            )
            assert (
                int(
                    c2.execute(
                        "SELECT COUNT(*) FROM attachments WHERE file_hash = ?",
                        (results[0]["content_hash"],),
                    ).fetchone()[0]
                )
                == 1
            )
        finally:
            c2.close()


# ---------------------------------------------------------------------------
# Migration 031 replay and integrity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Symlink rejection
# ---------------------------------------------------------------------------


class TestSymlinkRejection:
    def test_symlink_is_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        real_file = _make_test_file(tmp_path, "real.txt", b"real content")
        symlink = tmp_path / "link.txt"
        symlink.symlink_to(real_file)

        with pytest.raises(AttachmentFileNotFoundError, match="Symlinks are not supported"):
            persist_attachment_evidence(
                conn, symlink, raw_intake_id=rid, public_id=_unique_public_id()
            )


# ---------------------------------------------------------------------------
# Telegram identity whitespace validation
# ---------------------------------------------------------------------------


class TestTelegramIdentityWhitespace:
    """Reject leading or trailing whitespace in telegram_file_id and
    telegram_file_unique_id."""

    def test_telegram_file_id_leading_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "lead.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                telegram_file_id=" bad_id",
            )

    def test_telegram_file_id_trailing_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "trail.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                telegram_file_id="bad_id ",
            )

    def test_telegram_file_unique_id_leading_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "lead2.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                telegram_file_unique_id=" unq",
            )

    def test_telegram_file_unique_id_trailing_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "trail2.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                telegram_file_unique_id="unq ",
            )

    def test_telegram_file_id_whitespace_only_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "wsonly.txt", b"data")
        with pytest.raises(
            InvalidAttachmentIdentityError, match="must not be empty or whitespace-only"
        ):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                telegram_file_id="   ",
            )


# ---------------------------------------------------------------------------
# Filename and MIME whitespace validation
# ---------------------------------------------------------------------------


class TestFilenameAndMimeValidation:
    def test_original_filename_leading_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "fn.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                original_filename=" bad.pdf",
            )

    def test_original_filename_trailing_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "fn2.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                original_filename="bad.pdf ",
            )

    def test_declared_mime_type_leading_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "mime.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                declared_mime_type=" image/png",
            )

    def test_declared_mime_type_trailing_ws_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "mime2.txt", b"data")
        with pytest.raises(InvalidAttachmentIdentityError, match="leading or trailing whitespace"):
            persist_attachment_evidence(
                conn,
                f,
                raw_intake_id=rid,
                public_id=_unique_public_id(),
                declared_mime_type="image/png ",
            )


# ---------------------------------------------------------------------------
# File identity checks — device, mtime, path replacement
# ---------------------------------------------------------------------------


class TestFileIdentityChecks:
    def test_device_change_detected_during_hash(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """fstat reports different device pre vs post read."""

        f = _make_test_file(tmp_path, "dev_change.txt", b"device change test" * 100)
        _real_fstat = _os.fstat
        _call_count = [0]

        def _changing_fstat(fd):
            _call_count[0] += 1
            sr = _real_fstat(fd)
            if _call_count[0] == 2:
                return _os.stat_result(
                    (
                        sr.st_mode,
                        sr.st_ino,
                        99999,  # different device
                        sr.st_nlink,
                        sr.st_uid,
                        sr.st_gid,
                        sr.st_size,
                        sr.st_atime,
                        sr.st_mtime,
                        sr.st_ctime,
                    )
                )
            return sr

        from finance_core.intake import attachment_evidence

        with patch.object(_os, "fstat", side_effect=_changing_fstat):
            with pytest.raises(AttachmentFileChangedDuringHashError, match="device changed"):
                attachment_evidence._compute_stable_identity(f)

    def test_mtime_change_detected_during_hash(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """fstat reports different mtime pre vs post read."""

        f = _make_test_file(tmp_path, "mtime_change.txt", b"mtime test" * 100)
        _real_fstat = _os.fstat
        _call_count = [0]

        def _changing_fstat(fd):
            _call_count[0] += 1
            sr = _real_fstat(fd)
            if _call_count[0] == 2:
                return _os.stat_result(
                    (
                        sr.st_mode,
                        sr.st_ino,
                        sr.st_dev,
                        sr.st_nlink,
                        sr.st_uid,
                        sr.st_gid,
                        sr.st_size,
                        sr.st_atime,
                        sr.st_mtime_ns + 1,
                        sr.st_ctime,
                    )
                )
            return sr

        from finance_core.intake import attachment_evidence

        with patch.object(_os, "fstat", side_effect=_changing_fstat):
            with pytest.raises(AttachmentFileChangedDuringHashError, match="mtime changed"):
                attachment_evidence._compute_stable_identity(f)

    def test_path_replacement_after_hash_detected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """After hashing, the path points to a different inode."""

        f1 = _make_test_file(tmp_path, "original.txt", b"A" * 2000)
        f2 = _make_test_file(tmp_path, "replacement.txt", b"B" * 2000)
        _original_path = str(f1)

        # Patch os.stat to return replacement inode after hashing
        _real_stat = _os.stat
        _call_count = [0]

        def _replacing_stat(path, *args, **kwargs):
            _call_count[0] += 1
            # After hashing, when _check_path_identity calls os.stat,
            # return the replacement file's stat
            return _real_stat(str(f2), *args, **kwargs)

        from finance_core.intake import attachment_evidence

        with patch.object(_os, "stat", side_effect=_replacing_stat):
            with pytest.raises(AttachmentFileChangedDuringHashError, match="replaced"):
                attachment_evidence._compute_stable_identity(f1)

    def test_device_mismatch_in_check_path_identity(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """After hashing, os.stat returns same inode but different device → error."""
        f = _make_test_file(tmp_path, "dev_check.txt", b"device check" * 100)
        _real_stat = _os.stat

        def _device_mismatch_stat(path, *args, **kwargs):
            sr = _real_stat(path, *args, **kwargs)
            # Return same inode but different device
            return _os.stat_result(
                (
                    sr.st_mode,
                    sr.st_ino,
                    sr.st_dev + 99999,  # different device
                    sr.st_nlink,
                    sr.st_uid,
                    sr.st_gid,
                    sr.st_size,
                    sr.st_atime,
                    sr.st_mtime,
                    sr.st_ctime,
                )
            )

        from finance_core.intake import attachment_evidence

        with patch.object(_os, "stat", side_effect=_device_mismatch_stat):
            with pytest.raises(AttachmentFileChangedDuringHashError, match="replaced"):
                attachment_evidence._compute_stable_identity(f)

    def test_non_regular_file_handle_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        """stat.S_ISREG check on opened handle rejects non-regular files."""
        # Create a named pipe (FIFO)
        fifo_path = tmp_path / "test_fifo"
        _os.mkfifo(str(fifo_path))

        from finance_core.intake import attachment_evidence

        # Opening a FIFO blocks unless we handle it.  Since stat.S_ISREG
        # is checked after open, we test via patching fstat to look non-regular.
        f = _make_test_file(tmp_path, "regular.txt", b"regular data")
        _real_fstat = _os.fstat

        def _non_reg_fstat(fd):
            sr = _real_fstat(fd)
            # Return a non-regular mode (S_IFDIR = 0o040000)
            import stat as _stat

            return _os.stat_result(
                (
                    _stat.S_IFDIR | 0o755,
                    sr.st_ino,
                    sr.st_dev,
                    sr.st_nlink,
                    sr.st_uid,
                    sr.st_gid,
                    sr.st_size,
                    sr.st_atime,
                    sr.st_mtime,
                    sr.st_ctime,
                )
            )

        with patch.object(_os, "fstat", side_effect=_non_reg_fstat):
            with pytest.raises(AttachmentFileNotFoundError, match="not a regular file"):
                attachment_evidence._compute_stable_identity(f)


# ---------------------------------------------------------------------------
# Raw-intake attachment authority — first bind, replay, conflict
# ---------------------------------------------------------------------------


class TestRawIntakeAttachmentAuthority:
    def test_first_bind_links_attachment(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """When raw_intake has no attachment, persist sets it."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "first_bind.txt", b"first bind")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["idempotent"] is False

        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == result["attachment_id"]
        assert row["attachment_path"] == str(f)
        assert row["attachment_hash"] == result["content_hash"]

    def test_same_attachment_replay_is_idempotent(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Same public_id + same raw_intake + same attachment → idempotent."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "same_att.txt", b"same attachment")
        pid = _unique_public_id()
        content_hash = _sha256(b"same attachment")

        r1 = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert r1["idempotent"] is False

        r2 = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert r2["idempotent"] is True
        assert r2["attachment_id"] == r1["attachment_id"]
        assert r2["content_hash"] == content_hash

        # Only one source row
        assert _count_source_rows(conn) == 1

    def test_different_attachment_for_same_raw_intake_conflicts(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Different attachment path + hash for an already-bound raw_intake conflicts."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f1 = _make_test_file(tmp_path, "first_att.txt", b"first content")
        f2 = _make_test_file(tmp_path, "second_att.txt", b"second different content")
        pid1 = _unique_public_id()
        pid2 = _unique_public_id()

        persist_attachment_evidence(conn, f1, raw_intake_id=rid, public_id=pid1)

        # Now try to bind a different attachment to the same raw_intake
        # This should fail because raw_intake already has an attachment
        with pytest.raises(AttachmentRawIntakeConflictError, match="already linked"):
            persist_attachment_evidence(conn, f2, raw_intake_id=rid, public_id=pid2)

    def test_raw_input_unchanged_after_bind(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """raw_input is never modified during attachment binding."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "raw_unchanged.txt", b"raw data")
        pid = _unique_public_id()

        raw_before = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE id = ?", (rid,)
        ).fetchone()["raw_input"]

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        raw_after = conn.execute(
            "SELECT raw_input FROM raw_intake_records WHERE id = ?", (rid,)
        ).fetchone()["raw_input"]

        assert raw_after == raw_before

    def test_partially_populated_compatible_bind(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has path/hash set but attachment_id NULL — compatible bind."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        content = b"partial compatible content"
        f = _make_test_file(tmp_path, "partial_compat.txt", content)
        expected_hash = _sha256(content)
        pid = _unique_public_id()

        # Insert a raw_intake with path/hash populated but no attachment_id
        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_path, attachment_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test partial compatible raw input",
                "2026-07-19T10:00:00+00:00",
                str(f),
                expected_hash,
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        # Verify pre-state
        pre = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert pre["attachment_id"] is None
        assert pre["attachment_path"] == str(f)
        assert pre["attachment_hash"] == expected_hash

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["idempotent"] is False

        post = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert post["attachment_id"] == result["attachment_id"]
        assert post["attachment_path"] == str(f)
        assert post["attachment_hash"] == expected_hash

        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_partially_populated_conflicting_path(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has path/hash set but attachment_id NULL — conflicting path → conflict."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        content = b"partial conflict path"
        f_orig = _make_test_file(tmp_path, "partial_orig.txt", content)
        f_diff = _make_test_file(tmp_path, "partial_diff.txt", b"different content!!")
        expected_hash = _sha256(content)
        pid = _unique_public_id()

        # Insert with original path/hash
        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_path, attachment_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test partial conflicting path",
                "2026-07-19T10:00:00+00:00",
                str(f_orig),
                expected_hash,
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        with pytest.raises(AttachmentRawIntakeConflictError, match="conflicts"):
            persist_attachment_evidence(conn, f_diff, raw_intake_id=rid, public_id=pid)

    def test_partially_populated_conflicting_hash(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has path/hash set but attachment_id NULL — conflicting hash → conflict."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        content = b"partial conflict hash"
        f = _make_test_file(tmp_path, "partial_hash.txt", content)
        wrong_hash = "f" * 64
        pid = _unique_public_id()

        # Insert with a path that matches the file but a different hash
        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_path, attachment_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test partial conflicting hash",
                "2026-07-19T10:00:00+00:00",
                str(f),
                wrong_hash,
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        with pytest.raises(AttachmentRawIntakeConflictError, match="conflicts"):
            persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

    def test_path_only_partial_state(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has only attachment_path set, hash NULL → compatible
        bind that populates hash and attachment_id."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        content = b"path only partial state"
        f = _make_test_file(tmp_path, "path_only_partial.txt", content)
        expected_hash = _sha256(content)
        pid = _unique_public_id()

        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test path only partial",
                "2026-07-19T10:00:00+00:00",
                str(f),
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        pre = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert pre["attachment_id"] is None
        assert pre["attachment_path"] == str(f)
        assert pre["attachment_hash"] is None

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["idempotent"] is False

        post = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert post["attachment_id"] == result["attachment_id"]
        assert post["attachment_path"] == str(f)
        assert post["attachment_hash"] == expected_hash

        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_hash_only_partial_state(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has only attachment_hash set, path NULL → compatible
        bind that populates path and attachment_id."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        content = b"hash only partial state"
        f = _make_test_file(tmp_path, "hash_only_partial.txt", content)
        expected_hash = _sha256(content)
        pid = _unique_public_id()

        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test hash only partial",
                "2026-07-19T10:00:00+00:00",
                expected_hash,
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        pre = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert pre["attachment_id"] is None
        assert pre["attachment_path"] is None
        assert pre["attachment_hash"] == expected_hash

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        assert result["idempotent"] is False

        post = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert post["attachment_id"] == result["attachment_id"]
        assert post["attachment_path"] == str(f)
        assert post["attachment_hash"] == expected_hash

        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_path_only_conflicting_path(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has only a path that doesn't match — conflict."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        f_wrong = _make_test_file(tmp_path, "path_only_wrong.txt", b"wrong path")
        f_correct = _make_test_file(tmp_path, "path_only_correct.txt", b"correct path content")
        pid = _unique_public_id()

        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test path only conflict",
                "2026-07-19T10:00:00+00:00",
                str(f_wrong),
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        with pytest.raises(AttachmentRawIntakeConflictError, match="conflicts"):
            persist_attachment_evidence(conn, f_correct, raw_intake_id=rid, public_id=pid)

    def test_hash_only_conflicting_hash(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Raw-intake has only a hash that doesn't match — conflict."""
        from uuid import uuid4

        conn = migrated_temp_db_connection
        content = b"hash only conflict"
        f = _make_test_file(tmp_path, "hash_only_conflict.txt", content)
        wrong_hash = "a" * 64
        pid = _unique_public_id()

        rid_pid = f"raw_intake_{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input, received_at,
                attachment_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rid_pid,
                "telegram_text",
                "telegram",
                "Test hash only conflict",
                "2026-07-19T10:00:00+00:00",
                wrong_hash,
            ),
        )
        conn.commit()
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        with pytest.raises(AttachmentRawIntakeConflictError, match="conflicts"):
            persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)


# ---------------------------------------------------------------------------
# Canonical attachment immutability (database-level triggers)
# ---------------------------------------------------------------------------


class TestAttachmentImmutability:
    def test_cannot_update_file_path_of_referenced_attachment(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "immutable_path.txt", b"immutable")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        att_id = result["attachment_id"]

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET file_path = '/changed/path.txt' WHERE id = ?",
                (att_id,),
            )

    def test_cannot_update_file_hash_of_referenced_attachment(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "immutable_hash.txt", b"immutable")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        att_id = result["attachment_id"]

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE attachments SET file_hash = ? WHERE id = ?",
                ("0" * 64, att_id),
            )

    def test_cannot_delete_referenced_attachment(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "no_delete.txt", b"no delete")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        att_id = result["attachment_id"]

        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))

    def test_cannot_detach_raw_intake_attachment_with_telegram_source(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "no_detach.txt", b"no detach")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE raw_intake_records SET attachment_id = NULL WHERE id = ?",
                (rid,),
            )

    def test_unreferenced_attachment_still_mutable(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        # Create an attachment row that is NOT referenced by telegram_attachment_source
        conn.execute(
            """
            INSERT INTO attachments (
                public_id, attachment_type, file_path, file_hash,
                source_channel, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "at_unreferenced",
                "telegram_attachment",
                "/tmp/unref.txt",
                "0" * 64,
                "telegram",
                "2026-07-19T10:00:00+00:00",
                "2026-07-19T10:00:00+00:00",
            ),
        )
        att_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Unreferenced attachment can be updated
        conn.execute(
            "UPDATE attachments SET file_path = '/tmp/changed.txt' WHERE id = ?",
            (att_id,),
        )
        conn.commit()
        _row = conn.execute("SELECT file_path FROM attachments WHERE id = ?", (att_id,)).fetchone()

    def test_cannot_update_raw_intake_attachment_path_with_telegram_source(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Direct UPDATE of raw_intake_records.attachment_path is rejected
        when a Telegram source row references that raw_intake."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "immut_raw_path.txt", b"immutable raw path")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE raw_intake_records SET attachment_path = '/changed/path.txt' WHERE id = ?",
                (rid,),
            )

    def test_cannot_update_raw_intake_attachment_hash_with_telegram_source(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Direct UPDATE of raw_intake_records.attachment_hash is rejected
        when a Telegram source row references that raw_intake."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "immut_raw_hash.txt", b"immutable raw hash")
        pid = _unique_public_id()

        persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE raw_intake_records SET attachment_hash = ? WHERE id = ?",
                ("f" * 64, rid),
            )

    def test_cannot_replace_raw_intake_attachment_id_with_telegram_source(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Direct UPDATE of raw_intake_records.attachment_id to a different value
        is rejected when a Telegram source row references that raw_intake."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "immut_raw_att_id.txt", b"immutable raw att id")
        pid = _unique_public_id()

        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE raw_intake_records SET attachment_id = ? WHERE id = ?",
                (result["attachment_id"] + 1, rid),
            )

    def test_unrelated_raw_intake_row_still_mutable(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """A raw_intake row without any Telegram source row can still have
        its attachment fields updated (triggers only fire when source exists)."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)

        # No Telegram source exists for this raw_intake — updates should succeed
        conn.execute(
            "UPDATE raw_intake_records SET attachment_path = '/updated/path.txt' WHERE id = ?",
            (rid,),
        )
        conn.execute(
            "UPDATE raw_intake_records SET attachment_hash = ? WHERE id = ?",
            ("a" * 64, rid),
        )
        # For attachment_id, create a real attachment so FK passes
        conn.execute(
            "INSERT INTO attachments "
            "(public_id, attachment_type, file_path, file_hash, "
            "source_channel, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "at_unrelated",
                "telegram_attachment",
                "/tmp/unrelated_test.txt",
                "b" * 64,
                "telegram",
                "2026-07-19T10:00:00+00:00",
                "2026-07-19T10:00:00+00:00",
            ),
        )
        unrelated_att_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE raw_intake_records SET attachment_id = ? WHERE id = ?",
            (unrelated_att_id, rid),
        )
        conn.commit()

        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == unrelated_att_id
        assert row["attachment_path"] == "/updated/path.txt"
        assert row["attachment_hash"] == "a" * 64


# ---------------------------------------------------------------------------
# Real atomic rollback tests — fail at each write boundary
# ---------------------------------------------------------------------------


class TestRealAtomicRollback:
    """Inject failure at each real write boundary inside the service-owned
    transaction and verify complete rollback — no orphan rows."""

    def test_rollback_before_source_insert(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "rollback_src.txt", b"rollback test")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        _before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)

        # Simulate attachment resolution failure before source insert
        with patch(
            "finance_core.intake.attachment_evidence._resolve_attachment_row",
            side_effect=sqlite3.OperationalError("simulated attachment failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == _before_att
        assert _count_evidence_rows(conn) == before_ev
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_rollback_before_raw_intake_linkage(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "rollback_link.txt", b"rollback link")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)
        # Snapshot baseline raw-intake attachment state
        baseline = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        # Capture original file bytes
        original_bytes = f.read_bytes()

        with patch(
            "finance_core.intake.attachment_evidence._bind_raw_intake_attachment",
            side_effect=sqlite3.OperationalError("simulated linkage failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == before_att
        assert _count_evidence_rows(conn) == before_ev
        # Raw-intake attachment state returned to exact baseline
        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == baseline["attachment_id"]
        assert row["attachment_path"] == baseline["attachment_path"]
        assert row["attachment_hash"] == baseline["attachment_hash"]
        assert row["raw_input"] == baseline["raw_input"]
        assert f.read_bytes() == original_bytes
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_rollback_during_source_insert(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Fail the INSERT into telegram_attachment_source and verify rollback."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "rb_src.txt", b"rollback src insert")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)
        # Snapshot baseline raw-intake attachment state
        baseline = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        # Capture original file bytes
        original_bytes = f.read_bytes()

        # Simulate failure at the narrow _insert_telegram_source_row seam.
        with patch(
            "finance_core.intake.attachment_evidence._insert_telegram_source_row",
            side_effect=sqlite3.OperationalError("simulated source insert failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == before_att
        assert _count_evidence_rows(conn) == before_ev
        # Raw-intake attachment state returned to exact baseline
        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == baseline["attachment_id"]
        assert row["attachment_path"] == baseline["attachment_path"]
        assert row["attachment_hash"] == baseline["attachment_hash"]
        assert row["raw_input"] == baseline["raw_input"]
        assert f.read_bytes() == original_bytes
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)

    def test_rollback_during_evidence_trigger(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Fail the AFTER INSERT evidence trigger — verify complete rollback."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "rb_ev_trig.txt", b"rollback evidence trigger")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)
        # Snapshot baseline raw-intake attachment state
        baseline = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        # Capture original file bytes
        original_bytes = f.read_bytes()

        # Install a temp BEFORE INSERT trigger that aborts evidence insertion
        conn.execute(
            "CREATE TEMP TRIGGER IF NOT EXISTS _test_inject_evidence_failure "
            "BEFORE INSERT ON raw_intake_evidence "
            "BEGIN "
            "    SELECT RAISE(ABORT, 'injected evidence trigger failure'); "
            "END"
        )
        try:
            with pytest.raises(AttachmentEvidencePersistenceError):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        finally:
            conn.execute("DROP TRIGGER IF EXISTS _test_inject_evidence_failure")

        # Verify complete rollback

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == before_att
        assert _count_evidence_rows(conn) == before_ev
        # Raw-intake attachment state returned to exact baseline
        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == baseline["attachment_id"]
        assert row["attachment_path"] == baseline["attachment_path"]
        assert row["attachment_hash"] == baseline["attachment_hash"]
        assert row["raw_input"] == baseline["raw_input"]
        assert f.read_bytes() == original_bytes
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)
        # Connection remains usable after rollback
        pid2 = _unique_public_id()
        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid2)
        assert result["idempotent"] is False
        assert _count_source_rows(conn) == before_source + 1

    def test_persistence_error_retains_cause(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Evidence-trigger failure raises AttachmentEvidencePersistenceError,
        with the original sqlite3.IntegrityError chained as __cause__."""
        import finance_core.intake.attachment_evidence as _mod

        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "persist_err_cause.txt", b"persistence cause")
        pid = _unique_public_id()

        before_source = _count_source_rows(conn)
        before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)

        baseline = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        original_bytes = f.read_bytes()

        conn.execute(
            "CREATE TEMP TRIGGER IF NOT EXISTS _test_inject_evidence_failure "
            "BEFORE INSERT ON raw_intake_evidence "
            "BEGIN "
            "    SELECT RAISE(ABORT, 'injected evidence trigger failure'); "
            "END"
        )
        try:
            with pytest.raises(AttachmentEvidencePersistenceError) as exc_info:
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
            # Verify the original sqlite3 IntegrityError is chained as __cause__
            assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
            assert "injected evidence trigger failure" in str(exc_info.value.__cause__)
        finally:
            conn.execute("DROP TRIGGER IF EXISTS _test_inject_evidence_failure")

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == before_att
        assert _count_evidence_rows(conn) == before_ev
        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == baseline["attachment_id"]
        assert row["attachment_path"] == baseline["attachment_path"]
        assert row["attachment_hash"] == baseline["attachment_hash"]
        assert row["raw_input"] == baseline["raw_input"]
        assert f.read_bytes() == original_bytes
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)
        # Connection remains usable
        _mod._commit_failure_injection = None
        pid2 = _unique_public_id()
        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid2)
        assert result["idempotent"] is False

    def test_rollback_before_commit(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Fail commit after all writes succeed — verify complete rollback."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "rb_commit.txt", b"rollback before commit")
        pid = _unique_public_id()
        before_source = _count_source_rows(conn)
        before_att = _count_attachment_rows(conn)
        before_ev = _count_evidence_rows(conn)
        # Snapshot baseline raw-intake attachment state
        baseline = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        # Capture original file bytes
        original_bytes = f.read_bytes()

        # Let all writes complete, then fail at commit
        import finance_core.intake.attachment_evidence as _mod

        _mod._commit_failure_injection = "simulated commit failure"
        try:
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid)
        finally:
            _mod._commit_failure_injection = None

        assert _count_source_rows(conn) == before_source
        assert _count_attachment_rows(conn) == before_att
        assert _count_evidence_rows(conn) == before_ev
        # Raw-intake attachment state returned to exact baseline
        row = conn.execute(
            "SELECT attachment_id, attachment_path, attachment_hash, raw_input "
            "FROM raw_intake_records WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attachment_id"] == baseline["attachment_id"]
        assert row["attachment_path"] == baseline["attachment_path"]
        assert row["attachment_hash"] == baseline["attachment_hash"]
        assert row["raw_input"] == baseline["raw_input"]
        assert f.read_bytes() == original_bytes
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)
        # Connection remains usable after rollback
        pid2 = _unique_public_id()
        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid2)
        assert result["idempotent"] is False
        assert _count_source_rows(conn) == before_source + 1

    def test_connection_usable_after_rollback(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """After a forced rollback, the connection can still persist successfully."""
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        f = _make_test_file(tmp_path, "recovery.txt", b"recovery after rollback")
        pid_fail = _unique_public_id()
        pid_ok = _unique_public_id()

        # First attempt fails
        with patch(
            "finance_core.intake.attachment_evidence._resolve_attachment_row",
            side_effect=sqlite3.OperationalError("simulated failure"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="simulated"):
                persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid_fail)

        # Second attempt succeeds on the same connection
        result = persist_attachment_evidence(conn, f, raw_intake_id=rid, public_id=pid_ok)
        assert result["idempotent"] is False
        assert _count_source_rows(conn) == 1
        assert _foreign_key_ok(conn)
        assert _integrity_ok(conn)


class TestMigration031:
    def test_migration_031_is_in_manifest(self) -> None:
        filenames = [p.name for p in TEMP_DB_MIGRATION_PATHS]
        assert "031_telegram_attachment_evidence.sql" in filenames

    def test_fresh_migration_replay_001_through_031(self, tmp_path: Path) -> None:
        paths_through_031 = TEMP_DB_MIGRATION_PATHS[:31]
        db_path = tmp_path / "migration_031_fresh.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            apply_migration_paths(conn, paths_through_031)
            conn.commit()

            verify_migration_history(conn, paths_through_031)

            conn.execute("SELECT 1 FROM telegram_attachment_source LIMIT 0")

            # Append-only triggers exist
            for trg_name in [
                "trg_telegram_attachment_source_no_update",
                "trg_telegram_attachment_source_no_delete",
            ]:
                tr = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                    (trg_name,),
                ).fetchone()
                assert tr is not None, f"Missing trigger: {trg_name}"

            assert _foreign_key_ok(conn)
            assert _integrity_ok(conn)
        finally:
            conn.close()

    def test_repeated_replay_idempotent(self, tmp_path: Path) -> None:
        paths_through_031 = TEMP_DB_MIGRATION_PATHS[:31]
        db_path = tmp_path / "migration_031_replay.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            apply_migration_paths(conn, paths_through_031)
            conn.commit()

            apply_migration_paths(conn, paths_through_031)
            conn.commit()

            conn.execute("SELECT 1 FROM telegram_attachment_source LIMIT 0")
        finally:
            conn.close()

    def test_incremental_upgrade_030_to_031(self, tmp_path: Path) -> None:
        """Apply through 030, snapshot full ledger rows, then apply 031 independently.

        Verifies first 30 rows remain byte-for-byte equal, schema fingerprint,
        restart replay, foreign keys, and integrity.
        """
        from finance_core.reconciliation.migrations import (
            TEMP_DB_MIGRATION_PATHS,
            apply_migration_paths,
            verify_migration_history,
        )

        paths_through_030 = TEMP_DB_MIGRATION_PATHS[:30]
        paths_through_031 = TEMP_DB_MIGRATION_PATHS[:31]

        db_path = tmp_path / "migration_030_to_031.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # Apply through 030
            apply_migration_paths(conn, paths_through_030)
            conn.commit()
            verify_migration_history(conn, paths_through_030)

            # Snapshot full ledger rows before 031 (all columns)
            before_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM schema_migrations ORDER BY migration_sequence"
                ).fetchall()
            ]
            assert len(before_rows) == 30
            assert before_rows[-1]["migration_id"] == "030"
            assert "031" not in {r["migration_id"] for r in before_rows}

            # Snapshot schema fingerprint before 031
            before_tables = set(
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            )
            assert "telegram_attachment_source" not in {t[0] for t in before_tables}

            # Apply the manifest through 031 only.
            apply_migration_paths(conn, paths_through_031)
            conn.commit()

            # Full ledger rows after 031
            after_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM schema_migrations ORDER BY migration_sequence"
                ).fetchall()
            ]
            assert len(after_rows) == 31

            # First 30 rows must be byte-for-byte equal (matching before_rows)
            for i, (before, after) in enumerate(zip(before_rows, after_rows[:30])):
                assert before == after, f"Row {i} changed: {before} vs {after}"

            # Exactly one new 031 row
            assert after_rows[30]["migration_id"] == "031"

            conn.execute("SELECT 1 FROM telegram_attachment_source LIMIT 0")
            assert _foreign_key_ok(conn)
            assert _integrity_ok(conn)
        finally:
            conn.close()

        # Restart replay — same manifest on same database must be idempotent
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA foreign_keys = ON")
        try:
            apply_migration_paths(conn2, paths_through_031)
            conn2.commit()

            rows2 = [
                dict(r)
                for r in conn2.execute(
                    "SELECT * FROM schema_migrations ORDER BY migration_sequence"
                ).fetchall()
            ]
            assert len(rows2) == 31
            # Restart replay preserves all rows
            for i, (expected, actual) in enumerate(zip(after_rows, rows2)):
                assert expected == actual, f"Restart replay changed row {i}"
        finally:
            conn2.close()

    def test_migration_031_constraints(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)
        from uuid import uuid4

        # Create a valid attachment first so FK passes cleanly
        conn.execute(
            """
            INSERT INTO attachments (
                public_id, attachment_type, file_path, file_hash,
                source_channel, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "at_constraint_test",
                "telegram_attachment",
                "/tmp/constraint_test.txt",
                "a" * 64,
                "telegram",
                "2026-07-19T10:00:00+00:00",
                "2026-07-19T10:00:00+00:00",
            ),
        )
        att_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # First insert succeeds
        pid = f"tgae_constraint_{uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT INTO telegram_attachment_source (
                public_id, attachment_id, raw_intake_record_id,
                original_attachment_path, observed_file_size,
                content_hash, source_evidence_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (pid, att_id, rid, "/tmp/test.txt", 4, "a" * 64, '{"test": true}'),
        )

        # Duplicate public_id must fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO telegram_attachment_source (
                    public_id, attachment_id, raw_intake_record_id,
                    original_attachment_path, observed_file_size,
                    content_hash, source_evidence_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pid, att_id, rid, "/tmp/test.txt", 4, "a" * 64, '{"test": true}'),
            )

    def test_migration_031_content_hash_format_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO telegram_attachment_source (
                    public_id, attachment_id, raw_intake_record_id,
                    original_attachment_path, observed_file_size,
                    content_hash, source_evidence_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _unique_public_id(),
                    1,
                    rid,
                    "/tmp/badhash.txt",
                    4,
                    "ABCD" + "0" * 60,
                    '{"test": true}',
                ),
            )

    def test_migration_031_invalid_json_rejected(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection
        rid = _insert_raw_intake(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO telegram_attachment_source (
                    public_id, attachment_id, raw_intake_record_id,
                    original_attachment_path, observed_file_size,
                    content_hash, source_evidence_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _unique_public_id(),
                    1,
                    rid,
                    "/tmp/badjson.txt",
                    4,
                    "0" * 64,
                    "not json",
                ),
            )

    def test_foreign_key_enforcement(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        conn = migrated_temp_db_connection

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO telegram_attachment_source (
                    public_id, attachment_id, raw_intake_record_id,
                    original_attachment_path, observed_file_size,
                    content_hash, source_evidence_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _unique_public_id(),
                    99999,
                    99999,
                    "/tmp/fk.txt",
                    4,
                    "0" * 64,
                    '{"test": true}',
                ),
            )

    def test_migrated_temp_db_includes_table(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        conn.execute("SELECT 1 FROM telegram_attachment_source LIMIT 0")

    def test_migration_history_includes_031(
        self,
        migrated_temp_db_connection: sqlite3.Connection,
    ) -> None:
        conn = migrated_temp_db_connection
        rows = conn.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_sequence"
        ).fetchall()
        ids = {r["migration_id"] for r in rows}
        assert "031" in ids
        assert "030" in ids
        assert "029" in ids
