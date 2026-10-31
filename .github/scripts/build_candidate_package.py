"""Build candidate packages from the exact tracked Git tree, without release authority."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 20_000
MAX_BYTES = 128 * 1024 * 1024
PROJECT_INPUTS = {
    "pyproject.toml",
    "MANIFEST.in",
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
}
PACKAGE_SUFFIXES = {".py", ".json", ".sql", ".txt"}


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    )


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_name(value: str) -> str:
    path = PurePosixPath(value)
    require(
        value
        and not path.is_absolute()
        and str(path) == value
        and ".." not in path.parts
        and "\\" not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value),
        "unsafe archive path",
    )
    return value


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def tracked_snapshot(
    root: Path, identity: dict[str, Any]
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Read immutable blobs and compare every tracked working-tree byte/mode."""
    head = identity["head_sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", head), "invalid candidate SHA")
    require(
        git(root, "rev-parse", "HEAD").decode().strip() == head,
        "candidate checkout changed",
    )
    require(
        git(root, "rev-parse", "HEAD^{tree}").decode().strip() == identity["tested_tree"],
        "candidate tree changed",
    )
    require(
        not git(root, "status", "--porcelain", "--untracked-files=no"),
        "tracked source is dirty",
    )
    require(
        not git(root, "ls-files", "--others", "--exclude-standard"),
        "untracked source input",
    )
    entries = git(root, "ls-tree", "-r", "-l", "-z", head).split(b"\0")[:-1]
    require(0 < len(entries) <= MAX_FILES, "tracked source file bound exceeded")
    expected: dict[str, tuple[str, str, int]] = {}
    for entry in entries:
        metadata, name_bytes = entry.split(b"\t", 1)
        mode, kind, blob, size_text = metadata.decode().split()
        name = safe_name(name_bytes.decode("utf-8"))
        require(mode in {"100644", "100755"} and kind == "blob", "nonregular tracked source")
        require(name not in expected, "duplicate tracked source")
        expected[name] = mode, blob, int(size_text)
    require(
        sum(value[2] for value in expected.values()) <= MAX_BYTES,
        "tracked source byte bound exceeded",
    )
    raw = git(root, "archive", "--format=tar", head)
    require(len(raw) <= MAX_BYTES + MAX_FILES * 2048, "source archive bound exceeded")
    payloads: dict[str, bytes] = {}
    inventory: list[dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        require(archive.pax_headers.get("comment") == head, "source archive commit differs")
        for member in archive:
            name = safe_name(member.name.rstrip("/"))
            if member.isdir():
                require(
                    any(path.startswith(name + "/") for path in expected),
                    "unexpected source directory",
                )
                continue
            require(
                member.isfile()
                and not member.issparse()
                and name in expected
                and name not in payloads,
                "unexpected source archive member",
            )
            mode, blob, size = expected[name]
            require(member.size == size, "source archive size differs")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("source archive member unavailable")
            body = stream.read(size + 1)
            require(
                len(body) == size
                and hashlib.sha1(b"blob " + str(size).encode() + b"\0" + body).hexdigest() == blob,
                "source archive blob differs",
            )
            original = root / name
            for parent in original.parents:
                if parent == root:
                    break
                require(not parent.is_symlink(), "symlink source ancestor")
            require(
                original.is_file() and not original.is_symlink() and original.read_bytes() == body,
                "tracked checkout bytes differ",
            )
            require(
                bool(original.stat().st_mode & stat.S_IXUSR) == (mode == "100755"),
                "source mode differs",
            )
            payloads[name] = body
            inventory.append(
                {
                    "path": name,
                    "mode": mode,
                    "git_blob": blob,
                    "size_bytes": size,
                    "sha256": sha256(body),
                }
            )
    require(set(payloads) == set(expected), "tracked source archive is incomplete")
    return payloads, sorted(inventory, key=lambda item: item["path"])


def package_payloads(payloads: dict[str, bytes]) -> dict[str, bytes]:
    selected = {
        name: body
        for name, body in payloads.items()
        if name.startswith("finance_core/") and PurePosixPath(name).suffix in PACKAGE_SUFFIXES
    }
    require(
        "finance_core/__init__.py" in selected and PROJECT_INPUTS <= payloads.keys(),
        "package input missing",
    )
    return selected


def sdist_metadata(payloads: dict[str, bytes], version: str) -> dict[str, bytes]:
    """The exact generated metadata of the reviewed setuptools 84.1.0 recipe."""
    project = tomllib.loads(payloads["pyproject.toml"].decode())["project"]
    require(
        project["name"] == "finance-core" and project["version"] == version,
        "sdist project identity differs",
    )
    require(project["readme"] == "README.md", "sdist readme recipe differs")
    headers = [
        "Metadata-Version: 2.4",
        "Name: finance-core",
        f"Version: {version}",
        f"Summary: {project['description']}",
        f"License-Expression: {project['license']}",
        f"Requires-Python: {project['requires-python']}",
        "Description-Content-Type: text/markdown",
    ]
    headers += [f"License-File: {name}" for name in project["license-files"]]
    headers += [f"Requires-Dist: {requirement}" for requirement in project["dependencies"]]
    requirements = "".join(f"{requirement}\n" for requirement in project["dependencies"])
    for extra, dependencies in project.get("optional-dependencies", {}).items():
        headers.append(f"Provides-Extra: {extra}")
        headers += [
            f'Requires-Dist: {requirement}; extra == "{extra}"' for requirement in dependencies
        ]
        requirements += f"\n[{extra}]\n" + "".join(f"{item}\n" for item in dependencies)
    headers.append("Dynamic: license-file")
    metadata = ("\n".join(headers) + "\n\n").encode() + payloads["README.md"]
    return {
        "PKG-INFO": metadata,
        "finance_core.egg-info/PKG-INFO": metadata,
        "finance_core.egg-info/dependency_links.txt": b"\n",
        "finance_core.egg-info/requires.txt": requirements.encode(),
        "finance_core.egg-info/top_level.txt": b"finance_core\n",
        "setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
    }


def verify_distributions(
    wheel: Path, sdist: Path, payloads: dict[str, bytes], version: str
) -> None:
    """Check complete Core/resource membership and byte identity in both archives."""
    expected = package_payloads(payloads)
    generated = sdist_metadata(payloads, version)
    sources_name = "finance_core.egg-info/SOURCES.txt"
    allowed = set(expected) | PROJECT_INPUTS | set(generated) | {sources_name}
    allowed_directories = {""} | {
        str(parent)
        for name in allowed
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    dist_info = f"finance_core-{version}.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        entries = archive.infolist()
        names = [safe_name(member.filename) for member in entries]
        require(
            len(names) == len(set(names)) and len(names) <= MAX_FILES,
            "duplicate wheel member",
        )
        require(
            sum(member.file_size for member in entries) <= MAX_BYTES,
            "wheel expanded bound exceeded",
        )
        for wheel_member in entries:
            mode = wheel_member.external_attr >> 16
            require(
                not wheel_member.is_dir() and stat.S_IFMT(mode) in {0, stat.S_IFREG},
                "nonregular wheel wheel_member",
            )
            require(
                wheel_member.filename.startswith("finance_core/")
                or wheel_member.filename.startswith(dist_info),
                "unexpected wheel wheel_member",
            )
        actual = {name for name in names if name.startswith("finance_core/")}
        require(actual == set(expected), "wheel package inventory differs")
        require(
            all(archive.read(name) == body for name, body in expected.items()),
            "wheel source bytes differ",
        )
        require(
            f"{dist_info}METADATA" in names
            and f"{dist_info}RECORD" in names
            and f"{dist_info}WHEEL" in names,
            "wheel metadata missing",
        )
        metadata = archive.read(f"{dist_info}METADATA")
        require(metadata == generated["PKG-INFO"], "wheel metadata differs from source recipe")
    prefix = f"finance_core-{version}/"
    actual_sdist: dict[str, bytes] = {}
    seen_members: set[str] = set()
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive:
            name = safe_name(member.name.rstrip("/"))
            require(
                name == prefix.rstrip("/") or name.startswith(prefix),
                "unexpected sdist root",
            )
            require(
                name not in seen_members and len(seen_members) < MAX_FILES,
                "duplicate or excessive sdist member",
            )
            seen_members.add(name)
            relative = "" if name == prefix.rstrip("/") else name[len(prefix) :]
            if member.isdir():
                require(relative in allowed_directories, "undeclared sdist directory")
                continue
            require(
                member.isfile() and not member.issparse() and member.size <= MAX_BYTES,
                "nonregular sdist member",
            )
            require(relative in allowed, "undeclared sdist member")
            require(
                relative in expected or not member.mode & 0o111,
                "executable sdist metadata or project input",
            )
            require(
                relative and relative not in actual_sdist and len(actual_sdist) < MAX_FILES,
                "duplicate sdist member",
            )
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("sdist member unavailable")
            actual_sdist[relative] = stream.read(member.size + 1)
            require(
                sum(map(len, actual_sdist.values())) <= MAX_BYTES,
                "sdist expanded bound exceeded",
            )
    require(
        set(actual_sdist) == allowed,
        "sdist complete inventory differs",
    )
    require(
        all(actual_sdist[name] == body for name, body in expected.items()),
        "sdist source bytes differ",
    )
    require(
        all(actual_sdist.get(name) == payloads[name] for name in PROJECT_INPUTS),
        "sdist recipe inputs differ",
    )
    require(
        all(actual_sdist[name] == body for name, body in generated.items()),
        "sdist generated metadata differs from source recipe",
    )
    try:
        sources = actual_sdist[sources_name].decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ValueError("sdist SOURCES inventory is not UTF-8") from error
    source_inventory = (
        set(expected)
        | PROJECT_INPUTS
        | {name for name in allowed if name.startswith("finance_core.egg-info/")}
    )
    require(
        len(sources) == len(set(sources))
        and {safe_name(name) for name in sources} == source_inventory,
        "sdist SOURCES inventory differs",
    )


def build(root: Path, destination: Path, identity: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve(strict=True)
    require(
        destination.is_absolute() and not destination.exists() and not destination.is_symlink(),
        "output must be a fresh absolute directory",
    )
    require(
        not destination.resolve().is_relative_to(root),
        "output must be outside source checkout",
    )
    require(
        identity["tested_sha"] == identity["head_sha"],
        "identity is not exact candidate",
    )
    require(sys.version_info[:2] == (3, 12), "package producer requires Python 3.12")
    versions = {name: importlib.metadata.version(name) for name in ("build", "setuptools")}
    require(
        versions == {"build": "1.6.1", "setuptools": "84.1.0"},
        "build tools differ from locked recipe",
    )
    payloads, inventory = tracked_snapshot(root, identity)
    project = tomllib.loads(payloads["pyproject.toml"].decode())
    require(
        project["build-system"]
        == {
            "requires": ["setuptools==84.1.0"],
            "build-backend": "setuptools.build_meta",
        },
        "build backend differs from locked recipe",
    )
    require(project["project"]["name"] == "finance-core", "unexpected package")
    version = project["project"]["version"]
    require(
        isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version),
        "invalid package version",
    )
    destination.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="finance-candidate-build-") as temporary:
        snapshot = Path(temporary) / "source"
        snapshot.mkdir()
        for entry in inventory:
            target = snapshot / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payloads[entry["path"]])
            target.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "GH_TOKEN", "GITHUB_TOKEN"}
            and not key.startswith("PIP_")
        }
        environment.update({"PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(destination),
                str(snapshot),
            ],
            check=True,
            env=environment,
            timeout=600,
        )
    wheel = destination / f"finance_core-{version}-py3-none-any.whl"
    sdist = destination / f"finance_core-{version}.tar.gz"
    require(set(destination.iterdir()) == {wheel, sdist}, "unexpected distribution outputs")
    verify_distributions(wheel, sdist, payloads, version)
    # Check source again after build; the backend only received a copied tracked tree.
    after, after_inventory = tracked_snapshot(root, identity)
    require(
        after == payloads and after_inventory == inventory,
        "candidate changed during build",
    )
    inventory_bytes = encoded(
        {
            "schema": "finance-candidate-source-inventory-v1",
            "identity": identity,
            "files": inventory,
        }
    )
    (destination / "source-inventory.json").write_bytes(inventory_bytes)
    recipe = {
        "command": [
            "python",
            "-I",
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            "OUTPUT",
            "TRACKED_SNAPSHOT",
        ],
        "python": sys.version.split()[0],
        "tools": versions,
        "requirements_dev_sha256": sha256(payloads["requirements-dev.txt"]),
        "pyproject_sha256": sha256(payloads["pyproject.toml"]),
        "manifest_sha256": sha256(payloads["MANIFEST.in"]),
        "producer_sha256": sha256(payloads[".github/scripts/build_candidate_package.py"]),
    }
    record = {
        "schema": "finance-candidate-package-v1",
        "status": "BUILT_NOT_VALIDATED",
        "identity": identity,
        "source_inventory_sha256": sha256(inventory_bytes),
        "recipe": recipe,
        "recipe_sha256": sha256(encoded(recipe)),
        "artifacts": {
            path.name: {
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
            for path in (wheel, sdist)
        },
    }
    (destination / "package-receipt.json").write_bytes(encoded(record))
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    record = build(Path.cwd(), args.output, json.loads(args.identity.read_bytes()))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"receipt_sha256={sha256(encoded(record))}\n")


if __name__ == "__main__":
    main()
