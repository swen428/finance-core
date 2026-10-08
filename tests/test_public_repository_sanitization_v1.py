from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_OWNER_COMMENT_FILES = {
    "finance_core/resources/migrations/001_create_core_schema.sql",
    "finance_core/resources/migrations/006_reconciliation_persistence_schema_v1.sql",
    "finance_core/resources/migrations/007_reconciliation_review_resolution_persistence.sql",
    "finance_core/resources/migrations/008_reconciliation_apply_results.sql",
    "finance_core/resources/migrations/011_reconciliation_structured_evidence.sql",
    "finance_core/resources/migrations/012_reconciliation_apply_state_persistence.sql",
    "finance_core/resources/migrations/013_reconciliation_final_mutation_guard_decisions.sql",
    "finance_core/resources/migrations/014_reconciliation_guarded_apply_execution_persistence.sql",
    "finance_core/resources/migrations/015_calculation_run_persistence.sql",
    "finance_core/resources/migrations/016_calculation_snapshot_persistence.sql",
    "finance_core/resources/migrations/017_pdf_statement_import_run_persistence.sql",
    "finance_core/resources/migrations/020_receipt_finalization_hardening.sql",
}
NEGATIVE_SECRET_FIXTURE_FILES = {
    "tests/test_ai_fallback_service_v1.py",
    "tests/test_ai_model_compatibility_receipts_v2.py",
}
BINARY_FIXTURE_SHA256 = {
    "tests/fixtures/linux_receipt_ocr/synthetic_mixed_receipt.png": (
        "3d0f2b20ce2713babc596e27cb2d484704a717aa9ecb4df9a5773d3e557dd0db"
    ),
    "tests/fixtures/linux_receipt_ocr/synthetic_mixed_receipt.jpg": (
        "d7d1e62acc006b67d6a44e4c55459348a3fae5d25bb0dd94f1d99d57bef728bb"
    ),
    "tests/fixtures/linux_receipt_ocr/synthetic_incomplete_receipt.png": (
        "69f6d080d7039cecbd179d339be4783cb284febdbe50f3c80c5aac8e76fc6634"
    ),
    "tests/fixtures/receipt_ocr/synthetic_receipt_001.png": (
        "25336b8073d8862eae8f2dc699fea02e4240ce4bcb9b38b914e9450da3b04a15"
    ),
    "tests/fixtures/receipt_media_v1/vectors.zip": (
        "48d7df0d026c112f8703b4d4e345553cf6569a06275e0dd29ff3010f208c5ca7"
    ),
    "tests/fixtures/reconciliation/pdf_statement_temp_db/encrypted_bank_statement.pdf": (
        "fc1627306d80d0f0b3f41c1ec52d60f96d57cef86c089ac463bfa80b3365b1e5"
    ),
    "tests/fixtures/reconciliation/pdf_statement_temp_db/multi_page_bank_statement.pdf": (
        "710014f86dc68fe8469a0f1c8edb78ef405e9f7b60e5010ae2f33011b927e828"
    ),
    "tests/fixtures/reconciliation/pdf_statement_temp_db/near_text_boundary_statement.pdf": (
        "26d4a13be06272cba343972c2cf3829742ee1b9008ece8f19e76d342753a5bf5"
    ),
    "tests/fixtures/reconciliation/pdf_statement_temp_db/sample_bank_statement.pdf": (
        "ee0eff10fc9e5760c5fd30dddd8d5d5a752647c3697a9914900faa1cd794a556"
    ),
}
_APPROVED_FORMAT_TEXT = {
    "docs/development/receipt_media_v1.md": ("raw TI" + "FF",),
    "finance_core/intake/_receipt_media_worker.py": (
        "raw TI" + "FF",
        "ti" + "ff:Orientation",
    ),
    "tests/test_receipt_media_v1.py": ("ti" + "ff:Orientation",),
}
_FORMAT_WORKER_PATH = "finance_core/intake/_receipt_media_worker.py"


def _mask_python_format_name(source: str) -> str:
    lines = source.splitlines(keepends=True)
    marker_name = "ti" + "ff"
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.NAME or token.string != marker_name:
            continue
        line_number, start_column = token.start
        end_line_number, end_column = token.end
        if line_number != end_line_number:
            continue
        line = lines[line_number - 1]
        lines[line_number - 1] = (
            line[:start_column] + " " * (end_column - start_column) + line[end_column:]
        )
    return "".join(lines)


def _mask_approved_format_text(relative_path: str, source: str) -> str:
    for spelling in _APPROVED_FORMAT_TEXT.get(relative_path, ()):
        source = source.replace(spelling, " " * len(spelling))
    if relative_path == _FORMAT_WORKER_PATH:
        source = _mask_python_format_name(source)
    return source


def _forbidden_marker_matches(
    relative_path: str, source: str, markers: tuple[str, ...]
) -> list[str]:
    sanitized = _mask_approved_format_text(relative_path, source).lower()
    return [marker for marker in markers if marker.lower() in sanitized]


def _tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPOSITORY_ROOT / path for path in completed.stdout.decode().split("\0") if path]


def _tracked_modes() -> dict[str, str]:
    completed = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    entries: dict[str, str] = {}
    for raw_entry in completed.stdout.decode().split("\0"):
        if not raw_entry:
            continue
        metadata, path = raw_entry.split("\t", maxsplit=1)
        mode = metadata.split(" ", maxsplit=1)[0]
        entries[path] = mode
    return entries


def _text_files() -> list[Path]:
    values: list[Path] = []
    for path in _tracked_files():
        payload = path.read_bytes()
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
            observed_sha256 = hashlib.sha256(payload).hexdigest()
            assert BINARY_FIXTURE_SHA256.get(relative_path) == observed_sha256, (
                f"unapproved or changed binary file: {relative_path}"
            )
            continue
        values.append(path)
    return values


def test_tracked_entries_are_regular_files_with_approved_modes() -> None:
    unexpected = {
        path: mode for path, mode in _tracked_modes().items() if mode not in {"100644", "100755"}
    }
    assert unexpected == {}


def test_no_private_identity_or_local_path_markers() -> None:
    forbidden = (
        "swen" + "428",
        "finance-" + "automation",
        "/Users/" + "s." + "w" + "en",
        "Jo" + "anne",
        "Ch" + "loe",
        "Ti" + "ff",
        "Gle" + "nna",
        "Ce" + "lia",
        "Chicken " + "Run",
        "Play" + "Made",
    )
    findings: list[str] = []
    for path in _text_files():
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        for marker in _forbidden_marker_matches(relative_path, source, forbidden):
            findings.append(f"{relative_path}:{marker}")
    assert findings == []


def test_technical_format_mask_is_exact_and_path_bound() -> None:
    raw_format = "raw TI" + "FF"
    orientation_prefix = "ti" + "ff:Orientation"
    format_name = "ti" + "ff"
    approved_paths = (
        "docs/development/receipt_media_v1.md",
        _FORMAT_WORKER_PATH,
        "tests/test_receipt_media_v1.py",
    )

    assert _forbidden_marker_matches(approved_paths[0], raw_format, ("Ti" + "ff",)) == []
    assert (
        _forbidden_marker_matches(
            _FORMAT_WORKER_PATH,
            raw_format + " " + orientation_prefix + "\n" + format_name + " = 1",
            ("Ti" + "ff",),
        )
        == []
    )
    assert _forbidden_marker_matches(approved_paths[2], orientation_prefix, ("Ti" + "ff",)) == []

    worker_source = (
        format_name + " = 1\nlabel = '" + format_name + "'\n# " + format_name + " note\n"
    )
    masked_worker = _mask_approved_format_text(_FORMAT_WORKER_PATH, worker_source)
    assert masked_worker.splitlines()[0].strip() == "= 1"
    assert masked_worker.splitlines()[1] == worker_source.splitlines()[1]
    assert masked_worker.splitlines()[2] == worker_source.splitlines()[2]

    negative_cases = (
        (approved_paths[0], "Ti" + "ff"),
        (_FORMAT_WORKER_PATH, "value = '" + format_name + "'"),
        (_FORMAT_WORKER_PATH, "# " + format_name),
        (approved_paths[2], "ordinary '" + format_name + "' label"),
        ("tests/another_file.py", raw_format),
        ("tests/another_file.py", orientation_prefix),
    )
    for relative_path, source in negative_cases:
        assert _forbidden_marker_matches(relative_path, source, ("Ti" + "ff",))


def test_owner_name_occurs_only_in_immutable_migration_comments() -> None:
    owner_name = "W" + "en"
    pattern = re.compile(rf"\b{owner_name}\b", re.IGNORECASE)
    findings: list[str] = []
    for path in _text_files():
        if pattern.search(path.read_text(encoding="utf-8")):
            findings.append(path.relative_to(REPOSITORY_ROOT).as_posix())
    assert set(findings) == IMMUTABLE_OWNER_COMMENT_FILES
    assert len(findings) == len(IMMUTABLE_OWNER_COMMENT_FILES)


def test_secret_shaped_literals_are_confined_to_negative_tests() -> None:
    secret_patterns = (
        re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
        re.compile("BEGIN " + "PRIVATE KEY"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    )
    findings: set[str] = set()
    for path in _text_files():
        source = path.read_text(encoding="utf-8")
        if any(pattern.search(source) for pattern in secret_patterns):
            findings.add(path.relative_to(REPOSITORY_ROOT).as_posix())
    assert findings == NEGATIVE_SECRET_FIXTURE_FILES


def test_no_runtime_data_or_credential_files_are_tracked() -> None:
    prohibited_names = {".env", "finance.db"}
    prohibited_suffixes = {".db", ".key", ".pem", ".sqlite", ".sqlite3"}
    findings = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in _tracked_files()
        if path.name in prohibited_names or path.suffix.lower() in prohibited_suffixes
    ]
    assert findings == []


@pytest.mark.parametrize("identity", ["approved", "changed", "unknown"])
def test_binary_allowlist_keeps_exact_path_and_hash_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    payload = b"\xffsynthetic-binary"
    path = tmp_path / "synthetic.bin"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    expected = {"synthetic.bin": digest}
    if identity == "changed":
        expected["synthetic.bin"] = "0" * 64
    elif identity == "unknown":
        expected = {"another.bin": digest}
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "BINARY_FIXTURE_SHA256", expected)
    monkeypatch.setattr(module, "_tracked_files", lambda: [path])
    if identity == "approved":
        assert _text_files() == []
    else:
        with pytest.raises(AssertionError, match="unapproved or changed binary file"):
            _text_files()
