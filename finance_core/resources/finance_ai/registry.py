"""Load and verify the immutable S5e AI policy asset registry.

The registry is intentionally filesystem-only. It never reads credentials,
environment overrides, a live database, or a provider response.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_EXPECTED_ASSET_KINDS = {
    "runtime_policy": "json",
    "prompt": "text",
    "intent_policy": "json",
    "default_policy": "json",
    "deadline_policy": "json",
    "sqlite_money_policy": "json",
    "sensitive_text_policy": "json",
}
_EXPECTED_ASSETS = frozenset(_EXPECTED_ASSET_KINDS)
_ENTRY_FIELDS = frozenset({"name", "kind", "path", "version", "sha256", "byte_count"})
_ROOT_FIELDS = frozenset({"schema_version", "registry_version", "assets"})


class FinanceAiPolicyError(ValueError):
    """Raised when an S5e policy asset or registry is not canonical."""


@dataclass(frozen=True)
class FinanceAiAsset:
    name: str
    kind: str
    path: Path
    version: str
    sha256: str
    byte_count: int

    def read_bytes(self) -> bytes:
        data = self.path.read_bytes()
        if len(data) != self.byte_count:
            raise FinanceAiPolicyError(f"Asset byte count changed: {self.name}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != self.sha256:
            raise FinanceAiPolicyError(f"Asset hash changed: {self.name}")
        return data

    def read_json(self) -> dict[str, Any]:
        if self.kind != "json":
            raise FinanceAiPolicyError(f"Asset is not JSON: {self.name}")
        try:
            value = json.loads(self.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinanceAiPolicyError(f"Asset is not valid JSON: {self.name}") from exc
        if not isinstance(value, dict):
            raise FinanceAiPolicyError(f"JSON asset must be an object: {self.name}")
        return value


@dataclass(frozen=True)
class FinanceAiAssetRegistry:
    root: Path
    registry_version: str
    assets: dict[str, FinanceAiAsset]

    def asset(self, name: str) -> FinanceAiAsset:
        try:
            return self.assets[name]
        except KeyError as exc:
            raise FinanceAiPolicyError(f"Unknown Finance AI asset: {name}") from exc

    def json_asset(self, name: str) -> dict[str, Any]:
        return self.asset(name).read_json()

    def prompt_text(self) -> str:
        prompt = self.asset("prompt")
        if prompt.kind != "text":
            raise FinanceAiPolicyError("Prompt asset kind is invalid")
        text = prompt.read_bytes().decode("utf-8")
        if not text or "\x00" in text:
            raise FinanceAiPolicyError("Prompt asset is empty or contains NUL")
        return text


def load_asset_registry(root: Path | None = None) -> FinanceAiAssetRegistry:
    resolved_root = (root or Path(__file__).resolve().parent).resolve()
    registry_path = resolved_root / "asset_registry.json"
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinanceAiPolicyError("Finance AI asset registry cannot be read") from exc
    if (
        not isinstance(raw, dict)
        or frozenset(raw) != _ROOT_FIELDS
        or raw.get("schema_version") != "finance-ai-asset-registry-v1"
    ):
        raise FinanceAiPolicyError("Finance AI asset registry schema is invalid")
    registry_version = raw.get("registry_version")
    entries = raw.get("assets")
    if not isinstance(registry_version, str) or not registry_version:
        raise FinanceAiPolicyError("Finance AI asset registry version is invalid")
    if not isinstance(entries, list) or len(entries) != len(_EXPECTED_ASSETS):
        raise FinanceAiPolicyError("Finance AI asset registry asset list is invalid")

    assets: dict[str, FinanceAiAsset] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise FinanceAiPolicyError("Finance AI asset registry entry is invalid")
        if frozenset(entry) != _ENTRY_FIELDS:
            raise FinanceAiPolicyError("Finance AI asset registry entry fields are invalid")
        name = entry.get("name")
        kind = entry.get("kind")
        relative_path = entry.get("path")
        version = entry.get("version")
        expected_hash = entry.get("sha256")
        expected_byte_count = entry.get("byte_count")
        if (
            not isinstance(name, str)
            or _SAFE_NAME_RE.fullmatch(name) is None
            or name in assets
            or name not in _EXPECTED_ASSETS
            or kind != _EXPECTED_ASSET_KINDS.get(name)
            or not isinstance(relative_path, str)
            or Path(relative_path).is_absolute()
            or Path(relative_path).name != relative_path
            or not isinstance(version, str)
            or not version
            or not isinstance(expected_hash, str)
            or _SHA256_RE.fullmatch(expected_hash) is None
            or isinstance(expected_byte_count, bool)
            or not isinstance(expected_byte_count, int)
            or not 1 <= expected_byte_count <= 1_048_576
        ):
            raise FinanceAiPolicyError("Finance AI asset registry entry is non-canonical")
        path = resolved_root / relative_path
        if not path.is_file():
            raise FinanceAiPolicyError(f"Finance AI asset is missing: {relative_path}")
        data = path.read_bytes()
        if len(data) != expected_byte_count:
            raise FinanceAiPolicyError(f"Finance AI asset byte count mismatch: {name}")
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            raise FinanceAiPolicyError(f"Finance AI asset hash mismatch: {name}")
        if kind == "json":
            try:
                internal = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FinanceAiPolicyError(f"Asset is not valid JSON: {name}") from exc
            if not isinstance(internal, dict) or internal.get("version") != version:
                raise FinanceAiPolicyError(f"Finance AI asset internal version mismatch: {name}")
        assert isinstance(kind, str)
        assets[name] = FinanceAiAsset(
            name=name,
            kind=kind,
            path=path,
            version=version,
            sha256=actual_hash,
            byte_count=expected_byte_count,
        )
    if frozenset(assets) != _EXPECTED_ASSETS:
        raise FinanceAiPolicyError("Finance AI asset registry has an incomplete asset set")
    return FinanceAiAssetRegistry(
        root=resolved_root,
        registry_version=registry_version,
        assets=assets,
    )


__all__ = [
    "FinanceAiAsset",
    "FinanceAiAssetRegistry",
    "FinanceAiPolicyError",
    "load_asset_registry",
]
