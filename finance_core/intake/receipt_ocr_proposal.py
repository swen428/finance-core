"""Ingest verified receipt OCR evidence into total-level expense proposals.

This module binds one already-persisted, immutable receipt OCR extraction
(migration 032) to one deterministic total-level personal-expense parser
proposal (``parser_outputs``) and records an append-only OCR-to-proposal link
(migration 033).  It is staging-only evidence work: it never confirms a
proposal, converts it to a transaction, or creates receipt facts,
calculations, settlement obligations, or any other final financial state.

The deterministic parse itself lives in
:mod:`finance_core.parser_proposals.receipt_total_parser`, which has no database,
network, clock, locale, or model access.  This service adds only the bounded,
staging-only persistence boundary, the source/evidence binding, and the
idempotency, conflict, and concurrency contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from finance_core.parser_proposals.lifecycle import PARSED_PENDING_CONFIRMATION
from finance_core.parser_proposals.receipt_total_parser import (
    PARSER_CONTRACT_VERSION_DEFAULT,
    PARSER_NAME,
    PARSER_VERSION,
    PROPOSAL_INTENT,
    PROPOSAL_TRANSACTION_TYPE,
    ParserOcrBlock,
    ReceiptTotalParseResult,
    parse_receipt_total,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

LINK_ROLE_INITIAL = "initial"
_LINK_PUBLIC_ID_PREFIX = "ropl_"
_EVIDENCE_SOURCE_TYPE_OCR = "ocr"

_SAFE_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,200}$")
_CONTRACT_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_FIELD_ORDER = ("merchant", "amount", "currency", "transaction_date")


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ReceiptOcrProposalError(RuntimeError):
    """Stable base error for receipt OCR-to-proposal ingestion."""


class InvalidProposalCommandError(ReceiptOcrProposalError):
    """The caller command, identifiers, or contract version is invalid."""


class ProposalStagingDatabaseRejectedError(ReceiptOcrProposalError):
    """The supplied SQLite connection is not an authorised staging database."""


class ProposalCallerOwnedTransactionError(ReceiptOcrProposalError):
    """The caller supplied a connection with pending work."""


class OcrExtractionNotFoundError(ReceiptOcrProposalError):
    """The referenced receipt OCR extraction does not exist."""


class UnusableOcrEvidenceError(ReceiptOcrProposalError):
    """The persisted OCR evidence is missing, malformed, or contradictory."""


class ProposalSourceBindingConflictError(ReceiptOcrProposalError):
    """The attachment or raw-intake source binding is missing or ambiguous."""


class ProposalIdempotencyConflictError(ReceiptOcrProposalError):
    """A caller-owned identifier is already bound to different material."""


class ProposalDuplicateInitialError(ProposalIdempotencyConflictError):
    """A different initial proposal already exists for this extraction/version."""


class ProposalPersistenceConflictError(ReceiptOcrProposalError):
    """Persisted proposal or link evidence failed replay verification."""


class ProposalUnexpectedPersistenceError(ReceiptOcrProposalError):
    """An unexpected SQLite or transaction failure occurred."""


# ---------------------------------------------------------------------------
# Public result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptTotalProposalIngestionResult:
    """Immutable summary of one OCR-to-total-proposal ingestion."""

    extraction_public_id: str
    proposal_public_id: str
    parser_output_id: int
    link_public_id: str
    proposal_input_hash: str
    proposal_result_hash: str
    parser_contract_version: str
    parse_status: str
    ambiguity_flags: tuple[str, ...]
    idempotent: bool


# ---------------------------------------------------------------------------
# Internal immutable views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Extraction:
    id: int
    public_id: str
    attachment_id: int
    source_attachment_hash: str
    source_attachment_size: int
    source_mime_type: str
    extraction_fingerprint: str
    extraction_status: str
    block_count: int
    normalized_result_hash: str


@dataclass(frozen=True)
class _SourceBinding:
    attachment_id: int
    attachment_public_id: str
    raw_intake_record_id: int
    raw_intake_public_id: str


@dataclass(frozen=True)
class _Command:
    extraction_public_id: str
    proposal_public_id: str
    link_public_id: str
    parser_contract_version: str


# ---------------------------------------------------------------------------
# Test-only failure seam
# ---------------------------------------------------------------------------

_failure_injection_hook: Callable[[str], None] | None = None
"""Private test-only failure seam at real proposal write boundaries."""


def _inject_failure(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


def ingest_receipt_ocr_evidence_as_total_expense_proposal(
    conn: sqlite3.Connection,
    *,
    extraction_public_id: str,
    proposal_public_id: str,
    link_public_id: str,
    parser_contract_version: str = PARSER_CONTRACT_VERSION_DEFAULT,
    before_commit: Callable[[sqlite3.Connection], None] | None = None,
) -> ReceiptTotalProposalIngestionResult:
    """Ingest one verified OCR extraction into a total-level expense proposal.

    The proposal is always created ``parsed_pending_confirmation`` and never
    confirmed, converted, or finalized.  Repeated calls with identical
    caller-owned identifiers and canonical material return the original result
    with ``idempotent = True``; conflicting reuse raises a typed conflict.
    """
    command = _validate_public_arguments(
        extraction_public_id=extraction_public_id,
        proposal_public_id=proposal_public_id,
        link_public_id=link_public_id,
        parser_contract_version=parser_contract_version,
    )

    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise ProposalStagingDatabaseRejectedError(
            "Receipt OCR proposal ingestion requires an authorised staging database."
        ) from exc
    if conn.in_transaction:
        raise ProposalCallerOwnedTransactionError(
            "Receipt OCR proposal ingestion requires a connection without pending work."
        )

    extraction = _load_extraction(conn, command.extraction_public_id)
    binding = _resolve_source_binding(conn, extraction)
    blocks = _load_blocks(conn, extraction)

    parse = parse_receipt_total(blocks, extraction_status=extraction.extraction_status)
    payload = _build_proposal_payload(parse, extraction)
    payload_json = _canonical_json(payload)
    proposal_result_hash = _sha256_hex(payload_json)
    proposal_input_hash = _compute_input_hash(command, extraction, binding)

    return _persist(
        conn,
        command=command,
        extraction=extraction,
        binding=binding,
        parse=parse,
        payload=payload,
        payload_json=payload_json,
        proposal_input_hash=proposal_input_hash,
        proposal_result_hash=proposal_result_hash,
        before_commit=before_commit,
    )


# ---------------------------------------------------------------------------
# Validation and source loading
# ---------------------------------------------------------------------------


def _validate_public_arguments(
    *,
    extraction_public_id: object,
    proposal_public_id: object,
    link_public_id: object,
    parser_contract_version: object,
) -> _Command:
    if not isinstance(extraction_public_id, str) or not _SAFE_PUBLIC_ID_RE.match(
        extraction_public_id
    ):
        raise InvalidProposalCommandError("extraction_public_id is not a valid public identifier.")
    if not isinstance(proposal_public_id, str) or not _SAFE_PUBLIC_ID_RE.match(proposal_public_id):
        raise InvalidProposalCommandError("proposal_public_id is not a valid public identifier.")
    if (
        not isinstance(link_public_id, str)
        or not _SAFE_PUBLIC_ID_RE.match(link_public_id)
        or not link_public_id.startswith(_LINK_PUBLIC_ID_PREFIX)
    ):
        raise InvalidProposalCommandError(
            f"link_public_id must be a valid identifier prefixed with {_LINK_PUBLIC_ID_PREFIX!r}."
        )
    if not isinstance(parser_contract_version, str) or not _CONTRACT_VERSION_RE.match(
        parser_contract_version
    ):
        raise InvalidProposalCommandError("parser_contract_version is not a valid contract token.")
    return _Command(
        extraction_public_id=extraction_public_id,
        proposal_public_id=proposal_public_id,
        link_public_id=link_public_id,
        parser_contract_version=parser_contract_version,
    )


def _load_extraction(conn: sqlite3.Connection, extraction_public_id: str) -> _Extraction:
    try:
        row = conn.execute(
            """
            SELECT id, public_id, attachment_id, source_attachment_hash,
                   source_attachment_size, source_mime_type, extraction_fingerprint,
                   extraction_status, block_count, normalized_result_hash
            FROM receipt_ocr_extractions
            WHERE public_id = ?
            """,
            (extraction_public_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ProposalUnexpectedPersistenceError(
            "Unable to read persisted OCR extraction."
        ) from exc
    if row is None:
        raise OcrExtractionNotFoundError(
            f"No receipt OCR extraction found for public ID: {extraction_public_id}"
        )
    return _Extraction(
        id=row["id"],
        public_id=row["public_id"],
        attachment_id=row["attachment_id"],
        source_attachment_hash=row["source_attachment_hash"],
        source_attachment_size=row["source_attachment_size"],
        source_mime_type=row["source_mime_type"],
        extraction_fingerprint=row["extraction_fingerprint"],
        extraction_status=row["extraction_status"],
        block_count=row["block_count"],
        normalized_result_hash=row["normalized_result_hash"],
    )


def _resolve_source_binding(conn: sqlite3.Connection, extraction: _Extraction) -> _SourceBinding:
    # Schema capability check: local_attachment_source exists only in >= 040.
    has_local_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_attachment_source'"
    ).fetchone()

    try:
        attachment = conn.execute(
            "SELECT id, public_id FROM attachments WHERE id = ?",
            (extraction.attachment_id,),
        ).fetchone()
        telegram_rows = conn.execute(
            """
            SELECT tas.raw_intake_record_id AS raw_intake_record_id,
                   tas.content_hash AS content_hash,
                   rir.public_id AS raw_intake_public_id
            FROM telegram_attachment_source AS tas
            JOIN raw_intake_records AS rir
              ON rir.id = tas.raw_intake_record_id
            WHERE tas.attachment_id = ?
            ORDER BY tas.raw_intake_record_id
            """,
            (extraction.attachment_id,),
        ).fetchall()
        if has_local_table:
            local_rows = conn.execute(
                """
                SELECT las.raw_intake_record_id AS raw_intake_record_id,
                       las.content_hash AS content_hash,
                       rir.public_id AS raw_intake_public_id
                FROM local_attachment_source AS las
                JOIN raw_intake_records AS rir
                  ON rir.id = las.raw_intake_record_id
                WHERE las.attachment_id = ?
                ORDER BY las.raw_intake_record_id
                """,
                (extraction.attachment_id,),
            ).fetchall()
        else:
            local_rows = []
    except sqlite3.Error as exc:
        raise ProposalUnexpectedPersistenceError("Unable to resolve OCR source binding.") from exc

    if attachment is None:
        raise ProposalSourceBindingConflictError(
            "The extraction references an attachment that no longer exists."
        )

    # Reject ambiguous dual-source binding.
    if telegram_rows and local_rows:
        raise ProposalSourceBindingConflictError(
            "The attachment has ambiguous source evidence from both telegram and local channels."
        )

    source_rows = telegram_rows or local_rows
    if not source_rows:
        raise ProposalSourceBindingConflictError(
            "The attachment has no bound source evidence (telegram or local)."
        )

    distinct_records = {row["raw_intake_record_id"] for row in source_rows}
    if len(distinct_records) != 1:
        raise ProposalSourceBindingConflictError(
            "The attachment is bound to multiple ambiguous raw-intake records."
        )

    binding_row = source_rows[0]
    if binding_row["content_hash"] != extraction.source_attachment_hash:
        raise ProposalSourceBindingConflictError(
            "The source content hash does not match the extraction attachment hash."
        )
    return _SourceBinding(
        attachment_id=attachment["id"],
        attachment_public_id=attachment["public_id"],
        raw_intake_record_id=binding_row["raw_intake_record_id"],
        raw_intake_public_id=binding_row["raw_intake_public_id"],
    )


def _load_blocks(conn: sqlite3.Connection, extraction: _Extraction) -> tuple[ParserOcrBlock, ...]:
    try:
        rows = conn.execute(
            """
            SELECT sequence_index, page_index, engine_line_index,
                   normalized_text, coordinate_left, coordinate_top, confidence_scaled
            FROM receipt_ocr_blocks
            WHERE extraction_id = ?
            ORDER BY sequence_index
            """,
            (extraction.id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ProposalUnexpectedPersistenceError("Unable to read persisted OCR blocks.") from exc

    if len(rows) != extraction.block_count:
        raise UnusableOcrEvidenceError(
            "Persisted OCR block count does not match the recorded extraction block count."
        )

    try:
        return tuple(
            ParserOcrBlock(
                sequence_index=row["sequence_index"],
                page_index=row["page_index"],
                text=row["normalized_text"],
                left=row["coordinate_left"],
                top=row["coordinate_top"],
                engine_line_index=row["engine_line_index"],
                confidence_scaled=row["confidence_scaled"],
            )
            for row in rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UnusableOcrEvidenceError("Persisted OCR blocks are malformed.") from exc


# ---------------------------------------------------------------------------
# Proposal payload and evidence
# ---------------------------------------------------------------------------


def _build_proposal_payload(
    parse: ReceiptTotalParseResult, extraction: _Extraction
) -> dict[str, object]:
    field_confidence = {
        "merchant": parse.merchant.confidence,
        "amount": parse.amount.confidence,
        "currency": parse.currency.confidence,
        "transaction_date": parse.transaction_date.confidence,
    }
    payload: dict[str, object] = {
        "intent": PROPOSAL_INTENT,
        "transaction_type": PROPOSAL_TRANSACTION_TYPE,
        "merchant": parse.merchant.value,
        "amount": parse.amount.value,
        "currency": parse.currency.value,
        "transaction_date": parse.transaction_date.value,
        "description": parse.description.value,
        "category": parse.category.value,
        "overall_confidence": parse.overall_confidence,
        "field_confidence": field_confidence,
        "ambiguity_flags": list(parse.ambiguity_flags),
        "ocr_evidence": {
            "extraction_public_id": extraction.public_id,
            "normalized_result_hash": extraction.normalized_result_hash,
            "extraction_status": extraction.extraction_status,
        },
        "field_evidence": _field_evidence_payload(parse, extraction),
        "confirmation_required": True,
        "is_final": False,
        "status": PARSED_PENDING_CONFIRMATION,
    }
    return payload


def _field_evidence_payload(
    parse: ReceiptTotalParseResult, extraction: _Extraction
) -> list[dict[str, object]]:
    fields = {
        "merchant": parse.merchant,
        "amount": parse.amount,
        "currency": parse.currency,
        "transaction_date": parse.transaction_date,
    }
    evidence: list[dict[str, object]] = []
    for name in _FIELD_ORDER:
        parsed = fields[name]
        if parsed.value is None:
            continue
        evidence.append(
            {
                "field_name": name,
                "proposed_value": parsed.value,
                "confidence": parsed.confidence,
                "evidence_source_type": _EVIDENCE_SOURCE_TYPE_OCR,
                "extraction_public_id": extraction.public_id,
                "normalized_result_hash": extraction.normalized_result_hash,
                "block_sequence_indexes": list(parsed.block_sequence_indexes),
                "excerpt": parsed.excerpt,
            }
        )
    return evidence


def _field_evidence_rows(
    parser_output_id: int, evidence: list[dict[str, object]]
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for item in evidence:
        reference = _canonical_json(
            {
                "extraction_public_id": item["extraction_public_id"],
                "normalized_result_hash": item["normalized_result_hash"],
                "block_sequence_indexes": item["block_sequence_indexes"],
            }
        )
        rows.append(
            (
                parser_output_id,
                item["field_name"],
                _scalar_text(item["proposed_value"]),
                item["confidence"],
                _EVIDENCE_SOURCE_TYPE_OCR,
                reference,
                item["excerpt"],
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _compute_input_hash(command: _Command, extraction: _Extraction, binding: _SourceBinding) -> str:
    material = {
        "extraction_public_id": extraction.public_id,
        "extraction_fingerprint": extraction.extraction_fingerprint,
        "normalized_result_hash": extraction.normalized_result_hash,
        "extraction_status": extraction.extraction_status,
        "attachment_id": extraction.attachment_id,
        "attachment_hash": extraction.source_attachment_hash,
        "source_public_id": binding.raw_intake_public_id,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "parser_contract_version": command.parser_contract_version,
        "proposal_public_id": command.proposal_public_id,
        "link_public_id": command.link_public_id,
        "link_role": LINK_ROLE_INITIAL,
    }
    return _sha256_hex(_canonical_json(material))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scalar_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Persistence unit of work
# ---------------------------------------------------------------------------


def _persist(
    conn: sqlite3.Connection,
    *,
    command: _Command,
    extraction: _Extraction,
    binding: _SourceBinding,
    parse: ReceiptTotalParseResult,
    payload: dict[str, object],
    payload_json: str,
    proposal_input_hash: str,
    proposal_result_hash: str,
    before_commit: Callable[[sqlite3.Connection], None] | None = None,
) -> ReceiptTotalProposalIngestionResult:
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = _lookup_link_by_public_id(conn, command.link_public_id)
        if existing is not None:
            result = _verify_idempotent_replay(
                conn,
                existing,
                command=command,
                extraction=extraction,
                proposal_input_hash=proposal_input_hash,
                proposal_result_hash=proposal_result_hash,
                parse=parse,
            )
            if before_commit is not None:
                before_commit(conn)
            conn.commit()
            return result

        _reject_conflicting_reuse(
            conn,
            command=command,
            extraction=extraction,
        )

        _inject_failure("before_extraction_revalidation")
        _revalidate_extraction(conn, extraction)

        _inject_failure("before_proposal_insert")
        parser_output_id = _insert_proposal(
            conn,
            command=command,
            binding=binding,
            payload_json=payload_json,
            confidence=parse.overall_confidence,
        )

        _inject_failure("before_field_evidence_insert")
        _insert_field_evidence(conn, parser_output_id, payload)

        _inject_failure("before_link_insert")
        _insert_link(
            conn,
            command=command,
            extraction=extraction,
            parser_output_id=parser_output_id,
            proposal_input_hash=proposal_input_hash,
            proposal_result_hash=proposal_result_hash,
        )

        _inject_failure("before_raw_intake_update")
        _update_raw_intake_pointer(conn, binding, parser_output_id)

        _inject_failure("before_persisted_verification")
        _verify_persisted(
            conn,
            command=command,
            parser_output_id=parser_output_id,
            proposal_result_hash=proposal_result_hash,
            payload_json=payload_json,
        )

        _inject_failure("before_commit")
        if before_commit is not None:
            before_commit(conn)
        conn.commit()
        return _result(
            command=command,
            extraction=extraction,
            parser_output_id=parser_output_id,
            proposal_input_hash=proposal_input_hash,
            proposal_result_hash=proposal_result_hash,
            parse=parse,
            idempotent=False,
        )
    except ReceiptOcrProposalError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise ProposalUnexpectedPersistenceError(
            "Receipt OCR proposal could not be persisted atomically."
        ) from exc
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if not isinstance(exc, Exception):
            raise
        raise ProposalUnexpectedPersistenceError(
            "Receipt OCR proposal persistence failed at a guarded write boundary."
        ) from exc


def _lookup_link_by_public_id(
    conn: sqlite3.Connection, link_public_id: str
) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT ropl.id AS id, ropl.public_id AS public_id, ropl.extraction_id AS extraction_id,
               ropl.parser_output_id AS parser_output_id,
               ropl.proposal_input_hash AS proposal_input_hash,
               ropl.proposal_result_hash AS proposal_result_hash,
               ropl.parser_contract_version AS parser_contract_version,
               ropl.link_role AS link_role,
               po.public_id AS proposal_public_id
        FROM receipt_ocr_proposal_links AS ropl
        JOIN parser_outputs AS po ON po.id = ropl.parser_output_id
        WHERE ropl.public_id = ?
        """,
        (link_public_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _verify_idempotent_replay(
    conn: sqlite3.Connection,
    existing: dict[str, object],
    *,
    command: _Command,
    extraction: _Extraction,
    proposal_input_hash: str,
    proposal_result_hash: str,
    parse: ReceiptTotalParseResult,
) -> ReceiptTotalProposalIngestionResult:
    matches = (
        existing["extraction_id"] == extraction.id
        and existing["proposal_public_id"] == command.proposal_public_id
        and existing["parser_contract_version"] == command.parser_contract_version
        and existing["link_role"] == LINK_ROLE_INITIAL
        and existing["proposal_input_hash"] == proposal_input_hash
        and existing["proposal_result_hash"] == proposal_result_hash
    )
    if not matches:
        raise ProposalIdempotencyConflictError(
            "The link public ID is already bound to different proposal material."
        )
    parser_output_id = existing["parser_output_id"]
    if not isinstance(parser_output_id, int):
        raise ProposalPersistenceConflictError("Persisted link has a malformed proposal identity.")
    _verify_persisted(
        conn,
        command=command,
        parser_output_id=parser_output_id,
        proposal_result_hash=proposal_result_hash,
        payload_json=None,
    )
    return _result(
        command=command,
        extraction=extraction,
        parser_output_id=parser_output_id,
        proposal_input_hash=proposal_input_hash,
        proposal_result_hash=proposal_result_hash,
        parse=parse,
        idempotent=True,
    )


def _reject_conflicting_reuse(
    conn: sqlite3.Connection,
    *,
    command: _Command,
    extraction: _Extraction,
) -> None:
    proposal_row = conn.execute(
        "SELECT id FROM parser_outputs WHERE public_id = ?",
        (command.proposal_public_id,),
    ).fetchone()
    if proposal_row is not None:
        raise ProposalIdempotencyConflictError(
            "The proposal public ID already exists without a matching OCR link."
        )
    initial_row = conn.execute(
        """
        SELECT public_id FROM receipt_ocr_proposal_links
        WHERE extraction_id = ?
          AND parser_contract_version = ?
          AND link_role = ?
        """,
        (extraction.id, command.parser_contract_version, LINK_ROLE_INITIAL),
    ).fetchone()
    if initial_row is not None:
        raise ProposalDuplicateInitialError(
            "An initial proposal already exists for this extraction and parser contract version."
        )


def _revalidate_extraction(conn: sqlite3.Connection, extraction: _Extraction) -> None:
    row = conn.execute(
        """
        SELECT extraction_fingerprint, extraction_status, normalized_result_hash,
               source_attachment_hash, block_count
        FROM receipt_ocr_extractions
        WHERE id = ?
        """,
        (extraction.id,),
    ).fetchone()
    if row is None:
        raise ProposalPersistenceConflictError("The OCR extraction disappeared before commit.")
    if (
        row["extraction_fingerprint"] != extraction.extraction_fingerprint
        or row["extraction_status"] != extraction.extraction_status
        or row["normalized_result_hash"] != extraction.normalized_result_hash
        or row["source_attachment_hash"] != extraction.source_attachment_hash
        or row["block_count"] != extraction.block_count
    ):
        raise ProposalPersistenceConflictError("The OCR extraction identity changed before commit.")


def _insert_proposal(
    conn: sqlite3.Connection,
    *,
    command: _Command,
    binding: _SourceBinding,
    payload_json: str,
    confidence: float | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, attachment_id,
            parser_name, parser_version, raw_text, parsed_payload,
            normalized_payload, confidence_score, parse_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            command.proposal_public_id,
            "telegram_image",
            binding.raw_intake_public_id,
            binding.attachment_id,
            PARSER_NAME,
            PARSER_VERSION,
            None,
            payload_json,
            payload_json,
            confidence,
            PARSED_PENDING_CONFIRMATION,
        ),
    )
    parser_output_id = cursor.lastrowid
    if parser_output_id is None:
        raise ProposalUnexpectedPersistenceError("Proposal insert returned no identity.")
    return int(parser_output_id)


def _insert_field_evidence(
    conn: sqlite3.Connection, parser_output_id: int, payload: dict[str, object]
) -> None:
    evidence = payload["field_evidence"]
    assert isinstance(evidence, list)
    rows = _field_evidence_rows(parser_output_id, evidence)
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO parser_proposal_field_evidence (
            parser_output_id, field_name, proposed_value, confidence_score,
            evidence_source_type, evidence_reference, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_link(
    conn: sqlite3.Connection,
    *,
    command: _Command,
    extraction: _Extraction,
    parser_output_id: int,
    proposal_input_hash: str,
    proposal_result_hash: str,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id,
                proposal_input_hash, proposal_result_hash,
                parser_contract_version, link_role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command.link_public_id,
                extraction.id,
                parser_output_id,
                proposal_input_hash,
                proposal_result_hash,
                command.parser_contract_version,
                LINK_ROLE_INITIAL,
                datetime.now(UTC).isoformat(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ProposalDuplicateInitialError(
            "A concurrent initial proposal already claimed this extraction and contract version."
        ) from exc


def _update_raw_intake_pointer(
    conn: sqlite3.Connection, binding: _SourceBinding, parser_output_id: int
) -> None:
    row = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE id = ?",
        (binding.raw_intake_record_id,),
    ).fetchone()
    if row is None:
        raise ProposalSourceBindingConflictError(
            "The bound raw-intake record disappeared before commit."
        )
    if row["parser_output_id"] is not None:
        # Never overwrite an existing proposal binding on the raw-intake record.
        raise ProposalSourceBindingConflictError(
            "The raw-intake record is already bound to another proposal."
        )
    conn.execute(
        """
        UPDATE raw_intake_records
        SET parser_output_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (parser_output_id, PARSED_PENDING_CONFIRMATION, binding.raw_intake_record_id),
    )


def _verify_persisted(
    conn: sqlite3.Connection,
    *,
    command: _Command,
    parser_output_id: int,
    proposal_result_hash: str,
    payload_json: str | None,
) -> None:
    row = conn.execute(
        """
        SELECT public_id, source_type, parse_status, parsed_payload
        FROM parser_outputs WHERE id = ?
        """,
        (parser_output_id,),
    ).fetchone()
    if row is None:
        raise ProposalPersistenceConflictError("Persisted proposal disappeared before verify.")
    if (
        row["public_id"] != command.proposal_public_id
        or row["source_type"] != "telegram_image"
        or row["parse_status"] != PARSED_PENDING_CONFIRMATION
    ):
        raise ProposalPersistenceConflictError(
            "Persisted proposal identity does not match the ingestion command."
        )
    stored_payload = row["parsed_payload"]
    if payload_json is not None and stored_payload != payload_json:
        raise ProposalPersistenceConflictError("Persisted proposal payload does not match.")
    if _sha256_hex(stored_payload) != proposal_result_hash:
        raise ProposalPersistenceConflictError(
            "Persisted proposal payload hash does not match the recorded result hash."
        )


def _result(
    *,
    command: _Command,
    extraction: _Extraction,
    parser_output_id: int,
    proposal_input_hash: str,
    proposal_result_hash: str,
    parse: ReceiptTotalParseResult,
    idempotent: bool,
) -> ReceiptTotalProposalIngestionResult:
    return ReceiptTotalProposalIngestionResult(
        extraction_public_id=extraction.public_id,
        proposal_public_id=command.proposal_public_id,
        parser_output_id=parser_output_id,
        link_public_id=command.link_public_id,
        proposal_input_hash=proposal_input_hash,
        proposal_result_hash=proposal_result_hash,
        parser_contract_version=command.parser_contract_version,
        parse_status=PARSED_PENDING_CONFIRMATION,
        ambiguity_flags=parse.ambiguity_flags,
        idempotent=idempotent,
    )


def get_receipt_ocr_extraction_status_for_proposal(
    conn: sqlite3.Connection, parser_output_id: int
) -> str | None:
    """Read-only extraction status behind the earliest OCR link of a proposal.

    Returns the linked receipt OCR extraction's ``extraction_status`` (for
    example ``succeeded``, ``no_text``, or a partial outcome) or ``None``
    when the proposal carries no receipt OCR link.  Does not start a
    transaction and never mutates state.
    """
    row = conn.execute(
        """
        SELECT ext.extraction_status AS extraction_status
        FROM receipt_ocr_proposal_links AS ropl
        JOIN receipt_ocr_extractions AS ext ON ext.id = ropl.extraction_id
        WHERE ropl.parser_output_id = ?
        ORDER BY ropl.id ASC
        LIMIT 1
        """,
        (parser_output_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["extraction_status"])
