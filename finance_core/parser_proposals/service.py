"""Service-owned units of work for parser confirmation and conversion."""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from finance_core.parser_proposals import decision_owner
from finance_core.parser_proposals.decision_owner import (
    CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX as CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX,
)
from finance_core.parser_proposals.decision_owner import (
    SIMPLE_EXPENSE_TYPES as SIMPLE_EXPENSE_TYPES,
)
from finance_core.parser_proposals.decision_owner import (
    AlreadyConvertedProposalError as AlreadyConvertedProposalError,
)
from finance_core.parser_proposals.decision_owner import (
    CanonicalTransactionRepository as CanonicalTransactionRepository,
)
from finance_core.parser_proposals.decision_owner import (
    DecisionAuthority as DecisionAuthority,
)
from finance_core.parser_proposals.decision_owner import (
    InvalidProposalStatusError as InvalidProposalStatusError,
)
from finance_core.parser_proposals.decision_owner import (
    MissingConfirmationRecordError as MissingConfirmationRecordError,
)
from finance_core.parser_proposals.decision_owner import (
    MissingRequiredTransactionFieldError as MissingRequiredTransactionFieldError,
)
from finance_core.parser_proposals.decision_owner import (
    ParserConfirmationError as ParserConfirmationError,
)
from finance_core.parser_proposals.decision_owner import (
    ProposalConversionError as ProposalConversionError,
)
from finance_core.parser_proposals.decision_owner import (
    StaleProposalConfirmationError as StaleProposalConfirmationError,
)
from finance_core.parser_proposals.decision_owner import (
    StaleProposalDecisionStateError as StaleProposalDecisionStateError,
)
from finance_core.parser_proposals.decision_owner import (
    UnauthorizedConfirmationActorError as UnauthorizedConfirmationActorError,
)
from finance_core.parser_proposals.decision_owner import (
    UnsupportedProposalTypeError as UnsupportedProposalTypeError,
)
from finance_core.parser_proposals.decision_owner import (
    _append_parser_conversion_audit as _append_parser_conversion_audit,
)
from finance_core.parser_proposals.decision_owner import (
    _append_parser_decision_audit as _append_parser_decision_audit,
)
from finance_core.parser_proposals.decision_owner import (
    _begin_immediate as _begin_immediate,
)
from finance_core.parser_proposals.decision_owner import (
    _converted_transaction_public_id as _converted_transaction_public_id,
)
from finance_core.parser_proposals.decision_owner import (
    _effective_transaction_payload as _effective_transaction_payload,
)
from finance_core.parser_proposals.decision_owner import (
    _epoch_from_iso as _epoch_from_iso,
)
from finance_core.parser_proposals.decision_owner import (
    _existing_confirmation_result as _existing_confirmation_result,
)
from finance_core.parser_proposals.decision_owner import (
    _now as _now,
)
from finance_core.parser_proposals.decision_owner import (
    _optional_text as _optional_text,
)
from finance_core.parser_proposals.decision_owner import (
    _parser_source_references as _parser_source_references,
)
from finance_core.parser_proposals.decision_owner import (
    _require_active_authorization as _require_active_authorization,
)
from finance_core.parser_proposals.decision_owner import (
    _require_exact_transaction_money as _require_exact_transaction_money,
)
from finance_core.parser_proposals.decision_owner import (
    _require_existing_conversion_truth as _require_existing_conversion_truth,
)
from finance_core.parser_proposals.decision_owner import (
    _require_proposal as _require_proposal,
)
from finance_core.parser_proposals.decision_owner import (
    _required_canonical_money as _required_canonical_money,
)
from finance_core.parser_proposals.decision_owner import (
    _required_date as _required_date,
)
from finance_core.parser_proposals.decision_owner import (
    _rollback_if_needed as _rollback_if_needed,
)
from finance_core.parser_proposals.decision_owner import (
    _source_channel as _source_channel,
)
from finance_core.parser_proposals.decision_owner import (
    _source_evidence as _source_evidence,
)
from finance_core.parser_proposals.decision_owner import (
    _validate_human_command as _validate_human_command,
)
from finance_core.parser_proposals.decision_owner import (
    _verify_persisted_transaction_money as _verify_persisted_transaction_money,
)
from finance_core.parser_proposals.decision_owner import (
    convert_confirmed_parser_proposal as _convert_confirmed_parser_proposal_owner,
)
from finance_core.parser_proposals.decision_owner import (
    resolve_simple_expense_conversion_fields as resolve_simple_expense_conversion_fields,
)
from finance_core.parser_proposals.decision_owner import (
    verify_converted_parser_proposal as verify_converted_parser_proposal,
)
from finance_core.parser_proposals.human_drafts import (
    HumanDraftDecisionBinding,
    HumanDraftError,
    confirm_active_human_draft_in_transaction,
    reject_active_human_draft_in_transaction,
    require_current_human_draft_publication_in_transaction,
    require_human_draft_reject_capability_in_transaction,
)
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    require_telegram_source_context,
)


@dataclass(frozen=True)
class InitialProposalDecisionBinding:
    """Exact activated D2 initial-card authority for one proposal decision."""

    review_public_id: str
    reference_public_id: str
    authenticated_actor_id: str
    telegram_account_id: str
    telegram_conversation_id: str
    conversation_binding_id: str


def _require_current_initial_proposal_review_in_transaction(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_binding: InitialProposalDecisionBinding,
    now_epoch: int,
) -> None:
    if not conn.in_transaction:
        raise ParserConfirmationError("initial D2 decision requires an owning transaction")
    row = conn.execute(
        """
        SELECT reviews.parser_output_id, reviews.proposal_version,
               reviews.proposal_content_hash, reviews.expires_at,
               cards.raw_intake_record_id, cards.admitted_source_message_id,
               cards.admitted_source_identity_sha256,
               cards.authenticated_actor_id, cards.telegram_account_id,
               cards.telegram_conversation_id, cards.conversation_binding_id,
               cards.expires_at AS card_expires_at,
               refs.reference_public_id, refs.authenticated_actor_id AS ref_actor_id,
               refs.channel_account_id, refs.channel_conversation_id,
               refs.conversation_binding_id AS ref_binding_id,
               purposes.purpose, redemptions.callback_message_id,
               activations.provider_message_id,
               supersessions.successor_review_public_id
        FROM d2_posting_reviews AS reviews
        JOIN d2_initial_proposal_cards AS cards
          ON cards.initial_card_public_id = reviews.initial_card_public_id
        JOIN d2_posting_review_action_bindings AS bindings
          ON bindings.review_public_id = reviews.review_public_id
        JOIN openclaw_human_action_references AS refs
          ON refs.id = bindings.reference_id
        JOIN openclaw_human_action_reference_purposes AS purposes
          ON purposes.reference_id = refs.id
        JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
        JOIN d2_posting_review_delivery_activations AS activations
          ON activations.review_public_id = reviews.review_public_id
        LEFT JOIN d2_posting_review_supersessions AS supersessions
          ON supersessions.predecessor_review_public_id = reviews.review_public_id
        WHERE reviews.review_public_id = ?
          AND reviews.source_kind = 'initial_proposal_card'
          AND refs.reference_public_id = ?
          AND refs.action = 'confirm'
        """,
        (decision_binding.review_public_id, decision_binding.reference_public_id),
    ).fetchone()
    if row is None:
        raise ParserConfirmationError("initial D2 decision authority is unavailable")
    raw = conn.execute(
        "SELECT parser_output_id, source_message_id, external_source_id, source_channel, "
        "status FROM raw_intake_records WHERE id = ?",
        (row["raw_intake_record_id"],),
    ).fetchone()
    active_draft = conn.execute(
        "SELECT 1 FROM parser_human_drafts "
        "WHERE decision_target_parser_output_id = ? AND state = 'active' LIMIT 1",
        (parser_output_id,),
    ).fetchone()
    expected_context = (
        decision_binding.authenticated_actor_id,
        decision_binding.telegram_account_id,
        decision_binding.telegram_conversation_id,
        decision_binding.conversation_binding_id,
    )
    expected_source_identity = (
        f"telegram:{decision_binding.telegram_conversation_id}:{row['admitted_source_message_id']}"
    )
    source_context_digest: str | None = None
    try:
        source_context_digest = require_telegram_source_context(
            conn,
            raw_intake_record_id=int(row["raw_intake_record_id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=decision_binding.authenticated_actor_id,
                account_id=decision_binding.telegram_account_id,
                conversation_id=decision_binding.telegram_conversation_id,
                binding_id=decision_binding.conversation_binding_id,
                message_id=str(row["admitted_source_message_id"]),
            ),
        )
    except TelegramSourceContextError:
        source_context_digest = None
    if (
        int(row["parser_output_id"]) != parser_output_id
        or authenticated_actor_id != decision_binding.authenticated_actor_id
        or tuple(
            row[name]
            for name in (
                "authenticated_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
            )
        )
        != expected_context
        or tuple(
            row[name]
            for name in (
                "ref_actor_id",
                "channel_account_id",
                "channel_conversation_id",
                "ref_binding_id",
            )
        )
        != expected_context
        or row["purpose"] != "d2_post_v1"
        or int(row["callback_message_id"]) != int(row["provider_message_id"])
        or row["successor_review_public_id"] is not None
        or int(row["expires_at"]) <= now_epoch
        or int(row["card_expires_at"]) <= now_epoch
        or raw is None
        or int(raw["parser_output_id"]) != parser_output_id
        or str(raw["source_message_id"]) != str(row["admitted_source_message_id"])
        or raw["source_channel"] != "telegram"
        or str(raw["external_source_id"] or "") != expected_source_identity
        or source_context_digest is None
        or not hmac.compare_digest(
            str(row["admitted_source_identity_sha256"]),
            source_context_digest,
        )
        or raw["status"] != "parsed_pending_confirmation"
        or active_draft is not None
    ):
        raise ParserConfirmationError("initial D2 decision authority is stale")


class _LegacyDecisionAuthority:
    def __init__(self, parser_output_id, actor_id, decision, d1_binding, initial_binding):
        self.parser_output_id = parser_output_id
        self.actor_id = actor_id
        self.decision = decision
        self.d1_binding = d1_binding
        self.initial_binding = initial_binding
        self.decision_epoch = 0

    def verify_in_transaction(
        self,
        connection,
        *,
        proposal,
        content_hash,
        proposal_version,
        authenticated_actor_id,
        decision,
        decision_epoch,
    ):
        from finance_core.parser_proposals.amendment_lineage import (
            AmendmentLineageError,
            refuse_legacy_amendment,
        )

        try:
            refuse_legacy_amendment(connection, proposal)
        except AmendmentLineageError as exc:
            raise ParserConfirmationError(str(exc)) from exc
        self.decision_epoch = decision_epoch
        if self.d1_binding is not None and self.initial_binding is not None:
            raise ParserConfirmationError("proposal decision has conflicting authority sources")
        if decision == "confirmed":
            if self.initial_binding is not None:
                _require_current_initial_proposal_review_in_transaction(
                    connection,
                    parser_output_id=self.parser_output_id,
                    authenticated_actor_id=self.actor_id,
                    decision_binding=self.initial_binding,
                    now_epoch=decision_epoch,
                )
            else:
                require_current_human_draft_publication_in_transaction(
                    connection,
                    parser_output_id=self.parser_output_id,
                    authenticated_actor_id=self.actor_id,
                    decision_binding=self.d1_binding,
                    now_epoch=decision_epoch,
                )
        elif self.d1_binding is not None:
            require_human_draft_reject_capability_in_transaction(
                connection,
                parser_output_id=self.parser_output_id,
                authenticated_actor_id=self.actor_id,
                decision_binding=self.d1_binding,
                now_epoch=decision_epoch,
            )

    def persist_effect_in_transaction(self, connection, *, confirmation_public_id):
        if self.decision == "confirmed" and self.d1_binding is not None:
            confirm_active_human_draft_in_transaction(
                connection,
                parser_output_id=self.parser_output_id,
                authenticated_actor_id=self.actor_id,
                decision_public_id=confirmation_public_id,
                decision_binding=self.d1_binding,
                now_epoch=self.decision_epoch,
            )
        elif self.decision == "rejected":
            reject_active_human_draft_in_transaction(
                connection,
                parser_output_id=self.parser_output_id,
                authenticated_actor_id=self.actor_id,
                decision_public_id=confirmation_public_id,
                decision_binding=self.d1_binding,
                now_epoch=self.decision_epoch,
            )


def confirm_parser_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    authenticated_actor_id: str,
    actor_type: str = "human",
    decision: str = "confirmed",
    confirmation_channel: str = "cli",
    reason: str | None = None,
    confirmation_public_id: str | None = None,
    expected_content_hash: str | None = None,
    expected_version: int | None = None,
    d1_decision_binding: HumanDraftDecisionBinding | None = None,
    initial_d2_decision_binding: InitialProposalDecisionBinding | None = None,
    clock: Callable[[], str] | None = None,
    _caller_owns_transaction: bool = False,
) -> dict[str, Any]:
    """Preserve the existing legacy authority before neutral owning writes."""
    try:
        return decision_owner.confirm_parser_proposal(
            conn,
            parser_output_id,
            authenticated_actor_id=authenticated_actor_id,
            actor_type=actor_type,
            decision=decision,
            confirmation_channel=confirmation_channel,
            reason=reason,
            confirmation_public_id=confirmation_public_id,
            expected_content_hash=expected_content_hash,
            expected_version=expected_version,
            clock=lambda: _now(clock),
            _caller_owns_transaction=_caller_owns_transaction,
            decision_authority=_LegacyDecisionAuthority(
                parser_output_id,
                authenticated_actor_id,
                decision,
                d1_decision_binding,
                initial_d2_decision_binding,
            ),
        )
    except HumanDraftError as exc:
        raise ParserConfirmationError(str(exc)) from exc


def convert_confirmed_parser_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Compatibility facade retains legacy authority isolation."""
    decision_owner._require_legacy_conversion_target(conn, parser_output_id)
    return _convert_confirmed_parser_proposal_owner(
        conn, parser_output_id, persistence_effect=persistence_effect
    )
