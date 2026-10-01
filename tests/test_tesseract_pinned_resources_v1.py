"""Deterministic resource custody and compatibility tests; no real OCR claims."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

from finance_core.intake import receipt_ocr_evidence as ocr
from finance_core.intake import tesseract_resources as resources
from finance_core.openclaw_staging_bridge import errors, ocr_boundary


def descriptor(
    root: Path, *, value: bytes = b"synthetic-model"
) -> resources.PinnedTesseractResources:
    root.mkdir(mode=0o700)
    identities = []
    for language in ("eng", "chi_sim"):
        path = root / f"{language}.traineddata"
        path.write_bytes(value)
        path.chmod(0o400)
        identities.append(
            resources.TesseractLanguageResource(
                language, len(value), hashlib.sha256(value).hexdigest()
            )
        )
    return resources.PinnedTesseractResources(root.resolve(), tuple(identities))


def binary(root: Path) -> Path:
    path = root / "binary"
    path.write_bytes(b"synthetic executable identity")
    path.chmod(0o500)
    return path.resolve()


def config(root: Path, model: resources.PinnedTesseractResources, exe: Path) -> Path:
    runtime = root / "runtime"
    runtime.mkdir()
    path = runtime / "ocr_engine.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "v2",
                "engine": "tesseract_tsv",
                "helper_path": str(exe),
                "expected_version": "5.3.4",
                "binary_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
                "tessdata_directory": str(model.directory),
                "language_resources": model.identities(),
            }
        )
    )
    path.chmod(0o600)
    return path


def test_snapshot_is_private_and_source_replacement_cannot_change_current_bytes(
    tmp_path: Path,
) -> None:
    model = descriptor(tmp_path / "models")
    with model.snapshot(deadline=time.monotonic() + 5) as snapshot:
        directory = snapshot.directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for path in directory.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o400
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"replaced-model!")
        replacement.chmod(0o400)
        os.replace(replacement, model.directory / "eng.traineddata")
        snapshot.verify(time.monotonic() + 5)
        assert (directory / "eng.traineddata").read_bytes() == b"synthetic-model"
    assert not directory.exists()
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        model.validate()


@pytest.mark.parametrize("tamper", ["write", "replace", "link"])
def test_snapshot_tamper_and_exception_always_cleanup(tmp_path: Path, tamper: str) -> None:
    model = descriptor(tmp_path / "models")
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        with model.snapshot(deadline=time.monotonic() + 5) as snapshot:
            directory = snapshot.directory
            path = directory / "eng.traineddata"
            path.chmod(0o600)
            if tamper == "write":
                path.write_bytes(b"synthetic-MODEL")
            else:
                path.unlink()
                if tamper == "replace":
                    path.write_bytes(b"synthetic-model")
                else:
                    path.symlink_to(model.directory / "eng.traineddata")
            snapshot.verify(time.monotonic() + 5)
    assert not directory.exists()


@pytest.mark.parametrize(
    "failure", ["missing", "size", "hash", "writable", "symlink", "directory", "owner"]
)
def test_source_failures_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    model = descriptor(tmp_path / "models")
    path = model.directory / "chi_sim.traineddata"
    if failure == "missing":
        path.unlink()
    elif failure == "size":
        path.chmod(0o600)
        path.write_bytes(b"short")
    elif failure == "hash":
        path.chmod(0o600)
        path.write_bytes(b"SYNTHETIC-model")
    elif failure == "writable":
        path.chmod(0o622)
    elif failure == "symlink":
        path.unlink()
        path.symlink_to(model.directory / "eng.traineddata")
    elif failure == "directory":
        path.unlink()
        path.mkdir()
    else:
        monkeypatch.setattr(resources.os, "getuid", lambda: -1)
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        model.validate()


def test_size_order_type_and_deadline_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = descriptor(tmp_path / "models")
    for size in (True, 0, resources.MAX_MODEL_BYTES + 1):
        with pytest.raises(ocr.InvalidOcrConfigurationError):
            resources.TesseractLanguageResource("eng", size, "a" * 64)
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        resources.PinnedTesseractResources(model.directory, tuple(reversed(model.languages)))
    with pytest.raises(ocr.OcrDeadlineExceededError):
        with model.snapshot(deadline=time.monotonic() - 1):
            pass
    real_read = resources.os.read

    def slow_read(fd: int, length: int) -> bytes:
        value = real_read(fd, length)
        time.sleep(0.005)
        return value

    monkeypatch.setattr(resources.os, "read", slow_read)
    with pytest.raises(ocr.OcrDeadlineExceededError):
        with model.snapshot(deadline=time.monotonic() + 0.001):
            pass


def test_legacy_golden_hash_and_pinned_relocation_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "_require_supported_process_platform", lambda: None)
    exe = binary(tmp_path)
    legacy = ocr.TesseractTsvOcrEngine(exe, expected_version="5.3.4")
    assert (
        legacy.identity.configuration_hash
        == "d90b6e30be1ff4fc5999219271a48f3d20838f4033dafb212d7aeb952c88e188"
    )
    first = descriptor(tmp_path / "first")
    second = descriptor(tmp_path / "second")
    changed = descriptor(tmp_path / "changed", value=b"different-model")

    def make(model):
        return ocr.TesseractTsvOcrEngine(
            exe, expected_version="5.3.4", language="eng+chi_sim", pinned_resources=model
        )

    assert make(first).identity == make(second).identity
    assert make(first).identity.configuration_hash != make(changed).identity.configuration_hash
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        ocr.TesseractTsvOcrEngine(exe, expected_version="5.3.4", pinned_resources=first)


def test_pinned_fixed_arguments_and_snapshot_check_after_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "_require_supported_process_platform", lambda: None)
    model = descriptor(tmp_path / "models")
    engine = ocr.TesseractTsvOcrEngine(
        binary(tmp_path), expected_version="5.3.4", language="eng+chi_sim", pinned_resources=model
    )
    launches = []

    def runner(argv, **kwargs):
        launches.append((argv, kwargs))
        assert kwargs["pinned_environment"] is True
        if argv[-1] == "--version":
            return ocr._ProcessOutput(0, b"tesseract 5.3.4\n")
        assert argv[2:5] == ["stdout", "-l", "eng+chi_sim"]
        assert argv[7:] == [
            "--oem",
            "1",
            "--psm",
            "3",
            "--dpi",
            "300",
            "-c",
            "tessedit_create_tsv=1",
        ]
        snapshot = Path(argv[6])
        assert snapshot != model.directory
        path = snapshot / "eng.traineddata"
        path.chmod(0o600)
        path.write_bytes(b"modified-model!")
        return ocr._ProcessOutput(0, b"")

    monkeypatch.setattr(ocr, "_run_bounded_process", runner)
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        engine.extract(
            ocr.ReceiptOcrSource(0, "/synthetic.png", 1, "a" * 64, "image/png"),
            limits=ocr.ReceiptOcrLimits(),
            deadline=time.monotonic() + 5,
        )
    assert len(launches) == 2
    assert not Path(launches[1][0][6]).exists()


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "engine",
        "hash",
        "size_bool",
        "reorder",
        "duplicate",
        "unsafe_config",
        "binary_hash",
        "platform",
    ],
)
def test_v2_resolver_is_strict_and_v1_constant_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(ocr_boundary.sys, "platform", "linux")
    monkeypatch.setattr(ocr, "_require_supported_process_platform", lambda: None)
    path = config(tmp_path, descriptor(tmp_path / "models"), binary(tmp_path))
    payload = json.loads(path.read_text())
    if change == "extra":
        payload["environment"] = {"PATH": "anything"}
    elif change == "engine":
        payload["engine"] = "vision"
    elif change == "hash":
        payload["binary_sha256"] = "A" * 64
    elif change == "size_bool":
        payload["language_resources"][0]["size_bytes"] = True
    elif change == "reorder":
        payload["language_resources"].reverse()
    elif change == "duplicate":
        path.write_text('{"schema_version":"v1", "schema_version":"v2"}')
    elif change == "unsafe_config":
        path.chmod(0o622)
    elif change == "binary_hash":
        payload["binary_sha256"] = "a" * 64
    else:
        monkeypatch.setattr(ocr_boundary.sys, "platform", "darwin")
    if change != "duplicate":
        path.write_text(json.dumps(payload))
    assert ocr_boundary.OCR_ENGINE_CONFIG_SCHEMA_VERSION == "v1"
    with pytest.raises(errors.BridgeError) as exc:
        ocr_boundary.resolve_workspace_ocr_engine(tmp_path)
    assert exc.value.code == errors.OCR_ENGINE_UNAVAILABLE


def test_runner_environment_ignores_parent_and_legacy_stays_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESSDATA_PREFIX", "/attacker/models")
    monkeypatch.setenv("OMP_THREAD_LIMIT", "999")
    environments = []

    class Process:
        pid = 999999
        stdout = None
        stderr = None

        def wait(self, timeout):
            return 0

    def popen(_arguments, **kwargs):
        environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(ocr.subprocess, "Popen", popen)
    monkeypatch.setattr(ocr, "_read_process_output", lambda *args, **kwargs: (b"", b""))
    monkeypatch.setattr(ocr, "_terminate_remaining_process_group", lambda *args: None)
    for pinned in (False, True):
        ocr._run_bounded_process(
            ["/synthetic"],
            pass_fds=(),
            limits=ocr.ReceiptOcrLimits(),
            deadline=time.monotonic() + 5,
            working_directory="/private/tmp",
            pinned_environment=pinned,
        )
    assert environments == [
        {"LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
        {"LANG": "C", "LC_ALL": "C", "TZ": "UTC", "OMP_THREAD_LIMIT": "1"},
    ]


def test_source_mutation_during_copy_is_refused_and_private_directory_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = descriptor(tmp_path / "models")
    real_stream = resources._stream_checked
    real_temp = resources.tempfile.TemporaryDirectory
    directories = []

    def temp(*args, **kwargs):
        result = real_temp(*args, **kwargs)
        directories.append(Path(result.name))
        return result

    def mutate(fd, identity, deadline, *, target_fd=None):
        real_stream(fd, identity, deadline, target_fd=target_fd)
        if target_fd is not None:
            path = model.directory / f"{identity.language}.traineddata"
            path.chmod(0o600)
            path.write_bytes(b"changed-model!!")

    monkeypatch.setattr(resources, "_stream_checked", mutate)
    monkeypatch.setattr(resources.tempfile, "TemporaryDirectory", temp)
    with pytest.raises(ocr.InvalidOcrConfigurationError):
        model.validate()
    assert directories and all(not directory.exists() for directory in directories)


def test_v2_config_partial_json_oversize_and_replacement_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr_boundary.sys, "platform", "linux")
    monkeypatch.setattr(ocr, "_require_supported_process_platform", lambda: None)
    path = config(tmp_path, descriptor(tmp_path / "models"), binary(tmp_path))
    original = path.read_bytes()
    for value in (b'{"schema_version":"v2"', b" " * 4097):
        path.write_bytes(value)
        with pytest.raises(errors.BridgeError):
            ocr_boundary.resolve_workspace_ocr_engine(tmp_path)
    path.write_bytes(original)
    real_read = ocr_boundary.os.read

    def replace(fd, size):
        raw = real_read(fd, size)
        replacement = tmp_path / "replacement-config"
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        return raw

    monkeypatch.setattr(ocr_boundary.os, "read", replace)
    with pytest.raises(errors.BridgeError):
        ocr_boundary.resolve_workspace_ocr_engine(tmp_path)


def test_asset_preparation_hashes_bounded_download_and_refuses_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import io
    import subprocess

    spec = importlib.util.spec_from_file_location(
        "prepare_ocr", Path(__file__).parents[1] / "scripts" / "prepare_linux_ocr.py"
    )
    assert spec and spec.loader
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    monkeypatch.setattr(preparation.sys, "platform", "linux")
    monkeypatch.setattr(preparation.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        preparation.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )
    value = b"bounded-model"
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "commit": "a" * 40,
                "expected_tesseract_version": "5.3.4",
                "language_resources": [
                    {
                        "language": language,
                        "size_bytes": len(value),
                        "sha256": hashlib.sha256(value).hexdigest(),
                    }
                    for language in ("eng", "chi_sim")
                ],
            }
        )
    )
    monkeypatch.setattr(preparation, "LOCK", lock)
    monkeypatch.setattr(
        preparation.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(value)
    )
    executable = binary(tmp_path)

    def command(argv, **kwargs):
        output = (
            b"tesseract 5.3.4\n" if argv[-1] == "--version" else b"tesseract-ocr\t5.3.4-1\tamd64\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, b"")

    monkeypatch.setattr(preparation.subprocess, "run", command)
    destination = tmp_path / "prepared"
    preparation.prepare(destination, executable)
    assert stat.S_IMODE((destination / "tesseract").stat().st_mode) == 0o500
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    descriptor(destination / "extra-models")  # unrelated private directory remains untouched
    receipt = json.loads((destination / "preparation-receipt.json").read_text())
    assert (
        receipt["observed_version"] == "5.3.4"
        and receipt["package"] == "tesseract-ocr\t5.3.4-1\tamd64"
    )
    monkeypatch.setattr(
        preparation.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(value + b"extra")
    )
    with pytest.raises(ValueError):
        preparation.prepare(tmp_path / "oversize", executable)
    assert not (tmp_path / "oversize").exists()
    monkeypatch.setattr(
        preparation.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(value)
    )
    monkeypatch.setattr(
        preparation.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"tesseract 9.9.9\n", b""),
    )
    with pytest.raises(ValueError):
        preparation.prepare(tmp_path / "wrong-version", executable)
    assert not (tmp_path / "wrong-version").exists()
    assert not list(tmp_path.glob("linux-ocr-preparation-*"))
