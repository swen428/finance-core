"""Read-only validation of D2 conditional receipt-finalization authority."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Mapping

from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.money import canonical_money_str, money_decimal


class D2ConditionalAuthorityError(RuntimeError):
    """The D2 proof chain is absent, stale, or contradictory."""


def build_d2_receipt_projection(
    *,
    merchant: str,
    receipt_date: str,
    currency: str,
    payer_participant_public_id: str,
    calculation: Mapping[str, object],
) -> dict[str, object]:
    """Rebuild the user-visible receipt projection from Python-owned truth."""
    try:
        total_paid = canonical_money_str(
            money_decimal(str(calculation["total_paid"]), label="D2 total paid"), currency
        )
        total_to_collect = canonical_money_str(
            money_decimal(str(calculation["total_to_collect"]), label="D2 total to collect"),
            currency,
        )
        shares = calculation["participant_shares"]
        obligations = calculation["settlement_obligations"]
        if not isinstance(shares, Mapping) or not isinstance(obligations, list):
            raise TypeError
        personal_share = canonical_money_str(
            money_decimal(str(shares[payer_participant_public_id]), label="D2 personal share"),
            currency,
        )
        normalized_obligations = []
        for obligation in obligations:
            if not isinstance(obligation, Mapping):
                raise TypeError
            normalized_obligations.append(
                {
                    "debtor": str(obligation["debtor"]),
                    "creditor": str(obligation["creditor"]),
                    "amount": canonical_money_str(
                        money_decimal(str(obligation["amount"]), label="D2 settlement obligation"),
                        currency,
                    ),
                    "currency": str(obligation["currency"]),
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise D2ConditionalAuthorityError(
            "D2 authoritative calculation cannot form the reviewed projection"
        ) from exc
    return {
        "amount": total_paid,
        "currency": currency,
        "transaction_date": receipt_date,
        "merchant": merchant,
        "account": "unspecified",
        "receipt_total": total_paid,
        "personal_share": personal_share,
        "calculation": {
            "total_paid": total_paid,
            "total_to_collect": total_to_collect,
            "settlement_obligations": normalized_obligations,
        },
    }


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
               decisions.confirmation_public_id AS decision_confirmation_public_id,
               reviews.visible_projection_json AS durable_review_projection_json,
               reviews.visible_projection_hash AS durable_review_projection_hash,
               reviews.authenticated_actor_id AS review_actor_id,
               reviews.telegram_account_id AS review_account_id,
               reviews.telegram_conversation_id AS review_conversation_id,
               reviews.conversation_binding_id AS review_binding_id,
               refs.authenticated_actor_id AS reference_actor_id,
               refs.channel_account_id AS reference_account_id,
               refs.channel_conversation_id AS reference_conversation_id,
               refs.conversation_binding_id AS reference_binding_id,
               confirmations.confirmation_public_id AS durable_confirmation_public_id,
               confirmations.authenticated_actor_id AS confirmation_actor_id,
               evidence.fact_set_public_id AS evidence_fact_set_public_id,
               evidence.evidence_hash AS fact_set_evidence_hash,
               fact_sets.fact_set_result_hash,
               receipts.merchant AS receipt_merchant,
               receipts.receipt_datetime AS receipt_date,
               receipts.currency AS receipt_currency,
               payer.public_id AS receipt_payer_public_id,
               snapshots.output_payload_json,
               snapshot_binding.fact_set_public_id AS snapshot_fact_set_public_id,
               authorization_binding.fact_set_public_id AS authorization_fact_set_public_id
        FROM d2_conditional_authorization_proofs AS proof
        JOIN d2_posting_decisions AS decisions
          ON decisions.decision_public_id = proof.decision_public_id
        JOIN d2_posting_reviews AS reviews
          ON reviews.review_public_id = decisions.review_public_id
         AND reviews.review_public_id = proof.review_public_id
        JOIN d2_posting_review_action_bindings AS bindings
          ON bindings.review_public_id = reviews.review_public_id
         AND bindings.reference_id = decisions.reference_id
        JOIN openclaw_human_action_references AS refs
          ON refs.id = decisions.reference_id
        JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = decisions.reference_id
        JOIN parser_proposal_authorizations AS confirmations
          ON confirmations.confirmation_public_id = decisions.confirmation_public_id
        JOIN d2_posting_receipt_evidence AS evidence
          ON evidence.decision_public_id = decisions.decision_public_id
         AND evidence.evidence_type = 'fact_set'
        JOIN receipt_item_allocation_fact_sets AS fact_sets
          ON fact_sets.fact_set_public_id = evidence.fact_set_public_id
        JOIN receipts ON receipts.id = fact_sets.receipt_id
        JOIN participants AS payer ON payer.id = receipts.payer_participant_id
        JOIN authoritative_calculation_snapshots AS snapshots
          ON snapshots.snapshot_public_id = proof.calculation_snapshot_id
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
    try:
        calculation_value = canonical_json_value(
            str(values["output_payload_json"]), label="D2 authoritative snapshot output"
        )
        if not isinstance(calculation_value, dict):
            raise D2ConditionalAuthorityError("D2 authoritative snapshot output is malformed")
        projection = build_d2_receipt_projection(
            merchant=str(values["receipt_merchant"]),
            receipt_date=str(values["receipt_date"]),
            currency=str(values["receipt_currency"]),
            payer_participant_public_id=str(values["receipt_payer_public_id"]),
            calculation=calculation_value,
        )
    except (TypeError, ValueError) as exc:
        raise D2ConditionalAuthorityError("D2 authoritative snapshot output is malformed") from exc
    authoritative_projection_hash = hashlib.sha256(
        canonical_json_text(projection).encode("utf-8")
    ).hexdigest()
    durable_review_hash = hashlib.sha256(
        str(values["durable_review_projection_json"]).encode("utf-8")
    ).hexdigest()
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
    expected_participant_authority_hash = hashlib.sha256(
        canonical_json_text(
            {
                "active_self_count": 1,
                "authorization_id": authorization_id,
                "payer_participant_public_id": values["receipt_payer_public_id"],
                "payer_was_active_self": 1,
                "proof_version": "d2_participant_authority_v1",
                "review_public_id": values["review_public_id"],
            }
        ).encode("utf-8")
    ).hexdigest()
    if (
        values["proof_version"] != "d2_conditional_v1"
        or values["calculation_snapshot_id"] != snapshot_id
        or values["review_public_id"] != values["decision_review_public_id"]
        or values["decision_confirmation_public_id"] != values["durable_confirmation_public_id"]
        or str(authorization.get("actor_id") or "") != values["review_actor_id"]
        or values["review_actor_id"] != values["reference_actor_id"]
        or values["review_actor_id"] != values["confirmation_actor_id"]
        or values["review_account_id"] != values["reference_account_id"]
        or values["review_conversation_id"] != values["reference_conversation_id"]
        or values["review_binding_id"] != values["reference_binding_id"]
        or values["fact_set_public_id"] != values["evidence_fact_set_public_id"]
        or values["payer_participant_public_id"] != values["receipt_payer_public_id"]
        or int(values["payer_was_active_self"]) != 1
        or int(values["active_self_count"]) != 1
        or not hmac.compare_digest(
            str(values["participant_authority_hash"]), expected_participant_authority_hash
        )
        or values["fact_set_public_id"] != values["snapshot_fact_set_public_id"]
        or values["fact_set_public_id"] != values["authorization_fact_set_public_id"]
        or not hmac.compare_digest(
            str(values["fact_set_evidence_hash"]), str(values["fact_set_result_hash"])
        )
        or not hmac.compare_digest(
            str(values["reviewed_projection_hash"]), str(values["snapshot_projection_hash"])
        )
        or not hmac.compare_digest(
            str(values["reviewed_projection_hash"]),
            str(values["durable_review_projection_hash"]),
        )
        or not hmac.compare_digest(
            str(values["durable_review_projection_hash"]), durable_review_hash
        )
        or not hmac.compare_digest(
            str(values["snapshot_projection_hash"]), authoritative_projection_hash
        )
        or not hmac.compare_digest(str(values["equality_proof_hash"]), expected_proof_hash)
    ):
        raise D2ConditionalAuthorityError("D2 conditional authorization proof is contradictory")


__all__ = [
    "D2ConditionalAuthorityError",
    "build_d2_receipt_projection",
    "require_d2_conditional_authority",
]
