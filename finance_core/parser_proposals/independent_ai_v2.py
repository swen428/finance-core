"""Closed v2 interpretation: exact evidence, role-aware money and Python dates.

Coverage is a structural instruction ledger, not a claim of perfect language
understanding. Known financial contradictions remain deterministic blockers.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from finance_core.application.ai_invocation_contract import AI_RESULT_SCHEMA
from finance_core.money import (
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.independent_ai_source_grammar import assess_source_money
from finance_core.parser_proposals.independent_ai_validation import (
    _DATE_CUE,
    FIELDS,
    _known_date_alternative,
    _pairs,
    assets,
    canonical_text,
)

_WEEKDAYS = {
    1: ("monday", "周一", "星期一"),
    2: ("tuesday", "周二", "星期二"),
    3: ("wednesday", "周三", "星期三"),
    4: ("thursday", "周四", "星期四"),
    5: ("friday", "周五", "星期五"),
    6: ("saturday", "周六", "星期六"),
    7: ("sunday", "周日", "周天", "星期日", "星期天"),
}
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


DATE_KINDS = {
    "absolute_date": {"kind", "year", "month", "day"},
    "day_offset": {"kind", "days"},
    "calendar_week_weekday": {"kind", "week_offset", "iso_weekday"},
    "previous_or_next_weekday": {"kind", "direction", "iso_weekday"},
}
_MONTH_NAMES = _MONTHS + (
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_MONTH_WORD = re.compile(r"\b(?:" + "|".join(_MONTH_NAMES) + r")\b", re.I)
_DATE_DAY_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty first second third fourth "
    "fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth "
    "sixteenth seventeenth eighteenth nineteenth twentieth thirtieth"
).split()
_DATE_DAY_WORD = (
    r"(?:" + "|".join(_DATE_DAY_WORDS) + r")(?:[ -]+(?:" + "|".join(_DATE_DAY_WORDS) + r"))?"
)
_MONTH_DATE_WORDS = (
    set(_DATE_DAY_WORDS)
    | {words[0] for words in _WEEKDAYS.values()}
    | set(
        (
            "date time day days week weeks month months year years yesterday today tomorrow "
            "last next this previous before after ago in"
        ).split()
    )
)
_DATE_ARM_BOUNDARY = r"[;；\r\n!?！？。]|(?<![0-9])\.|\.(?![0-9])"

_DAY_OFFSET_CUES = {
    -2: ("前天", "the day before yesterday"),
    -1: ("昨天", "昨日", "yesterday"),
    0: ("今天", "今日", "today"),
    1: ("明天", "tomorrow"),
    2: ("后天", "the day after tomorrow"),
}
_V2_DATE_CUE = re.compile(
    _DATE_CUE
    + "|"
    + "|".join(re.escape(cue) for cues in _DAY_OFFSET_CUES.values() for cue in cues)
    + r"|\bin\s+[+-]?\d+(?:\.\d+)?\s+days?\b"
    + r"|(?<!\w)[+-]?\d+(?:\.\d+)?\s+days?\s+ago\b|[+-]?\d+(?:\.\d+)?天[前后]"
    + r"|\b(?:"
    + "|".join(_MONTH_NAMES)
    + r")(?=[0-9])",
    re.I,
)
_DATE_REQUEST = re.compile(
    r"日期|时间|\bdate\b|" + _V2_DATE_CUE.pattern,
    re.I,
)
_EDIT_CUES = {
    "amount": r"金额|价钱|数额|\bamount\b",
    "currency": r"币种|货币|\bcurrency\b",
    "merchant": r"商户|商家|\bmerchant\b",
    "description": r"描述|备注|\bdescription\b",
    "category": r"分类|类别|\bcategory\b",
}
_CLEAR_CUE = r"\b(?:remove|clear|delete)\b|删除|清空|移除"
_V2_INTENT_EXTENSION = re.compile(r"退费|退钱|退了|返款|转帐|\bshared[ -]expense\b", re.I)


def _unsupported_intent(texts):
    """Reuse the complete immutable v1 refusal policy on unmasked input."""
    try:
        assets("v1")
        policy = json.loads(
            files("finance_core.resources")
            .joinpath("independent_ai/text_readiness_v1.json")
            .read_text()
        )
    except OSError as exc:
        raise ValueError("unsupported_intent_policy_unavailable") from exc
    for text in texts:
        for token in policy["deny_intent_tokens"]:
            pattern = (
                re.escape(token)
                if any("\u4e00" <= c <= "\u9fff" for c in token)
                else r"(?<![\w])" + re.escape(token) + r"(?![\w])"
            )
            if re.search(pattern, text, re.I):
                return True
        if any(re.search(pattern, text, re.I) for pattern in policy["deny_intent_patterns"]):
            return True
        if _V2_INTENT_EXTENSION.search(text):
            return True
    return False


def _optional_clear_pattern(field):
    name = _EDIT_CUES[field]
    return re.compile(
        r"(?:"
        + _CLEAR_CUE
        + r")\s*(?:the\s+)?(?:"
        + name
        + r")"
        + r"|(?:"
        + name
        + r")\s*(?:[:：]\s*)?(?:"
        + _CLEAR_CUE
        + r")(?=\s*(?:$|[;；,，.!！？。\r\n]|\band\b|并且|然后|并))",
        re.I,
    )


def _reply_assignments(text):
    """Read top-level literal owners from raw reply syntax, never model choices."""
    names = "|".join(
        "(?P<" + field + ">" + _EDIT_CUES[field] + ")"
        for field in ("merchant", "description", "category")
    )
    assignment = re.compile(
        r"(?:" + names + r")\s*(?:to|as|改为|改成|为|成|是|[:：])\s*"
        r"|(?P<at>\bat)\s*(?=[\"“'])",
        re.I,
    )
    all_names = "|".join(_EDIT_CUES.values()) + r"|日期|时间|\bdate\b"
    boundary = re.compile(
        r"[;；,，!?！？。\r\n]|(?<![0-9])\.|\.(?![0-9])"
        r"|(?:\band\b|并且|然后|和|并)\s*(?:(?:\bset\b|\bchange\b)\s+)?(?:"
        + all_names
        + r"|(?:"
        + _CLEAR_CUE
        + r")\s*(?:the\s+)?(?:"
        + all_names
        + r"))",
        re.I,
    )
    quote_pairs = {'"': '"', "'": "'", "“": "”"}
    result = []
    consumed = 0
    for match in assignment.finditer(text):
        if match.start() < consumed:
            continue
        field = "merchant" if match.group("at") else match.lastgroup
        # Field-first "category: clear" is an instruction, not a literal value.
        if field in {"description", "category"} and _optional_clear_pattern(field).match(
            text, match.start()
        ):
            continue
        start = match.end()
        if start == len(text):
            continue
        value_start, value_end, closed = start, len(text), True
        if text[start] in quote_pairs:
            close = quote_pairs[text[start]]
            value_start = start + 1
            end = start + 1
            closed = False
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                elif text[end] == close:
                    value_end = end
                    end += 1
                    closed = True
                    break
                else:
                    end += 1
            end = min(end, len(text))
        else:
            cut = boundary.search(text, start)
            end = cut.start() if cut else len(text)
            value_end = end
            while value_end > value_start and text[value_end - 1].isspace():
                value_end -= 1
        consumed = end
        result.append(
            {
                "field": field,
                "start": start,
                "end": end,
                "value_start": value_start,
                "value_end": value_end,
                "closed": closed,
                "named": not bool(match.group("at")),
            }
        )
    return result


def _reply_instruction_text(text, assignments):
    for item in assignments:
        start, end = item["start"], item["end"]
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def _reply_assignment_compatible(field, value, text, assignments):
    """Every actual same-field owner constrains the answer, including quoted-at."""
    return all(
        item["closed"]
        and unicodedata.normalize("NFC", text[item["value_start"] : item["value_end"]]) == value
        for item in assignments
        if item["field"] == field
    )


def _explicit_currency_citation_supported(value, evidence, assessment):
    return any(
        token.kind == "currency"
        and token.currency == value
        and any(c["start"] <= token.start and token.end <= c["end"] for c in evidence)
        for token in assessment.partition
    )


def _reply_text_assignment_supported(field, value, evidence, text, assignments, *, named_only):
    if not _reply_assignment_compatible(field, value, text, assignments):
        return False
    for item in assignments:
        if item["field"] != field or not item["closed"] or named_only and not item["named"]:
            continue
        start, end = item["value_start"], item["value_end"]
        if unicodedata.normalize("NFC", text[start:end]) != value:
            continue
        if any(c["start"] <= start and end <= c["end"] for c in evidence):
            return True
    return False


def _explicit_optional_clear(field, evidence, instruction_text):
    """Require a freshly cited same-field instruction outside assigned literals."""
    if field not in {"description", "category"}:
        return False
    return any(
        citation["ref"] == "reply"
        and citation["role"] == "replacement_value"
        and citation["start"] <= match.start()
        and match.end() <= citation["end"]
        for match in _optional_clear_pattern(field).finditer(instruction_text)
        for citation in evidence
    )


def _fresh_retention_spans(field, value, current, evidence, instruction_text):
    """Recognize one bounded, freshly cited choice of this current field.

    Literal masking keeps another field's quoted value from acting as a cue.
    Return only the field-name span; a retained date never masks other dates.
    Pending optional null still needs the separate explicit-clear predicate.
    """
    if value is None or value != current.get(field):
        return []
    name = r"日期|时间|\bdate\b" if field == "transaction_date" else _EDIT_CUES[field]
    if len(list(re.finditer(name, instruction_text, re.I))) != 1:
        return []
    boundary = r"[;；,，.!！？。\r\n]|\band\b|并且|然后|并"
    pattern = (
        r"(?:^|(?:"
        + boundary
        + r"))\s*(?P<instruction>(?:\bkeep\b|\bretain\b|保持|保留)\s*"
        + r"(?:the\s+)?(?P<field>"
        + name
        + r")(?:\s*(?:\bunchanged\b|不变))?)\s*(?=$|(?:"
        + boundary
        + r"))"
    )
    return [
        match.span("field")
        for match in re.finditer(pattern, instruction_text, re.I)
        if any(
            citation["ref"] == "reply"
            and citation["role"] == "replacement_value"
            and citation["start"] <= match.start("instruction")
            and match.end("instruction") <= citation["end"]
            for citation in evidence
        )
    ]


def _pending_answer_supported(
    field,
    operation,
    value,
    evidence,
    prior_fields,
    text,
    instruction_text,
    assessment,
    retention,
    assignments,
):
    """One content predicate for all six authenticated pending obligations."""
    if operation is None:
        return False
    replacement = [
        citation
        for citation in evidence
        if citation["ref"] == "reply" and citation["role"] == "replacement_value"
    ]
    if not replacement or not _reply_assignment_compatible(field, value, text, assignments):
        return False
    if value is None:
        return _explicit_optional_clear(field, replacement, instruction_text)
    if retention:
        if field == "amount":
            return all(
                money_decimal(span.number) == money_decimal(value) for span in assessment.spans
            )
        if field == "currency":
            return not assessment.currencies or assessment.currencies == {value}
        return True
    if field == "amount":
        return any(
            money_decimal(span.number) == money_decimal(value)
            and any(
                citation["start"] <= span.number_start and span.number_end <= citation["end"]
                for citation in replacement
            )
            for span in assessment.spans
        )
    if field == "currency":
        # A bare symbol may inherit current currency for a normal edit, but it
        # cannot answer an outstanding currency choice without a fresh code.
        return _explicit_currency_citation_supported(value, replacement, assessment)
    if field == "transaction_date":
        return any(
            instruction_text[citation["start"] + start : citation["start"] + end]
            == text[citation["start"] + start : citation["start"] + end]
            for citation in replacement
            for start, end in date_evidence_spans(operation["date_expression"], citation["quote"])
        )
    if _reply_text_assignment_supported(
        field, value, replacement, text, assignments, named_only=True
    ):
        return True
    return (
        len(prior_fields & {"merchant", "description", "category"}) == 1
        and unicodedata.normalize("NFC", text.strip()) == value
        and any(c["start"] == 0 and c["end"] == len(text) for c in replacement)
        and not _DATE_REQUEST.search(instruction_text)
        and not assessment.spans
        and not assessment.currencies
        and assessment.currency_state == "PROVEN_ABSENT"
    )


def _initial_month_literal_spans(text, start, end, *, independent_date):
    """Mask only a month word in a grounded, simple non-date phrase.

    Full source neighbors prevent a model's shortened value from hiding a
    month/day token. Number words are rejection guards, never date operations.
    """
    value = text[start:end]
    if not independent_date or not re.fullmatch(r"[A-Za-z]+(?:[ \t]+[A-Za-z]+)*", value):
        return []
    months = list(_MONTH_WORD.finditer(value))
    words = value.lower().split()
    if len(months) != 1 or not any(word not in _MONTH_NAMES for word in words):
        return []
    if any(word in _MONTH_DATE_WORDS for word in words):
        return []
    month = months[0]
    a, b = start + month.start(), start + month.end()
    if (a and (text[a - 1].isalnum() or text[a - 1] == "_")) or (
        b < len(text) and (text[b].isalnum() or text[b] == "_")
    ):
        return []
    before, after = text[:a], text[b:]
    if re.match(r"\s*(?:[0-9]|\b" + _DATE_DAY_WORD + r"\b)", after, re.I):
        return []
    if re.search(
        r"(?:\b" + _DATE_DAY_WORD + r"|(?<![0-9./-])\b[0-9]{1,2}(?:st|nd|rd|th)?)\s+(?:of\s+)?$",
        before,
        re.I,
    ):
        return []
    # A date label in the same bounded clause remains a request, even if a
    # model assigns the following words to description/category/merchant.
    clause = re.split(_DATE_ARM_BOUNDARY, before)[-1]
    if re.search(r"\bdate\b|日期|时间", clause, re.I):
        return []
    return [(a, b)]


def _literal_instruction_text(
    text, text_ref, operations, *, initial=False, source_date_supported=False, weak_months=False
):
    """Exclude only exact explicitly named literals, never their whole citation.

    These are field-bound value spans, not a natural-language date parser.
    All instructions outside each literal remain subject to monetary/date checks.
    """
    if text_ref == "reply":
        return _reply_instruction_text(text, _reply_assignments(text))
    spans = []
    for op in operations:
        field, value = op["field"], op.get("value")
        if field not in {"merchant", "description", "category"} or not isinstance(value, str):
            continue
        pattern = _EDIT_CUES[field]
        for citation in op["evidence"]:
            if citation["ref"] != text_ref or citation["role"] != "replacement_value":
                continue
            for match in re.finditer(re.escape(value), citation["quote"]):
                start = citation["start"] + match.start()
                end = citation["start"] + match.end()
                prefix = text[:start]
                named = re.search(
                    r"(?:" + pattern + r")\s*(?:to|as|改为|改成|为|成|是|[:：])\s*[\"“']?\s*$",
                    prefix,
                    re.I,
                )
                at_merchant = field == "merchant" and re.search(r"\bat\s*[\"“']?\s*$", prefix, re.I)
                if named or at_merchant:
                    quote_pairs = {'"': '"', "'": "'", "“": "”"}
                    quoted = (
                        start > 0
                        and end < len(text)
                        and quote_pairs.get(text[start - 1]) == text[end]
                    )
                    if at_merchant and not named and not quoted:
                        # Initial "at ..." is not an explicit field literal.
                        # It cannot erase trailing relative/partial dates. Keep
                        # only a month-name word inside an ordinary name such
                        # as "May Cafe", not a numeric date or the entire value.
                        if initial and weak_months:
                            spans.extend(
                                _initial_month_literal_spans(
                                    text, start, end, independent_date=True
                                )
                            )
                        continue
                    if quoted:
                        start -= 1
                        end += 1
                    else:
                        # Unquoted model field values cannot swallow another
                        # instruction after a clause boundary or a named-field
                        # conjunction. Preserve every character after that cut.
                        boundary = re.search(
                            r"[;；,，\r\n]|(?:\band\b|并且|然后|和)\s*(?:"
                            + "|".join(_EDIT_CUES.values())
                            + r"|日期|时间|\bdate\b)",
                            text[start:end],
                            re.I,
                        )
                        if boundary is not None:
                            end = start + boundary.start()
                    if start < end:
                        spans.append((start, end))
                elif initial and weak_months and text_ref == "source":
                    spans.extend(
                        _initial_month_literal_spans(
                            text, start, end, independent_date=source_date_supported
                        )
                    )
    for start, end in spans:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def _known_v2_date_alternative(text):
    cuts = list(
        re.finditer(r"(?P<alternative>\bor\b|或者|或|还是)|" + _DATE_ARM_BOUNDARY, text, re.I)
    )
    for index, cut in enumerate(cuts):
        if cut.lastgroup == "alternative":
            left = text[cuts[index - 1].end() if index else 0 : cut.start()]
            right = text[
                cut.end() : cuts[index + 1].start() if index + 1 < len(cuts) else len(text)
            ]
            if _V2_DATE_CUE.search(left) and _V2_DATE_CUE.search(right):
                return True
    return False


def compute_date(expression: object, context: dict[str, Any]) -> str:
    if not isinstance(expression, dict) or expression.get("kind") not in DATE_KINDS:
        raise ValueError("date_expression")
    kind = expression["kind"]
    if set(expression) != DATE_KINDS[kind]:
        raise ValueError("date_expression_keys")
    anchor = datetime.fromtimestamp(
        context["anchor_timestamp"], ZoneInfo(context["timezone"])
    ).date()
    if context["local_date"] != anchor.isoformat():
        raise ValueError("date_anchor")
    for key, value in expression.items():
        if key not in {"kind", "direction"} and type(value) is not int:
            raise ValueError("date_integer")
    if kind == "absolute_date":
        resolved = date(expression["year"], expression["month"], expression["day"])
    elif kind == "day_offset":
        if not -366 <= expression["days"] <= 366:
            raise ValueError("date_offset")
        resolved = anchor + timedelta(days=expression["days"])
    else:
        weekday = expression["iso_weekday"]
        if not 1 <= weekday <= 7:
            raise ValueError("date_weekday")
        if kind == "calendar_week_weekday":
            if expression["week_offset"] not in {-1, 0, 1}:
                raise ValueError("date_week")
            resolved = anchor + timedelta(
                days=1 - anchor.isoweekday() + 7 * expression["week_offset"] + weekday - 1
            )
        else:
            direction = expression["direction"]
            if direction not in {"previous", "next"}:
                raise ValueError("date_direction")
            delta = (
                (weekday - anchor.isoweekday()) % 7
                if direction == "next"
                else -((anchor.isoweekday() - weekday) % 7)
            )
            resolved = anchor + timedelta(days=delta or (7 if direction == "next" else -7))
    if not 1900 <= resolved.year <= 9999:
        raise ValueError("date_range")
    return resolved.isoformat()


def date_evidence_spans(expression, quote):
    """Return only Python-recognized date characters within an exact citation.

    The citation can include an entire sentence. Its unrelated monetary text
    must remain visible to the financial grammar.
    """
    kind = expression["kind"]
    patterns = []
    if kind == "absolute_date":
        y, m, d = expression["year"], expression["month"], expression["day"]
        patterns = [
            rf"(?<![0-9]){y}[-/.年]0?{m}[-/.月]0?{d}(?:日|号)?(?![0-9])",
            rf"\b{_MONTHS[m - 1]}\s+{d}(?:st|nd|rd|th)?[, ]+{y}\b",
            rf"\b{d}(?:st|nd|rd|th)?\s+{_MONTHS[m - 1]}[, ]+{y}\b",
        ]
    elif kind == "day_offset":
        days = expression["days"]
        patterns = [
            re.escape(cue) if not cue.isascii() else r"\b" + re.escape(cue) + r"\b"
            for cue in _DAY_OFFSET_CUES.get(days, ())
        ]
        amount = abs(days)
        number = rf"(?<![0-9.+-]){amount}(?![0-9.])"
        patterns.append(
            rf"\b{number}\s+days?\s+ago\b|{number}天前"
            if days < 0
            else rf"\bin\s+{number}\s+days?\b|{number}天后"
        )
    else:
        words = _WEEKDAYS[expression["iso_weekday"]]
        chinese_weekday = "|".join(re.escape(word) for word in words[1:])
        if kind == "calendar_week_weekday":
            offset = expression["week_offset"]
            english = {-1: "(?:last|previous)", 0: "this", 1: "next"}[offset]
            chinese = {-1: "上", 0: "(?:本|这)", 1: "下"}[offset]
            patterns = [
                rf"\b{english}\s+week[ ,]+(?:on\s+)?{words[0]}\b",
                rf"{chinese}(?:{chinese_weekday})",
            ]
        else:
            direction = expression["direction"]
            prefix = "(?:previous|last)" if direction == "previous" else "next"
            chinese = "(?:上一个|上个|上一)" if direction == "previous" else "(?:下一个|下个|下一)"
            patterns = [rf"\b{prefix}\s+{words[0]}\b", rf"{chinese}(?:{chinese_weekday})"]
    spans = {
        (match.start(), match.end())
        for pattern in patterns
        for match in re.finditer(pattern, quote, re.I)
    }
    # 'yesterday' inside 'the day before yesterday' is a different operation.
    if kind == "day_offset" and expression["days"] in {-1, 1}:
        compound = list(
            re.finditer(r"\bthe day (?:before yesterday|after tomorrow)\b", quote, re.I)
        )
        spans = {
            (a, b) for a, b in spans if not any(m.start() <= a and b <= m.end() for m in compound)
        }
    return sorted(spans)


def date_evidence_supported(expression, quote, resolved):
    return bool(date_evidence_spans(expression, quote))


def evidence_texts(projection: dict[str, Any]) -> dict[str, str]:
    return {
        "source": projection["source_text"],
        "reply": projection["reply_text"],
        "context": canonical_text(projection["current_values"]),
        "source_context": canonical_text(projection["interpretation_context"]),
    }


def _evidence(values: Any, texts: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError("evidence_list")
    for item in values:
        if not isinstance(item, dict) or set(item) != {"ref", "start", "end", "quote", "role"}:
            raise ValueError("evidence_keys")
        ref, start, end = item["ref"], item["start"], item["end"]
        if (
            ref not in texts
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(texts[ref])
        ):
            raise ValueError("evidence_offsets")
        if (
            texts[ref][start:end] != item["quote"]
            or len(item["quote"]) > 2048
            or item["role"] not in {"replacement_value", "previous_value", "retained_value"}
        ):
            raise ValueError("evidence_quote")
    return values


def _coverage(items: Any, text: str, operations: set[str], questions: set[str]) -> None:
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        raise ValueError("coverage_list")
    previous = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "start",
            "end",
            "label",
            "operation_ids",
            "question_ids",
        }:
            raise ValueError("coverage_keys")
        a, b = item["start"], item["end"]
        if (
            type(a) is not int
            or type(b) is not int
            or not previous <= a < b <= len(text)
            or text[previous:a].strip()
        ):
            raise ValueError("coverage_gap")
        if item["label"] not in {
            "resolved_instruction",
            "unresolved_instruction",
            "non_instruction",
            "confirmation_request",
        }:
            raise ValueError("coverage_label")
        for key, allowed in (("operation_ids", operations), ("question_ids", questions)):
            value = item[key]
            if (
                not isinstance(value, list)
                or len(value) > 6
                or any(not isinstance(v, str) or v not in allowed for v in value)
                or len(value) != len(set(value))
            ):
                raise ValueError("coverage_reference")
        if (
            item["label"] == "resolved_instruction"
            and not item["operation_ids"]
            or item["label"] == "unresolved_instruction"
            and not item["question_ids"]
        ):
            raise ValueError("coverage_instruction")
        if item["label"] in {"non_instruction", "confirmation_request"} and (
            item["operation_ids"] or item["question_ids"]
        ):
            raise ValueError("coverage_authority")
        previous = b
    if text[previous:].strip():
        raise ValueError("coverage_gap")


def _questions(items: Any, texts: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > 6:
        raise ValueError("questions")
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "question_id",
            "fields",
            "reason",
            "candidates",
            "evidence",
        }:
            raise ValueError("question_keys")
        identity = item["question_id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity)
            or identity in ids
        ):
            raise ValueError("question_id")
        ids.add(identity)
        if (
            not isinstance(item["fields"], list)
            or not item["fields"]
            or len(set(item["fields"])) != len(item["fields"])
            or set(item["fields"]) - set(FIELDS)
            or item["reason"] not in {"missing", "ambiguous", "unsupported_expression"}
        ):
            raise ValueError("question_fields")
        candidates = item["candidates"]
        if (
            not isinstance(candidates, list)
            or len(candidates) > 8
            or any(v is not None and (not isinstance(v, str) or len(v) > 512) for v in candidates)
        ):
            raise ValueError("question_candidates")
        _evidence(item["evidence"], texts)
    return items


def _canonical_fields(values: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    result = {field: values.get(field) for field in FIELDS}
    for field, value in result.items():
        if value is None and (not complete or field in {"description", "category"}):
            continue
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 512
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("field_value")
    for field in ("merchant", "description", "category"):
        text_value = result[field]
        if isinstance(text_value, str):
            result[field] = unicodedata.normalize("NFC", text_value)
    if result["currency"] is not None:
        result["currency"] = normalize_currency(result["currency"])
    if result["amount"] is not None:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", result["amount"]):
            raise ValueError("money_type")
        amount = money_decimal(result["amount"])
        if amount <= 0:
            raise ValueError("positive_expense")
        if result["currency"] is not None:
            result["amount"] = canonical_money_str(
                validate_amount_for_currency(amount, result["currency"]), result["currency"]
            )
    if (
        result["transaction_date"] is not None
        and date.fromisoformat(result["transaction_date"]).isoformat() != result["transaction_date"]
    ):
        raise ValueError("date")
    return result


def validate_response_v2(
    raw: str | None,
    projection: dict[str, Any],
    base: dict[str, Any],
    *,
    receipt_money: tuple[str, str, str] | None = None,
    receipt_draft_total=None,
    _intent_source_text: str | None = None,
) -> dict[str, Any]:
    try:
        if not isinstance(raw, str) or len(raw.encode()) > 16384:
            raise ValueError("body_limit")
        response = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        if (
            not isinstance(response, dict)
            or set(response)
            != {"schema", "outcome", "intent", "operations", "coverage", "unresolved"}
            or response["schema"] != AI_RESULT_SCHEMA
        ):
            raise ValueError("schema")
        outcome, intent = response["outcome"], response["intent"]
        if outcome not in {"proposal", "clarification", "unsupported"} or intent not in {
            "personal_expense",
            "refund",
            "income",
            "transfer",
            "shared_expense",
            "unknown",
        }:
            raise ValueError("outcome")
        texts = evidence_texts(projection)
        text_ref = "source" if projection["interaction_kind"] == "initial" else "reply"
        text = texts[text_ref]
        questions = _questions(response["unresolved"], texts)
        operations = response["operations"]
        if not isinstance(operations, list) or len(operations) > 6:
            raise ValueError("operations")
        ids, fields, patch, evidences = set(), set(), {}, {}
        date_spans: list[tuple[int, int]] = []
        semantic_blockers = set()
        draft = projection.get("draft_values")
        current = _canonical_fields(projection["current_values"], complete=False)
        prior_fields = {
            field for question in projection.get("unresolved", []) for field in question["fields"]
        }
        # A partial draft is interpretation, never the actual context. Carry only
        # fields whose authenticated predecessor left no unresolved obligation.
        carry = {k: v for k, v in (draft or {}).items() if k not in prior_fields}
        for op in operations:
            if not isinstance(op, dict):
                raise ValueError("operation")
            field = op.get("field")
            key = "date_expression" if field == "transaction_date" else "value"
            if (
                set(op) != {"operation_id", "op", "field", key, "evidence"}
                or op["op"] != "set"
                or field not in FIELDS
                or field in fields
                or not isinstance(op["operation_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", op["operation_id"])
                or op["operation_id"] in ids
            ):
                raise ValueError("operation_keys")
            ids.add(op["operation_id"])
            fields.add(field)
            evidence = _evidence(op["evidence"], texts)
            value = (
                compute_date(op[key], projection["interpretation_context"])
                if field == "transaction_date"
                else op[key]
            )
            for cite in evidence:
                if cite["role"] == "previous_value" and (
                    current.get(field) is None or str(current[field]) not in cite["quote"]
                ):
                    # Monetary comparison allows canonical precision padding only.
                    if (
                        field != "amount"
                        or current.get(field) is None
                        or not any(
                            money_decimal(m[0]) == money_decimal(current[field])
                            for m in re.finditer(r"[0-9]+(?:\.[0-9]+)?", cite["quote"])
                        )
                    ):
                        raise ValueError("previous_value")
                if cite["role"] == "retained_value" and current.get(field) != value:
                    raise ValueError("retained_value")
            if not any(c["role"] != "previous_value" for c in evidence):
                raise ValueError("replacement_evidence")
            if (
                projection["interaction_kind"] != "initial"
                and field in {"amount", "currency", "transaction_date"}
                and any(c["ref"] == "source" for c in evidence)
            ):
                raise ValueError("edit_requires_reply")
            for cite in evidence:
                if cite["ref"] == "source_context":
                    expected_context = projection["interpretation_context"].get(
                        "default_currency" if field == "currency" else "local_date"
                    )
                    if (
                        field not in {"currency", "transaction_date"}
                        or value != expected_context
                        or cite["quote"] != value
                        or projection["interaction_kind"] != "initial"
                    ):
                        raise ValueError("source_context_evidence")
                if cite["ref"] == "context" and (
                    value != current.get(field) or cite["role"] != "retained_value"
                ):
                    raise ValueError("context_evidence")
            patch[field], evidences[field] = value, evidence
        _coverage(response["coverage"], text, ids, {q["question_id"] for q in questions})
        if (
            outcome == "unsupported"
            or intent != "personal_expense"
            or _unsupported_intent(
                (projection["source_text"], projection["reply_text"], _intent_source_text or "")
            )
        ):
            return {
                "state": "RESPONSE_REJECTED",
                "reason": "unsupported_intent",
                "text_readiness": {
                    "schema": "finance-independent-ai-text-readiness-v2",
                    "generic_score_applies": False,
                    "restrictions": ["unsupported_intent"],
                },
            }
        reply_assignments = _reply_assignments(text) if text_ref == "reply" else []
        instruction_text = (
            _reply_instruction_text(text, reply_assignments)
            if text_ref == "reply"
            else _literal_instruction_text(text, text_ref, operations)
        )
        requested_fields = {item["field"] for item in reply_assignments} | {
            field
            for field, pattern in _EDIT_CUES.items()
            if re.search(pattern, instruction_text, re.I)
        }
        canonical_patch = _canonical_fields(patch, complete=False)
        retention_spans = {
            field: _fresh_retention_spans(
                field, canonical_patch[field], current, evidences[field], instruction_text
            )
            for field in prior_fields & fields
        }
        operations_by_field = {op["field"]: op for op in operations}
        date_operation = operations_by_field.get("transaction_date")
        if date_operation is not None:
            value = patch["transaction_date"]
            evidence = evidences["transaction_date"]
            if not retention_spans.get("transaction_date") and not any(
                c["role"] != "previous_value"
                and (
                    c["ref"] in {"context", "source_context"}
                    or (
                        date_evidence_supported(
                            date_operation["date_expression"], c["quote"], value
                        )
                        and (
                            text_ref != "reply"
                            or any(
                                instruction_text[c["start"] + a : c["start"] + b]
                                == text[c["start"] + a : c["start"] + b]
                                for a, b in date_evidence_spans(
                                    date_operation["date_expression"], c["quote"]
                                )
                            )
                        )
                    )
                )
                for c in evidence
            ):
                semantic_blockers.add("unsupported_date_expression")
                patch["transaction_date"] = (
                    current.get("transaction_date")
                    if projection["interaction_kind"] != "initial"
                    else None
                )
            if "unsupported_date_expression" not in semantic_blockers:
                for citation in evidence:
                    if citation["ref"] == text_ref and citation["role"] != "previous_value":
                        date_spans.extend(
                            (citation["start"] + a, citation["start"] + b)
                            for a, b in date_evidence_spans(
                                date_operation["date_expression"], citation["quote"]
                            )
                            if text_ref != "reply"
                            or instruction_text[citation["start"] + a : citation["start"] + b]
                            == text[citation["start"] + a : citation["start"] + b]
                        )
                date_spans.extend(retention_spans.get("transaction_date", []))
        canonical = _canonical_fields({**base, **carry, **patch}, complete=False)
        blockers: set[str] = set(semantic_blockers)
        missing_required = {
            field
            for field in ("amount", "currency", "transaction_date", "merchant")
            if canonical.get(field) is None
        }
        blockers.update("unanswered_" + field for field in missing_required)
        initial_ocr = (
            projection["interaction_kind"] == "initial"
            and projection["selection"]["kind"] == "local_ocr"
        )
        # Date evidence is excluded from monetary partition only after exact
        # offsets and closed Python operation validation, never by model label.
        date_alternative = _known_date_alternative(instruction_text) or _known_v2_date_alternative(
            instruction_text
        )
        total_evidence = receipt_money or receipt_draft_total
        source_date_supported = False
        if projection["interaction_kind"] == "initial":
            date_operation = next(
                (op for op in operations if op["field"] == "transaction_date"), None
            )
            source_date_supported = (
                date_operation is not None
                and date_operation["date_expression"]["kind"] == "absolute_date"
                and "unsupported_date_expression" not in semantic_blockers
                and any(
                    citation["ref"] == "source"
                    and citation["role"] == "replacement_value"
                    and date_evidence_spans(date_operation["date_expression"], citation["quote"])
                    for citation in date_operation["evidence"]
                )
            )
            if date_operation is None:
                date_source = (
                    total_evidence[2] if initial_ocr and total_evidence else instruction_text
                )
                source_date_supported = (
                    canonical.get("transaction_date") in assess_source_money(date_source).dates
                )
            if not date_alternative:
                instruction_text = _literal_instruction_text(
                    instruction_text,
                    text_ref,
                    operations,
                    initial=True,
                    source_date_supported=source_date_supported,
                    weak_months=True,
                )
        monetary_text = instruction_text
        for start, end in date_spans:
            monetary_text = monetary_text[:start] + " " * (end - start) + monetary_text[end:]
        assessment = assess_source_money(
            total_evidence[2] if initial_ocr and total_evidence else monetary_text,
            bare_currency=current.get("currency")
            if projection["interaction_kind"] != "initial"
            else None,
            receipt_currency=total_evidence[1] if initial_ocr and total_evidence else None,
        )
        span_offset = text.find(total_evidence[2]) if initial_ocr and total_evidence else 0
        reasons = set(assessment.reasons)
        if "competing_amounts" in reasons:
            money_evidence = evidences.get("amount", [])
            covered = []
            for span in assessment.spans:
                roles = {
                    c["role"]
                    for c in money_evidence
                    if c["ref"] == text_ref
                    and c["start"] <= span_offset + span.number_start
                    and span_offset + span.number_end <= c["end"]
                }
                target = money_decimal(span.number)
                valid = (
                    "replacement_value" in roles
                    and canonical["amount"] is not None
                    and target == money_decimal(canonical["amount"])
                    or "previous_value" in roles
                    and current.get("amount") is not None
                    and target == money_decimal(current["amount"])
                )
                covered.append(valid)
            previous_cues = re.search(
                r"(?:\bnot\s+|不是\s*|并非\s*)[0-9]+(?:\.[0-9]+)?", text, re.I
            )
            replacement_cues = re.search(
                r"\b(?:change|replace|instead)\b|改成|改为|改[\s0-9]", text, re.I
            )
            uncertainty = re.search(r"\b(?:or|uncertain|unsure)\b|或者|或是|不确定", text, re.I)
            if (
                all(covered)
                and previous_cues
                and replacement_cues
                and not uncertainty
                and any(c["role"] == "previous_value" for c in money_evidence)
            ):
                reasons.remove("competing_amounts")
        blockers.update(reasons)
        supported_pending = {
            field
            for field in prior_fields
            if _pending_answer_supported(
                field,
                operations_by_field.get(field),
                canonical.get(field),
                evidences.get(field, []),
                prior_fields,
                text,
                instruction_text,
                assessment,
                retention_spans.get(field, []),
                reply_assignments,
            )
        }
        unanswered = prior_fields - supported_pending
        blockers.update("unanswered_" + field for field in unanswered)
        unchanged = _canonical_fields({**base, **carry}, complete=False)
        for field in unanswered:
            canonical[field] = unchanged[field]
        retained_pending = {field for field in supported_pending if retention_spans.get(field)}
        if projection["interaction_kind"] != "initial":
            if (
                "amount" not in patch
                or not any(
                    c["ref"] == "reply" and c["role"] == "replacement_value"
                    for c in evidences.get("amount", [])
                )
            ) and any(
                canonical["amount"] is None
                or money_decimal(span.number) != money_decimal(canonical["amount"])
                for span in assessment.spans
            ):
                blockers.add("omitted_amount_instruction")
            if (
                "currency" not in patch
                and assessment.currencies
                and assessment.currencies != {canonical["currency"]}
            ):
                blockers.add("omitted_currency_instruction")
        if date_alternative:
            blockers.add("ambiguous_date_alternative")
        date_remainder = instruction_text
        retained_initial_date = False
        # An unchanged deterministic initial ISO source date already has exact
        # source support. A month/relative cue or a default/context replacement
        # cannot acquire this exception, and alternative dates remain visible.
        if projection["interaction_kind"] == "initial" and "transaction_date" not in patch:
            initial_date = canonical.get("transaction_date")
            if initial_date is not None and initial_date in assessment.dates:
                retained_initial_date = True
                date_remainder = re.sub(
                    r"(?<![0-9])" + re.escape(initial_date) + r"(?![0-9])",
                    lambda match: " " * len(match[0]),
                    date_remainder,
                )
        for start, end in date_spans:
            date_remainder = date_remainder[:start] + " " * (end - start) + date_remainder[end:]
        if _V2_DATE_CUE.search(date_remainder):
            blockers.add("unsupported_date_instruction")
        if assessment.dates and any(d != canonical["transaction_date"] for d in assessment.dates):
            blockers.add("competing_dates")
        if (
            "amount" in patch
            and "amount" not in retained_pending
            and not any(
                c["ref"] == "context" and c["role"] == "retained_value" for c in evidences["amount"]
            )
        ):
            if not any(
                money_decimal(span.number) == money_decimal(canonical["amount"])
                and any(
                    c["ref"] == text_ref
                    and c["role"] == "replacement_value"
                    and c["start"] <= span_offset + span.number_start
                    and span_offset + span.number_end <= c["end"]
                    for c in evidences["amount"]
                )
                for span in assessment.spans
            ):
                blockers.add("unsupported_amount")
                if text_ref == "reply":
                    canonical["amount"] = unchanged["amount"]
        if "currency" in patch and not assessment.currencies:
            if projection["interaction_kind"] == "initial":
                if canonical["currency"] != projection["interpretation_context"][
                    "default_currency"
                ] or not any(c["ref"] == "source_context" for c in evidences["currency"]):
                    blockers.add("unknown_context_currency")
            elif "currency" not in retained_pending and not any(
                c["ref"] == "context" and c["role"] == "retained_value"
                for c in evidences["currency"]
            ):
                blockers.add("unsupported_currency_evidence")
                canonical["currency"] = unchanged["currency"]
        if (
            "currency" in patch
            and assessment.currencies
            and canonical["currency"] not in assessment.currencies
        ):
            blockers.add("competing_currencies")
        if (
            assessment.currency_state != "PROVEN_ABSENT"
            and "currency" in patch
            and not assessment.currencies
        ):
            blockers.add("unsupported_currency_notation")
        if initial_ocr:
            if (
                receipt_money is not None
                and (current.get("amount"), current.get("currency")) != receipt_money[:2]
            ):
                blockers.add("receipt_initial_current_money_differs")
            if receipt_money is None:
                if receipt_draft_total is None:
                    blockers.add("receipt_total_unqualified")
                    canonical["amount"] = None
                elif canonical["amount"] is not None and money_decimal(
                    canonical["amount"]
                ) != money_decimal(receipt_draft_total[0]):
                    blockers.add("receipt_total_role_mismatch")
                    canonical["amount"] = None
                if receipt_draft_total is not None and receipt_draft_total[1] is None:
                    blockers.add("unanswered_currency")
                    canonical["currency"] = None
            elif (canonical["amount"], canonical["currency"]) != receipt_money[:2]:
                blockers.add("receipt_total_unqualified")
        if (
            _DATE_REQUEST.search(instruction_text)
            and not retained_initial_date
            and (
                "transaction_date" not in patch
                or not any(
                    c["ref"] == text_ref and c["role"] == "replacement_value"
                    for c in evidences.get("transaction_date", [])
                )
            )
        ):
            blockers.add("unanswered_transaction_date")
        if projection["interaction_kind"] != "initial":
            for field in _EDIT_CUES:
                retained_null = (
                    field in {"description", "category"}
                    and field in patch
                    and patch[field] is None
                    and current[field] is None
                    and field not in prior_fields
                    and field not in requested_fields
                    and bool(evidences.get(field))
                    and all(
                        c["ref"] == "context" and c["role"] == "retained_value"
                        for c in evidences[field]
                    )
                )
                replacement = [
                    c
                    for c in evidences.get(field, [])
                    if c["ref"] == "reply" and c["role"] == "replacement_value"
                ]
                supported_literal = (
                    field in retained_pending
                    or field not in {"merchant", "description", "category"}
                    or any(
                        (
                            isinstance(patch.get(field), str)
                            and (
                                _reply_text_assignment_supported(
                                    field,
                                    canonical_patch[field],
                                    replacement,
                                    text,
                                    reply_assignments,
                                    named_only=False,
                                )
                                or (
                                    not any(item["field"] == field for item in reply_assignments)
                                    and canonical_patch[field]
                                    in unicodedata.normalize(
                                        "NFC", instruction_text[c["start"] : c["end"]]
                                    )
                                )
                            )
                        )
                        or (
                            patch.get(field) is None
                            and _explicit_optional_clear(field, replacement, instruction_text)
                        )
                        for c in replacement
                    )
                )
                supported_literal = supported_literal and _reply_assignment_compatible(
                    field, canonical_patch[field], text, reply_assignments
                )
                if (
                    field == "currency"
                    and field not in retained_pending
                    and replacement
                    and any(token.kind == "currency" for token in assessment.partition)
                ):
                    # An ordinary explicit-code replacement cites its actual outside
                    # token too. Bare-dollar and context-only retention stay unchanged.
                    supported_literal = _explicit_currency_citation_supported(
                        canonical_patch[field], replacement, assessment
                    )
                if (
                    not retained_null
                    and (
                        field in requested_fields
                        or field in patch
                        and (patch[field] is None or replacement)
                    )
                    and (field not in patch or not replacement or not supported_literal)
                ):
                    blockers.add("unanswered_" + field)
                    # A model-only answer remains a question, never a solved
                    # draft value that a later unrelated reply could carry.
                    if field in patch:
                        canonical[field] = unchanged[field]
        if questions or any(c["label"] == "unresolved_instruction" for c in response["coverage"]):
            blockers.add("unresolved_instruction")
        if outcome == "clarification" or blockers:
            targets = set(unanswered) | missing_required
            for blocker in blockers:
                if (
                    blocker.startswith("unanswered_")
                    and blocker.removeprefix("unanswered_") in FIELDS
                ):
                    targets.add(blocker.removeprefix("unanswered_"))
                elif "date" in blocker:
                    targets.add("transaction_date")
                elif blocker in {
                    "unsupported_currency_notation",
                    "unsupported_source_money_notation",
                    "receipt_initial_current_money_differs",
                }:
                    # Currency on unresolved replacement money is a joint obligation.
                    targets.update({"amount", "currency"})
                elif "currency" in blocker:
                    targets.add("currency")
                elif "amount" in blocker or "money" in blocker or "total" in blocker:
                    targets.add("amount")
                elif blocker == "omitted_field_instruction":
                    targets.update(
                        field
                        for field, pattern in _EDIT_CUES.items()
                        if (
                            field in requested_fields
                            if text_ref == "reply"
                            else re.search(pattern, text, re.I)
                        )
                        and field not in patch
                    )
            covered_fields = {field for question in questions for field in question["fields"]}
            question_ids = {question["question_id"] for question in questions}
            question_evidence = {
                "ref": text_ref,
                "start": 0,
                "end": len(text),
                "quote": text,
                "role": "replacement_value",
            }
            for field in FIELDS:
                if field not in targets or field in covered_fields:
                    continue
                identity = "core_" + field
                while identity in question_ids:
                    identity += "_"
                question_ids.add(identity)
                questions.append(
                    {
                        "question_id": identity,
                        "fields": [field],
                        "reason": "ambiguous" if canonical.get(field) is not None else "missing",
                        "candidates": [],
                        "evidence": [question_evidence],
                    }
                )
            if not questions:
                # Unmapped restrictions must never be disguised as amount guesses.
                return {"state": "RESPONSE_REJECTED", "reason": "unmapped_source_constraint"}
            return {
                "state": "NEEDS_CLARIFICATION",
                "reason": "source_ambiguity",
                "draft_values": canonical,
                "unresolved": questions,
                "hard_blockers": sorted(blockers),
                "text_readiness": {
                    "schema": "finance-independent-ai-text-readiness-v2",
                    "generic_score_applies": False,
                    "restrictions": sorted(blockers),
                },
            }
        result = {
            **base,
            **canonical,
            "intent": "personal_expense_log",
            "transaction_type": "personal_expense",
        }
        result.pop("correction", None)
        result.pop("field_evidence", None)
        return {
            "state": "VALID",
            "reason": None,
            "payload": result,
            "fields": list(patch),
            "evidence": evidences,
            "hard_blockers": [],
            "text_readiness": {
                "schema": "finance-independent-ai-text-readiness-v2",
                "generic_score_applies": False,
                "restrictions": [],
            },
        }
    except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError):
        return {"state": "RESPONSE_REJECTED", "reason": "invalid_response"}
