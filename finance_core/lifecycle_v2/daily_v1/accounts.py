"""Authenticated daily account proposals and complete prefix reconstruction."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, fields

from finance_core.instance_admission.sessions import (
    _evidence_read_callback,
    _require_runtime_scope,
    lifecycle_read_operation,
)

from ..contracts import (
    CommitReceipt,
    DailyCapacityV2,
    ExpectedAction,
    ExpectedDisplay,
    LifecycleError,
    ProposalRef,
    VerifiedAction,
    VerifiedDelivery,
    VerifiedObservation,
    VerifiedReceiptSourceV1,
    VerifiedSource,
    canonical,
    digest,
)
from ..reader import SnapshotEvidenceVerifier
from ..service import LedgerService, _event, _fact_material, _id, _row
from ._prefix_v1 import validate_eligibility, validate_order
from .contracts import (
    BUNDLE_FORMATS,
    COMMANDS,
    INCOMPLETE_FORMAT,
    OBSERVATION_FORMAT,
    REGISTRY_ID,
    REVIEW_FORMAT,
    BundleFactRefV1,
    DailyCommitReceiptV1,
    DailyReviewV1,
    ExpectedDailyInvocationV1,
    VerifiedDailyProposalV1,
    account_ref,
    compile_proposal,
    decode_command,
    decode_expected,
    exact,
    instruction_ref,
    opening_ref,
    strict_json,
    text,
    validate_bundle,
)

MAX_RECORDS = 128
MAX_FACT_VERSIONS = 2048
MAX_PROPOSALS = 4096


def _record_limit(daily_capacity: DailyCapacityV2 | None) -> int:
    return 512 if daily_capacity is DailyCapacityV2.SYNTHETIC_512 else MAX_RECORDS


def bundle_ref(fact):
    return BundleFactRefV1(
        fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]
    )


def _allocated(source_id, instruction, target, kind):
    if kind == "account_create":
        return "opening-" + digest(["finance-daily-initial-opening-id-v1", source_id])
    return "opening-" + digest(
        ["finance-daily-opening-id-v1", source_id, instruction["evidence_id"], target, kind]
    )


def _account_id(source_id):
    return "account-" + digest(["finance-daily-account-id-v1", source_id])


def _source_instruction(source):
    return asdict(
        VerifiedSource(
            source["evidence_id"],
            source["principal_id"],
            source["client_id"],
            source["source_key"],
            source["raw_input"],
            source["original_sha256"],
            source["created_at"],
        )
    )


def _limits(conn, *, _rows=None, daily_capacity=None):
    if _rows is None:
        records, versions = conn.execute(
            "SELECT count(DISTINCT record_id),count(*) FROM l2_facts"
        ).fetchone()
    else:
        records = len(_rows.vector(conn, "records"))
        versions = len(_rows.table(conn, "facts"))
    if records > _record_limit(daily_capacity) or versions > MAX_FACT_VERSIONS:
        raise LifecycleError("complete_read_limit_exceeded")
    proposals = (
        len(_rows.table(conn, "proposals"))
        if _rows is not None
        else conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0]
    )
    if proposals > MAX_PROPOSALS:
        raise LifecycleError("complete_read_limit_exceeded")


def _require_fact_capacity(conn, *, new_record: bool, daily_capacity=None) -> None:
    """Check the projected inventory for either kind of daily confirmation."""
    if new_record:
        records, versions = conn.execute(
            "SELECT count(DISTINCT record_id),count(*) FROM l2_facts"
        ).fetchone()
        at_record_limit = records >= _record_limit(daily_capacity)
    else:
        versions = conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0]
        at_record_limit = False
    if at_record_limit or versions >= MAX_FACT_VERSIONS:
        raise LifecycleError("complete_read_limit_exceeded")


def registered_values(value):
    if type(value) is not dict:
        raise LifecycleError("unsupported_record_format")
    if "format" in value:
        from .movement_contracts import MOVEMENT_FORMAT, validate_movement

        if value["format"] == MOVEMENT_FORMAT:
            return validate_movement(value)
        if value["format"] not in BUNDLE_FORMATS:
            raise LifecycleError("unsupported_record_format")
        return validate_bundle(value)
    from ..service import _money

    return _money(value)


def closure(
    conn, account_id, *, before_seq=None, before_values=None, _rows=None, daily_capacity=None
):
    """Scan every registered domain by account, never by an old opening ID.

    Caller supplies a commit/review prefix only for authenticated historical
    reconstruction. Current writes invoke a full evidence audit first.
    """
    _limits(conn, _rows=_rows, daily_capacity=daily_capacity)
    if before_seq is None:
        before_seq = conn.execute("SELECT coalesce(max(seq),0)+1 FROM l2_events").fetchone()[0]
    if (
        any(
            event["kind"] in {"import", "legacy_archive"} and event["seq"] < before_seq
            for event in _rows.table(conn, "events")
        )
        if _rows is not None
        else conn.execute(
            "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') AND seq<? LIMIT 1",
            (before_seq,),
        ).fetchone()
    ):
        raise LifecycleError("unsupported_ledger_scope: imported daily dependency scope")
    current = {}
    opening_owners = {}
    for fact in (
        _rows.facts_before(conn, before_seq)
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_facts WHERE commit_seq<? ORDER BY record_id,version", (before_seq,)
        )
    ):
        values = registered_values(strict_json(fact["values_json"]))
        if values.get("format") in BUNDLE_FORMATS:
            if values["account"]["account_id"] != fact["record_id"]:
                raise LifecycleError("Account component belongs to another fact")
            opening = values["opening"]
            if opening:
                identity = opening["opening_event_id"]
                owner = opening_owners.setdefault(identity, fact["record_id"])
                if owner != fact["record_id"]:
                    raise LifecycleError("Opening identity has multiple owners")
        elif values.get("format") == "finance-daily-movement-v1":
            pass  # Exact movement already validated by registered_values.
        else:
            # This is the exact old native expense domain, with unknown account.
            from ..effective_v1 import _business_fields

            _business_fields(fact["values_json"])
        current[fact["record_id"]] = values
    from ._impacts_v1 import current_impacts

    if current_impacts(conn, account_id, before_seq, _rows=_rows):
        raise LifecycleError("unsupported_nonempty_opening_dependencies")
    # Historical empty-closure bytes remain unchanged. Current effective
    # movement dependencies are explicitly refused by this account format.
    material = {
        "format": "finance-daily-opening-dependency-closure-v1",
        "registry": REGISTRY_ID,
        "account_id": account_id,
        "opening_ref": asdict(opening_ref(before_values))
        if before_values and before_values["opening"]
        else None,
        "opening_status": before_values["opening"]["status"]
        if before_values and before_values["opening"]
        else "UNKNOWN",
        "impacts": [],
    }
    return {**material, "closure_sha256": digest(material)}


def needs_closure(values):
    return values["transition"] != "account_rename" and (
        values["transition"] != "account_create" or values["opening"] is not None
    )


def _actual_expected(service, conn, expected, *, proposal_version, proposal_seq, _rows=None):
    """Rebuild the complete retained request from actual source/predecessors."""
    expected = decode_expected(asdict(expected))
    source = service._source(conn, expected.source_id, allow_archived=True, _rows=_rows)
    if (
        expected.source_sha256 != source["source_sha256"]
        or expected.binding.instance_id != service.binding.instance_id
        or (
            expected.command_kind == "account_create"
            and expected.binding.generation != source["generation"]
        )
    ):
        raise LifecycleError("Daily source identity changed")
    if expected.reply_evidence_id is None:
        instruction = _source_instruction(source)
    else:
        reply = service.port.source(conn, expected.reply_evidence_id)
        if (
            type(reply) is not VerifiedSource
            or reply.evidence_id != expected.reply_evidence_id
            or reply.evidence_id == source["evidence_id"]
        ):
            raise LifecycleError("Exact fresh daily reply evidence required")
        instruction = asdict(reply)
    instruction_ref(instruction, expected.binding)
    if expected.reply_evidence_id is not None:
        for prior_request in (
            tuple(
                (row["observation_json"],)
                for row in _rows.many(conn, "source_proposals", expected.source_id)
                if row["version"] < proposal_version
            )
            if _rows is not None
            else conn.execute(
                "SELECT observation_json FROM l2_proposals WHERE source_id=? AND version<?",
                (expected.source_id, proposal_version),
            )
        ):
            if (
                strict_json(prior_request[0]).get("expected", {}).get("reply_evidence_id")
                == expected.reply_evidence_id
            ):
                raise LifecycleError("reply_evidence_already_used")
    if instruction != expected.instruction:
        raise LifecycleError("Daily instruction differs from authenticated source")
    previous = (
        _rows.one(conn, "proposal_version", expected.source_id, proposal_version - 1)
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? AND version=?",
            (expected.source_id, proposal_version - 1),
        ).fetchone()
    )
    if (
        expected.current_proposal_id != (previous["proposal_id"] if previous else None)
        or expected.expected_proposal_version != proposal_version - 1
    ):
        raise LifecycleError("Daily request has different predecessor proposal")
    if expected.expected_version:
        prior = (
            _rows.one(conn, "fact_version", expected.record_id, expected.expected_version)
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
                (expected.record_id, expected.expected_version),
            ).fetchone()
        )
        if (
            prior is None
            or prior["source_id"] != source["source_id"]
            or prior["commit_seq"] >= proposal_seq
            or expected.before_values != strict_json(prior["values_json"])
            or expected.target_bundle_ref != asdict(bundle_ref(prior))
        ):
            raise LifecycleError("Daily target differs from exact original fact")
        validate_bundle(expected.before_values)
        if expected.command_kind == "account_create" or expected.reply_evidence_id is None:
            raise LifecycleError("Daily change requires authenticated reply")
    elif (
        expected.command_kind != "account_create"
        or expected.record_id != _account_id(source["source_id"])
        or (proposal_version == 1 and expected.reply_evidence_id is not None)
        or (proposal_version > 1 and expected.reply_evidence_id is None)
        or (
            any(
                row["commit_seq"] < proposal_seq
                for row in _rows.many(conn, "source_facts", expected.source_id)
            )
            if _rows is not None
            else conn.execute(
                "SELECT 1 FROM l2_facts WHERE source_id=? AND commit_seq<? LIMIT 1",
                (expected.source_id, proposal_seq),
            ).fetchone()
        )
    ):
        raise LifecycleError("Invalid initial account identity or supplemental reply")
    allocated = _allocated(
        expected.source_id, instruction, expected.target_bundle_ref, expected.command_kind
    )
    if allocated != expected.opening_event_id:
        raise LifecycleError("Opening identity was not Core allocated")
    return expected


def verify_proposal(service, conn, proposal, *, _rows=None):
    fingerprint = _fingerprint(conn)
    sealed = strict_json(proposal["observation_json"])
    if sealed.get("format") == "finance-daily-opening-observation-v2":
        from .opening_changes_v1 import verify_proposal as verify_opening_proposal

        return verify_opening_proposal(service, conn, proposal, _rows=_rows)
    exact(sealed, {"format", "expected", "observation"}, "daily observation envelope")
    if sealed["format"] != OBSERVATION_FORMAT:
        raise LifecycleError("Unsupported daily observation")
    events = (
        _rows.many(conn, "entity_events", "propose", proposal["proposal_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_events WHERE kind='propose' AND entity_id=?",
            (proposal["proposal_id"],),
        ).fetchall()
    )
    if len(events) != 1:
        raise LifecycleError("Daily proposal lacks exact event")
    expected = _actual_expected(
        service,
        conn,
        decode_expected(sealed["expected"]),
        proposal_version=proposal["version"],
        proposal_seq=events[0]["seq"],
        _rows=_rows,
    )
    _require_prefix(conn, expected, proposal, "proposal", _rows=_rows)
    if (
        expected.source_id != proposal["source_id"]
        or expected.record_id != proposal["record_id"]
        or expected.expected_version != proposal["expected_version"]
    ):
        raise LifecycleError("Daily proposal row differs from request")
    call = getattr(service.port, "daily_proposal", None)
    if not callable(call):
        raise LifecycleError("Daily evidence port unavailable")
    before = canonical(asdict(expected))
    actual = call(conn, proposal["observation_id"], expected)
    if (
        canonical(asdict(expected)) != before
        or type(actual) is not VerifiedDailyProposalV1
        or actual.format != OBSERVATION_FORMAT
        or actual.source_id != expected.source_id
        or actual.evidence_id != proposal["observation_id"]
        or actual.principal_id != expected.binding.principal_id
        or actual.client_id != expected.binding.client_id
        or actual.request_sha256 != digest(asdict(expected))
        or asdict(actual) != sealed["observation"]
    ):
        raise LifecycleError("Daily authenticated proposal differs")
    values = compile_proposal(decode_command(actual.raw_proposal), expected)
    material = [
        proposal["source_id"],
        proposal["version"],
        asdict(actual),
        values,
        proposal["expected_version"],
        proposal["record_id"],
    ]
    if (
        canonical(values) != proposal["values_json"]
        or digest(material) != proposal["proposal_sha256"]
        or canonical(material) != events[0]["payload_json"]
    ):
        raise LifecycleError("Daily compiled values/proposal hash mismatch")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Evidence callback changed protected state")
    return expected


def verify_review(service, conn, review, *, _rows=None):
    if strict_json(review["material_json"]).get("format") == "finance-daily-opening-review-v2":
        from .opening_changes_v1 import verify_review as verify_opening_review

        return verify_opening_review(service, conn, review, _rows=_rows)
    fingerprint = _fingerprint(conn)
    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_proposals WHERE proposal_id=?", (review["proposal_id"],)
        ).fetchone()
    )
    if proposal is None:
        raise LifecycleError("Daily review lacks proposal")
    expected = verify_proposal(service, conn, proposal, _rows=_rows)
    material = strict_json(review["material_json"])
    exact(
        material,
        {
            "format",
            "binding",
            "review_id",
            "proposal_id",
            "proposal_sha256",
            "command_kind",
            "target_bundle_ref",
            "before_values",
            "after_values",
            "instruction_ref",
            "reason_ref",
            "opening_dependency_closure",
            "created_at",
            "expires_at",
        },
        "daily review",
    )
    events = (
        _rows.many(conn, "entity_events", "review", review["review_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_events WHERE kind='review' AND entity_id=?", (review["review_id"],)
        ).fetchall()
    )
    _require_prefix(conn, expected, proposal, "review", review_id=review["review_id"], _rows=_rows)
    values = validate_bundle(strict_json(proposal["values_json"]))
    expected_closure = (
        closure(
            conn,
            expected.record_id,
            before_seq=events[0]["seq"],
            before_values=expected.before_values,
            _rows=_rows,
            daily_capacity=service.daily_capacity,
        )
        if len(events) == 1 and needs_closure(values)
        else None
    )
    command = decode_command(
        strict_json(proposal["observation_json"])["observation"]["raw_proposal"]
    )
    from .contracts import _reason

    if (
        len(events) != 1
        or material["format"] != REVIEW_FORMAT
        or material["binding"] != asdict(expected.binding)
        or material["review_id"] != review["review_id"]
        or material["proposal_id"] != proposal["proposal_id"]
        or material["proposal_sha256"] != proposal["proposal_sha256"]
        or material["command_kind"] != expected.command_kind
        or material["target_bundle_ref"] != expected.target_bundle_ref
        or material["before_values"] != expected.before_values
        or material["after_values"] != values
        or material["instruction_ref"] != instruction_ref(expected.instruction)
        or material["reason_ref"]
        != _reason(
            command["reason_span"],
            expected,
            expected.command_kind in {"opening_correct", "opening_void"},
        )
        or material["opening_dependency_closure"] != expected_closure
        or review["record_id"] != expected.record_id
        or review["expected_version"] != expected.expected_version
        or review["values_json"] != proposal["values_json"]
        or type(material["created_at"]) is not int
        or type(material["expires_at"]) is not int
        or not 0 < material["expires_at"] - material["created_at"] <= 600
        or material["created_at"] != review["created_at"]
        or digest(material) != review["review_sha256"]
        or canonical(material) != events[0]["payload_json"]
    ):
        raise LifecycleError("Daily complete review differs from proposal/closure")
    delivery = (
        _rows.one(conn, "delivery", review["review_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_deliveries WHERE review_id=?", (review["review_id"],)
        ).fetchone()
    )
    if delivery:
        _require_prefix(
            conn, expected, proposal, "delivery", review_id=review["review_id"], _rows=_rows
        )
        actual = service.port.delivery(
            conn,
            delivery["evidence_id"],
            ExpectedDisplay(expected.binding, review["review_id"], review["review_sha256"]),
        )
        if (
            asdict(actual) != strict_json(delivery["material_json"])
            or actual.evidence_id != delivery["evidence_id"]
            or actual.review_id != review["review_id"]
            or actual.display_sha256 != review["review_sha256"]
            or actual.principal_id != expected.binding.principal_id
            or actual.client_id != expected.binding.client_id
        ):
            raise LifecycleError("Daily delivery differs from actual full card")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Evidence callback changed protected state")
    return expected, delivery


def verify_fact(service, conn, fact, *, _rows=None):
    values = validate_bundle(strict_json(fact["values_json"]))
    account = values["account"]
    if account["account_id"] != fact["record_id"]:
        raise LifecycleError("Daily fact account identity mismatch")
    review = (
        _rows.one(conn, "review", fact["review_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_reviews WHERE review_id=?", (fact["review_id"],)
        ).fetchone()
    )
    if review is None:
        raise LifecycleError("Daily fact lacks review")
    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    )
    if proposal is None:
        raise LifecycleError("Missing l2_proposals identity")
    if (
        strict_json(proposal["observation_json"]).get("format")
        == "finance-daily-opening-observation-v2"
    ):
        from .opening_changes_v1 import verify_fact as verify_opening_fact

        return verify_opening_fact(service, conn, fact, _rows=_rows)
    expected = decode_expected(strict_json(proposal["observation_json"])["expected"])
    _require_prefix(
        conn,
        expected,
        proposal,
        "confirm",
        review_id=review["review_id"],
        commit_seq=fact["commit_seq"],
        _rows=_rows,
    )
    material = strict_json(review["material_json"])
    if needs_closure(values) and material["opening_dependency_closure"] != closure(
        conn,
        fact["record_id"],
        before_seq=fact["commit_seq"],
        before_values=material["before_values"],
        _rows=_rows,
        daily_capacity=service.daily_capacity,
    ):
        raise LifecycleError("Daily commit-prefix dependency closure changed")
    if values["transition"] in {"account_create", "opening_set"} and values["opening"]:
        oid = values["opening"]["opening_event_id"]
        for old in (
            tuple(
                (row["values_json"],)
                for row in _rows.many(conn, "record_facts", fact["record_id"])
                if row["version"] < fact["version"]
            )
            if _rows is not None
            else conn.execute(
                "SELECT values_json FROM l2_facts WHERE record_id=? AND version<?",
                (fact["record_id"], fact["version"]),
            )
        ):
            opening = validate_bundle(strict_json(old[0]))["opening"]
            if opening and opening["opening_event_id"] == oid:
                raise LifecycleError("Closed opening identity cannot be reused")


class _EvidenceReadPort:
    """Operation-local evidence reads on the original issued connection."""

    def __init__(self, port):
        self._port = port

    def _call(self, name, dto_type, conn, identity, expected=None):
        with _evidence_read_callback(conn):
            before = canonical(asdict(expected)) if expected is not None else None
            if dto_type is VerifiedReceiptSourceV1:
                method = getattr(self._port, name, None)
                if not callable(method):
                    raise LifecycleError("Original receipt evidence port is unavailable")
            else:
                method = getattr(self._port, name)
            actual = (
                method(conn, identity) if expected is None else method(conn, identity, expected)
            )
            if type(actual) is not dto_type:
                raise LifecycleError("Exact evidence value required")
            values = {}
            for field in fields(dto_type):
                value = getattr(actual, field.name)
                required_type = (
                    int
                    if dto_type in {VerifiedSource, VerifiedReceiptSourceV1}
                    and field.name in {"source_timestamp", "original_size_bytes"}
                    else str
                )
                if type(value) is not required_type:
                    raise LifecycleError("Evidence fields require exact scalar values")
                values[field.name] = value
            result = dto_type(**values)
            if expected is not None and canonical(asdict(expected)) != before:
                raise LifecycleError("Evidence callback changed its expected request")
        return result

    def source(self, conn, identity):
        return self._call("source", VerifiedSource, conn, identity)

    def receipt_source_v1(self, conn, identity):
        return self._call("receipt_source_v1", VerifiedReceiptSourceV1, conn, identity)

    def receipt_manual_replies_v1(self, conn, source):
        from ..receipt_draft_v1 import _read_manual_replies

        return _read_manual_replies(self._port, conn, source)

    def provider(self, conn, identity, expected):
        return self._call("provider", VerifiedObservation, conn, identity, expected)

    def daily_proposal(self, conn, identity, expected):
        return self._call("daily_proposal", VerifiedDailyProposalV1, conn, identity, expected)

    def movement_instruction_link(self, conn, identity, expected):
        from .movement_contracts import VerifiedMovementInstructionLinkV1

        return self._call(
            "movement_instruction_link", VerifiedMovementInstructionLinkV1, conn, identity, expected
        )

    def movement_proposal(self, conn, identity, expected):
        from .movement_contracts import VerifiedMovementProposalV1

        return self._call("movement_proposal", VerifiedMovementProposalV1, conn, identity, expected)

    def opening_instruction_link_v1(self, conn, identity, expected):
        from .opening_contracts_v1 import VerifiedOpeningInstructionLinkV1

        return self._call(
            "opening_instruction_link_v1",
            VerifiedOpeningInstructionLinkV1,
            conn,
            identity,
            expected,
        )

    def opening_proposal_v2(self, conn, identity, expected):
        from .opening_contracts_v1 import VerifiedOpeningProposalV2

        return self._call(
            "opening_proposal_v2", VerifiedOpeningProposalV2, conn, identity, expected
        )

    def delivery(self, conn, identity, expected):
        return self._call("delivery", VerifiedDelivery, conn, identity, expected)

    def action(self, conn, identity, expected):
        return self._call("action", VerifiedAction, conn, identity, expected)


class _BoundService(SnapshotEvidenceVerifier):
    """One operation's exact base service and evidence port references."""

    def __init__(self, base):
        super().__init__(
            _EvidenceReadPort(base.port),
            base.binding,
            base.legacy_verifier,
            daily_capacity=base.daily_capacity,
        )
        self.owner = base.owner
        self.runtime_admission = base.runtime_admission
        self.clock = base.clock

    def _now(self):
        return int(self.clock())


def _audit(service, conn):
    _limits(conn, daily_capacity=service.daily_capacity)
    if (
        conn.execute("SELECT 1 FROM l2_imports LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM l2_archives LIMIT 1").fetchone()
    ):
        raise LifecycleError("unsupported_ledger_scope")
    from ..reader import audit_evidence_connection

    fingerprint = _fingerprint(conn)
    result = audit_evidence_connection(
        conn,
        service.port,
        service.binding,
        service.legacy_verifier,
        daily_capacity=service.daily_capacity,
    )
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Evidence callback changed protected state")
    return result


def _state_at(conn, expected, before_seq=None, *, _rows=None):
    if _rows is not None:
        current, latest = _rows.state_at(conn, expected.record_id, expected.source_id, before_seq)
        ref = asdict(bundle_ref(current)) if current else None
        return ref, (latest["proposal_id"], latest["version"]) if latest else None
    current = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? AND (? IS NULL OR commit_seq<?) "
        "ORDER BY version DESC LIMIT 1",
        (expected.record_id, before_seq, before_seq),
    ).fetchone()
    ref = asdict(bundle_ref(current)) if current else None
    latest = conn.execute(
        "SELECT p.proposal_id,p.version FROM l2_proposals p JOIN l2_events e "
        "ON e.kind='propose' AND e.entity_id=p.proposal_id "
        "WHERE p.source_id=? AND (? IS NULL OR e.seq<?) ORDER BY p.version DESC LIMIT 1",
        (expected.source_id, before_seq, before_seq),
    ).fetchone()
    return ref, (latest["proposal_id"], latest["version"]) if latest else None


def _require_current(conn, expected, *, proposal=None):
    target, latest = _state_at(conn, expected)
    required = (
        (proposal["proposal_id"], proposal["version"])
        if proposal is not None
        else (expected.current_proposal_id, expected.expected_proposal_version)
        if expected.current_proposal_id is not None
        else None
    )
    validate_eligibility(target, expected.target_bundle_ref, latest, required)


def _event_seq(conn, kind, identity, *, _rows=None):
    if kind not in {"capture", "propose", "review", "delivery"}:
        raise LifecycleError("Unsupported daily prefix event")
    rows = (
        _rows.project_many(conn, "entity_events", ("seq",), kind, identity)
        if _rows is not None
        else conn.execute(
            "SELECT seq FROM l2_events WHERE kind=? AND entity_id=?", (kind, identity)
        ).fetchall()
    )
    if len(rows) != 1:
        raise LifecycleError("Daily prefix requires one exact event")
    return rows[0][0]


def _require_prefix(
    conn, expected, proposal, stage, *, review_id=None, commit_seq=None, _rows=None
):
    sequences = (
        _event_seq(conn, "capture", expected.source_id, _rows=_rows),
        _event_seq(conn, "propose", proposal["proposal_id"], _rows=_rows),
    )
    if stage in {"review", "delivery", "confirm"}:
        sequences += (_event_seq(conn, "review", review_id, _rows=_rows),)
    if stage in {"delivery", "confirm"}:
        sequences += (_event_seq(conn, "delivery", review_id, _rows=_rows),)
    if stage == "confirm":
        sequences += (commit_seq,)
    validate_order(stage, sequences)
    target, latest = _state_at(conn, expected, sequences[-1], _rows=_rows)
    required = (
        (
            (expected.current_proposal_id, expected.expected_proposal_version)
            if expected.current_proposal_id is not None
            else None
        )
        if stage == "proposal"
        else (proposal["proposal_id"], proposal["version"])
    )
    validate_eligibility(target, expected.target_bundle_ref, latest, required)
    return sequences[-1]


def _require_current_authority(service, conn, expected):
    if expected.binding != service.binding:
        raise LifecycleError("stale_proposal_binding")
    service._source(
        conn, expected.source_id, allow_archived=expected.command_kind != "account_create"
    )


def _tail(conn):
    row = conn.execute(
        "SELECT seq,event_sha256 FROM l2_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return tuple(row) if row else (0, None)


def _fingerprint(conn):
    # Legal owner tables are append-only. Tail plus every owned row count also
    # catches an unpaired append made by a callback without a matching event.
    tables = (
        "l2_sources",
        "l2_proposals",
        "l2_reviews",
        "l2_deliveries",
        "l2_facts",
        "l2_decisions",
        "l2_imports",
        "l2_archives",
        "l2_finalization_intents",
        "l2_finalization_materials",
        "l2_finalization_outcomes",
    )
    counts = ",".join("(SELECT count(*) FROM " + name + ")" for name in tables)
    row = conn.execute(
        "SELECT coalesce(tail.seq,0),tail.event_sha256,"
        + counts
        + " FROM (SELECT 1) LEFT JOIN (SELECT seq,event_sha256 FROM l2_events"
        " ORDER BY seq DESC LIMIT 1) AS tail ON 1"
    ).fetchone()
    return (row[0], row[1]), tuple(row)[2:]


def _review_dto(conn, row):
    delivery = conn.execute(
        "SELECT evidence_id FROM l2_deliveries WHERE review_id=?", (row["review_id"],)
    ).fetchone()
    return DailyReviewV1(
        row["review_id"],
        row["proposal_id"],
        row["review_sha256"],
        row["material_json"],
        delivery[0] if delivery else None,
    )


def _receipt(fact):
    values = validate_bundle(strict_json(fact["values_json"]))
    return DailyCommitReceiptV1(
        CommitReceipt(fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]),
        values["transition"],
        account_ref(values),
        opening_ref(values),
    )


def _existing_receipt(service, conn, review_id, action_id, operation_id):
    decision = conn.execute(
        "SELECT * FROM l2_decisions WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if decision is None:
        if conn.execute("SELECT 1 FROM l2_events WHERE operation_id=?", (operation_id,)).fetchone():
            raise LifecycleError("Operation belongs to another event")
        return None
    if decision["review_id"] != review_id or decision["action_evidence_id"] != action_id:
        raise LifecycleError("Commit replay mismatch")
    review = _row(conn, "l2_reviews", "review_id", review_id)
    if strict_json(review["material_json"]).get("binding") != asdict(service.binding):
        raise LifecycleError("Committed replay belongs to another binding")
    from ..reader import verify_record

    fingerprint = _fingerprint(conn)
    verify_record(service, conn, decision["record_id"])
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Evidence callback changed protected state")
    fact = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
        (decision["record_id"], decision["version"]),
    ).fetchone()
    if fact is None:
        raise LifecycleError("Original committed fact is absent")
    return _receipt(fact)


class DailyAccountServiceV1:
    """Opt-in daily protocol on the exact existing protected LedgerService."""

    def __init__(self, service):
        if not isinstance(service, LedgerService):
            raise LifecycleError("Actual LedgerService required")
        service.owner.assert_valid()
        self._service = service

    @contextmanager
    def _write(self):
        base = self._service
        with base._runtime_scope():
            service = _BoundService(base)
            with base._write_operation() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield service, conn
                    from ..prepared_finalization_v1 import _commit_local

                    _commit_local(conn, service.owner)
                except BaseException:
                    if conn.in_transaction:
                        conn.rollback()
                    raise

    @contextmanager
    def _read(self):
        base = self._service
        with base._runtime_scope():
            service = _BoundService(base)
            with lifecycle_read_operation(service.owner) as conn:
                _require_runtime_scope(conn, service.binding, service.runtime_admission)
                conn.execute("BEGIN")
                try:
                    yield service, conn
                finally:
                    conn.rollback()

    @staticmethod
    def _expected(
        service, conn, source_id, record_id, ref, reply, kind, expected_proposal_version=None
    ):
        fingerprint = _fingerprint(conn)
        if type(kind) is not str or kind not in COMMANDS:
            raise LifecycleError("Unknown daily command")
        source = service._source(conn, source_id, allow_archived=kind != "account_create")
        previous = conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        proposal_version = previous["version"] if previous else 0
        if expected_proposal_version is not None and (
            type(expected_proposal_version) is not int
            or proposal_version != expected_proposal_version
        ):
            raise LifecycleError("stale_proposal_version")
        before = None
        if kind == "account_create":
            if conn.execute(
                "SELECT 1 FROM l2_facts WHERE source_id=? LIMIT 1", (source_id,)
            ).fetchone():
                raise LifecycleError("Account source already posted")
            record_id = _account_id(source_id)
            if (proposal_version == 0 and reply is not None) or (
                proposal_version > 0 and reply is None
            ):
                raise LifecycleError("Supplemental create requires fresh reply")
        else:
            if type(ref) is not BundleFactRefV1 or ref.record_id != record_id:
                raise LifecycleError("Exact bundle reference required")
            fact = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if fact is None or bundle_ref(fact) != ref or fact["source_id"] != source_id:
                raise LifecycleError("stale_bundle_ref")
            before = validate_bundle(strict_json(fact["values_json"]))
            if reply is None:
                raise LifecycleError("Authenticated daily change reply required")
            if (
                kind == "opening_void"
                and before["opening"]
                and before["opening"]["status"] == "VOID"
            ):
                raise LifecycleError("opening_already_void")
        if reply is None:
            instruction = _source_instruction(source)
        else:
            actual = service.port.source(conn, reply)
            if (
                type(actual) is not VerifiedSource
                or actual.evidence_id != reply
                or reply == source["evidence_id"]
            ):
                raise LifecycleError("Exact new reply required")
            instruction = asdict(actual)
        instruction_ref(instruction, service.binding)
        if reply is not None:
            for prior_request in conn.execute(
                "SELECT observation_json FROM l2_proposals WHERE source_id=?", (source_id,)
            ):
                if (
                    strict_json(prior_request[0]).get("expected", {}).get("reply_evidence_id")
                    == reply
                ):
                    raise LifecycleError("reply_evidence_already_used")
        target = asdict(ref) if ref else None
        expected = ExpectedDailyInvocationV1(
            service.binding,
            source_id,
            source["source_sha256"],
            ref.version if ref else 0,
            record_id,
            kind,
            before,
            target,
            instruction,
            _allocated(source_id, instruction, target, kind),
            previous["proposal_id"] if previous else None,
            reply,
            proposal_version,
        )
        if _fingerprint(conn) != fingerprint:
            raise LifecycleError("Evidence callback changed protected state")
        return decode_expected(asdict(expected))

    def expected_account_create(
        self, source_id, expected_proposal_version=0, *, reply_evidence_id=None
    ):
        with self._read() as (service, conn):
            _audit(service, conn)
            return self._expected(
                service,
                conn,
                source_id,
                None,
                None,
                reply_evidence_id,
                "account_create",
                expected_proposal_version,
            )

    def expected_account_change(
        self, account_id, expected_bundle_ref, reply_evidence_id, command_kind
    ):
        with self._read() as (service, conn):
            _audit(service, conn)
            fact = _row(conn, "l2_facts", "record_id", account_id)
            return self._expected(
                service,
                conn,
                fact["source_id"],
                account_id,
                expected_bundle_ref,
                reply_evidence_id,
                command_kind,
            )

    @staticmethod
    def _propose(
        service,
        conn,
        source_id,
        observation_id,
        operation_id,
        *,
        record_id=None,
        ref=None,
        reply=None,
        kind="account_create",
        expected_proposal_version=None,
    ):
        text(observation_id, "observation id")
        text(operation_id, "operation id")
        existing = conn.execute(
            "SELECT * FROM l2_proposals WHERE observation_id=?", (observation_id,)
        ).fetchone()
        if existing:
            retained = verify_proposal(service, conn, existing)
            event = conn.execute(
                "SELECT * FROM l2_events WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if (
                event is None
                or event["kind"] != "propose"
                or event["entity_id"] != existing["proposal_id"]
                or retained.source_id != source_id
                or retained.command_kind != kind
                or retained.reply_evidence_id != reply
                or retained.binding != service.binding
                or retained.target_bundle_ref != (asdict(ref) if ref else None)
                or (record_id is not None and retained.record_id != record_id)
                or (
                    expected_proposal_version is not None
                    and retained.expected_proposal_version != expected_proposal_version
                )
            ):
                raise LifecycleError("Daily proposal replay mismatch")
            return ProposalRef(
                existing["proposal_id"], source_id, existing["version"], existing["proposal_sha256"]
            )
        if conn.execute("SELECT 1 FROM l2_events WHERE operation_id=?", (operation_id,)).fetchone():
            raise LifecycleError("Operation already used")
        if conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] >= MAX_PROPOSALS:
            raise LifecycleError("complete_read_limit_exceeded")
        expected = DailyAccountServiceV1._expected(
            service, conn, source_id, record_id, ref, reply, kind, expected_proposal_version
        )
        callback = getattr(service.port, "daily_proposal", None)
        if not callable(callback):
            raise LifecycleError("Daily evidence port unavailable")
        before = canonical(asdict(expected))
        fingerprint = _fingerprint(conn)
        observation = callback(conn, observation_id, expected)
        if (
            canonical(asdict(expected)) != before
            or _fingerprint(conn) != fingerprint
            or type(observation) is not VerifiedDailyProposalV1
            or observation.format != OBSERVATION_FORMAT
            or observation.evidence_id != observation_id
            or observation.source_id != source_id
            or observation.principal_id != service.binding.principal_id
            or observation.client_id != service.binding.client_id
            or observation.request_sha256 != digest(asdict(expected))
        ):
            raise LifecycleError("Daily proposal evidence/request mismatch")
        values = compile_proposal(decode_command(observation.raw_proposal), expected)
        _require_current(conn, expected)
        version = expected.expected_proposal_version + 1
        proposal_id = _id("daily-proposal")
        material = [
            source_id,
            version,
            asdict(observation),
            values,
            expected.expected_version,
            expected.record_id,
        ]
        sha = digest(material)
        sealed = {
            "format": OBSERVATION_FORMAT,
            "observation": asdict(observation),
            "expected": asdict(expected),
        }
        sealed_json = canonical(sealed)
        # Never append an envelope that this version's exact reader cannot
        # decode. Original capture remains retained on a size refusal.
        strict_json(sealed_json)
        conn.execute(
            "INSERT INTO l2_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                source_id,
                version,
                observation_id,
                expected.expected_version,
                expected.record_id,
                canonical(values),
                sealed_json,
                sha,
                service._now(),
            ),
        )
        _event(conn, operation_id, "propose", proposal_id, material)
        return ProposalRef(proposal_id, source_id, version, sha)

    def propose_account_create(
        self,
        source_id,
        observation_id,
        expected_proposal_version,
        operation_id,
        *,
        reply_evidence_id=None,
    ):
        with self._write() as (service, conn):
            _audit(service, conn)
            result = self._propose(
                service,
                conn,
                source_id,
                observation_id,
                operation_id,
                reply=reply_evidence_id,
                expected_proposal_version=expected_proposal_version,
            )
        return result

    def propose_account_change(
        self,
        account_id,
        observation_id,
        expected_bundle_ref,
        operation_id,
        *,
        reply_evidence_id,
        command_kind,
    ):
        with self._write() as (service, conn):
            _audit(service, conn)
            fact = _row(conn, "l2_facts", "record_id", account_id)
            result = self._propose(
                service,
                conn,
                fact["source_id"],
                observation_id,
                operation_id,
                record_id=account_id,
                ref=expected_bundle_ref,
                reply=reply_evidence_id,
                kind=command_kind,
            )
        return result

    def prepare_review(self, proposal_id, expected_proposal_version):
        with self._write() as (service, conn):
            _audit(service, conn)
            proposal = _row(conn, "l2_proposals", "proposal_id", proposal_id)
            expected = verify_proposal(service, conn, proposal)
            if proposal["version"] != expected_proposal_version:
                raise LifecycleError("stale_proposal_version")
            values = strict_json(proposal["values_json"])
            if values.get("format") == INCOMPLETE_FORMAT:
                raise LifecycleError("incomplete_proposal: " + ",".join(values["missing_fields"]))
            validate_bundle(values)
            existing = conn.execute(
                "SELECT * FROM l2_reviews WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if existing:
                verify_review(service, conn, existing)
                result = _review_dto(conn, existing)
            else:
                _require_current_authority(service, conn, expected)
                _require_current(conn, expected, proposal=proposal)
                review_id = _id("daily-review")
                now = service._now()
                dependency = (
                    closure(
                        conn,
                        expected.record_id,
                        before_values=expected.before_values,
                        daily_capacity=service.daily_capacity,
                    )
                    if needs_closure(values)
                    else None
                )
                command = decode_command(
                    strict_json(proposal["observation_json"])["observation"]["raw_proposal"]
                )
                from .contracts import _reason

                material = {
                    "format": REVIEW_FORMAT,
                    "binding": asdict(service.binding),
                    "review_id": review_id,
                    "proposal_id": proposal_id,
                    "proposal_sha256": proposal["proposal_sha256"],
                    "command_kind": expected.command_kind,
                    "target_bundle_ref": expected.target_bundle_ref,
                    "before_values": expected.before_values,
                    "after_values": values,
                    "instruction_ref": instruction_ref(expected.instruction),
                    "reason_ref": _reason(
                        command["reason_span"],
                        expected,
                        expected.command_kind in {"opening_correct", "opening_void"},
                    ),
                    "opening_dependency_closure": dependency,
                    "created_at": now,
                    "expires_at": now + 600,
                }
                sha = digest(material)
                conn.execute(
                    "INSERT INTO l2_reviews VALUES(?,?,?,?,?,?,?,?)",
                    (
                        review_id,
                        proposal_id,
                        expected.expected_version,
                        expected.record_id,
                        canonical(values),
                        canonical(material),
                        sha,
                        now,
                    ),
                )
                _event(conn, "prepare-" + review_id, "review", review_id, material)
                result = _review_dto(conn, _row(conn, "l2_reviews", "review_id", review_id))
        return result

    def record_delivery(self, review_id, delivery_evidence_id):
        from .opening_contracts_v1 import ExpectedOpeningInvocationV2

        with self._write() as (service, conn):
            _audit(service, conn)
            row = _row(conn, "l2_reviews", "review_id", review_id)
            expected, retained_delivery = verify_review(service, conn, row)
            if retained_delivery is None:
                _require_current_authority(service, conn, expected)
            fingerprint = _fingerprint(conn)
            actual = service.port.delivery(
                conn,
                delivery_evidence_id,
                ExpectedDisplay(expected.binding, review_id, row["review_sha256"]),
            )
            service._actor(actual)
            if (
                actual.evidence_id != delivery_evidence_id
                or actual.review_id != review_id
                or actual.display_sha256 != row["review_sha256"]
                or _fingerprint(conn) != fingerprint
            ):
                raise LifecycleError("Daily actual delivery mismatch")
            existing = conn.execute(
                "SELECT * FROM l2_deliveries WHERE review_id=?", (review_id,)
            ).fetchone()
            if existing:
                if existing["evidence_id"] != delivery_evidence_id or existing[
                    "material_json"
                ] != canonical(asdict(actual)):
                    raise LifecycleError("Daily delivery replay mismatch")
            else:
                proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
                _require_current(conn, expected, proposal=proposal)
                if type(expected) is ExpectedOpeningInvocationV2:
                    from .opening_changes_v1 import require_dependencies

                    require_dependencies(conn, expected)
                conn.execute(
                    "INSERT INTO l2_deliveries VALUES(?,?,?)",
                    (review_id, delivery_evidence_id, canonical(asdict(actual))),
                )
                _event(
                    conn, "delivery-" + delivery_evidence_id, "delivery", review_id, asdict(actual)
                )
            result = _review_dto(conn, row)
        return result

    def confirm(self, review_id, action_evidence_id, operation_id):
        text(review_id, "review id")
        text(action_evidence_id, "action evidence id")
        text(operation_id, "operation id")
        from .._finalization_journal_v1 import _assert_unreserved
        from .._finalization_plan_v1 import _append_finalization, _FinalizationPlanV1

        with self._write() as (service, conn):
            _audit(service, conn)
            result = _existing_receipt(service, conn, review_id, action_evidence_id, operation_id)
            if result is None:
                _assert_unreserved(conn, review_id, action_evidence_id, operation_id)
                row = _row(conn, "l2_reviews", "review_id", review_id)
                expected, delivery = verify_review(service, conn, row)
                material = strict_json(row["material_json"])
                values = validate_bundle(strict_json(row["values_json"]))
                now = service._now()
                if (
                    delivery is None
                    or material["binding"] != asdict(service.binding)
                    or not material["created_at"] <= now < material["expires_at"]
                ):
                    raise LifecycleError("Daily review lacks current delivery/binding/expiry")
                fingerprint = _fingerprint(conn)
                action = service.port.action(
                    conn,
                    action_evidence_id,
                    ExpectedAction(
                        service.binding, review_id, row["review_sha256"], delivery["evidence_id"]
                    ),
                )
                service._actor(action)
                if (
                    action.evidence_id != action_evidence_id
                    or action.review_id != review_id
                    or action.review_sha256 != row["review_sha256"]
                    or action.delivery_evidence_id != delivery["evidence_id"]
                    or action.action != "confirm"
                    or _fingerprint(conn) != fingerprint
                ):
                    raise LifecycleError("Daily action or callback state mismatch")
                proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
                _require_current(conn, expected, proposal=proposal)
                if material["format"] == "finance-daily-opening-review-v2":
                    from .opening_changes_v1 import require_current_opening

                    require_current_opening(conn, expected, proposal, values, action_evidence_id)
                elif needs_closure(values) and material["opening_dependency_closure"] != closure(
                    conn,
                    expected.record_id,
                    before_values=expected.before_values,
                    daily_capacity=service.daily_capacity,
                ):
                    raise LifecycleError("Opening dependency closure changed")
                predecessor = conn.execute(
                    "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                    (expected.record_id,),
                ).fetchone()
                _require_fact_capacity(
                    conn, new_record=predecessor is None, daily_capacity=service.daily_capacity
                )
                source = service._source(
                    conn, expected.source_id, allow_archived=predecessor is not None
                )
                # Source revalidation is an external callback too: recheck all
                # optimistic state after it and before the only three appends.
                if _fingerprint(conn) != fingerprint:
                    raise LifecycleError("State changed during source revalidation")
                _require_current(conn, expected, proposal=proposal)
                if material["format"] == "finance-daily-opening-review-v2":
                    require_current_opening(conn, expected, proposal, values, action_evidence_id)
                elif needs_closure(values) and material["opening_dependency_closure"] != closure(
                    conn,
                    expected.record_id,
                    before_values=expected.before_values,
                    daily_capacity=service.daily_capacity,
                ):
                    raise LifecycleError("Opening dependency closure changed after source callback")
                version = expected.expected_version + 1
                economic = predecessor["economic_event_id"] if predecessor else source["source_key"]
                previous = predecessor["fact_sha256"] if predecessor else None
                sha = digest(
                    _fact_material(
                        expected.record_id, version, economic, expected.source_id, values, previous
                    )
                )
                last_seq, last_sha = _tail(conn)
                seq = last_seq + 1
                payload = {
                    "review_id": review_id,
                    "action": asdict(action),
                    "fact_sha256": sha,
                    "version": version,
                    "accepted_at": now,
                }
                fact = (
                    expected.record_id,
                    version,
                    economic,
                    expected.source_id,
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
                    expected.record_id,
                    version,
                    canonical(asdict(action)),
                )
                event = (
                    seq,
                    operation_id,
                    "confirm",
                    expected.record_id,
                    canonical(payload),
                    last_sha,
                    digest([seq, operation_id, "confirm", expected.record_id, payload, last_sha]),
                )
                _append_finalization(conn, _FinalizationPlanV1(fact, decision, event))
                result = _receipt(_row_version(conn, expected.record_id, version))
        return result

    def lookup_commit_receipt(self, operation_id, *, review_id, action_evidence_id):
        text(review_id, "review id")
        text(action_evidence_id, "action evidence id")
        text(operation_id, "operation id")
        with self._read() as (service, conn):
            _audit(service, conn)
            return _existing_receipt(service, conn, review_id, action_evidence_id, operation_id)

    def pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise LifecycleError("Bounded pending page required")
        with self._read() as (service, conn):
            _audit(service, conn)
            rows = conn.execute(
                "SELECT p.* FROM l2_proposals p WHERE "
                "json_extract(p.observation_json,'$.format')=? AND "
                "p.version=(SELECT max(version) FROM l2_proposals WHERE source_id=p.source_id) "
                "AND NOT EXISTS(SELECT 1 FROM l2_reviews r JOIN l2_decisions d "
                "ON d.review_id=r.review_id WHERE r.proposal_id=p.proposal_id) "
                "ORDER BY p.created_at DESC,p.proposal_id LIMIT ?",
                (OBSERVATION_FORMAT, limit),
            ).fetchall()
            result = []
            for row in rows:
                if strict_json(row["observation_json"]).get("format") == OBSERVATION_FORMAT:
                    result.append(
                        {
                            "proposal": asdict(
                                ProposalRef(
                                    row["proposal_id"],
                                    row["source_id"],
                                    row["version"],
                                    row["proposal_sha256"],
                                )
                            ),
                            "values": strict_json(row["values_json"]),
                        }
                    )
            return tuple(result)


def _row_version(conn, record_id, version):
    row = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? AND version=?", (record_id, version)
    ).fetchone()
    if row is None:
        raise LifecycleError("Exact committed version missing")
    return row
