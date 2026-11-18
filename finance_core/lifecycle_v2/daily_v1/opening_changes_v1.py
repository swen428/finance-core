"""Controlled opening changes with complete, independently answered dependencies."""

from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from finance_core.money import canonical_money_str

from ..contracts import (
    ExpectedDisplay,
    LifecycleError,
    ProposalRef,
    VerifiedSource,
    canonical,
    digest,
)
from ..service import _event, _id, _row
from ._impacts_v1 import opening_inventory, resolve_effect_v1
from .accounts import (
    MAX_PROPOSALS,
    DailyAccountServiceV1,
    _allocated,
    _audit,
    _fingerprint,
    _require_current,
    _require_current_authority,
    _require_prefix,
    _review_dto,
    _source_instruction,
    bundle_ref,
)
from .contracts import (
    BundleFactRefV1,
    _reason,
    exact,
    instruction_ref,
    strict_json,
    text,
    validate_bundle,
)
from .opening_contracts_v1 import (
    BUNDLE_V2,
    INCOMPLETE_FORMAT,
    OBSERVATION_FORMAT,
    REVIEW_FORMAT,
    ExpectedOpeningInvocationV2,
    OpeningInstructionTargetV1,
    VerifiedOpeningInstructionLinkV1,
    VerifiedOpeningProposalV2,
    compile_opening,
    decode_command,
    decode_expected,
    decode_link,
    list_material,
)


def _previous(conn, source_id, version=None):
    if version is None:
        return conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM l2_proposals WHERE source_id=? AND version=?", (source_id, version)
    ).fetchone()


def _source(service, conn, identity):
    actual = service.port.source(conn, identity)
    if type(actual) is not VerifiedSource or actual.evidence_id != identity:
        raise LifecycleError("Exact genuine opening instruction/reply required")
    value = asdict(actual)
    instruction_ref(value, service.binding)
    return value


def _compose(
    service, conn, source, ref, reply, kind, original_id, previous, before_seq=None, *, binding=None
):
    binding = service.binding if binding is None else binding
    if binding.instance_id != service.binding.instance_id:
        raise LifecycleError("Opening request belongs to another instance")
    if type(ref) is not BundleFactRefV1 or kind not in {"opening_set", "opening_correct"}:
        raise LifecycleError("Exact opening target and command required")
    fact = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? AND version=?", (ref.record_id, ref.version)
    ).fetchone()
    if fact is None or bundle_ref(fact) != ref or fact["source_id"] != source["source_id"]:
        raise LifecycleError("stale_bundle_ref")
    before = validate_bundle(strict_json(fact["values_json"]))
    if reply == source["evidence_id"]:
        raise LifecycleError("Fresh opening instruction required")
    response = _source(service, conn, reply)
    original = response if original_id == reply else _source(service, conn, original_id)
    version = previous["version"] if previous else 0
    dependencies = opening_inventory(conn, ref.record_id, before_seq)
    retained, original_command = [], None
    if original_id != reply:
        if (
            previous is None
            or strict_json(previous["observation_json"]).get("format") != OBSERVATION_FORMAT
        ):
            raise LifecycleError("Opening answers lack original proposal")
        old = decode_expected(strict_json(previous["observation_json"])["expected"])
        if old.binding != binding:
            raise LifecycleError("stale_proposal_binding")
        if (
            old.instruction != original
            or old.target_bundle_ref != asdict(ref)
            or old.command_kind != kind
        ):
            raise LifecycleError("New target needs fresh opening instruction")
        command = decode_command(
            strict_json(previous["observation_json"])["observation"]["raw_proposal"]
        )
        original_command = {k: command[k] for k in ("command_kind", "fields", "reason_span")}
        old_values = strict_json(previous["values_json"])
        if old.dependencies == dependencies:
            retained = (
                old_values["answers"]
                if old_values["format"] == INCOMPLETE_FORMAT
                else old_values["opening_dependency_map"]["entries"]
            )
    for row in conn.execute(
        "SELECT observation_json FROM l2_proposals WHERE source_id=? AND version<=?",
        (source["source_id"], version),
    ):
        if strict_json(row[0]).get("expected", {}).get("reply_evidence_id") == reply:
            raise LifecycleError("reply_evidence_already_used")
    previous_ref = (
        {
            "proposal_id": previous["proposal_id"],
            "version": version,
            "proposal_sha256": previous["proposal_sha256"],
        }
        if previous
        else None
    )
    target = OpeningInstructionTargetV1(
        binding,
        source["source_id"],
        source["source_sha256"],
        instruction_ref(_source_instruction(source)),
        instruction_ref(original),
        instruction_ref(response),
        ref.record_id,
        kind,
        previous_ref,
        asdict(ref),
    )
    link = service.port.opening_instruction_link_v1(conn, reply, target)
    if type(link) is not VerifiedOpeningInstructionLinkV1:
        raise LifecycleError("Exact independently retained opening link required")
    decode_link(asdict(link), target)
    expected = ExpectedOpeningInvocationV2(
        binding,
        source["source_id"],
        source["source_sha256"],
        ref.version,
        ref.record_id,
        kind,
        before,
        asdict(ref),
        original,
        _allocated(source["source_id"], original, asdict(ref), kind),
        previous["proposal_id"] if previous else None,
        reply,
        version,
        response=response,
        instruction_link=asdict(link),
        dependencies=dependencies,
        retained_answers=retained,
        original_command=original_command,
    )
    return decode_expected(asdict(expected))


def _opening_proposal_chain(conn, proposal):
    """Collect one bounded supplement chain, without granting authentication.

    Rows are local to this protected operation. Every collected item is fully
    authenticated afterwards; structural collection never caches evidence or
    substitutes for the original callback/prefix/hash checks.
    """
    chain, seen_ids, seen_versions = [], set(), set()
    scope = None
    while True:
        if proposal is None:
            raise LifecycleError("Opening proposal predecessor absent")
        version = proposal["version"]
        if type(version) is not int or version <= 0:
            raise LifecycleError("Invalid bounded opening proposal version")
        if version > MAX_PROPOSALS or len(chain) >= MAX_PROPOSALS:
            raise LifecycleError("complete_read_limit_exceeded: opening proposal chain")
        identity = text(proposal["proposal_id"], "opening proposal id")
        sealed = strict_json(proposal["observation_json"])
        exact(sealed, {"format", "expected", "observation"}, "opening observation")
        if sealed["format"] != OBSERVATION_FORMAT:
            raise LifecycleError("Unsupported opening observation")
        expected = decode_expected(sealed["expected"])
        if (
            proposal["source_id"] != expected.source_id
            or proposal["record_id"] != expected.record_id
            or version != expected.expected_proposal_version + 1
        ):
            raise LifecycleError("Opening proposal chain identity differs")
        current_scope = (expected.source_id, expected.record_id, expected.instruction)
        if scope is None:
            scope = current_scope
        elif current_scope != scope:
            raise LifecycleError("Opening proposal chain source/target/instruction differs")
        version_key = (expected.source_id, version)
        if identity in seen_ids or version_key in seen_versions:
            raise LifecycleError("Repeated opening proposal predecessor")
        seen_ids.add(identity)
        seen_versions.add(version_key)
        chain.append(proposal)
        if expected.instruction["evidence_id"] == expected.reply_evidence_id:
            return chain
        previous = _previous(conn, expected.source_id, version - 1)
        if (
            previous is None
            or previous["proposal_id"] != expected.current_proposal_id
            or previous["version"] != version - 1
            or previous["source_id"] != expected.source_id
            or previous["record_id"] != expected.record_id
        ):
            raise LifecycleError("Opening proposal predecessor absent or inconsistent")
        proposal = previous


def verify_proposal(service, conn, proposal):
    fingerprint = _fingerprint(conn)
    chain = _opening_proposal_chain(conn, proposal)
    for item in reversed(chain):
        result = _verify_proposal_item(service, conn, item)
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Evidence callback changed protected opening chain state")
    return result


def _verify_proposal_item(service, conn, proposal):
    fingerprint = _fingerprint(conn)
    sealed = strict_json(proposal["observation_json"])
    exact(sealed, {"format", "expected", "observation"}, "opening observation")
    if sealed["format"] != OBSERVATION_FORMAT:
        raise LifecycleError("Unsupported opening observation")
    expected = decode_expected(sealed["expected"])
    events = conn.execute(
        "SELECT * FROM l2_events WHERE kind='propose' AND entity_id=?", (proposal["proposal_id"],)
    ).fetchall()
    if len(events) != 1:
        raise LifecycleError("Opening proposal lacks exact original event")
    previous = _previous(conn, expected.source_id, proposal["version"] - 1)
    source = service._source(conn, expected.source_id, allow_archived=True)
    actual_expected = _compose(
        service,
        conn,
        source,
        BundleFactRefV1(**expected.target_bundle_ref),
        expected.reply_evidence_id,
        expected.command_kind,
        expected.instruction["evidence_id"],
        previous,
        events[0]["seq"],
        binding=expected.binding,
    )
    # Historical runtime binding belongs to the retained request, not today's
    # service generation. Source/link verification uses that exact binding.
    if actual_expected != expected:
        raise LifecycleError("Opening request differs from original source/prefix")
    _require_prefix(conn, expected, proposal, "proposal")
    actual = service.port.opening_proposal_v2(conn, proposal["observation_id"], expected)
    if (
        type(actual) is not VerifiedOpeningProposalV2
        or actual.format != OBSERVATION_FORMAT
        or actual.evidence_id != proposal["observation_id"]
        or actual.source_id != expected.source_id
        or actual.principal_id != expected.binding.principal_id
        or actual.client_id != expected.binding.client_id
        or actual.request_sha256 != digest(asdict(expected))
        or asdict(actual) != sealed["observation"]
    ):
        raise LifecycleError("Authenticated opening proposal differs")
    values = compile_opening(decode_command(actual.raw_proposal), expected)
    material = [
        proposal["source_id"],
        proposal["version"],
        asdict(actual),
        values,
        proposal["expected_version"],
        proposal["record_id"],
    ]
    if (
        proposal["source_id"] != expected.source_id
        or proposal["record_id"] != expected.record_id
        or proposal["expected_version"] != expected.expected_version
        or proposal["version"] != expected.expected_proposal_version + 1
        or canonical(values) != proposal["values_json"]
        or digest(material) != proposal["proposal_sha256"]
        or canonical(material) != events[0]["payload_json"]
        or _fingerprint(conn) != fingerprint
    ):
        raise LifecycleError("Opening compiled proposal/prefix differs")
    return expected


def require_dependencies(conn, expected, before_seq=None):
    if opening_inventory(conn, expected.record_id, before_seq) != expected.dependencies:
        raise LifecycleError("stale_opening_dependency_closure")


def review_effects(expected, values):
    entries = values["opening_dependency_map"]["entries"]
    rows = []
    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        before_delta, after_delta = Decimal(0), Decimal(0)
        for dependency, entry in zip(expected.dependencies, entries, strict=True):
            before = dependency["resolved_effect"]
            resolved_after = resolve_effect_v1(
                None, dependency["values"], dependency["fact_ref"], values, _proposed=True
            )
            after = Decimal(resolved_after["balance_impact"])
            before_delta += Decimal(before["balance_impact"])
            after_delta += after
            currency = before["currency"]
            rows.append(
                {
                    "subject": entry["subject"],
                    "before_effect": before,
                    "new_treatment": entry["treatment"],
                    "after_effect": resolved_after,
                    "basis": entry["basis"],
                    "reply_link_ref": entry["reply_link_ref"],
                    "answer_ref": entry["answer_ref"],
                    "new_balance_impact": canonical_money_str(after, currency),
                    "balance_delta": canonical_money_str(
                        after - Decimal(before["balance_impact"]), currency
                    ),
                }
            )
        old = expected.before_values["opening"]
        before_balance = (
            Decimal(old["amount"]) + before_delta if old and old["status"] == "ACTIVE" else None
        )
        after_balance = Decimal(values["opening"]["amount"]) + after_delta
        currency = values["account"]["currency"]
        return {
            "format": "finance-daily-opening-review-effects-v1",
            "rows": rows,
            "dependency_count": len(rows),
            "unresolved_count": 0,
            "before_balance": canonical_money_str(before_balance, currency)
            if before_balance is not None
            else None,
            "after_balance": canonical_money_str(after_balance, currency),
            "balance_delta": canonical_money_str(after_balance - before_balance, currency)
            if before_balance is not None
            else None,
            "statistics_changed": False,
        }


def _material(expected, proposal, values, review_id, created_at):
    command = decode_command(
        strict_json(proposal["observation_json"])["observation"]["raw_proposal"]
    )
    return {
        "format": REVIEW_FORMAT,
        "binding": asdict(expected.binding),
        "review_id": review_id,
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "command_kind": expected.command_kind,
        "target_bundle_ref": expected.target_bundle_ref,
        "before_values": expected.before_values,
        "after_values": values,
        "instruction_ref": instruction_ref(expected.instruction),
        "response_ref": instruction_ref(expected.response),
        "reason_ref": _reason(
            command["reason_span"], expected, expected.command_kind == "opening_correct"
        ),
        "opening_dependency_closure": expected.dependencies,
        "opening_effects": review_effects(expected, values),
        "created_at": created_at,
        "expires_at": created_at + 600,
    }


def verify_review(service, conn, review):
    fingerprint = _fingerprint(conn)
    proposal = _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    expected = verify_proposal(service, conn, proposal)
    values = validate_bundle(strict_json(proposal["values_json"]))
    events = conn.execute(
        "SELECT * FROM l2_events WHERE kind='review' AND entity_id=?", (review["review_id"],)
    ).fetchall()
    material = strict_json(review["material_json"])
    if (
        len(events) != 1
        or material
        != _material(expected, proposal, values, review["review_id"], review["created_at"])
        or digest(material) != review["review_sha256"]
        or canonical(material) != events[0]["payload_json"]
        or review["record_id"] != expected.record_id
        or review["expected_version"] != expected.expected_version
        or review["values_json"] != proposal["values_json"]
    ):
        raise LifecycleError("Opening complete review differs")
    require_dependencies(conn, expected, events[0]["seq"])
    _require_prefix(conn, expected, proposal, "review", review_id=review["review_id"])
    delivery = conn.execute(
        "SELECT * FROM l2_deliveries WHERE review_id=?", (review["review_id"],)
    ).fetchone()
    if delivery:
        delivery_seq = _require_prefix(
            conn, expected, proposal, "delivery", review_id=review["review_id"]
        )
        require_dependencies(conn, expected, before_seq=delivery_seq)
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
            raise LifecycleError("Opening complete card delivery differs")
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Opening callback changed protected state")
    return expected, delivery


def verify_fact(service, conn, fact):
    review = _row(conn, "l2_reviews", "review_id", fact["review_id"])
    proposal = _row(conn, "l2_proposals", "proposal_id", review["proposal_id"])
    expected = decode_expected(strict_json(proposal["observation_json"])["expected"])
    _require_prefix(
        conn,
        expected,
        proposal,
        "confirm",
        review_id=review["review_id"],
        commit_seq=fact["commit_seq"],
    )
    require_dependencies(conn, expected, fact["commit_seq"])
    values = validate_bundle(strict_json(fact["values_json"]))
    if values["format"] != BUNDLE_V2 or values != compile_opening(
        decode_command(strict_json(proposal["observation_json"])["observation"]["raw_proposal"]),
        expected,
    ):
        raise LifecycleError("Opening fact differs from complete decision")
    decision = conn.execute(
        "SELECT action_evidence_id FROM l2_decisions WHERE record_id=? AND version=?",
        (fact["record_id"], fact["version"]),
    ).fetchone()
    if decision is None:
        raise LifecycleError("Opening fact lacks original decision")
    require_independent_action(conn, expected, proposal, decision[0])
    if values["transition"] == "opening_set":
        for old in conn.execute(
            "SELECT values_json FROM l2_facts WHERE record_id=? AND version<?",
            (fact["record_id"], fact["version"]),
        ):
            opening = validate_bundle(strict_json(old[0]))["opening"]
            if opening and opening["opening_event_id"] == values["opening"]["opening_event_id"]:
                raise LifecycleError("Closed opening identity cannot be reused")


class DailyOpeningChangeServiceV1(DailyAccountServiceV1):
    """Same original owner/transaction/finalizer; explicit v2 opening protocol."""

    def expected_opening_change_v2(
        self,
        account_id,
        expected_bundle_ref,
        reply_evidence_id,
        command_kind,
        *,
        opening_instruction_evidence_id=None,
    ):
        with self._read() as (service, conn):
            _audit(service, conn)
            fact = _row(conn, "l2_facts", "record_id", account_id)
            source = service._source(conn, fact["source_id"], allow_archived=True)
            previous = _previous(conn, fact["source_id"])
            expected = _compose(
                service,
                conn,
                source,
                expected_bundle_ref,
                reply_evidence_id,
                command_kind,
                opening_instruction_evidence_id or reply_evidence_id,
                previous,
            )
            _require_current(conn, expected)
            return expected

    def propose_opening_change_v2(
        self,
        account_id,
        observation_id,
        expected_bundle_ref,
        operation_id,
        *,
        reply_evidence_id,
        command_kind,
        opening_instruction_evidence_id=None,
    ):
        for value, label in (
            (observation_id, "opening observation"),
            (operation_id, "operation id"),
            (reply_evidence_id, "reply evidence"),
        ):
            text(value, label)
        with self._write() as (service, conn):
            _audit(service, conn)
            existing = conn.execute(
                "SELECT * FROM l2_proposals WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if existing:
                expected = verify_proposal(service, conn, existing)
                event = conn.execute(
                    "SELECT * FROM l2_events WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if (
                    event is None
                    or event["kind"] != "propose"
                    or event["entity_id"] != existing["proposal_id"]
                    or expected.record_id != account_id
                    or expected.target_bundle_ref != asdict(expected_bundle_ref)
                    or expected.reply_evidence_id != reply_evidence_id
                    or expected.command_kind != command_kind
                    or expected.instruction["evidence_id"]
                    != (opening_instruction_evidence_id or reply_evidence_id)
                    or expected.binding != service.binding
                ):
                    raise LifecycleError("Opening proposal replay mismatch")
                return ProposalRef(
                    existing["proposal_id"],
                    existing["source_id"],
                    existing["version"],
                    existing["proposal_sha256"],
                )
            if conn.execute(
                "SELECT 1 FROM l2_events WHERE operation_id=?", (operation_id,)
            ).fetchone():
                raise LifecycleError("Operation already used")
            if conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] >= MAX_PROPOSALS:
                raise LifecycleError("complete_read_limit_exceeded")
            fact = _row(conn, "l2_facts", "record_id", account_id)
            source = service._source(conn, fact["source_id"], allow_archived=True)
            expected = _compose(
                service,
                conn,
                source,
                expected_bundle_ref,
                reply_evidence_id,
                command_kind,
                opening_instruction_evidence_id or reply_evidence_id,
                _previous(conn, fact["source_id"]),
            )
            fingerprint = _fingerprint(conn)
            actual = service.port.opening_proposal_v2(conn, observation_id, expected)
            if (
                type(actual) is not VerifiedOpeningProposalV2
                or actual.format != OBSERVATION_FORMAT
                or actual.evidence_id != observation_id
                or actual.source_id != expected.source_id
                or actual.principal_id != service.binding.principal_id
                or actual.client_id != service.binding.client_id
                or actual.request_sha256 != digest(asdict(expected))
                or _fingerprint(conn) != fingerprint
            ):
                raise LifecycleError("Opening proposal evidence differs")
            values = compile_opening(decode_command(actual.raw_proposal), expected)
            _require_current(conn, expected)
            require_dependencies(conn, expected)
            version, proposal_id = expected.expected_proposal_version + 1, _id("opening-proposal")
            material = [
                expected.source_id,
                version,
                asdict(actual),
                values,
                expected.expected_version,
                expected.record_id,
            ]
            sealed = canonical(
                {
                    "format": OBSERVATION_FORMAT,
                    "expected": asdict(expected),
                    "observation": asdict(actual),
                }
            )
            strict_json(sealed)
            conn.execute(
                "INSERT INTO l2_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    expected.source_id,
                    version,
                    observation_id,
                    expected.expected_version,
                    expected.record_id,
                    canonical(values),
                    sealed,
                    digest(material),
                    service._now(),
                ),
            )
            _event(conn, operation_id, "propose", proposal_id, material)
            return ProposalRef(proposal_id, expected.source_id, version, digest(material))

    def opening_questions_v1(self, proposal_id):
        with self._read() as (service, conn):
            _audit(service, conn)
            proposal = _row(conn, "l2_proposals", "proposal_id", proposal_id)
            expected = verify_proposal(service, conn, proposal)
            _require_current_authority(service, conn, expected)
            _require_current(conn, expected, proposal=proposal)
            require_dependencies(conn, expected)
            values = strict_json(proposal["values_json"])
            candidate = values["candidate"] if values["format"] == INCOMPLETE_FORMAT else values
            if candidate is None:
                raise LifecycleError("Opening fields incomplete")
            material = list_material(expected.dependencies, candidate)
            return {
                "proposal_ref": asdict(
                    ProposalRef(
                        proposal["proposal_id"],
                        proposal["source_id"],
                        proposal["version"],
                        proposal["proposal_sha256"],
                    )
                ),
                "list_sha256": digest(material),
                **material,
            }

    def prepare_review(self, proposal_id, expected_proposal_version):
        with self._write() as (service, conn):
            _audit(service, conn)
            proposal = _row(conn, "l2_proposals", "proposal_id", proposal_id)
            expected = verify_proposal(service, conn, proposal)
            if proposal["version"] != expected_proposal_version:
                raise LifecycleError("stale_proposal_version")
            values = strict_json(proposal["values_json"])
            if values["format"] == INCOMPLETE_FORMAT:
                raise LifecycleError("incomplete_proposal: " + ",".join(values["missing_fields"]))
            existing = conn.execute(
                "SELECT * FROM l2_reviews WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if existing:
                verify_review(service, conn, existing)
                return _review_dto(conn, existing)
            _require_current_authority(service, conn, expected)
            _require_current(conn, expected, proposal=proposal)
            require_dependencies(conn, expected)
            review_id, now = _id("opening-review"), service._now()
            material = _material(expected, proposal, values, review_id, now)
            raw = canonical(material)
            strict_json(raw)
            conn.execute(
                "INSERT INTO l2_reviews VALUES(?,?,?,?,?,?,?,?)",
                (
                    review_id,
                    proposal_id,
                    expected.expected_version,
                    expected.record_id,
                    canonical(values),
                    raw,
                    digest(material),
                    now,
                ),
            )
            _event(conn, "prepare-" + review_id, "review", review_id, material)
            return _review_dto(conn, _row(conn, "l2_reviews", "review_id", review_id))

    def pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise LifecycleError("Bounded pending page required")
        with self._read() as (service, conn):
            _audit(service, conn)
            rows = conn.execute(
                "SELECT p.* FROM l2_proposals p "
                "WHERE json_extract(p.observation_json,'$.format')=? "
                "AND p.version=(SELECT max(version) FROM l2_proposals WHERE source_id=p.source_id) "
                "AND NOT EXISTS(SELECT 1 FROM l2_reviews r JOIN l2_decisions d "
                "ON d.review_id=r.review_id WHERE r.proposal_id=p.proposal_id) "
                "ORDER BY p.created_at DESC,p.proposal_id LIMIT ?",
                (OBSERVATION_FORMAT, limit),
            ).fetchall()
            return tuple(
                {
                    "proposal": asdict(
                        ProposalRef(
                            r["proposal_id"], r["source_id"], r["version"], r["proposal_sha256"]
                        )
                    ),
                    "values": strict_json(r["values_json"]),
                }
                for r in rows
            )


def require_current_opening(conn, expected, proposal, values, action_id):
    """Pure checks after the last external callback, before the only append."""
    require_dependencies(conn, expected)
    _require_current(conn, expected, proposal=proposal)
    command = decode_command(
        strict_json(proposal["observation_json"])["observation"]["raw_proposal"]
    )
    if compile_opening(command, expected) != values:
        raise LifecycleError("Opening treatment or question/answer binding changed")
    require_independent_action(conn, expected, proposal, action_id)


def require_independent_action(conn, expected, proposal, action_id):
    link = strict_json(expected.instruction_link["link_json"])
    used = {
        expected.instruction["evidence_id"],
        expected.response["evidence_id"],
        link["target"]["source_instruction_ref"]["evidence_id"],
        expected.instruction_link["evidence_id"],
    }
    for row in conn.execute(
        "SELECT observation_json FROM l2_proposals WHERE source_id=? AND version<=?",
        (expected.source_id, proposal["version"]),
    ):
        envelope = strict_json(row[0])
        prior = envelope.get("expected", {})
        if (
            envelope.get("format") == OBSERVATION_FORMAT
            and prior.get("instruction") == expected.instruction
        ):
            used.add(prior["reply_evidence_id"])
    if action_id in used:
        raise LifecycleError("Opening confirmation must be independent of instruction/answers")
