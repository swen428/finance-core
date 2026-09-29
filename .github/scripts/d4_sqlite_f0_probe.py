"""Collect a synthetic, upstream-only SQLite Unix I/O inventory on this host.

The output is diagnostic data. It does not establish reachable-path closure,
native FD safety, lawful recovery, or an F0/G1 pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

SQLITE_URL = "https://www.sqlite.org/2026/sqlite-amalgamation-3530100.zip"
SQLITE_ZIP_SHA256 = "36ad6e7f38540a3b21a2ac36340833f0a9e426bc1c752751c3ba669466827eae"
SQLITE_C_SHA256 = "9ac97192c93a6d9671d2ea667ba14e3c8fe3719a171afc5827c9d86d0278fc14"
SQLITE_C_MEMBER = "sqlite-amalgamation-3530100/sqlite3.c"
COMPILE_FLAGS = [
    "-std=c11",
    "-O1",
    "-fPIC",
    "-DSQLITE_THREADSAFE=1",
    "-DSQLITE_ENABLE_LOCKING_STYLE=0",
    "-DSQLITE_OMIT_LOAD_EXTENSION=1",
    "-DSQLITE_TEMP_STORE=3",
    "-DSQLITE_MAX_MMAP_SIZE=0",
    "-DSQLITE_OMIT_SHARED_CACHE=1",
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(*command: str, timeout: int = 180) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def optional_run(*command: str) -> str | None:
    try:
        return run(*command, timeout=15)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--source-zip", type=Path, help="Pinned offline input for local reproduction"
    )
    parser.add_argument("--clang", default="clang")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.source_zip:
        zipped = args.source_zip.read_bytes()
        source_origin = str(args.source_zip.resolve())
    else:
        with urllib.request.urlopen(SQLITE_URL, timeout=30) as response:
            zipped = response.read(16 * 1024 * 1024 + 1)
        if len(zipped) > 16 * 1024 * 1024:
            parser.error("SQLite download exceeds diagnostic limit")
        source_origin = SQLITE_URL
    if sha256(zipped) != SQLITE_ZIP_SHA256:
        parser.error("SQLite ZIP SHA-256 mismatch")
    with zipfile.ZipFile(BytesIO(zipped)) as archive:
        source = archive.read(SQLITE_C_MEMBER)
    if sha256(source) != SQLITE_C_SHA256:
        parser.error("sqlite3.c SHA-256 mismatch")
    source_path = out / "sqlite3.c"
    source_path.write_bytes(source)

    preprocessed = out / "sqlite3.preprocessed.c"
    ast = out / "sqlite3.ast.json"
    inventory = out / "callsite-inventory.json"
    run(args.clang, "-E", "-x", "c", *COMPILE_FLAGS, str(source_path), "-o", str(preprocessed))
    with ast.open("wb") as stream:
        subprocess.run(
            [
                args.clang,
                "-w",
                "-x",
                "c",
                *COMPILE_FLAGS,
                "-Xclang",
                "-ast-dump=json",
                "-fsyntax-only",
                str(preprocessed),
            ],
            check=True,
            stdout=stream,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    run(
        sys.executable,
        str(Path(__file__).with_name("d4_sqlite_callsite_inventory.py")),
        "--sqlite-source",
        str(source_path),
        "--preprocessed",
        str(preprocessed),
        "--ast",
        str(ast),
        "--output",
        str(inventory),
        timeout=180,
    )

    mount = optional_run("findmnt", "-T", str(out), "-no", "SOURCE,FSTYPE,OPTIONS")
    filesystem_type = mount.split()[1] if mount and len(mount.split()) >= 2 else None
    stat_fs_label = optional_run("stat", "-f", "-c", "%T", str(out))
    if sys.platform == "darwin":
        df = optional_run("df", "-P", str(out))
        mounted_at = df.splitlines()[-1].split()[-1] if df else None
        mounted = optional_run("mount")
        if mounted_at and mounted:
            mount = next(
                (line for line in mounted.splitlines() if f" on {mounted_at} (" in line),
                None,
            )
            if mount:
                filesystem_type = mount.partition(" (")[2].partition(",")[0]
        stat_fs_label = None
    inventory_data = json.loads(inventory.read_text())
    if inventory_data["sqlite3_c_sha256"] != sha256(source):
        raise RuntimeError("inventory original-source digest disagrees with probe")
    if inventory_data["preprocessed_sha256"] != sha256(preprocessed.read_bytes()):
        raise RuntimeError("inventory preprocessed digest disagrees with probe")
    if inventory_data["ast_sha256"] != sha256(ast.read_bytes()):
        raise RuntimeError("inventory AST digest disagrees with probe")
    info = {
        "status": "UPSTREAM_BASELINE_ONLY_NOT_F0_PASS",
        "source_origin": source_origin,
        "source_zip_sha256": sha256(zipped),
        "sqlite3_c_sha256": sha256(source),
        "preprocessed_sha256": sha256(preprocessed.read_bytes()),
        "ast_sha256": sha256(ast.read_bytes()),
        "callsite_inventory_sha256": sha256(inventory.read_bytes()),
        "compile_flags": COMPILE_FLAGS,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "clang_version": run(args.clang, "--version").splitlines()[0],
        "libc": platform.libc_ver(),
        "kernel": os.uname().release,
        "mount": mount,
        "filesystem_type": filesystem_type,
        "stat_filesystem_label": stat_fs_label,
        "macos_build": optional_run("sw_vers", "-buildVersion")
        if sys.platform == "darwin"
        else None,
        "runner_image": {
            "os": os.environ.get("ImageOS"),
            "version": os.environ.get("ImageVersion"),
        },
        "inventory": inventory_data,
    }
    # The AST is reproducible but large; retain its digest, not the 600 MB file.
    ast.unlink()
    (out / "probe.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "status": info["status"],
                "source_sha256": info["sqlite3_c_sha256"],
                "preprocessed_sha256": info["preprocessed_sha256"],
                "machine": info["machine"],
                "mount": info["mount"],
                "call_count": info["inventory"]["call_count"],
                "upper_sqlite_os_call_count": info["inventory"]["upper_sqlite_os_call_count"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
