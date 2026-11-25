"""Rebuild an OCR receipt expense from closed original evidence and signed replies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from finance_core.intake.attachment_publication import AttachmentPublicationError
from finance_core.intake.receipt_media import (
    _MAX_JSON,
    PARSER_VERSION_MEDIA_FIELDS_V1,
    PNG_BYTES,
    SOURCE_BYTES,
    MediaOcrReference,
    ReceiptMediaError,
    _read_retained_media_evidence,
    _total_fields_from_verified,
)

from .contracts import LifecycleError, VerifiedReceiptSourceV1
from .daily_v1.contracts import sha, text
from .receipt_draft_v1 import (
    MAX_RECEIPT_MANUAL_REPLIES_V1,
    VerifiedReceiptManualReplyV1,
    _copy_reply,
)
from .receipt_expense_candidate_v1 import ReceiptExpenseConfirmationPreviewV1
from .receipt_fields_v1 import (
    ReceiptWholeDraftV1,
    RecordedReceiptFieldsV1,
    _require_source_media_reference,
    _whole_receipt_draft,
)
from .receipt_retained_manual_v1 import (
    ReceiptJournalReplyRefV1,
    ReceiptLedgerCutV1,
    _compile_retained_expense,
    _copy_position,
    _receipt_history_origin,
    _require_transport_prefix,
)
from .receipt_review_fields_v1 import VerifiedReceiptReviewFieldsReplyV1, _copy_review_fields_reply
from .receipt_review_preparation_v1 import VerifiedReceiptPaymentV1, _copy_payment
from .receipt_snapshot_v1 import _receipt_lookup

_MEMBER_LIMITS = {
    "declaration.json": _MAX_JSON,
    "intent.json": _MAX_JSON,
    "normalized.png": PNG_BYTES,
    "ocr.json": _MAX_JSON,
    "original.bin": SOURCE_BYTES,
    "result.json": _MAX_JSON,
}


@dataclass(frozen=True)
class ReceiptOcrMemberIdentityV1:
    name: str
    size_bytes: int
    sha256: str

    def __post_init__(self):
        if (
            type(self.name) is not str
            or self.name not in _MEMBER_LIMITS
            or type(self.size_bytes) is not int
            or not 0 < self.size_bytes <= _MEMBER_LIMITS[self.name]
        ):
            raise LifecycleError("Exact OCR member identity required")
        sha(self.sha256)


@dataclass(frozen=True)
class VerifiedReceiptOcrRegistrationV1:
    """The installed port authenticates the prepared and closed signed registration rows.

    Both rows bind the same source and member inventory. Their identities and
    hashes preserve the original journal records selected by the paid draft.
    """

    source_evidence_id: str
    source_id: str
    original_message_sha256: str
    prepared_evidence_id: str
    prepared_evidence_sha256: str
    registration_evidence_id: str
    registration_evidence_sha256: str
    reference: MediaOcrReference
    parser_version: str
    members: tuple[ReceiptOcrMemberIdentityV1, ...]

    def __post_init__(self):
        for value in (
            self.source_id,
            self.source_evidence_id,
            self.prepared_evidence_id,
            self.registration_evidence_id,
        ):
            text(value, "receipt OCR evidence id", 512)
        for value in (
            self.original_message_sha256,
            self.prepared_evidence_sha256,
            self.registration_evidence_sha256,
        ):
            sha(value)
        if (
            len({self.source_evidence_id, self.prepared_evidence_id, self.registration_evidence_id})
            != 3
            or self.prepared_evidence_sha256 == self.registration_evidence_sha256
            or type(self.reference) is not MediaOcrReference
            or type(self.parser_version) is not str
            or self.parser_version != PARSER_VERSION_MEDIA_FIELDS_V1
            or type(self.members) is not tuple
            or len(self.members) != len(_MEMBER_LIMITS)
            or any(type(member) is not ReceiptOcrMemberIdentityV1 for member in self.members)
            or tuple(member.name for member in self.members) != tuple(sorted(_MEMBER_LIMITS))
        ):
            raise LifecycleError("Exact closed receipt OCR registration required")


@dataclass(frozen=True)
class VerifiedReceiptOcrHistoryV1:
    """Original signed registration, whole-draft replies, payment and review evidence."""

    source: VerifiedReceiptSourceV1
    source_message: ReceiptJournalReplyRefV1
    manual_replies: tuple[VerifiedReceiptManualReplyV1, ...]
    payment: VerifiedReceiptPaymentV1
    review_fields: VerifiedReceiptReviewFieldsReplyV1
    reply_positions: tuple[ReceiptJournalReplyRefV1, ...]
    registration: VerifiedReceiptOcrRegistrationV1


@dataclass(frozen=True)
class RetainedReceiptOcrEvidenceV1:
    """Port result binding signed history to a selected private whole-bundle root."""

    history: VerifiedReceiptOcrHistoryV1
    evidence_root: str


@dataclass(frozen=True)
class RetainedOcrReceiptExpenseV1:
    ledger_cut: ReceiptLedgerCutV1
    history: VerifiedReceiptOcrHistoryV1
    draft: ReceiptWholeDraftV1
    preview: ReceiptExpenseConfirmationPreviewV1


def _copy_registration(registration):
    registration = replace(registration)
    return replace(
        registration,
        reference=replace(registration.reference),
        members=tuple(replace(member) for member in registration.members),
    )


def _read_ocr_history(service, conn, source, lookup):
    requested = _copy_position(lookup)
    with _receipt_lookup(service.port, conn, service.binding) as read:
        material = read("receipt_retained_ocr_history_v1", source.evidence_id, requested)
        if requested != lookup:
            raise LifecycleError("Receipt callback changed its exact reply lookup")
        if (
            type(material) is not RetainedReceiptOcrEvidenceV1
            or type(material.history) is not VerifiedReceiptOcrHistoryV1
            or type(material.evidence_root) is not str
            or not 0 < len(material.evidence_root) <= 4096
        ):
            raise LifecycleError("Exact retained receipt OCR evidence required")
        history = material.history
        if (
            type(history.source) is not VerifiedReceiptSourceV1
            or type(history.registration) is not VerifiedReceiptOcrRegistrationV1
            or type(history.manual_replies) is not tuple
            or len(history.manual_replies) > MAX_RECEIPT_MANUAL_REPLIES_V1
            or type(history.reply_positions) is not tuple
            or len(history.reply_positions) != len(history.manual_replies) + 3
        ):
            raise LifecycleError("Exact retained receipt OCR history required")
        return (
            VerifiedReceiptOcrHistoryV1(
                replace(history.source),
                _copy_position(history.source_message),
                tuple(_copy_reply(reply) for reply in history.manual_replies),
                _copy_payment(history.payment),
                _copy_review_fields_reply(history.review_fields),
                tuple(_copy_position(ref) for ref in history.reply_positions),
                _copy_registration(history.registration),
            ),
            material.evidence_root,
        )


def _read_registered_fields(source_id, source, registration, evidence_root):
    if (
        registration.source_id != source_id
        or registration.source_evidence_id != source.evidence_id
        or registration.original_message_sha256 != source.original_message_sha256
    ):
        raise LifecycleError("OCR registration differs from the original receipt source")
    _require_source_media_reference(source, registration.reference)
    root = Path(evidence_root)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise LifecycleError("Retained receipt OCR root cannot be verified") from None
    if not root.is_absolute() or str(root) != evidence_root or resolved != root:
        raise LifecycleError("Exact private retained receipt OCR root required")
    try:
        verified = _read_retained_media_evidence(root, registration.reference)
        actual = sorted(verified.inventory, key=lambda item: item["name"])
        if actual != [asdict(item) for item in registration.members]:
            raise LifecycleError("OCR members differ from the signed registration")
        suggestions = _total_fields_from_verified(verified, source.original_size_bytes)
    except (AttachmentPublicationError, OSError, ReceiptMediaError):
        raise LifecycleError("Retained receipt OCR evidence cannot be verified") from None
    return RecordedReceiptFieldsV1(source_id, source.original_message_sha256, suggestions)


def _reconstruct_ocr_expense(
    service, conn, source_id, fields_reply, *, ledger_cut=None, _rows=None
) -> RetainedOcrReceiptExpenseV1:
    cut, source, lookup = _receipt_history_origin(
        service, conn, source_id, fields_reply, ledger_cut, _rows=_rows
    )
    history, root = _read_ocr_history(service, conn, source, lookup)
    _require_transport_prefix(history, source, lookup)
    recorded = _read_registered_fields(source_id, source, history.registration, root)
    draft = _whole_receipt_draft(source_id, source, recorded, history.manual_replies)
    if draft.missing_receipt_fields:
        raise LifecycleError("Complete retained receipt whole draft required")
    preview = _compile_retained_expense(service, conn, source, history, draft, cut, _rows=_rows)
    return RetainedOcrReceiptExpenseV1(cut, history, draft, preview)
