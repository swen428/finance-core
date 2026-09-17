"""Concrete credential-safe Telegram Bot API HTTPS transport.

The transport is intentionally retry-free. It constructs both Telegram URLs
internally, disables environment proxy discovery, rejects redirects, bounds
metadata and headers, and never includes the bot token or a token-bearing URL
in public errors or object representation. Production urllib work runs in a
forked POSIX worker that the single-threaded parent actively terminates and
joins when the remaining monotonic deadline expires.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from finance_core.intake.telegram_attachment_acquisition import (
    InvalidAcquisitionConfigurationError,
    MalformedTelegramMetadataError,
    TelegramAttachmentAcquisitionError,
    TelegramDownloadResponse,
    TelegramDownloadTimeoutError,
    TelegramFileMetadata,
    TelegramHttpResponseError,
    TelegramMetadataRequestError,
    TelegramRedirectError,
    _validate_remote_file_path,
)

_TELEGRAM_API_HOST = "api.telegram.org"
_BOT_TOKEN_MAX_LENGTH = 256
_RETURNED_IDENTITY_MAX_LENGTH = 512
_WORKER_TERMINATION_GRACE_SECONDS = 0.25
_WORKER_NAME = "telegram-urllib-deadline-worker"

# Private fork-inherited test seam used only to audit sanitized IPC messages.
_worker_message_audit_hook: Callable[[tuple[Any, ...]], None] | None = None


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        raise TelegramRedirectError("Telegram Bot API redirects are forbidden.")


class _UrlLibDownloadResponse(TelegramDownloadResponse):
    def __init__(self, response: Any, *, max_header_bytes: int) -> None:
        self._response = response
        self._headers = _bounded_headers(response.headers, max_header_bytes=max_header_bytes)
        try:
            status = response.getcode()
        except Exception:
            raise TelegramHttpResponseError("Telegram response status is malformed.") from None
        if isinstance(status, bool) or not isinstance(status, int):
            response.close()
            raise TelegramHttpResponseError("Telegram response status is malformed.")
        self._status_code = status

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    def read(self, max_bytes: int, *, timeout_seconds: float) -> bytes:
        try:
            _set_response_socket_timeout(self._response, timeout_seconds)
            return self._response.read(max_bytes)
        except TimeoutError:
            raise
        except Exception:
            raise TelegramHttpResponseError("Telegram download stream failed.") from None

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:
            raise TelegramHttpResponseError(
                "Telegram download response could not be closed cleanly."
            ) from None


class _WorkerController:
    """Own one forked urllib operation and enforce its absolute deadline."""

    def __init__(
        self,
        context: Any,
        target: Any,
        args: tuple[Any, ...],
        *,
        deadline: float,
    ) -> None:
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = context.Process(
            target=target,
            args=(child_connection, *args),
            name=_WORKER_NAME,
        )
        self._connection = parent_connection
        self._process = process
        self.deadline = deadline
        self._closed = False
        try:
            process.start()
        except BaseException:
            parent_connection.close()
            child_connection.close()
            raise
        child_connection.close()

    def send(self, message: tuple[Any, ...]) -> None:
        if self._closed:
            raise TelegramHttpResponseError("Telegram network worker is already closed.")
        try:
            _audit_worker_message(message)
            self._connection.send(message)
        except Exception:
            self.cancel()
            raise TelegramHttpResponseError(
                "Telegram network worker communication failed."
            ) from None

    def receive(self, *, timeout_seconds: float | None = None) -> tuple[Any, ...]:
        remaining = self.deadline - time.monotonic()
        if timeout_seconds is not None:
            remaining = min(remaining, timeout_seconds)
        if not math.isfinite(remaining) or remaining <= 0:
            self.cancel()
            raise TimeoutError("Telegram network worker deadline expired.")
        try:
            ready = self._connection.poll(remaining)
        except Exception:
            self.cancel()
            raise TelegramHttpResponseError(
                "Telegram network worker communication failed."
            ) from None
        if not ready:
            self.cancel()
            raise TimeoutError("Telegram network worker deadline expired.")
        try:
            message = self._connection.recv()
        except (EOFError, OSError):
            self.cancel()
            raise TelegramHttpResponseError("Telegram network worker ended unexpectedly.") from None
        if not isinstance(message, tuple):
            self.cancel()
            raise TelegramHttpResponseError("Telegram network worker response is malformed.")
        return message

    def finish(self) -> None:
        if self._closed:
            return
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.cancel()
            raise TimeoutError("Telegram network worker deadline expired.")
        self._process.join(remaining)
        if self._process.is_alive():
            self.cancel()
            raise TimeoutError("Telegram network worker deadline expired.")
        self._close_handles()

    def cancel(self) -> None:
        if self._closed:
            return
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(_WORKER_TERMINATION_GRACE_SECONDS)
        if self._process.is_alive():
            self._process.kill()
            self._process.join()
        self._close_handles()

    def _close_handles(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()
        self._process.close()


class _ProcessDownloadResponse(TelegramDownloadResponse):
    """Parent-side bounded streaming proxy for one cancellable urllib worker."""

    def __init__(
        self,
        worker: _WorkerController,
        *,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        self._worker = worker
        self._status_code = status_code
        self._headers = dict(headers)
        self._closed = False

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    def read(self, max_bytes: int, *, timeout_seconds: float) -> bytes:
        if self._closed:
            return b""
        _validate_positive_timeout(timeout_seconds)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise TelegramHttpResponseError("Telegram download read size is invalid.")
        remaining = min(timeout_seconds, self._worker.deadline - time.monotonic())
        if remaining <= 0:
            self._closed = True
            self._worker.cancel()
            raise TimeoutError("Telegram download deadline expired.")
        self._worker.send(("read", max_bytes, remaining))
        try:
            message = self._worker.receive(timeout_seconds=remaining)
        except BaseException:
            self._closed = True
            raise
        if not message:
            self._closed = True
            self._worker.cancel()
            raise TelegramHttpResponseError("Telegram network worker response is malformed.")
        if message[0] == "error":
            self._closed = True
            self._worker.finish()
            _raise_worker_error(message)
        if message[0] != "data" or len(message) != 2 or not isinstance(message[1], bytes):
            self._closed = True
            self._worker.cancel()
            raise TelegramHttpResponseError("Telegram network worker response is malformed.")
        chunk = message[1]
        if len(chunk) > max_bytes:
            self._closed = True
            self._worker.cancel()
            raise TelegramHttpResponseError("Telegram network worker exceeded the read bound.")
        if not chunk:
            self._closed = True
            self._worker.finish()
        return chunk

    def close(self) -> None:
        if self._closed:
            return
        remaining = self._worker.deadline - time.monotonic()
        if remaining <= 0:
            self._closed = True
            self._worker.cancel()
            raise TelegramDownloadTimeoutError(
                "Telegram download response close exceeded the acquisition deadline."
            )
        self._worker.send(("close", remaining))
        try:
            message = self._worker.receive(timeout_seconds=remaining)
            if message[0] == "error":
                self._worker.finish()
                _raise_worker_error(message)
            if message != ("closed",):
                raise TelegramHttpResponseError(
                    "Telegram network worker close response is malformed."
                )
            self._worker.finish()
        except TimeoutError as exc:
            raise TelegramDownloadTimeoutError(
                "Telegram download response close exceeded the acquisition deadline."
            ) from exc
        except BaseException:
            self._worker.cancel()
            raise
        finally:
            self._closed = True


class TelegramBotApiTransport:
    """POSIX HTTPS-only Telegram adapter with active deadline cancellation."""

    def __init__(
        self,
        bot_token: str,
        *,
        _opener: OpenerDirector | Any | None = None,
        _unsafe_inline_for_tests: bool = False,
        _process_context: Any | None = None,
    ) -> None:
        _validate_bot_token(bot_token)
        self._bot_token = bot_token
        self._unsafe_inline_for_tests = _unsafe_inline_for_tests
        self._opener = (
            _opener
            if _opener is not None
            else (_build_secure_opener() if _unsafe_inline_for_tests else None)
        )
        self._process_context = _process_context or _get_fork_context()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_host='{_TELEGRAM_API_HOST}', bot_token=<redacted>)"

    def get_file_metadata(
        self,
        file_id: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_header_bytes: int,
    ) -> TelegramFileMetadata:
        _validate_positive_timeout(timeout_seconds)
        if self._unsafe_inline_for_tests:
            return self._get_file_metadata_inline(
                file_id,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
                max_header_bytes=max_header_bytes,
            )
        _require_fork_safe_process()
        deadline = time.monotonic() + timeout_seconds
        try:
            worker = _WorkerController(
                self._process_context,
                _metadata_worker,
                (
                    self._opener,
                    self._bot_token,
                    file_id,
                    timeout_seconds,
                    max_response_bytes,
                    max_header_bytes,
                ),
                deadline=deadline,
            )
        except Exception:
            raise TelegramMetadataRequestError(
                "Telegram metadata worker could not be started."
            ) from None
        try:
            message = worker.receive()
            worker.finish()
        except TimeoutError:
            raise
        except TelegramAttachmentAcquisitionError:
            raise
        except Exception:
            worker.cancel()
            raise TelegramMetadataRequestError("Telegram metadata request failed.") from None
        if not message:
            raise TelegramMetadataRequestError("Telegram metadata worker response is malformed.")
        if message[0] == "error":
            _raise_worker_error(message)
        if message[0] != "metadata" or len(message) != 2 or not isinstance(message[1], dict):
            raise TelegramMetadataRequestError("Telegram metadata worker response is malformed.")
        payload = message[1]
        return TelegramFileMetadata(
            file_path=payload["file_path"],
            file_size=payload["file_size"],
            file_id=payload["file_id"],
            file_unique_id=payload["file_unique_id"],
        )

    def _get_file_metadata_inline(
        self,
        file_id: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_header_bytes: int,
    ) -> TelegramFileMetadata:
        encoded_file_id = quote(file_id, safe="")
        url = f"https://{_TELEGRAM_API_HOST}/bot{self._bot_token}/getFile?file_id={encoded_file_id}"
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        response = self._open(request, timeout_seconds=timeout_seconds, metadata=True)
        try:
            status = response.getcode()
            if isinstance(status, bool) or not isinstance(status, int):
                raise TelegramMetadataRequestError(
                    "Telegram metadata response status is malformed."
                )
            if 300 <= status < 400:
                raise TelegramRedirectError("Telegram Bot API redirects are forbidden.")
            if not 200 <= status < 300:
                raise TelegramMetadataRequestError(
                    f"Telegram metadata request returned HTTP status {status}."
                )
            _bounded_headers(response.headers, max_header_bytes=max_header_bytes)
            body = response.read(max_response_bytes + 1)
            if not isinstance(body, bytes):
                raise MalformedTelegramMetadataError(
                    "Telegram metadata response body is malformed."
                )
            if len(body) > max_response_bytes:
                raise MalformedTelegramMetadataError(
                    "Telegram metadata response exceeds the byte limit."
                )
        except TelegramAttachmentAcquisitionError:
            raise
        except Exception:
            raise TelegramMetadataRequestError("Telegram metadata response failed.") from None
        finally:
            try:
                response.close()
            except Exception:
                pass
        return _decode_metadata(body)

    def open_file_download(
        self,
        file_path: str,
        *,
        timeout_seconds: float,
        max_header_bytes: int,
    ) -> TelegramDownloadResponse:
        _validate_positive_timeout(timeout_seconds)
        validated = _validate_remote_file_path(file_path, max_length=4_096)
        if self._unsafe_inline_for_tests:
            return self._open_file_download_inline(
                validated,
                timeout_seconds=timeout_seconds,
                max_header_bytes=max_header_bytes,
            )
        _require_fork_safe_process()
        deadline = time.monotonic() + timeout_seconds
        try:
            worker = _WorkerController(
                self._process_context,
                _download_worker,
                (
                    self._opener,
                    self._bot_token,
                    validated,
                    timeout_seconds,
                    max_header_bytes,
                ),
                deadline=deadline,
            )
        except Exception:
            raise TelegramHttpResponseError(
                "Telegram download worker could not be started."
            ) from None
        try:
            message = worker.receive()
        except TimeoutError:
            raise
        except TelegramAttachmentAcquisitionError:
            raise
        except Exception:
            worker.cancel()
            raise TelegramHttpResponseError("Telegram download request failed.") from None
        if not message:
            worker.cancel()
            raise TelegramHttpResponseError("Telegram download worker response is malformed.")
        if message[0] == "error":
            worker.finish()
            _raise_worker_error(message)
        if (
            message[0] != "download"
            or len(message) != 3
            or isinstance(message[1], bool)
            or not isinstance(message[1], int)
            or not isinstance(message[2], dict)
        ):
            worker.cancel()
            raise TelegramHttpResponseError("Telegram download worker response is malformed.")
        return _ProcessDownloadResponse(
            worker,
            status_code=message[1],
            headers=message[2],
        )

    def _open_file_download_inline(
        self,
        file_path: str,
        *,
        timeout_seconds: float,
        max_header_bytes: int,
    ) -> TelegramDownloadResponse:
        validated = _validate_remote_file_path(file_path, max_length=4_096)
        encoded_path = "/".join(quote(component, safe="") for component in validated.split("/"))
        url = f"https://{_TELEGRAM_API_HOST}/file/bot{self._bot_token}/{encoded_path}"
        request = Request(
            url,
            headers={"Accept": "application/octet-stream"},
            method="GET",
        )
        response = self._open(request, timeout_seconds=timeout_seconds, metadata=False)
        try:
            return _UrlLibDownloadResponse(response, max_header_bytes=max_header_bytes)
        except BaseException:
            try:
                response.close()
            except Exception:
                pass
            raise

    def _open(self, request: Request, *, timeout_seconds: float, metadata: bool) -> Any:
        opener = self._opener
        if opener is None:
            raise InvalidAcquisitionConfigurationError(
                "Inline Telegram transport requires an injected secure opener."
            )
        try:
            return opener.open(request, timeout=timeout_seconds)
        except TelegramRedirectError:
            raise
        except HTTPError as exc:
            try:
                code = int(exc.code)
            finally:
                exc.close()
            if 300 <= code < 400:
                raise TelegramRedirectError("Telegram Bot API redirects are forbidden.") from None
            if metadata:
                raise TelegramMetadataRequestError(
                    f"Telegram metadata request returned HTTP status {code}."
                ) from None
            raise TelegramHttpResponseError(
                f"Telegram file download returned HTTP status {code}."
            ) from None
        except TimeoutError:
            raise
        except Exception:
            if metadata:
                raise TelegramMetadataRequestError("Telegram metadata request failed.") from None
            raise TelegramHttpResponseError("Telegram file download request failed.") from None


def _get_fork_context() -> Any:
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        raise InvalidAcquisitionConfigurationError(
            "TelegramBotApiTransport requires POSIX fork support for cancellable deadlines."
        ) from None


def _require_fork_safe_process() -> None:
    if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
        raise InvalidAcquisitionConfigurationError(
            "Cancellable Telegram transport requires a single-threaded POSIX caller."
        )


def _validate_positive_timeout(timeout_seconds: object) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise InvalidAcquisitionConfigurationError(
            "Telegram transport timeout must be a finite positive number."
        )
    numeric = float(timeout_seconds)
    if not math.isfinite(numeric) or numeric <= 0:
        raise InvalidAcquisitionConfigurationError(
            "Telegram transport timeout must be a finite positive number."
        )


def _metadata_worker(
    connection: Any,
    opener: Any,
    bot_token: str,
    file_id: str,
    timeout_seconds: float,
    max_response_bytes: int,
    max_header_bytes: int,
) -> None:
    try:
        transport = TelegramBotApiTransport(
            bot_token,
            _opener=opener,
            _unsafe_inline_for_tests=True,
            _process_context=multiprocessing.get_context("fork"),
        )
        metadata = transport._get_file_metadata_inline(
            file_id,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            max_header_bytes=max_header_bytes,
        )
        _send_worker_message(
            connection,
            (
                "metadata",
                {
                    "file_path": metadata.file_path,
                    "file_size": metadata.file_size,
                    "file_id": metadata.file_id,
                    "file_unique_id": metadata.file_unique_id,
                },
            ),
        )
    except BaseException as exc:
        _send_worker_error(connection, exc, metadata=True)
    finally:
        connection.close()


def _download_worker(
    connection: Any,
    opener: Any,
    bot_token: str,
    file_path: str,
    timeout_seconds: float,
    max_header_bytes: int,
) -> None:
    response: TelegramDownloadResponse | None = None
    try:
        transport = TelegramBotApiTransport(
            bot_token,
            _opener=opener,
            _unsafe_inline_for_tests=True,
            _process_context=multiprocessing.get_context("fork"),
        )
        response = transport._open_file_download_inline(
            file_path,
            timeout_seconds=timeout_seconds,
            max_header_bytes=max_header_bytes,
        )
        _send_worker_message(
            connection,
            ("download", response.status_code, dict(response.headers)),
        )
        while True:
            command = connection.recv()
            if not isinstance(command, tuple) or not command or command[0] not in {"read", "close"}:
                raise TelegramHttpResponseError("Telegram worker command is malformed.")
            if command[0] == "close":
                response.close()
                response = None
                _send_worker_message(connection, ("closed",))
                return
            if (
                len(command) != 3
                or isinstance(command[1], bool)
                or not isinstance(command[1], int)
                or command[1] <= 0
            ):
                raise TelegramHttpResponseError("Telegram worker read command is malformed.")
            _validate_positive_timeout(command[2])
            chunk = response.read(command[1], timeout_seconds=float(command[2]))
            if not isinstance(chunk, bytes) or len(chunk) > command[1]:
                raise TelegramHttpResponseError("Telegram worker read result is malformed.")
            if not chunk:
                response.close()
                response = None
                _send_worker_message(connection, ("data", b""))
                return
            _send_worker_message(connection, ("data", chunk))
    except BaseException as exc:
        if response is not None:
            try:
                response.close()
            except BaseException:
                pass
        _send_worker_error(connection, exc, metadata=False)
    finally:
        connection.close()


def _send_worker_message(connection: Any, message: tuple[Any, ...]) -> None:
    try:
        _audit_worker_message(message)
        connection.send(message)
    except BaseException:
        pass


def _audit_worker_message(message: tuple[Any, ...]) -> None:
    if _worker_message_audit_hook is None:
        return
    try:
        _worker_message_audit_hook(message)
    except BaseException:
        pass


def _send_worker_error(connection: Any, error: BaseException, *, metadata: bool) -> None:
    if isinstance(error, TimeoutError):
        code = "timeout"
    elif isinstance(error, TelegramRedirectError):
        code = "redirect"
    elif isinstance(error, MalformedTelegramMetadataError):
        code = "malformed_metadata"
    elif isinstance(error, InvalidAcquisitionConfigurationError):
        code = "configuration"
    elif isinstance(error, TelegramMetadataRequestError):
        code = "metadata"
    elif isinstance(error, TelegramHttpResponseError):
        code = "http"
    else:
        code = "metadata" if metadata else "http"
    _send_worker_message(connection, ("error", code))


def _raise_worker_error(message: tuple[Any, ...]) -> None:
    if len(message) != 2 or not isinstance(message[1], str):
        raise TelegramHttpResponseError("Telegram network worker error is malformed.")
    code = message[1]
    if code == "timeout":
        raise TimeoutError("Telegram network operation exceeded its absolute deadline.")
    if code == "redirect":
        raise TelegramRedirectError("Telegram Bot API redirects are forbidden.")
    if code == "malformed_metadata":
        raise MalformedTelegramMetadataError("Telegram metadata response is malformed.")
    if code == "configuration":
        raise InvalidAcquisitionConfigurationError(
            "Telegram network worker rejected its configuration."
        )
    if code == "metadata":
        raise TelegramMetadataRequestError("Telegram metadata request failed.")
    if code == "http":
        raise TelegramHttpResponseError("Telegram file download request failed.")
    raise TelegramHttpResponseError("Telegram network worker error is malformed.")


def _build_secure_opener() -> OpenerDirector:
    context = ssl.create_default_context()
    return build_opener(
        ProxyHandler({}),
        _RejectRedirectHandler(),
        HTTPSHandler(context=context),
    )


def _validate_bot_token(bot_token: object) -> None:
    if not isinstance(bot_token, str):
        raise InvalidAcquisitionConfigurationError("Telegram bot token must be a string.")
    if (
        not bot_token
        or bot_token.strip() != bot_token
        or len(bot_token) > _BOT_TOKEN_MAX_LENGTH
        or not bot_token.isascii()
        or "/" in bot_token
        or "?" in bot_token
        or "#" in bot_token
        or any(character.isspace() or ord(character) < 0x20 for character in bot_token)
    ):
        raise InvalidAcquisitionConfigurationError("Telegram bot token is malformed.")


def _bounded_headers(headers: Any, *, max_header_bytes: int) -> Mapping[str, str]:
    try:
        names = list(headers.keys())
    except Exception as exc:
        raise TelegramHttpResponseError("Telegram response headers are malformed.") from exc
    bounded: dict[str, str] = {}
    total = 0
    for raw_name in names:
        name = str(raw_name)
        values = headers.get_all(raw_name) if hasattr(headers, "get_all") else None
        if values is None:
            values = [headers[raw_name]]
        string_values = [str(value) for value in values]
        value = ",".join(string_values)
        total += len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
        if total > max_header_bytes:
            raise TelegramHttpResponseError("Telegram response headers exceed the byte limit.")
        lowered = name.lower()
        if lowered in bounded:
            raise TelegramHttpResponseError("Telegram response contains duplicate headers.")
        bounded[lowered] = value
    return bounded


def _decode_metadata(body: bytes) -> TelegramFileMetadata:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedTelegramMetadataError("Telegram metadata JSON is malformed.") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise MalformedTelegramMetadataError("Telegram metadata did not report ok=true.")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise MalformedTelegramMetadataError("Telegram metadata result is malformed.")
    file_path = result.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise MalformedTelegramMetadataError("Telegram metadata is missing file_path.")
    file_path = _validate_remote_file_path(file_path, max_length=4_096)
    file_size = result.get("file_size")
    if file_size is not None and (
        isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0
    ):
        raise MalformedTelegramMetadataError("Telegram metadata file_size is malformed.")
    file_id = result.get("file_id")
    if file_id is not None and not _valid_returned_identity(file_id):
        raise MalformedTelegramMetadataError("Telegram metadata file_id is malformed.")
    file_unique_id = result.get("file_unique_id")
    if file_unique_id is not None and not _valid_returned_identity(file_unique_id):
        raise MalformedTelegramMetadataError("Telegram metadata file_unique_id is malformed.")
    return TelegramFileMetadata(
        file_path=file_path,
        file_size=file_size,
        file_id=file_id,
        file_unique_id=file_unique_id,
    )


def _valid_returned_identity(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value.strip() == value
        and len(value) <= _RETURNED_IDENTITY_MAX_LENGTH
        and value.isascii()
        and not any(character.isspace() or ord(character) < 0x20 for character in value)
    )


def _set_response_socket_timeout(response: Any, timeout_seconds: float) -> None:
    """Best-effort per-read remaining-time propagation for urllib responses."""
    candidates = [
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
    ]
    for candidate in candidates:
        sock = getattr(candidate, "_sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(timeout_seconds)
            return


__all__ = ["TelegramBotApiTransport"]
