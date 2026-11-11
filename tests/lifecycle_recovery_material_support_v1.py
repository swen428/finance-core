"""Pre-registered synthetic acquisition bytes; never a production authenticator.

Evidence methods and archive export only look up already acquired records. In
particular, checker requests can never manufacture observations or human acts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import asdict

from finance_core.instance_admission import (
    acquire_production_owner,
    admit_production_instance,
    prepare_production_profile,
)
from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import (
    LedgerService,
    RuntimeBinding,
    VerifiedAction,
    VerifiedDelivery,
    VerifiedObservation,
    VerifiedSource,
)
from finance_core.lifecycle_v2.contracts import ExpectedAction, ExpectedDisplay, canonical, digest


def sha(data):
    return hashlib.sha256(data).hexdigest()


def response(text, ref="source", fields=None):
    """Synthetic provider bytes grounded in the explicitly acquired input."""
    operations = []
    for index, (field, value) in enumerate(fields.items()):
        operation = {
            "operation_id": f"o{index}",
            "op": "set",
            "field": field,
            "evidence": [
                {
                    "ref": ref,
                    "start": text.index(value),
                    "end": text.index(value) + len(value),
                    "quote": value,
                    "role": "replacement_value",
                }
            ],
        }
        if field == "transaction_date":
            operation["date_expression"] = {
                "kind": "absolute_date",
                "year": 2026,
                "month": 10,
                "day": 24,
            }
        else:
            operation["value"] = value
        operations.append(operation)
    return canonical(
        {
            "schema": "finance-independent-ai-result-v2",
            "outcome": "proposal",
            "intent": "personal_expense",
            "operations": operations,
            "unresolved": [],
            "coverage": [
                {
                    "start": 0,
                    "end": len(text),
                    "label": "resolved_instruction",
                    "operation_ids": [operation["operation_id"] for operation in operations],
                    "question_ids": [],
                }
            ],
        }
    )


class SyntheticAuthenticatorV1:
    """A fixed, out-of-package test root; no production cryptography claim."""

    owner_id = "synthetic-archive-owner"
    key_id = "synthetic-archive-key"

    def __init__(self, secret=b"test-only-preconfigured-root-not-from-package"):
        self._secret = secret
        self.calls = 0

    def authenticate(self, payload):
        return hmac.digest(self._secret, payload, "sha256")

    def verify(self, *, owner_id, key_id, payload, authentication):
        self.calls += 1
        if (
            owner_id != self.owner_id
            or key_id != self.key_id
            or not hmac.compare_digest(self.authenticate(payload), authentication)
        ):
            raise ValueError("Untrusted synthetic archive authentication")


class SizedSyntheticAuthenticatorV1(SyntheticAuthenticatorV1):
    """Valid HMAC plus fixed padding to exercise authentication wire capacity."""

    def __init__(self, size):
        super().__init__()
        self.size = size

    def authenticate(self, payload):
        signature = super().authenticate(payload)
        return signature + b"\x00" * (self.size - len(signature))


def resign(entry, authenticator):
    """Adversarial tests may hold the test key; finance audit must still run."""
    unsigned = {key: value for key, value in entry.items() if key != "authentication_b64"}
    entry["authentication_b64"] = base64.b64encode(
        authenticator.authenticate(canonical(unsigned).encode())
    ).decode()
    return entry


def resign_provider_context(entry, authenticator):
    """Keep an adversarial archive's actual request bytes aligned with its context."""
    request = canonical(entry["context"]).encode()
    artifact = next(item for item in entry["artifacts"] if item["role"] == "request")
    artifact.update(
        bytes_b64=base64.b64encode(request).decode(), size=len(request), sha256=sha(request)
    )
    entry["value"]["request_sha256"] = sha(request)
    return resign(entry, authenticator)


class RecordingEvidencePortV1:
    """Explicit normal acquisition, immutable export, and fail-closed lookups."""

    def __init__(self, authenticator, *, envelope_padding=b""):
        self.authenticator = authenticator
        self._envelope_padding = envelope_padding
        self._records = {}
        self.archives = {}
        self.disabled = False
        self.exports = []

    def _register(self, kind, value, context, artifacts):
        if self.disabled:
            raise AssertionError("Original evidence port is destroyed")
        key = (kind, value.evidence_id)
        # This is the synthetic acquisition envelope, saved once before use.
        envelope = (
            canonical(
                {
                    "synthetic_transport": "acquisition-v1",
                    "kind": kind,
                    "value": asdict(value),
                    "context": None if context is None else asdict(context),
                }
            ).encode()
            + self._envelope_padding
        )
        artifacts = dict(artifacts, **{"raw-envelope": envelope})
        entry = {
            "format": "native-evidence-archive-v1",
            "kind": kind,
            "evidence_id": value.evidence_id,
            "owner_id": self.authenticator.owner_id,
            "key_id": self.authenticator.key_id,
            "context": None if context is None else asdict(context),
            "value": asdict(value),
            "artifacts": [
                {
                    "role": role,
                    "sha256": sha(data),
                    "size": len(data),
                    "bytes_b64": base64.b64encode(data).decode(),
                    "locator": "historical-only://synthetic/" + kind + "/" + value.evidence_id,
                }
                for role, data in sorted(artifacts.items())
            ],
        }
        encoded = canonical(resign(entry, self.authenticator)).encode()
        if key in self.archives:
            raise AssertionError("Acquisition key already registered")
        self.archives[key] = encoded
        self._records[key] = (value, None if context is None else canonical(asdict(context)))
        return value

    def register_source(self, key, text, *, principal="user", source_key=None):
        value = VerifiedSource(
            key,
            principal,
            "chat",
            source_key or "message-" + key,
            text,
            sha(text.encode()),
            1792800000,
        )
        return self._register("source", value, None, {"original": text.encode()})

    def register_provider(self, key, expected, raw):
        value = VerifiedObservation(
            key,
            expected.source_id,
            "user",
            raw,
            digest(asdict(expected)),
            expected.projection["reply_text"],
        )
        return self._register(
            "provider",
            value,
            expected,
            {"request": canonical(asdict(expected)).encode(), "response": raw.encode()},
        )

    def register_delivery(self, service, review, key):
        # Read bytes of the actual review already prepared for normal display.
        with lifecycle_read_operation(service.owner) as conn:
            display = (
                conn.execute(
                    "SELECT material_json FROM l2_reviews WHERE review_id=?", (review.review_id,)
                )
                .fetchone()[0]
                .encode()
            )
        assert sha(display) == review.review_sha256
        expected = ExpectedDisplay(service.binding, review.review_id, review.review_sha256)
        value = VerifiedDelivery(key, review.review_id, review.review_sha256, "user", "chat")
        return self._register("delivery", value, expected, {"display": display})

    def register_action(self, service, review, delivery_key, key):
        expected = ExpectedAction(
            service.binding, review.review_id, review.review_sha256, delivery_key
        )
        value = VerifiedAction(
            key, review.review_id, review.review_sha256, delivery_key, "user", "chat"
        )
        return self._register("action", value, expected, {})

    def _get(self, kind, key, expected=None):
        if self.disabled:
            raise AssertionError("Original evidence port is destroyed")
        value, context = self._records[(kind, key)]
        assert context == (None if expected is None else canonical(asdict(expected)))
        return value

    def source(self, conn, evidence_id):
        return self._get("source", evidence_id)

    def provider(self, conn, evidence_id, expected):
        return self._get("provider", evidence_id, expected)

    def delivery(self, conn, evidence_id, expected):
        return self._get("delivery", evidence_id, expected)

    def action(self, conn, evidence_id, expected):
        return self._get("action", evidence_id, expected)

    def export(self, kind, evidence_id):
        if self.disabled:
            raise AssertionError("Original archive is destroyed")
        key = (kind, evidence_id)
        self.exports.append(key)
        return self.archives[key]

    def destroy(self):
        self.disabled = True
        self._records.clear()
        self.archives.clear()


def make_service(tmp_path, monkeypatch):
    from instance_admission_support_v1 import synthetic_config

    cfg = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
    owner = acquire_production_owner(cfg)
    authenticator = SyntheticAuthenticatorV1()
    port = RecordingEvidencePortV1(authenticator)
    service = LedgerService(
        owner,
        port,
        RuntimeBinding(cfg.instance_id, "gen1", "user", "chat"),
        clock=lambda: 1792800000,
    )
    return service, port, cfg, authenticator


def stage_expense(service, port, key, *, amount="12.00", currency="SGD", stage="confirm"):
    text = f"2026-10-24 Cafe {currency} {amount}"
    port.register_source(key, text)
    source = service.capture(key, "capture-" + key)
    if stage == "capture":
        return source, None, None, None
    expected = service.expected_invocation(source.source_id, 0)
    raw = response(
        text,
        fields={
            "amount": amount,
            "currency": currency,
            "merchant": "Cafe",
            "transaction_date": "2026-10-24",
        },
    )
    port.register_provider("obs-" + key, expected, raw)
    proposal = service.propose(source.source_id, "obs-" + key, 0, "propose-" + key)
    if stage == "propose":
        return source, proposal, None, None
    review = service.prepare_review(proposal.proposal_id, proposal.version)
    if stage == "review":
        return source, proposal, review, None
    port.register_delivery(service, review, "delivery-" + key)
    service.record_delivery(review.review_id, "delivery-" + key)
    if stage == "delivery":
        return source, proposal, review, None
    port.register_action(service, review, "delivery-" + key, "action-" + key)
    receipt = service.confirm(review.review_id, "action-" + key, "confirm-" + key)
    return source, proposal, review, receipt


def correct_expense(service, port, source, receipt):
    key = "correction-reply"
    text = "change amount to SGD 15.00\n原因：记录有误"
    port.register_source(key, text)
    expected = service.expected_invocation(
        source.source_id, receipt.version, receipt.record_id, key, "记录有误"
    )
    port.register_provider(
        "obs-correction", expected, response(text, "reply", {"amount": "15.00", "currency": "SGD"})
    )
    review = service.prepare_correction(
        receipt.record_id,
        receipt.version,
        "obs-correction",
        "prepare-correction",
        reply_evidence_id=key,
        reason="记录有误",
    )
    port.register_delivery(service, review, "delivery-correction")
    service.record_delivery(review.review_id, "delivery-correction")
    port.register_action(service, review, "delivery-correction", "action-correction")
    corrected = service.confirm(review.review_id, "action-correction", "confirm-correction")
    return review, corrected


def wire(payload):
    return json.loads(payload)


def encoded(value):
    return canonical(value).encode()
