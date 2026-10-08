"""Read-only neutral independent human amendment publication lineage.

These seals describe actual owner effects, not an alternate effective payload.
Every intervening completion must belong to exactly one immutable seal.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Protocol

from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.financial_audit import (
    FinancialAuditRepository,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.parser_proposals.content_hash import compute_proposal_content_hash
from finance_core.parser_proposals.lifecycle import raw_intake_status_for_proposal_status
from finance_core.parser_proposals.repository import ParserProposalRepository

TEXT_AMENDMENT_PARSER = "application_human_amendment"
TEXT_AMENDMENT_VERSION = "v1"


class AmendmentLineageError(ValueError):
    """An independent edit edge or actual publication is contradictory."""


class AmendmentPublicationAuthority(Protocol):
    def verify_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        persisted_operation: dict[str, Any] | None,
        proposal: dict[str, Any],
        canonical_patch: dict[str, Any],
    ) -> None: ...
    def persist_effect_in_transaction(
        self, connection: sqlite3.Connection, *, publication_result: dict[str, Any]
    ) -> None: ...


def sha(value: object) -> str:
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


def has_amendment_schema(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='application_amendment_records'"
        ).fetchone()
        is not None
    )


def _object(text: str) -> dict[str, Any]:
    value = canonical_json_value(text, label="amendment material")
    if not isinstance(value, dict) or canonical_json_text(value) != text:
        raise AmendmentLineageError("Amendment material is not canonical")
    return value


def payload_hash(
    conn: sqlite3.Connection, proposal: dict[str, Any], payload: dict[str, Any]
) -> str:
    return compute_proposal_content_hash(
        conn,
        {
            **proposal,
            "parsed_payload": json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        },
    )


def verify_amendment_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    material = _object(row["material_json"])
    if sha(material) != row["record_hash"]:
        raise AmendmentLineageError("Amendment seal hash does not verify")
    review = conn.execute(
        "SELECT * FROM application_amendment_reviews WHERE review_id=?", (row["review_id"],)
    ).fetchone()
    if review is None:
        raise AmendmentLineageError("Amendment old display review is absent")
    old = _object(review["material_json"])
    from finance_core.application.amendment_contract import (
        amendment_patch_sha256,
        amendment_review_sha256,
    )

    if (
        amendment_review_sha256(old) != review["review_hash"]
        or material["review_hash"] != review["review_hash"]
    ):
        raise AmendmentLineageError("Amendment full old review seal does not verify")
    proof = material["accepted_proof"]
    patch = material["canonical_patch"]
    applied = material["material_patch"]
    binding = old["binding"]
    source = old["source"]
    if (
        proof["schema"] != "finance-application-human-amendment-v1"
        or proof["namespace"] != binding["amendment_namespace"]
        or proof["key_id"] != binding["amendment_key_id"]
        or proof["instance_id"] != binding["binding"]["instance_id"]
        or proof["human_principal_id"] != binding["binding"]["human_principal_id"]
        or proof["evidence_id"] != row["evidence_id"]
        or proof["review_id"] != row["review_id"]
        or proof["review_projection_hash"] != review["review_hash"]
        or proof["source_evidence_id"] != source["evidence_id"]
        or proof["source_evidence_digest"] != source["evidence_digest"]
        or proof["intake_public_id"] != source["intake_public_id"]
        or proof["source_event_id"] != source["source_event_id"]
        or row["source_event_key"]
        != sha([source["namespace"], source["instance_id"], source["source_event_id"]])
        or row["amendment_namespace"] != binding["amendment_namespace"]
        or proof["action"] != "amend"
        or proof["revoked"] is not False
        or proof["consumed"] is not False
        or proof["reply_to_display_id"] != proof["display_id"]
        or material["accepted_at"] != row["accepted_at"]
        or not old["created_at"]
        <= proof["observed_at"]
        <= proof["issued_at"]
        <= row["accepted_at"]
        <= proof["expires_at"]
    ):
        raise AmendmentLineageError("Accepted amendment authority/source/display binding changed")
    if (
        proof["patch"] != patch
        or proof["patch_digest"] != amendment_patch_sha256(patch)
        or material["patch_digest"] != amendment_patch_sha256(patch)
        or material["material_patch_digest"] != amendment_patch_sha256(applied)
    ):
        raise AmendmentLineageError("Amendment signed whole patch does not verify")
    base = ParserProposalRepository(conn).get_lineage_row_by_id(row["base_parser_output_id"])
    child = ParserProposalRepository(conn).get_lineage_row_by_id(row["resulting_parser_output_id"])
    if base is None or child is None:
        raise AmendmentLineageError("Amendment base/result proposal is absent")
    for key in (
        "source_type",
        "source_public_id",
        "statement_batch_id",
        "attachment_id",
        "raw_text",
    ):
        if base[key] != child[key]:
            raise AmendmentLineageError("Amendment source evidence changed")
    base_payload = json.loads(material["base_payload_json"])
    result_payload = json.loads(material["result_payload_json"])
    if (
        payload_hash(conn, base, base_payload) != row["base_content_hash"]
        or payload_hash(conn, child, result_payload) != row["resulting_content_hash"]
    ):
        raise AmendmentLineageError("Amendment revision content hash does not verify")
    if (
        material["publication_kind"] != row["publication_kind"]
        or material["publication_public_id"] != row["publication_public_id"]
        or material["base_version"] != row["base_version"]
        or material["resulting_version"] != row["resulting_version"]
    ):
        raise AmendmentLineageError("Amendment row/result binding changed")
    old_view = old["proposal_review"]
    if (
        old_view["proposal_public_id"] != base["public_id"]
        or old_view["proposal_version"] != row["base_version"]
        or old_view["effective_content_hash"] != row["base_content_hash"]
        or json.loads(old["effective_payload_json"]) != base_payload
    ):
        raise AmendmentLineageError("Amendment base is not its immutable reviewed revision")
    if row["publication_kind"] == "completion":
        publication = conn.execute(
            "SELECT * FROM parser_proposal_completions WHERE completion_public_id=?",
            (row["publication_public_id"],),
        ).fetchone()
        if (
            publication is None
            or publication["parser_output_id"] != base["id"]
            or child["id"] != base["id"]
            or publication["version_number"] != row["resulting_version"]
            or publication["base_content_hash"] != row["base_content_hash"]
            or publication["completed_content_hash"] != row["resulting_content_hash"]
            or json.loads(publication["field_updates_json"]) != applied
            or json.loads(publication["completed_payload_json"]) != result_payload
            or result_payload != {**base_payload, **applied}
        ):
            raise AmendmentLineageError("Actual completion does not match its independent seal")
    else:
        if (
            child["parent_parser_output_id"] != base["id"]
            or base["parse_status"] != "superseded"
            or row["resulting_version"] != 0
        ):
            raise AmendmentLineageError("Amendment child topology or lifecycle changed")
        children = conn.execute(
            "SELECT id FROM parser_outputs WHERE parent_parser_output_id=?", (base["id"],)
        ).fetchall()
        if len(children) != 1 or children[0][0] != child["id"]:
            raise AmendmentLineageError("Amendment ancestry forks")
        if row["publication_kind"] == "text_supersession":
            edge = conn.execute(
                "SELECT * FROM parser_text_amendment_revisions WHERE publication_public_id=?",
                (row["publication_public_id"],),
            ).fetchone()
            if (
                edge is None
                or edge["amendment_id"] != row["amendment_id"]
                or edge["parent_parser_output_id"] != base["id"]
                or edge["child_parser_output_id"] != child["id"]
                or json.loads(edge["child_payload_json"]) != result_payload
                or json.loads(child["parsed_payload"]) != result_payload
                or result_payload != {**base_payload, **applied}
                or child["parser_name"] != TEXT_AMENDMENT_PARSER
                or child["parser_version"] != TEXT_AMENDMENT_VERSION
            ):
                raise AmendmentLineageError("Text amendment edge does not verify")
        else:
            revision = conn.execute(
                "SELECT * FROM receipt_proposal_revisions WHERE correction_public_id=?",
                (row["publication_public_id"],),
            ).fetchone()
            intake = conn.execute(
                "SELECT id FROM raw_intake_records WHERE public_id=?", (base["source_public_id"],)
            ).fetchone()
            if (
                revision is None
                or intake is None
                or revision["superseded_parser_output_id"] != base["id"]
                or revision["replacement_parser_output_id"] != child["id"]
                or json.loads(revision["field_updates_json"]) != patch
                or json.loads(revision["applied_field_updates_json"]) != applied
                or json.loads(revision["replacement_payload_json"]) != result_payload
            ):
                raise AmendmentLineageError("Receipt amendment revision does not verify")
            # Historical replay may have a later current leaf. Verify immutable
            # owner result directly; current-leaf equality belongs to fresh admission.
            link = conn.execute(
                "SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id=?", (child["id"],)
            ).fetchone()
            if (
                link is None
                or link["link_role"] != "superseding_correction"
                or link["proposal_result_hash"]
                != hashlib.sha256(
                    json.dumps(
                        result_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
            ):
                raise AmendmentLineageError("Receipt amendment OCR link does not verify")
            from finance_core.parser_proposals.human_revision import (
                verify_independent_receipt_revision_creation_edge,
            )

            verify_independent_receipt_revision_creation_edge(
                conn, child, content_hash=row["resulting_content_hash"]
            )

    invalidation = conn.execute(
        "SELECT * FROM application_amendment_invalidations WHERE amendment_id=?",
        (row["amendment_id"],),
    ).fetchone()
    if (
        invalidation is None
        or invalidation["base_parser_output_id"] != base["id"]
        or invalidation["base_version"] != row["base_version"]
        or invalidation["base_content_hash"] != row["base_content_hash"]
    ):
        raise AmendmentLineageError("Immutable old-review invalidation is absent")
    if row["publication_kind"] != "receipt_supersession":
        _verify_owner_history(conn, row, material, base, child)
    return material


def _verify_owner_history(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    material: dict[str, Any],
    base: dict[str, Any],
    child: dict[str, Any],
) -> None:
    actor = material["accepted_proof"]["human_principal_id"]
    publication_id = row["publication_public_id"]
    changed = sorted(material["material_patch"])
    if row["publication_kind"] == "completion":
        event_type = "parser_proposal_completed"
        event_payload = {
            "completion_public_id": publication_id,
            "version_number": row["resulting_version"],
            "previous_content_hash": row["base_content_hash"],
            "new_content_hash": row["resulting_content_hash"],
            "changed_fields": changed,
        }
        previous = {
            "parse_status": material["base_status"],
            "raw_intake_status": raw_intake_status_for_proposal_status(material["base_status"]),
            "proposal_content_hash": row["base_content_hash"],
            "conversion_status": "not_converted",
        }
        new = {
            "parse_status": "edited_pending_confirmation",
            "raw_intake_status": "parsed_pending_confirmation",
            "proposal_content_hash": row["resulting_content_hash"],
            "conversion_status": "not_converted",
        }
        lifecycle_payload = {**event_payload, "actor_type": "human"}
        events = conn.execute(
            "SELECT * FROM parser_proposal_events WHERE parser_output_id=? AND "
            "event_type='edited' AND event_reason=?",
            (base["id"], f"completion v{row['resulting_version']}"),
        ).fetchall()
        if (
            len(events) != 1
            or events[0]["from_status"] != material["base_status"]
            or events[0]["to_status"] != "edited_pending_confirmation"
            or events[0]["actor_type"] != "user"
            or events[0]["actor_identifier"] != actor
            or json.loads(events[0]["event_payload"]) != lifecycle_payload
        ):
            raise AmendmentLineageError("Independent completion lifecycle evidence changed")
    else:
        event_type = "text_proposal_superseded"
        event_payload = {
            "publication_public_id": publication_id,
            "replacement_proposal_public_id": child["public_id"],
            "previous_content_hash": row["base_content_hash"],
            "replacement_content_hash": row["resulting_content_hash"],
            "changed_fields": changed,
        }
        previous = {
            "parse_status": material["base_status"],
            "raw_intake_status": raw_intake_status_for_proposal_status(material["base_status"]),
            "proposal_content_hash": row["base_content_hash"],
            "conversion_status": "not_converted",
        }
        new = {
            "parse_status": "superseded",
            "raw_intake_status": "parsed_pending_confirmation",
            "proposal_content_hash": row["base_content_hash"],
            "conversion_status": "not_converted",
            "replacement_proposal_public_id": child["public_id"],
            "replacement_content_hash": row["resulting_content_hash"],
        }
        parent_events = conn.execute(
            "SELECT * FROM parser_proposal_events WHERE parser_output_id=? AND "
            "event_type='superseded' AND event_reason=?",
            (base["id"], f"superseding correction {publication_id}"),
        ).fetchall()
        child_events = conn.execute(
            "SELECT * FROM parser_proposal_events WHERE parser_output_id=? AND "
            "event_type='created' AND event_reason=?",
            (child["id"], f"replacement for correction {publication_id}"),
        ).fetchall()
        expected_parent = {
            "actor_type": "human",
            "correction_public_id": publication_id,
            "replacement_proposal_public_id": child["public_id"],
            "previous_content_hash": row["base_content_hash"],
            "replacement_content_hash": row["resulting_content_hash"],
            "changed_fields": changed,
        }
        expected_child = {
            "actor_type": "human",
            "correction_public_id": publication_id,
            "superseded_proposal_public_id": base["public_id"],
        }
        if (
            len(parent_events) != 1
            or len(child_events) != 1
            or parent_events[0]["from_status"] != material["base_status"]
            or parent_events[0]["to_status"] != "superseded"
            or child_events[0]["from_status"] is not None
            or child_events[0]["to_status"] != "parsed_pending_confirmation"
            or json.loads(parent_events[0]["event_payload"]) != expected_parent
            or json.loads(child_events[0]["event_payload"]) != expected_child
            or any(
                e["actor_type"] != "user" or e["actor_identifier"] != actor
                for e in [parent_events[0], child_events[0]]
            )
        ):
            raise AmendmentLineageError("Independent text child lifecycle evidence changed")
    audit_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=base["public_id"],
        event_type=event_type,
        causation_public_id=publication_id,
    )
    audit = FinancialAuditRepository(conn).fetch(audit_id)
    chain = verify_financial_audit_chain(
        conn, aggregate_type="parser_proposal", aggregate_public_id=base["public_id"]
    )
    if (
        audit is None
        or not chain.valid
        or audit.actor_type != "human"
        or audit.actor_public_id != actor
        or audit.authorization_public_id != publication_id
        or audit.causation_public_id != publication_id
        or canonical_json_value(audit.event_payload_json, label="amendment publication audit")
        != event_payload
        or canonical_json_value(audit.previous_state_json, label="amendment previous state")
        != previous
        or canonical_json_value(audit.new_state_json, label="amendment new state") != new
    ):
        raise AmendmentLineageError("Independent amendment financial audit evidence changed")


def verify_independent_amendment_descendant(
    conn: sqlite3.Connection, proposal: Any, *, content_hash: str, proposal_version: int
) -> dict[str, Any] | None:
    if not has_amendment_schema(conn):
        if proposal["parser_name"] == TEXT_AMENDMENT_PARSER:
            raise AmendmentLineageError("Independent text amendment schema is absent")
        return None
    subject = ParserProposalRepository(conn).get_lineage_row_by_id(int(proposal["id"]))
    if subject is None:
        raise AmendmentLineageError("Amendment subject is absent")
    ancestry: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = subject
    touched = False
    while True:
        if current["id"] in seen:
            raise AmendmentLineageError("Amendment ancestry cycles")
        seen.add(current["id"])
        ancestry.append(current)
        rows = conn.execute(
            "SELECT * FROM application_amendment_records WHERE resulting_parser_output_id=?"
            " ORDER BY resulting_version",
            (current["id"],),
        ).fetchall()
        for row in rows:
            verify_amendment_record(conn, row)
        touched = touched or bool(rows)
        if current["parser_name"] == TEXT_AMENDMENT_PARSER and not any(
            row["publication_kind"] == "text_supersession" for row in rows
        ):
            raise AmendmentLineageError("Independent text child has no sealed edge")
        parent_id = current["parent_parser_output_id"]
        if parent_id is None:
            break
        edge = next((row for row in rows if row["publication_kind"] != "completion"), None)
        if edge is None:
            if touched:
                # An AI child is the independent root, verified by its own
                # sealed result/admission owner rather than an amendment edge.
                ai = conn.execute(
                    "SELECT 1 FROM ai_fallback_proposal_links WHERE parser_output_id=?",
                    (current["id"],),
                ).fetchone()
                if ai is None:
                    raise AmendmentLineageError("Unsealed ancestor is not an independent root")
            break
        if edge["base_parser_output_id"] != parent_id:
            raise AmendmentLineageError("Amendment parent binding changed")
        parent = ParserProposalRepository(conn).get_lineage_row_by_id(parent_id)
        if parent is None:
            raise AmendmentLineageError("Amendment parent is absent")
        current = parent
    if not touched:
        return None
    from finance_core.application.amendment_contract import require_amendment_schema

    require_amendment_schema(conn)
    for ancestor in ancestry:
        completions = conn.execute(
            "SELECT * FROM parser_proposal_completions WHERE parser_output_id=? ORDER BY "
            "version_number",
            (ancestor["id"],),
        ).fetchall()
        previous = json.loads(ancestor["parsed_payload"])
        previous_hash = payload_hash(conn, ancestor, previous)
        for version, completion in enumerate(completions, 1):
            seal = conn.execute(
                "SELECT * FROM application_amendment_records WHERE publication_public_id=? "
                "AND publication_kind='completion'",
                (completion["completion_public_id"],),
            ).fetchone()
            if (
                seal is None
                or completion["version_number"] != version
                or completion["base_content_hash"] != previous_hash
                or seal["base_version"] != version - 1
            ):
                raise AmendmentLineageError("Intervening completion is unsealed or discontinuous")
            previous = json.loads(completion["completed_payload_json"])
            previous_hash = payload_hash(conn, ancestor, previous)
    latest = conn.execute(
        "SELECT * FROM application_amendment_records WHERE resulting_parser_output_id=? "
        "ORDER BY resulting_version DESC LIMIT 1",
        (subject["id"],),
    ).fetchone()
    if (
        latest is None
        or latest["resulting_version"] != proposal_version
        or latest["resulting_content_hash"] != content_hash
    ):
        raise AmendmentLineageError("Current amendment revision has no exact independent seal")
    root = ancestry[-1]
    return {
        "root_proposal": root,
        "subject_parser_output_id": subject["id"],
        "amendment_id": latest["amendment_id"],
    }


def refuse_legacy_amendment(conn: sqlite3.Connection, proposal: Any) -> None:
    if (
        has_amendment_schema(conn)
        and conn.execute(
            "SELECT 1 FROM application_amendment_records WHERE resulting_parser_output_id=?",
            (proposal["id"],),
        ).fetchone()
        is not None
        or proposal["parser_name"] == TEXT_AMENDMENT_PARSER
    ):
        raise AmendmentLineageError(
            "Independent amendments require their independent decision owner"
        )


def require_independent_source_edit_history(
    conn: sqlite3.Connection, proposal: dict[str, Any]
) -> None:
    """An original source or completely sealed independent history is eligible.

    A channel, authenticated source, or a version-zero legacy child never
    substitutes for durable human edit authority.
    """
    full = ParserProposalRepository(conn).get_lineage_row_by_id(proposal["id"])
    if full is None:
        raise AmendmentLineageError("Independent source proposal is absent")
    schema = has_amendment_schema(conn)
    seal = (
        conn.execute(
            "SELECT 1 FROM application_amendment_records WHERE resulting_parser_output_id=?",
            (full["id"],),
        ).fetchone()
        if schema
        else None
    )
    if full["parent_parser_output_id"] is not None and seal is None:
        ai = conn.execute(
            "SELECT 1 FROM ai_fallback_proposal_links WHERE parser_output_id=?", (full["id"],)
        ).fetchone()
        if ai is None:
            raise AmendmentLineageError("Unsealed legacy child cannot gain independent authority")
        from finance_core.parser_proposals.ai_fallback import verify_ai_fallback_child
        from finance_core.parser_proposals.content_hash import (
            compute_effective_proposal_content_hash,
        )
        from finance_core.parser_proposals.effective_payload import resolve_effective_payload

        if (
            verify_ai_fallback_child(
                conn,
                full,
                content_hash=compute_effective_proposal_content_hash(conn, full),
                proposal_version=resolve_effective_payload(conn, full)[2],
                require_resolved=False,
            )
            is None
        ):
            raise AmendmentLineageError("Independent AI source does not verify")
    for owner_table, identity, kind in (
        ("parser_proposal_completions", "completion_public_id", "completion"),
        ("receipt_proposal_revisions", "correction_public_id", "receipt_supersession"),
    ):
        target = "parser_output_id" if kind == "completion" else "replacement_parser_output_id"
        join = (
            (
                "LEFT JOIN application_amendment_records AS a ON a.publication_public_id=history."
                + identity
                + " AND a.publication_kind=? "
            )
            if schema
            else ""
        )
        absent = "AND a.amendment_id IS NULL " if schema else ""
        params = (kind, full["source_public_id"]) if schema else (full["source_public_id"],)
        row = conn.execute(
            "SELECT 1 FROM "
            + owner_table
            + " AS history JOIN parser_outputs AS p ON p.id=history."
            + target
            + " "
            + join
            + "WHERE p.source_public_id=? "
            + absent
            + "LIMIT 1",
            params,
        ).fetchone()
        if row is not None:
            raise AmendmentLineageError("Source event contains unsealed legacy edit history")
