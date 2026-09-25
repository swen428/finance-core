"""Append-only D1 human draft repository.

This module owns SQLite transaction and exact human-reply evidence boundaries.
It creates proposal drafts only; it never confirms, converts, dispatches, calls
a provider, or creates final financial facts.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from finance_core.parser_proposals.content_hash import (
    _ocr_link_evidence,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.lifecycle import TERMINAL_STATUSES
from finance_core.parser_proposals.repository import ParserProposalRepository

_FIELDS = ("amount", "currency", "transaction_date", "merchant", "description", "category")
_LABELS = {
    "金额": "amount",
    "amount": "amount",
    "币种": "currency",
    "currency": "currency",
    "日期": "transaction_date",
    "date": "transaction_date",
    "商户": "merchant",
    "merchant": "merchant",
    "描述": "description",
    "description": "description",
    "分类": "category",
    "category": "category",
}
_REFERENCE_LABELS = frozenset({"资料卡编号", "card ref"})
_HEX = frozenset("0123456789abcdef")
_REASON_POLICY_VERSION = "d1-reason-policy-v1"


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


class HumanDraftError(RuntimeError):
    """Fail-closed D1 repository error with a stable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _HumanDraftRefusal(RuntimeError):
    """Internal typed signal for a deterministic, evidence-retaining refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ValidatedHumanDraft(Protocol):
    @property
    def canonical_payload(self) -> Mapping[str, object]: ...

    @property
    def changed_fields(self) -> tuple[str, ...]: ...

    @property
    def completeness(self) -> str: ...

    @property
    def reason_contributors(self) -> tuple[HumanReasonContributor, ...]: ...

    @property
    def unresolved_flags(self) -> tuple[str, ...]: ...

    @property
    def explicit_clears(self) -> Mapping[str, tuple[object, object]]: ...


@dataclass(frozen=True)
class HumanReasonContributor:
    contributor_id: str
    reason_code: str
    origin_kind: str
    source_evidence_public_id: str | None
    source_evidence_hash: str | None
    flags: tuple[str, ...]
    affected_fields: tuple[str, ...]
    resolution_policy: str
    resolved_by_operation_id: str | None
    resolved_fields: tuple[str, ...]
    resolution_before_after: dict[str, tuple[object, object]]


@dataclass(frozen=True)
class HumanDraftContext:
    authenticated_actor_id: str
    telegram_account_id: str
    telegram_conversation_id: str
    conversation_binding_id: str


@dataclass(frozen=True)
class HumanDraftCommand:
    card_generation_public_id: str
    telegram_message_id: int
    operation_public_id: str
    authenticated_actor_id: str
    telegram_account_id: str
    telegram_conversation_id: str
    conversation_binding_id: str
    raw_card_text: str
    field_values: dict[str, str]


@dataclass(frozen=True)
class PublishedDraft:
    parser_output_id: int
    proposal_public_id: str
    proposal_version: int
    proposal_content_hash: str


@dataclass(frozen=True)
class HumanDraftResult:
    draft_public_id: str
    draft_version: int
    draft_content_hash: str
    completeness: str
    proposal_public_id: str | None
    proposal_version: int | None
    proposal_content_hash: str | None
    card_generation_public_id: str
    current_card_generation_public_id: str
    operation_outcome: str
    refusal_code: str | None
    decision_target_proposal_public_id: str
    decision_target_proposal_version: int
    decision_target_proposal_content_hash: str
    field_values: dict[str, str]
    reason_contributors: tuple[HumanReasonContributor, ...]
    unresolved_flags: tuple[str, ...]
    human_reply_evidence_public_id: str | None
    delivery_state: str
    delivery_state_hash: str
    delivery_attempts: tuple[dict[str, object], ...]
    delivery_outcomes: tuple[dict[str, object], ...]
    action_issue_batch_id: str
    action_issuance_state: str
    idempotent_replay: bool


@dataclass(frozen=True)
class HumanDraftDecisionBinding:
    reference_public_id: str
    card_generation_public_id: str
    authenticated_actor_id: str
    telegram_account_id: str
    telegram_conversation_id: str
    conversation_binding_id: str


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HumanDraftError("canonical_json_invalid") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _encode_material(*parts: str) -> bytes:
    return b"".join(
        len(encoded).to_bytes(4, "big") + encoded
        for encoded in (part.encode("utf-8") for part in parts)
    )


def _prefixed_id(prefix: str, domain: str, *parts: str) -> str:
    digest = _sha256_bytes(_encode_material(domain, *parts))[:32]
    return f"{prefix}{digest}"


def _context_from_row(row: Any) -> HumanDraftContext:
    return HumanDraftContext(
        str(row["authenticated_actor_id"]),
        str(row["telegram_account_id"]),
        str(row["telegram_conversation_id"]),
        str(row["conversation_binding_id"]),
    )


def _command_context(command: HumanDraftCommand) -> HumanDraftContext:
    return HumanDraftContext(
        command.authenticated_actor_id,
        command.telegram_account_id,
        command.telegram_conversation_id,
        command.conversation_binding_id,
    )


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise HumanDraftError("transaction_required")


def _d1_decision_lineage_schema_available(conn: sqlite3.Connection, parser_output_id: int) -> bool:
    # Local import avoids the module cycle: human_revision publishes PublishedDraft.
    from finance_core.parser_proposals.human_revision import (
        HumanRevisionLineageError,
        d1_publication_schema_available,
    )

    try:
        return d1_publication_schema_available(conn, parser_output_id)
    except HumanRevisionLineageError as exc:
        raise HumanDraftError("decision_lineage_schema_invalid") from exc


def _begin_immediate(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise HumanDraftError("transaction_conflict")
    conn.execute("BEGIN IMMEDIATE")


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _field_values(payload: Mapping[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in _FIELDS:
        value = payload.get(field)
        if field == "transaction_date" and value is None:
            value = payload.get("date")
        values[field] = "" if value is None else str(value)
    return values


def _contributors_material(
    contributors: tuple[HumanReasonContributor, ...],
) -> tuple[str, str]:
    ordered: list[dict[str, object]] = []
    for contributor in sorted(contributors, key=lambda item: item.contributor_id):
        material = asdict(contributor)
        material["flags"] = sorted(contributor.flags)
        material["affected_fields"] = sorted(contributor.affected_fields)
        material["resolved_fields"] = sorted(contributor.resolved_fields)
        ordered.append(material)
    canonical = _canonical_json(ordered)
    return canonical, _sha256_text(canonical)


def _contributors_from_json(value: str) -> tuple[HumanReasonContributor, ...]:
    loaded = json.loads(value)
    result: list[HumanReasonContributor] = []
    for item in loaded:
        before_after = {
            key: (pair[0], pair[1]) for key, pair in item["resolution_before_after"].items()
        }
        result.append(
            HumanReasonContributor(
                contributor_id=item["contributor_id"],
                reason_code=item["reason_code"],
                origin_kind=item["origin_kind"],
                source_evidence_public_id=item["source_evidence_public_id"],
                source_evidence_hash=item["source_evidence_hash"],
                flags=tuple(item["flags"]),
                affected_fields=tuple(item["affected_fields"]),
                resolution_policy=item["resolution_policy"],
                resolved_by_operation_id=item["resolved_by_operation_id"],
                resolved_fields=tuple(item["resolved_fields"]),
                resolution_before_after=before_after,
            )
        )
    return tuple(result)


def _flags_material(flags: tuple[str, ...]) -> tuple[str, str]:
    canonical = _canonical_json(sorted(flags))
    return canonical, _sha256_text(canonical)


def _draft_hash(
    payload: Mapping[str, object], contributors: tuple[HumanReasonContributor, ...]
) -> str:
    contributors_json, _ = _contributors_material(contributors)
    return _sha256_text(
        _canonical_json(
            {
                "payload": dict(payload),
                "reason_contributors": json.loads(contributors_json),
                "reason_policy_version": _REASON_POLICY_VERSION,
            }
        )
    )


def _validated_completeness(
    payload: dict[str, object],
    *,
    source_type: str,
    contributors: tuple[HumanReasonContributor, ...],
) -> str:
    try:
        validated = _validate_human_draft_adapter(
            payload,
            {},
            source_type=source_type,
            reason_contributors=contributors,
            operation_public_id="d1-initial-completeness",
        )
    except _HumanDraftRefusal:
        return "incomplete"
    if validated.completeness not in {"incomplete", "publishable"}:
        raise HumanDraftError("validation_completeness_invalid")
    return "complete" if validated.completeness == "publishable" else "incomplete"


def _result_completeness(conn: sqlite3.Connection, draft: sqlite3.Row) -> str:
    row = conn.execute(
        """
        SELECT operations.result_completeness
        FROM parser_human_draft_cards AS cards
        JOIN parser_human_draft_operations AS operations
          ON operations.id = cards.original_operation_id
        WHERE cards.card_generation_public_id = ? AND cards.draft_id = ?
        """,
        (draft["current_card_generation_public_id"], draft["id"]),
    ).fetchone()
    if row is None or row["result_completeness"] not in {"complete", "incomplete"}:
        raise HumanDraftError("draft_completeness_integrity")
    return str(row["result_completeness"])


def _read_reference(conn: sqlite3.Connection, reference_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT refs.*, proposals.public_id AS proposal_public_id
        FROM openclaw_human_action_references AS refs
        JOIN parser_outputs AS proposals ON proposals.id = refs.parser_output_id
        WHERE refs.id = ?
        """,
        (reference_id,),
    ).fetchone()


def _same_locked_reference(
    locked: sqlite3.Row | Mapping[str, Any],
    current: sqlite3.Row | Mapping[str, Any],
) -> bool:
    keys = (
        "id",
        "reference_public_id",
        "reference_sha256",
        "parser_output_id",
        "action",
        "proposal_version",
        "proposal_content_hash",
        "authenticated_actor_id",
        "channel",
        "channel_account_id",
        "channel_conversation_id",
        "conversation_binding_id",
        "expires_at",
    )
    return all(locked[key] == current[key] for key in keys)


def _verified_ai_observations(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
    *,
    content_hash: str,
    proposal_version: int,
) -> dict[str, tuple[str, str]] | None:
    """Return only field conflicts admitted by the sealed AI lineage verifier."""
    from finance_core.parser_proposals.ai_fallback import verify_ai_fallback_child

    verified = verify_ai_fallback_child(
        conn,
        proposal,
        content_hash=content_hash,
        proposal_version=proposal_version,
        require_resolved=False,
    )
    if verified is None:
        return None
    row = conn.execute(
        """
        WITH RECURSIVE lineage(id, parent_parser_output_id, depth) AS (
            SELECT id, parent_parser_output_id, 0
            FROM parser_outputs WHERE id = ?
            UNION ALL
            SELECT parent.id, parent.parent_parser_output_id, child.depth + 1
            FROM parser_outputs AS parent
            JOIN lineage AS child ON child.parent_parser_output_id = parent.id
        )
        SELECT results.result_public_id, results.result_material_hash,
               results.response_blob
        FROM lineage
        JOIN ai_fallback_proposal_links AS links ON links.parser_output_id = lineage.id
        JOIN ai_fallback_results AS results ON results.id = links.result_id
        ORDER BY lineage.depth
        LIMIT 1
        """,
        (proposal["id"],),
    ).fetchone()
    if row is None or not isinstance(row["response_blob"], bytes):
        raise HumanDraftError("ai_observation_evidence")
    try:
        response = json.loads(bytes(row["response_blob"]))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HumanDraftError("ai_observation_evidence") from exc
    conflicts = response.get("field_conflicts") if isinstance(response, dict) else None
    if not isinstance(conflicts, dict):
        return {}
    reason_for_field = {
        "amount": "ambiguous_amount",
        "currency": "ambiguous_currency",
        "transaction_date": "ambiguous_date",
        "merchant": "ambiguous_merchant",
    }
    evidence = (str(row["result_public_id"]), str(row["result_material_hash"]))
    return {
        reason_for_field[field]: evidence
        for field, references in conflicts.items()
        if field in reason_for_field and isinstance(references, list) and references
    }


def begin_human_draft_in_transaction(
    conn: sqlite3.Connection,
    *,
    locked_edit_reference_row: sqlite3.Row | Mapping[str, Any],
    source_edit_reference_id: int,
    reference_public_id: str,
    reference_integrity_material: bytes,
    callback_message_id: int,
    redemption_public_id: str,
    redemption_material_hash: str,
    now_epoch: int,
) -> HumanDraftResult:
    """Create G0 only as an internal effect of the caller's locked redemption UOW."""
    _require_transaction(conn)
    if not isinstance(reference_integrity_material, bytes):
        raise HumanDraftError("reference_integrity")
    if callback_message_id <= 0 or now_epoch <= 0 or not redemption_public_id:
        raise HumanDraftError("start_material_invalid")
    if not _valid_hash(redemption_material_hash):
        raise HumanDraftError("start_material_invalid")
    current = _read_reference(conn, source_edit_reference_id)
    if current is None or not _same_locked_reference(locked_edit_reference_row, current):
        raise HumanDraftError("reference_lock_mismatch")
    if current["reference_public_id"] != reference_public_id:
        raise HumanDraftError("reference_identity_mismatch")
    if current["action"] != "edit":
        raise HumanDraftError("wrong_action")
    if current["channel"] != "telegram":
        raise HumanDraftError("actor_or_context_mismatch")
    if int(current["expires_at"]) <= now_epoch:
        raise HumanDraftError("reference_expired")
    if not hmac.compare_digest(
        str(current["reference_sha256"]), _sha256_bytes(reference_integrity_material)
    ):
        raise HumanDraftError("reference_integrity")

    existing = conn.execute(
        "SELECT * FROM parser_human_drafts WHERE source_edit_reference_id = ?",
        (source_edit_reference_id,),
    ).fetchone()
    if existing is not None:
        operation = conn.execute(
            "SELECT * FROM parser_human_draft_operations "
            "WHERE draft_id = ? AND operation_type = 'start'",
            (existing["id"],),
        ).fetchone()
        if (
            existing["source_reference_public_id"] != reference_public_id
            or existing["start_redemption_public_id"] != redemption_public_id
            or not hmac.compare_digest(
                str(existing["start_redemption_material_hash"]), redemption_material_hash
            )
            or operation is None
            or int(operation["telegram_message_id"]) != callback_message_id
        ):
            raise HumanDraftError("start_conflict")
        return replace(_result_for(conn, existing, operation), idempotent_replay=True)

    redemption = conn.execute(
        "SELECT * FROM openclaw_human_action_redemptions WHERE reference_id = ?",
        (source_edit_reference_id,),
    ).fetchone()
    if redemption is None:
        raise HumanDraftError("redemption_required")
    if int(redemption["callback_message_id"]) != callback_message_id or not hmac.compare_digest(
        str(redemption["callback_id_sha256"]), redemption_material_hash
    ):
        raise HumanDraftError("redemption_conflict")

    active = conn.execute(
        """
        SELECT 1 FROM parser_human_drafts
        WHERE source_parser_output_id = ? AND authenticated_actor_id = ?
          AND telegram_account_id = ? AND telegram_conversation_id = ?
          AND conversation_binding_id = ? AND state = 'active'
        """,
        (
            current["parser_output_id"],
            current["authenticated_actor_id"],
            current["channel_account_id"],
            current["channel_conversation_id"],
            current["conversation_binding_id"],
        ),
    ).fetchone()
    if active is not None:
        raise HumanDraftError("active_draft_exists")

    proposal = ParserProposalRepository(conn).get(int(current["parser_output_id"]))
    if proposal is None:
        raise HumanDraftError("proposal_missing")
    if str(proposal["parse_status"]) in TERMINAL_STATUSES:
        raise HumanDraftError("proposal_terminal")
    payload, _completion_id, version = resolve_effective_payload(conn, proposal)
    content_hash = compute_effective_proposal_content_hash(conn, proposal)
    if version != int(current["proposal_version"]):
        raise HumanDraftError("stale_version")
    if not hmac.compare_digest(content_hash, str(current["proposal_content_hash"])):
        raise HumanDraftError("stale_content_hash")
    if (
        conn.execute(
            "SELECT 1 FROM parser_proposal_conversion_audit WHERE parser_output_id = ?",
            (proposal["id"],),
        ).fetchone()
        is not None
        or conn.execute(
            "SELECT 1 FROM receipt_proposal_conversions WHERE parser_output_id = ?",
            (proposal["id"],),
        ).fetchone()
        is not None
    ):
        raise HumanDraftError("proposal_converted")

    from finance_core.parser_proposals.human_draft_validation import initial_reason_contributors

    contributors = initial_reason_contributors(
        payload,
        source_type=str(proposal["source_type"]),
        verified_ocr_evidence=_ocr_link_evidence(conn, proposal),
        verified_ai_observations=_verified_ai_observations(
            conn,
            proposal,
            content_hash=content_hash,
            proposal_version=version,
        ),
    )
    contributors_json, contributors_hash = _contributors_material(contributors)
    initial_flags = tuple(
        sorted(
            {
                flag
                for contributor in contributors
                if contributor.resolved_by_operation_id is None
                for flag in contributor.flags
            }
        )
    )
    flags_json, flags_hash = _flags_material(initial_flags)
    initial_completeness = _validated_completeness(
        payload,
        source_type=str(proposal["source_type"]),
        contributors=contributors,
    )
    draft_hash = _draft_hash(payload, contributors)
    draft_public_id = _prefixed_id("d1draft_", "d1-human-draft-v1", reference_public_id)
    card_public_id = _prefixed_id(
        "d1card_", "d1-card-generation-v1", draft_public_id, redemption_public_id, "0"
    )
    action_batch_id = _sha256_bytes(_encode_material("d1-card-action-issue-v1", card_public_id))
    payload_json = _canonical_json(payload)
    field_values_json = _canonical_json(_field_values(payload))
    raw_intake = conn.execute(
        "SELECT id FROM raw_intake_records WHERE parser_output_id = ? ORDER BY id DESC LIMIT 1",
        (proposal["id"],),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO parser_human_drafts (
            draft_public_id, source_parser_output_id, source_raw_intake_id,
            source_edit_reference_id, source_reference_public_id,
            start_redemption_public_id, start_redemption_material_hash,
            current_draft_version, current_draft_content_hash, current_payload_json,
            field_values_json, reason_policy_version, reason_contributors_json,
            reason_contributors_hash, unresolved_flags_json, unresolved_flags_hash,
            current_parser_output_id, current_proposal_version,
            current_proposal_content_hash, decision_target_parser_output_id,
            decision_target_proposal_version, decision_target_proposal_content_hash,
            current_card_generation_public_id, authenticated_actor_id,
            telegram_account_id, telegram_conversation_id, conversation_binding_id,
            state, expires_at, last_claimed_message_id, last_claimed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                  ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            draft_public_id,
            proposal["id"],
            None if raw_intake is None else raw_intake["id"],
            source_edit_reference_id,
            reference_public_id,
            redemption_public_id,
            redemption_material_hash,
            draft_hash,
            payload_json,
            field_values_json,
            _REASON_POLICY_VERSION,
            contributors_json,
            contributors_hash,
            flags_json,
            flags_hash,
            proposal["id"],
            version,
            content_hash,
            card_public_id,
            current["authenticated_actor_id"],
            current["channel_account_id"],
            current["channel_conversation_id"],
            current["conversation_binding_id"],
            current["expires_at"],
            callback_message_id,
            now_epoch,
            now_epoch,
            now_epoch,
        ),
    )
    draft = conn.execute(
        "SELECT * FROM parser_human_drafts WHERE draft_public_id = ?", (draft_public_id,)
    ).fetchone()
    assert draft is not None
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome, result_completeness,
            telegram_message_id, request_material_hash, action_reference_id,
            before_draft_version, before_draft_content_hash, after_draft_version,
            after_draft_content_hash, canonical_supplied_fields_json,
            material_changes_json, explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, refusal_code, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, 'start', 'started', ?, ?, ?, ?, 0, ?, 0, ?, '{}', '{}', '[]',
                  ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 'telegram', ?)
        """,
        (
            redemption_public_id,
            draft["id"],
            initial_completeness,
            callback_message_id,
            redemption_material_hash,
            source_edit_reference_id,
            draft_hash,
            draft_hash,
            _REASON_POLICY_VERSION,
            contributors_json,
            contributors_hash,
            contributors_json,
            contributors_hash,
            flags_json,
            card_public_id,
            current["authenticated_actor_id"],
            current["channel_account_id"],
            current["channel_conversation_id"],
            current["conversation_binding_id"],
            now_epoch,
        ),
    )
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (redemption_public_id,),
    ).fetchone()
    assert operation is not None
    conn.execute(
        """
        INSERT INTO parser_human_draft_cards (
            card_generation_public_id, draft_id, draft_version, draft_content_hash,
            field_values_json, decision_target_parser_output_id,
            decision_target_proposal_version, decision_target_proposal_content_hash,
            language, format_version, action_issue_batch_id, original_operation_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id, expires_at, issued_at
        ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, 'mixed', 'd1-human-card-v1', ?, ?,
                  ?, ?, ?, ?, ?, ?)
        """,
        (
            card_public_id,
            draft["id"],
            draft_hash,
            field_values_json,
            proposal["id"],
            version,
            content_hash,
            action_batch_id,
            operation["id"],
            current["authenticated_actor_id"],
            current["channel_account_id"],
            current["channel_conversation_id"],
            current["conversation_binding_id"],
            min(int(current["expires_at"]), now_epoch + 300),
            now_epoch,
        ),
    )
    return _result_for(conn, draft, operation)


def _parse_card_fields(raw_card_text: str) -> tuple[str, dict[str, str]]:
    if any(ch in raw_card_text for ch in ("\u2028", "\u2029")) or any(
        ord(ch) < 32 and ch not in "\r\n\t" for ch in raw_card_text
    ):
        raise _HumanDraftRefusal("D1_CARD_CONTROL_CHARACTER")
    reference: str | None = None
    fields: dict[str, str] = {}
    for line in raw_card_text.splitlines():
        stripped = line.strip(" \t")
        if not stripped:
            continue
        ascii_pos = stripped.find(":")
        full_pos = stripped.find("：")
        positions = [position for position in (ascii_pos, full_pos) if position >= 0]
        if not positions:
            raise _HumanDraftRefusal("D1_CARD_FORMAT")
        position = min(positions)
        label = stripped[:position].strip(" \t")
        value = stripped[position + 1 :].strip(" \t")
        key = label.casefold()
        if key in _REFERENCE_LABELS:
            if reference is not None:
                raise _HumanDraftRefusal("D1_CARD_REFERENCE_DUPLICATE")
            reference = value
            continue
        field = _LABELS.get(key)
        if field is None:
            raise _HumanDraftRefusal("D1_UNKNOWN_FIELD")
        if field in fields:
            raise _HumanDraftRefusal("D1_DUPLICATE_FIELD")
        fields[field] = value
    if reference is None:
        raise _HumanDraftRefusal("D1_CARD_REFERENCE_MISSING")
    if len(reference) != 39 or not reference.startswith("d1card_") or set(reference[7:]) - _HEX:
        raise _HumanDraftRefusal("D1_CARD_REFERENCE_INVALID")
    if not fields:
        raise _HumanDraftRefusal("D1_NO_SUPPORTED_FIELD")
    return reference, fields


def parse_human_draft_card_structure_for_routing(raw_card_text: str) -> str | None:
    """Use D1's syntax authority without applying a card or creating facts."""
    try:
        reference, _fields = _parse_card_fields(raw_card_text)
    except _HumanDraftRefusal:
        return None
    return reference


def _validate_human_draft_adapter(
    current_payload: dict[str, object],
    field_values: dict[str, str],
    *,
    source_type: str,
    reason_contributors: tuple[HumanReasonContributor, ...],
    operation_public_id: str,
) -> _ValidatedHumanDraft:
    """Private local-import seam; Task 3 owns field and reason validation."""
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    try:
        return validate_human_draft(
            current_payload,
            field_values,
            source_type=source_type,
            reason_contributors=reason_contributors,
            operation_public_id=operation_public_id,
        )
    except HumanDraftValidationError as exc:
        raise _HumanDraftRefusal(exc.code) from exc


def _canonicalize_human_draft_before_adapter(
    current_payload: dict[str, object], *, source_type: str
) -> dict[str, object]:
    """Private local-import seam for validator-owned canonical-before rules."""
    from finance_core.parser_proposals.human_draft_validation import canonicalize_human_draft_before

    return canonicalize_human_draft_before(current_payload, source_type=source_type)


def _request_material(command: HumanDraftCommand, raw_sha256: str) -> str:
    return _sha256_bytes(
        _encode_material(
            "d1-human-draft-operation-v1",
            command.operation_public_id,
            command.card_generation_public_id,
            str(command.telegram_message_id),
            command.authenticated_actor_id,
            command.telegram_account_id,
            command.telegram_conversation_id,
            command.conversation_binding_id,
            raw_sha256,
            _canonical_json(command.field_values),
        )
    )


def _insert_evidence(
    conn: sqlite3.Connection,
    draft: sqlite3.Row,
    command: HumanDraftCommand,
    raw: bytes,
    raw_hash: str,
    now: int,
) -> sqlite3.Row:
    evidence_public_id = _prefixed_id(
        "d1evidence_",
        "d1-human-reply-evidence-v1",
        command.authenticated_actor_id,
        command.telegram_account_id,
        command.telegram_conversation_id,
        command.conversation_binding_id,
        str(command.telegram_message_id),
    )
    conn.execute(
        """
        INSERT INTO parser_human_draft_reply_evidence (
            evidence_public_id, draft_id, raw_utf8, encoding, format_version,
            byte_length, sha256, authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            telegram_message_id, received_at
        ) VALUES (?, ?, ?, 'UTF-8', 'd1-human-reply-v1', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_public_id,
            draft["id"],
            raw,
            len(raw),
            raw_hash,
            command.authenticated_actor_id,
            command.telegram_account_id,
            command.telegram_conversation_id,
            command.conversation_binding_id,
            command.telegram_message_id,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM parser_human_draft_reply_evidence WHERE evidence_public_id = ?",
        (evidence_public_id,),
    ).fetchone()
    assert row is not None
    return row


def _operation_replay(
    conn: sqlite3.Connection,
    command: HumanDraftCommand,
    request_hash: str,
    raw: bytes,
) -> HumanDraftResult | None:
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (command.operation_public_id,),
    ).fetchone()
    if operation is None:
        return None
    evidence = conn.execute(
        "SELECT * FROM parser_human_draft_reply_evidence WHERE id = ?",
        (operation["human_reply_evidence_id"],),
    ).fetchone()
    draft = conn.execute(
        "SELECT * FROM parser_human_drafts WHERE id = ?", (operation["draft_id"],)
    ).fetchone()
    if (
        evidence is None
        or draft is None
        or not hmac.compare_digest(str(operation["request_material_hash"]), request_hash)
        or bytes(evidence["raw_utf8"]) != raw
        or operation["draft_id"] != evidence["draft_id"]
        or int(operation["telegram_message_id"]) != int(evidence["telegram_message_id"])
        or operation["authenticated_actor_id"] != evidence["authenticated_actor_id"]
        or operation["telegram_account_id"] != evidence["telegram_account_id"]
        or operation["telegram_conversation_id"] != evidence["telegram_conversation_id"]
        or operation["conversation_binding_id"] != evidence["conversation_binding_id"]
        or _context_from_row(draft) != _command_context(command)
    ):
        raise HumanDraftError("operation_conflict")
    return replace(_result_for(conn, draft, operation), idempotent_replay=True)


def _require_current_decision_target_before_d1_write(
    conn: sqlite3.Connection,
    *,
    draft: sqlite3.Row,
    card: sqlite3.Row,
) -> None:
    """Prove the old unconfirmed leaf is still the exact publication parent."""
    if (
        int(card["draft_id"]) != int(draft["id"])
        or _context_from_row(card) != _context_from_row(draft)
        or int(card["draft_version"]) != int(draft["current_draft_version"])
        or card["draft_content_hash"] != draft["current_draft_content_hash"]
        or int(card["decision_target_parser_output_id"])
        != int(draft["decision_target_parser_output_id"])
        or int(card["decision_target_proposal_version"])
        != int(draft["decision_target_proposal_version"])
        or not hmac.compare_digest(
            str(card["decision_target_proposal_content_hash"]),
            str(draft["decision_target_proposal_content_hash"]),
        )
    ):
        raise HumanDraftError("proposal_context_integrity")

    target = ParserProposalRepository(conn).get(int(draft["decision_target_parser_output_id"]))
    source = ParserProposalRepository(conn).get(int(draft["source_parser_output_id"]))
    if target is None or source is None:
        raise HumanDraftError("proposal_missing")
    if target["parse_status"] not in {
        "parsed_pending_confirmation",
        "edited_pending_confirmation",
    }:
        raise HumanDraftError("proposal_terminal")
    effective_payload, _completion_id, effective_version = resolve_effective_payload(conn, target)
    del effective_payload
    if effective_version != int(draft["decision_target_proposal_version"]):
        raise HumanDraftError("proposal_version")
    effective_hash = compute_effective_proposal_content_hash(conn, target)
    if not hmac.compare_digest(effective_hash, str(draft["decision_target_proposal_content_hash"])):
        raise HumanDraftError("proposal_content_hash")
    for key in ("source_type", "source_public_id", "statement_batch_id", "attachment_id"):
        if target[key] != source[key]:
            raise HumanDraftError("proposal_source_binding")

    lineage = conn.execute(
        """
        WITH RECURSIVE lineage(id, parent_parser_output_id) AS (
            SELECT id, parent_parser_output_id FROM parser_outputs WHERE id = ?
            UNION ALL
            SELECT parent.id, parent.parent_parser_output_id
            FROM parser_outputs AS parent
            JOIN lineage AS child ON child.parent_parser_output_id = parent.id
        )
        SELECT id, parent_parser_output_id FROM lineage
        """,
        (target["id"],),
    ).fetchall()
    if int(source["id"]) not in {int(row["id"]) for row in lineage}:
        raise HumanDraftError("proposal_lineage")
    if (
        conn.execute(
            "SELECT 1 FROM parser_outputs WHERE parent_parser_output_id = ? LIMIT 1",
            (target["id"],),
        ).fetchone()
        is not None
    ):
        raise HumanDraftError("publication_stale")
    for row in lineage:
        parent_id = row["parent_parser_output_id"]
        if parent_id is not None:
            sibling_count = conn.execute(
                "SELECT COUNT(*) FROM parser_outputs WHERE parent_parser_output_id = ?",
                (parent_id,),
            ).fetchone()[0]
            if int(sibling_count) != 1:
                raise HumanDraftError("publication_stale")

    raw_intake_id = draft["source_raw_intake_id"]
    if raw_intake_id is not None:
        raw_intake = conn.execute(
            "SELECT * FROM raw_intake_records WHERE id = ?", (raw_intake_id,)
        ).fetchone()
        if (
            raw_intake is None
            or int(raw_intake["parser_output_id"] or 0) != int(target["id"])
            or raw_intake["public_id"] != target["source_public_id"]
        ):
            raise HumanDraftError("publication_raw_intake")
    for table in (
        "parser_proposal_authorizations",
        "parser_proposal_conversion_audit",
        "receipt_proposal_conversions",
    ):
        if (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE parser_output_id = ? LIMIT 1", (target["id"],)
            ).fetchone()
            is not None
        ):
            raise HumanDraftError("proposal_authorized_or_converted")


def _refused_operation(
    conn: sqlite3.Connection,
    draft: sqlite3.Row,
    command: HumanDraftCommand,
    evidence: sqlite3.Row,
    request_hash: str,
    code: str,
    now: int,
) -> HumanDraftResult:
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome, result_completeness,
            telegram_message_id, request_material_hash, human_reply_evidence_id,
            before_draft_version, before_draft_content_hash, after_draft_version,
            after_draft_content_hash, canonical_supplied_fields_json,
            material_changes_json, explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, refusal_code, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, 'refused', 'refused', ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '[]',
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?)
        """,
        (
            command.operation_public_id,
            draft["id"],
            _result_completeness(conn, draft),
            command.telegram_message_id,
            request_hash,
            evidence["id"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            _canonical_json(command.field_values),
            _REASON_POLICY_VERSION,
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["unresolved_flags_json"],
            code,
            draft["current_card_generation_public_id"],
            command.authenticated_actor_id,
            command.telegram_account_id,
            command.telegram_conversation_id,
            command.conversation_binding_id,
            now,
        ),
    )
    cursor = conn.execute(
        """
        UPDATE parser_human_drafts
        SET last_claimed_message_id = ?, last_claimed_at = max(last_claimed_at, ?),
            updated_at = max(updated_at, ?)
        WHERE id = ? AND state = 'active' AND current_draft_version = ?
          AND current_draft_content_hash = ? AND current_card_generation_public_id = ?
        """,
        (
            command.telegram_message_id,
            now,
            now,
            draft["id"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_card_generation_public_id"],
        ),
    )
    if cursor.rowcount != 1:
        raise HumanDraftError("draft_race")
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (command.operation_public_id,),
    ).fetchone()
    refreshed = conn.execute(
        "SELECT * FROM parser_human_drafts WHERE id = ?", (draft["id"],)
    ).fetchone()
    assert operation is not None and refreshed is not None
    return _result_for(conn, refreshed, operation)


def apply_human_draft_card(
    conn: sqlite3.Connection,
    command: HumanDraftCommand,
    *,
    publish: Callable[[sqlite3.Connection, dict[str, object], dict[str, object]], PublishedDraft],
    authority_validator: Callable[[sqlite3.Connection], None] | None = None,
) -> HumanDraftResult:
    """Validate and append one whole-card operation under one owned write UOW."""
    try:
        raw = command.raw_card_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise HumanDraftError("reply_utf8_invalid") from exc
    if not raw or len(raw) > 16384:
        raise HumanDraftError("reply_size_invalid")
    if command.telegram_message_id <= 0 or not command.operation_public_id:
        raise HumanDraftError("command_invalid")
    raw_hash = _sha256_bytes(raw)
    request_hash = _request_material(command, raw_hash)
    _begin_immediate(conn)
    try:
        replay = _operation_replay(conn, command, request_hash, raw)
        if replay is not None:
            conn.commit()
            return replay
        if authority_validator is not None:
            authority_validator(conn)
        card = conn.execute(
            "SELECT * FROM parser_human_draft_cards WHERE card_generation_public_id = ?",
            (command.card_generation_public_id,),
        ).fetchone()
        if card is None:
            raise HumanDraftError("card_missing")
        draft = conn.execute(
            "SELECT * FROM parser_human_drafts WHERE id = ?", (card["draft_id"],)
        ).fetchone()
        assert draft is not None
        if _context_from_row(draft) != _command_context(command):
            raise HumanDraftError("actor_or_context_mismatch")
        now = _now_epoch()
        if draft["state"] != "active":
            raise HumanDraftError("draft_terminal")
        if int(draft["expires_at"]) <= now or int(card["expires_at"]) <= now:
            raise HumanDraftError("card_expired")
        if draft["current_card_generation_public_id"] != command.card_generation_public_id:
            raise HumanDraftError("stale_card")
        if command.telegram_message_id <= int(draft["last_claimed_message_id"]):
            raise HumanDraftError("stale_message")
        _require_current_decision_target_before_d1_write(conn, draft=draft, card=card)
        existing_message = conn.execute(
            """
            SELECT 1 FROM parser_human_draft_reply_evidence
            WHERE authenticated_actor_id = ? AND telegram_account_id = ?
              AND telegram_conversation_id = ? AND conversation_binding_id = ?
              AND telegram_message_id = ?
            """,
            (
                command.authenticated_actor_id,
                command.telegram_account_id,
                command.telegram_conversation_id,
                command.conversation_binding_id,
                command.telegram_message_id,
            ),
        ).fetchone()
        if existing_message is not None:
            raise HumanDraftError("message_conflict")
        evidence = _insert_evidence(conn, draft, command, raw, raw_hash, now)

        try:
            parsed_reference, parsed_fields = _parse_card_fields(command.raw_card_text)
            if parsed_reference != command.card_generation_public_id:
                raise _HumanDraftRefusal("D1_CARD_REFERENCE_MISMATCH")
            if parsed_fields != command.field_values:
                raise _HumanDraftRefusal("D1_FIELD_VALUES_MISMATCH")
            current_payload = json.loads(draft["current_payload_json"])
            contributors = _contributors_from_json(draft["reason_contributors_json"])
            proposal = ParserProposalRepository(conn).get(int(draft["source_parser_output_id"]))
            if proposal is None:
                raise HumanDraftError("proposal_missing")
            validated = _validate_human_draft_adapter(
                current_payload,
                parsed_fields,
                source_type=str(proposal["source_type"]),
                reason_contributors=contributors,
                operation_public_id=command.operation_public_id,
            )
        except _HumanDraftRefusal as exc:
            result = _refused_operation(conn, draft, command, evidence, request_hash, exc.code, now)
            conn.commit()
            return result

        changed_fields = tuple(validated.changed_fields)
        if not changed_fields:
            result = _append_noop(conn, draft, command, evidence, request_hash, parsed_fields, now)
            conn.commit()
            return result
        canonical_payload_json = _canonical_json(validated.canonical_payload)
        canonical_payload = dict(json.loads(canonical_payload_json))
        after_contributors = tuple(validated.reason_contributors)
        unresolved_flags = tuple(sorted(validated.unresolved_flags))
        if validated.completeness not in {"incomplete", "publishable"}:
            raise HumanDraftError("validation_completeness_invalid")
        persisted_completeness = (
            "complete" if validated.completeness == "publishable" else "incomplete"
        )
        after_contributors_json, after_contributors_hash = _contributors_material(
            after_contributors
        )
        flags_json, flags_hash = _flags_material(unresolved_flags)
        after_hash = _draft_hash(canonical_payload, after_contributors)
        after_version = int(draft["current_draft_version"]) + 1
        canonical_before = _canonicalize_human_draft_before_adapter(
            current_payload, source_type=str(proposal["source_type"])
        )
        changed_material = {
            field: (canonical_before.get(field), canonical_payload.get(field))
            for field in sorted(changed_fields)
        }
        explicit_clears = dict(sorted(validated.explicit_clears.items()))

        published: PublishedDraft | None = None
        if validated.completeness == "publishable":
            publication_context: dict[str, object] = {
                "source_parser_output_id": int(draft["decision_target_parser_output_id"]),
                "draft_public_id": str(draft["draft_public_id"]),
                "draft_version": after_version,
                "draft_content_hash": after_hash,
                "changed_fields": tuple(sorted(changed_fields)),
                "authenticated_actor_id": command.authenticated_actor_id,
                "operation_public_id": command.operation_public_id,
                "expected_content_hash": str(draft["decision_target_proposal_content_hash"]),
                "canonical_supplied_fields": dict(parsed_fields),
                "correction_channel": "telegram",
                "reason": "d1-whole-card-edit",
                "timestamp": now,
                "human_reply_evidence_public_id": str(evidence["evidence_public_id"]),
                "reason_policy_version": _REASON_POLICY_VERSION,
                "reason_contributors_before": _contributors_from_json(
                    draft["reason_contributors_json"]
                ),
                "reason_contributors_after": after_contributors,
                "explicit_clears": explicit_clears,
            }
            conn.set_authorizer(
                lambda action, _arg1, _arg2, _database, _source: (
                    sqlite3.SQLITE_DENY
                    if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}
                    else sqlite3.SQLITE_OK
                )
            )
            try:
                published = publish(
                    conn,
                    dict(json.loads(canonical_payload_json)),
                    copy.deepcopy(publication_context),
                )
            finally:
                conn.set_authorizer(None)

        card_public_id = _prefixed_id(
            "d1card_",
            "d1-card-generation-v1",
            str(draft["draft_public_id"]),
            command.operation_public_id,
            str(after_version),
        )
        action_batch_id = _sha256_bytes(_encode_material("d1-card-action-issue-v1", card_public_id))
        decision_parser_output_id = (
            published.parser_output_id
            if published is not None
            else int(draft["decision_target_parser_output_id"])
        )
        decision_version = (
            published.proposal_version
            if published is not None
            else int(draft["decision_target_proposal_version"])
        )
        decision_hash = (
            published.proposal_content_hash
            if published is not None
            else str(draft["decision_target_proposal_content_hash"])
        )
        conn.execute(
            """
            INSERT INTO parser_human_draft_operations (
                operation_public_id, draft_id, operation_type, operation_outcome,
                result_completeness,
                telegram_message_id, request_material_hash, human_reply_evidence_id,
                before_draft_version, before_draft_content_hash, after_draft_version,
                after_draft_content_hash, canonical_supplied_fields_json,
                material_changes_json, explicit_clears_json, reason_policy_version,
                reason_contributors_before_json, reason_contributors_before_hash,
                reason_contributors_after_json, reason_contributors_after_hash,
                unresolved_flags_json, result_card_generation_public_id,
                publication_parser_output_id, authenticated_actor_id,
                telegram_account_id, telegram_conversation_id,
                conversation_binding_id, correction_channel, created_at
            ) VALUES (?, ?, 'accepted', 'accepted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?)
            """,
            (
                command.operation_public_id,
                draft["id"],
                persisted_completeness,
                command.telegram_message_id,
                request_hash,
                evidence["id"],
                draft["current_draft_version"],
                draft["current_draft_content_hash"],
                after_version,
                after_hash,
                _canonical_json(parsed_fields),
                _canonical_json(changed_material),
                _canonical_json(explicit_clears),
                _REASON_POLICY_VERSION,
                draft["reason_contributors_json"],
                draft["reason_contributors_hash"],
                after_contributors_json,
                after_contributors_hash,
                flags_json,
                card_public_id,
                None if published is None else published.parser_output_id,
                command.authenticated_actor_id,
                command.telegram_account_id,
                command.telegram_conversation_id,
                command.conversation_binding_id,
                now,
            ),
        )
        operation = conn.execute(
            "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
            (command.operation_public_id,),
        ).fetchone()
        assert operation is not None
        conn.execute(
            """
            INSERT INTO parser_human_draft_cards (
                card_generation_public_id, draft_id, draft_version, draft_content_hash,
                field_values_json, parser_output_id, proposal_version,
                proposal_content_hash, decision_target_parser_output_id,
                decision_target_proposal_version, decision_target_proposal_content_hash,
                language, format_version, action_issue_batch_id, predecessor_card_id,
                original_operation_id, authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, expires_at, issued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mixed', 'd1-human-card-v1',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_public_id,
                draft["id"],
                after_version,
                after_hash,
                _canonical_json(_field_values(canonical_payload)),
                None if published is None else published.parser_output_id,
                None if published is None else published.proposal_version,
                None if published is None else published.proposal_content_hash,
                decision_parser_output_id,
                decision_version,
                decision_hash,
                action_batch_id,
                card["id"],
                operation["id"],
                command.authenticated_actor_id,
                command.telegram_account_id,
                command.telegram_conversation_id,
                command.conversation_binding_id,
                min(int(draft["expires_at"]), now + 300),
                now,
            ),
        )
        if published is not None:
            conn.execute(
                """
                INSERT INTO parser_human_draft_publications (
                    publication_public_id, draft_id, operation_id, draft_version,
                    draft_content_hash, parser_output_id, proposal_public_id,
                    proposal_version, proposal_content_hash, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _prefixed_id("d1pub_", "d1-human-publication-v1", command.operation_public_id),
                    draft["id"],
                    operation["id"],
                    after_version,
                    after_hash,
                    published.parser_output_id,
                    published.proposal_public_id,
                    published.proposal_version,
                    published.proposal_content_hash,
                    now,
                ),
            )
            from finance_core.parser_proposals.human_revision import (
                HumanRevisionLineageError,
                finalize_human_revision_in_transaction,
            )

            try:
                finalize_human_revision_in_transaction(
                    conn,
                    published=published,
                    context=publication_context,
                )
            except HumanRevisionLineageError as exc:
                raise HumanDraftError("publication_lineage") from exc
            _verify_published(conn, published, draft, canonical_payload)
        cursor = conn.execute(
            """
            UPDATE parser_human_drafts
            SET current_draft_version = ?, current_draft_content_hash = ?,
                current_payload_json = ?, field_values_json = ?,
                reason_contributors_json = ?, reason_contributors_hash = ?,
                unresolved_flags_json = ?, unresolved_flags_hash = ?,
                current_parser_output_id = ?, current_proposal_version = ?,
                current_proposal_content_hash = ?, decision_target_parser_output_id = ?,
                decision_target_proposal_version = ?, decision_target_proposal_content_hash = ?,
                current_card_generation_public_id = ?, last_claimed_message_id = ?,
                last_claimed_at = max(last_claimed_at, ?), updated_at = max(updated_at, ?)
            WHERE id = ? AND state = 'active' AND current_draft_version = ?
              AND current_draft_content_hash = ? AND current_card_generation_public_id = ?
            """,
            (
                after_version,
                after_hash,
                _canonical_json(canonical_payload),
                _canonical_json(_field_values(canonical_payload)),
                after_contributors_json,
                after_contributors_hash,
                flags_json,
                flags_hash,
                None if published is None else published.parser_output_id,
                None if published is None else published.proposal_version,
                None if published is None else published.proposal_content_hash,
                decision_parser_output_id,
                decision_version,
                decision_hash,
                card_public_id,
                command.telegram_message_id,
                now,
                now,
                draft["id"],
                draft["current_draft_version"],
                draft["current_draft_content_hash"],
                draft["current_card_generation_public_id"],
            ),
        )
        if cursor.rowcount != 1:
            raise HumanDraftError("draft_race")
        refreshed = conn.execute(
            "SELECT * FROM parser_human_drafts WHERE id = ?", (draft["id"],)
        ).fetchone()
        assert refreshed is not None
        result = _result_for(conn, refreshed, operation)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _append_noop(
    conn: sqlite3.Connection,
    draft: sqlite3.Row,
    command: HumanDraftCommand,
    evidence: sqlite3.Row,
    request_hash: str,
    parsed_fields: dict[str, str],
    now: int,
) -> HumanDraftResult:
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome, result_completeness,
            telegram_message_id, request_material_hash, human_reply_evidence_id,
            before_draft_version, before_draft_content_hash, after_draft_version,
            after_draft_content_hash, canonical_supplied_fields_json,
            material_changes_json, explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, 'noop', 'noop', ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '[]', ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?)
        """,
        (
            command.operation_public_id,
            draft["id"],
            _result_completeness(conn, draft),
            command.telegram_message_id,
            request_hash,
            evidence["id"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            _canonical_json(parsed_fields),
            _REASON_POLICY_VERSION,
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["unresolved_flags_json"],
            draft["current_card_generation_public_id"],
            command.authenticated_actor_id,
            command.telegram_account_id,
            command.telegram_conversation_id,
            command.conversation_binding_id,
            now,
        ),
    )
    cursor = conn.execute(
        """
        UPDATE parser_human_drafts
        SET last_claimed_message_id = ?, last_claimed_at = max(last_claimed_at, ?),
            updated_at = max(updated_at, ?)
        WHERE id = ? AND state = 'active' AND current_draft_version = ?
          AND current_draft_content_hash = ? AND current_card_generation_public_id = ?
        """,
        (
            command.telegram_message_id,
            now,
            now,
            draft["id"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_card_generation_public_id"],
        ),
    )
    if cursor.rowcount != 1:
        raise HumanDraftError("draft_race")
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (command.operation_public_id,),
    ).fetchone()
    refreshed = conn.execute(
        "SELECT * FROM parser_human_drafts WHERE id = ?", (draft["id"],)
    ).fetchone()
    assert operation is not None and refreshed is not None
    return _result_for(conn, refreshed, operation)


def _verify_published(
    conn: sqlite3.Connection,
    published: PublishedDraft,
    draft: sqlite3.Row,
    expected_payload: Mapping[str, object],
) -> None:
    if published.proposal_version < 0 or not _valid_hash(published.proposal_content_hash):
        raise HumanDraftError("publication_invalid")
    proposal = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (published.parser_output_id,)
    ).fetchone()
    if proposal is None or proposal["public_id"] != published.proposal_public_id:
        raise HumanDraftError("publication_invalid")
    if int(proposal["id"]) == int(draft["decision_target_parser_output_id"]):
        raise HumanDraftError("publication_lineage")
    if int(proposal["parent_parser_output_id"] or 0) != int(
        draft["decision_target_parser_output_id"]
    ):
        raise HumanDraftError("publication_lineage")
    parent = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?",
        (draft["decision_target_parser_output_id"],),
    ).fetchone()
    if parent is None or parent["parse_status"] != "superseded":
        raise HumanDraftError("publication_lineage")
    if proposal["parse_status"] not in {
        "parsed_pending_confirmation",
        "edited_pending_confirmation",
    }:
        raise HumanDraftError("publication_terminal")
    for key in ("source_type", "source_public_id", "statement_batch_id", "attachment_id"):
        if proposal[key] != parent[key]:
            raise HumanDraftError("publication_source_binding")
    children = conn.execute(
        "SELECT id FROM parser_outputs WHERE parent_parser_output_id = ? ORDER BY id",
        (parent["id"],),
    ).fetchall()
    if [int(row["id"]) for row in children] != [int(proposal["id"])]:
        raise HumanDraftError("publication_stale")
    descendant = conn.execute(
        "SELECT 1 FROM parser_outputs WHERE parent_parser_output_id = ? LIMIT 1",
        (proposal["id"],),
    ).fetchone()
    if descendant is not None:
        raise HumanDraftError("publication_stale")
    actual_payload, _completion_id, actual_version = resolve_effective_payload(conn, proposal)
    if actual_version != published.proposal_version:
        raise HumanDraftError("publication_version")
    actual_hash = compute_effective_proposal_content_hash(conn, proposal)
    if not hmac.compare_digest(actual_hash, published.proposal_content_hash):
        raise HumanDraftError("publication_content_hash")
    if proposal["parser_name"] == "human_revision":
        from finance_core.parser_proposals.human_revision import (
            HumanRevisionLineageError,
            verify_human_revision_descendant,
        )

        if _field_values(actual_payload) != _field_values(expected_payload):
            raise HumanDraftError("publication_payload")
        try:
            lineage = verify_human_revision_descendant(
                conn,
                dict(proposal),
                content_hash=actual_hash,
                proposal_version=actual_version,
            )
        except HumanRevisionLineageError as exc:
            raise HumanDraftError("publication_lineage") from exc
        if lineage is None:
            raise HumanDraftError("publication_lineage")
    elif actual_payload != dict(expected_payload):
        raise HumanDraftError("publication_payload")
    raw_intake_id = draft["source_raw_intake_id"]
    if raw_intake_id is not None:
        raw_intake = conn.execute(
            "SELECT * FROM raw_intake_records WHERE id = ?", (raw_intake_id,)
        ).fetchone()
        if (
            raw_intake is None
            or int(raw_intake["parser_output_id"] or 0) != int(proposal["id"])
            or raw_intake["public_id"] != proposal["source_public_id"]
        ):
            raise HumanDraftError("publication_raw_intake")
    for table in (
        "parser_proposal_authorizations",
        "parser_proposal_conversion_audit",
        "receipt_proposal_conversions",
    ):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE parser_output_id = ? LIMIT 1", (proposal["id"],)
        ).fetchone():
            raise HumanDraftError("publication_authorized")


def _delivery_projection(
    conn: sqlite3.Connection, card_public_id: str, context: HumanDraftContext
) -> tuple[str, str, tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    attempt_rows = conn.execute(
        """
        SELECT attempt_public_id, card_generation_public_id, delivery_identity,
               delivery_material_hash, authenticated_actor_id, telegram_account_id,
               telegram_conversation_id, conversation_binding_id, transport_mode,
               outbound_target_message_id, attempted_at
        FROM parser_human_draft_card_delivery_attempts
        WHERE card_generation_public_id = ? ORDER BY id
        """,
        (card_public_id,),
    ).fetchall()
    if any(_context_from_row(row) != context for row in attempt_rows):
        raise HumanDraftError("attempt_context")
    attempts = tuple(dict(row) for row in attempt_rows)
    outcome_rows = conn.execute(
        """
        SELECT outcomes.observation_public_id, outcomes.attempt_public_id,
               outcomes.outcome, outcomes.error_code, outcomes.outbound_message_id,
               outcomes.trusted_receipt_hash, outcomes.observed_at
        FROM parser_human_draft_card_delivery_outcomes AS outcomes
        JOIN parser_human_draft_card_delivery_attempts AS attempts
          ON attempts.attempt_public_id = outcomes.attempt_public_id
        WHERE attempts.card_generation_public_id = ?
        ORDER BY attempts.id,
                 CASE outcomes.observation_slot WHEN 'initial' THEN 0 ELSE 1 END,
                 outcomes.id
        """,
        (card_public_id,),
    ).fetchall()
    outcomes = tuple(dict(row) for row in outcome_rows)
    if not attempts:
        state = "not_attempted"
    elif not outcomes:
        state = "unknown"
    else:
        latest_attempt_id = attempts[-1]["attempt_public_id"]
        latest_outcomes = tuple(
            outcome for outcome in outcomes if outcome["attempt_public_id"] == latest_attempt_id
        )
        state = "unknown" if not latest_outcomes else str(latest_outcomes[-1]["outcome"])
    state_hash = _sha256_text(
        _canonical_json(
            {
                "card_generation_public_id": card_public_id,
                "attempts": attempts,
                "outcomes": outcomes,
            }
        )
    )
    return state, state_hash, attempts, outcomes


def _result_for(
    conn: sqlite3.Connection,
    draft: sqlite3.Row,
    operation: sqlite3.Row,
    *,
    card_public_id: str | None = None,
) -> HumanDraftResult:
    card = conn.execute(
        "SELECT * FROM parser_human_draft_cards WHERE card_generation_public_id = ?",
        (
            operation["result_card_generation_public_id"]
            if card_public_id is None
            else card_public_id,
        ),
    ).fetchone()
    if card is None:
        raise HumanDraftError("card_missing")
    if (
        int(card["draft_id"]) != int(draft["id"])
        or int(operation["draft_id"]) != int(draft["id"])
        or _context_from_row(card) != _context_from_row(draft)
        or _context_from_row(operation) != _context_from_row(draft)
    ):
        raise HumanDraftError("draft_context_integrity")
    original_operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE id = ?",
        (card["original_operation_id"],),
    ).fetchone()
    if (
        original_operation is None
        or int(original_operation["draft_id"]) != int(draft["id"])
        or _context_from_row(original_operation) != _context_from_row(draft)
    ):
        raise HumanDraftError("card_operation_integrity")
    if card["predecessor_card_id"] is not None:
        predecessor = conn.execute(
            "SELECT * FROM parser_human_draft_cards WHERE id = ?",
            (card["predecessor_card_id"],),
        ).fetchone()
        if (
            predecessor is None
            or int(predecessor["draft_id"]) != int(draft["id"])
            or _context_from_row(predecessor) != _context_from_row(draft)
        ):
            raise HumanDraftError("card_predecessor_integrity")
    proposal = (
        None
        if card["parser_output_id"] is None
        else ParserProposalRepository(conn).get(int(card["parser_output_id"]))
    )
    target = ParserProposalRepository(conn).get(int(card["decision_target_parser_output_id"]))
    if target is None:
        raise HumanDraftError("decision_target_missing")
    state, state_hash, attempts, outcomes = _delivery_projection(
        conn, str(card["card_generation_public_id"]), _context_from_row(draft)
    )
    evidence_public_id: str | None = None
    if operation["human_reply_evidence_id"] is not None:
        evidence = conn.execute(
            "SELECT * FROM parser_human_draft_reply_evidence WHERE id = ?",
            (operation["human_reply_evidence_id"],),
        ).fetchone()
        if (
            evidence is None
            or evidence["encoding"] != "UTF-8"
            or evidence["format_version"] != "d1-human-reply-v1"
            or not isinstance(evidence["raw_utf8"], bytes)
            or not 0 < int(evidence["byte_length"]) <= 16384
            or not _valid_hash(str(evidence["sha256"]))
            or _sha256_bytes(bytes(evidence["raw_utf8"])) != evidence["sha256"]
            or evidence["draft_id"] != operation["draft_id"]
            or int(evidence["telegram_message_id"]) != int(operation["telegram_message_id"])
            or evidence["authenticated_actor_id"] != operation["authenticated_actor_id"]
            or evidence["telegram_account_id"] != operation["telegram_account_id"]
            or evidence["telegram_conversation_id"] != operation["telegram_conversation_id"]
            or evidence["conversation_binding_id"] != operation["conversation_binding_id"]
            or _context_from_row(evidence) != _context_from_row(draft)
        ):
            raise HumanDraftError("reply_evidence_integrity")
        if len(bytes(evidence["raw_utf8"])) != int(evidence["byte_length"]):
            raise HumanDraftError("reply_evidence_integrity")
        evidence_public_id = str(evidence["evidence_public_id"])
    contributors = _contributors_from_json(operation["reason_contributors_after_json"])
    unresolved_flags = tuple(json.loads(operation["unresolved_flags_json"]))
    completeness = str(operation["result_completeness"])
    issuance_count = conn.execute(
        "SELECT COUNT(*) FROM parser_human_draft_action_bindings "
        "WHERE card_generation_public_id = ?",
        (card["card_generation_public_id"],),
    ).fetchone()[0]
    return HumanDraftResult(
        draft_public_id=str(draft["draft_public_id"]),
        draft_version=int(card["draft_version"]),
        draft_content_hash=str(card["draft_content_hash"]),
        completeness=completeness,
        proposal_public_id=None if proposal is None else str(proposal["public_id"]),
        proposal_version=None
        if card["proposal_version"] is None
        else int(card["proposal_version"]),
        proposal_content_hash=None
        if card["proposal_content_hash"] is None
        else str(card["proposal_content_hash"]),
        card_generation_public_id=str(card["card_generation_public_id"]),
        current_card_generation_public_id=str(draft["current_card_generation_public_id"]),
        operation_outcome=str(operation["operation_outcome"]),
        refusal_code=None if operation["refusal_code"] is None else str(operation["refusal_code"]),
        decision_target_proposal_public_id=str(target["public_id"]),
        decision_target_proposal_version=int(card["decision_target_proposal_version"]),
        decision_target_proposal_content_hash=str(card["decision_target_proposal_content_hash"]),
        field_values=dict(json.loads(card["field_values_json"])),
        reason_contributors=contributors,
        unresolved_flags=unresolved_flags,
        human_reply_evidence_public_id=evidence_public_id,
        delivery_state=state,
        delivery_state_hash=state_hash,
        delivery_attempts=attempts,
        delivery_outcomes=outcomes,
        action_issue_batch_id=str(card["action_issue_batch_id"]),
        action_issuance_state="issued" if issuance_count else "not_issued",
        idempotent_replay=False,
    )


def _binding_guard_rows(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    binding: HumanDraftDecisionBinding,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT drafts.*, cards.expires_at AS card_expires_at,
               cards.draft_id AS card_draft_id,
               cards.draft_version AS card_draft_version,
               cards.draft_content_hash AS card_draft_hash,
               cards.parser_output_id AS card_parser_output_id,
               cards.decision_target_proposal_version AS card_target_version,
               cards.decision_target_proposal_content_hash AS card_target_hash,
               operations.result_completeness AS card_completeness,
               refs.action, refs.reference_public_id, refs.expires_at AS reference_expires_at,
               refs.authenticated_actor_id AS reference_actor_id,
               refs.channel_account_id, refs.channel_conversation_id,
               refs.conversation_binding_id AS reference_binding_id,
               refs.proposal_version AS reference_proposal_version,
               refs.proposal_content_hash AS reference_proposal_content_hash,
               bindings.draft_id AS binding_draft_id,
               refs.id AS bound_action_reference_id,
               redemptions.id AS redemption_id
        FROM parser_human_draft_action_bindings AS bindings
        JOIN openclaw_human_action_references AS refs ON refs.id = bindings.reference_id
        JOIN openclaw_human_action_redemptions AS redemptions ON redemptions.reference_id = refs.id
        JOIN parser_human_draft_cards AS cards
          ON cards.card_generation_public_id = bindings.card_generation_public_id
        JOIN parser_human_draft_operations AS operations
          ON operations.id = cards.original_operation_id
        JOIN parser_human_drafts AS drafts ON drafts.id = cards.draft_id
        WHERE refs.reference_public_id = ? AND cards.card_generation_public_id = ?
          AND cards.decision_target_parser_output_id = ?
          AND bindings.draft_id = cards.draft_id
          AND bindings.authenticated_actor_id = cards.authenticated_actor_id
          AND bindings.telegram_account_id = cards.telegram_account_id
          AND bindings.telegram_conversation_id = cards.telegram_conversation_id
          AND bindings.conversation_binding_id = cards.conversation_binding_id
        """,
        (binding.reference_public_id, binding.card_generation_public_id, parser_output_id),
    ).fetchone()


def require_current_human_draft_publication_in_transaction(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_binding: HumanDraftDecisionBinding | None,
    now_epoch: int,
) -> None:
    _require_transaction(conn)
    if not _d1_decision_lineage_schema_available(conn, parser_output_id):
        if decision_binding is not None:
            raise HumanDraftError("decision_binding_invalid")
        return
    lineage = conn.execute(
        """
        SELECT draft_id FROM parser_human_draft_publications WHERE parser_output_id = ?
        UNION
        SELECT id FROM parser_human_drafts WHERE decision_target_parser_output_id = ?
        """,
        (parser_output_id, parser_output_id),
    ).fetchone()
    if lineage is None:
        if decision_binding is not None:
            raise HumanDraftError("decision_binding_invalid")
        return
    if decision_binding is None:
        raise HumanDraftError("decision_binding_required")
    _validated_human_draft_confirm_binding_row(
        conn,
        parser_output_id=parser_output_id,
        authenticated_actor_id=authenticated_actor_id,
        decision_binding=decision_binding,
        now_epoch=now_epoch,
        allowed_states=("active", "confirmed"),
    )


def _validated_human_draft_confirm_binding_row(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_binding: HumanDraftDecisionBinding,
    now_epoch: int,
    allowed_states: tuple[str, ...],
) -> sqlite3.Row:
    row = _binding_guard_rows(conn, parser_output_id=parser_output_id, binding=decision_binding)
    if row is None:
        raise HumanDraftError("decision_binding_invalid")
    expected_context = HumanDraftContext(
        decision_binding.authenticated_actor_id,
        decision_binding.telegram_account_id,
        decision_binding.telegram_conversation_id,
        decision_binding.conversation_binding_id,
    )
    publication = conn.execute(
        """
        SELECT 1 FROM parser_human_draft_publications
        WHERE draft_id = ? AND draft_version = ? AND draft_content_hash = ?
          AND parser_output_id = ? AND proposal_version = ? AND proposal_content_hash = ?
        """,
        (
            row["id"],
            row["current_draft_version"],
            row["current_draft_content_hash"],
            parser_output_id,
            row["decision_target_proposal_version"],
            row["decision_target_proposal_content_hash"],
        ),
    ).fetchone()
    competing = conn.execute(
        "SELECT 1 FROM parser_human_drafts WHERE source_parser_output_id = ? "
        "AND id != ? AND state = 'active' LIMIT 1",
        (row["source_parser_output_id"], row["id"]),
    ).fetchone()
    converted = any(
        conn.execute(
            f"SELECT 1 FROM {table} WHERE parser_output_id = ? LIMIT 1",
            (parser_output_id,),
        ).fetchone()
        is not None
        for table in ("parser_proposal_conversion_audit", "receipt_proposal_conversions")
    )
    if (
        row["action"] != "confirm"
        or authenticated_actor_id != decision_binding.authenticated_actor_id
        or row["reference_actor_id"] != authenticated_actor_id
        or row["channel_account_id"] != decision_binding.telegram_account_id
        or row["channel_conversation_id"] != decision_binding.telegram_conversation_id
        or row["reference_binding_id"] != decision_binding.conversation_binding_id
        or _context_from_row(row) != expected_context
        or int(row["binding_draft_id"]) != int(row["id"])
        or int(row["card_draft_id"]) != int(row["id"])
        or int(row["reference_proposal_version"]) != int(row["decision_target_proposal_version"])
        or row["reference_proposal_content_hash"] != row["decision_target_proposal_content_hash"]
        or row["state"] not in allowed_states
        or row["current_card_generation_public_id"] != decision_binding.card_generation_public_id
        or row["current_parser_output_id"] != parser_output_id
        or row["card_parser_output_id"] != parser_output_id
        or row["card_completeness"] != "complete"
        or int(row["card_draft_version"]) != int(row["current_draft_version"])
        or row["card_draft_hash"] != row["current_draft_content_hash"]
        or int(row["reference_expires_at"]) <= now_epoch
        or int(row["card_expires_at"]) <= now_epoch
        or int(row["expires_at"]) <= now_epoch
        or publication is None
        or competing is not None
        or converted
    ):
        raise HumanDraftError("decision_binding_stale")
    if row["state"] == "confirmed":
        terminal = conn.execute(
            """
            SELECT 1 FROM parser_human_draft_operations AS operation
            JOIN parser_proposal_authorizations AS decision
              ON decision.confirmation_public_id = operation.decision_public_id
            WHERE operation.draft_id = ? AND operation.operation_type = 'confirmed'
              AND operation.action_reference_id = ? AND decision.parser_output_id = ?
              AND decision.authenticated_actor_id = ?
              AND decision.confirmation_state = 'confirmed'
            """,
            (
                row["id"],
                row["bound_action_reference_id"],
                parser_output_id,
                authenticated_actor_id,
            ),
        ).fetchone()
        if terminal is None:
            raise HumanDraftError("decision_binding_stale")
    return row


def require_human_draft_reject_capability_in_transaction(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_binding: HumanDraftDecisionBinding,
    now_epoch: int,
) -> None:
    _require_transaction(conn)
    if not _d1_decision_lineage_schema_available(conn, parser_output_id):
        raise HumanDraftError("reject_binding_invalid")
    _validated_human_draft_reject_binding_row(
        conn,
        parser_output_id=parser_output_id,
        authenticated_actor_id=authenticated_actor_id,
        decision_binding=decision_binding,
        now_epoch=now_epoch,
        allowed_states=("active", "rejected"),
    )


def _validated_human_draft_reject_binding_row(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_binding: HumanDraftDecisionBinding,
    now_epoch: int,
    allowed_states: tuple[str, ...],
) -> sqlite3.Row:
    row = _binding_guard_rows(conn, parser_output_id=parser_output_id, binding=decision_binding)
    if row is None or row["action"] != "reject":
        raise HumanDraftError("reject_binding_invalid")
    if (
        authenticated_actor_id != decision_binding.authenticated_actor_id
        or row["reference_actor_id"] != authenticated_actor_id
        or row["channel_account_id"] != decision_binding.telegram_account_id
        or row["channel_conversation_id"] != decision_binding.telegram_conversation_id
        or row["reference_binding_id"] != decision_binding.conversation_binding_id
        or _context_from_row(row)
        != HumanDraftContext(
            decision_binding.authenticated_actor_id,
            decision_binding.telegram_account_id,
            decision_binding.telegram_conversation_id,
            decision_binding.conversation_binding_id,
        )
        or int(row["binding_draft_id"]) != int(row["id"])
        or int(row["card_draft_id"]) != int(row["id"])
        or int(row["decision_target_parser_output_id"]) != parser_output_id
        or int(row["reference_proposal_version"]) != int(row["decision_target_proposal_version"])
        or row["reference_proposal_content_hash"] != row["decision_target_proposal_content_hash"]
        or int(row["card_target_version"]) != int(row["decision_target_proposal_version"])
        or row["card_target_hash"] != row["decision_target_proposal_content_hash"]
        or row["state"] not in allowed_states
        or int(row["reference_expires_at"]) <= now_epoch
        or int(row["card_expires_at"]) <= now_epoch
    ):
        raise HumanDraftError("reject_binding_stale")
    if row["state"] == "rejected":
        terminal = conn.execute(
            """
            SELECT 1 FROM parser_human_draft_operations AS operation
            JOIN parser_proposal_authorizations AS decision
              ON decision.confirmation_public_id = operation.decision_public_id
            WHERE operation.draft_id = ? AND operation.operation_type = 'rejected'
              AND operation.action_reference_id = ? AND decision.parser_output_id = ?
              AND decision.authenticated_actor_id = ?
              AND decision.confirmation_state = 'rejected'
            """,
            (
                row["id"],
                row["bound_action_reference_id"],
                parser_output_id,
                authenticated_actor_id,
            ),
        ).fetchone()
        if terminal is None:
            raise HumanDraftError("reject_binding_stale")
    return row


def confirm_active_human_draft_in_transaction(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_public_id: str,
    decision_binding: HumanDraftDecisionBinding,
    now_epoch: int,
) -> None:
    _require_transaction(conn)
    decision = conn.execute(
        "SELECT * FROM parser_proposal_authorizations WHERE confirmation_public_id = ?",
        (decision_public_id,),
    ).fetchone()
    if (
        decision is None
        or decision["authenticated_actor_id"] != authenticated_actor_id
        or decision["confirmation_state"] != "confirmed"
        or int(decision["parser_output_id"]) != parser_output_id
    ):
        raise HumanDraftError("decision_state")
    proposal = ParserProposalRepository(conn).get(parser_output_id)
    if proposal is None or proposal["parse_status"] != "confirmed":
        raise HumanDraftError("decision_state")
    draft = _validated_human_draft_confirm_binding_row(
        conn,
        parser_output_id=parser_output_id,
        authenticated_actor_id=authenticated_actor_id,
        decision_binding=decision_binding,
        now_epoch=now_epoch,
        allowed_states=("active", "confirmed"),
    )
    existing = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (decision_public_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["operation_type"] != "confirmed"
            or existing["decision_public_id"] != decision_public_id
            or int(existing["draft_id"]) != int(draft["id"])
            or existing["action_reference_id"] != draft["bound_action_reference_id"]
            or draft["state"] != "confirmed"
        ):
            raise HumanDraftError("decision_conflict")
        return
    if draft["state"] != "active":
        raise HumanDraftError("decision_binding_stale")
    request_hash = _sha256_bytes(
        _encode_material("d1-human-draft-confirm-v1", decision_public_id, str(parser_output_id))
    )
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome,
            result_completeness, request_material_hash, action_reference_id,
            decision_public_id, before_draft_version, before_draft_content_hash,
            after_draft_version, after_draft_content_hash,
            canonical_supplied_fields_json, material_changes_json,
            explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, 'confirmed', 'accepted', 'complete', ?, ?, ?, ?, ?, ?, ?,
                  '{}', '{}', '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?)
        """,
        (
            decision_public_id,
            draft["id"],
            request_hash,
            draft["bound_action_reference_id"],
            decision_public_id,
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            _REASON_POLICY_VERSION,
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["unresolved_flags_json"],
            draft["current_card_generation_public_id"],
            authenticated_actor_id,
            draft["telegram_account_id"],
            draft["telegram_conversation_id"],
            draft["conversation_binding_id"],
            now_epoch,
        ),
    )
    cursor = conn.execute(
        "UPDATE parser_human_drafts SET state = 'confirmed', updated_at = max(updated_at, ?) "
        "WHERE id = ? AND state = 'active'",
        (now_epoch, draft["id"]),
    )
    if cursor.rowcount != 1:
        raise HumanDraftError("draft_race")


def reject_active_human_draft_in_transaction(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    authenticated_actor_id: str,
    decision_public_id: str,
    decision_binding: HumanDraftDecisionBinding | None,
    now_epoch: int,
) -> None:
    _require_transaction(conn)
    decision = conn.execute(
        "SELECT * FROM parser_proposal_authorizations WHERE confirmation_public_id = ?",
        (decision_public_id,),
    ).fetchone()
    if decision is None:
        raise HumanDraftError("decision_missing")
    if decision["authenticated_actor_id"] != authenticated_actor_id:
        raise HumanDraftError("decision_actor")
    if decision["confirmation_state"] != "rejected":
        raise HumanDraftError("decision_state")
    if int(decision["parser_output_id"]) != parser_output_id:
        raise HumanDraftError("decision_target")
    proposal = ParserProposalRepository(conn).get(parser_output_id)
    if proposal is None:
        raise HumanDraftError("decision_target")
    if proposal["parse_status"] != "rejected":
        raise HumanDraftError("decision_state")

    if not _d1_decision_lineage_schema_available(conn, parser_output_id):
        if decision_binding is not None:
            raise HumanDraftError("reject_binding_invalid")
        return

    bound_draft: sqlite3.Row | None = None
    if decision_binding is not None:
        bound_draft = _validated_human_draft_reject_binding_row(
            conn,
            parser_output_id=parser_output_id,
            authenticated_actor_id=authenticated_actor_id,
            decision_binding=decision_binding,
            now_epoch=now_epoch,
            allowed_states=("active", "rejected"),
        )

    existing = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (decision_public_id,),
    ).fetchone()
    if existing is not None:
        existing_draft = conn.execute(
            "SELECT * FROM parser_human_drafts WHERE id = ?", (existing["draft_id"],)
        ).fetchone()
        if (
            existing_draft is None
            or existing["operation_type"] != "rejected"
            or existing["decision_public_id"] != decision_public_id
            or int(existing_draft["decision_target_parser_output_id"]) != parser_output_id
            or existing_draft["decision_target_proposal_content_hash"]
            != decision["proposal_content_hash"]
            or existing_draft["state"] != "rejected"
            or existing["authenticated_actor_id"] != authenticated_actor_id
            or _context_from_row(existing) != _context_from_row(existing_draft)
            or (bound_draft is None and existing["action_reference_id"] is not None)
            or (
                bound_draft is not None
                and (
                    int(existing_draft["id"]) != int(bound_draft["id"])
                    or existing["action_reference_id"] != bound_draft["bound_action_reference_id"]
                )
            )
        ):
            raise HumanDraftError("decision_conflict")
        return

    if bound_draft is not None:
        if bound_draft["state"] != "active":
            raise HumanDraftError("reject_binding_stale")
        draft = bound_draft
    else:
        rows = conn.execute(
            """
            SELECT * FROM parser_human_drafts
            WHERE state = 'active' AND authenticated_actor_id = ?
              AND decision_target_parser_output_id = ?
            """,
            (authenticated_actor_id, parser_output_id),
        ).fetchall()
        if not rows:
            return
        if len(rows) != 1:
            raise HumanDraftError("draft_ownership_ambiguous")
        draft = rows[0]
    _payload, _completion_id, effective_version = resolve_effective_payload(conn, proposal)
    effective_hash = compute_effective_proposal_content_hash(conn, proposal)
    if (
        effective_version != int(draft["decision_target_proposal_version"])
        or not hmac.compare_digest(
            effective_hash, str(draft["decision_target_proposal_content_hash"])
        )
        or not hmac.compare_digest(
            str(decision["proposal_content_hash"]),
            str(draft["decision_target_proposal_content_hash"]),
        )
    ):
        raise HumanDraftError("decision_target")
    action_reference_id: int | None = None
    if bound_draft is not None:
        action_reference_id = int(bound_draft["bound_action_reference_id"])
    request_hash = _sha256_bytes(
        _encode_material("d1-human-draft-reject-v1", decision_public_id, str(parser_output_id))
    )
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome,
            result_completeness,
            request_material_hash, action_reference_id, decision_public_id,
            before_draft_version, before_draft_content_hash, after_draft_version,
            after_draft_content_hash, canonical_supplied_fields_json,
            material_changes_json, explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, 'rejected', 'accepted', ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '[]',
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?)
        """,
        (
            decision_public_id,
            draft["id"],
            _result_completeness(conn, draft),
            request_hash,
            action_reference_id,
            decision_public_id,
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            _REASON_POLICY_VERSION,
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["unresolved_flags_json"],
            draft["current_card_generation_public_id"],
            authenticated_actor_id,
            draft["telegram_account_id"],
            draft["telegram_conversation_id"],
            draft["conversation_binding_id"],
            now_epoch,
        ),
    )
    cursor = conn.execute(
        "UPDATE parser_human_drafts SET state = 'rejected', updated_at = max(updated_at, ?) "
        "WHERE id = ? AND state = 'active'",
        (now_epoch, draft["id"]),
    )
    if cursor.rowcount != 1:
        raise HumanDraftError("draft_race")


__all__ = [
    "HumanDraftCommand",
    "HumanDraftContext",
    "HumanDraftDecisionBinding",
    "HumanDraftError",
    "HumanDraftResult",
    "HumanReasonContributor",
    "PublishedDraft",
    "apply_human_draft_card",
    "begin_human_draft_in_transaction",
    "confirm_active_human_draft_in_transaction",
    "reject_active_human_draft_in_transaction",
    "require_current_human_draft_publication_in_transaction",
    "require_human_draft_reject_capability_in_transaction",
]
