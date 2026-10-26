"""Indexed current facts and complete append-only audit; no legacy status traversal."""

from __future__ import annotations

import hashlib
import json

from finance_core.instance_admission.sessions import lifecycle_read_operation

from .contracts import LifecycleError, VerifiedSource, canonical, digest
from .service import _fact_material, _money


def audit_connection(conn):
    previous = None
    expected_seq = 1
    for row in conn.execute("SELECT * FROM l2_events ORDER BY seq"):
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
    audit_owned_inventory(conn)
    count = 0
    for row in conn.execute("SELECT * FROM l2_facts ORDER BY record_id,version"):
        values = _money(json.loads(row["values_json"]))
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
            prior = conn.execute(
                "SELECT fact_sha256 FROM l2_facts WHERE record_id=? AND version=?",
                (row["record_id"], row["version"] - 1),
            ).fetchone()
            if prior is None or prior[0] != row["predecessor_sha256"]:
                raise LifecycleError("Fact predecessor invalid")
        event = conn.execute(
            "SELECT kind,payload_json FROM l2_events WHERE seq=?", (row["commit_seq"],)
        ).fetchone()
        if event is None or event[0] not in {"confirm", "import"}:
            raise LifecycleError("Fact lacks audit commit")
        if event[0] == "confirm":
            if json.loads(event[1])["fact_sha256"] != expected:
                raise LifecycleError("Fact audit binding mismatch")
            decision = conn.execute(
                "SELECT * FROM l2_decisions WHERE record_id=? AND version=?",
                (row["record_id"], row["version"]),
            ).fetchone()
            if decision is None or decision["review_id"] != row["review_id"]:
                raise LifecycleError("Fact lacks exact decision")
        count += 1
    for row in conn.execute("SELECT * FROM l2_reviews"):
        material = json.loads(row["material_json"])
        if (
            digest(material) != row["review_sha256"]
            or canonical(material["values"]) != row["values_json"]
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
                verify_review(self.service, conn, row)
            return [self.service._review_dto(conn, row) for row in rows]

    def full_audit(self):
        with lifecycle_read_operation(self.service.owner) as conn:
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


def verify_proposal(service, conn, proposal):
    """Verify the actual retained provider request/output, not its DB self-hash."""
    from dataclasses import asdict

    from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

    from .contracts import ExpectedInvocation, RuntimeBinding

    source = service._source(conn, proposal["source_id"], allow_archived=True)
    sealed = json.loads(proposal["observation_json"])
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
        prior = conn.execute(
            "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
            (proposal["record_id"], proposal["expected_version"]),
        ).fetchone()
    elif proposal["expected_version"]:
        prior = conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? AND version=?",
            (proposal["source_id"], proposal["expected_version"]),
        ).fetchone()
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
    events = conn.execute(
        "SELECT payload_json FROM l2_events WHERE kind='propose' AND entity_id=?",
        (proposal["proposal_id"],),
    ).fetchall()
    if len(events) != 1 or events[0][0] != canonical(material):
        raise LifecycleError("Proposal lacks its exact original audit event")
    prior_proposal = conn.execute(
        "SELECT proposal_id FROM l2_proposals WHERE source_id=? AND version=?",
        (proposal["source_id"], proposal["version"] - 1),
    ).fetchone()
    if expected.current_proposal_id != (prior_proposal[0] if prior_proposal else None):
        raise LifecycleError("Proposal request has a different predecessor identity")
    if digest(material) != proposal["proposal_sha256"]:
        raise LifecycleError("Proposal hash mismatch")
    return expected


def verify_review(service, conn, review):
    from dataclasses import asdict

    from .contracts import ExpectedDisplay

    proposal = conn.execute(
        "SELECT * FROM l2_proposals WHERE proposal_id=?", (review["proposal_id"],)
    ).fetchone()
    if proposal is None:
        raise LifecycleError("Review lacks proposal")
    expected = verify_proposal(service, conn, proposal)
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
    events = conn.execute(
        "SELECT payload_json FROM l2_events WHERE kind='review' AND entity_id=?",
        (review["review_id"],),
    ).fetchall()
    if len(events) != 1 or events[0][0] != canonical(material):
        raise LifecycleError("Review lacks its exact original audit event")
    delivery = conn.execute(
        "SELECT * FROM l2_deliveries WHERE review_id=?", (review["review_id"],)
    ).fetchone()
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


def verify_record(service, conn, record_id):
    from dataclasses import asdict

    from .contracts import ExpectedAction

    audit_record_inventory(conn, record_id)
    previous = None
    version = 0
    economic = None
    for fact in conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version", (record_id,)
    ):
        version += 1
        values = _money(json.loads(fact["values_json"]))
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
        event = conn.execute(
            "SELECT * FROM l2_events WHERE seq=?", (fact["commit_seq"],)
        ).fetchone()
        if event is None:
            raise LifecycleError("Record lacks commit audit")
        if event["kind"] == "import":
            from .legacy import verify_imported_fact

            verify_imported_fact(conn, fact, service.legacy_verifier)
            continue
        review = conn.execute(
            "SELECT * FROM l2_reviews WHERE review_id=?", (fact["review_id"],)
        ).fetchone()
        if review is None:
            raise LifecycleError("Record lacks original review")
        expected, delivery = verify_review(service, conn, review)
        source = service._source(conn, expected.source_id, allow_archived=True)
        if fact["source_id"] != expected.source_id or (
            version == 1 and fact["economic_event_id"] != source["source_key"]
        ):
            raise LifecycleError("Economic event differs from authenticated source identity")
        decision = conn.execute(
            "SELECT * FROM l2_decisions WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
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

    def __init__(self, port, binding, legacy_verifier=None):
        self.port, self.binding, self.legacy_verifier = port, binding, legacy_verifier

    def _actor(self, evidence):
        if (
            evidence.principal_id != self.binding.principal_id
            or getattr(evidence, "client_id", self.binding.client_id) != self.binding.client_id
        ):
            raise LifecycleError("Snapshot source actor/client differs from admitted principal")

    def _source(self, conn, source_id, *, allow_archived=True):
        from .service import LedgerService

        return LedgerService._source(self, conn, source_id, allow_archived=allow_archived)


def audit_evidence_connection(conn, port, binding, legacy_verifier=None):
    """Authenticate every signed source/provider/display/action and dormant archive."""
    verifier = SnapshotEvidenceVerifier(port, binding, legacy_verifier)
    result = audit_connection(conn)
    if (
        conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
        or conn.execute(
            "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') LIMIT 1"
        ).fetchone()
    ):
        from .legacy import verify_import_archives

        verify_import_archives(conn, legacy_verifier)
    for source in conn.execute("SELECT source_id FROM l2_sources"):
        verifier._source(conn, source[0], allow_archived=True)
    for proposal in conn.execute("SELECT * FROM l2_proposals"):
        verify_proposal(verifier, conn, proposal)
    for review in conn.execute("SELECT * FROM l2_reviews"):
        verify_review(verifier, conn, review)
    for record in conn.execute("SELECT DISTINCT record_id FROM l2_facts"):
        verify_record(verifier, conn, record[0])
    return result


def _exact_confirm_inventory(conn, event):
    """Resolve a retained commit in both directions without assuming fact existence."""
    payload = json.loads(event["payload_json"])
    if (
        set(payload) != {"review_id", "action", "fact_sha256", "version", "accepted_at"}
        or type(payload["version"]) is not int
        or payload["version"] < 1
        or type(payload["accepted_at"]) is not int
    ):
        raise LifecycleError("Confirm event inventory payload invalid")
    facts = conn.execute("SELECT * FROM l2_facts WHERE commit_seq=?", (event["seq"],)).fetchall()
    decisions = conn.execute(
        "SELECT * FROM l2_decisions WHERE operation_id=?", (event["operation_id"],)
    ).fetchall()
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


def audit_record_inventory(conn, record_id):
    """Bounded reverse inventory for one record's commit versions."""
    events = conn.execute(
        "SELECT * FROM l2_events WHERE entity_id=? AND kind IN ('confirm','import')", (record_id,)
    ).fetchall()
    seen = set()
    for event in events:
        if event["kind"] == "confirm":
            fact, _ = _exact_confirm_inventory(conn, event)
        else:
            fact = _exact_import_inventory(conn, event)
        if fact["version"] in seen:
            raise LifecycleError("Duplicate record commit event inventory")
        seen.add(fact["version"])
    facts = conn.execute("SELECT * FROM l2_facts WHERE record_id=?", (record_id,)).fetchall()
    if {row["version"] for row in facts} != seen:
        raise LifecycleError("Record fact inventory differs from retained commit events")
    for decision in conn.execute("SELECT * FROM l2_decisions WHERE record_id=?", (record_id,)):
        matches = conn.execute(
            "SELECT * FROM l2_events WHERE kind='confirm' AND operation_id=?",
            (decision["operation_id"],),
        ).fetchall()
        if len(matches) != 1:
            raise LifecycleError("Decision inventory lacks its exact confirm event")
        fact, actual = _exact_confirm_inventory(conn, matches[0])
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


def audit_owned_inventory(conn):
    """Complete row/event bijection for retained owned state, including dormant rows."""
    counts = {kind: {} for kind in ("capture", "propose", "review", "delivery")}
    for event in conn.execute("SELECT * FROM l2_events ORDER BY seq"):
        kind, identity = event["kind"], event["entity_id"]
        payload = json.loads(event["payload_json"])
        if kind == "confirm":
            _exact_confirm_inventory(conn, event)
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
                row = conn.execute(
                    "SELECT * FROM l2_sources WHERE source_id=?", (identity,)
                ).fetchone()
                if (
                    row is None
                    or row["source_key"].startswith("legacy:")
                    or not isinstance(payload, dict)
                    or row["source_sha256"] != digest(payload)
                    or any(
                        row[column] != payload.get(key)
                        for column, key in (
                            ("evidence_id", "evidence_id"),
                            ("principal_id", "principal_id"),
                            ("client_id", "client_id"),
                            ("source_key", "source_key"),
                            ("raw_input", "raw_input"),
                            ("original_sha256", "original_sha256"),
                            ("created_at", "source_timestamp"),
                        )
                    )
                ):
                    raise LifecycleError("Capture event inventory lacks its exact source")
            elif kind == "propose":
                row = conn.execute(
                    "SELECT * FROM l2_proposals WHERE proposal_id=?", (identity,)
                ).fetchone()
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
                row = conn.execute(
                    "SELECT * FROM l2_reviews WHERE review_id=?", (identity,)
                ).fetchone()
                if row is None or event["payload_json"] != row["material_json"]:
                    raise LifecycleError("Review event inventory lacks its exact owned row")
            else:
                row = conn.execute(
                    "SELECT * FROM l2_deliveries WHERE review_id=?", (identity,)
                ).fetchone()
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
            for row in conn.execute(f"SELECT * FROM {table}")
            if kind != "capture" or not row["source_key"].startswith("legacy:")
        }
        if expected != set(counts[kind]):
            raise LifecycleError("Owned row inventory lacks its exact " + kind + " event")
    for record in conn.execute(
        "SELECT record_id FROM l2_facts UNION SELECT record_id FROM l2_decisions"
    ):
        audit_record_inventory(conn, record[0])
