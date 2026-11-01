"""Proof-driven append-only lifecycle, scoped to an actual production owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from finance_core.instance_admission import ProductionOwner
from finance_core.instance_admission.sessions import (
    lifecycle_read_operation,
    lifecycle_write_operation,
)
from finance_core.money import Money, canonical_money_str
from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

from .contracts import (
    CommitReceipt,
    EvidencePort,
    ExpectedAction,
    ExpectedDisplay,
    ExpectedInvocation,
    LifecycleError,
    ProposalRef,
    Review,
    RuntimeBinding,
    SnapshotCut,
    SourceRef,
    canonical,
    digest,
)


def _id(prefix):
    return prefix + "-" + uuid4().hex


def _row(conn, table, column, value):
    row = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()
    if row is None:
        raise LifecycleError(f"Missing {table} identity")
    return row


def _event(conn, operation_id, kind, entity_id, payload):
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 256:
        raise LifecycleError("Exact operation identity required")
    last = conn.execute(
        "SELECT seq,event_sha256 FROM l2_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    seq, previous = (last[0] + 1, last[1]) if last else (1, None)
    body = [seq, operation_id, kind, entity_id, payload, previous]
    conn.execute(
        "INSERT INTO l2_events VALUES(?,?,?,?,?,?,?)",
        (seq, operation_id, kind, entity_id, canonical(payload), previous, digest(body)),
    )
    return seq


def _fact_material(record_id, version, economic_event_id, source_id, values, predecessor):
    return [record_id, version, economic_event_id, source_id, values, predecessor]


def _money(values):
    amount = values.get("amount")
    currency = values.get("currency")
    if not isinstance(amount, str) or not isinstance(currency, str):
        raise LifecycleError("Canonical monetary strings required")
    money = Money.from_string(amount, currency)
    if money.amount <= 0 or canonical_money_str(money.amount, currency) != amount:
        raise LifecycleError("Canonical positive expense required")
    return values


def _snapshot_inventory(conn):
    facts = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT record_id,version,fact_sha256 FROM l2_facts f WHERE version="
            "(SELECT max(version) FROM l2_facts WHERE record_id=f.record_id) "
            "ORDER BY record_id"
        )
    )
    seq = conn.execute("SELECT coalesce(max(seq),0) FROM l2_events").fetchone()[0]
    return facts, seq


def _snapshot_integrity(conn):
    if [tuple(row) for row in conn.execute("PRAGMA integrity_check")] != [("ok",)] or conn.execute(
        "PRAGMA foreign_key_check"
    ).fetchone() is not None:
        raise LifecycleError("Snapshot SQLite integrity or foreign keys invalid")


def _capture_generation(row, payload_json):
    """Verify retained native capture material; old plain captures have no generation proof."""
    payload = json.loads(payload_json)
    if payload_json != canonical(payload):
        raise LifecycleError("Capture event must retain exact canonical JSON")
    source = {
        "evidence_id": row["evidence_id"],
        "principal_id": row["principal_id"],
        "client_id": row["client_id"],
        "source_key": row["source_key"],
        "raw_input": row["raw_input"],
        "original_sha256": row["original_sha256"],
        "source_timestamp": row["created_at"],
        "timezone": "Asia/Singapore",
    }
    if row["source_sha256"] != digest(source):
        raise LifecycleError("Capture event inventory lacks its exact source")
    if canonical(payload) == canonical(source):
        # Historical capture bytes stay readable, but cannot prove initial-write authority.
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "generation", "source"}
        or payload["format"] != "lifecycle-capture-v2"
        or not isinstance(payload["generation"], str)
        or not 1 <= len(payload["generation"]) <= 256
        or payload["generation"] != row["generation"]
        or canonical(payload["source"]) != canonical(source)
    ):
        raise LifecycleError("Original capture generation or source binding changed")
    return payload["generation"]


class LedgerService:
    def __init__(
        self,
        owner: ProductionOwner,
        port: EvidencePort,
        binding: RuntimeBinding,
        clock=time.time,
        *,
        legacy_verifier=None,
    ):
        if (
            not isinstance(owner, ProductionOwner)
            or owner.config.instance_id != binding.instance_id
        ):
            raise LifecycleError("Actual matching production owner required")
        owner.assert_valid()
        self.owner, self.port, self.binding, self.clock = owner, port, binding, clock
        self.legacy_verifier = legacy_verifier

    def _now(self):
        return int(self.clock())

    def _actor(self, evidence):
        if (
            evidence.principal_id != self.binding.principal_id
            or getattr(evidence, "client_id", self.binding.client_id) != self.binding.client_id
        ):
            raise LifecycleError("Evidence actor/client mismatch")

    def _source(self, conn, source_id, *, allow_archived=False):
        row = _row(conn, "l2_sources", "source_id", source_id)
        if row["source_key"].startswith("legacy:"):
            from .legacy import verify_imported_source

            verify_imported_source(conn, row, self.binding, self.legacy_verifier)
            if not allow_archived and row["generation"] != self.binding.generation:
                raise LifecycleError("Archived imported source has no current write authority")
            return row
        evidence = self.port.source(conn, row["evidence_id"])
        self._actor(evidence)
        material = asdict(evidence)
        if (
            row["source_sha256"] != digest(material)
            or row["raw_input"] != evidence.raw_input
            or row["source_key"] != evidence.source_key
            or row["principal_id"] != evidence.principal_id
            or row["client_id"] != evidence.client_id
            or row["original_sha256"] != evidence.original_sha256
            or row["created_at"] != evidence.source_timestamp
            or evidence.timezone != "Asia/Singapore"
            or evidence.evidence_id != row["evidence_id"]
            or hashlib.sha256(evidence.raw_input.encode()).hexdigest() != evidence.original_sha256
        ):
            raise LifecycleError("Source/original evidence changed")
        captures = conn.execute(
            "SELECT payload_json FROM l2_events WHERE kind='capture' AND entity_id=?", (source_id,)
        ).fetchall()
        if len(captures) != 1:
            raise LifecycleError("Source lacks its exact original capture event")
        generation = _capture_generation(row, captures[0][0])
        if not allow_archived:
            if generation is None:
                raise LifecycleError("Historical capture lacks original generation authority")
            if generation != self.binding.generation:
                raise LifecycleError("Source belongs to archived generation")
        return row

    def capture(self, source_evidence_id, operation_id):
        with lifecycle_write_operation(self.owner) as conn:
            evidence = self.port.source(conn, source_evidence_id)
            self._actor(evidence)
            if (
                evidence.evidence_id != source_evidence_id
                or not evidence.raw_input
                or evidence.source_key.startswith("legacy:")
                or type(evidence.source_timestamp) is not int
                or evidence.source_timestamp <= 0
                or evidence.timezone != "Asia/Singapore"
            ):
                raise LifecycleError("Exact nonempty source evidence required")
            if hashlib.sha256(evidence.raw_input.encode()).hexdigest() != evidence.original_sha256:
                raise LifecycleError("Original text hash mismatch")
            sha = digest(asdict(evidence))
            existing = conn.execute(
                "SELECT * FROM l2_sources WHERE evidence_id=? OR source_key=?",
                (source_evidence_id, evidence.source_key),
            ).fetchone()
            if existing:
                event = conn.execute(
                    "SELECT * FROM l2_events WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if (
                    event is None
                    or event["kind"] != "capture"
                    or event["entity_id"] != existing["source_id"]
                    or existing["source_sha256"] != sha
                ):
                    raise LifecycleError(
                        "Duplicate source requires exact original operation replay"
                    )
                self._source(conn, existing["source_id"], allow_archived=True)
                return SourceRef(existing["source_id"], sha)
            source_id = _id("source")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO l2_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    evidence.source_key,
                    evidence.evidence_id,
                    evidence.principal_id,
                    evidence.client_id,
                    evidence.raw_input,
                    evidence.original_sha256,
                    sha,
                    self.binding.generation,
                    evidence.source_timestamp,
                ),
            )
            _event(
                conn,
                operation_id,
                "capture",
                source_id,
                {
                    "format": "lifecycle-capture-v2",
                    "generation": self.binding.generation,
                    "source": asdict(evidence),
                },
            )
            conn.commit()
            return SourceRef(source_id, sha)

    def _projection(self, source, current, version, record_id):
        now = datetime.fromtimestamp(source["created_at"], ZoneInfo("Asia/Singapore"))
        return {
            "interaction_kind": "initial" if version == 0 and record_id is None else "edit",
            "source_text": source["raw_input"],
            "reply_text": "",
            "current_values": current,
            "interpretation_context": {
                "anchor_timestamp": source["created_at"],
                "local_date": now.date().isoformat(),
                "timezone": "Asia/Singapore",
                "default_currency": "SGD",
            },
            "selection": {"kind": "text"},
            "unresolved": [],
        }

    def expected_invocation(
        self,
        source_id,
        expected_version,
        record_id=None,
        reply_evidence_id=None,
        correction_reason=None,
    ):
        """Build exact request context before provider dispatch; no authority is granted."""
        with lifecycle_read_operation(self.owner) as conn:
            return self._expected(
                conn, source_id, expected_version, record_id, reply_evidence_id, correction_reason
            )

    def _expected(
        self,
        conn,
        source_id,
        expected_version,
        record_id=None,
        reply_evidence_id=None,
        correction_reason=None,
    ):
        source = self._source(conn, source_id, allow_archived=record_id is not None)
        current = {}
        if record_id is not None:
            from .reader import verify_record

            verify_record(self, conn, record_id)
            fact = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if (
                fact is None
                or fact["version"] != expected_version
                or fact["source_id"] != source_id
            ):
                raise LifecycleError("Correction target/version mismatch")
            current = json.loads(fact["values_json"])
        else:
            prior = conn.execute(
                "SELECT * FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            actual = prior["version"] if prior else 0
            if actual != expected_version:
                raise LifecycleError("Concurrent proposal version changed")
            if prior:
                from .reader import verify_proposal

                verify_proposal(self, conn, prior)
            current = json.loads(prior["values_json"]) if prior else {}
            if conn.execute("SELECT 1 FROM l2_facts WHERE source_id=?", (source_id,)).fetchone():
                raise LifecycleError("Posted source requires correction")
        prior_proposal = conn.execute(
            "SELECT proposal_id FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        prior_review = (
            conn.execute(
                "SELECT review_id FROM l2_reviews WHERE proposal_id=?", (prior_proposal[0],)
            ).fetchone()
            if prior_proposal
            else None
        )
        projection = self._projection(source, current, expected_version, record_id)
        if projection["interaction_kind"] != "initial":
            if reply_evidence_id is None:
                raise LifecycleError("Edit requires authenticated reply source")
            reply = self.port.source(conn, reply_evidence_id)
            self._actor(reply)
            if (
                reply.evidence_id != reply_evidence_id
                or type(reply.source_timestamp) is not int
                or reply.source_timestamp <= 0
                or reply.timezone != "Asia/Singapore"
                or hashlib.sha256(reply.raw_input.encode()).hexdigest() != reply.original_sha256
            ):
                raise LifecycleError("Reply source mismatch")
            projection["reply_text"] = reply.raw_input
            projection["interpretation_context"]["anchor_timestamp"] = reply.source_timestamp
            projection["interpretation_context"]["local_date"] = (
                datetime.fromtimestamp(reply.source_timestamp, ZoneInfo("Asia/Singapore"))
                .date()
                .isoformat()
            )
            if record_id is not None:
                if (
                    not isinstance(correction_reason, str)
                    or not correction_reason.strip()
                    or correction_reason != correction_reason.strip()
                    or len(correction_reason) > 500
                    or not any(
                        label + correction_reason in reply.raw_input
                        for label in ("原因：", "原因: ", "reason: ")
                    )
                ):
                    raise LifecycleError("Correction requires an explicit authenticated reason")
            elif correction_reason is not None:
                raise LifecycleError("Ordinary edits cannot carry correction authority")
        elif reply_evidence_id is not None:
            raise LifecycleError("Initial proposal cannot smuggle edit reply")
        return ExpectedInvocation(
            self.binding,
            source_id,
            source["source_sha256"],
            expected_version,
            projection,
            record_id,
            reply_evidence_id,
            prior_proposal[0] if prior_proposal else None,
            prior_review[0] if prior_review else None,
            correction_reason,
            fact["fact_sha256"] if record_id is not None else None,
            fact["economic_event_id"] if record_id is not None else None,
        )

    def _propose(
        self,
        conn,
        source_id,
        observation_id,
        expected_version,
        operation_id,
        record_id=None,
        reply_evidence_id=None,
        correction_reason=None,
    ):
        existing = conn.execute(
            "SELECT * FROM l2_proposals WHERE observation_id=?", (observation_id,)
        ).fetchone()
        if existing:
            from .reader import verify_proposal

            event = conn.execute(
                "SELECT * FROM l2_events WHERE operation_id=?", (operation_id,)
            ).fetchone()
            retained = verify_proposal(self, conn, existing)
            if (
                event is None
                or event["kind"] != "propose"
                or event["entity_id"] != existing["proposal_id"]
                or existing["source_id"] != source_id
                or existing["expected_version"] != expected_version
                or existing["record_id"] != record_id
                or retained.binding != self.binding
                or retained.source_id != source_id
                or retained.expected_version != expected_version
                or retained.record_id != record_id
                or retained.reply_evidence_id != reply_evidence_id
                or retained.correction_reason != correction_reason
            ):
                raise LifecycleError("Observation replay mismatch")
            return ProposalRef(
                existing["proposal_id"], source_id, existing["version"], existing["proposal_sha256"]
            )
        expected = self._expected(
            conn, source_id, expected_version, record_id, reply_evidence_id, correction_reason
        )
        observation = self.port.provider(conn, observation_id, expected)
        self._actor(observation)
        if (
            observation.evidence_id != observation_id
            or observation.source_id != source_id
            or observation.request_sha256 != digest(asdict(expected))
        ):
            raise LifecycleError("Provider request/observation binding mismatch")
        if observation.reply_text != expected.projection["reply_text"]:
            raise LifecycleError("Provider reply text mismatch")
        projection = expected.projection
        result = validate_response_v2(
            observation.raw_response, projection, projection["current_values"]
        )
        if result["state"] != "VALID":
            raise LifecycleError("Provider proposal is not complete: " + str(result.get("reason")))
        values = _money(result["payload"])
        latest = conn.execute(
            "SELECT max(version) FROM l2_proposals WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        version = (latest or 0) + 1
        proposal_id = _id("proposal")
        material = [source_id, version, asdict(observation), values, expected_version, record_id]
        sha = digest(material)
        conn.execute(
            "INSERT INTO l2_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                source_id,
                version,
                observation_id,
                expected_version,
                record_id,
                canonical(values),
                canonical({"observation": asdict(observation), "expected": asdict(expected)}),
                sha,
                self._now(),
            ),
        )
        _event(conn, operation_id, "propose", proposal_id, material)
        return ProposalRef(proposal_id, source_id, version, sha)

    def propose(
        self, source_id, observation_id, expected_version, operation_id, reply_evidence_id=None
    ):
        with lifecycle_write_operation(self.owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = self._propose(
                conn,
                source_id,
                observation_id,
                expected_version,
                operation_id,
                reply_evidence_id=reply_evidence_id,
            )
            conn.commit()
            return result

    def _review(self, conn, proposal_id, expected_version):
        proposal = _row(conn, "l2_proposals", "proposal_id", proposal_id)
        from .reader import verify_proposal

        verify_proposal(self, conn, proposal)
        self._source(conn, proposal["source_id"], allow_archived=proposal["record_id"] is not None)
        if proposal["version"] != expected_version:
            raise LifecycleError("Review proposal version mismatch")
        existing = conn.execute(
            "SELECT * FROM l2_reviews WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if existing:
            from .reader import verify_review

            verify_review(self, conn, existing)
            return self._review_dto(conn, existing)
        latest = conn.execute(
            "SELECT max(version) FROM l2_proposals WHERE source_id=?", (proposal["source_id"],)
        ).fetchone()[0]
        if latest != expected_version:
            raise LifecycleError("Superseded proposal cannot be reviewed")
        review_id = _id("review")
        material = {
            "binding": asdict(self.binding),
            "review_id": review_id,
            "proposal_id": proposal_id,
            "proposal_sha256": proposal["proposal_sha256"],
            "expected_version": proposal["expected_version"] if proposal["record_id"] else 0,
            "record_id": proposal["record_id"],
            "values": json.loads(proposal["values_json"]),
            "created_at": self._now(),
            "expires_at": self._now() + 600,
            "before_values": json.loads(proposal["observation_json"])["expected"]["projection"][
                "current_values"
            ]
            if proposal["record_id"]
            else None,
            "correction_reason": json.loads(proposal["observation_json"])["expected"][
                "correction_reason"
            ],
        }
        sha = digest(material)
        conn.execute(
            "INSERT INTO l2_reviews VALUES(?,?,?,?,?,?,?,?)",
            (
                review_id,
                proposal_id,
                material["expected_version"],
                material["record_id"],
                proposal["values_json"],
                canonical(material),
                sha,
                self._now(),
            ),
        )
        _event(conn, "prepare-" + review_id, "review", review_id, material)
        return Review(
            review_id,
            proposal_id,
            material["expected_version"],
            material["record_id"],
            material["values"],
            sha,
            before_values=material["before_values"],
            correction_reason=material["correction_reason"],
        )

    def _review_dto(self, conn, row):
        delivery = conn.execute(
            "SELECT evidence_id FROM l2_deliveries WHERE review_id=?", (row["review_id"],)
        ).fetchone()
        return Review(
            row["review_id"],
            row["proposal_id"],
            row["expected_version"],
            row["record_id"],
            json.loads(row["values_json"]),
            row["review_sha256"],
            delivery[0] if delivery else None,
            json.loads(row["material_json"])["before_values"],
            json.loads(row["material_json"])["correction_reason"],
        )

    def prepare_review(self, proposal_id, expected_version):
        with lifecycle_write_operation(self.owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = self._review(conn, proposal_id, expected_version)
            conn.commit()
            return result

    def record_delivery(self, review_id, delivery_evidence_id):
        with lifecycle_write_operation(self.owner) as conn:
            row = _row(conn, "l2_reviews", "review_id", review_id)
            expected = ExpectedDisplay(self.binding, review_id, row["review_sha256"])
            delivery = self.port.delivery(conn, delivery_evidence_id, expected)
            self._actor(delivery)
            if (
                delivery.evidence_id != delivery_evidence_id
                or delivery.review_id != review_id
                or delivery.display_sha256 != expected.review_sha256
            ):
                raise LifecycleError("Delivered display mismatch")
            existing = conn.execute(
                "SELECT * FROM l2_deliveries WHERE review_id=?", (review_id,)
            ).fetchone()
            if existing:
                if existing["evidence_id"] != delivery_evidence_id or existing[
                    "material_json"
                ] != canonical(asdict(delivery)):
                    raise LifecycleError("Delivery replay mismatch")
                return self._review_dto(conn, row)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO l2_deliveries VALUES(?,?,?)",
                (review_id, delivery_evidence_id, canonical(asdict(delivery))),
            )
            _event(
                conn, "delivery-" + delivery_evidence_id, "delivery", review_id, asdict(delivery)
            )
            conn.commit()
            return self._review_dto(conn, row)

    def confirm(self, review_id, action_evidence_id, operation_id):
        with lifecycle_write_operation(self.owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM l2_decisions WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                if (
                    existing["action_evidence_id"] != action_evidence_id
                    or existing["review_id"] != review_id
                ):
                    raise LifecycleError("Commit replay mismatch")
                original_review = _row(conn, "l2_reviews", "review_id", review_id)
                if json.loads(original_review["material_json"])["binding"] != asdict(self.binding):
                    raise LifecycleError("Committed replay belongs to another runtime binding")
                fact = conn.execute(
                    "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
                    (existing["record_id"], existing["version"]),
                ).fetchone()
                if fact is None:
                    raise LifecycleError("Committed replay fact is absent")
                from .reader import verify_record

                verify_record(self, conn, existing["record_id"])
                # verify_record authenticates the exact original provider, display,
                # action, decision and commit. This observes an existing result;
                # current expiry/latest-card gates apply only to fresh decisions.
                return CommitReceipt(
                    fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
                )
            row = _row(conn, "l2_reviews", "review_id", review_id)
            from .reader import verify_review

            verify_review(self, conn, row)
            material = json.loads(row["material_json"])
            accepted_at = self._now()
            if (
                material["binding"] != asdict(self.binding)
                or digest(material) != row["review_sha256"]
                or not material["created_at"] <= accepted_at < material["expires_at"]
            ):
                raise LifecycleError("Review belongs to another binding or was altered")
            proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
            source = self._source(
                conn, proposal["source_id"], allow_archived=row["record_id"] is not None
            )
            latest = conn.execute(
                "SELECT max(version) FROM l2_proposals WHERE source_id=?", (source["source_id"],)
            ).fetchone()[0]
            if latest != proposal["version"]:
                raise LifecycleError("Stale review after concurrent edit")
            delivery = _row(conn, "l2_deliveries", "review_id", review_id)
            expected = ExpectedAction(
                self.binding, review_id, row["review_sha256"], delivery["evidence_id"]
            )
            action = self.port.action(conn, action_evidence_id, expected)
            self._actor(action)
            if (
                action.evidence_id != action_evidence_id
                or action.review_id != review_id
                or action.review_sha256 != expected.review_sha256
                or action.delivery_evidence_id != expected.delivery_evidence_id
                or action.action != "confirm"
            ):
                raise LifecycleError("Confirmation action mismatch")
            record_id = row["record_id"] or _id("record")
            predecessor = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            version = predecessor["version"] if predecessor else 0
            if version != row["expected_version"]:
                raise LifecycleError("Correction target changed")
            if (
                predecessor is None
                and conn.execute(
                    "SELECT 1 FROM l2_facts WHERE source_id=?", (source["source_id"],)
                ).fetchone()
            ):
                raise LifecycleError("Economic source already posted")
            economic = predecessor["economic_event_id"] if predecessor else source["source_key"]
            values = _money(json.loads(row["values_json"]))
            previous = predecessor["fact_sha256"] if predecessor else None
            sha = digest(
                _fact_material(
                    record_id, version + 1, economic, source["source_id"], values, previous
                )
            )
            seq = _event(
                conn,
                operation_id,
                "confirm",
                record_id,
                {
                    "review_id": review_id,
                    "action": asdict(action),
                    "fact_sha256": sha,
                    "version": version + 1,
                    "accepted_at": accepted_at,
                },
            )
            conn.execute(
                "INSERT INTO l2_facts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    version + 1,
                    economic,
                    source["source_id"],
                    review_id,
                    canonical(values),
                    previous,
                    sha,
                    seq,
                ),
            )
            conn.execute(
                "INSERT INTO l2_decisions VALUES(?,?,?,?,?,?)",
                (
                    action_evidence_id,
                    review_id,
                    operation_id,
                    record_id,
                    version + 1,
                    canonical(asdict(action)),
                ),
            )
            conn.commit()
            return CommitReceipt(record_id, version + 1, sha, seq)

    def prepare_correction(
        self,
        record_id,
        expected_version,
        observation_id,
        operation_id,
        reply_evidence_id=None,
        reason=None,
    ):
        with lifecycle_write_operation(self.owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            fact = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if fact is None:
                raise LifecycleError("Unknown correction target")
            proposal = self._propose(
                conn,
                fact["source_id"],
                observation_id,
                expected_version,
                operation_id,
                record_id,
                reply_evidence_id,
                reason,
            )
            review = self._review(conn, proposal.proposal_id, proposal.version)
            conn.commit()
            return review

    def reader(self):
        from .reader import LedgerReader

        return LedgerReader(self)

    def import_verified(self, export):
        from .legacy import import_into_v2

        with lifecycle_write_operation(self.owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = import_into_v2(conn, export, self.binding, self.legacy_verifier)
            conn.commit()
            return result

    def seal_snapshot(self, destination: Path):
        from finance_core.instance_admission.authority import _logical_database_digest
        from finance_core.instance_admission.schema_contract import (
            ledger_sha256,
            validate_application_database,
        )

        from .reader import audit_evidence_connection
        from .snapshot_io import _secure_snapshot_destination

        destination = Path(destination)
        migration = self.owner.config._binding["migration_identity"]
        schema = self.owner.config.application_schema
        with _secure_snapshot_destination(destination) as target:
            with lifecycle_read_operation(self.owner) as conn:
                conn.execute("BEGIN")
                audit_evidence_connection(conn, self.port, self.binding, self.legacy_verifier)
                sealed_ledger = ledger_sha256(conn)
                validate_application_database(conn, migration, schema, sealed_ledger)
                source_logical = _logical_database_digest(conn)
                facts, seq = _snapshot_inventory(conn)
                target.backup(conn)
                conn.rollback()
            target.check()
            readback = sqlite3.connect(
                destination.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0
            )
            try:
                readback.execute("PRAGMA query_only=ON")
                validate_application_database(readback, migration, schema, sealed_ledger)
                _snapshot_integrity(readback)
                if _logical_database_digest(readback) != source_logical or _snapshot_inventory(
                    readback
                ) != (facts, seq):
                    raise LifecycleError("Snapshot copy differs from source transaction")
            finally:
                readback.close()
            target.check()
            database_sha256 = target.closed_sha256()
        return SnapshotCut(
            _id("snapshot"),
            self.binding.instance_id,
            self.binding.generation,
            seq,
            facts,
            database_sha256,
            self.owner.config.core_identity,
            self.owner.config._binding["migration_identity"],
            digest(self.owner.config._binding["application_schema"]),
            logical_database_sha256=source_logical,
            source_binding_sha256=self.owner.config.binding_sha256,
        )
