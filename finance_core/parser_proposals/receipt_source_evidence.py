"""Read-only receipt provenance shared by managed source admission.

This proves persisted relationships only. It never reads original bytes and
does not impose confirmation, conversion, OCR-success or monetary readiness.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence

from finance_core.intake.raw_text_repository import TELEGRAM_PHOTO_FINGERPRINT_VERSION
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    verify_receipt_relational_evidence,
)
from finance_core.parser_proposals.receipt_facts_conversion import (
    ConversionEvidenceLineageError,
    _require_source_binding,
)
from finance_core.persistence_fingerprint import canonical_fingerprint


class ReceiptSourceEvidenceError(RuntimeError):
    """Persisted receipt source or OCR provenance is inconsistent."""


def _one(conn: sqlite3.Connection, sql: str, args: tuple[object, ...]) -> dict[str, Any]:
    rows = conn.execute(sql, args).fetchall()
    if len(rows) != 1:
        raise ReceiptSourceEvidenceError
    return dict(rows[0])


def _object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReceiptSourceEvidenceError from exc
    if not isinstance(parsed, dict):
        raise ReceiptSourceEvidenceError
    return parsed


def _verify_fingerprint(intake: Mapping[str, Any], attachment_hash: str) -> None:
    raw_input = intake.get("raw_input")
    if not isinstance(raw_input, str):
        raise ReceiptSourceEvidenceError
    if intake.get("source_content_hash") != (
        "sha256:" + hashlib.sha256(raw_input.encode("utf-8")).hexdigest()
    ):
        raise ReceiptSourceEvidenceError
    version = intake.get("fingerprint_version")
    if version != TELEGRAM_PHOTO_FINGERPRINT_VERSION:
        raise ReceiptSourceEvidenceError
    expected = canonical_fingerprint(
        schema_version=version,
        material={
            "source_type": intake.get("source_type"),
            "source_channel": intake.get("source_channel"),
            "external_source_id": intake.get("external_source_id"),
            "source_message_id": intake.get("source_message_id"),
            "raw_input": raw_input,
            "attachment_content_hash": attachment_hash,
        },
    )
    if intake.get("content_fingerprint") != expected:
        raise ReceiptSourceEvidenceError


def verify_receipt_source_evidence(
    conn: sqlite3.Connection,
    *,
    intake: Mapping[str, Any],
    chain: Sequence[Mapping[str, Any]],
) -> None:
    """Verify image intake, unique OCR links, source binding and OCR references."""
    if not chain or intake.get("source_type") != "telegram_image":
        raise ReceiptSourceEvidenceError
    root = chain[-1]
    attachment_id = root.get("attachment_id")
    if not isinstance(attachment_id, int) or attachment_id < 1:
        raise ReceiptSourceEvidenceError
    if intake.get("attachment_id") != attachment_id:
        raise ReceiptSourceEvidenceError
    extraction_id: int | None = None
    extraction: dict[str, Any] | None = None
    for member in chain:
        if member.get("attachment_id") != attachment_id:
            raise ReceiptSourceEvidenceError
        link = _one(
            conn,
            "SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
            (member["id"],),
        )
        if extraction_id is None:
            extraction_id = int(link["extraction_id"])
            extraction = _one(
                conn,
                "SELECT * FROM receipt_ocr_extractions WHERE id = ?",
                (extraction_id,),
            )
        elif link["extraction_id"] != extraction_id:
            raise ReceiptSourceEvidenceError
        assert extraction is not None
        if extraction["attachment_id"] != attachment_id:
            raise ReceiptSourceEvidenceError
        payload = _object(member.get("parsed_payload"))
        if member is root:
            try:
                verify_receipt_relational_evidence(
                    conn, proposal_id=int(member["id"]), payload=payload
                )
            except HumanRevisionLineageError as exc:
                raise ReceiptSourceEvidenceError from exc
        ocr = payload.get("ocr_evidence")
        # Sealed AI receipt children carry an OCR link but their normalized
        # proposal is AI material, without the root's OCR payload object.
        # D1/receipt edge proofs decide whether their inheritance is valid.
        if ocr is None and member is not root:
            pass
        elif not isinstance(ocr, dict) or (
            ocr.get("extraction_public_id") != extraction["public_id"]
            or ocr.get("normalized_result_hash") != extraction["normalized_result_hash"]
            or ocr.get("extraction_status") != extraction["extraction_status"]
        ):
            raise ReceiptSourceEvidenceError
        blocks = {
            int(row["sequence_index"])
            for row in conn.execute(
                "SELECT sequence_index FROM receipt_ocr_blocks WHERE extraction_id = ?",
                (extraction_id,),
            ).fetchall()
        }
        if len(blocks) != int(extraction["block_count"]):
            raise ReceiptSourceEvidenceError
        for item in payload.get("field_evidence", []):
            if not isinstance(item, dict):
                raise ReceiptSourceEvidenceError
            if item.get("evidence_source_type") != "ocr":
                continue
            indexes = item.get("block_sequence_indexes")
            if (
                item.get("extraction_public_id") != extraction["public_id"]
                or item.get("normalized_result_hash") != extraction["normalized_result_hash"]
                or not isinstance(indexes, list)
                or any(not isinstance(i, int) or i not in blocks for i in indexes)
            ):
                raise ReceiptSourceEvidenceError

    assert extraction is not None
    attachment = _one(conn, "SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    if (
        conn.execute(
            "SELECT 1 FROM local_attachment_source WHERE attachment_id = ? LIMIT 1",
            (attachment_id,),
        ).fetchone()
        is not None
    ):
        raise ReceiptSourceEvidenceError
    try:
        _require_source_binding(conn, dict(root), dict(intake), extraction, attachment)
    except ConversionEvidenceLineageError as exc:
        raise ReceiptSourceEvidenceError from exc
    _verify_fingerprint(intake, str(extraction["source_attachment_hash"]))
