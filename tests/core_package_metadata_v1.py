"""Build real candidate wheel metadata for two fixed bundle test modules only.

The CI requirements environment deliberately has no installed finance-core.
These tests need the candidate wheel's real dist-info in both pytest and a
fixed worker subprocess, while continuing to import source from this checkout.
"""

from __future__ import annotations

import email.parser
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

_PROJECT_FILES = (
    "pyproject.toml",
    "MANIFEST.in",
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
)
_PACKAGE_SUFFIXES = frozenset({".py", ".json", ".sql", ".txt"})


def build_candidate_core_metadata(source_root: Path, scratch: Path) -> tuple[Path, str]:
    """Copy exact package inputs, build a wheel, and expose only its dist-info.

    The build runs in *scratch*, so setuptools cannot write egg-info or build
    products into the candidate checkout.  Every packaged Core member is
    compared byte-for-byte with the original source before metadata is used.
    """
    source_root = source_root.resolve(strict=True)
    scratch = scratch.resolve(strict=True)
    project = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    if project["project"]["name"] != "finance-core":
        raise AssertionError("Test candidate project identity differs")
    version = project["project"]["version"]
    if type(version) is not str or not version:
        raise AssertionError("Test candidate version is invalid")

    copied_root = scratch / "candidate-source"
    copied_root.mkdir(mode=0o700)
    for name in _PROJECT_FILES:
        original = source_root / name
        if not original.is_file() or original.is_symlink():
            raise AssertionError(f"Test candidate packaging input is invalid: {name}")
        shutil.copyfile(original, copied_root / name)

    expected: dict[str, bytes] = {}
    package_root = source_root / "finance_core"
    for original in sorted(package_root.rglob("*")):
        if original.is_symlink():
            raise AssertionError("Test candidate package contains a symlink")
        if not original.is_file() or original.suffix not in _PACKAGE_SUFFIXES:
            continue
        relative = original.relative_to(source_root)
        member = relative.as_posix()
        expected[member] = original.read_bytes()
        copied = copied_root / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(expected[member])
    if not expected or "finance_core/__init__.py" not in expected:
        raise AssertionError("Test candidate package is empty")

    wheel_dir = scratch / "wheel"
    wheel_dir.mkdir(mode=0o700)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_dir),
            str(copied_root),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"Candidate wheel build failed: {result.stderr[-3000:]}")
    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise AssertionError("Candidate wheel build did not produce one wheel")

    metadata_root = scratch / "metadata-only"
    metadata_root.mkdir(mode=0o700)
    with zipfile.ZipFile(wheels[0]) as archive:
        members = archive.namelist()
        if len(members) != len(set(members)):
            raise AssertionError("Candidate wheel contains duplicate members")
        packaged = {name for name in members if name.startswith("finance_core/")}
        if packaged != set(expected):
            raise AssertionError("Candidate wheel package membership differs from source")
        for name, source_bytes in expected.items():
            if archive.read(name) != source_bytes:
                raise AssertionError(f"Candidate wheel member differs from source: {name}")

        dist_info = f"finance_core-{version}.dist-info/"
        names = [name for name in members if name.startswith(dist_info) and not name.endswith("/")]
        if f"{dist_info}METADATA" not in names or f"{dist_info}RECORD" not in names:
            raise AssertionError("Candidate wheel has no complete real dist-info")
        metadata = email.parser.Parser().parsestr(
            archive.read(f"{dist_info}METADATA").decode("utf-8")
        )
        if metadata.get("Name") != "finance-core" or metadata.get("Version") != version:
            raise AssertionError("Candidate wheel metadata identity differs")
        for name in names:
            wheel_member_path = PurePosixPath(name)
            if ".." in wheel_member_path.parts or wheel_member_path.is_absolute():
                raise AssertionError("Candidate wheel metadata path is unsafe")
            destination = metadata_root.joinpath(*wheel_member_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(name))
    return metadata_root, version
