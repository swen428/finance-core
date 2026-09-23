"""D2 one-confirmation posting authority and crash-safe recovery.

The review API persists only an immutable, non-postable projection.  The
first valid Telegram Confirm redemption atomically records the existing D1
proposal confirmation plus a D2 decision and posting attempt.  Financial
writes remain owned by their existing guarded services; this module binds and
resumes them without manufacturing a second human authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.money import canonical_money_str, money_decimal, normalize_currency
from finance_core.openclaw_staging_bridge.human_actions import (
    HumanActionContext,
    IssuedHumanActionReference,
    issue_human_action_references,
    redeem_human_action_reference,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_drafts import HumanDraftDecisionBinding
from finance_core.parser_proposals.receipt_facts_conversion import (
    ReceiptFactsConversionCommand,
    ReceiptFactsConversionError,
    convert_confirmed_receipt_proposal_to_facts,
    resolve_receipt_conversion_payload_fields,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
    persist_receipt_item_allocation_facts,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.parser_proposals.service import (
    InitialProposalDecisionBinding,
    ParserConfirmationError,
    confirm_parser_proposal,
    convert_confirmed_parser_proposal,
    resolve_simple_expense_conversion_fields,
    verify_converted_parser_proposal,
)
from finance_core.receipt_finalization.d2_conditional import (
    build_d2_receipt_projection,
    require_d2_conditional_authority,
)
from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeAuthorizationConflictError,
    authorize_d2_conditional_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
    verify_finalized_prepared_receipt,
)
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    require_telegram_source_context,
)

POSTING_REVIEW_SCHEMA_VERSION = "d2-posting-review-v2"
DELIVERY_MANIFEST_VERSION = "finance_d2_controls_v1"
DELIVERY_MATERIAL_VERSION = "finance_d2_delivery_material_v1"
DELIVERY_MATERIAL_CAPABILITY = "telegram.finance-delivery-material-v1"
CALLBACK_VALUE_VERSION = "finance_d2_callback_value_v1"
_failure_injection_hook: Callable[[str], None] | None = None
_D2_TABLES = frozenset(
    {
        "d2_telegram_source_contexts",
        "d2_initial_proposal_cards",
        "d2_posting_reviews",
        "d2_posting_review_action_bindings",
        "d2_posting_review_controls",
        "d2_posting_review_delivery_attempts",
        "d2_posting_review_delivery_observations",
        "d2_posting_review_delivery_activations",
        "d2_posting_review_delivery_conflicts",
        "d2_posting_review_supersessions",
        "d2_posting_attempts",
        "d2_posting_decisions",
        "d2_posting_receipt_evidence",
        "d2_conditional_authorization_proofs",
        "d2_posting_attempt_events",
    }
)
_D2_SCHEMA_FINGERPRINT = "20e4ed72cf4749003f54bca99bd94d181d4f8f3fcb8f180fe9e4da8de5fb9ff2"


class PostingAuthorityError(RuntimeError):
    """Fail-closed D2 posting authority refusal."""


def _inject_failure(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


def _require_d2_schema(conn: sqlite3.Connection) -> None:
    observed = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'd2_%'"
        ).fetchall()
    }
    if observed != _D2_TABLES:
        raise PostingAuthorityError("migration 050 is missing or incomplete")
    schema_rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE (name GLOB 'd2_*' OR name GLOB 'trg_d2_*' "
        "OR name = 'openclaw_human_action_reference_purposes' "
        "OR name GLOB 'trg_human_action_purposes_*') "
        "AND type IN ('table', 'trigger', 'index') ORDER BY type, name"
    ).fetchall()
    material = json.dumps(
        [tuple(row) for row in schema_rows],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if not hmac.compare_digest(hashlib.sha256(material).hexdigest(), _D2_SCHEMA_FINGERPRINT):
        raise PostingAuthorityError("migration 050 is missing or incomplete")


@dataclass(frozen=True)
class PreparedPostingReview:
    review_public_id: str
    source_kind: str
    card_generation_public_id: str | None
    initial_card_public_id: str | None
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    posting_path: str
    visible_projection: Mapping[str, object]
    visible_projection_hash: str
    presentation_text: str
    expires_at: int
    idempotent: bool


@dataclass(frozen=True)
class PostingReviewControl:
    action: str
    label: str
    row_index: int
    column_index: int
    callback_value: str


@dataclass(frozen=True)
class PostingReviewDeliveryManifest:
    review_public_id: str
    delivery_attempt_public_id: str
    version: str
    text: str
    controls: tuple[PostingReviewControl, ...]
    finance_delivery_material_sha256: str
    delivery_attempt_nonce: str
    idempotent: bool


@dataclass(frozen=True)
class PostingStatus:
    review_public_id: str
    state: str
    attempt_public_id: str | None
    transaction_public_id: str | None
    attention_reason: str | None
    amount: str | None = None
    currency: str | None = None
    transaction_date: str | None = None
    merchant: str | None = None
    account: str | None = None


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def _now_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_lower_sha256(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PostingAuthorityError(f"{label} is malformed")
    return value


def _telegram_message_id(value: int | str) -> int:
    if isinstance(value, bool):
        raise PostingAuthorityError("provider message identity is invalid")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdigit():
        if value != str(int(value)):
            raise PostingAuthorityError("provider message identity is invalid")
        result = int(value)
    else:
        raise PostingAuthorityError("provider message identity is invalid")
    if result <= 0 or result > 9_223_372_036_854_775_807:
        raise PostingAuthorityError("provider message identity is invalid")
    return result


def _canonical(value: object) -> str:
    return canonical_json_text(value)


def _identity(prefix: str, *parts: object) -> str:
    framed = "".join(f"{len(str(part))}:{part}" for part in parts)
    return f"{prefix}_{_sha256_text(framed)[:30]}"


def _long_identity(prefix: str, *parts: object) -> str:
    framed = "".join(f"{len(str(part))}:{part}" for part in parts)
    return f"{prefix}_{_sha256_text(framed)[:32]}"


def _framed_field(name: str, value: bytes) -> bytes:
    encoded_name = name.encode("ascii")
    return (
        len(encoded_name).to_bytes(2, "big") + encoded_name + len(value).to_bytes(4, "big") + value
    )


def _callback_value_digest(value: str) -> bytes:
    material = _framed_field("version", CALLBACK_VALUE_VERSION.encode("utf-8"))
    material += _framed_field("value", value.encode("utf-8"))
    return hashlib.sha256(material).digest()


def finance_delivery_material_digest(text: str, controls: tuple[PostingReviewControl, ...]) -> str:
    """Hash exact text and ordered Telegram callback controls."""
    rows: dict[int, list[PostingReviewControl]] = {}
    for control in controls:
        rows.setdefault(control.row_index, []).append(control)
    if sorted(rows) != list(range(len(rows))):
        raise PostingAuthorityError("D2 control rows are not contiguous")
    material = _framed_field("version", DELIVERY_MATERIAL_VERSION.encode("utf-8"))
    material += _framed_field("text", text.encode("utf-8"))
    material += _framed_field("row_count", len(rows).to_bytes(4, "big"))
    for row_index in sorted(rows):
        ordered = sorted(rows[row_index], key=lambda item: item.column_index)
        if [item.column_index for item in ordered] != list(range(len(ordered))):
            raise PostingAuthorityError("D2 control columns are not contiguous")
        material += _framed_field("row_index", row_index.to_bytes(4, "big"))
        material += _framed_field("button_count", len(ordered).to_bytes(4, "big"))
        for control in ordered:
            material += _framed_field("column_index", control.column_index.to_bytes(4, "big"))
            material += _framed_field("label", control.label.encode("utf-8"))
            material += _framed_field("kind", b"callback_data")
            material += _framed_field(
                "callback_value_sha256", _callback_value_digest(control.callback_value)
            )
    return hashlib.sha256(material).hexdigest()


def _require_context(context: HumanActionContext) -> None:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
        )
    ):
        raise PostingAuthorityError("authenticated Telegram context is incomplete")
    if context.actor_id != context.conversation_id:
        raise PostingAuthorityError("D2 supports only authenticated private direct conversations")


def _require_active_self_participant(conn: sqlite3.Connection, public_id: str) -> None:
    rows = conn.execute(
        "SELECT public_id FROM participants WHERE is_self = 1 AND is_active = 1 ORDER BY public_id"
    ).fetchall()
    if len(rows) != 1 or str(rows[0]["public_id"]) != public_id:
        raise PostingAuthorityError("personal receipt payer must be the unique active self")


def _review_row(conn: sqlite3.Connection, review_public_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT reviews.*, proposals.public_id AS proposal_public_id "
        "FROM d2_posting_reviews AS reviews "
        "JOIN parser_outputs AS proposals ON proposals.id = reviews.parser_output_id "
        "WHERE reviews.review_public_id = ?",
        (review_public_id,),
    ).fetchone()
    if row is None:
        raise PostingAuthorityError("posting review not found")
    return row


def _projection_for_review(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    fields: Mapping[str, object],
    receipt_payer_participant_public_id: str | None,
) -> tuple[str, dict[str, object], dict[str, object] | None]:
    required = ("amount", "currency", "transaction_date")
    if any(
        not isinstance(fields.get(name), str) or not str(fields[name]).strip() for name in required
    ):
        raise PostingAuthorityError("proposal is incomplete for D2 posting")
    merchant = fields.get("merchant")
    description = fields.get("description")
    if not isinstance(merchant, str) and not isinstance(description, str):
        raise PostingAuthorityError("proposal is incomplete for D2 posting")
    currency = normalize_currency(str(fields["currency"]))
    amount = canonical_money_str(
        money_decimal(str(fields["amount"]), label="D2 reviewed amount"), currency
    )
    base_projection: dict[str, object] = {
        "amount": amount,
        "currency": currency,
        "transaction_date": str(fields["transaction_date"]),
        "merchant": merchant,
        "account": "unspecified",
    }
    is_receipt = (
        conn.execute(
            "SELECT 1 FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
            (parser_output_id,),
        ).fetchone()
        is not None
    )
    if not is_receipt:
        return "text", base_projection, None
    if not isinstance(merchant, str) or not merchant.strip():
        raise PostingAuthorityError("personal receipt proposal requires merchant")
    if receipt_payer_participant_public_id is None:
        participants = conn.execute(
            "SELECT public_id FROM participants "
            "WHERE is_self = 1 AND is_active = 1 ORDER BY public_id"
        ).fetchall()
        if len(participants) != 1:
            raise PostingAuthorityError(
                "personal receipt requires exactly one active self participant"
            )
        payer = str(participants[0]["public_id"])
    else:
        payer = receipt_payer_participant_public_id.strip()
        if not payer:
            raise PostingAuthorityError("personal receipt payer participant must not be empty")
    _require_active_self_participant(conn, payer)
    candidate: dict[str, object] = {
        "payer_participant_public_id": payer,
        "item": {
            "line_number": 1,
            "item_name": "Receipt total",
            "line_amount": amount,
            "currency": currency,
        },
    }
    preview = calculate_receipt_split(
        {
            "currency": currency,
            "participants": [payer],
            "payer": payer,
            "receipts": [
                {
                    "merchant": fields.get("merchant") or "Receipt total",
                    "paid_by": payer,
                    "net_paid": amount,
                    "items": [
                        {
                            "description": "Receipt total",
                            "amount": amount,
                            "owners": [payer],
                        }
                    ],
                }
            ],
        }
    )
    projection = {
        **base_projection,
        "receipt_total": amount,
        "personal_share": amount,
        "calculation": {
            "total_paid": canonical_money_str(preview["total_paid"], currency),
            "total_to_collect": canonical_money_str(preview["total_to_collect"], currency),
            "settlement_obligations": [],
        },
    }
    return "personal_receipt", projection, candidate


def _presentation_text(
    card_ref: str,
    projection: Mapping[str, object],
    posting_path: str,
    *,
    display_fields: Mapping[str, object] | None = None,
) -> str:
    fields = projection if display_fields is None else display_fields
    lines = [
        f"Card Ref: {card_ref}",
        f"Amount: {projection['amount']}",
        f"Currency: {projection['currency']}",
        f"Date: {projection['transaction_date']}",
        f"Merchant: {projection.get('merchant') or 'Not specified'}",
        f"Description: {fields.get('description') or 'Not specified'}",
        f"Category: {fields.get('category') or 'Not specified'}",
        "Account: Not specified",
    ]
    if posting_path == "personal_receipt":
        calculation = projection["calculation"]
        if not isinstance(calculation, Mapping):
            raise PostingAuthorityError("receipt projection is malformed")
        lines.extend(
            [
                "Source: Receipt",
                f"Receipt total: {projection['receipt_total']}",
                "Posting basis: one receipt-total line",
                f"Your share: {projection['personal_share']}",
                f"Collectible from others: {calculation['total_to_collect']}",
                "Settlement obligations: none",
                "No itemization, tax, fee, or shared allocation will be inferred.",
            ]
        )
    else:
        lines.append("No account or shared-expense details will be inferred.")
    return "\n".join(lines)


def prepare_posting_review(
    conn: sqlite3.Connection,
    *,
    review_idempotency_key: str,
    context: HumanActionContext,
    card_generation_public_id: str | None = None,
    proposal_public_id: str | None = None,
    admitted_source_message_id: str | None = None,
    receipt_payer_participant_public_id: str | None = None,
    clock: Callable[[], int] = _now_epoch,
) -> PreparedPostingReview:
    """Persist a non-authoritative D1-card or initial-proposal review projection."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    _require_context(context)
    if not review_idempotency_key.strip():
        raise PostingAuthorityError("review_idempotency_key must not be empty")
    if (card_generation_public_id is None) == (proposal_public_id is None):
        raise PostingAuthorityError("exactly one D2 review source is required")
    if conn.in_transaction:
        raise PostingAuthorityError("posting review requires a connection without pending work")

    conn.execute("BEGIN IMMEDIATE")
    try:
        now = clock()
        source_kind: str
        initial_card_public_id: str | None = None
        admitted_source_identity_sha256: str | None = None
        display_fields: Mapping[str, object] | None = None
        if card_generation_public_id is not None:
            source_kind = "d1_human_card"
            card = conn.execute(
                """
                SELECT cards.*, drafts.state AS draft_state,
                       drafts.expires_at AS draft_expires_at,
                       drafts.current_card_generation_public_id,
                       drafts.current_parser_output_id,
                       drafts.current_proposal_version,
                       drafts.current_proposal_content_hash,
                       operations.result_completeness,
                       proposals.public_id AS proposal_public_id,
                       proposals.parse_status
                FROM parser_human_draft_cards AS cards
                JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id
                JOIN parser_human_draft_operations AS operations
                  ON operations.id = cards.original_operation_id
                JOIN parser_outputs AS proposals
                  ON proposals.id = cards.decision_target_parser_output_id
                WHERE cards.card_generation_public_id = ?
                """,
                (card_generation_public_id,),
            ).fetchone()
            if card is None:
                raise PostingAuthorityError("card_generation_invalid")
            if (
                card["draft_state"] != "active"
                or card["current_card_generation_public_id"] != card_generation_public_id
                or card["result_completeness"] != "complete"
                or card["parse_status"] in {"confirmed", "rejected", "superseded"}
                or int(card["expires_at"]) <= now
                or int(card["draft_expires_at"]) <= now
                or card["parser_output_id"] != card["decision_target_parser_output_id"]
                or card["current_parser_output_id"] != card["decision_target_parser_output_id"]
                or card["current_proposal_version"] != card["decision_target_proposal_version"]
                or card["current_proposal_content_hash"]
                != card["decision_target_proposal_content_hash"]
                or card["authenticated_actor_id"] != context.actor_id
                or card["telegram_account_id"] != context.account_id
                or card["telegram_conversation_id"] != context.conversation_id
                or card["conversation_binding_id"] != context.binding_id
            ):
                raise PostingAuthorityError("card_generation_stale")
            parser_output_id = int(card["decision_target_parser_output_id"])
            proposal = ParserProposalRepository(conn).get(parser_output_id)
            if proposal is None:
                raise PostingAuthorityError("proposal_missing")
            effective_payload, _completion_id, version = resolve_effective_payload(conn, proposal)
            content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
            if version != int(card["decision_target_proposal_version"]) or not hmac.compare_digest(
                content_hash, str(card["decision_target_proposal_content_hash"])
            ):
                raise PostingAuthorityError("proposal_state_stale")
            display_fields = json.loads(str(card["field_values_json"]))
            resolved_proposal_public_id = str(card["proposal_public_id"])
            expires_at = min(int(card["expires_at"]), int(card["draft_expires_at"]))
            card_ref = card_generation_public_id
        else:
            source_kind = "initial_proposal_card"
            if not admitted_source_message_id or not admitted_source_message_id.strip():
                raise PostingAuthorityError(
                    "initial review requires admitted input message identity"
                )
            proposal = ParserProposalRepository(conn).get_by_public_id(str(proposal_public_id))
            if proposal is None:
                raise PostingAuthorityError("proposal_missing")
            parser_output_id = int(proposal["id"])
            if proposal["parse_status"] != "parsed_pending_confirmation":
                raise PostingAuthorityError("proposal_state_stale")
            effective_payload, _completion_id, version = resolve_effective_payload(conn, proposal)
            content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
            intake = conn.execute(
                "SELECT * FROM raw_intake_records WHERE parser_output_id = ?",
                (parser_output_id,),
            ).fetchone()
            expected_source_identity = (
                f"telegram:{context.conversation_id}:{admitted_source_message_id}"
            )
            if (
                intake is None
                or intake["source_channel"] != "telegram"
                or str(intake["source_message_id"] or "") != admitted_source_message_id
                or str(intake["external_source_id"] or "") != expected_source_identity
                or intake["status"] != "parsed_pending_confirmation"
            ):
                raise PostingAuthorityError("initial proposal source evidence is unavailable")
            try:
                admitted_source_identity_sha256 = require_telegram_source_context(
                    conn,
                    raw_intake_record_id=int(intake["id"]),
                    context=TelegramSourceContext(
                        authenticated_actor_id=context.actor_id,
                        account_id=context.account_id,
                        conversation_id=context.conversation_id,
                        binding_id=context.binding_id,
                        message_id=admitted_source_message_id,
                    ),
                )
            except TelegramSourceContextError as exc:
                raise PostingAuthorityError(
                    "initial proposal source context is unavailable"
                ) from exc
            if (
                conn.execute(
                    "SELECT 1 FROM parser_human_drafts WHERE decision_target_parser_output_id = ? "
                    "AND state = 'active' LIMIT 1",
                    (parser_output_id,),
                ).fetchone()
                is not None
            ):
                raise PostingAuthorityError("initial proposal was replaced by human edit authority")
            resolved_proposal_public_id = str(proposal["public_id"])
            initial_card_public_id = _long_identity(
                "d2card",
                resolved_proposal_public_id,
                version,
                content_hash,
                context.actor_id,
                context.account_id,
                context.conversation_id,
                context.binding_id,
                admitted_source_message_id,
            )
            expires_at = now + 3600
            card_ref = initial_card_public_id

        is_receipt = (
            conn.execute(
                "SELECT 1 FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
                (parser_output_id,),
            ).fetchone()
            is not None
        )
        try:
            if is_receipt:
                receipt_fields = resolve_receipt_conversion_payload_fields(conn, effective_payload)
                fields: Mapping[str, object] = {
                    "amount": receipt_fields["canonical_amount"],
                    "currency": receipt_fields["currency"],
                    "transaction_date": receipt_fields["receipt_date"],
                    "merchant": receipt_fields["merchant"],
                    "description": None,
                    "category": None,
                }
            else:
                fields = resolve_simple_expense_conversion_fields(conn, proposal)
        except (ParserConfirmationError, ReceiptFactsConversionError) as exc:
            raise PostingAuthorityError("proposal is not eligible for D2 posting") from exc
        if display_fields is None:
            display_fields = fields
        posting_path, projection, candidate = _projection_for_review(
            conn,
            parser_output_id=parser_output_id,
            fields=fields,
            receipt_payer_participant_public_id=receipt_payer_participant_public_id,
        )
        projection_json = _canonical(projection)
        projection_hash = _sha256_text(projection_json)
        presentation = _presentation_text(
            card_ref, projection, posting_path, display_fields=display_fields
        )
        created_at = _now_text(now)
        if source_kind == "initial_proposal_card":
            assert admitted_source_identity_sha256 is not None
            existing_card = conn.execute(
                "SELECT * FROM d2_initial_proposal_cards WHERE initial_card_public_id = ?",
                (initial_card_public_id,),
            ).fetchone()
            if existing_card is None:
                conn.execute(
                    """
                    INSERT INTO d2_initial_proposal_cards (
                        initial_card_public_id, card_idempotency_key, parser_output_id,
                        proposal_version, proposal_content_hash, raw_intake_record_id,
                        admitted_source_message_id, admitted_source_identity_sha256,
                        authenticated_actor_id,
                        telegram_account_id, telegram_conversation_id,
                        conversation_binding_id, visible_projection_json,
                        visible_projection_hash, presentation_text,
                        presentation_text_hash, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        initial_card_public_id,
                        f"initial:{review_idempotency_key}",
                        parser_output_id,
                        version,
                        content_hash,
                        int(intake["id"]),
                        admitted_source_message_id,
                        admitted_source_identity_sha256,
                        context.actor_id,
                        context.account_id,
                        context.conversation_id,
                        context.binding_id,
                        projection_json,
                        projection_hash,
                        presentation,
                        _sha256_text(presentation),
                        expires_at,
                        created_at,
                    ),
                )
            else:
                if existing_card["card_idempotency_key"] != f"initial:{review_idempotency_key}":
                    raise PostingAuthorityError("initial card idempotency conflict")
                expires_at = int(existing_card["expires_at"])
                if expires_at <= now:
                    raise PostingAuthorityError("initial card expired")
                expected_card = (
                    parser_output_id,
                    version,
                    content_hash,
                    int(intake["id"]),
                    admitted_source_message_id,
                    admitted_source_identity_sha256,
                    context.actor_id,
                    context.account_id,
                    context.conversation_id,
                    context.binding_id,
                    projection_json,
                    projection_hash,
                    presentation,
                    _sha256_text(presentation),
                    expires_at,
                )
                actual_card = tuple(
                    existing_card[name]
                    for name in (
                        "parser_output_id",
                        "proposal_version",
                        "proposal_content_hash",
                        "raw_intake_record_id",
                        "admitted_source_message_id",
                        "admitted_source_identity_sha256",
                        "authenticated_actor_id",
                        "telegram_account_id",
                        "telegram_conversation_id",
                        "conversation_binding_id",
                        "visible_projection_json",
                        "visible_projection_hash",
                        "presentation_text",
                        "presentation_text_hash",
                        "expires_at",
                    )
                )
                if actual_card != expected_card:
                    raise PostingAuthorityError("initial card idempotency conflict")

        review_public_id = _identity("d2rev", review_idempotency_key)
        collision = conn.execute(
            "SELECT review_public_id FROM d2_posting_reviews "
            "WHERE review_public_id = ? OR review_idempotency_key = ?",
            (review_public_id, review_idempotency_key),
        ).fetchone()
        idempotent = collision is not None
        if collision is not None and collision["review_public_id"] != review_public_id:
            raise PostingAuthorityError("posting review identity collision")
        if collision is None:
            conn.execute(
                """
                INSERT INTO d2_posting_reviews (
                    review_public_id, review_idempotency_key, source_kind,
                    source_generation, card_generation_public_id, initial_card_public_id,
                    predecessor_review_public_id, parser_output_id, proposal_version,
                    proposal_content_hash, posting_path, authenticated_actor_id,
                    telegram_account_id, telegram_conversation_id,
                    conversation_binding_id, visible_projection_json,
                    visible_projection_hash, receipt_fact_candidate_json,
                    expires_at, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_public_id,
                    review_idempotency_key,
                    source_kind,
                    card_generation_public_id,
                    initial_card_public_id,
                    parser_output_id,
                    version,
                    content_hash,
                    posting_path,
                    context.actor_id,
                    context.account_id,
                    context.conversation_id,
                    context.binding_id,
                    projection_json,
                    projection_hash,
                    None if candidate is None else _canonical(candidate),
                    expires_at,
                    created_at,
                ),
            )
        durable = _review_row(conn, review_public_id)
        expected = (
            review_idempotency_key,
            source_kind,
            1,
            card_generation_public_id,
            initial_card_public_id,
            parser_output_id,
            version,
            content_hash,
            posting_path,
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            projection_json,
            projection_hash,
            None if candidate is None else _canonical(candidate),
            expires_at,
        )
        actual = tuple(
            durable[name]
            for name in (
                "review_idempotency_key",
                "source_kind",
                "source_generation",
                "card_generation_public_id",
                "initial_card_public_id",
                "parser_output_id",
                "proposal_version",
                "proposal_content_hash",
                "posting_path",
                "authenticated_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
                "visible_projection_json",
                "visible_projection_hash",
                "receipt_fact_candidate_json",
                "expires_at",
            )
        )
        if actual != expected:
            raise PostingAuthorityError("posting review idempotency conflict")
        conn.commit()
        return PreparedPostingReview(
            review_public_id=review_public_id,
            source_kind=source_kind,
            card_generation_public_id=card_generation_public_id,
            initial_card_public_id=initial_card_public_id,
            proposal_public_id=resolved_proposal_public_id,
            proposal_version=version,
            proposal_content_hash=content_hash,
            posting_path=posting_path,
            visible_projection=projection,
            visible_projection_hash=projection_hash,
            presentation_text=presentation,
            expires_at=expires_at,
            idempotent=idempotent,
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def begin_posting_review_delivery(
    conn: sqlite3.Connection,
    *,
    review_public_id: str,
    key: bytes,
    context: HumanActionContext,
    clock: Callable[[], int] = _now_epoch,
) -> PostingReviewDeliveryManifest:
    """Atomically issue all controls and persist the complete pre-send attempt."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    review = _review_row(conn, review_public_id)
    _require_context(context)
    if (
        review["authenticated_actor_id"] != context.actor_id
        or review["telegram_account_id"] != context.account_id
        or review["telegram_conversation_id"] != context.conversation_id
        or review["conversation_binding_id"] != context.binding_id
    ):
        raise PostingAuthorityError("review context mismatch")
    now = clock()
    _require_current_review(conn, review, now=now)
    remaining = int(review["expires_at"]) - now
    if remaining < 60:
        raise PostingAuthorityError("posting review expired or too close to expiry")
    issuance_key = f"bridge-human-action-issue:{_sha256_text(review_public_id)[:32]}"
    existing_ttl_row = conn.execute(
        "SELECT ttl_seconds FROM openclaw_human_action_references "
        "WHERE issuance_idempotency_key = ? LIMIT 1",
        (issuance_key,),
    ).fetchone()
    issuance_ttl = (
        int(existing_ttl_row["ttl_seconds"])
        if existing_ttl_row is not None
        else min(3600, remaining)
    )
    if (
        conn.execute(
            "SELECT 1 FROM d2_posting_review_supersessions WHERE predecessor_review_public_id = ?",
            (review_public_id,),
        ).fetchone()
        is not None
    ):
        raise PostingAuthorityError("posting review was superseded")
    card_ref = (
        str(review["card_generation_public_id"])
        if review["source_kind"] == "d1_human_card"
        else str(review["initial_card_public_id"])
    )
    projection = canonical_json_value(
        str(review["visible_projection_json"]), label="D2 visible projection"
    )
    if not isinstance(projection, dict):
        raise PostingAuthorityError("D2 visible projection is malformed")
    display_fields: Mapping[str, object] | None = None
    if review["source_kind"] == "d1_human_card":
        d1_card = conn.execute(
            "SELECT field_values_json FROM parser_human_draft_cards "
            "WHERE card_generation_public_id = ?",
            (review["card_generation_public_id"],),
        ).fetchone()
        if d1_card is None:
            raise PostingAuthorityError("D1 card presentation authority is unavailable")
        parsed_fields = json.loads(str(d1_card["field_values_json"]))
        if not isinstance(parsed_fields, dict):
            raise PostingAuthorityError("D1 card presentation fields are malformed")
        display_fields = parsed_fields
    presentation = _presentation_text(
        card_ref,
        projection,
        str(review["posting_path"]),
        display_fields=display_fields,
    )
    if review["source_kind"] == "initial_proposal_card":
        initial = conn.execute(
            "SELECT presentation_text, presentation_text_hash "
            "FROM d2_initial_proposal_cards WHERE initial_card_public_id = ?",
            (review["initial_card_public_id"],),
        ).fetchone()
        if initial is None or not hmac.compare_digest(
            str(initial["presentation_text_hash"]),
            _sha256_text(str(initial["presentation_text"])),
        ):
            raise PostingAuthorityError("initial card presentation integrity mismatch")
        presentation = str(initial["presentation_text"])

    manifest_box: dict[str, object] = {}

    def bind(
        locked: sqlite3.Connection,
        rows: tuple[dict, ...],
        issued: tuple[IssuedHumanActionReference, ...],
        bound_at: int,
    ) -> None:
        if {str(row["action"]) for row in rows} != {"confirm", "edit", "reject"}:
            raise PostingAuthorityError("posting review controls are incomplete")
        row_by_action = {str(row["action"]): row for row in rows}
        issued_by_action = {item.action: item for item in issued}
        confirm_row = row_by_action["confirm"]
        collisions = locked.execute(
            "SELECT review_public_id, reference_id FROM d2_posting_review_action_bindings "
            "WHERE review_public_id = ? OR reference_id = ?",
            (review_public_id, confirm_row["id"]),
        ).fetchall()
        if not collisions:
            locked.execute(
                "INSERT INTO d2_posting_review_action_bindings "
                "(review_public_id, reference_id, bound_at) VALUES (?, ?, ?)",
                (review_public_id, confirm_row["id"], _now_text(bound_at)),
            )
            collisions = locked.execute(
                "SELECT review_public_id, reference_id "
                "FROM d2_posting_review_action_bindings WHERE review_public_id = ?",
                (review_public_id,),
            ).fetchall()
        if len(collisions) != 1 or (
            str(collisions[0]["review_public_id"]) != review_public_id
            or int(collisions[0]["reference_id"]) != int(confirm_row["id"])
        ):
            raise PostingAuthorityError("posting review action binding conflict")
        layouts = {
            "confirm": ("Confirm", "post:", 0, 0),
            "edit": ("Edit", "edit:", 1, 0),
            "reject": ("Reject", "reject:", 1, 1),
        }
        controls = tuple(
            PostingReviewControl(
                action=action,
                label=layouts[action][0],
                row_index=layouts[action][2],
                column_index=layouts[action][3],
                callback_value=layouts[action][1] + issued_by_action[action].reference,
            )
            for action in ("confirm", "edit", "reject")
        )
        material_digest = finance_delivery_material_digest(presentation, controls)
        delivery_id = _long_identity("d2send", review_public_id, material_digest)
        nonce_material = _framed_field("version", b"finance_d2_delivery_attempt_nonce_v1")
        nonce_material += _framed_field("attempt", delivery_id.encode("ascii"))
        nonce_material += _framed_field("digest", bytes.fromhex(material_digest))
        attempt_nonce = "d2nonce_" + hmac.new(key, nonce_material, hashlib.sha256).hexdigest()[:32]
        for control in controls:
            row = row_by_action[control.action]
            durable = locked.execute(
                "SELECT * FROM d2_posting_review_controls "
                "WHERE (review_public_id = ? AND action = ?) OR reference_id = ? "
                "OR (review_public_id = ? AND row_index = ? AND column_index = ?)",
                (
                    review_public_id,
                    control.action,
                    row["id"],
                    review_public_id,
                    control.row_index,
                    control.column_index,
                ),
            ).fetchall()
            expected_control = (
                review_public_id,
                control.action,
                int(row["id"]),
                str(row["purpose"]),
                control.row_index,
                control.column_index,
                control.label,
                layouts[control.action][1],
                _callback_value_digest(control.callback_value).hex(),
            )
            if not durable:
                locked.execute(
                    "INSERT INTO d2_posting_review_controls "
                    "(review_public_id, action, reference_id, purpose, row_index, "
                    "column_index, label, callback_route, callback_value_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*expected_control, _now_text(bound_at)),
                )
                durable = locked.execute(
                    "SELECT * FROM d2_posting_review_controls "
                    "WHERE review_public_id = ? AND action = ?",
                    (review_public_id, control.action),
                ).fetchall()
            actual_control = (
                tuple(
                    durable[0][name]
                    for name in (
                        "review_public_id",
                        "action",
                        "reference_id",
                        "purpose",
                        "row_index",
                        "column_index",
                        "label",
                        "callback_route",
                        "callback_value_sha256",
                    )
                )
                if len(durable) == 1
                else ()
            )
            if actual_control != expected_control:
                raise PostingAuthorityError("posting review control binding conflict")
        attempt = locked.execute(
            "SELECT * FROM d2_posting_review_delivery_attempts WHERE review_public_id = ?",
            (review_public_id,),
        ).fetchone()
        if attempt is None:
            locked.execute(
                "INSERT INTO d2_posting_review_delivery_attempts "
                "(delivery_attempt_public_id, review_public_id, manifest_version, "
                "presentation_text, finance_delivery_material_sha256, "
                "attempt_nonce_sha256, authenticated_actor_id, telegram_account_id, "
                "telegram_conversation_id, conversation_binding_id, attempted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    review_public_id,
                    DELIVERY_MANIFEST_VERSION,
                    presentation,
                    material_digest,
                    _sha256_text(attempt_nonce),
                    context.actor_id,
                    context.account_id,
                    context.conversation_id,
                    context.binding_id,
                    bound_at,
                ),
            )
        else:
            expected_attempt = (
                delivery_id,
                DELIVERY_MANIFEST_VERSION,
                presentation,
                material_digest,
                _sha256_text(attempt_nonce),
                context.actor_id,
                context.account_id,
                context.conversation_id,
                context.binding_id,
            )
            actual_attempt = tuple(
                attempt[name]
                for name in (
                    "delivery_attempt_public_id",
                    "manifest_version",
                    "presentation_text",
                    "finance_delivery_material_sha256",
                    "attempt_nonce_sha256",
                    "authenticated_actor_id",
                    "telegram_account_id",
                    "telegram_conversation_id",
                    "conversation_binding_id",
                )
            )
            if actual_attempt != expected_attempt:
                raise PostingAuthorityError("posting delivery attempt idempotency conflict")
        durable_controls = locked.execute(
            "SELECT action, reference_id, purpose, row_index, column_index, label, "
            "callback_route, callback_value_sha256 FROM d2_posting_review_controls "
            "WHERE review_public_id = ? ORDER BY row_index, column_index",
            (review_public_id,),
        ).fetchall()
        if len(durable_controls) != 3:
            raise PostingAuthorityError("posting review controls did not persist atomically")
        _inject_failure("before_delivery_attempt_transaction_commit")
        manifest_box.update(
            delivery_id=delivery_id,
            controls=controls,
            material_digest=material_digest,
            attempt_nonce=attempt_nonce,
        )

    _issued, replay = issue_human_action_references(
        conn,
        key=key,
        issuance_idempotency_key=issuance_key,
        proposal_public_id=str(review["proposal_public_id"]),
        expected_proposal_version=int(review["proposal_version"]),
        expected_proposal_content_hash=str(review["proposal_content_hash"]),
        context=context,
        ttl_seconds=issuance_ttl,
        allowed_actions=("confirm", "edit", "reject"),
        action_purposes={"confirm": "d2_post_v1", "edit": "edit_v1", "reject": "reject_v1"},
        card_generation_public_id=(
            None
            if review["card_generation_public_id"] is None
            else str(review["card_generation_public_id"])
        ),
        issuance_effect=bind,
        clock=clock,
    )
    _inject_failure("after_delivery_attempt_commit")
    return PostingReviewDeliveryManifest(
        review_public_id=review_public_id,
        delivery_attempt_public_id=str(manifest_box["delivery_id"]),
        version=DELIVERY_MANIFEST_VERSION,
        text=presentation,
        controls=manifest_box["controls"],  # type: ignore[arg-type]
        finance_delivery_material_sha256=str(manifest_box["material_digest"]),
        delivery_attempt_nonce=str(manifest_box["attempt_nonce"]),
        idempotent=replay,
    )


def issue_posting_review_actions(
    conn: sqlite3.Connection,
    *,
    review_public_id: str,
    key: bytes,
    context: HumanActionContext,
    clock: Callable[[], int] = _now_epoch,
) -> tuple[IssuedHumanActionReference, bool]:
    """Compatibility facade returning the Confirm reference from the full manifest."""
    manifest = begin_posting_review_delivery(
        conn, review_public_id=review_public_id, key=key, context=context, clock=clock
    )
    confirm = next(control for control in manifest.controls if control.action == "confirm")
    return (
        IssuedHumanActionReference(
            action="confirm",
            reference=confirm.callback_value.removeprefix("post:"),
            expires_at=_review_row(conn, review_public_id)["expires_at"],
        ),
        manifest.idempotent,
    )


def _require_current_review(conn: sqlite3.Connection, review: sqlite3.Row, *, now: int) -> None:
    if (
        conn.execute(
            "SELECT 1 FROM d2_posting_review_supersessions WHERE predecessor_review_public_id = ?",
            (review["review_public_id"],),
        ).fetchone()
        is not None
    ):
        raise PostingAuthorityError("posting review was superseded")
    if int(review["expires_at"]) <= now:
        raise PostingAuthorityError("posting review expired")
    proposal = ParserProposalRepository(conn).get(int(review["parser_output_id"]))
    if proposal is None or proposal["parse_status"] in {"confirmed", "rejected", "superseded"}:
        raise PostingAuthorityError("posting review proposal is no longer pending")
    _payload, _completion_id, version = resolve_effective_payload(conn, proposal)
    content_hash = compute_effective_proposal_content_hash(
        conn, {"id": int(review["parser_output_id"])}
    )
    if version != int(review["proposal_version"]) or not hmac.compare_digest(
        content_hash, str(review["proposal_content_hash"])
    ):
        raise PostingAuthorityError("posting review proposal changed")
    if review["source_kind"] == "d1_human_card":
        card = conn.execute(
            """
            SELECT cards.expires_at, drafts.expires_at AS draft_expires_at,
                   drafts.state, drafts.current_card_generation_public_id
            FROM parser_human_draft_cards AS cards
            JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id
            WHERE cards.card_generation_public_id = ?
            """,
            (review["card_generation_public_id"],),
        ).fetchone()
        if (
            card is None
            or card["state"] != "active"
            or card["current_card_generation_public_id"] != review["card_generation_public_id"]
            or int(card["expires_at"]) <= now
            or int(card["draft_expires_at"]) <= now
        ):
            raise PostingAuthorityError("D1 card review is stale")
        return
    if review["source_kind"] != "initial_proposal_card":
        raise PostingAuthorityError("posting review source is invalid")
    initial = conn.execute(
        """
        SELECT cards.*, intake.parser_output_id AS intake_parser_output_id,
               intake.source_message_id, intake.external_source_id,
               intake.source_channel, intake.status AS intake_status
        FROM d2_initial_proposal_cards AS cards
        JOIN raw_intake_records AS intake ON intake.id = cards.raw_intake_record_id
        WHERE cards.initial_card_public_id = ?
        """,
        (review["initial_card_public_id"],),
    ).fetchone()
    source_context_digest: str | None = None
    if initial is not None:
        try:
            source_context_digest = require_telegram_source_context(
                conn,
                raw_intake_record_id=int(initial["raw_intake_record_id"]),
                context=TelegramSourceContext(
                    authenticated_actor_id=str(review["authenticated_actor_id"]),
                    account_id=str(review["telegram_account_id"]),
                    conversation_id=str(review["telegram_conversation_id"]),
                    binding_id=str(review["conversation_binding_id"]),
                    message_id=str(initial["admitted_source_message_id"]),
                ),
            )
        except TelegramSourceContextError:
            source_context_digest = None
    active_draft = conn.execute(
        "SELECT 1 FROM parser_human_drafts "
        "WHERE decision_target_parser_output_id = ? AND state = 'active' LIMIT 1",
        (review["parser_output_id"],),
    ).fetchone()
    if (
        initial is None
        or int(initial["parser_output_id"]) != int(review["parser_output_id"])
        or int(initial["intake_parser_output_id"]) != int(review["parser_output_id"])
        or str(initial["source_message_id"] or "") != str(initial["admitted_source_message_id"])
        or initial["source_channel"] != "telegram"
        or str(initial["external_source_id"] or "")
        != f"telegram:{review['telegram_conversation_id']}:{initial['admitted_source_message_id']}"
        or source_context_digest is None
        or not hmac.compare_digest(
            str(initial["admitted_source_identity_sha256"]),
            source_context_digest,
        )
        or initial["intake_status"] != "parsed_pending_confirmation"
        or int(initial["expires_at"]) <= now
        or active_draft is not None
    ):
        raise PostingAuthorityError("initial proposal review is stale")


def record_posting_review_delivery(
    conn: sqlite3.Connection,
    *,
    receipt: object,
    clock: Callable[[], int] = _now_epoch,
) -> str:
    """Record material exposed only while consuming one host-owned receipt."""
    from finance_core.openclaw_staging_bridge.delivery_receipt_proof import (
        require_verified_delivery_receipt,
    )

    try:
        verified = require_verified_delivery_receipt(receipt)
    except ValueError as exc:
        raise PostingAuthorityError("host-authenticated delivery receipt is required") from exc
    attempt_nonce = verified.attempt_nonce
    capability = verified.capability
    delivery_material_version = verified.delivery_material_version
    finance_delivery_material_sha256 = verified.delivery_material_sha256
    provider_message_id = verified.provider_message_id
    receipt_token_sha256 = verified.receipt_token_sha256
    channel = verified.channel
    account_id = verified.account_id
    conversation_id = verified.conversation_id
    session_key = verified.session_key
    source_identity_sha256 = verified.source_identity_sha256
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    database_rows = conn.execute("PRAGMA database_list").fetchall()
    main_database = next((row for row in database_rows if str(row[1]) == "main"), None)
    if main_database is None:
        raise PostingAuthorityError("delivery receipt database identity is unavailable")
    database_path = str(main_database[2] or "")
    if not database_path:
        raise PostingAuthorityError("delivery receipt database identity is unavailable")
    expected_database = Path(verified.workspace_path) / "database" / "staging.sqlite"
    opened_database = Path(database_path)
    try:
        if (
            not opened_database.is_absolute()
            or opened_database.is_symlink()
            or expected_database.is_symlink()
            or opened_database.resolve(strict=True) != opened_database
            or expected_database.resolve(strict=True) != expected_database
        ):
            raise OSError("workspace database path is unsafe")
        expected_identity = os.stat(expected_database, follow_symlinks=False)
        opened_identity = os.stat(opened_database, follow_symlinks=False)
    except OSError as exc:
        raise PostingAuthorityError(
            "delivery receipt workspace database identity is unavailable"
        ) from exc
    if (
        opened_database != expected_database
        or (expected_identity.st_dev, expected_identity.st_ino)
        != (opened_identity.st_dev, opened_identity.st_ino)
        or expected_identity.st_nlink != 1
        or opened_identity.st_nlink != 1
    ):
        raise PostingAuthorityError("delivery receipt workspace database identity mismatch")
    if capability != DELIVERY_MATERIAL_CAPABILITY:
        raise PostingAuthorityError("terminal delivery capability is invalid")
    if delivery_material_version != DELIVERY_MATERIAL_VERSION:
        raise PostingAuthorityError("terminal delivery material version is invalid")
    if channel != "telegram":
        raise PostingAuthorityError("terminal delivery channel is invalid")
    if not all(
        isinstance(value, str)
        and value
        and not any(ord(char) <= 31 or ord(char) == 127 for char in value)
        for value in (attempt_nonce, account_id, conversation_id, session_key)
    ):
        raise PostingAuthorityError("terminal delivery receipt context is malformed")
    if len(attempt_nonce.encode("utf-8")) > 512:
        raise PostingAuthorityError("terminal delivery attempt nonce is malformed")
    material_sha256 = _require_lower_sha256(
        finance_delivery_material_sha256, label="terminal delivery material digest"
    )
    receipt_sha256 = _require_lower_sha256(
        receipt_token_sha256, label="terminal delivery receipt token digest"
    )
    source_sha256 = _require_lower_sha256(
        source_identity_sha256, label="terminal delivery source identity"
    )
    message_id = _telegram_message_id(provider_message_id)
    if conn.in_transaction:
        raise PostingAuthorityError("delivery activation requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    conflict = False
    try:
        row = conn.execute(
            """
            SELECT attempts.*, reviews.*,
                   activations.provider_message_id AS activated_message_id,
                   activations.observation_public_id AS activated_observation_id
            FROM d2_posting_review_delivery_attempts AS attempts
            JOIN d2_posting_reviews AS reviews
              ON reviews.review_public_id = attempts.review_public_id
            LEFT JOIN d2_posting_review_delivery_activations AS activations
              ON activations.review_public_id = reviews.review_public_id
            WHERE attempts.attempt_nonce_sha256 = ?
            """,
            (_sha256_text(attempt_nonce),),
        ).fetchone()
        if row is None:
            raise PostingAuthorityError("posting delivery attempt not found")
        if (
            row["telegram_account_id"] != account_id
            or row["telegram_conversation_id"] != conversation_id
            or row["conversation_binding_id"] != session_key
            or not hmac.compare_digest(
                str(row["finance_delivery_material_sha256"]),
                material_sha256,
            )
        ):
            raise PostingAuthorityError("terminal delivery receipt does not match its attempt")
        token_owner = conn.execute(
            "SELECT observation_public_id, delivery_attempt_public_id, provider_message_id, "
            "finance_delivery_material_sha256, source_identity_sha256 "
            "FROM d2_posting_review_delivery_observations WHERE receipt_token_sha256 = ?",
            (receipt_sha256,),
        ).fetchone()
        now = clock()
        delivery_attempt_public_id = str(row["delivery_attempt_public_id"])
        if token_owner is not None:
            if (
                str(token_owner["delivery_attempt_public_id"]) == delivery_attempt_public_id
                and int(token_owner["provider_message_id"]) == message_id
                and hmac.compare_digest(
                    str(token_owner["finance_delivery_material_sha256"]), material_sha256
                )
                and hmac.compare_digest(str(token_owner["source_identity_sha256"]), source_sha256)
            ):
                conn.rollback()
                return str(token_owner["observation_public_id"])
            raise PostingAuthorityError("terminal delivery receipt token was already consumed")
        observation_id = _long_identity(
            "d2dobs",
            delivery_attempt_public_id,
            message_id,
            material_sha256,
            receipt_sha256,
        )
        activated_message_id = row["activated_message_id"]
        if activated_message_id is not None and int(activated_message_id) == message_id:
            activation = conn.execute(
                "SELECT observation_public_id, finance_delivery_material_sha256, "
                "source_identity_sha256 FROM d2_posting_review_delivery_activations "
                "WHERE review_public_id = ?",
                (row["review_public_id"],),
            ).fetchone()
            if (
                activation is not None
                and hmac.compare_digest(
                    str(activation["finance_delivery_material_sha256"]), material_sha256
                )
                and hmac.compare_digest(str(activation["source_identity_sha256"]), source_sha256)
            ):
                conn.rollback()
                return str(activation["observation_public_id"])
            raise PostingAuthorityError("terminal delivery replay conflicts with activation")
        if activated_message_id is not None:
            conflict_id = _long_identity(
                "d2dcon", row["review_public_id"], activated_message_id, message_id
            )
            conn.execute(
                "INSERT INTO d2_posting_review_delivery_observations "
                "(observation_public_id, delivery_attempt_public_id, outcome, "
                "provider_message_id, finance_delivery_material_sha256, "
                "receipt_token_sha256, source_identity_sha256, channel, "
                "telegram_account_id, telegram_conversation_id, conversation_binding_id, "
                "error_code, observed_at) VALUES (?, ?, 'conflict', ?, ?, ?, ?, "
                "'telegram', ?, ?, ?, 'multiple_provider_messages', ?)",
                (
                    observation_id,
                    delivery_attempt_public_id,
                    message_id,
                    material_sha256,
                    receipt_sha256,
                    source_sha256,
                    account_id,
                    conversation_id,
                    session_key,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO d2_posting_review_delivery_conflicts "
                "(conflict_public_id, review_public_id, delivery_attempt_public_id, "
                "activated_observation_public_id, conflicting_observation_public_id, "
                "conflicting_observation_outcome, activated_provider_message_id, "
                "conflicting_provider_message_id, "
                "finance_delivery_material_sha256, receipt_token_sha256, "
                "source_identity_sha256, channel, telegram_account_id, "
                "telegram_conversation_id, conversation_binding_id, observed_at) "
                "VALUES (?, ?, ?, ?, ?, 'conflict', ?, ?, ?, ?, ?, 'telegram', ?, ?, ?, ?)",
                (
                    conflict_id,
                    row["review_public_id"],
                    delivery_attempt_public_id,
                    row["activated_observation_id"],
                    observation_id,
                    activated_message_id,
                    message_id,
                    material_sha256,
                    receipt_sha256,
                    source_sha256,
                    account_id,
                    conversation_id,
                    session_key,
                    now,
                ),
            )
            conflict = True
        else:
            _require_current_review(conn, row, now=now)
            conn.execute(
                "INSERT INTO d2_posting_review_delivery_observations "
                "(observation_public_id, delivery_attempt_public_id, outcome, "
                "provider_message_id, finance_delivery_material_sha256, "
                "receipt_token_sha256, source_identity_sha256, channel, "
                "telegram_account_id, telegram_conversation_id, conversation_binding_id, "
                "error_code, observed_at) VALUES (?, ?, 'success', ?, ?, ?, ?, "
                "'telegram', ?, ?, ?, NULL, ?)",
                (
                    observation_id,
                    delivery_attempt_public_id,
                    message_id,
                    material_sha256,
                    receipt_sha256,
                    source_sha256,
                    account_id,
                    conversation_id,
                    session_key,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO d2_posting_review_delivery_activations "
                "(review_public_id, delivery_attempt_public_id, observation_public_id, "
                "observation_outcome, provider_message_id, channel, telegram_account_id, "
                "telegram_conversation_id, conversation_binding_id, "
                "finance_delivery_material_sha256, receipt_token_sha256, "
                "source_identity_sha256, activated_at) "
                "VALUES (?, ?, ?, 'success', ?, 'telegram', ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["review_public_id"],
                    delivery_attempt_public_id,
                    observation_id,
                    message_id,
                    account_id,
                    conversation_id,
                    session_key,
                    material_sha256,
                    receipt_sha256,
                    source_sha256,
                    now,
                ),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    if conflict:
        raise PostingAuthorityError("multiple provider messages conflict for one D2 review")
    return observation_id


def replace_posting_review_delivery(
    conn: sqlite3.Connection,
    *,
    predecessor_review_public_id: str,
    replacement_idempotency_key: str,
    replacement_material_hash: str,
    reason: str,
    key: bytes,
    context: HumanActionContext,
    clock: Callable[[], int] = _now_epoch,
) -> PostingReviewDeliveryManifest:
    """Supersede one unaccepted review and issue a deterministic successor delivery."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    _require_context(context)
    if reason not in {"delivery_unknown", "delivery_conflict", "expired"}:
        raise PostingAuthorityError("replacement reason is invalid")
    if (
        len(replacement_material_hash) != 64
        or any(character not in "0123456789abcdef" for character in replacement_material_hash)
        or not replacement_idempotency_key.strip()
    ):
        raise PostingAuthorityError("replacement material is invalid")
    if conn.in_transaction:
        raise PostingAuthorityError("review replacement requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        predecessor = _review_row(conn, predecessor_review_public_id)
        if (
            predecessor["authenticated_actor_id"] != context.actor_id
            or predecessor["telegram_account_id"] != context.account_id
            or predecessor["telegram_conversation_id"] != context.conversation_id
            or predecessor["conversation_binding_id"] != context.binding_id
        ):
            raise PostingAuthorityError("replacement context mismatch")
        if (
            conn.execute(
                "SELECT 1 FROM d2_posting_attempts WHERE review_public_id = ?",
                (predecessor_review_public_id,),
            ).fetchone()
            is not None
        ):
            raise PostingAuthorityError("accepted review cannot be replaced")
        existing = conn.execute(
            "SELECT * FROM d2_posting_review_supersessions "
            "WHERE predecessor_review_public_id = ? OR replacement_idempotency_key = ?",
            (predecessor_review_public_id, replacement_idempotency_key),
        ).fetchone()
        successor_id = _identity(
            "d2rev",
            predecessor_review_public_id,
            replacement_idempotency_key,
            replacement_material_hash,
        )
        if existing is not None:
            if (
                existing["predecessor_review_public_id"] != predecessor_review_public_id
                or existing["successor_review_public_id"] != successor_id
                or existing["replacement_idempotency_key"] != replacement_idempotency_key
                or not hmac.compare_digest(
                    str(existing["replacement_material_hash"]), replacement_material_hash
                )
                or existing["reason"] != reason
            ):
                raise PostingAuthorityError("replacement compare-and-swap conflict")
            conn.commit()
        else:
            if (
                conn.execute(
                    "SELECT 1 FROM d2_posting_review_supersessions "
                    "WHERE predecessor_review_public_id = ?",
                    (predecessor_review_public_id,),
                ).fetchone()
                is not None
            ):
                raise PostingAuthorityError("replacement compare-and-swap conflict")
            now = clock()
            remaining = max(60, int(predecessor["expires_at"]) - now)
            successor_expires_at = now + min(3600, remaining)
            conn.execute(
                """
                INSERT INTO d2_posting_reviews (
                    review_public_id, review_idempotency_key, source_kind,
                    source_generation, card_generation_public_id, initial_card_public_id,
                    predecessor_review_public_id, parser_output_id, proposal_version,
                    proposal_content_hash, posting_path, authenticated_actor_id,
                    telegram_account_id, telegram_conversation_id,
                    conversation_binding_id, visible_projection_json,
                    visible_projection_hash, receipt_fact_candidate_json,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    successor_id,
                    f"replacement:{replacement_idempotency_key}",
                    predecessor["source_kind"],
                    int(predecessor["source_generation"]) + 1,
                    predecessor["card_generation_public_id"],
                    predecessor["initial_card_public_id"],
                    predecessor_review_public_id,
                    predecessor["parser_output_id"],
                    predecessor["proposal_version"],
                    predecessor["proposal_content_hash"],
                    predecessor["posting_path"],
                    context.actor_id,
                    context.account_id,
                    context.conversation_id,
                    context.binding_id,
                    predecessor["visible_projection_json"],
                    predecessor["visible_projection_hash"],
                    predecessor["receipt_fact_candidate_json"],
                    successor_expires_at,
                    _now_text(now),
                ),
            )
            conn.execute(
                "INSERT INTO d2_posting_review_supersessions "
                "(predecessor_review_public_id, successor_review_public_id, "
                "replacement_idempotency_key, replacement_material_hash, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    predecessor_review_public_id,
                    successor_id,
                    replacement_idempotency_key,
                    replacement_material_hash,
                    reason,
                    _now_text(now),
                ),
            )
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return begin_posting_review_delivery(
        conn,
        review_public_id=successor_id,
        key=key,
        context=context,
        clock=clock,
    )


def _accepted_attempt_for_callback(
    conn: sqlite3.Connection,
    *,
    reference: str,
    context: HumanActionContext,
    callback_id: str,
    callback_message_id: int,
) -> tuple[str, str, str] | None:
    """Authenticate an exact accepted replay without reapplying expiry checks."""
    row = conn.execute(
        """
        SELECT attempts.attempt_public_id, attempts.review_public_id,
               purposes.purpose, refs.reference_sha256,
               refs.authenticated_actor_id, refs.channel_account_id,
               refs.channel_conversation_id, refs.conversation_binding_id,
               redemptions.callback_id_sha256, redemptions.callback_message_id
        FROM openclaw_human_action_references AS refs
        JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
        JOIN openclaw_human_action_reference_purposes AS purposes
          ON purposes.reference_id = refs.id
        JOIN d2_posting_decisions AS decisions ON decisions.reference_id = refs.id
        JOIN d2_posting_attempts AS attempts
          ON attempts.attempt_public_id = decisions.attempt_public_id
        WHERE refs.reference_sha256 = ?
        """,
        (_sha256_text(reference),),
    ).fetchone()
    if row is None:
        return None
    if (
        row["authenticated_actor_id"] != context.actor_id
        or row["channel_account_id"] != context.account_id
        or row["channel_conversation_id"] != context.conversation_id
        or row["conversation_binding_id"] != context.binding_id
        or not hmac.compare_digest(str(row["callback_id_sha256"]), _sha256_text(callback_id))
        or int(row["callback_message_id"]) != callback_message_id
    ):
        raise PostingAuthorityError("accepted Confirm replay does not match durable authority")
    return (
        str(row["attempt_public_id"]),
        str(row["review_public_id"]),
        str(row["purpose"]),
    )


def _has_valid_delivery_authority(
    row: sqlite3.Row, *, allow_post_decision_conflict: bool = False
) -> bool:
    required = (
        "delivery_attempt_id",
        "delivery_review_id",
        "delivery_material_sha256",
        "delivery_actor_id",
        "delivery_account_id",
        "delivery_conversation_id",
        "delivery_binding_id",
        "activation_attempt_id",
        "activation_review_id",
        "activation_observation_id",
        "activation_message_id",
        "activation_channel",
        "activation_account_id",
        "activation_conversation_id",
        "activation_binding_id",
        "activation_material_sha256",
        "activation_receipt_sha256",
        "activation_source_sha256",
        "observation_id",
        "observation_attempt_id",
        "observation_outcome",
        "observation_message_id",
        "observation_channel",
        "observation_account_id",
        "observation_conversation_id",
        "observation_binding_id",
        "observation_material_sha256",
        "observation_receipt_sha256",
        "observation_source_sha256",
    )
    if any(row[name] is None for name in required):
        return False
    if row["delivery_conflict_id"] is not None and (
        not allow_post_decision_conflict
        or row["decision_accepted_at"] is None
        or row["delivery_conflict_observed_at"] is None
        or int(row["delivery_conflict_observed_at"]) < int(row["decision_accepted_at"])
    ):
        return False
    return (
        str(row["delivery_review_id"]) == str(row["review_public_id"])
        and str(row["delivery_actor_id"]) == str(row["review_actor_id"])
        and str(row["delivery_account_id"]) == str(row["review_account_id"])
        and str(row["delivery_conversation_id"]) == str(row["review_conversation_id"])
        and str(row["delivery_binding_id"]) == str(row["review_binding_id"])
        and str(row["activation_review_id"]) == str(row["review_public_id"])
        and str(row["activation_attempt_id"]) == str(row["delivery_attempt_id"])
        and str(row["activation_channel"]) == "telegram"
        and str(row["activation_account_id"]) == str(row["delivery_account_id"])
        and str(row["activation_conversation_id"]) == str(row["delivery_conversation_id"])
        and str(row["activation_binding_id"]) == str(row["delivery_binding_id"])
        and str(row["observation_id"]) == str(row["activation_observation_id"])
        and str(row["observation_attempt_id"]) == str(row["delivery_attempt_id"])
        and str(row["observation_outcome"]) == "success"
        and int(row["observation_message_id"]) == int(row["activation_message_id"])
        and str(row["observation_channel"]) == "telegram"
        and str(row["observation_account_id"]) == str(row["activation_account_id"])
        and str(row["observation_conversation_id"]) == str(row["activation_conversation_id"])
        and str(row["observation_binding_id"]) == str(row["activation_binding_id"])
        and all(
            hmac.compare_digest(str(row[left]), str(row[right]))
            for left, right in (
                ("delivery_material_sha256", "activation_material_sha256"),
                ("delivery_material_sha256", "observation_material_sha256"),
                ("activation_receipt_sha256", "observation_receipt_sha256"),
                ("activation_source_sha256", "observation_source_sha256"),
            )
        )
    )


def _review_has_valid_delivery_authority(conn: sqlite3.Connection, review_public_id: str) -> bool:
    row = conn.execute(
        """
        SELECT reviews.review_public_id,
               reviews.authenticated_actor_id AS review_actor_id,
               reviews.telegram_account_id AS review_account_id,
               reviews.telegram_conversation_id AS review_conversation_id,
               reviews.conversation_binding_id AS review_binding_id,
               deliveries.delivery_attempt_public_id AS delivery_attempt_id,
               deliveries.review_public_id AS delivery_review_id,
               deliveries.finance_delivery_material_sha256 AS delivery_material_sha256,
               deliveries.authenticated_actor_id AS delivery_actor_id,
               deliveries.telegram_account_id AS delivery_account_id,
               deliveries.telegram_conversation_id AS delivery_conversation_id,
               deliveries.conversation_binding_id AS delivery_binding_id,
               activations.review_public_id AS activation_review_id,
               activations.delivery_attempt_public_id AS activation_attempt_id,
               activations.observation_public_id AS activation_observation_id,
               activations.provider_message_id AS activation_message_id,
               activations.channel AS activation_channel,
               activations.telegram_account_id AS activation_account_id,
               activations.telegram_conversation_id AS activation_conversation_id,
               activations.conversation_binding_id AS activation_binding_id,
               activations.finance_delivery_material_sha256 AS activation_material_sha256,
               activations.receipt_token_sha256 AS activation_receipt_sha256,
               activations.source_identity_sha256 AS activation_source_sha256,
               observations.observation_public_id AS observation_id,
               observations.delivery_attempt_public_id AS observation_attempt_id,
               observations.outcome AS observation_outcome,
               observations.provider_message_id AS observation_message_id,
               observations.channel AS observation_channel,
               observations.telegram_account_id AS observation_account_id,
               observations.telegram_conversation_id AS observation_conversation_id,
               observations.conversation_binding_id AS observation_binding_id,
               observations.finance_delivery_material_sha256 AS observation_material_sha256,
               observations.receipt_token_sha256 AS observation_receipt_sha256,
               observations.source_identity_sha256 AS observation_source_sha256,
               conflicts.conflict_public_id AS delivery_conflict_id,
               conflicts.observed_at AS delivery_conflict_observed_at,
               decisions.accepted_at AS decision_accepted_at
        FROM d2_posting_reviews AS reviews
        LEFT JOIN d2_posting_review_delivery_attempts AS deliveries
          ON deliveries.review_public_id = reviews.review_public_id
        LEFT JOIN d2_posting_review_delivery_activations AS activations
          ON activations.review_public_id = reviews.review_public_id
         AND activations.delivery_attempt_public_id = deliveries.delivery_attempt_public_id
        LEFT JOIN d2_posting_review_delivery_observations AS observations
          ON observations.observation_public_id = activations.observation_public_id
        LEFT JOIN d2_posting_review_delivery_conflicts AS conflicts
          ON conflicts.review_public_id = reviews.review_public_id
        LEFT JOIN d2_posting_decisions AS decisions
          ON decisions.review_public_id = reviews.review_public_id
        WHERE reviews.review_public_id = ?
        """,
        (review_public_id,),
    ).fetchone()
    if row is None:
        return False
    return _has_valid_delivery_authority(
        row, allow_post_decision_conflict=row["decision_accepted_at"] is not None
    )


def _authorized_attempt_for_resume(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    context: HumanActionContext,
) -> sqlite3.Row:
    """Require the complete redeemed D2 authority chain before any financial write."""
    row = conn.execute(
        """
        SELECT attempts.*, reviews.parser_output_id, reviews.review_public_id,
               reviews.proposal_version, reviews.proposal_content_hash,
               reviews.authenticated_actor_id AS review_actor_id,
               reviews.telegram_account_id AS review_account_id,
               reviews.telegram_conversation_id AS review_conversation_id,
               reviews.conversation_binding_id AS review_binding_id,
               decisions.decision_public_id, decisions.confirmation_public_id,
               decisions.accepted_at AS decision_accepted_at,
               purposes.purpose,
               deliveries.delivery_attempt_public_id AS delivery_attempt_id,
               deliveries.review_public_id AS delivery_review_id,
               deliveries.finance_delivery_material_sha256 AS delivery_material_sha256,
               deliveries.authenticated_actor_id AS delivery_actor_id,
               deliveries.telegram_account_id AS delivery_account_id,
               deliveries.telegram_conversation_id AS delivery_conversation_id,
               deliveries.conversation_binding_id AS delivery_binding_id,
               activations.review_public_id AS activation_review_id,
               activations.delivery_attempt_public_id AS activation_attempt_id,
               activations.observation_public_id AS activation_observation_id,
               activations.provider_message_id AS activation_message_id,
               activations.channel AS activation_channel,
               activations.telegram_account_id AS activation_account_id,
               activations.telegram_conversation_id AS activation_conversation_id,
               activations.conversation_binding_id AS activation_binding_id,
               activations.finance_delivery_material_sha256 AS activation_material_sha256,
               activations.receipt_token_sha256 AS activation_receipt_sha256,
               activations.source_identity_sha256 AS activation_source_sha256,
               observations.observation_public_id AS observation_id,
               observations.delivery_attempt_public_id AS observation_attempt_id,
               observations.outcome AS observation_outcome,
               observations.provider_message_id AS observation_message_id,
               observations.channel AS observation_channel,
               observations.telegram_account_id AS observation_account_id,
               observations.telegram_conversation_id AS observation_conversation_id,
               observations.conversation_binding_id AS observation_binding_id,
               observations.finance_delivery_material_sha256 AS observation_material_sha256,
               observations.receipt_token_sha256 AS observation_receipt_sha256,
               observations.source_identity_sha256 AS observation_source_sha256,
               conflicts.conflict_public_id AS delivery_conflict_id,
               conflicts.observed_at AS delivery_conflict_observed_at
        FROM d2_posting_attempts AS attempts
        JOIN d2_posting_reviews AS reviews
          ON reviews.review_public_id = attempts.review_public_id
        JOIN d2_posting_review_action_bindings AS bindings
          ON bindings.review_public_id = reviews.review_public_id
         AND bindings.reference_id = attempts.reference_id
        JOIN openclaw_human_action_references AS refs
          ON refs.id = attempts.reference_id
         AND refs.action = 'confirm'
         AND refs.parser_output_id = reviews.parser_output_id
         AND refs.proposal_version = reviews.proposal_version
         AND refs.proposal_content_hash = reviews.proposal_content_hash
         AND refs.authenticated_actor_id = reviews.authenticated_actor_id
         AND refs.channel_account_id = reviews.telegram_account_id
         AND refs.channel_conversation_id = reviews.telegram_conversation_id
         AND refs.conversation_binding_id = reviews.conversation_binding_id
        JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
        JOIN openclaw_human_action_reference_purposes AS purposes
          ON purposes.reference_id = refs.id
        JOIN d2_posting_decisions AS decisions
          ON decisions.attempt_public_id = attempts.attempt_public_id
         AND decisions.review_public_id = reviews.review_public_id
         AND decisions.reference_id = refs.id
        JOIN parser_proposal_authorizations AS confirmations
          ON confirmations.confirmation_public_id = decisions.confirmation_public_id
         AND confirmations.parser_output_id = reviews.parser_output_id
         AND confirmations.proposal_content_hash = reviews.proposal_content_hash
         AND confirmations.authenticated_actor_id = reviews.authenticated_actor_id
         AND confirmations.actor_type = 'human'
         AND confirmations.confirmation_state = 'confirmed'
        LEFT JOIN d2_posting_review_delivery_attempts AS deliveries
          ON deliveries.review_public_id = reviews.review_public_id
        LEFT JOIN d2_posting_review_delivery_activations AS activations
          ON activations.review_public_id = reviews.review_public_id
         AND activations.delivery_attempt_public_id = deliveries.delivery_attempt_public_id
        LEFT JOIN d2_posting_review_delivery_observations AS observations
          ON observations.observation_public_id = activations.observation_public_id
        LEFT JOIN d2_posting_review_delivery_conflicts AS conflicts
          ON conflicts.review_public_id = reviews.review_public_id
        WHERE attempts.attempt_public_id = ?
          AND attempts.posting_path = reviews.posting_path
          AND reviews.authenticated_actor_id = ?
          AND reviews.telegram_account_id = ?
          AND reviews.telegram_conversation_id = ?
          AND reviews.conversation_binding_id = ?
        """,
        (
            attempt_public_id,
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
        ),
    ).fetchone()
    if row is None:
        raise PostingAuthorityError("posting authority unavailable")
    purpose = str(row["purpose"])
    if purpose == "d2_post_v1":
        if not _has_valid_delivery_authority(row, allow_post_decision_conflict=True):
            raise PostingAuthorityError("current D2 delivery authority is incomplete")
    elif purpose != "d2_post_accepted_pre050_v1":
        raise PostingAuthorityError("posting recovery purpose is invalid")
    return row


def confirm_and_post(
    conn: sqlite3.Connection,
    *,
    key: bytes,
    reference: str,
    context: HumanActionContext,
    callback_id: str,
    callback_message_id: int,
    clock: Callable[[], int] = _now_epoch,
) -> PostingStatus:
    """Accept one Confirm action and advance its durable posting attempt."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    _require_context(context)
    accepted = _accepted_attempt_for_callback(
        conn,
        reference=reference,
        context=context,
        callback_id=callback_id,
        callback_message_id=callback_message_id,
    )
    if accepted is not None:
        attempt_public_id, review_public_id, purpose = accepted
        if purpose == "d2_post_accepted_pre050_v1":
            return get_status(conn, review_public_id=review_public_id, context=context)
        if purpose != "d2_post_v1":
            raise PostingAuthorityError("accepted Confirm replay purpose is invalid")
        return resume_posting(conn, attempt_public_id=attempt_public_id, context=context)

    def validate(locked: sqlite3.Connection, ref_row: dict, action: str) -> None:
        binding = locked.execute(
            """
            SELECT reviews.*, purposes.purpose,
                   reviews.authenticated_actor_id AS review_actor_id,
                   reviews.telegram_account_id AS review_account_id,
                   reviews.telegram_conversation_id AS review_conversation_id,
                   reviews.conversation_binding_id AS review_binding_id,
                   attempts.delivery_attempt_public_id AS delivery_attempt_id,
                   attempts.review_public_id AS delivery_review_id,
                   attempts.finance_delivery_material_sha256 AS delivery_material_sha256,
                   attempts.authenticated_actor_id AS delivery_actor_id,
                   attempts.telegram_account_id AS delivery_account_id,
                   attempts.telegram_conversation_id AS delivery_conversation_id,
                   attempts.conversation_binding_id AS delivery_binding_id,
                   activations.review_public_id AS activation_review_id,
                   activations.delivery_attempt_public_id AS activation_attempt_id,
                   activations.observation_public_id AS activation_observation_id,
                   activations.provider_message_id AS activation_message_id,
                   activations.channel AS activation_channel,
                   activations.telegram_account_id AS activation_account_id,
                   activations.telegram_conversation_id AS activation_conversation_id,
                   activations.conversation_binding_id AS activation_binding_id,
                   activations.finance_delivery_material_sha256 AS activation_material_sha256,
                   activations.receipt_token_sha256 AS activation_receipt_sha256,
                   activations.source_identity_sha256 AS activation_source_sha256,
                   observations.observation_public_id AS observation_id,
                   observations.delivery_attempt_public_id AS observation_attempt_id,
                   observations.outcome AS observation_outcome,
                   observations.provider_message_id AS observation_message_id,
                   observations.channel AS observation_channel,
                   observations.telegram_account_id AS observation_account_id,
                   observations.telegram_conversation_id AS observation_conversation_id,
                   observations.conversation_binding_id AS observation_binding_id,
                   observations.finance_delivery_material_sha256 AS observation_material_sha256,
                   observations.receipt_token_sha256 AS observation_receipt_sha256,
                   observations.source_identity_sha256 AS observation_source_sha256,
                   conflicts.conflict_public_id AS delivery_conflict_id,
                   conflicts.observed_at AS delivery_conflict_observed_at,
                   NULL AS decision_accepted_at
            FROM d2_posting_review_action_bindings AS bindings
            JOIN d2_posting_reviews AS reviews
              ON reviews.review_public_id = bindings.review_public_id
            JOIN openclaw_human_action_reference_purposes AS purposes
              ON purposes.reference_id = bindings.reference_id
            JOIN d2_posting_review_controls AS controls
              ON controls.review_public_id = reviews.review_public_id
             AND controls.reference_id = bindings.reference_id
             AND controls.action = 'confirm'
            JOIN d2_posting_review_delivery_attempts AS attempts
              ON attempts.review_public_id = reviews.review_public_id
            JOIN d2_posting_review_delivery_activations AS activations
              ON activations.review_public_id = reviews.review_public_id
             AND activations.delivery_attempt_public_id = attempts.delivery_attempt_public_id
            JOIN d2_posting_review_delivery_observations AS observations
              ON observations.observation_public_id = activations.observation_public_id
            LEFT JOIN d2_posting_review_delivery_conflicts AS conflicts
              ON conflicts.review_public_id = reviews.review_public_id
            WHERE bindings.reference_id = ?
            """,
            (ref_row["id"],),
        ).fetchone()
        if binding is None or action != "confirm":
            raise PostingAuthorityError("Confirm reference is not bound to a D2 posting review")
        if int(binding["expires_at"]) <= clock():
            raise PostingAuthorityError("posting review expired")
        if (
            binding["purpose"] != "d2_post_v1"
            or not _has_valid_delivery_authority(binding)
            or int(binding["activation_message_id"]) != callback_message_id
        ):
            raise PostingAuthorityError("Confirm is not bound to the activated provider message")
        _require_current_review(locked, binding, now=clock())
        if (
            int(binding["parser_output_id"]) != int(ref_row["parser_output_id"])
            or int(binding["proposal_version"]) != int(ref_row["proposal_version"])
            or not hmac.compare_digest(
                str(binding["proposal_content_hash"]), str(ref_row["proposal_content_hash"])
            )
        ):
            raise PostingAuthorityError("Confirm reference and review material do not match")
        if binding["posting_path"] == "personal_receipt":
            candidate_value = canonical_json_value(
                str(binding["receipt_fact_candidate_json"]),
                label="D2 receipt fact candidate",
            )
            if not isinstance(candidate_value, dict):
                raise PostingAuthorityError("D2 receipt fact candidate is malformed")
            _require_active_self_participant(
                locked, str(candidate_value.get("payer_participant_public_id") or "")
            )

    def accept(locked: sqlite3.Connection, ref_row: dict, action: str, accepted_at: int) -> None:
        review = locked.execute(
            """
            SELECT reviews.* FROM d2_posting_review_action_bindings AS bindings
            JOIN d2_posting_reviews AS reviews
              ON reviews.review_public_id = bindings.review_public_id
            WHERE bindings.reference_id = ?
            """,
            (ref_row["id"],),
        ).fetchone()
        if review is None or action != "confirm":
            raise PostingAuthorityError("D2 posting review binding is missing")
        decision_id = _identity("d2dec", review["review_public_id"], ref_row["reference_public_id"])
        attempt_id = _identity("d2att", review["review_public_id"], ref_row["reference_public_id"])
        confirmation_id = _identity(
            "pca_d2", review["review_public_id"], ref_row["reference_public_id"]
        )
        attempts = locked.execute(
            "SELECT attempt_public_id, review_public_id, reference_id, posting_path "
            "FROM d2_posting_attempts WHERE attempt_public_id = ? OR review_public_id = ? "
            "OR reference_id = ?",
            (attempt_id, review["review_public_id"], ref_row["id"]),
        ).fetchall()
        decisions = locked.execute(
            "SELECT decision_public_id, review_public_id, attempt_public_id, reference_id, "
            "confirmation_public_id FROM d2_posting_decisions "
            "WHERE decision_public_id = ? OR review_public_id = ? OR attempt_public_id = ? "
            "OR reference_id = ? OR confirmation_public_id = ?",
            (
                decision_id,
                review["review_public_id"],
                attempt_id,
                ref_row["id"],
                confirmation_id,
            ),
        ).fetchall()
        if attempts or decisions:
            if len(attempts) != 1 or len(decisions) != 1:
                raise PostingAuthorityError("D2 accepted replay authority is incomplete")
            attempt_material = tuple(attempts[0])
            decision_material = tuple(decisions[0])
            if attempt_material != (
                attempt_id,
                review["review_public_id"],
                ref_row["id"],
                review["posting_path"],
            ) or decision_material != (
                decision_id,
                review["review_public_id"],
                attempt_id,
                ref_row["id"],
                confirmation_id,
            ):
                raise PostingAuthorityError("D2 accepted replay authority conflicts")
            return
        d1_binding = None
        initial_binding = None
        if review["source_kind"] == "d1_human_card":
            d1_binding = HumanDraftDecisionBinding(
                reference_public_id=str(ref_row["reference_public_id"]),
                card_generation_public_id=str(review["card_generation_public_id"]),
                authenticated_actor_id=context.actor_id,
                telegram_account_id=context.account_id,
                telegram_conversation_id=context.conversation_id,
                conversation_binding_id=context.binding_id,
            )
        elif review["source_kind"] == "initial_proposal_card":
            initial_binding = InitialProposalDecisionBinding(
                review_public_id=str(review["review_public_id"]),
                reference_public_id=str(ref_row["reference_public_id"]),
                authenticated_actor_id=context.actor_id,
                telegram_account_id=context.account_id,
                telegram_conversation_id=context.conversation_id,
                conversation_binding_id=context.binding_id,
            )
        else:
            raise PostingAuthorityError("D2 posting review source is invalid")
        result = confirm_parser_proposal(
            locked,
            int(review["parser_output_id"]),
            authenticated_actor_id=context.actor_id,
            decision="confirmed",
            confirmation_channel="telegram",
            confirmation_public_id=confirmation_id,
            expected_content_hash=str(review["proposal_content_hash"]),
            expected_version=int(review["proposal_version"]),
            d1_decision_binding=d1_binding,
            initial_d2_decision_binding=initial_binding,
            clock=lambda: _now_text(accepted_at),
            _caller_owns_transaction=True,
        )
        if result["confirmation_id"] != confirmation_id:
            raise PostingAuthorityError("D2 confirmation identity conflict")
        created_at = _now_text(accepted_at)
        locked.execute(
            "INSERT INTO d2_posting_attempts "
            "(attempt_public_id, review_public_id, reference_id, posting_path, stage, "
            "stage_evidence_public_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?)",
            (
                attempt_id,
                review["review_public_id"],
                ref_row["id"],
                review["posting_path"],
                confirmation_id,
                created_at,
                created_at,
            ),
        )
        locked.execute(
            "INSERT INTO d2_posting_decisions "
            "(decision_public_id, review_public_id, attempt_public_id, reference_id, "
            "confirmation_public_id, accepted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                review["review_public_id"],
                attempt_id,
                ref_row["id"],
                confirmation_id,
                accepted_at,
                created_at,
            ),
        )

    redeemed = redeem_human_action_reference(
        conn,
        key=key,
        reference=reference,
        action="confirm",
        context=context,
        callback_id=callback_id,
        callback_message_id=callback_message_id,
        required_purpose="d2_post_v1",
        action_validator=validate,
        redemption_effect=accept,
        clock=clock,
    )
    attempt = conn.execute(
        "SELECT attempts.attempt_public_id FROM d2_posting_attempts AS attempts "
        "JOIN openclaw_human_action_references AS refs ON refs.id = attempts.reference_id "
        "WHERE refs.reference_public_id = ?",
        (redeemed.reference_public_id,),
    ).fetchone()
    if attempt is None:
        raise PostingAuthorityError("accepted Confirm has no durable posting attempt")
    _inject_failure("after_confirmation_commit")
    return resume_posting(
        conn, attempt_public_id=str(attempt["attempt_public_id"]), context=context
    )


def _advance_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    expected_stage: str,
    new_stage: str,
    transaction_public_id: str | None = None,
    evidence_public_id: str | None = None,
) -> None:
    if conn.in_transaction:
        raise PostingAuthorityError("attempt transition requires a connection without pending work")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT stage FROM d2_posting_attempts WHERE attempt_public_id = ?", (attempt_id,)
        ).fetchone()
        if current is None:
            raise PostingAuthorityError("posting attempt not found")
        if current["stage"] != expected_stage:
            conn.rollback()
            return
        _advance_attempt_in_transaction(
            conn,
            attempt_id=attempt_id,
            expected_stage=expected_stage,
            new_stage=new_stage,
            transaction_public_id=transaction_public_id,
            evidence_public_id=evidence_public_id,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _advance_attempt_in_transaction(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    expected_stage: str,
    new_stage: str,
    transaction_public_id: str | None,
    evidence_public_id: str | None,
) -> None:
    if not conn.in_transaction:
        raise PostingAuthorityError("attempt transition requires an owning transaction")
    row = conn.execute(
        "SELECT stage, row_version FROM d2_posting_attempts WHERE attempt_public_id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise PostingAuthorityError("posting attempt not found")
    if row["stage"] != expected_stage:
        raise PostingAuthorityError("posting attempt stage changed")
    version = int(row["row_version"]) + 1
    created_at = _now_text(_now_epoch())
    cursor = conn.execute(
        "UPDATE d2_posting_attempts SET stage = ?, row_version = ?, "
        "transaction_public_id = ?, attention_reason = NULL, "
        "stage_evidence_public_id = ?, updated_at = ? "
        "WHERE attempt_public_id = ? AND stage = ? AND row_version = ?",
        (
            new_stage,
            version,
            transaction_public_id,
            evidence_public_id,
            created_at,
            attempt_id,
            expected_stage,
            version - 1,
        ),
    )
    if cursor.rowcount != 1:
        raise PostingAuthorityError("posting attempt transition conflict")


def _mark_attempt_needs_attention(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    expected_stage: str,
    reason: str,
) -> None:
    if conn.in_transaction:
        raise PostingAuthorityError("attention transition requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT stage, row_version FROM d2_posting_attempts WHERE attempt_public_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise PostingAuthorityError("posting attempt not found")
        if row["stage"] != expected_stage:
            conn.rollback()
            return
        version = int(row["row_version"]) + 1
        created_at = _now_text(_now_epoch())
        cursor = conn.execute(
            "UPDATE d2_posting_attempts SET stage = 'needs_attention', row_version = ?, "
            "transaction_public_id = NULL, attention_reason = ?, "
            "stage_evidence_public_id = NULL, updated_at = ? "
            "WHERE attempt_public_id = ? AND stage = ? AND row_version = ?",
            (version, reason, created_at, attempt_id, expected_stage, version - 1),
        )
        if cursor.rowcount != 1:
            raise PostingAuthorityError("posting attention transition conflict")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _catch_up_finalized_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    context: HumanActionContext,
) -> PostingStatus:
    """Re-verify financial truth and catch up coordination under one write lock."""
    if conn.in_transaction:
        raise PostingAuthorityError("finalized catch-up requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _authorized_attempt_for_resume(
            conn, attempt_public_id=attempt_public_id, context=context
        )
        status = get_status(conn, review_public_id=str(row["review_public_id"]), context=context)
        if status.state != "finalized" or row["stage"] == "finalized":
            conn.rollback()
            return status
        expected_stage = (
            "accepted" if row["posting_path"] == "text" else "conditional_authorization_persisted"
        )
        if row["stage"] != expected_stage:
            raise PostingAuthorityError("finalized result has an invalid coordination stage")
        if row["posting_path"] == "text":
            evidence_public_id = status.transaction_public_id
        else:
            authorization = conn.execute(
                "SELECT proofs.authorization_id "
                "FROM d2_posting_decisions AS decisions "
                "JOIN d2_conditional_authorization_proofs AS proofs "
                "ON proofs.decision_public_id = decisions.decision_public_id "
                "WHERE decisions.attempt_public_id = ?",
                (attempt_public_id,),
            ).fetchone()
            if authorization is None:
                raise PostingAuthorityError("finalized receipt authority is unavailable")
            verified = verify_finalized_prepared_receipt(
                conn, str(authorization["authorization_id"])
            )
            if verified.transaction_public_id != status.transaction_public_id:
                raise PostingAuthorityError("finalized receipt result changed during catch-up")
            evidence_public_id = verified.finalization_public_id
        _advance_attempt_in_transaction(
            conn,
            attempt_id=attempt_public_id,
            expected_stage=expected_stage,
            new_stage="finalized",
            transaction_public_id=status.transaction_public_id,
            evidence_public_id=evidence_public_id,
        )
        verified_status = get_status(
            conn, review_public_id=str(row["review_public_id"]), context=context
        )
        if verified_status.state != "finalized":
            raise PostingAuthorityError("finalized coordination catch-up did not verify")
        conn.commit()
        return verified_status
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def resume_posting(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    context: HumanActionContext,
) -> PostingStatus:
    """Advance an already-authorized attempt; never creates a human decision."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    _require_context(context)
    row = _authorized_attempt_for_resume(conn, attempt_public_id=attempt_public_id, context=context)
    existing_status = get_status(
        conn, review_public_id=str(row["review_public_id"]), context=context
    )
    if existing_status.state == "finalized":
        _inject_failure("before_finalized_catchup_lock")
        return _catch_up_finalized_attempt(
            conn, attempt_public_id=attempt_public_id, context=context
        )
    if existing_status.state in {"needs_attention", "rejected"}:
        return existing_status
    if row["stage"] == "accepted" and row["posting_path"] == "text":
        convert_confirmed_parser_proposal(conn, int(row["parser_output_id"]))
        _inject_failure("after_text_finalization_commit")
        return _catch_up_finalized_attempt(
            conn, attempt_public_id=attempt_public_id, context=context
        )
    elif row["posting_path"] == "personal_receipt" and row["stage"] != "finalized":
        _resume_personal_receipt(conn, attempt_public_id=attempt_public_id, context=context)
    return get_status(conn, review_public_id=str(row["review_public_id"]), context=context)


def _resume_personal_receipt(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    context: HumanActionContext,
) -> None:
    """Advance the closed D2 personal-total receipt stage machine."""
    while True:
        row = conn.execute(
            """
            SELECT attempts.*, reviews.review_public_id, reviews.parser_output_id,
                   reviews.proposal_content_hash, reviews.visible_projection_json,
                   reviews.visible_projection_hash, reviews.receipt_fact_candidate_json,
                   reviews.authenticated_actor_id, proposals.public_id AS proposal_public_id,
                   decisions.decision_public_id
            FROM d2_posting_attempts AS attempts
            JOIN d2_posting_reviews AS reviews
              ON reviews.review_public_id = attempts.review_public_id
            JOIN parser_outputs AS proposals ON proposals.id = reviews.parser_output_id
            JOIN d2_posting_decisions AS decisions
              ON decisions.attempt_public_id = attempts.attempt_public_id
            WHERE attempts.attempt_public_id = ?
            """,
            (attempt_public_id,),
        ).fetchone()
        if row is None:
            raise PostingAuthorityError("receipt posting attempt not found")
        stage = str(row["stage"])
        if stage in {"finalized", "needs_attention"}:
            return
        candidate_value = canonical_json_value(
            str(row["receipt_fact_candidate_json"]), label="D2 receipt fact candidate"
        )
        if not isinstance(candidate_value, dict):
            raise PostingAuthorityError("D2 receipt fact candidate is malformed")
        candidate = candidate_value
        payer = str(candidate["payer_participant_public_id"])
        item = candidate["item"]
        if not isinstance(item, dict):
            raise PostingAuthorityError("D2 receipt fact candidate item is malformed")
        try:
            _require_active_self_participant(conn, payer)
        except PostingAuthorityError:
            _mark_attempt_needs_attention(
                conn,
                attempt_id=attempt_public_id,
                expected_stage=stage,
                reason="personal_participant_authority_changed",
            )
            raise

        conversion_command_id = f"rpfc_d2_{_sha256_text(attempt_public_id)[:24]}"
        if stage == "accepted":
            command = ReceiptFactsConversionCommand(
                command_public_id=conversion_command_id,
                proposal_public_id=str(row["proposal_public_id"]),
                expected_content_hash=str(row["proposal_content_hash"]),
                payer_participant_public_id=payer,
                participants=({"participant_public_id": payer, "is_included": 1},),
                authenticated_actor_id=str(row["authenticated_actor_id"]),
                channel="telegram",
                reason="D2 accepted personal-total receipt review",
            )

            def bind_conversion(locked: sqlite3.Connection, result: object) -> None:
                conversion_result = result
                durable = locked.execute(
                    "SELECT conversion_command_public_id, evidence_hash "
                    "FROM d2_posting_receipt_evidence "
                    "WHERE decision_public_id = ? AND evidence_type = 'conversion'",
                    (row["decision_public_id"],),
                ).fetchone()
                if durable is None:
                    locked.execute(
                        "INSERT INTO d2_posting_receipt_evidence "
                        "(decision_public_id, evidence_type, conversion_command_public_id, "
                        "evidence_hash, created_at) VALUES (?, 'conversion', ?, ?, ?)",
                        (
                            row["decision_public_id"],
                            getattr(conversion_result, "command_public_id"),
                            getattr(conversion_result, "conversion_result_hash"),
                            _now_text(_now_epoch()),
                        ),
                    )
                    durable = locked.execute(
                        "SELECT conversion_command_public_id, evidence_hash "
                        "FROM d2_posting_receipt_evidence "
                        "WHERE decision_public_id = ? AND evidence_type = 'conversion'",
                        (row["decision_public_id"],),
                    ).fetchone()
                if (
                    durable is None
                    or durable["conversion_command_public_id"]
                    != getattr(conversion_result, "command_public_id")
                    or durable["evidence_hash"]
                    != getattr(conversion_result, "conversion_result_hash")
                ):
                    raise PostingAuthorityError("D2 receipt conversion binding conflict")

            conversion = convert_confirmed_receipt_proposal_to_facts(
                conn, command, persistence_effect=bind_conversion
            )
            _inject_failure("after_conversion_commit")
            _advance_attempt(
                conn,
                attempt_id=attempt_public_id,
                expected_stage="accepted",
                new_stage="conversion_persisted",
                evidence_public_id=conversion.command_public_id,
            )
            continue

        conversion_row = conn.execute(
            "SELECT command_public_id, conversion_result_hash, receipts.public_id "
            "AS receipt_public_id FROM receipt_proposal_conversions AS conversions "
            "JOIN receipts ON receipts.id = conversions.receipt_id "
            "WHERE conversions.command_public_id = ?",
            (conversion_command_id,),
        ).fetchone()
        if conversion_row is None:
            raise PostingAuthorityError("D2 receipt conversion authority is missing")
        fact_command_id = f"riaf_d2_{_sha256_text(attempt_public_id)[:24]}"

        if stage == "conversion_persisted":
            amount = str(item["line_amount"])
            currency = str(item["currency"])
            fact_command = ReceiptItemAllocationFactsCommand(
                command_public_id=fact_command_id,
                receipt_public_id=str(conversion_row["receipt_public_id"]),
                expected_conversion_command_public_id=conversion_command_id,
                expected_conversion_result_hash=str(conversion_row["conversion_result_hash"]),
                expected_current_fact_set="none",
                items=(
                    {
                        "line_number": 1,
                        "item_name": str(item["item_name"]),
                        "line_amount": amount,
                        "currency": currency,
                    },
                ),
                allocations=(
                    {
                        "line_number": 1,
                        "allocation_method": "manual",
                        "participants": (
                            {
                                "participant_public_id": payer,
                                "share_amount": amount,
                                "currency": currency,
                            },
                        ),
                    },
                ),
                adjustments=(),
                authenticated_actor_id=str(row["authenticated_actor_id"]),
                channel="telegram",
                reason="D2 accepted personal-total receipt review",
            )

            def bind_fact_set(locked: sqlite3.Connection, result: object) -> None:
                fact_result = result
                durable = locked.execute(
                    "SELECT fact_set_public_id, evidence_hash "
                    "FROM d2_posting_receipt_evidence "
                    "WHERE decision_public_id = ? AND evidence_type = 'fact_set'",
                    (row["decision_public_id"],),
                ).fetchone()
                if durable is None:
                    locked.execute(
                        "INSERT INTO d2_posting_receipt_evidence "
                        "(decision_public_id, evidence_type, fact_set_public_id, "
                        "evidence_hash, created_at) VALUES (?, 'fact_set', ?, ?, ?)",
                        (
                            row["decision_public_id"],
                            getattr(fact_result, "fact_set_public_id"),
                            getattr(fact_result, "fact_set_result_hash"),
                            _now_text(_now_epoch()),
                        ),
                    )
                    durable = locked.execute(
                        "SELECT fact_set_public_id, evidence_hash "
                        "FROM d2_posting_receipt_evidence "
                        "WHERE decision_public_id = ? AND evidence_type = 'fact_set'",
                        (row["decision_public_id"],),
                    ).fetchone()
                if (
                    durable is None
                    or durable["fact_set_public_id"] != getattr(fact_result, "fact_set_public_id")
                    or durable["evidence_hash"] != getattr(fact_result, "fact_set_result_hash")
                ):
                    raise PostingAuthorityError("D2 receipt fact-set binding conflict")

            fact_set = persist_receipt_item_allocation_facts(
                conn, fact_command, persistence_effect=bind_fact_set
            )
            _inject_failure("after_fact_set_commit")
            _advance_attempt(
                conn,
                attempt_id=attempt_public_id,
                expected_stage="conversion_persisted",
                new_stage="fact_set_persisted",
                evidence_public_id=fact_set.fact_set_public_id,
            )
            continue

        prepared = prepare_receipt_calculation(
            conn,
            str(conversion_row["receipt_public_id"]),
            actor_type="system",
            actor_id="d2-posting-authority",
        )
        if stage == "fact_set_persisted":
            _inject_failure("after_snapshot_commit")
            _advance_attempt(
                conn,
                attempt_id=attempt_public_id,
                expected_stage="fact_set_persisted",
                new_stage="snapshot_persisted",
                evidence_public_id=prepared.calculation_snapshot_id,
            )
            continue

        actual_projection = build_d2_receipt_projection(
            merchant=prepared.confirmed_receipt_identity.merchant,
            receipt_date=prepared.confirmed_receipt_identity.receipt_date,
            currency=prepared.currency,
            payer_participant_public_id=payer,
            calculation=prepared.calculation_result,
        )
        actual_json = _canonical(actual_projection)
        actual_hash = _sha256_text(actual_json)
        if actual_json != str(row["visible_projection_json"]) or not hmac.compare_digest(
            actual_hash, str(row["visible_projection_hash"])
        ):
            _mark_attempt_needs_attention(
                conn,
                attempt_id=attempt_public_id,
                expected_stage=stage,
                reason="authoritative_projection_mismatch",
            )
            raise PostingAuthorityError(
                "authoritative receipt calculation does not equal the accepted review"
            )

        try:
            authorization = authorize_d2_conditional_receipt_finalization(
                conn,
                prepared,
                actor_id=str(row["authenticated_actor_id"]),
                decision_public_id=str(row["decision_public_id"]),
                review_public_id=str(row["review_public_id"]),
            )
        except BridgeAuthorizationConflictError:
            _mark_attempt_needs_attention(
                conn,
                attempt_id=attempt_public_id,
                expected_stage=stage,
                reason="conditional_authorization_conflict",
            )
            raise
        if stage == "snapshot_persisted":
            _inject_failure("after_conditional_authorization_commit")
            _advance_attempt(
                conn,
                attempt_id=attempt_public_id,
                expected_stage="snapshot_persisted",
                new_stage="conditional_authorization_persisted",
                evidence_public_id=authorization.authorization_id,
            )
            continue
        if stage == "conditional_authorization_persisted":
            finalize_prepared_receipt(conn, authorization)
            _inject_failure("after_receipt_finalization_commit")
            _catch_up_finalized_attempt(conn, attempt_public_id=attempt_public_id, context=context)
            return
        raise PostingAuthorityError(f"unsupported D2 receipt attempt stage: {stage}")


def get_status(
    conn: sqlite3.Connection,
    *,
    review_public_id: str,
    context: HumanActionContext,
) -> PostingStatus:
    """Return stable D2 status using SELECTs only."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    _require_context(context)
    delivery_is_valid = _review_has_valid_delivery_authority(conn, review_public_id)
    row = conn.execute(
        "SELECT reviews.review_public_id, attempts.attempt_public_id, attempts.stage, "
        "attempts.transaction_public_id, attempts.attention_reason, "
        "proposals.parse_status, activations.provider_message_id, "
        "supersessions.successor_review_public_id, "
        "conflicts.conflict_public_id, "
        "reviews.source_kind, EXISTS ("
        "SELECT 1 FROM parser_human_drafts AS active_drafts "
        "WHERE active_drafts.decision_target_parser_output_id = reviews.parser_output_id "
        "AND active_drafts.state = 'active') AS initial_replaced_by_edit, "
        "reviews.posting_path, reviews.parser_output_id, reviews.proposal_content_hash, "
        "reviews.authenticated_actor_id, reviews.visible_projection_json "
        "FROM d2_posting_reviews AS reviews "
        "JOIN parser_outputs AS proposals ON proposals.id = reviews.parser_output_id "
        "LEFT JOIN d2_posting_attempts AS attempts "
        "ON attempts.review_public_id = reviews.review_public_id "
        "LEFT JOIN d2_posting_review_delivery_activations AS activations "
        "ON activations.review_public_id = reviews.review_public_id "
        "LEFT JOIN d2_posting_review_supersessions AS supersessions "
        "ON supersessions.predecessor_review_public_id = reviews.review_public_id "
        "LEFT JOIN d2_posting_review_delivery_conflicts AS conflicts "
        "ON conflicts.review_public_id = reviews.review_public_id "
        "WHERE reviews.review_public_id = ? "
        "AND reviews.authenticated_actor_id = ? AND reviews.telegram_account_id = ? "
        "AND reviews.telegram_conversation_id = ? AND reviews.conversation_binding_id = ?",
        (
            review_public_id,
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
        ),
    ).fetchone()
    if row is None:
        raise PostingAuthorityError("posting authority unavailable")
    stage = row["stage"]
    attention_reason: str | None = None
    if stage is None and row["parse_status"] == "rejected":
        state = "rejected"
    elif (
        stage is None
        and delivery_is_valid
        and row["successor_review_public_id"] is None
        and row["conflict_public_id"] is None
        and not (
            row["source_kind"] == "initial_proposal_card"
            and int(row["initial_replaced_by_edit"]) == 1
        )
    ):
        state = "awaiting_confirmation"
    elif stage is None:
        state = "needs_attention"
        if row["successor_review_public_id"] is not None:
            attention_reason = "review_superseded"
        elif row["conflict_public_id"] is not None:
            attention_reason = "delivery_conflict"
        elif (
            row["source_kind"] == "initial_proposal_card"
            and int(row["initial_replaced_by_edit"]) == 1
        ):
            attention_reason = "review_stale_after_edit"
        else:
            attention_reason = "delivery_not_activated"
    else:
        try:
            _authorized_attempt_for_resume(
                conn, attempt_public_id=str(row["attempt_public_id"]), context=context
            )
        except PostingAuthorityError:
            return PostingStatus(
                review_public_id=review_public_id,
                state="needs_attention",
                attempt_public_id=str(row["attempt_public_id"]),
                transaction_public_id=None,
                attention_reason="posting_authority_incomplete",
            )
        decision = conn.execute(
            "SELECT decision_public_id, confirmation_public_id FROM d2_posting_decisions "
            "WHERE attempt_public_id = ? AND review_public_id = ?",
            (row["attempt_public_id"], review_public_id),
        ).fetchone()
        if decision is None:
            return PostingStatus(
                review_public_id=review_public_id,
                state="needs_attention",
                attempt_public_id=str(row["attempt_public_id"]),
                transaction_public_id=None,
                attention_reason="coordination_integrity_mismatch",
            )

        authoritative_transaction: str | None = None
        integrity_error = False
        if row["posting_path"] == "text":
            conversion = conn.execute(
                "SELECT conversions.confirmation_public_id, "
                "conversions.proposal_content_hash, conversions.authenticated_actor_id, "
                "transactions.public_id AS transaction_public_id "
                "FROM parser_proposal_conversion_audit AS conversions "
                "JOIN transactions ON transactions.id = conversions.transaction_id "
                "WHERE conversions.parser_output_id = ?",
                (row["parser_output_id"],),
            ).fetchone()
            if conversion is not None:
                try:
                    verified = verify_converted_parser_proposal(conn, int(row["parser_output_id"]))
                except Exception:
                    integrity_error = True
                else:
                    if (
                        conversion["confirmation_public_id"] != decision["confirmation_public_id"]
                        or conversion["proposal_content_hash"] != row["proposal_content_hash"]
                        or conversion["authenticated_actor_id"] != row["authenticated_actor_id"]
                        or verified["transaction_public_id"] != conversion["transaction_public_id"]
                    ):
                        integrity_error = True
                    else:
                        authoritative_transaction = str(conversion["transaction_public_id"])
        else:
            finalization = conn.execute(
                "SELECT authorizations.*, audits.status AS finalization_status, "
                "audits.transaction_public_id AS final_transaction_public_id, "
                "transactions.public_id AS canonical_transaction_public_id "
                "FROM d2_conditional_authorization_proofs AS proofs "
                "JOIN receipt_finalization_authorizations AS authorizations "
                "ON authorizations.authorization_id = proofs.authorization_id "
                "JOIN receipt_finalization_audit AS audits "
                "ON audits.authorization_id = authorizations.authorization_id "
                "JOIN transactions ON transactions.public_id = audits.transaction_public_id "
                "WHERE proofs.decision_public_id = ?",
                (decision["decision_public_id"],),
            ).fetchone()
            if finalization is not None:
                try:
                    require_d2_conditional_authority(conn, dict(finalization))
                    verified_receipt = verify_finalized_prepared_receipt(
                        conn, str(finalization["authorization_id"])
                    )
                except Exception:
                    integrity_error = True
                else:
                    if (
                        finalization["authorization_state"] != "consumed"
                        or finalization["authorization_version"] != "d2_conditional_v1"
                        or finalization["finalization_status"] != "finalized"
                        or finalization["final_transaction_public_id"]
                        != finalization["canonical_transaction_public_id"]
                        or verified_receipt.transaction_public_id
                        != finalization["canonical_transaction_public_id"]
                    ):
                        integrity_error = True
                    else:
                        authoritative_transaction = str(
                            finalization["canonical_transaction_public_id"]
                        )

        if authoritative_transaction is not None:
            if row["transaction_public_id"] not in {None, authoritative_transaction}:
                integrity_error = True
            else:
                # D2 verifies historical posting authority.  Once a correction
                # exists, its original projection must never be labelled current.
                from finance_core.application.correction_schema import has_committed_correction

                if has_committed_correction(conn, authoritative_transaction):
                    return PostingStatus(
                        review_public_id=review_public_id,
                        state="needs_attention",
                        attempt_public_id=str(row["attempt_public_id"]),
                        transaction_public_id=None,
                        attention_reason="local_current_lookup_required",
                    )
                transaction = conn.execute(
                    "SELECT amount, currency, transaction_date, merchant, account_id "
                    "FROM transactions WHERE public_id = ?",
                    (authoritative_transaction,),
                ).fetchone()
                try:
                    projection = canonical_json_value(
                        str(row["visible_projection_json"]),
                        label="D2 visible projection",
                    )
                    if not isinstance(projection, dict) or transaction is None:
                        raise ValueError("missing D2 result projection")
                    currency = normalize_currency(str(transaction["currency"] or ""))
                    amount = canonical_money_str(
                        money_decimal(str(transaction["amount"]), label="D2 result amount"),
                        currency,
                    )
                    transaction_date = str(transaction["transaction_date"] or "")[:10]
                    authoritative_merchant = (
                        None if transaction["merchant"] is None else str(transaction["merchant"])
                    )
                    if (
                        projection.get("amount") != amount
                        or projection.get("currency") != currency
                        or projection.get("transaction_date") != transaction_date
                        or projection.get("merchant") != authoritative_merchant
                        or projection.get("account") != "unspecified"
                        or transaction["account_id"] is not None
                    ):
                        raise ValueError("D2 result differs from confirmed projection")
                except (TypeError, ValueError, json.JSONDecodeError):
                    integrity_error = True
                else:
                    merchant = authoritative_merchant or ""
                    return PostingStatus(
                        review_public_id=review_public_id,
                        state="finalized",
                        attempt_public_id=str(row["attempt_public_id"]),
                        transaction_public_id=authoritative_transaction,
                        attention_reason=None,
                        amount=amount,
                        currency=currency,
                        transaction_date=transaction_date,
                        merchant=merchant,
                        account="unspecified",
                    )
        if integrity_error or stage == "finalized":
            return PostingStatus(
                review_public_id=review_public_id,
                state="needs_attention",
                attempt_public_id=str(row["attempt_public_id"]),
                transaction_public_id=None,
                attention_reason="financial_authority_mismatch",
            )
        state = "needs_attention" if stage == "needs_attention" else "posting"
    return PostingStatus(
        review_public_id=review_public_id,
        state=state,
        attempt_public_id=(
            None if row["attempt_public_id"] is None else str(row["attempt_public_id"])
        ),
        transaction_public_id=(
            None if row["transaction_public_id"] is None else str(row["transaction_public_id"])
        ),
        attention_reason=(
            attention_reason
            if attention_reason is not None
            else None
            if row["attention_reason"] is None
            else str(row["attention_reason"])
        ),
    )


def get_status_by_reference(
    conn: sqlite3.Connection,
    *,
    reference: str,
    context: HumanActionContext,
) -> PostingStatus:
    """Resolve a D2 review from its opaque Confirm capability using SELECTs only."""
    require_staging_database(conn)
    _require_d2_schema(conn)
    _require_context(context)
    if not isinstance(reference, str) or not reference.strip():
        raise PostingAuthorityError("posting authority unavailable")
    row = conn.execute(
        "SELECT bindings.review_public_id "
        "FROM openclaw_human_action_references AS refs "
        "JOIN d2_posting_review_action_bindings AS bindings "
        "ON bindings.reference_id = refs.id "
        "JOIN d2_posting_reviews AS reviews "
        "ON reviews.review_public_id = bindings.review_public_id "
        "WHERE refs.reference_sha256 = ? AND refs.action = 'confirm' "
        "AND refs.authenticated_actor_id = ? AND refs.channel_account_id = ? "
        "AND refs.channel_conversation_id = ? AND refs.conversation_binding_id = ? "
        "AND reviews.authenticated_actor_id = refs.authenticated_actor_id "
        "AND reviews.telegram_account_id = refs.channel_account_id "
        "AND reviews.telegram_conversation_id = refs.channel_conversation_id "
        "AND reviews.conversation_binding_id = refs.conversation_binding_id",
        (
            _sha256_text(reference),
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
        ),
    ).fetchone()
    if row is None:
        raise PostingAuthorityError("posting authority unavailable")
    return get_status(
        conn,
        review_public_id=str(row["review_public_id"]),
        context=context,
    )


__all__ = [
    "CALLBACK_VALUE_VERSION",
    "DELIVERY_MANIFEST_VERSION",
    "DELIVERY_MATERIAL_VERSION",
    "POSTING_REVIEW_SCHEMA_VERSION",
    "PostingAuthorityError",
    "PostingReviewControl",
    "PostingReviewDeliveryManifest",
    "PostingStatus",
    "PreparedPostingReview",
    "begin_posting_review_delivery",
    "confirm_and_post",
    "finance_delivery_material_digest",
    "get_status",
    "get_status_by_reference",
    "issue_posting_review_actions",
    "prepare_posting_review",
    "record_posting_review_delivery",
    "replace_posting_review_delivery",
    "resume_posting",
]
