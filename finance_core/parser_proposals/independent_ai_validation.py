"""Pure strict validation against the exact bounded text projection."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from importlib.resources import files
from typing import Any

from finance_core.application.ai_invocation_contract import AI_RESULT_SCHEMA_V1 as AI_RESULT_SCHEMA
from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.money import (
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.independent_ai_source_grammar import (
    SourceMoneyAssessment,
    assess_source_money,
)

FIELDS = ("amount", "currency", "transaction_date", "merchant", "description", "category")

_DATE_CUE = (
    r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}|(?<!\d)\d{1,2}/\d{1,2}(?!\d)|"
    r"\d{1,2}月\d{1,2}|\d+(?:年|月|日|号)|"
    r"昨天|前天|前日|明天|今天|今日|昨日|上周|上星期|上个月|上月|去年|前年|今年|"
    r"明年|下周|下个月|本周|本月|星期|周[一二三四五六日天]|"
    r"yesterday|today|tomorrow|(?:last|next|this|previous)\s*(?:week|month|year|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b"
)


def _known_date_alternative(text: str) -> bool:
    """Identify adjoining ISO/known-cue alternatives in bounded raw arms.

    A cue need not consume its whole arm, and no natural date is converted.
    Neighboring connectors and clause boundaries prevent borrowing a distant
    cue. Original source-context absence and date-edit gates stay unchanged.
    """
    iso = r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"
    # Periods inside ASCII decimal/dotted-date tokens are not clause cuts.
    # Fullwidth punctuation only delimits an arm; it grants no input support.
    cuts = list(
        re.finditer(
            r"(?P<alternative>\bor\b|或者|或|还是)|"
            r"[;；\r\n!?！？。]|(?<![0-9])\.|\.(?![0-9])",
            text,
            re.I,
        )
    )
    for i, cut in enumerate(cuts):
        if cut.lastgroup != "alternative":
            continue
        left = text[cuts[i - 1].end() if i else 0 : cut.start()]
        right = text[cut.end() : cuts[i + 1].start() if i + 1 < len(cuts) else len(text)]
        if (
            re.search(iso + r"\s*\Z", left)
            and re.search(_DATE_CUE, right, re.I)
            or re.match(r"\s*" + iso, right)
            and re.search(_DATE_CUE, left, re.I)
        ):
            return True
    return False


def canonical_text(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha(value: object) -> str:
    return hashlib.sha256(canonical_text(value).encode()).hexdigest()


def assets(version: str = "v1") -> dict[str, str]:
    root = files("finance_core.resources").joinpath("independent_ai")
    registry = json.loads(root.joinpath("registry_" + version + ".json").read_text())
    for name, digest in registry.items():
        if hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest() != digest:
            raise AiInvocationError("Independent AI resource identity changed")
    return registry


def check_private_text(text: str) -> None:
    assets()
    policy = json.loads(
        files("finance_core.resources").joinpath("independent_ai/policy_v1.json").read_text()
    )
    if any(re.search(p, text, re.I) for p in policy["reject_if_any_pattern_matches"]) or any(
        marker.casefold() in text.casefold()
        for marker in policy["configured_finance_secret_markers"]
    ):
        raise AiInvocationError("sensitive_text_detected")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate key")
        result[k] = v
    return result


def currency_tokens(text: str) -> set[str]:
    """Compatibility view of the single complete source assessment."""
    assessment = assess_source_money(text)
    return set(assessment.currencies)


def monetary_tokens(text: str) -> list[str]:
    """Only complete qualified spans, never a new unsigned substring scan."""
    assessment = assess_source_money(text)
    return [span.number for span in assessment.spans] if assessment.qualified else []


def unsupported_source_money_notation(text: str) -> bool:
    return not assess_source_money(text).qualified


def text_readiness(
    projection: dict[str, Any],
    base: dict[str, Any],
    result: dict[str, Any],
    patch: dict[str, Any],
    *,
    _assessment: SourceMoneyAssessment | None = None,
) -> dict[str, Any]:
    policy = json.loads(
        files("finance_core.resources")
        .joinpath("independent_ai/text_readiness_v1.json")
        .read_text()
    )
    text = (
        projection["source_text"] if projection["kind"] == "initial" else projection["reply_text"]
    )
    initial_ocr = projection["kind"] == "initial" and projection["selection"]["kind"] == "local_ocr"
    # Receipt amount roles/technical ambiguity/confidence remain the receipt
    # owner's responsibility. Every natural edit is text, regardless of origin.
    restrictions = set() if initial_ocr else set(base.get("ambiguity_flags", []))
    assessment = _assessment or assess_source_money(
        text,
        bare_currency=(
            projection["current_values"].get("currency")
            if projection["kind"] == "edit" and "currency" not in patch
            else None
        ),
    )
    if not initial_ocr:
        restrictions.update(assessment.reasons)
    for token in policy["deny_intent_tokens"]:
        if re.search(
            re.escape(token)
            if any("\u4e00" <= c <= "\u9fff" for c in token)
            else r"(?<![\w])" + re.escape(token) + r"(?![\w])",
            text,
            re.I,
        ):
            restrictions.add("unsupported_intent")
    if any(re.search(pattern, text, re.I) for pattern in policy["deny_intent_patterns"]):
        restrictions.add("unsupported_intent")
    # Initial receipts keep non-TOTAL amounts/signs under their original role
    # owner, but explicit unsupported/competing currencies in the source stay.
    currency_assessment = assess_source_money(text) if initial_ocr else assessment
    currencies = set(currency_assessment.currencies)
    if "unsupported_currency_notation" in currency_assessment.reasons:
        restrictions.add("unsupported_currency_notation")
    if len(currencies) > 1 or currencies and result["currency"] not in currencies:
        restrictions.add("competing_currencies")
    if initial_ocr:
        return {
            "schema": policy["schema"],
            "generic_score_applies": True,
            "restrictions": sorted(restrictions),
        }
    dates = list(assessment.dates)
    if _known_date_alternative(text):
        restrictions.add("unresolved_date_cue")
    if projection["kind"] == "edit" and "transaction_date" in patch:
        # A cited ISO date does not erase an unparsed alternative request.
        # Only date patches use this cue rule; descriptive date words remain
        # descriptive. Reuse the initial-context gate without date inference.
        remainder = re.sub(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", "", text)
        if re.search(_DATE_CUE, remainder, re.I):
            restrictions.add("unresolved_date_cue")
    if (
        len(set(dates)) > 1
        or dates
        and result["transaction_date"] not in dates
        and "transaction_date" in patch
    ):
        restrictions.add("competing_dates")
    candidates = (
        {money_decimal(span.number) for span in assessment.spans} if assessment.qualified else set()
    )
    if len(candidates) > 1:
        restrictions.add("competing_amounts")
    if "amount" in patch and (not candidates or money_decimal(result["amount"]) not in candidates):
        restrictions.add("unsupported_amount")
    # A material monetary edit comes from the retained reply; it cannot cite
    # the original transaction's money to manufacture a new human request.
    if projection["kind"] == "edit":
        if (
            "currency" not in patch
            and projection["current_values"].get("currency") != result["currency"]
        ):
            restrictions.add("unknown_context_currency")
    return {
        "schema": policy["schema"],
        "generic_score_applies": bool(restrictions),
        "restrictions": sorted(restrictions),
    }


def validate_response(
    raw: str | None,
    projection: dict[str, Any],
    base: dict[str, Any],
    *,
    _receipt_money: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """An invalid member refuses the whole response; retain raw separately."""
    try:
        if not isinstance(raw, str) or len(raw.encode()) > 16384:
            raise ValueError("body_limit")
        response = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        if not isinstance(response, dict) or response.get("schema") != AI_RESULT_SCHEMA:
            raise ValueError("schema")
        kind = response.get("kind")
        if kind in ("clarification", "unsupported"):
            if (
                set(response) != {"schema", "kind", "reason"}
                or not isinstance(response["reason"], str)
                or len(response["reason"]) > 256
            ):
                raise ValueError("schema")
            return {
                "state": "NEEDS_CLARIFICATION" if kind == "clarification" else "RESPONSE_REJECTED",
                "reason": kind,
            }
        if set(response) != {"schema", "kind", "fields", "evidence"} or kind != (
            "initial" if projection["kind"] == "initial" else "patch"
        ):
            raise ValueError("schema")
        patch, evidence = response["fields"], response["evidence"]
        if (
            not isinstance(patch, dict)
            or not patch
            or set(patch) - set(FIELDS)
            or not isinstance(evidence, dict)
            or set(evidence) != set(patch)
        ):
            raise ValueError("fields")
        if kind == "initial" and not {"amount", "currency", "transaction_date"} <= set(patch):
            raise ValueError("incomplete_initial")
        initial_ocr = (
            projection["kind"] == "initial" and projection["selection"]["kind"] == "local_ocr"
        )
        source_text = (
            projection["source_text"]
            if projection["kind"] == "initial"
            else projection.get("reply_text", "")
        )
        assessed_text = _receipt_money[2] if initial_ocr and _receipt_money else source_text
        assessment = assess_source_money(
            assessed_text,
            bare_currency=(
                projection["current_values"].get("currency")
                if projection["kind"] == "edit" and "currency" not in patch
                else None
            ),
            receipt_currency=_receipt_money[1] if initial_ocr and _receipt_money else None,
        )
        for field, value in patch.items():
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or len(value) > 512
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("field_value")
            cite = evidence[field]
            if (
                not isinstance(cite, dict)
                or set(cite) != {"ref", "quote"}
                or not isinstance(cite["quote"], str)
                or not cite["quote"]
            ):
                raise ValueError("evidence")
            ref, quote = cite["ref"], cite["quote"]
            texts = {"source": projection["source_text"], "reply": projection.get("reply_text", "")}
            if ref == "source_context":
                if (
                    projection["kind"] != "initial"
                    or field not in {"currency", "transaction_date"}
                    or projection["source_context"].get(field) != value
                    or quote != value
                ):
                    raise ValueError("source_context_evidence")
                if field == "currency" and assessment.currency_state != "PROVEN_ABSENT":
                    raise ValueError("explicit_currency_required")
                if field == "transaction_date" and re.search(
                    _DATE_CUE, projection["source_text"], re.I
                ):
                    raise ValueError("explicit_date_required")
            elif ref == "context":
                if (
                    projection["kind"] != "edit"
                    or projection["current_values"].get(field) != value
                    or quote != value
                ):
                    raise ValueError("context_evidence")
            elif ref not in texts or quote not in texts[ref]:
                raise ValueError("unsupported_evidence")
            elif (
                projection["kind"] == "edit"
                and field in {"amount", "currency", "transaction_date"}
                and ref == "source"
            ):
                raise ValueError("material_edit_requires_reply_or_exact_context")
            elif field == "transaction_date" and value not in quote:
                raise ValueError("unsupported_material_value")
            elif field == "currency" and not any(
                token.currency == value and token.raw in quote
                for token in assessment.partition
                if token.kind == "currency"
            ):
                raise ValueError("unsupported_material_value")
            elif field == "amount":
                if assessment.qualified and not assessment.supports_quote(
                    quote, value, source=texts[ref]
                ):
                    raise ValueError("unsupported_amount")
        result = {**base, **patch}
        if not isinstance(result.get("amount"), str) or not re.fullmatch(
            r"[0-9]+(?:\.[0-9]+)?", result["amount"]
        ):
            raise ValueError("money_type")
        currency = normalize_currency(result["currency"])
        amount = validate_amount_for_currency(money_decimal(result["amount"]), currency)
        if amount <= 0:
            raise ValueError("positive_expense")
        result["amount"] = canonical_money_str(amount, currency)
        result["currency"] = currency
        value = result["transaction_date"]
        if (
            not isinstance(value, str)
            or len(value) != 10
            or date.fromisoformat(value).isoformat() != value
        ):
            raise ValueError("date")
        if not result.get("merchant") and not result.get("description"):
            raise ValueError("merchant_or_description")
        if result.get("intent") not in (
            None,
            "personal_expense_log",
            "simple_expense_log",
            "personal_expense",
        ) or result.get("transaction_type") not in (
            None,
            "personal_expense",
            "simple_expense",
            "expense",
        ):
            raise ValueError("unsupported_scope")
        if any(
            result.get(k) is not None
            for k in ("account", "account_id", "payer", "paid_by", "participants", "ledger")
        ):
            raise ValueError("unsupported_scope")
        for field in FIELDS:
            if isinstance(result.get(field), str):
                result[field] = unicodedata.normalize("NFC", result[field])
        result["intent"], result["transaction_type"] = "personal_expense_log", "personal_expense"
        # New AI values do not assert inherited HUMAN correction authority.
        result.pop("correction", None)
        inherited = base.get("field_evidence")
        if isinstance(inherited, list):
            result["field_evidence"] = [
                item
                for item in inherited
                if isinstance(item, dict)
                and item.get("field_name") in FIELDS
                and item.get("proposed_value") == result.get(item["field_name"])
            ]
        else:
            result.pop("field_evidence", None)
        readiness = text_readiness(projection, base, result, patch, _assessment=assessment)
        if projection["kind"] == "initial" and projection["selection"]["kind"] == "local_ocr":
            restrictions = set(readiness["restrictions"])
            if _receipt_money is None:
                restrictions.add("receipt_total_unqualified")
            else:
                qualified_amount, qualified_currency, _ = _receipt_money
                if (result["amount"], result["currency"]) != (qualified_amount, qualified_currency):
                    restrictions.add("receipt_total_role_mismatch")
                if (base.get("amount"), base.get("currency")) != (
                    qualified_amount,
                    qualified_currency,
                ):
                    restrictions.add("receipt_initial_current_money_differs")
                restrictions.update(assessment.reasons)
                if len({money_decimal(span.number) for span in assessment.spans}) != 1:
                    restrictions.add("receipt_total_unqualified")
            readiness["restrictions"] = sorted(restrictions)
        if readiness["restrictions"]:
            return {
                "state": "NEEDS_CLARIFICATION",
                "reason": "source_ambiguity",
                "text_readiness": readiness,
            }
        return {
            "state": "VALID",
            "reason": None,
            "payload": result,
            "fields": list(patch),
            "evidence": evidence,
            "text_readiness": readiness,
        }
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return {"state": "RESPONSE_REJECTED", "reason": "invalid_response"}
