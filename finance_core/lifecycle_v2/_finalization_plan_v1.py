"""The single native confirmation validator and immutable exact row compiler."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .contracts import CommitReceipt, ExpectedAction, LifecycleError, canonical, digest
from .service import _fact_material, _id, _money, _row


@dataclass(frozen=True)
class _FinalizationPlanV1:
    fact: tuple
    decision: tuple
    event: tuple

    @property
    def receipt(self):
        return CommitReceipt(self.fact[0], self.fact[1], self.fact[7], self.fact[8])


def _confirmed_receipt(service, conn, review_id, action_evidence_id, operation_id):
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
        if json.loads(original_review["material_json"])["binding"] != asdict(service.binding):
            raise LifecycleError("Committed replay belongs to another runtime binding")
        fact = conn.execute(
            "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
            (existing["record_id"], existing["version"]),
        ).fetchone()
        if fact is None:
            raise LifecycleError("Committed replay fact is absent")
        from .reader import verify_record

        verify_record(service, conn, existing["record_id"])
        # verify_record authenticates the exact original provider, display,
        # action, decision and commit. This observes an existing result;
        # current expiry/latest-card gates apply only to fresh decisions.
        return CommitReceipt(
            fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
        )
    return None


def _compile_finalization(
    service,
    conn,
    review_id,
    action_evidence_id,
    operation_id,
    *,
    record_id=None,
    accepted_at=None,
    reservation=None,
):
    from ._finalization_journal_v1 import _assert_unreserved

    _assert_unreserved(conn, review_id, action_evidence_id, operation_id, reservation=reservation)
    row = _row(conn, "l2_reviews", "review_id", review_id)
    from .reader import verify_review

    verify_review(service, conn, row)
    material = json.loads(row["material_json"])
    now = service._now()
    accepted_at = now if accepted_at is None else accepted_at
    if (
        material["binding"] != asdict(service.binding)
        or digest(material) != row["review_sha256"]
        or not material["created_at"] <= now < material["expires_at"]
        or not material["created_at"] <= accepted_at < material["expires_at"]
    ):
        raise LifecycleError("Review belongs to another binding or was altered")
    proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
    source = service._source(
        conn, proposal["source_id"], allow_archived=row["record_id"] is not None
    )
    latest = conn.execute(
        "SELECT max(version) FROM l2_proposals WHERE source_id=?", (source["source_id"],)
    ).fetchone()[0]
    if latest != proposal["version"]:
        raise LifecycleError("Stale review after concurrent edit")
    delivery = _row(conn, "l2_deliveries", "review_id", review_id)
    expected = ExpectedAction(
        service.binding, review_id, row["review_sha256"], delivery["evidence_id"]
    )
    action = service.port.action(conn, action_evidence_id, expected)
    service._actor(action)
    if (
        action.evidence_id != action_evidence_id
        or action.review_id != review_id
        or action.review_sha256 != expected.review_sha256
        or action.delivery_evidence_id != expected.delivery_evidence_id
        or action.action != "confirm"
    ):
        raise LifecycleError("Confirmation action mismatch")
    record_id = row["record_id"] or record_id or _id("record")
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
        _fact_material(record_id, version + 1, economic, source["source_id"], values, previous)
    )
    if type(operation_id) is not str or not operation_id or len(operation_id.encode()) > 256:
        raise LifecycleError("Exact operation identity required")
    last = conn.execute(
        "SELECT seq,event_sha256 FROM l2_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    seq, previous_event = (last[0] + 1, last[1]) if last else (1, None)
    payload = {
        "review_id": review_id,
        "action": asdict(action),
        "fact_sha256": sha,
        "version": version + 1,
        "accepted_at": accepted_at,
    }
    event = (
        seq,
        operation_id,
        "confirm",
        record_id,
        canonical(payload),
        previous_event,
        digest([seq, operation_id, "confirm", record_id, payload, previous_event]),
    )
    fact = (
        record_id,
        version + 1,
        economic,
        source["source_id"],
        review_id,
        canonical(values),
        previous,
        sha,
        seq,
    )
    decision = (
        action_evidence_id,
        review_id,
        operation_id,
        record_id,
        version + 1,
        canonical(asdict(action)),
    )
    return _FinalizationPlanV1(fact, decision, event)


def _append_finalization(conn, plan):
    conn.execute("INSERT INTO l2_events VALUES(?,?,?,?,?,?,?)", plan.event)
    conn.execute("INSERT INTO l2_facts VALUES(?,?,?,?,?,?,?,?,?)", plan.fact)
    conn.execute("INSERT INTO l2_decisions VALUES(?,?,?,?,?,?)", plan.decision)
    return plan.receipt
