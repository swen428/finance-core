"""Read verified OCR suggestions against an exact original receipt message."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from .contracts import LifecycleError, VerifiedReceiptSourceV1, digest
from .receipt_draft_v1 import ReceiptManualDraftV1, _correctable_field

if TYPE_CHECKING:
    from finance_core.intake.receipt_media import ReceiptMediaTotalFieldsV1


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


def _whole_receipt_draft(source_id, source, ocr, manual):
    values = {}
    if ocr is not None:
        suggestions = ocr.suggestions.fields
        for name in _WHOLE_FIELDS:
            proposed = getattr(suggestions, "transaction_date" if name == "date" else name)
            if type(proposed.value) is str:
                values[name] = ReceiptWholeDraftFieldV1(name, proposed.value, "ocr")
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


def _read_recorded_fields(source_id, source, processor, reference):
    from finance_core.intake.receipt_media import MediaOcrReference, ReceiptMediaProcessor

    if type(processor) is not ReceiptMediaProcessor or type(reference) is not MediaOcrReference:
        raise LifecycleError("Exact verified receipt media reference is required")
    if (
        reference.operation_id != receipt_media_operation_id_v1(source)
        or reference.original_sha256 != source.original_sha256
    ):
        raise LifecycleError("Verified OCR belongs to a different receipt source")
    suggestions = processor.read_total_fields_v1(
        reference, expected_original_size=source.original_size_bytes
    )
    return RecordedReceiptFieldsV1(source_id, source.original_message_sha256, suggestions)
