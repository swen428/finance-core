"""Exact opening-only dependency decisions; old component bytes stay intact."""

from dataclasses import asdict, dataclass
from types import SimpleNamespace

from ..contracts import LifecycleError, RuntimeBinding, canonical, digest
from .contracts import (
    BundleFactRefV1,
    ExpectedDailyInvocationV1,
    OpeningRefV1,
    _reason,
    account_ref,
    compile_proposal,
    exact,
    instruction_ref,
    opening_ref,
    positive,
    sha,
    strict_json,
    text,
    validate_bundle,
)
from .movement_contracts import _evidence_ref, _later, validate_movement

BUNDLE_V2 = "finance-daily-account-bundle-v2"
MAP_FORMAT = "finance-daily-opening-dependency-map-v1"
REGISTRY = "finance-daily-account-opening-ordinary-income-expense-v1"
EXPECTED_FORMAT = "finance-daily-opening-expected-v2"
OBSERVATION_FORMAT = "finance-daily-opening-observation-v2"
COMMAND_FORMAT = "finance-daily-opening-command-v2"
INCOMPLETE_FORMAT = "finance-daily-opening-incomplete-v2"
REVIEW_FORMAT = "finance-daily-opening-review-v2"


@dataclass(frozen=True)
class ExpectedOpeningInvocationV2(ExpectedDailyInvocationV1):
    response: dict | None = None
    instruction_link: dict | None = None
    dependencies: list | None = None
    retained_answers: list | None = None
    original_command: dict | None = None
    format: str = EXPECTED_FORMAT


@dataclass(frozen=True)
class OpeningInstructionTargetV1:
    binding: RuntimeBinding
    source_id: str
    source_sha256: str
    source_instruction_ref: dict
    instruction_ref: dict
    response_ref: dict
    record_id: str
    command_kind: str
    current_proposal: dict | None
    target_fact_ref: dict


@dataclass(frozen=True)
class VerifiedOpeningInstructionLinkV1:
    evidence_id: str
    link_json: str


@dataclass(frozen=True)
class VerifiedOpeningProposalV2:
    evidence_id: str
    source_id: str
    principal_id: str
    client_id: str
    request_sha256: str
    raw_proposal: str
    format: str = OBSERVATION_FORMAT


def decode_command(raw):
    command = strict_json(raw)
    exact(
        command, {"format", "command_kind", "fields", "reason_span", "answers"}, "opening command"
    )
    if command["format"] != COMMAND_FORMAT or command["command_kind"] not in {
        "opening_set",
        "opening_correct",
    }:
        raise LifecycleError("Unsupported opening command")
    if type(command["answers"]) is not list or len(command["answers"]) > 128:
        raise LifecycleError("Bounded opening answers required")
    return command


def base_expected(expected):
    return ExpectedDailyInvocationV1(
        **{
            **{k: getattr(expected, k) for k in ExpectedDailyInvocationV1.__dataclass_fields__},
            "format": "finance-daily-expected-invocation-v1",
        }
    )


def decode_expected(raw):
    exact(raw, ExpectedOpeningInvocationV2.__dataclass_fields__, "opening expected")
    value = dict(raw)
    value["binding"] = RuntimeBinding(**value["binding"])
    expected = ExpectedOpeningInvocationV2(**value)
    from .contracts import decode_expected as decode_base

    decode_base(asdict(base_expected(expected)))
    if expected.format != EXPECTED_FORMAT or expected.command_kind not in {
        "opening_set",
        "opening_correct",
    }:
        raise LifecycleError("Unsupported opening request")
    instruction_ref(expected.response, expected.binding)
    if expected.response["evidence_id"] != expected.reply_evidence_id:
        raise LifecycleError("Opening response identity differs")
    if type(expected.dependencies) is not list or len(expected.dependencies) > 128:
        raise LifecycleError("Bounded complete opening dependencies required")
    if type(expected.retained_answers) is not list:
        raise LifecycleError("Exact retained answers required")
    return expected


def decode_link(value, target):
    exact(value, {"evidence_id", "link_json"}, "opening instruction link")
    text(value["evidence_id"], "opening link id")
    material = strict_json(value["link_json"])
    exact(
        material,
        {"format", "mode", "target", "association_ref", "question", "selections"},
        "opening link material",
    )
    if material["format"] != "finance-daily-opening-instruction-link-v1" or material[
        "target"
    ] != asdict(target):
        raise LifecycleError("opening_instruction_target_mismatch")
    if canonical(material) != value["link_json"]:
        raise LifecycleError("Canonical opening instruction link required")
    _evidence_ref(material["association_ref"])
    if material["mode"] == "opening_instruction":
        if (
            material["question"] is not None
            or material["selections"] != []
            or target.instruction_ref != target.response_ref
        ):
            raise LifecycleError("Opening instruction and response roles differ")
    elif material["mode"] == "dependency_answers":
        if target.instruction_ref == target.response_ref:
            raise LifecycleError("Dependency reply cannot replace opening instruction")
        question = material["question"]
        exact(
            question,
            {
                "question_id",
                "question_version",
                "delivery_ref",
                "proposal_ref",
                "list_sha256",
                "subjects",
            },
            "opening dependency question",
        )
        text(question["question_id"], "opening question id")
        positive(question["question_version"])
        _evidence_ref(question["delivery_ref"])
        sha(question["list_sha256"])
        if (
            question["proposal_ref"] != target.current_proposal
            or type(question["subjects"]) is not list
        ):
            raise LifecycleError("Opening question differs from actual prior proposal")
        if type(material["selections"]) is not list or len(material["selections"]) > 128:
            raise LifecycleError("Bounded actual row selections required")
        selected = []
        for selection in material["selections"]:
            exact(
                selection,
                {"movement_fact_ref", "impact_role", "span"},
                "actual opening row selection",
            )
            BundleFactRefV1(**selection["movement_fact_ref"])
            if selection["impact_role"] not in {"debit", "credit"}:
                raise LifecycleError("Unsupported actual row role")
            if selection["span"] is not None and (
                type(selection["span"]) is not list
                or len(selection["span"]) != 2
                or any(type(v) is not int for v in selection["span"])
            ):
                raise LifecycleError("Exact actual answer span required")
            selected.append((selection["movement_fact_ref"]["record_id"], selection["impact_role"]))
        if len(selected) != len(set(selected)):
            raise LifecycleError("Duplicate actual row selection")
    else:
        raise LifecycleError("Unsupported opening instruction link mode")
    return material


def subject(dependency, candidate):
    value = validate_movement(dependency["values"])
    return {
        "account_ref": asdict(account_ref(candidate)),
        "new_opening_ref": asdict(opening_ref(candidate)),
        "new_opening_amount": candidate["opening"]["amount"],
        "new_opening_currency": candidate["opening"]["currency"],
        "new_opening_as_of": candidate["opening"]["as_of"],
        "movement_fact_ref": dependency["fact_ref"],
        "economic_event_id": dependency["economic_event_id"],
        "impact_role": "debit" if value["kind"] == "expense" else "credit",
        **{
            k: value[k]
            for k in (
                "kind",
                "description",
                "business_date",
                "original_amount",
                "original_currency",
                "account_amount",
                "account_currency",
                "funds_time",
            )
        },
    }


def list_material(dependencies, candidate):
    return {
        "format": "finance-daily-opening-question-list-v1",
        "subjects": [subject(d, candidate) for d in dependencies],
    }


def validate_map(value, bundle):
    exact(
        value,
        {
            "format",
            "registry",
            "predecessor_bundle_ref",
            "account_id",
            "new_opening_ref",
            "closure_sha256",
            "entries",
            "map_sha256",
        },
        "opening dependency map",
    )
    if value["format"] != MAP_FORMAT or value["registry"] != REGISTRY:
        raise LifecycleError("Unsupported opening dependency map")
    predecessor = BundleFactRefV1(**value["predecessor_bundle_ref"])
    if (
        predecessor.record_id != value["account_id"]
        or value["account_id"] != bundle["account"]["account_id"]
    ):
        raise LifecycleError("Opening map belongs to another account")
    if (
        value["new_opening_ref"] != asdict(opening_ref(bundle))
        or bundle["opening"]["status"] != "ACTIVE"
    ):
        raise LifecycleError("Opening map refers to another active opening")
    OpeningRefV1(**value["new_opening_ref"])
    sha(value["closure_sha256"])
    if type(value["entries"]) is not list or len(value["entries"]) > 128:
        raise LifecycleError("Bounded map entries required")
    keys = []
    for item in value["entries"]:
        exact(
            item,
            {
                "movement_fact_ref",
                "economic_event_id",
                "impact_role",
                "subject",
                "treatment",
                "basis",
                "reply_link_ref",
                "answer_ref",
            },
            "opening map entry",
        )
        ref = BundleFactRefV1(**item["movement_fact_ref"])
        text(item["economic_event_id"], "movement event id")
        if item["impact_role"] not in {"debit", "credit"} or item["treatment"] not in {
            "included",
            "excluded",
        }:
            raise LifecycleError("Unsupported opening map treatment")
        if item["basis"] == "strictly_later":
            if (
                item["treatment"] != "excluded"
                or item["reply_link_ref"] is not None
                or item["answer_ref"] is not None
            ):
                raise LifecycleError("Automatic treatment cannot borrow reply authority")
        elif item["basis"] == "authenticated_reply":
            _evidence_ref(item["reply_link_ref"])
            if type(item["answer_ref"]) is not dict:
                raise LifecycleError("Authenticated answer required")
        else:
            raise LifecycleError("Unsupported opening treatment basis")
        keys.append((ref.record_id, item["impact_role"]))
    if (
        keys != sorted(set(keys))
        or digest({k: v for k, v in value.items() if k != "map_sha256"}) != value["map_sha256"]
    ):
        raise LifecycleError("Opening dependency map order/hash differs")
    return value


# New opening replies use a finite complete-choice grammar. These aliases are
# choices, not a natural-language negation or intent classifier.
_OPENING_CHOICES = {
    **dict.fromkeys(("included", "already included", "已包含", "已经包含"), "included"),
    **dict.fromkeys(("excluded", "not included", "未包含", "尚未包含"), "excluded"),
    **dict.fromkeys(
        ("unknown", "uncertain", "unsure", "not sure", "不确定", "未知", "不清楚"), "unknown"
    ),
}


def _selection_key(item):
    ref = BundleFactRefV1(**item["movement_fact_ref"])
    if item["impact_role"] not in {"debit", "credit"}:
        raise LifecycleError("Unsupported actual opening row role")
    return ref.record_id, item["impact_role"]


def _parse_opening_choices(raw, subjects, selections):
    """Consume the whole reply and bind each complete choice to an actual row.

    A bare choice applies only to one independently selected row. Otherwise
    every nonempty line is `record_id:role=choice` or `list_ordinal=choice`;
    semicolons and newlines separate lines. Ordinals are explicit positions in
    the exact delivered list, never inferred from provider ordering.
    """
    import re

    if type(raw) is not str or not raw or len(raw.encode()) > 65536:
        raise LifecycleError("Bounded complete opening reply required")
    if type(subjects) is not list or type(selections) is not list or not selections:
        raise LifecycleError("Actual opening row selections required")
    known = {}
    for item in subjects:
        key = _selection_key(item)
        if key in known:
            raise LifecycleError("Duplicate delivered opening row")
        known[key] = item
    actual = {}
    for item in selections:
        key = _selection_key(item)
        if (
            key in actual
            or key not in known
            or item["movement_fact_ref"] != known[key]["movement_fact_ref"]
        ):
            raise LifecycleError("Actual selection differs from delivered opening row")
        actual[key] = item
    body = raw.strip()
    offset = len(raw) - len(raw.lstrip())
    parsed = {}

    def append(item, treatment, span):
        key = _selection_key(item)
        if key in parsed:
            raise LifecycleError("Duplicate opening reply row")
        parsed[key] = {
            "movement_fact_ref": item["movement_fact_ref"],
            "impact_role": item["impact_role"],
            "treatment": treatment,
            "span": span if treatment != "unknown" else None,
        }

    treatment = _OPENING_CHOICES.get(body.casefold())
    if treatment is not None:
        if len(actual) != 1:
            raise LifecycleError("Bare opening choice requires one actual selected row")
        append(next(iter(actual.values())), treatment, [offset, offset + len(body)])
    else:
        row_syntax = None
        for match in re.finditer(r"(?:^|[;\n])([^;\n]*)", body):
            line = match.group(1)
            if "=" not in line:
                raise LifecycleError("Unsupported complete opening answer syntax")
            left, right = line.split("=", 1)
            token, choice = left.strip(), right.strip()
            treatment = _OPENING_CHOICES.get(choice.casefold())
            if treatment is None:
                raise LifecycleError("Unsupported complete opening answer syntax")
            if (
                token.isascii()
                and token.isdecimal()
                and len(token) <= 3
                and token == str(int(token))
                and 1 <= int(token) <= len(subjects)
            ):
                item = subjects[int(token) - 1]
                syntax = "ordinal"
            else:
                syntax = "identity"
                parts = token.rsplit(":", 1)
                item = known.get(tuple(parts)) if len(parts) == 2 else None
                if item is None:
                    raise LifecycleError("Opening reply names an undelivered row")
            if row_syntax is not None and syntax != row_syntax:
                raise LifecycleError("Mixed opening row reference forms refused")
            row_syntax = syntax
            start = offset + match.start(1) + len(left) + 1 + len(right) - len(right.lstrip())
            append(item, treatment, [start, start + len(choice)])
    if not parsed or set(parsed) != set(actual):
        raise LifecycleError("Opening reply rows differ from actual selections")
    for key, item in parsed.items():
        if (
            actual[key]["movement_fact_ref"] != item["movement_fact_ref"]
            or actual[key]["span"] != item["span"]
        ):
            raise LifecycleError("Opening answer must retain the complete choice span")
    return parsed


def _answer(raw, expected, *, choices=None):
    exact(raw, {"movement_fact_ref", "impact_role", "treatment", "span"}, "opening answer")
    key = _selection_key(raw)
    if raw["treatment"] not in {"included", "excluded", "unknown"}:
        raise LifecycleError("Unsupported opening answer")
    if choices is None:
        # Pure helper calls still require the whole reply. Actual compilation
        # supplies the complete independently authenticated list/selection set.
        selection = {k: raw[k] for k in ("movement_fact_ref", "impact_role", "span")}
        choices = _parse_opening_choices(expected.response["raw_input"], [selection], [selection])
    actual = choices.get(key)
    if actual is None or actual != raw:
        raise LifecycleError("Opening treatment differs from complete actual choice")
    if raw["treatment"] == "unknown":
        return None
    return _reason(raw["span"], SimpleNamespace(instruction=expected.response), True)


def compile_opening(command, expected):
    expected = decode_expected(asdict(expected))
    command = decode_command(canonical(command))
    if command["command_kind"] != expected.command_kind:
        raise LifecycleError("Opening command differs from request")
    original = {k: command[k] for k in ("command_kind", "fields", "reason_span")}
    if expected.original_command is not None and original != expected.original_command:
        raise LifecycleError("New opening instruction required for changed opening fields/reason")
    candidate = compile_proposal(
        {"format": "finance-daily-command-v1", **original}, base_expected(expected)
    )
    if candidate["format"] == "finance-daily-incomplete-proposal-v1":
        if command["answers"]:
            raise LifecycleError("Cannot answer an unspecified opening")
        return {
            "format": INCOMPLETE_FORMAT,
            "command": command,
            "candidate": None,
            "answers": [],
            "missing_fields": candidate["missing_fields"],
        }
    candidate = {**candidate, "format": BUNDLE_V2, "opening_dependency_map": None}
    subjects = list_material(expected.dependencies, candidate)
    question_list_hash = digest(subjects)
    retained = {
        (i["movement_fact_ref"]["record_id"], i["impact_role"]): i
        for i in expected.retained_answers
    }
    link = strict_json(expected.instruction_link["link_json"])
    if command["answers"]:
        if (
            link["mode"] != "dependency_answers"
            or link["question"]["list_sha256"] != question_list_hash
            or link["question"]["subjects"] != subjects["subjects"]
        ):
            raise LifecycleError("Opening answers lack actual complete question/delivery")
    if command["answers"]:
        selections = [
            {k: answer[k] for k in ("movement_fact_ref", "impact_role", "span")}
            for answer in command["answers"]
        ]
        if selections != link["selections"]:
            raise LifecycleError("Opening answer row/span differs from independently linked reply")
    elif link["mode"] == "dependency_answers" and link["selections"]:
        raise LifecycleError("Opening command omitted actual reply selections")
    choices = (
        _parse_opening_choices(
            expected.response["raw_input"], link["question"]["subjects"], link["selections"]
        )
        if link["mode"] == "dependency_answers"
        else {}
    )
    supplied = set()
    for answer in command["answers"]:
        reason = _answer(answer, expected, choices=choices)
        key = (answer["movement_fact_ref"]["record_id"], answer["impact_role"])
        if key in supplied:
            raise LifecycleError("Duplicate opening answer")
        supplied.add(key)
        matches = [
            s
            for s in subjects["subjects"]
            if s["movement_fact_ref"] == answer["movement_fact_ref"]
            and s["impact_role"] == answer["impact_role"]
        ]
        if len(matches) != 1:
            raise LifecycleError("Opening answer selects a different dependency")
        item_subject = matches[0]
        if (
            _later(item_subject["funds_time"], candidate["opening"]["as_of"])
            and answer["treatment"] == "included"
        ):
            raise LifecycleError("Opening answer contradicts strictly later funds time")
        if reason is None:
            retained.pop(key, None)
        else:
            retained[key] = {
                "movement_fact_ref": answer["movement_fact_ref"],
                "economic_event_id": item_subject["economic_event_id"],
                "impact_role": answer["impact_role"],
                "subject": item_subject,
                "treatment": answer["treatment"],
                "basis": "authenticated_reply",
                "reply_link_ref": {
                    "evidence_id": expected.instruction_link["evidence_id"],
                    "sha256": digest(expected.instruction_link),
                },
                "answer_ref": reason,
            }
    entries, missing = [], []
    for item_subject in subjects["subjects"]:
        key = (item_subject["movement_fact_ref"]["record_id"], item_subject["impact_role"])
        if _later(item_subject["funds_time"], candidate["opening"]["as_of"]):
            item = {
                "movement_fact_ref": item_subject["movement_fact_ref"],
                "economic_event_id": item_subject["economic_event_id"],
                "impact_role": item_subject["impact_role"],
                "subject": item_subject,
                "treatment": "excluded",
                "basis": "strictly_later",
                "reply_link_ref": None,
                "answer_ref": None,
            }
        else:
            item = retained.get(key)
            if item is not None and item["subject"] != item_subject:
                raise LifecycleError("Retained answer subject differs")
        if item is None:
            missing.append("coverage:" + key[0] + ":" + key[1])
        else:
            entries.append(item)
    if missing:
        return {
            "format": INCOMPLETE_FORMAT,
            "command": command,
            "candidate": candidate,
            "answers": entries,
            "missing_fields": missing,
        }
    material = {
        "format": MAP_FORMAT,
        "registry": REGISTRY,
        "predecessor_bundle_ref": expected.target_bundle_ref,
        "account_id": expected.record_id,
        "new_opening_ref": asdict(opening_ref(candidate)),
        "closure_sha256": digest(expected.dependencies),
        "entries": entries,
    }
    candidate["opening_dependency_map"] = {**material, "map_sha256": digest(material)}
    strict_json(canonical(candidate))
    return validate_bundle(candidate)
