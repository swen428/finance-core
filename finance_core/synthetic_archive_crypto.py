"""Bounded, synthetic-only age/HMAC proof; this is not a backup entry point."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import selectors
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FRAME_BYTES = MAX_ARCHIVE_BYTES + MAX_MANIFEST_BYTES + 88
MAX_CIPHERTEXT_BYTES = MAX_FRAME_BYTES + 1024 * 1024
MAX_SIDECAR_BYTES = 4096
MAX_TOOL_BYTES = 64 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 128
MAX_STDERR_BYTES = 4096
_FRAME_MAGIC = b"FCD4AGE1"
_MAC_DOMAIN = b"finance-d4-synthetic-age-sidecar-v1\x00"
_FORMAT = "finance-d4-synthetic-age-v1"
_FIELDS = frozenset(
    {
        "format",
        "archive_id",
        "context_id",
        "key_epoch",
        "recipient_key_id",
        "auth_key_id",
        "ciphertext_length",
        "ciphertext_sha256",
        "mac_sha256",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}\Z", re.ASCII)
_IDENTITY = re.compile(rb"AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}\n\Z", re.ASCII)


class SyntheticArchiveError(ValueError):
    """Refusal without subprocess output, payload, or key material."""


@dataclass(frozen=True)
class AgeToolPin:
    executable: Path
    sha256: str
    platform: str
    release: str = "v1.3.2"
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ArchiveContext:
    archive_id: str
    context_id: str
    key_epoch: int
    recipient_key_id: str
    auth_key_id: str


def _refuse() -> SyntheticArchiveError:
    return SyntheticArchiveError("Synthetic archive verification failed")


def _runtime_platform() -> str:
    machine = platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    return f"{sys.platform}-{machine}"


def _require_bytes(value: object, limit: int) -> bytes:
    if type(value) is not bytes or len(value) > limit:
        raise _refuse()
    return value


def _require_context(context: ArchiveContext) -> dict[str, str | int]:
    if not isinstance(context, ArchiveContext):
        raise _refuse()
    values = {
        "archive_id": context.archive_id,
        "context_id": context.context_id,
        "recipient_key_id": context.recipient_key_id,
        "auth_key_id": context.auth_key_id,
    }
    if any(type(value) is not str or not _IDENTIFIER.fullmatch(value) for value in values.values()):
        raise _refuse()
    if type(context.key_epoch) is not int or not 1 <= context.key_epoch <= 2**31 - 1:
        raise _refuse()
    return {**values, "key_epoch": context.key_epoch}


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_tool(pin: AgeToolPin) -> None:
    if not isinstance(pin, AgeToolPin):
        raise _refuse()
    path = pin.executable
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or pin.release != "v1.3.2"
        or pin.platform != _runtime_platform()
        or type(pin.sha256) is not str
        or not _HEX64.fullmatch(pin.sha256)
        or type(pin.timeout_seconds) not in (int, float)
        or not 0 < pin.timeout_seconds <= 30
    ):
        raise _refuse()
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or not info.st_mode & 0o111
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or info.st_uid not in (0, os.geteuid())
        ):
            raise _refuse()
        if info.st_size > MAX_TOOL_BYTES:
            raise _refuse()
        digest = hashlib.sha256()
        while chunk := os.read(fd, 65536):
            digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), pin.sha256):
            raise _refuse()
    except (OSError, ValueError):
        raise _refuse() from None
    finally:
        if fd >= 0:
            os.close(fd)
    version = _run_bounded(
        [str(path), "--version"], b"", MAX_TOOL_OUTPUT_BYTES, pin.timeout_seconds
    )
    if version.strip() != pin.release.encode("ascii"):
        raise _refuse()


def _run_bounded(
    command: list[str],
    input_bytes: bytes,
    output_limit: int,
    timeout_seconds: float,
    *,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    """Drain every child pipe with hard caps; return stdout only after exit zero."""
    child: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    stderr_size = 0
    cursor = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=pass_fds,
        )
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        for pipe in (child.stdin, child.stdout, child.stderr):
            os.set_blocking(pipe.fileno(), False)
        if input_bytes:
            selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            child.stdin.close()
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _refuse()
            for key, _ in selector.select(remaining):
                stream = cast(BinaryIO, key.fileobj)
                if key.data == "stdin":
                    try:
                        written = os.write(stream.fileno(), input_bytes[cursor : cursor + 65536])
                    except BrokenPipeError:
                        written = 0
                        cursor = len(input_bytes)
                    cursor += written
                    if cursor >= len(input_bytes):
                        selector.unregister(stream)
                        stream.close()
                else:
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                    elif key.data == "stdout":
                        if len(output) + len(chunk) > output_limit:
                            raise _refuse()
                        output.extend(chunk)
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > MAX_STDERR_BYTES:
                            raise _refuse()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            raise _refuse()
        return bytes(output)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        output.clear()
        raise _refuse() from None
    finally:
        selector.close()
        if child is not None:
            if child.poll() is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            child.wait()
            for final_pipe in (child.stdin, child.stdout, child.stderr):
                if final_pipe is not None:
                    final_pipe.close()


def _frame(manifest: bytes, archive: bytes) -> bytes:
    return (
        _FRAME_MAGIC
        + struct.pack(">QQ", len(manifest), len(archive))
        + hashlib.sha256(manifest).digest()
        + hashlib.sha256(archive).digest()
        + manifest
        + archive
    )


def _unframe(payload: bytes) -> tuple[bytes, bytes]:
    if len(payload) < 88 or payload[:8] != _FRAME_MAGIC:
        raise _refuse()
    manifest_size, archive_size = struct.unpack(">QQ", payload[8:24])
    if (
        manifest_size > MAX_MANIFEST_BYTES
        or archive_size > MAX_ARCHIVE_BYTES
        or len(payload) != 88 + manifest_size + archive_size
    ):
        raise _refuse()
    manifest = payload[88 : 88 + manifest_size]
    archive = payload[88 + manifest_size :]
    if not hmac.compare_digest(hashlib.sha256(manifest).digest(), payload[24:56]):
        raise _refuse()
    if not hmac.compare_digest(hashlib.sha256(archive).digest(), payload[56:88]):
        raise _refuse()
    return archive, manifest


def _sidecar_fields(context: ArchiveContext, ciphertext: bytes) -> dict[str, str | int]:
    return {
        "format": _FORMAT,
        **_require_context(context),
        "ciphertext_length": len(ciphertext),
        "ciphertext_sha256": _sha256(ciphertext),
    }


def _parse_sidecar(sidecar: bytes) -> dict[str, Any]:
    if not sidecar or len(sidecar) > MAX_SIDECAR_BYTES:
        raise _refuse()

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise _refuse()
            result[name] = value
        return result

    try:
        decoded = json.loads(sidecar.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError, TypeError):
        raise _refuse() from None
    if type(decoded) is not dict or decoded.keys() != _FIELDS or _canonical(decoded) != sidecar:
        raise _refuse()
    if type(decoded["mac_sha256"]) is not str or not _HEX64.fullmatch(decoded["mac_sha256"]):
        raise _refuse()
    return decoded


def seal_synthetic_archive(
    *,
    archive: bytes,
    manifest: bytes,
    context: ArchiveContext,
    recipient: str,
    auth_key: bytes,
    tool: AgeToolPin,
) -> tuple[bytes, bytes]:
    """Return ciphertext and a separate authenticated sidecar, without publishing either."""
    archive = _require_bytes(archive, MAX_ARCHIVE_BYTES)
    manifest = _require_bytes(manifest, MAX_MANIFEST_BYTES)
    _require_context(context)
    if type(recipient) is not str or not _RECIPIENT.fullmatch(recipient):
        raise _refuse()
    auth_key = _require_bytes(auth_key, 32)
    if len(auth_key) != 32:
        raise _refuse()
    _validate_tool(tool)
    ciphertext = _run_bounded(
        [str(tool.executable), "-e", "-r", recipient],
        _frame(manifest, archive),
        MAX_CIPHERTEXT_BYTES,
        tool.timeout_seconds,
    )
    fields = _sidecar_fields(context, ciphertext)
    mac = hmac.new(auth_key, _MAC_DOMAIN + _canonical(fields), hashlib.sha256).hexdigest()
    return ciphertext, _canonical({**fields, "mac_sha256": mac})


def open_synthetic_archive(
    *,
    ciphertext: bytes,
    sidecar: bytes,
    expected_context: ArchiveContext,
    identity: bytes,
    auth_key: bytes,
    tool: AgeToolPin,
) -> tuple[bytes, bytes]:
    """Authenticate external context and ciphertext before age, then validate the full frame."""
    ciphertext = _require_bytes(ciphertext, MAX_CIPHERTEXT_BYTES)
    sidecar = _require_bytes(sidecar, MAX_SIDECAR_BYTES)
    identity = _require_bytes(identity, 256)
    auth_key = _require_bytes(auth_key, 32)
    if not _IDENTITY.fullmatch(identity) or len(auth_key) != 32:
        raise _refuse()
    expected = _sidecar_fields(expected_context, ciphertext)
    decoded = _parse_sidecar(sidecar)
    if any(
        type(decoded[name]) is not type(value) or decoded[name] != value
        for name, value in expected.items()
    ):
        raise _refuse()
    _validate_tool(tool)
    mac = hmac.new(auth_key, _MAC_DOMAIN + _canonical(expected), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, decoded["mac_sha256"]):
        raise _refuse()
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        raise _refuse() from None
    try:
        if os.write(write_fd, identity) != len(identity):
            raise _refuse()
        os.close(write_fd)
        write_fd = -1
        payload = _run_bounded(
            [str(tool.executable), "-d", "-i", f"/dev/fd/{read_fd}"],
            ciphertext,
            MAX_FRAME_BYTES,
            tool.timeout_seconds,
            pass_fds=(read_fd,),
        )
    except OSError:
        raise _refuse() from None
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    return _unframe(payload)
