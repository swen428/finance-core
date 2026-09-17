from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from finance_core.parser_proposals.lifecycle import SUPERSEDED, validate_transition
from finance_core.persistence_fingerprint import canonical_fingerprint
from finance_core.staging_guard import require_staging_database

PENDING_PARSE = "pending_parse"
PARSED_PENDING_CONFIRMATION = "parsed_pending_confirmation"
TELEGRAM_TEXT = "telegram_text"
TELEGRAM_CHANNEL = "telegram"
MANUAL_ENTRY = "manual_entry"
MANUAL_CHANNEL = "manual"
PARSER_NAME = "text_expense_parser"
PARSER_VERSION = "v2"


class RawIntakeIdempotencyConflictError(ValueError):
    """Same raw-intake logical key was submitted with different content."""

    reason_code = "IDEMPOTENCY_KEY_CONTENT_CONFLICT"


class RawIntakeLegacyFingerprintError(ValueError):
    """A pre-022 raw intake record lacks safe fingerprint material."""

    reason_code = "LEGACY_FINGERPRINT_UNAVAILABLE"


def create_raw_intake_record(
    conn: sqlite3.Connection,
    raw_input: str,
    *,
    source_type: str = TELEGRAM_TEXT,
    source_channel: str | None = None,
    source_metadata: Mapping[str, Any] | None = None,
    received_at: datetime | str | None = None,
    public_id: str | None = None,
) -> dict[str, Any]:
    """Persist a raw intake record before parser interpretation."""
    require_staging_database(conn)
    received_at_text = _timestamp_text(received_at)
    intake_public_id = public_id or f"raw_intake_{uuid4()}"
    source_details = _source_details(
        source_type=source_type,
        source_channel=source_channel,
        raw_input=raw_input,
        source_metadata=source_metadata,
    )

    try:
        cursor = conn.execute(
            """
            INSERT INTO raw_intake_records (
              public_id,
              source_type,
              source_channel,
              raw_input,
              received_at,
              source_received_at,
              external_source_id,
              source_message_id,
              idempotency_key,
              source_content_hash,
              content_fingerprint,
              fingerprint_version,
              status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intake_public_id,
                source_type,
                source_details["source_channel"],
                raw_input,
                received_at_text,
                source_details["source_received_at"] or received_at_text,
                source_details["external_source_id"],
                source_details["source_message_id"],
                source_details["idempotency_key"],
                source_details["source_content_hash"],
                source_details["content_fingerprint"],
                source_details["fingerprint_version"],
                PENDING_PARSE,
            ),
        )
    except sqlite3.IntegrityError as exc:
        if source_details["idempotency_key"] is None or "idempotency_key" not in str(exc):
            raise
        existing_record = get_raw_intake_record_by_idempotency_key(
            conn,
            source_details["idempotency_key"],
        )
        if existing_record is None:
            raise
        if existing_record.get("content_fingerprint") is None:
            raise RawIntakeLegacyFingerprintError(
                "Legacy raw intake record lacks reconstructable fingerprint material"
            ) from exc
        if existing_record.get("content_fingerprint") != source_details["content_fingerprint"]:
            raise RawIntakeIdempotencyConflictError(
                "Raw intake idempotency key is already bound to different content"
            ) from exc
        return existing_record

    raw_intake_id = cursor.lastrowid
    if raw_intake_id is None:
        raise RuntimeError("Raw intake insert did not return an id")
    _create_initial_raw_input_evidence(
        conn,
        raw_intake_record_id=raw_intake_id,
        raw_intake_public_id=intake_public_id,
        source_payload=source_details["source_payload"],
    )
    raw_intake_record = get_raw_intake_record(conn, raw_intake_id)
    if raw_intake_record is None:
        raise RuntimeError(f"Raw intake record not found after insert: {raw_intake_id}")
    return raw_intake_record


def get_raw_intake_record_by_idempotency_key(
    conn: sqlite3.Connection,
    idempotency_key: str,
) -> dict[str, Any] | None:
    cursor = conn.execute(
        """
        SELECT id
        FROM raw_intake_records
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return get_raw_intake_record(conn, row["id"])


def get_raw_intake_record_by_public_id(
    conn: sqlite3.Connection,
    public_id: str,
) -> dict[str, Any] | None:
    """Read-only raw-intake lookup by public identity."""
    cursor = conn.execute(
        """
        SELECT id
        FROM raw_intake_records
        WHERE public_id = ?
        """,
        (public_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return get_raw_intake_record(conn, row["id"])


def save_parser_proposal(
    conn: sqlite3.Connection,
    intake_record_id: int,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Store parser proposal JSON and keep it parsed_pending_confirmation.

    Repeat saves on an intake that already carries a proposal supersede the
    current pointer target: the new parser output is inserted as a direct
    child of the current proposal, the prior proposal moves to
    ``superseded``, and only then is the intake pointer advanced (migration
    035 permits pointer moves to direct children only).  Terminal prior
    statuses refuse with the typed lifecycle ``ValueError`` instead of a
    bare trigger ``sqlite3.IntegrityError``.
    """
    require_staging_database(conn)
    intake_record = get_raw_intake_record(conn, intake_record_id)
    if intake_record is None:
        raise ValueError(f"Raw intake record not found: {intake_record_id}")

    parent_parser_output_id: int | None = None
    parent_prior_status: str | None = None
    if intake_record["parser_output_id"] is not None:
        parent_parser_output_id = int(intake_record["parser_output_id"])
        prior = conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?",
            (parent_parser_output_id,),
        ).fetchone()
        if prior is None:
            raise ValueError(
                "Raw intake parser pointer references a missing parser output: "
                f"{parent_parser_output_id}"
            )
        parent_prior_status = str(prior[0])
        validate_transition(parent_prior_status, SUPERSEDED)

    proposal_to_store = {
        **proposal,
        "status": PARSED_PENDING_CONFIRMATION,
        "confirmation_required": True,
        "is_final": False,
    }
    parsed_payload = _json_dumps(proposal_to_store)
    confidence = proposal_to_store.get("confidence")

    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
          public_id,
          source_type,
          source_public_id,
          parser_name,
          parser_version,
          raw_text,
          parsed_payload,
          normalized_payload,
          confidence_score,
          parse_status,
          parent_parser_output_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"parser_output_{uuid4()}",
            intake_record["source_type"],
            intake_record["public_id"],
            PARSER_NAME,
            PARSER_VERSION,
            intake_record["raw_input"],
            parsed_payload,
            parsed_payload,
            _confidence_as_float(confidence),
            PARSED_PENDING_CONFIRMATION,
            parent_parser_output_id,
        ),
    )
    parser_output_id = cursor.lastrowid
    if parser_output_id is None:
        raise RuntimeError("Parser output insert did not return an id")
    _save_field_evidence(conn, parser_output_id, proposal_to_store)

    if parent_parser_output_id is not None:
        # Guard the read-then-write window: only supersede if the prior
        # status is still the one the transition was validated against.
        superseded = conn.execute(
            "UPDATE parser_outputs SET parse_status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND parse_status = ?",
            (SUPERSEDED, parent_parser_output_id, parent_prior_status),
        )
        if superseded.rowcount != 1:
            raise ValueError(
                "Prior parser proposal status changed concurrently; cannot "
                f"supersede parser output {parent_parser_output_id}"
            )

    conn.execute(
        """
        UPDATE raw_intake_records
        SET
          parser_output_id = ?,
          status = ?,
          updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            parser_output_id,
            PARSED_PENDING_CONFIRMATION,
            intake_record_id,
        ),
    )
    parser_proposal = get_parser_proposal(conn, parser_output_id=parser_output_id)
    if parser_proposal is None:
        raise RuntimeError(f"Parser proposal not found after insert: {parser_output_id}")
    return parser_proposal


def get_raw_intake_record(
    conn: sqlite3.Connection,
    intake_record_id: int,
) -> dict[str, Any] | None:
    cursor = conn.execute(
        """
        SELECT
          id,
          public_id,
          source_type,
          source_channel,
          raw_input,
          normalized_text,
          received_at,
          source_received_at,
          external_source_id,
          source_message_id,
          idempotency_key,
          source_content_hash,
          content_fingerprint,
          fingerprint_version,
          attachment_hash,
          attachment_path,
          attachment_id,
          status,
          parser_output_id,
          created_at,
          updated_at
        FROM raw_intake_records
        WHERE id = ?
        """,
        (intake_record_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row, cursor.description)


def get_parser_proposal(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int | None = None,
    intake_record_id: int | None = None,
) -> dict[str, Any] | None:
    if parser_output_id is None:
        if intake_record_id is None:
            raise ValueError("Provide parser_output_id or intake_record_id")
        intake_record = get_raw_intake_record(conn, intake_record_id)
        if intake_record is None or intake_record["parser_output_id"] is None:
            return None
        parser_output_id = intake_record["parser_output_id"]

    cursor = conn.execute(
        """
        SELECT
          id,
          public_id,
          source_type,
          source_public_id,
          parser_name,
          parser_version,
          raw_text,
          parsed_payload,
          normalized_payload,
          confidence_score,
          parse_status,
          created_at,
          updated_at
        FROM parser_outputs
        WHERE id = ?
        """,
        (parser_output_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    parser_output = _row_to_dict(row, cursor.description)
    parser_output["proposal"] = json.loads(parser_output["parsed_payload"])
    parser_output["field_evidence"] = _get_field_evidence(conn, parser_output["id"])
    return parser_output


def _source_details(
    *,
    source_type: str,
    source_channel: str | None,
    raw_input: str,
    source_metadata: Mapping[str, Any] | None,
) -> dict[str, str | None]:
    resolved_channel = source_channel or _default_source_channel(source_type)
    metadata = dict(source_metadata or {})
    source_message_id = _metadata_text(metadata, "source_message_id") or _metadata_text(
        metadata,
        "message_id",
    )
    chat_id = _metadata_text(metadata, "chat_id")
    external_source_id = _metadata_text(metadata, "external_source_id")
    if external_source_id is None and resolved_channel == TELEGRAM_CHANNEL and chat_id:
        if source_message_id is not None:
            external_source_id = f"telegram:{chat_id}:{source_message_id}"

    idempotency_key = _metadata_text(metadata, "idempotency_key")
    if idempotency_key is None and resolved_channel == TELEGRAM_CHANNEL and chat_id:
        if source_message_id is not None:
            idempotency_key = f"raw-intake:telegram:{chat_id}:{source_message_id}"
    if idempotency_key is None and external_source_id is not None and resolved_channel:
        idempotency_key = f"raw-intake:{resolved_channel}:{external_source_id}"

    return {
        "source_channel": resolved_channel,
        "source_received_at": _metadata_text(metadata, "source_received_at"),
        "external_source_id": external_source_id,
        "source_message_id": source_message_id,
        "idempotency_key": idempotency_key,
        "source_content_hash": _source_content_hash(raw_input),
        "content_fingerprint": canonical_fingerprint(
            schema_version="raw-intake-v1",
            material={
                "source_type": source_type,
                "source_channel": resolved_channel,
                "external_source_id": external_source_id,
                "source_message_id": source_message_id,
                "raw_input": raw_input,
                "attachment_content_hash": _metadata_text(metadata, "attachment_hash")
                or _metadata_text(metadata, "file_content_hash")
                or _metadata_text(metadata, "source_file_hash"),
            },
        ),
        "fingerprint_version": "raw-intake-v1",
        "source_payload": _json_dumps(metadata) if metadata else None,
    }


def _default_source_channel(source_type: str) -> str | None:
    if source_type.startswith("telegram_"):
        return TELEGRAM_CHANNEL
    if source_type == MANUAL_ENTRY:
        return MANUAL_CHANNEL
    return None


def _metadata_text(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    return str(value)


def _source_content_hash(raw_input: str) -> str:
    return f"sha256:{hashlib.sha256(raw_input.encode('utf-8')).hexdigest()}"


def _create_initial_raw_input_evidence(
    conn: sqlite3.Connection,
    *,
    raw_intake_record_id: int,
    raw_intake_public_id: str,
    source_payload: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO raw_intake_evidence (
          public_id,
          raw_intake_record_id,
          evidence_type,
          source_payload,
          evidence_reference
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            f"raw_intake_evidence_{raw_intake_public_id}_raw_input",
            raw_intake_record_id,
            "raw_input",
            source_payload,
            raw_intake_public_id,
        ),
    )


def _timestamp_text(received_at: datetime | str | None) -> str:
    if received_at is None:
        return datetime.now(UTC).isoformat()
    if isinstance(received_at, str):
        return received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    return received_at.isoformat()


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True)


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _confidence_as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _save_field_evidence(
    conn: sqlite3.Connection,
    parser_output_id: int,
    proposal: dict[str, Any],
) -> None:
    rows = []
    for evidence in proposal.get("field_evidence", []):
        field_name = evidence.get("field_name")
        if not field_name:
            continue
        rows.append(
            (
                parser_output_id,
                field_name,
                _value_as_text(evidence.get("proposed_value")),
                _confidence_as_float(evidence.get("confidence")),
                evidence.get("evidence_source_type"),
                evidence.get("evidence_reference"),
                _evidence_notes(evidence),
            )
        )
    if not rows:
        return

    conn.executemany(
        """
        INSERT INTO parser_proposal_field_evidence (
          parser_output_id,
          field_name,
          proposed_value,
          confidence_score,
          evidence_source_type,
          evidence_reference,
          notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _get_field_evidence(
    conn: sqlite3.Connection,
    parser_output_id: int,
) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
          id,
          parser_output_id,
          field_name,
          proposed_value,
          confidence_score,
          evidence_source_type,
          evidence_reference,
          notes,
          created_at
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output_id,),
    )
    return [_row_to_dict(row, cursor.description) for row in cursor.fetchall()]


def _evidence_notes(evidence: dict[str, Any]) -> str | None:
    parts = []
    substring = evidence.get("substring")
    if substring:
        parts.append(f"substring={substring}")
    start = evidence.get("start")
    end = evidence.get("end")
    if start is not None and end is not None:
        parts.append(f"span={start}:{end}")
    notes = evidence.get("notes")
    if notes:
        parts.append(str(notes))
    return "; ".join(parts) if parts else None


def _value_as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _row_to_dict(
    row: sqlite3.Row | tuple[Any, ...],
    description: tuple[Any, ...],
) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [column[0] for column in description]
    return dict(zip(columns, row, strict=True))
