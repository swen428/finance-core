"""Original independent posting verification, without correction-aware recursion."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from finance_core.application.admission import VerifiedSource
from finance_core.application.independent_correction_contract import (
    IndependentCorrectionIntegrityError,
    correction_hash,
)
from finance_core.bookkeeping_metadata import read_receipt_bookkeeping_metadata
from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeSnapshotRepository,
    canonical_json_text,
)

if TYPE_CHECKING:
    from finance_core.application.posting import PostingService


@dataclass(frozen=True)
class IndependentCorrectionFields:
    amount: str
    currency: str
    transaction_date: str
    merchant: str | None
    description: str | None
    category: str | None

    def as_dict(self) -> dict[str, str | None]:
        return vars(self).copy()


@dataclass(frozen=True)
class IndependentOriginalSource:
    target_id: str
    attempt_id: str
    route: str
    actor: str
    fields: IndependentCorrectionFields
    source: VerifiedSource
    source_json: str
    source_hash: str
    original_hash: str
    evidence_refs: tuple[str, ...]
    receipt_id: str | None = None
    fact_set_id: str | None = None
    fact_set_version: int | None = None
    fact_input_hash: str | None = None
    fact_result_hash: str | None = None
    snapshot_id: str | None = None
    snapshot_hash: str | None = None
    payer_id: str | None = None
    aggregate_id: str | None = None


def _row(row: sqlite3.Row) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in row.keys():
        value = row[key]
        if type(value) is float:
            value = {"sqlite_real_hex": value.hex()}
        elif type(value) is bytes:
            value = {"sqlite_blob_hex": value.hex()}
        result[key] = value
    return result


def verify_original(service: PostingService, target_id: str) -> IndependentOriginalSource:
    conn = service._conn
    if not conn.in_transaction:
        raise IndependentCorrectionIntegrityError(
            "Original verification requires an SQLite snapshot"
        )
    # The owning financial effect can precede its attempt's coordination update.
    attempts = conn.execute(
        "SELECT DISTINCT a.attempt_id FROM application_posting_attempts a "
        "JOIN application_posting_reviews r ON r.review_id=a.review_id "
        "LEFT JOIN parser_proposal_conversion_audit c ON c.parser_output_id=r.parser_output_id "
        "LEFT JOIN transactions t ON t.id=c.transaction_id "
        "LEFT JOIN application_conditional_authorization_proofs p ON p.attempt_id=a.attempt_id "
        "LEFT JOIN receipt_finalization_audit f ON f.authorization_id=p.authorization_id "
        "WHERE a.transaction_public_id=? OR t.public_id=? OR "
        "(f.transaction_public_id=? AND f.status IN ('finalized','already_finalized'))",
        (target_id, target_id, target_id),
    ).fetchall()
    if len(attempts) != 1:
        raise IndependentCorrectionIntegrityError("Target is not a unique independent posting")
    attempt = attempts[0][0]
    status = service._get_original_status(attempt)
    if status.state != "finalized" or status.transaction_public_id != target_id:
        raise IndependentCorrectionIntegrityError(
            "Original posting financial effect is unavailable"
        )
    accepted, review, decision = service._accepted(attempt)
    source = service._source(accepted["intake_public_id"])
    transaction = conn.execute(
        "SELECT * FROM transactions WHERE public_id=?", (target_id,)
    ).fetchone()
    if transaction is None:
        raise IndependentCorrectionIntegrityError("Correction requires a personal expense")
    route = "receipt" if review["posting_path"] == "personal_receipt" else "text"
    receipt_binding: dict[str, Any] = {}
    original_rows: dict[str, object] = {"transaction": _row(transaction)}
    if route == "receipt":
        row = conn.execute(
            "SELECT p.*,f.version,f.fact_set_input_hash,f.fact_set_result_hash,"
            "r.public_id AS receipt_public_id,r.id AS receipt_internal_id,"
            "payer.public_id AS payer_public_id,a.receipt_group_public_id "
            "FROM application_conditional_authorization_proofs p "
            "JOIN receipt_finalization_authorizations a ON a.authorization_id=p.authorization_id "
            "JOIN receipt_item_allocation_fact_sets f ON f.fact_set_public_id=p.fact_set_public_id "
            "JOIN receipts r ON r.id=f.receipt_id "
            "JOIN participants payer ON payer.id=r.payer_participant_id WHERE p.attempt_id=?",
            (attempt,),
        ).fetchone()
        if row is None:
            raise IndependentCorrectionIntegrityError("Original personal receipt proof is absent")
        snapshot = AuthoritativeSnapshotRepository(conn).fetch(row["calculation_snapshot_id"])
        if snapshot is None:
            raise IndependentCorrectionIntegrityError("Original receipt snapshot is absent")
        snapshot.verify()
        metadata = read_receipt_bookkeeping_metadata(conn, row["receipt_public_id"])
        original_rows["receipt"] = _row(
            conn.execute(
                "SELECT * FROM receipts WHERE id=?", (row["receipt_internal_id"],)
            ).fetchone()
        )
        original_rows["fact_set"] = _row(
            conn.execute(
                "SELECT * FROM receipt_item_allocation_fact_sets WHERE fact_set_public_id=?",
                (row["fact_set_public_id"],),
            ).fetchone()
        )
        original_rows["snapshot"] = vars(snapshot)
        original_rows["proof"] = _row(row)
        receipt_binding = dict(
            receipt_id=row["receipt_public_id"],
            fact_set_id=row["fact_set_public_id"],
            fact_set_version=row["version"],
            fact_input_hash=row["fact_set_input_hash"],
            fact_result_hash=row["fact_set_result_hash"],
            snapshot_id=snapshot.snapshot_public_id,
            snapshot_hash=snapshot.combined_snapshot_hash,
            payer_id=row["payer_public_id"],
            aggregate_id=row["receipt_group_public_id"],
        )
        financial = review["financial_projection"]
        fields = IndependentCorrectionFields(
            financial["amount"],
            financial["currency"],
            financial["transaction_date"],
            financial["merchant"],
            metadata.description if metadata else None,
            metadata.category if metadata else None,
        )
    else:
        financial = review["financial_projection"]
        fields = IndependentCorrectionFields(
            financial["amount"],
            financial["currency"],
            financial["transaction_date"],
            financial["merchant"],
            financial["description"],
            financial["category"],
        )
    material = {
        "schema": "finance-application-correction-original-v1",
        "target_id": target_id,
        "attempt_id": attempt,
        "route": route,
        "binding": vars(service._binding),
        "source": vars(source),
        "posting_review": review,
        "posting_decision": vars(decision),
        "original_rows": original_rows,
        "fields": fields.as_dict(),
        "receipt_binding": receipt_binding,
    }
    source_hash = correction_hash(material)
    original_hash = correction_hash(
        {
            "schema": "finance-application-correction-anchor-v1",
            "source_hash": source_hash,
            "fields": fields.as_dict(),
        }
    )
    refs = tuple(
        sorted(
            {
                f"transaction:{target_id}",
                f"application-posting:{attempt}",
                f"source:{source.evidence_id}:{source.evidence_digest}",
            }
        )
    )
    return IndependentOriginalSource(
        target_id,
        attempt,
        route,
        service._binding.human_principal_id,
        fields,
        source,
        canonical_json_text(material),
        source_hash,
        original_hash,
        refs,
        **receipt_binding,
    )
