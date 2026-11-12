"""Mandatory pinned native age proof, plus supplementary child failure bounds."""

from __future__ import annotations

import os
import platform
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from finance_core._native_age_v1 import (
    NativeAgeError,
    NativeAgeToolchainV1,
    _run_bounded,
    decrypt_v1,
    derive_recipient_v1,
    encrypt_v1,
    generate_identity_v1,
)


def real_toolchain() -> NativeAgeToolchainV1:
    host = f"{sys.platform}-{platform.machine().lower()}"
    root = os.environ.get("FINANCE_PREPARED_AGE_TOOL_DIR")
    if root is None and host == "darwin-arm64":
        root = "/private/tmp/finance-fl2b2-age-qualified-dd6vyh09/darwin-arm64"
    if root is None:
        pytest.fail("Mandatory qualified native age tool directory is missing")
    directory = Path(root)
    return NativeAgeToolchainV1(directory / "age", directory / "age-keygen", host)


@pytest.fixture(scope="module")
def native_keys():
    toolchain = real_toolchain()
    first = generate_identity_v1(toolchain)
    second = generate_identity_v1(toolchain)
    return NativeKeys(toolchain, first, second)


@dataclass
class NativeKeys:
    toolchain: NativeAgeToolchainV1
    first: bytes = field(repr=False)
    second: bytes = field(repr=False)


def test_native_two_recipient_and_real_failures(native_keys):
    toolchain, first, second = native_keys.toolchain, native_keys.first, native_keys.second
    recipients = tuple(derive_recipient_v1(k, toolchain) for k in (first, second))
    plaintext = b"synthetic prepared projection\n" * 1000
    ciphertext = encrypt_v1(plaintext, recipients, toolchain, maximum_output=1024 * 1024)
    for identity in (first, second):
        assert (
            decrypt_v1(ciphertext, identity, toolchain, maximum_output=len(plaintext)) == plaintext
        )
    wrong = generate_identity_v1(toolchain)
    with pytest.raises(NativeAgeError):
        decrypt_v1(ciphertext, wrong, toolchain, maximum_output=len(plaintext))
    corrupt = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    with pytest.raises(NativeAgeError):
        decrypt_v1(corrupt, first, toolchain, maximum_output=len(plaintext))


def test_native_unknown_platform_and_changed_tools_refused(native_keys, tmp_path):
    toolchain = native_keys.toolchain
    with pytest.raises(NativeAgeError):
        generate_identity_v1(replace(toolchain, platform="linux-arm64"))
    wrong = tmp_path / "age"
    wrong.write_bytes(b"unqualified executable")
    wrong.chmod(0o500)
    with pytest.raises(NativeAgeError):
        generate_identity_v1(replace(toolchain, age=wrong))
    symlink = tmp_path / "age-link"
    symlink.symlink_to(toolchain.age)
    with pytest.raises(NativeAgeError):
        generate_identity_v1(replace(toolchain, age=symlink))


@pytest.mark.parametrize(
    "program",
    [
        "import os; os.write(1,b'sensitive-prefix'); raise SystemExit(3)",
        "import os; os.write(1,b'x'*10000)",
        "import os; os.write(2,b'sensitive-stderr'*10000)",
    ],
)
def test_supplementary_child_late_failure_and_flood(program):
    with pytest.raises(NativeAgeError) as caught:
        _run_bounded([sys.executable, "-c", program], b"", 32, 2)
    assert "sensitive" not in str(caught.value)


def test_supplementary_timeout_reaps_child_and_closes_pipes(tmp_path, monkeypatch):
    import finance_core._native_age_v1 as native

    actual_popen = native.subprocess.Popen
    children = []

    def captured_popen(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(native.subprocess, "Popen", captured_popen)
    marker = tmp_path / "pid"
    program = (
        "import os,pathlib,time; pathlib.Path("
        + repr(str(marker))
        + ").write_text(str(os.getpid())); time.sleep(30)"
    )
    before = time.monotonic()
    with pytest.raises(NativeAgeError):
        _run_bounded([sys.executable, "-c", program], b"", 32, 1)
    assert time.monotonic() - before < 5
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert len(children) == 1
    assert all(
        stream.closed for stream in (children[0].stdin, children[0].stdout, children[0].stderr)
    )
    if sys.platform == "linux":
        assert not Path(f"/proc/{pid}").exists()


def test_supplementary_identity_fd_close_after_error_is_not_retried(native_keys, monkeypatch):
    import finance_core._native_age_v1 as native

    toolchain = native_keys.toolchain
    recipients = tuple(
        derive_recipient_v1(k, toolchain) for k in (native_keys.first, native_keys.second)
    )
    ciphertext = encrypt_v1(b"synthetic", recipients, toolchain, maximum_output=4096)
    read_fd, write_fd = os.pipe()
    actual_close = os.close
    calls = []

    def close_fault(fd):
        calls.append(fd)
        actual_close(fd)
        if fd == write_fd:
            raise OSError("injected close-after error")

    monkeypatch.setattr(native.os, "pipe", lambda: (read_fd, write_fd))
    monkeypatch.setattr(native.os, "close", close_fault)
    monkeypatch.setattr(
        native,
        "_invoke",
        lambda *args, **kwargs: pytest.fail("must not invoke after close uncertainty"),
    )
    with pytest.raises(NativeAgeError):
        decrypt_v1(ciphertext, native_keys.first, toolchain, maximum_output=4096)
    assert calls.count(write_fd) == 1
    assert calls.count(read_fd) == 1


def test_actual_tool_mutation_after_version_is_refused(native_keys, tmp_path, monkeypatch):
    import shutil

    import finance_core._native_age_v1 as native

    directory = tmp_path / "fixed-tools"
    directory.mkdir(mode=0o700)
    for source in (native_keys.toolchain.age, native_keys.toolchain.age_keygen):
        shutil.copyfile(source, directory / source.name)
        (directory / source.name).chmod(0o500)
    tools = replace(
        native_keys.toolchain, age=directory / "age", age_keygen=directory / "age-keygen"
    )
    actual_run = native._run_bounded
    executed = []

    def mutate_after_version(command, *args, **kwargs):
        result = actual_run(command, *args, **kwargs)
        executed.append(command)
        if command == [str(tools.age), "--version"]:
            tools.age.chmod(0o700)
            with tools.age.open("r+b") as binary:
                binary.seek(-1, 2)
                binary.write(b"x")
            tools.age.chmod(0o500)
        return result

    monkeypatch.setattr(native, "_run_bounded", mutate_after_version)
    with pytest.raises(NativeAgeError):
        generate_identity_v1(tools)
    assert executed == [[str(tools.age), "--version"]]
