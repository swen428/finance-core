"""Explicit additional receipt fields for the selected payment and opening."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from .contracts import LifecycleError
from .daily_v1.contracts import (
    AccountDefinitionRefV1,
    BundleFactRefV1,
    OpeningRefV1,
    sha,
    text,
    validate_as_of,
)
from .daily_v1.movement_contracts import _day, _later
from .receipt_review_preparation_v1 import VerifiedReceiptPaymentReplyV1

if TYPE_CHECKING:
    from .receipt_review_preparation_v1 import ReceiptReviewPreparationV1

_CATEGORY = re.compile(r"分类[：:] ?([^\r\n]+)")
_DATE = re.compile(r"扣款日期[：:] ?([0-9]{4}-[0-9]{2}-[0-9]{2})")
_OPENING = {"期初余额：已包含": "included", "期初余额：未包含": "excluded"}


@dataclass(frozen=True)
class ReceiptReviewFieldsTargetV1:
    source_id: str
    source_evidence_id: str
    source_sha256: str
    draft_sha256: str
    debit_response_id: str
    debit_response_sha256: str
    debit_update_id: int
    account_ref: AccountDefinitionRefV1
    bundle_ref: BundleFactRefV1
    opening_ref: OpeningRefV1 | None

    def __post_init__(self):
        for value in (self.source_id, self.source_evidence_id, self.debit_response_id):
            text(value, "receipt review origin", 512)
        for value in (self.source_sha256, self.draft_sha256, self.debit_response_sha256):
            sha(value)
        if (
            type(self.debit_update_id) is not int
            or not 0 <= self.debit_update_id < 2**63
            or type(self.account_ref) is not AccountDefinitionRefV1
            or type(self.bundle_ref) is not BundleFactRefV1
            or self.bundle_ref.record_id != self.account_ref.account_id
            or self.opening_ref is not None
            and (
                type(self.opening_ref) is not OpeningRefV1
                or self.opening_ref.account_id != self.account_ref.account_id
            )
        ):
            raise LifecycleError("Exact receipt review account and debit target required")


@dataclass(frozen=True)
class VerifiedReceiptReviewFieldsReplyV1:
    """Original delivered question and accepted reply from the installed evidence port.

    The port authenticates the original purpose and reconstructs the target
    from the delivered question bound to the reply. The call receives only a
    receipt lookup identity. Current Core values do not supply the target.
    """

    target: ReceiptReviewFieldsTargetV1
    reply: VerifiedReceiptPaymentReplyV1
    principal_id: str
    client_id: str


def _copy_review_fields_target(target):
    target = replace(target)
    return replace(
        target,
        account_ref=replace(target.account_ref),
        bundle_ref=replace(target.bundle_ref),
        opening_ref=replace(target.opening_ref) if target.opening_ref is not None else None,
    )


@dataclass(frozen=True)
class ReceiptReviewFieldsV1:
    preparation: ReceiptReviewPreparationV1
    evidence: VerifiedReceiptReviewFieldsReplyV1
    category: str
    actual_debit_date: str | None
    opening_treatment: Literal["no_baseline", "included", "excluded"] | None
    opening_basis: Literal["no_active_opening", "authenticated_reply", "strictly_later"] | None
    missing_movement_fields: tuple[str, ...]


def receipt_review_fields_target_v1(preparation: ReceiptReviewPreparationV1):
    """Identify precisely the retained draft and payment being completed."""
    payment = preparation.payment
    return ReceiptReviewFieldsTargetV1(
        preparation.draft.source_id,
        preparation.source.evidence_id,
        payment.source_sha256,
        payment.draft_sha256,
        payment.debit.response_id,
        payment.debit.response_sha256,
        payment.debit.reply_update_id,
        payment.account.account_ref,
        payment.account.bundle_ref,
        preparation.active_opening_ref,
    )


def parse_receipt_review_fields_v1(raw: str, reply_timestamp: int, *, has_opening: bool):
    """Read an exact category and optional debit date and inclusion answers."""
    lines = raw.strip().splitlines()
    if not 1 <= len(lines) <= 3 or (category := _CATEGORY.fullmatch(lines[0])) is None:
        raise LifecycleError("Explicit receipt category line required")
    category_value = text(category[1], "receipt category")
    local_day = coverage = None
    for line in lines[1:]:
        date_match = _DATE.fullmatch(line)
        if date_match is not None and local_day is None:
            local_day = _day(date_match[1])
        elif has_opening and line in _OPENING and coverage is None:
            coverage = _OPENING[line]
        else:
            raise LifecycleError("Exact actual debit date or opening inclusion answer required")
    if local_day is not None:
        try:
            observed_day = datetime.fromtimestamp(reply_timestamp, ZoneInfo("Asia/Singapore"))
        except (OverflowError, OSError, ValueError):
            raise LifecycleError("Invalid receipt reply timestamp") from None
        if local_day > observed_day.date().isoformat():
            raise LifecycleError("Actual debit date is after the authenticated amount reply")
    return category_value, local_day, coverage


def resolve_receipt_review_fields_v1(raw, reply_timestamp, opening_as_of):
    """Use the same strictly-later test as ordinary DailyMovement."""
    category, actual_day, coverage = parse_receipt_review_fields_v1(
        raw, reply_timestamp, has_opening=opening_as_of is not None
    )
    if opening_as_of is None:
        return category, actual_day, "no_baseline", "no_active_opening"
    if actual_day is not None and _later(
        validate_as_of(
            {
                "format": "finance-daily-as-of-v1",
                "precision": "date",
                "timestamp": None,
                "local_date": actual_day,
                "timezone": "Asia/Singapore",
            }
        ),
        validate_as_of(opening_as_of),
    ):
        if coverage == "included":
            raise LifecycleError("Coverage answer contradicts strictly later debit date")
        return category, actual_day, "excluded", "strictly_later"
    if coverage is not None:
        return category, actual_day, coverage, "authenticated_reply"
    return category, actual_day, None, None


def _complete_review_fields(preparation, evidence, opening):
    wanted = receipt_review_fields_target_v1(preparation)
    payment, source = preparation.payment, preparation.source
    reply = evidence.reply
    if (
        evidence.target != wanted
        or evidence.principal_id != source.principal_id
        or evidence.client_id != source.client_id
        or reply.question_timestamp < payment.debit.reply_timestamp
        or reply.reply_update_id <= payment.debit.reply_update_id
        or {reply.question_response_id, reply.response_id}
        & {
            payment.selection.question_response_id,
            payment.selection.response_id,
            payment.debit.question_response_id,
            payment.debit.response_id,
        }
        or {reply.question_message_id, reply.reply_message_id}
        & {
            payment.selection.question_message_id,
            payment.selection.reply_message_id,
            payment.debit.question_message_id,
            payment.debit.reply_message_id,
        }
    ):
        raise LifecycleError("Receipt review fields differ from the linked payment")
    category, actual_day, treatment, basis = resolve_receipt_review_fields_v1(
        reply.raw_text,
        payment.debit.reply_timestamp,
        opening["as_of"] if opening is not None else None,
    )
    return ReceiptReviewFieldsV1(
        preparation,
        evidence,
        category,
        actual_day,
        treatment,
        basis,
        ("opening_coverage",) if treatment is None else (),
    )
