"""Read-only validation of D2 conditional receipt-finalization authority."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Mapping

from finance_core.calculation.authoritative_snapshot import canonical_json_text


class D2ConditionalAuthorityError(RuntimeError):
    """The D2 proof chain is absent, stale, or contradictory."""


def require_d2_conditional_authority(
    conn: sqlite3.Connection,
    authorization: Mapping[str, object],
) -> None:
    """Verify the complete immutable D2 proof chain using SELECTs only."""
    authorization_id = str(authorization.get("authorization_id") or "")
    snapshot_id = str(authorization.get("calculation_snapshot_id") or "")
    row = conn.execute(
        """
        SELECT proof.*, decisions.review_public_id AS decision_review_public_id,
               evidence.fact_set_public_id AS evidence_fact_set_public_id,
               evidence.evidence_hash AS fact_set_evidence_hash,
               fact_sets.fact_set_result_hash,
               snapshot_binding.fact_set_public_id AS snapshot_fact_set_public_id,
               authorization_binding.fact_set_public_id AS authorization_fact_set_public_id
        FROM d2_conditional_authorization_proofs AS proof
        JOIN d2_posting_decisions AS decisions
          ON decisions.decision_public_id = proof.decision_public_id
        JOIN d2_posting_receipt_evidence AS evidence
          ON evidence.decision_public_id = decisions.decision_public_id
         AND evidence.evidence_type = 'fact_set'
        JOIN receipt_item_allocation_fact_sets AS fact_sets
          ON fact_sets.fact_set_public_id = evidence.fact_set_public_id
        JOIN receipt_fact_set_binding_evidence AS snapshot_binding
          ON snapshot_binding.calculation_snapshot_public_id = proof.calculation_snapshot_id
        JOIN receipt_fact_set_binding_evidence AS authorization_binding
          ON authorization_binding.finalization_authorization_id = proof.authorization_id
        WHERE proof.authorization_id = ?
        """,
        (authorization_id,),
    ).fetchone()
    if row is None:
        raise D2ConditionalAuthorityError("D2 conditional authorization proof is missing")
    values = dict(row)
    expected_proof_hash = hashlib.sha256(
        canonical_json_text(
            {
                "authorization_id": authorization_id,
                "decision_public_id": values["decision_public_id"],
                "review_public_id": values["review_public_id"],
                "fact_set_public_id": values["fact_set_public_id"],
                "calculation_snapshot_id": values["calculation_snapshot_id"],
                "projection_hash": values["reviewed_projection_hash"],
                "proof_version": "d2_conditional_v1",
            }
        ).encode("utf-8")
    ).hexdigest()
    if (
        values["proof_version"] != "d2_conditional_v1"
        or values["calculation_snapshot_id"] != snapshot_id
        or values["review_public_id"] != values["decision_review_public_id"]
        or values["fact_set_public_id"] != values["evidence_fact_set_public_id"]
        or values["fact_set_public_id"] != values["snapshot_fact_set_public_id"]
        or values["fact_set_public_id"] != values["authorization_fact_set_public_id"]
        or not hmac.compare_digest(
            str(values["fact_set_evidence_hash"]), str(values["fact_set_result_hash"])
        )
        or not hmac.compare_digest(
            str(values["reviewed_projection_hash"]), str(values["snapshot_projection_hash"])
        )
        or not hmac.compare_digest(str(values["equality_proof_hash"]), expected_proof_hash)
    ):
        raise D2ConditionalAuthorityError("D2 conditional authorization proof is contradictory")


__all__ = ["D2ConditionalAuthorityError", "require_d2_conditional_authority"]
