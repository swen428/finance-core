"""Exact opt-in income/expense values; original and account money stay distinct."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from finance_core.money import normalize_currency

from ..contracts import CommitReceipt, LifecycleError, RuntimeBinding, canonical, digest
from .contracts import (
    AccountDefinitionRefV1,
    BundleFactRefV1,
    OpeningRefV1,
    _money,
    _reason,
    _reference,
    account_ref,
    exact,
    instruction_ref,
    opening_ref,
    positive,
    sha,
    strict_json,
    text,
    validate_as_of,
    validate_bundle,
)

MOVEMENT_FORMAT = "finance-daily-movement-v1"
OBSERVATION_FORMAT = "finance-daily-movement-observation-v1"
REVIEW_FORMAT = "finance-daily-movement-review-v1"
COMMAND_FORMAT = "finance-daily-movement-command-v1"
INCOMPLETE_FORMAT = "finance-daily-movement-incomplete-v1"
CORRECTION_OPENING_LINK = "finance-daily-movement-instruction-link-v2"
TARGET_SELECTION_LINK = "finance-daily-movement-instruction-link-v3"
CORRECTION_OPENING_BASIS = "authenticated_correction_opening_reply"
COMMANDS = frozenset({"movement_create", "movement_correct", "movement_void"})
FIELDS = frozenset(
    {
        "kind",
        "business_date",
        "description",
        "category",
        "original_amount",
        "original_currency",
        "account_id",
        "account_amount",
        "funds_time",
        "coverage",
    }
)


def movement_record_id_v1(source_id):
    """Pure initial identity, available before independent link registration."""
    text(source_id, "captured movement source id")
    return "movement-" + digest(["finance-daily-movement-id-v1", source_id])


@dataclass(frozen=True)
class ExpectedMovementInvocationV1:
    binding: RuntimeBinding
    source_id: str
    source_sha256: str
    expected_version: int
    record_id: str
    command_kind: str
    before_values: dict | None
    target_bundle_ref: dict | None
    instruction: dict
    accounts: list
    instruction_link: dict
    current_proposal_id: str | None = None
    reply_evidence_id: str | None = None
    expected_proposal_version: int = 0
    format: str = "finance-daily-movement-expected-v1"


@dataclass(frozen=True)
class MovementInstructionTargetV1:
    binding: RuntimeBinding
    source_id: str
    source_sha256: str
    source_instruction_ref: dict
    instruction_ref: dict
    record_id: str
    command_kind: str
    current_proposal: dict | None
    target_fact_ref: dict | None


@dataclass(frozen=True)
class VerifiedMovementInstructionLinkV1:
    evidence_id: str
    link_json: str


def _evidence_ref(value):
    exact(value, {"evidence_id", "sha256"}, "association evidence reference")
    text(value["evidence_id"], "association evidence id")
    sha(value["sha256"])


def question_target(target):
    return {key: value for key, value in target.items() if key != "instruction_ref"}


def coverage_subject(values, account):
    opening = account["opening"]
    return {
        "account_ref": asdict(account_ref(account)),
        "opening_ref": asdict(opening_ref(account))
        if opening and opening["status"] == "ACTIVE"
        else None,
        **{
            key: values[key]
            for key in (
                "kind",
                "original_amount",
                "original_currency",
                "account_amount",
                "account_currency",
                "business_date",
                "funds_time",
            )
        },
    }


def _validate_account_question(subject, target):
    if target["command_kind"] != "movement_create" or target["current_proposal"] is None:
        raise LifecycleError("Account selection requires an unfinished movement creation")
    exact(subject, {"draft_fields", "offered_accounts", "selected_account"}, "account question")
    fields = subject["draft_fields"]
    if (
        type(fields) is not dict
        or not fields.keys() <= FIELDS
        or fields.get("kind") != "expense"
        or fields.get("account_id") is not None
        or fields.get("coverage") not in (None, {"answer": "unknown", "span": None})
    ):
        raise LifecycleError("Account selection requires an expense draft without an account")
    if type(subject["offered_accounts"]) is not list or not subject["offered_accounts"]:
        raise LifecycleError("Account question must offer actual accounts")
    ref_fields = AccountDefinitionRefV1.__dataclass_fields__
    offered = [
        AccountDefinitionRefV1(**exact(ref, ref_fields, "offered account reference"))
        for ref in subject["offered_accounts"]
    ]
    selected = AccountDefinitionRefV1(
        **exact(subject["selected_account"], ref_fields, "selected account reference")
    )
    if len({ref.account_id for ref in offered}) != len(offered) or selected not in offered:
        raise LifecycleError("Selected account must be one of the accounts offered")


def _validate_target_selection(selection, target, association):
    if target["command_kind"] not in {"movement_correct", "movement_void"}:
        raise LifecycleError("Target selection requires a movement correction or reversal")
    exact(selection, {"reply_ref", "offered_facts", "choice"}, "movement target selection")
    reply = selection["reply_ref"]
    _reference(reply)
    original = target["instruction_ref"]
    captured = target["source_instruction_ref"]
    if (
        reply["evidence_id"] in {original["evidence_id"], captured["evidence_id"]}
        or reply["source_key"] in {original["source_key"], captured["source_key"]}
        or reply["principal_id"] != original["principal_id"]
        or reply["client_id"] != original["client_id"]
        or reply["source_timestamp"] < original["source_timestamp"]
        or association["evidence_id"]
        in {original["evidence_id"], captured["evidence_id"], reply["evidence_id"]}
    ):
        raise LifecycleError("Target selection requires a separate reply to a delivered menu")
    offered = selection["offered_facts"]
    choice = selection["choice"]
    if (
        type(offered) is not list
        or not 2 <= len(offered) <= 8
        or type(choice) is not int
        or not 1 <= choice <= len(offered)
    ):
        raise LifecycleError("Target selection requires an exact choice from two to eight records")
    refs = [
        BundleFactRefV1(**exact(ref, BundleFactRefV1.__dataclass_fields__, "offered movement fact"))
        for ref in offered
    ]
    if (
        len({ref.record_id for ref in refs}) != len(refs)
        or offered[choice - 1] != target["target_fact_ref"]
    ):
        raise LifecycleError("Selected movement differs from the offered fact version")


def decode_instruction_link(value, target=None):
    exact(value, {"evidence_id", "link_json"}, "verified movement instruction link")
    text(value["evidence_id"], "instruction link id")
    material = strict_json(value["link_json"])
    if canonical(material) != value["link_json"]:
        raise LifecycleError("Canonical movement instruction link required")
    keys = {"format", "mode", "target", "association_ref", "question"}
    supplement = type(material) is dict and material.get("format") == CORRECTION_OPENING_LINK
    selecting = type(material) is dict and material.get("format") == TARGET_SELECTION_LINK
    if supplement:
        keys.add("correction")
    if selecting:
        keys.add("selection")
    exact(material, keys, "instruction link")
    modes = (
        {"target_selection"}
        if selecting
        else {"initial_instruction", "direct_targeted_instruction", "question_reply"}
    )
    if (
        (
            not (supplement or selecting)
            and material["format"] != "finance-daily-movement-instruction-link-v1"
        )
        or type(material["mode"]) is not str
        or material["mode"] not in modes
    ):
        raise LifecycleError("Unsupported movement instruction association")
    actual_target = material["target"]
    exact(actual_target, MovementInstructionTargetV1.__dataclass_fields__, "instruction target")
    RuntimeBinding(**actual_target["binding"])
    text(actual_target["source_id"], "linked source id")
    sha(actual_target["source_sha256"])
    _reference(actual_target["source_instruction_ref"])
    _reference(actual_target["instruction_ref"])
    text(actual_target["record_id"], "linked record id")
    if (
        type(actual_target["command_kind"]) is not str
        or actual_target["command_kind"] not in COMMANDS
    ):
        raise LifecycleError("Unsupported linked movement command")
    previous = actual_target["current_proposal"]
    if previous is not None:
        exact(previous, {"proposal_id", "version", "proposal_sha256"}, "linked proposal")
        text(previous["proposal_id"], "linked proposal id")
        positive(previous["version"])
        sha(previous["proposal_sha256"])
    if actual_target["target_fact_ref"] is not None:
        BundleFactRefV1(**actual_target["target_fact_ref"])
    if supplement:
        if (
            material["mode"] != "question_reply"
            or actual_target["command_kind"] != "movement_correct"
            or previous is None
            or actual_target["target_fact_ref"] is None
        ):
            raise LifecycleError("Opening supplement requires an exact pending correction")
        correction = material["correction"]
        exact(correction, {"fields", "reason_ref"}, "opening correction source")
        if (
            type(correction["fields"]) is not dict
            or not correction["fields"].keys() <= FIELDS - {"coverage"}
            or correction["reason_ref"] is None
        ):
            raise LifecycleError("Opening supplement requires the original correction and reason")
        _validate_reason_ref(correction["reason_ref"])
        original = correction["reason_ref"]["instruction_ref"]
        reply = actual_target["instruction_ref"]
        if (
            original["evidence_id"]
            in {reply["evidence_id"], actual_target["source_instruction_ref"]["evidence_id"]}
            or original["principal_id"] != reply["principal_id"]
            or original["client_id"] != reply["client_id"]
            or original["source_timestamp"] > reply["source_timestamp"]
        ):
            raise LifecycleError("Opening supplement requires separate authenticated instructions")
    if target is not None and actual_target != asdict(target):
        raise LifecycleError("movement_instruction_target_mismatch")
    _evidence_ref(material["association_ref"])
    initial = actual_target["instruction_ref"] == actual_target["source_instruction_ref"]
    if material["mode"] == "initial_instruction":
        if not initial or previous is not None or actual_target["target_fact_ref"] is not None:
            raise LifecycleError("Initial instruction cannot borrow a question or prior target")
        if material["association_ref"] != {
            "evidence_id": actual_target["instruction_ref"]["evidence_id"],
            "sha256": actual_target["instruction_ref"]["original_sha256"],
        }:
            raise LifecycleError("Initial association must be the actual captured source")
    elif initial:
        raise LifecycleError("Supplement/change requires independently linked instruction")
    question = material["question"]
    if material["mode"] == "question_reply":
        if actual_target["command_kind"] == "movement_void":
            raise LifecycleError("A movement question cannot instruct whole-event VOID")
        exact(
            question,
            {"question_id", "question_version", "delivery_ref", "purpose", "target", "subject"},
            "movement question",
        )
        text(question["question_id"], "question id")
        positive(question["question_version"])
        _evidence_ref(question["delivery_ref"])
        if question["target"] != question_target(actual_target):
            raise LifecycleError("movement_question_target_mismatch")
        subject = question["subject"]
        if question["purpose"] == "account_selection":
            if supplement:
                raise LifecycleError("Correction supplement requires an opening coverage question")
            _validate_account_question(subject, actual_target)
            return material
        if question["purpose"] != "opening_coverage":
            raise LifecycleError("unsupported_movement_question_purpose")
        exact(
            subject,
            {
                "account_ref",
                "opening_ref",
                "kind",
                "original_amount",
                "original_currency",
                "account_amount",
                "account_currency",
                "business_date",
                "funds_time",
            },
            "opening coverage subject",
        )
        account = AccountDefinitionRefV1(**subject["account_ref"])
        if (
            subject["opening_ref"] is not None
            and OpeningRefV1(**subject["opening_ref"]).account_id != account.account_id
        ):
            raise LifecycleError("Question account/opening mismatch")
        if type(subject["kind"]) is not str or subject["kind"] not in {"income", "expense"}:
            raise LifecycleError("Unsupported question financial kind")
        for amount, currency in (
            ("original_amount", "original_currency"),
            ("account_amount", "account_currency"),
        ):
            if (
                normalize_currency(subject[currency]) != subject[currency]
                or _positive_money(subject[amount], subject[currency]) != subject[amount]
            ):
                raise LifecycleError("Noncanonical question subject money")
        _day(subject["business_date"])
        validate_as_of(subject["funds_time"])
    elif question is not None:
        raise LifecycleError("Direct/initial instruction cannot invent a question")
    if selecting:
        _validate_target_selection(
            material["selection"], actual_target, material["association_ref"]
        )
    return material


def reuses_target_selection_evidence(expected, action_evidence_id):
    """True if an action attempts to use the delivered menu or its choice again."""
    material = strict_json(expected.instruction_link["link_json"])
    return material["format"] == TARGET_SELECTION_LINK and action_evidence_id in {
        material["association_ref"]["evidence_id"],
        material["selection"]["reply_ref"]["evidence_id"],
    }


@dataclass(frozen=True)
class VerifiedMovementProposalV1:
    evidence_id: str
    source_id: str
    principal_id: str
    client_id: str
    request_sha256: str
    raw_proposal: str
    format: str = OBSERVATION_FORMAT


@dataclass(frozen=True)
class DailyMovementReceiptV1:
    receipt: CommitReceipt
    command_kind: str
    status: str


def decode_command(raw):
    value = strict_json(raw)
    exact(value, {"format", "command_kind", "fields", "reason_span"}, "movement command")
    if (
        value["format"] != COMMAND_FORMAT
        or type(value["command_kind"]) is not str
        or value["command_kind"] not in COMMANDS
    ):
        raise LifecycleError("Unsupported movement command")
    if type(value["fields"]) is not dict or set(value["fields"]) - FIELDS:
        raise LifecycleError(
            "Unsupported movement fields (transfer/refund/fee/repayment unsupported)"
        )
    return value


def validate_before(value):
    if value.get("format") == MOVEMENT_FORMAT:
        return validate_movement(value)
    from ..effective_v1 import _business_fields
    from ..service import _money as legacy_money

    legacy_money(value)
    _business_fields(canonical(value))
    return value


def decode_expected(value):
    exact(value, ExpectedMovementInvocationV1.__dataclass_fields__, "movement expected")
    raw = dict(value)
    raw["binding"] = RuntimeBinding(**raw["binding"])
    expected = ExpectedMovementInvocationV1(**raw)
    if (
        expected.format != "finance-daily-movement-expected-v1"
        or type(expected.command_kind) is not str
        or expected.command_kind not in COMMANDS
    ):
        raise LifecycleError("Unsupported movement expected")
    text(expected.record_id, "record id")
    text(expected.source_id, "source id")
    sha(expected.source_sha256)
    instruction_ref(expected.instruction, expected.binding)
    link = decode_instruction_link(expected.instruction_link)
    target = link["target"]
    if (
        target["binding"] != asdict(expected.binding)
        or target["source_id"] != expected.source_id
        or target["source_sha256"] != expected.source_sha256
        or target["record_id"] != expected.record_id
        or target["command_kind"] != expected.command_kind
        or target["instruction_ref"] != instruction_ref(expected.instruction)
        or target["target_fact_ref"] != expected.target_bundle_ref
        or (target["current_proposal"]["proposal_id"] if target["current_proposal"] else None)
        != expected.current_proposal_id
        or (target["current_proposal"]["version"] if target["current_proposal"] else 0)
        != expected.expected_proposal_version
    ):
        raise LifecycleError("Stored movement instruction link differs from expected context")
    if any(
        type(v) is not int or v < 0
        for v in (expected.expected_version, expected.expected_proposal_version)
    ):
        raise LifecycleError("Exact nonnegative versions required")
    if type(expected.accounts) is not list:
        raise LifecycleError("Exact account inventory required")
    ids = []
    for item in expected.accounts:
        exact(item, {"fact_ref", "values"}, "account inventory item")
        ref = BundleFactRefV1(**item["fact_ref"])
        validate_bundle(item["values"])
        if ref.record_id != item["values"]["account"]["account_id"]:
            raise LifecycleError("Account inventory identity mismatch")
        ids.append(ref.record_id)
    if ids != sorted(set(ids)):
        raise LifecycleError("Account inventory must be unique and ordered")
    if expected.before_values is not None:
        validate_before(expected.before_values)
        ref = BundleFactRefV1(**expected.target_bundle_ref)
        if ref.record_id != expected.record_id or ref.version != expected.expected_version:
            raise LifecycleError("Movement target mismatch")
    elif expected.expected_version != 0 or expected.target_bundle_ref is not None:
        raise LifecycleError("Initial movement cannot claim predecessor")
    return expected


def _positive_money(raw, currency):
    result = _money(raw, currency)
    if Decimal(result) <= 0:
        raise LifecycleError("Movement amounts must be strictly positive")
    return result


def _day(value):
    if type(value) is not str:
        raise LifecycleError("Exact business date required")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError as exc:
        raise LifecycleError("Exact business date required") from exc
    return value


def _later(funds, boundary):
    if funds["precision"] == "instant" and boundary["precision"] == "instant":
        return funds["timestamp"] > boundary["timestamp"]
    if funds["precision"] != "unknown" and boundary["precision"] != "unknown":
        return funds["local_date"] > boundary["local_date"]
    return False


def _coverage(command, expected, values, account, question, *, require_whole_choice=False):
    opening = account["opening"]
    active = opening is not None and opening["status"] == "ACTIVE"
    supplied = command["fields"].get("coverage")
    if supplied is not None:
        exact(supplied, {"answer", "span"}, "opening coverage reply")
        if type(supplied["answer"]) is not str or supplied["answer"] not in {
            "included",
            "excluded",
            "unknown",
        }:
            raise LifecycleError("Unsupported opening coverage answer")
    answer = supplied["answer"] if supplied else "unknown"
    account_question = question is not None and question["purpose"] == "account_selection"
    accepted = {
        "included": {"included", "already included", "已包含", "已经包含"},
        "excluded": {"excluded", "not included", "尚未包含", "未包含"},
    }
    choices = {phrase: choice for choice, phrases in accepted.items() for phrase in phrases}
    raw = expected.instruction["raw_input"]
    whole_choice = None if account_question else choices.get(raw.strip().casefold())
    if whole_choice is not None and question is None:
        raise LifecycleError("Bare opening coverage reply requires actual linked question")
    if (
        question is not None
        and not account_question
        and question["subject"] != coverage_subject(values, account)
    ):
        return None
    reason = None
    if supplied and answer != "unknown":
        if require_whole_choice and whole_choice != answer:
            raise LifecycleError("Correction opening reply requires a complete coverage choice")
        reason = _reason(supplied["span"], expected, True)
        quote = reason["quote"].casefold()
        if quote not in accepted[answer] or (whole_choice is not None and whole_choice != answer):
            raise LifecycleError("Coverage answer must match the complete genuine choice")
        start, end = supplied["span"]
        if quote.isascii() and (
            (start and (raw[start - 1].isalnum() or raw[start - 1] == "_"))
            or (end < len(raw) and (raw[end].isalnum() or raw[end] == "_"))
        ):
            raise LifecycleError("Coverage selection must retain complete choice words")
        # A fixed, bounded negator/choice rule rejects omitted negation. It
        # does not infer answers from arbitrary natural-language sentences.
        import re

        for phrase in choices:
            pattern = r"(?<!\w)not\s+" + re.escape(phrase) + r"(?!\w)"
            for match in re.finditer(pattern, raw, flags=re.IGNORECASE):
                if match.start() < start and end <= match.end():
                    raise LifecycleError("Coverage selection cannot discard a negative prefix")
    if not active:
        treatment, basis = "no_baseline", "no_active_opening"
    elif _later(values["funds_time"], opening["as_of"]):
        if answer == "included":
            raise LifecycleError("Coverage answer contradicts strictly later funds time")
        treatment, basis = "excluded", "strictly_later"
    elif answer in {"included", "excluded"}:
        treatment, basis = answer, "authenticated_reply"
    else:
        return None
    material = {
        "account_ref": asdict(account_ref(account)),
        "opening_ref": asdict(opening_ref(account)) if active else None,
        "kind": values["kind"],
        "business_date": values["business_date"],
        "original_amount": values["original_amount"],
        "original_currency": values["original_currency"],
        "account_amount": values["account_amount"],
        "funds_time": values["funds_time"],
        "instruction_ref": instruction_ref(expected.instruction),
    }
    return {
        "treatment": treatment,
        "basis": basis,
        "reply_ref": reason,
        "binding": material,
        "binding_sha256": digest(material),
    }


def without_coverage(fields):
    return {key: value for key, value in fields.items() if key != "coverage"}


def compile_proposal(command, expected):
    if command["command_kind"] != expected.command_kind:
        raise LifecycleError("Movement command differs from request")
    before = expected.before_values
    link = decode_instruction_link(expected.instruction_link)
    correction = link.get("correction")
    if correction is not None:
        if command["reason_span"] is not None or canonical(
            without_coverage(command["fields"])
        ) != canonical(correction["fields"]):
            raise LifecycleError(
                "Opening reply can only supply coverage for the pending correction"
            )
        reason = correction["reason_ref"]
    elif command["reason_span"] is None and expected.command_kind != "movement_create":
        return {
            "format": INCOMPLETE_FORMAT,
            "missing_fields": ["reason_span"],
            "command": command,
            "instruction_ref": instruction_ref(expected.instruction),
        }
    else:
        reason = _reason(
            command["reason_span"], expected, expected.command_kind != "movement_create"
        )
    if before and before.get("format") == MOVEMENT_FORMAT and before["status"] == "VOID":
        raise LifecycleError("movement_already_void")
    if expected.command_kind == "movement_void":
        if not before:
            raise LifecycleError("VOID requires current event")
        if command["fields"]:
            raise LifecycleError("Whole-event VOID cannot modify business values")
        if before.get("format") != MOVEMENT_FORMAT:
            # A legacy unknown-account event has no account impact to undo.
            from ..effective_v1 import _business_fields

            old = _business_fields(canonical(before))
            return validate_movement(
                {
                    "format": MOVEMENT_FORMAT,
                    "status": "VOID",
                    "transition": expected.command_kind,
                    "kind": "expense",
                    "business_date": old["transaction_date"],
                    "description": old["merchant"],
                    "category": old["category"] or "uncategorized",
                    "original_amount": old["amount"],
                    "original_currency": old["currency"],
                    "account_id": None,
                    "account_amount": None,
                    "account_currency": None,
                    "funds_time": None,
                    "account_ref": None,
                    "opening_ref": None,
                    "coverage": None,
                    "instruction_ref": instruction_ref(expected.instruction),
                    "reason_ref": reason,
                }
            )
        return validate_movement(
            {
                **before,
                "status": "VOID",
                "transition": expected.command_kind,
                "instruction_ref": instruction_ref(expected.instruction),
                "reason_ref": reason,
            }
        )
    f = command["fields"]
    question = link["question"]
    selection = (
        question["subject"] if question and question["purpose"] == "account_selection" else None
    )
    if selection is not None:
        selected_fields = {
            **selection["draft_fields"],
            "account_id": selection["selected_account"]["account_id"],
        }
        if canonical(f) != canonical(selected_fields) or command["reason_span"] is not None:
            raise LifecycleError("Account reply can only set the account selected by the person")
    required = FIELDS - {"coverage", "original_amount", "original_currency"}
    missing = [k for k in sorted(required) if f.get(k) is None]
    if f.get("kind") is not None and (
        type(f["kind"]) is not str or f["kind"] not in {"income", "expense"}
    ):
        raise LifecycleError(
            "Only ordinary income/expense supported; transfer/refund/fee/repayment refused"
        )
    if f.get("kind") == "expense":
        missing += [k for k in ("original_amount", "original_currency") if f.get(k) is None]
    if missing:
        return {
            "format": INCOMPLETE_FORMAT,
            "missing_fields": sorted(set(missing)),
            "command": command,
            "instruction_ref": instruction_ref(expected.instruction),
        }
    account = next(
        (
            a["values"]
            for a in expected.accounts
            if a["values"]["account"]["account_id"] == f["account_id"]
        ),
        None,
    )
    if account is None:
        raise LifecycleError("Exact current account selection required")
    currency = account["account"]["currency"]
    if selection is not None and (
        asdict(account_ref(account)) != selection["selected_account"]
        or currency != f["original_currency"]
    ):
        raise LifecycleError("Account reply requires the current selected same-currency account")
    original_currency = f.get("original_currency") if f["kind"] == "expense" else currency
    if normalize_currency(original_currency) != original_currency:
        raise LifecycleError("Exact uppercase currency required")
    amount = _positive_money(f["account_amount"], currency)
    if f["kind"] == "income" and (
        (f.get("original_currency") is not None and f["original_currency"] != currency)
        or (
            f.get("original_amount") is not None
            and _positive_money(f["original_amount"], currency) != amount
        )
    ):
        raise LifecycleError(
            "Income has only actual credited money; conflicting original amount refused"
        )
    values = {
        "format": MOVEMENT_FORMAT,
        "status": "ACTIVE",
        "transition": expected.command_kind,
        "kind": f["kind"],
        "business_date": _day(f["business_date"]),
        "description": text(f["description"], "description", 1024),
        "category": text(f["category"], "category"),
        "original_amount": _positive_money(f["original_amount"], original_currency)
        if f["kind"] == "expense"
        else amount,
        "original_currency": original_currency,
        "account_id": f["account_id"],
        "account_amount": amount,
        "account_currency": currency,
        "funds_time": validate_as_of(f["funds_time"]),
        "account_ref": asdict(account_ref(account)),
        "opening_ref": asdict(opening_ref(account))
        if account["opening"] and account["opening"]["status"] == "ACTIVE"
        else None,
        "instruction_ref": (
            reason["instruction_ref"]
            if correction is not None
            else instruction_ref(expected.instruction)
        ),
        "reason_ref": reason,
    }
    coverage = _coverage(
        command, expected, values, account, question, require_whole_choice=correction is not None
    )
    if correction is not None:
        if coverage is None or coverage["basis"] != "authenticated_reply":
            raise LifecycleError("Correction opening reply must answer the exact current question")
        coverage["basis"] = CORRECTION_OPENING_BASIS
    if coverage is None:
        return {
            "format": INCOMPLETE_FORMAT,
            "missing_fields": ["opening_coverage"],
            "command": command,
            "instruction_ref": instruction_ref(expected.instruction),
        }
    return validate_movement({**values, "coverage": coverage})


def _validate_reason_ref(reason):
    if reason is None:
        return
    exact(reason, {"format", "instruction_ref", "span", "quote", "quote_sha256"}, "reason ref")
    _reference(reason["instruction_ref"])
    text(reason["quote"], "reason", 1024)
    span = reason["span"]
    if (
        reason["format"] != "finance-daily-reason-ref-v1"
        or type(span) is not list
        or len(span) != 2
        or any(type(n) is not int for n in span)
        or not 0 <= span[0] < span[1]
        or span[1] - span[0] != len(reason["quote"])
        or hashlib.sha256(reason["quote"].encode()).hexdigest() != reason["quote_sha256"]
    ):
        raise LifecycleError("Invalid genuine reason reference")


def validate_movement(value):
    exact(
        value,
        {
            "format",
            "status",
            "transition",
            "kind",
            "business_date",
            "description",
            "category",
            "original_amount",
            "original_currency",
            "account_id",
            "account_amount",
            "account_currency",
            "funds_time",
            "account_ref",
            "opening_ref",
            "coverage",
            "instruction_ref",
            "reason_ref",
        },
        "movement fact",
    )
    if (
        value["format"] != MOVEMENT_FORMAT
        or any(type(value[k]) is not str for k in ("status", "transition", "kind"))
        or value["status"] not in {"ACTIVE", "VOID"}
        or value["transition"] not in COMMANDS
        or value["kind"] not in {"expense", "income"}
        or (value["status"] == "VOID") != (value["transition"] == "movement_void")
    ):
        raise LifecycleError("Invalid movement domain/status")
    _day(value["business_date"])
    text(value["description"], "description", 1024)
    text(value["category"], "category")
    if (
        _positive_money(value["original_amount"], value["original_currency"])
        != value["original_amount"]
    ):
        raise LifecycleError("Noncanonical original money")
    _reference(value["instruction_ref"])
    _validate_reason_ref(value["reason_ref"])
    if (
        value["reason_ref"] is not None
        and value["reason_ref"]["instruction_ref"] != value["instruction_ref"]
    ):
        raise LifecycleError("Reason belongs to another instruction")
    if normalize_currency(value["original_currency"]) != value["original_currency"]:
        raise LifecycleError("Noncanonical original currency")
    if value["account_id"] is None:
        if value["status"] != "VOID" or any(
            value[k] is not None
            for k in (
                "account_amount",
                "account_currency",
                "funds_time",
                "account_ref",
                "opening_ref",
                "coverage",
            )
        ):
            raise LifecycleError("Only legacy VOID may retain unknown account")
    else:
        from .contracts import AccountDefinitionRefV1, OpeningRefV1

        ref = AccountDefinitionRefV1(**value["account_ref"])
        if ref.account_id != value["account_id"]:
            raise LifecycleError("Movement account mismatch")
        if (
            value["opening_ref"] is not None
            and OpeningRefV1(**value["opening_ref"]).account_id != ref.account_id
        ):
            raise LifecycleError("Movement opening mismatch")
        if (
            _positive_money(value["account_amount"], value["account_currency"])
            != value["account_amount"]
        ):
            raise LifecycleError("Noncanonical actual account money")
        if value["kind"] == "income" and (value["account_amount"], value["account_currency"]) != (
            value["original_amount"],
            value["original_currency"],
        ):
            raise LifecycleError("Income must retain actual credited money")
        validate_as_of(value["funds_time"])
        c = value["coverage"]
        exact(c, {"treatment", "basis", "reply_ref", "binding", "binding_sha256"}, "coverage")
        binding = c["binding"]
        exact(
            binding,
            {
                "account_ref",
                "opening_ref",
                "kind",
                "business_date",
                "original_amount",
                "original_currency",
                "account_amount",
                "funds_time",
                "instruction_ref",
            },
            "coverage binding",
        )
        if (
            c["treatment"] not in {"included", "excluded", "no_baseline"}
            or c["basis"]
            not in {
                "no_active_opening",
                "strictly_later",
                "authenticated_reply",
                CORRECTION_OPENING_BASIS,
            }
            or digest(binding) != c["binding_sha256"]
            or any(binding[k] != value[k] for k in binding if k != "instruction_ref")
            or (
                value["status"] == "ACTIVE"
                and c["basis"] != CORRECTION_OPENING_BASIS
                and binding["instruction_ref"] != value["instruction_ref"]
            )
            or (c["treatment"] == "no_baseline") != (value["opening_ref"] is None)
            or (c["basis"] == "no_active_opening") != (c["treatment"] == "no_baseline")
            or (c["basis"] == "strictly_later" and c["treatment"] != "excluded")
            or (
                c["treatment"] == "included"
                and c["basis"] not in {"authenticated_reply", CORRECTION_OPENING_BASIS}
            )
        ):
            raise LifecycleError("Invalid coverage binding")
        _reference(binding["instruction_ref"])
        _validate_reason_ref(c["reply_ref"])
        if (
            c["basis"] == CORRECTION_OPENING_BASIS
            and value["status"] == "ACTIVE"
            and (
                value["transition"] != "movement_correct"
                or binding["instruction_ref"]["evidence_id"]
                == value["instruction_ref"]["evidence_id"]
                or any(
                    binding["instruction_ref"][key] != value["instruction_ref"][key]
                    for key in ("principal_id", "client_id")
                )
            )
        ):
            raise LifecycleError("Correction coverage must bind a separate reply")
        if c["basis"] in {"authenticated_reply", CORRECTION_OPENING_BASIS} and (
            c["reply_ref"] is None
            or c["reply_ref"]["instruction_ref"] != binding["instruction_ref"]
        ):
            raise LifecycleError("Coverage lacks exact reply identity")
    if value["transition"] != "movement_create" and value["reason_ref"] is None:
        raise LifecycleError("Genuine reason required")
    return value
