"""Durable signed synthetic authority for Application amendment acceptance tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Mapping

from test_application_posting_recovery_v1 import BINDING, NOW

from finance_core.application.amendment_contract import (
    AMENDMENT_SCHEMA,
    AmendmentBinding,
    amendment_patch_sha256,
)

AMENDMENT_KEY = b"synthetic human amendment authority key for acceptance tests"
_TABLES = (
    "synthetic_amendment_displays_v1",
    "synthetic_amendment_replies_v1",
    "synthetic_amendment_evidence_v1",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _proof_digest(proof: Mapping[str, object]) -> str:
    """The consumed marker is mutable transport state, not accepted proof."""
    unsigned = {
        key: value for key, value in proof.items() if key not in {"evidence_digest", "consumed"}
    }
    return _digest(unsigned)


def _signature(table: str, value: object, key: bytes = AMENDMENT_KEY) -> str:
    payload = (table + "\x00" + _canonical(value)).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _ensure_tables(connection: sqlite3.Connection) -> None:
    for table in _TABLES:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )


def _persist(
    connection: sqlite3.Connection,
    table: str,
    record_id: str,
    material: Mapping[str, object],
    *,
    key: bytes = AMENDMENT_KEY,
) -> None:
    connection.execute(
        f"INSERT INTO {table} (id, material, signature) VALUES (?, ?, ?)",
        (record_id, _canonical(dict(material)), _signature(table, material, key)),
    )


def _load(
    connection: sqlite3.Connection,
    table: str,
    record_id: str,
    *,
    key: bytes = AMENDMENT_KEY,
) -> dict[str, object]:
    row = connection.execute(
        f"SELECT material, signature FROM {table} WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Missing signed synthetic amendment evidence: {table}/{record_id}")
    material = json.loads(str(row[0]))
    if not isinstance(material, dict) or not hmac.compare_digest(
        str(row[1]), _signature(table, material, key)
    ):
        raise ValueError(f"Invalid signed synthetic amendment evidence: {table}/{record_id}")
    return material


def persist_signed_amendment(
    connection: sqlite3.Connection,
    review: object,
    patch: Mapping[str, object],
    *,
    evidence_id: str = "amendment-evidence-one",
    amendment_binding: AmendmentBinding | None = None,
    proof_overrides: Mapping[str, object] | None = None,
    display_overrides: Mapping[str, object] | None = None,
    reply_overrides: Mapping[str, object] | None = None,
    sign_with: bytes = AMENDMENT_KEY,
) -> dict[str, str]:
    """Append HMAC-sealed receive, delivered-display, and direct-reply evidence.

    The test-only port below reads all three records through the exact SQLite
    connection supplied by Core, then independently binds their material to
    the immutable review and source evidence. Nothing in the port approves a
    request DTO or writes/consumes evidence.
    """
    _ensure_tables(connection)
    projection = dict(getattr(review, "projection"))
    review_id = str(getattr(review, "review_id"))
    review_hash = str(getattr(review, "review_hash"))
    base = projection["proposal_review"]
    source = projection["source"]
    if not isinstance(base, Mapping) or not isinstance(source, Mapping):
        raise AssertionError("Amendment review is missing locked base/source projection material")
    binding = amendment_binding or AmendmentBinding(
        BINDING, "synthetic-amendment-authority", "synthetic-amendment-key-v1"
    )
    display_id = f"synthetic-amendment-display-{evidence_id}"
    reply_id = f"synthetic-amendment-reply-{evidence_id}"
    decision_id = f"synthetic-amendment-decision-{evidence_id}"
    common = {
        "schema": "finance-application-synthetic-amendment-evidence-v1",
        "instance_id": binding.binding.instance_id,
        "human_principal_id": binding.binding.human_principal_id,
        "conversation_id": binding.binding.submission_client_id,
        "review_id": review_id,
        "review_projection_hash": review_hash,
    }
    display: dict[str, object] = {
        **common,
        "display_id": display_id,
        "state": "delivered",
        "direct": True,
        "private": True,
        "source_evidence_id": source["evidence_id"],
        "source_evidence_digest": source["evidence_digest"],
        "projection": projection,
        "sent_at": NOW - 1,
    }
    if display_overrides:
        display.update(display_overrides)
    reply: dict[str, object] = {
        **common,
        "reply_id": reply_id,
        "decision_id": decision_id,
        "display_id": display_id,
        "display_evidence_digest": _digest(display),
        "reply_to_display_id": display_id,
        "action": "amend",
        "direct": True,
        "private": True,
        "patch": dict(patch),
        "observed_at": NOW,
        "issued_at": NOW,
        "expires_at": NOW + 500,
    }
    if reply_overrides:
        reply.update(reply_overrides)
    reply_digest = _digest(reply)
    proof: dict[str, object] = {
        "schema": AMENDMENT_SCHEMA,
        "namespace": binding.amendment_namespace,
        "key_id": binding.amendment_key_id,
        "instance_id": binding.binding.instance_id,
        "human_principal_id": binding.binding.human_principal_id,
        "evidence_id": evidence_id,
        "decision_id": decision_id,
        "source_evidence_id": source["evidence_id"],
        "source_evidence_digest": source["evidence_digest"],
        "intake_public_id": source["intake_public_id"],
        "source_event_id": source["source_event_id"],
        "proposal_public_id": base["proposal_public_id"],
        "proposal_version": base["proposal_version"],
        "proposal_content_hash": base["effective_content_hash"],
        "review_id": review_id,
        "review_projection_hash": review_hash,
        "display_id": display_id,
        "display_evidence_digest": _digest(display),
        "reply_evidence_digest": reply_digest,
        "reply_to_display_id": display_id,
        "action": "amend",
        "patch": dict(patch),
        "patch_digest": amendment_patch_sha256(patch),
        "observed_at": NOW,
        "issued_at": NOW,
        "expires_at": NOW + 500,
        "consumed": False,
        "revoked": False,
    }
    if proof_overrides:
        proof.update(proof_overrides)
    proof["evidence_digest"] = _proof_digest(proof)
    _persist(connection, _TABLES[0], display_id, display, key=sign_with)
    _persist(connection, _TABLES[1], reply_id, reply, key=sign_with)
    _persist(connection, _TABLES[2], evidence_id, proof, key=sign_with)
    connection.commit()
    return {"evidence_id": evidence_id, "display_id": display_id, "reply_id": reply_id}


class DurableSyntheticAmendmentAuthority:
    """Verify durable, HMAC-sealed evidence against the supplied database snapshot."""

    def __init__(self, *, key: bytes = AMENDMENT_KEY) -> None:
        self._key = key
        self.last_connection: sqlite3.Connection | None = None

    def verify_persisted(self, connection, amendment_evidence_id, expected):
        from finance_core.application.amendment_contract import VerifiedHumanAmendment

        self.last_connection = connection
        proof = _load(connection, _TABLES[2], amendment_evidence_id, key=self._key)
        display = _load(connection, _TABLES[0], str(proof["display_id"]), key=self._key)
        reply = _load(
            connection,
            _TABLES[1],
            f"synthetic-amendment-reply-{amendment_evidence_id}",
            key=self._key,
        )
        if proof.get("evidence_digest") != _proof_digest(proof):
            raise ValueError("Synthetic amendment evidence digest does not match")
        expected_binding = expected.binding
        source = expected.source
        base = expected.review_projection.get("proposal_review")
        if not isinstance(base, Mapping):
            raise ValueError("Expected amendment review has no proposal revision")
        if (
            proof.get("schema") != AMENDMENT_SCHEMA
            or proof.get("namespace") != expected_binding.amendment_namespace
            or proof.get("key_id") != expected_binding.amendment_key_id
            or proof.get("instance_id") != expected_binding.binding.instance_id
            or proof.get("human_principal_id") != expected_binding.binding.human_principal_id
            or proof.get("evidence_id") != amendment_evidence_id
            or proof.get("source_evidence_id") != source.evidence_id
            or proof.get("source_evidence_digest") != source.evidence_digest
            or proof.get("intake_public_id") != source.intake_public_id
            or proof.get("source_event_id") != source.source_event_id
            or proof.get("proposal_public_id") != expected.proposal_public_id
            or proof.get("proposal_version") != expected.proposal_version
            or proof.get("proposal_content_hash") != expected.proposal_content_hash
            or proof.get("proposal_public_id") != base.get("proposal_public_id")
            or proof.get("proposal_version") != base.get("proposal_version")
            or proof.get("proposal_content_hash") != base.get("effective_content_hash")
            or proof.get("review_id") != expected.review_id
            or proof.get("review_projection_hash") != expected.review_projection_hash
            or proof.get("display_id") != display.get("display_id")
            or proof.get("display_evidence_digest") != _digest(display)
            or proof.get("reply_evidence_digest") != _digest(reply)
            or proof.get("reply_to_display_id") != display.get("display_id")
            or proof.get("action") != "amend"
            or proof.get("patch") != reply.get("patch")
            or proof.get("patch_digest") != amendment_patch_sha256(proof.get("patch", {}))
            or display.get("state") != "delivered"
            or display.get("direct") is not True
            or display.get("private") is not True
            or display.get("human_principal_id") != expected_binding.binding.human_principal_id
            or display.get("instance_id") != expected_binding.binding.instance_id
            or display.get("conversation_id") != expected_binding.binding.submission_client_id
            or display.get("source_evidence_id") != source.evidence_id
            or display.get("source_evidence_digest") != source.evidence_digest
            or display.get("review_id") != expected.review_id
            or display.get("review_projection_hash") != expected.review_projection_hash
            or display.get("projection") != expected.review_projection
            or reply.get("schema") != display.get("schema")
            or reply.get("direct") is not True
            or reply.get("private") is not True
            or reply.get("human_principal_id") != expected_binding.binding.human_principal_id
            or reply.get("instance_id") != expected_binding.binding.instance_id
            or reply.get("conversation_id") != expected_binding.binding.submission_client_id
            or reply.get("review_id") != expected.review_id
            or reply.get("review_projection_hash") != expected.review_projection_hash
            or reply.get("display_id") != display.get("display_id")
            or reply.get("display_evidence_digest") != _digest(display)
            or reply.get("reply_to_display_id") != display.get("display_id")
            or reply.get("decision_id") != proof.get("decision_id")
            or reply.get("action") != "amend"
        ):
            raise ValueError("Signed synthetic amendment does not match current review authority")
        accepted_replay = expected.accepted_amendment_id is not None
        if accepted_replay:
            accepted = connection.execute(
                "SELECT evidence_id FROM application_amendment_records WHERE amendment_id=?",
                (expected.accepted_amendment_id,),
            ).fetchone()
            if accepted is None or accepted[0] != amendment_evidence_id:
                raise ValueError("Historical amendment context names different accepted evidence")
        if not accepted_replay and (
            proof.get("consumed") is not False
            or proof.get("revoked") is not False
            or not (proof.get("issued_at") <= expected.checked_at < proof.get("expires_at"))
        ):
            raise ValueError("Fresh amendment evidence is expired, consumed, or revoked")
        return VerifiedHumanAmendment(**proof)


def read_signed_material(connection, record: str, record_id: str) -> dict[str, object]:
    """Test-only accessor for composing a second valid chained amendment."""
    table = {
        "display": _TABLES[0],
        "reply": _TABLES[1],
        "evidence": _TABLES[2],
    }[record]
    return _load(connection, table, record_id)


def replace_signed_material(
    connection, record: str, record_id: str, material: Mapping[str, object]
) -> None:
    """Replace signed synthetic evidence for an authenticated adversarial case."""
    table = {
        "display": _TABLES[0],
        "reply": _TABLES[1],
        "evidence": _TABLES[2],
    }[record]
    connection.execute(
        f"UPDATE {table} SET material=?,signature=? WHERE id=?",
        (_canonical(dict(material)), _signature(table, material), record_id),
    )
    connection.commit()


def tamper_signed_material(connection, record: str, record_id: str) -> None:
    """Change signed material without updating the HMAC, simulating lost integrity."""
    table = {
        "display": _TABLES[0],
        "reply": _TABLES[1],
        "evidence": _TABLES[2],
    }[record]
    row = connection.execute(f"SELECT material FROM {table} WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise AssertionError(f"Missing synthetic signed material: {record}/{record_id}")
    material = json.loads(str(row[0]))
    if not isinstance(material, dict):
        raise AssertionError("Synthetic signed material must be an object")
    material["tampered_after_signing"] = True
    connection.execute(
        f"UPDATE {table} SET material=? WHERE id=?", (_canonical(material), record_id)
    )
    connection.commit()
