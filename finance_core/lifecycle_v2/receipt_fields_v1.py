"""Read verified OCR suggestions against an exact original receipt message."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from .contracts import LifecycleError, VerifiedReceiptSourceV1, digest
from .daily_v1.contracts import text
from .receipt_draft_v1 import (
    ReceiptManualDraftV1,
    VerifiedReceiptManualReplyV1,
    _correctable_field,
    _project_manual_draft,
)

if TYPE_CHECKING:
    from pathlib import Path

    from finance_core.intake.receipt_media import (
        MediaOcrReference,
        ReceiptMediaProcessor,
        ReceiptMediaTotalFieldsV1,
    )

    from .reader import LedgerReader


@dataclass(frozen=True)
class RecordedReceiptFieldsV1:
    source_id: str
    original_message_sha256: str
    suggestions: ReceiptMediaTotalFieldsV1


@dataclass(frozen=True)
class ReceiptWholeDraftFieldV1:
    """A displayed draft value, sourced from the matching retained evidence."""

    name: str
    value: str
    origin: Literal["ocr", "manual"]


@dataclass(frozen=True)
class ReceiptWholeDraftV1:
    """One original-linked view of the suggested and explicitly supplied fields."""

    source_id: str
    source: VerifiedReceiptSourceV1
    ocr: RecordedReceiptFieldsV1 | None
    manual: ReceiptManualDraftV1 | None
    fields: tuple[ReceiptWholeDraftFieldV1, ...]
    missing_receipt_fields: tuple[str, ...]


_WHOLE_FIELDS = ("merchant", "date", "amount", "currency")


def _whole_receipt_draft(source_id, source, ocr, replies):
    values = {}
    if ocr is not None:
        suggestions = ocr.suggestions.fields
        for name in _WHOLE_FIELDS:
            proposed = getattr(suggestions, "transaction_date" if name == "date" else name)
            if type(proposed.value) is not str:
                continue
            if name == "merchant":
                try:
                    text(proposed.value, "description", 1024)
                except LifecycleError:
                    continue
            values[name] = ReceiptWholeDraftFieldV1(name, proposed.value, "ocr")
    currency = values.get("currency")
    manual = (
        _project_manual_draft(
            source_id,
            source,
            replies,
            fallback_currency=None if currency is None else currency.value,
        )
        if replies
        else None
    )
    if manual is not None:
        for field in manual.fields:
            values[field.name] = ReceiptWholeDraftFieldV1(field.name, field.value, "manual")
    unresolved = _correctable_field(values)
    if unresolved is not None:
        del values[unresolved]
    return ReceiptWholeDraftV1(
        source_id,
        source,
        ocr,
        manual,
        tuple(values[name] for name in _WHOLE_FIELDS if name in values),
        tuple(name for name in _WHOLE_FIELDS if name not in values),
    )


def receipt_media_operation_id_v1(source: VerifiedReceiptSourceV1) -> str:
    """Derive a stable local OCR operation from the authenticated original message."""
    if type(source) is not VerifiedReceiptSourceV1:
        raise LifecycleError("Exact receipt source is required for OCR analysis")
    return "media_receipt_" + digest(["daily-receipt-ocr-v1", asdict(source)])


def _require_source_media_reference(
    source: VerifiedReceiptSourceV1, reference: MediaOcrReference
) -> None:
    from finance_core.intake.receipt_media import MediaOcrReference

    if type(reference) is not MediaOcrReference:
        raise LifecycleError("Exact verified receipt media reference is required")
    if (
        reference.operation_id != receipt_media_operation_id_v1(source)
        or reference.original_sha256 != source.original_sha256
    ):
        raise LifecycleError("Verified OCR belongs to a different receipt source")


def _read_recorded_fields(source_id, source, processor, reference):
    from finance_core.intake.receipt_media import ReceiptMediaProcessor

    if type(processor) is not ReceiptMediaProcessor:
        raise LifecycleError("Exact receipt media processor required")
    _require_source_media_reference(source, reference)
    suggestions = processor.read_total_fields_v1(
        reference, expected_original_size=source.original_size_bytes
    )
    return RecordedReceiptFieldsV1(source_id, source.original_message_sha256, suggestions)


def _read_retained_recorded_fields(
    source_id: str,
    source: VerifiedReceiptSourceV1,
    evidence_root: str | Path,
    reference: MediaOcrReference,
) -> RecordedReceiptFieldsV1:
    from finance_core.intake.receipt_media import read_retained_media_total_fields_v1

    _require_source_media_reference(source, reference)
    suggestions = read_retained_media_total_fields_v1(
        evidence_root, reference, expected_original_size=source.original_size_bytes
    )
    return RecordedReceiptFieldsV1(source_id, source.original_message_sha256, suggestions)


def _read_optional_recorded_fields(
    reader: LedgerReader,
    source_id: str,
    processor: ReceiptMediaProcessor | None,
    reference: MediaOcrReference | None,
    *,
    retained_evidence_root: str | Path | None = None,
    verified_source: VerifiedReceiptSourceV1 | None = None,
) -> tuple[VerifiedReceiptSourceV1, RecordedReceiptFieldsV1] | None:
    if retained_evidence_root is not None:
        if processor is not None or reference is None:
            raise LifecycleError("Supply only the retained receipt evidence and exact reference")
        source = verified_source or reader.receipt_source_v1(source_id)
        return source, _read_retained_recorded_fields(
            source_id, source, retained_evidence_root, reference
        )
    if (processor is None) != (reference is None):
        raise LifecycleError("Supply the exact receipt media processor and reference together")
    if processor is None or reference is None:
        return None
    source = verified_source or reader.receipt_source_v1(source_id)
    return source, _read_recorded_fields(source_id, source, processor, reference)


def _project_payment_draft(
    source_id: str,
    source: VerifiedReceiptSourceV1,
    replies: tuple[VerifiedReceiptManualReplyV1, ...],
    recorded: tuple[VerifiedReceiptSourceV1, RecordedReceiptFieldsV1] | None,
) -> ReceiptManualDraftV1 | ReceiptWholeDraftV1 | None:
    if recorded is None:
        return _project_manual_draft(source_id, source, replies) if replies else None
    original, fields = recorded
    if original != source:
        raise LifecycleError("Receipt source changed before the protected payment read")
    return _whole_receipt_draft(source_id, source, fields, replies)
