"""Read verified OCR suggestions against an exact original receipt message."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .contracts import LifecycleError, VerifiedReceiptSourceV1, digest

if TYPE_CHECKING:
    from finance_core.intake.receipt_media import ReceiptMediaTotalFieldsV1


@dataclass(frozen=True)
class RecordedReceiptFieldsV1:
    source_id: str
    original_message_sha256: str
    suggestions: ReceiptMediaTotalFieldsV1


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
