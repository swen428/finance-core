"""Historical D2 source verifier for one finalized original transaction.

This adapter deliberately joins the caller's SQLite snapshot. It never opens
another connection and never calls the correction-aware ordinary status API.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from typing import Any

from finance_core.application.corrections import CorrectionFields, VerifiedOriginalSource
from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeSnapshotRepository,
    canonical_json_bytes,
    canonical_json_text,
    canonical_json_value,
)
from finance_core.financial_audit.chain import (
    FinancialAuditRepository,
    _normalize_timestamp,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.money import canonical_money_str, money_decimal, normalize_currency
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    verify_human_revision_descendant,
)
from finance_core.parser_proposals.service import verify_converted_parser_proposal
from finance_core.posting_authority import (
    _authorized_attempt_for_resume,
    _require_d2_schema,
)
from finance_core.receipt_finalization.d2_conditional import require_d2_conditional_authority
from finance_core.receipt_finalization.fact_set_bridge import verify_finalized_prepared_receipt
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    require_telegram_source_context,
)


class D2SourceError(ValueError):
    """The original D2 chain or immutable transaction is not trustworthy."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _plain(value: object) -> object:
    """Represent every stored SQLite scalar without losing its exact type."""
    if value is None or type(value) in (str, int):
        return value
    if type(value) is bytes:
        return {"sqlite_blob_sha256": _sha(value), "length": len(value)}
    if type(value) is float:
        return {"sqlite_real_hex": value.hex()}
    raise D2SourceError("original row contains unsupported SQLite value")


def _row_material(row: sqlite3.Row) -> dict[str, object]:
    return {name: _plain(row[name]) for name in row.keys()}


def _audit(conn: sqlite3.Connection, kind: str, identity: str, expected_event: str) -> None:
    result = verify_financial_audit_chain(conn, aggregate_type=kind, aggregate_public_id=identity)
    if not result.valid or result.legacy_without_chain:
        raise D2SourceError("original financial audit chain is missing or corrupt")
    event = conn.execute(
        "SELECT 1 FROM financial_audit_events WHERE aggregate_type=? "
        "AND aggregate_public_id=? AND event_type=? LIMIT 1",
        (kind, identity, expected_event),
    ).fetchone()
    if event is None:
        raise D2SourceError("expected original financial audit event is missing")


def _snapshot_audit(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    snapshot_hash: str,
    aggregate_id: str,
    authorization_id: str,
) -> None:
    """Verify the original snapshot's complete finalization event and chain."""
    snapshot = AuthoritativeSnapshotRepository(conn).fetch(snapshot_id)
    if snapshot is None:
        raise D2SourceError("original receipt snapshot is missing")
    snapshot.verify()
    if (
        snapshot.combined_snapshot_hash != snapshot_hash
        or snapshot.calculation_type != "receipt_split"
        or snapshot.aggregate_public_id != aggregate_id
        or snapshot.authorization_reference != authorization_id
        or snapshot.finalization_status != "finalized"
    ):
        raise D2SourceError("original receipt snapshot binding changed")
    audit = verify_financial_audit_chain(
        conn, aggregate_type="calculation_snapshot", aggregate_public_id=snapshot_id
    )
    if not audit.valid or audit.event_count != 1:
        raise D2SourceError("original receipt snapshot audit chain is missing or invalid")
    event_id = derive_audit_event_public_id(
        aggregate_type="calculation_snapshot",
        aggregate_public_id=snapshot_id,
        event_type="calculation_snapshot_finalized",
        causation_public_id=snapshot_id,
    )
    event = FinancialAuditRepository(conn).fetch(event_id)
    expected_payload = {
        "calculation_type": snapshot.calculation_type,
        "calculation_aggregate_public_id": snapshot.aggregate_public_id,
        "input_hash": snapshot.input_hash,
        "output_hash": snapshot.output_hash,
        "rules_hash": snapshot.rules_hash,
        "combined_snapshot_hash": snapshot.combined_snapshot_hash,
        "algorithm_version": snapshot.algorithm_version,
        "money_contract_version": snapshot.money_contract_version,
        "currency_contract_version": snapshot.currency_contract_version,
    }
    if event is None:
        raise D2SourceError("original receipt snapshot audit event is missing")
    expected = {
        "event_type": "calculation_snapshot_finalized",
        "event_payload_json": canonical_json_text(expected_payload),
        "new_state_json": canonical_json_text(
            {
                "finalization_status": "finalized",
                "combined_snapshot_hash": snapshot.combined_snapshot_hash,
            }
        ),
        "authorization_public_id": authorization_id,
        "calculation_snapshot_public_id": snapshot_id,
        "calculation_snapshot_hash": snapshot_hash,
        "source_evidence_references": snapshot.source_references,
        "actor_type": snapshot.actor_type,
        "actor_public_id": snapshot.actor_public_id
        or f"calculation-snapshot:{snapshot.actor_type}",
        "correlation_public_id": aggregate_id,
        "causation_public_id": snapshot_id,
        "created_at": _normalize_timestamp(snapshot.created_at),
    }
    mismatched = [field for field, value in expected.items() if getattr(event, field) != value]
    if mismatched:
        raise D2SourceError(
            f"original receipt snapshot audit event binding changed: {', '.join(mismatched)}"
        )


def _historical_d1_card(
    conn: sqlite3.Connection,
    *,
    card: sqlite3.Row,
    review: sqlite3.Row,
    context: HumanActionContext,
) -> None:
    """Reverify the old publication/reply/AI root without current-card freshness."""
    if any(
        (
            card["decision_target_parser_output_id"] != review["parser_output_id"],
            card["decision_target_proposal_version"] != review["proposal_version"],
            card["decision_target_proposal_content_hash"] != review["proposal_content_hash"],
            card["authenticated_actor_id"] != context.actor_id,
            card["telegram_account_id"] != context.account_id,
            card["telegram_conversation_id"] != context.conversation_id,
            card["conversation_binding_id"] != context.binding_id,
        )
    ):
        raise D2SourceError("original D1 card is not bound to accepted D2 review")
    publications = conn.execute(
        """SELECT publication_public_id FROM parser_human_draft_publications
        WHERE draft_id=? AND draft_version=? AND parser_output_id=?
          AND draft_content_hash=? AND proposal_version=? AND proposal_content_hash=?""",
        (
            card["draft_id"],
            card["draft_version"],
            card["decision_target_parser_output_id"],
            card["draft_content_hash"],
            card["decision_target_proposal_version"],
            card["decision_target_proposal_content_hash"],
        ),
    ).fetchall()
    if len(publications) != 1:
        raise D2SourceError("original D1 publication edge is missing or ambiguous")
    proposal = conn.execute(
        "SELECT * FROM parser_outputs WHERE id=?", (review["parser_output_id"],)
    ).fetchone()
    if proposal is None or proposal["public_id"] != review["proposal_public_id"]:
        raise D2SourceError("original D1 proposal is unavailable")
    content_hash = compute_effective_proposal_content_hash(conn, dict(proposal))
    _payload, _completion_id, version = resolve_effective_payload(conn, dict(proposal))
    if content_hash != review["proposal_content_hash"] or version != review["proposal_version"]:
        raise D2SourceError("original D1 proposal differs from accepted D2 review")
    try:
        lineage = verify_human_revision_descendant(
            conn, dict(proposal), content_hash=content_hash, proposal_version=version
        )
    except HumanRevisionLineageError as exc:
        raise D2SourceError("original D1 human revision lineage is invalid") from exc
    if lineage is None:
        raise D2SourceError("original D1 human revision lineage is missing")


def _attempt_events(conn: sqlite3.Connection, attempt: sqlite3.Row) -> None:
    rows = conn.execute(
        "SELECT from_stage,to_stage,row_version,transaction_public_id FROM "
        "d2_posting_attempt_events WHERE attempt_public_id=? ORDER BY row_version",
        (attempt["attempt_public_id"],),
    ).fetchall()
    if not rows or len(rows) != int(attempt["row_version"]) + 1:
        raise D2SourceError("D2 stage history has a gap")
    prior: str | None = None
    for index, row in enumerate(rows):
        if row["row_version"] != index or row["from_stage"] != prior:
            raise D2SourceError("D2 stage history is not contiguous")
        prior = str(row["to_stage"])
    if (
        prior != "finalized"
        or rows[-1]["transaction_public_id"] != attempt["transaction_public_id"]
    ):
        raise D2SourceError("D2 stage history does not finalize this transaction")


def _original_fields(transaction: sqlite3.Row) -> CorrectionFields:
    currency = normalize_currency(str(transaction["currency"] or ""))
    amount = canonical_money_str(
        money_decimal(str(transaction["amount"]), label="original transaction amount"),
        currency,
    )
    if transaction["total_amount"] is not None and money_decimal(
        str(transaction["total_amount"]), label="original transaction total"
    ) != money_decimal(amount):
        raise D2SourceError("original transaction monetary mirrors disagree")
    raw_date = str(transaction["transaction_date"] or "")
    day = raw_date[:10]
    merchant = transaction["merchant"]
    if type(merchant) not in (str, type(None)):
        raise D2SourceError("original merchant is malformed")
    return CorrectionFields(amount, currency, day, merchant)


class D2OriginalSourceVerifier:
    """Replay historical source, delivery, decision and finalization evidence."""

    def verify_original(
        self, connection: sqlite3.Connection, target_id: str
    ) -> VerifiedOriginalSource:
        require_staging_database(connection)
        require_foreign_keys_enabled(connection)
        _require_d2_schema(connection)
        if type(target_id) is not str or not target_id:
            raise D2SourceError("target ID is invalid")
        transaction = connection.execute(
            "SELECT * FROM transactions WHERE public_id=?", (target_id,)
        ).fetchone()
        if transaction is None:
            raise D2SourceError("original transaction is missing")
        rows = connection.execute(
            """SELECT attempts.*, reviews.parser_output_id, reviews.posting_path,
                      reviews.source_kind, reviews.initial_card_public_id,
                      reviews.card_generation_public_id, reviews.proposal_content_hash,
                      reviews.proposal_version,
                      reviews.visible_projection_json, reviews.visible_projection_hash,
                      reviews.authenticated_actor_id, reviews.telegram_account_id,
                      reviews.telegram_conversation_id, reviews.conversation_binding_id,
                      proposals.public_id AS proposal_public_id,
                      proposals.raw_text AS proposal_raw_text,
                      decisions.decision_public_id, decisions.confirmation_public_id
               FROM d2_posting_attempts AS attempts
               JOIN d2_posting_reviews AS reviews
                 ON reviews.review_public_id=attempts.review_public_id
               JOIN d2_posting_decisions AS decisions
                 ON decisions.attempt_public_id=attempts.attempt_public_id
                AND decisions.review_public_id=reviews.review_public_id
               JOIN parser_outputs AS proposals ON proposals.id=reviews.parser_output_id
               WHERE attempts.transaction_public_id=?""",
            (target_id,),
        ).fetchall()
        if len(rows) != 1:
            raise D2SourceError("target is not one finalized D2 economic event")
        row = rows[0]
        if row["stage"] != "finalized":
            raise D2SourceError("D2 coordination is not finalized")
        _attempt_events(connection, row)
        context = HumanActionContext(
            actor_id=str(row["authenticated_actor_id"]),
            account_id=str(row["telegram_account_id"]),
            conversation_id=str(row["telegram_conversation_id"]),
            binding_id=str(row["conversation_binding_id"]),
        )
        _authorized_attempt_for_resume(
            connection, attempt_public_id=str(row["attempt_public_id"]), context=context
        )
        review_json = str(row["visible_projection_json"])
        if not hmac.compare_digest(
            _sha(review_json.encode("utf-8")), str(row["visible_projection_hash"])
        ):
            raise D2SourceError("D2 visible projection hash changed")
        projection = canonical_json_value(review_json, label="original D2 review")
        if type(projection) is not dict:
            raise D2SourceError("D2 review projection is malformed")
        fields = _original_fields(transaction)
        if any(projection.get(name) != value for name, value in fields.as_dict().items()):
            raise D2SourceError("original transaction differs from approved D2 review")
        if projection.get("account") != "unspecified" or transaction["account_id"] is not None:
            raise D2SourceError("original transaction has unsupported account authority")
        proposal_id = str(row["proposal_public_id"])
        _audit(connection, "parser_proposal", proposal_id, "parser_proposal_confirmed")

        # The first-card source binds capture-time transport identity, while a
        # D1 human card carries its own prior revision chain. Both are frozen.
        source_card: dict[str, object]
        raw_record: sqlite3.Row | None
        if row["source_kind"] == "initial_proposal_card":
            card = connection.execute(
                "SELECT cards.*, intake.* FROM d2_initial_proposal_cards AS cards "
                "JOIN raw_intake_records AS intake ON intake.id=cards.raw_intake_record_id "
                "WHERE cards.initial_card_public_id=?",
                (row["initial_card_public_id"],),
            ).fetchone()
            if card is None or card["parser_output_id"] != row["parser_output_id"]:
                raise D2SourceError("original D2 initial card is unavailable")
            capture = TelegramSourceContext(
                authenticated_actor_id=context.actor_id,
                account_id=context.account_id,
                conversation_id=context.conversation_id,
                binding_id=context.binding_id,
                message_id=str(card["admitted_source_message_id"]),
            )
            digest = require_telegram_source_context(
                connection,
                raw_intake_record_id=int(card["raw_intake_record_id"]),
                context=capture,
            )
            if digest != card["admitted_source_identity_sha256"]:
                raise D2SourceError("original source identity changed")
            if (
                card["visible_projection_json"] != review_json
                or card["visible_projection_hash"] != row["visible_projection_hash"]
            ):
                raise D2SourceError("initial card and delivered review disagree")
            raw_record = connection.execute(
                "SELECT * FROM raw_intake_records WHERE id=?", (card["raw_intake_record_id"],)
            ).fetchone()
            source_card = {
                "kind": "initial",
                "id": str(row["initial_card_public_id"]),
                "capture_digest": digest,
            }
        elif row["source_kind"] == "d1_human_card":
            card = connection.execute(
                "SELECT * FROM parser_human_draft_cards WHERE card_generation_public_id=?",
                (row["card_generation_public_id"],),
            ).fetchone()
            if card is None:
                raise D2SourceError("original D1 card is unavailable")
            _historical_d1_card(connection, card=card, review=row, context=context)
            raw_record = connection.execute(
                "SELECT * FROM raw_intake_records WHERE parser_output_id=?",
                (row["parser_output_id"],),
            ).fetchone()
            source_card = {
                "kind": "d1",
                "id": str(row["card_generation_public_id"]),
                "row_hash": _sha(canonical_json_bytes(_row_material(card))),
            }
        else:
            raise D2SourceError("D2 review source kind is unsupported")
        if raw_record is None or raw_record["source_channel"] != "telegram":
            raise D2SourceError("original raw Telegram evidence is unavailable")
        raw_bytes = str(raw_record["raw_input"]).encode("utf-8")
        if row["posting_path"] == "text" and raw_record["raw_input"] != row["proposal_raw_text"]:
            raise D2SourceError("raw text and original proposal disagree")

        d2_route = str(row["posting_path"])
        route = "receipt" if d2_route == "personal_receipt" else d2_route
        receipt_binding: dict[str, object] | None = None
        receipt_kwargs: dict[str, Any] = {}
        if route == "text":
            result = verify_converted_parser_proposal(connection, int(row["parser_output_id"]))
            if (
                result["transaction_public_id"] != target_id
                or result["confirmation_id"] != row["confirmation_public_id"]
            ):
                raise D2SourceError("text conversion does not match D2 decision")
            _audit(connection, "parser_proposal", proposal_id, "parser_proposal_converted")
        elif route == "receipt":
            receipt = connection.execute(
                """SELECT proofs.*, auth.receipt_group_public_id, auth.actor_id AS actor_id,
                          facts.version AS fact_version, facts.fact_set_input_hash,
                          facts.fact_set_result_hash, receipts.public_id AS receipt_public_id,
                          payer.public_id AS payer_public_id, snapshots.combined_snapshot_hash,
                          audits.transaction_public_id AS audit_transaction,
                          audits.status AS audit_status
                   FROM d2_conditional_authorization_proofs AS proofs
                   JOIN receipt_finalization_authorizations AS auth
                     ON auth.authorization_id=proofs.authorization_id
                   JOIN receipt_finalization_audit AS audits
                     ON audits.authorization_id=auth.authorization_id
                   JOIN receipt_item_allocation_fact_sets AS facts
                     ON facts.fact_set_public_id=proofs.fact_set_public_id
                   JOIN receipts ON receipts.id=facts.receipt_id
                   JOIN participants AS payer ON payer.id=receipts.payer_participant_id
                   JOIN authoritative_calculation_snapshots AS snapshots
                     ON snapshots.snapshot_public_id=proofs.calculation_snapshot_id
                   WHERE proofs.decision_public_id=?""",
                (row["decision_public_id"],),
            ).fetchone()
            if (
                receipt is None
                or receipt["audit_transaction"] != target_id
                or receipt["audit_status"] != "finalized"
            ):
                raise D2SourceError("receipt finalization is not bound to original target")
            require_d2_conditional_authority(connection, dict(receipt))
            verified = verify_finalized_prepared_receipt(
                connection, str(receipt["authorization_id"])
            )
            if verified.transaction_public_id != target_id:
                raise D2SourceError("verified receipt finalization changed")
            group_id = str(receipt["receipt_group_public_id"])
            _audit(connection, "receipt_group", group_id, "receipt_finalized")
            _snapshot_audit(
                connection,
                snapshot_id=str(receipt["calculation_snapshot_id"]),
                snapshot_hash=str(receipt["combined_snapshot_hash"]),
                aggregate_id=group_id,
                authorization_id=str(receipt["authorization_id"]),
            )
            receipt_binding = {
                "receipt_id": str(receipt["receipt_public_id"]),
                "fact_set_id": str(receipt["fact_set_public_id"]),
                "fact_set_version": int(receipt["fact_version"]),
                "fact_input_hash": str(receipt["fact_set_input_hash"]),
                "fact_result_hash": str(receipt["fact_set_result_hash"]),
                "snapshot_id": str(receipt["calculation_snapshot_id"]),
                "snapshot_hash": str(receipt["combined_snapshot_hash"]),
                "payer_id": str(receipt["payer_public_id"]),
                "aggregate_id": group_id,
            }
            receipt_kwargs = receipt_binding.copy()
        else:
            raise D2SourceError("D2 posting route is unsupported")

        transaction_material = _row_material(transaction)
        original_hash = _sha(canonical_json_bytes(transaction_material))
        projection_hash = _sha(canonical_json_bytes(fields.as_dict()))
        raw_material = _row_material(raw_record)
        source_material = {
            "schema": "correction-original-d2-source-v1",
            "target_id": target_id,
            "route": route,
            "d2_route": d2_route,
            "actor": context.actor_id,
            "attempt_id": str(row["attempt_public_id"]),
            "review_id": str(row["review_public_id"]),
            "decision_id": str(row["decision_public_id"]),
            "confirmation_id": str(row["confirmation_public_id"]),
            "proposal_id": proposal_id,
            "proposal_content_hash": str(row["proposal_content_hash"]),
            "review_projection_hash": str(row["visible_projection_hash"]),
            "source_card": source_card,
            "raw_intake_id": str(raw_record["public_id"]),
            "raw_input_sha256": _sha(raw_bytes),
            "proposal_raw_text_sha256": _sha(str(row["proposal_raw_text"]).encode("utf-8")),
            "raw_record_hash": _sha(canonical_json_bytes(raw_material)),
            "attachment_path": raw_record["attachment_path"],
            "attachment_hash": raw_record["attachment_hash"],
            "transaction_hash": original_hash,
            "projection_hash": projection_hash,
            "receipt": receipt_binding,
        }
        source_json = canonical_json_text(source_material)
        refs = tuple(
            str(v)
            for v in (
                raw_record["public_id"],
                proposal_id,
                source_card["id"],
                row["review_public_id"],
                row["decision_public_id"],
                target_id,
            )
        )
        return VerifiedOriginalSource(
            target_id=target_id,
            route=route,
            actor=context.actor_id,
            fields=fields,
            source_hash=_sha(source_json.encode("utf-8")),
            original_hash=original_hash,
            original_projection_hash=projection_hash,
            source_json=source_json,
            evidence_refs=refs,
            **receipt_kwargs,
        )
