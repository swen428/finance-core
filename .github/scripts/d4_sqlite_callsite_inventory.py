"""Enumerate compiled SQLite Unix VFS call expressions for a D4 diagnostic.

This is a syntactic inventory, not a reachability or safety proof.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from pathlib import Path

# Includes callbacks and platform helpers so omissions are visible. These names
# are candidates; the report never treats them as a complete effect set.
OS_EFFECTS = {
    "open",
    "openat",
    "close",
    "read",
    "pread",
    "pread64",
    "write",
    "pwrite",
    "pwrite64",
    "lseek",
    "ftruncate",
    "truncate",
    "fcntl",
    "flock",
    "stat",
    "lstat",
    "fstat",
    "fstatat",
    "fstatfs",
    "access",
    "faccessat",
    "unlink",
    "unlinkat",
    "rename",
    "renameat",
    "renameatx_np",
    "mkdir",
    "rmdir",
    "mmap",
    "munmap",
    "msync",
    "fsync",
    "fdatasync",
    "fchmod",
    "fchown",
    "utimes",
    "futimes",
    "readlink",
    "getcwd",
    "getpagesize",
    "sysconf",
    "gettimeofday",
    "nanosleep",
    "sleep",
    "usleep",
}
HELPERS = {
    "robust_open",
    "robust_close",
    "robust_ftruncate",
    "openDirectory",
    "unixOpen",
    "unixClose",
    "unixDelete",
    "unixAccess",
    "unixFullPathname",
    "unixRandomness",
    "unixSync",
    "unixFileControl",
    "unixShmMap",
    "unixShmLock",
    "unixShmUnmap",
    "unixOpenSharedMemory",
    "unixLock",
    "unixUnlock",
    "unixRead",
    "unixWrite",
    "unixTruncate",
    "unixFileSize",
    "unixFetch",
    "unixUnfetch",
    "unixSetSystemCall",
    "full_fsync",
    "seekAndRead",
    "seekAndWrite",
    "seekAndWriteFd",
    "unixShmSystemLock",
}


def walk(node: dict):
    yield node
    for child in node.get("inner", ()):
        if isinstance(child, dict):
            yield from walk(child)


def callee_info(node: dict) -> tuple[str | None, str]:
    for item in walk(node):
        if item.get("kind") == "DeclRefExpr":
            referenced = item.get("referencedDecl", {})
            if referenced.get("kind") == "FunctionDecl" and referenced.get("name"):
                return referenced["name"], "direct_function"
    for item in walk(node):
        if item.get("kind") == "MemberExpr" and item.get("name"):
            return item["name"], "member_dispatch"
    for item in walk(node):
        if item.get("kind") == "DeclRefExpr":
            name = item.get("referencedDecl", {}).get("name")
            if name:
                return name, "indirect_reference"
    return None, "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-source", type=Path, required=True)
    parser.add_argument("--preprocessed", type=Path, required=True)
    parser.add_argument("--ast", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-preprocessed-sha256")
    args = parser.parse_args()
    raw = args.preprocessed.read_bytes()
    sqlite3_c_sha256 = hashlib.sha256(args.sqlite_source.read_bytes()).hexdigest()
    preprocessed_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        args.expected_preprocessed_sha256
        and preprocessed_sha256 != args.expected_preprocessed_sha256
    ):
        parser.error("preprocessed source SHA-256 mismatch")
    ast_raw = args.ast.read_bytes()
    root = json.loads(ast_raw)
    table_match = re.search(rb"aSyscall\[\]\s*=\s*\{(.*?)\n\};", raw, re.DOTALL)
    if not table_match:
        raise RuntimeError("SQLite aSyscall initializer not found")
    syscall_names = [name.decode() for name in re.findall(rb'\{\s*"([^"]+)"', table_match.group(1))]
    newlines = [index for index, char in enumerate(raw) if char == 10]
    original_lines = {}
    original_files = {}
    marker = re.compile(rb'^#\s+(\d+)\s+"([^"]+)"')
    current_original_line = None
    current_file = None
    for p_number, line_bytes in enumerate(raw.splitlines(), 1):
        match = marker.match(line_bytes)
        if match:
            current_original_line = int(match.group(1))
            current_file = match.group(2).decode("utf-8", "replace")
        elif current_original_line is not None:
            if current_file and current_file.endswith("/sqlite3.c"):
                original_lines[p_number] = current_original_line
            if current_file:
                original_files[p_number] = Path(current_file).name
            current_original_line += 1

    def line_for(offset: int) -> int:
        return bisect.bisect_left(newlines, offset) + 1

    rows = []
    functions = []
    upper_os_calls = []
    for decl in root.get("inner", ()):
        if decl.get("kind") != "FunctionDecl":
            continue
        loc = decl.get("loc", {})
        p_line = loc.get("line")
        s_line = loc.get("presumedLine")
        if any(child.get("kind") == "CompoundStmt" for child in decl.get("inner", ())):
            for item in walk(decl):
                if item.get("kind") != "CallExpr" or not item.get("inner"):
                    continue
                os_name, target_kind = callee_info(item["inner"][0])
                if target_kind != "direct_function":
                    continue
                if not os_name or not os_name.startswith("sqlite3Os"):
                    continue
                span = item.get("range", {})
                start = span.get("begin", {}).get("offset")
                end = span.get("end", {}).get("offset")
                end_len = span.get("end", {}).get("tokLen", 0)
                if start is None or end is None:
                    continue
                p_call_line = line_for(start)
                expression = raw[start : end + end_len].decode("utf-8", "replace")
                upper_os_calls.append(
                    {
                        "function": decl.get("name", "<unnamed>"),
                        "p_line": p_call_line,
                        "s_file": original_files.get(p_call_line),
                        "s_line": span.get("begin", {}).get("presumedLine")
                        or original_lines.get(p_call_line),
                        "callee": os_name,
                        "expression": re.sub(r"\s+", " ", expression).strip()[:300],
                    }
                )
        # SQLite's original-source Unix VFS interval, as compiled on this host.
        if not (p_line and s_line and 40500 <= s_line <= 48620):
            continue
        if not any(child.get("kind") == "CompoundStmt" for child in decl.get("inner", ())):
            continue
        function = decl.get("name", "<unnamed>")
        functions.append({"name": function, "p_line": p_line, "s_line": s_line})
        for item in walk(decl):
            if item.get("kind") != "CallExpr":
                continue
            span = item.get("range", {})
            start = span.get("begin", {}).get("offset")
            end = span.get("end", {}).get("offset")
            end_len = span.get("end", {}).get("tokLen", 0)
            if start is None or end is None:
                continue
            expression = raw[start : end + end_len].decode("utf-8", "replace")
            expression = re.sub(r"\s+", " ", expression).strip()
            callee, target_kind = (
                callee_info(item["inner"][0]) if item.get("inner") else (None, "unknown")
            )
            if re.search(r"aSyscall\[\d+\]", expression):
                category = "indirect_aSyscall"
                callee = "aSyscall"
            elif target_kind != "direct_function":
                category = "indirect_dispatch_candidate"
            elif callee in OS_EFFECTS:
                category = "direct_os_effect_candidate"
            elif callee in HELPERS:
                category = "vfs_or_helper_candidate"
            elif callee and (callee.startswith("sqlite3Os") or callee.startswith("os")):
                category = "sqlite_os_wrapper_candidate"
            else:
                category = "unclassified_direct_call"
            slot_match = (
                re.search(r"aSyscall\[(\d+)\]", expression)
                if category == "indirect_aSyscall"
                else None
            )
            slot = int(slot_match.group(1)) if slot_match else None
            rows.append(
                {
                    "function": function,
                    "p_line": line_for(start),
                    "s_file": original_files.get(line_for(start)),
                    "s_line": span.get("begin", {}).get("presumedLine")
                    or original_lines.get(line_for(start)),
                    "s_function_line": s_line,
                    "callee": callee,
                    "target_kind": target_kind,
                    "category": category,
                    "syscall_slot": slot,
                    "syscall_name": syscall_names[slot]
                    if slot is not None and slot < len(syscall_names)
                    else None,
                    "expression": expression[:300],
                }
            )
    counts = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    result = {
        "status": "syntactic callsite candidates only; reachability and F0/G1 NOT PROVEN",
        "sqlite3_c_sha256": sqlite3_c_sha256,
        "preprocessed_sha256": preprocessed_sha256,
        "ast_sha256": hashlib.sha256(ast_raw).hexdigest(),
        "unix_function_start_s_line_filter": [40500, 48620],
        "observed_unix_call_s_line_range": [
            min(row["s_line"] for row in rows if row["s_line"] is not None),
            max(row["s_line"] for row in rows if row["s_line"] is not None),
        ],
        "function_count": len(functions),
        "call_count": len(rows),
        "upper_sqlite_os_call_count": len(upper_os_calls),
        "category_counts": counts,
        "syscall_table": syscall_names,
        "functions": functions,
        "calls": rows,
        "upper_sqlite_os_calls": upper_os_calls,
        "known_blind_spots": [
            "Direct and indirect call candidates are not a complete control-flow "
            "or reachable-effect proof.",
            "Unclassified direct calls and indirect dispatch candidates may have "
            "file effects; no category counts as a safe or closed path.",
            "Function-pointer dispatch, aSyscall slots, method-table aliases, "
            "and dynamic overrides require separate closure.",
            "Upstream sqlite3Os* call expressions are listed separately but are "
            "not connected to supported entrypoints or native callback branches.",
            "Non-SQLite owner/registry/output I/O is outside this SQLite source inventory.",
            "Each patched candidate and platform has a different source expansion "
            "and requires a separate inventory.",
        ],
    }
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "status",
                    "function_count",
                    "call_count",
                    "upper_sqlite_os_call_count",
                    "category_counts",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
