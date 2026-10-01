"""Prepare locked test models and a service-owned distro binary, outside Git.

This explicit CI preparation downloads assets; the production resolver never
connects to a network. A failed preparation removes its own partial directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

LOCK = Path(__file__).with_name("linux_ocr_assets_v1.json")
MAX_BINARY_BYTES = 32 * 1024 * 1024
MAX_FAILURE_RECEIPT_BYTES = 16 * 1024


def failure_receipt_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}-preparation-failure.json")


def _write_failure_receipt(destination: Path, receipt: dict) -> None:
    # Exclusive creation preserves earlier failed evidence and never follows a link.
    data = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_FAILURE_RECEIPT_BYTES:
        raise ValueError("OCR failure receipt exceeds its bounded contract.")
    fd = os.open(
        failure_receipt_path(destination),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def prepare(destination: Path, executable: Path) -> None:
    if not destination.is_absolute() or destination.exists():
        raise ValueError("Preparation requires a new absolute private directory.")
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("OCR preparation requires Ubuntu 24.04 x86_64.")
    release = platform.freedesktop_os_release()
    if (release.get("ID"), release.get("VERSION_ID")) != ("ubuntu", "24.04"):
        raise ValueError("OCR preparation requires Ubuntu 24.04 x86_64.")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    failure: dict[str, Any] = {
        "schema_version": "linux-ocr-preparation-failure-v1",
        "status": "failed",
        "stage": "resource_download",
        "failure_category": "preparation_failure",
        "resource_lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
        "expected_tesseract_version": lock["expected_tesseract_version"],
        "observed_version": None,
        "language_resources": [
            {
                "language": model["language"],
                "expected_size_bytes": model["size_bytes"],
                "expected_sha256": model["sha256"],
                "observed_size_bytes": None,
                "observed_sha256": None,
            }
            for model in lock["language_resources"]
        ],
        "binary_size_bytes": None,
        "binary_sha256": None,
    }
    try:
        _prepare_assets(destination, executable, release, lock, failure)
    except Exception as exc:
        if failure["failure_category"] == "preparation_failure":
            failure["failure_category"] = (
                "deadline"
                if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                else "process_failure"
                if isinstance(exc, subprocess.SubprocessError)
                else "io_failure"
                if isinstance(exc, OSError)
                else "preparation_failure"
            )
        _write_failure_receipt(destination, failure)
        raise


def _prepare_assets(
    destination: Path, executable: Path, release: dict, lock: dict, failure: dict
) -> None:
    deadline = time.monotonic() + 120
    with tempfile.TemporaryDirectory(
        prefix="linux-ocr-preparation-", dir=destination.parent
    ) as temp:
        root = Path(temp)
        root.chmod(0o700)
        models = root / "tessdata"
        models.mkdir(mode=0o700)
        for model, resource_receipt in zip(
            lock["language_resources"], failure["language_resources"], strict=True
        ):
            resource_receipt["observed_size_bytes"] = 0
            resource_receipt["observed_sha256"] = hashlib.sha256(b"").hexdigest()
            url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{lock['commit']}/{model['language']}.traineddata"
            digest = hashlib.sha256()
            count = 0
            with (
                urllib.request.urlopen(url, timeout=30) as response,
                (models / f"{model['language']}.traineddata").open("xb") as output,
            ):
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("OCR asset preparation exceeded its deadline.")
                    chunk = response.read(min(65536, model["size_bytes"] - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    digest.update(chunk)
                    resource_receipt["observed_size_bytes"] = count
                    resource_receipt["observed_sha256"] = digest.hexdigest()
                    if count > model["size_bytes"]:
                        failure["failure_category"] = "model_size_mismatch"
                        raise ValueError("OCR model exceeded locked size.")
                    output.write(chunk)
            if count != model["size_bytes"] or digest.hexdigest() != model["sha256"]:
                failure["failure_category"] = "model_identity_mismatch"
                raise ValueError("OCR model failed locked hash/size verification.")
            (models / f"{model['language']}.traineddata").chmod(0o400)
        failure["stage"] = "binary_copy"
        binary = root / "tesseract"
        binary_digest = hashlib.sha256()
        with executable.open("rb") as source, binary.open("xb") as output:
            count = 0
            while chunk := source.read(65536):
                if time.monotonic() >= deadline or count + len(chunk) > MAX_BINARY_BYTES:
                    failure["failure_category"] = (
                        "deadline" if time.monotonic() >= deadline else "binary_budget"
                    )
                    raise ValueError("OCR binary exceeds preparation budget.")
                count += len(chunk)
                binary_digest.update(chunk)
                failure["binary_size_bytes"] = count
                failure["binary_sha256"] = binary_digest.hexdigest()
                output.write(chunk)
        binary.chmod(0o500)
        failure["stage"] = "binary_version"
        version = (
            subprocess.run(
                [str(binary), "--version"],
                check=True,
                capture_output=True,
                timeout=max(0.001, deadline - time.monotonic()),
                env={"LANG": "C", "LC_ALL": "C", "TZ": "UTC", "OMP_THREAD_LIMIT": "1"},
            )
            .stdout.decode("utf-8")
            .splitlines()[0]
            .split()[1]
        )
        failure["observed_version"] = version if re.fullmatch(r"[0-9.]{1,32}", version) else None
        if version != lock["expected_tesseract_version"]:
            failure["failure_category"] = "version_mismatch"
            raise ValueError("Distro Tesseract version does not match the fixed acceptance lock.")
        failure["stage"] = "package_identity"
        package = (
            subprocess.run(
                [
                    "/usr/bin/dpkg-query",
                    "-W",
                    "-f=${Package}\t${Version}\t${Architecture}\n",
                    "tesseract-ocr",
                ],
                check=True,
                capture_output=True,
                timeout=max(0.001, deadline - time.monotonic()),
                env={"LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
            )
            .stdout.decode("utf-8")
            .strip()
        )
        binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        receipt = {
            "schema_version": "linux-ocr-preparation-v1",
            "platform": release,
            "package": package,
            "source_executable": str(executable),
            "expected_tesseract_version": lock["expected_tesseract_version"],
            "observed_version": version,
            "binary_sha256": binary_hash,
            "resource_lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
            "language_resources": lock["language_resources"],
            "trust_boundary": (
                "Ubuntu distro installation; shared libraries and full OS are not hash-pinned"
            ),
        }
        (root / "preparation-receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        (root / "preparation-receipt.json").chmod(0o600)
        config = {
            "schema_version": "v2",
            "engine": "tesseract_tsv",
            "helper_path": str(destination / "tesseract"),
            "expected_version": version,
            "binary_sha256": binary_hash,
            "tessdata_directory": str(destination / "tessdata"),
            "language_resources": lock["language_resources"],
        }
        (root / "ocr_engine.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        (root / "ocr_engine.json").chmod(0o600)
        failure["stage"] = "publish"
        os.rename(root, destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.destination, args.executable)


if __name__ == "__main__":
    main()
