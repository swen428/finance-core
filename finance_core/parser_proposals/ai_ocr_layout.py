"""Read-only OCR layout bridge; reuse the existing normalized-result hash.

No layout supplied by a model is accepted. Business callers use persisted
extraction rows; the fixed harness uses separately hash-bound fixture material.
"""

import sqlite3
from collections.abc import Mapping
from typing import Any

from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrBlock,
    ReceiptOcrError,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    _normalized_outcome,
)
from finance_core.parser_proposals.receipt_total_parser import OcrLayoutContext, ParserOcrBlock

# Audit already-sealed evidence under the existing absolute OCR contract limits.
# This does not change the ingestion/engine resource profile.
_AUDIT_LIMITS = ReceiptOcrLimits(
    max_block_count=100_000,
    max_text_characters_per_block=65_536,
    max_total_normalized_text_characters=2_000_000,
    max_page_count=100,
    max_coordinate_value=1_000_000,
    max_image_width=100_000,
    max_image_height=100_000,
)


def load_ocr_layout(
    conn: sqlite3.Connection, *, parent_payload: Mapping[str, Any], source_kind: str
) -> OcrLayoutContext | None:
    """Read complete persisted geometry, after the caller verifies parent lineage."""
    if source_kind != "receipt_local_ocr_text":
        return None
    evidence = parent_payload.get("ocr_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("OCR parent evidence is missing")
    row = conn.execute(
        "SELECT id, normalized_result_hash, extraction_status, sanitized_outcome_code "
        "FROM receipt_ocr_extractions WHERE public_id = ?",
        (evidence.get("extraction_public_id"),),
    ).fetchone()
    if row is None or row[1] != evidence.get("normalized_result_hash") or row[2] != "succeeded":
        raise ValueError("OCR extraction binding is invalid")
    cursor = conn.execute(
        "SELECT sequence_index, page_index, engine_block_index, engine_paragraph_index, "
        "engine_line_index, engine_word_index, normalized_text AS text, "
        "coordinate_left AS left, coordinate_top AS top, coordinate_width AS width, "
        "coordinate_height AS height, page_width, page_height, confidence_scaled "
        "FROM receipt_ocr_blocks WHERE extraction_id = ? ORDER BY sequence_index",
        (row[0],),
    )
    columns = [d[0] for d in cursor.description]
    return verify_ocr_layout(
        {
            "extraction_public_id": evidence["extraction_public_id"],
            "outcome_code": row[3],
            "blocks": [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()],
        },
        parent_payload=parent_payload,
    )


def verify_ocr_layout(
    material: Mapping[str, Any], *, parent_payload: Mapping[str, Any]
) -> OcrLayoutContext:
    """Recompute all OCR bytes and geometry before granting any role proof."""
    if set(material) != {"extraction_public_id", "outcome_code", "blocks"}:
        raise ValueError("OCR layout fields are invalid")
    evidence = parent_payload.get("ocr_evidence")
    if not isinstance(evidence, Mapping) or material["extraction_public_id"] != evidence.get(
        "extraction_public_id"
    ):
        raise ValueError("OCR layout extraction does not match")
    rows = material["blocks"]
    if not isinstance(rows, list) or not rows or len(rows) > _AUDIT_LIMITS.max_block_count:
        raise ValueError("OCR layout blocks are invalid")
    blocks = tuple(ReceiptOcrBlock(**row) for row in rows)
    try:
        normalized = _normalized_outcome(
            ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks,
            material["outcome_code"],
            limits=_AUDIT_LIMITS,
        )
    except ReceiptOcrError as exc:
        raise ValueError("OCR layout blocks do not normalize") from exc
    if normalized.result_hash != evidence.get("normalized_result_hash"):
        raise ValueError("OCR layout normalized hash does not match")
    return OcrLayoutContext(
        extraction_public_id=str(material["extraction_public_id"]),
        normalized_result_hash=normalized.result_hash,
        blocks=tuple(
            ParserOcrBlock(
                sequence_index=b.sequence_index,
                page_index=b.page_index,
                text=b.text,
                left=b.left,
                top=b.top,
                engine_line_index=b.engine_line_index,
                confidence_scaled=b.confidence_scaled,
                engine_block_index=b.engine_block_index,
                engine_paragraph_index=b.engine_paragraph_index,
                height=b.height,
            )
            for b in normalized.blocks
        ),
    )
