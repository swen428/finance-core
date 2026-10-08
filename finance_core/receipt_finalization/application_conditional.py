"""SELECT-only validation of independent accepted receipt authority.

The accepted Core record is the historical decision owner. Adapter signatures
are verified at acceptance; this boundary verifies its immutable commitment,
all cross-bindings, and Python-owned facts inside the finalizer's snapshot.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from finance_core.application.admission import SOURCE_SCHEMA
from finance_core.application.posting_contract import (
    POSTING_DECISION_SCHEMA,
    POSTING_REVIEW_SCHEMA,
    PostingContractError,
    posting_review_sha256,
    require_posting_schema,
)
from finance_core.bookkeeping_metadata import build_application_receipt_projection
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.calculators.receipt_calculator_input_projection import (
    project_receipt_calculator_input,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.receipt_finalization.models import FinalizationInput, to_settlement_obligations
from finance_core.receipt_finalization.persistence import read_fact_set_binding_evidence
from finance_core.receipt_finalization.snapshot_authority import read_snapshot_bound_authority

APPLICATION_CONDITIONAL_VERSION = "application_conditional_v1"


class ApplicationConditionalAuthorityError(RuntimeError):
    """Independent conditional authority is missing or contradictory."""


def require_application_authorization_version(
    conn: sqlite3.Connection, authorization: Mapping[str, object]
) -> None:
    """An independently owned snapshot cannot fall back to old authority."""
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('application_posting_receipt_evidence','application_conditional_authorization_proofs',"
            "'receipt_fact_set_binding_evidence','receipt_item_allocation_fact_sets',"
            "'receipt_proposal_conversions','parser_proposal_authorizations')"
        ).fetchall()
    }
    owned = False
    if "application_posting_receipt_evidence" in tables:
        owned = (
            conn.execute(
                "SELECT 1 FROM application_posting_receipt_evidence "
                "WHERE evidence_type = 'snapshot' AND evidence_public_id = ?",
                (authorization.get("calculation_snapshot_id"),),
            ).fetchone()
            is not None
        )
    if "application_conditional_authorization_proofs" in tables:
        owned = (
            owned
            or conn.execute(
                "SELECT 1 FROM application_conditional_authorization_proofs "
                "WHERE authorization_id = ?",
                (authorization.get("authorization_id"),),
            ).fetchone()
            is not None
        )
    if {
        "receipt_fact_set_binding_evidence",
        "receipt_item_allocation_fact_sets",
        "receipt_proposal_conversions",
        "parser_proposal_authorizations",
    } <= tables:
        owned = (
            owned
            or conn.execute(
                """
            SELECT 1 FROM receipt_fact_set_binding_evidence AS bindings
            JOIN receipt_item_allocation_fact_sets AS fact_sets
              ON fact_sets.fact_set_public_id = bindings.fact_set_public_id
            JOIN receipt_proposal_conversions AS conversions
              ON conversions.command_public_id = fact_sets.conversion_command_public_id
            JOIN parser_proposal_authorizations AS confirmations
              ON confirmations.confirmation_public_id = conversions.confirmation_public_id
            WHERE bindings.calculation_snapshot_public_id = ?
              AND confirmations.confirmation_channel = 'independent_application'
            """,
                (authorization.get("calculation_snapshot_id"),),
            ).fetchone()
            is not None
        )
    if owned and authorization.get("authorization_version") != APPLICATION_CONDITIONAL_VERSION:
        raise ApplicationConditionalAuthorityError(
            "Independent receipt cannot use manual or D2 authorization"
        )


def material_sha256(material: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(dict(material)).encode("utf-8")).hexdigest()


def _object(text: object, label: str) -> dict[str, Any]:
    value = canonical_json_value(str(text), label=label)
    if not isinstance(value, dict) or canonical_json_text(value) != text:
        raise ApplicationConditionalAuthorityError(f"{label} is not a canonical object")
    return value


def require_application_receipt_acceptance(
    conn: sqlite3.Connection, attempt_id: str
) -> dict[str, Any]:
    """Verify frozen review/decision/confirmation without fresh expiry semantics."""
    try:
        require_posting_schema(conn)
    except PostingContractError as exc:
        raise ApplicationConditionalAuthorityError(str(exc)) from exc
    row = conn.execute(
        """
        SELECT attempts.attempt_id, attempts.review_id, attempts.source_event_key,
               attempts.intake_public_id,
               attempts.stage, reviews.parser_output_id, reviews.material_json AS review_json,
               reviews.review_hash, decisions.decision_namespace, decisions.decision_id,
               decisions.decision_digest, decisions.confirmation_public_id,
               decisions.material_json AS decision_json, decisions.accepted_at,
               confirmations.proposal_content_hash, confirmations.authenticated_actor_id,
               confirmations.confirmation_state, confirmations.actor_type,
               confirmations.confirmation_channel, confirmations.revoked_at,
               confirmations.decided_at,
               confirmations.parser_output_id AS confirmation_parser_output_id
        FROM application_posting_attempts AS attempts
        JOIN application_posting_reviews AS reviews ON reviews.review_id = attempts.review_id
        JOIN application_posting_decisions AS decisions
          ON decisions.attempt_id = attempts.attempt_id
        JOIN parser_proposal_authorizations AS confirmations
          ON confirmations.confirmation_public_id = decisions.confirmation_public_id
        WHERE attempts.attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise ApplicationConditionalAuthorityError("Independent receipt acceptance is missing")
    values = dict(row)
    try:
        review = _object(values["review_json"], "Independent posting review")
        decision = _object(values["decision_json"], "Independent posting decision")
        binding, source, proposal_review = (
            review["binding"],
            review["source"],
            review["proposal_review"],
        )
        if not all(isinstance(value, dict) for value in (binding, source, proposal_review)):
            raise TypeError
        proposal = ParserProposalRepository(conn).get(int(values["parser_output_id"]))
        if proposal is None:
            raise ApplicationConditionalAuthorityError("Accepted proposal is missing")
        _payload, _completion, version = resolve_effective_payload(conn, proposal)
        content_hash = compute_effective_proposal_content_hash(conn, {"id": proposal["id"]})
        from finance_core.parser_proposals.amendment_lineage import (
            verify_independent_amendment_descendant,
        )

        verify_independent_amendment_descendant(
            conn, proposal, content_hash=content_hash, proposal_version=version
        )
        if (
            review["schema"] != POSTING_REVIEW_SCHEMA
            or review["posting_path"] != "personal_receipt"
            or not hmac.compare_digest(posting_review_sha256(review), str(values["review_hash"]))
            or decision["schema"] != POSTING_DECISION_SCHEMA
            or decision["action"] != "confirm"
            or decision["consumed"] is not False
            or decision["decision_id"] != values["decision_id"]
            or decision["decision_digest"] != values["decision_digest"]
            or decision["namespace"] != values["decision_namespace"]
            or decision["namespace"] != binding["decision_namespace"]
            or decision["key_id"] != binding["decision_key_id"]
            or decision["instance_id"] != binding["instance_id"]
            or decision["human_principal_id"] != binding["human_principal_id"]
            or decision["source_evidence_id"] != source["evidence_id"]
            or decision["source_evidence_digest"] != source["evidence_digest"]
            or source["schema"] != SOURCE_SCHEMA
            or source["namespace"] != binding["source_namespace"]
            or source["key_id"] != binding["source_key_id"]
            or source["instance_id"] != binding["instance_id"]
            or source["submission_client_id"] != binding["submission_client_id"]
            or values["source_event_key"]
            != hashlib.sha256(
                canonical_json_text(
                    [source["namespace"], source["instance_id"], source["source_event_id"]]
                ).encode("utf-8")
            ).hexdigest()
            or source["intake_public_id"] != proposal["source_public_id"]
            or source["intake_public_id"] != proposal_review["intake_public_id"]
            or source["intake_public_id"] != values["intake_public_id"]
            or decision["review_id"] != values["review_id"]
            or source["evidence_id"] != review["source_evidence_id"]
            or source["evidence_digest"] != review["source_evidence_digest"]
            or decision["proposal_public_id"] != proposal["public_id"]
            or decision["proposal_public_id"] != proposal_review["proposal_public_id"]
            or decision["proposal_version"] != version
            or decision["proposal_version"] != proposal_review["proposal_version"]
            or decision["proposal_content_hash"] != content_hash
            or decision["proposal_content_hash"] != proposal_review["effective_content_hash"]
            or decision["review_projection_hash"] != values["review_hash"]
            or values["proposal_content_hash"] != content_hash
            or values["confirmation_parser_output_id"] != values["parser_output_id"]
            or values["authenticated_actor_id"] != binding["human_principal_id"]
            or values["confirmation_state"] != "confirmed"
            or values["actor_type"] != "human"
            or values["confirmation_channel"] != "independent_application"
            or values["revoked_at"] is not None
            or datetime.fromisoformat(values["decided_at"]).timestamp() != values["accepted_at"]
            or proposal["parse_status"] != "confirmed"
            or type(values["accepted_at"]) is not int
            or not review["prepared_at"] <= values["accepted_at"] < review["expires_at"]
            or not decision["issued_at"] <= values["accepted_at"] < decision["expires_at"]
            or not decision["display_id"]
            or not decision["display_evidence_digest"]
            or not review["payer_participant_public_id"]
        ):
            raise ApplicationConditionalAuthorityError(
                "Independent receipt acceptance is contradictory"
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplicationConditionalAuthorityError(
            "Independent receipt acceptance is malformed"
        ) from exc
    event = conn.execute(
        "SELECT from_stage,created_at FROM application_posting_events "
        "WHERE attempt_id = ? AND to_stage = 'accepted'",
        (attempt_id,),
    ).fetchone()
    if event is None or event[0] is not None or event[1] != values["accepted_at"]:
        raise ApplicationConditionalAuthorityError("Independent acceptance event is contradictory")
    receipt_evidence = {
        str(item["evidence_type"]): dict(item)
        for item in conn.execute(
            "SELECT * FROM application_posting_receipt_evidence "
            "WHERE attempt_id = ? AND evidence_type != 'authorization' ORDER BY evidence_type",
            (attempt_id,),
        ).fetchall()
    }
    for item in receipt_evidence.values():
        _object(item["material_json"], "Independent receipt evidence")
    return {
        **values,
        "review": review,
        "decision": decision,
        "receipt_evidence": receipt_evidence,
    }


def participant_authority_sha256(proof: Mapping[str, object]) -> str:
    return material_sha256(
        {
            "schema": "application_participant_authority_v1",
            "authorization_id": proof["authorization_id"],
            "attempt_id": proof["attempt_id"],
            "review_id": proof["review_id"],
            "payer_participant_public_id": proof["payer_participant_public_id"],
            "payer_was_active_self": 1,
            "active_self_count": 1,
        }
    )


def conditional_proof_sha256(proof: Mapping[str, object], acceptance: Mapping[str, Any]) -> str:
    return material_sha256(
        {
            "proof_version": APPLICATION_CONDITIONAL_VERSION,
            **{
                key: proof[key]
                for key in (
                    "authorization_id",
                    "attempt_id",
                    "review_id",
                    "confirmation_public_id",
                    "fact_set_public_id",
                    "fact_set_version",
                    "fact_set_input_hash",
                    "fact_set_result_hash",
                    "calculation_snapshot_id",
                    "calculation_snapshot_hash",
                    "reviewed_projection_hash",
                    "snapshot_projection_hash",
                    "participant_authority_hash",
                )
            },
            "review_hash": acceptance["review_hash"],
            "decision_digest": acceptance["decision_digest"],
            "decision_material_hash": material_sha256(acceptance["decision"]),
            "source_event_key": acceptance["source_event_key"],
            "accepted_at": acceptance["accepted_at"],
            "proposal_content_hash": acceptance["proposal_content_hash"],
            "receipt_evidence_material_hash": material_sha256(acceptance["receipt_evidence"]),
        }
    )


def require_application_conditional_authority(
    conn: sqlite3.Connection,
    authorization: Mapping[str, object],
    *,
    require_current_payer: bool = False,
    require_authorization_evidence: bool = False,
) -> None:
    """Verify the independent proof in the caller's owned read/write snapshot.

    Fresh finalization additionally checks current payer rights. Historical
    consumed replay retains the frozen witness after those rights change.
    """
    try:
        require_posting_schema(conn)
        row = conn.execute(
            "SELECT * FROM application_conditional_authorization_proofs WHERE authorization_id = ?",
            (str(authorization.get("authorization_id") or ""),),
        ).fetchone()
        if row is None:
            raise ApplicationConditionalAuthorityError("Independent conditional proof is missing")
        proof = dict(row)
        durable_auth = conn.execute(
            "SELECT authorization_version,calculation_snapshot_id,actor_id,created_at,"
            "content_hash,authorization_state "
            "FROM receipt_finalization_authorizations WHERE authorization_id = ?",
            (proof["authorization_id"],),
        ).fetchone()
        if (
            durable_auth is None
            or durable_auth["authorization_version"] != APPLICATION_CONDITIONAL_VERSION
            or durable_auth["calculation_snapshot_id"] != proof["calculation_snapshot_id"]
            or durable_auth["actor_id"] != authorization.get("actor_id")
            or durable_auth["created_at"] != proof["created_at"]
        ):
            raise ApplicationConditionalAuthorityError(
                "Independent receipt authorization identity is contradictory"
            )
        acceptance = require_application_receipt_acceptance(conn, str(proof["attempt_id"]))
        review = acceptance["review"]
        authorization_evidence = conn.execute(
            "SELECT evidence_public_id,evidence_hash,material_json "
            "FROM application_posting_receipt_evidence "
            "WHERE attempt_id = ? AND evidence_type = 'authorization'",
            (proof["attempt_id"],),
        ).fetchone()
        if authorization_evidence is None:
            if require_authorization_evidence or durable_auth["authorization_state"] == "consumed":
                raise ApplicationConditionalAuthorityError(
                    "Independent authorization evidence is missing"
                )
        elif tuple(authorization_evidence) != (
            proof["authorization_id"],
            durable_auth["content_hash"],
            canonical_json_text(
                {
                    "authorization_id": proof["authorization_id"],
                    "content_hash": durable_auth["content_hash"],
                }
            ),
        ):
            raise ApplicationConditionalAuthorityError(
                "Independent authorization evidence is contradictory"
            )
        snapshot = read_snapshot_bound_authority(
            conn,
            snapshot_public_id=str(proof["calculation_snapshot_id"]),
            expected_combined_hash=str(proof["calculation_snapshot_hash"]),
        )
        if snapshot is None:
            raise ApplicationConditionalAuthorityError(
                "Independent receipt snapshot has no fact binding"
            )
        binding = snapshot.active_fact_set_binding
        projection = project_receipt_calculator_input(conn, binding.receipt_public_id)
        calculation = calculate_receipt_split(projection.calculator_input)
        recalculated = FinalizationInput(
            calculation_run_public_id=snapshot.calculation_run_public_id,
            receipt_group_public_id=snapshot.receipt_group_public_id,
            currency=snapshot.currency,
            payer_participant_public_id=str(proof["payer_participant_public_id"]),
            settlement_obligations=to_settlement_obligations(
                calculation["settlement_obligations"], snapshot.currency
            ),
            calculation_snapshot=calculation,
            source_evidence_refs=snapshot.source_references,
        ).calculation_snapshot
        if recalculated != snapshot.output_payload:
            raise ApplicationConditionalAuthorityError(
                "Independent receipt snapshot differs from Python calculation"
            )
        evidence = acceptance["receipt_evidence"]
        actual = build_application_receipt_projection(
            bookkeeping_metadata=snapshot.confirmed_receipt_identity.bookkeeping_metadata,
            merchant=snapshot.confirmed_receipt_identity.merchant,
            receipt_date=snapshot.confirmed_receipt_identity.receipt_date,
            currency=snapshot.currency,
            payer_participant_public_id=str(proof["payer_participant_public_id"]),
            calculation=snapshot.output_payload,
        )
        for record_type, public_id in (
            ("calculation_snapshot", proof["calculation_snapshot_id"]),
            ("finalization_authorization", proof["authorization_id"]),
        ):
            if (
                read_fact_set_binding_evidence(
                    conn, bound_record_type=record_type, bound_record_public_id=str(public_id)
                )
                != binding
            ):
                raise ApplicationConditionalAuthorityError(
                    "Independent downstream fact binding differs"
                )
        if (
            proof["proof_version"] != APPLICATION_CONDITIONAL_VERSION
            or proof["calculation_snapshot_id"] != authorization.get("calculation_snapshot_id")
            or authorization.get("actor_id") != review["binding"]["human_principal_id"]
            or proof["review_id"] != acceptance["review_id"]
            or proof["confirmation_public_id"] != acceptance["confirmation_public_id"]
            or proof["payer_participant_public_id"] != review["payer_participant_public_id"]
            or proof["payer_was_active_self"] != 1
            or proof["active_self_count"] != 1
            or proof["fact_set_public_id"] != binding.fact_set_public_id
            or proof["fact_set_version"] != binding.fact_set_version
            or proof["fact_set_input_hash"] != binding.fact_set_input_hash
            or proof["fact_set_result_hash"] != binding.fact_set_result_hash
            or projection.fact_set_public_id != binding.fact_set_public_id
            or projection.fact_set_version != binding.fact_set_version
            or projection.fact_set_input_hash != binding.fact_set_input_hash
            or projection.fact_set_result_hash != binding.fact_set_result_hash
            or projection.calculator_input.get("payer") != proof["payer_participant_public_id"]
            or projection.calculator_input.get("participants")
            != [proof["payer_participant_public_id"]]
            or snapshot.output_payload.get("payer") != proof["payer_participant_public_id"]
            or snapshot.output_payload.get("participants") != [proof["payer_participant_public_id"]]
            or projection.source_evidence.confirmation_public_id
            != acceptance["confirmation_public_id"]
            or projection.source_evidence.proposal_content_hash
            != acceptance["proposal_content_hash"]
            or evidence["conversion"]["evidence_public_id"]
            != projection.conversion_command_public_id
            or evidence["conversion"]["evidence_hash"] != projection.conversion_result_hash
            or evidence["fact_set"]["evidence_public_id"] != binding.fact_set_public_id
            or evidence["fact_set"]["evidence_hash"] != binding.fact_set_result_hash
            or evidence["snapshot"]["evidence_public_id"] != proof["calculation_snapshot_id"]
            or evidence["snapshot"]["evidence_hash"] != proof["calculation_snapshot_hash"]
            or not hmac.compare_digest(
                material_sha256(review["financial_projection"]),
                str(proof["reviewed_projection_hash"]),
            )
            or actual != review["financial_projection"]
            or not hmac.compare_digest(
                material_sha256(actual), str(proof["snapshot_projection_hash"])
            )
            or proof["snapshot_projection_hash"] != proof["reviewed_projection_hash"]
            or not hmac.compare_digest(
                participant_authority_sha256(proof), str(proof["participant_authority_hash"])
            )
            or not hmac.compare_digest(
                conditional_proof_sha256(proof, acceptance), str(proof["equality_proof_hash"])
            )
        ):
            raise ApplicationConditionalAuthorityError(
                "Independent conditional proof is contradictory"
            )
        if require_current_payer:
            payers = conn.execute(
                "SELECT public_id FROM participants "
                "WHERE is_self = 1 AND is_active = 1 ORDER BY public_id"
            ).fetchall()
            if [str(payer[0]) for payer in payers] != [proof["payer_participant_public_id"]]:
                raise ApplicationConditionalAuthorityError(
                    "Independent receipt payer authority changed"
                )
    except ApplicationConditionalAuthorityError:
        raise
    except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise ApplicationConditionalAuthorityError(
            "Independent conditional proof cannot be verified"
        ) from exc
