"""Durable, direct-human OpenClaw action-reference boundary (S5c).

The Telegram callback carries only an opaque short reference.  SQLite binds
that reference to the exact proposal state, action, authenticated human,
private conversation, and core binding.  Redemption is an append-only,
one-reference/one-callback transaction.  The raw reference and callback ID
are never persisted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from finance_core.openclaw_staging_bridge import callback_tokens
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_drafts import HumanDraftDecisionBinding
from finance_core.parser_proposals.lifecycle import TERMINAL_STATUSES
from finance_core.parser_proposals.repository import ParserProposalRepository, row_to_dict

REFERENCE_PREFIX = "fha1_"
REFERENCE_BODY_LENGTH = 24
REFERENCE_ACTIONS = (
    callback_tokens.ACTION_CONFIRM,
    callback_tokens.ACTION_EDIT,
    callback_tokens.ACTION_REJECT,
)
REFERENCE_PURPOSES = frozenset(
    {
        "d2_post_v1",
        "d2_post_accepted_pre050_v1",
        "d2_post_fenced_pre050_v1",
        "edit_v1",
        "reject_v1",
        "manual_s5d_v1",
        "legacy_pre050_v1",
    }
)
DEFAULT_ACTION_PURPOSES = {
    callback_tokens.ACTION_CONFIRM: "manual_s5d_v1",
    callback_tokens.ACTION_EDIT: "edit_v1",
    callback_tokens.ACTION_REJECT: "reject_v1",
}


class HumanActionReferenceError(RuntimeError):
    """Fail-closed durable action-reference refusal with a stable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class HumanActionContext:
    actor_id: str
    account_id: str
    conversation_id: str
    binding_id: str


@dataclass(frozen=True)
class IssuedHumanActionReference:
    action: str
    reference: str
    expires_at: int


@dataclass(frozen=True)
class RedeemedHumanAction:
    reference_public_id: str
    action: str
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str
    callback_token: str
    callback_expiry: int
    actor_id: str
    d1_decision_binding: HumanDraftDecisionBinding | None
    idempotent_replay: bool


def _utc_text(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_material(*parts: str) -> bytes:
    encoded = (part.encode("utf-8") for part in parts)
    return b"".join(len(part).to_bytes(4, "big") + part for part in encoded)


def _reference_public_id(issuance_key: str, action: str) -> str:
    digest = hashlib.sha256(
        _encode_material("openclaw-human-action-public-v1", issuance_key, action)
    ).hexdigest()[:32]
    return f"haref_{digest}"


def _reference_value(
    key: bytes,
    *,
    reference_public_id: str,
    proposal_public_id: str,
    action: str,
    proposal_version: int,
    proposal_content_hash: str,
    context: HumanActionContext,
    expires_at: int,
    card_generation_public_id: str | None = None,
) -> str:
    if card_generation_public_id is None:
        material = _encode_material(
            "openclaw-human-action-reference-v1",
            reference_public_id,
            proposal_public_id,
            action,
            str(proposal_version),
            proposal_content_hash,
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            str(expires_at),
        )
    else:
        material = _encode_material(
            "openclaw-human-action-reference-d1-v1",
            reference_public_id,
            proposal_public_id,
            action,
            str(proposal_version),
            proposal_content_hash,
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            card_generation_public_id,
            str(expires_at),
        )
    body = base64.urlsafe_b64encode(hmac.new(key, material, hashlib.sha256).digest()).decode(
        "ascii"
    )[:REFERENCE_BODY_LENGTH]
    return f"{REFERENCE_PREFIX}{body}"


def reference_is_well_formed(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(REFERENCE_PREFIX):
        return False
    body = value[len(REFERENCE_PREFIX) :]
    return len(body) == REFERENCE_BODY_LENGTH and set(body) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def _begin_immediate(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise HumanActionReferenceError("transaction_conflict")
    conn.execute("BEGIN IMMEDIATE")


def _proposal_state(conn: sqlite3.Connection, proposal_id: int) -> tuple[dict, int, str]:
    proposal = ParserProposalRepository(conn).get(proposal_id)
    if proposal is None:
        raise HumanActionReferenceError("proposal_missing")
    _payload, _completion_id, version = resolve_effective_payload(conn, proposal)
    content_hash = compute_effective_proposal_content_hash(conn, {"id": proposal_id})
    return proposal, version, content_hash


def _reference_rows_for_issuance(conn: sqlite3.Connection, issuance_key: str) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT refs.*, proposals.public_id AS proposal_public_id,
               redemptions.id AS redemption_id,
               bindings.card_generation_public_id,
               purposes.purpose
        FROM openclaw_human_action_references AS refs
        JOIN parser_outputs AS proposals ON proposals.id = refs.parser_output_id
        LEFT JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
        LEFT JOIN parser_human_draft_action_bindings AS bindings
          ON bindings.reference_id = refs.id
        LEFT JOIN openclaw_human_action_reference_purposes AS purposes
          ON purposes.reference_id = refs.id
        WHERE refs.issuance_idempotency_key = ?
        ORDER BY refs.action
        """,
        (issuance_key,),
    )
    return [row_to_dict(row, cursor.description) for row in cursor.fetchall()]


def _row_context(row: dict) -> HumanActionContext:
    return HumanActionContext(
        actor_id=str(row["authenticated_actor_id"]),
        account_id=str(row["channel_account_id"]),
        conversation_id=str(row["channel_conversation_id"]),
        binding_id=str(row["conversation_binding_id"]),
    )


def _issued_from_row(row: dict, key: bytes) -> IssuedHumanActionReference:
    reference = _reference_value(
        key,
        reference_public_id=str(row["reference_public_id"]),
        proposal_public_id=str(row["proposal_public_id"]),
        action=str(row["action"]),
        proposal_version=int(row["proposal_version"]),
        proposal_content_hash=str(row["proposal_content_hash"]),
        context=_row_context(row),
        expires_at=int(row["expires_at"]),
        card_generation_public_id=row.get("card_generation_public_id"),
    )
    if not hmac.compare_digest(_sha256_text(reference), str(row["reference_sha256"])):
        raise HumanActionReferenceError("reference_integrity")
    return IssuedHumanActionReference(
        action=str(row["action"]), reference=reference, expires_at=int(row["expires_at"])
    )


def _issuance_rows_match(
    rows: list[dict],
    *,
    allowed_actions: tuple[str, ...],
    proposal_public_id: str,
    expected_proposal_version: int,
    expected_proposal_content_hash: str,
    context: HumanActionContext,
    ttl_seconds: int,
    action_purposes: Mapping[str, str],
    card_generation_public_id: str | None,
) -> bool:
    expected_actions = set(allowed_actions)
    return (
        len(rows) == len(expected_actions)
        and {str(row["action"]) for row in rows} == expected_actions
        and len({str(row["issuance_idempotency_key"]) for row in rows}) == 1
        and all(
            row["proposal_public_id"] == proposal_public_id
            and int(row["proposal_version"]) == expected_proposal_version
            and hmac.compare_digest(
                str(row["proposal_content_hash"]), expected_proposal_content_hash
            )
            and _row_context(row) == context
            and row["channel"] == "telegram"
            and int(row["ttl_seconds"]) == ttl_seconds
            and row["card_generation_public_id"] == card_generation_public_id
            and row["purpose"] == action_purposes[str(row["action"])]
            for row in rows
        )
    )


def _require_d1_card_for_issuance(
    conn: sqlite3.Connection,
    *,
    card_generation_public_id: str,
    proposal_public_id: str,
    expected_proposal_version: int,
    expected_proposal_content_hash: str,
    context: HumanActionContext,
    allowed_actions: tuple[str, ...],
    now: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT cards.*, drafts.state AS draft_state,
               drafts.expires_at AS draft_expires_at,
               drafts.current_card_generation_public_id,
               drafts.current_parser_output_id,
               drafts.current_proposal_version,
               drafts.current_proposal_content_hash,
               operations.result_completeness,
               proposals.public_id AS target_proposal_public_id
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
    if row is None:
        raise HumanActionReferenceError("generation_invalid")
    if (
        row["draft_state"] != "active"
        or row["current_card_generation_public_id"] != card_generation_public_id
        or row["target_proposal_public_id"] != proposal_public_id
        or int(row["decision_target_proposal_version"]) != expected_proposal_version
        or not hmac.compare_digest(
            str(row["decision_target_proposal_content_hash"]),
            expected_proposal_content_hash,
        )
        or row["authenticated_actor_id"] != context.actor_id
        or row["telegram_account_id"] != context.account_id
        or row["telegram_conversation_id"] != context.conversation_id
        or row["conversation_binding_id"] != context.binding_id
        or int(row["expires_at"]) <= now
        or int(row["draft_expires_at"]) <= now
    ):
        raise HumanActionReferenceError("generation_stale")
    if "confirm" in allowed_actions:
        publication = conn.execute(
            """
            SELECT 1 FROM parser_human_draft_publications
            WHERE draft_id = ? AND draft_version = ? AND parser_output_id = ?
              AND proposal_version = ? AND proposal_content_hash = ?
            """,
            (
                row["draft_id"],
                row["draft_version"],
                row["decision_target_parser_output_id"],
                row["decision_target_proposal_version"],
                row["decision_target_proposal_content_hash"],
            ),
        ).fetchone()
        if (
            row["result_completeness"] != "complete"
            or row["parser_output_id"] != row["decision_target_parser_output_id"]
            or row["current_parser_output_id"] != row["decision_target_parser_output_id"]
            or row["current_proposal_version"] != row["decision_target_proposal_version"]
            or row["current_proposal_content_hash"] != row["decision_target_proposal_content_hash"]
            or publication is None
        ):
            raise HumanActionReferenceError("action_unavailable")
    return row


def _require_d1_redemption_freshness(
    conn: sqlite3.Connection,
    *,
    row: dict,
    action: str,
    now: int,
) -> None:
    generation = row.get("card_generation_public_id")
    if generation is None:
        return
    terminal_replay = row.get("callback_id_sha256") is not None and (
        (action == "confirm" and row.get("draft_state") == "confirmed")
        or (action == "reject" and row.get("draft_state") == "rejected")
    )
    if (
        row.get("card_expires_at") is None
        or row.get("draft_expires_at") is None
        or int(row["card_expires_at"]) <= now
        or int(row["draft_expires_at"]) <= now
        or (row.get("draft_state") != "active" and not terminal_replay)
        or int(row["parser_output_id"]) != int(row["card_target_parser_output_id"])
        or int(row["proposal_version"]) != int(row["card_target_version"])
        or not hmac.compare_digest(str(row["proposal_content_hash"]), str(row["card_target_hash"]))
        or int(row["parser_output_id"]) != int(row["draft_target_parser_output_id"])
        or int(row["proposal_version"]) != int(row["draft_target_version"])
        or not hmac.compare_digest(str(row["proposal_content_hash"]), str(row["draft_target_hash"]))
    ):
        raise HumanActionReferenceError("generation_stale")
    if action in {"confirm", "edit"} and row["current_card_generation_public_id"] != generation:
        raise HumanActionReferenceError("generation_stale")
    if action == "confirm":
        publication = conn.execute(
            """
            SELECT 1 FROM parser_human_draft_publications AS publication
            JOIN parser_human_draft_cards AS card
              ON card.card_generation_public_id = ?
            WHERE publication.draft_id = card.draft_id
              AND publication.draft_version = card.draft_version
              AND publication.parser_output_id = card.decision_target_parser_output_id
              AND publication.proposal_version = card.decision_target_proposal_version
              AND publication.proposal_content_hash = card.decision_target_proposal_content_hash
            """,
            (generation,),
        ).fetchone()
        if (
            row.get("card_completeness") != "complete"
            or row.get("card_parser_output_id") != row.get("card_target_parser_output_id")
            or row.get("current_parser_output_id") != row.get("parser_output_id")
            or row.get("current_proposal_version") != row.get("proposal_version")
            or row.get("current_proposal_content_hash") != row.get("proposal_content_hash")
            or publication is None
        ):
            raise HumanActionReferenceError("generation_stale")


def issue_human_action_references(
    conn: sqlite3.Connection,
    *,
    key: bytes,
    issuance_idempotency_key: str,
    proposal_public_id: str,
    expected_proposal_version: int,
    expected_proposal_content_hash: str,
    context: HumanActionContext,
    ttl_seconds: int,
    minimum_remaining_seconds: int = 0,
    require_unconsumed_replay: bool = False,
    allowed_actions: tuple[str, ...] = REFERENCE_ACTIONS,
    card_generation_public_id: str | None = None,
    fallback_issuance_idempotency_keys: tuple[str, ...] = (),
    action_purposes: Mapping[str, str] | None = None,
    issuance_effect: Callable[
        [sqlite3.Connection, tuple[dict, ...], tuple[IssuedHumanActionReference, ...], int],
        None,
    ]
    | None = None,
    clock: Callable[[], int] = lambda: int(datetime.now(UTC).timestamp()),
) -> tuple[tuple[IssuedHumanActionReference, ...], bool]:
    """Issue or reconstruct the allowed direct-human references atomically."""
    if not allowed_actions or len(set(allowed_actions)) != len(allowed_actions):
        raise HumanActionReferenceError("issuance_conflict")
    if any(action not in REFERENCE_ACTIONS for action in allowed_actions):
        raise HumanActionReferenceError("issuance_conflict")
    purposes = (
        {action: DEFAULT_ACTION_PURPOSES[action] for action in allowed_actions}
        if action_purposes is None
        else dict(action_purposes)
    )
    if set(purposes) != set(allowed_actions) or any(
        purpose not in REFERENCE_PURPOSES for purpose in purposes.values()
    ):
        raise HumanActionReferenceError("issuance_conflict")
    if minimum_remaining_seconds < 0 or minimum_remaining_seconds >= ttl_seconds:
        raise HumanActionReferenceError("issuance_conflict")
    issuance_keys = (issuance_idempotency_key, *fallback_issuance_idempotency_keys)
    if len(issuance_keys) > 16 or len(set(issuance_keys)) != len(issuance_keys):
        raise HumanActionReferenceError("issuance_conflict")
    _begin_immediate(conn)
    try:
        now = clock()
        d1_card = None
        if card_generation_public_id is not None:
            d1_card = _require_d1_card_for_issuance(
                conn,
                card_generation_public_id=card_generation_public_id,
                proposal_public_id=proposal_public_id,
                expected_proposal_version=expected_proposal_version,
                expected_proposal_content_hash=expected_proposal_content_hash,
                context=context,
                allowed_actions=allowed_actions,
                now=now,
            )
        existing: list[dict] = []
        for candidate_key in issuance_keys:
            candidate_rows = _reference_rows_for_issuance(conn, candidate_key)
            if not candidate_rows:
                issuance_idempotency_key = candidate_key
                break
            if not _issuance_rows_match(
                candidate_rows,
                allowed_actions=allowed_actions,
                proposal_public_id=proposal_public_id,
                expected_proposal_version=expected_proposal_version,
                expected_proposal_content_hash=expected_proposal_content_hash,
                context=context,
                ttl_seconds=ttl_seconds,
                action_purposes=purposes,
                card_generation_public_id=card_generation_public_id,
            ):
                occupied_generations = {row["card_generation_public_id"] for row in candidate_rows}
                same_issuance_identity = (
                    card_generation_public_id is None and None in occupied_generations
                ) or (
                    card_generation_public_id is not None
                    and card_generation_public_id in occupied_generations
                )
                if same_issuance_identity:
                    raise HumanActionReferenceError("issuance_conflict")
                continue
            issuance_idempotency_key = candidate_key
            existing = candidate_rows
            break
        else:
            raise HumanActionReferenceError("issuance_conflict")
        if existing:
            current_proposal, version, content_hash = _proposal_state(
                conn, int(existing[0]["parser_output_id"])
            )
            if str(current_proposal["parse_status"]) in TERMINAL_STATUSES:
                raise HumanActionReferenceError("proposal_terminal")
            if version != expected_proposal_version:
                raise HumanActionReferenceError("stale_version")
            if not hmac.compare_digest(content_hash, expected_proposal_content_hash):
                raise HumanActionReferenceError("stale_content_hash")
            if require_unconsumed_replay and any(
                row["redemption_id"] is not None for row in existing
            ):
                raise HumanActionReferenceError("reference_consumed")
            if min(int(row["expires_at"]) for row in existing) <= (now + minimum_remaining_seconds):
                raise HumanActionReferenceError("reference_expiring")
            issued = tuple(_issued_from_row(row, key) for row in existing)
            if issuance_effect is not None:
                issuance_effect(conn, tuple(existing), issued, now)
            conn.commit()
            return issued, True

        proposal = ParserProposalRepository(conn).get_by_public_id(proposal_public_id)
        if proposal is None:
            raise HumanActionReferenceError("proposal_missing")
        _payload, _completion_id, version = resolve_effective_payload(conn, proposal)
        content_hash = compute_effective_proposal_content_hash(conn, {"id": proposal["id"]})
        if str(proposal["parse_status"]) in TERMINAL_STATUSES:
            raise HumanActionReferenceError("proposal_terminal")
        if version != expected_proposal_version:
            raise HumanActionReferenceError("stale_version")
        if not hmac.compare_digest(content_hash, expected_proposal_content_hash):
            raise HumanActionReferenceError("stale_content_hash")
        expires_at = now + ttl_seconds
        issued_at = _utc_text(now)
        result: list[IssuedHumanActionReference] = []
        for action in allowed_actions:
            public_id = _reference_public_id(issuance_idempotency_key, action)
            reference = _reference_value(
                key,
                reference_public_id=public_id,
                proposal_public_id=proposal_public_id,
                action=action,
                proposal_version=version,
                proposal_content_hash=content_hash,
                context=context,
                expires_at=expires_at,
                card_generation_public_id=card_generation_public_id,
            )
            conn.execute(
                """
                INSERT INTO openclaw_human_action_references (
                    reference_public_id, reference_sha256, issuance_idempotency_key,
                    parser_output_id, action, proposal_version, proposal_content_hash,
                    authenticated_actor_id, channel, channel_account_id,
                    channel_conversation_id, conversation_binding_id, ttl_seconds,
                    expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    _sha256_text(reference),
                    issuance_idempotency_key,
                    proposal["id"],
                    action,
                    version,
                    content_hash,
                    context.actor_id,
                    context.account_id,
                    context.conversation_id,
                    context.binding_id,
                    ttl_seconds,
                    expires_at,
                    issued_at,
                ),
            )
            if d1_card is not None:
                reference_id = int(
                    conn.execute(
                        "SELECT id FROM openclaw_human_action_references "
                        "WHERE reference_public_id = ?",
                        (public_id,),
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO parser_human_draft_action_bindings (
                        reference_id, card_generation_public_id, draft_id,
                        parser_output_id, proposal_version, proposal_content_hash,
                        authenticated_actor_id, telegram_account_id,
                        telegram_conversation_id, conversation_binding_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reference_id,
                        card_generation_public_id,
                        d1_card["draft_id"],
                        d1_card["decision_target_parser_output_id"],
                        d1_card["decision_target_proposal_version"],
                        d1_card["decision_target_proposal_content_hash"],
                        context.actor_id,
                        context.account_id,
                        context.conversation_id,
                        context.binding_id,
                        now,
                    ),
                )
            reference_id = int(
                conn.execute(
                    "SELECT id FROM openclaw_human_action_references WHERE reference_public_id = ?",
                    (public_id,),
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO openclaw_human_action_reference_purposes "
                "(reference_id, purpose, classified_at) VALUES (?, ?, ?)",
                (reference_id, purposes[action], issued_at),
            )
            result.append(
                IssuedHumanActionReference(
                    action=action, reference=reference, expires_at=expires_at
                )
            )
        if issuance_effect is not None:
            persisted = _reference_rows_for_issuance(conn, issuance_idempotency_key)
            issuance_effect(conn, tuple(persisted), tuple(result), now)
        conn.commit()
        return tuple(result), False
    except Exception:
        conn.rollback()
        raise


def _reference_row(conn: sqlite3.Connection, reference: str) -> dict | None:
    cursor = conn.execute(
        """
        SELECT refs.*, proposals.public_id AS proposal_public_id,
               redemptions.callback_id_sha256, redemptions.callback_message_id,
               purposes.purpose,
               bindings.card_generation_public_id,
               cards.expires_at AS card_expires_at,
               cards.parser_output_id AS card_parser_output_id,
               cards.decision_target_parser_output_id AS card_target_parser_output_id,
               cards.decision_target_proposal_version AS card_target_version,
               cards.decision_target_proposal_content_hash AS card_target_hash,
               operations.result_completeness AS card_completeness,
               drafts.state AS draft_state, drafts.expires_at AS draft_expires_at,
               drafts.current_card_generation_public_id,
               drafts.current_parser_output_id,
               drafts.current_proposal_version,
               drafts.current_proposal_content_hash,
               drafts.decision_target_parser_output_id AS draft_target_parser_output_id,
               drafts.decision_target_proposal_version AS draft_target_version,
               drafts.decision_target_proposal_content_hash AS draft_target_hash
        FROM openclaw_human_action_references AS refs
        JOIN parser_outputs AS proposals ON proposals.id = refs.parser_output_id
        LEFT JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
        LEFT JOIN openclaw_human_action_reference_purposes AS purposes
          ON purposes.reference_id = refs.id
        LEFT JOIN parser_human_draft_action_bindings AS bindings
          ON bindings.reference_id = refs.id
        LEFT JOIN parser_human_draft_cards AS cards
          ON cards.card_generation_public_id = bindings.card_generation_public_id
        LEFT JOIN parser_human_draft_operations AS operations
          ON operations.id = cards.original_operation_id
        LEFT JOIN parser_human_drafts AS drafts ON drafts.id = bindings.draft_id
        WHERE refs.reference_sha256 = ?
        """,
        (_sha256_text(reference),),
    )
    row = cursor.fetchone()
    return None if row is None else row_to_dict(row, cursor.description)


def _decision_material(row: dict, key: bytes, *, replay: bool) -> RedeemedHumanAction:
    generation = row.get("card_generation_public_id")
    binding = None
    if generation is not None:
        binding = HumanDraftDecisionBinding(
            reference_public_id=str(row["reference_public_id"]),
            card_generation_public_id=str(generation),
            authenticated_actor_id=str(row["authenticated_actor_id"]),
            telegram_account_id=str(row["channel_account_id"]),
            telegram_conversation_id=str(row["channel_conversation_id"]),
            conversation_binding_id=str(row["conversation_binding_id"]),
        )
    return RedeemedHumanAction(
        reference_public_id=str(row["reference_public_id"]),
        action=str(row["action"]),
        proposal_public_id=str(row["proposal_public_id"]),
        proposal_version=int(row["proposal_version"]),
        proposal_content_hash=str(row["proposal_content_hash"]),
        callback_token=callback_tokens.compute_token(
            key,
            proposal_public_id=str(row["proposal_public_id"]),
            version=int(row["proposal_version"]),
            content_hash=str(row["proposal_content_hash"]),
            action=str(row["action"]),
            expiry=int(row["expires_at"]),
        ),
        callback_expiry=int(row["expires_at"]),
        actor_id=str(row["authenticated_actor_id"]),
        d1_decision_binding=binding,
        idempotent_replay=replay,
    )


def load_redeemed_human_action_binding(
    conn: sqlite3.Connection,
    *,
    reference_public_id: str,
    action: str,
    proposal_public_id: str,
    proposal_version: int,
    proposal_content_hash: str,
    context: HumanActionContext,
) -> HumanDraftDecisionBinding:
    """Reload one redeemed D1 authority from append-only persisted evidence."""
    cursor = conn.execute(
        """
        SELECT refs.action, refs.proposal_version, refs.proposal_content_hash,
               refs.authenticated_actor_id, refs.channel_account_id,
               refs.channel_conversation_id, refs.conversation_binding_id,
               proposals.public_id AS proposal_public_id,
               bindings.card_generation_public_id,
               redemptions.id AS redemption_id
        FROM openclaw_human_action_references AS refs
        JOIN parser_outputs AS proposals ON proposals.id = refs.parser_output_id
        LEFT JOIN openclaw_human_action_redemptions AS redemptions
          ON redemptions.reference_id = refs.id
        LEFT JOIN parser_human_draft_action_bindings AS bindings
          ON bindings.reference_id = refs.id
        WHERE refs.reference_public_id = ?
        """,
        (reference_public_id,),
    )
    row = cursor.fetchone()
    material = None if row is None else row_to_dict(row, cursor.description)
    if (
        material is None
        or material["redemption_id"] is None
        or material["card_generation_public_id"] is None
        or material["action"] != action
        or material["proposal_public_id"] != proposal_public_id
        or int(material["proposal_version"]) != proposal_version
        or material["proposal_content_hash"] != proposal_content_hash
        or _row_context(material) != context
    ):
        raise HumanActionReferenceError("decision_binding_invalid")
    return HumanDraftDecisionBinding(
        reference_public_id=reference_public_id,
        card_generation_public_id=str(material["card_generation_public_id"]),
        authenticated_actor_id=context.actor_id,
        telegram_account_id=context.account_id,
        telegram_conversation_id=context.conversation_id,
        conversation_binding_id=context.binding_id,
    )


def redeem_human_action_reference(
    conn: sqlite3.Connection,
    *,
    key: bytes,
    reference: str,
    action: str,
    context: HumanActionContext,
    callback_id: str,
    callback_message_id: int,
    required_purpose: str | None = None,
    action_validator: Callable[[sqlite3.Connection, dict, str], None] | None = None,
    redemption_effect: Callable[[sqlite3.Connection, dict, str, int], None] | None = None,
    clock: Callable[[], int] = lambda: int(datetime.now(UTC).timestamp()),
) -> RedeemedHumanAction:
    """Atomically redeem one reference, allowing only the same callback replay."""
    if not reference_is_well_formed(reference):
        raise HumanActionReferenceError("reference_invalid")
    callback_hash = _sha256_text(callback_id)
    _begin_immediate(conn)
    try:
        now = clock()
        row = _reference_row(conn, reference)
        if row is None:
            raise HumanActionReferenceError("reference_invalid")
        if str(row["action"]) != action:
            raise HumanActionReferenceError("wrong_action")
        purpose = row.get("purpose")
        if purpose not in REFERENCE_PURPOSES:
            raise HumanActionReferenceError("reference_purpose_invalid")
        if required_purpose is not None:
            if required_purpose not in REFERENCE_PURPOSES or purpose != required_purpose:
                raise HumanActionReferenceError("reference_purpose_mismatch")
        elif purpose in {
            "d2_post_v1",
            "d2_post_accepted_pre050_v1",
            "d2_post_fenced_pre050_v1",
        }:
            raise HumanActionReferenceError("reference_requires_d2_redemption")
        if _row_context(row) != context or row["channel"] != "telegram":
            raise HumanActionReferenceError("actor_or_context_mismatch")
        if int(row["expires_at"]) <= now:
            raise HumanActionReferenceError("reference_expired")
        _require_d1_redemption_freshness(conn, row=row, action=action, now=now)

        expected_reference = _reference_value(
            key,
            reference_public_id=str(row["reference_public_id"]),
            proposal_public_id=str(row["proposal_public_id"]),
            action=str(row["action"]),
            proposal_version=int(row["proposal_version"]),
            proposal_content_hash=str(row["proposal_content_hash"]),
            context=context,
            expires_at=int(row["expires_at"]),
            card_generation_public_id=row.get("card_generation_public_id"),
        )
        if not hmac.compare_digest(reference, expected_reference):
            raise HumanActionReferenceError("reference_integrity")

        if row["callback_id_sha256"] is not None:
            if (
                not hmac.compare_digest(str(row["callback_id_sha256"]), callback_hash)
                or int(row["callback_message_id"]) != callback_message_id
            ):
                raise HumanActionReferenceError("reference_replayed")
            if redemption_effect is not None:
                redemption_effect(conn, row, action, now)
            result = _decision_material(row, key, replay=True)
            conn.commit()
            return result

        callback_reuse = conn.execute(
            "SELECT 1 FROM openclaw_human_action_redemptions WHERE callback_id_sha256 = ?",
            (callback_hash,),
        ).fetchone()
        if callback_reuse is not None:
            raise HumanActionReferenceError("reference_replayed")

        proposal, version, content_hash = _proposal_state(conn, int(row["parser_output_id"]))
        if str(proposal["parse_status"]) in TERMINAL_STATUSES:
            raise HumanActionReferenceError("proposal_terminal")
        if version != int(row["proposal_version"]):
            raise HumanActionReferenceError("stale_version")
        if not hmac.compare_digest(content_hash, str(row["proposal_content_hash"])):
            raise HumanActionReferenceError("stale_content_hash")
        if action_validator is not None:
            action_validator(conn, row, action)
        conn.execute(
            """
            INSERT INTO openclaw_human_action_redemptions (
                reference_id, callback_id_sha256, callback_message_id, redeemed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (row["id"], callback_hash, callback_message_id, _utc_text(now)),
        )
        if redemption_effect is not None:
            redemption_effect(conn, row, action, now)
        result = _decision_material(row, key, replay=False)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


__all__ = [
    "HumanActionContext",
    "HumanActionReferenceError",
    "IssuedHumanActionReference",
    "REFERENCE_ACTIONS",
    "REFERENCE_PURPOSES",
    "REFERENCE_PREFIX",
    "RedeemedHumanAction",
    "issue_human_action_references",
    "load_redeemed_human_action_binding",
    "redeem_human_action_reference",
    "reference_is_well_formed",
]
