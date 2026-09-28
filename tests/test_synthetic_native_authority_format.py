"""Independent format vectors for the synthetic native-authority component."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from finance_core.synthetic_native_authority.format import (
    FormatError,
    decode_frame,
    encode_frame,
)

KEY = bytes(range(32))
ZERO = "0" * 64
IDENTITY = "0123456789abcdef0123456789abcdef"
OTHER_ID = "fedcba9876543210fedcba9876543210"
FORMAT_SPECS = {
    "descriptor": (b"FNA1CAP\n", 16_384, b"finance-core/synthetic-authority/v1/capability\0"),
    "registry": (b"FNA1REG\n", 4_096, b"finance-core/synthetic-authority/v1/registry\0"),
    "head": (b"FNA1HED\n", 2_048, b"finance-core/synthetic-authority/v1/head\0"),
    "receipt": (b"FNA1RCP\n", 2_048, b"finance-core/synthetic-authority/v1/receipt\0"),
}
OBJECT_ROLES = {
    "main": ("root", "main.witness", ["witness"]),
    "gate": ("root", "profile-gate.lock", ["lock", "witness"]),
    "journal0": ("slots", "journal-0.slot", ["barrier", "read", "reset", "write"]),
    "journal1": ("slots", "journal-1.slot", ["barrier", "read", "reset", "write"]),
    "wal0": ("slots", "wal-0.slot", ["barrier", "read", "reset", "write"]),
    "wal1": ("slots", "wal-1.slot", ["barrier", "read", "reset", "write"]),
    "registry": ("authority", "registry.log", ["append", "barrier", "read"]),
    "head": ("authority", "committed-head.log", ["append", "barrier", "read"]),
    "receipt": ("authority", "enrollment.receipt", ["barrier", "enroll-write", "read"]),
    "lifecycle": ("authority", "core-lifecycle.lock", ["lock", "witness"]),
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _independent_frame(kind: str, payload: dict[str, object], *, raw: bytes | None = None) -> bytes:
    """Construct a frame from the published wire description, not production code."""

    magic, size, domain = FORMAT_SPECS[kind]
    body_payload = _canonical(payload) if raw is None else raw
    if len(body_payload) < 2 or len(body_payload) > size - 44:
        raise ValueError("test vector exceeds fixed frame bound")
    body = magic + len(body_payload).to_bytes(4, "big") + body_payload
    body += b"\0" * (size - 32 - len(body))
    return body + hmac.new(KEY, domain + body, hashlib.sha256).digest()


def _descriptor_payload() -> dict[str, object]:
    directories = {
        name: {"dev": 1, "ino": index, "uid": 501, "mode": 448, "acl_policy": "no-grants-v1"}
        for index, name in enumerate(("root", "authority", "slots"), start=1)
    }
    objects = {
        role: {
            "directory": directory,
            "basename": basename,
            "dev": 1,
            "ino": index + 10,
            "uid": 501,
            "mode": 384,
            "nlink": 1,
            "type": "regular",
            "acl_policy": "no-grants-v1",
            "operations": operations,
        }
        for index, (role, (directory, basename, operations)) in enumerate(OBJECT_ROLES.items())
    }
    return {
        "v": 1,
        "purpose": "synthetic-component-only",
        "installation_id": IDENTITY,
        "profile_id": OTHER_ID,
        "instance_id": IDENTITY,
        "issuer_epoch": OTHER_ID,
        "registry_id": IDENTITY,
        "key_id": OTHER_ID,
        "enrollment_id": IDENTITY,
        "enrollment_kind": "fresh",
        "directories": directories,
        "objects": objects,
        "limits": {
            "registry_record_cap": 256,
            "head_record_cap": 256,
            "registry_byte_cap": 1_048_576,
            "head_byte_cap": 524_288,
            "generation_cap": 63,
            "token_cap": 16,
            "slot_byte_cap": 65_536,
            "operation_byte_cap": 4_096,
        },
        "barrier_profile": "process-crash-fsync-v1",
    }


def _registry_payload() -> dict[str, object]:
    return {
        "v": 1,
        "profile_id": IDENTITY,
        "instance_id": OTHER_ID,
        "issuer_epoch": IDENTITY,
        "registry_id": OTHER_ID,
        "c_digest": "a" * 64,
        "seq": 1,
        "op_id": IDENTITY,
        "event_id": OTHER_ID,
        "prev_record_digest": ZERO,
        "event": "GENESIS",
        "kind": None,
        "generation": 0,
        "slot": None,
        "prior_state_digest": ZERO,
        "result_state_digest": "b" * 64,
    }


def _head_payload() -> dict[str, object]:
    return {
        "v": 1,
        "profile_id": IDENTITY,
        "instance_id": OTHER_ID,
        "issuer_epoch": IDENTITY,
        "registry_id": OTHER_ID,
        "c_digest": "a" * 64,
        "seq": 1,
        "op_id": IDENTITY,
        "event_id": OTHER_ID,
        "record_digest": "c" * 64,
        "state_digest": "b" * 64,
        "prev_head_digest": ZERO,
    }


def _receipt_payload() -> dict[str, object]:
    return {
        "v": 1,
        "purpose": "synthetic-component-only",
        "enrollment_id": IDENTITY,
        "enrollment_kind": "fresh",
        "installation_id": IDENTITY,
        "profile_id": OTHER_ID,
        "instance_id": IDENTITY,
        "issuer_epoch": OTHER_ID,
        "registry_id": IDENTITY,
        "key_id": OTHER_ID,
        "c_digest": "a" * 64,
        "genesis_record_digest": "c" * 64,
        "genesis_head_digest": "d" * 64,
        "component_state": "ENROLLED_TEST_ONLY",
    }


@pytest.mark.parametrize(
    ("kind", "payload_factory"),
    [
        pytest.param("descriptor", _descriptor_payload, id="FMT-001-descriptor"),
        pytest.param("registry", _registry_payload, id="FMT-001-registry"),
        pytest.param("head", _head_payload, id="FMT-001-head"),
        pytest.param("receipt", _receipt_payload, id="FMT-001-receipt"),
    ],
)
def test_fixed_frames_match_independent_bytes_and_round_trip(
    kind: str, payload_factory: object
) -> None:
    payload = payload_factory()  # type: ignore[operator]
    expected = _independent_frame(kind, payload)
    actual = encode_frame(kind, payload, KEY)

    assert actual == expected
    assert len(actual) == FORMAT_SPECS[kind][1]
    assert decode_frame(kind, actual, KEY) == payload


def test_frame_authentication_is_domain_separated() -> None:
    payload = _head_payload()
    frame = _independent_frame("head", payload)

    assert decode_frame("head", frame, KEY) == payload
    with pytest.raises(FormatError):
        decode_frame("registry", frame, KEY)
    with pytest.raises(FormatError):
        decode_frame("head", frame, bytes(reversed(KEY)))


@pytest.mark.parametrize(
    ("case_id", "raw", "payload"),
    [
        pytest.param(
            "FMT-002-duplicate-key",
            b'{"c_digest":"'
            + b"a" * 64
            + b'","c_digest":"'
            + b"a" * 64
            + b'","event_id":"'
            + OTHER_ID.encode()
            + b'","op_id":"'
            + IDENTITY.encode()
            + b'","prev_head_digest":"'
            + ZERO.encode()
            + b'","record_digest":"'
            + b"c" * 64
            + b'","registry_id":"'
            + OTHER_ID.encode()
            + b'","seq":1,"state_digest":"'
            + b"b" * 64
            + b'","instance_id":"'
            + OTHER_ID.encode()
            + b'","issuer_epoch":"'
            + IDENTITY.encode()
            + b'","profile_id":"'
            + IDENTITY.encode()
            + b'","v":1}',
            _head_payload(),
            id="FMT-002-duplicate-key",
        ),
        pytest.param(
            "FMT-002-unknown-field",
            None,
            {**_head_payload(), "unexpected": 1},
            id="FMT-002-unknown-field",
        ),
        pytest.param(
            "FMT-002-bool-is-not-int",
            None,
            {**_head_payload(), "seq": True},
            id="FMT-002-bool-is-not-int",
        ),
        pytest.param(
            "FMT-002-invalid-id",
            None,
            {**_head_payload(), "profile_id": "A" * 32},
            id="FMT-002-invalid-id",
        ),
    ],
)
def test_authenticated_but_invalid_payloads_are_rejected(
    case_id: str, raw: bytes | None, payload: dict[str, object]
) -> None:
    del case_id  # pytest's visible parameter ID is the case identifier.
    frame = _independent_frame("head", payload, raw=raw)

    with pytest.raises(FormatError):
        decode_frame("head", frame, KEY)


@pytest.mark.parametrize(
    ("case_id", "payload"),
    [
        pytest.param(
            "FMT-002-invalid-enum",
            {**_registry_payload(), "event": "NOT_AN_EVENT"},
            id="FMT-002-invalid-enum",
        ),
        pytest.param(
            "FMT-002-generation-range",
            {**_registry_payload(), "generation": 64},
            id="FMT-002-generation-range",
        ),
    ],
)
def test_authenticated_registry_enum_and_range_are_rejected(
    case_id: str, payload: dict[str, object]
) -> None:
    del case_id
    frame = _independent_frame("registry", payload)

    with pytest.raises(FormatError):
        decode_frame("registry", frame, KEY)


@pytest.mark.parametrize(
    "case_id",
    [
        "FMT-003-truncated",
        "FMT-003-extra-byte",
        "FMT-003-wrong-magic",
        "FMT-003-nonzero-padding",
        "FMT-003-bad-mac",
    ],
)
def test_bad_frame_boundaries_are_rejected(case_id: str) -> None:
    payload = _head_payload()
    valid = _independent_frame("head", payload)
    if case_id.endswith("truncated"):
        candidate = valid[:-1]
    elif case_id.endswith("extra-byte"):
        candidate = valid + b"x"
    elif case_id.endswith("wrong-magic"):
        candidate = b"X" + valid[1:]
    elif case_id.endswith("nonzero-padding"):
        raw = _canonical(payload)
        padding_start = 12 + len(raw)
        body = bytearray(valid[:-32])
        body[padding_start] = 1
        candidate = (
            bytes(body)
            + hmac.new(KEY, FORMAT_SPECS["head"][2] + bytes(body), hashlib.sha256).digest()
        )
    else:
        candidate = valid[:-1] + bytes([valid[-1] ^ 1])

    with pytest.raises(FormatError):
        decode_frame("head", candidate, KEY)
