"""Strict, bounded synthetic authority framing. No production credential lives here."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

ID = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ZERO = "0" * 64
MAX_INT = (1 << 63) - 1
PURPOSE = "synthetic-component-only"
SPECS = {
    "descriptor": (b"FNA1CAP\n", 16384, b"finance-core/synthetic-authority/v1/capability\0"),
    "registry": (b"FNA1REG\n", 4096, b"finance-core/synthetic-authority/v1/registry\0"),
    "head": (b"FNA1HED\n", 2048, b"finance-core/synthetic-authority/v1/head\0"),
    "receipt": (b"FNA1RCP\n", 2048, b"finance-core/synthetic-authority/v1/receipt\0"),
    "test-cut": (b"FNA1TST\n", 4096, b"finance-core/synthetic-authority/v1/test-cut-certificate\0"),
}
OBJECTS = {
    "main": ("root", "main.witness", ("witness",)),
    "gate": ("root", "profile-gate.lock", ("lock", "witness")),
    "journal0": ("slots", "journal-0.slot", ("barrier", "read", "reset", "write")),
    "journal1": ("slots", "journal-1.slot", ("barrier", "read", "reset", "write")),
    "wal0": ("slots", "wal-0.slot", ("barrier", "read", "reset", "write")),
    "wal1": ("slots", "wal-1.slot", ("barrier", "read", "reset", "write")),
    "registry": ("authority", "registry.log", ("append", "barrier", "read")),
    "head": ("authority", "committed-head.log", ("append", "barrier", "read")),
    "receipt": ("authority", "enrollment.receipt", ("barrier", "enroll-write", "read")),
    "lifecycle": ("authority", "core-lifecycle.lock", ("lock", "witness")),
}
DEFAULT_LIMITS = {
    "registry_record_cap": 256,
    "head_record_cap": 256,
    "registry_byte_cap": 1048576,
    "head_byte_cap": 524288,
    "generation_cap": 63,
    "token_cap": 16,
    "slot_byte_cap": 65536,
    "operation_byte_cap": 4096,
}


class FormatError(ValueError):
    """A frame is noncanonical, unbounded, or inconsistent."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise FormatError("duplicate key")
        result[key] = value
    return result


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FormatError("noncanonical value") from exc


def parse_canonical(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("ascii"),
            object_pairs_hook=_pairs,
            parse_float=lambda _: (_ for _ in ()).throw(FormatError("float")),
            parse_constant=lambda _: (_ for _ in ()).throw(FormatError("constant")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormatError("invalid JSON") from exc
    if not isinstance(value, dict) or canonical(value) != data:
        raise FormatError("noncanonical JSON")
    return value


def frame_digest(frame: bytes) -> str:
    return hashlib.sha256(frame).hexdigest()


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(state)).hexdigest()


def encode_frame(kind: str, payload: dict[str, Any], key: bytes) -> bytes:
    if len(key) != 32:
        raise FormatError("test MAC key must be 32 bytes")
    validate_payload(kind, payload)
    magic, size, domain = SPECS[kind]
    data = canonical(payload)
    if not 2 <= len(data) <= size - 44:
        raise FormatError("payload size")
    body = magic + len(data).to_bytes(4, "big") + data
    body += bytes(size - 32 - len(body))
    return body + hmac.digest(key, domain + body, "sha256")


def decode_frame(kind: str, frame: bytes, key: bytes) -> dict[str, Any]:
    if len(key) != 32:
        raise FormatError("test MAC key must be 32 bytes")
    magic, size, domain = SPECS[kind]
    if len(frame) != size or frame[:8] != magic:
        raise FormatError("frame size or magic")
    length = int.from_bytes(frame[8:12], "big")
    if not 2 <= length <= size - 44 or any(frame[12 + length : -32]):
        raise FormatError("length or padding")
    if not hmac.compare_digest(frame[-32:], hmac.digest(key, domain + frame[:-32], "sha256")):
        raise FormatError("MAC")
    payload = parse_canonical(frame[12 : 12 + length])
    validate_payload(kind, payload)
    return payload


def _keys(value: Any, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise FormatError("field set")
    return value


def _id(value: Any) -> None:
    if type(value) is not str or ID.fullmatch(value) is None:
        raise FormatError("ID")


def _digest(value: Any) -> None:
    if type(value) is not str or DIGEST.fullmatch(value) is None:
        raise FormatError("digest")


def _int(value: Any, maximum: int = MAX_INT, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FormatError("integer")


def validate_payload(kind: str, p: dict[str, Any]) -> None:
    if kind == "descriptor":
        _keys(
            p,
            {
                "v",
                "purpose",
                "installation_id",
                "profile_id",
                "instance_id",
                "issuer_epoch",
                "registry_id",
                "key_id",
                "enrollment_id",
                "enrollment_kind",
                "directories",
                "objects",
                "limits",
                "barrier_profile",
            },
        )
        _int(p["v"], 1, 1)
        if p["purpose"] != PURPOSE or p["enrollment_kind"] not in (
            "fresh",
            "verified-restore-test",
        ):
            raise FormatError("descriptor purpose")
        for field in (
            "installation_id",
            "profile_id",
            "instance_id",
            "issuer_epoch",
            "registry_id",
            "key_id",
            "enrollment_id",
        ):
            _id(p[field])
        if p["barrier_profile"] not in ("process-crash-fsync-v1", "injected-storage-model-v1"):
            raise FormatError("barrier profile")
        directories = _keys(p["directories"], {"root", "authority", "slots"})
        for witness in directories.values():
            _keys(witness, {"dev", "ino", "uid", "mode", "acl_policy"})
            for field in ("dev", "ino", "uid"):
                _int(witness[field])
            if (
                type(witness["mode"]) is not int
                or witness["mode"] != 448
                or witness["acl_policy"] != "no-grants-v1"
            ):
                raise FormatError("directory policy")
        objects = _keys(p["objects"], set(OBJECTS))
        for role, witness in objects.items():
            _keys(
                witness,
                {
                    "directory",
                    "basename",
                    "dev",
                    "ino",
                    "uid",
                    "mode",
                    "nlink",
                    "type",
                    "acl_policy",
                    "operations",
                },
            )
            directory, basename, operations = OBJECTS[role]
            if (witness["directory"], witness["basename"], witness["operations"]) != (
                directory,
                basename,
                list(operations),
            ):
                raise FormatError("object role")
            for field in ("dev", "ino", "uid"):
                _int(witness[field])
            if (
                type(witness["mode"]) is not int
                or witness["mode"] != 384
                or type(witness["nlink"]) is not int
                or witness["nlink"] != 1
                or witness["type"] != "regular"
                or witness["acl_policy"] != "no-grants-v1"
            ):
                raise FormatError("object policy")
        limits = _keys(p["limits"], set(DEFAULT_LIMITS))
        for field, maximum in DEFAULT_LIMITS.items():
            _int(limits[field], maximum, 1)
    elif kind == "registry":
        _keys(
            p,
            {
                "v",
                "profile_id",
                "instance_id",
                "issuer_epoch",
                "registry_id",
                "c_digest",
                "seq",
                "op_id",
                "event_id",
                "prev_record_digest",
                "event",
                "kind",
                "generation",
                "slot",
                "prior_state_digest",
                "result_state_digest",
            },
        )
        _common(p)
        for field in ("op_id", "event_id"):
            _id(p[field])
        for field in ("prev_record_digest", "prior_state_digest", "result_state_digest"):
            _digest(p[field])
        if p["event"] not in ("GENESIS", "RESET_INTENT", "RESET_DONE", "ACTIVE", "RETIRED"):
            raise FormatError("event")
        if p["event"] == "GENESIS":
            if p["kind"] is not None or p["slot"] is not None or p["generation"] != 0:
                raise FormatError("genesis fields")
        elif (
            p["kind"] not in ("JOURNAL", "WAL")
            or type(p["slot"]) is not int
            or p["slot"] not in (0, 1)
        ):
            raise FormatError("event kind/slot")
        _int(p["generation"], 63)
    elif kind == "head":
        _keys(
            p,
            {
                "v",
                "profile_id",
                "instance_id",
                "issuer_epoch",
                "registry_id",
                "c_digest",
                "seq",
                "op_id",
                "event_id",
                "record_digest",
                "state_digest",
                "prev_head_digest",
            },
        )
        _common(p)
        for field in ("op_id", "event_id"):
            _id(p[field])
        for field in ("record_digest", "state_digest", "prev_head_digest"):
            _digest(p[field])
    elif kind == "receipt":
        _keys(
            p,
            {
                "v",
                "purpose",
                "enrollment_id",
                "enrollment_kind",
                "installation_id",
                "profile_id",
                "instance_id",
                "issuer_epoch",
                "registry_id",
                "key_id",
                "c_digest",
                "genesis_record_digest",
                "genesis_head_digest",
                "component_state",
            },
        )
        _int(p["v"], 1, 1)
        if (
            p["purpose"] != PURPOSE
            or p["component_state"] != "ENROLLED_TEST_ONLY"
            or p["enrollment_kind"] not in ("fresh", "verified-restore-test")
        ):
            raise FormatError("receipt purpose/state")
        for field in (
            "enrollment_id",
            "installation_id",
            "profile_id",
            "instance_id",
            "issuer_epoch",
            "registry_id",
            "key_id",
        ):
            _id(p[field])
        for field in ("c_digest", "genesis_record_digest", "genesis_head_digest"):
            _digest(p[field])
    elif kind == "test-cut":
        _keys(
            p,
            {
                "v",
                "purpose",
                "source_profile_id",
                "source_instance_id",
                "source_issuer_epoch",
                "source_registry_id",
                "source_key_id",
                "cut_id",
                "c_digest",
                "registry_file_digest",
                "head_file_digest",
                "accepted_seq",
                "accepted_head_digest",
                "state_digest",
                "closed_owner_inventory_digest",
                "snapshot_sentinel_digest",
            },
        )
        _int(p["v"], 1, 1)
        if p["purpose"] != "synthetic-cut-only":
            raise FormatError("cut purpose")
        for field in (
            "source_profile_id",
            "source_instance_id",
            "source_issuer_epoch",
            "source_registry_id",
            "source_key_id",
            "cut_id",
        ):
            _id(p[field])
        for field in (
            "c_digest",
            "registry_file_digest",
            "head_file_digest",
            "accepted_head_digest",
            "state_digest",
            "closed_owner_inventory_digest",
            "snapshot_sentinel_digest",
        ):
            _digest(p[field])
        _int(p["accepted_seq"], 256, 1)
    else:
        raise FormatError("unknown frame type")


def _common(p: dict[str, Any]) -> None:
    _int(p["v"], 1, 1)
    for field in ("profile_id", "instance_id", "issuer_epoch", "registry_id"):
        _id(p[field])
    _digest(p["c_digest"])
    _int(p["seq"], 256, 1)
