"""Durable independent AI invocation and authentic unconfirmed publication.

Operations supply references only. Fixed constructor ports own source, reply
and actual provider evidence. No network/credential implementation lives here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from finance_core.application.admission import (
    SourceVerifier,
    TrustedBinding,
    VerifiedSource,
    _digest,
    _reference,
    validate_verified_source,
)
from finance_core.application.ai_invocation_contract import (
    AI_INPUT_SCHEMA_V1 as AI_INPUT_SCHEMA,
)
from finance_core.application.ai_invocation_contract import (
    AI_INTERPRETATION_SCHEMA_V1 as AI_INTERPRETATION_SCHEMA,
)
from finance_core.application.ai_invocation_contract import (
    AiDispatchRequest,
    AiInvocationError,
    AiInvocationStatus,
    AiProviderObservation,
    ExpectedAiLineage,
    ExpectedInterpretationInput,
    InterpretationInputVerifier,
    InterpretationPolicy,
    ProviderBinding,
    TrustedAiProvider,
    VerifiedInterpretationInput,
)
from finance_core.calculation.authoritative_snapshot import canonical_json_value
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.independent_ai_publication import publish_in_transaction
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
from finance_core.sqlite_connection import ForeignKeysDisabledError, require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database


def _sealed(row: sqlite3.Row) -> dict[str, Any]:
    material = json.loads(row["material_json"])
    if (
        not isinstance(material, dict)
        or canonical_json_text(material) != row["material_json"]
        or sha(material) != row["material_hash"]
    ):
        raise AiInvocationError("Independent AI evidence seal changed")
    return material


class _AiInvocationServiceV1:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        binding: TrustedBinding,
        source_verifier: SourceVerifier,
        interpretation_input_verifier: InterpretationInputVerifier,
        provider: TrustedAiProvider,
        clock: Callable[[], int],
        interpretation_policy: InterpretationPolicy,
    ) -> None:
        if type(binding) is not TrustedBinding or not all(
            _reference(v) for v in vars(binding).values()
        ):
            raise AiInvocationError("AI source binding incomplete")
        p = provider.binding
        if (
            type(p) is not ProviderBinding
            or not all(_reference(v) for k, v in vars(p).items() if k != "deadline_seconds")
            or type(p.deadline_seconds) is not int
            or not 1 <= p.deadline_seconds <= 300
        ):
            raise AiInvocationError("AI provider binding incomplete")
        self._conn, self._binding, self._source_port = connection, binding, source_verifier
        self._input_port, self._provider, self._clock = (
            interpretation_input_verifier,
            provider,
            clock,
        )
        from finance_core.money import normalize_currency

        if (
            type(interpretation_policy) is not InterpretationPolicy
            or normalize_currency(interpretation_policy.default_currency)
            != interpretation_policy.default_currency
        ):
            raise AiInvocationError("Fixed interpretation policy unavailable")
        ZoneInfo(interpretation_policy.timezone)
        self._policy = interpretation_policy
        self._provider_binding = p

    def _now(self) -> int:
        now = self._clock()
        if type(now) is not int or now <= 0:
            raise AiInvocationError("AI clock absent")
        return now

    def _source(self, intake: str, now: int) -> VerifiedSource:
        return validate_verified_source(
            self._source_port.verify_persisted(self._conn, intake),
            binding=self._binding,
            intake_public_id=intake,
            now=now,
        )

    def _begin(self) -> None:
        require_staging_database(self._conn)
        try:
            require_foreign_keys_enabled(self._conn)
        except ForeignKeysDisabledError as exc:
            raise AiInvocationError(str(exc)) from exc
        if self._conn.in_transaction:
            raise AiInvocationError("AI owner requires clean connection transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            from finance_core.parser_proposals.independent_ai_lineage import require_ai_schema

            require_ai_schema(self._conn)
        except BaseException:
            self._conn.rollback()
            raise

    def _target(self, intake: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?", (intake,)
        ).fetchone()
        proposal = (
            ParserProposalRepository(self._conn).get_lineage_row_by_id(row[0]) if row else None
        )
        if (
            proposal is None
            or proposal["source_public_id"] != intake
            or proposal["parse_status"]
            not in ("parsed_pending_confirmation", "edited_pending_confirmation")
        ):
            raise AiInvocationError("AI target is not a current pending source")
        self._no_decision(intake)
        from finance_core.parser_proposals.amendment_lineage import (
            require_independent_source_edit_history,
        )

        require_independent_source_edit_history(self._conn, proposal)
        self.verify_lineage(proposal["public_id"])
        return proposal

    def _no_decision(self, intake: str) -> None:
        for table, column in (("application_posting_attempts", "intake_public_id"),):
            if self._conn.execute(
                f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (intake,)
            ).fetchone():
                raise AiInvocationError("AI source already has posting authority")
        if self._conn.execute(
            (
                "SELECT 1 FROM parser_proposal_authorizations a JOIN parser_output"
                "s p ON p.id=a.parser_output_id WHERE p.source_public_id=? LIMIT 1"
            ),
            (intake,),
        ).fetchone():
            raise AiInvocationError("AI source already has a decision")
        if self._conn.execute(
            (
                "SELECT 1 FROM parser_human_drafts d JOIN parser_outputs p ON p.id"
                "=d.decision_target_parser_output_id WHERE p.source_public_id=? LI"
                "MIT 1"
            ),
            (intake,),
        ).fetchone():
            raise AiInvocationError("AI source contains foreign draft history")

    def _projection(
        self,
        proposal: dict[str, Any],
        kind: str,
        reply: str | None,
        source: VerifiedSource,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        link = self._conn.execute(
            (
                "SELECT e.* FROM receipt_ocr_proposal_links l JOIN receipt_ocr_ext"
                "ractions e ON e.id=l.extraction_id WHERE l.parser_output_id=?"
            ),
            (proposal["id"],),
        ).fetchone()
        if link is not None:
            if link["extraction_status"] != "succeeded":
                raise AiInvocationError("verified_local_ocr_unavailable")
            text = "\n".join(
                row[0]
                for row in self._conn.execute(
                    (
                        "SELECT normalized_text FROM receipt_ocr_blocks WHERE extraction_i"
                        "d=? ORDER BY sequence_index"
                    ),
                    (link["id"],),
                )
            )
            selection = {
                "kind": "local_ocr",
                "extraction_id": link["public_id"],
                "normalized_result_hash": link["normalized_result_hash"],
                "source_hash": link["source_attachment_hash"],
            }
        else:
            if proposal["attachment_id"] is not None:
                raise AiInvocationError("media_requires_verified_local_ocr")
            retained = self._conn.execute(
                "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
                (proposal["source_public_id"],),
            ).fetchone()
            if retained is None or not isinstance(retained["raw_input"], str):
                raise AiInvocationError("Authenticated text source unavailable")
            text = retained["raw_input"]
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if (
                digest != source.source_content_hash
                or retained["source_content_hash"] != "sha256:" + digest
                or proposal["raw_text"] != text
                or source.intake_public_id != proposal["source_public_id"]
            ):
                raise AiInvocationError("Selected text differs from authenticated raw source")
            selection = {"kind": "text", "source_public_id": proposal["source_public_id"]}
        if kind == "initial" and (
            not isinstance(text, str) or not text or len(text.encode()) > 8192
        ):
            raise AiInvocationError("source_text_limit")
        if kind == "edit":
            text = ""
        if payload is None:
            payload, _, _ = resolve_effective_payload(self._conn, proposal)
        projection = {
            "schema": AI_INPUT_SCHEMA,
            "kind": kind,
            "source_text": text,
            "selection": selection,
            "image_to_model": 0,
            "source_context": {
                "transaction_date": datetime.fromtimestamp(
                    source.source_occurred_at, ZoneInfo(self._policy.timezone)
                )
                .date()
                .isoformat(),
                "currency": self._policy.default_currency,
            },
            "interpretation_policy": vars(self._policy),
        }
        if kind == "edit":
            projection.pop("source_context")
            projection.pop("interpretation_policy")
            projection["reply_text"] = reply
            projection["current_values"] = {k: payload.get(k) for k in FIELDS}

        # The adapter receives decoded strings: JSON escapes must not conceal
        # credential separators from the fixed pre-dispatch privacy policy.
        def check_strings(value: Any) -> None:
            if isinstance(value, str):
                check_private_text(value)
            elif isinstance(value, dict):
                for key, member in value.items():
                    check_strings(key)
                    check_strings(member)
            elif isinstance(value, (tuple, list)):
                for member in value:
                    check_strings(member)

        if kind == "initial" and selection["kind"] == "local_ocr":
            from finance_core.parser_proposals.independent_ai_source_money import (
                qualify_receipt_money,
            )

            qualify_receipt_money(self._conn, proposal["id"], projection)
        check_strings(projection)
        check_private_text(canonical_json_text(projection))
        return projection

    def prepare_initial(self, intake_id: str, operation_id: str) -> AiInvocationStatus:
        return self._prepare(intake_id, operation_id, None, None)

    def prepare_edit(
        self, review_id: str, reply_evidence_id: str, operation_id: str
    ) -> AiInvocationStatus:
        review = self._conn.execute(
            "SELECT * FROM application_amendment_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if review is None:
            raise AiInvocationError("Interpretation requires retained full review")
        return self._prepare(review["intake_public_id"], operation_id, review_id, reply_evidence_id)

    def _input(self, material: dict[str, Any], *, historical: bool) -> None:
        if material["kind"] != "edit":
            return
        from finance_core.application.amendment_contract import amendment_review_sha256

        review = self._conn.execute(
            "SELECT * FROM application_amendment_reviews WHERE review_id=?",
            (material["review_id"],),
        ).fetchone()
        if review is None:
            raise AiInvocationError("Interpretation original review absent")
        old = canonical_json_value(review["material_json"])
        if (
            not isinstance(old, dict)
            or amendment_review_sha256(old) != material["review_hash"]
            or review["review_hash"] != material["review_hash"]
            or review["parser_output_id"] != material["parent_id"]
            or review["base_version"] != material["parent_version"]
            or review["base_content_hash"] != material["parent_content_hash"]
            or canonical_json_text(json.loads(old["effective_payload_json"]))
            != canonical_json_text(material["base_payload"])
        ):
            raise AiInvocationError("Interpretation original review changed")
        expected = ExpectedInterpretationInput(
            self._binding,
            VerifiedSource(**material["source"]),
            material["review_id"],
            material["review_hash"],
            material["parent_public_id"],
            material["parent_version"],
            material["parent_content_hash"],
            material["created_at"],
            historical,
        )
        proof = self._input_port.verify_persisted(self._conn, material["input_identity"], expected)
        if dataclasses.asdict(proof) != material["input_proof"]:
            raise AiInvocationError("Interpretation reply evidence changed")

    def _prepare(
        self, intake: str, operation: str, review_id: str | None, reply_id: str | None
    ) -> AiInvocationStatus:
        if not _reference(operation) or not _reference(intake):
            raise AiInvocationError("AI reference invalid")
        self._begin()
        try:
            existing = self._conn.execute(
                "SELECT * FROM independent_ai_attempts WHERE operation_id=?", (operation,)
            ).fetchone()
            if existing is not None:
                old = _sealed(existing)
                if (
                    old["intake_public_id"] != intake
                    or old["review_id"] != review_id
                    or old["input_identity"] != (reply_id or "initial")
                ):
                    raise AiInvocationError("AI operation collision")
                result = self.get_status(existing["attempt_id"])
                self._conn.commit()
                return result
            now = self._now()
            source = self._source(intake, now)
            proposal = self._target(intake)
            payload, _, version = resolve_effective_payload(self._conn, proposal)
            current_hash = compute_effective_proposal_content_hash(self._conn, proposal)
            proof = None
            review_hash = None
            kind = "edit" if review_id else "initial"
            if review_id:
                review = self._conn.execute(
                    "SELECT * FROM application_amendment_reviews WHERE review_id=?", (review_id,)
                ).fetchone()
                if (
                    review is None
                    or review["parser_output_id"] != proposal["id"]
                    or review["base_version"] != version
                    or review["base_content_hash"] != current_hash
                    or not review["created_at"] <= now < review["expires_at"]
                ):
                    raise AiInvocationError("Interpretation display review is stale")
                from finance_core.application.amendment_contract import amendment_review_sha256

                old_value = canonical_json_value(review["material_json"])
                if not isinstance(old_value, dict):
                    raise AiInvocationError("Interpretation review is malformed")
                old = old_value
                if amendment_review_sha256(old) != review["review_hash"]:
                    raise AiInvocationError("Interpretation review seal changed")
                review_hash = review["review_hash"]
                expected = ExpectedInterpretationInput(
                    self._binding,
                    source,
                    review_id,
                    review_hash,
                    proposal["public_id"],
                    version,
                    current_hash,
                    now,
                )
                proof = self._input_port.verify_persisted(self._conn, str(reply_id), expected)
                if (
                    type(proof) is not VerifiedInterpretationInput
                    or type(proof.proposal_version) is not int
                    or type(proof.observed_at) is not int
                    or type(proof.expires_at) is not int
                    or proof.revoked is not False
                    or proof.schema != AI_INTERPRETATION_SCHEMA
                    or proof.instance_id != self._binding.instance_id
                    or proof.human_principal_id != self._binding.human_principal_id
                    or proof.evidence_id != reply_id
                    or not _digest(proof.evidence_digest)
                    or proof.source_evidence_id != source.evidence_id
                    or proof.source_evidence_digest != source.evidence_digest
                    or proof.review_id != review_id
                    or proof.review_hash != review_hash
                    or proof.proposal_public_id != proposal["public_id"]
                    or proof.proposal_version != version
                    or proof.proposal_content_hash != current_hash
                    or not _reference(proof.display_id)
                    or not _digest(proof.display_evidence_digest)
                    or proof.reply_to_display_id != proof.display_id
                    or proof.revoked
                    or not review["created_at"] <= proof.observed_at <= now < proof.expires_at
                    or not isinstance(proof.text, str)
                    or not 0 < len(proof.text.encode()) <= 2048
                ):
                    raise AiInvocationError("Interpretation reply binding unavailable")
            if self._conn.execute(
                (
                    "SELECT 1 FROM independent_ai_attempts WHERE source_event_key=? AN"
                    "D kind=? AND input_identity=?"
                ),
                (
                    sha([source.namespace, source.instance_id, source.source_event_id]),
                    kind,
                    reply_id or "initial",
                ),
            ).fetchone():
                raise AiInvocationError("Existing source operation cannot be dispatched again")
            if self._conn.execute(
                (
                    "SELECT 1 FROM independent_ai_attempts a LEFT JOIN independent_ai_"
                    "publications p ON p.attempt_id=a.attempt_id WHERE a.parent_parser"
                    "_output_id=? AND p.attempt_id IS NULL LIMIT 1"
                ),
                (proposal["id"],),
            ).fetchone():
                raise AiInvocationError("Existing invocation must be reconciled; no retry")
            projection = self._projection(
                proposal, kind, None if proof is None else proof.text, source
            )
            registry = assets()
            request = {
                "input": projection,
                "assets": registry,
                "retry": 0,
                "tools": [],
                "image_to_model": 0,
            }
            request_json = canonical_json_text(request)
            material = {
                "schema": AI_INPUT_SCHEMA,
                "binding": vars(self._binding),
                "provider_binding": vars(self._provider_binding),
                "interpretation_policy": vars(self._policy),
                "assets": registry,
                "operation_id": operation,
                "kind": kind,
                "intake_public_id": intake,
                "source": dataclasses.asdict(source),
                "parent_public_id": proposal["public_id"],
                "parent_id": proposal["id"],
                "parent_version": version,
                "parent_content_hash": current_hash,
                "base_payload": payload,
                "review_id": review_id,
                "review_hash": review_hash,
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
            event_key = sha([source.namespace, source.instance_id, source.source_event_id])
            self._conn.execute(
                "INSERT INTO independent_ai_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    operation,
                    intake,
                    proposal["id"],
                    version,
                    current_hash,
                    event_key,
                    kind,
                    reply_id or "initial",
                    canonical_json_text(material),
                    sha(material),
                    now,
                    material["deadline_at"],
                ),
            )
            self._conn.commit()
            return AiInvocationStatus(identity, "PREPARED")
        except BaseException:
            self._conn.rollback()
            raise

    def _attempt(
        self, identity: str, *, historical: bool = False
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM independent_ai_attempts WHERE attempt_id=?", (identity,)
        ).fetchone()
        if row is None:
            raise AiInvocationError("AI invocation absent")
        material = _sealed(row)
        if (
            material["schema"] != AI_INPUT_SCHEMA
            or material["intake_public_id"] != row["intake_public_id"]
            or material["operation_id"] != row["operation_id"]
            or material["kind"] != row["kind"]
            or material["input_identity"] != row["input_identity"]
            or material["created_at"] != row["created_at"]
            or material["deadline_at"] != row["deadline_at"]
            or sha(
                [
                    material["source"]["namespace"],
                    material["source"]["instance_id"],
                    material["source"]["source_event_id"],
                ]
            )
            != row["source_event_key"]
            or type(material["parent_id"]) is not int
            or material["parent_id"] != row["parent_parser_output_id"]
            or type(material["parent_version"]) is not int
            or material["parent_version"] != row["parent_version"]
            or material["parent_content_hash"] != row["parent_content_hash"]
            or material["interpretation_policy"] != vars(self._policy)
            or material["binding"] != vars(self._binding)
            or material["provider_binding"] != vars(self._provider_binding)
            or self._provider.binding != self._provider_binding
            or material["assets"] != assets()
            or hashlib.sha256(material["request_json"].encode()).hexdigest()
            != material["request_hash"]
        ):
            raise AiInvocationError("AI fixed composition or resources changed")
        if material["request_json"] != canonical_json_text(
            {
                "input": material["projection"],
                "assets": material["assets"],
                "retry": 0,
                "tools": [],
                "image_to_model": 0,
            }
        ):
            raise AiInvocationError("Original bounded request changed")
        if self._source(row["intake_public_id"], self._now()) != VerifiedSource(
            **material["source"]
        ):
            raise AiInvocationError("AI source custody changed")
        self._input(material, historical=True)
        from finance_core.parser_proposals.independent_ai_source_money import require_source_base

        payload = require_source_base(self._conn, material, historical=historical)
        parent = ParserProposalRepository(self._conn).get_lineage_row_by_id(
            row["parent_parser_output_id"]
        )
        if (
            parent is None
            or _AiInvocationServiceV1._projection(
                self,
                parent,
                material["kind"],
                None if material["input_proof"] is None else material["input_proof"]["text"],
                VerifiedSource(**material["source"]),
                payload=payload,
            )
            != material["projection"]
        ):
            raise AiInvocationError("Original bounded input selection changed")
        if not historical and not self._current_target(row):
            raise AiInvocationError("AI current operation target changed")
        return row, material

    def claim(self, attempt_id: str) -> AiDispatchRequest | AiInvocationStatus:
        self._begin()
        try:
            if self._conn.execute(
                "SELECT 1 FROM independent_ai_publications WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                status = self.get_status(attempt_id)
                self._conn.commit()
                return status
            row, material = self._attempt(attempt_id)
            status = self.get_status(attempt_id)
            if status.state == "RESULT_STALE":
                raise AiInvocationError("AI target changed before claim")
            if status.state != "PREPARED":
                self._conn.commit()
                return status
            now = self._now()
            if now >= row["deadline_at"]:
                self._event(attempt_id, "UNKNOWN", now)
                self._conn.commit()
                return AiInvocationStatus(attempt_id, "UNKNOWN", "deadline")
            target = self._target(row["intake_public_id"])
            if (
                target["id"] != row["parent_parser_output_id"]
                or compute_effective_proposal_content_hash(self._conn, target)
                != row["parent_content_hash"]
                or resolve_effective_payload(self._conn, target)[2] != row["parent_version"]
            ):
                raise AiInvocationError("AI target changed before claim")
            self._input(material, historical=False)
            claim_id = "aic_" + sha([attempt_id, material["request_hash"]])[:32]
            claim_material = {
                "attempt_id": attempt_id,
                "claim_id": claim_id,
                "request_hash": material["request_hash"],
                "claimed_at": now,
                "deadline_at": row["deadline_at"],
            }
            self._conn.execute(
                "INSERT INTO independent_ai_claims VALUES (?,?,?,?,?,?)",
                (
                    attempt_id,
                    claim_id,
                    now,
                    material["request_hash"],
                    canonical_json_text(claim_material),
                    sha(claim_material),
                ),
            )
            request = self._request(row, material)
            self._conn.commit()
            return request
        except BaseException:
            self._conn.rollback()
            raise

    def _request(self, attempt: sqlite3.Row, material: dict[str, Any]) -> AiDispatchRequest:
        claim = self._conn.execute(
            "SELECT * FROM independent_ai_claims WHERE attempt_id=?", (attempt["attempt_id"],)
        ).fetchone()
        if claim is None:
            raise AiInvocationError("AI dispatch has no claim")
        sealed = _sealed(claim)
        expected = {
            "attempt_id": attempt["attempt_id"],
            "claim_id": claim["claim_id"],
            "request_hash": material["request_hash"],
            "claimed_at": claim["claimed_at"],
            "deadline_at": attempt["deadline_at"],
        }
        if (
            claim["claim_id"]
            != "aic_" + sha([attempt["attempt_id"], material["request_hash"]])[:32]
            or claim["request_hash"] != material["request_hash"]
            or canonical_json_text(sealed) != canonical_json_text(expected)
        ):
            raise AiInvocationError("AI claim correlation changed")
        return AiDispatchRequest(
            attempt["attempt_id"],
            claim["claim_id"],
            material["request_json"],
            material["request_hash"],
            self._provider_binding,
            claim["claimed_at"],
            attempt["deadline_at"],
        )

    def invoke(self, attempt_id: str) -> AiInvocationStatus:
        request = self.claim(attempt_id)
        if isinstance(request, AiInvocationStatus):
            return request
        if not request.claimed_at <= self._now() < min(request.claimed_at + 5, request.deadline_at):
            self._begin()
            try:
                self._event(attempt_id, "UNKNOWN", self._now())
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return self.get_status(attempt_id)
        try:
            receipt = self._provider.dispatch(request)
        except Exception:
            self._begin()
            try:
                self._event(attempt_id, "UNKNOWN", self._now())
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return self.get_status(attempt_id)
        return self.record_outcome(attempt_id, receipt)

    def _observation(self, receipt: str, request: AiDispatchRequest) -> AiProviderObservation:
        proof = self._provider.verify_persisted(self._conn, receipt, request)
        if (
            type(proof) is not AiProviderObservation
            or proof.claim_id != request.claim_id
            or proof.request_hash != request.request_hash
            or proof.outcome not in ("RESPONSE", "UNKNOWN", "NOT_SENT")
            or type(proof.observed_at) is not int
            or not request.claimed_at <= proof.observed_at <= self._now()
            or not _digest(proof.evidence_digest)
            or proof.raw_response is not None
            and (
                not isinstance(proof.raw_response, str) or len(proof.raw_response.encode()) > 16384
            )
            or proof.outcome != "RESPONSE"
            and proof.raw_response is not None
        ):
            raise AiInvocationError("Provider observation cannot be verified")
        return proof

    def record_outcome(self, attempt_id: str, observation_record_id: str) -> AiInvocationStatus:
        if not _reference(observation_record_id):
            raise AiInvocationError("Provider observation reference invalid")
        self._begin()
        try:
            row, material = self._attempt(attempt_id)
            target = self._target(row["intake_public_id"])
            if target["id"] != row["parent_parser_output_id"]:
                raise AiInvocationError("AI outcome target stale")
            self._input(material, historical=False)
            request = self._request(row, material)
            proof = self._observation(observation_record_id, request)
            old = self._conn.execute(
                "SELECT * FROM independent_ai_results WHERE observation_record_id=?",
                (observation_record_id,),
            ).fetchone()
            if old is not None:
                if old["attempt_id"] != attempt_id or _sealed(old) != dataclasses.asdict(proof):
                    raise AiInvocationError("Provider observation collision")
            else:
                validation = self._validate_observation(row, material, proof)
                result_id = "air_" + sha([attempt_id, observation_record_id])[:32]
                self._conn.execute(
                    "INSERT INTO independent_ai_results VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        result_id,
                        attempt_id,
                        observation_record_id,
                        proof.raw_response,
                        hashlib.sha256((proof.raw_response or "").encode()).hexdigest(),
                        canonical_json_text(dataclasses.asdict(proof)),
                        sha(dataclasses.asdict(proof)),
                        canonical_json_text(validation),
                        proof.observed_at,
                    ),
                )
            self._conn.commit()
            return self.get_status(attempt_id)
        except BaseException:
            self._conn.rollback()
            raise

    def _validate_observation(
        self,
        row: sqlite3.Row,
        material: dict[str, Any],
        proof: AiProviderObservation,
        *,
        historical: bool = False,
    ) -> dict[str, Any]:
        if proof.outcome != "RESPONSE":
            return {"state": proof.outcome, "reason": "transport_outcome"}
        if (
            proof.observed_provider_id != self._provider_binding.provider_id
            or proof.observed_model_id != self._provider_binding.model_id
        ):
            return {"state": "RESPONSE_REJECTED", "reason": "provider_model_mismatch"}
        if proof.observed_at >= row["deadline_at"]:
            return {"state": "LATE", "reason": "deadline"}
        from finance_core.parser_proposals.independent_ai_source_money import (
            validate_source_response,
        )

        return validate_source_response(
            self._conn, proof.raw_response, material, historical=historical
        )

    def _result(
        self,
        row: sqlite3.Row,
        material: dict[str, Any],
        result: sqlite3.Row,
        *,
        historical: bool = False,
    ) -> dict[str, Any]:
        persisted = _sealed(result)
        proof = self._observation(result["observation_record_id"], self._request(row, material))
        validation = self._validate_observation(row, material, proof, historical=historical)
        if (
            result["attempt_id"] != row["attempt_id"]
            or result["result_id"]
            != "air_" + sha([row["attempt_id"], result["observation_record_id"]])[:32]
            or type(result["observed_at"]) is not int
            or result["observed_at"] != proof.observed_at
            or canonical_json_text(dataclasses.asdict(proof)) != canonical_json_text(persisted)
            or result["raw_response"] != proof.raw_response
            or result["raw_response_hash"]
            != hashlib.sha256((proof.raw_response or "").encode()).hexdigest()
            or result["validation_json"] != canonical_json_text(validation)
        ):
            raise AiInvocationError("Provider result evidence changed")
        return validation

    def publish(self, attempt_id: str) -> AiInvocationStatus:
        self._begin()
        try:
            prior = self._conn.execute(
                "SELECT * FROM independent_ai_publications WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if prior is not None:
                result = self.get_status(attempt_id)
                self._conn.commit()
                return result
            row, material = self._attempt(attempt_id)
            if self._conn.execute(
                "SELECT 1 FROM independent_ai_events WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                raise AiInvocationError("Cancelled or uncertain invocation cannot publish")
            results = self._conn.execute(
                "SELECT * FROM independent_ai_results WHERE attempt_id=?", (attempt_id,)
            ).fetchall()
            if len(results) != 1:
                raise AiInvocationError("Publication requires exactly one verified result")
            validation = self._result(row, material, results[0])
            if validation["state"] != "VALID" or self._now() >= row["deadline_at"]:
                raise AiInvocationError("Only valid timely AI result may publish")
            target = self._target(row["intake_public_id"])
            if (
                target["id"] != row["parent_parser_output_id"]
                or compute_effective_proposal_content_hash(self._conn, target)
                != row["parent_content_hash"]
                or resolve_effective_payload(self._conn, target)[2] != row["parent_version"]
            ):
                raise AiInvocationError("AI publication target stale")
            self._input(material, historical=False)
            public_id = publish_in_transaction(
                self._conn, row, material, results[0], validation, self._now()
            )
            result = self.get_status(attempt_id)
            if (
                result.state != "PUBLISHED"
                or result.proposal_public_id != public_id
                or not result.is_current
            ):
                raise AiInvocationError("New AI publication graph is not the current verified leaf")
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def _event(self, identity: str, kind: str, now: int) -> None:
        if not self._conn.execute(
            "SELECT 1 FROM independent_ai_events WHERE attempt_id=? AND kind=?", (identity, kind)
        ).fetchone():
            self._conn.execute(
                "INSERT INTO independent_ai_events VALUES (?,?,?,?)",
                ("aie_" + sha([identity, kind])[:32], identity, kind, now),
            )

    def cancel(self, attempt_id: str) -> AiInvocationStatus:
        self._begin()
        try:
            self._attempt(attempt_id)
            self.get_status(attempt_id)
            if self._conn.execute(
                "SELECT 1 FROM independent_ai_publications WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                raise AiInvocationError("Published proposal cannot be cancelled as an invocation")
            claimed = self._conn.execute(
                "SELECT 1 FROM independent_ai_claims WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            self._event(attempt_id, "CANCEL_REQUESTED" if claimed else "CANCELLED", self._now())
            if claimed:
                self._event(attempt_id, "UNKNOWN", self._now())
            result = self.get_status(attempt_id)
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def get_status(self, attempt_id: str) -> AiInvocationStatus:
        from finance_core.application.review import review_snapshot

        with review_snapshot(self._conn):
            return self._get_status(attempt_id)

    def _get_status(self, attempt_id: str) -> AiInvocationStatus:
        row, material = self._attempt(attempt_id, historical=True)
        results = self._conn.execute(
            "SELECT * FROM independent_ai_results WHERE attempt_id=?", (attempt_id,)
        ).fetchall()
        claimed = self._conn.execute(
            "SELECT 1 FROM independent_ai_claims WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if claimed:
            self._request(row, material)
        validations = [self._result(row, material, result, historical=True) for result in results]
        current_row = self._conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (row["intake_public_id"],),
        ).fetchone()
        current_proposal = (
            ParserProposalRepository(self._conn).get_lineage_row_by_id(current_row[0])
            if current_row
            else None
        )
        if current_proposal is None:
            raise AiInvocationError("AI source current leaf is absent")
        self.verify_lineage(current_proposal["public_id"])
        publication = self._conn.execute(
            "SELECT * FROM independent_ai_publications WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if publication:
            child = ParserProposalRepository(self._conn).get_lineage_row_by_id(
                publication["child_parser_output_id"]
            )
            if child is None:
                raise AiInvocationError("Published child absent")
            self.verify_lineage(child["public_id"])
            current = self._conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
                (row["intake_public_id"],),
            ).fetchone()
            return AiInvocationStatus(
                attempt_id,
                "PUBLISHED",
                proposal_public_id=child["public_id"],
                proposal_version=0,
                effective_content_hash=publication["child_content_hash"],
                is_current=bool(
                    current
                    and current[0] == child["id"]
                    and resolve_effective_payload(self._conn, child)[2] == 0
                    and compute_effective_proposal_content_hash(self._conn, child)
                    == publication["child_content_hash"]
                ),
            )
        events = {
            r[0]
            for r in self._conn.execute(
                "SELECT kind FROM independent_ai_events WHERE attempt_id=?", (attempt_id,)
            )
        }
        if "CANCELLED" in events:
            return AiInvocationStatus(attempt_id, "CANCELLED")
        if "CANCEL_REQUESTED" in events:
            return AiInvocationStatus(attempt_id, "UNKNOWN", "cancel_requested")
        if "UNKNOWN" in events:
            return AiInvocationStatus(attempt_id, "UNKNOWN", "unresolved_dispatch")
        if validations:
            if len(validations) != 1:
                return AiInvocationStatus(attempt_id, "UNKNOWN", "multiple_observations")
            if validations[0]["state"] == "VALID" and not self._current_target(row):
                return AiInvocationStatus(attempt_id, "RESULT_STALE", "target_changed")
            return AiInvocationStatus(attempt_id, validations[0]["state"], validations[0]["reason"])
        if not claimed and not self._current_target(row):
            return AiInvocationStatus(attempt_id, "RESULT_STALE", "target_changed")
        return AiInvocationStatus(
            attempt_id,
            "UNKNOWN" if claimed else "PREPARED",
            "unresolved_dispatch" if claimed else None,
        )

    def recover(self, attempt_id: str) -> AiInvocationStatus:
        self._begin()
        try:
            row, _ = self._attempt(attempt_id, historical=True)
            status = self.get_status(attempt_id)
            if status.state == "UNKNOWN" and self._current_target(row):
                self._event(attempt_id, "UNKNOWN", self._now())
            self._conn.commit()
            return status
        except BaseException:
            self._conn.rollback()
            raise

    def _current_target(self, row: sqlite3.Row) -> bool:
        current = self._conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (row["intake_public_id"],),
        ).fetchone()
        proposal = (
            ParserProposalRepository(self._conn).get_lineage_row_by_id(current[0])
            if current
            else None
        )
        if (
            proposal is None
            or proposal["id"] != row["parent_parser_output_id"]
            or proposal["parse_status"]
            not in ("parsed_pending_confirmation", "edited_pending_confirmation")
            or resolve_effective_payload(self._conn, proposal)[2] != row["parent_version"]
            or compute_effective_proposal_content_hash(self._conn, proposal)
            != row["parent_content_hash"]
        ):
            return False
        try:
            self._no_decision(row["intake_public_id"])
        except AiInvocationError:
            return False
        return True

    def verify_in_snapshot(
        self, connection: sqlite3.Connection, expected: ExpectedAiLineage
    ) -> dict[str, Any] | None:
        if (
            connection is not self._conn
            or expected.binding != self._binding
            or self._source(expected.source.intake_public_id, self._now()) != expected.source
        ):
            raise AiInvocationError("AI lineage authority connection/binding differs")
        proposal = ParserProposalRepository(self._conn).get_by_public_id(
            expected.proposal_public_id
        )
        if (
            proposal is None
            or compute_effective_proposal_content_hash(self._conn, proposal)
            != expected.proposal_content_hash
            or resolve_effective_payload(self._conn, proposal)[2] != expected.proposal_version
        ):
            raise AiInvocationError("AI lineage authority expected revision differs")
        return self.verify_lineage(expected.proposal_public_id)

    def verify_lineage(self, proposal_public_id: str) -> dict[str, Any] | None:
        from finance_core.application.review import review_snapshot

        with review_snapshot(self._conn):
            return self._verify_lineage(proposal_public_id)

    def _verify_lineage(self, proposal_public_id: str) -> dict[str, Any] | None:
        from finance_core.parser_proposals.independent_ai_lineage import (
            verify_independent_ai_lineage,
        )

        proposal = ParserProposalRepository(self._conn).get_by_public_id(proposal_public_id)
        if proposal is None:
            raise AiInvocationError("AI lineage proposal absent")
        proposal = ParserProposalRepository(self._conn).get_lineage_row_by_id(proposal["id"])
        if proposal is None:
            raise AiInvocationError("AI lineage full proposal absent")
        lineage = verify_independent_ai_lineage(
            self._conn,
            proposal,
            content_hash=compute_effective_proposal_content_hash(self._conn, proposal),
            proposal_version=resolve_effective_payload(self._conn, proposal)[2],
            require_resolved=False,
        )
        if lineage is None:
            return None
        for identity in lineage["attempt_ids"]:
            attempt, material = self._attempt(identity, historical=True)
            result = self._conn.execute(
                (
                    "SELECT r.* FROM independent_ai_results r JOIN independent_ai_publ"
                    "ications p ON p.result_id=r.result_id WHERE p.attempt_id=?"
                ),
                (identity,),
            ).fetchone()
            if (
                result is None
                or self._result(attempt, material, result, historical=True)["state"] != "VALID"
            ):
                raise AiInvocationError("Published invocation has no genuine valid observation")
        return lineage


# The current owner extends the historical verifier after its class is defined.
from finance_core.application.ai_invocation_v2 import AiInvocationV2Mixin  # noqa: E402


class AiInvocationService(AiInvocationV2Mixin):
    """New operations use v2; immutable v1 invocation evidence is read only."""


__all__ = ["AiInvocationService"]
