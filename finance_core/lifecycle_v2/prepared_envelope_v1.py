"""Authenticated prepared projection envelopes; never a recovery or write ticket."""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING

from finance_core._native_age_v1 import (
    NativeAgeError,
    NativeAgeToolchainV1,
    _check_ciphertext_v1,
    decrypt_v1,
    derive_recipient_v1,
    encrypt_v1,
    generate_identity_v1,
    validate_recipient_v1,
)

from ._prepared_content_v1 import (
    PreparedContentAnchorV1,
    PreparedContentV1,
    _check_prepared_content_v1,
    _validate_prepared_content_anchor_v1,
)
from ._recovery_rows_v1 import RecoveryMaterialError, exact, sha, strict_json
from .contracts import canonical
from .recovery_material_v1 import ClosureLimitsV1, RecoveryEvidenceAuthenticatorV1
from .recovery_material_v2 import (
    ClosureCheckV2,
    ClosureFrontierV2,
    ClosureIdentityV2,
    ClosureInventoryV2,
)

if TYPE_CHECKING:
    from ._prepared_envelope_store_v1 import PreparedEnvelopeStoreV1 as PreparedEnvelopeStoreV1

_FRAME_MAGIC = b"FCPREP01"
_ENVELOPE_MAGIC = b"FCENV001"
_AUTH_DOMAIN = b"finance-prepared-envelope-auth-v1\x00"
_CONTEXT_DOMAIN = b"finance-prepared-envelope-context-v1\x00"
_SCOPE = "offline-prepared-projection-v1"
_MANIFEST_FIELDS = {
    "format",
    "scope",
    "content_kind",
    "context_id",
    "recovery_key_id",
    "temporary_key_id",
    "key_epoch",
    "authentication_key_id",
    "recovery_recipient_sha256",
    "temporary_recipient_sha256",
    "ciphertext_sha256",
    "ciphertext_size_bytes",
    "frame_sha256",
    "frame_size_bytes",
    "mac_sha256",
}


class PreparedEnvelopeErrorV1(ValueError):
    """A bounded refusal without plaintext, secret identity, or child diagnostics."""

    def __init__(self, code: str, message: str = "Prepared envelope operation refused"):
        self.code = code
        super().__init__(message)


PreparedEnvelopeError = PreparedEnvelopeErrorV1


class PreparedEnvelopePublicationUnknown(PreparedEnvelopeErrorV1):
    """Local artifact durability/custody is uncertain; no successful result."""


@dataclass(frozen=True)
class PreparedEnvelopeLimitsV1:
    max_intent_bytes: int = 16 * 1024 * 1024
    max_package_bytes: int = 16 * 1024 * 1024
    max_chain_bytes: int = 64 * 1024 * 1024
    max_packages: int = 32
    max_metadata_bytes: int = 64 * 1024
    max_manifest_bytes: int = 16 * 1024
    max_private_header_bytes: int = 4096
    max_identity_bytes: int = 256
    max_slots: int = 16
    max_store_bytes: int = 512 * 1024 * 1024
    child_deadline_seconds: int = 30
    closure_limits: ClosureLimitsV1 = field(default_factory=ClosureLimitsV1)

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "closure_limits":
                if type(value) is not ClosureLimitsV1:
                    raise PreparedEnvelopeErrorV1("invalid_limits")
                value.__post_init__()
            else:
                maximum = 120 if item.name == "child_deadline_seconds" else item.default
                if type(value) is not int or not 1 <= value <= maximum:
                    raise PreparedEnvelopeErrorV1("invalid_limits")

    @property
    def max_frame_bytes(self) -> int:
        return (
            32
            + self.max_metadata_bytes
            + self.max_intent_bytes
            + self.max_chain_bytes
            + 8 * self.max_packages
        )

    @property
    def max_ciphertext_bytes(self) -> int:
        return self.max_frame_bytes + 1024 * 1024

    @property
    def max_envelope_bytes(self) -> int:
        return 24 + self.max_manifest_bytes + self.max_ciphertext_bytes

    @property
    def max_capsule_bytes(self) -> int:
        return (
            32 + self.max_private_header_bytes + self.max_identity_bytes + self.max_envelope_bytes
        )


def _limits_v1(value=None) -> PreparedEnvelopeLimitsV1:
    if value is None:
        return PreparedEnvelopeLimitsV1()
    if type(value) is not PreparedEnvelopeLimitsV1:
        raise PreparedEnvelopeErrorV1("invalid_limits")
    value.__post_init__()
    return value


def _canonical_v1(value) -> bytes:
    return canonical(value).encode("utf-8")


def _strict_json_v1(payload: bytes, *, maximum: int, limits=None):
    limits = _limits_v1(limits)
    try:
        return strict_json(payload, limits.closure_limits, maximum=maximum)
    except RecoveryMaterialError:
        raise PreparedEnvelopeErrorV1("invalid_wire") from None


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_bytes(value, maximum):
    if type(value) is not bytes or not 1 <= len(value) <= maximum:
        raise PreparedEnvelopeErrorV1("invalid_wire")
    return value


def _identifier(value):
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= 128
        or not value.isascii()
        or any(not (c.isalnum() or c in "._:-") for c in value)
        or not value[0].isalnum()
    ):
        raise PreparedEnvelopeErrorV1("invalid_context")
    return value


@dataclass(frozen=True)
class PreparedEnvelopeKeyContextV1:
    namespace: str
    recovery_key_id: str
    recovery_recipient: str
    key_epoch: int
    authentication_key_id: str


def _key_context(value):
    if type(value) is not PreparedEnvelopeKeyContextV1:
        raise PreparedEnvelopeErrorV1("invalid_context")
    for text in (value.namespace, value.recovery_key_id, value.authentication_key_id):
        _identifier(text)
    if type(value.key_epoch) is not int or not 1 <= value.key_epoch <= 2**31 - 1:
        raise PreparedEnvelopeErrorV1("invalid_context")
    validate_recipient_v1(value.recovery_recipient)
    return value


@dataclass(frozen=True)
class PreparedEnvelopeRefV1:
    key_context: PreparedEnvelopeKeyContextV1
    temporary_recipient: str
    context_id: str
    content_anchor: PreparedContentAnchorV1 = field(repr=False)
    envelope_sha256: str
    envelope_size_bytes: int
    ciphertext_sha256: str
    ciphertext_size_bytes: int
    format: str = "prepared-envelope-ref-v1"


@dataclass(frozen=True)
class PreparedEnvelopeCheckV1:
    operation_id: str
    intent_sha256: str
    envelope_sha256: str
    ciphertext_sha256: str
    identity: ClosureIdentityV2
    frontier: ClosureFrontierV2
    inventory: ClosureInventoryV2
    current_values: tuple[tuple[str, str, int, str, int, str, str], ...] = field(repr=False)
    status: str = "CONTENT_VERIFIED_OFFLINE"
    content_kind: str = "PREPARED_PROJECTION"
    source_finalization: str = "NOT_ESTABLISHED"


def _context(content_anchor, key_context, temporary_recipient, limits):
    _key_context(key_context)
    _validate_prepared_content_anchor_v1(content_anchor, limits=limits.closure_limits)
    validate_recipient_v1(temporary_recipient)
    if temporary_recipient == key_context.recovery_recipient:
        raise PreparedEnvelopeErrorV1("invalid_context")
    base = {
        "format": "prepared-envelope-context-v1",
        "scope": _SCOPE,
        "content_kind": "PREPARED_PROJECTION",
        **asdict(key_context),
        "temporary_recipient": temporary_recipient,
        "temporary_key_id": "temporary-" + _sha(temporary_recipient.encode("ascii")),
    }
    context_id = _sha(
        _CONTEXT_DOMAIN + _canonical_v1({"context": base, "source_anchor": asdict(content_anchor)})
    )
    return {**base, "context_id": context_id}


def _authentication_key(value):
    if type(value) is not bytes or len(value) != 32:
        raise PreparedEnvelopeErrorV1("invalid_authentication_key")
    return value


def _manifest_base(context):
    return {
        "format": "prepared-envelope-manifest-v1",
        "scope": _SCOPE,
        "content_kind": "PREPARED_PROJECTION",
        "context_id": context["context_id"],
        "recovery_key_id": context["recovery_key_id"],
        "temporary_key_id": context["temporary_key_id"],
        "key_epoch": context["key_epoch"],
        "authentication_key_id": context["authentication_key_id"],
        "recovery_recipient_sha256": _sha(context["recovery_recipient"].encode("ascii")),
        "temporary_recipient_sha256": _sha(context["temporary_recipient"].encode("ascii")),
    }


def _frame_v1(content, context, limits):
    if (
        type(content) is not PreparedContentV1
        or content.format != "prepared-content-export-v1"
        or content.content_kind != "PREPARED_PROJECTION"
    ):
        raise PreparedEnvelopeErrorV1("invalid_content")
    _bounded_bytes(content.intent_body, limits.max_intent_bytes)
    _packages(content.packages, limits)
    metadata = _canonical_v1(
        {
            "format": "prepared-envelope-frame-v1",
            "scope": _SCOPE,
            "content_kind": "PREPARED_PROJECTION",
            "context": context,
            "source_anchor": asdict(content.anchor),
            "packages": [asdict(p) for p in content.anchor.packages],
        }
    )
    _bounded_bytes(metadata, limits.max_metadata_bytes)
    frame = (
        _FRAME_MAGIC
        + struct.pack(">QQQ", len(metadata), len(content.intent_body), len(content.packages))
        + metadata
        + content.intent_body
        + b"".join(struct.pack(">Q", len(p)) + p for p in content.packages)
    )
    return _bounded_bytes(frame, limits.max_frame_bytes)


def _packages(packages, limits):
    if type(packages) is not tuple or not 1 <= len(packages) <= limits.max_packages:
        raise PreparedEnvelopeErrorV1("invalid_content")
    total = 0
    digests = set()
    for package in packages:
        _bounded_bytes(package, limits.max_package_bytes)
        total += len(package)
        if total > limits.max_chain_bytes or _sha(package) in digests:
            raise PreparedEnvelopeErrorV1("invalid_content")
        digests.add(_sha(package))


def _decode_envelope_v1(envelope, authentication_key, limits):
    _authentication_key(authentication_key)
    _bounded_bytes(envelope, limits.max_envelope_bytes)
    if len(envelope) < 24 or envelope[:8] != _ENVELOPE_MAGIC:
        raise PreparedEnvelopeErrorV1("invalid_wire")
    metadata_size, ciphertext_size = struct.unpack(">QQ", envelope[8:24])
    if (
        not (
            1 <= metadata_size <= limits.max_manifest_bytes
            and 1 <= ciphertext_size <= limits.max_ciphertext_bytes
        )
        or len(envelope) != 24 + metadata_size + ciphertext_size
    ):
        raise PreparedEnvelopeErrorV1("invalid_wire")
    manifest = _strict_json_v1(
        envelope[24 : 24 + metadata_size], maximum=limits.max_manifest_bytes, limits=limits
    )
    exact(manifest, _MANIFEST_FIELDS)
    mac = sha(manifest["mac_sha256"])
    unsigned = {k: v for k, v in manifest.items() if k != "mac_sha256"}
    expected_mac = hmac.new(
        authentication_key, _AUTH_DOMAIN + _canonical_v1(unsigned), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(mac, expected_mac):
        raise PreparedEnvelopeErrorV1("authentication_failed")
    ciphertext = envelope[24 + metadata_size :]
    if (
        type(manifest["ciphertext_size_bytes"]) is not int
        or manifest["ciphertext_size_bytes"] != ciphertext_size
        or manifest["ciphertext_sha256"] != _sha(ciphertext)
    ):
        raise PreparedEnvelopeErrorV1("content_mismatch")
    if (
        type(manifest["frame_size_bytes"]) is not int
        or not 32 <= manifest["frame_size_bytes"] <= limits.max_frame_bytes
    ):
        raise PreparedEnvelopeErrorV1("invalid_wire")
    sha(manifest["frame_sha256"])
    _check_ciphertext_v1(ciphertext)
    return manifest, ciphertext


def _reference_for_content_v1(
    envelope: bytes,
    *,
    content_anchor: PreparedContentAnchorV1,
    key_context: PreparedEnvelopeKeyContextV1,
    temporary_recipient: str,
    authentication_key: bytes,
    limits: PreparedEnvelopeLimitsV1 | None = None,
) -> PreparedEnvelopeRefV1:
    try:
        limits = _limits_v1(limits)
        context = _context(content_anchor, key_context, temporary_recipient, limits)
        manifest, ciphertext = _decode_envelope_v1(envelope, authentication_key, limits)
        for key, value in _manifest_base(context).items():
            if type(manifest[key]) is not type(value) or manifest[key] != value:
                raise PreparedEnvelopeErrorV1("context_mismatch")
        return PreparedEnvelopeRefV1(
            key_context,
            temporary_recipient,
            context["context_id"],
            content_anchor,
            _sha(envelope),
            len(envelope),
            _sha(ciphertext),
            len(ciphertext),
        )
    except (NativeAgeError, RecoveryMaterialError, UnicodeError, TypeError, KeyError):
        raise PreparedEnvelopeErrorV1("invalid_envelope") from None


def _check_reference(reference, limits):
    if (
        type(reference) is not PreparedEnvelopeRefV1
        or reference.format != "prepared-envelope-ref-v1"
    ):
        raise PreparedEnvelopeErrorV1("invalid_reference")
    context = _context(
        reference.content_anchor, reference.key_context, reference.temporary_recipient, limits
    )
    for value in (reference.context_id, reference.envelope_sha256, reference.ciphertext_sha256):
        sha(value)
    if reference.context_id != context["context_id"]:
        raise PreparedEnvelopeErrorV1("context_mismatch")
    for value, maximum in (
        (reference.envelope_size_bytes, limits.max_envelope_bytes),
        (reference.ciphertext_size_bytes, limits.max_ciphertext_bytes),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise PreparedEnvelopeErrorV1("invalid_reference")
    return context


def _unframe_v1(frame, *, context, expected, limits):
    _bounded_bytes(frame, limits.max_frame_bytes)
    if len(frame) < 32 or frame[:8] != _FRAME_MAGIC:
        raise PreparedEnvelopeErrorV1("invalid_wire")
    metadata_size, body_size, count = struct.unpack(">QQQ", frame[8:32])
    if not (
        1 <= metadata_size <= limits.max_metadata_bytes
        and 1 <= body_size <= limits.max_intent_bytes
        and 1 <= count <= limits.max_packages
    ):
        raise PreparedEnvelopeErrorV1("invalid_wire")
    offset = 32 + metadata_size
    if offset + body_size + 8 * count > len(frame):
        raise PreparedEnvelopeErrorV1("invalid_wire")
    metadata = _strict_json_v1(frame[32:offset], maximum=limits.max_metadata_bytes, limits=limits)
    wanted = {
        "format": "prepared-envelope-frame-v1",
        "scope": _SCOPE,
        "content_kind": "PREPARED_PROJECTION",
        "context": context,
        "source_anchor": asdict(expected.content_anchor),
        "packages": [asdict(p) for p in expected.content_anchor.packages],
    }
    # Canonical equality preserves exact JSON types while normalizing the
    # immutable anchor's tuples to JSON arrays, as the encoder necessarily does.
    if _canonical_v1(metadata) != _canonical_v1(wanted):
        raise PreparedEnvelopeErrorV1("context_mismatch")
    body = frame[offset : offset + body_size]
    offset += body_size
    packages = []
    total = 0
    for _ in range(count):
        if offset + 8 > len(frame):
            raise PreparedEnvelopeErrorV1("invalid_wire")
        size = struct.unpack(">Q", frame[offset : offset + 8])[0]
        offset += 8
        total += size
        if (
            not 1 <= size <= limits.max_package_bytes
            or total > limits.max_chain_bytes
            or offset + size > len(frame)
        ):
            raise PreparedEnvelopeErrorV1("invalid_wire")
        packages.append(frame[offset : offset + size])
        offset += size
    if offset != len(frame):
        raise PreparedEnvelopeErrorV1("invalid_wire")
    _packages(tuple(packages), limits)
    return body, tuple(packages)


def _check_envelope_v1(
    envelope, *, expected, identity, local, authentication_key, authenticator, toolchain, limits
):
    try:
        limits = _limits_v1(limits)
        _check_reference(expected, limits)
        if (
            len(envelope) != expected.envelope_size_bytes
            or _sha(envelope) != expected.envelope_sha256
        ):
            raise PreparedEnvelopeErrorV1("reference_mismatch")
        reference = _reference_for_content_v1(
            envelope,
            content_anchor=expected.content_anchor,
            key_context=expected.key_context,
            temporary_recipient=expected.temporary_recipient,
            authentication_key=authentication_key,
            limits=limits,
        )
        if reference != expected:
            raise PreparedEnvelopeErrorV1("reference_mismatch")
        manifest, ciphertext = _decode_envelope_v1(envelope, authentication_key, limits)
        recipient = derive_recipient_v1(
            identity, toolchain, deadline_seconds=limits.child_deadline_seconds
        )
        required = (
            expected.temporary_recipient if local else expected.key_context.recovery_recipient
        )
        if recipient != required:
            raise PreparedEnvelopeErrorV1("identity_role_mismatch")
        frame = decrypt_v1(
            ciphertext,
            identity,
            toolchain,
            maximum_output=limits.max_frame_bytes,
            deadline_seconds=limits.child_deadline_seconds,
        )
        if len(frame) != manifest["frame_size_bytes"] or _sha(frame) != manifest["frame_sha256"]:
            raise PreparedEnvelopeErrorV1("content_mismatch")
        body, packages = _unframe_v1(
            frame,
            context=_context(
                expected.content_anchor, expected.key_context, expected.temporary_recipient, limits
            ),
            expected=expected,
            limits=limits,
        )
        checked = _check_prepared_content_v1(
            body,
            packages,
            expected=expected.content_anchor,
            authenticator=authenticator,
            limits=limits.closure_limits,
        )
        return _result(expected, checked)
    except (NativeAgeError, RecoveryMaterialError, UnicodeError, TypeError, KeyError, struct.error):
        raise PreparedEnvelopeErrorV1("invalid_envelope") from None


def _result(reference, checked: ClosureCheckV2) -> PreparedEnvelopeCheckV1:
    return PreparedEnvelopeCheckV1(
        reference.content_anchor.operation_id,
        reference.content_anchor.intent_sha256,
        reference.envelope_sha256,
        reference.ciphertext_sha256,
        checked.identity,
        checked.frontier,
        checked.inventory,
        checked.current_values,
    )


def check_prepared_envelope_v1(
    envelope: bytes,
    *,
    expected: PreparedEnvelopeRefV1,
    recovery_identity: bytes,
    authentication_key: bytes,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    toolchain: NativeAgeToolchainV1,
    limits: PreparedEnvelopeLimitsV1 | None = None,
) -> PreparedEnvelopeCheckV1:
    """Check complete source-free content using an independently saved reference and R."""
    return _check_envelope_v1(
        envelope,
        expected=expected,
        identity=recovery_identity,
        local=False,
        authentication_key=authentication_key,
        authenticator=authenticator,
        toolchain=toolchain,
        limits=limits,
    )


def _check_local_envelope_v1(
    envelope: bytes,
    *,
    expected: PreparedEnvelopeRefV1,
    temporary_identity: bytes,
    authentication_key: bytes,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    toolchain: NativeAgeToolchainV1,
    limits: PreparedEnvelopeLimitsV1 | None = None,
) -> PreparedEnvelopeCheckV1:
    return _check_envelope_v1(
        envelope,
        expected=expected,
        identity=temporary_identity,
        local=True,
        authentication_key=authentication_key,
        authenticator=authenticator,
        toolchain=toolchain,
        limits=limits,
    )


def _create_envelope_v1(
    content: PreparedContentV1,
    *,
    key_context: PreparedEnvelopeKeyContextV1,
    authentication_key: bytes,
    authenticator: RecoveryEvidenceAuthenticatorV1,
    toolchain: NativeAgeToolchainV1,
    limits: PreparedEnvelopeLimitsV1 | None = None,
) -> tuple[bytes, bytes, PreparedEnvelopeRefV1]:
    try:
        limits = _limits_v1(limits)
        _authentication_key(authentication_key)
        if type(content) is not PreparedContentV1:
            raise PreparedEnvelopeErrorV1("invalid_content")
        _check_prepared_content_v1(
            content.intent_body,
            content.packages,
            expected=content.anchor,
            authenticator=authenticator,
            limits=limits.closure_limits,
        )
        identity = generate_identity_v1(toolchain, deadline_seconds=limits.child_deadline_seconds)
        temporary_recipient = derive_recipient_v1(
            identity, toolchain, deadline_seconds=limits.child_deadline_seconds
        )
        context = _context(content.anchor, key_context, temporary_recipient, limits)
        frame = _frame_v1(content, context, limits)
        ciphertext = encrypt_v1(
            frame,
            (key_context.recovery_recipient, temporary_recipient),
            toolchain,
            maximum_output=limits.max_ciphertext_bytes,
            deadline_seconds=limits.child_deadline_seconds,
        )
        unsigned = {
            **_manifest_base(context),
            "ciphertext_sha256": _sha(ciphertext),
            "ciphertext_size_bytes": len(ciphertext),
            "frame_sha256": _sha(frame),
            "frame_size_bytes": len(frame),
        }
        manifest = _canonical_v1(
            {
                **unsigned,
                "mac_sha256": hmac.new(
                    authentication_key, _AUTH_DOMAIN + _canonical_v1(unsigned), hashlib.sha256
                ).hexdigest(),
            }
        )
        _bounded_bytes(manifest, limits.max_manifest_bytes)
        envelope = (
            _ENVELOPE_MAGIC
            + struct.pack(">QQ", len(manifest), len(ciphertext))
            + manifest
            + ciphertext
        )
        reference = _reference_for_content_v1(
            envelope,
            content_anchor=content.anchor,
            key_context=key_context,
            temporary_recipient=temporary_recipient,
            authentication_key=authentication_key,
            limits=limits,
        )
        return identity, envelope, reference
    except (NativeAgeError, RecoveryMaterialError, UnicodeError, TypeError, KeyError):
        raise PreparedEnvelopeErrorV1("invalid_envelope") from None


def __getattr__(name):
    if name == "PreparedEnvelopeStoreV1":
        from ._prepared_envelope_store_v1 import PreparedEnvelopeStoreV1

        return PreparedEnvelopeStoreV1
    raise AttributeError(name)
