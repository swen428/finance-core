"""Same-snapshot initial receipt qualification from the original total owner.

This is an internal evidence reconstruction, not an AI/runtime authority port.
No model-provided layout, current child evidence or cached VALID grants a role.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from typing import Any

from finance_core.application.ai_invocation_contract import AiInvocationError
from finance_core.intake import receipt_ocr_proposal as owner
from finance_core.money import money_decimal
from finance_core.parser_proposals import receipt_total_parser as receipt_parser
from finance_core.parser_proposals.ai_ocr_layout import load_ocr_layout
from finance_core.parser_proposals.independent_ai_source_grammar import assess_source_money
from finance_core.parser_proposals.receipt_total_parser import (
    PARSER_CONTRACT_VERSION_DEFAULT,
    PARSER_CONTRACT_VERSION_TSV_HIERARCHY,
    PARSER_NAME,
    PARSER_VERSION,
    PARSER_VERSION_TSV_HIERARCHY,
    parse_receipt_total,
)
from finance_core.parser_proposals.repository import ParserProposalRepository


def qualify_receipt_money(
    conn: sqlite3.Connection, parent_id: int, projection: dict[str, Any]
) -> tuple[str, str, str] | None:
    """Return original amount/currency/full total span, or unresolved source.

    Bad custody/identity/evidence raises; a real but incomplete/ambiguous total
    returns None. The caller separately preserves confidence and human guards.
    """
    try:
        return _qualify(conn, parent_id, projection)
    except (ValueError, KeyError, TypeError, owner.ReceiptOcrProposalError) as exc:
        raise AiInvocationError("Original receipt total source integrity changed") from exc


def _qualify(conn, parent_id, projection, *, draft=False):
    repository = ParserProposalRepository(conn)
    parent = repository.get_lineage_row_by_id(parent_id)
    if parent is None:
        raise AiInvocationError("Receipt source parent absent")
    current = parent
    seen = set()
    while current["parent_parser_output_id"] is not None:
        if current["id"] in seen or len(seen) >= 128:
            raise AiInvocationError("Receipt source ancestry invalid")
        seen.add(current["id"])
        previous = repository.get_lineage_row_by_id(current["parent_parser_output_id"])
        if previous is None or any(
            previous[k] != parent[k]
            for k in (
                "source_type",
                "source_public_id",
                "attachment_id",
                "raw_text",
            )
        ):
            raise AiInvocationError("Receipt original source ancestry changed")
        current = previous
    root = current
    contracts = {
        PARSER_VERSION: PARSER_CONTRACT_VERSION_DEFAULT,
        PARSER_VERSION_TSV_HIERARCHY: PARSER_CONTRACT_VERSION_TSV_HIERARCHY,
    }
    links = conn.execute(
        "SELECT * FROM receipt_ocr_proposal_links WHERE parser_output_id=?", (root["id"],)
    ).fetchall()
    if (
        root["parser_name"] != PARSER_NAME
        or root["parser_version"] not in contracts
        or root["source_type"] != "telegram_image"
        or root["attachment_id"] is None
        or root["raw_text"] is not None
        or root["statement_batch_id"] is not None
        or any(root[k] is not None for k in ("ai_provider", "ai_model", "prompt_version"))
        or len(links) != 1
    ):
        raise AiInvocationError("Original receipt total owner identity missing")
    link = links[0]
    if (
        link["link_role"] != "initial"
        or link["parser_contract_version"] != contracts[root["parser_version"]]
    ):
        raise AiInvocationError("Original receipt total contract/role changed")
    payload = json.loads(root["parsed_payload"])
    layout = load_ocr_layout(conn, parent_payload=payload, source_kind="receipt_local_ocr_text")
    if layout is None:
        raise AiInvocationError("Original receipt layout absent")
    extraction = owner._load_extraction(conn, layout.extraction_public_id)
    binding = owner._resolve_source_binding(conn, extraction)
    if (
        binding.raw_intake_public_id != root["source_public_id"]
        or binding.attachment_id != root["attachment_id"]
        or extraction.block_count != len(layout.blocks)
        or link["extraction_id"] != extraction.id
        or projection["selection"]
        != {
            "kind": "local_ocr",
            "extraction_id": extraction.public_id,
            "normalized_result_hash": extraction.normalized_result_hash,
            "source_hash": extraction.source_attachment_hash,
        }
        or projection["source_text"] != "\n".join(b.text for b in layout.blocks)
    ):
        raise AiInvocationError("Original receipt selection/source binding changed")
    parse = parse_receipt_total(
        layout.blocks,
        extraction_status=extraction.extraction_status,
        parser_version=root["parser_version"],
    )
    rebuilt = owner._build_proposal_payload(parse, extraction)
    expected_json = owner._canonical_json(rebuilt)
    command = owner._Command(
        extraction.public_id,
        root["public_id"],
        link["public_id"],
        contracts[root["parser_version"]],
        root["parser_version"],
    )
    if (
        root["parsed_payload"] != expected_json
        or root["normalized_payload"] != expected_json
        or root["confidence_score"] != parse.overall_confidence
        or link["proposal_result_hash"] != hashlib.sha256(expected_json.encode()).hexdigest()
        or link["proposal_input_hash"] != owner._compute_input_hash(command, extraction, binding)
    ):
        raise AiInvocationError("Original receipt total payload/hash changed")
    expected = Counter(owner._field_evidence_rows(root["id"], rebuilt["field_evidence"]))
    stored = Counter(
        tuple(row)
        for row in conn.execute(
            "SELECT parser_output_id,field_name,proposed_value,confidence_score,"
            "evidence_source_type,evidence_reference,notes FROM parser_proposal_field_evidence "
            "WHERE parser_output_id=?",
            (root["id"],),
        )
    )
    if expected != stored:
        raise AiInvocationError("Original receipt total field evidence changed")
    if parse.amount.value is None or parse.currency.value is None:
        if not draft:
            return None
        # A partial draft may retain a unique unsigned literal from the real
        # versioned TOTAL geometry. It grants no currency or posting authority.
        lines = (
            receipt_parser._build_tsv_hierarchy_lines(layout.blocks)
            if root["parser_version"] == PARSER_VERSION_TSV_HIERARCHY
            else receipt_parser._build_lines(layout.blocks)
        )
        by_index = {block.sequence_index: block for block in layout.blocks}
        candidates = []
        for line in lines:
            if not receipt_parser._is_total_line(line.upper):
                continue
            extracted = receipt_parser._extract_line_total(line.upper)
            text = "\n".join(by_index[index].text for index in line.block_sequence_indexes)
            assessed = assess_source_money(text)
            if (
                extracted is None
                or not assessed.qualified
                or len(assessed.spans) != 1
                or money_decimal(extracted[1]) <= 0
                or money_decimal(assessed.spans[0].number) != money_decimal(extracted[1])
            ):
                return None
            candidates.append((assessed.spans[0].number, extracted[0], text))
        if not candidates or len({(money_decimal(a), c) for a, c, _ in candidates}) != 1:
            return None
        return candidates[0]
    indexes = parse.amount.block_sequence_indexes
    if not indexes or indexes != parse.currency.block_sequence_indexes:
        raise AiInvocationError("Original receipt total money pair is not joint evidence")
    by_index = {block.sequence_index: block for block in layout.blocks}
    # The legacy owner deduplicates canonical pairs. That cannot hide another
    # genuinely TOTAL role whose raw notation is unknown to the new grammar.
    # Reuse its exact version-specific line geometry and role delimitation;
    # do not add roles or reinterpret non-TOTAL discount/cash/item lines.
    lines = (
        receipt_parser._build_tsv_hierarchy_lines(layout.blocks)
        if root["parser_version"] == PARSER_VERSION_TSV_HIERARCHY
        else receipt_parser._build_lines(layout.blocks)
    )
    for line in lines:
        if not receipt_parser._is_total_line(line.upper):
            continue
        original_span = "\n".join(by_index[index].text for index in line.block_sequence_indexes)
        assessment = assess_source_money(original_span, receipt_currency=str(parse.currency.value))
        if (
            not assessment.qualified
            or len(assessment.spans) != 1
            or assessment.currencies != {str(parse.currency.value)}
            or money_decimal(assessment.spans[0].number) != money_decimal(str(parse.amount.value))
        ):
            return None
    # Retain the same block separator as the exact model source projection;
    # qualified quote offsets must map back into that raw selection.
    total_text = "\n".join(by_index[index].text for index in indexes)
    return str(parse.amount.value), str(parse.currency.value), total_text


def qualify_receipt_total_draft(conn, parent_id, projection):
    try:
        return _qualify(conn, parent_id, projection, draft=True)
    except (ValueError, KeyError, TypeError, owner.ReceiptOcrProposalError) as exc:
        raise AiInvocationError("Original receipt draft TOTAL source integrity changed") from exc


def require_source_base(conn, material, *, historical: bool = False) -> dict[str, Any]:
    """Do not replace an actual HUMAN base with cached invocation values."""
    from finance_core.parser_proposals.independent_ai_revision import verified_source_revision

    try:
        return verified_source_revision(conn, material, historical=historical)
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, AiInvocationError):
            raise
        raise AiInvocationError("Original source revision history does not verify") from exc


def validate_source_response(conn, raw, material, *, historical: bool = False):
    """One owning calculation for live results and all immutable rebuilds."""
    from finance_core.parser_proposals.independent_ai_validation import validate_response

    payload = require_source_base(conn, material, historical=historical)
    projection = material["projection"]
    money = None
    if projection["kind"] == "initial" and projection["selection"]["kind"] == "local_ocr":
        money = qualify_receipt_money(conn, material["parent_id"], projection)
    from finance_core.application.ai_invocation_contract import AI_INPUT_SCHEMA

    if material["schema"] == AI_INPUT_SCHEMA:
        from finance_core.parser_proposals.independent_ai_v2 import validate_response_v2

        draft_total = (
            qualify_receipt_total_draft(conn, material["parent_id"], projection)
            if projection["kind"] == "initial" and projection["selection"]["kind"] == "local_ocr"
            else None
        )
        return validate_response_v2(
            raw,
            projection,
            payload,
            receipt_money=money,
            receipt_draft_total=draft_total,
            _intent_source_text=_v2_intent_source_text(conn, material),
        )
    return validate_response(raw, projection, payload, _receipt_money=money)


def _v2_intent_source_text(conn, material):
    """Reconstruct local deny-only source after the actual revision proof.

    This text never enters model projection, operation evidence or money/date
    interpretation. Missing custody cannot turn into an empty-source exemption.
    """
    try:
        repository = ParserProposalRepository(conn)
        parent = repository.get_lineage_row_by_id(material["parent_id"])
        source, projection = material["source"], material["projection"]
        if (
            parent is None
            or parent["source_public_id"] != material["intake_public_id"]
            or source["intake_public_id"] != parent["source_public_id"]
        ):
            raise ValueError("intent_source_identity")
        retained = conn.execute(
            "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
            (parent["source_public_id"],),
        ).fetchone()
        if retained is None or not isinstance(retained["raw_input"], str):
            raise ValueError("intent_source_absent")
        digest = hashlib.sha256(retained["raw_input"].encode("utf-8")).hexdigest()
        if retained["source_content_hash"] != "sha256:" + digest:
            raise ValueError("intent_source_hash")
        if projection["selection"]["kind"] == "text":
            text = retained["raw_input"]
            if (
                source["source_content_hash"] != digest
                or parent["attachment_id"] is not None
                or parent["raw_text"] != text
                or projection["selection"]
                != {"kind": "text", "source_public_id": parent["source_public_id"]}
            ):
                raise ValueError("intent_text_selection")
        elif projection["selection"]["kind"] == "local_ocr":
            root, seen = parent, set()
            while root["parent_parser_output_id"] is not None:
                if root["id"] in seen or len(seen) >= 128:
                    raise ValueError("intent_ocr_ancestry")
                seen.add(root["id"])
                root = repository.get_lineage_row_by_id(root["parent_parser_output_id"])
                if root is None or any(
                    root[k] != parent[k]
                    for k in ("source_type", "source_public_id", "attachment_id", "raw_text")
                ):
                    raise ValueError("intent_ocr_source")
            layout = load_ocr_layout(
                conn,
                parent_payload=json.loads(root["parsed_payload"]),
                source_kind="receipt_local_ocr_text",
            )
            if layout is None:
                raise ValueError("intent_ocr_layout")
            text = "\n".join(block.text for block in layout.blocks)
            # Reuse the fixed receipt source/selection owner, including complete
            # root payload, extraction, blocks and source-binding identities.
            # Its returned TOTAL grants no authority and is deliberately unused.
            qualify_receipt_money(conn, parent["id"], {**projection, "source_text": text})
        else:
            raise ValueError("intent_selection")
        if not text or len(text.encode("utf-8")) > 8192:
            raise ValueError("intent_source_limit")
        if projection["interaction_kind"] != "card_edit" and projection["source_text"] != text:
            raise ValueError("intent_source_projection")
        return text
    except (ValueError, KeyError, TypeError, sqlite3.Error, owner.ReceiptOcrProposalError) as exc:
        raise AiInvocationError("Original V2 intent source integrity changed") from exc
