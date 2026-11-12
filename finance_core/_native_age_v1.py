"""Pinned native age X25519 subprocess boundary; no file or runtime authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import selectors
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO, cast

from finance_core.profile_paths import ProfilePathError, _reject_acl_grants, _trusted_ancestor

_RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}\Z", re.ASCII)
_IDENTITY = re.compile(rb"AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}\n\Z")


class NativeAgeError(ValueError):
    """Refusal that never includes child output, plaintext, or identities."""


@dataclass(frozen=True)
class NativeAgeToolchainV1:
    age: Path
    age_keygen: Path
    platform: str
    release: str = "v1.3.2"


def _refuse() -> NativeAgeError:
    return NativeAgeError("Native age verification failed")


def _deadline(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 120:
        raise _refuse()


def _identity(identity: bytes) -> bytes:
    if type(identity) is not bytes or not _IDENTITY.fullmatch(identity):
        raise _refuse()
    return identity


def validate_recipient_v1(recipient: str) -> str:
    if type(recipient) is not str or not _RECIPIENT.fullmatch(recipient):
        raise _refuse()
    return recipient


def _run_bounded(
    command: list[str],
    input_bytes: bytes,
    output_limit: int,
    deadline_seconds: int,
    *,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    """Bound every pipe and wait; release output only after exit and actual close."""
    _deadline(deadline_seconds)
    if type(output_limit) is not int or not 1 <= output_limit <= 82 * 1024 * 1024:
        raise _refuse()
    child = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    cursor = 0
    stderr_size = 0
    deadline = time.monotonic() + deadline_seconds
    failed = False
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=pass_fds,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/nonexistent"},
        )
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        for stream in (child.stdin, child.stdout, child.stderr):
            os.set_blocking(stream.fileno(), False)
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
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        raise _refuse() from None
                    if written <= 0:
                        raise _refuse()
                    cursor += written
                    if cursor == len(input_bytes):
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
                        if len(chunk) > output_limit - len(output):
                            raise _refuse()
                        output.extend(chunk)
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > 4096:
                            raise _refuse()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0 or cursor != len(input_bytes):
            raise _refuse()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        failed = True
    finally:
        try:
            selector.close()
        except OSError:
            failed = True
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                failed = True
            for final_stream in (child.stdin, child.stdout, child.stderr):
                if final_stream is not None:
                    try:
                        final_stream.close()
                    except OSError:
                        failed = True
    if failed:
        output.clear()
        raise _refuse() from None
    return bytes(output)


def _host_platform() -> str:
    machine = platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    return f"{sys.platform}-{machine}"


def _registry(toolchain: NativeAgeToolchainV1) -> dict:
    if (
        type(toolchain) is not NativeAgeToolchainV1
        or toolchain.release != "v1.3.2"
        or toolchain.platform not in {"darwin-arm64", "linux-x86_64"}
        or toolchain.platform != _host_platform()
    ):
        raise _refuse()
    try:
        resource = files("finance_core.resources").joinpath("crypto/age_toolchain_v1.json")
        with resource.open("rb") as registry_file:
            raw = registry_file.read(65537)
        if len(raw) > 65536:
            raise _refuse()
        registry = json.loads(raw)
        if (
            type(registry) is not dict
            or set(registry) != {"format", "release", "source_commit", "platforms"}
            or registry["format"] != "native-age-toolchain-v1"
            or registry["release"] != "v1.3.2"
            or registry["source_commit"] != "b74dce4cdbe35b5e5f66c06d9612b72f89028758"
            or set(registry["platforms"]) != {"darwin-arm64", "linux-x86_64"}
        ):
            raise _refuse()
        selected = registry["platforms"][toolchain.platform]
        if type(selected) is not dict or set(selected) != {"age", "age_keygen"}:
            raise _refuse()
        for pin in selected.values():
            if (
                type(pin) is not dict
                or set(pin) != {"sha256", "size_bytes"}
                or type(pin["size_bytes"]) is not int
                or not 1 <= pin["size_bytes"] <= 64 * 1024 * 1024
                or type(pin["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) is None
            ):
                raise _refuse()
        return selected
    except (OSError, ValueError, KeyError, TypeError, ProfilePathError):
        raise _refuse() from None


def _file_identity(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _pin(path: Path, pin: dict) -> tuple[int, tuple]:
    fd = -1
    try:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path.resolve(strict=True) != path
        ):
            raise _refuse()
        _trusted_ancestor(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        identity = _file_identity(os.fstat(fd))
        _verify_pin(path, fd, identity, pin)
        return fd, identity
    except (OSError, ValueError, KeyError, TypeError, ProfilePathError):
        if fd >= 0:
            os.close(fd)
        raise _refuse() from None


def _verify_pin(path: Path, fd: int, identity: tuple, pin: dict) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or info.st_mode & 0o022
        or info.st_nlink != 1
        or info.st_uid not in {0, os.geteuid()}
        or _file_identity(info) != identity
        or _file_identity(path.lstat()) != identity
        or info.st_size != pin["size_bytes"]
        or not 1 <= info.st_size <= 64 * 1024 * 1024
    ):
        raise _refuse()
    _reject_acl_grants(fd, path)
    digest = hashlib.sha256()
    cursor = 0
    while chunk := os.pread(fd, 65536, cursor):
        cursor += len(chunk)
        if cursor > info.st_size:
            raise _refuse()
        digest.update(chunk)
    if cursor != info.st_size or not hmac.compare_digest(digest.hexdigest(), pin["sha256"]):
        raise _refuse()
    if _file_identity(os.fstat(fd)) != identity or _file_identity(path.lstat()) != identity:
        raise _refuse()


def _invoke(
    toolchain, name, arguments, input_bytes, maximum_output, deadline_seconds, *, pass_fds=()
):
    _deadline(deadline_seconds)
    registry = _registry(toolchain)
    pinned = []
    output = b""
    failed = False
    try:
        for role, path in (("age", toolchain.age), ("age_keygen", toolchain.age_keygen)):
            fd, identity = _pin(path, registry[role])
            pinned.append((path, fd, identity, registry[role]))
        for path, fd, identity, pin in pinned:
            _verify_pin(path, fd, identity, pin)
            if (
                _run_bounded([str(path), "--version"], b"", 128, deadline_seconds).strip()
                != b"v1.3.2"
            ):
                raise _refuse()
            _verify_pin(path, fd, identity, pin)
        for path, fd, identity, pin in pinned:
            _verify_pin(path, fd, identity, pin)
        executable = toolchain.age if name == "age" else toolchain.age_keygen
        output = _run_bounded(
            [str(executable), *arguments],
            input_bytes,
            maximum_output,
            deadline_seconds,
            pass_fds=pass_fds,
        )
        for path, fd, identity, pin in pinned:
            _verify_pin(path, fd, identity, pin)
            _trusted_ancestor(path.parent)
    except (OSError, ValueError, KeyError, TypeError, ProfilePathError):
        failed = True
    finally:
        for _, fd, _, _ in pinned:
            try:
                os.close(fd)
            except OSError:
                failed = True
    if failed:
        raise _refuse() from None
    return output


def generate_identity_v1(toolchain: NativeAgeToolchainV1, *, deadline_seconds: int = 30) -> bytes:
    output = _invoke(toolchain, "age_keygen", [], b"", 4096, deadline_seconds)
    lines = [line + b"\n" for line in output.splitlines() if not line.startswith(b"#")]
    if len(lines) != 1:
        raise _refuse()
    identity = _identity(lines[0])
    derive_recipient_v1(identity, toolchain, deadline_seconds=deadline_seconds)
    return identity


def derive_recipient_v1(
    identity: bytes, toolchain: NativeAgeToolchainV1, *, deadline_seconds: int = 30
) -> str:
    output = _invoke(toolchain, "age_keygen", ["-y"], _identity(identity), 128, deadline_seconds)
    try:
        if not output.endswith(b"\n") or output.count(b"\n") != 1:
            raise _refuse()
        return validate_recipient_v1(output[:-1].decode("ascii"))
    except UnicodeError:
        raise _refuse() from None


def _check_ciphertext_v1(ciphertext: bytes) -> None:
    if type(ciphertext) is not bytes or not ciphertext.startswith(b"age-encryption.org/v1\n"):
        raise _refuse()
    marker = ciphertext.find(b"\n--- ", 0, 16384)
    if marker < 0:
        raise _refuse()
    lines = ciphertext[:marker].splitlines()[1:]
    stanzas = [line for line in lines if line.startswith(b"-> ")]
    if len(stanzas) != 2 or any(not line.startswith(b"-> X25519 ") for line in stanzas):
        raise _refuse()


def encrypt_v1(
    frame: bytes,
    recipients: tuple[str, str],
    toolchain: NativeAgeToolchainV1,
    *,
    maximum_output: int,
    deadline_seconds: int = 30,
) -> bytes:
    if type(frame) is not bytes or len(frame) > 81 * 1024 * 1024:
        raise _refuse()
    if type(recipients) is not tuple or len(recipients) != 2 or recipients[0] == recipients[1]:
        raise _refuse()
    for recipient in recipients:
        validate_recipient_v1(recipient)
    output = _invoke(
        toolchain,
        "age",
        ["-r", recipients[0], "-r", recipients[1]],
        frame,
        maximum_output,
        deadline_seconds,
    )
    _check_ciphertext_v1(output)
    return output


def decrypt_v1(
    ciphertext: bytes,
    identity: bytes,
    toolchain: NativeAgeToolchainV1,
    *,
    maximum_output: int,
    deadline_seconds: int = 30,
) -> bytes:
    _check_ciphertext_v1(ciphertext)
    if len(ciphertext) > 82 * 1024 * 1024:
        raise _refuse()
    identity = _identity(identity)
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        raise _refuse() from None
    failed = False
    output = b""
    try:
        if os.write(write_fd, identity) != len(identity):
            raise _refuse()
        closing_fd, write_fd = write_fd, -1
        os.close(closing_fd)
        output = _invoke(
            toolchain,
            "age",
            ["-d", "-i", f"/dev/fd/{read_fd}"],
            ciphertext,
            maximum_output,
            deadline_seconds,
            pass_fds=(read_fd,),
        )
    except (OSError, ValueError):
        failed = True
    finally:
        for fd in (read_fd, write_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    failed = True
    if failed:
        raise _refuse() from None
    return output
