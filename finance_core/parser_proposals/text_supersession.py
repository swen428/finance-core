"""Neutral text monetary supersession owned by one guarded transaction."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.parser_proposals.amendment_lineage import (
    TEXT_AMENDMENT_PARSER,
    TEXT_AMENDMENT_VERSION,
    AmendmentPublicationAuthority,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.lifecycle import raw_intake_status_for_proposal_status
from finance_core.parser_proposals.numeric_mirror import sqlite_numeric_roundtrip_matches
from finance_core.parser_proposals.receipt_supersession import (
    _acquire_write_transaction,
    _insert_parent_superseded_event,
    _insert_replacement_created_event,
    _repoint_raw_intake,
    _require_current_raw_intake,
    _require_full_proposal,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.staging_guard import require_staging_database


def supersede_text_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    expected_content_hash: str,
    field_updates: dict[str, Any],
    publication_public_id: str,
    amendment_id: str,
    actor: str,
    amendment_authority: AmendmentPublicationAuthority,
    clock: Callable[[], str],
) -> dict[str, Any]:
    require_staging_database(conn)
    _acquire_write_transaction(conn)
    try:
        parent = _require_full_proposal(conn, parser_output_id)
        edge = conn.execute(
            "SELECT * FROM parser_text_amendment_revisions WHERE publication_public_id=?",
            (publication_public_id,),
        ).fetchone()
        existing = None if edge is None else dict(edge)
        amendment_authority.verify_in_transaction(
            conn, persisted_operation=existing, proposal=parent, canonical_patch=field_updates
        )
        if edge is not None:
            child = _require_full_proposal(conn, edge["child_parser_output_id"])
            result = {
                "replacement_parser_output_id": child["id"],
                "replacement_proposal_public_id": child["public_id"],
                "publication_public_id": publication_public_id,
                "idempotent": True,
            }
            amendment_authority.persist_effect_in_transaction(conn, publication_result=result)
            conn.commit()
            return result
        intake = _require_current_raw_intake(conn, parent)
        if compute_effective_proposal_content_hash(conn, parent) != expected_content_hash:
            raise ValueError("Text amendment base content is stale")
        effective, _, _ = resolve_effective_payload(conn, parent)
        child_payload = {**effective, **field_updates}
        if not sqlite_numeric_roundtrip_matches(conn, str(child_payload["amount"])):
            raise ValueError(
                "Text amendment amount cannot retain exact SQLite monetary representation"
            )
        child_json = json.dumps(
            child_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        child_public_id = "atxt_" + publication_public_id.removeprefix("apub_")
        cur = conn.execute(
            "INSERT INTO parser_outputs "
            "(public_id,source_type,source_public_id,statement_batch_id,attachment_id,parser_name,parser_version,raw_text,parsed_payload,normalized_payload,confidence_score,parse_status,parent_parser_output_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,'parsed_pending_confirmation',?)",
            (
                child_public_id,
                parent["source_type"],
                parent["source_public_id"],
                parent["statement_batch_id"],
                parent["attachment_id"],
                TEXT_AMENDMENT_PARSER,
                TEXT_AMENDMENT_VERSION,
                parent["raw_text"],
                child_json,
                child_json,
                parent["confidence_score"],
                parent["id"],
            ),
        )
        child_id = cur.lastrowid
        assert child_id is not None
        conn.execute(
            "INSERT INTO parser_text_amendment_revisions VALUES (?,?,?,?,?)",
            (publication_public_id, amendment_id, parent["id"], child_id, child_json),
        )
        child_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
        now = clock()
        ParserProposalRepository(conn).update_status(parent["id"], "superseded")
        _insert_parent_superseded_event(
            conn,
            parser_output_id=parent["id"],
            from_status=parent["parse_status"],
            actor=actor,
            correction_public_id=publication_public_id,
            replacement_public_id=child_public_id,
            current_hash=expected_content_hash,
            replacement_hash=child_hash,
            changed_fields=sorted(field_updates),
            created_at=now,
        )
        _insert_replacement_created_event(
            conn,
            replacement_id=child_id,
            actor=actor,
            correction_public_id=publication_public_id,
            parent_public_id=parent["public_id"],
            created_at=now,
        )
        event_type = "text_proposal_superseded"
        audit_id = derive_audit_event_public_id(
            aggregate_type="parser_proposal",
            aggregate_public_id=parent["public_id"],
            event_type=event_type,
            causation_public_id=publication_public_id,
        )
        append_financial_audit_event(
            conn,
            AuditEventCommand(
                event_public_id=audit_id,
                aggregate_type="parser_proposal",
                aggregate_public_id=parent["public_id"],
                event_type=event_type,
                event_payload={
                    "publication_public_id": publication_public_id,
                    "replacement_proposal_public_id": child_public_id,
                    "previous_content_hash": expected_content_hash,
                    "replacement_content_hash": child_hash,
                    "changed_fields": sorted(field_updates),
                },
                previous_state={
                    "parse_status": parent["parse_status"],
                    "raw_intake_status": raw_intake_status_for_proposal_status(
                        parent["parse_status"]
                    ),
                    "proposal_content_hash": expected_content_hash,
                    "conversion_status": "not_converted",
                },
                new_state={
                    "parse_status": "superseded",
                    "raw_intake_status": "parsed_pending_confirmation",
                    "proposal_content_hash": expected_content_hash,
                    "conversion_status": "not_converted",
                    "replacement_proposal_public_id": child_public_id,
                    "replacement_content_hash": child_hash,
                },
                actor_type="human",
                actor_public_id=actor,
                authorization_public_id=publication_public_id,
                source_evidence_references=(
                    f"parser-output:{parent['public_id']}",
                    f"source:{parent['source_public_id']}",
                ),
                correlation_public_id=parent["public_id"],
                causation_public_id=publication_public_id,
                created_at=now,
            ),
        )
        result = {
            "replacement_parser_output_id": child_id,
            "replacement_proposal_public_id": child_public_id,
            "publication_public_id": publication_public_id,
            "idempotent": False,
        }
        amendment_authority.persist_effect_in_transaction(conn, publication_result=result)
        _repoint_raw_intake(conn, intake["id"], child_id)
        pointer = conn.execute(
            "SELECT parser_output_id,status FROM raw_intake_records WHERE id=?", (intake["id"],)
        ).fetchone()
        if pointer is None or tuple(pointer) != (child_id, "parsed_pending_confirmation"):
            raise ValueError("Independent text publication lost its atomic current leaf")
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
