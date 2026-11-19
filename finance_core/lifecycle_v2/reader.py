"""Indexed current facts and complete append-only audit; no legacy status traversal."""

from __future__ import annotations

import hashlib
import json

from finance_core.instance_admission.sessions import lifecycle_read_operation

from .contracts import (
    DailyCapacityV2,
    LifecycleError,
    VerifiedSource,
    canonical,
    digest,
    validate_daily_capacity,
)
from .service import _capture_generation, _fact_material


def _registered_values(value):
    from .daily_v1.accounts import registered_values

    return registered_values(value)


def _old_record(values):
    if "format" in values:
        raise LifecycleError("unsupported_record_format: use the explicit daily reader")


def audit_connection(conn, *, _rows=None):
    previous = None
    expected_seq = 1
    for row in (
        _rows.table(conn, "events")
        if _rows is not None
        else conn.execute("SELECT * FROM l2_events ORDER BY seq")
    ):
        payload = json.loads(row["payload_json"])
        if (
            row["seq"] != expected_seq
            or row["previous_sha256"] != previous
            or row["event_sha256"]
            != digest(
                [row["seq"], row["operation_id"], row["kind"], row["entity_id"], payload, previous]
            )
        ):
            raise LifecycleError("Audit event chain invalid")
        previous = row["event_sha256"]
        expected_seq += 1
    audit_owned_inventory(conn, _rows=_rows)
    count = 0
    for row in (
        _rows.table(conn, "facts")
        if _rows is not None
        else conn.execute("SELECT * FROM l2_facts ORDER BY record_id,version")
    ):
        values = _registered_values(json.loads(row["values_json"]))
        expected = digest(
            _fact_material(
                row["record_id"],
                row["version"],
                row["economic_event_id"],
                row["source_id"],
                values,
                row["predecessor_sha256"],
            )
        )
        if expected != row["fact_sha256"]:
            raise LifecycleError("Fact material hash invalid")
        if row["version"] > 1:
            prior = (
                _rows.project(
                    conn, "fact_version", ("fact_sha256",), row["record_id"], row["version"] - 1
                )
                if _rows is not None
                else conn.execute(
                    "SELECT fact_sha256 FROM l2_facts WHERE record_id=? AND version=?",
                    (row["record_id"], row["version"] - 1),
                ).fetchone()
            )
            if prior is None or prior[0] != row["predecessor_sha256"]:
                raise LifecycleError("Fact predecessor invalid")
        event = (
            _rows.project(conn, "event", ("kind", "payload_json"), row["commit_seq"])
            if _rows is not None
            else conn.execute(
                "SELECT kind,payload_json FROM l2_events WHERE seq=?", (row["commit_seq"],)
            ).fetchone()
        )
        if event is None or event[0] not in {"confirm", "import"}:
            raise LifecycleError("Fact lacks audit commit")
        if event[0] == "confirm":
            if json.loads(event[1])["fact_sha256"] != expected:
                raise LifecycleError("Fact audit binding mismatch")
            decision = (
                _rows.one(conn, "decision_version", row["record_id"], row["version"])
                if _rows is not None
                else conn.execute(
                    "SELECT * FROM l2_decisions WHERE record_id=? AND version=?",
                    (row["record_id"], row["version"]),
                ).fetchone()
            )
            if decision is None or decision["review_id"] != row["review_id"]:
                raise LifecycleError("Fact lacks exact decision")
        count += 1
    for row in (
        _rows.table(conn, "reviews")
        if _rows is not None
        else conn.execute("SELECT * FROM l2_reviews")
    ):
        material = json.loads(row["material_json"])
        if "format" in material and material["format"] not in {
            "finance-daily-review-v1",
            "finance-daily-movement-review-v1",
            "finance-daily-movement-review-v2",
            "finance-daily-opening-review-v2",
        }:
            raise LifecycleError("Unsupported review format")
        values_key = (
            "after_values"
            if material.get("format")
            in {
                "finance-daily-review-v1",
                "finance-daily-movement-review-v1",
                "finance-daily-movement-review-v2",
                "finance-daily-opening-review-v2",
            }
            else "values"
        )
        if (
            digest(material) != row["review_sha256"]
            or canonical(material[values_key]) != row["values_json"]
        ):
            raise LifecycleError("Review hash invalid")
    return {"event_count": expected_seq - 1, "fact_version_count": count, "audit_sha256": previous}


class LedgerReader:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return {
            "record_id": row["record_id"],
            "version": row["version"],
            "economic_event_id": row["economic_event_id"],
            "source_id": row["source_id"],
            "values": json.loads(row["values_json"]),
            "fact_sha256": row["fact_sha256"],
            "commit_seq": row["commit_seq"],
            "status": "LOCAL_POSTED",
        }

    def get(self, record_id):
        with lifecycle_read_operation(self.service.owner) as conn:
            row = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if row:
                _old_record(json.loads(row["values_json"]))
                verify_record(self.service, conn, row["record_id"])
            else:
                audit_record_inventory(conn, record_id)
            return self._record(row)

    def source(self, source_id: str) -> VerifiedSource:
        """Read authenticated original evidence without reviving archived rights."""
        with lifecycle_read_operation(self.service.owner) as conn:
            row = self.service._source(conn, source_id, allow_archived=True)
            evidence = VerifiedSource(
                row["evidence_id"],
                row["principal_id"],
                row["client_id"],
                row["source_key"],
                row["raw_input"],
                row["original_sha256"],
                row["created_at"],
            )
            from dataclasses import asdict

            if digest(asdict(evidence)) != row["source_sha256"]:
                raise LifecycleError("Authenticated source DTO differs from retained source")
            return evidence

    def recent(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise LifecycleError("Bounded page size required")
        with lifecycle_read_operation(self.service.owner) as conn:
            rows = conn.execute(
                "SELECT * FROM l2_facts f WHERE version=(SELECT max(version) "
                "FROM l2_facts WHERE record_id=f.record_id) ORDER BY commit_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                _old_record(json.loads(row["values_json"]))
                verify_record(self.service, conn, row["record_id"])
            return [self._record(row) for row in rows]

    def pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise LifecycleError("Bounded page size required")
        with lifecycle_read_operation(self.service.owner) as conn:
            rows = conn.execute(
                "SELECT r.* FROM l2_reviews r JOIN l2_proposals p ON p.proposal_id=r.proposal_id "
                "WHERE NOT EXISTS(SELECT 1 FROM l2_decisions d WHERE d.review_id=r.review_id) "
                "AND p.version=(SELECT max(version) FROM l2_proposals WHERE source_id=p.source_id) "
                "ORDER BY r.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                _old_record(json.loads(row["values_json"]))
                verify_review(self.service, conn, row)
            return [self.service._review_dto(conn, row) for row in rows]

    def full_audit(self):
        with lifecycle_read_operation(self.service.owner) as conn:
            if _has_daily_values(conn):
                # New opt-in formats require an operation-local evidence port.
                # The historical expense-only read path keeps its contract.
                conn.execute("BEGIN")
                try:
                    return audit_evidence_connection(
                        conn,
                        self.service.port,
                        self.service.binding,
                        self.service.legacy_verifier,
                        daily_capacity=self.service.daily_capacity,
                    )
                finally:
                    conn.rollback()
            result = audit_connection(conn)
            if (
                conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
                or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
                or conn.execute(
                    "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') LIMIT 1"
                ).fetchone()
            ):
                from .legacy import verify_import_archives

                verify_import_archives(conn, self.service.legacy_verifier)
            for source in conn.execute("SELECT source_id FROM l2_sources"):
                self.service._source(conn, source[0], allow_archived=True)
            for proposal in conn.execute("SELECT * FROM l2_proposals"):
                verify_proposal(self.service, conn, proposal)
            for review in conn.execute("SELECT * FROM l2_reviews"):
                verify_review(self.service, conn, review)
            for record in conn.execute("SELECT DISTINCT record_id FROM l2_facts"):
                verify_record(self.service, conn, record[0])
            return result


def verify_proposal(service, conn, proposal, *, _rows=None):
    """Verify the actual retained provider request/output, not its DB self-hash."""
    from dataclasses import asdict

    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

    from .contracts import ExpectedInvocation, RuntimeBinding

    sealed = json.loads(proposal["observation_json"])
    if sealed.get("format") == "finance-daily-opening-observation-v2":
        from .daily_v1.opening_changes_v1 import verify_proposal as verify_opening_proposal

        return verify_opening_proposal(service, conn, proposal, _rows=_rows)
    if sealed.get("format") == "finance-daily-movement-observation-v1":
        from .daily_v1.movements import verify_proposal as verify_movement_proposal

        return verify_movement_proposal(service, conn, proposal, _rows=_rows)
    if "format" in sealed:
        if sealed["format"] != "finance-daily-proposal-observation-v1":
            raise LifecycleError("Unsupported proposal observation format")
        from .daily_v1.accounts import verify_proposal as verify_daily_proposal

        return verify_daily_proposal(service, conn, proposal, _rows=_rows)
    source = service._source(conn, proposal["source_id"], allow_archived=True, _rows=_rows)
    raw_expected = dict(sealed["expected"])
    raw_expected["binding"] = RuntimeBinding(**raw_expected["binding"])
    expected = ExpectedInvocation(**raw_expected)

    if (
        expected.source_id != source["source_id"]
        or expected.source_sha256 != source["source_sha256"]
        or expected.expected_version != proposal["expected_version"]
        or expected.record_id != proposal["record_id"]
        or expected.projection["source_text"] != source["raw_input"]
    ):
        raise LifecycleError("Proposal retained request differs from source/target")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    anchor = source["created_at"]
    if expected.reply_evidence_id is not None:
        reply = service.port.source(conn, expected.reply_evidence_id)
        if (
            reply.evidence_id != expected.reply_evidence_id
            or reply.raw_input != expected.projection["reply_text"]
            or reply.principal_id != expected.binding.principal_id
            or reply.client_id != expected.binding.client_id
            or type(reply.source_timestamp) is not int
            or reply.source_timestamp <= 0
            or reply.timezone != "Asia/Singapore"
            or hashlib.sha256(reply.raw_input.encode()).hexdigest() != reply.original_sha256
        ):
            raise LifecycleError("Retained reply source differs from actual authenticated input")
        anchor = reply.source_timestamp
    context = expected.projection["interpretation_context"]
    if (
        context["anchor_timestamp"] != anchor
        or context["timezone"] != "Asia/Singapore"
        or context["local_date"]
        != datetime.fromtimestamp(anchor, ZoneInfo("Asia/Singapore")).date().isoformat()
    ):
        raise LifecycleError("Provider interpretation context differs from actual source time")
    observation = service.port.provider(conn, proposal["observation_id"], expected)
    if (
        asdict(observation) != sealed["observation"]
        or observation.request_sha256 != digest(asdict(expected))
        or observation.source_id != source["source_id"]
        or observation.evidence_id != proposal["observation_id"]
        or observation.principal_id != expected.binding.principal_id
        or observation.reply_text != expected.projection["reply_text"]
    ):
        raise LifecycleError("Proposal differs from authenticated provider observation")
    # Reconstruct the predecessor values; stored request context cannot invent them.
    if proposal["record_id"]:
        prior = (
            _rows.one(conn, "fact_version", proposal["record_id"], proposal["expected_version"])
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
                (proposal["record_id"], proposal["expected_version"]),
            ).fetchone()
        )
    elif proposal["expected_version"]:
        prior = (
            _rows.one(conn, "proposal_version", proposal["source_id"], proposal["expected_version"])
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_proposals WHERE source_id=? AND version=?",
                (proposal["source_id"], proposal["expected_version"]),
            ).fetchone()
        )
    else:
        prior = None
    if expected.projection["current_values"] != (json.loads(prior["values_json"]) if prior else {}):
        raise LifecycleError("Proposal current values differ from actual predecessor")
    if expected.record_id is not None and (
        prior is None
        or expected.current_fact_sha256 != prior["fact_sha256"]
        or expected.current_economic_event_id != prior["economic_event_id"]
    ):
        raise LifecycleError("Correction context differs from exact committed fact identity")
    if expected.record_id is not None and expected.correction_reason is None:
        raise LifecycleError("Correction lacks original explicit reason")
    result = validate_response_v2(
        observation.raw_response, expected.projection, expected.projection["current_values"]
    )
    if result["state"] != "VALID" or canonical(result["payload"]) != proposal["values_json"]:
        raise LifecycleError("Persisted values differ from independently validated provider output")
    material = [
        proposal["source_id"],
        proposal["version"],
        asdict(observation),
        result["payload"],
        proposal["expected_version"],
        proposal["record_id"],
    ]
    events = (
        _rows.project_many(
            conn, "entity_events", ("payload_json",), "propose", proposal["proposal_id"]
        )
        if _rows is not None
        else conn.execute(
            "SELECT payload_json FROM l2_events WHERE kind='propose' AND entity_id=?",
            (proposal["proposal_id"],),
        ).fetchall()
    )
    if len(events) != 1 or events[0][0] != canonical(material):
        raise LifecycleError("Proposal lacks its exact original audit event")
    prior_proposal = (
        _rows.project(
            conn,
            "proposal_version",
            ("proposal_id",),
            proposal["source_id"],
            proposal["version"] - 1,
        )
        if _rows is not None
        else conn.execute(
            "SELECT proposal_id FROM l2_proposals WHERE source_id=? AND version=?",
            (proposal["source_id"], proposal["version"] - 1),
        ).fetchone()
    )
    if expected.current_proposal_id != (prior_proposal[0] if prior_proposal else None):
        raise LifecycleError("Proposal request has a different predecessor identity")
    if digest(material) != proposal["proposal_sha256"]:
        raise LifecycleError("Proposal hash mismatch")
    return expected


def verify_review(service, conn, review, *, _rows=None):
    material = json.loads(review["material_json"])
    if material.get("format") == "finance-daily-opening-review-v2":
        from .daily_v1.opening_changes_v1 import verify_review as verify_opening_review

        return verify_opening_review(service, conn, review, _rows=_rows)
    if material.get("format") in {
        "finance-daily-movement-review-v1",
        "finance-daily-movement-review-v2",
    }:
        from .daily_v1.movements import verify_review as verify_movement_review

        return verify_movement_review(service, conn, review, _rows=_rows)
    if "format" in material:
        if material["format"] != "finance-daily-review-v1":
            raise LifecycleError("Unsupported review format")
        from .daily_v1.accounts import verify_review as verify_daily_review

        return verify_daily_review(service, conn, review, _rows=_rows)
    from dataclasses import asdict

    from .contracts import ExpectedDisplay

    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_proposals WHERE proposal_id=?", (review["proposal_id"],)
        ).fetchone()
    )
    if proposal is None:
        raise LifecycleError("Review lacks proposal")
    expected = verify_proposal(service, conn, proposal, _rows=_rows)
    material = json.loads(review["material_json"])
    if (
        material["binding"] != asdict(expected.binding)
        or material["proposal_id"] != proposal["proposal_id"]
        or material["proposal_sha256"] != proposal["proposal_sha256"]
        or material["values"] != json.loads(proposal["values_json"])
        or material["record_id"] != proposal["record_id"]
        or material["record_id"] != review["record_id"]
        or material["correction_reason"] != expected.correction_reason
        or material["before_values"]
        != (expected.projection["current_values"] if proposal["record_id"] else None)
        or material["expected_version"] != review["expected_version"]
        or material["review_id"] != review["review_id"]
        or digest(material) != review["review_sha256"]
        or not 0 < material["expires_at"] - material["created_at"] <= 600
    ):
        raise LifecycleError("Review material differs from exact provider proposal")
    events = (
        _rows.project_many(conn, "entity_events", ("payload_json",), "review", review["review_id"])
        if _rows is not None
        else conn.execute(
            "SELECT payload_json FROM l2_events WHERE kind='review' AND entity_id=?",
            (review["review_id"],),
        ).fetchall()
    )
    if len(events) != 1 or events[0][0] != canonical(material):
        raise LifecycleError("Review lacks its exact original audit event")
    delivery = (
        _rows.one(conn, "delivery", review["review_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_deliveries WHERE review_id=?", (review["review_id"],)
        ).fetchone()
    )
    if delivery:
        actual = service.port.delivery(
            conn,
            delivery["evidence_id"],
            ExpectedDisplay(expected.binding, review["review_id"], review["review_sha256"]),
        )
        if (
            asdict(actual) != json.loads(delivery["material_json"])
            or actual.review_id != review["review_id"]
            or actual.display_sha256 != review["review_sha256"]
            or actual.principal_id != expected.binding.principal_id
            or actual.client_id != expected.binding.client_id
        ):
            raise LifecycleError("Review delivery differs from actual publication evidence")
    return expected, delivery


def verify_record(service, conn, record_id, *, _rows=None):
    from dataclasses import asdict

    from .contracts import ExpectedAction

    audit_record_inventory(conn, record_id, _rows=_rows)
    previous = None
    version = 0
    economic = None
    for fact in (
        _rows.many(conn, "record_facts", record_id)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_facts WHERE record_id=? ORDER BY version", (record_id,))
    ):
        version += 1
        values = _registered_values(json.loads(fact["values_json"]))
        if values.get("format") in {
            "finance-daily-account-bundle-v1",
            "finance-daily-account-bundle-v2",
        }:
            from .daily_v1.accounts import verify_fact

            verify_fact(service, conn, fact, _rows=_rows)
        elif values.get("format") == "finance-daily-movement-v1":
            from .daily_v1.movements import verify_fact as verify_movement_fact

            verify_movement_fact(service, conn, fact, _rows=_rows)
        if (
            fact["version"] != version
            or fact["predecessor_sha256"] != previous
            or digest(
                _fact_material(
                    record_id,
                    version,
                    fact["economic_event_id"],
                    fact["source_id"],
                    values,
                    previous,
                )
            )
            != fact["fact_sha256"]
            or (economic is not None and fact["economic_event_id"] != economic)
        ):
            raise LifecycleError("Current record/version predecessor hash mismatch")
        economic = fact["economic_event_id"]
        previous = fact["fact_sha256"]
        event = (
            _rows.one(conn, "event", fact["commit_seq"])
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_events WHERE seq=?", (fact["commit_seq"],)
            ).fetchone()
        )
        if event is None:
            raise LifecycleError("Record lacks commit audit")
        if event["kind"] == "import":
            from .legacy import verify_imported_fact

            verify_imported_fact(conn, fact, service.legacy_verifier)
            continue
        review = (
            _rows.one(conn, "review", fact["review_id"])
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_reviews WHERE review_id=?", (fact["review_id"],)
            ).fetchone()
        )
        if review is None:
            raise LifecycleError("Record lacks original review")
        expected, delivery = verify_review(service, conn, review, _rows=_rows)
        source = service._source(conn, expected.source_id, allow_archived=True, _rows=_rows)
        if fact["source_id"] != expected.source_id or (
            version == 1 and fact["economic_event_id"] != source["source_key"]
        ):
            raise LifecycleError("Economic event differs from authenticated source identity")
        decision = (
            _rows.one(conn, "decision_version", record_id, version)
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_decisions WHERE record_id=? AND version=?", (record_id, version)
            ).fetchone()
        )
        if (
            delivery is None
            or decision is None
            or decision["review_id"] != review["review_id"]
            or review["expected_version"] != version - 1
            or review["values_json"] != fact["values_json"]
            or (version > 1 and review["record_id"] != record_id)
        ):
            raise LifecycleError("Current record differs from confirmed review")
        action = service.port.action(
            conn,
            decision["action_evidence_id"],
            ExpectedAction(
                expected.binding,
                review["review_id"],
                review["review_sha256"],
                delivery["evidence_id"],
            ),
        )
        if (
            asdict(action) != json.loads(decision["material_json"])
            or action.action != "confirm"
            or action.evidence_id != decision["action_evidence_id"]
            or action.review_id != review["review_id"]
            or action.review_sha256 != review["review_sha256"]
            or action.delivery_evidence_id != delivery["evidence_id"]
            or action.principal_id != expected.binding.principal_id
            or action.client_id != expected.binding.client_id
        ):
            raise LifecycleError("Record differs from authenticated human decision")
        payload = json.loads(event["payload_json"])
        accepted_at = payload.get("accepted_at")
        review_material = json.loads(review["material_json"])
        if (
            type(accepted_at) is not int
            or not review_material["created_at"] <= accepted_at < review_material["expires_at"]
        ):
            raise LifecycleError("Recorded human decision was outside exact review expiry")
        if (
            event["kind"] != "confirm"
            or event["entity_id"] != record_id
            or event["operation_id"] != decision["operation_id"]
            or payload
            != {
                "review_id": review["review_id"],
                "action": asdict(action),
                "fact_sha256": fact["fact_sha256"],
                "version": version,
                "accepted_at": accepted_at,
            }
        ):
            raise LifecycleError("Record differs from commit audit payload")
    return version


class SnapshotEvidenceVerifier:
    """Read-only snapshot proof context; has no owner or operation capability."""

    def __init__(self, port, binding, legacy_verifier=None, *, daily_capacity=None):
        validate_daily_capacity(binding, daily_capacity)
        self.port, self.binding, self.legacy_verifier = port, binding, legacy_verifier
        self.daily_capacity = daily_capacity

    def _actor(self, evidence):
        if (
            evidence.principal_id != self.binding.principal_id
            or getattr(evidence, "client_id", self.binding.client_id) != self.binding.client_id
        ):
            raise LifecycleError("Snapshot source actor/client differs from admitted principal")

    def _source(self, conn, source_id, *, allow_archived=True, _rows=None):
        from .service import LedgerService

        return LedgerService._source(
            self, conn, source_id, allow_archived=allow_archived, _rows=_rows
        )


def _has_daily_values(conn):
    from .daily_v1.contracts import BUNDLE_FORMAT, INCOMPLETE_FORMAT

    return any(
        json.loads(row[0]).get("format")
        in {
            BUNDLE_FORMAT,
            "finance-daily-account-bundle-v2",
            "finance-daily-opening-incomplete-v2",
            INCOMPLETE_FORMAT,
            "finance-daily-movement-v1",
            "finance-daily-movement-incomplete-v1",
        }
        for row in conn.execute("SELECT values_json FROM l2_proposals")
    )


def audit_evidence_connection(
    conn, port, binding, legacy_verifier=None, *, daily_capacity: DailyCapacityV2 | None = None
):
    """Authenticate every signed source/provider/display/action and dormant archive."""
    from finance_core.instance_admission.sessions import _witness_for

    from ._audit_rows_v1 import audit_rows

    if _witness_for(conn) is not None and _has_daily_values(conn):
        from .daily_v1.accounts import _EvidenceReadPort

        if type(port) is not _EvidenceReadPort:
            port = _EvidenceReadPort(port)
    # Raw immutable snapshot readback retains its independent SQL boundary.
    # An exact adapter alone is not admission; the selector proves its context.
    if daily_capacity is not None:
        from .daily_v1.accounts import _limits

        validate_daily_capacity(binding, daily_capacity)
        _limits(conn, daily_capacity=daily_capacity)
    with audit_rows(conn, port) as rows:
        return _audit_evidence_connection(
            conn, port, binding, legacy_verifier, _rows=rows, daily_capacity=daily_capacity
        )


def _audit_evidence_connection(
    conn, port, binding, legacy_verifier=None, *, _rows=None, daily_capacity=None
):
    """Original SQL reference with optional private, audit-local raw lookups."""
    result = audit_connection(conn, _rows=_rows)
    verifier = SnapshotEvidenceVerifier(
        port, binding, legacy_verifier, daily_capacity=daily_capacity
    )
    if (
        conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
        or conn.execute(
            "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') LIMIT 1"
        ).fetchone()
    ):
        from .legacy import verify_import_archives

        verify_import_archives(conn, legacy_verifier)
    sources = (
        ((identity,) for identity in _rows.vector(conn, "sources"))
        if _rows is not None
        else conn.execute("SELECT source_id FROM l2_sources")
    )
    for source in sources:
        verifier._source(conn, source[0], allow_archived=True, _rows=_rows)
    proposals = (
        _rows.table(conn, "proposals")
        if _rows is not None
        else conn.execute("SELECT * FROM l2_proposals")
    )
    for proposal in proposals:
        verify_proposal(verifier, conn, proposal, _rows=_rows)
    reviews = (
        _rows.table(conn, "reviews")
        if _rows is not None
        else conn.execute("SELECT * FROM l2_reviews")
    )
    for review in reviews:
        verify_review(verifier, conn, review, _rows=_rows)
    records = (
        ((identity,) for identity in _rows.vector(conn, "records"))
        if _rows is not None
        else conn.execute("SELECT DISTINCT record_id FROM l2_facts")
    )
    for record in records:
        verify_record(verifier, conn, record[0], _rows=_rows)
    return result


def _exact_confirm_inventory(conn, event, *, _rows=None):
    """Resolve a retained commit in both directions without assuming fact existence."""
    payload = json.loads(event["payload_json"])
    if (
        set(payload) != {"review_id", "action", "fact_sha256", "version", "accepted_at"}
        or type(payload["version"]) is not int
        or payload["version"] < 1
        or type(payload["accepted_at"]) is not int
    ):
        raise LifecycleError("Confirm event inventory payload invalid")
    facts = (
        _rows.many(conn, "commit_facts", event["seq"])
        if _rows is not None
        else conn.execute("SELECT * FROM l2_facts WHERE commit_seq=?", (event["seq"],)).fetchall()
    )
    decisions = (
        _rows.many(conn, "operation_decisions", event["operation_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_decisions WHERE operation_id=?", (event["operation_id"],)
        ).fetchall()
    )
    if len(facts) != 1 or len(decisions) != 1:
        raise LifecycleError("Confirm event inventory lacks its exact fact/decision")
    fact, decision = facts[0], decisions[0]
    action = payload["action"]
    if (
        not isinstance(action, dict)
        or event["entity_id"] != fact["record_id"]
        or payload["version"] != fact["version"]
        or payload["fact_sha256"] != fact["fact_sha256"]
        or payload["review_id"] != fact["review_id"]
        or decision["record_id"] != fact["record_id"]
        or decision["version"] != fact["version"]
        or decision["review_id"] != fact["review_id"]
        or decision["action_evidence_id"] != action.get("evidence_id")
        or action.get("review_id") != fact["review_id"]
        or canonical(action) != decision["material_json"]
    ):
        raise LifecycleError("Confirm event/fact/decision inventory differs")
    return fact, decision


def audit_record_inventory(conn, record_id, *, _rows=None):
    """Bounded reverse inventory for one record's commit versions."""
    events = (
        _rows.many(conn, "entity_events", "confirm", record_id)
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_events WHERE entity_id=? AND kind IN ('confirm','import')",
            (record_id,),
        ).fetchall()
    )
    seen = set()
    for event in events:
        if event["kind"] == "confirm":
            fact, _ = _exact_confirm_inventory(conn, event, _rows=_rows)
        else:
            fact = _exact_import_inventory(conn, event)
        if fact["version"] in seen:
            raise LifecycleError("Duplicate record commit event inventory")
        seen.add(fact["version"])
    facts = (
        _rows.many(conn, "record_facts", record_id)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_facts WHERE record_id=?", (record_id,)).fetchall()
    )
    if {row["version"] for row in facts} != seen:
        raise LifecycleError("Record fact inventory differs from retained commit events")
    for decision in (
        _rows.many(conn, "record_decisions", record_id)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_decisions WHERE record_id=?", (record_id,))
    ):
        matches = (
            _rows.many(conn, "operation_events", "confirm", decision["operation_id"])
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_events WHERE kind='confirm' AND operation_id=?",
                (decision["operation_id"],),
            ).fetchall()
        )
        if len(matches) != 1:
            raise LifecycleError("Decision inventory lacks its exact confirm event")
        fact, actual = _exact_confirm_inventory(conn, matches[0], _rows=_rows)
        if dict(actual) != dict(decision) or fact["record_id"] != record_id:
            raise LifecycleError("Decision reverse inventory differs")


def _exact_import_inventory(conn, event):
    payload = json.loads(event["payload_json"])
    facts = conn.execute("SELECT * FROM l2_facts WHERE commit_seq=?", (event["seq"],)).fetchall()
    if (
        len(facts) != 1
        or payload.get("version") != facts[0]["version"]
        or payload.get("fact_sha256") != facts[0]["fact_sha256"]
        or event["entity_id"] != facts[0]["record_id"]
        or facts[0]["review_id"] is not None
    ):
        raise LifecycleError("Import event inventory lacks its exact fact version")
    if conn.execute(
        "SELECT 1 FROM l2_decisions WHERE record_id=? AND version=?",
        (facts[0]["record_id"], facts[0]["version"]),
    ).fetchone():
        raise LifecycleError("Imported history acquired a native executable decision")
    return facts[0]


def audit_owned_inventory(conn, *, _rows=None):
    """Complete row/event bijection for retained owned state, including dormant rows."""
    counts = {kind: {} for kind in ("capture", "propose", "review", "delivery")}
    for event in (
        _rows.table(conn, "events")
        if _rows is not None
        else conn.execute("SELECT * FROM l2_events ORDER BY seq")
    ):
        kind, identity = event["kind"], event["entity_id"]
        payload = json.loads(event["payload_json"])
        if kind == "confirm":
            _exact_confirm_inventory(conn, event, _rows=_rows)
        elif kind == "import":
            _exact_import_inventory(conn, event)
        elif kind == "legacy_archive":
            imported = conn.execute(
                "SELECT * FROM l2_imports WHERE export_id=?", (identity,)
            ).fetchone()
            if (
                imported is None
                or payload.get("export_id") != identity
                or payload.get("export_sha256") != imported["export_sha256"]
            ):
                raise LifecycleError("Legacy archive event inventory lacks its exact import")
        elif kind in counts:
            if identity in counts[kind]:
                raise LifecycleError("Duplicate owned event inventory")
            counts[kind][identity] = event
            if kind == "capture":
                row = (
                    _rows.one(conn, "source", identity)
                    if _rows is not None
                    else conn.execute(
                        "SELECT * FROM l2_sources WHERE source_id=?", (identity,)
                    ).fetchone()
                )
                if row is None or row["source_key"].startswith("legacy:"):
                    raise LifecycleError("Capture event inventory lacks its exact source")
                _capture_generation(row, event["payload_json"])
            elif kind == "propose":
                row = (
                    _rows.one(conn, "proposal", identity)
                    if _rows is not None
                    else conn.execute(
                        "SELECT * FROM l2_proposals WHERE proposal_id=?", (identity,)
                    ).fetchone()
                )
                if row is None:
                    raise LifecycleError("Proposal event inventory lacks owned row")
                observation = json.loads(row["observation_json"])["observation"]
                expected = [
                    row["source_id"],
                    row["version"],
                    observation,
                    json.loads(row["values_json"]),
                    row["expected_version"],
                    row["record_id"],
                ]
                if payload != expected or digest(payload) != row["proposal_sha256"]:
                    raise LifecycleError("Proposal event inventory differs from owned row")
            elif kind == "review":
                row = (
                    _rows.one(conn, "review", identity)
                    if _rows is not None
                    else conn.execute(
                        "SELECT * FROM l2_reviews WHERE review_id=?", (identity,)
                    ).fetchone()
                )
                if row is None or event["payload_json"] != row["material_json"]:
                    raise LifecycleError("Review event inventory lacks its exact owned row")
            else:
                row = (
                    _rows.one(conn, "delivery", identity)
                    if _rows is not None
                    else conn.execute(
                        "SELECT * FROM l2_deliveries WHERE review_id=?", (identity,)
                    ).fetchone()
                )
                if (
                    row is None
                    or event["payload_json"] != row["material_json"]
                    or payload.get("review_id") != identity
                    or payload.get("evidence_id") != row["evidence_id"]
                ):
                    raise LifecycleError("Delivery event inventory lacks its exact owned row")
        else:
            raise LifecycleError("Unknown lifecycle audit event kind")
    for kind, table, key in (
        ("capture", "l2_sources", "source_id"),
        ("propose", "l2_proposals", "proposal_id"),
        ("review", "l2_reviews", "review_id"),
        ("delivery", "l2_deliveries", "review_id"),
    ):
        expected = {
            row[key]
            for row in (
                _rows.table(conn, table.removeprefix("l2_"))
                if _rows is not None
                else conn.execute(f"SELECT * FROM {table}")
            )
            if kind != "capture" or not row["source_key"].startswith("legacy:")
        }
        if expected != set(counts[kind]):
            raise LifecycleError("Owned row inventory lacks its exact " + kind + " event")
    for record in (
        tuple((identity,) for identity in _rows.vector(conn, "owned_records"))
        if _rows is not None
        else conn.execute("SELECT record_id FROM l2_facts UNION SELECT record_id FROM l2_decisions")
    ):
        audit_record_inventory(conn, record[0], _rows=_rows)
