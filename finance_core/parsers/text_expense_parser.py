from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

CONFIRMATION_REQUIRED = True
PROPOSAL_STATUS = "parsed_pending_confirmation"
SELF_PAYER = "Owner"

CURRENCY_ALIASES = {
    "SGD": "SGD",
    "JPY": "JPY",
    "MYR": "MYR",
    "RM": "MYR",
}
SYMBOL_CURRENCIES = {"$": "SGD"}

_AMOUNT_RE = re.compile(
    r"""
    (?<![A-Za-z0-9])
    (?P<token>
      (?:(?P<prefix_currency>SGD|JPY|MYR|RM)\s*|\$\s*)?
      (?P<amount>\d+(?:,\d{3})*(?:\.\d{1,2})?)
      (?:\s*(?P<suffix_currency>SGD|JPY|MYR|RM))?
    )
    (?![A-Za-z0-9])
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PAID_BY_RE = re.compile(
    r"\bpaid\s+by\s+(?P<payer>[A-Za-z][A-Za-z0-9_-]*)\b",
    re.IGNORECASE,
)
_AT_MERCHANT_RE = re.compile(
    r"\bat\s+(?P<merchant>[A-Za-z][A-Za-z0-9&'_-]*)\b",
    re.IGNORECASE,
)
_WITH_PARTICIPANTS_RE = re.compile(
    r"\b(?:with|shared\s+(?:equally\s+)?with|split\s+(?:equally\s+)?with)\s+"
    r"(?P<participants>[A-Za-z][A-Za-z0-9_,\s&+-]*?)"
    r"(?=\s*,|\s+split\b|\s+shared\b|\s+paid\b|$)",
    re.IGNORECASE,
)
_FOR_PARTICIPANTS_RE = re.compile(
    r"\bfor\s+(?P<participants>[A-Za-z][A-Za-z0-9_,\s&+-]*?)"
    r"(?=\s*,|\s+split\b|\s+shared\b|$)",
    re.IGNORECASE,
)
_PAID_BACK_RE = re.compile(
    r"\b(?P<person>[A-Za-z][A-Za-z0-9_-]*)\s+paid\s+me\s+back"
    r"(?:\s+(?P<amount>\d+(?:\.\d{1,2})?))?\b",
    re.IGNORECASE,
)
_FOR_DESCRIPTION_RE = re.compile(r"\bfor\s+(?P<description>[^,]+)", re.IGNORECASE)

_MERCHANT_STOP_WORDS = {
    "i",
    "paid",
    "sgd",
    "jpy",
    "myr",
    "rm",
    "coffee",
    "lunch",
    "dinner",
    "hotel",
    "groceries",
    "grocery",
    "malaysia",
}
_PARTICIPANT_STOP_WORDS = {
    "and",
    "for",
    "split",
    "shared",
    "equally",
    "paid",
    "me",
    "back",
}


@dataclass(frozen=True)
class AmountCandidate:
    amount: Decimal
    currency: str | None
    currency_token: str | None
    token: str
    start: int
    end: int

    @property
    def amount_text(self) -> str:
        return _format_decimal(self.amount)


def parse_text_expense(
    raw_input: str,
    *,
    raw_input_reference: str | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    """Parse text expense input into a non-final proposal."""
    normalized = " ".join(raw_input.split())
    amount_candidates = find_amount_candidates(raw_input)
    primary_amount = _select_primary_amount(raw_input, amount_candidates)
    secondary_amounts = [
        candidate for candidate in amount_candidates if candidate is not primary_amount
    ]

    paid_back = _detect_paid_back(raw_input, amount_candidates)
    paid_by, paid_by_evidence = _detect_payer(raw_input, paid_back)
    participants, participant_evidence = _detect_participants(raw_input, paid_by, paid_back)
    split_type, split_evidence = _detect_split_type(raw_input, paid_back)
    transaction_type = "shared_expense" if split_type or participants else "personal_expense"

    merchant, merchant_evidence = _detect_merchant(
        raw_input,
        normalized,
        primary_amount,
    )
    description, description_evidence = _detect_description(
        raw_input,
        normalized,
        primary_amount,
        merchant,
        participants,
    )
    category = _detect_category(" ".join(value for value in (description, merchant) if value))
    missing_fields = _missing_fields(
        amount=primary_amount,
        currency=primary_amount.currency if primary_amount else None,
        merchant=merchant,
        description=description,
        transaction_type=transaction_type,
        participants=participants,
        split_type=split_type,
    )
    field_confidence = _field_confidence(
        primary_amount=primary_amount,
        amount_candidates=amount_candidates,
        merchant=merchant,
        description=description,
        participants=participants,
        split_type=split_type,
        transaction_type=transaction_type,
    )
    confidence = _overall_confidence(field_confidence, missing_fields, amount_candidates)
    evidence = _build_field_evidence(
        raw_input_reference=raw_input_reference,
        primary_amount=primary_amount,
        secondary_amounts=secondary_amounts,
        paid_back=paid_back,
        paid_by=paid_by,
        paid_by_evidence=paid_by_evidence,
        merchant=merchant,
        merchant_evidence=merchant_evidence,
        description=description,
        description_evidence=description_evidence,
        participants=participants,
        participant_evidence=participant_evidence,
        split_type=split_type,
        split_evidence=split_evidence,
        field_confidence=field_confidence,
    )

    proposal = {
        "intent": f"{transaction_type}_log",
        "transaction_type": transaction_type,
        "status": PROPOSAL_STATUS,
        "description": description,
        "merchant": merchant,
        "amount": primary_amount.amount if primary_amount else None,
        "currency": primary_amount.currency if primary_amount else None,
        "paid_by": paid_by,
        "participants": participants,
        "split_type": split_type,
        "category": category,
        "confidence": confidence,
        "confidence_metadata": {
            "overall_score": confidence,
            "field_confidence": field_confidence,
            "reasons": _confidence_reasons(
                missing_fields=missing_fields,
                amount_candidates=amount_candidates,
                primary_amount=primary_amount,
                transaction_type=transaction_type,
            ),
        },
        "field_confidence": field_confidence,
        "field_evidence": evidence,
        "missing_fields": missing_fields,
        "confirmation_required": CONFIRMATION_REQUIRED,
        "raw_input_reference": raw_input_reference,
        "source_tracking": {
            "source_type": source_type,
            "source_reference": raw_input_reference,
            "raw_input_preserved": True,
            "evidence_source": "raw_input",
        },
        "is_final": False,
    }

    if secondary_amounts:
        proposal["secondary_amounts"] = [
            {
                "amount": candidate.amount,
                "currency": candidate.currency,
                "source_text": candidate.token,
            }
            for candidate in secondary_amounts
        ]
    foreign_amount = _foreign_amount(primary_amount, secondary_amounts)
    if foreign_amount:
        proposal["foreign_amount"] = foreign_amount.amount
        proposal["foreign_currency"] = foreign_amount.currency
    if paid_back:
        proposal["related_payments"] = [
            {
                "person": paid_back["person"],
                "relationship": "paid_me_back",
                "amount": paid_back.get("amount"),
                "currency": primary_amount.currency if primary_amount else None,
                "source_text": paid_back["substring"],
            }
        ]

    return proposal


def find_amount_candidates(raw_input: str) -> list[AmountCandidate]:
    candidates = []
    for match in _AMOUNT_RE.finditer(raw_input):
        token = match.group("token")
        if not token or not re.search(r"\d", token):
            continue
        prefix_currency = match.group("prefix_currency")
        suffix_currency = match.group("suffix_currency")
        currency_token = prefix_currency or suffix_currency
        currency = None
        if currency_token:
            currency = CURRENCY_ALIASES[currency_token.upper()]
        elif token.strip().startswith("$"):
            currency_token = "$"
            currency = SYMBOL_CURRENCIES["$"]
        amount = Decimal(match.group("amount").replace(",", ""))
        candidates.append(
            AmountCandidate(
                amount=amount,
                currency=currency,
                currency_token=currency_token,
                token=token,
                start=match.start("token"),
                end=match.end("token"),
            )
        )
    return candidates


def _select_primary_amount(
    raw_input: str,
    candidates: list[AmountCandidate],
) -> AmountCandidate | None:
    if not candidates:
        return None
    for candidate in candidates:
        after = raw_input[candidate.end : candidate.end + 24]
        if candidate.currency == "SGD" and re.search(r"\bcharged\b", after, re.IGNORECASE):
            return candidate
    return candidates[0]


def _detect_payer(
    raw_input: str,
    paid_back: dict[str, Any] | None,
) -> tuple[str | None, tuple[str, int, int] | None]:
    explicit = _PAID_BY_RE.search(raw_input)
    if explicit:
        return explicit.group("payer"), (
            explicit.group(0),
            explicit.start(0),
            explicit.end(0),
        )
    self_match = re.search(r"\b(?:I\s+paid|Paid)\b", raw_input, re.IGNORECASE)
    if self_match:
        return SELF_PAYER, (self_match.group(0), self_match.start(0), self_match.end(0))
    if paid_back:
        return SELF_PAYER, (
            paid_back["substring"],
            paid_back["start"],
            paid_back["end"],
        )
    return None, None


def _detect_paid_back(
    raw_input: str,
    amount_candidates: list[AmountCandidate],
) -> dict[str, Any] | None:
    match = _PAID_BACK_RE.search(raw_input)
    if not match:
        return None
    amount = None
    if match.group("amount"):
        amount = Decimal(match.group("amount"))
    elif len(amount_candidates) > 1:
        amount = amount_candidates[-1].amount
    return {
        "person": match.group("person"),
        "amount": amount,
        "substring": match.group(0),
        "start": match.start(0),
        "end": match.end(0),
    }


def _detect_participants(
    raw_input: str,
    paid_by: str | None,
    paid_back: dict[str, Any] | None,
) -> tuple[list[str] | None, tuple[str, int, int] | None]:
    evidence_match = None
    participants: list[str] = []
    with_match = _WITH_PARTICIPANTS_RE.search(raw_input)
    if with_match:
        participants.extend(_parse_people(with_match.group("participants")))
        evidence_match = (with_match.group(0), with_match.start(0), with_match.end(0))
    elif re.search(r"\b(?:split|shared)\b", raw_input, re.IGNORECASE):
        for_match = _FOR_PARTICIPANTS_RE.search(raw_input)
        if for_match:
            parsed_people = _parse_people(for_match.group("participants"))
            if _looks_like_people(parsed_people):
                participants.extend(parsed_people)
                evidence_match = (for_match.group(0), for_match.start(0), for_match.end(0))
    if paid_back:
        participants.append(paid_back["person"])
        evidence_match = (
            paid_back["substring"],
            paid_back["start"],
            paid_back["end"],
        )
    if paid_by and participants and paid_by not in participants:
        participants.insert(0, paid_by)
    deduped = []
    for person in participants:
        if person not in deduped:
            deduped.append(person)
    return deduped or None, evidence_match


def _detect_split_type(
    raw_input: str,
    paid_back: dict[str, Any] | None,
) -> tuple[str | None, tuple[str, int, int] | None]:
    equal = re.search(
        r"\b(?:shared\s+equally|split\s+equally|equally\s+split)\b",
        raw_input,
        re.IGNORECASE,
    )
    if equal:
        return "equal", (equal.group(0), equal.start(0), equal.end(0))
    split = re.search(r"\b(?:shared|split)\b", raw_input, re.IGNORECASE)
    if split:
        return "unspecified", (split.group(0), split.start(0), split.end(0))
    if paid_back:
        return "unspecified", (
            paid_back["substring"],
            paid_back["start"],
            paid_back["end"],
        )
    return None, None


def _detect_merchant(
    raw_input: str,
    normalized: str,
    amount: AmountCandidate | None,
) -> tuple[str | None, tuple[str, int, int] | None]:
    at_match = _AT_MERCHANT_RE.search(raw_input)
    if at_match:
        return at_match.group("merchant"), (
            at_match.group("merchant"),
            at_match.start("merchant"),
            at_match.end("merchant"),
        )

    if amount:
        before_amount = " ".join(raw_input[: amount.start].split())
        tokens = before_amount.split()
        for token in tokens:
            cleaned_token = _clean_word(token)
            if (
                cleaned_token
                and cleaned_token.lower() not in _MERCHANT_STOP_WORDS
                and not re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d{1,2})?", cleaned_token)
            ):
                start = raw_input.lower().find(cleaned_token.lower())
                return cleaned_token, (
                    cleaned_token,
                    start,
                    start + len(cleaned_token),
                )

    first_token = _clean_word(normalized.split()[0]) if normalized else None
    if first_token and first_token.lower() not in _MERCHANT_STOP_WORDS:
        start = raw_input.lower().find(first_token.lower())
        return first_token, (first_token, start, start + len(first_token))
    return None, None


def _detect_description(
    raw_input: str,
    normalized: str,
    amount: AmountCandidate | None,
    merchant: str | None,
    participants: list[str] | None,
) -> tuple[str | None, tuple[str, int, int] | None]:
    for_match = _FOR_DESCRIPTION_RE.search(raw_input)
    if for_match:
        description = for_match.group("description").strip()
        if not _looks_like_participants(description, participants):
            return description, (
                description,
                for_match.start("description"),
                for_match.end("description"),
            )

    if amount:
        before_amount = " ".join(raw_input[: amount.start].split())
        before_description = _clean_description(before_amount, merchant)
        if before_description:
            start = _find_span_start(raw_input, before_description)
            return before_description, (before_description, start, start + len(before_description))

        after_amount = " ".join(raw_input[amount.end :].split())
        after_description = _clean_description(after_amount, merchant)
        if after_description:
            start = raw_input.find(after_description, amount.end)
            if start == -1:
                start = amount.end
            return after_description, (after_description, start, start + len(after_description))

        return None, None

    return normalized or None, (normalized, 0, len(raw_input)) if normalized else None


def _clean_description(value: str, merchant: str | None) -> str | None:
    cleaned = value.strip()
    cleaned = re.sub(r"\b\w+\s+paid\s+me\s+back\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:I\s+paid|Paid)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpaid\s+by\s+\w+\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:SGD|JPY|MYR|RM)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", "", cleaned)
    cleaned = re.sub(r"\b\d+(?:,\d{3})*(?:\.\d{1,2})?\b", "", cleaned)
    cleaned = re.sub(r"\bat\s+\w+\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwith\s+.+$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bsplit\s+.+$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bshared\s+.+$", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split()).strip(" ,")
    if merchant and cleaned == merchant:
        return None
    if merchant and cleaned.lower().startswith(f"{merchant.lower()} "):
        cleaned = cleaned[len(merchant) :].strip()
    return cleaned or None


def _detect_category(description: str | None) -> str | None:
    if not description:
        return None
    lowered = description.lower()
    categories = (
        ("groceries", ("groceries", "grocery", "ntuc")),
        ("coffee", ("coffee", "starbucks")),
        ("dining", ("lunch", "dinner")),
        ("travel", ("grab", "hotel")),
    )
    for category, keywords in categories:
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def _missing_fields(
    *,
    amount: AmountCandidate | None,
    currency: str | None,
    merchant: str | None,
    description: str | None,
    transaction_type: str,
    participants: list[str] | None,
    split_type: str | None,
) -> list[str]:
    missing = []
    if amount is None:
        missing.append("amount")
    if currency is None:
        missing.append("currency")
    if not merchant and not description:
        missing.append("merchant_or_description")
    if transaction_type == "shared_expense":
        if not participants:
            missing.append("participants")
        if split_type in {None, "unspecified"}:
            missing.append("split_type")
    return missing


def _field_confidence(
    *,
    primary_amount: AmountCandidate | None,
    amount_candidates: list[AmountCandidate],
    merchant: str | None,
    description: str | None,
    participants: list[str] | None,
    split_type: str | None,
    transaction_type: str,
) -> dict[str, Decimal]:
    amount_confidence = Decimal("0.00")
    currency_confidence = Decimal("0.00")
    if primary_amount:
        amount_confidence = Decimal("0.95") if len(amount_candidates) == 1 else Decimal("0.72")
        if primary_amount.currency_token == "$":
            currency_confidence = Decimal("0.82")
        elif primary_amount.currency:
            currency_confidence = Decimal("0.95")
        else:
            currency_confidence = Decimal("0.35")

    merchant_confidence = Decimal("0.85") if merchant else Decimal("0.20")
    description_confidence = Decimal("0.85") if description else Decimal("0.20")
    participants_confidence = Decimal("0.00")
    split_confidence = Decimal("0.00")
    if transaction_type == "shared_expense":
        participants_confidence = Decimal("0.82") if participants else Decimal("0.25")
        if split_type == "equal":
            split_confidence = Decimal("0.90")
        elif split_type == "unspecified":
            split_confidence = Decimal("0.45")
        else:
            split_confidence = Decimal("0.25")

    return {
        "amount": amount_confidence,
        "currency": currency_confidence,
        "merchant": merchant_confidence,
        "description": description_confidence,
        "transaction_date": Decimal("0.00"),
        "participants": participants_confidence,
        "split_type": split_confidence,
    }


def _overall_confidence(
    field_confidence: dict[str, Decimal],
    missing_fields: list[str],
    amount_candidates: list[AmountCandidate],
) -> Decimal:
    scored_fields = ["amount", "currency", "merchant", "description"]
    if field_confidence["participants"] > 0:
        scored_fields.append("participants")
    if field_confidence["split_type"] > 0:
        scored_fields.append("split_type")
    total = sum(field_confidence[field] for field in scored_fields)
    score = total / Decimal(len(scored_fields))
    score -= Decimal("0.06") * Decimal(len(missing_fields))
    if len(amount_candidates) > 1:
        score -= Decimal("0.10")
    return max(Decimal("0.10"), min(Decimal("0.98"), score)).quantize(Decimal("0.01"))


def _confidence_reasons(
    *,
    missing_fields: list[str],
    amount_candidates: list[AmountCandidate],
    primary_amount: AmountCandidate | None,
    transaction_type: str,
) -> list[str]:
    reasons = []
    if primary_amount and primary_amount.currency:
        reasons.append("amount and currency were found in raw input")
    if primary_amount and primary_amount.currency is None:
        reasons.append("amount was found but currency is missing")
    if len(amount_candidates) > 1:
        reasons.append("multiple amount-like values require review")
    if transaction_type == "shared_expense":
        reasons.append("shared expense details require participant and split confirmation")
    for field in missing_fields:
        reasons.append(f"{field} requires confirmation")
    return reasons or ["parser found limited deterministic evidence"]


def _build_field_evidence(
    *,
    raw_input_reference: str | None,
    primary_amount: AmountCandidate | None,
    secondary_amounts: list[AmountCandidate],
    paid_back: dict[str, Any] | None,
    paid_by: str | None,
    paid_by_evidence: tuple[str, int, int] | None,
    merchant: str | None,
    merchant_evidence: tuple[str, int, int] | None,
    description: str | None,
    description_evidence: tuple[str, int, int] | None,
    participants: list[str] | None,
    participant_evidence: tuple[str, int, int] | None,
    split_type: str | None,
    split_evidence: tuple[str, int, int] | None,
    field_confidence: dict[str, Decimal],
) -> list[dict[str, Any]]:
    evidence = []
    if primary_amount:
        evidence.append(
            _evidence(
                "amount",
                primary_amount.amount_text,
                field_confidence["amount"],
                primary_amount.token,
                primary_amount.start,
                primary_amount.end,
                raw_input_reference,
                "primary amount candidate",
            )
        )
        if primary_amount.currency:
            evidence.append(
                _evidence(
                    "currency",
                    primary_amount.currency,
                    field_confidence["currency"],
                    primary_amount.currency_token or primary_amount.token,
                    primary_amount.start,
                    primary_amount.end,
                    raw_input_reference,
                    "currency token attached to primary amount",
                )
            )
    if secondary_amounts:
        evidence.append(
            _evidence(
                "secondary_amounts",
                ", ".join(candidate.amount_text for candidate in secondary_amounts),
                Decimal("0.60"),
                ", ".join(candidate.token for candidate in secondary_amounts),
                secondary_amounts[0].start,
                secondary_amounts[-1].end,
                raw_input_reference,
                "additional amount-like values found",
            )
        )
    if paid_by and paid_by_evidence:
        evidence.append(
            _evidence(
                "paid_by",
                paid_by,
                Decimal("0.80"),
                paid_by_evidence[0],
                paid_by_evidence[1],
                paid_by_evidence[2],
                raw_input_reference,
                "payer phrase",
            )
        )
    if merchant and merchant_evidence:
        evidence.append(
            _evidence(
                "merchant",
                merchant,
                field_confidence["merchant"],
                merchant_evidence[0],
                merchant_evidence[1],
                merchant_evidence[2],
                raw_input_reference,
                "merchant phrase",
            )
        )
    if description and description_evidence:
        evidence.append(
            _evidence(
                "description",
                description,
                field_confidence["description"],
                description_evidence[0],
                description_evidence[1],
                description_evidence[2],
                raw_input_reference,
                "description phrase",
            )
        )
    if participants and participant_evidence:
        evidence.append(
            _evidence(
                "participants",
                ",".join(participants),
                field_confidence["participants"],
                participant_evidence[0],
                participant_evidence[1],
                participant_evidence[2],
                raw_input_reference,
                "participant phrase",
            )
        )
    if split_type and split_evidence:
        evidence.append(
            _evidence(
                "split_type",
                split_type,
                field_confidence["split_type"],
                split_evidence[0],
                split_evidence[1],
                split_evidence[2],
                raw_input_reference,
                "split phrase",
            )
        )
    if paid_back:
        evidence.append(
            _evidence(
                "related_payments",
                paid_back["person"],
                Decimal("0.70"),
                paid_back["substring"],
                paid_back["start"],
                paid_back["end"],
                raw_input_reference,
                "paid-me-back phrase",
            )
        )
    return evidence


def _evidence(
    field_name: str,
    proposed_value: str,
    confidence: Decimal,
    substring: str,
    start: int,
    end: int,
    reference: str | None,
    note: str,
) -> dict[str, Any]:
    return {
        "field_name": field_name,
        "proposed_value": proposed_value,
        "confidence": confidence,
        "evidence_source_type": "raw_input",
        "evidence_reference": reference,
        "substring": substring,
        "start": start,
        "end": end,
        "notes": note,
    }


def _foreign_amount(
    primary_amount: AmountCandidate | None,
    secondary_amounts: list[AmountCandidate],
) -> AmountCandidate | None:
    if not primary_amount or primary_amount.currency != "SGD":
        return None
    for candidate in secondary_amounts:
        if candidate.currency and candidate.currency != "SGD":
            return candidate
    return None


def _parse_people(value: str) -> list[str]:
    names = []
    for raw_name in re.split(r",|&|\band\b", value, flags=re.IGNORECASE):
        name = _clean_word(raw_name)
        if name and name.lower() not in _PARTICIPANT_STOP_WORDS:
            names.append(name)
    return names


def _looks_like_participants(value: str, participants: list[str] | None) -> bool:
    if not participants:
        return False
    parsed = _parse_people(value)
    return bool(parsed) and set(parsed).issubset(set(participants))


def _looks_like_people(values: list[str]) -> bool:
    return bool(values) and all(value[:1].isupper() for value in values)


def _clean_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-].*$", "", value.strip())


def _find_span_start(raw_input: str, substring: str) -> int:
    start = raw_input.find(substring)
    return start if start >= 0 else 0


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")
