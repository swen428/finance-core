"""Bounded tests for the explicit fixed age-tool qualification helper."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "age_toolchain_qualification", ROOT / ".github/scripts/qualify_age_toolchain_v1.py"
)
assert SPEC and SPEC.loader
QUALIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALIFIER)


def _small_archive(path: Path, *, attack: str | None = None) -> None:
    bodies = {
        "age/age-keygen": b"keygen",
        "age/age-plugin-tag": b"a",
        "age/age-plugin-batchpass": b"b",
        "age/age-plugin-pq": b"c",
        "age/age-plugin-tagpq": b"d",
        "age/age": b"age",
        "age/LICENSE": b"license",
        "age/age-inspect": b"inspect",
    }
    members = {"age": ("directory", b"")}
    members.update({name: ("file", body) for name, body in bodies.items()})
    if attack == "unexpected":
        members["age/extra"] = ("file", b"extra")
        members.pop("age/age-inspect")
    if attack == "symlink":
        members["age/age-inspect"] = ("symlink", b"age")
    if attack == "traversal":
        members.pop("age/age-inspect")
        members["../escape"] = ("file", b"escape")
    with tarfile.open(path, "w:gz") as archive:
        for name, (kind, body) in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(body)
            if kind == "directory":
                item.type = tarfile.DIRTYPE
            elif kind == "symlink":
                item.type = tarfile.SYMTYPE
                item.linkname = body.decode()
            archive.addfile(item, io.BytesIO(body) if kind == "file" else None)


def _fake_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        QUALIFIER.LINUX,
        "tools",
        {
            "age": {"sha256": hashlib.sha256(b"age").hexdigest(), "size_bytes": 3},
            "age_keygen": {
                "sha256": hashlib.sha256(b"keygen").hexdigest(),
                "size_bytes": 6,
            },
        },
    )
    monkeypatch.setattr(
        QUALIFIER,
        "LINUX_MEMBERS",
        {
            "age": ("directory", 0),
            "age/age-keygen": ("file", 6),
            "age/age-plugin-tag": ("file", 1),
            "age/age-plugin-batchpass": ("file", 1),
            "age/age-plugin-pq": ("file", 1),
            "age/age-plugin-tagpq": ("file", 1),
            "age/age": ("file", 3),
            "age/LICENSE": ("file", 7),
            "age/age-inspect": ("file", 7),
        },
    )


def test_fixed_registry_exactly_matches_the_qualification_pins() -> None:
    registry = QUALIFIER.check_registry(ROOT)
    assert registry == QUALIFIER.expected_registry()
    assert registry["source_commit"] == QUALIFIER.SOURCE_COMMIT
    assert set(registry["platforms"]) == {"darwin-arm64", "linux-x86_64"}


def test_safe_extraction_reads_only_exact_pinned_tool_members(tmp_path, monkeypatch) -> None:
    _fake_pins(monkeypatch)
    archive = tmp_path / "age.tar.gz"
    _small_archive(archive)
    output = QUALIFIER.extract_tools(archive, tmp_path / "tools")
    assert set(output) == {"age", "age_keygen"}
    assert (tmp_path / "tools/age").read_bytes() == b"age"
    assert (tmp_path / "tools/age-keygen").read_bytes() == b"keygen"
    assert (tmp_path / "tools/age").stat().st_mode & 0o777 == 0o500


@pytest.mark.parametrize("attack", ["unexpected", "symlink", "traversal"])
def test_extraction_refuses_unlisted_or_unsafe_members(tmp_path, monkeypatch, attack) -> None:
    _fake_pins(monkeypatch)
    archive = tmp_path / "age.tar.gz"
    _small_archive(archive, attack=attack)
    with pytest.raises((ValueError, tarfile.TarError)):
        QUALIFIER.extract_tools(archive, tmp_path / "tools")


def test_registry_mismatch_fails_closed(tmp_path) -> None:
    resource = tmp_path / "finance_core/resources/crypto/age_toolchain_v1.json"
    resource.parent.mkdir(parents=True)
    value = json.loads((ROOT / "finance_core/resources/crypto/age_toolchain_v1.json").read_bytes())
    value["release"] = "latest"
    resource.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="registry"):
        QUALIFIER.check_registry(tmp_path)
