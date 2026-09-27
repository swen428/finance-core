"""Offline synthetic archive tests; optional native age uses disposable in-memory keys."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import finance_core.synthetic_archive_crypto as archive_crypto
from finance_core.synthetic_archive_crypto import (
    MAX_ARCHIVE_BYTES,
    MAX_CIPHERTEXT_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SIDECAR_BYTES,
    AgeToolPin,
    ArchiveContext,
    SyntheticArchiveError,
    open_synthetic_archive,
    seal_synthetic_archive,
)

_FAKE = r"""#!PYTHON
import hashlib
import os
import struct
import sys
import time

if sys.argv[1:] == ["--version"]:
    print("v1.3.2")
    raise SystemExit(0)
mode = os.environ.get("FAKE_AGE_MODE", "")
if mode == "sleep":
    marker = os.environ.get("FAKE_AGE_PID_MARKER")
    if marker:
        with open(marker, "w", encoding="ascii") as output:
            output.write(str(os.getpid()))
    time.sleep(10)
    raise SystemExit(0)
if mode == "flood":
    sys.stdout.buffer.write(b"X" * (11 * 1024 * 1024))
    raise SystemExit(0)
source = sys.stdin.buffer.read()
if sys.argv[1] == "-e":
    recipient = sys.argv[3]
    suffix = recipient[4:].upper().encode("ascii")
    if mode == "malformed-frame":
        source = b"BADFRAME" + source[8:]
    elif mode == "bad-frame-length":
        source = source[:8] + struct.pack(">Q", 2**63) + source[16:]
    elif mode == "bad-frame-hash":
        source = source[:24] + bytes([source[24] ^ 1]) + source[25:]
    elif mode == "trailing-frame":
        source += b"extra"
    digest = hashlib.sha256(b"FAKE" + suffix + source).digest()
    sys.stdout.buffer.write(b"FAKE1" + suffix + struct.pack(">Q", len(source)) + source + digest)
elif sys.argv[1] == "-d":
    marker = os.environ.get("FAKE_AGE_DECRYPT_MARKER")
    if marker:
        with open(marker, "w", encoding="ascii") as output:
            output.write("invoked")
    with open(sys.argv[3], "rb") as key_file:
        key = key_file.read().strip()
    suffix = key[16:]
    if not source.startswith(b"FAKE1" + suffix) or len(source) < 5 + 58 + 8 + 32:
        raise SystemExit(2)
    length = struct.unpack(">Q", source[63:71])[0]
    if len(source) != 71 + length + 32:
        raise SystemExit(2)
    payload = source[71:-32]
    if mode == "late-fail":
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        raise SystemExit(3)
    digest = hashlib.sha256(b"FAKE" + suffix + payload).digest()
    if digest != source[-32:]:
        raise SystemExit(2)
    sys.stdout.buffer.write(payload)
else:
    raise SystemExit(2)
"""


def _runtime_platform() -> str:
    machine = platform.machine().lower()
    return f"{sys.platform}-{'arm64' if machine == 'aarch64' else machine}"


@pytest.fixture
def fake_tool(tmp_path: Path) -> AgeToolPin:
    script = tmp_path / "age-fake"
    script.write_text(_FAKE.replace("#!PYTHON", f"#!{sys.executable}"), encoding="utf-8")
    script.chmod(0o700)
    return AgeToolPin(
        script,
        hashlib.sha256(script.read_bytes()).hexdigest(),
        _runtime_platform(),
        timeout_seconds=5.0,
    )


@pytest.fixture
def context() -> ArchiveContext:
    return ArchiveContext("synthetic-archive-1", "synthetic-cut-1", 1, "recipient-1", "auth-1")


_RECIPIENT_A = "age1" + "q" * 58
_IDENTITY_A = ("AGE-SECRET-KEY-1" + "Q" * 58 + "\n").encode("ascii")
_RECIPIENT_B = "age1" + "p" * 58
_IDENTITY_B = ("AGE-SECRET-KEY-1" + "P" * 58 + "\n").encode("ascii")
_AUTH_A = b"A" * 32
_AUTH_B = b"B" * 32
_NATIVE_AGE_SHA256 = "4012dfc2725883beafb710894af4f599b7a94f8c8e0f51f02cc96ab8df33915e"


def _seal(
    tool: AgeToolPin,
    context: ArchiveContext,
    archive: bytes = b"synthetic archive bytes",
    manifest: bytes = b"synthetic manifest bytes",
    recipient: str = _RECIPIENT_A,
    auth_key: bytes = _AUTH_A,
) -> tuple[bytes, bytes]:
    return seal_synthetic_archive(
        archive=archive,
        manifest=manifest,
        context=context,
        recipient=recipient,
        auth_key=auth_key,
        tool=tool,
    )


def _open(
    tool: AgeToolPin,
    context: ArchiveContext,
    ciphertext: bytes,
    sidecar: bytes,
    identity: bytes = _IDENTITY_A,
    auth_key: bytes = _AUTH_A,
) -> tuple[bytes, bytes]:
    return open_synthetic_archive(
        ciphertext=ciphertext,
        sidecar=sidecar,
        expected_context=context,
        identity=identity,
        auth_key=auth_key,
        tool=tool,
    )


def _resign(sidecar: bytes, ciphertext: bytes, key: bytes = _AUTH_A) -> bytes:
    fields = json.loads(sidecar)
    fields["ciphertext_length"] = len(ciphertext)
    fields["ciphertext_sha256"] = hashlib.sha256(ciphertext).hexdigest()
    fields.pop("mac_sha256")
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("ascii")
    fields["mac_sha256"] = hmac.new(
        key, b"finance-d4-synthetic-age-sidecar-v1\x00" + canonical, hashlib.sha256
    ).hexdigest()
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("ascii")


@pytest.mark.parametrize("size", [0, 65535, 65536, 262267])
def test_exact_byte_roundtrip(fake_tool: AgeToolPin, context: ArchiveContext, size: int) -> None:
    archive = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    manifest = bytes(range(255, -1, -1)) * 513
    ciphertext, sidecar = _seal(fake_tool, context, archive, manifest)
    opened_archive, opened_manifest = _open(fake_tool, context, ciphertext, sidecar)
    assert hmac.compare_digest(opened_archive, archive)
    assert hmac.compare_digest(opened_manifest, manifest)
    if archive:
        assert sidecar.count(archive) == 0
    assert sidecar.count(_IDENTITY_A) == 0


def test_context_sidecar_and_auth_key_refuse_before_decryption(
    fake_tool: AgeToolPin,
    context: ArchiveContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ciphertext, sidecar = _seal(fake_tool, context)
    marker = tmp_path / "decrypt-called"
    monkeypatch.setenv("FAKE_AGE_DECRYPT_MARKER", str(marker))
    changed_context = ArchiveContext("other", context.context_id, 1, "recipient-1", "auth-1")
    candidates = [
        (sidecar, changed_context, _AUTH_A),
        (sidecar, context, _AUTH_B),
        (b"", context, _AUTH_A),
        (sidecar[:-1], context, _AUTH_A),
        (sidecar.replace(b"recipient-1", b"recipient-2"), context, _AUTH_A),
        (sidecar[:-1] + b',"unknown":1}', context, _AUTH_A),
        (sidecar[:-1] + b',"format":"duplicate"}', context, _AUTH_A),
    ]
    for candidate, expected, auth_key in candidates:
        with pytest.raises(SyntheticArchiveError):
            _open(fake_tool, expected, ciphertext, candidate, auth_key=auth_key)
        assert not marker.exists()


def test_ciphertext_tamper_and_truncation_even_with_valid_sidecar(
    fake_tool: AgeToolPin, context: ArchiveContext
) -> None:
    ciphertext, sidecar = _seal(fake_tool, context)
    for bad in (ciphertext[:-1], ciphertext[:10], ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])):
        with pytest.raises(SyntheticArchiveError):
            _open(fake_tool, context, bad, _resign(sidecar, bad))
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, ciphertext[:-1], sidecar)


@pytest.mark.parametrize(
    "mode", ["malformed-frame", "bad-frame-length", "bad-frame-hash", "trailing-frame"]
)
def test_authenticated_malformed_frame_refused(
    fake_tool: AgeToolPin,
    context: ArchiveContext,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("FAKE_AGE_MODE", mode)
    ciphertext, sidecar = _seal(fake_tool, context)
    monkeypatch.delenv("FAKE_AGE_MODE")
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, ciphertext, sidecar)


def test_late_child_failure_returns_no_partial_plaintext(
    fake_tool: AgeToolPin, context: ArchiveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ciphertext, sidecar = _seal(fake_tool, context, b"PRIVATE_SYNTHETIC_MARKER")
    monkeypatch.setenv("FAKE_AGE_MODE", "late-fail")
    with pytest.raises(SyntheticArchiveError) as failure:
        _open(fake_tool, context, ciphertext, sidecar)
    assert "PRIVATE_SYNTHETIC_MARKER" not in str(failure.value)
    assert _IDENTITY_A.decode().strip() not in str(failure.value)


def test_wrong_age_key_and_rotation(fake_tool: AgeToolPin, context: ArchiveContext) -> None:
    old_ciphertext, old_sidecar = _seal(fake_tool, context)
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, old_ciphertext, old_sidecar, identity=_IDENTITY_B)
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, old_ciphertext, old_sidecar, auth_key=_AUTH_B)
    new_context = ArchiveContext(
        "synthetic-archive-2", "synthetic-cut-2", 2, "recipient-2", "auth-2"
    )
    new_ciphertext, new_sidecar = _seal(
        fake_tool, new_context, recipient=_RECIPIENT_B, auth_key=_AUTH_B
    )
    assert _open(fake_tool, context, old_ciphertext, old_sidecar)
    assert _open(fake_tool, new_context, new_ciphertext, new_sidecar, _IDENTITY_B, _AUTH_B)
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, new_context, new_ciphertext, new_sidecar, _IDENTITY_A, _AUTH_B)


def test_wrong_or_changed_tool_fails_before_key_use(
    fake_tool: AgeToolPin,
    context: ArchiveContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ciphertext, sidecar = _seal(fake_tool, context)
    wrong_digest = AgeToolPin(fake_tool.executable, "0" * 64, fake_tool.platform)
    changed_platform = AgeToolPin(fake_tool.executable, fake_tool.sha256, "other-platform")
    changed_release = AgeToolPin(fake_tool.executable, fake_tool.sha256, fake_tool.platform, "v0")
    missing = AgeToolPin(tmp_path / "missing", fake_tool.sha256, fake_tool.platform)
    symlink = tmp_path / "linked-age"
    symlink.symlink_to(fake_tool.executable)
    linked = AgeToolPin(symlink, fake_tool.sha256, fake_tool.platform)
    for pin in (wrong_digest, changed_platform, changed_release, missing, linked):
        with pytest.raises(SyntheticArchiveError):
            _open(pin, context, ciphertext, sidecar)
    with monkeypatch.context() as patch:
        patch.setattr(
            archive_crypto.hmac,
            "new",
            lambda *_args, **_kwargs: pytest.fail("HMAC key used before tool validation"),
        )
        with pytest.raises(SyntheticArchiveError):
            _open(wrong_digest, context, ciphertext, sidecar)
    fake_tool.executable.write_text(
        fake_tool.executable.read_text(encoding="utf-8").replace(
            'print("v1.3.2")', 'print("v1.3.3")'
        ),
        encoding="utf-8",
    )
    wrong_version = AgeToolPin(
        fake_tool.executable,
        hashlib.sha256(fake_tool.executable.read_bytes()).hexdigest(),
        fake_tool.platform,
    )
    with pytest.raises(SyntheticArchiveError):
        _open(wrong_version, context, ciphertext, sidecar)
    fake_tool.executable.write_text(
        fake_tool.executable.read_text(encoding="utf-8").replace(
            'print("v1.3.3")', 'print("v1.3.2")'
        ),
        encoding="utf-8",
    )
    fake_tool.executable.chmod(0o777)
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, ciphertext, sidecar)
    fake_tool.executable.chmod(0o700)
    fake_tool.executable.write_text("changed", encoding="ascii")
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, ciphertext, sidecar)


def test_fifo_tool_path_refuses_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "age-fifo"
    os.mkfifo(fifo)
    script = """
import sys
from pathlib import Path
from finance_core.synthetic_archive_crypto import AgeToolPin, SyntheticArchiveError, _validate_tool
try:
    _validate_tool(AgeToolPin(Path(sys.argv[1]), "0" * 64, sys.argv[2], timeout_seconds=0.1))
except SyntheticArchiveError:
    print("refused")
else:
    raise SystemExit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo), _runtime_platform()],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b"refused\n"


def test_size_and_identifier_limits(fake_tool: AgeToolPin, context: ArchiveContext) -> None:
    with pytest.raises(SyntheticArchiveError):
        _seal(fake_tool, context, archive=b"x" * (MAX_ARCHIVE_BYTES + 1))
    with pytest.raises(SyntheticArchiveError):
        _seal(fake_tool, context, manifest=b"x" * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(SyntheticArchiveError):
        _seal(fake_tool, context, auth_key=b"short")
    bad_context = ArchiveContext("bad/path", context.context_id, 1, "recipient-1", "auth-1")
    with pytest.raises(SyntheticArchiveError):
        _seal(fake_tool, bad_context)
    ciphertext, sidecar = _seal(fake_tool, context)
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, b"x" * (MAX_CIPHERTEXT_BYTES + 1), sidecar)
    with pytest.raises(SyntheticArchiveError):
        _open(fake_tool, context, ciphertext, b"x" * (MAX_SIDECAR_BYTES + 1))


def test_timeout_reaps_child(
    fake_tool: AgeToolPin,
    context: ArchiveContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child-pid"
    monkeypatch.setenv("FAKE_AGE_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGE_PID_MARKER", str(marker))
    with pytest.raises(SyntheticArchiveError):
        _seal(replace(fake_tool, timeout_seconds=0.5), context)
    pid = int(marker.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_child_output_is_bounded(
    fake_tool: AgeToolPin, context: ArchiveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGE_MODE", "flood")
    with pytest.raises(SyntheticArchiveError):
        _seal(fake_tool, context)


@pytest.mark.skipif(not os.environ.get("FINANCE_D4_TEST_AGE_BINARY"), reason="optional native age")
def test_native_age_roundtrip_and_wrong_identity(context: ArchiveContext) -> None:
    path = Path(os.environ["FINANCE_D4_TEST_AGE_BINARY"])
    pin = AgeToolPin(path, _NATIVE_AGE_SHA256, _runtime_platform())
    keygen = path.with_name("age-keygen")
    generated = subprocess.run([str(keygen)], capture_output=True, check=True, timeout=5)
    lines = generated.stdout.splitlines()
    identity = next(line + b"\n" for line in lines if line.startswith(b"AGE-SECRET-KEY-1"))
    recipient = next(
        line.split(b": ", 1)[1].decode("ascii")
        for line in lines
        if line.startswith(b"# public key: ")
    )
    archive = bytes(range(256)) * 1025
    ciphertext, sidecar = _seal(pin, context, archive=archive, recipient=recipient)
    opened_archive, _ = _open(pin, context, ciphertext, sidecar, identity=identity)
    assert hmac.compare_digest(opened_archive, archive)
    corrupted = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    with pytest.raises(SyntheticArchiveError):
        _open(pin, context, corrupted, _resign(sidecar, corrupted), identity=identity)
    wrong = subprocess.run([str(keygen)], capture_output=True, check=True, timeout=5)
    wrong_identity = next(
        line + b"\n" for line in wrong.stdout.splitlines() if line.startswith(b"AGE-SECRET-KEY-1")
    )
    with pytest.raises(SyntheticArchiveError):
        _open(pin, context, ciphertext, sidecar, identity=wrong_identity)
    next_recipient = next(
        line.split(b": ", 1)[1].decode("ascii")
        for line in wrong.stdout.splitlines()
        if line.startswith(b"# public key: ")
    )
    next_context = ArchiveContext(
        "synthetic-archive-2", "synthetic-cut-2", 2, "recipient-2", "auth-2"
    )
    next_ciphertext, next_sidecar = _seal(
        pin, next_context, archive=archive, recipient=next_recipient, auth_key=_AUTH_B
    )
    assert _open(pin, next_context, next_ciphertext, next_sidecar, wrong_identity, _AUTH_B)
    assert _open(pin, context, ciphertext, sidecar, identity=identity)
