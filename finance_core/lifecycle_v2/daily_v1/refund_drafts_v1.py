"""Persist refund proposals after authenticating their exact financial dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from finance_core.instance_admission.sessions import _witness_for

from .._finalization_journal_v1 import _assert_unreserved
from .._finalization_plan_v1 import _append_finalization, _FinalizationPlanV1
from ..contracts import (
    CommitReceipt,
    ExpectedAction,
    LifecycleError,
    ProposalRef,
    RuntimeBinding,
    VerifiedAction,
    canonical,
    digest,
)
from ..service import _event, _fact_material, _id, _row
from .accounts import (
    MAX_PROPOSALS,
    DailyAccountServiceV1,
    _audit,
    _EvidenceReadPort,
    _existing_receipt,
    _fingerprint,
    _require_current,
    _require_fact_capacity,
    _require_prefix,
    _review_dto,
    _tail,
    bundle_ref,
)
from .contracts import BundleFactRefV1, exact, strict_json, text
from .refund_allowance_v1 import _fact_at
from .refund_facts_v1 import REFUND_FORMAT
from .refund_review_v1 import _refund_review_material
from .transfer_drafts_v1 import _require_unused_capture, _verified_delivery

DRAFT_FORMAT = "finance-daily-refund-draft-v1"
REVIEW_FORMAT = "finance-daily-refund-review-v1"


@dataclass(frozen=True)
class _ExpectedRefundDraft:
    binding: RuntimeBinding
    source_id: str
    record_id: str
    expected_proposal_version: int
    current_proposal_id: str | None
    target_bundle_ref: dict | None


def _record_id(source_id):
    return "refund-" + digest([DRAFT_FORMAT, source_id])


def _previous_proposal(conn, source_id, version, *, _rows=None):
    if version == 0:
        return None
    previous = (
        _rows.one(conn, "proposal_version", source_id, version)
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? AND version=?", (source_id, version)
        ).fetchone()
    )
    if previous is None or strict_json(previous["observation_json"]).get("format") != DRAFT_FORMAT:
        raise LifecycleError("Refund draft requires the exact previous refund proposal")
    return previous["proposal_id"]


def _observation(expected, observation_id, material):
    return {
        "evidence_id": observation_id,
        "request_sha256": digest(asdict(expected)),
        "retained_material_sha256": digest(material),
        "retained_evidence": material["evidence"],
    }


def _proposal_hash_material(proposal, observation, values):
    return [
        proposal["source_id"],
        proposal["version"],
        observation,
        values,
        proposal["expected_version"],
        proposal["record_id"],
    ]


def verify_proposal(service, conn, proposal, *, _rows=None):
    if _witness_for(conn) is not None and type(service.port) is not _EvidenceReadPort:
        raise LifecycleError("unsupported_record_format: use the protected refund API")
    sealed = strict_json(proposal["observation_json"])
    exact(sealed, {"format", "expected", "observation"}, "refund proposal envelope")
    raw = sealed["expected"]
    exact(raw, _ExpectedRefundDraft.__dataclass_fields__, "refund proposal request")
    exact(raw["binding"], RuntimeBinding.__dataclass_fields__, "refund binding")
    expected = _ExpectedRefundDraft(**{**raw, "binding": RuntimeBinding(**raw["binding"])})
    if expected.target_bundle_ref is not None:
        exact(
            expected.target_bundle_ref, BundleFactRefV1.__dataclass_fields__, "refund predecessor"
        )
        target = BundleFactRefV1(**expected.target_bundle_ref)
    else:
        target = None
    version = expected.expected_proposal_version
    text(expected.source_id, "refund proposal source")
    if (
        sealed["format"] != DRAFT_FORMAT
        or expected.binding.instance_id != service.binding.instance_id
        or type(version) is not int
        or not 0 <= version < MAX_PROPOSALS
        or proposal["version"] != version + 1
        or expected.source_id != proposal["source_id"]
        or expected.record_id != proposal["record_id"]
        or expected.record_id != (target.record_id if target else _record_id(expected.source_id))
        or proposal["expected_version"] != (target.version if target else 0)
        or expected.current_proposal_id
        != _previous_proposal(conn, expected.source_id, version, _rows=_rows)
    ):
        raise LifecycleError("Retained refund proposal identity differs")
    cutoff = _require_prefix(conn, expected, proposal, "proposal", _rows=_rows)
    if target is None:
        _require_unused_capture(conn, expected.source_id, cutoff, _rows=_rows, label="Refund")
    values = strict_json(proposal["values_json"])
    material = _refund_review_material(
        service, conn, expected.binding, expected.source_id, values, target, before_seq=cutoff
    )
    observed = _observation(expected, proposal["observation_id"], material)
    event = (
        _rows.one(conn, "event", cutoff)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_events WHERE seq=?", (cutoff,)).fetchone()
    )
    hashed = _proposal_hash_material(proposal, observed, values)
    if (
        canonical(sealed) != proposal["observation_json"]
        or canonical(values) != proposal["values_json"]
        or observed != sealed["observation"]
        or event is None
        or canonical(hashed) != event["payload_json"]
        or digest(hashed) != proposal["proposal_sha256"]
    ):
        raise LifecycleError("Refund proposal differs from its verified evidence or ledger prefix")
    return expected


def _review_material(service, conn, proposal, expected, review_id, created_at, cutoff=None):
    replaced = BundleFactRefV1(**expected.target_bundle_ref) if expected.target_bundle_ref else None
    refund = _refund_review_material(
        service,
        conn,
        expected.binding,
        expected.source_id,
        strict_json(proposal["values_json"]),
        replaced,
        before_seq=cutoff,
    )
    return {
        "format": REVIEW_FORMAT,
        "binding": asdict(expected.binding),
        "review_id": review_id,
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "record_id": expected.record_id,
        "after_values": refund.pop("after_values"),
        "refund": refund,
        "created_at": created_at,
        "expires_at": created_at + 600,
    }


def _require_current_review(service, conn, proposal, expected, review, cutoff=None):
    rebuilt = _review_material(
        service, conn, proposal, expected, review["review_id"], review["created_at"], cutoff
    )
    if canonical(rebuilt) != review["material_json"]:
        raise LifecycleError("Refund review no longer describes the exact allowance or credit")


def verify_review(service, conn, review, *, _rows=None):
    fingerprint = _fingerprint(conn)
    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    )
    if proposal is None:
        raise LifecycleError("Refund review lacks its original proposal")
    expected = verify_proposal(service, conn, proposal, _rows=_rows)
    review_id = review["review_id"]
    cutoff = _require_prefix(conn, expected, proposal, "review", review_id=review_id, _rows=_rows)
    _require_current_review(service, conn, proposal, expected, review, cutoff)
    event = (
        _rows.one(conn, "event", cutoff)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_events WHERE seq=?", (cutoff,)).fetchone()
    )
    if (
        review["expected_version"] != proposal["expected_version"]
        or review["record_id"] != expected.record_id
        or review["values_json"] != proposal["values_json"]
        or digest(strict_json(review["material_json"])) != review["review_sha256"]
        or event is None
        or event["operation_id"] != "prepare-" + review_id
        or event["payload_json"] != review["material_json"]
    ):
        raise LifecycleError("Refund review differs from the original proposal or ledger")
    delivery = (
        _rows.one(conn, "delivery", review_id)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_deliveries WHERE review_id=?", (review_id,)).fetchone()
    )
    if delivery is not None:
        cutoff = _require_prefix(
            conn, expected, proposal, "delivery", review_id=review_id, _rows=_rows
        )
        _require_current_review(service, conn, proposal, expected, review, cutoff)
        actual = _verified_delivery(service, conn, expected, review, delivery["evidence_id"])
        if canonical(actual) != delivery["material_json"]:
            raise LifecycleError("Refund delivery differs from the original authenticated card")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Refund evidence callback changed protected state")
    return expected, delivery


def _require_separate_action(conn, proposal, action_id):
    rows = conn.execute(
        "SELECT p.observation_json,p.values_json,d.evidence_id AS delivery_evidence_id "
        "FROM l2_proposals p LEFT JOIN l2_reviews r USING (proposal_id) "
        "LEFT JOIN l2_deliveries d USING (review_id) "
        "WHERE p.source_id=? AND p.version<=?",
        (proposal["source_id"], proposal["version"]),
    )
    for row in rows:
        sealed = strict_json(row["observation_json"])
        if sealed.get("format") != DRAFT_FORMAT:
            continue
        observed = sealed["observation"]
        values = strict_json(row["values_json"])
        proof = observed["retained_evidence"]
        evidence_ids = {
            row["delivery_evidence_id"],
            observed["evidence_id"],
            values["instruction_ref"]["evidence_id"],
            proof["selection"]["evidence_id"],
        }
        if proof["opening_question"] is not None:
            opening = proof["opening_question"]
            evidence_ids.update((opening["reply_evidence_id"], opening["question"]["delivery_id"]))
        if action_id in evidence_ids:
            raise LifecycleError("Refund confirmation requires a separate human action")


def verify_confirmed_refund(service, conn, fact, review, expected, action, *, _rows=None):
    if type(expected) is not _ExpectedRefundDraft or type(action) is not VerifiedAction:
        raise LifecycleError("Refund fact lacks its exact original review and action")
    target = BundleFactRefV1(**expected.target_bundle_ref) if expected.target_bundle_ref else None
    version = target.version + 1 if target is not None else 1
    if (
        strict_json(review["material_json"]).get("format") != REVIEW_FORMAT
        or fact["version"] != version
        or fact["record_id"] != expected.record_id
        or (target is not None and fact["predecessor_sha256"] != target.fact_sha256)
    ):
        raise LifecycleError("Refund fact differs from its review or exact predecessor")
    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    )
    cutoff = _require_prefix(
        conn,
        expected,
        proposal,
        "confirm",
        review_id=review["review_id"],
        commit_seq=fact["commit_seq"],
        _rows=_rows,
    )
    _require_current_review(service, conn, proposal, expected, review, cutoff)
    if target is None:
        _require_unused_capture(conn, expected.source_id, cutoff, _rows=_rows, label="Refund")
    _require_separate_action(conn, proposal, action.evidence_id)


def _refund_receipt(fact):
    if strict_json(fact["values_json"]).get("format") != REFUND_FORMAT:
        raise LifecycleError("Commit belongs to another record format")
    return CommitReceipt(
        fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
    )


class DailyRefundServiceV1:
    """Post authenticated linked refunds within the existing daily write boundary."""

    def __init__(self, ledger):
        self._boundary = DailyAccountServiceV1(ledger)

    def propose(
        self,
        source_id,
        values,
        observation_id,
        operation_id,
        *,
        expected_proposal_version=0,
        replaced=None,
    ):
        text(source_id, "refund source")
        text(observation_id, "refund proposal identity")
        text(operation_id, "refund proposal operation")
        version = expected_proposal_version
        if type(version) is not int or not 0 <= version < MAX_PROPOSALS:
            raise LifecycleError("Exact preceding refund proposal version required")
        if replaced is not None and type(replaced) is not BundleFactRefV1:
            raise LifecycleError("Exact refund predecessor required")
        cleaned = strict_json(canonical(values))
        target = asdict(replaced) if replaced is not None else None
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            existing = conn.execute(
                "SELECT * FROM l2_proposals WHERE observation_id=?", (observation_id,)
            ).fetchone()
            used = conn.execute(
                "SELECT kind,entity_id FROM l2_events WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing is not None:
                actual = verify_proposal(service, conn, existing)
                if (
                    used is None
                    or tuple(used) != ("propose", existing["proposal_id"])
                    or actual.binding != service.binding
                    or actual.source_id != source_id
                    or actual.target_bundle_ref != target
                    or actual.expected_proposal_version != version
                    or existing["values_json"] != canonical(cleaned)
                ):
                    raise LifecycleError("Refund proposal replay mismatch")
                return ProposalRef(
                    existing["proposal_id"],
                    source_id,
                    existing["version"],
                    existing["proposal_sha256"],
                )
            if used is not None:
                raise LifecycleError("Operation already used")
            if conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] >= MAX_PROPOSALS:
                raise LifecycleError("complete_read_limit_exceeded")
            if replaced is None:
                _require_unused_capture(conn, source_id, label="Refund")
            expected = _ExpectedRefundDraft(
                service.binding,
                source_id,
                replaced.record_id if replaced is not None else _record_id(source_id),
                version,
                _previous_proposal(conn, source_id, version),
                target,
            )
            _require_current(conn, expected)
            material = _refund_review_material(
                service, conn, service.binding, source_id, cleaned, replaced
            )
            observed = _observation(expected, observation_id, material)
            row = {
                "source_id": source_id,
                "version": version + 1,
                "expected_version": replaced.version if replaced is not None else 0,
                "record_id": expected.record_id,
            }
            hashed = _proposal_hash_material(row, observed, cleaned)
            sha = digest(hashed)
            sealed = {"format": DRAFT_FORMAT, "expected": asdict(expected), "observation": observed}
            proposal_id = _id("daily-refund-proposal")
            conn.execute(
                "INSERT INTO l2_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    source_id,
                    row["version"],
                    observation_id,
                    row["expected_version"],
                    row["record_id"],
                    canonical(cleaned),
                    canonical(sealed),
                    sha,
                    service._now(),
                ),
            )
            _event(conn, operation_id, "propose", proposal_id, hashed)
            return ProposalRef(proposal_id, source_id, row["version"], sha)

    def prepare_review(self, proposal_id, expected_proposal_version):
        text(proposal_id, "refund proposal id")
        if type(expected_proposal_version) is not int or expected_proposal_version < 1:
            raise LifecycleError("Exact refund proposal version required")
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            proposal = _row(conn, "l2_proposals", "proposal_id", proposal_id)
            expected = verify_proposal(service, conn, proposal)
            if proposal["version"] != expected_proposal_version:
                raise LifecycleError("Stale refund proposal version")
            existing = conn.execute(
                "SELECT * FROM l2_reviews WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                verify_review(service, conn, existing)
                return _review_dto(conn, existing)
            if expected.binding != service.binding:
                raise LifecycleError("Stale refund proposal binding")
            _require_current(conn, expected, proposal=proposal)
            review_id = _id("daily-refund-review")
            created_at = service._now()
            material = _review_material(service, conn, proposal, expected, review_id, created_at)
            conn.execute(
                "INSERT INTO l2_reviews VALUES(?,?,?,?,?,?,?,?)",
                (
                    review_id,
                    proposal_id,
                    proposal["expected_version"],
                    expected.record_id,
                    proposal["values_json"],
                    canonical(material),
                    digest(material),
                    created_at,
                ),
            )
            _event(conn, "prepare-" + review_id, "review", review_id, material)
            return _review_dto(conn, _row(conn, "l2_reviews", "review_id", review_id))

    def record_delivery(self, review_id, evidence_id):
        text(review_id, "refund review id")
        text(evidence_id, "refund delivery evidence")
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            review = _row(conn, "l2_reviews", "review_id", review_id)
            expected, retained = verify_review(service, conn, review)
            if retained is not None:
                if retained["evidence_id"] != evidence_id:
                    raise LifecycleError("Refund delivery replay mismatch")
                return _review_dto(conn, review)
            if expected.binding != service.binding:
                raise LifecycleError("Stale refund binding")
            fingerprint = _fingerprint(conn)
            material = _verified_delivery(service, conn, expected, review, evidence_id)
            proposal = _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
            _require_current(conn, expected, proposal=proposal)
            _require_current_review(service, conn, proposal, expected, review)
            if _fingerprint(conn) != fingerprint:
                raise LifecycleError("Refund evidence callback changed protected state")
            if service._now() >= strict_json(review["material_json"])["expires_at"]:
                raise LifecycleError("Refund review expired before delivery")
            conn.execute(
                "INSERT INTO l2_deliveries VALUES(?,?,?)",
                (review_id, evidence_id, canonical(material)),
            )
            _event(conn, "delivery-" + evidence_id, "delivery", review_id, material)
            return _review_dto(conn, review)

    def confirm(self, review_id, action_evidence_id, operation_id):
        """Recheck the complete original allowance and append once under the owner lock."""
        text(review_id, "refund review id")
        text(action_evidence_id, "refund action evidence")
        text(operation_id, "refund operation")
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            prior = _existing_receipt(
                service, conn, review_id, action_evidence_id, operation_id, receipt=_refund_receipt
            )
            if prior is not None:
                return prior
            _assert_unreserved(conn, review_id, action_evidence_id, operation_id)
            row = _row(conn, "l2_reviews", "review_id", review_id)
            expected, delivery = verify_review(service, conn, row)
            material = strict_json(row["material_json"])
            now = service._now()
            if (
                delivery is None
                or expected.binding != service.binding
                or not material["created_at"] <= now < material["expires_at"]
            ):
                raise LifecycleError("Refund review lacks an unexpired authenticated delivery")
            proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
            _require_separate_action(conn, proposal, action_evidence_id)
            fingerprint = _fingerprint(conn)
            action = service.port.action(
                conn,
                action_evidence_id,
                ExpectedAction(
                    service.binding, review_id, row["review_sha256"], delivery["evidence_id"]
                ),
            )
            if type(action) is not VerifiedAction or action != VerifiedAction(
                action_evidence_id,
                review_id,
                row["review_sha256"],
                delivery["evidence_id"],
                service.binding.principal_id,
                service.binding.client_id,
            ):
                raise LifecycleError("Refund action differs from the authenticated review")
            _require_current(conn, expected, proposal=proposal)
            _require_current_review(service, conn, proposal, expected, row)
            source = service._source(
                conn, expected.source_id, allow_archived=expected.target_bundle_ref is not None
            )
            if _fingerprint(conn) != fingerprint:
                raise LifecycleError("Refund confirmation callback changed protected state")
            target = _fact_at(conn, expected.record_id)
            if target is None:
                _require_unused_capture(conn, expected.source_id, label="Refund")
            _require_fact_capacity(
                conn, new_record=target is None, daily_capacity=service.daily_capacity
            )
            accepted = service._now()
            if not material["created_at"] <= now <= accepted < material["expires_at"]:
                raise LifecycleError("Refund review expired before confirmation")
            record_id = expected.record_id
            version = target["version"] + 1 if target is not None else 1
            economic = target["economic_event_id"] if target is not None else source["source_key"]
            predecessor = bundle_ref(target).fact_sha256 if target is not None else None
            values = material["after_values"]
            sha = digest(
                _fact_material(
                    record_id, version, economic, expected.source_id, values, predecessor
                )
            )
            last_seq, last_sha = _tail(conn)
            seq = last_seq + 1
            decision_material = asdict(action)
            payload = {
                "review_id": review_id,
                "action": decision_material,
                "fact_sha256": sha,
                "version": version,
                "accepted_at": accepted,
            }
            return _append_finalization(
                conn,
                _FinalizationPlanV1(
                    (
                        record_id,
                        version,
                        economic,
                        expected.source_id,
                        review_id,
                        canonical(values),
                        predecessor,
                        sha,
                        seq,
                    ),
                    (
                        action_evidence_id,
                        review_id,
                        operation_id,
                        record_id,
                        version,
                        canonical(decision_material),
                    ),
                    (
                        seq,
                        operation_id,
                        "confirm",
                        record_id,
                        canonical(payload),
                        last_sha,
                        digest([seq, operation_id, "confirm", record_id, payload, last_sha]),
                    ),
                ),
            )

    def lookup_commit_receipt(self, operation_id, *, review_id, action_evidence_id):
        text(operation_id, "refund operation")
        text(review_id, "refund review id")
        text(action_evidence_id, "refund action evidence")
        with self._boundary._read() as (service, conn):
            _audit(service, conn)
            return _existing_receipt(
                service, conn, review_id, action_evidence_id, operation_id, receipt=_refund_receipt
            )
