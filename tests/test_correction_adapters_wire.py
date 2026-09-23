"""Strict wire vectors for local D2b proof material (synthetic only)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from finance_core.correction_adapters.wire import (
    DECISION_KEYS,
    CorrectionWireError,
    authentic_consumption,
    authentic_decision,
    consumption_seal,
    decision_digest,
    decision_signature,
    key_id,
    strict_object,
)


def _canonical(value: object) -> bytes:
    # Deliberately independent of the production canonical encoder.
    return json.dumps(
        {"contract_version": "finance-canonical-json-v1", "value": value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _frame(tag: str, value: bytes) -> bytes:
    return tag.encode("ascii") + b"\x00" + len(value).to_bytes(8, "big") + value


def _envelope() -> dict[str, object]:
    return {
        "schema": "correction-decision-v1",
        "authority_id": "corrauth_a",
        "key_id": "a" * 64,
        "realm": "realm_a",
        "instance_id": "instance_a",
        "actor": "actor_a",
        "target_id": "txn_a",
        "expected_version": 0,
        "predecessor_hash": "b" * 64,
        "plan_id": "corrplan_a",
        "plan_hash": "c" * 64,
        "source_hash": "d" * 64,
        "before_hash": "e" * 64,
        "after_hash": "f" * 64,
        "fact_hash": None,
        "snapshot_id": None,
        "snapshot_hash": None,
        "reason": "correct amount",
        "renderer": "correction-terminal-v1",
        "display_sha256": "1" * 64,
        "challenge": "2" * 64,
        "issued_at_epoch": 1_700_000_000,
        "expires_at_epoch": 1_700_000_300,
        "nonce": "3" * 64,
    }


def test_full_wire_matches_independent_length_framed_hmac() -> None:
    key = b"K" * 32
    envelope = _envelope()
    assert key_id(key) == hashlib.sha256(_frame("finance-correction-key-id-v1", key)).hexdigest()
    expected_signature = hmac.new(
        key,
        _frame("finance-correction-decision-v1", _canonical(envelope)),
        hashlib.sha256,
    ).hexdigest()
    assert decision_signature(key, envelope) == expected_signature
    expected_digest = hashlib.sha256(
        _frame(
            "finance-correction-decision-digest-v1",
            _canonical({"envelope": envelope, "signature": expected_signature}),
        )
    ).hexdigest()
    assert decision_digest(envelope, expected_signature) == expected_digest
    raw = _canonical(envelope).decode()
    assert authentic_decision(key, raw, expected_signature) == envelope
    material = {
        "schema": "correction-consumption-v1",
        "key_id": envelope["key_id"],
        "realm": envelope["realm"],
        "actor": envelope["actor"],
        "target_id": envelope["target_id"],
        "instance_id": envelope["instance_id"],
        "authority_id": envelope["authority_id"],
        "plan_id": envelope["plan_id"],
        "correction_id": "corr_a",
        "nonce": envelope["nonce"],
        "decision_digest": expected_digest,
        "result_core_hash": "4" * 64,
        "checked_at_epoch": 1_700_000_010,
    }
    expected_seal = hmac.new(
        key,
        _frame("finance-correction-consumption-v1", _canonical(material)),
        hashlib.sha256,
    ).hexdigest()
    assert consumption_seal(key, material) == expected_seal
    assert authentic_consumption(key, _canonical(material).decode(), expected_seal) == material


@pytest.mark.parametrize("name", sorted(DECISION_KEYS))
def test_every_decision_binding_is_signed(name: str) -> None:
    key = b"K" * 32
    envelope = _envelope()
    signature = decision_signature(key, envelope)
    value = envelope[name]
    envelope[name] = (
        (value + 1) if type(value) is int else ("changed" if value is None else value + "x")
    )
    with pytest.raises(CorrectionWireError):
        authentic_decision(key, _canonical(envelope).decode(), signature)


def test_duplicate_noncanonical_and_wrong_type_refuse_before_signature() -> None:
    envelope = _envelope()
    raw = _canonical(envelope).decode()
    duplicate = raw.replace(
        '"schema":"correction-decision-v1"',
        '"schema":"correction-decision-v1","schema":"correction-decision-v1"',
    )
    with pytest.raises(CorrectionWireError, match="duplicate"):
        strict_object(duplicate, DECISION_KEYS)
    with pytest.raises(CorrectionWireError, match="noncanonical"):
        strict_object(json.dumps(json.loads(raw), indent=2), DECISION_KEYS)
    envelope["expected_version"] = True
    with pytest.raises(CorrectionWireError, match="type"):
        strict_object(_canonical(envelope), DECISION_KEYS)
    envelope = _envelope()
    envelope["reason"] = "e\u0301"
    with pytest.raises(CorrectionWireError, match="noncanonical"):
        strict_object(_canonical(envelope), DECISION_KEYS)
