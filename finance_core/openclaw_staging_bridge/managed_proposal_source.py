"""Read-only durable Telegram source proof for a managed Bridge proposal.

The caller context is supplied by a trusted adapter; this verifies its match
to persisted evidence, not the original Telegram update's authentication.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from finance_core.intake.raw_text_repository import get_raw_intake_record_by_public_id
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    verify_ai_fallback_child,
)
from finance_core.parser_proposals.content_hash import (
    ProposalContentHashError,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.effective_payload import (
    EffectivePayloadError,
    resolve_effective_payload,
)
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    verify_human_revision_descendant,
    verify_public_receipt_revision_edge,
)
from finance_core.parser_proposals.receipt_source_evidence import (
    ReceiptSourceEvidenceError,
    verify_receipt_source_evidence,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    require_telegram_source_context,
)


class ManagedSourceUnavailable(RuntimeError):
    """The persisted source cannot be bound to this managed caller."""


def _effective_identity(conn: sqlite3.Connection, proposal: Mapping[str, Any]) -> tuple[str, int]:
    _payload, _completion_id, version = resolve_effective_payload(conn, dict(proposal))
    return compute_effective_proposal_content_hash(conn, dict(proposal)), version


def require_managed_proposal_source(
    conn: sqlite3.Connection,
    *,
    context: Any,
    proposal: Mapping[str, Any],
    supported_source_types: frozenset[str],
) -> None:
    """Prove the target is on the current, sealed source chain without writes."""
    source_public_id = proposal.get("source_public_id")
    if not isinstance(source_public_id, str) or not source_public_id:
        raise ManagedSourceUnavailable
    intake = get_raw_intake_record_by_public_id(conn, source_public_id)
    if intake is None or intake.get("public_id") != source_public_id:
        raise ManagedSourceUnavailable
    message_id = intake.get("source_message_id")
    if (
        intake.get("source_channel") != "telegram"
        or intake.get("source_type") not in supported_source_types
        or not isinstance(message_id, str)
        or not message_id.isascii()
        or not message_id.isdecimal()
        or message_id.startswith("0")
        or intake.get("external_source_id") != f"telegram:{context.conversation_id}:{message_id}"
    ):
        raise ManagedSourceUnavailable
    try:
        require_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=TelegramSourceContext(
                authenticated_actor_id=context.actor_id,
                account_id=context.account_id,
                conversation_id=context.conversation_id,
                binding_id=context.binding_id,
                message_id=message_id,
            ),
        )
    except (TelegramSourceContextError, TypeError, ValueError) as exc:
        raise ManagedSourceUnavailable from exc

    current_id = intake.get("parser_output_id")
    target_id = proposal.get("id")
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    repository = ParserProposalRepository(conn)
    while (
        isinstance(current_id, int)
        and not isinstance(current_id, bool)
        and current_id > 0
        and current_id not in seen
        and len(seen) < 128
    ):
        seen.add(current_id)
        row = repository.get_lineage_row_by_id(current_id)
        if (
            row is None
            or row.get("source_public_id") != source_public_id
            or row.get("source_type") != intake.get("source_type")
        ):
            raise ManagedSourceUnavailable
        rows.append(row)
        parent_id = row.get("parent_parser_output_id")
        if parent_id is None:
            break
        current_id = parent_id
    else:
        raise ManagedSourceUnavailable
    if not rows or rows[-1].get("parent_parser_output_id") is not None:
        raise ManagedSourceUnavailable
    if target_id not in seen:
        raise ManagedSourceUnavailable
    if rows[-1].get("source_type") != intake.get("source_type"):
        raise ManagedSourceUnavailable

    current = rows[0]
    try:
        current_hash, current_version = _effective_identity(conn, current)
        d1_lineage = verify_human_revision_descendant(
            conn, current, content_hash=current_hash, proposal_version=current_version
        )
        verify_ai_fallback_child(
            conn,
            current,
            content_hash=current_hash,
            proposal_version=current_version,
            require_resolved=False,
        )
        # A complete chain can mix sealed D1, AI and public receipt revision
        # edges. A proof for its current child alone must not bless an earlier
        # arbitrary same-source parent pointer.
        for child, parent in zip(rows, rows[1:], strict=False):
            if child["parent_parser_output_id"] != parent["id"]:
                raise ManagedSourceUnavailable
            child_id = int(child["id"])
            has_d1 = (
                conn.execute(
                    "SELECT 1 FROM parser_human_draft_publications WHERE parser_output_id = ?",
                    (child_id,),
                ).fetchone()
                is not None
            )
            has_ai = (
                conn.execute(
                    "SELECT 1 FROM ai_fallback_proposal_links WHERE parser_output_id = ?",
                    (child_id,),
                ).fetchone()
                is not None
            )
            if has_d1:
                if d1_lineage is None:
                    raise ManagedSourceUnavailable
            elif has_ai:
                child_hash, child_version = _effective_identity(conn, child)
                ai_edge = verify_ai_fallback_child(
                    conn,
                    child,
                    content_hash=child_hash,
                    proposal_version=child_version,
                    require_resolved=False,
                    _skip_human=True,
                    _human_descendant_parser_output_id=int(current["id"]),
                )
                if ai_edge is None:
                    raise ManagedSourceUnavailable
            else:
                child_hash, child_version = _effective_identity(conn, child)
                if (
                    verify_public_receipt_revision_edge(
                        conn,
                        child,
                        content_hash=child_hash,
                        proposal_version=child_version,
                    )
                    is None
                ):
                    raise ManagedSourceUnavailable
        if intake["source_type"] == "telegram_image":
            verify_receipt_source_evidence(conn, intake=intake, chain=rows)
    except (
        AiFallbackServiceError,
        EffectivePayloadError,
        HumanRevisionLineageError,
        ProposalContentHashError,
        ReceiptSourceEvidenceError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ManagedSourceUnavailable from exc
