"""S5e-B Finance-owned AI fallback preparation, claim, and result boundary.

The host plugin never supplies source text or proposal facts.  This module
re-reads staging truth, builds the bounded request, owns all clocks, and
persists one immutable attempt/claim/result lineage.  A model result is still
only a parser proposal and can never create final financial state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from finance_core.calculation.authoritative_snapshot import canonical_json_text
from finance_core.financial_audit import (
    AUDIT_SCHEMA_VERSION,
    ZERO_AUDIT_HASH,
    AuditEventCommand,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.money import (
    MoneyValidationError,
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.ai_fallback_provenance import (
    AiFallbackProvenanceValidationError,
    canonical_json_bytes,
    canonical_request_sha256,
    canonical_response_sha256,
    claim_material_hash,
    derive_attempt_public_id,
    derive_claim_public_id,
    derive_link_public_id,
    derive_result_public_id,
    link_material_hash,
    preparation_material_hash,
    result_material_v2_hash,
)
from finance_core.parser_proposals.ai_model_admission import (
    AiModelAdmissionError,
    _record_ai_model_admission_denial_v2,
    get_ai_model_admission_decision_v2,
)
from finance_core.parser_proposals.ai_model_compatibility import (
    AUDIT_CALLER_ID,
    AUDIT_CALLER_KIND,
    AUDIT_PURPOSE,
    ModelCompatibilityError,
    compatibility_receipt_for_attempt,
    link_attempt_to_receipt,
    resolve_compatibility_receipt,
    verify_persisted_compatibility_receipt,
)
from finance_core.parser_proposals.ai_ocr_layout import load_ocr_layout
from finance_core.parser_proposals.ai_processing_status_v2 import derive_ai_processing_status_v2
from finance_core.parser_proposals.ai_response_validation import (
    _AMBIGUITY_FLAGS,
)
from finance_core.parser_proposals.ai_response_validation import (
    validate_ai_response as _validate_ai_response,
)
from finance_core.parser_proposals.ai_source_assessment import (
    _money_pair_candidates,
    _ocr_evidence_material,
    _parse_ocr_evidence_reference,
    _payload_field,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.lifecycle import (
    PARSED_PENDING_CONFIRMATION,
    TERMINAL_STATUSES,
    raw_intake_status_for_proposal_status,
)
from finance_core.parser_proposals.numeric_mirror import decimal_from_numeric_mirror
from finance_core.parser_proposals.receipt_total_parser import RECEIPT_AMBIGUITY_FLAGS
from finance_core.resources.finance_ai import FinanceAiPolicyError, load_asset_registry
from finance_core.sqlite_connection import ForeignKeysDisabledError, require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

MAX_SOURCE_BYTES = 24_576
MAX_SEGMENT_BYTES = 4_096
MAX_CATALOG_ENTRIES = 64
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536
MAX_CHILD_RESPONSE_BYTES = 16_384
MODEL_MAX_TOKENS = 1_024
MODEL_TEMPERATURE = 0
CALL_START_WINDOW_MS = 250
RESULT_WINDOW_MS = 35_000
INVOCATION_WINDOW_MS = 5_000
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = (
    "amount",
    "currency",
    "transaction_date",
    "merchant",
    "description",
    "account",
    "category",
)
_NORMAL_METADATA_FIELDS = {
    "returned_provider",
    "returned_model",
    "returned_agent_id",
    "audit_caller_kind",
    "audit_caller_id",
    "audit_caller_name",
    "audit_purpose",
    "audit_session_key_sha256",
    "usage_input_tokens",
    "usage_output_tokens",
}
_METADATA_REFUSAL_FIELDS = {
    "metadata_field",
    "metadata_reason",
    "metadata_code_unit_count",
    "metadata_sha256",
    "response_body_state",
}
_RESPONSE_BODY_FIELDS = {
    "retained": {"response_utf8_b64", "response_byte_count", "response_sha256"},
    "oversize": {"response_code_unit_count", "response_byte_count", "response_sha256"},
    "unencodable": {"response_code_unit_count", "response_utf16_sha256"},
    "resource_refused": {"response_code_unit_count"},
    "none": set(),
}
_METADATA_FIELDS = (
    "provider",
    "model",
    "agent",
    "caller_kind",
    "caller_id",
    "caller_name",
    "purpose",
    "session_key",
    "usage_input_tokens",
    "usage_output_tokens",
)
_METADATA_REASONS = frozenset({"invalid_type", "oversize", "unencodable", "out_of_range"})
AI_CONFIRMATION_CONFIDENCE_THRESHOLD = 0.8
_POLICY_REASONS = frozenset(
    {
        "unsupported_language",
        "mixed_language_incomplete",
        "deterministic_fields_incomplete",
        "conflicting_text_candidates",
        "receipt_ocr_fields_incomplete",
        "intent_classification_required",
    }
)


class AiFallbackServiceError(RuntimeError):
    """Typed refusal from the Finance-owned fallback service."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = None if details is None else dict(details)


class _CompatibilityReceiptAvailable(RuntimeError):
    """Abort a denial transaction when the exact receipt is now available."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        super().__init__("The exact compatibility receipt is now available.")
        self.receipt = dict(receipt)


def _row_dict(row: sqlite3.Row | tuple[Any, ...], description: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return {column[0]: value for column, value in zip(description, row, strict=True)}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _domain_hash_bytes(domain: str, value: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + value).hexdigest()


def _ocr_input_hash(source_projection_hash: str) -> str:
    return _domain_hash_bytes("finance-ai-ocr-input-v1", bytes.fromhex(source_projection_hash))


def _ocr_result_hash(normalized_payload_hash: str) -> str:
    return _domain_hash_bytes("finance-ai-ocr-result-v1", bytes.fromhex(normalized_payload_hash))


def _derive_ai_ocr_link_public_id(proposal_link_public_id: str) -> str:
    digest = hashlib.sha256(proposal_link_public_id.encode("ascii")).hexdigest()[:32]
    return "ropl_ai_" + digest


def _derive_child_public_id(result_public_id: str, normalized_payload_hash: str) -> str:
    return (
        "prop_ai_"
        + hashlib.sha256((result_public_id + normalized_payload_hash).encode("ascii")).hexdigest()[
            :32
        ]
    )


def _hash_material(domain: str, material: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\x00" + canonical_json_bytes(material)
    ).hexdigest()


def _sample_now_ms(fixed_now_ms: int | None) -> int:
    return int(time.time() * 1000) if fixed_now_ms is None else int(fixed_now_ms)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return canonical_json_bytes(value)
    except AiFallbackProvenanceValidationError as exc:
        raise AiFallbackServiceError("AI_FALLBACK_VALIDATION_REFUSED", str(exc)) from exc


def _policy_assets(repo_root: Path | None = None) -> dict[str, Any]:
    root = (
        Path(__file__).resolve().parents[1] / "resources" / "finance_ai"
        if repo_root is None
        else repo_root / "finance_core" / "resources" / "finance_ai"
    )
    try:
        registry = load_asset_registry(root)
        return {
            "registry": registry,
            "runtime": registry.json_asset("runtime_policy"),
            "intent": registry.json_asset("intent_policy"),
            "default": registry.json_asset("default_policy"),
            "deadline": registry.json_asset("deadline_policy"),
            "sqlite_money": registry.json_asset("sqlite_money_policy"),
            "sensitive": registry.json_asset("sensitive_text_policy"),
            "prompt": registry.prompt_text(),
        }
    except (FinanceAiPolicyError, OSError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_POLICY_REFUSED", "AI policy assets are invalid."
        ) from exc


def _asset_hash(assets: Mapping[str, Any], name: str) -> str:
    return assets["registry"].asset(name).sha256


def _scan_sensitive(text: str, policy: Mapping[str, Any]) -> str | None:
    for pattern in policy.get("reject_if_any_pattern_matches", []):
        if not isinstance(pattern, str):
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "Sensitive policy is invalid."
            )
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return "sensitive_text_detected"
        except re.error as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "Sensitive policy is invalid."
            ) from exc
    markers = policy.get(
        "configured_finance_secret_markers",
        policy.get("finance_secret_markers", policy.get("literal_markers", [])),
    )
    if not isinstance(markers, list) or any(
        not isinstance(marker, str) or not marker for marker in markers
    ):
        raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Sensitive policy is invalid.")
    lowered = text.casefold()
    if any(marker.casefold() in lowered for marker in markers):
        return "sensitive_text_detected"
    return None


def _bounded_prefix(text: str) -> str:
    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) <= MAX_SEGMENT_BYTES:
        return text
    prefix = encoded[:MAX_SEGMENT_BYTES]
    while True:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _projection(
    *,
    source_kind: str,
    segments: list[tuple[str, str]],
    reasons: list[str],
) -> tuple[dict[str, Any], list[dict[str, str]], str, str, int]:
    catalog: list[dict[str, str]] = []
    total = 0
    selected: list[dict[str, str]] = []
    for ordinal, (kind, text) in enumerate(segments, start=1):
        encoded = text.encode("utf-8", errors="strict")
        if len(catalog) >= MAX_CATALOG_ENTRIES or total + len(encoded) > MAX_SOURCE_BYTES:
            raise AiFallbackServiceError(
                "AI_FALLBACK_NOT_ELIGIBLE",
                "Fallback source exceeds the bounded projection limit.",
                details={
                    "eligibility_disposition": "manual_recovery",
                    "refusal_reason": "resource_refused",
                },
            )
        if len(encoded) > MAX_SEGMENT_BYTES:
            raise AiFallbackServiceError(
                "AI_FALLBACK_NOT_ELIGIBLE",
                "Fallback source segment exceeds the bounded projection limit.",
                details={
                    "eligibility_disposition": "manual_recovery",
                    "refusal_reason": "resource_refused",
                },
            )
        clipped = text
        ref = f"e{ordinal:04d}"
        entry = {"ref": ref, "kind": kind, "text": clipped}
        catalog.append(entry)
        selected.append({"ref": ref, "kind": kind})
        total += len(clipped.encode("utf-8"))
    if not catalog:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "No bounded source text is available for fallback.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    projection = {
        "projection_schema_version": "finance-ai-input-v1",
        "source_kind": source_kind,
        "evidence_catalog": catalog,
        "fallback_reasons": sorted(set(reasons)),
    }
    projection_bytes = _json_bytes(projection)
    if len(projection_bytes) > MAX_REQUEST_BYTES:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Fallback request projection exceeds the bounded limit.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "resource_refused",
            },
        )
    selection_hash = _hash_material("finance-ai-source-selection-v1", {"selected": selected})
    projection_hash = _sha256_bytes(projection_bytes)
    return projection, selected, projection_hash, selection_hash, total


def _source_segments(
    conn: sqlite3.Connection, intake: Mapping[str, Any], parent: Mapping[str, Any]
) -> tuple[str, list[tuple[str, str]]]:
    source_type = str(intake["source_type"])
    if source_type == "telegram_text":
        evidence = conn.execute(
            """
            SELECT public_id, raw_intake_record_id, evidence_reference
            FROM raw_intake_evidence
            WHERE raw_intake_record_id = ? AND evidence_type = 'raw_input'
            """,
            (intake["id"],),
        ).fetchall()
        if (
            parent.get("source_type") != "telegram_text"
            or intake.get("parser_output_id") != parent.get("id")
            or parent.get("source_public_id") != intake.get("public_id")
            or len(evidence) != 1
            or evidence[0]["raw_intake_record_id"] != intake["id"]
            or evidence[0]["evidence_reference"] != intake["public_id"]
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_NOT_ELIGIBLE",
                "Telegram text fallback requires a matching text proposal source.",
                details={
                    "eligibility_disposition": "manual_recovery",
                    "refusal_reason": "source_refused",
                },
            )
        text = str(intake["raw_input"])
        return "telegram_raw_text", [("raw_text", text)]
    if source_type not in {"telegram_image", "local_image"}:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "This source type is outside the S5e text-only fallback boundary.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    attachment_id = intake.get("attachment_id")
    attachment_hash = intake.get("attachment_hash")
    attachment_path = intake.get("attachment_path")
    if attachment_id is None or not isinstance(attachment_hash, str) or not attachment_hash:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Receipt fallback requires durable attachment identity and hash.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    attachment = conn.execute(
        "SELECT id, public_id, file_path, file_hash, mime_type FROM attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    if (
        attachment is None
        or attachment["file_hash"] != attachment_hash
        or attachment_path != attachment["file_path"]
        or parent.get("attachment_id") != attachment_id
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Receipt fallback attachment lineage is not exact.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    if source_type == "telegram_image":
        source_rows = conn.execute(
            """
            SELECT id, attachment_id, raw_intake_record_id, original_attachment_path,
                   content_hash, declared_mime_type
            FROM telegram_attachment_source
            WHERE raw_intake_record_id = ? AND attachment_id = ?
            """,
            (intake["id"], attachment_id),
        ).fetchall()
        source_path_key = "original_attachment_path"
    else:
        source_rows = conn.execute(
            """
            SELECT id, attachment_id, raw_intake_record_id, workspace_copy_path AS
                   original_attachment_path, content_hash, declared_mime_type
            FROM local_attachment_source
            WHERE raw_intake_record_id = ? AND attachment_id = ?
            """,
            (intake["id"], attachment_id),
        ).fetchall()
        source_path_key = "original_attachment_path"
    if (
        len(source_rows) != 1
        or source_rows[0]["attachment_id"] != attachment_id
        or source_rows[0]["raw_intake_record_id"] != intake["id"]
        or source_rows[0]["content_hash"] != attachment_hash
        or source_rows[0][source_path_key] != attachment_path
        or source_rows[0]["content_hash"] != attachment["file_hash"]
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Receipt fallback source evidence is missing or mismatched.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    link = conn.execute(
        """
        SELECT ropl.extraction_id, ext.public_id AS extraction_public_id,
               ext.attachment_id, ext.source_attachment_hash,
               ext.source_mime_type, ext.extraction_status,
               ext.normalized_result_hash,
               ropl.public_id AS proposal_link_public_id,
               ropl.parser_output_id AS proposal_link_parser_output_id,
               ropl.parser_contract_version AS proposal_link_contract_version,
               ropl.link_role AS proposal_link_role
        FROM receipt_ocr_proposal_links AS ropl
        JOIN receipt_ocr_extractions AS ext ON ext.id = ropl.extraction_id
        WHERE ropl.parser_output_id = ?
          AND ropl.link_role IN ('initial', 'superseding_correction')
        ORDER BY ropl.id
        """,
        (parent["id"],),
    ).fetchall()
    if (
        len(link) != 1
        or link[0]["extraction_status"] != "succeeded"
        or link[0]["attachment_id"] != attachment_id
        or link[0]["source_attachment_hash"] != attachment_hash
        or link[0]["source_attachment_hash"] != attachment["file_hash"]
        or (
            source_rows[0]["declared_mime_type"] is not None
            and link[0]["source_mime_type"] != source_rows[0]["declared_mime_type"]
        )
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "A succeeded, source-bound local OCR extraction is required for receipt fallback.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    _verify_ocr_parent_binding(conn, parent=parent, link=dict(link[0]))
    blocks = conn.execute(
        """
        SELECT sequence_index, normalized_text
        FROM receipt_ocr_blocks
        WHERE extraction_id = ?
        ORDER BY sequence_index, id
        """,
        (link[0]["extraction_id"],),
    ).fetchall()
    if not blocks:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "OCR fallback requires at least one persisted normalized text block.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    return "receipt_local_ocr_text", [("ocr_block", str(row["normalized_text"])) for row in blocks]


def _verify_ocr_parent_binding(
    conn: sqlite3.Connection,
    *,
    parent: Mapping[str, Any],
    link: Mapping[str, Any],
) -> None:
    """Bind payload and relational OCR evidence to the actual extraction row."""
    try:
        payload, _completion_id, _version = resolve_effective_payload(conn, dict(parent))
    except Exception as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "OCR parent payload cannot be re-read."
        ) from exc
    ocr_evidence = payload.get("ocr_evidence")
    if not isinstance(ocr_evidence, Mapping):
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "OCR parent evidence is missing.")
    if (
        link.get("proposal_link_parser_output_id") != parent["id"]
        or not isinstance(link.get("proposal_link_public_id"), str)
        or not link["proposal_link_public_id"]
        or not isinstance(link.get("proposal_link_contract_version"), str)
        or not link["proposal_link_contract_version"]
        or link.get("proposal_link_role") not in {"initial", "superseding_correction"}
        or ocr_evidence.get("extraction_public_id") != link["extraction_public_id"]
        or ocr_evidence.get("normalized_result_hash") != link["normalized_result_hash"]
        or ocr_evidence.get("extraction_status") != link["extraction_status"]
        or link["extraction_status"] != "succeeded"
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "OCR parent extraction identity does not verify."
        )
    block_rows = conn.execute(
        """
        SELECT sequence_index
        FROM receipt_ocr_blocks
        WHERE extraction_id = ?
        """,
        (link["extraction_id"],),
    ).fetchall()
    actual_indexes = {int(row["sequence_index"]) for row in block_rows}
    evidence_rows = conn.execute(
        """
        SELECT field_name, proposed_value, evidence_source_type, evidence_reference
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ? AND evidence_source_type = 'ocr'
        ORDER BY id
        """,
        (parent["id"],),
    ).fetchall()
    payload_evidence = payload.get("field_evidence")
    if not isinstance(payload_evidence, list):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "OCR parent field evidence is missing."
        )
    expected_rows = [
        item
        for item in payload_evidence
        if isinstance(item, Mapping) and item.get("evidence_source_type") == "ocr"
    ]
    if (
        len(expected_rows) != len(payload_evidence)
        or len({str(item.get("field_name")) for item in expected_rows}) != len(expected_rows)
        or len(evidence_rows) != len(expected_rows)
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "OCR relational evidence cardinality does not verify."
        )
    expected_by_field = {str(item["field_name"]): item for item in expected_rows}
    for row in evidence_rows:
        expected = expected_by_field.get(str(row["field_name"]))
        if expected is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "OCR relational evidence has an unexpected field.",
            )
        parsed = _parse_ocr_evidence_reference(row["evidence_reference"])
        expected_material = _ocr_evidence_material(expected)
        if (
            row["evidence_source_type"] != "ocr"
            or row["proposed_value"] != expected.get("proposed_value")
            or parsed is None
            or expected_material != parsed
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "OCR relational evidence does not match the parent payload.",
            )
        indexes, extraction_public_id, normalized_result_hash = parsed
        if (
            extraction_public_id != link["extraction_public_id"]
            or normalized_result_hash != link["normalized_result_hash"]
            or not indexes.issubset(actual_indexes)
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "OCR relational evidence is not bound to the persisted extraction.",
            )


def _controlled_payer_match(
    text: str,
    policy: Mapping[str, Any],
) -> tuple[list[re.Match[str]], bool]:
    """Return every controlled payer span and whether any prefix is malformed."""
    raw_patterns = policy.get(
        "controlled_payer_patterns",
        policy.get("payer_syntax_patterns", []),
    )
    if not isinstance(raw_patterns, list):
        raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid.")
    payer_matches: list[re.Match[str]] = []
    for raw_pattern in raw_patterns:
        if not isinstance(raw_pattern, str):
            raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid.")
        try:
            payer_matches.extend(re.finditer(raw_pattern, text, flags=re.IGNORECASE | re.UNICODE))
        except re.error as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid."
            ) from exc

    raw_prefixes = policy.get("payer_prefix_patterns", [])
    if not isinstance(raw_prefixes, list):
        raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid.")
    prefix_matches: list[re.Match[str]] = []
    for raw_pattern in raw_prefixes:
        if not isinstance(raw_pattern, str):
            raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid.")
        try:
            prefix_matches.extend(re.finditer(raw_pattern, text, flags=re.IGNORECASE | re.UNICODE))
        except re.error as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid."
            ) from exc
    malformed_prefix = any(
        not any(
            payer_match.start() == prefix_match.start() and payer_match.end() >= prefix_match.end()
            for payer_match in payer_matches
        )
        for prefix_match in prefix_matches
    )
    payer_matches.sort(key=lambda match: (match.start(), match.end()))
    raw_follow_patterns = policy.get("payer_clause_follow_patterns", [])
    if not isinstance(raw_follow_patterns, list) or any(
        not isinstance(pattern, str) or not pattern for pattern in raw_follow_patterns
    ):
        raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid.")
    unsupported_clause_tail = False
    for payer_match in payer_matches:
        suffix = text[payer_match.end() :]
        try:
            if not any(
                re.match(pattern, suffix, flags=re.IGNORECASE | re.UNICODE)
                for pattern in raw_follow_patterns
            ):
                unsupported_clause_tail = True
                break
        except re.error as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid."
            ) from exc
    return payer_matches, malformed_prefix or unsupported_clause_tail


def _intent_result(text: str, policy: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    def policy_tokens(key: str) -> list[str]:
        raw_tokens = policy.get(key, [])
        if not isinstance(raw_tokens, list) or any(
            not isinstance(token, str) or not token.strip() for token in raw_tokens
        ):
            raise AiFallbackServiceError("AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid.")
        return list(raw_tokens)

    positive = policy_tokens("positive_personal_intent_tokens")
    deny = policy_tokens("deny_intent_tokens")
    lowered = text.casefold()

    def matches(token: str) -> bool:
        folded = token.casefold()
        if any("\u4e00" <= char <= "\u9fff" for char in folded):
            return folded in lowered
        escaped = re.escape(folded).replace(r"\ ", r"\s+")
        return re.search(rf"(?<![\w]){escaped}(?![\w])", lowered, flags=re.UNICODE) is not None

    payer_matches, malformed_payer = _controlled_payer_match(text, policy)
    if malformed_payer:
        return "deny", "child_eligible", []
    intent_text = text
    if payer_matches:
        try:
            payer_names = policy.get("self_payer_names", [])
            if not isinstance(payer_names, list) or any(
                not isinstance(name, str) or not name.strip() for name in payer_names
            ):
                raise ValueError("self payer names are invalid")
            self_payer_names = {name.casefold().strip() for name in payer_names}
            payer_spans: set[tuple[int, int]] = set()
            for payer_match in payer_matches:
                payer_name = payer_match.groupdict().get("payer")
                if payer_name is None and payer_match.lastindex is not None:
                    payer_name = payer_match.group(1)
                if not isinstance(payer_name, str) or not payer_name.strip():
                    return "deny", "child_eligible", []
                if payer_name.casefold().strip() not in self_payer_names:
                    return "deny", "child_eligible", []
                payer_spans.add(payer_match.span())

            # Every self-payer phrase is attribution only. Removing all spans
            # prevents one attribution from supplying positive intent or from
            # hiding a later controlled payer span.
            merged_spans: list[tuple[int, int]] = []
            for start, end in sorted(payer_spans):
                if merged_spans and start <= merged_spans[-1][1]:
                    previous_start, previous_end = merged_spans[-1]
                    merged_spans[-1] = (previous_start, max(previous_end, end))
                else:
                    merged_spans.append((start, end))
            parts: list[str] = []
            cursor = 0
            for start, end in merged_spans:
                parts.extend((text[cursor:start], " "))
                cursor = end
            parts.append(text[cursor:])
            intent_text = "".join(parts)
            lowered = intent_text.casefold()
        except (IndexError, ValueError, TypeError) as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "Intent policy is invalid."
            ) from exc

    if any(matches(token) for token in deny):
        return "deny", "child_eligible", []
    if any(matches(token) for token in positive):
        return "positive", "child_eligible", []
    return "unknown", "classification_only", ["intent_classification_required"]


def verify_deterministic_intent_policy(proposal: Mapping[str, Any]) -> str:
    """Recheck preserved source intent before a deterministic lifecycle step."""
    raw_text = proposal.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "Deterministic proposal has no preserved raw source for intent verification.",
        )
    assets = _policy_assets()
    result, _mode, _reasons = _intent_result(raw_text, assets["intent"])
    if result == "deny":
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "Deterministic proposal is refused by the Finance-owned intent deny policy.",
        )
    return result


def requires_deterministic_intent_policy(proposal: Mapping[str, Any]) -> bool:
    """Return whether the text-only deterministic policy owns this proposal."""
    transaction_type = proposal.get("transaction_type")
    if transaction_type is None:
        # ParserProposalRepository exposes the persisted payload columns as
        # JSON strings; use the authoritative normalized payload when the
        # denormalized convenience field is absent.
        for field in ("normalized_payload", "parsed_payload"):
            raw_payload = proposal.get(field)
            if not isinstance(raw_payload, str):
                continue
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("transaction_type") is not None:
                transaction_type = payload["transaction_type"]
                break
    return (
        proposal.get("source_type") == "telegram_text"
        and proposal.get("parser_name") != "finance_ai_proposal"
        # The deterministic parser already owns shared-expense proposals.
        # They remain human-confirmable and are rejected by the dedicated
        # shared-expense conversion boundary; S5e must not turn that existing
        # proposal lifecycle into an unavailable state.
        and transaction_type != "shared_expense"
    )


def _forbidden_parent_fields(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return destination fields that are not the exact JSON null value."""
    return tuple(
        field for field in ("description", "account", "category") if payload.get(field) is not None
    )


def _sqlite_numeric_roundtrip_matches(
    conn: sqlite3.Connection,
    canonical_amount: str,
) -> bool:
    """Apply the receipt NUMERIC mirror contract before an AI child write."""
    row = conn.execute("SELECT CAST(? AS NUMERIC)", (canonical_amount,)).fetchone()
    if row is None:
        return False
    mirrored = decimal_from_numeric_mirror(row[0])
    try:
        expected = money_decimal(canonical_amount)
    except MoneyValidationError:
        return False
    return mirrored is not None and mirrored == expected


def _field_state_hash(payload: Mapping[str, Any]) -> str:
    state = {
        field: payload.get(field, payload.get("date") if field == "transaction_date" else None)
        for field in _FIELDS
    }
    return _hash_material("finance-ai-source-field-state-v1", state)


def _projection_catalog(attempt: Mapping[str, Any]) -> dict[str, str]:
    try:
        request = json.loads(bytes(attempt["request_blob"]).decode("utf-8"))
        content = json.loads(request["messages"][0]["content"])
        catalog = content["evidence_catalog"]
        if not isinstance(catalog, list):
            raise ValueError("evidence catalog is not a list")
        return {
            str(entry["ref"]): str(entry["text"])
            for entry in catalog
            if isinstance(entry, dict) and "ref" in entry and "text" in entry
        }
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Prepared projection cannot be re-read."
        ) from exc


def _has_conflicting_text_money_candidates(catalog: Mapping[str, str]) -> bool:
    """Return whether text evidence contains more than one distinct money pair."""
    pairs = {
        (str(candidate["amount"]), str(candidate["currency"]))
        for candidate in _money_pair_candidates(catalog)
    }
    return len(pairs) > 1


def _verify_attempt_source_material(
    conn: sqlite3.Connection,
    *,
    intake: Mapping[str, Any],
    parent: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> None:
    if intake.get("parser_output_id") != parent.get("id"):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback raw-intake proposal pointer has changed."
        )
    source_kind, segments = _source_segments(conn, intake, parent)
    if source_kind != attempt["source_kind"]:
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "AI fallback source kind has drifted.")
    try:
        reasons = json.loads(str(attempt["eligibility_reasons_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback eligibility reasons cannot be verified."
        ) from exc
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback eligibility reasons cannot be verified."
        )
    _projection_value, selected, projection_hash, selection_hash, source_bytes = _projection(
        source_kind=source_kind,
        segments=segments,
        reasons=sorted(set(reasons)),
    )
    if (
        projection_hash != attempt["source_projection_hash"]
        or selection_hash != attempt["source_selection_manifest_hash"]
        or source_bytes != int(attempt["source_projection_byte_count"])
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source projection has drifted."
        )


def _verify_current_parent_state(
    conn: sqlite3.Connection,
    *,
    intake_id: int,
    parent_id: int,
    expected_intake_public_id: str,
    expected_parent_public_id: str,
    expected_parent_version: int,
    expected_parent_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    intake_cursor = conn.execute("SELECT * FROM raw_intake_records WHERE id = ?", (intake_id,))
    parent_cursor = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (parent_id,))
    intake_row = intake_cursor.fetchone()
    parent_row = parent_cursor.fetchone()
    if intake_row is None or parent_row is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent or raw-intake source disappeared."
        )
    intake = _row_dict(intake_row, intake_cursor.description)
    parent = _row_dict(parent_row, parent_cursor.description)
    if (
        intake["public_id"] != expected_intake_public_id
        or intake.get("parser_output_id") != parent_id
        or parent["public_id"] != expected_parent_public_id
        or parent["parse_status"] != PARSED_PENDING_CONFIRMATION
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent lifecycle or intake binding changed."
        )
    _assert_single_raw_intake_binding(
        conn,
        parser_output_id=parent_id,
        expected_intake_id=intake_id,
    )
    _payload, _completion_id, version = resolve_effective_payload(conn, parent)
    content_hash = compute_effective_proposal_content_hash(conn, parent)
    if version != expected_parent_version or content_hash != expected_parent_hash:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent content changed before invocation."
        )
    return intake, parent


def _verify_attempt_current_parent_state(
    conn: sqlite3.Connection,
    attempt: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    intake_cursor = conn.execute(
        "SELECT * FROM raw_intake_records WHERE id = ?", (attempt["raw_intake_record_id"],)
    )
    parent_cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (attempt["parent_parser_output_id"],)
    )
    intake_row = intake_cursor.fetchone()
    parent_row = parent_cursor.fetchone()
    if intake_row is None or parent_row is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent or raw-intake source disappeared."
        )
    intake = _row_dict(intake_row, intake_cursor.description)
    parent = _row_dict(parent_row, parent_cursor.description)
    if (
        intake.get("parser_output_id") != parent["id"]
        or parent["parse_status"] != PARSED_PENDING_CONFIRMATION
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent lifecycle or intake binding changed."
        )
    _assert_single_raw_intake_binding(
        conn,
        parser_output_id=int(parent["id"]),
        expected_intake_id=int(attempt["raw_intake_record_id"]),
    )
    _payload, _completion_id, version = resolve_effective_payload(conn, parent)
    content_hash = compute_effective_proposal_content_hash(conn, parent)
    if (
        version != int(attempt["parent_proposal_version"])
        or content_hash != attempt["parent_effective_content_hash"]
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent content changed before invocation."
        )
    _verify_attempt_source_material(conn, intake=intake, parent=parent, attempt=attempt)
    return intake, parent


def _assert_single_raw_intake_binding(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    expected_intake_id: int,
) -> None:
    rows = conn.execute(
        """
        SELECT id
        FROM raw_intake_records
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output_id,),
    ).fetchall()
    if len(rows) != 1 or int(rows[0]["id"]) != expected_intake_id:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "Fallback proposal must retain exactly one raw-intake binding.",
        )


def _assert_fallback_raw_intake_lineage(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    child_parser_output_id: int | None = None,
    current_parser_output_id: int | None = None,
) -> None:
    """Require one current route across the complete fallback parent/child edge."""
    parent_id = int(attempt["parent_parser_output_id"])
    edge_id = parent_id if child_parser_output_id is None else int(child_parser_output_id)
    expected_id = edge_id if current_parser_output_id is None else int(current_parser_output_id)
    rows = conn.execute(
        """
        SELECT id, parser_output_id
        FROM raw_intake_records
        WHERE parser_output_id IN (?, ?, ?)
        ORDER BY id
        """,
        (parent_id, edge_id, expected_id),
    ).fetchall()
    if (
        len(rows) != 1
        or int(rows[0]["id"]) != int(attempt["raw_intake_record_id"])
        or int(rows[0]["parser_output_id"]) != expected_id
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "Fallback parent/child lineage must retain exactly one raw-intake binding.",
        )


def _assert_result_raw_intake_binding(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    result: Mapping[str, Any],
) -> sqlite3.Row | None:
    links = conn.execute(
        """
        SELECT parser_output_id
        FROM ai_fallback_proposal_links
        WHERE result_id = ?
        ORDER BY id
        """,
        (result["id"],),
    ).fetchall()
    proposal_created = result["result_status"] == "proposal_created"
    if proposal_created:
        if len(links) != 1:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "A proposal-created result requires exactly one child link.",
            )
        parser_output_id = int(links[0]["parser_output_id"])
    else:
        if links:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "A non-child result cannot carry a proposal link.",
            )
        parser_output_id = int(attempt["parent_parser_output_id"])
    _assert_fallback_raw_intake_lineage(
        conn,
        attempt=attempt,
        child_parser_output_id=(
            parser_output_id
            if parser_output_id != int(attempt["parent_parser_output_id"])
            else None
        ),
    )
    return links[0] if links else None


def _selected_money_pair(
    amount: str,
    currency: str,
    catalog: Mapping[str, str],
    refs: set[str] | None = None,
    *,
    source_kind: str = "telegram_text",
    parent_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    matches = [
        candidate
        for candidate in _money_pair_candidates(
            catalog,
            source_kind=source_kind,
            parent_payload=parent_payload,
        )
        if candidate["amount"] == amount
        and candidate["currency"] == currency
        and (refs is None or refs.intersection(candidate["refs"]))
    ]
    return matches[0] if len(matches) == 1 else None


def _money_pair_identity(candidate: Mapping[str, str | int]) -> str:
    identity = candidate.get("pair_identity")
    if not isinstance(identity, str):
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "Money pair identity is invalid.")
    return identity


def _deterministic_ambiguity_flags(
    attempt: Mapping[str, Any],
    parent_payload: Mapping[str, Any],
    effective_payload: Mapping[str, Any],
) -> set[str]:
    flags: set[str] = set()
    field_missing_flags = {
        "amount": "missing_amount",
        "currency": "missing_currency",
        "transaction_date": "missing_date",
        "merchant": "missing_merchant_or_description",
    }
    for field, flag in field_missing_flags.items():
        if _payload_field(effective_payload, field) in (None, ""):
            flags.add(flag)

    try:
        reasons = json.loads(str(attempt["eligibility_reasons_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback eligibility reasons cannot be verified."
        ) from exc
    if "conflicting_text_candidates" in reasons:
        flags.update({"source_conflict", "ambiguous_amount"})

    ocr_mapping = {
        "total_not_found": {"missing_amount"},
        "conflicting_total_candidates": {"ambiguous_amount", "source_conflict"},
        "currency_not_determined": {"missing_currency"},
        "ambiguous_currency_symbol": {"ambiguous_currency", "source_conflict"},
        "unsupported_currency_for_amount": {"ambiguous_currency", "source_conflict"},
        "transaction_date_not_found": {"missing_date"},
        "ambiguous_transaction_date": {"ambiguous_date", "source_conflict"},
        "conflicting_date_candidates": {"ambiguous_date", "source_conflict"},
        "merchant_not_determined": {"missing_merchant_or_description"},
    }
    parent_flags = parent_payload.get("ambiguity_flags", [])
    if isinstance(parent_flags, list):
        for raw_flag in parent_flags:
            if isinstance(raw_flag, str):
                flags.update(ocr_mapping.get(raw_flag, set()))
    return flags


def _normalized_ambiguity_flags(
    attempt: Mapping[str, Any],
    parent_payload: Mapping[str, Any],
    effective_payload: Mapping[str, Any],
    model_flags: list[str],
) -> list[str]:
    generic = set(model_flags) | _deterministic_ambiguity_flags(
        attempt, parent_payload, effective_payload
    )
    if attempt["source_kind"] != "receipt_local_ocr_text":
        return sorted(generic)

    receipt_flags = {
        flag
        for flag in parent_payload.get("ambiguity_flags", [])
        if isinstance(flag, str) and flag in RECEIPT_AMBIGUITY_FLAGS
    }
    mapping = {
        "missing_amount": "total_not_found",
        "ambiguous_amount": "conflicting_total_candidates",
        "missing_currency": "currency_not_determined",
        "ambiguous_currency": "ambiguous_currency_symbol",
        "missing_date": "transaction_date_not_found",
        "ambiguous_date": "ambiguous_transaction_date",
        "missing_merchant_or_description": "merchant_not_determined",
        "ambiguous_merchant": "merchant_not_determined",
    }
    for flag in generic:
        mapped = mapping.get(flag)
        if mapped is not None:
            receipt_flags.add(mapped)
    return sorted(receipt_flags)


def _planned_child_evidence(
    response_payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    attempt_public_id: str,
    parent_public_id: str,
    parent_effective_content_hash: str,
    catalog: Mapping[str, str],
    source_kind: str,
    parent_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in ("amount", "currency", "transaction_date", "merchant"):
        value = normalized[field]
        parent_value = _payload_field(parent_payload, field)
        refs = response_payload["field_evidence_refs"][field]
        if value is not None and value == parent_value:
            rows.append(
                {
                    "field_name": field,
                    "proposed_value": value,
                    "confidence_bps": None,
                    "evidence_source_type": "system",
                    "evidence_reference": json.dumps(
                        {
                            "field": field,
                            "inherited_from_parent": parent_public_id,
                            "parent_effective_content_hash": parent_effective_content_hash,
                        },
                        sort_keys=True,
                    ),
                    "notes": (
                        "Deterministic parent field retained alongside AI evidence; "
                        "human confirmation required."
                    ),
                }
            )
        if refs:
            for ref in refs:
                rows.append(
                    {
                        "field_name": field,
                        "proposed_value": value,
                        "confidence_bps": response_payload["field_confidence_bps"][field] or 0,
                        "evidence_source_type": "ai_model",
                        "evidence_reference": json.dumps(
                            {"attempt_public_id": attempt_public_id, "ref": ref}, sort_keys=True
                        ),
                        "notes": "AI proposal evidence; human confirmation required.",
                    }
                )
    amount = normalized["amount"]
    currency = normalized["currency"]
    if amount is not None and currency is not None:
        response_pair_refs = set(response_payload["field_evidence_refs"]["amount"]) & set(
            response_payload["field_evidence_refs"]["currency"]
        )
        inherited_pair = response_payload["amount"] is None and response_payload["currency"] is None
        pair = _selected_money_pair(
            str(amount),
            str(currency),
            catalog,
            response_pair_refs or None,
            source_kind=source_kind,
            parent_payload=parent_payload,
        )
        if pair is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Effective amount/currency pair is not source-bound."
            )
        rows.append(
            {
                "field_name": "amount_currency_pair",
                "proposed_value": f"{amount} {currency}",
                "confidence_bps": min(
                    response_payload["field_confidence_bps"]["amount"] or 0,
                    response_payload["field_confidence_bps"]["currency"] or 0,
                ),
                "evidence_source_type": "system" if inherited_pair else "ai_model",
                "evidence_reference": json.dumps(
                    {
                        "pair_identity": _money_pair_identity(pair),
                        "evidence_refs": list(pair["refs"]),
                        "source_span": list(pair["source_span"]),
                    },
                    sort_keys=True,
                ),
                "notes": (
                    "Sealed Finance-derived amount/currency candidate pair; "
                    "human confirmation required."
                ),
            }
        )
    return rows


def _child_confidence_score(
    response_payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> float:
    scores: list[int] = []
    for field in ("amount", "currency", "transaction_date", "merchant"):
        if normalized[field] is None:
            continue
        model_value = response_payload[field]
        if model_value is None:
            scores.append(10_000)
        else:
            score = response_payload["field_confidence_bps"][field]
            scores.append(0 if score is None else int(score))
    return min(scores, default=0) / 10_000


def _audit_timestamp(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _ai_child_source_evidence_references(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    result_public_id: str,
    child_id: int,
    child_public_id: str,
    link_public_id: str,
) -> tuple[str, ...]:
    """Return stable public identities needed to reconstruct the AI source edge."""
    intake = conn.execute(
        "SELECT public_id, attachment_id FROM raw_intake_records WHERE id = ?",
        (attempt["raw_intake_record_id"],),
    ).fetchone()
    parent = conn.execute(
        "SELECT public_id, attachment_id FROM parser_outputs WHERE id = ?",
        (attempt["parent_parser_output_id"],),
    ).fetchone()
    if intake is None or parent is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI child source identities are missing."
        )
    references = [
        f"attempt:{attempt['attempt_public_id']}",
        f"raw-intake:{intake['public_id']}",
        f"proposal:{parent['public_id']}",
        f"result:{result_public_id}",
        f"proposal:{child_public_id}",
        f"proposal-link:{link_public_id}",
    ]
    attachment_id = parent["attachment_id"]
    if attachment_id is not None:
        attachment = conn.execute(
            "SELECT public_id FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if attachment is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "AI child attachment identity is missing."
            )
        references.append(f"attachment:{attachment['public_id']}")
    parent_ocr = conn.execute(
        """
        SELECT public_id, extraction_id
        FROM receipt_ocr_proposal_links
        WHERE parser_output_id = ?
          AND link_role IN ('initial', 'superseding_correction')
        ORDER BY id
        """,
        (attempt["parent_parser_output_id"],),
    ).fetchall()
    child_ocr = conn.execute(
        """
        SELECT public_id, extraction_id
        FROM receipt_ocr_proposal_links
        WHERE parser_output_id = ? AND link_role = 'ai_fallback'
        ORDER BY id
        """,
        (child_id,),
    ).fetchall()
    for row in (*parent_ocr, *child_ocr):
        references.append(f"ocr-link:{row['public_id']}")
        extraction = conn.execute(
            "SELECT public_id FROM receipt_ocr_extractions WHERE id = ?",
            (row["extraction_id"],),
        ).fetchone()
        if extraction is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "AI child OCR extraction identity is missing."
            )
        references.append(f"ocr-extraction:{extraction['public_id']}")
    evidence_rows = conn.execute(
        """
        SELECT field_name
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (child_id,),
    ).fetchall()
    field_ordinals: dict[str, int] = {}
    for row in evidence_rows:
        field_name = str(row["field_name"])
        ordinal = field_ordinals.get(field_name, 0) + 1
        field_ordinals[field_name] = ordinal
        references.append(f"field-evidence:{child_public_id}:{field_name}:{ordinal}")
    return tuple(sorted(set(references)))


def _append_ai_child_audit_event(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    result_public_id: str,
    child_id: int,
    child_public_id: str,
    child_content_hash: str,
    link_public_id: str,
    link_material_hash_value: str,
    decision_at_ms: int,
) -> None:
    event_type = "parser_proposal_ai_fallback_child_created"
    event_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=child_public_id,
        event_type=event_type,
        causation_public_id=result_public_id,
    )
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=child_public_id,
            event_type=event_type,
            event_payload={
                "attempt_public_id": attempt["attempt_public_id"],
                "result_public_id": result_public_id,
                "child_proposal_public_id": child_public_id,
                "parent_effective_content_hash": attempt["parent_effective_content_hash"],
                "child_effective_content_hash": child_content_hash,
                "proposal_link_public_id": link_public_id,
                "proposal_link_material_hash": link_material_hash_value,
            },
            previous_state=None,
            new_state={
                "parse_status": PARSED_PENDING_CONFIRMATION,
                "raw_intake_status": raw_intake_status_for_proposal_status(
                    PARSED_PENDING_CONFIRMATION
                ),
                "proposal_content_hash": child_content_hash,
                "conversion_status": "not_converted",
            },
            actor_type="ai",
            actor_public_id=str(attempt["expected_agent_id"]),
            correlation_public_id=str(attempt["attempt_public_id"]),
            causation_public_id=result_public_id,
            created_at=_audit_timestamp(decision_at_ms),
            source_evidence_references=_ai_child_source_evidence_references(
                conn,
                attempt=attempt,
                result_public_id=result_public_id,
                child_id=child_id,
                child_public_id=child_public_id,
                link_public_id=link_public_id,
            ),
        ),
    )


def _effective_fields(payload: Mapping[str, Any]) -> tuple[bool, bool]:
    amount = payload.get("amount")
    currency = payload.get("currency")
    merchant = payload.get("merchant")
    transaction_date = payload.get("transaction_date", payload.get("date"))
    complete = all(
        value is not None and (not isinstance(value, str) or value.strip())
        for value in (amount, currency, merchant, transaction_date)
    )
    return complete, payload.get("transaction_type") == "personal_expense" or payload.get(
        "intent"
    ) == "personal_expense"


def _attempt_row(conn: sqlite3.Connection, attempt_public_id: str) -> dict[str, Any] | None:
    cursor = conn.execute(
        "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?", (attempt_public_id,)
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(row, cursor.description)


def _replay_view(
    conn: sqlite3.Connection, attempt: Mapping[str, Any], now_ms: int
) -> dict[str, Any]:
    result = conn.execute(
        "SELECT * FROM ai_fallback_results WHERE attempt_id = ?", (attempt["id"],)
    ).fetchone()
    if result is not None:
        return {"view_kind": "result", "result": _result_response(conn, dict(result))}
    claim = conn.execute(
        "SELECT 1 FROM ai_fallback_invocation_claims WHERE attempt_id = ?", (attempt["id"],)
    ).fetchone()
    if claim is None and now_ms >= int(attempt["invoke_not_after_ms"]):
        return {
            "view_kind": "attempt",
            "attempt_status": "not_invoked",
            "recovery_disposition": "resend_new_intake_after_not_invoked",
        }
    if claim is not None and now_ms >= int(attempt["result_not_after_ms"]):
        return {
            "view_kind": "attempt",
            "attempt_status": "outcome_unknown",
            "recovery_disposition": "resend_new_intake_after_unknown_outcome",
        }
    return {
        "view_kind": "attempt",
        "attempt_status": "claimed_unresolved" if claim is not None else "prepared_unresolved",
        "recovery_disposition": "pending_existing_attempt",
    }


def _prepared_replay_response(
    conn: sqlite3.Connection,
    attempt: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    return {
        "attempt_public_id": attempt["attempt_public_id"],
        "claim_disposition": "do_not_claim",
        "prepared_at_ms": attempt["prepared_at_ms"],
        "invoke_not_after_ms": attempt["invoke_not_after_ms"],
        "result_not_after_ms": attempt["result_not_after_ms"],
        "request_sha256": attempt["request_sha256"],
        "replay_view": _replay_view(conn, attempt, now_ms),
    }


def _require_attempt_receipt_binding(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    receipt: Mapping[str, Any] | None,
) -> None:
    bound = _verified_compatibility_receipt_for_attempt(conn, attempt_id=attempt_id)
    if receipt is None and bound is not None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "A v2 attempt cannot replay through the v1 boundary."
        )
    if receipt is not None and (
        bound is None or bound["receipt_public_id"] != receipt["receipt_public_id"]
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "The attempt is not bound to the exact accepted receipt."
        )


def _verified_compatibility_receipt_for_attempt(
    conn: sqlite3.Connection, *, attempt_id: int
) -> dict[str, Any] | None:
    try:
        return compatibility_receipt_for_attempt(conn, attempt_id=attempt_id)
    except ModelCompatibilityError as exc:
        raise AiFallbackServiceError(exc.code, str(exc)) from exc


def _prepare_ai_fallback_impl(
    conn: sqlite3.Connection,
    *,
    intake_public_id: str,
    now_ms: int | None = None,
    repo_root: Path | None = None,
    compatibility_receipt: Mapping[str, Any] | None = None,
    eligibility_only: bool = False,
) -> dict[str, Any]:
    """Prepare one immutable AI fallback attempt, without claiming a call."""
    require_staging_database(conn)
    now = _sample_now_ms(now_ms)
    intake_cursor = conn.execute(
        "SELECT * FROM raw_intake_records WHERE public_id = ?", (intake_public_id,)
    )
    intake_row = intake_cursor.fetchone()
    if intake_row is None:
        raise AiFallbackServiceError("INTAKE_NOT_FOUND", "Raw intake was not found.")
    intake = _row_dict(intake_row, intake_cursor.description)
    existing_cursor = conn.execute(
        "SELECT * FROM ai_fallback_attempts WHERE raw_intake_record_id = ?",
        (intake["id"],),
    )
    existing_row = existing_cursor.fetchone()
    if existing_row is not None:
        # A committed result may have repointed the intake to an AI child, but
        # an unresolved attempt is equally one-shot. Never recompute or insert
        # a second attempt for the same raw intake.
        existing_attempt = _row_dict(existing_row, existing_cursor.description)
        if eligibility_only:
            return {"eligibility_disposition": "existing_attempt"}
        _require_attempt_receipt_binding(
            conn,
            attempt_id=int(existing_attempt["id"]),
            receipt=compatibility_receipt,
        )
        existing_result = conn.execute(
            """
            SELECT * FROM ai_fallback_results
            WHERE attempt_id = ?
            """,
            (existing_attempt["id"],),
        ).fetchone()
        if existing_result is not None:
            _verify_committed_result_replay(
                conn,
                attempt=existing_attempt,
                result=dict(existing_result),
            )
        else:
            _assert_fallback_raw_intake_lineage(conn, attempt=existing_attempt)
            _verify_attempt_provenance_material(conn, attempt=existing_attempt)
        return _prepared_replay_response(
            conn,
            existing_attempt,
            now,
        )
    assets = _policy_assets(repo_root)
    if compatibility_receipt is None:
        runtime = dict(assets["runtime"])
        runtime_policy_version = runtime["version"]
        runtime_policy_hash = _asset_hash(assets, "runtime_policy")
    else:
        if compatibility_receipt["prompt_sha256"] != _asset_hash(assets, "prompt"):
            raise AiFallbackServiceError(
                "AI_FALLBACK_POLICY_REFUSED", "The accepted receipt prompt does not match."
            )
        runtime = {
            "provider": compatibility_receipt["canonical_provider"],
            "model": compatibility_receipt["canonical_model"],
            "agent_id": compatibility_receipt["agent_id"],
            "audit_caller_kind": AUDIT_CALLER_KIND,
            "audit_caller_id": AUDIT_CALLER_ID,
            "audit_caller_name": None,
            "audit_purpose": AUDIT_PURPOSE,
        }
        runtime_policy_version = compatibility_receipt["projection_policy_version"]
        runtime_policy_hash = compatibility_receipt["config_projection_hash"]
    intake_bindings = conn.execute(
        "SELECT COUNT(*) FROM raw_intake_records WHERE parser_output_id = ?",
        (intake.get("parser_output_id"),),
    ).fetchone()
    if intake_bindings is None or int(intake_bindings[0]) != 1:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Fallback requires exactly one raw intake binding.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "integrity_refused",
            },
        )
    if intake.get("parser_output_id") is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "A deterministic proposal is required before fallback.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "source_refused",
            },
        )
    parent_cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (intake["parser_output_id"],)
    )
    parent_row = parent_cursor.fetchone()
    if parent_row is None:
        raise AiFallbackServiceError("PROPOSAL_NOT_FOUND", "The intake proposal is missing.")
    parent = _row_dict(parent_row, parent_cursor.description)
    if (
        parent["parse_status"] != PARSED_PENDING_CONFIRMATION
        or parent["parse_status"] in TERMINAL_STATUSES
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Only a pending deterministic proposal may enter fallback.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "explicit_deny",
            },
        )
    effective_payload, _completion_id, parent_version = resolve_effective_payload(conn, parent)
    parent_hash = compute_effective_proposal_content_hash(conn, parent)
    source_kind, segments = _source_segments(conn, intake, parent)
    source_text = "\n".join(text for _kind, text in segments)
    sensitive_reason = _scan_sensitive(source_text, assets["sensitive"])
    if sensitive_reason is not None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Sensitive source text is refused before model invocation.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "sensitive_text_refused",
            },
        )
    for _kind, segment in segments:
        if _scan_sensitive(segment, assets["sensitive"]) is not None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_NOT_ELIGIBLE",
                "Sensitive source text is refused before model invocation.",
                details={
                    "eligibility_disposition": "manual_recovery",
                    "refusal_reason": "sensitive_text_refused",
                },
            )
    intent_result, mode, intent_reasons = _intent_result(source_text, assets["intent"])
    if intent_result == "deny":
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Explicitly denied intent cannot enter model fallback.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "explicit_deny",
            },
        )
    forbidden_fields = _forbidden_parent_fields(effective_payload)
    if forbidden_fields:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Fallback cannot carry parent destination fields that S5e cannot persist.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "forbidden_field",
            },
        )
    complete, positive = _effective_fields(effective_payload)
    if complete and positive and intent_result == "positive":
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "The deterministic personal proposal is already complete.",
            details={
                "eligibility_disposition": "review_existing",
                "refusal_reason": "deterministic_complete",
            },
        )
    reasons = list(intent_reasons)
    if not complete:
        reasons.append(
            "receipt_ocr_fields_incomplete"
            if source_kind == "receipt_local_ocr_text"
            else "deterministic_fields_incomplete"
        )
    if not positive:
        mode = "classification_only"
    if source_kind == "telegram_raw_text":
        source_catalog = {f"t{index + 1:04d}": text for index, (_kind, text) in enumerate(segments)}
        if _has_conflicting_text_money_candidates(source_catalog):
            reasons.append("conflicting_text_candidates")
    projection, selected, projection_hash, selection_hash, source_bytes = _projection(
        source_kind=source_kind,
        segments=segments,
        reasons=sorted(set(reasons)),
    )
    projection_text = _json_bytes(projection).decode("utf-8")
    if _scan_sensitive(projection_text, assets["sensitive"]) is not None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "The complete fallback user projection is sensitive.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "sensitive_text_refused",
            },
        )
    request_material = {
        "messages": [{"role": "user", "content": projection_text}],
        "model": f"{runtime['provider']}/{runtime['model']}",
        "maxTokens": MODEL_MAX_TOKENS,
        "temperature": MODEL_TEMPERATURE,
        "systemPrompt": assets["prompt"],
        "purpose": runtime["audit_purpose"],
        "agentId": runtime["agent_id"],
    }
    request_blob = _json_bytes(request_material)
    request_hash = canonical_request_sha256(request_blob)
    if len(request_blob) > MAX_REQUEST_BYTES:
        raise AiFallbackServiceError(
            "AI_FALLBACK_NOT_ELIGIBLE",
            "Canonical model request exceeds the bounded limit.",
            details={
                "eligibility_disposition": "manual_recovery",
                "refusal_reason": "resource_refused",
            },
        )
    if eligibility_only:
        return {"eligibility_disposition": "model_eligible"}
    intent_evidence_hash = _hash_material(
        "finance-ai-intent-evidence-v1",
        {
            "result": intent_result,
            "positive": positive,
            "source_hash": _sha256_bytes(source_text.encode("utf-8")),
        },
    )
    material = {
        "schema_version": "finance-ai-preparation-material-v1",
        "intake_public_id": intake["public_id"],
        "parent_public_id": parent["public_id"],
        "parent_version": parent_version,
        "parent_effective_content_hash": parent_hash,
        "source_kind": source_kind,
        "source_projection_hash": projection_hash,
        "source_selection_manifest_hash": selection_hash,
        "source_field_state_hash": _field_state_hash(effective_payload),
        "eligibility_mode": mode,
        "eligibility_reasons": sorted(set(reasons)),
        "runtime_policy_version": runtime_policy_version,
        "runtime_policy_hash": runtime_policy_hash,
        "prompt_version": assets["registry"].asset("prompt").version,
        "prompt_template_hash": _asset_hash(assets, "prompt"),
        "intent_policy_version": assets["intent"]["version"],
        "intent_policy_hash": _asset_hash(assets, "intent_policy"),
        "intent_policy_result": intent_result,
        "intent_evidence_hash": intent_evidence_hash,
        "default_policy_version": assets["default"]["version"],
        "default_policy_hash": _asset_hash(assets, "default_policy"),
        "default_evidence_hash": None,
        "sensitive_text_policy_version": assets["sensitive"]["version"],
        "sensitive_text_policy_hash": _asset_hash(assets, "sensitive_text_policy"),
        "sensitive_text_scan_hash": _hash_material(
            "finance-ai-sensitive-scan-v1",
            {"result": "clear", "source_hash": _sha256_bytes(source_text.encode("utf-8"))},
        ),
        "deadline_policy_version": assets["deadline"]["version"],
        "deadline_policy_hash": _asset_hash(assets, "deadline_policy"),
        "sqlite_money_policy_version": assets["sqlite_money"]["version"],
        "sqlite_money_policy_hash": _asset_hash(assets, "sqlite_money_policy"),
        "expected_provider": runtime["provider"],
        "expected_model": runtime["model"],
        "expected_agent_id": runtime["agent_id"],
        "expected_audit_caller_kind": runtime["audit_caller_kind"],
        "expected_audit_caller_id": runtime["audit_caller_id"],
        "expected_audit_caller_name": runtime["audit_caller_name"],
        "expected_audit_purpose": runtime["audit_purpose"],
        "expected_audit_session_key_sha256": None,
        "request_sha256": request_hash,
        "request_byte_count": len(request_blob),
    }
    prep_hash = preparation_material_hash(material)
    attempt_public_id = derive_attempt_public_id(prep_hash)
    invoke_not_after = now + INVOCATION_WINDOW_MS
    result_not_after = invoke_not_after + RESULT_WINDOW_MS
    eligibility_json = json.dumps(sorted(set(reasons)), separators=(",", ":"), ensure_ascii=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        committed = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE raw_intake_record_id = ?",
            (intake["id"],),
        ).fetchone()
        if committed is not None:
            if committed["preparation_material_hash"] != prep_hash:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Raw intake is already bound to different fallback material.",
                )
            committed_attempt = dict(committed)
            _require_attempt_receipt_binding(
                conn,
                attempt_id=int(committed_attempt["id"]),
                receipt=compatibility_receipt,
            )
            committed_result = conn.execute(
                "SELECT * FROM ai_fallback_results WHERE attempt_id = ?",
                (committed_attempt["id"],),
            ).fetchone()
            if committed_result is not None:
                _verify_committed_result_replay(
                    conn,
                    attempt=committed_attempt,
                    result=dict(committed_result),
                )
            else:
                _assert_fallback_raw_intake_lineage(conn, attempt=committed_attempt)
                _verify_attempt_provenance_material(conn, attempt=committed_attempt)
            conn.commit()
            return _prepared_replay_response(conn, committed_attempt, now)
        locked_intake, locked_parent = _verify_current_parent_state(
            conn,
            intake_id=intake["id"],
            parent_id=parent["id"],
            expected_intake_public_id=str(intake["public_id"]),
            expected_parent_public_id=str(parent["public_id"]),
            expected_parent_version=parent_version,
            expected_parent_hash=parent_hash,
        )
        if locked_intake["source_type"] != intake["source_type"]:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Fallback source identity changed before persistence."
            )
        _verify_attempt_source_material(
            conn,
            intake=locked_intake,
            parent=locked_parent,
            attempt={
                "source_kind": source_kind,
                "source_projection_hash": projection_hash,
                "source_selection_manifest_hash": selection_hash,
                "source_projection_byte_count": source_bytes,
                "eligibility_reasons_json": eligibility_json,
            },
        )
        cursor = conn.execute(
            """
            INSERT INTO ai_fallback_attempts (
                attempt_public_id, preparation_material_hash, raw_intake_record_id,
                parent_parser_output_id, parent_proposal_version,
                parent_effective_content_hash, source_kind, source_projection_hash,
                source_projection_byte_count, source_selection_manifest_hash,
                source_field_state_hash, fallback_mode, eligibility_reasons_json,
                runtime_policy_version, runtime_policy_hash, prompt_version,
                prompt_template_hash, intent_policy_version, intent_policy_hash,
                intent_policy_result, intent_evidence_hash, default_policy_version,
                default_policy_hash, default_evidence_hash, sensitive_text_policy_version,
                sensitive_text_policy_hash, sensitive_text_scan_hash,
                deadline_policy_version, deadline_policy_hash, sqlite_money_policy_version,
                sqlite_money_policy_hash, expected_provider, expected_model,
                expected_agent_id, expected_audit_caller_kind, expected_audit_caller_id,
                expected_audit_caller_name, expected_audit_purpose,
                expected_audit_session_key_sha256, request_blob, request_sha256,
                request_byte_count, prepared_at_ms, invoke_not_after_ms, result_not_after_ms
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                attempt_public_id,
                prep_hash,
                intake["id"],
                parent["id"],
                parent_version,
                parent_hash,
                source_kind,
                projection_hash,
                source_bytes,
                selection_hash,
                material["source_field_state_hash"],
                mode,
                eligibility_json,
                material["runtime_policy_version"],
                material["runtime_policy_hash"],
                material["prompt_version"],
                material["prompt_template_hash"],
                material["intent_policy_version"],
                material["intent_policy_hash"],
                intent_result,
                intent_evidence_hash,
                material["default_policy_version"],
                material["default_policy_hash"],
                None,
                material["sensitive_text_policy_version"],
                material["sensitive_text_policy_hash"],
                material["sensitive_text_scan_hash"],
                material["deadline_policy_version"],
                material["deadline_policy_hash"],
                material["sqlite_money_policy_version"],
                material["sqlite_money_policy_hash"],
                material["expected_provider"],
                material["expected_model"],
                material["expected_agent_id"],
                material["expected_audit_caller_kind"],
                material["expected_audit_caller_id"],
                None,
                material["expected_audit_purpose"],
                None,
                request_blob,
                request_hash,
                len(request_blob),
                now,
                invoke_not_after,
                result_not_after,
            ),
        )
        if cursor.lastrowid is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_INTERNAL", "Attempt insert returned no identity."
            )
        if compatibility_receipt is not None:
            try:
                link_attempt_to_receipt(
                    conn,
                    attempt_id=int(cursor.lastrowid),
                    receipt_id=int(compatibility_receipt["id"]),
                )
            except ModelCompatibilityError as exc:
                raise AiFallbackServiceError(exc.code, str(exc)) from exc
        conn.commit()
    except AiFallbackServiceError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        committed = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE raw_intake_record_id = ?",
            (intake["id"],),
        ).fetchone()
        if committed is not None and committed["preparation_material_hash"] == prep_hash:
            _require_attempt_receipt_binding(
                conn,
                attempt_id=int(committed["id"]),
                receipt=compatibility_receipt,
            )
            return _prepared_replay_response(conn, dict(committed), now)
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback attempt identity collided."
        ) from exc
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise AiFallbackServiceError(
            "AI_FALLBACK_INTERNAL", "Fallback attempt persistence failed."
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "attempt_public_id": attempt_public_id,
        "claim_disposition": "claim_once",
        "prepared_at_ms": now,
        "invoke_not_after_ms": invoke_not_after,
        "result_not_after_ms": result_not_after,
        "request_sha256": request_hash,
        "replay_view": None,
    }


def prepare_ai_fallback(
    conn: sqlite3.Connection,
    *,
    intake_public_id: str,
    now_ms: int | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Preserved v1 preparation boundary."""
    return _prepare_ai_fallback_impl(
        conn,
        intake_public_id=intake_public_id,
        now_ms=now_ms,
        repo_root=repo_root,
    )


def prepare_ai_fallback_v2(
    conn: sqlite3.Connection,
    *,
    intake_public_id: str,
    config_projection: Mapping[str, Any],
    now_ms: int | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Prepare a receipt-pinned v2 attempt and link in one transaction."""
    require_staging_database(conn)
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise AiFallbackServiceError(
            "AI_MODEL_COMPATIBILITY_FK_REFUSED", "Foreign keys must be enabled."
        ) from exc
    try:
        existing_denial = get_ai_model_admission_decision_v2(
            conn, intake_public_id=intake_public_id
        )
    except AiModelAdmissionError as exc:
        raise AiFallbackServiceError(exc.code, str(exc)) from exc
    if existing_denial is not None:
        raise AiFallbackServiceError(
            "AI_MODEL_CONFIG_NOT_ACCEPTED",
            "The raw intake already has a terminal model-admission denial.",
        )
    _prepare_ai_fallback_impl(
        conn,
        intake_public_id=intake_public_id,
        now_ms=now_ms,
        repo_root=repo_root,
        eligibility_only=True,
    )
    try:
        receipt = resolve_compatibility_receipt(
            conn, config_projection=config_projection, repo_root=repo_root
        )
    except ModelCompatibilityError as exc:
        if exc.code in {
            "AI_MODEL_CONFIG_REFUSED",
            "AI_MODEL_CONFIG_NOT_ACCEPTED",
        }:

            def guard_denial(locked_conn: sqlite3.Connection) -> None:
                _prepare_ai_fallback_impl(
                    locked_conn,
                    intake_public_id=intake_public_id,
                    now_ms=now_ms,
                    repo_root=repo_root,
                    eligibility_only=True,
                )
                try:
                    locked_receipt = resolve_compatibility_receipt(
                        locked_conn,
                        config_projection=config_projection,
                        repo_root=repo_root,
                    )
                except ModelCompatibilityError as locked_exc:
                    if locked_exc.code in {
                        "AI_MODEL_CONFIG_REFUSED",
                        "AI_MODEL_CONFIG_NOT_ACCEPTED",
                    }:
                        return
                    raise AiModelAdmissionError(locked_exc.code, str(locked_exc)) from locked_exc
                raise _CompatibilityReceiptAvailable(locked_receipt)

            try:
                _record_ai_model_admission_denial_v2(
                    conn,
                    intake_public_id=intake_public_id,
                    config_evidence=config_projection,
                    now_ms=now_ms,
                    eligibility_guard=guard_denial,
                )
            except _CompatibilityReceiptAvailable as available:
                receipt = available.receipt
            except AiModelAdmissionError as admission_exc:
                raise AiFallbackServiceError(
                    admission_exc.code, str(admission_exc)
                ) from admission_exc
            else:
                raise AiFallbackServiceError(exc.code, str(exc)) from exc
        else:
            raise AiFallbackServiceError(exc.code, str(exc)) from exc
    result = _prepare_ai_fallback_impl(
        conn,
        intake_public_id=intake_public_id,
        now_ms=now_ms,
        repo_root=repo_root,
        compatibility_receipt=receipt,
    )
    return {
        **result,
        "receipt_public_id": receipt["receipt_public_id"],
        "config_projection_hash": receipt["config_projection_hash"],
        "request_identity": {
            "request_sha256": result["request_sha256"],
            "model": f"{receipt['canonical_provider']}/{receipt['canonical_model']}",
            "agent_id": receipt["agent_id"],
            "purpose": AUDIT_PURPOSE,
        },
    }


def _claim_ai_fallback_invocation(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    compatibility_receipt: Mapping[str, Any] | None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    require_staging_database(conn)
    now = _sample_now_ms(now_ms)
    cursor = conn.execute(
        "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?", (attempt_public_id,)
    )
    row = cursor.fetchone()
    if row is None:
        raise AiFallbackServiceError("AI_FALLBACK_NOT_FOUND", "Fallback attempt was not found.")
    attempt = _row_dict(row, cursor.description)
    _require_attempt_receipt_binding(
        conn,
        attempt_id=int(attempt["id"]),
        receipt=compatibility_receipt,
    )
    existing = conn.execute(
        "SELECT * FROM ai_fallback_invocation_claims WHERE attempt_id = ?", (attempt["id"],)
    ).fetchone()
    if existing is not None:
        child_link = conn.execute(
            """
            SELECT link.parser_output_id
            FROM ai_fallback_proposal_links AS link
            JOIN ai_fallback_results AS result ON result.id = link.result_id
            WHERE result.attempt_id = ?
            """,
            (attempt["id"],),
        ).fetchone()
        _assert_fallback_raw_intake_lineage(
            conn,
            attempt=attempt,
            child_parser_output_id=(
                None if child_link is None else int(child_link["parser_output_id"])
            ),
        )
        return {"invocation_disposition": "do_not_invoke"}
    if now >= int(attempt["invoke_not_after_ms"]):
        raise AiFallbackServiceError("DEADLINE_EXCEEDED", "Fallback invocation window expired.")
    request_blob = bytes(attempt["request_blob"])
    if canonical_request_sha256(request_blob) != attempt["request_sha256"]:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Prepared request hash does not verify."
        )
    claim_public_id = derive_claim_public_id(attempt_public_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM ai_fallback_invocation_claims WHERE attempt_id = ?", (attempt["id"],)
        ).fetchone()
        if existing is not None:
            child_link = conn.execute(
                """
                SELECT link.parser_output_id
                FROM ai_fallback_proposal_links AS link
                JOIN ai_fallback_results AS result ON result.id = link.result_id
                WHERE result.attempt_id = ?
                """,
                (attempt["id"],),
            ).fetchone()
            _assert_fallback_raw_intake_lineage(
                conn,
                attempt=attempt,
                child_parser_output_id=(
                    None if child_link is None else int(child_link["parser_output_id"])
                ),
            )
            conn.commit()
            return {"invocation_disposition": "do_not_invoke"}
        _verify_attempt_current_parent_state(conn, attempt)
        _verify_attempt_provenance_material(conn, attempt=attempt)
        claim_time = _sample_now_ms(now_ms)
        if claim_time >= int(attempt["invoke_not_after_ms"]):
            raise AiFallbackServiceError("DEADLINE_EXCEEDED", "Fallback invocation window expired.")
        call_start_not_after = claim_time + CALL_START_WINDOW_MS
        claim_material = {
            "schema_version": "finance-ai-claim-material-v1",
            "attempt_public_id": attempt_public_id,
            "invocation_claimed_at_ms": claim_time,
            "call_start_not_after_ms": call_start_not_after,
            "request_sha256": attempt["request_sha256"],
            "invocation_disposition": "invoke_once",
        }
        claim_hash = claim_material_hash(claim_material)
        conn.execute(
            """
            INSERT INTO ai_fallback_invocation_claims (
                claim_public_id, claim_material_hash, attempt_id,
                invocation_claimed_at_ms, call_start_not_after_ms,
                invocation_disposition
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                claim_public_id,
                claim_hash,
                attempt["id"],
                claim_time,
                call_start_not_after,
                "invoke_once",
            ),
        )
        conn.commit()
    except AiFallbackServiceError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        existing = conn.execute(
            "SELECT * FROM ai_fallback_invocation_claims WHERE attempt_id = ?", (attempt["id"],)
        ).fetchone()
        if existing is not None:
            return {"invocation_disposition": "do_not_invoke"}
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "Invocation claim collided.") from exc
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise AiFallbackServiceError(
            "AI_FALLBACK_INTERNAL", "Fallback invocation claim persistence failed."
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "invocation_disposition": "invoke_once",
        "call_start_not_after_ms": call_start_not_after,
        "request_sha256": attempt["request_sha256"],
        "model_call": json.loads(request_blob.decode("utf-8")),
    }


def claim_ai_fallback_invocation(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Claim only a preserved v1 attempt."""
    return _claim_ai_fallback_invocation(
        conn,
        attempt_public_id=attempt_public_id,
        compatibility_receipt=None,
        now_ms=now_ms,
    )


def claim_ai_fallback_invocation_v2(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Claim only an attempt that carries an immutable v2 receipt link."""
    require_staging_database(conn)
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise AiFallbackServiceError(
            "AI_MODEL_COMPATIBILITY_FK_REFUSED", "Foreign keys must be enabled."
        ) from exc
    row = conn.execute(
        "SELECT id FROM ai_fallback_attempts WHERE attempt_public_id = ?",
        (attempt_public_id,),
    ).fetchone()
    if row is None:
        raise AiFallbackServiceError("AI_FALLBACK_NOT_FOUND", "Fallback attempt was not found.")
    receipt = _verified_compatibility_receipt_for_attempt(conn, attempt_id=int(row["id"]))
    if receipt is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "The fallback attempt has no v2 compatibility receipt."
        )
    result = _claim_ai_fallback_invocation(
        conn,
        attempt_public_id=attempt_public_id,
        compatibility_receipt=receipt,
        now_ms=now_ms,
    )
    return {
        **result,
        "receipt_public_id": receipt["receipt_public_id"],
        "display_alias": receipt["display_alias"],
        "execution_class": receipt["execution_class"],
    }


def _normal_meta_hashes(
    arguments: Mapping[str, Any], attempt: Mapping[str, Any]
) -> tuple[str | None, str | None, bool]:
    names = (
        "returned_provider",
        "returned_model",
        "returned_agent_id",
        "audit_caller_kind",
        "audit_caller_id",
        "audit_caller_name",
        "audit_purpose",
        "audit_session_key_sha256",
    )
    expected = {
        "returned_provider": attempt["expected_provider"],
        "returned_model": attempt["expected_model"],
        "returned_agent_id": attempt["expected_agent_id"],
        "audit_caller_kind": attempt["expected_audit_caller_kind"],
        "audit_caller_id": attempt["expected_audit_caller_id"],
        "audit_caller_name": None,
        "audit_purpose": attempt["expected_audit_purpose"],
        "audit_session_key_sha256": None,
    }
    actual = {name: arguments.get(name) for name in names}
    normal_hash = _hash_material("finance-ai-returned-attribution-material-v1", actual)
    usage = {
        "usage_input_tokens": arguments.get("usage_input_tokens"),
        "usage_output_tokens": arguments.get("usage_output_tokens"),
    }
    usage_hash = _hash_material("finance-ai-usage-material-v1", usage)
    return normal_hash, usage_hash, all(name in arguments for name in names) and actual == expected


def _require_result_argument_shape(
    transport_outcome: str,
    arguments: Mapping[str, Any],
) -> None:
    if transport_outcome in {
        "response_received",
        "response_oversize",
        "response_unencodable",
        "response_resource_refused",
    }:
        body_fields = {
            "response_received": {"response_utf8_b64", "response_byte_count", "response_sha256"},
            "response_oversize": {
                "response_code_unit_count",
                "response_byte_count",
                "response_sha256",
            },
            "response_unencodable": {"response_code_unit_count", "response_utf16_sha256"},
            "response_resource_refused": {"response_code_unit_count"},
        }[transport_outcome]
        expected = _NORMAL_METADATA_FIELDS | body_fields
    elif transport_outcome == "response_metadata_refused":
        body_state = arguments.get("response_body_state")
        if body_state not in _RESPONSE_BODY_FIELDS:
            raise AiFallbackServiceError(
                "AI_FALLBACK_ARGUMENTS_REFUSED",
                "response_metadata_refused has an invalid response_body_state.",
            )
        expected = _METADATA_REFUSAL_FIELDS | _RESPONSE_BODY_FIELDS[str(body_state)]
    elif transport_outcome in {
        "provider_error",
        "local_preinvocation_refused",
        "timeout",
        "cancelled",
    }:
        expected = {"failure_code"}
    else:
        raise AiFallbackServiceError(
            "AI_FALLBACK_ARGUMENTS_REFUSED",
            "transport_outcome is not allowlisted.",
        )
    if set(arguments) != expected:
        raise AiFallbackServiceError(
            "AI_FALLBACK_ARGUMENTS_REFUSED",
            "Result arguments do not match the exact transport union.",
        )


def _validate_normal_metadata(arguments: Mapping[str, Any]) -> None:
    for field in (
        "returned_provider",
        "returned_model",
        "returned_agent_id",
        "audit_caller_kind",
        "audit_caller_id",
        "audit_caller_name",
        "audit_purpose",
    ):
        value = arguments[field]
        if value is not None and (
            not isinstance(value, str) or len(value.encode("utf-8", errors="strict")) > 256
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_ARGUMENTS_REFUSED",
                f"{field} metadata is invalid.",
            )
    session_hash = arguments["audit_session_key_sha256"]
    if session_hash is not None and (
        not isinstance(session_hash, str) or _HASH_RE.fullmatch(session_hash) is None
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_ARGUMENTS_REFUSED",
            "audit_session_key_sha256 metadata is invalid.",
        )
    for field in ("usage_input_tokens", "usage_output_tokens"):
        value = arguments[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_ARGUMENTS_REFUSED",
                f"{field} metadata is invalid.",
            )


def _require_hash_argument(arguments: Mapping[str, Any], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_ARGUMENTS_REFUSED",
            f"{field} is invalid.",
        )
    return value


def _result_response(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    link = conn.execute(
        """
        SELECT po.public_id AS proposal_public_id,
               po.id AS proposal_id,
               link.proposal_version AS proposal_version,
               link.effective_content_hash AS effective_content_hash
        FROM ai_fallback_proposal_links AS link
        JOIN parser_outputs AS po ON po.id = link.parser_output_id
        WHERE link.result_id = ?
        """,
        (row["id"],),
    ).fetchone()
    return {
        "attempt_public_id": conn.execute(
            "SELECT attempt_public_id FROM ai_fallback_attempts WHERE id = ?", (row["attempt_id"],)
        ).fetchone()[0],
        "claim_public_id": conn.execute(
            "SELECT claim_public_id FROM ai_fallback_invocation_claims WHERE id = ?",
            (row["claim_id"],),
        ).fetchone()[0],
        "result_public_id": row["result_public_id"],
        "result_material_hash": row["result_material_hash"],
        "result_status": row["result_status"],
        "retention_state": row["retention_state"],
        "response_sha256": row["response_sha256"],
        "response_byte_count": row["response_byte_count"],
        "result_received_at_ms": row["result_received_at_ms"],
        "post_lock_at_ms": row["post_lock_at_ms"],
        "decision_at_ms": row["decision_at_ms"],
        "deadline_disposition": row["deadline_disposition"],
        "non_child_reason": row["non_child_reason"],
        "recovery_disposition": row["recovery_disposition"],
        "proposal_public_id": None if link is None else link["proposal_public_id"],
        "proposal_version": None if link is None else link["proposal_version"],
        "effective_content_hash": None if link is None else link["effective_content_hash"],
    }


def _sealed_result_material(
    *,
    attempt: Mapping[str, Any],
    claim: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    result_arguments_hash = result.get("result_arguments_hash")
    if result_arguments_hash is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "Legacy result lacks the cryptographic transport-arguments seal.",
        )
    return {
        "schema_version": "finance-ai-result-material-v2",
        "attempt_public_id": attempt["attempt_public_id"],
        "claim_public_id": claim["claim_public_id"],
        "claim_material_hash": claim["claim_material_hash"],
        "result_arguments_hash": result_arguments_hash,
        "transport_outcome": result["transport_outcome"],
        "result_status": result["result_status"],
        "retention_state": result["retention_state"],
        "normal_attribution_hash": result["normal_attribution_hash"],
        "metadata_refusal_hash": result["metadata_refusal_hash"],
        "usage_hash": result["usage_hash"],
        "result_received_at_ms": result["result_received_at_ms"],
        "post_lock_at_ms": result["post_lock_at_ms"],
        "decision_at_ms": result["decision_at_ms"],
        "deadline_policy_version": result["deadline_policy_version"],
        "deadline_policy_hash": result["deadline_policy_hash"],
        "deadline_disposition": result["deadline_disposition"],
        "response_body_state": result["response_body_state"],
        "response_sha256": result["response_sha256"],
        "response_byte_count": result["response_byte_count"],
        "response_code_unit_count": result["response_code_unit_count"],
        "response_utf16_sha256": result["response_utf16_sha256"],
        "failure_code": result["failure_code"],
        "non_child_reason": result["non_child_reason"],
        "recovery_disposition": result["recovery_disposition"],
        "normalized_payload_hash": result["normalized_payload_hash"],
        "source_field_state_hash": result["source_field_state_hash"],
        "ambiguity_hash": result["ambiguity_hash"],
        "evidence_set_hash": result["evidence_set_hash"],
    }


def _verify_committed_result_replay(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Revalidate durable result lineage before returning any replay."""
    claim = conn.execute(
        "SELECT * FROM ai_fallback_invocation_claims WHERE id = ?", (result["claim_id"],)
    ).fetchone()
    if claim is None or int(claim["attempt_id"]) != int(attempt["id"]):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Committed fallback result claim lineage is invalid."
        )
    _assert_result_raw_intake_binding(
        conn,
        attempt=attempt,
        result=result,
    )
    _verify_attempt_provenance_material(conn, attempt=attempt)
    _verify_claim_material(attempt, claim)
    result_material = _sealed_result_material(attempt=attempt, claim=claim, result=result)
    _require_matching_hash(
        result["result_material_hash"],
        result_material_v2_hash(result_material),
        "result hash",
    )
    _require_matching_hash(
        result["result_public_id"],
        derive_result_public_id(attempt["attempt_public_id"]),
        "result identity",
    )
    response_blob = result["response_blob"]
    if response_blob is None:
        if result["response_body_state"] == "retained":
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Retained fallback response body is missing."
            )
    else:
        if result["response_body_state"] != "retained":
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Unretained fallback response has a body."
            )
        response_bytes = bytes(response_blob)
        _require_matching_hash(
            result["response_sha256"],
            canonical_response_sha256(response_bytes),
            "response hash",
        )
        if int(result["response_byte_count"]) != len(response_bytes):
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "AI fallback response byte count does not verify."
            )
    if result["result_status"] == "proposal_created":
        link = conn.execute(
            "SELECT * FROM ai_fallback_proposal_links WHERE result_id = ?", (result["id"],)
        ).fetchone()
        if link is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Committed AI child link is missing."
            )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?", (link["parser_output_id"],)
        ).fetchone()
        if child is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Committed AI child proposal is missing."
            )
        _verify_ai_provenance_material(
            conn,
            attempt=dict(attempt),
            claim=dict(claim),
            result=dict(result),
            link=dict(link),
            child=dict(child),
        )


def _record_ai_fallback_result_with_disposition(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    transport_outcome: str,
    arguments: Mapping[str, Any],
    compatibility_receipt: Mapping[str, Any] | None,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist one result and, only when fully validated, an AI child."""
    require_staging_database(conn)
    now = _sample_now_ms(now_ms)
    _require_result_argument_shape(transport_outcome, arguments)
    if transport_outcome in {
        "response_received",
        "response_oversize",
        "response_unencodable",
        "response_resource_refused",
    }:
        _validate_normal_metadata(arguments)
    attempt_cursor = conn.execute(
        "SELECT * FROM ai_fallback_attempts WHERE attempt_public_id = ?", (attempt_public_id,)
    )
    attempt_row = attempt_cursor.fetchone()
    if attempt_row is None:
        raise AiFallbackServiceError("AI_FALLBACK_NOT_FOUND", "Fallback attempt was not found.")
    attempt = _row_dict(attempt_row, attempt_cursor.description)
    _require_attempt_receipt_binding(
        conn,
        attempt_id=int(attempt["id"]),
        receipt=compatibility_receipt,
    )
    result_arguments_hash = _hash_material(
        "finance-ai-result-arguments-v1",
        {"transport_outcome": transport_outcome, "arguments": dict(arguments)},
    )
    existing = conn.execute(
        "SELECT * FROM ai_fallback_results WHERE attempt_id = ?", (attempt["id"],)
    ).fetchone()
    if existing is not None:
        _assert_result_raw_intake_binding(
            conn,
            attempt=attempt,
            result=existing,
        )
        if existing["result_arguments_hash"] is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "Legacy result replay cannot be verified against transport material.",
            )
        if existing["result_arguments_hash"] != result_arguments_hash:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "Result replay carries different transport material.",
            )
        _verify_committed_result_replay(
            conn,
            attempt=attempt,
            result=dict(existing),
        )
        return _result_response(conn, dict(existing)), True
    parent_cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (attempt["parent_parser_output_id"],)
    )
    parent_row = parent_cursor.fetchone()
    if parent_row is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback parent proposal was not found."
        )
    parent = _row_dict(parent_row, parent_cursor.description)
    parent_payload, _parent_completion_id, _parent_version = resolve_effective_payload(conn, parent)
    intake_cursor = conn.execute(
        "SELECT * FROM raw_intake_records WHERE id = ?", (attempt["raw_intake_record_id"],)
    )
    intake_row = intake_cursor.fetchone()
    if intake_row is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Fallback raw-intake source is missing."
        )
    _verify_attempt_source_material(
        conn,
        intake=_row_dict(intake_row, intake_cursor.description),
        parent=parent,
        attempt=attempt,
    )
    projection_catalog = _projection_catalog(attempt)
    claim_cursor = conn.execute(
        "SELECT * FROM ai_fallback_invocation_claims WHERE attempt_id = ?", (attempt["id"],)
    )
    claim_row = claim_cursor.fetchone()
    if claim_row is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "A result requires one durable invocation claim."
        )
    claim = _row_dict(claim_row, claim_cursor.description)
    _verify_claim_material(attempt, claim)
    result_received = now
    post_lock = now
    late = result_received >= int(attempt["result_not_after_ms"])
    response_blob: bytes | None = None
    response_sha: str | None = None
    response_byte_count: int | None = None
    response_code_units: int | None = None
    response_utf16_sha: str | None = None
    normal_hash: str | None = None
    usage_hash: str | None = None
    result_status: str
    non_child: str | None = None
    recovery: str | None = None
    normalized_hash: str | None = None
    ambiguity_hash: str | None = None
    evidence_hash: str | None = None
    planned_evidence: list[dict[str, Any]] | None = None
    metadata_hash: str | None = None
    body_state = "none"
    retention = "none"
    response_payload: dict[str, Any] | None = None
    if transport_outcome in {"response_received", "response_oversize"}:
        normal_hash, usage_hash, attribution_ok = _normal_meta_hashes(arguments, attempt)
        encoded = arguments.get("response_utf8_b64")
        if transport_outcome == "response_received":
            if not isinstance(encoded, str):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "response_utf8_b64 is required."
                )
            try:
                response_blob = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "response_utf8_b64 is invalid."
                ) from exc
            supplied_count = arguments.get("response_byte_count")
            supplied_hash = arguments.get("response_sha256")
            if (
                base64.b64encode(response_blob).decode("ascii") != encoded
                or len(response_blob) > MAX_RESPONSE_BYTES
                or supplied_count != len(response_blob)
                or not isinstance(supplied_hash, str)
                or _HASH_RE.fullmatch(supplied_hash) is None
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "response body encoding is invalid."
                )
            response_sha = canonical_response_sha256(response_blob)
            if supplied_hash != response_sha:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "response body hash is invalid."
                )
            response_byte_count = len(response_blob)
            body_state, retention = "retained", "blob_retained"
            try:
                response_payload = _validate_ai_response(
                    response_blob,
                    set(projection_catalog),
                    parent_payload=parent_payload,
                    catalog=projection_catalog,
                    source_kind=str(attempt["source_kind"]),
                    ocr_layout=load_ocr_layout(
                        conn, parent_payload=parent_payload, source_kind=str(attempt["source_kind"])
                    ),
                )
            except Exception:
                response_payload = None
            if response_byte_count > MAX_CHILD_RESPONSE_BYTES:
                result_status, recovery = "response_oversize", "use_manual_intake"
            elif not attribution_ok:
                result_status, recovery = "attribution_refused", "operator_runtime_review"
            elif response_payload is None:
                result_status, non_child = "response_refused", "validation_refused"
            elif attempt["fallback_mode"] == "classification_only":
                result_status = "classification_only"
                non_child = "intent_unproven"
            elif response_payload["intent_type"] != "personal_expense":
                result_status, non_child = "response_refused", "intent_unproven"
            else:
                result_status = "proposal_created"
        else:
            response_byte_count = arguments.get("response_byte_count")
            response_sha = arguments.get("response_sha256")
            response_code_units = arguments.get("response_code_unit_count")
            body_state, retention = "oversize", "unretained_oversize"
            result_status, recovery = "response_oversize", "use_manual_intake"
            attribution_ok = False
            if (
                not isinstance(response_byte_count, int)
                or isinstance(response_byte_count, bool)
                or response_byte_count < MAX_RESPONSE_BYTES + 1
                or not isinstance(response_code_units, int)
                or isinstance(response_code_units, bool)
                or not 0 <= response_code_units <= 131_072
                or not isinstance(response_sha, str)
                or _HASH_RE.fullmatch(response_sha) is None
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "Oversize response hash is invalid."
                )
        if late:
            result_status, non_child, recovery = (
                "late_result",
                None,
                "resend_new_intake_after_late_result",
            )
    elif transport_outcome in {
        "response_unencodable",
        "response_resource_refused",
        "response_metadata_refused",
    }:
        normal_hash, usage_hash, attribution_ok = (
            (None, None, False)
            if transport_outcome == "response_metadata_refused"
            else _normal_meta_hashes(arguments, attempt)
        )
        if transport_outcome == "response_unencodable":
            response_code_units = arguments.get("response_code_unit_count")
            response_utf16_sha = _require_hash_argument(arguments, "response_utf16_sha256")
            body_state, retention = "unencodable", "unretained_unencodable"
            result_status, recovery = "response_unencodable", "use_manual_intake"
            if (
                not isinstance(response_code_units, int)
                or isinstance(response_code_units, bool)
                or response_code_units < 0
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "UTF-16 response count is invalid."
                )
        elif transport_outcome == "response_resource_refused":
            response_code_units = arguments.get("response_code_unit_count")
            body_state, retention = "resource_refused", "unretained_resource_refused"
            result_status, recovery = "response_resource_refused", "use_manual_intake"
            if (
                not isinstance(response_code_units, int)
                or isinstance(response_code_units, bool)
                or response_code_units < 131_073
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "Resource-refused response count is invalid."
                )
        else:
            body_state = str(arguments["response_body_state"])
            retention = {
                "retained": "blob_retained",
                "oversize": "unretained_oversize",
                "unencodable": "unretained_unencodable",
                "resource_refused": "unretained_resource_refused",
                "none": "none",
            }[body_state]
            result_status, recovery = "attribution_refused", "operator_runtime_review"
            metadata_hash = _hash_material(
                "finance-ai-metadata-refusal-material-v1",
                {
                    "metadata_field": arguments["metadata_field"],
                    "metadata_reason": arguments["metadata_reason"],
                    "metadata_code_unit_count": arguments["metadata_code_unit_count"],
                    "metadata_sha256": arguments["metadata_sha256"],
                },
            )
            if (
                arguments["metadata_field"] not in _METADATA_FIELDS
                or arguments["metadata_reason"] not in _METADATA_REASONS
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata refusal material is invalid."
                )
            refusal_count = arguments["metadata_code_unit_count"]
            refusal_hash = arguments["metadata_sha256"]
            if refusal_count is not None and (
                not isinstance(refusal_count, int)
                or isinstance(refusal_count, bool)
                or refusal_count < 0
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata refusal count is invalid."
                )
            if refusal_hash is not None and (
                not isinstance(refusal_hash, str) or _HASH_RE.fullmatch(refusal_hash) is None
            ):
                raise AiFallbackServiceError(
                    "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata refusal hash is invalid."
                )
            if body_state == "retained":
                encoded = arguments["response_utf8_b64"]
                try:
                    response_blob = base64.b64decode(str(encoded).encode("ascii"), validate=True)
                except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata-refused body is invalid."
                    ) from exc
                response_sha = canonical_response_sha256(response_blob)
                if (
                    base64.b64encode(response_blob).decode("ascii") != encoded
                    or arguments["response_byte_count"] != len(response_blob)
                    or arguments["response_sha256"] != response_sha
                    or len(response_blob) > MAX_RESPONSE_BYTES
                ):
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata-refused body is invalid."
                    )
                response_byte_count = len(response_blob)
            elif body_state == "oversize":
                response_code_units = arguments["response_code_unit_count"]
                response_byte_count = arguments["response_byte_count"]
                response_sha = arguments["response_sha256"]
                if (
                    not isinstance(response_code_units, int)
                    or isinstance(response_code_units, bool)
                    or not 0 <= response_code_units <= 131_072
                    or not isinstance(response_byte_count, int)
                    or isinstance(response_byte_count, bool)
                    or response_byte_count < 65_537
                    or not isinstance(response_sha, str)
                    or _HASH_RE.fullmatch(response_sha) is None
                ):
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata-refused body is invalid."
                    )
            elif body_state == "unencodable":
                response_code_units = arguments["response_code_unit_count"]
                response_utf16_sha = _require_hash_argument(arguments, "response_utf16_sha256")
                if (
                    not isinstance(response_code_units, int)
                    or isinstance(response_code_units, bool)
                    or response_code_units < 0
                ):
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata-refused body is invalid."
                    )
            elif body_state == "resource_refused":
                response_code_units = arguments["response_code_unit_count"]
                if (
                    not isinstance(response_code_units, int)
                    or isinstance(response_code_units, bool)
                    or response_code_units < 131_073
                ):
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_ARGUMENTS_REFUSED", "Metadata-refused body is invalid."
                    )
        if late:
            result_status, non_child, recovery = (
                "late_result",
                None,
                "resend_new_intake_after_late_result",
            )
    elif transport_outcome in {
        "provider_error",
        "local_preinvocation_refused",
        "timeout",
        "cancelled",
    }:
        body_state, retention = "none", "none"
        mapping = {
            "provider_error": (
                "provider_error",
                "resend_new_intake_after_provider_failure",
                "host_llm_failed",
            ),
            "local_preinvocation_refused": (
                "preinvocation_refused",
                "operator_runtime_review",
                arguments.get("failure_code"),
            ),
            "timeout": ("timeout", "resend_new_intake_after_timeout", "deadline_exceeded"),
            "cancelled": ("cancelled", "resend_new_intake_after_cancellation", "cancelled"),
        }
        result_status, recovery, failure = mapping[transport_outcome]
        if failure not in {
            "host_llm_failed",
            "request_integrity_refused",
            "runtime_policy_refused",
            "call_start_deadline_exceeded",
            "deadline_exceeded",
            "cancelled",
        }:
            raise AiFallbackServiceError(
                "AI_FALLBACK_ARGUMENTS_REFUSED", "Failure code is invalid."
            )
        if (
            transport_outcome == "local_preinvocation_refused"
            and failure == "call_start_deadline_exceeded"
        ):
            recovery = "resend_new_intake_after_not_invoked"
        if late and transport_outcome != "local_preinvocation_refused":
            result_status, recovery = "late_result", "resend_new_intake_after_late_result"
        arguments = dict(arguments)
        arguments["_failure_code"] = failure
    else:
        raise AiFallbackServiceError(
            "AI_FALLBACK_ARGUMENTS_REFUSED", "transport_outcome is not allowlisted."
        )
    if result_status == "proposal_created" and response_payload is not None:
        normalized = {
            "intent": "personal_expense",
            "transaction_type": "personal_expense",
            "amount": response_payload["amount"]
            if response_payload["amount"] is not None
            else _payload_field(parent_payload, "amount"),
            "currency": response_payload["currency"]
            if response_payload["currency"] is not None
            else _payload_field(parent_payload, "currency"),
            "transaction_date": response_payload["transaction_date"]
            if response_payload["transaction_date"] is not None
            else _payload_field(parent_payload, "transaction_date"),
            "merchant": response_payload["merchant"]
            if response_payload["merchant"] is not None
            else _payload_field(parent_payload, "merchant"),
            "description": None,
            "account": None,
            "category": None,
            "ambiguity_flags": [],
        }
        normalized["ambiguity_flags"] = _normalized_ambiguity_flags(
            attempt,
            parent_payload,
            normalized,
            response_payload["ambiguity_flags"],
        )
        if normalized["amount"] is None or normalized["currency"] is None:
            result_status, non_child = "response_refused", "source_unresolved"
        elif normalized["merchant"] is None or not str(normalized["merchant"]).strip():
            result_status, non_child = "response_refused", "missing_merchant"
        else:
            normalized_currency = normalize_currency(str(normalized["currency"]))
            normalized_amount = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
                validate_amount_for_currency(
                    money_decimal(str(normalized["amount"]), label="AI effective amount"),
                    normalized_currency,
                    label="AI effective amount",
                ),
                label="AI effective amount",
            )
            if canonical_money_str(normalized_amount, normalized_currency) != normalized["amount"]:
                result_status, non_child = "response_refused", "validation_refused"
            elif not _sqlite_numeric_roundtrip_matches(
                conn,
                str(normalized["amount"]),
            ):
                result_status, non_child = "response_refused", "validation_refused"
            elif (
                _selected_money_pair(
                    str(normalized["amount"]),
                    normalized_currency,
                    projection_catalog,
                    source_kind=str(attempt["source_kind"]),
                    parent_payload=parent_payload,
                )
                is None
            ):
                result_status, non_child = "response_refused", "validation_refused"
        normalized_bytes = _json_bytes(normalized)
        normalized_hash = _sha256_bytes(normalized_bytes)
        ambiguity_hash = _hash_material(
            "finance-ai-ambiguity-v1", {"flags": normalized["ambiguity_flags"]}
        )
        planned_evidence = _planned_child_evidence(
            response_payload,
            normalized,
            attempt_public_id=attempt_public_id,
            parent_public_id=parent["public_id"],
            parent_effective_content_hash=attempt["parent_effective_content_hash"],
            catalog=projection_catalog,
            source_kind=str(attempt["source_kind"]),
            parent_payload=parent_payload,
        )
        evidence_hash = _hash_material("finance-ai-evidence-set-v1", {"rows": planned_evidence})
    if result_status == "proposal_created" and response_payload is not None:
        non_child = recovery = None
    result_material = {
        "schema_version": "finance-ai-result-material-v2",
        "attempt_public_id": attempt_public_id,
        "claim_public_id": claim["claim_public_id"],
        "claim_material_hash": claim["claim_material_hash"],
        "result_arguments_hash": result_arguments_hash,
        "transport_outcome": transport_outcome,
        "result_status": result_status,
        "retention_state": retention,
        "normal_attribution_hash": normal_hash
        if transport_outcome != "response_metadata_refused"
        else None,
        "metadata_refusal_hash": metadata_hash,
        "usage_hash": usage_hash if transport_outcome != "response_metadata_refused" else None,
        "result_received_at_ms": result_received,
        "post_lock_at_ms": post_lock,
        "decision_at_ms": now,
        "deadline_policy_version": attempt["deadline_policy_version"],
        "deadline_policy_hash": attempt["deadline_policy_hash"],
        "deadline_disposition": "late" if late else "within_window",
        "response_body_state": body_state,
        "response_sha256": response_sha,
        "response_byte_count": response_byte_count,
        "response_code_unit_count": response_code_units,
        "response_utf16_sha256": response_utf16_sha,
        "failure_code": arguments.get("_failure_code"),
        "non_child_reason": non_child,
        "recovery_disposition": recovery,
        "normalized_payload_hash": normalized_hash,
        "source_field_state_hash": attempt["source_field_state_hash"],
        "ambiguity_hash": ambiguity_hash,
        "evidence_set_hash": evidence_hash,
    }
    try:
        result_hash = result_material_v2_hash(result_material)
    except AiFallbackProvenanceValidationError as exc:
        raise AiFallbackServiceError("AI_FALLBACK_VALIDATION_REFUSED", str(exc)) from exc
    result_public_id = derive_result_public_id(attempt_public_id)
    child_public_id: str | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked_attempt_row = conn.execute(
            "SELECT * FROM ai_fallback_attempts WHERE id = ? AND attempt_public_id = ?",
            (attempt["id"], attempt["attempt_public_id"]),
        ).fetchone()
        if locked_attempt_row is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Fallback attempt changed before result commit."
            )
        locked_attempt = dict(locked_attempt_row)
        if locked_attempt != attempt:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Fallback attempt changed before result commit."
            )
        locked_claim_row = conn.execute(
            "SELECT * FROM ai_fallback_invocation_claims WHERE id = ? AND attempt_id = ?",
            (claim["id"], attempt["id"]),
        ).fetchone()
        if locked_claim_row is None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Invocation claim changed before result commit."
            )
        locked_claim = dict(locked_claim_row)
        _verify_claim_material(locked_attempt, locked_claim)
        if locked_claim != claim:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "Invocation claim changed before result commit."
            )
        locked_intake, locked_parent = _verify_attempt_current_parent_state(conn, locked_attempt)
        _verify_attempt_provenance_material(conn, attempt=locked_attempt)
        attempt = locked_attempt
        claim = locked_claim
        intake = locked_intake
        parent = locked_parent
        parent_payload, _parent_completion_id, _parent_version = resolve_effective_payload(
            conn, parent
        )
        committed_result = conn.execute(
            "SELECT * FROM ai_fallback_results WHERE attempt_id = ?", (attempt["id"],)
        ).fetchone()
        if committed_result is not None:
            if committed_result["result_arguments_hash"] is None:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Legacy result replay cannot be verified against transport material.",
                )
            if committed_result["result_arguments_hash"] != result_arguments_hash:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Result replay carries different transport material.",
                )
            _verify_committed_result_replay(
                conn,
                attempt=attempt,
                result=dict(committed_result),
            )
            conn.commit()
            return _result_response(conn, dict(committed_result)), True
        post_lock = _sample_now_ms(now_ms)
        _assert_fallback_raw_intake_lineage(conn, attempt=attempt)
        current_parent = conn.execute(
            "SELECT public_id, parse_status FROM parser_outputs WHERE id = ?",
            (attempt["parent_parser_output_id"],),
        ).fetchone()
        prepared_parent = conn.execute(
            "SELECT public_id FROM parser_outputs WHERE id = ?",
            (attempt["parent_parser_output_id"],),
        ).fetchone()
        current_parent_full = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (attempt["parent_parser_output_id"],),
        ).fetchone()
        current_hash = (
            None
            if current_parent_full is None
            else compute_effective_proposal_content_hash(
                conn, {"id": attempt["parent_parser_output_id"]}
            )
        )
        _current_payload, _current_completion, current_version = (
            (None, None, None)
            if current_parent_full is None
            else resolve_effective_payload(conn, current_parent_full)
        )
        stale = (
            current_parent is None
            or prepared_parent is None
            or current_parent["public_id"] != prepared_parent["public_id"]
            or current_parent["parse_status"] != PARSED_PENDING_CONFIRMATION
            or current_hash != attempt["parent_effective_content_hash"]
            or current_version != attempt["parent_proposal_version"]
        )
        if stale and result_status == "proposal_created":
            result_status, non_child, recovery = "stale_parent", None, "review_current_parent_state"
        decision_at = _sample_now_ms(now_ms)
        late = decision_at >= int(attempt["result_not_after_ms"])
        if late:
            result_status, non_child, recovery = (
                "late_result",
                None,
                "resend_new_intake_after_late_result",
            )
            normalized_hash = None
            ambiguity_hash = None
            evidence_hash = None
        result_material.update(
            {
                "result_status": result_status,
                "post_lock_at_ms": post_lock,
                "decision_at_ms": decision_at,
                "deadline_disposition": "late" if late else "within_window",
                "non_child_reason": non_child,
                "recovery_disposition": recovery,
                "normalized_payload_hash": normalized_hash,
                "ambiguity_hash": ambiguity_hash,
                "evidence_set_hash": evidence_hash,
            }
        )
        result_hash = result_material_v2_hash(result_material)
        conn.execute(
            """
            INSERT INTO ai_fallback_results (
                result_public_id, result_material_hash, attempt_id, claim_id,
                transport_outcome, result_status, retention_state,
                normal_attribution_hash, metadata_refusal_hash, usage_hash,
                result_received_at_ms, post_lock_at_ms, decision_at_ms,
                deadline_policy_version, deadline_policy_hash, deadline_disposition,
                response_body_state, response_blob, response_sha256, response_byte_count,
                response_code_unit_count, response_utf16_sha256, failure_code,
                non_child_reason, recovery_disposition, normalized_payload_hash,
                source_field_state_hash, ambiguity_hash, evidence_set_hash,
                result_arguments_hash
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                result_public_id,
                result_hash,
                attempt["id"],
                claim["id"],
                transport_outcome,
                result_status,
                retention,
                result_material["normal_attribution_hash"],
                result_material["metadata_refusal_hash"],
                result_material["usage_hash"],
                result_received,
                post_lock,
                decision_at,
                attempt["deadline_policy_version"],
                attempt["deadline_policy_hash"],
                result_material["deadline_disposition"],
                body_state,
                response_blob,
                response_sha,
                response_byte_count,
                response_code_units,
                response_utf16_sha,
                result_material["failure_code"],
                non_child,
                recovery,
                normalized_hash,
                attempt["source_field_state_hash"],
                ambiguity_hash,
                evidence_hash,
                result_arguments_hash,
            ),
        )
        result_id = int(
            conn.execute(
                "SELECT id FROM ai_fallback_results WHERE result_public_id = ?", (result_public_id,)
            ).fetchone()[0]
        )
        if result_status == "proposal_created" and response_payload is not None:
            if normalized_hash is None:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Created proposal is missing its normalized payload hash.",
                )
            child_public_id = _derive_child_public_id(result_public_id, normalized_hash)
            child_confidence = _child_confidence_score(response_payload, normalized)
            parent = conn.execute(
                "SELECT * FROM parser_outputs WHERE id = ?", (attempt["parent_parser_output_id"],)
            ).fetchone()
            intake = conn.execute(
                "SELECT public_id FROM raw_intake_records WHERE id = ?",
                (attempt["raw_intake_record_id"],),
            ).fetchone()
            if parent is None or intake is None:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Fallback source lineage disappeared before child commit.",
                )
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, attachment_id,
                    parser_name, parser_version, ai_provider, ai_model, prompt_version,
                    raw_text, parsed_payload, normalized_payload, confidence_score,
                    parse_status, parent_parser_output_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    child_public_id,
                    parent["source_type"],
                    intake["public_id"],
                    parent["attachment_id"],
                    "finance_ai_proposal",
                    "finance-ai-proposal-v1",
                    attempt["expected_provider"],
                    attempt["expected_model"],
                    attempt["prompt_version"],
                    parent["raw_text"],
                    json.dumps(normalized, sort_keys=True),
                    json.dumps(normalized, sort_keys=True),
                    child_confidence,
                    PARSED_PENDING_CONFIRMATION,
                    parent["id"],
                ),
            )
            child_id = int(
                conn.execute(
                    "SELECT id FROM parser_outputs WHERE public_id = ?", (child_public_id,)
                ).fetchone()[0]
            )
            if planned_evidence is None:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Created proposal is missing its sealed evidence set.",
                )
            for evidence in planned_evidence:
                confidence_bps = evidence["confidence_bps"]
                confidence = None if confidence_bps is None else int(confidence_bps) / 10_000
                conn.execute(
                    """
                    INSERT INTO parser_proposal_field_evidence (
                        parser_output_id, field_name, proposed_value,
                        confidence_score, evidence_source_type,
                        evidence_reference, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child_id,
                        evidence["field_name"],
                        evidence["proposed_value"],
                        confidence,
                        evidence["evidence_source_type"],
                        evidence["evidence_reference"],
                        evidence["notes"],
                    ),
                )
            link_public_id = derive_link_public_id(result_public_id, child_public_id)
            if attempt["source_kind"] == "receipt_local_ocr_text":
                extraction = conn.execute(
                    """
                    SELECT extraction_id, public_id, parser_contract_version, link_role
                    FROM receipt_ocr_proposal_links
                    WHERE parser_output_id = ?
                      AND link_role IN ('initial', 'superseding_correction')
                    """,
                    (attempt["parent_parser_output_id"],),
                ).fetchone()
                if extraction is None:
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_CONFLICT", "OCR lineage disappeared before child commit."
                    )
                if (
                    extraction["link_role"] not in {"initial", "superseding_correction"}
                    or not extraction["public_id"]
                    or not extraction["parser_contract_version"]
                ):
                    raise AiFallbackServiceError(
                        "AI_FALLBACK_CONFLICT",
                        "OCR parent link contract disappeared before child commit.",
                    )
                conn.execute(
                    """
                    INSERT INTO receipt_ocr_proposal_links (
                        public_id, extraction_id, parser_output_id,
                        proposal_input_hash, proposal_result_hash,
                        parser_contract_version, link_role
                    ) VALUES (?, ?, ?, ?, ?, 'finance-ai-proposal-v1', 'ai_fallback')
                    """,
                    (
                        _derive_ai_ocr_link_public_id(link_public_id),
                        extraction["extraction_id"],
                        child_id,
                        _ocr_input_hash(attempt["source_projection_hash"]),
                        _ocr_result_hash(normalized_hash),
                    ),
                )
            effective_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
            link_material = {
                "schema_version": "finance-ai-link-material-v1",
                "result_public_id": result_public_id,
                "result_material_hash": result_hash,
                "proposal_public_id": child_public_id,
                "effective_content_hash": effective_hash,
            }
            link_hash = link_material_hash(link_material)
            conn.execute(
                """
                INSERT INTO ai_fallback_proposal_links (
                    link_public_id, link_material_hash, result_id,
                    parser_output_id, proposal_version, effective_content_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (link_public_id, link_hash, result_id, child_id, 0, effective_hash),
            )
            superseded = conn.execute(
                """
                UPDATE parser_outputs
                SET parse_status = 'superseded', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND parse_status = ?
                """,
                (attempt["parent_parser_output_id"], PARSED_PENDING_CONFIRMATION),
            )
            if superseded.rowcount != 1:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Fallback parent changed before lifecycle commit.",
                )
            repointed = conn.execute(
                """
                UPDATE raw_intake_records
                SET parser_output_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND parser_output_id = ?
                """,
                (
                    child_id,
                    PARSED_PENDING_CONFIRMATION,
                    attempt["raw_intake_record_id"],
                    attempt["parent_parser_output_id"],
                ),
            )
            if repointed.rowcount != 1:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Fallback raw-intake pointer changed before lifecycle commit.",
                )
            event_payload = json.dumps(
                {
                    "attempt_public_id": attempt_public_id,
                    "result_public_id": result_public_id,
                    "child_proposal_public_id": child_public_id,
                    "previous_content_hash": attempt["parent_effective_content_hash"],
                    "child_content_hash": effective_hash,
                    "actor_type": "ai",
                },
                sort_keys=True,
            )
            conn.execute(
                """
                INSERT INTO parser_proposal_events (
                    parser_output_id, from_status, to_status, event_type,
                    event_reason, actor_type, actor_identifier, event_payload
                ) VALUES (?, ?, 'superseded', 'superseded', ?, 'ai', ?, ?)
                """,
                (
                    attempt["parent_parser_output_id"],
                    PARSED_PENDING_CONFIRMATION,
                    f"AI fallback child {child_public_id}",
                    attempt["attempt_public_id"],
                    event_payload,
                ),
            )
            conn.execute(
                """
                INSERT INTO parser_proposal_events (
                    parser_output_id, from_status, to_status, event_type,
                    event_reason, actor_type, actor_identifier, event_payload
                ) VALUES (?, NULL, ?, 'created', ?, 'ai', ?, ?)
                """,
                (
                    child_id,
                    PARSED_PENDING_CONFIRMATION,
                    f"AI fallback child of {parent['public_id']}",
                    attempt["attempt_public_id"],
                    event_payload,
                ),
            )
            _append_ai_child_audit_event(
                conn,
                attempt=attempt,
                result_public_id=result_public_id,
                child_id=child_id,
                child_public_id=child_public_id,
                child_content_hash=effective_hash,
                link_public_id=link_public_id,
                link_material_hash_value=link_hash,
                decision_at_ms=decision_at,
            )
        conn.commit()
    except AiFallbackServiceError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        committed_result = conn.execute(
            "SELECT * FROM ai_fallback_results WHERE attempt_id = ?", (attempt["id"],)
        ).fetchone()
        if committed_result is not None:
            if (
                committed_result["result_arguments_hash"] is not None
                and committed_result["result_arguments_hash"] == result_arguments_hash
            ):
                _verify_committed_result_replay(
                    conn,
                    attempt=attempt,
                    result=dict(committed_result),
                )
                return _result_response(conn, dict(committed_result)), True
            if committed_result["result_arguments_hash"] is None:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "Legacy result replay cannot be verified against transport material.",
                ) from exc
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "Result replay carries different transport material.",
            ) from exc
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "Result or child lineage collided."
        ) from exc
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise AiFallbackServiceError(
            "AI_FALLBACK_INTERNAL", "Fallback result persistence failed."
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return _result_response(
        conn,
        conn.execute(
            "SELECT * FROM ai_fallback_results WHERE result_public_id = ?", (result_public_id,)
        ).fetchone(),
    ), False


def record_ai_fallback_result_with_disposition(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    transport_outcome: str,
    arguments: Mapping[str, Any],
    now_ms: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist a result only for a preserved v1 attempt."""
    return _record_ai_fallback_result_with_disposition(
        conn,
        attempt_public_id=attempt_public_id,
        transport_outcome=transport_outcome,
        arguments=arguments,
        compatibility_receipt=None,
        now_ms=now_ms,
    )


def record_ai_fallback_result(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    transport_outcome: str,
    arguments: Mapping[str, Any],
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Persist one result and return only its stable result projection."""
    result, _replay = record_ai_fallback_result_with_disposition(
        conn,
        attempt_public_id=attempt_public_id,
        transport_outcome=transport_outcome,
        arguments=arguments,
        now_ms=now_ms,
    )
    return result


def record_ai_fallback_result_v2(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
    transport_outcome: str,
    arguments: Mapping[str, Any],
    now_ms: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Record one receipt-bound result; the v1 atomic result/child path is reused."""
    require_staging_database(conn)
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise AiFallbackServiceError(
            "AI_MODEL_COMPATIBILITY_FK_REFUSED", "Foreign keys must be enabled."
        ) from exc
    row = conn.execute(
        "SELECT id FROM ai_fallback_attempts WHERE attempt_public_id = ?",
        (attempt_public_id,),
    ).fetchone()
    if row is None:
        raise AiFallbackServiceError("AI_FALLBACK_NOT_FOUND", "Fallback attempt was not found.")
    receipt = _verified_compatibility_receipt_for_attempt(conn, attempt_id=int(row["id"]))
    if receipt is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "The fallback attempt has no v2 compatibility receipt."
        )
    result, replay = _record_ai_fallback_result_with_disposition(
        conn,
        attempt_public_id=attempt_public_id,
        transport_outcome=transport_outcome,
        arguments=arguments,
        compatibility_receipt=receipt,
        now_ms=now_ms,
    )
    return {
        **result,
        "receipt_public_id": receipt["receipt_public_id"],
    }, replay


def get_ai_processing_status_v2(
    conn: sqlite3.Connection,
    *,
    intake_public_id: str,
) -> dict[str, Any]:
    """Project durable v2 processing state without consulting current OpenClaw config."""
    require_staging_database(conn)
    row = conn.execute(
        """
        SELECT attempt.*
        FROM raw_intake_records AS intake
        LEFT JOIN ai_fallback_attempts AS attempt
          ON attempt.raw_intake_record_id = intake.id
        WHERE intake.public_id = ?
        """,
        (intake_public_id,),
    ).fetchone()
    if row is None:
        raise AiFallbackServiceError("INTAKE_NOT_FOUND", "Raw intake was not found.")
    try:
        admission_decision = get_ai_model_admission_decision_v2(
            conn, intake_public_id=intake_public_id
        )
    except AiModelAdmissionError as exc:
        raise AiFallbackServiceError(exc.code, str(exc)) from exc
    claim = None
    result = None
    receipt = None
    if row["attempt_public_id"] is not None:
        claim = conn.execute(
            "SELECT 1 FROM ai_fallback_invocation_claims WHERE attempt_id = ?", (row["id"],)
        ).fetchone()
        result = conn.execute(
            "SELECT result_status, failure_code FROM ai_fallback_results WHERE attempt_id = ?",
            (row["id"],),
        ).fetchone()
        try:
            receipt = _verified_compatibility_receipt_for_attempt(conn, attempt_id=int(row["id"]))
        except AiFallbackServiceError:
            receipt = None
    return derive_ai_processing_status_v2(
        {
            "intake_public_id": intake_public_id,
            "admission_decision_public_id": (
                None if admission_decision is None else admission_decision["decision_public_id"]
            ),
            "admission_safe_reason_code": (
                None if admission_decision is None else admission_decision["safe_reason_code"]
            ),
            **dict(row),
            **({} if receipt is None else receipt),
        },
        claim_exists=claim is not None,
        result_status=None if result is None else str(result["result_status"]),
        failure_code=None if result is None else result["failure_code"],
    )


def has_ai_fallback_result(
    conn: sqlite3.Connection,
    *,
    attempt_public_id: str,
) -> bool:
    """Return whether an attempt already has its immutable result row."""
    require_staging_database(conn)
    row = conn.execute(
        """
        SELECT 1
        FROM ai_fallback_results AS result
        JOIN ai_fallback_attempts AS attempt ON attempt.id = result.attempt_id
        WHERE attempt.attempt_public_id = ?
        """,
        (attempt_public_id,),
    ).fetchone()
    return row is not None


def _require_matching_hash(actual: Any, expected: str, label: str) -> None:
    if actual != expected:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            f"AI fallback {label} does not verify.",
        )


def _verify_claim_material(attempt: Mapping[str, Any], claim: Mapping[str, Any]) -> None:
    claim_material = {
        "schema_version": "finance-ai-claim-material-v1",
        "attempt_public_id": attempt["attempt_public_id"],
        "invocation_claimed_at_ms": claim["invocation_claimed_at_ms"],
        "call_start_not_after_ms": claim["call_start_not_after_ms"],
        "request_sha256": attempt["request_sha256"],
        "invocation_disposition": claim["invocation_disposition"],
    }
    _require_matching_hash(
        claim["claim_material_hash"],
        claim_material_hash(claim_material),
        "claim hash",
    )
    _require_matching_hash(
        claim["claim_public_id"],
        derive_claim_public_id(str(attempt["attempt_public_id"])),
        "claim identity",
    )
    claimed_at = claim["invocation_claimed_at_ms"]
    start_not_after = claim["call_start_not_after_ms"]
    if (
        int(claim["attempt_id"]) != int(attempt["id"])
        or claim["invocation_disposition"] != "invoke_once"
        or not isinstance(claimed_at, int)
        or isinstance(claimed_at, bool)
        or not isinstance(start_not_after, int)
        or isinstance(start_not_after, bool)
        or claimed_at < int(attempt["prepared_at_ms"])
        or claimed_at >= int(attempt["invoke_not_after_ms"])
        or start_not_after != claimed_at + CALL_START_WINDOW_MS
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback claim material does not verify."
        )


def _persisted_child_evidence(
    conn: sqlite3.Connection,
    parser_output_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT field_name, proposed_value, confidence_score,
               evidence_source_type, evidence_reference, notes
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output_id,),
    ).fetchall()
    material: list[dict[str, Any]] = []
    for row in rows:
        confidence = row["confidence_score"]
        material.append(
            {
                "field_name": row["field_name"],
                "proposed_value": row["proposed_value"],
                "confidence_bps": (
                    None if confidence is None else int(round(float(confidence) * 10_000))
                ),
                "evidence_source_type": row["evidence_source_type"],
                "evidence_reference": row["evidence_reference"],
                "notes": row["notes"],
            }
        )
    return material


def _verify_attempt_provenance_material(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    """Verify the complete immutable preparation material for any replay.

    Result replay and child verification must share this exact attempt-level
    verifier. In particular, a durable result cannot make changed model,
    policy, source projection, parent fields, or attempt identity look like a
    valid replay merely because no child is present.
    """
    verified = cast(
        tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]],
        _verify_ai_provenance_material(
            conn,
            attempt=attempt,
            claim=None,
            result=None,
            link=None,
            child=None,
            verify_child=False,
        ),
    )
    parent_payload, catalog, parent, intake = verified
    return parent, intake, parent_payload, catalog


def _verify_ai_provenance_material(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    claim: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    link: Mapping[str, Any] | None,
    child: Mapping[str, Any] | None,
    verify_child: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]] | dict[str, Any]:
    parent_cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (attempt["parent_parser_output_id"],)
    )
    parent_row = parent_cursor.fetchone()
    intake_cursor = conn.execute(
        "SELECT * FROM raw_intake_records WHERE id = ?", (attempt["raw_intake_record_id"],)
    )
    intake_row = intake_cursor.fetchone()
    if parent_row is None or intake_row is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source lineage is incomplete."
        )
    parent = _row_dict(parent_row, parent_cursor.description)
    intake = _row_dict(intake_row, intake_cursor.description)
    parent_payload, _completion_id, parent_version = resolve_effective_payload(conn, parent)
    source_intake = dict(intake)
    # The raw-intake pointer is intentionally repointed to the AI child after
    # commit; rebuild the immutable pre-child source projection against the
    # original parent binding.
    source_intake["parser_output_id"] = parent["id"]
    source_kind, source_segments = _source_segments(conn, source_intake, parent)
    if source_kind != attempt["source_kind"]:
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "AI fallback source kind has drifted.")
    _require_matching_hash(
        attempt["parent_effective_content_hash"],
        compute_effective_proposal_content_hash(conn, parent),
        "parent effective-content hash",
    )
    request_blob = bytes(attempt["request_blob"])
    _require_matching_hash(
        attempt["request_sha256"],
        canonical_request_sha256(request_blob),
        "request hash",
    )
    if int(attempt["request_byte_count"]) != len(request_blob):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback request byte count does not verify."
        )
    try:
        request = json.loads(request_blob.decode("utf-8"))
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        messages = request.get("messages")
        if (
            not isinstance(messages, list)
            or len(messages) != 1
            or not isinstance(messages[0], dict)
            or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)
        ):
            raise TypeError("request messages are invalid")
        projection_text = messages[0]["content"]
        projection = json.loads(projection_text)
        if not isinstance(projection, dict):
            raise TypeError("projection must be an object")
        catalog = projection.get("evidence_catalog")
        if not isinstance(catalog, list):
            raise TypeError("evidence catalog must be a list")
        for entry in catalog:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("ref"), str)
                or not isinstance(entry.get("kind"), str)
                or not isinstance(entry.get("text"), str)
            ):
                raise TypeError("evidence catalog entry is invalid")
        reasons = json.loads(attempt["eligibility_reasons_json"])
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            raise TypeError("eligibility reasons are invalid")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback request material cannot be verified."
        ) from exc
    assets = _policy_assets()
    receipt = _verified_compatibility_receipt_for_attempt(conn, attempt_id=int(attempt["id"]))
    if receipt is None:
        runtime_policy_values = {
            "runtime_policy_version": assets["runtime"]["version"],
            "runtime_policy_hash": _asset_hash(assets, "runtime_policy"),
        }
        runtime_values = {
            "expected_provider": assets["runtime"]["provider"],
            "expected_model": assets["runtime"]["model"],
            "expected_agent_id": assets["runtime"]["agent_id"],
            "expected_audit_caller_kind": assets["runtime"]["audit_caller_kind"],
            "expected_audit_caller_id": assets["runtime"]["audit_caller_id"],
            "expected_audit_caller_name": assets["runtime"]["audit_caller_name"],
            "expected_audit_purpose": assets["runtime"]["audit_purpose"],
            "expected_audit_session_key_sha256": None,
        }
    else:
        try:
            verify_persisted_compatibility_receipt(receipt)
        except ModelCompatibilityError as exc:
            raise AiFallbackServiceError(exc.code, str(exc)) from exc
        runtime_policy_values = {
            "runtime_policy_version": receipt["projection_policy_version"],
            "runtime_policy_hash": receipt["config_projection_hash"],
        }
        runtime_values = {
            "expected_provider": receipt["canonical_provider"],
            "expected_model": receipt["canonical_model"],
            "expected_agent_id": receipt["agent_id"],
            "expected_audit_caller_kind": AUDIT_CALLER_KIND,
            "expected_audit_caller_id": AUDIT_CALLER_ID,
            "expected_audit_caller_name": None,
            "expected_audit_purpose": AUDIT_PURPOSE,
            "expected_audit_session_key_sha256": None,
        }
    expected_policy_values = {
        **runtime_policy_values,
        "prompt_version": assets["registry"].asset("prompt").version,
        "prompt_template_hash": _asset_hash(assets, "prompt"),
        "intent_policy_version": assets["intent"]["version"],
        "intent_policy_hash": _asset_hash(assets, "intent_policy"),
        "default_policy_version": assets["default"]["version"],
        "default_policy_hash": _asset_hash(assets, "default_policy"),
        "sensitive_text_policy_version": assets["sensitive"]["version"],
        "sensitive_text_policy_hash": _asset_hash(assets, "sensitive_text_policy"),
        "deadline_policy_version": assets["deadline"]["version"],
        "deadline_policy_hash": _asset_hash(assets, "deadline_policy"),
        "sqlite_money_policy_version": assets["sqlite_money"]["version"],
        "sqlite_money_policy_hash": _asset_hash(assets, "sqlite_money_policy"),
        **runtime_values,
    }
    if any(attempt[field] != value for field, value in expected_policy_values.items()):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback policy provenance does not verify."
        )
    if (
        request.get("model") != f"{attempt['expected_provider']}/{attempt['expected_model']}"
        or request.get("agentId") != attempt["expected_agent_id"]
        or request.get("purpose") != attempt["expected_audit_purpose"]
        or request.get("systemPrompt") != assets["prompt"]
        or request.get("maxTokens") != MODEL_MAX_TOKENS
        or request.get("temperature") != MODEL_TEMPERATURE
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback request policy does not verify."
        )
    selected = [{"ref": entry["ref"], "kind": entry["kind"]} for entry in catalog]
    source_bytes = sum(len(str(entry["text"]).encode("utf-8")) for entry in catalog)
    source_text = "\n".join(text for _kind, text in source_segments)
    forbidden_fields = _forbidden_parent_fields(parent_payload)
    if forbidden_fields:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "AI fallback parent carries a forbidden destination field.",
        )
    try:
        source_intent, source_mode, _source_intent_reasons = _intent_result(
            source_text, assets["intent"]
        )
    except AiFallbackServiceError as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback intent policy cannot be revalidated."
        ) from exc
    if source_intent == "deny" or source_intent != attempt["intent_policy_result"]:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source intent policy has drifted."
        )
    expected_mode = "child_eligible" if source_intent == "positive" else "classification_only"
    if source_mode != expected_mode or attempt["fallback_mode"] != expected_mode:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback intent eligibility mode has drifted."
        )
    try:
        stored_reasons = json.loads(str(attempt["eligibility_reasons_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback eligibility reasons cannot be revalidated."
        ) from exc
    if not isinstance(stored_reasons, list):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback eligibility reasons cannot be revalidated."
        )
    if source_intent == "unknown" and "intent_classification_required" not in stored_reasons:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback classification provenance is incomplete."
        )
    if source_intent == "positive" and "intent_classification_required" in stored_reasons:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback positive intent provenance is inconsistent."
        )
    for segment in source_segments:
        if _scan_sensitive(segment[1], assets["sensitive"]) is not None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "AI fallback source now contains sensitive text."
            )
    try:
        (
            expected_projection,
            expected_selected,
            _expected_projection_hash,
            _expected_selection_hash,
            _,
        ) = _projection(
            source_kind=source_kind,
            segments=source_segments,
            reasons=sorted(set(reasons)),
        )
    except AiFallbackServiceError as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source projection cannot be rebuilt."
        ) from exc
    if projection != expected_projection or selected != expected_selected:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source projection no longer matches evidence."
        )
    if _scan_sensitive(str(projection_text), assets["sensitive"]) is not None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback user projection now contains sensitive text."
        )
    _require_matching_hash(
        attempt["source_projection_hash"],
        _sha256_bytes(str(projection_text).encode("utf-8")),
        "source projection hash",
    )
    _require_matching_hash(
        attempt["source_selection_manifest_hash"],
        _hash_material("finance-ai-source-selection-v1", {"selected": selected}),
        "source selection hash",
    )
    if int(attempt["source_projection_byte_count"]) != source_bytes:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source byte count does not verify."
        )
    _require_matching_hash(
        attempt["source_field_state_hash"],
        _field_state_hash(parent_payload),
        "source field-state hash",
    )
    positive = _effective_fields(parent_payload)[1]
    _require_matching_hash(
        attempt["intent_evidence_hash"],
        _hash_material(
            "finance-ai-intent-evidence-v1",
            {
                "result": attempt["intent_policy_result"],
                "positive": positive,
                "source_hash": _sha256_bytes(source_text.encode("utf-8")),
            },
        ),
        "intent evidence hash",
    )
    _require_matching_hash(
        attempt["sensitive_text_scan_hash"],
        _hash_material(
            "finance-ai-sensitive-scan-v1",
            {
                "result": "clear",
                "source_hash": _sha256_bytes(source_text.encode("utf-8")),
            },
        ),
        "sensitive-text scan hash",
    )
    preparation = {
        "schema_version": "finance-ai-preparation-material-v1",
        "intake_public_id": intake["public_id"],
        "parent_public_id": parent["public_id"],
        "parent_version": parent_version,
        "parent_effective_content_hash": attempt["parent_effective_content_hash"],
        "source_kind": attempt["source_kind"],
        "source_projection_hash": attempt["source_projection_hash"],
        "source_selection_manifest_hash": attempt["source_selection_manifest_hash"],
        "source_field_state_hash": attempt["source_field_state_hash"],
        "eligibility_mode": attempt["fallback_mode"],
        "eligibility_reasons": reasons,
        "runtime_policy_version": attempt["runtime_policy_version"],
        "runtime_policy_hash": attempt["runtime_policy_hash"],
        "prompt_version": attempt["prompt_version"],
        "prompt_template_hash": attempt["prompt_template_hash"],
        "intent_policy_version": attempt["intent_policy_version"],
        "intent_policy_hash": attempt["intent_policy_hash"],
        "intent_policy_result": attempt["intent_policy_result"],
        "intent_evidence_hash": attempt["intent_evidence_hash"],
        "default_policy_version": attempt["default_policy_version"],
        "default_policy_hash": attempt["default_policy_hash"],
        "default_evidence_hash": attempt["default_evidence_hash"],
        "sensitive_text_policy_version": attempt["sensitive_text_policy_version"],
        "sensitive_text_policy_hash": attempt["sensitive_text_policy_hash"],
        "sensitive_text_scan_hash": attempt["sensitive_text_scan_hash"],
        "deadline_policy_version": attempt["deadline_policy_version"],
        "deadline_policy_hash": attempt["deadline_policy_hash"],
        "sqlite_money_policy_version": attempt["sqlite_money_policy_version"],
        "sqlite_money_policy_hash": attempt["sqlite_money_policy_hash"],
        "expected_provider": attempt["expected_provider"],
        "expected_model": attempt["expected_model"],
        "expected_agent_id": attempt["expected_agent_id"],
        "expected_audit_caller_kind": attempt["expected_audit_caller_kind"],
        "expected_audit_caller_id": attempt["expected_audit_caller_id"],
        "expected_audit_caller_name": attempt["expected_audit_caller_name"],
        "expected_audit_purpose": attempt["expected_audit_purpose"],
        "expected_audit_session_key_sha256": attempt["expected_audit_session_key_sha256"],
        "request_sha256": attempt["request_sha256"],
        "request_byte_count": attempt["request_byte_count"],
    }
    prep_hash = preparation_material_hash(preparation)
    _require_matching_hash(attempt["preparation_material_hash"], prep_hash, "preparation hash")
    _require_matching_hash(
        attempt["attempt_public_id"],
        derive_attempt_public_id(prep_hash),
        "attempt identity",
    )

    if not verify_child:
        return (
            parent_payload,
            {str(entry["ref"]): str(entry["text"]) for entry in catalog},
            parent,
            intake,
        )
    if claim is None or result is None or link is None or child is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI child verification material is incomplete."
        )
    _verify_claim_material(attempt, claim)

    result_material = _sealed_result_material(attempt=attempt, claim=claim, result=result)
    result_hash = result_material_v2_hash(result_material)
    _require_matching_hash(result["result_material_hash"], result_hash, "result hash")
    _require_matching_hash(
        result["result_public_id"],
        derive_result_public_id(attempt["attempt_public_id"]),
        "result identity",
    )
    response_blob = bytes(result["response_blob"])
    _require_matching_hash(
        result["response_sha256"],
        canonical_response_sha256(response_blob),
        "response hash",
    )
    if int(result["response_byte_count"]) != len(response_blob):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback response byte count does not verify."
        )
    response_payload = None
    if result["result_status"] == "proposal_created":
        if result["response_body_state"] != "retained":
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "A created AI proposal must retain its validated response body.",
            )
        try:
            response_payload = _validate_ai_response(
                response_blob,
                {str(entry["ref"]) for entry in catalog},
                parent_payload=parent_payload,
                catalog={str(entry["ref"]): str(entry["text"]) for entry in catalog},
                source_kind=str(attempt["source_kind"]),
                ocr_layout=load_ocr_layout(
                    conn, parent_payload=parent_payload, source_kind=str(attempt["source_kind"])
                ),
            )
        except Exception as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "AI fallback response payload cannot be verified."
            ) from exc

    try:
        child_payload = json.loads(child["parsed_payload"])
        child_normalized = json.loads(child["normalized_payload"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI child payload cannot be verified."
        ) from exc
    if child_payload != child_normalized:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI child parsed and normalized payloads disagree."
        )
    if response_payload is not None:
        expected_payload = {
            "intent": "personal_expense",
            "transaction_type": "personal_expense",
            "amount": (
                response_payload["amount"]
                if response_payload["amount"] is not None
                else _payload_field(parent_payload, "amount")
            ),
            "currency": (
                response_payload["currency"]
                if response_payload["currency"] is not None
                else _payload_field(parent_payload, "currency")
            ),
            "transaction_date": (
                response_payload["transaction_date"]
                if response_payload["transaction_date"] is not None
                else _payload_field(parent_payload, "transaction_date")
            ),
            "merchant": (
                response_payload["merchant"]
                if response_payload["merchant"] is not None
                else _payload_field(parent_payload, "merchant")
            ),
            "description": None,
            "account": None,
            "category": None,
            "ambiguity_flags": [],
        }
        expected_payload["ambiguity_flags"] = _normalized_ambiguity_flags(
            attempt,
            parent_payload,
            expected_payload,
            response_payload["ambiguity_flags"],
        )
        if child_normalized != expected_payload:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "AI child payload does not match its retained response body.",
            )
        expected_confidence = _child_confidence_score(response_payload, expected_payload)
        if (
            child["confidence_score"] is None
            or abs(float(child["confidence_score"]) - expected_confidence) > 1e-9
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "AI child confidence does not match its retained response body.",
            )
    _require_matching_hash(
        result["normalized_payload_hash"],
        _sha256_bytes(_json_bytes(child_payload)),
        "normalized payload hash",
    )
    _require_matching_hash(
        result["ambiguity_hash"],
        _hash_material(
            "finance-ai-ambiguity-v1",
            {"flags": child_payload.get("ambiguity_flags")},
        ),
        "ambiguity hash",
    )
    _require_matching_hash(
        result["evidence_set_hash"],
        _hash_material(
            "finance-ai-evidence-set-v1",
            {"rows": _persisted_child_evidence(conn, int(child["id"]))},
        ),
        "evidence-set hash",
    )
    link_material = {
        "schema_version": "finance-ai-link-material-v1",
        "result_public_id": result["result_public_id"],
        "result_material_hash": result["result_material_hash"],
        "proposal_public_id": child["public_id"],
        "effective_content_hash": link["effective_content_hash"],
    }
    link_hash = link_material_hash(link_material)
    _require_matching_hash(link["link_material_hash"], link_hash, "link hash")
    _require_matching_hash(
        link["link_public_id"],
        derive_link_public_id(result["result_public_id"], child["public_id"]),
        "link identity",
    )
    _require_matching_hash(
        child["public_id"],
        _derive_child_public_id(result["result_public_id"], result["normalized_payload_hash"]),
        "child identity",
    )
    audit_event_type = "parser_proposal_ai_fallback_child_created"
    audit_event_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=str(child["public_id"]),
        event_type=audit_event_type,
        causation_public_id=str(result["result_public_id"]),
    )
    audit_chain = verify_financial_audit_chain(
        conn,
        aggregate_type="parser_proposal",
        aggregate_public_id=str(child["public_id"]),
    )
    audit_event = FinancialAuditRepository(conn).fetch(audit_event_id)
    expected_audit_payload = canonical_json_text(
        {
            "attempt_public_id": attempt["attempt_public_id"],
            "result_public_id": result["result_public_id"],
            "child_proposal_public_id": child["public_id"],
            "parent_effective_content_hash": attempt["parent_effective_content_hash"],
            "child_effective_content_hash": link["effective_content_hash"],
            "proposal_link_public_id": link["link_public_id"],
            "proposal_link_material_hash": link["link_material_hash"],
        }
    )
    expected_audit_state = canonical_json_text(
        {
            "parse_status": PARSED_PENDING_CONFIRMATION,
            "raw_intake_status": raw_intake_status_for_proposal_status(PARSED_PENDING_CONFIRMATION),
            "proposal_content_hash": link["effective_content_hash"],
            "conversion_status": "not_converted",
        }
    )
    expected_sources = _ai_child_source_evidence_references(
        conn,
        attempt=attempt,
        result_public_id=str(result["result_public_id"]),
        child_id=int(child["id"]),
        child_public_id=str(child["public_id"]),
        link_public_id=str(link["link_public_id"]),
    )
    if (
        not audit_chain.valid
        or audit_chain.legacy_without_chain
        or audit_event is None
        or audit_event.event_public_id != audit_event_id
        or audit_event.audit_schema_version != AUDIT_SCHEMA_VERSION
        or audit_event.aggregate_type != "parser_proposal"
        or audit_event.aggregate_public_id != child["public_id"]
        or audit_event.event_type != audit_event_type
        or audit_event.event_payload_json != expected_audit_payload
        or audit_event.previous_state_json != canonical_json_text(None)
        or audit_event.new_state_json != expected_audit_state
        or audit_event.actor_type != "ai"
        or audit_event.actor_public_id != attempt["expected_agent_id"]
        or audit_event.authorization_public_id is not None
        or audit_event.calculation_snapshot_public_id is not None
        or audit_event.calculation_snapshot_hash is not None
        or audit_event.source_evidence_references != expected_sources
        or audit_event.correlation_public_id != attempt["attempt_public_id"]
        or audit_event.causation_public_id != result["result_public_id"]
        or audit_event.sequence_number != 1
        or audit_event.previous_event_hash != ZERO_AUDIT_HASH
        or audit_event.created_at != _audit_timestamp(int(result["decision_at_ms"]))
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "AI child financial audit chain is missing or invalid.",
        )
    if attempt["source_kind"] == "receipt_local_ocr_text":
        parent_ocr_link = conn.execute(
            """
            SELECT public_id, extraction_id, parser_output_id,
                   parser_contract_version, link_role
            FROM receipt_ocr_proposal_links
            WHERE parser_output_id = ?
              AND link_role IN ('initial', 'superseding_correction')
            """,
            (attempt["parent_parser_output_id"],),
        ).fetchall()
        ocr_link = conn.execute(
            """
            SELECT public_id, extraction_id, proposal_input_hash, proposal_result_hash,
                   parser_contract_version, link_role
            FROM receipt_ocr_proposal_links
            WHERE parser_output_id = ? AND link_role = 'ai_fallback'
            """,
            (child["id"],),
        ).fetchall()
        expected_ocr_link_id = _derive_ai_ocr_link_public_id(str(link["link_public_id"]))
        if (
            len(parent_ocr_link) != 1
            or len(ocr_link) != 1
            or parent_ocr_link[0]["parser_output_id"] != attempt["parent_parser_output_id"]
            or parent_ocr_link[0]["public_id"] is None
            or not parent_ocr_link[0]["parser_contract_version"]
            or parent_ocr_link[0]["link_role"] not in {"initial", "superseding_correction"}
            or ocr_link[0]["extraction_id"] != parent_ocr_link[0]["extraction_id"]
            or ocr_link[0]["public_id"] != expected_ocr_link_id
            or ocr_link[0]["parser_contract_version"] != "finance-ai-proposal-v1"
            or ocr_link[0]["link_role"] != "ai_fallback"
        ):
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT", "AI OCR child link cardinality does not verify."
            )
        _require_matching_hash(
            ocr_link[0]["proposal_input_hash"],
            _ocr_input_hash(attempt["source_projection_hash"]),
            "OCR input hash",
        )
        _require_matching_hash(
            ocr_link[0]["proposal_result_hash"],
            _ocr_result_hash(result["normalized_payload_hash"]),
            "OCR result hash",
        )
    return child_payload


def verify_ai_fallback_child(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
    *,
    content_hash: str,
    proposal_version: int,
    require_resolved: bool = True,
    _skip_human: bool = False,
    _human_descendant_parser_output_id: int | None = None,
) -> dict[str, Any] | None:
    """Verify the immutable AI edge before a human or conversion reader proceeds."""
    if not _skip_human:
        from finance_core.parser_proposals.human_revision import (
            HumanRevisionLineageError,
            verify_human_revision_descendant,
        )

        try:
            human_lineage = verify_human_revision_descendant(
                conn,
                proposal,
                content_hash=content_hash,
                proposal_version=proposal_version,
            )
        except HumanRevisionLineageError as exc:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "Human descendant of AI fallback lineage could not be verified.",
            ) from exc
        if human_lineage is not None:
            if human_lineage.get("root_proposal_origin") != "ai_fallback":
                return None
            if require_resolved and human_lineage.get("requires_resolution") is True:
                raise AiFallbackServiceError(
                    "AI_FALLBACK_CONFLICT",
                    "AI proposal ambiguity remains unresolved.",
                )
            return {
                **human_lineage,
                "proposal_origin": "ai_fallback",
            }
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_fallback_proposal_links'"
    ).fetchone()
    if table is None:
        return None
    try:
        proposal_id = proposal["id"]
    except (KeyError, IndexError, TypeError):
        proposal_id = None
    if not isinstance(proposal_id, int) or isinstance(proposal_id, bool) or proposal_id < 1:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI proposal projection does not contain a valid identity."
        )
    link = conn.execute(
        "SELECT * FROM ai_fallback_proposal_links WHERE parser_output_id = ?",
        (proposal_id,),
    ).fetchone()
    if link is None:
        escaped = conn.execute(
            """
            SELECT 1
            FROM raw_intake_records AS intake
            JOIN ai_fallback_attempts AS attempt
              ON attempt.raw_intake_record_id = intake.id
            LEFT JOIN ai_fallback_results AS result
              ON result.attempt_id = attempt.id
            LEFT JOIN ai_fallback_proposal_links AS child_link
              ON child_link.result_id = result.id
            WHERE (
                intake.parser_output_id = ?
                OR attempt.parent_parser_output_id = ?
                OR child_link.parser_output_id = ?
            )
              AND (
                  (result.id IS NULL
                   AND intake.parser_output_id IS NOT attempt.parent_parser_output_id)
                  OR (result.id IS NOT NULL
                      AND result.result_status = 'proposal_created'
                      AND (child_link.parser_output_id IS NULL
                           OR intake.parser_output_id IS NOT child_link.parser_output_id))
                  OR (result.id IS NOT NULL
                      AND result.result_status != 'proposal_created'
                      AND intake.parser_output_id IS NOT attempt.parent_parser_output_id)
              )
            """,
            (proposal_id, proposal_id, proposal_id),
        ).fetchone()
        if escaped is not None:
            raise AiFallbackServiceError(
                "AI_FALLBACK_CONFLICT",
                "Current proposal escaped its sealed AI fallback lineage.",
            )
        return None
    result = conn.execute(
        "SELECT * FROM ai_fallback_results WHERE id = ?", (link["result_id"],)
    ).fetchone()
    if result is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback result lineage is missing."
        )
    claim = conn.execute(
        "SELECT * FROM ai_fallback_invocation_claims WHERE id = ?", (result["claim_id"],)
    ).fetchone()
    attempt = conn.execute(
        "SELECT * FROM ai_fallback_attempts WHERE id = ?", (result["attempt_id"],)
    ).fetchone()
    child = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (link["parser_output_id"],)
    ).fetchone()
    if claim is None or attempt is None or child is None:
        raise AiFallbackServiceError("AI_FALLBACK_CONFLICT", "AI fallback lineage is incomplete.")
    parent = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (attempt["parent_parser_output_id"],)
    ).fetchone()
    intake = conn.execute(
        "SELECT * FROM raw_intake_records WHERE id = ?", (attempt["raw_intake_record_id"],)
    ).fetchone()
    if parent is None or intake is None:
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI fallback source lineage is missing."
        )
    if (
        child["source_type"] != parent["source_type"]
        or child["source_public_id"] != intake["public_id"]
        or intake["parser_output_id"]
        != (
            child["id"]
            if _human_descendant_parser_output_id is None
            else _human_descendant_parser_output_id
        )
        or child["attachment_id"] != parent["attachment_id"]
        or child["parser_name"] != "finance_ai_proposal"
        or child["parser_version"] != "finance-ai-proposal-v1"
        or child["raw_text"] != parent["raw_text"]
        or child["parent_parser_output_id"] != parent["id"]
        or child["ai_provider"] != attempt["expected_provider"]
        or child["ai_model"] != attempt["expected_model"]
        or child["prompt_version"] != attempt["prompt_version"]
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT", "AI child source or attribution fields do not verify."
        )
    _assert_fallback_raw_intake_lineage(
        conn,
        attempt=attempt,
        child_parser_output_id=int(child["id"]),
        current_parser_output_id=_human_descendant_parser_output_id,
    )
    if (
        link["effective_content_hash"] != content_hash
        or int(link["proposal_version"]) != proposal_version
        or result["result_status"] != "proposal_created"
        or child["parent_parser_output_id"] != attempt["parent_parser_output_id"]
        or result["claim_id"] != claim["id"]
        or claim["attempt_id"] != attempt["id"]
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "AI proposal lineage is stale or inconsistent.",
        )
    payload = _verify_ai_provenance_material(
        conn,
        attempt=dict(attempt),
        claim=dict(claim),
        result=dict(result),
        link=dict(link),
        child=dict(child),
    )
    if not isinstance(payload, dict):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "AI proposal payload cannot be verified.",
        )
    ambiguity_flags = payload.get("ambiguity_flags")
    allowed_flags = (
        RECEIPT_AMBIGUITY_FLAGS
        if attempt["source_kind"] == "receipt_local_ocr_text"
        else _AMBIGUITY_FLAGS
    )
    if (
        not isinstance(ambiguity_flags, list)
        or len(ambiguity_flags) != len(set(ambiguity_flags))
        or any(not isinstance(flag, str) or flag not in allowed_flags for flag in ambiguity_flags)
    ):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "AI proposal ambiguity provenance is invalid.",
        )
    confidence_requires_resolution = (
        child["confidence_score"] is None
        or float(child["confidence_score"]) < AI_CONFIRMATION_CONFIDENCE_THRESHOLD
    )
    if require_resolved and (ambiguity_flags or confidence_requires_resolution):
        raise AiFallbackServiceError(
            "AI_FALLBACK_CONFLICT",
            "AI proposal ambiguity remains unresolved.",
        )
    return {
        "proposal_origin": "ai_fallback",
        "ai_source_kind": attempt["source_kind"],
        "ambiguity_flags": tuple(ambiguity_flags),
        "requires_resolution": bool(ambiguity_flags or confidence_requires_resolution),
    }


__all__ = [
    "AiFallbackServiceError",
    "claim_ai_fallback_invocation",
    "has_ai_fallback_result",
    "prepare_ai_fallback",
    "record_ai_fallback_result",
    "verify_ai_fallback_child",
]
