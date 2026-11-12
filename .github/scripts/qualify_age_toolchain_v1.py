"""Qualify the pinned Linux age tools in a caller-selected temporary directory.

This explicit CI preparation step is the only networked age-tool path. Finance
runtime code receives absolute executable paths and never downloads tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

RELEASE = "v1.3.2"
SOURCE_COMMIT = "b74dce4cdbe35b5e5f66c06d9612b72f89028758"
SIGSUM_POLICY = "sigsum-generic-2025-1"
SIGSUM_VERSION = "sigsum-verify (sigsum-go module) v0.13.1"
MAX_EXPANDED_BYTES = 96 * 1024 * 1024

LINUX_MEMBERS = {
    "age": ("directory", 0),
    "age/age-keygen": ("file", 4051278),
    "age/age-plugin-tag": ("file", 4276743),
    "age/age-plugin-batchpass": ("file", 4224528),
    "age/age-plugin-pq": ("file", 4339590),
    "age/age-plugin-tagpq": ("file", 4291077),
    "age/age": ("file", 6977014),
    "age/LICENSE": ("file", 2975),
    "age/age-inspect": ("file", 4511789),
}

LINUX: dict[str, Any] = {
    "platform": "linux-x86_64",
    "asset_suffix": "linux-amd64",
    "archive_sha256": "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10",
    "proof_sha256": "98402fa59b4e433426156b4a7e74a32e44902e0434c61158f7de18ffc84f1f97",
    "tools": {
        "age": {
            "sha256": "eb7dd1b518f0a307c99cd97782623c5321da049154b04acd2d98d21aa7bc9b2c",
            "size_bytes": 6977014,
        },
        "age_keygen": {
            "sha256": "0a0009db842259d6717f7eeb30acb6b90d2a2eb924c6acd0a0db0ca1f1537899",
            "size_bytes": 4051278,
        },
    },
}

_HEX = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def expected_registry() -> dict[str, Any]:
    return {
        "format": "native-age-toolchain-v1",
        "release": RELEASE,
        "source_commit": SOURCE_COMMIT,
        "platforms": {
            "darwin-arm64": {
                "age": {
                    "sha256": "4012dfc2725883beafb710894af4f599b7a94f8c8e0f51f02cc96ab8df33915e",
                    "size_bytes": 6502258,
                },
                "age_keygen": {
                    "sha256": "c16e229245123d0ad27442317461d63915416cad0294395cd19ca93feb3211ea",
                    "size_bytes": 3806562,
                },
            },
            LINUX["platform"]: LINUX["tools"],
        },
    }


def check_registry(root: Path | None = None) -> dict[str, Any]:
    repository = root or Path(__file__).resolve().parents[2]
    path = repository / "finance_core/resources/crypto/age_toolchain_v1.json"
    record = json.loads(path.read_bytes())
    require(record == expected_registry(), "packaged age registry differs from fixed pins")
    return record


def download(url: str, target: Path, expected_sha256: str, *, maximum: int) -> dict[str, Any]:
    require(url.startswith("https://"), "tool download URL must use HTTPS")
    require(_HEX.fullmatch(expected_sha256) is not None, "invalid pinned download digest")
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(
        url, headers={"User-Agent": "finance-core-age-qualification-v1"}
    )
    with urllib.request.urlopen(request, timeout=45) as response, target.open("xb") as output:
        require(response.geturl().startswith("https://"), "tool download redirect is not HTTPS")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            require(size <= maximum, "tool download exceeds its fixed bound")
            digest.update(chunk)
            output.write(chunk)
    observed = digest.hexdigest()
    require(observed == expected_sha256, f"download digest mismatch: {target.name}")
    return {"filename": target.name, "size_bytes": size, "sha256": observed}


def extract_tools(archive_path: Path, destination: Path) -> dict[str, dict[str, Any]]:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    expected = {"age/age": "age", "age/age-keygen": "age_keygen"}
    found: dict[str, bytes] = {}
    names: set[str] = set()
    expanded = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        require(len(members) == len(LINUX_MEMBERS), "age archive member inventory differs")
        for member in members:
            path = PurePosixPath(member.name)
            require(
                not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in member.name
                and member.name not in names,
                "unsafe or duplicate age archive path",
            )
            names.add(member.name)
            expected_type, expected_size = LINUX_MEMBERS.get(member.name, ("", -1))
            require(expected_type != "", "age archive contains an undeclared member")
            require(
                (expected_type == "directory" and member.isdir())
                or (expected_type == "file" and member.isfile() and not member.issparse()),
                "age archive member type differs",
            )
            require(member.size == expected_size, "age archive member size differs")
            if member.isfile():
                expanded += member.size
                require(expanded <= MAX_EXPANDED_BYTES, "age archive expanded bound exceeded")
            role = expected.get(member.name)
            if role is None:
                continue
            require(member.isfile() and not member.issparse(), "pinned tool is not a regular file")
            require(0 < member.size <= 16 * 1024 * 1024, "pinned tool size is outside bounds")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("pinned tool member is unavailable")
            body = stream.read(member.size + 1)
            require(len(body) == member.size, "pinned tool member size changed")
            found[role] = body
    require(set(found) == set(expected.values()), "age archive omits a pinned tool")
    records = {}
    for role, body in found.items():
        pin = LINUX["tools"][role]
        require(len(body) == pin["size_bytes"], f"{role} size differs from registry pin")
        digest = sha256(body)
        require(digest == pin["sha256"], f"{role} digest differs from registry pin")
        name = "age" if role == "age" else "age-keygen"
        target = destination / name
        with target.open("xb") as output:
            output.write(body)
        target.chmod(0o500)
        records[role] = {
            "path": str(target),
            "sha256": digest,
            "size_bytes": len(body),
            "version": "v1.3.2",
        }
    return records


def _run_bounded(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> bytes:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    require(
        len(result.stdout) <= 65536 and len(result.stderr) <= 4096, "tool output bound exceeded"
    )
    require(result.returncode == 0, f"fixed qualification command failed: {Path(command[0]).name}")
    return result.stdout


def qualify(*, output_dir: Path, tool_directory: Path) -> dict[str, Any]:
    require(sys.version_info[:2] == (3, 12), "age qualification requires Python 3.12")
    require(
        platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"},
        "age qualification requires Linux x86_64",
    )
    release = platform.freedesktop_os_release()
    require(
        release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "24.04",
        "age qualification requires Ubuntu 24.04",
    )
    check_registry()
    require(
        output_dir.is_absolute() and not output_dir.exists(),
        "qualification output must be a new absolute directory",
    )
    require(
        tool_directory.is_absolute() and not tool_directory.exists(),
        "tool directory must be a new absolute path",
    )
    output_dir.mkdir(mode=0o700, parents=True)
    work = output_dir / "work"
    work.mkdir(mode=0o700)
    base = "https://github.com/FiloSottile/age/releases/download/v1.3.2/"
    archive_name = f"age-v1.3.2-{LINUX['asset_suffix']}.tar.gz"
    archive = work / archive_name
    proof = work / (archive_name + ".proof")
    source_record = download(
        base + archive_name, archive, LINUX["archive_sha256"], maximum=64 * 1024 * 1024
    )
    proof_record = download(
        base + proof.name, proof, LINUX["proof_sha256"], maximum=16 * 1024 * 1024
    )
    extracted = extract_tools(archive, tool_directory)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    for tool in extracted.values():
        version = (
            _run_bounded([tool["path"], "--version"], cwd=work, env=environment, timeout=30)
            .decode()
            .strip()
        )
        require(version == "v1.3.2", "pinned age tool reported the wrong version")
        tool["version"] = version

    source_auth = {
        "status": "PREQUALIFIED_SIGSUM_PROOF_REUSED",
        "source_commit": SOURCE_COMMIT,
        "archive_sha256": source_record["sha256"],
        "proof_sha256": proof_record["sha256"],
        "policy": SIGSUM_POLICY,
        "verifier_version": SIGSUM_VERSION,
    }
    qualification = {
        "schema": "finance-age-toolchain-qualification-v1",
        "status": "PINNED_LINUX_TOOLS_READY",
        "platform": LINUX["platform"],
        "release": RELEASE,
        "source_commit": SOURCE_COMMIT,
        "source_authentication": source_auth,
        "archive": source_record,
        "proof": proof_record,
        "archive_members": [
            {"name": name, "type": member_type, "size_bytes": size}
            for name, (member_type, size) in LINUX_MEMBERS.items()
        ],
        "tools": extracted,
    }
    result_path = output_dir / "qualification-result.json"
    result_path.write_text(
        json.dumps(qualification, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    result_path.chmod(0o600)
    return qualification


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tool-directory", type=Path, required=True)
    args = parser.parse_args()
    result = qualify(
        output_dir=args.output_dir,
        tool_directory=args.tool_directory,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "platform": result["platform"],
                "tools": {
                    name: {
                        "sha256": value["sha256"],
                        "size_bytes": value["size_bytes"],
                        "version": value["version"],
                    }
                    for name, value in result["tools"].items()
                },
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
