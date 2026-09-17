"""Authoritative, deterministic content hashes for parser proposal conversion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from finance_core.money import (
    MoneyValidationError,
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.effective_payload import resolve_effective_payload


class ProposalContentHashError(ValueError):
    """Raised when authoritative parser proposal data cannot be hashed safely."""


def compute_proposal_content_hash(conn: sqlite3.Connection, parser_output: dict[str, Any]) -> str:
    """Hash persisted conversion inputs, never caller-provided proposal state.

    The contract covers the stable proposal identity, source/raw evidence,
    parser schema identity, material transaction fields, payer/account fields,
    and attachment evidence.  Money is normalized with the Money Contract so
    equivalent values such as ``6.4`` and ``6.40`` bind identically.

    Important: this function hashes the *original* parser output payload.
    For proposals that have been completed through the authenticated completion
    boundary, use :func:`compute_effective_proposal_content_hash` instead,
    which resolves the effective payload (original + latest completion fields)
    before hashing.  All service-level operations (confirmation, conversion,
    stale-content checks) MUST use the effective hash when a completion exists
    so that the hash contract is consistent end to end.
    """
    try:
        payload = json.loads(parser_output["parsed_payload"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProposalContentHashError("Parser proposal JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ProposalContentHashError("Parser proposal JSON must be an object")

    material = {
        "proposal_public_id": parser_output["public_id"],
        "source": {
            "source_type": parser_output["source_type"],
            "source_public_id": parser_output["source_public_id"],
            "statement_batch_id": parser_output["statement_batch_id"],
            "raw_text": parser_output["raw_text"],
        },
        "parser": {
            "name": parser_output["parser_name"],
            "version": parser_output["parser_version"],
        },
        "transaction": {
            "intent": payload.get("intent"),
            "transaction_type": payload.get("transaction_type"),
            "amount": _canonical_amount(payload),
            "currency": _canonical_currency(payload),
            "transaction_date": payload.get("transaction_date", payload.get("date")),
            "merchant": payload.get("merchant"),
            "description": payload.get("description"),
            "payer": payload.get("payer", payload.get("paid_by")),
            "account": payload.get("account", payload.get("account_id")),
            "category": payload.get("category"),
        },
        "attachments": _attachment_evidence(conn, parser_output),
    }
    ocr_evidence = _ocr_link_evidence(conn, parser_output)
    if ocr_evidence is not None:
        material["ocr_evidence"] = ocr_evidence
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_effective_proposal_content_hash(
    conn: sqlite3.Connection, proposal: dict[str, Any]
) -> str:
    """Hash the *effective* proposal payload, resolving any completion record.

    This is the authoritative hash for all service-level operations:
    confirmation, conversion, stale-content checks, and audit evidence.
    Proposals without a completion record produce the same hash as
    :func:`compute_proposal_content_hash`.

    Always re-fetches the full parser_outputs row from the database so
    that every column required by the hash contract is present.  Callers
    may pass lightweight result dicts.
    """
    # Always re-fetch to guarantee all columns are present.
    full = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?",
        (proposal["id"],),
    ).fetchone()
    if full is None:
        raise ProposalContentHashError(f"parser output not found in database: {proposal['id']}")
    # Resolve effective payload through the single authoritative resolver.
    # When no completion exists this returns the original parsed_payload
    # unchanged, so the hash is identical to compute_proposal_content_hash.
    effective_payload_dict, _cid, _version = resolve_effective_payload(conn, full)
    effective = dict(full)
    effective["parsed_payload"] = json.dumps(effective_payload_dict, sort_keys=True)
    return compute_proposal_content_hash(conn, effective)


def _canonical_amount(payload: dict[str, Any]) -> str | None:
    value = payload.get("amount")
    currency = payload.get("currency")
    if value is None or currency is None:
        return None
    try:
        return canonicalize_proposal_money(value, currency)
    except MoneyValidationError:
        # Invalid proposals remain reviewable; conversion will reject them with
        # its existing deterministic field validation before any write.
        return _json_scalar(value)


def _canonical_currency(payload: dict[str, Any]) -> str | None:
    value = payload.get("currency")
    if value is None:
        return None
    if not isinstance(value, str):
        return _json_scalar(value)
    try:
        return normalize_currency(value)
    except MoneyValidationError:
        return _json_scalar(value)


def canonicalize_proposal_money(value: object, currency: object) -> str:
    """Return Money-Contract canonical text for valid parser conversion input.

    Callers pass the original amount object directly so float, bool, non-finite,
    unsupported-currency, sign, and minor-unit failures remain visible to the
    shared Money Contract.
    """
    if not isinstance(currency, str):
        raise MoneyValidationError(
            f"parser proposal currency must be a string, got {type(currency).__name__}"
        )
    normalized_currency = normalize_currency(currency)
    amount = money_decimal(value, label="parser proposal amount")
    amount = validate_amount_for_currency(
        amount, normalized_currency, label="parser proposal amount"
    )
    amount = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
        amount, label="parser proposal amount"
    )
    return canonical_money_str(amount, normalized_currency)


def _json_scalar(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _attachment_evidence(
    conn: sqlite3.Connection, parser_output: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT public_id, file_path, file_hash
        FROM attachments
        WHERE parser_output_id = ? OR id = ?
        ORDER BY public_id ASC, id ASC
        """,
        (parser_output["id"], parser_output["attachment_id"]),
    ).fetchall()
    return [
        {
            "public_id": row["public_id"] if isinstance(row, sqlite3.Row) else row[0],
            "file_path": row["file_path"] if isinstance(row, sqlite3.Row) else row[1],
            "file_hash": row["file_hash"] if isinstance(row, sqlite3.Row) else row[2],
        }
        for row in rows
    ]


def _ocr_link_evidence(
    conn: sqlite3.Connection, parser_output: dict[str, Any]
) -> dict[str, Any] | None:
    """Return OCR link/extraction material bound to this proposal, if any.

    Returns ``None`` when no receipt OCR link exists for the proposal, or when
    the link table is absent from the schema.  In both cases the caller omits
    the ``ocr_evidence`` key entirely so that every non-OCR proposal hash stays
    byte-for-byte identical to the pre-OCR contract.
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'receipt_ocr_proposal_links'"
    ).fetchone()
    if table is None:
        return None
    cursor = conn.execute(
        """
        SELECT ropl.public_id AS link_public_id,
               ropl.link_role AS link_role,
               ext.public_id AS extraction_public_id,
               ext.extraction_fingerprint AS extraction_fingerprint,
               ext.normalized_result_hash AS normalized_result_hash,
               ext.extraction_status AS extraction_status,
               ext.attachment_id AS source_attachment_id,
               ext.source_attachment_hash AS source_attachment_hash,
               ropl.parser_contract_version AS parser_contract_version,
               ropl.proposal_input_hash AS proposal_input_hash,
               ropl.proposal_result_hash AS proposal_result_hash
        FROM receipt_ocr_proposal_links AS ropl
        JOIN receipt_ocr_extractions AS ext ON ext.id = ropl.extraction_id
        WHERE ropl.parser_output_id = ?
        ORDER BY ropl.id ASC
        """,
        (parser_output["id"],),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [column[0] for column in cursor.description]
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in columns}
    return dict(zip(columns, row, strict=True))
