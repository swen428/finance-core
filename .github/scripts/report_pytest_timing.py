#!/usr/bin/env python3
"""Generate a machine-readable pytest timing report from JUnit XML.

This script is a CI observability tool.  It parses the JUnit XML produced by
``pytest --junitxml``, writes a compact ``pytest-timing-v1.json`` artifact, and
appends a human-readable summary to ``GITHUB_STEP_SUMMARY``.

Safety properties
-----------------
* Standard-library only – no third-party dependencies.
* Never modifies test outcomes, gate results, or the validate job conclusion.
* Rejects oversized, corrupt, or partial XML gracefully.
* Sanitises test names against HTML, control-character, and Markdown-table
  injection before writing them into the step summary.
* Does not emit tracebacks, captured output, or environment information.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_XML_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_TEST_ENTRIES = 50_000
MAX_FIELD_LENGTH = 500
SLOWEST_N = 25
SCHEMA_VERSION = "pytest-timing-v1"

# Reject XML that contains DTD or entity declarations (entity-expansion defence).
_DTD_ENTITY_RE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_name(raw: str) -> str:
    """Return a safe, display-friendly test identifier.

    * HTML-escapes ``& < > " '``.
    * Strips ASCII control characters.
    * Replaces ``|`` (Markdown table delimiter) with a full-width pipe.
    * Truncates to *MAX_FIELD_LENGTH* characters.
    """
    escaped = html.escape(raw, quote=True)
    cleaned = _CONTROL_CHARS_RE.sub("", escaped)
    cleaned = cleaned.replace("|", "\uff5c")
    return cleaned[:MAX_FIELD_LENGTH]


def safe_duration(value: str | None) -> float | None:
    """Parse a duration string, rejecting negative, NaN, and infinite values."""
    if value is None:
        return None
    try:
        duration = float(value)
    except (ValueError, OverflowError):
        return None
    if duration < 0 or not math.isfinite(duration):
        return None
    return round(duration, 6)


# ---------------------------------------------------------------------------
# JUnit XML parsing
# ---------------------------------------------------------------------------


def _classify_testcase(case: ET.Element) -> str:
    """Determine the outcome of a single ``<testcase>`` element."""
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "error"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def parse_junit_xml(xml_path: Path) -> tuple[list[dict[str, object]], bool, str | None]:
    """Parse JUnit XML into a list of test entries.

    Returns ``(entries, is_complete, error_message)``.
    """
    if not xml_path.exists():
        return [], False, "junit_xml_not_found"

    file_size = xml_path.stat().st_size
    if file_size > MAX_XML_BYTES:
        return [], False, "junit_xml_too_large"

    # Reject documents with DTD / entity declarations before parsing.
    with xml_path.open("rb") as fh:
        raw_head = fh.read(10_000)
    if _DTD_ENTITY_RE.search(raw_head):
        return [], False, "junit_xml_contains_dtd"

    try:
        tree = ET.parse(xml_path)  # noqa: S314
    except ET.ParseError:
        return [], False, "junit_xml_parse_error"

    root = tree.getroot()
    suites: list[ET.Element] = []
    if root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    elif root.tag == "testsuite":
        suites = [root]
    else:
        return [], False, "junit_xml_unexpected_root"

    entries: list[dict[str, object]] = []
    truncated = False
    for suite in suites:
        for case in suite.iter("testcase"):
            if len(entries) >= MAX_TEST_ENTRIES:
                truncated = True
                break
            classname = case.get("classname", "")
            name = case.get("name", "")
            test_id = f"{classname}::{name}" if classname else name
            duration = safe_duration(case.get("time"))
            entries.append(
                {
                    "id": sanitize_name(test_id)[:MAX_FIELD_LENGTH],
                    "outcome": _classify_testcase(case),
                    "duration_seconds": duration,
                }
            )
        if truncated:
            break

    is_complete = not truncated
    return entries, is_complete, None


# ---------------------------------------------------------------------------
# Report construction
# ---------------------------------------------------------------------------


def build_report(
    entries: list[dict[str, object]],
    is_complete: bool,
    error: str | None,
    pytest_exit_code: int,
) -> dict[str, object]:
    """Assemble the ``pytest-timing-v1`` JSON structure."""
    counts: dict[str, int] = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    total_duration = 0.0

    for entry in entries:
        outcome = str(entry["outcome"])
        if outcome in counts:
            counts[outcome] += 1
        dur = entry["duration_seconds"]
        if dur is not None:
            total_duration += float(dur)  # type: ignore[arg-type]

    # Deterministic sort: longest first, ties broken by test id (ascending).
    sorted_entries = sorted(
        entries,
        key=lambda e: (-float(e["duration_seconds"] or 0.0), str(e["id"])),  # type: ignore[arg-type]
    )

    complete = is_complete and error is None

    return {
        "schema": SCHEMA_VERSION,
        "complete": complete,
        "error": error,
        "pytest_exit_code": pytest_exit_code,
        "total_tests": len(entries),
        "counts": counts,
        "total_duration_seconds": round(total_duration, 3),
        "slowest": sorted_entries[:SLOWEST_N],
        "tests": sorted_entries,
    }


# ---------------------------------------------------------------------------
# Step summary (Markdown)
# ---------------------------------------------------------------------------


def _outcome_emoji(exit_code: int) -> str:
    return "\u2705" if exit_code == 0 else "\u274c"


def build_step_summary(report: dict[str, object]) -> str:
    """Render a Markdown summary for ``GITHUB_STEP_SUMMARY``."""
    lines: list[str] = ["## Pytest Timing Report", ""]

    error = report.get("error")
    if error:
        lines.append(f"> **Report status:** partial \u2014 `{error}`")
        lines.append("")
    elif not report.get("complete", False):
        lines.append("> **Report status:** partial \u2014 results may be incomplete")
        lines.append("")

    exit_code = int(report.get("pytest_exit_code", 1))  # type: ignore[call-overload]
    counts = report.get("counts", {})
    assert isinstance(counts, dict)
    total = report.get("total_tests", 0)
    total_dur = report.get("total_duration_seconds", 0)

    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Pytest result | {_outcome_emoji(exit_code)} exit code {exit_code} |")
    lines.append(f"| Total tests | {total} |")
    lines.append(f"| Passed | {counts.get('passed', 0)} |")
    lines.append(f"| Failed | {counts.get('failed', 0)} |")
    lines.append(f"| Skipped | {counts.get('skipped', 0)} |")
    lines.append(f"| Errors | {counts.get('error', 0)} |")
    lines.append(f"| Total duration | {total_dur:.1f}s |")
    lines.append("")

    slowest = report.get("slowest", [])
    assert isinstance(slowest, list)
    if slowest:
        lines.append(f"### Slowest {len(slowest)} tests")
        lines.append("")
        lines.append("| # | Test | Duration (s) | Result |")
        lines.append("|---|------|-------------|--------|")
        for i, entry in enumerate(slowest, 1):
            assert isinstance(entry, dict)
            tid = entry.get("id", "?")
            dur = entry.get("duration_seconds")
            dur_str = f"{dur:.3f}" if dur is not None else "n/a"
            outcome = entry.get("outcome", "?")
            lines.append(f"| {i} | {tid} | {dur_str} | {outcome} |")
        lines.append("")

    if not report.get("complete", False):
        lines.append(
            "> \u26a0\ufe0f This report is partial. Some test results may be missing or inaccurate."
        )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate pytest timing report from JUnit XML.")
    parser.add_argument("--xml", required=True, help="Path to JUnit XML file")
    parser.add_argument("--output", required=True, help="Path for output JSON report")
    parser.add_argument(
        "--exit-code",
        type=int,
        default=1,
        help="Pytest process exit code",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="Path to GITHUB_STEP_SUMMARY file (appends Markdown)",
    )
    args = parser.parse_args(argv)

    xml_path = Path(args.xml)
    output_path = Path(args.output)

    entries, is_complete, error = parse_junit_xml(xml_path)
    report = build_report(entries, is_complete, error, args.exit_code)

    # Write JSON artifact.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Write step summary.
    summary_text = build_step_summary(report)
    if args.summary:
        summary_path = Path(args.summary)
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(summary_text + "\n")
    else:
        print(summary_text)  # noqa: T201

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
