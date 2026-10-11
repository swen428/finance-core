"""Current v2 interaction owner; fixed ports and immutable clarification frontier."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from finance_core.application.admission import VerifiedSource, _digest, _reference
from finance_core.application.ai_invocation_contract import (
    AI_INPUT_SCHEMA,
    AI_INPUT_SCHEMA_V1,
    AI_INTERPRETATION_SCHEMA,
    AiInvocationError,
    AiInvocationStatus,
    ClarificationReview,
    ClarificationStatus,
    ExpectedInterpretationInputV2,
    VerifiedInterpretationInputV2,
)
from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.independent_ai_validation import (
    FIELDS,
    assets,
    check_private_text,
    sha,
)
from finance_core.parser_proposals.independent_ai_validation import (
    canonical_text as canonical_json_text,
)
from finance_core.parser_proposals.repository import ParserProposalRepository

CLARIFICATION_SCHEMA = "finance-independent-ai-clarification-review-v2"


def _seal(row):
    material = json.loads(row["material_json"])
    if (
        not isinstance(material, dict)
        or canonical_json_text(material) != row["material_json"]
        or sha(material) != row["material_hash"]
    ):
        raise AiInvocationError("Interaction evidence seal changed")
    return material


# The public module defines its historical base before importing this owner.
from finance_core.application.ai_invocation import _AiInvocationServiceV1  # noqa: E402


class AiInvocationV2Mixin(_AiInvocationServiceV1):
    def _v2_schema(self):
        from finance_core.parser_proposals.independent_ai_lineage import (
            require_clarification_schema,
        )

        require_clarification_schema(self._conn)

    def prepare_initial(self, intake_id: str, operation_id: str):
        return self._prepare_v2(intake_id, operation_id)

    def _interaction(self, identity: str, *, historical: bool = False):
        row = self._conn.execute(
            "SELECT * FROM application_posting_reviews WHERE review_id=?", (identity,)
        ).fetchone()
        if row is not None:
            from finance_core.application.posting_contract import (
                POSTING_REVIEW_SCHEMA_V2,
                posting_review_sha256,
            )

            material = canonical_json_value(row["material_json"])
            if not isinstance(material, dict):
                raise AiInvocationError("Interpretation full card is not an object")
            if (
                material.get("schema") != POSTING_REVIEW_SCHEMA_V2
                or posting_review_sha256(material) != row["review_hash"]
            ):
                raise AiInvocationError("Interpretation requires actual full PostingReview v2")
            return {
                "kind": "card_edit",
                "id": identity,
                "hash": row["review_hash"],
                "projection": material,
                "intake": material["source"]["intake_public_id"],
                "parent_id": row["parser_output_id"],
                "version": material["proposal_version"],
                "content_hash": material["effective_content_hash"],
                "created_at": material["prepared_at"],
                "expires_at": material["expires_at"],
                "predecessor": None,
            }
        row = self._conn.execute(
            "SELECT * FROM independent_ai_clarification_requests WHERE request_id=?", (identity,)
        ).fetchone()
        if row is not None:
            material = self._verify_clarification(row)
            if not historical and self._clarification_state(row) != "OPEN":
                raise AiInvocationError("Clarification is not the open current frontier")
            return {
                "kind": "clarification_followup",
                "id": identity,
                "hash": row["material_hash"],
                "projection": material,
                "intake": row["intake_public_id"],
                "parent_id": row["parent_parser_output_id"],
                "version": row["parent_version"],
                "content_hash": row["parent_content_hash"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "predecessor": row["attempt_id"],
            }
        # Explicit old amendment-review compatibility, never an implicit v2
        # Posting or clarification display.
        row = self._conn.execute(
            "SELECT * FROM application_amendment_reviews WHERE review_id=?", (identity,)
        ).fetchone()
        if row is None:
            raise AiInvocationError("Interpretation retained display absent")
        from finance_core.application.amendment_contract import amendment_review_sha256

        material = canonical_json_value(row["material_json"])
        if not isinstance(material, dict):
            raise AiInvocationError("Compatibility interaction is not an object")
        if amendment_review_sha256(material) != row["review_hash"]:
            raise AiInvocationError("Compatibility interpretation review seal changed")
        return {
            "kind": "card_edit",
            "id": identity,
            "hash": row["review_hash"],
            "projection": material,
            "intake": row["intake_public_id"],
            "parent_id": row["parser_output_id"],
            "version": row["base_version"],
            "content_hash": row["base_content_hash"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "predecessor": None,
            "compatibility_schema": material["schema"],
        }

    def prepare_edit(self, review_id: str, reply_evidence_id: str, operation_id: str):
        self._v2_schema()
        interaction = self._interaction(review_id)
        if interaction["kind"] != "card_edit":
            raise AiInvocationError("Edit requires a delivered complete card")
        return self._prepare_v2(interaction["intake"], operation_id, interaction, reply_evidence_id)

    def prepare_followup(self, request_id: str, reply_evidence_id: str, operation_id: str):
        self._v2_schema()
        # Replays check the sealed operation first, without reopening consumed
        # request permission or granting a second dispatch.
        existing = self._conn.execute(
            "SELECT * FROM independent_ai_attempts WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            old = _seal(existing)
            if (
                old.get("interaction", {}).get("subject_id") != request_id
                or old["input_identity"] != reply_evidence_id
            ):
                raise AiInvocationError("AI operation collision")
            return self.get_status(existing["attempt_id"])
        interaction = self._interaction(request_id)
        if interaction["kind"] != "clarification_followup":
            raise AiInvocationError("Followup requires an authenticated question")
        return self._prepare_v2(interaction["intake"], operation_id, interaction, reply_evidence_id)

    def _proof(
        self, interaction, source, proposal, version, digest, reply_id, now, *, historical=False
    ):
        expected = ExpectedInterpretationInputV2(
            self._binding,
            source,
            interaction["kind"],
            interaction["id"],
            interaction["hash"],
            proposal["public_id"],
            version,
            digest,
            now,
            historical,
        )
        proof = self._input_port.verify_persisted(self._conn, reply_id, expected)
        if (
            type(proof) is not VerifiedInterpretationInputV2
            or proof.schema != AI_INTERPRETATION_SCHEMA
            or proof.instance_id != self._binding.instance_id
            or proof.human_principal_id != self._binding.human_principal_id
            or proof.evidence_id != reply_id
            or not _digest(proof.evidence_digest)
            or proof.source_evidence_id != source.evidence_id
            or proof.source_evidence_digest != source.evidence_digest
            or proof.interaction_kind != interaction["kind"]
            or proof.interaction_id != interaction["id"]
            or proof.interaction_projection_hash != interaction["hash"]
            or proof.proposal_public_id != proposal["public_id"]
            or type(proof.proposal_version) is not int
            or proof.proposal_version != version
            or proof.proposal_content_hash != digest
            or not _reference(proof.display_id)
            or not _digest(proof.display_evidence_digest)
            or proof.reply_to_display_id != proof.display_id
            or proof.revoked is not False
            or type(proof.observed_at) is not int
            or type(proof.expires_at) is not int
            or not interaction["created_at"] <= proof.observed_at <= now < proof.expires_at
            or not isinstance(proof.text, str)
            or not 0 < len(proof.text.encode()) <= 2048
        ):
            raise AiInvocationError("Interpretation reply binding unavailable")
        return proof

    def _projection_v2(self, proposal, source, payload, interaction, proof):
        # Retain original OCR/text selection owner and privacy checks. Only
        # ordinary edit suppresses original source; followup supplies it.
        original = super()._projection(proposal, "initial", None, source, payload=payload)
        anchor = source.source_occurred_at if proof is None else proof.observed_at
        context = {
            "anchor_kind": "source" if proof is None else "reply",
            "anchor_timestamp": anchor,
            "timezone": self._policy.timezone,
            "local_date": datetime.fromtimestamp(anchor, ZoneInfo(self._policy.timezone))
            .date()
            .isoformat(),
            "default_currency": self._policy.default_currency,
        }
        kind = "initial" if interaction is None else interaction["kind"]
        draft, unresolved = None, []
        if kind == "clarification_followup":
            draft, unresolved = (
                interaction["projection"]["draft_values"],
                interaction["projection"]["unresolved"],
            )
        projection = {
            "schema": AI_INPUT_SCHEMA,
            "kind": "initial" if interaction is None else "edit",
            "interaction_kind": kind,
            "interaction": {
                "subject_id": source.intake_public_id if interaction is None else interaction["id"],
                "subject_hash": source.evidence_digest
                if interaction is None
                else interaction["hash"],
                "predecessor_attempt_id": None
                if interaction is None
                else interaction["predecessor"],
            },
            "source_text": "" if kind == "card_edit" else original["source_text"],
            "reply_text": "" if proof is None else proof.text,
            "selection": original["selection"],
            "image_to_model": 0,
            "current_values": {k: payload.get(k) for k in FIELDS},
            "draft_values": draft,
            "unresolved": unresolved,
            "interpretation_context": context,
        }

        def private(value):
            if isinstance(value, str):
                check_private_text(value)
            elif isinstance(value, dict):
                for k, v in value.items():
                    private(k)
                    private(v)
            elif isinstance(value, list):
                for v in value:
                    private(v)

        private(projection)
        return projection

    def _prepare_v2(self, intake, operation, interaction=None, reply_id=None):
        if not _reference(intake) or not _reference(operation):
            raise AiInvocationError("AI reference invalid")
        self._begin()
        try:
            self._v2_schema()
            existing = self._conn.execute(
                "SELECT * FROM independent_ai_attempts WHERE operation_id=?", (operation,)
            ).fetchone()
            if existing is not None:
                old = _seal(existing)
                if (
                    old["schema"] != AI_INPUT_SCHEMA
                    or old["intake_public_id"] != intake
                    or old["input_identity"] != (reply_id or "initial")
                    or old["review_id"] != (None if interaction is None else interaction["id"])
                ):
                    raise AiInvocationError("AI operation collision")
                status = self.get_status(existing["attempt_id"])
                self._conn.commit()
                return status
            now = self._now()
            source = self._source(intake, now)
            proposal = self._target(intake)
            payload, _, version = resolve_effective_payload(self._conn, proposal)
            digest = compute_effective_proposal_content_hash(self._conn, proposal)
            proof = None
            if interaction is not None:
                # Refresh the actual row inside this writer snapshot.
                interaction = self._interaction(interaction["id"])
                if (
                    interaction["parent_id"] != proposal["id"]
                    or interaction["version"] != version
                    or interaction["content_hash"] != digest
                    or not interaction["created_at"] <= now < interaction["expires_at"]
                ):
                    raise AiInvocationError("Interpretation display review is stale")
                proof = self._proof(interaction, source, proposal, version, digest, reply_id, now)
            predecessor = None if interaction is None else interaction["predecessor"]
            pending = self._conn.execute(
                "SELECT a.attempt_id FROM independent_ai_attempts a LEFT JOIN "
                "independent_ai_publications p ON p.attempt_id=a.attempt_id WHERE "
                "a.parent_parser_output_id=? AND p.attempt_id IS NULL",
                (proposal["id"],),
            ).fetchall()
            permitted = set()
            cursor = predecessor
            while cursor is not None:
                if cursor in permitted or len(permitted) >= 128:
                    raise AiInvocationError("Clarification predecessor ancestry cycles")
                permitted.add(cursor)
                ancestor = self._conn.execute(
                    "SELECT * FROM independent_ai_followup_links WHERE successor_attempt_id=?",
                    (cursor,),
                ).fetchone()
                cursor = (
                    self._verify_followup_link(ancestor)["predecessor_attempt_id"]
                    if ancestor is not None
                    else None
                )
            if any(row[0] not in permitted for row in pending):
                raise AiInvocationError("Existing invocation must be reconciled; no retry")
            event_key = sha([source.namespace, source.instance_id, source.source_event_id])
            kind = "initial" if interaction is None else "edit"
            if self._conn.execute(
                "SELECT 1 FROM independent_ai_attempts WHERE source_event_key=? AND kind=? "
                "AND input_identity=?",
                (event_key, kind, reply_id or "initial"),
            ).fetchone():
                raise AiInvocationError("Existing source operation cannot be dispatched again")
            projection = self._projection_v2(proposal, source, payload, interaction, proof)
            registry = assets("v2")
            request_json = canonical_json_text(
                {
                    "input": projection,
                    "assets": registry,
                    "retry": 0,
                    "tools": [],
                    "image_to_model": 0,
                }
            )
            material = {
                "schema": AI_INPUT_SCHEMA,
                "binding": vars(self._binding),
                "provider_binding": vars(self._provider_binding),
                "interpretation_policy": vars(self._policy),
                "assets": registry,
                "operation_id": operation,
                "kind": kind,
                "interaction_kind": projection["interaction_kind"],
                "interaction": projection["interaction"],
                "interaction_contract": interaction,
                "intake_public_id": intake,
                "source": dataclasses.asdict(source),
                "parent_public_id": proposal["public_id"],
                "parent_id": proposal["id"],
                "parent_version": version,
                "parent_content_hash": digest,
                "base_payload": payload,
                "review_id": None if interaction is None else interaction["id"],
                "review_hash": None if interaction is None else interaction["hash"],
                "input_identity": reply_id or "initial",
                "input_proof": None if proof is None else dataclasses.asdict(proof),
                "projection": projection,
                "request_json": request_json,
                "request_hash": hashlib.sha256(request_json.encode()).hexdigest(),
                "created_at": now,
                "deadline_at": min(
                    now + self._provider_binding.deadline_seconds,
                    proof.expires_at if proof else now + self._provider_binding.deadline_seconds,
                ),
            }
            identity = "aia_" + sha([self._binding.instance_id, operation])[:32]
            self._conn.execute(
                "INSERT INTO independent_ai_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    operation,
                    intake,
                    proposal["id"],
                    version,
                    digest,
                    event_key,
                    kind,
                    reply_id or "initial",
                    canonical_json_text(material),
                    sha(material),
                    now,
                    material["deadline_at"],
                ),
            )
            if predecessor is not None:
                link = {
                    "schema": "finance-independent-ai-followup-link-v2",
                    "request_id": interaction["id"],
                    "predecessor_attempt_id": predecessor,
                    "successor_attempt_id": identity,
                    "source_event_key": event_key,
                    "reply_evidence_id": reply_id,
                    "reply_evidence_digest": proof.evidence_digest,
                    "accepted_at": now,
                }
                self._conn.execute(
                    "INSERT INTO independent_ai_followup_links VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        interaction["id"],
                        predecessor,
                        identity,
                        event_key,
                        reply_id,
                        proof.evidence_digest,
                        canonical_json_text(link),
                        sha(link),
                        now,
                    ),
                )
            self._conn.commit()
            return AiInvocationStatus(identity, "PREPARED")
        except BaseException:
            self._conn.rollback()
            raise

    def _input(self, material, *, historical):
        if material["schema"] == AI_INPUT_SCHEMA_V1:
            return super()._input(material, historical=historical)
        if material["interaction_kind"] == "initial":
            return
        interaction = self._interaction(material["review_id"], historical=True)
        if interaction != material["interaction_contract"]:
            raise AiInvocationError("Original interaction changed")
        parent = ParserProposalRepository(self._conn).get_lineage_row_by_id(material["parent_id"])
        proof = self._proof(
            interaction,
            VerifiedSource(**material["source"]),
            parent,
            material["parent_version"],
            material["parent_content_hash"],
            material["input_identity"],
            material["created_at"],
            historical=historical,
        )
        if dataclasses.asdict(proof) != material["input_proof"]:
            raise AiInvocationError("Interpretation reply evidence changed")
        if interaction["kind"] == "clarification_followup":
            row = self._conn.execute(
                "SELECT * FROM independent_ai_followup_links WHERE successor_attempt_id=?",
                (
                    material["attempt_id"]
                    if "attempt_id" in material
                    else "aia_" + sha([self._binding.instance_id, material["operation_id"]])[:32],
                ),
            ).fetchone()
            if row is None:
                raise AiInvocationError("Clarification consumption absent")
            self._verify_followup_link(row, material=material, proof=proof)

    def _verify_followup_link(self, row, *, material=None, proof=None):
        """One edge proof for consumption, traversal and financial ancestry.

        No physical mirror supplies its own expected value. Request/successor
        seals, fixed source custody and actual reply authority reconstruct it.
        Keep this local proof independent of recursive current-frontier checks.
        """
        link = _seal(row)
        if any(
            row[key] != link.get(key)
            for key in (
                "request_id",
                "predecessor_attempt_id",
                "successor_attempt_id",
                "source_event_key",
                "reply_evidence_id",
                "reply_evidence_digest",
                "accepted_at",
            )
        ):
            raise AiInvocationError("Clarification consumption physical mirrors changed")
        successor = self._conn.execute(
            "SELECT * FROM independent_ai_attempts WHERE attempt_id=?",
            (link["successor_attempt_id"],),
        ).fetchone()
        request = self._conn.execute(
            "SELECT * FROM independent_ai_clarification_requests WHERE request_id=?",
            (link["request_id"],),
        ).fetchone()
        if successor is None or request is None:
            raise AiInvocationError("Clarification consumption identities absent")
        sealed_successor, question = _seal(successor), _seal(request)
        if material is None:
            material = sealed_successor
        if material != sealed_successor or material.get("schema") != AI_INPUT_SCHEMA:
            raise AiInvocationError("Clarification successor seal changed")
        expected_interaction = {
            "kind": "clarification_followup",
            "id": request["request_id"],
            "hash": request["material_hash"],
            "projection": question,
            "intake": request["intake_public_id"],
            "parent_id": request["parent_parser_output_id"],
            "version": request["parent_version"],
            "content_hash": request["parent_content_hash"],
            "created_at": request["created_at"],
            "expires_at": request["expires_at"],
            "predecessor": request["attempt_id"],
        }
        source = VerifiedSource(**material["source"])
        event_key = sha([source.namespace, source.instance_id, source.source_event_id])
        identity = "aia_" + sha([self._binding.instance_id, material["operation_id"]])[:32]
        if (
            material["binding"] != vars(self._binding)
            or material["interaction_contract"] != expected_interaction
            or material["interaction_kind"] != "clarification_followup"
            or material["source"] != question["source"]
            or question["attempt_id"] != request["attempt_id"]
            or request["request_id"] != "aiclar_" + sha(question)[:32]
            or material["intake_public_id"] != request["intake_public_id"]
            or material["parent_id"] != request["parent_parser_output_id"]
            or material["parent_version"] != request["parent_version"]
            or material["parent_content_hash"] != request["parent_content_hash"]
            or successor["attempt_id"] != identity
            or successor["source_event_key"] != event_key
            or successor["input_identity"] != material["input_identity"]
            or successor["created_at"] != material["created_at"]
            or self._source(material["intake_public_id"], self._now()) != source
        ):
            raise AiInvocationError("Clarification consumption independent binding changed")
        if proof is None:
            parent = ParserProposalRepository(self._conn).get_lineage_row_by_id(
                material["parent_id"]
            )
            proof = self._proof(
                expected_interaction,
                source,
                parent,
                material["parent_version"],
                material["parent_content_hash"],
                material["input_identity"],
                material["created_at"],
                historical=True,
            )
        if dataclasses.asdict(proof) != material["input_proof"] or link != {
            "schema": "finance-independent-ai-followup-link-v2",
            "request_id": expected_interaction["id"],
            "predecessor_attempt_id": expected_interaction["predecessor"],
            "successor_attempt_id": identity,
            "source_event_key": event_key,
            "reply_evidence_id": proof.evidence_id,
            "reply_evidence_digest": proof.evidence_digest,
            "accepted_at": material["created_at"],
        }:
            raise AiInvocationError("Clarification consumption changed")
        return link

    def _attempt(self, identity, *, historical=False):
        stack = getattr(self, "_verification_stack", [])
        if identity in stack or len(stack) >= 128:
            raise AiInvocationError("Interaction ancestry cycles or exceeds bound")
        self._verification_stack = [*stack, identity]
        try:
            return self._attempt_v2(identity, historical=historical)
        finally:
            self._verification_stack = stack

    def _attempt_v2(self, identity, *, historical=False):
        row = self._conn.execute(
            "SELECT * FROM independent_ai_attempts WHERE attempt_id=?", (identity,)
        ).fetchone()
        if row is None:
            raise AiInvocationError("AI invocation absent")
        material = _seal(row)
        if material["schema"] == AI_INPUT_SCHEMA_V1:
            if not historical:
                raise AiInvocationError("Independent AI v1 is historical-read-only")
            return super()._attempt(identity, historical=True)
        self._v2_schema()
        if (
            material["schema"] != AI_INPUT_SCHEMA
            or any(
                material[k] != row[col]
                for k, col in (
                    ("operation_id", "operation_id"),
                    ("kind", "kind"),
                    ("input_identity", "input_identity"),
                    ("intake_public_id", "intake_public_id"),
                    ("parent_id", "parent_parser_output_id"),
                    ("parent_version", "parent_version"),
                    ("parent_content_hash", "parent_content_hash"),
                    ("created_at", "created_at"),
                    ("deadline_at", "deadline_at"),
                )
            )
            or material["binding"] != vars(self._binding)
            or material["provider_binding"] != vars(self._provider_binding)
            or self._provider.binding != self._provider_binding
            or material["interpretation_policy"] != vars(self._policy)
            or material["assets"] != assets("v2")
            or hashlib.sha256(material["request_json"].encode()).hexdigest()
            != material["request_hash"]
            or sha(
                [
                    material["source"]["namespace"],
                    material["source"]["instance_id"],
                    material["source"]["source_event_id"],
                ]
            )
            != row["source_event_key"]
        ):
            raise AiInvocationError("AI fixed composition or resources changed")
        source = VerifiedSource(**material["source"])
        if self._source(row["intake_public_id"], self._now()) != source:
            raise AiInvocationError("AI source custody changed")
        self._input(material, historical=True)
        from finance_core.parser_proposals.independent_ai_source_money import require_source_base

        payload = require_source_base(self._conn, material, historical=historical)
        parent = ParserProposalRepository(self._conn).get_lineage_row_by_id(material["parent_id"])
        interaction = material["interaction_contract"]
        proof = (
            None
            if material["input_proof"] is None
            else VerifiedInterpretationInputV2(**material["input_proof"])
        )
        projection = self._projection_v2(parent, source, payload, interaction, proof)
        if (
            projection != material["projection"]
            or material["interaction"] != projection["interaction"]
            or material["interaction_kind"] != projection["interaction_kind"]
            or material["request_json"]
            != canonical_json_text(
                {
                    "input": projection,
                    "assets": material["assets"],
                    "retry": 0,
                    "tools": [],
                    "image_to_model": 0,
                }
            )
        ):
            raise AiInvocationError("Original bounded input changed")
        if not historical and not self._current_target(row):
            raise AiInvocationError("AI current operation target changed")
        return row, material

    def _verify_clarification(self, row):
        self._v2_schema()
        attempt, input_material = self._attempt(row["attempt_id"], historical=True)
        if input_material["schema"] != AI_INPUT_SCHEMA:
            raise AiInvocationError("Old independent invocation cannot create v2 questions")
        result = self._conn.execute(
            "SELECT * FROM independent_ai_results WHERE result_id=?", (row["result_id"],)
        ).fetchone()
        if result is None or result["attempt_id"] != row["attempt_id"]:
            raise AiInvocationError("Clarification result absent")
        validation = self._result(attempt, input_material, result, historical=True)
        if validation["state"] != "NEEDS_CLARIFICATION" or not validation.get("unresolved"):
            raise AiInvocationError("Clarification requires specific authenticated questions")
        generations = self._conn.execute(
            "SELECT * FROM independent_ai_clarification_requests WHERE attempt_id=? ORDER BY "
            "generation",
            (row["attempt_id"],),
        ).fetchall()
        if not generations or len(generations) > 128:
            raise AiInvocationError("Clarification generation ancestry absent or exceeds bound")
        selected = None
        previous = None
        for index, generation in enumerate(generations):
            material = _seal(generation)
            expected_previous = None if previous is None else previous["request_id"]
            if (
                material.get("schema") != CLARIFICATION_SCHEMA
                or material.get("confirm_available") is not False
                or material["binding"] != input_material["binding"]
                or material["draft_values"] != validation.get("draft_values", {})
                or material["unresolved"] != validation["unresolved"]
                or material["hard_blockers"] != validation.get("hard_blockers", [])
                or material["attempt_id"] != attempt["attempt_id"]
                or material["result_id"] != result["result_id"]
                or generation["result_id"] != result["result_id"]
                or type(material["generation"]) is not int
                or material["generation"] != index
                or generation["generation"] != index
                or material["previous_request_id"] != expected_previous
                or generation["previous_request_id"] != expected_previous
                or material["proposal_public_id"] != input_material["parent_public_id"]
                or generation["parent_parser_output_id"] != input_material["parent_id"]
                or material["proposal_version"] != input_material["parent_version"]
                or generation["parent_version"] != input_material["parent_version"]
                or material["effective_content_hash"] != input_material["parent_content_hash"]
                or generation["parent_content_hash"] != input_material["parent_content_hash"]
                or generation["intake_public_id"] != input_material["intake_public_id"]
                or material["source"] != input_material["source"]
                or material["created_at"] != generation["created_at"]
                or material["expires_at"] != generation["expires_at"]
                or generation["request_id"] != "aiclar_" + sha(material)[:32]
                or type(material["created_at"]) is not int
                or type(material["expires_at"]) is not int
                or material["expires_at"] != material["created_at"] + 900
            ):
                raise AiInvocationError("Clarification generation binding changed")
            if previous is not None:
                if (
                    previous["expires_at"] > generation["created_at"]
                    or self._clarification_state(previous) == "CONSUMED"
                ):
                    raise AiInvocationError(
                        "Clarification refresh did not follow expired unconsumed generation"
                    )
            if generation["request_id"] == row["request_id"]:
                selected = material
            previous = generation
        if selected is None:
            raise AiInvocationError("Clarification selected generation is outside ancestry")
        return selected

    def _clarification_state(self, row):
        # Search every independent identity, so corrupt request mirrors cannot
        # hide an authentic consumption or invent a new open generation.
        links = self._conn.execute(
            "SELECT * FROM independent_ai_followup_links WHERE request_id=? "
            "OR predecessor_attempt_id=? OR json_extract(material_json,'$.request_id')=? "
            "OR json_extract(material_json,'$.predecessor_attempt_id')=?",
            (row["request_id"], row["attempt_id"], row["request_id"], row["attempt_id"]),
        ).fetchall()
        for link in links:
            self._verify_followup_link(link)
        if any(link["request_id"] == row["request_id"] for link in links):
            return "CONSUMED"
        if self._conn.execute(
            "SELECT 1 FROM independent_ai_clarification_requests WHERE previous_request_id=?",
            (row["request_id"],),
        ).fetchone():
            return "STALE"
        if self._conn.execute(
            "SELECT 1 FROM independent_ai_events WHERE attempt_id=?", (row["attempt_id"],)
        ).fetchone():
            return "CANCELLED"
        attempt = self._conn.execute(
            "SELECT * FROM independent_ai_attempts WHERE attempt_id=?", (row["attempt_id"],)
        ).fetchone()
        if not self._current_target(attempt):
            return "STALE"
        return "EXPIRED" if self._now() >= row["expires_at"] else "OPEN"

    def prepare_clarification(self, attempt_id):
        self._begin()
        try:
            self._v2_schema()
            attempt, material = self._attempt(attempt_id)
            if self.get_status(attempt_id).state != "NEEDS_CLARIFICATION":
                raise AiInvocationError("Only sealed NEEDS_CLARIFICATION may ask a followup")
            rows = self._conn.execute(
                "SELECT * FROM independent_ai_results WHERE attempt_id=?", (attempt_id,)
            ).fetchall()
            if len(rows) != 1:
                raise AiInvocationError("Clarification requires one authenticated result")
            validation = self._result(attempt, material, rows[0])
            previous = self._conn.execute(
                "SELECT * FROM independent_ai_clarification_requests WHERE attempt_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            now = self._now()
            if previous is not None:
                old = self._verify_clarification(previous)
                state = self._clarification_state(previous)
                if state == "OPEN":
                    self._conn.commit()
                    return ClarificationReview(
                        previous["request_id"],
                        previous["material_hash"],
                        old,
                        previous["expires_at"],
                    )
                if state != "EXPIRED":
                    raise AiInvocationError("Changed or consumed clarification cannot refresh")
            generation = 0 if previous is None else previous["generation"] + 1
            projection = {
                "schema": CLARIFICATION_SCHEMA,
                "binding": material["binding"],
                "source": material["source"],
                "attempt_id": attempt_id,
                "result_id": rows[0]["result_id"],
                "generation": generation,
                "previous_request_id": None if previous is None else previous["request_id"],
                "proposal_public_id": material["parent_public_id"],
                "proposal_version": material["parent_version"],
                "effective_content_hash": material["parent_content_hash"],
                "draft_values": validation.get("draft_values", {}),
                "unresolved": validation.get("unresolved", []),
                "hard_blockers": validation.get("hard_blockers", []),
                "confirm_available": False,
                "created_at": now,
                "expires_at": now + 900,
            }
            identity = "aiclar_" + sha(projection)[:32]
            self._conn.execute(
                "INSERT INTO independent_ai_clarification_requests VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    attempt_id,
                    rows[0]["result_id"],
                    generation,
                    projection["previous_request_id"],
                    material["intake_public_id"],
                    material["parent_id"],
                    material["parent_version"],
                    material["parent_content_hash"],
                    canonical_json_text(projection),
                    sha(projection),
                    now,
                    now + 900,
                ),
            )
            self._conn.commit()
            return ClarificationReview(identity, sha(projection), projection, now + 900)
        except BaseException:
            self._conn.rollback()
            raise

    def get_clarification(self, request_id):
        from finance_core.application.review import review_snapshot

        with review_snapshot(self._conn):
            row = self._conn.execute(
                "SELECT * FROM independent_ai_clarification_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise AiInvocationError("Clarification absent")
            projection = self._verify_clarification(row)
            return ClarificationStatus(
                request_id, self._clarification_state(row), projection, row["expires_at"]
            )
