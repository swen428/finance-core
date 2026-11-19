"""Exact opt-in account/opening codecs; no storage or authorization shortcuts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, localcontext
from zoneinfo import ZoneInfo

from finance_core.money import Money, canonical_money_str, normalize_currency

from ..contracts import CommitReceipt, LifecycleError, RuntimeBinding, canonical, digest

DAILY_CONTRACT_VERSION = "finance-daily-v1"
BUNDLE_FORMAT = "finance-daily-account-bundle-v1"
BUNDLE_FORMATS = frozenset({BUNDLE_FORMAT, "finance-daily-account-bundle-v2"})
OBSERVATION_FORMAT = "finance-daily-proposal-observation-v1"
REVIEW_FORMAT = "finance-daily-review-v1"
COMMAND_FORMAT = "finance-daily-command-v1"
REGISTRY_ID = "finance-daily-account-opening-domains-v1"
COMMANDS = frozenset(
    {"account_create", "account_rename", "opening_set", "opening_correct", "opening_void"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def exact(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise LifecycleError("Invalid exact " + label + " fields")
    return value


def text(value, label, limit=256):
    if type(value) is not str or not value or value != value.strip():
        raise LifecycleError("Invalid bounded " + label)
    if value.isascii():
        invalid = len(value) > limit or not value.isprintable()
    else:
        invalid = (
            len(value.encode("utf-8")) > limit
            or unicodedata.normalize("NFC", value) != value
            or any(unicodedata.category(c).startswith("C") for c in value)
        )
    if invalid:
        raise LifecycleError("Invalid bounded " + label)
    return value


def sha(value):
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LifecycleError("Exact lowercase SHA-256 required")
    return value


def positive(value):
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise LifecycleError("Bounded positive integer required")
    return value


@dataclass(frozen=True)
class AccountDefinitionRefV1:
    account_id: str
    definition_version: int
    definition_sha256: str

    def __post_init__(self):
        text(self.account_id, "account id")
        positive(self.definition_version)
        sha(self.definition_sha256)


@dataclass(frozen=True)
class OpeningRefV1:
    account_id: str
    opening_event_id: str
    opening_version: int
    opening_sha256: str

    def __post_init__(self):
        text(self.account_id, "account id")
        text(self.opening_event_id, "opening id")
        positive(self.opening_version)
        sha(self.opening_sha256)


@dataclass(frozen=True)
class BundleFactRefV1:
    record_id: str
    version: int
    fact_sha256: str
    commit_seq: int

    def __post_init__(self):
        text(self.record_id, "record id")
        positive(self.version)
        sha(self.fact_sha256)
        positive(self.commit_seq)


@dataclass(frozen=True)
class ExpectedDailyInvocationV1:
    binding: RuntimeBinding
    source_id: str
    source_sha256: str
    expected_version: int
    record_id: str
    command_kind: str
    before_values: dict | None
    target_bundle_ref: dict | None
    instruction: dict
    opening_event_id: str
    current_proposal_id: str | None = None
    reply_evidence_id: str | None = None
    expected_proposal_version: int = 0
    format: str = "finance-daily-expected-invocation-v1"


@dataclass(frozen=True)
class VerifiedDailyProposalV1:
    evidence_id: str
    source_id: str
    principal_id: str
    client_id: str
    request_sha256: str
    raw_proposal: str
    format: str = OBSERVATION_FORMAT


@dataclass(frozen=True)
class DailyReviewV1:
    review_id: str
    proposal_id: str
    review_sha256: str
    material_json: str
    delivery_evidence_id: str | None = None

    @property
    def material(self):
        return strict_json(self.material_json)


@dataclass(frozen=True)
class DailyCommitReceiptV1:
    receipt: CommitReceipt
    command_kind: str
    account_ref: AccountDefinitionRefV1
    opening_ref: OpeningRefV1 | None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleError("Duplicate JSON key")
        result[key] = value
    return result


def strict_json(raw):
    if type(raw) is not str or len(raw.encode("utf-8")) > 131072:
        raise LifecycleError("Bounded exact JSON text required")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(LifecycleError("Nonfinite JSON")),
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise LifecycleError("Invalid strict daily JSON") from exc


def decode_command(raw):
    value = strict_json(raw)
    exact(value, {"format", "command_kind", "fields", "reason_span"}, "daily command")
    if (
        value["format"] != COMMAND_FORMAT
        or type(value["command_kind"]) is not str
        or value["command_kind"] not in COMMANDS
    ):
        raise LifecycleError("Unsupported daily command format")
    if type(value["fields"]) is not dict:
        raise LifecycleError("Exact command fields required")
    return value


def validate_as_of(value):
    exact(value, {"format", "precision", "timestamp", "local_date", "timezone"}, "as-of")
    if value["format"] != "finance-daily-as-of-v1" or value["timezone"] != "Asia/Singapore":
        raise LifecycleError("Unsupported as-of format/timezone")
    precision, timestamp, day = value["precision"], value["timestamp"], value["local_date"]
    if precision == "unknown":
        if timestamp is not None or day is not None:
            raise LifecycleError("Unknown as-of must retain null time fields")
    elif precision in {"date", "instant"}:
        try:
            if type(day) is not str or date.fromisoformat(day).isoformat() != day:
                raise ValueError("date")
            if precision == "date":
                if timestamp is not None:
                    raise ValueError("date-only")
            elif (
                type(timestamp) is not int
                or timestamp <= 0
                or datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Singapore")).date().isoformat()
                != day
            ):
                raise ValueError("instant")
        except (ValueError, OverflowError, OSError) as exc:
            raise LifecycleError("As-of precision/date/instant mismatch") from exc
    else:
        raise LifecycleError("Unknown as-of precision")
    return value


def instruction_ref(instruction, binding=None):
    exact(
        instruction,
        {
            "evidence_id",
            "principal_id",
            "client_id",
            "source_key",
            "raw_input",
            "original_sha256",
            "source_timestamp",
            "timezone",
        },
        "instruction",
    )
    for key in ("evidence_id", "principal_id", "client_id", "source_key"):
        text(instruction[key], key)
    raw = instruction["raw_input"]
    if (
        type(raw) is not str
        or not raw
        or len(raw.encode()) > 65536
        or instruction["timezone"] != "Asia/Singapore"
        or type(instruction["source_timestamp"]) is not int
        or instruction["source_timestamp"] <= 0
        or hashlib.sha256(raw.encode()).hexdigest() != instruction["original_sha256"]
    ):
        raise LifecycleError("Invalid authenticated instruction")
    if binding and (
        instruction["principal_id"] != binding.principal_id
        or instruction["client_id"] != binding.client_id
    ):
        raise LifecycleError("Instruction actor/client mismatch")
    return {
        "format": "finance-daily-instruction-ref-v1",
        **{
            k: instruction[k]
            for k in (
                "evidence_id",
                "principal_id",
                "client_id",
                "source_key",
                "original_sha256",
                "source_timestamp",
            )
        },
    }


def _reference(value):
    exact(
        value,
        {
            "format",
            "evidence_id",
            "principal_id",
            "client_id",
            "source_key",
            "original_sha256",
            "source_timestamp",
        },
        "instruction ref",
    )
    if value["format"] != "finance-daily-instruction-ref-v1":
        raise LifecycleError("Unsupported instruction reference")
    for key in ("evidence_id", "principal_id", "client_id", "source_key"):
        text(value[key], key)
    sha(value["original_sha256"])
    if type(value["source_timestamp"]) is not int or value["source_timestamp"] <= 0:
        raise LifecycleError("Invalid original instruction time")


def _reason(span, expected, required):
    if span is None:
        if required:
            raise LifecycleError("Explicit authenticated reason required")
        return None
    raw = expected.instruction["raw_input"]
    if (
        type(span) is not list
        or len(span) != 2
        or any(type(v) is not int for v in span)
        or not 0 <= span[0] < span[1] <= len(raw)
    ):
        raise LifecycleError("Exact reason codepoint span required")
    quote = raw[span[0] : span[1]]
    text(quote, "reason", 1024)
    return {
        "format": "finance-daily-reason-ref-v1",
        "instruction_ref": instruction_ref(expected.instruction),
        "span": span,
        "quote": quote,
        "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
    }


def _money(raw, currency):
    if type(raw) is not str or not 1 <= len(raw) <= 128:
        raise LifecycleError("Bounded decimal money string required")
    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        value = Money.from_string(raw, currency)
        amount = abs(value.amount) if value.amount == 0 else value.amount
        result = canonical_money_str(amount, value.currency)
    if len(result) > 128:
        raise LifecycleError("Canonical amount exceeds daily limit")
    return result


def _sealed(value, field):
    return {**value, field: digest(value)}


def account_ref(value):
    a = value["account"]
    return AccountDefinitionRefV1(a["account_id"], a["definition_version"], a["definition_sha256"])


def opening_ref(value):
    o = value["opening"]
    return (
        None
        if o is None
        else OpeningRefV1(
            o["account_id"], o["opening_event_id"], o["opening_version"], o["opening_sha256"]
        )
    )


def validate_bundle(value):
    v2 = type(value) is dict and value.get("format") == "finance-daily-account-bundle-v2"
    keys = {"format", "kind", "transition", "account", "opening"}
    exact(value, keys | ({"opening_dependency_map"} if v2 else set()), "account bundle")
    if (
        value["format"] not in BUNDLE_FORMATS
        or value["kind"] != "account_bundle"
        or value["transition"] not in COMMANDS
    ):
        raise LifecycleError("Unsupported account bundle format")
    a = value["account"]
    exact(
        a,
        {
            "format",
            "account_id",
            "definition_version",
            "name",
            "account_type",
            "currency",
            "predecessor_definition_sha256",
            "instruction_ref",
            "definition_sha256",
        },
        "account definition",
    )
    if a["format"] != "finance-daily-account-definition-v1" or a["account_type"] not in {
        "cash",
        "bank",
        "credit_card",
    }:
        raise LifecycleError("Unsupported account definition")
    account_ref(value)
    text(a["name"], "account name")
    _reference(a["instruction_ref"])
    if normalize_currency(a["currency"]) != a["currency"]:
        raise LifecycleError("Noncanonical currency")
    prior = a["predecessor_definition_sha256"]
    if (a["definition_version"] == 1) != (prior is None):
        raise LifecycleError("Definition predecessor missing")
    if prior is not None:
        sha(prior)
    if digest({k: v for k, v in a.items() if k != "definition_sha256"}) != a["definition_sha256"]:
        raise LifecycleError("Account component hash mismatch")
    o = value["opening"]
    if o is not None:
        exact(
            o,
            {
                "format",
                "account_id",
                "opening_event_id",
                "opening_version",
                "status",
                "amount",
                "currency",
                "as_of",
                "predecessor_opening_sha256",
                "instruction_ref",
                "reason_ref",
                "opening_sha256",
            },
            "opening",
        )
        opening_ref(value)
        _reference(o["instruction_ref"])
        validate_as_of(o["as_of"])
        if (
            o["format"] != "finance-daily-opening-v1"
            or o["account_id"] != a["account_id"]
            or o["status"] not in {"ACTIVE", "VOID"}
            or o["currency"] != a["currency"]
            or _money(o["amount"], o["currency"]) != o["amount"]
        ):
            raise LifecycleError("Invalid canonical opening")
        prior = o["predecessor_opening_sha256"]
        if (o["opening_version"] == 1) != (prior is None):
            raise LifecycleError("Opening predecessor missing")
        if prior is not None:
            sha(prior)
        reason = o["reason_ref"]
        if reason is not None:
            exact(
                reason, {"format", "instruction_ref", "span", "quote", "quote_sha256"}, "reason ref"
            )
            _reference(reason["instruction_ref"])
            text(reason["quote"], "reason", 1024)
            span = reason["span"]
            if (
                type(span) is not list
                or len(span) != 2
                or any(type(v) is not int for v in span)
                or not 0 <= span[0] < span[1]
                or span[1] - span[0] != len(reason["quote"])
            ):
                raise LifecycleError("Invalid exact reason span")
            if (
                reason["format"] != "finance-daily-reason-ref-v1"
                or hashlib.sha256(reason["quote"].encode()).hexdigest() != reason["quote_sha256"]
            ):
                raise LifecycleError("Invalid reason reference")
        if o["status"] == "VOID" and reason is None:
            raise LifecycleError("VOID needs original reason")
        if digest({k: v for k, v in o.items() if k != "opening_sha256"}) != o["opening_sha256"]:
            raise LifecycleError("Opening component hash mismatch")
    if v2 and value["opening_dependency_map"] is not None:
        from .opening_contracts_v1 import validate_map

        validate_map(value["opening_dependency_map"], value)
    return value


def decode_expected(value):
    exact(value, ExpectedDailyInvocationV1.__dataclass_fields__, "daily expected invocation")
    raw = dict(value)
    raw["binding"] = RuntimeBinding(**raw["binding"])
    expected = ExpectedDailyInvocationV1(**raw)
    if (
        expected.format != "finance-daily-expected-invocation-v1"
        or expected.command_kind not in COMMANDS
    ):
        raise LifecycleError("Unsupported daily expected invocation")
    text(expected.record_id, "account id")
    text(expected.source_id, "source id")
    sha(expected.source_sha256)
    text(expected.opening_event_id, "allocated opening id")
    instruction_ref(expected.instruction, expected.binding)
    if (
        type(expected.expected_version) is not int
        or expected.expected_version < 0
        or type(expected.expected_proposal_version) is not int
        or expected.expected_proposal_version < 0
    ):
        raise LifecycleError("Exact nonnegative expected versions required")
    if expected.before_values is not None:
        validate_bundle(expected.before_values)
        ref = BundleFactRefV1(**expected.target_bundle_ref)
        if (
            ref.record_id != expected.record_id
            or ref.version != expected.expected_version
            or expected.before_values["account"]["account_id"] != expected.record_id
        ):
            raise LifecycleError("Target reference differs from expected state")
    elif expected.target_bundle_ref is not None or expected.expected_version != 0:
        raise LifecycleError("Create cannot claim predecessor")
    return expected


def compile_bundle(command, expected):
    expected = decode_expected(asdict(expected))
    command = decode_command(canonical(command))
    kind = command["command_kind"]
    if kind != expected.command_kind:
        raise LifecycleError("Proposal command differs from expected")
    fields = command["fields"]
    before = expected.before_values
    ref = instruction_ref(expected.instruction)
    reason = _reason(command["reason_span"], expected, kind in {"opening_correct", "opening_void"})
    if kind == "account_create":
        if before is not None:
            raise LifecycleError("Account already exists")
        exact(fields, {"name", "account_type", "currency", "opening"}, "account create")
        text(fields["name"], "account name")
        if fields["account_type"] not in {"cash", "bank", "credit_card"}:
            raise LifecycleError("Invalid account type")
        currency = normalize_currency(fields["currency"])
        a = _sealed(
            {
                "format": "finance-daily-account-definition-v1",
                "account_id": expected.record_id,
                "definition_version": 1,
                "name": fields["name"],
                "account_type": fields["account_type"],
                "currency": currency,
                "predecessor_definition_sha256": None,
                "instruction_ref": ref,
            },
            "definition_sha256",
        )
        proposed = fields["opening"]
        if proposed is not None:
            exact(proposed, {"amount", "as_of"}, "initial opening")
        opening = None
    else:
        if before is None:
            raise LifecycleError("Existing account required")
        a = before["account"]
        opening = before["opening"]
        proposed = None
        if kind == "account_rename":
            exact(fields, {"name"}, "account rename")
            text(fields["name"], "account name")
            if fields["name"] == a["name"]:
                raise LifecycleError("Account rename has no change")
            a = _sealed(
                {
                    **{k: v for k, v in a.items() if k != "definition_sha256"},
                    "name": fields["name"],
                    "definition_version": a["definition_version"] + 1,
                    "predecessor_definition_sha256": a["definition_sha256"],
                    "instruction_ref": ref,
                },
                "definition_sha256",
            )
        else:
            exact(fields, {} if kind == "opening_void" else {"amount", "as_of"}, kind)
            if kind == "opening_set":
                if opening is not None and (
                    opening["status"] != "VOID"
                    or opening["opening_event_id"] == expected.opening_event_id
                ):
                    raise LifecycleError(
                        "New opening requires closed predecessor and fresh identity"
                    )
                proposed = fields
            elif opening is None or opening["status"] != "ACTIVE":
                raise LifecycleError("Active opening required")
            else:
                proposed = (
                    fields
                    if kind == "opening_correct"
                    else {"amount": opening["amount"], "as_of": opening["as_of"]}
                )
    if proposed is not None:
        old = opening if kind in {"opening_correct", "opening_void"} else None
        opening = _sealed(
            {
                "format": "finance-daily-opening-v1",
                "account_id": expected.record_id,
                "opening_event_id": old["opening_event_id"] if old else expected.opening_event_id,
                "opening_version": old["opening_version"] + 1 if old else 1,
                "status": "VOID" if kind == "opening_void" else "ACTIVE",
                "amount": _money(proposed["amount"], a["currency"]),
                "currency": a["currency"],
                "as_of": validate_as_of(proposed["as_of"]),
                "predecessor_opening_sha256": old["opening_sha256"] if old else None,
                "instruction_ref": ref,
                "reason_ref": reason,
            },
            "opening_sha256",
        )
        if (
            old
            and kind == "opening_correct"
            and all(opening[k] == old[k] for k in ("amount", "as_of"))
        ):
            raise LifecycleError("Opening correction has no material change")
    result = {
        "format": before["format"] if before else BUNDLE_FORMAT,
        "kind": "account_bundle",
        "transition": kind,
        "account": a,
        "opening": opening,
    }
    if result["format"] == "finance-daily-account-bundle-v2":
        result["opening_dependency_map"] = (
            before["opening_dependency_map"] if kind == "account_rename" else None
        )
    return validate_bundle(result)


INCOMPLETE_FORMAT = "finance-daily-incomplete-proposal-v1"


def compile_proposal(command, expected):
    """Preserve valid partial proposals; invalid supplied values always refuse."""
    from finance_core.money import money_decimal

    expected = decode_expected(asdict(expected))
    command = decode_command(canonical(command))
    kind = command["command_kind"]
    fields = command["fields"]
    before = expected.before_values
    if kind != expected.command_kind:
        raise LifecycleError("Proposal command differs from expected")
    if kind == "account_create":
        if before is not None:
            raise LifecycleError("Account already exists")
        required = {"name", "account_type", "currency", "opening"}
        currency = fields.get("currency")
    else:
        if before is None:
            raise LifecycleError("Existing account required")
        required = (
            {"name"}
            if kind == "account_rename"
            else set()
            if kind == "opening_void"
            else {"amount", "as_of"}
        )
        currency = before["account"]["currency"]
        opening = before["opening"]
        if kind == "opening_set" and opening is not None and opening["status"] != "VOID":
            raise LifecycleError("Active opening cannot be replaced")
        if kind in {"opening_correct", "opening_void"} and (
            opening is None or opening["status"] != "ACTIVE"
        ):
            raise LifecycleError("Active opening required")
    if set(fields) - required:
        raise LifecycleError("Unknown partial command fields")
    missing = set(required) - set(fields)
    if "name" in fields:
        text(fields["name"], "account name")
    if "account_type" in fields and fields["account_type"] not in {"cash", "bank", "credit_card"}:
        raise LifecycleError("Invalid account type")
    if currency is not None:
        currency = normalize_currency(currency)
    if "currency" in fields and fields["currency"] is None:
        raise LifecycleError("Explicit currency required")

    def partial_opening(value, prefix):
        if type(value) is not dict or set(value) - {"amount", "as_of"}:
            raise LifecycleError("Invalid partial opening fields")
        for key in {"amount", "as_of"} - set(value):
            missing.add(prefix + key)
        if "amount" in value:
            raw = value["amount"]
            if type(raw) is not str or not 1 <= len(raw) <= 128:
                raise LifecycleError("Bounded decimal money string required")
            money_decimal(raw)
            if currency is not None:
                _money(raw, currency)
        if "as_of" in value:
            validate_as_of(value["as_of"])

    if kind == "account_create" and "opening" in fields and fields["opening"] is not None:
        partial_opening(fields["opening"], "opening.")
    elif kind in {"opening_set", "opening_correct"}:
        partial_opening(fields, "")
    required_reason = kind in {"opening_correct", "opening_void"}
    if command["reason_span"] is None and required_reason:
        missing.add("reason_span")
    else:
        _reason(command["reason_span"], expected, False)
    if not missing:
        return compile_bundle(command, expected)
    return {
        "format": INCOMPLETE_FORMAT,
        "command_kind": kind,
        "fields": fields,
        "reason_span": command["reason_span"],
        "missing_fields": sorted(missing),
    }
