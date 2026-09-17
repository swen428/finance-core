"""Internal D1 proposal publication and human-lineage verification.

These transaction-neutral functions are invoked only from the D1 draft unit
of work.  They expose no bridge, decision, confirmation, conversion, provider,
or runtime entry point.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_drafts import PublishedDraft
from finance_core.parser_proposals.lifecycle import (
    PARSED_PENDING_CONFIRMATION,
    SUPERSEDED,
    raw_intake_status_for_proposal_status,
    validate_transition,
)
from finance_core.parser_proposals.repository import ParserProposalRepository

if TYPE_CHECKING:
    from finance_core.parser_proposals.receipt_supersession import ReceiptRevisionMaterial

_TEXT_SOURCE_TYPES = frozenset({"text", "telegram_text", "telegram_raw_text"})
_RECEIPT_SOURCE_TYPES = frozenset(
    {"telegram_image", "local_image", "receipt_local_ocr_text", "receipt"}
)
_HASH_HEX = frozenset("0123456789abcdef")
_D1_PUBLICATION_TABLE = "parser_human_draft_publications"
_D1_SCHEMA_TABLES = (
    "parser_human_drafts",
    "parser_human_draft_reply_evidence",
    "parser_human_draft_operations",
    "parser_human_draft_cards",
    "parser_human_draft_card_delivery_attempts",
    "parser_human_draft_card_delivery_outcomes",
    _D1_PUBLICATION_TABLE,
    "parser_human_draft_action_bindings",
)


class HumanRevisionLineageError(RuntimeError):
    """Persisted D1 publication evidence does not verify."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _derived_id(prefix: str, domain: str, *parts: object) -> str:
    material = _canonical_json([domain, *parts]).encode("utf-8")
    return prefix + hashlib.sha256(material).hexdigest()[:32]


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not (set(value) - _HASH_HEX)


def _context_int(context: Mapping[str, object], key: str) -> int:
    value = context.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise HumanRevisionLineageError(f"human revision {key} is invalid")
    return value


def _context_fields(context: Mapping[str, object]) -> tuple[str, ...]:
    value = context.get("changed_fields")
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        raise HumanRevisionLineageError("human revision changed_fields is invalid")
    return value


def _receipt_revision_material(canonical_payload: dict[str, object], context: Mapping[str, object]):
    from finance_core.parser_proposals.human_drafts import HumanReasonContributor
    from finance_core.parser_proposals.receipt_supersession import ReceiptRevisionMaterial

    clears_value = context.get("explicit_clears")
    contributors_value = context.get("reason_contributors_after")
    supplied_value = context.get("canonical_supplied_fields")
    if (
        not isinstance(clears_value, dict)
        or not isinstance(contributors_value, tuple)
        or any(not isinstance(item, HumanReasonContributor) for item in contributors_value)
        or not isinstance(supplied_value, dict)
    ):
        raise HumanRevisionLineageError("receipt human revision context is invalid")
    clears: dict[str, tuple[object, object]] = {}
    for field, pair in clears_value.items():
        if not isinstance(field, str) or not isinstance(pair, tuple) or len(pair) != 2:
            raise HumanRevisionLineageError("receipt explicit-clear material is invalid")
        clears[field] = pair
    timestamp = _context_int(context, "timestamp")
    operation_public_id = str(context["operation_public_id"])
    return ReceiptRevisionMaterial(
        source_parser_output_id=_context_int(context, "source_parser_output_id"),
        expected_content_hash=str(context["expected_content_hash"]),
        canonical_supplied_fields=dict(supplied_value),
        canonical_payload=canonical_payload,
        changed_fields=_context_fields(context),
        authenticated_actor_id=str(context["authenticated_actor_id"]),
        correction_public_id=(
            "rcor_d1_" + hashlib.sha256(operation_public_id.encode()).hexdigest()[:32]
        ),
        correction_channel=str(context["correction_channel"]),
        reason=str(context["reason"]) if context.get("reason") is not None else None,
        timestamp=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        d1_operation_public_id=operation_public_id,
        d1_draft_public_id=str(context["draft_public_id"]),
        d1_draft_version=_context_int(context, "draft_version"),
        d1_draft_content_hash=str(context["draft_content_hash"]),
        human_reply_evidence_public_id=str(context["human_reply_evidence_public_id"]),
        explicit_clears=clears,
        reason_contributors=tuple(contributors_value),
    )


def _insert_receipt_human_field_evidence(
    conn: sqlite3.Connection,
    *,
    child_id: int,
    parent_id: int,
    canonical_payload: Mapping[str, object],
    changed_fields: tuple[str, ...],
    evidence_reference: str,
) -> None:
    placeholders = ", ".join("?" for _field in changed_fields)
    if changed_fields:
        conn.execute(
            f"""
            INSERT INTO parser_proposal_field_evidence (
                parser_output_id, field_name, proposed_value, confidence_score,
                evidence_source_type, evidence_reference, notes
            )
            SELECT ?, field_name, proposed_value, confidence_score,
                   evidence_source_type, evidence_reference, notes
            FROM parser_proposal_field_evidence
            WHERE parser_output_id = ? AND field_name NOT IN ({placeholders})
            """,
            (child_id, parent_id, *changed_fields),
        )
    conn.executemany(
        """
        INSERT INTO parser_proposal_field_evidence (
            parser_output_id, field_name, proposed_value, confidence_score,
            evidence_source_type, evidence_reference, notes
        ) VALUES (?, ?, ?, NULL, 'user_message', ?, NULL)
        """,
        (
            (
                child_id,
                field,
                None if canonical_payload.get(field) is None else str(canonical_payload[field]),
                evidence_reference,
            )
            for field in changed_fields
        ),
    )


def _build_d1_receipt_payload(
    conn: sqlite3.Connection,
    *,
    parent: Mapping[str, object],
    effective_payload: dict[str, Any],
    applied_updates: dict[str, Any],
    material: ReceiptRevisionMaterial,
) -> dict[str, Any]:
    """Build one receipt child whose embedded evidence matches its current values."""
    from finance_core.parser_proposals.receipt_supersession import (
        _build_replacement_payload,
        _canonical_monetary_pair,
        _latest_completion_provenance,
    )

    if (
        material.d1_operation_public_id is None
        or material.d1_draft_public_id is None
        or material.d1_draft_version is None
        or material.d1_draft_content_hash is None
        or material.human_reply_evidence_public_id is None
    ):
        raise HumanRevisionLineageError("D1 receipt payload binding is incomplete")
    parent_row = dict(parent)
    canonical_amount, canonical_currency = _canonical_monetary_pair(
        {
            "amount": material.canonical_payload.get("amount"),
            "currency": material.canonical_payload.get("currency"),
        },
        effective_payload,
    )
    payload = _build_replacement_payload(
        effective_payload,
        applied_updates,
        parent_row,
        material.correction_public_id,
        _latest_completion_provenance(conn, parent_row),
        canonical_amount=canonical_amount,
        canonical_currency=canonical_currency,
    )
    evidence = payload.get("field_evidence")
    if not isinstance(evidence, list):
        raise HumanRevisionLineageError("D1 receipt payload evidence is invalid")
    current_items = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if item.get("correction_public_id") != material.correction_public_id:
            continue
        item.update(
            {
                "draft_content_hash": material.d1_draft_content_hash,
                "draft_public_id": material.d1_draft_public_id,
                "draft_version": material.d1_draft_version,
                "human_reply_evidence_public_id": material.human_reply_evidence_public_id,
                "operation_public_id": material.d1_operation_public_id,
            }
        )
        current_items.append(item)
    if sorted(str(item.get("field_name")) for item in current_items) != sorted(applied_updates):
        raise HumanRevisionLineageError("D1 receipt payload evidence is incomplete")
    correction = payload.get("correction")
    if not isinstance(correction, dict):
        raise HumanRevisionLineageError("D1 receipt correction material is invalid")
    correction.update(
        {
            "draft_content_hash": material.d1_draft_content_hash,
            "draft_public_id": material.d1_draft_public_id,
            "draft_version": material.d1_draft_version,
            "human_reply_evidence_public_id": material.human_reply_evidence_public_id,
            "operation_public_id": material.d1_operation_public_id,
        }
    )
    for field in (
        "amount",
        "currency",
        "transaction_date",
        "merchant",
        "description",
        "category",
    ):
        if payload.get(field) != material.canonical_payload.get(field):
            raise HumanRevisionLineageError(
                "D1 receipt payload disagrees with the validated whole card"
            )
    return payload


def publish_receipt_nonmonetary_revision_in_transaction(
    conn: sqlite3.Connection,
    *,
    material: ReceiptRevisionMaterial,
) -> PublishedDraft:
    """Create a receipt child without fabricating a monetary revision row."""
    from finance_core.parser_proposals.receipt_supersession import (
        ReceiptRevisionMaterial,
        _inject_failure,
        _insert_superseding_link,
        _require_receipt_link,
    )

    if not isinstance(material, ReceiptRevisionMaterial) or not conn.in_transaction:
        raise HumanRevisionLineageError("receipt revision material is invalid")
    if {"amount", "currency"} & set(material.changed_fields):
        raise HumanRevisionLineageError("non-monetary receipt publisher received money")
    parent = _row(conn, material.source_parser_output_id)
    if parent["source_type"] not in _RECEIPT_SOURCE_TYPES:
        raise HumanRevisionLineageError("receipt publisher received a non-receipt source")
    _require_unconverted(conn, material.source_parser_output_id)
    _require_current_intake(conn, parent)
    parent_payload, _completion_id, _version = resolve_effective_payload(conn, parent)
    if not hmac.compare_digest(
        compute_effective_proposal_content_hash(conn, parent),
        material.expected_content_hash,
    ):
        raise HumanRevisionLineageError("receipt human revision source is stale")
    actual_changes = tuple(
        sorted(
            field
            for field in material.changed_fields
            if parent_payload.get(field) != material.canonical_payload.get(field)
        )
    )
    if actual_changes != tuple(sorted(material.changed_fields)) or not actual_changes:
        raise HumanRevisionLineageError("receipt human revision change set is invalid")
    parent_link = _require_receipt_link(conn, material.source_parser_output_id)
    proposal_public_id = _derived_id(
        "po_d1_", "d1-human-receipt-nonmonetary-v1", material.correction_public_id
    )
    child_payload = _build_d1_receipt_payload(
        conn,
        parent=parent,
        effective_payload=parent_payload,
        applied_updates={field: material.canonical_payload.get(field) for field in actual_changes},
        material=material,
    )
    payload_json = _canonical_json(child_payload)
    _inject_failure("before_child_insert")
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, statement_batch_id,
            attachment_id, parser_name, parser_version, raw_text,
            parsed_payload, normalized_payload, confidence_score, parse_status,
            parent_parser_output_id
        ) VALUES (?, ?, ?, ?, ?, 'human_revision', 'd1-human-revision-v1', ?,
                  ?, ?, NULL, ?, ?)
        """,
        (
            proposal_public_id,
            parent["source_type"],
            parent["source_public_id"],
            parent["statement_batch_id"],
            parent["attachment_id"],
            parent["raw_text"],
            payload_json,
            payload_json,
            PARSED_PENDING_CONFIRMATION,
            parent["id"],
        ),
    )
    if cursor.lastrowid is None:
        raise HumanRevisionLineageError("receipt human revision child has no identity")
    child_id = int(cursor.lastrowid)
    evidence_reference = _canonical_json(
        {
            "correction_public_id": material.correction_public_id,
            "draft_content_hash": material.d1_draft_content_hash,
            "draft_public_id": material.d1_draft_public_id,
            "draft_version": material.d1_draft_version,
            "human_reply_evidence_public_id": material.human_reply_evidence_public_id,
            "operation_public_id": material.d1_operation_public_id,
            "superseded_proposal_public_id": parent["public_id"],
        }
    )
    _inject_failure("before_field_evidence_insert")
    _insert_receipt_human_field_evidence(
        conn,
        child_id=child_id,
        parent_id=int(parent["id"]),
        canonical_payload=child_payload,
        changed_fields=actual_changes,
        evidence_reference=evidence_reference,
    )
    link_id = _derived_id("ropl_d1_", "d1-human-receipt-link-v1", material.correction_public_id)
    _inject_failure("before_link_insert")
    _insert_superseding_link(
        conn,
        link_public_id=link_id,
        extraction_id=int(parent_link["extraction_id"]),
        parser_output_id=child_id,
        parser_contract_version=str(parent_link["parser_contract_version"]),
        input_hash=hashlib.sha256(evidence_reference.encode()).hexdigest(),
        result_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
        created_at=material.timestamp,
    )
    child_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
    return PublishedDraft(child_id, proposal_public_id, 0, child_hash)


def publish_receipt_human_revision_in_transaction(
    conn: sqlite3.Connection,
    *,
    material: ReceiptRevisionMaterial,
) -> PublishedDraft:
    """Dispatch receipt revisions by actual monetary materiality."""
    if {"amount", "currency"} & set(material.changed_fields):
        from finance_core.parser_proposals.receipt_supersession import (
            supersede_receipt_total_proposal_in_transaction,
        )

        result = supersede_receipt_total_proposal_in_transaction(conn, material=material)
        replacement_id = result["replacement_parser_output_id"]
        if not isinstance(replacement_id, int) or isinstance(replacement_id, bool):
            raise HumanRevisionLineageError("receipt revision child identity is invalid")
        return PublishedDraft(
            parser_output_id=replacement_id,
            proposal_public_id=str(result["replacement_proposal_public_id"]),
            proposal_version=0,
            proposal_content_hash=str(result["replacement_content_hash"]),
        )
    return publish_receipt_nonmonetary_revision_in_transaction(conn, material=material)


def _row(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    cursor = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (parser_output_id,))
    value = cursor.fetchone()
    if value is None:
        raise HumanRevisionLineageError("human revision source proposal is missing")
    if isinstance(value, sqlite3.Row):
        return dict(value)
    return dict(zip((column[0] for column in cursor.description), value, strict=True))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _has_legacy_safe_d1_marker_ancestor(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    """Detect D1 identity without querying migration-047-only tables."""
    current_id: int | None = parser_output_id
    seen: set[int] = set()
    while current_id is not None:
        if current_id in seen:
            raise HumanRevisionLineageError("human revision parent chain contains a cycle")
        seen.add(current_id)
        row = _row(conn, current_id)
        if (
            row["parser_name"] == "human_revision"
            or row["parser_version"] == "d1-human-revision-v1"
            or conn.execute(
                "SELECT 1 FROM parser_proposal_events WHERE parser_output_id = ? "
                "AND event_reason LIKE 'D1 human revision %' LIMIT 1",
                (current_id,),
            ).fetchone()
            is not None
        ):
            return True
        parent_id = row.get("parent_parser_output_id")
        if parent_id is None:
            current_id = None
        elif isinstance(parent_id, int) and not isinstance(parent_id, bool):
            current_id = parent_id
        else:
            raise HumanRevisionLineageError("human revision parent identity is invalid")
    return False


def _require_acyclic_parent_chain(conn: sqlite3.Connection, parser_output_id: int) -> None:
    current_id: int | None = parser_output_id
    seen: set[int] = set()
    while current_id is not None:
        if current_id in seen:
            raise HumanRevisionLineageError("human revision parent chain contains a cycle")
        seen.add(current_id)
        parent_id = _row(conn, current_id).get("parent_parser_output_id")
        if parent_id is None:
            current_id = None
        elif isinstance(parent_id, int) and not isinstance(parent_id, bool):
            current_id = parent_id
        else:
            raise HumanRevisionLineageError("human revision parent identity is invalid")


def d1_publication_schema_available(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    """Distinguish a legitimate pre-047 schema from missing D1 evidence schema."""
    present_d1_tables = {table for table in _D1_SCHEMA_TABLES if _table_exists(conn, table)}
    if len(present_d1_tables) == len(_D1_SCHEMA_TABLES):
        return True
    if present_d1_tables:
        raise HumanRevisionLineageError("human revision publication schema is missing")

    post_047_migration_recorded = False
    if _table_exists(conn, "schema_migrations"):
        try:
            post_047_migration_recorded = (
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE migration_sequence >= 47 LIMIT 1"
                ).fetchone()
                is not None
            )
        except sqlite3.DatabaseError as exc:
            raise HumanRevisionLineageError("human revision migration ledger is invalid") from exc
    post_047_trigger_present = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND sql LIKE ? LIMIT 1",
            (f"%{_D1_PUBLICATION_TABLE}%",),
        ).fetchone()
        is not None
    )
    if (
        post_047_migration_recorded
        or post_047_trigger_present
        or _has_legacy_safe_d1_marker_ancestor(conn, parser_output_id)
    ):
        raise HumanRevisionLineageError("human revision publication schema is missing")
    return False


def _require_unconverted(conn: sqlite3.Connection, parser_output_id: int) -> None:
    for table in (
        "parser_proposal_authorizations",
        "parser_proposal_conversion_audit",
        "receipt_proposal_conversions",
    ):
        if (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE parser_output_id = ? LIMIT 1",
                (parser_output_id,),
            ).fetchone()
            is not None
        ):
            raise HumanRevisionLineageError("human revision source is authorized or converted")


def _require_current_intake(conn: sqlite3.Connection, parent: Mapping[str, object]) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM raw_intake_records WHERE parser_output_id = ? ORDER BY id",
        (parent["id"],),
    ).fetchall()
    if len(rows) != 1:
        raise HumanRevisionLineageError(
            "human revision source raw-intake binding is missing or ambiguous"
        )
    intake = rows[0]
    if (
        intake["public_id"] != parent["source_public_id"]
        or intake["attachment_id"] != parent["attachment_id"]
        or intake["status"] != raw_intake_status_for_proposal_status(str(parent["parse_status"]))
    ):
        raise HumanRevisionLineageError("human revision source raw-intake binding is invalid")
    return intake


def publish_text_human_revision_in_transaction(
    conn: sqlite3.Connection,
    *,
    source_parser_output_id: int,
    draft_public_id: str,
    draft_version: int,
    draft_content_hash: str,
    canonical_payload: dict[str, object],
    changed_fields: tuple[str, ...],
    authenticated_actor_id: str,
    operation_public_id: str,
    expected_content_hash: str,
    human_reply_evidence_public_id: str,
    timestamp: int,
) -> PublishedDraft:
    """Append one text child inside the caller-owned D1 transaction."""
    if not conn.in_transaction:
        raise HumanRevisionLineageError("human revision publisher requires a transaction")
    if (
        not draft_public_id
        or draft_version < 1
        or not _valid_hash(draft_content_hash)
        or not authenticated_actor_id
        or not operation_public_id
        or not human_reply_evidence_public_id
        or timestamp < 1
    ):
        raise HumanRevisionLineageError("human revision publication material is invalid")
    parent = _row(conn, source_parser_output_id)
    if parent["source_type"] not in _TEXT_SOURCE_TYPES:
        raise HumanRevisionLineageError("text publisher received a non-text source")
    if parent["parse_status"] not in {
        "parsed_pending_confirmation",
        "edited_pending_confirmation",
    }:
        raise HumanRevisionLineageError("human revision source is not pending confirmation")
    _require_unconverted(conn, source_parser_output_id)
    _require_current_intake(conn, parent)
    current_payload, _completion_id, _current_version = resolve_effective_payload(conn, parent)
    current_hash = compute_effective_proposal_content_hash(conn, parent)
    if not hmac.compare_digest(current_hash, expected_content_hash):
        raise HumanRevisionLineageError("human revision source content hash is stale")
    material = tuple(
        sorted(
            field
            for field in changed_fields
            if current_payload.get(field) != canonical_payload.get(field)
        )
    )
    if material != tuple(sorted(changed_fields)) or not material:
        raise HumanRevisionLineageError("human revision changed-field material is invalid")
    if (
        conn.execute(
            "SELECT 1 FROM parser_outputs WHERE parent_parser_output_id = ? LIMIT 1",
            (source_parser_output_id,),
        ).fetchone()
        is not None
    ):
        raise HumanRevisionLineageError("human revision source already has a child")

    proposal_public_id = _derived_id("po_d1_", "d1-human-text-proposal-v1", operation_public_id)
    payload_json = _canonical_json(canonical_payload)
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, statement_batch_id,
            attachment_id, parser_name, parser_version, ai_provider, ai_model,
            prompt_version, raw_text, parsed_payload, normalized_payload,
            confidence_score, parse_status, parent_parser_output_id
        ) VALUES (?, ?, ?, ?, ?, 'human_revision', 'd1-human-revision-v1',
                  NULL, NULL, NULL, ?, ?, ?, NULL, ?, ?)
        """,
        (
            proposal_public_id,
            parent["source_type"],
            parent["source_public_id"],
            parent["statement_batch_id"],
            parent["attachment_id"],
            parent["raw_text"],
            payload_json,
            payload_json,
            PARSED_PENDING_CONFIRMATION,
            source_parser_output_id,
        ),
    )
    if cursor.lastrowid is None:
        raise HumanRevisionLineageError("human revision child insert returned no identity")
    child_id = int(cursor.lastrowid)
    evidence_reference = _canonical_json(
        {
            "draft_content_hash": draft_content_hash,
            "draft_public_id": draft_public_id,
            "draft_version": draft_version,
            "human_reply_evidence_public_id": human_reply_evidence_public_id,
            "operation_public_id": operation_public_id,
            "superseded_proposal_public_id": parent["public_id"],
        }
    )
    conn.executemany(
        """
        INSERT INTO parser_proposal_field_evidence (
            parser_output_id, field_name, proposed_value, confidence_score,
            evidence_source_type, evidence_reference, notes
        ) VALUES (?, ?, ?, NULL, 'user_message', ?, NULL)
        """,
        (
            (
                child_id,
                field,
                None if canonical_payload.get(field) is None else str(canonical_payload[field]),
                evidence_reference,
            )
            for field in material
        ),
    )
    child_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
    return PublishedDraft(child_id, proposal_public_id, 0, child_hash)


def finalize_human_revision_in_transaction(
    conn: sqlite3.Connection,
    *,
    published: PublishedDraft,
    context: Mapping[str, object],
) -> None:
    """Seal the publication edge before superseding and moving the intake pointer."""
    if not conn.in_transaction:
        raise HumanRevisionLineageError("human revision finalizer requires a transaction")
    source_parser_output_id = _context_int(context, "source_parser_output_id")
    parent = _row(conn, source_parser_output_id)
    child = _row(conn, published.parser_output_id)
    if child["parent_parser_output_id"] != source_parser_output_id:
        raise HumanRevisionLineageError("human revision child/parent binding is invalid")
    publication = conn.execute(
        """
        SELECT publications.parser_output_id, publications.proposal_public_id,
               publications.proposal_version, publications.proposal_content_hash,
               operations.operation_public_id
        FROM parser_human_draft_publications AS publications
        JOIN parser_human_draft_operations AS operations
          ON operations.id = publications.operation_id
        WHERE publications.parser_output_id = ?
        """,
        (published.parser_output_id,),
    ).fetchone()
    if (
        publication is None
        or publication["proposal_public_id"] != published.proposal_public_id
        or int(publication["proposal_version"]) != published.proposal_version
        or publication["proposal_content_hash"] != published.proposal_content_hash
        or publication["operation_public_id"] != context["operation_public_id"]
    ):
        raise HumanRevisionLineageError("human revision publication edge is not sealed")
    if parent["parse_status"] == SUPERSEDED:
        current_intakes = conn.execute(
            "SELECT * FROM raw_intake_records WHERE parser_output_id = ? ORDER BY id",
            (published.parser_output_id,),
        ).fetchall()
        if len(current_intakes) != 1:
            raise HumanRevisionLineageError(
                "finalized human revision raw-intake pointer is invalid"
            )
        current_intake = current_intakes[0]
        if (
            current_intake["public_id"] != child["source_public_id"]
            or current_intake["attachment_id"] != child["attachment_id"]
        ):
            raise HumanRevisionLineageError("finalized human revision source binding is invalid")
        return
    intake = _require_current_intake(conn, parent)
    current_hash = compute_effective_proposal_content_hash(conn, parent)
    if not hmac.compare_digest(current_hash, str(context["expected_content_hash"])):
        raise HumanRevisionLineageError("human revision source changed before finalization")
    validate_transition(str(parent["parse_status"]), SUPERSEDED)
    ParserProposalRepository(conn).update_status(source_parser_output_id, SUPERSEDED)
    operation_public_id = str(context["operation_public_id"])
    draft_public_id = str(context["draft_public_id"])
    draft_content_hash = str(context["draft_content_hash"])
    draft_version = _context_int(context, "draft_version")
    actor = str(context["authenticated_actor_id"])
    evidence_public_id = str(context["human_reply_evidence_public_id"])
    changed_fields = list(_context_fields(context))
    timestamp = _context_int(context, "timestamp")
    event_payload = _canonical_json(
        {
            "changed_fields": changed_fields,
            "draft_content_hash": draft_content_hash,
            "draft_public_id": draft_public_id,
            "draft_version": draft_version,
            "human_reply_evidence_public_id": evidence_public_id,
            "operation_public_id": operation_public_id,
            "previous_content_hash": current_hash,
            "replacement_content_hash": published.proposal_content_hash,
            "replacement_proposal_public_id": published.proposal_public_id,
        }
    )
    created_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO parser_proposal_events (
            parser_output_id, from_status, to_status, event_type, event_reason,
            actor_type, actor_identifier, event_payload, created_at
        ) VALUES (?, ?, ?, 'superseded', ?, 'user', ?, ?, ?)
        """,
        (
            source_parser_output_id,
            parent["parse_status"],
            SUPERSEDED,
            f"D1 human revision {operation_public_id}",
            actor,
            event_payload,
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO parser_proposal_events (
            parser_output_id, from_status, to_status, event_type, event_reason,
            actor_type, actor_identifier, event_payload, created_at
        ) VALUES (?, NULL, ?, 'created', ?, 'user', ?, ?, ?)
        """,
        (
            published.parser_output_id,
            PARSED_PENDING_CONFIRMATION,
            f"D1 human revision {operation_public_id}",
            actor,
            event_payload,
            created_at,
        ),
    )
    pointer = conn.execute(
        """
        UPDATE raw_intake_records
        SET parser_output_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND parser_output_id = ?
        """,
        (
            published.parser_output_id,
            PARSED_PENDING_CONFIRMATION,
            intake["id"],
            source_parser_output_id,
        ),
    )
    if pointer.rowcount != 1:
        raise HumanRevisionLineageError("human revision raw-intake pointer race")

    event_type = "parser_proposal_human_revision"
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=str(parent["public_id"]),
        event_type=event_type,
        causation_public_id=operation_public_id,
    )
    source_references = [
        f"parser-output:{parent['public_id']}",
        f"human-reply:{evidence_public_id}",
    ]
    if parent["source_public_id"]:
        source_references.append(f"source:{parent['source_public_id']}")
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=audit_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=str(parent["public_id"]),
            event_type=event_type,
            event_payload=json.loads(event_payload),
            previous_state={
                "conversion_status": "not_converted",
                "parse_status": parent["parse_status"],
                "proposal_content_hash": current_hash,
                "raw_intake_status": raw_intake_status_for_proposal_status(
                    str(parent["parse_status"])
                ),
            },
            new_state={
                "conversion_status": "not_converted",
                "parse_status": SUPERSEDED,
                "proposal_content_hash": current_hash,
                "raw_intake_status": PARSED_PENDING_CONFIRMATION,
                "replacement_content_hash": published.proposal_content_hash,
                "replacement_proposal_public_id": published.proposal_public_id,
            },
            actor_type="human",
            actor_public_id=actor,
            source_evidence_references=tuple(source_references),
            correlation_public_id=draft_public_id,
            causation_public_id=operation_public_id,
            created_at=created_at,
        ),
    )


def publish_human_revision_in_transaction(
    conn: sqlite3.Connection,
    canonical_payload: dict[str, object],
    context: dict[str, object],
) -> PublishedDraft:
    """D1 draft callback; dispatch is intentionally internal and bounded."""
    source_parser_output_id = _context_int(context, "source_parser_output_id")
    source = _row(conn, source_parser_output_id)
    source_type = str(source["source_type"])
    if source_type in _TEXT_SOURCE_TYPES:
        return publish_text_human_revision_in_transaction(
            conn,
            source_parser_output_id=source_parser_output_id,
            draft_public_id=str(context["draft_public_id"]),
            draft_version=_context_int(context, "draft_version"),
            draft_content_hash=str(context["draft_content_hash"]),
            canonical_payload=canonical_payload,
            changed_fields=_context_fields(context),
            authenticated_actor_id=str(context["authenticated_actor_id"]),
            operation_public_id=str(context["operation_public_id"]),
            expected_content_hash=str(context["expected_content_hash"]),
            human_reply_evidence_public_id=str(context["human_reply_evidence_public_id"]),
            timestamp=_context_int(context, "timestamp"),
        )
    if source_type in _RECEIPT_SOURCE_TYPES:
        return publish_receipt_human_revision_in_transaction(
            conn,
            material=_receipt_revision_material(canonical_payload, context),
        )
    raise HumanRevisionLineageError("human revision source type is unsupported")


def _verify_publication_lifecycle_and_audit(
    conn: sqlite3.Connection,
    *,
    record: Mapping[str, object],
    parent: Mapping[str, object],
    child: Mapping[str, object],
    parent_hash: str,
    child_hash: str,
    changed_fields: tuple[str, ...],
) -> None:
    operation_public_id = str(record["operation_public_id"])
    actor = str(record["authenticated_actor_id"])
    published_at = record["published_at"]
    draft_version = record["draft_version"]
    if (
        not isinstance(published_at, int)
        or isinstance(published_at, bool)
        or not isinstance(draft_version, int)
        or isinstance(draft_version, bool)
    ):
        raise HumanRevisionLineageError("human revision publication time is invalid")
    created_at = datetime.fromtimestamp(published_at, timezone.utc).isoformat()
    event_payload = {
        "changed_fields": list(changed_fields),
        "draft_content_hash": record["draft_content_hash"],
        "draft_public_id": record["draft_public_id"],
        "draft_version": draft_version,
        "human_reply_evidence_public_id": record["evidence_public_id"],
        "operation_public_id": operation_public_id,
        "previous_content_hash": parent_hash,
        "replacement_content_hash": child_hash,
        "replacement_proposal_public_id": child["public_id"],
    }
    reason = f"D1 human revision {operation_public_id}"
    parent_events = conn.execute(
        "SELECT * FROM parser_proposal_events "
        "WHERE parser_output_id = ? AND event_type = 'superseded' "
        "AND event_reason = ? ORDER BY id",
        (parent["id"], reason),
    ).fetchall()
    child_events = conn.execute(
        "SELECT * FROM parser_proposal_events "
        "WHERE parser_output_id = ? AND event_type = 'created' "
        "AND event_reason = ? ORDER BY id",
        (child["id"], reason),
    ).fetchall()
    if len(parent_events) != 1 or len(child_events) != 1:
        raise HumanRevisionLineageError("human revision lifecycle evidence is missing")
    parent_event = parent_events[0]
    child_event = child_events[0]
    from_status = parent_event["from_status"]
    if (
        from_status not in {"parsed_pending_confirmation", "edited_pending_confirmation"}
        or parent_event["to_status"] != SUPERSEDED
        or child_event["from_status"] is not None
        or child_event["to_status"] != PARSED_PENDING_CONFIRMATION
        or parent_event["actor_type"] != "user"
        or child_event["actor_type"] != "user"
        or parent_event["actor_identifier"] != actor
        or child_event["actor_identifier"] != actor
        or parent_event["created_at"] != created_at
        or child_event["created_at"] != created_at
    ):
        raise HumanRevisionLineageError("human revision lifecycle evidence is invalid")
    try:
        if (
            json.loads(parent_event["event_payload"]) != event_payload
            or json.loads(child_event["event_payload"]) != event_payload
        ):
            raise HumanRevisionLineageError("human revision lifecycle evidence is invalid")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HumanRevisionLineageError("human revision lifecycle evidence is invalid") from exc

    aggregate_public_id = str(parent["public_id"])
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_public_id,
        event_type="parser_proposal_human_revision",
        causation_public_id=operation_public_id,
    )
    audit = conn.execute(
        "SELECT * FROM financial_audit_events WHERE event_public_id = ?",
        (audit_id,),
    ).fetchone()
    if audit is None:
        raise HumanRevisionLineageError("human revision audit evidence is missing")
    source_references = [
        f"parser-output:{parent['public_id']}",
        f"human-reply:{record['evidence_public_id']}",
    ]
    if parent["source_public_id"]:
        source_references.append(f"source:{parent['source_public_id']}")
    source_references.sort()
    audit_created_at = (
        datetime.fromtimestamp(published_at, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    expected_previous = {
        "conversion_status": "not_converted",
        "parse_status": from_status,
        "proposal_content_hash": parent_hash,
        "raw_intake_status": raw_intake_status_for_proposal_status(str(from_status)),
    }
    expected_new = {
        "conversion_status": "not_converted",
        "parse_status": SUPERSEDED,
        "proposal_content_hash": parent_hash,
        "raw_intake_status": PARSED_PENDING_CONFIRMATION,
        "replacement_content_hash": child_hash,
        "replacement_proposal_public_id": child["public_id"],
    }
    wrapped_event_payload = {
        "contract_version": "finance-canonical-json-v1",
        "value": event_payload,
    }
    wrapped_previous = {
        "contract_version": "finance-canonical-json-v1",
        "value": expected_previous,
    }
    wrapped_new = {
        "contract_version": "finance-canonical-json-v1",
        "value": expected_new,
    }
    try:
        audit_valid = (
            audit["aggregate_type"] == "parser_proposal"
            and audit["aggregate_public_id"] == aggregate_public_id
            and audit["event_type"] == "parser_proposal_human_revision"
            and json.loads(audit["event_payload_json"]) == wrapped_event_payload
            and json.loads(audit["previous_state_json"]) == wrapped_previous
            and json.loads(audit["new_state_json"]) == wrapped_new
            and json.loads(audit["source_evidence_refs_json"]) == source_references
            and audit["actor_type"] == "human"
            and audit["actor_public_id"] == actor
            and audit["authorization_public_id"] is None
            and audit["calculation_snapshot_public_id"] is None
            and audit["calculation_snapshot_hash"] is None
            and audit["correlation_public_id"] == record["draft_public_id"]
            and audit["causation_public_id"] == operation_public_id
            and audit["created_at"] == audit_created_at
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise HumanRevisionLineageError("human revision audit evidence is invalid") from exc
    chain = verify_financial_audit_chain(
        conn,
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_public_id,
    )
    if not audit_valid or not chain.valid or chain.legacy_without_chain:
        raise HumanRevisionLineageError("human revision audit evidence is invalid")


def _verify_one_publication_edge(
    conn: sqlite3.Connection,
    child: Mapping[str, object],
    *,
    expected_hash: str,
    expected_version: int,
) -> tuple[dict[str, Any], str]:
    child_id = child.get("id")
    if not isinstance(child_id, int) or isinstance(child_id, bool):
        raise HumanRevisionLineageError("human revision child identity is invalid")
    record = conn.execute(
        """
        SELECT publications.*, operations.operation_public_id,
               operations.operation_type, operations.operation_outcome,
               operations.result_completeness,
               operations.request_material_hash,
               operations.human_reply_evidence_id,
               operations.before_draft_version,
               operations.before_draft_content_hash,
               operations.after_draft_version,
               operations.after_draft_content_hash,
               operations.canonical_supplied_fields_json,
               operations.material_changes_json,
               operations.explicit_clears_json,
               operations.reason_policy_version,
               operations.reason_contributors_before_json,
               operations.reason_contributors_before_hash,
               operations.reason_contributors_after_json,
               operations.reason_contributors_after_hash,
               operations.unresolved_flags_json,
               operations.authenticated_actor_id,
               operations.telegram_account_id,
               operations.telegram_conversation_id,
               operations.conversation_binding_id,
               operations.telegram_message_id,
               operations.result_card_generation_public_id,
               operations.correction_channel,
               drafts.draft_public_id,
               drafts.authenticated_actor_id AS draft_actor_id,
               drafts.telegram_account_id AS draft_account_id,
               drafts.telegram_conversation_id AS draft_conversation_id,
               drafts.conversation_binding_id AS draft_binding_id,
               evidence.evidence_public_id, evidence.raw_utf8, evidence.encoding,
               evidence.format_version, evidence.byte_length, evidence.sha256,
               evidence.authenticated_actor_id AS evidence_actor_id,
               evidence.telegram_account_id AS evidence_account_id,
               evidence.telegram_conversation_id AS evidence_conversation_id,
               evidence.conversation_binding_id AS evidence_binding_id,
               evidence.telegram_message_id AS evidence_message_id
        FROM parser_human_draft_publications AS publications
        JOIN parser_human_draft_operations AS operations
          ON operations.id = publications.operation_id
         AND operations.draft_id = publications.draft_id
        JOIN parser_human_drafts AS drafts ON drafts.id = publications.draft_id
        JOIN parser_human_draft_reply_evidence AS evidence
          ON evidence.id = operations.human_reply_evidence_id
        WHERE publications.parser_output_id = ?
        """,
        (child_id,),
    ).fetchone()
    if record is None:
        raise HumanRevisionLineageError("human revision publication edge is missing")
    published_at = record["published_at"]
    if not isinstance(published_at, int) or isinstance(published_at, bool):
        raise HumanRevisionLineageError("human revision publication time is invalid")
    expected_created_at = datetime.fromtimestamp(published_at, timezone.utc).isoformat()
    parent_id = child.get("parent_parser_output_id")
    if not isinstance(parent_id, int) or isinstance(parent_id, bool):
        raise HumanRevisionLineageError("human revision parent identity is invalid")
    parent = _row(conn, parent_id)
    child_row = dict(child)
    actual_payload, _completion_id, actual_version = resolve_effective_payload(conn, child_row)
    parent_payload, _parent_completion_id, _parent_version = resolve_effective_payload(conn, parent)
    actual_hash = compute_effective_proposal_content_hash(conn, child_row)
    if (
        record["proposal_public_id"] != child["public_id"]
        or int(record["proposal_version"]) != expected_version
        or record["proposal_content_hash"] != expected_hash
        or actual_version != expected_version
        or not hmac.compare_digest(actual_hash, expected_hash)
        or record["operation_type"] != "accepted"
        or record["operation_outcome"] != "accepted"
        or record["result_completeness"] != "complete"
        or int(record["draft_version"]) != int(record["after_draft_version"])
        or record["draft_content_hash"] != record["after_draft_content_hash"]
        or record["reason_policy_version"] != "d1-reason-policy-v1"
        or record["authenticated_actor_id"] != record["draft_actor_id"]
        or record["telegram_account_id"] != record["draft_account_id"]
        or record["telegram_conversation_id"] != record["draft_conversation_id"]
        or record["conversation_binding_id"] != record["draft_binding_id"]
    ):
        raise HumanRevisionLineageError("human revision publication binding is invalid")
    raw = record["raw_utf8"]
    if (
        not isinstance(raw, bytes)
        or record["encoding"] != "UTF-8"
        or record["format_version"] != "d1-human-reply-v1"
        or len(raw) != int(record["byte_length"])
        or _sha256_bytes(raw) != record["sha256"]
        or record["authenticated_actor_id"] != record["evidence_actor_id"]
        or record["telegram_account_id"] != record["evidence_account_id"]
        or record["telegram_conversation_id"] != record["evidence_conversation_id"]
        or record["conversation_binding_id"] != record["evidence_binding_id"]
        or int(record["telegram_message_id"]) != int(record["evidence_message_id"])
    ):
        raise HumanRevisionLineageError("human revision exact reply evidence is invalid")
    try:
        reply_text = raw.decode("utf-8")
        from finance_core.parser_proposals.human_drafts import (
            HumanDraftCommand,
            _contributors_from_json,
            _contributors_material,
            _draft_hash,
            _parse_card_fields,
            _request_material,
            _validate_human_draft_adapter,
        )

        card_reference, raw_fields = _parse_card_fields(reply_text)
        supplied = json.loads(record["canonical_supplied_fields_json"])
        material_changes = json.loads(record["material_changes_json"])
        explicit_clears = json.loads(record["explicit_clears_json"])
        before = _contributors_from_json(record["reason_contributors_before_json"])
        after = _contributors_from_json(record["reason_contributors_after_json"])
        unresolved = json.loads(record["unresolved_flags_json"])
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HumanRevisionLineageError(
            "human revision operation evidence cannot be reconstructed"
        ) from exc
    predecessor = conn.execute(
        """
        SELECT predecessor.card_generation_public_id
        FROM parser_human_draft_cards AS result
        JOIN parser_human_draft_cards AS predecessor ON predecessor.id = result.predecessor_card_id
        WHERE result.card_generation_public_id = ?
        """,
        (record["result_card_generation_public_id"],),
    ).fetchone()
    if predecessor is None or card_reference != predecessor[0] or raw_fields != supplied:
        raise HumanRevisionLineageError("human revision reply/card binding is invalid")
    reconstructed = HumanDraftCommand(
        card_generation_public_id=card_reference,
        telegram_message_id=int(record["telegram_message_id"]),
        operation_public_id=str(record["operation_public_id"]),
        authenticated_actor_id=str(record["authenticated_actor_id"]),
        telegram_account_id=str(record["telegram_account_id"]),
        telegram_conversation_id=str(record["telegram_conversation_id"]),
        conversation_binding_id=str(record["conversation_binding_id"]),
        raw_card_text=reply_text,
        field_values=raw_fields,
    )
    if not hmac.compare_digest(
        _request_material(reconstructed, str(record["sha256"])),
        str(record["request_material_hash"]),
    ):
        raise HumanRevisionLineageError("human revision request material is invalid")
    before_json, before_hash = _contributors_material(before)
    after_json, after_hash = _contributors_material(after)
    if (
        before_json != record["reason_contributors_before_json"]
        or before_hash != record["reason_contributors_before_hash"]
        or after_json != record["reason_contributors_after_json"]
        or after_hash != record["reason_contributors_after_hash"]
    ):
        raise HumanRevisionLineageError("human revision contributor evidence is invalid")
    try:
        validated = _validate_human_draft_adapter(
            parent_payload,
            raw_fields,
            source_type=str(parent["source_type"]),
            reason_contributors=before,
            operation_public_id=str(record["operation_public_id"]),
        )
    except Exception as exc:
        raise HumanRevisionLineageError("human revision operation no longer validates") from exc
    draft_base = parent
    seen_draft_base_ids: set[int] = set()
    while (
        conn.execute(
            "SELECT 1 FROM parser_human_draft_publications WHERE parser_output_id = ?",
            (draft_base["id"],),
        ).fetchone()
        is not None
    ):
        draft_base_id = draft_base.get("id")
        if (
            not isinstance(draft_base_id, int)
            or isinstance(draft_base_id, bool)
            or draft_base_id in seen_draft_base_ids
        ):
            raise HumanRevisionLineageError("human revision parent chain contains a cycle")
        seen_draft_base_ids.add(draft_base_id)
        draft_base_parent_id = draft_base.get("parent_parser_output_id")
        if not isinstance(draft_base_parent_id, int) or isinstance(draft_base_parent_id, bool):
            raise HumanRevisionLineageError("human revision draft root is invalid")
        draft_base = _row(conn, draft_base_parent_id)
    if (
        draft_base["parser_name"] == "human_revision"
        or draft_base["parser_version"] == "d1-human-revision-v1"
    ):
        raise HumanRevisionLineageError("human revision publication edge is missing")
    draft_base_payload, _draft_base_completion_id, _draft_base_version = resolve_effective_payload(
        conn, draft_base
    )
    try:
        draft_validated = _validate_human_draft_adapter(
            draft_base_payload,
            raw_fields,
            source_type=str(draft_base["source_type"]),
            reason_contributors=before,
            operation_public_id=str(record["operation_public_id"]),
        )
    except Exception as exc:
        raise HumanRevisionLineageError("human revision draft no longer validates") from exc
    validated_after_json, _validated_after_hash = _contributors_material(
        tuple(validated.reason_contributors)
    )
    expected_correction_id = (
        "rcor_d1_" + hashlib.sha256(str(record["operation_public_id"]).encode()).hexdigest()[:32]
    )
    expected_published_payload = validated.canonical_payload
    if str(child["source_type"]) in _RECEIPT_SOURCE_TYPES:
        from finance_core.parser_proposals.receipt_supersession import ReceiptRevisionMaterial

        expected_published_payload = _build_d1_receipt_payload(
            conn,
            parent=parent,
            effective_payload=parent_payload,
            applied_updates={
                field: validated.canonical_payload.get(field) for field in validated.changed_fields
            },
            material=ReceiptRevisionMaterial(
                source_parser_output_id=int(parent["id"]),
                expected_content_hash=compute_effective_proposal_content_hash(conn, parent),
                canonical_supplied_fields=dict(supplied),
                canonical_payload=dict(validated.canonical_payload),
                changed_fields=tuple(validated.changed_fields),
                authenticated_actor_id=str(record["authenticated_actor_id"]),
                correction_public_id=expected_correction_id,
                correction_channel=str(record["correction_channel"]),
                reason=None,
                timestamp="",
                d1_operation_public_id=str(record["operation_public_id"]),
                d1_draft_public_id=str(record["draft_public_id"]),
                d1_draft_version=int(record["draft_version"]),
                d1_draft_content_hash=str(record["draft_content_hash"]),
                human_reply_evidence_public_id=str(record["evidence_public_id"]),
                explicit_clears={key: tuple(value) for key, value in explicit_clears.items()},
                reason_contributors=tuple(after),
            ),
        )
    expected_changes = {
        field: [parent_payload.get(field), validated.canonical_payload.get(field)]
        for field in sorted(validated.changed_fields)
    }
    if (
        validated.completeness != "publishable"
        or expected_published_payload != actual_payload
        or tuple(sorted(validated.changed_fields)) != tuple(sorted(material_changes))
        or material_changes != expected_changes
        or dict(validated.explicit_clears)
        != {key: tuple(value) for key, value in explicit_clears.items()}
        or validated_after_json != after_json
        or sorted(validated.unresolved_flags) != unresolved
        or _draft_hash(draft_validated.canonical_payload, after) != record["draft_content_hash"]
    ):
        raise HumanRevisionLineageError("human revision operation lineage is invalid")
    for key in ("source_type", "source_public_id", "statement_batch_id", "attachment_id"):
        if child[key] != parent[key]:
            raise HumanRevisionLineageError("human revision source binding is invalid")
    if child["raw_text"] != parent["raw_text"] or parent["parse_status"] != SUPERSEDED:
        raise HumanRevisionLineageError("human revision source evidence was not preserved")
    if (
        child["parser_name"] != "human_revision"
        or child["parser_version"] != "d1-human-revision-v1"
        or child["ai_provider"] is not None
        or child["ai_model"] is not None
        or child["prompt_version"] is not None
    ):
        raise HumanRevisionLineageError("human revision child has forged AI attribution")
    children = conn.execute(
        "SELECT id FROM parser_outputs WHERE parent_parser_output_id = ? ORDER BY id",
        (parent["id"],),
    ).fetchall()
    if [int(row["id"]) for row in children] != [child_id]:
        raise HumanRevisionLineageError("human revision lineage forked")
    references = conn.execute(
        """
        SELECT field_name, proposed_value, confidence_score, evidence_reference
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ? AND evidence_source_type = 'user_message'
        ORDER BY field_name
        """,
        (child_id,),
    ).fetchall()
    current_references = []
    for reference in references:
        try:
            decoded = json.loads(reference["evidence_reference"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HumanRevisionLineageError("human revision field evidence is invalid") from exc
        if (
            decoded.get("operation_public_id") == record["operation_public_id"]
            or decoded.get("correction_public_id") == expected_correction_id
        ):
            current_references.append(reference)
            expected_value = expected_published_payload.get(reference["field_name"])
            if (
                reference["proposed_value"]
                != (None if expected_value is None else str(expected_value))
                or reference["confidence_score"] is not None
                or decoded.get("human_reply_evidence_public_id") != record["evidence_public_id"]
                or decoded.get("draft_public_id") != record["draft_public_id"]
                or decoded.get("draft_version") != record["draft_version"]
                or decoded.get("draft_content_hash") != record["draft_content_hash"]
                or decoded.get("superseded_proposal_public_id") != parent["public_id"]
            ):
                raise HumanRevisionLineageError("human revision field evidence is invalid")
    if [row["field_name"] for row in current_references] != sorted(validated.changed_fields):
        raise HumanRevisionLineageError("human revision field evidence is incomplete")
    if str(child["source_type"]) in _RECEIPT_SOURCE_TYPES:
        _verify_complete_receipt_relational_evidence(
            conn,
            child_id=child_id,
            payload=actual_payload,
        )
        parent_links = conn.execute(
            """
            SELECT extraction_id, parser_contract_version, link_role
            FROM receipt_ocr_proposal_links WHERE parser_output_id = ?
            """,
            (parent["id"],),
        ).fetchall()
        child_links = conn.execute(
            """
            SELECT public_id, extraction_id, parser_contract_version, link_role,
                   proposal_input_hash, proposal_result_hash, created_at
            FROM receipt_ocr_proposal_links WHERE parser_output_id = ?
            """,
            (child_id,),
        ).fetchall()
        monetary_change = bool({"amount", "currency"} & set(validated.changed_fields))
        expected_payload_json = _canonical_json(actual_payload)
        expected_result_hash = _sha256_bytes(expected_payload_json.encode("utf-8"))
        revision = conn.execute(
            "SELECT * FROM receipt_proposal_revisions WHERE replacement_parser_output_id = ?",
            (child_id,),
        ).fetchall()
        if monetary_change:
            from finance_core.parser_proposals.receipt_supersession import (
                _correction_input_hash,
                _derive_link_public_id,
                _derive_replacement_public_id,
            )

            expected_updates = {
                field: actual_payload.get(field) for field in validated.changed_fields
            }
            expected_input_hash = _correction_input_hash(
                expected_correction_id,
                parent,
                compute_effective_proposal_content_hash(conn, parent),
                expected_updates,
                str(record["authenticated_actor_id"]),
                str(record["correction_channel"]),
            )
            if len(revision) != 1:
                raise HumanRevisionLineageError(
                    "human receipt monetary revision evidence is missing"
                )
            revision_row = revision[0]
            try:
                persisted_updates = json.loads(revision_row["field_updates_json"])
                persisted_applied = json.loads(revision_row["applied_field_updates_json"])
                persisted_payload = json.loads(revision_row["replacement_payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise HumanRevisionLineageError(
                    "human receipt monetary revision evidence is invalid"
                ) from exc
            if (
                revision_row["correction_public_id"] != expected_correction_id
                or int(revision_row["superseded_parser_output_id"]) != int(parent["id"])
                or revision_row["superseded_content_hash"]
                != compute_effective_proposal_content_hash(conn, parent)
                or revision_row["replacement_content_hash"] != actual_hash
                or persisted_updates != expected_updates
                or persisted_applied != expected_updates
                or persisted_payload != actual_payload
                or revision_row["actor_type"] != "human"
                or revision_row["authenticated_actor_id"] != record["authenticated_actor_id"]
                or revision_row["correction_channel"] != record["correction_channel"]
                or child["public_id"] != _derive_replacement_public_id(expected_correction_id)
                or revision_row["created_at"] != expected_created_at
            ):
                raise HumanRevisionLineageError(
                    "human receipt monetary revision evidence is invalid"
                )
            expected_link_id = _derive_link_public_id(expected_correction_id)
        else:
            if revision:
                raise HumanRevisionLineageError(
                    "human receipt non-monetary revision fabricated monetary evidence"
                )
            expected_input_hash = _sha256_bytes(
                str(current_references[0]["evidence_reference"]).encode("utf-8")
            )
            expected_link_id = _derived_id(
                "ropl_d1_", "d1-human-receipt-link-v1", expected_correction_id
            )
        if (
            len(parent_links) != 1
            or len(child_links) != 1
            or child_links[0]["extraction_id"] != parent_links[0]["extraction_id"]
            or child_links[0]["parser_contract_version"]
            != parent_links[0]["parser_contract_version"]
            or child_links[0]["link_role"] != "superseding_correction"
            or child_links[0]["public_id"] != expected_link_id
            or child_links[0]["created_at"] != expected_created_at
            or not hmac.compare_digest(
                str(child_links[0]["proposal_input_hash"]), expected_input_hash
            )
            or not hmac.compare_digest(
                str(child_links[0]["proposal_result_hash"]), expected_result_hash
            )
        ):
            raise HumanRevisionLineageError("human receipt OCR publication link is invalid")
    _verify_publication_lifecycle_and_audit(
        conn,
        record=record,
        parent=parent,
        child=child,
        parent_hash=compute_effective_proposal_content_hash(conn, parent),
        child_hash=actual_hash,
        changed_fields=tuple(sorted(validated.changed_fields)),
    )
    return parent, str(record["operation_public_id"])


def _expected_receipt_relational_evidence(
    payload: Mapping[str, object],
) -> list[tuple[object, ...]]:
    """Project every embedded receipt evidence item into its relational mirror."""
    from finance_core.parser_proposals.receipt_supersession import (
        _canonical_json as _receipt_canonical_json,
    )
    from finance_core.parser_proposals.receipt_supersession import (
        _correction_evidence_reference,
        _has_completion_provenance,
        _has_correction_provenance,
        _scalar_text,
    )

    evidence_items = payload.get("field_evidence")
    if not isinstance(evidence_items, list):
        raise HumanRevisionLineageError("receipt revision field evidence is invalid")
    expected: list[tuple[object, ...]] = []
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        source_type = item.get("evidence_source_type")
        if source_type == "user_message":
            if _has_correction_provenance(item):
                reference = _receipt_canonical_json(_correction_evidence_reference(item))
            elif _has_completion_provenance(item):
                reference = _receipt_canonical_json(
                    {
                        "completion_public_id": item["completion_public_id"],
                        "completion_version": item["completion_version"],
                        "authenticated_actor_id": item["authenticated_actor_id"],
                        "source_proposal_public_id": item.get("source_proposal_public_id"),
                        "completed_content_hash": item.get("completed_content_hash"),
                    }
                )
            else:
                raise HumanRevisionLineageError("receipt revision human evidence is invalid")
            notes = None
        else:
            reference = _receipt_canonical_json(
                {
                    "extraction_public_id": item.get("extraction_public_id"),
                    "normalized_result_hash": item.get("normalized_result_hash"),
                    "block_sequence_indexes": item.get("block_sequence_indexes"),
                }
            )
            notes = item.get("excerpt")
        expected.append(
            (
                item.get("field_name"),
                _scalar_text(item.get("proposed_value")),
                item.get("confidence"),
                source_type,
                reference,
                notes,
            )
        )
    return expected


def _verify_complete_receipt_relational_evidence(
    conn: sqlite3.Connection,
    *,
    child_id: int,
    payload: Mapping[str, object],
) -> None:
    actual = conn.execute(
        "SELECT field_name, proposed_value, confidence_score, evidence_source_type, "
        "evidence_reference, notes FROM parser_proposal_field_evidence "
        "WHERE parser_output_id = ? ORDER BY id",
        (child_id,),
    ).fetchall()
    if [tuple(row) for row in actual] != _expected_receipt_relational_evidence(payload):
        raise HumanRevisionLineageError("receipt revision relational evidence is invalid")


def _verify_public_receipt_revision_edge(
    conn: sqlite3.Connection,
    child: Mapping[str, object],
    *,
    expected_hash: str,
    expected_version: int,
) -> dict[str, Any] | None:
    """Verify one public monetary edge that follows a D1 receipt child."""
    child_id = child.get("id")
    if not isinstance(child_id, int) or isinstance(child_id, bool):
        raise HumanRevisionLineageError("receipt revision child identity is invalid")
    revisions = conn.execute(
        "SELECT * FROM receipt_proposal_revisions WHERE replacement_parser_output_id = ?",
        (child_id,),
    ).fetchall()
    if not revisions:
        return None
    if len(revisions) != 1:
        raise HumanRevisionLineageError("receipt revision edge is ambiguous")
    revision = revisions[0]
    parent_id = child.get("parent_parser_output_id")
    if (
        not isinstance(parent_id, int)
        or isinstance(parent_id, bool)
        or int(revision["superseded_parser_output_id"]) != parent_id
    ):
        raise HumanRevisionLineageError("receipt revision topology is invalid")
    parent = _row(conn, parent_id)
    parent_payload, _parent_completion_id, _parent_version = resolve_effective_payload(conn, parent)
    child_payload, _child_completion_id, child_version = resolve_effective_payload(
        conn, dict(child)
    )
    child_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
    parent_hash = compute_effective_proposal_content_hash(conn, parent)
    try:
        field_updates = json.loads(revision["field_updates_json"])
        applied_updates = json.loads(revision["applied_field_updates_json"])
        replacement_payload = json.loads(revision["replacement_payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise HumanRevisionLineageError("receipt revision material is invalid") from exc
    if not all(
        isinstance(value, dict) for value in (field_updates, applied_updates, replacement_payload)
    ):
        raise HumanRevisionLineageError("receipt revision material is invalid")

    from finance_core.parser_proposals.receipt_supersession import (
        _build_replacement_payload,
        _canonicalize_field_updates,
        _correction_input_hash,
        _derive_link_public_id,
        _latest_completion_provenance,
        _material_field_updates,
        _parser_source_references,
    )
    from finance_core.parser_proposals.receipt_supersession import (
        _canonical_json as _receipt_canonical_json,
    )

    try:
        canonical_updates, canonical_amount, canonical_currency = _canonicalize_field_updates(
            field_updates, parent_payload
        )
        material_updates = _material_field_updates(canonical_updates, parent_payload)
        rebuilt_payload = _build_replacement_payload(
            parent_payload,
            material_updates,
            parent,
            str(revision["correction_public_id"]),
            _latest_completion_provenance(conn, parent),
            canonical_amount=canonical_amount,
            canonical_currency=canonical_currency,
        )
    except Exception as exc:
        raise HumanRevisionLineageError("receipt revision no longer validates") from exc
    if (
        child_version != expected_version
        or not hmac.compare_digest(child_hash, expected_hash)
        or revision["superseded_content_hash"] != parent_hash
        or revision["replacement_content_hash"] != child_hash
        or canonical_updates != field_updates
        or material_updates != applied_updates
        or rebuilt_payload != child_payload
        or replacement_payload != child_payload
        or parent["parse_status"] != SUPERSEDED
        or child["parse_status"]
        not in {
            PARSED_PENDING_CONFIRMATION,
            SUPERSEDED,
            "edited_pending_confirmation",
            "confirmed",
        }
    ):
        raise HumanRevisionLineageError("receipt revision binding is invalid")
    for key in ("source_type", "source_public_id", "statement_batch_id", "attachment_id"):
        if child[key] != parent[key]:
            raise HumanRevisionLineageError("receipt revision source binding is invalid")
    if child["raw_text"] != parent["raw_text"]:
        raise HumanRevisionLineageError("receipt revision source evidence was not preserved")
    children = conn.execute(
        "SELECT id FROM parser_outputs WHERE parent_parser_output_id = ? ORDER BY id",
        (parent_id,),
    ).fetchall()
    if [int(row["id"]) for row in children] != [child_id]:
        raise HumanRevisionLineageError("receipt revision lineage forked")

    parent_links = conn.execute(
        "SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id = ? ORDER BY id",
        (parent_id,),
    ).fetchall()
    child_links = conn.execute(
        "SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id = ? ORDER BY id",
        (child_id,),
    ).fetchall()
    if len(parent_links) != 1 or len(child_links) != 1:
        raise HumanRevisionLineageError("receipt revision OCR link is invalid")
    link = child_links[0]
    revision_created_at = revision["created_at"]
    if not isinstance(revision_created_at, str):
        raise HumanRevisionLineageError("receipt revision timestamp is invalid")
    try:
        parsed_revision_time = datetime.fromisoformat(revision_created_at.replace("Z", "+00:00"))
        if parsed_revision_time.tzinfo is None or parsed_revision_time.utcoffset() is None:
            raise ValueError("timestamp has no timezone")
        expected_audit_created_at = (
            parsed_revision_time.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except ValueError as exc:
        raise HumanRevisionLineageError("receipt revision timestamp is invalid") from exc
    expected_input_hash = _correction_input_hash(
        str(revision["correction_public_id"]),
        parent,
        parent_hash,
        field_updates,
        str(revision["authenticated_actor_id"]),
        str(revision["correction_channel"]),
    )
    expected_result_hash = hashlib.sha256(
        _receipt_canonical_json(child_payload).encode("utf-8")
    ).hexdigest()
    if (
        link["extraction_id"] != parent_links[0]["extraction_id"]
        or link["public_id"] != _derive_link_public_id(str(revision["correction_public_id"]))
        or link["parser_contract_version"] != parent_links[0]["parser_contract_version"]
        or link["link_role"] != "superseding_correction"
        or link["created_at"] != revision_created_at
        or not hmac.compare_digest(str(link["proposal_input_hash"]), expected_input_hash)
        or not hmac.compare_digest(str(link["proposal_result_hash"]), expected_result_hash)
    ):
        raise HumanRevisionLineageError("receipt revision OCR link is invalid")

    _verify_complete_receipt_relational_evidence(
        conn,
        child_id=child_id,
        payload=child_payload,
    )

    correction_id = str(revision["correction_public_id"])
    parent_event_payload = {
        "actor_type": "human",
        "changed_fields": sorted(applied_updates),
        "correction_public_id": correction_id,
        "previous_content_hash": parent_hash,
        "replacement_content_hash": child_hash,
        "replacement_proposal_public_id": child["public_id"],
    }
    child_event_payload = {
        "actor_type": "human",
        "correction_public_id": correction_id,
        "superseded_proposal_public_id": parent["public_id"],
    }
    parent_events = conn.execute(
        "SELECT * FROM parser_proposal_events WHERE parser_output_id = ? "
        "AND event_type = 'superseded' AND event_reason = ? ORDER BY id",
        (parent_id, f"superseding correction {correction_id}"),
    ).fetchall()
    child_events = conn.execute(
        "SELECT * FROM parser_proposal_events WHERE parser_output_id = ? "
        "AND event_type = 'created' AND event_reason = ? ORDER BY id",
        (child_id, f"replacement for correction {correction_id}"),
    ).fetchall()
    try:
        lifecycle_valid = (
            len(parent_events) == 1
            and len(child_events) == 1
            and parent_events[0]["from_status"] == revision["superseded_from_status"]
            and parent_events[0]["to_status"] == SUPERSEDED
            and child_events[0]["from_status"] is None
            and child_events[0]["to_status"] == PARSED_PENDING_CONFIRMATION
            and parent_events[0]["actor_type"] == "user"
            and child_events[0]["actor_type"] == "user"
            and parent_events[0]["actor_identifier"] == revision["authenticated_actor_id"]
            and child_events[0]["actor_identifier"] == revision["authenticated_actor_id"]
            and parent_events[0]["created_at"] == revision_created_at
            and child_events[0]["created_at"] == revision_created_at
            and json.loads(parent_events[0]["event_payload"]) == parent_event_payload
            and json.loads(child_events[0]["event_payload"]) == child_event_payload
        )
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise HumanRevisionLineageError("receipt revision lifecycle evidence is invalid") from exc
    if not lifecycle_valid:
        raise HumanRevisionLineageError("receipt revision lifecycle evidence is invalid")

    aggregate_id = str(parent["public_id"])
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_id,
        event_type="receipt_proposal_superseded",
        causation_public_id=correction_id,
    )
    audit = conn.execute(
        "SELECT * FROM financial_audit_events WHERE event_public_id = ?", (audit_id,)
    ).fetchone()
    expected_audit_payload = {
        "changed_fields": sorted(applied_updates),
        "correction_public_id": correction_id,
        "previous_content_hash": parent_hash,
        "replacement_content_hash": child_hash,
        "replacement_proposal_public_id": child["public_id"],
    }
    expected_previous = {
        "conversion_status": "not_converted",
        "parse_status": revision["superseded_from_status"],
        "proposal_content_hash": parent_hash,
        "raw_intake_status": raw_intake_status_for_proposal_status(
            str(revision["superseded_from_status"])
        ),
    }
    expected_new = {
        "conversion_status": "not_converted",
        "parse_status": SUPERSEDED,
        "proposal_content_hash": parent_hash,
        "raw_intake_status": PARSED_PENDING_CONFIRMATION,
        "replacement_content_hash": child_hash,
        "replacement_proposal_public_id": child["public_id"],
    }
    wrapped = lambda value: {  # noqa: E731 - compact canonical audit comparison
        "contract_version": "finance-canonical-json-v1",
        "value": value,
    }
    try:
        audit_valid = (
            audit is not None
            and audit["aggregate_type"] == "parser_proposal"
            and audit["aggregate_public_id"] == aggregate_id
            and audit["event_type"] == "receipt_proposal_superseded"
            and json.loads(audit["event_payload_json"]) == wrapped(expected_audit_payload)
            and json.loads(audit["previous_state_json"]) == wrapped(expected_previous)
            and json.loads(audit["new_state_json"]) == wrapped(expected_new)
            and json.loads(audit["source_evidence_refs_json"])
            == sorted(_parser_source_references(parent))
            and audit["actor_type"] == "human"
            and audit["actor_public_id"] == revision["authenticated_actor_id"]
            and audit["authorization_public_id"] == correction_id
            and audit["calculation_snapshot_public_id"] is None
            and audit["calculation_snapshot_hash"] is None
            and audit["correlation_public_id"] == aggregate_id
            and audit["causation_public_id"] == correction_id
            and audit["created_at"] == expected_audit_created_at
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise HumanRevisionLineageError("receipt revision audit evidence is invalid") from exc
    audit_chain = verify_financial_audit_chain(
        conn, aggregate_type="parser_proposal", aggregate_public_id=aggregate_id
    )
    if not audit_valid or not audit_chain.valid or audit_chain.legacy_without_chain:
        raise HumanRevisionLineageError("receipt revision audit evidence is invalid")
    return parent


def _has_d1_publication_ancestor(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    """Detect an expected D1 edge without trusting mutable parser attribution."""
    current_id: int | None = parser_output_id
    seen: set[int] = set()
    while current_id is not None:
        if current_id in seen:
            raise HumanRevisionLineageError("human revision parent chain contains a cycle")
        seen.add(current_id)
        row = _row(conn, current_id)
        if (
            row["parser_name"] == "human_revision"
            or row["parser_version"] == "d1-human-revision-v1"
            or conn.execute(
                "SELECT 1 FROM parser_human_draft_publications WHERE parser_output_id = ? LIMIT 1",
                (current_id,),
            ).fetchone()
            is not None
            or conn.execute(
                "SELECT 1 FROM parser_human_draft_operations "
                "WHERE publication_parser_output_id = ? LIMIT 1",
                (current_id,),
            ).fetchone()
            is not None
            or conn.execute(
                "SELECT 1 FROM parser_proposal_events WHERE parser_output_id = ? "
                "AND event_reason LIKE 'D1 human revision %' LIMIT 1",
                (current_id,),
            ).fetchone()
            is not None
        ):
            return True
        parent_id = row.get("parent_parser_output_id")
        if parent_id is None:
            current_id = None
        elif isinstance(parent_id, int) and not isinstance(parent_id, bool):
            current_id = parent_id
        else:
            raise HumanRevisionLineageError("human revision parent identity is invalid")
    return False


def verify_human_revision_descendant(
    conn: sqlite3.Connection,
    proposal: Mapping[str, object],
    *,
    content_hash: str,
    proposal_version: int,
) -> dict[str, object] | None:
    """Verify every D1 edge back to a deterministic or sealed AI root."""
    try:
        current_id = proposal["id"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HumanRevisionLineageError("human revision projection has no identity") from exc
    if not isinstance(current_id, int) or isinstance(current_id, bool) or current_id < 1:
        raise HumanRevisionLineageError("human revision projection identity is invalid")
    current_projection = _row(conn, current_id)
    if not d1_publication_schema_available(conn, current_id):
        return None
    _require_acyclic_parent_chain(conn, current_id)
    direct = conn.execute(
        "SELECT 1 FROM parser_human_draft_publications WHERE parser_output_id = ?",
        (current_id,),
    ).fetchone()
    current = current_projection
    expected_hash = content_hash
    expected_version = proposal_version
    operations: list[str] = []
    if direct is None and _has_d1_publication_ancestor(conn, current_id):
        seen_receipt_revision_ids: set[int] = set()
        while direct is None:
            current_receipt_id = current.get("id")
            if (
                not isinstance(current_receipt_id, int)
                or isinstance(current_receipt_id, bool)
                or current_receipt_id in seen_receipt_revision_ids
            ):
                raise HumanRevisionLineageError("human revision parent chain contains a cycle")
            seen_receipt_revision_ids.add(current_receipt_id)
            parent = _verify_public_receipt_revision_edge(
                conn,
                current,
                expected_hash=expected_hash,
                expected_version=expected_version,
            )
            if parent is None:
                raise HumanRevisionLineageError("human revision publication edge is missing")
            current = parent
            expected_hash = compute_effective_proposal_content_hash(conn, current)
            _payload, _completion_id, expected_version = resolve_effective_payload(conn, current)
            direct = conn.execute(
                "SELECT 1 FROM parser_human_draft_publications WHERE parser_output_id = ?",
                (current["id"],),
            ).fetchone()
    elif direct is None:
        return None
    current_intakes = conn.execute(
        "SELECT * FROM raw_intake_records WHERE parser_output_id = ? ORDER BY id",
        (current_id,),
    ).fetchall()
    if len(current_intakes) != 1:
        raise HumanRevisionLineageError("human revision current raw-intake pointer is invalid")
    current_intake = current_intakes[0]
    if (
        current_intake["public_id"] != current_projection["source_public_id"]
        or current_intake["attachment_id"] != current_projection["attachment_id"]
        or current_intake["status"]
        != raw_intake_status_for_proposal_status(str(current_projection["parse_status"]))
    ):
        raise HumanRevisionLineageError("human revision current raw-intake pointer is invalid")
    seen_publication_ids: set[int] = set()
    while (
        conn.execute(
            "SELECT 1 FROM parser_human_draft_publications WHERE parser_output_id = ?",
            (current["id"],),
        ).fetchone()
        is not None
    ):
        current_publication_id = current.get("id")
        if (
            not isinstance(current_publication_id, int)
            or isinstance(current_publication_id, bool)
            or current_publication_id in seen_publication_ids
        ):
            raise HumanRevisionLineageError("human revision parent chain contains a cycle")
        seen_publication_ids.add(current_publication_id)
        current, operation_id = _verify_one_publication_edge(
            conn,
            current,
            expected_hash=expected_hash,
            expected_version=expected_version,
        )
        operations.append(operation_id)
        expected_hash = compute_effective_proposal_content_hash(conn, current)
        _payload, _completion_id, expected_version = resolve_effective_payload(conn, current)

    if (
        current["parser_name"] == "human_revision"
        or current["parser_version"] == "d1-human-revision-v1"
    ):
        raise HumanRevisionLineageError("human revision publication edge is missing")

    from finance_core.parser_proposals.ai_fallback import (
        AI_CONFIRMATION_CONFIDENCE_THRESHOLD,
        AiFallbackServiceError,
        verify_ai_fallback_child,
    )

    try:
        ai_root = verify_ai_fallback_child(
            conn,
            current,
            content_hash=expected_hash,
            proposal_version=expected_version,
            require_resolved=False,
            _skip_human=True,
            _human_descendant_parser_output_id=current_id,
        )
    except AiFallbackServiceError as exc:
        raise HumanRevisionLineageError("sealed AI ancestor does not verify") from exc
    confidence_requires_resolution = bool(
        ai_root is not None
        and (
            current["confidence_score"] is None
            or float(current["confidence_score"]) < AI_CONFIRMATION_CONFIDENCE_THRESHOLD
        )
    )
    return {
        "proposal_origin": "human_revision",
        "root_proposal_origin": "ai_fallback" if ai_root is not None else "deterministic",
        "ai_source_kind": None if ai_root is None else ai_root.get("ai_source_kind"),
        "ambiguity_flags": (),
        "requires_resolution": confidence_requires_resolution,
        "human_operation_ids": tuple(reversed(operations)),
    }


__all__ = [
    "d1_publication_schema_available",
    "finalize_human_revision_in_transaction",
    "HumanRevisionLineageError",
    "publish_human_revision_in_transaction",
    "publish_receipt_human_revision_in_transaction",
    "publish_receipt_nonmonetary_revision_in_transaction",
    "publish_text_human_revision_in_transaction",
    "verify_human_revision_descendant",
]
