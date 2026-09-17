"""Immutable data preflight bundled with migration 029.

This module is a migration artifact, not a rolling repository verifier. Its
exact bytes are included in migration 029's ledger checksum. Contract constants
and reconstruction rules are deliberately pinned here so future runtime
changes cannot silently change the meaning of an already-recorded migration.

Changing this artifact after merge is equivalent to changing an applied
migration. Corrections must use a new migration and a new immutable artifact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

PREFLIGHT_ARTIFACT_VERSION = "migration-029-authoritative-preflight-v1"
STATEMENT_IMPORT_CONTRACT_VERSION = "statement-import-v3"
GENERIC_ROW_FINGERPRINT_VERSION = "statement-row-fingerprint-v1"
PDF_ROW_FINGERPRINT_VERSION = "pdf-row-fingerprint-v2"
PDF_EVIDENCE_CONTRACT_VERSION = "pdf-row-evidence-v2"
AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS = frozenset(
    {GENERIC_ROW_FINGERPRINT_VERSION, PDF_ROW_FINGERPRINT_VERSION}
)
ROW_SET_FINGERPRINT_VERSION = "statement-row-set-v1"
IMPORT_COMMAND_VERSION = "statement-import-command-v1"
SOURCE_EVIDENCE_OBSERVATION_VERSION = "statement-source-evidence-observation-v1"
CANDIDATE_SET_VERSION = "reconciliation-candidate-set-v1"
CANONICAL_JSON_CONTRACT_VERSION = "finance-canonical-json-v1"
AUDIT_SCHEMA_VERSION = "v1"
ZERO_AUDIT_HASH = "0" * 64

_ROW_DOMAIN = "finance-statement-row-fingerprint-v1"
_ROW_PUBLIC_ID_DOMAIN = "finance-statement-row-public-id-v3"
_ROW_SET_DOMAIN = "finance-statement-row-set-v1"
_COMMAND_DOMAIN = "finance-statement-import-command-v1"
_SOURCE_EVIDENCE_DOMAIN = "finance-statement-source-evidence-observation-v1"
_DECISION_HASH_DOMAIN = "finance-reconciliation-decision-v1"
_CANDIDATE_HASH_DOMAIN = "finance-reconciliation-candidate-set-v1"
_AUDIT_STATE_DOMAIN = "finance-audit-state-v1"
_AUDIT_EVENT_DOMAIN = "finance-audit-event-v1"

_OPERATIONAL_EVIDENCE_KEYS = frozenset(
    {
        "absolute_path",
        "attachment_path",
        "audit_serialization_metadata",
        "batch_id",
        "database_row_id",
        "host",
        "import_timestamp",
        "original_filename",
        "processing_host",
        "source_file_path",
        "source_filename",
        "temporary_directory",
        "temporary_path",
    }
)
_SUPPORTED_DIRECTIONS = frozenset(
    {
        "debit",
        "credit",
        "refund",
        "reversal",
        "chargeback",
        "payment",
        "card_payment",
        "transfer_in",
        "transfer_out",
        "fee",
        "interest_debit",
        "interest_credit",
        "cash_withdrawal",
        "cash_deposit",
    }
)
_OUTGOING_DIRECTIONS = frozenset(
    {
        "debit",
        "payment",
        "card_payment",
        "transfer_out",
        "fee",
        "interest_debit",
        "cash_withdrawal",
    }
)
_INCOMING_DIRECTIONS = frozenset(
    {
        "credit",
        "refund",
        "reversal",
        "chargeback",
        "transfer_in",
        "interest_credit",
        "cash_deposit",
    }
)
_CURRENCY_PREFIXES = ("SGD", "MYR", "USD", "S$", "RM", "$")
_CURRENCY_PREFIX_CODES = {
    "S$": "SGD",
    "SGD": "SGD",
    "RM": "MYR",
    "MYR": "MYR",
    "USD": "USD",
}
_ISO_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")
_NUMBER_RE = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\Z")
_MERCHANT_ALIAS_MAP = {
    "apple.com/bill": "apple",
    "apple": "apple",
    "google": "google",
    "google play": "google",
    "google *youtube": "google",
    "netflix": "netflix",
    "netflix.com": "netflix",
    "spotify": "spotify",
    "spotify usa": "spotify",
    "uber": "uber",
    "uber trip": "uber",
    "uber eats": "uber",
    "grab": "grab",
    "grab taxi": "grab",
    "grab food": "grab",
    "foodpanda": "foodpanda",
    "foodpanda sg": "foodpanda",
    "shopee": "shopee",
    "shopee pay": "shopee",
    "lazada": "lazada",
    "lazada sg": "lazada",
    "amazon": "amazon",
    "amazon prime": "amazon",
    "amazon web services": "amazon",
    "aws": "amazon",
}


class Migration029PreflightError(ValueError):
    """Migration-028/029 authoritative data does not satisfy the pinned proof."""


class Migration029CanonicalSerializationError(ValueError):
    """A value cannot enter migration-029's pinned canonical JSON contract."""


def canonical_decimal_str(amount: Decimal) -> str:
    """Return the pinned plain-decimal representation used by migration 029."""
    if not isinstance(amount, Decimal):
        raise Migration029CanonicalSerializationError(
            f"canonical_decimal_str requires Decimal, got {type(amount).__name__}"
        )
    if not amount.is_finite():
        raise Migration029CanonicalSerializationError(
            f"canonical_decimal_str requires a finite Decimal, got {amount!r}"
        )
    normalized = amount.normalize()
    if normalized == Decimal("-0"):
        return "0"

    sign_tuple, digits, exponent = normalized.as_tuple()
    body = "".join(str(digit) for digit in digits)
    normalized_exponent = int(exponent) if isinstance(exponent, int) else 0
    if normalized_exponent > 0:
        body += "0" * normalized_exponent
    elif normalized_exponent < 0:
        negative_exponent = -normalized_exponent
        if negative_exponent >= len(body):
            body = "0." + "0" * (negative_exponent - len(body)) + body
        else:
            decimal_position = len(body) - negative_exponent
            body = body[:decimal_position] + "." + body[decimal_position:]
    return "-" + body if sign_tuple else body


def canonical_json_text(value: object) -> str:
    """Serialize with migration-029's pinned canonical JSON envelope."""
    normalized = _normalize_canonical_json(value)
    envelope = {
        "contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "value": normalized,
    }
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _normalize_canonical_json(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise Migration029CanonicalSerializationError("Decimal must be finite")
        return {"$decimal": canonical_decimal_str(value)}
    if isinstance(value, float):
        raise Migration029CanonicalSerializationError(
            "float is not supported in authoritative JSON"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise Migration029CanonicalSerializationError("datetime must be timezone-aware")
        utc_value = value.astimezone(timezone.utc)
        return {"$datetime": utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Mapping):
        normalized_mapping: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise Migration029CanonicalSerializationError("mapping keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized_mapping:
                raise Migration029CanonicalSerializationError(
                    "Unicode normalization produced duplicate keys"
                )
            normalized_mapping[key] = _normalize_canonical_json(raw_value)
        return normalized_mapping
    if isinstance(value, (list, tuple)):
        return [_normalize_canonical_json(item) for item in value]
    raise Migration029CanonicalSerializationError(
        f"unsupported authoritative JSON value: {type(value).__name__}"
    )


def verify_authoritative_state(conn: sqlite3.Connection) -> None:
    """Verify every authoritative statement batch and decision, fail closed."""
    duplicate_owner = _fetch_one(
        conn,
        """SELECT source_file_hash
        FROM statement_import_batches
        WHERE import_contract_version IS NOT NULL AND source_file_hash IS NOT NULL
        GROUP BY source_file_hash HAVING COUNT(*) > 1
        ORDER BY source_file_hash LIMIT 1""",
    )
    if duplicate_owner is not None:
        raise Migration029PreflightError("duplicate authoritative statement source owners")

    batches = _fetch_all(
        conn,
        """SELECT DISTINCT b.id
        FROM statement_import_batches AS b
        LEFT JOIN statement_transactions AS st ON st.batch_id = b.id
        WHERE b.import_contract_version IS NOT NULL
           OR b.import_command_hash IS NOT NULL
           OR b.row_set_fingerprint IS NOT NULL
           OR st.row_fingerprint_version IS NOT NULL
        ORDER BY b.id""",
    )
    for batch in batches:
        _verify_statement_batch(conn, int(batch["id"]))

    decisions = _fetch_all(
        conn,
        """SELECT public_id
        FROM reconciliation_match_results
        WHERE decision_contract_version IS NOT NULL
           OR matcher_version IS NOT NULL
           OR compatibility_version IS NOT NULL
           OR merchant_normalization_version IS NOT NULL
           OR candidate_set_fingerprint IS NOT NULL
           OR decision_hash IS NOT NULL
           OR decision_material_json IS NOT NULL
        ORDER BY id""",
    )
    for decision in decisions:
        _verify_decision(conn, str(decision["public_id"]))


def _verify_statement_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    batch = _fetch_one(
        conn,
        "SELECT * FROM statement_import_batches WHERE id = ?",
        (batch_id,),
    )
    if batch is None:
        raise Migration029PreflightError("statement batch is missing")
    rows = _fetch_all(
        conn,
        "SELECT * FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch_id,),
    )
    identity = (
        batch.get("import_contract_version"),
        batch.get("import_command_hash"),
        batch.get("row_set_fingerprint"),
    )
    if all(value is None for value in identity):
        if any(row.get("row_fingerprint_version") is not None for row in rows):
            raise Migration029PreflightError("versioned rows have no batch authority")
        return
    if any(value is None for value in identity) or not rows:
        raise Migration029PreflightError("statement batch proof is incomplete")
    if batch["import_contract_version"] != STATEMENT_IMPORT_CONTRACT_VERSION:
        raise Migration029PreflightError("unsupported statement import contract")

    fingerprints: list[tuple[str, str]] = []
    row_public_ids: list[str] = []
    for index, row in enumerate(rows, start=1):
        fingerprint = row.get("row_fingerprint")
        version = row.get("row_fingerprint_version")
        if not isinstance(fingerprint, str) or not _is_sha256(fingerprint):
            raise Migration029PreflightError("statement row fingerprint is invalid")
        if version not in AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS:
            raise Migration029PreflightError("unsupported authoritative fingerprint version")
        _verify_external_fingerprint(row)
        if version == GENERIC_ROW_FINGERPRINT_VERSION:
            expected = _generic_row_fingerprint(batch, row, index)
        else:
            expected = _pdf_row_fingerprint(batch, row)
        if not hmac.compare_digest(expected, fingerprint):
            raise Migration029PreflightError("statement row fingerprint mismatch")
        expected_public_id = _statement_row_public_id(
            fingerprint,
            _optional_text(batch.get("source_file_hash")),
        )
        if row.get("public_id") != expected_public_id:
            raise Migration029PreflightError("statement row public identity mismatch")
        fingerprints.append((fingerprint, str(version)))
        row_public_ids.append(expected_public_id)

    row_set = _row_set_fingerprint(fingerprints)
    if row_set != batch["row_set_fingerprint"]:
        raise Migration029PreflightError("statement batch row-set mismatch")
    command = _import_command_hash(
        {
            "import_contract_version": batch["import_contract_version"],
            "source_type": batch["source_type"],
            "source_content_hash": batch.get("source_file_hash"),
            "account_id": batch.get("account_id"),
            "account_name": batch.get("account_name"),
            "statement_period_start": batch.get("statement_period_start"),
            "statement_period_end": batch.get("statement_period_end"),
            "currency": batch.get("currency"),
            "row_set_fingerprint": row_set,
        }
    )
    if command != batch["import_command_hash"]:
        raise Migration029PreflightError("statement import command mismatch")
    _verify_source_evidence(conn, batch)
    _verify_statement_audit(conn, batch, rows, row_public_ids, fingerprints)


def _verify_external_fingerprint(row: Mapping[str, Any]) -> None:
    if "external_row_fingerprint" not in row:
        return
    digest = row.get("external_row_fingerprint")
    version = row.get("external_row_fingerprint_version")
    if digest is None and version is None:
        return
    if (
        not isinstance(digest, str)
        or not _is_sha256(digest)
        or not isinstance(version, str)
        or not version.strip()
    ):
        raise Migration029PreflightError("external row fingerprint evidence is invalid")


def _generic_row_fingerprint(
    batch: Mapping[str, Any],
    row: Mapping[str, Any],
    source_row_index: int,
) -> str:
    payload = _json_value(row.get("raw_row_payload_json"))
    amount = _decimal(row["amount"])
    if not amount.is_finite():
        raise Migration029PreflightError("statement amount is not finite")
    material = {
        "row_contract_version": GENERIC_ROW_FINGERPRINT_VERSION,
        "source_content_hash": batch.get("source_file_hash"),
        "import_contract_version": batch["import_contract_version"],
        "source_row_locator": row.get("statement_row_reference")
        or f"source-row-{source_row_index}",
        "transaction_date": row.get("transaction_date"),
        "posted_date": row.get("posted_date"),
        "original_amount": row.get("raw_amount"),
        "normalized_amount": canonical_decimal_str(amount),
        "currency": row["currency"],
        "direction": row.get("amount_direction"),
        "raw_amount_type": row.get("raw_amount_type"),
        "merchant_raw": row["merchant_raw"],
        "merchant_normalized": row.get("merchant_normalized"),
        "account_id": (str(row["account_id"]) if row.get("account_id") is not None else None),
        "account_name": row.get("account_name"),
        "statement_row_reference": row.get("statement_row_reference"),
        "stable_row_evidence": _stable_evidence(payload),
    }
    return _domain_hash(_ROW_DOMAIN, canonical_json_text(material))


def _pdf_row_fingerprint(
    batch: Mapping[str, Any],
    row: Mapping[str, Any],
) -> str:
    payload = _json_object(row.get("raw_row_payload_json"))
    amount = _decimal(row["amount"])
    direction = _required_text(row.get("amount_direction"), "PDF direction")
    if amount <= 0 or direction not in _SUPPORTED_DIRECTIONS:
        raise Migration029PreflightError("PDF amount or direction is not authoritative")
    if payload.get("evidence_contract_version") != PDF_EVIDENCE_CONTRACT_VERSION:
        raise Migration029PreflightError("PDF evidence version mismatch")
    if payload.get("row_fingerprint_version") != PDF_ROW_FINGERPRINT_VERSION:
        raise Migration029PreflightError("PDF fingerprint version mismatch")
    if payload.get("review_status") != "authoritative":
        raise Migration029PreflightError("PDF row is not authoritative")
    if payload.get("direction") != direction:
        raise Migration029PreflightError("PDF direction mismatch")
    if payload.get("direction_source") not in {"explicit_token", "explicit_column"}:
        raise Migration029PreflightError("PDF direction source is not explicit")
    if payload.get("direction_confidence") != "high":
        raise Migration029PreflightError("PDF direction confidence is not high")
    if payload.get("source_content_hash") != batch.get("source_file_hash") or not _is_sha256(
        str(payload.get("source_content_hash") or "")
    ):
        raise Migration029PreflightError("PDF source content identity mismatch")
    _positive_int(payload.get("source_page_number"), "PDF page")
    if payload.get("source_row_number") is not None:
        _positive_int(payload.get("source_row_number"), "PDF row")
    locator = _required_text(payload.get("stable_row_locator"), "PDF row locator")
    if locator != locator.strip() or locator != row.get("statement_row_reference"):
        raise Migration029PreflightError("PDF row locator mismatch")
    for field in (
        "attachment_path",
        "source_text_excerpt",
        "original_line_text",
        "parser_name",
        "parser_version",
        "template_name",
        "template_version",
        "extraction_version",
        "currency_token",
        "currency_source",
    ):
        _required_text(payload.get(field), f"PDF {field}")

    original_token = _required_text(
        payload.get("original_amount_token"),
        "PDF original amount token",
    )
    if original_token != row.get("raw_amount"):
        raise Migration029PreflightError("PDF original amount evidence mismatch")
    parsed_amount = _parse_amount_token(original_token)
    if parsed_amount is None:
        raise Migration029PreflightError("PDF amount token is invalid")
    parsed_value, parsed_sign, _prefix = parsed_amount
    if abs(parsed_value) != amount or payload.get("original_amount_sign") != parsed_sign:
        raise Migration029PreflightError("PDF amount sign evidence mismatch")
    convention = payload.get("amount_sign_convention")
    if convention not in {"unsigned_explicit", "outflow_positive", "outflow_negative"}:
        raise Migration029PreflightError("PDF amount sign convention is invalid")
    if not _sign_direction_compatible(parsed_sign, direction, str(convention)):
        raise Migration029PreflightError("PDF amount sign contradicts direction")
    normalized_amount = _canonical_pdf_amount_text(payload.get("normalized_amount"))
    if normalized_amount != amount:
        raise Migration029PreflightError("PDF canonical amount mismatch")

    currency = _required_text(row.get("currency"), "PDF currency")
    if currency != currency.strip().upper() or not _ISO_CURRENCY_RE.fullmatch(currency):
        raise Migration029PreflightError("PDF currency is invalid")
    currency_resolution, has_conflict = _currency_resolution(
        original_token,
        str(payload["currency_token"]),
        currency,
    )
    if has_conflict or payload.get("currency_resolution") != currency_resolution:
        raise Migration029PreflightError("PDF currency evidence mismatch")
    if row.get("transaction_date") is None and row.get("posted_date") is None:
        raise Migration029PreflightError("PDF row has no usable date")
    if row.get("transaction_date") is not None:
        _required_text(payload.get("transaction_date_token"), "PDF transaction date token")
    if row.get("posted_date") is not None:
        _required_text(payload.get("posted_date_token"), "PDF posted date token")

    fingerprint_material = {
        "row_contract_version": PDF_ROW_FINGERPRINT_VERSION,
        "source_content_hash": batch.get("source_file_hash"),
        "page_number": payload.get("source_page_number"),
        "row_number": payload.get("source_row_number"),
        "row_locator": locator,
        "original_amount_token": original_token,
        "original_amount_sign": parsed_sign,
        "amount_sign_convention": convention,
        "normalized_amount": canonical_decimal_str(amount),
        "currency": currency,
        "currency_token": payload.get("currency_token"),
        "currency_source": payload.get("currency_source"),
        "currency_resolution": currency_resolution,
        "direction": direction,
        "direction_source": payload.get("direction_source"),
        "direction_confidence": payload.get("direction_confidence"),
        "transaction_date": row.get("transaction_date"),
        "transaction_date_token": payload.get("transaction_date_token"),
        "posted_date": row.get("posted_date"),
        "posted_date_token": payload.get("posted_date_token"),
        "merchant_or_description": row.get("merchant_raw"),
        "table_section_id": payload.get("table_section_id"),
        "parser_name": payload.get("parser_name"),
        "parser_version": payload.get("parser_version"),
        "template_name": payload.get("template_name"),
        "template_version": payload.get("template_version"),
        "extraction_version": payload.get("extraction_version"),
        "evidence_contract_version": PDF_EVIDENCE_CONTRACT_VERSION,
    }
    fingerprint = _domain_hash(_ROW_DOMAIN, canonical_json_text(fingerprint_material))
    expected_payload: dict[str, Any] = {
        "evidence_contract_version": PDF_EVIDENCE_CONTRACT_VERSION,
        "row_fingerprint": fingerprint,
        "row_fingerprint_version": PDF_ROW_FINGERPRINT_VERSION,
        "source_content_hash": batch.get("source_file_hash"),
        "attachment_path": payload.get("attachment_path"),
        "source_filename": payload.get("source_filename"),
        "source_text_excerpt": payload.get("source_text_excerpt"),
        "original_line_text": payload.get("original_line_text"),
        "raw_row_text": payload.get("original_line_text"),
        "parser_name": payload.get("parser_name"),
        "parser_version": payload.get("parser_version"),
        "template_name": payload.get("template_name"),
        "template_version": payload.get("template_version"),
        "extraction_version": payload.get("extraction_version"),
        "direction": direction,
        "direction_source": payload.get("direction_source"),
        "direction_confidence": payload.get("direction_confidence"),
        "review_status": "authoritative",
        "review_reason": payload.get("review_reason"),
        "original_amount_token": original_token,
        "original_amount_sign": parsed_sign,
        "amount_sign_convention": convention,
        "normalized_amount": canonical_decimal_str(amount),
        "currency_token": payload.get("currency_token"),
        "currency_source": payload.get("currency_source"),
        "currency_resolution": currency_resolution,
        "transaction_date_token": payload.get("transaction_date_token"),
        "posted_date_token": payload.get("posted_date_token"),
        "source_page_number": payload.get("source_page_number"),
        "source_row_ref": locator,
        "stable_row_locator": locator,
    }
    if payload.get("source_row_number") is not None:
        expected_payload["source_row_number"] = payload["source_row_number"]
    if payload.get("table_section_id") is not None:
        expected_payload["table_section_id"] = payload["table_section_id"]
    if payload != expected_payload:
        raise Migration029PreflightError("PDF evidence payload is not canonical")
    return fingerprint


def _verify_source_evidence(conn: sqlite3.Connection, batch: Mapping[str, Any]) -> None:
    evidence_rows = _fetch_all(
        conn,
        "SELECT * FROM statement_import_source_evidence WHERE batch_id = ? ORDER BY id",
        (batch["id"],),
    )
    if batch.get("source_hash_verification_status") == "verified_from_bytes" and not evidence_rows:
        raise Migration029PreflightError("verified statement source evidence is missing")
    for evidence in evidence_rows:
        if (
            evidence.get("source_content_hash") != batch.get("source_file_hash")
            or evidence.get("verification_status") != "verified_from_bytes"
        ):
            raise Migration029PreflightError("statement source evidence ownership mismatch")
        path = _required_text(evidence.get("evidence_path"), "source evidence path")
        filename = _required_text(evidence.get("original_filename"), "source filename")
        observation = _source_observation_hash(
            str(batch["import_command_hash"]),
            str(evidence["source_content_hash"]),
            path,
            filename,
        )
        event = _fetch_one(
            conn,
            """SELECT 1 AS found FROM financial_audit_events
            WHERE aggregate_type = 'statement_import_batch'
              AND aggregate_public_id = ?
              AND event_type = 'statement_import_source_evidence_observed'
              AND causation_public_id = ?""",
            (batch["public_id"], observation),
        )
        if event is None:
            raise Migration029PreflightError("statement source evidence audit is missing")


def _verify_statement_audit(
    conn: sqlite3.Connection,
    batch: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    row_public_ids: Sequence[str],
    fingerprints: Sequence[tuple[str, str]],
) -> None:
    _verify_audit_chain(conn, "statement_import_batch", str(batch["public_id"]))
    event = _fetch_one(
        conn,
        """SELECT * FROM financial_audit_events
        WHERE aggregate_type = 'statement_import_batch'
          AND aggregate_public_id = ?
          AND event_type = 'statement_import_accepted'
          AND causation_public_id = ?""",
        (batch["public_id"], batch["import_command_hash"]),
    )
    if event is None:
        raise Migration029PreflightError("statement accepted audit is missing")
    payload = _canonical_audit_object(event["event_payload_json"])
    state = _canonical_audit_object(event["new_state_json"])
    expected_pdf: list[dict[str, Any]] = [
        {
            "row_public_id": str(row["public_id"]),
            "row_fingerprint": str(row["row_fingerprint"]),
            "evidence": _json_object(row.get("raw_row_payload_json")),
        }
        for row in rows
        if row.get("row_fingerprint_version") == PDF_ROW_FINGERPRINT_VERSION
    ]
    expected_pdf.sort(key=lambda item: str(item["row_fingerprint"]))
    expected_external: list[dict[str, str]] = [
        {
            "row_public_id": str(row["public_id"]),
            "external_row_fingerprint": str(row["external_row_fingerprint"]),
            "external_row_fingerprint_version": str(row["external_row_fingerprint_version"]),
        }
        for row in rows
        if row.get("external_row_fingerprint") is not None
    ]
    expected_external.sort(
        key=lambda item: (
            item["external_row_fingerprint"],
            item["external_row_fingerprint_version"],
            item["row_public_id"],
        )
    )
    expected_ids = sorted(row_public_ids)
    expected_fingerprints = sorted(fingerprint for fingerprint, _version in fingerprints)
    if (
        payload.get("source_type") != batch.get("source_type")
        or payload.get("source_file_hash") != batch.get("source_file_hash")
        or payload.get("import_contract_version") != batch.get("import_contract_version")
        or payload.get("import_command_hash") != batch.get("import_command_hash")
        or payload.get("row_set_fingerprint") != batch.get("row_set_fingerprint")
        or payload.get("row_count") != len(rows)
        or sorted(payload.get("row_public_ids", [])) != expected_ids
        or payload.get("pdf_row_evidence", []) != expected_pdf
        or payload.get("external_row_evidence", []) != expected_external
        or state.get("import_command_hash") != batch.get("import_command_hash")
        or state.get("row_set_fingerprint") != batch.get("row_set_fingerprint")
        or sorted(state.get("row_public_ids", [])) != expected_ids
        or sorted(state.get("row_fingerprints", [])) != expected_fingerprints
        or state.get("pdf_row_evidence", []) != expected_pdf
        or state.get("external_row_evidence", []) != expected_external
    ):
        raise Migration029PreflightError("statement accepted audit mismatch")


def _verify_decision(conn: sqlite3.Connection, public_id: str) -> None:
    decision = _fetch_one(
        conn,
        "SELECT * FROM reconciliation_match_results WHERE public_id = ?",
        (public_id,),
    )
    if decision is None:
        raise Migration029PreflightError("reconciliation decision is missing")
    proof_columns = (
        "decision_contract_version",
        "matcher_version",
        "compatibility_version",
        "merchant_normalization_version",
        "candidate_set_fingerprint",
        "decision_hash",
        "decision_material_json",
    )
    if any(decision.get(column) is None for column in proof_columns):
        raise Migration029PreflightError("reconciliation decision proof is incomplete")
    statement = _fetch_one(
        conn,
        """SELECT st.*, b.source_file_hash AS source_content_hash
        FROM statement_transactions AS st
        JOIN statement_import_batches AS b ON b.id = st.batch_id
        WHERE st.id = ?""",
        (decision["statement_transaction_id"],),
    )
    if statement is None:
        raise Migration029PreflightError("decision statement row is missing")
    material_json = str(decision["decision_material_json"])
    material = _canonical_envelope_value(material_json)
    if not isinstance(material, dict):
        raise Migration029PreflightError("decision material is malformed")
    expected_hash = _domain_hash(_DECISION_HASH_DOMAIN, material_json)
    if not _is_sha256(str(decision["decision_hash"])) or not hmac.compare_digest(
        expected_hash,
        str(decision["decision_hash"]),
    ):
        raise Migration029PreflightError("decision hash mismatch")
    candidates = material.get("candidates")
    thresholds = material.get("thresholds")
    if not isinstance(candidates, list) or not isinstance(thresholds, dict):
        raise Migration029PreflightError("decision candidate proof is malformed")
    candidate_ids = [
        item.get("candidate_public_id") if isinstance(item, dict) else None for item in candidates
    ]
    if any(not isinstance(item, str) for item in candidate_ids) or len(set(candidate_ids)) != len(
        candidate_ids
    ):
        raise Migration029PreflightError("decision candidate identities are invalid")
    candidate_set = _domain_hash(
        _CANDIDATE_HASH_DOMAIN,
        canonical_json_text(
            {"candidate_set_version": CANDIDATE_SET_VERSION, "candidates": candidates}
        ),
    )
    if candidate_set != decision["candidate_set_fingerprint"]:
        raise Migration029PreflightError("decision candidate-set mismatch")
    reasons = _json_value(decision["reason_codes_json"])
    evidence = _json_value(decision["evidence_json"])
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        raise Migration029PreflightError("decision reasons are malformed")
    if not isinstance(evidence, dict):
        raise Migration029PreflightError("decision evidence is malformed")
    best_candidate = decision.get("internal_candidate_id")
    if best_candidate is not None and best_candidate not in candidate_ids:
        raise Migration029PreflightError("decision best candidate is not in candidate set")
    amount = _decimal(statement["amount"])
    raw_merchant = str(statement["merchant_raw"])
    normalized_merchant = statement.get("merchant_normalized") or _normalize_merchant(raw_merchant)
    reconstructed = {
        "decision_contract_version": decision["decision_contract_version"],
        "matcher_version": decision["matcher_version"],
        "compatibility_version": decision["compatibility_version"],
        "merchant_normalization_version": decision["merchant_normalization_version"],
        "statement": {
            "derived_identity": statement["public_id"],
            "public_id": statement["public_id"],
            "row_fingerprint": statement.get("row_fingerprint"),
            "source_content_hash": statement.get("source_content_hash"),
            "source_batch_id": str(statement["batch_id"]),
            "row_reference": statement.get("statement_row_reference"),
            "original_amount_text": statement.get("raw_amount"),
            "original_amount_type": statement.get("raw_amount_type"),
            "original_amount_sign": _original_amount_sign(
                amount,
                _optional_text(statement.get("raw_amount")),
            ),
            "normalized_amount": canonical_decimal_str(amount),
            "currency": statement["currency"],
            "direction": statement.get("amount_direction"),
            "transaction_date": statement.get("transaction_date"),
            "posted_date": statement.get("posted_date"),
            "merchant_fingerprint": hashlib.sha256(raw_merchant.encode("utf-8")).hexdigest(),
            "merchant_normalized": normalized_merchant,
        },
        "thresholds": thresholds,
        "candidate_set_fingerprint": candidate_set,
        "candidates": candidates,
        "final_decision": {
            "status": decision["match_status"],
            "reason_codes": reasons,
            "best_candidate_public_id": best_candidate,
            "authorization_public_id": decision.get("authorization_public_id"),
        },
    }
    if canonical_json_text(reconstructed) != material_json:
        raise Migration029PreflightError("decision material does not match relational authority")
    if bool(decision["needs_review"]) != (decision["match_status"] != "matched"):
        raise Migration029PreflightError("decision review state mismatch")
    _verify_decision_evidence(decision, statement, evidence, candidates, candidate_ids)
    _verify_decision_audit(conn, decision, statement, reasons)


def _verify_decision_evidence(
    decision: Mapping[str, Any],
    statement: Mapping[str, Any],
    evidence: Mapping[str, Any],
    candidates: Sequence[Any],
    candidate_ids: Sequence[Any],
) -> None:
    evidence_ids = evidence.get("candidate_ids")
    count = evidence.get("candidate_count")
    if (
        not isinstance(evidence_ids, list)
        or not all(isinstance(item, str) for item in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
        or not set(evidence_ids).issubset(set(candidate_ids))
        or type(count) is not int
        or count < 0
        or count > len(candidate_ids)
    ):
        raise Migration029PreflightError("decision evidence candidate set mismatch")
    statement_amount = evidence.get("statement_amount")
    candidate_amount = evidence.get("candidate_amount")
    expected_delta = (
        abs(_decimal(statement_amount) - _decimal(candidate_amount))
        if statement_amount is not None and candidate_amount is not None
        else None
    )
    if (
        not _optional_decimal_equal(decision.get("amount_delta"), expected_delta)
        or decision.get("date_delta_days") != evidence.get("date_delta_days")
        or not _optional_decimal_equal(
            decision.get("merchant_similarity"),
            evidence.get("merchant_similarity"),
        )
        or not _candidate_evidence_matches(evidence, candidates, evidence_ids)
    ):
        raise Migration029PreflightError("decision evidence metrics mismatch")
    amount = _decimal(statement["amount"])
    if statement_amount is None or _decimal(statement_amount) != amount:
        raise Migration029PreflightError("decision statement amount evidence mismatch")
    expected = {
        "statement_currency": statement["currency"],
        "statement_txn_date": statement.get("transaction_date"),
        "statement_posted_date": statement.get("posted_date"),
        "statement_merchant": statement["merchant_raw"],
        "statement_direction": statement.get("amount_direction"),
        "original_amount_text": statement.get("raw_amount"),
        "original_amount_sign": _original_amount_sign(
            amount,
            _optional_text(statement.get("raw_amount")),
        ),
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise Migration029PreflightError("decision statement evidence mismatch")


def _candidate_evidence_matches(
    evidence: Mapping[str, Any],
    candidates: Sequence[Any],
    evidence_ids: Sequence[str],
) -> bool:
    detail_keys = (
        "candidate_amount",
        "candidate_currency",
        "candidate_txn_date",
        "candidate_merchant",
        "candidate_merchant_normalized",
        "candidate_transaction_type",
    )
    if not any(evidence.get(key) is not None for key in detail_keys):
        return True
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        merchant = evidence.get("candidate_merchant")
        if (
            candidate.get("candidate_public_id") in evidence_ids
            and _optional_decimal_equal(candidate.get("amount"), evidence.get("candidate_amount"))
            and candidate.get("currency") == evidence.get("candidate_currency")
            and candidate.get("transaction_date") == evidence.get("candidate_txn_date")
            and isinstance(merchant, str)
            and candidate.get("merchant_fingerprint")
            == hashlib.sha256(merchant.encode("utf-8")).hexdigest()
            and candidate.get("merchant_normalized")
            == evidence.get("candidate_merchant_normalized")
            and candidate.get("transaction_type") == evidence.get("candidate_transaction_type")
            and _optional_decimal_equal(
                candidate.get("merchant_similarity_score"),
                evidence.get("merchant_similarity"),
            )
            and candidate.get("date_delta_days") == evidence.get("date_delta_days")
            and candidate.get("hard_gate_results") == evidence.get("hard_gate_results")
            and candidate.get("compatibility_result") == evidence.get("compatibility_result")
        ):
            return True
    return False


def _verify_decision_audit(
    conn: sqlite3.Connection,
    decision: Mapping[str, Any],
    statement: Mapping[str, Any],
    reasons: Sequence[str],
) -> None:
    _verify_audit_chain(conn, "reconciliation_match_result", str(decision["public_id"]))
    event = _fetch_one(
        conn,
        """SELECT * FROM financial_audit_events
        WHERE aggregate_type = 'reconciliation_match_result'
          AND aggregate_public_id = ?
          AND event_type = 'reconciliation_decision_recorded'
          AND causation_public_id = ?""",
        (decision["public_id"], decision["decision_hash"]),
    )
    if event is None:
        raise Migration029PreflightError("reconciliation decision audit is missing")
    state = _canonical_audit_object(event["new_state_json"])
    references = _json_value(event["source_evidence_refs_json"])
    if not isinstance(references, list):
        raise Migration029PreflightError("decision audit references are malformed")
    required = {f"statement-row:{statement['public_id']}"}
    if statement.get("row_fingerprint"):
        required.add(f"row-fingerprint:{statement['row_fingerprint']}")
    if statement.get("source_content_hash"):
        required.add(f"source-content-sha256:{statement['source_content_hash']}")
    if (
        state.get("decision_hash") != decision["decision_hash"]
        or state.get("candidate_set_fingerprint") != decision["candidate_set_fingerprint"]
        or state.get("match_status") != decision["match_status"]
        or state.get("reason_codes") != list(reasons)
        or state.get("authorization_public_id") != decision.get("authorization_public_id")
        or not required.issubset(set(references))
    ):
        raise Migration029PreflightError("reconciliation decision audit mismatch")


def _verify_audit_chain(
    conn: sqlite3.Connection,
    aggregate_type: str,
    aggregate_public_id: str,
) -> None:
    rows = _fetch_all(
        conn,
        """SELECT * FROM financial_audit_events
        WHERE aggregate_type = ? AND aggregate_public_id = ?
        ORDER BY sequence_number""",
        (aggregate_type, aggregate_public_id),
    )
    if not rows:
        raise Migration029PreflightError("required financial audit chain is missing")
    previous_event_hash = ZERO_AUDIT_HASH
    previous_new_state_hash: str | None = None
    for expected_sequence, event in enumerate(rows, start=1):
        if event.get("audit_schema_version") != AUDIT_SCHEMA_VERSION:
            raise Migration029PreflightError("unsupported audit schema version")
        if event.get("sequence_number") != expected_sequence:
            raise Migration029PreflightError("audit sequence is not contiguous")
        if event.get("previous_event_hash") != previous_event_hash:
            raise Migration029PreflightError("audit predecessor mismatch")
        for field in (
            "event_public_id",
            "aggregate_type",
            "aggregate_public_id",
            "event_type",
            "actor_type",
            "actor_public_id",
            "correlation_public_id",
            "causation_public_id",
        ):
            _required_text(event.get(field), f"audit {field}")
        payload_text = _verified_canonical_json(str(event["event_payload_json"]))
        previous_state_text = _verified_canonical_json(str(event["previous_state_json"]))
        new_state_text = _verified_canonical_json(str(event["new_state_json"]))
        previous_state_hash = _domain_hash(_AUDIT_STATE_DOMAIN, previous_state_text)
        new_state_hash = _domain_hash(_AUDIT_STATE_DOMAIN, new_state_text)
        if (
            event.get("previous_state_hash") != previous_state_hash
            or event.get("new_state_hash") != new_state_hash
            or (
                previous_new_state_hash is not None
                and previous_state_hash != previous_new_state_hash
            )
        ):
            raise Migration029PreflightError("audit state hash mismatch")
        references_value = _json_value(event["source_evidence_refs_json"])
        if not isinstance(references_value, list) or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in references_value
        ):
            raise Migration029PreflightError("audit evidence references are invalid")
        references = tuple(sorted(unicodedata.normalize("NFC", ref) for ref in references_value))
        if len(set(references)) != len(references) or list(references) != references_value:
            raise Migration029PreflightError("audit evidence references are not canonical")
        created_at = _canonical_timestamp(event.get("created_at"))
        material = {
            "event_public_id": event["event_public_id"],
            "audit_schema_version": AUDIT_SCHEMA_VERSION,
            "aggregate_type": event["aggregate_type"],
            "aggregate_public_id": event["aggregate_public_id"],
            "event_type": event["event_type"],
            "event_payload_json": payload_text,
            "previous_state_json": previous_state_text,
            "new_state_json": new_state_text,
            "previous_state_hash": previous_state_hash,
            "new_state_hash": new_state_hash,
            "previous_event_hash": previous_event_hash,
            "actor_type": event["actor_type"],
            "actor_public_id": event["actor_public_id"],
            "authorization_public_id": event.get("authorization_public_id"),
            "calculation_snapshot_public_id": event.get("calculation_snapshot_public_id"),
            "calculation_snapshot_hash": event.get("calculation_snapshot_hash"),
            "source_evidence_references": references,
            "correlation_public_id": event["correlation_public_id"],
            "causation_public_id": event["causation_public_id"],
            "sequence_number": expected_sequence,
            "created_at": created_at,
        }
        expected_event_hash = _domain_hash(_AUDIT_EVENT_DOMAIN, canonical_json_text(material))
        if event.get("event_hash") != expected_event_hash:
            raise Migration029PreflightError("audit event hash mismatch")
        previous_event_hash = expected_event_hash
        previous_new_state_hash = new_state_hash


def _row_set_fingerprint(items: Sequence[tuple[str, str]]) -> str:
    entries = [
        {"row_fingerprint": fingerprint, "version": version} for fingerprint, version in items
    ]
    material = {
        "row_set_version": ROW_SET_FINGERPRINT_VERSION,
        "rows": sorted(
            entries,
            key=lambda entry: (entry["row_fingerprint"], entry["version"]),
        ),
    }
    return _domain_hash(_ROW_SET_DOMAIN, canonical_json_text(material))


def _import_command_hash(material: Mapping[str, Any]) -> str:
    return _domain_hash(
        _COMMAND_DOMAIN,
        canonical_json_text({"command_version": IMPORT_COMMAND_VERSION, **material}),
    )


def _statement_row_public_id(fingerprint: str, source_hash: str | None) -> str:
    material = canonical_json_text(
        {
            "identity_version": "statement-row-public-id-v3",
            "row_fingerprint": fingerprint,
            "source_content_hash": source_hash,
        }
    )
    return f"stmt-{_domain_hash(_ROW_PUBLIC_ID_DOMAIN, material)}"


def _source_observation_hash(
    command_hash: str,
    source_hash: str,
    evidence_path: str,
    original_filename: str,
) -> str:
    material = {
        "observation_version": SOURCE_EVIDENCE_OBSERVATION_VERSION,
        "import_command_hash": command_hash,
        "source_content_hash": source_hash,
        "evidence_path": evidence_path,
        "original_filename": original_filename,
    }
    return _domain_hash(_SOURCE_EVIDENCE_DOMAIN, canonical_json_text(material))


def _stable_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_evidence(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in _OPERATIONAL_EVIDENCE_KEYS
        }
    if isinstance(value, list):
        return [_stable_evidence(item) for item in value]
    return value


def _parse_amount_token(token: str) -> tuple[Decimal, str, str | None] | None:
    """Pinned PDF token grammar shared with migration-029 SQLite checks."""
    if token != token.strip() or not token:
        return None
    remaining = token
    negative = False
    sign_count = 0
    if "(" in remaining or ")" in remaining:
        if not (remaining.startswith("(") and remaining.endswith(")")):
            return None
        remaining = remaining[1:-1]
        if not remaining or remaining != remaining.strip():
            return None
        negative = True
        sign_count += 1
    if remaining.endswith("-"):
        remaining = remaining[:-1]
        negative = True
        sign_count += 1
    leading_sign: str | None = None
    if remaining.startswith(("+", "-")):
        leading_sign = remaining[0]
        remaining = remaining[1:]
        sign_count += 1
    prefix = next(
        (value for value in _CURRENCY_PREFIXES if remaining.upper().startswith(value)),
        None,
    )
    if prefix is not None:
        remaining = remaining[len(prefix) :]
        if remaining.startswith(" "):
            remaining = remaining[1:]
    if remaining.startswith(("+", "-")):
        if leading_sign is not None:
            return None
        leading_sign = remaining[0]
        remaining = remaining[1:]
        sign_count += 1
    if sign_count > 1 or not remaining or any(char.isspace() for char in remaining):
        return None
    if not _NUMBER_RE.fullmatch(remaining):
        return None
    try:
        magnitude = Decimal(remaining.replace(",", ""))
    except InvalidOperation:
        return None
    if not magnitude.is_finite():
        return None
    if leading_sign == "-":
        negative = True
    value = -magnitude if negative else magnitude
    sign = "zero" if magnitude == 0 else "negative" if negative else "positive"
    return value, sign, prefix


def _currency_resolution(
    amount_token: str,
    currency_token: str,
    row_currency: str,
) -> tuple[dict[str, str], bool]:
    parsed = _parse_amount_token(amount_token)
    prefix = parsed[2] if parsed is not None else None
    amount_code = _CURRENCY_PREFIX_CODES.get(prefix or "")
    raw_token = currency_token.strip().upper()
    if raw_token == "$":
        token_code = None
        token_supported = True
    elif raw_token in _CURRENCY_PREFIX_CODES:
        token_code = _CURRENCY_PREFIX_CODES[raw_token]
        token_supported = True
    elif _ISO_CURRENCY_RE.fullmatch(raw_token):
        token_code = raw_token
        token_supported = True
    else:
        token_code = None
        token_supported = False
    codes = {code for code in (amount_code, token_code) if code is not None}
    conflict = not token_supported or len(codes) > 1 or any(code != row_currency for code in codes)
    result = {"resolved_currency": row_currency}
    if prefix is not None:
        result["amount_token_prefix"] = prefix
    if amount_code is not None:
        result["amount_token_currency"] = amount_code
    if raw_token:
        result["currency_token"] = raw_token
    if token_code is not None:
        result["currency_token_currency"] = token_code
    return result, conflict


def _sign_direction_compatible(sign: str, direction: str, convention: str) -> bool:
    if sign not in {"positive", "negative"}:
        return False
    if convention == "unsigned_explicit":
        return not (sign == "negative" and direction in _OUTGOING_DIRECTIONS)
    if convention == "outflow_positive":
        return (sign == "positive" and direction in _OUTGOING_DIRECTIONS) or (
            sign == "negative" and direction in _INCOMING_DIRECTIONS
        )
    return (sign == "negative" and direction in _OUTGOING_DIRECTIONS) or (
        sign == "positive" and direction in _INCOMING_DIRECTIONS
    )


def _canonical_pdf_amount_text(value: Any) -> Decimal:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise Migration029PreflightError("PDF normalized amount text is invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise Migration029PreflightError("PDF normalized amount text is invalid") from exc
    if not amount.is_finite() or amount.is_signed() or amount <= 0:
        raise Migration029PreflightError("PDF normalized amount text is invalid")
    exponent = amount.normalize().as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise Migration029PreflightError("PDF normalized amount scale is invalid")
    if canonical_decimal_str(amount) != value:
        raise Migration029PreflightError("PDF normalized amount is not canonical")
    return amount


def _original_amount_sign(amount: Decimal, raw: str | None) -> str:
    if amount == 0:
        return "zero"
    if raw is None or not raw.strip():
        return "missing"
    token = raw.strip()
    return "negative" if (token.startswith("-") or token.startswith("(")) else "positive"


def _normalize_merchant(value: str) -> str:
    key = value.strip().lower()
    return _MERCHANT_ALIAS_MAP.get(key, key)


def _verified_canonical_json(value: str) -> str:
    parsed = _json_value(value)
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"contract_version", "value"}
        or parsed.get("contract_version") != CANONICAL_JSON_CONTRACT_VERSION
        or canonical_json_text(parsed["value"]) != value
    ):
        raise Migration029PreflightError("stored canonical JSON is invalid")
    return value


def _canonical_envelope_value(value: str) -> Any:
    _verified_canonical_json(value)
    parsed = _json_value(value)
    return parsed["value"]


def _canonical_audit_object(value: Any) -> dict[str, Any]:
    parsed = _canonical_envelope_value(str(value))
    if not isinstance(parsed, dict):
        raise Migration029PreflightError("audit payload must contain an object")
    return parsed


def _canonical_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise Migration029PreflightError("audit timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Migration029PreflightError("audit timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Migration029PreflightError("audit timestamp is not timezone aware")
    canonical = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if canonical != value:
        raise Migration029PreflightError("audit timestamp is not canonical UTC")
    return canonical


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Migration029PreflightError(f"{label} must be a positive integer")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Migration029PreflightError(f"{label} must be non-empty text")
    return value


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise Migration029PreflightError("authoritative amount must not be bool")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise Migration029PreflightError("authoritative amount is invalid") from exc
    if not amount.is_finite():
        raise Migration029PreflightError("authoritative amount must be finite")
    return amount


def _optional_decimal_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return _decimal(left) == _decimal(right)
    except Migration029PreflightError:
        return False


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise Migration029PreflightError("persisted JSON is malformed") from exc


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    if not isinstance(parsed, dict):
        raise Migration029PreflightError("persisted evidence must be a JSON object")
    return parsed


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _domain_hash(domain: str, payload: str) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + payload.encode("utf-8")).hexdigest()


def _fetch_all(
    conn: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(parameters))
    columns = tuple(column[0] for column in cursor.description or ())
    return [dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall()]


def _fetch_one(
    conn: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> dict[str, Any] | None:
    rows = _fetch_all(conn, sql, parameters)
    return rows[0] if rows else None


__all__ = [
    "AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS",
    "Migration029PreflightError",
    "PREFLIGHT_ARTIFACT_VERSION",
    "verify_authoritative_state",
]
