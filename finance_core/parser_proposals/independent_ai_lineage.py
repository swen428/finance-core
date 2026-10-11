"""Bounded mixed independent-AI/HUMAN lineage, separate from old AI custody."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from finance_core.application.ai_invocation_contract import (
    AI_INPUT_SCHEMA,
    AI_INPUT_SCHEMA_V1,
    AI_PUBLICATION_SCHEMA,
    AI_PUBLICATION_SCHEMA_V1,
    AiInvocationError,
)
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text as authoritative_json_text,
)
from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.financial_audit import (
    FinancialAuditRepository,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.financial_audit.chain import _normalize_timestamp
from finance_core.parser_proposals.content_hash import (
    compute_effective_proposal_content_hash,
    compute_proposal_content_hash,
)
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.independent_ai_publication import (
    AI_PARSER,
    _audit_states,
)
from finance_core.parser_proposals.independent_ai_validation import assets, sha
from finance_core.parser_proposals.independent_ai_validation import (
    canonical_text as canonical_json_text,
)
from finance_core.parser_proposals.repository import ParserProposalRepository, row_to_dict

_AI_SCHEMA_HASH = "8f7a12a4cdfa503d3c684654a4274ace4040d168ff46626fce619a546db1573b"


def require_ai_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        (
            "SELECT type,name,sql FROM sqlite_master WHERE (name LIKE 'independ"
            "ent_ai_%' AND name NOT LIKE 'independent_ai_clarification_%' "
            "AND name NOT LIKE 'independent_ai_followup_%') "
            "OR name='trg_ai_fallback_raw_intake_no_lineage_escape' "
            "OR name='receipt_ocr_proposal_links' OR name LIKE 'trg_receipt_oc"
            "r_proposal_links_%' OR name LIKE 'idx_receipt_ocr_proposal_links_"
            "%' ORDER BY type,name"
        )
    ).fetchall()
    digest = hashlib.sha256(
        json.dumps([tuple(row) for row in rows], separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if digest != _AI_SCHEMA_HASH:
        raise AiInvocationError("Independent AI migration 058 absent or changed")


def has_independent_ai_ancestry(conn: sqlite3.Connection, proposal: Any) -> bool:
    repository = ParserProposalRepository(conn)
    has_schema = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'independent_ai_%' LIMIT 1"
        ).fetchone()
        is not None
    )
    if has_schema:
        require_ai_schema(conn)
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name LIKE 'independent_ai_clarification_%' "
                "OR name LIKE 'independent_ai_followup_%' LIMIT 1"
            ).fetchone()
            is not None
        ):
            require_clarification_schema(conn)
    current = repository.get_lineage_row_by_id(proposal["id"])
    visited: set[int] = set()
    while current is not None:
        if current["id"] in visited or len(visited) >= 128:
            raise AiInvocationError("Proposal ancestry cycles or exceeds bound")
        visited.add(current["id"])
        ownership = verify_node_ownership(conn, current)
        if ownership.independent_domain:
            if not has_schema:
                raise AiInvocationError("Independent AI origin schema missing")
            return True
        parent = (
            current.get("parent_parser_output_id")
            if isinstance(current, dict)
            else current["parent_parser_output_id"]
        )
        current = repository.get_lineage_row_by_id(parent) if parent is not None else None
        if parent is not None and current is None:
            raise AiInvocationError("Proposal ancestry parent absent")
    return False


def _local_one(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    row = cursor.fetchone()
    return None if row is None else row_to_dict(row, cursor.description)


def _local_rows(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    return [row_to_dict(row, cursor.description) for row in cursor.fetchall()]


def _sealed(row: Any) -> dict[str, Any]:
    material = json.loads(row["material_json"])
    if (
        not isinstance(material, dict)
        or canonical_json_text(material) != row["material_json"]
        or sha(material) != row["material_hash"]
    ):
        raise AiInvocationError("Independent AI evidence seal changed")
    return material


@dataclass(frozen=True)
class PublicationCreationRecord:
    publication_id: str
    attempt_id: str
    result_id: str
    parent_id: int
    child_id: int
    creation_payload: dict[str, Any]
    creation_content_hash: str
    material: dict[str, Any]
    seal: dict[str, Any]
    audit_id: str


def verify_publication_creation_record(
    conn: sqlite3.Connection, edge: sqlite3.Row
) -> PublicationCreationRecord:
    """Local sealed creation only: never reconstruct history/current/provider."""
    seal = _sealed(edge)
    attempt = _local_one(
        conn, "SELECT * FROM independent_ai_attempts WHERE attempt_id=?", (edge["attempt_id"],)
    )
    result = _local_one(
        conn, "SELECT * FROM independent_ai_results WHERE result_id=?", (edge["result_id"],)
    )
    claim = _local_one(
        conn, "SELECT * FROM independent_ai_claims WHERE attempt_id=?", (edge["attempt_id"],)
    )
    if attempt is None or result is None or claim is None:
        raise AiInvocationError("Independent AI invocation evidence absent")
    material, observation, dispatch = _sealed(attempt), _sealed(result), _sealed(claim)
    version = "v2" if material["schema"] == AI_INPUT_SCHEMA else "v1"
    publication_schema = AI_PUBLICATION_SCHEMA if version == "v2" else AI_PUBLICATION_SCHEMA_V1
    if version == "v2":
        require_clarification_schema(conn)
    if (
        material["schema"] not in {AI_INPUT_SCHEMA, AI_INPUT_SCHEMA_V1}
        or material["assets"] != assets(version)
        or material["parent_id"] != attempt["parent_parser_output_id"]
        or material["parent_content_hash"] != attempt["parent_content_hash"]
        or material["parent_version"] != attempt["parent_version"]
        or material["intake_public_id"] != attempt["intake_public_id"]
        or material["operation_id"] != attempt["operation_id"]
        or material["kind"] != attempt["kind"]
        or material["input_identity"] != attempt["input_identity"]
        or material["created_at"] != attempt["created_at"]
        or material["deadline_at"] != attempt["deadline_at"]
        or sha(
            [
                material["source"]["namespace"],
                material["source"]["instance_id"],
                material["source"]["source_event_id"],
            ]
        )
        != attempt["source_event_key"]
        or material["source"]["intake_public_id"] != attempt["intake_public_id"]
        or hashlib.sha256(material["request_json"].encode()).hexdigest() != material["request_hash"]
        or claim["claim_id"] != "aic_" + sha([attempt["attempt_id"], material["request_hash"]])[:32]
        or claim["request_hash"] != material["request_hash"]
        or canonical_json_text(dispatch)
        != canonical_json_text(
            {
                "attempt_id": attempt["attempt_id"],
                "claim_id": claim["claim_id"],
                "request_hash": material["request_hash"],
                "claimed_at": claim["claimed_at"],
                "deadline_at": attempt["deadline_at"],
            }
        )
    ):
        raise AiInvocationError("Independent AI request correlation changed")
    request = json.loads(material["request_json"])
    if request != {
        "input": material["projection"],
        "assets": material["assets"],
        "retry": 0,
        "tools": [],
        "image_to_model": 0,
    }:
        raise AiInvocationError("Independent AI request projection changed")
    if (
        result["attempt_id"] != attempt["attempt_id"]
        or result["result_id"]
        != "air_" + sha([attempt["attempt_id"], result["observation_record_id"]])[:32]
        or observation["claim_id"] != claim["claim_id"]
        or observation["request_hash"] != claim["request_hash"]
        or observation["outcome"] != "RESPONSE"
        or observation["observed_provider_id"] != material["provider_binding"]["provider_id"]
        or observation["observed_model_id"] != material["provider_binding"]["model_id"]
        or not claim["claimed_at"] <= observation["observed_at"] < attempt["deadline_at"]
        or observation["raw_response"] != result["raw_response"]
        or result["raw_response_hash"]
        != hashlib.sha256(result["raw_response"].encode()).hexdigest()
        or type(result["observed_at"]) is not int
        or observation["observed_at"] != result["observed_at"]
    ):
        raise AiInvocationError("Independent AI response attribution changed")
    validation = json.loads(result["validation_json"])
    if (
        not isinstance(validation, dict)
        or validation.get("state") != "VALID"
        or (result["validation_json"] != canonical_json_text(validation))
    ):
        raise AiInvocationError("Independent AI stored validation seal changed")
    parent = ParserProposalRepository(conn).get_lineage_row_by_id(edge["parent_parser_output_id"])
    child = ParserProposalRepository(conn).get_lineage_row_by_id(edge["child_parser_output_id"])
    if (
        parent is None
        or child is None
        or parent["id"] != attempt["parent_parser_output_id"]
        or parent["parse_status"] != "superseded"
        or child["parent_parser_output_id"] != parent["id"]
        or child["parser_name"] != AI_PARSER
        or child["parser_version"] != version
        or child["ai_provider"] != material["provider_binding"]["provider_id"]
        or child["ai_model"] != material["provider_binding"]["model_id"]
        or child["prompt_version"] != material["assets"]["prompt_" + version + ".txt"]
    ):
        raise AiInvocationError("Independent AI proposal topology changed")
    if any(
        parent[key] != child[key]
        for key in (
            "source_type",
            "source_public_id",
            "statement_batch_id",
            "attachment_id",
            "raw_text",
        )
    ):
        raise AiInvocationError("Independent AI original source changed")
    if (
        compute_proposal_content_hash(
            conn, {**parent, "parsed_payload": canonical_json_text(material["base_payload"])}
        )
        != attempt["parent_content_hash"]
        or child["parsed_payload"] != seal["payload_json"]
        or child["normalized_payload"] != seal["payload_json"]
        or json.loads(seal["payload_json"]) != validation["payload"]
        or compute_proposal_content_hash(conn, child) != edge["child_content_hash"]
        or seal["child_hash"] != edge["child_content_hash"]
        or seal["child_id"] != child["id"]
        or seal["parent_id"] != parent["id"]
        or seal["parent_hash"] != attempt["parent_content_hash"]
        or seal["parent_version"] != attempt["parent_version"]
        or seal["schema"] != publication_schema
        or seal["attempt_id"] != attempt["attempt_id"]
        or seal["result_id"] != result["result_id"]
        or seal["published_at"] != edge["published_at"]
        or not result["observed_at"] <= edge["published_at"] < attempt["deadline_at"]
    ):
        raise AiInvocationError("Independent AI publication payload binding changed")
    events = conn.execute(
        "SELECT kind FROM independent_ai_events WHERE attempt_id=? AND observed_at<=?",
        (attempt["attempt_id"], edge["published_at"]),
    ).fetchall()
    if events:
        raise AiInvocationError("Cancelled or uncertain invocation was published")
    rows = conn.execute(
        (
            "SELECT field_name,proposed_value,confidence_score,evidence_source"
            "_type,evidence_reference,notes FROM parser_proposal_field_evidenc"
            "e WHERE parser_output_id=? ORDER BY id"
        ),
        (child["id"],),
    ).fetchall()
    keys = (
        "field_name",
        "proposed_value",
        "confidence_score",
        "evidence_source_type",
        "evidence_reference",
        "notes",
    )
    if [dict(zip(keys, tuple(row), strict=True)) for row in rows] != seal["fields"] or any(
        row[3] != "ai_model" for row in rows
    ):
        raise AiInvocationError("Independent AI field evidence set changed")
    link = conn.execute(
        (
            "SELECT public_id,extraction_id,parser_output_id,proposal_input_ha"
            "sh,proposal_result_hash,parser_contract_version,link_role FROM re"
            "ceipt_ocr_proposal_links WHERE parser_output_id=?"
        ),
        (child["id"],),
    ).fetchone()
    link_keys = (
        "public_id",
        "extraction_id",
        "parser_output_id",
        "proposal_input_hash",
        "proposal_result_hash",
        "parser_contract_version",
        "link_role",
    )
    if (None if link is None else dict(zip(link_keys, tuple(link), strict=True))) != seal[
        "receipt_link"
    ]:
        raise AiInvocationError("Independent AI receipt source link changed")
    parent_link = conn.execute(
        "SELECT extraction_id FROM receipt_ocr_proposal_links WHERE parser_output_id=?",
        (parent["id"],),
    ).fetchone()
    if (
        (link is None) != (parent_link is None)
        or link is not None
        and (
            link[1] != parent_link[0]
            or link[3] != material["request_hash"]
            or link[4] != result["raw_response_hash"]
            or link[6] != "independent_ai"
        )
    ):
        raise AiInvocationError("Independent AI receipt extraction identity changed")
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=parent["public_id"],
        event_type="independent_ai_proposal_published",
        causation_public_id=edge["publication_id"],
    )
    audit = FinancialAuditRepository(conn).fetch(audit_id)
    previous_state, new_state = _audit_states(
        seal["base_status"],
        attempt["parent_content_hash"],
        child["public_id"],
        edge["child_content_hash"],
    )
    if (
        audit is None
        or not verify_financial_audit_chain(
            conn, aggregate_type="parser_proposal", aggregate_public_id=parent["public_id"]
        ).valid
        or audit.aggregate_type != "parser_proposal"
        or audit.aggregate_public_id != parent["public_id"]
        or audit.event_type != "independent_ai_proposal_published"
        or audit.created_at
        != _normalize_timestamp(datetime.fromtimestamp(edge["published_at"], UTC).isoformat())
        or audit.calculation_snapshot_public_id is not None
        or audit.calculation_snapshot_hash is not None
        or audit.actor_type != "ai"
        or audit.actor_public_id != material["provider_binding"]["adapter_id"]
        or audit.authorization_public_id != attempt["attempt_id"]
        or audit.causation_public_id != edge["publication_id"]
        or audit.correlation_public_id != parent["public_id"]
        or canonical_json_value(audit.previous_state_json) != previous_state
        or canonical_json_value(audit.new_state_json) != new_state
        or set(audit.source_evidence_references)
        != {f"source:{parent['source_public_id']}", f"independent-ai-result:{result['result_id']}"}
        or canonical_json_value(audit.event_payload_json)
        != canonical_json_value(authoritative_json_text(seal))
    ):
        raise AiInvocationError("Independent AI publication audit changed")
    lifecycle = conn.execute(
        (
            "SELECT parser_output_id,event_type,actor_type,actor_identifier,ev"
            "ent_payload,from_status,to_status,created_at FROM parser_pro"
            "posal_events WHERE event_reason=? ORDE"
            "R BY id"
        ),
        (edge["publication_id"],),
    ).fetchall()
    expected_payload = canonical_json_text(
        {
            "publication_id": edge["publication_id"],
            "attempt_id": attempt["attempt_id"],
            "actor_type": "ai",
        }
    )
    if [tuple(row) for row in lifecycle] != [
        (
            parent["id"],
            "superseded",
            "ai",
            material["provider_binding"]["adapter_id"],
            expected_payload,
            seal["base_status"],
            "superseded",
            datetime.fromtimestamp(edge["published_at"], UTC).isoformat(),
        ),
        (
            child["id"],
            "created",
            "ai",
            material["provider_binding"]["adapter_id"],
            expected_payload,
            None,
            "parsed_pending_confirmation",
            datetime.fromtimestamp(edge["published_at"], UTC).isoformat(),
        ),
    ]:
        raise AiInvocationError("Independent AI lifecycle evidence changed")
    # Fetch by owned domain identities as well as expected aggregate/event ID.
    # SQL text matching is discovery only; inspect explicit typed identity slots.
    owned = {
        edge["publication_id"],
        attempt["attempt_id"],
        result["result_id"],
        "independent-ai-result:" + result["result_id"],
    }
    candidates = _local_rows(
        conn,
        "SELECT * FROM financial_audit_events WHERE causation_public_"
        "id=? OR authorization_public_id=? OR event_payload_json LIKE"
        " ? OR source_evidence_refs_json LIKE ?",
        (
            edge["publication_id"],
            attempt["attempt_id"],
            "%" + edge["publication_id"] + "%",
            "%" + result["result_id"] + "%",
        ),
    )
    for candidate in candidates:
        identities = _domain_identity_values(
            json.loads(candidate["event_payload_json"])
        ) | _domain_identity_values(
            json.loads(candidate["source_evidence_refs_json"]), references=True
        )
        if (
            candidate["causation_public_id"] in owned
            or candidate["authorization_public_id"] in owned
            or identities & owned
        ) and candidate["event_public_id"] != audit_id:
            raise AiInvocationError("Independent AI audit identity is duplicated or relocated")
    candidates = _local_rows(
        conn,
        "SELECT * FROM parser_proposal_events WHERE event_reason=? OR"
        " event_payload LIKE ? OR event_payload LIKE ?",
        (
            edge["publication_id"],
            "%" + edge["publication_id"] + "%",
            "%" + attempt["attempt_id"] + "%",
        ),
    )
    if any(
        (
            _domain_identity_values(json.loads(row["event_payload"] or "{}")) & owned
            or row["event_reason"] == edge["publication_id"]
        )
        and (
            row["event_reason"] != edge["publication_id"]
            or row["parser_output_id"] not in {parent["id"], child["id"]}
        )
        for row in candidates
    ):
        raise AiInvocationError("Independent AI lifecycle identity is duplicated or relocated")
    if edge["publication_id"] != "aipub_" + sha([attempt["attempt_id"], result["result_id"]])[
        :32
    ] or (child["public_id"] != "aipo_" + edge["publication_id"].removeprefix("aipub_")):
        raise AiInvocationError("Independent AI creation namespace changed")
    return PublicationCreationRecord(
        edge["publication_id"],
        attempt["attempt_id"],
        result["result_id"],
        parent["id"],
        child["id"],
        json.loads(seal["payload_json"]),
        edge["child_content_hash"],
        material,
        seal,
        audit_id,
    )


def verify_publication(conn: sqlite3.Connection, edge: sqlite3.Row) -> dict[str, Any]:
    creation = verify_publication_creation_record(conn, edge)
    result = _local_one(
        conn, "SELECT * FROM independent_ai_results WHERE result_id=?", (creation.result_id,)
    )
    from finance_core.parser_proposals.independent_ai_source_money import validate_source_response

    validation = validate_source_response(
        conn, result["raw_response"], creation.material, historical=True
    )
    if validation["state"] != "VALID" or result["validation_json"] != canonical_json_text(
        validation
    ):
        raise AiInvocationError("Independent AI response no longer validates")
    return creation.material


def _reserved_identity(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(
        ("aipub_", "aipo_", "aia_", "aic_", "air_", "result:air_", "independent-ai:")
    )


_IDENTITY_KEYS = frozenset(
    {
        "publication_id",
        "attempt_id",
        "result_id",
        "result_public_id",
        "attempt_public_id",
        "claim_id",
        "authorization_public_id",
        "causation_public_id",
        "child_proposal_public_id",
        "replacement_proposal_public_id",
        "evidence_reference",
        "source_evidence_references",
        "source_evidence_refs",
    }
)


def _domain_identity_values(value: Any, *, references: bool = False) -> set[str]:
    if isinstance(value, dict) and set(value) == {"contract_version", "value"}:
        return _domain_identity_values(value["value"], references=references)
    if references and isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    if not isinstance(value, dict):
        return set()
    identities: set[str] = set()
    for key, item in value.items():
        if key in _IDENTITY_KEYS:
            if isinstance(item, str):
                identities.add(item)
            elif isinstance(item, list):
                identities.update(_domain_identity_values(item, references=True))
    return identities


def _structured_marker(value: Any, *, references: bool = False) -> bool:
    # Only domain identity slots are markers. Quotes, description, category,
    # candidates and arbitrary user strings are never searched for prefixes.
    if isinstance(value, dict) and set(value) == {"contract_version", "value"}:
        # The audit canonical envelope is a protocol container, not user text.
        return _structured_marker(value["value"], references=references)
    if references and isinstance(value, list):
        return any(
            _reserved_identity(v) or isinstance(v, str) and v.startswith("independent-ai-result:")
            for v in value
        )
    if not isinstance(value, dict):
        return False
    return any(
        k in _IDENTITY_KEYS
        and (
            _reserved_identity(v)
            or isinstance(v, str)
            and v.startswith("independent-ai-result:")
            or isinstance(v, list)
            and _structured_marker(v, references=True)
        )
        or k == "schema"
        and isinstance(v, str)
        and v.startswith("finance-independent-ai-")
        for k, v in value.items()
    )


def independent_marker_inventory(conn: sqlite3.Connection, node: Any) -> dict[str, Any]:
    """Reserved structured identities only; user source text is never searched."""
    identity = node["id"]

    def rows(sql):
        cursor = conn.execute(sql, (identity,))
        return [row_to_dict(row, cursor.description) for row in cursor.fetchall()]

    links = rows("SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id=?")
    fields = rows(
        "SELECT * FROM parser_proposal_field_evidence WHERE parser_output_id=? ORDER BY id"
    )
    lifecycle = rows("SELECT * FROM parser_proposal_events WHERE parser_output_id=? ORDER BY id")
    cursor = conn.execute(
        "SELECT * FROM financial_audit_events WHERE aggregate_public_id=? ORDER BY sequence_number",
        (node["public_id"],),
    )
    audits = [row_to_dict(row, cursor.description) for row in cursor.fetchall()]

    def marker_json(raw, *, references=False, protocol=False):
        try:
            return _structured_marker(json.loads(raw or "null"), references=references)
        except (ValueError, TypeError) as exc:
            if protocol:
                raise AiInvocationError("Reserved protocol marker JSON malformed") from exc
            return False

    # These columns are protocol JSON at the persistence boundary, even when
    # another typed identity already identifies a reserved marker.
    for row in audits:
        marker_json(row["event_payload_json"], protocol=True)
        marker_json(row["source_evidence_refs_json"], references=True, protocol=True)
    for row in lifecycle:
        marker_json(row["event_payload"], protocol=True)

    return {
        "parser": (
            isinstance(node["parser_name"], str) and node["parser_name"].startswith(AI_PARSER)
        )
        or node["public_id"].startswith("aipo_")
        or str(node["parser_version"]).startswith("finance-independent-ai-"),
        "links": [
            r
            for r in links
            if r["link_role"].startswith("independent_ai")
            or str(r["parser_contract_version"]).startswith("finance-independent-ai-")
        ],
        "fields": [
            r
            for r in fields
            if _reserved_identity(r["evidence_reference"])
            or marker_json(
                r["evidence_reference"],
                protocol=isinstance(r["evidence_reference"], str)
                and r["evidence_reference"].lstrip().startswith(("{", "[")),
            )
            or marker_json(r["notes"])
        ],
        "lifecycle": [
            r
            for r in lifecycle
            if r["event_type"].startswith(("independent_ai", "independent-ai"))
            or _reserved_identity(r["actor_identifier"])
            or _reserved_identity(r["event_reason"])
            or marker_json(r["event_payload"], protocol=True)
        ],
        "audits": [
            r
            for r in audits
            if r["event_type"].startswith(("independent_ai", "independent-ai"))
            or any(
                _reserved_identity(r[k]) for k in ("causation_public_id", "authorization_public_id")
            )
            or (r["actor_type"] == "ai" and _reserved_identity(r["actor_public_id"]))
            or marker_json(r["event_payload_json"], protocol=True)
            or marker_json(r["source_evidence_refs_json"], references=True, protocol=True)
        ],
    }


@dataclass(frozen=True)
class NodeOwnershipProof:
    domain: str
    creation: Any
    independent_domain: bool


def _owner_rows(conn, table, predicate, identity):
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is None
    ):
        return []
    cursor = conn.execute("SELECT * FROM " + table + " WHERE " + predicate, (identity,))
    return [row_to_dict(row, cursor.description) for row in cursor.fetchall()]


def _legacy_marker_claims(conn, link):
    """Claim only the exact old audit and writer lifecycle after native proof."""
    from finance_core.parser_proposals.ai_fallback import _verify_legacy_ai_creation_edge

    child = ParserProposalRepository(conn).get_lineage_row_by_id(link["parser_output_id"])
    creation = _verify_legacy_ai_creation_edge(conn, child)
    result = _owner_rows(conn, "ai_fallback_results", "id=?", link["result_id"])[0]
    result_id = result["result_public_id"]
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=child["public_id"],
        event_type="parser_proposal_ai_fallback_child_created",
        causation_public_id=result_id,
    )
    candidates = _local_rows(
        conn,
        "SELECT * FROM financial_audit_events WHERE causation_public_id=? OR "
        "authorization_public_id=? OR event_payload_json LIKE ? "
        "OR source_evidence_refs_json LIKE ?",
        (result_id, result_id, "%" + result_id + "%", "%" + result_id + "%"),
    )
    for row in candidates:
        identities = _domain_identity_values(
            json.loads(row["event_payload_json"])
        ) | _domain_identity_values(json.loads(row["source_evidence_refs_json"]), references=True)
        if (
            row["causation_public_id"] == result_id
            or row["authorization_public_id"] == result_id
            or identities & {result_id, "result:" + result_id}
        ) and row["event_public_id"] != audit_id:
            raise AiInvocationError("Legacy AI shared result marker has no exact native owner")
    expected_payload = {
        "attempt_public_id": creation.attempt["attempt_public_id"],
        "result_public_id": result_id,
        "child_proposal_public_id": child["public_id"],
        "previous_content_hash": creation.attempt["parent_effective_content_hash"],
        "child_content_hash": creation.creation_content_hash,
        "actor_type": "ai",
    }
    events = _local_rows(
        conn,
        "SELECT * FROM parser_proposal_events WHERE event_payload LIKE ? OR actor_identifier=?",
        ("%" + result_id + "%", creation.attempt["attempt_public_id"]),
    )
    events = [
        row
        for row in events
        if result_id in _domain_identity_values(json.loads(row["event_payload"] or "{}"))
        or row["actor_identifier"] == creation.attempt["attempt_public_id"]
    ]
    expected = [
        (
            creation.attempt["parent_parser_output_id"],
            "superseded",
            "parsed_pending_confirmation",
            "superseded",
            "AI fallback child " + child["public_id"],
        ),
        (
            child["id"],
            "created",
            None,
            "parsed_pending_confirmation",
            "AI fallback child of "
            + ParserProposalRepository(conn).get_lineage_row_by_id(
                creation.attempt["parent_parser_output_id"]
            )["public_id"],
        ),
    ]
    actual = sorted(
        (
            row["parser_output_id"],
            row["event_type"],
            row["from_status"],
            row["to_status"],
            row["event_reason"],
        )
        for row in events
    )
    if actual != sorted(expected) or any(
        row["actor_type"] != "ai"
        or row["actor_identifier"] != creation.attempt["attempt_public_id"]
        or json.loads(row["event_payload"]) != expected_payload
        for row in events
    ):
        raise AiInvocationError("Legacy AI lifecycle marker has no exact native owner")
    return creation, audit_id, events


def verify_node_ownership(conn: sqlite3.Connection, node: Any) -> NodeOwnershipProof:
    """Local exact native owners; never historical/current admission or provider calls."""
    incoming = _owner_rows(
        conn, "independent_ai_publications", "child_parser_output_id=?", node["id"]
    )
    outgoing = _owner_rows(
        conn, "independent_ai_publications", "parent_parser_output_id=?", node["id"]
    )
    human = _owner_rows(
        conn,
        "application_amendment_records",
        "resulting_parser_output_id=? AND publication_kind!='completion'",
        node["id"],
    )
    legacy = _owner_rows(conn, "ai_fallback_proposal_links", "parser_output_id=?", node["id"])
    if (
        any(len(rows) > 1 for rows in (incoming, outgoing, human, legacy))
        or sum(bool(rows) for rows in (incoming, human, legacy)) > 1
    ):
        raise AiInvocationError("Independent AI creation owner is contradictory")
    inventory = independent_marker_inventory(conn, node)
    # Pure native roots/D1 retain their existing public native owner dispatcher.
    # Their ordinary metadata is not a new independent authority requirement.
    relevant = any(inventory.values()) or incoming or outgoing
    legacy_creation = None
    native_audits = []
    native_lifecycle = []
    outgoing_legacy = []
    if legacy:
        legacy_creation, native_audit, native_events = _legacy_marker_claims(conn, legacy[0])
        native_audits.append(native_audit)
        native_lifecycle.extend(native_events)
    elif node["parser_name"] == "finance_ai_proposal" and not human and relevant:
        from finance_core.parser_proposals.ai_fallback import _verify_legacy_ai_creation_edge

        # Missing native link must retain its native typed diagnostic.
        _verify_legacy_ai_creation_edge(conn, node)
        raise AiInvocationError("Legacy AI creation has no concrete link")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ai_fallback_proposal_links'"
    ).fetchone():
        outgoing_legacy = _local_rows(
            conn,
            "SELECT l.* FROM ai_fallback_proposal_links l JOIN ai_fallback_results r "
            "ON r.id=l.result_id JOIN ai_fallback_attempts a ON a.id=r.attempt_id "
            "WHERE a.parent_parser_output_id=?",
            (node["id"],),
        )
    if len(outgoing_legacy) + len(outgoing) > 1:
        raise AiInvocationError("AI creation has contradictory outgoing owners")
    for link in outgoing_legacy:
        _, _, native_events = _legacy_marker_claims(conn, link)
        native_lifecycle.extend(native_events)
    human_material = None
    if human:
        if human[0]["publication_kind"] not in {"receipt_supersession", "text_supersession"}:
            raise AiInvocationError("Unknown HUMAN creation owner kind")
        from finance_core.parser_proposals.amendment_lineage import verify_amendment_record

        human_material = verify_amendment_record(conn, human[0])
    if not relevant:
        return NodeOwnershipProof("HUMAN" if human else "ORIGINAL", None, False)
    if incoming or outgoing or inventory["parser"] or inventory["links"] or inventory["fields"]:
        require_ai_schema(conn)
    proofs = [verify_publication_creation_record(conn, edge) for edge in [*incoming, *outgoing]]
    creation = proofs[0] if incoming else None
    inherited_parser = False
    inherited_links = []
    if human_material is not None and human[0]["publication_kind"] == "receipt_supersession":
        parent = ParserProposalRepository(conn).get_lineage_row_by_id(
            human[0]["base_parser_output_id"]
        )
        if parent is None or any(node[k] != parent[k] for k in ("parser_name", "parser_version")):
            raise AiInvocationError("HUMAN inherited parser pair changed")
        inherited_parser = bool(inventory["parser"]) and not node["public_id"].startswith("aipo_")
        inherited_links = inventory["links"]
        if any(row["link_role"] != "superseding_correction" for row in inherited_links):
            raise AiInvocationError("HUMAN inherited OCR role has no native owner")
    if bool(inventory["parser"]) != bool(incoming or inherited_parser):
        raise AiInvocationError("Independent AI parser namespace has no creation owner")
    expected_links = (
        []
        if creation is None or creation.seal["receipt_link"] is None
        else [creation.seal["receipt_link"]]
    )
    link_keys = (
        "public_id",
        "extraction_id",
        "parser_output_id",
        "proposal_input_hash",
        "proposal_result_hash",
        "parser_contract_version",
        "link_role",
    )
    actual_links = [{k: row[k] for k in link_keys} for row in inventory["links"]]
    if actual_links != expected_links + [{k: row[k] for k in link_keys} for row in inherited_links]:
        raise AiInvocationError("Independent AI OCR markers have no exact owner")
    expected_audits = [proof.audit_id for proof in proofs if proof.parent_id == node["id"]]
    expected_audits.extend(native_audits)
    if sorted(row["event_public_id"] for row in inventory["audits"]) != sorted(expected_audits):
        raise AiInvocationError("Independent AI audit marker has no unique owner")
    expected_lifecycle = sorted(
        (proof.publication_id, "created" if proof.child_id == node["id"] else "superseded")
        for proof in proofs
    )
    expected_lifecycle.extend(
        (row["event_reason"], row["event_type"])
        for row in native_lifecycle
        if row["parser_output_id"] == node["id"]
    )
    if sorted((row["event_reason"], row["event_type"]) for row in inventory["lifecycle"]) != sorted(
        expected_lifecycle
    ):
        raise AiInvocationError("Independent AI lifecycle marker has no unique owner")
    for proof in proofs:
        for row in _local_rows(
            conn,
            "SELECT * FROM parser_proposal_field_evidence WHERE evidence_reference LIKE ?",
            ("independent-ai:" + proof.publication_id + ":%",),
        ):
            if row["parser_output_id"] != proof.child_id:
                _verify_inherited_field_marker(conn, row, proof)
    if creation is None:
        for row in inventory["fields"]:
            ref = row["evidence_reference"]
            if not isinstance(ref, str) or not ref.startswith("independent-ai:"):
                raise AiInvocationError("Independent AI field marker has no inherited owner")
            edge = conn.execute(
                "SELECT * FROM independent_ai_publications WHERE publication_id=?",
                (ref.split(":")[1],),
            ).fetchone()
            if edge is None:
                raise AiInvocationError("Independent AI inherited field owner absent")
            _verify_inherited_field_marker(
                conn, row, verify_publication_creation_record(conn, edge)
            )
    domain = (
        "INDEPENDENT_AI"
        if creation
        else "LEGACY_AI"
        if legacy_creation
        else "HUMAN"
        if human
        else "ORIGINAL"
    )
    return NodeOwnershipProof(
        domain,
        creation or legacy_creation,
        bool(incoming or outgoing or inherited_parser or inventory["fields"]),
    )


def verify_independent_marker_owners(
    conn: sqlite3.Connection, node: Any
) -> PublicationCreationRecord | None:
    ownership = verify_node_ownership(conn, node)
    return ownership.creation if ownership.domain == "INDEPENDENT_AI" else None


def _verify_inherited_field_marker(conn, row, original: PublicationCreationRecord) -> None:
    node = ParserProposalRepository(conn).get_lineage_row_by_id(row["parser_output_id"])
    records = conn.execute(
        (
            "SELECT * FROM application_amendment_records WHERE resulting_"
            "parser_output_id=? AND publication_kind!='completion'"
        ),
        (row["parser_output_id"],),
    ).fetchall()
    if node is None or len(records) != 1:
        raise AiInvocationError("Independent AI field marker has no genuine HUMAN owner")
    from finance_core.parser_proposals.amendment_lineage import verify_amendment_record

    material = verify_amendment_record(conn, records[0])
    payload = json.loads(material["result_payload_json"])
    matches = [
        item
        for item in payload.get("field_evidence", [])
        if isinstance(item, dict)
        and all(
            item.get(k) == row[k]
            for k in (
                "field_name",
                "proposed_value",
                "evidence_source_type",
                "evidence_reference",
                "notes",
            )
        )
    ]
    parent = node["parent_parser_output_id"]
    seen: set[int] = set()
    while (
        parent is not None
        and parent != original.child_id
        and parent not in seen
        and len(seen) < 128
    ):
        seen.add(parent)
        ancestor = ParserProposalRepository(conn).get_lineage_row_by_id(parent)
        seals = _owner_rows(
            conn,
            "application_amendment_records",
            "resulting_parser_output_id=? AND publication_kind!='completion'",
            parent,
        )
        if (
            ancestor is None
            or len(seals) != 1
            or _owner_rows(conn, "independent_ai_publications", "child_parser_output_id=?", parent)
        ):
            raise AiInvocationError("Independent AI inherited field crossed another creation owner")
        inherited = json.loads(verify_amendment_record(conn, seals[0])["result_payload_json"])
        candidates = [
            item
            for item in inherited.get("field_evidence", [])
            if isinstance(item, dict)
            and all(
                item.get(k) == row[k]
                for k in (
                    "field_name",
                    "proposed_value",
                    "evidence_source_type",
                    "evidence_reference",
                    "notes",
                )
            )
            and item.get("confidence") == row["confidence_score"]
        ]
        if len(candidates) != 1:
            raise AiInvocationError("Independent AI inherited field continuity changed")
        parent = ancestor["parent_parser_output_id"]
    if (
        len(matches) != 1
        or parent != original.child_id
        or not any(
            all(
                row[k] == item[k]
                for k in (
                    "field_name",
                    "proposed_value",
                    "confidence_score",
                    "evidence_source_type",
                    "evidence_reference",
                    "notes",
                )
            )
            for item in original.seal["fields"]
        )
    ):
        raise AiInvocationError("Independent AI inherited field changed value/source")


def verify_independent_ai_lineage(
    conn: sqlite3.Connection,
    proposal: Any,
    *,
    content_hash: str,
    proposal_version: int,
    require_resolved: bool = True,
) -> dict[str, Any] | None:
    if not has_independent_ai_ancestry(conn, proposal):
        return None
    require_ai_schema(conn)
    repository = ParserProposalRepository(conn)
    subject = repository.get_lineage_row_by_id(proposal["id"])
    if subject is None:
        raise AiInvocationError("Independent AI lineage subject absent")
    if (
        compute_effective_proposal_content_hash(conn, subject) != content_hash
        or resolve_effective_payload(conn, subject)[2] != proposal_version
    ):
        raise AiInvocationError("Independent AI effective content changed")
    intake = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (subject["source_public_id"],),
    ).fetchone()
    current = repository.get_lineage_row_by_id(intake[0]) if intake else None
    seen: set[int] = set()
    attempts: list[str] = []
    text_ready = True
    successor = None
    from finance_core.parser_proposals.amendment_lineage import (
        verify_amendment_record,
    )

    while current is not None:
        if current["id"] in seen or len(seen) >= 128:
            raise AiInvocationError("Independent AI ancestry cycles or exceeds bound")
        seen.add(current["id"])
        if any(
            subject[k] != current[k]
            for k in (
                "source_type",
                "source_public_id",
                "statement_batch_id",
                "attachment_id",
                "raw_text",
            )
        ):
            raise AiInvocationError("Independent AI ancestry changed source")
        children = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM parser_outputs WHERE parent_parser_output_id=?", (current["id"],)
            )
        ]
        if children != ([] if successor is None else [successor]):
            raise AiInvocationError("Independent AI ancestry has an unsealed child/fork")
        from finance_core.parser_proposals.independent_ai_revision import ordered_revision_proof

        ownership = verify_node_ownership(conn, current)
        legacy_creation = ownership.creation if ownership.domain == "LEGACY_AI" else None
        ordered_revision_proof(conn, current, ownership.creation)
        parent_id = current["parent_parser_output_id"]
        if parent_id is None:
            root = current
            break
        human_edges = conn.execute(
            "SELECT * FROM application_amendment_records WHERE resulting_parser_output_id=? "
            "AND publication_kind!='completion'",
            (current["id"],),
        ).fetchall()
        if human_edges:
            if (
                len(human_edges) != 1
                or human_edges[0]["base_parser_output_id"] != parent_id
                or conn.execute(
                    "SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=?",
                    (current["id"],),
                ).fetchone()
            ):
                raise AiInvocationError("Mixed lineage owner edge is contradictory")
            verify_amendment_record(conn, human_edges[0])
        elif ownership.domain == "INDEPENDENT_AI":
            edge = conn.execute(
                "SELECT * FROM independent_ai_publications WHERE child_parser_output_id=?",
                (current["id"],),
            ).fetchone()
            if edge is None or edge["parent_parser_output_id"] != parent_id:
                raise AiInvocationError("Independent AI child has no actual sealed publication")
            material = verify_publication(conn, edge)
            response = conn.execute(
                "SELECT raw_response FROM independent_ai_results WHERE result_id=?",
                (edge["result_id"],),
            ).fetchone()[0]
            from finance_core.parser_proposals.independent_ai_source_money import (
                validate_source_response,
            )

            readiness = validate_source_response(conn, response, material, historical=True)[
                "text_readiness"
            ]
            text_ready = (
                text_ready
                and material["projection"]["selection"]["kind"] == "text"
                and not readiness["generic_score_applies"]
                and current["attachment_id"] is None
                and conn.execute(
                    "SELECT 1 FROM receipt_ocr_proposal_links WHERE parser_output_id=?",
                    (current["id"],),
                ).fetchone()
                is None
            )
            attempts.append(edge["attempt_id"])
        else:
            human = conn.execute(
                (
                    "SELECT * FROM application_amendment_records WHERE resulting_parse"
                    "r_output_id=? AND publication_kind!='completion'"
                ),
                (current["id"],),
            ).fetchall()
            if len(human) == 1 and human[0]["base_parser_output_id"] == parent_id:
                verify_amendment_record(conn, human[0])
            elif legacy_creation is not None:
                # Immutable creation is already proved; completions above are
                # separate genuine HUMAN records, never part of its v0 link.
                pass
            else:
                raise AiInvocationError("Independent AI ancestry contains an unsealed foreign edge")
        successor, current = current["id"], repository.get_lineage_row_by_id(parent_id)
    else:
        raise AiInvocationError("Independent AI ancestor absent")
    if subject["id"] not in seen or not attempts:
        raise AiInvocationError("Independent AI historical result outside current source path")
    payload, _, _ = resolve_effective_payload(conn, subject)
    flags = payload.get("ambiguity_flags", [])
    if not isinstance(flags, list):
        raise AiInvocationError("Independent AI ambiguity evidence malformed")
    # Receipt historical indicators are resolved exclusively by the unchanged
    # receipt HUMAN guard. AI never removes flags or raises OCR confidence.
    requires_resolution = bool(flags) and subject["attachment_id"] is None
    if require_resolved and requires_resolution:
        raise AiInvocationError("Independent AI requires unresolved source clarification")
    return {
        "proposal_origin": "independent_ai",
        "ai_source_kind": "receipt_ocr" if subject["attachment_id"] else "text",
        "ambiguity_flags": flags,
        "requires_resolution": requires_resolution,
        "root_proposal": root,
        "subject_parser_output_id": subject["id"],
        "attempt_ids": attempts,
        "amendment_id": None,
        "text_readiness": {
            "schema": "finance-independent-ai-text-readiness-v1",
            "generic_score_applies": not text_ready,
        },
    }


_CLARIFICATION_SCHEMA_HASH = "5839a9b56363288daf23266fc8cc9be2955be50243f7a4b60789912f2c773b63"


def require_clarification_schema(conn: sqlite3.Connection) -> None:
    require_ai_schema(conn)
    rows = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name LIKE "
        "'independent_ai_clarification_%' OR name LIKE 'independent_ai_followup_%' ORDER BY "
        "type,name"
    ).fetchall()
    digest = hashlib.sha256(
        json.dumps([tuple(row) for row in rows], separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if digest != _CLARIFICATION_SCHEMA_HASH:
        raise AiInvocationError("Independent AI migration 059 absent or changed")
