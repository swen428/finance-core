"""Reconstruct an AI attempt's real revision without granting live authority."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.parser_proposals.amendment_lineage import payload_hash, verify_amendment_record
from finance_core.parser_proposals.independent_ai_validation import canonical_text
from finance_core.parser_proposals.repository import ParserProposalRepository


@dataclass(frozen=True)
class ProposalRevision:
    version: int
    payload: dict[str, Any]
    content_hash: str
    amendment_id: str | None
    publication_id: str | None


@dataclass(frozen=True)
class OrderedProposalRevisions:
    proposal_id: int
    proposal_public_id: str
    revisions: tuple[ProposalRevision, ...]


def ordered_revision_proof(
    conn: sqlite3.Connection, proposal: dict[str, Any], creation=None
) -> OrderedProposalRevisions:
    original = json.loads(proposal["parsed_payload"])
    if not isinstance(original, dict):
        raise AiInvocationError("Original parser payload is not an object")
    if creation is not None and (
        canonical_text(creation.creation_payload) != canonical_text(original)
        or creation.creation_content_hash != payload_hash(conn, proposal, original)
    ):
        raise AiInvocationError("Source revision original creation changed")
    revisions = [original]
    typed_revisions = [
        ProposalRevision(0, original, payload_hash(conn, proposal, original), None, None)
    ]
    previous_hash = payload_hash(conn, proposal, original)
    completions = conn.execute(
        "SELECT * FROM parser_proposal_completions WHERE parser_output_id=? "
        "ORDER BY version_number",
        (proposal["id"],),
    ).fetchall()
    records = conn.execute(
        "SELECT * FROM application_amendment_records WHERE resulting_parser_output_id=? "
        "AND publication_kind='completion' ORDER BY resulting_version",
        (proposal["id"],),
    ).fetchall()
    if len(records) != len(completions):
        raise AiInvocationError("Source completion owner ledger is incomplete")
    for version, completion in enumerate(completions, 1):
        seals = [
            row
            for row in records
            if row["publication_public_id"] == completion["completion_public_id"]
        ]
        if len(seals) != 1:
            raise AiInvocationError("Source completion has no unique HUMAN seal")
        seal = seals[0]
        material = verify_amendment_record(conn, seal)
        completed = json.loads(completion["completed_payload_json"])
        if (
            type(completion["version_number"]) is not int
            or completion["version_number"] != version
            or seal["base_parser_output_id"] != proposal["id"]
            or seal["resulting_parser_output_id"] != proposal["id"]
            or seal["base_version"] != version - 1
            or seal["resulting_version"] != version
            or completion["base_content_hash"] != previous_hash
            or seal["base_content_hash"] != previous_hash
            or canonical_text(json.loads(material["base_payload_json"]))
            != canonical_text(revisions[-1])
            or canonical_text(json.loads(material["result_payload_json"]))
            != canonical_text(completed)
            or canonical_text(completed)
            != canonical_text({**revisions[-1], **material["material_patch"]})
            or payload_hash(conn, proposal, completed) != completion["completed_content_hash"]
        ):
            raise AiInvocationError("Source completion revisions are discontinuous")
        revisions.append(completed)
        previous_hash = payload_hash(conn, proposal, completed)
        typed_revisions.append(
            ProposalRevision(
                version,
                completed,
                previous_hash,
                seal["amendment_id"],
                completion["completion_public_id"],
            )
        )
    return OrderedProposalRevisions(proposal["id"], proposal["public_id"], tuple(typed_revisions))


def _revisions(conn: sqlite3.Connection, proposal: dict[str, Any]) -> list[dict[str, Any]]:
    return [revision.payload for revision in ordered_revision_proof(conn, proposal).revisions]


def verified_source_revision(
    conn: sqlite3.Connection,
    material: dict[str, Any],
    *,
    historical: bool = False,
) -> dict[str, Any]:
    """Verify the whole actual source chain, then select the captured revision.

    No cached payload is evidence. The default still requires the latest
    physical proposal revision; historical callers only receive a verified
    old payload, never permission to claim, publish or change the leaf.
    """
    from finance_core.application.amendment_contract import require_amendment_schema

    require_amendment_schema(conn)
    repository = ParserProposalRepository(conn)
    parent = repository.get_lineage_row_by_id(material["parent_id"])
    if (
        parent is None
        or parent["public_id"] != material["parent_public_id"]
        or parent["source_public_id"] != material["intake_public_id"]
    ):
        raise AiInvocationError("Original source base identity changed")
    intake = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
        (material["intake_public_id"],),
    ).fetchone()
    current = repository.get_lineage_row_by_id(intake[0]) if intake else None
    seen: set[int] = set()
    successor = None
    selected = None
    selected_latest = None
    while current is not None:
        if current["id"] in seen or len(seen) >= 128:
            raise AiInvocationError("Source revision ancestry cycles or exceeds bound")
        seen.add(current["id"])
        if any(
            current[key] != parent[key]
            for key in (
                "source_type",
                "source_public_id",
                "statement_batch_id",
                "attachment_id",
                "raw_text",
            )
        ):
            raise AiInvocationError("Source revision ancestry changed custody")
        children = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM parser_outputs WHERE parent_parser_output_id=?", (current["id"],)
            )
        ]
        if children != ([] if successor is None else [successor]):
            raise AiInvocationError("Source revision ancestry has an unsealed child or fork")
        from finance_core.parser_proposals.independent_ai_lineage import verify_node_ownership

        ownership = verify_node_ownership(conn, current)
        legacy_creation = ownership.creation if ownership.domain == "LEGACY_AI" else None
        independent_creation = ownership.creation if ownership.domain == "INDEPENDENT_AI" else None
        revision_proof = ordered_revision_proof(conn, current, ownership.creation)
        revisions = [revision.payload for revision in revision_proof.revisions]
        if current["id"] == parent["id"]:
            version = material["parent_version"]
            if type(version) is not int or not 0 <= version < len(revisions):
                raise AiInvocationError("Original source revision does not exist")
            selected, selected_latest = revisions[version], len(revisions) - 1
        parent_id = current["parent_parser_output_id"]
        if parent_id is None:
            break
        human = conn.execute(
            "SELECT * FROM application_amendment_records WHERE resulting_parser_output_id=? "
            "AND publication_kind!='completion'",
            (current["id"],),
        ).fetchall()
        ai = conn.execute(
            "SELECT parent_parser_output_id FROM independent_ai_publications "
            "WHERE child_parser_output_id=?",
            (current["id"],),
        ).fetchall()
        if human:
            if len(human) != 1 or ai or human[0]["base_parser_output_id"] != parent_id:
                raise AiInvocationError("Source revision owner edge is contradictory")
            verified = verify_amendment_record(conn, human[0])
            if canonical_text(json.loads(verified["result_payload_json"])) != canonical_text(
                revisions[0]
            ):
                raise AiInvocationError("Source revision original child payload changed")
        elif len(ai) == 1 and ai[0][0] == parent_id:
            if independent_creation is None or independent_creation.parent_id != parent_id:
                raise AiInvocationError("Source revision independent creation owner absent")
        elif legacy_creation is None:
            raise AiInvocationError("Source revision has no authenticated owning edge")
        successor = current["id"]
        current = repository.get_lineage_row_by_id(parent_id)
        if current is None:
            raise AiInvocationError("Source revision parent absent")
    if selected is None:
        raise AiInvocationError("Original source base is outside current ancestry")
    if (
        canonical_text(selected) != canonical_text(material["base_payload"])
        or payload_hash(conn, parent, selected) != material["parent_content_hash"]
    ):
        raise AiInvocationError("Original source base revision payload or hash changed")
    if not historical and selected_latest != material["parent_version"]:
        raise AiInvocationError("Original source base changed")
    return selected
