"""Retain complete transfer candidates without granting posting authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..contracts import (
    ExpectedDisplay,
    LifecycleError,
    ProposalRef,
    RuntimeBinding,
    VerifiedDelivery,
    canonical,
    digest,
)
from ..service import _event, _id, _row
from .accounts import (
    MAX_PROPOSALS,
    DailyAccountServiceV1,
    _audit,
    _fingerprint,
    _require_current,
    _require_prefix,
    _review_dto,
    _source_instruction,
)
from .contracts import exact, instruction_ref, strict_json, text
from .transfer_evidence_v1 import require_transfer_opening_replies_v1
from .transfer_facts_v1 import project_transfer_fact_v1, require_transfer_predecessor_v1
from .transfer_opening_v1 import require_active_transfer_openings_v1
from .transfer_review_v1 import _balance_changes

OBSERVATION_FORMAT = "finance-daily-transfer-draft-v1"
REVIEW_FORMAT = "finance-daily-transfer-review-v1"


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
    if prior is None or strict_json(prior["observation_json"]).get("format") != OBSERVATION_FORMAT:
        raise LifecycleError("Transfer draft requires its exact prior transfer proposal")
    return prior["proposal_id"]


def _require_values(service, conn, expected, source, values, before_seq=None, *, _rows=None):
    require_transfer_predecessor_v1(None, values)
    if (
        source["source_sha256"] != expected.source_sha256
        or source["generation"] != expected.binding.generation
        or instruction_ref(_source_instruction(source), expected.binding)
        != values["instruction_ref"]
    ):
        raise LifecycleError("Transfer draft differs from its authenticated capture")
    require_active_transfer_openings_v1(conn, values, before_seq, _rows=_rows)
    require_transfer_opening_replies_v1(service, conn, values, binding=expected.binding)


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
    exact(raw, _ExpectedTransferDraft.__dataclass_fields__, "transfer proposal request")
    exact(raw["binding"], RuntimeBinding.__dataclass_fields__, "transfer proposal binding")
    expected = _ExpectedTransferDraft(**{**raw, "binding": RuntimeBinding(**raw["binding"])})
    if (
        sealed["format"] != OBSERVATION_FORMAT
        or observed["evidence_id"] != proposal["observation_id"]
        or observed["request_sha256"] != digest(raw)
        or type(expected.expected_proposal_version) is not int
        or not 0 <= expected.expected_proposal_version < MAX_PROPOSALS
        or expected.expected_proposal_version + 1 != proposal["version"]
        or expected.record_id != _record_id(proposal["source_id"])
        or expected.record_id != proposal["record_id"]
        or expected.source_id != proposal["source_id"]
        or proposal["expected_version"] != 0
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
    _require_values(service, conn, expected, source, values, cutoff, _rows=_rows)
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
    require_active_transfer_openings_v1(conn, values, cutoff, _rows=_rows)
    projection = project_transfer_fact_v1(values)
    return {
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
        review["expected_version"] != 0
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
        require_active_transfer_openings_v1(conn, material["after_values"], cutoff, _rows=_rows)
        actual = _verified_delivery(service, conn, expected, review, delivery["evidence_id"])
        if canonical(actual) != delivery["material_json"]:
            raise LifecycleError("Transfer delivery differs from its original evidence")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Transfer evidence callback changed protected state")
    return expected, delivery


class DailyTransferDraftServiceV1:
    """Stage an original-linked candidate for a separate guarded transfer confirmation."""

    def __init__(self, service):
        self._boundary = DailyAccountServiceV1(service)

    def propose_create(self, source_id, value, observation_id, operation_id, *, expected_version=0):
        text(source_id, "transfer source id")
        text(observation_id, "transfer proposal evidence")
        text(operation_id, "transfer proposal operation")
        if type(expected_version) is not int or not 0 <= expected_version < MAX_PROPOSALS:
            raise LifecycleError("Exact previous transfer proposal version required")
        require_transfer_predecessor_v1(None, value)
        values = strict_json(canonical(value))
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
            source = service._source(conn, source_id)
            expected = _ExpectedTransferDraft(
                service.binding,
                source_id,
                source["source_sha256"],
                _record_id(source_id),
                _load_predecessor(conn, source_id, expected_version),
                expected_version,
            )
            _require_values(service, conn, expected, source, values)
            _require_current(conn, expected)
            if _fingerprint(conn) != fingerprint:
                raise LifecycleError("Transfer evidence callback changed protected state")
            if conn.execute("SELECT 1 FROM l2_facts WHERE source_id=?", (source_id,)).fetchone():
                raise LifecycleError("Transfer capture already has a posted fact")

            proposal_id = _id("daily-transfer-proposal")
            row = {
                "source_id": source_id,
                "version": expected_version + 1,
                "expected_version": 0,
                "record_id": expected.record_id,
            }
            expected_material = asdict(expected)
            sealed = {
                "format": OBSERVATION_FORMAT,
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
                    0,
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
                    0,
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
            require_active_transfer_openings_v1(conn, strict_json(review["values_json"]))
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
