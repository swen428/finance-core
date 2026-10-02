"""Bounded custody of the two explicit Tesseract language resources.

No discovery or downloads occur here. Each invocation owns a private snapshot;
replacement of a source after copying cannot alter that invocation's bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from finance_core.intake.receipt_ocr_evidence import (
    InvalidOcrConfigurationError,
    OcrDeadlineExceededError,
)

MAX_MODEL_BYTES = 32 * 1024 * 1024
MAX_TOTAL_MODEL_BYTES = 64 * 1024 * 1024
RESOURCE_VALIDATION_SECONDS = 10.0
PINNED_ENVIRONMENT = {"LANG": "C", "LC_ALL": "C", "TZ": "UTC", "OMP_THREAD_LIMIT": "1"}
_FIELDS = ("st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return tuple(int(getattr(value, name)) for name in _FIELDS)


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise OcrDeadlineExceededError("The OCR deadline expired during resource verification.")


def _safe_stat(value: os.stat_result, *, directory: bool = False) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o022:
        raise InvalidOcrConfigurationError("OCR resources must be direct service-owned safe files.")


@dataclass(frozen=True)
class TesseractLanguageResource:
    language: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or self.language not in {"eng", "chi_sim"}:
            raise InvalidOcrConfigurationError("Unsupported OCR language resource.")
        if type(self.size_bytes) is not int or not 0 < self.size_bytes <= MAX_MODEL_BYTES:
            raise InvalidOcrConfigurationError("OCR resource size exceeds its bounded contract.")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise InvalidOcrConfigurationError("OCR resource SHA-256 is malformed.")


@dataclass(frozen=True)
class PinnedTesseractResources:
    directory: Path
    languages: tuple[TesseractLanguageResource, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.directory, Path) or not self.directory.is_absolute():
            raise InvalidOcrConfigurationError("OCR resource directory must be absolute.")
        if type(self.languages) is not tuple or any(
            not isinstance(item, TesseractLanguageResource) for item in self.languages
        ):
            raise InvalidOcrConfigurationError("OCR language identities must be immutable.")
        if tuple(item.language for item in self.languages) != ("eng", "chi_sim"):
            raise InvalidOcrConfigurationError("OCR requires ordered eng then chi_sim resources.")
        if sum(item.size_bytes for item in self.languages) > MAX_TOTAL_MODEL_BYTES:
            raise InvalidOcrConfigurationError("OCR resources exceed the total byte ceiling.")

    def identities(self) -> list[dict[str, str | int]]:
        return [
            {"language": item.language, "size_bytes": item.size_bytes, "sha256": item.sha256}
            for item in self.languages
        ]

    def validate(self) -> None:
        with self.snapshot(deadline=time.monotonic() + RESOURCE_VALIDATION_SECONDS):
            pass

    @contextmanager
    def snapshot(self, *, deadline: float) -> Iterator[ResourceSnapshot]:
        check_deadline(deadline)
        try:
            if self.directory != self.directory.resolve(strict=True):
                raise InvalidOcrConfigurationError("OCR resource directory has symlink components.")
            directory_fd = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except OSError as exc:
            raise InvalidOcrConfigurationError(
                "OCR resource directory cannot be opened safely."
            ) from exc
        files: list[tuple[int, Path, TesseractLanguageResource, tuple[int, ...]]] = []
        try:
            before_directory = os.fstat(directory_fd)
            _safe_stat(before_directory, directory=True)
            if _identity(before_directory) != _identity(os.lstat(self.directory)):
                raise InvalidOcrConfigurationError("OCR resource directory changed during opening.")
            snapshot_path = Path(tempfile.mkdtemp(prefix="receipt-ocr-models-"))
            created_directory = os.lstat(snapshot_path)
            snapshot_fd = os.open(
                snapshot_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            try:
                owned_directory = os.fstat(snapshot_fd)
                _safe_stat(owned_directory, directory=True)
                if _identity(created_directory) != _identity(owned_directory):
                    raise InvalidOcrConfigurationError(
                        "Private OCR directory changed during opening."
                    )
                try:
                    for model in self.languages:
                        check_deadline(deadline)
                        name = f"{model.language}.traineddata"
                        source_fd = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                        )
                        try:
                            before = os.fstat(source_fd)
                            _safe_stat(before)
                            if before.st_size != model.size_bytes:
                                raise InvalidOcrConfigurationError(
                                    "OCR resource length does not match."
                                )
                            target = snapshot_path / name
                            target_fd = os.open(
                                name,
                                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=snapshot_fd,
                            )
                            try:
                                _stream_checked(source_fd, model, deadline, target_fd=target_fd)
                                if _identity(before) != _identity(os.fstat(source_fd)) or _identity(
                                    before
                                ) != _identity(
                                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                                ):
                                    raise InvalidOcrConfigurationError(
                                        "OCR source resource changed during copying."
                                    )
                                os.fchmod(target_fd, 0o400)
                                files.append(
                                    (target_fd, target, model, _identity(os.fstat(target_fd)))
                                )
                            except BaseException:
                                os.close(target_fd)
                                raise
                        finally:
                            os.close(source_fd)
                    if _identity(before_directory) != _identity(
                        os.fstat(directory_fd)
                    ) or _identity(before_directory) != _identity(os.lstat(self.directory)):
                        raise InvalidOcrConfigurationError(
                            "OCR resource directory changed during copying."
                        )
                    snapshot = ResourceSnapshot(
                        snapshot_path, snapshot_fd, _identity(os.fstat(snapshot_fd)), files
                    )
                    snapshot.verify(deadline)
                    yield snapshot
                finally:
                    for fd, _path, _model, _original in files:
                        os.close(fd)
                    _cleanup_snapshot(snapshot_path, snapshot_fd, owned_directory, self.languages)
            finally:
                os.close(snapshot_fd)
        except OSError as exc:
            raise InvalidOcrConfigurationError(
                "OCR resources changed or cannot be opened safely."
            ) from exc
        finally:
            os.close(directory_fd)


def _stream_checked(
    fd: int, model: TesseractLanguageResource, deadline: float, *, target_fd: int | None = None
) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    count = 0
    while True:
        check_deadline(deadline)
        chunk = os.read(fd, min(65_536, model.size_bytes - count + 1))
        if not chunk:
            break
        count += len(chunk)
        if count > model.size_bytes:
            raise InvalidOcrConfigurationError("OCR resource exceeded its expected length.")
        digest.update(chunk)
        if target_fd is not None:
            view = memoryview(chunk)
            while view:
                check_deadline(deadline)
                written = os.write(target_fd, view)
                if written <= 0:
                    raise InvalidOcrConfigurationError("OCR resource copy did not progress.")
                view = view[written:]
    check_deadline(deadline)
    if count != model.size_bytes or digest.hexdigest() != model.sha256:
        raise InvalidOcrConfigurationError("OCR resource hash/length does not match its identity.")


@dataclass
class ResourceSnapshot:
    directory: Path
    directory_fd: int
    directory_identity: tuple[int, ...]
    files: list[tuple[int, Path, TesseractLanguageResource, tuple[int, ...]]]

    @property
    def invocation_directory(self) -> str:
        # The descriptor is explicitly inherited by the Linux OCR child.
        return f"/proc/self/fd/{self.directory_fd}"

    def _verify_directory(self) -> None:
        opened = os.fstat(self.directory_fd)
        _safe_stat(opened, directory=True)
        if self.directory_identity != _identity(opened) or self.directory_identity != _identity(
            os.lstat(self.directory)
        ):
            raise InvalidOcrConfigurationError("Private OCR snapshot directory identity changed.")

    def verify(self, deadline: float) -> None:
        check_deadline(deadline)
        self._verify_directory()
        for fd, path, model, original in self.files:
            check_deadline(deadline)
            if original != _identity(os.fstat(fd)) or original != _identity(
                os.stat(path.name, dir_fd=self.directory_fd, follow_symlinks=False)
            ):
                raise InvalidOcrConfigurationError(
                    "Private OCR resource snapshot identity changed."
                )
            _stream_checked(fd, model, deadline)
            if original != _identity(os.fstat(fd)) or original != _identity(
                os.stat(path.name, dir_fd=self.directory_fd, follow_symlinks=False)
            ):
                raise InvalidOcrConfigurationError(
                    "Private OCR resource snapshot changed during verification."
                )
        self._verify_directory()


def _cleanup_snapshot(
    path: Path,
    directory_fd: int,
    owned: os.stat_result,
    languages: tuple[TesseractLanguageResource, ...],
) -> None:
    # Never recursively traverse a mutable pathname. Only our model entries are
    # unlinked in the held directory; a replacement path (including its data) stays.
    for model in languages:
        try:
            os.unlink(f"{model.language}.traineddata", dir_fd=directory_fd)
        except OSError:
            # A refused invocation keeps its original error. Unknown replacement
            # entries are never recursively removed to make cleanup succeed.
            pass
    try:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
            os.rmdir(path)
    except OSError:
        pass
