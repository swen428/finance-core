"""Protected preparation of receipt review inputs from original human evidence."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from finance_core.instance_admission.sessions import _evidence_read_callback
from finance_core.money import SUPPORTED_CURRENCIES, MoneyValidationError

from .contracts import LifecycleError, RuntimeBinding, VerifiedReceiptSourceV1, digest
from .daily_v1._impacts_v1 import account_inventory
from .daily_v1.accounts import DailyAccountServiceV1, _audit
from .daily_v1.contracts import (
    AccountDefinitionRefV1,
    BundleFactRefV1,
    OpeningRefV1,
    account_ref,
    opening_ref,
    sha,
    text,
)
from .daily_v1.movement_contracts import _day, _positive_money
from .receipt_draft_v1 import ReceiptManualDraftV1
from .receipt_fields_v1 import (
    ReceiptWholeDraftV1,
    _project_payment_draft,
    _read_optional_recorded_fields,
)
from .receipt_payment_handoff_v1 import ReceiptPaymentAccountOptionV1

if TYPE_CHECKING:
    from pathlib import Path

    from finance_core.intake.receipt_media import MediaOcrReference, ReceiptMediaProcessor

    from .service import LedgerService

_DEBIT_AMOUNT = re.compile(r"(?:([A-Z]{3}) )?((?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?)")


@dataclass(frozen=True)
class VerifiedReceiptPaymentReplyV1:
    """A reply authenticated against the exact original delivered question."""

    question_response_id: str
    question_response_sha256: str
    question_message_id: int
    question_timestamp: int
    reply_message_id: int
    reply_update_id: int
    reply_timestamp: int
    response_id: str
    response_sha256: str
    raw_text: str

    def __post_init__(self):
        for value in (self.question_response_id, self.response_id):
            text(value, "receipt payment response id", 512)
        for value in (self.question_response_sha256, self.response_sha256):
            sha(value)
        numbers = (
            self.question_message_id,
            self.question_timestamp,
            self.reply_message_id,
            self.reply_update_id,
            self.reply_timestamp,
        )
        if (
            any(type(value) is not int or not 0 <= value < 2**63 for value in numbers)
            or not self.question_message_id
            or not self.reply_message_id
            or self.question_message_id == self.reply_message_id
            or not 0 < self.question_timestamp <= self.reply_timestamp
            or self.question_response_id == self.response_id
            or type(self.raw_text) is not str
            or not 1 <= len(self.raw_text) <= 4096
        ):
            raise LifecycleError("Exact original receipt payment reply required")


@dataclass(frozen=True)
class VerifiedReceiptPaymentV1:
    """Original verified payment questions and replies for one receipt draft.

    The installed port authenticates known delivery, accepted native replies,
    actor/client, exact quoted questions and the selected first-question
    option. It derives the source/draft hashes from that retained question and
    the selection references and exact account from the retained debit question.
    It reads that account from the delivered question itself.
    """

    source_evidence_id: str
    source_sha256: str
    draft_sha256: str
    principal_id: str
    client_id: str
    option_number: int
    account: ReceiptPaymentAccountOptionV1
    selection: VerifiedReceiptPaymentReplyV1
    debit: VerifiedReceiptPaymentReplyV1
    debit_question_selection_response_id: str
    debit_question_selection_response_sha256: str
    debit_question_selection_update_id: int
    debit_question_account: ReceiptPaymentAccountOptionV1

    def __post_init__(self):
        for value in (
            self.source_evidence_id,
            self.principal_id,
            self.client_id,
            self.debit_question_selection_response_id,
        ):
            text(value, "receipt payment identity", 512)
        for value in (
            self.source_sha256,
            self.draft_sha256,
            self.debit_question_selection_response_sha256,
        ):
            sha(value)
        if (
            type(self.option_number) is not int
            or not 0 < self.option_number < 2**63
            or type(self.debit_question_selection_update_id) is not int
            or not 0 <= self.debit_question_selection_update_id < 2**63
            or type(self.selection) is not VerifiedReceiptPaymentReplyV1
            or type(self.debit) is not VerifiedReceiptPaymentReplyV1
        ):
            raise LifecycleError("Exact authenticated receipt payment required")
        for account in (self.account, self.debit_question_account):
            if (
                type(account) is not ReceiptPaymentAccountOptionV1
                or type(account.account_ref) is not AccountDefinitionRefV1
                or type(account.bundle_ref) is not BundleFactRefV1
                or account.account_ref.account_id != account.bundle_ref.record_id
            ):
                raise LifecycleError("Exact original receipt payment account required")
            text(account.name, "receipt account name")
            text(account.account_type, "receipt account type")
            text(account.currency, "receipt account currency", 3)


@dataclass(frozen=True)
class ReceiptReviewPreparationV1:
    """Read-only input for a future receipt review whose own confirmation is required."""

    binding: RuntimeBinding
    source: VerifiedReceiptSourceV1
    draft: ReceiptManualDraftV1 | ReceiptWholeDraftV1
    payment: VerifiedReceiptPaymentV1
    business_date: str
    merchant: str
    original_amount: str
    original_currency: str
    account_amount: str
    account_currency: str
    active_opening_ref: OpeningRefV1 | None
    missing_movement_fields: tuple[str, ...]
    format: str = "finance-receipt-review-preparation-v1"

    @property
    def preparation_sha256(self) -> str:
        """Commit to the full original evidence and reviewed account dependency."""
        return digest(asdict(self))


def _copy_account(account):
    return replace(
        account,
        account_ref=replace(account.account_ref),
        bundle_ref=replace(account.bundle_ref),
    )


def _receipt_payment(conn, port, source, draft):
    expected_source = replace(source)
    expected_draft = deepcopy(draft)
    draft_sha256 = digest(asdict(draft))
    source_sha256 = digest(asdict(source))
    with _evidence_read_callback(conn):
        verify = getattr(port, "receipt_review_payment_v1", None)
        if not callable(verify):
            raise LifecycleError("Installed evidence port lacks authenticated receipt payment")
        payment = verify(conn, expected_source, expected_draft)
        if (
            digest(asdict(expected_source)) != source_sha256
            or digest(asdict(expected_draft)) != draft_sha256
        ):
            raise LifecycleError("Receipt callback changed its expected draft or source")
        if payment is None:
            return None
        if type(payment) is not VerifiedReceiptPaymentV1:
            raise LifecycleError("Exact authenticated receipt payment required")
        payment = replace(payment)
        selection = replace(payment.selection)
        debit = replace(payment.debit)
        payment = replace(
            payment,
            account=_copy_account(payment.account),
            selection=selection,
            debit=debit,
            debit_question_account=_copy_account(payment.debit_question_account),
        )
    manual = draft.manual if type(draft) is ReceiptWholeDraftV1 else draft
    last_fields = None
    if manual is not None:
        last_fields = (
            manual.manual_supplements[-1] if manual.manual_supplements else manual.manual_reply
        )
    payment_messages = {
        selection.question_message_id,
        selection.reply_message_id,
        debit.question_message_id,
        debit.reply_message_id,
    }
    payment_responses = {
        selection.question_response_id,
        selection.response_id,
        debit.question_response_id,
        debit.response_id,
    }
    if (
        payment.source_evidence_id != source.evidence_id
        or payment.source_sha256 != source_sha256
        or payment.draft_sha256 != draft_sha256
        or payment.principal_id != source.principal_id
        or payment.client_id != source.client_id
        or selection.raw_text.strip() != str(payment.option_number)
        or len(payment_messages) != 4
        or len(payment_responses) != 4
        or selection.question_timestamp < source.source_timestamp
        or (
            last_fields is not None
            and (
                last_fields.question_message_id in payment_messages
                or last_fields.reply_message_id in payment_messages
                or selection.question_timestamp < last_fields.reply_timestamp
                or selection.reply_update_id <= last_fields.reply_update_id
            )
        )
        or debit.question_timestamp < selection.reply_timestamp
        or debit.reply_update_id <= selection.reply_update_id
        or payment.debit_question_selection_response_id != selection.response_id
        or payment.debit_question_selection_response_sha256 != selection.response_sha256
        or payment.debit_question_selection_update_id != selection.reply_update_id
        or payment.debit_question_account != payment.account
    ):
        raise LifecycleError("Receipt payment differs from original draft and linked replies")
    return payment


def _current_account(conn, account):
    for item in account_inventory(conn):
        values = item["values"]
        definition = values["account"]
        current = ReceiptPaymentAccountOptionV1(
            account_ref(values),
            BundleFactRefV1(**item["fact_ref"]),
            definition["name"],
            definition["account_type"],
            definition["currency"],
        )
        if current == account:
            opening = values["opening"]
            if opening and opening["status"] == "ACTIVE":
                return current, opening_ref(values), opening
            return current, None, None
    raise LifecycleError("stale_receipt_payment_account_or_opening")


class ReceiptReviewReaderV1:
    """Prepare receipt review inputs through the existing admitted ledger owner.

    The installed port must supply `receipt_review_payment_v1(conn, source, draft)`
    on this same protected read connection. Missing observed payment returns
    None. Preparation has no review, delivery or confirmation authority.
    """

    def __init__(self, service: LedgerService):
        self._boundary = DailyAccountServiceV1(service)

    def prepare(
        self,
        source_id: str,
        *,
        processor: ReceiptMediaProcessor | None = None,
        reference: MediaOcrReference | None = None,
        retained_evidence_root: str | Path | None = None,
    ) -> ReceiptReviewPreparationV1 | None:
        """Read a complete payment from original evidence on one protected transaction."""
        recorded = _read_optional_recorded_fields(
            self._boundary._service.reader(),
            source_id,
            processor,
            reference,
            retained_evidence_root=retained_evidence_root,
        )
        with self._boundary._read() as (service, conn):
            preparation, _ = self._prepare(service, conn, source_id, recorded)
            return preparation

    def completed(
        self,
        source_id: str,
        *,
        processor: ReceiptMediaProcessor | None = None,
        reference: MediaOcrReference | None = None,
        retained_evidence_root: str | Path | None = None,
    ):
        """Read the separately observed review fields without creating a review or fact."""
        from .receipt_review_fields_v1 import (
            ReceiptReviewFieldsTargetV1,
            VerifiedReceiptReviewFieldsReplyV1,
            _complete_review_fields,
            _copy_review_fields_target,
        )

        recorded = _read_optional_recorded_fields(
            self._boundary._service.reader(),
            source_id,
            processor,
            reference,
            retained_evidence_root=retained_evidence_root,
        )
        with self._boundary._read() as (service, conn):
            preparation, opening = self._prepare(service, conn, source_id, recorded)
            if preparation is None:
                return None
            with _evidence_read_callback(conn):
                verify = getattr(service.port._port, "receipt_review_fields_reply_v1", None)
                if not callable(verify):
                    raise LifecycleError(
                        "Installed evidence port lacks receipt review fields reply"
                    )
                evidence = verify(conn, preparation.source.evidence_id)
                if evidence is None:
                    return None
                if (
                    type(evidence) is not VerifiedReceiptReviewFieldsReplyV1
                    or type(evidence.target) is not ReceiptReviewFieldsTargetV1
                    or type(evidence.reply) is not VerifiedReceiptPaymentReplyV1
                    or type(evidence.principal_id) is not str
                    or type(evidence.client_id) is not str
                ):
                    raise LifecycleError("Exact authenticated receipt review reply required")
                evidence = replace(
                    evidence,
                    target=_copy_review_fields_target(evidence.target),
                    reply=replace(evidence.reply),
                )
            return _complete_review_fields(preparation, evidence, opening)

    @staticmethod
    def _prepare(service, conn, source_id, recorded=None):
        _audit(service, conn)
        source = service._receipt_source(conn, source_id, allow_archived=False)
        replies = service.port.receipt_manual_replies_v1(conn, source)
        draft = _project_payment_draft(source_id, source, replies, recorded)
        if draft is None or draft.missing_receipt_fields:
            return None, None
        payment = _receipt_payment(conn, service.port._port, source, draft)
        if payment is None:
            return None, None
        account, active_opening, opening = _current_account(conn, payment.account)
        fields = {field.name: field.value for field in draft.fields}
        original_currency = fields["currency"]
        debit = _DEBIT_AMOUNT.fullmatch(payment.debit.raw_text.strip())
        if (
            original_currency not in SUPPORTED_CURRENCIES
            or debit is None
            or debit[1] not in {None, account.currency}
        ):
            raise LifecycleError("Receipt payment currency or debit reply differs")
        try:
            original_amount = _positive_money(fields["amount"], original_currency)
            account_amount = _positive_money(debit[2], account.currency)
        except MoneyValidationError:
            raise LifecycleError("Receipt payment amount exceeds its currency precision") from None
        # The receipt date does not establish funds time or coverage by an opening.
        missing = ("category", "opening_coverage") if active_opening else ("category",)
        preparation = ReceiptReviewPreparationV1(
            service.binding,
            source,
            draft,
            replace(payment, account=account),
            _day(fields["date"]),
            text(fields["merchant"], "description", 1024),
            original_amount,
            original_currency,
            account_amount,
            account.currency,
            active_opening,
            missing,
        )
        return preparation, opening
