"""Admitted ordinary income/expense lifecycle with one protected finalization."""

from dataclasses import asdict

from ..contracts import (
    CommitReceipt,
    ExpectedAction,
    ExpectedDisplay,
    LifecycleError,
    ProposalRef,
    VerifiedSource,
    canonical,
    digest,
)
from ..service import _event, _fact_material, _id, _row
from ._impacts_v1 import (
    account_inventory,
    movement_review_effects,
    require_accounts,
    review_impact_changes,
    selected_dependency,
)
from .accounts import (
    MAX_PROPOSALS,
    DailyAccountServiceV1,
    _audit,
    _fingerprint,
    _require_current,
    _require_current_authority,
    _require_fact_capacity,
    _require_prefix,
    _review_dto,
    _row_version,
    _source_instruction,
    _tail,
    bundle_ref,
)
from .contracts import (
    BundleFactRefV1,
    _reason,
    exact,
    instruction_ref,
    strict_json,
    text,
)
from .movement_contracts import (
    COMMANDS,
    INCOMPLETE_FORMAT,
    OBSERVATION_FORMAT,
    REVIEW_FORMAT,
    DailyMovementReceiptV1,
    ExpectedMovementInvocationV1,
    MovementInstructionTargetV1,
    VerifiedMovementInstructionLinkV1,
    VerifiedMovementProposalV1,
    compile_proposal,
    decode_command,
    decode_expected,
    decode_instruction_link,
    movement_record_id_v1,
    validate_before,
    validate_movement,
    without_coverage,
)


def _movement_id(source_id):
    return movement_record_id_v1(source_id)


def _pending_view(row):
    return {
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


def _instruction_target(binding, source, instruction, record_id, kind, previous, ref):
    return MovementInstructionTargetV1(
        binding,
        source["source_id"],
        source["source_sha256"],
        instruction_ref(_source_instruction(source)),
        instruction_ref(instruction),
        record_id,
        kind,
        {
            "proposal_id": previous["proposal_id"],
            "version": previous["version"],
            "proposal_sha256": previous["proposal_sha256"],
        }
        if previous
        else None,
        asdict(ref) if ref else None,
    )


def _instruction_link(service, conn, instruction, target, previous, *, current=False):
    fingerprint = _fingerprint(conn)
    actual = service.port.movement_instruction_link(conn, instruction["evidence_id"], target)
    if type(actual) is not VerifiedMovementInstructionLinkV1:
        raise LifecycleError("Exact independent movement instruction link required")
    value = asdict(actual)
    link = decode_instruction_link(value, target)
    if "correction" in link:
        prior = strict_json(previous["values_json"])
        if prior.get("format") != INCOMPLETE_FORMAT or prior.get("missing_fields") != [
            "opening_coverage"
        ]:
            raise LifecycleError("Opening supplement requires the pending correction")
        command = decode_command(canonical(prior["command"]))
        retained = strict_json(previous["observation_json"])
        if retained.get("format") != OBSERVATION_FORMAT:
            raise LifecycleError("Opening supplement requires a retained movement correction")
        original = decode_expected(retained["expected"])
        if (
            original.command_kind != "movement_correct"
            or command["command_kind"] != "movement_correct"
            or original.binding != target.binding
            or original.source_id != target.source_id
            or original.source_sha256 != target.source_sha256
            or original.target_bundle_ref != target.target_fact_ref
            or original.reply_evidence_id is None
            or original.reply_evidence_id == instruction["evidence_id"]
        ):
            raise LifecycleError("Opening supplement differs from the original correction target")
        actual_reason_source = service.port.source(conn, original.reply_evidence_id)
        if (
            type(actual_reason_source) is not VerifiedSource
            or actual_reason_source.evidence_id != original.reply_evidence_id
            or asdict(actual_reason_source) != original.instruction
            or prior["instruction_ref"] != instruction_ref(original.instruction, target.binding)
            or canonical(link["correction"])
            != canonical(
                {
                    "fields": without_coverage(command["fields"]),
                    "reason_ref": _reason(command["reason_span"], original, True),
                }
            )
        ):
            raise LifecycleError("Opening supplement differs from the authenticated correction")
    question = link["question"]
    if (
        current
        and target.command_kind == "movement_correct"
        and question is not None
        and question["purpose"] == "opening_coverage"
        and "correction" not in link
    ):
        raise LifecycleError("Correction opening reply requires the original correction evidence")
    if question is not None and question["purpose"] == "account_selection":
        prior = strict_json(previous["values_json"])
        if (
            prior.get("format") != INCOMPLETE_FORMAT
            or prior.get("missing_fields") != ["account_id"]
            or prior["command"]["reason_span"] is not None
            or canonical(prior["command"]["fields"])
            != canonical(question["subject"]["draft_fields"])
        ):
            raise LifecycleError("Account question differs from the previous incomplete draft")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Instruction link callback changed protected state")
    return value


def _actual_expected(service, conn, expected, *, proposal_version, proposal_seq, _rows=None):
    expected = decode_expected(asdict(expected))
    source = service._source(conn, expected.source_id, allow_archived=True, _rows=_rows)
    if (
        expected.source_sha256 != source["source_sha256"]
        or expected.binding.instance_id != service.binding.instance_id
    ):
        raise LifecycleError("Movement source identity changed")
    if expected.reply_evidence_id is None:
        instruction = _source_instruction(source)
    else:
        actual = service.port.source(conn, expected.reply_evidence_id)
        if (
            type(actual) is not VerifiedSource
            or actual.evidence_id != expected.reply_evidence_id
            or actual.evidence_id == source["evidence_id"]
        ):
            raise LifecycleError("Exact fresh movement reply required")
        instruction = asdict(actual)
    instruction_ref(instruction, expected.binding)
    if instruction != expected.instruction:
        raise LifecycleError("Movement instruction differs from actual source")
    for prior in (
        (
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
            expected.reply_evidence_id is not None
            and strict_json(prior[0]).get("expected", {}).get("reply_evidence_id")
            == expected.reply_evidence_id
        ):
            raise LifecycleError("reply_evidence_already_used")
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
        raise LifecycleError("Movement predecessor proposal differs")
    if expected.accounts != account_inventory(conn, proposal_seq, _rows=_rows):
        raise LifecycleError("Movement account inventory differs from proposal prefix")
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
            or prior["source_id"] != expected.source_id
            or prior["commit_seq"] >= proposal_seq
            or strict_json(prior["values_json"]) != expected.before_values
            or asdict(bundle_ref(prior)) != expected.target_bundle_ref
        ):
            raise LifecycleError("Movement target differs from retained fact")
        validate_before(expected.before_values)
        if expected.command_kind == "movement_create" or expected.reply_evidence_id is None:
            raise LifecycleError("Movement change requires genuine reply")
    elif (
        expected.command_kind != "movement_create"
        or expected.record_id != _movement_id(expected.source_id)
        or (proposal_version == 1 and expected.reply_evidence_id is not None)
        or (proposal_version > 1 and expected.reply_evidence_id is None)
        or expected.binding.generation != source["generation"]
    ):
        raise LifecycleError("Invalid initial movement identity/reply/generation")
    target = _instruction_target(
        expected.binding,
        source,
        instruction,
        expected.record_id,
        expected.command_kind,
        previous,
        BundleFactRefV1(**expected.target_bundle_ref) if expected.target_bundle_ref else None,
    )
    if _instruction_link(service, conn, instruction, target, previous) != expected.instruction_link:
        raise LifecycleError(
            "Authenticated movement instruction link differs from retained evidence"
        )
    return expected


def verify_proposal(service, conn, proposal, *, _rows=None):
    fingerprint = _fingerprint(conn)
    sealed = strict_json(proposal["observation_json"])
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
    call = getattr(service.port, "movement_proposal", None)
    if not callable(call):
        raise LifecycleError("Daily evidence port unavailable")
    before = canonical(asdict(expected))
    actual = call(conn, proposal["observation_id"], expected)
    if (
        canonical(asdict(expected)) != before
        or type(actual) is not VerifiedMovementProposalV1
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
    v2 = material.get("format") == "finance-daily-movement-review-v2"
    exact(
        material,
        ({"resolved_effects"} if v2 else set())
        | {
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
            "account_dependencies",
            "account_impact_changes",
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
    values = validate_movement(strict_json(proposal["values_json"]))
    expected_closure = selected_dependency(expected, values)
    if len(events) == 1:
        require_accounts(conn, expected, values, events[0]["seq"], _rows=_rows)
    if (
        len(events) != 1
        or material["format"] != ("finance-daily-movement-review-v2" if v2 else REVIEW_FORMAT)
        or material["binding"] != asdict(expected.binding)
        or material["review_id"] != review["review_id"]
        or material["proposal_id"] != proposal["proposal_id"]
        or material["proposal_sha256"] != proposal["proposal_sha256"]
        or material["command_kind"] != expected.command_kind
        or material["target_bundle_ref"] != expected.target_bundle_ref
        or material["before_values"] != expected.before_values
        or material["after_values"] != values
        or material["instruction_ref"] != values["instruction_ref"]
        or material["reason_ref"] != values["reason_ref"]
        or material["account_impact_changes"]
        != (
            movement_review_effects(conn, expected, values, events[0]["seq"], _rows=_rows)[0]
            if v2
            else review_impact_changes(expected.before_values, values)
        )
        or (
            v2
            and material["resolved_effects"]
            != movement_review_effects(conn, expected, values, events[0]["seq"], _rows=_rows)[1]
        )
        or v2
        != any(i["values"].get("opening_dependency_map") is not None for i in expected_closure)
        or material["account_dependencies"] != expected_closure
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
    values = validate_movement(strict_json(fact["values_json"]))
    review = (
        _rows.one(conn, "review", fact["review_id"])
        if _rows is not None
        else _row(conn, "l2_reviews", "review_id", fact["review_id"])
    )
    if review is None:
        raise LifecycleError("Missing l2_reviews identity")
    proposal = (
        _rows.one(conn, "proposal", review["proposal_id"])
        if _rows is not None
        else _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    )
    if proposal is None:
        raise LifecycleError("Missing l2_proposals identity")
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
    require_accounts(conn, expected, values, fact["commit_seq"], _rows=_rows)


def _receipt(fact):
    values = validate_movement(strict_json(fact["values_json"]))
    return DailyMovementReceiptV1(
        CommitReceipt(fact["record_id"], fact["version"], fact["fact_sha256"], fact["commit_seq"]),
        values["transition"],
        values["status"],
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


class DailyMovementServiceV1:
    def __init__(self, service):
        self._boundary = DailyAccountServiceV1(service)

    def _read(self):
        return self._boundary._read()

    def _write(self):
        return self._boundary._write()

    @staticmethod
    def _expected(
        service, conn, source_id, record_id, ref, reply, kind, expected_proposal_version=None
    ):
        if kind not in COMMANDS:
            raise LifecycleError("Unknown movement command")
        source = service._source(conn, source_id, allow_archived=kind != "movement_create")
        previous = conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        version = previous["version"] if previous else 0
        if expected_proposal_version is not None and (
            type(expected_proposal_version) is not int or version != expected_proposal_version
        ):
            raise LifecycleError("stale_proposal_version")
        before = None
        if kind == "movement_create":
            if conn.execute(
                "SELECT 1 FROM l2_facts WHERE source_id=? LIMIT 1", (source_id,)
            ).fetchone():
                raise LifecycleError("Movement source already posted")
            record_id = _movement_id(source_id)
            if (version == 0 and reply is not None) or (version > 0 and reply is None):
                raise LifecycleError("Supplement requires genuine fresh reply")
        else:
            if type(ref) is not BundleFactRefV1 or ref.record_id != record_id:
                raise LifecycleError("Exact movement target required")
            fact = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if fact is None or bundle_ref(fact) != ref or fact["source_id"] != source_id:
                raise LifecycleError("stale_movement_ref")
            before = validate_before(strict_json(fact["values_json"]))
            if before.get("status") == "VOID":
                raise LifecycleError("movement_already_void")
            if reply is None:
                raise LifecycleError("Genuine change reply required")
        if reply is None:
            instruction = _source_instruction(source)
        else:
            actual = service.port.source(conn, reply)
            if (
                type(actual) is not VerifiedSource
                or actual.evidence_id != reply
                or reply == source["evidence_id"]
            ):
                raise LifecycleError("Exact fresh reply required")
            instruction = asdict(actual)
            for prior in conn.execute(
                "SELECT observation_json FROM l2_proposals WHERE source_id=?", (source_id,)
            ):
                if strict_json(prior[0]).get("expected", {}).get("reply_evidence_id") == reply:
                    raise LifecycleError("reply_evidence_already_used")
        instruction_ref(instruction, service.binding)
        target = _instruction_target(
            service.binding, source, instruction, record_id, kind, previous, ref
        )
        link = _instruction_link(service, conn, instruction, target, previous, current=True)
        return decode_expected(
            asdict(
                ExpectedMovementInvocationV1(
                    binding=service.binding,
                    source_id=source_id,
                    source_sha256=source["source_sha256"],
                    expected_version=ref.version if ref else 0,
                    record_id=record_id,
                    command_kind=kind,
                    before_values=before,
                    target_bundle_ref=asdict(ref) if ref else None,
                    instruction=instruction,
                    accounts=account_inventory(conn),
                    instruction_link=link,
                    current_proposal_id=previous["proposal_id"] if previous else None,
                    reply_evidence_id=reply,
                    expected_proposal_version=version,
                )
            )
        )

    def expected_movement_create(
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
                "movement_create",
                expected_proposal_version,
            )

    def expected_movement_change(
        self, record_id, expected_bundle_ref, reply_evidence_id, command_kind
    ):
        with self._read() as (service, conn):
            _audit(service, conn)
            fact = _row(conn, "l2_facts", "record_id", record_id)
            return self._expected(
                service,
                conn,
                fact["source_id"],
                record_id,
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
        kind="movement_create",
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
        expected = DailyMovementServiceV1._expected(
            service, conn, source_id, record_id, ref, reply, kind, expected_proposal_version
        )
        callback = getattr(service.port, "movement_proposal", None)
        if not callable(callback):
            raise LifecycleError("Daily evidence port unavailable")
        before = canonical(asdict(expected))
        fingerprint = _fingerprint(conn)
        observation = callback(conn, observation_id, expected)
        if (
            canonical(asdict(expected)) != before
            or _fingerprint(conn) != fingerprint
            or type(observation) is not VerifiedMovementProposalV1
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

    def propose_movement_create(
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

    def propose_movement_change(
        self,
        record_id,
        observation_id,
        expected_bundle_ref,
        operation_id,
        *,
        reply_evidence_id,
        command_kind,
    ):
        with self._write() as (service, conn):
            _audit(service, conn)
            fact = _row(conn, "l2_facts", "record_id", record_id)
            result = self._propose(
                service,
                conn,
                fact["source_id"],
                observation_id,
                operation_id,
                record_id=record_id,
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
            validate_movement(values)
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
                require_accounts(conn, expected, values)
                dependency = selected_dependency(expected, values)
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
                    "instruction_ref": values["instruction_ref"],
                    "reason_ref": values["reason_ref"],
                    "account_dependencies": dependency,
                    "account_impact_changes": review_impact_changes(expected.before_values, values),
                    "created_at": now,
                    "expires_at": now + 600,
                }
                if any(i["values"].get("opening_dependency_map") is not None for i in dependency):
                    changes, effects = movement_review_effects(conn, expected, values)
                    material["format"] = "finance-daily-movement-review-v2"
                    material["account_impact_changes"] = changes
                    material["resolved_effects"] = effects
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
                require_accounts(conn, expected, strict_json(row["values_json"]))
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
                values = validate_movement(strict_json(row["values_json"]))
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
                    or action_evidence_id
                    in {
                        expected.instruction["evidence_id"],
                        values["instruction_ref"]["evidence_id"],
                    }
                    or action.review_id != review_id
                    or action.review_sha256 != row["review_sha256"]
                    or action.delivery_evidence_id != delivery["evidence_id"]
                    or action.action != "confirm"
                    or _fingerprint(conn) != fingerprint
                ):
                    raise LifecycleError("Daily action or callback state mismatch")
                proposal = _row(conn, "l2_proposals", "proposal_id", row["proposal_id"])
                _require_current(conn, expected, proposal=proposal)
                require_accounts(conn, expected, values)
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
                require_accounts(conn, expected, values)
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
                    result.append(_pending_view(row))
            return tuple(result)

    def pending_for_source(self, source_id: str) -> dict | None:
        """Find the current unfinished movement for an exact captured source."""
        text(source_id, "movement source id")
        with self._read() as (service, conn):
            _audit(service, conn)
            proposal = conn.execute(
                "SELECT * FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if proposal is None:
                return None
            observation = strict_json(proposal["observation_json"])
            if observation.get("format") != OBSERVATION_FORMAT:
                return None
            if observation["expected"]["binding"] != asdict(service.binding):
                return None
            if conn.execute(
                "SELECT 1 FROM l2_reviews r JOIN l2_decisions d ON d.review_id=r.review_id "
                "WHERE r.proposal_id=? LIMIT 1",
                (proposal["proposal_id"],),
            ).fetchone():
                return None
            return _pending_view(proposal)
