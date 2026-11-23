"""Stage, display and independently confirm complete transfers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .._finalization_journal_v1 import _assert_unreserved
from .._finalization_plan_v1 import _append_finalization, _FinalizationPlanV1
from ..contracts import (
    CommitReceipt,
    ExpectedAction,
    ExpectedDisplay,
    LifecycleError,
    ProposalRef,
    RuntimeBinding,
    VerifiedAction,
    VerifiedDelivery,
    VerifiedSource,
    canonical,
    digest,
)
from ..service import _event, _fact_material, _id, _row
from ._impacts_v1 import account_inventory, resolve_transfer_effect_v1
from .accounts import (
    MAX_PROPOSALS,
    DailyAccountServiceV1,
    _audit,
    _existing_receipt,
    _fingerprint,
    _require_current,
    _require_fact_capacity,
    _require_prefix,
    _review_dto,
    _source_instruction,
    _tail,
    bundle_ref,
)
from .contracts import BundleFactRefV1, exact, instruction_ref, strict_json, text
from .transfer_evidence_v1 import require_transfer_opening_replies_v1, transfer_change_target_v1
from .transfer_facts_v1 import (
    TRANSFER_FORMAT,
    project_transfer_fact_v1,
    require_transfer_predecessor_v1,
)
from .transfer_opening_v1 import require_active_transfer_openings_v1
from .transfer_review_v1 import _balance_changes, _change_totals

OBSERVATION_FORMAT = "finance-daily-transfer-draft-v1"
CHANGE_DRAFT_FORMAT = "finance-daily-transfer-change-draft-v1"
REVIEW_FORMAT = "finance-daily-transfer-review-v1"
CHANGE_REVIEW_FORMAT = "finance-daily-transfer-change-review-v1"


@dataclass(frozen=True)
class _ExpectedTransferDraft:
    binding: RuntimeBinding
    source_id: str
    source_sha256: str
    record_id: str
    current_proposal_id: str | None
    expected_proposal_version: int

    @property
    def target_bundle_ref(self):
        return None


@dataclass(frozen=True)
class _ExpectedTransferChange(_ExpectedTransferDraft):
    target: dict
    link: dict | None

    @property
    def target_bundle_ref(self):
        return self.target


def _record_id(source_id):
    return "transfer-" + digest([OBSERVATION_FORMAT, source_id])


def _load_predecessor(conn, source_id, version, *, _rows=None):
    if version == 0:
        return None
    prior = (
        _rows.one(conn, "proposal_version", source_id, version)
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? AND version=?", (source_id, version)
        ).fetchone()
    )
    if prior is None or strict_json(prior["observation_json"]).get("format") not in {
        OBSERVATION_FORMAT,
        CHANGE_DRAFT_FORMAT,
    }:
        raise LifecycleError("Transfer draft requires its exact prior transfer proposal")
    return prior["proposal_id"]


def _target_row(conn, expected, *, _rows=None):
    target = expected.target_bundle_ref
    if target is None:
        return None
    row = (
        _rows.one(conn, "fact_version", target["record_id"], target["version"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
            (target["record_id"], target["version"]),
        ).fetchone()
    )
    if row is None or asdict(bundle_ref(row)) != target:
        raise LifecycleError("Transfer change requires its exact retained predecessor")
    return row


def _require_values(service, conn, expected, source, values, before_seq=None, *, _rows=None):
    previous = _target_row(conn, expected, _rows=_rows)
    before = strict_json(previous["values_json"]) if previous is not None else None
    require_transfer_predecessor_v1(before, values)
    if source["source_sha256"] != expected.source_sha256:
        raise LifecycleError("Transfer draft differs from its authenticated capture")
    if previous is None:
        if source["generation"] != expected.binding.generation:
            raise LifecycleError("Transfer draft differs from its capture generation")
        instruction = _source_instruction(source)
    else:
        if previous["source_id"] != expected.source_id:
            raise LifecycleError("Transfer predecessor belongs to another capture")
        actual = service.port.source(conn, values["instruction_ref"]["evidence_id"])
        if type(actual) is not VerifiedSource:
            raise LifecycleError("Exact authenticated transfer change instruction required")
        instruction = asdict(actual)
    if instruction_ref(instruction, expected.binding) != values["instruction_ref"]:
        raise LifecycleError("Transfer draft differs from its authenticated instruction")
    if values["status"] == "ACTIVE":
        require_active_transfer_openings_v1(conn, values, before_seq, _rows=_rows)
        require_transfer_opening_replies_v1(service, conn, values, binding=expected.binding)
    if expected.target_bundle_ref is not None:
        return transfer_change_target_v1(
            service, conn, instruction, expected.binding, expected.target_bundle_ref, values
        )
    return None


def _material(proposal, sealed, values):
    return [
        proposal["source_id"],
        proposal["version"],
        sealed["observation"],
        values,
        proposal["expected_version"],
        proposal["record_id"],
    ]


def verify_proposal(service, conn, proposal, *, _rows=None):
    fingerprint = _fingerprint(conn)
    sealed = strict_json(proposal["observation_json"])
    exact(sealed, {"format", "expected", "observation"}, "transfer proposal envelope")
    observed = sealed["observation"]
    exact(observed, {"evidence_id", "request_sha256"}, "transfer proposal identity")
    raw = sealed["expected"]
    format = sealed["format"]
    if format == CHANGE_DRAFT_FORMAT:
        expected_type = _ExpectedTransferChange
    elif format == OBSERVATION_FORMAT:
        expected_type = _ExpectedTransferDraft
    else:
        raise LifecycleError("Unsupported transfer proposal format")
    exact(raw, expected_type.__dataclass_fields__, "transfer proposal request")
    exact(raw["binding"], RuntimeBinding.__dataclass_fields__, "transfer proposal binding")
    expected = expected_type(**{**raw, "binding": RuntimeBinding(**raw["binding"])})
    target = expected.target_bundle_ref
    record_id = (
        BundleFactRefV1(**target).record_id
        if target is not None
        else _record_id(proposal["source_id"])
    )
    if (
        (isinstance(expected, _ExpectedTransferChange) and target is None)
        or observed["evidence_id"] != proposal["observation_id"]
        or observed["request_sha256"] != digest(raw)
        or type(expected.expected_proposal_version) is not int
        or not 0 <= expected.expected_proposal_version < MAX_PROPOSALS
        or expected.expected_proposal_version + 1 != proposal["version"]
        or expected.record_id != record_id
        or expected.record_id != proposal["record_id"]
        or expected.source_id != proposal["source_id"]
        or proposal["expected_version"] != (target["version"] if target is not None else 0)
        or expected.binding.instance_id != service.binding.instance_id
        or expected.current_proposal_id
        != _load_predecessor(
            conn, expected.source_id, expected.expected_proposal_version, _rows=_rows
        )
    ):
        raise LifecycleError("Retained transfer proposal identity differs")
    text(observed["evidence_id"], "transfer proposal evidence")
    cutoff = _require_prefix(conn, expected, proposal, "proposal", _rows=_rows)
    source = service._source(conn, expected.source_id, allow_archived=True, _rows=_rows)
    values = strict_json(proposal["values_json"])
    link = _require_values(service, conn, expected, source, values, cutoff, _rows=_rows)
    if isinstance(expected, _ExpectedTransferChange) and link != expected.link:
        raise LifecycleError("Transfer change differs from its original association")
    event = (
        _rows.one(conn, "event", cutoff)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_events WHERE seq=?", (cutoff,)).fetchone()
    )
    material = _material(proposal, sealed, values)
    if (
        canonical(values) != proposal["values_json"]
        or canonical(sealed) != proposal["observation_json"]
        or event is None
        or canonical(material) != event["payload_json"]
        or digest(material) != proposal["proposal_sha256"]
        or _fingerprint(conn) != fingerprint
    ):
        raise LifecycleError("Transfer proposal differs from its original evidence or event")
    return expected


def _review_material(conn, proposal, expected, review_id, created_at, cutoff=None, *, _rows=None):
    values = strict_json(proposal["values_json"])
    if values["status"] == "ACTIVE":
        require_active_transfer_openings_v1(conn, values, cutoff, _rows=_rows)
    projection = project_transfer_fact_v1(values)
    material = {
        "format": REVIEW_FORMAT,
        "binding": asdict(expected.binding),
        "review_id": review_id,
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "record_id": expected.record_id,
        "after_values": values,
        "account_roles": list(projection.effects),
        "account_impact_changes": [
            asdict(change) for change in _balance_changes(projection.effects)
        ],
        "expense_by_currency": [list(entry) for entry in projection.expenses_by_currency],
        "created_at": created_at,
        "expires_at": created_at + 600,
    }
    target = _target_row(conn, expected, _rows=_rows)
    if target is not None:
        before_values = strict_json(target["values_json"])
        before = project_transfer_fact_v1(before_values)
        accounts = {
            row["fact_ref"]["record_id"]: row["values"]
            for row in account_inventory(conn, cutoff, _rows=_rows)
        }
        previous_roles = [
            resolve_transfer_effect_v1(
                conn,
                role,
                expected.target_bundle_ref,
                accounts[role["account_id"]],
                cutoff,
                _rows=_rows,
            )
            for role in before.effects
        ]
        next_roles = [
            resolve_transfer_effect_v1(
                conn, role, None, accounts[role["account_id"]], cutoff, _rows=_rows
            )
            for role in projection.effects
        ]
        material.update(
            format=CHANGE_REVIEW_FORMAT,
            target_fact_ref=expected.target_bundle_ref,
            before_values=before_values,
            before_account_roles=previous_roles,
            after_account_roles=next_roles,
            before_expense_by_currency=[list(entry) for entry in before.expenses_by_currency],
            account_impact_changes=[
                asdict(change) for change in _change_totals(previous_roles, next_roles)
            ],
        )
    return material


def _require_current_review(conn, proposal, expected, review, cutoff=None, *, _rows=None):
    material = _review_material(
        conn, proposal, expected, review["review_id"], review["created_at"], cutoff, _rows=_rows
    )
    if canonical(material) != review["material_json"]:
        raise LifecycleError("Transfer review no longer describes the exact account effects")


def _verified_delivery(service, conn, expected, review, evidence_id):
    actual = service.port.delivery(
        conn,
        evidence_id,
        ExpectedDisplay(expected.binding, review["review_id"], review["review_sha256"]),
    )
    if (
        type(actual) is not VerifiedDelivery
        or actual.evidence_id != evidence_id
        or actual.review_id != review["review_id"]
        or actual.display_sha256 != review["review_sha256"]
        or actual.principal_id != expected.binding.principal_id
        or actual.client_id != expected.binding.client_id
    ):
        raise LifecycleError("Transfer delivery differs from the authenticated review")
    return asdict(actual)


def verify_review(service, conn, review, *, _rows=None):
    fingerprint = _fingerprint(conn)
    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    )
    if proposal is None:
        raise LifecycleError("Transfer review lacks its original proposal")
    expected = verify_proposal(service, conn, proposal, _rows=_rows)
    review_id = review["review_id"]
    cutoff = _require_prefix(conn, expected, proposal, "review", review_id=review_id, _rows=_rows)
    material = _review_material(
        conn, proposal, expected, review_id, review["created_at"], cutoff, _rows=_rows
    )
    event = (
        _rows.one(conn, "event", cutoff)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_events WHERE seq=?", (cutoff,)).fetchone()
    )
    if (
        review["expected_version"] != proposal["expected_version"]
        or review["record_id"] != expected.record_id
        or review["values_json"] != proposal["values_json"]
        or review["material_json"] != canonical(material)
        or review["review_sha256"] != digest(material)
        or event is None
        or event["operation_id"] != "prepare-" + review_id
        or event["payload_json"] != review["material_json"]
    ):
        raise LifecycleError("Transfer review differs from its original proposal and ledger state")
    delivery = (
        _rows.one(conn, "delivery", review_id)
        if _rows is not None
        else conn.execute("SELECT * FROM l2_deliveries WHERE review_id=?", (review_id,)).fetchone()
    )
    if delivery is not None:
        cutoff = _require_prefix(
            conn, expected, proposal, "delivery", review_id=review_id, _rows=_rows
        )
        _require_current_review(conn, proposal, expected, review, cutoff, _rows=_rows)
        actual = _verified_delivery(service, conn, expected, review, delivery["evidence_id"])
        if canonical(actual) != delivery["material_json"]:
            raise LifecycleError("Transfer delivery differs from its original evidence")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Transfer evidence callback changed protected state")
    return expected, delivery


def _require_unused_capture(conn, source_id, before_seq=None, *, _rows=None):
    if _rows is None:
        used = conn.execute(
            "SELECT 1 FROM l2_facts WHERE source_id=? AND (? IS NULL OR commit_seq<?) LIMIT 1",
            (source_id, before_seq, before_seq),
        ).fetchone()
    else:
        used = any(
            before_seq is None or row["commit_seq"] < before_seq
            for row in _rows.many(conn, "source_facts", source_id)
        )
    if used:
        raise LifecycleError("Transfer capture already has a posted fact")


def _require_separate_action(material, expected, delivery_id, action_id):
    retained = {delivery_id}
    if isinstance(expected, _ExpectedTransferChange):
        retained.add(expected.link["evidence_id"])
    for values in (material.get("before_values"), material["after_values"]):
        if values is None:
            continue
        retained.add(values["instruction_ref"]["evidence_id"])
        for effect in project_transfer_fact_v1(values).effects:
            reply = effect["coverage"]["reply_ref"]
            if reply is not None:
                retained.add(reply["evidence_id"])
    if action_id in retained:
        raise LifecycleError("Transfer confirmation requires a separate human action")


def verify_confirmed_transfer(conn, fact, review, expected, delivery, action, *, _rows=None):
    material = strict_json(review["material_json"])
    if not isinstance(expected, _ExpectedTransferDraft) or type(action) is not VerifiedAction:
        raise LifecycleError("Transfer fact lacks its exact review and action")
    target = _target_row(conn, expected, _rows=_rows)
    version = target["version"] + 1 if target is not None else 1
    if (
        material.get("format") != (CHANGE_REVIEW_FORMAT if target is not None else REVIEW_FORMAT)
        or fact["version"] != version
        or fact["record_id"] != expected.record_id
        or (target is not None and fact["predecessor_sha256"] != target["fact_sha256"])
    ):
        raise LifecycleError("Transfer fact lacks its exact review and predecessor")
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
    _require_current_review(conn, proposal, expected, review, cutoff, _rows=_rows)
    if target is None:
        _require_unused_capture(conn, expected.source_id, cutoff, _rows=_rows)
    _require_separate_action(material, expected, delivery["evidence_id"], action.evidence_id)


def _transfer_receipt(fact):
    values = strict_json(fact["values_json"])
    if values.get("format") != TRANSFER_FORMAT:
        raise LifecycleError("Commit belongs to another record format")
    return CommitReceipt(
        fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
    )


class DailyTransferServiceV1:
    """Use the protected daily owner to stage and independently confirm a transfer."""

    def __init__(self, service):
        self._boundary = DailyAccountServiceV1(service)

    def propose_create(self, source_id, value, observation_id, operation_id, *, expected_version=0):
        return self._propose(source_id, None, value, observation_id, operation_id, expected_version)

    def propose_change(
        self, source_id, target, value, observation_id, operation_id, *, expected_version
    ):
        if type(target) is not BundleFactRefV1:
            raise LifecycleError("Exact prior transfer fact required")
        return self._propose(
            source_id, target, value, observation_id, operation_id, expected_version
        )

    def _propose(self, source_id, target, value, observation_id, operation_id, expected_version):
        text(source_id, "transfer source id")
        text(observation_id, "transfer proposal evidence")
        text(operation_id, "transfer proposal operation")
        if type(expected_version) is not int or not 0 <= expected_version < MAX_PROPOSALS:
            raise LifecycleError("Exact previous transfer proposal version required")
        values = strict_json(canonical(value))
        target_ref = asdict(target) if target is not None else None
        format = CHANGE_DRAFT_FORMAT if target is not None else OBSERVATION_FORMAT
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            existing = conn.execute(
                "SELECT * FROM l2_proposals WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if existing is not None:
                retained = verify_proposal(service, conn, existing)
                event = conn.execute(
                    "SELECT kind,entity_id FROM l2_events WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if (
                    event is None
                    or tuple(event) != ("propose", existing["proposal_id"])
                    or retained.source_id != source_id
                    or retained.target_bundle_ref != target_ref
                    or retained.expected_proposal_version != expected_version
                    or canonical(values) != existing["values_json"]
                    or retained.binding != service.binding
                ):
                    raise LifecycleError("Transfer proposal replay mismatch")
                return ProposalRef(
                    existing["proposal_id"],
                    source_id,
                    existing["version"],
                    existing["proposal_sha256"],
                )
            if conn.execute(
                "SELECT 1 FROM l2_events WHERE operation_id=?", (operation_id,)
            ).fetchone():
                raise LifecycleError("Operation already used")
            if conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] >= MAX_PROPOSALS:
                raise LifecycleError("complete_read_limit_exceeded")
            fingerprint = _fingerprint(conn)
            source = service._source(conn, source_id, allow_archived=target is not None)
            args = (
                service.binding,
                source_id,
                source["source_sha256"],
                target.record_id if target is not None else _record_id(source_id),
                _load_predecessor(conn, source_id, expected_version),
                expected_version,
            )
            expected = (
                _ExpectedTransferDraft(*args)
                if target_ref is None
                else _ExpectedTransferChange(*args, target_ref, None)
            )
            link = _require_values(service, conn, expected, source, values)
            if isinstance(expected, _ExpectedTransferChange):
                expected = replace(expected, link=link)
            _require_current(conn, expected)
            if _fingerprint(conn) != fingerprint:
                raise LifecycleError("Transfer evidence callback changed protected state")
            if target is None:
                _require_unused_capture(conn, source_id)

            proposal_id = _id("daily-transfer-proposal")
            row = {
                "source_id": source_id,
                "version": expected_version + 1,
                "expected_version": target.version if target is not None else 0,
                "record_id": expected.record_id,
            }
            expected_material = asdict(expected)
            sealed = {
                "format": format,
                "expected": expected_material,
                "observation": {
                    "evidence_id": observation_id,
                    "request_sha256": digest(expected_material),
                },
            }
            material = _material(row, sealed, values)
            sha = digest(material)
            observation_json = canonical(sealed)
            strict_json(observation_json)
            conn.execute(
                "INSERT INTO l2_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    source_id,
                    row["version"],
                    observation_id,
                    row["expected_version"],
                    expected.record_id,
                    canonical(values),
                    observation_json,
                    sha,
                    service._now(),
                ),
            )
            _event(conn, operation_id, "propose", proposal_id, material)
            return ProposalRef(proposal_id, source_id, row["version"], sha)

    def prepare_review(self, proposal_id, expected_proposal_version):
        text(proposal_id, "transfer proposal id")
        if type(expected_proposal_version) is not int or expected_proposal_version < 1:
            raise LifecycleError("Exact transfer proposal version required")
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            proposal = _row(conn, "l2_proposals", "proposal_id", proposal_id)
            expected = verify_proposal(service, conn, proposal)
            if proposal["version"] != expected_proposal_version:
                raise LifecycleError("stale_proposal_version")
            existing = conn.execute(
                "SELECT * FROM l2_reviews WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                verify_review(service, conn, existing)
                return _review_dto(conn, existing)
            if expected.binding != service.binding:
                raise LifecycleError("stale_proposal_binding")
            _require_current(conn, expected, proposal=proposal)
            review_id = _id("daily-transfer-review")
            created_at = service._now()
            material = _review_material(conn, proposal, expected, review_id, created_at)
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
        """Retain actual display of an exact transfer review before financial confirmation."""
        text(review_id, "transfer review id")
        text(evidence_id, "transfer delivery evidence")
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            review = _row(conn, "l2_reviews", "review_id", review_id)
            expected, retained = verify_review(service, conn, review)
            if retained is not None:
                if retained["evidence_id"] != evidence_id:
                    raise LifecycleError("Transfer delivery replay mismatch")
                return _review_dto(conn, review)
            if expected.binding != service.binding:
                raise LifecycleError("stale_proposal_binding")
            fingerprint = _fingerprint(conn)
            material = _verified_delivery(service, conn, expected, review, evidence_id)
            proposal = _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
            _require_current(conn, expected, proposal=proposal)
            _require_current_review(conn, proposal, expected, review)
            if _fingerprint(conn) != fingerprint:
                raise LifecycleError("Transfer evidence callback changed protected state")
            if service._now() >= strict_json(review["material_json"])["expires_at"]:
                raise LifecycleError("Transfer review expired before delivery")
            conn.execute(
                "INSERT INTO l2_deliveries VALUES(?,?,?)",
                (review_id, evidence_id, canonical(material)),
            )
            _event(conn, "delivery-" + evidence_id, "delivery", review_id, material)
            return _review_dto(conn, review)

    def confirm(self, review_id, action_evidence_id, operation_id):
        """Append one whole transfer and its decision under the protected owner lock."""
        text(review_id, "transfer review id")
        text(action_evidence_id, "transfer action evidence")
        text(operation_id, "transfer confirm operation")
        with self._boundary._write() as (service, conn):
            _audit(service, conn)
            existing = _existing_receipt(
                service,
                conn,
                review_id,
                action_evidence_id,
                operation_id,
                receipt=_transfer_receipt,
            )
            if existing is not None:
                return existing
            _assert_unreserved(conn, review_id, action_evidence_id, operation_id)
            row = _row(conn, "l2_reviews", "review_id", review_id)
            expected, delivery = verify_review(service, conn, row)
            material = strict_json(row["material_json"])
            values = material["after_values"]
            now = service._now()
            if (
                delivery is None
                or expected.binding != service.binding
                or not material["created_at"] <= now < material["expires_at"]
            ):
                raise LifecycleError("Transfer review lacks current delivery, binding or expiry")
            _require_separate_action(
                material, expected, delivery["evidence_id"], action_evidence_id
            )
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
                raise LifecycleError("Transfer action differs from the authenticated review")
            target = _target_row(conn, expected)
            source = service._source(conn, expected.source_id, allow_archived=target is not None)
            if isinstance(expected, _ExpectedTransferChange):
                link = _require_values(service, conn, expected, source, values)
                if link != expected.link:
                    raise LifecycleError("Transfer change target changed before confirmation")
            if _fingerprint(conn) != fingerprint:
                raise LifecycleError("Transfer evidence callback changed protected state")
            proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
            _require_current(conn, expected, proposal=proposal)
            _require_current_review(conn, proposal, expected, row)
            if target is None:
                _require_unused_capture(conn, expected.source_id)
            _require_fact_capacity(
                conn, new_record=target is None, daily_capacity=service.daily_capacity
            )
            accepted_at = service._now()
            if not material["created_at"] <= now <= accepted_at < material["expires_at"]:
                raise LifecycleError("Transfer review expired before confirmation")

            record_id = expected.record_id
            version = target["version"] + 1 if target is not None else 1
            economic_id = (
                target["economic_event_id"] if target is not None else source["source_key"]
            )
            predecessor = target["fact_sha256"] if target is not None else None
            sha = digest(
                _fact_material(
                    record_id, version, economic_id, expected.source_id, values, predecessor
                )
            )
            tail_seq, tail_sha = _tail(conn)
            seq = tail_seq + 1
            decision_material = asdict(action)
            payload = {
                "review_id": review_id,
                "action": decision_material,
                "fact_sha256": sha,
                "version": version,
                "accepted_at": accepted_at,
            }
            plan = _FinalizationPlanV1(
                (
                    record_id,
                    version,
                    economic_id,
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
                    tail_sha,
                    digest([seq, operation_id, "confirm", record_id, payload, tail_sha]),
                ),
            )
            return _append_finalization(conn, plan)

    def lookup_commit_receipt(self, operation_id, *, review_id, action_evidence_id):
        """Observe an existing exact commitment without running confirmation again."""
        text(operation_id, "transfer confirm operation")
        text(review_id, "transfer review id")
        text(action_evidence_id, "transfer action evidence")
        with self._boundary._read() as (service, conn):
            _audit(service, conn)
            return _existing_receipt(
                service,
                conn,
                review_id,
                action_evidence_id,
                operation_id,
                receipt=_transfer_receipt,
            )
