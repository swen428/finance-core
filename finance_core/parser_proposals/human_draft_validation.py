"""Deterministic Python validation for D1 whole-card draft snapshots.

This module is deliberately pure: it owns no database transaction, provider,
clock, publication, confirmation, or final-fact authority.  The draft
repository persists its typed result or its stable typed refusal.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from finance_core.money import MoneyValidationError, canonical_decimal_str, normalize_currency
from finance_core.parser_proposals.content_hash import canonicalize_proposal_money
from finance_core.parser_proposals.human_drafts import HumanReasonContributor

_FIELDS = ("amount", "currency", "transaction_date", "merchant", "description", "category")
_FIELD_SET = frozenset(_FIELDS)
_RECEIPT_SOURCE_TYPES = frozenset(
    {"telegram_image", "local_image", "receipt_local_ocr_text", "receipt"}
)
_TEXT_SOURCE_TYPES = frozenset({"text", "telegram_text", "telegram_raw_text"})
_ALLOWED_INTENTS = frozenset({"personal_expense", "personal_expense_log"})
_AMOUNT_SHAPE = re.compile(r"[0-9]+(?:\.[0-9]+)?", flags=re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_TEXT_BYTE_LIMITS = {"merchant": 1024, "description": 1024, "category": 128}
_ORIGIN_KINDS = frozenset({"ocr", "deterministic", "ai_observation", "model_only", "unknown"})


class HumanDraftValidationError(ValueError):
    """One deterministic draft-validation rule refused the whole field batch."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ValidatedHumanDraft:
    canonical_payload: dict[str, object]
    changed_fields: tuple[str, ...]
    completeness: str
    reason_contributors: tuple[HumanReasonContributor, ...]
    unresolved_flags: tuple[str, ...]
    explicit_clears: dict[str, tuple[object, object]]


@dataclass(frozen=True)
class ReasonPolicy:
    affected_fields: tuple[str, ...]
    flags: tuple[str, ...]
    resolution_policy: str


RECEIPT_REASON_POLICY: dict[str, ReasonPolicy] = {
    "total_not_found": ReasonPolicy(("amount",), ("missing_amount",), "explicit_valid_amount"),
    "conflicting_total_candidates": ReasonPolicy(
        ("amount",), ("ambiguous_amount", "source_conflict"), "explicit_material_amount_pair"
    ),
    "total_amount_invalid": ReasonPolicy(
        ("amount",), ("total_amount_invalid",), "explicit_material_amount_pair"
    ),
    "ambiguous_currency_symbol": ReasonPolicy(
        ("currency",), ("ambiguous_currency", "source_conflict"), "explicit_supported_currency"
    ),
    "currency_not_determined": ReasonPolicy(
        ("currency",), ("missing_currency",), "explicit_supported_currency"
    ),
    "unsupported_currency_for_amount": ReasonPolicy(
        ("amount", "currency"),
        ("ambiguous_currency", "source_conflict"),
        "explicit_target_money_pair",
    ),
    "ambiguous_transaction_date": ReasonPolicy(
        ("transaction_date",), ("ambiguous_date", "source_conflict"), "explicit_valid_date"
    ),
    "conflicting_date_candidates": ReasonPolicy(
        ("transaction_date",), ("ambiguous_date", "source_conflict"), "explicit_valid_date"
    ),
    "transaction_date_not_found": ReasonPolicy(
        ("transaction_date",), ("missing_date",), "explicit_valid_date"
    ),
    "merchant_not_determined": ReasonPolicy(
        ("merchant",), ("missing_merchant_or_description",), "explicit_nonempty_merchant"
    ),
    "ocr_no_text": ReasonPolicy((), ("ocr_no_text",), "never_discharge"),
    "ocr_unsupported_input": ReasonPolicy((), ("ocr_unsupported_input",), "never_discharge"),
    "ocr_engine_failed": ReasonPolicy((), ("ocr_engine_failed",), "never_discharge"),
    "ocr_resource_rejected": ReasonPolicy((), ("ocr_resource_rejected",), "never_discharge"),
}

_GENERIC_REASON_POLICY: dict[str, ReasonPolicy] = {
    "missing_amount": ReasonPolicy(("amount",), ("missing_amount",), "explicit_valid_amount"),
    "ambiguous_amount": ReasonPolicy(
        ("amount",), ("ambiguous_amount", "source_conflict"), "explicit_material_amount_pair"
    ),
    "amount_conflict_refs": ReasonPolicy(
        ("amount",), ("ambiguous_amount", "source_conflict"), "explicit_material_amount_pair"
    ),
    "conflicting_text_candidates": ReasonPolicy(
        ("amount",), ("ambiguous_amount", "source_conflict"), "explicit_material_amount_pair"
    ),
    "missing_currency": ReasonPolicy(
        ("currency",), ("missing_currency",), "explicit_supported_currency"
    ),
    "ambiguous_currency": ReasonPolicy(
        ("currency",), ("ambiguous_currency", "source_conflict"), "explicit_supported_currency"
    ),
    "currency_conflict_refs": ReasonPolicy(
        ("currency",), ("ambiguous_currency", "source_conflict"), "explicit_supported_currency"
    ),
    "missing_date": ReasonPolicy(("transaction_date",), ("missing_date",), "explicit_valid_date"),
    "ambiguous_date": ReasonPolicy(
        ("transaction_date",), ("ambiguous_date", "source_conflict"), "explicit_valid_date"
    ),
    "date_conflict_refs": ReasonPolicy(
        ("transaction_date",), ("ambiguous_date", "source_conflict"), "explicit_valid_date"
    ),
    "ambiguous_merchant": ReasonPolicy(
        ("merchant",), ("ambiguous_merchant", "source_conflict"), "explicit_nonempty_merchant"
    ),
    "merchant_conflict_refs": ReasonPolicy(
        ("merchant",), ("ambiguous_merchant", "source_conflict"), "explicit_nonempty_merchant"
    ),
    "missing_merchant_or_description": ReasonPolicy(
        ("merchant", "description"),
        ("missing_merchant_or_description",),
        "explicit_text_merchant_or_description",
    ),
    "unsupported_intent": ReasonPolicy((), ("unsupported_intent",), "never_discharge"),
}


def _contributor_id(
    *, reason_code: str, origin_kind: str, evidence_public_id: str | None, flags: tuple[str, ...]
) -> str:
    material = "\x00".join((reason_code, origin_kind, evidence_public_id or "", *sorted(flags)))
    return "d1reason_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _new_contributor(
    *,
    reason_code: str,
    origin_kind: str,
    source_evidence_public_id: str | None,
    source_evidence_hash: str | None,
    policy: ReasonPolicy,
) -> HumanReasonContributor:
    return HumanReasonContributor(
        contributor_id=_contributor_id(
            reason_code=reason_code,
            origin_kind=origin_kind,
            evidence_public_id=source_evidence_public_id,
            flags=policy.flags,
        ),
        reason_code=reason_code,
        origin_kind=origin_kind,
        source_evidence_public_id=source_evidence_public_id,
        source_evidence_hash=source_evidence_hash,
        flags=policy.flags,
        affected_fields=policy.affected_fields,
        resolution_policy=policy.resolution_policy,
        resolved_by_operation_id=None,
        resolved_fields=(),
        resolution_before_after={},
    )


def initial_reason_contributors(
    current_payload: dict[str, object],
    *,
    source_type: str,
    verified_ocr_evidence: Mapping[str, object] | None = None,
    verified_ai_observations: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[HumanReasonContributor, ...]:
    """Build the initial exhaustive source restrictions without guessing provenance."""
    receipt = _is_receipt(source_type)
    contributors: list[HumanReasonContributor] = []
    seen_reasons: set[str] = set()
    covered_flags: set[str] = set()

    raw_flags = current_payload.get("ambiguity_flags", [])
    if not isinstance(raw_flags, list):
        raw_flags = ["source_ambiguity_invalid"]

    ocr_public_id: str | None = None
    ocr_hash: str | None = None
    ocr_status: object = None
    payload_ocr = current_payload.get("ocr_evidence")
    if receipt and isinstance(payload_ocr, Mapping) and isinstance(verified_ocr_evidence, Mapping):
        public_id = verified_ocr_evidence.get("extraction_public_id")
        normalized_hash = verified_ocr_evidence.get("normalized_result_hash")
        if (
            isinstance(public_id, str)
            and public_id
            and isinstance(normalized_hash, str)
            and _HASH.fullmatch(normalized_hash) is not None
            and payload_ocr.get("extraction_public_id") == public_id
            and payload_ocr.get("normalized_result_hash") == normalized_hash
            and payload_ocr.get("extraction_status")
            == verified_ocr_evidence.get("extraction_status")
        ):
            ocr_public_id = public_id
            ocr_hash = normalized_hash
            ocr_status = verified_ocr_evidence.get("extraction_status")
    elif (
        receipt
        and payload_ocr is None
        and isinstance(verified_ocr_evidence, Mapping)
        and verified_ai_observations is not None
    ):
        public_id = verified_ocr_evidence.get("extraction_public_id")
        normalized_hash = verified_ocr_evidence.get("normalized_result_hash")
        if (
            isinstance(public_id, str)
            and public_id
            and isinstance(normalized_hash, str)
            and _HASH.fullmatch(normalized_hash) is not None
        ):
            ocr_public_id = public_id
            ocr_hash = normalized_hash
            ocr_status = verified_ocr_evidence.get("extraction_status")

    for raw_flag_value in raw_flags:
        raw_flag = (
            raw_flag_value
            if isinstance(raw_flag_value, str) and raw_flag_value
            else "source_ambiguity_invalid"
        )
        policy = RECEIPT_REASON_POLICY.get(raw_flag) if receipt else None
        if policy is not None:
            resolvable_evidence = (
                ocr_public_id is not None
                and ocr_hash is not None
                and (policy.resolution_policy == "never_discharge" or ocr_status == "succeeded")
            )
            if resolvable_evidence:
                contributor = _new_contributor(
                    reason_code=raw_flag,
                    origin_kind="ocr",
                    source_evidence_public_id=ocr_public_id,
                    source_evidence_hash=ocr_hash,
                    policy=policy,
                )
            else:
                unknown_policy = ReasonPolicy(
                    policy.affected_fields,
                    (raw_flag,),
                    "never_discharge",
                )
                contributor = _new_contributor(
                    reason_code=f"unverified:{raw_flag}",
                    origin_kind="unknown",
                    source_evidence_public_id=None,
                    source_evidence_hash=None,
                    policy=unknown_policy,
                )
            contributors.append(contributor)
            seen_reasons.add(raw_flag)
            covered_flags.update(contributor.flags)
            continue

        observation = (
            verified_ai_observations.get(raw_flag)
            if isinstance(verified_ai_observations, Mapping)
            else None
        )
        ai_policy = _GENERIC_REASON_POLICY.get(raw_flag)
        if (
            ai_policy is not None
            and observation is not None
            and isinstance(observation, tuple)
            and len(observation) == 2
            and isinstance(observation[0], str)
            and observation[0]
            and isinstance(observation[1], str)
            and _HASH.fullmatch(observation[1]) is not None
        ):
            contributor = _new_contributor(
                reason_code=raw_flag,
                origin_kind="ai_observation",
                source_evidence_public_id=observation[0],
                source_evidence_hash=observation[1],
                policy=ai_policy,
            )
            contributors.append(contributor)
            seen_reasons.add(raw_flag)
            covered_flags.update(contributor.flags)

    derived: list[str] = []
    if current_payload.get("amount") in (None, "") and "missing_amount" not in covered_flags:
        derived.append("missing_amount")
    if current_payload.get("currency") in (None, "") and "missing_currency" not in covered_flags:
        derived.append("missing_currency")
    if (
        current_payload.get("transaction_date", current_payload.get("date")) in (None, "")
        and "missing_date" not in covered_flags
    ):
        derived.append("missing_date")
    missing_required_text = current_payload.get("merchant") in (None, "") and (
        receipt or current_payload.get("description") in (None, "")
    )
    if missing_required_text and "missing_merchant_or_description" not in covered_flags:
        derived.append("missing_merchant_or_description")
    if current_payload.get("intent") not in _ALLOWED_INTENTS:
        derived.append("unsupported_intent")
    for reason_code in derived:
        if reason_code in seen_reasons:
            continue
        contributors.append(
            _new_contributor(
                reason_code=reason_code,
                origin_kind="deterministic",
                source_evidence_public_id=None,
                source_evidence_hash=None,
                policy=_GENERIC_REASON_POLICY[reason_code],
            )
        )
        seen_reasons.add(reason_code)
        covered_flags.update(_GENERIC_REASON_POLICY[reason_code].flags)

    for raw_flag_value in raw_flags:
        raw_flag = (
            raw_flag_value
            if isinstance(raw_flag_value, str) and raw_flag_value
            else "source_ambiguity_invalid"
        )
        if raw_flag in seen_reasons or raw_flag in covered_flags:
            continue
        policy = ReasonPolicy((), (raw_flag,), "never_discharge")
        contributors.append(
            _new_contributor(
                reason_code=f"unverified:{raw_flag}",
                origin_kind="unknown",
                source_evidence_public_id=None,
                source_evidence_hash=None,
                policy=policy,
            )
        )
        covered_flags.add(raw_flag)
    return tuple(sorted(contributors, key=lambda item: item.contributor_id))


def _refuse(code: str) -> NoReturn:
    raise HumanDraftValidationError(code)


def _is_receipt(source_type: str) -> bool:
    if source_type in _RECEIPT_SOURCE_TYPES:
        return True
    if source_type in _TEXT_SOURCE_TYPES:
        return False
    _refuse("D1_SOURCE_TYPE_UNSUPPORTED")
    raise AssertionError("unreachable")


def _amount_parts(raw: str) -> tuple[str, str | None]:
    if _AMOUNT_SHAPE.fullmatch(raw) is None:
        _refuse("D1_AMOUNT_SYNTAX")
    integral, separator, fraction = raw.partition(".")
    if len(integral) > 18 or (separator and len(fraction) > 6) or len(raw.encode("utf-8")) > 25:
        _refuse("D1_AMOUNT_LIMIT")
    return integral, fraction if separator else None


def _bounded_positive_decimal(raw_value: str) -> Decimal:
    raw = raw_value.strip(" \t")
    _amount_parts(raw)
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        _refuse("D1_AMOUNT_SYNTAX")
    if not amount.is_finite():
        _refuse("D1_AMOUNT_SYNTAX")
    if amount <= 0:
        _refuse("D1_AMOUNT_NONPOSITIVE")
    return amount


def _canonical_amount(raw_value: str, currency: str | None) -> str:
    amount = _bounded_positive_decimal(raw_value)
    if currency is None:
        canonical = canonical_decimal_str(amount)
    else:
        try:
            canonical = canonicalize_proposal_money(raw_value.strip(" \t"), currency)
        except MoneyValidationError:
            _refuse("D1_MONEY_INVALID")
    _amount_parts(canonical)
    return canonical


def _canonical_currency(raw_value: str) -> str | None:
    raw = raw_value.strip(" \t")
    if not raw:
        return None
    try:
        return normalize_currency(raw)
    except MoneyValidationError:
        _refuse("D1_CURRENCY_INVALID")
    raise AssertionError("unreachable")


def _canonical_date(raw_value: str) -> str | None:
    value = raw_value.strip(" \t")
    if not value:
        return None
    if len(value) != 10 or value[4:5] != "-" or value[7:8] != "-":
        _refuse("D1_DATE_INVALID")
    try:
        if date.fromisoformat(value).isoformat() != value:
            _refuse("D1_DATE_INVALID")
    except ValueError:
        _refuse("D1_DATE_INVALID")
    return value


def _canonical_text(raw_value: str, field: str, *, receipt: bool) -> object:
    value = raw_value.strip()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _refuse("D1_TEXT_INVALID")
    if any(character in value for character in ("\u2028", "\u2029")) or any(
        ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value
    ):
        _refuse("D1_TEXT_INVALID")
    if len(encoded) > _TEXT_BYTE_LIMITS[field]:
        _refuse("D1_TEXT_LIMIT")
    if receipt and field in {"description", "category"} and not value:
        return None
    return value


def _try_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        amount = Decimal(value.strip(" \t"))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _materially_changed(field: str, before: object, after: object) -> bool:
    if field == "amount":
        before_decimal = _try_decimal(before)
        after_decimal = _try_decimal(after)
        if before_decimal is not None and after_decimal is not None:
            return before_decimal != after_decimal
    if field == "currency" and isinstance(before, str):
        try:
            before = normalize_currency(before)
        except MoneyValidationError:
            pass
    if field in {"merchant", "description", "category"} and isinstance(before, str):
        before = before.strip()
    return before != after


def _candidate_money_is_valid(payload: dict[str, object]) -> bool:
    amount = payload.get("amount")
    currency = payload.get("currency")
    if not isinstance(amount, str) or not amount.strip(" \t"):
        return False
    if not isinstance(currency, str) or not currency.strip(" \t"):
        return False
    try:
        normalized_currency = normalize_currency(currency)
        _canonical_amount(amount, normalized_currency)
    except (HumanDraftValidationError, MoneyValidationError):
        return False
    return True


def _canonical_snapshot(payload: dict[str, object], *, receipt: bool) -> dict[str, object]:
    """Canonicalize all editable fields, including inherited snapshot material."""
    candidate = dict(payload)
    raw_currency = candidate.get("currency")
    if raw_currency in (None, ""):
        normalized_currency = None
        candidate["currency"] = None
    elif not isinstance(raw_currency, str):
        _refuse("D1_CURRENCY_INVALID")
    else:
        normalized_currency = _canonical_currency(raw_currency)
        candidate["currency"] = normalized_currency

    raw_amount = candidate.get("amount")
    if raw_amount in (None, ""):
        candidate["amount"] = None
    elif not isinstance(raw_amount, str):
        _refuse("D1_AMOUNT_SYNTAX")
    else:
        candidate["amount"] = _canonical_amount(raw_amount.strip(" \t"), normalized_currency)

    raw_date = candidate.get("transaction_date", candidate.get("date"))
    candidate.pop("date", None)
    if raw_date in (None, ""):
        candidate["transaction_date"] = None
    elif not isinstance(raw_date, str):
        _refuse("D1_DATE_INVALID")
    else:
        candidate["transaction_date"] = _canonical_date(raw_date)

    for field in ("merchant", "description", "category"):
        raw_text = candidate.get(field)
        if raw_text is None:
            candidate[field] = None
        elif not isinstance(raw_text, str):
            _refuse("D1_TEXT_INVALID")
        else:
            candidate[field] = _canonical_text(raw_text, field, receipt=receipt)
    return candidate


def _candidate_date_is_valid(payload: dict[str, object]) -> bool:
    value = payload.get("transaction_date", payload.get("date"))
    if not isinstance(value, str):
        return False
    try:
        return _canonical_date(value) is not None
    except HumanDraftValidationError:
        return False


def canonicalize_human_draft_before(
    current_payload: dict[str, object], *, source_type: str
) -> dict[str, object]:
    """Return the canonical-before snapshot used for material-change decisions."""
    try:
        return _canonical_snapshot(dict(current_payload), receipt=_is_receipt(source_type))
    except HumanDraftValidationError:
        return dict(current_payload)


def _policy_for(contributor: HumanReasonContributor) -> ReasonPolicy | None:
    if contributor.reason_code in RECEIPT_REASON_POLICY:
        return RECEIPT_REASON_POLICY[contributor.reason_code]
    return _GENERIC_REASON_POLICY.get(contributor.reason_code)


def _validate_contributor(
    contributor: HumanReasonContributor, *, receipt: bool
) -> ReasonPolicy | None:
    if contributor.origin_kind not in _ORIGIN_KINDS:
        _refuse("D1_REASON_UNVERIFIABLE")
    if not isinstance(contributor.contributor_id, str) or not contributor.contributor_id:
        _refuse("D1_REASON_UNVERIFIABLE")
    if len(set(contributor.flags)) != len(contributor.flags) or any(
        not isinstance(flag, str) or not flag for flag in contributor.flags
    ):
        _refuse("D1_REASON_UNVERIFIABLE")
    if len(set(contributor.affected_fields)) != len(contributor.affected_fields) or any(
        field not in _FIELD_SET for field in contributor.affected_fields
    ):
        _refuse("D1_REASON_UNVERIFIABLE")
    evidence_pair = (
        contributor.source_evidence_public_id is not None,
        contributor.source_evidence_hash is not None,
    )
    if evidence_pair[0] != evidence_pair[1]:
        _refuse("D1_REASON_UNVERIFIABLE")
    if contributor.origin_kind in {"ocr", "ai_observation"}:
        if (
            not isinstance(contributor.source_evidence_public_id, str)
            or not contributor.source_evidence_public_id
            or not isinstance(contributor.source_evidence_hash, str)
            or _HASH.fullmatch(contributor.source_evidence_hash) is None
        ):
            _refuse("D1_REASON_UNVERIFIABLE")
    if contributor.origin_kind == "ocr" and not receipt:
        _refuse("D1_REASON_UNVERIFIABLE")
    if contributor.resolved_by_operation_id is None:
        if contributor.resolved_fields or contributor.resolution_before_after:
            _refuse("D1_REASON_UNVERIFIABLE")
    elif (
        not contributor.resolved_by_operation_id
        or not contributor.resolved_fields
        or set(contributor.resolved_fields) != set(contributor.resolution_before_after)
    ):
        _refuse("D1_REASON_UNVERIFIABLE")
    if contributor.origin_kind in {"model_only", "unknown"}:
        if (
            contributor.resolution_policy != "never_discharge"
            or contributor.resolved_by_operation_id
        ):
            _refuse("D1_REASON_UNVERIFIABLE")
        return None

    policy = _policy_for(contributor)
    if policy is None:
        _refuse("D1_REASON_UNVERIFIABLE")
    if contributor.origin_kind == "ocr" and contributor.reason_code not in RECEIPT_REASON_POLICY:
        _refuse("D1_REASON_UNVERIFIABLE")
    if (
        tuple(sorted(contributor.flags)) != tuple(sorted(policy.flags))
        or tuple(sorted(contributor.affected_fields)) != tuple(sorted(policy.affected_fields))
        or contributor.resolution_policy != policy.resolution_policy
    ):
        _refuse("D1_REASON_UNVERIFIABLE")
    return policy


def _resolution_fields(
    policy: ReasonPolicy,
    *,
    receipt: bool,
    supplied: dict[str, str],
    changed_fields: tuple[str, ...],
    candidate: dict[str, object],
) -> tuple[str, ...]:
    changed = set(changed_fields)
    explicit = set(supplied)
    materially_affected = changed.intersection(policy.affected_fields)
    if not materially_affected:
        return ()
    money_valid = _candidate_money_is_valid(candidate)
    if policy.resolution_policy == "explicit_valid_amount":
        amount = candidate.get("amount")
        amount_decimal = _try_decimal(amount)
        if "amount" not in explicit or amount_decimal is None or amount_decimal <= 0:
            return ()
    elif policy.resolution_policy == "explicit_material_amount_pair":
        if "amount" not in explicit or candidate.get("amount") is None or not money_valid:
            return ()
    elif policy.resolution_policy == "explicit_supported_currency":
        if "currency" not in explicit or candidate.get("currency") is None:
            return ()
    elif policy.resolution_policy == "explicit_target_money_pair":
        if not {"amount", "currency"} <= explicit or not money_valid:
            return ()
    elif policy.resolution_policy == "explicit_valid_date":
        if "transaction_date" not in explicit or not _candidate_date_is_valid(candidate):
            return ()
    elif policy.resolution_policy == "explicit_nonempty_merchant":
        if "merchant" not in explicit or not candidate.get("merchant"):
            return ()
    elif policy.resolution_policy == "explicit_text_merchant_or_description":
        eligible = {"merchant"} if receipt else {"merchant", "description"}
        if not any(
            field in explicit and field in changed and candidate.get(field) for field in eligible
        ):
            return ()
    elif policy.resolution_policy == "never_discharge":
        return ()
    else:
        _refuse("D1_REASON_UNVERIFIABLE")
    return tuple(field for field in _FIELDS if field in materially_affected and field in explicit)


def validate_human_draft(
    current_payload: dict[str, object],
    field_values: dict[str, str],
    *,
    source_type: str,
    reason_contributors: tuple[HumanReasonContributor, ...],
    operation_public_id: str,
) -> ValidatedHumanDraft:
    """Canonicalize one explicit field batch and validate its complete candidate."""
    if not isinstance(current_payload, dict) or not isinstance(field_values, dict):
        _refuse("D1_VALIDATION_MATERIAL_INVALID")
    if (
        not isinstance(source_type, str)
        or not isinstance(operation_public_id, str)
        or not operation_public_id
    ):
        _refuse("D1_VALIDATION_MATERIAL_INVALID")
    receipt = _is_receipt(source_type)
    if any(not isinstance(key, str) or key not in _FIELD_SET for key in field_values):
        _refuse("D1_FIELD_UNSUPPORTED")
    if any(not isinstance(value, str) for value in field_values.values()):
        _refuse("D1_FIELD_VALUE_INVALID")
    if not isinstance(reason_contributors, tuple):
        _refuse("D1_REASON_UNVERIFIABLE")

    supplied = dict(field_values)
    raw_candidate = dict(current_payload)
    raw_candidate.update(supplied)
    candidate = _canonical_snapshot(raw_candidate, receipt=receipt)
    canonical_before = canonicalize_human_draft_before(current_payload, source_type=source_type)

    changed_fields = tuple(
        field
        for field in _FIELDS
        if field in supplied
        and _materially_changed(field, canonical_before.get(field), candidate.get(field))
    )
    explicit_clears: dict[str, tuple[object, object]] = {}
    for field in ("merchant", "description", "category"):
        if field not in supplied or supplied[field].strip():
            continue
        before = canonical_before.get(field)
        after = candidate.get(field)
        if _materially_changed(field, before, after):
            explicit_clears[field] = (before, after)

    after_contributors: list[HumanReasonContributor] = []
    unresolved_flags: set[str] = set()
    for contributor in reason_contributors:
        if not isinstance(contributor, HumanReasonContributor):
            _refuse("D1_REASON_UNVERIFIABLE")
        policy = _validate_contributor(contributor, receipt=receipt)
        resolved = contributor.resolved_by_operation_id is not None
        after = contributor
        if not resolved and policy is not None:
            resolved_fields = _resolution_fields(
                policy,
                receipt=receipt,
                supplied=supplied,
                changed_fields=changed_fields,
                candidate=candidate,
            )
            if resolved_fields:
                before_after = {
                    field: (canonical_before.get(field), candidate.get(field))
                    for field in resolved_fields
                }
                after = replace(
                    contributor,
                    resolved_by_operation_id=operation_public_id,
                    resolved_fields=resolved_fields,
                    resolution_before_after=before_after,
                )
                resolved = True
        if not resolved:
            unresolved_flags.update(contributor.flags)
        after_contributors.append(after)

    intent_valid = candidate.get("intent") in _ALLOWED_INTENTS
    money_valid = _candidate_money_is_valid(candidate)
    date_valid = _candidate_date_is_valid(candidate)
    merchant = candidate.get("merchant")
    description = candidate.get("description")
    source_required_fields_valid = bool(merchant) if receipt else bool(merchant or description)
    publishable = (
        intent_valid
        and money_valid
        and date_valid
        and source_required_fields_valid
        and not unresolved_flags
    )
    return ValidatedHumanDraft(
        canonical_payload=candidate,
        changed_fields=changed_fields,
        completeness="publishable" if publishable else "incomplete",
        reason_contributors=tuple(after_contributors),
        unresolved_flags=tuple(sorted(unresolved_flags)),
        explicit_clears=explicit_clears,
    )


__all__ = [
    "HumanDraftValidationError",
    "RECEIPT_REASON_POLICY",
    "ReasonPolicy",
    "ValidatedHumanDraft",
    "canonicalize_human_draft_before",
    "initial_reason_contributors",
    "validate_human_draft",
]
