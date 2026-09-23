"""Strict, versioned correction approval wire format.

The application owns the business objects; this module owns only local HMAC
proof bytes.  Decoding is intentionally stricter than the older snapshot JSON
reader because duplicate keys and noncanonical text must never enter a proof.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from finance_core.calculation.authoritative_snapshot import canonical_json_bytes

MAX_WIRE_BYTES = 65_536
MAX_EPOCH = 253402300799
DECISION_KEYS = frozenset(
    {
        "schema",
        "authority_id",
        "key_id",
        "realm",
        "instance_id",
        "actor",
        "target_id",
        "expected_version",
        "predecessor_hash",
        "plan_id",
        "plan_hash",
        "source_hash",
        "before_hash",
        "after_hash",
        "fact_hash",
        "snapshot_id",
        "snapshot_hash",
        "reason",
        "renderer",
        "display_sha256",
        "challenge",
        "issued_at_epoch",
        "expires_at_epoch",
        "nonce",
    }
)
CONSUMPTION_KEYS = frozenset(
    {
        "schema",
        "key_id",
        "realm",
        "actor",
        "target_id",
        "instance_id",
        "authority_id",
        "plan_id",
        "correction_id",
        "nonce",
        "decision_digest",
        "result_core_hash",
        "checked_at_epoch",
    }
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class CorrectionWireError(ValueError):
    """Malformed or unauthenticated local correction proof."""


def framed(tag: str, value: bytes) -> bytes:
    if not tag.isascii() or not tag or "\x00" in tag:
        raise CorrectionWireError("invalid proof domain")
    if len(value) >= 1 << 64:
        raise CorrectionWireError("proof material is too large")
    return tag.encode("ascii") + b"\x00" + len(value).to_bytes(8, "big") + value


def key_id(key: bytes) -> str:
    if type(key) is not bytes or len(key) != 32:
        raise CorrectionWireError("correction key must contain exactly 32 bytes")
    return hashlib.sha256(framed("finance-correction-key-id-v1", key)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise CorrectionWireError("duplicate JSON key")
        result[name] = value
    return result


def _reject_constant(_value: str) -> None:
    raise CorrectionWireError("non-finite JSON number")


def strict_object(raw: str | bytes, keys: frozenset[str]) -> dict[str, object]:
    try:
        data = raw.encode("utf-8") if type(raw) is str else raw
        if type(data) is not bytes or len(data) > MAX_WIRE_BYTES or not data:
            raise CorrectionWireError("proof exceeds wire limit or is empty")
        parsed = json.loads(data, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if type(parsed) is not dict or set(parsed) != {"contract_version", "value"}:
            raise CorrectionWireError("invalid canonical wrapper")
        if parsed["contract_version"] != "finance-canonical-json-v1":
            raise CorrectionWireError("invalid canonical contract version")
        value = parsed["value"]
        if type(value) is not dict or set(value) != keys:
            raise CorrectionWireError("invalid proof fields")
        for name, item in value.items():
            if type(name) is not str or unicodedata.normalize("NFC", name) != name:
                raise CorrectionWireError("noncanonical proof key")
            if item is not None and type(item) not in {str, int}:
                raise CorrectionWireError("invalid proof field type")
            if type(item) is str and unicodedata.normalize("NFC", item) != item:
                raise CorrectionWireError("noncanonical proof text")
        if canonical_json_bytes(value) != data:
            raise CorrectionWireError("noncanonical proof encoding")
        if keys == DECISION_KEYS:
            _validate_decision(value)
        elif keys == CONSUMPTION_KEYS:
            _validate_consumption(value)
        return value
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, CorrectionWireError):
            raise
        raise CorrectionWireError("invalid proof JSON") from exc


def require_hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise CorrectionWireError(f"invalid {label}")
    return value


def require_epoch(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_EPOCH:
        raise CorrectionWireError(f"invalid {label}")
    return value


def _validate_decision(value: Mapping[str, object]) -> None:
    if value["schema"] != "correction-decision-v1" or value["renderer"] != "correction-terminal-v1":
        raise CorrectionWireError("invalid decision protocol")
    for name in DECISION_KEYS - {
        "schema",
        "renderer",
        "expected_version",
        "issued_at_epoch",
        "expires_at_epoch",
        "fact_hash",
        "snapshot_id",
        "snapshot_hash",
    }:
        if type(value[name]) is not str or not value[name]:
            raise CorrectionWireError(f"invalid {name} type")
    if type(value["expected_version"]) is not int or value["expected_version"] < 0:
        raise CorrectionWireError("invalid expected_version type")
    require_epoch(value["issued_at_epoch"], "issued_at_epoch")
    require_epoch(value["expires_at_epoch"], "expires_at_epoch")
    for name in (
        "key_id",
        "predecessor_hash",
        "plan_hash",
        "source_hash",
        "before_hash",
        "after_hash",
        "display_sha256",
        "challenge",
        "nonce",
    ):
        require_hash(value[name], name)
    optional = (value["fact_hash"], value["snapshot_id"], value["snapshot_hash"])
    if any(item is None for item in optional) and any(item is not None for item in optional):
        raise CorrectionWireError("partial receipt decision binding")
    if optional[0] is not None:
        require_hash(optional[0], "fact_hash")
        require_hash(optional[2], "snapshot_hash")
        if type(optional[1]) is not str or not optional[1]:
            raise CorrectionWireError("invalid snapshot_id type")


def _validate_consumption(value: Mapping[str, object]) -> None:
    if value["schema"] != "correction-consumption-v1":
        raise CorrectionWireError("invalid consumption protocol")
    for name in CONSUMPTION_KEYS - {"schema", "checked_at_epoch"}:
        if type(value[name]) is not str or not value[name]:
            raise CorrectionWireError(f"invalid {name} type")
    require_epoch(value["checked_at_epoch"], "checked_at_epoch")
    for name in ("key_id", "nonce", "decision_digest", "result_core_hash"):
        require_hash(value[name], name)


def _signature(key: bytes, tag: str, material: Mapping[str, object]) -> str:
    key_id(key)
    return hmac.new(key, framed(tag, canonical_json_bytes(material)), hashlib.sha256).hexdigest()


def decision_signature(key: bytes, envelope: Mapping[str, object]) -> str:
    if set(envelope) != DECISION_KEYS:
        raise CorrectionWireError("invalid decision fields")
    _validate_decision(envelope)
    return _signature(key, "finance-correction-decision-v1", envelope)


def decision_digest(envelope: Mapping[str, object], signature: str) -> str:
    require_hash(signature, "decision signature")
    payload = {"envelope": dict(envelope), "signature": signature}
    return hashlib.sha256(
        framed("finance-correction-decision-digest-v1", canonical_json_bytes(payload))
    ).hexdigest()


def consumption_seal(key: bytes, material: Mapping[str, object]) -> str:
    if set(material) != CONSUMPTION_KEYS:
        raise CorrectionWireError("invalid consumption fields")
    _validate_consumption(material)
    return _signature(key, "finance-correction-consumption-v1", material)


def authentic_decision(key: bytes, raw: str, signature: str) -> dict[str, object]:
    value = strict_object(raw, DECISION_KEYS)
    require_hash(signature, "decision signature")
    if not hmac.compare_digest(decision_signature(key, value), signature):
        raise CorrectionWireError("decision signature mismatch")
    return value


def authentic_consumption(key: bytes, raw: str, seal: str) -> dict[str, object]:
    value = strict_object(raw, CONSUMPTION_KEYS)
    require_hash(seal, "consumption seal")
    if not hmac.compare_digest(consumption_seal(key, value), seal):
        raise CorrectionWireError("consumption seal mismatch")
    return value
