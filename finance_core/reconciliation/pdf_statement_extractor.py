"""Bounded PDF statement text extraction.

This module is the only production boundary that opens untrusted statement
PDFs.  It validates the source before parsing, hashes the exact opened bytes,
and performs ``pypdf`` work in a disposable child process.  The process is
terminated on timeout and receives an address-space limit on Linux.

The defaults are intentionally conservative for uploaded bank statements:

* 25 MiB accepts normal downloadable statements without permitting large
  arbitrary uploads.
* 100 pages covers long monthly/yearly statements while bounding page-tree
  traversal.
* 250,000 characters per page and 2,000,000 total characters allow dense
  text statements while bounding decompressed parser output.
* 15 seconds bounds malformed or adversarial parser work.
* 1 GiB of worker address space leaves headroom for ``pypdf`` while isolating
  pathological allocation from the importing process where the OS supports
  the limit.

No OCR, table extraction, database access, or financial mutation occurs here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import multiprocessing
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from finance_core.reconciliation.pdf_statement_evidence import PDF_TEXT_EXTRACTION_VERSION

_MIB = 1024 * 1024
_MIN_WORKER_MEMORY_BYTES = 128 * _MIB
_MAX_SAFE_FILE_BYTES = 100 * _MIB
_MAX_SAFE_PAGE_COUNT = 500
_MAX_SAFE_CHARS_PER_PAGE = 2_000_000
_MAX_SAFE_TOTAL_CHARS = 20_000_000
_MAX_SAFE_PARSE_SECONDS = 120.0
_MAX_SAFE_WORKER_MEMORY_BYTES = 4 * 1024 * _MIB
_WORKER_CLEANUP_SECONDS = 2.0


class PdfExtractionErrorCode(StrEnum):
    """Stable, non-sensitive classifications for extraction failures."""

    PATH_NOT_FOUND = "pdf_path_not_found"
    NOT_REGULAR_FILE = "pdf_not_regular_file"
    UNSUPPORTED_FILE_TYPE = "pdf_unsupported_file_type"
    UNSAFE_PATH = "pdf_unsafe_path"
    FILE_SIZE_LIMIT_EXCEEDED = "pdf_file_size_limit_exceeded"
    INVALID_SIGNATURE = "pdf_invalid_signature"
    PARSER_UNAVAILABLE = "pdf_parser_unavailable"
    MALFORMED_DOCUMENT = "pdf_malformed_document"
    ENCRYPTED_DOCUMENT = "pdf_encrypted_document"
    PAGE_LIMIT_EXCEEDED = "pdf_page_limit_exceeded"
    PAGE_TEXT_LIMIT_EXCEEDED = "pdf_page_text_limit_exceeded"
    TOTAL_TEXT_LIMIT_EXCEEDED = "pdf_total_text_limit_exceeded"
    PARSE_TIMEOUT = "pdf_parse_timeout"
    WORKER_RESOURCE_LIMIT_EXCEEDED = "pdf_worker_resource_limit_exceeded"
    WORKER_FAILURE = "pdf_worker_failure"
    NO_EXTRACTABLE_TEXT = "pdf_no_extractable_text"


_ERROR_MESSAGES: dict[PdfExtractionErrorCode, str] = {
    PdfExtractionErrorCode.PATH_NOT_FOUND: "PDF file was not found.",
    PdfExtractionErrorCode.NOT_REGULAR_FILE: (
        "PDF source is not a file; a regular file is required."
    ),
    PdfExtractionErrorCode.UNSUPPORTED_FILE_TYPE: "PDF source must use the .pdf extension.",
    PdfExtractionErrorCode.UNSAFE_PATH: "PDF source path is unsafe.",
    PdfExtractionErrorCode.FILE_SIZE_LIMIT_EXCEEDED: "PDF input file size limit exceeded.",
    PdfExtractionErrorCode.INVALID_SIGNATURE: "PDF signature is invalid.",
    PdfExtractionErrorCode.PARSER_UNAVAILABLE: "PDF parser dependency is unavailable.",
    PdfExtractionErrorCode.MALFORMED_DOCUMENT: "PDF document is malformed or unreadable.",
    PdfExtractionErrorCode.ENCRYPTED_DOCUMENT: "Encrypted PDF documents are not supported.",
    PdfExtractionErrorCode.PAGE_LIMIT_EXCEEDED: "PDF page count limit exceeded.",
    PdfExtractionErrorCode.PAGE_TEXT_LIMIT_EXCEEDED: "PDF per-page text limit exceeded.",
    PdfExtractionErrorCode.TOTAL_TEXT_LIMIT_EXCEEDED: "PDF total text limit exceeded.",
    PdfExtractionErrorCode.PARSE_TIMEOUT: "PDF parsing timed out.",
    PdfExtractionErrorCode.WORKER_RESOURCE_LIMIT_EXCEEDED: (
        "PDF parser worker resource limit exceeded."
    ),
    PdfExtractionErrorCode.WORKER_FAILURE: "PDF parser worker failed.",
    PdfExtractionErrorCode.NO_EXTRACTABLE_TEXT: "PDF contains no extractable text.",
}


def _require_positive_int(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{field} exceeds the supported safety maximum")
    return value


@dataclass(frozen=True)
class PdfResourceLimits:
    """Authoritative immutable resource limits for untrusted PDF parsing."""

    max_file_bytes: int = 25 * _MIB
    max_pages: int = 100
    max_chars_per_page: int = 250_000
    max_total_chars: int = 2_000_000
    max_parse_seconds: float = 15.0
    max_worker_memory_bytes: int = 1024 * _MIB

    def __post_init__(self) -> None:
        file_bytes = _require_positive_int(
            self.max_file_bytes,
            field="max_file_bytes",
            maximum=_MAX_SAFE_FILE_BYTES,
        )
        _require_positive_int(
            self.max_pages,
            field="max_pages",
            maximum=_MAX_SAFE_PAGE_COUNT,
        )
        per_page = _require_positive_int(
            self.max_chars_per_page,
            field="max_chars_per_page",
            maximum=_MAX_SAFE_CHARS_PER_PAGE,
        )
        total = _require_positive_int(
            self.max_total_chars,
            field="max_total_chars",
            maximum=_MAX_SAFE_TOTAL_CHARS,
        )
        worker_memory = _require_positive_int(
            self.max_worker_memory_bytes,
            field="max_worker_memory_bytes",
            maximum=_MAX_SAFE_WORKER_MEMORY_BYTES,
        )
        if isinstance(self.max_parse_seconds, bool) or not isinstance(
            self.max_parse_seconds, (int, float)
        ):
            raise ValueError("max_parse_seconds must be a positive finite number")
        parse_seconds = float(self.max_parse_seconds)
        if not 0.05 <= parse_seconds <= _MAX_SAFE_PARSE_SECONDS:
            raise ValueError(
                "max_parse_seconds must be between 0.05 and the supported safety maximum"
            )
        if total < per_page:
            raise ValueError("max_total_chars must be at least max_chars_per_page")
        if worker_memory < _MIN_WORKER_MEMORY_BYTES:
            raise ValueError("max_worker_memory_bytes is too small for the isolated PDF parser")
        if worker_memory < file_bytes * 4:
            raise ValueError("max_worker_memory_bytes must be at least four times max_file_bytes")


DEFAULT_PDF_RESOURCE_LIMITS = PdfResourceLimits()


@dataclass(frozen=True)
class PdfExtractedLine:
    """One stripped line of text extracted from a PDF page."""

    text: str
    line_number: int


@dataclass(frozen=True)
class PdfExtractedPage:
    """Text and page evidence extracted from one PDF page."""

    page_number: int
    lines: tuple[PdfExtractedLine, ...]
    raw_text_block: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdfExtractionResult:
    """Complete bounded extraction result."""

    pdf_path: str
    pages: tuple[PdfExtractedPage, ...]
    total_pages: int
    total_lines: int
    source_content_hash: str | None = None
    source_filename: str | None = None
    extraction_version: str = PDF_TEXT_EXTRACTION_VERSION
    warnings: tuple[str, ...] = ()
    success: bool = True
    error_message: str = ""
    error_code: PdfExtractionErrorCode | None = None


@dataclass(frozen=True)
class _OpenedPdfSource:
    content: bytes
    content_hash: str
    original_filename: str


_WorkerTarget = Callable[[Any, bytes, PdfResourceLimits], None]


def _failure(
    pdf_path: str,
    code: PdfExtractionErrorCode,
    *,
    source: _OpenedPdfSource | None = None,
) -> PdfExtractionResult:
    return PdfExtractionResult(
        pdf_path=pdf_path,
        pages=(),
        total_pages=0,
        total_lines=0,
        source_content_hash=source.content_hash if source else None,
        source_filename=source.original_filename if source else None,
        success=False,
        error_message=_ERROR_MESSAGES[code],
        error_code=code,
    )


def _open_pdf_source(
    requested_path: Path,
    *,
    limits: PdfResourceLimits,
) -> tuple[_OpenedPdfSource | None, PdfExtractionErrorCode | None]:
    """Open, bound, read, and hash an exact regular file without symlink following."""
    if requested_path.is_symlink():
        return None, PdfExtractionErrorCode.UNSAFE_PATH
    if not requested_path.exists():
        return None, PdfExtractionErrorCode.PATH_NOT_FOUND
    if not requested_path.is_file():
        return None, PdfExtractionErrorCode.NOT_REGULAR_FILE
    if requested_path.suffix.lower() != ".pdf":
        return None, PdfExtractionErrorCode.UNSUPPORTED_FILE_TYPE

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(requested_path, flags)
    except OSError:
        return None, PdfExtractionErrorCode.UNSAFE_PATH

    digest = hashlib.sha256()
    content = bytearray()
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return None, PdfExtractionErrorCode.NOT_REGULAR_FILE
        if metadata.st_size > limits.max_file_bytes:
            return None, PdfExtractionErrorCode.FILE_SIZE_LIMIT_EXCEEDED
        with os.fdopen(fd, "rb", closefd=True) as source_file:
            fd = -1
            while True:
                chunk = source_file.read(min(_MIB, limits.max_file_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                digest.update(chunk)
                if len(content) > limits.max_file_bytes:
                    return None, PdfExtractionErrorCode.FILE_SIZE_LIMIT_EXCEEDED
    except OSError:
        return None, PdfExtractionErrorCode.UNSAFE_PATH
    finally:
        if fd >= 0:
            os.close(fd)

    source_bytes = bytes(content)
    if not source_bytes.startswith(b"%PDF-"):
        return None, PdfExtractionErrorCode.INVALID_SIGNATURE
    return (
        _OpenedPdfSource(
            content=source_bytes,
            content_hash=digest.hexdigest(),
            original_filename=requested_path.name,
        ),
        None,
    )


def _apply_worker_memory_limit(limit_bytes: int) -> None:
    """Apply Linux RLIMIT_AS; other platforms rely on process isolation."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource
    except ImportError:
        return
    if not hasattr(resource, "RLIMIT_AS"):
        return
    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
    infinity = resource.RLIM_INFINITY
    requested = limit_bytes if current_hard == infinity else min(limit_bytes, current_hard)
    if current_soft != infinity:
        requested = min(requested, current_soft)
    resource.setrlimit(resource.RLIMIT_AS, (requested, requested))


def _pdf_worker_entry(
    connection: Any,
    source_content: bytes,
    limits: PdfResourceLimits,
) -> None:
    """Parse inside a disposable process and return only bounded plain data."""
    try:
        _apply_worker_memory_limit(limits.max_worker_memory_bytes)
        from io import BytesIO

        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(source_content), strict=True)
        except Exception:
            connection.send(
                {"success": False, "error_code": PdfExtractionErrorCode.MALFORMED_DOCUMENT.value}
            )
            return
        if reader.is_encrypted:
            connection.send(
                {"success": False, "error_code": PdfExtractionErrorCode.ENCRYPTED_DOCUMENT.value}
            )
            return
        try:
            page_count = len(reader.pages)
        except Exception:
            connection.send(
                {"success": False, "error_code": PdfExtractionErrorCode.MALFORMED_DOCUMENT.value}
            )
            return
        if page_count > limits.max_pages:
            connection.send(
                {"success": False, "error_code": PdfExtractionErrorCode.PAGE_LIMIT_EXCEEDED.value}
            )
            return

        extracted_pages: list[str] = []
        total_chars = 0
        for pdf_page in reader.pages:
            try:
                raw_text = (pdf_page.extract_text() or "").strip()
            except Exception:
                connection.send(
                    {
                        "success": False,
                        "error_code": PdfExtractionErrorCode.MALFORMED_DOCUMENT.value,
                    }
                )
                return
            page_chars = len(raw_text)
            if page_chars > limits.max_chars_per_page:
                connection.send(
                    {
                        "success": False,
                        "error_code": PdfExtractionErrorCode.PAGE_TEXT_LIMIT_EXCEEDED.value,
                    }
                )
                return
            total_chars += page_chars
            if total_chars > limits.max_total_chars:
                connection.send(
                    {
                        "success": False,
                        "error_code": PdfExtractionErrorCode.TOTAL_TEXT_LIMIT_EXCEEDED.value,
                    }
                )
                return
            extracted_pages.append(raw_text)
        if total_chars == 0:
            connection.send(
                {"success": False, "error_code": PdfExtractionErrorCode.NO_EXTRACTABLE_TEXT.value}
            )
            return
        connection.send({"success": True, "pages": extracted_pages})
    except MemoryError:
        try:
            connection.send(
                {
                    "success": False,
                    "error_code": PdfExtractionErrorCode.WORKER_RESOURCE_LIMIT_EXCEEDED.value,
                }
            )
        except Exception:
            pass
    except Exception:
        try:
            connection.send(
                {"success": False, "error_code": PdfExtractionErrorCode.WORKER_FAILURE.value}
            )
        except Exception:
            pass
    finally:
        connection.close()


def _stop_worker(process: Any) -> None:
    """Terminate and reap one worker without leaving a child process behind."""
    if process.is_alive():
        process.terminate()
        process.join(_WORKER_CLEANUP_SECONDS)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(_WORKER_CLEANUP_SECONDS)
    if not process.is_alive():
        process.join()
        process.close()


def _run_isolated_worker(
    source_content: bytes,
    *,
    limits: PdfResourceLimits,
    worker_target: _WorkerTarget,
) -> tuple[dict[str, Any] | None, PdfExtractionErrorCode | None]:
    """Run one parser worker with a real deadline and deterministic cleanup."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker_target, args=(sender, source_content, limits))
    process.daemon = False
    try:
        process.start()
    except Exception:
        receiver.close()
        sender.close()
        return None, PdfExtractionErrorCode.WORKER_FAILURE
    sender.close()

    deadline = time.monotonic() + float(limits.max_parse_seconds)
    payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        if receiver.poll(min(0.05, remaining)):
            try:
                received = receiver.recv()
                if isinstance(received, dict):
                    payload = received
            except (EOFError, OSError):
                payload = None
            break
        if not process.is_alive():
            break

    timed_out = process.is_alive() and payload is None and time.monotonic() >= deadline
    receiver.close()
    _stop_worker(process)
    if timed_out:
        return None, PdfExtractionErrorCode.PARSE_TIMEOUT
    if payload is None:
        return None, PdfExtractionErrorCode.WORKER_FAILURE
    return payload, None


def extract_text_from_pdf(
    pdf_path: str | Path,
    *,
    limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
    _worker_target: _WorkerTarget = _pdf_worker_entry,
) -> PdfExtractionResult:
    """Extract page text under the authoritative limits and a real deadline."""
    if not isinstance(limits, PdfResourceLimits):
        raise TypeError("limits must be a PdfResourceLimits instance")
    requested_path = Path(pdf_path).expanduser()
    evidence_path = str(requested_path.resolve(strict=False))

    source, source_error = _open_pdf_source(requested_path, limits=limits)
    if source_error is not None:
        return _failure(evidence_path, source_error)
    assert source is not None

    if importlib.util.find_spec("pypdf") is None:
        return _failure(
            evidence_path,
            PdfExtractionErrorCode.PARSER_UNAVAILABLE,
            source=source,
        )

    payload, worker_error = _run_isolated_worker(
        source.content,
        limits=limits,
        worker_target=_worker_target,
    )
    if worker_error is not None:
        return _failure(evidence_path, worker_error, source=source)
    assert payload is not None
    if not payload.get("success"):
        try:
            error_code = PdfExtractionErrorCode(str(payload.get("error_code")))
        except ValueError:
            error_code = PdfExtractionErrorCode.WORKER_FAILURE
        return _failure(evidence_path, error_code, source=source)

    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list) or not all(isinstance(page, str) for page in raw_pages):
        return _failure(
            evidence_path,
            PdfExtractionErrorCode.WORKER_FAILURE,
            source=source,
        )

    pages: list[PdfExtractedPage] = []
    total_lines = 0
    for page_number, raw_text in enumerate(raw_pages, start=1):
        lines = tuple(
            PdfExtractedLine(text=stripped, line_number=line_number)
            for line_number, raw_line in enumerate(raw_text.splitlines(), start=1)
            if (stripped := raw_line.strip())
        )
        total_lines += len(lines)
        warnings = ("No extractable text on this page.",) if not raw_text else ()
        pages.append(
            PdfExtractedPage(
                page_number=page_number,
                lines=lines,
                raw_text_block=raw_text,
                warnings=warnings,
            )
        )

    return PdfExtractionResult(
        pdf_path=evidence_path,
        pages=tuple(pages),
        total_pages=len(pages),
        total_lines=total_lines,
        source_content_hash=source.content_hash,
        source_filename=source.original_filename,
    )


def extract_text_lines(
    pdf_path: str | Path,
    *,
    limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
) -> list[str]:
    """Return all bounded extracted lines, or an empty list on failure."""
    result = extract_text_from_pdf(pdf_path, limits=limits)
    if not result.success:
        return []
    return [line.text for page in result.pages for line in page.lines]


__all__ = [
    "DEFAULT_PDF_RESOURCE_LIMITS",
    "PdfExtractedLine",
    "PdfExtractedPage",
    "PdfExtractionErrorCode",
    "PdfExtractionResult",
    "PdfResourceLimits",
    "extract_text_from_pdf",
    "extract_text_lines",
]
