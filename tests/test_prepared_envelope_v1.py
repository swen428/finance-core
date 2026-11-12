"""Actual prepared content, strict authenticated framing and native role separation."""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass, field, replace

import pytest
from prepared_envelope_test_support import native_rows, prepared_case
from test_native_age_v1 import real_toolchain

from finance_core._native_age_v1 import (
    decrypt_v1,
    derive_recipient_v1,
    encrypt_v1,
    generate_identity_v1,
)
from finance_core.lifecycle_v2.prepared_envelope_v1 import (
    _AUTH_DOMAIN,
    PreparedEnvelopeErrorV1,
    PreparedEnvelopeKeyContextV1,
    PreparedEnvelopeLimitsV1,
    _canonical_v1,
    _check_local_envelope_v1,
    _create_envelope_v1,
    _decode_envelope_v1,
    _limits_v1,
    _reference_for_content_v1,
    _strict_json_v1,
    check_prepared_envelope_v1,
)


@dataclass
class SealedCase:
    content: object = field(repr=False)
    authenticator: object = field(repr=False)
    toolchain: object
    recovery_identity: bytes = field(repr=False)
    temporary_identity: bytes = field(repr=False)
    authentication_key: bytes = field(repr=False)
    envelope: bytes = field(repr=False)
    reference: object


def create_sealed(root, environment, *, padding=b""):
    toolchain = real_toolchain()
    recovery_identity = generate_identity_v1(toolchain)
    recipient = derive_recipient_v1(recovery_identity, toolchain)
    key_context = PreparedEnvelopeKeyContextV1(
        "synthetic-ledger", "recovery-1", recipient, 1, "auth-1"
    )
    authentication_key = os.urandom(32)
    with prepared_case(root, environment, envelope_padding=padding) as case:
        before = native_rows(case.service)
        content = case.preparation.export_content_v1(
            case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
        )
        identity, envelope, reference = _create_envelope_v1(
            content,
            key_context=key_context,
            authentication_key=authentication_key,
            authenticator=case.authenticator,
            toolchain=toolchain,
        )
        assert native_rows(case.service) == before
        result = SealedCase(
            content,
            case.authenticator,
            toolchain,
            recovery_identity,
            identity,
            authentication_key,
            envelope,
            reference,
        )
        case.port.destroy()
        return result


@pytest.fixture(scope="module")
def sealed(tmp_path_factory):
    environment = pytest.MonkeyPatch()
    try:
        return create_sealed(tmp_path_factory.mktemp("prepared-codec"), environment)
    finally:
        environment.undo()


def checked(case, *, envelope=None, expected=None, recovery_identity=None, authentication_key=None):
    return check_prepared_envelope_v1(
        case.envelope if envelope is None else envelope,
        expected=case.reference if expected is None else expected,
        recovery_identity=case.recovery_identity
        if recovery_identity is None
        else recovery_identity,
        authentication_key=case.authentication_key
        if authentication_key is None
        else authentication_key,
        authenticator=case.authenticator,
        toolchain=case.toolchain,
    )


def test_real_prepared_content_and_projection_only_result(sealed):
    result = checked(sealed)
    assert result.status == "CONTENT_VERIFIED_OFFLINE"
    assert result.content_kind == "PREPARED_PROJECTION"
    assert result.source_finalization == "NOT_ESTABLISHED"
    assert result.operation_id == sealed.reference.content_anchor.operation_id
    assert result.intent_sha256 == sealed.reference.content_anchor.intent_sha256
    assert result.frontier == sealed.reference.content_anchor.projected_frontier
    assert not any(
        hasattr(result, name)
        for name in ("owner", "ticket", "confirmed", "connection", "profile", "apply_offline")
    )
    assert (
        _check_local_envelope_v1(
            sealed.envelope,
            expected=sealed.reference,
            temporary_identity=sealed.temporary_identity,
            authentication_key=sealed.authentication_key,
            authenticator=sealed.authenticator,
            toolchain=sealed.toolchain,
        )
        == result
    )
    manifest, _ = _decode_envelope_v1(sealed.envelope, sealed.authentication_key, _limits_v1())
    assert not any(
        name in manifest
        for name in (
            "operation_id",
            "intent_body",
            "source_anchor",
            "namespace",
            "recovery_recipient",
        )
    )
    assert sealed.content.intent_body not in sealed.envelope


def test_native_key_roles_are_not_interchangeable(sealed):
    with pytest.raises(PreparedEnvelopeErrorV1):
        checked(sealed, recovery_identity=sealed.temporary_identity)
    with pytest.raises(PreparedEnvelopeErrorV1):
        _check_local_envelope_v1(
            sealed.envelope,
            expected=sealed.reference,
            temporary_identity=sealed.recovery_identity,
            authentication_key=sealed.authentication_key,
            authenticator=sealed.authenticator,
            toolchain=sealed.toolchain,
        )
    wrong = generate_identity_v1(sealed.toolchain)
    with pytest.raises(PreparedEnvelopeErrorV1):
        checked(sealed, recovery_identity=wrong)


@pytest.mark.parametrize(
    "change",
    [
        {"envelope_sha256": "0" * 64},
        {"ciphertext_sha256": "0" * 64},
        {"envelope_size_bytes": True},
        {"context_id": "0" * 64},
        {"format": "future"},
    ],
)
def test_independently_expected_reference_refuses_mismatch(sealed, change):
    with pytest.raises(PreparedEnvelopeErrorV1):
        checked(sealed, expected=replace(sealed.reference, **change))


def test_private_retry_uses_actual_source_anchor_and_trusted_context(sealed):
    ref = _reference_for_content_v1(
        sealed.envelope,
        content_anchor=sealed.content.anchor,
        key_context=sealed.reference.key_context,
        temporary_recipient=sealed.reference.temporary_recipient,
        authentication_key=sealed.authentication_key,
    )
    assert ref == sealed.reference
    for anchor, context in (
        (replace(sealed.content.anchor, operation_id="different-operation"), ref.key_context),
        (sealed.content.anchor, replace(ref.key_context, key_epoch=2)),
        (sealed.content.anchor, replace(ref.key_context, namespace="other-ledger")),
    ):
        with pytest.raises(PreparedEnvelopeErrorV1):
            _reference_for_content_v1(
                sealed.envelope,
                content_anchor=anchor,
                key_context=context,
                temporary_recipient=ref.temporary_recipient,
                authentication_key=sealed.authentication_key,
            )


@pytest.mark.parametrize(
    "mutation", ["trailing", "truncated", "capsule", "bad-magic", "huge-length"]
)
def test_public_wire_refuses_before_success(sealed, mutation):
    envelope = sealed.envelope
    if mutation == "trailing":
        envelope += b"x"
    elif mutation == "truncated":
        envelope = envelope[:-1]
    elif mutation == "capsule":
        envelope = b"FCLOC001" + envelope[8:]
    elif mutation == "bad-magic":
        envelope = b"UNKNOWN1" + envelope[8:]
    else:
        envelope = envelope[:8] + struct.pack(">QQ", 2**63, 2**63) + envelope[24:]
    with pytest.raises(PreparedEnvelopeErrorV1):
        checked(sealed, envelope=envelope)
    with pytest.raises(PreparedEnvelopeErrorV1):
        _reference_for_content_v1(
            envelope,
            content_anchor=sealed.content.anchor,
            key_context=sealed.reference.key_context,
            temporary_recipient=sealed.reference.temporary_recipient,
            authentication_key=sealed.authentication_key,
        )


def rewrap(case, frame, *, extra_manifest=None):
    ciphertext = encrypt_v1(
        frame,
        (case.reference.key_context.recovery_recipient, case.reference.temporary_recipient),
        case.toolchain,
        maximum_output=_limits_v1().max_ciphertext_bytes,
    )
    manifest, _ = _decode_envelope_v1(case.envelope, case.authentication_key, _limits_v1())
    manifest.update(
        ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
        ciphertext_size_bytes=len(ciphertext),
        frame_sha256=hashlib.sha256(frame).hexdigest(),
        frame_size_bytes=len(frame),
    )
    if extra_manifest:
        manifest.update(extra_manifest)
    manifest.pop("mac_sha256")
    manifest["mac_sha256"] = hmac.new(
        case.authentication_key, _AUTH_DOMAIN + _canonical_v1(manifest), hashlib.sha256
    ).hexdigest()
    raw = _canonical_v1(manifest)
    envelope = b"FCENV001" + struct.pack(">QQ", len(raw), len(ciphertext)) + raw + ciphertext
    reference = _reference_for_content_v1(
        envelope,
        content_anchor=case.content.anchor,
        key_context=case.reference.key_context,
        temporary_recipient=case.reference.temporary_recipient,
        authentication_key=case.authentication_key,
    )
    return envelope, reference


@pytest.mark.parametrize(
    "mutation", ["body", "frame-tail", "metadata-context", "duplicate-package", "count-zero"]
)
def test_resigned_outer_frame_tampering_still_rejected(sealed, mutation):
    _, ciphertext = _decode_envelope_v1(sealed.envelope, sealed.authentication_key, _limits_v1())
    frame = decrypt_v1(
        ciphertext,
        sealed.recovery_identity,
        sealed.toolchain,
        maximum_output=_limits_v1().max_frame_bytes,
    )
    metadata_size, body_size, count = struct.unpack(">QQQ", frame[8:32])
    body_offset = 32 + metadata_size
    if mutation == "body":
        frame = frame[:body_offset] + bytes([frame[body_offset] ^ 1]) + frame[body_offset + 1 :]
    elif mutation == "frame-tail":
        frame += b"trailing"
    elif mutation == "metadata-context":
        metadata = _strict_json_v1(frame[32:body_offset], maximum=65536)
        metadata["context"]["namespace"] = "forged-context"
        raw = _canonical_v1(metadata)
        frame = (
            frame[:8] + struct.pack(">QQQ", len(raw), body_size, count) + raw + frame[body_offset:]
        )
    elif mutation == "count-zero":
        frame = frame[:24] + struct.pack(">Q", 0) + frame[32:]
    else:
        package_section = frame[body_offset + body_size :]
        frame = frame[:24] + struct.pack(">Q", count * 2) + frame[32:] + package_section
    envelope, reference = rewrap(sealed, frame)
    with pytest.raises(PreparedEnvelopeErrorV1):
        checked(sealed, envelope=envelope, expected=reference)


def test_unknown_manifest_field_refused_even_when_resigned(sealed):
    _, ciphertext = _decode_envelope_v1(sealed.envelope, sealed.authentication_key, _limits_v1())
    frame = decrypt_v1(
        ciphertext,
        sealed.recovery_identity,
        sealed.toolchain,
        maximum_output=_limits_v1().max_frame_bytes,
    )
    with pytest.raises(PreparedEnvelopeErrorV1):
        rewrap(sealed, frame, extra_manifest={"unknown": "rejected"})


@pytest.mark.parametrize(
    "raw", [b'{"a":1,"a":1}', b'{"a":1.0}', b'{"a":NaN}', b'{"a":1} ', b'{"a":true,"b":1e5}']
)
def test_strict_canonical_json_refuses_ambiguous_inputs(raw):
    with pytest.raises(PreparedEnvelopeErrorV1):
        _strict_json_v1(raw, maximum=128)


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_intent_bytes", True),
        ("max_packages", 33),
        ("max_store_bytes", 0),
        ("child_deadline_seconds", 121),
        ("max_metadata_bytes", 65537),
    ],
)
def test_limits_exact_lowering_only(name, value):
    with pytest.raises(PreparedEnvelopeErrorV1):
        PreparedEnvelopeLimitsV1(**{name: value})
    assert PreparedEnvelopeLimitsV1(child_deadline_seconds=120).child_deadline_seconds == 120


def test_small_exact_bounds_and_plus_one(sealed):
    limits = PreparedEnvelopeLimitsV1(
        max_intent_bytes=len(sealed.content.intent_body),
        max_package_bytes=max(map(len, sealed.content.packages)),
        max_chain_bytes=sum(map(len, sealed.content.packages)),
        max_packages=len(sealed.content.packages),
    )
    assert (
        check_prepared_envelope_v1(
            sealed.envelope,
            expected=sealed.reference,
            recovery_identity=sealed.recovery_identity,
            authentication_key=sealed.authentication_key,
            authenticator=sealed.authenticator,
            toolchain=sealed.toolchain,
            limits=limits,
        ).status
        == "CONTENT_VERIFIED_OFFLINE"
    )
    with pytest.raises(PreparedEnvelopeErrorV1):
        check_prepared_envelope_v1(
            sealed.envelope,
            expected=sealed.reference,
            recovery_identity=sealed.recovery_identity,
            authentication_key=sealed.authentication_key,
            authenticator=sealed.authenticator,
            toolchain=sealed.toolchain,
            limits=replace(limits, max_intent_bytes=limits.max_intent_bytes - 1),
        )


def test_real_valid_prepared_frame_larger_than_old_eight_mib(tmp_path, monkeypatch):
    case = create_sealed(tmp_path, monkeypatch, padding=b"synthetic-large-evidence-" * 100000)
    manifest, _ = _decode_envelope_v1(case.envelope, case.authentication_key, _limits_v1())
    assert manifest["frame_size_bytes"] > 8 * 1024 * 1024
    assert checked(case).status == "CONTENT_VERIFIED_OFFLINE"
