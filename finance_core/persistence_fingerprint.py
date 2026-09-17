"""Canonical, versioned SHA-256 fingerprints for durable write contracts."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from finance_core.money import canonical_decimal_str


def canonical_fingerprint(*, schema_version: str, material: dict[str, Any]) -> str:
    """Return a full SHA-256 hex digest over explicitly versioned canonical JSON.

    Callers must pass immutable, material fields only.  This deliberately
    rejects float values so Decimal precision cannot be silently lost.
    """
    if not schema_version.strip():
        raise ValueError("fingerprint schema_version is required")
    payload = _normalize({"fingerprint_schema_version": schema_version, "material": material})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize(value: Any) -> Any:
    """Return only canonical JSON-safe values; reject lossy coercions."""
    if isinstance(value, Decimal):
        return canonical_decimal_str(value)
    if isinstance(value, float):
        raise TypeError("float values are not permitted in canonical fingerprints")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical fingerprint dictionary keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise TypeError(
        f"Object of type {type(value).__name__} is not permitted in canonical fingerprints"
    )
