"""Transaction-private independent AI child writer; no human edit/decision."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from finance_core.application.ai_invocation_contract import (
    AI_INPUT_SCHEMA,
    AI_PUBLICATION_SCHEMA,
    AI_PUBLICATION_SCHEMA_V1,
    AiInvocationError,
)
from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.independent_ai_validation import FIELDS, sha
from finance_core.parser_proposals.independent_ai_validation import (
    canonical_text as canonical_json_text,
)
from finance_core.parser_proposals.lifecycle import raw_intake_status_for_proposal_status
from finance_core.parser_proposals.numeric_mirror import sqlite_numeric_roundtrip_matches
from finance_core.parser_proposals.repository import ParserProposalRepository

AI_PARSER = "independent_finance_ai"
AI_PARSER_VERSION = "v1"


def _audit_states(base_status, parent_hash, child_public_id, child_hash):
    previous = {
        "parse_status": base_status,
        "raw_intake_status": raw_intake_status_for_proposal_status(base_status),
        "proposal_content_hash": parent_hash,
        "conversion_status": "not_converted",
    }
    successor = {
        **previous,
        "parse_status": "superseded",
        "raw_intake_status": "parsed_pending_confirmation",
        "replacement_proposal_public_id": child_public_id,
        "replacement_content_hash": child_hash,
    }
    return previous, successor


def publish_in_transaction(
    conn: sqlite3.Connection,
    attempt: sqlite3.Row,
    material: dict[str, Any],
    result: sqlite3.Row,
    validation: dict[str, Any],
    now: int,
) -> str:
    if not conn.in_transaction:
        raise AiInvocationError("AI publication requires owner transaction")
    parent = ParserProposalRepository(conn).get_lineage_row_by_id(
        attempt["parent_parser_output_id"]
    )
    if parent is None:
        raise AiInvocationError("AI parent is absent")
    payload = validation["payload"]
    if not sqlite_numeric_roundtrip_matches(conn, payload["amount"]):
        raise AiInvocationError("AI monetary value cannot retain exact representation")
    publication_id = "aipub_" + sha([attempt["attempt_id"], result["result_id"]])[:32]
    public_id = "aipo_" + publication_id.removeprefix("aipub_")
    version = "v2" if material["schema"] == AI_INPUT_SCHEMA else "v1"
    publication_schema = AI_PUBLICATION_SCHEMA if version == "v2" else AI_PUBLICATION_SCHEMA_V1
    child_json = canonical_json_text(payload)
    binding = material["provider_binding"]
    cur = conn.execute(
        (
            "INSERT INTO parser_outputs (public_id,source_type,source_public_i"
            "d,statement_batch_id,attachment_id,parser_name,parser_version,ai_"
            "provider,ai_model,prompt_version,raw_text,parsed_payload,normaliz"
            "ed_payload,confidence_score,parse_status,parent_parser_output_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        ),
        (
            public_id,
            parent["source_type"],
            parent["source_public_id"],
            parent["statement_batch_id"],
            parent["attachment_id"],
            AI_PARSER,
            version,
            binding["provider_id"],
            binding["model_id"],
            material["assets"]["prompt_" + version + ".txt"],
            parent["raw_text"],
            child_json,
            child_json,
            parent["confidence_score"],
            "parsed_pending_confirmation",
            parent["id"],
        ),
    )
    child_id = cur.lastrowid
    if child_id is None:
        raise AiInvocationError("AI child identity absent")
    timestamp = datetime.fromtimestamp(now, UTC).isoformat()
    evidences = []
    for field in FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        cite = validation["evidence"].get(field, {"ref": "context", "quote": str(value)})
        ref = cite[0]["ref"] if isinstance(cite, list) else cite["ref"]
        item = {
            "field_name": field,
            "proposed_value": value,
            "confidence_score": None,
            "evidence_source_type": "ai_model",
            "evidence_reference": f"independent-ai:{publication_id}:{ref}",
            "notes": canonical_json_text(
                {
                    "input_hash": material["request_hash"],
                    "result_id": result["result_id"],
                    "citation": cite,
                }
            ),
        }
        conn.execute(
            (
                "INSERT INTO parser_proposal_field_evidence (parser_output_id,fiel"
                "d_name,proposed_value,confidence_score,evidence_source_type,evide"
                "nce_reference,notes) VALUES (?,?,?,?,?,?,?)"
            ),
            (child_id, *item.values()),
        )
        evidences.append(item)
    parent_link = conn.execute(
        "SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id=?", (parent["id"],)
    ).fetchone()
    link_material = None
    if parent_link is not None:
        link_material = {
            "public_id": "ropl_" + sha(publication_id)[:32],
            "extraction_id": parent_link["extraction_id"],
            "parser_output_id": child_id,
            "proposal_input_hash": material["request_hash"],
            "proposal_result_hash": result["raw_response_hash"],
            "parser_contract_version": publication_schema,
            "link_role": "independent_ai",
        }
        conn.execute(
            (
                "INSERT INTO receipt_ocr_proposal_links (public_id,extraction_id,p"
                "arser_output_id,proposal_input_hash,proposal_result_hash,parser_c"
                "ontract_version,link_role) VALUES (?,?,?,?,?,?,?)"
            ),
            tuple(link_material.values()),
        )
    child_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
    seal = {
        "schema": publication_schema,
        "attempt_id": attempt["attempt_id"],
        "result_id": result["result_id"],
        "parent_id": parent["id"],
        "parent_version": attempt["parent_version"],
        "parent_hash": attempt["parent_content_hash"],
        "child_id": child_id,
        "child_hash": child_hash,
        "payload_json": child_json,
        "fields": evidences,
        "receipt_link": link_material,
        "published_at": now,
        "base_status": parent["parse_status"],
    }
    conn.execute(
        "INSERT INTO independent_ai_publications VALUES (?,?,?,?,?,?,?,?,?)",
        (
            publication_id,
            attempt["attempt_id"],
            result["result_id"],
            parent["id"],
            child_id,
            child_hash,
            canonical_json_text(seal),
            sha(seal),
            now,
        ),
    )
    ParserProposalRepository(conn).update_status(parent["id"], "superseded")
    for identity, kind, from_status, to_status in (
        (parent["id"], "superseded", parent["parse_status"], "superseded"),
        (child_id, "created", None, "parsed_pending_confirmation"),
    ):
        conn.execute(
            (
                "INSERT INTO parser_proposal_events (parser_output_id,event_type,f"
                "rom_status,to_status,actor_type,actor_identifier,event_reason,eve"
                "nt_payload,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
            ),
            (
                identity,
                kind,
                from_status,
                to_status,
                "ai",
                binding["adapter_id"],
                publication_id,
                canonical_json_text(
                    {
                        "publication_id": publication_id,
                        "attempt_id": attempt["attempt_id"],
                        "actor_type": "ai",
                    }
                ),
                timestamp,
            ),
        )
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=parent["public_id"],
        event_type="independent_ai_proposal_published",
        causation_public_id=publication_id,
    )
    previous_state, new_state = _audit_states(
        parent["parse_status"], attempt["parent_content_hash"], public_id, child_hash
    )
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=audit_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=parent["public_id"],
            event_type="independent_ai_proposal_published",
            event_payload=seal,
            previous_state=previous_state,
            new_state=new_state,
            actor_type="ai",
            actor_public_id=binding["adapter_id"],
            authorization_public_id=attempt["attempt_id"],
            source_evidence_references=(
                f"source:{parent['source_public_id']}",
                f"independent-ai-result:{result['result_id']}",
            ),
            correlation_public_id=parent["public_id"],
            causation_public_id=publication_id,
            created_at=timestamp,
        ),
    )
    conn.execute(
        (
            "UPDATE raw_intake_records SET parser_output_id=?,status='parsed_p"
            "ending_confirmation' WHERE public_id=? AND parser_output_id=?"
        ),
        (child_id, attempt["intake_public_id"], parent["id"]),
    )
    return public_id
