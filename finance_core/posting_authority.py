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
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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
    convert_confirmed_receipt_proposal_to_facts,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
    persist_receipt_item_allocation_facts,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.parser_proposals.service import (
    confirm_parser_proposal,
    convert_confirmed_parser_proposal,
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

POSTING_REVIEW_SCHEMA_VERSION = "d2-posting-review-v1"
_failure_injection_hook: Callable[[str], None] | None = None
_D2_TABLES = frozenset(
    {
        "d2_posting_reviews",
        "d2_posting_review_action_bindings",
        "d2_posting_attempts",
        "d2_posting_decisions",
        "d2_posting_receipt_evidence",
        "d2_conditional_authorization_proofs",
        "d2_posting_attempt_events",
    }
)
_D2_SCHEMA_FINGERPRINT = "a8d6d11e9bdf65b09691a23df69eabcd8c2f63427f0b4245d7bffa3f4d95cdb2"


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
        raise PostingAuthorityError("migration 049 is missing or incomplete")
    schema_rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE (name GLOB 'd2_*' OR name GLOB 'trg_d2_*') "
        "AND type IN ('table', 'trigger', 'index') ORDER BY type, name"
    ).fetchall()
    material = json.dumps(
        [tuple(row) for row in schema_rows],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if not hmac.compare_digest(hashlib.sha256(material).hexdigest(), _D2_SCHEMA_FINGERPRINT):
        raise PostingAuthorityError("migration 049 is missing or incomplete")


@dataclass(frozen=True)
class PreparedPostingReview:
    review_public_id: str
    card_generation_public_id: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    posting_path: str
    visible_projection: Mapping[str, object]
    visible_projection_hash: str
    expires_at: int
    idempotent: bool


@dataclass(frozen=True)
class PostingStatus:
    review_public_id: str
    state: str
    attempt_public_id: str | None
    transaction_public_id: str | None
    attention_reason: str | None


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def _now_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return canonical_json_text(value)


def _identity(prefix: str, *parts: object) -> str:
    framed = "".join(f"{len(str(part))}:{part}" for part in parts)
    return f"{prefix}_{_sha256_text(framed)[:30]}"


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


def prepare_posting_review(
    conn: sqlite3.Connection,
    *,
    review_idempotency_key: str,
    card_generation_public_id: str,
    context: HumanActionContext,
    receipt_payer_participant_public_id: str | None = None,
    clock: Callable[[], int] = _now_epoch,
) -> PreparedPostingReview:
    """Persist an immutable review projection without creating posting authority."""
    require_staging_database(conn)
    require_foreign_keys_enabled(conn)
    _require_d2_schema(conn)
    _require_context(context)
    if not review_idempotency_key.strip():
        raise PostingAuthorityError("review_idempotency_key must not be empty")
    if conn.in_transaction:
        raise PostingAuthorityError("posting review requires a connection without pending work")

    conn.execute("BEGIN IMMEDIATE")
    try:
        now = clock()
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
        payload, _completion_id, version = resolve_effective_payload(conn, proposal)
        content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
        if version != int(card["decision_target_proposal_version"]) or not hmac.compare_digest(
            content_hash, str(card["decision_target_proposal_content_hash"])
        ):
            raise PostingAuthorityError("proposal_state_stale")

        fields = json.loads(str(card["field_values_json"]))
        currency = normalize_currency(str(fields.get("currency", "")))
        amount = canonical_money_str(
            money_decimal(str(fields.get("amount", "")), label="D2 reviewed amount"),
            currency,
        )
        base_projection: dict[str, object] = {
            "amount": amount,
            "currency": currency,
            "transaction_date": fields.get("transaction_date", ""),
            "merchant": fields.get("merchant", ""),
            "account": "unspecified",
        }
        is_receipt = (
            conn.execute(
                "SELECT 1 FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
                (parser_output_id,),
            ).fetchone()
            is not None
        )
        candidate: dict[str, object] | None = None
        if is_receipt:
            if (
                not receipt_payer_participant_public_id
                or not receipt_payer_participant_public_id.strip()
            ):
                raise PostingAuthorityError(
                    "personal receipt requires the authenticated payer participant"
                )
            posting_path = "personal_receipt"
            payer = receipt_payer_participant_public_id
            _require_active_self_participant(conn, payer)
            candidate = {
                "payer_participant_public_id": payer,
                "item": {
                    "line_number": 1,
                    "item_name": fields.get("merchant") or "Receipt total",
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
                                    "description": fields.get("merchant") or "Receipt total",
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
        else:
            posting_path = "text"
            projection = base_projection

        projection_json = _canonical(projection)
        projection_hash = _sha256_text(projection_json)
        expires_at = min(int(card["expires_at"]), int(card["draft_expires_at"]))
        review_public_id = _identity("d2rev", review_idempotency_key)
        created_at = _now_text(now)
        collision = conn.execute(
            "SELECT review_public_id FROM d2_posting_reviews "
            "WHERE review_public_id = ? OR review_idempotency_key = ? "
            "OR card_generation_public_id = ?",
            (review_public_id, review_idempotency_key, card_generation_public_id),
        ).fetchone()
        idempotent = collision is not None
        if collision is not None and collision["review_public_id"] != review_public_id:
            raise PostingAuthorityError("posting review identity collision")
        if collision is None:
            conn.execute(
                """
                INSERT INTO d2_posting_reviews (
                    review_public_id, review_idempotency_key, card_generation_public_id,
                    parser_output_id, proposal_version, proposal_content_hash, posting_path,
                    authenticated_actor_id, telegram_account_id, telegram_conversation_id,
                    conversation_binding_id, visible_projection_json, visible_projection_hash,
                    receipt_fact_candidate_json, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_public_id,
                    review_idempotency_key,
                    card_generation_public_id,
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
            card_generation_public_id,
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
                "card_generation_public_id",
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
            card_generation_public_id=card_generation_public_id,
            proposal_public_id=str(card["proposal_public_id"]),
            proposal_version=version,
            proposal_content_hash=content_hash,
            posting_path=posting_path,
            visible_projection=projection,
            visible_projection_hash=projection_hash,
            expires_at=expires_at,
            idempotent=idempotent,
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def issue_posting_review_actions(
    conn: sqlite3.Connection,
    *,
    review_public_id: str,
    key: bytes,
    context: HumanActionContext,
    clock: Callable[[], int] = _now_epoch,
) -> tuple[IssuedHumanActionReference, bool]:
    """Issue exactly one Confirm reference and bind it before returning the raw capability."""
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
    remaining = int(review["expires_at"]) - now
    if remaining < 60:
        raise PostingAuthorityError("posting review expired or too close to expiry")

    def bind(locked: sqlite3.Connection, rows: tuple[dict, ...], bound_at: int) -> None:
        if len(rows) != 1 or rows[0]["action"] != "confirm":
            raise PostingAuthorityError("posting review must bind exactly one Confirm reference")
        collisions = locked.execute(
            "SELECT review_public_id, reference_id FROM d2_posting_review_action_bindings "
            "WHERE review_public_id = ? OR reference_id = ?",
            (review_public_id, rows[0]["id"]),
        ).fetchall()
        if not collisions:
            locked.execute(
                "INSERT INTO d2_posting_review_action_bindings "
                "(review_public_id, reference_id, bound_at) VALUES (?, ?, ?)",
                (review_public_id, rows[0]["id"], _now_text(bound_at)),
            )
            collisions = locked.execute(
                "SELECT review_public_id, reference_id "
                "FROM d2_posting_review_action_bindings WHERE review_public_id = ?",
                (review_public_id,),
            ).fetchall()
        if len(collisions) != 1 or (
            str(collisions[0]["review_public_id"]) != review_public_id
            or int(collisions[0]["reference_id"]) != int(rows[0]["id"])
        ):
            raise PostingAuthorityError("posting review action binding conflict")

    issued, replay = issue_human_action_references(
        conn,
        key=key,
        issuance_idempotency_key=(
            f"bridge-human-action-issue:{_sha256_text(review_public_id)[:32]}"
        ),
        proposal_public_id=str(review["proposal_public_id"]),
        expected_proposal_version=int(review["proposal_version"]),
        expected_proposal_content_hash=str(review["proposal_content_hash"]),
        context=context,
        ttl_seconds=min(3600, remaining),
        allowed_actions=("confirm",),
        card_generation_public_id=str(review["card_generation_public_id"]),
        issuance_effect=bind,
        clock=clock,
    )
    return issued[0], replay


def _accepted_attempt_for_callback(
    conn: sqlite3.Connection,
    *,
    reference: str,
    context: HumanActionContext,
    callback_id: str,
    callback_message_id: int,
) -> str | None:
    """Authenticate an exact accepted replay without reapplying expiry checks."""
    row = conn.execute(
        """
        SELECT attempts.attempt_public_id, refs.reference_sha256,
               refs.authenticated_actor_id, refs.channel_account_id,
               refs.channel_conversation_id, refs.conversation_binding_id,
               redemptions.callback_id_sha256, redemptions.callback_message_id
        FROM openclaw_human_action_references AS refs
        JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
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
    return str(row["attempt_public_id"])


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
               decisions.decision_public_id, decisions.confirmation_public_id
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
        return resume_posting(conn, attempt_public_id=accepted, context=context)

    def validate(locked: sqlite3.Connection, ref_row: dict, action: str) -> None:
        binding = locked.execute(
            """
            SELECT reviews.* FROM d2_posting_review_action_bindings AS bindings
            JOIN d2_posting_reviews AS reviews
              ON reviews.review_public_id = bindings.review_public_id
            WHERE bindings.reference_id = ?
            """,
            (ref_row["id"],),
        ).fetchone()
        if binding is None or action != "confirm":
            raise PostingAuthorityError("Confirm reference is not bound to a D2 posting review")
        if int(binding["expires_at"]) <= clock():
            raise PostingAuthorityError("posting review expired")
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
        d1_binding = HumanDraftDecisionBinding(
            reference_public_id=str(ref_row["reference_public_id"]),
            card_generation_public_id=str(review["card_generation_public_id"]),
            authenticated_actor_id=context.actor_id,
            telegram_account_id=context.account_id,
            telegram_conversation_id=context.conversation_id,
            conversation_binding_id=context.binding_id,
        )
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
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


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
    if row["stage"] == "accepted" and row["posting_path"] == "text":
        result = convert_confirmed_parser_proposal(conn, int(row["parser_output_id"]))
        _inject_failure("after_text_finalization_commit")
        _advance_attempt(
            conn,
            attempt_id=attempt_public_id,
            expected_stage="accepted",
            new_stage="finalized",
            transaction_public_id=str(result["transaction_public_id"]),
            evidence_public_id=str(result["transaction_public_id"]),
        )
    elif row["posting_path"] == "personal_receipt" and row["stage"] != "finalized":
        _resume_personal_receipt(conn, attempt_public_id=attempt_public_id)
    return get_status(conn, review_public_id=str(row["review_public_id"]), context=context)


def _resume_personal_receipt(conn: sqlite3.Connection, *, attempt_public_id: str) -> None:
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
            result = finalize_prepared_receipt(conn, authorization)
            _inject_failure("after_receipt_finalization_commit")
            _advance_attempt(
                conn,
                attempt_id=attempt_public_id,
                expected_stage="conditional_authorization_persisted",
                new_stage="finalized",
                transaction_public_id=result.transaction_public_id,
                evidence_public_id=result.finalization_public_id,
            )
            continue
        raise PostingAuthorityError(f"unsupported D2 receipt attempt stage: {stage}")


def get_status(
    conn: sqlite3.Connection,
    *,
    review_public_id: str,
    context: HumanActionContext,
) -> PostingStatus:
    """Return stable D2 status using SELECTs only."""
    require_staging_database(conn)
    _require_d2_schema(conn)
    _require_context(context)
    row = conn.execute(
        "SELECT reviews.review_public_id, attempts.attempt_public_id, attempts.stage, "
        "attempts.transaction_public_id, attempts.attention_reason, drafts.state AS draft_state, "
        "reviews.posting_path, reviews.parser_output_id, reviews.proposal_content_hash, "
        "reviews.authenticated_actor_id "
        "FROM d2_posting_reviews AS reviews "
        "JOIN parser_human_draft_cards AS cards "
        "ON cards.card_generation_public_id = reviews.card_generation_public_id "
        "JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id "
        "LEFT JOIN d2_posting_attempts AS attempts "
        "ON attempts.review_public_id = reviews.review_public_id "
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
    if stage is None and row["draft_state"] == "rejected":
        state = "rejected"
    elif stage is None:
        state = "awaiting_confirmation"
    else:
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
                return PostingStatus(
                    review_public_id=review_public_id,
                    state="finalized",
                    attempt_public_id=str(row["attempt_public_id"]),
                    transaction_public_id=authoritative_transaction,
                    attention_reason=None,
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
        attention_reason=None if row["attention_reason"] is None else str(row["attention_reason"]),
    )


__all__ = [
    "POSTING_REVIEW_SCHEMA_VERSION",
    "PostingAuthorityError",
    "PostingStatus",
    "PreparedPostingReview",
    "confirm_and_post",
    "get_status",
    "issue_posting_review_actions",
    "prepare_posting_review",
    "resume_posting",
]
