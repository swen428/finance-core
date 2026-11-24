"""Persist refund proposals after authenticating their exact financial dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from finance_core.instance_admission.sessions import _witness_for

from ..contracts import LifecycleError, ProposalRef, RuntimeBinding, canonical, digest
from ..service import _event, _id
from .accounts import (
    MAX_PROPOSALS,
    DailyAccountServiceV1,
    _audit,
    _EvidenceReadPort,
    _require_current,
    _require_prefix,
)
from .contracts import BundleFactRefV1, exact, strict_json, text
from .refund_review_v1 import _refund_review_material
from .transfer_drafts_v1 import _require_unused_capture

DRAFT_FORMAT = "finance-daily-refund-draft-v1"


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


class DailyRefundDraftServiceV1:
    """Persist exact refund proposals within the existing daily write boundary."""

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
