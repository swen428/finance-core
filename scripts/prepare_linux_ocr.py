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
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

LOCK = Path(__file__).with_name("linux_ocr_assets_v1.json")
MAX_BINARY_BYTES = 32 * 1024 * 1024


def prepare(destination: Path, executable: Path) -> None:
    if not destination.is_absolute() or destination.exists():
        raise ValueError("Preparation requires a new absolute private directory.")
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("OCR preparation requires Ubuntu 24.04 x86_64.")
    release = platform.freedesktop_os_release()
    if (release.get("ID"), release.get("VERSION_ID")) != ("ubuntu", "24.04"):
        raise ValueError("OCR preparation requires Ubuntu 24.04 x86_64.")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 120
    with tempfile.TemporaryDirectory(
        prefix="linux-ocr-preparation-", dir=destination.parent
    ) as temp:
        root = Path(temp)
        root.chmod(0o700)
        models = root / "tessdata"
        models.mkdir(mode=0o700)
        for model in lock["language_resources"]:
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
                    if count > model["size_bytes"]:
                        raise ValueError("OCR model exceeded locked size.")
                    digest.update(chunk)
                    output.write(chunk)
            if count != model["size_bytes"] or digest.hexdigest() != model["sha256"]:
                raise ValueError("OCR model failed locked hash/size verification.")
            (models / f"{model['language']}.traineddata").chmod(0o400)
        binary = root / "tesseract"
        with executable.open("rb") as source, binary.open("xb") as output:
            count = 0
            while chunk := source.read(65536):
                if time.monotonic() >= deadline or count + len(chunk) > MAX_BINARY_BYTES:
                    raise ValueError("OCR binary exceeds preparation budget.")
                count += len(chunk)
                output.write(chunk)
        binary.chmod(0o500)
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
        if version != lock["expected_tesseract_version"]:
            raise ValueError("Distro Tesseract version does not match the fixed acceptance lock.")
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
        os.rename(root, destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.destination, args.executable)


if __name__ == "__main__":
    main()
