"""Bounded one-request/one-response JSON CLI for the OpenClaw staging bridge.

Invocation::

    <repo>/.venv/bin/python -m finance_core.openclaw_staging_bridge.cli

Exactly one v1 request envelope is read from stdin (closed after write);
exactly one v1 response envelope is written to stdout; diagnostics are
bounded and written only to stderr.  Exit codes follow design §5.4.  The CLI
performs no network I/O, reads no credentials, and never opens the live
database: every connection goes through the staging guard.
"""

from __future__ import annotations

import sys
from typing import BinaryIO, TextIO

from finance_core.openclaw_staging_bridge import commands, envelope, errors, identity

DEFAULT_DEADLINE_SECONDS = commands.DEFAULT_DEADLINE_SECONDS

_MAX_STDERR_DIAGNOSTIC_LENGTH = 500


def _bounded_diagnostic(message: str) -> str:
    text = message.replace("\n", " ").strip()
    if len(text) > _MAX_STDERR_DIAGNOSTIC_LENGTH:
        return text[:_MAX_STDERR_DIAGNOSTIC_LENGTH]
    return text


def _emit(response: dict[str, object], exit_code: int, stdout: TextIO) -> int:
    try:
        serialized = envelope.serialize_response(response)
    except errors.BridgeError as oversized:
        fallback = envelope.error_response(
            request_id=None,
            operation_id=None,
            error=oversized,
        )
        serialized = envelope.serialize_response(fallback)
        exit_code = oversized.exit_code
    stdout.write(serialized + "\n")
    stdout.flush()
    return exit_code


def execute_stream(
    stdin: BinaryIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> int:
    """Run exactly one request/response exchange and return the exit code."""
    try:
        raw = stdin.read(envelope.MAX_REQUEST_BYTES + 1)
    except Exception as exc:  # pragma: no cover - OS-level stdin failure
        error = errors.bridge_error(
            errors.MALFORMED_ENVELOPE,
            "Request envelope could not be read from stdin.",
            errors.EXIT_MALFORMED_ENVELOPE,
        )
        stderr.write(_bounded_diagnostic(f"{error.code}: {exc}") + "\n")
        return _emit(
            envelope.error_response(request_id=None, operation_id=None, error=error),
            error.exit_code,
            stdout,
        )

    request: envelope.BridgeRequest | None = None
    operation_id: str | None = None
    try:
        request = envelope.parse_request(raw)
        operation_id = identity.operation_id(
            request.command, request.idempotency_key, request.arguments
        )
        deadline = commands.Deadline(deadline_seconds=deadline_seconds)
        result, idempotent_replay = commands.dispatch(request, deadline)
        response = envelope.success_response(
            request_id=request.request_id,
            operation_id=operation_id,
            result=result,
            idempotent_replay=idempotent_replay,
        )
        return _emit(response, errors.EXIT_OK, stdout)
    except errors.BridgeError as bridge_refusal:
        error = bridge_refusal
        stderr.write(_bounded_diagnostic(str(error)) + "\n")
        response = envelope.error_response(
            request_id=request.request_id if request is not None else None,
            operation_id=operation_id,
            error=error,
        )
        return _emit(response, error.exit_code, stdout)
    except Exception as exc:
        error = errors.bridge_error(
            errors.INTERNAL_ERROR,
            f"Unexpected bridge failure: {type(exc).__name__}",
            errors.EXIT_INTERNAL,
        )
        stderr.write(_bounded_diagnostic(f"{error.code}: {type(exc).__name__}") + "\n")
        response = envelope.error_response(
            request_id=request.request_id if request is not None else None,
            operation_id=operation_id,
            error=error,
        )
        return _emit(response, error.exit_code, stdout)


def main() -> int:
    return execute_stream(sys.stdin.buffer, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
