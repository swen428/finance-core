"""Finance authority for Nomi OpenClaw model compatibility receipts v2.

This module is deliberately unreachable from the reviewed Telegram plugin in
PR A.  It accepts only a bounded, non-secret Agent projection and raw fixed-
harness observations; Python recomputes the verdict and owns canonical hashes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from finance_core.parser_proposals.ai_fact_observations import normalize_fact_observations
from finance_core.parser_proposals.ai_ocr_layout import verify_ocr_layout
from finance_core.parser_proposals.ai_response_validation import validate_ai_response
from finance_core.parser_proposals.ai_source_assessment import assess_source
from finance_core.sqlite_connection import ForeignKeysDisabledError, require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECTION_POLICY_PATH = (
    PROJECT_ROOT / "finance_core/resources/finance_ai/agent_projection_policy_v2.json"
)
FIXTURE_SET_PATH = (
    PROJECT_ROOT / "finance_core/resources/finance_ai/model_compatibility_fixture_set_v1.json"
)
ASSET_REGISTRY_PATH = PROJECT_ROOT / "finance_core/resources/finance_ai/asset_registry_v2.json"

PROJECTION_SCHEMA_VERSION = "finance-openclaw-agent-config-projection-v2"
RECEIPT_SCHEMA_VERSION = "finance-ai-model-compatibility-receipt-v2"
VERIFICATION_METHOD = "python_recomputed_fixed_harness_v1"
FRESHNESS_RULE = "hash_bound_no_calendar_ttl_v1"
OPERATOR_WORKFLOW_VERSION = "finance-model-compatibility-operator-workflow-v1"
AGENT_ID = "finance"
AUDIT_CALLER_KIND = "plugin"
AUDIT_CALLER_ID = "finance-bridge"
AUDIT_PURPOSE = "finance-bridge.ai-proposal-v2"
MAX_OUTCOME_BYTES = 65_536
MAX_RESPONSE_BYTES = 16_384
MAX_ELAPSED_MS = 30_000
FIXED_FIXTURE_CASE_IDS = (
    "clear_text",
    "clear_ocr",
    "ambiguous_amount_currency",
    "ocr_prompt_injection",
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")
_ALIAS_RE = re.compile(r"^[^\x00-\x1f\x7f\n\r\t]{1,64}$")
_PROJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "openclaw_version",
        "openclaw_package_sha256",
        "finance_commit",
        "plugin_build_sha256",
        "agent_id",
        "canonical_provider",
        "canonical_model",
        "display_alias",
        "execution_class",
        "fallbacks",
        "effective_max_retries",
        "tool_policy_sha256",
        "memory_policy_sha256",
        "plugin_binding_policy_sha256",
        "projection_policy_version",
        "projection_policy_sha256",
        "prompt_version",
        "prompt_sha256",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "case_id",
        "ordinary_agent_turn_count",
        "isolated_completion_count",
        "provider_dispatch_count",
        "effective_max_retries",
        "elapsed_ms",
        "observed_provider",
        "observed_model",
        "observed_agent_id",
        "response_utf8_b64",
    }
)
_PROPOSAL_FIELDS = frozenset(
    {"amount", "currency", "transaction_date", "merchant", "description", "account", "category"}
)


class ModelCompatibilityError(RuntimeError):
    """Stable fail-closed receipt authority error."""

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


class _VerificationRefusal(ValueError):
    """Private fixed-code refusal that never carries response values."""

    def __init__(self, reason: str, *, field: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


def _verification_details(reason: str, *, field: str | None = None) -> dict[str, str]:
    details = {"verification_reason": reason}
    if field is not None:
        details["verification_field"] = field
    return details


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_projection_hash(projection: Mapping[str, Any]) -> str:
    """Hash the exact canonical projection; shared golden fixtures bind TS parity."""
    return _sha256(_canonical_json_bytes(dict(projection)))


def _unsafe_alias_codepoint(value: str) -> bool:
    return any(
        0xD800 <= ord(character) <= 0xDFFF
        or unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _material_hash(domain: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii"))
    digest.update(b"\0")
    digest.update(_canonical_json_bytes(dict(value)))
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_POLICY_REFUSED", "A required v2 policy asset is invalid."
        ) from exc
    if not isinstance(value, dict):
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_POLICY_REFUSED", "A required v2 policy asset is invalid."
        )
    return value


def policy_assets(repo_root: Path | None = None) -> dict[str, Any]:
    root = PROJECT_ROOT if repo_root is None else repo_root
    registry = _load_json(root / "finance_core/resources/finance_ai/asset_registry_v2.json")
    entries = registry.get("assets")
    expected_names = {"agent_projection_policy", "model_compatibility_fixtures", "prompt"}
    if (
        registry.get("schema_version") != "finance-ai-asset-registry-v2"
        or registry.get("registry_version") != "finance-ai-asset-registry-v2"
        or not isinstance(entries, dict)
        or set(entries) != expected_names
    ):
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_POLICY_REFUSED", "The v2 asset registry is invalid."
        )

    def asset(name: str, expected_path: str) -> tuple[Path, dict[str, Any]]:
        entry = entries[name]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "version", "sha256", "byte_count"}
            or entry["path"] != expected_path
            or not isinstance(entry["version"], str)
            or not isinstance(entry["sha256"], str)
            or _HASH_RE.fullmatch(entry["sha256"]) is None
            or isinstance(entry["byte_count"], bool)
            or not isinstance(entry["byte_count"], int)
            or not 1 <= entry["byte_count"] <= 1_048_576
        ):
            raise ModelCompatibilityError(
                "AI_MODEL_COMPATIBILITY_POLICY_REFUSED", "A v2 asset binding is invalid."
            )
        path = root / expected_path
        try:
            blob = path.read_bytes()
            actual_hash = _sha256(blob)
        except OSError as exc:
            raise ModelCompatibilityError(
                "AI_MODEL_COMPATIBILITY_POLICY_REFUSED", "A required v2 asset is missing."
            ) from exc
        if actual_hash != entry["sha256"] or len(blob) != entry["byte_count"]:
            raise ModelCompatibilityError(
                "AI_MODEL_COMPATIBILITY_POLICY_REFUSED",
                "A v2 asset hash or byte count does not verify.",
            )
        return path, entry

    projection_path, projection_entry = asset(
        "agent_projection_policy",
        "finance_core/resources/finance_ai/agent_projection_policy_v2.json",
    )
    fixture_path, fixture_entry = asset(
        "model_compatibility_fixtures",
        "finance_core/resources/finance_ai/model_compatibility_fixture_set_v1.json",
    )
    _prompt_path, prompt_entry = asset("prompt", "finance_core/resources/finance_ai/prompt_v1.txt")
    projection = _load_json(projection_path)
    fixtures = _load_json(fixture_path)
    expected_policy = {
        "version": "finance-openclaw-agent-projection-policy-v2",
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "allowed_execution_classes": ["cloud_projection", "local_model"],
        "display_alias_max_utf8_bytes": 64,
        "fallbacks": [],
        "effective_max_retries": 0,
        "audit_caller_kind": AUDIT_CALLER_KIND,
        "audit_caller_id": AUDIT_CALLER_ID,
        "audit_purpose": AUDIT_PURPOSE,
    }
    if (
        projection.get("version") != projection_entry["version"]
        or fixtures.get("version") != fixture_entry["version"]
        or projection != expected_policy
    ):
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_POLICY_REFUSED", "A v2 asset version does not verify."
        )
    return {
        "projection": projection,
        "projection_sha256": projection_entry["sha256"],
        "fixtures": fixtures,
        "fixture_sha256": fixture_entry["sha256"],
        "prompt_version": prompt_entry["version"],
        "prompt_sha256": prompt_entry["sha256"],
    }


def _validate_config_projection_shape(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable v2 shape without consulting mutable current assets."""
    value = dict(projection)
    if set(value) != _PROJECTION_FIELDS:
        raise ModelCompatibilityError(
            "AI_MODEL_CONFIG_REFUSED", "Agent config projection fields are not exact."
        )
    if (
        value["schema_version"] != PROJECTION_SCHEMA_VERSION
        or value["agent_id"] != AGENT_ID
        or value["fallbacks"] != []
        or type(value["effective_max_retries"]) is not int
        or value["effective_max_retries"] != 0
        or value["execution_class"] not in {"local_model", "cloud_projection"}
    ):
        raise ModelCompatibilityError(
            "AI_MODEL_CONFIG_REFUSED", "Agent config projection violates the frozen v2 policy."
        )
    for field in (
        "openclaw_package_sha256",
        "plugin_build_sha256",
        "tool_policy_sha256",
        "memory_policy_sha256",
        "plugin_binding_policy_sha256",
        "projection_policy_sha256",
        "prompt_sha256",
    ):
        if not isinstance(value[field], str) or _HASH_RE.fullmatch(value[field]) is None:
            raise ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", f"{field} is invalid.")
    if (
        not isinstance(value["finance_commit"], str)
        or _COMMIT_RE.fullmatch(value["finance_commit"]) is None
    ):
        raise ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", "finance_commit is invalid.")
    for field in ("openclaw_version", "projection_policy_version", "prompt_version"):
        if not isinstance(value[field], str) or _VERSION_RE.fullmatch(value[field]) is None:
            raise ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", f"{field} is invalid.")
    if (
        not isinstance(value["canonical_provider"], str)
        or _PROVIDER_RE.fullmatch(value["canonical_provider"]) is None
    ):
        raise ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", "canonical_provider is invalid.")
    if (
        not isinstance(value["canonical_model"], str)
        or _MODEL_RE.fullmatch(value["canonical_model"]) is None
    ):
        raise ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", "canonical_model is invalid.")
    alias = value["display_alias"]
    if (
        not isinstance(alias, str)
        or _ALIAS_RE.fullmatch(alias) is None
        or _unsafe_alias_codepoint(alias)
        or alias != alias.strip()
        or len(alias.encode("utf-8")) > 64
    ):
        raise ModelCompatibilityError("AI_MODEL_CONFIG_REFUSED", "display_alias is invalid.")
    return value


def validate_config_projection(
    projection: Mapping[str, Any], *, repo_root: Path | None = None
) -> dict[str, Any]:
    """Return the exact current-policy projection or refuse it."""
    value = _validate_config_projection_shape(projection)
    assets = policy_assets(repo_root)
    policy = assets["projection"]
    if (
        value["projection_policy_version"] != policy.get("version")
        or value["projection_policy_sha256"] != assets["projection_sha256"]
        or value["prompt_version"] != assets["prompt_version"]
        or value["prompt_sha256"] != assets["prompt_sha256"]
    ):
        raise ModelCompatibilityError(
            "AI_MODEL_CONFIG_REFUSED", "Agent config projection violates the frozen v2 policy."
        )
    return value


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise _VerificationRefusal("RESPONSE_DUPLICATE_KEY")
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _VerificationRefusal("RESPONSE_UTF8_INVALID") from exc
    try:
        value = json.loads(decoded, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise _VerificationRefusal("RESPONSE_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise _VerificationRefusal("RESPONSE_NOT_OBJECT")
    return value


def _verify_response(case: Mapping[str, Any], response_raw: bytes) -> None:
    if len(response_raw) > MAX_RESPONSE_BYTES:
        raise _VerificationRefusal("RESPONSE_OVERSIZED")
    response = _strict_json_object(response_raw)
    try:
        layout = (
            verify_ocr_layout(case["ocr_layout"], parent_payload=case["parent_payload"])
            if case.get("ocr_layout") is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise _VerificationRefusal("RESPONSE_ADMISSION_INVALID") from exc
    try:
        response = normalize_fact_observations(
            response,
            catalog=case["catalog"],
            assessment=assess_source(
                catalog=case["catalog"],
                parent_payload=case["parent_payload"],
                source_kind=case["source_kind"],
                ocr_layout=layout,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise _VerificationRefusal("RESPONSE_SCHEMA_INVALID") from exc
    expected = case["expected"]
    policy = expected.get("acceptance_policy", "exact-v1")
    if policy not in {"exact-v1", "conservative-abstention-v1"}:
        raise _VerificationRefusal("ACCEPTANCE_POLICY_INVALID")
    abstention = policy == "conservative-abstention-v1"
    required_abstention_flags = {
        "ambiguous_amount",
        "missing_amount",
        "missing_currency",
        "missing_date",
        "source_conflict",
    }
    if abstention and (
        expected["amount"] is not None
        or expected["currency"] is not None
        or not required_abstention_flags.issubset(expected["ambiguity_flags"])
    ):
        raise _VerificationRefusal("ACCEPTANCE_POLICY_INVALID")
    flags = response["ambiguity_flags"]
    expected_flags = expected["ambiguity_flags"]
    missing_flags = [flag for flag in expected_flags if flag not in flags]
    extra_flags = [flag for flag in flags if flag not in expected_flags]
    if abstention:
        allowed_restrictions = {
            "ambiguous_currency",
            "ambiguous_date",
            "ambiguous_merchant",
        }
        extra_flags = [flag for flag in extra_flags if flag not in allowed_restrictions]
    if missing_flags and not extra_flags:
        raise _VerificationRefusal("AMBIGUITY_FLAGS_MISSING")
    if extra_flags and not missing_flags:
        raise _VerificationRefusal("AMBIGUITY_FLAGS_EXTRA")
    if missing_flags or extra_flags:
        raise _VerificationRefusal("AMBIGUITY_FLAGS_MISMATCH")
    confidence = response.get("field_confidence_bps")
    if not isinstance(confidence, dict) or set(confidence) != _PROPOSAL_FIELDS:
        raise _VerificationRefusal("CONFIDENCE_SHAPE_INVALID")
    for field in sorted(_PROPOSAL_FIELDS):
        value = confidence[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000
        ):
            raise _VerificationRefusal("CONFIDENCE_VALUE_INVALID", field=field)
    refs = response.get("field_evidence_refs")
    if not isinstance(refs, dict) or set(refs) != _PROPOSAL_FIELDS:
        raise _VerificationRefusal("EVIDENCE_REFS_SHAPE_INVALID")
    expected = case["expected"]
    for field in (
        "intent_type",
        "amount",
        "currency",
        "transaction_date",
        "merchant",
        "description",
        "account",
        "category",
    ):
        if abstention and field == "merchant" and response.get(field) is None:
            continue
        if response.get(field) != expected[field]:
            raise _VerificationRefusal("FIELD_MISMATCH", field=field)
    catalog_refs = set(case["catalog"])
    for field in sorted(_PROPOSAL_FIELDS):
        field_refs = refs.get(field)
        if (
            not isinstance(field_refs, list)
            or len(field_refs) > 16
            or len(field_refs) != len(set(field_refs))
            or any(not isinstance(ref, str) or ref not in catalog_refs for ref in field_refs)
        ):
            raise _VerificationRefusal("EVIDENCE_REFERENCE_INVALID", field=field)
        if response.get(field) is None and field_refs:
            raise _VerificationRefusal("NULL_FIELD_HAS_EVIDENCE", field=field)
        if response.get(field) is not None and not field_refs:
            raise _VerificationRefusal("PRESENT_FIELD_LACKS_EVIDENCE", field=field)
    decoded = response_raw.decode("utf-8")
    if any(fragment in decoded for fragment in case["forbidden_output_fragments"]):
        raise _VerificationRefusal("PROMPT_INJECTION_ECHO")
    # Keep precise fixture diagnostics above, but never accept a case that the
    # real proposal path refuses. Both paths use this exact pure admission core.
    try:
        validate_ai_response(
            response_raw,
            catalog_refs,
            parent_payload=case["parent_payload"],
            catalog=case["catalog"],
            source_kind=case["source_kind"],
            ocr_layout=layout,
        )
    except (TypeError, ValueError) as exc:
        raise _VerificationRefusal("RESPONSE_ADMISSION_INVALID") from exc


def _verify_harness_outcome(
    projection: Mapping[str, Any],
    case: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        if not isinstance(outcome, Mapping) or set(outcome) != _OUTCOME_FIELDS:
            raise _VerificationRefusal("HARNESS_OUTCOME_FIELDS_INVALID")
        case_id = case["case_id"]
        if outcome.get("case_id") != case_id:
            raise _VerificationRefusal("CASE_IDENTITY_INVALID")
        if (
            type(outcome["ordinary_agent_turn_count"]) is not int
            or outcome["ordinary_agent_turn_count"] != 0
            or type(outcome["isolated_completion_count"]) is not int
            or outcome["isolated_completion_count"] != 1
            or type(outcome["provider_dispatch_count"]) is not int
            or outcome["provider_dispatch_count"] != 1
            or type(outcome["effective_max_retries"]) is not int
            or outcome["effective_max_retries"] != 0
            or isinstance(outcome["elapsed_ms"], bool)
            or not isinstance(outcome["elapsed_ms"], int)
            or not 0 <= outcome["elapsed_ms"] < MAX_ELAPSED_MS
            or outcome["observed_provider"] != projection["canonical_provider"]
            or outcome["observed_model"] != projection["canonical_model"]
            or outcome["observed_agent_id"] != AGENT_ID
        ):
            raise _VerificationRefusal("HARNESS_CONTRACT_INVALID")
        encoded = outcome["response_utf8_b64"]
        if not isinstance(encoded, str) or len(encoded) > MAX_OUTCOME_BYTES * 2:
            raise _VerificationRefusal("RESPONSE_ENCODING_INVALID")
        try:
            response_raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _VerificationRefusal("RESPONSE_ENCODING_INVALID") from exc
        _verify_response(case, response_raw)
        return {
            "case_id": case_id,
            "ordinary_agent_turn_count": 0,
            "isolated_completion_count": 1,
            "provider_dispatch_count": 1,
            "effective_max_retries": 0,
            "elapsed_ms": outcome["elapsed_ms"],
            "observed_provider": outcome["observed_provider"],
            "observed_model": outcome["observed_model"],
            "observed_agent_id": outcome["observed_agent_id"],
            "response_sha256": _sha256(response_raw),
        }
    except _VerificationRefusal as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_EVAL_REFUSED",
            "A fixed compatibility case did not pass Python verification.",
            details=_verification_details(exc.reason, field=exc.field),
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_EVAL_REFUSED",
            "A fixed compatibility case did not pass Python verification.",
            details=_verification_details("VERIFIER_INPUT_INVALID"),
        ) from exc


def verify_harness_outcome(
    projection: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Recompute one fixed-case verdict without opening a database."""
    validated_projection = validate_config_projection(projection, repo_root=repo_root)
    assets = policy_assets(repo_root)
    cases = assets["fixtures"].get("cases")
    if not isinstance(cases, list):
        raise ModelCompatibilityError(
            "AI_MODEL_EVAL_REFUSED", "The fixed compatibility fixture set is invalid."
        )
    case_id = outcome.get("case_id") if isinstance(outcome, Mapping) else None
    matching = [
        case for case in cases if isinstance(case, Mapping) and case.get("case_id") == case_id
    ]
    if len(matching) != 1 or case_id not in FIXED_FIXTURE_CASE_IDS:
        raise ModelCompatibilityError(
            "AI_MODEL_EVAL_REFUSED",
            "Harness case identity is invalid.",
            details=_verification_details("CASE_IDENTITY_INVALID"),
        )
    return _verify_harness_outcome(validated_projection, matching[0], outcome)


def verify_harness_outcomes(
    projection: Mapping[str, Any],
    outcomes: Sequence[Mapping[str, Any]],
    *,
    repo_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recompute every contract and four-case verdict from bounded raw results."""
    assets = policy_assets(repo_root)
    fixture_set = assets["fixtures"]
    cases = fixture_set.get("cases")
    if (
        not isinstance(cases, list)
        or tuple(case.get("case_id") for case in cases if isinstance(case, dict))
        != FIXED_FIXTURE_CASE_IDS
        or len(outcomes) != 4
    ):
        raise ModelCompatibilityError(
            "AI_MODEL_EVAL_REFUSED", "The fixed four-case compatibility harness is incomplete."
        )
    by_id: dict[str, Mapping[str, Any]] = {}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping) or set(outcome) != _OUTCOME_FIELDS:
            raise ModelCompatibilityError(
                "AI_MODEL_EVAL_REFUSED", "A raw harness outcome has invalid fields."
            )
        case_id = outcome.get("case_id")
        if not isinstance(case_id, str) or case_id in by_id:
            raise ModelCompatibilityError(
                "AI_MODEL_EVAL_REFUSED", "Harness case identities are invalid."
            )
        by_id[case_id] = outcome
    try:
        verified = [
            _verify_harness_outcome(projection, case, by_id[case["case_id"]]) for case in cases
        ]
    except KeyError as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_EVAL_REFUSED", "Harness case identities are invalid."
        ) from exc
    return verified, assets


# Registration and later preparation share this immutable identity chain:
# projection + raw outcomes -> Python verify -> receipt
# receipt + intake         -> [attempt + unique link] -> one transaction
def register_ai_model_compatibility_receipt_v2(
    conn: sqlite3.Connection,
    *,
    config_projection: Mapping[str, Any],
    harness_outcomes: Sequence[Mapping[str, Any]],
    now_ms: int | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and append/replay one compatibility receipt.

    State: raw observations -> Python verdict -> immutable receipt.  Registration
    never invokes a model, changes runtime state, or accepts caller pass/fail.
    """
    require_staging_database(conn)
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_FK_REFUSED", "Foreign keys must be enabled."
        ) from exc
    projection = validate_config_projection(config_projection, repo_root=repo_root)
    verified_results, assets = verify_harness_outcomes(
        projection, harness_outcomes, repo_root=repo_root
    )
    projection_hash = canonical_projection_hash(projection)
    results_json = _canonical_json_bytes(verified_results).decode("utf-8")
    results_hash = _sha256(results_json.encode("utf-8"))
    material = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "config_projection_hash": projection_hash,
        "fixture_set_version": assets["fixtures"]["version"],
        "fixture_set_sha256": assets["fixture_sha256"],
        "fixture_results_sha256": results_hash,
        "verification_method": VERIFICATION_METHOD,
        "freshness_rule": FRESHNESS_RULE,
        "operator_workflow_version": OPERATOR_WORKFLOW_VERSION,
    }
    receipt_hash = _material_hash("finance-ai-model-compatibility-receipt-v2", material)
    receipt_public_id = f"aimr_{receipt_hash}"
    issued_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if isinstance(issued_at_ms, bool) or not isinstance(issued_at_ms, int) or issued_at_ms < 0:
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_ARGUMENTS_REFUSED", "now_ms is invalid."
        )
    values = (
        receipt_public_id,
        receipt_hash,
        RECEIPT_SCHEMA_VERSION,
        _canonical_json_bytes(projection).decode("utf-8"),
        projection_hash,
        projection["openclaw_version"],
        projection["openclaw_package_sha256"],
        projection["finance_commit"],
        projection["plugin_build_sha256"],
        AGENT_ID,
        projection["canonical_provider"],
        projection["canonical_model"],
        projection["display_alias"],
        projection["execution_class"],
        "[]",
        0,
        projection["tool_policy_sha256"],
        projection["memory_policy_sha256"],
        projection["plugin_binding_policy_sha256"],
        projection["projection_policy_version"],
        projection["projection_policy_sha256"],
        projection["prompt_version"],
        projection["prompt_sha256"],
        assets["fixtures"]["version"],
        assets["fixture_sha256"],
        results_json,
        results_hash,
        VERIFICATION_METHOD,
        FRESHNESS_RULE,
        OPERATOR_WORKFLOW_VERSION,
        issued_at_ms,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM ai_model_compatibility_receipts WHERE receipt_public_id = ?",
            (receipt_public_id,),
        ).fetchone()
        projection_existing = conn.execute(
            "SELECT * FROM ai_model_compatibility_receipts WHERE config_projection_hash = ?",
            (projection_hash,),
        ).fetchone()
        if projection_existing is not None and (
            existing is None or projection_existing["id"] != existing["id"]
        ):
            raise ModelCompatibilityError(
                "AI_MODEL_COMPATIBILITY_CONFLICT",
                "The exact Agent config is already bound to different eval material.",
            )
        replay = existing is not None
        if existing is None:
            conn.execute(
                """
                INSERT INTO ai_model_compatibility_receipts (
                    receipt_public_id, receipt_material_hash, schema_version,
                    config_projection_json, config_projection_hash, openclaw_version,
                    openclaw_package_sha256, finance_commit, plugin_build_sha256,
                    agent_id, canonical_provider, canonical_model, display_alias,
                    execution_class, fallbacks_json, effective_max_retries,
                    tool_policy_sha256, memory_policy_sha256,
                    plugin_binding_policy_sha256, projection_policy_version,
                    projection_policy_sha256, prompt_version, prompt_sha256,
                    fixture_set_version, fixture_set_sha256, fixture_results_json,
                    fixture_results_sha256, verification_method, freshness_rule,
                    operator_workflow_version, issued_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
        elif existing["receipt_material_hash"] != receipt_hash:
            raise ModelCompatibilityError(
                "AI_MODEL_COMPATIBILITY_CONFLICT", "Receipt identity collided."
            )
        else:
            verify_persisted_compatibility_receipt(dict(existing))
        conn.commit()
    except ModelCompatibilityError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_INTERNAL", "Receipt persistence failed."
        ) from exc
    return {
        "receipt_public_id": receipt_public_id,
        "receipt_material_hash": receipt_hash,
        "config_projection_hash": projection_hash,
        "canonical_model": f"{projection['canonical_provider']}/{projection['canonical_model']}",
        "idempotent_replay": replay,
    }


def resolve_compatibility_receipt(
    conn: sqlite3.Connection,
    *,
    config_projection: Mapping[str, Any],
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Resolve the exact receipt for the current projection; never use current config as history."""
    projection = validate_config_projection(config_projection, repo_root=repo_root)
    projection_json = _canonical_json_bytes(projection).decode("utf-8")
    projection_hash = canonical_projection_hash(projection)
    rows = conn.execute(
        "SELECT * FROM ai_model_compatibility_receipts WHERE config_projection_hash = ?",
        (projection_hash,),
    ).fetchall()
    matching = [dict(row) for row in rows if row["config_projection_json"] == projection_json]
    if len(matching) != 1:
        raise ModelCompatibilityError(
            "AI_MODEL_CONFIG_NOT_ACCEPTED", "The exact Agent config has no unique accepted receipt."
        )
    verify_persisted_compatibility_receipt(matching[0])
    return matching[0]


def compatibility_receipt_for_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
) -> dict[str, Any] | None:
    """Resolve and verify both immutable receipt and deterministic attempt binding."""
    row = conn.execute(
        """
        SELECT link.link_public_id AS binding_public_id,
               link.link_material_hash AS binding_material_hash,
               attempt.attempt_public_id AS bound_attempt_public_id,
               receipt.*
        FROM ai_fallback_attempt_compatibility_receipts AS link
        JOIN ai_fallback_attempts AS attempt ON attempt.id = link.attempt_id
        JOIN ai_model_compatibility_receipts AS receipt ON receipt.id = link.receipt_id
        WHERE link.attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = dict(row)
    verify_persisted_compatibility_receipt(receipt)
    link_hash = _material_hash(
        "finance-ai-attempt-receipt-link-v2",
        {
            "attempt_public_id": receipt["bound_attempt_public_id"],
            "receipt_public_id": receipt["receipt_public_id"],
            "receipt_material_hash": receipt["receipt_material_hash"],
        },
    )
    if (
        receipt["binding_material_hash"] != link_hash
        or receipt["binding_public_id"] != f"aiml_{link_hash}"
    ):
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_CONFLICT", "Persisted attempt receipt binding does not verify."
        )
    return receipt


def verify_persisted_compatibility_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild a stored receipt's canonical material before any replay or use."""
    try:
        projection_value = _strict_json_object(
            str(receipt["config_projection_json"]).encode("utf-8")
        )
        projection = _validate_config_projection_shape(projection_value)
        results = json.loads(str(receipt["fixture_results_json"]))
        if not isinstance(results, list) or len(results) != 4:
            raise ValueError("fixture results invalid")
        if (
            not isinstance(receipt["fixture_set_version"], str)
            or _VERSION_RE.fullmatch(receipt["fixture_set_version"]) is None
            or not isinstance(receipt["fixture_set_sha256"], str)
            or _HASH_RE.fullmatch(receipt["fixture_set_sha256"]) is None
        ):
            raise ValueError("fixture binding invalid")
        expected_result_fields = {
            "case_id",
            "ordinary_agent_turn_count",
            "isolated_completion_count",
            "provider_dispatch_count",
            "effective_max_retries",
            "elapsed_ms",
            "observed_provider",
            "observed_model",
            "observed_agent_id",
            "response_sha256",
        }
        for result, case_id in zip(results, FIXED_FIXTURE_CASE_IDS, strict=True):
            if (
                not isinstance(result, dict)
                or set(result) != expected_result_fields
                or result["case_id"] != case_id
                or type(result["ordinary_agent_turn_count"]) is not int
                or result["ordinary_agent_turn_count"] != 0
                or type(result["isolated_completion_count"]) is not int
                or result["isolated_completion_count"] != 1
                or type(result["provider_dispatch_count"]) is not int
                or result["provider_dispatch_count"] != 1
                or type(result["effective_max_retries"]) is not int
                or result["effective_max_retries"] != 0
                or isinstance(result["elapsed_ms"], bool)
                or not isinstance(result["elapsed_ms"], int)
                or not 0 <= result["elapsed_ms"] < MAX_ELAPSED_MS
                or result["observed_provider"] != projection["canonical_provider"]
                or result["observed_model"] != projection["canonical_model"]
                or result["observed_agent_id"] != AGENT_ID
                or not isinstance(result["response_sha256"], str)
                or _HASH_RE.fullmatch(result["response_sha256"]) is None
            ):
                raise ValueError("fixture result summary invalid")
        results_json = _canonical_json_bytes(results).decode("utf-8")
        results_hash = _sha256(results_json.encode("utf-8"))
        projection_hash = canonical_projection_hash(projection)
        expected_columns = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "config_projection_hash": projection_hash,
            "openclaw_version": projection["openclaw_version"],
            "openclaw_package_sha256": projection["openclaw_package_sha256"],
            "finance_commit": projection["finance_commit"],
            "plugin_build_sha256": projection["plugin_build_sha256"],
            "agent_id": projection["agent_id"],
            "canonical_provider": projection["canonical_provider"],
            "canonical_model": projection["canonical_model"],
            "display_alias": projection["display_alias"],
            "execution_class": projection["execution_class"],
            "fallbacks_json": "[]",
            "effective_max_retries": 0,
            "tool_policy_sha256": projection["tool_policy_sha256"],
            "memory_policy_sha256": projection["memory_policy_sha256"],
            "plugin_binding_policy_sha256": projection["plugin_binding_policy_sha256"],
            "projection_policy_version": projection["projection_policy_version"],
            "projection_policy_sha256": projection["projection_policy_sha256"],
            "prompt_version": projection["prompt_version"],
            "prompt_sha256": projection["prompt_sha256"],
            "fixture_set_version": receipt["fixture_set_version"],
            "fixture_set_sha256": receipt["fixture_set_sha256"],
            "fixture_results_sha256": results_hash,
            "verification_method": VERIFICATION_METHOD,
            "freshness_rule": FRESHNESS_RULE,
            "operator_workflow_version": OPERATOR_WORKFLOW_VERSION,
        }
        if any(receipt[field] != expected for field, expected in expected_columns.items()):
            raise ValueError("receipt column mismatch")
        material = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "config_projection_hash": projection_hash,
            "fixture_set_version": receipt["fixture_set_version"],
            "fixture_set_sha256": receipt["fixture_set_sha256"],
            "fixture_results_sha256": results_hash,
            "verification_method": VERIFICATION_METHOD,
            "freshness_rule": FRESHNESS_RULE,
            "operator_workflow_version": OPERATOR_WORKFLOW_VERSION,
        }
        receipt_hash = _material_hash("finance-ai-model-compatibility-receipt-v2", material)
        if (
            receipt["receipt_material_hash"] != receipt_hash
            or receipt["receipt_public_id"] != f"aimr_{receipt_hash}"
        ):
            raise ValueError("receipt identity mismatch")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_CONFLICT", "Persisted compatibility receipt does not verify."
        ) from exc
    return projection


def link_attempt_to_receipt(conn: sqlite3.Connection, *, attempt_id: int, receipt_id: int) -> str:
    """Insert a deterministic link inside the caller-owned attempt transaction."""
    require_staging_database(conn)
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_FK_REFUSED", "Foreign keys must be enabled."
        ) from exc
    persisted_row = conn.execute(
        "SELECT * FROM ai_model_compatibility_receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    attempt_row = conn.execute(
        "SELECT attempt_public_id FROM ai_fallback_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if persisted_row is None or attempt_row is None:
        raise ModelCompatibilityError(
            "AI_MODEL_COMPATIBILITY_CONFLICT", "Attempt receipt binding target is missing."
        )
    persisted = dict(persisted_row)
    verify_persisted_compatibility_receipt(persisted)
    material = {
        "attempt_public_id": attempt_row["attempt_public_id"],
        "receipt_public_id": persisted["receipt_public_id"],
        "receipt_material_hash": persisted["receipt_material_hash"],
    }
    link_hash = _material_hash("finance-ai-attempt-receipt-link-v2", material)
    link_public_id = f"aiml_{link_hash}"
    conn.execute(
        """
        INSERT INTO ai_fallback_attempt_compatibility_receipts (
            link_public_id, link_material_hash, attempt_id, receipt_id
        ) VALUES (?, ?, ?, ?)
        """,
        (link_public_id, link_hash, attempt_id, persisted["id"]),
    )
    return link_public_id


__all__ = [
    "AGENT_ID",
    "AUDIT_CALLER_ID",
    "AUDIT_CALLER_KIND",
    "AUDIT_PURPOSE",
    "ModelCompatibilityError",
    "canonical_projection_hash",
    "compatibility_receipt_for_attempt",
    "link_attempt_to_receipt",
    "policy_assets",
    "register_ai_model_compatibility_receipt_v2",
    "resolve_compatibility_receipt",
    "validate_config_projection",
    "verify_persisted_compatibility_receipt",
    "verify_harness_outcome",
    "verify_harness_outcomes",
]
