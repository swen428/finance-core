"""Direct tests for the shared bounded attachment publication seam.

The seam was extracted behavior-preserving from
``finance_core/intake/telegram_attachment_acquisition.py`` so the OpenClaw staging
bridge receipt handoff reuses the exact crash-safe no-overwrite publication
and replay contract instead of reimplementing it.  These tests bind the
extracted module's safety contract directly, and prove the acquisition saga
taxonomy stays intact through the translation wrappers.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path

import pytest

from finance_core.intake import attachment_publication as publication
from finance_core.intake import telegram_attachment_acquisition as acquisition

JPEG = b"\xff\xd8\xff" + b"seam-jpeg-evidence"
PNG = b"\x89PNG\r\n\x1a\n" + b"seam-png-evidence"
PDF = b"%PDF-1.7\nseam-pdf-evidence"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "attachments"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def _publish(
    root: Path, content: bytes, *, detected: publication.DetectedType | None = None
) -> tuple[str, bool]:
    handle = publication.open_storage_root(root)
    temp_name: str | None = None
    try:
        publication.acquire_storage_root_lock(
            handle, deadline=time.monotonic() + 10, clock=time.monotonic
        )
        try:
            temp_name, fd = publication.create_private_temp(handle)
            try:
                os.write(fd, content)
                os.fsync(fd)
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            finally:
                os.close(fd)
            detected_type = detected or publication.detect_content_type(
                content[:16], observed_size=len(content)
            )
            final_path, reused = publication.publish_no_overwrite(
                handle,
                temp_name=temp_name,
                observed_size=len(content),
                content_hash=_sha(content),
                detected=detected_type,
                deadline=time.monotonic() + 10,
                clock=time.monotonic,
            )
            cleanup = temp_name
            temp_name = None
            publication.cleanup_temp(handle, cleanup)
            return final_path, reused
        finally:
            publication.release_storage_root_lock(handle)
    finally:
        if temp_name is not None:
            publication.cleanup_after_failure(
                handle, temp_name, publication.DurablePublicationError("test abort")
            )
        handle.close()


class TestStorageRootContract:
    def test_relative_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(publication.UnsafeStorageRootError):
            publication.open_storage_root("relative/root")

    def test_symlinked_root_is_refused(self, storage_root: Path, tmp_path: Path) -> None:
        link = tmp_path / "link"
        link.symlink_to(storage_root)
        with pytest.raises(publication.UnsafeStorageRootError):
            publication.open_storage_root(link)

    def test_group_readable_root_is_refused(self, tmp_path: Path) -> None:
        unsafe = tmp_path / "unsafe"
        unsafe.mkdir(mode=0o755)
        with pytest.raises(publication.UnsafeStorageRootError):
            publication.open_storage_root(unsafe)

    def test_missing_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(publication.UnsafeStorageRootError):
            publication.open_storage_root(tmp_path / "missing")


class TestContentClassification:
    @pytest.mark.parametrize(
        ("content", "mime"),
        [(JPEG, "image/jpeg"), (PNG, "image/png"), (PDF, "application/pdf")],
    )
    def test_signatures_are_detected(self, content: bytes, mime: str) -> None:
        detected = publication.detect_content_type(content[:16], observed_size=len(content))
        assert detected.mime_type == mime

    def test_empty_and_unknown_signatures_are_refused(self) -> None:
        with pytest.raises(publication.ContentSignatureMismatchError):
            publication.detect_content_type(b"", observed_size=0)
        with pytest.raises(publication.ContentSignatureMismatchError):
            publication.detect_content_type(b"plain-text", observed_size=10)

    def test_declared_mime_conflict_is_refused(self) -> None:
        detected = publication.detect_content_type(JPEG[:16], observed_size=len(JPEG))
        with pytest.raises(publication.ContentSignatureMismatchError):
            publication.validate_content_evidence(
                detected, original_filename=None, declared_mime_type="image/png"
            )

    def test_misleading_double_extension_is_refused(self) -> None:
        with pytest.raises(publication.UnsupportedFilenameExtensionError):
            publication.expected_type_from_filename("receipt.exe.jpg")


class TestNoOverwritePublication:
    def test_publish_is_content_addressed_and_private(self, storage_root: Path) -> None:
        final_path, reused = _publish(storage_root, JPEG)
        content_hash = _sha(JPEG)
        expected = storage_root / content_hash[:2] / f"{content_hash}.jpg"
        assert Path(final_path) == expected
        assert reused is False
        st = os.lstat(final_path)
        assert stat.S_IMODE(st.st_mode) == 0o400
        shard = storage_root / content_hash[:2]
        assert stat.S_IMODE(os.lstat(shard).st_mode) == 0o700

    def test_second_publish_reuses_durable_target(self, storage_root: Path) -> None:
        first_path, first_reused = _publish(storage_root, JPEG)
        second_path, second_reused = _publish(storage_root, JPEG)
        assert first_path == second_path
        assert first_reused is False
        assert second_reused is True

    def test_conflicting_existing_target_fails_closed(self, storage_root: Path) -> None:
        content_hash = _sha(JPEG)
        shard = storage_root / content_hash[:2]
        shard.mkdir(mode=0o700)
        conflict = shard / f"{content_hash}.jpg"
        conflict.write_bytes(b"\xff\xd8\xff totally different bytes")
        conflict.chmod(0o400)
        with pytest.raises(publication.DurableFileIntegrityConflictError):
            _publish(storage_root, JPEG)

    def test_no_temp_residue_after_success(self, storage_root: Path) -> None:
        _publish(storage_root, PNG)
        residue = [path for path in storage_root.rglob("*") if path.name.startswith(".")]
        assert residue == []


class TestPersistedRowReplay:
    def test_canonical_replay_mapping_verifies(self, storage_root: Path) -> None:
        final_path, _reused = _publish(storage_root, JPEG)
        existing = {
            "original_attachment_path": final_path,
            "observed_file_size": len(JPEG),
            "content_hash": _sha(JPEG),
        }
        handle = publication.open_storage_root(storage_root)
        try:
            stored_path, size, content_hash, detected = publication.verify_persisted_durable_replay(
                handle, existing
            )
        finally:
            handle.close()
        assert stored_path == final_path
        assert size == len(JPEG)
        assert content_hash == _sha(JPEG)
        assert detected.mime_type == "image/jpeg"

    def test_wrong_shard_mapping_is_refused(self, storage_root: Path) -> None:
        final_path, _reused = _publish(storage_root, JPEG)
        wrong_path = str(Path(final_path).parent.parent / "zz" / Path(final_path).name)
        existing = {
            "original_attachment_path": wrong_path,
            "observed_file_size": len(JPEG),
            "content_hash": _sha(JPEG),
        }
        handle = publication.open_storage_root(storage_root)
        try:
            with pytest.raises(publication.DurableContractError):
                publication.verify_persisted_durable_replay(handle, existing)
        finally:
            handle.close()

    def test_missing_durable_bytes_are_refused(self, storage_root: Path) -> None:
        final_path, _reused = _publish(storage_root, JPEG)
        os.unlink(final_path)
        existing = {
            "original_attachment_path": final_path,
            "observed_file_size": len(JPEG),
            "content_hash": _sha(JPEG),
        }
        handle = publication.open_storage_root(storage_root)
        try:
            with pytest.raises(publication.DurableContractError):
                publication.verify_persisted_durable_replay(handle, existing)
        finally:
            handle.close()

    def test_malformed_replay_fields_are_refused(self, storage_root: Path) -> None:
        handle = publication.open_storage_root(storage_root)
        try:
            for broken in (
                {},
                {"original_attachment_path": "", "observed_file_size": 1, "content_hash": "x"},
                {
                    "original_attachment_path": "/tmp/a",
                    "observed_file_size": -1,
                    "content_hash": "a" * 64,
                },
            ):
                with pytest.raises(publication.DurableContractError):
                    publication.verify_persisted_durable_replay(handle, broken)
        finally:
            handle.close()


class TestAcquisitionTaxonomyPreserved:
    def test_acquisition_errors_stay_in_the_saga_family(self) -> None:
        for error_type in (
            acquisition.UnsafeStorageRootError,
            acquisition.DurablePublicationError,
            acquisition.DurableFileIntegrityConflictError,
            acquisition.TemporaryFileError,
            acquisition.TemporaryFileCleanupError,
            acquisition.ContentSignatureMismatchError,
            acquisition.UnsupportedMimeTypeError,
            acquisition.UnsupportedFilenameExtensionError,
        ):
            assert issubclass(error_type, acquisition.TelegramAttachmentAcquisitionError)

    def test_translated_wrappers_raise_acquisition_taxonomy(self, storage_root: Path) -> None:
        with pytest.raises(acquisition.UnsafeStorageRootError):
            acquisition._open_storage_root("relative/path")
        detected = acquisition._detect_content_type(JPEG[:16], observed_size=len(JPEG))
        assert detected == publication.JPEG
        with pytest.raises(acquisition.ContentSignatureMismatchError):
            acquisition._detect_content_type(b"", observed_size=0)
